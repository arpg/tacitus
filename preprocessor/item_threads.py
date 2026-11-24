"""One conversation in the EA channel per mail conversation, on the web chat.

Two posters open a thread in the EA channel about a mail item the owner
never asked about: the priority-email alert (slack_notifier,
notify_priority_emails, through chat_notify.post) and a hand-off from a
briefing (ea-chat-bot.py, _remote_escalation_tick). Each records here the
thread it opened under the mail's conversation id. The later one posts
into that thread instead of opening another (the bot runs a hand-off's
turn there with the thread as history), and an alert about a message the
thread already carries posts nothing.

Web chat only (chat_notify.frontend_name): Slack keeps one post per alert.
A thread older than KEEP_DAYS is not reused; the next post opens a new one
and takes its place. Only owner processes read or write the record, through
owner_trust; a record that fails the owner checks counts as none.

    {"<conversation id>": {"channel": "main", "thread": "t812", "at": 1790000000,
                           "messages": ["<message id>", ...]}}
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

try:
    from . import owner_trust
except ImportError:  # run from preprocessor/
    import owner_trust

FILE = "item-threads.json"
KEEP_DAYS = 14
MESSAGES_MAX = 20
_LOCK = threading.Lock()


def path() -> Path:
    return owner_trust.private_dir() / FILE


def on() -> bool:
    """True on the web chat, where a post can join a thread by its key."""
    try:
        try:
            import chat_notify
        except ImportError:
            from preprocessor import chat_notify
        return chat_notify.frontend_name() == "web"
    except Exception:
        return False


def _load() -> dict:
    try:
        doc = json.loads(owner_trust.read_owner_file(path()))
    except (OSError, ValueError, owner_trust.OwnerTrustError):
        return {}
    return {str(k): v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


@contextlib.contextmanager
def _locked():
    """This process's lock, and the record's lock file across processes
    (the bot and a cron alert may write at once)."""
    with _LOCK:
        fd = None
        try:
            import fcntl
            p = path()
            fd = os.open(str(p.with_name(p.name + ".lock")),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            fd = None
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)


def find(conversation_id: str, *, now: float | None = None) -> dict | None:
    """{"channel", "thread", "messages"} of the conversation's thread, or
    None: none recorded, older than KEEP_DAYS, or the record unreadable."""
    if not conversation_id:
        return None
    row = _load().get(str(conversation_id))
    if not row or not row.get("channel") or not row.get("thread"):
        return None
    if (now or time.time()) - float(row.get("at") or 0) > KEEP_DAYS * 86400:
        return None
    return {"channel": str(row["channel"]), "thread": str(row["thread"]),
            "messages": [str(m) for m in row.get("messages") or []]}


def carries(conversation_id: str, message_id: str) -> bool:
    """The conversation's thread already carries this message."""
    row = find(conversation_id)
    return bool(row and message_id and message_id in row["messages"])


def record(conversation_id: str, channel: str, thread: str, message_ids=(), *,
           now: float | None = None) -> None:
    """Name `thread` in `channel` as the conversation's one thread and add
    the messages it now carries. Entries past KEEP_DAYS fall off."""
    if not (conversation_id and channel and thread):
        return
    now = now or time.time()
    with _locked():
        rows = {k: v for k, v in _load().items()
                if now - float(v.get("at") or 0) <= KEEP_DAYS * 86400}
        row = rows.get(conversation_id) or {}
        msgs = [] if row.get("thread") != thread else list(row.get("messages") or [])
        msgs += [m for m in message_ids if m and m not in msgs]
        rows[conversation_id] = {"channel": channel, "thread": thread, "at": now,
                                 "messages": msgs[-MESSAGES_MAX:]}
        p = path()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner_trust.write_owner_file(p, json.dumps(rows, indent=1, sort_keys=True) + "\n",
                                     mode=0o600)
