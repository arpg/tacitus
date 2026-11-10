"""The reply queue's selector and its ladder (ledger.reply_backlog).

A thread that waits for the user's reply gets a draft at one day, a name
on the briefing's queue line at three and a place in today's ranking at
five. The age runs from the other side's last message, in the user's
zone. What a run did about a message is recorded on the row and stands
until a newer message comes. A snoozed row is open work again on its
day.
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import ledger  # noqa: E402
import preprocess  # noqa: E402

TODAY = date(2026, 10, 12)
ZONE = ZoneInfo("America/Denver")


def _at(days_ago: int, hour: int = 18) -> str:
    """An instant `days_ago` local days before TODAY, in UTC."""
    local = datetime(TODAY.year, TODAY.month, TODAY.day, hour, tzinfo=ZONE) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(rid: str, days_ago: int, **over) -> dict:
    """An open row whose thread ends on the other side's message."""
    brief = {"needs_reply": True, "state": "needs-reply", "subject": f"About {rid}",
             "ask": "Confirm the date.", "scheduling": "none", "user_replied_after_latest": False,
             "last_inbound": _at(days_ago),
             "messages": [{"id": f"m-{rid}-0", "from": "owner@example.edu", "own": True,
                           "received": _at(days_ago + 2), "gist": "The user asked for the dates."},
                          {"id": f"m-{rid}-1", "from": f"{rid}@example.edu", "own": False,
                           "received": _at(days_ago), "gist": f"{rid} asks to confirm the date."}]}
    brief.update(over.pop("brief", {}))
    row = {"id": f"row-{rid}", "type": "response_needed", "status": "open",
           "description": f"Reply to {rid}", "from_email": f"{rid}@example.edu",
           "from_name": rid.title(), "source_message_id": f"m-{rid}-1",
           "conversation_id": f"c-{rid}", "date_identified": _at(days_ago + 2),
           "thread_brief": brief}
    row.update(over)
    return row


def _backlog(rows, **kw):
    kw.setdefault("draft_convs", ())
    return ledger.reply_backlog(rows, TODAY, zone=ZONE, **kw)


def test_the_age_runs_from_the_other_sides_last_message_in_the_users_zone():
    row = _row("pat", 4)
    assert ledger.reply_age(row, TODAY, ZONE) == 4
    # 03:00 UTC is the evening before in Denver: one more local day.
    late = _row("lee", 0, brief={"messages": [{"id": "m-late", "from": "lee@example.edu", "own": False,
                                               "received": "2026-10-09T03:00:00Z", "gist": "A question."}]})
    assert ledger.reply_age(late, TODAY, ZONE) == 4
    assert ledger.reply_target(late) == {"message_id": "m-late", "from": "lee@example.edu",
                                         "since": "2026-10-09T03:00:00Z"}


def test_the_ladder_is_one_three_and_five_days():
    assert [ledger.reply_rung(n) for n in (None, 0, 1, 2, 3, 4, 5, 30)] == \
        ["", "", "draft", "draft", "name", "name", "ask", "ask"]
    assert (ledger.REPLY_DRAFT_DAYS, ledger.REPLY_NAME_DAYS, ledger.REPLY_ASK_DAYS) == (1, 3, 5)


def test_the_queue_is_one_entry_per_thread_oldest_first():
    task = _row("pat", 6, id="row-pat-task", type="task")
    rows = [_row("lee", 2), task, _row("pat", 6), _row("sam", 0)]
    out = _backlog(rows)
    assert [e["id"] for e in out] == ["row-pat", "row-lee"]      # the row a reply closes stands for its thread
    assert [(e["age_days"], e["rung"], e["total"]) for e in out] == [(6, "ask", 2), (2, "draft", 2)]
    first = out[0]
    assert first["message_id"] == "m-pat-1" and first["from"] == "pat@example.edu"
    assert first["conversation_id"] == "c-pat" and first["from_name"] == "Pat"
    assert first["ask"] == "Confirm the date." and first["thread"][-1]["gist"].startswith("pat asks")
    assert first["has_draft"] is False and first["queued"] == {} and first["kept"] is False


def test_the_cap_cuts_the_newest_and_the_total_counts_them_all():
    out = _backlog([_row(f"p{n}", n) for n in range(1, 8)], cap=3)
    assert [e["age_days"] for e in out] == [7, 6, 5] and {e["total"] for e in out} == {7}


def test_a_row_that_owes_no_reply_now_is_out():
    out = _backlog([
        _row("answered", 4, brief={"user_replied_after_latest": True}),
        _row("fyi", 4, brief={"needs_reply": False}),
        _row("parked", 4, follow_up_after="2026-10-20"),
        _row("snoozed", 4, status="snoozed"),
        _row("proposed", 4, close_proposal={"why": "looks done"}),
        _row("theirs", 4, type="waiting"),
        _row("asked", 4, brief={"waiting_on_reply": {"since": _at(1), "asked": "Which room?"}}),
        _row("plugin", 4, source="plugin:reviews"),
    ])
    assert out == []


def test_a_row_with_no_thread_record_owes_a_reply_by_its_type():
    bare = {"id": "row-bare", "type": "response_needed", "status": "open", "description": "Reply to Jo",
            "from_email": "jo@example.edu", "source_message_id": "m-jo", "conversation_id": "c-jo",
            "date_identified": _at(3)}
    (e,) = _backlog([bare, dict(bare, id="row-task", type="task", conversation_id="c-other")])
    assert (e["id"], e["message_id"], e["from"], e["age_days"]) == ("row-bare", "m-jo", "jo@example.edu", 3)


def test_a_draft_in_the_folder_marks_the_thread():
    rows = [_row("pat", 3), _row("lee", 2)]
    assert [e["has_draft"] for e in _backlog(rows, draft_convs={"c-lee", ""})] == [False, True]
    # The folder could not be read: nobody may conclude there is no draft.
    assert [e["has_draft"] for e in _backlog(rows, draft_convs=None)] == [None, None]


def test_mail_that_stays_on_the_machine_is_marked_kept():
    rows = [_row("local", 3, brief={"keep_level": "local"}), _row("facts", 3, brief={"keep_level": "facts"}),
            _row("summary", 3, brief={"keep_level": "summary"}), _row("flag", 3, kept_from_frontier=True)]
    assert {e["id"]: e["kept"] for e in _backlog(rows)} == {
        "row-local": True, "row-facts": True, "row-summary": False, "row-flag": True}


def test_a_recorded_outcome_stands_until_a_newer_message():
    row = _row("pat", 3)
    rec = {"inbound": ledger.reply_target(row)["since"], "at": _at(1), "outcome": "decision",
           "note": "One year or three?"}
    (e,) = _backlog([dict(row, reply_queue=rec)])
    assert e["queued"] == rec
    newer = _row("pat", 1, reply_queue=rec)
    (e,) = _backlog([newer])
    assert e["queued"] == {} and e["age_days"] == 1


# ── the record on the row ─────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    monkeypatch.setattr(ledger, "DB_PATH", path)
    return path


def _insert(path, rid, *, status="open", snooze_until=None, follow_up_after=None, last_action="",
            metadata=None):
    conn = sqlite3.connect(str(path))
    now = ledger._now()
    conn.execute("INSERT INTO ledger (id, type, status, description, source_message_id, conversation_id, "
                 "from_email, snooze_until, follow_up_after, date_identified, last_action, notes, metadata, "
                 "created_at, updated_at) VALUES (?, 'response_needed', ?, 'Reply to Pat', 'm-1', 'c-1', "
                 "'pat@example.edu', ?, ?, ?, ?, '', ?, ?, ?)",
                 (rid, status, snooze_until, follow_up_after, "2026-10-08T15:00:00Z", last_action,
                  json.dumps(metadata or {}), "2026-10-08T15:00:00Z", "2026-10-08T15:00:00Z"))
    conn.commit()
    conn.close()


def test_note_reply_records_the_message_it_was_about(db):
    brief = _row("pat", 3)["thread_brief"]
    _insert(db, "row-1", metadata={"thread_brief": brief})
    rec = ledger.note_reply("row-1", "drafted", draft_id="draft-9", note="Yes,  the date\nworks.")
    assert rec["inbound"] == brief["messages"][-1]["received"] and rec["outcome"] == "drafted"
    assert rec["draft_id"] == "draft-9" and rec["note"] == "Yes, the date works."
    row = ledger.get_entry("row-1")
    assert row["reply_queue"] == rec
    assert row["updated_at"] == "2026-10-08T15:00:00Z"       # a record is not activity on the item
    assert row["thread_brief"] == brief                        # the rest of the metadata stands
    (e,) = ledger.reply_backlog([row], TODAY, zone=ZONE)
    assert e["queued"] == rec


def test_note_reply_refuses_an_unknown_row_and_an_unknown_outcome(db):
    assert ledger.note_reply("row-none", "left") is None
    _insert(db, "row-1")
    with pytest.raises(ValueError):
        ledger.note_reply("row-1", "sent")


def test_the_note_reply_verb(db, monkeypatch, capsys):
    _insert(db, "row-1")
    monkeypatch.setattr(sys, "argv", ["ledger.py", "note-reply", "--id", "row-1", "--outcome", "decision",
                                      "--note", "One year or three?"])
    assert ledger.main() == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["reply_queue"]["outcome"] == "decision"
    assert out["reply_queue"]["inbound"] == "2026-10-08T15:00:00Z"     # no thread record: the row's own date
    assert ledger.get_entry("row-1")["reply_queue"]["note"] == "One year or three?"
    monkeypatch.setattr(sys, "argv", ["ledger.py", "note-reply", "--id", "row-none", "--outcome", "left"])
    assert ledger.main() == 1


def test_the_reply_queue_verb_lists_the_live_queue_from_an_age(db, monkeypatch, capsys):
    """The bot's Replies card reads the queue with `--min-age 3`; `owed`
    counts every thread that waits, whatever its age."""
    now = datetime.now(timezone.utc)

    def brief(days: float) -> dict:
        at = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"thread_brief": {"needs_reply": True, "state": "needs-reply", "last_inbound": at,
                                 "messages": [{"id": "m-in", "from": "pat@example.edu", "own": False,
                                               "received": at, "gist": "A question."}]}}
    ages = (("row-old", 9.5), ("row-mid", 4.5), ("row-new", 1.5))
    for rid, days in ages:
        _insert(db, rid, metadata=brief(days))
    conn = sqlite3.connect(str(db))
    for rid, _days in ages:
        conn.execute("UPDATE ledger SET conversation_id = ? WHERE id = ?", (f"c-{rid}", rid))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["ledger.py", "reply-queue", "--min-age", "3"])
    assert ledger.main() == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["owed"] == 3 and out["count"] == 2
    assert [e["id"] for e in out["entries"]] == ["row-old", "row-mid"]
    assert all(e["age_days"] >= 3 and e["queued"] == {} for e in out["entries"])
    monkeypatch.setattr(sys, "argv", ["ledger.py", "reply-queue"])
    assert ledger.main() == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [e["id"] for e in out["entries"]] == ["row-old", "row-mid", "row-new"]


# ── a snooze that ends ────────────────────────────────────────────────

def test_a_snoozed_row_is_open_again_on_its_day(db):
    today = datetime.now().astimezone().date()
    past, future = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=5)).isoformat()
    _insert(db, "due", status="snoozed", snooze_until=past, last_action="2026-10-01: snoozed a week")
    _insert(db, "today", status="snoozed", snooze_until=today.isoformat())
    _insert(db, "by-follow-up", status="snoozed", follow_up_after=past)
    _insert(db, "later", status="snoozed", snooze_until=future)
    _insert(db, "later-of-two", status="snoozed", snooze_until=past, follow_up_after=future)
    _insert(db, "no-date", status="snoozed")
    _insert(db, "parked", status="follow_up", follow_up_after=past)
    woke = ledger.resurrect_due_snoozed()
    assert sorted(r["id"] for r in woke) == ["by-follow-up", "due", "today"]
    assert {r["status"] for r in woke} == {"open"}
    row = ledger.get_entry("due")
    assert row["status"] == "open"
    assert row["last_action"] == f"{today.isoformat()}: snooze ended, open again. 2026-10-01: snoozed a week"
    assert {rid: ledger.get_entry(rid)["status"] for rid in ("later", "later-of-two", "no-date", "parked")} == {
        "later": "snoozed", "later-of-two": "snoozed", "no-date": "snoozed", "parked": "follow_up"}
    assert ledger.resurrect_due_snoozed() == []


# ── the ranking ───────────────────────────────────────────────────────

def _live_row(rid: str, days_ago: int, **over) -> dict:
    """A row whose last inbound message is `days_ago` days before now."""
    since = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = _row(rid, 0, **over)
    row["thread_brief"]["messages"][-1]["received"] = since
    row["thread_brief"]["last_inbound"] = since
    return row


def test_a_reply_owed_for_five_days_ranks_with_todays_work(monkeypatch):
    monkeypatch.setattr(preprocess, "_user_zone", lambda: timezone.utc)
    # The row is three weeks old and was touched yesterday: the age that
    # counts is the unanswered message's.
    top, rest = preprocess.build_today_top([], [], [], [], [
        _live_row("old", 6, days_stale=0), _live_row("new", 2, days_stale=0),
        _live_row("answered", 6, days_stale=0, brief={"user_replied_after_latest": True})], [])
    assert [t["ref"] for t in top] == ["row-old"]
    assert top[0]["reason"] == "reply owed for 6d" and top[0]["score"] >= 30
    assert {r["title"] for r in rest} == {"Reply to new", "Reply to answered"}
