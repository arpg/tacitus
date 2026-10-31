#!/usr/bin/env python3
"""Install signed Tacitus releases: fetch, verify, stage, swap, restart, check.

The design is docs/updates.md. In short: every green push to main is a
release tag vX.Y.Z signed in CI with an SSH key whose public half is
.release-key.pub; this script, run hourly as the owner, installs the newest
signed release on the install's channel into /opt/ea-system.releases/<tag>,
switches the link /opt/ea-system points through, restarts the services
whose in-memory code changed once nothing is running, and switches back
when they do not come up healthy.

    update.py status [--json]            what is installed and what the channel offers
    update.py check [--json]             fetch the tags, verify, say whether an update is due
    update.py apply [--tag vX.Y.Z] [--dry-run]
    update.py run [--jitter SECONDS]     the timer's entry: apply on the channel, quiet when current
    update.py restart                    restart the services, once idle
    update.py verify                     the health gate on what runs now
    update.py rollback                   switch back to the previous release
    update.py channel                    the effective channel (the root step reads it)
    update.py layout --owner NAME [--seed DIR]          root, once: the release layout
    update.py next-version [--head SHA] [--main REF]    CI: the next tag, or nothing
    update.py allowed-signers --key FILE                CI and cut-tag.sh
    update.py verify-tag --repo DIR --tag vX.Y.Z [--key FILE]
    update.py relay [--dry-run]          the relay host's updater (root)

Standard library and git only. It never runs as the EA user; `layout` and
`relay` run as root, everything else as the owner.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parent
sys.path.insert(0, str(TREE / "preprocessor"))
import update_status as us  # noqa: E402

REPO_URL = "https://github.com/arpg/tacitus.git"
PRINCIPAL = "tacitus-release"
KEY_FILE = ".release-key.pub"
STABLE_REF = "refs/tags/channels/stable"
FETCH_REFSPECS = ("+refs/tags/v*:refs/tags/v*", "+refs/tags/channels/*:refs/tags/channels/*")
# Every update restarts both services. Each resolves its own path when it
# starts (Path(__file__).resolve()), so it keeps reading the release it
# started from: skills and CLAUDE.md included, not only the code it holds
# in memory. A restart moves it to the new release, and only then is the
# old one safe to delete.
ROLES = ("bot", "mediator")
IDLE_WAIT_S = 600
IDLE_POLL_S = 5
GATE_S = 60
GATE_POLL_S = 3
# The service must hold one main process this long, with every marker seen.
GATE_STABLE_S = 10
KEEP_RELEASES = 3
# Health markers in the bot's log (ea-chat-bot.py, botcore/botcore/chat/).
START_RE = re.compile(r"EA chat bot starting")
FRONTEND_RE = re.compile(r"frontend=\w+ stream=|Bot user ID: \S|web chat serving on ")
WEB_RE = re.compile(r"web chat serving on ")
BAD_RE = re.compile(r"Traceback \(most recent call last\)|chat frontend refused to start|"
                    r"chat frontend did not start|\bCRITICAL\b")
# Lanes the bot runs for the owner that a restart would cut short; the
# deploy hook counts the same ones.
IDLE_LANES = ("resident", "deferred")
RELAY_BASE = Path("/opt/tacitus-relay")
RELAY_STATE = Path("/var/lib/tacitus-relay")
RELAY_CHANNEL_FILE = Path("/etc/tacitus-relay/update-channel")
RELAY_HEALTH = "http://127.0.0.1:8471/healthz"


class UpdateError(Exception):
    pass


class VerifyError(UpdateError):
    pass


def say(msg: str) -> None:
    print(f"[update] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── git ──────────────────────────────────────────────────────────────

def _git_env() -> dict:
    """This environment without git's repository variables (a hook or a
    caller's GIT_DIR would point every command at another repository), with
    no terminal prompt and messages in English."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C")
    return env


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120,
        stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], stdout=stdout, stderr=subprocess.PIPE,
                       text=True, timeout=timeout, env=_git_env())
    if check and r.returncode != 0:
        raise UpdateError(f"git {args[0]} failed: {(r.stderr or '').strip()[:300]}")
    return r


def git_out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def release_tags(repo: Path, merged: str | None = None) -> list[str]:
    """The vX.Y.Z tags, newest first (optionally only those merged into
    `merged`)."""
    args = ["tag", "--list", "v*"] + (["--merged", merged] if merged else [])
    tags = [t for t in git_out(repo, *args).splitlines() if us.version_key(t)]
    return sorted(tags, key=us.version_key, reverse=True)


def fetch_tags(cache: Path, url: str) -> None:
    """The release tags and the channel pointers into the owner's bare
    cache. Forced, so a moved channels/ tag follows; pruned, so a deleted
    release is not offered."""
    if not (cache / "HEAD").exists():
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(cache)], check=True,
                       env=_git_env(), capture_output=True)
    git(cache, "fetch", "--quiet", "--force", "--prune", "--no-tags", url, *FETCH_REFSPECS,
        timeout=600)


# ── signatures ───────────────────────────────────────────────────────

def allowed_signers(key_text: str) -> str:
    """An allowed-signers file from .release-key.pub: each key line becomes
    `tacitus-release namespaces="git" <type> <key>`."""
    out = []
    for raw in key_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or not re.fullmatch(r"(ssh|ecdsa|sk)-[a-z0-9@.-]+", parts[0]) \
                or not re.fullmatch(r"[A-Za-z0-9+/]{40,}={0,3}", parts[1]):
            raise VerifyError(f"{KEY_FILE}: not an SSH public key line: {line[:40]}")
        out.append(f'{PRINCIPAL} namespaces="git" {parts[0]} {parts[1]}')
    if not out:
        raise VerifyError(f"{KEY_FILE} holds no key")
    return "\n".join(out) + "\n"


def write_signers(key_file: Path, dest: Path) -> Path:
    try:
        text = key_file.read_text()
    except OSError:
        raise VerifyError(f"no {KEY_FILE} at {key_file}: nothing can be verified")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(allowed_signers(text))
    os.replace(tmp, dest)
    return dest


def verify_tag(repo: Path, tag: str, signers: Path) -> str:
    """The full commit `tag` names, once it proves to be a release signed
    by a key in `signers`: an annotated tag whose own name is `tag`, with
    an SSH signature that git verifies for the release principal."""
    if not us.version_key(tag):
        raise VerifyError(f"{tag} is not a release tag name (vX.Y.Z)")
    ref = f"refs/tags/{tag}"
    kind = git(repo, "cat-file", "-t", ref, check=False).stdout.strip()
    if kind != "tag":
        raise VerifyError(f"{tag} is not an annotated tag ({kind or 'missing'})")
    body = git_out(repo, "cat-file", "tag", ref)
    header, _, rest = body.partition("\n\n")
    fields = dict(line.split(" ", 1) for line in header.splitlines() if " " in line)
    if fields.get("tag") != tag:
        raise VerifyError(f"{tag} holds the tag object of {fields.get('tag')!r}")
    if fields.get("type") != "commit":
        raise VerifyError(f"{tag} does not name a commit")
    if "-----BEGIN SSH SIGNATURE-----" not in rest or "-----BEGIN PGP" in rest:
        raise VerifyError(f"{tag} carries no SSH signature")
    r = git(repo, "-c", f"gpg.ssh.allowedSignersFile={signers}", "-c", "gpg.ssh.program=ssh-keygen",
            "verify-tag", ref, check=False)
    said = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or f'Good "git" signature for {PRINCIPAL}' not in said:
        raise VerifyError(f"{tag}: signature not from the release key "
                          f"({said.strip().splitlines()[-1][:120] if said.strip() else 'no output'})")
    return git_out(repo, "rev-parse", f"{ref}^{{commit}}")


def channel_candidates(repo: Path, channel: str) -> list[str]:
    """The tags a channel offers, newest first. latest: every vX.Y.Z tag.
    stable: the vX.Y.Z tags at the commit channels/stable points to (the
    pointer is not signed; it only chooses among signed releases)."""
    if channel == "latest":
        return release_tags(repo)
    if channel == "stable":
        r = git(repo, "rev-parse", "-q", "--verify", f"{STABLE_REF}^{{commit}}", check=False)
        if r.returncode != 0:
            return []
        at = git_out(repo, "tag", "--points-at", r.stdout.strip(), "--list", "v*").splitlines()
        return sorted([t for t in at if us.version_key(t)], key=us.version_key, reverse=True)
    return []


def pick(repo: Path, channel: str, signers: Path, installed: str = "",
         held: str = "") -> tuple[str, str, list[str]] | None:
    """(tag, commit, notes) for the newest signed release on the channel
    that is newer than `installed` and is not `held`, else None. A tag that
    fails verification is skipped with a note, never installed."""
    notes: list[str] = []
    floor = us.version_key(installed)
    for tag in channel_candidates(repo, channel):
        if floor is not None and us.version_key(tag) <= floor:
            break
        if tag == held:
            notes.append(f"{tag} is held back")
            continue
        try:
            return tag, verify_tag(repo, tag, signers), notes
        except VerifyError as e:
            notes.append(str(e))
    return None


def newest_signed(repo: Path, channel: str, signers: Path) -> str:
    for tag in channel_candidates(repo, channel):
        try:
            verify_tag(repo, tag, signers)
            return tag
        except VerifyError:
            continue
    return ""


# ── versions for CI ──────────────────────────────────────────────────

BUMP_MAJOR_RE = re.compile(r"#major\b|^release:\s*major\s*$", re.I | re.M)
BUMP_MINOR_RE = re.compile(r"#minor\b|^release:\s*minor\s*$", re.I | re.M)


def bump(version: str, messages: str) -> str:
    major, minor, patch = us.version_key(version) or (0, 1, 0)
    if BUMP_MAJOR_RE.search(messages):
        return f"v{major + 1}.0.0"
    if BUMP_MINOR_RE.search(messages):
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def next_version(repo: Path, head: str = "HEAD", main: str = "HEAD") -> str:
    """The tag the release job puts on `head`, or "" when a release already
    contains it (a re-run, or a newer push that finished first). The last
    release is the newest vX.Y.Z tag in `main`'s history; the commit
    messages since it may raise the minor or the major number."""
    head_sha = git_out(repo, "rev-parse", f"{head}^{{commit}}")
    tags = release_tags(repo, merged=main)
    if not tags:
        return "v0.1.0"
    last = tags[0]
    last_sha = git_out(repo, "rev-parse", f"refs/tags/{last}^{{commit}}")
    if git(repo, "merge-base", "--is-ancestor", head_sha, last_sha, check=False).returncode == 0:
        return ""
    messages = git_out(repo, "log", "--format=%B", f"{last_sha}..{head_sha}")
    nxt = bump(last, messages)
    if git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{nxt}", check=False).returncode == 0:
        raise UpdateError(f"{nxt} already exists outside {main}'s history; tag by hand")
    return nxt


# ── paths ────────────────────────────────────────────────────────────

class Paths:
    def __init__(self, live: Path | None = None, releases: Path | None = None,
                 private: Path | None = None, ea: Path | None = None,
                 lanes: Path | None | bool = None, repo_url: str | None = None):
        self.live = Path(live) if live else us.live_dir()
        self.releases = Path(releases) if releases else us.releases_dir(self.live)
        self.private = Path(private) if private else us.private_dir()
        self.ea = Path(ea) if ea else us.ea_dir()
        if lanes is None:
            lanes = _eabot_tree()
        self.lanes = Path(lanes) if lanes else None
        self.repo_url = repo_url or self._repo_url()

    def _repo_url(self) -> str:
        doc = us.read_owner_json(self.private / "update.json") or {}
        url = doc.get("repo")
        return url if isinstance(url, str) and url else REPO_URL

    @property
    def dir(self) -> Path:
        return us.update_dir(self.private)

    @property
    def cache(self) -> Path:
        return self.dir / "tacitus.git"

    @property
    def signers(self) -> Path:
        return self.dir / "allowed_signers"

    @property
    def lock(self) -> Path:
        return self.dir / "update.lock"

    @property
    def inflight(self) -> Path:
        return self.private / "bot-inflight.json"

    @property
    def pending(self) -> Path:
        return self.private / "deploy-pending.json"

    @property
    def shared_status(self) -> Path:
        return self.ea / "state" / "update-status.json"


def _eabot_tree() -> Path | None:
    try:
        import pwd
        home = Path(pwd.getpwnam("ea-bot").pw_dir)
    except (ImportError, KeyError):
        return None
    try:
        home = Path(os.path.realpath(home))
    except OSError:
        pass
    return home / "ea-system"


# ── status ───────────────────────────────────────────────────────────

def write_private_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_status(paths: Paths, doc: dict) -> None:
    """The owner's copy, then the copy the EA reads (written as the EA user
    through resident_io in an owner process; skipped when that fails)."""
    doc = dict(doc, schema=1, written_at=now_iso())
    write_private_json(us.status_file(paths.private), doc)
    try:
        import resident_io
        if paths.shared_status.parent.is_dir():
            resident_io.replace_text(paths.shared_status, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    except Exception as e:
        say(f"could not write {paths.shared_status}: {e}")


def base_status(paths: Paths) -> dict:
    st = us.load_status(paths.private)
    channel, source = us.effective_channel(paths.live, paths.private, paths.ea)
    inst = us.installed(paths.live)
    st.update(channel=channel, channel_source=source, mode=us.tree_kind(paths.live),
              installed=inst["version"], installed_commit=inst["commit"])
    return st


# ── staging and the swap ─────────────────────────────────────────────

def _ea_gid() -> int:
    try:
        import grp
        return grp.getgrnam("ea").gr_gid
    except (ImportError, KeyError):
        return -1


def export_tree(repo: Path, commit: str, dest: Path, tag: str = "") -> Path:
    """`git archive` of `commit` into `dest` (new), with .version and
    .deployed-commit beside the code, the files the deploys write."""
    dest.mkdir(mode=0o755)
    with subprocess.Popen(["git", "-C", str(repo), "-c", "tar.umask=022", "archive",
                           "--format=tar", commit], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=_git_env()) as arch:
        untar = subprocess.run(["tar", "-x", "-C", str(dest), "-f", "-"], stdin=arch.stdout,
                               capture_output=True)
        arch.stdout.close()
        err = arch.stderr.read().decode(errors="replace")
        arch.wait()
    if arch.returncode != 0 or untar.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise UpdateError(f"export of {commit[:12]} failed: {err.strip()[:200]}"
                          f"{untar.stderr.decode(errors='replace').strip()[:200]}")
    if tag:
        (dest / ".version").write_text(tag + "\n")
    (dest / ".deployed-commit").write_text(commit[:7] + "\n")
    os.chmod(dest, 0o750)
    gid = _ea_gid()
    if gid >= 0:
        try:
            os.chown(dest, -1, gid)
        except OSError:
            pass
    return dest


def smoke(tree: Path, python: str | None = None) -> list[str]:
    """The cheap checks a release passes before it is switched in: the
    config templates validate, and every entry point compiles."""
    problems: list[str] = []
    py = python or sys.executable
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "EA_"))}
    env.update(CI="1", PYTHONDONTWRITEBYTECODE="1")
    validator = tree / "tests" / "validate_config.py"
    if validator.exists():
        r = subprocess.run([py, str(validator), "--ci"], cwd=str(tree), env=env,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            fails = [ln.strip() for ln in r.stdout.splitlines() if "FAIL" in ln]
            problems.append("validate_config.py --ci: " + ("; ".join(fails[:3]) or f"exit {r.returncode}"))
    else:
        problems.append("tests/validate_config.py is missing")
    files = [tree / "ea-chat-bot.py", tree / "scripts" / "update.py", tree / "scripts" / "ea-doctor.py"]
    files += sorted((tree / "preprocessor").glob("*.py"))
    for f in files:
        try:
            compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except FileNotFoundError:
            problems.append(f"{f.relative_to(tree)} is missing")
        except (SyntaxError, ValueError) as e:
            problems.append(f"{f.relative_to(tree)} does not compile: {e}")
    for sh in ("run-agent.sh", "run-plugin.sh"):
        p = tree / sh
        if not p.exists():
            problems.append(f"{sh} is missing")
            continue
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"{sh}: {r.stderr.strip()[:200]}")
    return problems


def link_target(releases: Path, name: str) -> str | None:
    try:
        return os.readlink(releases / name)
    except OSError:
        return None


def switch(releases: Path, target: str, name: str = "current") -> None:
    """Point releases/<name> at `target` (a release directory's name): a new
    link renamed over the old one, which is atomic."""
    tmp = releases / f".{name}.new"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, releases / name)


def promote(staged: Path, releases: Path, tag: str) -> Path:
    final = releases / tag
    if final.exists() or final.is_symlink():
        if tag in (link_target(releases, "current"), link_target(releases, "previous")):
            raise UpdateError(f"{final} is in use; not replacing it")
        shutil.rmtree(final)
    os.rename(staged, final)
    return final


def prune(releases: Path, keep: int = KEEP_RELEASES) -> list[str]:
    """Delete release directories other than current and previous, keeping
    the `keep` newest; and stages more than an hour old."""
    pinned = {link_target(releases, "current"), link_target(releases, "previous")}
    dirs = [p for p in releases.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]
    dirs.sort(key=lambda p: (us.version_key(p.name) or (-1, -1, -1), p.stat().st_mtime), reverse=True)
    gone = []
    for p in dirs[keep:]:
        if p.name in pinned:
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    for p in releases.glob(".staging-*"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return gone


# ── the EA user's copy ───────────────────────────────────────────────

def update_lanes(paths: Paths, *, ref: str, expect: str, source: Path | None) -> None:
    """Move the EA user's copy to `expect` with scripts/deploy.sh, which
    enters that tree and checks it the way the deploy hook does. `source`
    None resets to a commit the copy already has (a rollback)."""
    if paths.lanes is None or not (paths.lanes / ".git").is_dir():
        say("no EA user copy to move (no ea-bot user or no git tree there)")
        return
    cmd = ["bash", str(HERE / "deploy.sh"), "--to", str(paths.lanes), "--ref", ref, "--expect", expect]
    cmd += ["--from", str(source)] if source is not None else ["--local"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=_git_env(), timeout=600)
    if r.returncode != 0:
        raise UpdateError(f"deploy.sh to {paths.lanes} failed: "
                          f"{(r.stderr or r.stdout).strip().splitlines()[-1:] or ['?']}")


# ── idle ─────────────────────────────────────────────────────────────

def inflight_count(path: Path) -> int:
    doc = us.read_owner_json(path) or {}
    return sum(1 for v in doc.values() if isinstance(v, dict) and v.get("lane") not in IDLE_LANES)


def lane_processes(ps_text: str | None = None, uid: int | None = None) -> list[int]:
    """The owner's run-agent.sh and run-plugin.sh runs: a process whose
    program, or whose shell's script, is one of them. A command line that
    only mentions the name (grep, an editor) is not a lane."""
    uid = os.geteuid() if uid is None else uid
    if ps_text is None:
        r = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "uid=", "-o", "args="],
                           capture_output=True, text=True)
        ps_text = r.stdout
    out = []
    for line in ps_text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, puid, argv = int(parts[0]), int(parts[1]), parts[2].split()
        if puid != uid or pid == os.getpid():
            continue
        names = [os.path.basename(a) for a in argv[:2]]
        lane = names[0] in ("run-agent.sh", "run-plugin.sh") or (
            len(names) > 1 and names[0] in ("bash", "sh") and names[1] in ("run-agent.sh", "run-plugin.sh"))
        if lane:
            out.append(pid)
    return out


def busy_reasons(paths: Paths, ps_text: str | None = None) -> list[str]:
    reasons = []
    n = inflight_count(paths.inflight)
    if n:
        reasons.append(f"{n} chat turn(s) in flight")
    lanes = lane_processes(ps_text)
    if lanes:
        reasons.append(f"{len(lanes)} cron lane(s) running")
    return reasons


def wait_idle(paths: Paths, timeout: float = IDLE_WAIT_S, *, probe=None, sleep=time.sleep,
              clock=time.monotonic) -> list[str]:
    """[] once nothing runs; the last reasons when `timeout` passes first."""
    probe = probe or (lambda: busy_reasons(paths))
    deadline = clock() + timeout
    reasons = probe()
    shown = 0.0
    while reasons and clock() < deadline:
        if clock() - shown >= 60:
            say("waiting: " + ", ".join(reasons))
            shown = clock()
        sleep(IDLE_POLL_S)
        reasons = probe()
    return reasons


# ── services ─────────────────────────────────────────────────────────

class Host:
    """The services on this machine: how to restart one, whether it runs,
    and what it logged. tests/test_update.py passes a fake with the same
    methods."""

    def __init__(self, private: Path):
        self.private = private
        self.darwin = sys.platform == "darwin"

    def _run(self, *cmd: str, timeout: float = 180) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(list(cmd), 1, "", str(e))

    def _label(self, name: str) -> str:
        import launchd_labels
        return launchd_labels.label(name)

    def _unit_loaded(self, unit: str) -> bool:
        r = self._run("systemctl", "show", "-p", "LoadState", "--value", unit)
        return r.returncode == 0 and r.stdout.strip() not in ("", "not-found")

    def service(self, role: str) -> str | None:
        """The installed service for a role (bot, mediator), or None."""
        if self.darwin:
            label = self._label("ea-chat-bot" if role == "bot" else "ea-mail-mediator")
            import launchd_labels
            return label if launchd_labels.plist_path(label).exists() else None
        if role == "bot":
            for unit in ("ea-chat-bot", "ea-slack-bot"):
                if self._unit_loaded(unit):
                    return unit
            return None
        return "ea-mail-mediator" if self._unit_loaded("ea-mail-mediator") else None

    def restart(self, name: str, role: str) -> tuple[bool, str]:
        if self.darwin:
            r = self._run("sudo", "-n", "/bin/launchctl", "kickstart", "-k", f"system/{name}")
        elif role == "mediator":
            # Restart=always brings it back; the owner may signal its own process.
            r = self._run("pkill", "-u", str(os.geteuid()), "-f",
                          r"^[^ ]*/python3? [^ ]*/preprocessor/mail_mediator\.py")
            if r.returncode == 1:
                return True, "no mediator process to restart"
        else:
            r = self._run("sudo", "-n", "systemctl", "restart", f"{name}.service")
        ok = r.returncode == 0
        return ok, "restarted" if ok else (r.stderr.strip() or f"exit {r.returncode}")[:200]

    def state(self, name: str) -> tuple[bool, str]:
        """(running, main pid)."""
        if self.darwin:
            r = self._run("launchctl", "print", f"system/{name}")
            m = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
            return (m is not None, m.group(1) if m else "")
        r = self._run("systemctl", "show", "-p", "ActiveState", "-p", "MainPID", name)
        vals = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        pid = vals.get("MainPID", "0")
        return vals.get("ActiveState") == "active" and pid not in ("", "0"), pid

    def _log_files(self, name: str, role: str) -> list[Path]:
        """The owner-private logs a service writes: the bot's own log for
        the bot, and on macOS the launchd log that holds each service's
        stderr (on Linux that is the journal, read below)."""
        logs = self.private / "logs"
        files = [logs / "slack-bot.log"] if role == "bot" else []
        if self.darwin:
            files.append(logs / f"{name}.launchd.log")
        return files

    def log_mark(self, name: str, role: str = "bot") -> dict:
        sizes = {}
        for f in self._log_files(name, role):
            try:
                sizes[str(f)] = f.stat().st_size
            except OSError:
                sizes[str(f)] = 0
        return {"files": sizes, "since": time.time()}

    def log_since(self, name: str, mark: dict) -> tuple[str, bool]:
        """(text logged since `mark`, whether any log could be read)."""
        chunks, readable = [], False
        for f, size in mark.get("files", {}).items():
            try:
                with open(f, "rb") as fh:
                    readable = True
                    end = fh.seek(0, 2)
                    fh.seek(size if end >= size else 0)
                    chunks.append(fh.read(4_000_000).decode("utf-8", "replace"))
            except OSError:
                continue
        if not self.darwin:
            r = self._run("journalctl", "-u", name, "--since", f"@{int(mark.get('since', 0))}",
                          "-o", "cat", "--no-pager", "-q", timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                readable = True
                chunks.append(r.stdout)
        return "\n".join(chunks), readable

    def log_before(self, name: str, role: str = "bot") -> str:
        """The tail of the service's log as it stands: what the instance
        that runs now logged."""
        files = self._log_files(name, role)
        if not files:
            return ""
        try:
            with open(files[0], "rb") as fh:
                end = fh.seek(0, 2)
                fh.seek(max(0, end - 2_000_000))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""


def expected_markers(before: str) -> list[tuple[str, re.Pattern]]:
    """What the new instance must log: the chat frontend connected, and the
    web chat serving when the instance before it served one."""
    starts = [m.end() for m in START_RE.finditer(before)]
    last = before[starts[-1]:] if starts else ""
    markers = [("the chat frontend connected", FRONTEND_RE)]
    if WEB_RE.search(last):
        markers.append(("the web chat serving", WEB_RE))
    return markers


def gate(host, name: str, mark: dict, markers: list[tuple[str, re.Pattern]], *,
         need_start: bool = True, timeout: float = GATE_S, stable: float = GATE_STABLE_S,
         sleep=time.sleep, clock=time.monotonic) -> tuple[bool, str]:
    """The health gate: within `timeout`, the service runs one main process
    for at least `stable` seconds, its log since the restart shows a new
    start (`need_start`) and every marker, and no traceback. With no
    readable log, a process that stays up twice `stable` passes."""
    t0 = clock()
    first_pid, up_since, missing, readable = "", None, [m[0] for m in markers], False
    while True:
        running, pid = host.state(name)
        text, readable = host.log_since(name, mark)
        bad = BAD_RE.search(text)
        if bad:
            line = text[text.rfind("\n", 0, bad.start()) + 1:].split("\n", 1)[0]
            return False, f"the log shows: {line.strip()[:200]}"
        if running:
            if not first_pid:
                first_pid, up_since = pid, clock()
            elif pid != first_pid:
                return False, f"the main process changed ({first_pid} to {pid}): it restarted"
        elif first_pid:
            return False, "the service stopped"
        missing = [n for n, rx in markers if not rx.search(text)]
        if need_start and not START_RE.search(text):
            missing.insert(0, "the start line")
        up = clock() - up_since if up_since is not None else 0
        if running and readable and not missing and up >= stable:
            return True, f"up {up:.0f}s" + "".join(f", {n}" for n, _ in markers)
        if running and not readable and up >= 2 * stable:
            return True, f"up {up:.0f}s (no readable log; process stability only)"
        if clock() - t0 >= timeout:
            break
        sleep(GATE_POLL_S)
    if not first_pid:
        return False, f"not running after {timeout:.0f}s"
    return False, f"after {timeout:.0f}s the log does not show " + ", ".join(missing)


def verify_now(host, role: str, name: str) -> tuple[bool, str]:
    """The gate's checks on the instance that runs now, from its own start
    line in the log, with no restart and no wait."""
    running, pid = host.state(name)
    if not running:
        return False, f"{name} is not running"
    text = host.log_before(name, role)
    starts = [m.start() for m in START_RE.finditer(text)]
    seg = text[starts[-1]:] if starts else (text if role != "bot" else "")
    bad = BAD_RE.search(seg)
    if bad:
        line = seg[seg.rfind("\n", 0, bad.start()) + 1:].split("\n", 1)[0]
        return False, f"{name}: the log since its start shows: {line.strip()[:200]}"
    if role == "bot":
        if not starts:
            return True, f"{name} runs (pid {pid}); no start line in its log to read"
        missing = [n for n, rx in expected_markers(text) if not rx.search(seg)]
        if missing:
            return False, f"{name}: the log since its start does not show " + ", ".join(missing)
    return True, f"{name} runs (pid {pid})"


def installed_services(host) -> dict:
    """{role: service name} for the services this host has."""
    return {r: n for r in ROLES if (n := host.service(r))}


def restart_services(host, names: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Restart each named service; ([(role, service)], problems)."""
    done, problems = [], []
    for role, name in names.items():
        ok, why = host.restart(name, role)
        if ok:
            done.append((role, name))
        else:
            problems.append(f"{role} ({name}): {why}")
    return done, problems


def check_health(host, restarted: list[tuple[str, str]], marks: dict, befores: dict,
                 **kw) -> tuple[bool, str]:
    """The gate for each restarted service: the bot with its markers, the
    mediator on its process and its log's tracebacks alone."""
    notes = []
    for role, name in restarted:
        if role == "bot":
            ok, why = gate(host, name, marks[name], expected_markers(befores.get(name, "")), **kw)
        else:
            ok, why = gate(host, name, marks[name], [], need_start=False, **kw)
        if not ok:
            return False, f"{name}: {why}"
        notes.append(f"{name} {why}")
    return True, "; ".join(notes) or "no service restarted"


# ── apply ────────────────────────────────────────────────────────────

class Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise UpdateError("another update.py run holds the lock")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _stamp_pending(paths: Paths, commit: str) -> None:
    try:
        write_private_json(paths.pending, {"commit": commit[:7], "since": time.time()})
    except OSError:
        pass


def _clear_pending(paths: Paths) -> None:
    try:
        paths.pending.unlink()
    except OSError:
        pass


def resolve_target(paths: Paths, st: dict, *, channel: str, tag: str | None) -> tuple[str, str] | None:
    """Fetch, then the release to install: `tag` when named, else the
    channel's pick. Records the check in `st`."""
    write_signers(paths.live / KEY_FILE, paths.signers)
    fetch_tags(paths.cache, paths.repo_url)
    st["checked_at"] = now_iso()
    st["latest_on_channel"] = newest_signed(paths.cache, channel, paths.signers) if channel in us.CHANNELS else ""
    if tag:
        return tag, verify_tag(paths.cache, tag, paths.signers)
    held = (st.get("held_back") or {}).get("version", "")
    got = pick(paths.cache, channel, paths.signers, installed=st.get("installed", ""), held=held)
    if got is None:
        return None
    for note in got[2]:
        say(note)
    return got[0], got[1]


def apply(paths: Paths, host=None, *, channel: str, tag: str | None = None, dry_run: bool = False,
          idle_timeout: float = IDLE_WAIT_S, gate_kw: dict | None = None, quiet: bool = False,
          idle_probe=None) -> int:
    """One update, docs/updates.md "One update, step by step". 0 when
    current, installed or deferred; 1 when held back or failed."""
    host = host or Host(paths.private)
    gate_kw = gate_kw or {}
    with Lock(paths.lock):
        st = base_status(paths)
        try:
            target = resolve_target(paths, st, channel=channel, tag=tag)
        except UpdateError as e:
            st["last_error"] = {"at": now_iso(), "error": str(e)}
            save_status(paths, st)
            raise
        st.pop("last_error", None)
        if target is None:
            save_status(paths, st)
            if not quiet:
                say(f"up to date: {st.get('installed') or st.get('installed_commit') or '?'} "
                    f"on {channel} (newest {st.get('latest_on_channel') or 'none'})")
            return 0
        new_tag, commit = target
        old_tag, old_commit = st.get("installed", ""), st.get("installed_commit", "")
        say(f"{new_tag} ({commit[:7]}) to install over {old_tag or old_commit or '?'}")
        if dry_run:
            scratch = Path(tempfile.mkdtemp(prefix="update-dry-"))
            try:
                staged = export_tree(paths.cache, commit, scratch / new_tag, new_tag)
                problems = smoke(staged)
                say("smoke: " + ("passed" if not problems else "; ".join(problems)))
                say("would restart: " + (", ".join(installed_services(host).values()) or "nothing"))
                busy = idle_probe() if idle_probe else busy_reasons(paths)
                say("idle now" if not busy else "busy now: " + ", ".join(busy))
                say(f"dry run: nothing switched (layout: {us.tree_kind(paths.live)})")
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            save_status(paths, st)
            return 0 if not problems else 1
        if us.tree_kind(paths.live) != "layout":
            raise UpdateError(f"{paths.live} is not the release layout; the root step makes it "
                              "(sudo bash deploy/harden-permissions.sh, docs/updates.md)")
        staging = paths.releases / f".staging-{new_tag}"
        shutil.rmtree(staging, ignore_errors=True)
        staged = export_tree(paths.cache, commit, staging, new_tag)
        problems = smoke(staged)
        if problems:
            shutil.rmtree(staged, ignore_errors=True)
            return _held(paths, st, new_tag, old_tag, "the smoke failed: " + "; ".join(problems)[:300])
        busy = wait_idle(paths, idle_timeout, probe=idle_probe)
        if busy:
            shutil.rmtree(staged, ignore_errors=True)
            st["last_apply"] = {"at": now_iso(), "from": old_tag, "to": new_tag, "result": "deferred",
                                "detail": ", ".join(busy)}
            save_status(paths, st)
            say("still busy after the wait (" + ", ".join(busy) + "); the next run tries again")
            return 0
        final = promote(staged, paths.releases, new_tag)
        old_current = link_target(paths.releases, "current")
        old_previous = link_target(paths.releases, "previous")
        try:
            update_lanes(paths, ref=f"refs/tags/{new_tag}", expect=commit, source=paths.cache)
        except UpdateError as e:
            shutil.rmtree(final, ignore_errors=True)
            st["last_apply"] = {"at": now_iso(), "from": old_tag, "to": new_tag, "result": "failed",
                                "detail": str(e)[:300]}
            save_status(paths, st)
            say(f"{new_tag} not installed: {e}")
            return 1
        names = installed_services(host)
        roles = list(names)
        befores = {names[r]: host.log_before(names[r], r) for r in roles}
        _stamp_pending(paths, commit)
        switch(paths.releases, final.name)
        if old_current:
            switch(paths.releases, old_current, "previous")
        say(f"switched {paths.live} to {final.name}")
        marks = {names[r]: host.log_mark(names[r], r) for r in roles}
        restarted, problems = restart_services(host, names)
        ok, detail = (False, "; ".join(problems)) if problems else check_health(
            host, restarted, marks, befores, **gate_kw)
        _clear_pending(paths)
        if problems:
            # The restart itself was refused (no sudoers grant, no unit): the
            # host's fault, not the release's. Switch back so the services and
            # the link agree, and try again next run; nothing is held back.
            say(f"could not restart ({detail}); switching back to {old_current}")
            if old_current:
                switch(paths.releases, old_current)
            if old_previous:
                switch(paths.releases, old_previous, "previous")
            if old_commit:
                try:
                    update_lanes(paths, ref=old_commit, expect=old_commit, source=None)
                except UpdateError as e:
                    say(f"the EA user's copy stays at {new_tag}: {e}")
            restart_services(host, dict(restarted))
            st["last_apply"] = {"at": now_iso(), "from": old_tag, "to": new_tag, "result": "failed",
                                "detail": f"could not restart: {detail}"[:400]}
            save_status(paths, st)
            return 1
        if ok:
            say(f"{new_tag} is healthy: {detail}")
            st.update(installed=new_tag, installed_commit=commit[:7], previous=old_tag or old_current or "")
            st["last_apply"] = {"at": now_iso(), "from": old_tag, "to": new_tag, "result": "ok",
                                "detail": detail}
            if (st.get("held_back") or {}).get("version") and \
                    us.version_key(st["held_back"]["version"]) and \
                    us.version_key(st["held_back"]["version"]) <= us.version_key(new_tag):
                st.pop("held_back", None)
            prune(paths.releases)
            save_status(paths, st)
            return 0
        say(f"{new_tag} failed the health gate ({detail}); switching back")
        if old_current:
            switch(paths.releases, old_current)
        if old_previous:
            switch(paths.releases, old_previous, "previous")
        if old_commit:
            try:
                update_lanes(paths, ref=old_commit, expect=old_commit, source=None)
            except UpdateError as e:
                say(f"the EA user's copy stays at {new_tag}: {e}")
        marks = {names[r]: host.log_mark(names[r], r) for r in roles}
        back, problems2 = restart_services(host, names)
        ok2, detail2 = check_health(host, back, marks, befores, **gate_kw) if not problems2 \
            else (False, "; ".join(problems2))
        say(f"{old_tag or old_current}: " + ("healthy again" if ok2 else f"STILL UNHEALTHY: {detail2}"))
        return _held(paths, st, new_tag, old_tag, f"failed the health gate: {detail}",
                     after=("healthy" if ok2 else f"unhealthy: {detail2}"))


def _held(paths: Paths, st: dict, tag: str, old: str, reason: str, after: str = "") -> int:
    st["held_back"] = {"version": tag, "at": now_iso(), "reason": reason[:300]}
    st["last_apply"] = {"at": now_iso(), "from": old, "to": tag, "result": "rolled-back" if after else "held",
                        "detail": (reason + (f"; {old or 'the old release'} {after}" if after else ""))[:400]}
    save_status(paths, st)
    say(f"{tag} held back: {reason}")
    return 1


def rollback(paths: Paths, host=None, gate_kw: dict | None = None) -> int:
    host = host or Host(paths.private)
    with Lock(paths.lock):
        cur, prev = link_target(paths.releases, "current"), link_target(paths.releases, "previous")
        if not prev or not (paths.releases / prev).is_dir():
            raise UpdateError("no previous release to switch back to")
        st = base_status(paths)
        prev_commit = (paths.releases / prev / ".deployed-commit").read_text().strip() \
            if (paths.releases / prev / ".deployed-commit").exists() else ""
        if prev_commit:
            update_lanes(paths, ref=prev_commit, expect=prev_commit, source=None)
        switch(paths.releases, prev)
        switch(paths.releases, cur, "previous")
        names = installed_services(host)
        befores = {n: host.log_before(n, r) for r, n in names.items()}
        marks = {n: host.log_mark(n, r) for r, n in names.items()}
        restarted, problems = restart_services(host, names)
        ok, detail = check_health(host, restarted, marks, befores, **(gate_kw or {})) if not problems \
            else (False, "; ".join(problems))
        st = base_status(paths)
        if us.version_key(cur or ""):
            st["held_back"] = {"version": cur, "at": now_iso(), "reason": "rolled back by hand"}
        st["last_apply"] = {"at": now_iso(), "from": cur, "to": prev, "result": "manual rollback",
                            "detail": detail}
        save_status(paths, st)
        say(f"switched back to {prev}: {detail}")
        return 0 if ok else 1


# ── the root step ────────────────────────────────────────────────────

def _chown_tree(top: Path, uid: int, gid: int) -> None:
    for dirpath, dirs, files in os.walk(top, followlinks=False):
        for n in dirs + files:
            os.chown(os.path.join(dirpath, n), uid, gid, follow_symlinks=False)
    os.chown(top, uid, gid, follow_symlinks=False)


def _label_for(tree: Path) -> str:
    inst = us.installed(tree)
    if inst["version"]:
        return inst["version"]
    if re.fullmatch(r"[0-9a-f]{4,40}", inst["commit"] or ""):
        return f"commit-{inst['commit'][:12]}"
    return "migrated-" + datetime.now().strftime("%Y%m%d%H%M%S")


def layout(live: Path, owner: str, seed: Path | None = None, releases: Path | None = None) -> str:
    """Root, once: /opt/ea-system becomes a root-owned link to
    <releases>/current, the owner's link to a release directory. A plain
    tree moves in as the first release; with no tree, `seed` (an export)
    does. Idempotent: on the layout it only sets the owner and modes."""
    import pwd
    releases = releases or us.releases_dir(live)
    pw = pwd.getpwnam(owner)
    gid = _ea_gid()
    gid = gid if gid >= 0 else pw.pw_gid
    am_root = os.geteuid() == 0
    if releases.is_symlink() or (releases.exists() and not releases.is_dir()):
        raise UpdateError(f"{releases} exists and is not a directory")
    releases.mkdir(mode=0o750, exist_ok=True)
    if am_root:
        os.chown(releases, pw.pw_uid, gid)
    os.chmod(releases, 0o2750)
    link = f"{releases.name}/current"
    if live.is_symlink():
        if os.readlink(live) != link:
            raise UpdateError(f"{live} links to {os.readlink(live)!r}, not {link!r}; fix it by hand")
        cur = link_target(releases, "current")
        if not cur or not (releases / cur).is_dir():
            raise UpdateError(f"{releases}/current names no release")
        return f"{live} is already the release layout (current: {cur})"
    if live.exists() and not live.is_dir():
        raise UpdateError(f"{live} is neither a directory nor the layout's link")
    src = live if live.is_dir() else seed
    if src is None or not Path(src).is_dir():
        raise UpdateError(f"no {live} and no --seed to make the first release from")
    label = _label_for(Path(src))
    dest = releases / label
    n = 1
    while dest.exists():
        dest = releases / f"{label}-{n}"
        n += 1
    shutil.move(str(src), str(dest))
    if am_root:
        _chown_tree(dest, pw.pw_uid, gid)
    os.chmod(dest, 0o750)
    switch(releases, dest.name)
    if am_root:
        os.chown(releases / "current", pw.pw_uid, gid, follow_symlinks=False)
    tmp = live.with_name(f".{live.name}.new")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(link, tmp)
    os.replace(tmp, live)
    return f"{live} -> {link} -> {dest.name}"


# ── the relay host ───────────────────────────────────────────────────

def relay_channel(path: Path = RELAY_CHANNEL_FILE) -> str:
    try:
        word = (path.read_text().split() or ["latest"])[0]
    except OSError:
        return "latest"
    return word if word in ("latest", "stable", "off") else "latest"


def relay_healthy(url: str = RELAY_HEALTH, tries: int = 15) -> bool:
    import urllib.request
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def relay(base: Path = RELAY_BASE, state: Path = RELAY_STATE, *, dry_run: bool = False,
          channel: str | None = None, repo_url: str = REPO_URL, healthy=relay_healthy,
          runner=subprocess.run) -> int:
    """The relay host's updater: the newest signed release on the channel,
    installed by that release's own relay/install.sh --tag."""
    src = base / "src"
    channel = channel or relay_channel()
    if channel == "off":
        say("relay updates are off (/etc/tacitus-relay/update-channel)")
        return 0
    if "--tag)" not in _read(src / "relay" / "install.sh"):
        say("relay/install.sh takes no --tag yet; nothing installed")
        return 0
    cache, signers = state / "tacitus.git", state / "allowed_signers"
    status_path = state / "status.json"
    try:
        st = json.loads(status_path.read_text())
    except (OSError, ValueError):
        st = {}
    write_signers(src / KEY_FILE, signers)
    fetch_tags(cache, repo_url)
    head = git_out(src, "rev-parse", "HEAD")
    at = [t for t in git_out(cache, "tag", "--points-at", head, "--list", "v*").splitlines()
          if us.version_key(t)]
    installed = max(at, key=us.version_key) if at else ""
    held = (st.get("held_back") or {}).get("version", "")
    got = pick(cache, channel, signers, installed=installed, held=held)
    st.update(channel=channel, installed=installed, installed_commit=head[:7], checked_at=now_iso())
    if got is None or got[1] == head:
        say(f"relay up to date at {installed or head[:7]} on {channel}")
        _write_json(status_path, st)
        return 0
    tag, commit, _ = got
    say(f"relay: {tag} to install over {installed or head[:7]}")
    if dry_run:
        return 0
    scratch = Path(tempfile.mkdtemp(prefix="relay-update-"))
    try:
        tree = export_tree(cache, commit, scratch / tag, tag)
        r = runner(["bash", str(tree / "relay" / "install.sh"), "--tag", tag], cwd=str(tree))
        now_head = git(src, "rev-parse", "HEAD", check=False).stdout.strip()
        if r.returncode == 0 and now_head == commit and healthy():
            st.update(installed=tag, installed_commit=commit[:7])
            st["last_apply"] = {"at": now_iso(), "from": installed, "to": tag, "result": "ok"}
            _write_json(status_path, st)
            say(f"relay at {tag}")
            return 0
        why = (f"install.sh exited {r.returncode}" if r.returncode else
               "the clone is not at the tag" if now_head != commit else "/healthz did not answer")
        say(f"relay: {tag} failed ({why}); switching back")
        if installed:
            old = export_tree(cache, git_out(cache, "rev-parse", f"refs/tags/{installed}^{{commit}}"),
                              scratch / installed, installed)
            runner(["bash", str(old / "relay" / "install.sh"), "--tag", installed], cwd=str(old))
        st["held_back"] = {"version": tag, "at": now_iso(), "reason": why}
        st["last_apply"] = {"at": now_iso(), "from": installed, "to": tag, "result": "rolled-back",
                            "detail": why}
        _write_json(status_path, st)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(doc, written_at=now_iso()), indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


# ── commands ─────────────────────────────────────────────────────────

def _print_status(st: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(st, indent=1, sort_keys=True))
        return
    print(f"mode:       {st.get('mode')}")
    print(f"channel:    {st.get('channel')} ({st.get('channel_source')})")
    print(f"installed:  {st.get('installed') or '-'} ({st.get('installed_commit') or '?'})")
    print(f"newest:     {st.get('latest_on_channel') or '-'}"
          + (f" (checked {st['checked_at']})" if st.get("checked_at") else ""))
    last = st.get("last_apply") or {}
    if last:
        print(f"last apply: {last.get('at')} {last.get('from') or '?'} -> {last.get('to')}: "
              f"{last.get('result')}" + (f" ({last['detail']})" if last.get("detail") else ""))
    held = st.get("held_back") or {}
    if held:
        print(f"held back:  {held.get('version')} ({held.get('reason')})")
    if st.get("last_error"):
        print(f"last error: {st['last_error'].get('at')} {st['last_error'].get('error')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="install signed Tacitus releases (docs/updates.md)")
    ap.add_argument("--live", help="the code tree the services run (default /opt/ea-system)")
    ap.add_argument("--releases", help="the release directory (default <live>.releases)")
    ap.add_argument("--private", help="the owner-private directory (default EA_PRIVATE_DIR)")
    ap.add_argument("--ea-dir", help="the EA tree (default EA_DIR, else ~/.ea)")
    ap.add_argument("--lanes", help="the EA user's copy (default <ea-bot home>/ea-system)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--tag")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wait", type=float, default=IDLE_WAIT_S, help="seconds to wait for idle")
    p = sub.add_parser("run")
    p.add_argument("--jitter", type=float, default=0, help="wait up to this many seconds first")
    sub.add_parser("restart")
    sub.add_parser("verify")
    sub.add_parser("rollback")
    sub.add_parser("channel")
    p = sub.add_parser("layout")
    p.add_argument("--owner", required=True)
    p.add_argument("--seed")
    p = sub.add_parser("next-version")
    p.add_argument("--repo", default=".")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--main", default="HEAD")
    p = sub.add_parser("allowed-signers")
    p.add_argument("--key", default=str(TREE / KEY_FILE))
    p = sub.add_parser("verify-tag")
    p.add_argument("--repo", default=".")
    p.add_argument("--tag", required=True)
    p.add_argument("--key", default=str(TREE / KEY_FILE))
    p = sub.add_parser("relay")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--channel", choices=("latest", "stable", "off"))
    a = ap.parse_args(argv)

    try:
        if a.cmd == "next-version":
            out = next_version(Path(a.repo), a.head, a.main)
            if out:
                print(out)
            return 0
        if a.cmd == "allowed-signers":
            sys.stdout.write(allowed_signers(Path(a.key).read_text()))
            return 0
        if a.cmd == "verify-tag":
            with tempfile.TemporaryDirectory() as td:
                signers = write_signers(Path(a.key), Path(td) / "allowed_signers")
                print(verify_tag(Path(a.repo), a.tag, signers))
            return 0
        if a.cmd == "relay":
            return relay(dry_run=a.dry_run, channel=a.channel)
        paths = Paths(a.live, a.releases, a.private, a.ea_dir,
                      lanes=(a.lanes if a.lanes else None))
        if a.cmd == "layout":
            say(layout(paths.live, a.owner, Path(a.seed) if a.seed else None, paths.releases))
            return 0
        channel, source = us.effective_channel(paths.live, paths.private, paths.ea)
        kind = us.tree_kind(paths.live)
        if a.cmd == "channel":
            print(channel)
            return 0
        if a.cmd == "status":
            _print_status(base_status(paths), a.json)
            return 0
        if a.cmd == "check":
            st = base_status(paths)
            if channel == "off":
                _print_status(st, a.json)
                return 0
            with Lock(paths.lock):
                try:
                    target = resolve_target(paths, st, channel=channel, tag=None)
                except UpdateError as e:
                    st["last_error"] = {"at": now_iso(), "error": str(e)}
                    save_status(paths, st)
                    raise
                st.pop("last_error", None)
                save_status(paths, st)
            st["update_due"] = target[0] if target else ""
            if a.json:
                _print_status(st, True)
            else:
                say(f"{target[0]} is due on {channel}" if target else
                    f"up to date on {channel} (newest {st.get('latest_on_channel') or 'none'})")
            return 0
        if a.cmd == "run":
            if kind == "container":
                say("the code is in the image; docs/docker.md 'Upgrade' updates it")
                return 0
            if channel == "off":
                save_status(paths, base_status(paths))
                return 0
            if kind != "layout":
                st = base_status(paths)
                st["last_error"] = {"at": now_iso(), "error": "channel on, but no release layout "
                                    "(sudo bash deploy/harden-permissions.sh)"}
                save_status(paths, st)
                say(st["last_error"]["error"])
                return 0
            if a.jitter > 0:
                time.sleep(random.uniform(0, a.jitter))
            return apply(paths, channel=channel, quiet=True)
        if a.cmd == "apply":
            if kind == "container" and not a.dry_run:
                raise UpdateError("the code is in the image: git fetch --tags && git checkout "
                                  "<tag> && docker compose build && docker compose up -d")
            if channel == "off" and not a.tag:
                say(f"updates are off ({source}); name a release with --tag to install one")
                return 0
            return apply(paths, channel=channel if channel != "off" else "latest", tag=a.tag,
                         dry_run=a.dry_run, idle_timeout=a.wait)
        if a.cmd == "rollback":
            return rollback(paths)
        host = Host(paths.private)
        names = installed_services(host)
        roles = list(names)
        if a.cmd == "restart":
            busy = wait_idle(paths)
            if busy:
                raise UpdateError("still busy: " + ", ".join(busy))
            befores = {names[r]: host.log_before(names[r], r) for r in roles}
            marks = {names[r]: host.log_mark(names[r], r) for r in roles}
            restarted, problems = restart_services(host, names)
            if problems:
                raise UpdateError("; ".join(problems))
            ok, detail = check_health(host, restarted, marks, befores)
        else:   # verify: the gate's checks on what runs now
            results = [verify_now(host, r, names[r]) for r in roles]
            ok = all(r[0] for r in results)
            detail = "; ".join(r[1] for r in results) or "no service installed"
        say(("healthy: " if ok else "UNHEALTHY: ") + detail)
        return 0 if ok else 1
    except (UpdateError, OSError, subprocess.SubprocessError) as e:
        say(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
