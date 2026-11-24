"""Google calendar events through the Create, Reschedule and Delete buttons.

A staged event with "calendar": "<Google account>" goes on that Google
calendar: the card names the calendar and refuses attendees, the click mints
a token bound to the account and the title (create) or the event id (update,
delete), and google_calendar_client.py refuses every calendar but EA-Managed
without that token. The repeat keys become one RRULE. No network.
"""
import argparse
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# google_calendar_client creates AUTH_DIR at import; keep it in a sandbox.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-gcal-create-")
# The calendar the un-gated writes may touch; a resident process takes it
# from the environment (mail_mediation.ea_managed_calendar_id).
os.environ["EA_MANAGED_CALENDAR_ID"] = "ea@group"   # the id the fixture config names

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import google_calendar_client as g  # noqa: E402
import graph_actions as ga  # noqa: E402
import mail_mediation as M  # noqa: E402

FAMILY = "xan.marchetti45@example.com"


# --- RRULE from the staged repeat keys -------------------------------------

def test_a_tuesday_thursday_series_ends_at_the_end_of_its_last_day_in_utc():
    rule = g.rrule_from_repeat("weekly", "2027-02-11T09:30:00", "2027-02-11T10:45:00",
                               days="TU,TH", until="2027-03-23")
    # Mar 23 2027 is after the DST change, so 23:59:59 MDT is 05:59:59Z.
    assert rule == ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270324T055959Z"]


def test_count_interval_and_all_day():
    assert g.rrule_from_repeat("daily", "2027-02-11T09:00", "2027-02-11T10:00",
                               interval=2, count=5) == ["RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5"]
    # An all-day occurrence ends the next day (Google's end is exclusive).
    assert g.rrule_from_repeat("weekly", "2027-02-11", "2027-02-12", days="th",
                               count="3") == ["RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=3"]


@pytest.mark.parametrize("kw", [
    dict(repeat="weekly", days="TU,TH"),                                  # no end
    dict(repeat="weekly", days="TU", until="2027-03-23", count=3),        # two ends
    dict(repeat="weekly", days="TU,XX", count=3),                        # bad day
    dict(repeat="weekly", count=3),                                      # no days
    dict(repeat="daily", days="TU", count=3),                            # days on daily
    dict(repeat="monthly", count=3),
    dict(repeat="weekly", days="TU", until="2027-01-01"),                # before start
    dict(repeat="weekly", days="TU", until="03/23/2027"),
    dict(repeat="weekly", days="TU", count=3, end="2027-02-12T10:45:00"),  # spans days
])
def test_bad_combinations_are_refused(kw):
    end = kw.pop("end", "2027-02-11T10:45:00")
    repeat = kw.pop("repeat")
    with pytest.raises(ValueError):
        g.rrule_from_repeat(repeat, "2027-02-11T09:30:00", end, **kw)


# --- the token gate in create-event ----------------------------------------

def _args(**over):
    base = dict(email=FAMILY, summary="Night class", start="2027-02-11T09:30:00",
                end="2027-02-11T10:45:00", description="", location="",
                timezone="America/Denver", calendar_id="", recurrence=None,
                color_id="", repeat=None, repeat_days=None, repeat_interval=None,
                repeat_until=None, repeat_count=None, confirm_token=None)
    base.update(over)
    return argparse.Namespace(**base)


def _run(args, valid=True):
    created, checked = [], []

    def validate(token, action, ident):
        checked.append((token, action, ident))
        return {"ok": True} if valid else None
    with patch.object(ga, "_validate_and_consume_token", validate), \
         patch.object(g, "create_event", lambda **kw: created.append(kw) or {"status": "created"}):
        out = g.cmd_create_event(args)
    return out, created, checked


def test_a_real_calendar_needs_a_token():
    out, created, checked = _run(_args())
    assert out["error"].startswith("BLOCKED") and not created and not checked


def test_a_bad_token_creates_nothing():
    out, created, checked = _run(_args(confirm_token="tok"), valid=False)
    assert out["error"].startswith("BLOCKED") and not created
    assert checked == [("tok", "create_google_event", f"{FAMILY}:Night class")]


def test_a_good_token_creates_the_series():
    out, created, checked = _run(_args(confirm_token="tok", repeat="weekly",
                                       repeat_days="TU,TH", repeat_until="2027-03-23"))
    assert out == {"status": "created"}
    assert checked == [("tok", "create_google_event", f"{FAMILY}:Night class")]
    assert created[0]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270324T055959Z"]
    assert created[0]["calendar_id"] == "" and created[0]["email"] == FAMILY


def test_a_bad_series_does_not_burn_the_token():
    out, created, checked = _run(_args(confirm_token="tok", repeat="weekly", repeat_days="TU"))
    assert "recurrence" in out["error"] and not checked and not created
    out, _, checked = _run(_args(confirm_token="tok", repeat="weekly", repeat_days="TU",
                                 repeat_count="3", recurrence=["RRULE:FREQ=DAILY;COUNT=2"]))
    assert out["error"].startswith("BLOCKED") and not checked


def test_ea_managed_stays_ungated():
    out, created, checked = _run(_args(calendar_id=M.ea_managed_calendar_id(),
                                       recurrence=["RRULE:FREQ=DAILY;COUNT=2"]))
    assert out == {"status": "created"} and not checked
    assert created[0]["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=2"]


def test_a_create_error_is_json_not_a_traceback():
    def boom(**kw):
        raise RuntimeError("403 forbidden")
    with patch.object(ga, "_validate_and_consume_token", lambda *a: {"ok": True}), \
         patch.object(g, "create_event", boom):
        out = g.cmd_create_event(_args(confirm_token="tok"))
    assert "403 forbidden" in out["error"]


def test_token_round_trip_is_one_shot_and_bound(tmp_path):
    """A real token from the store: it works once, for this account and title
    only, and an Outlook create_event token does not open the Google route."""
    with patch.object(ga, "PENDING_CONFIRMS_FILE", tmp_path / "pending.json"), \
         patch.object(ga, "PENDING_CONFIRMS_LOCK", tmp_path / "pending.lock"), \
         patch.object(g, "create_event", lambda **kw: {"status": "created"}):
        tok = ga._create_confirmation("create_google_event", g.confirm_key(FAMILY, "Night class"))
        other = ga._create_confirmation("create_google_event", g.confirm_key("jo.ambrose4@example.com", "Night class"))
        outlook = ga._create_confirmation("create_event", f"{FAMILY}:Night class")
        assert "error" in g.cmd_create_event(_args(confirm_token=other))
        assert "error" in g.cmd_create_event(_args(confirm_token=outlook))
        assert g.cmd_create_event(_args(confirm_token=tok)) == {"status": "created"}
        assert "error" in g.cmd_create_event(_args(confirm_token=tok))


def test_the_resident_still_cannot_reach_it():
    assert M.is_gated("google_calendar_client", "create-event")
    assert M.is_gated("graph_actions", "request-confirm")
    for argv in (["create-event", "--email", FAMILY, "--confirm-token", "x"],
                 ["create-event", "--calendar-id", FAMILY]):
        assert not M.is_allowed("google_calendar_client", "create-event", argv)
    assert "create_google_event" in (ROOT / "preprocessor" / "graph_actions.py").read_text()


# --- the bot ----------------------------------------------------------------

def _bot_fn(name):
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index(f"\ndef {name}(")
    ends = [i for i in (src.find("\ndef ", start + 1), src.find("\n@", start + 1),
                        src.find("\n# ", start + 1)) if i > 0]
    return src[start:min(ends)]


def _cards_ns():
    """The bot's `# ── Confirmation cards` block with the real card types
    and a frontend that takes any card."""
    import dataclasses

    import chat
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("# ── Confirmation cards")
    ns = {"json": json, "_chat": chat, "dataclasses": dataclasses,
          "FRONTEND": types.SimpleNamespace()}
    exec(src[start:src.index("# ── End confirmation cards", start)], ns)
    return ns


def _read(subject="Night class", **kw):
    """An owner-side read as _read_calendar_event returns it."""
    ev = {"subject": subject, "when": "Thu Feb 11 2027, 11:00 AM MST to Thu Feb 11 2027, "
          "12:15 PM MST", "organizer": "you", "is_organizer": True, "attendees": [],
          "location": "", "series": "", "cancelled": False, "fingerprint": "f" * 64}
    ev.update(kw)
    return ev


def _bot_ns(files: dict):
    log = types.SimpleNamespace(warning=lambda *a: None, info=lambda *a: None,
                                exception=lambda *a: None, error=lambda *a: None)
    ns = {"json": json, "EA_DIR": Path("/ea"), "log": log,
          "_resident_text": lambda p: files.get(Path(p).name),
          "_OUTLOOK_CALENDAR_NAMES": {"", "outlook", "work", "office365", "o365", "microsoft"}}
    exec(_bot_fn("_csv"), ns)
    exec(_bot_fn("_google_calendar_account"), ns)
    return ns


PREFS = json.dumps({"calendars": {
    "outlook": {"type": "graph_api"},
    "google_primary": {"type": "google_calendar_mcp", "email": "jo.ambrose4@example.com"},
    "google_family": {"type": "google_calendar_mcp", "email": FAMILY},
    "google_ea_blocks": {"type": "google_calendar_mcp", "email": "jo.ambrose4@example.com",
                         "calendar_id": "bri.fairbanks70@example.com"}}})
PROFILE = json.dumps({"calendar_accounts": ["jo.ambrose4@example.com", FAMILY,
                                            "finley.iverson2@example.com"]})


def test_the_calendar_key_resolves_to_an_account_or_an_error():
    acct = _bot_ns({"scheduling-preferences.json": PREFS, "user-profile.json": PROFILE})[
        "_google_calendar_account"]
    for outlook in ("", None, "outlook", "Outlook", "work"):
        assert acct(outlook) == ("", "")
    assert acct(FAMILY) == (FAMILY, "")
    assert acct("xan.marchetti45@example.com") == (FAMILY, "")
    assert acct("google_family") == (FAMILY, "")
    assert acct("finley.iverson2@example.com") == ("finley.iverson2@example.com", "")
    for bad in ("google_ea_blocks", "family", "someone@x.com"):
        hit, err = acct(bad)
        assert hit == "" and "unknown calendar" in err and FAMILY in err


def test_the_card_names_the_google_calendar_instead_of_attendees():
    ns = _cards_ns()
    value = json.dumps({"payload_ref": "k"})
    card = ns["_event_card"]("C", "T", "Night class", "s", "e", "", "", "weekly on TU,TH", False,
                             value, calendar=FAMILY)
    labels = [f.label for f in card.fields]
    assert "Attendees" not in labels and ("Calendar", f"{FAMILY} (Google, no invites)") in [
        (f.label, f.value) for f in card.fields]
    assert card.title == ":lock: *Confirm calendar event*"
    outlook = ns["_event_card"]("C", "T", "Sync", "s", "e", "gray.jamison0@example.edu", "", "",
                                False, value)
    assert [f.label for f in outlook.fields][:3] == ["Subject", "When", "Attendees"]
    assert outlook.title == ":lock: *Confirm calendar invite*"


def _post_ns():
    ns = _bot_ns({"scheduling-preferences.json": PREFS, "user-profile.json": PROFILE})
    posted, cards, payloads = [], [], []
    ns.update(
        FRONTEND=types.SimpleNamespace(post=lambda **kw: posted.append(kw)),
        _resolve_online_location=lambda loc, online: "https://zoom" if online else loc,
        _staged_button_value=lambda base, payload: payloads.append(payload) or "v",
        _event_card=lambda *a, **kw: cards.append((a, kw)) or "card",
        _repeat_summary=lambda r: "weekly",
        _event_attendee_warning=lambda *a: "WARN",
        _event_time_local=lambda v: v,
        _event_time_text=lambda v, *a: v,
        # A Google event gets no join block: the card shows the text as staged.
        _invite_body_as_sent=lambda body, online: body + ("+JOIN" if online else ""),
        _deliver_confirmation=lambda *a: None)
    exec(_bot_fn("_staged_google_target"), ns)
    exec(_bot_fn("_post_event_confirmation"), ns)
    return ns["_post_event_confirmation"], posted, cards, payloads


def test_staging_a_google_event_with_attendees_posts_no_button():
    post, posted, cards, _ = _post_ns()
    post("C", "T", "Dinner", "s", "e", "reese.carrow60@example.com", calendar=FAMILY)
    assert not cards and ":x:" in posted[0]["text"] and "cannot invite" in posted[0]["text"]
    post("C", "T", "Dinner", "s", "e", "", calendar="nowhere")
    assert len(posted) == 2 and "unknown calendar" in posted[1]["text"] and not cards


def test_a_google_event_carries_its_calendar_and_drops_zoom_and_hold():
    post, posted, cards, payloads = _post_ns()
    post("C", "T", "Night class", "s", "e", "", location="Zoom", online=True, hold=True,
         repeat={"repeat": "weekly"}, calendar="google_family")
    assert not posted
    assert payloads[0]["calendar"] == FAMILY
    assert payloads[0]["online"] is False and payloads[0]["hold"] is False
    assert payloads[0]["location"] == "Zoom" and payloads[0]["repeat"] == "weekly"
    assert cards[0][1]["calendar"] == FAMILY and cards[0][1]["warning"] == ""
    assert cards[0][1]["body"] == ""
    post("C", "T", "Sync", "s", "e", "gray.jamison0@example.edu", online=True)
    assert "calendar" not in payloads[1] and cards[1][1]["warning"] == "WARN"
    assert cards[1][1]["body"] == "+JOIN"


def _exec_ns():
    ns = {"json": json, "SCRIPT_DIR": Path("/srv/ea"),
          "REPEAT_KEYS": ("repeat", "repeat_days", "repeat_interval",
                          "repeat_until", "repeat_count"),
          "log": types.SimpleNamespace(exception=lambda *a: None)}
    posted = []
    ns.update(_remote_llm_note=lambda *a: None,
              _replace_buttons_with_status=lambda *a: None,
              FRONTEND=types.SimpleNamespace(post=lambda **kw: posted.append(kw)))
    exec(_bot_fn("_csv"), ns)
    exec(_bot_fn("_quietly"), ns)
    exec(_bot_fn("_post_result"), ns)
    exec(_bot_fn("_execute_event_create"), ns)
    return ns["_execute_event_create"], posted


def _fake_run(calls, status):
    def run(cmd, **kw):
        calls.append(cmd)
        out = {"token": "TOK"} if "request-confirm" in cmd else {"status": status}
        return types.SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)
    return run


def test_the_click_routes_a_google_event_to_the_google_client():
    run, posted = _exec_ns()
    calls = []
    with patch("subprocess.run", _fake_run(calls, "created")):
        ok = run("C", "T", None, {"subject": "Night class", "start": "2027-02-11T09:30:00",
                                  "end": "2027-02-11T10:45:00", "calendar": FAMILY,
                                  "attendees": "", "body": "Room TBD", "online": False,
                                  "repeat": "weekly", "repeat_days": "TU,TH",
                                  "repeat_until": "2027-03-23"})
    assert ok is True
    mint, create = calls
    assert mint[mint.index("--action") + 1] == "create_google_event"
    assert mint[mint.index("--message-id") + 1] == f"{FAMILY}:Night class"
    assert create[1].endswith("google_calendar_client.py") and create[2] == "create-event"
    assert create[create.index("--email") + 1] == FAMILY
    assert create[create.index("--description") + 1] == "Room TBD"
    assert create[create.index("--repeat-days") + 1] == "TU,TH"
    assert "--confirm-token=TOK" in create
    assert not any(a.startswith("--attendee") or a in ("--online", "--hold") for a in create)
    assert f"on {FAMILY}" in posted[0]["text"]


def test_the_click_still_creates_an_outlook_event_without_the_key():
    run, posted = _exec_ns()
    calls = []
    with patch("subprocess.run", _fake_run(calls, "event_created")):
        ok = run("C", "T", None, {"subject": "Sync", "start": "s", "end": "e",
                                  "attendees": "gray.jamison0@example.edu", "online": True})
    assert ok is True
    mint, create = calls
    assert mint[mint.index("--action") + 1] == "create_event"
    assert mint[mint.index("--message-id") + 1] == "Sync"
    assert create[1].endswith("graph_actions.py") and "--online" in create
    assert posted[0]["text"] == ":white_check_mark: Calendar event created: Sync"


# --- move and delete: the client -------------------------------------------

def _uargs(**over):
    base = dict(email=FAMILY, event_id="E1", calendar_id="", summary="", start="",
                end="", description="", location="", timezone="America/Denver",
                recurrence=None, color_id="", confirm_token=None)
    base.update(over)
    return argparse.Namespace(**base)


def _dargs(**over):
    base = dict(email=FAMILY, event_id="E1", calendar_id="", confirm_token=None)
    base.update(over)
    return argparse.Namespace(**base)


def _gated(fn, args, valid=True):
    done, checked = [], []

    def validate(token, action, ident):
        checked.append((token, action, ident))
        return {"ok": True} if valid else None
    with patch.object(ga, "_validate_and_consume_token", validate), \
         patch.object(g, "_patch_event",
                      lambda *a, **kw: done.append(("patch", a)) or {"status": "updated"}), \
         patch.object(g, "_remove_event",
                      lambda *a, **kw: done.append(("delete", a)) or {"status": "deleted"}), \
         patch.object(g, "_ea_managed_source",
                      lambda: {"email": "jo.ambrose4@example.com", "calendar_id": "ea@group"}):
        out = fn(args)
    return out, done, checked


def test_without_a_token_update_and_delete_stay_on_ea_managed():
    out, done, checked = _gated(g.cmd_update_event, _uargs(calendar_id=FAMILY, start="2027-02-11T11:00:00"))
    assert "refused" in out["error"] and not done and not checked
    out, done, checked = _gated(g.cmd_delete_event, _dargs(calendar_id=FAMILY))
    assert "refused" in out["error"] and not done and not checked
    # flag-less, they still default to EA-Managed
    out, done, _ = _gated(g.cmd_delete_event, _dargs(email=""))
    assert done == [("delete", ("jo.ambrose4@example.com", "E1", "ea@group"))]


def test_a_token_moves_an_event_on_the_accounts_own_calendar():
    out, done, checked = _gated(g.cmd_update_event, _uargs(
        confirm_token="tok", start="2027-02-11T11:00:00", end="2027-02-11T12:15:00",
        summary="p1 Night class"))
    assert out == {"status": "updated"}
    assert checked == [("tok", "update_google_event", f"{FAMILY}:E1")]
    (op, (email, event_id, cal, body)), = done
    assert (op, email, event_id, cal) == ("patch", FAMILY, "E1", FAMILY)
    assert body == {"summary": "p1 Night class",
                    "start": {"dateTime": "2027-02-11T11:00:00", "timeZone": "America/Denver"},
                    "end": {"dateTime": "2027-02-11T12:15:00", "timeZone": "America/Denver"}}


def test_a_token_deletes_from_the_accounts_own_calendar():
    out, done, checked = _gated(g.cmd_delete_event, _dargs(confirm_token="tok"))
    assert out == {"status": "deleted"}
    assert checked == [("tok", "delete_google_event", f"{FAMILY}:E1")]
    assert done == [("delete", (FAMILY, "E1", FAMILY))]


def test_the_button_paths_refuse_before_touching_the_calendar():
    for fn, args in ((g.cmd_update_event, _uargs(confirm_token="tok", start="2027-02-11T11:00:00")),
                     (g.cmd_delete_event, _dargs(confirm_token="tok"))):
        out, done, _ = _gated(fn, args, valid=False)
        assert out["error"].startswith("BLOCKED") and not done
    # no account, or nothing to change: refused, token not consumed
    for fn, args in ((g.cmd_update_event, _uargs(email="", confirm_token="tok", start="x")),
                     (g.cmd_delete_event, _dargs(email="", confirm_token="tok")),
                     (g.cmd_update_event, _uargs(confirm_token="tok"))):
        out, done, checked = _gated(fn, args)
        assert "error" in out and not done and not checked


class _Svc:
    """A Google service that records every events() call."""
    def __init__(self, log):
        self.log = log

    def events(self):
        return self

    def __getattr__(self, name):
        def call(**kw):
            self.log.append((name, kw))
            return types.SimpleNamespace(execute=lambda: {"id": kw.get("eventId", "N1")})
        return call


def test_a_button_click_reaches_the_library_with_its_own_approval():
    """The CLI consumes the button token and hands the library a one-time
    approval for that account, calendar and event; the library gate lets
    exactly that write through, and a direct call without one writes
    nothing."""
    log = []
    with patch.object(ga, "_validate_and_consume_token", lambda *a: {"ok": True}), \
         patch.object(g, "_build_service", lambda email, interactive=False: _Svc(log)):
        assert g.cmd_update_event(_uargs(confirm_token="tok", summary="p1 moved"))[
            "status"] == "updated"
        assert g.cmd_delete_event(_dargs(confirm_token="tok"))["status"] == "deleted"
        out = g.cmd_create_event(_args(confirm_token="tok"))
        assert out["status"] == "created", out
        writes = [(n, kw["calendarId"]) for n, kw in log if n in ("patch", "delete", "insert")]
        assert writes == [("patch", FAMILY), ("delete", FAMILY), ("insert", FAMILY)]
        log.clear()
        assert "BLOCKED" in g._patch_event(FAMILY, "E1", FAMILY, {"summary": "x"}, hint="")["error"]
        assert "BLOCKED" in g._remove_event(FAMILY, "E1", FAMILY)["error"]
        assert log == []


def test_update_and_delete_tokens_are_bound_to_their_verb_and_event(tmp_path):
    with patch.object(ga, "PENDING_CONFIRMS_FILE", tmp_path / "pending.json"), \
         patch.object(ga, "PENDING_CONFIRMS_LOCK", tmp_path / "pending.lock"), \
         patch.object(g, "_remove_event", lambda *a, **kw: {"status": "deleted"}), \
         patch.object(g, "_patch_event", lambda *a, **kw: {"status": "updated"}):
        upd = ga._create_confirmation("update_google_event", g.confirm_key(FAMILY, "E1"))
        other = ga._create_confirmation("delete_google_event", g.confirm_key(FAMILY, "E2"))
        outlook = ga._create_confirmation("delete_event", f"{FAMILY}:E1")
        dele = ga._create_confirmation("delete_google_event", g.confirm_key(FAMILY, "E1"))
        assert "error" in g.cmd_delete_event(_dargs(confirm_token=upd))
        assert "error" in g.cmd_delete_event(_dargs(confirm_token=other))
        assert "error" in g.cmd_delete_event(_dargs(confirm_token=outlook))
        assert g.cmd_delete_event(_dargs(confirm_token=dele)) == {"status": "deleted"}
        assert g.cmd_update_event(_uargs(confirm_token=upd, start="2027-02-11T11:00:00")) == {
            "status": "updated"}
        assert "error" in g.cmd_update_event(_uargs(confirm_token=upd, start="2027-02-11T11:00:00"))


def test_the_resident_reaches_only_ea_managed_and_never_with_a_token():
    ea = M.ea_managed_calendar_id()
    for verb in ("update-event", "delete-event"):
        assert M.is_gated("google_calendar_client", verb)
        assert M.is_allowed("google_calendar_client", verb, [verb, "--event-id", "E1"])
        assert M.is_allowed("google_calendar_client", verb,
                            [verb, "--event-id", "E1", "--calendar-id", ea])
        for argv in ([verb, "--event-id", "E1", "--confirm-token", "t"],
                     [verb, "--event-id", "E1", "--confirm-token=t"],
                     [verb, "--event-id", "E1", "--email", FAMILY, "--calendar-id", FAMILY],
                     [verb, "--event-id", "E1", "--calendar-id", ea, "--confirm-token", "t"]):
            assert not M.is_allowed("google_calendar_client", verb, argv), argv
    assert not M.is_allowed("google_calendar_client", "create-event",
                            ["create-event", "--calendar-id", ea, "--confirm-token", "t"])


# --- move and delete: the bot ----------------------------------------------

def test_the_reschedule_and_delete_cards_name_the_google_calendar():
    ns = _cards_ns()
    value = json.dumps({"payload_ref": "k"})
    want = ("Calendar", f"{FAMILY} (Google)")
    change = {"subject": "Night class", "when": "s to e"}
    upd = ns["_event_update_card"]("C", "T", _read(), change, value, calendar=FAMILY)
    dele = ns["_event_delete_card"]("C", "T", _read(), value, calendar=FAMILY)
    for card in (upd, dele):
        assert want in [(f.label, f.value) for f in card.fields]
        # Google mails nothing the card could promise; the Outlook lines stay off.
        assert not any("Outlook" in ln or "cancellation" in ln for ln in card.lines)
    for card in (ns["_event_update_card"]("C", "T", _read("x"), change, value),
                 ns["_event_delete_card"]("C", "T", _read("x"), value)):
        assert "Calendar" not in [f.label for f in card.fields]


def _stage_ns():
    ns = _bot_ns({"scheduling-preferences.json": PREFS, "user-profile.json": PROFILE})
    posted, cards, payloads, reads = [], [], [], []
    ns.update(
        FRONTEND=types.SimpleNamespace(post=lambda **kw: posted.append(kw)),
        _staged_button_value=lambda base, payload: payloads.append(payload) or "v",
        _event_update_card=lambda *a, **kw: cards.append(kw) or "card",
        _event_delete_card=lambda *a, **kw: cards.append(kw) or "card",
        _read_calendar_event=lambda event_id, google="": reads.append((event_id, google))
        or _read(),
        _event_span_text=lambda s, e, *a: f"{s} to {e}",
        _event_time_local=lambda v: v,
        _deliver_confirmation=lambda *a: None)
    ns["reads"] = reads
    exec(_bot_fn("_staged_google_target"), ns)
    exec(_bot_fn("_refuse_event_card"), ns)
    exec(_bot_fn("_post_event_update_confirmation"), ns)
    exec(_bot_fn("_post_event_delete_confirmation"), ns)
    return ns, posted, cards, payloads


def test_staged_moves_and_deletes_carry_the_calendar_or_post_no_button():
    ns, posted, cards, payloads = _stage_ns()
    ns["_post_event_update_confirmation"]("C", "T", "E1", "Night class", "2027-02-11T11:00:00",
                                          "2027-02-11T12:15:00", calendar="google_family")
    ns["_post_event_delete_confirmation"]("C", "T", "E1", "Night class", calendar=FAMILY)
    assert not posted
    assert [p["calendar"] for p in payloads] == [FAMILY, FAMILY]
    assert [c["calendar"] for c in cards] == [FAMILY, FAMILY]
    # The card comes from the owner's read of that calendar, and the payload
    # keeps the read's fingerprint for the check at click time.
    assert ns["reads"] == [("E1", FAMILY), ("E1", FAMILY)]
    assert [p["event_fingerprint"] for p in payloads] == ["f" * 64] * 2
    ns["_post_event_update_confirmation"]("C", "T", "E1", "Sync", "s", "e",
                                          attendees="gray.jamison0@example.edu", calendar=FAMILY)
    ns["_post_event_delete_confirmation"]("C", "T", "E1", "Sync", calendar="nowhere")
    assert len(cards) == 2 and len(posted) == 2
    assert "No Reschedule button" in posted[0]["text"] and "cannot invite" in posted[0]["text"]
    assert "No Delete button" in posted[1]["text"] and "unknown calendar" in posted[1]["text"]
    ns["_post_event_update_confirmation"]("C", "T", "AAMk", "Sync", "s", "e",
                                          attendees="gray.jamison0@example.edu")
    assert "calendar" not in payloads[-1] and cards[-1]["calendar"] == ""


def _exec_fn(name):
    ns = {"json": json, "SCRIPT_DIR": Path("/srv/ea"),
          "log": types.SimpleNamespace(exception=lambda *a: None)}
    posted = []
    ns.update(_remote_llm_note=lambda *a: None,
              _replace_buttons_with_status=lambda *a: None,
              # The click-time re-read (test_event_card_gate covers it):
              # here the event still reads as its card showed it.
              _event_unchanged_since_card=lambda event_data, google: (True, ""),
              _recard_event_change=lambda *a: None,
              FRONTEND=types.SimpleNamespace(post=lambda **kw: posted.append(kw)))
    exec(_bot_fn("_csv"), ns)
    exec(_bot_fn(name), ns)
    return ns[name], posted


def test_the_reschedule_click_routes_a_google_event_to_the_google_client():
    run, posted = _exec_fn("_execute_event_update")
    calls = []
    with patch("subprocess.run", _fake_run(calls, "updated")):
        ok = run("C", "T", None, {"event_id": "E1", "subject": "Night class",
                                  "start": "2027-02-11T11:00:00", "end": "2027-02-11T12:15:00",
                                  "attendees": "", "location": "ECCS 1B12", "calendar": FAMILY})
    assert ok is True
    mint, upd = calls
    assert mint[mint.index("--action") + 1] == "update_google_event"
    assert mint[mint.index("--message-id") + 1] == f"{FAMILY}:E1"
    assert upd[1].endswith("google_calendar_client.py") and upd[2] == "update-event"
    assert upd[upd.index("--email") + 1] == FAMILY and upd[upd.index("--event-id") + 1] == "E1"
    assert upd[upd.index("--summary") + 1] == "Night class"
    assert upd[upd.index("--start") + 1] == "2027-02-11T11:00:00"
    assert upd[upd.index("--location") + 1] == "ECCS 1B12"
    assert "--confirm-token=TOK" in upd and "--subject" not in upd
    assert f"on {FAMILY}" in posted[0]["text"]


def test_the_delete_click_routes_a_google_event_to_the_google_client():
    run, posted = _exec_fn("_execute_event_delete")
    calls = []
    with patch("subprocess.run", _fake_run(calls, "deleted")):
        ok = run("C", "T", None, {"event_id": "E1", "subject": "Night class", "calendar": FAMILY})
    assert ok is True
    mint, dele = calls
    assert mint[mint.index("--action") + 1] == "delete_google_event"
    assert mint[mint.index("--message-id") + 1] == f"{FAMILY}:E1"
    assert dele[1].endswith("google_calendar_client.py") and dele[2] == "delete-event"
    assert dele[dele.index("--email") + 1] == FAMILY and "--confirm-token=TOK" in dele
    assert f"from {FAMILY}" in posted[0]["text"]


def test_outlook_moves_and_deletes_are_unchanged():
    run, posted = _exec_fn("_execute_event_update")
    calls = []
    with patch("subprocess.run", _fake_run(calls, "event_updated")):
        assert run("C", "T", None, {"event_id": "AAMk", "subject": "Sync", "start": "s",
                                    "end": "e", "attendees": "gray.jamison0@example.edu"}) is True
    mint, upd = calls
    assert mint[mint.index("--action") + 1] == "update_event"
    assert mint[mint.index("--message-id") + 1] == "AAMk"
    assert upd[1].endswith("graph_actions.py") and upd[upd.index("--subject") + 1] == "Sync"
    assert upd[upd.index("--attendees") + 1] == "gray.jamison0@example.edu"
    assert posted[0]["text"] == ":white_check_mark: Event rescheduled."
    run, posted = _exec_fn("_execute_event_delete")
    calls = []
    with patch("subprocess.run", _fake_run(calls, "deleted")):
        assert run("C", "T", None, {"event_id": "AAMk", "subject": "Sync"}) is True
    mint, dele = calls
    assert mint[mint.index("--action") + 1] == "delete_event"
    assert dele[1].endswith("graph_actions.py") and "--email" not in dele
    assert posted[0]["text"] == ":white_check_mark: Event deleted."
