#!/usr/bin/env python3
"""Build the hosted Tacitus web app from a signed release.

This file is the build of the public site repository (README.md beside
it). The site's workflow (deploy.yml) runs it in four steps:

    build.py fetch  --src DIR [--repo URL]
        the release tags and main of the public repository into a bare
        repository at DIR (made when missing)
    build.py pick   --src DIR --key release-key.pub [--site URL] [--force]
                    [--github-output FILE]
        the newest vX.Y.Z tag that verifies, and whether the site needs it:
        `deploy` is "yes" unless the site's release.txt already names that
        tag or a newer one
    build.py build  --src DIR --key release-key.pub --tag vX.Y.Z
                    [--commit SHA] [--relays relays.json] --out DIR
        verifies the tag again and writes the site: the release's
        botcore/botcore/chat/webui/, relays.json and release.txt
    build.py verify --src DIR --key release-key.pub --tag vX.Y.Z
        the verification alone; prints the commit

A tag is a release only when every check of docs/updates.md ("The trust
root") holds, against the key list pinned in this repository
(release-key.pub), never a key file from the release:

1. it is an annotated tag whose own name is the name of the ref;
2. it carries an SSH signature and no PGP block;
3. `git verify-tag` with an allowed-signers file made from the pin
   (principal tacitus-release, namespace git) passes and says it checked
   an ED25519 key for that principal;
4. the commit it names is in the history of the repository's main;
5. the release's own .release-key.pub names no key the pin lacks.

A tag that fails is skipped and the next older one is tried. The build
reads the release with git (no checkout and no code from it runs), and
uses the standard library and git only.

release.txt holds three lines: `tag <vX.Y.Z>`, `commit <40 hex>` and
`files <64 hex>`. `files` is the sha256 hex of the canonical JSON of the
app's file map ({path: sha256 hex}, keys sorted, no spaces), with the
rules of botcore/botcore/chat/web_bundle.py: dotfiles and paths a
manifest cannot name are left out. A bot's signed manifest holds the same
map plus the headers.json it writes itself, so a bot at that release
names the same hash for its files without headers.json. The bot's own
bundle hash also covers its signing key and build time, so it differs
from install to install and is not this value.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/arpg/tacitus.git"
PRINCIPAL = "tacitus-release"
KEY_FILE = ".release-key.pub"
KEY_TYPE = "ssh-ed25519"
MAIN_REF = "refs/remotes/origin/main"
FETCH_REFSPECS = ("+refs/tags/v*:refs/tags/v*", f"+refs/heads/main:{MAIN_REF}")
APP_DIR = "botcore/botcore/chat/webui"
TAG_RE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")  # each used with fullmatch: no trailing newline
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# A file of the app as a manifest names it (relay_crypto.MANIFEST_PATH_RE).
PATH_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")
MANIFEST_NAME = "manifest.sig.json"
RELAYS_NAME = "relays.json"
RELEASE_NAME = "release.txt"
MAX_RELAYS = 16
MAX_URL = 200


class BuildError(Exception):
    pass


class VerifyError(BuildError):
    pass


def say(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def version_key(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.fullmatch(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ── git ──────────────────────────────────────────────────────────────

def _git_env() -> dict:
    """This environment without git's repository variables, with no
    terminal prompt and messages in English."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C")
    return env


def git(repo: Path, *args: str, check: bool = True, text: bool = True,
        timeout: float = 600) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text,
                       timeout=timeout, env=_git_env())
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        raise BuildError(f"git {args[0]} failed: {err.strip()[:300]}")
    return r


def git_out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def fetch(src: Path, url: str = REPO_URL) -> None:
    """The release tags and main (as MAIN_REF) into a bare repository at
    `src`. Forced and pruned, so the tags are the remote's."""
    if not (src / "HEAD").exists():
        src.mkdir(parents=True, exist_ok=True)
        git(src, "init", "-q", "--bare")
    git(src, "fetch", "--quiet", "--force", "--prune", "--no-tags", url, *FETCH_REFSPECS)


def release_tags(src: Path) -> list[str]:
    """The vX.Y.Z tags, newest first."""
    tags = [t for t in git_out(src, "tag", "--list", "v*").splitlines() if version_key(t)]
    return sorted(tags, key=version_key, reverse=True)


# ── the release key ──────────────────────────────────────────────────

def release_keys(key_text: str, name: str = KEY_FILE) -> list[tuple[str, str]]:
    """The (type, base64) pairs of a key list: one key per line, comments
    and blank lines skipped. Every key is ssh-ed25519."""
    out = []
    for raw in key_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or not re.fullmatch(r"(ssh|ecdsa|sk)-[a-z0-9@.-]+", parts[0]) \
                or not re.fullmatch(r"[A-Za-z0-9+/]{40,}={0,3}", parts[1]):
            raise VerifyError(f"{name}: not an SSH public key line: {line[:40]}")
        if parts[0] != KEY_TYPE:
            raise VerifyError(f"{name}: a {parts[0]} key; a release key is {KEY_TYPE}")
        out.append((parts[0], parts[1]))
    if not out:
        raise VerifyError(f"{name} holds no key")
    return out


def allowed_signers(key_text: str) -> str:
    """Each key line as `tacitus-release namespaces="git" ssh-ed25519 <key>`."""
    return "".join(f'{PRINCIPAL} namespaces="git" {kind} {b64}\n'
                   for kind, b64 in release_keys(key_text, "release-key.pub"))


def read_pin(path: Path) -> str:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        raise VerifyError(f"no release key at {path}: nothing can be verified")
    release_keys(text, path.name)
    return text


# ── verification ─────────────────────────────────────────────────────

def verify_tag(src: Path, tag: str, signers: Path) -> str:
    """The full commit `tag` names, once it is an annotated tag of that
    name with an SSH signature git verifies for the release principal
    with an ED25519 key in `signers`."""
    if not version_key(tag):
        raise VerifyError(f"{tag} is not a release tag name (vX.Y.Z)")
    ref = f"refs/tags/{tag}"
    kind = git(src, "cat-file", "-t", ref, check=False).stdout.strip()
    if kind != "tag":
        raise VerifyError(f"{tag} is not an annotated tag ({kind or 'missing'})")
    body = git_out(src, "cat-file", "tag", ref)
    header, _, rest = body.partition("\n\n")
    fields = dict(line.split(" ", 1) for line in header.splitlines() if " " in line)
    if fields.get("tag") != tag:
        raise VerifyError(f"{tag} holds the tag object of {fields.get('tag')!r}")
    if fields.get("type") != "commit":
        raise VerifyError(f"{tag} does not name a commit")
    if "-----BEGIN SSH SIGNATURE-----" not in rest or "-----BEGIN PGP" in rest:
        raise VerifyError(f"{tag} carries no SSH signature")
    r = git(src, "-c", f"gpg.ssh.allowedSignersFile={signers}", "-c", "gpg.ssh.program=ssh-keygen",
            "verify-tag", ref, check=False)
    said = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or f'Good "git" signature for {PRINCIPAL}' not in said:
        last = said.strip().splitlines()[-1][:120] if said.strip() else "no output"
        raise VerifyError(f"{tag}: signature not from the release key ({last})")
    if f'Good "git" signature for {PRINCIPAL} with ED25519 key' not in said:
        raise VerifyError(f"{tag}: not signed with an ED25519 key")
    return git_out(src, "rev-parse", f"{ref}^{{commit}}")


def on_main(src: Path, commit: str) -> bool:
    if git(src, "rev-parse", "-q", "--verify", f"{MAIN_REF}^{{commit}}", check=False).returncode:
        return False
    return git(src, "merge-base", "--is-ancestor", commit, MAIN_REF, check=False).returncode == 0


def tree_keys(src: Path, commit: str) -> set[tuple[str, str]] | None:
    r = git(src, "show", f"{commit}:{KEY_FILE}", check=False)
    if r.returncode != 0:
        return None
    try:
        return set(release_keys(r.stdout))
    except VerifyError:
        return None


def verify_release(src: Path, tag: str, pin: str) -> str:
    """The commit of `tag` when all five checks hold; VerifyError otherwise."""
    with tempfile.TemporaryDirectory(prefix="hosted-signers-") as td:
        signers = Path(td) / "allowed_signers"
        signers.write_text(allowed_signers(pin))
        commit = verify_tag(src, tag, signers)
    if not on_main(src, commit):
        raise VerifyError(f"{tag} names {commit[:12]}, which is not in the history of main")
    theirs = tree_keys(src, commit)
    if theirs is None or not theirs <= set(release_keys(pin, "release-key.pub")):
        raise VerifyError(f"{tag} changes {KEY_FILE}; it builds once release-key.pub here "
                          f"holds the new key (README.md, A new release key)")
    return commit


def newest_release(src: Path, pin: str) -> tuple[str, str, list[str]] | None:
    """(tag, commit, notes) for the newest tag that verifies, else None.
    Each tag that fails adds a note."""
    notes: list[str] = []
    for tag in release_tags(src):
        try:
            return tag, verify_release(src, tag, pin), notes
        except VerifyError as e:
            notes.append(str(e))
    return None


# ── what the site serves now ─────────────────────────────────────────

def parse_release(text: str) -> dict:
    """The fields of a release.txt; only the three known ones, each
    checked."""
    out = {}
    for line in (text or "").splitlines()[:10]:
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "tag" and version_key(value):
            out["tag"] = value
        elif key == "commit" and COMMIT_RE.fullmatch(value):
            out["commit"] = value
        elif key == "files" and re.fullmatch(r"[0-9a-f]{64}", value):
            out["files"] = value
    return out


def deployed(site: str, opener=urllib.request.urlopen) -> dict:
    """The site's release.txt, parsed; {} when it has none or does not
    answer (the build then deploys)."""
    if not site:
        return {}
    url = site.rstrip("/") + "/" + RELEASE_NAME
    try:
        with opener(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}),
                    timeout=20) as r:
            return parse_release(r.read(4096).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        say(f"no release on the site yet ({url}: {e})")
        return {}


def should_deploy(tag: str, current: dict, force: bool = False) -> tuple[bool, str]:
    have = current.get("tag", "")
    if force:
        return True, f"{tag}: deploy asked for by hand (the site has {have or 'nothing'})"
    if not have:
        return True, f"{tag}: the site has no release yet"
    if version_key(have) == version_key(tag):
        return False, f"the site already serves {tag}"
    if version_key(have) > version_key(tag):
        return False, f"the site serves {have}, newer than {tag}; never moved back"
    return True, f"{tag}: newer than {have} on the site"


# ── the site ─────────────────────────────────────────────────────────

def check_relays(doc) -> list[str]:
    """`doc` when it is a list of relay URLs a page may call: https, a
    host, no user, query or fragment; a list of at most MAX_RELAYS."""
    if not isinstance(doc, list) or len(doc) > MAX_RELAYS:
        raise BuildError(f"{RELAYS_NAME} is a JSON list of at most {MAX_RELAYS} relay URLs")
    seen = set()
    for url in doc:
        if not isinstance(url, str) or len(url) > MAX_URL or url != url.strip() \
                or re.search(r"[\s\\\"'<>`]", url):
            raise BuildError(f"{RELAYS_NAME}: not a relay URL: {url!r}")
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or not p.hostname or "@" in p.netloc or p.query or p.fragment \
                or url.endswith("/") or "?" in url or "#" in url:
            raise BuildError(f"{RELAYS_NAME}: a relay URL is https://host/path, "
                             f"with no trailing slash, query or user: {url!r}")
        if url in seen:
            raise BuildError(f"{RELAYS_NAME}: {url} is listed twice")
        seen.add(url)
    return list(doc)


def read_relays(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        say(f"no {RELAYS_NAME}: the site lists no relay; a device then needs the address "
            f"the bot shows, which names its relay")
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise BuildError(f"{path}: not JSON ({e})")
    return check_relays(doc)


def canonical_json(obj) -> bytes:
    """relay_crypto.canonical_json: keys sorted, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def app_files(src: Path, commit: str) -> tuple[dict[str, bytes], list[str]]:
    """({path under webui/: its bytes}, the paths left out) at `commit`,
    with web_bundle.file_hashes' rules: a dotfile or a file in a dot
    directory, a path a manifest cannot name, a link, a submodule and
    manifest.sig.json are left out."""
    listing = git(src, "ls-tree", "-r", "-z", "--full-tree", commit, "--", APP_DIR + "/",
                  text=False).stdout
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        mode, kind, obj = meta.decode().split()
        full = name.decode("utf-8", "replace")
        if not full.startswith(APP_DIR + "/"):
            continue
        rel = full[len(APP_DIR) + 1:]
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        if rel == MANIFEST_NAME:
            continue
        if kind != "blob" or mode not in ("100644", "100755") or not PATH_RE.fullmatch(rel):
            skipped.append(rel)
            continue
        files[rel] = git(src, "cat-file", "blob", obj, text=False).stdout
    return files, skipped


def files_hash(files: dict[str, bytes]) -> str:
    return hashlib.sha256(canonical_json(
        {p: hashlib.sha256(b).hexdigest() for p, b in files.items()})).hexdigest()


def release_text(tag: str, commit: str, digest: str) -> str:
    return f"tag {tag}\ncommit {commit}\nfiles {digest}\n"


def build(src: Path, tag: str, pin: str, out: Path, relays: list[str],
          expect_commit: str = "") -> dict:
    """Verify `tag` and write the site into `out` (made empty first)."""
    commit = verify_release(src, tag, pin)
    if expect_commit and commit != expect_commit:
        raise VerifyError(f"{tag} names {commit[:12]} now, not {expect_commit[:12]}")
    files, skipped = app_files(src, commit)
    if "index.html" not in files or "sw.js" not in files:
        raise BuildError(f"{tag} has no web app in {APP_DIR}/")
    for name in (RELAYS_NAME, RELEASE_NAME):
        if name in files:
            raise BuildError(f"{tag}: the app has a file named {name}, which the site writes")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for rel, data in files.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        os.chmod(dest, 0o644)
    digest = files_hash(files)
    (out / RELAYS_NAME).write_text(json.dumps(relays, indent=1) + "\n")
    (out / RELEASE_NAME).write_text(release_text(tag, commit, digest))
    for rel in skipped:
        say(f"left out {rel} (not a file the app's check can name)")
    say(f"built {tag} ({commit[:12]}): {len(files)} files, {len(relays)} relays, files {digest[:16]}")
    return {"tag": tag, "commit": commit, "files": digest, "count": len(files)}


# ── the command line ─────────────────────────────────────────────────

def _outputs(path: str, values: dict) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    if path:
        with open(path, "a") as f:
            f.write(lines)
    sys.stdout.write(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("--src", required=True)
    p.add_argument("--repo", default=REPO_URL)
    for name in ("pick", "verify", "build"):
        p = sub.add_parser(name)
        p.add_argument("--src", required=True)
        p.add_argument("--key", required=True)
        if name == "pick":
            p.add_argument("--site", default="")
            p.add_argument("--force", action="store_true")
            p.add_argument("--github-output", default="")
        else:
            p.add_argument("--tag", required=True)
        if name == "build":
            p.add_argument("--commit", default="")
            p.add_argument("--relays", default="")
            p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        src = Path(a.src)
        if a.cmd == "fetch":
            if not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+", a.repo) \
                    and not Path(a.repo).is_dir():
                raise BuildError("--repo takes an https URL")
            fetch(src, a.repo)
            say(f"fetched {len(release_tags(src))} release tags")
            return 0
        pin = read_pin(Path(a.key))
        if a.cmd == "verify":
            print(verify_release(src, a.tag, pin))
            return 0
        if a.cmd == "pick":
            got = newest_release(src, pin)
            if got is None:
                raise VerifyError("no tag verifies against release-key.pub")
            tag, commit, notes = got
            for note in notes:
                print(f"::warning::{note}", flush=True)
            go, why = should_deploy(tag, deployed(a.site), a.force)
            say(why)
            _outputs(a.github_output, {"tag": tag, "commit": commit, "deploy": "yes" if go else "no"})
            return 0
        if a.commit and not COMMIT_RE.fullmatch(a.commit):
            raise BuildError("--commit takes a full commit id")
        relays = read_relays(Path(a.relays) if a.relays else None)
        build(src, a.tag, pin, Path(a.out), relays, a.commit)
        return 0
    except BuildError as e:
        print(f"::error::{e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
