"""The note a ledger write leaves for Home's stored briefings
(home_deeper, docs/webchat.md, Home, "The deeper briefing").

ledger.py tells this module of each open row it adds or changes
(ledger.ROW_WRITTEN_HOOKS). The writer is often the resident's chat turn,
which cannot reach the owner's store of briefings: it appends the row's id
to a small file in its own state, and the owner's next refresh writes that
row's briefing first. The note only orders the work: refresh writes a row
only when its stored briefing is missing or older than the row, by the
ledger's own record. In an owner process no note is written; refresh
finds the row by its version anyway.

Standard library only, and nothing done at import: the resident's process
loads it on every ledger write and cannot cache its bytecode.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

QUEUE_FILE = "home-briefing-queue.jsonl"
QUEUE_MAX_BYTES = 64_000
QUEUE_KEEP = 300
QUEUE_DAYS = 2


def ea_dir() -> Path:
    """The EA tree this process uses (EA_DIR, default ~/.ea), as
    owner_trust.ea_dir reads it."""
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))


def path() -> Path:
    return ea_dir() / "state" / QUEUE_FILE


def resident() -> bool:
    """This process hosts the EA tree in its own home: the resident (as
    owner_trust.is_owner_process tells them apart). The owner's ~/.ea is a
    link into the resident's home."""
    try:
        return ea_dir().resolve().is_relative_to(Path.home().resolve())
    except OSError:
        return False


def row_written(entry_id: str, *, created: bool = False) -> None:
    """ledger.py's hook: the open row `entry_id` was added or changed. In
    the resident's process, one line in its own state, cut to the last
    QUEUE_KEEP lines when it grows past QUEUE_MAX_BYTES; nothing in an owner
    process. Never raises."""
    try:
        if not resident():
            return
        p = path()
        line = json.dumps({"id": str(entry_id)[:200], "at": round(time.time(), 1), "created": bool(created)})
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     0o640)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
        if p.stat().st_size > QUEUE_MAX_BYTES:
            keep = p.read_text(errors="replace").splitlines()[-QUEUE_KEEP:]
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(keep) + "\n")
            os.replace(tmp, p)
    except Exception as e:
        import sys
        print(f"home_briefing_queue: the row was not noted for its briefing: {str(e)[:120]}", file=sys.stderr)


def ids(text: str, *, now: float | None = None) -> list[str]:
    """The ids noted in the last QUEUE_DAYS days in `text` (the file's
    contents), newest first, each once."""
    cut = (now if now is not None else time.time()) - QUEUE_DAYS * 86400
    out: list[str] = []
    for line in reversed((text or "").splitlines()[-QUEUE_KEEP:]):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        lid = str(rec.get("id") or "")
        try:
            at = float(rec.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if lid and at >= cut and lid not in out:
            out.append(lid)
    return out
