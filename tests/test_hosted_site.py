"""hosted/: the build of the hosted web app's site repository (its
README.md). build.py verifies a release tag the way the updater does and
builds the site from its web app; deploy.yml runs it hourly with pinned
actions and no secret.

Everything runs against a fake origin repository whose tags a key made in
the test signs. Nothing reaches the network."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOSTED = REPO / "hosted"
spec = importlib.util.spec_from_file_location("hosted_build", HOSTED / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

GIT_ID = ["-c", "user.name=Release Test", "-c", "user.email=release@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]
APP = {
    "index.html": "<!doctype html><title>Tacitus</title>\n",
    "sw.js": "self.addEventListener('fetch', () => {});\n",
    "app.js": "console.log('app');\n",
    "icons/icon-192.png": "png",
    ".hidden": "never served\n",
    "has space.txt": "a path a manifest cannot name\n",
}


def _git_ok() -> bool:
    if not shutil.which("ssh-keygen"):
        return False
    m = re.search(r"(\d+)\.(\d+)", subprocess.run(["git", "--version"], capture_output=True,
                                                  text=True).stdout)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 34)


pytestmark = pytest.mark.skipif(not _git_ok(), reason="needs ssh-keygen and git 2.34+ (SSH signing)")


def git(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    r = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def keypair(d: Path, name: str) -> Path:
    key = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
                   check=True)
    return key


def sign_tag(repo: Path, tag: str, key: Path | None, rev: str = "HEAD") -> None:
    if key is None:
        git(repo, "tag", "-a", "-m", tag, tag, rev)
    else:
        git(repo, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s",
            "-m", tag, tag, rev)


def write(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def commit(repo: Path, rel: str, text: str, msg: str = "change") -> str:
    write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    """(origin, key, pin text, fetched bare repository)."""
    key = keypair(tmp_path, "release")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    for rel, text in APP.items():
        write(origin, f"{build.APP_DIR}/{rel}", text)
    write(origin, "README.md", "tacitus\n")
    shutil.copy(str(key) + ".pub", origin / ".release-key.pub")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "first")
    pin = (Path(str(key) + ".pub")).read_text()
    src = tmp_path / "src.git"

    def refetch():
        build.fetch(src, str(origin))
        return src

    return origin, key, pin, refetch


# ── verification ─────────────────────────────────────────────────────

def test_a_signed_tag_on_main_verifies(world):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", key)
    src = refetch()
    assert build.verify_release(src, "v1.0.0", pin) == git(origin, "rev-parse", "HEAD")
    tag, commit_id, notes = build.newest_release(src, pin)
    assert (tag, notes) == ("v1.0.0", [])


def test_an_unsigned_or_lightweight_tag_is_refused(world):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", None)
    git(origin, "tag", "v1.0.1")
    src = refetch()
    with pytest.raises(build.VerifyError, match="no SSH signature"):
        build.verify_release(src, "v1.0.0", pin)
    with pytest.raises(build.VerifyError, match="not an annotated tag"):
        build.verify_release(src, "v1.0.1", pin)
    assert build.newest_release(src, pin) is None
    assert build.main(["pick", "--src", str(src), "--key", str(key) + ".pub"]) == 1


def test_a_tag_signed_by_another_key_is_refused(world, tmp_path):
    origin, key, pin, refetch = world
    other = keypair(tmp_path, "other")
    sign_tag(origin, "v1.0.0", other)
    src = refetch()
    with pytest.raises(build.VerifyError, match="not from the release key"):
        build.verify_release(src, "v1.0.0", pin)


def test_a_commit_off_main_is_refused(world):
    origin, key, pin, refetch = world
    git(origin, "checkout", "-qb", "side")
    commit(origin, f"{build.APP_DIR}/app.js", "console.log('changed');\n", "off main")
    sign_tag(origin, "v1.0.0", key)
    git(origin, "checkout", "-q", "main")
    src = refetch()
    with pytest.raises(build.VerifyError, match="not in the history of main"):
        build.verify_release(src, "v1.0.0", pin)


def test_a_release_that_adds_a_key_waits_for_the_pin(world, tmp_path):
    origin, key, pin, refetch = world
    other = keypair(tmp_path, "next")
    commit(origin, ".release-key.pub", pin + Path(str(other) + ".pub").read_text(), "new key")
    sign_tag(origin, "v1.1.0", key)
    src = refetch()
    with pytest.raises(build.VerifyError, match="changes .release-key.pub"):
        build.verify_release(src, "v1.1.0", pin)
    both = pin + Path(str(other) + ".pub").read_text()
    assert build.verify_release(src, "v1.1.0", both)


def test_a_tag_that_poses_as_another_is_refused(world):
    origin, key, pin, refetch = world
    sign_tag(origin, "v0.1.0", key)
    obj = git(origin, "rev-parse", "refs/tags/v0.1.0")
    git(origin, "update-ref", "refs/tags/v9.9.9", obj)
    src = refetch()
    with pytest.raises(build.VerifyError, match="holds the tag object of 'v0.1.0'"):
        build.verify_release(src, "v9.9.9", pin)
    assert build.newest_release(src, pin)[0] == "v0.1.0"


def test_the_newest_tag_that_fails_gives_way_to_the_one_before(world):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", key)
    commit(origin, f"{build.APP_DIR}/app.js", "console.log('two');\n")
    sign_tag(origin, "v1.0.1", None)
    src = refetch()
    tag, _commit, notes = build.newest_release(src, pin)
    assert tag == "v1.0.0" and len(notes) == 1 and "v1.0.1" in notes[0]


def test_the_pin_takes_only_ed25519_keys(tmp_path):
    with pytest.raises(build.VerifyError, match="a release key is ssh-ed25519"):
        build.release_keys("ssh-rsa " + "A" * 60 + " x\n")
    with pytest.raises(build.VerifyError, match="holds no key"):
        build.release_keys("# nothing\n")
    with pytest.raises(build.VerifyError, match="nothing can be verified"):
        build.read_pin(tmp_path / "missing.pub")


def test_the_checks_match_the_updater():
    """build.py repeats scripts/update.py's checks; the two agree on the
    allowed-signers line and the fetched refs."""
    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(REPO / "preprocessor"))
    import update
    key = (REPO / ".release-key.pub").read_text()
    assert build.allowed_signers(key) == update.allowed_signers(key)
    assert build.PRINCIPAL == update.PRINCIPAL and build.KEY_TYPE == update.KEY_TYPE
    assert build.MAIN_REF == update.MAIN_REF and build.REPO_URL == update.REPO_URL
    assert set(build.FETCH_REFSPECS) <= set(update.FETCH_REFSPECS)
    for line in ('Good "git" signature for {PRINCIPAL} with ED25519 key',
                 "-----BEGIN SSH SIGNATURE-----", "merge-base", "--is-ancestor"):
        assert line in (HOSTED / "build.py").read_text(), line


# ── the built tree ───────────────────────────────────────────────────

def test_the_build_writes_the_app_the_relays_and_the_release(world, tmp_path):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.2.3", key)
    src = refetch()
    out = tmp_path / "site"
    relays = ["https://relay.example.org/relay"]
    got = build.build(src, "v1.2.3", pin, out, relays)
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == ["app.js", "icons/icon-192.png", "index.html", "relays.json", "release.txt",
                     "sw.js"]
    assert (out / "sw.js").read_text() == APP["sw.js"]
    assert json.loads((out / "relays.json").read_text()) == relays
    head = git(origin, "rev-parse", "HEAD")
    text = (out / "release.txt").read_text()
    assert text == f"tag v1.2.3\ncommit {head}\nfiles {got['files']}\n"
    assert build.parse_release(text) == {"tag": "v1.2.3", "commit": head, "files": got["files"]}


def test_the_files_hash_is_the_bundle_s_file_map(world, tmp_path):
    """The `files` line is the sha256 of the canonical file map that
    web_bundle.file_hashes makes from the same folder."""
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", key)
    src = refetch()
    got = build.build(src, "v1.0.0", pin, tmp_path / "site", [])
    sys.path.insert(0, str(REPO / "botcore"))
    from botcore.chat import relay_crypto, web_bundle
    files, _newest, skipped = web_bundle.file_hashes(origin / build.APP_DIR)
    assert skipped == ["has space.txt"]
    assert got["files"] == hashlib.sha256(relay_crypto.canonical_json(files)).hexdigest()


def test_the_real_app_builds_to_the_bundle_s_file_map():
    sys.path.insert(0, str(REPO / "botcore"))
    from botcore.chat import web_bundle
    files, _newest, _skipped = web_bundle.file_hashes(REPO / build.APP_DIR)
    head = git(REPO, "rev-parse", "HEAD")
    mine, _left = build.app_files(REPO, head)
    tracked = set(git(REPO, "ls-files", build.APP_DIR).splitlines())
    changed = set(git(REPO, "diff", "--name-only", "HEAD", "--", build.APP_DIR).splitlines())
    want = {p: h for p, h in files.items() if f"{build.APP_DIR}/{p}" in tracked}
    assert set(mine) == set(want)
    for p, data in mine.items():
        if f"{build.APP_DIR}/{p}" not in changed:
            assert hashlib.sha256(data).hexdigest() == want[p], p
    assert "index.html" in mine and "sw.js" in mine
    assert build.RELAYS_NAME not in mine and build.RELEASE_NAME not in mine


def test_the_build_checks_the_tag_again(world, tmp_path):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", None)
    src = refetch()
    out = tmp_path / "site"
    with pytest.raises(build.VerifyError):
        build.build(src, "v1.0.0", pin, out, [])
    assert not out.exists()
    sign_tag(origin, "v1.0.1", key)
    src = refetch()
    with pytest.raises(build.VerifyError, match="not 000000000000"):
        build.build(src, "v1.0.1", pin, out, [], expect_commit="0" * 40)


def test_the_command_line_builds_with_or_without_relays(world, tmp_path):
    origin, key, pin, refetch = world
    sign_tag(origin, "v2.0.0", key)
    src = refetch()
    relays = tmp_path / "relays.json"
    relays.write_text('["https://relay.example.org/relay"]')
    out = tmp_path / "site"
    args = ["build", "--src", str(src), "--key", str(key) + ".pub", "--tag", "v2.0.0",
            "--out", str(out)]
    assert build.main(args + ["--relays", str(relays)]) == 0
    assert json.loads((out / "relays.json").read_text()) == ["https://relay.example.org/relay"]
    assert build.main(args) == 0
    assert json.loads((out / "relays.json").read_text()) == []
    relays.write_text('["http://relay.example.org/relay"]')
    assert build.main(args + ["--relays", str(relays)]) == 1


def test_a_relay_list_names_only_https_relays():
    good = ["https://relay.example.org/relay", "https://relay.example.org:8443/r"]
    assert build.check_relays(good) == good
    assert build.check_relays([]) == []
    for bad in (["http://relay.example.org/relay"], ["https://relay.example.org/relay/"],
                ["https://user@relay.example.org/relay"], ["https://relay.example.org/r?x=1"],
                ["https://relay.example.org/r#x"], ["javascript:alert(1)"], [" https://a.example.org"],
                ["https://relay.example.org/a b"], [1], "https://relay.example.org/relay",
                {"r": 1}, ["https://relay.example.org/r"] * 2,
                [f"https://r{i}.example.org/r" for i in range(17)]):
        with pytest.raises(build.BuildError):
            build.check_relays(bad)


def test_the_example_relay_list_is_a_valid_list():
    doc = json.loads((HOSTED / "relays.json.example").read_text())
    assert doc == ["https://relay.example.org/relay"]
    assert build.check_relays(doc) == doc


# ── what the site serves now ─────────────────────────────────────────

def test_the_run_skips_a_site_that_is_current():
    assert build.should_deploy("v1.2.0", {}) == (True, "v1.2.0: the site has no release yet")
    assert build.should_deploy("v1.2.0", {"tag": "v1.2.0"})[0] is False
    assert build.should_deploy("v1.2.0", {"tag": "v1.10.0"})[0] is False, "never moved back"
    assert build.should_deploy("v1.10.0", {"tag": "v1.2.0"})[0] is True
    assert build.should_deploy("v1.2.0", {"tag": "v1.2.0"}, force=True)[0] is True


def test_the_deployed_release_is_read_and_checked():
    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body[:n]

    seen = []

    def opener(req, timeout):
        seen.append(req.full_url)
        return Answer(b"tag v1.2.0\ncommit " + b"a" * 40 + b"\nfiles " + b"b" * 64 + b"\n")

    assert build.deployed("https://site.example.org/", opener) == {
        "tag": "v1.2.0", "commit": "a" * 40, "files": "b" * 64}
    assert seen == ["https://site.example.org/release.txt"]
    assert build.parse_release("tag main\ncommit xyz\n") == {}

    def down(req, timeout):
        raise build.urllib.error.URLError("no route")

    assert build.deployed("https://site.example.org/", down) == {}
    assert build.deployed("", opener) == {}


def test_pick_writes_the_workflow_outputs(world, tmp_path):
    origin, key, pin, refetch = world
    sign_tag(origin, "v1.0.0", key)
    src = refetch()
    out = tmp_path / "out.txt"
    assert build.main(["pick", "--src", str(src), "--key", str(key) + ".pub",
                       "--github-output", str(out)]) == 0
    head = git(origin, "rev-parse", "HEAD")
    assert out.read_text() == f"tag=v1.0.0\ncommit={head}\ndeploy=yes\n"


# ── the workflow and the README ──────────────────────────────────────

def test_the_workflow_pins_every_action_and_holds_no_secret():
    wf = (HOSTED / "deploy.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)(.*)", wf)
    assert {u.split("@")[0] for u, _ in uses} == {
        "actions/checkout", "actions/configure-pages", "actions/upload-pages-artifact",
        "actions/deploy-pages"}
    for ref, comment in uses:
        assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r"\s*# v\d+\.\d+\.\d+", comment), ref
    assert "secrets." not in wf and "GITHUB_TOKEN" not in wf
    assert re.search(r"schedule:\n\s+- cron: \"\d+ \* \* \* \*\"", wf), "hourly"
    assert "workflow_dispatch:" in wf
    # The workflow's own permissions read; only the deploy job writes Pages.
    top = wf.split("\njobs:", 1)[0]
    assert "permissions:\n  contents: read\n" in top and "pages: write" not in top
    deploy = wf.split("\n  deploy:", 1)[1]
    for want in ("contents: read", "pages: write", "id-token: write", "name: github-pages",
                 "needs.check.outputs.deploy == 'yes'"):
        assert want in deploy, want
    check = wf.split("\n  check:", 1)[1].split("\n  deploy:", 1)[0]
    assert "permissions:" not in check and "pages: write" not in check
    assert wf.count("persist-credentials: false") == 2
    assert "SOURCE: https://github.com/arpg/tacitus.git" in wf
    # The tag reaches the shell through the environment, never the template.
    for line in wf.splitlines():
        if "run:" in line or line.startswith("          python3"):
            assert "${{" not in line, line


def test_the_workflow_actions_match_the_repo_pins():
    """checkout is pinned at the commit the repository's own release job
    uses."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    pinned = re.search(r"actions/checkout@([0-9a-f]{40}) # (v[\d.]+)", ci)
    wf = (HOSTED / "deploy.yml").read_text()
    assert f"actions/checkout@{pinned.group(1)} # {pinned.group(2)}" in wf


def test_the_readme_keeps_write_access_to_the_owner():
    text = (HOSTED / "README.md").read_text()
    assert "Only the owner of the Tacitus project keeps write access here." in text
    assert "whoever can push to this repository decides what code this" in text
    assert "bot account" in text and "sw.js" in text
    assert "release-key.pub" in text and "relays.json" in text and "release.txt" in text
