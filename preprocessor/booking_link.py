#!/usr/bin/env python3
"""
booking_link.py — Read a cal.com or Calendly booking link's open times
for the solver.

A colleague who sends a booking link ("book a time on my cal.com", a
calendly.com page) has published their free time. This tool reads it and
nothing else: the open slots of one event type, through the public API the
booking page itself calls, with no account and no login. The meeting is
never booked through the link. The user's own
scheduler makes it: the open times go to scheduling_engine.py
propose-meeting as that person's stated availability, and the chosen slot
becomes an Outlook invite staged in pending-event.json behind the Create
button.

Read only by construction: the module sends GET requests to two fixed
hosts (api.cal.com, calendly.com/api/booking) and has no booking verb. A
link supplies only a username, an event slug and a duration, each matched
against a strict pattern, so a link cannot point a request anywhere else.
Outlook Safe Links and Google redirect wrappers are unwrapped first. A
Calendly meeting poll (calendly.com/d/...) is a vote, not a booking page:
poll_filler.py reads it.

Usage:
    python3 booking_link.py read --url <link> [--duration MIN] [--days-ahead N]
    python3 booking_link.py propose --attendees '<JSON>' [--duration MIN]
        [--days-ahead N] [--num-options N] [--constraints '<JSON>']
        [--ignore-event ID ...]

propose takes propose-meeting's attendee list with one more key per
attendee, "booking_link": that person's link. Each link's open slots become
the attendee's "availability". An attendee at the user's own domain is
"internal", so the solver reads their free/busy as well. The output is
propose-meeting's, plus a "booking_links" block saying what each link
offered. Without --duration the meeting takes the first link's length.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outbound_log  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
ENGINE = Path(__file__).resolve().parent / "scheduling_engine.py"

API = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
EVENT_TYPES_API_VERSION = "2024-06-14"
TIMEOUT_S = 20

CALENDLY_API = "https://calendly.com/api/booking"
# Calendly answers "this calendar is currently unavailable" for a range
# longer than about five weeks; the page itself asks a month at a time.
CALENDLY_RANGE_DAYS = 28

LINK_HOSTS = {"cal.com": "cal.com", "www.cal.com": "cal.com",
              "app.cal.com": "cal.com",
              "calendly.com": "calendly.com", "www.calendly.com": "calendly.com"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GOOGLE_HOST_RE = re.compile(r"^(?:www\.)?google\.[a-z]{2,3}(?:\.[a-z]{2})?$")

NOTE = ("Read only. These are the times each link offered when read; nothing "
        "is held or booked. Make the meeting with the user's own invite "
        "(pending-event.json, Create button), never through the link.")


class LinkError(Exception):
    """A link this tool cannot read, or an answer the platform refused."""


def _user_tz() -> ZoneInfo:
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return user_tz.zone(EA_DIR)


def _internal_domain() -> str:
    """The user's own mail domain: an attendee there is 'internal' to the
    solver (the same rule as scheduling_engine._internal_domains). '' when
    the profile names none."""
    try:
        import institution
    except ImportError:
        from preprocessor import institution
    return institution.own_suffix()


def solver_window(days_ahead: int, today: date | None = None) -> tuple[date, date]:
    """The days propose-meeting searches: from the next weekday after today
    through days_ahead days later (cmd_propose_meeting's own rule)."""
    earliest = (today or date.today()) + timedelta(days=1)
    while earliest.weekday() >= 5:
        earliest += timedelta(days=1)
    return earliest, earliest + timedelta(days=days_ahead)


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------

def unwrap(url: str) -> str:
    """The address behind an Outlook Safe Links or Google redirect wrapper,
    or the url itself."""
    url = (url or "").strip()
    for _ in range(3):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        qs = parse_qs(parts.query)
        if host.endswith(".safelinks.protection.outlook.com") and qs.get("url"):
            url = qs["url"][0].strip()
        elif _GOOGLE_HOST_RE.match(host) and parts.path == "/url" and qs.get("q"):
            url = qs["q"][0].strip()
        else:
            break
    return url


def parse_link(url: str) -> dict:
    """{"platform", "url", "username", "event_slug", "duration"} for a
    cal.com or Calendly link. platform is "cal.com" or "calendly.com".
    event_slug is None for a profile link (cal.com/<user>), which names a
    person and not an event. Raises LinkError for anything else."""
    url = unwrap(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in LINK_HOSTS:
        raise LinkError(f"not a cal.com or Calendly link: {url!r}")
    platform = LINK_HOSTS[host]
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        raise LinkError(f"the link names no person: {url!r}")
    if platform == "cal.com" and segs[0] == "team":
        raise LinkError("cal.com team links are not supported; ask for a "
                        "person's own link or for times")
    if platform == "calendly.com" and segs[0] == "d":
        raise LinkError("a calendly.com/d/ link is a meeting poll or a "
                        "one-off link, not a booking page; read it with "
                        "poll_filler.py read-poll")
    if len(segs) > 2 or not all(_NAME_RE.match(s) for s in segs):
        raise LinkError(f"not a booking link ({platform}/<user>/<event>): {url!r}")
    duration = None
    raw = (parse_qs(parts.query).get("duration") or [""])[0]
    if platform == "cal.com" and raw.isdigit() and 5 <= int(raw) <= 480:
        duration = int(raw)
    username = segs[0]
    slug = segs[1] if len(segs) == 2 else None
    clean = f"https://{platform}/{username}" + (f"/{slug}" if slug else "")
    return {"platform": platform, "url": clean, "username": username,
            "event_slug": slug, "duration": duration}


# ---------------------------------------------------------------------------
# cal.com public API (GET only)
# ---------------------------------------------------------------------------

def _get(path: str, params: dict, version: str):
    # Every request goes on the reviewed outbound log (outbound_log).
    url = API + path + (("?" + urlencode(params)) if params else "")
    try:
        r = requests.get(API + path, params=params, timeout=TIMEOUT_S,
                         headers={"cal-api-version": version,
                                  "Accept": "application/json"})
    except requests.RequestException as e:
        outbound_log.note("error", "booking_link", url=url, method="GET",
                          reason=str(e)[:200])
        raise LinkError(f"cal.com did not answer: {e}")
    outbound_log.note("fetch", "booking_link", url=url, method="GET",
                      status=r.status_code)
    try:
        body = r.json()
    except ValueError:
        raise LinkError(f"cal.com answered HTTP {r.status_code} with no JSON")
    if r.status_code != 200 or body.get("status") != "success":
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        raise LinkError(f"cal.com refused the request: "
                        f"{err.get('message') or 'HTTP %d' % r.status_code}")
    return body.get("data")


def fetch_slots(username: str, slug: str, start: date, end: date, tz: ZoneInfo,
                duration: int | None) -> list[tuple[datetime, datetime]]:
    """The event's open slots from start through end (both whole days), as
    (start, end) pairs. duration asks cal.com for slots of that length."""
    params = {"username": username, "eventTypeSlug": slug,
              "start": start.isoformat(), "end": end.isoformat(),
              "timeZone": str(tz), "format": "range"}
    if duration:
        params["duration"] = duration
    data = _get("/slots", params, SLOTS_API_VERSION) or {}
    out = []
    for slots in data.values():
        for s in slots or []:
            try:
                out.append((datetime.fromisoformat(s["start"]),
                            datetime.fromisoformat(s["end"])))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(out)


def event_info(username: str, slug: str) -> dict:
    """Title, host and length of a listed event type. A hidden event type
    (a VIP link) is not listed, so this is {} for one."""
    try:
        data = _get("/event-types", {"username": username, "eventSlug": slug},
                    EVENT_TYPES_API_VERSION)
    except LinkError:
        return {}
    for e in data or []:
        if e.get("slug") == slug:
            users = e.get("users") or [{}]
            return {"title": e.get("title"), "host": users[0].get("name"),
                    "event_length_minutes": e.get("lengthInMinutes"),
                    "length_options": e.get("lengthInMinutesOptions")}
    return {}


def public_events(username: str) -> list[dict]:
    """The person's listed event types, for a profile link."""
    data = _get("/event-types", {"username": username}, EVENT_TYPES_API_VERSION)
    return [{"slug": e.get("slug"), "title": e.get("title"),
             "length_minutes": e.get("lengthInMinutes")}
            for e in data or [] if not e.get("hidden")]


# ---------------------------------------------------------------------------
# Calendly public booking API (GET only), the calls its booking page makes
# ---------------------------------------------------------------------------

def _calendly_get(path: str, params: dict | None = None):
    url = CALENDLY_API + path + (("?" + urlencode(params)) if params else "")
    try:
        r = requests.get(CALENDLY_API + path, params=params, timeout=TIMEOUT_S,
                         headers={"Accept": "application/json"})
    except requests.RequestException as e:
        outbound_log.note("error", "booking_link", url=url, method="GET",
                          reason=str(e)[:200])
        raise LinkError(f"Calendly did not answer: {e}")
    outbound_log.note("fetch", "booking_link", url=url, method="GET",
                      status=r.status_code)
    if r.status_code == 404:
        raise LinkError("Calendly has no such booking page")
    try:
        body = r.json()
    except ValueError:
        raise LinkError(f"Calendly answered HTTP {r.status_code} with no JSON")
    if r.status_code != 200:
        fail = body.get("failure") if isinstance(body, dict) else None
        msg = fail.get("error") if isinstance(fail, dict) else None
        raise LinkError(f"Calendly refused the request: "
                        f"{msg or 'HTTP %d' % r.status_code}")
    return body


def calendly_event(profile: str, slug: str) -> dict:
    """The event type behind calendly.com/<profile>/<slug>: its uuid, title,
    host and length. A meeting poll is refused (poll_filler reads those)."""
    et = _calendly_get("/event_types/lookup",
                       {"event_type_slug": slug, "profile_slug": profile}) or {}
    if et.get("booking_method") == "poll":
        raise LinkError("this Calendly link is a meeting poll, not a booking "
                        "page; read it with poll_filler.py read-poll")
    if not et.get("uuid"):
        raise LinkError(f"Calendly named no event for {profile}/{slug}")
    return {"uuid": et["uuid"], "title": et.get("name"),
            "host": (et.get("profile") or {}).get("name") or et.get("publishers_list"),
            "event_length_minutes": et.get("duration")}


def calendly_slots(uuid: str, length: int, start: date, end: date,
                   tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """The event's open slots from start through end, as (start, end)
    pairs. Calendly gives each spot's start; the event length gives its end."""
    out, day = [], max(start, date.today())
    while day <= end:
        last = min(end, day + timedelta(days=CALENDLY_RANGE_DAYS - 1))
        body = _calendly_get(f"/event_types/{uuid}/calendar/range",
                             {"timezone": str(tz), "diagnostics": "false",
                              "range_start": day.isoformat(),
                              "range_end": last.isoformat()}) or {}
        for d in body.get("days") or []:
            for spot in d.get("spots") or []:
                if spot.get("status") not in (None, "available"):
                    continue
                try:
                    s = datetime.fromisoformat(str(spot.get("start_time")))
                except ValueError:
                    continue
                if s.tzinfo is None:
                    s = s.replace(tzinfo=tz)
                out.append((s, s + timedelta(minutes=length)))
        day = last + timedelta(days=1)
    return sorted(out)


def calendly_events(profile: str) -> list[dict]:
    """The person's public event types, for a profile link."""
    data = _calendly_get(f"/profiles/{profile}/event_types") or []
    return [{"slug": e.get("slug"), "title": e.get("name"),
             "length_minutes": e.get("duration")}
            for e in data if isinstance(e, dict)]


# ---------------------------------------------------------------------------
# Slots to solver windows
# ---------------------------------------------------------------------------

def _quarter(dt: datetime, up: bool) -> datetime:
    """dt moved to a 15-minute boundary (the solver's grid): up for a
    start, down for an end, so a window never grows."""
    base = dt.replace(second=0, microsecond=0)
    over = base.minute % 15
    if up and (over or dt != base):
        base += timedelta(minutes=15 - over)
    elif not up:
        base -= timedelta(minutes=over)
    return base


def windows(slots: list[tuple[datetime, datetime]], tz: ZoneInfo) -> list[dict]:
    """Merge overlapping or touching slots into free windows in local time,
    one entry per day, in propose-meeting's availability shape
    {"day", "start", "end"}. Every slot is free, so their union is free."""
    spans = sorted((s.astimezone(tz), e.astimezone(tz)) for s, e in slots if e > s)
    merged: list[list[datetime]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        while s < e:
            midnight = datetime.combine(s.date() + timedelta(days=1), time(0), tzinfo=tz)
            piece_end = min(e, midnight)
            a = _quarter(s, up=True)
            # A window that runs to midnight stops at 23:45, so every end
            # stays a clock time of its own day.
            b = min(_quarter(piece_end, up=False), midnight - timedelta(minutes=15))
            if b > a:
                out.append({"day": a.date().isoformat(),
                            "start": a.strftime("%H:%M"),
                            "end": b.strftime("%H:%M")})
            s = piece_end
    return out


def read_link(url: str, start: date, end: date, tz: ZoneInfo,
              duration: int | None = None) -> dict:
    """Everything one link says about its owner's free time in [start, end]."""
    link = parse_link(url)
    calendly = link["platform"] == "calendly.com"
    if not link["event_slug"]:
        events = (calendly_events if calendly else public_events)(link["username"])
        raise LinkError(
            f"{link['url']} names a person, not an event. Their public "
            f"events: " + (", ".join(f"{link['url']}/{e['slug']}"
                                      + (f" ({e['length_minutes']} min)"
                                         if e["length_minutes"] else "")
                                      for e in events) or "none"))
    asked = duration or link["duration"]
    if calendly:
        info = calendly_event(link["username"], link["event_slug"])
        slots = calendly_slots(info["uuid"], info["event_length_minutes"] or 30,
                               start, end, tz)
    else:
        slots = fetch_slots(link["username"], link["event_slug"], start, end, tz, asked)
        info = event_info(link["username"], link["event_slug"])
    slot_minutes = (int((slots[0][1] - slots[0][0]).total_seconds() // 60)
                    if slots else None)
    return {
        "platform": link["platform"],
        "url": link["url"],
        "username": link["username"],
        "event_slug": link["event_slug"],
        "title": info.get("title"),
        "host": info.get("host"),
        "listed": bool(info),
        "event_length_minutes": info.get("event_length_minutes"),
        "slot_minutes": slot_minutes or asked or info.get("event_length_minutes"),
        "timezone": str(tz),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "open_slots": len(slots),
        "availability": windows(slots, tz),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def cmd_read(args) -> int:
    tz = _user_tz()
    start, end = solver_window(args.days_ahead)
    try:
        out = read_link(args.url, start, end, tz, args.duration)
    except LinkError as e:
        _print({"error": str(e)})
        return 1
    out["note"] = NOTE
    _print(out)
    return 0


def _summary(read: dict, who: str) -> dict:
    keep = ("platform", "url", "title", "host", "listed", "event_length_minutes",
            "slot_minutes", "open_slots", "from", "to", "availability")
    return {"attendee": who, **{k: read[k] for k in keep}}


def build_attendees(attendees: list, days_ahead: int, duration: int | None,
                    tz: ZoneInfo, domain: str) -> tuple[list, list, int]:
    """(solver attendees, link summaries, meeting duration). Each attendee
    with a booking_link gets the link's windows as its availability. The
    first link fixes the duration when none is given; every other link is
    read at that length."""
    start, end = solver_window(days_ahead)
    out, links = [], []
    for a in attendees:
        a = dict(a)
        url = a.pop("booking_link", None)
        email = (a.get("email") or "").strip()
        if "type" not in a:
            a["type"] = ("internal" if domain and email.lower().endswith(domain)
                         else "external")
        if url:
            read = read_link(url, start, end, tz, duration)
            if duration is None:
                duration = read["slot_minutes"] or 30
            a["email"] = email or f"{read['username']}@{read['platform']}"
            a["name"] = a.get("name") or read.get("host") or read["username"]
            if not read["availability"]:
                raise LinkError(
                    f"{read['url']} offers no open time from {start} to {end} "
                    f"at {duration} minutes; try a larger --days-ahead or ask "
                    f"{a['name']} for times")
            a["availability"] = read["availability"]
            links.append(_summary(read, a["name"]))
        out.append(a)
    if not links:
        raise LinkError("no attendee has a booking_link; use "
                        "scheduling_engine.py propose-meeting")
    return out, links, duration or 30


def _run_engine(argv: list[str]) -> dict:
    """propose-meeting in a child process. Run as the resident, the engine
    forwards itself to the mail mediator, which holds the calendar tokens."""
    r = subprocess.run([sys.executable, str(ENGINE), *argv],
                       capture_output=True, text=True, timeout=300)
    out = r.stdout.strip()
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {"error": (r.stderr or out)[-500:] or "the solver printed nothing"}


def cmd_propose(args) -> int:
    try:
        attendees = json.loads(args.attendees)
        if not isinstance(attendees, list):
            raise ValueError("--attendees must be a JSON array")
    except ValueError as e:
        _print({"error": f"--attendees: {e}"})
        return 1
    try:
        solver_atts, links, duration = build_attendees(
            attendees, args.days_ahead, args.duration, _user_tz(), _internal_domain())
    except LinkError as e:
        _print({"error": str(e)})
        return 1
    argv = ["propose-meeting", "--duration", str(duration),
            "--days-ahead", str(args.days_ahead),
            "--num-options", str(args.num_options),
            "--attendees", json.dumps(solver_atts)]
    if args.constraints:
        argv += ["--constraints", args.constraints]
    for event_id in args.ignore_event:
        argv += ["--ignore-event", event_id]
    result = _run_engine(argv)
    result["duration_minutes"] = duration
    result["booking_links"] = links
    result["note"] = NOTE
    _print(result)
    return 0 if "error" not in result else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read cal.com and Calendly booking links (read only) and propose times",
        allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read", help="One link's open windows, local time", allow_abbrev=False)
    p.add_argument("--url", required=True, help="cal.com/<user>/<event> or "
                   "calendly.com/<user>/<event> link "
                   "(Safe Links wrappers are fine)")
    p.add_argument("--duration", type=int, default=None,
                   help="Meeting length in minutes (default: the link's own)")
    p.add_argument("--days-ahead", type=int, default=14)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("propose", help="propose-meeting with booking links "
                       "as stated availability", allow_abbrev=False)
    p.add_argument("--attendees", required=True,
                   help='JSON array: propose-meeting attendees plus '
                        '"booking_link" on each person who sent one')
    p.add_argument("--duration", type=int, default=None,
                   help="Meeting length in minutes (default: the first link's)")
    p.add_argument("--days-ahead", type=int, default=14)
    p.add_argument("--num-options", type=int, default=5)
    p.add_argument("--constraints", default="",
                   help="JSON array of soft constraints (passed through)")
    p.add_argument("--ignore-event", action="append", default=[], metavar="ID",
                   help="Treat this event of the user's own as free (repeatable)")
    p.set_defaults(func=cmd_propose)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
