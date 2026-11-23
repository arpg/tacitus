"""Work cases (preprocessor/cases.py): one store and one machine for every
kind of work someone asks the owner for, each kind a small declaration
(preprocessor/case_kinds/).

Pinned here:
  1. A kind is a declaration: the new kinds are small files with no code,
     and a plugin declares a kind as manifest data.
  2. A form: a signing service's request opens a case with its own row,
     its reminder joins it, and its "Completed: <title>" notice delivers
     it; a notice for another envelope does nothing.
  3. A review: an invitation with an item id opens a case and its
     reminder joins it; a reply in a thread, a notice to another reviewer
     the owner is copied on and an editor's own work open none. No closes
     the case on the owner's decision.
  4. Closing: a reply, an archive, a lapse and a model never finish a case
     row of any kind; the owner's word, the kind's delivery and a send of
     a part that goes by email do.
  5. A confirmation that matches no part for sure asks once which part,
     remembers the answer, and never guesses.
  6. The letter tables of the first letter release: read as they are and
     never written until the owner's `migrate --apply`, which moves them
     in one transaction, keeps the old tables and is idempotent.
  7. Keep levels, the version digest, the allowed moves, the draft gate,
     the reply queue and the model's work field.
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

import case_kinds  # noqa: E402
import cases  # noqa: E402
import closure  # noqa: E402
import draft_gate  # noqa: E402
import ledger  # noqa: E402
import letters  # noqa: E402
import mail_completion  # noqa: E402
import mail_privacy  # noqa: E402
import plugin_manifest  # noqa: E402

NOW = "2026-11-12T00:00:00Z"
FORMS = ["form"]
REVIEWS = ["review"]


def _ledger_db(c):
    c.row_factory = sqlite3.Row
    c.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            c.execute(m)
        except sqlite3.OperationalError:
            pass
    closure.ensure_schema(c)
    return c


@pytest.fixture
def conn():
    c = _ledger_db(sqlite3.connect(":memory:"))
    cases.ensure_schema(c)
    yield c
    c.close()


def _add(c, rid, *, type_="task", conv="", desc="", status="open", meta=None, sender="",
         source=None):
    c.execute(
        "INSERT INTO ledger (id, type, status, description, from_email, source_message_id, "
        "conversation_id, date_identified, deadline, last_action, notes, metadata, created_at, "
        "updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, type_, status, desc or f"desc {rid}", sender, f"msg-{rid}", conv, NOW, None, "",
         "", json.dumps(meta or {}), NOW, NOW, source))


def _row(c, rid) -> dict:
    return ledger._row_to_dict(c.execute("SELECT * FROM ledger WHERE id = ?", (rid,)).fetchone())


def _events(c, rid) -> list[tuple]:
    return [(e["kind"], e["signal"]) for e in c.execute(
        "SELECT kind, signal FROM ledger_events WHERE entry_id = ? ORDER BY seq", (rid,))]


def _sign(mid, title, *, sender="esign@sign.mapleton.example", subject=None, ask=None, conv=""):
    return {"id": mid, "conversation_id": conv or f"conv-{mid}", "received": NOW,
            "subject": subject if subject is not None else title, "from": sender,
            "result": {"ask": f"Review and sign the {title} via the signing service." if ask is None
                       else ask, "summary": "", "state": "todo" if ask != "" else "fyi",
                       "deadline": "", "links": ["https://sign.mapleton.example/envelope/abc"]}}


def _invite(mid, number, *, subject=None, ask=None, summary="", sender="system@reviews.example",
            conv=""):
    return {"id": mid, "conversation_id": conv or f"conv-{mid}", "received": NOW,
            "subject": subject or f"Invitation to review manuscript ID {number}", "from": sender,
            "result": {"ask": ask or f"Decide whether to accept or decline the invitation to review "
                                     f"manuscript {number}.",
                       "summary": summary, "state": "decide", "deadline": "2026-12-01",
                       "confidential_kind": "manuscript-review"}}


def _ingest(c, rec, kinds_, ledger_id=""):
    found = cases.detect(rec, kinds_=kinds_, plugin_senders=([], []))
    assert found, f"no {kinds_} read in {rec['subject']!r}"
    return cases.ingest_record(c, rec, ledger_id=ledger_id, found=found)


# ── 1. a kind is a declaration ────────────────────────────────────────

def test_the_core_kinds_are_registered_in_order_and_the_new_ones_are_declarations():
    assert list(cases.kinds())[:4] == ["letter", "review", "form", "report"]
    for name in ("review", "form", "report"):
        path = ROOT / "preprocessor" / "case_kinds" / f"{name}.py"
        text = path.read_text()
        assert len(text.splitlines()) <= 40, f"{name}.py is a declaration, not machinery"
        assert "def " not in text and "class " not in text
        k = cases.kind(name)
        assert k.decision and k.guide == f"{name}s.md" and k.rules is not None
    assert cases.kind("letter").delivered == "submitted"
    assert cases.kind("review").delivered == "returned"
    assert cases.kind("report").decision.then == {"Yes, ask for materials": "ask_materials"}


def test_detection_defaults_follow_the_measurements_and_the_owner_can_turn_one_on(tmp_path,
                                                                                  monkeypatch):
    on = {n: cases.detection_on(k) for n, k in cases.kinds().items()}
    assert on["letter"] and on["form"] and not on["review"] and not on["report"]
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"case_detection": {"review": True,
                                                                       "form": False}}))
    monkeypatch.setattr(cases, "EA_DIR", tmp_path)
    assert cases.detection_on(cases.kind("review")) and not cases.detection_on(cases.kind("form"))


def test_a_plugin_declares_a_kind_as_data_and_never_replaces_a_core_kind(monkeypatch):
    raw = {"manifest_version": 2, "name": "visa-papers", "version": "1", "zone": "agent",
           "capabilities": {"case_kinds": {
               "visa": {"words": {"noun": "Visa paperwork", "part": "document"},
                        "decision": {"question": "Will you handle it?", "choices": ["Yes", "No"]},
                        "detect": {"senders": ["@consulate-forms.example"]}, "detect_default": True},
               "letter": {"words": {"noun": "Not a letter"}},
               "x": {"words": {}}}}}
    block = plugin_manifest._norm_case_kinds(raw["capabilities"]["case_kinds"], errs := [])
    assert "visa" in block and any("x" in e for e in errs)
    monkeypatch.setattr(cases, "_KINDS_MEMO", [0.0, None])
    import plugin_registry
    monkeypatch.setattr(plugin_registry, "case_kinds", lambda lock=None: {"visa-papers": block})
    ks = cases.kinds()
    assert ks["visa"].source == "plugin:visa-papers" and ks["visa"].noun == "Visa paperwork"
    assert ks["letter"].source == "core" and ks["letter"].noun == "Letter"
    rec = {"id": "m-v", "subject": "Your file", "from": "office@consulate-forms.example",
           "result": {"ask": "Upload the signed form.", "summary": "", "state": "todo"}}
    assert cases.detect(rec, kinds_=["visa"])["case_kind"] == "visa"
    monkeypatch.setattr(cases, "_KINDS_MEMO", [0.0, None])


# ── 2. a form ─────────────────────────────────────────────────────────

def test_a_signing_request_opens_a_form_its_reminder_joins_and_completed_delivers_it(conn):
    first = _ingest(conn, _sign("m-1", "Thesis Approval Form - Rowan Ellery"), FORMS)
    assert first["action"] == "case"
    c = cases.case(conn, first["case"])
    row = _row(conn, c["parts"][0]["ledger_id"])
    assert row["type"] == "task" and row["closes_on"] == "submission"
    assert row["work_case"]["kind"] == "form" and cases.is_case_row(row)
    again = _ingest(conn, _sign("m-2", "Thesis Approval Form - Rowan Ellery",
                                subject="Reminder: Thesis Approval Form - Rowan Ellery"), FORMS)
    assert again["case"] == first["case"] and again["part"] == first["part"]
    assert len(cases.case(conn, first["case"])["parts"]) == 1
    # A notice about another envelope from the same service does nothing.
    other = _sign("m-3", "x", subject="Completed: Offer Letter - Sky Brandt", ask="")
    found = cases.detect(other, kinds_=FORMS)
    assert found["role"] == "confirmation"
    assert cases.ingest_record(conn, other, found=found)["action"] == "unmatched"
    assert cases.questions(conn, kind="form")[0]["kind"] == "decide"  # only the first decision
    done = _sign("m-4", "x", subject="Completed: Thesis Approval Form - Rowan Ellery",
                 ask="Sign the thesis approval form.")  # a stale ask the model kept
    res = cases.ingest_record(conn, done, found=cases.detect(done, kinds_=FORMS))
    assert res["action"] == "delivered" and res["case_closed"] is True
    assert row["id"] in res["closed"] and _row(conn, row["id"])["status"] == "completed"
    assert ("close", "form_delivered") in _events(conn, row["id"])
    c = cases.case(conn, first["case"])
    assert c["status"] == "closed" and c["decision"] == "accepted"
    assert cases.summary_line(c) == "Form: Thesis Approval Form - Rowan Ellery: all 1 submitted."


def test_a_training_reminder_and_a_forward_are_no_form():
    training = _sign("m-t", "x", subject="Training required",
                     ask="Complete the lab safety training by Friday.")
    assert cases.detect(training, kinds_=FORMS) is None
    fwd = _sign("m-f", "Travel form", subject="Fwd: Travel form")
    assert cases.detect(fwd, kinds_=FORMS) is None
    own = dict(_sign("m-o", "Travel form"), own=True)
    assert cases.detect(own, kinds_=FORMS) is None


# ── 3. a review ───────────────────────────────────────────────────────

def test_a_review_invitation_opens_a_case_and_its_reminder_joins_by_its_id(conn):
    first = _ingest(conn, _invite("m-r1", "JRX-2026-0042"), REVIEWS)
    c = cases.case(conn, first["case"])
    assert c["kind"] == "review" and c["parts"][0]["due"] == "2026-12-01"
    q = cases.questions(conn, kind="review")[0]
    assert q["choices"] == ["Yes", "No"] and "Will you do this review?" in q["text"]
    rem = _invite("m-r2", "JRX-2026-0042", subject="Reminder: review invitation JRX-2026-0042",
                  ask="Accept or decline the invitation to review manuscript JRX-2026-0042.")
    assert _ingest(conn, rem, REVIEWS)["case"] == first["case"]
    out = cases.answer(conn, q["id"], "No", actor="owner")
    assert out["decision"] == "declined"
    row = _row(conn, c["parts"][0]["ledger_id"])
    assert row["status"] == "completed" and ("close", "review_declined") in _events(conn, row["id"])


@pytest.mark.parametrize("subject,ask,summary", [
    # The owner is copied, as the editor, on another reviewer's reminder.
    ("JRX-2026-0042 - Review is Overdue", "Complete and return the overdue review.",
     "The editor is reminding Dr. Quinn Avery that the review is overdue."),
    # The editor's own work: choosing reviewers.
    ("Reviewer selections due", "Log in to the Associate Editor Center and select reviewers.", ""),
    # A reply in a thread about a review request.
    ("Re: Request to review submission 5061", "Acknowledge the reviewer's acceptance.", ""),
    # Reviews of the owner's own paper.
    ("Decision on your manuscript", "Revise and resubmit with the reviews of your paper.", ""),
])
def test_mail_about_reviews_that_asks_the_owner_for_none_opens_no_review(subject, ask, summary):
    rec = _invite("m-x", "JRX-2026-0042", subject=subject, ask=ask, summary=summary)
    assert cases.detect(rec, kinds_=REVIEWS) is None


# ── 4. closing ────────────────────────────────────────────────────────

def test_no_reply_archive_lapse_or_model_finishes_a_case_row_of_any_kind(conn, monkeypatch):
    _add(conn, "task-rv", type_="response_needed", conv="conv-rv")
    out = _ingest(conn, _invite("m-rv", "JRX-2026-0099", conv="conv-rv"), REVIEWS, "task-rv")
    row = _row(conn, "task-rv")
    assert row["type"] == "task" and cases.case_role(row) == "part"
    assert not ledger.reply_owed(row) and not ledger.closes_on_reply(row) and not ledger.lapses(row)
    for signal in ("user_replied_outlook", "reply_in_thread", "user_archived", "lapsed",
                   "date_passed", "llm:reconcile", "inbound_ack:submission-ack", "letter_submitted"):
        assert closure.settle(conn, "task-rv", signal=signal, actor="x", note="n",
                              evidence_ts=NOW)["action"] == "skipped"
        assert closure.propose(conn, "task-rv", signal=signal, actor="x",
                               note="n")["action"] in ("skipped",)
    assert closure.settle(conn, "task-rv", signal="user_handled", actor="cli:owner",
                          note=cases.REPLY_CARD_NOTE)["action"] == "skipped"
    # A send closes a part only when the part goes by email.
    monkeypatch.setattr(cases, "_part_method", lambda link: "portal")
    assert closure.settle(conn, "task-rv", signal="linked_send", actor="x",
                          note="sent")["action"] == "skipped"
    monkeypatch.setattr(cases, "_part_method", lambda link: "email")
    assert cases.signal_allowed("linked_send", row) is True
    res = closure.settle(conn, "task-rv", signal="user_stated", actor="cli:owner", note="done",
                         evidence={"quote": "I sent the review"})
    assert res["action"] == "closed" and out["case"]


def test_a_reply_that_closes_a_reply_row_does_not_sweep_a_case_row_on_the_thread(conn):
    _add(conn, "response-q1", type_="response_needed", conv="conv-s")
    _add(conn, "priority-form", type_="response_needed", conv="conv-s")
    cid = cases.open_case(conn, "form", "Payroll office", part_name="Direct deposit form",
                          ledger_id="priority-form")["case"]
    res = closure.settle(conn, "response-q1", signal="user_replied_outlook",
                         actor="closure_scrubber", note="replied", evidence_ts="2026-11-20T00:00:00Z")
    assert res["ids"] == ["response-q1"] and _row(conn, "priority-form")["status"] == "open"
    assert cases.case(conn, cid)["decision"] == "accepted", "a case opened at his word starts accepted"


def test_a_plugins_row_never_becomes_a_core_case(conn):
    _add(conn, "plugin-row", source="plugin:editor-tools")
    cid = cases.ensure_case(conn, "review", "A journal", title="Paper 7")
    pid = cases.add_part(conn, cid, "report", ledger_id="plugin-row")
    assert cases.part(conn, pid)["ledger_id"] != "plugin-row"
    assert not cases.is_case_row(_row(conn, "plugin-row"))


# ── 5. a confirmation that matches no part for sure ───────────────────

def test_an_unmatched_confirmation_asks_once_remembers_and_never_guesses(conn):
    cid = letters.ensure_case(conn, "Sam Ortiz", "sam.ortiz@uni.example")
    a = letters.add_destination(conn, cid, "Northfield University", "2026-12-01", "portal",
                                sender="apply@northfield.example")
    b = letters.add_destination(conn, cid, "Lakeside Institute", "2026-12-15", "portal",
                                sender="apply@lakeside.example")
    conf = {"id": "m-c1", "subject": "Thank you for submitting a recommendation for the Westbrook "
                                     "Graduate School",
            "from": "no-reply@letters-service.example",
            "result": {"ask": "", "summary": "Your recommendation for Sam Ortiz has been received."}}
    found = letters.detect(conf, plugin_senders=([], []))
    assert found["role"] == "confirmation"
    res = letters.ingest_record(conn, conf, found=found)
    assert res["action"] == "ask_part"
    q = [x for x in letters.questions(conn) if x["kind"] == "part"][0]
    assert q["case"] == cid and q["choices"][-1] == cases.NONE_OF_THESE
    assert {letters.destination(conn, a)["state"], letters.destination(conn, b)["state"]} == {"asked"}
    again = letters.ingest_record(conn, conf, found=found)
    assert again["question"] == q["id"], "asked once"
    pick = next(c for c in q["choices"] if "Lakeside" in c)
    out = cases.answer(conn, q["id"], pick)
    assert out["state"] == "submitted" and letters.destination(conn, b)["state"] == "submitted"
    assert letters.destination(conn, a)["state"] == "asked"
    # The notice's own words for the place are remembered for that part.
    assert cases.same_part(conn, cid, found["destination"]) == b
    # None of these delivers nothing.
    conf2 = dict(conf, id="m-c2", subject="Thank you for submitting a recommendation for the "
                                          "Eastfield Honors College")
    res2 = letters.ingest_record(conn, conf2, found=letters.detect(conf2, plugin_senders=([], [])))
    q2 = cases.questions(conn)[0]
    assert res2["question"] == q2["id"] and len(q2["choices"]) == 2
    cases.answer(conn, q2["id"], cases.NONE_OF_THESE)
    assert letters.destination(conn, a)["state"] == "asked"


def test_a_kept_cases_which_one_question_names_no_one(conn):
    cid = cases.ensure_case(conn, "letter", "Jamie Rivera", keep_level="local")
    for name in ("Northfield University", "Lakeside Institute"):
        cases.add_part(conn, cid, name, sender="lor@service.example")
    conf = {"id": "m-k", "subject": "Thank you for submitting a recommendation",
            "from": "lor@service.example", "result": {"ask": "", "summary": "A letter was received."}}
    res = cases.ingest_record(conn, conf, found=dict(letters.detect(conf, plugin_senders=([], [])),
                                                   case_kind="letter"))
    q = cases.questions(conn)[0]
    assert res["action"] == "ask_part"
    assert "Jamie" not in json.dumps(q) and "Northfield" not in json.dumps(q["choices"])


# ── 6. the letter tables of the first letter release ──────────────────

LEGACY_LETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS letter_cases (
    id            TEXT PRIMARY KEY,
    person_name   TEXT NOT NULL DEFAULT '',
    person_email  TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'recommendation',
    status        TEXT NOT NULL DEFAULT 'open',
    decision      TEXT NOT NULL DEFAULT '',
    opened        TEXT NOT NULL,
    closed_at     TEXT,
    materials     TEXT NOT NULL DEFAULT '[]',
    base_letter   TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    aliases       TEXT NOT NULL DEFAULT '[]',
    keep_level    TEXT NOT NULL DEFAULT 'full',
    case_ledger_id TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS letter_destinations (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    due             TEXT NOT NULL DEFAULT '',
    method          TEXT NOT NULL DEFAULT 'unknown',
    link            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'asked',
    ledger_id       TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    sender          TEXT NOT NULL DEFAULT '',
    draft           TEXT NOT NULL DEFAULT '',
    submitted_at    TEXT,
    evidence        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_letter_dest_case ON letter_destinations(case_id);
CREATE INDEX IF NOT EXISTS idx_letter_dest_ledger ON letter_destinations(ledger_id);
CREATE TABLE IF NOT EXISTS letter_questions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    dest_id     TEXT NOT NULL DEFAULT '',
    ledger_id   TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    choices     TEXT NOT NULL DEFAULT '[]',
    payload     TEXT NOT NULL DEFAULT '{}',
    asked_at    TEXT NOT NULL,
    answer      TEXT NOT NULL DEFAULT '',
    answered_at TEXT,
    actor       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS letter_aliases (
    key      TEXT NOT NULL,
    case_id  TEXT NOT NULL,
    answer   TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (key, case_id)
);
CREATE TABLE IF NOT EXISTS letter_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    case_id  TEXT NOT NULL,
    dest_id  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    state    TEXT NOT NULL DEFAULT '',
    actor    TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT ''
);
"""


def _legacy_db(path):
    c = _ledger_db(sqlite3.connect(str(path)))
    c.executescript(LEGACY_LETTER_SCHEMA)
    c.execute("INSERT INTO letter_cases (id, person_name, person_email, kind, status, decision, "
              "opened, materials, base_letter, notes, aliases, keep_level, case_ledger_id, "
              "updated_at) VALUES ('lc-old1', 'Rowan Ellery', 'rowan@uni.example', 'reference', "
              "'open', 'accepted', ?, ?, '/letters/rowan/letter.md', '', '[]', 'full', "
              "'task-own', ?)", (NOW, json.dumps([{"kind": "cv", "ref": "CV.pdf", "at": NOW}]), NOW))
    for did, name, state in (("ld-a", "Northfield University", "submitted"),
                             ("ld-b", "Lakeside Institute", "accepted")):
        c.execute("INSERT INTO letter_destinations (id, case_id, name, due, method, state, ledger_id, "
                  "created_at, updated_at, submitted_at) VALUES (?, 'lc-old1', ?, '2026-12-01', "
                  "'portal', ?, ?, ?, ?, ?)", (did, name, state, f"task-{did}", NOW, NOW,
                                               NOW if state == "submitted" else None))
        _add(c, f"task-{did}", status="completed" if state == "submitted" else "open",
             meta={"letter": {"case": "lc-old1", "destination": did, "role": "destination"},
                   "closes_on": "submission"})
    c.execute("INSERT INTO letter_questions (id, kind, case_id, text, choices, asked_at, answer) "
              "VALUES ('lq-1', 'write', 'lc-old1', 'Will you write it?', '[]', ?, 'Yes')", (NOW,))
    c.execute("INSERT INTO letter_aliases (key, case_id, answer, at) VALUES "
              "('name:r ellery', 'lc-old1', 'same', ?)", (NOW,))
    for kind_ in ("open", "destination", "accept"):
        c.execute("INSERT INTO letter_events (ts, case_id, kind) VALUES (?, 'lc-old1', ?)",
                  (NOW, kind_))
    c.commit()
    return c


def test_the_first_releases_letter_tables_read_as_they_are_until_the_owner_moves_them(tmp_path):
    db = tmp_path / "ledger.db"
    _legacy_db(db).close()
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    assert [c["person_name"] for c in letters.cases(ro)] == ["Rowan Ellery"], "a read-only reader"
    ro.close()
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    cases.ensure_schema(c)
    assert cases.pending_migration(c)
    lc = letters.case(c, "lc-old1")
    assert [d["state"] for d in lc["destinations"]] == ["submitted", "accepted"]
    assert lc["materials"][0]["ref"] == "CV.pdf" and lc["base_letter"].endswith("letter.md")
    assert cases.version(c, "lc-old1")
    with pytest.raises(cases.MigrationPending):
        letters.set_state(c, "ld-b", "submitted", "upload said OK")
    assert letters.destination(c, "ld-b")["state"] == "accepted", "nothing written"
    c.close()
    plan = cases.migration_plan(_ro(db))
    assert plan["tables"] == {"letter_cases": 1, "letter_destinations": 2, "letter_questions": 1,
                              "letter_aliases": 1, "letter_events": 3}
    report = cases.plan_report(plan)
    assert "Rowan" not in report and "case 1" in report
    w = sqlite3.connect(str(db), isolation_level=None)
    w.row_factory = sqlite3.Row
    out = cases.apply_migration(w)
    assert out["moved"] == {"cases": 1, "parts": 2, "materials": 1, "questions": 1, "aliases": 1,
                            "events": 3}
    assert cases.apply_migration(w)["already"] is True, "idempotent"
    objs = cases._objects(w)
    assert objs["letter_cases"] == "view" and objs["letter_cases_v1"] == "table"
    w.close()
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    cases.ensure_schema(c)
    assert not cases.pending_migration(c)
    assert letters.case(c, "lc-old1")["destinations"][1]["state"] == "accepted"
    out = letters.set_state(c, "ld-b", "submitted", "upload said OK", actor="cli:chat")
    assert out["closed"] == ["task-ld-b"] and out["case_closed"] is True
    assert c.execute("SELECT COUNT(*) FROM letter_events").fetchone()[0] >= 4
    c.close()


def _ro(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def test_empty_letter_tables_give_way_to_the_views_at_once(tmp_path):
    c = _ledger_db(sqlite3.connect(str(tmp_path / "l.db")))
    c.executescript(LEGACY_LETTER_SCHEMA)
    cases.ensure_schema(c)
    assert cases._objects(c)["letter_cases"] == "view" and not cases.pending_migration(c)


# ── 7. levels, versions, moves, gates ─────────────────────────────────

def test_a_kept_case_names_nothing_where_a_hosted_model_reads(conn):
    cid = cases.open_case(conn, "review", "A journal", title="Paper on Ridge Mapping",
                          part_name="report", due="2026-12-01", link="https://r.example/x")["case"]
    conn.execute("UPDATE cases SET keep_level = 'local' WHERE id = ?", (cid,))
    c = cases.case(conn, cid)
    v = cases.view(c)
    assert v["counterpart_name"] == "" and v["title"] == "" and v["questions"] == []
    assert all(set(d) == {"id", "due", "method", "state", "ledger_id", "delivered_at"} for d in v["parts"])
    assert cases.summary_line(c, today=date(2026, 11, 22)) == \
        "Review (kept on your own model): report due Dec 1."


def test_the_version_changes_with_the_case_and_moves_follow_its_state(conn):
    cid = cases.ensure_case(conn, "report", "", title="Annual project report")
    pid = cases.add_part(conn, cid, "", link="https://agency.example/report")
    v1 = cases.version(conn, cid)
    names = [m["move"] for m in cases.moves(cases.case(conn, cid))]
    assert names[:2] == ["accept", "decline"] and {"draft", "delivered", "set_due", "open_link"} <= set(names)
    cases.set_due(conn, pid, "2026-12-01")
    v2 = cases.version(conn, cid)
    cases.add_material(conn, cid, "template", "report-template.docx")
    v3 = cases.version(conn, cid)
    assert len({v1, v2, v3}) == 3
    cases.record_draft(conn, cid, "/docs/report.md")
    assert {"ready", "delivered"} <= {m["move"] for m in cases.moves(cases.case(conn, cid))}


def test_no_scheduled_lane_drafts_a_reply_on_a_case_row():
    row = {"id": "task-f", "type": "task", "conversation_id": "conv-f",
           "work_case": {"kind": "form", "case": "case-form-1", "part": "p", "role": "part"}}
    assert draft_gate.cron_refusal(run_id="cron-morning-1", rows=[row], decisions=[],
                                   keep_records=[]) == draft_gate.CASE_REFUSAL
    assert draft_gate.check(dict(row, title="Form", ask="Sign it."))["rule"] == "case"


def test_the_models_work_field_is_in_the_record_and_names_the_mode():
    assert "work" in mail_privacy.SCHEMA["required"]
    rec = mail_privacy.validate({"state": "todo", "ask": "Sign the waiver.", "work": {
        "kind": "form", "role": "request", "counterpart": " Payroll ", "part": "", "item": "W-1",
        "due": "soon"}})
    assert rec["work"] == {"kind": "form", "role": "request", "counterpart": "Payroll", "part": "",
                           "item": "W-1", "due": ""}
    assert rec["response_mode"] == "form" and rec["needs_reply"] is False
    assert mail_privacy.validate({"state": "fyi"})["work"] == mail_privacy.NO_WORK
    assert mail_completion.work_mode("report") == "document"
    assert mail_completion.response_mode({"thread_brief": {"work": {"kind": "review",
                                                                    "role": "reminder"}}}) == "submission"


def test_the_models_work_field_fills_facts_but_opens_no_case_alone():
    rec = _invite("m-w", "JRX-2026-0042")
    rec["result"]["work"] = {"kind": "review", "role": "request", "counterpart": "Journal of Ridges",
                             "part": "", "item": "", "due": ""}
    assert cases.detect(rec, kinds_=REVIEWS)["counterpart"] == "Journal of Ridges"
    alone = {"id": "m-a", "subject": "Hello", "from": "a@b.example",
             "result": {"ask": "See below.", "summary": "", "state": "todo",
                        "work": {"kind": "review", "role": "request", "counterpart": "",
                                 "part": "", "item": "", "due": ""}}}
    assert cases.detect(alone, kinds_=REVIEWS) is None


def test_a_new_message_on_a_cases_thread_shows_on_the_case_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.db")
    rec = _sign("m-n1", "Lease Renewal Form", conv="conv-lease")
    first = cases.ingest([rec], kinds_=FORMS)
    assert first[0]["action"] == "case"
    note = {"id": "m-n2", "conversation_id": "conv-lease", "received": "2026-11-13T00:00:00Z",
            "subject": "Re: Lease Renewal Form", "from": "Office@Mapleton.example",
            "from_name": "Leasing office", "result": {"ask": "", "summary": "Any questions?"}}
    mine = dict(note, id="m-n3", own=True)
    cases.ingest([note, mine, note], kinds_=FORMS)
    conn = cases.connect()
    try:
        c = cases.case(conn, first[0]["case"])
        assert [(m["from"], m["name"], m["message_id"]) for m in c["messages"]] == [
            ("office@mapleton.example", "Leasing office", "m-n2")]
        conn.execute("UPDATE cases SET keep_level = 'local' WHERE id = ?", (c["id"],))
        assert "from" not in cases.view(cases.case(conn, c["id"]))["messages"][0]
    finally:
        conn.close()


def test_a_meeting_request_is_never_a_case_and_a_kept_record_fails_closed():
    rec = _sign("m-s", "Room booking form")
    rec["result"]["scheduling"] = "request"
    assert cases.detect(rec, kinds_=FORMS) is None
    assert cases.record_level({"keep_from_frontier": "applicant"}) == "local"
    assert cases.record_level({"keep_level": "facts"}) == "facts"
    assert cases.record_level({}, "summary") == "summary" and cases.record_level({}) == "full"
