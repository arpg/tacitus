"""Work cases of every kind where the owner sees them
(preprocessor/case_surface.py): a review to return, a form to fill in, a
report owed by a date, beside the letters (tests/test_letter_surface.py
keeps the letters' own lines). One line per case in the briefings, in the
kind's words, never one per mail and never a reply owed; one list for all
cases under one plain heading; each kind's reminder ladder; the decision
card and the case block in Action requests and a press on them; Home's
item; the pre-send review's record and its rule; a room's name; the Slack
cards; and a new message from the other side, which a case row (no reply
owed) would otherwise lose. Every name, address and place here is made
up."""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))
sys.path.insert(2, str(ROOT / "botcore"))

import case_surface as C  # noqa: E402
import letter_surface as L  # noqa: E402

TODAY = date(2026, 11, 23)                                   # a Monday
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)     # 04:00 in Denver
ZONE = ZoneInfo("America/Denver")


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    """No test reads a real store: no letters, no cases, no letters_work."""
    monkeypatch.setattr(L, "_letters", lambda: None)
    monkeypatch.setattr(C, "_cases", lambda: None)
    monkeypatch.setitem(sys.modules, "letters_work", None)
    C._DECL.clear()
    yield
    C._DECL.clear()


@pytest.fixture
def mountain(monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _part(pid, name, due="", state="accepted", method="email", link=""):
    return {"id": pid, "case": "", "name": name, "due": due, "method": method, "link": link, "state": state,
            "ledger_id": f"row-{pid}", "conversation_id": f"conv-{pid}", "sender": "", "draft": "",
            "delivered_at": "", "evidence": ""}


def _case(cid, kind, sub, who, email, parts, **over):
    c = {"id": cid, "kind": kind, "subkind": sub, "counterpart_name": who, "counterpart_email": email,
         "title": "", "status": "open", "decision": "accepted", "opened": "2026-11-10", "closed_at": "",
         "materials": [], "base_doc": "", "notes": "", "keep_level": "full", "case_ledger_id": "",
         "parts": parts, "questions": [], "case_kind": kind}
    c.update(over)
    return c


def review():
    """A paper review the owner said yes to by Dec 1 (its one date), its
    report due Dec 1 on a site: within its lead time, so its block is up."""
    return _case("rv-1", "review", "paper", "Pat Lee", "pat.lee@example.org",
                 [_part("rp1", "Referee report", "2026-12-01", method="portal",
                        link="https://review.example.org/ms/123")],
                 materials=[{"kind": "document", "ref": "/docs/reviews/rv-1/paper.pdf"}], do_by="2026-12-01")


def shown(c, today=TODAY):
    """A bot record that has shown the case as it is now."""
    return {"sig": C.block_sig(c, today), "do_by": c.get("do_by", ""),
            "parts": [d["id"] for d in C.open_parts(c)]}


def new_review():
    """A review request from yesterday with no answer yet."""
    return _case("rv-2", "review", "proposal", "Jordan Park", "jpark@example.net",
                 [_part("rp2", "Panel report", "2026-12-14", state="asked")], decision="", opened="2026-11-22",
                 questions=[{"id": "rq-1", "kind": "decide", "text": "Will you do this review?",
                             "choices": ["Yes", "No"], "answer": ""}])


def form():
    return _case("fm-1", "form", "disclosure", "Compliance Office", "compliance@example.edu",
                 [_part("fp1", "Annual disclosure", "2026-11-27", method="portal")], opened="2026-11-20")


def report():
    """Three parts: one delivered, one drafted, one to write."""
    return _case("rp-1", "report", "progress", "Program Office", "office@example.gov",
                 [_part("pp1", "Narrative", "2026-12-04", state="drafted"), _part("pp2", "Budget", "2026-12-04"),
                  _part("pp3", "Data table", "2026-11-20", state="delivered")], opened="2026-11-01")


def letter():
    """One letter case, as letters.py gives it (its own shape)."""
    return {"id": "lc-1", "person_name": "Rowan Ellery", "person_email": "re@example.org", "kind": "nomination",
            "opened": "2026-11-02", "materials": [{"kind": "cv", "ref": "cv.pdf"}],
            "destinations": [{"id": "ld1", "name": "Mountain Teaching Award", "due": "2026-11-24",
                              "method": "email", "link": "", "state": "accepted", "ledger_id": "task-ld1",
                              "submitted_at": "", "evidence": ""}]}


def _row(c, d, **over):
    r = {"id": d["ledger_id"], "type": "task", "status": "open", "description": f"Task: {d['name']}",
         "conversation_id": d["conversation_id"], "source_message_id": f"m-{d['id']}", "closes_on": "submission",
         "work_case": {"kind": c["kind"], "case": c["id"], "part": d["id"], "role": "part"}}
    r.update(over)
    return r


def rows_of(*cases):
    return {d["ledger_id"]: _row(c, d) for c in cases for d in C.parts(c)}


# ── one line per case, in the kind's words ────────────────────────────

def test_each_kind_has_one_line_in_its_own_words():
    assert C.line(review(), TODAY) == "Review for Pat Lee: report due Tue Dec 1."
    assert C.line(new_review(), TODAY) == ("Review for Jordan Park: asked Nov 22, report due Mon Dec 14. "
                                           "Will you do this review? Answer in Action requests.")
    assert C.line(form(), TODAY) == "Form for Compliance Office: due Fri Nov 27."
    assert C.line(report(), TODAY) == "Report for Program Office: 1 of 3 delivered. Next due Fri Dec 4: 2 parts."
    assert C.name(review()) == "Paper review for Pat Lee" and C.name(form()) == "Disclosure form for Compliance Office"
    late = form()
    late["parts"][0]["due"] = "2026-11-20"
    late["parts"][0]["state"] = "drafted"
    assert C.line(late, TODAY) == "Form for Compliance Office: was due Fri Nov 20, filled in, not checked."
    # kept on the owner's own model: fixed words, no name
    assert C.line(review(), TODAY, lv="local") == "A kept review: report due Tue Dec 1."
    # no one named (a service's form): its title, which kept mail holds back
    untitled = dict(form(), counterpart_name="", title="Annual disclosure 2026")
    assert C.line(untitled, TODAY) == "Form: Annual disclosure 2026: due Fri Nov 27."
    assert C.post_line(C.candidate(C.entry(untitled, TODAY))) == "*Form: Annual disclosure 2026*: due Fri Nov 27."
    assert C.line(untitled, TODAY, lv="facts") == "A kept form: due Fri Nov 27."
    assert C.name(dict(untitled, title=""), "full") == "Disclosure form for someone not named yet"
    assert "—" not in C.line(report(), TODAY)


def test_a_letter_keeps_its_own_line_through_the_general_surface():
    assert C.line(C._tag(letter(), "letter"), TODAY) == \
        "Letter for Rowan Ellery: 0 of 1 submitted. Next due tomorrow: 1 destination."
    assert C.kind_of(letter()) == "letter" and C.kind_of(review()) == "review"


# ── a new message from the other side ─────────────────────────────────

def _brief(conv, *msgs, last_own=""):
    return {"conversation_id": conv, "messages": [dict(m) for m in msgs], "last_from_user": last_own}


def test_a_new_message_from_them_ends_the_line_and_is_never_lost():
    c = review()
    rows = rows_of(c)
    now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
    rows["row-rp1"]["thread_brief"] = _brief("conv-rp1", {"id": "m1", "from": "pat.lee@example.org",
                                                         "received": "2026-11-23T09:00:00Z", "own": False})
    got = C.news(c, rows, now=now)
    assert [(n["from"], n["at"]) for n in got] == [("Pat Lee", "2026-11-23T09:00:00Z")]
    assert C.line(c, TODAY, news=got) == "Review for Pat Lee: report due Tue Dec 1. New message from Pat Lee."
    e = C.entry(c, TODAY, rows, now=now)
    assert e["news"] == [{"from": "Pat Lee", "at": "2026-11-23T09:00:00Z"}] and e["line"].endswith("Pat Lee.")
    # he wrote on that thread after it: no news
    rows["row-rp1"]["thread_brief"]["last_from_user"] = "2026-11-23T10:00:00Z"
    assert C.news(c, rows, now=now) == []
    # a system's notice is no news from a person; an old message is no news
    rows["row-rp1"]["thread_brief"] = _brief("conv-rp1", {"id": "m2", "from": "no-reply@review.example.org",
                                                         "received": "2026-11-23T09:00:00Z"},
                                             {"id": "m3", "from": "pat.lee@example.org",
                                              "received": "2026-11-19T09:00:00Z"})
    assert C.news(c, rows, now=now) == []
    # the run's own new inbox mail (Graph's shape) counts too; kept: fixed words
    mail = [{"id": "m4", "conversationId": "conv-rp1", "receivedDateTime": "2026-11-23T12:00:00Z",
             "from": {"emailAddress": {"name": "Sam Ortiz", "address": "sam.ortiz@example.org"}}},
            {"id": "m5", "conversationId": "conv-other", "receivedDateTime": "2026-11-23T12:00:00Z",
             "from": {"emailAddress": {"name": "Someone Else", "address": "x@example.org"}}}]
    got = C.news(c, rows, mail=mail, now=now)
    assert [n["from"] for n in got] == ["Sam Ortiz"]
    assert C.news_words(C.news(c, rows, mail=mail, now=now, lv="local"), "local") == "A new message came."


def test_the_block_is_posted_again_when_they_write_after_it():
    c = review()
    rows = rows_of(c)
    rows["row-rp1"]["thread_brief"] = _brief("conv-rp1", {"id": "m1", "from": "pat.lee@example.org",
                                                         "received": "2026-11-23T09:00:00Z"})
    now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
    marks = C.news_marks([c], rows, now=now)
    assert marks["rv-1"]["at"] == "2026-11-23T09:00:00Z" and marks["rv-1"]["words"] == "New message from Pat Lee."
    posted = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc).timestamp()
    cards = {"rv-1": {"kind": "case", "live": True, "posted": posted, **shown(c)}}
    [plan] = C.card_plan([c], cards, TODAY, {}, marks)
    assert (plan["what"], plan["kind"], plan["at"]) == ("news", "case", "2026-11-23T09:00:00Z")
    spec = C.case_spec(c, TODAY, news=marks["rv-1"]["items"])
    assert spec["lines"][0] == "New message from Pat Lee."
    cards["rv-1"]["news_at"] = "2026-11-23T09:00:00Z"
    assert C.card_plan([c], cards, TODAY, {}, marks) == []          # shown once
    cards = {"rv-1": {"kind": "case", "live": True, "posted": now.timestamp(), **shown(c)}}
    assert C.card_plan([c], cards, TODAY, {}, marks) == []          # the block came after it


def test_a_message_while_the_case_is_quiet_is_one_line():
    """News is not a nag: a quiet case says it once, as a line, and stays quiet."""
    c = dict(review(), do_by="2026-12-20")
    c["parts"][0]["due"] = ""
    rows = rows_of(c)
    rows["row-rp1"]["thread_brief"] = _brief("conv-rp1", {"id": "m1", "from": "pat.lee@example.org",
                                                         "received": "2026-11-23T09:00:00Z"})
    marks = C.news_marks([c], rows, now=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc))
    cards = {"rv-1": {"kind": "ask", "live": False, "posted": 1.0, **shown(c)}}
    [step] = C.card_plan([c], cards, TODAY, {}, marks)
    assert (step["what"], step["text_key"]) == ("note", "news")
    assert C.note_text(c, step, TODAY) == ":envelope: New message from Pat Lee. The report for Pat Lee stays set for Sun Dec 20."
    cards["rv-1"]["news_at"] = step["at"]
    assert C.card_plan([c], cards, TODAY, {}, marks) == []


# ── the briefing JSON ─────────────────────────────────────────────────

def test_the_briefing_json_keeps_letters_apart_and_lists_every_other_kind_once():
    cases = [C._tag(letter(), "letter"), review(), form(), report()]
    rows = list(rows_of(review(), form(), report()).values()) + [
        {"id": "task-ld1", "type": "task", "status": "open", "description": "Task: award portal",
         "conversation_id": "conv-ld1", "letter": {"case": "lc-1", "destination": "ld1"}},
        {"id": "deadline-x", "type": "deadline", "status": "open", "description": "Due: budget memo",
         "conversation_id": "conv-x", "deadline": "2026-11-25"}]
    view = C.briefing_view(rows, today=TODAY, cases=cases, closed=[], now=NOW)
    b = C.apply_briefing({"open_obligations": rows, "today_top": [],
                          "new_needs_response": [{"message_id": "m-fp1", "conversation_id": "conv-fp1"},
                                                 {"message_id": "m-y", "conversation_id": "conv-y"}]}, view)
    assert [e["case"] for e in b["letter_cases"]] == ["lc-1"]
    assert [e["case"] for e in b["work_cases"]] == ["fm-1", "rv-1", "rp-1"]
    assert [e["case_kind"] for e in b["work_cases"]] == ["form", "review", "report"]
    assert [o["id"] for o in b["open_obligations"]] == ["deadline-x"]
    assert [m["message_id"] for m in b["new_needs_response"]] == ["m-y"]
    top = {t["ref"]: t for t in b["today_top"]}
    assert top["case:fm-1"]["reason"] == "form due in 4 days" and top["case:fm-1"]["source"] == "case"
    assert top["letter:lc-1"]["reason"] == "letter due tomorrow"
    assert "case:rv-1" not in top                                  # Dec 1 is not close yet
    assert b["work_cases"][1]["parts"] == [{"name": "Referee report", "due": "2026-12-01", "state": "accepted"}]


def test_a_finished_case_of_each_kind_says_so_quietly():
    done = review()
    done["parts"][0].update(state="returned", delivered_at="2026-11-22T18:00:00Z")
    view = C.briefing_view([], today=TODAY, cases=[], closed=[done, C._tag(dict(letter(), destinations=[dict(
        letter()["destinations"][0], state="submitted", submitted_at="2026-11-23T08:00:00Z")]), "letter")])
    assert C.finished_words(view["finished"]) == (
        "1 review finished: all its reports are returned (Paper review for Pat Lee); "
        "1 letter finished: all its destinations are submitted (Nomination letter for Rowan Ellery)")


# ── the remote briefing post ──────────────────────────────────────────

@pytest.fixture
def priv(tmp_path, monkeypatch):
    import owner_trust
    import remote_briefing as rb
    import remote_llm as rl
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    monkeypatch.setattr(rb, "outlook_links", lambda convs, **k: {})
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "drafting": "remote", "briefings": "remote", "shadow_until": "2099-01-01"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def _model(plan):
    def chat(messages, **kw):
        chat.calls.append((kw.get("purpose"), messages))
        return json.loads(json.dumps(plan)), {"latency_ms": 5}
    chat.calls = []
    return chat


def _post(cases):
    rows = [r for c in cases if C.kind_of(c) != "letter" for r in rows_of(c).values()]
    view = C.briefing_view(rows, today=TODAY, cases=cases, closed=[], now=NOW)
    return C.apply_briefing({"open_obligations": rows, "today_top": []}, view)


EMPTY = {"get_done_today": [], "flexes": [], "drafts": [], "needs_you": [], "escalate": []}


def test_one_list_for_every_case_under_one_plain_heading(mountain, priv):
    import remote_briefing as rb
    b = _post([C._tag(letter(), "letter"), review(), report()])
    cands = rb.candidates(b, today=TODAY)
    kinds = sorted(c["kind"] for c in cands.values())
    assert kinds == ["case", "case", "letter"]
    plan = dict(EMPTY, drafts=[{"ref": "C2", "reply_must_say": "Yes."}], needs_you=[{"ref": "C3", "decision": "x"}])
    text, rec = rb.run("morning-briefing", b, run_id="r", now=NOW, chat=_model(plan), dry_run=True,
                       audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert ":memo: *Work you owe* (2)" in text
    block = text.split(":memo: *Work you owe* (2)")[1].split("\n\n")[0]
    assert "- *Review for Pat Lee*: report due Tue Dec 1." in block
    assert "- *Report for Program Office*: 1 of 3 delivered. Next due Fri Dec 4: 2 parts." in block
    assert ":alarm_clock: DUE SOON: *Letter for Rowan Ellery*" in text
    assert "Drafts ready" not in text and "Needs you" not in text and rec["drafts"] == 0
    system = _model(EMPTY)
    rb.run("morning-briefing", b, run_id="r", now=NOW, chat=system, dry_run=True,
           audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert "`case` (a review, a form or a report he owes" in system.calls[0][1][0]["content"]
    # one kind alone: the kind's own heading
    only = _post([review(), new_review()])
    text, _ = rb.run("morning-briefing", only, run_id="r", now=NOW, chat=_model(EMPTY), dry_run=True,
                     audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert ":memo: *Reviews you are doing* (2)" in text and "Will you do this review? Answer in Action requests." in text


def test_the_afternoon_names_cases_due_today_under_the_kinds_heading(mountain, priv):
    import remote_briefing as rb
    due = review()
    due["parts"][0]["due"] = "2026-11-23"
    b = _post([due])
    text, _ = rb.run("afternoon-catchup", b, run_id="r", now=NOW + timedelta(hours=10),
                     chat=_model({"since_morning": [{"ref": "C1", "line": "new mail"}], "pending": []}),
                     audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert ":memo: *Reviews due* (1):\n- *Review for Pat Lee*: report due today." in text
    assert "new mail" not in text


def test_the_weekly_audit_lists_a_case_once_and_its_delivered_parts_as_one_line(monkeypatch):
    import remote_audit as ra
    rows = list(rows_of(report()).values())
    b = _post([report()])
    cands = ra.candidates(rows, b, today=TODAY)
    assert [c["kind"] for c in cands.values()] == ["case"]
    assert ra._line(cands["C1"], "x") == "• *Report for Program Office*: 1 of 3 delivered. Next due Fri Dec 4: 2 parts."
    monkeypatch.setattr(C, "load_cases", lambda conn=None, open_only=True, kinds=None: [report()])
    done = [dict(r, status="completed") for r in rows]
    out = C.collapse_rows(done, open_only=False, today=TODAY)
    assert len(out) == 1 and out[0]["description"].startswith("Progress report for Program Office: 1 of 3")


# ── reminders by date ─────────────────────────────────────────────────

def _rungs(c, due: date):
    got = {}
    for n in range(-3, 20):
        day = due - timedelta(days=n)
        r = C.reminders([c], day)
        if r:
            got[n] = (r[0]["rung"], r[0]["loud"])
    return got


def test_each_kind_rings_its_own_ladder():
    # The quiet rungs posted nothing: each kind's lead time brings the case
    # back (its stage turns due), and the loud rungs ring on the nearest date.
    assert _rungs(review(), date(2026, 12, 1)) == {1: (1, True), 0: (0, True), -1: (-1, True)}
    assert _rungs(form(), date(2026, 11, 27)) == {1: (1, True), 0: (0, True), -1: (-1, True)}
    assert {k: C._dates().kind_lead(k) for k in ("letter", "form", "review", "report")} == \
        {"letter": 7, "form": 3, "review": 10, "report": 7}
    items = C.reminders([review()], date(2026, 11, 30))
    assert C.reminder_text(review(), items, date(2026, 11, 30), "full") == \
        "Review for Pat Lee: 1 report due tomorrow (Referee report)."


# ── the cards in Action requests ──────────────────────────────────────

def test_the_new_case_card_asks_the_kinds_own_question():
    spec = C.decision_spec(new_review(), TODAY)
    assert spec["kind"] == "case_ask" and spec["title"] == ":memo: *A review request.* Will you do this review?"
    # The yes carries the one date in the same tap: the date the records
    # state, or another the owner picks (a date picker, never free words).
    assert [(a[0], a[1]) for a in spec["actions"]] == [("case_yes", ":white_check_mark: Yes, by Mon Dec 14"),
                                                       ("case_yes_date", ":calendar: Yes, by another date"),
                                                       ("case_no", ":no_entry_sign: No")]
    assert spec["actions"][0][2] == {"case": "rv-2", "rows": ["row-rp2"], "question": "rq-1", "by": "2026-12-14"}
    assert spec["actions"][1][5] == "The date" and spec["actions"][1][6] == {
        "proposed": "2026-12-14", "min": "2026-11-23", "max": "2027-11-24"}
    assert spec["no_default"] and all(a[3] != "danger" for a in spec["actions"])
    assert "1. Panel report: due Mon Dec 14, not answered yet, by email" in spec["lines"]
    assert dict(spec["fields"])["From"] == "Jordan Park"
    rep = dict(report(), decision="")
    for d in rep["parts"]:
        d["state"] = "asked"
    assert [a[0] for a in C.decision_spec(rep, TODAY)["actions"]] == ["case_yes", "case_yes_date", "case_yes_ask",
                                                                      "case_no"]
    nodate = dict(new_review())
    nodate["parts"] = [dict(nodate["parts"][0], due="")]
    yes = C.decision_spec(nodate, TODAY)["actions"][0]                # no date stated: the yes asks for it
    assert (yes[0], yes[1], yes[5], yes[6]) == ("case_yes", ":white_check_mark: Yes", "The date",
                                                {"min": "2026-11-23", "max": "2027-11-24"})
    assert C.decision_spec(dict(form(), decision=""), TODAY)["title"].endswith("Will you fill it in?")
    kept = C.decision_spec(new_review(), TODAY, "local")
    assert "Jordan" not in json.dumps(kept) and "Panel report" not in json.dumps(kept)


def test_the_case_block_offers_what_fits_its_state():
    # Its time came: "Time to do the review", Draft, the date as a picker,
    # Decline and a menu for a report already returned (the delivery
    # buttons are the ready report's list).
    spec = C.case_spec(review(), TODAY)
    assert spec["kind"] == "case" and spec["title"] == ":memo: *Time to do the review for Pat Lee*: due Tue Dec 1, 1 report."
    assert [(a[0], a[1]) for a in spec["actions"]] == [
        ("case_draft", ":pencil2: Draft the report"), ("case_date_pick", ":calendar: Change the date"),
        ("case_decline", "Decline")]
    assert spec["select"] == ("case_delivered", "Returned: pick one", [("Referee report", "rp1")])
    assert "Missing: the report." in spec["lines"]
    assert "<https://review.example.org/ms/123|Open the review site for Referee report>" in spec["lines"]
    assert all("ledger_id" not in a[2] for a in spec["actions"])
    undated = form()
    undated["parts"][0]["due"] = ""
    acts = C.case_spec(undated, TODAY)["actions"]
    assert [a[0] for a in acts] == ["case_draft", "case_date_pick", "case_decline"]
    assert acts[0][1] == ":pencil2: Draft the answers" and acts[1][1] == ":calendar: Pick the date" and "part" not in acts[1][2]
    assert C.case_spec(report(), TODAY)["actions"][0][1] == ":pencil2: Edit the report"
    kept = C.case_spec(review(), TODAY, "local")
    assert not any(ln.startswith("<https://") for ln in kept["lines"]) and "Pat Lee" not in json.dumps(kept)


def test_the_card_plan_asks_once_then_keeps_one_block():
    plan = C.card_plan([new_review(), review()], {}, TODAY)
    assert sorted((p["case"], p["what"]) for p in plan) == [("rv-1", "case"), ("rv-2", "ask")]
    cards = {"rv-2": {"kind": "ask", "live": True}, "rv-1": {"kind": "case", "live": True, **shown(review())}}
    assert C.card_plan([new_review(), review()], cards, TODAY) == []
    plan = C.card_plan([review()], cards, date(2026, 11, 30))
    assert plan[0]["what"] == "remind" and plan[0]["items"][0]["rung"] == 1
    done = review()
    done["parts"][0]["state"] = "returned"
    assert C.card_plan([done], {"rv-1": cards["rv-1"]}, TODAY) == [{"case": "rv-1", "what": "retire"}]


def test_an_answer_runs_the_stores_verbs_and_one_turn_for_the_rest(monkeypatch):
    calls = []

    def runner(argv):
        calls.append(argv)
        return {"ok": True}
    monkeypatch.setattr(C, "today_local", lambda: TODAY)
    # The yes sets the one date (the earliest the records state) and says
    # when the case comes back (a review's lead time: ten days).
    yes = C.answer(new_review(), "yes", question="rq-1", runner=runner)
    assert yes == {"ok": True, "said": "You said yes, by Mon Dec 14. I bring it back on Fri Dec 4."}
    assert calls[-1] == ["answer", "--question", "rq-1", "--choice", "Yes", "--date", "2026-12-14"]
    other = C.answer(new_review(), "yes_date", text="2026-12-10", question="rq-1", runner=runner)
    assert other["said"] == "You said yes, by Thu Dec 10. I bring it back on Mon Nov 30."
    assert calls[-1][-2:] == ["--date", "2026-12-10"]
    assert C.answer(new_review(), "yes_date", text="", question="rq-1", runner=runner) == \
        {"ok": False, "said": C.PICK_A_DATE}
    no = C.answer(new_review(), "no", question="rq-1", runner=runner)
    assert calls[-1] == ["answer", "--question", "rq-1", "--choice", "No"]
    assert "Write a short decline to Jordan Park as a draft that waits for his Send" in no["turn"]
    assert "Never send it." in no["turn"]
    quiet = C.answer(dict(form(), decision=""), "no", runner=runner)
    assert quiet == {"ok": True, "said": "You said no. Nothing is sent."} and calls[-1] == ["decline", "--case", "fm-1"]
    got = C.answer(review(), "delivered", dest="rp1", runner=runner)
    assert got["said"] == "Returned."
    assert calls[-1] == ["record-delivered", "--part", "rp1", "--evidence", "the owner said so in Action requests"]
    assert not C.answer(review(), "delivered", runner=runner)["ok"]
    undated = form()
    undated["parts"][0]["due"] = ""
    # One date for the whole case (never a part's, never a turn that asks
    # which part); words with no day in them change nothing and loop on nothing.
    dated = C.answer(undated, "date", text="Dec 1", runner=runner)
    assert dated == {"ok": True, "said": "The date is Tue Dec 1. I bring it back on Sat Nov 28."}
    assert calls[-1] == ["set-date", "--case", "fm-1", "--date", "2026-12-01"]
    n = len(calls)
    assert C.answer(undated, "date", text="soon", runner=runner) == {"ok": False, "said": C.PICK_A_DATE}
    assert len(calls) == n and "turn" not in C.answer(undated, "date", text="soon", runner=runner)
    draft = C.answer(review(), "draft", text="be kind about the method", runner=runner)
    assert "His judgment, scores and recommendation are his" in draft["turn"]
    assert "docs/ea/reviews.md" in draft["turn"] and "never goes in a channel post" in draft["turn"]
    assert 'The owner added: "be kind about the method".' in draft["turn"]
    assert "A signature" in C.answer(form(), "draft", runner=runner)["turn"]
    ask = C.answer(dict(report(), decision=""), "yes_ask", runner=runner)
    assert ask["turn"].startswith("Report case rp-1") and "Ask only for what the request did not" in ask["turn"]
    join = dict(review(), questions=[{"id": "rq-9", "kind": "join", "choices": ["Same person", "Someone else"],
                                      "answer": ""}])
    assert C.answer(join, "join_same", question="rq-9", runner=runner)["ok"]
    assert calls[-1] == ["answer", "--question", "rq-9", "--choice", "Same person"]
    assert not C.answer(review(), "close", runner=runner)["ok"]


def test_a_letter_answer_still_goes_the_letters_way():
    calls = []
    got = C.answer(C._tag(letter(), "letter"), "delivered", dest="ld1",
                   runner=lambda argv: calls.append(argv) or {"ok": True})
    assert got["said"] == "Submitted." and calls[-1][:3] == ["record-submitted", "--dest", "ld1"]


def test_the_parse_of_a_due_date():
    assert C.parse_day("Dec 1", TODAY) == date(2026, 12, 1)
    assert C.parse_day("2027-01-15", TODAY) == date(2027, 1, 15)
    assert C.parse_day("1/5", TODAY) == date(2027, 1, 5)
    assert C.parse_day("tomorrow", TODAY) == date(2026, 11, 24)
    assert C.parse_day("next week", TODAY) is None


def test_a_which_one_question_is_one_card_and_its_pick_is_recorded():
    q = {"id": "fq-1", "kind": "part", "case": "", "text": "A submitted notice from example.edu matches no form "
         "for sure. Which one was it?", "choices": ["Compliance Office: Annual disclosure (due Nov 27)",
                                                    "Compliance Office: Travel form", "None of these"], "answer": ""}
    spec = C.which_spec(q)
    assert spec["kind"] == "case_ask" and spec["title"] == ":memo: *Which one was it?*"
    assert [a[0] for a in spec["actions"]] == ["case_which"] * 3 and spec["actions"][2][2]["pick"] == 2
    assert "Nothing is marked done until you answer." in spec["lines"]
    assert C.which_plan([q], {}) == [{"case": "q:fq-1", "what": "which", "question": "fq-1", "key": "q:fq-1"}]
    assert C.which_plan([], {"q:fq-1": {"kind": "which", "live": True}}) == [{"case": "q:fq-1", "what": "retire"}]
    calls = []
    got = C.answer_which(q, 0, runner=lambda argv: calls.append(argv) or {"ok": True})
    assert got["said"] == "Recorded. That one is marked done."
    assert calls[-1] == ["answer", "--question", "fq-1", "--choice", "Compliance Office: Annual disclosure (due Nov 27)"]
    assert C.answer_which(q, 2, runner=lambda argv: {"ok": True})["said"] == "Recorded. Nothing is marked done."
    assert not C.answer_which(q, 9, runner=lambda argv: {"ok": True})["ok"]


def test_the_slack_front_end_draws_both_cards_and_the_folded_block_names_them():
    from botcore.chat import action_requests as ar
    from botcore.chat import slack_frontend as sf
    from preprocessor import chat as _chat
    for spec in (C.decision_spec(new_review(), TODAY), C.case_spec(report(), TODAY)):
        actions = [_chat.CardAction(*a[:5], ask=a[5], date=a[6] if len(a) > 6 else {}) for a in spec["actions"]]
        card = _chat.ConfirmationCard(kind=spec["kind"], title=spec["title"], lines=spec["lines"],
                                      actions=actions, block_id=spec["block_id"])
        text, blocks = sf.render_card(card)
        assert "actions" in [b["type"] for b in blocks] and text
    fields = [{"label": "From", "value": "Pat Lee"}, {"label": "Subject", "value": "Paper review for Pat Lee"},
              {"label": "When", "value": "Next due Tue Dec 1"}]
    assert ar.block({"card": {"kind": "case", "title": "x", "fields": fields}}) == {
        "action": "Work", "who": "From Pat Lee", "what": "Paper review for Pat Lee", "when": "Next due Tue Dec 1"}
    assert ar.block({"card": {"kind": "case_ask", "title": "x", "fields": fields}})["action"] == "Work request"


def test_the_block_edge_reads_the_case_due_date(monkeypatch):
    import web_requests as wr
    monkeypatch.setattr(wr._wts, "ledger_rows", lambda ids, convs, db: [])
    monkeypatch.setattr(wr, "_projects", lambda ids, convs, db: ({}, {}))
    monkeypatch.setattr(C, "load_cases", lambda conn=None, open_only=True, kinds=None: [review()])
    spec = C.case_spec(review(), TODAY)
    card = {"kind": "case", "actions": [{"id": a[0], "value": a[2]} for a in spec["actions"]]}
    assert wr.context([{"id": "m1", "card": card}]) == {"m1": {"due": "2026-12-01"}}


# ── the bot's tick and a press (ea-chat-bot.py) ───────────────────────

class _Sync:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


def _bot(monkeypatch, case_list, questions=()):
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_LETTER_CARDS = _private_state(")
    end = src.index("# The Replies card: the replies that wait")
    from preprocessor import case_surface as PC
    from preprocessor import chat as _chat
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "_letters", lambda: None)
    monkeypatch.setattr(PL, "load_cases", lambda conn=None, open_only=True: [])
    monkeypatch.setattr(PC, "_cases", lambda: None)
    monkeypatch.setattr(PC, "load_cases", lambda conn=None, open_only=True, kinds=None: case_list)
    monkeypatch.setattr(PC, "today_local", lambda: TODAY)
    monkeypatch.setattr(PC, "part_questions", lambda conn=None: list(questions))
    calls = {"cards": [], "status": [], "posts": [], "turns": [], "answers": [], "actions": []}
    store: dict = {}
    retired = set()

    class App:
        def action(self, name):
            calls["actions"].append(name)
            return lambda fn: fn

    def post_card(ch, card, thread_ts=None):
        calls["cards"].append(card)
        return types.SimpleNamespace(channel=ch, id=f"m{len(calls['cards'])}", thread_key=f"m{len(calls['cards'])}")

    class Cards:
        def by_ref(self, ch, mid):
            return {"status": "retired" if mid in retired else "live"}

    def replace(ch, ts, text):
        calls["status"].append((ts, text))
        retired.add(ts)

    class Frontend:
        def post(self, channel="", thread_ts=None, text=""):
            calls["posts"].append((channel, thread_ts, text))
            return types.SimpleNamespace(id=f"p{len(calls['posts'])}")
    ns = {"json": json, "re": __import__("re"), "time": __import__("time"), "_chat": _chat, "app": App(),
          "_CARDS": Cards(), "threading": types.SimpleNamespace(Thread=_Sync),
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(json.loads(json.dumps(d)))),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
          "_verify_button_user": lambda ev: True, "_resolve_dm_channel": lambda: "dm",
          "_post_card": post_card, "_replace_buttons_with_status": replace, "FRONTEND": Frontend(),
          "handle_message_async": lambda *a, session_key="": calls["turns"].append((a, session_key)),
          "OWNER_SLACK_USER_ID": "U1",
          "_one_list": lambda: False}
    exec(compile(src[start:end], "bot[cases]", "exec"), ns)
    ns["_load_case_cards"] = lambda: json.loads(json.dumps(store))
    ns["_load_letter_cards"] = lambda: {}
    ns["_case_rows"] = lambda: {}
    ns["_case_levels"] = lambda cs: {str(c["id"]): "full" for c in cs}
    return ns, calls, store


def test_the_tick_posts_a_reviews_decision_and_a_forms_date_card(monkeypatch):
    # A form with a yes and no date anywhere gets one card for its date.
    undated = form()
    undated["parts"][0]["due"] = ""
    ns, calls, store = _bot(monkeypatch, [new_review(), undated])
    assert ns["_post_letter_cards"](force=True) == 2
    assert [c.kind for c in calls["cards"]] == ["case_ask", "case_ask"]
    assert store["cards"]["rv-2"]["kind"] == "ask" and store["cards"]["fm-1"]["kind"] == "date"
    assert calls["cards"][1].title == ":calendar: *When will you fill it in for Compliance Office?*"
    assert ns["_post_letter_cards"](force=True) == 0
    values = [f.value for c in calls["cards"] for f in c.fields]
    assert values and all("" not in v and "" not in v for v in values)
    assert any(getattr(a, "pattern", "").startswith("^case_") for a in calls["actions"])
    assert "case_which" in calls["actions"]


def test_a_press_on_a_case_card_runs_its_answer_and_turn(monkeypatch):
    ns, calls, _store = _bot(monkeypatch, [review()])
    ns["_post_letter_cards"](force=True)
    from preprocessor import case_surface as PC
    monkeypatch.setattr(PC, "answer", lambda case, choice, dest="", text="", question="": calls["answers"].append(
        (case["id"], choice, dest, text)) or {"ok": True, "said": "I am writing a draft.", "turn": "Draft rv-1."})
    ev = types.SimpleNamespace(action_id="case_draft", value={"case": "rv-1", "rows": []}, user_id="U1",
                               text="short", selected="", card=types.SimpleNamespace(channel="dm", id="m1",
                                                                                      thread_key="m1"))
    ns["handle_case"](ev)
    assert calls["answers"] == [("rv-1", "draft", "", "short")]
    (args, _key), = calls["turns"]
    assert args[3] == "Draft rv-1."
    gone = types.SimpleNamespace(action_id="case_delivered", value={"case": "rv-1", "part": "rp-gone"},
                                 user_id="U1", text="", selected="",
                                 card=types.SimpleNamespace(channel="dm", id="m9", thread_key="m9"))
    ns["handle_case"](gone)
    assert calls["status"][-1] == ("m9", ns["CASE_CARD_GONE"])
    # The press changed nothing the block shows: no second block.
    assert [c.kind for c in calls["cards"]] == ["case"]
    got = ns["_home_case"]("rv-1", "delivered", "rp1")
    assert got["ok"] and calls["answers"][-1] == ("rv-1", "delivered", "rp1", "")


def test_the_tick_posts_a_which_one_card_and_a_pick_answers_it(monkeypatch):
    q = {"id": "fq-1", "kind": "part", "case": "", "text": "Which one was it?",
         "choices": ["Form A", "Form B", "None of these"], "answer": ""}
    ns, calls, store = _bot(monkeypatch, [], questions=[q])
    assert ns["_post_letter_cards"](force=True) == 1
    assert calls["cards"][0].title == ":memo: *Which one was it?*" and store["cards"]["q:fq-1"]["kind"] == "which"
    from preprocessor import case_surface as PC
    monkeypatch.setattr(PC, "answer_which", lambda qq, pick: {"ok": True, "said": f"picked {pick}"})
    ev = types.SimpleNamespace(action_id="case_which", value={"question": "fq-1", "pick": 1}, user_id="U1",
                               text="", selected="", card=types.SimpleNamespace(channel="dm", id="m1",
                                                                                thread_key="m1"))
    ns["handle_case_which"](ev)
    assert calls["status"][-1][1].endswith("picked 1") or "picked 1" in calls["status"][-1][1]


# ── Home ──────────────────────────────────────────────────────────────

def _src(**over):
    import home_screen as H
    now = datetime(2026, 11, 23, 13, 10, tzinfo=ZONE).timestamp()
    src = H.Sources(now=now, zone=ZONE)
    for k, v in over.items():
        setattr(src, k, v)
    return src


def test_home_lists_one_item_per_case_of_each_kind_in_place_of_its_rows():
    import home_screen as H
    rows = rows_of(new_review(), form())
    src = _src(rows=rows, cases=[new_review(), form(), review()],
               replies=[{"id": "row-fp1", "conversation_id": "conv-fp1", "age_days": 2, "subject": "Disclosure"},
                        {"id": "response-x", "conversation_id": "c-x", "age_days": 2, "subject": "Intro"}])
    left = H.left(src)
    items = {x["id"]: x for x in left if x["kind"] == "case"}
    assert sorted(items) == ["case:fm-1", "case:rv-1", "case:rv-2"]
    assert not any(x["id"] == "reply:row-fp1" for x in left) and any(x["id"] == "reply:response-x" for x in left)
    assert items["case:rv-2"]["title"] == "Will you do this review? Proposal review for Jordan Park."
    assert items["case:rv-2"]["label"] == "Review request" and items["case:fm-1"]["label"] == "Form"
    # a yes with no date: Home asks for its one date
    assert items["case:fm-1"]["title"] == "When will you fill it in for Compliance Office?"
    assert items["case:fm-1"]["detail"] == "due Fri Nov 27."
    assert "row-" not in json.dumps([x for x in H.build(src)["left"] if x["kind"] == "case"])


def test_home_quick_briefing_and_choices_for_a_case():
    assert C.home_brief(review(), TODAY) == {
        "what": "Paper review for Pat Lee.", "asked": "Review it and return 1 report.", "due": "Due Tue Dec 1.",
        "standing": ["0 of 1 reports returned."], "next": "Tacitus can draft the report from your points."}
    # A new case's yes carries the date; a case whose time came offers
    # Draft and the date; the deliveries are the ready work's list.
    assert [c["id"] for c in C.home_choices(new_review(), TODAY)] == ["case:yes", "case:yes_date", "case:no"]
    assert C.home_choices(new_review(), TODAY)[0]["label"] == "Yes, by Mon Dec 14"
    started = dict(report(), do_by="2026-12-04")                    # dated, and a draft exists
    assert [c["id"] for c in C.home_choices(started, TODAY)] == ["case:draft", "case:date_pick", "open"]
    ready = dict(report(), do_by="2026-12-04")
    for d in ready["parts"][:2]:
        d["state"] = "ready"
    assert [c["id"] for c in C.home_choices(ready, TODAY)] == ["case:delivered:pp1", "case:delivered:pp2",
                                                               "case:all", "case:draft", "open"]
    assert C.home_choice("case:delivered:pp1") == ("delivered", "pp1")
    assert C.home_choice("case:submitted:pp1") == ("", "")          # a letter's word, not a case's
    assert L.home_choice("case:draft") == ("", "")                  # the letters' layer takes letters only


def _live_review(days: int = 2):
    sys.path.insert(0, str(ROOT / "tests"))
    import home_fixtures as F
    c = review()
    c["parts"][0]["due"] = (datetime.now(F.ZONE).date() + timedelta(days=days)).isoformat()
    return c


def test_home_opens_a_case_item_and_its_choice_runs_the_case_hook(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    import home_deeper
    import home_fixtures as F
    import home_items as I
    import home_screen as H
    import ledger
    # no ledger and no local model: the briefing's own fallback, whatever
    # another test left at the ledger's usual place
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "no-ledger.db")
    monkeypatch.setattr(home_deeper, "_store_path", lambda: tmp_path / "home-briefings.sqlite")
    monkeypatch.setattr(home_deeper, "local_ready", lambda cfg=None: False)
    case = _live_review()
    monkeypatch.setattr(C, "load_cases", lambda conn=None, open_only=True, kinds=None:
                        [] if kinds == ["letter"] else [case])
    src = F.sources()
    src.cases = [case]
    it = I.item(src, "case:rv-1")
    assert it["kind"] == "case" and it["label"] == "Review"
    assert it["brief"]["what"] == "Paper review for Pat Lee."
    # no card of its own in Action requests (one list, or none up yet): no
    # Open it in Action requests, a button that would lead nowhere
    assert [c["id"] for c in it["choices"]] == ["case:draft", "case:date_pick", "start"]
    f = I.find(src, "case:rv-1")
    f.item["href"] = "#/dm/t1/m1"
    assert [c["id"] for c in I.choices(f, src)] == ["case:draft", "case:date_pick", "open", "start"]
    # the briefing on a case (case_briefing), as a letter gets it; with no
    # store to read, from the case itself and kept (no name, no part)
    assert it["deeper"]["state"] == "ready" and "The reports\n- Report 1: due " in it["deeper"]["text"]
    assert "Pat Lee" not in it["deeper"]["text"] and it["deeper"]["note"] == home_deeper.NOTE_KEPT_NO_LOCAL
    called = []

    class FE:
        owner_id, main_channel, cfg = "owner", "main", {}
        store = types.SimpleNamespace(home_get=lambda n: {}, home_set=lambda n, v: None,
                                      message=lambda ch, mid: {"channel": "dm", "id": mid, "thread_key": mid},
                                      channel=lambda cid: {"id": cid})

        def home_changed(self):
            called.append("changed")

        def dm_channel(self, user):
            return "dm"
    h = H.Home(FE(), clock=lambda: F.at(13, 10), sources=lambda tz: src, background=False)
    h.hooks.case = lambda cid, answer, part, words: called.append((cid, answer, part, words)) or \
        {"ok": True, "said": "The date is set."}
    got, _after = h.act("case:rv-1", "case:date_pick", "2026-12-20")
    assert got == {"ok": True, "said": "The date is set."} and ("rv-1", "date", "", "2026-12-20") in called
    got, _after = h.act("case:rv-1", "done")
    assert got["ok"] is False
    # kept, with no local model: no model call starts, and a step it never
    # offered is refused, cleanly
    assert h.deeper("case:rv-1") == {"ok": True, "state": "unavailable", "note": home_deeper.NOTE_KEPT_NO_LOCAL}
    got, _after = h.act("case:rv-1", "step:s1")
    assert got["ok"] is False and got["said"] == I.NO_CHOICE


# ── the pre-send review, rooms, rows ──────────────────────────────────

def test_the_review_sees_a_case_with_its_parts_and_cannot_drop_it():
    import briefing_review as br
    import remote_briefing as rb
    cands = rb.candidates(_post([review(), form()]), today=TODAY)
    lists = {"top": [("C1", "*Form for Compliance Office*: due Fri Nov 27.")], "letters": [("C2", "x")]}
    items = br.collect_items(lists, cands)
    assert items[0]["work_case"]["kind"] == "form" and items[0]["work_case"]["total"] == 1
    ledger = {"rows": {}, "events": {}, "by_conv": {}, "naming": {}}
    packet, _meta = br.build_packet(agent="morning-briefing", post="p", items=items, ledger=ledger,
                                    levels={}, projects={}, drafts={}, evidence={}, calendar=[], rooms={},
                                    rules="")
    assert packet["items"][0]["work_case"]["rule"].endswith("A reply in its thread is not a submission.")
    obj = {"items": [{"ref": "C1", "action": "drop", "kind": "done", "to": "", "line": "",
                      "why": "the owner replied"},
                     {"ref": "C2", "action": "rewrite-line", "kind": "", "to": "", "line": "Done.", "why": ""}]}
    verdicts, rejected, _notes = br.check_verdicts(obj, packet, agent="morning-briefing", items=items)
    assert verdicts == {} and [r["ref"] for r in rejected] == ["C1", "C2"]
    assert rejected[0]["why"] == "a form leaves the post only when every form is submitted"
    assert rejected[1]["why"] == "a review's line is code's own"
    assert "work_case" in br.PROMPT


def test_a_room_names_a_case_by_its_kind_and_places_it_by_its_rows():
    import briefing_rooms as rooms
    import remote_briefing as rb
    cands = rb.candidates(_post([review()]), today=TODAY)
    entries = rb.room_entries(cands, {"letters": [("C1", C.post_line(cands["C1"]))]})
    assert entries[0]["title"] == "Paper review for Pat Lee" and entries[0]["label"] == ":memo: Review"
    idx = {"rows": {"row-rp1": ["journal-service"]}, "threads": {}, "loose": set(), "votes": {}, "sources": {}}
    assert rooms.placement(entries[0], idx, None) == {"projects": ["journal-service"], "by": "link", "hits": []}
    text = rooms.render("Morning briefing, Mon Nov 23", entries, [], rb._clean)
    assert ":memo: Review: *Review for Pat Lee*: report due Tue Dec 1." in text


def test_a_case_row_is_known_by_its_own_record_without_the_store():
    r = _row(review(), review()["parts"][0])
    assert C.is_case_row(r) and C.is_case_row({"id": "x", "metadata": json.dumps({"work_case": {"case": "rv-1"}})})
    assert not C.is_case_row({"id": "x", "description": "Review the memo"})
    assert C.without({"hide": {"ids": [], "convs": [], "mids": []}}, [r, {"id": "y"}])[0] == [{"id": "y"}]


def test_a_plugin_kind_gets_its_words_from_its_declaration(monkeypatch):
    decl = types.SimpleNamespace(name="grant_check", noun="Grant check", part_noun="item", part_nouns="items",
                                 delivered="checked", ladder=(5, 1, 0), loud=(0,), guide="",
                                 decision=types.SimpleNamespace(question="Will you check it?", choices=("Yes", "No"),
                                                                yes=("Yes",), no=("No",), then={}))
    fake = types.SimpleNamespace(kind=lambda n: decl if n == "grant_check" else None,
                                 kinds=lambda: {"letter": None, "grant_check": decl})
    monkeypatch.setattr(C, "_cases", lambda: fake)
    C._DECL.clear()
    w = C.words("grant_check")
    assert (w.noun, w.part, w.delivered_word, w.question, w.ladder) == ("Grant check", "item", "checked",
                                                                        "Will you check it?", (5, 1, 0))
    c = _case("gc-1", "grant_check", "", "Finance Desk", "fd@example.org",
              [_part("gp1", "Budget lines", "2026-11-28"), _part("gp2", "Receipts", "2026-11-28")])
    assert C.line(c, TODAY) == "Grant check for Finance Desk: 0 of 2 checked. Next due Sat Nov 28: 2 items."
    assert C.decision_spec(dict(c, decision=""), TODAY)["title"] == ":memo: *A grant check request.* Will you check it?"
    assert "grant_check" in C.other_kinds()
