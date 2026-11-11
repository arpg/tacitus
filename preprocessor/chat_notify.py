#!/usr/bin/env python3
"""Zero-token notifier for cron agents, plugins and the preprocessor.

post() holds the quiet-hours gate and the SPLIT_MARKER split, then
routes by the configured chat frontend (`chat.frontend` in
ea-config.json):

  * slack: one incoming-webhook POST per part, the same payloads
    slack_notifier always sent. Webhook URLs come from
    slack_webhooks.json in the private dir; only a process that is not
    the owner's falls back to ~/.ea/config. A webhook returns no message
    id, so beside a web chat shadow the post is not mirrored into it.
  * web: the parts go into the web chat's store as the bot, all in one
    new thread, through the writer-only web front end
    (chat.load_frontend(for_cli=True)); the bot's tail thread pushes
    them to the open streams. The channel is the one
    `chat.web.notify[<channel_key>].channel` names, else the EA channel
    (Scheduling for the `scheduling` key while the store holds it).
    A digest key (DIGEST_KEYS) always goes to the digest channel, which
    is made when it is missing. Only an owner process writes the store,
    which is the owner's; any other process posts nothing and says so.
  * memory: parts are recorded in MEMORY_POSTS (tests).

post_digest() posts a mail digest where no Claude lane reads it: the
digest channel on the web chat, the `mail-digest` webhook on Slack
(Mail digests, below).

On the web chat, post() and post_digest() return the key of the thread
the post opened (a non-empty str) where they would return True, so a
caller can link to it (usage_tips: the push of a weekly tip opens its
thread).

post_as_bot() is the direct post with bot credentials that owner-side
jobs (scheduled_sends, owner plugins) use for alerts into the EA
channel. llm_tool_grants() names the chat tools the LLM gets on the
configured platform, without building a client.

CLI:
    chat_notify.py llm-grants
    chat_notify.py post --key personal --text "..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
WEBHOOKS_FILE = EA_DIR / "config" / "slack_webhooks.json"

# Multi-post split marker. When this string appears in the text passed
# to post(), the text is split on the marker and each part is posted as
# its own top-level message (earlier parts land higher in scrollback).
# Skills that already emit the literal value keep working, so it must not
# be renamed.
SPLIT_MARKER = "---===FF_BRIEFING_SPLIT===---"

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
SLACK_LLM_GRANTS = ("mcp__claude_ai_Slack__slack_read_channel,"
                    "mcp__claude_ai_Slack__slack_read_thread,"
                    "mcp__claude_ai_Slack__slack_read_user_profile")

# What the memory frontend "sent": (channel_key, text, blocks) tuples.
MEMORY_POSTS: list[tuple] = []


def _chat_pkg():
    try:
        import chat
    except ImportError:
        from preprocessor import chat
    return chat


def _ea_config():
    try:
        import ea_config
    except ImportError:
        from preprocessor import ea_config
    return ea_config


def _chat_cfg() -> dict:
    return _ea_config().get_chat()


def frontend_name() -> str:
    """The configured chat frontend ("slack" when unset)."""
    return str(_chat_cfg().get("frontend") or "slack")


def _owner_trust():
    try:
        import owner_trust
    except ImportError:
        from preprocessor import owner_trust
    return owner_trust


def _secret(name: str) -> str:
    """A credential by the owner_trust rule: the environment, then the
    owner-private secrets.env, then EA_DIR/.env.secrets if it passes the
    ownership checks; "" when none has it. ~/.ea/.env is never read for
    a secret: the resident can write it."""
    ot = _owner_trust()
    return ot.owner_secret(name, legacy=EA_DIR / ot.LEGACY_SECRETS_FILE)


# ── Slack webhooks ────────────────────────────────────────────────────

def _webhooks_path() -> Path:
    """The webhook file this process reads. An owner process
    (owner_trust.is_owner_process) reads only the copy in the owner-private
    directory: the copy in ~/.ea/config is ea-bot space, and a URL there
    would carry the owner's posts wherever ea-bot points it. Any other
    process takes the private copy when it can see it, else the config copy."""
    ot = _owner_trust()
    private = ot.private_dir() / "slack_webhooks.json"
    if ot.is_owner_process():
        return private
    for p in (private, WEBHOOKS_FILE):
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return WEBHOOKS_FILE


def _load_webhooks() -> dict:
    """{channel key: webhook URL}. An owner process reads the private copy
    with the owner checks (owner_trust.read_owner_file: owned by this user,
    not a link, not writable by others); a copy that fails them gives {}
    with a warning."""
    path = _webhooks_path()
    ot = _owner_trust()
    try:
        if ot.is_owner_process():
            data = json.loads(ot.read_owner_file(path))
        elif path.exists():
            data = json.loads(path.read_text())
        else:
            return {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, ot.OwnerTrustError) as e:
        print(f"WARNING: webhook file {path} refused: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _send_slack(channel_key: str, parts: list[str], blocks: Optional[list]) -> bool:
    webhooks = _load_webhooks()
    url = webhooks.get(channel_key)
    if not url:
        print(f"WARNING: No webhook configured for '{channel_key}'")
        print(f"  Configure in {_webhooks_path()}")
        print(f"  Message was: {(parts[0] if parts else '')[:100]}...")
        return False

    all_ok = True
    for i, part in enumerate(parts):
        payload = {"text": part}
        # Attach `blocks` only to the LAST part (typical caller pattern:
        # blocks accompany the main briefing, not auxiliary posts).
        if blocks and i == len(parts) - 1:
            payload["blocks"] = blocks
        try:
            resp = requests.post(url, json=payload)
        except requests.RequestException as e:
            print(f"Slack post failed (network error): {e}")
            all_ok = False
            continue
        if resp.status_code != 200:
            print(f"Slack post failed ({resp.status_code}): {resp.text}")
            all_ok = False
    return all_ok


# ── The web chat's origin note ────────────────────────────────────────

def _note_origin(fe, channel: str, thread_key: str, channel_key: str) -> None:
    """Record in the web chat's store that this notifier post opened the
    thread, under its channel key: the thread-state rules take such a
    thread as an automated post (docs/webchat-state.md, rule 7). `fe` is
    the web front end, or a composite whose shadow is one. Best effort: a
    failure leaves the post as it is."""
    for web in (fe, getattr(fe, "shadow", None)):
        note = getattr(getattr(web, "store", None), "note_origin", None) if web is not None else None
        if callable(note):
            try:
                note(channel, thread_key, channel_key)
            except Exception as e:
                print(f"Web chat origin note failed: {e}")
            return


# ── Project rooms ─────────────────────────────────────────────────────
# A room is addressed by its channel id, on every frontend, through the
# chat abstraction (chat.load_frontend): `post(channel, text)` with no
# thread opens a new top-level post. The owner's own record (owner_rooms)
# is the authority on which rooms exist; the registry in ea-bot space is
# never consulted here, and a process that cannot read the owner record
# posts nowhere.

ROOM_KEY_PREFIX = "room:"
_ROOM_FE: list = []


def room_key(channel_id: str) -> str:
    """The channel key that posts to one project room (a new thread)."""
    return ROOM_KEY_PREFIX + str(channel_id)


def _owner_room(channel_id: str) -> dict | None:
    """The owner record's row for a room the bot may post in, or None."""
    try:
        try:
            import owner_rooms
        except ImportError:
            from preprocessor import owner_rooms
        row = owner_rooms.get(channel_id)
    except Exception:
        return None
    if not row or row.get("archived_by_bot"):
        return None
    return row


def _room_frontend():
    """One sender-only frontend per process, for the room posts."""
    if not _ROOM_FE:
        _ROOM_FE.append(_chat_pkg().load_frontend(for_cli=True))
    return _ROOM_FE[0]


def room_mention(channel_id: str) -> str:
    """The room as a link in the frontend's own syntax; `#<name>` from
    the owner record when the frontend cannot say."""
    row = _owner_room(channel_id) or {}
    try:
        return _room_frontend().mention(channel_id)
    except Exception:
        return f"#{row.get('name') or channel_id}"


# ── The web chat alone ────────────────────────────────────────────────
# With `"frontend": "web"` the store is the chat: a post goes into it as
# the bot, and the bot's tail thread pushes it to the open streams
# (docs/webchat.md, CLI senders). The `web.notify` map takes the
# notifier's channel keys, each naming a web channel id
# ({"ea-assistant": {"channel": "main"}}); a key it does not name goes to
# the EA channel, and `scheduling` to the Scheduling channel while the
# store holds it. A digest key goes to the digest channel whatever the
# map says: the briefing's chat reader reads the EA channel, and a
# channel of kind `digest` never.

WEB_DIGEST_ID = "digests"
WEB_DIGEST_NAME = "Mail digests"
WEB_DIGEST_KIND = "digest"
WEB_DIGEST_ABOUT = "Mail digests written by the remote model. Claude does not read this channel."


def _web_cfg() -> dict:
    web = _chat_cfg().get("web")
    return web if isinstance(web, dict) else {}


def _web_target(channel_key: str) -> str:
    """The web channel `web.notify[<channel_key>]` names, or ""."""
    notify = _web_cfg().get("notify")
    entry = notify.get(channel_key) if isinstance(notify, dict) else None
    return str(entry.get("channel") or "") if isinstance(entry, dict) else ""


def _web_sender():
    """The writer-only web front end on the owner's store, or None (with
    the reason printed) in a process that is not the owner's: the copy such
    a process could open is one the bot never reads."""
    if not _owner_trust().is_owner_process():
        print("WARNING: the web chat's store is the owner's and this process is not; "
              "nothing posted (docs/webchat.md, CLI senders)")
        return None
    fe = _room_frontend()
    fe.start()
    return fe


def _web_channel(fe, cid: str) -> bool:
    """True when the store holds channel `cid`. The EA channel, the DM and
    the digest channel are made here when the bot has not made them yet."""
    if fe.store.channel(cid) is not None:
        return True
    dm = fe.dm_channel(fe.owner_id)
    own = {fe.main_channel: (getattr(fe, "main_channel_name", "") or fe.main_channel, "main"),
           dm: (dm, "dm"),
           WEB_DIGEST_ID: (WEB_DIGEST_NAME, WEB_DIGEST_KIND, False, WEB_DIGEST_ABOUT)}
    if cid not in own:
        return False
    fe.store.upsert_channel(cid, *own[cid])
    return True


def _web_sched(fe) -> str:
    """The web chat's Scheduling channel while the store holds it and it
    is not archived (the bot archives it when the web block turns it
    off), else ""."""
    cid = str(getattr(fe, "sched_channel", "") or "")
    row = fe.store.channel(cid) if cid else None
    return cid if row and row.get("kind") == "sched" and not row.get("archived") else ""


def _web_route(fe, channel_key: str, direct: bool = False) -> str:
    """The web channel a key posts to: the one `web.notify` names, else
    the DM for a direct message, Scheduling for the `scheduling` key while
    that channel is there, and the EA channel for the rest. A digest key
    never comes here (_send_digest names its channel)."""
    target = _web_target(channel_key)
    if target:
        return target
    if direct:
        return fe.dm_channel(fe.owner_id)
    if channel_key == "scheduling":
        return _web_sched(fe) or fe.main_channel
    return fe.main_channel


def _send_web(channel_key: str, parts: list[str], *, channel: str = "",
              direct: bool = False) -> bool | str:
    """Post the parts in one new thread: in `channel` when given, else
    where _web_route sends the key. The new thread's key, False on a
    failure."""
    try:
        fe = _web_sender()
        if fe is None:
            return False
        cid = channel or _web_route(fe, channel_key, direct)
        if not _web_channel(fe, cid):
            print(f"WARNING: web chat channel {cid!r} (for '{channel_key}') does not exist; "
                  "nothing posted")
            return False
        # The key goes with the post, so the push policy reads the thread's
        # origin when it decides (a briefing pushes; another notice does not).
        first = fe.post(cid, parts[0], origin=channel_key)
        for part in parts[1:]:
            fe.post(cid, part, thread_ts=first.thread_key)
        return first.thread_key or True
    except Exception as e:
        print(f"Web chat post failed: {e}")
        return False


def _send_room(channel_key: str, parts: list[str]) -> bool | str:
    cid = channel_key[len(ROOM_KEY_PREFIX):]
    if _owner_room(cid) is None:
        print(f"WARNING: '{channel_key}' is not a room in the owner's record; nothing posted")
        return False
    try:
        fe = _room_frontend()
        first = fe.post(cid, parts[0])
        _note_origin(fe, cid, getattr(first, "thread_key", ""), channel_key)
        for part in parts[1:]:
            fe.post(cid, part, thread_ts=first.thread_key)
        return (first.thread_key or True) if frontend_name() == "web" else True
    except Exception as e:
        print(f"Room post to {cid} failed: {e}")
        return False


# ── Mail digests ──────────────────────────────────────────────────────
# A digest carries what the remote model read for the owner alone: the
# mail the gate leaves out of the Claude lanes (mail_gate digest), the
# list and admissions digests (remote_digests) and the briefing preview
# (remote_briefing --preview). It goes where no Claude lane reads: the
# digest channel on the web chat, the `mail-digest` webhook on Slack. A digest key never waits in the
# quiet-hours queue, which sits in the resident's space.

DIGEST_STREAM = "mail-digest"
DIGEST_KEYS = ("mail-digest", "list-digest", "admissions-digest", "briefing-preview")


def _send_digest(key: str, parts: list[str]) -> bool | str | None:
    """The parts of one digest in one thread at the digest place; None
    when the frontend has none (post_digest)."""
    name = frontend_name()
    if name == "web":
        return _send_web(key, parts, channel=WEB_DIGEST_ID)
    if name == "slack":
        if not _load_webhooks().get(DIGEST_STREAM):
            return None
        return _send_slack(DIGEST_STREAM, parts, None)
    if name == "memory":
        MEMORY_POSTS.extend((key, part, None) for part in parts)
        return True
    return None


def post_digest(key: str, text: str) -> bool | str | None:
    """Post one digest. `key` names it (DIGEST_KEYS; the web chat records
    it as the thread's notifier key). Web chat: a new thread in the digest
    channel, made when it is missing. Slack: the `mail-digest`
    webhook. True when it posted (the thread's key on the web chat), False
    when the post failed, None when the frontend has no such place (Slack
    without that webhook, or no chat): the caller decides, and nothing
    goes to a channel a Claude lane reads. post() routes a digest key here
    too."""
    return _send_digest(key, [text])


def web_direct(text: str) -> bool:
    """A direct message on the web chat alone (chat.direct.send_direct):
    a new thread in the DM, or in the channel `web.notify` names for
    `personal`."""
    return bool(_send_web("personal", [text], direct=True))


# ── Public API ────────────────────────────────────────────────────────

def post(channel_key: str, text: str, *, blocks: Optional[list] = None,
         priority: str = "normal", _bypass_quiet: bool = False) -> bool | str:
    """Post a message to the configured chat platform.

    If `text` contains `SPLIT_MARKER`, splits into multiple top-level
    messages and posts them sequentially. Otherwise posts as a single
    message.

    `priority`: "normal" or "urgent". When the user is in local quiet hours
    (see quiet_hours.py / travel mode), a deferrable post is HELD in a queue
    and delivered as one digest after quiet hours end, rather than buzzing
    the phone at night. `_bypass_quiet=True` skips the gate (used by the
    quiet-hours flush itself, and any caller that must post immediately);
    a mail digest key (DIGEST_KEYS) always skips it.

    Returns True if every part posted successfully OR if the post was
    deferred (so cron jobs / plugin-state commits treat a deferral as a
    successful handoff). Returns False on a real send failure. A post
    that went out on the web chat returns the key of the thread it opened
    instead of True (a non-empty str): truthy wherever True is.
    """
    if not _bypass_quiet and channel_key not in DIGEST_KEYS:
        try:
            import quiet_hours
            if quiet_hours.should_defer(priority):
                quiet_hours.defer(channel_key, text, priority)
                print(f"[quiet-hours] held post to '{channel_key}' "
                      f"(priority={priority}); will deliver after quiet hours")
                return True
        except Exception as e:
            # Fail open: if the gate errors, deliver normally.
            print(f"[quiet-hours] gate skipped ({e})")

    if SPLIT_MARKER in text:
        parts = [p.strip() for p in text.split(SPLIT_MARKER)]
        parts = [p for p in parts if p]
    else:
        parts = [text]

    name = frontend_name()
    if channel_key in DIGEST_KEYS:
        sent = _send_digest(channel_key, parts)
        if sent is None:
            print(f"WARNING: no digest channel on the {name} chat for '{channel_key}' "
                  "(on Slack, a `mail-digest` webhook); nothing posted")
        return sent or False
    if channel_key.startswith(ROOM_KEY_PREFIX) and name != "memory":
        return _send_room(channel_key, parts)
    if name == "slack":
        return _send_slack(channel_key, parts, blocks)
    if name == "web":
        return _send_web(channel_key, parts)
    if name == "memory":
        for i, part in enumerate(parts):
            MEMORY_POSTS.append((channel_key, part,
                                 blocks if blocks and i == len(parts) - 1 else None))
        return True
    print(f"WARNING: unknown chat frontend {name!r}; nothing posted")
    return False


def scheduling_channel_key() -> str:
    """The channel key for scheduling output: "scheduling" when that
    channel is configured (on the web chat alone, also while its
    Scheduling channel is on), else the main EA channel."""
    name = frontend_name()
    if name == "slack":
        return "scheduling" if _load_webhooks().get("scheduling") else "ea-assistant"
    if name == "web":
        on = _web_target("scheduling") or _ea_config().web_sched_on(_chat_cfg())
        return "scheduling" if on else "ea-assistant"
    return "ea-assistant"


def calendar_channel_key() -> str:
    """The channel key for calendar-protection warnings: "calendar" when
    that target is configured, else the scheduling key, which itself falls
    back to the main EA channel."""
    name = frontend_name()
    if name == "slack":
        return "calendar" if _load_webhooks().get("calendar") else scheduling_channel_key()
    if name == "web":
        return "calendar" if _web_target("calendar") else scheduling_channel_key()
    return "ea-assistant"


def health_channel_key() -> str:
    """The channel key for health and boundary checks (sentinel, the boundary
    audit, the outbound-web and preference-rule reviews): "health" when that
    channel is configured, else the main EA channel."""
    name = frontend_name()
    if name == "slack":
        return "health" if _load_webhooks().get("health") else "ea-assistant"
    if name == "web":
        return "health" if _web_target("health") else "ea-assistant"
    return "ea-assistant"


def post_as_bot(channel: str, text: str, *, token: Optional[str] = None,
                log=None) -> bool:
    """Post text to a channel with the bot's own credentials.

    Slack: a bearer POST to chat.postMessage with {"channel", "text"};
    `token` defaults to SLACK_BOT_TOKEN in the environment, and a missing
    token or channel posts nothing. The web chat: `channel` is ignored
    and the text goes to the EA channel unless `web.notify` names another
    for "ea-assistant". `log`,
    when given, is called with a one-line reason on failure. Returns True when the
    platform accepted the message.
    """
    def _fail(detail: str) -> bool:
        if log is not None:
            log(detail)
        return False

    name = frontend_name()
    if name == "slack":
        token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "")
        if not token or not channel:
            return _fail("missing SLACK_BOT_TOKEN or channel")
        try:
            resp = requests.post(
                SLACK_POST_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "text": text},
                timeout=10,
            )
        except Exception as e:
            return _fail(f"error: {e}")
        try:
            accepted = bool(resp.ok and resp.json().get("ok"))
        except Exception:
            accepted = False
        return True if accepted else _fail(str(getattr(resp, "text", ""))[:120])
    if name == "web":
        return True if _send_web("ea-assistant", [text]) else _fail("web chat post failed")
    if name == "memory":
        MEMORY_POSTS.append((channel, text, None))
        return True
    return _fail(f"unknown chat frontend {name!r}")


def llm_tool_grants() -> str:
    """Extra tool names the LLM gets on the configured platform, comma
    separated; "" when there are none. No client is built."""
    return SLACK_LLM_GRANTS if frontend_name() == "slack" else ""


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Chat notifier")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("llm-grants", help="print the LLM tool grants for the configured frontend")
    p = sub.add_parser("post", help="post a message through the notifier")
    p.add_argument("--key", required=True, help="channel key, e.g. personal or ea-assistant")
    p.add_argument("--text", required=True)
    p.add_argument("--priority", default="normal", choices=("normal", "urgent"))
    p.add_argument("--bypass-quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "llm-grants":
        print(llm_tool_grants())
        return 0
    return 0 if post(a.key, a.text, priority=a.priority,
                     _bypass_quiet=a.bypass_quiet) else 1


if __name__ == "__main__":
    sys.exit(main())
