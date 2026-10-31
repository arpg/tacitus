"""The Zulip chat frontend.

Identity: a stream is the channel `stream:<id>`; a topic is the thread
`stream:<id>:topic:<percent-encoded topic>`; a message id is `str(int)`.
A private stream (cfg["dm_stream"], default "ea-dm") stands in for the
owner's DM with the bot, so a topic there is a conversation too.

Threads work as on Slack without the owner naming a topic. The owner's
post in a stream's general chat (the empty topic) is moved into a new
topic and dispatched as the start of that thread; a post in an existing
topic continues it. The topic takes its name from `thread_namer` (the
core's short title) when one is set and answers, else from the post's
first words; the naming and the move run off the event loop. Everything
the bot starts (a post with no thread, a card) opens a new topic, never
one in use, named from its first line. A room's opening context goes to
general chat (`channel_top`), so the owner's first post there starts
its own topic instead of continuing the room's intro.

A conversation whose topic is renamed or moved (to another topic name,
another stream, or merged into an existing topic) keeps its identity:
a move that leaves the old topic empty reaches the core as a
"thread_moved" event {old, new} so it can re-key what it stores, and
the frontend keeps old -> new in `<private_dir>/zulip-moves.json`, so a
post, card, upload or read aimed at the old key follows it instead of
recreating the old topic. A partial move that leaves messages behind is
a split: the old key stays. A new message under a moved-away name makes
that key live again. A real
1:1 direct message to the bot arrives as channel `dm:<sender_id>` with
one rolling thread key `dm:<sender_id>`; a group DM gets the subtype
"other:group_dm".

Zulip has no buttons. A confirmation card is text that carries a
one-time code, and the bot reacts on it once per action (cards.action_
reactions: thumbs up for the primary, thumbs down for cancel, the label's
emoji for the rest), so one tap on a chip picks that action, the way a
button did. The owner may also reply with the code in the card's own topic
(optionally led by an action word); a code anywhere else is a message.
The chips go on after a short wait and are put back twice more while the
card is live (CHIP_DELAY, CHIP_REFRESH_AT), because the web
client drops a reaction that arrives in the same event batch as its
message. The chips come off once the card is answered.
Both reach the core as one ActionEvent through the pure resolvers in
chat.cards. The owner check runs before any registry lookup, and the
bot's own messages never count as codes.

The event loop long-polls the bot's own event queue. The queue id, the
last event id, the last message id and the realm limits persist in
`<private_dir>/zulip-queue.json`, so a restart resumes the same queue;
on BAD_EVENT_QUEUE_ID the frontend registers a new queue and dispatches
a "queue_reset" event carrying the last message id, so the core can
backfill the gap with `recent_messages(since_id=...)`.

Shadow mode (ShadowFrontend) mirrors what the core does through the
composite. The writes this frontend makes on its own go to the
composite through `_outbound` (base.ChatFrontend.on_outbound): the
owner's card code, which is never dispatched as a message, its done
reaction and the reply to a word the card does not take; an edit refused
for the edit window, whose new text goes out as a fresh message (the
mirror updates the message in place); and a stream's description or
archive change. A rename reaches the composite as its channel_rename
message, and a whole thread move as the "thread_moved" event. Not
mirrored, by design: the reaction chips on a card (they stand in for
buttons, which the web chat draws itself) and a partial move that splits
a topic, which reaches the core as nothing either.

The translators (inbound_from_message, rename_from_stream_event,
channel_changes_from_event, reaction_action, code_action, render_card)
are pure functions over the API's dicts, tested with fixtures. The
`zulip` and `requests` packages are imported inside methods, so
importing this module needs neither.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import card_text as _ct
from . import files as _files

from .base import (
    AlreadyArchived,
    ChannelNotPrivate,
    ChatError,
    ChatFrontend,
    Limits,
    NotArchived,
    NotPermitted,
    NotSupported,
    RoomNameTaken,
)
from .cards import (
    CardRegistry,
    action_reactions,
    ConfirmationCard,
    parse_code,
    resolve_code,
    resolve_reaction,
)
from .text import EMOJI_ALIASES, mrkdwn_to_zulip, smart_chunk, topic_from_text, unique_topic
from .types import (
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
)

log = logging.getLogger("chat.zulip")

EVENT_TYPES = ["message", "reaction", "stream", "subscription",
               "update_message", "delete_message"]

QUEUE_FILE = "zulip-queue.json"
CARDS_FILE = "chat-cards.json"
# Old thread key -> new, for conversations whose topic was renamed or moved.
MOVES_FILE = "zulip-moves.json"
MOVES_KEEP = 1000

# Emoji name candidates per status, tried in order; the first name the
# server accepts is cached for the status.
STATUS_CANDIDATES: dict[str, tuple[str, ...]] = {
    Status.DONE.value: ("white_check_mark", "check"),
    Status.FAILED.value: ("x", "cross_mark"),
    Status.WAITING.value: ("hourglass_flowing_sand", "hourglass"),
    Status.PAUSED.value: ("double_vertical_bar", "pause"),
    Status.EYES.value: ("eyes",),
    Status.WARNING.value: ("warning",),
    Status.QUESTION.value: ("question",),
}

# Seconds a new card waits before its chips go on. The web client, which
# the desktop app runs, applies the reactions in a batch of events before
# it stores the batch's new messages, and drops a reaction for a message it
# has not stored (server_events.js get_events_success, reactions.ts
# add_reaction). A background window polls for its next batch up to a
# second late, so a chip added right after the card can share the card's
# batch and not show there until a reload. The wait puts the chips in a
# later batch.
CHIP_DELAY = 2.0
# Seconds after a card at which its chips are taken off and put back while
# the card is still live. The wait above helps only a client that polls
# promptly: a desktop window hidden for minutes polls once a minute, pulls
# the card and its chips in one batch, and drops the chips the same way.
# By the first pass that client has stored the card, so the remove and add
# events land on a message it holds and the chips show. The second pass
# covers a client whose gap was longer still.
CHIP_REFRESH_AT = (75.0, 600.0)
# A reaction call answered RATE_LIMIT_HIT (the bot's limit is shared by
# every lane) is tried again after the server's retry-after, capped at
# RATE_WAIT_MAX seconds, up to RATE_RETRIES times.
RATE_RETRIES = 3
RATE_WAIT_MAX = 5.0

# Unicode code points of the reactions that confirm or cancel a card,
# mapped to a name REACTION_ROLES knows, so a reaction resolves whatever
# name the server reports for it.
_CODEPOINT_NAMES = {
    "2705": "white_check_mark",
    "2714": "check",
    "274c": "x",
    "1f44d": "+1",
    "1f44e": "-1",
}

_UPLOAD_LINK_RE = re.compile(r"\[([^\]]*)\]\((/user_uploads/[^)\s]+)\)")
_CARD_RULE = "\n\n----\n"
_TOPIC_MARK = ":topic:"
# The empty topic as the bot's client sees it: the server shows it under
# this name to a client that does not declare empty-topic support.
GENERAL_CHAT = "general chat"


# identity


def _quote_topic(topic: str) -> str:
    return urllib.parse.quote(topic, safe="").replace("~", "%7E")


def thread_key_for(stream_id: int | str, topic: str) -> str:
    """The thread key of a topic in a stream."""
    return f"stream:{int(stream_id)}{_TOPIC_MARK}{_quote_topic(topic)}"


def parse_thread_key(key: str) -> tuple[str, str | None]:
    """(channel, topic) for a stream thread key, (channel, None) for a DM
    key. Raises ValueError for anything else."""
    key = key or ""
    if key.startswith("stream:"):
        head, sep, quoted = key.partition(_TOPIC_MARK)
        if sep and head[7:].isdigit():
            return head, urllib.parse.unquote(quoted)
    elif key.startswith("dm:") and re.fullmatch(r"dm:\d+(?:-\d+)*", key):
        return key, None
    raise ValueError(f"not a Zulip thread key: {key!r}")


def stream_id_of(channel: str) -> int:
    """The stream id in a `stream:<id>` channel; ChatError otherwise."""
    if channel.startswith("stream:"):
        rest = channel[7:].split(":", 1)[0]
        if rest.isdigit():
            return int(rest)
    raise ChatError(f"not a Zulip stream channel: {channel!r}")


def _dm_ids(channel: str) -> list[int]:
    if not re.fullmatch(r"dm:\d+(?:-\d+)*", channel or ""):
        raise ChatError(f"not a Zulip DM channel: {channel!r}")
    return [int(x) for x in channel[3:].split("-")]


def encode_hash_component(text: str) -> str:
    """Encode a narrow-URL component the way Zulip's web client does:
    encodeURIComponent, then % becomes . and ( ) . get dotted escapes."""
    enc = urllib.parse.quote(text, safe="-_.!~*'()")
    table = {"%": ".", "(": ".28", ")": ".29", ".": ".2E"}
    return "".join(table.get(c, c) for c in enc)


def _stream_slug(stream_id: int, name: str) -> str:
    return encode_hash_component(f"{stream_id}-{name.replace(' ', '-')}")


# pure translators


def _uploads(content: str, site: str) -> list[FileRef]:
    out = []
    for m in _UPLOAD_LINK_RE.finditer(content or ""):
        label, path = m.group(1), m.group(2)
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        out.append(FileRef(name=name, url=f"{site}{path}", id=path, title=label))
    return out


def _looks_like_bot(m: dict) -> bool:
    email = str(m.get("sender_email") or "")
    return "-bot@" in email


def inbound_from_message(m: dict, self_id: int | str, owner_id: int | str,
                         dm_stream_id: int | str | None,
                         topic_seen: Callable[[str, int], bool],
                         site: str = "") -> InboundMessage | None:
    """Translate one Zulip message object into an InboundMessage.

    topic_seen(thread_key, msg_id) answers whether the conversation had a
    message before msg_id; that makes is_reply. None for a message type
    the frontend does not know.
    """
    mid = int(m.get("id", 0))
    sender = str(m.get("sender_id", ""))
    self_s = str(self_id)
    content = str(m.get("content") or "")
    files = _uploads(content, site)
    common = dict(msg_id=str(mid), text=content, user_id=sender,
                  posted_at=float(m.get("timestamp") or 0.0),
                  is_self=sender == self_s,
                  # The owner is never a bot, whatever the address says;
                  # the core checks the owner by id.
                  is_bot=sender == self_s or (sender != str(owner_id) and _looks_like_bot(m)),
                  files=files, subtype="file_share" if files else "", raw=m)
    mtype = m.get("type")
    if mtype == "stream":
        sid = int(m["stream_id"])
        topic = str(m.get("subject", m.get("topic", "")))
        key = thread_key_for(sid, topic)
        return InboundMessage(channel=f"stream:{sid}", thread_key=key,
                              is_reply=bool(topic_seen(key, mid)),
                              is_dm=dm_stream_id is not None and str(sid) == str(dm_stream_id),
                              **common)
    if mtype == "private":
        ids = sorted({int(r["id"]) for r in (m.get("display_recipient") or [])
                      if isinstance(r, dict) and "id" in r})
        others = [i for i in ids if str(i) != self_s]
        if len(others) == 1:
            channel = f"dm:{others[0]}"
            return InboundMessage(channel=channel, thread_key=channel,
                                  is_reply=bool(topic_seen(channel, mid)),
                                  is_dm=True, **common)
        channel = "dm:" + "-".join(str(i) for i in (others or ids))
        common["subtype"] = "other:group_dm"
        return InboundMessage(channel=channel, thread_key=channel,
                              is_reply=bool(topic_seen(channel, mid)), **common)
    return None


def rename_from_stream_event(ev: dict) -> InboundMessage | None:
    """A stream rename event as a channel_rename InboundMessage, else None."""
    if ev.get("type") != "stream" or ev.get("op") != "update" or ev.get("property") != "name":
        return None
    sid = int(ev["stream_id"])
    return InboundMessage(channel=f"stream:{sid}", msg_id="", text="", user_id="",
                          posted_at=time.time(), subtype="channel_rename",
                          new_name=str(ev.get("value") or ""), raw=ev)


def channel_changes_from_event(ev: dict) -> list[tuple[str, dict]]:
    """A stream event's changes to rooms other than a rename, as
    [(channel, {"description": text} or {"archived": bool})]. Archiving
    reaches a client that does not declare archived channels as a delete
    and unarchiving as a create; a newer server may send the is_archived
    property instead."""
    if ev.get("type") != "stream":
        return []
    op = ev.get("op")
    if op == "update" and ev.get("stream_id") is not None:
        channel = f"stream:{int(ev['stream_id'])}"
        prop = ev.get("property")
        if prop == "description":
            return [(channel, {"description": str(ev.get("value") or "")})]
        if prop == "is_archived":
            return [(channel, {"archived": bool(ev.get("value"))})]
        return []
    if op in ("delete", "create"):
        return [(f"stream:{int(s['stream_id'])}", {"archived": op == "delete"})
                for s in ev.get("streams") or []
                if isinstance(s, dict) and s.get("stream_id") is not None]
    return []


def thread_move_from_event(ev: dict) -> dict | None:
    """A topic rename or move as a "thread_moved" payload, else None.

    Payload: {type, old, new, channel, old_channel, message_ids, whole,
    user_id}; old and new are thread keys, whole is True when the move took
    every message of the topic, user_id is who moved it ("" when the event
    names no one). A move into or out of general chat is None:
    general chat is the channel's top level, not a conversation, and the
    owner's post leaves it to start a thread."""
    if ev.get("type") != "update_message" or "orig_subject" not in ev \
            or ev.get("stream_id") is None:
        return None
    old_topic = str(ev.get("orig_subject") or "")
    new_topic = str(ev["subject"]) if "subject" in ev else old_topic
    if old_topic in ("", GENERAL_CHAT) or new_topic in ("", GENERAL_CHAT):
        return None
    old_sid = int(ev["stream_id"])
    new_sid = int(ev.get("new_stream_id") or old_sid)
    old, new = thread_key_for(old_sid, old_topic), thread_key_for(new_sid, new_topic)
    if old == new:
        return None
    return {"type": "thread_moved", "old": old, "new": new,
            "channel": f"stream:{new_sid}", "old_channel": f"stream:{old_sid}",
            "message_ids": [int(i) for i in ev.get("message_ids") or []],
            "whole": ev.get("propagate_mode") == "change_all",
            "user_id": str(ev.get("user_id") or "")}


def reaction_name(ev: dict) -> str:
    """The reaction's name, normalised through its code point when known."""
    code = str(ev.get("emoji_code") or "").lower()
    if ev.get("reaction_type", "unicode_emoji") == "unicode_emoji" and code in _CODEPOINT_NAMES:
        return _CODEPOINT_NAMES[code]
    return str(ev.get("emoji_name") or "")


def reaction_action(ev: dict, owner_id: str,
                    lookup: Callable[[int], dict | None],
                    self_id: str = "") -> ActionEvent | None:
    """The ActionEvent an owner's added reaction on a live card selects.

    The owner check runs first, and a reaction by the bot itself (its own
    chips) never counts, even under a misconfigured owner id equal to the
    bot's; lookup(message_id) is called only for the owner's reactions."""
    if ev.get("type") != "reaction" or ev.get("op") != "add":
        return None
    user = str(ev.get("user_id", ""))
    if not owner_id or user != str(owner_id):
        return None
    if not self_id or user == str(self_id):
        return None
    name = reaction_name(ev)
    if not name:
        return None
    row = lookup(int(ev["message_id"]))
    act = resolve_reaction(row, name, user, str(owner_id), self_id=str(self_id))
    if act is not None:
        act.raw = ev
    return act


def code_action(msg: InboundMessage, registry: CardRegistry, owner_id: str,
                self_id: str) -> tuple[str, Any] | None:
    """Classify a message as a card code.

    ("action", ActionEvent) when it confirms a live card; ("unknown", row)
    when its code names a live card but its word is none of the card's;
    None otherwise, and always None for anyone but the owner and for the
    bot's own messages, before the registry is read. A code counts only in
    the topic (or DM) its card was posted in: a code typed anywhere else is
    an ordinary message."""
    if not owner_id or msg.user_id != str(owner_id) or msg.user_id == str(self_id):
        return None
    parsed = parse_code(msg.text)
    if not parsed or not msg.thread_key:
        return None
    ev = resolve_code(msg.text, registry, msg.user_id, str(owner_id),
                      thread_key=msg.thread_key)
    if ev is not None:
        ev.raw = msg.raw
        return ("action", ev)
    row = registry.by_code(parsed[1], thread_key=msg.thread_key)
    if row is not None:
        return ("unknown", row)
    return None


def unknown_word_reply(row: dict) -> str:
    """One line naming the words a live card accepts."""
    code = row.get("code") or ""
    words = [a.get("word") for a in (row.get("actions") or {}).values() if a.get("word")]
    choices = ", ".join(f"`{w} {code}`" for w in words) or f"`{code}`"
    return f"Card **{code}** takes {choices}."


def _clip(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


_TITLE_LOCK_RE = re.compile(r"^\s*:(?:lock|locked):\s*")
# The lock at the front of every card header, in Zulip's own spelling: this
# line is built after the converter runs, and Zulip shows Slack's :lock: as
# plain text.
_CARD_LOCK = f":{EMOJI_ALIASES.get('lock', 'lock')}:"


def _card_md(text: str, mention: Callable[[str], str] | None = None) -> str:
    """Card text the bot or a plugin template wrote (a title, a line, a
    label, the footer) as Zulip Markdown in card mode: the values in it
    that came from the model or mail (card_text.inert spans) are literal,
    and nothing in it can open a block, link elsewhere or mention anyone."""
    return mrkdwn_to_zulip(text, mention, card=True)


def card_title_line(title: str, mention: Callable[[str], str] | None = None) -> str:
    """The header line for a card title. The bot writes titles for Slack
    (`:lock: *Confirm send*`), so the lock the renderer supplies is not
    added twice and a title that already carries bold is not wrapped in
    a second bold: `**:locked: **Confirm send****` was the result. The
    lock it supplies is Zulip's name for the emoji."""
    bare = _TITLE_LOCK_RE.sub("", title or "", count=1)
    lines = [ln.strip() for ln in _card_md(bare, mention).splitlines() if ln.strip()]
    text = "\n".join(lines)
    if "**" not in text and text:
        text = f"**{text}**"
    return f"{_CARD_LOCK} {text}".rstrip()


def field_value(label: str, value: str) -> str:
    """A field's value as inert text: one line, cut at the field's limit
    with a marker, in a code span, so a subject, a display name or a
    location shows exactly as written and renders nothing."""
    return _ct.zulip_code_span(_ct.one_line(_ct.strip_inert(value), _ct.field_limit(label)))


def _spoiler(label: str, text: str) -> str:
    """A collapsed Zulip spoiler block holding `text` as a quote. Every
    line of the quote starts with ">", so no line of the text is the
    fence, and the fence is longer than any backtick run in the text."""
    quoted = _ct.zulip_body(text)
    runs = [len(m.group(0)) for m in re.finditer(r"`+", quoted)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}spoiler {label}\n{quoted}\n{fence}"


QUOTED_LABEL = _ct.QUOTED_LABEL


def render_card_header(card: ConfirmationCard,
                       mention: Callable[[str], str] | None = None) -> str:
    """The card's title, fields, lines, select options and footer: every
    part of the card that is not the draft body. Each field value is inert
    text on one line; no line of it can be the card's rule."""
    parts = [card_title_line(card.title, mention)]
    for f in card.fields:
        parts.append(f"**{_card_md(_ct.one_line(f.label, 60), mention)}:** "
                     f"{field_value(f.label, f.value)}")
    for ln in card.lines:
        parts.extend(x for x in _card_md(ln, mention).splitlines() if x.strip())
    if card.select is not None and card.select.options:
        parts.append(_card_md(_ct.one_line(card.select.placeholder), mention) + ":")
        parts.extend(f"- {_ct.zulip_code_span(_ct.one_line(_ct.strip_inert(label)))}"
                     for label, _v in card.select.options)
    if card.footer:
        parts.extend(x for x in _card_md(card.footer, mention).splitlines() if x.strip())
    return "\n".join(parts)


def render_card_body(card: ConfirmationCard, body_chars: int = 2800) -> str:
    """The draft body under its label, quoted, and the collapsed block.

    `body_chars` 0 shows the whole body, then the text that matched an
    earlier message in a collapsed spoiler block; otherwise the body is cut
    at `body_chars` and the collapsed block is left out. The body keeps its
    lines, but Zulip's own link, mention and widget syntax in it is broken,
    and as a quote it can neither close its block nor open one that hides
    what follows."""
    if not card.body and not (card.quoted and not body_chars):
        return ""
    text = "**Body:**\n" + _ct.zulip_body(_clip(card.body or "", body_chars))
    if card.quoted and not body_chars:
        text += "\n\n" + _spoiler(QUOTED_LABEL, card.quoted)
    return text


_LABEL_LEAD_RE = re.compile(r"^\s*:[a-z0-9_+-]+:\s*")


def _legend_label(label: str) -> str:
    return _ct.one_line(_LABEL_LEAD_RE.sub("", label or ""), 80, keep_inert=True)


def render_card_actions(card: ConfirmationCard, code: str) -> str:
    """The legend for the reaction chips the bot puts on the card, then the
    typed fallback: each action's word with the code. Each label is one
    line of card text, cut at 80 characters."""
    by_id = {a.id: a for a in card.actions}
    chips = action_reactions(card)
    words = [_ct.one_line(a.word, 30) for a in card.actions if a.word]
    lines = []
    if chips:
        lines.append("Tap a reaction below: " + " · ".join(
            f":{name}: {_legend_label(by_id[aid].label)}" for name, aid in chips))
    if words:
        lead = "Or reply" if chips else "Reply"
        lines.append(f"{lead} `<word> {code}` in this topic: " + ", ".join(words) + ".")
    return "\n".join(lines) or f"Code **{code}**, in this topic."


def render_card(card: ConfirmationCard, code: str,
                mention: Callable[[str], str] | None = None,
                body_chars: int = 2800) -> str:
    """The full card text: the header, a rule, the action legend, and
    after a second rule the body. The recipients, the subject and the
    legend come before the body, so no body, however long, pushes them out
    of view, and the first two rules in the text are always the card's."""
    text = (render_card_header(card, mention) + _CARD_RULE
            + _card_md(render_card_actions(card, code), mention))
    tail = render_card_body(card, body_chars)
    return text + _CARD_RULE + tail if tail else text


def split_card_text(content: str) -> tuple[str, str]:
    """(header, body tail) of a posted card's text: the parts a status line
    keeps. The legend, or an earlier status, between them is dropped."""
    parts = (content or "").split(_CARD_RULE, 2)
    return parts[0], (parts[2] if len(parts) > 2 else "")


def card_topic(card: ConfirmationCard, code: str, limit: int = 60) -> str:
    """`lock <code> <first field value or title>`, cut to the topic limit."""
    subject = card.fields[0].value if card.fields and card.fields[0].value else card.title
    # The first field is model or mail text: no invisible or reordering
    # character goes into the topic name either.
    subject = _ct.clean(_ct.strip_inert(subject))
    subject = topic_from_text(subject, max(limit, 1))
    topic = f"lock {code} {subject}".strip()
    if limit and len(topic) > limit:
        cut = topic.rfind(" ", 0, limit + 1)
        topic = topic[: cut if cut > len(f"lock {code}") else limit].rstrip()
    return topic


def limits_from_register(res: dict) -> Limits:
    """Limits from a register response (realm fields present)."""
    msg_max = int(res.get("max_message_length") or 10000)
    topic_max = int(res.get("max_topic_length") or 60)
    name_max = int(res.get("max_stream_name_length") or 60)
    return Limits(min(msg_max, 10000) - 200, 2800, name_max, topic_max)


# the frontend


def _ok(res: Any) -> bool:
    return isinstance(res, dict) and res.get("result") == "success"


def _err(res: Any) -> str:
    if not isinstance(res, dict):
        return str(res)
    return f"{res.get('code', '')}: {res.get('msg', '')}".strip(": ")


def _is_edit_window_refusal(res: dict) -> bool:
    msg = str(res.get("msg") or "").lower()
    code = str(res.get("code") or "")
    return "time limit" in msg or "TIME_LIMIT" in code


def _is_emoji_error(res: dict) -> bool:
    msg = str(res.get("msg") or "").lower()
    return "emoji" in msg


class ZulipFrontend(ChatFrontend):
    """A Zulip bot on the server's REST API and its own event queue."""

    name = "zulip"
    capabilities = frozenset({"reactions", "codes", "files", "private_rooms",
                              "registers_cards"})
    # Messages read back carry the Zulip Markdown they were posted in.
    history_markdown = True

    PROMPT_STRINGS = {
        "platform": "Zulip",
        "format_rule_main": ("Format in Slack mrkdwn: *bold*, _italic_, <url|text> links; "
                             "the bot converts the marks for Zulip."),
        "format_rule_reply": ("Your reply is posted to a Zulip topic. Write Slack mrkdwn; "
                              "the bot converts the marks. A message under a new topic "
                              "starts a new conversation."),
        "confirm_location": ("the confirmation card is in your {dm_stream} stream: react with "
                             ":+1: or reply with its code"),
        "confirm_sentence": ("The bot posts a :lock: confirmation card with a one-time code "
                             "in your {dm_stream} stream; react with :+1: to confirm or "
                             ":-1: to cancel, or reply with the code."),
        "history_note": "",
        "room_word": "stream",
        "continuation_header": "Continuing the Zulip topic:",
        "output_only": "Your text output is posted to Zulip automatically.",
        "output_only_resume": ("Output ONLY the reply text (the bot converts Slack-style "
                               "marks: *single-asterisk bold*, :emoji_shortcodes:)."),
        "format_rule_closing": (
            "Formatting: write Slack-style mrkdwn, *bold* (single asterisk), _italic_ "
            "(underscore);\nthe bot converts the marks for Zulip. NEVER use "
            "**double asterisks**.\n"
            "Use emoji shortcodes (:white_check_mark:, :email:) not unicode emoji."),
        "confirm_sentence_wrapped": (
            "buttons themselves are posted as confirmation cards in your {dm_stream} stream, so "
            "say \"the\nconfirmation card is in your {dm_stream} stream\", never \"above\"."),
    }

    def __init__(self, cfg: dict, *, api_key: str, private_dir: Path,
                 client: Any = None) -> None:
        super().__init__()
        self.cfg = dict(cfg or {})
        self.site = str(self.cfg.get("site") or "").rstrip("/")
        self.bot_email = str(self.cfg.get("bot_email") or "")
        self.owner_id = str(self.cfg.get("owner_user_id") or "")
        # "@**Full Name|id**": put on every card so it notifies the owner on
        # every client, whatever the stream's own notification setting.
        self.owner_mention = ""
        self.stream_name = str(self.cfg.get("stream") or "ea")
        self.sched_stream_name = str(self.cfg.get("sched_stream") or "")
        self.dm_stream_name = str(self.cfg.get("dm_stream") or "ea-dm")
        # Where a card settled outside chat goes (file_card): private, the
        # bot its only required subscriber.
        self.done_stream_name = str(self.cfg.get("dm_done_stream") or f"{self.dm_stream_name}-done")
        self.main_channel_name = self.stream_name
        self.private_dir = Path(private_dir)
        self.queue_path = self.private_dir / QUEUE_FILE
        self.card_registry = CardRegistry(self.private_dir / CARDS_FILE)
        self.last_card_row: dict | None = None
        self.limits = Limits(9800, 2800, 60, 60)
        self.self_id = 0
        self.dm_stream_id: int | None = None
        self.queue_id: str | None = None
        self.last_event_id = -1
        self.last_message_id = 0
        self._stop = threading.Event()
        self._stream_names: dict[int, str] = {}
        self._msg_topics: OrderedDict[int, tuple[str, str | None]] = OrderedDict()
        self._seen_keys: set[str] = set()
        self._card_channels: dict[str, str] = {}
        # What a status line keeps of each card: (header, body tail).
        self._card_headers: dict[str, tuple[str, str]] = {}
        self._status_names: dict[str, str] = {}
        self._moves_lock = threading.Lock()
        self._moves: dict[str, str] = self._load_private(MOVES_FILE)
        self._api_key = api_key
        # Runs the slow start of a thread (title, move, dispatch) off the
        # event loop; tests replace it with a direct call.
        self._run_later: Callable[[Callable[[], None]], None] = self._in_thread
        # Tests set these to run the chip and rate-limit waits instantly.
        self.chip_delay = CHIP_DELAY
        self._sleep: Callable[[float], None] = time.sleep
        # Runs a chip refresh after a delay; tests collect the calls.
        self._later: Callable[[float, Callable[[], None]], None] = self._after_delay
        if client is None:
            import zulip
            client = zulip.Client(email=self.bot_email, api_key=api_key, site=self.site)
        self.client = client
        self._resolve_channel_ids()

    def _lookup_stream_id(self, name: str) -> int | None:
        """The stream's id by name, or None when the lookup fails."""
        try:
            res = self.client.get_stream_id(name)
        except Exception:
            return None
        if not _ok(res):
            return None
        sid = int(res["stream_id"])
        self._stream_names[sid] = name
        return sid

    def _resolve_channel_ids(self) -> None:
        """Best-effort channel ids at construction, so the core reads its
        channel constants before start(); start() resolves them again with
        the privacy and subscription checks and raises when they fail."""
        sid = self._lookup_stream_id(self.stream_name)
        self.main_channel = f"stream:{sid}" if sid is not None else ""
        self.sched_channel = ""
        if self.sched_stream_name:
            s2 = self._lookup_stream_id(self.sched_stream_name)
            self.sched_channel = f"stream:{s2}" if s2 is not None else ""
        self.dm_stream_id = self._lookup_stream_id(self.dm_stream_name)

    # lifecycle

    def _call(self, path: str, method: str = "GET", request: dict | None = None) -> dict:
        return self.client.call_endpoint(url=path, method=method, request=request or {})

    def _get_stream(self, stream_id: int) -> dict | None:
        res = self._call(f"streams/{stream_id}")
        if _ok(res) and isinstance(res.get("stream"), dict):
            s = res["stream"]
            self._stream_names[stream_id] = str(s.get("name") or "")
            return s
        return None

    def _subscribed_ids(self) -> set[int]:
        res = self.client.get_subscriptions()
        if not _ok(res):
            raise ChatError(f"zulip get_subscriptions failed: {_err(res)}")
        out = set()
        for s in res.get("subscriptions") or []:
            sid = int(s["stream_id"])
            out.add(sid)
            self._stream_names[sid] = str(s.get("name") or "")
        return out

    def _resolve_private_stream(self, name: str, subscribed: set[int]) -> int:
        res = self.client.get_stream_id(name)
        if not _ok(res):
            raise ChatError(f"zulip stream '{name}' not found: {_err(res)}")
        sid = int(res["stream_id"])
        info = self._get_stream(sid)
        if info is None:
            raise ChatError(f"zulip stream '{name}' ({sid}) cannot be read by the bot")
        if not info.get("invite_only"):
            raise ChannelNotPrivate(f"zulip stream '{name}' is public; the EA needs a "
                                    f"private (invite-only) stream")
        if sid not in subscribed:
            raise ChatError(f"the bot is not subscribed to zulip stream '{name}'")
        self._stream_names[sid] = name
        self._check_owner_only(name, sid)
        return sid

    def _check_owner_only(self, name: str, sid: int) -> None:
        """The stream's subscribers must be the owner and the bot. The EA
        stream, the scheduling stream and the DM stream carry the briefings,
        the drafts and every confirmation card with its full body, so a
        third subscriber would read them. Raises ChannelNotPrivate when
        anyone else is subscribed, ChatError when the list cannot be read."""
        members = self.channel_members(f"stream:{sid}")
        if members is None:
            raise ChatError(f"zulip stream '{name}': the bot cannot read its subscribers")
        others = sorted(members - {str(self.self_id), str(self.owner_id)})
        if others:
            raise ChannelNotPrivate(
                f"zulip stream '{name}' has subscriber(s) {', '.join(others)} besides the "
                f"owner ({self.owner_id}) and the bot ({self.self_id}). The EA posts drafts "
                f"and confirmation cards there; unsubscribe them (stream settings, "
                f"Subscribers) and restart the bot")
        if str(self.owner_id) not in members:
            log.warning("zulip stream '%s': the owner (%s) is not subscribed", name,
                        self.owner_id)

    def _register(self) -> None:
        res = self.client.register(event_types=EVENT_TYPES,
                                   fetch_event_types=[*EVENT_TYPES, "realm"],
                                   apply_markdown=False)
        if not _ok(res):
            raise ChatError(f"zulip register failed: {_err(res)}")
        self.queue_id = str(res["queue_id"])
        self.last_event_id = int(res.get("last_event_id", -1))
        if res.get("max_message_id"):
            self.last_message_id = max(self.last_message_id, int(res["max_message_id"]))
        self.limits = limits_from_register(res)
        self._save_queue()

    def _load_private(self, name: str) -> dict:
        try:
            data = json.loads((self.private_dir / name).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_private(self, name: str, data: dict) -> None:
        """Write one JSON file in the private dir, atomically at 0600."""
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.private_dir, prefix=f".{name}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.private_dir / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load_queue(self) -> dict:
        return self._load_private(QUEUE_FILE)

    def _save_queue(self) -> None:
        self._save_private(QUEUE_FILE, {
            "queue_id": self.queue_id, "last_event_id": self.last_event_id,
            "last_message_id": self.last_message_id, "limits": list(self.limits)})

    def start(self) -> None:
        """Identify the bot, check the three streams, and open or resume
        the event queue. Raises ChatError when a stream is public, absent
        or lacks the bot."""
        prof = self.client.get_profile()
        if not _ok(prof):
            raise ChatError(f"zulip get_profile failed: {_err(prof)}")
        self.self_id = int(prof["user_id"])
        self.bot_user_id = str(self.self_id)
        if prof.get("email"):
            self.bot_email = str(prof["email"])
        if not self.owner_id:
            raise ChatError("chat.zulip.owner_user_id is not set")
        if self.owner_id == str(self.self_id):
            raise ChatError("chat.zulip.owner_user_id is the bot's own user id; set it to "
                            "the owner's id, so the bot's own reactions and messages "
                            "can never confirm a card")
        self.owner_mention = self._owner_mention()
        subscribed = self._subscribed_ids()
        sid = self._resolve_private_stream(self.stream_name, subscribed)
        self.main_channel = f"stream:{sid}"
        self.sched_channel = ""
        if self.sched_stream_name:
            self.sched_channel = f"stream:{self._resolve_private_stream(self.sched_stream_name, subscribed)}"
        self.dm_stream_id = self._resolve_private_stream(self.dm_stream_name, subscribed)
        saved = self._load_queue()
        self.last_message_id = int(saved.get("last_message_id") or 0)
        lim = saved.get("limits")
        if saved.get("queue_id") and isinstance(lim, list) and len(lim) == 4:
            self.queue_id = str(saved["queue_id"])
            self.last_event_id = int(saved.get("last_event_id", -1))
            self.limits = Limits(*(int(x) for x in lim))
        else:
            self._register()
        log.info("frontend=zulip stream=%s (private, subscribed) owner=%s",
                 self.stream_name, self.owner_id)

    def stop(self) -> None:
        """Ask run_forever to return after the current poll."""
        self._stop.set()

    def poll_once(self) -> int:
        """One get_events round: handle each event, persist the position.
        The number of events handled."""
        if not self.queue_id:
            self._register()
        res = self.client.get_events(queue_id=self.queue_id,
                                     last_event_id=self.last_event_id, dont_block=False)
        if not _ok(res):
            if res.get("code") == "BAD_EVENT_QUEUE_ID":
                log.warning("zulip event queue expired; registering a new one")
                gap_from = self.last_message_id
                self.queue_id = None
                self._register()
                self.registry.dispatch_event("queue_reset",
                                             {"last_message_id": gap_from})
                return 0
            raise ChatError(f"zulip get_events failed: {_err(res)}")
        n = 0
        for ev in res.get("events") or []:
            self.last_event_id = max(self.last_event_id, int(ev.get("id", -1)))
            try:
                self.handle_event(ev)
            except Exception:
                log.exception("zulip event %s failed", ev.get("type"))
            n += 1
        if n:
            self._save_queue()
        return n

    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except Exception:
                log.exception("zulip poll failed; retrying in %.0fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def ping(self) -> bool:
        try:
            return _ok(self.client.get_profile())
        except Exception:
            return False

    # event handling

    def _remember(self, mid: int, channel: str, topic: str | None) -> None:
        self._msg_topics[mid] = (channel, topic)
        while len(self._msg_topics) > 5000:
            self._msg_topics.popitem(last=False)

    def _topic_seen(self, key: str, msg_id: int) -> bool:
        if key in self._seen_keys:
            return True
        try:
            res = self.client.get_messages({
                "anchor": msg_id, "num_before": 1, "num_after": 0,
                "include_anchor": False, "apply_markdown": False,
                "narrow": self._narrow_for_key(key)})
            seen = _ok(res) and bool(res.get("messages"))
        except Exception:
            seen = False
        self._seen_keys.add(key)
        return seen

    def translate_message(self, m: dict) -> InboundMessage | None:
        """inbound_from_message bound to this frontend's ids and caches."""
        msg = inbound_from_message(m, self.self_id, self.owner_id, self.dm_stream_id,
                                   self._topic_seen, self.site)
        if msg is None:
            return None
        topic = parse_thread_key(msg.thread_key)[1] if msg.thread_key else None
        self._remember(int(msg.msg_id), msg.channel, topic)
        if msg.thread_key:
            self._seen_keys.add(msg.thread_key)
        return msg

    def _card_row_for(self, msg_id: int) -> dict | None:
        mid = str(msg_id)
        channels = [self._card_channels.get(mid)]
        if self.dm_stream_id is not None:
            channels.append(f"stream:{self.dm_stream_id}")
        channels += [self.main_channel, self.sched_channel]
        for ch in dict.fromkeys(c for c in channels if c):
            row = self.card_registry.by_ref(ch, mid)
            if row:
                return row
        ch = self._channel_of(msg_id)
        return self.card_registry.by_ref(ch, mid) if ch else None

    def handle_event(self, ev: dict) -> None:
        """Translate and dispatch one queue event."""
        etype = ev.get("type")
        if etype == "heartbeat":
            return
        if etype == "message":
            m = ev.get("message") or {}
            self.last_message_id = max(self.last_message_id, int(m.get("id", 0)))
            msg = self.translate_message(m)
            if msg is None:
                return
            self._revive(msg.thread_key)
            if self._consume_code(msg):
                return
            if self._starts_thread(m, msg):
                self._run_later(lambda: self.registry.dispatch_message(
                    self._thread_general_chat(m, msg)))
                return
            self.registry.dispatch_message(msg)
            return
        if etype == "reaction":
            act = reaction_action(ev, self.owner_id, self._card_row_for,
                                  str(self.self_id or ""))
            if act is not None:
                self.registry.dispatch_action(act)
            return
        if etype == "update_message" and "orig_subject" in ev:
            if not self._own_move(ev):
                # Someone else's move re-keys nothing: the bot's replies,
                # sessions and cards stay where the owner left them.
                log.warning("zulip: ignored a topic move by user %s in stream %s",
                            ev.get("user_id") or "unknown", ev.get("stream_id"))
                return
            self._note_moved(ev)
            mv = thread_move_from_event(ev)
            if mv is not None:
                if mv["whole"]:
                    self._thread_moved(mv)
                else:
                    self._run_later(lambda: self._thread_moved_if_gone(mv))
                return
        if etype == "stream":
            ren = rename_from_stream_event(ev)
            if ren is not None:
                self._stream_names[int(ev["stream_id"])] = ren.new_name
                self.registry.dispatch_message(ren)
                return
            for channel, change in channel_changes_from_event(ev):
                self._outbound("channel", channel=channel, **change)
        self.registry.dispatch_event(str(etype), ev)

    # moved threads

    def _own_move(self, ev: dict) -> bool:
        """True when the owner or the bot itself moved the messages (the
        event's user_id, or a thread_moved payload's)."""
        who = str(ev.get("user_id") or "")
        return bool(who) and who in {str(self.owner_id), str(self.self_id)}

    def _note_moved(self, ev: dict) -> None:
        """Point the cached topic of every moved message at its new place."""
        sid = int(ev.get("new_stream_id") or ev.get("stream_id") or 0)
        topic = str(ev["subject"]) if "subject" in ev else str(ev.get("orig_subject") or "")
        if sid:
            for mid in ev.get("message_ids") or []:
                self._remember(int(mid), f"stream:{sid}", topic)

    def _topic_exists(self, key: str) -> bool | None:
        """Whether the thread's topic still holds messages; None when the
        server does not answer. Zulip matches topic names ignoring case."""
        ch, topic = parse_thread_key(key)
        try:
            res = self.client.get_stream_topics(stream_id_of(ch))
        except Exception:
            return None
        if not _ok(res):
            return None
        want = (topic or "").casefold()
        return any(str(t.get("name") or "").casefold() == want for t in res.get("topics") or [])

    def _thread_moved_if_gone(self, mv: dict) -> None:
        """A partial move (one message, or from one message on) moved the
        conversation when it left nothing behind: moving from the first
        message, or merging into another topic. Otherwise it split one."""
        if self._topic_exists(mv["old"]) is False:
            self._thread_moved(mv)

    def _thread_moved(self, mv: dict) -> None:
        """Record a conversation's new key and tell the core."""
        old, new = mv["old"], mv["new"]
        with self._moves_lock:
            for k, v in list(self._moves.items()):
                if v == old:
                    self._moves[k] = new
            self._moves[old] = new
            self._moves.pop(new, None)
            while len(self._moves) > MOVES_KEEP:
                self._moves.pop(next(iter(self._moves)))
            snapshot = dict(self._moves)
        try:
            self._save_private(MOVES_FILE, snapshot)
        except OSError:
            log.exception("zulip: saving thread moves failed")
        self._seen_keys.add(new)
        log.info("zulip: thread moved %s -> %s", old, new)
        self.registry.dispatch_event("thread_moved", dict(mv))

    def _revive(self, key: str) -> None:
        """A message in a topic that had moved away: the name is in use
        again, so its key is a conversation of its own once more."""
        if not key or key not in self._moves:
            return
        with self._moves_lock:
            self._moves.pop(key, None)
            snapshot = dict(self._moves)
        try:
            self._save_private(MOVES_FILE, snapshot)
        except OSError:
            log.exception("zulip: saving thread moves failed")

    def _follow(self, channel: str, thread_ts: str | None) -> tuple[str, str | None]:
        """The current channel and key of a thread that moved after the
        caller learned its key, so a late reply lands in the conversation
        and never recreates the old topic."""
        key = thread_ts
        for _ in range(16):
            nxt = self._moves.get(key or "")
            if nxt is None:
                break
            key = nxt
        if key == thread_ts:
            return channel, thread_ts
        return parse_thread_key(key)[0], key

    def _consume_code(self, msg: InboundMessage) -> bool:
        found = code_action(msg, self.card_registry, self.owner_id, str(self.self_id))
        if found is None:
            return False
        # The code never reaches the core as a message, so the composite
        # copies it from here, before the card's answer lands beside it.
        self._outbound("owner_message", msg=msg)
        kind, val = found
        if kind == "action":
            self.registry.dispatch_action(val)
            if self.react(msg.channel, msg.msg_id, Status.DONE):
                self._outbound("reaction", channel=msg.channel, msg_id=msg.msg_id,
                               status=Status.DONE, on=True)
            return True
        text = unknown_word_reply(val)
        ref = self.post(msg.channel, text, thread_ts=msg.thread_key)
        self._outbound("post", ref=ref, text=text)
        return True

    # messages

    @staticmethod
    def _in_thread(fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception:
                log.exception("zulip: starting a thread failed")
        threading.Thread(target=run, name="zulip-new-thread", daemon=True).start()

    def _starts_thread(self, m: dict, msg: InboundMessage) -> bool:
        """True for the owner's own post in a stream's general chat."""
        return (m.get("type") == "stream" and msg.user_id == str(self.owner_id)
                and not msg.is_bot and str(m.get("subject", "")) in ("", GENERAL_CHAT))

    def _opening_topic(self, channel: str, text: str) -> str:
        """The new thread's topic: the core's title when thread_namer gives
        one, else the text's first words; never a topic in use."""
        limit = self.limits.topic_chars or 60
        name = ""
        if self.thread_namer is not None:
            try:
                name = (self.thread_namer(text, channel) or "").strip()
            except Exception:
                log.exception("zulip: thread_namer failed")
        name = topic_from_text(name, limit) if name else topic_from_text(text, limit)
        return unique_topic(name, self._stream_topics(stream_id_of(channel)), limit)

    def _thread_general_chat(self, m: dict, msg: InboundMessage) -> InboundMessage:
        """The owner's post in a stream's general chat, moved into a new
        topic and translated as that thread's first message, the way a
        top-level message starts a thread on Slack. Any other message, or
        one the server would not move, as it is."""
        if not self._starts_thread(m, msg):
            return msg
        try:
            topic = self._opening_topic(msg.channel, msg.text)
            res = self.client.update_message({
                "message_id": int(msg.msg_id), "topic": topic, "propagate_mode": "change_one",
                "send_notification_to_old_thread": False,
                "send_notification_to_new_thread": False})
        except Exception:
            log.exception("zulip: message %s stays in general chat", msg.msg_id)
            return msg
        if not _ok(res):
            log.warning("zulip: message %s stays in general chat, move to %r failed: %s",
                        msg.msg_id, topic, _err(res))
            return msg
        moved = self._to_inbound(dict(m, subject=topic), is_reply=False)
        if moved is None:
            return msg
        self._seen_keys.add(moved.thread_key)
        return moved

    def _stream_topics(self, stream_id: int) -> list[str]:
        """The stream's topic names; empty when the server does not answer."""
        try:
            res = self.client.get_stream_topics(stream_id)
        except Exception:
            return []
        if not _ok(res):
            return []
        return [str(t.get("name") or "") for t in res.get("topics") or []]

    def _new_topic(self, channel: str, text: str) -> str:
        """A topic named from the text that the stream does not use yet."""
        limit = self.limits.topic_chars or 60
        return unique_topic(topic_from_text(text, limit),
                            self._stream_topics(stream_id_of(channel)), limit)

    def _topic_for(self, channel: str, thread_ts: str | None, text: str) -> str:
        if not thread_ts:
            return self._new_topic(channel, text)
        if thread_ts.isdigit():
            ch, topic = self._msg_topics.get(int(thread_ts)) or self._fetch_topic(int(thread_ts))
            if topic is not None:
                return topic
            return self._new_topic(channel, text)
        ch, topic = parse_thread_key(thread_ts)
        if ch != channel:
            raise ChatError(f"thread {thread_ts!r} is not in {channel!r}")
        return topic or ""

    def _fetch_message(self, msg_id: int) -> dict | None:
        res = self.client.get_messages({"anchor": msg_id, "num_before": 0,
                                        "num_after": 0, "apply_markdown": False,
                                        "narrow": []})
        if not _ok(res):
            return None
        for m in res.get("messages") or []:
            if int(m.get("id", 0)) == msg_id:
                return m
        return None

    def _fetch_topic(self, msg_id: int) -> tuple[str, str | None]:
        m = self._fetch_message(msg_id)
        if not m:
            return ("", None)
        if m.get("type") == "stream":
            val = (f"stream:{m['stream_id']}", str(m.get("subject", "")))
        else:
            msg = self._to_inbound(m, is_reply=True)
            val = (msg.channel if msg else "", None)
        self._remember(msg_id, *val)
        return val

    def _channel_of(self, msg_id: int) -> str:
        hit = self._msg_topics.get(msg_id)
        return hit[0] if hit else self._fetch_topic(msg_id)[0]

    def _send(self, channel: str, content: str, topic: str | None) -> int:
        if channel.startswith("stream:"):
            req = {"type": "stream", "to": stream_id_of(channel), "topic": topic or "",
                   "content": content}
        else:
            req = {"type": "private", "to": _dm_ids(channel), "content": content}
        res = self.client.send_message(req)
        if not _ok(res):
            raise ChatError(f"zulip send_message failed: {_err(res)}")
        return int(res["id"])

    def _key(self, channel: str, topic: str | None) -> str:
        if channel.startswith("stream:"):
            return thread_key_for(stream_id_of(channel), topic or "")
        return channel

    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        channel, thread_ts = self._follow(channel, thread_ts)
        topic = self._topic_for(channel, thread_ts, text) if channel.startswith("stream:") else None
        content = mrkdwn_to_zulip(text, self.mention)
        first: MessageRef | None = None
        for chunk in smart_chunk(content, self.limits.message_chars or 9800):
            mid = self._send(channel, chunk, topic)
            self._remember(mid, channel, topic)
            key = self._key(channel, topic)
            self._seen_keys.add(key)
            if first is None:
                first = MessageRef(channel, str(mid), key, time.time(),
                                   self.permalink(channel, str(mid)))
        assert first is not None
        return first

    def channel_top(self, channel: str) -> str | None:
        """General chat: a post with no thread opens a topic of its own."""
        return thread_key_for(stream_id_of(channel), "") if channel.startswith("stream:") else None

    def update(self, ref: MessageRef, text: str) -> bool:
        return self._edit(ref, mrkdwn_to_zulip(text, self.mention),
                          lambda: self._outbound("update", ref=ref, text=text))

    def _edit(self, ref: MessageRef, content: str,
              reposted: Callable[[], None] | None = None) -> bool:
        """Edit a message. Past the server's edit window the content goes
        out as a new message in the same topic, `reposted` runs, and the
        answer is False."""
        res = self.client.update_message({"message_id": int(ref.id), "content": content})
        if _ok(res):
            return True
        if _is_edit_window_refusal(res):
            try:
                ch, topic = parse_thread_key(ref.thread_key)
            except ValueError:
                ch, topic = self._msg_topics.get(int(ref.id)) or (ref.channel, None)
            self._send(ref.channel, content, topic)
            if reposted is not None:
                reposted()
            return False
        log.warning("zulip update_message %s failed: %s", ref.id, _err(res))
        return False

    def _candidates(self, status: Status | str) -> tuple[str, ...]:
        key = status.value if isinstance(status, Status) else str(status)
        cached = self._status_names.get(key)
        cands = STATUS_CANDIDATES.get(key)
        if cands is None:
            alias = EMOJI_ALIASES.get(key, key)
            cands = (key,) if alias == key else (key, alias)
        if cached:
            return (cached, *(c for c in cands if c != cached))
        return cands

    def _reaction_call(self, verb: str, msg_id: str, name: str) -> Any:
        """add_reaction ("add") or remove_reaction ("remove"), tried again
        after the server's retry-after while it answers RATE_LIMIT_HIT."""
        call = self.client.add_reaction if verb == "add" else self.client.remove_reaction
        for attempt in range(RATE_RETRIES + 1):
            res = call({"message_id": int(msg_id), "emoji_name": name})
            if attempt == RATE_RETRIES or not (isinstance(res, dict)
                                               and res.get("code") == "RATE_LIMIT_HIT"):
                return res
            try:
                wait = float(res.get("retry-after") or 1.0)
            except (TypeError, ValueError):
                wait = 1.0
            self._sleep(min(max(wait, 0.0), RATE_WAIT_MAX))
        return res

    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        key = status.value if isinstance(status, Status) else str(status)
        for name in self._candidates(status):
            res = self._reaction_call("add", msg_id, name)
            if _ok(res) or res.get("code") == "REACTION_ALREADY_EXISTS" \
                    or "already exists" in str(res.get("msg", "")).lower():
                self._status_names[key] = name
                return True
            if not _is_emoji_error(res):
                log.warning("zulip add_reaction %s on %s failed: %s", name, msg_id, _err(res))
                return False
        return False

    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        for name in self._candidates(status):
            res = self._reaction_call("remove", msg_id, name)
            if _ok(res):
                return True
            if res.get("code") == "REACTION_DOES_NOT_EXIST" or _is_emoji_error(res) \
                    or "doesn't exist" in str(res.get("msg", "")).lower():
                continue
            log.warning("zulip remove_reaction %s on %s failed: %s", name, msg_id, _err(res))
            return False
        return False

    # cards

    def _owner_mention(self) -> str:
        """The owner's mention, or "" when the name cannot be read."""
        try:
            u = self.client.get_user_by_id(int(self.owner_id))
        except Exception:
            return ""
        name = ((u or {}).get("user") or {}).get("full_name") if _ok(u) else ""
        return f"@**{name}|{self.owner_id}**" if name else ""

    def _with_mention(self, text: str) -> str:
        return f"{self.owner_mention} {text}" if self.owner_mention else text

    # Room for the card's own code (drawn at post time) and the mention.
    CARD_FIT_MARGIN = 100

    def _full_card_text(self, card: ConfirmationCard, code: str) -> str:
        return self._with_mention(render_card(card, code, self.mention, 0))

    def card_fits(self, card: ConfirmationCard) -> bool:
        """True when the whole card, the full body and the quoted thread
        included, fits in one message under the server's message limit
        (max_message_length from the register, less a margin)."""
        limit = self.limits.message_chars or 9800
        text = self._full_card_text(card, "X" * 6)
        return len(text) + self.CARD_FIT_MARGIN <= limit

    def _card_text(self, card: ConfirmationCard, code: str) -> str:
        """The card's text: the whole body and quoted thread when the card
        fits in one message, else the body cut at the card body limit."""
        if self.card_fits(card):
            return self._full_card_text(card, code)
        return self._with_mention(render_card(card, code, self.mention,
                                              self.limits.card_body_chars or 2800))

    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        """Post a card and record its registry row.

        On Zulip the code must be in the rendered text, so this method
        draws the code and writes the CardRegistry row itself; the core
        must not add the row again. The row is in `last_card_row`."""
        channel, thread_ts = self._follow(channel, thread_ts)
        code = self.card_registry.new_code()
        topic = None
        if channel.startswith("stream:"):
            if thread_ts:
                try:
                    ch, t = parse_thread_key(thread_ts)
                    if ch == channel:
                        topic = t
                except ValueError:
                    pass
            if topic is None:
                topic = card_topic(card, code, self.limits.topic_chars or 60)
        text = self._card_text(card, code)
        mid = self._send(channel, text, topic)
        self._remember(mid, channel, topic)
        key = self._key(channel, topic)
        self._seen_keys.add(key)
        ref = MessageRef(channel, str(mid), key, time.time(), self.permalink(channel, str(mid)))
        self._card_channels[ref.id] = channel
        self._card_headers[ref.id] = self._status_parts(card)
        self.last_card_row = self.card_registry.add(ref, card, code=code)
        if not action_reactions(card):
            return ref
        # The chips wait for the card's message event to reach clients in a
        # batch of its own (CHIP_DELAY). A card answered by its code in the
        # meantime gets no chips, and its retired row is not rewritten live.
        self._sleep(self.chip_delay)
        if not self._card_live(ref):
            return ref
        reactions, chips = self._add_chips(ref.id, card)
        if chips and self._card_live(ref):
            self.last_card_row = self.card_registry.add(ref, card, code=code,
                                                        reactions=reactions, chips=chips)
            for secs in CHIP_REFRESH_AT:
                self._later(secs, lambda: self._refresh_chips(ref))
        return ref

    def _refresh_chips(self, ref: MessageRef) -> int:
        """Take every chip off a still-live card and put it back, so a
        client that dropped them with the card's own batch gets them in a
        batch of their own. Returns the number of chips put back."""
        row = self.card_registry.by_ref(ref.channel, ref.id) or {}
        chips = row.get("chips") or []
        if row.get("status") != "live" or not chips:
            return 0
        back = 0
        for name in chips:
            self._reaction_call("remove", ref.id, name)
            res = self._reaction_call("add", ref.id, name)
            if _ok(res) or "already exists" in str(res.get("msg", "")).lower():
                back += 1
            else:
                log.warning("zulip card chip %s back on %s failed: %s", name, ref.id, _err(res))
        log.info("zulip card %s: %d/%d chips put back", ref.id, back, len(chips))
        return back

    @staticmethod
    def _after_delay(secs: float, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception:
                log.exception("zulip: a delayed call failed")
        t = threading.Timer(secs, run)
        t.daemon = True
        t.start()

    def _card_live(self, ref: MessageRef) -> bool:
        return (self.card_registry.by_ref(ref.channel, ref.id) or {}).get("status") == "live"

    def _add_chips(self, msg_id: str, card: ConfirmationCard) -> tuple[dict, list]:
        """React on the card once per action, so one tap on a chip picks
        it. Returns ({name: action id} for every name the owner's tap may
        report: the shortcode and the name the server took), [the names
        added, which come off when the card is answered])."""
        reactions: dict = {}
        chips: list = []
        for shortcode, action_id in action_reactions(card):
            names = self._candidates(shortcode)
            for name in names:
                res = self._reaction_call("add", msg_id, name)
                if _ok(res) or "already exists" in str(res.get("msg", "")).lower():
                    # A tap may report any name the emoji goes by.
                    reactions.update({n: action_id for n in (shortcode, *names)})
                    chips.append(name)
                    break
                if not _is_emoji_error(res):
                    log.warning("zulip card chip %s on %s failed: %s", name, msg_id, _err(res))
                    break
        return reactions, chips

    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        row = self.card_registry.by_ref(ref.channel, ref.id)
        code = (row or {}).get("code") or self.card_registry.new_code()
        text = self._card_text(card, code)
        self._card_headers[ref.id] = self._status_parts(card)
        if row is None or row.get("status") == "live":
            posted = ref if ref.posted_at is not None else MessageRef(
                ref.channel, ref.id, ref.thread_key, (row or {}).get("posted_at"))
            self.last_card_row = self.card_registry.add(
                posted, card, code=code, reactions=(row or {}).get("reactions"),
                chips=(row or {}).get("chips"))
        return self._edit(ref, text, lambda: self._outbound("card", ref=ref, card=card))

    def _status_parts(self, card: ConfirmationCard) -> tuple[str, str]:
        """What a status line keeps of a card: the header, and the body cut
        at the card body limit, so header, status and body fit one message."""
        return (render_card_header(card, self.mention),
                render_card_body(card, self.limits.card_body_chars or 2800))

    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        parts = self._card_headers.get(ref.id)
        if parts is None:
            m = self._fetch_message(int(ref.id))
            parts = split_card_text(str((m or {}).get("content") or ""))
        header, tail = parts
        # Card text too: a status can carry an executor's line, and must not
        # open a block or form the card's rule.
        status = _card_md(text, self.mention)
        chips = (self.card_registry.by_ref(ref.channel, ref.id) or {}).get("chips") or []
        self.card_registry.retire(ref)
        body = f"{header}{_CARD_RULE}{status}" if header else status
        if header and tail:
            body += f"{_CARD_RULE}{tail}"
        edited = self._edit(ref, body,
                            lambda: self._outbound("card_status", ref=ref, text=text))
        # An answered card keeps the owner's tap and loses the bot's chips,
        # so nothing on it still looks tappable.
        for name in chips:
            try:
                self._reaction_call("remove", ref.id, name)
            except Exception:
                log.warning("zulip: could not take chip %s off card %s", name, ref.id)
        return edited

    def file_card(self, ref: MessageRef) -> bool:
        """Move a card settled outside chat, with its topic, to the done
        stream. An edit leaves a card the owner never opened unread in the
        Inbox; a moved one leaves it, since the owner is not subscribed
        there (a subscriber who mutes the stream keeps it out too). False,
        and the card stays, when the stream cannot be made or vetted or the
        server refuses the move (past its move limit)."""
        if not ref.channel.startswith("stream:"):
            return False
        sid = self._done_stream_id()
        if sid is None or stream_id_of(ref.channel) == sid:
            return False
        try:
            res = self.client.update_message({
                "message_id": int(ref.id), "stream_id": sid, "propagate_mode": "change_all",
                "send_notification_to_old_thread": False,
                "send_notification_to_new_thread": False})
        except Exception as e:
            log.warning("zulip: could not file card %s: %s", ref.id, e)
            return False
        if not _ok(res):
            log.warning("zulip: could not file card %s: %s", ref.id, _err(res))
            return False
        return True

    def _done_stream_id(self) -> int | None:
        """The done stream's id, made on first use as a private stream with
        the bot as its subscriber. None when it cannot be made, is not
        private, or has a subscriber besides the bot and the owner: it
        holds whole cards, so it gets the EA streams' own rule."""
        name = self.done_stream_name
        sid = self._lookup_stream_id(name)
        if sid is None:
            res = self.client.add_subscriptions(
                [{"name": name, "description": "Confirmation cards settled outside chat "
                                               "(sent or deleted in the mail client)."}],
                invite_only=True, history_public_to_subscribers=True,
                principals=[self.bot_email])
            if not _ok(res):
                log.warning("zulip: could not make stream %s: %s", name, _err(res))
                return None
            sid = self._lookup_stream_id(name)
            if sid is None:
                return None
        info = self._get_stream(sid)
        if info is None or not info.get("invite_only"):
            log.warning("zulip: stream %s is not private; no card is filed there", name)
            return None
        members = self.channel_members(f"stream:{sid}")
        if members is None or str(self.self_id) not in members \
                or not members <= {str(self.self_id), str(self.owner_id)}:
            log.warning("zulip: stream %s has subscribers besides the owner and the bot, or "
                        "none the bot can read; no card is filed there", name)
            return None
        self._stream_names[sid] = name
        return sid

    def message_posted_at(self, ref: MessageRef) -> float | None:
        try:
            m = self._fetch_message(int(ref.id))
        except (ValueError, ChatError):
            return None
        return float(m["timestamp"]) if m and m.get("timestamp") else None

    # conversations and rooms

    def dm_channel(self, user_id: str) -> str:
        if self.dm_stream_id is None:
            return f"dm:{int(user_id)}"
        return f"stream:{self.dm_stream_id}"

    def is_dm(self, channel: str) -> bool:
        if channel.startswith("dm:"):
            return "-" not in channel
        return self.dm_stream_id is not None and channel == f"stream:{self.dm_stream_id}"

    def _stream_name(self, stream_id: int) -> str:
        name = self._stream_names.get(stream_id)
        if name is None:
            info = self._get_stream(stream_id)
            name = str(info.get("name") or "") if info else ""
        return name

    def channel_info(self, channel: str) -> ChannelInfo:
        if channel.startswith("dm:"):
            return ChannelInfo(channel, "", is_private=True, is_member=True,
                               is_dm=self.is_dm(channel))
        sid = stream_id_of(channel)
        info = self._get_stream(sid)
        if info is None:
            return ChannelInfo(channel, "", is_private=False, is_member=False, exists=False)
        try:
            member = sid in self._subscribed_ids()
        except ChatError:
            member = False
        return ChannelInfo(channel, str(info.get("name") or ""),
                           is_private=bool(info.get("invite_only")), is_member=member,
                           is_archived=bool(info.get("is_archived")),
                           is_dm=self.is_dm(channel),
                           description=str(info.get("description") or ""))

    def channel_members(self, channel: str) -> set[str] | None:
        """The subscribers of a stream (GET /streams/{id}/members); for a
        one-to-one DM, the bot and the other user. None on a failure."""
        if channel.startswith("dm:"):
            try:
                ids = _dm_ids(channel)
            except ChatError:
                return None
            return {str(i) for i in ids} | {str(self.self_id)}
        try:
            res = self._call(f"streams/{stream_id_of(channel)}/members")
        except Exception:
            return None
        if not _ok(res) or not isinstance(res.get("subscribers"), list):
            return None
        return {str(int(i)) for i in res["subscribers"]}

    def mention(self, channel: str) -> str:
        try:
            sid = stream_id_of(channel)
        except ChatError:
            return channel
        name = self._stream_name(sid)
        return f"#**{name}**" if name else channel

    def room_url(self, channel: str) -> str:
        if channel.startswith("dm:"):
            return f"{self.site}/#narrow/dm/{self._dm_url_ids(channel)}-dm"
        sid = stream_id_of(channel)
        return f"{self.site}/#narrow/channel/{_stream_slug(sid, self._stream_name(sid))}"

    def _dm_url_ids(self, channel: str) -> str:
        ids = _dm_ids(channel)
        if len(ids) == 1 and str(ids[0]) == self.owner_id and self.self_id:
            ids = [self.self_id]
        return ",".join(str(i) for i in ids)

    def permalink(self, channel: str, msg_id: str) -> str:
        if channel.startswith("dm:"):
            return f"{self.site}/#narrow/dm/{self._dm_url_ids(channel)}-dm/near/{msg_id}"
        hit = self._msg_topics.get(int(msg_id))
        topic = hit[1] if hit else self._fetch_topic(int(msg_id))[1]
        base = self.room_url(channel)
        if topic is None:
            return f"{base}/near/{msg_id}"
        return f"{base}/topic/{encode_hash_component(topic)}/near/{msg_id}"

    def _narrow_for_key(self, key: str) -> list[dict]:
        ch, topic = parse_thread_key(key)
        if ch.startswith("stream:"):
            return [{"operator": "channel", "operand": stream_id_of(ch)},
                    {"operator": "topic", "operand": topic or ""}]
        return [{"operator": "dm", "operand": _dm_ids(ch)}]

    def _narrow_for_channel(self, channel: str) -> list[dict]:
        if channel.startswith("stream:"):
            return [{"operator": "channel", "operand": stream_id_of(channel)}]
        return [{"operator": "dm", "operand": _dm_ids(channel)}]

    def _history(self, narrow: list[dict], limit: int) -> tuple[list[dict], bool]:
        res = self.client.get_messages({"anchor": "newest", "num_before": int(limit),
                                        "num_after": 0, "apply_markdown": False,
                                        "narrow": narrow})
        if not _ok(res):
            raise ChatError(f"zulip get_messages failed: {_err(res)}")
        msgs = sorted(res.get("messages") or [], key=lambda m: int(m.get("id", 0)))
        return msgs, bool(res.get("found_oldest"))

    def _to_inbound(self, m: dict, is_reply: bool) -> InboundMessage | None:
        msg = inbound_from_message(m, self.self_id, self.owner_id, self.dm_stream_id,
                                   lambda _k, _i: is_reply, self.site)
        if msg is not None:
            topic = parse_thread_key(msg.thread_key)[1] if msg.thread_key else None
            self._remember(int(msg.msg_id), msg.channel, topic)
        return msg

    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        channel, thread_ts = self._follow(channel, thread_ts)
        if thread_ts.isdigit():
            ch, topic = self._msg_topics.get(int(thread_ts)) or self._fetch_topic(int(thread_ts))
            thread_ts = self._key(ch or channel, topic)
        msgs, found_oldest = self._history(self._narrow_for_key(thread_ts), limit)
        out = []
        for i, m in enumerate(msgs):
            msg = self._to_inbound(m, is_reply=not (i == 0 and found_oldest))
            if msg is not None:
                out.append(msg)
        return out

    def recent_messages(self, channel: str, since_s: float, limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        """One root per topic among the newest 200 messages posted after
        since_s (and after since_id when given): the topic's earliest such
        message, with reply_count the number of later ones."""
        msgs, _ = self._history(self._narrow_for_channel(channel), 200)
        floor = int(since_id) if since_id else 0
        roots: dict[str, InboundMessage] = {}
        for m in msgs:
            if float(m.get("timestamp") or 0) <= since_s or int(m.get("id", 0)) <= floor:
                continue
            msg = self._to_inbound(m, is_reply=False)
            if msg is None:
                continue
            key = msg.thread_key or msg.channel
            if key in roots:
                roots[key].reply_count += 1
            else:
                roots[key] = msg
        out = sorted(roots.values(), key=lambda r: (r.posted_at, int(r.msg_id)))
        return out[:limit]

    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        if _ok(self.client.get_stream_id(name)):
            raise RoomNameTaken(name)
        res = self.client.add_subscriptions([{"name": name, "description": description}],
                                            invite_only=True, principals=[self.bot_email])
        if not _ok(res):
            msg = str(res.get("msg", "")).lower()
            if "already exists" in msg:
                raise RoomNameTaken(name)
            if "permission" in msg or "not allowed" in msg or res.get("code") == "UNAUTHORIZED":
                raise NotPermitted(_err(res))
            raise ChatError(f"zulip create stream failed: {_err(res)}")
        got = self.client.get_stream_id(name)
        if not _ok(got):
            raise ChatError(f"zulip created stream '{name}' but cannot find it: {_err(got)}")
        sid = int(got["stream_id"])
        self._stream_names[sid] = name
        channel = f"stream:{sid}"
        return RoomRef(channel, name, self.room_url(channel))

    def invite(self, channel: str, user_id: str) -> None:
        sid = stream_id_of(channel)
        res = self.client.add_subscriptions([{"name": self._stream_name(sid)}],
                                            principals=[int(user_id)])
        if not _ok(res):
            raise self._room_error(res)

    def set_description(self, channel: str, text: str) -> None:
        res = self.client.update_stream({"stream_id": stream_id_of(channel),
                                         "description": text})
        if not _ok(res):
            raise self._room_error(res)

    @staticmethod
    def _room_error(res: dict) -> ChatError:
        msg = str(res.get("msg", "")).lower()
        if "permission" in msg or "not allowed" in msg or res.get("code") == "UNAUTHORIZED":
            return NotPermitted(_err(res))
        return ChatError(_err(res))

    def archive(self, channel: str) -> None:
        sid = stream_id_of(channel)
        info = self._get_stream(sid)
        if info is not None and info.get("is_archived"):
            raise AlreadyArchived(channel)
        res = self.client.delete_stream(sid)
        if not _ok(res):
            msg = str(res.get("msg", "")).lower()
            if "archived" in msg or "deactivated" in msg:
                raise AlreadyArchived(channel)
            raise self._room_error(res)

    def unarchive(self, channel: str) -> None:
        sid = stream_id_of(channel)
        info = self._get_stream(sid)
        if info is not None and "is_archived" in info and not info["is_archived"]:
            raise NotArchived(channel)
        res = self.client.update_stream({"stream_id": sid, "is_archived": False})
        if _ok(res):
            if "is_archived" in (res.get("ignored_parameters_unsupported") or []):
                raise NotSupported("this Zulip server cannot unarchive streams (needs 11.0)")
            return
        msg = str(res.get("msg", "")).lower()
        if "is_archived" in msg or "unknown" in msg or "invalid parameter" in msg:
            raise NotSupported(f"this Zulip server cannot unarchive streams: {_err(res)}")
        if "not archived" in msg:
            raise NotArchived(channel)
        raise self._room_error(res)

    def archived_map(self) -> dict[str, bool]:
        try:
            res = self.client.get_streams(include_all=True, exclude_archived=False)
        except Exception:
            return {}
        if not _ok(res):
            return {}
        out = {}
        for s in res.get("streams") or []:
            sid = int(s["stream_id"])
            self._stream_names[sid] = str(s.get("name") or "")
            if s.get("invite_only") and sid != self.dm_stream_id:
                out[f"stream:{sid}"] = bool(s.get("is_archived"))
        return out

    # files

    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        channel, thread_ts = self._follow(channel, thread_ts)
        p = Path(path)
        name = filename or p.name
        # The topic first: a thread outside this channel raises here, before
        # the server holds an upload no message will ever link.
        topic = self._topic_for(channel, thread_ts, comment or title or name) \
            if channel.startswith("stream:") else None
        buf = io.BytesIO(p.read_bytes())
        buf.name = name
        res = self.client.upload_file(buf)
        if not _ok(res):
            raise ChatError(f"zulip upload_file failed: {_err(res)}")
        url = str(res.get("url") or res.get("uri") or "")
        shown = str(res.get("filename") or name)
        link = f"[{title or shown}]({url})"
        text = f"{comment}\n{link}" if comment else link
        mid = self._send(channel, mrkdwn_to_zulip(text, self.mention) if comment else text, topic)
        self._remember(mid, channel, topic)
        full = url if url.startswith("http") else f"{self.site}{url}"
        return FileRef(name=shown, url=full, id=url, title=title)

    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        import requests
        url = ref.url or ref.id
        if not url:
            return None
        if url.startswith("/"):
            url = f"{self.site}{url}"
        if not url.startswith(self.site + "/"):
            log.warning("zulip download refused for off-site url %s", url)
            return None
        dest = Path(dest_dir)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", ref.name or url.rsplit("/", 1)[-1]) or "file"
        name = name.lstrip(".") or "file"
        target = dest / name
        try:
            r = requests.get(url, auth=(self.bot_email, self._api_key), timeout=60)
        except Exception as e:
            log.warning("zulip download %s failed: %s", url, e)
            return None
        if r.status_code != 200:
            log.warning("zulip download %s: HTTP %s", url, r.status_code)
            return None
        # dest_dir is the resident's uploads dir: the directory and the file
        # are made with the resident's access, and the file replaces
        # whatever is at the name instead of writing through it.
        if not (_files.make_dir(dest, 0o2775) and _files.replace_bytes(target, r.content, 0o664)):
            log.warning("zulip download could not be saved to %s", target)
            return None
        return str(target)

    # LLM-facing

    def prompt_strings(self) -> dict[str, str]:
        return self.prompt_strings_for(self.dm_stream_name)

    @classmethod
    def prompt_strings_for(cls, dm_stream: str = "") -> dict[str, str]:
        """PROMPT_STRINGS with the install's DM stream name filled in."""
        name = dm_stream or "ea-dm"
        return {k: v.replace("{dm_stream}", name) for k, v in cls.PROMPT_STRINGS.items()}

    def llm_tool_grants(self) -> str:
        return ""
