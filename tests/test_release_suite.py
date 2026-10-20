"""tests/release/run_release.py: routing, checks, verdicts and the store,
with every model call replaced. No case here reaches a model.

    python3 -m pytest tests/test_release_suite.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "release"))
sys.path.insert(0, str(REPO / "preprocessor"))

import run_release as rr  # noqa: E402


# ── routing ────────────────────────────────────────────────────────────

def test_route_follows_the_switches():
    r = rr.route({"drafting": "remote", "scheduling": "shadow", "mail": "enforce"})
    assert r["drafting"]["owner"] == "remote"
    assert r["scheduling"]["owner"] == "remote"
    assert r["briefing"]["owner"] == "claude"
    r = rr.route({"drafting": "off", "scheduling": "off"})
    assert r["drafting"]["owner"] == "claude"
    assert r["scheduling"]["owner"] == "skip" and "no canned runner" in r["scheduling"]["why"]
    r = rr.route({})
    assert r["drafting"]["owner"] == "claude" and r["scheduling"]["owner"] == "skip"


def test_the_codex_harness_owns_briefing_and_drafting_unless_the_remote_writer_is_on():
    r = rr.route({"drafting": "off"}, harness="codex")
    assert r["briefing"]["owner"] == "codex" and r["drafting"]["owner"] == "codex"
    assert "codex harness writes the body" in r["drafting"]["why"]
    r = rr.route({"drafting": "remote"}, harness="codex")
    assert r["drafting"]["owner"] == "remote" and r["briefing"]["owner"] == "codex"


def test_draft_body_on_codex_uses_the_codex_writer_and_names_the_model(monkeypatch):
    seen = {}

    def fake_codex(prompt, model, timeout):
        seen.update(prompt=prompt, model=model)
        return {"text": "Dear Sol,\n\nTuesday works.\n\nBest,\nKit", "error": ""}
    got = rr.draft_body(CASE, "codex", "", claude=fake_codex)
    assert got["error"] == "" and "Tuesday works" in got["body"] and got["model"] == "codex"
    assert "=== THE REQUEST ===" in seen["prompt"]
    got = rr.draft_body(CASE, "codex", "", claude=lambda p, m, t: {"text": "", "error": "codex exit 1: x"})
    assert got["error"].startswith("codex exit 1") and got["body"] == ""


def test_codex_text_runs_as_the_resident_read_only(monkeypatch):
    import codex_cli
    seen = {}

    def fake_run(prompt, **kw):
        seen.update(kw)
        return {"output": "body", "harness_error": ""}
    monkeypatch.setattr(codex_cli, "run", fake_run)
    out = rr.codex_text("P", "", 30)
    assert out == {"text": "body", "error": "", "cost_usd": None}
    assert seen["sandbox"] == "read-only" and seen["prefix"][2:4] == ["-u", "ea-bot"]


def test_unchanged_compares_code_models_and_switches():
    now = {"commit": "abc", "claude_model": "m1", "remote_model": "q", "switches": {"mail": "enforce"}}
    assert rr.unchanged(now, dict(now))
    assert not rr.unchanged(now, None)
    assert not rr.unchanged(now, {**now, "commit": "def"})
    assert not rr.unchanged(now, {**now, "remote_model": "q2"})
    assert not rr.unchanged(now, {**now, "switches": {"mail": "sealed"}})


# ── drafting checks ────────────────────────────────────────────────────

def test_draft_checks_catch_the_known_tells():
    case = {"must_include": ["Friday"], "must_not_include": ["ARO"], "max_words": 20}
    good = "Thanks Maria. I will confirm the speedtype by Friday.\n\nKit"
    assert all(c["pass"] for c in rr.draft_checks(good, case))
    by_name = lambda body: {c["name"]: c for c in rr.draft_checks(body, case)}  # noqa: E731
    assert not by_name("Thanks Maria — Friday.")["no_em_dash"]["pass"]
    assert not by_name("Thanks Maria -- Friday.")["no_em_dash"]["pass"]
    assert not by_name("Friday works, which is why I will do it.")["no_significance_clause"]["pass"]
    assert not by_name("I genuinely think Friday works.")["no_llm_adverbs"]["pass"]
    assert not by_name("Charge the ARO award. Friday.")["excludes:ARO"]["pass"]
    assert not by_name("Thanks Maria.")["includes:Friday"]["pass"]
    assert not by_name("Friday " * 30)["max_words:20"]["pass"]
    assert not by_name("")["non_empty"]["pass"]


def test_every_case_file_loads():
    for lane in ("drafting", "scheduling"):
        cases = rr.load_cases(lane)
        assert cases, lane
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), "duplicate case id"
        for c in cases:
            assert c["id"].startswith(lane + "-")
            assert c["category"] in ("regression", "quality")


def test_load_cases_refuses_a_bad_file(tmp_path, monkeypatch):
    lane = tmp_path / "drafting"
    lane.mkdir()
    (lane / "x.json").write_text(json.dumps({"id": "drafting-9", "name": "n", "category": "nope",
                                             "to": "a@b", "brief": "b"}))
    monkeypatch.setattr(rr, "CASES", tmp_path)
    with pytest.raises(ValueError):
        rr.load_cases("drafting")


# ── case runners with injected models ──────────────────────────────────

CASE = {"id": "drafting-t", "name": "t", "category": "regression", "to": "jo.foxworth53@example.edu",
        "brief": "say thanks", "must_include": ["thanks"], "max_words": 50}


def test_drafting_case_remote_writer_and_pinned_judge(monkeypatch):
    monkeypatch.setattr(rr, "store_dir", lambda: Path("/tmp/does-not-matter"))
    writer = lambda to, brief, out: ({"status": "ok", "body": "Thanks, Sam.\n\nKit"}, 0)  # noqa: E731
    seen = {}

    def judge(prompt, model):
        seen["model"] = model
        assert "Thanks, Sam." in prompt and "say thanks" in prompt
        return {"text": '{"score": 9, "problems": [], "note": "fine"}', "error": ""}

    import email_writer  # noqa: F401  (the runner imports it; the writer itself is replaced)
    monkeypatch.setattr(rr, "draft_body",
                        lambda case, owner, model, writer=None, claude=None:
                        {"body": writer(case["to"], case["brief"], "")[0]["body"], "error": "",
                         "model": "qwen"})
    r = rr.run_drafting_case(CASE, "remote", "claude-x", writer=writer, judge=judge)
    assert r["pass"] and r["score"] == 0.9 and r["rubric"] == 9
    assert seen["model"] == rr.JUDGE_MODEL
    assert r["owner"] == "remote" and r["model"] == "qwen"


def test_drafting_case_deterministic_failure_caps_the_score(monkeypatch):
    monkeypatch.setattr(rr, "draft_body",
                        lambda case, owner, model, writer=None, claude=None:
                        {"body": "Thanks — Sam.", "error": "", "model": "m"})
    judge = lambda prompt, model: {"text": '{"score": 10, "problems": []}', "error": ""}  # noqa: E731
    r = rr.run_drafting_case(CASE, "claude", "m", judge=judge)
    assert not r["pass"] and r["score"] == 0.4
    assert [c for c in r["checks"] if c["name"] == "no_em_dash"][0]["pass"] is False


def test_drafting_case_writer_fallback_is_an_error(monkeypatch):
    monkeypatch.setattr(rr, "draft_body",
                        lambda case, owner, model, writer=None, claude=None:
                        {"body": "", "error": "writer fallback: writer off", "model": "m"})
    r = rr.run_drafting_case(CASE, "remote", "m", judge=lambda p, m: pytest.fail("judge called"))
    assert r["error"].startswith("writer fallback") and not r["pass"] and r["score"] == 0.0


def test_drafting_case_judge_failure_is_an_error(monkeypatch):
    monkeypatch.setattr(rr, "draft_body",
                        lambda case, owner, model, writer=None, claude=None:
                        {"body": "Thanks, Sam.", "error": "", "model": "m"})
    r = rr.run_drafting_case(CASE, "claude", "m",
                             judge=lambda p, m: {"text": "", "error": "claude failed (rc=1): limit"})
    assert r["error"].startswith("judge:") and not r["pass"]


def test_scheduling_case_scores_the_agent_turn(monkeypatch):
    import sched_agent  # noqa: F401
    case = {"id": "scheduling-t", "name": "t", "category": "regression",
            "text": "Priya asked to meet next week; reply yes with times",
            "expect": {"times": True, "no_decline": True, "drafts_reply": True, "sender_only": True},
            "min_times": 2}
    body = ("Hi Priya,\n\nYes. Would Tuesday, October 20 at 2:00 pm or Wednesday, October 21 "
            "at 10:00 am work?\n\nKit")
    turn = {"status": "ok", "reply": "I drafted a reply offering two times.",
            "writes": [{"tool": "create-reply", "args": {"body": body, "sender_only": True}}]}
    r = rr.run_scheduling_case(case, agent=lambda job: (turn, [], []))
    names = {c["name"]: c["pass"] for c in r["checks"]}
    assert names["replied"] and names["proposes_times>=2"] and names["no_decline"]
    assert names["sender_only"] and names["drafts_reply"] and names["no_tool_talk"]
    assert r["pass"] and r["score"] == 1.0 and r["writes"] == ["create-reply"]

    bad = {"status": "ok", "reply": "I ran scheduling_engine.py and it failed.",
           "writes": [{"tool": "create-reply",
                       "args": {"body": "Hi Priya, unfortunately I cannot make next week."}}]}
    r = rr.run_scheduling_case(case, agent=lambda job: (bad, [], []))
    names = {c["name"]: c["pass"] for c in r["checks"]}
    assert not names["no_decline"] and not names["no_tool_talk"] and not names["proposes_times>=2"]
    assert not r["pass"]

    # Ask-first: a turn that proposes in chat and stages nothing passes the
    # sender-only rule (there is no draft to bind) but must still name times.
    chat_only = {"status": "ok", "reply": "Tue Oct 20 2pm conflicts with your lecture; Wed Oct 21 "
                                          "10am is free. Want me to reply to Dana?", "writes": []}
    case["min_times"] = 1
    case["must_mention"] = ["Tue", "Wed", "Fri"]
    r = rr.run_scheduling_case(case, agent=lambda job: (chat_only, [], []))
    names = {c["name"]: c["pass"] for c in r["checks"]}
    assert names["sender_only"] and names["proposes_times>=1"] and not names["drafts_reply"]
    assert names["mentions:Tue"] and names["mentions:Wed"] and not names["mentions:Fri"]


def test_scheduling_case_agent_failure_is_an_error():
    case = {"id": "scheduling-t", "name": "t", "category": "quality", "text": "x"}

    def boom(job):
        raise RuntimeError("endpoint down")

    r = rr.run_scheduling_case(case, agent=boom)
    assert "endpoint down" in r["error"] and not r["pass"]
    r = rr.run_scheduling_case(case, agent=lambda job: ({"status": "error", "reply": ""}, [], []))
    assert r["error"].startswith("agent status error")


def test_briefing_case_maps_the_eval_result():
    fixture = {"id": "regression-001", "name": "n", "category": "regression", "bug_ref": "b"}
    ev = {"pass": True, "score": 1.0, "assertions": [{"assertion": "a", "pass": True, "reason": ""}],
          "output_full": "briefing text"}
    r = rr.run_briefing_case(fixture, "m", 10, run_eval=lambda f, mode, model, t: ev)
    assert r["pass"] and r["score"] == 1.0 and r["category"] == "regression" and r["owner"] == "claude"
    fixture["category"] = "hard-success"
    ev2 = {"pass": False, "score": 0.0, "assertions": [], "error": "claude CLI failure message: limit"}
    r = rr.run_briefing_case(fixture, "m", 10, run_eval=lambda f, mode, model, t: ev2)
    assert r["category"] == "quality" and r["error"] and not r["pass"]


# ── repeats, summary, verdict, baseline ────────────────────────────────

def _res(lane, cid, cat, score, ok, err=""):
    return {"lane": lane, "id": cid, "category": cat, "score": score, "pass": ok, "error": err}


def test_fold_repeats_takes_the_median_and_requires_every_pass():
    runs = [_res("drafting", "d", "quality", 0.9, True), _res("drafting", "d", "quality", 0.5, False),
            _res("drafting", "d", "quality", 0.8, True)]
    f = rr.fold_repeats(runs)
    assert f["score"] == 0.8 and f["pass"] is False and f["repeats"] == 3 and f["pass_rate"] == 0.67
    f = rr.fold_repeats([_res("d", "d", "q", 0.0, False, "err"), _res("d", "d", "q", 0.7, True)])
    assert f["score"] == 0.7 and f["pass"] and f["errors"] == 1
    f = rr.fold_repeats([_res("d", "d", "q", 0.0, False, "err")])
    assert f["error"] == "err"


def test_verdicts():
    results = [_res("briefing", "regression-001", "regression", 1.0, True),
               _res("briefing", "hard-success-001", "quality", 0.6, False),
               _res("drafting", "drafting-001", "regression", 0.9, True),
               _res("scheduling", "scheduling-001", "regression", 0.0, False, "endpoint down")]
    lanes = rr.summarize(results, [{"lane": "scheduling", "id": "scheduling-002", "why": "off"}])
    assert lanes["briefing"] == {"n": 2, "measured": 2, "passed": 1, "failed": 1, "errors": 0,
                                 "skipped": 0, "mean": 0.8, "regressions_failed": []}
    assert lanes["scheduling"]["errors"] == 1 and lanes["scheduling"]["skipped"] == 1
    assert lanes["scheduling"]["mean"] is None
    assert rr.verdict_for(lanes, None) == ("green", [])

    base = {"run_id": "v1.1.0-x", "lanes": {"briefing": {"mean": 0.98}, "drafting": {"mean": 0.9}}}
    v, why = rr.verdict_for(lanes, base)
    assert v == "amber" and "briefing mean fell 0.98 -> 0.80" in why[0]

    results[2] = _res("drafting", "drafting-001", "regression", 0.2, False)
    v, why = rr.verdict_for(rr.summarize(results, []), base)
    assert v == "red" and any("drafting: drafting-001" in w for w in why)

    errs = [_res("briefing", f"r{i}", "regression", 0.0, False, "limit") for i in range(3)] + [
        _res("briefing", "r9", "regression", 1.0, True)]
    v, why = rr.verdict_for(rr.summarize(errs, []), base)
    assert v == "harness" and "3 of 4" in why[0]


def test_pick_baseline_skips_this_run_and_unmeasured_runs():
    runs = [{"run_id": "a", "verdict": "green", "lanes": {}},
            {"run_id": "b", "verdict": "harness", "lanes": {}},
            {"run_id": "c", "verdict": "green", "lanes": {}}]
    assert rr.pick_baseline(runs, run_id="c")["run_id"] == "a"
    assert rr.pick_baseline(runs, run_id="d")["run_id"] == "c"
    assert rr.pick_baseline(runs, run_id="d", explicit="b")["run_id"] == "b"
    assert rr.pick_baseline([], run_id="x") is None
    with pytest.raises(ValueError):
        rr.pick_baseline(runs, explicit="zz")


# ── the CLI end to end, models replaced ────────────────────────────────

@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path))
    # main sets the canned-calendar variable for the process when no token
    # is readable; record it so teardown restores the state before the test
    monkeypatch.setenv("EA_CANNED_CALENDAR", "")
    monkeypatch.delenv("EA_CANNED_CALENDAR")
    monkeypatch.setattr(rr, "switches", lambda: {"drafting": "remote", "scheduling": "off",
                                                 "_model": "qwen-27b", "_endpoint": "https://x"})
    monkeypatch.setattr(rr, "claude_model", lambda: "claude-test")
    monkeypatch.setattr(rr, "fingerprint", lambda sw, model, harness="claude": {
        "commit": "c1", "claude_model": model, "remote_model": sw["_model"], "switches": {"drafting": "remote"},
        "harness": harness})
    calls = []

    def drafting(case, owner, model, writer=None, claude=None, judge=None):
        calls.append(("drafting", case["id"], owner))
        return _res("drafting", case["id"], case["category"], 0.9, True) | {"checks": [], "duration_s": 1}

    monkeypatch.setattr(rr, "run_drafting_case", drafting)
    monkeypatch.setattr(rr, "run_scheduling_case",
                        lambda case: pytest.fail("scheduling ran with the switch off"))
    monkeypatch.setattr(rr, "run_briefing_case",
                        lambda f, model, timeout, run_eval=None, harness="claude": pytest.fail("briefing ran"))
    return tmp_path, calls


def test_dry_run_lists_routing_and_calls_nothing(harness, capsys):
    tmp, calls = harness
    assert rr.main(["--dry-run"]) == rr.EXIT_GREEN
    out = capsys.readouterr().out
    assert "drafting: remote" in out and "scheduling: skip" in out
    assert "skip scheduling/scheduling-001" in out
    assert not calls and not (tmp / "release-tests").exists()


def test_a_run_stores_results_and_the_fingerprint_then_skips_when_unchanged(harness, capsys):
    tmp, calls = harness
    rc = rr.main(["--lane", "drafting", "--lane", "scheduling", "--tag", "vtest"])
    assert rc == rr.EXIT_GREEN
    out = capsys.readouterr().out
    assert "Release tests vtest-" in out and "GREEN" in out
    store = tmp / "release-tests"
    runs = [json.loads(line) for line in (store / "runs.jsonl").read_text().splitlines()]
    assert len(runs) == 1 and runs[0]["verdict"] == "green" and runs[0]["partial"] is True
    assert runs[0]["lanes"]["scheduling"]["skipped"] == len(rr.load_cases("scheduling"))
    assert runs[0]["owners"]["drafting"]["owner"] == "remote"
    full = json.loads(next((store / "runs").glob("vtest-*.json")).read_text())
    assert {r["id"] for r in full["results"]} == {c["id"] for c in rr.load_cases("drafting")}
    assert {("drafting", c["id"], "remote") for c in rr.load_cases("drafting")} == set(calls)
    # A partial run is no baseline for --skip-if-unchanged.
    assert not (store / "last-run.json").exists()

    (store / "last-run.json").write_text(json.dumps(runs[0]["fingerprint"] | {"run_id": "prior"}))
    assert rr.main(["--skip-if-unchanged", "--lane", "drafting"]) == rr.EXIT_SKIPPED
    assert "unchanged since prior" in capsys.readouterr().out


def test_second_run_compares_to_the_first(harness, capsys, monkeypatch):
    tmp, calls = harness
    assert rr.main(["--lane", "drafting", "--tag", "v1"]) == rr.EXIT_GREEN
    capsys.readouterr()

    def worse(case, owner, model, writer=None, claude=None, judge=None):
        ok = case["category"] != "regression"
        return _res("drafting", case["id"], case["category"], 0.3, ok) | {"checks": [], "duration_s": 1}

    monkeypatch.setattr(rr, "run_drafting_case", worse)
    posted = []
    monkeypatch.setattr(rr, "_alert", lambda text: posted.append(text))
    assert rr.main(["--lane", "drafting", "--tag", "v2", "--alert"]) == rr.EXIT_FAIL
    out = capsys.readouterr().out
    assert "RED" in out and "regression case failed" in out and "(baseline v1-" in out
    assert posted and "red" in posted[0]


def test_release_tests_toggle_defaults_on(tmp_path, monkeypatch):
    """The weekly toggle is the owner's evals.json (eval_gates), on when
    absent; ea-config.json no longer holds it."""
    sys.path.insert(0, str(REPO / "tests" / "evals"))
    import eval_gates
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    assert eval_gates.enabled("release") is True
    (priv / "evals.json").write_text(json.dumps({"release_tests_enabled": False}))
    os.chmod(priv / "evals.json", 0o600)
    assert eval_gates.enabled("release") is False
    weekly = (REPO / "tests" / "release" / "run_weekly.sh").read_text()
    assert "eval_gates.py\" enabled release" in weekly and "ea_config" not in weekly


def test_expand_dates_lands_in_the_future():
    import datetime as dt
    today = dt.date(2026, 10, 17)
    out = rr.expand_dates({"a": "{{+7d}} at 10", "b": ["{{-1d}}T13:00:00Z", "{{+6d:%A}}"], "n": 3}, today)
    assert out == {"a": "2026-10-24 at 10", "b": ["2026-10-16T13:00:00Z", "Friday"], "n": 3}


def test_pipeline_case_checks_the_shape():
    case = {"id": "scheduling-p", "name": "p", "category": "regression", "text": "send the invite to Dana",
            "pipeline": {"thread": [{"id": "m1"}], "expect": {"status": "ok", "kind": "invite", "hold": False,
                                                            "attendees": ["sage.bancroft74@example.edu"], "subject": "Sync", "starts_at": "2026-10-23T10:00"}}}
    rec = {"status": "ok", "kind": "invite", "reply": ":calendar: staged", "picked": [],
           "event": {"subject": "Seminar sync", "start": "2026-10-23T10:00:00-06:00", "attendees": "sage.bancroft74@example.edu", "online": True}}
    r = rr.run_scheduling_case(case, pipeline=lambda text, **kw: rec)
    assert r["pass"] and r["score"] == 1.0 and r["arm"] == "pipeline" and "--- event ---" in r["output"]
    counter = {"id": "scheduling-c", "name": "c", "category": "regression", "text": "handle Oakley",
               "pipeline": {"thread": [{"id": "m1"}], "expect": {"status": "ok", "counter": True, "min_picks": 2}}}
    rec = {"status": "ok", "counter": 0, "picked": ["o1"], "problems": ["same-day"], "reply": "x", "body": "Hi"}
    r = rr.run_scheduling_case(counter, pipeline=lambda text, **kw: rec)
    names = {c["name"]: c["pass"] for c in r["checks"]}
    assert not names["counter:True"] and not names["picks>=2"] and names["no_unrepaired_problems"] and not r["pass"]
    rec = {"status": "ok", "counter": 1, "picked": ["o1", "o2"], "problems": ["clock-in-opening"], "reply": "x", "body": "Hi"}
    r = rr.run_scheduling_case(counter, pipeline=lambda text, **kw: rec)
    assert not {c["name"]: c["pass"] for c in r["checks"]}["no_unrepaired_problems"]
    rec = {"status": "no-options", "picked": [], "reply": ""}
    r = rr.run_scheduling_case(counter, pipeline=lambda text, **kw: rec)
    assert not r["pass"] and not r.get("error") and r["checks"][0]["name"] == "status:ok"
    r = rr.run_scheduling_case(counter, pipeline=lambda text, **kw: {"status": "solver-failed", "error": "rc=1"})
    assert r["error"].startswith("pipeline solver-failed")


def test_load_cases_expands_pipeline_dates_and_requires_a_thread(tmp_path, monkeypatch):
    lane = tmp_path / "scheduling"
    lane.mkdir()
    (lane / "a.json").write_text(json.dumps({"id": "scheduling-a", "name": "n", "category": "quality", "text": "t",
                                             "pipeline": {"thread": [{"text": "see {{+1d}}"}]}}))
    monkeypatch.setattr(rr, "CASES", tmp_path)
    c = rr.load_cases("scheduling")[0]
    assert "{{" not in c["pipeline"]["thread"][0]["text"]
    (lane / "b.json").write_text(json.dumps({"id": "scheduling-b", "name": "n", "category": "quality", "text": "t",
                                             "pipeline": {"thread": []}}))
    with pytest.raises(ValueError):
        rr.load_cases("scheduling")


# ── the canned calendar ────────────────────────────────────────────────

def test_calendar_tokens_names_only_readable_token_files(tmp_path):
    assert rr.calendar_tokens(tmp_path) == []
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "msal_token_cache.json").write_text("{}")
    (auth / "google_token_you_at_gmail_com.json").write_text("{}")
    (auth / "google_client_secret.json").write_text("{}")
    assert rr.calendar_tokens(tmp_path) == ["msal_token_cache.json", "google_token_you_at_gmail_com.json"]


def test_calendar_mode_is_canned_by_default_only_without_a_token():
    live = rr.calendar_mode(None, False, ["msal_tokens.json"])
    assert live["mode"] == "live" and "msal_tokens.json" in live["why"]
    default = rr.calendar_mode(None, False, [])
    assert default["mode"] == "canned" and default["path"] == str(rr.CANNED_CALENDAR)
    assert "no Graph or Google token" in default["why"]
    forced = rr.calendar_mode("/x/cal.json", False, ["msal_tokens.json"])
    assert forced == {"mode": "canned", "path": "/x/cal.json", "why": "--canned-calendar"}
    assert rr.calendar_mode(None, True, [])["mode"] == "live"


def test_the_default_fixture_loads_and_covers_the_cases():
    import scheduling_engine as se
    cal = se.load_canned_calendar(rr.CANNED_CALENDAR)
    assert cal["outlook"] and cal["google"] and cal["ea_managed"]
    assert {e["subject"].split()[0] for e in cal["ea_managed"]} >= {"wh", "nm"}
    assert any(e.get("isAllDay") for e in cal["outlook"])
    assert any("EA Hold" in (e.get("categories") or []) for e in cal["outlook"])
    # every sender the scheduling cases name has an entry, so check-attendees answers
    senders = set()
    for case in rr.load_cases("scheduling"):
        for m in (case.get("pipeline") or {}).get("thread") or []:
            senders.add(m["from"]["address"].lower())
    assert senders <= set(cal["attendees"])


def test_dry_run_says_canned_when_no_token_is_readable(harness, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("EA_DIR", str(tmp_path / "no-such-ea"))
    assert rr.main(["--dry-run", "--lane", "scheduling"]) == rr.EXIT_GREEN
    out = capsys.readouterr().out
    assert "calendar: canned" in out and "no Graph or Google token readable" in out
    assert os.environ.get("EA_CANNED_CALENDAR") == str(rr.CANNED_CALENDAR)
    monkeypatch.delenv("EA_CANNED_CALENDAR", raising=False)
    assert rr.main(["--dry-run", "--lane", "scheduling", "--live-calendar"]) == rr.EXIT_GREEN
    assert "calendar: live" in capsys.readouterr().out and "EA_CANNED_CALENDAR" not in os.environ


def test_a_run_records_the_calendar_mode(harness, capsys, monkeypatch, tmp_path):
    tmp, _calls = harness
    monkeypatch.setenv("EA_DIR", str(tmp_path / "no-such-ea"))
    assert rr.main(["--lane", "drafting", "--tag", "vcal"]) == rr.EXIT_GREEN
    out = capsys.readouterr().out
    assert "calendar canned" in out
    run = json.loads((tmp / "release-tests" / "runs.jsonl").read_text().splitlines()[-1])
    assert run["calendar"]["mode"] == "canned" and run["harness"] == "claude"
    assert rr.main(["--lane", "drafting", "--tag", "vmissing", "--canned-calendar", str(tmp_path / "none.json")]) \
        == rr.EXIT_HARNESS


# ── the harness ────────────────────────────────────────────────────────

def test_briefing_case_passes_the_harness_only_when_it_is_not_claude():
    fixture = {"id": "regression-001", "name": "n", "category": "regression"}
    ev = {"pass": True, "score": 1.0, "assertions": []}
    seen = []

    def run_eval(f, mode, model, t, **kw):
        seen.append(kw)
        return ev

    r = rr.run_briefing_case(fixture, "m", 10, run_eval=run_eval)
    assert seen == [{}] and r["harness"] == "claude"
    r = rr.run_briefing_case(fixture, "m", 10, run_eval=run_eval, harness="codex")
    assert seen[-1] == {"harness": "codex"} and r["harness"] == "codex"
    # a run_eval without the keyword still works on the default harness
    r = rr.run_briefing_case(fixture, "m", 10, run_eval=lambda f, mode, model, t: ev)
    assert r["pass"]


def test_baseline_is_per_harness():
    runs = [{"run_id": "a", "verdict": "green", "lanes": {}},
            {"run_id": "b", "verdict": "green", "lanes": {}, "harness": "codex"},
            {"run_id": "c", "verdict": "harness", "lanes": {}, "harness": "codex"}]
    assert rr.pick_baseline(runs, run_id="x")["run_id"] == "a"
    assert rr.pick_baseline(runs, run_id="x", harness="codex")["run_id"] == "b"
    assert rr.pick_baseline(runs, run_id="b", harness="codex") is None
    with pytest.raises(ValueError):
        rr.pick_baseline(runs, run_id="x", explicit="a", harness="codex")
    assert rr.pick_baseline(runs, run_id="x", explicit="b", harness="codex")["run_id"] == "b"


def test_harness_is_in_the_fingerprint_and_the_run(harness, capsys, monkeypatch):
    tmp, _calls = harness
    now = {"commit": "c", "claude_model": "m", "remote_model": "q", "switches": {}}
    assert rr.unchanged(now, dict(now)) and rr.unchanged(now | {"harness": "claude"}, dict(now))
    assert not rr.unchanged(now | {"harness": "codex"}, dict(now))
    assert rr.main(["--lane", "drafting", "--tag", "vclaude"]) == rr.EXIT_GREEN
    capsys.readouterr()
    assert rr.main(["--lane", "drafting", "--tag", "vcodex", "--harness", "codex"]) == rr.EXIT_GREEN
    out = capsys.readouterr().out
    assert "on codex" in out and "baseline" not in out
    runs = [json.loads(line) for line in (tmp / "release-tests" / "runs.jsonl").read_text().splitlines()]
    assert [r["harness"] for r in runs] == ["claude", "codex"]
    assert runs[1]["baseline"] is None and runs[1]["fingerprint"]["harness"] == "codex"
    full = json.loads(next((tmp / "release-tests" / "runs").glob("vcodex-*.json")).read_text())
    assert {r["harness"] for r in full["results"]} == {""}, "remote-owned drafting names no harness"
