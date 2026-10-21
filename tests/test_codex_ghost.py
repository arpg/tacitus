"""The Codex ghost arm (preprocessor/codex_ghost.py): a window the owner
opens, replays queued per turn and run as the resident with writes
confined, the mediator's read-only rule for a ghost run id, grades
against Claude's output, a verdict per kind, and the quota stop. Nothing
here runs codex, a resident process or a judge: each is a fake."""
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
import codex_cli  # noqa: E402
import codex_ghost as cg  # noqa: E402
import remote_llm  # noqa: E402


@pytest.fixture
def private(tmp_path, monkeypatch):
    """An owner-private dir with a remote-llm.json, as the real one is."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    cfg = priv / remote_llm.CONFIG_FILE
    cfg.write_text(json.dumps({"endpoint": "https://models.example.net/v1", "model": "m", "ghost": {}}))
    os.chmod(cfg, 0o600)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    (tmp_path / "ea").mkdir()
    return priv


def _rows(name):
    return cg._read(name)


# ── the window ───────────────────────────────────────────────────────

def test_a_window_opens_for_days_and_kinds_and_closes(private):
    assert cg.window() == {} and not cg.active()
    w = cg.start(3, ["briefing"], 0.25)
    assert w["from"] == date.today().isoformat() and w["kinds"] == ["briefing"] and w["share"] == 0.25
    assert cg.active("briefing") and not cg.active("chat")
    raw = json.loads((private / remote_llm.CONFIG_FILE).read_text())
    assert raw["ghost"]["codex"]["until"] >= raw["ghost"]["codex"]["from"]
    assert raw["endpoint"].startswith("https://"), "the rest of the file is kept"
    cg.stop()
    assert cg.window() == {}


def test_a_day_after_until_is_closed(private):
    cg.start(1)
    assert cg.active(today=date.today())
    assert not cg.active(today=date.fromordinal(date.today().toordinal() + 1))


# ── the mediator's rule ──────────────────────────────────────────────

def test_a_ghost_run_may_read_and_every_write_is_recorded(private):
    rid = cg.run_id_for("briefing-x")
    assert rid.startswith("cron-ghost-codex-") and cg.is_ghost_run(rid)
    for verb in ("get-conversation", "search-mail", "list-mail", "calendar-view", "get-event", "preflight-check"):
        assert cg.ghost_refusal("graph_actions", verb, rid) == "", verb
    for verb in ("create-reply", "create-draft", "archive-message", "snooze-message", "update-draft", "set-categories"):
        assert "recorded, not executed" in cg.ghost_refusal("graph_actions", verb, rid), verb
    assert cg.ghost_refusal("graph_actions", "create-draft", "cron-morning-briefing-1") == ""
    assert cg.ghost_refusal("graph_actions", "create-draft", "") == ""
    cg.record_write(rid, "graph_actions", "create-reply", ["--message-id", "m1", "--body", "x"])
    assert _rows(cg.WRITES)[0]["verb"] == "create-reply" and cg._writes_for(rid)[0]["run_id"] == rid


def test_the_mediator_answers_recorded_for_a_ghost_write(private, monkeypatch):
    import mail_mediator as mm
    seen = []
    monkeypatch.setattr(cg, "record_write", lambda *a: seen.append(a))
    assert mm._ghost_recorded("graph_actions", "get-message", [], cg.run_id_for("chat-1")) == ""
    msg = mm._ghost_recorded("graph_actions", "archive-message", ["--message-id", "m"], cg.run_id_for("chat-1"))
    assert "recorded" in msg and seen and seen[0][2] == "archive-message"
    assert mm._ghost_recorded("graph_actions", "archive-message", [], "cron-x") == ""
    text = (REPO / "preprocessor" / "mail_mediator.py").read_text()
    assert text.index("_ghost_recorded(tool, verb, argv, run_id)") < text.index("refusal = _gate_precheck(tool, verb, argv, run_id)")
    assert '"status": "recorded", "ghost": True' in text


# ── enqueue ──────────────────────────────────────────────────────────

def test_enqueue_skips_without_a_window_and_queues_with_one(private):
    spawned = []
    res = cg.enqueue({"kind": "briefing", "agent": "morning-briefing", "source_run_id": "cron-a"},
                     spawn=spawned.append, lim={})
    assert res == {"skipped": "no open window for this kind"} and not spawned
    cg.start(7, ["briefing", "chat"], 1.0)
    res = cg.enqueue({"kind": "briefing", "agent": "morning-briefing", "source_run_id": "cron-a",
                      "prompt": "P", "claude": {"output": "A"}}, spawn=spawned.append, lim={})
    assert "queued" in res and len(spawned) == 1
    job = json.loads(spawned[0].read_text())
    assert job["run_id"].startswith("cron-ghost-codex-") and job["kind"] == "briefing" and job["prompt"] == "P"
    assert stat.S_IMODE(spawned[0].stat().st_mode) == 0o600
    assert cg.enqueue({"kind": "other", "source_run_id": "x"}, spawn=spawned.append, lim={})["skipped"].startswith("unknown kind")


def test_deck_builds_are_not_replayed_and_the_skip_is_recorded(private):
    cg.start(7, ["chat"], 1.0)
    assert cg.window()["skip"] == cg.DEFAULT_SKIP
    res = cg.enqueue({"kind": "chat", "source_run_id": "d1", "user_msg": "rebuild the deck with the new photos"},
                     spawn=lambda p: pytest.fail("a deck turn must not run"), lim={})
    assert res["skipped"].startswith("pattern:")
    assert _rows(cg.TURNS)[-1]["skip_reason"].startswith("pattern:")
    assert "queued" in cg.enqueue({"kind": "chat", "source_run_id": "d2", "user_msg": "snooze the vendor thread"},
                                  spawn=lambda p: None, lim={})
    assert cg.skip_reason({"kind": "briefing", "user_msg": "deck"}) == ""
    cg.start(7, ["chat"], 1.0, skip=[r"\bpoll\b"])
    assert cg.window()["skip"] == [r"\bpoll\b"]
    assert cg.skip_reason({"kind": "chat", "user_msg": "rebuild the deck"}) == ""
    assert cg.skip_reason({"kind": "chat", "user_msg": "fill the poll"}) == r"\bpoll\b"


def test_the_chat_share_is_a_stable_draw_per_turn(private):
    cg.start(7, ["chat"], 0.0)
    assert cg.enqueue({"kind": "chat", "source_run_id": "r1"}, spawn=lambda p: None, lim={}) == {"skipped": "not in the share"}
    cg.start(7, ["chat"], 1.0)
    assert "queued" in cg.enqueue({"kind": "chat", "source_run_id": "r1"}, spawn=lambda p: None, lim={})
    assert {cg._share_pick(f"run-{i}", 0.5) for i in range(40)} == {True, False}
    assert cg._share_pick("same", 0.5) == cg._share_pick("same", 0.5)


def test_quota_stops_the_ghost_and_records_the_skip(private):
    cg.start(7, ["briefing"])
    lim = {"plan": "edu_plus", "primary": {"used_percent": 10}, "secondary": {"used_percent": 85}}
    assert "weekly window is at 85%" in cg.quota_block(lim)
    assert cg.quota_block({"primary": {"used_percent": 95}, "secondary": {"used_percent": 1}}).startswith("the 5-hour")
    assert cg.quota_block({"primary": {"used_percent": 5}, "secondary": {"used_percent": 5}}) == ""
    assert cg.quota_block({}) == "" and cg.quota_block({"error": "x"}) == ""
    res = cg.enqueue({"kind": "briefing", "agent": "morning-briefing", "source_run_id": "cron-b"},
                     spawn=lambda p: None, lim=lim)
    assert res["skipped"].startswith("quota:")
    rows = _rows(cg.TURNS)
    assert rows[-1]["status"] == "skipped" and rows[-1]["skip_reason"].startswith("quota:")


def test_a_job_a_dead_worker_left_behind_runs_from_the_sweep(private, monkeypatch):
    """A worker is a detached child of the bot, so a bot restart kills it
    and the job file stays; the sweep runs a job older than the stale
    age with no worker alive, and leaves a fresh one to its worker."""
    import time as _t
    cg.start(7, ["chat"], 1.0)
    spawned = []
    cg.enqueue({"kind": "chat", "source_run_id": "old", "prompt": "P"}, spawn=spawned.append, lim={})
    cg.enqueue({"kind": "chat", "source_run_id": "new", "prompt": "P"}, spawn=spawned.append, lim={})
    old, new = spawned
    os.utime(old, (_t.time() - 600, _t.time() - 600))
    monkeypatch.setattr(cg, "_worker_alive", lambda path: False)
    ran = []

    def runner(job):
        ran.append(job["id"])
        (cg._store() / cg.JOBS / f"{job['id']}.json").unlink()
        return {"status": "ok"}
    out = cg.run_queued(runner=runner)
    assert ran == [json.loads(old.read_text())["id"]] if old.exists() else len(ran) == 1
    assert out["ran"][0]["status"] == "ok" and new.name in out["skipped"]
    # a worker still alive keeps its job
    os.utime(new, (_t.time() - 600, _t.time() - 600))
    monkeypatch.setattr(cg, "_worker_alive", lambda path: True)
    assert cg.run_queued(runner=lambda job: pytest.fail("a live worker's job must not run twice"))["ran"] == []


def test_limits_come_from_the_app_server_answer_and_are_cached(private):
    calls = []

    def runner():
        calls.append(1)
        return {"plan": "edu_plus", "primary": {"used_percent": 0, "window_minutes": 300, "resets_at": 1},
                "secondary": {"used_percent": 6, "window_minutes": 10080, "resets_at": 2},
                "read_at": cg._iso(cg._now())}
    first = cg.limits(fresh=True, runner=runner)
    again = cg.limits(runner=runner)
    assert first["secondary"]["used_percent"] == 6 and again == first and calls == [1]
    assert (cg._store() / cg.LIMITS).exists()


# ── the replay ───────────────────────────────────────────────────────

class _Run:
    returncode = 0
    stderr = ""


def _fake_scratch(cmd, inp):
    spec = json.loads(inp)
    assert cmd[2:4] == ["-u", cg.RESIDENT] and "python3" in cmd, "made as the resident"
    assert spec["root"].startswith(str(cg.BOT_HOME / ".ea" / "state" / "codex-ghost")), "under an allowed mediator root"
    assert "state/obligations-ledger.db" in spec["copies"] and "state/briefing-latest.json" in spec["copies"]
    return _Run()


def test_a_briefing_replay_runs_as_the_resident_with_writes_confined(private, monkeypatch):
    monkeypatch.setattr(cg, "remove_scratch", lambda jid, runner=None: None)
    seen = {}

    def codex_run(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return {"output": "## Briefing\nCodex says.",
                "tool_calls": [{"tool": "Bash", "input": {"command": "python3 x"}, "exit_code": 0}],
                "usage": {"input_tokens": 1000, "output_tokens": 50}, "duration_s": 12.3, "exit_code": 0,
                "harness_error": "", "files": []}
    job = {"kind": "briefing", "id": "briefing-1", "run_id": cg.run_id_for("briefing-1"), "agent": "morning-briefing",
           "lane": "cron", "source_run_id": "cron-morning-briefing-1", "prompt": "You are running as the EA agent",
           "claude": {"output": "## Briefing\nClaude says.", "tools": [], "elapsed_s": 90}}
    cg.record_write(job["run_id"], "graph_actions", "create-reply", ["--message-id", "m"])
    rec = cg.run_job(job, codex_run=codex_run, scratch_runner=_fake_scratch)
    assert rec["status"] == "ok" and rec["codex"]["output"].endswith("Codex says.")
    assert rec["writes"] and rec["writes"][0]["verb"] == "create-reply"
    assert seen["sandbox"] == "workspace-write" and seen["add_dirs"] == [cg.BOT_HOME / ".ea" / "state" / "mail-queue"]
    prefix = seen["prefix"]
    assert prefix[2:4] == ["-u", cg.RESIDENT] and prefix[4] == "env"
    env = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in prefix[5:]}
    assert env["EA_RUN_ID"] == job["run_id"] and env["EA_DIR"].endswith("/briefing-1/ea") and env["HOME"] == str(cg.BOT_HOME)
    assert str(seen["cwd"]) == env["EA_DIR"] and str(seen["last_message_dir"]) == env["EA_DIR"]
    assert "# Operating instructions (CLAUDE.md)" in seen["prompt"] and "You are running as the EA agent" in seen["prompt"]
    assert "You are Codex, not Claude" in seen["prompt"] and "recorded by the mediator" in seen["prompt"]
    assert _rows(cg.TURNS)[-1]["id"] == "briefing-1"


def test_a_chat_replay_carries_the_context_and_a_harness_error_is_an_error_row(private, monkeypatch):
    monkeypatch.setattr(cg, "remove_scratch", lambda jid, runner=None: None)
    seen = {}

    def codex_run(prompt, **kw):
        seen["prompt"] = prompt
        return {"output": "", "tool_calls": [], "usage": {}, "duration_s": 1, "exit_code": 1,
                "harness_error": "codex exit 1: boom", "files": []}
    job = {"kind": "chat", "id": "chat-1", "run_id": cg.run_id_for("chat-1"), "lane": "dm", "channel": "dm:1",
           "thread_ts": "t1", "source_run_id": "abc", "user_msg": "snooze the vendor thread",
           "task_block": "Thread: vendor", "claude": {"output": "Snoozed.", "tools": ["Bash: snooze"], "elapsed_s": 5}}
    rec = cg.run_job(job, codex_run=codex_run, scratch_runner=_fake_scratch)
    assert rec["status"] == "error" and rec["codex"]["harness_error"].startswith("codex exit 1")
    assert "## The user's message\n\nsnooze the vendor thread" in seen["prompt"] and "Thread: vendor" in seen["prompt"]
    assert "the reply you would post" in seen["prompt"]


def test_a_failed_scratch_is_an_error_row_not_a_crash(private, monkeypatch):
    monkeypatch.setattr(cg, "remove_scratch", lambda jid, runner=None: None)

    class Bad:
        returncode = 1
        stderr = "mkdir: denied"
    rec = cg.run_job({"kind": "chat", "id": "chat-2", "run_id": cg.run_id_for("chat-2"), "source_run_id": "s"},
                     codex_run=lambda *a, **k: pytest.fail("codex must not run"), scratch_runner=lambda c, i: Bad())
    assert rec["status"] == "error" and "scratch setup failed" in rec["codex"]["harness_error"]


# ── codex_cli with a prefix ──────────────────────────────────────────

def test_codex_cli_runs_the_residents_codex_behind_the_prefix(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen.update(kw)
        Path(cmd[cmd.index("-o") + 1]).write_text("final answer")

        class R:
            returncode = 0
            stdout = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}) + "\n"
            stderr = ""
        return R()
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_cli, "binary", lambda: None)
    prefix = ["env", "HOME=/x"]
    res = codex_cli.run("hi", cwd=tmp_path, sandbox="workspace-write", add_dirs=[tmp_path / "q"],
                        prefix=prefix, last_message_dir=tmp_path)
    assert seen["cmd"][:2] == prefix and seen["cmd"][2] == codex_cli.CODEX_BIN
    assert "--add-dir" in seen["cmd"] and seen["cwd"] == str(tmp_path) and seen["input"] == "hi"
    assert res["output"] == "final answer" and res["usage"] == {"input_tokens": 3} and not res["harness_error"]
    assert Path(seen["cmd"][seen["cmd"].index("-o") + 1]).parent == tmp_path


# ── grades, verdicts, the gate ───────────────────────────────────────

def _seed(kind, n, answer):
    for i in range(n):
        cg._append(cg.TURNS, {"id": f"{kind}-{i}", "ts": cg._iso(cg._now()), "kind": kind, "agent": "morning-briefing",
                              "lane": "dm", "status": "ok", "claude": {"output": "A", "tools": []},
                              "codex": {"output": "B", "tool_calls": [], "usage": {"input_tokens": 10, "output_tokens": 1},
                                        "duration_s": 3}})
    cg.grade(judge="claude", runner=lambda prompt, schema, judge: (answer, {"cost_usd": 0.01}))


def test_each_turn_keeps_the_quota_reading_and_the_report_shows_the_peak(private, monkeypatch):
    monkeypatch.setattr(cg, "remove_scratch", lambda jid, runner=None: None)
    readings = iter([{"primary": {"used_percent": 3}, "secondary": {"used_percent": 18}, "read_at": "2026-01-01T00:00:00+00:00"},
                     {"primary": {"used_percent": 9}, "secondary": {"used_percent": 19}, "read_at": "2026-01-01T00:10:00+00:00"},
                     {"primary": {"used_percent": 5}, "secondary": {"used_percent": 19}, "read_at": "2026-01-01T00:20:00+00:00"}])
    monkeypatch.setattr(cg, "limits", lambda fresh=False, runner=None: next(readings))

    def codex_run(prompt, **kw):
        return {"output": "Nothing was staged.", "tool_calls": [], "usage": {"input_tokens": 10, "output_tokens": 1},
                "duration_s": 1, "exit_code": 0, "harness_error": "", "files": []}
    for i in range(3):
        job = {"kind": "chat", "id": f"chat-q{i}", "run_id": cg.run_id_for(f"chat-q{i}"), "lane": "dm", "channel": "dm:1",
               "thread_ts": f"t{i}", "source_run_id": f"s{i}", "user_msg": "hi", "task_block": "",
               "claude": {"output": "Hi.", "tools": [], "elapsed_s": 1}}
        rec = cg.run_job(job, codex_run=codex_run, scratch_runner=_fake_scratch)
    assert rec["quota"] == {"primary": 5, "secondary": 19, "read_at": "2026-01-01T00:20:00+00:00"}
    peak = cg.quota_peak(_rows(cg.TURNS))
    assert peak["primary"] == {"used_percent": 9, "id": "chat-q1"} and peak["secondary"]["used_percent"] == 19
    assert cg.quota_after({}) == {} and cg.quota_after({"error": "x"}) == {}
    assert "quota peak 5-hour 9%, weekly 19%" in cg.report()


def test_briefings_are_graded_against_claude_and_the_verdict_follows_pass(private):
    _seed("briefing", 5, {"missing_in_codex": ["the dean's deadline"], "extra_in_codex": [], "wrong_in_codex": [],
                          "verdict": "minor", "better": "claude", "enhancement": "read the ledger deadlines"})
    m = cg.measure("briefing")
    assert m["judged"] == 5 and m["minor"] == 5 and m["major"] == 0
    v, reasons = cg.verdict("briefing", m)
    assert v == "NO-GO" and any("minor or worse" in r for r in reasons)
    assert "read the ledger deadlines" in cg.report()
    grades = _rows(cg.GRADES)
    assert grades[0]["missing"] == ["the dean's deadline"] and grades[0]["verdict"] == "minor"
    assert cg.grade(judge="claude", runner=lambda *a: pytest.fail("already graded"))["graded"] == 0


def test_chat_turns_go_and_the_gate_opens_on_a_fresh_final(private):
    _seed("chat", 15, {"codex_grade": "send", "claude_grade": "send", "better": "same", "problems": [],
                       "enhancement": ""})
    v, reasons = cg.verdict("chat")
    assert v == "GO" and reasons == []
    assert cg.verdict("briefing")[0] == "NOT ENOUGH DATA"
    out = cg.final()
    assert out["chat"]["verdict"] == "GO" and out["briefing"]["verdict"] == "NOT ENOUGH DATA"
    ok, why = cg.gate("chat")
    assert ok and "GO" in why
    ok, why = cg.gate("briefing")
    assert not ok and "NOT ENOUGH DATA" in why
    assert not cg.gate("chat", today=date.fromordinal(date.today().toordinal() + 20))[0]
    rep = json.loads(cg.report(as_json=True))
    assert rep["kinds"]["chat"]["verdict"] == "GO" and rep["kinds"]["chat"]["measure"]["send"] == 15


def test_a_wrong_chat_turn_lands_in_the_wrong_list(private):
    _seed("chat", 2, {"codex_grade": "wrong", "claude_grade": "send", "better": "claude",
                      "problems": [{"kind": "wrong-fact", "note": "named a time the data lacks"}],
                      "enhancement": "give it the calendar"})
    m = cg.measure("chat")
    assert m["wrong"] == 2 and m["wrong_list"][0]["problems"][0]["kind"] == "wrong-fact"
    assert m["problem_kinds"] == [("wrong-fact", 2)]


def test_a_reply_that_claims_a_draft_without_a_write_is_a_mismatch_the_judge_sees():
    none = cg.claim_check("Both notes are ready; the Send cards are in your DM.", [])
    assert none["draft_claimed"] and not none["draft_written"] and none["mismatch"]
    help_only = cg.claim_check("Draft created.", [{"verb": "create-reply", "argv": ["create-reply", "--help"]}])
    assert help_only["mismatch"], "a --help call backs nothing"
    backed = cg.claim_check("Draft created, id AAMk1.", [{"verb": "create-reply", "argv": ["create-reply", "--message-id", "x"]}])
    assert backed["draft_written"] and not backed["mismatch"]
    sent = cg.claim_check("Reply sent and the thread archived.", [{"verb": "create-reply", "argv": ["create-reply"]}])
    assert sent["sent_claimed"] and sent["mismatch"]
    assert cg.claim_check("Nothing was staged; I need the amount.", [])["mismatch"] is False
    note = cg._harness_note({"claims": none}, {})
    assert "no create-reply" in note and note.startswith("(")
    assert cg._harness_note({"claims": backed}, {}) == ""
    text = cg.codex_suffix(Path("/x"), "chat")
    assert "returned an id" in text and "never report either as done" in text and "--help` is not a run" in text
    assert "never list or read `state/mail-queue`" in text


# ── the wiring ───────────────────────────────────────────────────────

def test_the_bot_and_the_lane_runner_hand_turns_to_the_ghost():
    bot = (REPO / "ea-chat-bot.py").read_text()
    assert "def _codex_ghost_turn(" in bot
    assert 'if live_cr is None and arm == "claude" and remote_turn is None:\n            _codex_ghost_turn(' in bot
    assert '_cg.active("chat")' in bot and "threading.Thread(target=_cg.enqueue" in bot
    lane = (REPO / "run-agent.sh").read_text()
    assert 'codex_ghost.py" gate briefing' in lane and "run-lane" in lane and "EA_HARNESS:-claude" in lane
    assert lane.index("CODEX_LANE=1") < lane.index('if [[ "$REMOTE_BRIEFING" == 0 ]]; then\nset +e')
    # the ghost enqueue follows the report file, never a run Codex produced
    assert 'codex_ghost.py" enqueue-lane' in lane
    hook = lane.index('enqueue-lane')
    assert lane.index('log "Report written to') < hook < lane.index('if [[ "$HAS_SLACK" == 1 ]]; then', hook)
    assert 'if [[ "${CODEX_LANE:-0}" == 0 ]]; then' in lane
    doc = (REPO / "docs" / "codex-harness.md").read_text()
    assert "## The ghost arm" in doc and "## Production" in doc
    assert (REPO / "deploy" / "enable-codex-ghost.sh").exists()


def test_the_enable_script_finds_the_binary_behind_npms_launcher(tmp_path):
    """npm puts a JavaScript launcher on PATH and the static binary in the
    platform package beside it; the script copies the binary, never the
    launcher (the resident has no node)."""
    import subprocess
    pkg = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    vendor = pkg / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / "x86_64-unknown-linux-musl"
    (vendor / "bin").mkdir(parents=True)
    (vendor / "codex-resources").mkdir()
    (vendor / "bin" / "codex").write_text("#!/bin/sh\necho binary\n")
    os.chmod(vendor / "bin" / "codex", 0o755)
    (pkg / "bin").mkdir()
    (pkg / "bin" / "codex.js").write_text("// launcher\n")
    binp = tmp_path / "bin"
    binp.mkdir()
    (binp / "codex").symlink_to(pkg / "bin" / "codex.js")
    script = ["/bin/bash", str(REPO / "deploy" / "enable-codex-ghost.sh"), "--print-source"]
    env = dict(os.environ, PATH=f"{binp}:/usr/bin:/bin")
    r = subprocess.run(script, capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == vendor
    # a direct binary install: its own directory
    direct = tmp_path / "direct"
    direct.mkdir()
    (direct / "codex").write_text("#!/bin/sh\n")
    os.chmod(direct / "codex", 0o755)
    r = subprocess.run(script, capture_output=True, text=True,
                       env=dict(os.environ, PATH=f"{direct}:/usr/bin:/bin"), timeout=30)
    assert r.returncode == 0 and Path(r.stdout.strip()) == direct
    # no codex on PATH: a clear line and exit 1, before any sudo
    (tmp_path / "empty").mkdir()
    r = subprocess.run(script, capture_output=True, text=True,
                       env=dict(os.environ, PATH=f"{tmp_path / 'empty'}:/usr/bin:/bin"), timeout=30)
    assert r.returncode == 1 and "no codex binary" in r.stderr
