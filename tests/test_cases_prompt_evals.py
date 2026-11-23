"""Prompt evals for the work cases beyond letters: one chat scenario per
new kind (a review, a form, a report), made-up people and places.

Each scenario checks the two halves a turn depends on, as the letters'
evals do (tests/test_letters_prompt_evals.py). First, what the model is
given: the modules prompt_logic detects for the message (with its
thread as context), the operating instructions build_claude_md assembles
from CLAUDE.md and the kind's guide, and the skills it loads; the rule
the scenario needs must be in them. Second, the code behind the rule
(case_surface: the decision card, an answer's turn), so a model that
skips the rule still cannot do the wrong thing.

  1. a review request: one decision, no reply draft; the judgment and
     the scores stay the owner's
  2. a form: fill in only what the records show; a signature is his
  3. a report: "yes, ask for the template" asks only for what is missing
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))
import case_surface as C  # noqa: E402
import prompt_logic as pl  # noqa: E402

CORE = (ROOT / "CLAUDE.md").read_text()
TODAY = date(2026, 11, 23)


def mark(name: str) -> str:
    return f"<!-- topic guide: docs/ea/{name} -->"


def flat(text: str) -> str:
    return " ".join(text.split())


def prompt_for(message: str, thread: str = "") -> tuple[set, str, list]:
    modules = pl.detect_needed_modules(message, thread)
    text = pl.build_claude_md(modules, CORE, overlay="")
    return modules, flat(text), pl.detect_skills(message, bool(thread))


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    monkeypatch.setattr(C, "_cases", lambda: None)
    monkeypatch.setattr(C, "today_local", lambda: TODAY)
    C._DECL.clear()


def _case(cid, kind, sub, who, email, part, due, *, decision="", state="asked", questions=()):
    return {"id": cid, "kind": kind, "case_kind": kind, "subkind": sub, "counterpart_name": who,
            "counterpart_email": email, "status": "open", "decision": decision, "opened": "2026-11-22",
            "materials": [], "keep_level": "full", "case_ledger_id": "",
            "parts": [{"id": f"{cid}-p", "name": part, "due": due, "method": "email", "link": "", "state": state,
                       "ledger_id": f"row-{cid}", "conversation_id": f"conv-{cid}"}],
            "questions": list(questions)}


def test_core_names_the_guides_and_keeps_its_cap():
    assert "The same for `reviews.md`, `forms.md`\n  and `reports.md`." in CORE
    assert len(CORE.encode()) <= 25_000
    for module in pl.CASE_MODULES:
        for name in pl.TOPIC_FILES[module]:
            assert (ROOT / "docs" / "ea" / name).is_file()
    tiers = json.loads((ROOT / "config" / "autonomy-tiers.json").read_text())["task_tiers"]
    assert [tiers[t]["tier"] for t in ("draft_review", "fill_form", "draft_report")] == ["ask-first"] * 3
    assert tiers["deliver_case_work"]["tier"] == "button-required"
    assert "- Declining invitations or requests" in CORE   # the safety rail stays in the core


def test_1_a_review_request_gets_one_decision_and_the_judgment_stays_his():
    msg = "The journal editor invited me to review a paper on soil sensors. The report is due Dec 14."
    modules, text, skills = prompt_for(msg)
    assert "reviews" in modules and mark("reviews.md") in text
    assert "Never draft an email reply to a review request on your own" in text
    assert "\"Will you do this review? Yes / No\"" in text
    assert "reply" not in skills, "a review request is not a reply task"
    # the thread a card or a briefing line starts makes the next message work on the case
    thread = ("Paper review for Ana Cruz. 1. Referee report: due Mon Dec 14, not answered yet, by email. "
              "Will you do this review?")
    modules, text, _ = prompt_for("Yes. Draft the report from my notes: the method is sound.", thread)
    assert "reviews" in modules and mark("reviews.md") in text
    assert "His judgment is his: the recommendation (accept, revise, reject), every score or rating" in text
    assert "Never write one." in text
    assert "Never put them in a channel post, a project room, the doc-review repo" in text
    # the code: the card asks the kind's one question; a No writes nothing it sends
    c = _case("rv-9", "review", "paper", "Ana Cruz", "ana.cruz@example.org", "Referee report", "2026-12-14",
              questions=[{"id": "rq-9", "kind": "decide", "choices": ["Yes", "No"], "answer": ""}])
    spec = C.decision_spec(c, TODAY)
    assert spec["title"].endswith("Will you do this review?")
    # its yes carries the case's one date: the date stated, or another one picked
    assert [a[0] for a in spec["actions"]] == ["case_yes", "case_yes_date", "case_no"]
    calls = []
    no = C.answer(c, "no", question="rq-9", runner=lambda argv: calls.append(argv) or {"ok": True})
    assert calls == [["answer", "--question", "rq-9", "--choice", "No"]]
    assert "as a draft that waits for his Send" in no["turn"] and "Never send it." in no["turn"]
    accepted = dict(c, decision="accepted")
    accepted["parts"][0]["state"] = "accepted"
    draft = C.answer(accepted, "draft", text="the method is sound", runner=lambda argv: {"ok": True})
    assert "His judgment, scores and recommendation are his" in draft["turn"]
    assert "never write one" in draft["turn"]


def test_2_a_form_is_filled_from_the_records_and_a_signature_is_his():
    msg = "The compliance office sent the annual disclosure form, due Friday. Fill in the form for me."
    modules, text, skills = prompt_for(msg)
    assert "forms" in modules and mark("forms.md") in text
    assert "Never submit one on your own and never sign one." in text
    assert "Every other field is a question for him: a signature, a date he attests" in text
    assert "A form's personal data (an ID number, a home address, money, health, family) never goes" in text
    c = _case("fm-9", "form", "disclosure", "Compliance Office", "co@example.edu", "Annual disclosure",
              "2026-11-27", decision="accepted", state="accepted")
    draft = C.answer(c, "draft", runner=lambda argv: {"ok": True})
    assert "Fill in only what his records show. A signature, or a fact about him the records do not show, " \
           "is a question for him." in draft["turn"]
    assert "A form's personal data never goes in a channel post" in draft["turn"]
    quiet = C.answer(dict(c, decision=""), "no", runner=lambda argv: {"ok": True})
    assert quiet == {"ok": True, "said": "You said no. Nothing is sent."}


def test_3_a_report_asks_only_for_what_is_missing():
    msg = "The program office wants our annual report by Dec 4. Yes, ask them for the template."
    modules, text, _ = prompt_for(msg)
    assert "reports" in modules and mark("reports.md") in text
    assert "a short request for only what is missing (a template, data, instructions)" in text
    assert "A fact, a number or a result the materials do not support is a question, never a sentence." in text
    c = _case("rp-9", "report", "annual", "Program Office", "po@example.gov", "Annual report", "2026-12-04",
              questions=[{"id": "pq-9", "kind": "decide", "choices": ["Yes", "Yes, ask for materials", "No"],
                          "answer": ""}])
    spec = C.decision_spec(c, TODAY)
    assert [a[1] for a in spec["actions"]] == [":white_check_mark: Yes, by Fri Dec 4", ":calendar: Yes, by another date",
                                               ":page_facing_up: Yes, ask for materials", ":no_entry_sign: No"]
    calls = []
    got = C.answer(c, "yes_ask", question="pq-9", runner=lambda argv: calls.append(argv) or {"ok": True})
    assert calls == [["answer", "--question", "pq-9", "--choice", "Yes, ask for materials", "--date", "2026-12-04"]]
    assert "Ask only for what the request did not already include" in got["turn"]
    assert "waits for his Send" in got["turn"]
