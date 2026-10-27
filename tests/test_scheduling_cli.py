#!/usr/bin/env python3
"""Layer B: CLI integration tests for scheduling_engine.py.

Mocks `_fetch_all_events`, `graph.get_schedule_detail`, and `_load_prefs` so
`cmd_propose_meeting` and `cmd_validate_draft` run end-to-end against
synthetic calendar state. One test per past incident, plus universal
contract assertions.

Hermetic — no network, no live config dependency.

Run with:
    python3 tests/test_scheduling_cli.py            # standalone runner
    python3 -m pytest tests/test_scheduling_cli.py   # via pytest
"""

import io
import json
import sys
import traceback
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                 # for tests.fixtures
sys.path.insert(0, str(HERE.parent / "preprocessor")) # for scheduling_engine


# No test may touch production state: sandbox EA_DIR before the
# preprocessor import below binds module paths.
import os
import tempfile
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-schedcli-")

import scheduling_engine as se  # noqa: E402
from tests.fixtures.calendar_events import (  # noqa: E402
    _mt, outlook_event, google_event,
)


# A reasonable default prefs dict. Tests can override per-call.
_DEFAULT_PREFS = {
    "timezone": "America/Denver",
    "working_hours": {"start": "09:00", "end": "17:00"},
    "preferred_hours": {"end": "15:00"},
    "work_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
    "no_meeting_days": ["friday"],
    "no_meeting_blocks": [
        {"day": "monday",    "start": "12:00", "end": "13:00", "reason": "lunch"},
        {"day": "tuesday",   "start": "12:00", "end": "13:00", "reason": "lunch"},
        {"day": "wednesday", "start": "12:00", "end": "13:00", "reason": "lunch"},
        {"day": "thursday",  "start": "12:00", "end": "13:00", "reason": "lunch"},
        {"day": "friday",    "start": "12:00", "end": "13:00", "reason": "lunch"},
    ],
    "slot_selection": {
        "default_options_to_propose": 3,
        "preferred_duration_minutes": 30,
    },
    "calendars": {
        "outlook": {"type": "graph_api"},
        "google_primary": {
            "email": "jo.ambrose4@example.com", "type": "google_calendar_mcp",
            "blocks_availability": True,
        },
        "google_family": {
            "email": "wren.abernathy86@example.com", "type": "google_calendar_mcp",
            "blocks_availability": False,
        },
    },
}


def _default_token_constraints(earliest_date, latest_date):
    """The standard EA-Managed token set, expanded across the horizon: wh 9-5
    Mon-Fri, nm Lunch 12-1 Mon-Fri, p2 Prefer-mornings 3-5pm Mon-Fri, and
    nm No-meeting Friday (all-day). Mirrors what the real calendar now holds so
    the CLI tests exercise the token path (there is no config fallback)."""
    instances = []
    d = earliest_date
    while d <= latest_date:
        wd = d.weekday()  # 0=Mon .. 6=Sun
        if wd <= 4:       # Mon-Fri
            instances.append({"token": "wh", "label": "wh Working Hours",
                              "date": d, "start_min": 540, "end_min": 1020,
                              "all_day": False})
            instances.append({"token": "nm", "label": "nm Lunch",
                              "date": d, "start_min": 720, "end_min": 780,
                              "all_day": False})
            instances.append({"token": "p2", "label": "p2 Prefer mornings",
                              "date": d, "start_min": 900, "end_min": 1020,
                              "all_day": False})
        if wd == 4:       # Friday: no meetings all day
            instances.append({"token": "nm", "label": "nm No-meeting Friday",
                              "date": d, "start_min": 0, "end_min": 1440,
                              "all_day": True})
        d += timedelta(days=1)
    return {"instances": instances,
            "has_wh": any(i["token"] == "wh" for i in instances)}


@contextmanager
def _patched_engine(outlook_events=None, google_events=None,
                    internal_schedules=None, prefs=None,
                    token_constraints_fn=None):
    """Patch all I/O-bound entrypoints in scheduling_engine.

    - outlook_events: list returned by graph.get_calendar_view
    - google_events:  list returned by gcal.get_all_events
    - internal_schedules: dict[email -> {"busy": set, "tentative": set}]
                          for _get_internal_busy_slots
    - prefs: synthetic prefs dict (defaults to _DEFAULT_PREFS)
    - token_constraints_fn: (earliest, latest) -> constraints dict; defaults to
                          the standard EA-Managed token set.
    """
    prefs = prefs or _DEFAULT_PREFS
    token_fn = token_constraints_fn or _default_token_constraints
    accounts = [
        {
            "email": cal["email"],
            "key": key,
            "blocks_availability": cal.get("blocks_availability", True),
        }
        for key, cal in prefs.get("calendars", {}).items()
        if cal.get("type") == "google_calendar_mcp"
    ]
    with patch.object(se.graph, "get_calendar_view",
                      return_value=outlook_events or []), \
         patch.object(se.gcal, "get_all_events",
                      return_value=google_events or []), \
         patch.object(se.gcal, "_get_google_accounts",
                      return_value=accounts), \
         patch.object(se, "_load_prefs", return_value=prefs), \
         patch.object(se, "_load_token_constraints", side_effect=token_fn), \
         patch.object(se.graph, "get_schedule_detail",
                      side_effect=lambda emails, start, end, **kw: {
                          e: {"view": _internal_view_string(
                              internal_schedules or {}, e, start, end,
                          ), "error": ""}
                          for e in emails
                      }):
        yield


def _internal_view_string(schedules, email, start, end):
    """Encode an attendee's busy/tentative slots into the
    `getSchedule` availabilityView string format Microsoft Graph
    returns (one char per 15-min interval: 0=free, 1=tentative,
    2=busy, 3=OOF, 4=workingElsewhere)."""
    info = schedules.get(email, {})
    busy = info.get("busy", set())
    tent = info.get("tentative", set())
    total_minutes = int((end - start).total_seconds() / 60)
    n_slots = total_minutes // 15
    out = []
    for i in range(n_slots):
        if i in busy:
            out.append("2")
        elif i in tent:
            out.append("1")
        else:
            out.append("0")
    return "".join(out)


def _run_propose_meeting(spec):
    """Drive cmd_propose_meeting end-to-end. `spec` must contain at
    minimum `attendees` (a list of attendee dicts).

    Returns parsed JSON output.
    """
    import argparse
    args = argparse.Namespace(
        duration=spec.get("duration", 30),
        days_ahead=spec.get("days_ahead", 7),
        num_options=spec.get("num_options", 5),
        attendees=json.dumps(spec.get("attendees", [])),
        constraints=json.dumps(spec.get("constraints", [])),
        earliest_date=spec.get("earliest_date",
                                date.today().isoformat()),
        latest_date=spec.get("latest_date",
                              (date.today() + timedelta(days=14)).isoformat()),
        ignore_event=spec.get("ignore_event", []),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        se.cmd_propose_meeting(args)
    out = buf.getvalue()
    # The CLI prints a JSON object; sometimes prefixed with a banner
    # like "Using MSAL token..." that we want to strip.
    idx = out.find("{")
    if idx < 0:
        raise AssertionError(f"no JSON in cmd_propose_meeting output: {out!r}")
    return json.loads(out[idx:])


def _run_validate_draft(times_iso, duration_minutes=30, ignore_event=None):
    """Drive cmd_validate_draft end-to-end."""
    import argparse
    args = argparse.Namespace(
        times=json.dumps(times_iso),
        duration=duration_minutes,
        ignore_event=ignore_event or [],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        se.cmd_validate_draft(args)
    out = buf.getvalue()
    idx = out.find("{")
    if idx < 0:
        raise AssertionError(f"no JSON in cmd_validate_draft output: {out!r}")
    return json.loads(out[idx:])


def _run_check_attendees(attendees, start_iso, end_iso, ignore_event=None):
    """Drive cmd_check_attendees end-to-end."""
    import argparse
    args = argparse.Namespace(
        attendees=attendees,
        start=start_iso,
        end=end_iso,
        ignore_event=ignore_event or [],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        se.cmd_check_attendees(args)
    out = buf.getvalue()
    idx = out.find("{")
    if idx < 0:
        raise AssertionError(f"no JSON in cmd_check_attendees output: {out!r}")
    return json.loads(out[idx:])


def _with_id(event: dict, event_id: str) -> dict:
    """The fixture builders emit no id; the real producers do (Outlook `id`,
    Google `event_id`). --ignore-event matches on it."""
    e = dict(event)
    e["id"] = event_id
    return e


# ---------------------------------------------------------------------------
# Universal contracts
# ---------------------------------------------------------------------------

def test_propose_meeting_json_schema():
    """Output ALWAYS has the documented top-level keys, even on an
    empty calendar. Pinned so callers (skill prompts, eval judges)
    can rely on the schema."""
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_propose_meeting({
            "duration": 30, "days_ahead": 3, "num_options": 3,
            "attendees": [],
        })
    for key in ("options", "conditional_options", "infeasible_days"):
        assert key in result, f"missing top-level key {key!r}: {result.keys()}"
    assert isinstance(result["options"], list)
    assert isinstance(result["conditional_options"], list)


def test_validate_draft_returns_per_time_valid_field():
    """validate-draft returns one valid:true/false per time given."""
    tue_morning = _mt(2026, 5, 26, 9, 30)
    iso = tue_morning.isoformat()
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_validate_draft([iso], duration_minutes=30)
    assert "results" in result or "times" in result or "valid" in result, (
        f"validate-draft output missing the per-time valid field: {result}"
    )


# ---------------------------------------------------------------------------
# One test per past failure of the CLI contract
# ---------------------------------------------------------------------------

def test_harper_2026_05_18_validate_draft_returns_valid_for_free_930():
    """validate-draft on a known-free 9:30 returns valid:true. A time
    absent from propose-meeting's `options` is not thereby blocked, so
    validate-draft is the verb that answers the question.
    """
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_validate_draft(
            ["2026-05-26T09:30:00-06:00"], duration_minutes=60,
        )
    # The exact field name varies (valid / valid_times / per-entry), so
    # accept any of the documented shapes.
    if "valid" in result:
        assert result["valid"] is True, f"9:30 should be valid: {result}"
    elif "results" in result:
        assert all(r.get("valid") is True for r in result["results"]), result
    elif "times" in result:
        assert all(t.get("valid") is True for t in result["times"]), result
    else:
        raise AssertionError(f"unrecognized validate-draft shape: {result}")


def test_tam_2026_05_19_returns_quarter_hour_options():
    """5/19: "Tuesday mornings, 30 min, Tam available 8-12" returned
    only 2 options because of the no_buffer penalty + ±15-min
    exclusion stride. Fix produced 6 options at quarter-hour
    increments.

    The integration test: with Tam's stated 8-12 availability and
    Kit's calendar containing one 9:30-10:30 block, propose-meeting
    should return at least the 9:00, 10:30, 10:45, 11:00, 11:15, 11:30
    starts. (We don't assert exactly six because the solver may
    relax further or fewer based on soft costs.)"""
    # Kit's calendar: just one 9:30-10:30 Tuesday meeting
    outlook = [outlook_event(
        "Existing meeting",
        _mt(2026, 5, 26, 9, 30),
        _mt(2026, 5, 26, 10, 30),
    )]
    spec = {
        "duration": 30, "days_ahead": 7, "num_options": 8,
        "earliest_date": "2026-05-26", "latest_date": "2026-05-26",
        "attendees": [{
            "email": "tam@example.com",
            "name": "Tam",
            "type": "external",
            "availability": [
                {"day": "2026-05-26", "start": "08:00", "end": "12:00"},
            ],
        }],
    }
    with _patched_engine(outlook_events=outlook, google_events=[]):
        result = _run_propose_meeting(spec)
    starts = {opt.get("start") for opt in result.get("options", [])}
    # Extract just "HH:MM" from the ISO start strings
    times = {s.split("T")[1][:5] for s in starts if s and "T" in s}
    # The set must include the back-to-back-with-existing-meeting starts
    # AND their quarter-hour neighbours, not only later ones.
    assert "09:00" in times, (
        f"9:00 (back-to-back before 9:30-10:30 block) missing from "
        f"options: {sorted(times)}"
    )
    assert "10:30" in times, (
        f"10:30 (back-to-back after 9:30-10:30 block) missing: {sorted(times)}"
    )
    assert "10:45" in times, (
        f"10:45 (quarter-hour increment) missing: {sorted(times)}"
    )


def test_today_2026_05_22_propose_meeting_with_absolute_path_succeeds():
    """A direct call to cmd_propose_meeting, the equivalent of the
    absolute-path CLI invocation the allowlist accepts, completes cleanly
    on a normal request shape."""
    spec = {
        "duration": 30, "days_ahead": 7, "num_options": 3,
        "attendees": [{"email": "alice@example.com", "name": "Alice",
                       "type": "internal"}],
    }
    with _patched_engine(outlook_events=[], google_events=[],
                          internal_schedules={"alice@example.com": {}}):
        result = _run_propose_meeting(spec)
    assert "options" in result
    assert len(result["options"]) >= 1


def test_zakariya_2026_04_29_malformed_attendees_doesnt_crash():
    """4/29: Claude passed malformed attendee JSON; cmd_propose_meeting
    exited with code 2 (argparse usage error). The contract: even when
    `--attendees` is wonky (empty array, missing fields, bad email),
    the engine returns a structured error or empty options rather than
    a stack trace."""
    # Empty attendees array = just Kit's calendar
    spec = {
        "duration": 30, "days_ahead": 3, "num_options": 3,
        "attendees": [],
    }
    with _patched_engine(outlook_events=[], google_events=[]):
        # Should NOT raise
        result = _run_propose_meeting(spec)
    assert "options" in result


def test_harper_2026_04_16_no_false_positive_on_calendar_word():
    """4/16: Harper wrote 'I have one fewer meeting on my calendar'
    and the bot misclassified the email as a scheduling REQUEST,
    creating a spurious ledger entry.

    This test sits at the engine boundary (the bot's misclassification
    is in prompt_logic — separate test). What we pin HERE: even if a
    spurious scheduling call is made with bizarre inputs (e.g. no
    real attendees, no real ask), the engine returns a clean schema
    rather than a misleading result."""
    spec = {
        "duration": 30, "days_ahead": 3, "num_options": 3,
        "attendees": [],
    }
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_propose_meeting(spec)
    assert isinstance(result.get("options"), list)
    assert isinstance(result.get("conditional_options", []), list)


# ---------------------------------------------------------------------------
# Calendar source semantics — blocks_availability, internal getSchedule
# ---------------------------------------------------------------------------

def test_blocks_availability_false_does_not_hard_block():
    """The `google_family` calendar in scheduling-preferences.json has
    `blocks_availability: false`. Events there should NOT prevent
    propose-meeting from offering overlapping slots — this is the
    pattern Kit uses for his spouse's calendar (informational
    visibility, no scheduling impact).

    Compares two runs (same scenario, only the spouse calendar's
    blocks_availability flag flipped) and asserts the false-case sees
    MORE feasible candidates."""
    family_event = google_event(
        "Spouse appointment",
        _mt(2026, 5, 26, 10, 0),
        _mt(2026, 5, 26, 11, 0),
        source_calendar="wren.abernathy86@example.com",
    )
    spec = {
        "duration": 30, "days_ahead": 7, "num_options": 30,
        "earliest_date": "2026-05-26", "latest_date": "2026-05-26",
        "attendees": [],
    }

    prefs_blocking = json.loads(json.dumps(_DEFAULT_PREFS))
    prefs_blocking["calendars"]["google_family"]["blocks_availability"] = True
    prefs_nonblock = json.loads(json.dumps(_DEFAULT_PREFS))
    prefs_nonblock["calendars"]["google_family"]["blocks_availability"] = False

    with _patched_engine(outlook_events=[], google_events=[family_event],
                          prefs=prefs_blocking):
        blocking = _run_propose_meeting(spec)
    with _patched_engine(outlook_events=[], google_events=[family_event],
                          prefs=prefs_nonblock):
        nonblock = _run_propose_meeting(spec)

    n_blocking = len(blocking.get("options", []))
    n_nonblock = len(nonblock.get("options", []))
    # blocks_availability:true removes the 10:00, 10:15, 10:30, 10:45
    # starts. blocks_availability:false leaves them all in. So the
    # non-blocking run must have STRICTLY more feasible candidates.
    assert n_nonblock > n_blocking, (
        f"blocks_availability:false should expose more candidates than "
        f"=true. Got blocking={n_blocking}, nonblock={n_nonblock}"
    )


def test_outlook_event_does_hard_block_normally():
    """Sanity counterpart to the blocks_availability:false test: a
    regular Outlook event MUST hard-block its time range, or a change
    that lets every calendar through would pass unnoticed."""
    outlook = [outlook_event(
        "Faculty meeting",
        _mt(2026, 5, 26, 10, 0),
        _mt(2026, 5, 26, 11, 0),
    )]
    spec = {
        "duration": 30, "days_ahead": 7, "num_options": 8,
        "earliest_date": "2026-05-26", "latest_date": "2026-05-26",
        "attendees": [],
    }
    with _patched_engine(outlook_events=outlook, google_events=[]):
        result = _run_propose_meeting(spec)
    starts = {opt.get("start") for opt in result.get("options", [])}
    times = {s.split("T")[1][:5] for s in starts if s and "T" in s}
    # Slots that would COVER the meeting time must not appear:
    # 10:00 (10:00-10:30 covers 10:00-10:30), 10:15 (covers 10:15-10:45),
    # 10:30 (covers 10:30-11:00). 9:45 covers 9:45-10:15 → overlaps too.
    blocked = {"09:45", "10:00", "10:15", "10:30"}
    for t in blocked:
        assert t not in times, (
            f"slot {t} should be blocked by the 10:00-11:00 meeting, "
            f"but appeared in options: {sorted(times)}"
        )


def test_external_attendee_without_availability_blocks_entire_day():
    """If an external attendee has NO `availability` window for a day,
    that day is fully blocked for them (we have no signal they're
    free, and external attendees are not in the GAL for getSchedule)."""
    spec = {
        "duration": 30, "days_ahead": 7, "num_options": 5,
        "earliest_date": "2026-05-26", "latest_date": "2026-05-27",
        "attendees": [{
            "email": "rio.ravensworth19@example.org",
            "name": "External",
            "type": "external",
            # Only Wednesday given
            "availability": [
                {"day": "2026-05-27", "start": "09:00", "end": "17:00"},
            ],
        }],
    }
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_propose_meeting(spec)
    starts = [opt.get("start") for opt in result.get("options", [])]
    dates = {s[:10] for s in starts if s}
    assert "2026-05-26" not in dates, (
        f"Tuesday should be unavailable (no external availability "
        f"window provided); options: {starts}"
    )


def _internal_with_one_day(complete):
    att = {"email": "lane@example.edu", "name": "Lane", "type": "internal",
           "availability": [{"day": "2026-05-27", "start": "09:00", "end": "17:00"}]}
    if complete:
        att["availability_complete"] = True
    spec = {"duration": 30, "num_options": 5, "earliest_date": "2026-05-26",
            "latest_date": "2026-05-27", "attendees": [att]}
    with _patched_engine(outlook_events=[], google_events=[]):
        result = _run_propose_meeting(spec)
    return {o["start"][:10] for o in result.get("options", [])}


def test_internal_stated_availability_leaves_other_days_to_getschedule():
    """An internal person's stated windows ("Wednesday works") constrain
    only the days they name."""
    assert "2026-05-26" in _internal_with_one_day(complete=False)


def test_complete_availability_blocks_unnamed_days_for_internal_too():
    """A booking link lists all of a person's open time: a day it leaves
    empty is not free, even when getSchedule shows the person free."""
    dates = _internal_with_one_day(complete=True)
    assert dates == {"2026-05-27"}, dates


# ---------------------------------------------------------------------------
# Token-based preferences (EA-Managed calendar) + transparency
# ---------------------------------------------------------------------------

def test_token_prefs_enforce_wh_lunch_friday():
    """Preferences now come from EA-Managed token events: propose only inside
    wh (9-5), never during nm Lunch (12-1) or on nm No-meeting Friday."""
    with _patched_engine():
        result = _run_propose_meeting({
            "attendees": [{"email": "colleague@example.edu", "type": "internal"}],
            "duration": 30,
            "earliest_date": "2026-07-20",   # Monday
            "latest_date": "2026-07-24",      # Friday
            "num_options": 6,
        })
    assert result["status"] == "ok" and result["options"]
    for opt in result["options"]:
        st = datetime.fromisoformat(opt["start"])
        assert st.weekday() != 4, f"Friday proposed: {opt['time_label']}"
        assert 9 <= st.hour < 17, f"outside 9-5: {opt['time_label']}"
        assert st.hour != 12, f"lunch proposed: {opt['time_label']}"


def test_token_transparency_excluded_and_applied_prefs():
    """Output explains WHY: applied_preferences per option + an excluded list
    naming the token blocks the user could override."""
    with _patched_engine():
        result = _run_propose_meeting({
            "attendees": [{"email": "colleague@example.edu", "type": "internal"}],
            "duration": 30,
            "earliest_date": "2026-07-20",
            "latest_date": "2026-07-24",
            "num_options": 5,
        })
    assert "excluded" in result
    for opt in result["options"]:
        assert "applied_preferences" in opt
    reasons = " | ".join(e["reason"] for e in result["excluded"])
    assert "Lunch" in reasons, f"lunch not in excluded: {reasons}"
    assert "Friday" in reasons, f"Friday not in excluded: {reasons}"
    # each excluded entry is either a hard block or a soft demotion
    assert all(e["type"] in ("blocked", "demoted") for e in result["excluded"])


def test_token_p2_afternoon_demotes_not_blocks():
    """p2 Prefer mornings (3-5pm) is a soft penalty: afternoon slots may still
    be offered, but they carry the p2 label in applied_preferences."""
    with _patched_engine():
        result = _run_propose_meeting({
            "attendees": [{"email": "colleague@example.edu", "type": "internal"}],
            "duration": 30,
            "earliest_date": "2026-07-20",
            "latest_date": "2026-07-23",     # Mon-Thu (skip Friday)
            "num_options": 12,
        })
    afternoon = [o for o in result["options"]
                 if datetime.fromisoformat(o["start"]).hour >= 15]
    for o in afternoon:
        assert any("Prefer mornings" in k for k in o["applied_preferences"]), o


def test_calendar_preferences_traceability_and_no_stale_friday():
    """The output exposes exactly which token events were read
    (calendar_preferences.tokens), and with NO nm event, Friday is a
    normal working day. A remembered 'no-meeting Friday' that is not on
    the calendar must not apply."""
    def _wh_and_p2_only(earliest, latest):
        # wh 9-5 Mon-Fri + p2 3-5 Mon-Fri, but NO nm anywhere.
        insts = []
        d = earliest
        while d <= latest:
            if d.weekday() <= 4:
                insts.append({"token": "wh", "label": "wh Working Hours",
                              "date": d, "start_min": 540, "end_min": 1020,
                              "all_day": False})
                insts.append({"token": "p2", "label": "p2 Prefer mornings",
                              "date": d, "start_min": 900, "end_min": 1020,
                              "all_day": False})
            d += timedelta(days=1)
        return {"instances": insts, "has_wh": True}

    # Friday-only window: if Friday were blocked, status would be no_options.
    with _patched_engine(token_constraints_fn=_wh_and_p2_only):
        result = _run_propose_meeting({
            "attendees": [{"email": "colleague@example.edu", "type": "internal"}],
            "duration": 30,
            "earliest_date": "2026-07-24",   # Friday
            "latest_date": "2026-07-24",      # Friday
            "num_options": 3,
        })

    cp = result.get("calendar_preferences", {})
    toks = cp.get("tokens", {})
    # every token key present; empty list == rule absent
    for t in ("wh", "nm", "p0", "p1", "p2", "wr"):
        assert t in toks, f"{t} missing from calendar_preferences.tokens"
    assert toks["nm"] == [], f"nm should be empty (no events), got {toks['nm']}"
    assert toks["wh"], "wh should be populated"
    # No nm => Friday is a normal working day and MUST be offered.
    assert result["status"] == "ok" and result["options"], \
        "Friday must be proposable when no nm event blocks it"
    for o in result["options"]:
        assert datetime.fromisoformat(o["start"]).weekday() == 4
    # The Friday must not be flagged infeasible, and nothing in excluded
    # may cite a no-meeting/Friday block.
    assert "2026-07-24" not in result.get("infeasible_days", {})
    reasons = " ".join(e["reason"] for e in result.get("excluded", []))
    assert "No-meeting" not in reasons and "Friday" not in reasons, reasons


def test_no_wh_events_errors_cleanly():
    """No config fallback: if the EA-Managed calendar yields no wh window,
    propose-meeting refuses rather than guessing."""
    def _no_tokens(earliest, latest):
        return {"instances": [], "has_wh": False}
    with _patched_engine(token_constraints_fn=_no_tokens):
        result = _run_propose_meeting({
            "attendees": [{"email": "colleague@example.edu", "type": "internal"}],
            "earliest_date": "2026-07-20",
            "latest_date": "2026-07-24",
        })
    assert result["status"] == "error"
    assert "wh" in result["message"].lower()


# ---------------------------------------------------------------------------
# All-day events
#
# `event["start"]["date"]` is a shape neither Graph nor
# google_calendar_client emits, so an all-day path keyed on it solves a
# conference week as five free days. The factories in
# tests/fixtures/calendar_events.py still cannot express a real all-day event
# (`outlook_event(is_all_day=True)` emits a timed dateTime pair), so these
# build the two production shapes here.
# ---------------------------------------------------------------------------

def _all_day_outlook(subject, first_day, last_day=None, show_as="free"):
    """Graph's all-day shape: `isAllDay` plus dateTimeTimeZone objects whose
    END is midnight of the day AFTER the last covered day."""
    last_day = last_day or first_day
    exclusive_end = date.fromisoformat(last_day) + timedelta(days=1)
    return {
        "subject": subject,
        "start": {"dateTime": f"{first_day}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{exclusive_end.isoformat()}T00:00:00.0000000",
                "timeZone": "UTC"},
        "isAllDay": True,
        "showAs": show_as,
        "attendees": [],
        "organizer": {"emailAddress": {"address": "kit@org"}},
    }


def _all_day_google(subject, first_day, last_day=None,
                    source_calendar="jo.ambrose4@example.com"):
    """google_calendar_client.get_events' normalized all-day shape:
    `is_all_day` (snake_case) and the EXCLUSIVE end date with a 23:59:59
    tail bolted on by the client."""
    last_day = last_day or first_day
    exclusive_end = date.fromisoformat(last_day) + timedelta(days=1)
    return {
        "subject": subject,
        "start": {"dateTime": f"{first_day}T00:00:00"},
        "end": {"dateTime": f"{exclusive_end.isoformat()}T23:59:59"},
        "is_all_day": True,
        "source_calendar": source_calendar,
        "source_type": "google",
    }


def _option_dates(result):
    return {o["start"][:10] for o in result.get("options", [])}


def _all_day_by_subject(result, subject):
    for ad in result.get("all_day_events", []):
        if ad["subject"] == subject:
            return ad
    raise AssertionError(
        f"{subject!r} missing from all_day_events: "
        f"{[a['subject'] for a in result.get('all_day_events', [])]}")


def test_all_day_conference_blocks_every_day_and_is_reported():
    """A Tue-Wed conference on Outlook. Every day it covers is
    infeasible, with an infeasible_days entry and metadata for the LLM to
    judge from."""
    outlook = [_all_day_outlook("ICRA 2026 Conference", "2026-07-21", "2026-07-22")]
    with _patched_engine(outlook_events=outlook, google_events=[]):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 12,
            "earliest_date": "2026-07-20",    # Monday
            "latest_date": "2026-07-23",       # Thursday
        })

    dates = _option_dates(result)
    assert "2026-07-21" not in dates and "2026-07-22" not in dates, (
        f"conference days were offered as free: {sorted(dates)}")
    assert dates, "the open days should still produce options"

    for day in ("2026-07-21", "2026-07-22"):
        assert day in result["infeasible_days"], (
            f"{day} missing from infeasible_days: {result['infeasible_days']}")
        assert "ICRA" in result["infeasible_days"][day]

    ad = _all_day_by_subject(result, "ICRA 2026 Conference")
    assert ad["classification"] == "hard_block"
    assert ad["effect"] == "blocked"
    assert ad["days"] == ["2026-07-21", "2026-07-22"]
    assert ad["start_date"] == "2026-07-21"
    assert ad["end_date"] == "2026-07-22", "end_date is the INCLUSIVE last day"


def test_all_day_event_ends_on_its_exclusive_end_date():
    """A one-day all-day event must not swallow the next morning: Graph's
    end is midnight of the FOLLOWING day."""
    outlook = [_all_day_outlook("Travel to Boston", "2026-07-21")]
    with _patched_engine(outlook_events=outlook, google_events=[]):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 8,
            "earliest_date": "2026-07-21",     # Tuesday (travel)
            "latest_date": "2026-07-22",        # Wednesday (clear)
        })
    dates = _option_dates(result)
    assert "2026-07-21" not in dates, f"travel day offered: {sorted(dates)}"
    assert dates == {"2026-07-22"}, (
        f"Wednesday must be fully available: {sorted(dates)}")
    assert "2026-07-22" not in result["infeasible_days"]
    assert _all_day_by_subject(result, "Travel to Boston")["days"] == ["2026-07-21"]


def test_informational_all_day_event_is_reported_but_does_not_block():
    """A birthday is context, not a commitment. It must appear in
    all_day_events and cost the day nothing."""
    google = [_all_day_google("Lane's birthday", "2026-07-21")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 5,
            "earliest_date": "2026-07-21", "latest_date": "2026-07-21",
        })
    assert result["status"] == "ok" and result["options"], (
        "an all-day birthday must not empty the day")
    assert "2026-07-21" not in result["infeasible_days"]

    ad = _all_day_by_subject(result, "Lane's birthday")
    assert ad["classification"] == "ignore"
    assert ad["effect"] == "none"
    assert ad["days"] == ["2026-07-21"]
    for opt in result["options"]:
        assert "all_day_event" not in opt["cost_breakdown"], (
            f"an ignored all-day event must not penalize the day: {opt}")


def test_unknown_all_day_event_penalizes_and_is_flagged_for_judgment():
    """An unclassifiable all-day title stays proposable, carries the
    all_day_unknown penalty, and is named on the option so the LLM can ask
    about it (skills/scheduling-rules.md: 'Uncertain -> ask the user')."""
    google = [_all_day_google("Xan", "2026-07-21")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 3,
            "earliest_date": "2026-07-21", "latest_date": "2026-07-21",
        })
    assert result["options"], "an unknown all-day event must not block the day"
    ad = _all_day_by_subject(result, "Xan")
    assert ad["classification"] == "unknown"
    assert ad["effect"] == "penalized"
    assert ad["penalty"] == se._DEFAULT_WEIGHTS["all_day_unknown"]
    top = result["options"][0]
    assert top["cost_breakdown"].get("all_day_event") == \
        se._DEFAULT_WEIGHTS["all_day_unknown"], top
    assert "Xan" in top["notes"], (
        f"the proposed option must name the all-day event: {top['notes']!r}")


def test_all_day_event_on_a_non_blocking_calendar_never_blocks():
    """The spouse calendar is context only (blocks_availability: false), so
    even an all-day 'Vacation' there is reported, not enforced."""
    google = [_all_day_google("Vacation", "2026-07-21",
                              source_calendar="wren.abernathy86@example.com")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 3,
            "earliest_date": "2026-07-21", "latest_date": "2026-07-21",
        })
    assert result["options"], "a non-blocking calendar must not empty the day"
    ad = _all_day_by_subject(result, "Vacation")
    assert ad["classification"] == "hard_block"
    assert ad["effect"] == "none"
    assert ad["note"], "the downgrade must say why"


def test_ea_managed_all_day_token_is_not_double_counted_as_all_day():
    """`nm No-meeting Friday` is a CONSTRAINT, applied from the token path.
    It must not also appear as an unknown all-day event that penalizes the
    day a second time."""
    google = [_all_day_google("nm No-meeting Friday", "2026-07-21",
                              source_calendar="ea-managed")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_propose_meeting({
            "attendees": [], "duration": 30, "num_options": 3,
            "earliest_date": "2026-07-21", "latest_date": "2026-07-21",
        })
    assert result["all_day_events"] == [], (
        f"token events need no all-day judgment: {result['all_day_events']}")
    for opt in result["options"]:
        assert "all_day_event" not in opt["cost_breakdown"], opt


def test_validate_draft_flags_a_time_inside_an_all_day_conference():
    """The send-time preflight: a draft proposing Wednesday of a
    conference week must not validate clean."""
    outlook = [_all_day_outlook("ICRA 2026 Conference", "2026-07-21", "2026-07-22")]
    with _patched_engine(outlook_events=outlook, google_events=[]):
        result = _run_validate_draft(["2026-07-22T10:00:00-06:00"], 30)
    assert result["valid"] is False, (
        f"a time inside a conference week is not valid: {result}")
    conflicts = [i for i in result["issues"] if i["type"] == "conflict"]
    assert any("ICRA" in i.get("conflict_with", "") for i in conflicts), conflicts
    ad = result["all_day_events"][0]
    assert ad["subject"] == "ICRA 2026 Conference"
    assert ad["effect"] == "blocked"


def test_validate_draft_stays_quiet_about_an_informational_all_day_event():
    """The preflight must not cry wolf. A birthday is reported under
    all_day_events and raises no issue."""
    google = [_all_day_google("Lane's birthday", "2026-07-21")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_validate_draft(["2026-07-21T10:00:00-06:00"], 30)
    assert result["valid"] is True, result
    assert not [i for i in result["issues"] if i["type"] == "all_day_event"], (
        f"a birthday must not warn at send time: {result['issues']}")
    assert result["all_day_events"][0]["subject"] == "Lane's birthday"


def test_validate_draft_surfaces_an_unknown_all_day_event_for_judgment():
    """An unclassifiable all-day event is a soft issue, not a conflict."""
    google = [_all_day_google("Xan", "2026-07-21")]
    with _patched_engine(outlook_events=[], google_events=google):
        result = _run_validate_draft(["2026-07-21T10:00:00-06:00"], 30)
    assert result["valid"] is True, result
    soft = [i for i in result["issues"] if i["type"] == "all_day_event"]
    assert soft and "Xan" in soft[0]["detail"], result["issues"]


# ---------------------------------------------------------------------------
# check-attendees and --ignore-event: "can Lane join my existing 1:1?"
# ---------------------------------------------------------------------------
# The solver blocks every slot the user's own calendar fills, so it never
# scored the 1:1 window itself and the resident answered "I could not
# check this". check-attendees reads the people; --ignore-event lifts the
# user's own event so the other two verbs can score its slot.

# A Tuesday. 11:15-12:00 MT is the 1:1 with Oakley, and Lane is busy for its
# first quarter hour only.
_T_DAY = date(2026, 10, 6)
_ONE_ON_ONE = _with_id(outlook_event(
    "1:1 Oakley", _mt(2026, 10, 6, 11, 15), _mt(2026, 10, 6, 12, 0),
    attendees=["oakley@example.edu"]), "EV-1ON1")


def test_check_attendees_reports_people_and_own_events():
    schedules = {"lane@example.edu": {"busy": {0}},   # 11:15-11:30 busy
                 "oakley@example.edu": {}}
    with _patched_engine(outlook_events=[_ONE_ON_ONE],
                         internal_schedules=schedules):
        result = _run_check_attendees(
            "lane@example.edu,oakley@example.edu",
            "2026-10-06T11:15:00", "2026-10-06T12:00:00")
    assert result["status"] == "ok", result
    lane = result["attendees"]["lane@example.edu"]
    oakley = result["attendees"]["oakley@example.edu"]
    assert lane["status"] == "partial", lane
    assert lane["view"] == "200", lane
    assert lane["not_free"] == [{"state": "busy",
                                 "start": "2026-10-06T11:15:00-06:00",
                                 "end": "2026-10-06T11:30:00-06:00"}], lane
    assert oakley["status"] == "free", oakley
    assert result["all_attendees_free"] is False
    # The user's own 1:1 is listed, with the id a follow-up call can name.
    assert [e["id"] for e in result["organizer_events"]] == ["EV-1ON1"], result
    assert result["organizer_free"] is False
    assert "Tue Oct 06 2026, 11:15 AM" in result["window"]["interpreted_as"]


def test_check_attendees_unknown_address_and_ignore_event():
    """An address getSchedule returns nothing for is `unknown`, never
    `free`; an ignored id drops out of organizer_events; an id that
    matches nothing is reported."""
    def no_view(emails, start, end, **kw):
        return {"lane@example.edu": {"view": "000", "error": ""}}     # nothing for the other one
    with _patched_engine(outlook_events=[_ONE_ON_ONE]), \
         patch.object(se.graph, "get_schedule_detail", side_effect=no_view):
        result = _run_check_attendees(
            '["lane@example.edu", {"email": "someone@elsewhere.org"}]',
            "2026-10-06T11:15:00", "2026-10-06T12:00:00",
            ignore_event=["EV-1ON1", "EV-TYPO"])
    assert result["attendees"]["lane@example.edu"]["status"] == "free"
    assert result["attendees"]["someone@elsewhere.org"]["status"] == "unknown"
    assert result["all_attendees_free"] is False
    assert result["organizer_events"] == []
    assert result["organizer_free"] is True
    assert [e["id"] for e in result["ignored_events"]] == ["EV-1ON1"]
    assert result["ignore_not_found"] == ["EV-TYPO"]


def test_validate_draft_ignore_event_lifts_the_conflict():
    with _patched_engine(outlook_events=[_ONE_ON_ONE]):
        before = _run_validate_draft(["2026-10-06T11:15:00-06:00"], 45)
        after = _run_validate_draft(["2026-10-06T11:15:00-06:00"], 45,
                                    ignore_event=["EV-1ON1"])
    assert before["valid"] is False, before
    assert any(i["type"] == "conflict" and i["conflict_with"] == "1:1 Oakley"
               for i in before["issues"]), before
    assert after["valid"] is True, after
    assert not any(i["type"] == "conflict" for i in after["issues"]), after
    assert [e["id"] for e in after["ignored_events"]] == ["EV-1ON1"]
    assert after["ignore_not_found"] == []


def test_propose_meeting_ignore_event_makes_its_slot_a_candidate():
    """Over one day with the 1:1 as the only event, no option may overlap
    it; with the event ignored, at least one option must."""
    spec = {
        "duration": 30, "num_options": 40,
        "earliest_date": _T_DAY.isoformat(), "latest_date": _T_DAY.isoformat(),
        "attendees": [{"email": "lane@example.edu", "name": "Lane",
                       "type": "internal"}],
    }
    def overlaps(opt):
        s = datetime.fromisoformat(opt["start"])
        e = datetime.fromisoformat(opt["end"])
        return s < _mt(2026, 10, 6, 12, 0) and e > _mt(2026, 10, 6, 11, 15)
    with _patched_engine(outlook_events=[_ONE_ON_ONE]):
        before = _run_propose_meeting(spec)
        after = _run_propose_meeting({**spec, "ignore_event": ["EV-1ON1"]})
    assert before["status"] == "ok", before
    assert not any(overlaps(o) for o in before["options"]), before["options"]
    assert before["ignored_events"] == []
    assert any(overlaps(o) for o in after["options"]), after["options"]
    assert [e["id"] for e in after["ignored_events"]] == ["EV-1ON1"]
    assert after["ignore_not_found"] == []



def test_an_unreadable_attendee_calendar_is_not_checked_not_free():
    """getSchedule's per-person error (an address outside the tenant, an
    unknown one) is reported: the attendee is "NOT CHECKED" and the run
    warns, instead of treating them as free."""
    spec = {
        "duration": 30, "num_options": 3,
        "earliest_date": _T_DAY.isoformat(), "latest_date": _T_DAY.isoformat(),
        "attendees": [{"email": "lane@example.edu", "name": "Lane", "type": "internal"},
                      {"email": "hollis.isenberg21@example.org", "name": "Pat", "type": "internal"}],
    }

    def detail(emails, start, end, **kw):
        return {"lane@example.edu": {"view": "0" * 96, "error": ""},
                "hollis.isenberg21@example.org": {"view": "", "error": "ErrorMailboxNotFound"}}
    with _patched_engine(), patch.object(se.graph, "get_schedule_detail", side_effect=detail):
        result = _run_propose_meeting(spec)
    assert result["attendees_checked"]["lane@example.edu"] == "Graph getSchedule"
    assert result["attendees_checked"]["hollis.isenberg21@example.org"] == "NOT CHECKED: ErrorMailboxNotFound"
    assert any("hollis.isenberg21@example.org" in w for w in result["warnings"]), result["warnings"]



def test_check_attendees_window_across_the_fall_back_reads_to_its_end():
    """A window across the Nov 1 change is an hour longer in real time; the
    whole view is read and the busy run shows at its clock time."""
    from zoneinfo import ZoneInfo
    den = ZoneInfo("America/Denver")
    start, end = datetime(2026, 10, 31, 9, 0, tzinfo=den), datetime(2026, 11, 2, 10, 0, tzinfo=den)
    t, view = start.astimezone(timezone.utc), []
    while t < end.astimezone(timezone.utc):
        local = t.astimezone(den)
        view.append("2" if local >= datetime(2026, 11, 2, 9, 30, tzinfo=den) else "0")
        t += timedelta(minutes=15)
    with _patched_engine(), patch.object(
            se.graph, "get_schedule_detail",
            return_value={"bo@example.edu": {"view": "".join(view), "error": ""}}):
        result = _run_check_attendees("bo@example.edu", "2026-10-31T09:00:00", "2026-11-02T10:00:00")
    bo = result["attendees"]["bo@example.edu"]
    assert bo["status"] == "partial"
    assert bo["not_free"] == [{"state": "busy", "start": "2026-11-02T09:30:00-07:00",
                                  "end": "2026-11-02T10:00:00-07:00"}]


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
