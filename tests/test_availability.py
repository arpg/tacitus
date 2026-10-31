"""availability: whose free/busy the solver reads, under each provider.

graph (getSchedule through graph_client's fetch), owa (the same call, only
when the outlook-web-auth plugin has a token), none (no one's calendar; the
invitees pick), the Mac's own calendars first (LocalFirst), the provider
read from config, and the solver's degraded mode: every internal attendee
unchecked by design, the options offered for the invitees to pick. Nothing
reaches the network."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocessor"))
import availability as av  # noqa: E402
import native_accounts as na  # noqa: E402
from tests.test_scheduling_cli import _T_DAY, _patched_engine, _run_propose_meeting  # noqa: E402
import scheduling_engine as se  # noqa: E402

START = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


class Fetch:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def __call__(self, emails, start, end, interval_minutes=15):
        self.calls.append((list(emails), interval_minutes))
        if self.answer is not None:
            return self.answer
        return {e: {"view": "0020", "error": ""} for e in emails}


def test_graph_reads_getschedule_and_never_reports_a_missing_person_as_free():
    fetch = Fetch({"a@example.edu": {"view": "0020", "error": ""}})
    p = av.get_provider("graph", fetch=fetch, native=False)
    out = p.schedule_detail(["a@example.edu", "b@example.edu"], START, END)
    assert fetch.calls == [(["a@example.edu", "b@example.edu"], 15)]
    assert out["a@example.edu"]["view"] == "0020"
    assert out["b@example.edu"] == {"view": "", "error": "no schedule returned"}
    assert p.source == "Graph getSchedule" and p.degraded is False
    assert p.schedule_detail([], START, END) == {}


def test_owa_asks_only_when_the_plugin_holds_a_token():
    fetch = Fetch()
    p = av.get_provider("owa", fetch=fetch, token_source=lambda: None, native=False)
    out = p.schedule_detail(["a@example.edu"], START, END)
    assert out == {"a@example.edu": {"view": "", "error": av.OWA_NO_TOKEN}}
    assert fetch.calls == []
    p = av.get_provider("owa", fetch=fetch, token_source=lambda: "header.payload.sig", native=False)
    assert p.schedule_detail(["a@example.edu"], START, END)["a@example.edu"]["view"] == "0020"
    assert fetch.calls and "Outlook on the web" in p.source and p.degraded is False


def test_none_reads_no_one_and_says_so():
    fetch = Fetch()
    p = av.get_provider("none", fetch=fetch, native=False)
    out = p.schedule_detail(["a@example.edu", "b@example.edu"], START, END)
    assert all(v == {"view": "", "error": av.NONE_REASON} for v in out.values())
    assert fetch.calls == [] and p.degraded is True


class Native:
    def __init__(self, readable):
        self.readable = {e.lower() for e in readable}
        self.asked = []

    def can_read(self, email):
        return email.lower() in self.readable

    def get_schedule_detail(self, emails, start, end, interval_minutes=15):
        self.asked.append(list(emails))
        return {e: {"view": "1111", "error": ""} for e in emails}


@pytest.mark.parametrize("name", av.PROVIDERS)
def test_the_macs_own_calendars_answer_first_under_every_provider(name):
    native = Native(["me@example.edu", "pat@example.edu"])
    fetch = Fetch()
    p = av.get_provider(name, fetch=fetch, token_source=lambda: "t", native=native)
    out = p.schedule_detail(["pat@example.edu", "dean@example.edu"], START, END)
    assert native.asked == [["pat@example.edu"]]
    assert out["pat@example.edu"]["view"] == "1111"
    assert out["dean@example.edu"]["view"] == ("" if name == "none" else "0020")
    assert fetch.calls == ([] if name == "none" else [(["dean@example.edu"], 15)])
    assert p.name == name


def test_a_native_calendar_client_plugs_in_as_the_local_reader():
    fake = na.FakeNativeHelper()
    fake.add_calendar("CAL-PAT", "Pat")
    fake.add_event("CAL-PAT", "Seminar", "2026-10-27T15:00:00Z", "2026-10-27T15:30:00Z")
    scope = na.scope_from_dict({"calendar": {"calendars": [{"id": "CAL-PAT"}],
                                             "shared": {"pat@example.edu": "CAL-PAT"}}})
    client = na.NativeCalendarClient(na.NativeHelper(fake), scope)
    p = av.get_provider("none", native=client)
    out = p.schedule_detail(["pat@example.edu"], START, END, 15)
    assert out["pat@example.edu"] == {"view": "2200", "error": ""}


def test_the_provider_comes_from_config_and_an_unknown_one_is_graph(tmp_path):
    cfg = tmp_path / "ea-config.json"
    assert av.provider_name(cfg) == "graph"
    for name in av.PROVIDERS:
        cfg.write_text(json.dumps({"availability": {"provider": name}}))
        assert av.provider_name(cfg) == name
        assert av.mode(cfg) == {"provider": name,
                                "mode": "invitee-picks" if name == "none" else "checked"}
        assert av.source_label(cfg) == {"graph": "Graph getSchedule",
                                        "owa": "Graph getSchedule (Outlook on the web session)",
                                        "none": "none (the invitees pick)"}[name]
    cfg.write_text(json.dumps({"availability": {"provider": "exchange-ews"}}))
    assert av.provider_name(cfg) == "graph"


def test_no_native_reader_off_a_mac(monkeypatch):
    monkeypatch.setattr(av.sys, "platform", "linux")
    assert av._native_calendar() is None
    assert isinstance(av.get_provider("graph", fetch=Fetch()), av.GraphAvailability)


# ── the solver ───────────────────────────────────────────────────────

def test_the_solver_asks_graph_by_default_through_its_own_fetch():
    with patch.object(se.availability, "provider_name", return_value="graph"), \
            patch.object(se.graph, "get_schedule_detail",
                         side_effect=lambda e, s, t, **kw: {x: {"view": "2", "error": ""} for x in e}) as g:
        out = se._schedule_detail(["a@example.edu"], START, END)
    assert g.called and out["a@example.edu"]["view"] == "2"
    with patch.object(se.availability, "provider_name", return_value="graph"):
        assert se._schedule_source() == "Graph getSchedule"
        assert se._availability_mode() == {"provider": "graph", "mode": "checked"}


def test_with_provider_none_the_solver_reads_no_attendee_and_marks_them_unchecked():
    with patch.object(se.availability, "provider_name", return_value="none"), \
            patch.object(se.graph, "get_schedule_detail") as g:
        slots = se._get_internal_busy_slots(
            ["lane@example.edu"], datetime(2026, 10, 27, 0, 0, tzinfo=se.USER_TZ),
            datetime(2026, 10, 28, 0, 0, tzinfo=se.USER_TZ), 96)
    assert not g.called
    assert slots["lane@example.edu"]["busy"] == set()
    assert slots["lane@example.edu"]["unchecked"] == av.NONE_REASON


def test_propose_meeting_in_invitee_picks_mode_offers_times_and_says_so():
    spec = {
        "duration": 30, "num_options": 3,
        "earliest_date": _T_DAY.isoformat(), "latest_date": _T_DAY.isoformat(),
        "attendees": [{"email": "lane@example.edu", "name": "Lane", "type": "internal"}],
    }
    with _patched_engine(), patch.object(se.availability, "provider_name", return_value="none"):
        result = _run_propose_meeting(spec)
    assert result["status"] == "ok" and result["options"], result
    assert result["availability"] == {"provider": "none", "mode": "invitee-picks"}
    assert result["attendees_checked"]["lane@example.edu"] == f"NOT CHECKED: {av.NONE_REASON}"
    assert any("availability provider none" in w and "pick" in w for w in result["warnings"])
    assert not any(w.startswith("Could not read the calendar") for w in result["warnings"])


def test_stated_availability_still_constrains_the_options_under_none():
    """Offered times in the thread (an attendee's `availability`) are the
    invitee's own word, so provider none keeps them: every option sits in
    the window the attendee stated."""
    window = {"day": _T_DAY.isoformat(), "start": "13:00", "end": "14:00"}
    spec = {
        "duration": 30, "num_options": 3,
        "earliest_date": _T_DAY.isoformat(), "latest_date": _T_DAY.isoformat(),
        "attendees": [{"email": "guest@example.org", "name": "Guest", "type": "external",
                       "availability": [window]}],
    }
    with _patched_engine(), patch.object(se.availability, "provider_name", return_value="none"):
        result = _run_propose_meeting(spec)
    assert result["options"], result
    for opt in result["options"]:
        start = datetime.fromisoformat(opt["start"])
        assert (start.hour, start.minute) >= (13, 0) and (start.hour, start.minute) <= (13, 30), opt
    assert result["attendees_checked"]["guest@example.org"] == "stated availability"
