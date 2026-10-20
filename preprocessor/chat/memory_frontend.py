"""An in-process chat frontend for tests.

MemoryFrontend records every outbound call as a dict in a list (posts,
cards, updates, reactions, uploads, room_ops, home_views, modals) and
hands out ids "m1", "m2", ... It has no network. Tests drive it inward
with inject_message, click, react_confirm and type_code, which dispatch
through the same HandlerRegistry the real frontends use; react_confirm
and type_code go through the same pure resolvers as the Zulip frontend.
"""
from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import Any

from .base import AlreadyArchived, ChatError, ChatFrontend, Limits, NotArchived, RoomNameTaken
from .cards import CardRegistry, ConfirmationCard, resolve_code, resolve_reaction
from .types import (
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
)

# Slack-flavoured prompt wording, so a prompt assembled on this frontend
# reads like a Slack one.
SLACK_LIKE_PROMPT_STRINGS = {
    "platform": "Slack",
    "format_rule_main": "Format for Slack mrkdwn: *bold*, _italic_, <url|text> links.",
    "format_rule_reply": "Your reply is posted to a Slack thread in Slack mrkdwn.",
    "confirm_location": "the Send button is in your DM with the bot",
    "confirm_sentence": "The bot posts a :lock: confirmation with Send and Cancel buttons in your DM.",
    "history_note": "Earlier messages in this Slack thread are included below.",
    "room_word": "channel",
    "continuation_header": "Continuing the Slack thread:",
    "output_only": "Your text output is posted to Slack automatically.",
    "output_only_resume": "Output ONLY the reply text.",
    "format_rule_closing": "Use Slack mrkdwn: *bold*, _italic_, :emoji_shortcodes:.",
    "confirm_sentence_wrapped": "buttons are posted to your DM with the bot.",
}


class MemoryFrontend(ChatFrontend):
    """A recording frontend with no network, for tests."""

    name = "memory"
    capabilities = frozenset({"buttons", "select", "reactions", "files",
                              "private_rooms", "codes"})
    limits = Limits(3000, 2800, 80, 0)
    main_channel = "C_MAIN"
    main_channel_name = "ea"
    sched_channel = ""
    owner_id = "U_OWNER"
    bot_user_id = "U_BOT"

    def __init__(self, card_registry: CardRegistry | None = None) -> None:
        super().__init__()
        self.card_registry = card_registry
        self.posts: list[dict] = []
        self.cards: list[dict] = []
        self.updates: list[dict] = []
        self.reactions: list[dict] = []
        self.uploads: list[dict] = []
        self.room_ops: list[dict] = []
        self.home_views: list[dict] = []
        self.modals: list[dict] = []
        self.created_rooms: list[RoomRef] = []
        self.filed_cards: list[MessageRef] = []
        self.history: list[InboundMessage] = []
        self.rooms: dict[str, ChannelInfo] = {
            "C_MAIN": ChannelInfo("C_MAIN", "ea", is_private=True, is_member=True),
            "D_OWNER": ChannelInfo("D_OWNER", "", is_private=True, is_member=True,
                                   is_dm=True),
        }
        # Room id -> member ids. A room with no row has the owner and the bot.
        self.members: dict[str, set[str]] = {}
        self._ids = itertools.count(1)
        self._room_ids = itertools.count(1)
        self._posted_at: dict[tuple[str, str], float] = {}

    @classmethod
    def from_config(cls, cfg: dict | None) -> MemoryFrontend:
        """Build one; cfg may override main_channel, main_channel_name,
        sched_channel, owner_id and bot_user_id, and name a card_registry
        file."""
        cfg = cfg or {}
        path = cfg.get("card_registry")
        fe = cls(CardRegistry(Path(path)) if path else None)
        for key in ("main_channel", "main_channel_name", "sched_channel",
                    "owner_id", "bot_user_id"):
            if isinstance(cfg.get(key), str):
                setattr(fe, key, cfg[key])
        return fe

    def _ref(self, channel: str, thread_ts: str | None) -> MessageRef:
        mid = f"m{next(self._ids)}"
        now = time.time()
        self._posted_at[(channel, mid)] = now
        return MessageRef(channel, mid, thread_ts or mid, now,
                          self.permalink(channel, mid))

    # lifecycle

    def start(self) -> None:
        """Nothing to connect."""

    def run_forever(self) -> None:
        """Returns at once; tests drive events by hand."""

    def ping(self) -> bool:
        return True

    # messages

    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        ref = self._ref(channel, thread_ts)
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts,
                           "unfurl": unfurl, "id": ref.id, "ref": ref})
        return ref

    def update(self, ref: MessageRef, text: str) -> bool:
        self.updates.append({"ref": ref, "text": text})
        return True

    @staticmethod
    def _emoji(status: Status | str) -> str:
        return status.value if isinstance(status, Status) else str(status)

    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        self.reactions.append({"op": "add", "channel": channel, "msg_id": msg_id,
                               "name": self._emoji(status)})
        return True

    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        self.reactions.append({"op": "remove", "channel": channel, "msg_id": msg_id,
                               "name": self._emoji(status)})
        return True

    # cards

    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        ref = self._ref(channel, thread_ts)
        self.cards.append({"channel": channel, "card": card, "thread_ts": thread_ts,
                           "id": ref.id, "ref": ref})
        return ref

    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        self.updates.append({"ref": ref, "card": card})
        return True

    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        self.updates.append({"ref": ref, "status": text})
        return True

    def file_card(self, ref: MessageRef) -> bool:
        self.filed_cards.append(ref)
        return True

    def message_posted_at(self, ref: MessageRef) -> float | None:
        return self._posted_at.get((ref.channel, ref.id))

    # conversations and rooms

    def dm_channel(self, user_id: str) -> str:
        return "D_OWNER"

    def is_dm(self, channel: str) -> bool:
        return channel.startswith("D")

    def channel_info(self, channel: str) -> ChannelInfo:
        info = self.rooms.get(channel)
        if info is None:
            return ChannelInfo(channel, "", is_private=False, is_member=False, exists=False)
        return info

    def channel_members(self, channel: str) -> set[str] | None:
        if channel in self.members:
            return set(self.members[channel])
        if channel in self.rooms:
            return {self.owner_id, self.bot_user_id}
        return None

    def mention(self, channel: str) -> str:
        return f"<#{channel}>"

    def room_url(self, channel: str) -> str:
        return f"memory://{channel}"

    def permalink(self, channel: str, msg_id: str) -> str:
        return f"memory://{channel}/{msg_id}"

    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        out = [m for m in self.history if m.channel == channel
               and (m.thread_key == thread_ts or m.msg_id == thread_ts)]
        return out[:limit]

    def recent_messages(self, channel: str, since_s: float,
                        limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        out = [m for m in self.history if m.channel == channel
               and m.posted_at > since_s and not m.is_reply]
        if since_id is not None:
            ids = [m.msg_id for m in self.history]
            if since_id in ids:
                later = set(ids[ids.index(since_id) + 1:])
                out = [m for m in out if m.msg_id in later]
        return out[:limit]

    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        if any(info.name == name for info in self.rooms.values()):
            raise RoomNameTaken(name)
        cid = f"C_{next(self._room_ids)}"
        self.rooms[cid] = ChannelInfo(cid, name, is_private=True, is_member=True)
        room = RoomRef(cid, name, self.room_url(cid))
        self.created_rooms.append(room)
        self.room_ops.append({"op": "create", "channel": cid, "name": name,
                              "description": description})
        return room

    def invite(self, channel: str, user_id: str) -> None:
        if channel in self.members:
            self.members[channel].add(user_id)
        self.room_ops.append({"op": "invite", "channel": channel, "user_id": user_id})

    def set_description(self, channel: str, text: str) -> None:
        self.room_ops.append({"op": "describe", "channel": channel, "text": text})

    def _room(self, channel: str) -> ChannelInfo:
        info = self.rooms.get(channel)
        if info is None:
            raise ChatError(f"channel_not_found: {channel}")
        return info

    def archive(self, channel: str) -> None:
        info = self._room(channel)
        if info.is_archived:
            raise AlreadyArchived(channel)
        info.is_archived = True
        self.room_ops.append({"op": "archive", "channel": channel})

    def unarchive(self, channel: str) -> None:
        info = self._room(channel)
        if not info.is_archived:
            raise NotArchived(channel)
        info.is_archived = False
        self.room_ops.append({"op": "unarchive", "channel": channel})

    def archived_map(self) -> dict[str, bool]:
        return {cid: info.is_archived for cid, info in self.rooms.items()
                if info.is_private and not info.is_dm}

    # files

    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        n = len(self.uploads) + 1
        ref = FileRef(filename or Path(path).name, f"memory://files/F{n}", f"F{n}",
                      "", title)
        self.uploads.append({"channel": channel, "path": str(path), "thread_ts": thread_ts,
                             "filename": filename, "title": title, "comment": comment,
                             "file": ref})
        return ref

    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        return None

    # Slack-only surfaces: recorded, and reported as done so the core's
    # Slack path runs under test

    def publish_home(self, user_id: str, view: dict) -> bool:
        self.home_views.append({"user_id": user_id, "view": view})
        return True

    def open_modal(self, trigger_id: str, view: dict) -> bool:
        self.modals.append({"trigger_id": trigger_id, "view": view})
        return True

    # LLM-facing

    def prompt_strings(self) -> dict[str, str]:
        return dict(SLACK_LIKE_PROMPT_STRINGS)

    def llm_tool_grants(self) -> str:
        return ""

    # test drivers

    def inject_message(self, **fields: Any) -> InboundMessage:
        """Build an InboundMessage (owner, main channel, fresh id by
        default), dispatch it to the message handlers and return it."""
        channel = fields.pop("channel", self.main_channel)
        fields.setdefault("msg_id", f"m{next(self._ids)}")
        fields.setdefault("text", "")
        fields.setdefault("user_id", self.owner_id)
        fields.setdefault("posted_at", time.time())
        fields.setdefault("is_dm", self.is_dm(channel))
        fields.setdefault("is_self", fields["user_id"] == self.bot_user_id)
        msg = InboundMessage(channel=channel, **fields)
        self.registry.dispatch_message(msg)
        return msg

    def _card_value(self, card_ref: MessageRef, action_id: str) -> dict | str:
        for rec in self.cards:
            if rec["channel"] == card_ref.channel and rec["id"] == card_ref.id:
                for a in rec["card"].actions:
                    if a.id == action_id:
                        return a.value
        if self.card_registry is not None:
            row = self.card_registry.by_ref(card_ref.channel, card_ref.id) or {}
            action = (row.get("actions") or {}).get(action_id)
            if action:
                return action.get("value", "")
        return ""

    def click(self, card_ref: MessageRef, action_id: str, user_id: str,
              value: dict | str | None = None) -> ActionEvent:
        """Press a card button and dispatch the resulting ActionEvent."""
        ev = ActionEvent(action_id=action_id, user_id=user_id, card=card_ref,
                         value=self._card_value(card_ref, action_id) if value is None else value,
                         source="button")
        self.registry.dispatch_action(ev)
        return ev

    def react_confirm(self, card_ref: MessageRef, emoji: str,
                      user_id: str) -> ActionEvent | None:
        """React on a card; dispatch and return the event it resolves to."""
        if self.card_registry is None:
            return None
        row = self.card_registry.by_ref(card_ref.channel, card_ref.id)
        ev = resolve_reaction(row, emoji, user_id, self.owner_id)
        if ev is not None:
            self.registry.dispatch_action(ev)
        return ev

    def type_code(self, text: str, user_id: str,
                  thread_key: str | None = None) -> ActionEvent | None:
        """Type a code reply in thread `thread_key`; dispatch and return the
        event it resolves to (a code counts only in its card's thread)."""
        if self.card_registry is None:
            return None
        ev = resolve_code(text, self.card_registry, user_id, self.owner_id,
                          thread_key=thread_key)
        if ev is not None:
            self.registry.dispatch_action(ev)
        return ev
