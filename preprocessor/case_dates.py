#!/usr/bin/env python3
"""One date per case, then quiet until it is time: the date rules every
surface of a work case shares (cases.py keeps the store, case_surface.py
the surfaces, letters_work.py a letter's buttons).

A case has ONE date it is done by (`do_by`). The owner gives it with his
yes ("Yes, by Tue Dec 1": the earliest date the records state, or a date
he picks), or once on the date card when the case had none. Every part is
due by it: a part's own date is kept and counts only when it is earlier
(a part that comes later with an earlier date moves the case's date to
it, cases.pull_date).

The case then waits out of sight until its LEAD TIME before its nearest
date (letters 7 days, forms 3, reviews 10, reports 7: the kind's `lead`,
the owner's setting `case_lead_days` in ea-config.json, or the case's own
`lead_days`); `start_on` brings it back sooner. Its stage:

    new        no answer yet: the first decision is open
    undated    he said yes and the case has no date: the store takes the
               earliest date a part states (cases.stated_date); with
               parts and no date anywhere, the date card, once; with no
               part, silence (the weekly review)
    quiet      dated, not started, before its lead time: on no list but
               the weekly review
    due        its time came (or work on it began): the case's block
    ready      the work is ready: the block is the delivery list
    closed     every part delivered or declined

Pure over a case dict in either shape (the store's `parts`, the letters'
`destinations`); the only read is the owner's config for the lead time.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

OPEN_STATES = ("asked", "accepted", "drafted", "ready")
STAGES = ("new", "undated", "quiet", "due", "ready", "closed")
# The days before its date a case of each kind comes back (the kinds
# declare their own as `lead`; these stand in without the registry).
LEAD_DAYS = {"letter": 7, "form": 3, "review": 10, "report": 7}
LEAD_DEFAULT = 7
LEAD_MAX = 60
LEAD_KEY = "case_lead_days"
# How far ahead the date picker reaches.
PICK_DAYS = 366


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def day_words(d: date | None, today: date | None = None) -> str:
    """"today", "tomorrow", "Tue Dec 1"; "" for none."""
    if not d:
        return ""
    if today is not None and d == today:
        return "today"
    if today is not None and d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d:%a %b} {d.day}"


def on_day(d: date | None, today: date | None = None) -> str:
    """The day after a verb: "today", "tomorrow", "on Tue Dec 1"."""
    w = day_words(d, today)
    return w if w in ("", "today", "tomorrow") else f"on {w}"


def parts(c: dict) -> list[dict]:
    got = c.get("parts")
    if not isinstance(got, list):
        got = c.get("destinations")
    return [d for d in got or [] if isinstance(d, dict)]


def open_parts(c: dict) -> list[dict]:
    return [d for d in parts(c) if (d.get("state") or "asked") in OPEN_STATES]


def kind_of(c: dict) -> str:
    """A case's kind: `case_kind`, else a letter for the letters' shape,
    else `kind`."""
    k = str(c.get("case_kind") or "")
    if k:
        return k
    if "destinations" in c or "person_name" in c:
        return "letter"
    return str(c.get("kind") or "letter")


def do_by(c: dict) -> date | None:
    return _day(c.get("do_by"))


def own_due(d: dict) -> date | None:
    """A part's own date (what its mail stated), not the case's."""
    return _day(d.get("own_due") if "own_due" in d else d.get("due"))


def is_ready(c: dict) -> bool:
    """The work is ready: an open part waits only for its delivery."""
    return any(d.get("state") == "ready" for d in open_parts(c))


def due_of(d: dict, c: dict, today: date | None = None) -> date | None:
    """The date a part is due by: the case's one date, or the part's own
    when that is earlier. Once the work is ready and the case's date has
    passed (`today`), a part still open with a later date of its own is
    due by that date: it is late only against its own."""
    own, by = own_due(d), do_by(c)
    if own and by and own > by and today and by < today and is_ready(c):
        return own
    got = [x for x in (own, by) if x]
    return min(got) if got else None


def proposed(c: dict) -> date | None:
    """The date a yes proposes: the earliest date its open parts state."""
    return min((x for x in (own_due(d) for d in open_parts(c)) if x), default=None)


def next_due(c: dict, today: date | None = None) -> date | None:
    """The nearest date the case is due by: its open parts' dates (their
    own, else the case's), or the case's own date with no part open."""
    got = [x for x in (due_of(d, c, today) for d in open_parts(c)) if x]
    if got:
        return min(got)
    return do_by(c) if not parts(c) else None


def _config() -> dict:
    ea = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))
    try:
        got = json.loads((ea / "config" / "ea-config.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _int(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if 0 <= n <= LEAD_MAX else default


def kind_lead(kind: str) -> int:
    """The lead time of a kind: the owner's setting, the kind's own
    declaration, else LEAD_DAYS."""
    said = (_config().get(LEAD_KEY) or {})
    if isinstance(said, dict) and said.get(kind) is not None:
        return _int(said.get(kind), LEAD_DAYS.get(kind, LEAD_DEFAULT))
    try:
        try:
            import cases as _cases
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import cases as _cases
        k = _cases.kind(kind)
        if k is not None:
            return _int(getattr(k, "lead", None), LEAD_DAYS.get(kind, LEAD_DEFAULT))
    except Exception:
        pass
    return LEAD_DAYS.get(kind, LEAD_DEFAULT)


def lead(c: dict) -> int:
    """The case's lead time: its own, else its kind's."""
    if c.get("lead_days") not in (None, ""):
        return _int(c.get("lead_days"), kind_lead(kind_of(c)))
    return kind_lead(kind_of(c))


def wake(c: dict) -> date | None:
    """The day the case comes back to the owner's lists: its lead time
    before its nearest date, or its `start_on` when sooner."""
    nd = next_due(c)
    start = _day(c.get("start_on"))
    w = nd - timedelta(days=lead(c)) if nd else None
    return min(x for x in (w, start) if x) if (w or start) else None


def is_new(c: dict) -> bool:
    if c.get("decision"):
        return False
    return all((d.get("state") or "asked") == "asked" for d in parts(c))


def stage(c: dict, today: date) -> str:
    """The case's stage today (STAGES)."""
    if (c.get("status") or "open") != "open" or c.get("decision") == "declined":
        return "closed"
    if is_new(c):
        return "new"
    opn = open_parts(c)
    if parts(c) and not opn:
        return "closed"
    if any(d.get("state") == "ready" for d in opn):
        return "ready"
    if not do_by(c):
        return "undated"
    if any(d.get("state") == "drafted" for d in opn):
        return "due"
    w = wake(c)
    return "quiet" if w and today < w else "due"


def pick_range(today: date) -> tuple[str, str]:
    """(min, max) of the date picker: today to PICK_DAYS ahead."""
    return today.isoformat(), (today + timedelta(days=PICK_DAYS)).isoformat()


def date_spec(today: date, proposed_: date | None = None) -> dict:
    """A date action's `date` (the wire shape the card and Home share):
    {"proposed"?, "min", "max"}."""
    lo, hi = pick_range(today)
    out = {"min": lo, "max": hi}
    if proposed_ and lo <= proposed_.isoformat() <= hi:
        out["proposed"] = proposed_.isoformat()
    return out


def picked(text: str, today: date) -> date | None:
    """A date from the picker (YYYY-MM-DD) within its range, else None."""
    d = _day(str(text or "").strip()) if len(str(text or "").strip()) == 10 else None
    lo, hi = pick_range(today)
    return d if d and lo <= d.isoformat() <= hi else None


# ── a date in the owner's words ───────────────────────────────────────
# Where a frontend has no date picker (a code typed with words), the words
# are read here. The common forms only; anything else is no date, and the
# answer offers the picker again instead of guessing.

import re  # noqa: E402

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"), 1)}
_WEEKDAYS = {w: i for i, w in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


def _next_of(month: int, day: int, today: date, year: int | None = None) -> date | None:
    if year is not None:
        try:
            return date(year if year > 99 else 2000 + year, month, day)
        except ValueError:
            return None
    for y in (today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def parse_words(text: str, today: date) -> date | None:
    """A date from the owner's words: "2026-12-01", "12/1", "12/1/2026",
    "Dec 1", "December 1st", "1 December", "Tue Dec 1", "today",
    "tomorrow", "Friday", "next Friday", "in 10 days", "in 2 weeks". A
    month and day without a year is the next one to come. None for
    anything else."""
    t = " ".join(str(text or "").lower().replace(",", " ").split())
    if not t:
        return None
    if t in ("today", "now"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _next_of(int(m.group(2)), int(m.group(3)), today, int(m.group(1)))
    m = re.search(r"\bin\s+(a|an|one|two|three|\d{1,3})\s+(day|days|week|weeks)\b", t)
    if m:
        n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1))
        n = n if n is not None else int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        return _next_of(int(m.group(1)), int(m.group(2)), today,
                        int(m.group(3)) if m.group(3) else None)
    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:\s+(\d{4}))?", t)
    if m and m.group(1) in _MONTHS:
        return _next_of(_MONTHS[m.group(1)], int(m.group(2)), today,
                        int(m.group(3)) if m.group(3) else None)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\b(?:\s+(\d{4}))?", t)
    if m and m.group(2) in _MONTHS:
        return _next_of(_MONTHS[m.group(2)], int(m.group(1)), today,
                        int(m.group(3)) if m.group(3) else None)
    m = re.search(r"\b(?:(?:next|this|on)\s+)?(mon|tue|wed|thu|fri|sat|sun)(?:day|s|sday|nesday|rs|r|"
                  r"rsday|urday)?\b", t)
    if m:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    return None
