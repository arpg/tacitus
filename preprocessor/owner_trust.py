#!/usr/bin/env python3
"""What an owner process may take from a path, and where owner secrets live.

The owner's services and crons (the bot, the mail mediator, the scheduled-send
check, owner plugins' token harvests) run as the owner and hold the tokens.
The resident runs as `ea-bot` and is treated as untrusted. Everything under
the resident's home, `~/.ea` included, is resident space: the resident owns
the directories above every path there, so it can rename an entry away and
put its own in its place.
Ownership of a file alone does not make such a path trustworthy. What does is
an object that this process's user owns, in a directory that no other user
can write, checked on the object that is then read or replaced.

The checks:

  check_owner_dir / check_owner_file
      lstat the object: not a symlink, the expected type, owned by this
      process's euid, not writable by group or others, and a file with one
      link (macOS has no fs.protected_hardlinks, so there the resident can
      give any file it can reach a second name).
  read_owner_file / write_owner_file / set_owner_file_mode
      the same checks on open descriptors (the directory opened with
      O_NOFOLLOW, the file opened relative to it with O_NOFOLLOW), so the
      object checked is the object read or replaced, and a rename in the
      resident's directories between the check and the use changes nothing.

The limit: the checks see who owns an object now, not who wrote it. A file
the resident wrote and root later chowned to the owner passes them. So no
root step adopts an entry in the auth directory (deploy/tree-custody.sh
quarantines what the owner does not own), and no fix line says to chown a
trust anchor. A ctime fingerprint would not close the gap: the owner's own
token writers replace the token files many times a day, and a mode change
moves the ctime too, so every writer would have to keep the fingerprint.

`allow_private_group=True` also accepts group write when the group holds no
user but the owner (no other member, and no other user with it as the primary
group). The token files the owner's processes write are mode 0660 in the
owner's own group, which only the owner can use; a group the resident is in
never passes.

The secrets rule (`owner_secret`): the environment, then
`<private_dir>/secrets.env` (owner processes only), then the legacy
`~/.ea/.env.secrets` if it is a regular file, not a symlink, owned by this
euid and not writable by group or others. `~/.ea/.env` is never a source of
secrets. Env files are parsed as data (KEY=VALUE lines), never executed, and
only the keys in SECRET_KEYS are returned.

`is_owner_process()` decides which processes run the owner checks, with no
user name in the code: this process does not host the EA tree in its own
home (the resident's `~/.ea` is the tree; the owner's is a link into the
resident's home), this euid owns the private dir, and the EA tree in use is
the one the owner's own `~/.ea` names. A resident process fails the first
test whatever it keeps at its own `.ea-private`; a test that points EA_DIR
at a scratch tree fails the last, so it never reads the live private files.

CLI (run as the owner):
    owner_trust.py secret NAME          print one secret for capture, exit 1 if none
    owner_trust.py seed [--secrets] [--chat] [--calendar] [--profile] [--ea-dir DIR]
                        [--chat-frontend F --chat-site URL --chat-bot-email E
                         --chat-owner-id ID] [--chat-stream S
                         --chat-sched-stream S --chat-dm-stream S
                         --chat-notify KEY=STREAM ...] [--chat-public-url URL
                         --chat-port N --chat-tailscale-login L
                         --chat-pair-token-file PATH] [--calendar-id ID]
                        [--calendar-account ADDR] [--profile-name NAME]
                                        create secrets.env, chat.json,
                                        calendars.json and profile.json in the
                                        private dir when missing (the root step
                                        deploy/harden-permissions.sh runs this).
                                        They are copied from ea-bot space, so
                                        every value is shown and each trusted
                                        one (the chat server, bot and owner,
                                        every stream the bot posts to, the web
                                        chat's address, port, login and token
                                        file, the calendar, the name) is typed
                                        back at the terminal or passed with its
                                        flag; otherwise nothing is written. A
                                        web chat store or token file in the
                                        resident's tree is refused
    owner_trust.py token-modes [--ea-dir DIR]
                                        set every harvested token file in the
                                        auth directory to token_file_mode()
"""
from __future__ import annotations

import argparse
import errno
import functools
import grp
import json
import os
import pwd
import re
import secrets as _secrets
import stat
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

SECRETS_FILE = "secrets.env"
LEGACY_SECRETS_FILE = ".env.secrets"
CHAT_FILE = "chat.json"
CALENDARS_FILE = "calendars.json"
PROFILE_FILE = "profile.json"

# The only keys an env file may hand to the code that asks for secrets. An
# entry outside this list (EA_DIR, EA_PRIVATE_DIR, PATH, ...) is ignored, so
# no secrets file can steer where an owner process looks next. Each secret
# has one name; scripts/set-secret.sh offers exactly these.
SECRET_KEYS = frozenset({
    "ZULIP_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    "GRAPH_CLIENT_SECRET", "EA_DASH_TOKEN", "TWOCAPTCHA_API_KEY",
    "REMOTE_LLM_API_KEY", "NTFY_TOKEN",
})

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_WARNED: set[str] = set()


class OwnerTrustError(RuntimeError):
    """An owner process refused a path it would otherwise trust."""


def _warn_once(msg: str) -> None:
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(f"owner_trust: {msg}", file=sys.stderr)


# ── where things are ──────────────────────────────────────────────────

def owner_home() -> Path:
    """This euid's passwd home (not $HOME, which a caller can repoint).
    For an owner process that is the owner's home."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def default_private_dir() -> str:
    """`.ea-private` in the euid's passwd home: the default for
    EA_PRIVATE_DIR, shared by every module that spells the default."""
    return str(owner_home() / ".ea-private")


def private_dir() -> Path:
    """The owner-only directory for state the resident must not reach
    (EA_PRIVATE_DIR, default `.ea-private` in the euid's passwd home).
    Not created here."""
    return Path(os.environ.get("EA_PRIVATE_DIR") or default_private_dir())


def ea_dir() -> Path:
    """The EA tree this process uses (EA_DIR, default ~/.ea), read at call
    time so it follows the environment."""
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))


def install_ea_dir() -> Path:
    """The EA tree the owner installed: `.ea` in this euid's passwd home
    (not $HOME), which the resident cannot repoint."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea"


def hosts_ea_tree() -> bool:
    """True when the EA tree the install names lives in this euid's own
    passwd home: the process is the resident, whose `~/.ea` is the tree
    itself. The owner's `~/.ea` is a link into the resident's home, so for
    the owner the resolved tree sits in another home. False when the tree
    cannot be resolved."""
    try:
        tree = os.path.realpath(install_ea_dir())
        return os.path.dirname(tree) == os.path.realpath(owner_home())
    except (OSError, RuntimeError, ValueError, KeyError):
        return False


def resident_roots(*trees) -> tuple[Path, ...]:
    """The trees an owner-private file must lie outside: the EA tree in
    use, the one the install names, each tree passed, and the home of a
    tree's owner (the resident) when that is another user whose home holds
    the tree. Resolved; never `/`."""
    roots: list[Path] = []
    for tree in (ea_dir(), install_ea_dir(), *trees):
        try:
            real = Path(os.path.realpath(tree))
        except (OSError, ValueError):
            continue
        roots.append(real)
        try:
            uid = os.stat(real).st_uid
            home = Path(os.path.realpath(pwd.getpwuid(uid).pw_dir))
        except (OSError, ValueError, KeyError):
            continue
        if uid != os.geteuid() and home == real.parent:
            roots.append(home)
    return tuple(dict.fromkeys(r for r in roots if str(r) not in ("/", "")))


def inside_roots(path, roots) -> Path | None:
    """The first of `roots` that `path` resolves into, else None."""
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    for root in roots:
        root = Path(root)
        if real == root or real.is_relative_to(root):
            return root
    return None


def is_owner_process() -> bool:
    """True when this process is the owner of the install: it does not host
    the EA tree in its own home (that process is the resident, whatever it
    keeps at its own `.ea-private`), its euid owns the private dir (lstat,
    a directory, not a symlink) and its EA tree resolves to the one the
    owner's `~/.ea` names. Unknown answers on the last test count as owner,
    so the checks stay on."""
    if hosts_ea_tree():
        return False
    try:
        st = os.lstat(private_dir())
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        return False
    try:
        return os.path.realpath(ea_dir()) == os.path.realpath(install_ea_dir())
    except (OSError, RuntimeError, ValueError, KeyError):
        return True


# ── the checks ────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=64)
def _group_has_others(gid: int, uid: int) -> bool:
    """True unless the group is known to hold no user but `uid`: no other
    listed member and no other passwd entry with it as the primary group.
    Any lookup failure counts as 'has others'."""
    try:
        me = pwd.getpwuid(uid).pw_name
        if set(grp.getgrgid(gid).gr_mem) - {me}:
            return True
        return any(p.pw_gid == gid and p.pw_uid != uid for p in pwd.getpwall())
    except Exception:
        return True


def _check_stat(st: os.stat_result, path, kind: str, allow_private_group: bool) -> None:
    """Raise OwnerTrustError unless `st` is a `kind` ("dir" or "file") this
    euid owns and no other user can write; a file must have one link."""
    euid = os.geteuid()
    want = stat.S_ISDIR if kind == "dir" else stat.S_ISREG
    if stat.S_ISLNK(st.st_mode):
        raise OwnerTrustError(f"{path} is a symlink; an owner process does not follow it")
    if not want(st.st_mode):
        raise OwnerTrustError(
            f"{path} is not a {'directory' if kind == 'dir' else 'regular file'}")
    if st.st_uid != euid:
        try:
            who = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            who = str(st.st_uid)
        raise OwnerTrustError(
            f"{path} is owned by {who} (uid {st.st_uid}), not by this process's "
            f"user (uid {euid}); fix: recreate it as the owner from a source you trust "
            f"(sign in again; a new key). Never chown it: its contents are that user's")
    if kind == "file" and st.st_nlink != 1:
        raise OwnerTrustError(
            f"{path} has {st.st_nlink} links, so another name may be a file elsewhere "
            f"(macOS has no protected_hardlinks); fix: recreate it as the owner")
    mode = st.st_mode & 0o7777
    if mode & stat.S_IWOTH:
        raise OwnerTrustError(f"{path} is writable by others (mode {oct(mode)}); fix: chmod o-w {path}")
    if mode & stat.S_IWGRP and not (allow_private_group and not _group_has_others(st.st_gid, euid)):
        try:
            gname = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            gname = str(st.st_gid)
        raise OwnerTrustError(
            f"{path} is writable by group {gname} (mode {oct(mode)}); fix: chmod g-w {path}")


def check_owner_dir(path, *, allow_private_group: bool = False) -> os.stat_result:
    """lstat `path`: a directory, not a symlink, owned by this euid, not
    writable by group or others. Returns the stat; raises OwnerTrustError
    (or OSError when it cannot be looked at)."""
    st = os.lstat(path)
    _check_stat(st, path, "dir", allow_private_group)
    return st


def check_owner_file(path, *, allow_private_group: bool = False) -> os.stat_result:
    """lstat `path`: a regular file, not a symlink, owned by this euid, not
    writable by group or others. Returns the stat; raises OwnerTrustError
    (or OSError when it cannot be looked at)."""
    st = os.lstat(path)
    _check_stat(st, path, "file", allow_private_group)
    return st


def _nofollow_error(path, e: OSError, kind: str) -> Exception:
    """A clear error for an O_NOFOLLOW open that failed."""
    if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
        try:
            st = os.lstat(path)
        except OSError:
            return e
        try:
            _check_stat(st, path, kind, True)
        except OwnerTrustError as err:
            return err
    return e


@contextmanager
def open_owner_dir(path, *, allow_private_group: bool = False):
    """Yield a descriptor for directory `path`, opened without following a
    final symlink and checked on the descriptor itself."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC)
    except OSError as e:
        raise _nofollow_error(path, e, "dir") from None
    try:
        _check_stat(os.fstat(fd), path, "dir", allow_private_group)
        yield fd
    finally:
        os.close(fd)


def _open_checked_file(name, shown, dir_fd, allow_private_group: bool) -> int:
    """Open `name` read-only without following a symlink, check it on the
    descriptor, and return the descriptor."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise OwnerTrustError(f"{shown} is a symlink; an owner process does not follow it") from None
        raise
    try:
        _check_stat(os.fstat(fd), shown, "file", allow_private_group)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def read_owner_file(path, *, allow_private_group: bool = False,
                    check_dir: bool = True) -> bytes:
    """The bytes of `path` after the owner checks on its directory (unless
    check_dir is False) and on the file, both made on the open descriptors.
    Raises OwnerTrustError, FileNotFoundError or another OSError."""
    p = Path(path)
    if not check_dir:
        fd = _open_checked_file(str(p), p, None, allow_private_group)
        try:
            return _read_fd(fd)
        finally:
            os.close(fd)
    with open_owner_dir(p.parent, allow_private_group=allow_private_group) as dfd:
        fd = _open_checked_file(p.name, p, dfd, allow_private_group)
        try:
            return _read_fd(fd)
        finally:
            os.close(fd)


def _check_existing(dfd: int, name: str, shown, allow_private_group: bool) -> None:
    """Refuse to replace an entry that fails the owner checks."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _check_stat(st, shown, "file", allow_private_group)


def write_owner_file(path, data, *, mode: int = 0o600, allow_private_group: bool = False,
                     gid: int | None = None) -> None:
    """Atomically replace `path` with `data` after the owner checks on its
    directory (on the open descriptor) and on any file already there. The
    new file is created O_EXCL|O_NOFOLLOW with a random name in the checked
    directory, set to `mode` (and group `gid` when given) before any byte is
    written, then renamed onto `path` relative to the same descriptor."""
    p = Path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    with open_owner_dir(p.parent, allow_private_group=allow_private_group) as dfd:
        _check_existing(dfd, p.name, p, allow_private_group)
        tmp = f".{p.name}.{_secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
                     0o600, dir_fd=dfd)
        try:
            try:
                if gid is not None:
                    os.fchown(fd, -1, gid)
                os.fchmod(fd, mode)
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, p.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise


def set_owner_file_mode(path, mode: int, *, gid: int | None = None,
                        allow_private_group: bool = False) -> None:
    """Set the mode (and group) of an existing `path` through a checked
    descriptor, so the change lands on the checked file and never on a
    symlink's target."""
    p = Path(path)
    with open_owner_dir(p.parent, allow_private_group=allow_private_group) as dfd:
        fd = _open_checked_file(p.name, p, dfd, allow_private_group)
        try:
            if gid is not None:
                os.fchown(fd, -1, gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)


# ── token file modes ──────────────────────────────────────────────────

# Plain-text tokens an owner process harvests into the auth directory (an
# owner plugin's browser harvest, a pasted token). JSON token stores keep
# the modes their own writers set.
HARVESTED_TOKEN_GLOB = "*token*.txt"


def _resident_reads_tokens() -> bool:
    """True only on an install without mail mediation, where the resident
    reads the tokens itself and needs group read. Under mediation the
    owner's mediator reads them, so no group bit is set. An unknown answer
    counts as mediated."""
    try:
        try:
            import mail_mediation
        except ModuleNotFoundError:  # imported as preprocessor.owner_trust
            from preprocessor import mail_mediation
        return not mail_mediation.mediation_active_for_resident()
    except Exception:
        return False


def _ea_gid() -> int | None:
    try:
        return grp.getgrnam("ea").gr_gid
    except KeyError:
        return None


def token_file_mode() -> tuple[int, int | None]:
    """(mode, group) for a token file an owner process writes: 0600 and no
    group change under mail mediation, 0640 group ea without it. Never
    group write: the owner checks refuse it when the group holds another
    user."""
    if _resident_reads_tokens():
        return 0o640, _ea_gid()
    return 0o600, None


def fix_token_modes(auth_dir) -> tuple[list[Path], list[str]]:
    """Apply token_file_mode() to every harvested token file in `auth_dir`
    (HARVESTED_TOKEN_GLOB). Only the mode and group change; no token is
    read. Returns (files changed, problems)."""
    mode, gid = token_file_mode()
    done, problems = [], []
    for path in sorted(Path(auth_dir).glob(HARVESTED_TOKEN_GLOB)):
        try:
            set_owner_file_mode(path, mode, gid=gid, allow_private_group=True)
            done.append(path)
        except (OSError, OwnerTrustError) as e:
            problems.append(f"{path}: {e}")
    return done, problems


# ── env files as data ─────────────────────────────────────────────────

_ENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str, keys=SECRET_KEYS) -> dict[str, str]:
    """KEY=VALUE lines as data: blank lines, comments and anything that is not
    an assignment are skipped, one pair of matching quotes around a value is
    removed, and only keys in `keys` are kept (None keeps every key). Nothing
    is expanded or executed. A later line wins over an earlier one."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.fullmatch(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if keys is not None and key not in keys:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def read_env_values(path, keys) -> dict[str, str]:
    """The allowlisted keys of a non-secret env file (such as ~/.ea/.env),
    read as data; {} when it is absent or unreadable. The caller decides
    which keys are safe to take from a file the resident can write."""
    try:
        return parse_env(Path(path).read_text(encoding="utf-8", errors="replace"), keys)
    except OSError:
        return {}


# Non-secret settings an owner process may take from the resident-owned
# ~/.ea/.env, read as data. None of them names a path or an interpreter
# variable; graph_client checks the authority and the redirect where it
# uses them.
DOTENV_SETTING_KEYS = frozenset({
    "EA_SLACK_CHANNEL_ID", "GRAPH_AUTH_METHOD", "GRAPH_CLIENT_ID",
    "GRAPH_SCOPES", "GRAPH_AUTHORITY", "GRAPH_REDIRECT_URI",
})


def load_owner_env(secret_names=SECRET_KEYS, setting_keys=DOTENV_SETTING_KEYS) -> None:
    """Fill os.environ, without overriding it, for an owner process: the
    secrets in `secret_names` by the owner_secret rule, then `setting_keys`
    from ~/.ea/.env as data."""
    for key, val in owner_secrets(secret_names).items():
        os.environ.setdefault(key, val)
    for key, val in read_env_values(ea_dir() / ".env", setting_keys).items():
        os.environ.setdefault(key, val)


def private_secrets() -> dict[str, str]:
    """The owner-private secrets file, checked and parsed; {} when this is
    not an owner process or the file is absent or refused."""
    if not is_owner_process():
        return {}
    p = private_dir() / SECRETS_FILE
    try:
        data = read_owner_file(p)
    except FileNotFoundError:
        return {}
    except (OwnerTrustError, OSError) as e:
        _warn_once(f"owner secrets file refused: {e}")
        return {}
    return parse_env(data.decode("utf-8", errors="replace"))


def legacy_secrets(path=None) -> dict[str, str]:
    """The legacy secrets file (default ~/.ea/.env.secrets), accepted only as
    a regular file, not a symlink, owned by this euid and not writable by
    group or others; {} otherwise."""
    p = Path(path) if path is not None else ea_dir() / LEGACY_SECRETS_FILE
    try:
        data = read_owner_file(p, check_dir=False)
    except FileNotFoundError:
        return {}
    except (OwnerTrustError, OSError) as e:
        if is_owner_process():
            _warn_once(f"legacy secrets file refused: {e}")
        return {}
    return parse_env(data.decode("utf-8", errors="replace"))


def owner_secret(name: str, *, env: Mapping[str, str] | None = None,
                 legacy=None) -> str:
    """One secret by the rule: `env` (default os.environ), then the private
    secrets.env, then the checked legacy file (`legacy`, default
    ~/.ea/.env.secrets). "" when none has it."""
    if name not in SECRET_KEYS:
        raise ValueError(f"{name} is not a known secret (owner_trust.SECRET_KEYS)")
    val = (os.environ if env is None else env).get(name, "")
    if val:
        return val
    val = private_secrets().get(name, "")
    if val:
        return val
    return legacy_secrets(legacy).get(name, "")


def owner_secrets(names=SECRET_KEYS, *, env: Mapping[str, str] | None = None,
                  legacy=None) -> dict[str, str]:
    """Every secret in `names` that the rule finds, as a dict."""
    env = os.environ if env is None else env
    found = {**legacy_secrets(legacy), **private_secrets()}
    out = {}
    for name in names:
        if name not in SECRET_KEYS:
            raise ValueError(f"{name} is not a known secret (owner_trust.SECRET_KEYS)")
        val = env.get(name, "") or found.get(name, "")
        if val:
            out[name] = val
    return out


# ── chat trust settings ───────────────────────────────────────────────

def owner_chat_block() -> dict | None:
    """The chat block from `<private_dir>/chat.json` for an owner process;
    None for any other process. Raises OwnerTrustError when the file is
    absent (naming the seed step), fails the checks or does not hold a JSON
    object: the owner never falls back to the resident's copy."""
    if not is_owner_process():
        return None
    p = private_dir() / CHAT_FILE
    try:
        data = read_owner_file(p)
    except FileNotFoundError:
        raise OwnerTrustError(
            f"{p} is missing. An owner process takes the chat settings (server, bot, "
            f"owner id) only from it, never from ea-config.json in ea-bot space. Create "
            f"it as the owner: python3 preprocessor/owner_trust.py seed --chat (the root "
            f"step deploy/harden-permissions.sh runs the seed)") from None
    try:
        block = json.loads(data)
    except ValueError as e:
        raise OwnerTrustError(f"{p} is not valid JSON: {e}") from None
    if not isinstance(block, dict):
        raise OwnerTrustError(f"{p} must hold the chat block as a JSON object")
    return block


# ── the owner's own name ──────────────────────────────────────────────

def owner_profile_name() -> str | None:
    """The display name in `<private_dir>/profile.json` for an owner
    process: the name poll_filler may put on an outside poll, which the
    outbound review checks each poll submission against. None for any
    other process, when the file is absent, or when it fails the checks or
    names no one."""
    if not is_owner_process():
        return None
    try:
        doc = json.loads(read_owner_file(private_dir() / PROFILE_FILE))
    except (OSError, OwnerTrustError, ValueError):
        return None
    name = doc.get("name") if isinstance(doc, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


# ── CLI ───────────────────────────────────────────────────────────────

def _seed_secrets(priv: Path, tree: Path) -> int:
    target = priv / SECRETS_FILE
    if target.exists():
        check_owner_file(target)
        print(f"  secrets: {target} exists, kept")
        return 0
    legacy = tree / LEGACY_SECRETS_FILE
    try:
        data = read_owner_file(legacy, check_dir=False)
    except FileNotFoundError:
        print(f"  secrets: no {legacy}; create {target} (mode 600) by hand if a secret is needed")
        return 0
    except OwnerTrustError as e:
        print(f"  secrets: REFUSED to copy {legacy}: {e}", file=sys.stderr)
        return 1
    write_owner_file(target, data, mode=0o600)
    names = sorted(parse_env(data.decode("utf-8", errors="replace"), keys=None))
    print(f"  secrets: {target} created from {legacy} (keys: {', '.join(names) or 'none'})")
    return 0


def _read_tree_json(path: Path) -> dict:
    """A JSON file in resident space, read as data and never through a
    symlink. Raises OSError or ValueError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        doc = json.loads(_read_fd(fd))
    finally:
        os.close(fd)
    return doc if isinstance(doc, dict) else {}


# The seed copies trust settings out of ea-bot space once. Every value is
# shown, and each one the owner side trusts is typed back by the operator
# (or passed on the command line) before the file is written. Tests replace
# these two.
_prompt = input


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _flat(block, prefix: str = "") -> list[tuple[str, str]]:
    """Every leaf of a JSON object as (dotted key, value), sorted."""
    out = []
    if isinstance(block, dict):
        for k in sorted(block, key=str):
            out += _flat(block[k], f"{prefix}{k}.")
    else:
        out.append((prefix[:-1], block if isinstance(block, str) else json.dumps(block)))
    return out


# What each trusted value is and what a wrong one would do, shown before the
# operator is asked to type it back. A notify.<key>.stream key uses "notify".
CONFIRM_HELP = {
    "frontend": ("the chat platform the bot uses (web, zulip or slack)",
                 "a wrong one points the bot at another platform"),
    "site": ("the address of your chat server",
             "the bot sends its chat API key there, so a wrong one hands the key to that server"),
    "bot_email": ("the bot's own account on the chat server",
                  "the bot signs in as this account; a wrong one is another account's identity"),
    "owner_user_id": ("your own user id on the chat server",
                      "only this id can approve a Send, Create or Publish card; a wrong one "
                      "lets that account approve"),
    "stream": ("the EA channel where you talk to the EA",
               "the bot takes instructions and posts replies there"),
    "sched_stream": ("the scheduling channel",
                     "scheduling replies and proposals are posted there"),
    "dm_stream": ("your private stream for confirmation cards",
                  "every card, with the full draft text, is posted there; a wrong one shows "
                  "your drafts to whoever reads that stream"),
    "notify": ("a channel the owner-side jobs post notices to",
               "health and digest notices go there"),
    "web.public_url": ("the address you open the web chat at: your tailnet host and the "
                       "chat's own HTTPS port",
                       "the bot answers only requests made to this address, so a wrong one "
                       "lets a page at that address reach your chat"),
    "web.port": ("the loopback port the bot serves the web chat on",
                 "your `tailscale serve` line forwards to it; a wrong one serves the chat "
                 "where another program expects its own port"),
    "web.tailscale_login": ("your Tailscale login, as `tailscale whois` shows it",
                            "a request from this login is you: it reads every draft and "
                            "presses Send, so a wrong one gives that to another login"),
    "web.pair_token_file": ("the file that holds the web chat's pairing token",
                            "whoever reads it can pair a browser as you, so it must stay in "
                            "your private directory"),
    "ea_managed_calendar_id": ("the EA-Managed calendar: the one calendar the EA may write "
                               "without asking you",
                               "your own cron copies your schedule there every 30 minutes, so a "
                               "wrong one sends your schedule to another calendar"),
    "ea_managed_account": ("the Google account that holds the EA-Managed calendar",
                           "that account's sign-in is used for those writes"),
    "name": ("the only name the EA may put on a Doodle or When2Meet poll",
             "a wrong one lets polls be filled in under another name"),
}


def _help(key: str) -> tuple[str, str] | None:
    return CONFIRM_HELP.get("notify" if key.startswith("notify.") else key)


def _confirm(what: str, shown: list[tuple[str, str]],
             must: list[tuple[str, str, str]], given: dict) -> dict | None:
    """Show every value in `shown`; for each (key, value, flag) in `must`,
    take the value passed on the command line (`given[key]`), or explain the
    value and have the operator type the shown value back. Returns {key:
    confirmed value}, or None (with the reason on stderr) when a value is
    not confirmed."""
    print(f"  {what}: these values were copied from the EA's own config, which the EA")
    print("  can edit. Your processes will trust them from now on, so check that each")
    print("  is yours. Type a value back exactly to confirm it; press Enter to stop")
    print("  without writing anything.")
    for key, val in shown:
        print(f"    {key} = {val}")
    out: dict = {}
    for key, val, flag in must:
        if given.get(key) is not None:
            out[key] = str(given[key]).strip()
            note = "" if out[key] == val else " (not the value shown above)"
            print(f"    {key}: {out[key]!r} from {flag}{note}")
            continue
        h = _help(key)
        if h and _interactive():
            print(f"\n  {key}: {h[0]}.")
            print(f"    Why it matters: {h[1]}.")
        if not _interactive():
            print(f"  {what}: NOT written. Confirm each trusted value: run the seed at a "
                  f"terminal and type it back, or pass "
                  f"{' '.join(f for _, _, f in must)}", file=sys.stderr)
            return None
        try:
            typed = _prompt(f"  type {key} to confirm it is yours ({val!r}): ").strip()
        except EOFError:
            typed = ""
        if not typed or typed != val:
            print(f"  {what}: NOT written: {key} was not confirmed. Pass {flag} with the "
                  f"right value, or fix ea-bot's copy and seed again", file=sys.stderr)
            return None
        out[key] = typed
    return out


def _seed_calendar(priv: Path, tree: Path, *, required: bool = True,
                   given: dict | None = None) -> int:
    """calendars.json: the EA-Managed calendar id the mediator lets the
    resident write without a button, and the Google account that holds it.
    Copied once, by the owner, from what the installer put in
    scheduling-preferences.json; after that only this file is read on the
    owner side (mail_mediation.owner_ea_managed_calendar_id,
    google_calendar_client.ea_managed_target). A file from before the
    account key gets the account added; its calendar id is kept."""
    target = priv / CALENDARS_FILE
    if target.exists():
        doc = json.loads(read_owner_file(target))
        print(f"  calendar: {target} exists, kept")
        if isinstance(doc, dict) and doc.get("ea_managed_calendar_id") \
                and not doc.get("ea_managed_account"):
            # A file seeded before the account key: add the account only.
            # The calendar id stays the owner's.
            try:
                ea = _read_tree_json(tree / "config" / "scheduling-preferences.json").get(
                    "ea_managed_calendar") or {}
            except (OSError, ValueError):
                ea = {}
            acct = str((ea or {}).get("email") or "").strip() if isinstance(ea, dict) else ""
            given_acct = (given or {}).get("ea_managed_account")
            ok = None
            if acct or given_acct is not None:
                ok = _confirm("calendar account", [("ea_managed_account", acct)],
                              [("ea_managed_account", acct, "--calendar-account")],
                              {"ea_managed_account": given_acct})
            if ok and ok.get("ea_managed_account"):
                doc["ea_managed_account"] = ok["ea_managed_account"]
                write_owner_file(target, json.dumps(doc, indent=2) + "\n", mode=0o600)
                print(f"  calendar: added ea_managed_account to {target}")
    else:
        given = dict(given or {})
        flag_id = given.get("ea_managed_calendar_id")
        conf = tree / "config" / "scheduling-preferences.json"
        cal, ea = "", {}
        try:
            ea = _read_tree_json(conf).get("ea_managed_calendar") or {}
            cal = str((ea or {}).get("calendar_id") or "").strip() if isinstance(ea, dict) else ""
        except (OSError, ValueError) as e:
            if flag_id is None:
                print(f"  calendar: cannot read {conf}: {e}",
                      file=sys.stderr if required else sys.stdout)
                return 1 if required else 0
        if not cal and flag_id is None:
            print(f"  calendar: no ea_managed_calendar.calendar_id in {conf}; "
                  f"docs/SETUP.md 'The EA-Managed calendar', then `owner_trust.py seed --calendar`",
                  file=sys.stderr if required else sys.stdout)
            return 1 if required else 0
        acct = str((ea or {}).get("email") or "").strip() if isinstance(ea, dict) else ""
        must = [("ea_managed_calendar_id", cal, "--calendar-id")]
        if acct or given.get("ea_managed_account") is not None:
            must.append(("ea_managed_account", acct, "--calendar-account"))
        ok = _confirm("calendar", [("ea_managed_calendar_id", cal), ("ea_managed_account", acct)],
                      must, given)
        if ok is None or not ok["ea_managed_calendar_id"]:
            return 1 if required else 0
        doc = {"ea_managed_calendar_id": ok["ea_managed_calendar_id"]}
        if ok.get("ea_managed_account"):
            doc["ea_managed_account"] = ok["ea_managed_account"]
        write_owner_file(target, json.dumps(doc, indent=2) + "\n", mode=0o600)
        print(f"  calendar: {target} created")
    print("  CONFIRM this is the EA-Managed calendar you created (the mediator lets the")
    print("  resident write this calendar, and only this one, without a button):")
    print(f"    ea_managed_calendar_id = {doc.get('ea_managed_calendar_id', '')}")
    print(f"    ea_managed_account     = {doc.get('ea_managed_account', '')}")
    print(f"  If it is not, fix {target}.")
    return 0


# The chat keys the owner side trusts: (key in the confirm step, path in the
# block, command-line flag). The zulip ones apply only to a zulip frontend.
CHAT_TRUSTED = (("frontend", ("frontend",), "--chat-frontend"),
                ("site", ("zulip", "site"), "--chat-site"),
                ("bot_email", ("zulip", "bot_email"), "--chat-bot-email"),
                ("owner_user_id", ("zulip", "owner_user_id"), "--chat-owner-id"))

# The zulip routing keys: where the bot posts briefings, drafts and every
# confirmation card (the DM stream gets each card with its full body). A
# set one is confirmed like a trusted key; each notify target's stream is
# confirmed as `notify.<key>.stream`, passed as --chat-notify <key>=<stream>.
CHAT_ROUTING = (("stream", "--chat-stream"),
                ("sched_stream", "--chat-sched-stream"),
                ("dm_stream", "--chat-dm-stream"))


# The web chat keys (docs/webchat.md, Config): (key in the confirm step,
# key in the `web` block, flag). They decide which address the bot answers
# to, who signs in and where the pairing token lives, so each one set in
# ea-bot's copy or passed with its flag is confirmed like the server; the
# store and the token file must lie outside the resident's tree.
CHAT_WEB = (("web.public_url", "public_url", "--chat-public-url"),
            ("web.port", "port", "--chat-port"),
            ("web.tailscale_login", "tailscale_login", "--chat-tailscale-login"),
            ("web.pair_token_file", "pair_token_file", "--chat-pair-token-file"))
WEB_DEFAULT_PORT = 8470


def runs_web(block: dict, frontend: str | None = None) -> bool:
    """True when the chat block runs the web chat, alone or as the shadow."""
    return (frontend or block.get("frontend")) == "web" or block.get("shadow") == "web"


def _web_must(web: dict, given: dict) -> list[tuple[str, str, str]]:
    """The web values to confirm: each one set in ea-bot's copy or passed
    on the command line."""
    must = []
    for key, wkey, flag in CHAT_WEB:
        val = web.get(wkey)
        val = "" if val is None else str(val)
        if val or given.get(key) is not None:
            must.append((key, val, flag))
    return must


def web_block_refusal(web: dict, tree=None) -> str | None:
    """Why the owner side may not take this `web` block, or None: a port
    that is not one, or a store or pairing token file that is relative or
    lies in the resident's tree (`resident_roots`), where the bot refuses
    to serve the web chat."""
    port = web.get("port")
    if port not in (None, "") and not (str(port).isdigit() and 0 < int(port) < 65536):
        return f"web.port {port!r} is not a port number"
    roots = resident_roots(*((tree,) if tree else ()))
    for wkey in ("store_path", "pair_token_file"):
        val = str(web.get(wkey) or "").strip()
        if not val:
            continue
        if not os.path.isabs(val):
            return f"web.{wkey} {val!r} is not an absolute path"
        root = inside_roots(val, roots)
        if root is not None:
            return (f"web.{wkey} {val} is inside {root}, which the resident controls; the "
                    f"bot does not serve the web chat from there. Leave it empty (the "
                    f"owner-private directory) or name a path outside that tree")
    return None


def _notify_flag(nkey: str) -> str:
    return f"--chat-notify {nkey}=<stream>"


def parse_notify_flags(values) -> dict:
    """--chat-notify <key>=<stream> values as {"notify.<key>.stream": stream}.
    Raises ValueError for a value without a key."""
    out = {}
    for raw in values or ():
        nkey, sep, stream = str(raw).partition("=")
        if not sep or not nkey.strip():
            raise ValueError(f"--chat-notify takes <key>=<stream>, not {raw!r}")
        out[f"notify.{nkey.strip()}.stream"] = stream.strip()
    return out


def _routing_must(z: dict, given: dict) -> list[tuple[str, str, str]]:
    """The routing values to confirm: each routing key and notify stream
    that is set in ea-bot's copy or passed on the command line."""
    must = []
    for key, flag in CHAT_ROUTING:
        val = str(z.get(key) or "")
        if val or given.get(key) is not None:
            must.append((key, val, flag))
    notify = z.get("notify") if isinstance(z.get("notify"), dict) else {}
    keys = {k for k, t in notify.items() if isinstance(t, dict) and t.get("stream")}
    keys |= {k[len("notify."):-len(".stream")] for k in given
             if k.startswith("notify.") and k.endswith(".stream") and given[k] is not None}
    for nkey in sorted(keys, key=str):
        t = notify.get(nkey) if isinstance(notify.get(nkey), dict) else {}
        must.append((f"notify.{nkey}.stream", str(t.get("stream") or ""), _notify_flag(nkey)))
    return must


def _seed_chat(priv: Path, tree: Path, given: dict | None = None) -> int:
    target = priv / CHAT_FILE
    slack_owner = read_env_values(tree / ".env", ("EA_SLACK_OWNER_ID",)).get(
        "EA_SLACK_OWNER_ID", "")
    if target.exists():
        block = json.loads(read_owner_file(target))
        print(f"  chat: {target} exists, kept")
        s = block.get("slack") if isinstance(block.get("slack"), dict) else {}
        if block.get("frontend", "slack") == "slack" and not s.get("owner_user_id"):
            # A chat.json from before the Slack owner id moved here.
            ok = _confirm("chat", [("EA_SLACK_OWNER_ID", slack_owner)],
                          [("owner_user_id", slack_owner, "--chat-owner-id")], given or {})
            if ok is None:
                return 1
            block["slack"] = {**s, "owner_user_id": ok["owner_user_id"]}
            write_owner_file(target, json.dumps(block, indent=2) + "\n", mode=0o600)
            print(f"  chat: slack.owner_user_id added to {target}")
    else:
        given = dict(given or {})
        conf = tree / "config" / "ea-config.json"
        try:
            doc = _read_tree_json(conf)
        except (OSError, ValueError) as e:
            print(f"  chat: cannot read the chat block from {conf}: {e}", file=sys.stderr)
            return 1
        block = doc.get("chat") if isinstance(doc, dict) else None
        if not isinstance(block, dict):
            block = {}
        z = block.get("zulip") if isinstance(block.get("zulip"), dict) else {}
        frontend = str(given.get("frontend") or block.get("frontend") or "slack")
        must = []
        for key, path, flag in CHAT_TRUSTED:
            if path[0] == "zulip" and frontend != "zulip":
                continue
            src = block if len(path) == 1 else z
            must.append((key, str(src.get(path[-1], "slack" if key == "frontend" else "")), flag))
        if frontend == "slack":
            must.append(("owner_user_id", slack_owner, "--chat-owner-id"))
        if frontend == "zulip":
            must += _routing_must(z, given)
        web = block.get("web") if isinstance(block.get("web"), dict) else {}
        if runs_web(block, frontend):
            # The values that would be written: ea-bot's copy, or the flag.
            planned = {**web, **{wkey: given[key] for key, wkey, _ in CHAT_WEB
                                 if given.get(key) is not None}}
            why = web_block_refusal(planned, tree)
            if why:
                print(f"  chat: NOT written: {why}", file=sys.stderr)
                return 1
            must += _web_must(web, given)
        ok = _confirm("chat", _flat(block) or [("frontend", "slack")], must, given)
        if ok is None:
            return 1
        block = json.loads(json.dumps(block))
        for key, path, _ in CHAT_TRUSTED:
            if key not in ok:
                continue
            if len(path) == 1:
                block[path[0]] = ok[key]
            else:
                section = "slack" if frontend == "slack" else path[0]
                if not isinstance(block.get(section), dict):
                    block[section] = {}
                block[section][path[1]] = ok[key]
        for key, val in ok.items():
            if key in dict(CHAT_ROUTING):
                block.setdefault("zulip", {})[key] = val
            elif key.startswith("notify.") and key.endswith(".stream"):
                nkey = key[len("notify."):-len(".stream")]
                notify = block.setdefault("zulip", {}).setdefault("notify", {})
                if not isinstance(notify.get(nkey), dict):
                    notify[nkey] = {}
                notify[nkey]["stream"] = val
        for key, wkey, _ in CHAT_WEB:
            if key in ok:
                if not isinstance(block.get("web"), dict):
                    block["web"] = {}
                val = ok[key]
                block["web"][wkey] = int(val) if wkey == "port" and val.isdigit() else val
        write_owner_file(target, json.dumps(block, indent=2) + "\n", mode=0o600)
        print(f"  chat: {target} created from the confirmed values")
    z = block.get("zulip") if isinstance(block.get("zulip"), dict) else {}
    print("  CONFIRM these are yours (the owner bot now trusts only this file):")
    print(f"    frontend      = {block.get('frontend', 'slack')}")
    print(f"    site          = {z.get('site', '')}")
    print(f"    bot_email     = {z.get('bot_email', '')}")
    print(f"    owner_user_id = {z.get('owner_user_id', '')}")
    s = block.get("slack") if isinstance(block.get("slack"), dict) else {}
    print(f"    slack.owner_user_id = {s.get('owner_user_id', '')}")
    for key, _ in CHAT_ROUTING:
        print(f"    {key:<13} = {z.get(key, '')}")
    notify = z.get("notify") if isinstance(z.get("notify"), dict) else {}
    for nkey in sorted(notify, key=str):
        t = notify[nkey] if isinstance(notify[nkey], dict) else {}
        print(f"    notify.{nkey}.stream = {t.get('stream', '')}")
    if runs_web(block):
        w = block.get("web") if isinstance(block.get("web"), dict) else {}
        print(f"    shadow        = {block.get('shadow', '')}")
        print(f"    web.public_url = {w.get('public_url', '')}")
        print(f"    web.port      = {w.get('port') or WEB_DEFAULT_PORT}")
        print(f"    web.tailscale_login = {w.get('tailscale_login', '')}")
        print(f"    web.pair_token_file = "
              f"{w.get('pair_token_file') or '(the default, beside the store)'}")
        why = web_block_refusal(w, tree)
        if why:
            print(f"  WARNING: {why}")
    print(f"  If any value is not yours, fix {target} and restart the bot.")
    return 0


def _seed_profile(priv: Path, tree: Path, given_name: str | None = None, *,
                  required: bool = False) -> int:
    """profile.json: the user's display name, copied once by the owner from
    user-profile.json. The outbound review checks each poll submission's
    name against it; ea-bot can rewrite its own copy, not this one, so the
    name is typed back by the operator or passed with --profile-name. An
    unconfirmed name writes nothing; it fails only `seed --profile`, so the
    bare seed of the root step notes it and goes on."""
    target = priv / PROFILE_FILE
    if target.exists():
        doc = json.loads(read_owner_file(target))
        print(f"  profile: {target} exists, kept")
    else:
        conf = tree / "config" / "user-profile.json"
        name = ""
        try:
            name = str(_read_tree_json(conf).get("name") or "").strip()
        except (OSError, ValueError) as e:
            if given_name is None:
                print(f"  profile: cannot read {conf}: {e}")
                return 0
        if not name and given_name is None:
            print(f"  profile: no name in {conf}; poll submissions are not checked owner-side")
            return 0
        ok = _confirm("profile", [("name", name)], [("name", name, "--profile-name")],
                      {"name": given_name})
        if ok is None or not ok.get("name"):
            print("  profile: profile.json not written; poll submissions are not checked "
                  "owner-side until `owner_trust.py seed --profile` is confirmed")
            return 1 if required else 0
        doc = {"name": ok["name"]}
        write_owner_file(target, json.dumps(doc, indent=2) + "\n", mode=0o600)
        print(f"  profile: {target} created from {conf}")
    print("  CONFIRM this is your name (the only name the EA may put on an outside poll):")
    print(f"    name = {doc.get('name', '') if isinstance(doc, dict) else ''}")
    print(f"  If it is not, fix {target}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="owner trust checks and the private-dir seed")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("secret", help="print one secret for capture (exit 1 if none)")
    p.add_argument("name", choices=sorted(SECRET_KEYS))
    p = sub.add_parser("seed", help="create secrets.env, chat.json, calendars.json and "
                                    "profile.json in the private dir when missing")
    p.add_argument("--ea-dir", default=None, help="the EA tree (default: EA_DIR or ~/.ea)")
    p.add_argument("--secrets", action="store_true", help="only secrets.env")
    p.add_argument("--chat", action="store_true", help="only chat.json")
    p.add_argument("--calendar", action="store_true", help="only calendars.json")
    p.add_argument("--profile", action="store_true", help="only profile.json")
    for key, _, flag in CHAT_TRUSTED:
        p.add_argument(flag, dest=f"chat_{key}", default=None,
                       help=f"the chat {key} to write, confirmed without a prompt")
    for key, flag in CHAT_ROUTING:
        p.add_argument(flag, dest=f"chat_{key}", default=None,
                       help=f"the zulip {key} to write, confirmed without a prompt")
    p.add_argument("--chat-notify", dest="chat_notify", action="append", default=[],
                   metavar="KEY=STREAM",
                   help="a zulip notify target's stream to write, confirmed without a "
                        "prompt (repeat for each key)")
    for key, wkey, flag in CHAT_WEB:
        p.add_argument(flag, dest=f"chat_web_{wkey}", default=None,
                       help=f"the web chat's {wkey} to write, confirmed without a prompt")
    p.add_argument("--profile-name", dest="profile_name", default=None,
                   help="the display name for profile.json, confirmed without a prompt")
    p.add_argument("--calendar-id", dest="calendar_id", default=None,
                   help="the EA-Managed calendar id to write, confirmed without a prompt")
    p.add_argument("--calendar-account", dest="calendar_account", default=None,
                   help="the Google account that holds it, confirmed without a prompt")
    p = sub.add_parser("token-modes", help="set every harvested token file in the auth "
                                           "directory to the mail mediation rule")
    p.add_argument("--ea-dir", default=None, help="the EA tree (default: EA_DIR or ~/.ea)")
    a = ap.parse_args(argv)
    if a.cmd == "secret":
        val = owner_secret(a.name)
        if not val:
            return 1
        sys.stdout.write(val + "\n")
        return 0
    if a.cmd == "token-modes":
        auth = (Path(a.ea_dir) if a.ea_dir else ea_dir()) / "auth"
        done, problems = fix_token_modes(auth)
        mode, gid = token_file_mode()
        for path in done:
            print(f"  {path}: mode {oct(mode)}"
                  f"{' group ea (no mail mediation)' if gid is not None else ''}")
        for line in problems:
            print(f"  REFUSED {line}", file=sys.stderr)
        if not done and not problems:
            print(f"  no harvested token file in {auth}")
        return 1 if problems else 0
    priv, tree = private_dir(), Path(a.ea_dir) if a.ea_dir else ea_dir()
    try:
        priv.mkdir(mode=0o700, exist_ok=True)
        check_owner_dir(priv)
    except (OSError, OwnerTrustError) as e:
        print(f"  private dir {priv}: {e}", file=sys.stderr)
        return 1
    every = not (a.secrets or a.chat or a.calendar or a.profile)
    rc = 0
    try:
        if a.secrets or every:
            rc |= _seed_secrets(priv, tree)
        if a.chat or every:
            given = {key: getattr(a, f"chat_{key}") for key, _, _ in CHAT_TRUSTED}
            given.update({key: getattr(a, f"chat_{key}") for key, _ in CHAT_ROUTING})
            given.update(parse_notify_flags(a.chat_notify))
            given.update({key: getattr(a, f"chat_web_{wkey}") for key, wkey, _ in CHAT_WEB})
            rc |= _seed_chat(priv, tree, given)
        if a.calendar or every:
            # The root step runs the bare seed before step 4d may be done;
            # a missing id is then a note, not a failure.
            rc |= _seed_calendar(priv, tree, required=bool(a.calendar),
                                 given={"ea_managed_calendar_id": a.calendar_id,
                                        "ea_managed_account": a.calendar_account})
        if a.profile or every:
            rc |= _seed_profile(priv, tree, a.profile_name, required=bool(a.profile))
    except (OSError, OwnerTrustError, ValueError) as e:
        print(f"  seed failed: {e}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
