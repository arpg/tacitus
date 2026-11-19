"""briefing_review: the hosted model's check of a briefing before it posts.

One made-up morning holds an item of each fault class the check exists
for: a reply drafted against the owner's own recorded decision, a yes or
no answered for him, an item headed to the wrong project room, finished
work for a past event, work that waits on others in the do-list, someone
else's deadline shown as his, one obligation on two rows (one closed), a
closed row still in the post, audit text inside a line, one row twice,
and an event already on the calendar. Code's own checks catch some with
no model; a recorded answer stands in for the hosted model for the rest.
Nothing here reaches the network, Graph, a model, the real ledger or the
real private dir."""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import briefing_review as br  # noqa: E402
import closure  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import owner_trust  # noqa: E402
import remote_briefing as rb  # noqa: E402
import remote_llm as rl  # noqa: E402

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)      # Thu 05:00 MT
TODAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def mountain(monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "kit@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    monkeypatch.setattr(br, "_rules", lambda: "- Never answer an invitation for me.")
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "drafting": "remote", "briefings": "remote", "shadow_until": "2099-01-01"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


# ── the made-up morning ───────────────────────────────────────────────

def _row(rid, typ, desc, **kw):
    base = {"id": rid, "type": typ, "status": "open", "description": desc, "from_email": "",
            "from_name": "", "source_message_id": "", "conversation_id": "",
            "date_identified": "2026-10-10T12:00:00Z", "deadline": None, "follow_up_after": None,
            "snooze_until": None, "last_action": "", "notes": "", "priority": None, "email_archived": 0,
            "source": None, "metadata": "{}", "created_at": "2026-10-10T12:00:00Z",
            "updated_at": "2026-10-10T12:00:00Z", "closed_at": None}
    base.update(kw)
    return base


ROWS = [
    # 1. The owner said no on an earlier row of the same thread; a follow-up
    #    made a new row, and the plan drafts a yes to everyone on it.
    _row("row-pilot", "task", "Pilot program invitation", status="completed",
         conversation_id="c-pilot", closed_at="2026-10-09T15:00:00Z",
         last_action="Kit will not join the pilot and wants no reply."),
    _row("row-pilot-2", "response_needed", "Pilot program follow-up", conversation_id="c-pilot",
         source_message_id="m-pilot-2", from_email="ana@example.edu", from_name="Ana Diaz",
         notes="Ana asks for a yes or no on the pilot by Friday."),
    # 2. A yes or no only the owner can give, answered for him.
    _row("row-panel", "response_needed", "Panel seat for the spring workshop",
         conversation_id="c-panel", source_message_id="m-panel", from_email="lee@example.edu",
         from_name="Lee Wong", notes="Lee asks whether Kit will sit on the panel: yes or no."),
    # 3 and 5. Collaborators' own deadline, no link to its project.
    _row("row-edits", "deadline", "Survey paper: co-authors finish their edits by Friday",
         deadline="2026-10-16", notes="The co-authors' deadline; Kit integrates the edits on Monday."),
    # 4. Prep for a workshop that was on Tuesday.
    _row("row-runshow", "draft_pending", "Review Tuesday's workshop run of show",
         notes="Run of show PDF for the Oct 13 workshop.", date_identified="2026-10-12T12:00:00Z"),
    # 6. A reviewer's deadline; and the same thread kept from hosted models.
    _row("row-reviewer", "waiting", "Reviewer report for manuscript SUR-0042", deadline="2026-10-15",
         conversation_id="c-review", source_message_id="m-review",
         notes="The confirmation is addressed to the reviewer; the Oct 15 deadline is the reviewer's."),
    _row("kept-secret", "deadline", "Review confirmation", deadline="2026-10-15",
         conversation_id="c-secret", source_message_id="m-secret",
         notes="Secret manuscript details that must stay local.",
         metadata=json.dumps({"kept_from_frontier": "keep list"})),
    # 7. One obligation on two rows: the duplicate closed, this one open past its date.
    _row("row-survey", "deadline", "Course survey opens", deadline="2026-10-12",
         conversation_id="c-survey", notes="Remind students between Oct 12 and Oct 30."),
    _row("row-survey-2", "deadline", "Course survey reminder", status="completed",
         conversation_id="c-survey-2", closed_at="2026-10-13T10:00:00Z"),
    # 8. A row closed before the post (its date lapsed) still in the plan.
    _row("row-lapsed", "deadline", "Admissions workshop sign-up", status="completed",
         deadline="2026-10-14", closed_at="2026-10-15T10:30:00Z"),
    # 10. The same row on the do-list and on a Follow-up due line.
    _row("row-install", "task", "Send Sam the install guide"),
    # 11. An event already on today's calendar.
    _row("row-townhall", "deadline", "Department town hall", deadline="2026-10-15",
         notes="Town hall 4-6 PM today."),
]
EVENTS = [
    ("2026-10-09T15:00:00Z", "row-pilot", "close", "decisive", "user_stated", "cli:chat",
     json.dumps({"quote": "Not joining. No reply."}), "Kit will not join the pilot."),
    ("2026-10-13T10:00:00Z", "row-survey-2", "close", "decisive", "duplicate_of", "reconcile",
     "{}", "duplicate of row-survey: the same student reminder."),
]


def _db(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(str(path))
    c.executescript(ledger_mod.SCHEMA)
    c.executescript(closure.EVENTS_SCHEMA)
    c.executescript("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, status TEXT);"
                    "CREATE TABLE project_ledger (project_id TEXT, ledger_id TEXT);")
    cols = list(ROWS[0])
    c.executemany(f"INSERT INTO ledger ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [[r[k] for k in cols] for r in ROWS])
    c.executemany("INSERT INTO ledger_events (ts, entry_id, kind, tier, signal, actor, evidence, note) "
                  "VALUES (?,?,?,?,?,?,?,?)", EVENTS)
    c.executemany("INSERT INTO projects VALUES (?,?,?)",
                  [("survey", "Survey paper", "active"), ("radar", "Radar grant", "active")])
    c.execute("INSERT INTO project_ledger VALUES ('survey', 'row-runshow')")
    c.commit()
    return c


def _briefing():
    opened = [{k: v for k, v in r.items() if k != "metadata"} for r in ROWS if r["status"] == "open"]
    opened += [{k: v for k, v in r.items() if k != "metadata"} for r in ROWS if r["id"] == "row-lapsed"]
    for o in opened:
        if o["id"] == "row-lapsed":
            o["status"] = "open"     # the briefing's copy predates the close
    return {
        "new_needs_response": [
            {"message_id": "m-pilot-2", "conversation_id": "c-pilot", "subject": "RE: Pilot program",
             "from": "ana@example.edu", "from_name": "Ana Diaz",
             "to": ["kit@example.edu"] + [f"member{i}@example.edu" for i in range(23)],
             "cc": [], "body_preview": "Ana asks for a yes or no on the pilot by Friday.",
             "remote_record": {"ask": "Reply yes or no on joining the pilot.", "deadline": "2026-10-16"}},
            {"message_id": "m-panel", "conversation_id": "c-panel", "subject": "Spring workshop panel",
             "from": "lee@example.edu", "from_name": "Lee Wong", "to": ["kit@example.edu"], "cc": [],
             "body_preview": "Lee asks whether Kit will sit on the panel.",
             "remote_record": {"ask": "Say yes or no to the panel seat."}},
        ],
        "open_obligations": opened,
        "resurrected_follow_ups": [{"id": "row-install", "description": "Send Sam the install guide"}],
        "todays_events": [{"subject": "Department town hall",
                           "start": {"dateTime": "2026-10-15T22:00:00.0000000", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-10-16T00:00:00.0000000", "timeZone": "UTC"}}],
        "delta_summary": {"open_obligations": 9, "total_inbox": 20},
        "recently_closed": [],
    }


def _morning(b):
    cands = rb.candidates(b, today=TODAY)
    ref = {c["ledger_id"] or c["message_id"]: r for r, c in cands.items()}
    r = lambda lid: ref[lid]  # noqa: E731
    plan = {"get_done_today": [{"ref": r("row-install"), "when": "first thing", "why": "Sam waits"},
                               {"ref": r("row-edits"), "when": "in the 1-2 gap", "why": "check in"},
                               {"ref": r("row-survey"), "when": "", "why": "remind students"}],
            "flexes": [{"ref": r("row-runshow"), "line": "review the PDF"},
                       {"ref": r("row-townhall"), "line": "attend the town hall"},
                       {"ref": r("row-lapsed"), "line": "sign up"}],
            "drafts": [], "needs_you": [], "escalate": []}
    pilot, panel = ref["row-pilot-2"], ref["row-panel"]
    drafts = [{"ref": pilot, "ok": True, "says": "Yes, I would like to join the pilot.", "ack": False,
               "pending": True, "draft_id": ""},
              {"ref": panel, "ok": True, "says": "Yes, I will sit on the panel.", "ack": False,
               "pending": True, "draft_id": ""}]
    escalated = [{"ref": r("row-reviewer"), "why": "the review is due today"},
                 {"ref": r("kept-secret"), "why": "a full review is due today"}]
    return cands, plan, drafts, escalated, r


ROOM_NAMES = {"survey": "Survey paper", "radar": "Radar grant"}


def _rooms(r):
    return [{"project": "radar", "room": "ea-radar", "refs": [r("row-edits")],
             "also": ["row-runshow"], "also_text": ["Review Tuesday's workshop run of show"]},
            {"project": "survey", "room": "ea-survey", "refs": [r("row-edits")], "also": [], "also_text": []}]


def _answer(r):
    """What the hosted model answered, recorded; refs as this test's
    candidates name them. Code refuses the last five: a ref it never gave,
    a room that does not exist, a second verdict for one ref, a number no
    record holds, a hold on an item that is not a draft."""
    return json.dumps({"items": [
        {"ref": r("row-panel"), "action": "hold-draft",
         "why": "The yes or no is his to give; the draft answers it for him."},
        {"ref": r("row-edits"), "action": "drop", "kind": "not-owners",
         "why": "Friday is the co-authors' deadline, not his."},
        {"ref": r("row-runshow"), "action": "drop", "kind": "done",
         "why": "It prepared for the Oct 13 workshop, which is past."},
        {"ref": r("row-reviewer"), "action": "rewrite-line",
         "line": "The reviewer's report is due today; nothing for you to write.",
         "why": "The deadline is the reviewer's."},
        {"ref": r("row-survey"), "action": "rewrite-line",
         "line": "Remind students to fill in the course survey by Oct 30.",
         "why": "The Oct 12 date is when the survey opened."},
        {"ref": r("row-townhall"), "action": "drop", "kind": "on-calendar",
         "why": "The town hall is on today's calendar."},
        {"ref": "A1", "action": "move", "to": "room:none", "why": "Wrong room."},
        {"ref": "X9", "action": "drop", "kind": "done", "why": "invented"},
        {"ref": r("row-install"), "action": "move", "to": "room:moon", "why": "no such room"},
        {"ref": r("row-survey"), "action": "drop", "kind": "done", "why": "second verdict, ignored"},
        {"ref": r("kept-secret"), "action": "rewrite-line", "line": "Due in 47 days.", "why": "invented"},
        {"ref": r("row-install"), "action": "hold-draft", "why": "not a draft"},
    ], "notes": ["Two items were someone else's work."]})


def _review(tmp_path, *, call=None, who="claude", dry_run=False, fallback=None, levels=None,
            state=None):
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    lists = rb._morning_lists(cands, plan, drafts, escalated, {}, TODAY)
    text = rb.render_morning(b, cands, plan, now=NOW, drafts=drafts, escalated=escalated)
    seen = {"proposed": [], "prompts": [], "log": []}

    def recorded(prompt, timeout):
        seen["prompts"].append(prompt)
        return {"text": _answer(r), "rc": 0, "model_used": who}
    levels = levels if levels is not None else {"kept-secret": "local"}
    checked = br.review_briefing(
        "morning-briefing", text, b, cands=cands, lists=lists, drafts=drafts, now=NOW,
        dry_run=dry_run, run_id="cron-morning-briefing-t", room_posts=_rooms(r), room_names=ROOM_NAMES,
        bodies={r("row-pilot-2"): "Hi Ana,\n\nYes, I would like to join the pilot.\n\nKit"},
        conn=_db(tmp_path), levels=levels, evidence={}, call=call or recorded, who=who,
        propose=lambda lid, why: seen["proposed"].append(lid) or True,
        appender=lambda name, rec: seen["log"].append((name, rec)), fallback=fallback, state=state)
    return b, cands, plan, drafts, escalated, r, checked, seen


# ── the faults, one by one ────────────────────────────────────────────

def test_code_holds_a_reply_against_the_owners_own_close_with_no_model(tmp_path, priv):
    """Fault 1: the thread's earlier row closed in the owner's words. Code
    holds the draft before any model; the model's answer cannot undo it."""
    *_, r, checked, seen = _review(tmp_path)
    hold = checked["review"]["hold"]
    assert hold[r("row-pilot-2")] == 'you closed this thread on Oct 9: "Not joining. No reply."'
    assert checked["review"]["by"][r("row-pilot-2")] == "code"


def test_the_prompt_carries_the_records_and_never_kept_mail(tmp_path, priv):
    *_, r, checked, seen = _review(tmp_path)
    prompt = seen["prompts"][0]
    packet = json.loads(prompt.split("The post and its items:\n", 1)[1])
    items = {i["ref"]: i for i in packet["items"]}
    pilot = items[r("row-pilot-2")]
    sib = pilot["same_thread"][0]
    assert sib["closed_by_owner"]["quote"] == "Not joining. No reply."
    assert pilot["draft"]["to_count"] == 24 and "join the pilot" in pilot["draft"]["body"]
    # 7: the closed duplicate names this row
    assert items[r("row-survey")]["named_by"][0]["row"] == "row-survey-2"
    # 6: a thread kept from hosted models goes as its line and dates only
    kept = items[r("kept-secret")]
    assert kept["withheld"] and "notes" not in kept["row"] and "Secret manuscript" not in prompt
    assert "Department town hall" in json.dumps(packet["calendar_today"])
    assert packet["rooms"] == {"room:radar": "Radar grant", "room:survey": "Survey paper"}
    assert "Never answer an invitation for me." in packet["owner_rules"]
    assert {"A1"} <= set(items) and items["A1"]["section"] == "also"


def test_each_fault_class_and_what_the_post_shows(tmp_path, priv):
    b, cands, plan, drafts, escalated, r, checked, seen = _review(tmp_path)
    rv = checked["review"]
    assert rv["status"] == "ok"
    # 2: a yes or no answered for him is held
    assert r("row-panel") in rv["hold"]
    # 5 and 3: others' deadline leaves the do-list and every room
    assert rv["drop"][r("row-edits")]["kind"] == "not-owners"
    # 4: done is a proposal for the owner, never a close
    assert seen["proposed"] == ["row-runshow"] and checked["closed"] == ["row-runshow"]
    # 6 and 7: lines that claimed what the records do not say
    assert rv["rewrite"][r("row-reviewer")].startswith("The reviewer's report is due today")
    assert rv["rewrite"][r("row-survey")] == "Remind students to fill in the course survey by Oct 30."
    # 8: a row closed since the briefing's copy was taken leaves, by code
    assert rv["drop"][r("row-lapsed")] == {"kind": "closed", "why": "it is closed", "by": "code"}
    # 10: one row twice: the Follow-up due line goes
    assert rv["hide_tail"] == ["row-install"]
    # 11: on the calendar
    assert rv["drop"][r("row-townhall")]["kind"] == "on-calendar"
    # 3: the room's Also open list
    assert rv["also_skip"] == {"radar": ["row-runshow"]}
    # the model cannot add, invent a room, invent a number, or hold what is no draft
    rejected = {(x["ref"], x["why"]) for x in checked["review_record"]["rejected"]}
    assert ("X9", "unknown ref") in rejected
    assert (r("row-install"), "unknown room") in rejected
    assert (r("kept-secret"), "a number the records do not hold") in rejected
    assert (r("row-install"), "hold-draft on an item that is not a draft") in rejected

    br.settle_drafts(drafts, cands, checked, run_id="t", dry_run=True)
    text = rb.render_morning(b, cands, plan, now=NOW, drafts=drafts, escalated=escalated, audit=checked)
    top = text.split(":dart:")[1].split("\n\n")[0]
    assert "co-authors finish" not in top and "Send Sam the install guide" in top
    assert "Follow-up due" not in text
    assert "Drafts ready" not in text
    needs = text.split("*Needs you*")[1]
    assert 'I held the reply I wrote: you closed this thread on Oct 9: "Not joining. No reply." Tell me' in needs
    assert "Department town hall" not in text.split(":clipboard:")[0]
    assert "*Looks done?* (1)" in text and "Review Tuesday's workshop run of show" in text
    assert "Remind students to fill in the course survey by Oct 30." in text
    assert "Evidence:" not in text and "—" not in text
    footer = text.split(":mag: *Checked before sending* by Claude.")[1]
    assert "Left out *Survey paper: co-authors finish their edits by Friday*: Friday is the co-authors'" in footer
    assert "Held the reply to *Spring workshop panel*" in footer
    assert "To keep their mail private, Claude did not see the records of 1 item." in footer
    assert "Note: Two items were someone else's work." in footer
    assert "*Survey paper: co-authors" not in footer.split("Held the reply")[1]


def test_a_held_reply_is_never_staged_and_the_queue_records_a_decision(tmp_path, priv, monkeypatch):
    """The writer runs before the check; create-reply runs after it, for
    the replies it did not hold. A held reply never reaches Drafts, so the
    bot never posts its Send card."""
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    pending = br.PendingDrafts()
    monkeypatch.setattr(rb, "write_reply", lambda c, says, write=None: {"ok": True, "body": f"Hi. {says}"})
    staged, marks = [], []
    for d in drafts:
        res = pending(cands[d["ref"]], d["says"], run_id="t")
        d.update(draft_id=res["draft_id"], pending=res["pending"])
    mark = pending.wrap(lambda *a, **k: marks.append(("now", a, k)))
    mark("row-pilot-2", "2026-10-14T10:00:00Z", "drafted", now=NOW, note="Yes")
    mark("row-panel", "2026-10-14T11:00:00Z", "drafted", now=NOW, note="Yes")
    assert marks == []                          # waits for the check
    checked = {"review": {"hold": {r("row-pilot-2"): "you said no reply"}}}
    out = br.settle_drafts(drafts, cands, checked, run_id="cron-t", dry_run=False, pending=pending,
                           mark=lambda lid, inbound, outcome, now, draft_id="", note="": marks.append(
                               (lid, outcome, draft_id)),
                           stage=lambda c, body, run_id: staged.append((c["message_id"], body)) or
                           {"ok": True, "draft_id": "d-7"})
    assert out == {"held": 1, "staged": 1, "failed": 0, "withdrawn": 0}
    assert staged == [("m-panel", "Hi. Yes, I will sit on the panel.")]
    assert drafts[0]["ok"] is False and drafts[0]["held"] == "you said no reply"
    assert ("row-pilot-2", "decision", "") in marks and ("row-panel", "drafted", "d-7") in marks


def test_a_reply_create_reply_refuses_becomes_the_gates_decision(tmp_path, priv, monkeypatch):
    """A reply the check let through that create-reply then refuses for a
    scheduled lane (draft_gate.cron_refusal, on the message itself) is the
    owner's decision as the gate words it, with its choices, and the queue
    records a decision: the line never carries the answer the model wrote."""
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    pending = br.PendingDrafts()
    monkeypatch.setattr(rb, "write_reply", lambda c, says, write=None: {"ok": True, "body": f"Hi. {says}"})
    for d in drafts:
        res = pending(cands[d["ref"]], d["says"], run_id="t")
        d.update(draft_id=res["draft_id"], pending=res["pending"])
    marks = []
    mark = pending.wrap(lambda *a, **k: marks.append(("now", a, k)))
    mark("row-panel", "2026-10-14T11:00:00Z", "drafted", now=NOW, note="Yes")
    refused = {"ok": False, "why": "the reply needs the owner", "owner": "a reply-all that reaches 12 people"}
    out = br.settle_drafts(drafts, cands, {"review": {"hold": {}}}, run_id="cron-t", dry_run=False,
                           pending=pending,
                           mark=lambda lid, inbound, outcome, now, draft_id="", note="", **k:
                           marks.append((lid, outcome, note, k.get("choices"))),
                           stage=lambda c, body, run_id: refused if c["message_id"] == "m-panel"
                           else {"ok": True, "draft_id": "d-8"})
    assert out["refused"] == 1 and out["staged"] == 1 and out["failed"] == 0
    panel = drafts[1]
    want = rb._refused(cands[panel["ref"]], refused["owner"])
    assert panel["ok"] is False and not panel.get("failed")
    assert panel["decision"] == want["line"] and panel["choices"] == want["choices"]
    assert "sit on the panel" not in panel["decision"]
    assert [m for m in marks if m[0] == "row-panel"] == [
        ("row-panel", "decision", want["line"], list(want["choices"]) or None)]


def test_a_reply_already_staged_by_a_stand_in_is_withdrawn(tmp_path, priv):
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    drafts[0].update(pending=False, draft_id="d-old")
    gone = []
    out = br.settle_drafts(drafts, cands, {"review": {"hold": {r("row-pilot-2"): "x"}}}, run_id="t",
                           dry_run=False, withdraw=lambda did: gone.append(did) or True)
    assert gone == ["d-old"] and out["withdrawn"] == 1 and drafts[0]["draft_id"] == ""


def test_room_slices_follow_the_review(tmp_path, priv, monkeypatch):
    """Fault 3: an item the review moved out of a room is not posted there,
    and a row it took off a room's Also open list is not listed."""
    import briefing_rooms
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE projects (id TEXT, name TEXT, status TEXT);"
                       "CREATE TABLE project_ledger (project_id TEXT, ledger_id TEXT);"
                       "CREATE TABLE project_messages (project_id TEXT, conversation_id TEXT);")
    conn.executemany("INSERT INTO projects VALUES (?,?,?)", [("radar", "Radar grant", "active"),
                                                            ("survey", "Survey paper", "active")])
    conn.executemany("INSERT INTO project_ledger VALUES (?,?)",
                     [("radar", "row-install"), ("radar", "row-runshow"), ("radar", "row-survey")])
    monkeypatch.setattr(briefing_rooms, "enabled", lambda cfg=None: True)
    targets = {"radar": {"channel": "r1", "room": "ea-radar"}, "survey": {"channel": "r2", "room": "ea-survey"}}
    conn.execute("INSERT INTO project_ledger VALUES ('radar', 'row-other')")
    other = {"id": "row-other", "type": "task", "status": "open", "description": "Order the radar parts"}
    kw = dict(now=NOW, dry_run=True, conn=conn, targets=targets, open_rows=b["open_obligations"] + [other],
              matcher=lambda text, c: [{"id": "radar", "score": 3}] if "co-authors" in text else [])
    before = rb.room_slices("morning-briefing", cands, plan, [], [], {}, b, **kw)
    assert [p["room"] for p in before] == ["ea-radar"] and before[0]["also"] == ["row-other"]
    review = {"rooms": {r("row-install"): "room:survey"}, "also_skip": {"radar": ["row-other"]},
              "drop": {r("row-edits"): {"kind": "not-owners"}}}
    after = rb.room_slices("morning-briefing", cands, plan, [], [], {"review": review}, b, **kw)
    by_room = {p["room"]: p for p in after}
    assert r("row-install") in by_room["ea-survey"]["refs"]
    assert r("row-install") not in by_room["ea-radar"]["refs"] and r("row-edits") not in by_room["ea-radar"]["refs"]
    assert by_room["ea-radar"]["also"] == [] and "Order the radar parts" not in by_room["ea-radar"]["text"]


def test_audit_text_never_reaches_a_line():
    """Fault 9: the still-open audit's clause, cut mid-word, is cut from
    every line on every render, review or not."""
    lists = {"top": [("L1", "*Survey* (x): remind students. Was due Mon Oct 12 Evidence: The Oct 13 "
                            "closed row concerns the same rem; still open?")],
             "flex": [("L2", "*Calendar changes*: apply them (reconcile)")],
             "draft": [("R1", "*Talk*: Yes, the date works.")]}
    out = br.apply_lists(lists, None)
    assert out["top"] == [("L1", "*Survey* (x): remind students. Was due Mon Oct 12")]
    assert out["flex"] == [("L2", "*Calendar changes*: apply them")]
    assert out["draft"] == [("R1", "*Talk*: Yes, the date works.")]


# ── failure, routing, the record, the light ───────────────────────────

def test_a_failed_check_falls_back_to_the_audit_and_says_so(tmp_path, priv):
    calls = []

    def limited(prompt, timeout):
        return {"text": "", "rc": 1, "stderr": "Claude AI usage limit reached", "model_used": "claude"}

    def audit():
        calls.append("audit")
        return {"closed": [], "closed_rows": [], "unsure_rows": [], "parked": [], "asked": 2,
                "judge": "remote (Claude failed)"}
    b, cands, plan, drafts, escalated, r, checked, seen = _review(tmp_path, call=limited, fallback=audit)
    rv = checked["review"]
    assert calls == ["audit"] and rv["status"] == "failed" and rv["reason"] == "limit"
    assert r("row-pilot-2") in rv["hold"] and r("row-lapsed") in rv["drop"]     # code still checks
    assert r("row-edits") not in rv["drop"]
    assert rv["footer"][1] == (":warning: *Not checked before sending*: your plan's limit is reached. "
                               "Tacitus's own checks ran.")
    assert seen["log"][0][0] == br.REVIEW_LOG and seen["log"][0][1]["status"] == "failed"

    def slow(prompt, timeout):
        raise subprocess.TimeoutExpired("claude", timeout)
    *_, checked, _seen = _review(tmp_path / "t", call=slow)
    assert checked["review"]["reason"] == "timeout"
    assert "did not answer in time" in checked["review"]["footer"][1]
    *_, checked, _seen = _review(tmp_path / "u", call=lambda p, timeout: {"text": "no json here", "rc": 0})
    assert checked["review"]["reason"] == "unreadable"


def test_review_routed_to_the_owners_own_model_says_so(tmp_path, priv):
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    lists = rb._morning_lists(cands, plan, drafts, escalated, {}, TODAY)
    asked = []

    def chat(messages, **kw):
        asked.append(kw["purpose"])
        return json.loads(_answer(r)), {"usage": {"prompt_tokens": 10}}
    checked = br.review_briefing("morning-briefing", "post", b, cands=cands, lists=lists, drafts=drafts,
                                 now=NOW, dry_run=True, conn=_db(tmp_path / "v"), levels={},
                                 evidence={}, who="remote", chat=chat, appender=lambda n, rec: None)
    assert asked == ["briefing-review"] and checked["review"]["status"] == "local"
    assert checked["review"]["footer"][1].startswith(":mag: *Checked before sending* by your own model, "
                                                     "not by Claude or Codex.")


def test_the_run_is_recorded_with_size_time_and_what_changed(tmp_path, priv):
    *_, checked, seen = _review(tmp_path)
    name, rec = seen["log"][0]
    assert name == "briefing-reviews.jsonl"
    for k in ("prompt_chars", "est_prompt_tokens", "asked", "verdicts", "rejected", "hard", "changes",
              "seconds", "call_seconds", "withheld", "status", "model"):
        assert k in rec, k
    assert rec["changes"]["proposed"] == ["row-runshow"] and rec["withheld"]
    assert br.summary(checked)["held"] == 2


def test_the_prompt_stays_bounded_and_the_post_names_what_was_cut(tmp_path, priv, monkeypatch):
    monkeypatch.setattr(br, "PROMPT_MAX_CHARS", 9000)
    b = _briefing()
    cands, plan, drafts, escalated, r = _morning(b)
    items = [{"ref": f"L{i}", "section": "top", "sections": ["top"], "line": "x" * 200,
              "ledger_id": f"row-{i}", "title": f"Item {i}", "rooms": []} for i in range(60)]
    led = {"rows": {f"row-{i}": _row(f"row-{i}", "task", "d" * 300, notes="n" * 2000) for i in range(60)},
           "events": {}, "by_conv": {}, "naming": {}}
    packet, meta = br.build_packet(agent="morning-briefing", post="p", items=items, ledger=led, levels={},
                                   projects={}, drafts={}, evidence={}, calendar=[], rooms={}, rules="")
    assert len(json.dumps(packet)) <= 9000 and meta["shed_step"] == len(br.SHED) - 1
    cut = meta["cut"]
    assert len(cut) > 15 and cut == [f"L{i}" for i in range(60 - len(cut), 60)]
    lines = br.footer({"status": "ok", "model": "codex", "cut": meta["cut"]}, items)
    assert lines[-1].endswith("items were not checked: the post was too long.")


def test_keep_levels_fail_closed(monkeypatch):
    import mail_privacy
    monkeypatch.setattr(mail_privacy, "load_index", lambda: (_ for _ in ()).throw(OSError("locked")))
    levels = br.keep_levels([{"id": "a", "conversation_id": "c"}, {"id": "b", "source_message_id": "m"},
                             {"id": "c"}])
    assert levels == {"a": "local", "b": "local", "c": "full"}


def test_the_light_turns_yellow_after_failed_days_in_a_row(tmp_path, monkeypatch):
    import status_light as sl
    d = tmp_path / "private"
    (d / "remote-llm").mkdir(parents=True, mode=0o700)
    d.chmod(0o700)
    path = d / "remote-llm" / br.STATE
    for day, status in (("2026-10-12", "failed"), ("2026-10-13", "failed"), ("2026-10-14", "failed"),
                        ("2026-10-15", "failed")):
        br.note_day(path, {"ts": f"{day}T18:00:00+00:00", "status": status, "reason": "limit"})
    state = json.loads(path.read_text())
    assert br.failing_days(state, today=TODAY) == (4, state["days"]["2026-10-15"])
    br.note_day(path, {"ts": "2026-10-15T20:00:00+00:00", "status": "ok"})
    assert br.failing_days(json.loads(path.read_text()), today=TODAY)[0] == 0
    assert br.failing_days(json.loads(path.read_text()), today=date(2026, 10, 16))[0] == 0

    ctx = sl.Ctx(now=datetime(2026, 10, 15, 22, tzinfo=timezone.utc).timestamp(), private=d,
                 logs=tmp_path, sentinel_dir=tmp_path, boundary_file=tmp_path / "b.json",
                 ea=tmp_path, live=tmp_path)
    assert sl.src_review(ctx) == []
    path.unlink()
    for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
        br.note_day(path, {"ts": f"{day}T18:00:00+00:00", "status": "failed", "reason": "timeout"})
    (cond,) = sl.src_review(ctx)
    assert cond.id == "review" and cond.level == "warn" and cond.title == "Briefing not checked"
    assert cond.line == "For 3 days, Tacitus could not check the briefing before it went out."


def test_a_morning_run_reviews_before_staging_and_queues_no_dropped_hand_off(priv, monkeypatch):
    """remote_briefing.run with the check on: the writer runs, the check
    holds one reply, only the other is staged, and a hand-off the check
    dropped is not queued."""
    b = {"new_needs_response": [
            {"message_id": "m-a", "conversation_id": "c-a", "subject": "Talk on Friday",
             "from": "pat@example.edu", "from_name": "Pat", "body_preview": "Can you give the talk?",
             "remote_record": {"ask": "Confirm the talk."}},
            {"message_id": "m-b", "conversation_id": "c-b", "subject": "Slides", "from": "lee@example.edu",
             "from_name": "Lee", "body_preview": "Send the slides.", "remote_record": {"ask": "Send the slides."}},
            {"message_id": "m-c", "conversation_id": "c-c", "subject": "Paper section",
             "from": "ana@example.edu", "from_name": "Ana", "body_preview": "Write the section.",
             "remote_record": {"ask": "Write the section."}}],
         "open_obligations": [], "delta_summary": {}, "recently_closed": []}
    plan = {"get_done_today": [], "flexes": [], "needs_you": [],
            "drafts": [{"ref": "R1", "reply_must_say": "Yes."}, {"ref": "R2", "reply_must_say": "Here they are."}],
            "escalate": [{"ref": "R3", "why": "a section to write", "task": "write it"}]}
    monkeypatch.setattr(rb, "rail", lambda c, says, **k: "")
    monkeypatch.setattr(rb, "write_reply", lambda c, says, write=None: {"ok": True, "body": says})
    staged, queued = [], []
    monkeypatch.setattr(rb, "stage_reply", lambda c, body, run_id, **k: staged.append(c["message_id"]) or
                        {"ok": True, "draft_id": "d-" + c["message_id"]})

    def review(agent, text, b, **kw):
        assert staged == []                    # nothing is staged before the check
        assert not any(d.get("draft_id") for d in kw["drafts"])
        return {"closed": [], "closed_rows": [], "unsure_rows": [], "parked": [],
                "review": {"status": "ok", "model": "codex", "hold": {"R1": "the choice is his"},
                           "drop": {"R3": {"kind": "not-owners", "why": "Ana writes it"}},
                           "footer": ["", ":mag: *Checked before sending* by Codex."]}}
    chat = lambda messages, **kw: (json.loads(json.dumps(plan)), {})  # noqa: E731
    text, rec = rb.run("morning-briefing", b, run_id="cron-morning-briefing-x", now=NOW, chat=chat,
                       review=review, queue=lambda items, agent, now: queued.extend(items) or len(items),
                       rooms=lambda *a, **k: [], mark=lambda *a, **k: True)
    assert staged == ["m-b"] and queued == []
    assert "*Drafts ready* (1)" in text and "I held the reply I wrote: the choice is his." in text
    assert "Sent to Claude" not in text and ":mag: *Checked before sending* by Codex." in text
    assert rec["review"]["held"] == 1 and rec["review"]["staged"] == 1


def test_the_weekly_audit_takes_the_review(priv, tmp_path):
    """The weekly lane: a dropped row leaves both lists, a move crosses
    from important to critical, a rewrite becomes the item's why, and
    the footer closes the post."""
    import remote_audit as ra
    rows = [{"id": "row-a", "description": "Sponsor report", "status": "open", "deadline": "2026-10-20",
             "type": "deadline"},
            {"id": "row-b", "description": "Co-authors' edits", "status": "open", "deadline": "2026-10-17",
             "type": "deadline"},
            {"id": "row-c", "description": "Lab visit reply", "status": "snoozed", "type": "response_needed"}]
    plan = {"critical": [{"ref": "L1", "why": "check in"}, {"ref": "L2", "why": "due soon"}],
            "important": [{"ref": "L3", "why": "a reply"}], "patterns": [], "handoffs": []}
    seen = {}

    def review(text, b, cands, plan, **kw):
        seen["refs"] = sorted(cands)
        return {"closed": [], "parked": [], "review": {
            "status": "ok", "model": "claude", "drop": {"L1": {"kind": "not-owners", "why": "x"}},
            "move": {"L3": "critical"}, "rewrite": {"L2": "Send the report by Oct 20."},
            "footer": ["", ":mag: *Checked before sending* by Claude."]}}
    text, rec = ra.run({}, run_id="r", now=NOW, rows=rows, closed=[], closure={}, review=review,
                       chat=lambda messages, **kw: (json.loads(json.dumps(plan)), {}),
                       queue=lambda *a, **k: 0)
    crit = text.split("🔴")[1].split("🟡")[0]
    assert "Co-authors' edits" not in text.split("🟢")[0]
    assert "Sponsor report" in crit and "Send the report by Oct 20." in crit and "Lab visit reply" in crit
    assert text.rstrip().endswith(":mag: *Checked before sending* by Claude.")
    assert rec["review"]["dropped"] == 1 and rec["review"]["moved"] == 1
    # the weekly list keeps snoozed rows: code never drops them there
    items = [{"ref": "L3", "section": "important", "sections": ["important"], "line": "x",
              "ledger_id": "row-c"}]
    led = {"rows": {"row-c": dict(rows[2])}, "events": {}, "by_conv": {}, "naming": {}}
    assert br.hard_checks(items, led, today=TODAY, keep_parked=True) == {}
    assert br.hard_checks(items, led, today=TODAY)["L3"]["kind"] == "snoozed"


def test_a_review_that_raises_never_stops_the_briefing(priv, monkeypatch):
    """A bug in the check costs the check, not the briefing: the
    still-open audit runs and every reply the run wrote is staged."""
    b = {"new_needs_response": [
            {"message_id": "m-b", "conversation_id": "c-b", "subject": "Slides", "from": "lee@example.edu",
             "from_name": "Lee", "body_preview": "Send the slides.", "remote_record": {"ask": "Send the slides."}}],
         "open_obligations": [], "delta_summary": {}, "recently_closed": []}
    plan = {"get_done_today": [], "flexes": [], "needs_you": [], "escalate": [],
            "drafts": [{"ref": "R1", "reply_must_say": "Here they are."}]}
    monkeypatch.setattr(rb, "rail", lambda c, says, **k: "")
    monkeypatch.setattr(rb, "write_reply", lambda c, says, write=None: {"ok": True, "body": says})
    staged = []
    monkeypatch.setattr(rb, "stage_reply", lambda c, body, run_id, **k: staged.append(c["message_id"]) or
                        {"ok": True, "draft_id": "d-1"})
    monkeypatch.setattr(rb, "audit_post", lambda *a, **k: {"closed": [], "parked": [], "asked": 1})

    def broken(*a, **k):
        raise KeyError("a bug")
    text, rec = rb.run("morning-briefing", b, run_id="cron-x", now=NOW, review=broken,
                       chat=lambda messages, **kw: (json.loads(json.dumps(plan)), {}),
                       rooms=lambda *a, **k: [], mark=lambda *a, **k: True)
    assert staged == ["m-b"] and "*Drafts ready* (1)" in text
    assert rec["review"]["status"] == "off" and rec["audit"]["asked"] == 1
