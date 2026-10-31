#!/usr/bin/env python3
"""Bot-mediated file (and thread-text) delivery for the resident EA, on
every chat front end.

The resident Claude runs as the ``ea-bot`` user and, by design, holds no
chat credential (``SLACK_BOT_TOKEN``, ``ZULIP_API_KEY``) and cannot open the
web chat's store, which is the owner's (the security model keeps all of
them out of its reach, so a prompt-injected resident cannot exfiltrate or
forge them). It therefore cannot post files to the chat directly. This
module is the bridge:

  * the resident STAGES a file into a shared outbox  -> ``stage()``
  * the main bot (which holds the credential, or serves the web chat's
    store) DRAINS it through its own front end      -> ``drain()``

The credential never leaves the bot. Layout under ``EA_DIR/state/outbox/``::

    <stamp>.<name>                  the staged file (copied from the resident)
    <stamp>.<name>.deliver.json     sidecar: {thread_ts, comment, title,
                                              original_name, attempts}

The file uploaded for a job is ALWAYS the sidecar's paired file inside the
outbox (derived from the sidecar's own name), never an arbitrary path from
the sidecar body, and the channel is always the one the bot passes in. So a
staged job can only post a file the resident already placed here, to the EA
channel -- it cannot be steered to read /etc/anything or post elsewhere.

Text-only jobs ride the same rails::

    <stamp>.message.deliver.json    sidecar: {kind: "text", thread_ts,
                                              comment, attempts}

An out-of-dispatch invocation (cron, plugin, anything nobody is reading the
stdout of) can already post to a CHANNEL through an incoming webhook, but a
webhook cannot reply into a THREAD. Staging a sidecar with no paired file
closes that gap: ``drain()`` posts the comment on its own via ``post_fn``.
The ``kind`` key separates a deliberate text job from an orphaned file job;
a missing ``kind`` means "file".

The outbox is the resident's space. Only the resident stages (an owner
process holds the credential and sends directly, so ``stage()`` refuses to
run in one). The bot reads sidecars, rewrites their attempt count and
dead-letters jobs through resident_io, with the resident's access, and reads
a paired file for upload through the resident child below.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
if __name__ == "__main__":
    # The resident child runs as `python3 -I`, which leaves the script's own
    # directory off sys.path; the sibling modules below live there.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import resident_io as _rio
import time
from pathlib import Path

try:
    from chat.types import THREAD_KEY_RE
except ImportError:
    try:
        from preprocessor.chat.types import THREAD_KEY_RE
    except ImportError:
        # The same pattern as chat.types, so the module stays importable
        # on its own.
        THREAD_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]{0,399}$")
log = logging.getLogger(__name__)
SIDECAR_SUFFIX = ".deliver.json"
DEFAULT_MAX_ATTEMPTS = 3
# Failed attempts per sidecar in this process: a job whose sidecar cannot
# be rewritten still reaches max_attempts instead of retrying forever.
_ATTEMPTS: dict[str, int] = {}

# The resident user, and the token that turns this module into a one-shot
# reader run as the resident (see materialize_for_owner).
RESIDENT_USER = "ea-bot"
READ_AS_RESIDENT = "--read-as-resident"
MAX_UPLOAD_BYTES = 128 * 1024 * 1024   # ceiling on a staged file read for upload
# A clean child reply starts with READ_OK and the byte count; anything else
# (non-zero exit, missing or malformed output) is a refusal. REFUSED_MARK only
# makes the child's message readable; it carries no authority.
READ_OK = b"OUTBOX-READ-OK "
REFUSED_MARK = "OUTBOX-READ-REFUSED:"
PROBE_TIMEOUT_S = 15
_RESIDENT_OK: bool | None = None   # preflight result, cached per process


class OutboxReadError(Exception):
    """The staged file could not be read with the resident's access."""


def _private_dir() -> Path:
    """The owner-private directory the resident cannot reach (matches
    chat.private_dir, without importing the chat package)."""
    import pwd
    return Path(os.environ.get("EA_PRIVATE_DIR") or str(Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea-private"))


def _read_staged_file(path_str: str, newer_than: float = 0.0,
                      max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read one staged file, refusing a symlink at the final component, a
    multiply-linked or non-regular file, a file larger than max_bytes (never
    above MAX_UPLOAD_BYTES), and one last changed before newer_than (epoch
    seconds; 0 refuses nothing). The checks and the read use one descriptor.
    Runs as whichever user calls it; the owner reaches it only through the
    resident (see _resident_read)."""
    max_bytes = min(int(max_bytes), MAX_UPLOAD_BYTES)
    fd = os.open(path_str, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OutboxReadError(f"staged path {path_str!r} is not a regular file")
        if st.st_nlink != 1:
            raise OutboxReadError(f"staged path {path_str!r} is multiply linked")
        if st.st_size > max_bytes:
            raise OutboxReadError(f"staged file {path_str!r} exceeds "
                                  f"{max_bytes} bytes")
        if newer_than and st.st_mtime < newer_than:
            raise OutboxReadError(f"staged file {path_str!r} was last changed "
                                  f"before {newer_than:.0f}")
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read(max_bytes + 1)[:max_bytes]
    finally:
        os.close(fd)


def _resident_python() -> str:
    """The interpreter for the resident child: the system python3, which the
    resident can execute (the owner's venv lives under the owner's home)."""
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"


def _helper_argv(path_str: str, newer_than: float = 0.0,
                 max_bytes: int = MAX_UPLOAD_BYTES) -> list[str]:
    """The child's command line; the two limits ride along only when set."""
    argv = [_resident_python(), "-I", str(Path(__file__).resolve()),
            READ_AS_RESIDENT, path_str]
    if newer_than or max_bytes != MAX_UPLOAD_BYTES:
        argv += [repr(float(newer_than)), str(int(max_bytes))]
    return argv


def _probe_resident() -> bool:
    """True when the sudo policy lets the owner run the reader as the resident.

    List mode checks the command against the root-owned sudoers and executes
    nothing as the resident, so nothing the resident controls changes the
    answer.
    """
    try:
        import pwd
        pwd.getpwnam(RESIDENT_USER)
    except (KeyError, ImportError):
        return False
    cmd = ["sudo", "-n", "-l", "-u", RESIDENT_USER] + _helper_argv("/")
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


def _resident_read(path_str: str, newer_than: float = 0.0,
                   max_bytes: int = MAX_UPLOAD_BYTES,
                   fallback: bool = True) -> bytes:
    """Read a staged outbox file with the RESIDENT's access.

    The outbox is ea-bot-writable, so the file the owner is about to upload
    could have been swapped for a symlink to an owner-only file after it was
    staged. Reading it as the resident means the owner only ever ships bytes
    the resident could itself read, and the O_NOFOLLOW open refuses a swapped
    symlink outright.

    The direct read is used only when the once-per-process preflight says the
    drop is unavailable (logged as RESIDENT-IO-FALLBACK). After a passing
    preflight, any child outcome except a clean framed reply raises: the child
    runs with the resident's identity, so its behaviour never selects the
    fallback. With fallback=False a failed preflight refuses the read
    instead: a path the resident names in text, not one it staged, is read
    with its access or not at all. Overridable via `_read_runner` for
    tests."""
    if not _resident_available():
        if not fallback:
            raise OutboxReadError(f"no resident read for {path_str!r} "
                                  f"(sudo to {RESIDENT_USER} unavailable)")
        log.warning("RESIDENT-IO-FALLBACK: staged file read directly by the "
                    "owner (sudo to %s unavailable)", RESIDENT_USER)
        return _read_staged_file(path_str, newer_than, max_bytes)
    cmd = (["sudo", "-n", "-u", RESIDENT_USER]
           + _helper_argv(path_str, newer_than, max_bytes))
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OutboxReadError(f"resident read did not complete ({type(e).__name__})")
    out = p.stdout or b""
    if p.returncode == 0 and out.startswith(READ_OK):
        head, sep, payload = out[len(READ_OK):].partition(b"\n")
        if sep and head.isdigit() and int(head) == len(payload):
            return payload
    msg = (p.stderr or b"").decode(errors="replace")[-600:]
    raise OutboxReadError(msg.split(REFUSED_MARK, 1)[-1].strip()
                          or f"resident read of {path_str!r} gave no valid reply")


# Overridable so tests exercise materialisation without sudo.
_read_runner = _resident_read


def materialize_for_owner(paired, *, private_root: Path | None = None,
                          newer_than: float = 0.0,
                          max_bytes: int = MAX_UPLOAD_BYTES,
                          fallback: bool = True) -> Path:
    """Copy a staged outbox file into an owner-private temp, reading the source
    with the resident's access. Returns the temp path for the owner to upload;
    the caller passes it to discard_materialized when done. newer_than,
    max_bytes and fallback are _resident_read's; a refused read raises
    OutboxReadError."""
    data = _read_runner(str(paired), newer_than=newer_than,
                        max_bytes=max_bytes, fallback=fallback)
    root = (private_root or _private_dir()) / "upload-staging"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(root),
                               suffix="." + _safe_name(Path(paired).name))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    return Path(tmp)


def discard_materialized(path) -> None:
    """Remove an owner-private temp made by materialize_for_owner."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def outbox_dir(base: Path | None = None) -> Path:
    """The outbox directory (EA_DIR/state/outbox by default)."""
    return (base or _ea_dir()) / "state" / "outbox"


def _ensure_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    # setgid + group rwx so the resident (ea-bot) and the token-holding bot
    # (same shared group on ~/.ea/state) can both read/write regardless of
    # which one created it. Best-effort: unit tests run single-user.
    try:
        os.chmod(d, 0o2770)
    except OSError:
        pass


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return cleaned[:120] or "file"


def _stamp(i: int) -> str:
    return f"{int(time.time() * 1000)}-{os.getpid()}-{i}"


def _check_ts(thread_ts) -> str:
    if not THREAD_KEY_RE.match(str(thread_ts or "")):
        raise ValueError(
            "thread_ts must be a thread key with no spaces "
            f"(Slack: 1712345678.123456), got {thread_ts!r}")
    return str(thread_ts)


def _write_sidecar(sidecar: Path, meta: dict) -> None:
    # Group-writable: the resident (ea-bot) stages it and the token-holding
    # bot rewrites its attempt count, with the resident's access.
    if not _rio.replace_text(sidecar, json.dumps(meta), 0o660):
        raise OSError(f"could not write the sidecar {sidecar}")


def _refuse_owner() -> None:
    """Staging writes into the resident's outbox, which only the resident
    does: an owner process has the credential and sends directly."""
    if _rio.is_owner():
        raise ValueError("an owner process sends directly with its own "
                         "credential; staging into the outbox is the "
                         "resident's path")


def stage(files, thread_ts, *, comment=None, title=None,
          base: Path | None = None) -> list[dict]:
    """Copy each file into the outbox and write its delivery sidecar.

    ``files`` is a path or list of paths. Returns one dict per staged file:
    ``{staged, sidecar, original_name}``. Raises ``ValueError`` on a bad
    thread_ts or a missing file so the caller can report it.
    """
    _check_ts(thread_ts)
    _refuse_owner()
    files = [files] if isinstance(files, (str, Path)) else list(files)
    if not files:
        raise ValueError("no files to stage")

    d = outbox_dir(base)
    _ensure_dir(d)
    staged: list[dict] = []
    for i, fp in enumerate(files):
        src = Path(fp)
        if not src.is_file():
            raise ValueError(f"not a file: {src}")
        dest = d / f"{_stamp(i)}.{_safe_name(src.name)}"
        shutil.copy2(src, dest)
        try:
            os.chmod(dest, 0o640)
        except OSError:
            pass
        meta = {
            "thread_ts": str(thread_ts),
            "comment": comment,
            "title": title or src.name,
            "original_name": src.name,
            "attempts": 0,
        }
        sidecar = Path(str(dest) + SIDECAR_SUFFIX)
        _write_sidecar(sidecar, meta)
        staged.append({"staged": str(dest), "sidecar": str(sidecar),
                       "original_name": src.name})
    return staged


def stage_text(text, thread_ts, *, base: Path | None = None) -> dict:
    """Stage a text-only message for the bot to post INTO a thread.

    Same rails as ``stage()`` minus the payload file: a lone sidecar marked
    ``kind="text"``. This is the only way an out-of-dispatch invocation
    (cron, plugin, external) reaches a thread -- incoming webhooks post to
    the channel and cannot thread. Returns ``{sidecar, kind, thread_ts}``.
    Raises ``ValueError`` on a bad thread_ts or empty text.
    """
    _check_ts(thread_ts)
    _refuse_owner()
    text = (text or "").strip()
    if not text:
        raise ValueError("no text to stage")

    d = outbox_dir(base)
    _ensure_dir(d)
    i = 0
    sidecar = d / f"{_stamp(i)}.message{SIDECAR_SUFFIX}"
    while sidecar.exists():  # same-millisecond staging from one process
        i += 1
        sidecar = d / f"{_stamp(i)}.message{SIDECAR_SUFFIX}"
    _write_sidecar(sidecar, {
        "kind": "text",
        "thread_ts": str(thread_ts),
        "comment": text,
        "attempts": 0,
    })
    return {"sidecar": str(sidecar), "kind": "text",
            "thread_ts": str(thread_ts)}


def drain(upload_fn, *, channel, post_fn=None, base: Path | None = None,
          outbox: Path | None = None,
          max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[dict]:
    """Deliver every staged job via ``upload_fn`` / ``post_fn`` and clean up.

    ``upload_fn(channel, thread_ts, file, filename, title, initial_comment)``
    performs a file upload; ``post_fn(channel, thread_ts, text)`` posts a
    text-only job into its thread. Either raising signals failure (the job is
    left for retry until ``max_attempts``, then dead-lettered to
    ``outbox/failed/``). ``channel`` is forced by the caller (the bot passes
    the EA channel); the sidecar cannot override it. Returns a result dict
    per job with ``kind`` in {file, text} and ``status`` in
    {sent, retry, failed, dropped}.

    ``post_fn`` is optional; without it a text job is dead-lettered rather
    than looping forever.
    """
    d = outbox or outbox_dir(base)
    if not d.is_dir():
        return []

    results: list[dict] = []
    for sidecar in sorted(d.glob("*" + SIDECAR_SUFFIX)):
        # Read with the resident's access: the text is posted, so a link at
        # the name must not hand the bot a file only the owner may read.
        text = _rio.read_text(sidecar)
        if text is None:
            if os.path.lexists(sidecar):
                # Refused (a link, a second name, not a regular file): set
                # it aside so it is not retried every tick.
                _deadletter(d, sidecar, None)
                results.append({"kind": "text", "file": None, "thread_ts": None,
                                "status": "dropped",
                                "error": "sidecar is not a plain file"})
            continue
        try:
            meta = json.loads(text)
        except Exception:
            # Corrupt sidecar: skip this tick and try again.
            continue
        if not isinstance(meta, dict):
            continue
        # No "kind" means a file job.
        is_text = meta.get("kind") == "text"
        # A text job owns nothing but its sidecar; never let it unlink or
        # dead-letter a path it did not stage.
        paired = (None if is_text
                  else d / sidecar.name[:-len(SIDECAR_SUFFIX)])
        tts = meta.get("thread_ts")
        base_result = {"kind": "text" if is_text else "file",
                       "file": None if is_text else paired.name,
                       "thread_ts": tts}
        bad = None
        if not THREAD_KEY_RE.match(str(tts or "")):
            bad = "bad thread_ts"
        elif is_text and post_fn is None:
            bad = "text job staged but drain got no post_fn"
        elif is_text and not str(meta.get("comment") or "").strip():
            bad = "text job with no message body"
        elif not is_text and not paired.is_file():
            bad = "missing paired file"
        if bad:
            _deadletter(d, sidecar, paired)
            results.append({**base_result, "status": "dropped", "error": bad})
            continue
        try:
            if is_text:
                post_fn(channel=channel, thread_ts=str(tts),
                        text=meta["comment"])
            else:
                upload_fn(
                    channel=channel,
                    thread_ts=str(tts),
                    file=str(paired),
                    filename=meta.get("original_name") or paired.name,
                    title=meta.get("title") or paired.name,
                    initial_comment=meta.get("comment"),
                )
        except Exception as e:  # noqa: BLE001 - report, retry, or dead-letter
            key = str(sidecar)
            attempts = max(int(meta.get("attempts", 0)),
                           _ATTEMPTS.get(key, 0)) + 1
            _ATTEMPTS[key] = attempts
            meta["attempts"] = attempts
            meta["last_error"] = str(e)[:300]
            try:
                # A rename, not a write in place: the sidecar belongs to
                # whoever staged it, and only the directory is shared.
                _write_sidecar(sidecar, meta)
            except OSError:
                pass
            if attempts >= max_attempts:
                _ATTEMPTS.pop(key, None)
                _deadletter(d, sidecar, paired)
                results.append({**base_result, "status": "failed",
                                "error": str(e)[:300]})
            else:
                results.append({**base_result, "status": "retry",
                                "error": str(e)[:300]})
            continue
        for p in (sidecar, paired):
            if p is None:
                continue
            try:
                p.unlink()
            except OSError:
                pass
        _ATTEMPTS.pop(str(sidecar), None)
        results.append({**base_result, "status": "sent"})
    return results


def _deadletter(d: Path, sidecar: Path, paired: Path | None) -> None:
    """Move a permanently-failed / orphaned job aside so it stops recurring.

    ``paired`` is None for a text job (sidecar only)."""
    dead = d / "failed"
    owner = _rio.is_owner()
    try:
        if owner:
            # The bot: the directory and the renames with the resident's
            # access, so a link at `failed` cannot carry a job elsewhere.
            if not _rio.make_dir(dead, 0o2770):
                return
        else:
            dead.mkdir(exist_ok=True)
        for p in (sidecar, paired):
            if p is None or not os.path.lexists(p):
                continue
            if owner:
                _rio.move(p, dead / p.name)
            else:
                p.replace(dead / p.name)
    except OSError:
        pass


def _read_as_resident_main(path_str: str, newer_than: str = "0",
                           max_bytes: str = str(MAX_UPLOAD_BYTES)) -> int:
    """Entrypoint the owner runs through sudo as the resident: stream one
    staged file to stdout, refusing a symlink or multiply-linked path, and
    one outside the limits the owner passed."""
    try:
        data = _read_staged_file(path_str, float(newer_than), int(max_bytes))
        sys.stdout.buffer.write(READ_OK + str(len(data)).encode() + b"\n" + data)
        sys.stdout.buffer.flush()
        return 0
    except (OutboxReadError, OSError) as e:
        sys.stderr.write(REFUSED_MARK + str(e))
        return 4
    except ValueError as e:
        sys.stderr.write(REFUSED_MARK + f"bad limits: {e}")
        return 4


if __name__ == "__main__":
    if len(sys.argv) in (3, 5) and sys.argv[1] == READ_AS_RESIDENT:
        sys.exit(_read_as_resident_main(*sys.argv[2:]))
    sys.stderr.write("slack_outbox is a library; only "
                     f"{READ_AS_RESIDENT} <path> [<newer_than> <max_bytes>] "
                     "is a valid invocation\n")
    sys.exit(2)
