"""OVERDUE names a date the user still owes something by. A meeting's date
passes when the meeting is held, and a reminder the user wrote back on
after its date is history: neither reads OVERDUE in the ranked queue or
the morning post, and a meeting never mints a deadline row (the 2026-10-30
morning post put two such rows under the red circle)."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# preprocess.py makes STATE_DIR at import: a sandbox, never the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-date-spent-")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ledger  # noqa: E402
import remote_briefing as rb  # noqa: E402
from preprocess import build_today_top, mail_row_kind  # noqa: E402

TODAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)   # Fri 04:00 MT


def _row(oid: str, **kw) -> dict:
    return {"id": oid, "type": "deadline", "status": "open", "description": oid,
            "days_stale": 0, **kw}


def test_a_meeting_date_or_a_date_the_user_wrote_after_is_spent():
    held = _row("robo", deadline="2026-10-28",
                thread_brief={"scheduling": "confirmation", "state": "fyi"})
    assert ledger.is_meeting_date(held)
    assert ledger.date_spent(held, TODAY) == "the meeting was Oct 28"
    # the same row before the meeting is a plain upcoming date
    assert ledger.date_spent(held, date(2026, 10, 27)) == ""
    assert ledger.is_meeting_date(_row("ev", closes_on="event"))
    assert ledger.is_meeting_date(_row("sched", type="scheduling"))

    goals = _row("goals", deadline="2026-09-30",
                 thread_brief={"scheduling": "none", "last_from_user": "2026-10-27T20:02:07Z"})
    assert ledger.acted_since(goals, date(2026, 9, 30)) == "2026-10-27"
    assert ledger.acted_since(goals, date(2026, 10, 28)) == ""
    assert ledger.date_spent(goals, TODAY) == "you wrote Oct 27"

    # a passed date the user has not written on since is overdue work
    late = _row("letter", deadline="2026-10-22",
                thread_brief={"scheduling": "none", "last_from_user": "2026-10-20T15:00:00Z"})
    assert ledger.date_spent(late, TODAY) == ""
    assert ledger.date_spent(_row("bare", deadline="2026-10-22"), TODAY) == ""
    assert ledger.date_spent(_row("none"), TODAY) == ""
    assert ledger.date_spent(_row("bad", deadline="soon"), TODAY) == ""


def test_the_ranked_queue_gives_a_spent_date_no_overdue_credit():
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=2)).date().isoformat()
    held = _row("robo", deadline=past, thread_brief={"scheduling": "confirmation"})
    wrote = _row("goals", deadline=past,
                 thread_brief={"scheduling": "none",
                               "last_from_user": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    late = _row("letter", deadline=past)
    top, _rest = build_today_top([], [], [], [], [held, wrote, late], [], floor=0)
    by = {t["ref"]: t for t in top}
    assert "OVERDUE" in by["letter"]["reason"]
    for ref in ("robo", "goals"):
        assert "OVERDUE" not in by[ref]["reason"], by[ref]
        assert "date passed" in by[ref]["reason"]
        assert by[ref]["score"] < by["letter"]["score"]
    assert "the meeting was" in by["robo"]["reason"]
    assert "you wrote" in by["goals"]["reason"]


def test_a_meeting_record_never_mints_a_deadline_row():
    base = {"subject": "Exec committee meeting Wed 10-11am", "body_preview": "A reminder."}
    for kind in ("confirmation", "request", "proposes-times"):
        rec = {"state": "fyi", "needs_reply": False, "deadline": "2026-10-28", "scheduling": kind}
        k = mail_row_kind(dict(base, remote_record=rec))
        assert (k["prefix"], k["type"], k["deadline"]) == ("response-", "response_needed", ""), kind
    # an RSVP owed by a date is a task, not a deadline row with the meeting's date
    rec = {"state": "todo", "needs_reply": False, "deadline": "2026-10-28", "scheduling": "request",
           "ask": "Say whether you can attend."}
    k = mail_row_kind(dict(base, remote_record=rec))
    assert (k["prefix"], k["type"], k["deadline"]) == ("task-", "task", "")
    # a dated reminder about work the user owes still tracks the date
    rec = {"state": "fyi", "needs_reply": False, "deadline": "2026-11-04", "scheduling": "none"}
    k = mail_row_kind(dict(base, remote_record=rec))
    assert (k["prefix"], k["type"], k["deadline"]) == ("deadline-", "deadline", "2026-11-04")


def _briefing() -> dict:
    return {
        "today_top": [],
        "new_needs_response": [
            {"message_id": "m-sync", "conversation_id": "c-sync", "subject": "Project sync Tue 3pm",
             "from": "lee@example.edu", "from_name": "Lee", "body_preview": "See you Tuesday.",
             "remote_record": {"ask": "", "deadline": "2026-10-27", "scheduling": "confirmation",
                               "people": []}},
        ],
        "open_obligations": [
            _row("deadline-robo", description="Due 2026-10-28: Exec committee meeting Wed 10-11am",
                 deadline="2026-10-28", thread_brief={"scheduling": "confirmation", "state": "fyi"}),
            _row("deadline-goals", description="Due 2026-09-30: Goal setting reminder",
                 deadline="2026-09-30",
                 thread_brief={"scheduling": "none", "last_from_user": "2026-10-27T20:02:07Z"}),
            _row("deadline-letter", description="Due 2026-10-22: Your letter for a student",
                 deadline="2026-10-22"),
        ],
        "todays_events": [], "availability_merged": [], "handled_by_reply": [],
        "delta_summary": {"open_obligations": 3, "total_inbox": 10}, "recently_closed": [],
    }


def test_the_morning_post_puts_only_owed_dates_under_overdue():
    b = _briefing()
    c = rb.candidates(b, today=TODAY)
    by = {v.get("ledger_id") or v.get("message_id"): v for v in c.values()}
    assert by["deadline-robo"]["due"] == "" and by["deadline-goals"]["due"] == ""
    assert by["deadline-letter"]["due"] == "2026-10-22"
    assert "its Oct 28 date passed; the meeting was Oct 28" in by["deadline-robo"]["tags"]
    assert "its Sep 30 date passed; you wrote Oct 27" in by["deadline-goals"]["tags"]
    assert by["m-sync"]["due"] == "" and by["m-sync"]["tags"] == ["the meeting was Oct 27"]
    post = rb.render_morning(b, c, {}, now=NOW, drafts=[], escalated=[])
    assert post.count("OVERDUE") == 1, post
    assert "OVERDUE: *Due 2026-10-22: Your letter for a student*" in post
    assert "Exec committee" not in post.split(":dart:")[0]
    assert "Goal setting" not in post.split(":dart:")[0]
    # the model's view carries the tag, so it cannot call the row overdue
    items = {i["ref"]: i for i in rb._prompt_items(c)}
    robo = next(i for i in items.values() if i.get("title", "").startswith("Due 2026-10-28"))
    assert "due" not in robo and "the meeting was Oct 28" in robo["tags"][0]
