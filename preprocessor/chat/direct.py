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

On the web chat alone (`"frontend": "web"`) the store is the chat:
`send_direct` posts in the owner's DM channel, or in the channel
`web.notify` names for `personal`; `schedule_direct` queues the post in
the store (`scheduled_posts`), and
the bot posts it in the owner's DM channel when it is due
(`WebFrontend.post_due`, on its tail thread). `cancel_scheduled` drops a
queued one and `list_scheduled` lists them, as on Zulip. The bot must be
up to post it; one it was down for goes out when it is back. Only an
owner process opens that store: in any other `frontend()` raises, so
alert.py stages the alert for the owner's flush.

Requires the owner's chat credential (owner_trust), so these run in owner
lanes: the owner's shell, cron as the owner, and `alert.py flush`.

With the web chat as a shadow, `frontend()` is the composite
(docs/webchat.md, CLI senders); its client is the primary's. A Zulip
direct message is not mirrored into the web chat: it lives outside the
streams the web chat holds.

`web_notify` also pushes the message to the browsers that turned on
notifications in the web chat (docs/webchat.md, Notifications), now or,
for a scheduled alert, at its moment: the bot's push worker sends a push
queued in the web chat's store. Without a web front end in the chat
block it does nothing. On the web chat alone `send_direct`'s post in the
DM pushes as well; the web chat drops the second push of one text within
ten seconds (web_push_policy), so the owner gets one, at high urgency.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import (preprocessor.chat.direct) or path import (chat.direct)
    from . import load_frontend, load_web_frontend
except ImportError:  # pragma: no cover
    from chat import load_frontend, load_web_frontend  # type: ignore


def frontend():
    fe = load_frontend(for_cli=True)
    if is_web(fe) and not _owner_process():
        raise PermissionError("the web chat's store is the owner's, and this process is not "
                              "(docs/webchat.md, CLI senders)")
    return fe


def is_zulip(fe) -> bool:
    return hasattr(fe, "client") and hasattr(fe, "owner_id") and hasattr(fe, "_call")


def is_web(fe) -> bool:
    """The web chat alone (`"frontend": "web"`), not a composite."""
    return getattr(fe, "name", "") == "web" and hasattr(fe, "schedule_post")


def _owner_process() -> bool:
    try:  # the chat package's owner_trust, which the tests patch
        from . import owner_trust
    except ImportError:  # pragma: no cover
        import owner_trust  # type: ignore
    return owner_trust.is_owner_process()


def owner_mention(fe) -> str:
    try:
        return fe._owner_mention()
    except Exception:
        return ""


def send_direct(text: str, fe=None) -> dict:
    """Send `text` to the owner now. Returns {"id": message id} on Zulip,
    or {"posted": True} on the web chat and on the fallback path."""
    fe = fe or frontend()
    if is_zulip(fe):
        req = {"type": "private", "to": [int(fe.owner_id)], "content": text}
        res = fe.client.send_message(req)
        if res.get("result") != "success":
            raise RuntimeError(f"direct message failed: {res.get('msg')}")
        return {"id": int(res["id"])}
    import chat_notify  # type: ignore
    if is_web(fe):
        if not chat_notify.web_direct(text):
            raise RuntimeError("direct message to the web chat's DM failed")
        return {"posted": True}
    ok = chat_notify.post("personal", text, priority="urgent", _bypass_quiet=True)
    if not ok:
        raise RuntimeError("fallback post to the personal channel failed")
    return {"posted": True}


def schedule_direct(text: str, when: datetime, fe=None) -> int:
    """Schedule `text` as a direct message to the owner at `when` (an
    aware datetime). Returns the server's scheduled_message_id; on the web
    chat alone, the id of the post queued in its store."""
    fe = fe or frontend()
    if is_web(fe):
        return _schedule_web(text, when, fe)
    if not is_zulip(fe):
        raise RuntimeError("scheduled direct messages need the Zulip or the web frontend")
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware")
    res = fe._call("scheduled_messages", "POST", {
        "type": "direct", "to": json.dumps([int(fe.owner_id)]),
        "content": text, "scheduled_delivery_timestamp": int(when.timestamp())})
    if res.get("result") != "success":
        raise RuntimeError(f"schedule failed: {res.get('msg')}")
    return int(res["scheduled_message_id"])


def _schedule_web(text: str, when: datetime, fe) -> int:
    """The post waits in the web chat's store; the bot posts it in the DM
    channel at `when`."""
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware")
    if not _owner_process():
        raise PermissionError("the web chat's store is the owner's, and this process is not")
    return int(fe.schedule_post(fe.dm_channel(fe.owner_id), text, when.timestamp()))


def list_scheduled(fe=None) -> list[dict]:
    """The bot's pending scheduled messages, direct ones only."""
    fe = fe or frontend()
    if is_web(fe):
        dm = fe.dm_channel(fe.owner_id)
        return [{"id": int(r["id"]), "at": datetime.fromtimestamp(r["due_at"], timezone.utc),
                 "content": r["text"] or ""} for r in fe.scheduled_posts() if r["channel"] == dm]
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
    if is_web(fe):
        return fe.cancel_scheduled_post(int(sid))
    if not is_zulip(fe):
        return False
    res = fe._call(f"scheduled_messages/{int(sid)}", "DELETE")
    return res.get("result") == "success"


def web_notify(text: str, *, tag: str = "", when: datetime | None = None,
               ea_dir: str | Path | None = None, urgency: str = "high") -> dict:
    """Push `text` (its first line) to the web chat's subscribed browsers at
    `urgency` (high for an alert; a weekly usage tip passes normal): now, or
    at `when` (aware) through the store's queue under `tag`, replacing a
    push queued there before. A push now is dropped when the web chat pushed
    the same text in the last ten seconds: the direct message `send_direct`
    posted in the DM just before (web_push_policy). {"web": "off"} when the
    chat block runs no web front end or its store does not exist yet; never
    raises, so the chat post stands whatever push does."""
    try:
        fe = load_web_frontend(ea_dir=ea_dir)
        if fe is None:
            return {"web": "off"}
        if not Path(fe.store_path).exists():
            return {"web": "off", "why": "no web chat store yet"}
        at = when.timestamp() if when is not None else None
        return fe.notify("Tacitus", text, fe.app_url(), tag, urgency=urgency, at=at, wait=True,
                         dedupe=True)
    except Exception as e:
        return {"web": "error", "why": f"{type(e).__name__}: {e}"}


def web_cancel(tag: str, *, ea_dir: str | Path | None = None) -> bool:
    """Drop the web push queued under `tag`; False when there was none or
    there is no web front end. Never raises."""
    try:
        fe = load_web_frontend(ea_dir=ea_dir)
        if fe is None or not Path(fe.store_path).exists():
            return False
        return bool(fe.cancel_notify(tag))
    except Exception:
        return False
