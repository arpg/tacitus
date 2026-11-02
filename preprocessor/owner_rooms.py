"""The rooms the chat bot created for the owner, recorded owner side.

The room registry (`~/.ea/config/ea-channels.json`) is ea-bot space, so a
row there only asks. This record, `<private_dir>/owner-rooms.json`, names
each room the bot itself created for the owner (`how: "created"`), and each
room it took over from the registry once, the first time it ran with the
record (`how: "adopted"`: a private room whose members were exactly the
owner and the bot). The bot serves, posts in, archives or unarchives a
registered room only when this record names it and the live checks pass
(docs/SECURITY.md, "Owner trust rules").

Only owner processes read or write it; the reads and writes go through
owner_trust, so a file that fails the owner checks counts as no record.

    {"rooms": {"<channel id>": {"name": "ea-atlas", "kind": "project",
                                "how": "created", "at": 1790000000}}}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

try:
    from . import owner_trust
except ImportError:  # run from preprocessor/
    import owner_trust

FILE = "owner-rooms.json"

_LOCK = threading.Lock()


def path() -> Path:
    return owner_trust.private_dir() / FILE


def load() -> dict | None:
    """{channel id: row}, or None when the record does not exist yet.
    Raises OwnerTrustError or ValueError when the file fails the owner
    checks or is not the record's shape."""
    try:
        raw = owner_trust.read_owner_file(path())
    except FileNotFoundError:
        return None
    doc = json.loads(raw)
    rooms = doc.get("rooms") if isinstance(doc, dict) else None
    if not isinstance(rooms, dict):
        raise ValueError(f"{path()} holds no rooms object")
    return {str(k): dict(v) for k, v in rooms.items() if isinstance(v, dict)}


def _save(rooms: dict) -> None:
    p = path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner_trust.write_owner_file(p, json.dumps({"rooms": rooms}, indent=2) + "\n",
                                 mode=0o600)


def get(channel_id: str) -> dict | None:
    """The record's row for the room, or None (also when the record cannot
    be read)."""
    try:
        rooms = load() or {}
    except (OSError, ValueError, owner_trust.OwnerTrustError):
        return None
    return rooms.get(str(channel_id))


def has(channel_id: str) -> bool:
    return get(channel_id) is not None


def record(channel_id: str, *, name: str = "", kind: str = "project",
           how: str = "created") -> None:
    """Name the room in the record (kept when it is there already)."""
    with _LOCK:
        rooms = load() or {}
        cid = str(channel_id)
        if cid not in rooms:
            rooms[cid] = {"name": str(name), "kind": str(kind), "how": str(how),
                          "at": int(time.time())}
        _save(rooms)


def update(channel_id: str, **fields) -> bool:
    """Set fields on the room's row (archived_by_bot, for one). False when
    the record does not name the room."""
    with _LOCK:
        rooms = load() or {}
        row = rooms.get(str(channel_id))
        if row is None:
            return False
        row.update(fields)
        _save(rooms)
        return True


def move(moves: dict) -> list[str]:
    """Re-key each room the record names under an old id in `moves` to its
    new id, the row kept as it is: the move from shadow mode to the web
    chat alone (scripts/webchat-cutover.py). A new id the record names
    already keeps its own row. The old ids moved; none when there is no
    record."""
    with _LOCK:
        rooms = load()
        if rooms is None:
            return []
        moved = []
        for old, new in moves.items():
            old, new = str(old), str(new)
            if old in rooms and new not in rooms and old != new:
                rooms[new] = rooms.pop(old)
                moved.append(old)
        if moved:
            _save(rooms)
        return moved


def start(adopt: dict) -> list[str] | None:
    """Create the record once: `adopt` maps each room to take over to its
    row ({"name", "kind"}). Returns the adopted ids, or None when the record
    exists already (nothing is adopted then)."""
    with _LOCK:
        if load() is not None:
            return None
        now = int(time.time())
        rooms = {str(cid): {"name": str(row.get("name") or ""),
                            "kind": str(row.get("kind") or "project"),
                            "how": "adopted", "at": now}
                 for cid, row in adopt.items()}
        _save(rooms)
        return sorted(rooms)
