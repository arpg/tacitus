"""Prompt evals for letter cases: five chat scenarios, made-up people.

Each scenario checks the two halves a turn depends on. First, what the
model is given: the modules prompt_logic detects for the message (with
its thread as context), the operating instructions build_claude_md
assembles from CLAUDE.md and the topic guides, and the skills it loads;
the rule the scenario needs must be in them. Second, the code behind the
rule (letters_work.py on a fake store), so a model that skips the rule
still cannot do the wrong thing. The cron lane's scenario (a new request
in the briefing) is the eval fixture hard-success-letter-one-decision.

  1. a new request: one decision, no reply draft
  2. "yes I will write, send X" when X came with the request
  3. the owner dictates points
  4. a second destination arrives later
  5. a portal asks a rating question
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))
import letters_work as lw  # noqa: E402
import prompt_logic as pl  # noqa: E402
from letters_fake import FakeStore, a_case, ROWS  # noqa: E402

CORE = (ROOT / "CLAUDE.md").read_text()
AGENT = (ROOT / ".claude" / "agents" / "email-author.md").read_text()
GUIDE_MARK = "<!-- topic guide: docs/ea/letters.md -->"
# The thread a decision card or a briefing line starts.
LETTER_THREAD = ("Rowan Vale asks for a recommendation letter. For: Harbor Tech PhD (Dec 1) and "
                 "Pinecrest College MS (Dec 10). The request included: Rowan_Vale_CV.pdf and "
                 "statement.pdf. Will you write it? Yes / Yes, ask for materials / No")


def prompt_for(message: str, thread: str = "") -> tuple[set, str, list]:
    modules = pl.detect_needed_modules(message, thread)
    text = pl.build_claude_md(modules, CORE, overlay="")
    return modules, text, pl.detect_skills(message, bool(thread))


@pytest.fixture
def ea(tmp_path, monkeypatch):
    for d in ("state", "config", "letters"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(lw, "EA_DIR", tmp_path)
    monkeypatch.setattr(lw, "owner_rules", lambda: [])
    monkeypatch.setattr(lw, "_ledger_row", lambda i: dict(ROWS.get(i, {})))
    monkeypatch.setattr(lw, "_letterhead_template", lambda: None)
    return tmp_path


def test_core_names_the_guide_and_the_rule():
    assert "- *Letters* (`letters.md`): one decision per request, never a reply" in CORE
    assert "Letter uploads" in CORE and "replies and letters" in CORE
    assert len(CORE.encode()) <= 25_000


def test_1_a_new_request_gets_one_decision_and_no_reply_draft(ea):
    msg = "Rowan Vale asked me for a recommendation letter for two programs. The first is due Dec 1."
    modules, text, skills = prompt_for(msg)
    assert "lor" in modules and GUIDE_MARK in text
    assert "Never draft an email reply to a letter request" in text
    assert "Will you write it? Yes / Yes, ask for" in text
    assert "reply" not in skills, "a letter request is not a reply task"
    # The code: before the owner answers, status offers the decision and
    # decide refuses to act without the owner's words.
    st = FakeStore([a_case()])
    assert lw.status(st, "lc-1")["decision"].endswith("Will you write it? Yes / Yes, ask for materials / No")
    with pytest.raises(lw.LetterError):
        lw.decide(st, "lc-1", "materials", "")
    assert st.cases["lc-1"]["decision"] == ""


def test_2_yes_send_x_when_x_was_attached(ea):
    msg = "Yes I will write it. Ask her to send her CV and statement."
    modules, text, _ = prompt_for(msg, LETTER_THREAD)
    assert "lor" in modules and GUIDE_MARK in text, "the thread makes this a letter turn"
    assert "--answer yes|materials|no" in text and "Always tell the owner `already_received`" in text
    st = FakeStore([a_case(materials=[{"kind": "cv", "ref": "MSG-REQ#Rowan_Vale_CV.pdf"},
                                      {"kind": "statement", "ref": "MSG-REQ#statement.pdf"}])])
    out = lw.decide(st, "lc-1", "materials", msg, want=lw.parse_want("CV and statement"))
    assert out["email"] == "none" and out["missing"] == []
    assert "already sent" in out["owner_line"] and "Rowan_Vale_CV.pdf" in out["owner_line"]


def test_3_the_owner_dictates_points(ea):
    msg = ("For Rowan's letter: she ran the evening shift for a year and trained two new hires. "
           "Put that in the letter for Rowan.")
    modules, text, skills = prompt_for(msg)
    assert "lor" in modules and "compose-letter" in skills
    assert "word for word" in text and "--notes-file" in text
    assert "a fact the materials do not support is a question" in text
    assert "[ASK: <the question>]" in AGENT and "Never write\na rating" in AGENT
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "accepted"
    st = FakeStore([c])
    notes = ea / "state" / "rowan-notes.txt"
    notes.write_text("She ran the evening shift for a year and trained two new hires.\n")
    out = lw.brief(st, "lc-1", notes_file=str(notes), owner_said=msg)
    brief = Path(out["brief"]).read_text()
    assert "She ran the evening shift for a year and trained two new hires." in brief
    assert "Never invent a project, a number, a comparison or a rank." in brief


def test_4_a_second_destination_arrives_later(ea):
    msg = "Another portal mail for Rowan's letter: Pinecrest College MS, due Dec 10."
    modules, text, _ = prompt_for(msg, LETTER_THREAD)
    assert "lor" in modules
    assert "It needs no new\n  decision" in text or "needs no new decision" in text.replace("\n  ", " ")
    c = a_case(decision="accepted")
    c["destinations"][0]["state"] = "ready"
    st = FakeStore([c])
    base = lw.case_dir(st.case("lc-1")) / "letter.md"
    base.write_text("Dear {{addressee}},\n\nI recommend Rowan Vale for {{program}}.\n\nSincerely,\nKit\n")
    st.cases["lc-1"]["base_letter"] = str(base)
    out = lw.extend(st, "lc-1", "d2")
    copy = Path(out["variant"]["variants"][0]["file"]).read_text()
    assert "Pinecrest College MS" in copy and "Harbor" not in copy
    assert st.cases["lc-1"]["decision"] == "accepted", "no new decision"


def test_5_a_portal_asks_a_rating(ea, monkeypatch):
    msg = "The portal asks me to rate her research ability: top 1%, 5%, 10% or 25%."
    modules, text, _ = prompt_for(msg, LETTER_THREAD)
    assert "lor" in modules
    assert "Ratings and form\n  answers are the owner's" in text and "Never guess one." in text
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "drafted"
    st = FakeStore([c])
    lw.require(st, "lc-1", "d1", question="Research ability", options="Top 1%|Top 5%|Top 10%|Top 25%",
               rating=True)
    base = lw.case_dir(st.case("lc-1")) / "letter.md"
    base.write_text("Dear Committee,\n\nRowan is careful.\n\nSincerely,\nKit\n")
    st.cases["lc-1"]["base_letter"] = str(base)
    monkeypatch.setattr(lw, "_compile", lambda tex, pdf: pdf.write_bytes(b"%PDF") or True)
    lw.render(st, "lc-1", str(base))
    out = lw.approve(st, "lc-1", "it reads fine")
    assert "d1" in [b["dest"] for b in out["blocked"]], "no rating, no approval"
    brief_q = lw.status(st, "lc-1")["questions"]
    assert brief_q[0]["rating"] and brief_q[0]["options"][0] == "Top 1%"
    with pytest.raises(lw.LetterError):
        lw.answered(st, "lc-1", "d1", "q1", "Top 5%", "")
    lw.answered(st, "lc-1", "d1", "q1", "Top 5%", "top five percent")
    assert "d1" in [r["dest"] for r in lw.approve(st, "lc-1", "it reads fine")["ready"]]


def test_the_letter_text_stays_out_of_every_output(ea, monkeypatch):
    secret = "an-unrepeatable-phrase"
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "drafted"
    st = FakeStore([c])
    base = lw.case_dir(st.case("lc-1")) / "letter.md"
    base.write_text(f"Dear Committee,\n\nRowan is {secret}.\n\nSincerely,\nKit\n")
    st.cases["lc-1"]["base_letter"] = str(base)
    monkeypatch.setattr(lw, "_compile", lambda tex, pdf: pdf.write_bytes(b"%PDF") or True)
    outs = [lw.check(st, "lc-1", str(base), record=True), lw.variants(st, "lc-1", str(base)),
            lw.render(st, "lc-1", str(base)), lw.approve(st, "lc-1", "ready"),
            lw.submit(st, "lc-1", "d2"), lw.status(st, "lc-1")]
    assert secret not in json.dumps(outs)
    assert "Never paste the letter text" in prompt_for("draft the letter for Rowan")[1]
