#!/usr/bin/env bash
#
# owner-io.sh: file access and environment loading for the scripts the
# owner runs (the cron agents, the plugin runner, the resident pane, the
# eval runner) where they meet the ea-bot user's space.
#
# ea-bot runs the LLM and is untrusted. Any directory it can change (its
# home, ~/.ea and everything below it, the plugin dirs) is ea-bot space,
# even where a single file there is owner-owned. In ea-bot space a symlink
# at a file name turns an owner write into a write to any file the owner
# can write, and an owner read into a read of any file only the owner can
# read. So the owner's scripts send those paths through this helper, which
# sorts every path into one of two kinds:
#
#   owner path    every directory from / down, every symlink followed on
#                 the way and the file itself belong to root or the owner,
#                 and no other user can add, remove or rename entries in
#                 those directories. Read and written as the owner, at the
#                 resolved path.
#   shared path   anything else. Read and written as ea-bot through
#                 `sudo -n -u ea-bot`, so a planted symlink reaches only
#                 what ea-bot can reach already.
#
# Usage:
#   owner-io.sh check PATH            print "owner" or "shared", a tab, the path used
#   owner-io.sh read PATH             copy the regular file PATH to stdout
#   owner-io.sh write PATH            copy stdin to PATH
#   owner-io.sh mkdir PATH            mkdir -p PATH
#   owner-io.sh touch PATH            touch PATH
#   owner-io.sh rm PATH               rm -f PATH (the entry, never a link target)
#   owner-io.sh mv SRC DST            rename SRC onto DST (both of one kind)
#   owner-io.sh env EA_DIR PRIV_DIR   the process environment (below)
#   owner-io.sh kv PATH               a plugin's own KEY=VALUE secrets (below)
#
# Files ea-bot writes get umask 002, so they stay group-shared like the
# rest of ~/.ea.
#
# `env` prints NUL-terminated KEY and VALUE fields, a pair per variable,
# for the caller to `export "$key=$value"`. The sources, in order, a later
# one winning:
#   EA_DIR/.env            ea-bot space: read as ea-bot, and only the
#                          non-secret keys in CONFIG_KEYS are taken
#   EA_DIR/.env.secrets    legacy: read by the owner only when it is a
#                          regular file, not a symlink, owned by the running
#                          user and writable by no one else
#   PRIV_DIR/secrets.env   the owner-private secrets file, same checks
# `kv` prints NUL-terminated NAME=VALUE entries for an `env` argument list;
# it never takes the loader, interpreter or identity variables.
# Both parse file content as data: a line is KEY=VALUE, optionally with
# `export ` in front and quotes around the value, and nothing in a file is
# ever run as shell. A skipped file or key costs one stderr line, which
# names keys and never prints a value.
set -uo pipefail

EA_BOT_USER=ea-bot

read -r -d '' _PY <<'PY' || true
import errno
import grp
import os
import pwd
import re
import stat
import sys

UID = os.getuid()

# Keys the owner's scripts, and the Python they run, take from the
# environment. CONFIG_KEYS may come from ea-bot's .env; SECRET_KEYS only
# from a secrets file the owner alone controls.
CONFIG_KEYS = frozenset("""
    EA_MODEL EA_BRIEFING_MODEL EA_LITERAL_COMMANDS EA_DEBUG TEAMS_CHAT_REGION
    EA_SLACK_CHANNEL_ID EA_SLACK_CHANNEL_NAME EA_SLACK_OWNER_ID EA_SCHED_CHANNEL_ID
    GRAPH_AUTH_METHOD GRAPH_CLIENT_ID GRAPH_AUTHORITY GRAPH_REDIRECT_URI GRAPH_SCOPES
""".split())
SECRET_KEYS = frozenset("""
    SLACK_BOT_TOKEN SLACK_APP_TOKEN GRAPH_CLIENT_SECRET
""".split())

LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
# No quote, backslash, dollar, backtick, semicolon, ampersand, pipe,
# redirection or control character: a value that could mean something to
# a shell is refused rather than passed on.
ENV_VALUE = re.compile(r"[A-Za-z0-9 _.,:/@%+=~!?*^()\[\]{}#-]*")
# A plugin value only ever becomes one `env` argument, so any printable
# character but whitespace is taken.
KV_VALUE = re.compile(r"[!-~]*")
# What a plugin's secrets may never set: the variables that steer the
# loader, the interpreter, the shell or the process identity, and the ones
# the plugin runner sets itself.
RESERVED = re.compile(r"""(?x)
    PATH|HOME|USER|LOGNAME|SHELL|IFS|ENV|BASH_ENV|TMPDIR
  | EA_DIR|EA_PRIVATE_DIR|EA_RUN_ID|EA_PLUGIN_\w*
  | LD_\w*|DYLD_\w*|PYTHON\w*|PERL5?LIB|PERL5OPT|RUBYLIB|RUBYOPT
  | NODE_OPTIONS|NODE_PATH|BASH_\w*|GIT_\w*|SUDO_\w*
""")


def warn(msg):
    sys.stderr.write(f"owner-env: {msg}\n")


# ---- path kinds ------------------------------------------------------------

_group_cache = {}


def _others_in_group(gid):
    """True when a user other than root and this one is in group gid."""
    if gid not in _group_cache:
        try:
            me = pwd.getpwuid(UID).pw_name
        except KeyError:
            me = None
        try:
            names = set(grp.getgrgid(gid).gr_mem)
        except KeyError:
            names = {"(unknown group)"}
        names |= {p.pw_name for p in pwd.getpwall() if p.pw_gid == gid}
        _group_cache[gid] = bool(names - {me, "root"})
    return _group_cache[gid]


def _has_access_acl(path):
    try:
        return "system.posix_acl_access" in os.listxattr(path, follow_symlinks=False)
    except (OSError, AttributeError):
        return False


def _owner_dir(path, st):
    """A directory owned by root or this user in which no other user can
    add, remove or rename entries. A sticky directory (/tmp) qualifies:
    others can add entries there but cannot touch ours, and every entry
    this walk passes through must be ours or root's."""
    if st.st_uid not in (0, UID):
        return False
    mode = st.st_mode
    if mode & stat.S_ISVTX:
        return True
    if mode & 0o002:
        return False
    if mode & 0o020 and (_has_access_acl(path) or _others_in_group(st.st_gid)):
        return False
    return True


def classify(path):
    """("owner", resolved) when only root or this user can change where
    path leads, else ("shared", path) with the owner-controlled prefix
    resolved, so ea-bot can reach the rest itself. A path that ends in a
    missing name under owner directories is an owner path."""
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    if not _owner_dir("/", os.lstat("/")):
        return "shared", path
    todo = [p for p in path.split("/") if p not in ("", ".")]
    cur, hops = "/", 0
    while todo:
        name = todo.pop(0)
        if name == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, name)
        rest = os.path.join(nxt, *todo)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            return "owner", rest
        except OSError:
            return "shared", rest
        if st.st_uid not in (0, UID):
            return "shared", rest
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > 40:
                return "shared", rest
            target = os.readlink(nxt)
            if target.startswith("/"):
                cur = "/"
            todo = [p for p in target.split("/") if p not in ("", ".")] + todo
            continue
        if stat.S_ISDIR(st.st_mode):
            if not _owner_dir(nxt, st):
                return "shared", rest
            cur = nxt
            continue
        # A file: the last name, or a path that fails with ENOTDIR on use.
        return "owner", rest
    return "owner", cur


def classify_entry(path):
    """The kind of the directory holding path, and path's entry in it,
    for operations on the entry itself (rm, mv), which never follow it."""
    parent, base = os.path.split(path.rstrip("/") or "/")
    if base in ("", ".", ".."):
        raise ValueError(f"not a directory entry: {path}")
    kind, where = classify(parent or ".")
    return kind, os.path.join(where, base)


# ---- env files -------------------------------------------------------------

def read_owner_file(path, uid=UID):
    """The text of path when it is a regular file, not a symlink, owned by
    uid and writable by no one else; None otherwise, with one stderr line
    unless the file is simply absent. The file is opened without following
    a symlink and checked through the open descriptor, so what is checked
    is what is read."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        reason = "a symlink" if e.errno in (errno.ELOOP, errno.EMLINK) else e.strerror
        warn(f"{path}: skipped ({reason})")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            reason = "not a regular file"
        elif st.st_uid != uid:
            reason = "not owned by the running user"
        elif st.st_mode & 0o022:
            reason = "writable by group or others"
        else:
            data = b""
            while len(data) < (1 << 20):
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
            return data.decode("utf-8", "replace")
        warn(f"{path}: skipped ({reason})")
        return None
    finally:
        os.close(fd)


def _unquote(raw):
    """The value of a KEY=VALUE line: surrounding quotes removed, or for an
    unquoted value a trailing ' # comment' removed. None when a quote does
    not close or text other than a comment follows the closing quote."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end < 0:
            return None
        tail = v[end + 1:].strip()
        if tail and not tail.startswith("#"):
            return None
        return v[1:end]
    cut = re.search(r"\s#", v)
    return v[:cut.start()].rstrip() if cut else v


def _entries(text):
    """(key, value-or-None) per KEY=VALUE line, and the count of other
    lines that are neither blank nor comments."""
    out, odd = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.fullmatch(line)
        if not m:
            odd += 1
            continue
        out.append((m.group(1), _unquote(m.group(2))))
    return out, odd


def _report(label, refused, unsafe, odd):
    if refused:
        warn(f"{label}: not loaded from this file: {', '.join(sorted(refused))}")
    if unsafe:
        warn(f"{label}: skipped, the value is not plain data: {', '.join(sorted(unsafe))}")
    if odd:
        warn(f"{label}: skipped {odd} line(s) that are not KEY=VALUE")


def parse_env(text, label, keys):
    """The (key, value) pairs of text whose key is in keys."""
    pairs, refused, unsafe = [], set(), set()
    entries, odd = _entries(text)
    for key, value in entries:
        if key not in keys or not ENV_KEY.fullmatch(key):
            refused.add(key)
        elif value is None or not ENV_VALUE.fullmatch(value):
            unsafe.add(key)
        else:
            pairs.append((key, value))
    _report(label, refused, unsafe, odd)
    return pairs


def parse_kv(text, label):
    """The NAME=VALUE strings of text, reserved names left out."""
    out, refused, unsafe = [], set(), set()
    entries, odd = _entries(text)
    for key, value in entries:
        if RESERVED.fullmatch(key):
            refused.add(key)
        elif value is None or not KV_VALUE.fullmatch(value):
            unsafe.add(key)
        else:
            out.append(f"{key}={value}")
    _report(label, refused, unsafe, odd)
    return out


def load_env(ea_dir, priv_dir, dotenv_text):
    """The pairs to export, in load order (a later pair wins)."""
    pairs = parse_env(dotenv_text, os.path.join(ea_dir, ".env"), CONFIG_KEYS)
    both = CONFIG_KEYS | SECRET_KEYS
    legacy = os.path.join(ea_dir, ".env.secrets")
    text = read_owner_file(legacy)
    if text is not None:
        pairs += parse_env(text, legacy, both)
    private = os.path.join(priv_dir, "secrets.env")
    kind, where = classify(private)
    if kind != "owner":
        warn(f"{private}: skipped (other users can change its directory)")
    else:
        text = read_owner_file(where)
        if text is not None:
            pairs += parse_env(text, private, both)
    return pairs


def main(argv):
    out = sys.stdout.buffer
    cmd = argv[0] if argv else ""
    if cmd in ("check", "check-entry") and len(argv) == 2:
        kind, where = (classify if cmd == "check" else classify_entry)(argv[1])
        print(f"{kind}\t{where}")
    elif cmd == "env" and len(argv) == 3:
        for key, value in load_env(argv[1], argv[2], sys.stdin.read()):
            out.write(key.encode() + b"\0" + value.encode() + b"\0")
    elif cmd == "kv" and len(argv) == 2:
        for entry in parse_kv(sys.stdin.read(), argv[1]):
            out.write(entry.encode() + b"\0")
    else:
        sys.stderr.write("owner-io: bad internal call\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PY

_py() { python3 -c "$_PY" "$@"; }

# where MODE PATH: set KIND (owner|shared) and WHERE (the path to use).
# MODE is check (the file itself) or check-entry (the directory entry).
where() {
    local out
    out=$(_py "$1" "$2") || { echo "owner-io: cannot classify $2" >&2; return 1; }
    KIND=${out%%$'\t'*}
    WHERE=${out#*$'\t'}
    [ "$KIND" = owner ] || [ "$KIND" = shared ]
}

as_bot() { sudo -n -u "$EA_BOT_USER" "$@"; }

do_read() {
    where check "$1" || return 1
    if [ "$KIND" = owner ]; then
        [ -f "$WHERE" ] && cat -- "$WHERE"
    else
        as_bot sh -c '[ -f "$1" ] && exec cat -- "$1"' sh "$WHERE"
    fi
}

# Blank when absent, the content when readable, one stderr line otherwise.
read_if_present() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        do_read "$1" 2>/dev/null || echo "owner-env: $1: could not be read; skipped" >&2
    fi
    return 0
}

usage() { sed -n '/^# Usage:/,/^# Files ea-bot/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

cmd="${1:-}"
case "$cmd" in
    check)
        [ $# -eq 2 ] || usage
        where check "$2" && printf '%s\t%s\n' "$KIND" "$WHERE" ;;
    read)
        [ $# -eq 2 ] || usage
        do_read "$2" ;;
    write)
        [ $# -eq 2 ] || usage
        where check "$2" || exit 1
        if [ "$KIND" = owner ]; then
            cat > "$WHERE"
        else
            as_bot sh -c 'umask 002 && exec cat > "$1"' sh "$WHERE"
        fi ;;
    mkdir)
        [ $# -eq 2 ] || usage
        where check "$2" || exit 1
        if [ "$KIND" = owner ]; then
            mkdir -p -- "$WHERE"
        else
            as_bot sh -c 'umask 002 && exec mkdir -p -- "$1"' sh "$WHERE"
        fi ;;
    touch)
        [ $# -eq 2 ] || usage
        where check "$2" || exit 1
        if [ "$KIND" = owner ]; then
            touch -- "$WHERE"
        else
            as_bot sh -c 'umask 002 && exec touch -- "$1"' sh "$WHERE"
        fi ;;
    rm)
        [ $# -eq 2 ] || usage
        where check-entry "$2" || exit 1
        if [ "$KIND" = owner ]; then
            rm -f -- "$WHERE"
        else
            as_bot rm -f -- "$WHERE"
        fi ;;
    mv)
        [ $# -eq 3 ] || usage
        where check-entry "$2" || exit 1
        src_kind=$KIND src=$WHERE
        where check-entry "$3" || exit 1
        if [ "$src_kind" != "$KIND" ]; then
            echo "owner-io: $2 and $3 are not both owner paths or both shared paths" >&2
            exit 1
        fi
        if [ "$KIND" = owner ]; then
            mv -f -- "$src" "$WHERE"
        else
            as_bot mv -f -- "$src" "$WHERE"
        fi ;;
    env)
        [ $# -eq 3 ] || usage
        read_if_present "$2/.env" | _py env "$2" "$3" ;;
    kv)
        [ $# -eq 2 ] || usage
        read_if_present "$2" | _py kv "$2" ;;
    *)
        usage ;;
esac
