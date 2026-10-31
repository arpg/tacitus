"""The chat frontend interface, its handler registry and its errors.

ChatFrontend is every call the core makes to a chat platform. A frontend
translates platform events into the neutral types in chat.types and
dispatches them through its HandlerRegistry; the core registers its
handlers on that registry with the same decorator text it used on the
bolt App (`@app.action("approve_send")`), because the bot binds the name
`app` to `FRONTEND.registry`.
"""
from __future__ import annotations

import inspect
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Union

from .types import (
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
    ViewEvent,
)

if TYPE_CHECKING:
    from .cards import CardRegistry, ConfirmationCard

log = logging.getLogger("chat")


class Limits(NamedTuple):
    """Platform size limits, in characters; 0 means no limit."""
    message_chars: int
    card_body_chars: int
    room_name_chars: int
    topic_chars: int


class ChatError(Exception):
    """Base error for every chat frontend failure."""


class RoomNameTaken(ChatError):
    """A room with that name already exists."""


class NotPermitted(ChatError):
    """The bot lacks the scope or permission for the call."""


class AlreadyArchived(ChatError):
    """The room is already archived."""


class NotArchived(ChatError):
    """The room is not archived, so it cannot be unarchived."""


class NotSupported(ChatError):
    """The platform or server version cannot do this."""


class ChannelNotPrivate(ChatError):
    """One of the bot's own channels is public or holds someone besides the
    owner and the bot. Not transient: the bot does not start."""


Key = Union[str, re.Pattern]  # Union, not |: botcore supports Python 3.9
Handler = Callable[..., Any]


class HandlerRegistry:
    """Handlers for card actions, modal views, messages and other events.

    Each decorator records the function and returns it unchanged, so a
    test can still call the handler directly. Keys are exact strings or
    compiled patterns; dispatch tries the exact key first, then each
    pattern in registration order with `fullmatch`, and runs the first
    handler that matches. Message handlers (registered as
    `event("message")`) are kept apart: `dispatch_message` runs every one
    of them in order, and `dispatch_event` never calls them.

    Legacy bridge: while bolt-style handlers remain in the bot, a
    decorated action or view handler whose signature has a parameter
    named `body` is a bolt handler. When `bind_legacy` is set, the
    registry calls `bind_legacy(kind, key, fn)` for such a handler (kind
    is "action" or "view") instead of recording it, so the Slack frontend
    binds it straight to bolt. With `bind_legacy` unset every handler is
    recorded.
    """

    def __init__(self) -> None:
        self.bind_legacy: Callable[[str, Key, Handler], None] | None = None
        self._actions: dict[str, Handler] = {}
        self._action_patterns: list[tuple[re.Pattern, Handler]] = []
        self._views: dict[str, Handler] = {}
        self._view_patterns: list[tuple[re.Pattern, Handler]] = []
        self._events: dict[str, Handler] = {}
        self._event_patterns: list[tuple[re.Pattern, Handler]] = []
        self._message_handlers: list[Handler] = []

    @staticmethod
    def _is_legacy(fn: Handler) -> bool:
        try:
            return "body" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _record(key: Key, fn: Handler, exact: dict, patterns: list) -> None:
        if isinstance(key, re.Pattern):
            patterns.append((key, fn))
        else:
            exact[key] = fn

    def _decorator(self, kind: str, key: Key, exact: dict, patterns: list):
        def wrap(fn: Handler) -> Handler:
            if self.bind_legacy is not None and self._is_legacy(fn):
                self.bind_legacy(kind, key, fn)
            else:
                self._record(key, fn, exact, patterns)
            return fn
        return wrap

    def action(self, key: Key):
        """Register a card action handler, called with an ActionEvent."""
        return self._decorator("action", key, self._actions, self._action_patterns)

    def view(self, callback_id: Key):
        """Register a modal submission handler, called with a ViewEvent."""
        return self._decorator("view", callback_id, self._views, self._view_patterns)

    def event(self, name: Key):
        """Register an event handler; "message" handlers get an InboundMessage."""
        def wrap(fn: Handler) -> Handler:
            if name == "message":
                self._message_handlers.append(fn)
            else:
                self._record(name, fn, self._events, self._event_patterns)
            return fn
        return wrap

    @staticmethod
    def _find(key: str, exact: dict, patterns: list) -> Handler | None:
        fn = exact.get(key)
        if fn is not None:
            return fn
        for pat, pfn in patterns:
            if pat.fullmatch(key):
                return pfn
        return None

    def dispatch_action(self, ev: ActionEvent) -> bool:
        """Run the handler for ev.action_id; True when one ran."""
        fn = self._find(ev.action_id, self._actions, self._action_patterns)
        if fn is None:
            return False
        fn(ev)
        return True

    def dispatch_view(self, ev: ViewEvent) -> bool:
        """Run the handler for ev.callback_id; True when one ran."""
        fn = self._find(ev.callback_id, self._views, self._view_patterns)
        if fn is None:
            return False
        fn(ev)
        return True

    def dispatch_message(self, msg: InboundMessage) -> bool:
        """Run every message handler in order; True when at least one ran."""
        for fn in self._message_handlers:
            fn(msg)
        return bool(self._message_handlers)

    def dispatch_event(self, name: str, payload: Any) -> bool:
        """Run the handler for a non-message event; True when one ran."""
        if name == "message":
            return False
        fn = self._find(name, self._events, self._event_patterns)
        if fn is None:
            return False
        fn(payload)
        return True


class ChatFrontend(ABC):
    """Every call the core makes to a chat platform.

    Attributes set by each frontend: name, capabilities (a set of
    strings such as "buttons", "reactions", "codes"), limits,
    main_channel and main_channel_name (the EA channel), sched_channel
    ("" when there is none), owner_id, bot_user_id, and registry.
    """

    name: str = ""
    capabilities: frozenset[str] | set[str] = frozenset()
    # The card registry the frontend resolves reactions and codes against.
    # A frontend with the "registers_cards" capability writes the row itself
    # in post_card (the code must appear in the rendered text); otherwise the
    # core writes it after post_card. The core shares this instance, so the
    # file has one in-memory owner per process.
    card_registry: "CardRegistry | None" = None
    limits: Limits = Limits(0, 0, 0, 0)
    main_channel: str = ""
    main_channel_name: str = ""
    sched_channel: str = ""
    owner_id: str = ""
    bot_user_id: str = ""
    # Titles a new thread from (opening text, channel): a short name, or
    # None to fall back to the frontend's own rule. The core sets it; a
    # frontend whose threads carry names (Zulip topics) calls it, off its
    # event loop because it may take seconds, and others never do.
    thread_namer: Callable[[str, str], str | None] | None = None
    # True when the text of the messages this frontend reads back
    # (thread_replies, recent_messages) is already the Markdown the web chat
    # renders (Zulip's); False when it is the Slack mrkdwn the core writes.
    history_markdown: bool = False
    # Shadow mode: the composite that mirrors this frontend (ShadowFrontend)
    # sets a callable here, and the frontend calls it through _outbound
    # after a write it makes on its own, outside the verbs the core calls
    # through the composite. The verbs are listed in shadow_frontend. None
    # leaves every frontend as it is.
    on_outbound: Callable[..., Any] | None = None

    def __init__(self) -> None:
        self.registry = HandlerRegistry()

    def _outbound(self, verb: str, **args: Any) -> None:
        """Tell on_outbound about one of the frontend's own writes. A failure
        there is logged and dropped: the platform write stands."""
        hook = self.on_outbound
        if hook is None:
            return
        try:
            hook(verb, **args)
        except Exception:
            log.warning("chat: the outbound hook failed on %s", verb, exc_info=True)

    # lifecycle

    @abstractmethod
    def start(self) -> None:
        """Connect, identify the bot and validate the configured rooms."""

    @abstractmethod
    def run_forever(self) -> None:
        """Receive events and dispatch them until the process stops."""

    @abstractmethod
    def ping(self) -> bool:
        """True when the platform answers an authenticated call."""

    def supports(self, cap: str) -> bool:
        """True when the frontend has the named capability."""
        return cap in self.capabilities

    # messages

    @abstractmethod
    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        """Post text to a channel, in a thread when thread_ts is given."""

    def channel_top(self, channel: str) -> str | None:
        """The thread_ts that posts at the channel's top level, outside every
        conversation: where a room's opening context belongs. None when a
        post with no thread already lands there."""
        return None

    @abstractmethod
    def update(self, ref: MessageRef, text: str) -> bool:
        """Replace the text of a posted message."""

    @abstractmethod
    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        """Add a status reaction to a message."""

    @abstractmethod
    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        """Remove a status reaction from a message."""

    # cards

    @abstractmethod
    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        """Post a confirmation card rendered for this platform."""

    def card_fits(self, card: ConfirmationCard) -> bool:
        """True when one card on this platform shows every character of the
        card's body and quoted thread. A Send approval covers the whole
        body, so a draft card that does not fit offers no Send. This
        default counts against the card body limit; a frontend that can
        show more in one card says so."""
        limit = self.limits.card_body_chars or 2800
        return len(card.body or "") + len(card.quoted or "") <= limit

    @abstractmethod
    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        """Re-render a posted card in place."""

    @abstractmethod
    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        """Replace a card's actions with a final status line."""

    @abstractmethod
    def message_posted_at(self, ref: MessageRef) -> float | None:
        """Epoch seconds when the message was posted, None when unknown."""

    def file_card(self, ref: MessageRef) -> bool:
        """Take a card settled outside chat (a send from the mail client)
        out of the owner's unread view, keeping it readable. False when the
        platform has no such place or the move fails; the card stays."""
        return False

    # conversations and rooms

    @abstractmethod
    def dm_channel(self, user_id: str) -> str:
        """The channel that serves as the user's DM with the bot."""

    @abstractmethod
    def is_dm(self, channel: str) -> bool:
        """True when the channel is a DM with the bot."""

    @abstractmethod
    def channel_info(self, channel: str) -> ChannelInfo:
        """Privacy, membership and archive state of a channel."""

    def channel_members(self, channel: str) -> set[str] | None:
        """The user ids subscribed to a room, read from the platform; None
        when the platform cannot say. The core posts to a registered room
        only when these are the owner and the bot."""
        return None

    @abstractmethod
    def mention(self, channel: str) -> str:
        """Text that renders as a clickable reference to the channel."""

    @abstractmethod
    def room_url(self, channel: str) -> str:
        """A web URL that opens the channel."""

    @abstractmethod
    def permalink(self, channel: str, msg_id: str) -> str:
        """A web URL that opens one message."""

    @abstractmethod
    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        """Messages in a thread, oldest first, the parent included."""

    @abstractmethod
    def recent_messages(self, channel: str, since_s: float,
                        limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        """Top-level messages posted after the epoch time since_s, oldest
        first; with since_id, only messages whose id sorts after it (a
        frontend with numeric ids compares them as numbers)."""

    @abstractmethod
    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        """Create a private room; raises RoomNameTaken on a duplicate name."""

    @abstractmethod
    def invite(self, channel: str, user_id: str) -> None:
        """Add a user to a room."""

    @abstractmethod
    def set_description(self, channel: str, text: str) -> None:
        """Set a room's purpose or description."""

    @abstractmethod
    def archive(self, channel: str) -> None:
        """Archive a room; raises AlreadyArchived when it is archived."""

    @abstractmethod
    def unarchive(self, channel: str) -> None:
        """Unarchive a room; raises NotArchived or NotSupported."""

    @abstractmethod
    def archived_map(self) -> dict[str, bool]:
        """Room id to archived flag for every private room the bot is in; {} on failure."""

    # files

    @abstractmethod
    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        """Upload a local file to a channel or thread."""

    @abstractmethod
    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        """Download an attached file into dest_dir; the local path, or None."""

    # Slack-only surfaces; other platforms keep these defaults

    def publish_home(self, user_id: str, view: dict) -> bool:
        """Publish an App Home view; False where the platform has none."""
        return False

    def open_modal(self, trigger_id: str, view: dict) -> bool:
        """Open a modal view; False where the platform has none."""
        return False

    # LLM-facing

    @abstractmethod
    def prompt_strings(self) -> dict[str, str]:
        """Platform wording interpolated into the LLM prompts."""

    @abstractmethod
    def llm_tool_grants(self) -> str:
        """Extra tool names granted to the LLM on this platform, comma separated."""
