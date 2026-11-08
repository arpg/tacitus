#!/usr/bin/env python3
"""The words for a scheduling option, written by code.

Every clock time a reply names comes from here: the solver's option gets
an id, a label on the user's clock and the same window on each attendee's
clock, and a reply is assembled from those lines. A model picks ids and
writes the sentences around them; it never writes a time.

No I/O and no calendar. The user's zone is the one a caller passes
(`user_zone`, an IANA name); with none, user_tz resolves this install's
zone. The solver (scheduling_engine) calls label_options on its output;
the pipeline (sched_pipeline) calls render_body.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

# Zones whose clock the user's contacts name by region, not by city.
ZONE_SHORT = {
    "America/New_York": "ET", "America/Detroit": "ET", "US/Eastern": "ET",
    "America/Chicago": "CT", "US/Central": "CT",
    "America/Denver": "MT", "America/Boise": "MT", "US/Mountain": "MT",
    "America/Phoenix": "MST",
    "America/Los_Angeles": "PT", "US/Pacific": "PT",
    "America/Anchorage": "AKT", "Pacific/Honolulu": "HST",
}

# How a clock's zone label is read back (sched_agent.extract_times). A
# regional label maps to the region's zone, daylight time included, since
# mail writes "EST" in July as often as in January; inside the user's own
# region it is the user's zone.
REGION_ZONES = {
    "eastern": ("America/New_York", ("America/New_York", "America/Detroit", "US/Eastern")),
    "central": ("America/Chicago", ("America/Chicago", "US/Central")),
    "mountain": ("America/Denver", ("America/Denver", "America/Boise", "America/Phoenix", "US/Mountain")),
    "pacific": ("America/Los_Angeles", ("America/Los_Angeles", "US/Pacific")),
    "alaska": ("America/Anchorage", ("America/Anchorage",)),
    "hawaii": ("Pacific/Honolulu", ("Pacific/Honolulu",)),
}
ZONE_LABELS = {
    "ET": "eastern", "EST": "eastern", "EDT": "eastern",
    "CT": "central", "CST": "central", "CDT": "central",
    "MT": "mountain", "MST": "mountain", "MDT": "mountain",
    "PT": "pacific", "PST": "pacific", "PDT": "pacific",
    "AKT": "alaska", "AKST": "alaska", "AKDT": "alaska",
    "HT": "hawaii", "HST": "hawaii",
    "UTC": "UTC", "GMT": "Europe/London", "BST": "Europe/London",
    "WET": "Europe/Lisbon", "WEST": "Europe/Lisbon",
    "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "EET": "Europe/Kyiv", "EEST": "Europe/Kyiv",
    "IST": "Asia/Kolkata", "SGT": "Asia/Singapore", "HKT": "Asia/Hong_Kong",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
}

_CLOCK_IN_TEXT = re.compile(r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\b|\b\d{1,2}\s*[ap]\.?m\.?\b", re.I)
_EM_DASH = "—"


def parse_dt(s: str) -> datetime:
    """An ISO date-time with an offset; 'Z' accepted."""
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def user_zone_name(user_zone: str = "") -> str:
    """`user_zone` when a caller gives one, else the zone user_tz resolves
    for this install (the profile, the scheduling preferences, the
    system), else UTC."""
    if user_zone:
        return str(user_zone)
    try:
        try:
            import user_tz
        except ImportError:
            from preprocessor import user_tz
        return user_tz.name()
    except Exception:
        return "UTC"


def zone_label(tz_name: str, at: datetime) -> str:
    """The short name a reply gives a zone at one instant: the regional
    label for a US zone ('MT'), else the zone's own abbreviation then
    ('GMT' or 'BST', 'JST'), else the city when the abbreviation is only
    an offset ('+05')."""
    short = ZONE_SHORT.get(tz_name)
    if short:
        return short
    try:
        abbr = at.astimezone(ZoneInfo(tz_name)).strftime("%Z")
    except Exception:
        abbr = ""
    if abbr and abbr.isalpha():
        return abbr
    return tz_name.rsplit("/", 1)[-1].replace("_", " ")


def clock(t: datetime) -> str:
    """'10:30 AM'."""
    return t.strftime("%-I:%M %p")


def clock_range(start: datetime, end: datetime) -> str:
    """'10:30-11:00 AM' when both halves share AM/PM, else '11:30 AM-12:00 PM'."""
    if start.strftime("%p") == end.strftime("%p"):
        return f"{start.strftime('%-I:%M')}-{clock(end)}"
    return f"{clock(start)}-{clock(end)}"


def option_label(start: datetime, end: datetime, user_zone: str = "") -> str:
    """'Tue Nov 3, 10:30-11:00 AM MT': how a reply names an option, on
    the user's clock with that zone's label for the option's instant."""
    tz_name = user_zone_name(user_zone)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = start.tzinfo
    s, e = start.astimezone(tz), end.astimezone(tz)
    return f"{s.strftime('%a %b %-d')}, {clock_range(s, e)} {zone_label(tz_name, s)}"


def option_clocks(start: datetime, end: datetime, attendees: list) -> dict:
    """The same window on each attendee's clock, by address, for the
    attendees given with a `timezone`: '12:30-1:00 PM ET', or
    '5:30-6:00 PM in London' for a zone named by city. A day change is
    written out ('7:00-7:30 AM Wed in Tokyo')."""
    out = {}
    for a in attendees or []:
        tz_name = str(a.get("timezone") or "").strip()
        email = str(a.get("email") or "").strip().lower()
        if not tz_name or not email:
            continue
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            continue
        s, e = start.astimezone(tz), end.astimezone(tz)
        text = clock_range(s, e)
        if s.date() != start.date():
            text += f" {s.strftime('%a')}"
        short = ZONE_SHORT.get(tz_name)
        if short:
            out[email] = f"{text} {short}"
        else:
            city = tz_name.rsplit("/", 1)[-1].replace("_", " ")
            out[email] = f"{text} in {city}"
    return out


def label_options(options: list, attendees: list, prefix: str = "o", user_zone: str = "") -> None:
    """Give each option an `id` (o1, o2, ...), a `label` on the user's
    clock (`user_zone`, else this install's zone), the attendees'
    `clocks` and a `line` that carries all of it: 'Tue Nov 3, 10:30-11:00
    AM MT (12:30-1:00 PM ET)'. In place; a malformed option is skipped."""
    tz_name = user_zone_name(user_zone)
    for i, o in enumerate(options or [], 1):
        try:
            start, end = parse_dt(o["start"]), parse_dt(o["end"])
        except Exception:
            continue
        o["id"] = f"{prefix}{i}"
        o["label"] = option_label(start, end, tz_name)
        o["clocks"] = option_clocks(start, end, attendees)
        clocks = "; ".join(o["clocks"][k] for k in sorted(o["clocks"]))
        o["line"] = f"{o['label']} ({clocks})" if clocks else o["label"]


def names_a_clock(text: str) -> bool:
    """True when the text carries a clock time ('10:30', '2 PM'): a note
    the model writes must not."""
    return bool(_CLOCK_IN_TEXT.search(text or ""))


def clean_prose(text: str) -> str:
    """The model's sentences as the reply carries them: one paragraph,
    no em-dash (a comma), no clock times (the lines carry them)."""
    text = str(text or "").replace(_EM_DASH, ",").replace(" ,", ",")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def render_body(*, first_name: str, opening: str, lines: list[str], closing: str,
                signature: str = "") -> str:
    """The reply: greeting, the model's opening sentences, the option lines
    one per line, the closing sentence, the signature. Only `lines` carry
    times, and they are the solver's own words."""
    parts = [f"Hi {first_name}," if first_name else "Hi,"]
    opening = clean_prose(opening)
    if opening:
        parts.append(opening)
    if lines:
        parts.append("\n".join(lines))
    closing = clean_prose(closing)
    if closing:
        parts.append(closing)
    if signature:
        parts.append(signature)
    return "\n\n".join(parts) + "\n"
