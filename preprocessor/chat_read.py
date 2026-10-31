#!/usr/bin/env python3
"""Chat channel reader for the EA preprocessor.

Fetches recent messages from the user's EA channel so the morning
briefing can see overnight replies, approvals, and instructions. Reads
through the configured chat frontend (`chat.load_frontend(for_cli=True)`),
so the same code serves Slack and Zulip. Zero Claude tokens.

The dicts it returns keep the shape slack_reader always produced; see
fetch_recent_messages and summarize_for_briefing.

The owner's web chat messages sit on the primary as the bot's posts
(docs/webchat.md, Shadow mode). A message counts as the owner's only
when its id is in the web chat's owner-private list
(`owner_posted`), which an owner process reads through the composite;
the text's "You (web)" mark proves nothing and is only dropped.

CLI:
    chat_read.py [--hours 24]                     briefing summary as JSON
    chat_read.py --thread <key> --channel <id>    one thread as JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone


def _chat_pkg():
    try:
        import chat
    except ImportError:
        from preprocessor import chat
    return chat


def _frontend():
    """The configured frontend for reading, or None (with a note on
    stderr) when it cannot be built here."""
    try:
        return _chat_pkg().load_frontend(for_cli=True)
    except KeyError as e:
        print(f"  Chat reader: no token or channel ID ({e}), skipping", file=sys.stderr)
    except Exception as e:
        print(f"  Chat reader: frontend unavailable ({e}), skipping", file=sys.stderr)
    return None


def _is_bot(msg, bot_user_id: str) -> bool:
    return bool(msg.is_bot or msg.is_self
                or (bot_user_id and msg.user_id == bot_user_id))


def _owner_check(fe):
    """The frontend's owner_posted(channel, msg_id) when it has one (the
    web chat composite in an owner process), else a check that says no."""
    check = getattr(type(fe), "owner_posted", None) and getattr(fe, "owner_posted", None)
    if not callable(check):
        return lambda channel, msg_id: False

    def owned(channel, msg_id) -> bool:
        try:
            return bool(check(channel, msg_id))
        except Exception:
            return False
    return owned


def _strip_web_prefix(text: str) -> str:
    import importlib
    mod = importlib.import_module(_chat_pkg().__name__ + ".shadow_frontend")
    return mod.strip_web_prefix(text)


def _sender_and_text(msg, channel: str, bot_user_id: str, owned) -> tuple[bool, str]:
    """(is the bot's, text) of a message: a bot post whose id the web chat
    recorded as the owner's input is the owner's, its mark dropped."""
    text = (msg.text or "").strip()
    if _is_bot(msg, bot_user_id) and owned(msg.channel or channel, msg.msg_id or ""):
        return False, _strip_web_prefix(text).strip()
    return _is_bot(msg, bot_user_id), text


def _file_entries(msg) -> list[dict]:
    """[{name, type}] for a message's files. The platform's own file type
    (Slack `filetype`) comes from the raw event when it is there."""
    raw_files = (msg.raw or {}).get("files") if isinstance(msg.raw, dict) else None
    if isinstance(raw_files, list) and raw_files:
        return [{"name": f.get("name", ""), "type": f.get("filetype", "")}
                for f in raw_files if isinstance(f, dict)]
    return [{"name": f.name, "type": f.mimetype} for f in msg.files]


def _has_files(msg) -> bool:
    raw_files = (msg.raw or {}).get("files") if isinstance(msg.raw, dict) else None
    return bool(msg.files or raw_files)


def _thread_parent(fe, channel: str, thread_key: str, bot_user_id: str, owned=None) -> str:
    """A short summary of what the parent message was about."""
    try:
        msgs = fe.thread_replies(channel, thread_key, limit=1)
    except Exception:
        return ""
    if not msgs:
        return ""
    is_bot, parent_text = _sender_and_text(msgs[0], channel, bot_user_id,
                                           owned or _owner_check(None))
    # Truncate long bot messages to just the first 200 chars
    if len(parent_text) > 200:
        parent_text = parent_text[:200] + "..."
    sender = "bot" if is_bot else "user"
    return f"[{sender}]: {parent_text}"


def fetch_recent_messages(hours_back: int = 24) -> list[dict]:
    """
    Fetch recent messages from the EA channel.

    Returns a list of message dicts, each with:
      - ts: message id (Slack timestamp on Slack)
      - sender: "user" or "bot"
      - text: message text
      - time: human-readable timestamp
      - has_bot_reply: user messages only, True when the message has replies
      - thread_ts: parent thread key (if a reply)
      - thread_context: summary of what the user was replying to
      - files: list of file names/types (if any)
    """
    fe = _frontend()
    if fe is None:
        return []
    channel = fe.main_channel
    if not channel:
        print("  Chat reader: no token or channel ID, skipping", file=sys.stderr)
        return []
    bot_user_id = fe.bot_user_id or ""
    owned = _owner_check(fe)

    oldest = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp()

    try:
        raw_messages = fe.recent_messages(channel, oldest, limit=200)
    except Exception as e:
        print(f"  Chat reader: recent_messages failed: {e}", file=sys.stderr)
        return []
    # Oldest first, whatever order the frontend returned.
    raw_messages = sorted(raw_messages, key=lambda m: m.posted_at or 0.0)

    results = []
    for msg in raw_messages:
        is_bot, text = _sender_and_text(msg, channel, bot_user_id, owned)
        if not text and not _has_files(msg):
            continue
        # Slack history carried over by the porter: old, whatever its time.
        if is_bot and _chat_pkg().text.is_ported(text):
            continue

        ts = msg.msg_id or ""
        ts_dt = (datetime.fromtimestamp(float(msg.posted_at), tz=timezone.utc)
                 if msg.posted_at else None)

        entry = {
            "ts": ts,
            "sender": "bot" if is_bot else "user",
            "text": text,
            "time": ts_dt.strftime("%Y-%m-%d %H:%M UTC") if ts_dt else "",
        }

        # Mark user instructions that have thread replies as handled.
        if not is_bot:
            entry["has_bot_reply"] = (msg.reply_count or 0) > 0

        # Thread context: if the user replied in a thread, fetch the parent
        thread_ts = msg.thread_key
        if thread_ts and thread_ts != ts:
            entry["thread_ts"] = thread_ts
            entry["thread_context"] = _thread_parent(fe, channel, thread_ts, bot_user_id, owned)

        # File uploads (receipts, screenshots)
        if _has_files(msg):
            entry["files"] = _file_entries(msg)

        results.append(entry)

    return results


def summarize_for_briefing(messages: list[dict]) -> dict:
    """
    Summarize chat messages into structured data for the briefing JSON.

    Returns:
      {
        "message_count": N,
        "user_message_count": N,
        "messages": [...],          # all messages (user + bot)
        "user_instructions": [...], # user messages only (for quick scan)
        "pending_instructions": [...],  # user messages with NO bot reply yet
        "handled_instructions": [...],  # user messages the bot already responded to
      }
    """
    user_msgs = [m for m in messages if m["sender"] == "user"]
    pending = [m for m in user_msgs if not m.get("has_bot_reply", False)]
    handled = [m for m in user_msgs if m.get("has_bot_reply", False)]
    return {
        "message_count": len(messages),
        "user_message_count": len(user_msgs),
        "messages": messages,
        "user_instructions": user_msgs,
        "pending_instructions": pending,
        "handled_instructions": handled,
    }


def fetch_thread(channel: str, thread_key: str, limit: int = 200) -> list[dict]:
    """One thread as [{ts, sender, text, time}], oldest first."""
    fe = _frontend()
    if fe is None:
        return []
    bot_user_id = fe.bot_user_id or ""
    owned = _owner_check(fe)
    try:
        msgs = fe.thread_replies(channel, thread_key, limit=limit)
    except Exception as e:
        print(f"  Chat reader: thread_replies failed: {e}", file=sys.stderr)
        return []
    out = []
    for msg in msgs:
        ts_dt = (datetime.fromtimestamp(float(msg.posted_at), tz=timezone.utc)
                 if msg.posted_at else None)
        is_bot, text = _sender_and_text(msg, channel, bot_user_id, owned)
        out.append({"ts": msg.msg_id,
                    "sender": "bot" if is_bot else "user",
                    "text": text,
                    "time": ts_dt.strftime("%Y-%m-%d %H:%M UTC") if ts_dt else ""})
    return out


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the EA chat channel")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--thread", help="thread key to read instead of the channel")
    parser.add_argument("--channel", help="channel id holding --thread")
    args = parser.parse_args(argv)

    if args.thread:
        if not args.channel:
            parser.error("--thread requires --channel")
        print(json.dumps(fetch_thread(args.channel, args.thread), indent=2))
        return 0
    msgs = fetch_recent_messages(hours_back=args.hours)
    print(json.dumps(summarize_for_briefing(msgs), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
