"""Which of the bot's posts in the web chat notify the owner's devices: the
push policy in one place (docs/webchat.md, Notifications (Web Push)).

A new bot message pushes when

  (a) it is in the DM. A direct message (`chat.direct.send_direct`, whose
      thread carries the notifier key `personal`) goes at high urgency, the
      urgency of the alert that sends it; the rest at normal.
  (b) it opens a morning briefing or an afternoon update in the EA channel:
      a thread a notifier opened (its origin is recorded) that the owner
      never wrote in, and the message's first lines carry the report's
      title. The push is "Your morning briefing is ready" or "Afternoon
      update" and nothing of the report.
  (c) it answers a conversation the owner wrote in, in any other channel
      (rooms too): the owner's newest message there is newer than the bot's
      previous answer (a message, a file or a card). A reply in parts
      pushes once, and a reply after a card in the same conversation adds
      no push to the card's own.

A new card always pushes (WebFrontend._push_card). Nothing else does: a
digest, a notice the owner never wrote in, the "Still need this?" nudge,
a progress line (":hourglass_flowing_sand: Still working..."), a pointer
to a card in the DM (the card's own push covers it), an edit, a reaction,
the owner's own message.

The `notify_push` block in the web block (Settings) turns (b), (c) outside
rooms and (c) in rooms off, and says whether (b) and (c) wait for the end
of the owner's quiet hours ("queue", the default) or go at once ("send").
(a) and the cards always go at once.

The front end applies the decision: the push's title and body, the
thread's tag, the queue during quiet hours, and the check that drops a
second push of the same text within DEDUPE_S seconds (`fingerprint`).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from . import thread_state as _ts
from .web_push import plain_line

BOT = "bot"
# The notifier key of a direct message (chat_notify.web_direct).
DIRECT_ORIGIN = "personal"
# Seconds within which a second push of the same text is dropped.
DEDUPE_S = 10.0
# Lines of a message read for a report's title: a held post's line goes
# first ("(held 04:00)"), then the report's own.
TITLE_LINES = 3
# The reports whose opening pushes, and the title each push carries.
REPORTS = (
    (re.compile(r"^\W*morning briefing\b", re.I), "Your morning briefing is ready"),
    (re.compile(r"^\W*afternoon update\b", re.I), "Afternoon update"),
)
# A progress line: the bot's own name for "at work", or Zulip's for it.
_PROGRESS_RE = re.compile(r"^\s*:(?:hourglass(?:_flowing_sand)?|time_ticking):")
# Rows of the bot that count as its answer in a conversation.
_ANSWER_KINDS = frozenset({"message", "file", "card", "status"})
_FALSE = frozenset({"false", "0", "no", "off", ""})
QUIET_MODES = ("queue", "send")


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE


@dataclass(frozen=True)
class Settings:
    """The owner's `notify_push` block: briefings, replies (outside rooms),
    rooms, and quiet ("queue" or "send")."""
    briefings: bool = True
    replies: bool = True
    rooms: bool = True
    quiet: str = "queue"

    @classmethod
    def from_config(cls, block: Any) -> Settings:
        """The settings a `notify_push` block names; a key it lacks, or a
        block that is not a mapping, keeps the default."""
        b = block if isinstance(block, dict) else {}
        quiet = str(b.get("quiet") or "queue").strip().lower()
        return cls(briefings=_flag(b.get("briefings"), True),
                   replies=_flag(b.get("replies"), True),
                   rooms=_flag(b.get("rooms"), True),
                   quiet=quiet if quiet in QUIET_MODES else "queue")


@dataclass(frozen=True)
class Push:
    """One push: the clause that chose it ("dm", "briefing" or "reply"),
    what it shows, and its urgency."""
    clause: str
    title: str
    body: str = ""
    urgency: str = "normal"

    @property
    def may_wait(self) -> bool:
        """True when the owner's quiet hours may hold it."""
        return self.clause in ("briefing", "reply")


def is_progress(text: str | None) -> bool:
    return bool(_PROGRESS_RE.match(str(text or "")))


def is_pointer(text: str | None) -> bool:
    """A bot line that points at a card in the DM ("... is in your DM: [open
    it](...)")."""
    return bool(_ts.POINTER_RE.search(str(text or "")))


def report_title(text: str | None) -> str:
    """The push title of a briefing or an update whose title opens `text`
    (in its first TITLE_LINES lines), else ""."""
    lines = [plain_line(line, 200) for line in str(text or "").splitlines()]
    for line in [ln for ln in lines if ln][:TITLE_LINES]:
        for pattern, title in REPORTS:
            if pattern.search(line):
                return title
    return ""


def _answers(row: dict, nudges: set) -> bool:
    """True when a bot row is an answer: a message, a file or a card; not a
    progress line, a pointer to a card, or a nudge."""
    if row.get("kind") not in _ANSWER_KINDS or row.get("id") in nudges:
        return False
    if row.get("kind") == "message":
        text = row.get("text") or ""
        return not (is_progress(text) or is_pointer(text))
    return True


def message_push(store: Any, row: dict | None, settings: Settings) -> Push | None:
    """The push for a new row of the store, or None. `store` is the
    WebStore that holds the row."""
    if not row or row.get("author") != BOT or row.get("kind") != "message":
        return None
    text = row.get("text") or ""
    if is_progress(text) or is_pointer(text):
        return None
    channel, key = row.get("channel") or "", row.get("thread_key") or ""
    ch = store.channel(channel)
    kind = ch["kind"] if ch else ""
    if kind in ("", "digest"):
        return None
    nudges = store.nudge_ids(channel, key)
    if row.get("id") in nudges:
        return None
    if kind == "dm":
        loud = store.thread_origin(channel, key) == DIRECT_ORIGIN
        return Push("dm", "Tacitus", text, "high" if loud else "normal")
    owner_seq, since = store.owner_turn(channel, key, int(row.get("seq") or 0))
    if owner_seq is None:
        title = report_title(text) if kind == "main" and settings.briefings else ""
        if title and store.thread_origin(channel, key):
            return Push("briefing", title)
        return None
    if not (settings.rooms if kind == "room" else settings.replies):
        return None
    if any(_answers(r, nudges) for r in since):
        return None
    name = (ch.get("name") or "").strip() if kind == "room" else ""
    return Push("reply", f"Tacitus replied in {name}" if name else "Tacitus replied", text)


def fingerprint(title: str, body: str = "") -> str:
    """The key under which a push's text counts as sent: the line the
    notification shows, in lower case, letters and digits only; "" for
    an empty one."""
    line = plain_line(body, 2000) or plain_line(title, 2000)
    norm = re.sub(r"\W+", "", line.lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32] if norm else ""
