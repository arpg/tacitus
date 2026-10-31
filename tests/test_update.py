"""The updater (scripts/update.py, docs/updates.md): channels, signatures,
the release layout and its swap, the health gate and its rollback, the idle
check, the status file and what the doctor, the bot and the briefing show.

Everything runs in temp directories against a fake origin repository whose
tags a key made in the test signs. Nothing reaches the network, /opt, the
EA user's home or a real service: the host is a fake."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "preprocessor"))
import update  # noqa: E402
import update_status as us  # noqa: E402

# A test that reads the origin repository itself names its own main; the
# updater's cache holds the fetched main as update.MAIN_REF.
MAIN = "refs/heads/main"
GIT_ID = ["-c", "user.name=Release Test", "-c", "user.email=release@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]


def _git_ok() -> bool:
    if not shutil.which("ssh-keygen"):
        return False
    m = re.search(r"(\d+)\.(\d+)", subprocess.run(["git", "--version"], capture_output=True,
                                                  text=True).stdout)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 34)


needs_ssh = pytest.mark.skipif(not _git_ok(), reason="needs ssh-keygen and git 2.34+ (SSH signing)")


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


def sign_tag(repo: Path, tag: str, key: Path | None, rev: str = "HEAD", msg: str = "") -> None:
    if key is None:
        git(repo, "tag", "-a", "-m", msg or tag, tag, rev)
    else:
        git(repo, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s",
            "-m", msg or tag, tag, rev)


# ── a fake release tree and origin ──────────────────────────────────

MINI = {
    "ea-chat-bot.py": "print('bot')\n",
    "run-agent.sh": "#!/bin/bash\necho agent\n",
    "run-plugin.sh": "#!/bin/bash\necho plugin\n",
    "scripts/update.py": "print('updater')\n",
    "scripts/ea-doctor.py": "print('doctor')\n",
    "preprocessor/thing.py": "X = 1\n",
    "tests/validate_config.py": "import sys\nsys.exit(0)\n",
    "skills/a.md": "skill\n",
}


def make_origin(tmp: Path, key: Path) -> Path:
    origin = tmp / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    for rel, text in MINI.items():
        f = origin / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    shutil.copy(str(key) + ".pub", origin / ".release-key.pub")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "first")
    return origin


def commit(origin: Path, rel: str, text: str, msg: str = "change") -> str:
    f = origin / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", msg)
    return git(origin, "rev-parse", "HEAD")


class FakeHost:
    """A service host: restarts are recorded, the state and the log are
    scripted per restart."""

    def __init__(self, healthy=True, services=("bot",), log_lines=None):
        self.healthy = healthy
        self.services = set(services)
        self.restarts: list[str] = []
        self.pid = 100
        self.log = ""
        self.before = log_lines if log_lines is not None else (
            "x EA chat bot starting... (commit old)\nx frontend=zulip stream=ea (private) owner=9\n")

    def service(self, role):
        return {"bot": "ea-chat-bot", "mediator": "ea-mail-mediator"}[role] if role in self.services else None

    def restart(self, name, role):
        self.restarts.append(name)
        self.pid += 1
        if self.healthy if not callable(self.healthy) else self.healthy(len(self.restarts)):
            self.log = "EA chat bot starting... (commit new)\nfrontend=zulip stream=ea (private)\n"
        else:
            self.log = "EA chat bot starting...\nTraceback (most recent call last):\n  boom\n"
        return True, "restarted"

    def state(self, name):
        return True, str(self.pid)

    def log_mark(self, name, role="bot"):
        return {"files": {}, "since": 0}

    def log_since(self, name, mark):
        return self.log, True

    def log_before(self, name, role="bot"):
        return self.before

    def restart_dashboard(self):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fast_gate():
    c = Clock()
    return {"timeout": 30, "stable": 3, "sleep": c.sleep, "clock": c}


@pytest.fixture(autouse=True)
def _not_in_a_container(monkeypatch):
    """The Docker wall runs this file inside a container, where every tree
    would read as the image's; each test says when it means one."""
    monkeypatch.setattr(us, "in_container", lambda: False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """EA_DIR, EA_PRIVATE_DIR and EA_LIVE_DIR in the temp dir, so nothing
    reads or writes the real install."""
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "state").mkdir()
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    opt = tmp_path / "opt"
    opt.mkdir()
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setenv("EA_LIVE_DIR", str(opt / "ea-system"))
    return {"ea": ea, "priv": priv, "opt": opt, "live": opt / "ea-system", "tmp": tmp_path}


def set_config(ea: Path, **kw) -> None:
    (ea / "config" / "ea-config.json").write_text(json.dumps(kw))


# ── channel selection ───────────────────────────────────────────────

def test_an_unset_channel_on_a_plain_tree_belongs_to_the_hook(env):
    env["live"].mkdir()
    (env["live"] / ".deployed-commit").write_text("abc1234\n")
    set_config(env["ea"], profile="assistant")
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("off", "hook")
    status, detail, _ = us.doctor_row(env["live"], env["priv"], env["ea"])
    assert status == "PASS" and "post-commit hook" in detail and "abc1234" in detail


def test_an_unset_channel_on_the_layout_follows_the_profile(env):
    rel = env["opt"] / "ea-system.releases" / "v0.1.1"
    rel.mkdir(parents=True)
    update.switch(rel.parent, "v0.1.1")
    env["live"].symlink_to("ea-system.releases/current")
    set_config(env["ea"], profile="assistant")
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("latest", "default")
    set_config(env["ea"], profile="email")
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("stable", "default")


def test_auto_and_explicit_channels_and_the_private_file_outranks_config(env):
    env["live"].mkdir()
    set_config(env["ea"], profile="review-only", update={"channel": "auto"})
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("stable", "ea-config.json")
    set_config(env["ea"], profile="review-only", update={"channel": "latest"})
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("latest", "ea-config.json")
    set_config(env["ea"], profile="assistant", update={"channel": "nonsense"})
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("off", "hook")
    pin = env["priv"] / "update.json"
    pin.write_text(json.dumps({"channel": "off"}))
    os.chmod(pin, 0o600)
    set_config(env["ea"], profile="assistant", update={"channel": "latest"})
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("off", "update.json")
    # a private file others can write is not the owner's word
    os.chmod(pin, 0o666)
    assert us.effective_channel(env["live"], env["priv"], env["ea"]) == ("latest", "ea-config.json")


def test_in_a_container_the_image_is_the_release(env, monkeypatch, capsys):
    env["live"].mkdir()
    set_config(env["ea"], profile="assistant", update={"channel": "latest"})
    monkeypatch.setattr(us, "in_container", lambda: True)
    assert us.tree_kind(env["live"]) == "container"
    status, detail, fix = us.doctor_row(env["live"], env["priv"], env["ea"])
    assert status == "PASS" and "image" in detail and "docker.md" in fix
    args = ["--live", str(env["live"]), "--private", str(env["priv"]), "--ea-dir", str(env["ea"])]
    assert update.main(args + ["run"]) == 0
    assert update.main(args + ["apply"]) == 1
    assert "docker compose build" in capsys.readouterr().out


def test_the_config_template_and_the_default():
    import ea_config
    tpl = REPO / "config" / "ea-config.json"
    assert json.loads(tpl.read_text())["update"]["channel"] == "auto"
    assert ea_config.get_update_channel(tpl) == "auto"
    assert ea_config.DEFAULTS["update"] == {"channel": ""}
    assert ea_config.get_update_channel(Path("/nonexistent/ea-config.json")) == ""


# ── signatures ──────────────────────────────────────────────────────

def test_allowed_signers_from_the_key_file():
    line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample0nly0000000000000000000000000000000 tacitus-release\n"
    out = update.allowed_signers("# the release key\n\n" + line)
    assert out == ('tacitus-release namespaces="git" ssh-ed25519 '
                   'AAAAC3NzaC1lZDI1NTE5AAAAIExample0nly0000000000000000000000000000000\n')
    with pytest.raises(update.VerifyError):
        update.allowed_signers("# no key here\n")
    with pytest.raises(update.VerifyError):
        update.allowed_signers("not a key; rm -rf /\n")
    with pytest.raises(update.VerifyError, match="a release key is ssh-ed25519"):
        update.allowed_signers("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIExample0nly00000 x\n")


@needs_ssh
def test_a_signature_by_a_key_of_another_type_is_refused(tmp_path):
    """verify_tag pins the key type: an allowed-signers file that names an
    ECDSA key (made by hand, since allowed_signers refuses one) still does
    not pass a tag that key signed."""
    key = tmp_path / "ecdsa"
    subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-C", "e", "-f", str(key)], check=True)
    origin = make_origin(tmp_path, keypair(tmp_path, "release"))
    sign_tag(origin, "v0.1.1", key)
    kind, b64 = Path(str(key) + ".pub").read_text().split()[:2]
    signers = tmp_path / "allowed"
    signers.write_text(f'tacitus-release namespaces="git" {kind} {b64}\n')
    with pytest.raises(update.VerifyError, match="ED25519"):
        update.verify_tag(origin, "v0.1.1", signers)


@needs_ssh
def test_a_tag_signed_by_the_release_key_verifies_and_others_are_refused(tmp_path):
    key, other = keypair(tmp_path, "release"), keypair(tmp_path, "other")
    origin = make_origin(tmp_path, key)
    signers = update.write_signers(origin / ".release-key.pub", tmp_path / "allowed")
    sign_tag(origin, "v0.1.1", key)
    sha = update.verify_tag(origin, "v0.1.1", signers)
    assert sha == git(origin, "rev-parse", "HEAD")
    commit(origin, "a.txt", "a")
    sign_tag(origin, "v0.1.2", other)
    with pytest.raises(update.VerifyError, match="not from the release key"):
        update.verify_tag(origin, "v0.1.2", signers)
    sign_tag(origin, "v0.1.3", None)
    with pytest.raises(update.VerifyError, match="no SSH signature"):
        update.verify_tag(origin, "v0.1.3", signers)
    git(origin, "tag", "v0.1.4")   # lightweight
    with pytest.raises(update.VerifyError, match="not an annotated tag"):
        update.verify_tag(origin, "v0.1.4", signers)
    with pytest.raises(update.VerifyError, match="missing"):
        update.verify_tag(origin, "v9.9.9", signers)


@needs_ssh
def test_a_ref_that_holds_an_older_releases_tag_object_is_refused(tmp_path):
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    signers = update.write_signers(origin / ".release-key.pub", tmp_path / "allowed")
    sign_tag(origin, "v0.1.1", key)
    obj = git(origin, "rev-parse", "refs/tags/v0.1.1")
    git(origin, "update-ref", "refs/tags/v9.9.9", obj)
    with pytest.raises(update.VerifyError, match="holds the tag object"):
        update.verify_tag(origin, "v9.9.9", signers)


@needs_ssh
def test_pick_takes_the_newest_signed_release_and_never_goes_back(tmp_path):
    key, other = keypair(tmp_path, "release"), keypair(tmp_path, "other")
    origin = make_origin(tmp_path, key)
    signers = update.write_signers(origin / ".release-key.pub", tmp_path / "allowed")
    sign_tag(origin, "v0.1.1", key)
    commit(origin, "a.txt", "a")
    sign_tag(origin, "v0.1.2", key)
    commit(origin, "b.txt", "b")
    sign_tag(origin, "v0.1.3", other)          # a stranger's tag is skipped
    commit(origin, "c.txt", "c")
    sign_tag(origin, "v0.1.10", None)          # an unsigned tag is skipped
    got = update.pick(origin, "latest", signers, installed="v0.1.1", main=MAIN)
    assert got[0] == "v0.1.2" and len(got[2]) == 2
    assert update.pick(origin, "latest", signers, installed="v0.1.2", main=MAIN) is None
    assert update.pick(origin, "latest", signers, installed="v0.2.0", main=MAIN) is None
    assert update.pick(origin, "latest", signers, installed="v0.1.1", held="v0.1.2", main=MAIN) is None
    assert update.newest_signed(origin, "latest", signers, main=MAIN) == "v0.1.2"
    # semver order, not string order
    assert update.release_tags(origin)[:2] == ["v0.1.10", "v0.1.3"]


@needs_ssh
def test_stable_follows_its_pointer_among_signed_releases(tmp_path):
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    signers = update.write_signers(origin / ".release-key.pub", tmp_path / "allowed")
    sign_tag(origin, "v0.1.1", key)
    commit(origin, "a.txt", "a")
    sign_tag(origin, "v0.1.2", key)
    assert update.pick(origin, "stable", signers, main=MAIN) is None
    git(origin, "tag", "-a", "-m", "stable: v0.1.1", "channels/stable", "v0.1.1^{commit}")
    assert update.pick(origin, "stable", signers, main=MAIN)[0] == "v0.1.1"
    assert update.pick(origin, "stable", signers, installed="v0.1.2", main=MAIN) is None
    git(origin, "tag", "-f", "-a", "-m", "stable: v0.1.2", "channels/stable", "v0.1.2^{commit}")
    assert update.pick(origin, "stable", signers, installed="v0.1.1", main=MAIN)[0] == "v0.1.2"


@needs_ssh
def test_fetch_takes_release_tags_and_pointers_and_follows_a_moved_pointer(tmp_path):
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    sign_tag(origin, "v0.1.1", key)
    git(origin, "tag", "other-tag")
    git(origin, "tag", "-a", "-m", "s", "channels/stable", "v0.1.1^{commit}")
    cache = tmp_path / "priv" / "update" / "tacitus.git"
    update.fetch_tags(cache, str(origin))
    tags = git(cache, "tag", "--list").split()
    assert "v0.1.1" in tags and "channels/stable" in tags and "other-tag" not in tags
    commit(origin, "a.txt", "a")
    sign_tag(origin, "v0.1.2", key)
    git(origin, "tag", "-f", "-a", "-m", "s", "channels/stable", "v0.1.2^{commit}")
    update.fetch_tags(cache, str(origin))
    assert git(cache, "rev-parse", "channels/stable^{commit}") == git(origin, "rev-parse", "HEAD")


# ── versions for CI ─────────────────────────────────────────────────

def test_the_next_version_bumps_patch_minor_and_major(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    assert update.next_version(repo) == "v0.1.0"
    git(repo, "tag", "-a", "-m", "v0.1.0", "v0.1.0")
    assert update.next_version(repo) == "", "a released commit gets no second tag"
    git(repo, "commit", "-q", "--allow-empty", "-m", "fix a thing")
    assert update.next_version(repo) == "v0.1.1"
    git(repo, "commit", "-q", "--allow-empty", "-m", "Merge pull request #7\n\nA new verb #minor")
    assert update.next_version(repo) == "v0.2.0"
    git(repo, "commit", "-q", "--allow-empty", "-m", "Break the config\n\nrelease: major")
    assert update.next_version(repo) == "v1.0.0"
    assert update.bump("v1.4.9", "nothing") == "v1.4.10"


def test_a_marker_in_an_older_commit_never_raises_the_version(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    git(repo, "tag", "-a", "-m", "v0.1.0", "v0.1.0")
    git(repo, "commit", "-q", "--allow-empty", "-m",
        "The updater docs\n\nThe patch goes up; #minor,\n  #major or a `release: minor|major` footer\n"
        "raises that part.\n\nrelease: major")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Merge pull request #9 from someone/fix\n\nA plain fix")
    assert update.next_version(repo) == "v0.1.1", "only the released commit's message counts"


def test_a_marker_stands_alone():
    for text in ("A new verb #minor", "#minor", "Title\n\n#Minor\n", "Body\n\nrelease: minor\n",
                 "x\nrelease:minor"):
        assert update.bump("v0.1.4", text) == "v0.2.0", text
    for text in ("Break it #major", "Body\n\nRelease: Major\n"):
        assert update.bump("v0.1.4", text) == "v1.0.0", text
    for text in ("#minor, #major, or", "see `#major`", "the #majority", "issue#minor",
                 "a `release: minor|major` footer", "release: major soon", "(#major)"):
        assert update.bump("v0.1.4", text) == "v0.1.5", text


def test_the_pull_request_body_of_the_released_commit_may_hold_the_marker(tmp_path, capsys):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    git(repo, "tag", "-a", "-m", "v0.1.0", "v0.1.0")
    git(repo, "commit", "-q", "--allow-empty", "-m", "An old change (#3)\n\n#major")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Merge pull request #9 from someone/verb\n\nA new verb")
    asked = []

    def body(slug, number):
        asked.append((slug, number))
        return "What it does.\n\nrelease: minor\n"

    assert update.next_version(repo, github_repo="o/n", fetch=body) == "v0.2.0"
    assert asked == [("o/n", 9)], "only the released commit's pull request"

    def down(slug, number):
        raise OSError("no network")

    assert update.next_version(repo, github_repo="o/n", fetch=down) == "v0.1.1"
    assert "#9 did not come" in capsys.readouterr().err
    git(repo, "commit", "-q", "--allow-empty", "-m", "A direct commit")
    asked.clear()
    assert update.next_version(repo, github_repo="o/n", fetch=body) == "v0.1.1" and asked == []
    git(repo, "commit", "-q", "--allow-empty", "-m", "A squashed verb (#11)")
    assert update.next_version(repo, github_repo="o/n", fetch=body) == "v0.2.0" and asked == [("o/n", 11)]
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "update.py"), "next-version",
                        "--repo", str(repo)], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    assert r.returncode == 0 and r.stdout == "v0.1.1\n", "stdout is the tag alone"


def test_an_older_commit_finishing_late_gets_no_tag(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "a")
    git(repo, "tag", "-a", "-m", "v0.1.0", "v0.1.0")
    git(repo, "commit", "-q", "--allow-empty", "-m", "b")
    older = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    git(repo, "tag", "-a", "-m", "v0.1.1", "v0.1.1")   # the newer push released first
    assert update.next_version(repo, head=older, main="HEAD") == ""
    # tags outside main's history do not count
    git(repo, "checkout", "-q", "--orphan", "old-history")
    git(repo, "commit", "-q", "--allow-empty", "-m", "private past")
    git(repo, "tag", "-a", "-m", "v1.2.0", "v1.2.0")
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "d")
    assert update.next_version(repo, main="main") == "v0.1.2"


# ── the release layout and the swap ─────────────────────────────────

def test_layout_moves_a_plain_tree_in_and_is_idempotent(env):
    live = env["live"]
    live.mkdir()
    (live / "ea-chat-bot.py").write_text("x\n")
    (live / ".deployed-commit").write_text("abc1234\n")
    msg = update.layout(live, os.environ.get("USER") or _me())
    releases = env["opt"] / "ea-system.releases"
    assert live.is_symlink() and os.readlink(live) == "ea-system.releases/current"
    assert os.readlink(releases / "current") == "commit-abc1234"
    assert (live / "ea-chat-bot.py").read_text() == "x\n"
    assert "commit-abc1234" in msg
    assert update.layout(live, _me()).startswith(f"{live} is already")
    assert us.tree_kind(live) == "layout"


def test_layout_seeds_a_new_install_from_an_export(env):
    seed = env["tmp"] / "seed"
    seed.mkdir()
    (seed / ".version").write_text("v0.1.4\n")
    update.layout(env["live"], _me(), seed=seed)
    assert os.readlink(env["opt"] / "ea-system.releases" / "current") == "v0.1.4"
    assert us.installed(env["live"])["version"] == "v0.1.4"
    with pytest.raises(update.UpdateError):
        update.layout(env["opt"] / "other", _me())


def test_the_switch_is_a_rename_of_a_new_link(env):
    releases = env["opt"] / "ea-system.releases"
    for name in ("v0.1.1", "v0.1.2"):
        (releases / name).mkdir(parents=True)
    update.switch(releases, "v0.1.1")
    update.switch(releases, "v0.1.2")
    update.switch(releases, "v0.1.1", "previous")
    assert os.readlink(releases / "current") == "v0.1.2"
    assert os.readlink(releases / "previous") == "v0.1.1"
    assert not (releases / ".current.new").exists()


def test_prune_keeps_current_previous_and_the_newest(env):
    releases = env["opt"] / "ea-system.releases"
    for n in range(1, 7):
        (releases / f"v0.1.{n}").mkdir(parents=True)
    (releases / "commit-abc1234").mkdir()
    update.switch(releases, "v0.1.6")
    update.switch(releases, "commit-abc1234", "previous")
    gone = update.prune(releases, keep=3)
    left = sorted(p.name for p in releases.iterdir() if not p.is_symlink())
    assert left == ["commit-abc1234", "v0.1.4", "v0.1.5", "v0.1.6"]
    assert "v0.1.1" in gone


def test_the_smoke_passes_on_this_tree_and_catches_a_broken_release(tmp_path):
    assert update.smoke(REPO) == [], "this checkout passes its own smoke"
    tree = tmp_path / "t"
    for rel, text in MINI.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text)
    assert update.smoke(tree) == []
    (tree / "preprocessor" / "bad.py").write_text("def broken(:\n")
    (tree / "run-agent.sh").write_text("if then fi\n")
    problems = update.smoke(tree)
    assert any("bad.py does not compile" in p for p in problems)
    assert any(p.startswith("run-agent.sh") for p in problems)


# ── the health gate ─────────────────────────────────────────────────

class GateHost:
    def __init__(self, frames):
        self.frames = frames   # [(running, pid, log)] per poll; the last repeats
        self.i = 0

    def _frame(self):
        f = self.frames[min(self.i, len(self.frames) - 1)]
        return f

    def state(self, name):
        running, pid, _ = self._frame()
        return running, pid

    def log_since(self, name, mark):
        log = self._frame()[2]
        self.i += 1
        return (log if log is not None else ""), log is not None


START = "EA chat bot starting... (commit n)\n"
ZULIP = "frontend=zulip stream=ea (private, subscribed) owner=9\n"
WEB = "web chat serving on 127.0.0.1:8470/chat/\n"


def run_gate(frames, markers=None, **kw):
    c = Clock()
    return update.gate(GateHost(frames), "ea-chat-bot", {}, markers if markers is not None
                       else update.expected_markers(START + ZULIP),
                       sleep=c.sleep, clock=c, **kw)


def test_the_gate_passes_a_connected_stable_bot():
    ok, why = run_gate([(True, "7", START), (True, "7", START + ZULIP)])
    assert ok, why


def test_the_gate_fails_on_a_traceback_a_restart_loop_or_no_connection():
    ok, why = run_gate([(True, "7", START + "Traceback (most recent call last):\n  x\n")])
    assert not ok and "Traceback" in why
    ok, why = run_gate([(True, "7", START), (True, "8", START)])
    assert not ok and "main process changed" in why
    ok, why = run_gate([(True, "7", START)], timeout=30)
    assert not ok and "chat frontend connected" in why
    ok, why = run_gate([(False, "0", "")], timeout=9)
    assert not ok and "not running" in why
    ok, why = run_gate([(True, "7", START + "CRITICAL chat frontend refused to start: public\n")])
    assert not ok


def test_the_web_chat_is_required_only_when_the_old_instance_served_it():
    old = START + ZULIP + WEB + "later lines\n"
    markers = update.expected_markers("older\n" + old)
    assert [n for n, _ in markers] == ["the chat frontend connected", "the web chat serving"]
    ok, why = run_gate([(True, "7", START + ZULIP)], markers=markers, timeout=30)
    assert not ok and "web chat" in why
    ok, _ = run_gate([(True, "7", START + ZULIP + WEB)], markers=markers)
    assert ok
    # a web chat served before the last restart does not count
    assert len(update.expected_markers(START + WEB + START + ZULIP)) == 1


def test_with_no_readable_log_the_gate_falls_back_to_process_stability():
    ok, why = run_gate([(True, "7", None)])
    assert ok and "no readable log" in why
    ok, why = run_gate([(True, "7", None), (True, "9", None)])
    assert not ok


def test_the_mediator_gate_needs_no_start_line():
    c = Clock()
    ok, why = update.gate(GateHost([(True, "3", "")]), "ea-mail-mediator", {}, [],
                          need_start=False, sleep=c.sleep, clock=c, stable=3)
    assert ok, why


def test_the_services_pin_the_release_they_start_from():
    # Why every update restarts them: each resolves its own path at start.
    bot = (REPO / "ea-chat-bot.py").read_text()
    assert "SCRIPT_DIR = Path(__file__).resolve().parent" in bot
    assert "Path(__file__).resolve()" in (REPO / "preprocessor" / "mail_mediator.py").read_text()
    host = FakeHost(services=("bot", "mediator"))
    assert update.installed_services(host) == {"bot": "ea-chat-bot", "mediator": "ea-mail-mediator"}
    done, problems = update.restart_services(host, update.installed_services(host))
    assert [r for r, _ in done] == ["bot", "mediator"] and not problems


# ── idle ────────────────────────────────────────────────────────────

def test_idle_counts_chat_turns_and_cron_lanes(env, tmp_path):
    paths = update.Paths(live=env["live"], private=env["priv"], ea=env["ea"], lanes=False,
                         repo_url="unused")
    inflight = env["priv"] / "bot-inflight.json"
    inflight.write_text(json.dumps({"r1": {"lane": "resident"}, "r2": {"lane": "deferred"},
                                    "r3": {"lane": "room"}}))
    os.chmod(inflight, 0o600)
    assert update.inflight_count(inflight) == 1
    me = os.geteuid()
    ps = (f"  101 {me} /bin/bash /opt/ea-system/run-agent.sh morning-briefing\n"
          f"  102 {me} /opt/ea-system/run-plugin.sh lists\n"
          f"  103 {me} grep run-agent.sh\n"
          f"  104 {me} vim run-agent.sh\n"
          f"  105 {me + 1} /bin/bash /opt/ea-system/run-agent.sh intake-check\n")
    assert update.lane_processes(ps) == [101, 102]
    assert update.busy_reasons(paths, ps_text=ps) == ["1 chat turn(s) in flight", "2 cron lane(s) running"]
    inflight.write_text("{}")
    assert update.busy_reasons(paths, ps_text="") == []


def test_wait_idle_returns_once_idle_and_gives_up_after_the_timeout(env):
    paths = update.Paths(live=env["live"], private=env["priv"], ea=env["ea"], lanes=False,
                         repo_url="unused")
    c = Clock()
    answers = iter([["busy"], ["busy"], []])
    assert update.wait_idle(paths, 600, probe=lambda: next(answers), sleep=c.sleep, clock=c) == []
    assert c.t == 2 * update.IDLE_POLL_S
    c = Clock()
    assert update.wait_idle(paths, 600, probe=lambda: ["1 cron lane(s) running"],
                            sleep=c.sleep, clock=c) == ["1 cron lane(s) running"]
    assert c.t >= 600


# ── a whole update, in temp directories ─────────────────────────────

def _me() -> str:
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


@pytest.fixture
def install(env, tmp_path):
    """A fake origin with two signed releases, an install on the first one
    in the release layout, and an EA user's copy at the first one."""
    if not _git_ok():
        pytest.skip("needs ssh-keygen and git 2.34+")
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    sign_tag(origin, "v0.1.1", key)
    lanes = tmp_path / "lanes"
    git(tmp_path, "clone", "-q", str(origin), str(lanes))
    os.chmod(lanes, 0o755)
    os.chmod(lanes / ".git", 0o755)
    first = git(origin, "rev-parse", "HEAD")
    commit(origin, "preprocessor/thing.py", "X = 2\n", "runtime change")
    sign_tag(origin, "v0.1.2", key)
    seed = tmp_path / "seed"
    git(origin, "archive", "--format=tar", "-o", str(tmp_path / "s.tar"), "v0.1.1")
    seed.mkdir()
    subprocess.run(["tar", "-x", "-C", str(seed), "-f", str(tmp_path / "s.tar")], check=True)
    (seed / ".version").write_text("v0.1.1\n")
    (seed / ".deployed-commit").write_text(first[:7] + "\n")
    update.layout(env["live"], _me(), seed=seed)
    set_config(env["ea"], profile="assistant", update={"channel": "latest"})
    paths = update.Paths(live=env["live"], private=env["priv"], ea=env["ea"], lanes=lanes,
                         repo_url=str(origin))
    return dict(env, origin=origin, key=key, lanes=lanes, paths=paths, first=first)


def test_an_update_installs_the_new_release_and_records_it(install):
    paths, host = install["paths"], FakeHost(healthy=True)
    rc = update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: [])
    assert rc == 0
    releases = install["opt"] / "ea-system.releases"
    assert os.readlink(releases / "current") == "v0.1.2"
    assert os.readlink(releases / "previous") == "v0.1.1"
    assert us.installed(paths.live)["version"] == "v0.1.2"
    assert (paths.live / "preprocessor" / "thing.py").read_text() == "X = 2\n"
    assert host.restarts == ["ea-chat-bot"], "a runtime change restarts the bot"
    assert git(install["lanes"], "rev-parse", "HEAD") == git(install["origin"], "rev-parse", "v0.1.2^{commit}")
    st = us.load_status(paths.private)
    assert st["installed"] == "v0.1.2" and st["last_apply"]["result"] == "ok"
    assert st["latest_on_channel"] == "v0.1.2" and "held_back" not in st
    shared = json.loads((install["ea"] / "state" / "update-status.json").read_text())
    assert shared["installed"] == "v0.1.2"
    assert not (paths.private / "deploy-pending.json").exists()
    # the next run has nothing to do
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    assert host.restarts == ["ea-chat-bot"]


def test_a_release_that_fails_the_gate_is_switched_back_and_held(install):
    paths = install["paths"]
    host = FakeHost(healthy=lambda n: n != 1)    # the new release crashes, the old one comes back
    rc = update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: [])
    assert rc == 1
    releases = install["opt"] / "ea-system.releases"
    assert os.readlink(releases / "current") == "v0.1.1"
    assert us.installed(paths.live)["version"] == "v0.1.1"
    assert host.restarts == ["ea-chat-bot", "ea-chat-bot"]
    assert git(install["lanes"], "rev-parse", "HEAD") == install["first"]
    st = us.load_status(paths.private)
    assert st["held_back"]["version"] == "v0.1.2" and "Traceback" in st["held_back"]["reason"]
    assert st["last_apply"]["result"] == "rolled-back"
    # held back: the next run leaves it, until a newer release appears
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    assert len(host.restarts) == 2
    commit(install["origin"], "preprocessor/thing.py", "X = 3\n", "the fix")
    sign_tag(install["origin"], "v0.1.3", install["key"])
    host.healthy = True
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert st["installed"] == "v0.1.3" and "held_back" not in st


def test_a_refused_restart_switches_back_without_holding_the_release(install):
    paths, host = install["paths"], FakeHost()
    host.restart = lambda name, role: (False, "sudo: a password is required")
    rc = update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: [])
    assert rc == 1
    assert us.installed(paths.live)["version"] == "v0.1.1"
    assert git(install["lanes"], "rev-parse", "HEAD") == install["first"]
    st = us.load_status(paths.private)
    assert st["last_apply"]["result"] == "failed" and "held_back" not in st
    status, detail, fix = us.doctor_row(paths.live, paths.private, paths.ea)
    assert status == "WARN" and "password" in detail and "sudoers" in fix


def test_a_release_that_fails_the_smoke_is_held_without_a_swap(install):
    commit(install["origin"], "preprocessor/thing.py", "def broken(:\n", "broken")
    sign_tag(install["origin"], "v0.1.3", install["key"])
    paths, host = install["paths"], FakeHost()
    rc = update.apply(paths, host, channel="latest", tag="v0.1.3", gate_kw=fast_gate(),
                      idle_probe=lambda: [])
    assert rc == 1 and host.restarts == []
    assert us.installed(paths.live)["version"] == "v0.1.1"
    assert us.load_status(paths.private)["held_back"]["version"] == "v0.1.3"
    assert not list((install["opt"] / "ea-system.releases").glob(".staging-*"))


def test_a_busy_install_defers_and_every_release_restarts_the_services(install):
    paths, host = install["paths"], FakeHost()
    rc = update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_timeout=0,
                      idle_probe=lambda: ["1 cron lane(s) running"])
    assert rc == 0 and host.restarts == []
    assert us.installed(paths.live)["version"] == "v0.1.1"
    assert us.load_status(paths.private)["last_apply"]["result"] == "deferred"
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    commit(install["origin"], "skills/a.md", "new words\n", "docs")
    sign_tag(install["origin"], "v0.1.3", install["key"])
    host.restarts.clear()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    assert us.installed(paths.live)["version"] == "v0.1.3" and host.restarts == ["ea-chat-bot"]
    # the release the bot ran before stays as previous; older ones go
    releases = install["opt"] / "ea-system.releases"
    assert os.readlink(releases / "previous") == "v0.1.2"


def test_a_dry_run_switches_nothing(install, capsys):
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", dry_run=True, idle_probe=lambda: []) == 0
    out = capsys.readouterr().out
    assert "v0.1.2" in out and "smoke: passed" in out and "would restart: ea-chat-bot" in out
    assert us.installed(paths.live)["version"] == "v0.1.1" and host.restarts == []


def test_a_release_signed_by_another_key_is_never_installed(install, tmp_path):
    other = keypair(tmp_path, "intruder")
    commit(install["origin"], "preprocessor/thing.py", "X = 666\n", "not ours")
    sign_tag(install["origin"], "v0.2.0", other)
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    assert us.installed(paths.live)["version"] == "v0.1.2"


def test_rollback_by_hand_switches_to_previous_and_holds_the_release(install):
    paths, host = install["paths"], FakeHost()
    update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: [])
    assert update.rollback(paths, host, gate_kw=fast_gate()) == 0
    assert us.installed(paths.live)["version"] == "v0.1.1"
    assert us.load_status(paths.private)["held_back"]["version"] == "v0.1.2"
    assert git(install["lanes"], "rev-parse", "HEAD") == install["first"]


def test_with_no_trust_root_nothing_installs_and_the_error_is_recorded(install):
    (install["live"] / ".release-key.pub").unlink()
    (install["priv"] / "update.json").unlink()
    paths, host = install["paths"], FakeHost()
    with pytest.raises(update.VerifyError, match="nothing can be verified"):
        update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: [])
    assert us.installed(paths.live)["version"] == "v0.1.1" and host.restarts == []
    assert "release-key" in us.load_status(paths.private)["last_error"]["error"]


def _pin(install) -> list:
    return json.loads((install["priv"] / "update.json").read_text())["release_key"]


def _pub(key: Path) -> str:
    kind, b64 = Path(str(key) + ".pub").read_text().split()[:2]
    return f"{kind} {b64}"


def test_layout_pins_the_first_release_key_outside_every_release(install):
    """The trust root is the owner's update.json, written once from the
    first installed release; the running release's .release-key.pub is
    never read again, so a release cannot replace it."""
    assert _pin(install) == [_pub(install["key"])]
    assert os.stat(install["priv"] / "update.json").st_mode & 0o777 == 0o600
    # a running release whose key file names an intruder's key
    intruder = keypair(install["tmp"], "intruder")
    shutil.copy(str(intruder) + ".pub", install["live"] / ".release-key.pub")
    commit(install["origin"], "preprocessor/thing.py", "X = 666\n", "not ours")
    sign_tag(install["origin"], "v0.2.0", intruder)
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    assert us.installed(paths.live)["version"] == "v0.1.2"
    assert _pin(install) == [_pub(install["key"])], "apply never rewrites the pin"
    # the root step again keeps the pin it finds
    assert "pinned" not in update.layout(paths.live, _me())


def test_an_install_without_a_pin_pins_the_installed_key_once(install, capsys):
    (install["priv"] / "update.json").write_text(json.dumps({"channel": "latest"}))
    os.chmod(install["priv"] / "update.json", 0o600)
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    doc = json.loads((install["priv"] / "update.json").read_text())
    assert doc == {"channel": "latest", "release_key": [_pub(install["key"])]}
    assert "(first use)" in capsys.readouterr().out
    # a pin the owner made by path, and a refused update.json
    keyfile = install["priv"] / "release.pub"
    shutil.copy(str(install["key"]) + ".pub", keyfile)
    os.chmod(keyfile, 0o644)
    (install["priv"] / "update.json").write_text(json.dumps({"release_key": str(keyfile)}))
    assert update.pinned_key(install["priv"]).split()[:2] == _pub(install["key"]).split()
    os.chmod(keyfile, 0o666)
    with pytest.raises(update.VerifyError, match="no one else can write"):
        update.pinned_key(install["priv"])
    os.chmod(install["priv"] / "update.json", 0o666)
    with pytest.raises(update.VerifyError, match="refused"):
        update.pin_key(install["priv"], Path(str(install["key"]) + ".pub").read_text())


def test_a_release_that_changes_the_key_waits_for_the_owner(install):
    """A rotation signed by the pinned key still installs only once the
    owner puts the new key in update.json; until then it is logged, the
    doctor warns, and the install stays on the newest release the pin
    covers."""
    new = keypair(install["tmp"], "next")
    origin, paths, host = install["origin"], install["paths"], FakeHost()
    commit(origin, ".release-key.pub", Path(str(new) + ".pub").read_text(), "Rotate the release key")
    sign_tag(origin, "v0.1.3", install["key"])
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert st["installed"] == "v0.1.2" and st["key_change"]["version"] == "v0.1.3"
    assert st["latest_on_channel"] == "v0.1.2"
    st["checked_at"] = update.now_iso()
    update.save_status(paths, st)
    status, detail, fix = us.doctor_row(paths.live, paths.private, paths.ea)
    assert status == "WARN" and "v0.1.3" in detail and "update.json" in fix
    with pytest.raises(update.KeyChangeError):
        update.apply(paths, host, channel="latest", tag="v0.1.3", gate_kw=fast_gate(),
                     idle_probe=lambda: [])
    # the owner adds the new key: the rotation installs, and the pin stays the owner's
    doc = json.loads((install["priv"] / "update.json").read_text())
    doc["release_key"].append(_pub(new))
    (install["priv"] / "update.json").write_text(json.dumps(doc))
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert st["installed"] == "v0.1.3" and "key_change" not in st
    assert _pin(install) == [_pub(install["key"]), _pub(new)]


@needs_ssh
def test_a_signed_tag_off_main_is_refused_and_one_on_main_is_taken(tmp_path, capsys):
    """The tag must name a commit in the history of the repository's main,
    fetched beside the tags: a signed tag on an orphan commit (a branch a
    stolen key signed) is never installed."""
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    sign_tag(origin, "v1.0.0", key)
    git(origin, "checkout", "-q", "--orphan", "elsewhere")
    commit(origin, "x.txt", "x", "not on main")
    sign_tag(origin, "v9.9.9", key)
    git(origin, "checkout", "-q", "main")
    cache = tmp_path / "cache.git"
    update.fetch_tags(cache, str(origin))
    assert git(cache, "rev-parse", update.MAIN_REF) == git(origin, "rev-parse", "main")
    signers = update.write_signers(origin / ".release-key.pub", tmp_path / "allowed")
    with pytest.raises(update.VerifyError, match="not in the history of main"):
        update.verify_release(cache, "v9.9.9", signers)
    assert update.verify_release(cache, "v1.0.0", signers) == git(origin, "rev-parse", "v1.0.0^{commit}")
    refused = []
    got = update.pick(cache, "latest", signers, refused=refused)
    assert got[0] == "v1.0.0" and "not in the history of main" in str(refused[0])
    assert update.newest_signed(cache, "latest", signers) == "v1.0.0"
    # a cache with no main fetched accepts nothing
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(bare, "fetch", "-q", str(origin), "+refs/tags/v*:refs/tags/v*")
    assert update.pick(bare, "latest", signers) is None


@needs_ssh
def test_check_warns_when_no_signed_release_is_reachable(install, capsys):
    paths = install["paths"]
    git(install["origin"], "tag", "-d", "v0.1.2")
    args = ["--live", str(paths.live), "--private", str(paths.private), "--ea-dir", str(paths.ea),
            "--lanes", str(install["lanes"])]
    capsys.readouterr()
    # the CLI reads the repository from update.json
    doc = json.loads((install["priv"] / "update.json").read_text())
    doc["repo"] = str(install["origin"])
    (install["priv"] / "update.json").write_text(json.dumps(doc))
    git(install["origin"], "tag", "-d", "v0.1.1")
    assert update.main(args + ["check"]) == 0
    err = capsys.readouterr().err
    assert "no release on latest" in err and "Supply chain" in err


# ── release notes ───────────────────────────────────────────────────

def test_an_update_records_the_release_notes_from_the_history(install, capsys):
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    notes = st["notes"]
    assert notes["tag"] == "v0.1.2" and notes["previous"] == "v0.1.1" and notes["source"] == "git"
    assert notes["notes_url"] == "", "a repository that is not on GitHub has no Release page"
    assert [(c["subject"], c["pr"]) for c in notes["changes"]] == [("runtime change", None)]
    assert notes["count"] == 1 and notes["owner_steps"] == [] and st["notes_history"] == []
    assert "behind" not in st
    shared = json.loads((install["ea"] / "state" / "update-status.json").read_text())
    assert shared["notes"]["changes"][0]["subject"] == "runtime change"
    # a pull request merged with a merge commit, with an owner step on its branch
    origin = install["origin"]
    git(origin, "checkout", "-q", "-b", "topic")
    commit(origin, "skills/a.md", "words\n", "Branch work\n\nOwner-steps: run the root step once")
    git(origin, "checkout", "-q", "main")
    git(origin, "merge", "-q", "--no-ff", "topic", "-m",
        "Merge pull request #5 from someone/topic\n\nSkills say more")
    commit(origin, "preprocessor/thing.py", "X = 4\n", "The thing counts to four (#6)")
    sign_tag(origin, "v0.1.3", install["key"])
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert [(c["subject"], c["pr"]) for c in st["notes"]["changes"]] == \
        [("Skills say more", 5), ("The thing counts to four", 6)]
    assert [s["step"] for s in st["notes"]["owner_steps"]] == ["run the root step once"]
    assert [h["tag"] for h in st["notes_history"]] == ["v0.1.2"]
    assert us.installed_notes(paths.live, paths.private)["tag"] == "v0.1.3"
    capsys.readouterr()
    args = ["--live", str(paths.live), "--private", str(paths.private), "--ea-dir", str(paths.ea),
            "--lanes", str(install["lanes"])]
    assert update.main(args + ["status"]) == 0
    out = capsys.readouterr().out
    assert "changes:    2 in v0.1.3 since v0.1.2" in out and "- Skills say more" in out
    assert "owner step: run the root step once" in out


def test_the_history_keeps_the_newest_notes():
    st = {}
    for n in range(1, 14):
        update.record_notes(st, {"tag": f"v0.1.{n}", "changes": []})
    update.record_notes(st, {"tag": "v0.1.13", "changes": [{"subject": "again"}]})
    assert st["notes"]["tag"] == "v0.1.13" and st["notes"]["changes"] == [{"subject": "again"}]
    assert [h["tag"] for h in st["notes_history"]] == [f"v0.1.{n}" for n in range(3, 13)]


def test_the_notes_come_from_the_github_release_when_the_api_answers(install):
    import urllib.error
    update.fetch_tags(install["paths"].cache, str(install["origin"]))
    gh = update.Paths(live=install["live"], private=install["priv"], ea=install["ea"],
                      lanes=install["lanes"], repo_url="https://github.com/arpg/tacitus.git")
    page = "https://github.com/arpg/tacitus/releases/tag/v0.1.2"
    body = ("## Changes since v0.1.1\n\n"
            "- The thing counts to two ([#4](https://github.com/arpg/tacitus/pull/4), abcdef1)\n\n"
            "## Owner steps\n\n- restart the bot ([#4](https://github.com/arpg/tacitus/pull/4))\n")
    asked = []

    def answer(slug, tag):
        asked.append((slug, tag))
        return {"html_url": page, "body": body}

    notes = update.release_notes(gh, "v0.1.2", "v0.1.1", fetch=answer)
    assert asked == [("arpg/tacitus", "v0.1.2")]
    assert notes["source"] == "api" and notes["notes_url"] == page and notes["previous"] == "v0.1.1"
    assert notes["changes"] == [{"subject": "The thing counts to two", "pr": 4, "sha": "abcdef1"}]
    assert notes["owner_steps"] == [], "the Release body is shown, never trusted for owner steps"
    # an install that skipped a release reads the whole span from the history, and keeps the link
    first = git(install["origin"], "rev-parse", "v0.1.1^{commit}")
    notes = update.release_notes(gh, "v0.1.2", first, fetch=answer)
    assert notes["source"] == "git" and notes["notes_url"] == page
    assert [c["subject"] for c in notes["changes"]] == ["runtime change"]
    # a Release with no notes in its body: the history
    notes = update.release_notes(gh, "v0.1.2", "v0.1.1",
                                 fetch=lambda s, t: {"html_url": page, "body": "Release v0.1.2.\n"})
    assert notes["source"] == "git" and notes["changes"][0]["subject"] == "runtime change"

    def down(slug, tag):
        raise urllib.error.URLError("no network")

    notes = update.release_notes(gh, "v0.1.2", "v0.1.1", fetch=down)
    assert notes["source"] == "git" and notes["notes_url"] == page, "the Release page CI makes"

    def missing(slug, tag):
        raise urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)

    notes = update.release_notes(gh, "v0.1.2", "v0.1.1", fetch=missing)
    assert notes["source"] == "git" and notes["notes_url"] == "", "no Release: no link"


def test_notes_that_cannot_be_read_never_fail_an_update(install, monkeypatch):
    monkeypatch.setattr(update, "_notes_module", lambda: None)
    paths, host = install["paths"], FakeHost()
    assert update.apply(paths, host, channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert st["installed"] == "v0.1.2" and "notes" not in st


def test_a_check_counts_the_changes_behind_and_the_doctor_shows_them(install):
    paths = install["paths"]
    commit(install["origin"], "skills/a.md", "more\n", "Skills say more (#7)")
    sign_tag(install["origin"], "v0.1.3", install["key"])
    st = update.base_status(paths)
    assert update.resolve_target(paths, st, channel="latest", tag=None)[0] == "v0.1.3"
    assert st["behind"] == {"tag": "v0.1.3", "since": "v0.1.1", "changes": 2}
    st["checked_at"] = update.now_iso()
    update.save_status(paths, st)
    status, detail, _ = us.doctor_row(paths.live, paths.private, paths.ea)
    assert status == "PASS" and "newest on the channel v0.1.3 (2 changes behind)" in detail
    assert update.apply(paths, FakeHost(), channel="latest", gate_kw=fast_gate(), idle_probe=lambda: []) == 0
    st = us.load_status(paths.private)
    assert "behind" not in st and st["notes"]["count"] == 2
    status, detail, _ = us.doctor_row(paths.live, paths.private, paths.ea)
    assert "behind" not in detail and "newest on the channel v0.1.3;" in detail


def test_the_cli_status_and_channel_on_a_hook_tree(env, capsys):
    env["live"].mkdir()
    (env["live"] / ".deployed-commit").write_text("abc1234\n")
    set_config(env["ea"], profile="assistant")
    args = ["--live", str(env["live"]), "--private", str(env["priv"]), "--ea-dir", str(env["ea"])]
    assert update.main(args + ["channel"]) == 0
    assert capsys.readouterr().out.strip() == "off"
    assert update.main(args + ["run"]) == 0
    assert update.main(args + ["status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["mode"] == "plain" and st["channel_source"] == "hook"


# ── deploy.sh for the EA user's copy ────────────────────────────────

def test_deploy_sh_moves_a_copy_to_a_ref_and_checks_the_commit(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    git(src, "commit", "-q", "--allow-empty", "-m", "one")
    one = git(src, "rev-parse", "HEAD")
    git(tmp_path, "clone", "-q", str(src), str(tmp_path / "dst"))
    os.chmod(tmp_path / "dst", 0o755)
    os.chmod(tmp_path / "dst" / ".git", 0o755)
    git(src, "commit", "-q", "--allow-empty", "-m", "two")
    git(src, "tag", "-a", "-m", "t", "v0.0.2")
    two = git(src, "rev-parse", "HEAD")
    sh = str(REPO / "scripts" / "deploy.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["bash", sh, "--from", str(src), "--ref", "refs/tags/v0.0.2", "--expect", two,
                        "--to", str(tmp_path / "dst")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert git(tmp_path / "dst", "rev-parse", "HEAD") == two
    r = subprocess.run(["bash", sh, "--local", "--ref", one[:7], "--expect", one[:7],
                        "--to", str(tmp_path / "dst")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert git(tmp_path / "dst", "rev-parse", "HEAD") == one
    r = subprocess.run(["bash", sh, "--from", str(src), "--ref", "HEAD", "--expect", one,
                        "--to", str(tmp_path / "dst")], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "not " in r.stderr
    os.chmod(tmp_path / "dst", 0o777)
    r = subprocess.run(["bash", sh, "--local", "--ref", one, "--to", str(tmp_path / "dst")],
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "writable by no one else" in r.stderr


# ── what the user sees ──────────────────────────────────────────────

def _layout_at(env, version="v0.1.2", commit_="abc1234"):
    rel = env["opt"] / "ea-system.releases" / version
    rel.mkdir(parents=True, exist_ok=True)
    (rel / ".version").write_text(version + "\n")
    (rel / ".deployed-commit").write_text(commit_ + "\n")
    update.switch(rel.parent, version)
    if not env["live"].is_symlink():
        env["live"].symlink_to("ea-system.releases/current")


def _status(env, **doc):
    update.write_private_json(us.status_file(env["priv"]), doc)


def test_the_doctor_row(env):
    set_config(env["ea"], profile="assistant")
    _layout_at(env)
    now = 2_000_000_000.0
    iso = lambda s: __import__("datetime").datetime.fromtimestamp(now - s, __import__("datetime").timezone.utc).isoformat()
    status, detail, fix = us.doctor_row(env["live"], env["priv"], env["ea"], now=now)
    assert status == "WARN" and "no check yet" in detail and "tacitus-update" in fix
    _status(env, checked_at=iso(600), latest_on_channel="v0.1.2",
            last_apply={"at": iso(900), "to": "v0.1.2", "result": "ok"})
    status, detail, _ = us.doctor_row(env["live"], env["priv"], env["ea"], now=now)
    assert status == "PASS" and "v0.1.2 (abc1234) on latest" in detail and "(ok v0.1.2)" in detail
    _status(env, checked_at=iso(600), held_back={"version": "v0.1.3", "reason": "failed the health gate"})
    status, detail, fix = us.doctor_row(env["live"], env["priv"], env["ea"], now=now)
    assert status == "WARN" and "v0.1.3 held back" in detail and "--tag v0.1.3" in fix
    _status(env, checked_at=iso(4 * 86400))
    assert us.doctor_row(env["live"], env["priv"], env["ea"], now=now)[0] == "WARN"
    _status(env, checked_at=iso(600), last_error={"at": iso(600), "error": "no .release-key.pub at x"})
    status, detail, fix = us.doctor_row(env["live"], env["priv"], env["ea"], now=now)
    assert status == "WARN" and "release-key" in detail and "Supply chain" in fix and "update.json" in fix


def test_the_doctor_calls_the_updates_row(env, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor_t", REPO / "scripts" / "ea-doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    set_config(env["ea"], profile="assistant")
    env["live"].mkdir()
    doctor.RESULTS.clear()
    doctor.check_updates(env["ea"])
    row = [r for r in doctor.RESULTS if r["name"] == "updates"]
    assert row and row[0]["status"] == "PASS" and "hook" in row[0]["detail"]
    assert "        check_updates(ea_dir)\n" in (REPO / "scripts" / "ea-doctor.py").read_text()


def test_the_briefing_line_says_an_update_once(env):
    _layout_at(env, "v0.1.2")
    assert us.briefing_line(env["live"], env["priv"]) == "", "no marker yet: nothing to compare"
    us.mark_briefed(env["live"], env["priv"])
    assert us.briefing_line(env["live"], env["priv"]) == ""
    _layout_at(env, "v0.1.4")
    assert us.briefing_line(env["live"], env["priv"]) == ":gear: Updated to v0.1.4 (from v0.1.2)."
    us.mark_briefed(env["live"], env["priv"])
    assert us.briefing_line(env["live"], env["priv"]) == ""
    _status(env, held_back={"version": "v0.1.5", "reason": "x"})
    assert us.briefing_line(env["live"], env["priv"]) == ":gear: Update v0.1.5 held back; running v0.1.4."
    us.mark_briefed(env["live"], env["priv"])
    assert us.briefing_line(env["live"], env["priv"]) == ""
    st = os.stat(us.briefed_file(env["priv"]))
    assert st.st_mode & 0o077 == 0


def _notes(tag, previous, subjects, count=None, steps=(), url=""):
    return {"tag": tag, "previous": previous, "notes_url": url, "source": "api",
            "count": count if count is not None else len(subjects),
            "changes": [{"subject": s, "pr": i + 1, "sha": f"{i:06x}{tag[-1]}"}
                        for i, s in enumerate(subjects)],
            "owner_steps": [{"step": s, "pr": 1, "sha": ""} for s in steps]}


def test_the_briefing_line_counts_and_names_the_changes_since_the_last_briefing(env):
    _layout_at(env, "v0.1.2")
    us.mark_briefed(env["live"], env["priv"])
    _layout_at(env, "v0.1.4")
    _status(env, notes=_notes("v0.1.4", "v0.1.3", ["Skills say more", "The doctor counts"], count=3,
                              steps=["run the root step once"]),
            notes_history=[_notes("v0.1.2", "v0.1.1", ["already briefed"]),
                           _notes("v0.1.3", "v0.1.2", ["The relay key rotates", "Web alerts post",
                                                       "x" * 120])])
    line = us.briefing_line(env["live"], env["priv"])
    assert line == (":gear: Updated to v0.1.4 (6 changes: The relay key rotates; Web alerts post…)."
                    " Owner step: run the root step once.")
    us.mark_briefed(env["live"], env["priv"])
    _layout_at(env, "v0.1.5")
    _status(env, notes=_notes("v0.1.5", "v0.1.4", ["One change"]))
    assert us.briefing_line(env["live"], env["priv"]) == ":gear: Updated to v0.1.5 (1 change: One change)."
    us.mark_briefed(env["live"], env["priv"])
    _layout_at(env, "v0.1.6")
    _status(env, notes=_notes("v0.1.6", "v0.1.5", ["A" * 200, "B"]))
    line = us.briefing_line(env["live"], env["priv"])
    assert line == f":gear: Updated to v0.1.6 (2 changes: {'A' * 79}…; B)."
    us.mark_briefed(env["live"], env["priv"])
    # notes recorded for another release than the one installed: the plain line
    _layout_at(env, "v0.1.7")
    assert us.briefing_line(env["live"], env["priv"]) == ":gear: Updated to v0.1.7 (from v0.1.6)."


def test_the_version_answer_lists_the_first_three_changes(env):
    set_config(env["ea"], profile="assistant", update={"channel": "latest"})
    _layout_at(env, "v0.1.2")
    url = "https://github.com/arpg/tacitus/releases/tag/v0.1.2"
    _status(env, latest_on_channel="v0.1.2",
            notes=_notes("v0.1.2", "v0.1.1", ["First", "Second", "Third", "Fourth"], count=5,
                         steps=["restart the bot"], url=url))
    text = us.version_text(env["live"], env["priv"], env["ea"])
    assert "\n5 changes in v0.1.2 since v0.1.1:\n- First (#1)\n- Second (#2)\n- Third (#3)\n- and 2 more\n" in text
    assert "Owner step: restart the bot" in text and f"Release notes: {url}" in text
    assert "Fourth" not in text
    _status(env, notes=_notes("v0.1.1", "v0.1.0", ["Older"]))
    assert "change" not in us.version_text(env["live"], env["priv"], env["ea"])
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "update_status.py"), "notes", "--json"],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0 and json.loads(r.stdout) == {}
    _status(env, notes=_notes("v0.1.2", "v0.1.1", ["First", "Second", "Third", "Fourth"]))
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "update_status.py"), "notes"],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0 and "- Fourth (#4)" in r.stdout and "more" not in r.stdout


def test_run_agent_appends_the_line_to_the_morning_briefing_only():
    text = (REPO / "run-agent.sh").read_text()
    i = text.index("update_status.py\" briefing-line")
    block = text[text.rindex("\n#", 0, i):i + 400]
    assert '"$AGENT" == "morning-briefing"' in block
    assert text.index("update_status.py\" briefing-line") < text.index("# Post to Slack via bot token")
    j = text.index("update_status.py\" mark-briefed")
    assert text.index('if [[ "$POST_RC" == "0" ]]') < j


def test_the_version_answer(env):
    set_config(env["ea"], profile="assistant", update={"channel": "latest"})
    _layout_at(env, "v0.1.2")
    _status(env, latest_on_channel="v0.1.3", held_back={"version": "v0.1.3", "reason": "failed the gate"},
            last_apply={"at": "2026-10-31T12:00:00Z", "result": "rolled-back"})
    text = us.version_text(env["live"], env["priv"], env["ea"])
    assert text.startswith(":label: Tacitus v0.1.2 (abc1234).")
    assert "Channel: latest." in text and "Newest on the channel: v0.1.3." in text
    assert "Update v0.1.3 is held back" in text
    old = env["tmp"] / "old"
    old.mkdir()
    (old / ".version").write_text("v0.1.1\n")
    assert "started on v0.1.1" in us.version_text(env["live"], env["priv"], env["ea"], running=old)
    shutil.rmtree(env["opt"])
    env["opt"].mkdir()
    env["live"].mkdir()
    (env["live"] / ".deployed-commit").write_text("def5678\n")
    set_config(env["ea"], profile="assistant")
    text = us.version_text(env["live"], env["priv"], env["ea"])
    assert "commit def5678" in text and "post-commit hook" in text


def test_the_bot_answers_version_without_a_model_call():
    bot = (REPO / "ea-chat-bot.py").read_text()
    m = re.search(r"_VERSION_RE = re\.compile\((r\".*?\")", bot)
    assert m, "the version command's pattern"
    rx = re.compile(eval(m.group(1)), re.I)
    for said in ("version", "/version", " Version ", "version?"):
        assert rx.match(said), said
    for said in ("version of the draft", "what version of the form"):
        assert not rx.match(said), said
    start = bot.index("def _colleague_command(")
    body = bot[start:bot.index("\ndef ", start + 10)]
    assert "_VERSION_RE.match(text)" in body and "version_text(" in body


# ── the hooks leave an updater install alone ────────────────────────

def test_the_deploy_hooks_skip_a_tree_the_updater_manages():
    hook = (REPO / ".githooks" / "post-commit").read_text()
    i = hook.index('if [ -L "$LIVE_DIR" ]; then')
    assert i < hook.index('if [ -d "$DEPLOY_DIR/.git" ]; then') or \
        i < hook.index('elif [ -d "$DEPLOY_DIR/.git" ]; then')
    mac = (REPO / "deploy" / "macos" / "deploy.sh").read_text()
    assert mac.index('[ -L "$LIVE_DIR" ]') < mac.index("deploy_git fetch")


def test_the_root_steps_install_the_layout_and_the_timer():
    harden = (REPO / "deploy" / "harden-permissions.sh").read_text()
    assert 'layout --owner "$OWNER"' in harden and "tacitus-update.timer" in harden
    assert "RandomizedDelaySec=" in harden and "SupplementaryGroups=ea" in harden
    mac = (REPO / "deploy" / "macos" / "install-services.sh").read_text()
    assert 'layout --owner "$OWNER"' in mac and "tacitus-update" in mac
    assert "<key>StartInterval</key>" in mac
    for f in ("tacitus-relay-update.service", "tacitus-relay-update.timer"):
        assert (REPO / "relay" / f).exists(), f
    timer = (REPO / "relay" / "tacitus-relay-update.timer").read_text()
    assert "OnCalendar=hourly" in timer and "RandomizedDelaySec=" in timer
    svc = (REPO / "relay" / "tacitus-relay-update.service").read_text()
    assert "scripts/update.py relay" in svc


def test_the_relay_updater_waits_for_install_sh_to_take_a_tag(tmp_path, capsys):
    base = tmp_path / "relay"
    (base / "src" / "relay").mkdir(parents=True)
    (base / "src" / "relay" / "install.sh").write_text("case $1 in --ref) ;; esac\n")
    assert update.relay(base, tmp_path / "state", channel="latest") == 0
    assert "takes no --tag yet" in capsys.readouterr().out
    assert update.relay(base, tmp_path / "state", channel="off") == 0
    assert update.relay_channel(tmp_path / "missing") == "latest"
    (tmp_path / "ch").write_text("stable\n")
    assert update.relay_channel(tmp_path / "ch") == "stable"


@needs_ssh
def test_the_relay_updater_installs_a_signed_release_with_its_install_sh(tmp_path):
    key = keypair(tmp_path, "release")
    origin = make_origin(tmp_path, key)
    (origin / "relay").mkdir()
    (origin / "relay" / "install.sh").write_text("case $1 in --tag) ;; esac\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "relay")
    sign_tag(origin, "v0.1.1", key)
    base = tmp_path / "relay"
    base.mkdir()
    git(tmp_path, "clone", "-q", str(origin), str(base / "src"))
    commit(origin, "relay/x.txt", "x", "relay change")
    sign_tag(origin, "v0.1.2", key)
    calls = []

    def runner(cmd, cwd=None):
        calls.append(cmd[-1])
        git(base / "src", "fetch", "-q", str(origin), f"refs/tags/{cmd[-1]}")
        git(base / "src", "checkout", "-q", "FETCH_HEAD")
        return subprocess.CompletedProcess(cmd, 0)

    rc = update.relay(base, tmp_path / "state", channel="latest", repo_url=str(origin),
                      healthy=lambda: True, runner=runner)
    assert rc == 0 and calls == ["v0.1.2"]
    st = json.loads((tmp_path / "state" / "status.json").read_text())
    assert st["installed"] == "v0.1.2"
    # the relay's trust root is pinned in its state directory, outside the clone
    pin = tmp_path / "state" / "release-key.pub"
    assert pin.read_text() == Path(str(key) + ".pub").read_text()
    intruder = keypair(tmp_path, "intruder")
    shutil.copy(str(intruder) + ".pub", base / "src" / ".release-key.pub")
    commit(origin, "relay/z.txt", "z", "not ours")
    sign_tag(origin, "v0.1.9", intruder)
    calls.clear()
    assert update.relay(base, tmp_path / "state", channel="latest", repo_url=str(origin),
                        healthy=lambda: True, runner=runner) == 0
    assert calls == [] and pin.read_text() == Path(str(key) + ".pub").read_text()
    git(origin, "tag", "-d", "v0.1.9")
    git(base / "src", "checkout", "-q", ".release-key.pub")
    calls.clear()
    commit(origin, "relay/y.txt", "y", "bad")
    sign_tag(origin, "v0.1.3", key)
    rc = update.relay(base, tmp_path / "state", channel="latest", repo_url=str(origin),
                      healthy=lambda: False, runner=runner)
    assert rc == 1 and calls == ["v0.1.3", "v0.1.2"]
    st = json.loads((tmp_path / "state" / "status.json").read_text())
    assert st["held_back"]["version"] == "v0.1.3"


# ── the release workflow ────────────────────────────────────────────

def test_the_release_job_signs_after_a_green_main_only():
    wf = REPO / ".github" / "workflows" / "ci.yml"
    if not wf.exists():
        print("  (no workflow files in this tree; the Docker image leaves .github out)")
        return
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(wf.read_text())
    job = doc["jobs"]["release"]
    assert job["needs"] == ["ci"] or job["needs"] == "ci"
    cond = job["if"]
    assert "github.event_name == 'push'" in cond and "refs/heads/main" in cond
    assert "needs.ci.result == 'success'" in cond
    assert job["permissions"] == {"contents": "write", "pull-requests": "read"}
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["environment"] == "release", "the secret lives in a protected environment"
    steps = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "scripts/update.py next-version" in steps
    nxt = next(s for s in job["steps"] if s.get("name") == "Next version")
    assert '--github-repo "$GITHUB_REPOSITORY"' in nxt["run"]
    assert nxt["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gpg.format=ssh" in steps and "tag -s" in steps and "verify-tag" in steps
    assert "channels/latest" in steps and "secrets.RELEASE_SIGNING_KEY" in wf.read_text()
    checkout = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkout and checkout[0]["with"]["fetch-depth"] == 0
    assert "release" not in doc["jobs"]["ci"]["needs"], "the summary never waits on a release"
    for name, j in doc["jobs"].items():
        for s in j.get("steps", []):
            if "uses" in s:
                if name == "release":
                    assert re.search(r"@[0-9a-f]{40}$", s["uses"]), (name, s["uses"])
                else:
                    assert re.search(r"@v\d+\.\d+\.\d+$", s["uses"]), (name, s["uses"])


def test_the_signing_key_reaches_one_shell_step_and_no_python():
    """The secret is in the environment of one step, which runs shell,
    ssh-keygen and git; the Python steps (the version, the signers, the
    verify, the notes) run without it, and the job and the workflow set
    no env that carries it. Every action of the job is pinned by commit,
    with its version in a comment."""
    wf = REPO / ".github" / "workflows" / "ci.yml"
    if not wf.exists():
        print("  (no workflow files in this tree; the Docker image leaves .github out)")
        return
    yaml = pytest.importorskip("yaml")
    text = wf.read_text()
    doc = yaml.safe_load(text)
    job = doc["jobs"]["release"]
    assert "RELEASE_SIGNING_KEY" not in json.dumps(doc.get("env") or {})
    assert "RELEASE_SIGNING_KEY" not in json.dumps(job.get("env") or {})
    holders = [s for s in job["steps"] if "secrets.RELEASE_SIGNING_KEY" in json.dumps(s.get("env") or {})]
    assert [s["name"] for s in holders] == ["Sign the tag"]
    sign = holders[0]
    assert "python" not in sign["run"] and "uses" not in sign
    assert sign["run"].index("trap 'rm -f \"$key\"' EXIT") < sign["run"].index('> "$key"')
    assert "GITHUB_ENV" not in sign["run"] and "GITHUB_PATH" not in sign["run"]
    assert text.count("secrets.RELEASE_SIGNING_KEY") == 1
    for s in job["steps"]:
        if "python" in str(s.get("run", "")):
            assert s is not sign
    uses = [ln for ln in text[text.index("\n  release:\n"):].splitlines() if "uses:" in ln]
    assert uses
    for ln in uses:
        assert re.search(r"uses: \S+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", ln), ln


def test_the_release_job_creates_a_github_release_with_the_notes_and_never_fails_on_it():
    wf = REPO / ".github" / "workflows" / "ci.yml"
    if not wf.exists():
        print("  (no workflow files in this tree; the Docker image leaves .github out)")
        return
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load(wf.read_text())["jobs"]["release"]["steps"]
    sign = next(s for s in steps if s.get("name") == "Sign the tag")
    push = next(s for s in steps if s.get("name") == "Verify and push the tag")
    i = steps.index(push)
    notes = steps[i + 1]
    assert sign["id"] == "sign" and push["id"] == "push"
    assert sign["run"].index("exit 0") < sign["run"].index("signed=true"), "no secret: no tag"
    assert push["if"] == "steps.sign.outputs.signed == 'true'", "no tag: no push, no Release"
    run = push["run"]
    assert run.index("scripts/update.py verify-tag") < run.index('git push origin "refs/tags/$TAG"')
    assert run.index('git push origin "refs/tags/$TAG"') < run.index('echo "pushed=true" >> "$GITHUB_OUTPUT"')
    assert notes["if"] == "steps.push.outputs.pushed == 'true'"
    assert notes["continue-on-error"] is True
    assert notes["env"]["GH_TOKEN"] == "${{ github.token }}"
    body = notes["run"]
    assert "python3 scripts/release_notes.py --tag \"$TAG\"" in body
    assert "--pr-bodies" not in body, "owner steps come from the signed history only"
    assert "gh release create \"$TAG\" --verify-tag" in body and "--notes-file \"$notes\"" in body
    assert body.count("::warning::") == 2 and "exit 1" not in body
    for line in body.splitlines():
        if line.lstrip().startswith(("python3 scripts/release_notes.py", "gh release create")):
            raise AssertionError(f"a failure must reach the warning, under an `if`: {line}")
    assert body.count("if ! python3 scripts/release_notes.py") == 1
    assert body.count("if gh release create") == 1


def test_cut_tag_parses():
    r = subprocess.run(["bash", "-n", str(REPO / "scripts" / "cut-tag.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_ssh
def test_cut_tag_moves_the_stable_pointer_to_a_signed_release_only(tmp_path):
    key, other = keypair(tmp_path, "release"), keypair(tmp_path, "other")
    repo = tmp_path / "repo"
    for rel in ("scripts/cut-tag.sh", "scripts/update.py", "preprocessor/update_status.py",
                "preprocessor/ea_config.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, repo / rel)
    shutil.copy(str(key) + ".pub", repo / ".release-key.pub")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    sign_tag(repo, "v0.1.1", key)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    cmd = ["bash", str(repo / "scripts" / "cut-tag.sh"), "--stable", "--skip-tests"]
    r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(repo, "rev-parse", "channels/stable^{commit}") == git(repo, "rev-parse", "HEAD")
    assert "stable: v0.1.1" in git(repo, "tag", "-n1", "--list", "channels/stable")
    commit(repo, "x.txt", "x")
    sign_tag(repo, "v0.1.2", other)
    r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "does not verify" in r.stderr
    assert git(repo, "rev-parse", "channels/stable^{commit}") != git(repo, "rev-parse", "HEAD")


# ── the boundary audit and the sentinel know the layout ─────────────

def test_the_audit_lets_the_owner_hold_the_release_directory(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("boundary_audit_t", REPO / "scripts" / "boundary-audit.py")
    audit = importlib.util.module_from_spec(spec)
    sys.modules["boundary_audit_t"] = audit
    spec.loader.exec_module(audit)
    opt = tmp_path / "opt"
    rel = opt / "ea-system.releases" / "v0.1.2"
    rel.mkdir(parents=True)
    os.chmod(opt / "ea-system.releases", 0o750)
    (rel / ".deployed-commit").write_text("abc1234\n")
    (rel / ".version").write_text("v0.1.2\n")
    update.switch(rel.parent, "v0.1.2")
    live = opt / "ea-system"
    live.symlink_to("ea-system.releases/current")
    ctx = audit.Ctx(ea_dir=tmp_path / "ea", private=tmp_path / "priv", owner_home=tmp_path,
                    owner_logs=tmp_path / "priv" / "logs", live_tree=live, lanes_tree=None,
                    eabot_home=None, resident=None, repo=REPO)
    out = audit.check_code_trees(ctx)
    tree = [r for r in out if r["name"] == "code: the services' tree"][0]
    releases = os.path.realpath(opt / "ea-system.releases")
    assert f"{releases} above it" not in tree.get("detail", ""), tree
    head = [r for r in out if r["name"] == "code: the services' tree is the committed HEAD"]
    assert head and head[0]["status"] == "PASS" and "v0.1.2" in head[0]["detail"]
    # a plain tree under an owner-owned directory is still refused
    plain = tmp_path / "own" / "ea-system"
    plain.mkdir(parents=True)
    os.chmod(plain.parent, 0o755)
    ctx.live_tree = plain
    tree = [r for r in audit.check_code_trees(ctx) if r["name"] == "code: the services' tree"][0]
    assert f"{os.path.realpath(plain.parent)} above it" in tree["detail"]


def test_the_sentinel_compares_ea_bots_copy_with_the_installed_release(tmp_path):
    import sentinel
    rel = tmp_path / "ea-system.releases" / "v0.1.2"
    rel.mkdir(parents=True)
    (rel / ".deployed-commit").write_text("abc1234\n")
    update.switch(rel.parent, "v0.1.2")
    live = tmp_path / "ea-system"
    assert sentinel._updater_release(live) is None
    live.symlink_to("ea-system.releases/current")
    assert sentinel._updater_release(live) == "abc1234"
