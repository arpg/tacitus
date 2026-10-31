"""Direct messages to the owner: the loud channel.

A stream post is read when the owner opens the stream. A direct message
notifies the owner's devices (Zulip pushes direct messages by default),
so it is the channel for a moment that must not be missed: a ticket
on-sale, a registration window, a deadline with a clock time.

Zulip also schedules a direct message for later delivery on the server,
so an alert set in the morning fires on time whatever the bot is doing.
On a Slack install there is no server-side schedule; `send_direct`
falls back to an urgent post in the personal channel with a mention, and
`schedule_direct` raises.

Requires the owner's chat credential (owner_trust), so these run in owner
lanes: the owner's shell, cron as the owner, and `alert.py flush`.

With the web chat as a shadow, `frontend()` is the composite
(docs/webchat.md, CLI senders); its client is the primary's. A Zulip
direct message is not mirrored into the web chat: it lives outside the
streams the web chat holds.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import (preprocessor.chat.direct) or path import (chat.direct)
    from . import load_frontend
except ImportError:  # pragma: no cover
    from chat import load_frontend  # type: ignore


def frontend():
    return load_frontend(for_cli=True)


def is_zulip(fe) -> bool:
    return hasattr(fe, "client") and hasattr(fe, "owner_id") and hasattr(fe, "_call")


def owner_mention(fe) -> str:
    try:
        return fe._owner_mention()
    except Exception:
        return ""


def send_direct(text: str, fe=None) -> dict:
    """Send `text` to the owner now. Returns {"id": message id} on Zulip,
    or {"posted": True} on the fallback path."""
    fe = fe or frontend()
    if is_zulip(fe):
        req = {"type": "private", "to": [int(fe.owner_id)], "content": text}
        res = fe.client.send_message(req)
        if res.get("result") != "success":
            raise RuntimeError(f"direct message failed: {res.get('msg')}")
        return {"id": int(res["id"])}
    import chat_notify  # type: ignore
    ok = chat_notify.post("personal", text, priority="urgent", _bypass_quiet=True)
    if not ok:
        raise RuntimeError("fallback post to the personal channel failed")
    return {"posted": True}


def schedule_direct(text: str, when: datetime, fe=None) -> int:
    """Schedule `text` as a direct message to the owner at `when` (an
    aware datetime). Returns the server's scheduled_message_id."""
    fe = fe or frontend()
    if not is_zulip(fe):
        raise RuntimeError("scheduled direct messages need the Zulip frontend")
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware")
    res = fe._call("scheduled_messages", "POST", {
        "type": "direct", "to": json.dumps([int(fe.owner_id)]),
        "content": text, "scheduled_delivery_timestamp": int(when.timestamp())})
    if res.get("result") != "success":
        raise RuntimeError(f"schedule failed: {res.get('msg')}")
    return int(res["scheduled_message_id"])


def list_scheduled(fe=None) -> list[dict]:
    """The bot's pending scheduled messages, direct ones only."""
    fe = fe or frontend()
    if not is_zulip(fe):
        return []
    res = fe._call("scheduled_messages")
    out = []
    for m in res.get("scheduled_messages", []):
        if m.get("type") not in ("private", "direct"):
            continue
        out.append({"id": m["scheduled_message_id"],
                    "at": datetime.fromtimestamp(m["scheduled_delivery_timestamp"], timezone.utc),
                    "content": m.get("content", "")})
    return out


def cancel_scheduled(sid: int, fe=None) -> bool:
    fe = fe or frontend()
    if not is_zulip(fe):
        return False
    res = fe._call(f"scheduled_messages/{int(sid)}", "DELETE")
    return res.get("result") == "success"
