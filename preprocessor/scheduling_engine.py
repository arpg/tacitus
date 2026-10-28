#!/usr/bin/env python3
"""
Scheduling engine: deterministic calendar math for the EA system.

Handles candidate-slot construction, conflict detection, and preference
application. Claude handles judgment (soft vs hard conflicts).

Usage:
  python3 scheduling_engine.py propose-meeting --attendees '[...]' --duration 30
  python3 scheduling_engine.py validate-draft --times '[...]' --duration 30
  python3 scheduling_engine.py check-attendees --attendees a@x,b@y --start <ISO> --end <ISO>
  python3 scheduling_engine.py log-decision --event "..." --action moved --for "..." [--comment "..."]

propose-meeting, validate-draft and check-attendees take --ignore-event <id>
(repeatable) to treat one of the user's own events as free for that call.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
if __name__ == "__main__":
    # graph_client reads the Outlook token cache at import (module level), which
    # ea-bot cannot do under mail mediation (token is owner-only). Mediate/refuse
    # HERE, before that import, so a resident-side solver call forwards to the
    # owner-side mediator and runs there with the token instead of failing at import.
    import id_handles as _id_handles
    sys.argv[1:] = _id_handles.resolve_argv(sys.argv[1:])
    # A canned calendar (EA_CANNED_CALENDAR, the release suite) needs no
    # token, so the engine runs where it is called instead of forwarding to
    # the mediator, whose solver would read the live calendar.
    if not os.environ.get("EA_CANNED_CALENDAR"):
        from mail_mediation import forward_or_none
        forward_or_none("scheduling_engine", sys.argv[1:], transform=_id_handles.annotate_output)
import graph_client as graph
import google_calendar_client as gcal
import resident_io as _rio
from sched_render import label_options

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
PREFS_FILE = EA_DIR / "config" / "scheduling-preferences.json"
DECISIONS_FILE = EA_DIR / "state" / "scheduling-decisions.json"


def _load_prefs() -> dict:
    if PREFS_FILE.exists():
        return json.loads(PREFS_FILE.read_text())
    return {}


# User's local timezone: scheduling-preferences.json, else user_tz.py
# (the profile, then the system's zone).
import user_tz as _user_tz  # noqa: E402
USER_TZ = ZoneInfo(_load_prefs().get("timezone") or _user_tz.name(EA_DIR))
# propose-meeting with this many options or more is an enumeration (a poll
# reply lists every feasible start); fewer is a proposal of separate windows.
ENUMERATE_FROM = 100


# scheduling-decisions.json is resident space: the resident's lanes read it,
# and the verbs that use it run as the owner (the mail mediator executes this
# CLI for the resident). So it is read and rewritten with the resident's
# access (resident_io), which refuses a link or a hard link at the name, and
# only dict entries are kept, so the output and a rewrite carry nothing else.
# calendar_reconciler appends to the same file under the same lock.
def _decision_list(text: str | None) -> list:
    """The dict entries of a decisions text; [] for an empty, corrupt or
    wrongly shaped file."""
    try:
        data = json.loads(text) if text and text.strip() else []
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _load_decisions() -> list:
    return _decision_list(_rio.read_text(DECISIONS_FILE))


def _append_decision(entry: dict) -> None:
    """Append one decision, keeping the last 50, in one locked
    read-modify-write (resident_io.update_text)."""
    def _append(old: str) -> str:
        return json.dumps((_decision_list(old) + [entry])[-50:], indent=2)

    if not _rio.update_text(DECISIONS_FILE, _append, mode=0o664):
        raise OSError(f"could not record the decision in {DECISIONS_FILE}")


# --- Canned calendar ---------------------------------------------------------
# EA_CANNED_CALENDAR=<path> replaces every calendar read with a JSON fixture
# (tests/release/fixtures/calendar-canned.json is the release suite's), so
# the solver, validate-draft and check-attendees run on a machine with no
# Graph or Google token. The fixture holds the event dicts the fetchers
# return, dated with `{{+Nd}}` tokens relative to today:
#
#   outlook     Graph calendarView items (subject, start/end {dateTime,
#               timeZone}, attendees, isAllDay, showAs, categories)
#   google      google_calendar_client.get_events items (source_calendar,
#               source_type "google", is_all_day)
#   ea_managed  the EA-Managed calendar: token rule events (wh/nm/p0/p1/p2/
#               wr), holds and [auto] mirrors; merged into the Google list
#               tagged "ea-managed", as get_all_events does
#   weekly      recurring templates (days MO..SU, start, end, calendar),
#               expanded into one dated instance per day over the horizon,
#               the way singleEvents=True expands a Google recurrence
#   attendees   {email: [{start, end, state}]}: busy blocks that stand in
#               for Graph getSchedule; an address not listed is unknown
#
# A naive dateTime is read in the event's timeZone, else the fixture's
# `timezone`, else UTC, and rewritten with its offset, so the fixture is
# written in wall-clock time and the engine parses it like a live read.
CANNED_CALENDAR_ENV = "EA_CANNED_CALENDAR"
_CANNED_DATE_TOKEN = re.compile(r"\{\{([+-]\d+)d(?::([^}]+))?\}\}")
_CANNED_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_CANNED_HORIZON = (-7, 42)
_CANNED_CACHE: dict = {}


def canned_calendar_path() -> Path | None:
    raw = os.environ.get(CANNED_CALENDAR_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _canned_dates(obj, today: date):
    """`{{+7d}}` in any string becomes the ISO date seven days from today,
    `{{+7d:%A}}` that day's name: the token run_release.expand_dates uses."""
    if isinstance(obj, str):
        return _CANNED_DATE_TOKEN.sub(
            lambda m: (today + timedelta(days=int(m.group(1)))).strftime(m.group(2) or "%Y-%m-%d"), obj)
    if isinstance(obj, list):
        return [_canned_dates(x, today) for x in obj]
    if isinstance(obj, dict):
        return {k: _canned_dates(v, today) for k, v in obj.items()}
    return obj


def _canned_stamp(part, zone: ZoneInfo):
    """One start/end endpoint with its offset written in. A date-only
    (all-day) endpoint and an aware string stay as they are."""
    if not isinstance(part, dict):
        return part
    raw = str(part.get("dateTime") or "")
    if not raw or "T" not in raw:
        return part
    if raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw):
        return part
    try:
        tz = ZoneInfo(part["timeZone"]) if part.get("timeZone") else zone
        stamped = datetime.fromisoformat(raw).replace(tzinfo=tz)
    except (ValueError, KeyError):
        return part
    out = dict(part)
    out["dateTime"] = stamped.isoformat()
    out["timeZone"] = tz.key
    return out


def _canned_weekly(template: dict, today: date, zone: ZoneInfo) -> list[dict]:
    """The dated instances of one weekly template over the horizon."""
    days = {_CANNED_DAYS[d.upper()] for d in template.get("days") or [] if d.upper() in _CANNED_DAYS}
    start_hm, end_hm = str(template.get("start") or "09:00"), str(template.get("end") or "17:00")
    base = {k: v for k, v in template.items() if k not in ("days", "start", "end", "calendar", "weeks")}
    slug = re.sub(r"[^a-z0-9]+", "-", str(template.get("subject") or "event").lower()).strip("-")
    out = []
    first, last = _CANNED_HORIZON
    for offset in range(first, last + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        ev = dict(base)
        ev["subject"] = template.get("subject", "")
        ev["start"] = {"dateTime": f"{day.isoformat()}T{start_hm}:00", "timeZone": zone.key}
        ev["end"] = {"dateTime": f"{day.isoformat()}T{end_hm}:00", "timeZone": zone.key}
        ev["id"] = f"{slug}-{day.isoformat()}"
        ev["recurring_event_id"] = slug
        out.append(ev)
    return out


def load_canned_calendar(path: Path, today: date | None = None) -> dict:
    """The fixture at `path`, dates expanded and times stamped:
    {"outlook", "google", "ea_managed", "attendees", "timezone"}."""
    today = today or datetime.now(USER_TZ).date()
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: a canned calendar is a JSON object")
    zone = ZoneInfo(str(raw.get("timezone") or "UTC"))
    data = _canned_dates(raw, today)
    buckets = {"outlook": [], "google": [], "ea_managed": []}
    for name in buckets:
        for e in data.get(name) or []:
            if isinstance(e, dict):
                buckets[name].append(e)
    for template in data.get("weekly") or []:
        if not isinstance(template, dict):
            continue
        cal = str(template.get("calendar") or "ea_managed")
        buckets.setdefault(cal, []).extend(_canned_weekly(template, today, zone))
    for name, events in buckets.items():
        for e in events:
            e["start"] = _canned_stamp(e.get("start"), zone)
            e["end"] = _canned_stamp(e.get("end"), zone)
            if name == "google":
                e.setdefault("source_type", "google")
                e.setdefault("source_calendar", "google")
            if name == "ea_managed":
                e.setdefault("source_type", "google")
                e["source_calendar"] = "ea-managed"
                e["ea_managed_hold"] = True
            if e.get("is_all_day") and "isAllDay" not in e:
                e["isAllDay"] = True
    attendees = {}
    for email, blocks in (data.get("attendees") or {}).items():
        attendees[str(email).lower()] = [
            {"start": _canned_stamp({"dateTime": str(b.get("start") or "")}, zone)["dateTime"],
             "end": _canned_stamp({"dateTime": str(b.get("end") or "")}, zone)["dateTime"],
             "state": str(b.get("state") or "busy")}
            for b in blocks or [] if isinstance(b, dict) and b.get("start") and b.get("end")]
    return {"outlook": buckets["outlook"], "google": buckets["google"],
            "ea_managed": buckets["ea_managed"], "attendees": attendees, "timezone": zone.key}


def _canned() -> dict | None:
    """The loaded fixture when canned mode is on, cached per path, day and
    file version; None otherwise."""
    path = canned_calendar_path()
    if path is None:
        return None
    today = datetime.now(USER_TZ).date()
    key = (str(path), today, os.stat(path).st_mtime_ns)
    if _CANNED_CACHE.get("key") != key:
        _CANNED_CACHE.clear()
        _CANNED_CACHE["key"] = key
        _CANNED_CACHE["data"] = load_canned_calendar(path, today)
    return _CANNED_CACHE["data"]


def _canned_in_window(events: list, start: datetime, end: datetime) -> list:
    """Copies of the events that touch [start, end], as a calendar view
    would return them."""
    out = []
    for e in events:
        try:
            s = _parse_dt(str((e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or ""))
            t = _parse_dt(str((e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date") or ""))
        except (ValueError, TypeError):
            continue
        if t >= start and s <= end:
            out.append(dict(e))
    return out


_CANNED_STATE_CHARS = {"free": "0", "tentative": "1", "busy": "2", "out_of_office": "3",
                       "oof": "3", "working_elsewhere": "4"}


def _canned_schedule_detail(canned: dict, emails: list[str], start: datetime, end: datetime,
                            interval_minutes: int) -> dict:
    """getSchedule's answer from the fixture's attendee blocks: a view
    string per listed address, an error for one the fixture lacks."""
    base = start.astimezone(timezone.utc)
    minutes = int((end.astimezone(timezone.utc) - base).total_seconds() // 60)
    n = max(0, (minutes + interval_minutes - 1) // interval_minutes)
    out = {}
    for email in emails:
        blocks = canned["attendees"].get(email.lower())
        if blocks is None:
            out[email] = {"view": "", "error": "not in the canned calendar"}
            continue
        parsed = []
        for b in blocks:
            try:
                parsed.append((_parse_dt(b["start"]), _parse_dt(b["end"]),
                               _CANNED_STATE_CHARS.get(b["state"], "2")))
            except (ValueError, KeyError):
                continue
        chars = []
        for i in range(n):
            s = base + timedelta(minutes=i * interval_minutes)
            t = s + timedelta(minutes=interval_minutes)
            ch = "0"
            for bs, bt, state in parsed:
                if bs < t and bt > s:
                    ch = max(ch, state)
            chars.append(ch)
        out[email] = {"view": "".join(chars), "error": ""}
    return out


def _schedule_source() -> str:
    """What answered an attendee's free/busy, for the output."""
    return "canned calendar" if _canned() is not None else "Graph getSchedule"


def _schedule_detail(emails: list[str], start: datetime, end: datetime,
                     interval_minutes: int = 15) -> dict:
    """Attendee free/busy: the fixture in canned mode, else Graph getSchedule."""
    canned = _canned()
    if canned is not None:
        return _canned_schedule_detail(canned, emails, start, end, interval_minutes)
    return graph.get_schedule_detail(emails, start, end, interval_minutes=interval_minutes)


def _fetch_all_events(days_ahead: int = 14) -> tuple[list, list]:
    """Fetch events from ALL calendars. Returns (outlook_events, google_events)."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)

    canned = _canned()
    if canned is not None:
        outlook = _canned_in_window(canned["outlook"], now, end)
        google = _canned_in_window(canned["google"] + canned["ea_managed"], now, end)
        google.sort(key=lambda e: e["start"]["dateTime"])
        return outlook, google

    outlook_events = graph.get_calendar_view(now, end, days_ahead)
    google_events = gcal.get_all_events(now, end, days_ahead)

    return outlook_events, google_events


# --- Token-based scheduling preferences (EA-Managed calendar) ---------------
# The user's day/time scheduling preferences live as recurring, tokened events on
# the EA-Managed calendar (NOT in config), so he can drag/edit them. The leading
# word of each event title is a token; the rest is a human label surfaced in
# solver reports. There is no config fallback. See skills/scheduling-rules.md.
#   wh = working hours (meetings proposed only inside these windows)
#   nm = no meetings / p0 = hard constraint  -> hard block
#   p1 = soft constraint (strong penalty) / p2 = preference (mild penalty)
#   wr = working remotely (informational; never blocks)
_TOKENS = {"p0", "p1", "p2", "nm", "wh", "wr"}

# Separate from the token vocabulary above: any event on ANY calendar whose
# title contains "[note]" records when something happens without claiming
# the user's time (the house cleaner, a delivery window, someone else's travel).
# These never block a slot and are not surfaced for all-day judgment.
_NOTE_MARKER = "[note]"


def _parse_token(summary: str) -> tuple:
    """(token, full_label) if the title starts with a known token, else (None, title)."""
    s = (summary or "").strip()
    head = s.split(None, 1)[0].lower() if s else ""
    if head in _TOKENS:
        return head, s
    return None, s


def _is_token_event(summary: str) -> bool:
    return _parse_token(summary)[0] is not None


def _is_note_event(summary: str) -> bool:
    """True if the title carries the [note] marker anywhere.

    Unlike the tokens above, [note] is not positional -- "[note] cleaner"
    and "cleaner [note]" both work, so the user can tag an existing event
    without retyping the title.
    """
    return _NOTE_MARKER in (summary or "").lower()


# --- All-day events --------------------------------------------------------
# The two shapes the producers emit:
#
#   Graph  (graph_client.get_calendar_view): {"isAllDay": true, "start":
#          {"dateTime": "2026-10-05T00:00:00.0000000", "timeZone": "UTC"}, ...}
#          -- a dateTimeTimeZone object; the END is midnight of the day AFTER
#          the last covered day.
#   Google (google_calendar_client.get_events): {"is_all_day": true} (snake
#          case) with start normalized to "<first day>T00:00:00" and end to
#          "<exclusive end date>T23:59:59".
#
# Raw API items ({"start": {"date": "2026-10-05"}}) are accepted too, so a
# direct API read or a hand-built event still classifies.

def _is_all_day_event(e: dict) -> bool:
    """True for an all-day event from ANY of the shapes above."""
    if e.get("isAllDay") or e.get("is_all_day"):
        return True
    start = e.get("start") or {}
    if not isinstance(start, dict):
        return False
    return bool(start.get("date")) and not start.get("dateTime")


def _event_day(part: dict) -> date | None:
    """The calendar date of one endpoint of an all-day event.

    Reads the DATE PART of the string instead of converting to local time.
    Graph reports all-day boundaries as UTC (midnight of a local day, so
    06:00Z here) while Google reports naive local midnight; a blanket
    `.astimezone(USER_TZ)` would move the Google one back a day. The date
    part is right for both. Same convention as the all-day branch of
    `_load_token_constraints`.
    """
    if not isinstance(part, dict):
        return None
    raw = part.get("date") or part.get("dateTime") or ""
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _all_day_span(e: dict) -> tuple | None:
    """(first_day, last_day) INCLUSIVE for an all-day event, else None.

    Both producers report the end date EXCLUSIVELY -- Graph as midnight of
    the following day, Google as the exclusive end date with a 23:59:59 tail
    bolted on by the client -- so the last covered day is `end - 1 day`. An
    end at or before the start (a hand-built event using an inclusive end)
    means a single day. A Mon-Fri conference therefore covers five days.
    """
    if not _is_all_day_event(e):
        return None
    first = _event_day(e.get("start") or {})
    if first is None:
        return None
    last = _event_day(e.get("end") or {})
    if last is None or last <= first:
        return first, first
    return first, last - timedelta(days=1)


def _non_blocking_calendars() -> set:
    """Google accounts configured with blocks_availability: false.

    Their events never remove the user's time (a spouse's calendar is context,
    not a commitment). Unreadable config means treat everything as blocking,
    which is the conservative direction.
    """
    try:
        accounts = gcal._get_google_accounts()
    except Exception:
        return set()
    return {a["email"] for a in accounts
            if not a.get("blocks_availability", True)}


def _all_day_disposition(e: dict, non_blocking: set = frozenset()) -> dict | None:
    """Metadata + engine disposition for one all-day event, or None when the
    event needs no judgment.

    None is returned for: anything that is not all-day, a `[note]` event
    (informational by definition), and an EA-Managed token event
    (wh/nm/p0/p1/p2/wr), whose meaning is already applied as a scheduling
    constraint and must not be double-counted here.

    Otherwise the dict carries what `skills/scheduling-rules.md` ("All-day
    event judgment") promises the LLM: title, dates, showAs, calendar, the
    engine's `classification`, and the `effect` it actually applied.
    `end_date` is the INCLUSIVE last day.
    """
    span = _all_day_span(e)
    if span is None:
        return None
    subject = e.get("subject", "") or ""
    if _is_note_event(subject):
        return None
    source_cal = e.get("source_calendar", "") or ""
    if source_cal == "ea-managed" and _is_token_event(subject):
        return None

    first, last = span
    classification = _classify_all_day_event(subject, e.get("showAs", ""))
    effect = {"hard_block": "blocked", "soft_penalty": "penalized",
              "unknown": "penalized", "ignore": "none"}[classification]
    note = ""
    if source_cal in non_blocking:
        # Same rule as _build_busy_intervals: a calendar marked non-blocking
        # never costs the user time. Still surfaced, so the LLM can overrule.
        effect = "none"
        note = "calendar does not block availability"

    return {
        "subject": subject or "(no subject)",
        "is_all_day": True,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "showAs": e.get("showAs", "unknown"),
        "calendar": source_cal or "Outlook",
        "classification": classification,
        "effect": effect,
        "note": note,
    }


def _internal_domains() -> tuple:
    """Domains whose people are 'internal': meetings with them are cheap
    to move. Derived from the user's own address in user-profile.json
    (preprocessor/institution.py); empty when the profile names none."""
    global _INTERNAL_DOMAINS_CACHE
    try:
        return _INTERNAL_DOMAINS_CACHE
    except NameError:
        pass
    import institution
    suffix = institution.own_suffix()
    if not suffix:
        return ()
    _INTERNAL_DOMAINS_CACHE = (suffix,)
    return _INTERNAL_DOMAINS_CACHE


def _is_movable_block(event: dict) -> bool:
    """True if the relaxation pass may offer this occupied slot away.

    A real 1:1 carries exactly one attendee besides the organizer. Zero
    attendees means a personal block: a lecture, a focus hold, travel, none
    of them movable. Nobody invites eighty students to a calendar entry, so
    attendee count alone cannot tell a 1:1 from a teaching block.

    EA-Managed holds are attendee-less by design and are meant to be
    reclaimable, so they stay offerable. Keying that on the calendar keeps it
    independent of the attendee count.
    """
    if event.get("source_calendar", "") == "ea-managed":
        return True
    # The event's [auto] mirror is the user's word on it: p1 may be offered,
    # p0 never. The heuristic below covers an event not mirrored yet.
    if event.get("mirror_level") == "p1":
        return True
    if event.get("mirror_level") == "p0":
        return False
    attendees = event.get("attendees") or []
    if len(attendees) != 1:
        return False
    # Internal-vs-external is the second axis: a 1:1 with a group member
    # reschedules with one Slack message, but a 1:1 with an EXTERNAL party
    # can take weeks to land and is never offered away by the relaxation
    # pass. The reconciler's p0 mirror says the same, but the solver reads
    # the real calendars before a new meeting's mirror exists, so the rule
    # lives here too.
    a = attendees[0]
    if isinstance(a, str):
        addr = a
    elif isinstance(a, dict):
        addr = (a.get("emailAddress", {}) or {}).get("address") \
            or a.get("email", "") or ""
    else:
        addr = ""
    return addr.lower().endswith(_internal_domains())


def _fetch_ea_managed_events(earliest_date: date, latest_date: date) -> list:
    """Raw EA-Managed calendar event instances across [earliest, latest].

    A single seam so tests can mock the calendar read. get_events expands
    recurrences (singleEvents=True), so each instance already has a concrete
    date -- no weekday math needed here.
    """
    start = datetime(earliest_date.year, earliest_date.month, earliest_date.day,
                     0, 0, 0, tzinfo=USER_TZ).astimezone(timezone.utc)
    end = datetime(latest_date.year, latest_date.month, latest_date.day,
                   23, 59, 59, tzinfo=USER_TZ).astimezone(timezone.utc)
    canned = _canned()
    if canned is not None:
        return _canned_in_window(canned["ea_managed"], start, end)
    ea = gcal._ea_managed_source()
    if not ea:
        return []
    return gcal.get_events(ea["email"], start, end, calendar_id=ea["calendar_id"])


def _load_token_constraints(earliest_date: date, latest_date: date) -> dict:
    """Fetch + parse EA-Managed token events into concrete dated instances.

    Returns {"instances": [ {token,label,date,start_min,end_min,all_day} ],
             "has_wh": bool}. Non-token EA-Managed events (regular auto-holds)
    are skipped here -- they remain in the busy view. Since preferences live
    ONLY on this calendar (no config fallback), 'has_wh' is the solver's
    go/no-go signal: false => refuse rather than guess.

    A multi-day all-day token (`nm IROS travel` dragged across a week) yields
    ONE instance PER DAY it covers, clamped to the requested horizon, so a
    five-day travel block closes five working days.
    """
    raw = _fetch_ea_managed_events(earliest_date, latest_date)
    instances = []
    has_wh = False
    for e in raw:
        token, label = _parse_token(e.get("subject", ""))
        if token is None:
            continue
        is_all_day = _is_all_day_event(e)
        try:
            if is_all_day:
                span = _all_day_span(e)
                if span is None:
                    continue
                first, last = span
                start_min, end_min = 0, 1440
                days = []
                d = max(first, earliest_date)
                while d <= min(last, latest_date):
                    days.append(d)
                    d += timedelta(days=1)
            else:
                sdt = _parse_dt(e.get("start", {}).get("dateTime", "")).astimezone(USER_TZ)
                edt = _parse_dt(e.get("end", {}).get("dateTime", "")).astimezone(USER_TZ)
                start_min = sdt.hour * 60 + sdt.minute
                end_min = edt.hour * 60 + edt.minute
                if edt.date() > sdt.date():   # crosses midnight -> clamp to day end
                    end_min = 1440
                days = [sdt.date()]
        except Exception:
            continue
        for d in days:
            instances.append({"token": token, "label": label, "date": d,
                              "start_min": start_min, "end_min": end_min,
                              "all_day": is_all_day})
        if days and token == "wh":
            has_wh = True
    return {"instances": instances, "has_wh": has_wh}


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def summarize_token_constraints(token_constraints: dict) -> dict:
    """A traceable, per-token summary of what was ACTUALLY read from the
    EA-Managed calendar for this horizon -- the ground truth the resident must
    anchor to. Recurring instances are deduped by (label, window) with the dates
    they cover. EVERY token type is present, so an empty list literally means
    "no such rule exists on the calendar right now" (e.g. no nm => no
    no-meeting blocks). This defeats the failure mode where the resident applies
    a remembered rule -- a Friday-off habit -- that is not on the calendar."""
    buckets = {t: {} for t in ("wh", "nm", "p0", "p1", "p2", "wr")}
    for inst in token_constraints.get("instances", []):
        tok = inst["token"]
        if tok not in buckets:
            continue
        window = "all day" if inst["all_day"] else (
            f"{_hhmm(inst['start_min'])}-{_hhmm(inst['end_min'])}")
        key = (inst["label"], window)
        buckets[tok].setdefault(key, []).append(inst["date"].isoformat())
    summary = {}
    for tok, entries in buckets.items():
        summary[tok] = [
            {"label": label, "window": window, "dates": sorted(dates)}
            for (label, window), dates in sorted(entries.items())
        ]
    return summary


def _check_preference_violations(slot_start_local: datetime, slot_end_local: datetime,
                                  token_constraints: dict) -> dict:
    """Check a proposed time against the EA-Managed token constraints for that
    date. All datetimes must already be in the user's local timezone.
    Returns {"warnings": [...]}.

    All constraints are surfaced as warnings; Claude decides whether to offer an
    override based on who the meeting is with. Working hours / no-meeting blocks
    / preferences all come from tokened calendar events now (no config)."""
    warnings = []
    d = slot_start_local.date()
    start_min = slot_start_local.hour * 60 + slot_start_local.minute
    end_min = slot_end_local.hour * 60 + slot_end_local.minute

    day_insts = [i for i in token_constraints.get("instances", []) if i["date"] == d]
    wh_windows = [(i["start_min"], i["end_min"]) for i in day_insts if i["token"] == "wh"]

    if not wh_windows:
        warnings.append(f"{slot_start_local.strftime('%A')} has no working-hours (wh) window")
    elif not any(start_min >= w0 and end_min <= w1 for w0, w1 in wh_windows):
        warnings.append("outside working hours (wh)")

    _kind = {"nm": "no-meeting block", "p0": "hard constraint",
             "p1": "soft constraint", "p2": "preference"}
    for i in day_insts:
        if i["token"] in ("wh", "wr"):
            continue
        if start_min < i["end_min"] and end_min > i["start_min"]:
            warnings.append(f"overlaps '{i['label']}' ({_kind.get(i['token'], i['token'])})")

    return {"warnings": warnings}


def _blocks_a_proposal(warning: str) -> bool:
    """Whether a _check_preference_violations warning names a rule the
    solver enforces as a hard block (wh, nm, p0)."""
    return (warning.startswith("outside working hours")
            or "has no working-hours (wh) window" in warning
            or warning.endswith("(no-meeting block)")
            or warning.endswith("(hard constraint)"))


def _get_all_day_events_on_date(target_date: date, all_events: list) -> list[dict]:
    """All-day events covering `target_date`, with the metadata Claude judges
    from (see `_all_day_disposition`). Multi-day events count on every day of
    their span, `[note]` events and EA-Managed tokens are left out."""
    non_blocking = _non_blocking_calendars()
    result = []
    for e in all_events:
        span = _all_day_span(e)
        if span is None:
            continue
        first, last = span
        if not (first <= target_date <= last):
            continue
        disposition = _all_day_disposition(e, non_blocking)
        if disposition is None:
            continue
        result.append(disposition)
    return result


def _parse_dt(dt_str: str) -> datetime:
    """Parse a datetime string handling various formats from Graph/Google APIs."""
    dt_str = dt_str.replace("Z", "+00:00")
    # Truncate fractional seconds to 6 digits (Graph API sends 7)
    dt_str = re.sub(r'(\.\d{6})\d+', r'\1', dt_str)
    if "T" not in dt_str:
        dt_str = dt_str + "T00:00:00+00:00"
    if "+" not in dt_str and "-" not in dt_str[10:]:
        dt_str += "+00:00"
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_user_dt(dt_str: str) -> datetime:
    """Parse a time a human or Claude typed, where naive means LOCAL.

    `_parse_dt` reads a naive string as UTC, which is right for Graph and
    Google (both hand back naive UTC) and wrong for anything typed: the UTC
    reading turns a typed 9am into 3am, and every conflict answer about it
    is then correct about the wrong hour.

    An explicit offset is honoured as given, so a `propose-meeting`
    option pasted back verbatim keeps its meaning.
    """
    parsed = _parse_dt(dt_str)
    has_offset = ("+" in dt_str[10:] or "-" in dt_str[10:]
                  or dt_str.rstrip().endswith("Z"))
    if has_offset:
        return parsed
    return parsed.replace(tzinfo=None).replace(tzinfo=USER_TZ)


# --- [auto] mirrors decide what a real event costs ---------------------------
# calendar_reconciler puts an [auto] mirror on EA-Managed for every event on
# the real calendars, and its first word is the event's priority, which the
# user edits. Where an event has a mirror, the mirror decides: p0 blocks and
# is never offered away, p1 blocks but the relaxation pass may offer it
# (a "requires moving X" option), p2 does not block (its token adds the mild
# p2 penalty), and a [note] mirror makes the event free time. An event with
# no mirror yet (made since the last reconciler run) keeps the attendee
# heuristic in _is_movable_block.

def _mirror_level_of(summary: str) -> str:
    """"p0"/"p1"/"p2" or "note" for an [auto] mirror title, else ""."""
    s = (summary or "").strip()
    if "[auto]" not in s:
        return ""
    if s.lower().startswith("[note]"):
        return "note"
    head = s.split(None, 1)[0].lower()
    return head if head in ("p0", "p1", "p2") else ""


def _mirror_index(all_events: list) -> dict:
    """{"keys": {source_key: [(start, end, level)]}, "outlook_times":
    {(start, end): level}} from the EA-Managed mirrors among `all_events`.
    outlook_times lets a Google copy of an Exchange series, which the
    reconciler folds into the Exchange mirror, find that mirror by its
    identical occurrence."""
    keys, outlook_times = defaultdict(list), {}
    for e in all_events:
        key = e.get("mirror_of") or ""
        level = _mirror_level_of(e.get("subject", ""))
        if e.get("source_calendar") != "ea-managed" or not key or not level:
            continue
        try:
            s = _parse_dt(e.get("start", {}).get("dateTime", ""))
            t = _parse_dt(e.get("end", {}).get("dateTime", ""))
        except Exception:
            continue
        keys[key].append((s, t, level))
        if key.startswith("outlook/"):
            outlook_times[(s, t)] = level
    return {"keys": keys, "outlook_times": outlook_times}


def _mirror_level(e: dict, start, end, index: dict) -> str:
    """The level the event's mirror declares, or "" when it has none."""
    if e.get("source_calendar") == "ea-managed":
        return ""  # the EA's own holds are never mirrored
    if e.get("source_type") == "google":
        cal = e.get("calendar_id") or e.get("source_calendar") or ""
        candidates = [f"{cal}/{e.get('recurring_event_id')}",
                      f"{cal}/{e.get('id')}"]
    else:
        candidates = [f"outlook/{e.get('seriesMasterId')}",
                      f"outlook/{e.get('id')}"]
    for key in candidates:
        found = index["keys"].get(key)
        if not found or key.endswith("/None") or key.endswith("/"):
            continue
        # The occurrence's own mirror instance first (the user may relabel
        # one occurrence); else the series' level.
        for s, _t, level in found:
            if s == start:
                return level
        return found[0][2]
    if e.get("source_type") == "google":
        return index["outlook_times"].get((start, end), "")
    return ""


def _build_busy_intervals(all_events: list, keep_soft: bool = False) -> list[tuple]:
    """Build busy intervals from events, skipping non-blocking calendars and all-day events.
    Returns list of (start_dt, end_dt, event_dict) tuples.

    An event whose [auto] mirror says p2 or [note] is not busy time; with
    keep_soft it is kept anyway, for a caller that lists events. A kept
    event carries its mirror's level as "mirror_level"."""
    non_blocking = _non_blocking_calendars()
    mirrors = _mirror_index(all_events)

    busy = []
    for e in all_events:
        source_cal = e.get("source_calendar", "")
        if source_cal in non_blocking:
            continue
        # Tokened EA-Managed events (wh/nm/p0/p1/p2/wr) are scheduling
        # CONSTRAINTS, not busy time -- applied in _build_candidate_slots, not
        # here. A wh working-hours window is emphatically NOT "busy". Non-token
        # EA-Managed events (regular auto-holds) still block normally.
        if source_cal == "ea-managed" and _is_token_event(e.get("subject", "")):
            continue
        # [note] events mark when something happens without claiming the user's
        # time. They never block, on any calendar.
        if _is_note_event(e.get("subject", "")):
            continue
        # All-day events are surfaced to Claude for judgment and applied by
        # classification in _build_candidate_slots, not treated as busy time.
        # Detection covers BOTH producers' shapes: a camelCase-only check
        # lets every Google all-day event through as a 24-hour busy block.
        if _is_all_day_event(e):
            continue
        dt_str = e.get("start", {}).get("dateTime", "")
        if not dt_str:
            continue  # date-only = all-day, skip
        try:
            s = _parse_dt(dt_str)
            t = _parse_dt(e.get("end", {}).get("dateTime", ""))
            # Skip events spanning 24+ hours (all-day without flag)
            if (t - s).total_seconds() >= 86400:
                continue
        except Exception:
            continue
        level = _mirror_level(e, s, t, mirrors)
        if level in ("p2", "note") and not keep_soft:
            continue
        busy.append((s, t, {**e, "mirror_level": level} if level else e))
    return busy


def calendar_fetch_errors() -> list:
    """Per-account calendar read failures from the most recent fetch.

    `gcal.get_all_events` clears and repopulates the journal, so this is
    only meaningful AFTER the events for a command have been fetched. A
    non-empty list means one calendar answered nothing because it could
    not be read -- indistinguishable, downstream, from "that calendar is
    empty". Every command that answers an availability question copies
    this into its output.
    """
    return [dict(e) for e in getattr(gcal, "LAST_FETCH_ERRORS", [])]


def _fetch_error_warning(errors: list) -> str:
    """One-line warning naming the calendars that failed, or ""."""
    if not errors:
        return ""
    accounts = ", ".join(str(e.get("account", "?")) for e in errors)
    return (f"Calendar source(s) failed to load: {accounts}. "
            "A calendar that could not be read looks free here. "
            "Availability is INCOMPLETE -- warn before proposing.")


def _subtract_interval(intervals: list, s, e) -> list:
    """[(start, end)] minus one (s, e). Endpoints compare directly."""
    out = []
    for a, b in intervals:
        if e <= a or s >= b:
            out.append((a, b))
            continue
        if a < s:
            out.append((a, s))
        if b > e:
            out.append((e, b))
    return out


def _local_dt(day: date, minute_of_day: int) -> datetime:
    """Wall-clock local datetime for `minute_of_day` on `day`.

    Built naive then stamped with the zone: adding a timedelta to an
    aware datetime keeps the old UTC offset, which shifts every time on
    a DST changeover day.
    """
    naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minute_of_day)
    return naive.replace(tzinfo=USER_TZ)


def compute_free_slots(
    outlook_events: list | None = None,
    google_events: list | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    days_ahead: int = 14,
    min_minutes: int = 15,
) -> dict:
    """Free time across every calendar, under the SOLVER's model.

    This is the one availability model in the system. A second one, reading
    preferences from anywhere but the EA-Managed calendar, publishes
    availability the solver contradicts in both directions.

    Everything here comes from the same primitives `propose-meeting`
    uses: `_load_token_constraints` (wh/nm/p0/p1/p2/wr on the EA-Managed
    calendar), `_build_busy_intervals` (non-blocking calendars, `[note]`
    events and all-day events excluded) and `_all_day_disposition`.

    Returns {"slots", "calendar_fetch_errors", "has_wh", "unavailable_days",
             "all_day_events"}. Each slot keeps the published shape:
    {start, end, duration_minutes, day} (+ "notes" when a soft token or an
    all-day event touches it), start/end being ISO UTC.
    """
    now = datetime.now(timezone.utc)
    if start is None:
        start = now
    if end is None:
        end = start + timedelta(days=days_ahead)
    if outlook_events is None and google_events is None:
        outlook_events, google_events = _fetch_all_events(days_ahead)
    all_events = list(outlook_events or []) + list(google_events or [])

    start_local = start.astimezone(USER_TZ)
    end_local = end.astimezone(USER_TZ)
    first_date, last_date = start_local.date(), end_local.date()

    token_constraints = _load_token_constraints(first_date, last_date)
    insts_by_date = defaultdict(list)
    for inst in token_constraints.get("instances", []):
        insts_by_date[inst["date"]].append(inst)

    busy = _build_busy_intervals(all_events)
    non_blocking = _non_blocking_calendars()

    # All-day events: same classification the solver applies, so a
    # conference week is gone from availability instead of arriving as a
    # multi-day busy block or (worse) not arriving at all.
    all_day_events = []
    blocked_days = {}      # date -> reason
    soft_day_notes = defaultdict(list)
    for e in all_events:
        span = _all_day_span(e)
        if span is None:
            continue
        disposition = _all_day_disposition(e, non_blocking)
        if disposition is None:
            continue
        first, last = span
        covered = []
        d = max(first, first_date)
        while d <= min(last, last_date):
            covered.append(d.isoformat())
            if disposition["effect"] == "blocked":
                blocked_days.setdefault(
                    d, f"all-day: {disposition['subject']} (blocked)")
            elif disposition["effect"] == "penalized":
                soft_day_notes[d].append(f"all-day: {disposition['subject']}")
            d += timedelta(days=1)
        if covered:
            all_day_events.append({**disposition, "days": covered})

    slots = []
    unavailable_days = {}
    day = first_date
    while day <= last_date:
        day_insts = insts_by_date.get(day, [])
        wh_windows = [(i["start_min"], i["end_min"]) for i in day_insts
                      if i["token"] == "wh"]
        if not wh_windows:
            unavailable_days[day.isoformat()] = (
                f"{day.strftime('%A')}: no working-hours (wh) window")
            day += timedelta(days=1)
            continue
        if day in blocked_days:
            unavailable_days[day.isoformat()] = blocked_days[day]
            day += timedelta(days=1)
            continue

        free = []
        for w0, w1 in sorted(wh_windows):
            s, e = _local_dt(day, w0), _local_dt(day, min(w1, 1440))
            if e > s:
                free.append((s, e))
        # merge overlapping wh windows
        free.sort()
        merged = []
        for s, e in free:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        free = merged

        # nm / p0 are hard blocks; p1 / p2 annotate.
        soft_windows = []
        for inst in day_insts:
            tok = inst["token"]
            if tok in ("nm", "p0"):
                free = _subtract_interval(
                    free, _local_dt(day, inst["start_min"]),
                    _local_dt(day, min(inst["end_min"], 1440)))
            elif tok in ("p1", "p2", "wr"):
                soft_windows.append(
                    (_local_dt(day, inst["start_min"]),
                     _local_dt(day, min(inst["end_min"], 1440)),
                     inst["label"]))

        for busy_start, busy_end, _event in busy:
            free = _subtract_interval(free, busy_start.astimezone(USER_TZ),
                                      busy_end.astimezone(USER_TZ))

        for s, e in free:
            s = max(s, start_local)
            e = min(e, end_local)
            duration = (e - s).total_seconds() / 60
            if duration < min_minutes:
                continue
            notes = [label for (w0, w1, label) in soft_windows
                     if s < w1 and e > w0]
            notes += soft_day_notes.get(day, [])
            slot = {
                "start": s.astimezone(timezone.utc).isoformat(),
                "end": e.astimezone(timezone.utc).isoformat(),
                "duration_minutes": int(duration),
                "day": s.strftime("%A %b %d"),
            }
            if notes:
                slot["notes"] = "; ".join(notes)
            slots.append(slot)
        day += timedelta(days=1)

    errors = calendar_fetch_errors()
    if not token_constraints.get("has_wh"):
        # No wh event anywhere in the horizon means the EA-Managed
        # calendar is empty or unreadable. The solver refuses outright;
        # the briefing publishes nothing and says why, in the field the
        # skills already treat as "availability is not authoritative".
        errors.append({
            "account": "ea-managed",
            "error": ("no working-hours (wh) events readable for this "
                      "window; availability cannot be computed"),
        })
    return {
        "slots": slots,
        "calendar_fetch_errors": errors,
        "has_wh": bool(token_constraints.get("has_wh")),
        "unavailable_days": unavailable_days,
        "all_day_events": all_day_events,
    }


def cmd_validate_draft(args):
    """Final safety check before creating a draft with proposed times."""
    times = json.loads(args.times)
    duration = args.duration

    parsed_times = [_parse_user_dt(t) for t in times]
    days_ahead = max(14, (max(parsed_times) - datetime.now(timezone.utc)).days + 2)

    outlook_events, google_events = _fetch_all_events(days_ahead)
    all_events = outlook_events + google_events
    busy_intervals, ignored_events, ignore_not_found = _drop_ignored_events(
        _build_busy_intervals(all_events), _ignore_ids(args))

    # Token preferences (working hours / no-meeting / prefs) for the proposed
    # dates, read from the EA-Managed calendar.
    _local_dates = [t.astimezone(USER_TZ).date() for t in parsed_times]
    token_constraints = _load_token_constraints(min(_local_dates), max(_local_dates))

    # A calendar that failed to load answered "no events", which reads here
    # as "free". Say so on every proposed time: the bot's send-time preflight
    # renders any non-conflict issue as a warning line, so the draft cannot
    # go out silently during an outage.
    fetch_errors = calendar_fetch_errors()
    fetch_warning = _fetch_error_warning(fetch_errors)

    issues = []
    all_day_seen = []
    for time_str in times:
        slot_start = _parse_user_dt(time_str)
        slot_end = slot_start + timedelta(minutes=duration)
        local_start = slot_start.astimezone(USER_TZ)
        local_end = slot_end.astimezone(USER_TZ)

        # Echo how the string was read. A six-hour misreading is invisible
        # in a {"valid": true} answer.
        issues.append({
            "proposed_time": time_str,
            "type": "interpreted_as",
            "detail": local_start.strftime("%a %b %d %Y, %-I:%M %p %Z")
                      + " to " + local_end.strftime("%-I:%M %p %Z"),
        })

        if fetch_warning:
            issues.append({
                "proposed_time": time_str,
                "type": "calendar_fetch_error",
                "detail": fetch_warning,
            })

        # Check preference warnings
        pref_check = _check_preference_violations(local_start, local_end, token_constraints)
        for w in pref_check["warnings"]:
            issues.append({
                "proposed_time": time_str,
                "type": "preference_warning",
                "detail": w,
            })

        # Check calendar conflicts
        for busy_s, busy_e, event in busy_intervals:
            if slot_start < busy_e and slot_end > busy_s:
                issues.append({
                    "proposed_time": time_str,
                    "type": "conflict",
                    "conflict_with": event.get("subject", "event"),
                    "conflict_start": busy_s.isoformat(),
                    "conflict_end": busy_e.isoformat(),
                })

        # All-day events on that date. A hard-blocking one (conference,
        # travel, showAs oof) IS a conflict; a hold or an unrecognized title
        # is surfaced for judgment; an "ignore" (birthday, deadline) stays
        # out of the issue list and appears only under `all_day_events`, so
        # the send-time preflight does not warn on a birthday.
        for ad in _get_all_day_events_on_date(local_start.date(), all_events):
            entry = dict(ad)
            entry["proposed_time"] = time_str
            all_day_seen.append(entry)
            if ad["effect"] == "blocked":
                issues.append({
                    "proposed_time": time_str,
                    "type": "conflict",
                    "conflict_with": f"{ad['subject']} (all-day)",
                    "conflict_start": f"{ad['start_date']}T00:00:00",
                    "conflict_end": f"{ad['end_date']}T23:59:59",
                })
            elif ad["classification"] != "ignore":
                issues.append({
                    "proposed_time": time_str,
                    "type": "all_day_event",
                    "detail": (f"All-day event: {ad['subject']} "
                               f"(showAs: {ad['showAs']}, "
                               f"{ad['classification']})"),
                })

    # valid = true only if no hard conflicts. Preference warnings and
    # informational all-day events don't block; an all-day event the engine
    # hard-blocks on (conference, travel, out of office) counts as a conflict.
    hard_issues = [i for i in issues if i["type"] == "conflict"]
    # A time propose-meeting would never return (outside the day's working
    # hours, a day with none, a no-meeting block, a p0 event) stays "valid"
    # for a time the sender offered or the user named, but it is not one to
    # propose on the EA's own initiative.
    unproposable = sorted({i["proposed_time"] for i in issues
                           if i["type"] == "preference_warning"
                           and _blocks_a_proposal(i.get("detail", ""))})
    output = {
        "valid": len(hard_issues) == 0,
        "proposable": not hard_issues and not unproposable,
        "not_proposable": unproposable,
        "proposable_note": ("not_proposable times break a rule propose-meeting "
                            "enforces (working hours, a no-meeting block, a p0 "
                            "event). Offer one only when the sender offered it "
                            "or the user named it; otherwise say the window "
                            "has no time and ask.") if unproposable else "",
        "issues": issues,
        # Every all-day event covering a proposed date, with the engine's
        # `classification` and the `effect` it applied. The judgment on an
        # "unknown" one is yours (skills/scheduling-rules.md).
        "all_day_events": all_day_seen,
        # Calendars that could not be READ this run. Non-empty means every
        # "free" answer above is unverified for those accounts.
        "calendar_fetch_errors": fetch_errors,
        # The user's own events treated as free for this check (--ignore-event),
        # and any id that matched nothing on the merged calendars.
        "ignored_events": ignored_events,
        "ignore_not_found": ignore_not_found,
        "checked_at": datetime.now(USER_TZ).isoformat(),
    }
    print(json.dumps(output, indent=2))


# --- Ignoring the user's own events ----------------------------------------
# "Can Alan join my 11:15 1:1?" is unanswerable while that 1:1 blocks the
# slot. --ignore-event <id> (repeatable) treats the named event as free time
# for one call. Outlook events carry `id`; Google events carry `event_id`.
# The solver's every other rule still applies, so an ignored event never
# lifts a working-hours or no-meeting block.

def _ignore_ids(args) -> set[str]:
    """The --ignore-event ids on an argparse Namespace, as a set."""
    raw = getattr(args, "ignore_event", None) or []
    if isinstance(raw, str):
        raw = [raw]
    return {s.strip() for s in raw if s and s.strip()}


def _event_id(e: dict) -> str:
    return str(e.get("id") or e.get("event_id") or "")


def _event_summary(e: dict, start=None, end=None) -> dict:
    """One event as the CLI reports it: id, subject, local times, attendee
    count. `start`/`end` are the parsed busy-interval bounds when known."""
    if start is None or end is None:
        try:
            start = _parse_dt(e.get("start", {}).get("dateTime", ""))
            end = _parse_dt(e.get("end", {}).get("dateTime", ""))
        except Exception:
            start = end = None
    return {
        "id": _event_id(e),
        "subject": e.get("subject") or e.get("summary") or "event",
        "start": start.astimezone(USER_TZ).isoformat() if start else "",
        "end": end.astimezone(USER_TZ).isoformat() if end else "",
        "attendees": len(e.get("attendees") or []),
        "source": e.get("source_calendar") or e.get("source_type") or "outlook",
        # The level its [auto] mirror declares; "" when it has no mirror.
        "level": e.get("mirror_level", ""),
    }


def _drop_ignored_events(busy_intervals: list, ignored: set) -> tuple:
    """Remove the busy intervals of the events in `ignored`.

    Returns (kept_intervals, ignored_summaries, ids_not_found). An id that
    matched nothing is reported, not silently accepted: a typo would
    otherwise read as "the meeting is still there".
    """
    if not ignored:
        return busy_intervals, [], []
    kept, dropped, seen = [], [], set()
    for s, t, e in busy_intervals:
        eid = _event_id(e)
        if eid and eid in ignored:
            dropped.append(_event_summary(e, s, t))
            seen.add(eid)
        else:
            kept.append((s, t, e))
    return kept, dropped, sorted(ignored - seen)


# --- check-attendees -------------------------------------------------------
# The question the solver cannot answer: "are THESE people free in THIS
# window?" propose-meeting ranks open slots and blocks every slot the user's
# own calendar fills, so it never scores a window the user already holds
# (adding someone to an existing 1:1). validate-draft reads the user's
# calendars only. This verb reads the attendees' free/busy for the window
# (Graph getSchedule, same tenant) and lists the user's own events in it,
# with ids, so the caller can name one in --ignore-event on the next call.

_VIEW_STATES = {"0": "free", "1": "tentative", "2": "busy",
                "3": "out_of_office", "4": "working_elsewhere"}


def _view_intervals(view: str, start_local: datetime,
                    interval_minutes: int) -> list[dict]:
    """Collapse a getSchedule availabilityView string into non-free runs.
    Each character covers `interval_minutes` of real time from the start,
    shown as local time, so a window across a daylight-saving change
    reports the right clock times."""
    runs = []
    base = start_local.astimezone(timezone.utc)
    for i, ch in enumerate(view):
        state = _VIEW_STATES.get(ch, "unknown")
        if state == "free":
            continue
        s = (base + timedelta(minutes=i * interval_minutes)).astimezone(start_local.tzinfo)
        t = (base + timedelta(minutes=(i + 1) * interval_minutes)).astimezone(start_local.tzinfo)
        if runs and runs[-1]["state"] == state and runs[-1]["_end"] == s:
            runs[-1]["_end"] = t
        else:
            runs.append({"state": state, "_start": s, "_end": t})
    return [{"state": r["state"], "start": r["_start"].isoformat(),
             "end": r["_end"].isoformat()} for r in runs]


def _attendee_status(view: str) -> str:
    """free / busy / tentative / partial / unknown for one attendee's view.
    Busy, OOF and working-elsewhere all count as busy; a window that is
    free in part and busy in part is partial."""
    if not view:
        return "unknown"
    states = {_VIEW_STATES.get(ch, "unknown") for ch in view}
    hard = states & {"busy", "out_of_office", "working_elsewhere", "unknown"}
    if states == {"free"}:
        return "free"
    if not hard:
        return "tentative"          # free and/or tentative only
    if "free" in states:
        return "partial"            # some of the window is open
    return "busy"


def cmd_check_attendees(args):
    """Free/busy for named people over one window, plus the user's own
    events in that window. No ranking, no preferences: a yes/no per person."""
    raw = (args.attendees or "").strip()
    if raw.startswith("["):
        parsed = json.loads(raw)
        emails = [a["email"] if isinstance(a, dict) else str(a) for a in parsed]
    else:
        emails = [s.strip() for s in raw.split(",")]
    emails = [e for e in emails if e]
    if not emails:
        print(json.dumps({"status": "error",
                          "message": "--attendees needs at least one address"}))
        return

    start = _parse_user_dt(args.start).astimezone(USER_TZ)
    end = _parse_user_dt(args.end).astimezone(USER_TZ)
    if end <= start:
        print(json.dumps({"status": "error",
                          "message": "--end must be after --start"}))
        return
    interval = 15
    # Real minutes, so a window across a daylight-saving change is the
    # length of the view Graph returns.
    minutes = int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() // 60)
    n_slots = (minutes + interval - 1) // interval

    schedule = _schedule_detail(emails, start, end, interval_minutes=interval)
    attendees = {}
    for email in emails:
        item = {"view": "", "error": "no schedule returned"}
        for k, v in schedule.items():
            if k.lower() == email.lower():
                item = v
                break
        view = (item.get("view") or "")[:n_slots]
        attendees[email] = {
            "status": _attendee_status(view),
            "view": view,
            "not_free": _view_intervals(view, start, interval),
            "source": (_schedule_source() if view
                       else f"not checked: {item.get('error') or 'no schedule returned'} "
                            "(outside the tenant, or the address is wrong)"),
        }

    days_ahead = max(14, (end - datetime.now(USER_TZ)).days + 2)
    outlook_events, google_events = _fetch_all_events(days_ahead)
    busy, ignored_events, ignore_not_found = _drop_ignored_events(
        _build_busy_intervals(outlook_events + google_events, keep_soft=True),
        _ignore_ids(args))
    organizer_events = [_event_summary(e, s, t) for s, t, e in busy
                        if s < end and t > start]
    # A p2 or [note] event is listed (it has an id to act on) but is not
    # busy time.
    organizer_blocking = [e for e in organizer_events
                          if e["level"] not in ("p2", "note")]

    statuses = {a["status"] for a in attendees.values()}
    all_attendees_free = statuses <= {"free"}
    fetch_errors = calendar_fetch_errors()
    fetch_warning = _fetch_error_warning(fetch_errors)
    output = {
        "status": "ok",
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "interpreted_as": (start.strftime("%a %b %d %Y, %-I:%M %p %Z")
                               + " to " + end.strftime("%-I:%M %p %Z")),
        },
        # Per person: free | busy | tentative | partial | unknown, the raw
        # 15-minute view (0 free, 1 tentative, 2 busy, 3 OOF, 4 elsewhere)
        # and the non-free runs as local times.
        "attendees": attendees,
        "all_attendees_free": all_attendees_free,
        # The user's OWN events overlapping the window, ids included. One of
        # these is usually the meeting being asked about: pass its id to
        # --ignore-event to treat it as free, here or in the other verbs.
        "organizer_events": organizer_events,
        "organizer_free": not organizer_blocking,
        "ignored_events": ignored_events,
        "ignore_not_found": ignore_not_found,
        "calendar_fetch_errors": fetch_errors,
        "warnings": [fetch_warning] if fetch_warning else [],
        "checked_at": datetime.now(USER_TZ).isoformat(),
    }
    print(json.dumps(output, indent=2))


def cmd_log_decision(args):
    """Record a scheduling decision for preference learning."""
    entry = {
        "date": date.today().isoformat(),
        "event": args.event,
        "action": args.action,
    }
    if args.moved_for:
        entry["for"] = args.moved_for
    if args.comment:
        entry["comment"] = args.comment
    if args.override:
        entry["override"] = args.override  # e.g. "lunch", "friday", "after_hours"
    if args.contact_type:
        entry["contact_type"] = args.contact_type  # e.g. "external", "dean", "student"
    if args.contact:
        entry["contact"] = args.contact  # email or name

    _append_decision(entry)
    print(json.dumps({"status": "logged", "entry": entry}))


def cmd_review_patterns(args):
    """Analyze scheduling decisions and suggest preference updates."""
    decisions = _load_decisions()

    if not decisions:
        print(json.dumps({"patterns": [], "suggestions": [],
                           "total_decisions": 0}))
        return

    # Count actions by event type (normalized lowercase)
    event_actions = {}  # event_type -> {moved: N, kept: N, cancelled: N}
    override_counts = Counter()  # override_type -> count
    contact_overrides = {}  # contact -> [override_types]

    for d in decisions:
        event = d.get("event", "unknown").lower()
        action = d.get("action", "")

        if event not in event_actions:
            event_actions[event] = Counter()
        event_actions[event][action] += 1

        if d.get("override"):
            override_counts[d["override"]] += 1
            contact = d.get("contact", d.get("for", "unknown"))
            if contact not in contact_overrides:
                contact_overrides[contact] = []
            contact_overrides[contact].append(d["override"])

    # Generate patterns
    patterns = []
    for event_type, actions in event_actions.items():
        total = sum(actions.values())
        if total >= 2:
            dominant = actions.most_common(1)[0]
            rate = dominant[1] / total
            patterns.append({
                "event_type": event_type,
                "total": total,
                "dominant_action": dominant[0],
                "rate": round(rate, 2),
                "breakdown": dict(actions),
            })

    # Generate suggestions
    suggestions = []

    # Suggest VIP additions for contacts with repeated overrides
    for contact, overrides in contact_overrides.items():
        if len(overrides) >= 2:
            suggestions.append({
                "type": "add_vip",
                "contact": contact,
                "reason": f"Overrode preferences {len(overrides)} times for this contact",
                "overrides": overrides,
            })

    # Suggest preference changes for consistent override patterns
    for override_type, count in override_counts.items():
        if count >= 3:
            suggestions.append({
                "type": "update_preference",
                "preference": override_type,
                "reason": f"Overrode {override_type} {count} times",
                "suggestion": f"Consider relaxing the {override_type} constraint",
            })

    # Suggest soft-classifying event types that are always moved
    for event_type, actions in event_actions.items():
        moved = actions.get("moved", 0)
        total = sum(actions.values())
        if total >= 3 and moved / total >= 0.8:
            suggestions.append({
                "type": "soft_classify",
                "event_type": event_type,
                "reason": f"Moved {moved}/{total} times ({int(moved/total*100)}%)",
                "suggestion": f"Treat '{event_type}' as a soft conflict by default",
            })

    output = {
        "patterns": sorted(patterns, key=lambda p: p["total"], reverse=True),
        "suggestions": suggestions,
        "total_decisions": len(decisions),
    }
    print(json.dumps(output, indent=2))


# -----------------------------------------------------------------------
# Meeting proposal solver (CP-SAT)
# -----------------------------------------------------------------------

# Default solver weights — overridden by config solver_weights key
_DEFAULT_WEIGHTS = {
    "lunch_overlap": 3.0,
    "preferred_slot_miss": 2.0,
    "preferred_window_miss": 1.5,
    "clustering": 1.0,
    "later_in_week": 0.5,
    "after_preferred": 0.5,
    # Token-event soft penalties (EA-Managed calendar): p1 = strong soft
    # constraint, p2 = mild preference. p0/nm are hard blocks (not weighted).
    "p1_soft": 3.0,
    "p2_pref": 0.5,
    # Linear penalty growing from 9 AM (=0) to 5 PM (=1× weight).
    # Default 0 = disabled. Set in scheduling-preferences.json
    # solver_weights to favor earlier slots within working hours.
    "earlier_in_day": 0.0,
    "all_day_hold_tentative": 2.0,
    "all_day_unknown": 1.0,
}

# All-day event classification patterns
_ALL_DAY_HARD_BLOCK = {"conference", "travel", "vacation", "ooo", "flight",
                        "out of office", "away", "retreat"}
_ALL_DAY_IGNORE = {"deadline", "reminder", "birthday", "exam week", "exam",
                    "anniversary", "task"}
_ALL_DAY_SOFT = {"hold", "tentative", "placeholder", "block"}


def _load_solver_weights() -> dict:
    """Load solver weights from config, falling back to defaults."""
    prefs = _load_prefs()
    configured = prefs.get("solver_weights", {})
    weights = dict(_DEFAULT_WEIGHTS)
    weights.update(configured)
    return weights


def _classify_all_day_event(subject: str, show_as: str = "") -> str:
    """Classify an all-day event: 'hard_block', 'ignore', 'soft_penalty', or 'unknown'.

    `show_as` only ESCALATES. An explicit Out-of-Office blocks whatever the
    title says (skills/scheduling-rules.md: "showAs: oof -> always blocked
    (engine enforces this)"), but Outlook marks all-day events `free` by
    default, so a Free conference week must still hard-block on its title.
    """
    if str(show_as or "").lower() in ("oof", "outofoffice"):
        return "hard_block"
    lower = (subject or "").lower()
    for pattern in _ALL_DAY_HARD_BLOCK:
        if pattern in lower:
            return "hard_block"
    for pattern in _ALL_DAY_IGNORE:
        if pattern in lower:
            return "ignore"
    for pattern in _ALL_DAY_SOFT:
        if pattern in lower:
            return "soft_penalty"
    return "unknown"


def _time_to_slot_index(dt: datetime, origin: datetime) -> int:
    """Convert a datetime to a 15-minute slot index relative to origin."""
    delta = (dt - origin).total_seconds()
    return int(delta // 900)


def _slot_index_to_time(index: int, origin: datetime) -> datetime:
    """Convert a 15-minute slot index back to a datetime."""
    return origin + timedelta(minutes=index * 15)


def _get_organizer_busy_intervals(latest: datetime) -> tuple:
    """The organizer's merged calendars up to `latest`.

    Returns ([(start_dt, end_dt, event_dict)], all_events).
    """
    days_ahead = max(14, (latest - datetime.now(timezone.utc)).days + 2)
    outlook_events, google_events = _fetch_all_events(days_ahead)
    all_events = outlook_events + google_events
    return _build_busy_intervals(all_events), all_events


def _get_internal_busy_slots(
    emails: list[str],
    earliest_local: datetime,
    latest_local: datetime,
    total_slots: int,
) -> dict:
    """Busy and tentative slot indices for internal attendees, from Graph
    getSchedule. `earliest_local` is the grid's origin.

    The grid is wall-clock time (96 slots a day); the availability view is
    real time, one character per 15 minutes from the start. Each character
    is placed by its real time, converted to local wall time, so a day
    after a daylight-saving change is not read an hour off.

    Returns {email: {"busy", "tentative"}}; an attendee whose calendar
    could not be read also carries "unchecked": the reason, and no busy
    time (the caller must say so, not treat them as free).
    """
    result = {}
    if not emails:
        return result

    detail = _schedule_detail(
        emails,
        earliest_local,
        latest_local,
        interval_minutes=15,
    )

    base = earliest_local.astimezone(timezone.utc)
    for email, item in detail.items():
        view_str = item.get("view") or ""
        busy_indices, tentative = set(), set()
        for i, ch in enumerate(view_str):
            local = (base + timedelta(minutes=15 * i)).astimezone(earliest_local.tzinfo)
            slot = _time_to_slot_index(local, earliest_local)
            if slot < 0 or slot >= total_slots:
                continue
            # 0=free, 1=tentative, 2=busy, 3=OOF, 4=working elsewhere;
            # tentative is a soft penalty, handled by the caller
            if ch in ('2', '3', '4'):
                busy_indices.add(slot)
            elif ch == '1':
                tentative.add(slot)
        result[email] = {"busy": busy_indices, "tentative": tentative}
        if item.get("error") or not view_str:
            result[email]["unchecked"] = item.get("error") or "no schedule returned"

    return result


def _build_candidate_slots(
    spec: dict,
    weights: dict,
    token_constraints: dict,
    relaxations: list | None = None,
) -> tuple:
    """Build the list of candidate slot indices and their hard/soft constraints.

    Returns (slot_info, hard_blocked, soft_costs, organizer_events, infeasible_days)
    where:
    - slot_info: dict with origin, total_slots, duration_slots, etc.
    - hard_blocked: set of slot indices that are hard-blocked
    - soft_costs: dict mapping slot_index -> dict of {cost_name: cost_value}
    - organizer_events: list of all organizer events (for notes)
    - infeasible_days: dict of day_str -> reason
    """
    duration_minutes = spec["duration_minutes"]
    duration_slots = duration_minutes // 15

    earliest_date = date.fromisoformat(spec["earliest_date"])
    latest_date = date.fromisoformat(spec["latest_date"])

    # Build time origin: start of earliest_date in user timezone
    origin = datetime(earliest_date.year, earliest_date.month, earliest_date.day,
                      0, 0, 0, tzinfo=USER_TZ)
    end_dt = datetime(latest_date.year, latest_date.month, latest_date.day,
                      23, 59, 59, tzinfo=USER_TZ)

    total_days = (latest_date - earliest_date).days + 1
    total_slots = total_days * 96  # 96 fifteen-minute slots per day

    # Collect hard-blocked slots and soft costs
    hard_blocked = set()
    soft_costs = {}  # slot_index -> {cost_name: cost_value}
    infeasible_days = {}

    # --- Step 1: Token-based constraints from the EA-Managed calendar ---
    # Working hours, no-meeting blocks, and time-of-day preferences are read
    # from tokened recurring events (see skills/scheduling-rules.md). There is
    # NO config fallback -- token_constraints is authoritative.
    insts_by_date = defaultdict(list)
    for _inst in token_constraints.get("instances", []):
        insts_by_date[_inst["date"]].append(_inst)

    p1_weight = weights.get("p1_soft", 3.0)
    p2_weight = weights.get("p2_pref", 0.5)

    token_block_reasons = {}   # slot_index -> reason label (for 'excluded')
    wh_slots = set()           # slots inside a wh window (any work day)
    wr_tags = []               # (date, start_min, end_min, label) informational

    for day_offset in range(total_days):
        current_date = earliest_date + timedelta(days=day_offset)
        day_name = current_date.strftime("%A")
        day_base = day_offset * 96  # slot index for midnight of this day
        day_insts = insts_by_date.get(current_date, [])

        wh_windows = [(i["start_min"], i["end_min"]) for i in day_insts
                      if i["token"] == "wh"]

        # No working-hours window today -> the whole day is off (hard block).
        if not wh_windows:
            for s in range(96):
                hard_blocked.add(day_base + s)
            infeasible_days[current_date.isoformat()] = (
                f"{day_name}: no working-hours (wh) window")
            continue

        # A slot is feasible only if the whole meeting fits inside a wh window.
        for s in range(96):
            slot_start_min = s * 15
            slot_end_min = slot_start_min + duration_minutes
            if any(slot_start_min >= w0 and slot_end_min <= w1
                   for (w0, w1) in wh_windows):
                wh_slots.add(day_base + s)
            else:
                hard_blocked.add(day_base + s)

        # Apply nm/p0 (hard) before p1/p2 (soft) so soft tokens skip blocks.
        for inst in sorted(day_insts,
                           key=lambda i: 0 if i["token"] in ("nm", "p0") else 1):
            tok = inst["token"]
            if tok == "wh":
                continue
            if tok == "wr":
                wr_tags.append((current_date, inst["start_min"],
                                inst["end_min"], inst["label"]))
                continue
            start_slot = inst["start_min"] // 15
            end_slot = (inst["end_min"] + 14) // 15   # ceil to cover partials
            for s in range(start_slot, min(end_slot, 96)):
                idx = day_base + s
                if tok in ("nm", "p0"):
                    hard_blocked.add(idx)
                    token_block_reasons.setdefault(idx, inst["label"])
                elif tok in ("p1", "p2"):
                    if idx in hard_blocked:
                        continue
                    w = p1_weight if tok == "p1" else p2_weight
                    soft_costs.setdefault(idx, {})
                    soft_costs[idx][inst["label"]] = max(
                        soft_costs[idx].get(inst["label"], 0.0), w)

    # --- Step 2: Organizer calendar ---
    organizer_busy, organizer_events = _get_organizer_busy_intervals(
        end_dt.astimezone(timezone.utc))
    # --ignore-event: the named events of the user's own are free time for
    # this solve. Reported on slot_info so the caller can see what was lifted.
    organizer_busy, ignored_events, ignore_not_found = _drop_ignored_events(
        organizer_busy, set(spec.get("ignore_events") or []))

    # Movable 1:1 set (for relaxation pass)
    movable_1on1s = set()  # set of slot indices that are blocked by 1:1s
    movable_1on1_info = {}  # slot_index -> event description

    for busy_start, busy_end, event in organizer_busy:
        event_start_idx = _time_to_slot_index(
            busy_start.astimezone(USER_TZ), origin)
        event_end_idx = _time_to_slot_index(
            busy_end.astimezone(USER_TZ), origin)

        attendees = event.get("attendees", [])
        is_movable = _is_movable_block(event)

        for s in range(max(0, event_start_idx), min(total_slots, event_end_idx)):
            if relaxations and "movable_1on1" in relaxations and is_movable:
                movable_1on1s.add(s)
                subj = event.get("subject", "1:1")
                local_start = busy_start.astimezone(USER_TZ)
                attendee_name = ""
                if attendees:
                    a = attendees[0]
                    if isinstance(a, dict):
                        attendee_name = a.get("emailAddress", {}).get("name",
                            a.get("emailAddress", {}).get("address", ""))
                    elif isinstance(a, str):
                        attendee_name = a
                movable_1on1_info[s] = {
                    "subject": subj,
                    "attendee": attendee_name,
                    "time": local_start.strftime("%-I:%M %p"),
                }
            else:
                hard_blocked.add(s)

    # All-day events on the organizer's calendars.
    # The engine classifies and applies (hard block / soft penalty / ignore);
    # the same metadata goes out in `all_day_events` so the LLM can overrule
    # the call it disagrees with (skills/scheduling-rules.md, "All-day event
    # judgment").
    non_blocking = _non_blocking_calendars()
    all_day_events = []
    for e in organizer_events:
        span = _all_day_span(e)
        if span is None:
            continue
        disposition = _all_day_disposition(e, non_blocking)
        if disposition is None:
            continue

        classification = disposition["classification"]
        penalty = (weights.get("all_day_hold_tentative", 2.0)
                   if classification == "soft_penalty"
                   else weights.get("all_day_unknown", 1.0))

        event_first, event_last = span
        covered_days = []
        current = max(event_first, earliest_date)
        while current <= min(event_last, latest_date):
            covered_days.append(current.isoformat())
            day_base = (current - earliest_date).days * 96
            if disposition["effect"] == "blocked":
                for s in range(96):
                    hard_blocked.add(day_base + s)
                infeasible_days[current.isoformat()] = (
                    f"All-day: {disposition['subject']} (blocked)")
            elif disposition["effect"] == "penalized":
                for s in range(96):
                    idx = day_base + s
                    if idx not in hard_blocked:
                        soft_costs.setdefault(idx, {})
                        soft_costs[idx]["all_day_event"] = penalty
            # effect "none" (ignore / non-blocking calendar): report only
            current += timedelta(days=1)

        if not covered_days:
            continue  # outside this horizon entirely
        disposition["days"] = covered_days
        if disposition["effect"] == "penalized":
            disposition["penalty"] = penalty
        all_day_events.append(disposition)

    # --- Step 3: Internal attendees ---
    internal_emails = [a["email"] for a in spec.get("attendees", [])
                       if a.get("type") == "internal"]

    internal_data = {}
    if internal_emails:
        internal_data = _get_internal_busy_slots(
            internal_emails,
            origin,
            end_dt,
            total_slots,
        )

    for data in internal_data.values():
        for s in data["busy"]:
            hard_blocked.add(s)
        for s in data["tentative"]:
            if s not in hard_blocked:
                soft_costs.setdefault(s, {})
                soft_costs[s]["attendee_tentative"] = 1.0

    # --- Step 4a: Days an attendee ruled out ---
    # `excluded_days` on an attendee (YYYY-MM-DD) blocks those days whole.
    # It composes with the zone's working hours (Step 4) and any stated
    # availability instead of replacing them, so a London attendee who
    # rules out one day keeps London hours on the other days.
    for attendee in spec.get("attendees", []):
        for raw in attendee.get("excluded_days") or []:
            try:
                day = date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
            if day < earliest_date or day > latest_date:
                continue
            day_base = (day - earliest_date).days * 96
            for s in range(96):
                hard_blocked.add(day_base + s)
            who = attendee.get("name") or attendee.get("email") or "an attendee"
            infeasible_days.setdefault(day.isoformat(), f"{who} ruled out this day")

    # --- Step 4: Stated availability constraints ---
    # An attendee can be BOTH internal (checked via getSchedule) AND have
    # stated availability constraints. The logic differs by attendee type:
    #   - internal with availability: stated windows are ADDITIONAL constraints
    #     on top of getSchedule data (the person said "only these times work")
    #   - external with availability: stated windows are the ONLY source of
    #     truth. Days without stated availability are fully blocked.
    #   - "availability_complete": the windows are all the person's free time
    #     (a booking link's open slots), so a day with none is blocked for an
    #     internal attendee too; getSchedule still applies on top.
    #   - internal without availability: handled by Step 3 (getSchedule only)
    #   - external/unknown without availability: all working-hours slots feasible
    for attendee in spec.get("attendees", []):
        availability = attendee.get("availability", [])
        if not availability and attendee.get("timezone"):
            availability = _timezone_availability(
                attendee["timezone"], earliest_date, latest_date,
                hours=attendee.get("hours"))
        if not availability:
            continue

        is_internal = attendee.get("type") == "internal"
        complete = bool(attendee.get("availability_complete"))

        # Build set of slots where this attendee IS available
        avail_slots = set()
        for window in availability:
            window_date = date.fromisoformat(window["day"])
            if window_date < earliest_date or window_date > latest_date:
                continue
            day_offset = (window_date - earliest_date).days
            day_base = day_offset * 96
            w_start_h, w_start_m = int(window["start"][:2]), int(window["start"][3:5])
            w_end_h, w_end_m = int(window["end"][:2]), int(window["end"][3:5])
            start_slot = (w_start_h * 60 + w_start_m) // 15
            end_slot = (w_end_h * 60 + w_end_m) // 15
            for s in range(start_slot, end_slot):
                avail_slots.add(day_base + s)

        avail_days = {date.fromisoformat(w["day"]) for w in availability
                      if earliest_date <= date.fromisoformat(w["day"]) <= latest_date}

        for day_offset in range(total_days):
            current_date = earliest_date + timedelta(days=day_offset)
            day_base = day_offset * 96

            if current_date not in avail_days:
                if not is_internal or complete:
                    # External attendee with stated availability but none for
                    # this day: block the entire day
                    for s in range(96):
                        hard_blocked.add(day_base + s)
                # Internal attendees: days without stated availability are
                # constrained only by getSchedule (Step 3), not blocked here
                continue

            # For days WITH stated availability, block slots outside the windows
            for s in range(96):
                idx = day_base + s
                if idx not in avail_slots and idx not in hard_blocked:
                    hard_blocked.add(idx)

    # --- Step 5: Soft constraints from spec ---
    for constraint in spec.get("soft_constraints", []):
        c_type = constraint.get("type", "")
        c_weight = constraint.get("weight", 1.0)

        if c_type == "preferred_slot":
            # Slots OUTSIDE this window get penalized
            pref_date = date.fromisoformat(constraint["day"])
            if pref_date < earliest_date or pref_date > latest_date:
                continue
            day_offset = (pref_date - earliest_date).days
            day_base = day_offset * 96
            ps_start_h, ps_start_m = int(constraint["start"][:2]), int(constraint["start"][3:5])
            ps_end_h, ps_end_m = int(constraint["end"][:2]), int(constraint["end"][3:5])
            pref_start_slot = (ps_start_h * 60 + ps_start_m) // 15
            pref_end_slot = (ps_end_h * 60 + ps_end_m) // 15

            # Penalize all non-preferred slots on all days
            for s in range(total_slots):
                if s in hard_blocked:
                    continue
                # Check if this slot is in the preferred window
                s_day_offset = s // 96
                s_in_day = s % 96
                if s_day_offset == day_offset and pref_start_slot <= s_in_day < pref_end_slot:
                    continue  # In preferred window, no penalty
                soft_costs.setdefault(s, {})
                current_miss = soft_costs[s].get("preferred_slot_miss", 0)
                soft_costs[s]["preferred_slot_miss"] = max(
                    current_miss, weights.get("preferred_slot_miss", 2.0) * c_weight)

        elif c_type == "preferred_window":
            # General time-of-day preference (e.g., "mornings")
            pw_start_h, pw_start_m = int(constraint["start"][:2]), int(constraint["start"][3:5])
            pw_end_h, pw_end_m = int(constraint["end"][:2]), int(constraint["end"][3:5])
            pw_start_slot_in_day = (pw_start_h * 60 + pw_start_m) // 15
            pw_end_slot_in_day = (pw_end_h * 60 + pw_end_m) // 15

            for s in range(total_slots):
                if s in hard_blocked:
                    continue
                s_in_day = s % 96
                if pw_start_slot_in_day <= s_in_day < pw_end_slot_in_day:
                    continue  # In preferred window
                soft_costs.setdefault(s, {})
                current_miss = soft_costs[s].get("preferred_window_miss", 0)
                soft_costs[s]["preferred_window_miss"] = max(
                    current_miss, weights.get("preferred_window_miss", 1.5) * c_weight)

    # --- Step 6: Clustering and buffer penalties ---
    # Find organizer event start/end times as slot indices for proximity calc
    event_slot_indices = []
    for busy_start, busy_end, event in organizer_busy:
        e_start_idx = _time_to_slot_index(busy_start.astimezone(USER_TZ), origin)
        e_end_idx = _time_to_slot_index(busy_end.astimezone(USER_TZ), origin)
        event_slot_indices.append((e_start_idx, e_end_idx, event))

    for s in range(total_slots):
        if s in hard_blocked:
            continue

        slot_day_offset = s // 96

        # Later-in-week penalty
        current_date = earliest_date + timedelta(days=slot_day_offset)
        weekday = current_date.weekday()  # 0=Mon, 4=Fri
        if weekday > 0:
            soft_costs.setdefault(s, {})
            soft_costs[s]["later_in_week"] = (
                weights.get("later_in_week", 0.5) * weekday / 4.0)

        # Earlier-in-day preference: linear cost from working_start (9 AM)
        # to working_end (5 PM). Default weight 0 = disabled (no time-of-day
        # bias beyond preferred_hours). Set solver_weights.earlier_in_day in
        # scheduling-preferences.json to opt in.
        slot_min_of_day = (s % 96) * 15
        # Working window 9 AM - 5 PM = 480 min span
        if 540 <= slot_min_of_day <= 1020:
            earliness = (slot_min_of_day - 540) / 480.0
            if earliness > 0 and weights.get("earlier_in_day", 0.0) > 0:
                soft_costs.setdefault(s, {})
                soft_costs[s]["earlier_in_day"] = (
                    weights.get("earlier_in_day", 0.0) * earliness)

        # Clustering and buffer penalties
        if event_slot_indices:
            min_gap_before = float('inf')
            min_gap_after = float('inf')
            meeting_end_slot = s + duration_slots

            for e_start, e_end, _ in event_slot_indices:
                # Same day check
                if e_start // 96 != slot_day_offset:
                    continue
                # Gap before: event ends before our slot starts
                if e_end <= s:
                    gap = s - e_end
                    min_gap_before = min(min_gap_before, gap)
                # Gap after: event starts after our slot ends
                if e_start >= meeting_end_slot:
                    gap = e_start - meeting_end_slot
                    min_gap_after = min(min_gap_after, gap)

            # Adjacent meetings are allowed, so there is no back-to-back
            # buffer penalty. Clustering is the only proximity signal: a
            # small linear cost as slots drift farther from existing events.

            # Clustering: distance from nearest event (same day)
            nearest = min(
                min_gap_before if min_gap_before != float('inf') else 999,
                min_gap_after if min_gap_after != float('inf') else 999,
            )
            if nearest < 999:
                cluster_cost = (nearest * 15 / 60.0) * weights.get("clustering", 1.0)
                soft_costs.setdefault(s, {})
                soft_costs[s]["clustering"] = cluster_cost

    # Duration feasibility (a meeting starting at slot s would extend into a
    # hard-blocked slot s+d) is enforced by `_solve_one_pass`'s candidate
    # filter, NOT here. hard_blocked means "slots a meeting must not COVER";
    # adding starts that cannot fit a meeting double-counts, and the slot
    # before them then loses a half that is actually free.

    slot_info = {
        "origin": origin,
        "total_slots": total_slots,
        "duration_slots": duration_slots,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
        "total_days": total_days,
        "movable_1on1s": movable_1on1s,
        "movable_1on1_info": movable_1on1_info,
        "ignored_events": ignored_events,
        "ignore_not_found": ignore_not_found,
        # Token-preference transparency (see cmd_propose_meeting 'excluded'):
        "token_block_reasons": token_block_reasons,
        "wh_slots": wh_slots,
        "wr_tags": wr_tags,
        # All-day events in this horizon, with the engine's classification and
        # what it applied. Surfaced for the LLM's block-vs-informational call.
        "all_day_events": all_day_events,
        # Internal attendees whose calendar getSchedule could not read:
        # email -> reason. Their busy time is unknown, never "free".
        "unchecked_attendees": {e: d["unchecked"] for e, d in internal_data.items()
                                if d.get("unchecked")},
    }

    return slot_info, hard_blocked, soft_costs, organizer_events, infeasible_days


def _timezone_availability(tz_name: str, earliest_date: date, latest_date: date,
                           hours=None) -> list:
    """An attendee elsewhere, given as a zone name, becomes stated
    availability: their working hours (9:00 to 17:00 local unless `hours`
    gives ["HH:MM", "HH:MM"]) on each day of the horizon, expressed on the
    user's wall clock. A window that crosses midnight here is split by
    date, so a Tokyo morning lands on the user's previous evening."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise ValueError(f"unknown timezone {tz_name!r}; use an IANA name such as Europe/London")
    h0, h1 = (hours or ["09:00", "17:00"])
    sh, sm = int(h0[:2]), int(h0[3:5])
    eh, em = int(h1[:2]), int(h1[3:5])
    by_day: dict = {}
    d = earliest_date - timedelta(days=1)
    while d <= latest_date + timedelta(days=1):
        start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz).astimezone(USER_TZ)
        end = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz).astimezone(USER_TZ)
        cur = start
        while cur < end:
            day_end = datetime(cur.year, cur.month, cur.day, tzinfo=USER_TZ) + timedelta(days=1)
            seg_end = min(end, day_end)
            if earliest_date <= cur.date() <= latest_date:
                by_day.setdefault(cur.date(), []).append(
                    (cur.strftime("%H:%M"),
                     "24:00" if seg_end == day_end else seg_end.strftime("%H:%M")))
            cur = seg_end
        d += timedelta(days=1)
    return [{"day": day.isoformat(), "start": a, "end": b}
            for day in sorted(by_day) for a, b in by_day[day]]


def _solve_one_pass(
    slot_info: dict,
    hard_blocked: set,
    soft_costs: dict,
    excluded: set,
    num_options: int,
    distinct: bool = False,
) -> list:
    """Run CP-SAT solver to find optimal meeting slots.

    Returns list of dicts with slot_index and cost_breakdown.
    Runs the solver num_options times, excluding previously found slots each time.
    With `distinct`, a pick also excludes every start whose meeting would
    overlap it, so N options are N separate windows rather than one window
    listed at each quarter hour.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return []

    total_slots = slot_info["total_slots"]
    duration_slots = slot_info["duration_slots"]
    results = []
    # `excluded` is a set of EXACT start indices already picked (or
    # otherwise off-limits), distinct from `hard_blocked`, which lists slot
    # indices the solver must not let a meeting *cover*. Consecutive
    # quarter-hour starts whose meetings overlap are allowed: the user picks
    # one, listing both is informative.
    picked_starts = set(excluded)

    for _ in range(num_options):
        model = cp_model.CpModel()

        # Decision variables: one boolean per candidate slot
        # A slot is only a candidate if:
        #   - its start index has not already been picked, AND
        #   - none of the duration_slots consecutive slots starting at
        #     it land on a hard-blocked time.
        candidates = []
        slot_vars = {}
        for s in range(total_slots - duration_slots + 1):
            if s in picked_starts:
                continue
            if any((s + d) in hard_blocked for d in range(duration_slots)):
                continue
            var = model.new_bool_var(f"slot_{s}")
            slot_vars[s] = var
            candidates.append(s)

        if not candidates:
            break

        # Exactly one slot selected
        model.add(sum(slot_vars[s] for s in candidates) == 1)

        # Objective: minimize weighted soft cost
        # CP-SAT works with integers, so multiply costs by 1000 for precision
        SCALE = 1000
        objective_terms = []
        for s in candidates:
            costs = soft_costs.get(s, {})
            total_cost_scaled = int(sum(costs.values()) * SCALE)
            if total_cost_scaled > 0:
                objective_terms.append(slot_vars[s] * total_cost_scaled)

        if objective_terms:
            model.minimize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2.0

        status = solver.solve(model)

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for s in candidates:
                if solver.value(slot_vars[s]) == 1:
                    cost_breakdown = soft_costs.get(s, {})
                    total_cost = sum(cost_breakdown.values())
                    results.append({
                        "slot_index": s,
                        "cost": round(total_cost, 3),
                        "cost_breakdown": {k: round(v, 3) for k, v in cost_breakdown.items() if v > 0},
                    })
                    # Without `distinct`, exclude ONLY the exact picked
                    # start: an enumeration (a poll reply) reads absence
                    # from the options as "blocked", so picking 10:45 must
                    # not hide 10:30 and 11:00 from later passes.
                    picked_starts.add(s)
                    if distinct:
                        picked_starts.update(range(s - duration_slots + 1, s + duration_slots))
                    break
        else:
            break  # No more feasible solutions

    return results


def _format_solver_result(
    result: dict,
    slot_info: dict,
    rank: int,
    organizer_events: list,
    conditional: str | None = None,
    relaxations: list | None = None,
) -> dict:
    """Format a solver result into the output schema."""
    origin = slot_info["origin"]
    duration_slots = slot_info["duration_slots"]
    s = result["slot_index"]

    start_dt = _slot_index_to_time(s, origin)
    end_dt = _slot_index_to_time(s + duration_slots, origin)

    day_label = start_dt.strftime("%A %b %d")
    start_label = start_dt.strftime("%-I:%M %p")
    end_label = end_dt.strftime("%-I:%M %p")
    time_label = f"{start_label}-{end_label} MT"

    # Build notes from adjacent meetings
    notes_parts = []
    mirrors = _mirror_index(organizer_events)
    for e in organizer_events:
        try:
            e_start = _parse_dt(e.get("start", {}).get("dateTime", ""))
            e_end = _parse_dt(e.get("end", {}).get("dateTime", ""))
        except Exception:
            continue

        e_local_start = e_start.astimezone(USER_TZ)
        e_local_end = e_end.astimezone(USER_TZ)

        # Check if event is on the same day
        if e_local_start.date() != start_dt.date():
            continue

        # A real event the slot sits on top of, allowed because its mirror
        # says p2 or [note]: name it, so the proposal never reads as clear.
        if e_start < end_dt and e_end > start_dt:
            level = _mirror_level(e, e_start, e_end, mirrors)
            if level in ("p2", "note"):
                notes_parts.append(
                    f"Overlaps {e.get('subject', 'event')} "
                    f"({e_local_start.strftime('%-I:%M')}-"
                    f"{e_local_end.strftime('%-I:%M %p')}, {level} on EA-Managed)")
            continue

        gap_before = (start_dt - e_local_end).total_seconds() / 60
        gap_after = (e_local_start - end_dt).total_seconds() / 60

        subj = e.get("subject", "event")
        if 0 <= gap_before <= 30:
            notes_parts.append(
                f"After {subj} (ends {e_local_end.strftime('%-I:%M %p')})")
        elif 0 <= gap_after <= 30:
            notes_parts.append(
                f"Before {subj} (starts {e_local_start.strftime('%-I:%M %p')})")

    # All-day events on the proposed day. A hard-blocked day never reaches
    # here, so what is left is what the LLM must judge: a hold, a conference
    # the engine did not recognize, an event on a non-blocking calendar.
    for ad in slot_info.get("all_day_events", []):
        if start_dt.date().isoformat() in ad.get("days", []):
            notes_parts.append(
                f"all-day: {ad['subject']} ({ad['classification']})")

    # "working remotely" (wr) is informational -- note it on overlapping slots.
    slot_start_min = start_dt.hour * 60 + start_dt.minute
    slot_end_min = end_dt.hour * 60 + end_dt.minute
    for wr_date, wr_s, wr_e, wr_label in slot_info.get("wr_tags", []):
        if wr_date == start_dt.date() and slot_start_min < wr_e and slot_end_min > wr_s:
            notes_parts.append(f"working remotely ({wr_label})")
            break

    # UTC offset for ISO format
    utc_offset = start_dt.strftime("%z")
    offset_formatted = f"{utc_offset[:3]}:{utc_offset[3:]}"

    formatted = {
        "rank": rank,
        "start": start_dt.strftime(f"%Y-%m-%dT%H:%M:%S{offset_formatted}"),
        "end": end_dt.strftime(f"%Y-%m-%dT%H:%M:%S{offset_formatted}"),
        "day_label": day_label,
        "time_label": time_label,
        "cost": result["cost"],
        "cost_breakdown": result["cost_breakdown"],
        # The token preferences (p1/p2) that penalized THIS slot -- so the user
        # sees why it costs what it does.
        "applied_preferences": {k: v for k, v in result["cost_breakdown"].items()
                                if _is_token_event(k)},
        "notes": ". ".join(notes_parts) if notes_parts else "",
    }

    if conditional is not None:
        formatted["conditional"] = conditional
        formatted["relaxations"] = relaxations or []
    else:
        formatted["conditional"] = None

    return formatted


def _build_conditional_label(slot_info: dict, slot_index: int) -> tuple:
    """Build a human-readable conditional label for relaxed slots.

    Returns (conditional_str, relaxations_list).
    """
    movable_1on1s = slot_info.get("movable_1on1s", set())
    movable_1on1_info = slot_info.get("movable_1on1_info", {})
    duration_slots = slot_info["duration_slots"]

    conditionals = []
    relaxation_keys = []

    # Check if any 1:1 meetings were made movable for this slot
    overlap_1on1s = set()
    for offset in range(duration_slots):
        s = slot_index + offset
        if s in movable_1on1s and s in movable_1on1_info:
            info = movable_1on1_info[s]
            key = f"{info['subject']}@{info['time']}"
            if key not in overlap_1on1s:
                overlap_1on1s.add(key)
                conditionals.append(
                    f"Move {info['subject']} ({info['time']}) to accommodate")
                attendee = info.get("attendee", "")
                relaxation_keys.append(f"movable_1on1:{attendee}")

    return "; ".join(conditionals) if conditionals else None, relaxation_keys


def _build_excluded(slot_info: dict, hard_blocked: set, soft_costs: dict,
                    proposed_starts: set) -> list:
    """Transparency: WHY certain times were not offered or ranked lower, so the
    user can override a preference.

    - "blocked": times inside working hours removed by a hard token (nm/p0),
      e.g. lunch or no-meeting Friday. Consecutive same-reason slots per day are
      merged into one range.
    - "demoted": feasible times carrying a soft token penalty (p1/p2) that were
      not among the proposed options -- the "kicked further down" cases.
    """
    origin = slot_info["origin"]
    total_slots = slot_info["total_slots"]
    duration_slots = slot_info["duration_slots"]
    token_block_reasons = slot_info.get("token_block_reasons", {})
    wh_slots = slot_info.get("wh_slots", set())

    def _entry(s0, s1, reason, kind):
        st = _slot_index_to_time(s0, origin)
        en = _slot_index_to_time(s1, origin)
        return {
            "start": st.strftime("%Y-%m-%dT%H:%M"),
            "end": en.strftime("%Y-%m-%dT%H:%M"),
            "day_label": st.strftime("%A %b %d"),
            "time_label": f"{st.strftime('%-I:%M %p')}-{en.strftime('%-I:%M %p')} MT",
            "reason": reason,
            "type": kind,
        }

    excluded = []

    # (1) Hard token blocks INSIDE a working-hours window -- the "override this
    # rule" cases (lunch, Friday). Merge consecutive same-reason runs per day.
    blocked = sorted((s, token_block_reasons[s]) for s in token_block_reasons
                     if s in wh_slots)
    i = 0
    while i < len(blocked) and len(excluded) < 8:
        s0, reason = blocked[i]
        j = i
        while (j + 1 < len(blocked)
               and blocked[j + 1][0] == blocked[j][0] + 1
               and blocked[j + 1][1] == reason):
            j += 1
        excluded.append(_entry(s0, blocked[j][0] + 1, reason, "blocked"))
        i = j + 1

    # (2) Soft-demoted feasible starts (token p1/p2 penalty), not proposed.
    demoted = []
    for s in range(total_slots - duration_slots + 1):
        if s in proposed_starts or s in hard_blocked:
            continue
        if any((s + d) in hard_blocked for d in range(duration_slots)):
            continue
        tok_costs = {k: v for k, v in soft_costs.get(s, {}).items()
                     if _is_token_event(k) and v > 0}
        if tok_costs:
            demoted.append((sum(tok_costs.values()), s, tok_costs))
    demoted.sort(key=lambda t: (t[0], t[1]))
    for _cost, s, tok_costs in demoted[:5]:
        excluded.append(_entry(s, s + duration_slots,
                               "; ".join(sorted(tok_costs)), "demoted"))

    return excluded


def cmd_propose_meeting(args):
    """Find ranked meeting times in one call (the only solver entry point).

    Builds the spec from args, runs the primary solver pass, runs the
    relaxation pass (movable 1:1s) if the primary returned fewer
    options than asked, formats results, returns JSON.
    """
    attendees = json.loads(args.attendees)
    constraints = json.loads(args.constraints) if args.constraints else []

    # Build spec directly
    today = date.today()
    # Allow earliest_date override on Namespace (for hermetic tests).
    raw_earliest = getattr(args, "earliest_date", None)
    if raw_earliest:
        earliest = date.fromisoformat(raw_earliest)
    else:
        earliest = today + timedelta(days=1)
        while earliest.weekday() >= 5:
            earliest += timedelta(days=1)
    raw_latest = getattr(args, "latest_date", None)
    if raw_latest:
        latest = date.fromisoformat(raw_latest)
    else:
        latest = earliest + timedelta(days=args.days_ahead)

    spec = {
        "duration_minutes": args.duration,
        "earliest_date": earliest.isoformat(),
        "latest_date": latest.isoformat(),
        "num_options": args.num_options,
        "attendees": attendees,
        "soft_constraints": constraints,
        "ignore_events": sorted(_ignore_ids(args)),
    }

    solve_start = time.monotonic()
    weights = _load_solver_weights()
    num_options = spec.get("num_options", 5)

    # Scheduling preferences (working hours, no-meeting blocks, time-of-day
    # prefs) live as tokened recurring events on the EA-Managed calendar. Fetch
    # them once; there is NO config fallback, so no working-hours (wh) window
    # means refuse rather than guess.
    token_constraints = _load_token_constraints(earliest, latest)
    if not token_constraints.get("has_wh"):
        print(json.dumps({
            "status": "error",
            "message": (
                "No working-hours (wh) events found on the EA-Managed calendar "
                "for this window (or it could not be read). The user's scheduling "
                "preferences now live there as tokened events; cannot propose "
                "times without a wh window. Check the EA-Managed calendar."),
            "options": [],
            "conditional_options": [],
            "calendar_fetch_errors": calendar_fetch_errors(),
        }, indent=2))
        return

    # --- First pass: all hard constraints enforced ---
    slot_info, hard_blocked, soft_costs, organizer_events, infeasible_days = \
        _build_candidate_slots(spec, weights, token_constraints,
                               relaxations=None)

    # An open proposal lists separate windows. Offered times (an attendee
    # with stated availability, a poll) and an enumeration (ENUMERATE_FROM
    # options or more) keep every quarter-hour start: the user picks from
    # the sender's window, and the lane reads absence from that list as a
    # conflict.
    distinct = (num_options < ENUMERATE_FROM
                and not any(a.get("availability") for a in spec.get("attendees", [])))
    primary_results = _solve_one_pass(
        slot_info, hard_blocked, soft_costs,
        excluded=set(), num_options=num_options, distinct=distinct,
    )

    options = []
    for i, r in enumerate(primary_results):
        options.append(_format_solver_result(
            r, slot_info, rank=i + 1,
            organizer_events=organizer_events,
        ))

    # --- Second pass (relaxation) if the primary returned fewer options
    # than asked. Currently only `movable_1on1` is tried. ---
    conditional_options = []
    if len(primary_results) < num_options:
        primary_excluded = {r["slot_index"] for r in primary_results}
        remaining_needed = num_options - len(primary_results)
        for relaxation_set in [["movable_1on1"]]:
            if len(conditional_options) >= remaining_needed:
                break
            r_slot_info, r_hard_blocked, r_soft_costs, _, _ = \
                _build_candidate_slots(spec, weights, token_constraints,
                                       relaxations=relaxation_set)
            new_candidates = hard_blocked - r_hard_blocked
            if not new_candidates:
                continue
            r_results = _solve_one_pass(
                r_slot_info, r_hard_blocked, r_soft_costs,
                excluded=primary_excluded,
                num_options=remaining_needed - len(conditional_options),
                distinct=distinct,
            )
            for r in r_results:
                cond_label, relaxation_keys = _build_conditional_label(
                    r_slot_info, r["slot_index"])
                if cond_label is None:
                    cond_label = "Relax constraints"
                    relaxation_keys = relaxation_set
                conditional_options.append(_format_solver_result(
                    r, r_slot_info,
                    rank=len(conditional_options) + 1,
                    organizer_events=organizer_events,
                    conditional=cond_label,
                    relaxations=relaxation_keys,
                ))

    solve_time_ms = int((time.monotonic() - solve_start) * 1000)

    attendees_checked = {"organizer": "merged (Outlook + Google)"}
    unchecked = {e.lower(): why for e, why in (slot_info.get("unchecked_attendees") or {}).items()}
    for a in spec.get("attendees", []):
        if a.get("type") == "internal" and a["email"].lower() in unchecked:
            attendees_checked[a["email"]] = f"NOT CHECKED: {unchecked[a['email'].lower()]}"
        elif a.get("type") == "internal":
            attendees_checked[a["email"]] = _schedule_source()
        elif a.get("availability"):
            attendees_checked[a["email"]] = "stated availability"
        else:
            attendees_checked[a["email"]] = (
                f"type={a.get('type', 'unknown')} (no calendar data)"
            )

    proposed_starts = {r["slot_index"] for r in primary_results}
    excluded = _build_excluded(slot_info, hard_blocked, soft_costs, proposed_starts)

    # A calendar account that failed to read contributed no busy time, so
    # every option below may sit on top of a real meeting. Warn on the
    # option itself, not only in a field a reader can skip: the resident is
    # told to TRUST the solver output verbatim.
    fetch_errors = calendar_fetch_errors()
    fetch_warning = _fetch_error_warning(fetch_errors)
    warnings = [fetch_warning] if fetch_warning else []
    if fetch_warning:
        for opt in options + conditional_options:
            opt["notes"] = ". ".join(
                p for p in (opt.get("notes", ""), f"WARNING: {fetch_warning}") if p)
    not_read = slot_info.get("unchecked_attendees") or {}
    if not_read:
        # Treated as unknown, not free: say so beside every option.
        warnings.append(
            "Could not read the calendar of " + ", ".join(sorted(not_read))
            + "; these options do not account for them. Ask them, or treat them as external.")

    # Every option carries an id, a label and the attendees' clocks, so a
    # reply quotes solver lines and never writes a clock time itself.
    label_options(options, attendees, user_zone=USER_TZ.key)
    label_options(conditional_options, attendees, prefix="c", user_zone=USER_TZ.key)

    output = {
        "status": "ok" if options else "no_options",
        "options": options,
        "lines": [o["line"] for o in options],
        "conditional_options": conditional_options,
        # Calendars that could not be READ this run, and the plain-language
        # consequence. Non-empty => the options are not authoritative.
        "calendar_fetch_errors": fetch_errors,
        "warnings": warnings,
        # Why some times were not offered / ranked lower — token preferences the
        # user can override (see skills/scheduling-rules.md).
        "excluded": excluded,
        "infeasible_days": infeasible_days,
        # All-day events across the horizon, each with `classification` (the
        # engine's read) and `effect` (what it applied: blocked / penalized /
        # none). YOU make the final block-vs-informational call on anything
        # classified "unknown" -- see skills/scheduling-rules.md.
        "all_day_events": slot_info.get("all_day_events", []),
        "attendees_checked": attendees_checked,
        # The user's own events treated as free this solve (--ignore-event),
        # and any id that matched nothing.
        "ignored_events": slot_info.get("ignored_events", []),
        "ignore_not_found": slot_info.get("ignore_not_found", []),
        # GROUND TRUTH: the day/time preference events actually read from the
        # EA-Managed calendar for this horizon. This is the ONLY source of
        # scheduling rules. A token with an empty list has NO events -> that
        # rule does not exist right now. Do NOT apply a remembered preference
        # (e.g. "Friday is usually off") that is not present here.
        "calendar_preferences": {
            "source": "EA-Managed calendar (live read)",
            "horizon": f"{earliest.isoformat()}..{latest.isoformat()}",
            "tokens": summarize_token_constraints(token_constraints),
        },
        "solver_time_ms": solve_time_ms,
    }

    print(json.dumps(output, indent=2))


# --- Reschedule brief ---------------------------------------------------------
# One call for "can we move our meeting?": the thread, the meeting(s) on the
# calendar with that sender (with the Outlook event ids a reschedule needs),
# the sender's history with the user (their usual weekday and hour), solver
# options ranked by that history, the sender's own proposed times validated,
# and a recommendation the proactive lane can act on: stage the Reschedule
# button for the obvious slot, or offer options.

def _user_email() -> str:
    try:
        prof = json.loads((EA_DIR / "config" / "user-profile.json").read_text())
        return str(prof.get("email", "")).strip().lower()
    except Exception:
        return ""


def _attendee_addrs(event: dict) -> set:
    out = set()
    for a in event.get("attendees") or []:
        addr = ((a.get("emailAddress") or {}).get("address") or "").strip().lower()
        if addr:
            out.add(addr)
    org = ((event.get("organizer") or {}).get("emailAddress") or {}).get("address", "")
    if org:
        out.add(org.strip().lower())
    return out


def events_with_attendee(events: list, addr: str) -> list:
    """Graph events where `addr` is an attendee or the organiser, oldest first.
    Cancelled events are skipped."""
    a = (addr or "").strip().lower()
    if not a:
        return []
    out = [e for e in events if a in _attendee_addrs(e) and not e.get("isCancelled")]
    out.sort(key=lambda e: (e.get("start") or {}).get("dateTime", ""))
    return out


_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def history_profile(events: list, addr: str, now: datetime | None = None) -> dict:
    """What meeting this person usually means: how often, which weekdays,
    the median start (local), the typical length, the last one. Built from
    past events that carry them as attendee. An empty history returns
    count 0 and no usual values."""
    now = now or datetime.now(timezone.utc)
    past = []
    for e in events_with_attendee(events, addr):
        try:
            st = _parse_dt((e.get("start") or {}).get("dateTime", ""))
            en = _parse_dt((e.get("end") or {}).get("dateTime", ""))
        except Exception:
            continue
        if st >= now or e.get("isAllDay"):
            continue
        past.append((st.astimezone(USER_TZ), en.astimezone(USER_TZ), e.get("subject", "")))
    if not past:
        return {"count": 0, "weekdays": {}, "usual_weekday": "", "usual_start": "",
                "median_start_min": None, "usual_duration_min": None, "last_met": "",
                "subjects": []}
    weekdays: dict = {}
    for st, _en, _s in past:
        weekdays[_WEEKDAYS[st.weekday()]] = weekdays.get(_WEEKDAYS[st.weekday()], 0) + 1
    starts = sorted(st.hour * 60 + st.minute for st, _e, _s in past)
    durations = sorted(int((en - st).total_seconds() // 60) for st, en, _s in past)
    med_start = starts[len(starts) // 2]
    med_dur = durations[len(durations) // 2]
    top_day = sorted(weekdays.items(), key=lambda kv: (-kv[1], _WEEKDAYS.index(kv[0])))[0][0]
    subjects = []
    for _st, _en, subj in past[-5:]:
        if subj and subj not in subjects:
            subjects.append(subj)
    return {"count": len(past),
            "weekdays": dict(sorted(weekdays.items(), key=lambda kv: -kv[1])),
            "usual_weekday": top_day,
            "usual_start": _hhmm(med_start),
            "median_start_min": med_start,
            "usual_duration_min": med_dur,
            "last_met": past[-1][0].isoformat(timespec="minutes"),
            "subjects": subjects}


def history_fit(start_iso: str, profile: dict) -> float:
    """0 to 1: how much a candidate start looks like this person's usual
    slot. Half the score is the weekday (full for their top weekday, part
    for any weekday they have met on), half is closeness to their median
    start hour (zero at three hours off). No history reads as 0.5, a
    neutral value that neither promotes nor demotes an option."""
    if not profile or not profile.get("count"):
        return 0.5
    try:
        st = _parse_user_dt(start_iso).astimezone(USER_TZ)
    except Exception:
        return 0.0
    day = _WEEKDAYS[st.weekday()]
    weekdays = profile.get("weekdays") or {}
    if day == profile.get("usual_weekday"):
        day_score = 0.5
    elif day in weekdays:
        day_score = 0.3
    else:
        day_score = 0.0
    med = profile.get("median_start_min")
    if med is None:
        hour_score = 0.25
    else:
        delta = abs(st.hour * 60 + st.minute - med)
        hour_score = 0.5 * max(0.0, 1.0 - delta / 180.0)
    return round(day_score + hour_score, 3)


def rank_options_by_history(options: list, profile: dict) -> list:
    """The solver's options with `history_fit` added, best fit first and the
    solver's own rank as the tie-break. The solver's cost stays on each
    option, so a reader can see both orderings."""
    ranked = []
    for o in options or []:
        o = dict(o)
        o["history_fit"] = history_fit(o.get("start", ""), profile)
        ranked.append(o)
    ranked.sort(key=lambda o: (-o["history_fit"], o.get("rank", 99)))
    return ranked


def recommend_reschedule(meetings: list, ranked_options: list, validated: list,
                         profile: dict, duration_min: int,
                         fit_threshold: float = 0.75) -> dict:
    """The lane's decision, made in code so it is the same every time.

    stage_reschedule when exactly one upcoming meeting with the sender is
    on the calendar AND either (a) a time the sender proposed validates
    clean, or (b) the best-fit solver option is unconditional and matches
    their usual slot at or above `fit_threshold`. Everything else is
    offer_options with the reason, so the lane proposes instead of guessing.
    """
    if not meetings:
        return {"action": "offer_options",
                "reason": "no upcoming meeting with this sender is on the calendar; "
                          "propose times and ask which meeting they mean"}
    if len(meetings) > 1:
        return {"action": "offer_options",
                "reason": f"{len(meetings)} upcoming meetings with this sender; "
                          f"say which one moves before staging anything",
                "meetings": [m.get("id") for m in meetings]}
    meeting = meetings[0]
    clean = [v for v in validated if v.get("valid") is True]
    clean.sort(key=lambda v: -history_fit(v.get("time", ""), profile))
    if clean:
        start = _parse_user_dt(clean[0]["time"]).astimezone(USER_TZ)
        end = start + timedelta(minutes=duration_min)
        return {"action": "stage_reschedule",
                "basis": "the sender proposed this time and the calendar is clear",
                "event_id": meeting["id"], "subject": meeting.get("subject", ""),
                "original_start": meeting.get("start", ""),
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "history_fit": history_fit(clean[0]["time"], profile)}
    if validated and not clean:
        blockers = [{"time": v.get("time"), "issues": v.get("issues", [])} for v in validated]
    else:
        blockers = []
    top = ranked_options[0] if ranked_options else None
    if (top and not top.get("conditional") and top.get("history_fit", 0) >= fit_threshold):
        return {"action": "stage_reschedule",
                "basis": f"the best solver option is their usual slot "
                         f"({profile.get('usual_weekday')} around {profile.get('usual_start')}, "
                         f"fit {top['history_fit']})",
                "event_id": meeting["id"], "subject": meeting.get("subject", ""),
                "original_start": meeting.get("start", ""),
                "start": top.get("start", ""), "end": top.get("end", ""),
                "history_fit": top.get("history_fit"), "proposed_blocked": blockers}
    reason = ("the sender's proposed time(s) do not validate" if blockers else
              "no option stands out as their usual slot" if profile.get("count") else
              "no history with this sender to lean on")
    return {"action": "offer_options", "reason": reason,
            "event_id": meeting["id"], "subject": meeting.get("subject", ""),
            "original_start": meeting.get("start", ""),
            "options": ranked_options[:3], "proposed_blocked": blockers}


def _compact_event(e: dict) -> dict:
    try:
        st = _parse_dt((e.get("start") or {}).get("dateTime", "")).astimezone(USER_TZ)
        en = _parse_dt((e.get("end") or {}).get("dateTime", "")).astimezone(USER_TZ)
        dur = int((en - st).total_seconds() // 60)
        st_s, en_s = st.isoformat(timespec="minutes"), en.isoformat(timespec="minutes")
    except Exception:
        st_s = (e.get("start") or {}).get("dateTime", "")
        en_s = (e.get("end") or {}).get("dateTime", "")
        dur = None
    return {"id": e.get("id", ""), "subject": e.get("subject", ""),
            "start": st_s, "end": en_s, "duration_min": dur,
            "type": e.get("type", ""), "series_master_id": e.get("seriesMasterId", ""),
            "attendees": sorted(_attendee_addrs(e)),
            "location": ((e.get("location") or {}).get("displayName") or ""),
            "show_as": e.get("showAs", "")}


def _strip_html(text: str) -> str:
    text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _run_self(verb: str, *argv: str) -> dict:
    """Run one of this module's own verbs in a child process and parse its
    JSON (their entry points print rather than return)."""
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), verb, *argv],
                       capture_output=True, text=True, timeout=180)
    out = r.stdout.strip()
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else {"error": out or r.stderr[-300:]}
    except Exception:
        return {"error": (r.stderr or out)[-300:]}


def cmd_reschedule_brief(args):
    """Everything the scheduling lane needs to answer a reschedule email in
    one call. Reads only: the thread, both calendars (through the solver's
    own verbs), the sender's history. The move itself is staged by the
    caller for the Reschedule button."""
    user = _user_email()
    msgs: list = []
    if args.conversation_id:
        try:
            msgs, _trunc = graph.get_conversation_messages(args.conversation_id, top=30)
        except Exception as e:
            print(json.dumps({"error": f"thread read failed: {e}"}))
            return
    elif args.message_id:
        try:
            msgs = [graph.get_message_full(args.message_id)]
        except Exception as e:
            print(json.dumps({"error": f"message read failed: {e}"}))
            return
    msgs = sorted(msgs, key=lambda m: m.get("receivedDateTime", ""))
    inbound = [m for m in msgs
               if ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower() != user]
    latest = inbound[-1] if inbound else (msgs[-1] if msgs else {})
    sender = (args.sender or ((latest.get("from") or {}).get("emailAddress") or {}).get("address", "")).strip().lower()
    sender_name = ((latest.get("from") or {}).get("emailAddress") or {}).get("name", "")
    if not sender:
        print(json.dumps({"error": "no sender: pass --sender or a conversation with an inbound message"}))
        return

    now = datetime.now(timezone.utc)
    try:
        past = graph.get_calendar_view(now - timedelta(days=args.history_days), now, args.history_days)
    except Exception as e:
        past = []
        print(f"history fetch failed: {e}", file=sys.stderr)
    try:
        future = graph.get_calendar_view(now, now + timedelta(days=45), 45)
    except Exception as e:
        future = []
        print(f"calendar fetch failed: {e}", file=sys.stderr)
    meetings = [_compact_event(e) for e in events_with_attendee(future, sender)
                if e.get("type") != "seriesMaster"]
    profile = history_profile(past, sender, now)
    duration = args.duration or (meetings[0]["duration_min"] if len(meetings) == 1 and meetings[0].get("duration_min") else None) \
        or profile.get("usual_duration_min") or 30

    domains = _internal_domains()          # ("@example.edu",)
    a_type = "internal" if any(sender.endswith(d.lower()) for d in domains) else "external"
    prop = _run_self("propose-meeting", "--duration", str(duration), "--days-ahead", str(args.days_ahead),
                     "--num-options", "6",
                     "--attendees", json.dumps([{"email": sender, "name": sender_name, "type": a_type}]))
    ranked = rank_options_by_history(prop.get("options", []), profile)

    validated = []
    for t in (json.loads(args.times) if args.times else []):
        v = _run_self("validate-draft", "--times", json.dumps([t]), "--duration", str(duration))
        validated.append({"time": t, "valid": v.get("valid"), "issues": v.get("issues", []),
                          "history_fit": history_fit(t, profile)})

    rec = recommend_reschedule(meetings, ranked, validated, profile, duration)
    thread = []
    for m in msgs[-3:]:
        fr = (m.get("from") or {}).get("emailAddress") or {}
        thread.append({"from": fr.get("address", ""), "name": fr.get("name", ""),
                       "received": m.get("receivedDateTime", ""),
                       "preview": (m.get("bodyPreview") or "")[:400]})
    latest_text = _strip_html(((latest.get("body") or {}).get("content") or latest.get("bodyPreview") or ""))[:1500]
    print(json.dumps({
        "sender": sender, "sender_name": sender_name, "attendee_type": a_type,
        "duration_min": duration,
        "thread": thread, "latest_inbound": latest_text,
        "meetings": meetings, "history": profile,
        "options": ranked, "solver_status": prop.get("status", prop.get("error", "")),
        "proposed_times": validated,
        "recommendation": rec,
        "how_to_stage": ("write ~/.ea/state/pending-event-update.json with event_id, subject, "
                         "original_start, start, end and thread_ts; the bot posts the Reschedule button"),
    }, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    parser = argparse.ArgumentParser(description="Scheduling engine", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # validate-draft
    p = sub.add_parser("validate-draft", help="Pre-send validation", allow_abbrev=False)
    p.add_argument("--times", required=True, help='JSON array of ISO datetimes')
    p.add_argument("--duration", type=int, default=30, help="Meeting duration (minutes)")
    p.add_argument("--ignore-event", action="append", default=[], metavar="ID",
                   help="Treat this event of the user's own as free (repeatable)")
    p.set_defaults(func=cmd_validate_draft)

    # check-attendees: are THESE people free in THIS window?
    p = sub.add_parser("check-attendees",
                       help="Free/busy for named people over one window, plus the "
                            "user's own events in it (with ids)",
                       allow_abbrev=False)
    p.add_argument("--attendees", required=True,
                   help='Comma-separated addresses, or a JSON array of addresses '
                        'or {"email": ...} objects')
    p.add_argument("--start", required=True, help="Window start (ISO; naive = local)")
    p.add_argument("--end", required=True, help="Window end (ISO; naive = local)")
    p.add_argument("--ignore-event", action="append", default=[], metavar="ID",
                   help="Leave this event of the user's own out of organizer_events")
    p.set_defaults(func=cmd_check_attendees)

    # log-decision
    p = sub.add_parser("log-decision", help="Record a scheduling decision", allow_abbrev=False)
    p.add_argument("--event", required=True, help="Event that was affected")
    p.add_argument("--action", required=True, choices=["moved", "kept", "cancelled",
                   "overrode_lunch", "overrode_friday", "overrode_hours"],
                   help="What happened")
    p.add_argument("--for", dest="moved_for", default=None, help="What it was moved/overridden for")
    p.add_argument("--comment", default=None, help="User's reason or comment")
    p.add_argument("--override", default=None, choices=["lunch", "friday", "after_hours"],
                   help="Which preference was overridden")
    p.add_argument("--contact-type", default=None, help="Type: external, dean, student, vip, etc.")
    p.add_argument("--contact", default=None, help="Contact email or name")
    p.set_defaults(func=cmd_log_decision)

    # review-patterns
    p = sub.add_parser("review-patterns", help="Analyze decisions and suggest preference updates", allow_abbrev=False)
    p.set_defaults(func=cmd_review_patterns)

    # propose-meeting: build the spec, add attendees and solve in one call
    p = sub.add_parser("propose-meeting",
                       help="Find meeting times (single command, no intermediate steps)",
                       allow_abbrev=False)
    p.add_argument("--duration", type=int, default=30, help="Meeting duration in minutes")
    p.add_argument("--days-ahead", type=int, default=7, help="Days to look ahead")
    p.add_argument("--earliest-date", default=None, metavar="YYYY-MM-DD",
                   help="First day of the window (default: the next weekday)")
    p.add_argument("--latest-date", default=None, metavar="YYYY-MM-DD",
                   help="Last day of the window (default: earliest + --days-ahead)")
    p.add_argument("--num-options", type=int, default=5,
                   help="Number of options to return. An open proposal gets separate windows; "
                        "offered times (attendee availability) and a poll reply "
                        f"(--num-options {ENUMERATE_FROM} or more) get every feasible quarter-hour start")
    p.add_argument("--attendees", required=True,
                   help='JSON array: [{"email":"x@y","name":"X","type":"internal",'
                        '"timezone":"Europe/London","hours":["09:00","17:00"],'
                        '"excluded_days":["YYYY-MM-DD"],'
                        '"availability":[{"day":"YYYY-MM-DD","start":"HH:MM","end":"HH:MM"}]}]. '
                        'excluded_days block whole days and compose with the zone hours; '
                        'availability replaces them. "availability_complete":true (a booking '
                        "link's slots) blocks every day the windows leave out, internal too")
    p.add_argument("--constraints", default="[]",
                   help='JSON array of soft constraints (optional)')
    p.add_argument("--ignore-event", action="append", default=[], metavar="ID",
                   help="Treat this event of the user's own as free (repeatable): "
                        "the slot it holds becomes a candidate")
    p.set_defaults(func=cmd_propose_meeting)

    # reschedule-brief: one call for a "can we move our meeting?" email
    p = sub.add_parser("reschedule-brief",
                       help="Thread + the meeting(s) with the sender + their history + "
                            "solver options + a recommendation, in one call",
                       allow_abbrev=False)
    p.add_argument("--conversation-id", default="", help="Outlook conversation id of the request")
    p.add_argument("--message-id", default="", help="Outlook message id (when no conversation id)")
    p.add_argument("--sender", default="", help="Sender address override")
    p.add_argument("--times", default="", help="JSON array of times the sender proposed (ISO, local)")
    p.add_argument("--duration", type=int, default=0,
                   help="Meeting length in minutes (default: the meeting's, else their usual, else 30)")
    p.add_argument("--days-ahead", type=int, default=14)
    p.add_argument("--history-days", type=int, default=180)
    p.set_defaults(func=cmd_reschedule_brief)
    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mm_child.check_child_parse(parser, args)  # the verb the mediator checked
    args.func(args)
