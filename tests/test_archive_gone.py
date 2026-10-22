"""A close whose message id has gone stale (the mail already moved, so
Graph answers 404) is not an archive failure: archive-message answers
{"gone": true}, and when the conversation archive then finds nothing left
in the inbox, the close reports the mail archived by conversation and
marks the row."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import ledger  # noqa: E402


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ledger (id TEXT, source_message_id TEXT, conversation_id TEXT, status TEXT, "
               "source TEXT, email_archived INTEGER)")
    db.execute("INSERT INTO ledger VALUES ('r1', 'stale-id', 'conv-1', 'completed', NULL, 0)")
    return db


def _row(db):
    return db.execute("SELECT * FROM ledger WHERE id = 'r1'").fetchone()


def test_a_stale_id_with_an_empty_inbox_thread_counts_as_archived(monkeypatch):
    calls = []

    def cli(*args):
        calls.append(args[0])
        if args[0] == "archive-message":
            return {"archived": False, "gone": True, "message": "no message with this id"}
        return {"conversation_id": "conv-1", "archived_count": 0, "archived_ids": [], "errors": []}
    monkeypatch.setattr(ledger, "_graph_cli", cli)
    db = _db()
    out = ledger._archive_after_close(db, _row(db), ["r1"], skip=False)
    assert calls == ["archive-message", "archive-conversation"]
    assert out["email_archive"]["archived"] and out["email_archive"]["by"] == "conversation"
    assert _row(db)["email_archived"] == 1


def test_a_real_archive_error_still_reads_as_one(monkeypatch):
    def cli(*args):
        if args[0] == "archive-message":
            return {"error": "no JSON from graph_actions (rc=1): Traceback"}
        return {"conversation_id": "conv-1", "archived_count": 0, "archived_ids": [], "errors": []}
    monkeypatch.setattr(ledger, "_graph_cli", cli)
    db = _db()
    out = ledger._archive_after_close(db, _row(db), ["r1"], skip=False)
    assert not out["email_archive"]["archived"] and "Traceback" in out["email_archive"]["error"]
    assert _row(db)["email_archived"] == 0


def test_a_stale_id_with_a_sibling_still_open_stays_unarchived_but_says_why(monkeypatch):
    monkeypatch.setattr(ledger, "_graph_cli",
                        lambda *a: {"archived": False, "gone": True, "message": "no message with this id"})
    db = _db()
    db.execute("INSERT INTO ledger VALUES ('r2', 'other', 'conv-1', 'open', NULL, 0)")
    out = ledger._archive_after_close(db, _row(db), ["r1"], skip=False)
    assert out["email_archive"] == {"archived": False, "gone": True, "note": "no message with this id"}
    assert out["conversation_kept"] == ["r2"] and _row(db)["email_archived"] == 0


def test_archive_message_answers_gone_on_a_404():
    src = (ROOT / "preprocessor" / "graph_actions.py").read_text()
    body = src.split("def cmd_archive_message")[1].split("\ndef ")[0]
    assert '"gone": True' in body and '"404" in str(e)' in body
    assert json.loads('{"archived": false, "gone": true}')["gone"]
