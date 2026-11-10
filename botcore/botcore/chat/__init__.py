"""One interface in front of Slack, Zulip or an in-memory fake.

`ChatFrontend` (base.py) is every call a bot makes to a chat platform.
`SlackFrontend`, `ZulipFrontend` and `MemoryFrontend` implement it; import
the one you use from its module, so a Slack bot never imports the Zulip
client and the reverse. `ConfirmationCard` (cards.py) is an approval
button set that renders natively on each platform. `files.use()` installs
the application's writers for downloaded files.
"""
from __future__ import annotations

from . import files  # noqa: F401
from . import text  # noqa: F401
from .base import (
    AlreadyArchived,
    ChannelNotPrivate,
    ChatError,
    ChatFrontend,
    HandlerRegistry,
    Limits,
    NotArchived,
    NotPermitted,
    NotSupported,
    RoomNameTaken,
)
from .cards import (
    ACTION_TEXT_CHARS,
    CODE_RE,
    REACTION_ROLES,
    CardAction,
    CardField,
    CardRegistry,
    CardSelect,
    ConfirmationCard,
    action_text,
    cancel_action,
    parse_code,
    parse_code_text,
    primary_action,
    resolve_code,
    resolve_reaction,
)
from .types import (
    CODE_ALPHABET,
    THREAD_KEY_RE,
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
    ViewEvent,
)

__all__ = [
    "AlreadyArchived", "ChannelNotPrivate", "ChatError", "ChatFrontend", "HandlerRegistry", "Limits",
    "NotArchived", "NotPermitted", "NotSupported", "RoomNameTaken",
    "ACTION_TEXT_CHARS", "CODE_RE", "REACTION_ROLES", "CardAction", "CardField", "CardRegistry",
    "CardSelect", "ConfirmationCard", "action_text", "cancel_action", "parse_code",
    "parse_code_text", "primary_action", "resolve_code", "resolve_reaction",
    "CODE_ALPHABET", "THREAD_KEY_RE", "ActionEvent", "ChannelInfo", "FileRef",
    "InboundMessage", "MessageRef", "RoomRef", "Status", "ViewEvent",
    "files", "text",
]
