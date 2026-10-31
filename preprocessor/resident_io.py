#!/usr/bin/env python3
"""Owner file access in the resident's space, made with the resident's access.

The resident (`ea-bot`) runs the LLM and is untrusted. Its home, `~/.ea`
included, is resident space: the resident owns the directories there, so it
can put a symlink or a hard link at any name in them. An owner process (the
bot, the dashboard, the crons) that appends to, rewrites, creates or renames
an entry there by path can be steered by such a link onto a file only the
owner may change. The rule (docs/SECURITY.md, "Owner trust rules"): an owner
process writes into resident space only with the resident's own access. This
module is that rule for the Python writers:

  append_text(path, text)            add text at the end of a file
  replace_text(path, text, mode)     replace a file atomically with text
  replace_bytes(path, data, mode)    the same with bytes
  update_text(path, fn, mode)        read-modify-write in place under an
                                     exclusive lock; fn(old) returns the new
                                     text, or None to keep the file
  make_dir(path, mode)               create a directory (and its missing
                                     parents); an existing one is kept as is
  ensure_file(path)                  create an empty file when missing (a
                                     lock file); an existing one is kept
  move(src, dst)                     rename an entry within one top-level
                                     directory of resident space
  read_bytes(path)                   the bytes of a file, or None
  read_text(path)                    the text (UTF-8) of a file, or None
  list_dir(path)                     [(kind, name)] of a directory's entries
                                     (kind d, f, l or o; no link followed),
                                     or None

An owner process reads a file in resident space with these whenever what it
reads flows back to a place the resident can read (a read-modify-write of a
state file), so a link at the name cannot hand it a file only the owner may
read.

Every write call returns True when it is done, or prints one stderr line
and returns False; none raises, except that an exception raised by an
update_text `fn` leaves the file as it was and propagates. The read calls
return None for a missing file (silently) and for any failure (with one
stderr line). Files a call creates get umask 002 (mode 664), like the rest
of ~/.ea. The final name of a path is never followed: a symlink there is
refused (append, update, ensure_file, make_dir, read_*) or replaced
(replace_*, move). A file that is not a single-linked regular file is
refused by append, update, ensure_file and read_*.

In an owner process (owner_trust.is_owner_process()) each call runs one
isolated child as the resident: `sudo -n -u ea-bot /usr/bin/python3 -I
<program> <op> <mode> <path> [<path>]`, started in / with CHILD_ENV only, the
bytes on stdin. The program is CHILD_FILE, the child's own file in the owner
services' tree (/opt/ea-system on a hardened install), when this is the
install's owner process and that file is one the resident can read, that
only root or this user can change, and that holds the program below after
comment lines only; sudo then logs a short command line. Otherwise it is
`-c <_CHILD>`, the same program inline, which needs no file the resident
can read. Either way the child depends on no file the resident can change.
The owner resolves the directory part of each path first (the
owner's `~/.ea` is a symlink the resident cannot traverse); the final name
stays as given.

Whether the child is possible is decided once per process by a preflight
the resident cannot influence: a sudo list-mode check asks the root-owned
policy whether the owner may run the child as the resident, and runs
nothing as the resident. When the policy refuses the file form, the inline
form is asked for next, and a yes keeps the inline form for the process.
Only a failed preflight selects the direct call
(append, update and the reads with O_NOFOLLOW, replace_text through
atomic_io, replace_bytes by the same design), with one warning per process.
After a passing preflight, any child outcome other than a clean framed
reply (a non-zero exit, missing, short or malformed output, a timeout, a
child that does not start) is a failure for that call: the child runs with
the resident's identity, so its behaviour never selects the direct call.
REFUSED_MARK only makes a child's message readable; it carries no
authority.

In any other process (the resident itself, a test with a scratch EA_DIR)
the call is made directly, the same way as the fallback.

move() refuses a destination outside the source's top-level shared
directory: the first directory below the EA tree (EA_DIR, as resolved) or
below the resident's home that holds the source, or, for an entry directly
in one of those, that same directory. The check runs in this process on the
resolved directories, before the child starts.

Seams for tests (module attributes): `_owner_check` () -> bool,
`_availability` () -> bool, `_spawn` (argv) -> a Popen-like object,
`_child_file` () -> the program file's path, or None for the inline form.
"""
from __future__ import annotations

import fcntl
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import threading

try:
    import atomic_io as _atomic
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic

RESIDENT_USER = "ea-bot"
OK = b"RESIDENT-IO-OK "
REFUSED_MARK = "RESIDENT-IO-REFUSED:"
PROBE_TIMEOUT_S = 15
CHILD_TIMEOUT_S = 30
MAX_UPDATE_BYTES = 16 * 1024 * 1024
MAX_READ_BYTES = 128 * 1024 * 1024   # the child's MAX_READ says the same
MISSING = b"RESIDENT-IO-MISSING\n"
# The whole environment sudo and the child start with: no owner variable,
# secret or setting reaches the resident's process.
CHILD_ENV = {"PATH": "/usr/bin:/bin"}

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC

# The child program as its own file, in the tree the owner's services run
# from. preprocessor/resident_io_child.py is `_CHILD` after a comment header;
# tests/test_resident_io_child_file.py keeps the two the same, and the owner
# names the file only when it holds this module's `_CHILD`, so a tree at
# another commit falls back to the inline form.
CHILD_FILE = "/opt/ea-system/preprocessor/resident_io_child.py"
MAX_CHILD_FILE_BYTES = 1024 * 1024

# The program the child runs as the resident. It imports only the standard
# library. argv: <op> <octal mode> <absolute path> [<absolute path>].
#   append   stdin to the end of the file
#   replace  stdin to a random temp name in the same directory, renamed over
#            the file
#   update   lock, send the old bytes framed, read "WRITE <n>\n" and n bytes,
#            rewrite in place; any other request keeps the file
#   mkdir    create the directory and its missing parents; a new leaf gets
#            the mode through its own descriptor
#   touch    create the file when missing
#   move     rename the first path onto the second
#   read     send the file's bytes framed, or MISSING when there is none
#   list     send the directory's entries framed, each a kind letter (d, f,
#            l, o) and the name, NUL separated; MISSING when there is none
# A clean reply is `RESIDENT-IO-OK <n>\n` (n bytes written, or 0), followed
# by the n bytes for update, read and list.
_CHILD = r'''
import fcntl, os, stat, sys, tempfile
OK = b"RESIDENT-IO-OK "
MARK = "RESIDENT-IO-REFUSED:"
MISSING = b"RESIDENT-IO-MISSING\n"
MAX_READ = 134217728
FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def plain(fd, path):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(path + " is not a plain single-linked file")


def put(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def take(fd):
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def reply(n, payload=b""):
    sys.stdout.buffer.write(OK + str(n).encode() + b"\n" + payload)
    sys.stdout.buffer.flush()


def parent_of(path):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    return parent


def main(op, mode, paths):
    os.umask(0o002)
    path = paths[0]
    if op == "append":
        data = sys.stdin.buffer.read()
        parent_of(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
            put(fd, data)
        finally:
            os.close(fd)
        reply(len(data))
    elif op == "replace":
        data = sys.stdin.buffer.read()
        fd, tmp = tempfile.mkstemp(dir=parent_of(path),
                                   prefix="." + os.path.basename(path) + ".", suffix=".tmp")
        try:
            try:
                os.fchmod(fd, mode)
                put(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        reply(len(data))
    elif op == "update":
        parent_of(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            old = take(fd)
            reply(len(old), old)
            head = sys.stdin.buffer.readline(64)
            if head.startswith(b"WRITE ") and head[6:].strip().isdigit():
                n = int(head[6:])
                data = sys.stdin.buffer.read(n)
                if len(data) != n:
                    raise OSError("the new content arrived short")
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                put(fd, data)
                os.fsync(fd)
                try:
                    os.fchmod(fd, mode)
                except OSError:
                    pass
                reply(len(data))
        finally:
            os.close(fd)
    elif op == "mkdir":
        parent_of(path)
        try:
            os.mkdir(path, 0o700)
            made = True
        except FileExistsError:
            made = False
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | FLAGS)
        try:
            if made:
                os.fchmod(fd, mode)
        finally:
            os.close(fd)
        reply(0)
    elif op == "touch":
        parent_of(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | FLAGS, 0o666)
        try:
            plain(fd, path)
        finally:
            os.close(fd)
        reply(0)
    elif op == "move":
        os.rename(path, paths[1])
        reply(0)
    elif op == "read":
        try:
            fd = os.open(path, os.O_RDONLY | FLAGS)
        except FileNotFoundError:
            sys.stdout.buffer.write(MISSING)
            sys.stdout.buffer.flush()
            return
        try:
            plain(fd, path)
            if os.fstat(fd).st_size > MAX_READ:
                raise OSError(path + " is larger than " + str(MAX_READ) + " bytes")
            data = take(fd)
        finally:
            os.close(fd)
        if len(data) > MAX_READ:
            raise OSError(path + " is larger than " + str(MAX_READ) + " bytes")
        reply(len(data), data)
    elif op == "list":
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | FLAGS)
        except FileNotFoundError:
            sys.stdout.buffer.write(MISSING)
            sys.stdout.buffer.flush()
            return
        try:
            rows = []
            for name in sorted(os.listdir(fd)):
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = ("d" if stat.S_ISDIR(st.st_mode) else
                        "f" if stat.S_ISREG(st.st_mode) else
                        "l" if stat.S_ISLNK(st.st_mode) else "o")
                rows.append(kind + name)
        finally:
            os.close(fd)
        data = "\0".join(rows).encode("utf-8", "surrogateescape")
        if len(data) > MAX_READ:
            raise OSError(path + " lists more than " + str(MAX_READ) + " bytes")
        reply(len(data), data)
    else:
        raise OSError("unknown op " + repr(op))


try:
    args = sys.argv[1:]
    want = 4 if args[:1] == ["move"] else 3
    if len(args) != want or not all(os.path.isabs(p) for p in args[2:]):
        raise OSError("usage: <op> <octal mode> <absolute path> [<absolute path>]")
    main(args[0], int(args[1], 8), args[2:])
except Exception as e:
    sys.stderr.write(MARK + " " + str(e))
    sys.exit(4)
'''

_RESIDENT_OK: bool | None = None   # preflight result, cached per process
_CHILD_FILE_OK: dict[str, bool] = {}   # _child_file_usable per path, per process
_INLINE_ONLY = False               # the policy allows only the inline form
_WARNED: set[str] = set()


class _ChildError(Exception):
    """The child did not give a clean framed reply."""


class _FnRaised(Exception):
    """Carries an exception raised by an update_text `fn`."""

    def __init__(self, exc: BaseException):
        super().__init__(str(exc))
        self.exc = exc


# ── decisions ─────────────────────────────────────────────────────────

def _owner_trust():
    try:
        import owner_trust
    except ModuleNotFoundError:
        from preprocessor import owner_trust
    return owner_trust


def _default_owner_check() -> bool:
    return _owner_trust().is_owner_process()


def _resident_python() -> str:
    """The interpreter for the child: the system python3, which the resident
    can execute (the owner's venv lives under the owner's home)."""
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"


def _sudo() -> str:
    return "/usr/bin/sudo" if os.path.exists("/usr/bin/sudo") else "sudo"


def _resident_ids() -> tuple[int, set[int]]:
    """The resident's uid and every group it is in; KeyError without it."""
    pw = pwd.getpwnam(RESIDENT_USER)
    return pw.pw_uid, set(os.getgrouplist(RESIDENT_USER, pw.pw_gid))


def _grants(st: os.stat_result, uid: int, gids: set[int], bit: int) -> bool:
    """Whether the mode bits give `bit` (4 read, 1 search) to that user."""
    if st.st_uid == uid:
        return bool(st.st_mode & (bit << 6))
    if st.st_gid in gids:
        return bool(st.st_mode & (bit << 3))
    return bool(st.st_mode & bit)


def _fixed_for(fd: int, st: os.stat_result, uid: int, gids: set[int], bit: int) -> bool:
    """A component the resident may use (`bit`) and only root or this user
    may change: owned by one of them, writable by no group and no other
    user, no ACL (mode bits then tell the whole answer), `bit` granted."""
    # Linux keeps a POSIX ACL in an extended attribute; macOS has no
    # os.listxattr, and its ACLs are not read here (as in boundary-audit.py).
    listxattr = getattr(os, "listxattr", None)
    try:
        acl = bool(listxattr) and "system.posix_acl_access" in listxattr(fd)
    except OSError:
        acl = False
    return (st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022
            and not acl and _grants(st, uid, gids, bit))


def _holds_program(data: bytes) -> bool:
    """True when `data` is `_CHILD` after comment lines only."""
    body = _CHILD.encode("utf-8")
    if not data.endswith(body):
        return False
    return all(line.startswith(b"#") for line in data[:-len(body)].splitlines())


def _child_file_usable(path: str) -> bool:
    """True when `path` is a regular file the resident can read and only root
    or this user can change, reached from / through directories the
    resident can search and only they can change, with no link at any
    component, and it holds `_CHILD` after comment lines only. The walk and
    the read go through descriptors, so the file checked is the file read."""
    parts = path.split("/")[1:] if path.startswith("/") else []
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    try:
        uid, gids = _resident_ids()
    except (KeyError, OSError):
        return False
    fd = -1
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
        for name in parts[:-1]:
            if not _fixed_for(fd, os.fstat(fd), uid, gids, 1):
                return False
            nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if not _fixed_for(fd, os.fstat(fd), uid, gids, 1):
            return False
        ffd = os.open(parts[-1], os.O_RDONLY | _OPEN_FLAGS, dir_fd=fd)
        try:
            st = os.fstat(ffd)
            if (not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CHILD_FILE_BYTES
                    or not _fixed_for(ffd, st, uid, gids, 4)):
                return False
            return _holds_program(_read_all(ffd))
        finally:
            os.close(ffd)
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _default_child_file() -> str | None:
    """CHILD_FILE when this is the install's owner process and the file is
    usable (checked once per process), else None. A process on another EA
    tree (a test on a scratch tree) keeps the inline form."""
    try:
        if not _owner_trust().is_owner_process():
            return None
    except Exception:
        return None
    # Resolved first: on the updater's release layout the services' tree is
    # a link into its release directory (docs/updates.md), and the walk in
    # _child_file_usable accepts no link, so it checks every directory of
    # the resolved path instead.
    path = os.path.realpath(CHILD_FILE)
    if path not in _CHILD_FILE_OK:
        _CHILD_FILE_OK[path] = _child_file_usable(path)
    return path if _CHILD_FILE_OK[path] else None


def _child_program() -> list[str]:
    """The program part of the child's argv: the file's path, or `-c` and
    the program text."""
    prog = None
    if not _INLINE_ONLY:
        try:
            prog = _child_file()
        except Exception:
            prog = None
    return [prog] if prog else ["-c", _CHILD]


def _child_argv(op: str, path: str, mode: int, *more: str) -> list[str]:
    return [_resident_python(), "-I", *_child_program(), op, format(mode, "o"), path, *more]


def _policy_allows(argv: list[str]) -> bool:
    """True when the root-owned sudo policy lets the owner run `argv` as the
    resident. List mode executes nothing as the resident."""
    cmd = [_sudo(), "-n", "-l", "-u", RESIDENT_USER] + argv
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S,
                           cwd="/", env=dict(CHILD_ENV))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _probe_resident() -> bool:
    """True when the sudo policy lets the owner run the child as the resident.

    List mode checks the command against the root-owned policy and executes
    nothing as the resident, so nothing the resident controls changes the
    answer. A policy that names only the inline form is asked about it
    after the file form, and the inline form is then kept."""
    global _INLINE_ONLY
    try:
        pwd.getpwnam(RESIDENT_USER)
    except KeyError:
        return False
    if _policy_allows(_child_argv("append", "/", 0o664)):
        return True
    if _child_program()[0] == "-c":
        return False
    _INLINE_ONLY = True
    return _policy_allows(_child_argv("append", "/", 0o664))


def _resident_available() -> bool:
    """The preflight result, computed once per process."""
    global _RESIDENT_OK
    if _RESIDENT_OK is None:
        _RESIDENT_OK = _probe_resident()
    return _RESIDENT_OK


def _sudo_spawn(argv: list[str]):
    """Start the child as the resident, in /, with CHILD_ENV only."""
    return subprocess.Popen([_sudo(), "-n", "-u", RESIDENT_USER] + list(argv),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(CHILD_ENV),
                            close_fds=True)


_owner_check = _default_owner_check
_availability = _resident_available
_spawn = _sudo_spawn
_child_file = _default_child_file


def is_owner() -> bool:
    """Whether this process writes into resident space through the child.
    An error in the decision counts as owner, so the rule stays on."""
    try:
        return bool(_owner_check())
    except Exception:
        return True


def _via_child() -> bool:
    """True when this call goes through the child; False when it is made
    directly (not an owner process, or the policy does not allow the child,
    which warns once)."""
    if not is_owner():
        return False
    try:
        ok = bool(_availability())
    except Exception:
        ok = False
    if not ok:
        _warn_once(f"sudo -u {RESIDENT_USER} is not allowed by the sudo policy; this "
                   f"owner process reads and writes resident space directly "
                   f"(O_NOFOLLOW, atomic replace)")
    return ok


# ── messages ──────────────────────────────────────────────────────────

def _one_line(text, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


def _warn_once(msg: str) -> None:
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(f"resident_io: {msg}", file=sys.stderr)


def _fail(verb: str, path: str, reason) -> bool:
    print(f"resident_io: could not {verb} {path}: {_one_line(reason)}", file=sys.stderr)
    return False


# ── paths ─────────────────────────────────────────────────────────────

def _abs(path) -> str:
    return os.path.abspath(os.fspath(path))


def _resolved(path: str) -> str:
    """The directory part resolved, the final name as given: what the child
    and the move check see."""
    head, name = os.path.split(path)
    return os.path.join(os.path.realpath(head or "."), name)


def _roots() -> list[str]:
    """The resolved EA tree, then the resident's home."""
    out = [os.path.realpath(_owner_trust().ea_dir())]
    try:
        out.append(os.path.realpath(pwd.getpwnam(RESIDENT_USER).pw_dir))
    except KeyError:
        pass
    return out


def _inside(path: str, top: str) -> bool:
    return path != top and os.path.commonpath([path, top]) == top


def _check_move(src: str, dst: str) -> None:
    """Raise OSError unless `dst` is in the top-level shared directory that
    holds `src` (both already resolved by _resolved)."""
    for root in _roots():
        if not _inside(src, root):
            continue
        first = os.path.relpath(src, root).split(os.sep)[0]
        top = os.path.join(root, first)
        if top == src:   # an entry directly in the root stays in the root
            if os.path.dirname(dst) == root and dst != src:
                return
        elif _inside(dst, top) and not _inside(dst, src):
            return
        raise OSError(f"{dst} is outside {src}'s top-level directory "
                      f"{root if top == src else top}")
    raise OSError(f"{src} is not in resident space")


# ── direct writes (non-owner processes, and the fallback) ─────────────

def _check_plain(fd: int, path: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OSError(f"{path} is not a plain single-linked file")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def _direct_append(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _OPEN_FLAGS, 0o666)
    try:
        _check_plain(fd, path)
        _write_all(fd, data)
    finally:
        os.close(fd)


def _direct_replace_bytes(path: str, data: bytes, mode: int) -> None:
    """atomic_io's design for bytes: a random O_EXCL temp name in the same
    directory, set to `mode`, written and synced, then renamed over `path`."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(path)}.",
                               suffix=".tmp")
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _direct_update(path: str, fn, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _OPEN_FLAGS, 0o666)
    try:
        _check_plain(fd, path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        old = _read_all(fd).decode("utf-8")
        try:
            new = fn(old)
        except Exception as e:
            raise _FnRaised(e) from e
        if new is None:
            return
        data = new.encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        _write_all(fd, data)
        os.fsync(fd)
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
    finally:
        os.close(fd)   # releases the lock


def _direct_make_dir(path: str, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.mkdir(path, 0o700)
        made = True
    except FileExistsError:
        made = False
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _OPEN_FLAGS)
    try:
        if made:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _direct_ensure_file(path: str, mode: int = 0o664) -> None:
    """Create the file when missing and set `mode` on it then, through its
    own descriptor, whatever the umask took away; an existing file keeps
    its mode."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_FLAGS, 0o666)
        made = True
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | _OPEN_FLAGS)
        made = False
    try:
        _check_plain(fd, path)
        if made:
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
    finally:
        os.close(fd)


def _direct_move(src: str, dst: str) -> None:
    os.rename(src, dst)


def _direct_read(path: str) -> bytes | None:
    """The file's bytes, or None when it is missing."""
    try:
        fd = os.open(path, os.O_RDONLY | _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    try:
        _check_plain(fd, path)
        if os.fstat(fd).st_size > MAX_READ_BYTES:
            raise OSError(f"{path} is larger than {MAX_READ_BYTES} bytes")
        data = _read_all(fd)
    finally:
        os.close(fd)
    if len(data) > MAX_READ_BYTES:
        raise OSError(f"{path} is larger than {MAX_READ_BYTES} bytes")
    return data


def _decode_list(data: bytes) -> list[tuple[str, str]]:
    out = []
    for row in data.split(b"\0") if data else []:
        name = row[1:].decode("utf-8", errors="surrogateescape")
        if not row or row[:1] not in (b"d", b"f", b"l", b"o") or not name or "/" in name:
            raise OSError("a malformed directory entry")
        out.append((row[:1].decode(), name))
    return out


def _direct_list(path: str) -> list[tuple[str, str]] | None:
    """[(kind, name)] of the directory's entries, or None when it is missing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    try:
        rows = []
        for name in sorted(os.listdir(fd)):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            kind = ("d" if stat.S_ISDIR(st.st_mode) else
                    "f" if stat.S_ISREG(st.st_mode) else
                    "l" if stat.S_ISLNK(st.st_mode) else "o")
            rows.append((kind, name))
        return rows
    finally:
        os.close(fd)


# ── the child ─────────────────────────────────────────────────────────

def _parse_ok(line: bytes):
    """The count in a clean `OK <n>\\n` line, or None."""
    if not line.startswith(OK) or not line.endswith(b"\n"):
        return None
    digits = line[len(OK):-1]
    return int(digits) if digits.isdigit() else None


def _reason(rc, err: bytes, fallback: str) -> str:
    text = (err or b"").decode("utf-8", errors="replace")[-600:]
    if REFUSED_MARK in text:
        text = text.split(REFUSED_MARK, 1)[1]
    text = text.strip()
    if text:
        return text
    return f"{fallback} (exit {rc})" if rc not in (None, 0) else fallback


def _kill_quietly(p) -> None:
    try:
        p.kill()
    except Exception:
        pass


def _close_quietly(stream, last: bytes = b"") -> None:
    try:
        if last:
            stream.write(last)
        stream.close()
    except Exception:
        pass


def _finish(p, wait_s: float = 5.0) -> tuple[int | None, bytes]:
    """Reap the child: close its stdin, give it `wait_s` to exit, then kill
    it. Returns (returncode, stderr)."""
    err = b""
    try:
        _, err = p.communicate(timeout=wait_s)
    except subprocess.TimeoutExpired:
        _kill_quietly(p)
        try:
            _, err = p.communicate(timeout=5)
        except Exception:
            pass
    except Exception:
        _kill_quietly(p)
    return p.returncode, err or b""


def _start(argv: list[str]):
    try:
        return _spawn(argv)
    except Exception as e:
        raise _ChildError(f"the resident child did not start ({type(e).__name__}: {e})")


def _child_call(op: str, paths: list[str], mode: int, data: bytes = b"") -> None:
    """One non-interactive op through the child (append, replace, mkdir,
    touch, move); raises _ChildError on anything but a clean reply."""
    p = _start(_child_argv(op, paths[0], mode, *paths[1:]))
    try:
        out, err = p.communicate(data, timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError("the resident child did not finish in time")
    except Exception as e:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError(f"the resident child failed ({type(e).__name__}: {e})")
    if p.returncode == 0 and out == OK + str(len(data)).encode() + b"\n":
        return
    raise _ChildError(_reason(p.returncode, err, "the resident child gave no valid reply"))


def _child_read(path: str) -> bytes | None:
    """read through the child: the bytes, or None when the file is
    missing; raises _ChildError on anything but a clean reply."""
    p = _start(_child_argv("read", path, 0o664))
    try:
        out, err = p.communicate(b"", timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError("the resident child did not finish in time")
    except Exception as e:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError(f"the resident child failed ({type(e).__name__}: {e})")
    if p.returncode == 0:
        if out == MISSING:
            return None
        head, sep, payload = out.partition(b"\n")
        n = _parse_ok(head + sep)
        if n is not None and n <= MAX_READ_BYTES and n == len(payload):
            return payload
    raise _ChildError(_reason(p.returncode, err, "the resident child gave no valid reply"))


def _child_list(path: str) -> list[tuple[str, str]] | None:
    """list through the child: the entries, or None when the directory is
    missing; raises _ChildError on anything but a clean reply."""
    p = _start(_child_argv("list", path, 0o664))
    try:
        out, err = p.communicate(b"", timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError("the resident child did not finish in time")
    except Exception as e:
        _kill_quietly(p)
        _finish(p)
        raise _ChildError(f"the resident child failed ({type(e).__name__}: {e})")
    if p.returncode == 0:
        if out == MISSING:
            return None
        head, sep, payload = out.partition(b"\n")
        n = _parse_ok(head + sep)
        if n is not None and n <= MAX_READ_BYTES and n == len(payload):
            try:
                return _decode_list(payload)
            except OSError as e:
                raise _ChildError(str(e))
    raise _ChildError(_reason(p.returncode, err, "the resident child gave no valid reply"))


def _child_update(path: str, fn, mode: int) -> None:
    """update through the child: it locks and sends the old bytes, `fn`
    runs here, the child writes the new bytes. Raises _ChildError on
    anything but a clean exchange, _FnRaised when `fn` raises."""
    p = _start(_child_argv("update", path, mode))
    timer = threading.Timer(CHILD_TIMEOUT_S, _kill_quietly, (p,))
    timer.daemon = True
    timer.start()
    try:
        try:
            n = _parse_ok(p.stdout.readline(64))
            if n is None or n > MAX_UPDATE_BYTES:
                raise _ChildError("the resident child gave no valid reply")
            old = p.stdout.read(n) if n else b""
            if len(old) != n:
                raise _ChildError("the resident child's reply arrived short")
            text = old.decode("utf-8")
        except (_ChildError, OSError, ValueError) as e:
            rc, err = _finish(p)
            raise _ChildError(_reason(rc, err, str(e)))
        try:
            new = fn(text)
        except Exception as e:
            _close_quietly(p.stdin, b"ABORT\n")
            _finish(p)
            raise _FnRaised(e) from e
        if new is None:
            _close_quietly(p.stdin, b"KEEP\n")
            _finish(p)
            return
        data = new.encode("utf-8")
        try:
            p.stdin.write(b"WRITE " + str(len(data)).encode() + b"\n" + data)
            p.stdin.close()
            ack = _parse_ok(p.stdout.readline(64))
            rest = p.stdout.read()
            rc = p.wait()
        except (OSError, ValueError) as e:
            rc, err = _finish(p)
            raise _ChildError(_reason(rc, err, f"the exchange with the resident child "
                                                f"failed ({type(e).__name__})"))
        if rc == 0 and ack == len(data) and not rest:
            return
        rc, err = _finish(p)
        raise _ChildError(_reason(rc, err, "the resident child gave no valid reply"))
    finally:
        timer.cancel()
        _finish(p)


# ── the API ───────────────────────────────────────────────────────────

def append_text(path, text: str) -> bool:
    """Append `text` (UTF-8) to `path`, creating it (and its directory) when
    missing."""
    p = _abs(path)
    try:
        data = text.encode("utf-8")
        if _via_child():
            _child_call("append", [_resolved(p)], 0o664, data)
        else:
            _direct_append(p, data)
        return True
    except Exception as e:
        return _fail("append to", p, e)


def replace_text(path, text: str, mode: int = 0o664) -> bool:
    """Replace `path` with `text` (UTF-8) atomically, the new file at `mode`
    (direct: through atomic_io)."""
    p = _abs(path)
    try:
        if _via_child():
            _child_call("replace", [_resolved(p)], mode, text.encode("utf-8"))
        else:
            _atomic.write_text(p, text, mode=mode)
        return True
    except Exception as e:
        return _fail("replace", p, e)


def replace_bytes(path, data: bytes, mode: int = 0o664) -> bool:
    """Replace `path` with `data` atomically, the new file at `mode`."""
    p = _abs(path)
    try:
        data = bytes(data)
        if _via_child():
            _child_call("replace", [_resolved(p)], mode, data)
        else:
            _direct_replace_bytes(p, data, mode)
        return True
    except Exception as e:
        return _fail("replace", p, e)


def update_text(path, fn, mode: int = 0o664) -> bool:
    """Read-modify-write `path` in place under one exclusive lock: `fn` gets
    the current text ('' for a new file) and returns the new text, or None
    to keep the file. The lock is held by whichever process has the file
    open (the child, in an owner process), so a writer that locks the same
    file cannot lose this change or have this one lose its. `mode` is set
    on the file after the write where this user may set it. An exception
    from `fn` keeps the file and propagates."""
    p = _abs(path)
    raised = None
    try:
        if _via_child():
            _child_update(_resolved(p), fn, mode)
        else:
            _direct_update(p, fn, mode)
        return True
    except _FnRaised as e:
        raised = e.exc
    except Exception as e:
        return _fail("update", p, e)
    raise raised


def make_dir(path, mode: int = 0o2775) -> bool:
    """Make `path` a directory, creating it and any missing parents (the
    parents with umask 002). A directory this call creates gets `mode`,
    set through its own descriptor; an existing directory is kept as it
    is. A symlink or a file at `path` is a failure."""
    p = _abs(path)
    try:
        if _via_child():
            _child_call("mkdir", [_resolved(p)], mode)
        else:
            _direct_make_dir(p, mode)
        return True
    except Exception as e:
        return _fail("make the directory", p, e)


def ensure_file(path) -> bool:
    """Create `path` empty when it is missing (a lock file), mode 664; an
    existing plain file is kept as it is. A symlink or anything but a
    single-linked regular file at `path` is a failure."""
    p = _abs(path)
    try:
        if _via_child():
            _child_call("touch", [_resolved(p)], 0o664)
        else:
            _direct_ensure_file(p)
        return True
    except Exception as e:
        return _fail("create", p, e)


def move(src, dst) -> bool:
    """Rename the entry `src` onto `dst` (replacing an entry there; a
    symlink at either name is renamed or replaced, never followed). `dst`
    must be in `src`'s top-level shared directory (see the module
    docstring); the check runs on the resolved directories before the
    rename."""
    s, d = _abs(src), _abs(dst)
    try:
        rs, rd = _resolved(s), _resolved(d)
        _check_move(rs, rd)
        if _via_child():
            _child_call("move", [rs, rd], 0o664)
        else:
            _direct_move(rs, rd)
        return True
    except Exception as e:
        return _fail(f"move {s} to", d, e)


def read_bytes(path) -> bytes | None:
    """The bytes of `path`, or None when it is missing or cannot be read
    (a failure costs one stderr line)."""
    p = _abs(path)
    try:
        if _via_child():
            return _child_read(_resolved(p))
        return _direct_read(p)
    except Exception as e:
        _fail("read", p, e)
        return None


def read_text(path) -> str | None:
    """The text (UTF-8) of `path`, or None when it is missing or cannot be
    read or decoded (a failure costs one stderr line)."""
    data = read_bytes(path)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        _fail("read", _abs(path), e)
        return None


def list_dir(path) -> list[tuple[str, str]] | None:
    """[(kind, name)] for the entries of the directory `path`, sorted by
    name: kind is d (directory), f (regular file), l (symlink, never
    followed) or o (anything else). None when it is missing or cannot be
    listed (a failure costs one stderr line). A symlink at `path` itself is
    refused."""
    p = _abs(path)
    try:
        if _via_child():
            return _child_list(_resolved(p))
        return _direct_list(p)
    except Exception as e:
        _fail("list", p, e)
        return None
