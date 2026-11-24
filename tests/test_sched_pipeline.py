"""sched_pipeline: code owns the numbers, the model picks and phrases.

Pure tests: the model is a fake that answers the schema, the resident
runner is a fake that answers the solver. Nothing reaches the endpoint,
the mailbox or the calendar."""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import sched_pipeline as sp  # noqa: E402
import sched_render as sr  # noqa: E402

USER = {"name": "Kit Calloway", "first": "Kit", "email": "wren.fairbanks84@example.edu", "zone": "America/Denver"}
TODAY = date(2026, 10, 14)
THREAD = [
    {"id": "m1", "conversation_id": "c1", "from": {"address": "sol.ashcombe@example.org"}, "sent": "2026-10-14T14:48:00Z",
     "text": "I wanted to follow up on the application materials I sent last week."},
    {"id": "m2", "conversation_id": "c1", "from": {"address": "wren.fairbanks84@example.edu"}, "sent": "2026-10-14T16:50:00Z",
     "text": "Let's schedule a chat for early November. Any days/times that don't work for you in the first week of Nov?"},
    {"id": "m3", "conversation_id": "c1", "from": {"address": "sol.ashcombe@example.org"}, "sent": "2026-10-14T21:07:00Z",
     "text": "Any day in the first week of November works for me, though Friday would be best."},
    {"id": "m4", "conversation_id": "c1", "from": {"address": "wren.fairbanks84@example.edu"}, "sent": "2026-10-14T21:42:00Z",
     "text": "Friday Nov 6 works.", "is_draft": False},
    {"id": "m5", "conversation_id": "c1", "from": {"address": "sol.ashcombe@example.org"}, "sent": "2026-10-14T21:46:00Z",
     "text": "Friday 9:00 works well for me."},
]
OPTIONS = [
    {"start": "2026-11-02T14:15:00-07:00", "end": "2026-11-02T14:45:00-07:00"},
    {"start": "2026-11-02T14:30:00-07:00", "end": "2026-11-02T15:00:00-07:00"},
    {"start": "2026-11-03T10:30:00-07:00", "end": "2026-11-03T11:00:00-07:00"},
    {"start": "2026-11-05T09:00:00-07:00", "end": "2026-11-05T09:30:00-07:00"},
    {"start": "2026-11-06T09:30:00-07:00", "end": "2026-11-06T10:00:00-07:00"},
]
sr.label_options(OPTIONS, [{"email": "sol.ashcombe@example.org", "timezone": "America/New_York"}])
INTENT = {"mode": "propose", "sender_name": "Sol Ashcombe", "sender_email": "sol.ashcombe@example.org",
          "sender_timezone": "America/New_York", "duration_minutes": 30, "window_start": "2026-11-02",
          "window_end": "2026-11-06", "preferred_days": ["2026-11-06"], "excluded_days": [], "offered_times": [],
          "other_attendees": [], "note_to_user": ""}


def test_weeks_table_and_name_tokens():
    t = sp.weeks_table(TODAY, n=4)
    assert "week of Mon Oct 12: 2026-10-12 to 2026-10-16  (this week)" in t
    assert "week of Mon Nov 2: 2026-11-02 to 2026-11-06" in t
    assert sp.name_tokens("Follow up with Sol with times that work that week.") == ["Sol"]
    assert sp.name_tokens("Reply to the Morgan QRX email, just to Lane Corvino") == ["Morgan", "QRX", "Lane", "Corvino"]
    assert sp.name_tokens("find a time next Tuesday") == []


def test_reply_target_is_the_latest_message_not_from_the_user():
    t = sp.reply_target(THREAD, USER["email"])
    assert t["message_id"] == "m5" and t["from"] == "sol.ashcombe@example.org"
    assert sp.reply_target([THREAD[1]], USER["email"]) == {}


def test_normalize_intent_fixes_sender_zone_duration_and_dates():
    raw = {"mode": "propose", "sender_name": "Sol Ashcombe", "sender_email": USER["email"],
           "sender_timezone": "Boston", "duration_minutes": "abc", "window_start": "2026-11-06",
           "window_end": "2026-11-02", "preferred_days": ["2026-11-06", "junk"], "excluded_days": [],
           "offered_times": ["2026-11-03T14:00:00", "nope"], "other_attendees": ["Someone@Example.edu", USER["email"], "name only"]}
    i = sp.normalize_intent(raw, THREAD, USER)
    assert i["sender_email"] == "sol.ashcombe@example.org" and i["sender_fixed"]
    assert i["sender_timezone"] == ""
    assert i["duration_minutes"] == 30
    assert (i["window_start"], i["window_end"]) == ("2026-11-02", "2026-11-06")
    assert i["preferred_days"] == ["2026-11-06"]
    assert i["offered_times"] == ["2026-11-03T14:00:00"]
    assert i["other_attendees"] == ["someone@example.edu"]
    assert sp.normalize_intent({"mode": "weird"}, THREAD, USER)["mode"] == "other"
    # a colleague on the user's domain is on the user's clock
    same = sp.normalize_intent(dict(raw, sender_email="bo@example.edu", sender_timezone=""), THREAD, USER)
    assert same["sender_timezone"] == "America/Denver"


def test_solver_args_from_the_intent():
    tool, args = sp.solver_args(INTENT, today=TODAY, user_email=USER["email"])
    assert tool == "propose-meeting"
    assert (args["earliest_date"], args["latest_date"], args["duration"]) == ("2026-11-02", "2026-11-06", 30)
    att = args["attendees"][0]
    assert att["email"] == "sol.ashcombe@example.org" and att["timezone"] == "America/New_York" and att["type"] == "external"
    assert "availability" not in att
    # an excluded day is an exclusion the solver composes with the zone's
    # hours, never stated 00:00-24:00 availability that replaces them
    tool, args = sp.solver_args(dict(INTENT, excluded_days=["2026-11-04", "2026-12-01"], other_attendees=["alice@example.edu"]),
                                today=TODAY, user_email=USER["email"])
    assert args["attendees"][0]["excluded_days"] == ["2026-11-04"]
    assert "availability" not in args["attendees"][0] and args["attendees"][0]["timezone"] == "America/New_York"
    assert args["attendees"][1] == {"email": "alice@example.edu", "name": "", "type": "internal"}
    # offered exact times are validated, not proposed
    tool, args = sp.solver_args(dict(INTENT, mode="validate", offered_times=["2026-11-03T14:00:00"]), today=TODAY, user_email=USER["email"])
    assert tool == "validate-draft" and args == {"times": ["2026-11-03T14:00:00"], "duration": 30}
    # no window: the next two weeks
    tool, args = sp.solver_args(dict(INTENT, window_start="", window_end=""), today=TODAY, user_email=USER["email"])
    assert (args["earliest_date"], args["latest_date"]) == ("2026-10-15", "2026-10-28")


def test_options_from_validate_keeps_the_free_offered_times():
    # the zone is explicit: a naive offered time is on the user's clock,
    # whatever the machine's zone
    intent = dict(INTENT, mode="validate", offered_times=["2026-11-03T14:00:00", "2026-11-04T10:00:00"])
    out = {"valid": False, "issues": [
        {"proposed_time": "2026-11-03T14:00:00", "type": "interpreted_as", "detail": "..."},
        {"proposed_time": "2026-11-04T10:00:00", "type": "conflict", "detail": "NLP seminar"}]}
    opts = sp.options_from_validate(out, intent, [{"email": "sol.ashcombe@example.org", "timezone": "America/New_York"}],
                                    zone="America/Denver")
    assert [o["line"] for o in opts] == ["Tue Nov 3, 2:00-2:30 PM MT (4:00-4:30 PM ET)"]
    assert opts[0]["warnings"] == []


def test_options_from_validate_keeps_soft_warnings_on_available_times():
    # only a hard conflict makes a time unavailable; a preference warning
    # and an all-day event stay on the option as warnings
    intent = dict(INTENT, mode="validate", offered_times=["2026-11-03T06:00:00", "2026-11-03T21:30:00", "2026-11-04T10:00:00"])
    out = {"valid": False, "issues": [
        {"proposed_time": "2026-11-03T06:00:00", "type": "preference_warning", "detail": "outside working hours (09:00-17:00)"},
        {"proposed_time": "2026-11-03T21:30:00", "type": "all_day_event", "detail": "All-day event: Hold (unknown)"},
        {"proposed_time": "2026-11-04T10:00:00", "type": "conflict", "conflict_with": "Lab meeting"}]}
    opts = sp.options_from_validate(out, intent, [], zone="America/Denver")
    assert [o["label"] for o in opts] == ["Tue Nov 3, 6:00-6:30 AM MT", "Tue Nov 3, 9:30-10:00 PM MT"]
    assert opts[0]["warnings"] == ["outside working hours (09:00-17:00)"]
    assert "[warning: All-day event: Hold (unknown)]" in sp.option_listing(opts)
    # no fetch error: nothing to ask
    assert sp.fetch_error_need(out) == ""


def test_a_calendar_that_could_not_be_read_is_a_need_not_a_free_answer():
    intent = dict(INTENT, mode="validate", offered_times=["2026-11-03T14:00:00"])
    out = {"valid": True, "issues": [
        {"proposed_time": "2026-11-03T14:00:00", "type": "calendar_fetch_error", "detail": "Calendar source(s) failed to load: family"}],
        "calendar_fetch_errors": [{"account": "family", "error": "timeout"}]}
    need = sp.fetch_error_need(out)
    assert "family" in need and "cannot say" in need
    calls = []
    rec = sp.run("Reply to Sol about Tuesday", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_solver(calls, validate=out), record=False)
    assert rec["status"] == "need" and "family" in rec["need"] and rec["error"] == rec["need"]
    assert [c[0] for c in calls] == ["validate-draft"] and rec["reply"].startswith(":question:")


def test_soft_warnings_do_not_make_a_counter_offer():
    # scheduling-005: both offered times are free but outside working
    # hours; the reply confirms one of them and the user's line warns
    calls = []
    intent = dict(INTENT, mode="validate", offered_times=["2026-11-03T06:00:00", "2026-11-03T21:30:00"])
    warned = {"valid": True, "issues": [
        {"proposed_time": "2026-11-03T06:00:00", "type": "preference_warning", "detail": "outside working hours"},
        {"proposed_time": "2026-11-03T21:30:00", "type": "preference_warning", "detail": "outside working hours"}]}
    llm = _llm(intent, dict(PICK, picks=["o1"], preferred_day_available=False))
    rec = sp.run("Handle the scheduling request from Sol", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, validate=warned), record=False)
    assert rec["status"] == "ok" and rec["counter"] == 0 and [c[0] for c in calls] == ["validate-draft"]
    assert rec["picked"] == ["o1"] and "none is free" not in llm.contexts[0]
    assert "[warning: outside working hours]" in sp.option_listing(rec["options"])
    assert "(warning: outside working hours)" in rec["reply"] and "warning" not in rec["body"]


def test_check_pick_flags_and_repairs():
    good = {"picks": ["o5", "o1", "o3"], "preferred_day_available": True, "opening": "Friday works.", "closing": "Pick one."}
    problems, ids = sp.check_pick(good, INTENT, OPTIONS)
    assert problems == [] and ids == ["o5", "o1", "o3"]
    # same day twice, preferred Friday skipped, a clock in the prose, the flag wrong
    bad = {"picks": ["o1", "o2", "o9"], "preferred_day_available": True,
           "opening": "How about 2:15 PM?", "closing": "Let me know."}
    problems, ids = sp.check_pick(bad, INTENT, OPTIONS)
    assert set(problems) >= {"unknown-id", "same-day", "preferred-day-skipped", "preferred-flag-wrong", "clock-in-opening", "repaired"}
    assert ids[0] == "o5" and len({OPTIONS[int(i[1:]) - 1]["start"][:10] for i in ids}) == 3
    # no preference: the model's distinct picks stand
    problems, ids = sp.check_pick({"picks": ["o3", "o4"], "preferred_day_available": False, "opening": "", "closing": ""},
                                  dict(INTENT, preferred_days=[]), OPTIONS)
    assert problems == [] and ids == ["o3", "o4"]


def test_run_end_to_end_with_fakes_writes_nothing_and_renders_solver_lines():
    calls, listings = [], []

    def llm(messages, *, purpose, schema, **kw):
        calls.append(schema)
        if schema is sp.INTENT_SCHEMA:
            return dict(INTENT), {"latency_ms": 5, "model": "fake"}
        listings.append(messages[-1]["content"])
        return {"picks": ["o5", "o1", "o3"], "preferred_day_available": True,
                "opening": "Friday works for me. Here are a few times, Friday first:",
                "closing": "Let me know which one and I will send a Zoom invite.", "note_to_user": ""}, {"latency_ms": 5}

    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        assert argv[0] == "propose-meeting" and "--earliest-date" in argv and "2026-11-02" in argv
        opts = [dict(o) for o in OPTIONS]
        # the solver's own words for a p2 token that penalised the Monday afternoon
        opts[0]["applied_preferences"] = {"p2 Prefer mornings": 0.5}
        return 0, json.dumps({"status": "ok", "options": opts, "warnings": []}), ""

    rec = sp.run("Follow up with Sol with times that work that week.", messages=THREAD, as_of="2026-10-14T21:30:00Z",
                 today=TODAY, user=USER, prompts=sp.load_prompts(Path("/nonexistent")), llm=llm, runner=runner, record=False)
    assert rec["status"] == "ok" and rec["problems"] == []
    assert rec["as_of"] == "2026-10-14T21:30:00Z" and rec["target"]["message_id"] == "m3"
    assert rec["picked"] == ["o5", "o1", "o3"]
    assert rec["body"] == ("Hi Sol,\n\nFriday works for me. Here are a few times, Friday first:\n\n"
                           "Fri Nov 6, 9:30-10:00 AM MT (11:30 AM-12:00 PM ET)\n"
                           "Mon Nov 2, 2:15-2:45 PM MT (4:15-4:45 PM ET)\n"
                           "Tue Nov 3, 10:30-11:00 AM MT (12:30-1:00 PM ET)\n\n"
                           "Let me know which one and I will send a Zoom invite.\n\nKit\n")
    assert rec["write"] is None and "shadow" in rec["reply"]
    assert [s["step"] for s in rec["steps"]] == ["read", "intent", "solve", "pick"]
    # the penalty reaches the pick model and the user's line, never the body
    assert "o1: Mon Nov 2, 2:15-2:45 PM MT (4:15-4:45 PM ET)  [ranked lower: p2 Prefer mornings]" in listings[0]
    assert "o3: Tue Nov 3, 10:30-11:00 AM MT (12:30-1:00 PM ET)\n" in listings[0]
    assert "• Mon Nov 2, 2:15-2:45 PM MT (4:15-4:45 PM ET) (ranked lower: p2 Prefer mornings)" in rec["reply"]
    assert "• Fri Nov 6, 9:30-10:00 AM MT (11:30 AM-12:00 PM ET)\n" in rec["reply"]
    assert "ranked lower" not in rec["body"]
    assert [o["prefs"] for o in rec["options"]][:2] == [["p2 Prefer mornings"], []]


def test_run_hands_off_when_the_request_is_not_a_proposal():
    def llm(messages, *, purpose, schema, **kw):
        return dict(INTENT, mode="other", note_to_user="They already accepted Friday 9:00."), {}
    rec = sp.run("Follow up with Sol", messages=THREAD, today=TODAY, user=USER,
                 prompts=sp.load_prompts(Path("/nonexistent")), llm=llm, runner=lambda cmd: (1, "", "never"), record=False)
    assert rec["status"] == "handoff-other" and rec["intent"]["note_to_user"].startswith("They already")


def test_load_prompts_falls_back_to_defaults(tmp_path):
    p = sp.load_prompts(tmp_path)
    assert p["name"] == "default" and p["intent"] == sp.INTENT_PROMPT
    (tmp_path / "tuning").mkdir()
    (tmp_path / sp.PROMPTS_FILE).write_text(json.dumps({"name": "gen3", "intent": "X {today} {weeks} {user_name} {user_email} {user_first} {user_zone}"}))
    p = sp.load_prompts(tmp_path)
    assert p["name"] == "gen3" and p["intent"].startswith("X ") and p["pick"] == sp.PICK_PROMPT


# ── the shapes added from the shadow's hand-offs ──────────────────────

def test_with_addenda_appends_the_code_owned_lines_once():
    old = "X {today}\n- mode: propose or other.\n\nAnswer with the JSON object only."
    out = sp.with_addenda(old)
    assert out.endswith("\n\nAnswer with the JSON object only.") and 'mode "invite"' in out and "- subject:" in out
    assert "CC'd" in out and sp.with_addenda(out) == out
    assert sp.with_addenda(sp.INTENT_PROMPT) == sp.INTENT_PROMPT


def test_name_tokens_takes_a_lowercase_name_after_a_preposition():
    assert sp.name_tokens("We dropped the ball on jules again. Get back to him with a time.") == ["jules"]
    assert sp.name_tokens("Send an invite and reply both then say the options if there are any.") == []
    assert sp.name_tokens("find a time next Tuesday") == []


def test_last_conversation_for_follows_the_chat_thread():
    rows = [{"thread_ts": "t1", "conversation_id": "c-old"}, {"shadow_of": {"thread_ts": "t1"}, "conversation_id": "c-new"},
            {"thread_ts": "t2", "conversation_id": "c-other"}, {"thread_ts": "t1"}]
    assert sp.last_conversation_for("t1", rows) == "c-new"
    assert sp.last_conversation_for("t9", rows) == "" and sp.last_conversation_for("", rows) == ""


def _solver(calls, *, validate=None, propose=None):
    """A fake resident: validate-draft answers `validate`, propose-meeting
    answers in order from `propose` (a list of option lists)."""
    propose = list(propose or [])

    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        calls.append(argv)
        if argv[0] == "validate-draft":
            return 0, json.dumps(validate or {"valid": True, "issues": []}), ""
        opts = [dict(o) for o in (propose.pop(0) if propose else [])]
        return 0, json.dumps({"status": "ok", "options": opts, "warnings": []}), ""
    return runner


def _llm(intent, pick):
    def llm(messages, *, purpose, schema, **kw):
        if schema is sp.INTENT_SCHEMA:
            return dict(intent), {"latency_ms": 1, "model": "fake"}
        llm.contexts.append(messages[0]["content"])
        return dict(pick), {"latency_ms": 1}
    llm.contexts = []
    return llm


PROMPTS = sp.load_prompts(Path("/nonexistent"))
PICK = {"picks": ["o5", "o1", "o3"], "preferred_day_available": True, "opening": "Here are a few times.",
        "closing": "Let me know which works.", "note_to_user": ""}


def test_offered_times_that_fail_become_a_counter_offer():
    calls = []
    intent = dict(INTENT, mode="validate", window_start="", window_end="",
                  offered_times=["2026-11-03T14:00:00", "2026-11-04T10:00:00"])
    bad = {"valid": False, "issues": [{"proposed_time": "2026-11-03T14:00:00", "type": "conflict", "detail": "seminar"},
                                      {"proposed_time": "2026-11-04T10:00:00", "type": "conflict", "detail": "lecture"}]}
    llm = _llm(intent, PICK)
    rec = sp.run("Reply to Sol about the times", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, validate=bad, propose=[OPTIONS]), record=False)
    assert rec["status"] == "ok" and rec["counter"] == 2 and rec["picked"] == ["o5", "o1", "o3"]
    assert [c[0] for c in calls] == ["validate-draft", "propose-meeting"]
    # the window comes from the offered days when the intent named none
    assert "2026-11-03" in calls[1] and "2026-11-10" in calls[1]
    assert "none is free on the user's side" in llm.contexts[0]
    assert "None of the 2 times they offered is free" in rec["reply"]
    assert [s["step"] for s in rec["steps"]] == ["read", "intent", "validate", "solve", "pick"]


def test_offered_times_that_pass_are_the_options():
    calls = []
    intent = dict(INTENT, mode="propose", offered_times=["2026-11-03T14:00:00"])
    pick = dict(PICK, picks=["o1"])
    rec = sp.run("Any avail for Sol Tuesday 2pm?", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, pick), runner=_solver(calls), record=False)
    assert rec["status"] == "ok" and rec["counter"] == 0 and [c[0] for c in calls] == ["validate-draft"]
    assert rec["options"][0]["line"] == "Tue Nov 3, 2:00-2:30 PM MT (4:00-4:30 PM ET)"


def test_no_options_retries_without_the_ccd_attendees():
    calls = []
    intent = dict(INTENT, other_attendees=["edie@example.edu", "zev@example.edu"])
    llm = _llm(intent, PICK)
    rec = sp.run("Respond to Sol with times", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, propose=[[], OPTIONS]), record=False)
    assert rec["status"] == "ok" and rec["dropped"] == ["edie@example.edu", "zev@example.edu"]
    assert len(calls) == 2 and "edie@example.edu" in json.dumps(calls[0]) and "edie" not in json.dumps(calls[1])
    assert "No slot fits everyone" in llm.contexts[0] and "left out of the search" in rec["reply"]


def test_a_single_day_with_nothing_is_widened_by_a_week():
    calls = []
    intent = dict(INTENT, window_start="2026-11-02", window_end="2026-11-02", preferred_days=[])
    llm = _llm(intent, dict(PICK, picks=["o3"], preferred_day_available=False))
    rec = sp.run("Any avail for Sol Monday?", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, propose=[[], OPTIONS]), record=False)
    assert rec["status"] == "ok" and rec["widened_from"] == "2026-11-02"
    assert calls[0][calls[0].index("--latest-date") + 1] == "2026-11-02"
    assert calls[1][calls[1].index("--latest-date") + 1] == "2026-11-09"
    assert "Nothing is free on Mon Nov 2" in llm.contexts[0] and "widened by a week" in rec["reply"]
    # nothing anywhere: no-options, with both retries recorded
    rec = sp.run("Any avail for Sol Monday?", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_solver([], propose=[[], []]), record=False)
    assert rec["status"] == "no-options" and rec["widened_from"] == "2026-11-02"


def test_an_availability_request_lists_up_to_two_per_day():
    assert sp.wants_listing("Can you give full availability to Ash?")
    assert sp.wants_listing("Respond to Ash who asked me to post times I'm available")
    assert not sp.wants_listing("Follow up with Sol with times that work that week")
    calls = []
    llm = _llm(dict(INTENT, preferred_days=[]), {"picks": ["o1", "o2", "o3", "o4", "o5"], "preferred_day_available": False,
                                                 "opening": "These are my open times.", "closing": "Pick any.", "note_to_user": ""})
    rec = sp.run("Give Sol my full availability that week", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, propose=[OPTIONS]), record=False)
    assert rec["wide"] and rec["problems"] == [] and rec["picked"] == ["o1", "o2", "o3", "o4", "o5"]
    assert "at most two per day" in llm.contexts[0]
    problems, ids = sp.check_pick({"picks": ["o1", "o2", "o3"], "preferred_day_available": False, "opening": "", "closing": ""},
                                  dict(INTENT, preferred_days=[]), OPTIONS)
    assert "same-day" in problems and ids == ["o1", "o3", "o4"]


INVITE_THREAD = [dict(m, subject="Re: Survey outline sync") for m in THREAD]


def test_invite_builds_the_event_from_the_thread_and_stages_it_live(monkeypatch):
    staged = []
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: (staged.append((action, args, job)) or
                                                                                 ({"status": "staged", "file": "pending-event.json"}, action)))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="", duration_minutes=45,
                  other_attendees=["dana@example.edu"])
    conflict = {"valid": False, "issues": [{"proposed_time": "2026-11-06T09:00:00", "type": "conflict", "detail": "Lab meeting"}]}
    rec = sp.run("Send the calendar invite with zoom to Sol and Dana", messages=INVITE_THREAD, today=TODAY, user=USER,
                 prompts=PROMPTS, llm=_llm(intent, PICK), runner=_solver([], validate=conflict), live=True,
                 job={"run_id": "r1", "thread_ts": "t1", "channel": "C1"}, record=False)
    assert rec["status"] == "ok" and rec["kind"] == "invite" and rec["write"]["status"] == "staged"
    assert rec["event"] == {"subject": "Survey outline sync", "start": "2026-11-06T09:00:00-07:00",
                            "end": "2026-11-06T09:45:00-07:00",
                            "attendees": "dana@example.edu,sol.ashcombe@example.org", "online": True}
    assert staged[0][0] == "stage-event" and staged[0][1] == {"event": rec["event"]} and staged[0][2]["thread_ts"] == "t1"
    assert rec["reply"].startswith(":calendar: I staged an invite to dana@example.edu,sol.ashcombe@example.org: "
                                   "Survey outline sync, Fri Nov 6, 9:00-9:45 AM MT")
    assert "overlaps Lab meeting" in rec["reply"] and "your DM" not in rec["reply"]  # the bot's pointer says where
    assert rec["body"] == "" and [s["step"] for s in rec["steps"]] == ["read", "intent", "calendar", "validate"]


def _calendar(events, details=None, *, validate=None, calls=None):
    """A fake resident whose calendar-view answers `events` and get-event
    answers `details[id]`; validate-draft answers `validate`."""
    calls = calls if calls is not None else []

    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        calls.append(argv)
        if argv[0] == "calendar-view":
            return 0, json.dumps({"count": len(events), "events": events}), ""
        if argv[0] == "get-event":
            eid = argv[argv.index("--event-id") + 1]
            return 0, json.dumps((details or {}).get(eid, {"error": "not found"})), ""
        if argv[0] == "validate-draft":
            return 0, json.dumps(validate or {"valid": True, "issues": []}), ""
        return 0, json.dumps({"status": "ok", "options": [], "warnings": []}), ""
    runner.calls = calls
    return runner


def _cal_event(eid, subject, **kw):
    return dict({"id": eid, "subject": subject, "start": "2026-11-06T16:00:00", "end": "2026-11-06T16:30:00",
                 "start_local": "2026-11-06T09:00:00-07:00", "end_local": "2026-11-06T09:30:00-07:00",
                 "all_day": False, "show_as": "busy", "ea_hold": False}, **kw)


def test_a_meeting_already_on_the_calendar_is_said_and_nothing_is_staged(monkeypatch):
    staged = []
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: (staged.append(action) or
                                                                                 ({"status": "staged"}, action)))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="Scope of work meeting")
    events = [_cal_event("e1", "Survey project: scope of work (Calloway / Ashcombe / Das)")]
    rec = sp.run("Send the invite to Ashcombe if there is no time set for that yet", messages=INVITE_THREAD,
                 today=TODAY, user=USER, prompts=PROMPTS, llm=_llm(intent, PICK), runner=_calendar(events),
                 live=True, job={"run_id": "r1"}, record=False)
    assert staged == [] and rec["status"] == "ok" and rec["write"] is None
    assert rec["existing"]["id"] == "e1"
    assert rec["reply"].startswith(":calendar: You already have a time: Survey project: scope of work "
                                   "(Calloway / Ashcombe / Das), Fri Nov 6, 9:00-9:30 AM MT")
    assert rec["reply"].endswith("I staged no invite.")
    assert [s["step"] for s in rec["steps"]] == ["read", "intent", "calendar"]


def test_an_invitee_on_an_overlapping_event_makes_it_the_same_meeting(monkeypatch):
    monkeypatch.setattr(sp._sa, "live_write", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("staged")))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="Intro call")
    details = {"e1": {"attendees": [{"emailAddress": {"address": "Sol.Ashcombe@example.org"}, "type": "required"}]}}
    rec = sp.run("Send the calendar invite", messages=INVITE_THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_calendar([_cal_event("e1", "Weekly sync")], details),
                 live=True, job={"run_id": "r1"}, record=False)
    assert rec["existing"]["subject"] == "Weekly sync" and "already have a time" in rec["reply"]


def test_a_hold_or_another_meeting_at_that_time_still_stages(monkeypatch):
    staged = []
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: (staged.append(action) or
                                                                                 ({"status": "staged"}, action)))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="Intro call")
    events = [_cal_event("h1", "Hold: Ashcombe call", ea_hold=True),
              _cal_event("e2", "Lab meeting"),
              _cal_event("e3", "Ashcombe trip", all_day=True)]
    details = {"e2": {"attendees": [{"emailAddress": {"address": "dana@example.org"}}]}}
    conflict = {"valid": False, "issues": [{"proposed_time": "2026-11-06T09:00:00", "type": "conflict",
                                            "conflict_with": "Lab meeting"}]}
    calls = []
    rec = sp.run("Send the calendar invite to Sol", messages=INVITE_THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_calendar(events, details, validate=conflict, calls=calls),
                 live=True, job={"run_id": "r1"}, record=False)
    assert staged == ["stage-event"] and "existing" not in rec
    assert "That time overlaps Lab meeting on your calendar." in rec["reply"]
    # the user's own name in a subject is never a match, and only the busy
    # timed event that names no one is looked up by id
    assert [c[0] for c in calls].count("get-event") == 1


def test_an_unreadable_calendar_stages_as_before(monkeypatch):
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: ({"status": "staged"}, action))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="Intro call")
    rec = sp.run("Send the calendar invite to Sol", messages=INVITE_THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=lambda cmd: (1, "", "calendar unreachable"),
                 live=True, job={"run_id": "r1"}, record=False)
    assert rec["write"]["status"] == "staged" and "existing" not in rec


def test_invite_for_my_calendar_is_a_hold_and_shadow_writes_nothing():
    # a hold even with others on the thread: the user asked for his own calendar
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="NIST visit",
                  other_attendees=["lane@example.edu"])
    rec = sp.run("Put the NIST meeting on my own calendar with transit time", messages=INVITE_THREAD, today=TODAY,
                 user=USER, prompts=PROMPTS, llm=_llm(intent, PICK), runner=_solver([]), record=False)
    assert rec["status"] == "ok" and rec["event"] == {"subject": "NIST visit", "start": "2026-11-06T09:00:00-07:00",
                                                       "end": "2026-11-06T09:30:00-07:00", "hold": True}
    assert rec["write"] is None and rec["reply"].startswith("Would stage a hold: NIST visit, Fri Nov 6, 9:00-9:30 AM MT")
    # no agreed time: a hand-off that says why
    rec = sp.run("Send the invite", messages=INVITE_THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(dict(intent, offered_times=[]), PICK), runner=_solver([]), record=False)
    assert rec["status"] == "handoff-invite" and "no agreed time" in rec["error"]


def test_a_follow_up_with_no_name_stays_on_the_chat_threads_conversation(monkeypatch):
    monkeypatch.setattr(sp, "last_conversation_for", lambda ts: "c1" if ts == "t1" else "")
    reads = []

    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        reads.append(argv[0])
        if argv[0] == "get-conversation":
            assert "c1" in argv
            return 0, json.dumps({"messages": THREAD}), ""
        return 0, json.dumps({"status": "ok", "options": [dict(o) for o in OPTIONS], "warnings": []}), ""

    rec = sp.run("Send an invite and reply both then say the options if there are any.", today=TODAY, user=USER,
                 prompts=PROMPTS, llm=_llm(INTENT, PICK), runner=runner, job={"run_id": "r", "thread_ts": "t1"}, record=False)
    assert rec["status"] == "ok" and rec["steps"][0]["fallback"] == "the previous turn in this chat thread"
    assert reads[0] == "get-conversation" and rec["thread_ts"] == "t1"


def test_nudge_mode_reads_the_shapes_the_request_states():
    i, why = sp.nudge_mode(dict(INTENT, mode="other"), "Can you give full availability to Ash?")
    assert i["mode"] == "propose" and why == "availability request"
    i, why = sp.nudge_mode(dict(INTENT, mode="other", offered_times=["2026-10-22T10:00:00"]),
                           "Check out the emails with Lane and Sam to put into my own calendar for that day")
    assert i["mode"] == "invite" and "invite or hold" in why
    i, why = sp.nudge_mode(dict(INTENT, mode="propose", offered_times=["2026-11-03T12:00:00"]),
                           "Send the calendar invite with zoom to Oakley and Dana")
    assert i["mode"] == "invite"
    # no time in hand: "send the invite" stays what the model said
    i, why = sp.nudge_mode(dict(INTENT, mode="propose"), "Follow up with Sol with times, then send the invite")
    assert i["mode"] == "propose" and why == ""
    i, why = sp.nudge_mode(dict(INTENT, mode="reschedule", offered_times=["2026-11-03T12:00:00"]), "send the invite")
    assert i["mode"] == "reschedule"


def test_invite_names_the_conflict_and_only_the_senders_clock(monkeypatch):
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: ({"status": "staged"}, action))
    intent = dict(INTENT, mode="invite", offered_times=["2026-11-06T09:00:00"], subject="Intro call",
                  other_attendees=["dana@example.org"])
    conflict = {"valid": False, "issues": [{"proposed_time": "2026-11-06T09:00:00", "type": "conflict",
                                            "conflict_with": "Lab meeting", "conflict_start": "x", "conflict_end": "y"}]}
    rec = sp.run("Send the calendar invite with zoom to Sol and Dana", messages=INVITE_THREAD, today=TODAY, user=USER,
                 prompts=PROMPTS, llm=_llm(intent, PICK), runner=_solver([], validate=conflict), live=True,
                 job={"run_id": "r1"}, record=False)
    assert rec["event_line"] == "Fri Nov 6, 9:00-9:30 AM MT (11:00-11:30 AM ET)"
    assert "That time overlaps Lab meeting on your calendar." in rec["reply"]


def test_thread_subject_strips_reply_and_external_tags():
    assert sp.thread_subject([{"subject": "RE: [EXTERNAL] Deciding Next Steps"}]) == "Deciding Next Steps"
    assert sp.thread_subject([{"subject": "Fwd: Re: Plan"}, {"subject": ""}]) == "Plan"
    assert sp.thread_subject([]) == ""


# ── locate: names, party matches, no guessing ─────────────────────────

def test_name_tokens_skips_imperatives_and_takes_short_names():
    assert sp.name_tokens("Meet with the dean") == []
    assert sp.name_tokens("lunch with the group") == []
    assert sp.name_tokens("set up a call with sam about the DARPA review") == ["DARPA", "sam"]
    assert sp.name_tokens("Follow up with Bo with times that work next week") == ["Bo"]
    assert sp.name_tokens("Arrange a time to meet Oakley") == ["Oakley"]
    assert sp.name_tokens("Ping Sol about Tuesday") == ["Sol"]
    # a verb that is also a name counts after the first word
    assert sp.name_tokens("Propose times to Ping") == ["Ping"]
    assert sp.name_tokens("Handle the scheduling request from Oakley") == ["Oakley"]
    assert sp.name_tokens("Put the NIST meeting on my own calendar") == ["NIST"]


def _search(rows_by_query):
    """A fake resident whose search-mail answers from `rows_by_query`."""
    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        if argv[0] == "search-mail":
            q = argv[argv.index("--query") + 1]
            return 0, json.dumps(rows_by_query.get(q, [])), ""
        return 1, "", "unexpected"
    return runner


ROW_SOL = {"conversation_id": "c-sol", "subject": "Survey outline", "from": "sol.ashcombe@example.org",
           "to": ["wren.fairbanks84@example.edu"], "received": "2026-10-13T10:00:00Z"}
ROW_NOISE = {"conversation_id": "c-news", "subject": "Newsletter: Sol power on campus", "from": "news@example.com",
             "to": ["wren.fairbanks84@example.edu"], "received": "2026-10-14T10:00:00Z"}


def test_locate_takes_a_single_hit_only_when_a_name_is_on_its_parties():
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the model is not asked"))
    cid, d = sp.locate_thread("Follow up with Sol with times", run_id="r", runner=_search({"Sol": [ROW_SOL]}),
                              llm=boom, user=USER, prompts=PROMPTS)
    assert cid == "c-sol" and d["why"] == "one conversation with a named party"
    # the newest hit names no party: the older one with Sol on it wins,
    # and the model is not asked
    cid, d = sp.locate_thread("Follow up with Sol with times", run_id="r",
                              runner=_search({"Sol": [ROW_NOISE, ROW_SOL]}), llm=boom, user=USER, prompts=PROMPTS)
    assert cid == "c-sol" and d["candidates"] == 2
    # two threads with Sol on them: the model chooses among those two only
    asked = []

    def llm(messages, **kw):
        asked.append(messages[0]["content"])
        return {"conversation_id": "c-sol2", "why": "the newer survey thread"}, {}
    rows = [ROW_NOISE, ROW_SOL, dict(ROW_SOL, conversation_id="c-sol2", subject="Reading group")]
    cid, d = sp.locate_thread("Follow up with Sol with times", run_id="r",
                              runner=_search({"Sol": rows}), llm=llm, user=USER, prompts=PROMPTS)
    assert cid == "c-sol2" and d["candidates"] == 2 and "c-news" not in asked[0] and "c-sol2" in asked[0]
    # a lone hit with no named party is a question, not a thread
    cid, d = sp.locate_thread("Follow up with Sol with times", run_id="r", runner=_search({"Sol": [ROW_NOISE]}),
                              llm=lambda *a, **k: ({"conversation_id": "", "why": "none fits"}, {}),
                              user=USER, prompts=PROMPTS)
    assert cid == "" and "Which thread" in d["need"]


def test_locate_never_falls_back_to_the_newest_conversation():
    rows = {"Sol": [dict(ROW_NOISE, from_name="Sol Daily", **{"from": "sol@news.example.com"}),
                    dict(ROW_SOL, subject="Another thread with Sol", conversation_id="c-sol2")]}
    fail = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("endpoint down"))
    cid, d = sp.locate_thread("Follow up with Sol", run_id="r", runner=_search(rows), llm=fail, user=USER, prompts=PROMPTS)
    assert cid == "" and "could not tell" in d["need"] and "newest" not in d["why"]
    cid, d = sp.locate_thread("Follow up with Sol", run_id="r", runner=_search(rows),
                              llm=lambda *a, **k: ({"conversation_id": "", "why": ""}, {}), user=USER, prompts=PROMPTS)
    assert cid == "" and "None of the 2 threads" in d["need"]
    cid, d = sp.locate_thread("Follow up with Sol", run_id="r", runner=_search(rows),
                              llm=lambda *a, **k: ({"conversation_id": "c-unlisted", "why": ""}, {}), user=USER, prompts=PROMPTS)
    assert cid == ""
    # no name, nothing found: each a need
    assert "names no one" in sp.locate_thread("Meet with the dean", run_id="r", runner=_search({}), user=USER, prompts=PROMPTS)[1]["need"]
    assert "no email thread for Sol" in sp.locate_thread("Reply to Sol", run_id="r", runner=_search({}), user=USER, prompts=PROMPTS)[1]["need"]


def test_run_turns_a_locate_need_into_a_need_record_not_the_chat_threads_conversation(monkeypatch):
    monkeypatch.setattr(sp, "last_conversation_for", lambda ts: "c-previous")
    rec = sp.run("Follow up with Sol with times", today=TODAY, user=USER, prompts=PROMPTS, llm=_llm(INTENT, PICK),
                 runner=_search({"Sol": [ROW_NOISE]}), job={"run_id": "r", "thread_ts": "t1"}, record=False)
    assert rec["status"] == "need" and "Which thread" in rec["need"] and rec["error"] == rec["need"]
    assert rec["steps"][0]["step"] == "locate" and "need" not in rec["steps"][0]


# ── the invite time is the one the other side agreed to ───────────────

THREE_TIMES = ["2026-11-03T10:00:00", "2026-11-04T14:00:00", "2026-11-05T09:00:00"]
TZ_DENVER = ZoneInfo("America/Denver")


def test_agreed_time_is_the_one_named_in_the_latest_reply():
    thread = INVITE_THREAD + [{"id": "m6", "conversation_id": "c1", "from": {"address": "sol.ashcombe@example.org"},
                               "sent": "2026-10-15T10:00:00Z", "text": "Wednesday at 2 PM works best for me."}]
    assert sp.agreed_time(THREE_TIMES, thread, USER, tz=TZ_DENVER) == ("2026-11-04T14:00:00", "")
    # named on the sender's clock (4 PM ET is 2 PM MT)
    thread[-1]["text"] = "4:00 PM ET on Wednesday Nov 4 is perfect."
    assert sp.agreed_time(THREE_TIMES, thread, USER, tz=TZ_DENVER, sender_tz="America/New_York")[0] == "2026-11-04T14:00:00"
    # the day alone settles it when the clock is not repeated
    thread[-1]["text"] = "Let's do Thursday."
    assert sp.agreed_time(THREE_TIMES, thread, USER, tz=TZ_DENVER)[0] == "2026-11-05T09:00:00"
    # the user's own last message does not count as agreement
    thread[-1] = {"id": "m7", "conversation_id": "c1", "from": {"address": USER["email"]}, "own": True,
                  "sent": "2026-10-15T11:00:00Z", "text": "Wednesday at 2 PM then?"}
    _t, why = sp.agreed_time(THREE_TIMES, thread, USER, tz=TZ_DENVER)
    assert _t == "" and "3 times are in play" in why
    assert sp.agreed_time(THREE_TIMES[:1], [], USER, tz=TZ_DENVER) == (THREE_TIMES[0], "")


def test_invite_stages_the_accepted_time_not_the_first_offered(monkeypatch):
    staged = []
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: (staged.append(args) or
                                                                                 ({"status": "staged", "file": "pending-event.json"}, action)))
    thread = INVITE_THREAD + [{"id": "m6", "conversation_id": "c1", "subject": "Re: Survey outline sync",
                               "from": {"address": "sol.ashcombe@example.org"}, "sent": "2026-10-15T10:00:00Z",
                               "text": "The second one, Wednesday 2:00 PM, works for me. Send the invite."}]
    intent = dict(INTENT, mode="invite", offered_times=list(THREE_TIMES), subject="Survey outline sync")
    calls = []
    rec = sp.run("Send the calendar invite with zoom to Sol", messages=thread, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_solver(calls), live=True, job={"run_id": "r1"}, record=False)
    assert rec["status"] == "ok" and rec["event"]["start"] == "2026-11-04T14:00:00-07:00"
    assert staged[0]["event"]["start"] == "2026-11-04T14:00:00-07:00"
    assert "2026-11-04T14:00:00-07:00" in json.dumps(calls[0]) and "2026-11-03" not in json.dumps(calls[0])
    # ambiguous: nothing is staged, the user is asked
    staged.clear()
    thread[-1]["text"] = "Any of those is fine with me."
    rec = sp.run("Send the calendar invite with zoom to Sol", messages=thread, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=_llm(intent, PICK), runner=_solver([]), live=True, job={"run_id": "r1"}, record=False)
    assert rec["status"] == "need" and "3 times are in play" in rec["need"] and staged == [] and "event" not in rec
    # a time already past: nothing is staged either
    rec = sp.run("Send the invite to Sol", messages=INVITE_THREAD, today=date(2026, 11, 10), user=USER, prompts=PROMPTS,
                 llm=_llm(dict(intent, offered_times=["2026-11-06T09:00:00"]), PICK), runner=_solver([]), live=True,
                 job={"run_id": "r1"}, record=False)
    assert rec["status"] == "need" and "has passed" in rec["need"] and staged == []


# ── the user's zone label, the video platform, the profile ────────────

def test_option_labels_carry_the_users_zone_not_a_constant():
    opts = [{"start": "2026-11-03T10:30:00-07:00", "end": "2026-11-03T11:00:00-07:00"},
            {"start": "2026-07-03T10:30:00-06:00", "end": "2026-07-03T11:00:00-06:00"}]
    sr.label_options(opts, [], user_zone="Europe/London")
    assert opts[0]["label"] == "Tue Nov 3, 5:30-6:00 PM GMT" and opts[1]["label"] == "Fri Jul 3, 5:30-6:00 PM BST"
    sr.label_options(opts, [], user_zone="America/New_York")
    assert opts[0]["label"] == "Tue Nov 3, 12:30-1:00 PM ET"
    sr.label_options(opts, [], user_zone="Asia/Kolkata")
    assert opts[0]["label"].endswith(" IST")
    assert sr.zone_label("America/Denver", sr.parse_dt(opts[0]["start"])) == "MT"
    assert sr.user_zone_name("Europe/Zurich") == "Europe/Zurich" and sr.user_zone_name("")


def test_invite_regex_matches_any_platform_word_and_the_closing_names_the_platform(monkeypatch):
    for word in ("zoom", "teams", "a Teams", "meet", "webex", "online", "video"):
        assert sp._INVITE_RE.search(f"send an invite with {word} to Bo"), word
    assert not sp._INVITE_RE.search("invite them over for dinner with family")
    import institution
    monkeypatch.setattr(institution, "video_platform", lambda: "Microsoft Teams")
    body = sp.render_reply({"picks": ["o1"], "opening": "Here are times.", "closing": ""}, ["o1"], OPTIONS, INTENT, USER)
    assert "I will send a Microsoft Teams invite." in body and "Zoom" not in body
    monkeypatch.setattr(institution, "video_platform", lambda: "")
    assert "I will send a calendar invite." in sp.render_reply({"picks": ["o1"], "opening": "", "closing": ""}, ["o1"], OPTIONS, INTENT, USER)


def test_an_unreadable_profile_is_a_need_not_an_unsigned_reply():
    import pytest
    with pytest.raises(sp.ProfileError):
        sp.user_profile(reader=lambda p: "")
    with pytest.raises(sp.ProfileError):
        sp.user_profile(reader=lambda p: json.dumps({"email": "x@example.edu"}))
    assert sp.user_profile(reader=lambda p: json.dumps({"name": "Kit Calloway", "email": "k@example.edu"}))["first"] == "Kit"
    import unittest.mock as um
    with um.patch.object(sp, "user_profile", side_effect=sp.ProfileError("no name")):
        rec = sp.run("Follow up with Sol", messages=THREAD, today=TODAY, prompts=PROMPTS, llm=_llm(INTENT, PICK),
                     runner=_solver([]), record=False)
    assert rec["status"] == "need" and "profile" in rec["need"] and rec["body"] == ""


def test_dropped_attendees_make_the_reply_go_to_the_sender_only(monkeypatch):
    writes = []
    monkeypatch.setattr(sp._sa, "live_write", lambda action, args, job, **kw: (writes.append((action, args)) or
                                                                                 ({"status": "created", "id": "d1"}, action)))
    calls = []
    intent = dict(INTENT, other_attendees=["edie@example.edu"])
    llm = _llm(intent, PICK)
    rec = sp.run("Respond to Sol with times", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
                 llm=llm, runner=_solver(calls, propose=[[], OPTIONS]), live=True, job={"run_id": "r1"}, record=False)
    assert rec["status"] == "ok" and rec["dropped"] == ["edie@example.edu"]
    assert writes[0][0] == "create-reply" and writes[0][1]["sender_only"] is True
    assert "may not make it" not in llm.contexts[0] and "goes to them alone" in llm.contexts[0]
    assert "the reply goes to Sol Ashcombe only" in rec["reply"]
    # nothing dropped: reply-all stays the default
    writes.clear()
    sp.run("Respond to Sol with times", messages=THREAD, today=TODAY, user=USER, prompts=PROMPTS,
           llm=_llm(INTENT, PICK), runner=_solver([], propose=[OPTIONS]), live=True, job={"run_id": "r1"}, record=False)
    assert "sender_only" not in writes[0][1]


# ── the solver: an excluded day composes with the zone's hours ────────

def test_an_excluded_day_keeps_a_london_attendee_inside_london_hours(tmp_path, monkeypatch):
    import os
    import importlib
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "k@example.edu", "timezone": "America/Denver"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    import scheduling_engine as se
    se = importlib.reload(se)
    assert se.USER_TZ.key == "America/Denver"
    earliest, latest = date(2026, 11, 2), date(2026, 11, 4)
    monkeypatch.setattr(se, "_get_organizer_busy_intervals", lambda latest_utc: ([], []))
    monkeypatch.setattr(se, "_get_internal_busy_slots", lambda *a, **k: {})
    monkeypatch.setattr(se, "_non_blocking_calendars", lambda: set())
    insts = [{"date": earliest + timedelta(days=i), "token": "wh", "start_min": 9 * 60, "end_min": 17 * 60,
              "label": "wh", "all_day": False} for i in range(3)]
    token_constraints = {"instances": insts, "has_wh": True}
    spec = {"duration_minutes": 30, "earliest_date": earliest.isoformat(), "latest_date": latest.isoformat(),
            "attendees": [{"email": "ira@example.co.uk", "name": "Ira", "type": "external",
                           "timezone": "Europe/London", "excluded_days": ["2026-11-03"]}],
            "soft_constraints": []}
    slot_info, hard_blocked, _soft, _ev, infeasible = se._build_candidate_slots(spec, {}, token_constraints)
    london = ZoneInfo("Europe/London")
    free = [slot_info["origin"] + timedelta(minutes=15 * s) for s in range(slot_info["total_slots"]) if s not in hard_blocked]
    assert free, "the London working window and the user's hours overlap"
    assert all(t.date() != date(2026, 11, 3) for t in free)
    assert "2026-11-03" in infeasible and "Ira" in infeasible["2026-11-03"]
    # nothing after 17:00 London on the days left (the zone hours survive the exclusion)
    assert all(t.astimezone(london).hour < 17 for t in free)
    assert all(9 <= t.astimezone(se.USER_TZ).hour for t in free)
    # a time outside London hours is blocked: 4 PM MT is 11 PM London
    four_pm = (datetime(2026, 11, 2, 16, 0, tzinfo=se.USER_TZ) - slot_info["origin"]) // timedelta(minutes=15)
    assert four_pm in hard_blocked
