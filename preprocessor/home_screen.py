"""The web chat's Home: what Tacitus did today against what is left for the
owner, the next gap in the calendar, what changed since the owner last
checked in, and one line that greets them (docs/webchat.md, Home).

The app opens on Home. Every number and every list on it is built here,
in code, from the state the system already keeps; a model writes the one
greeting line only, and Home never waits for it.

The sources (read-only):

  briefing   the last briefing's data (briefing-latest.json, the
             resident's space, read with its access): the ranked
             `today_top`, today's and tomorrow's events, the first-week
             question, and when it ran
  requests   Action requests as the web front end lists them
             (WebFrontend.requests: the waiting cards, the answered ones)
  ledger     the open rows (the replies owed now, ledger.reply_backlog on
             the live rows, as `ledger.py reply-queue` reads them) and the
             rows closed since yesterday, read-only
  travel     travel mode (quiet_hours), the all-day blocks of today's
             calendar (vacation, out of office, a trip or a conference),
             and the time zone of the device that asks
  light      the status light's level
  store      the web chat's own store: the check-ins, the briefing text the
             briefing lane handed to Home, the greeting cache

The greeting: the owner's own model writes it when one is set up
(remote_llm, a tool-less call on the `chat-home` purpose, the bot's small
jobs' breaker) from the numbers alone, never from mail; a line with a
number the facts do not hold, or longer than a line, is dropped. Until it
answers, and with no model, a line from the templates here shows. Facts
only (the tone), a red light or anything overdue gives the plain line.

The briefing post: on the web chat, with Home on (the web block's
`home.briefing`, "home" by default), the morning briefing and the
afternoon update are handed to Home instead of a post
(chat_notify.post(..., home=<agent>)): the text is kept in the store for
Home's "This morning's briefing", and one short push opens Home. With
Home off, on Slack, or while Home's tick has not run in five minutes,
they post as before.

An item opened in place: each item Home lists carries its `ref`, and
home_items.py builds the quick briefing and the choices for it and runs a
choice (GET /api/home/item, POST /api/home/act, POST /api/home/undo; Home's
item, act and undo). A Done, No reply needed, Waiting or Later takes the item
off Home at once and is written when its undo window ends (`hidden`, the
tick).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("chat.web.home")

# How the greeting sounds (Settings, Home): breezy and dry use the owner's
# own model where one is set up; facts is always the plain line.
TONES = ("breezy", "dry", "facts")
DEFAULT_TONE = "breezy"
# What happens to the briefing on the web chat: Home shows it ("home") or
# it posts in Ask Tacitus as before ("post").
BRIEFING_PLACES = ("home", "post")
DEFAULT_BRIEFING = "home"
# The agents whose report Home carries, and how Home names each.
REPORTS = {"morning-briefing": "This morning's briefing", "afternoon-catchup": "This afternoon's update"}
REPORT_PUSH = {"morning-briefing": "Your morning is ready.",
               "afternoon-catchup": "Your afternoon update is ready."}
REPORT_MAX = 40_000
# A payload newer than this is served again as it is.
FRESH_S = 20.0
# A check-in this long after the last one starts a new visit: "since you
# last checked in" counts from the visit before.
VISIT_GAP_S = 30 * 60
# The greeting: the longest line kept, the model's wait, and how often the
# model is asked at most.
GREETING_MAX = 120
GREETING_TIMEOUT_S = 25
GREETING_EVERY_S = 300
PURPOSE = "chat-home"
# Rough minutes per item, for "that is enough for ...": a tap, a reply, a
# task from the briefing's list.
MINUTES = {"request": 2, "reply": 10, "item": 20, "question": 3}
# A gap this long is said as "nothing until", not as a count of minutes.
LONG_GAP_MIN = 180
# Meetings with less than this between them are one busy stretch: the
# next gap is the free time after it, never "0 minutes before your 12:00".
SHORT_GAP_MIN = 10
# A reply counts on Home once it has waited this many days: the Replies
# ladder's first rung (ledger.REPLY_DRAFT_DAYS), so mail that came today
# does not swell what is left.
REPLY_MIN_DAYS = 1
# The most items each list carries.
LEFT_MAX = 30
DONE_MAX = 40
SINCE_MAX = 6
# All-day blocks: words that make a day a vacation or a trip
# (scheduling_engine's hard all-day blocks, split by what they mean here).
VACATION_WORDS = ("vacation", "holiday", "pto", "out of office", "ooo", "time off", "day off", "leave")
TRAVEL_WORDS = ("travel", "trip", "flight", "conference", "retreat", "away", "offsite", "workshop")
# Closures that are the owner's own doing; anything else Tacitus did.
OWNER_SIGNALS = frozenset({
    "user_stated", "user_confirmed", "user_dashboard", "user_handled", "user_sent_myself",
    "user_feedback", "user_archived", "user_replied_outlook", "user_replied", "linked_send",
    "send_reply_row", "meeting_response_sent"})
# A request card kind that is a draft Tacitus made.
DRAFT_KINDS = frozenset({"send", "bulk", "schedule"})
_NUM_RE = re.compile(r"\d+")
_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")
_MARK_RE = re.compile(r"[*_`~]+")


# ── small helpers ────────────────────────────────────────────────────

def _plain(text: Any, limit: int = 120) -> str:
    """A record's text on one line, without emoji shortcodes or marks, cut on
    a word."""
    t = _MARK_RE.sub("", _SHORTCODE_RE.sub("", str(text or "")))
    t = " ".join(t.split()).strip(" -:")
    if len(t) <= limit:
        return t
    cut = t[:limit - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def _n(count: int, one: str, many: str = "") -> str:
    """"1 reply", "3 replies"."""
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _ts(value: Any) -> float | None:
    """A Unix time from a number or an ISO text ("Z", an offset, or none,
    which reads as UTC); None when there is none."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    raw = str(value or "").strip()
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", raw)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).replace(" ", "T"))
    except ValueError:
        return None
    if m.group(2) and m.group(2) != "Z":
        off = m.group(2) if ":" in m.group(2) else m.group(2)[:3] + ":" + m.group(2)[3:]
        dt = datetime.fromisoformat(dt.isoformat() + off)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _event_time(part: Any) -> float | None:
    """An event's start or end ({"dateTime", "timeZone"}, Graph's shape:
    UTC wall clock with no offset when the zone says UTC)."""
    if not isinstance(part, dict):
        return _ts(part)
    raw = str(part.get("dateTime") or "")
    zone = str(part.get("timeZone") or "UTC")
    if not raw:
        return None
    if re.search(r"(Z|[+-]\d{2}:?\d{2})$", raw) or zone.upper() == "UTC":
        return _ts(raw)
    try:
        dt = datetime.fromisoformat(raw[:19]).replace(tzinfo=ZoneInfo(zone))
    except (ValueError, KeyError, Exception):
        return _ts(raw)
    return dt.timestamp()


def clock(ts: float, zone: ZoneInfo) -> str:
    """"2:00", "9:30": the hour as people say it."""
    dt = datetime.fromtimestamp(ts, zone)
    return f"{(dt.hour % 12) or 12}:{dt.minute:02d}"


def _day_name(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if (d - today).days < 7:
        return d.strftime("%A")
    return f"{d.strftime('%B')} {d.day}"


# ── the sources a payload is built from ─────────────────────────────

@dataclass
class Sources:
    """Everything one Home is built from. `Home.sources()` reads the live
    ones; a test builds one by hand."""
    now: float
    zone: ZoneInfo
    device_zone: str = ""
    briefing: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    replies: list = field(default_factory=list)
    open_ids: set | None = None
    # The open rows by id as the lanes read them (with `owner_said`, the
    # owner's word on the thread); None where they were not read.
    rows: dict | None = None
    closed: list = field(default_factory=list)
    travel: dict = field(default_factory=dict)
    light: str = ""
    reports: list = field(default_factory=list)
    checkin: dict = field(default_factory=dict)
    recent: list = field(default_factory=list)
    tone: str = DEFAULT_TONE
    dm: str = "dm"
    # The refs of the items a choice on Home took off the list while its
    # undo window runs (home_items.Acts): left out of the payload.
    hidden: set = field(default_factory=set)


def _today(src: Sources) -> date:
    return datetime.fromtimestamp(src.now, src.zone).date()


def _local_day(ts: float | None, zone: ZoneInfo) -> date | None:
    return datetime.fromtimestamp(ts, zone).date() if ts else None


# ── the calendar ─────────────────────────────────────────────────────

def events(src: Sources) -> list[dict]:
    """Today's and tomorrow's events from the briefing, on the owner's
    clock: {"title", "start", "end", "all_day", "show", "day"} sorted by
    start. An event the owner declined or that is cancelled is left out.
    The briefing's own split into today and tomorrow is not trusted (it
    may be yesterday's): each event's own start says its day."""
    b = src.briefing or {}
    out, seen = [], set()
    for e in list(b.get("todays_events") or []) + list(b.get("tomorrows_events") or []):
        if not isinstance(e, dict) or e.get("isCancelled"):
            continue
        status = str(((e.get("responseStatus") or {}).get("response")) or "").lower()
        if status == "declined":
            continue
        all_day = bool(e.get("isAllDay"))
        if all_day:
            d0 = str((e.get("start") or {}).get("dateTime") or "")[:10]
            d1 = str((e.get("end") or {}).get("dateTime") or "")[:10]
            try:
                first = date.fromisoformat(d0)
                last = date.fromisoformat(d1) - timedelta(days=1) if d1 else first
            except ValueError:
                continue
            start = datetime.combine(first, datetime.min.time(), src.zone).timestamp()
            end = datetime.combine(max(first, last) + timedelta(days=1), datetime.min.time(),
                                   src.zone).timestamp()
        else:
            start, end = _event_time(e.get("start")), _event_time(e.get("end"))
            if start is None:
                continue
            end = end or start + 1800
        key = (str(e.get("id") or ""), start)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": _plain(e.get("subject"), 80) or "Busy", "start": start, "end": end,
                    "all_day": all_day, "show": str(e.get("showAs") or "").lower(),
                    "day": _local_day(start, src.zone).isoformat()})
    out.sort(key=lambda x: (x["start"], not x["all_day"]))
    return out


def _block_kind(ev: dict) -> str:
    """"vacation", "travel" or "" for an all-day event."""
    if not ev["all_day"]:
        return ""
    words = ev["title"].lower()
    if any(re.search(rf"\b{re.escape(w)}\b", words) for w in TRAVEL_WORDS):
        return "travel"
    if any(re.search(rf"\b{re.escape(w)}\b", words) for w in VACATION_WORDS):
        return "vacation"
    if ev["show"] in ("oof", "outofoffice"):
        return "vacation"
    return ""


def _offset(zone_name: str, now: float) -> float | None:
    try:
        return datetime.fromtimestamp(now, ZoneInfo(zone_name)).utcoffset().total_seconds()
    except Exception:
        return None


def mode(src: Sources, evs: list[dict]) -> dict:
    """Where the owner is today: {"kind": "normal" | "travel" | "vacation",
    "until": "YYYY-MM-DD" or "", "why": "calendar" | "travel mode" |
    "time zone"}. A vacation or out-of-office block on today's calendar
    makes a vacation; a trip or conference block, travel mode
    (quiet_hours), or a device whose clock is in another time zone than
    home makes travel."""
    today = _today(src)
    blocks = [(e, _block_kind(e)) for e in evs if e["all_day"]
              and _local_day(e["start"], src.zone) <= today < _local_day(e["end"] - 1, src.zone) + timedelta(days=1)]
    for want in ("vacation", "travel"):
        hit = [e for e, k in blocks if k == want]
        if hit:
            last = max(_local_day(e["end"] - 1, src.zone) for e in hit)
            return {"kind": want, "until": last.isoformat() if last > today else "", "why": "calendar"}
    tm = src.travel or {}
    if tm.get("enabled") and tm.get("timezone"):
        until = str(tm.get("until") or "")
        try:
            live = not until or date.fromisoformat(until) >= today
        except ValueError:
            live = True
        if live:
            return {"kind": "travel", "until": until if until and until > today.isoformat() else "",
                    "why": "travel mode"}
    if src.device_zone:
        here, home = _offset(src.device_zone, src.now), src.zone.utcoffset(datetime.fromtimestamp(src.now, src.zone))
        if here is not None and home is not None and here != home.total_seconds():
            return {"kind": "travel", "until": "", "why": "time zone"}
    return {"kind": "normal", "until": "", "why": ""}


# ── mail kept from hosted models ──────────────────────────────────────
# Home's payload goes to the browser and through the relay, and a
# conversation started from it goes to a hosted model. An item kept below
# Summary (keep_gate) is shown there in code's fixed words alone
# (keep_gate.kept_line, as the post shows it): at `local` its date and an
# Outlook link where the post had one, at `facts` also its sender and
# subject as the headers give them. Nothing else of it is in the payload:
# no line, no name, no project, no words to ask with. Every item Home
# lists carries its `keep_level` (mark_keep, from the live sources); when
# the keep check cannot run, every item from mail reads `local`.

def _keep():
    try:
        import keep_gate
    except ModuleNotFoundError:  # imported as preprocessor.home_screen
        from preprocessor import keep_gate
    return keep_gate


def is_kept(level) -> bool:
    return level in ("local", "facts")


def kept_title(level: str, *, day: str = "", who: str = "", subject: str = "", draft: bool = False) -> str:
    """A kept item's whole line (keep_gate.kept_line); the date-only line
    when it cannot be made."""
    try:
        return _keep().kept_line(level, day=day, who=who, subject=subject, draft=draft)
    except Exception:
        return "A kept message needs you. Open it in Outlook."


def kept_name() -> str:
    """A kept item where a list names it in a few words (So far today)."""
    try:
        return _keep().KEPT_NAME
    except Exception:
        return "A kept message"


_LINK_MARK = re.compile(r"<(https?://[^|>\s]+)\|([^>]*)>")


def _unlink(text: str) -> tuple[str, str]:
    """A post line's "<url|words>" as its words, and the url."""
    m = _LINK_MARK.search(text or "")
    if not m:
        return text or "", ""
    return text[:m.start()] + m.group(2) + text[m.end():], m.group(1)


def _mail_day(value, zone: ZoneInfo) -> str:
    """"Nov 17" on the owner's clock for a mail time; '' with none."""
    ts = _ts(value)
    if ts is None:
        return ""
    d = datetime.fromtimestamp(ts, zone)
    return f"{d.strftime('%b')} {d.day}"


def _from_mail(item: dict) -> bool:
    brief = item.get("thread_brief") if isinstance(item.get("thread_brief"), dict) else {}
    return bool(item.get("conversation_id") or item.get("source_message_id") or item.get("message_id")
                or brief.get("conversation_id"))


def mark_keep(src: Sources, levels=None) -> None:
    """Write `keep_level` on every item Home may list from mail: the replies
    owed, the open rows, the rows closed, each request (by the rows and the
    thread its card names) and each item of the post's own list. One shared
    check (keep_gate.levels, or `levels`, a stand-in); when it cannot run,
    every item from mail reads `local`. A level already written (the post's)
    stays when it is stricter."""
    rows = src.rows or {}
    pairs: list[tuple[dict, dict]] = []
    for r in src.replies or []:
        if isinstance(r, dict):
            pairs.append((r, r))
    for row in rows.values():
        pairs.append((row, row))
    for row in src.closed or []:
        if isinstance(row, dict):
            pairs.append((row, row))
    for r in list((src.requests or {}).get("waiting") or []) + list((src.requests or {}).get("done") or []):
        if not isinstance(r, dict):
            continue
        ids, convs = _card_keys(r.get("card"))
        row = next((rows[i] for i in ids if i in rows), None)
        check = dict(row or {})
        if convs:
            check["conversation_id"] = convs[0]
        if check:
            pairs.append((r, check))
    for rep_ in src.reports or []:
        for it in ((rep_.get("view") or {}).get("items") or []) if isinstance(rep_.get("view"), dict) else []:
            check = dict(rows.get(it.get("ledger_id")) or {})
            if it.get("conversation_id"):
                check["conversation_id"] = it["conversation_id"]
            if check:
                pairs.append((it, check))
    if not pairs:
        return
    try:
        kg = _keep()
        found = (levels or kg.levels)([c for _t, c in pairs], key=lambda i: str(id(i)))
        for target, check in pairs:
            level = found.get(str(id(check)), "local")
            mine = target.get("keep_level") or (target.get("kept") if is_kept(target.get("kept")) else "")
            target["keep_level"] = kg.stricter(level, mine) if mine else level
    except Exception as e:
        log.warning("home: the keep check could not run; mail items read local: %s", e)
        for target, check in pairs:
            if _from_mail(check):
                target["keep_level"] = "local"


# ── what is left ─────────────────────────────────────────────────────

def _card_keys(card: Any) -> tuple[list[str], list[str]]:
    try:
        import web_requests as _wr
    except ModuleNotFoundError:  # imported as preprocessor.home_screen
        from preprocessor import web_requests as _wr
    try:
        return _wr._keys(card)
    except Exception:
        return [], []


def _request_href(r: dict, dm: str) -> str:
    from urllib.parse import quote
    return f"#/{quote(str(r.get('channel') or dm), safe='')}/{quote(str(r.get('thread_key') or ''), safe='')}" \
           f"/{quote(str(r.get('id') or ''), safe='')}"


def _fire(urgency: str, due: float | None, now: float) -> bool:
    """Cannot wait a day: overdue, or due within the next day."""
    return urgency == "overdue" or (due is not None and due - now < 86400)


# What the post's sections are on Home: the label and whether its line is a
# question for the owner (its line after the item's name is the question).
VIEW_SECTIONS = {"top": ("To do", False), "due": ("Due", False), "flex": ("Can wait", False),
                 "needs": ("Answer needed", True), "since": ("New today", False),
                 "pending": ("Waiting on you", True)}
def _no_reply(row: dict | None) -> bool:
    """The owner said no reply goes out on this thread (owner_said, the
    draft gate's record): it is not his to do, and code asks him once."""
    said = (row or {}).get("owner_said")
    if not said:
        return False
    try:
        import draft_gate
    except ModuleNotFoundError:  # imported as preprocessor.home_screen
        from preprocessor import draft_gate
    try:
        return bool(draft_gate.no_reply_said(said))
    except Exception:
        return False


def _off_list(row: dict | None, today: date) -> bool:
    """Others owe it and nothing is asked of him before their date
    (ledger.waiting_off_list): off the do-list, as the post has it."""
    if not row:
        return False
    try:
        import ledger
    except ModuleNotFoundError:
        from preprocessor import ledger
    try:
        return bool(ledger.waiting_off_list(row, today))
    except Exception:
        return False


def views(src: Sources) -> list[dict]:
    """Today's lists the briefing runs kept for Home (the post's own items
    after the pre-send review), newest first; [] when no run kept one today
    (the Claude lane, the review off, a run from before)."""
    today = _today(src)
    out = [r for r in src.reports or [] if isinstance(r.get("view"), dict)
           and _local_day(float(r.get("at") or 0), src.zone) == today]
    return [r["view"] for r in sorted(out, key=lambda r: -float(r.get("at") or 0))]


def _due_at(day: str, zone: ZoneInfo) -> float | None:
    try:
        return datetime.combine(date.fromisoformat(day[:10]), datetime.max.time().replace(microsecond=0),
                                zone).timestamp()
    except (TypeError, ValueError):
        return None


def _urgency(due: float | None, now: float) -> str:
    return "overdue" if due and due < now else ("soon" if due and due - now < 2 * 86400 else "calm")


def left(src: Sources) -> list[dict]:
    """What waits for the owner, most urgent first, as the post has it.

    - The Action requests that wait for a tap (a decision the draft gate
      or the review made is one, with its answer buttons there).
    - The briefing's items: the post's own sections after its pre-send
      review where the run kept them for Home (views): what the review
      dropped, held or the audit set aside is never here, a held reply is
      its question, never a draft, and a rewritten line shows as rewritten.
      With no view, the briefing's ranking with the rules the post applies
      upstream: no row others owe before their date, no row merged into
      another, no thread the owner said gets no reply.
    - The replies owed a day or more (REPLY_MIN_DAYS), less those threads:
      a reply an earlier run turned into a decision is that question; one
      it found needs none, or the owner said gets none, is not here; a kept
      thread shows its subject and the post's fixed words only.

    Each is {"id", "kind", "label", "title", "detail", "urgency", "due",
    "fire", "mins", "href"?}: a request opens its block in Action requests;
    anything else opens in place with its choices, its Ask among them
    (home_items). A row or thread is listed once."""
    now, today = src.now, _today(src)
    rows = src.rows or {}
    out: list[dict] = []
    named_ids: set = set()
    named_convs: set = set()

    def taken(lid: str, conv: str) -> bool:
        return bool(lid and lid in named_ids) or bool(conv and conv in named_convs)

    def take(lid: str, conv: str) -> None:
        if lid:
            named_ids.add(lid)
        if conv:
            named_convs.add(conv)
    for r in (src.requests or {}).get("waiting") or []:
        if not isinstance(r, dict) or r.get("working") or not r.get("waiting", True):
            continue
        ids, convs = _card_keys(r.get("card"))
        named_ids.update(ids)
        named_convs.update(convs)
        due = _ts(r.get("at")) if r.get("why") in ("due", "starts", "expires") else None
        urgency = str(r.get("urgency") or "calm")
        base = {"id": f"request:{r.get('id')}", "kind": "request",
                "label": _plain(r.get("action"), 40) or "Request", "urgency": urgency, "due": due,
                "fire": _fire(urgency, due, now), "mins": MINUTES["request"], "href": _request_href(r, src.dm),
                "_rid": str(r.get("id") or ""), "_lid": ids[0] if len(ids) == 1 else "", "_conv": convs[0] if convs else ""}
        if is_kept(r.get("keep_level")):
            card = r.get("card") if isinstance(r.get("card"), dict) else {}
            out.append({**base, "title": kept_title(r["keep_level"], subject=_plain(r.get("what"), 100),
                                                    draft=card.get("kind") in DRAFT_KINDS),
                        "detail": "", "kept": r["keep_level"]})
            continue
        what = _plain(r.get("what") or r.get("text"), 120)
        detail = " · ".join(x for x in (_plain(r.get("who"), 80), _plain(r.get("when"), 60)) if x)
        out.append({**base, "title": what or "A request", "detail": detail,
                    "project": _plain(r.get("project"), 60)})
    kept_views = views(src)
    gone_ids, gone_convs = set(), set()
    for v in kept_views:
        for g in v.get("gone") or []:
            if g.get("kind") == "held":
                continue  # a held reply is under the post's Answer needed as its question
            if g.get("ledger_id"):
                gone_ids.add(g["ledger_id"])
            if g.get("conversation_id"):
                gone_convs.add(g["conversation_id"])
    for v in kept_views:
        for it in v.get("items") or []:
            lid, conv = str(it.get("ledger_id") or ""), str(it.get("conversation_id") or "")
            if taken(lid, conv) or lid in gone_ids or (conv and conv in gone_convs):
                continue
            take(lid, conv)
            label, asks = VIEW_SECTIONS.get(it.get("section"), ("To do", False))
            due = _due_at(str(it.get("due") or ""), src.zone)
            urgency = "calm" if it.get("section") == "flex" else _urgency(due, now)
            level = it.get("keep_level") or (it.get("kept") if is_kept(it.get("kept")) else "")
            if is_kept(level):
                # the post's own fixed line where it made one, else the date-only line
                words, link = _unlink(str(it.get("line") or "")) if it.get("kept") else ("", "")
                out.append({"id": f"item:{lid or conv}", "kind": "question" if asks else "item",
                            "label": label, "title": _plain(words, 300) or kept_title(level), "detail": "",
                            "urgency": urgency, "due": due, "fire": _fire(urgency, due, now),
                            "mins": MINUTES["question" if asks else "item"], "kept": level,
                            **({"link": link} if link else {}), "_lid": lid, "_conv": conv})
                continue
            head = _plain(it.get("head"), 120)
            line = str(it.get("line") or "")
            rest = line[len(it.get("head") or ""):].lstrip(" :,") if line.startswith(it.get("head") or "\0") \
                else line
            rest = _plain(rest, 200)
            title, detail = (rest or head, head) if asks else (head or rest, rest)
            out.append({"id": f"item:{lid or conv or hashlib.sha1(line.encode()).hexdigest()[:10]}",
                        "kind": "question" if asks else "item", "label": label, "title": title or "An item",
                        "detail": detail if detail != title else "", "urgency": urgency, "due": due,
                        "fire": _fire(urgency, due, now), "mins": MINUTES["question" if asks else "item"],
                        "_lid": lid, "_conv": conv})
    for e in src.replies or []:
        if not isinstance(e, dict):
            continue
        lid, conv = str(e.get("id") or ""), str(e.get("conversation_id") or "")
        if taken(lid, conv) or lid in gone_ids or (conv and conv in gone_convs):
            continue
        age = int(e.get("age_days") or 0)
        queued = e.get("queued") if isinstance(e.get("queued"), dict) else {}
        if age < REPLY_MIN_DAYS or queued.get("outcome") == "left" or _no_reply(e) or _no_reply(rows.get(lid)):
            continue
        take(lid, conv)
        urgency = "soon" if age >= 3 else "calm"
        waited = f"Waiting {_n(age, 'day')}"
        level = e.get("keep_level") or ("facts" if e.get("kept") else "")
        if is_kept(level):
            out.append({"id": f"reply:{lid}", "kind": "reply", "label": "Reply owed",
                        "title": kept_title(level, day=_mail_day(e.get("since"), src.zone),
                                            who=_plain(e.get("from_name") or e.get("from"), 60),
                                            subject=_plain(e.get("subject"), 100)),
                        "detail": waited, "urgency": urgency, "due": None, "fire": False,
                        "mins": MINUTES["reply"], "kept": level, "_lid": lid, "_conv": conv})
            continue
        title = _plain(e.get("subject") or e.get("description"), 120) or "A reply"
        who = _plain(e.get("from_name") or e.get("from"), 60)
        if queued.get("outcome") == "decision" and queued.get("note"):
            out.append({"id": f"reply:{lid}", "kind": "question", "label": "Answer needed",
                        "title": _plain(queued["note"], 200), "detail": " · ".join(
                            x for x in (title, f"From {who}" if who else "", waited) if x),
                        "urgency": urgency, "due": None, "fire": False, "mins": MINUTES["question"],
                        "_lid": lid, "_conv": conv})
            continue
        out.append({"id": f"reply:{lid}", "kind": "reply", "label": "Reply owed", "title": title,
                    "detail": " · ".join(x for x in (f"From {who}" if who else "", waited) if x),
                    "urgency": urgency, "due": None, "fire": False, "mins": MINUTES["reply"],
                    "_lid": lid, "_conv": conv})
    if not kept_views:
        merged = {o.get("id") for o in (src.briefing or {}).get("open_obligations") or []
                  if isinstance(o, dict) and o.get("merged_into")}
        for t in (src.briefing or {}).get("today_top") or []:
            if not isinstance(t, dict):
                continue
            ref = str(t.get("ref") or "")
            row = rows.get(ref)
            if ref and (taken(ref, "") or (src.open_ids is not None and ref not in src.open_ids)
                        or ref in merged or _off_list(row, today) or _no_reply(row)):
                continue
            title = _plain(t.get("title"), 120)
            if not title:
                continue
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(t.get("title") or "") + " " + str(t.get("reason") or ""))
            due = _due_at(m.group(1), src.zone) if m else None
            urgency = _urgency(due, now)
            title = re.sub(r"^Due \d{4}-\d{2}-\d{2}:\s*", "", title)
            take(ref, str((row or {}).get("conversation_id") or ""))
            if is_kept((row or {}).get("keep_level")):
                brief = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else {}
                out.append({"id": f"item:{ref}", "kind": "item", "label": "To do",
                            "title": kept_title(row["keep_level"], day=_mail_day(row.get("date_identified"), src.zone),
                                                who=_plain(row.get("from_name") or row.get("from_email"), 60),
                                                subject=_plain(brief.get("subject"), 100)),
                            "detail": "", "urgency": urgency, "due": due, "fire": _fire(urgency, due, now),
                            "mins": MINUTES["item"], "kept": row["keep_level"], "_lid": ref,
                            "_conv": str(row.get("conversation_id") or "")})
                continue
            out.append({"id": f"item:{ref or hashlib.sha1(title.encode()).hexdigest()[:10]}", "kind": "item",
                        "label": "To do", "title": title, "detail": _plain(t.get("reason"), 120),
                        "urgency": urgency, "due": due, "fire": _fire(urgency, due, now),
                        "mins": MINUTES["item"], "_lid": ref,
                        "_conv": str((row or {}).get("conversation_id") or "")})
    out = _cases_left(out, src)
    rank = {"overdue": 0, "soon": 1, "calm": 2}
    kinds = {"request": 0, "question": 1, "reply": 2, "item": 3, "letter": 3, "case": 3}
    out.sort(key=lambda x: (rank.get(x["urgency"], 2), kinds[x["kind"]], x["due"] or float("inf")))
    return out[:LEFT_MAX]


def _cases_left(out: list[dict], src: Sources) -> list[dict]:
    """One item per work case in place of its rows, threads and cards
    (case_surface.home_left): kind `letter` for a letter, kind `case` for
    a review, a form, a report; `out` as it is without the store. A
    test's Sources may carry the cases (`cases`, or `letters`)."""
    try:
        import case_surface
    except ModuleNotFoundError:  # imported as preprocessor.home_screen
        from preprocessor import case_surface
    given = getattr(src, "cases", None)
    if given is None:
        given = getattr(src, "letters", None)
    return case_surface.home_left(out, src, cases=given)


def question(src: Sources) -> dict | None:
    """The first-week question the briefing carried today, or None."""
    q = (src.briefing or {}).get("onboarding_question")
    if not isinstance(q, dict) or not q.get("text"):
        return None
    ran = _local_day(_ts((src.briefing or {}).get("generated_at")), src.zone)
    if ran != _today(src):
        return None
    text = _plain(q.get("text"), 300)
    return {"id": str(q.get("id") or ""), "text": text, "why": _plain(q.get("why"), 300),
            "example": _plain(q.get("example"), 200), "left": int(q.get("remaining") or 0),
            "ask": f'My answer to today\'s question ("{_plain(q.get("text"), 120)}"): '}


# ── what is done ─────────────────────────────────────────────────────

def _closed_by(row: dict) -> str:
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    cb = (meta or {}).get("closed_by") if isinstance(meta, dict) else None
    cb = cb if isinstance(cb, dict) else {}
    sig, actor = str(cb.get("signal") or ""), str(cb.get("actor") or "")
    if sig in OWNER_SIGNALS or actor.startswith("user") or (sig == "" and "owner" in actor):
        return "you"
    return "tacitus"


def done(src: Sources, since: float | None = None) -> list[dict]:
    """What got done today (or after `since`), newest first: the ledger rows
    closed (one per mail conversation; `by` says whose doing it was) and the
    requests answered in Action requests. A draft that waits for the
    owner's Send is not done: it is left for him (a `request` item) until
    he sends it, and the send is his answer.
    Each is {"id", "kind", "by", "title", "at", "href"?}."""
    start = since if since is not None else datetime.combine(_today(src), datetime.min.time(), src.zone).timestamp()
    out: list[dict] = []
    convs: set = set()
    for row in src.closed or []:
        at = _ts(row.get("closed_at"))
        if at is None or at < start or at > src.now + 60:
            continue
        conv = str(row.get("conversation_id") or "")
        if conv and conv in convs:
            continue
        if conv:
            convs.add(conv)
        out.append({"id": f"closed:{row.get('id')}", "kind": "closed", "by": _closed_by(row),
                    "title": kept_name() if is_kept(row.get("keep_level"))
                    else _plain(row.get("description"), 120) or "An item", "at": at,
                    **({"kept": row["keep_level"]} if is_kept(row.get("keep_level")) else {}),
                    "_lid": str(row.get("id") or ""), "_conv": conv})
    reqs = src.requests or {}
    for r in list(reqs.get("waiting") or []) + list(reqs.get("done") or []):
        if not isinstance(r, dict):
            continue
        card = r.get("card") if isinstance(r.get("card"), dict) else {}
        changed = _ts(r.get("updated_at"))
        href = _request_href(r, src.dm)
        if r.get("kind") == "status" and not r.get("waiting") and not r.get("working") and changed \
                and start <= changed <= src.now + 60:
            _ids, cv = _card_keys(card)
            if any(c in convs for c in cv):
                continue
            convs.update(cv)
            out.append({"id": f"answered:{r.get('id')}", "kind": "answered", "by": "you",
                        "title": kept_name() if is_kept(r.get("keep_level"))
                        else _plain(r.get("text"), 120) or "A request", "at": changed, "href": href,
                        "_rid": str(r.get("id") or "")})
    out.sort(key=lambda x: -x["at"])
    return out[:DONE_MAX]


# ── since the last check-in ──────────────────────────────────────────

def since(src: Sources, got: list[dict], gone: list[dict]) -> dict | None:
    """What changed since the visit before this one: {"at", "lines": [{"text",
    "href"?}]}, None on a first visit or with nothing new."""
    prev = _ts((src.checkin or {}).get("prev"))
    if not prev or src.now - prev < 60:
        return None
    lines: list[dict] = []
    new_reqs = [x for x in got if x["kind"] == "request"]
    reqs = (src.requests or {}).get("waiting") or []
    fresh = [r for r in reqs if isinstance(r, dict) and (_ts(r.get("posted_at")) or 0) > prev
             and not r.get("working")]
    if fresh:
        first = next((x for x in new_reqs if x["id"] == f"request:{fresh[0].get('id')}"), None)
        lines.append({"text": f"{_n(len(fresh), 'new request')} in Action requests.",
                      **({"href": first["href"]} if first else {})})
    mine = [d for d in gone if d["kind"] == "closed" and d["by"] == "tacitus"]
    yours = [d for d in gone if d["by"] == "you"]
    if mine:
        lines.append({"text": f"Tacitus closed {_n(len(mine), 'item')}."})
    if yours:
        lines.append({"text": f"You finished {_n(len(yours), 'thing')}."})
    answers = [t for t in src.recent or [] if isinstance(t, dict) and int(t.get("unread") or 0) > 0
               and (_ts(t.get("last_at")) or 0) > prev]
    if answers:
        from urllib.parse import quote
        t = answers[0]
        lines.append({"text": f"{_n(len(answers), 'conversation')} {'has' if len(answers) == 1 else 'have'} "
                              f"a new answer.",
                      "href": f"#/{quote(str(t.get('channel')), safe='')}/"
                              f"{quote(str(t.get('thread_key') or t.get('id')), safe='')}"})
    for rep in sorted(src.reports or [], key=lambda r: float(r.get("at") or 0)):
        if rep.get("at") and float(rep["at"]) > prev and rep.get("kind") in REPORTS:
            lines.append({"text": _report_arrived(rep, src.zone), "href": f"#/home/{rep['kind']}"})
    if not lines:
        return None
    return {"at": prev, "lines": lines[:SINCE_MAX]}


def _report_arrived(rep: dict, zone: ZoneInfo) -> str:
    word = "briefing" if rep.get("kind") == "morning-briefing" else "afternoon update"
    return f"Your {word} came in at {clock(float(rep['at']), zone)}."


# ── the next gap ─────────────────────────────────────────────────────

def _fit(items: list[dict], minutes: int) -> tuple[int, str]:
    """How many of the list's first items fit in `minutes`, and the words for
    them ("the 2 replies", "the first 3 things on your list")."""
    used, n = 0, 0
    for x in items:
        if used + x["mins"] > minutes:
            break
        used += x["mins"]
        n += 1
    if n == 0:
        return 0, ""
    kinds = {x["kind"] for x in items[:n]}
    if n == len(items):
        if kinds == {"reply"}:
            return n, "the reply" if n == 1 else f"the {n} replies"
        if kinds == {"request"}:
            return n, "the tap" if n == 1 else f"the {n} taps"
        return n, "everything on your list" if n > 1 else "the one thing on your list"
    if n == 1:
        return 1, "the first thing on your list"
    if kinds == {"reply"}:
        return n, f"{n} of the replies"
    return n, f"the first {n} things on your list"


def next_gap(src: Sources, evs: list[dict], todo: list[dict]) -> dict | None:
    """The first free time before the next timed event today: {"line",
    "minutes", "event": {"title", "start", "end"}, "fits"}. Meetings now
    and the ones that follow them with less than SHORT_GAP_MIN between are
    one busy stretch, and the gap is the one after it; with nothing left
    today, the first event tomorrow."""
    now = src.now
    today = _today(src)
    timed = sorted((e for e in evs if not e["all_day"] and e["show"] != "free" and e["end"] > now),
                   key=lambda e: e["start"])
    free_from = now
    for e in timed:  # sorted by start: each one that begins before the stretch ends extends it
        if e["start"] <= free_from + SHORT_GAP_MIN * 60 and e["end"] > free_from:
            free_from = e["end"]
    busy = free_from > now + 60
    upcoming = [e for e in timed if e["start"] >= free_from]
    later_today = [e for e in upcoming if _local_day(e["start"], src.zone) == today]
    until = f"You are busy until {clock(free_from, src.zone)}."
    if later_today:
        e = later_today[0]
        minutes = int((e["start"] - free_from) // 60)
        at = clock(e["start"], src.zone)
        if part_of_day(src) == "night" and not busy:
            return {"line": f"It is late. Your calendar starts at {at}.", "minutes": 0,
                    "event": {k: e[k] for k in ("title", "start", "end")}, "fits": 0}
        if minutes >= LONG_GAP_MIN:
            head = (f"{until} Then nothing until your {at}." if busy
                    else f"Nothing is on your calendar until your {at}.")
        elif busy:
            head = f"{until} Then you have {_n(minutes, 'minute')} before your {at}."
        else:
            head = f"You have {_n(minutes, 'minute')} before your {at}."
        n, words = _fit(todo, minutes)
        tail = f" That is enough for {words}." if n else (" Save the list for after it." if todo else "")
        return {"line": head + tail, "minutes": max(minutes, 0),
                "event": {k: e[k] for k in ("title", "start", "end")}, "fits": n}
    if busy and _local_day(free_from - 1, src.zone) == today:
        return {"line": f"{until} After that your day is open.", "minutes": 0, "event": None, "fits": 0}
    tomorrow = [e for e in upcoming if _local_day(e["start"], src.zone) == today + timedelta(days=1)]
    if tomorrow:
        return {"line": f"Nothing else is on your calendar today. Tomorrow starts at "
                        f"{clock(tomorrow[0]['start'], src.zone)}.",
                "minutes": 0, "event": {k: tomorrow[0][k] for k in ("title", "start", "end")}, "fits": 0}
    return {"line": "Nothing else is on your calendar today.", "minutes": 0, "event": None, "fits": 0}


# ── the greeting ─────────────────────────────────────────────────────

def part_of_day(src: Sources) -> str:
    h = datetime.fromtimestamp(src.now, src.zone).hour
    if h < 4 or h >= 22:
        return "night"
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"


def facts(src: Sources, todo: list[dict], got: list[dict], md: dict, gap: dict | None,
          shown: list[dict]) -> dict:
    """The numbers the greeting may use, and nothing else: no name, no
    subject, no mail."""
    overdue = sum(1 for x in todo if x["urgency"] == "overdue")
    out = {"part_of_day": part_of_day(src), "weekday": datetime.fromtimestamp(src.now, src.zone).strftime("%A"),
           "done_today": len(got), "done_by_tacitus": sum(1 for d in got if d["by"] == "tacitus"),
           "left": len(shown), "overdue": overdue,
           "replies": sum(1 for x in shown if x["kind"] == "reply"),
           "taps": sum(1 for x in shown if x["kind"] == "request"),
           "todos": sum(1 for x in shown if x["kind"] == "item"),
           "questions": sum(1 for x in shown if x["kind"] == "question"),
           "mode": md["kind"], "held": len(todo) - len(shown),
           "light": src.light or "ok"}
    if gap and gap.get("minutes"):
        out["minutes_free"] = gap["minutes"]
        out["next_at"] = clock(gap["event"]["start"], src.zone) if gap.get("event") else ""
    return out


def quiet(f: dict) -> bool:
    """No jokes: the light is red, or something is overdue."""
    return f["light"] == "problem" or f["overdue"] > 0


def _pick(options: list[str], key: str) -> str:
    """One line of several, the same all day: the day picks it."""
    return options[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(options)]


def template(f: dict, tone: str, day_key: str) -> str:
    """The code-built greeting: plain when quiet() holds or the tone is
    facts, else in the tone. Every number comes from `f`."""
    left_n, got, part = f["left"], f["done_today"], f["part_of_day"]
    hi = {"morning": "Morning.", "afternoon": "Afternoon.", "evening": "Evening.", "night": "Late one."}[part]
    if f["light"] == "problem":
        if not left_n:
            return "Tacitus needs a look. Nothing else waits for you."
        return f"Tacitus needs a look. {_n(left_n, 'thing')} {'waits' if left_n == 1 else 'wait'} for you."
    if f["overdue"]:
        return f"{_n(f['overdue'], 'thing')} {'is' if f['overdue'] == 1 else 'are'} overdue. Start there."
    if f["mode"] == "vacation":
        if left_n:
            return f"{_n(left_n, 'thing')} cannot wait. Tacitus holds the rest."
        if tone == "facts":
            return f"Nothing is on fire. {f['held']} held until you are back."
        return _pick(["Nothing is on fire. Go enjoy your time off.",
                      "Nothing is on fire. Close the app and go."], day_key)
    if f["mode"] == "travel":
        if tone == "facts":
            return f"{_n(left_n, 'thing')} cannot wait. {f['held']} held until you are back."
        if not left_n:
            return "Safe travels. Nothing here needs you on the road."
        return f"Safe travels. {_n(left_n, 'thing')} cannot wait. Tacitus holds the rest."
    if not left_n:
        if tone == "facts":
            return "Nothing waits for you."
        if tone == "dry":
            return _pick(["Nothing left. Enjoy it while it lasts.", "Nothing left. Suspicious, but true."], day_key)
        return _pick(["All clear. Go do something fun.", "You are done for today. Really.",
                      "Nothing left for you. Close the app."], day_key)
    if tone == "facts":
        return f"{got} done today. {left_n} {'waits' if left_n == 1 else 'wait'} for you."
    if tone == "dry":
        return _pick([f"{got} handled. {left_n} left.",
                      f"{_n(left_n, 'thing')} for you. Tacitus did {got}."], day_key)
    if got and part in ("morning", "afternoon"):
        return _pick([f"{hi} Tacitus already handled {_n(got, 'thing')}. {left_n} {'is' if left_n == 1 else 'are'} yours.",
                      f"{hi} {got} down, {left_n} to go."], day_key)
    if part == "evening":
        return f"{hi} {_n(left_n, 'thing')} left. Most of it can wait for the morning."
    return _pick([f"{hi} {_n(left_n, 'thing')} for you today. Coffee first.",
                  f"{hi} {_n(left_n, 'thing')} on your list. None of them bite."], day_key)


_BAD_LINE = re.compile(r"[\u2014\u2013\n\r\"*#`<>\[\]{}]|https?:|@")


# How a line counts items: a number (a digit or a word, "a" for one) before
# a noun for items, maybe with a word or two between ("two quick replies").
# The noun says which part of the list it counts; "things", "items" and a
# bare "6 left" are the whole of it. A vague count ("a few replies") cannot
# be checked.
_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_COUNT_WORDS = {**_NUMBER_WORDS, "no": 0, "a": 1, "an": 1, "single": 1, "both": 2}
_VAGUE = ("a few", "a couple of", "a couple", "a handful of", "a bunch of", "a pile of", "lots of",
          "plenty of", "several", "some", "many", "few")
_PART = {"reply": "replies", "replies": "replies", "answer": "replies", "answers": "replies",
         "email": "replies", "emails": "replies", "message": "replies", "messages": "replies",
         "tap": "taps", "taps": "taps", "request": "taps", "requests": "taps", "button": "taps",
         "buttons": "taps", "approval": "taps", "approvals": "taps",
         "task": "todos", "tasks": "todos", "to-do": "todos", "to-dos": "todos", "todo": "todos",
         "todos": "todos", "chore": "todos", "chores": "todos",
         "question": "questions", "questions": "questions", "decision": "questions",
         "decisions": "questions",
         "thing": "left", "things": "left", "item": "left", "items": "left",
         # a bare count of what is left: "6 left", "6 to go", "6 are yours"
         "left": "left", "to go": "left", "remain": "left", "remaining": "left", "waiting": "left",
         "yours": "left", "on your list": "left"}
_COUNT_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _VAGUE), key=len, reverse=True)) + r"|\d+|"
    + "|".join(sorted(_COUNT_WORDS, key=len, reverse=True)) + r")\s+(?:[a-z]+\s+){0,2}?("
    + "|".join(sorted(map(re.escape, _PART), key=len, reverse=True)) + r")\b", re.I)
# What a clause says its count is: done, overdue or held; else what is left.
_DONE_RE = re.compile(r"\b(handled|done|did|finished|closed|cleared|sorted|answered|sent|took care|"
                      r"taken care|wrapped|knocked out|behind you)\b", re.I)
_OVERDUE_RE = re.compile(r"\b(overdue|late|past due)\b", re.I)
_HELD_RE = re.compile(r"\b(hold|holds|held|until you are back|when you are back|for later)\b", re.I)
_CLAUSE_RE = re.compile(r"[.;:!?,]|\bbut\b|\bwhile\b", re.I)


def counts_ok(line: str, f: dict) -> bool:
    """Whether every count of items in `line` is true to the facts, so a line
    never makes the list sound smaller or bigger than it is. A count in a
    clause about what got done is the day's done count (or Tacitus's part);
    about what is overdue, the overdue count; about what is held, the held
    count. Of what is left, a part (replies, taps, to-dos) is that part's own
    count, and the line names the whole ("6 things") or parts that add up to
    it. A vague count is refused."""
    left = int(f.get("left") or 0)
    parts_true = {"replies": f.get("replies"), "taps": f.get("taps"), "todos": f.get("todos"),
                  "questions": f.get("questions")}
    whole, parts = [], []
    for clause in _CLAUSE_RE.split(line):
        done, overdue, held = _DONE_RE.search(clause), _OVERDUE_RE.search(clause), _HELD_RE.search(clause)
        for m in _COUNT_RE.finditer(clause):
            word, part = m.group(1).lower(), _PART[m.group(2).lower()]
            if word in _VAGUE:
                return False
            n = int(word) if word.isdigit() else _COUNT_WORDS[word]
            if done and done.start() < m.start():
                if n not in (f.get("done_today"), f.get("done_by_tacitus")):
                    return False
            elif overdue:
                if n != f.get("overdue"):
                    return False
            elif held:
                if n != f.get("held"):
                    return False
            elif part == "left":
                whole.append(n)
            else:
                if n != parts_true.get(part):
                    return False
                parts.append(n)
    if whole:
        return all(n == left for n in whole)
    return not parts or sum(parts) == left


def checked_line(text: str, f: dict) -> str:
    """The model's line, or "" when it is not one line, too long, holds a
    number the facts do not (a time's digits count; so does a number in
    words), or counts the list in a way the facts do not (counts_ok)."""
    raw = str(text or "").strip().strip("\"'")
    line = " ".join(raw.split())
    if not line or "\n" in raw or len(line) > GREETING_MAX or _BAD_LINE.search(line):
        return ""
    allowed = set()
    for v in f.values():
        allowed.update(_NUM_RE.findall(str(v)))
    said = _NUM_RE.findall(line) + [str(_NUMBER_WORDS[w]) for w in re.findall(r"[a-z]+", line.lower())
                                     if w in _NUMBER_WORDS and w != "one"]
    if any(n not in allowed for n in said) or not counts_ok(line, f):
        return ""
    return line


def greeting_prompt(f: dict, tone: str, liked: list[str], disliked: list[str]) -> str:
    voice = {"breezy": "friendly and light, a little cheeky, like a good assistant you run into at "
                       "the water cooler",
             "dry": "deadpan and short, a dry wit"}[tone]
    lines = [
        "Write the one line that greets the owner on the Home screen of their assistant app.",
        f"Tone: {voice}. Encouraging, never pushy.",
        "Facts (the only things you may say; every number you write must be one of these):",
        json.dumps(f, sort_keys=True),
        "Rules: one line, at most 90 characters, plain words, no names, no emoji, no quotes, "
        "no dash between clauses. Do not invent a number, a meeting or a task.",
        f"What is left is {f.get('left', 0)} things in all. Speak of it as that whole, or stay general. "
        "Never count only a part of it (the replies, the taps, the to-dos) as if it were all there is.",
    ]
    if liked:
        lines.append("Lines the owner liked: " + " | ".join(liked[-4:]))
    if disliked:
        lines.append("Lines the owner did not like: " + " | ".join(disliked[-4:]))
    lines.append("Write only the line.")
    return "\n".join(lines)


# ── the whole payload ────────────────────────────────────────────────

def build(src: Sources) -> dict:
    """Home as the app draws it, from `src` alone. The greeting is the
    template's; Home.payload() puts the model's line in where it has one."""
    evs = events(src)
    md = mode(src, evs)
    todo = [x for x in left(src) if x["id"] not in src.hidden]
    got = done(src)
    today = _today(src)
    if md["kind"] == "normal":
        shown = todo
    elif md["kind"] == "travel" and md["until"]:
        back = datetime.combine(date.fromisoformat(md["until"]) + timedelta(days=1), datetime.min.time(),
                                src.zone).timestamp()
        shown = [x for x in todo if x["urgency"] == "overdue" or (x["due"] is not None and x["due"] < back)]
    else:
        shown = [x for x in todo if x["fire"]]
    held = [x for x in todo if x not in shown]
    gap = next_gap(src, evs, shown)
    f = facts(src, todo, got, md, gap, shown)
    tone = src.tone if src.tone in TONES else DEFAULT_TONE
    plain = quiet(f) or tone == "facts"
    day_key = f"{today.isoformat()}:{f['part_of_day']}"
    q = question(src)
    if md["kind"] != "normal" and q:
        q = None  # a question waits for a normal day
    reps = sorted((r for r in src.reports or [] if r.get("text") and r.get("kind") in REPORTS
                   and _local_day(float(r.get("at") or 0), src.zone) == today), key=lambda r: -float(r["at"]))
    mode_out = dict(md)
    if md["kind"] != "normal":
        mode_out.update(line=mode_line(md, len(shown), len(held), today), held=len(held))
    out = {
        "as_of": src.now,
        "date": today.isoformat(),
        "day": f"{today.strftime('%A')}, {today.strftime('%B')} {today.day}",
        "greeting": {"text": template(f, tone, day_key), "by": "code", "tone": tone, "plain": plain},
        "facts": f,
        "mode": mode_out,
        "tally": {"done": len(got), "left": len(shown)},
        "left": [_public(x) for x in shown],
        "held": [_public(x) for x in held],
        "done": [_public(d) for d in got],
        # The gap is for fitting the list in: none with nothing left, or on vacation.
        "next": gap if (gap and shown and md["kind"] != "vacation") else None,
        "since": since(src, todo, done(src, _ts((src.checkin or {}).get("prev")) or src.now)),
        "calendar": [{k: e[k] for k in ("title", "start", "end", "all_day")} for e in evs
                     if e["day"] == today.isoformat()],
        "question": q,
        "reports": [{"kind": r["kind"], "title": REPORTS[r["kind"]], "text": r["text"], "at": r["at"]}
                    for r in reps],
        "clear": not shown,
        # The post's own line when its pre-send review did not run.
        "unchecked": next((v["review"]["note"] for v in views(src)
                           if (v.get("review") or {}).get("note")), ""),
        "light": src.light or "ok",
    }
    return out


def mode_line(md: dict, shown: int, held: int, today: date) -> str:
    until = ""
    if md.get("until"):
        try:
            until = _day_name(date.fromisoformat(md["until"]), today)
        except ValueError:
            until = ""
    back = f" until {until}" if until and until != "today" else ""
    keep = f" Tacitus holds {_n(held, 'other thing')} for when you are back." if held else ""
    if md["kind"] == "vacation":
        if not shown:  # the greeting says nothing is on fire
            return f"You are off{back}." + keep
        return f"You are off{back}. Only what cannot wait shows here." + keep
    where = "away" if md.get("why") != "time zone" else "in another time zone"
    return f"You are {where}{back}. Only what cannot wait shows here." + keep


def _public(x: dict) -> dict:
    """An item as the app gets it: its `ref` (the id GET /api/home/item
    takes), and nothing of the bot's own (`mins`, the `_` keys that name
    the row, the reply, the request behind it)."""
    out = {k: v for k, v in x.items() if k != "mins" and not k.startswith("_")}
    out["ref"] = x["id"]
    return out


def signature(p: dict) -> str:
    """What makes two payloads the same for the `home` event: everything but
    the time it was read and the minutes the next gap counts down (the app
    reads Home again every few minutes while it shows)."""
    key = {k: v for k, v in p.items() if k not in ("as_of", "next", "facts")}
    nxt = p.get("next") or {}
    key["next"] = [(nxt.get("event") or {}).get("start"), nxt.get("fits")]
    key["facts"] = {k: v for k, v in (p.get("facts") or {}).items() if k != "minutes_free"}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ── the live side ────────────────────────────────────────────────────

def _ea_dir() -> Path:
    try:
        import owner_trust
    except ModuleNotFoundError:
        from preprocessor import owner_trust
    try:
        return owner_trust.ea_dir()
    except Exception:
        return Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


class _Briefing:
    """briefing-latest.json, read again only when its time stamp changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mtime: float | None = None
        self.data: dict = {}

    def read(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self.data
        if mtime == self.mtime:
            return self.data
        try:
            import resident_io
        except ModuleNotFoundError:
            from preprocessor import resident_io
        text = resident_io.read_text(self.path)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        self.data = data if isinstance(data, dict) else {}
        self.mtime = mtime
        return self.data


def home_settings(cfg: dict | None) -> dict:
    """The web block's `home` settings with their defaults: {"tone",
    "briefing"}."""
    block = (cfg or {}).get("home") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}
    tone = block.get("tone") if block.get("tone") in TONES else DEFAULT_TONE
    place = block.get("briefing") if block.get("briefing") in BRIEFING_PLACES else DEFAULT_BRIEFING
    return {"tone": tone, "briefing": place}


class Home:
    """Home as the web front end serves it (WebFrontend.home): the payload
    for a device, a check-in, the owner's vote on the greeting, and the
    tick, which tells the open apps when Home changed."""

    def __init__(self, fe, *, clock: Callable[[], float] = time.time,
                 sources: Callable[..., Sources] | None = None,
                 model: Callable[[str], str | None] | None = None,
                 calibrate: Callable[[str, str], None] | None = None,
                 background: bool = True) -> None:
        self.fe = fe
        self.clock = clock
        self._sources = sources
        self._model = model
        self._calibrate = calibrate
        self.background = background
        self._briefing = _Briefing(_ea_dir() / "state" / "briefing-latest.json")
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._asked: dict[str, float] = {}
        self._asking: set = set()
        self.settings = home_settings(getattr(fe, "cfg", {}))
        items = _items()
        # What only the bot runs for a choice on an item (the Replies card's
        # draft turn and No reply needed, an Outlook link): ea-chat-bot.py sets them.
        self.hooks = items.Hooks()
        self.acts = items.Acts(self)

    # sources

    def sources(self, device_zone: str = "") -> Sources:
        if self._sources is not None:
            return self._sources(device_zone)
        return self._live_sources(device_zone)

    def _live_sources(self, device_zone: str) -> Sources:
        try:
            import user_tz
        except ModuleNotFoundError:
            from preprocessor import user_tz
        zone = user_tz.zone()
        now = self.clock()
        fe, store = self.fe, self.fe.store
        src = Sources(now=now, zone=zone, device_zone=device_zone if _valid_zone(device_zone) else "",
                      tone=self.settings["tone"], dm=fe.dm_channel(fe.owner_id))
        src.briefing = self._briefing.read()
        try:
            src.requests = fe.requests()
        except Exception as e:
            log.warning("home: Action requests could not be read: %s", e)
        try:
            src.replies, src.rows, src.closed = _ledger_reads(zone, now)
            src.open_ids = set(src.rows)
        except Exception as e:
            log.warning("home: the ledger could not be read: %s", e)
        try:
            src.travel = _travel()
        except Exception as e:
            log.debug("home: travel mode could not be read: %s", e)
        light = getattr(fe, "light", None)
        if light is not None:
            try:
                src.light = str(light.summary().get("level") or "")
            except Exception:
                src.light = ""
        src.reports = self.reports()
        src.checkin = self.checkins()
        mark_keep(src)
        try:
            hidden = fe.hidden_channels()
            src.recent = [t for t in store.recent(20).get("threads", []) if t.get("channel") not in hidden]
        except Exception:
            src.recent = []
        return src

    # the store's own records

    def _meta(self, name: str) -> dict:
        try:
            return self.fe.store.home_get(name)
        except Exception:
            return {}

    def _meta_set(self, name: str, value: dict) -> None:
        self.fe.store.home_set(name, value)

    def reports(self) -> list[dict]:
        """The briefing texts the briefing lane handed to Home, the newest
        of each kind (build() keeps today's)."""
        out = []
        for kind in REPORTS:
            r = self._meta(f"report:{kind}")
            if r.get("text"):
                out.append({"kind": kind, "text": str(r["text"]), "at": float(r.get("at") or 0)})
        return out

    def checkins(self) -> dict:
        return self._meta("checkin")

    def checkin(self) -> dict:
        """The owner opened Home: a visit that starts more than VISIT_GAP_S
        after the last check-in moves `prev` to the last one."""
        now = self.clock()
        with self._lock:
            got = self.checkins()
            last = float(got.get("last") or 0)
            if not last or now - last >= VISIT_GAP_S:
                got["prev"] = last or None
            got["last"] = now
            self._meta_set("checkin", got)
        return got

    # the payload

    def payload(self, device_zone: str = "", checkin: bool = False) -> dict:
        if checkin:
            try:
                self.checkin()
            except Exception as e:
                log.warning("home: the check-in was not kept: %s", e)
        key = device_zone if _valid_zone(device_zone) else ""
        hit = self._cache.get(key)
        now = self.clock()
        if hit and now - hit[0] < FRESH_S and not checkin:
            return hit[1]
        src = self.sources(key)
        src.hidden = self._hidden()
        p = build(src)
        self._greet(p)
        self._cache[key] = (now, p)
        return p

    def _greet(self, p: dict) -> None:
        """Put the model's line in where it has one for these facts; else ask
        for one on a thread of its own, and keep the template's line."""
        g = p["greeting"]
        if g["plain"]:
            return
        key = self._greeting_key(p)
        cached = self._meta("greeting")
        if cached.get("key") == key and cached.get("text"):
            g.update(text=cached["text"], by="model")
            return
        if self._model is None and not _local_ready():
            return
        now = self.clock()
        if key in self._asking or now - self._asked.get("last", 0) < GREETING_EVERY_S:
            return
        self._asked["last"] = now
        self._asking.add(key)
        facts_, tone = dict(p["facts"]), g["tone"]

        def ask() -> None:
            try:
                liked, disliked = _votes()
                text = (self._model or _local_model)(greeting_prompt(facts_, tone, liked, disliked))
                line = checked_line(text or "", facts_)
                if line:
                    self._meta_set("greeting", {"key": key, "text": line, "at": self.clock()})
                    self._cache.clear()
                    self.fe.home_changed()
                else:
                    log.info("home: the model's greeting was not used")
            except Exception as e:
                log.warning("home: no greeting from the model: %s", e)
            finally:
                self._asking.discard(key)
        if self.background:
            threading.Thread(target=ask, name="webchat-home-greeting", daemon=True).start()
        else:
            ask()

    @staticmethod
    def _greeting_key(p: dict) -> str:
        f = p["facts"]
        return hashlib.sha1(json.dumps([p["date"], f["part_of_day"], p["greeting"]["tone"], f["mode"],
                                        f["done_today"], f["left"], f["overdue"], f.get("next_at", "")],
                                       sort_keys=True).encode()).hexdigest()[:16]

    # the tick and the event

    def tick(self) -> dict:
        """One look with the owner's home clock, and a `home` event when Home
        changed (the front end marks the store after it: Home runs). A choice
        on an item whose undo window passed is written first (one a restart
        left behind too)."""
        try:
            self.acts.commit_due()
        except Exception as e:
            log.warning("home: a choice on an item was not written: %s", e)
        self._cache.pop("", None)
        p = self.payload()
        sig = signature(p)
        if sig != self._meta("sig").get("sig"):
            self._meta_set("sig", {"sig": sig})
            self.fe.home_changed()
        if self.background:
            _prewarm(self.fe, p.get("left"))
        return p

    # the owner's vote on the greeting

    def vote(self, up: bool, text: str) -> dict:
        """The owner's thumbs up or down on the greeting: a calibration line
        in the preferences store under the scope `home`, which the next
        greeting reads."""
        line = " ".join(str(text or "").split())[:GREETING_MAX * 2]
        tone = self.settings["tone"]
        note = (f'{"Liked" if up else "Did not like"} the Home greeting ({tone}): "{line}"')
        (self._calibrate or _calibrate)("home", note)
        if not up:
            self._meta_set("greeting", {})
            self._cache.clear()
        return {"ok": True, "vote": "up" if up else "down"}

    # an item opened in place (home_items)

    def _hidden(self) -> set:
        try:
            return set(self.acts.pending())
        except Exception as e:
            log.warning("home: the choices that wait for an undo could not be read: %s", e)
            return set()

    def changed(self) -> None:
        """A choice changed what Home lists: read it again, and tell the apps."""
        self._cache.clear()
        try:
            self.fe.home_changed()
        except Exception as e:
            log.warning("home: the apps were not told Home changed: %s", e)

    def item(self, ref: str, device_zone: str = "") -> dict | None:
        """GET /api/home/item: the item `ref` names, built at once (the quick
        briefing and the choices), or None when Home does not list it."""
        key = device_zone if _valid_zone(device_zone) else ""
        src = self.sources(key)
        return _items().item(src, ref, fe=self.fe, pending=self.acts.pending().get(ref))

    def act(self, ref: str, choice: str, arg: str = "", version: str = "") -> tuple[dict, Callable | None]:
        """POST /api/home/act: (the answer, a call to run once it is sent)."""
        return self.acts.act(ref, choice, arg, version)

    def undo(self, token: str) -> dict:
        """POST /api/home/undo."""
        return self.acts.undo(token)

    def deeper(self, ref: str) -> dict | None:
        """POST /api/home/deeper: start the item's deeper briefing
        (home_deeper.start): {"ok", "state", "note"?}; None when Home does
        not list the item."""
        items = _items()
        src = self.sources("")
        f = items.find(src, ref)
        if f is None:
            return None
        return items.deeper_start(self.fe, f, items.shape(f, src, fe=self.fe))


def _items():
    """home_items, beside this module."""
    if __package__:
        from . import home_items
    else:  # preprocessor/ on sys.path
        import home_items
    return home_items


def _prewarm(fe, left) -> None:
    """The deeper briefings of the first items of Left for you, ahead of a
    tap, where they cost no hosted plan (home_deeper.prewarm). Never
    raises."""
    try:
        try:
            import home_deeper
        except ModuleNotFoundError:  # imported as preprocessor.home_screen
            from preprocessor import home_deeper
        home_deeper.prewarm(fe, left)
    except Exception as e:
        log.debug("home: no prewarm: %s", e)


def _valid_zone(name: str) -> bool:
    if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", name):
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _ledger_reads(zone: ZoneInfo, now: float) -> tuple[list, dict, list]:
    """(the replies owed a day or more, the open rows by id with the owner's
    word on each thread, the rows closed since yesterday), read-only."""
    try:
        import ledger
    except ModuleNotFoundError:
        from preprocessor import ledger
    rows = ledger.with_owner_decisions(ledger.get_open_entries())
    replies = ledger.reply_backlog(rows, draft_convs=None, zone=zone, cap=ledger.REPLY_BACKLOG_CAP,
                                   min_days=REPLY_MIN_DAYS)
    by_id = {r.get("id"): r for r in rows if r.get("id")}
    cutoff = datetime.fromtimestamp(now - 2 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = ledger.guarded_connect(ledger.DB_PATH, read_only=True)
    try:
        import sqlite3
        conn.row_factory = sqlite3.Row
        closed = [dict(r) for r in conn.execute(
            "SELECT id, description, closed_at, conversation_id, metadata, last_action FROM ledger WHERE status IN "
            "('completed', 'closed') AND source IS NULL AND closed_at >= ? ORDER BY closed_at DESC LIMIT 200",
            (cutoff,))]
    finally:
        conn.close()
    return replies, by_id, closed


def _travel() -> dict:
    try:
        import quiet_hours
    except ModuleNotFoundError:
        from preprocessor import quiet_hours
    return dict(quiet_hours._get_travel())


def _local_ready() -> bool:
    try:
        import model_routing
        import remote_llm
    except ModuleNotFoundError:
        from preprocessor import model_routing, remote_llm
    try:
        return model_routing.local_ready(remote_llm.config())
    except Exception:
        return False


def _local_model(prompt: str) -> str | None:
    try:
        import remote_llm
    except ModuleNotFoundError:
        from preprocessor import remote_llm
    res = remote_llm.chat([{"role": "user", "content": prompt}], purpose=PURPOSE, max_tokens=60,
                          timeout=GREETING_TIMEOUT_S, temperature=0.7)
    return str(res.get("content") or "")


def _votes() -> tuple[list[str], list[str]]:
    """The greetings the owner liked and did not like, from the calibration
    lines under the scope `home`."""
    try:
        import preferences
    except ModuleNotFoundError:
        from preprocessor import preferences
    try:
        rows = preferences.select(["home"]).get("calibration") or []
    except Exception:
        return [], []
    liked, disliked = [], []
    for c in rows:
        note = str(c.get("note") or "")
        m = re.search(r'"(.+)"$', note)
        if not m:
            continue
        (disliked if note.startswith("Did not like") else liked).append(m.group(1))
    return liked, disliked


def _calibrate(scope: str, note: str) -> None:
    try:
        import preferences
    except ModuleNotFoundError:
        from preprocessor import preferences
    preferences.update(lambda d: preferences.add_calibration(d, scope=scope, note=note))


# ── Settings: Home ───────────────────────────────────────────────────

class HomeSettings:
    """The Home page of the web chat's Settings (/api/settings/home): the
    greeting's tone, and where the briefing goes. A save writes the web
    block's `home` in the owner-private chat.json, as the Notifications page
    does (web_notify_settings._rewrite)."""

    def __init__(self, fe) -> None:
        self.fe = fe

    def _home(self) -> Home | None:
        return getattr(self.fe, "home", None)

    def get(self) -> dict:
        h = self._home()
        return dict(h.settings if h else home_settings(getattr(self.fe, "cfg", {})))

    def save(self, body: Any, who: dict | None = None) -> dict:
        if not isinstance(body, dict) or not body:
            raise ValueError("Choose a setting to change.")
        changes: dict = {}
        for k, v in body.items():
            if k == "tone":
                if v not in TONES:
                    raise ValueError("The greeting takes Breezy, Dry or Facts only.")
                changes[k] = v
            elif k == "briefing":
                if v not in BRIEFING_PLACES:
                    raise ValueError("The briefing goes to Home or to Ask Tacitus.")
                changes[k] = v
            else:
                raise ValueError(f"Home has no setting named {str(k)[:40]!r}.")
        try:
            import web_notify_settings as wns
        except ModuleNotFoundError:
            from preprocessor import web_notify_settings as wns
        merged: dict = {}

        def change(block: dict) -> None:
            web = block.get("web") if isinstance(block.get("web"), dict) else {}
            merged.update(home_settings(web))
            merged.update(changes)
            block["web"] = {**web, "home": dict(merged)}
        try:
            wns._rewrite(change, "Home settings")
        except wns.NotifyRefused as e:
            raise ValueError(str(e)) from None
        log.info("web chat: Home settings saved (%s): %s",
                 str((who or {}).get("credential") or "unknown")[:20], json.dumps(changes, sort_keys=True))
        h = self._home()
        if h is not None:
            h.settings = dict(merged)
            h._cache.clear()
            try:
                self.fe.home_changed()
            except Exception:
                pass
        if isinstance(getattr(self.fe, "cfg", None), dict):
            self.fe.cfg["home"] = dict(merged)
        return {**merged, "saved": True}
