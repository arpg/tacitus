#!/usr/bin/env python3
"""boundary-audit: does the owner and resident boundary still hold? Read-only.

docs/SECURITY.md sets the posture in "Owner trust rules" (what an owner
process may take from ea-bot space) and "Threat Model: What Prompt Injection
Cannot Do". The doctor checks the first at install time. This runs the same
owner-trust checks every day on the live install, plus the threat-model
claims that metadata can show, so drift shows the day it happens:

  doctor     the doctor's owner-trust items, services off the ea-bot tree,
             and the private dir
  tokens     no token file and not the signing key is readable by the resident
  auth       the auth directory is the owner's, and no entry under it is the
             resident's or writable by it
  private    the resident can neither search nor replace the owner-private dir
  code       nothing in the services' tree, and no file in the lanes' tree, is
             the resident's to change; the services' tree is the committed
             HEAD under a root-owned parent
  services   each unit's interpreter, script and working directory are out of
             the resident's reach
  deny list  every lane tree carries the committed deny list
  claude settings  ea-bot's ~/.claude/settings.json sets no bypassPermissions
             default, no hooks and no allow rules (a warning)
  mediation  mail mediation is on and its service runs, and requests the
             resident submits with a path argument that names an owner-only
             decoy are refused in each spelling argparse takes
  sudo       the policy lets owner processes reach resident space as the
             resident (list mode, nothing runs)
  proc       /proc hides the owner's processes from the resident (hidepid)
             while the owner keeps the resident's in view by gid; "staged,
             not active" until the boot that applies the fstab line
  cron       no owner job writes into, runs from or searches its PATH in
             resident space, and the owner log dir is private
  env        ~/.ea/.env names no secret
  resident cron   ea-bot has no crontab, and cron (and at, when installed)
             would refuse one from it
             (deploy/harden-permissions.sh writes the deny lines)
  resident units  ea-bot's systemd user units (its unit files and what
             runs in its user manager) are the distribution's baseline or
             the EA's deck scopes, and it lingers only for the deck builder
  netpolicy  the resident's network policy (deploy/enable-resident-netpolicy.sh)
             is loaded at boot and matches this host, and probes made as the
             resident show that this host and local networks refuse it while
             its allowlist (the resolver, its deck servers) still answers

The probes read only: lstat and stat, systemctl show, sudo -n -l, crontab -l,
git in the owner's repo, /etc/fstab and /proc/self/mountinfo, /etc/cron.*
and /etc/at.*, the linger directory and the resident's cgroup tree, one
`crontab -l` run as the resident (in / with only PATH set), reads of
resident space made with the resident's access (resident_io), of this
process's own /proc entry, /proc/net, and one run of the network probe as the
resident (resident_netpolicy.PROBE: a TCP connect per target closed without
sending a byte, one byte to a UDP port nobody holds, one resolver query for
localhost), and one run of the mediator probe as the resident. A refused
target is probed on a port nobody listens on, so no probe reaches a service
even when the policy is missing.

The mediator probe (check_mediator_argv) puts four graph_actions
create-draft requests in the mediator's queue as the resident, each naming
the decoy for --body-file in another spelling, and reads and removes each
answer. The decoy is an empty owner-only file in this script's own state
directory. create-draft reads its body file before any mail API call and
stops on an empty body, so even a request the mediator wrongly accepts ends
in the owner-side tool with no draft and no call made. A request still in
the queue at the deadline is withdrawn. Each request adds one line to the
mediator's own audit log. Nothing else on the install changes but this
script's own state file and the decoy, both in the owner's home.

Every run prints a table. A chat post goes to the EA channel only when the set
of checks that do not pass (with a digest of each one's detail) differs from
the last run's, including one post when all pass again. The post names the
checks and their status only: the resident reads the EA channel, and the
details stay in the table, which the cron writes to the owner-private logs.

Exit 1 when a check FAILs; a WARN is advisory.

    python3 scripts/boundary-audit.py             # check, print, post on change
    python3 scripts/boundary-audit.py --dry-run   # check and print; no post, no state write
    python3 scripts/boundary-audit.py --json
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import importlib.util
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))


def _owner_repo() -> Path:
    """The owner's checkout, where the committed tree is read with git: this
    script's own tree when it is a checkout, else ea-system in the owner's
    passwd home (the cron runs this from /opt/ea-system, which has no .git)."""
    if (REPO / ".git").is_dir():
        return REPO
    import pwd
    home_repo = Path(pwd.getpwuid(os.geteuid()).pw_dir) / "ea-system"
    return home_repo if (home_repo / ".git").is_dir() else REPO

RESIDENT_USER = "ea-bot"
LIVE_TREE = Path("/opt/ea-system")
UNITS = ("ea-chat-bot", "ea-mail-mediator")
MEDIATOR_UNIT = "ea-mail-mediator"
# Out of ~/.ea on purpose: whoever can write this file can silence the audit.
STATE_FILE = Path.home() / ".local" / "state" / "ea-boundary-audit" / "state.json"
DENY_LISTS = ("scripts/disallowed-tools.txt", "scripts/disallowed-tools.review-only.txt")
# Every file in the auth directory with "token" in its name: the MSAL
# cache, the Google tokens and whatever an owner plugin's harvest writes.
TOKEN_GLOB = "*token*"
HMAC_KEY = "scheduled-send-hmac.key"
# Secrets the resident holds by design: docs/ea/reimbursement.md names
# ~/.ea/.env as their place. The bot does not pass them into a lane's
# environment; no granted verb reads them.
RESIDENT_SECRETS = frozenset({"TWOCAPTCHA_API_KEY"})
INTERPRETERS = ("python", "python3", "bash", "sh", "env", "nice", "ionice", "flock", "timeout")
SHOWN = 3            # offending paths named per check in the table
FSTAB = Path("/etc/fstab")
MOUNTINFO = Path("/proc/self/mountinfo")
# The kernel prints hidepid by name; fstab lines and older kernels use the number.
HIDEPID = {"0": "off", "1": "noaccess", "2": "invisible", "4": "ptraceable"}

R, W, X = 4, 2, 1
ACL_PATHS: set[str] = set()   # inodes whose ACL the mode bits do not describe


def res(status: str, name: str, detail: str = "", fix: str = "") -> dict:
    return {"status": status, "name": name, "detail": detail, "fix": fix}


# ── access as the resident ────────────────────────────────────────────

@dataclass(frozen=True)
class Who:
    """A user by uid and groups; the resident's access is judged from mode
    bits as the kernel would, without running anything as it."""
    uid: int
    gids: frozenset

    @classmethod
    def resident(cls) -> "Who | None":
        try:
            pw = pwd.getpwnam(RESIDENT_USER)
        except KeyError:
            return None
        return cls(pw.pw_uid, frozenset(os.getgrouplist(RESIDENT_USER, pw.pw_gid)))

    def can(self, path: str, st: os.stat_result, bit: int) -> bool:
        """Whether this user has `bit` on the inode. Its owner can change the
        mode, so ownership grants every bit. An ACL counts as a grant (the
        mode bits do not describe it) and is noted in ACL_PATHS."""
        if self.uid == 0 or st.st_uid == self.uid:
            return True
        # Linux keeps a POSIX ACL in an extended attribute; macOS has no
        # listxattr in Python's os module, and its ACLs are not read here.
        listxattr = getattr(os, "listxattr", None)
        try:
            acl = bool(listxattr) and "system.posix_acl_access" in listxattr(
                path, follow_symlinks=False)
        except OSError:
            acl = False
        if acl:
            ACL_PATHS.add(path)
            return True
        if st.st_gid in self.gids:
            return bool(st.st_mode & (bit << 3))
        return bool(st.st_mode & bit)


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _prefixes(real: str) -> list[str]:
    parts = [p for p in real.split("/") if p]
    return ["/"] + ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def reaches(who: Who, real_dir: str) -> bool:
    """The user can search every directory from / to `real_dir` (resolved)."""
    for d in _prefixes(real_dir):
        st = _lstat(d)
        if st is None or not stat.S_ISDIR(st.st_mode) or not who.can(d, st, X):
            return False
    return True


def reads(who: Who, path) -> bool:
    """The user can read the object at `path`, following links."""
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return False
    return reaches(who, os.path.dirname(real)) and who.can(real, st, R)


def can_replace(who: Who, path: str) -> bool:
    """The user can rename, unlink or create the entry at `path`: it reaches
    the resolved parent and may write and search it; a sticky parent limits
    that to entries (or a parent) it owns, or a name not yet taken."""
    parent = os.path.realpath(os.path.dirname(path) or "/")
    st = _lstat(parent)
    if st is None or not reaches(who, parent):
        return False
    if not (who.can(parent, st, W) and who.can(parent, st, X)):
        return False
    if st.st_mode & stat.S_ISVTX and st.st_uid != who.uid:
        entry = _lstat(os.path.join(parent, os.path.basename(path)))
        return entry is None or entry.st_uid == who.uid
    return True


def weak_point(who: Who, path, _depth: int = 0) -> str | None:
    """The first place on the way to `path` the user can change, described:
    a component it can replace (each link's target walked too), or the
    object itself when it may write it. None when there is none."""
    path = os.path.abspath(str(path))
    cur = "/"
    for name in [p for p in path.split("/") if p]:
        nxt = os.path.join(cur, name)
        if can_replace(who, nxt):
            return f"{nxt} (it can replace the entry)"
        st = _lstat(nxt)
        if st is None:
            return None
        if stat.S_ISLNK(st.st_mode) and _depth < 8:
            weak = weak_point(who, os.path.realpath(nxt), _depth + 1)
            if weak:
                return weak
        cur = nxt
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return None
    if reaches(who, os.path.dirname(real)) and who.can(real, st, W):
        return f"{real} (it can write it)"
    return None


def changeable_entries(who: Who, root: Path) -> list[str]:
    """Entries under `root` (links not followed) the user can change: its
    own, a directory it may write and search, or a file it may write."""
    if not reaches(who, os.path.realpath(root)):
        return []
    out = []
    for dirpath, dirs, files in os.walk(root):
        st = _lstat(dirpath)
        if st is not None and (st.st_uid == who.uid or
                               (who.can(dirpath, st, W) and who.can(dirpath, st, X))):
            out.append(dirpath)
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = _lstat(p)
            if st is None:
                continue
            if st.st_uid == who.uid or (stat.S_ISREG(st.st_mode) and who.can(p, st, W)):
                out.append(p)
    return sorted(set(out))


def _named(paths: list[str], root=None) -> str:
    shown = []
    for p in paths[:SHOWN]:
        label = os.path.relpath(p, root) if root else p
        shown.append(label + (" (ACL)" if p in ACL_PATHS else ""))
    more = f" and {len(paths) - SHOWN} more" if len(paths) > SHOWN else ""
    return ", ".join(shown) + more


# ── the install ───────────────────────────────────────────────────────

@dataclass
class Ctx:
    ea_dir: Path
    private: Path
    owner_home: Path
    owner_logs: Path
    live_tree: Path
    lanes_tree: Path | None
    eabot_home: str | None
    resident: Who | None
    repo: Path
    fstab: Path = FSTAB
    mountinfo: Path = MOUNTINFO

    @classmethod
    def live(cls, ea_dir: Path) -> "Ctx":
        import owner_trust
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        try:
            eabot_home = pwd.getpwnam(RESIDENT_USER).pw_dir
        except KeyError:
            eabot_home = None
        return cls(ea_dir=ea_dir, private=owner_trust.private_dir(), owner_home=home,
                   # log_paths.owner_log_dir(), named without creating it
                   owner_logs=Path.home() / ".ea-private" / "logs",
                   live_tree=LIVE_TREE,
                   lanes_tree=Path(eabot_home) / "ea-system" if eabot_home else None,
                   eabot_home=eabot_home, resident=Who.resident(), repo=_owner_repo())

    def in_resident_space(self, path: str) -> bool:
        """`path`, as written or resolved, lies under the resident's home or
        the EA tree."""
        roots = [str(self.ea_dir)] + ([self.eabot_home] if self.eabot_home else [])
        real = os.path.realpath(path)
        for root in roots:
            for r, p in ((root, os.path.abspath(path)), (os.path.realpath(root), real)):
                r = r.rstrip("/")
                if p == r or p.startswith(r + "/"):
                    return True
        return False


def _load_doctor():
    spec = importlib.util.spec_from_file_location("ea_doctor", REPO / "scripts" / "ea-doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _systemctl(*args: str) -> str:
    if not shutil.which("systemctl"):
        return ""
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                              timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _unit_prop(unit: str, prop: str) -> str:
    return _systemctl("show", "-p", prop, "--value", unit)


def _mediation_on(ctx: Ctx) -> bool:
    """mail_mediation.mediation_active_for_resident() for this EA tree
    (unknown counts as on, as the gate does)."""
    try:
        import mail_mediation as mm
        mm.EA_DIR = ctx.ea_dir
        mm.CONFIG = ctx.ea_dir / "config" / "ea-config.json"
        return bool(mm.mediation_active_for_resident())
    except Exception:
        return True


def _git(ctx: Ctx, *args: str) -> subprocess.CompletedProcess | None:
    """git in the owner's repo, with no GIT_* variable inherited."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "-C", str(ctx.repo), *args], capture_output=True,
                              timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _read_shared(path: Path) -> bytes | None:
    """A file in resident space, read with the resident's access."""
    import resident_io
    return resident_io.read_bytes(path)


def _read_owner_tree(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            return f.read()
    except OSError:
        return None


# ── the checks ────────────────────────────────────────────────────────

def check_doctor(ctx: Ctx) -> list[dict]:
    """The doctor's owner-trust section, services off the ea-bot tree, and
    the private dir, as the doctor reports them."""
    doctor = _load_doctor()
    doctor.RESULTS.clear()
    doctor.configure(ctx.ea_dir)
    doctor.check_owner_trust(ctx.ea_dir)
    doctor.check_hardening()
    if doctor.NEEDS_SLACK:
        doctor.check_private(ctx.ea_dir)
    return [dict(r) for r in doctor.RESULTS]


def check_tokens(ctx: Ctx) -> list[dict]:
    name, key_name = "tokens: out of the resident's reach", "tokens: signing key out of the resident's reach"
    who = ctx.resident
    if who is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to judge")]
    auth = ctx.ea_dir / "auth"
    tokens = sorted(auth.glob(TOKEN_GLOB)) if auth.is_dir() else []
    tokens = [t for t in tokens if os.path.lexists(t)]
    readable = [str(t) for t in tokens if reads(who, t)]
    fix = ("sudo bash deploy/enable-mail-mediation.sh (sets the owner's tokens to mode 600 and "
           "quarantines any the owner does not own; it chowns none)")
    out = []
    if readable and _mediation_on(ctx):
        out.append(res("FAIL", name, f"{len(readable)} of {len(tokens)} token file(s) readable by "
                       f"the resident under mail mediation: {_named(readable, auth)}", fix))
    elif readable:
        out.append(res("WARN", name, f"mail mediation is off, so the resident reads "
                       f"{len(readable)} token file(s) by design", fix))
    else:
        out.append(res("PASS", name, f"{len(tokens)} token file(s), none readable by the resident"))
    key = auth / HMAC_KEY
    if os.path.lexists(key):
        if reads(who, key):
            out.append(res("FAIL", key_name, "the resident can read the key, and so sign a "
                           "deferred send", f"chmod 600 {key} as the owner"))
        else:
            out.append(res("PASS", key_name, "not readable by the resident"))
    return out


# An auth directory the owner does not own may hold the resident's own key
# or tokens: the way back makes new ones, and never chowns the old files to
# the owner (deploy/tree-custody.sh refuses to).
AUTH_RECOVERY = ("as root: mv {auth} {auth}.untrusted-<date> and install -d -o <owner> -g ea "
                 "-m 0750 {auth}; then as the owner put token files back only from your own "
                 "backup or sign in again (graph_client.py; google_calendar_client.py --setup), "
                 "and make a new scheduled-send key (docs/SETUP.md 'The deferred-send signing key'); "
                 "never chown the old files to the owner")
# What deploy/tree-custody.sh moves out of auth/ (an entry the owner did not
# own, a file with other names) goes to an owner-only directory by this name.
QUARANTINE_PREFIX = ".quarantine-"


def check_auth_custody(ctx: Ctx, owner_uid: int | None = None) -> list[dict]:
    """The auth directory is the owner's, and nothing under it (links not
    followed) is the resident's or writable by it: an entry it owns, a file
    it may write, or a directory it may write and search. The owner's own
    quarantine directories (mode 0700) are left out of that walk and named
    in a WARN until the owner reads and deletes them."""
    name = "auth: the owner's, nothing in it the resident's"
    who = ctx.resident
    if who is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to judge")]
    owner = os.geteuid() if owner_uid is None else owner_uid
    auth = ctx.ea_dir / "auth"
    st = _lstat(str(auth))
    recover = AUTH_RECOVERY.format(auth=auth)
    if st is None:
        return [res("PASS", name, "no auth directory yet")]
    if not stat.S_ISDIR(st.st_mode):
        return [res("FAIL", name, "auth is not a directory (a link or a file)", recover)]
    if st.st_uid != owner:
        return [res("FAIL", name, f"the auth directory is uid {st.st_uid}'s, not the owner's "
                    f"(uid {owner}); nothing in it is trusted", recover)]
    bad, held = [], []
    for dirpath, dirs, files in os.walk(auth):
        if dirpath == str(auth):
            for d in list(dirs):
                qst = _lstat(os.path.join(dirpath, d))
                if (d.startswith(QUARANTINE_PREFIX) and qst is not None
                        and stat.S_ISDIR(qst.st_mode) and qst.st_uid == owner
                        and qst.st_mode & 0o077 == 0):
                    dirs.remove(d)
                    held.append(os.path.join(dirpath, d))
        dst = _lstat(dirpath)
        if dst is not None and (dst.st_uid == who.uid or
                                (who.can(dirpath, dst, W) and who.can(dirpath, dst, X))):
            bad.append(dirpath)
        for n in dirs + files:
            p = os.path.join(dirpath, n)
            est = _lstat(p)
            if est is None:
                continue
            if est.st_uid == who.uid or (stat.S_ISREG(est.st_mode) and who.can(p, est, W)):
                bad.append(p)
    out = []
    if bad:
        bad = sorted(set(bad))
        owned = [b for b in bad if (s := _lstat(b)) is not None and s.st_uid != owner]
        fix = ("sudo bash deploy/enable-mail-mediation.sh tightens the owner's entries and "
               "quarantines every other one, chowning none; then make a new scheduled-send key "
               "and sign in again for any token it quarantined" if owned else
               f"as the owner: chmod go-w the entries named (under {auth})")
        out.append(res("FAIL", name, f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} under "
                       f"auth/ the resident owns or can write: {_named(bad, auth)}", fix))
    else:
        out.append(res("PASS", name, "the owner's directory; no entry the resident owns or can write"))
    if held:
        out.append(res("WARN", "auth: quarantined entries",
                       f"{len(held)} quarantine director{'y' if len(held) == 1 else 'ies'} in auth/: "
                       f"{_named(sorted(held), auth)}",
                       "read NOTE.txt in each; make a new scheduled-send key and sign in again "
                       "for any token there; then delete the directory (never move a file back)"))
    return out


def check_private_reach(ctx: Ctx) -> list[dict]:
    name = "private: out of the resident's reach"
    who = ctx.resident
    if who is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to judge")]
    priv = ctx.private
    if not priv.is_dir():
        return [res("WARN", name, f"{priv} does not exist yet", "start the bot once, or mkdir -m 700 it")]
    problems = []
    real = os.path.realpath(priv)
    if reaches(who, real):
        problems.append("the resident can search the private dir")
    for f in ("secrets.env", "chat.json"):
        if (priv / f).exists() and reads(who, priv / f):
            problems.append(f"the resident can read {f}")
    weak = weak_point(who, priv)
    if weak:
        problems.append(f"the resident can change {weak}")
    fix = f"chmod 700 {priv}; chmod o-rwx {ctx.owner_home}"
    if problems:
        return [res("FAIL", name, "; ".join(problems), fix)]
    if reaches(who, os.path.realpath(ctx.owner_home)):
        return [res("WARN", name, "the owner's home is searchable by the resident; the private "
                    "dir is not", f"chmod o-x {ctx.owner_home}")]
    return [res("PASS", name, "the resident can search neither the owner's home nor the private dir")]


def check_code_trees(ctx: Ctx) -> list[dict]:
    out = []
    who = ctx.resident
    live_name, lanes_name = "code: the services' tree", "code: the lanes' tree"
    live = ctx.live_tree
    if not live.is_dir():
        out.append(res("WARN", live_name, f"{live} is not installed; the services run from "
                       "elsewhere", "sudo bash deploy/harden-permissions.sh"))
    else:
        problems = []
        real = os.path.realpath(live)
        for d in _prefixes(os.path.dirname(real)):
            st = _lstat(d)
            if st is None or st.st_uid != 0 or st.st_mode & 0o022:
                problems.append(f"{d} above it is not root's alone")
        st = _lstat(real)
        if st is not None and (st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022):
            problems.append("its top directory is not the owner's alone")
        if who is not None:
            changeable = changeable_entries(who, live)
            if changeable:
                problems.append(f"{len(changeable)} entr(ies) the resident can change: "
                                f"{_named(changeable, live)}")
            outward = []
            for dirpath, dirs, files in os.walk(live):
                for n in dirs + files:
                    p = os.path.join(dirpath, n)
                    if os.path.islink(p) and ctx.in_resident_space(os.path.realpath(p)):
                        outward.append(p)
            if outward:
                problems.append(f"{len(outward)} link(s) into resident space: {_named(outward, live)}")
        if problems:
            out.append(res("FAIL", live_name, "; ".join(problems), "sudo bash deploy/harden-permissions.sh"))
        else:
            out.append(res("PASS", live_name, f"{live}: under root-owned directories, nothing the "
                           "resident can change"))
        deployed = (_read_owner_tree(live / ".deployed-commit") or b"").decode(errors="replace").strip()
        head = _git(ctx, "rev-parse", "--short", "HEAD")
        head = head.stdout.decode().strip() if head is not None and head.returncode == 0 else ""
        if deployed and head and not (deployed.startswith(head) or head.startswith(deployed)):
            out.append(res("WARN", "code: the services' tree is the committed HEAD",
                           f"{live} holds {deployed}, the owner's repo is at {head}",
                           "commit on main in the main checkout (the post-commit hook deploys)"))
        elif deployed and head:
            out.append(res("PASS", "code: the services' tree is the committed HEAD", deployed))
    lanes = ctx.lanes_tree
    if lanes is not None and lanes.is_dir() and who is not None:
        changeable = changeable_entries(who, lanes)
        if changeable:
            out.append(res("FAIL", lanes_name, f"{len(changeable)} entr(ies) the resident can change: "
                           f"{_named(changeable, lanes)}",
                           f"chown -R the owner {lanes}; chmod -R g-w,o-w {lanes}"))
        else:
            out.append(res("PASS", lanes_name, "no entry the resident can change (the tree sits "
                           "in the resident's home, by design)"))
    return out


def _exec_paths(unit: str) -> list[str]:
    """The unit's interpreter, script and working directory."""
    m = re.search(r"argv\[\]=([^;]*)", _unit_prop(unit, "ExecStart"))
    argv = m.group(1).split() if m else []
    paths = [a for a in argv[:2] if a.startswith("/")]
    wd = _unit_prop(unit, "WorkingDirectory")
    if wd.startswith("/"):
        paths.append(wd)
    return paths


def check_units(ctx: Ctx) -> list[dict]:
    out = []
    who = ctx.resident
    for unit in UNITS:
        if _unit_prop(unit, "LoadState") in ("", "not-found"):
            continue
        name = f"services: {unit} code out of the resident's reach"
        paths = _exec_paths(unit)
        if not paths:
            out.append(res("WARN", name, "no ExecStart path to judge"))
            continue
        problems = []
        for p in paths:
            if ctx.in_resident_space(p):
                problems.append(f"{p} is in resident space")
            elif who is not None and (weak := weak_point(who, p)):
                problems.append(f"on the way to {p}, the resident can change {weak}")
        if problems:
            out.append(res("FAIL", name, "; ".join(problems), "sudo bash deploy/harden-permissions.sh"))
        else:
            out.append(res("PASS", name, ", ".join(paths)))
    return out


def check_deny_lists(ctx: Ctx) -> list[dict]:
    name = "deny list: every lane tree has the committed one"
    committed = {}
    for rel in DENY_LISTS:
        r = _git(ctx, "show", f"HEAD:{rel}")
        if r is not None and r.returncode == 0:
            committed[rel] = r.stdout
    if DENY_LISTS[0] not in committed:
        return [res("WARN", name, f"could not read {DENY_LISTS[0]} at HEAD in {ctx.repo}")]
    problems, seen = [], 0
    for tree, label, shared in ((ctx.live_tree, "services' tree", False),
                                (ctx.lanes_tree, "lanes' tree", True)):
        if tree is None or not tree.is_dir():
            continue
        for rel, want in committed.items():
            seen += 1
            data = _read_shared(tree / rel) if shared else _read_owner_tree(tree / rel)
            if data is None:
                problems.append(f"{label}: {rel} is missing or unreadable")
            elif hashlib.sha256(data).digest() != hashlib.sha256(want).digest():
                problems.append(f"{label}: {rel} differs from the committed copy")
    if problems:
        return [res("FAIL", name, "; ".join(problems),
                    "commit on main in the main checkout (the post-commit hook deploys both trees)")]
    return [res("PASS", name, f"{seen} file(s) match HEAD")]


def claude_settings_findings(doc) -> list[str]:
    """What in ea-bot's Claude user settings widens the lanes. Every headless
    lane runs in dontAsk mode (claude_cli.LANE_PERMISSION_MODE), which honors
    the settings' allow rules and runs their hooks; a bypassPermissions
    default would put any lane started without a mode flag in bypass."""
    if not isinstance(doc, dict):
        return ["the file is not a JSON object"]
    out = []
    perms = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
    if perms.get("defaultMode") == "bypassPermissions":
        out.append("permissions.defaultMode is bypassPermissions")
    hooks = doc.get("hooks")
    if hooks:
        events = sorted(hooks) if isinstance(hooks, dict) else ["(not an object)"]
        out.append(f"hooks are set ({', '.join(str(e) for e in events)})")
    allow = perms.get("allow")
    if isinstance(allow, list) and allow:
        out.append(f"permissions.allow holds {len(allow)} rule(s), which every lane honors")
    return out


def check_claude_settings(ctx: Ctx) -> list[dict]:
    """ea-bot's ~/.claude/settings.json, read with the resident's access:
    no bypassPermissions default, no hooks and no allow rules. A WARN: the
    file is the resident's own, and the persistence digest reports each
    change to it."""
    name = "claude settings: no bypass default, no hooks"
    if not ctx.eabot_home:
        return [res("WARN", name, f"no {RESIDENT_USER} user")]
    path = Path(ctx.eabot_home) / ".claude" / "settings.json"
    data = _read_shared(path)
    if data is None:
        return [res("PASS", name, "no ~/.claude/settings.json")]
    try:
        doc = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return [res("WARN", name, "~/.claude/settings.json is not valid JSON")]
    found = claude_settings_findings(doc)
    if found:
        return [res("WARN", name, "; ".join(found),
                    f"remove them from {path} (the lanes pass their own mode and allow list)")]
    return [res("PASS", name, "no bypass default, no hooks, no allow rules")]


def check_mediation(ctx: Ctx) -> list[dict]:
    name = "mediation: the mail tokens stay with the owner's mediator"
    fix = "sudo bash deploy/enable-mail-mediation.sh"
    if not _mediation_on(ctx):
        return [res("FAIL", name, "mail mediation is off for the resident", fix)]
    state = _systemctl("is-active", MEDIATOR_UNIT)
    if shutil.which("systemctl") and state != "active":
        return [res("WARN", name, f"mediation is on, but {MEDIATOR_UNIT} is {state or 'unknown'} "
                    "(mail reads and drafts fail; nothing is exposed)",
                    f"sudo systemctl restart {MEDIATOR_UNIT}")]
    return [res("PASS", name, "on" + (f", {MEDIATOR_UNIT} active" if state else ""))]


# The resident submits these through the mediator's queue, each with a path
# argument that names an owner-only decoy, in a different spelling. The
# mediator must refuse every one. The verb is graph_actions create-draft:
# the tool reads --body-file in _resolve_body, before graph_client makes any
# call, and the decoy is empty, so the tool's empty-body guard exits there.
# A mediator that lets a spelling through therefore starts a child that
# reads an empty owner file and exits 1; no draft is made and no mail API
# call is made. The address is in the reserved .invalid domain.
MEDIATOR_DECOY = STATE_FILE.parent / "mediator-decoy"
MEDIATOR_PROBE_WAIT_S = 45
_PROBE_DRAFT = ["create-draft", "--to", "boundary-audit@example.invalid",
                "--subject", "boundary audit probe", "--body", "x"]


def mediator_probe_requests(decoy: str) -> list[dict]:
    """The probe requests: the exact spelling, the `=` spelling, and two
    prefixes argparse resolves to --body-file."""
    forms = [("--body-file", ["--body-file", decoy]),
             ("--body-file=", [f"--body-file={decoy}"]),
             ("--body-fi", ["--body-fi", decoy]),
             ("--body-f=", [f"--body-f={decoy}"])]
    return [{"id": i, "label": label, "tool": "graph_actions", "verb": "create-draft",
             "argv": _PROBE_DRAFT + form} for i, (label, form) in enumerate(forms)]


# Run as the resident with `python3 -I -c`. argv[1] is JSON {queue, timeout,
# requests: [{id, tool, verb, argv}]}. Each request goes into the queue the
# way mail_mediation.forward_or_none writes one; each answer is read and
# removed; a request still waiting at the deadline is withdrawn. stdout is a
# JSON object {id: {rc, stderr} | {result: "withdrawn" | "timeout"}}.
MEDIATOR_PROBE = r'''
import json, os, sys, time, uuid
spec = json.loads(sys.argv[1])
queue = spec["queue"]
ids = {}
for r in spec["requests"]:
    rid = "%d-audit-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:12])
    tmp = os.path.join(queue, ".req-%s.tmp" % rid)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump({"tool": r["tool"], "verb": r["verb"], "argv": r["argv"],
                   "ts": time.time(), "run_id": ""}, f)
    os.chmod(tmp, 0o640)
    os.replace(tmp, os.path.join(queue, "req-%s.json" % rid))
    ids[rid] = str(r["id"])
out = {}
deadline = time.time() + float(spec["timeout"])
while ids and time.time() < deadline:
    for rid in list(ids):
        resp = os.path.join(queue, "resp-%s.json" % rid)
        try:
            fd = os.open(resp, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        os.unlink(resp)
        out[ids.pop(rid)] = {"rc": int(d.get("rc", 0)), "stderr": str(d.get("stderr", ""))[-400:]}
    time.sleep(0.3)
for rid, i in ids.items():
    try:
        os.unlink(os.path.join(queue, "req-%s.json" % rid))
        out[i] = {"result": "withdrawn"}
    except FileNotFoundError:
        out[i] = {"result": "timeout"}
print(json.dumps(out))
'''


def _mediator_decoy(path: Path) -> str | None:
    """The decoy: an empty file only the owner can open, in this script's
    own 0700 state directory. Made when missing; None when the name holds
    anything but this user's plain file."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     0o600)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            return None
        if st.st_size:
            os.ftruncate(fd, 0)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return str(path)


def judge_mediator_probe(requests: list[dict], answers: dict) -> tuple[str, str]:
    """(status, detail) for the probe's answers. PASS only when the mediator
    refused every request; an answer from the tool itself is a FAIL."""
    through, silent = [], []
    for r in requests:
        a = answers.get(str(r["id"])) if isinstance(answers, dict) else None
        if not isinstance(a, dict) or "rc" not in a:
            silent.append(f"{r['label']} ({(a or {}).get('result', 'no answer')})")
            continue
        err = str(a.get("stderr", ""))
        if a["rc"] == 2 and "mediator refused" in err:
            continue
        what = ("the owner-side tool read the decoy" if "Empty body" in err
                else f"the owner-side tool ran (rc {a['rc']})")
        through.append(f"{r['label']}: {what}")
    if through:
        return "FAIL", (f"{len(through)} of {len(requests)} spelling(s) got through: "
                        + "; ".join(through))
    if silent:
        return "WARN", "the mediator did not answer: " + ", ".join(silent)
    return "PASS", (f"{len(requests)} create-draft request(s) with an owner-only decoy "
                    "refused: " + ", ".join(r["label"] for r in requests))


def check_mediator_argv(ctx: Ctx) -> list[dict]:
    """A mediated request whose path argument names an owner-only file is
    refused in every spelling argparse takes, submitted by the resident to
    the live mediator."""
    import mail_mediation as mm
    import resident_io as rio
    name = "mediation: a mediated path outside ea-bot's dirs is refused"
    fix = "refuse the spelling in mail_mediation.sanitize (tests/test_mediator_argv_tripwire.py)"
    if ctx.resident is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to probe")]
    if not _mediation_on(ctx):
        return [res("WARN", name, "mail mediation is off; nothing to probe")]
    if shutil.which("systemctl") and _systemctl("is-active", MEDIATOR_UNIT) != "active":
        return [res("WARN", name, f"{MEDIATOR_UNIT} is not active; nothing to probe")]
    decoy = _mediator_decoy(MEDIATOR_DECOY)
    if decoy is None:
        return [res("WARN", name, f"could not make the decoy {MEDIATOR_DECOY}")]
    if mm._within_allowed(decoy):
        return [res("WARN", name, f"the decoy {decoy} is inside an allowed root; it proves nothing")]
    requests = mediator_probe_requests(decoy)
    spec = {"queue": os.path.realpath(ctx.ea_dir / "state" / "mail-queue"),
            "timeout": MEDIATOR_PROBE_WAIT_S,
            "requests": [{k: r[k] for k in ("id", "tool", "verb", "argv")} for r in requests]}
    argv = [rio._resident_python(), "-I", "-c", MEDIATOR_PROBE,
            json.dumps(spec, separators=(",", ":"))]
    if not rio._policy_allows(argv):
        return [res("WARN", name, "the sudo policy does not let the owner run the probe as the "
                    "resident", f"allow the owner to run {rio._resident_python()} -I as "
                    f"{RESIDENT_USER} in sudoers")]
    try:
        p = subprocess.run([rio._sudo(), "-n", "-u", RESIDENT_USER] + argv, capture_output=True,
                           timeout=MEDIATOR_PROBE_WAIT_S + 15, cwd="/",
                           env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [res("WARN", name, f"the probe did not run ({type(e).__name__})")]
    try:
        answers = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        answers = None
    if not isinstance(answers, dict):
        err = p.stderr.decode(errors="replace").strip().splitlines()
        return [res("WARN", name, f"the probe exited {p.returncode}"
                    + (f": {err[-1][:160]}" if err else ""))]
    status, detail = judge_mediator_probe(requests, answers)
    return [res(status, name, detail, fix if status == "FAIL" else "")]


def check_sudo(ctx: Ctx) -> list[dict]:
    name = "sudo: owner writes reach resident space as the resident"
    if ctx.resident is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user")]
    import resident_io as rio
    child = rio._probe_resident()
    form = "its own file" if rio._child_program()[0] != "-c" else "inline"
    helper = [rio._resident_python(), "-I", str(ctx.live_tree / "preprocessor" / "mail_mediator.py"),
              "--confined-io", "read", str(ctx.ea_dir), "/"]
    mediator = rio._policy_allows(helper)
    refused = [what for what, ok in (("the resident_io child", child),
                                     ("the mediator's file helper", mediator)) if not ok]
    if refused:
        return [res("WARN", name, f"the policy refuses {' and '.join(refused)}; those owner "
                    "processes read and write resident space directly (O_NOFOLLOW, atomic replace)",
                    f"allow the owner to run {rio._resident_python()} -I as {RESIDENT_USER} in sudoers")]
    return [res("PASS", name, f"the resident_io child ({form}) and the mediator's file helper")]


def _mount_opts(text: str) -> dict[str, str]:
    out = {}
    for item in text.split(","):
        if item:
            key, _, value = item.partition("=")
            out[key] = value
    return out


def proc_mount_options(mountinfo: str) -> dict[str, str] | None:
    """The options of the proc mount at /proc, from mountinfo text: the
    mount's own and its superblock's, where hidepid and gid live. The last
    such line is the mount on top."""
    found = None
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if sep and len(fields) >= 6 and len(rest) >= 3 and fields[4] == "/proc" and rest[0] == "proc":
            found = {**_mount_opts(fields[5]), **_mount_opts(rest[2])}
    return found


def fstab_proc_options(fstab: str) -> dict[str, str] | None:
    """The options of the first active /proc line in fstab text, or None."""
    for line in fstab.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and f[1].rstrip("/") == "/proc":
            return _mount_opts(f[3])
    return None


def _hidepid(opts: dict | None) -> str:
    value = (opts or {}).get("hidepid", "off")
    return HIDEPID.get(value, value)


def _proc_gid(opts: dict | None) -> int | None:
    value = (opts or {}).get("gid")
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    try:
        return grp.getgrnam(value).gr_gid
    except KeyError:
        return None


def _gid_label(gid: int) -> str:
    try:
        return f"{gid} ({grp.getgrgid(gid).gr_name})"
    except KeyError:
        return str(gid)


def _resident_sees(pid: int) -> bool | None:
    """Whether the resident can open /proc/<pid>/status, asked of the
    resident_io child, which runs with the resident's access. None when no
    child answers: this is not the owner's process, the policy refuses the
    child, or the call fails."""
    import resident_io as rio
    try:
        if not (rio.is_owner() and rio._resident_available()):
            return None
        return rio._child_read(f"/proc/{pid}/status") is not None
    except Exception:
        return None


def check_proc(ctx: Ctx, probe=None, held_gids=None) -> list[dict]:
    """hidepid=invisible on /proc hides every other user's processes from the
    resident; the gid= group keeps the owner's full view (the bot's start-up
    reap of orphaned dispatch runs lists the resident's processes).
    deploy/stage-proc-hidepid.sh writes the fstab line and the boot applies
    it. The probe child runs as the resident, so its answer only counts
    against a pass: a PASS also needs the mount options and the resident's
    groups to show that the resident is blind."""
    name = "proc: the resident sees only its own processes"
    fix = "sudo bash deploy/stage-proc-hidepid.sh, then reboot"
    who = ctx.resident
    if who is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to judge")]
    try:
        live = proc_mount_options(Path(ctx.mountinfo).read_text())
    except OSError as e:
        return [res("WARN", name, f"could not read {ctx.mountinfo} ({type(e).__name__})")]
    try:
        staged = fstab_proc_options(Path(ctx.fstab).read_text())
    except OSError:
        staged = None
    if live is None:
        return [res("WARN", name, f"no proc mount at /proc in {ctx.mountinfo}")]
    mode, gid = _hidepid(live), _proc_gid(live)
    staged_gid = _proc_gid(staged)
    if mode == "off":
        if _hidepid(staged) != "invisible":
            return [res("WARN", name, "not staged: the resident lists every process on the host, "
                        "with its command line", fix)]
        detail = (f"staged, not active: {ctx.fstab} mounts /proc with hidepid=invisible"
                  + (f", gid={_gid_label(staged_gid)}" if staged_gid is not None else "")
                  + " at the next boot; until then the resident lists every process")
        if staged_gid is not None and staged_gid in who.gids:
            detail += f"; the resident holds gid {staged_gid}, so that boot hides nothing from it"
        return [res("WARN", name, detail, "reboot")]
    if mode == "noaccess":
        return [res("FAIL", name, "hidepid=noaccess is live: the resident still lists every "
                    "process ID, and only their contents are hidden", fix)]
    if mode != "invisible":
        return [res("WARN", name, f"hidepid={mode} is live: the gid= exemption does not apply "
                    "to it, so the owner's processes lose the resident's too", fix)]
    problems = []
    if gid is not None and gid in who.gids:
        problems.append(f"the resident holds the exemption gid {_gid_label(gid)}")
    sees = (probe or _resident_sees)(os.getpid())
    if sees:
        problems.append("the resident child opened the /proc entry of an owner process")
    if problems:
        return [res("FAIL", name, "; ".join(problems),
                    "sudo bash deploy/stage-proc-hidepid.sh (it takes the resident out of the "
                    "group), then reboot")]
    held = (set(os.getgroups()) | {os.getegid()}) if held_gids is None else set(held_gids)
    notes = []
    if gid is None:
        notes.append("no gid= exemption, so the owner's processes cannot see the resident's")
    elif gid not in held:
        notes.append(f"this owner process does not hold gid {_gid_label(gid)}, so the owner's "
                     "processes cannot see the resident's")
    if _hidepid(staged) != "invisible" or staged_gid != gid:
        notes.append(f"{ctx.fstab} no longer carries this /proc line, so the next boot drops it")
    if notes:
        return [res("WARN", name, "hidepid=invisible is live and hides the owner's processes "
                    "from the resident, but " + "; ".join(notes), fix)]
    how = ("the resident child could not open this process's /proc entry" if sees is False
           else "judged from the mount and the groups (no resident child to ask)")
    return [res("PASS", name, f"hidepid=invisible, gid={_gid_label(gid)}; the resident holds no "
                f"exemption, and {how}")]


_REDIRECT =re.compile(r"(?:^|[\s;|&])(?:\d|&)?>>?\s*([^\s;|&<>]+)")
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _brief(line: str, width: int = 80) -> str:
    brief = re.sub(r"(\b[A-Za-z_][A-Za-z0-9_]*=)\S+", r"\1…", " ".join(line.split()))
    return brief if len(brief) <= width else brief[:width - 1] + "…"


def cron_findings(text: str, in_resident_space) -> list[tuple[str, str]]:
    """(problem, job) for each crontab line that writes into, runs from, works
    in, or searches its PATH in resident space."""
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        env = _ENV_LINE.match(line)
        if env:
            if env.group(1) == "PATH":
                for entry in env.group(2).strip("\"'").split(":"):
                    if entry and in_resident_space(entry):
                        found.append(("the cron PATH names a directory in resident space", _brief(line)))
            continue
        fields = line.split(None, 1 if line.startswith("@") else 5)
        command = fields[-1] if len(fields) > (1 if line.startswith("@") else 5) else ""
        for target in _REDIRECT.findall(command):
            if target.startswith("/") and in_resident_space(target):
                found.append(("a job writes its output into resident space", _brief(line)))
        for segment in re.split(r"&&|\|\||;|\|", command):
            words = [w for w in segment.split() if not _ENV_LINE.match(w)]
            if not words:
                continue
            if words[0] == "cd" and len(words) > 1 and in_resident_space(words[1]):
                found.append(("a job works in resident space", _brief(line)))
                continue
            prog = words[0]
            rest = [w for w in words[1:] if not w.startswith("-")]
            if os.path.basename(prog).rstrip("0123456789.") in INTERPRETERS and rest:
                prog = rest[0]
            if prog.startswith("/") and in_resident_space(prog):
                found.append(("a job runs code from resident space", _brief(line)))
    return found


def check_cron(ctx: Ctx, crontab: str | None = None) -> list[dict]:
    name = "cron: owner jobs stay off resident space"
    if crontab is None:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [res("WARN", name, f"crontab -l failed ({type(e).__name__})")]
        if r.returncode != 0:
            return [res("WARN", name, "no crontab installed")]
        crontab = r.stdout
    found = cron_findings(crontab, ctx.in_resident_space)
    problems = []
    if found:
        kinds = sorted({k for k, _ in found})
        problems.append(f"{len(found)} finding(s): {'; '.join(kinds)} (first: {found[0][1]})")
    logs = ctx.owner_logs
    st = _lstat(str(logs))
    if st is not None:
        if (st.st_uid != os.geteuid() or not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077
                or (ctx.resident is not None and reaches(ctx.resident, os.path.realpath(logs)))):
            problems.append("the owner log dir is not the owner's alone (mode 700)")
    if problems:
        return [res("FAIL", name, "; ".join(problems),
                    f"point the job at {logs} (crontab ~/crontab.master); chmod 700 {logs}")]
    jobs = [ln for ln in crontab.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return [res("PASS", name, f"{len(jobs)} line(s); none reaches resident space")]


def check_env_secrets(ctx: Ctx) -> list[dict]:
    name = "env: no secret in resident space"
    import owner_trust
    try:
        data = _read_shared(ctx.ea_dir / ".env")
    except Exception as e:
        return [res("WARN", name, f"could not read ~/.ea/.env ({type(e).__name__})")]
    if data is None:
        return [res("PASS", name, "no ~/.ea/.env")]
    named = set(owner_trust.parse_env(data.decode("utf-8", errors="replace")))
    keys = sorted(named - RESIDENT_SECRETS)
    if keys:
        return [res("FAIL", name, f"~/.ea/.env names {len(keys)} owner secret key(s): {', '.join(keys)}",
                    f"move them to {ctx.private / 'secrets.env'} (mode 600), then rotate them")]
    shared = sorted(named & RESIDENT_SECRETS)
    return [res("PASS", name, "~/.ea/.env names no owner secret"
                + (f" ({', '.join(shared)} is the resident's by design)" if shared else ""))]


# ── the resident's own scheduling ─────────────────────────────────────

CRON_FILES = {"cron": (Path("/etc/cron.allow"), Path("/etc/cron.deny")),
              "at": (Path("/etc/at.allow"), Path("/etc/at.deny"))}
HARDEN_FIX = "sudo bash deploy/harden-permissions.sh"


def _listed(path: Path, user: str) -> bool | None:
    """Whether `path` names `user` on a line of its own; None when it is absent."""
    try:
        return user in {ln.strip() for ln in path.read_text().splitlines()}
    except FileNotFoundError:
        return None


def scheduler_allows(kind: str, files=None, user: str = RESIDENT_USER) -> bool:
    """Whether `user` may install a `kind` job (cron or at): with an allow
    file only a user it names may; otherwise any user the deny file does not
    name may."""
    allow, deny = (files or CRON_FILES)[kind]
    listed = _listed(allow, user)
    if listed is not None:
        return listed
    return not _listed(deny, user)


def _resident_crontab() -> tuple[str, list[str]]:
    """("none" | "entries" | "denied" | "unknown", job lines) for the
    resident's crontab, read as the resident with `crontab -l` in / with a
    minimal environment (its spool file is root's to read)."""
    crontab = shutil.which("crontab", path="/usr/bin:/bin")
    if crontab is None:
        return "unknown", []
    try:
        r = subprocess.run(["sudo", "-n", "-u", RESIDENT_USER, crontab, "-l"],
                           capture_output=True, text=True, timeout=15, cwd="/",
                           env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", []
    err = r.stderr.lower()
    if r.returncode == 0:
        jobs = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        return ("entries" if jobs else "none"), jobs
    if "no crontab" in err:
        return "none", []
    if "not allowed" in err or ("permission denied" in err and "crontab" in err):
        return "denied", []
    return "unknown", []


def check_resident_cron(ctx: Ctx, probe=None, files=None, kinds=None) -> list[dict]:
    """The resident has no crontab or at job of its own, and may not install
    one. Every EA job is the owner's (crontab.master)."""
    name = "resident cron: no jobs of its own"
    state, jobs = (probe or _resident_crontab)()
    if kinds is None:
        kinds = ["cron"] + (["at"] if shutil.which("at") else [])
    open_to = [k for k in kinds if scheduler_allows(k, files)]
    if state == "entries":
        return [res("FAIL", name, f"ea-bot has a crontab with {len(jobs)} job(s) "
                    f"(first: {_brief(jobs[0])}); nothing in the install writes one", HARDEN_FIX)]
    detail = {"none": "ea-bot has no crontab", "denied": "ea-bot may not run crontab",
              "unknown": "ea-bot's crontab could not be read"}[state]
    if open_to:
        return [res("WARN", name, f"{detail}; {' and '.join(open_to)} would accept one from it "
                    "(no deny line)", HARDEN_FIX)]
    if state == "unknown":
        return [res("WARN", name, f"{detail}; {' and '.join(kinds)} refuse it")]
    return [res("PASS", name, f"{detail}; {' and '.join(kinds)} refuse it")]


LINGER_DIR = Path("/var/lib/systemd/linger")
CGROUP_USER_SLICE = Path("/sys/fs/cgroup/user.slice")
DECK_BUILDER_TAG = "# ea-deck-builder"
# Units a desktop distribution starts in every user manager, and the EA's own
# transient deck scopes (the deck plugin's builds). Anything else is reported by name.
USER_UNIT_BASELINE = (
    "init.scope", "dbus.service", "dbus.socket", "dbus-broker.service",
    "pipewire*.service", "pipewire*.socket", "wireplumber.service", "filter-chain.service",
    "xdg-*.service", "gpg-agent*.socket", "ssh-agent.socket", "gcr-ssh-agent.socket",
    "gnome-keyring*.service", "at-spi-dbus-bus.service", "snap.*", "run-*.scope",
    "ea-deck-*.scope", "ea-deck-*.service",
)


def _baseline(name: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatchcase(name, pat) for pat in USER_UNIT_BASELINE)


def _unit_files(home: str, lister) -> list[str] | None:
    """Unit names under the resident's ~/.config/systemd/user: unit files
    and the links in its .wants and .requires directories."""
    top = f"{home.rstrip('/')}/.config/systemd/user"
    entries = lister(top)
    if entries is None:
        return []
    names = []
    for kind, n in entries:
        if kind == "d" and n.endswith((".wants", ".requires", ".d")):
            names += [m for _, m in lister(f"{top}/{n}") or [] if "." in m]
        elif kind != "d":
            names.append(n)
    return sorted(set(names))


def _running_units(uid: int, root: Path) -> list[str] | None:
    """Unit names running in the resident's user manager, from its cgroup
    tree (readable by anyone; nothing runs). None when it is not running."""
    base = root / f"user-{uid}.slice" / f"user@{uid}.service"
    if not base.is_dir():
        return None
    names = []
    for d in [base] + [p for p in base.iterdir() if p.is_dir() and p.name.endswith(".slice")]:
        for p in d.iterdir():
            if p.is_dir() and p.name.rsplit(".", 1)[-1] in ("service", "scope", "socket", "timer"):
                names.append(p.name)
    return sorted(set(names))


def check_resident_units(ctx: Ctx, lister=None, cgroup_root: Path | None = None,
                         linger_dir: Path | None = None, fstab: Path | None = None) -> list[dict]:
    """The resident's systemd user units and its linger. Linger keeps its
    user manager, and so any unit it enables, running with no login; only
    the deck builder needs it."""
    name = "resident units: none of its own, linger only for decks"
    if ctx.eabot_home is None:
        return [res("WARN", name, f"no {RESIDENT_USER} user; nothing to judge")]
    if lister is None:
        import resident_io
        lister = resident_io.list_dir
    uid = pwd.getpwnam(RESIDENT_USER).pw_uid if ctx.resident is None else ctx.resident.uid
    lingers = (linger_dir or LINGER_DIR).joinpath(RESIDENT_USER).exists()
    try:
        decks = DECK_BUILDER_TAG in (fstab or ctx.fstab).read_text()
    except OSError:
        decks = False
    files = _unit_files(ctx.eabot_home, lister)
    running = _running_units(uid, cgroup_root or CGROUP_USER_SLICE) or []
    extra = sorted({n for n in (files or []) + running if not _baseline(n)})
    problems, notes = [], []
    if extra:
        problems.append(f"{len(extra)} user unit(s) outside the distribution baseline: "
                        f"{', '.join(extra[:SHOWN])}{' …' if len(extra) > SHOWN else ''}")
    if lingers and not decks:
        problems.append("ea-bot lingers and the deck builder is not enabled")
    notes.append(f"linger {'on (the deck builder needs it)' if lingers and decks else 'on' if lingers else 'off'}")
    notes.append(f"{len(files or [])} unit file(s), {len(running)} running unit(s), all baseline"
                 if not extra else "")
    if problems:
        fix = (f"review them as {RESIDENT_USER}: systemctl --user list-unit-files; "
               f"{HARDEN_FIX} switches linger off without the deck builder")
        return [res("WARN", name, "; ".join(problems + [n for n in notes[:1]]), fix)]
    return [res("PASS", name, "; ".join(n for n in notes if n))]


SSHD_CONFIG = Path("/etc/ssh/sshd_config")
SSH_DROPIN = Path("/etc/ssh/sshd_config.d/01-ea-deny-resident.conf")
SSH_FIX = "sudo bash deploy/ssh-deny-resident.sh"


def check_ssh(ctx: Ctx) -> list[dict]:
    """sshd refuses logins for the resident, which controls its own
    authorized_keys: the drop-in names it in DenyUsers and sshd_config
    includes the drop-in directory. (The effective value needs root; the
    script's --status shows it.)"""
    name = "ssh: the resident cannot log in"
    try:
        text = SSH_DROPIN.read_text()
    except FileNotFoundError:
        return [res("WARN", name, "not staged: sshd accepts a login for the resident with a key it plants", SSH_FIX)]
    except OSError as e:
        return [res("WARN", name, f"could not read {SSH_DROPIN} ({type(e).__name__})")]
    denied = any(ln.split()[:1] == ["DenyUsers"] and RESIDENT_USER in ln.split()[1:]
                 for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    if not denied:
        return [res("FAIL", name, f"{SSH_DROPIN} does not deny {RESIDENT_USER}", SSH_FIX)]
    try:
        main = SSHD_CONFIG.read_text()
    except OSError as e:
        return [res("WARN", name, f"could not read {SSHD_CONFIG} ({type(e).__name__})")]
    if not re.search(r"(?m)^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf", main):
        return [res("FAIL", name, f"{SSHD_CONFIG} does not include sshd_config.d, so the drop-in is ignored")]
    return [res("PASS", name, f"{SSH_DROPIN.name} denies {RESIDENT_USER}")]


# The owner of the policy's rules file: root. Tests name their own uid.
NETPOLICY_OWNER_UID = 0
NETPOLICY_FIX = "sudo bash deploy/enable-resident-netpolicy.sh"


def _netpolicy_file(ctx: Ctx, NP) -> tuple[list[str], dict | None]:
    """Problems with the policy's rules file, and the facts it was rendered
    from (None when it cannot be trusted or read)."""
    path = str(NP.RULES_FILE)
    st = _lstat(path)
    if st is None:
        return [f"{path} is missing, so a boot loads no policy"], None
    if not stat.S_ISREG(st.st_mode):
        return [f"{path} is not a regular file"], None
    problems = []
    if st.st_uid != NETPOLICY_OWNER_UID or st.st_mode & 0o022:
        problems.append(f"{path} is not root's alone")
    weak = weak_point(ctx.resident, path)
    if weak:
        problems.append(f"the resident can change {weak}")
    if problems:
        return problems, None
    data = _read_owner_tree(Path(path))
    facts = NP.parse(data.decode(errors="replace")) if data is not None else None
    if facts is None:
        return [f"{path} names no facts (the script did not render it)"], None
    return [], facts


def check_netpolicy(ctx: Ctx) -> list[dict]:
    """The resident's network policy: installed and matching this host, and,
    from probes made as the resident, refusing this host and local networks
    while its allowlist still answers."""
    import resident_io as rio
    import resident_netpolicy as NP
    inst = "netpolicy: the resident's network policy is installed"
    deny = "netpolicy: this host and local networks refuse the resident"
    allow = "netpolicy: the resident reaches what the EA needs"
    who = ctx.resident
    if who is None:
        return [res("WARN", inst, f"no {RESIDENT_USER} user; nothing to judge")]
    enabled = _systemctl("is-enabled", NP.UNIT)
    active = _systemctl("is-active", NP.UNIT)
    if _lstat(str(NP.RULES_FILE)) is None and enabled in ("", "not-found"):
        return [res("WARN", inst, "not installed: the resident can open connections to this "
                    "host and to local networks", NETPOLICY_FIX)]
    live = NP.system_facts()
    problems, stored = _netpolicy_file(ctx, NP)
    if shutil.which("systemctl"):
        if enabled != "enabled":
            problems.append(f"{NP.UNIT} is {enabled or 'not installed'}, so a boot does not load it")
        if active != "active":
            problems.append(f"{NP.UNIT} is {active or 'unknown'}")
    drifted = []
    if stored is not None:
        if int(stored.get("uid", -1)) != who.uid:
            problems.append(f"the rules filter uid {stored.get('uid')}, the resident is uid {who.uid}")
        drifted = NP.drift(dict(stored, uid=live["uid"]), live)
    if problems:
        out = [res("FAIL", inst, "; ".join(problems), NETPOLICY_FIX)]
    elif drifted:
        out = [res("WARN", inst, "the rules no longer match this host: " + "; ".join(drifted),
                   f"{NETPOLICY_FIX} (renders them again)")]
    else:
        out = [res("PASS", inst, f"{NP.UNIT} enabled and active; {NP.RULES_FILE} is root's "
                   f"alone, for uid {who.uid}")]

    socks = NP.sockets()
    targets = NP.probe_targets(live, socks)
    if not rio._policy_allows(NP.probe_argv(targets)):
        why = "the sudo policy does not let the owner run the probe as the resident"
        fix = f"allow the owner to run {NP.PYTHON} -I as {RESIDENT_USER} in sudoers"
        return out + [res("WARN", deny, why, fix), res("WARN", allow, why, fix)]
    try:
        results = NP.run_probes(targets, [rio._sudo(), "-n", "-u", RESIDENT_USER])
    except NP.ProbeError as e:
        return out + [res("WARN", deny, str(e)), res("WARN", allow, str(e))]
    j = NP.judge(targets, results)

    if j["deny_fail"]:
        out.append(res("FAIL", deny, f"{len(j['deny_fail'])} of {j['host'] + j['net']} probe(s) got "
                       f"through: {_named(j['deny_fail'])}", NETPOLICY_FIX))
    elif not j["loaded"] or j["deny_unknown"]:
        out.append(res("WARN", deny, "the probes do not show the policy's refusal"
                       + (f": {_named(j['deny_unknown'])}" if j["deny_unknown"] else " on loopback"),
                       "sudo bash deploy/enable-resident-netpolicy.sh --status"))
    else:
        noroute = f"; {j['noroute']} had no route" if j["noroute"] else ""
        out.append(res("PASS", deny, f"{j['refused']} probe(s) refused: loopback, this host's "
                       f"addresses and {j['net']} local range(s){noroute}"))

    theirs = NP.other_listeners(live, socks)
    if j["allow_fail"]:
        out.append(res("FAIL", allow, f"the policy refuses the EA's own needs: {_named(j['allow_fail'])}",
                       f"{NETPOLICY_FIX} (renders the allowlist again), or --rollback"))
    elif theirs:
        out.append(res("WARN", allow, "another user listens on a loopback port the allowlist opens, "
                       f"so the resident reaches it: {_named(theirs)}",
                       "move that service off the EA's deck ports"))
    elif j["allow_unknown"]:
        out.append(res("WARN", allow, f"no clear answer from: {_named(j['allow_unknown'])}"))
    else:
        kinds = sorted({t["label"] for t in targets if t["expect"] == "reached"})
        out.append(res("PASS", allow, f"{j['allow']} probe(s) answered: {', '.join(kinds)}"))
    return out


def _mount_points() -> set[str]:
    """Every mount point in this process's namespace, from the kernel's
    table. os.path.ismount() compares device numbers, so it misses a bind
    mount from the same file system, which is what the auth pin is."""
    out = set()
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 4:
                    out.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[4]))
    except OSError:
        pass
    return out


def _is_mount(path: str) -> bool:
    return path in _mount_points() or os.path.ismount(path)


def check_auth_pinned(ctx: Ctx, ismount=_is_mount, system=None) -> list[dict]:
    """On Linux, the auth directory and the EA tree above it are mount points
    (deploy/pin-auth-dir.sh), which no one can rename. The EA tree is the
    resident's directory, so without them the resident can move auth/ aside;
    the owner checks then refuse it and mail stops."""
    name = "auth: pinned (the resident cannot rename it away)"
    if (system or platform.system()) != "Linux":
        return [res("PASS", name, "not Linux; bind mounts are not used here")]
    auth = ctx.ea_dir / "auth"
    if _lstat(str(auth)) is None:
        return [res("PASS", name, "no auth directory yet")]
    # The EA tree is often reached through a link (~/.ea -> ~ea-bot/.ea), and
    # ismount() is false for a link: judge the real paths.
    real = [os.path.realpath(str(p)) for p in (ctx.ea_dir, auth)]
    loose = [p for p in real if not ismount(p)]
    if loose:
        return [res("WARN", name, "not a mount point: " + ", ".join(loose),
                    "sudo bash /opt/ea-system/deploy/pin-auth-dir.sh")]
    return [res("PASS", name, f"{real[0]} and {real[1]} are mount points")]


CHECKS = [check_doctor, check_tokens, check_auth_custody, check_auth_pinned, check_private_reach, check_code_trees,
          check_units, check_deny_lists, check_claude_settings, check_mediation, check_mediator_argv,
          check_sudo, check_proc, check_cron, check_env_secrets, check_netpolicy, check_ssh,
          check_resident_cron, check_resident_units]


def run_checks(ctx: Ctx, checks=None) -> list[dict]:
    results = []
    for check in checks or CHECKS:
        try:
            results.extend(check(ctx))
        except Exception as e:
            results.append(res("WARN", f"{check.__name__}: did not run", f"{type(e).__name__}: {e}"))
    return results


# ── alert on change ───────────────────────────────────────────────────

def fingerprint(results: list[dict]) -> list[str]:
    """Each check that does not pass, with its status and a digest of its
    detail: a different problem in the same check is a change."""
    return sorted(f"{r['name']}|{r['status']}|"
                  f"{hashlib.sha256(r['detail'].encode()).hexdigest()[:10]}"
                  for r in results if r["status"] != "PASS")


def decide(prev: dict, results: list[dict]) -> tuple[str | None, dict]:
    """The chat post for this run (None when the result is unchanged) and
    the state to keep. The post names checks and statuses only."""
    fp = fingerprint(results)
    state = {"fingerprint": fp, "checked": datetime.now(timezone.utc).isoformat()}
    was = prev.get("fingerprint")
    if fp == (was or []):
        return None, state
    bad = [r for r in results if r["status"] != "PASS"]
    if not bad:
        return ":white_check_mark: *EA boundary audit*: every check passes again.", state
    fails = sum(r["status"] == "FAIL" for r in bad)
    lines = [f":shield: *EA boundary audit*: {len(bad)} check(s) do not pass"
             f" ({fails} FAIL, {len(bad) - fails} WARN)"]
    lines += [f"• {r['status']} {r['name']}" for r in bad]
    lines.append("_The details are in the audit's table in the owner's cron log "
                 "(scripts/boundary-audit.py)._")
    return "\n".join(lines), state


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    try:
        import atomic_io
        STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_io.write_json(STATE_FILE, state, mode=0o600)
    except Exception as e:
        print(f"boundary-audit: could not save {STATE_FILE}: {e}", file=sys.stderr)


def _post(text: str) -> bool:
    """Post to the health channel (the main EA channel when none is
    configured); retry once, then the personal channel."""
    try:
        import slack_notifier
    except Exception as e:
        print(f"boundary-audit: no chat notifier ({e})", file=sys.stderr)
        return False
    try:
        key = slack_notifier.health_channel_key()
    except Exception:
        key = "ea-assistant"
    for attempt in (1, 2):
        try:
            if slack_notifier.post_to_slack(key, text, priority="urgent",
                                            _bypass_quiet=True):
                return True
        except Exception:
            pass
        if attempt == 1:
            time.sleep(3)
    try:
        return bool(slack_notifier.post_to_slack("personal", f"[EA boundary audit, fallback]\n{text}",
                                                 priority="urgent", _bypass_quiet=True))
    except Exception:
        return False


def _table(results: list[dict]) -> str:
    mark = {"PASS": "ok", "WARN": "warn", "FAIL": "FAIL"}
    width = min(60, max((len(r["name"]) for r in results), default=10))
    rows = [f"{'STATUS':<6}  {'CHECK':<{width}}  DETAIL"]
    for r in results:
        rows.append(f"{mark[r['status']]:<6}  {r['name']:<{width}}  {r['detail']}")
        if r["status"] != "PASS" and r.get("fix"):
            rows.append(f"{'':<6}  {'':<{width}}  fix: {r['fix']}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily read-only check of the owner and resident boundary")
    ap.add_argument("--dry-run", action="store_true", help="check and print only: no post, no state write")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ea-dir", default=os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    a = ap.parse_args(argv)
    results = run_checks(Ctx.live(Path(a.ea_dir)))
    text, state = decide(_load_state(), results)
    posted = False
    if not a.dry_run:
        if text is not None:
            posted = _post(text)
            if not posted:
                print("boundary-audit: the chat post failed; the next run tries again", file=sys.stderr)
        if text is None or posted:
            _save_state(state)
    fails = sum(r["status"] == "FAIL" for r in results)
    warns = sum(r["status"] == "WARN" for r in results)
    if a.json:
        print(json.dumps({"results": results, "fingerprint": state["fingerprint"],
                          "changed": text is not None, "posted": posted}, indent=1))
    else:
        print(_table(results))
        verdict = ("changed; " + ("posted" if posted else "would post" if a.dry_run else "post failed")
                   if text is not None else "unchanged since the last run")
        print(f"\n{len(results) - fails - warns} ok, {warns} warn, {fails} FAIL; {verdict}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
