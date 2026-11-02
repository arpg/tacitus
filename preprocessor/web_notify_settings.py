"""The web chat's Notifications settings: which of the bot's posts notify
the owner's devices (docs/webchat.md, Notifications (Web Push)).

The switches are the web block's `notify_push` in the owner-private
chat.json (botcore's web_push_policy.Settings reads it):

  briefings  the morning briefing and the afternoon update
  replies    the bot's answer in a conversation the owner wrote in, in
             the EA and scheduling channels
  rooms      the same in the project rooms
  quiet      "queue" (those three wait for the end of the owner's quiet
             hours) or "send" (they go at once)

Cards, the DM and alerts push whatever the block says.

`NotifySettings` is the page botcore's WebFrontend serves at
/api/settings/notify (ea-chat-bot.py registers it beside the routing
table): GET is the settings in force, POST takes the keys to change. A
save merges them over the block in chat.json, writes the file through
owner_trust.write_owner_file (owner processes only, mode 0600, every
other key kept) and puts the new settings in force in the running front
end at once; a command-line sender reads the file on its next run. The
front end refuses a relayed save: settings change only in the owner's
own web chat. What the page says is in plain words (tests/plain_words.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any

try:
    import owner_trust as _ot
    from chat import web_frontend as _wf
except ImportError:  # imported as preprocessor.web_notify_settings
    from preprocessor import owner_trust as _ot
    from preprocessor.chat import web_frontend as _wf

log = logging.getLogger("chat.web")

_policy = _wf.web_push_policy
# The switches, by the name the app shows for each.
SWITCHES = {"briefings": "Briefings", "replies": "Replies", "rooms": "Rooms"}
QUIET = _policy.QUIET_MODES


class NotifyRefused(ValueError):
    """A change the page refuses; the message says why in plain words."""


def payload(settings: Any) -> dict:
    """The page's data: each switch and the quiet-hours choice."""
    return {"briefings": bool(settings.briefings), "replies": bool(settings.replies),
            "rooms": bool(settings.rooms), "quiet": str(settings.quiet)}


def checked(body: Any) -> dict:
    """The changes a POST body names, each checked: a switch takes true or
    false, `quiet` takes "queue" or "send". Raises NotifyRefused."""
    if not isinstance(body, dict) or not body:
        raise NotifyRefused("Choose at least one notification setting to change.")
    out: dict = {}
    for key, value in body.items():
        if key in SWITCHES:
            if not isinstance(value, bool):
                raise NotifyRefused(f'"{SWITCHES[key]}" takes on or off.')
            out[key] = value
        elif key == "quiet":
            if value not in QUIET:
                raise NotifyRefused('Quiet hours take "Hold until morning" or "Send anyway".')
            out[key] = value
        else:
            raise NotifyRefused(f"Tacitus has no notification setting named {str(key)[:40]!r}.")
    return out


def save(body: Any, who: dict | None = None) -> Any:
    """Merge the changes in `body` over the web block's `notify_push` in the
    owner-private chat.json and write it; the settings now in force
    (web_push_policy.Settings). The log names the credential in `who` that
    signed the save in. Raises NotifyRefused."""
    changes = checked(body)
    if not _ot.is_owner_process():
        raise NotifyRefused("Tacitus could not save: only the part of Tacitus that runs as you "
                            "may change these settings.")
    path = _ot.private_dir() / _ot.CHAT_FILE
    try:
        block = json.loads(_ot.read_owner_file(path))
    except (OSError, ValueError, _ot.OwnerTrustError) as e:
        log.warning("web chat: notification settings not saved: %s is unreadable: %s", path, e)
        raise NotifyRefused("Tacitus could not read its settings file.") from None
    if not isinstance(block, dict):
        raise NotifyRefused("Tacitus could not read its settings file.")
    web = block.get("web") if isinstance(block.get("web"), dict) else {}
    current = web.get("notify_push") if isinstance(web.get("notify_push"), dict) else {}
    merged = {**current, **changes}
    block["web"] = {**web, "notify_push": merged}
    try:
        _ot.write_owner_file(path, json.dumps(block, indent=2) + "\n", mode=0o600)
    except (OSError, _ot.OwnerTrustError) as e:
        log.warning("web chat: notification settings not saved: %s was not written: %s", path, e)
        raise NotifyRefused("Tacitus could not write the settings file.") from None
    log.info("web chat: notification settings saved (%s): %s",
             str((who or {}).get("credential") or "unknown")[:20], json.dumps(changes, sort_keys=True))
    return _policy.Settings.from_config(merged)


class NotifySettings:
    """The Notifications section of the web chat's Settings screen: botcore's
    WebFrontend serves it at /api/settings/notify for the front end `fe`,
    whose push settings a save replaces."""

    def __init__(self, fe: Any) -> None:
        self.fe = fe

    def get(self) -> dict:
        return payload(self.fe.push_settings)

    def save(self, body: dict, who: dict | None = None) -> dict:
        settings = save(body, who)
        self.fe.push_settings = settings
        return {**payload(settings), "saved": True}
