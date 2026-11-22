"""Letters someone asked the owner to write (preprocessor/letters.py): one
case per person, each destination tracked to its submission.

Pinned here:
  1. A reply in a letter's thread never closes the row and never raises a
     "looks done" card: the owner's "yes, I will write it" is not the
     letter (the request row of a person's own ask, and a nomination row
     the hill climb promoted a reply on).
  2. The Replies card, the reply queue and the inbox scan leave letter rows
     out, and the card's "Not needed" cannot close one.
  3. One person, many destinations: the person's own mail and each
     portal's request join one case; a portal's confirmation submits its
     destination; the case closes itself when all are submitted.
  4. A decline closes the case and its rows; a name that may be someone
     else asks once and the answer is remembered; merge folds two cases.
  5. Detection reads the common shapes of request mail, for any owner (a
     job reference, a tenant reference, an award nomination) and leaves
     out calls for nominations, manuscript decisions and advice asked for.
  6. No scheduled lane drafts an email reply to a letter request.
  7. A kept case names no one where a hosted model reads it.
  8. The migration is a dry run unless --apply, and its public report
     names no one.
All names, addresses and places are made up.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import closure  # noqa: E402
import draft_gate  # noqa: E402
import ledger  # noqa: E402
import letters  # noqa: E402
import mail_privacy  # noqa: E402

NOW = "2026-11-12T00:00:00Z"
NO_PLUGINS = ([], [])


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            c.execute(m)
        except sqlite3.OperationalError:
            pass
    letters.ensure_schema(c)
    closure.ensure_schema(c)
    yield c
    c.close()


def _add(c, rid, *, type_="task", conv="", desc="", status="open", meta=None, sender="",
         notes="", deadline=None):
    c.execute(
        "INSERT INTO ledger (id, type, status, description, from_email, source_message_id, "
        "conversation_id, date_identified, deadline, last_action, notes, metadata, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, type_, status, desc or f"desc {rid}", sender, f"msg-{rid}", conv, NOW, deadline, "",
         notes, json.dumps(meta or {}), NOW, NOW))


def _row(c, rid) -> dict:
    return ledger._row_to_dict(c.execute("SELECT * FROM ledger WHERE id = ?", (rid,)).fetchone())


def _events(c, rid) -> list[tuple]:
    return [(e["kind"], e["signal"]) for e in c.execute(
        "SELECT kind, signal FROM ledger_events WHERE entry_id = ? ORDER BY seq", (rid,))]


def _portal(mid, person, place, *, sender="gradapply@northfield.example", due="", link=True,
            conv=""):
    return {"id": mid, "conversation_id": conv or f"conv-{mid}", "received": NOW,
            "subject": f"Recommendation Request from {person} for {place}", "from": sender,
            "result": {"ask": f"Submit a letter of recommendation for {person} to {place} via the "
                              f"secure link.",
                       "summary": "", "confidential_kind": "letter", "deadline": due,
                       "links": [f"https://apply.{place.split()[0].lower()}.example/apply/refer?key={mid}"]
                       if link else []}}


def _own_ask(mid, sender, name, *, attachments=(), conv=""):
    return {"id": mid, "conversation_id": conv or f"conv-{mid}", "received": NOW,
            "subject": "Recommendation letter request", "from": sender, "from_name": name,
            "result": {"ask": f"Decide whether to write a recommendation letter for {name}.",
                       "summary": "", "confidential_kind": "letter", "deadline": "",
                       "attachments": [{"name": a} for a in attachments]}}


def _ingest(c, rec, ledger_id=""):
    return letters.ingest_record(c, rec, ledger_id=ledger_id,
                                 found=letters.detect(rec, plugin_senders=NO_PLUGINS))


# ── 1. a reply never closes or proposes ───────────────────────────────

def test_the_owners_yes_reply_raises_no_close_card_on_a_persons_letter_request(conn):
    """A person asks for a letter; the owner replies that he will write it.
    Before letter cases, the reply in the thread proposed closing the row
    twice (the send and the scrubber's Outlook reply), and the owner had
    to tap Keep open."""
    _add(conn, "task-ask1", conv="conv-ask1", sender="riley.chen@uni.example",
         notes="Ask: Decide whether to write a PhD recommendation letter for Riley Chen.")
    _ingest(conn, _own_ask("m-ask1", "riley.chen@uni.example", "Riley Chen",
                           attachments=("Riley_Chen_CV.pdf", "Statement_of_Purpose.docx"),
                           conv="conv-ask1"), ledger_id="task-ask1")
    row = _row(conn, "task-ask1")
    assert letters.is_letter_row(row) and letters.letter_role(row) == "case"
    sent = "2026-11-22T17:28:48Z"
    for signal, actor in (("reply_in_thread", "cli:owner"), ("user_replied_outlook", "closure_scrubber")):
        res = closure.propose_or_promote(conn, "task-ask1", signal=signal, actor=actor,
                                         note="you replied in its thread", evidence_ts=sent)
        assert res["action"] == "skipped"
        res = closure.settle(conn, "task-ask1", signal=signal, actor=actor, note="replied",
                             evidence_ts=sent)
        assert res["action"] == "skipped"
    row = _row(conn, "task-ask1")
    assert row["status"] == "open" and "close_proposal" not in row
    assert not [e for e in _events(conn, "task-ask1") if e[0] in ("propose", "close")]
    # The decision is one question with three answers, naming what came with it.
    q = letters.questions(conn)[0]
    assert q["kind"] == "write" and q["choices"] == list(letters.WRITE_CHOICES)
    assert "a CV" in q["text"] and "a statement" in q["text"]


def test_a_promoted_reply_signal_cannot_close_a_nomination_letter_row(conn, monkeypatch):
    """The hill climb had promoted the Outlook-reply signal: a nomination
    letter row closed the minute the owner wrote "I will write it"."""
    import closure_policy
    monkeypatch.setattr(closure_policy, "state", lambda signal: closure.PROMOTED)
    _add(conn, "nomination-letter-row", conv="conv-nom", meta={"closes_on": "submission"},
         desc="Write and submit a nomination letter")
    cid = letters.ensure_case(conn, "Dana Okafor", "dana.okafor@uni.example", "nomination")
    letters.add_destination(conn, cid, "Young Researchers Forum", "", "unknown", "",
                            "nomination-letter-row")
    res = closure.settle(conn, "nomination-letter-row", signal="user_replied_outlook",
                         actor="closure_scrubber", note="replied", evidence_ts=NOW)
    assert res["action"] == "skipped"
    assert _row(conn, "nomination-letter-row")["status"] == "open"
    assert closure.hill_move("user_replied_outlook", _row(conn, "nomination-letter-row"),
                             "closure_scrubber", closure.PROPOSAL) == ""


def test_archive_lapse_and_model_signals_do_nothing_to_a_letter_row(conn):
    _add(conn, "task-p1", conv="conv-p1")
    _ingest(conn, _portal("m-p1", "Jamie Rivera", "Northfield University", conv="conv-p1"),
            ledger_id="task-p1")
    for signal in ("user_archived", "lapsed", "date_passed", "llm:reconcile", "detector_sibling",
                   "inbound_ack:submission-ack"):
        assert closure.settle(conn, "task-p1", signal=signal, actor="x", note="n")["action"] == "skipped"
    assert closure.propose(conn, "task-p1", signal="llm:reconcile-duplicate", actor="reconcile",
                           note="dup")["action"] == "skipped"
    assert _row(conn, "task-p1")["status"] == "open"


def test_a_reply_that_closes_a_reply_row_does_not_sweep_a_letter_row_on_the_thread(conn):
    _add(conn, "response-abc", type_="response_needed", conv="conv-shared")
    _add(conn, "priority-letter", type_="response_needed", conv="conv-shared")
    cid = letters.ensure_case(conn, "Jamie Rivera")
    letters.add_destination(conn, cid, "Northfield University", ledger_id="priority-letter")
    res = closure.settle(conn, "response-abc", signal="user_replied_outlook", actor="closure_scrubber",
                         note="replied", evidence_ts="2026-11-20T00:00:00Z")
    assert res["action"] == "closed" and res["ids"] == ["response-abc"]
    assert _row(conn, "priority-letter")["status"] == "open"


def test_a_linked_send_closes_a_letter_only_when_it_goes_by_email(conn):
    _add(conn, "task-portal", conv="c1")
    _add(conn, "task-mail", conv="c2")
    cid = letters.ensure_case(conn, "Jamie Rivera")
    portal = letters.add_destination(conn, cid, "Northfield University", method="portal",
                                     ledger_id="task-portal")
    mailed = letters.add_destination(conn, cid, "Eastbrook Fellowship", method="email",
                                     link="mailto:fellowships@eastbrook.example", ledger_id="task-mail")
    assert letters.destination(conn, portal)["method"] == "portal"
    entry = _row(conn, "task-portal")
    # letter_signal_allowed reads the destination's method from the live
    # store; the rule itself, with the method given:
    assert closure.tier("user_stated", entry) == closure.DECISIVE
    assert letters.letter_signal_allowed("user_handled", entry,
                                         note=letters.REPLY_CARD_NOTE) is False
    assert letters.letter_signal_allowed("user_handled", entry,
                                         note=letters.DRAFT_HANDLED_NOTE) is False
    assert letters.letter_signal_allowed("user_handled", entry, note="done, he says") is True
    assert mailed


# ── 2. the reply queue and the Replies card ───────────────────────────

def test_letter_rows_are_no_reply_owed_and_not_needed_cannot_close_them(conn):
    _add(conn, "task-k1", conv="conv-k1", type_="response_needed")
    conn.execute("UPDATE ledger SET source_message_id = 'm-k1' WHERE id = 'task-k1'")
    brief = {"needs_reply": True, "state": "needs-reply",
             "ask": "Decide whether to write a recommendation letter for Kai Moreno and reply.",
             "messages": [{"id": "m-k1", "own": False, "received": "2026-11-10T12:00:00Z"}]}
    meta = {"thread_brief": brief}
    conn.execute("UPDATE ledger SET metadata = ? WHERE id = 'task-k1'", (json.dumps(meta),))
    before = _row(conn, "task-k1")
    assert ledger.reply_owed(before), "the old row reads as a reply owed"
    _ingest(conn, _own_ask("m-k1", "kai.moreno@uni.example", "Kai Moreno", conv="conv-k1"),
            ledger_id="task-k1")
    row = _row(conn, "task-k1")
    assert not ledger.reply_owed(row) and not ledger.closes_on_reply(row)
    assert ledger.reply_backlog([row], date(2026, 11, 22)) == []
    res = closure.settle(conn, "task-k1", signal="user_handled", actor="cli:owner",
                         note=letters.REPLY_CARD_NOTE)
    assert res["action"] == "skipped" and _row(conn, "task-k1")["status"] == "open"
    # The owner's own word still closes it.
    res = closure.settle(conn, "task-k1", signal="user_stated", actor="cli:owner",
                         note="done, per the owner", evidence={"quote": "I sent that letter"})
    assert res["action"] == "closed"


def test_a_letter_row_never_lapses():
    row = {"id": "deadline-x", "type": "deadline", "closes_on": "lapse",
           "letter": {"case": "lc-1", "destination": "ld-1", "role": "destination"}}
    assert not ledger.lapses(row)


# ── 3. one person, many destinations ──────────────────────────────────

def test_a_persons_mail_and_each_portals_request_join_one_case(conn):
    _add(conn, "task-own", conv="conv-own", sender="sam.ortiz@uni.example")
    own = _own_ask("m-own", "sam.ortiz@uni.example", "Sam Ortiz", attachments=("Sam_Ortiz_CV.pdf",),
                   conv="conv-own")
    own["result"]["letter"] = {"role": "request", "person": "Sam Ortiz", "kind": "recommendation",
                               "destination": "", "due": "", "method": "unknown",
                               "destinations": [{"name": "Northfield University", "due": "2026-12-01"},
                                                {"name": "Lakeside Institute", "due": "2026-12-15"},
                                                {"name": "Westmoor College", "due": "2026-12-15"}]}
    first = _ingest(conn, own, ledger_id="task-own")
    cid = first["case"]
    assert first["action"] == "case"
    # Portal mails name the person the portal's way and state no date.
    _add(conn, "task-n", conv="conv-n")
    _add(conn, "task-l", conv="conv-l")
    a = _ingest(conn, _portal("m-n", "Sam Ortiz", "Northfield University", conv="conv-n"),
                ledger_id="task-n")
    b = _ingest(conn, _portal("m-l", "SAM ORTIZ".title(), "Lakeside Institute",
                              sender="admissions@lakeside.example", conv="conv-l"), ledger_id="task-l")
    assert a["case"] == cid and b["case"] == cid
    c = letters.case(conn, cid)
    assert len(c["destinations"]) == 3
    by = {d["name"]: d for d in c["destinations"]}
    assert by["Northfield University"]["ledger_id"] == "task-n"
    assert by["Northfield University"]["due"] == "2026-12-01", "the person's list fills the date"
    assert by["Northfield University"]["method"] == "portal" and by["Northfield University"]["link"]
    assert _row(conn, "task-n")["deadline"] == "2026-12-01"
    assert _row(conn, "task-n")["closes_on"] == "submission"
    assert c["materials"][0]["kind"] == "cv"
    letters.accept(conn, cid, actor="owner")
    line = letters.summary_line(letters.case(conn, cid), today=date(2026, 11, 22))
    assert line == "Letter for Sam Ortiz: 0 of 3 submitted, next due Dec 1."


def test_a_confirmation_submits_its_destination_and_the_last_one_closes_the_case(conn):
    _add(conn, "task-own", conv="conv-own", sender="sam.ortiz@uni.example")
    cid = _ingest(conn, _own_ask("m-own", "sam.ortiz@uni.example", "Sam Ortiz", conv="conv-own"),
                  ledger_id="task-own")["case"]
    _add(conn, "task-n", conv="conv-n")
    _ingest(conn, _portal("m-n", "Sam Ortiz", "Northfield University", conv="conv-n"),
            ledger_id="task-n")
    conf = {"id": "m-c", "subject": "Thank you for submitting a recommendation",
            "from": "no-reply@apply.northfield.example",
            "result": {"ask": "", "summary": "Your recommendation for Sam Ortiz has been received.",
                       "confidential_kind": "letter"}}
    found = letters.detect(conf, plugin_senders=NO_PLUGINS)
    assert found["role"] == "confirmation" and found["person"] == "Sam Ortiz"
    res = letters.ingest_record(conn, conf, found=found)
    assert res["action"] == "submitted" and res["case_closed"] is True
    assert _row(conn, "task-n")["status"] == "completed"
    assert _row(conn, "task-own")["status"] == "completed", "the case row closes with the case"
    assert ("close", letters.SUBMITTED_SIGNAL) in _events(conn, "task-n")
    c = letters.case(conn, cid)
    assert c["status"] == "closed" and c["decision"] == "accepted"
    assert letters.summary_line(c) == "Letter for Sam Ortiz: all 1 submitted."


def test_a_thank_you_in_the_persons_thread_is_no_submission(conn):
    rec = {"subject": "Re: Recommendation letter request", "from": "sam.ortiz@uni.example",
           "result": {"ask": "", "summary": "Sam thanks you for submitting the letter to Northfield.",
                      "confidential_kind": "letter"}}
    assert letters.detect(rec, plugin_senders=NO_PLUGINS) is None


def test_record_submitted_from_the_cli_path_closes_the_row(conn):
    _add(conn, "task-n", conv="conv-n")
    cid = letters.ensure_case(conn, "Sam Ortiz")
    did = letters.add_destination(conn, cid, "Northfield University", ledger_id="task-n")
    letters.record_draft(conn, cid, "/letters/sam-ortiz/letter.pdf")
    assert letters.destination(conn, did)["state"] == "drafted"
    letters.set_state(conn, did, "ready", actor="owner")
    out = letters.set_state(conn, did, "submitted", "upload tool said OK", actor="cli:chat")
    assert out["closed"] == ["task-n"] and _row(conn, "task-n")["status"] == "completed"


# ── 4. decline, join questions, merge ─────────────────────────────────

def test_no_closes_the_case_and_every_row_on_the_owners_decision(conn):
    _add(conn, "task-own", conv="conv-own", sender="sam.ortiz@uni.example")
    cid = _ingest(conn, _own_ask("m-own", "sam.ortiz@uni.example", "Sam Ortiz", conv="conv-own"),
                  ledger_id="task-own")["case"]
    _add(conn, "task-n", conv="conv-n")
    _ingest(conn, _portal("m-n", "Sam Ortiz", "Northfield University", conv="conv-n"),
            ledger_id="task-n")
    q = letters.questions(conn)[0]
    out = letters.answer(conn, q["id"], "No", actor="owner")
    assert out["decision"] == "declined"
    assert {_row(conn, r)["status"] for r in ("task-own", "task-n")} == {"completed"}
    assert letters.case(conn, cid)["status"] == "declined"
    assert ("close", letters.DECLINED_SIGNAL) in _events(conn, "task-n")


def test_yes_ask_for_materials_accepts_and_names_the_next_step(conn):
    cid = letters.ensure_case(conn, "Sam Ortiz", "sam.ortiz@uni.example")
    qid = letters.ask_write(conn, cid)
    out = letters.answer(conn, qid, "Yes, ask for materials")
    assert out["decision"] == "accepted" and out["next"] == "ask_materials"
    assert letters.questions(conn) == []


def test_a_name_that_may_be_someone_else_asks_once_and_remembers(conn):
    _add(conn, "task-a", conv="c-a")
    _add(conn, "task-b", conv="c-b")
    first = _ingest(conn, _portal("m-a", "Alexandra Brooks", "Northfield University", conv="c-a"),
                    ledger_id="task-a")
    second = _ingest(conn, _portal("m-b", "Alex Brooks", "Lakeside Institute",
                                   sender="admissions@lakeside.example", conv="c-b"), ledger_id="task-b")
    assert second["action"] == "ask_join" and second["case"] != first["case"]
    join = [q for q in letters.questions(conn) if q["kind"] == "join"][0]
    letters.answer(conn, join["id"], letters.JOIN_OTHER)
    # The same pair never asks again.
    assert letters.find_case(conn, "Alex Brooks") == {"case": second["case"]}
    _add(conn, "task-c", conv="c-c")
    third = _ingest(conn, _portal("m-c", "Alex Brooks", "Westmoor College",
                                  sender="apply@westmoor.example", conv="c-c"), ledger_id="task-c")
    assert third["case"] == second["case"] and not third.get("question")


def test_same_person_merges_and_the_answer_holds_for_the_next_mail(conn):
    _add(conn, "task-a", conv="c-a")
    _add(conn, "task-b", conv="c-b")
    first = _ingest(conn, _portal("m-a", "Alexandra Brooks", "Northfield University", conv="c-a"),
                    ledger_id="task-a")
    second = _ingest(conn, _portal("m-b", "A. Brooks", "Lakeside Institute",
                                   sender="admissions@lakeside.example", conv="c-b"), ledger_id="task-b")
    join = [q for q in letters.questions(conn) if q["kind"] == "join"][0]
    letters.answer(conn, join["id"], letters.JOIN_SAME)
    c = letters.case(conn, first["case"])
    assert len(c["destinations"]) == 2 and "A Brooks" in c["aliases"]
    assert _row(conn, "task-b")["letter"]["case"] == first["case"]
    assert letters.case(conn, second["case"])["status"] == "merged"
    assert letters.find_case(conn, "A. Brooks") == {"case": first["case"]}


@pytest.mark.parametrize("a,b,verdict", [
    ("Ana Ruiz", "ana ruiz", "same"), ("Ana M. Ruiz", "Ana Ruiz", "same"),
    ("Ruiz, Ana", "Ana Ruiz", "same"), ("Mia Zavala-Ortiz", "Mia Zavala Ortiz", "same"),
    ("Alex Ruiz", "Alexandra Ruiz", "maybe"), ("A. Ruiz", "Ana Ruiz", "maybe"),
    ("Ana Ruiz", "Ben Ruiz", "no"), ("Ana", "Ana Ruiz", "no"),
])
def test_name_match(a, b, verdict):
    assert letters.name_match(a, b) == verdict


# ── 5. detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,ask,sender,kind,person", [
    # A portal's automated request on a person's behalf.
    ("Recommendation Request from Jamie Rivera for Northfield University",
     "Submit a letter of recommendation for Jamie Rivera via the secure link.",
     "noreply@apply.northfield.example", "recommendation", "Jamie Rivera"),
    # A hiring manager asks for a job reference: no academia anywhere.
    ("Reference request for Jordan Lee",
     "Provide a reference for Jordan Lee, who applied for the operations manager role.",
     "hiring@brightline.example", "reference", "Jordan Lee"),
    # A landlord's tenant reference.
    ("Tenant reference", "Complete the reference form for your former tenant Casey Park.",
     "leasing@maplecourt.example", "reference", "Casey Park"),
    # An award nomination letter for a colleague.
    ("Nomination letter for the Community Service Award",
     "Write a nomination letter for Robin Hale for the Community Service Award by March 3.",
     "awards@civicfund.example", "nomination", "Robin Hale"),
    # A reference service: the person listed the owner.
    ("You have been listed as a reference",
     "Pat Morgan has listed you as a reference; complete the online questionnaire.",
     "no-reply@refcheck.example", "reference", "Pat Morgan"),
])
def test_detect_reads_the_common_request_shapes(subject, ask, sender, kind, person):
    found = letters.detect({"subject": subject, "from": sender, "result": {"ask": ask, "summary": ""}},
                           plugin_senders=NO_PLUGINS)
    assert found and found["role"] in ("request", "reminder")
    assert found["kind"] == kind and found["person"] == person


@pytest.mark.parametrize("subject,ask,kind", [
    ("Call for nominations: Teaching Award", "Nominate a colleague for the teaching award by Friday.",
     "none"),
    ("Manuscript ID RX-26-001", "Review the manuscript and submit a recommendation to the editor.",
     "manuscript-review"),
    ("robot question", "Provide recommendations for commercial outdoor robots.", "none"),
    ("Offer letter for Spring", "Review and sign the offer letter for Spring 2027.", "personnel"),
    ("Re: Search check-in", "Confirm that all letters of recommendation should be initiated in the "
                            "hiring system.", "personnel"),
    ("Letters of support for the new degree program",
     "Collect letters of support for the new degree program from industry partners.", "none"),
])
def test_detect_leaves_out_mail_that_asks_for_no_letter(subject, ask, kind):
    rec = {"subject": subject, "from": "someone@uni.example",
           "result": {"ask": ask, "summary": "", "confidential_kind": kind}}
    assert letters.detect(rec, plugin_senders=NO_PLUGINS) is None


def test_the_owners_own_mail_is_never_a_request():
    rec = {"subject": "Re: Recommendation letter request", "from": "me@uni.example", "own": True,
           "result": {"ask": "Submit a letter of recommendation for Sam Ortiz.", "summary": ""}}
    assert letters.detect(rec, plugin_senders=NO_PLUGINS) is None


def test_the_local_models_letter_field_decides_and_rules_fill_the_link():
    rec = {"subject": "Following up", "from": "help@letters.example",
           "result": {"ask": "Please do the thing in the link.", "summary": "",
                      "links": ["https://safe.example/?url=https%3A%2F%2Fwww.letters.example%2Flor%3Ftoken%3Dabc"],
                      "letter": {"role": "reminder", "person": "Quinn Avery", "kind": "reference",
                                 "destination": "Harbor Fellowship", "due": "2026-12-01",
                                 "method": "unknown", "destinations": []}}}
    found = letters.detect(rec, plugin_senders=NO_PLUGINS)
    assert found["source"] == "model" and found["role"] == "reminder"
    assert found["person"] == "Quinn Avery" and found["due"] == "2026-12-01"
    assert found["method"] == "portal" and found["link"] == "https://www.letters.example/lor?token=abc"
    none = dict(rec, result=dict(rec["result"], letter=dict(mail_privacy.NO_LETTER),
                                 ask="Submit a letter of recommendation for Quinn Avery."))
    assert letters.detect(none, plugin_senders=NO_PLUGINS) is None, "the model said none"


def test_a_plugin_adds_a_service_core_does_not_know():
    rec = {"subject": "Action needed", "from": "robot@obscure-refs.example",
           "result": {"ask": "Finish your part for Lane Brooks.", "summary": ""}}
    assert letters.detect(rec, plugin_senders=NO_PLUGINS) is None
    found = letters.detect(rec, plugin_senders=(["@obscure-refs.example"], []))
    assert found["role"] == "request" and found["source"] == "plugin"


def test_the_extraction_schema_carries_a_letter_field():
    assert "letter" in mail_privacy.SCHEMA["required"]
    rec = mail_privacy.validate({"state": "todo", "letter": {
        "role": "request", "person": " Quinn Avery ", "kind": "odd", "destination": "Harbor",
        "due": "soon", "method": "fax", "destinations": [{"name": "A", "due": "2026-12-01"}, "x"]}})
    assert rec["letter"] == {"role": "request", "person": "Quinn Avery", "kind": "other",
                             "destination": "Harbor", "due": "", "method": "unknown",
                             "destinations": [{"name": "A", "due": "2026-12-01"}]}
    assert rec["needs_reply"] is False and rec["response_mode"] == "submission"
    assert mail_privacy.validate({"state": "fyi"})["letter"] == mail_privacy.NO_LETTER


def test_a_portals_automated_request_gets_a_row_of_its_own(conn):
    """The needs-response detector drops no-reply senders, so before letter
    cases most portal requests had no row at all."""
    out = _ingest(conn, _portal("m-x", "Jamie Rivera", "Northfield University",
                                sender="lor-noreply@vendor.example"))
    d = letters.destination(conn, out["destination"])
    row = _row(conn, d["ledger_id"])
    assert row["status"] == "open" and row["type"] == "task" and row["closes_on"] == "submission"
    assert row["description"] == "Letter for Jamie Rivera: Northfield University"


def test_one_service_for_many_places_keeps_its_destinations_apart(conn):
    cid = letters.ensure_case(conn, "Jamie Rivera")
    a = letters.add_destination(conn, cid, "", link="https://vendor.example/lor?token=1",
                                sender="lor-noreply@vendor.example")
    b = letters.add_destination(conn, cid, "Northfield University",
                                link="https://vendor.example/lor?token=2",
                                sender="lor-noreply@vendor.example")
    assert a != b and len(letters.case(conn, cid)["destinations"]) == 2


# ── 6. the draft gate ─────────────────────────────────────────────────

def test_no_scheduled_lane_drafts_a_reply_to_a_letter_request(conn):
    row = {"id": "task-k1", "type": "task", "conversation_id": "conv-k1",
           "letter": {"case": "lc-1", "destination": "", "role": "case"}}
    why = draft_gate.cron_refusal(run_id="cron-morning-1", rows=[row], decisions=[], keep_records=[])
    assert why == draft_gate.LETTER_REFUSAL
    assert draft_gate.cron_refusal(run_id="chat-1", rows=[row], decisions=[]) == ""
    held = draft_gate.check({"title": "PhD recommendation letter request",
                             "ask": "Decide whether to write a recommendation letter for Kai Moreno.",
                             "brief": {}})
    assert held["rule"] == "letter" and held["choices"] == []
    # A record whose letter field says request is held whatever its words.
    held = draft_gate.check({"title": "Hello", "ask": "See below.",
                             "remote_record": {"letter": {"role": "request", "person": "Kai Moreno"}}})
    assert held["rule"] == "letter"


def test_the_remote_briefing_keeps_the_letter_rule_whole():
    import remote_briefing
    found = remote_briefing.gate({"title": "Recommendation letter request",
                                  "ask": "Write and submit a letter of recommendation for Kai Moreno.",
                                  "kind": "mail"}, "Yes, I will write it.")
    assert found["rule"] == "letter" and found["line"] == draft_gate.LETTER_LINE


# ── 7. keep levels ────────────────────────────────────────────────────

def test_a_kept_case_names_no_one_where_a_hosted_model_reads(conn):
    out = letters.ingest_record(
        conn, _portal("m-k", "Jamie Rivera", "Northfield University"), keep_level="local",
        found=letters.detect(_portal("m-k", "Jamie Rivera", "Northfield University"),
                             plugin_senders=NO_PLUGINS))
    c = letters.case(conn, out["case"])
    assert c["keep_level"] == "local"
    line = letters.summary_line(c)
    assert "Jamie" not in line and line.startswith("Letter (kept on your own model)")
    v = letters.view(c)
    assert v["person_name"] == "" and all("name" not in d and "link" not in d for d in v["destinations"])
    row = _row(conn, c["destinations"][0]["ledger_id"])
    assert "Jamie" not in row["description"] and row["keep_level"] == "local"


# ── 8. migration ──────────────────────────────────────────────────────

def test_the_migration_plans_cases_and_its_public_report_names_no_one(conn):
    _add(conn, "task-AbCdEf12", conv="c1", sender="gradapply@northfield.example",
         desc="Task: Letter of Recommendation Request",
         notes="[confidential: letter] Ask: Submit a letter of recommendation for Jamie Rivera to "
               "Northfield University via the secure link.")
    _add(conn, "task-GhIjKl34", conv="c2", sender="admissions@lakeside.example",
         desc="Task: Recommendation request for Jamie Rivera",
         notes="Ask: Write and submit a letter of recommendation for Jamie Rivera to Lakeside "
               "Institute via the provided link.")
    _add(conn, "jamie-rivera-letter-row", conv="c3", sender="jamie.rivera@uni.example",
         desc="Write the letter for Jamie Rivera", status="completed",
         notes="Ask: Decide whether to write a recommendation letter for Jamie Rivera.")
    conn.execute("UPDATE ledger SET closed_at = ?, metadata = ? WHERE id = 'jamie-rivera-letter-row'",
                 ("2026-11-20T00:00:00Z", json.dumps({"closed_by": {"signal": "user_replied_outlook"}})))
    plan = letters.migration_plan(conn, days=30, today=date(2026, 11, 22))
    assert len(plan["cases"]) == 1 and plan["rows"] == 3
    report = letters.plan_report(plan)
    assert "Jamie" not in report and "Rivera" not in report
    assert "task-AbCdEf12" in report and "jamie-rivera-letter-row" not in report
    assert not conn.execute("SELECT COUNT(*) FROM letter_cases").fetchone()[0], "a dry run writes nothing"
    counts = letters.apply_migration(conn, plan)
    assert counts["cases"] == 1 and counts["destinations"] == 2
    assert letters.is_letter_row(_row(conn, "task-AbCdEf12"))


# ── intake ────────────────────────────────────────────────────────────

def test_intake_joins_letter_mail_once_and_gives_portal_mail_a_row(tmp_path, monkeypatch):
    import preprocess
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.db")
    db = ledger._get_db()
    db.execute("INSERT INTO ledger (id, type, status, description, source_message_id, "
               "conversation_id, date_identified, last_action, notes, metadata, created_at, "
               "updated_at) VALUES ('task-own1', 'task', 'open', 'Task: letter request', 'm-own', "
               "'conv-own', ?, '', '', '{}', ?, ?)", (NOW, NOW, NOW))
    db.commit()
    db.close()
    recent = letters._now()
    own = dict(_own_ask("m-own", "sam.ortiz@uni.example", "Sam Ortiz", conv="conv-own"),
               status="ok", received=recent)
    portal = dict(_portal("m-p", "Sam Ortiz", "Northfield University",
                          sender="lor-noreply@vendor.example"), status="ok", received=recent)

    class Gate:
        on = True
        index = {"by_id": {"m-own": own, "m-p": portal}}

        def decision(self, rec):
            return {"level": "full"}

    inbox = [{"id": "m-own", "from": {"emailAddress": {"name": "Sam Ortiz"}}}, {"id": "m-p"}]
    first = preprocess.ingest_letters(Gate(), inbox)
    assert {d["action"] for d in first} == {"case", "destination"}
    again = preprocess.ingest_letters(Gate(), inbox)
    assert again == [], "each message is read once"
    conn = letters.connect()
    try:
        c = letters.cases(conn)[0]
        assert c["case_ledger_id"] == "task-own1" and len(c["destinations"]) == 1
        row = _row(conn, c["destinations"][0]["ledger_id"])
        assert row["status"] == "open" and row["closes_on"] == "submission"
        assert letters.letter_role(_row(conn, "task-own1")) == "case"
    finally:
        conn.close()


def test_old_requests_in_the_inbox_open_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.db")
    old = dict(_portal("m-old", "Sam Ortiz", "Northfield University"), received="2026-01-02T00:00:00Z")
    assert letters.ingest([old]) == []
