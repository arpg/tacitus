"""Action requests as blocks (docs/webchat.md, Action requests): what one
request in the DM shows folded, read from the card the bot posted.

A request is a card (or the status line that replaced it) in the DM, at
the top or in a thread (the Replies card's rows), or a thread there that
opens with a plain message (a notice, a device to approve, or something
the owner typed before the place took button presses only). block()
gives the fields a folded block needs from the card's own structure: its
kind as plain words (`action`), who it concerns (`who`), its subject or
event (`what`) and the event's time (`when`), each from the card's
labelled fields, never from its body or its text. A card with none of
them falls back to its title.

urgency() is the one cue the block is coloured by: `overdue`, `soon` or
`calm`, with `why` (what decided it: `due`, `starts`, `expires` or
`waited`) and `at` (the moment it names), which the app puts in words. The
inputs are the request's linked deadline and its event's start (the
host's request_context, from the ledger and the staged event), the card's
age against the seven days its buttons last, and how long it has waited.
"Soon" for a deadline or a start counts working days (soon_until): Monday
to Friday, the week the scheduler's earliest slot and the booking links
count, since the install keeps no list of working days outside its
calendar's working-hours events.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from . import card_text
from . import thread_state as _ts

# A card kind as the owner reads it, folded.
ACTIONS = {
    "send": "Send email", "bulk": "Send emails", "schedule": "Send later",
    "event": "Create meeting", "event_update": "Move meeting", "event_delete": "Delete meeting",
    "publish": "Publish", "close": "Close item", "closed": "Closed for you",
    "replies": "Reply needed", "room": "Pick a room", "invite": "Answer an invite",
    "letter_ask": "Letter request", "letter": "Letter to write",
}
# The labelled fields each part reads, first found wins.
WHO = ("To", "Attendees", "Organizer", "From")
WHAT = ("Subject", "Event")
WHEN = ("New time", "When")
# A waiting request turns `soon` when its deadline or its event comes by the
# end of the SOON_WORKDAYS-th working day after today (WORKDAYS: Monday is
# 0), or after it has waited WAITED_S (calendar days, as the Replies card's
# first rung counts); `overdue` once the deadline or the event has passed,
# or a day before its buttons stop acting.
SOON_WORKDAYS = 2
WORKDAYS = frozenset(range(5))
WAITED_S = 3 * 86400
EXPIRES_S = 86400
LEVELS = ("overdue", "soon", "calm")
_ADDRESSES_SHOWN = 2
# A link in a status line: Markdown's [text](url), or the core's <url|text>,
# which a status line keeps as written.
_LINK_RE = re.compile(r"\]\((\S+?)\)|<([^|>\s]+)\|[^>]*>")
_MULTI_RE = re.compile(r"\s*[,;]\s*")


def _fields(card: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in card.get("fields") or []:
        if isinstance(f, dict) and f.get("label") and f.get("label") not in out:
            out[str(f["label"])] = card_text.one_line(
                card_text.strip_inert(str(f.get("value") or "")), 300)
    return out


def _first(fields: dict[str, str], labels: tuple[str, ...]) -> tuple[str, str]:
    for label in labels:
        if fields.get(label):
            return label, fields[label]
    return "", ""


def _people(value: str) -> str:
    """At most two names or addresses, then how many more."""
    parts = [p for p in _MULTI_RE.split(value) if p]
    if len(parts) <= _ADDRESSES_SHOWN + 1:
        return ", ".join(parts)
    return ", ".join(parts[:_ADDRESSES_SHOWN]) + f" and {len(parts) - _ADDRESSES_SHOWN} more"


def block(msg: dict) -> dict:
    """The folded block's words for one request (a stored message as the
    store's JSON gives it): {"action", "who", "what", "when"}, "" where the
    card has no such field. `what` falls back to the card's title (or a
    message's first line) when the card names no subject."""
    card = msg.get("card") if isinstance(msg.get("card"), dict) else {}
    title = _ts.plain(card_text.strip_inert(card.get("title") or msg.get("text") or ""), 120)
    if not card:
        file = msg.get("file") if isinstance(msg.get("file"), dict) else {}
        if msg.get("author") == "owner":
            return {"action": "Your message", "who": "", "what": title, "when": ""}
        if file:
            return {"action": "A file for you", "who": "", "what": title or card_text.one_line(
                str(file.get("name") or ""), 120), "when": ""}
        device = _ts.device_code(msg.get("text"))
        return {"action": "Approve a device" if device else "Notice", "who": "", "what": title,
                "when": ""}
    problem = card.get("problem") if isinstance(card.get("problem"), dict) else {}
    if problem.get("text"):
        who = card_text.one_line(str(problem.get("from") or ""), 80)
        return {"action": "Not written", "who": f"From {who}" if who else "",
                "what": card_text.one_line(str(problem["text"]), 300), "when": ""}
    question = card.get("question") if isinstance(card.get("question"), dict) else {}
    if question.get("text"):
        who = card_text.one_line(str(question.get("from") or ""), 80)
        return {"action": "Answer needed", "who": f"From {who}" if who else "",
                "what": card_text.one_line(str(question["text"]), 300), "when": ""}
    kind = str(card.get("kind") or "")
    fields = _fields(card)
    label, who = _first(fields, WHO)
    _l, what = _first(fields, WHAT)
    _w, when = _first(fields, WHEN)
    action = ACTIONS.get(kind) or title or "Request"
    if kind == "close" and len(card.get("actions") or []) > 2:
        action = "Close items"
    return {"action": action,
            "who": f"{label} {_people(who)}".strip() if who else "",
            "what": what or (title if title != action else ""),
            "when": when}


def _epoch(value: Any, *, end_of_day: bool) -> float | None:
    """A moment from the host: seconds, an ISO time, or a date (the end of
    that local day when `end_of_day`, a deadline's last moment)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d")
            return (day.replace(hour=23, minute=59, second=59) if end_of_day else day).timestamp()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def soon_until(now: float, workdays: int = SOON_WORKDAYS) -> float:
    """The last moment that is still soon: the end of the `workdays`-th
    working day after today, local time. On a Friday that is the end of
    Tuesday; on a Wednesday, the end of Friday. It is never less than two
    calendar days ahead, since two working days span at least two days."""
    day: date = datetime.fromtimestamp(now).date()
    left = workdays
    while left:
        day += timedelta(days=1)
        if day.weekday() in WORKDAYS:
            left -= 1
    return datetime.combine(day, dtime(23, 59, 59)).timestamp()


def urgency(msg: dict, waiting: bool, ctx: dict | None = None,
            now: float | None = None) -> dict:
    """{"urgency", "why", "at"} for one request. Only a waiting request is
    anything but calm. Overdue: its linked deadline passed, its event's
    start passed, or its buttons stop acting within a day. Soon: a deadline
    or a start within two days, or three days of waiting. The first that
    holds, in that order, names `why` and `at`. "Within two days" is two
    working days (soon_until)."""
    now = time.time() if now is None else float(now)
    soon = soon_until(now)
    if not waiting:
        return {"urgency": "calm", "why": "", "at": None}
    ctx = ctx or {}
    due = _epoch(ctx.get("due"), end_of_day=True)
    starts = _epoch(ctx.get("starts"), end_of_day=False)
    posted = float(msg.get("posted_at") or now)
    expires = posted + _ts.CARD_LIVE_S
    for level, why, at, test in (
            ("overdue", "due", due, due is not None and due < now),
            ("overdue", "starts", starts, starts is not None and starts < now),
            ("overdue", "expires", expires, msg.get("kind") == "card" and expires - now < EXPIRES_S),
            ("soon", "due", due, due is not None and due <= soon),
            ("soon", "starts", starts, starts is not None and starts <= soon),
            ("soon", "waited", posted, now - posted >= WAITED_S)):
        if test:
            return {"urgency": level, "why": why, "at": at}
    return {"urgency": "calm", "why": "", "at": None}


def order(items: list[dict]) -> list[dict]:
    """Waiting requests as the list shows them: the ones Tacitus is acting
    on (`working`) first, then overdue, soon and calm, each newest first."""
    return sorted(items, key=lambda j: (not j.get("working"),
                                        LEVELS.index(j.get("urgency") or "calm"),
                                        -float(j.get("posted_at") or 0)))


LINES_CAP = 10


def lines_of(item: dict, thread: list, tags: dict) -> list[dict]:
    """The bot's lines that belong to one request, from its thread's rows
    (oldest first) and the store's tags: a tagged request takes the later
    lines with its own tag; an untagged one that opens a thread holding no
    other card or status line takes the later untagged lines; any other
    takes none, so a thread several requests share shows each only its
    own. Each {"id", "text", "posted_at"}, the newest LINES_CAP."""
    def row(r, k):
        return r[k] if not isinstance(r, dict) else r.get(k)
    mine = tags.get(item.get("id"))
    seq = int(item.get("seq") or 0)
    cards = [r for r in thread if row(r, "kind") in ("card", "status")]
    opens = bool(thread) and row(thread[0], "id") == item.get("id")
    if not mine and not (opens and len(cards) <= 1):
        return []
    out = [{"id": row(r, "id"), "text": row(r, "text") or "", "posted_at": row(r, "posted_at")}
           for r in thread
           if int(row(r, "seq") or 0) > seq and row(r, "author") != "owner"
           and row(r, "kind") == "message" and not row(r, "deleted_at") and row(r, "text")
           and tags.get(row(r, "id")) == mine]
    return out[-LINES_CAP:]


def working(msg: dict, now: float) -> bool:
    """A card whose status line says Tacitus is acting on the press (an
    hourglass: "Writing the reply", "Sending...") within the window a turn
    may take: the list keeps it at the top, in progress."""
    return msg.get("kind") == "status" and _ts.outcome(msg.get("text")) == "busy" \
        and now - float(msg.get("updated_at") or msg.get("posted_at") or 0) < _ts.WORKING_WINDOW_S


def next_request(msg: dict, channel: str) -> str:
    """The id of the request a status line links to in the same place (the
    draft's Send request, Tacitus's one more question), or ""."""
    if msg.get("kind") != "status":
        return ""
    for md, core in _LINK_RE.findall(str(msg.get("text") or "")):
        got = _ts.link_target(md or core)
        if got and got[0] == channel:
            return got[1]
    return ""
