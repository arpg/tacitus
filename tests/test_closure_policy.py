"""The closure policy (preprocessor/closure.py): no row closes by mistake,
and every row that is done is closed or put in front of the user.

Pinned here, bottom up:
  1. tier(): which evidence closes which row, and closes_on.
  2. settle(): a close writes its record and event; a proposal keeps the
     row open; a refusal holds until newer evidence; the sibling sweep
     takes only the asks the evidence answers.
  3. the ledger CLI: a model lane's close is a proposal, the user's word
     closes, confirm is the owner's click, a reopen vetoes its closer, the
     thread is archived only when nothing on it is open, closure-report.
  4. the EA's own mail moves never read as the user's archive.
  5. the briefing and the bot carry the proposals to the user.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))

import closure  # noqa: E402
import ledger as ledger_mod  # noqa: E402

LEDGER = ROOT / "preprocessor" / "ledger.py"


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    return conn


def _add(conn, rid, *, type_="task", conv="", status="open", born="2026-10-10T12:00:00Z",
         meta=None, source=None):
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, conversation_id, date_identified, "
        "last_action, metadata, created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, type_, status, f"desc {rid}", conv, born, "", json.dumps(meta or {}), born, born,
         source))


def _row(conn, rid) -> dict:
    return ledger_mod._row_to_dict(conn.execute("SELECT * FROM ledger WHERE id = ?",
                                                (rid,)).fetchone())


def _events(conn, rid) -> list[tuple]:
    return [(e["kind"], e["signal"]) for e in conn.execute(
        "SELECT * FROM ledger_events WHERE entry_id = ? ORDER BY seq", (rid,))]


# ── 1. tier ────────────────────────────────────────────────────────────

def test_a_reply_closes_a_reply_row_and_only_proposes_on_other_work():
    reply = {"id": "response-x", "type": "response_needed"}
    deadline = {"id": "field-trial-ae28-submission", "type": "deadline"}
    assert closure.tier("user_replied_outlook", reply) == closure.DECISIVE
    assert closure.tier("user_replied_outlook", deadline) == closure.PROPOSAL
    assert closure.tier("send_reply_row", {"id": "t", "type": "task"}) == closure.PROPOSAL


def test_closes_on_declares_what_finishes_a_row():
    task_email = {"id": "send-guofeng-doc", "type": "task", "closes_on": "reply"}
    manual_reply = {"id": "r", "type": "response_needed", "closes_on": "manual"}
    assert closure.tier("user_replied_outlook", task_email) == closure.DECISIVE
    assert closure.tier("user_replied_outlook", manual_reply) == closure.PROPOSAL
    sub = {"id": "s", "type": "deadline", "closes_on": "submission"}
    assert closure.tier("inbound_ack:submission-ack", sub) == closure.DECISIVE
    assert closure.tier("inbound_ack:submission-ack", {"id": "s", "type": "deadline"}) \
        == closure.PROPOSAL
    rsvp = {"id": "e", "type": "scheduling", "closes_on": "event"}
    assert closure.tier("meeting_response_sent", rsvp) == closure.DECISIVE


def test_the_users_word_and_links_close_but_never_a_wait_by_a_send():
    task = {"id": "t", "type": "task"}
    wait = {"id": "w", "type": "waiting"}
    assert closure.tier("user_stated", task) == closure.DECISIVE
    assert closure.tier("linked_send", task) == closure.DECISIVE
    assert closure.tier("linked_send", wait) == closure.PROPOSAL
    assert closure.tier("user_stated", wait) == closure.DECISIVE
    assert closure.tier("calendar_event_match", task) == closure.PROPOSAL
    assert closure.tier("llm:morning-briefing", task) == closure.PROPOSAL


def test_an_archive_closes_only_the_detector_row_of_that_mail():
    assert closure.tier("user_archived", {"id": "response-a"}) == closure.DECISIVE
    assert closure.tier("user_archived", {"id": "nsf-report", "type": "task"}) == closure.PROPOSAL


def test_a_plugin_row_keeps_its_own_lifecycle():
    assert closure.tier("llm:plugin", {"id": "ff-1", "source": "plugin:ff-watcher"}) \
        == closure.DECISIVE


# ── 2. settle ──────────────────────────────────────────────────────────

def test_a_plugin_closes_its_row_again_after_a_reopen(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "mr-1", source="plugin:example-notes")
    closure.settle(conn, "mr-1", signal="llm:chat", actor="t", note="done")
    row = conn.execute("SELECT * FROM ledger WHERE id='mr-1'").fetchone()
    meta = closure.note_reopen(conn, row)
    conn.execute("UPDATE ledger SET status='open', metadata=? WHERE id='mr-1'", (json.dumps(meta),))
    assert closure.settle(conn, "mr-1", signal="llm:chat", actor="t", note="done")["action"] == "closed"


def test_a_decisive_close_records_how_and_an_event(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    res = closure.settle(conn, "response-a", signal="user_replied_outlook", actor="t",
                         note="replied")
    assert res["action"] == "closed"
    row = _row(conn, "response-a")
    assert row["status"] == "completed" and row["closed_by"]["signal"] == "user_replied_outlook"
    assert _events(conn, "response-a") == [("close", "user_replied_outlook")]


def test_a_proposal_keeps_the_row_open_and_is_recorded_once(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "ae28", type_="deadline", conv="c1")
    for _ in range(3):
        res = closure.settle(conn, "ae28", signal="user_replied_outlook", actor="t",
                             note="n", why="you replied in its thread")
    assert res["action"] == "proposed"
    row = _row(conn, "ae28")
    assert row["status"] == "open" and row["close_proposal"]["why"] == "you replied in its thread"
    assert _events(conn, "ae28") == [("propose", "user_replied_outlook")]
    assert [r["id"] for r in closure.pending_proposals(conn)] == ["ae28"]


def test_still_open_holds_until_newer_evidence(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "t1", conv="c1")
    closure.settle(conn, "t1", signal="calendar_event_match", actor="t", note="n")
    closure.reject(conn, "t1")
    assert not _row(conn, "t1").get("close_proposal")
    again = closure.settle(conn, "t1", signal="calendar_event_match", actor="t", note="n")
    assert again["action"] == "vetoed"
    newer = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = closure.settle(conn, "t1", signal="calendar_event_match", actor="t", note="n",
                           evidence_ts=newer)
    assert later["action"] == "proposed"


def test_a_model_refusal_holds_two_weeks():
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entry = {"close_vetoes": {"llm": at.strftime("%Y-%m-%dT%H:%M:%SZ")}}
    assert closure.vetoed(entry, "llm:morning-briefing", now=at + timedelta(days=13))
    assert not closure.vetoed(entry, "llm:weekly-audit", now=at + timedelta(days=15))


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_still_open_refuses_the_same_send_under_every_signal_name(tmp_path):
    # One Outlook send reaches the bot as a reply in the thread, the
    # scrubber as the user's Outlook reply and the mail tick as a message in
    # the thread. "Still open" on the first card answers all three.
    conn = _db(tmp_path)
    _add(conn, "kept-slides", type_="deadline", conv="c1")
    sent = _ts(datetime.now(timezone.utc) - timedelta(minutes=30))
    first = closure.propose(conn, "kept-slides", signal="reply_in_thread", actor="bot",
                            note="you sent a reply in its thread from Outlook", evidence_ts=sent)
    assert first["action"] == "proposed"
    closure.reject(conn, "kept-slides")
    for signal in ("user_replied_outlook", "user_wrote_in_thread", "reply_in_thread"):
        res = closure.settle(conn, "kept-slides", signal=signal, actor="t", note="n",
                             evidence_ts=sent)
        assert res["action"] == "vetoed", signal
    assert closure.propose(conn, "kept-slides", signal="reply_in_thread", actor="bot",
                           note="n", evidence_ts=sent)["action"] == "vetoed"
    assert closure.settle(conn, "kept-slides", signal="llm:morning-briefing", actor="t",
                          note="n")["action"] == "vetoed"
    assert not closure.pending_proposals(conn)
    assert [k for k, _ in _events(conn, "kept-slides")] == ["propose", "reject"]
    # The slides going out later is new evidence, and it is heard.
    newer = _ts(datetime.now(timezone.utc) + timedelta(hours=1))
    later = closure.settle(conn, "kept-slides", signal="user_replied_outlook", actor="t",
                           note="n", evidence_ts=newer)
    assert later["action"] == "proposed"


def test_a_kept_open_row_closes_on_its_own_only_on_a_named_send(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _add(conn, "response-b", type_="response_needed", conv="c2")
    closure.propose(conn, "response-a", signal="calendar_event_match", actor="t", note="n")
    closure.reject(conn, "response-a")
    closure.propose(conn, "response-b", signal="calendar_event_match", actor="t", note="n")
    closure.reject(conn, "response-b")
    newer = _ts(datetime.now(timezone.utc) + timedelta(hours=1))
    # A newer reply on a reply row proposes; before "Still open" it closed.
    assert closure.settle(conn, "response-a", signal="user_replied_outlook", actor="t",
                          note="n", evidence_ts=newer)["action"] == "proposed"
    # An archive with no clock is heard, as a proposal.
    assert closure.settle(conn, "response-b", signal="user_archived", actor="t",
                          note="n")["action"] == "proposed"
    assert closure.settle(conn, "response-a", signal="linked_send", actor="t",
                          note="sent", evidence_ts=newer)["action"] == "closed"
    assert closure.settle(conn, "response-b", signal="user_stated", actor="t",
                          note="done")["action"] == "closed"


def test_a_reopen_refuses_all_evidence_older_than_it(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "t1", conv="c1")
    closure.settle(conn, "t1", signal="user_stated", actor="t", note="done")
    row = conn.execute("SELECT * FROM ledger WHERE id='t1'").fetchone()
    meta = closure.note_reopen(conn, row, actor="t")
    assert meta["close_vetoes"].get(closure.KEPT_OPEN)
    conn.execute("UPDATE ledger SET status='open', metadata=? WHERE id='t1'",
                 (json.dumps(meta),))
    older = _ts(datetime.now(timezone.utc) - timedelta(hours=1))
    assert closure.settle(conn, "t1", signal="calendar_event_match", actor="t", note="n",
                          evidence_ts=older)["action"] == "vetoed"


def test_the_sweep_takes_the_asks_before_the_evidence_and_not_the_ones_after(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "response-old", type_="response_needed", conv="c1", born="2026-10-01T00:00:00Z")
    _add(conn, "priority-old", type_="response_needed", conv="c1", born="2026-10-02T00:00:00Z")
    _add(conn, "response-new", type_="response_needed", conv="c1", born="2026-10-09T00:00:00Z")
    _add(conn, "task-on-thread", type_="task", conv="c1", born="2026-10-01T00:00:00Z")
    res = closure.settle(conn, "response-old", signal="user_replied_outlook", actor="t",
                         note="r", evidence_ts="2026-10-05T00:00:00Z")
    assert sorted(res["ids"]) == ["priority-old", "response-old"]
    assert _row(conn, "response-new")["status"] == "open"
    assert _row(conn, "task-on-thread")["status"] == "open"


def test_a_linked_send_or_a_duplicate_closes_its_row_alone(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "send-doc", conv="c1")
    _add(conn, "response-c1", type_="response_needed", conv="c1")
    res = closure.settle(conn, "send-doc", signal="linked_send", actor="t", note="sent")
    assert res["ids"] == ["send-doc"]
    assert _row(conn, "response-c1")["status"] == "open"


def test_a_reopen_vetoes_the_signal_that_closed_it(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    closure.settle(conn, "response-a", signal="user_replied_outlook", actor="t", note="r",
                   evidence_ts="2026-10-11T00:00:00Z")
    row = conn.execute("SELECT * FROM ledger WHERE id='response-a'").fetchone()
    meta = closure.note_reopen(conn, row, actor="t")
    assert "user_replied_outlook" in meta["close_vetoes"] and meta["reopened_at"]
    assert _events(conn, "response-a")[-1] == ("reopen", "user_replied_outlook")


def test_a_legacy_close_note_still_names_its_signal():
    assert closure.infer_signal(
        "Auto-closed by closure_scrubber: user_replied_outlook (sent=x). Previous: y") \
        == "user_replied_outlook"
    assert closure.infer_signal(
        "Auto-closed by closure_scrubber: inbound_ack:submission-ack from=a") \
        == "inbound_ack:submission-ack"
    assert closure.infer_signal("2026-10-01: Auto-closed: email archived from inbox by user.") \
        == "user_archived"
    assert closure.infer_signal("done by hand") == ""


def test_the_report_counts_false_closes_and_proposal_outcomes(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _add(conn, "t1", conv="c2")
    _add(conn, "t2", conv="c3")
    closure.settle(conn, "response-a", signal="user_replied_outlook", actor="t", note="r")
    closure.note_reopen(conn, conn.execute("SELECT * FROM ledger WHERE id='response-a'").fetchone())
    closure.settle(conn, "t1", signal="calendar_event_match", actor="t", note="n")
    closure.confirm(conn, "t1")
    closure.settle(conn, "t2", signal="calendar_event_match", actor="t", note="n")
    closure.reject(conn, "t2")
    rep = closure.report(conn)
    assert rep["false_closes"] == 1
    assert rep["signals"]["user_replied_outlook"]["reopened"] == 1
    cal = rep["signals"]["calendar_event_match"]
    assert (cal["proposed"], cal["confirmed"], cal["rejected"]) == (2, 1, 1)


# ── 3. the ledger CLI ──────────────────────────────────────────────────

def _cli(ea: Path, *args, ok: bool = True) -> dict:
    env = dict(os.environ, EA_DIR=str(ea))
    env.pop("EA_RUN_ID", None)
    r = subprocess.run([sys.executable, str(LEDGER), *args], env=env,
                       capture_output=True, text=True, timeout=60)
    if ok:
        assert r.returncode == 0, r.stderr
    return json.loads(r.stdout or "{}")


def _ea(tmp_path: Path) -> Path:
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    return ea


def test_a_model_lanes_close_is_a_proposal_and_says_so(tmp_path):
    ea = _ea(tmp_path)
    _cli(ea, "add", "--id", "t1", "--type", "task", "--description", "Submit AE28")
    out = _cli(ea, "close", "--id", "t1", "--note", "looks submitted", "--skip-archive")
    assert out["action"] == "proposed" and out["entry"]["status"] == "open"
    assert "--user-said" in out["message"]
    assert out["entry"]["close_proposal"]["signal"] == "llm:cli"
    listed = _cli(ea, "proposals")
    assert [e["id"] for e in listed["entries"]] == ["t1"]


def test_the_users_word_closes_and_is_kept_as_evidence(tmp_path):
    ea = _ea(tmp_path)
    _cli(ea, "add", "--id", "t1", "--type", "task", "--description", "x")
    out = _cli(ea, "close", "--id", "t1", "--user-said", "AE28 is in", "--skip-archive")
    assert out["action"] == "closed" and out["entry"]["closed_by"]["signal"] == "user_stated"


def test_a_duplicate_folds_into_a_row_that_exists(tmp_path):
    ea = _ea(tmp_path)
    for rid in ("a", "b"):
        _cli(ea, "add", "--id", rid, "--type", "task", "--description", rid)
    assert "error" in _cli(ea, "close", "--id", "a", "--duplicate-of", "nope", ok=False)
    out = _cli(ea, "close", "--id", "a", "--duplicate-of", "b", "--skip-archive")
    assert out["action"] == "closed" and _cli(ea, "get", "--id", "b")["status"] == "open"


def test_a_resident_cannot_name_an_owner_signal_or_confirm(tmp_path):
    ea = _ea(tmp_path)
    _cli(ea, "add", "--id", "t1", "--type", "task", "--description", "x")
    out = _cli(ea, "close", "--id", "t1", "--signal", "linked_send", "--skip-archive")
    assert out["action"] == "proposed"
    assert "error" in _cli(ea, "confirm", "--id", "t1", ok=False)
    kept = _cli(ea, "reject", "--id", "t1")
    assert kept["action"] == "kept_open" and _cli(ea, "proposals")["count"] == 0


def test_closes_on_is_checked_and_stored(tmp_path):
    ea = _ea(tmp_path)
    out = _cli(ea, "add", "--id", "t1", "--type", "task", "--description", "x",
               "--closes-on", "reply")
    assert out["entry"]["closes_on"] == "reply"
    bad = subprocess.run([sys.executable, str(LEDGER), "add", "--id", "t2", "--type", "task",
                          "--description", "x", "--closes-on", "whenever"],
                         env=dict(os.environ, EA_DIR=str(ea)), capture_output=True, text=True)
    assert bad.returncode != 0


def test_a_reopen_through_update_records_the_event_and_the_veto(tmp_path):
    ea = _ea(tmp_path)
    _cli(ea, "add", "--id", "t1", "--type", "task", "--description", "x")
    _cli(ea, "close", "--id", "t1", "--duplicate-of", "t1", ok=False)
    _cli(ea, "add", "--id", "t2", "--type", "task", "--description", "y")
    _cli(ea, "close", "--id", "t1", "--duplicate-of", "t2", "--skip-archive")
    out = _cli(ea, "update", "--id", "t1", "--status", "open")
    assert "duplicate_of" in out["entry"]["close_vetoes"]
    rep = _cli(ea, "closure-report", "--days", "1")
    assert rep["false_closes"] == 1


def test_the_thread_is_archived_only_when_nothing_on_it_is_open(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _add(conn, "task-b", conv="c1")
    conn.execute("UPDATE ledger SET source_message_id = 'm-a' WHERE id = 'response-a'")
    calls = []
    monkeypatch.setattr(ledger_mod, "_archive_email",
                        lambda mid: calls.append(("msg", mid)) or {"archived": True})
    monkeypatch.setattr(ledger_mod, "_archive_conversation",
                        lambda cid: calls.append(("conv", cid)) or {"archived": True})
    row = conn.execute("SELECT * FROM ledger WHERE id='response-a'").fetchone()
    closure.settle(conn, "response-a", signal="user_stated", actor="t", note="done")
    out = ledger_mod._archive_after_close(conn, row, ["response-a"], skip=False)
    assert calls == [("msg", "m-a")] and out["conversation_kept"] == ["task-b"]
    calls.clear()
    closure.settle(conn, "task-b", signal="user_stated", actor="t", note="done")
    row_b = conn.execute("SELECT * FROM ledger WHERE id='task-b'").fetchone()
    ledger_mod._archive_after_close(conn, row_b, ["task-b"], skip=False)
    assert ("conv", "c1") in calls


# ── 4. the EA's own moves ──────────────────────────────────────────────

def test_an_ea_move_is_found_through_the_new_id_and_a_wake_cancels_it(tmp_path, monkeypatch):
    import ea_moves
    path = tmp_path / "moves.jsonl"
    monkeypatch.setattr(ea_moves, "journal_path", lambda: path)
    ea_moves.record("m1", "m1-archived", "archive", "c1")
    hit = ea_moves.moved_out_by_ea(["m1-archived"], since="2000-01-01")
    assert hit and hit["from_id"] == "m1"
    assert ea_moves.moved_out_by_ea(["m1"], since="2999-01-01") is None
    ea_moves.record("m1-archived", "m1-back", "inbox", "c1")
    assert ea_moves.moved_out_by_ea(["m1"], since="2000-01-01") is None
    assert ea_moves.moved_out_by_ea(["other"]) is None


def test_ledger_sync_defers_on_an_ea_move_until_the_mail_is_seen_back(tmp_path, monkeypatch):
    import ea_moves
    import ledger_sync
    path = tmp_path / "moves.jsonl"
    monkeypatch.setattr(ea_moves, "journal_path", lambda: path)
    ea_moves.record("m1", "m1-arch", "archive", "c1")
    assert ledger_sync._moved_by_ea("m1", "", "2000-01-01T00:00:00Z", "{}")
    stamp = json.loads(path.read_text().splitlines()[0])["ts"]
    assert not ledger_sync._moved_by_ea("m1", "", "2000-01-01T00:00:00Z",
                                        json.dumps({"ea_move_cleared": stamp}))


def test_graph_client_records_every_move(monkeypatch):
    import graph_client
    seen = []

    class Resp:
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"Id": "new-id", "ConversationId": "conv-1"}
    monkeypatch.setattr(graph_client, "_api_base", lambda: "https://x")
    monkeypatch.setattr(graph_client, "_headers", lambda: {})
    monkeypatch.setattr(graph_client.requests, "post", lambda *a, **k: Resp())
    import ea_moves
    monkeypatch.setattr(ea_moves, "record", lambda *a: seen.append(a))
    graph_client.graph_post("/me/messages/old-id/move", {"destinationId": "archive"})
    graph_client.graph_post("/me/messages/old-id/reply", {"comment": "x"})
    assert seen == [("old-id", "new-id", "archive", "conv-1")]


# ── 5. the briefing and the bot ────────────────────────────────────────

def test_the_briefing_lists_proposals_and_keeps_them_off_the_queue():
    import preprocess
    obs = [{"id": "a", "description": "Submit AE28",
            "close_proposal": {"why": "you replied in its thread", "first_at": "t"}},
           {"id": "b", "description": "Reply to Pat"}]
    assert preprocess._close_proposals(obs) == [
        {"id": "a", "description": "Submit AE28", "why": "you replied in its thread",
         "since": "t"}]


def _bot_ns(calls: dict):
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_CLOSE_BATCHES = _private_state(")
    end = src.index('@app.action("dismiss_bulk_send")')
    from preprocessor import chat as _chat

    class App:
        def action(self, _name):
            return lambda fn: fn
    store = {}
    ns = {"json": json, "time": __import__("time"), "datetime": datetime, "timezone": timezone,
          "uuid": __import__("uuid"), "_chat": _chat, "app": App(),
          "SCRIPT_DIR": ROOT,
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(d=d)),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
          "_verify_button_user": lambda ev: True,
          "_resolve_dm_channel": lambda: "dm",
          "_post_card": lambda ch, card: calls.setdefault("cards", []).append(card),
          "_replace_buttons_with_status": lambda ch, ts, text: calls.setdefault("status", []).append(text)}
    exec(compile(src[start:end], "bot[close-proposals]", "exec"), ns)
    ns["_load_close_batches"] = lambda: store.get("d", {})
    return ns


def test_the_bot_cards_settled_proposals_once_and_acts_on_the_clicks():
    calls: dict = {}
    ns = _bot_ns(calls)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending = [{"id": "ae28", "description": "Submit AE28", "proposal": {"why": "w", "at": old}},
               {"id": "t2", "description": "Send doc", "proposal": {"why": "v", "at": old}},
               {"id": "seen", "description": "x", "proposal": {"why": "", "at": old, "carded": old}}]
    cli = []

    def ledger_cli(*argv):
        cli.append(argv)
        if argv[0] == "proposals":
            return {"entries": pending}
        if argv[0] == "confirm":
            return {"action": "closed"}
        return {"action": "kept_open"}
    ns["_ledger_cli"] = ledger_cli
    assert ns["_post_close_proposals"](force=True) == 2
    card = calls["cards"][0]
    assert card.kind == "close" and "2 items look done" in card.title
    assert [a.id for a in card.actions] == ["close_all_proposed", "review_proposed"]
    assert ("proposals", "--mark-carded", "ae28", "t2") in cli
    batch_id = card.actions[0].value
    ev = types.SimpleNamespace(value=batch_id, card=types.SimpleNamespace(channel="dm", id="1"))
    ns["handle_close_all_proposed"](ev)
    assert [a for a in cli if a[0] == "confirm"] == [("confirm", "--id", "ae28"),
                                                    ("confirm", "--id", "t2")]
    assert calls["status"][-1].startswith(":white_check_mark: Closed 2")
    ev1 = types.SimpleNamespace(value="ae28", card=types.SimpleNamespace(channel="dm", id="2"))
    ns["handle_keep_open_proposed"](ev1)
    assert cli[-1] == ("reject", "--id", "ae28")


def test_a_fresh_proposal_waits_for_the_batch_to_settle():
    calls: dict = {}
    ns = _bot_ns(calls)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ns["_ledger_cli"] = lambda *a: {"entries": [{"id": "x", "description": "d",
                                                 "proposal": {"at": now}}]}
    ns["_LAST_PROPOSAL_CHECK"][0] = 0.0
    assert ns["_post_close_proposals"]() == 0 and "cards" not in calls


# ── 6. a date that ends a row ─────────────────────────────────────────

def _brief(*, ask="", needs_reply=False, wrote=""):
    return {"thread_brief": {"needs_reply": needs_reply, "ask": "", "last_from_user": wrote,
                             "messages": [{"from": "staff-list@example.edu", "ask": ask,
                                           "state": "todo", "deadline": "2026-10-30"}]}}


def test_a_dated_row_whose_mail_asks_nothing_lapses_and_owed_work_never_does():
    """A vote, a contest or a talk sent to a whole list: the record names
    no ask, so its date is a chance that closes, not a debt. A row with an
    ask, a row with no brief (the EA's own) and a row declared to close
    another way never lapse; a declared lapse always does."""
    vote = {"id": "deadline-vote", "deadline": "2026-10-30", **_brief()}
    assert ledger_mod.lapses(vote) and ledger_mod.asks_nothing(vote)
    assert not ledger_mod.lapses({**vote, **_brief(ask="Upload the letter.")})
    assert not ledger_mod.lapses({**vote, **_brief(needs_reply=True)})
    assert not ledger_mod.lapses({**vote, "notes": "Ask: Send the form."})
    assert not ledger_mod.lapses({"id": "deadline-x", "deadline": "2026-10-30"})
    assert not ledger_mod.lapses({"id": "my-own-row", "deadline": "2026-10-30", **_brief()})
    assert not ledger_mod.lapses({**vote, "closes_on": "manual"})
    assert ledger_mod.lapses({"id": "my-own-row", "closes_on": "lapse"})
    # intake declares it from the first record; a later ask in the thread keeps the row
    declared = {"id": "deadline-vote", "deadline": "2026-10-30", "closes_on": "lapse"}
    assert ledger_mod.lapses(declared) and ledger_mod.lapses({**declared, **_brief()})
    assert not ledger_mod.lapses({**declared, **_brief(ask="Confirm your nomination.")})
    from datetime import date
    assert ledger_mod.date_spent(vote, date(2026, 11, 3)) == "nothing was asked of you"
    assert ledger_mod.date_spent(vote, date(2026, 10, 30)) == ""
    assert ledger_mod.record_asks_nothing({"state": "todo", "ask": "", "needs_reply": False})
    assert not ledger_mod.record_asks_nothing({"state": "todo", "ask": "Vote.", "needs_reply": False})
    assert not ledger_mod.record_asks_nothing({})


def test_the_lapsed_signal_closes_only_a_row_that_lapses_and_he_never_wrote_in():
    vote = {"id": "deadline-vote", "deadline": "2026-10-30", **_brief()}
    assert closure.tier("lapsed", vote) == closure.DECISIVE
    assert closure.tier("lapsed", {**vote, **_brief(wrote="2026-10-29T15:00:00Z")}) == closure.PROPOSAL
    declared = {"id": "my-own-row", "closes_on": "lapse", **_brief(wrote="2026-10-29T15:00:00Z")}
    assert closure.tier("lapsed", declared) == closure.DECISIVE
    intake = {"id": "deadline-vote", "closes_on": "lapse", **_brief(wrote="2026-10-29T15:00:00Z")}
    assert closure.tier("lapsed", intake) == closure.PROPOSAL
    assert closure.tier("lapsed", {**vote, **_brief(ask="Upload the letter.")}) == closure.PROPOSAL


def test_the_sweep_closes_lapsed_rows_after_their_date_and_leaves_the_rest(tmp_path):
    from datetime import date
    conn = _db(tmp_path)
    _add(conn, "deadline-vote", meta=_brief())
    _add(conn, "deadline-letter", meta=_brief(ask="Upload the letter."))
    _add(conn, "deadline-talk", meta=_brief(wrote="2026-10-29T15:00:00Z"))
    _add(conn, "deadline-later", meta=_brief())
    for rid, due in (("deadline-vote", "2026-10-30"), ("deadline-letter", "2026-10-30"),
                     ("deadline-talk", "2026-10-30"), ("deadline-later", "2026-11-20")):
        conn.execute("UPDATE ledger SET deadline = ? WHERE id = ?", (due, rid))
    conn.commit()
    conn.close()
    out = ledger_mod.close_lapsed(db_path=tmp_path / "ledger.db", today=date(2026, 11, 3))
    assert {(r["id"], r["action"]) for r in out} == {("deadline-vote", "closed"),
                                                      ("deadline-talk", "proposed")}
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    vote = _row(conn, "deadline-vote")
    assert vote["status"] == "completed" and "Oct 30 date passed" in vote["last_action"]
    assert vote["closed_by"]["signal"] == "lapsed"
    assert _row(conn, "deadline-talk")["status"] == "open"
    assert _row(conn, "deadline-talk")["close_proposal"]["signal"] == "lapsed"
    assert _row(conn, "deadline-letter")["status"] == "open"
    assert _row(conn, "deadline-later")["status"] == "open"
    assert ("close", "lapsed") in _events(conn, "deadline-vote")
