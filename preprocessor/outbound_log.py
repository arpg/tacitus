"""One audit record per outbound request made by EA code other than fetch_url.

fetch_url.py logs its own requests to `~/.ea/logs/fetch-url.log`. The other
modules that reach the network (booking_link's booking-page reads, list_digest's
redirect walk, poll_filler's poll reads and submissions, email_to_pdf's
renderer) and the remote email writer log here, so the daily review
(outbound_review.py, scripts/outbound-review.py) sees them too:

  * a resident process (ea-bot) appends to the same `fetch-url.log`, the
    only log it can write;
  * an owner process (the mediator, an owner cron) appends to the
    owner-private `outbound-owner.jsonl` (log_paths.owner_log), which
    ea-bot cannot reach, so it never writes by path into resident space.

Record: {"ts", "event", "source", ...}. The events the review reads:
"fetch" (url, host, method, status), "blocked" (url, reason),
"poll_submit" (host, poll_id, platform, fields, sizes), "render" (the PDF
renderer's refused requests) and "remote_brief" (the writer's brief: size,
hash, the first 200 characters). A URL is capped at URL_MAX characters.
`note` never raises: the record must not break the request it describes.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import owner_trust as _owner_trust
    from log_paths import owner_log
except ModuleNotFoundError:  # imported as preprocessor.outbound_log
    from preprocessor import owner_trust as _owner_trust
    from preprocessor.log_paths import owner_log

RESIDENT_LOG_NAME = "fetch-url.log"
OWNER_LOG_NAME = "outbound-owner.jsonl"
URL_MAX = 4000
BRIEF_HEAD = 200
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def resident_log_path() -> Path:
    """The log a resident process writes (fetch_url's own)."""
    return _owner_trust.ea_dir() / "logs" / RESIDENT_LOG_NAME


def owner_log_path() -> Path:
    """The owner-private log an owner process writes: in log_paths' owner
    log directory, or under EA_PRIVATE_DIR when that names the private
    directory (the same place on an install; a scratch one in a test)."""
    if os.environ.get("EA_PRIVATE_DIR"):
        d = _owner_trust.private_dir() / "logs"
        try:
            d.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            pass
        return d / OWNER_LOG_NAME
    return owner_log(OWNER_LOG_NAME)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(event: str, source: str, **fields) -> dict:
    rec = {"ts": _now(), "event": event, "source": source}
    for k, v in fields.items():
        if v is None:
            continue
        if k == "url":
            v = str(v)[:URL_MAX]
        rec[k] = v
    if "url" in rec and "host" not in rec:
        rec["host"] = host_of(rec["url"])
    return rec


def _append_owner(rec: dict) -> None:
    path = owner_log_path()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | _O_CLOEXEC,
                 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def note(event: str, source: str, **fields) -> None:
    """Append one record to the log this process may write. Never raises."""
    try:
        rec = record(event, source, **fields)
        if _owner_trust.is_owner_process():
            _append_owner(rec)
            return
        path = resident_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | _O_CLOEXEC,
                     0o664)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def note_owner(event: str, source: str, **fields) -> bool:
    """Append one record to the owner-private log only: a record whose
    content the resident must not read or rewrite (the writer's brief). A
    process that is not the owner writes nothing. Never raises; True when
    the record was written."""
    try:
        if not _owner_trust.is_owner_process():
            return False
        _append_owner(record(event, source, **fields))
        return True
    except Exception:
        return False


def brief_fields(brief: str) -> dict:
    """What the owner keeps of a brief sent to the remote writer: its size,
    a hash of the whole text and its first BRIEF_HEAD characters."""
    text = brief or ""
    return {"brief_chars": len(text),
            "brief_sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "brief_head": text[:BRIEF_HEAD]}
