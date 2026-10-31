#!/usr/bin/env python3
"""Tests for the EA-Managed calendar write verbs in google_calendar_client.

`update-event` and `delete-event` are how the resident moves or deletes
a hold on the EA-Managed calendar. Access is not the constraint: the
account holds `owner` on that calendar.

The verbs are un-gated on EA-Managed (no attendees, no invitations, and
a cron reconciler already rewrites it) and refuse every other calendar,
so adding them cannot become a way around the button gate that protects
real meetings.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

# No test may touch production state. google_calendar_client does
# AUTH_DIR.mkdir() at import, so EA_DIR must be a sandbox BEFORE the import
# below; unset, it resolves to the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-gcalwrite-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import google_calendar_client as g  # noqa: E402

EA = "eli.calloway8@example.com"


class _FakeEvents:
    # The EA's own hold: no attendees, organized by the EA-Managed calendar.
    HOLD = {"summary": "p2 scratch", "organizer": {"email": EA, "self": True}}

    def __init__(self, log, event=None):
        self.log = log
        self.event = self.HOLD if event is None else event
        self.items = []

    def patch(self, **kw):
        self.log.append(("patch", kw))
        return _Exec({"id": kw.get("eventId"), "summary": "patched",
                      "start": {"dateTime": "2026-12-29T16:00:00-07:00"},
                      "end": {"dateTime": "2026-12-29T17:00:00-07:00"}})

    def delete(self, **kw):
        self.log.append(("delete", kw))
        return _Exec({})

    def insert(self, **kw):
        self.log.append(("insert", kw))
        return _Exec({"id": "NEW1", "htmlLink": ""})

    def list(self, **kw):
        return _Exec({"items": self.items})

    def get(self, **kw):
        self.log.append(("get", kw))
        return _Exec(dict(self.event))


class _Exec:
    def __init__(self, val):
        self.val = val

    def execute(self):
        return self.val


class _FakeService:
    def __init__(self, log, event=None):
        self._events = _FakeEvents(log, event)

    def events(self):
        return self._events


_ORIG = {"src": g._ea_managed_source, "svc": g._build_service}


def _patch(monkey_log, event=None):
    g._ea_managed_source = lambda: {"email": "jo.westbrook34@example.com", "calendar_id": EA}
    g._build_service = lambda email, interactive=False: _FakeService(monkey_log, event)


def _writes(log):
    return [entry for entry in log if entry[0] != "get"]


def _restore():
    g._ea_managed_source = _ORIG["src"]
    g._build_service = _ORIG["svc"]


def isolated(fn):
    """Put the module globals back, so a stubbed service cannot leak into
    a later test in the same process."""
    def wrapper():
        try:
            return fn()
        finally:
            _restore()
    wrapper.__name__ = fn.__name__
    return wrapper


@isolated
def test_update_moves_an_ea_managed_hold():
    calls = []
    _patch(calls)
    r = g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA,
                       start="2026-12-29T16:00:00", end="2026-12-29T17:00:00")
    assert r["status"] == "updated"
    assert r["updated_fields"] == ["end", "start"]
    op, kw = _writes(calls)[0]
    assert op == "patch" and kw["calendarId"] == EA
    # only what was asked for is sent, so a retime keeps the token word
    assert set(kw["body"]) == {"start", "end"}


@isolated
def test_delete_removes_an_ea_managed_hold():
    calls = []
    _patch(calls)
    r = g.delete_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA)
    assert r["status"] == "deleted"
    assert ("delete", {"calendarId": EA, "eventId": "E1"}) in calls


@isolated
def test_both_verbs_refuse_any_other_calendar():
    """The gate that matters: a real meeting mails its attendees when it
    moves, so it stays on the button-approved path."""
    for fn in (lambda: g.update_event(email="jo.westbrook34@example.com", event_id="E1",
                                      calendar_id="jules.ellery0@example.com",
                                      start="2026-12-29T16:00:00"),
               lambda: g.delete_event(email="jo.westbrook34@example.com", event_id="E1",
                                      calendar_id="jules.ellery0@example.com")):
        calls = []
        _patch(calls)
        r = fn()
        assert "refused" in r["error"]
        assert r["ea_managed"] == EA
        assert "pending-event" in r["hint"]
        assert _writes(calls) == []          # nothing reached the API


@isolated
def test_calendar_id_match_ignores_case_and_padding():
    calls = []
    _patch(calls)
    r = g.update_event(email="jo.westbrook34@example.com", event_id="E1",
                       calendar_id=f"  {EA.upper()}  ", summary="p1 hold")
    assert r["status"] == "updated"


@isolated
def test_refuses_when_ea_managed_is_not_configured():
    # Another test file may have set the environment fallback at import;
    # this test is about an install with no id anywhere. Put it back after,
    # since the pop would otherwise reach every later test in the process.
    saved = os.environ.pop("EA_MANAGED_CALENDAR_ID", None)
    try:
        _refuses_when_not_configured()
    finally:
        if saved is not None:
            os.environ["EA_MANAGED_CALENDAR_ID"] = saved


def _refuses_when_not_configured():
    calls = []
    _patch(calls)
    g._ea_managed_source = lambda: {}
    r = g.delete_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA)
    assert "not configured" in r["error"]
    assert calls == []


@isolated
def test_update_with_no_fields_is_rejected_before_the_api():
    calls = []
    _patch(calls)
    r = g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA)
    assert "nothing to update" in r["error"]
    assert calls == []


@isolated
def test_retitling_recolors_to_match_the_new_token():
    calls = []
    _patch(calls)
    g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA,
                   summary="p0 hard block")
    body = _writes(calls)[0][1]["body"]
    assert body["colorId"] == g._token_color("p0 hard block")


@isolated
def test_explicit_color_wins_over_the_token_default():
    calls = []
    _patch(calls)
    g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA,
                   summary="p0 hard block", color_id="9")
    assert _writes(calls)[0][1]["body"]["colorId"] == "9"


def test_create_event_cannot_invite_anyone():
    """The no-invite invariant behind the EA-Managed auto-hold carve-out:
    create-event stays off the deny list ONLY because it structurally
    cannot address a human. Neither the library function nor the CLI
    parser may grow an attendee parameter without this test forcing the
    carve-out to be re-decided."""
    import inspect
    params = inspect.signature(g.create_event).parameters
    assert not any("attendee" in p.lower() for p in params), (
        "create_event() grew an attendee parameter; the mail_mediation "
        "auto-hold carve-out is no longer safe as designed")
    src = inspect.getsource(g.create_event)
    assert "attendees" not in src, (
        "create_event() references attendees; event bodies must never "
        "carry them on the un-gated path")
    module_src = Path(g.__file__).read_text()
    assert "--attendee" not in module_src, (
        "the CLI grew an --attendee/--attendees flag; the auto-hold "
        "carve-out must be re-decided before any verb can invite")


def test_list_ea_managed_is_a_mediated_read_the_resident_can_run():
    """The flow CLAUDE.md documents is list-ea-managed, then update-event
    or delete-event on an id it returned, so the listing verb must be in
    SAFE or the ea-bot shim refuses it as an unknown verb and the
    resident cannot get an id. It is a read of one calendar; the write
    gate is untouched, so create-event stays GATED."""
    sys.path.insert(0, str(HERE.parent / "preprocessor"))
    import mail_mediation as M
    assert M.is_safe("google_calendar_client", "list-ea-managed")
    assert not M.is_gated("google_calendar_client", "list-ea-managed")
    assert M.is_gated("google_calendar_client", "create-event")
    # A verb in SAFE with no subparser is forwarded and then dies in the
    # mediator's argparse with exit 2.
    import re
    module_src = Path(g.__file__).read_text()
    verbs = set(re.findall(r"""sub\.add_parser\(\s*["']([a-z-]+)["']""",
                           module_src))
    declared = (M.SAFE["google_calendar_client"]
                | M.GATED["google_calendar_client"])
    assert not (declared - verbs), f"declared but unparseable: {declared - verbs}"


@isolated
def test_date_only_values_are_sent_as_all_day():
    calls = []
    _patch(calls)
    g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA,
                   start="2026-12-29", end="2026-12-30")
    body = _writes(calls)[0][1]["body"]
    assert body["start"] == {"date": "2026-12-29"}
    assert "dateTime" not in body["end"]


def test_failed_refresh_does_not_return_stale_credentials():
    """A refresh that raises returns None when non-interactive and falls
    through to the browser flow when interactive; the expired credentials
    object is never handed back as if it were valid."""
    import json as _json

    class _Creds:
        valid = False
        expired = True
        refresh_token = "r"
        def refresh(self, _req):
            raise RuntimeError("invalid_grant")
        def to_json(self):
            return "{}"

    email = "stale@example.com"
    token = g._token_path(email)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(_json.dumps({"token": "x"}))
    orig_from_file = g.Credentials.from_authorized_user_file
    orig_secret = g.CLIENT_SECRET_FILE
    orig_published = g._published.PUBLISHED_GOOGLE_CLIENT_ID
    try:
        g.Credentials.from_authorized_user_file = staticmethod(lambda *_a, **_k: _Creds())
        g.CLIENT_SECRET_FILE = Path(tempfile.mkdtemp()) / "missing.json"
        g._published.PUBLISHED_GOOGLE_CLIENT_ID = ""  # no client at all, not even the project's
        assert g.authenticate_account(email, interactive=False) is None
        # interactive: falls to the browser flow, which stops at the missing secret
        assert g.authenticate_account(email, interactive=True) is None
    finally:
        g.Credentials.from_authorized_user_file = orig_from_file
        g.CLIENT_SECRET_FILE = orig_secret
        g._published.PUBLISHED_GOOGLE_CLIENT_ID = orig_published


OTHER = "jules.ellery0@example.com"   # an account's own calendar, not EA-Managed


@isolated
def test_the_library_writes_no_other_calendar_without_an_approval():
    """create_event, _patch_event and _remove_event check for themselves,
    so a direct call cannot reach a calendar the CLI gates."""
    calls = []
    _patch(calls)
    r = g.create_event(email=OTHER, summary="Night class", start="2027-02-11T09:30:00",
                       end="2027-02-11T10:45:00", timezone="America/Denver")
    assert r["error"].startswith("BLOCKED")
    r = g.create_event(email=OTHER, summary="Night class", start="2027-02-11T09:30:00",
                       end="2027-02-11T10:45:00", timezone="America/Denver",
                       approval="gcal-approved:made-up")
    assert r["error"].startswith("BLOCKED")
    assert "BLOCKED" in g._patch_event(OTHER, "E1", OTHER, {"summary": "x"}, hint="")["error"]
    assert "BLOCKED" in g._remove_event(OTHER, "E1", OTHER)["error"]
    assert calls == []


@isolated
def test_an_approval_is_bound_and_spent_once():
    calls = []
    _patch(calls)
    wrong = g.issue_inprocess_approval("create", OTHER, OTHER, "Another title")
    assert "BLOCKED" in g.create_event(
        email=OTHER, summary="Night class", start="2027-02-11T09:30:00",
        end="2027-02-11T10:45:00", timezone="America/Denver", approval=wrong)["error"]
    ok = g.issue_inprocess_approval("create", OTHER, OTHER, "Night class")
    kw = dict(email=OTHER, summary="Night class", start="2027-02-11T09:30:00",
              end="2027-02-11T10:45:00", timezone="America/Denver", approval=ok)
    assert g.create_event(**kw)["status"] == "created"
    assert "BLOCKED" in g.create_event(**kw)["error"]
    assert [c[0] for c in calls] == ["insert"] and calls[0][1]["calendarId"] == OTHER
    # an update approval does not delete, and the reverse
    upd = g.issue_inprocess_approval("update", OTHER, OTHER, "E1")
    assert "BLOCKED" in g._remove_event(OTHER, "E1", OTHER, approval=upd)["error"]
    dele = g.issue_inprocess_approval("delete", OTHER, OTHER, "E1")
    assert g._remove_event(OTHER, "E1", OTHER, approval=dele)["status"] == "deleted"


@isolated
def test_ea_managed_create_needs_no_approval():
    calls = []
    _patch(calls)
    r = g.create_event(email="jo.westbrook34@example.com", summary="p1 focus",
                       start="2027-02-11T09:30:00", end="2027-02-11T10:45:00",
                       timezone="America/Denver", calendar_id=EA)
    assert r["status"] == "created" and calls[0][1]["calendarId"] == EA


@isolated
def test_an_ea_managed_move_or_delete_needs_the_eas_own_hold():
    """No attendees, and the EA-Managed calendar as organizer. A meeting
    someone invited that calendar to is not the EA's to change."""
    refused = {
        "attendees": {"summary": "sync", "organizer": {"email": EA, "self": True},
                      "attendees": [{"email": "harper.fairbanks70@example.edu"}]},
        "another organizer": {"summary": "sync",
                              "organizer": {"email": "harper.fairbanks70@example.edu"}},
        "no organizer": {"summary": "sync"},
        "organizer not self": {"summary": "sync", "organizer": {"email": OTHER, "self": False}},
    }
    for label, event in refused.items():
        for fn in (lambda: g.update_event(email="jo.westbrook34@example.com", event_id="E1",
                                          calendar_id=EA, summary="p1 moved"),
                   lambda: g.delete_event(email="jo.westbrook34@example.com", event_id="E1",
                                          calendar_id=EA)):
            calls = []
            _patch(calls, event)
            r = fn()
            assert "not the EA's own hold" in r.get("error", ""), (label, r)
            assert _writes(calls) == [], label
    # the organizer's address equal to the calendar id counts as its own
    calls = []
    _patch(calls, {"summary": "p2", "organizer": {"email": EA.upper()}})
    assert g.delete_event(email="jo.westbrook34@example.com", event_id="E1",
                          calendar_id=EA)["status"] == "deleted"


@isolated
def test_an_unreadable_event_is_not_moved():
    calls = []
    _patch(calls)

    def boom(**kw):
        raise RuntimeError("404 not found")
    svc = _FakeService(calls)
    svc._events.get = boom
    g._build_service = lambda email, interactive=False: svc
    r = g.update_event(email="jo.westbrook34@example.com", event_id="E1", calendar_id=EA,
                       summary="p1")
    assert "could not read" in r["error"] and _writes(calls) == []


@isolated
def test_recolor_skips_what_is_not_the_eas_own():
    calls = []
    _patch(calls)
    svc = _FakeService(calls)
    svc._events.items = [
        {"id": "A", "summary": "p0 block", "organizer": {"email": EA, "self": True}},
        {"id": "B", "summary": "p0 sync", "organizer": {"email": EA, "self": True},
         "attendees": [{"email": "harper.fairbanks70@example.edu"}]},
        {"id": "C", "summary": "p0 invite", "organizer": {"email": OTHER}},
    ]
    g._build_service = lambda email, interactive=False: svc
    r = g.recolor_ea_managed("jo.westbrook34@example.com", EA)
    assert [c[1]["eventId"] for c in _writes(calls)] == ["A"]
    assert r["non_token_skipped"] == 2


def test_graph_ea_hold_needs_an_explicit_organizer():
    """is_ea_hold counts a read without isOrganizer as not organiser."""
    import graph_client as gc
    hold = {"categories": [gc.EA_HOLD_CATEGORY], "showAs": "tentative", "attendees": []}
    assert gc.is_ea_hold(hold) == (False, "the user is not the organiser")
    assert gc.is_ea_hold({**hold, "isOrganizer": None})[0] is False
    assert gc.is_ea_hold({**hold, "isOrganizer": True}) == (True, "")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
