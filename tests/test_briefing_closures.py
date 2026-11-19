"""Done work leaves the briefing, and the post shows each item once, in the
owner's words, with no work made up (issue #318, items 4 to 11).

  4. a row from chat with no mail has a way to end: `add` takes its date
     from its words or warns; once the date passes the row is put to the
     user; a staged document asks to close once it is ready;
  5. work others owe is a `waiting` row, off the do-list until their date,
     with no action made up for it;
  6. a row merged into another, and work others owe, is never handed off;
  7. a row closed as a repeat hands its date, and the user's word, to the
     row it repeats;
  8. the night's reconcile leaves a lapsing row to the date rule, and a
     card whose rows were closed another way is taken down;
  9. the audit's working text never reaches an item's line;
 10. a row is named once in the post;
 11. a notice of an event already on the calendar is not an item.

All names, mail and ids here are made up.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import tempfile

import pytest

# A made-up install: no live config, policy or ledger is ever read.
EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-briefing-closures-"))
(EA_DIR / "state").mkdir(parents=True, exist_ok=True)
os.environ["EA_DIR"] = str(EA_DIR)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import closure  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import remote_briefing as rb  # noqa: E402

LEDGER = ROOT / "preprocessor" / "ledger.py"
NOW = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)      # Thu Oct 15, 07:00 MT
TODAY = date(2026, 10, 15)


# ── helpers ───────────────────────────────────────────────────────────

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


def _add(conn, rid, *, type_="task", desc="", conv="", status="open", deadline=None,
         born="2026-10-10T12:00:00Z", meta=None, snooze=None):
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, conversation_id, date_identified, "
        "deadline, snooze_until, last_action, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, type_, status, desc or f"desc {rid}", conv, born, deadline, snooze, "",
         json.dumps(meta or {}), born, born))
    conn.commit()


def _row(conn, rid) -> dict:
    return ledger_mod._row_to_dict(conn.execute("SELECT * FROM ledger WHERE id = ?",
                                                (rid,)).fetchone())


def _cli(ea: Path, *args, ok: bool = True) -> dict:
    env = dict(os.environ, EA_DIR=str(ea))
    env.pop("EA_RUN_ID", None)
    r = subprocess.run([sys.executable, str(LEDGER), *args], env=env,
                       capture_output=True, text=True, timeout=60)
    if ok:
        assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout or "{}")
    out["_stderr"] = r.stderr
    return out


def _ea(tmp_path: Path) -> Path:
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    return ea


# ── 4. a row from chat has a way to end ───────────────────────────────

def test_a_rows_own_words_give_its_date():
    """The run-of-show row: written on a Monday evening for "Tuesday".
    A date that already passed names old mail, never the row's end."""
    monday = date(2026, 10, 12)
    assert ledger_mod.date_in_text("Review Tuesday team run of show: milestones", monday) \
        == ("2026-10-13", "Tuesday")
    assert ledger_mod.date_in_text("Partners finish the edits by Friday, October 16", monday) \
        == ("2026-10-16", "Friday")
    assert ledger_mod.date_in_text("Replies to the Oct 9 notes, due Wed Oct 14", monday)[0] \
        == "2026-10-14"
    assert ledger_mod.date_in_text("Slides for the Oct 30 talk; draft by Oct 20", monday)[0] \
        == "2026-10-20"
    assert ledger_mod.date_in_text("Send the notes to all partners Thu Oct 8", monday) == ("", "")
    assert ledger_mod.date_in_text("Cut 2/3 of the budget table", monday) == ("", "")
    assert ledger_mod.date_in_text("Grader hours due by 10/19", monday)[0] == "2026-10-19"
    assert ledger_mod.date_in_text("Submit the form", monday) == ("", "")


def test_add_gives_a_chat_row_its_date_or_warns_loudly(tmp_path):
    ea = _ea(tmp_path)
    dated = _cli(ea, "add", "--id", "rehearsal-notes", "--type", "draft_pending",
                 "--description", "Run of show for the 2099-03-04 rehearsal")
    assert dated["entry"]["event_date"] == "2099-03-04"
    assert "2099-03-04" in dated["end"] and "warning" not in dated
    bare = _cli(ea, "add", "--id", "look-into-it", "--type", "task",
                "--description", "Look into the lab key")
    assert "event_date" not in bare["entry"]
    assert bare["warning"].startswith("WARNING") and "--deadline" in bare["warning"]
    assert "WARNING" in bare["_stderr"]
    # a declared end, a date, or mail behind the row: nothing to warn about
    for extra in (["--closes-on", "manual"], ["--deadline", "2099-01-02"],
                  ["--source-message-id", "msg-1", "--conversation-id", "conv-1"]):
        out = _cli(ea, "add", "--id", f"row-{len(extra)}-{extra[0]}", "--type", "task",
                   "--description", "Look into the lab key", *extra)
        assert "warning" not in out and "event_date" not in out["entry"], extra


def test_a_chat_row_whose_date_passed_is_put_to_the_user(tmp_path):
    """The Nov 19 fault: a document for a dated event, written from chat
    as draft_pending with no mail and no deadline, stayed open after the
    event and after its document went out; nothing could close it."""
    conn = _db(tmp_path)
    delivered = {"at": "2026-10-12T22:00:00Z", "path": "/home/ea/.ea/docs/team/run-of-show.pdf"}
    _add(conn, "team-run-of-show", type_="draft_pending",
         desc="Review Tuesday team run of show",
         meta={"event_date": "2026-10-13", "delivered": delivered})
    _add(conn, "partner-letter", deadline="2026-10-14", desc="Write the partner letter")
    _add(conn, "partner-edits", type_="waiting", deadline="2026-10-14",
         desc="Partners finish the edits")
    _add(conn, "talk-slides", deadline="2026-10-20", desc="Slides for the talk")
    _add(conn, "parked-row", deadline="2026-10-12", snooze="2026-10-18", status="snoozed")
    _add(conn, "mail-row", conv="conv-1", deadline="2026-10-12")
    conn.close()
    out = ledger_mod.close_lapsed(db_path=tmp_path / "ledger.db", today=TODAY)
    assert {(r["id"], r["action"]) for r in out} == {("team-run-of-show", "proposed"),
                                                      ("partner-letter", "proposed")}
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    show = _row(conn, "team-run-of-show")
    assert show["status"] == "open" and show["close_proposal"]["signal"] == "date_passed"
    assert show["close_proposal"]["why"] == ("Its Oct 13 date passed, and the document was "
                                             "ready on Oct 12.")
    for rid in ("partner-edits", "talk-slides", "parked-row", "mail-row"):
        assert "close_proposal" not in _row(conn, rid), rid
    conn.close()
    # asked once: the next run adds nothing, and "Still open" holds
    assert ledger_mod.close_lapsed(db_path=tmp_path / "ledger.db", today=TODAY) == []
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    closure.reject(conn, "partner-letter")
    conn.commit()
    conn.close()
    assert ledger_mod.close_lapsed(db_path=tmp_path / "ledger.db",
                                   today=TODAY + timedelta(days=1)) == []


def test_a_staged_documents_row_asks_to_close_once_the_document_is_ready(tmp_path):
    ea = _ea(tmp_path)
    for rid, kind in (("show-notes", "draft_pending"), ("budget-memo", "task")):
        _cli(ea, "add", "--id", rid, "--type", kind, "--description", f"{rid} for the review",
             "--closes-on", "manual")
    line = "2026-10-12: two-page PDF at /home/ea/.ea/docs/team/Show-Notes.pdf, checked"
    out = _cli(ea, "update", "--id", "show-notes", "--last-action", line)
    assert out["close"] == "proposed" and out["entry"]["status"] == "open"
    assert out["entry"]["close_proposal"]["signal"] == "delivered"
    assert out["entry"]["close_proposal"]["why"] == "the document is ready: Show-Notes.pdf"
    assert out["entry"]["delivered"]["path"].endswith("/Show-Notes.pdf")
    # other work keeps the delivery for its date's card, and asks nothing yet
    memo = _cli(ea, "update", "--id", "budget-memo", "--last-action",
                "2026-10-12: memo at ~/.ea/docs/budget/memo.docx")
    assert "close" not in memo and "close_proposal" not in memo["entry"]
    assert memo["entry"]["delivered"]["path"] == "~/.ea/docs/budget/memo.docx"


# ── 5. work others owe ────────────────────────────────────────────────

def test_add_stores_work_others_owe_as_waiting(tmp_path):
    ea = _ea(tmp_path)
    out = _cli(ea, "add", "--id", "partner-edits", "--type", "deadline", "--description",
               "Harbor proposal: collaborators finish the edits by Friday", "--deadline",
               "2099-01-09")
    assert out["entry"]["type"] == "waiting"
    assert out["type_changed"] == {"from": "deadline", "to": "waiting",
                                   "why": "the description says someone else does the work"}
    mine = _cli(ea, "add", "--id", "merge-edits", "--type", "deadline", "--description",
                "Harbor proposal: merge the collaborators' edits", "--deadline", "2099-01-10")
    assert mine["entry"]["type"] == "deadline" and "type_changed" not in mine


def _briefing(**over) -> dict:
    b = {"open_obligations": [], "today_top": [], "todays_events": [], "tomorrows_events": [],
         "recently_closed": [], "delta_summary": {}}
    b.update(over)
    return b


def _edits_row(due: str) -> dict:
    """The Nov 19 row: others' deadline, written as a `deadline` row."""
    return {"id": "harbor-partner-edits", "type": "deadline", "deadline": due,
            "description": "Harbor proposal: collaborators finish the edits by Friday",
            "notes": "Integrate the edits for the consultant on Monday.", "closes_on": "manual"}


def test_work_others_owe_stays_off_the_do_list_until_their_date_passes():
    b = _briefing(open_obligations=[_edits_row("2026-10-16")])
    c = rb.candidates(b, today=TODAY)
    assert not any(x.get("ledger_id") == "harbor-partner-edits" for x in c.values())
    # their date passed: the row is back, as others' work, with code's words
    b = _briefing(open_obligations=[_edits_row("2026-10-14")])
    c = rb.candidates(b, today=TODAY)
    ref = next(r for r, x in c.items() if x.get("ledger_id") == "harbor-partner-edits")
    assert c[ref]["waiting"] and "others owe this work, not him" in c[ref]["tags"]
    plan = rb.check_plan({"get_done_today": [{"ref": ref, "when": "in the 12:30 gap",
                                              "why": "Check in on the edits"}],
                          "flexes": [{"ref": ref, "line": "check in"}], "drafts": [],
                          "needs_you": [], "escalate": [{"ref": ref, "why": "x", "task": "y"}]},
                         c, morning=True)
    assert plan["flexes"] == [] and plan["escalate"] == []
    post = rb.render_morning(b, c, plan, now=NOW, drafts=[], escalated=[])
    assert "Check in" not in post and "check in" not in post
    assert "Others owe this. Was due Wed Oct 14" in post
    assert "OVERDUE" not in post


# ── 6. ownership: a merged row, a waiting row ─────────────────────────

def test_a_row_merged_into_a_monitoring_row_is_never_its_own_item(tmp_path):
    """The Nov 19 escalation: a reviewer's due date on a row merged into
    the editor's monitoring row went to Claude as the owner's review."""
    import project_store
    conn = _db(tmp_path)
    conn.executescript(project_store.SCHEMA)
    _add(conn, "kept-review-confirm", type_="deadline", conv="conv-r", deadline="2026-10-15",
         desc="Review confirmation - JRNL-2026-0001")
    conn.execute("INSERT INTO ledger (id, type, status, description, created_at, updated_at, "
                 "source) VALUES ('editor-jrnl-2026-0001', 'ae_review_request', 'open', "
                 "'JRNL-2026-0001: a survey', '2026-09-01', '2026-09-01', 'plugin:reviewers')")
    conn.execute("INSERT INTO item_merges (merged_id, primary_id, entity, merged_at) VALUES "
                 "('kept-review-confirm', 'editor-jrnl-2026-0001', 'JRNL-2026-0001', '2026-10-01')")
    conn.commit()
    assert project_store.merged_into(conn) == {"kept-review-confirm": "editor-jrnl-2026-0001"}
    row = {**_row(conn, "kept-review-confirm"), "merged_into": "editor-jrnl-2026-0001"}
    c = rb.candidates(_briefing(open_obligations=[row]), today=TODAY)
    assert c == {}
    conn.execute("UPDATE ledger SET status = 'completed' WHERE id = 'editor-jrnl-2026-0001'")
    assert project_store.merged_into(conn) == {}


# ── 7. a repeat hands over its date and the user's word ───────────────

def test_a_confirmed_repeat_hands_its_newer_date_to_the_row_it_repeats(tmp_path):
    """The Nov 19 pair: the older notice's row carried the window's first
    day, the newer one's the last. The owner confirmed the newer row as a
    repeat; the older row kept its passed date and read as overdue."""
    conn = _db(tmp_path)
    _add(conn, "deadline-window-opens", type_="deadline", conv="c1", deadline="2026-10-12",
         desc="Due 2026-10-12: Course surveys open next week", born="2026-10-08T12:00:00Z")
    _add(conn, "deadline-window-closes", type_="deadline", conv="c2", deadline="2026-10-27",
         desc="Due 2026-10-27: Course surveys are open", born="2026-10-12T12:00:00Z")
    closure.propose(conn, "deadline-window-closes", signal="llm:reconcile-duplicate",
                    actor="reconcile", note="duplicate of deadline-window-opens: the same reminder",
                    evidence={"group": "loose#1"})
    out = closure.confirm(conn, "deadline-window-closes")
    assert out["folded"] == {"deadline": "2026-10-27"}
    prim = _row(conn, "deadline-window-opens")
    assert prim["status"] == "open" and prim["deadline"] == "2026-10-27"
    assert prim["description"] == "Due 2026-10-27: Course surveys open next week"
    assert "from a newer message about the same item" in prim["last_action"]
    kinds = [e["kind"] for e in conn.execute(
        "SELECT kind FROM ledger_events WHERE entry_id = 'deadline-window-opens'")]
    assert kinds == ["fold"]


def test_a_repeat_closed_on_the_users_word_closes_the_row_it_repeats(tmp_path):
    ea = _ea(tmp_path)
    for rid in ("survey-reminder", "survey-reminder-2"):
        _cli(ea, "add", "--id", rid, "--type", "task", "--description", "Remind the class",
             "--closes-on", "manual")
    out = _cli(ea, "close", "--id", "survey-reminder-2", "--duplicate-of", "survey-reminder",
               "--user-said", "I reminded the class today", "--skip-archive")
    assert out["action"] == "closed" and out["folded_into"]["closed"] == ["survey-reminder"]
    prim = _cli(ea, "get", "--id", "survey-reminder")
    assert prim["status"] == "completed" and prim["closed_by"]["signal"] == "user_stated"
    # a repeat closed with no word of the user's leaves the other row open
    for rid in ("lab-keys", "lab-keys-2"):
        _cli(ea, "add", "--id", rid, "--type", "task", "--description", "Lab keys",
             "--closes-on", "manual")
    _cli(ea, "close", "--id", "lab-keys-2", "--duplicate-of", "lab-keys", "--skip-archive")
    assert _cli(ea, "get", "--id", "lab-keys")["status"] == "open"


# ── 8. one close, one card ────────────────────────────────────────────

def test_the_reconcile_leaves_a_row_that_lapses_to_the_date_rule():
    """The Nov 19 card: the night proposed "lapsed" for a row that code
    closed two hours later, and the card stayed up for a closed row."""
    import ledger_reconcile as lr
    row = {"id": "deadline-signup", "type": "deadline", "deadline": "2026-10-14",
           "closes_on": "lapse", "thread_brief": {"needs_reply": False, "ask": "", "messages": [
               {"from": "events@example.edu", "ask": ""}]}}
    later = dict(row, id="deadline-later", deadline="2026-10-20")
    group = {"label": "loose#1", "rows": [row, later], "project": ""}
    items = [{"ref": "R1", "title": "Sign-up"}, {"ref": "R2", "title": "Later sign-up"}]
    verdicts = [{"ref": r, "verdict": "lapsed", "of": "", "thread": "", "note": "",
                 "why": "The sign-up closed."} for r in ("R1", "R2")]
    asked = []
    out = lr.apply(group, items, {}, verdicts, now=NOW, dry_run=False,
                   propose=lambda rid, **kw: asked.append(rid) or {"action": "proposed"},
                   set_meta=lambda *a, **k: None, append_action=lambda *a: None,
                   set_conv=lambda *a: None)
    assert asked == ["deadline-later"]
    assert out["skipped"] == [{"id": "deadline-signup", "why": "the date rule closes it"}]


# ── 9, 10. the post: no audit text, one line per row ─────────────────

def _post_briefing() -> dict:
    follow = {"id": "lab-install", "type": "task", "description": "Send Kim the install steps",
              "closes_on": "manual"}
    survey = {"id": "deadline-survey", "type": "deadline", "deadline": "2026-10-27",
              "conversation_id": "c-survey", "description": "Due 2026-10-27: Remind the class"}
    return _briefing(open_obligations=[follow, survey], resurrected_follow_ups=[follow])


def test_audit_working_text_never_reaches_an_items_line():
    b = _post_briefing()
    c = rb.candidates(b, today=TODAY)
    refs = {x["ledger_id"]: r for r, x in c.items()}
    plan = {"get_done_today": [{"ref": refs["deadline-survey"], "when": "after class",
                                "why": "Remind the class"}],
            "flexes": [{"ref": refs["lab-install"], "line": "Send the steps"}]}
    audit = {"closed": [], "parked": [], "unsure_rows": [
        {"id": "deadline-survey", "why": "A closed row is about the same survey, but its notes "
                                         "still ask for a reminder and do not establish it"},
        {"id": "lab-install", "why": "Some steps went out on Monday"}]}
    post = rb.render_morning(b, c, plan, now=NOW, drafts=[], escalated=[], audit=audit)
    assert "Evidence" not in post and "still open?" not in post and "establish" not in post
    assert "*Remind the class*: after class. Remind the class. Due Tue Oct 27" in post


def test_a_row_is_named_once_in_the_post():
    """The Nov 19 post named the install row on the do-list and again as
    "Follow-up due"; a drafted reply also stood in the date banner."""
    b = _post_briefing()
    c = rb.candidates(b, today=TODAY)
    refs = {x["ledger_id"]: r for r, x in c.items()}
    plan = {"get_done_today": [{"ref": refs["lab-install"], "when": "first thing",
                                "why": "Send the steps"}]}
    post = rb.render_morning(b, c, plan, now=NOW, drafts=[], escalated=[])
    assert post.count("Send Kim the install steps") == 1, post
    assert "Follow-up due" not in post
    alone = rb.render_morning(b, c, {"get_done_today": []}, now=NOW, drafts=[], escalated=[])
    assert ":alarm_clock: Follow-up due: Send Kim the install steps" in alone
    soon = dict(b["open_obligations"][1], deadline="2026-10-16")
    b2 = _briefing(open_obligations=[soon])
    c2 = rb.candidates(b2, today=TODAY)
    ref = next(iter(c2))
    drafted = rb.render_morning(b2, c2, {"get_done_today": []}, now=NOW, escalated=[],
                                drafts=[{"ref": ref, "ok": True, "says": "Reminder sent."}])
    assert drafted.count("Remind the class") == 1 and "DUE SOON" not in drafted


# ── 11. an event on the calendar is not an item ───────────────────────

def _event(subject: str, start: str) -> dict:
    return {"subject": subject, "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": start, "timeZone": "UTC"}}


def test_a_notice_of_an_event_on_the_calendar_is_not_an_item():
    town_hall = {"id": "deadline-town-hall", "type": "deadline", "deadline": "2026-10-15",
                 "conversation_id": "c-th", "closes_on": "lapse",
                 "description": "Due 2026-10-15: Reminder: Department Town Hall and Trivia Night",
                 "thread_brief": {"needs_reply": False, "ask": "", "messages": [
                     {"from": "grads@example.edu", "ask": ""}]}}
    picnic = dict(town_hall, id="deadline-picnic", conversation_id="c-p",
                  description="Due 2026-10-15: Lab picnic signup")
    attend = {"id": "deadline-seminar", "type": "deadline", "deadline": "2026-10-16",
              "conversation_id": "c-s", "description": "Due 2026-10-16: Robotics seminar",
              "thread_brief": {"needs_reply": False, "ask": "Attend the robotics seminar",
                               "messages": [{"from": "dept@example.edu",
                                             "ask": "Attend the robotics seminar"}]}}
    b = _briefing(open_obligations=[town_hall, picnic, attend],
                  todays_events=[_event("Grad council: Department Town Hall panel",
                                        "2026-10-15T22:30:00")],
                  tomorrows_events=[_event("Robotics Seminar series", "2026-10-16T18:00:00")])
    c = rb.candidates(b, today=TODAY)
    assert {x["ledger_id"] for x in c.values()} == {"deadline-picnic"}
    assert rb.on_calendar("Reminder: Town Hall", TODAY, b, TODAY) \
        == "Grad council: Department Town Hall panel"
    assert rb.on_calendar("Reminder: Town Hall", TODAY + timedelta(days=5), b, TODAY) == ""
