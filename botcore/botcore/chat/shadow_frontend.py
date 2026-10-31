"""Shadow mode: a primary chat front end mirrored into a second one.

ShadowFrontend(primary, shadow) is the ChatFrontend the core runs on while
the owner tries a new front end (the web chat) against live traffic. The
primary (Zulip or Slack) stays the source of truth: every id, thread key,
card ref and file the core sees is the primary's, and every read goes to
the primary. The shadow holds a copy keyed by the primary's ids, and is a
second place where the owner can type and click.

Attributes. `registry`, `card_registry`, `capabilities`, `limits`, the
channel ids, `owner_id`, `bot_user_id` and `thread_namer` are the
primary's (the core's writes to `card_registry` and `thread_namer` land on
the primary); `name` is "<primary>+<shadow>". Any other attribute the core
reads (Zulip's `cfg`, Slack's `client`) comes from the primary.

Outbound. post, update, react, unreact, post_card, update_card,
replace_card_with_status and upload call the primary, then copy the result
into the shadow under the primary's refs. create_private_room, archive,
unarchive, set_description and invite act on the primary, then copy the
channel row (with its description where one is known). A shadow failure
is logged and dropped: the core gets the primary's result whatever the
shadow does. start() also copies the channel list: the main and
scheduling channels, the owner's DM and the rooms in the primary's
archived_map() that `room_filter` keeps (every one when it is None; the
bot keeps the rooms it made for the owner). A room the core posts to
later is copied then.

History. start() then copies the primary's recent history into the
shadow on a daemon thread, `backfill_days` back (0, the default here,
copies none; load_frontend passes the chat block's web.backfill_days):
in each channel the shadow holds, every thread with a top-level message
in the window (recent_messages), whole (thread_replies), under the
primary's ids and times. The author is the owner for the primary's
owner id and for a web message (WEB_PREFIX), else the bot; a card goes
in as the plain text the primary shows. The shadow takes these rows
quietly (mirror_post and mirror_file with quiet=True): no event per row,
never unread, a row it holds left as it is, so a second start copies
nothing twice. Each channel that took rows then gets one
mirror_backfilled(channel), one event, so an open app reads it again.
The copy stops at `backfill_cap` messages, and a read the primary fails
ends that channel's copy. While it runs, live mirroring waits (up to
`backfill_hold_s`, then it writes what waits and goes on), so a thread's
history goes in before the replies the bot posts on waking.

Inbound from the primary. The composite wraps the dispatch_message of the
primary's registry, so an owner's message is copied into the shadow
(author "owner") before the core handles it, and a channel_rename renames
the row. It wraps dispatch_event too: a "thread_moved" event re-keys the
thread's rows (mirror_move) when its new channel is one the shadow
holds. Actions pass through.

The primary's own writes. The composite sets `primary.on_outbound`
(base.ChatFrontend); a frontend calls it after a write it makes outside
the verbs above (Zulip's card codes, an edit past the edit window, a
stream change), with a verb and keywords:

    post(ref, text)                 update(ref, text)
    card(ref, card)                 card_status(ref, text)
    reaction(channel, msg_id, status, on)
    owner_message(msg)              an owner's message the frontend handled
                                    itself, never dispatched as a message
    channel(channel[, name=, archived=, description=])

Inbound from the shadow. The composite sets `shadow.inbound` to a
callable; the shadow calls it with the owner's input instead of
dispatching that input itself, in either of two shapes:

    inbound(<InboundMessage>)    channel, text and thread_key (the primary's
                                 key, or None for a new thread); an upload
                                 carries its bytes' local path in
                                 raw["path"] and its name in files[0]
    inbound("message", channel=..., text=..., thread_key=..., files=[...])
        both return the MessageRef of the primary message that carries it
    inbound(<ActionEvent>)
    inbound("action", event=<ActionEvent>)
        both return True when a handler ran

A message is posted into the primary as the bot, after WEB_PREFIX and in
the Slack mrkdwn that post() takes, so it has a primary id, sits in the
primary thread and shows as the owner typed it. The shadow row is then
written under that id, author "owner", with the text as the owner typed
it; the core gets an InboundMessage from the primary's owner id with that
id and thread key. Each item of `files` is a dict {"path": <local path
of the stored bytes>, "name": <the file's name>, "file": <the shadow's own
FileRef>, optional}; a bare path, or a FileRef that shadow.download can
fetch, also serves. It goes up through primary.upload into the same
thread, the shadow row gets the shadow's FileRef (else the primary's) and
the core gets the primary's. The shadow reads the bytes before inbound
returns, so it may delete them then. A primary refusal
raises to the caller. An action reaches the core with the primary's owner
id; its card ref is the primary's already, and the value of an action
the card registry knows comes from the registry, not from the request.

The shadow's mirror entry points, each idempotent on (channel, id) and
called with positional arguments; a keyword in brackets is passed only
to a shadow whose method takes it:

    mirror_channel(id, name, kind, archived[, description=])
                                               kind: main, sched, dm, room
    mirror_post(ref, text, author[, quiet=, markdown=])
                                               author: "bot" or "owner"
    mirror_update(ref, text)
    mirror_card(ref, card)                     the ConfirmationCard itself
    mirror_card_status(ref, text)
    mirror_reaction(channel, msg_id, status, on)   status: a Status value
    mirror_file(ref, file, author[, path=, text=, quiet=, markdown=])
                                               file: a FileRef
    mirror_backfilled(channel)                 optional
    mirror_move(old_channel, old_key, new_channel, new_key)   optional

The history copy needs `quiet` on mirror_post and mirror_file; a shadow
without it gets none. With quiet the shadow returns None for a row it
did not take.

The text is as the primary got it: the core's Slack mrkdwn on the bot's
rows (the shadow converts it as its own post() would) and the owner's text
as typed. History from a primary whose `history_markdown` is True (Zulip)
goes with markdown=True: the bot's rows are Markdown already and the
shadow keeps them as they are. When the shadow's mirror_file takes
`path` and `text`, a file row gets the local bytes and its text in that
one call; otherwise the text goes first through mirror_post on the same
ref. A file row without
local bytes gets the primary's FileRef with an empty id, so the shadow
links to the primary's absolute url. A file the core uploads has no
primary message id (upload returns a FileRef), so its row id is
"file:<the primary's file id>" and its thread is the upload's thread, or
the row itself.
"""
from __future__ import annotations

import dataclasses
import inspect
import logging
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import ChatError, ChatFrontend
from .types import (
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
)

if TYPE_CHECKING:
    from .cards import ConfirmationCard

log = logging.getLogger("chat.shadow")

# Marks the owner's web message where the primary shows it as the bot's.
WEB_PREFIX = "**You (web):** "

# Messages the history copy takes at most, over every channel.
BACKFILL_CAP = 5000
# Top-level messages asked of the primary per channel.
BACKFILL_ROOTS = 500
# Seconds live mirroring waits for the history copy, and the writes it
# keeps waiting at most, before it writes them and goes on.
BACKFILL_HOLD_S = 60.0
HOLD_MAX = 2000

# The subtypes the core reads as a person's message.
_PERSON_SUBTYPES = ("", "file_share", "thread_broadcast")

# Markdown marks as the owner types them, for _to_mrkdwn.
_MD_CODE_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_BOLD_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_MD_STRIKE_RE = re.compile(r"~~(?=\S)([^~\n]+?)(?<=\S)~~")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([a-z][a-z0-9+.-]*:[^)\s]+)\)", re.IGNORECASE)


def _to_mrkdwn(text: str) -> str:
    """The owner's markdown as the Slack mrkdwn every primary's post()
    takes, so the primary shows what the owner typed: **bold** to *bold*,
    *italic* to _italic_, ~~strike~~ to ~strike~, [label](url) to
    <url|label>. Code spans and fences stay as written."""
    def plain(part: str) -> str:
        part = _MD_ITALIC_RE.sub(r"_\1_", part)
        part = _MD_BOLD_RE.sub(r"*\1*", part)
        part = _MD_STRIKE_RE.sub(r"~\1~", part)
        return _MD_LINK_RE.sub(r"<\2|\1>", part)
    parts = _MD_CODE_RE.split(text or "")
    return "".join(p if i % 2 else plain(p) for i, p in enumerate(parts))


def _from_primary(attr: str) -> property:
    """A property that reads and writes `attr` on the primary."""
    def get(self: ShadowFrontend) -> Any:
        return getattr(self.primary, attr)

    def put(self: ShadowFrontend, value: Any) -> None:
        setattr(self.primary, attr, value)

    return property(get, put, doc=f"The primary's {attr}.")


def _status_name(status: Status | str) -> str:
    return status.value if isinstance(status, Status) else str(status)


class ShadowFrontend(ChatFrontend):
    """A primary front end mirrored into a shadow; see the module docstring."""

    capabilities = _from_primary("capabilities")
    card_registry = _from_primary("card_registry")
    history_markdown = _from_primary("history_markdown")
    limits = _from_primary("limits")
    main_channel = _from_primary("main_channel")
    main_channel_name = _from_primary("main_channel_name")
    sched_channel = _from_primary("sched_channel")
    owner_id = _from_primary("owner_id")
    bot_user_id = _from_primary("bot_user_id")
    thread_namer = _from_primary("thread_namer")

    def __init__(self, primary: ChatFrontend, shadow: ChatFrontend, *,
                 backfill_days: float = 0.0, backfill_cap: int = BACKFILL_CAP) -> None:
        # ChatFrontend.__init__ is not called: the registry is the primary's.
        self.primary = primary
        self.shadow = shadow
        self.backfill_days = max(0.0, float(backfill_days or 0))
        self.backfill_cap = int(backfill_cap)
        self.backfill_hold_s = BACKFILL_HOLD_S
        # Picks the rooms of archived_map that start() copies; None takes all.
        self.room_filter: Callable[[str], bool] | None = None
        # What the last history copy did (backfill's report), None before one.
        self.backfill_report: dict | None = None
        self._backfill_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Channel id -> (name, kind, archived) of each row the shadow holds.
        self._channels: dict[str, tuple[str, str, bool]] = {}
        # Live writes waiting for the history copy; None when none wait.
        self._held: list[tuple[str, str, Callable[[], Any]]] | None = None
        self._hold_until = 0.0
        self._hold_lock = threading.Lock()
        self._release_lock = threading.Lock()
        registry = primary.registry
        self._dispatch_message = registry.dispatch_message
        registry.dispatch_message = self._primary_message
        self._dispatch_event = registry.dispatch_event
        registry.dispatch_event = self._primary_event
        primary.on_outbound = self._primary_outbound
        shadow.inbound = self._from_shadow

    def __getattr__(self, name: str) -> Any:
        # Runs only for a name the composite lacks: a platform's own
        # attribute (Zulip's cfg, Slack's client) comes from the primary.
        primary = self.__dict__.get("primary")
        if primary is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(primary, name)

    @property
    def name(self) -> str:
        return f"{self.primary.name}+{self.shadow.name}"

    @property
    def registry(self):
        return self.primary.registry

    # lifecycle

    def start(self) -> None:
        """Start the primary (its failure raises), then the shadow, then
        copy the channel list into the shadow and start the history copy."""
        self.primary.start()
        try:
            self.shadow.start()
        except Exception:
            log.exception("shadow: the shadow front end did not start")
        self.mirror_channels()
        self.start_backfill()

    def run_forever(self) -> None:
        """Run the shadow's loop on a daemon thread and the primary's here."""
        threading.Thread(target=self._run_shadow, name="chat-shadow", daemon=True).start()
        self.primary.run_forever()

    def _run_shadow(self) -> None:
        try:
            self.shadow.run_forever()
        except Exception:
            log.exception("shadow: the shadow front end stopped")

    def stop(self) -> None:
        """Ask both front ends to stop, where they can."""
        for fe in (self.shadow, self.primary):
            stop = getattr(fe, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    log.exception("shadow: stopping %s failed", fe.name)

    def ping(self) -> bool:
        return self.primary.ping()

    # channel rows

    def mirror_channels(self) -> int:
        """Copy the main and scheduling channels, the owner's DM and the
        rooms in the primary's archived_map that room_filter keeps into the
        shadow; the number of rows the shadow took."""
        p = self.primary
        rows: dict[str, tuple[str, bool | None]] = {}
        dm = ""
        if p.owner_id:
            try:
                dm = p.dm_channel(p.owner_id)
            except Exception:
                log.warning("shadow: no DM channel for the owner", exc_info=True)
        for cid, kind in ((p.main_channel, "main"), (p.sched_channel, "sched"), (dm, "dm")):
            if cid:
                rows.setdefault(cid, (kind, None))
        try:
            rooms = p.archived_map() or {}
        except Exception:
            log.warning("shadow: the primary's room list failed", exc_info=True)
            rooms = {}
        for cid, archived in rooms.items():
            if cid not in rows and self._room_wanted(cid):
                rows[cid] = ("room", bool(archived))
        return sum(self._mirror_channel(cid, kind=kind, archived=archived)
                   for cid, (kind, archived) in rows.items())

    def _room_wanted(self, channel: str) -> bool:
        """room_filter's answer for one room; a filter that fails keeps the
        room out (the core's first post there copies it)."""
        keep = self.room_filter
        if keep is None:
            return True
        try:
            return bool(keep(channel))
        except Exception:
            log.warning("shadow: the room filter failed on %s", channel, exc_info=True)
            return False

    def _kind_of(self, channel: str) -> str:
        p = self.primary
        if channel == p.main_channel:
            return "main"
        if p.sched_channel and channel == p.sched_channel:
            return "sched"
        return "dm" if self._is_dm(channel) else "room"

    def _mirror_channel(self, channel: str, *, name: str | None = None,
                        kind: str | None = None, archived: bool | None = None,
                        description: str | None = None) -> bool:
        """Write the channel's row into the shadow; True when it took it.
        A value not given comes from the row written before, else from the
        primary's channel_info; a description not given and not read there
        leaves the shadow's own."""
        with self._lock:
            known = self._channels.get(channel)
        if known is not None:
            name = known[0] if name is None else name
            kind = known[1] if kind is None else kind
            archived = known[2] if archived is None else archived
        info: ChannelInfo | None = None
        if name is None or archived is None:
            try:
                info = self.primary.channel_info(channel)
            except Exception:
                log.warning("shadow: channel_info(%s) failed", channel, exc_info=True)
        kind = kind or self._kind_of(channel)
        if name is None:
            name = (info.name if info else "") or (
                self.primary.main_channel_name if kind == "main" else "") or channel
        archived = bool(info.is_archived) if archived is None and info else bool(archived)
        if description is None and info is not None:
            description = getattr(info, "description", "") or None
        extra: dict[str, Any] = {}
        if description is not None and self._takes("mirror_channel", "description"):
            extra["description"] = description
        try:
            self.shadow.mirror_channel(channel, name, kind, archived, **extra)
        except Exception:
            log.warning("shadow: channel %s not mirrored", channel, exc_info=True)
            return False
        with self._lock:
            self._channels[channel] = (name, kind, archived)
        return True

    def _ensure_channel(self, channel: str) -> None:
        with self._lock:
            known = channel in self._channels
        if not known:
            self._mirror_channel(channel)

    # mirroring helpers

    def _mirror(self, what: str, channel: str, write: Callable[[], Any], *,
                hold: bool = True) -> None:
        """Run one shadow write, or keep it for later while the history copy
        holds live writes (`hold` False writes at once); a failure is logged
        and dropped."""
        if hold and self._defer((what, channel, write)):
            return
        self._write(what, channel, write)

    def _write(self, what: str, channel: str, write: Callable[[], Any]) -> None:
        try:
            self._ensure_channel(channel)
            write()
        except Exception:
            log.warning("shadow: %s in %s not mirrored", what, channel, exc_info=True)

    def _hold(self) -> None:
        """Keep live writes until _release, for backfill_hold_s at most."""
        with self._hold_lock:
            if self._held is None:
                self._held = []
            self._hold_until = time.monotonic() + self.backfill_hold_s

    def _defer(self, item: tuple[str, str, Callable[[], Any]]) -> bool:
        """True when the write waits for the history copy. Past the hold's
        time or size the waiting writes go out first, then this one."""
        with self._hold_lock:
            if self._held is None:
                return False
            if time.monotonic() < self._hold_until and len(self._held) < HOLD_MAX:
                self._held.append(item)
                return True
        log.info("shadow: the history copy outlasts its hold; live mirroring goes on")
        self._release()
        return False

    def _release(self) -> None:
        """Write the waiting live writes in their order and stop holding."""
        with self._release_lock:
            while True:
                with self._hold_lock:
                    batch = self._held or []
                    if not batch:
                        self._held = None
                        return
                    self._held = []
                for item in batch:
                    self._write(*item)

    def _takes(self, method: str, *names: str) -> set[str]:
        """Which of the optional keywords `names` the shadow's `method`
        takes."""
        try:
            params = inspect.signature(getattr(self.shadow, method)).parameters
        except (AttributeError, TypeError, ValueError):
            return set()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return set(names)
        return set(names) & set(params)

    def _file_options(self) -> set[str]:
        """Which of the optional `path` and `text` the shadow's mirror_file
        takes."""
        return self._takes("mirror_file", "path", "text")

    def _mirror_rows(self, ref: MessageRef, text: str, files: list[FileRef],
                     author: str, paths: list[str] | None = None) -> None:
        """A message's rows: its text, and each file (the first on the row
        itself, the others on rows `<id>:<n>`), with the file's local bytes
        when `paths` has them and the shadow takes them, else as a link to
        the primary's absolute url."""
        options = self._file_options() if files else set()
        if not files or (text and "text" not in options):
            self.shadow.mirror_post(ref, text, author)
        for n, f in enumerate(files):
            row = ref if n == 0 else dataclasses.replace(ref, id=f"{ref.id}:{n}")
            extra: dict[str, Any] = {}
            if n == 0 and text and "text" in options:
                extra["text"] = text
            path = paths[n] if paths and n < len(paths) else ""
            if path and "path" in options:
                extra["path"] = path
            elif "://" in (f.url or ""):
                # No bytes go with it: the shadow links to the primary's copy.
                f = dataclasses.replace(f, id="")
            self.shadow.mirror_file(row, f, author, **extra)

    def _serves(self, channel: str) -> bool:
        """True for a channel the shadow mirrors: one it holds a row for,
        the main or scheduling channel, or a DM."""
        p = self.primary
        with self._lock:
            if channel in self._channels:
                return True
        if channel and channel in (p.main_channel, p.sched_channel):
            return True
        return self._is_dm(channel)

    def _is_dm(self, channel: str) -> bool:
        try:
            return bool(self.primary.is_dm(channel))
        except Exception:
            return False

    # history

    def start_backfill(self) -> threading.Thread | None:
        """Start the history copy (backfill) on a daemon thread over the
        channels the shadow holds, holding live writes until it ends; the
        thread, or None when backfill_days is 0, the shadow takes no quiet
        history, or a copy is running."""
        if self.backfill_days <= 0:
            return None
        if self._takes("mirror_post", "quiet") != {"quiet"} or \
                self._takes("mirror_file", "quiet") != {"quiet"}:
            log.info("shadow: %s takes no quiet history; no backfill", self.shadow.name)
            return None
        running = self._backfill_thread
        if running is not None and running.is_alive():
            return None
        with self._lock:
            channels = list(self._channels)
        self._hold()
        t = threading.Thread(target=self._backfill_run, args=(channels,),
                             name="chat-shadow-backfill", daemon=True)
        self._backfill_thread = t
        t.start()
        return t

    def _backfill_run(self, channels: list[str]) -> None:
        try:
            self.backfill(channels)
        except Exception:
            log.exception("shadow: the history copy failed")
        finally:
            self._release()

    def backfill(self, channels: list[str] | None = None, *, days: float | None = None,
                 cap: int | None = None) -> dict:
        """Copy the primary's last `days` of history in each channel (by
        default every channel the shadow holds) into the shadow, quietly;
        see the module docstring. A message posted after the copy began is
        live traffic and is left to the live mirror. Returns and keeps in
        `backfill_report` {"copied": n, "channels": {channel: n},
        "failed": [channel], "capped": bool}."""
        days = self.backfill_days if days is None else max(0.0, float(days))
        cap = self.backfill_cap if cap is None else int(cap)
        if channels is None:
            with self._lock:
                channels = list(self._channels)
        until = time.time()
        since = until - days * 86400
        markdown = bool(getattr(self.primary, "history_markdown", False))
        opts = {
            verb: {"quiet": True, **({"markdown": True} if markdown and
                                     "markdown" in self._takes(verb, "markdown") else {})}
            for verb in ("mirror_post", "mirror_file")}
        files_take = self._file_options()
        report: dict = {"copied": 0, "channels": {}, "failed": [], "capped": False}
        for cid in channels:
            if report["copied"] >= cap:
                break
            n, ok = self._backfill_channel(cid, since, until, cap - report["copied"],
                                           opts, files_take)
            report["channels"][cid] = n
            report["copied"] += n
            if not ok:
                report["failed"].append(cid)
            if n:
                self._backfilled(cid)
            log.info("shadow: history copy: %d message(s) in %s%s", n, cid,
                     "" if ok else ", stopped on a read the primary failed")
        report["capped"] = report["copied"] >= cap
        log.info("shadow: history copy done: %d message(s) in %d channel(s), %d failed%s",
                 report["copied"], len(report["channels"]), len(report["failed"]),
                 f", stopped at the cap of {cap}" if report["capped"] else "")
        self.backfill_report = report
        return report

    def _backfill_channel(self, channel: str, since: float, until: float, room: int,
                          opts: dict, files_take: set[str]) -> tuple[int, bool]:
        """(messages copied, True unless a read of the primary failed) for
        one channel, at most `room` messages."""
        try:
            roots = self.primary.recent_messages(channel, since, BACKFILL_ROOTS)
        except Exception:
            log.warning("shadow: history copy of %s: the primary's history read failed",
                        channel, exc_info=True)
            return 0, False
        copied = 0
        threads: set[str] = set()
        for root in roots:
            key = root.thread_key or root.msg_id
            if not key or key in threads:
                continue
            threads.add(key)
            try:
                msgs = self.primary.thread_replies(channel, key) or [root]
            except Exception:
                log.warning("shadow: history copy of %s: the primary's thread read failed",
                            channel, exc_info=True)
                return copied, False
            for msg in msgs:
                if copied >= room:
                    return copied, True
                copied += self._backfill_message(channel, key, msg, until, opts, files_take)
        return copied, True

    def _history_author(self, msg: InboundMessage) -> tuple[str, str]:
        """(author, text) of a message from the primary's past: the owner's
        own, the owner's web message (posted as the bot after WEB_PREFIX),
        or the bot's."""
        p = self.primary
        text = msg.text or ""
        if p.owner_id and str(msg.user_id) == str(p.owner_id) and not msg.is_self:
            return "owner", text
        if msg.is_self or (p.bot_user_id and str(msg.user_id) == str(p.bot_user_id)):
            for prefix in (WEB_PREFIX, _to_mrkdwn(WEB_PREFIX)):
                if text.startswith(prefix):
                    return "owner", text[len(prefix):]
        return "bot", text

    def _backfill_message(self, channel: str, key: str, msg: InboundMessage, until: float,
                          opts: dict, files_take: set[str]) -> int:
        """Copy one message of the primary's past; the rows the shadow took."""
        sub = msg.subtype or ""
        if not msg.msg_id or (msg.channel and msg.channel != channel) \
                or (msg.posted_at or 0) > until or sub == "channel_rename" \
                or (sub.startswith("other:") and sub != "other:bot_message"):
            return 0
        author, text = self._history_author(msg)
        ref = MessageRef(channel, msg.msg_id, msg.thread_key or key, msg.posted_at)
        try:
            if not msg.files:
                return int(self.shadow.mirror_post(ref, text, author,
                                                   **opts["mirror_post"]) is not None)
            took = 0
            for n, f in enumerate(msg.files):
                row = ref if n == 0 else dataclasses.replace(ref, id=f"{ref.id}:{n}")
                if "://" in (f.url or ""):
                    # No bytes go with it: the shadow links to the primary's copy.
                    f = dataclasses.replace(f, id="")
                extra = dict(opts["mirror_file"])
                if n == 0 and text and "text" in files_take:
                    extra["text"] = text
                took += self.shadow.mirror_file(row, f, author, **extra) is not None
            return took
        except Exception:
            log.warning("shadow: history copy: message %s in %s not mirrored", msg.msg_id,
                        channel, exc_info=True)
            return 0

    def _backfilled(self, channel: str) -> None:
        """One event for a channel that took history, where the shadow has
        the call."""
        done = getattr(self.shadow, "mirror_backfilled", None)
        if callable(done):
            try:
                done(channel)
            except Exception:
                log.warning("shadow: the history event for %s failed", channel, exc_info=True)

    # messages

    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        ref = self.primary.post(channel, text, thread_ts=thread_ts, unfurl=unfurl)
        self._mirror("post", ref.channel,
                     lambda: self.shadow.mirror_post(ref, text, "bot"))
        return ref

    def channel_top(self, channel: str) -> str | None:
        return self.primary.channel_top(channel)

    def update(self, ref: MessageRef, text: str) -> bool:
        ok = self.primary.update(ref, text)
        if ok:
            self._mirror("update", ref.channel,
                         lambda: self.shadow.mirror_update(ref, text))
        return ok

    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        """A reaction reaches the shadow once the primary holds it."""
        ok = self.primary.react(channel, msg_id, status)
        if ok:
            self._mirror("reaction", channel, lambda: self.shadow.mirror_reaction(
                channel, msg_id, _status_name(status), True))
        return ok

    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        """A removal reaches the shadow whatever the primary answers, so the
        shadow never keeps a status the primary dropped."""
        ok = self.primary.unreact(channel, msg_id, status)
        self._mirror("reaction removal", channel, lambda: self.shadow.mirror_reaction(
            channel, msg_id, _status_name(status), False))
        return ok

    # cards

    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        ref = self.primary.post_card(channel, card, thread_ts=thread_ts)
        self._mirror("card", ref.channel, lambda: self.shadow.mirror_card(ref, card))
        return ref

    def card_fits(self, card: ConfirmationCard) -> bool:
        return self.primary.card_fits(card)

    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        ok = self.primary.update_card(ref, card)
        if ok:
            self._mirror("card update", ref.channel, lambda: self.shadow.mirror_card(ref, card))
        return ok

    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        ok = self.primary.replace_card_with_status(ref, text)
        if ok:
            self._mirror("card status", ref.channel,
                         lambda: self.shadow.mirror_card_status(ref, text))
        return ok

    def message_posted_at(self, ref: MessageRef) -> float | None:
        return self.primary.message_posted_at(ref)

    def file_card(self, ref: MessageRef) -> bool:
        return self.primary.file_card(ref)

    # conversations and rooms: reads go to the primary

    def dm_channel(self, user_id: str) -> str:
        return self.primary.dm_channel(user_id)

    def is_dm(self, channel: str) -> bool:
        return self.primary.is_dm(channel)

    def channel_info(self, channel: str) -> ChannelInfo:
        return self.primary.channel_info(channel)

    def channel_members(self, channel: str) -> set[str] | None:
        return self.primary.channel_members(channel)

    def mention(self, channel: str) -> str:
        return self.primary.mention(channel)

    def room_url(self, channel: str) -> str:
        return self.primary.room_url(channel)

    def permalink(self, channel: str, msg_id: str) -> str:
        return self.primary.permalink(channel, msg_id)

    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        return self.primary.thread_replies(channel, thread_ts, limit)

    def recent_messages(self, channel: str, since_s: float,
                        limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        return self.primary.recent_messages(channel, since_s, limit, since_id=since_id)

    def archived_map(self) -> dict[str, bool]:
        return self.primary.archived_map()

    # rooms: act on the primary, then copy the channel row

    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        room = self.primary.create_private_room(name, description)
        self._mirror_channel(room.id, name=room.name, kind="room", archived=False,
                             description=description or None)
        return room

    def invite(self, channel: str, user_id: str) -> None:
        self.primary.invite(channel, user_id)
        self._mirror_channel(channel)

    def set_description(self, channel: str, text: str) -> None:
        self.primary.set_description(channel, text)
        self._mirror_channel(channel, description=text or "")

    def archive(self, channel: str) -> None:
        self.primary.archive(channel)
        self._mirror_channel(channel, archived=True)

    def unarchive(self, channel: str) -> None:
        self.primary.unarchive(channel)
        self._mirror_channel(channel, archived=False)

    # files

    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        ref = self.primary.upload(channel, path, thread_ts=thread_ts, filename=filename,
                                  title=title, comment=comment)
        fid = f"file:{ref.id or ref.url or ref.name}"
        row = MessageRef(channel, fid, thread_ts or fid, time.time())
        # The core may remove the file once upload returns: the shadow reads
        # the bytes now, never after the history copy.
        self._mirror("upload", channel, lambda: self._mirror_rows(
            row, comment, [ref], "bot", [str(path)]), hold=False)
        return ref

    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        return self.primary.download(ref, dest_dir)

    # Slack-only surfaces

    def publish_home(self, user_id: str, view: dict) -> bool:
        return self.primary.publish_home(user_id, view)

    def open_modal(self, trigger_id: str, view: dict) -> bool:
        return self.primary.open_modal(trigger_id, view)

    # LLM-facing: the primary's wording, since the core answers there

    def prompt_strings(self) -> dict[str, str]:
        return self.primary.prompt_strings()

    def llm_tool_grants(self) -> str:
        return self.primary.llm_tool_grants()

    # inbound from the primary

    def _is_owner_input(self, msg: InboundMessage) -> bool:
        owner = str(self.primary.owner_id or "")
        return (bool(owner) and str(msg.user_id) == owner and not msg.is_self
                and not msg.is_bot and msg.subtype in _PERSON_SUBTYPES
                and self._serves(msg.channel))

    def _primary_message(self, msg: InboundMessage) -> bool:
        """The primary registry's dispatch_message: copy an owner's message
        (or a rename of a channel the shadow holds) into the shadow, then
        hand it to the core."""
        if msg.subtype == "channel_rename":
            if msg.new_name and self._serves(msg.channel):
                self._mirror_channel(msg.channel, name=msg.new_name)
        elif self._is_owner_input(msg):
            self._mirror_owner(msg)
        return self._dispatch_message(msg)

    def _mirror_owner(self, msg: InboundMessage) -> None:
        ref = MessageRef(msg.channel, msg.msg_id, msg.thread_key or msg.msg_id, msg.posted_at)
        files = list(msg.files or [])
        self._mirror("owner message", msg.channel,
                     lambda: self._mirror_rows(ref, msg.text or "", files, "owner"))

    def _primary_event(self, name: str, payload: Any) -> bool:
        """The primary registry's dispatch_event: a whole thread move re-keys
        the thread's rows in the shadow, then the core gets the event."""
        if name == "thread_moved" and isinstance(payload, dict):
            self._mirror_move(payload)
        return self._dispatch_event(name, payload)

    def _mirror_move(self, payload: dict) -> None:
        old, new = str(payload.get("old") or ""), str(payload.get("new") or "")
        channel = str(payload.get("channel") or "")
        old_channel = str(payload.get("old_channel") or "") or channel
        move = getattr(self.shadow, "mirror_move", None)
        if not (old and new and channel) or old == new or not callable(move):
            return
        if not self._serves(channel):
            # A card filed out of the owner's view, for one: the shadow has
            # no such place, and the rows stay where the owner saw them.
            log.info("shadow: thread %s moved to %s, which the shadow does not hold; "
                     "its rows stay", old, channel)
            return
        self._mirror("thread move", channel, lambda: move(old_channel, old, channel, new))

    def _primary_outbound(self, verb: str, **args: Any) -> None:
        """The primary's on_outbound: a write the primary made on its own
        (the verbs are in the module docstring), copied into the shadow."""
        sh = self.shadow
        what = f"the primary's {verb}"
        if verb in ("post", "update", "card_status"):
            ref: MessageRef = args["ref"]
            text = str(args.get("text") or "")
            mirror = {"post": lambda: sh.mirror_post(ref, text, "bot"),
                      "update": lambda: sh.mirror_update(ref, text),
                      "card_status": lambda: sh.mirror_card_status(ref, text)}[verb]
            self._mirror(what, ref.channel, mirror)
        elif verb == "card":
            ref, card = args["ref"], args["card"]
            self._mirror(what, ref.channel, lambda: sh.mirror_card(ref, card))
        elif verb == "reaction":
            channel, msg_id = str(args["channel"]), str(args["msg_id"])
            name, on = _status_name(args["status"]), bool(args.get("on", True))
            self._mirror("the primary's reaction", channel,
                         lambda: sh.mirror_reaction(channel, msg_id, name, on))
        elif verb == "owner_message":
            msg = args["msg"]
            if isinstance(msg, InboundMessage) and self._is_owner_input(msg):
                self._mirror_owner(msg)
        elif verb == "channel":
            channel = str(args.get("channel") or "")
            if channel and self._serves(channel):
                self._mirror_channel(channel, name=args.get("name"),
                                     archived=args.get("archived"),
                                     description=args.get("description"))
        else:
            log.debug("shadow: no mirror for the primary's %s", verb)

    # inbound from the shadow

    def _from_shadow(self, kind: Any, **payload: Any) -> Any:
        """The shadow's `inbound` hook; see the module docstring."""
        if isinstance(kind, ActionEvent):
            return self._web_action(kind)
        if isinstance(kind, InboundMessage):
            msg = kind
            path = str((msg.raw or {}).get("path") or "")
            files: list = []
            if path:
                name = msg.files[0].name if msg.files else ""
                files.append({"path": path, "name": name or Path(path).name})
            return self._web_message(msg.channel, msg.text or "", msg.thread_key or None, files)
        if kind == "message":
            return self._web_message(str(payload.get("channel") or ""),
                                     str(payload.get("text") or ""),
                                     payload.get("thread_key") or None,
                                     list(payload.get("files") or []))
        if kind == "action":
            ev = payload.get("event")
            if not isinstance(ev, ActionEvent):
                ev = next((v for v in payload.values() if isinstance(v, ActionEvent)), None)
            if ev is None:
                raise ValueError("an inbound action carries no ActionEvent")
            return self._web_action(ev)
        raise ValueError(f"unknown inbound kind {kind!r}")

    def _local_file(self, item: Any) -> tuple[str, str, FileRef | None, str | None]:
        """(local path, file name, the shadow's FileRef, a temporary directory
        to remove) for one file of a web message."""
        if isinstance(item, (str, Path)):
            return str(item), Path(item).name, None, None
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            if not path:
                raise ChatError("a web file names no local path")
            own = item.get("file") if isinstance(item.get("file"), FileRef) else None
            name = str(item.get("name") or (own.name if own else "") or Path(path).name)
            return path, name, own, None
        if isinstance(item, FileRef):
            tmp = tempfile.mkdtemp(prefix="chat-shadow-")
            got = None
            try:
                got = self.shadow.download(item, tmp)
            finally:
                if not got:
                    shutil.rmtree(tmp, ignore_errors=True)
            if not got:
                raise ChatError(f"the web file {item.name!r} could not be read")
            return str(got), item.name or Path(got).name, item, tmp
        raise ChatError(f"a web file is a path, a dict or a FileRef, not {type(item).__name__}")

    def _web_message(self, channel: str, text: str, thread_key: str | None,
                     files: list) -> MessageRef:
        """Post the owner's web message into the primary as the bot, copy it
        into the shadow under the primary id, then hand it to the core."""
        if not channel:
            raise ChatError("a web message names no channel")
        p = self.primary
        local: list[tuple[str, str, FileRef | None, str | None]] = []
        try:
            for item in files:
                local.append(self._local_file(item))
            shown = text.strip() or ", ".join(name for _, name, _, _ in local)
            if not shown:
                raise ChatError("an empty web message")
            ref = p.post(channel, _to_mrkdwn(WEB_PREFIX + shown), thread_ts=thread_key)
            sent: list[FileRef] = []
            kept: list[FileRef] = []
            for path, name, own, _ in local:
                up = p.upload(ref.channel, path, thread_ts=ref.thread_key, filename=name)
                sent.append(up)
                kept.append(own or up)
            # The bytes of a file the shadow already holds stay where they are.
            # The shadow answers the browser with this row, and the bytes'
            # path lasts only for this call: the write never waits.
            paths = [path if own is None else "" for path, _, own, _ in local]
            self._mirror("web message", ref.channel,
                         lambda: self._mirror_rows(ref, text, kept, "owner", paths),
                         hold=False)
        finally:
            for _, _, _, tmp in local:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
        msg = InboundMessage(
            channel=ref.channel, msg_id=ref.id, text=text, user_id=str(p.owner_id),
            posted_at=ref.posted_at or time.time(), thread_key=ref.thread_key,
            is_reply=bool(thread_key), is_dm=self._is_dm(ref.channel),
            subtype="file_share" if sent else "", files=sent, raw={"source": "web"})
        try:
            self._dispatch_message(msg)
        except Exception:
            log.exception("shadow: the core failed on a web message in %s", ref.channel)
        return ref

    def _web_action(self, ev: ActionEvent) -> bool:
        """Hand the owner's web click to the core as the primary's owner."""
        value = ev.value
        registry = self.card_registry
        if registry is not None and ev.source != "select":
            try:
                row = registry.by_ref(ev.card.channel, ev.card.id) or {}
            except Exception:
                row = {}
            action = (row.get("actions") or {}).get(ev.action_id)
            if isinstance(action, dict):
                value = action.get("value", "")
        out = dataclasses.replace(ev, user_id=str(self.primary.owner_id), value=value,
                                  trigger_id="", raw={**(ev.raw or {}), "source": "web"})
        try:
            return self.registry.dispatch_action(out)
        except Exception:
            log.exception("shadow: the core failed on a web click on %s", ev.card.id)
            return False
