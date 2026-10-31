"""Neutral chat types shared by the core and every frontend.

The core speaks in these types; each frontend (Slack, Zulip, the
in-memory fake) translates its platform's events and ids into them.

Identity is opaque: `channel`, `id` and `thread_key` are strings whose
shape belongs to the frontend. Slack values are the platform's own
bytes (channel ids, message ts), so every state file keyed by them
stays valid. Zulip values look like `stream:<id>` for a channel and
`stream:<id>:topic:<urlquoted topic>` for a thread. THREAD_KEY_RE is the
one validation every key must pass: no whitespace and no `]`, so a key
survives inside the bracketed markers the bridge writes. The 400-character
cap leaves room for a percent-encoded non-ASCII Zulip topic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

THREAD_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]{0,399}$")

# Confirmation codes use an alphabet without 0/O and 1/I, so a code read
# off a phone screen cannot be mistyped between look-alike characters.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Status(str, Enum):
    """Reaction states on a user's message. Values are Slack emoji names;
    a frontend with other names maps them itself."""
    EYES = "eyes"
    WAITING = "hourglass_flowing_sand"
    DONE = "white_check_mark"
    FAILED = "x"
    WARNING = "warning"
    PAUSED = "double_vertical_bar"
    QUESTION = "question"


@dataclass(frozen=True)
class MessageRef:
    """A posted message: where it is, its id, and the thread it belongs to."""
    channel: str
    id: str
    thread_key: str
    posted_at: float | None = None
    permalink: str = ""


@dataclass(frozen=True)
class RoomRef:
    """A room (channel or stream) the frontend created or found."""
    id: str
    name: str
    url: str = ""


@dataclass(frozen=True)
class FileRef:
    """An uploaded or attached file."""
    name: str
    url: str = ""
    id: str = ""
    mimetype: str = ""
    title: str = ""


@dataclass
class ChannelInfo:
    """What the core needs to know about a channel before answering in it."""
    id: str
    name: str
    is_private: bool
    is_member: bool
    is_archived: bool = False
    is_dm: bool = False
    exists: bool = True
    # The room's purpose or description; "" when the platform gives none.
    description: str = ""


@dataclass
class InboundMessage:
    """One message the frontend received.

    subtype is "" for a plain message, or one of "file_share",
    "thread_broadcast", "channel_rename" (new_name set) and
    "other:<name>" for anything the core ignores.
    """
    channel: str
    msg_id: str
    text: str
    user_id: str
    posted_at: float
    thread_key: str | None = None
    is_reply: bool = False
    is_dm: bool = False
    is_bot: bool = False
    is_self: bool = False
    subtype: str = ""
    new_name: str = ""
    files: list[FileRef] = field(default_factory=list)
    reply_count: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class ActionEvent:
    """One owner decision on a card, however it arrived.

    source is "button", "select", "reaction" or "code"; the core handles
    all four the same way.
    """
    action_id: str
    user_id: str
    card: MessageRef
    value: dict | str = ""
    source: str = "button"
    selected: str = ""
    block_id: str = ""
    trigger_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ViewEvent:
    """A submitted modal view (Slack only)."""
    callback_id: str
    user_id: str
    private_metadata: str
    values: dict
    raw: dict = field(default_factory=dict)
