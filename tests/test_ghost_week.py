"""ghost_week: the ghost mail value (text served, sealed on the side,
recorded), the sealed records after a cutover, the judge on a fake
runner, the measures, the verdicts, the gate on the cutover switches,
the tripwire and the bot's turn note. Nothing here reaches the network,
the mailbox or the real private dir."""
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

# The fixture's ghost window ends in the future on every run, so the
# tests do not expire with the calendar.
GHOST_UNTIL = (date.today() + timedelta(days=30)).isoformat()
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402
import ghost_week as gw  # noqa: E402

NOW = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 14)
GOOD = {"sensitivity": "normal", "confidential_kind": "none", "needs_reply": True,
        "ask": "Send the slides.", "deadline": "2026-10-15", "deadline_text": "by Wednesday",
        "scheduling": "none", "offered_times": [], "people": [{"name": "Pat", "role": "colleague"}],
        "tone": [], "state": "needs-reply", "domain": "research",
        "summary": "Pat wants the slides by Wednesday."}


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {"emerson.vasquez27@example.edu"})
    monkeypatch.setattr(mp, "_profile_line", lambda: "The user is a professor.")
    monkeypatch.setattr(mp, "_default_graph", lambda: object())
    mp._INDEX_CACHE.update(mtime=None, index=None)
    _cfg(d, mail="ghost", ghost={"mail": {"from": "2026-10-12", "until": GHOST_UNTIL}})
    return d


def _cfg(d, **over):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": True}}
    cfg.update(over)
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)


def _store(mid, conv="conv1", result=GOOD, frm="morgan.vasquez41@example.edu"):
    rl.append(mp.EXTRACTIONS, {"ts": "2026-10-14T15:00:00Z", "key": f"{conv}|2026-10-14T14:00:00Z",
                               "id": mid, "conversation_id": conv, "received": "2026-10-14T14:00:00Z",
                               "from": frm, "subject": "Slides", "status": "ok", "result": dict(result)})
    mp._INDEX_CACHE.update(mtime=None, index=None)


def _conversation(mid="AAMk" + "x" * 40 + "1", mid2="AAMk" + "y" * 40 + "2"):
    return json.dumps({"conversation_id": "conv1", "subject": "Slides", "message_count": 2,
                       "participants": ["morgan.vasquez41@example.edu", "emerson.vasquez27@example.edu"],
                       "messages": [
                           {"id": mid, "subject": "Slides", "from": {"address": "morgan.vasquez41@example.edu"},
                            "to": ["emerson.vasquez27@example.edu"], "cc": [], "sent": "2026-10-14T14:00:00Z",
                            "is_draft": False, "has_attachments": False, "conversation_id": "conv1",
                            "text": "Could you send the slides by Wednesday? The deck is at https://x.example/deck"},
                           {"id": mid2, "subject": "RE: Slides", "from": {"address": "emerson.vasquez27@example.edu"},
                            "to": ["morgan.vasquez41@example.edu"], "cc": [], "sent": "2026-10-14T15:00:00Z",
                            "is_draft": False, "has_attachments": False, "conversation_id": "conv1",
                            "text": "Tuesday evening works."}]})


# ── the switch ────────────────────────────────────────────────────────

def test_ghost_is_mail_only_enforcing_and_reads_enforce_past_its_window(priv):
    cfg = rl.config()
    assert cfg["ghost"]["mail"] == {"from": "2026-10-12", "until": GHOST_UNTIL}
    assert rl.mode("mail", cfg, today=TODAY) == "ghost"
    assert rl.enforcing("mail", cfg) and rl.reading("mail", cfg)
    assert rl.mode("mail", cfg, today=date.fromisoformat(GHOST_UNTIL) + timedelta(days=1)) == "enforce"
    _cfg(priv, mail="ghost")                                   # no window: ghost stays on
    assert rl.mode("mail", today=TODAY) == "ghost"
    _cfg(priv, mail="off", scheduling="ghost")
    assert rl.mode("scheduling") == "off"


def test_set_ghost_opens_a_window_and_shadow_days_moves_shadow_until(priv):
    _cfg(priv, mail="enforce", scheduling="off")
    r = rl.set_switch("mail", "ghost", today=TODAY)
    assert r["window"] == {"from": "2026-10-14", "until": "2026-10-20"} and r["effective"] == "ghost"
    r = rl.set_switch("scheduling", "shadow", days=3, today=TODAY)
    assert r["window"] == {"from": "2026-10-14", "until": "2026-10-16"}
    raw = json.loads((priv / rl.CONFIG_FILE).read_text())
    assert raw["shadow_until"] == "2026-10-16" and raw["ghost"]["mail"]["until"] == "2026-10-20"
    assert gw.window("scheduling", rl.config()) == (date(2026, 10, 14), date(2026, 10, 16))
    assert gw.day_number("mail", rl.config(), today=date(2026, 10, 16)) == (3, 7)
    with pytest.raises(ValueError):
        rl.set_switch("mail", "enforce", days=4)


def test_cutover_values_need_a_go_or_force(priv, monkeypatch):
    with pytest.raises(rl.RemoteLLMError, match="no ghost week report for mail"):
        rl.set_switch("mail", "sealed")
    with pytest.raises(rl.RemoteLLMError, match="no ghost week report for scheduling"):
        rl.set_switch("scheduling", "enforce")
    assert rl.set_switch("scheduling", "enforce", force=True)["after"] == "enforce"
    monkeypatch.setattr(gw, "gate", lambda part, **k: (False, "the ghost week for mail ended NO-GO"))
    with pytest.raises(rl.RemoteLLMError, match="NO-GO"):
        rl.set_switch("mail", "sealed")
    monkeypatch.setattr(gw, "gate", lambda part, **k: (True, "GO"))
    assert rl.set_switch("mail", "sealed")["after"] == "sealed"


# ── the reads ─────────────────────────────────────────────────────────

def test_ghost_read_serves_the_text_and_records_the_sealed_copy_on_the_side(priv, monkeypatch):
    mid = "AAMk" + "x" * 40 + "1"
    _store(mid)
    raw = _conversation(mid)
    threads = []
    real = gw.ghost_read_async

    def capture(*a, **k):
        t = real(*a, **k)
        threads.append(t)
        return t
    monkeypatch.setattr(gw, "ghost_read_async", capture)
    monkeypatch.setattr(mp, "_inline_records", lambda msgs, **k: None)   # the own reply stays unread
    out, refusal = mp.release_filter("graph_actions", "get-conversation",
                                     ["--conversation-id", "conv1"], "1a12-room", raw)
    assert refusal == "" and out == raw                         # the lane got the text, untouched
    for t in threads:
        t.join(5)
    reads = rl.read(gw.READS)
    assert len(reads) == 1
    r = reads[0]
    assert r["served"] == "text" and r["lane"] == "bot" and r["verb"] == "get-conversation"
    assert r["messages"] == 2 and r["text_messages"] == 2
    assert r["unread_before"] == 1 and r["unread_after"] == 1 and r["seal_error"] == ""
    assert r["conversations"] == ["conv1"] and r["seal_s"] is not None
    assert "slides" not in json.dumps(r).lower()                 # no text in the reads file
    rawf = json.loads(gw._raw_path(r["id"]).read_text())
    assert "https://x.example/deck" in rawf["raw"]
    assert rawf["sealed"]["sealed"] is True and rawf["sealed"]["messages"][1]["remote_unread"] is True


def test_ghost_keeps_the_cron_rules_and_skips_the_shadow_agents_reads(priv, monkeypatch):
    mid = "AAMk" + "m" * 40 + "9"
    rl.append(mp.EXTRACTIONS, {"ts": "t", "key": "k", "id": mid, "conversation_id": "convK",
                               "received": "2026-10-14T14:00:00Z", "from": "gray.juniper49@example.com", "subject": "s",
                               "keep_from_anthropic": "listed domain mil", "status": "ok",
                               "result": dict(GOOD)})
    mp._INDEX_CACHE.update(mtime=None, index=None)
    raw = json.dumps({"id": mid, "subject": "s", "from": {"address": "gray.juniper49@example.com"}, "to": [], "cc": [],
                      "received": "2026-10-14T14:00:00Z", "body": "x", "conversation_id": "convK"})
    out, refusal = mp.release_filter("graph_actions", "get-message", ["--message-id", mid],
                                     "cron-weekly-audit-1", raw)
    assert out == "" and "kept on the user's machine" in refusal
    assert gw.ghost_read("get-message", [], "shadow-abc", raw) is None
    assert rl.read(gw.READS) == []


def test_sealed_read_is_recorded_with_its_latency_and_a_failure_is_recorded_too(priv, monkeypatch):
    _cfg(priv, mail="sealed")
    mid = "AAMk" + "x" * 40 + "1"
    _store(mid)
    raw = json.dumps({"id": mid, "subject": "Slides", "from": {"address": "morgan.vasquez41@example.edu"},
                      "to": [], "cc": [], "received": "2026-10-14T14:00:00Z",
                      "body": "SECRET", "conversation_id": "conv1"})
    out, refusal = mp.release_filter("graph_actions", "get-message", ["--message-id", mid], "1a12-room", raw)
    assert refusal == "" and "SECRET" not in out and json.loads(out)["sealed"] is True
    r = rl.read(gw.READS)[-1]
    assert r["served"] == "sealed" and r["unread_before"] == 0 and r["unread_after"] == 0
    assert r["sealed_bytes"] and r["seal_s"] is not None
    assert "SECRET" in json.loads(gw._raw_path(r["id"]).read_text())["raw"]
    monkeypatch.setattr(mp, "seal_output", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out, refusal = mp.release_filter("graph_actions", "get-message", ["--message-id", mid], "1a12-room", raw)
    assert out == "" and "refused" in refusal
    r = rl.read(gw.READS)[-1]
    assert r["seal_error"] == "boom" and r["served"] == "sealed"


# ── the turns, the judge, the measures ────────────────────────────────

def test_note_turn_records_while_a_ghost_value_is_set_and_flags_asks_for_text(priv):
    gw.note_turn({"run_id": "1a12-room", "channel": "stream:5", "thread_ts": "t", "room": "ea-x",
                  "lane": "room", "user_msg": "what did Pat ask?", "reply": "Pat asked for the slides.",
                  "tools": ["Bash: graph_actions.py get-conversation"]})
    gw.note_turn({"run_id": "1a13-room", "reply": "I could not read that message yet; please paste the email."})
    turns = rl.read(gw.TURNS)
    assert [t["asked_for_text"] for t in turns] == [False, True]
    _cfg(priv, mail="enforce", scheduling="off")
    gw.note_turn({"run_id": "1a14", "reply": "x"})
    assert len(rl.read(gw.TURNS)) == 2


def _fake_judge(answers):
    seen = []

    def run(prompt, schema, judge):
        seen.append((prompt, judge))
        return dict(answers.pop(0)), {"cost_usd": 0.01}
    run.seen = seen
    return run


def test_grade_mail_judges_one_read_per_turn_and_thread_then_deletes_the_raw(priv, monkeypatch):
    mid = "AAMk" + "x" * 40 + "1"
    _store(mid)
    raw = _conversation(mid)
    monkeypatch.setattr(mp, "_inline_records", lambda msgs, **k: None)
    gw.ghost_read("get-conversation", ["--conversation-id", "conv1"], "1a12-room", raw)
    gw.ghost_read("search-mail", ["--query", "slides"], "1a12-room",
                  json.dumps([{"id": mid, "subject": "Slides", "from": "morgan.vasquez41@example.edu",
                               "conversation_id": "conv1", "body_preview": "Could you send"}]))
    gw.ghost_read("get-message", ["--message-id", mid], "", raw)        # a shell read: never judged
    gw.note_turn({"run_id": "1a12-room", "lane": "room", "user_msg": "reply to Pat",
                  "reply": "Draft created to Pat with the deck link."})
    monkeypatch.setattr(gw, "_journal_drafts", lambda rid: ["d1"] if rid == "1a12-room" else [])
    monkeypatch.setattr(gw, "_draft_bodies", lambda ids: [{"to": ["morgan.vasquez41@example.edu"], "subject": "RE: Slides",
                                                           "body": "Hi Pat, here is the deck: https://x.example/deck"}])
    judge = _fake_judge([{"facts_used": [{"fact": "deck URL", "in_record": False, "category": "url"},
                                         {"fact": "due Wednesday", "in_record": True, "category": "date"}],
                          "record_errors": [{"field": "deadline", "record": "2026-10-15", "thread": "Wednesday Oct 14"}],
                          "verdict": "degraded", "enhancement": "Carry each URL in the record."}])
    res = gw.grade_mail(runner=judge, now=NOW)
    assert res == {"judged": 1, "covered": 1, "errors": 0, "verdicts": {"degraded": 1},
                   "cost_usd": 0.01, "raw_purged": 0}
    prompt, which = judge.seen[0]
    assert which == "claude" and "https://x.example/deck" in prompt and "here is the deck" in prompt
    assert "remote_unread" in prompt and "`get-conversation` in a bot lane" in prompt
    grades = rl.read(gw.MAIL_GRADES)
    g = [x for x in grades if x.get("verdict")][0]
    assert g["facts_missing"] == [{"fact": "deck URL", "category": "url"}]
    assert g["critical_errors"] == 1 and g["has_artifacts"] is True and g["judge"] == "claude"
    assert sum(1 for x in grades if x.get("covered_by") == g["read_id"]) == 1
    assert not list((priv / rl.STORE_DIRNAME / gw.RAW_DIR).glob("*.json")) or \
        len(list((priv / rl.STORE_DIRNAME / gw.RAW_DIR).glob("*.json"))) == 1   # only the shell read's raw stays
    assert gw.grade_mail(runner=judge, now=NOW)["judged"] == 0


def test_sealed_reads_are_judged_on_the_remote_model(priv, monkeypatch):
    _cfg(priv, mail="sealed")
    mid = "AAMk" + "x" * 40 + "1"
    _store(mid)
    raw = json.dumps({"id": mid, "subject": "Slides", "from": {"address": "morgan.vasquez41@example.edu"}, "to": [],
                      "cc": [], "received": "2026-10-14T14:00:00Z", "body": "SECRET", "conversation_id": "conv1"})
    mp.release_filter("graph_actions", "get-message", ["--message-id", mid], "1a12-room", raw)
    judge = _fake_judge([{"facts_used": [], "record_errors": [], "verdict": "same", "enhancement": ""}])
    gw.grade_mail(runner=judge, now=NOW)
    assert judge.seen[0][1] == "remote" and "SECRET" in judge.seen[0][0]


def test_measure_and_verdict_mail(priv, monkeypatch):
    for i in range(3):
        rl.append(gw.READS, {"id": f"r{i}", "ts": "2026-10-14T10:00:00Z", "verb": "get-conversation",
                             "run_id": f"run{i}", "lane": "bot", "served": "text", "messages": 3,
                             "text_messages": 3, "conversations": [f"c{i}"], "unread_before": 1,
                             "unread_after": 0, "inline_extracted": 1, "seal_s": 20.0 + i,
                             "seal_error": "", "raw_bytes": 9000, "sealed_bytes": 2000})
    rl.append(gw.READS, {"id": "r9", "ts": "2026-10-14T11:00:00Z", "verb": "get-message", "run_id": "run9",
                         "lane": "bot", "served": "text", "messages": 1, "text_messages": 1,
                         "conversations": ["c9"], "unread_before": 1, "unread_after": None,
                         "seal_s": 3.0, "seal_error": "boom", "raw_bytes": 100, "sealed_bytes": None})
    rl.append(gw.READS, {"id": "old", "ts": "2026-10-01T11:00:00Z", "verb": "get-message", "run_id": "x",
                         "lane": "bot", "served": "text", "messages": 1, "text_messages": 1,
                         "seal_error": "", "raw_bytes": 1, "sealed_bytes": 1})
    rl.append(gw.MAIL_GRADES, {"ts": "t", "read_id": "r0", "run_id": "run0", "lane": "bot", "verb": "get-conversation",
                               "verdict": "same", "facts": 3, "facts_missing": [], "record_errors": [],
                               "critical_errors": 0, "enhancement": "", "has_artifacts": True, "cost_usd": 0.02})
    rl.append(gw.MAIL_GRADES, {"ts": "t", "read_id": "r1", "run_id": "run1", "lane": "bot", "verb": "get-conversation",
                               "verdict": "blocked", "facts": 2,
                               "facts_missing": [{"fact": "u", "category": "url"}, {"fact": "n", "category": "number"}],
                               "record_errors": [{"field": "offered_times", "record": "[]", "thread": "Tue 2pm"}],
                               "critical_errors": 1, "enhancement": "Carry each URL in the record.", "cost_usd": 0.02})
    monkeypatch.setattr(gw, "_now", lambda: NOW)
    gw.note_turn({"run_id": "run1", "reply": "Please paste the full text."})
    monkeypatch.setattr(gw, "_draft_outcomes", lambda runs: {"drafts": 0})
    m = gw.measure_mail(date(2026, 10, 12), date(2026, 10, 18))
    assert m["reads"] == 4 and m["seal_failures"] == 1 and m["seal_failure_share"] == 0.25
    assert m["messages"] == 10 and m["unread_after"] == 0 and m["seal_p95_s"] == 22.0
    assert m["judged"] == 2 and m["verdicts"] == {"blocked": 1, "same": 1} and m["blocked_share"] == 0.5
    assert m["missing_by_category"] == {"number": 1, "url": 1} and m["errors_by_field"] == {"offered_times": 1}
    assert m["critical_error_share"] == 0.5 and m["asked_for_text"] == 1 and m["turns_with_reads"] == 1
    assert m["blocked"][0]["run_id"] == "run1" and m["raw_kb"] == 27
    v = gw.verdict_mail(m)
    assert v["verdict"] == "NOT ENOUGH DATA"
    assert [c["ok"] for c in v["checks"]] == [False, False, False, False, True, True]
    m2 = dict(m, judged=40, blocked_share=0.02, critical_error_share=0.0, seal_failure_share=0.0)
    assert gw.verdict_mail(m2)["verdict"] == "GO"
    m3 = dict(m2, seal_p95_s=80.0)
    assert gw.verdict_mail(m3)["verdict"] == "NO-GO"


def _sched_turn(run_id, *, live=False, status="ok", invented=(), lane="sched-channel", ts="2026-10-14T10:00:00-06:00"):
    t = {"run_id": run_id, "ts": ts, "thread_ts": "t1", "lane": lane, "status": status,
         "reply": "How does Tue Oct 20 2:00 pm sound?", "writes": [], "latency_s": 30.0,
         "solver_options": [["2026-10-20T14:00:00-06:00", "2026-10-20T14:30:00-06:00"]],
         "user_msg": "find time with Ana", "task_block": "Find 30 minutes with lane.zabriskie34@example.edu",
         "claude": {"reply": "Tue Oct 20 at 2 pm works. Shall I hold it?", "elapsed_s": 50.0,
                    "tool_calls": [["Bash", 2.0, "scheduling_engine.py propose-meeting"]], "drafts": []},
         "checks": {"shadow": {"times": ["2026-10-20T14:00"], "invented": list(invented), "breaks": [],
                               "mode": "propose", "validity": {"named": 1, "checked": 1, "valid": 1}},
                    "claude": {"times": ["2026-10-20T14:00"], "breaks": [], "mode": "propose", "staged": 0},
                    "mode_agree": True, "overlap": 1.0}}
    if live:
        t.update({"live": True, "fallback": "code reply", "claude": {}})
    return t


def test_grade_sched_judges_shadow_turns_on_claude_and_live_turns_on_the_remote(priv):
    _cfg(priv, mail="off", scheduling="shadow", shadow_until="2026-10-20",
         ghost={"scheduling": {"from": "2026-10-14", "until": "2026-10-20"}})
    rl.append(gw.SCHED_TURNS, _sched_turn("s1"))
    rl.append(gw.SCHED_TURNS, _sched_turn("s2", status="error"))
    rl.append(gw.SCHED_TURNS, _sched_turn("s3", live=True))
    judge = _fake_judge([
        {"shadow_grade": "edit", "claude_grade": "send", "better": "claude",
         "problems": [{"kind": "wording", "note": "no range shown"}], "enhancement": "Show ranges."},
        {"shadow_grade": "send", "claude_grade": "none", "better": "same", "problems": [], "enhancement": ""}])
    res = gw.grade_sched(runner=judge, now=NOW)
    assert res["judged"] == 2 and res["grades"] == {"edit": 1, "send": 1}
    assert [j for _, j in judge.seen] == ["claude", "remote"]
    assert "Claude's reply is a reference, not the truth" in judge.seen[0][0]
    assert "no Claude answer on file" in judge.seen[1][0] and "run for real" in judge.seen[1][0]
    s = gw.measure_sched(date(2026, 10, 14), date(2026, 10, 20))
    assert s["turns"] == 2 and s["errors"] == 1 and s["error_share"] == 0.5
    assert s["judged"] == 1 and s["grades"] == {"edit": 1} and s["problems_by_kind"] == {"wording": 1}
    assert s["claude_grades"] == {"send": 1} and s["better"] == {"claude": 1}
    lv = gw.measure_sched(date(2026, 10, 14), date(2026, 10, 20), live=True)
    assert lv["turns"] == 1 and lv["grades"] == {"send": 1} and lv["fallbacks"] == {"code reply": 1}
    v = gw.verdict_sched(s)
    assert v["verdict"] == "NOT ENOUGH DATA"
    big = dict(s, judged=20, invented_times=0, wrong_share=0.05, send_share=0.7, error_share=0.0,
               latency_p95_s=40.0, outcomes={"events": 4, "shadow_named": 4, "claude_named": 4})
    assert gw.verdict_sched(big)["verdict"] == "GO"
    assert gw.verdict_sched(dict(big, invented_times=1))["verdict"] == "NO-GO"
    assert gw.verdict_sched(dict(big, outcomes={"events": 4, "shadow_named": 2, "claude_named": 4}))["verdict"] == "NO-GO"
    assert gw.verdict_sched(dict(big, outcomes={"events": 1, "shadow_named": 0, "claude_named": 1}))["verdict"] == "GO"


def test_final_writes_the_verdict_the_gate_reads_and_the_daily_step_posts_on_the_last_day(priv, monkeypatch):
    _cfg(priv, mail="ghost", scheduling="shadow", shadow_until="2026-10-18",
         ghost={"mail": {"from": "2026-10-12", "until": "2026-10-18"},
                "scheduling": {"from": "2026-10-12", "until": "2026-10-18"}})
    monkeypatch.setattr(gw, "_draft_outcomes", lambda runs: {"drafts": 0})
    monkeypatch.setattr(gw, "_now", lambda: NOW)
    assert gw.gate("mail")[0] is False
    r = gw.final("mail", today=TODAY)
    assert r["verdict"] == "NOT ENOUGH DATA" and Path(r["path"]).read_text().startswith("# Ghost week: mail")
    ok, why = gw.gate("mail", today=TODAY)
    assert ok is False and "NOT ENOUGH DATA" in why
    data = json.loads((priv / rl.STORE_DIRNAME / "ghost-final-mail.json").read_text())
    data["verdict"] = "GO"
    (priv / rl.STORE_DIRNAME / "ghost-final-mail.json").write_text(json.dumps(data))
    assert gw.gate("mail", today=TODAY) == (True, "the ghost week for mail ended GO on 2026-10-14")
    assert gw.gate("mail", today=date(2026, 11, 20))[0] is False      # too old
    posts = []
    out = gw.daily(post=posts.append, today=date(2026, 10, 18))
    assert set(out["finals"]) == {"mail", "scheduling"}
    assert len(posts) == 1 and "ended today" in posts[0] and "Record quality, mail (day 7 of 7)" in posts[0]
    assert "Ghost week, scheduling (day 7 of 7)" in posts[0]
    assert gw.daily(post=posts.append, today=TODAY)["finals"] == {}


def test_tripwire_posts_once_per_six_hours_after_a_cutover(priv):
    _cfg(priv, mail="sealed")
    for i in range(4):
        rl.append(gw.READS, {"id": f"r{i}", "ts": gw._iso(NOW - timedelta(minutes=5)), "verb": "get-message",
                             "run_id": "x", "lane": "bot", "served": "sealed", "messages": 1,
                             "text_messages": 1, "unread_after": 1, "seal_error": "down" if i < 3 else ""})
    posts = []
    out = gw.tripwire(now=NOW, post=posts.append)
    assert out["posted"] and len(out["alerts"]) == 2 and "3 of 4 reads failed" in posts[0]
    assert "set mail enforce" in posts[0]
    assert gw.tripwire(now=NOW + timedelta(minutes=10), post=posts.append)["posted"] is False
    for i in range(3):
        rl.append(gw.READS, {"id": f"q{i}", "ts": gw._iso(NOW + timedelta(hours=7)), "verb": "get-message",
                             "run_id": "x", "lane": "bot", "served": "sealed", "messages": 1,
                             "text_messages": 1, "unread_after": 0, "seal_error": "down"})
    assert gw.tripwire(now=NOW + timedelta(hours=7), post=posts.append)["posted"] is True
    _cfg(priv, mail="enforce")
    assert gw.tripwire(now=NOW + timedelta(hours=14), post=posts.append) == {"alerts": [], "posted": False}


def test_status_and_report_cli(priv, monkeypatch, capsys):
    monkeypatch.setattr(gw, "_draft_outcomes", lambda runs: {"drafts": 0})
    monkeypatch.setattr(gw._owner_trust, "is_owner_process", lambda: True)
    assert gw.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["mail"] == "ghost" and st["gate"]["mail"][0] is False
    assert gw.main(["report", "--part", "mail"]) == 0
    out = capsys.readouterr().out
    assert "mail: day" in out and "NOT ENOUGH DATA" in out and "Record quality, mail" in out
