#!/usr/bin/env python3
"""ea-mail-mediator: the owner-side executor for the resident's SAFE mail ops.

Runs as the owner (systemd: ea-mail-mediator.service), holds the Graph/Google tokens,
and drains the request queue ea-bot writes to. For each request it RE-VALIDATES
(default-deny verb allowlist + path confinement, never trusting the client),
STAGES every file argument through an owner-private directory (so the child never
opens a path the resident can swap for a symlink mid-flight), then runs the
real client tool as the owner and writes the captured result back.

GATED verbs (send / calendar mutate) never reach here as SAFE, so they are
refused. This process is the only side that can send; the resident cannot.

See docs/mail-mediation-spec.md.
"""
from __future__ import annotations

import glob
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draft_lock  # noqa: E402  (one owner-side lock per draft, shared with the send path)
import mail_mediation as M  # noqa: E402
import resident_io  # noqa: E402  (queue reads and writes with the resident's access)

REPO = Path(__file__).resolve().parent.parent
TOOLS = {
    "graph_actions": REPO / "preprocessor" / "graph_actions.py",
    "gmail_client": REPO / "preprocessor" / "gmail_client.py",
    "mail_actions": REPO / "preprocessor" / "mail_actions.py",
    "google_calendar_client": REPO / "preprocessor" / "google_calendar_client.py",
    "scheduling_engine": REPO / "preprocessor" / "scheduling_engine.py",
    "deck_serve": REPO / "preprocessor" / "deck_serve.py",
    "email_writer": REPO / "preprocessor" / "email_writer.py",
    "plugin_cards": REPO / "preprocessor" / "plugin_cards.py",
}
# Tools whose calls take long (a remote model writing a body): each runs on
# its own thread, so the queue keeps serving every other verb meanwhile.
ASYNC_TOOLS = {"email_writer"}
ASYNC_MAX = 4
_async_slots = threading.BoundedSemaphore(ASYNC_MAX)
from log_paths import owner_log  # the mediator runs only as the owner
AUDIT = owner_log("mail-mediator.log")
IDLE_S = 0.3
EXEC_TIMEOUT_S = 120
# How long a draft-changing verb waits for a send of that draft to finish.
DRAFT_LOCK_WAIT_S = 30.0

# Internal entrypoint token. `mail_mediator.py --confined-io <read|write>
# <ea_dir> <path>` performs ONE confined read or write and exits. The mediator
# invokes it through `sudo -u ea-bot` so the byte transfer runs with the
# resident's own access; see `_run_as_resident` below. A clean reply starts
# with CONFINED_OK and the byte count; anything else is a refusal.
CONFINED_IO = "--confined-io"
CONFINED_OK = b"CONFINED-IO-OK "

# --- file staging: no TOCTOU on a mediated path -----------------------------
# Path confinement is a pre-check, and the owner-side write happens seconds later
# in a child process. Both allowed roots (~/.ea/state, /tmp) are ea-bot-
# writable, so the resident could swap the checked path for a symlink inside
# that window and the child, running as the owner, would follow it.
#
# So the child never sees a path the resident chose. Every FILE_FLAGS value is
# re-pointed at an owner-private staging directory (0700, under the owner's home, which
# ea-bot cannot traverse): inputs are COPIED IN before the child starts,
# outputs are DELIVERED OUT after it exits. Delivery re-validates the
# destination at delivery TIME (not enqueue time) and then opens it with
# O_NOFOLLOW at every path component, so there is no window left to race.
#
# The copy-in READ of the resident-named source and the deliver-out WRITE to
# the resident-named destination run with the RESIDENT's own access, not the
# owner's, so no path a symlink-free walk can reach under an allowed root (a
# owner-owned file or directory that happens to sit in /tmp, say) is read into a
# draft or written by the owner. `_run_as_resident` drops to the resident (ea-bot)
# for exactly those two byte transfers; the confined O_NOFOLLOW open still runs
# inside that resident helper, so the resident's broader read set does not
# reopen the symlink hole. The invariant: an owner process reads a
# resident-named file, and writes a resident-named path, only with what the
# resident could itself read or write.
#
# The staging root is derived from the passwd entry, never from $HOME, so
# no environment setting can move it.
def _default_staging_root() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        if home.is_dir():
            return home / ".ea-mediator-staging"
    except Exception:
        pass
    return Path(tempfile.gettempdir()) / f".ea-mediator-staging-{os.geteuid()}"


STAGING_ROOT = _default_staging_root()
MAX_STAGE_BYTES = 32 * 1024 * 1024   # cap on a single copied-in file
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class Refused(Exception):
    """The request cannot be executed safely; refuse it like a bad path."""


def _audit(tool, verb, outcome):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT, "a") as f:
            f.write(f"{int(time.time())} {tool} {verb} {outcome}\n")
    except OSError:
        pass


def _safe_name(value: str) -> str:
    """A harmless basename for a staged copy of an ea-bot-supplied path.

    The basename is preserved as far as it safely can be: attach-file names the
    attachment after it when --name is absent, so mangling it would rename the
    file the recipient sees. Only separators, control characters and the two
    directory entries that can escape are rewritten; the staged copy lives in a
    fresh 0700 directory and is passed to the child as an argv element, never
    through a shell.
    """
    base = re.sub(r"[\x00-\x1f/]", "_", os.path.basename(value.rstrip("/")))[:120]
    return "file" if base in ("", ".", "..") else base


def _ensure_staging_root() -> Path:
    """Create (or validate) the owner-private staging root. 0700, owned by us."""
    root = STAGING_ROOT
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        raise Refused(f"staging root unavailable ({e})")
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise Refused("staging root is not a directory owned by the mediator")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(root, 0o700)
    return Path(root)


def _split_confined(path_str: str) -> tuple[Path, list[str]]:
    """Split an ea-bot-supplied path into (trusted anchor, components).

    The anchor is an allowed root, matched LEXICALLY (normpath, no symlink
    resolution) so that nothing below it is trusted; the components are then
    walked with O_NOFOLLOW. Raises Refused when the path is not inside a root.
    """
    nd = os.path.normpath(os.path.abspath(path_str))
    for root in M._allowed_roots():
        anchors = {os.path.normpath(os.path.abspath(str(root)))}
        try:
            anchors.add(os.path.realpath(str(root)))
        except OSError:
            pass
        for anchor in anchors:
            if nd == anchor:
                raise Refused(f"path {path_str!r} is an allowed root itself")
            if nd.startswith(anchor.rstrip(os.sep) + os.sep):
                rest = nd[len(anchor.rstrip(os.sep)):].strip(os.sep).split(os.sep)
                if any(c in ("", ".", "..") for c in rest):
                    raise Refused(f"path {path_str!r} has unsafe components")
                return Path(anchor), rest
    raise Refused(f"path {path_str!r} is outside ea-bot dirs")


def _walk_dirs(anchor: Path, parts: list[str], create: bool) -> int:
    """Open the directory holding `parts[-1]`, refusing every symlink on the
    way. Returns a dir fd the caller must close."""
    dir_fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts[:-1]:
            try:
                nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=dir_fd)
            except FileNotFoundError:
                if not create:
                    raise Refused(f"missing directory component {comp!r}")
                os.mkdir(comp, 0o755, dir_fd=dir_fd)
                nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=dir_fd)
            except OSError as e:
                raise Refused(f"refusing symlinked path component {comp!r} ({e})")
            os.close(dir_fd)
            dir_fd = nxt
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _read_confined(path_str: str) -> bytes:
    """Read an ea-bot-supplied INPUT file without following any symlink."""
    anchor, parts = _split_confined(path_str)
    dir_fd = _walk_dirs(anchor, parts, create=False)
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            raise Refused(f"input file {path_str!r} does not exist")
        except OSError as e:
            raise Refused(f"refusing to read {path_str!r} ({e})")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"input {path_str!r} is not a regular file")
            if st.st_size > MAX_STAGE_BYTES:
                raise Refused(f"input {path_str!r} exceeds {MAX_STAGE_BYTES} bytes")
            with os.fdopen(os.dup(fd), "rb") as f:
                return f.read(MAX_STAGE_BYTES + 1)[:MAX_STAGE_BYTES]
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _write_confined(path_str: str, data: bytes):
    """Write an OUTPUT file to an ea-bot-supplied destination, safely.

    Re-validates the destination NOW (delivery time, not enqueue time), then
    opens it with O_NOFOLLOW on every component: a symlink planted while the
    child was talking to Graph is refused rather than followed. An existing
    plain file is replaced (download-attachment has always overwritten); an
    existing symlink or hard link is refused.
    """
    if not M._within_allowed(path_str):
        raise Refused(f"destination {path_str!r} is outside ea-bot dirs")
    anchor, parts = _split_confined(path_str)
    dir_fd = _walk_dirs(anchor, parts, create=True)
    try:
        name = parts[-1]
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=dir_fd)
        except FileExistsError:
            try:
                fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as e:   # ELOOP: it is a symlink -> refuse
                raise Refused(f"refusing to write through {path_str!r} ({e})")
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                os.close(fd)
                raise Refused(f"refusing to write {path_str!r} "
                              "(not a plain single-linked file)")
            os.ftruncate(fd, 0)
        except OSError as e:
            raise Refused(f"refusing to write {path_str!r} ({e})")
        try:
            os.fchmod(fd, 0o644)   # ea-bot reads it back; do not inherit umask
            os.write(fd, data)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


# --- byte transfer with the resident's own access ---------------------------
# `_run_as_resident` is the drop-to-ea-bot step, kept overridable so the unit
# tests exercise staging and delivery without sudo. main() installs the sudo
# implementation; a direct handle() call (the tests) leaves it None, and the
# confined read/write then runs in-process.
#
# Whether the drop is possible is decided ONCE per process by a preflight the
# resident cannot influence: a sudo LIST-mode check (`sudo -n -l -u ea-bot
# <helper>`) asks the root-owned policy whether the owner may run the exact
# helper as the resident, and runs nothing as the resident. Only a failed
# preflight falls back to the in-process O_NOFOLLOW-as-owner read/write (and
# audits RESIDENT-IO-FALLBACK). Once the preflight has passed, the child's own
# behaviour never selects the fallback: the child runs with the resident's
# identity, so any outcome other than a clean framed success (a non-zero exit,
# missing or malformed output, a marker or none) refuses that file.
#
# The child is started as `python3 -I`, so the resident's user site-packages
# and PYTHON* variables do not load; the owner passes the EA directory on the
# command line rather than through the environment. REFUSED_MARK only makes
# the child's message readable; it carries no authority.
_run_as_resident = None
REFUSED_MARK = "CONFINED-IO-REFUSED:"
PROBE_TIMEOUT_S = 15
_RESIDENT_OK: bool | None = None   # preflight result, cached per process


def _resident_python() -> str:
    """The interpreter for the resident child: the system python3, which the
    resident can execute (the owner's venv lives under the owner's home)."""
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"


def _helper_argv(mode: str, path_str: str) -> list[str]:
    return [_resident_python(), "-I", str(Path(__file__).resolve()),
            CONFINED_IO, mode, str(M.EA_DIR), path_str]


def _probe_resident() -> bool:
    """True when the sudo policy lets the owner run the helper as the resident.

    List mode checks the command against the root-owned sudoers and executes
    nothing as the resident, so the resident's environment, files and process
    table cannot change the answer.
    """
    try:
        pwd.getpwnam(M.RESIDENT_USER)
    except KeyError:
        return False
    cmd = ["sudo", "-n", "-l", "-u", M.RESIDENT_USER] + _helper_argv("read", "/")
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _resident_available() -> bool:
    """The preflight result, computed once per process."""
    global _RESIDENT_OK
    if _RESIDENT_OK is None:
        _RESIDENT_OK = _probe_resident()
    return _RESIDENT_OK


def _frame(count: int, payload: bytes = b"") -> bytes:
    return CONFINED_OK + str(count).encode() + b"\n" + payload


def _unframe(out: bytes):
    """(count, payload) from a clean child reply, or None when malformed."""
    if not out or not out.startswith(CONFINED_OK):
        return None
    head, sep, payload = out[len(CONFINED_OK):].partition(b"\n")
    if not sep or not head.isdigit():
        return None
    return int(head), payload


def _sudo_resident_io(mode: str, path_str: str, data: bytes) -> tuple[bytes, int, bytes]:
    """Run one confined read/write AS THE RESIDENT via sudo.

    Falls back to the in-process confined I/O only when the preflight says the
    drop is unavailable. After a passing preflight every child outcome except a
    clean framed success is a refusal for this file.
    """
    if not _resident_available():
        _audit("resident-io", mode, "RESIDENT-IO-FALLBACK")
        return _inprocess_resident_io(mode, path_str, data)
    cmd = ["sudo", "-n", "-u", M.RESIDENT_USER] + _helper_argv(mode, path_str)
    try:
        p = subprocess.run(cmd, input=data, capture_output=True,
                           timeout=EXEC_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return b"", 4, f"resident {mode} did not complete ({type(e).__name__})".encode()
    reply = _unframe(p.stdout or b"") if p.returncode == 0 else None
    if reply is not None:
        count, payload = reply
        if mode == "read" and count == len(payload):
            return payload, 0, b""
        if mode == "write" and payload == b"" and count == len(data):
            return b"", 0, b""
    err = (p.stderr or b"")[-600:] or f"resident {mode} gave no valid reply".encode()
    return b"", p.returncode or 4, err


def _inprocess_resident_io(mode: str, path_str: str, data: bytes) -> tuple[bytes, int, bytes]:
    """The confined read/write in this process (the O_NOFOLLOW-as-owner path)."""
    try:
        if mode == "read":
            return _read_confined(path_str), 0, b""
        _write_confined(path_str, data)
        return b"", 0, b""
    except Refused as e:
        return b"", 4, (REFUSED_MARK + str(e)).encode()


def _resident_err(err: bytes, fallback: str) -> str:
    text = err.decode(errors="replace")
    if REFUSED_MARK in text:
        text = text.split(REFUSED_MARK, 1)[1]
    return text.strip() or fallback


def _read_via_resident(path_str: str) -> bytes:
    """Copy an INPUT file into staging, reading it with the resident's access."""
    runner = _run_as_resident or _inprocess_resident_io
    out, rc, err = runner("read", path_str, b"")
    if rc != 0:
        raise Refused(_resident_err(err, f"cannot read {path_str!r} as the resident"))
    return out


def _write_via_resident(path_str: str, data: bytes) -> None:
    """Deliver an OUTPUT file to its destination, writing with the resident's
    access."""
    runner = _run_as_resident or _inprocess_resident_io
    out, rc, err = runner("write", path_str, data)
    if rc != 0:
        raise Refused(_resident_err(err, f"cannot write {path_str!r} as the resident"))


def _confined_io(mode: str, path_str: str) -> int:
    """Entrypoint run as the resident: one confined read (framed bytes to
    stdout) or write (from stdin, framed byte count to stdout), then exit.
    Refusals go to stderr with a non-zero code."""
    try:
        if mode == "read":
            data = _read_confined(path_str)
            sys.stdout.buffer.write(_frame(len(data), data))
            sys.stdout.buffer.flush()
            return 0
        if mode == "write":
            data = sys.stdin.buffer.read()
            _write_confined(path_str, data)
            sys.stdout.buffer.write(_frame(len(data)))
            sys.stdout.buffer.flush()
            return 0
        sys.stderr.write(REFUSED_MARK + f"unknown confined-io mode {mode!r}")
        return 2
    except (Refused, OSError) as e:
        sys.stderr.write(REFUSED_MARK + str(e))
        return 4


def _stage_paths(rid: str, argv: list[str]):
    """Re-point every file flag at an owner-private staging path.

    Returns (new_argv, stage_dir_or_None, deliveries, rewrites): `deliveries`
    is the (staged_path, requested_destination) pairs to write out after the
    child exits, `rewrites` the same pairs for EVERY staged flag, so the
    child's output can be reported in the caller's own path names.
    Raises Refused if an input cannot be staged safely.
    """
    new_argv = list(argv)
    deliveries: list[tuple[Path, str]] = []
    rewrites: list[tuple[str, str]] = []
    stage_dir: Path | None = None
    try:
        for idx, flag, value, attached in M.file_arg_positions(argv):
            if not value:
                continue
            if stage_dir is None:
                root = _ensure_staging_root()
                safe_rid = _SAFE_NAME.sub("_", rid)[:64] or uuid.uuid4().hex
                stage_dir = root / safe_rid
                shutil.rmtree(stage_dir, ignore_errors=True)
                stage_dir.mkdir(mode=0o700)
            # One subdir per argument so two flags cannot collide on a basename,
            # and the basename itself survives (attach-file names the attachment
            # after it when --name is absent).
            slot = stage_dir / f"a{idx}"
            slot.mkdir(mode=0o700, exist_ok=True)
            staged = slot / _safe_name(value)
            if flag in M.OUTPUT_FLAGS:
                deliveries.append((staged, value))
            else:
                staged.write_bytes(_read_via_resident(value))
            rewrites.append((str(staged), value))
            new_argv[idx] = f"{flag}={staged}" if attached else str(staged)
    except BaseException:
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return new_argv, stage_dir, deliveries, rewrites


def _deliver(deliveries) -> list[str]:
    """Write staged outputs to their requested destinations. Returns errors."""
    errors = []
    for staged, dest in deliveries:
        if not staged.exists():
            continue          # the child produced nothing; nothing to deliver
        try:
            _write_via_resident(dest, staged.read_bytes())
        except Refused as e:
            errors.append(str(e))
        except OSError as e:
            errors.append(f"could not deliver to {dest!r} ({e})")
    return errors


def _respond(rid: str, stdout: str, stderr: str, rc: int):
    """Write the reply into the queue with the resident's access (the queue
    is the resident's space), mode 0640 so the resident reads it."""
    resp = M.QUEUE / f"resp-{rid}.json"
    if not resident_io.replace_text(
            resp, json.dumps({"stdout": stdout, "stderr": stderr, "rc": rc}), mode=0o640):
        _audit("-", "-", f"RESP-WRITE-FAILED {rid[:40]}")


def handle(req_path: str):
    name = Path(req_path).name           # req-<rid>.json
    rid = name[len("req-"):-len(".json")]
    try:
        # The request is the resident's file: read it with the resident's
        # access, so a link at its name reaches nothing the resident could
        # not read itself.
        text = resident_io.read_text(req_path)
        if text is None:
            raise OSError("request unreadable")
        req = json.loads(text)
        if not isinstance(req, dict):
            raise ValueError("request is not a JSON object")
    except Exception:
        try:
            Path(req_path).unlink()
        except OSError:
            pass
        return
    tool = req.get("tool")
    verb = req.get("verb", "")
    argv = req.get("argv") or []

    # Re-validate server-side. This is the trust boundary, not the ea-bot shim.
    # is_allowed = SAFE verbs + the EA-Managed attendee-less auto-hold exception.
    # A plugin tool's verb is SAFE only when the owner's plugin lock says so.
    # The EA-Managed id comes from the owner-private calendars.json only.
    known = tool in TOOLS or M.is_plugin_tool(tool)
    if not known or not M.is_allowed(tool, verb, argv, owner=True):
        _respond(rid, "", json.dumps(
            {"status": "error", "message": f"mediator refused '{tool} {verb}'"}), 2)
        _audit(tool, verb, "REFUSED-VERB")
        _cleanup(req_path)
        return
    err = M.sanitize(tool, verb, argv)
    if err:
        _respond(rid, "", json.dumps(
            {"status": "error", "message": f"mediator refused: {err}"}), 2)
        _audit(tool, verb, "REFUSED-PATH")
        _cleanup(req_path)
        return

    run_id = str(req.get("run_id") or "")[:64]
    # A Codex ghost run (codex_ghost.py) may read; every other verb is
    # recorded for the judge and answered as the real verb answers (a
    # synthetic draft id for a draft), never executed.
    recorded = _ghost_recorded(tool, verb, argv, run_id)
    if recorded is not None:
        _respond(rid, json.dumps(recorded), "", 0)
        _audit(tool, verb, "GHOST-RECORDED")
        _cleanup(req_path)
        return
    if _ghost_help(argv, run_id):
        # A ghost's `<verb> --help` runs as the verb's usage alone: the
        # parser prints it and exits before any verb runs.
        argv = [verb, "--help"]
    refusal = _gate_precheck(tool, verb, argv, run_id)
    if refusal:
        _respond(rid, "", json.dumps({"status": "error", "message": refusal}), 2)
        _audit(tool, verb, "REFUSED-KEPT")
        _cleanup(req_path)
        return

    # The child never receives a path the resident controls. Inputs are copied
    # into an owner-private staging dir now; outputs are delivered from it after
    # the child exits, with the destination re-checked at that moment.
    stage_dir = None
    try:
        child_argv, stage_dir, deliveries, rewrites = _stage_paths(rid, argv)
    except (Refused, OSError) as e:
        _respond(rid, "", json.dumps(
            {"status": "error", "message": f"mediator refused: {e}"}), 2)
        _audit(tool, verb, "REFUSED-PATH")
        _cleanup(req_path)
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)
        return

    if tool in ASYNC_TOOLS and _async_slots.acquire(blocking=False):
        # The request file goes now, so the loop does not pick it up again.
        _cleanup(req_path)

        def run():
            try:
                _execute(rid, tool, verb, argv, run_id, child_argv, stage_dir,
                         deliveries, rewrites, req_path)
            finally:
                _async_slots.release()
        threading.Thread(target=run, daemon=True).start()
        return
    _execute(rid, tool, verb, argv, run_id, child_argv, stage_dir, deliveries, rewrites,
             req_path)


def _execute(rid, tool, verb, argv, run_id, child_argv, stage_dir, deliveries, rewrites,
             req_path):
    """Run one validated, staged request as the owner and answer it."""
    cwd, timeout = str(REPO), EXEC_TIMEOUT_S
    plugin_name = ""
    if M.is_plugin_tool(tool):
        # An owner-zone plugin's verb: its command from the owner's lock,
        # run from the owner-private plugin directory after the owner and
        # integrity checks (plugin_registry.owner_verb_command).
        try:
            import plugin_registry
            prefix, plugin_cwd, timeout = plugin_registry.owner_verb_command(
                tool[len(M.PLUGIN_TOOL_PREFIX):], verb)
        except Exception as e:
            _respond(rid, "", json.dumps(
                {"status": "error", "message": f"mediator refused '{tool} {verb}': {e}"}), 2)
            _audit(tool, verb, "REFUSED-PLUGIN")
            _cleanup(req_path)
            if stage_dir is not None:
                shutil.rmtree(stage_dir, ignore_errors=True)
            return
        cmd, cwd = list(prefix) + list(child_argv), str(plugin_cwd)
        plugin_name = tool[len(M.PLUGIN_TOOL_PREFIX):]
    else:
        cmd = [sys.executable, str(TOOLS[tool])] + list(child_argv)
    env = dict(os.environ)
    if plugin_name:
        import plugin_registry
        env = plugin_registry.owner_command_env(plugin_name, cwd, env)
    env.pop("EA_FORCE_MEDIATE", None)   # never recurse into the shim
    # A core tool's own parser confirms it resolved the verb checked here
    # (mail_mediation.check_child_parse).
    env.pop(M.MEDIATED_VERB_ENV, None)
    if not plugin_name:
        env[M.MEDIATED_VERB_ENV] = verb
    # The child reads the EA-Managed id from the owner-private file only,
    # whatever owner_trust makes of it (mail_mediation.owner_side).
    env[M.OWNER_SIDE_ENV] = "1"
    # Forward the dispatch identity so draft_events.record() can label the
    # journal line with its owning run. The value crosses the trust boundary
    # from ea-bot, so it is length-capped and charset-restricted before
    # entering the owner-side child's environment.
    if run_id and all(c.isalnum() or c in "._-" for c in run_id):
        env["EA_RUN_ID"] = run_id
    else:
        env.pop("EA_RUN_ID", None)
    # A verb that changes a draft runs under that draft's lock, the lock a
    # send holds from its content check to the send (draft_lock.py).
    scope = None if plugin_name else M.draft_lock_scope(tool, verb, argv)
    try:
        if scope is None:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env=env)
        else:
            with draft_lock.hold(scope[0], whole_mailbox=scope[1],
                                 wait_s=DRAFT_LOCK_WAIT_S):
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                   timeout=timeout, env=env)
        out, err_txt, rc = p.stdout, p.stderr, p.returncode
        if rc == 0:
            out, refusal = _gate_filter(tool, verb, argv, run_id, out)
            if refusal:
                err_txt, rc = json.dumps({"status": "error", "message": refusal}), 2
                deliveries = []
                _audit(tool, verb, "REFUSED-KEPT")
        errors = _deliver(deliveries)
        # The child reported the staging path it read/wrote; the caller asked
        # for its own path, and that is what now holds the bytes.
        for staged, original in rewrites:
            out = out.replace(staged, original)
            err_txt = err_txt.replace(staged, original)
        if errors:
            err_txt += json.dumps(
                {"status": "error", "message": "mediator refused delivery: "
                                               + "; ".join(errors)})
            rc = rc or 2
            _audit(tool, verb, "REFUSED-DELIVERY")
        _respond(rid, out, err_txt, rc)
        if not errors:
            _audit(tool, verb, f"rc={rc}")
            if rc == 0:
                _note_release(tool, verb, argv, run_id, out)
    except subprocess.TimeoutExpired:
        _respond(rid, "", json.dumps(
            {"status": "error", "message": "mediator exec timeout"}), 3)
        _audit(tool, verb, "EXEC-TIMEOUT")
    except draft_lock.Busy:
        _respond(rid, "", json.dumps(
            {"status": "error", "message": "the draft is being sent; nothing was "
                                           "changed. Try again in a minute."}), 3)
        _audit(tool, verb, "DRAFT-LOCKED")
    finally:
        _cleanup(req_path)
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)


_GHOST_PREFIX = "cron-ghost-codex-"


def _ghost_recorded(tool, verb, argv, run_id) -> dict | None:
    """None unless the run id is a Codex ghost's and the call is a write
    (or a read of a draft the ghost made): then the JSON answered in place
    of the call, with the call recorded in the ghost store
    (codex_ghost.ghost_answer). A failure to record still refuses the
    write: a ghost never executes one."""
    if not str(run_id or "").startswith(_GHOST_PREFIX):
        return None
    try:
        import codex_ghost
        reads = codex_ghost.is_ghost_read(tool, verb) or codex_ghost.wants_help(argv)
        return codex_ghost.ghost_answer(tool, verb, list(argv), run_id, body="" if reads else _ghost_body(argv))
    except Exception as e:
        return {"status": "recorded", "ghost": True,
                "message": f"ghost run: '{tool} {verb}' not executed ({str(e)[:80]})"}


def _ghost_help(argv, run_id) -> bool:
    if not str(run_id or "").startswith(_GHOST_PREFIX):
        return False
    try:
        import codex_ghost
        return codex_ghost.wants_help(argv)
    except Exception:
        return False


def _ghost_body(argv) -> str:
    """The body a ghost's draft call carries, for the judge: the
    --body-file's text, read with the resident's access (the path passed
    the root checks already), else a --body that is not the placeholder."""
    argv = [str(a) for a in argv or []]
    for flag in ("--body-file",):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            text = resident_io.read_text(argv[argv.index(flag) + 1])
            if text:
                return text[:6000]
    if "--body" in argv and argv.index("--body") + 1 < len(argv):
        body = argv[argv.index("--body") + 1]
        if body.strip() and body.strip() != "x":
            return body[:6000]
    return ""


def _gate_precheck(tool, verb, argv, run_id) -> str:
    """The mail gate's refusal for a cron lane's read of a kept thread
    (mail_privacy.release_precheck); '' to let the read run. An error
    with the mail switch at enforce refuses a cron lane's read."""
    try:
        import mail_privacy
        return mail_privacy.release_precheck(tool, verb, list(argv), run_id)
    except Exception as e:
        return _gate_error(tool, verb, run_id, e)


def _gate_filter(tool, verb, argv, run_id, out):
    try:
        import mail_privacy
        return mail_privacy.release_filter(tool, verb, list(argv), run_id, out)
    except Exception as e:
        refusal = _gate_error(tool, verb, run_id, e)
        return ("", refusal) if refusal else (out, "")


def _gate_error(tool, verb, run_id, e) -> str:
    if not str(run_id or "").startswith("cron-") or tool != "graph_actions":
        return ""
    try:
        import remote_llm
        if not remote_llm.enforcing("mail"):
            return ""
    except Exception:
        pass
    return f"The mail gate failed ({str(e)[:100]}); refused for a cron lane."


def _note_release(tool, verb, argv, run_id, out):
    """The remote-LLM shadow run's record of which threads a mail read
    handed to a lane (docs/remote-llm.md). It runs after the answer is
    sent, does nothing while that experiment is off, and never raises."""
    try:
        import mail_privacy
        mail_privacy.note_release(tool, verb, list(argv), run_id, out)
    except Exception:
        pass


def _cleanup(req_path: str):
    try:
        Path(req_path).unlink()
    except OSError:
        pass


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == CONFINED_IO:
        # Invoked as the resident (via sudo, python3 -I) to read/write one
        # confined path. The EA directory comes from the owner's argv, not the
        # environment, so the allowed roots are the owner's.
        if len(sys.argv) != 5 or not os.path.isabs(sys.argv[3]):
            sys.stderr.write("confined-io requires <mode> <abs ea_dir> <path>")
            sys.exit(2)
        M.EA_DIR = Path(sys.argv[3])
        sys.exit(_confined_io(sys.argv[2], sys.argv[4]))

    # The service side drops to the resident for every byte transfer of a
    # resident-named file, so the read/write uses the resident's access.
    global _run_as_resident
    _run_as_resident = _sudo_resident_io

    # setgid + group-writable so ea-bot can enqueue; created with the
    # resident's access, and an existing directory is kept as it is.
    resident_io.make_dir(M.QUEUE, mode=0o2770)
    _audit("-", "-", "STARTED")
    while True:
        try:
            for req in sorted(glob.glob(str(M.QUEUE / "req-*.json"))):
                handle(req)
        except Exception as e:  # never die on one bad request
            _audit("-", "-", f"LOOP-ERR {str(e)[:120]}")
        time.sleep(IDLE_S)


if __name__ == "__main__":
    main()
