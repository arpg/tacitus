"""sched_agent: the scheduling shadow agent. The switch gates every entry
point, the keep-from-Anthropic list labels turns, the step protocol survives
bad steps and stops at its budget, write tools are recorded and never
run, reads run as the resident, and the checks and report score both
sides. The model and the tools are fakes; nothing leaves the test."""
import json
import os
import stat
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import remote_llm as rl  # noqa: E402
import sched_agent as sa  # noqa: E402

TZ = ZoneInfo("America/Denver")
TODAY = date(2026, 10, 8)


def _cfg(scheduling="shadow", until="2099-12-31"):
    return {"endpoint": "https://example.invalid/v1", "model": "m", "shadow_until": until,
            "mail": "off", "scheduling": scheduling,
            "keep_from_anthropic": {"domains": ["mil"], "addresses": ["someone@x.edu"],
                                    "admissions": True}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "config", lambda: _cfg())
    monkeypatch.setattr(sa, "build_prompt", lambda job: (
        [{"role": "system", "content": "rules"},
         {"role": "user", "content": str(job.get("task_block") or "")}], TZ))
    return priv / "remote-llm"


class FakeChat:
    """Answers each call with the next scripted step."""

    def __init__(self, steps, latency_ms=900):
        self.steps = list(steps)
        self.calls = []
        self.latency_ms = latency_ms

    def __call__(self, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        if not self.steps:
            raise AssertionError("chat called more often than scripted")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, {"latency_ms": self.latency_ms}


def step(action, args=None, note="n"):
    return {"action": action, "args_json": json.dumps(args or {}), "note": note}


class FakeRunner:
    def __init__(self, outputs=None):
        self.cmds = []
        self.outputs = outputs or {}

    def __call__(self, cmd, timeout=0):
        self.cmds.append(cmd)
        verb = cmd[cmd.index("python3") + 2]
        out = self.outputs.get(verb, {"status": "ok"})
        if callable(out):
            out = out(cmd)
        return 0, json.dumps(out), ""


PROPOSE_OUT = {"status": "ok", "options": [
    {"rank": 1, "start": "2026-10-13T14:00:00-06:00", "end": "2026-10-13T14:30:00-06:00"}],
    "conditional_options": []}


def _job(**kw):
    job = {"run_id": "r1", "ts": datetime.now().astimezone().isoformat(), "thread_ts": "t1",
           "lane": "sched-channel", "mixed": False, "user_msg": "find time",
           "task_block": "Find 30 minutes with bo@example.edu next week",
           "claude": {"reply": "How does Tue Oct 13 2:00-2:30pm sound?",
                      "tool_calls": [["Bash", 2.0, "scheduling_engine.py propose-meeting --duration 30"]],
                      "elapsed_s": 40.0, "returncode": 0}}
    job.update(kw)
    return job


# ── the switch ────────────────────────────────────────────────────────

def test_switch_off_makes_every_entry_a_noop(store, monkeypatch):
    monkeypatch.setattr(rl, "config", lambda: _cfg(scheduling="off"))
    spawned = []
    monkeypatch.setattr(sa.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    sa.enqueue_shadow(_job())
    sa.note_claude_staged("c", "t1", {"action": "event_create"})
    sa.note_event_outcome("create", "t1", {"start": "x"}, True)
    assert spawned == []
    assert not (store / "sched-jobs").exists()
    assert not (store / "claude-staged.jsonl").exists()
    assert not (store / "event-outcomes.jsonl").exists()


def test_switch_past_end_date_reads_off(store, monkeypatch):
    monkeypatch.setattr(rl, "config", lambda: _cfg(until="2000-01-01"))
    assert not sa._shadow_on()


def test_enqueue_writes_private_job_and_spawns_runner(store, monkeypatch, tmp_path):
    spawned = []

    class P:
        def __init__(self, argv, **kw):
            spawned.append((argv, kw))

    monkeypatch.setattr(sa.subprocess, "Popen", P)
    import log_paths
    monkeypatch.setattr(log_paths, "owner_log", lambda name: tmp_path / name)
    sa.enqueue_shadow(_job(run_id="abc/../x"))
    jobs = list((store / "sched-jobs").iterdir())
    assert len(jobs) == 1 and jobs[0].name == "abc_.._x.json"
    assert stat.S_IMODE(jobs[0].stat().st_mode) == 0o600
    argv, kw = spawned[0]
    assert argv[-2:] == ["run-job", str(jobs[0])]
    assert kw["start_new_session"] is True


def test_enqueue_never_raises(store, monkeypatch):
    monkeypatch.setattr(rl, "store_dir", lambda: (_ for _ in ()).throw(OSError("boom")))
    sa.enqueue_shadow(_job())   # logged, not raised


def test_notes_record_when_on(store):
    sa.note_claude_staged("c", "t1", {"action": "event_create", "start": "s", "_route": {}})
    sa.note_event_outcome("create", "t1", {"start": "2026-10-13T14:00:00-06:00"}, True)
    staged = rl.read("claude-staged.jsonl")
    assert staged[0]["action"] == {"action": "event_create", "start": "s"}
    assert rl.read("event-outcomes.jsonl")[0]["kind"] == "create"


# ── the agent loop ────────────────────────────────────────────────────

def test_listed_address_in_task_labels_the_turn_and_it_runs(store):
    chat = FakeChat([step("reply", {"text": "Which day suits you?"})])
    turn, _, _ = sa.run_agent(_job(task_block="From: someone@army.mil can we meet?"),
                              chat=chat, runner=FakeRunner())
    assert turn["status"] == "ok" and turn["keep_from_anthropic"] == "listed domain mil"
    assert len(chat.calls) == 1


def test_listed_address_in_tool_output_labels_the_turn_and_reaches_the_model(store):
    runner = FakeRunner({"get-conversation": {"messages": [{"from": "someone@x.edu"}]}})
    chat = FakeChat([step("get-conversation", {"conversation_id": "c1"}),
                     step("reply", {"text": "Which day suits you?"})])
    turn, _, msgs = sa.run_agent(_job(), chat=chat, runner=runner)
    assert turn["status"] == "ok" and turn["keep_from_anthropic"] == "listed address"
    assert any("someone@x.edu" in m["content"] for m in msgs)
    assert not sa.run_agent(_job(), chat=FakeChat([step("reply", {"text": "ok"})]),
                            runner=FakeRunner())[0]["keep_from_anthropic"]


def test_reads_run_as_the_resident_with_a_shadow_run_id(store):
    runner = FakeRunner({"propose-meeting": PROPOSE_OUT})
    chat = FakeChat([step("propose-meeting", {"duration": 30, "attendees": [{"email": "a@b"}]}),
                     step("reply", {"text": "How does Tue Oct 13 2:00-2:30pm sound?"})])
    turn, results, _ = sa.run_agent(_job(), chat=chat, runner=runner)
    assert turn["status"] == "ok"
    cmd = runner.cmds[0]
    assert cmd[:5] == ["sudo", "-n", "-u", "ea-bot", "env"]
    assert "EA_RUN_ID=shadow-r1" in cmd
    assert not any(c.startswith("GRAPH_CLIENT_SECRET") for c in cmd)
    i = cmd.index("python3")
    assert cmd[i + 1].endswith("/ea-system/preprocessor/scheduling_engine.py")
    assert cmd[i + 2:i + 4] == ["propose-meeting", "--duration"]
    assert results[0]["parsed"]["options"][0]["rank"] == 1
    assert turn["solver_options"] == [["2026-10-13T14:00:00-06:00", "2026-10-13T14:30:00-06:00"]]


def test_write_tools_are_recorded_and_never_run(store):
    runner = FakeRunner()
    chat = FakeChat([
        step("create-reply", {"message_id": "m", "conversation_id": "c", "body": "Tuesday at 2 works."}),
        step("stage-event", {"event": {"subject": "Sync", "start": "2026-10-13T14:00:00",
                                       "end": "2026-10-13T14:30:00", "online": True}}),
        step("ea-managed-hold", {"op": "create", "start": "2026-10-13T14:00:00"}),
        step("clear-hold", {"event_id": "e1"}),
        step("log-decision", {"event": "Sync", "action": "kept"}),
        step("reply", {"text": "Draft ready."}),
    ])
    turn, _, msgs = sa.run_agent(_job(), chat=chat, runner=runner)
    assert turn["status"] == "ok"
    assert runner.cmds == []
    assert [w["tool"] for w in turn["writes"]] == [
        "create-reply", "stage-event", "ea-managed-hold", "clear-hold", "log-decision"]
    assert '"draft_id": "shadow-draft-1"' in msgs[3]["content"]


def test_bad_steps_cost_a_step_and_come_back_as_errors(store):
    chat = FakeChat([
        {"action": "propose-meeting", "args_json": "not json", "note": ""},
        step("propose-meeting", {"duration": 30}),                 # attendees missing
        step("validate-draft", {"times": ["x"], "bogus": 1}),      # unknown key
        step("stage-event", {"event": {"subject": "S"}}),          # start/end missing
        step("reply", {"text": ""}),                               # empty reply
        step("reply", {"text": "Which day suits you?"}),
    ])
    turn, _, msgs = sa.run_agent(_job(), chat=chat, runner=FakeRunner())
    assert turn["status"] == "ok"
    assert turn["schema_failures"] == 5
    errors = [m["content"] for m in msgs if m["content"].startswith("OBSERVATION")]
    assert "not valid JSON" in errors[0] and "needs attendees" in errors[1]
    assert "takes no bogus" in errors[2] and "needs start" in errors[3]


def test_budget_stops_a_turn_that_never_replies(store):
    chat = FakeChat([step("check-date", {"date": "2026-10-13"})] * sa.STEP_BUDGET)
    turn, _, _ = sa.run_agent(_job(), chat=chat, runner=FakeRunner())
    assert turn["status"] == "budget" and len(chat.calls) == sa.STEP_BUDGET
    assert turn["reply"] == ""


def test_model_error_ends_the_turn(store):
    chat = FakeChat([rl.RemoteLLMError("HTTP 503")])
    turn, _, _ = sa.run_agent(_job(), chat=chat, runner=FakeRunner())
    assert turn["status"] == "error" and "503" in turn["error"]


def test_cold_start_is_flagged(store):
    chat = FakeChat([step("reply", {"text": "Which day?"})], latency_ms=200_000)
    turn, _, _ = sa.run_agent(_job(), chat=chat, runner=FakeRunner())
    assert turn["cold_start"] is True


def test_check_date_answers_locally(store):
    assert sa._check_date({"date": "2026-05-27", "weekday": "Tuesday"}) == {
        "date": "2026-05-27", "weekday": "Wednesday", "matches": False}
    with pytest.raises(sa.StepError):
        sa._check_date({"date": "May 27"})


def test_argv_builds_flags_by_kind():
    assert sa._argv_for("check-attendees", {"attendees": ["a@x", "b@x"], "start": "s", "end": "e",
                                            "ignore_event": ["i1", "i2"]}) == [
        "check-attendees", "--attendees", "a@x,b@x", "--start", "s", "--end", "e",
        "--ignore-event", "i1", "--ignore-event", "i2"]
    assert sa._argv_for("ledger-list", {"has_deadline": True, "limit": "5"}) == [
        "list", "--has-deadline", "--limit", "5"]
    with pytest.raises(sa.StepError):
        sa._argv_for("propose-meeting", {"attendees": "{bad"})


def test_trim_blanks_oldest_observations_first():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(4):
        msgs.append({"role": "assistant", "content": "{}"})
        msgs.append({"role": "user", "content": f"OBSERVATION (x):\n" + "y" * 100_000})
    sa._trim(msgs)
    assert sum(len(m["content"]) for m in msgs) <= sa.CONV_CHARS
    assert msgs[3]["content"].endswith("[trimmed]")
    assert not msgs[-1]["content"].endswith("[trimmed]")


# ── time parsing and checks ───────────────────────────────────────────

def _fmt(times):
    return [(t["start"].strftime("%m-%d %H:%M"), t["end"].strftime("%H:%M") if t["end"] else None)
            for t in times]


def test_time_parsing_handles_the_common_shapes():
    times, mm = sa.extract_times("How do Tue Oct 13 2:00-2:30pm or Wed Oct 14 10-10:30am sound?",
                                 TODAY, TZ)
    assert _fmt(times) == [("10-13 14:00", "14:30"), ("10-14 10:00", "10:30")] and mm == []
    assert _fmt(sa.extract_times("Tuesday, October 13 at 2 pm works.", TODAY, TZ)[0]) == [("10-13 14:00", None)]
    assert _fmt(sa.extract_times("Thu 10/15 3:30 pm or Fri 10/16 9am", TODAY, TZ)[0]) == [
        ("10-15 15:30", None), ("10-16 09:00", None)]
    iso = sa.extract_times("Holding 2026-10-15T09:00:00-06:00.", TODAY, TZ)[0]
    assert iso[0]["start"].isoformat() == "2026-10-15T09:00:00-06:00"
    # a meeting count or a 1:1 is not a time
    assert sa.extract_times("Oct 13: a 30-minute 1:1", TODAY, TZ)[0] == []
    # January named in October is next year
    assert sa.extract_times("Jan 5 at 10am", TODAY, TZ)[0][0]["start"].year == 2027


def test_a_clock_in_another_zone_is_read_in_that_zone():
    # the same window on two clocks is one time (an offer the EA writes)
    two = sa.extract_times("Tue Nov 3, 3:30-4:00 PM MT (1:30-2:00 PM AKT) or Wed Nov 4, 3:15-3:45 PM MT "
                           "(5:15-5:45 PM ET)", TODAY, TZ)[0]
    assert _fmt(two) == [("11-03 15:30", "16:00"), ("11-04 15:15", "15:45")]
    # "to noon" ends a range; the zone after it still applies
    assert _fmt(sa.extract_times("Monday, November 30, 9:00 to 10:00 am MT (11:00 am to noon ET)", TODAY, TZ)[0]) == [
        ("11-30 09:00", "10:00")]
    # a city clock, and one a day ahead there
    assert _fmt(sa.extract_times("Tue Nov 3, 5:00-5:30 PM MT (12:00-12:30 AM Wed in London)", TODAY, TZ)[0]) == [
        ("11-03 17:00", "17:30")]
    assert _fmt(sa.extract_times("Tue Nov 3, 5:00-5:30 PM MT (9:00-9:30 AM Wed in Tokyo)", TODAY, TZ)[0]) == [
        ("11-03 17:00", "17:30")]
    # a time named only in another zone is converted; daylight labels follow the date
    assert _fmt(sa.extract_times("Could we do Thu Nov 5 at 3 pm Eastern?", TODAY, TZ)[0]) == [("11-05 13:00", None)]
    assert _fmt(sa.extract_times("Thu Oct 15 at 3 pm EST", TODAY, TZ)[0]) == [("10-15 13:00", None)]
    # a place is not a zone; the user's own region keeps the user's zone
    assert _fmt(sa.extract_times("Tue Nov 3 at 3pm in Room 101", TODAY, TZ)[0]) == [("11-03 15:00", None)]
    phx = ZoneInfo("America/Phoenix")
    t = sa.extract_times("Tue Nov 3 at 3pm MST", TODAY, phx)[0][0]
    assert t["start"].tzinfo == phx and t["start"].hour == 15


def test_weekday_mismatch_is_a_rule_break():
    assert "weekday-mismatch" in sa.rule_breaks("Monday Oct 13 at 11:15", today=TODAY, tz=TZ)
    assert sa.rule_breaks("Tuesday Oct 13 at 11:15", today=TODAY, tz=TZ) == []


def test_rule_breaks_by_where_they_appear():
    assert sa.rule_breaks("Here — **bold** on Teams via the tool", today=TODAY, tz=TZ) == [
        "dash", "double-asterisk", "teams", "tool-talk"]
    # an attendee who cannot make it is a report in the thread, not a decline
    assert sa.rule_breaks("Lane can't make Tuesday.", today=TODAY, tz=TZ) == []
    assert "declines" in sa.rule_breaks("", drafts=["I'm afraid I can't attend."], today=TODAY, tz=TZ) or \
        "declines" in sa.rule_breaks("", drafts=["I cannot attend on Tuesday."], today=TODAY, tz=TZ)
    # tools in an email body are fine
    assert sa.rule_breaks("Draft ready.", drafts=["The workshop covers AI tools."], today=TODAY, tz=TZ) == []


def test_modes_from_either_side():
    assert sa.claude_mode([["Bash", 1, "scheduling_engine.py reschedule-brief --conversation-id c"]]) == "reschedule"
    assert sa.claude_mode([["Bash", 1, "graph_actions.py create-reply --sender-only"]]) == "poll"
    assert sa.claude_mode([["Bash", 1, "scheduling_engine.py validate-draft --times x"]]) == "validate"
    assert sa.shadow_mode([{"action": "booking-propose"}], []) == "booking"
    assert sa.shadow_mode([{"action": "propose-meeting"}], []) == "propose"
    assert sa.shadow_mode([], []) == "other"


def test_score_turn_flags_invented_and_invalid_times(store):
    results = [{"tool": "propose-meeting", "args": {"duration": 30}, "parsed": PROPOSE_OUT}]
    turn = {"reply": "How do Tue Oct 13 2:00-2:30pm or Wed Oct 14 10am sound?", "steps": [
        {"action": "propose-meeting"}], "writes": []}

    def validate(cmd):
        t = json.loads(cmd[cmd.index("--times") + 1])[0]
        return {"valid": t.startswith("2026-10-13")}

    runner = FakeRunner({"validate-draft": validate})
    checks = sa.score_turn(_job(), turn, results, runner=runner, today=TODAY, tz=TZ)
    assert checks["shadow"]["invented"] == ["2026-10-14T10:00:00-06:00"]
    assert checks["shadow"]["validity"] == {"named": 2, "checked": 2, "valid": 1,
                                            "invalid": ["2026-10-14T10:00:00-06:00"]}
    assert checks["claude"]["times"] == ["2026-10-13T14:00"]
    assert checks["claude"]["validity"]["valid"] == 1
    assert checks["mode_agree"] is True
    assert checks["overlap"] == 0.5
    # every validate-draft check ran as the resident
    assert all(c[:4] == ["sudo", "-n", "-u", "ea-bot"] for c in runner.cmds)


def test_valid_validate_draft_and_free_window_sanction_times():
    results = [
        {"tool": "validate-draft", "args": {"times": ["2026-10-15T09:30:00-06:00"], "duration": 60},
         "parsed": {"valid": True}},
        {"tool": "check-attendees", "args": {"start": "2026-10-16T11:15:00-06:00",
                                             "end": "2026-10-16T12:00:00-06:00"},
         "parsed": {"all_attendees_free": True}},
        {"tool": "validate-draft", "args": {"times": ["2026-10-17T09:30:00-06:00"]},
         "parsed": {"valid": False}},
    ]
    wins = sa.solver_windows(results)
    assert len(wins) == 2
    t = {"start": datetime(2026, 10, 15, 9, 30, tzinfo=TZ),
         "end": datetime(2026, 10, 15, 10, 30, tzinfo=TZ)}
    assert sa._inside(t, wins)
    assert not sa._inside({"start": datetime(2026, 10, 17, 9, 30, tzinfo=TZ), "end": None}, wins)


def test_poll_reply_to_all_is_a_rule_break(store):
    turn = {"reply": "Draft ready.", "steps": [{"action": "read-poll"}], "writes": [
        {"tool": "create-reply", "args": {"message_id": "m", "body": "Works for me."}}]}
    checks = sa.score_turn(_job(), turn, [], runner=FakeRunner(), today=TODAY, tz=TZ, validate=False)
    assert "reply-all-on-poll" in checks["shadow"]["breaks"]


def test_claude_draft_bodies_are_checked_when_the_bot_passes_them(store):
    turn = {"reply": "", "steps": [], "writes": []}
    job = _job(claude={"reply": "Draft ready.", "drafts": ["Friday Oct 13 at 2pm works — thanks."]})
    checks = sa.score_turn(job, turn, [], runner=FakeRunner(), today=TODAY, tz=TZ, validate=False)
    assert checks["claude"]["times"] == ["2026-10-13T14:00"]
    assert {"dash", "weekday-mismatch"} <= set(checks["claude"]["breaks"])


def test_claude_staged_events_count_as_its_times(store):
    sa.note_claude_staged("c", "t1", {"action": "event_create", "subject": "Sync",
                                      "start": "2026-10-13T15:00:00-06:00",
                                      "end": "2026-10-13T15:30:00-06:00"})
    sa.note_claude_staged("c", "other", {"action": "event_create",
                                         "start": "2026-10-20T15:00:00-06:00"})
    turn = {"reply": "", "steps": [], "writes": []}
    checks = sa.score_turn(_job(claude={"reply": "Staged it."}), turn, [], runner=FakeRunner(),
                           today=TODAY, tz=TZ, validate=False)
    assert checks["claude"]["times"] == ["2026-10-13T15:00"]
    assert checks["claude"]["staged"] == 1


# ── runner, report, grading ───────────────────────────────────────────

def test_execute_job_records_turn_and_transcript(store):
    chat = FakeChat([step("propose-meeting", {"attendees": [{"email": "a@b"}]}),
                     step("reply", {"text": "How does Tue Oct 13 2:00-2:30pm sound?"})])
    runner = FakeRunner({"propose-meeting": PROPOSE_OUT, "validate-draft": {"valid": True}})
    turn = sa.execute_job(_job(), chat=chat, runner=runner)
    assert turn["status"] == "ok" and turn["checks"]["shadow"]["invented"] == []
    assert rl.read("sched-turns.jsonl")[0]["run_id"] == "r1"
    tr = store / "sched-transcripts" / "r1.json"
    assert stat.S_IMODE(tr.stat().st_mode) == 0o600
    assert json.loads(tr.read_text())["messages"][-1]["role"] == "assistant"


def test_old_job_is_skipped_as_backlog(store):
    old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
    chat = FakeChat([])
    turn = sa.execute_job(_job(queued_at=old), chat=chat, runner=FakeRunner())
    assert turn["status"] == "skipped-backlog" and chat.calls == []


def test_run_job_refuses_a_path_outside_the_queue(store, tmp_path):
    stray = tmp_path / "job.json"
    stray.write_text(json.dumps(_job()))
    assert sa.run_job(str(stray)) == 2


def test_run_job_drops_queued_job_when_switch_turned_off(store, monkeypatch):
    path = store / "sched-jobs" / "r1.json"
    sa._private_write(path, json.dumps(_job()))
    monkeypatch.setattr(rl, "config", lambda: _cfg(scheduling="off"))
    assert sa.run_job(str(path)) == 0
    assert not path.exists()
    assert rl.read("sched-turns.jsonl") == []


def test_report_and_grading_sheet(store):
    runner = FakeRunner({"propose-meeting": PROPOSE_OUT, "validate-draft": {"valid": True}})
    sa.execute_job(_job(run_id="a"), runner=runner, chat=FakeChat([
        step("propose-meeting", {"attendees": [{"email": "a@b"}]}),
        step("reply", {"text": "How does Tue Oct 13 2:00-2:30pm sound?"})]))
    sa.execute_job(_job(run_id="b"), runner=runner, chat=FakeChat([
        step("reply", {"text": "How about Wed Oct 14 at 4pm — or later?"})]))
    sa.note_event_outcome("create", "t1", {"start": "2026-10-13T14:00:00-06:00"}, True)
    r = sa.report()
    assert r["turns"] == 2 and r["by_status"] == {"ok": 2}
    assert r["invented_times"] == 1 and r["turns_with_invented"] == 1
    assert r["shadow"]["breaks"] == {"dash": 1}
    assert r["outcomes"] == {"events": 1, "shadow_named": 1, "claude_named": 1}
    assert r["mode_agreement"] == 0.5
    assert "Invented times: 1" in sa._report_text(r)
    sheet = sa.grading_sheet(5)
    text = sheet.read_text()
    assert text.count("Grade: ____") == 4 and "Wed Oct 14 at 4pm" in text
    assert stat.S_IMODE(sheet.stat().st_mode) == 0o600


def test_build_prompt_carries_rules_tools_and_task(monkeypatch):
    monkeypatch.setattr(sa, "_resident_text", lambda path: '{"timezone": "America/Denver"}'
                        if path.name == "user-profile.json" else "")
    fake_prefs = type(sys)("preferences")
    fake_prefs.render = lambda scopes: "## User rules (scopes: scheduling)"
    monkeypatch.setitem(sys.modules, "preferences", fake_prefs)
    msgs, tz = sa.build_prompt({"task_block": "Find time with Bo", "lane": "proactive"})
    system = msgs[0]["content"]
    assert "Scheduling Agent" in system and "SCHEDULING RULES" in system
    assert "- propose-meeting (duration?, days_ahead?, earliest_date?, latest_date?, num_options?, attendees" in system
    assert "## User rules (scopes: scheduling)" in system
    assert msgs[1]["content"].endswith("Find time with Bo") and "proactive" in msgs[1]["content"]
    assert str(tz) == "America/Denver"
    assert set(sa.STEP_SCHEMA["properties"]["action"]["enum"]) == set(sa.ACTIONS)


# ── live turns ────────────────────────────────────────────────────────

def _live(monkeypatch, verdict="ok"):
    monkeypatch.setattr(rl, "config", lambda: _cfg(scheduling="enforce"))
    # The backstop's Claude run is scripted: `verdict` for every review.
    monkeypatch.setattr(sa, "_hosted_json", lambda prompt, schema, model: (
        {"verdict": verdict, "issues": [] if verdict == "ok" else [{"kind": "time", "detail": "2pm is busy"}],
         "note": "scripted"}, {"cost_usd": 0.05}))
    monkeypatch.setattr(sa, "backstop_model", lambda: "opus")
    monkeypatch.setattr(sa, "resident_unlink", lambda path, timeout=0: (0, "", ""))


class FakeWriter:
    def __init__(self):
        self.files = {}

    def __call__(self, path, text, timeout=0):
        self.files[path] = text
        return 0, "", ""


def test_live_on_reads_the_enforce_value(store, monkeypatch):
    assert not sa.live_on() and sa._shadow_on()
    _live(monkeypatch)
    assert sa.live_on() and not sa._shadow_on()


def test_live_turn_runs_writes_as_the_resident_under_the_turns_run_id(store, monkeypatch):
    import mail_mediation
    monkeypatch.setattr(mail_mediation, "ea_managed_calendar_id", lambda: "example@group.calendar.google.com")
    _live(monkeypatch)
    chat = FakeChat([
        step("propose-meeting", {"duration": 30, "attendees": ["bo@example.edu"]}),
        step("create-reply", {"message_id": "m1", "conversation_id": "c1",
                              "body": "Tuesday 2pm works.", "sender_only": True}),
        step("stage-event", {"event": {"subject": "Bo", "start": "2026-10-13T14:00:00",
                                       "end": "2026-10-13T14:30:00", "attendees": "bo@example.edu"}}),
        step("ea-managed-hold", {"op": "create", "summary": "hold", "start": "2026-10-13T14:00:00",
                                 "end": "2026-10-13T14:30:00"}),
        step("fill-poll", {"url": "https://when2meet.example/x", "name": "Kit", "available": []}),
        step("reply", {"text": "How does Tue Oct 13 2:00-2:30pm sound? The Send button is in your DM."}),
    ])
    runner = FakeRunner({"propose-meeting": PROPOSE_OUT,
                         "create-reply": {"status": "reply_draft_created", "id": "draft-9"},
                         "validate-draft": {"status": "ok", "results": []}})
    writer = FakeWriter()
    monkeypatch.setattr(sa, "_ea_managed_email", lambda: "someone@gmail.com")
    turn = sa.run_live(_job(channel="C1", thread_ts="t9", user_msg="find time with Bo"),
                       chat=chat, runner=runner, writer=writer, validate=False)
    assert turn["status"] == "ok" and turn["live"] is True and "fallback" not in turn
    assert turn["reply"].startswith("How does Tue")
    # The draft and the hold ran as the resident with the live run id, no shadow- prefix.
    reply_cmd = next(c for c in runner.cmds if "create-reply" in c)
    assert "EA_RUN_ID=r1" in reply_cmd and "--sender-only" in reply_cmd and "--body" in reply_cmd
    hold_cmd = next(c for c in runner.cmds if "create-event" in c)
    assert "--calendar-id" in hold_cmd and "group.calendar.google.com" in hold_cmd[hold_cmd.index("--calendar-id") + 1]
    assert "--attendees" not in hold_cmd
    # The staged event went to the resident's state with this thread's routing keys.
    (path, text), = writer.files.items()
    assert path.endswith("/.ea/state/pending-event.json")
    staged = json.loads(text)
    assert staged["thread_ts"] == "t9" and staged["channel"] == "C1" and staged["subject"] == "Bo"
    # The poll fill was refused, never run.
    assert not any("poll_filler" in " ".join(c) for c in runner.cmds)
    results = {w["tool"]: w["result"] for w in turn["writes"]}
    assert results["create-reply"] == "reply_draft_created" and results["stage-event"] == "staged"
    assert results["fill-poll"] == "refused"
    # The model saw the draft id and the refusal.
    obs = [m["content"] for m in chat.calls[-1] if m["content"].startswith("OBSERVATION")]
    assert any("draft-9" in o for o in obs) and any("needs the user's approval" in o for o in obs)
    # The turn is on record as live and the report counts it apart from the shadow.
    rec = rl.read(sa.TURNS)[-1]
    assert rec["live"] and rec["user_msg"] == "find time with Bo"
    r = sa.report()
    assert r["turns"] == 0 and r["live"] == {
        "turns": 1, "by_outcome": {"ok": 1}, "writes": 3, "invented_times": 0,
        "latency_s": r["live"]["latency_s"], "backstop": {"ok": 1, "withheld": 0}}
    assert "Live turns: 1" in sa._report_text(r) and 'backstop {"ok": 1, "withheld": 0}' in sa._report_text(r)
    # The backstop saw the writes and said ok; its verdict is on the turn.
    assert turn["backstop"]["verdict"] == "ok" and turn["backstop"]["model"] == "opus"
    assert turn["backstop"]["cost_usd"] == 0.05 and "withdrawn" not in turn
    assert turn["writes"][0]["ids"] == {"id": "draft-9"}


def test_backstop_withholds_defective_writes_and_claude_takes_the_turn(store, monkeypatch):
    _live(monkeypatch, verdict="withhold")
    chat = FakeChat([
        step("propose-meeting", {"duration": 30, "attendees": ["bo@example.edu"]}),
        step("create-reply", {"message_id": "m1", "conversation_id": "c1", "body": "Tuesday 2pm works."}),
        step("stage-event", {"event": {"subject": "Bo", "start": "2026-10-13T14:00:00",
                                       "end": "2026-10-13T14:30:00", "attendees": "bo@example.edu"}}),
        step("ea-managed-hold", {"op": "create", "summary": "hold", "start": "2026-10-13T14:00:00",
                                 "end": "2026-10-13T14:30:00"}),
        step("log-decision", {"event": "Bo", "action": "proposed"}),
        step("reply", {"text": "How does Tue 2pm sound?"}),
    ])
    runner = FakeRunner({"propose-meeting": PROPOSE_OUT,
                         "create-reply": {"status": "reply_draft_created", "id": "draft-9"},
                         "create-event": {"status": "ok", "id": "hold-3"},
                         "delete-draft": {"status": "deleted"}, "delete-event": {"status": "deleted"}})
    removed = []
    monkeypatch.setattr(sa, "_ea_managed_email", lambda: "someone@gmail.com")
    turn = sa.run_live(_job(channel="C1", thread_ts="t9", user_msg="find time with Bo"), chat=chat,
                       runner=runner, writer=FakeWriter(), validate=False,
                       remover=lambda path, timeout=0: removed.append(path) or (0, "", ""))
    assert turn["backstop"]["verdict"] == "withhold"
    assert turn["backstop"]["issues"] == [{"kind": "time", "detail": "2pm is busy"}]
    # Nothing reaches a button: the reply is empty and Claude takes the turn.
    assert turn["fallback"] == "claude" and turn["reply"] == ""
    # The draft and the hold were deleted as the resident under the live run id; the staged file removed.
    del_draft = next(c for c in runner.cmds if "delete-draft" in c)
    assert "EA_RUN_ID=r1" in del_draft and del_draft[del_draft.index("--message-id") + 1] == "draft-9"
    del_event = next(c for c in runner.cmds if "delete-event" in c)
    assert del_event[del_event.index("--event-id") + 1] == "hold-3" and "--confirm-token" not in del_event
    assert removed == ["/home/ea-bot/.ea/state/pending-event.json"]
    assert [(w["tool"], w["ok"], w["how"]) for w in turn["withdrawn"]] == [
        ("create-reply", True, "delete-draft draft-9"), ("stage-event", True, "removed pending-event.json"),
        ("ea-managed-hold", True, "delete-event hold-3"), ("log-decision", True, "left as is")]
    r = sa.report()
    assert r["live"]["by_outcome"] == {"claude": 1} and r["live"]["backstop"] == {"ok": 0, "withheld": 1}


def test_backstop_fails_closed_and_can_be_switched_off(store, monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(sa, "_hosted_json", lambda *a: (_ for _ in ()).throw(RuntimeError("claude rc=1")))
    steps = [step("create-reply", {"message_id": "m1", "conversation_id": "c1", "body": "ok"}),
             step("reply", {"text": "Drafted."})]
    runner = FakeRunner({"create-reply": {"status": "reply_draft_created", "id": "d1"},
                         "delete-draft": {"status": "deleted"}})
    turn = sa.run_live(_job(), chat=FakeChat(list(steps)), runner=runner, writer=FakeWriter(), validate=False)
    assert turn["fallback"] == "claude" and turn["backstop"]["verdict"] == "withhold"
    assert turn["backstop"]["issues"][0]["detail"].startswith("review failed: claude rc=1")
    assert any("delete-draft" in c for c in runner.cmds)
    # A turn that wrote nothing is not reviewed.
    turn = sa.run_live(_job(), chat=FakeChat([step("reply", {"text": "No slot."})]), runner=FakeRunner(),
                       writer=FakeWriter(), validate=False)
    assert "backstop" not in turn and turn["reply"] == "No slot."
    # `"sched_backstop": false` in the config turns the review off.
    monkeypatch.setattr(rl, "config", lambda: dict(_cfg(scheduling="enforce"), sched_backstop=False))
    runner = FakeRunner({"create-reply": {"status": "reply_draft_created", "id": "d1"}})
    turn = sa.run_live(_job(), chat=FakeChat(list(steps)), runner=runner, writer=FakeWriter(), validate=False)
    assert "backstop" not in turn and turn["reply"] == "Drafted." and "fallback" not in turn


def test_backstop_prompt_carries_the_reads_the_writes_and_the_rules(store, monkeypatch):
    seen = {}

    def judge(prompt, schema, model):
        seen["prompt"], seen["schema"], seen["model"] = prompt, schema, model
        return {"verdict": "ok", "issues": [], "note": ""}, {"cost_usd": 0.01}
    monkeypatch.setattr(sa, "backstop_model", lambda: "opus")
    monkeypatch.setattr(sa, "_repo_text", lambda p: "RULE: never reply-all on a poll" if p == sa.RULES_FILE else "")
    turn = {"reply": "Tue 2pm?", "writes": [
        {"tool": "create-reply", "args": {"body": "Tuesday 2pm works.", "sender_only": True},
         "executed": True, "result": "reply_draft_created", "ids": {"id": "d1"}},
        {"tool": "fill-poll", "args": {}, "executed": True, "result": "refused"}]}
    results = [{"tool": "propose-meeting", "args": {"duration": 30}, "rc": 0, "parsed": PROPOSE_OUT}]
    review = sa.backstop_review(_job(history="Earlier: User: find time"), turn, results, judge=judge)
    assert review == {"verdict": "ok", "issues": [], "note": "", "model": "opus", "cost_usd": 0.01}
    p = seen["prompt"]
    assert "RULE: never reply-all on a poll" in p and "Earlier: User: find time" in p
    assert "### propose-meeting" in p and "2026-10-13T14:00:00-06:00" in p
    assert "### create-reply -> reply_draft_created" in p and "Tuesday 2pm works." in p
    assert "fill-poll" not in p.split("## What the agent wrote")[1].split("## The agent's reply")[0]
    assert seen["schema"] == sa.BACKSTOP_SCHEMA and seen["model"] == "opus"


def test_live_turn_that_wrote_nothing_falls_back_to_claude(store, monkeypatch):
    _live(monkeypatch)
    chat = FakeChat([rl.RemoteLLMError("down")])
    turn = sa.run_live(_job(), chat=chat, runner=FakeRunner(), writer=FakeWriter(), validate=False)
    assert turn["status"] == "error" and turn["fallback"] == "claude" and turn["reply"] == ""


def test_live_turn_that_wrote_something_gets_a_code_reply(store, monkeypatch):
    _live(monkeypatch)
    chat = FakeChat([
        step("create-reply", {"message_id": "m1", "conversation_id": "c1", "body": "ok"}),
        rl.RemoteLLMError("down"),
    ])
    turn = sa.run_live(_job(), chat=chat, runner=FakeRunner(
        {"create-reply": {"status": "reply_draft_created", "id": "d1"}}),
        writer=FakeWriter(), validate=False)
    assert turn["fallback"] == "code reply"
    assert turn["reply"].startswith("I drafted the reply; the Send button is in your DM.")


def test_live_history_feeds_the_next_turn_in_the_thread(store, monkeypatch):
    _live(monkeypatch)
    first = FakeChat([step("reply", {"text": "How does Tuesday 2pm sound?"})])
    sa.run_live(_job(thread_ts="th", user_msg="find time"), chat=first, runner=FakeRunner(),
                writer=FakeWriter(), validate=False)
    second = FakeChat([step("reply", {"text": "Staged."})])
    monkeypatch.setattr(sa, "build_prompt", lambda job: (
        [{"role": "system", "content": "rules"},
         {"role": "user", "content": str(job.get("history") or "") + "\n" + str(job.get("task_block"))}], TZ))
    sa.run_live(_job(run_id="r2", thread_ts="th", user_msg="take Tuesday"), chat=second,
                runner=FakeRunner(), writer=FakeWriter(), validate=False)
    seen = second.calls[0][1]["content"]
    assert "Earlier in this thread" in seen and "User: find time" in seen
    assert "You: How does Tuesday 2pm sound?" in seen
    assert sa.thread_history("other") == ""


def test_shadow_turn_never_runs_a_write_even_with_a_writer(store):
    ev = {"subject": "x", "start": "2026-10-13T14:00:00", "end": "2026-10-13T14:30:00"}
    chat = FakeChat([step("stage-event", {"event": ev}), step("reply", {"text": "ok"})])
    writer = FakeWriter()
    turn, _r, _m = sa.run_agent(_job(), chat=chat, runner=FakeRunner(), writer=writer)
    assert writer.files == {} and turn["writes"][0] == {"tool": "stage-event", "args": {"event": ev}}
