"""The weekly audit on the remote model (preprocessor/remote_audit.py)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import owner_trust  # noqa: E402
import remote_audit as ra  # noqa: E402
import remote_briefing as rb  # noqa: E402
import remote_llm as rl  # noqa: E402

NOW = datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc)   # a Wednesday in any US zone
ROWS = [
    {"id": "deadline-report", "description": "Annual report to the sponsor", "status": "open",
     "deadline": "2026-03-20", "type": "deadline", "from_name": "Program Office",
     "conversation_id": "CONV-REPORT", "source_message_id": "m-report"},
    {"id": "response-visit", "description": "Reply to Dana about the lab visit", "status": "open",
     "deadline": "2026-04-05", "type": "response_needed", "from_name": "Dana Quill",
     "conversation_id": "CONV-VISIT", "source_message_id": "m-visit"},
    {"id": "kept-badge", "description": "Badge form", "status": "snoozed",
     "conversation_id": "CONV-KEPT", "kept_from_claude": "mil"},
    {"id": "reimb-trip", "description": "Reimburse the March trip", "status": "open",
     "type": "reimbursement", "last_action": "2026-03-10: receipts filed"},
]
CLOSED = [{"id": "task-slides", "description": "Send the slides", "closed_at": "2026-03-12"}]
CLOSURE = {"hit_rate": 0.75, "autonomous_closures": 3, "manual_closures": 1,
           "provenance": {"scrubber": 2, "manual": 1, "inbox_archive": 1},
           "user_stated_closures_logged": 1, "false_positive_candidates": [],
           "suggested_signals": {"sender:example.org": 1}}
BRIEFING = {"new_dropped_threads": [
    {"conversation_id": "CONV-DROP", "subject": "Seminar invite", "from_name": "Lee",
     "days_stale": 9, "body_preview": "Could you speak?", "message_id": "m-drop"},
    {"conversation_id": "CONV-VISIT", "subject": "dup of a row", "from_name": "Dana"}],
    "project_hygiene": {"candidates": [{"id": "old-proj", "name": "Old project",
                                        "last_activity": "2026-01-02"}]}}


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "audits": "remote"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def _model(plan):
    def chat(messages, **kw):
        chat.calls.append(kw)
        return json.loads(json.dumps(plan)), {"latency_ms": 5}
    chat.calls = []
    return chat


def _run(plan=None, **kw):
    queued = []
    args = dict(run_id="r", now=NOW, rows=ROWS, closed=CLOSED, closure=CLOSURE,
                queue=lambda items, agent, now: queued.extend(items) or len(items),
                audit=lambda *a, **k: {})
    args.update(kw)
    if plan is not None:
        args["chat"] = _model(plan)
    text, rec = ra.run(BRIEFING, **args)
    return text, rec, queued


def test_candidates_put_deadlines_first_and_skip_covered_threads():
    c = ra.candidates(ROWS, BRIEFING, today=NOW.date())
    assert c["L1"]["ledger_id"] == "deadline-report" and c["L1"]["days_left"] == 2
    assert [v["title"] for k, v in c.items() if k.startswith("D")] == ["Seminar invite"]


def test_a_run_renders_the_template_and_queues_hand_offs(priv):
    c = ra.candidates(ROWS, BRIEFING, today=NOW.date())
    ref = {v["ledger_id"] or v["title"]: k for k, v in c.items()}
    plan = {"critical": [{"ref": ref["deadline-report"], "why": "Due Friday; no draft yet."}],
            "important": [{"ref": ref["response-visit"], "why": "Visit dates need an answer."},
                          {"ref": "L99", "why": "invented"}],
            "patterns": ["Sponsor mail waits longest."],
            "handoffs": [{"ref": ref["deadline-report"], "why": "a report", "task": "draft the report"},
                         {"ref": ref["kept-badge"], "why": "kept", "task": "read it"}]}
    text, rec, queued = _run(plan)
    assert text.startswith("📊 Weekly Audit, week of Mar 16")
    assert "*Annual report to the sponsor* (Program Office), due 2026-03-20: Due Friday" in text
    assert "Reply to Dana about the lab visit" in text and "invented" not in text
    assert "• Send the slides" in text
    assert "Reimburse the March trip: 2026-03-10: receipts filed" in text
    assert "Sponsor mail waits longest." in text
    assert "old-proj: Old project, last activity 2026-01-02" in text
    assert "Hit rate: 75% (3 autonomous / 1 manual)" in text and "No rule changes proposed." in text
    assert "📋 Obligations: 3 open, 1 snoozed, 1 closed this week" in text
    assert "—" not in text and "–" not in text
    assert [q["conversation_id"] for q in queued] == ["CONV-REPORT"]
    assert rec["handed"] == 1 and rec["plan"]["important"] == 1


def test_no_reimbursement_rows_says_so(priv):
    text, _rec, _q = _run({"critical": [], "important": [], "patterns": [], "handoffs": []},
                          rows=ROWS[:3])
    assert "• No open reimbursement items." in text


def test_dry_run_queues_nothing(priv):
    c = ra.candidates(ROWS, BRIEFING, today=NOW.date())
    plan = {"critical": [], "important": [], "patterns": [],
            "handoffs": [{"ref": "L1", "why": "x", "task": "y"}]}
    _text, _rec, queued = _run(plan, dry_run=True)
    assert queued == [] and c["L1"]["conversation_id"] == "CONV-REPORT"


def test_the_pre_send_audit_drops_closed_rows(priv):
    plan = {"critical": [{"ref": "L1", "why": "due soon"}], "important": [], "patterns": [],
            "handoffs": []}
    text, _rec, _q = _run(plan, audit=lambda *a, **k: {"closed": ["deadline-report"], "parked": []})
    assert "Annual report to the sponsor" not in text.split("🟡")[0]


def test_fallback_lists_deadlines_from_code(priv):
    def down(messages, **kw):
        raise rl.RemoteLLMError("no answer")
    text, rec, queued = _run(chat=down)
    assert "deadlines first" in text and "Annual report to the sponsor" in text
    assert rec["fallback"] == "no answer" and queued == []


def test_signals_without_backtests_never_become_a_proposal(priv):
    m = dict(CLOSURE, suggested_signals={"sender:example.org": 3})
    text, _rec, _q = _run({"critical": [], "important": [], "patterns": [], "handoffs": []},
                          closure=m)
    assert "needs a backtest first" in text and "PROPOSAL" not in text


def test_off_switch_exits_three(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "_owner", lambda: False)
    assert ra.main(["run", "--briefing", str(tmp_path / "b.json"), "--run-id", "r"]) == ra.EXIT_OFF


def test_switch_and_run_agent_wiring():
    assert rl.SWITCHES["audits"] == ("off", "remote")
    src = (ROOT / "run-agent.sh").read_text()
    i = src.index("    weekly-audit)\n        # The weekly audit on the remote model")
    body = src[i:i + 1200]
    assert "remote_audit.py\" run" in body and 'elif [[ "$REMOTE_RC" != "3" ]]' in body
    assert "REMOTE_BRIEFING=1" in body.split("elif")[0]
    assert "REMOTE_BRIEFING=1" not in body.split("elif")[1].split(";;")[0]


def test_a_row_kept_by_its_brief_or_the_records_is_never_handed_off(priv):
    """A row whose thread brief says the thread stays on the machine, and
    one only the mail's records keep: the shared check (keep_gate) holds
    both back from the hand-off, and the post names them as the owner's."""
    rows = ROWS + [
        {"id": "row-review", "description": "Review for the journal", "status": "open",
         "deadline": "2026-03-19", "type": "deadline", "conversation_id": "CONV-REVIEW",
         "thread_brief": {"conversation_id": "CONV-REVIEW", "keep_level": "local",
                          "kept_level": "Kept on your machine"}},
        {"id": "row-contract", "description": "Partner follow-up", "status": "open",
         "deadline": "2026-03-21", "type": "task", "conversation_id": "CONV-PARTNER"}]
    c = ra.candidates(rows, BRIEFING, today=NOW.date())
    ref = {v["ledger_id"] or v["title"]: k for k, v in c.items()}
    plan = {"critical": [{"ref": ref["row-review"], "why": "Due tomorrow."}], "important": [],
            "patterns": [],
            "handoffs": [{"ref": ref["row-review"], "why": "a review", "task": "read the manuscript"},
                         {"ref": ref["row-contract"], "why": "a reply", "task": "draft it"},
                         {"ref": ref["deadline-report"], "why": "a report", "task": "draft the report"}]}
    records = lambda i: "local" if i.get("conversation_id") == "CONV-PARTNER" else "full"  # noqa: E731
    text, rec, queued = _run(plan, rows=rows, keep_records=records)
    assert [q["conversation_id"] for q in queued] == ["CONV-REPORT"] and rec["handed"] == 1
    kept = text.split("🔒 Kept on your own model, so no hosted model works on these. They need you:")[1]
    assert "• Review for the journal" in kept and "• Partner follow-up" in kept
    assert "read the manuscript" not in text
    # The planner is told which items stay on the owner's own model.
    items = ra._prompt_items(c)
    assert next(i for i in items if i["ref"] == ref["row-review"]).get("kept") is True
