"""An emailed deliverable stays work until delivered, not until answered."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import draft_gate
import closure
import ledger
import mail_completion
import mail_privacy
import preprocess
import remote_briefing


def row(ask, **fields):
    return {"id": "response-letter", "type": "response_needed", "status": "open",
            "source_message_id": "m-letter", "conversation_id": "c-letter",
            "description": "Recommendation request", "from_email": "portal@example.org",
            "thread_brief": {"ask": ask, "needs_reply": True, "state": "todo",
                             "messages": [{"id": "m-letter", "own": False,
                                           "received": "2026-11-10T12:00:00Z"}]}, **fields}


@pytest.mark.parametrize("ask,mode,rule", [
    ("Write and submit a letter of recommendation via the secure link.", "submission", "letter"),
    ("Acknowledge receipt of the CV and confirm the plan to write the letters of recommendation.",
     "document", "letter"),
    ("Upload the final report to the portal.", "submission", "non_email"),
    ("Complete the annual disclosure form.", "form", "non_email"),
    ("Sign the travel form.", "form", "non_email"),
    ("Prepare a revised manuscript.", "document", "non_email"),
])
def test_deliverables_never_become_replies_or_close_on_an_acknowledgement(ask, mode, rule):
    r = row(ask)
    assert mail_completion.response_mode(r) == mode
    assert ledger.reply_backlog([r], date(2026, 11, 22)) == []
    assert not ledger.closes_on_reply(r)
    held = draft_gate.check({"title": r["description"], "ask": ask, "brief": r["thread_brief"]})
    # A letter request has its own rule (letters.py); other work is non_email.
    assert held["rule"] == rule
    assert draft_gate.cron_refusal(run_id="cron-morning-1", rows=[r], decisions=[],
                                   keep_records=[]) != ""
    # The owner's explicit chat instruction to write an email still works.
    assert draft_gate.cron_refusal(run_id="chat-1", rows=[r], decisions=[]) == ""


def test_submission_declaration_beats_an_old_reply_flag_without_losing_the_task():
    r = row("Confirm receipt.", closes_on="submission")
    assert not ledger.reply_owed(r) and not ledger.closes_on_reply(r)
    assert r["status"] == "open"
    assert closure.tier("user_archived", r) == closure.PROPOSAL
    assert closure.tier("user_replied", r) == closure.PROPOSAL
    assert closure.tier("linked_send", r) == closure.PROPOSAL
    assert closure.hill_move("user_replied", r, "reply-scan", closure.PROPOSAL) == ""


def test_an_email_acknowledgement_does_not_erase_an_overdue_deliverable():
    r = row("Write and submit the recommendation through the secure link.", deadline="2026-11-20")
    r["thread_brief"]["last_from_user"] = "2026-11-21T12:00:00Z"
    assert ledger.date_spent(r, date(2026, 11, 22)) == ""


@pytest.mark.parametrize("ask,state", [
    ("Decide whether to write a recommendation and reply to confirm or decline.", "decide"),
    ("Confirm whether the recommendation was received.", "needs-reply"),
    ("Send the submission receipt number by email.", "needs-reply"),
    ("Which deadline should I use?", "needs-reply"),
    ("Confirm you will write the letter.", "needs-reply"),
    ("Write an email about the recommendation letter.", "needs-reply"),
    ("Draft a reply about the report.", "needs-reply"),
])
def test_a_real_email_question_about_a_letter_or_submission_is_preserved(ask, state):
    r = row(ask)
    r["thread_brief"]["state"] = state
    assert ledger.reply_owed(r)
    assert ledger.closes_on_reply(r)


def test_new_extraction_and_intake_track_the_work_and_keep_its_deadline():
    rec = mail_privacy.validate({"ask": "Submit the recommendation through the portal.",
                                "state": "needs-reply", "needs_reply": True,
                                "response_mode": "submission", "deadline": "2026-12-01"})
    assert rec["state"] == "todo" and rec["needs_reply"] is False
    kind = preprocess.mail_row_kind({"subject": "Letter request", "remote_record": rec})
    assert (kind["type"], kind["closes_on"], kind["deadline"]) == ("deadline", "submission", "2026-12-01")
    rec.update(response_mode="form", ask="Complete the evaluation form.", deadline="")
    kind = preprocess.mail_row_kind({"subject": "Evaluation", "remote_record": rec})
    assert (kind["type"], kind["closes_on"]) == ("task", "submission")


def test_old_mail_records_are_normalized_and_existing_briefs_are_refreshed(monkeypatch):
    monkeypatch.setattr(mail_privacy.privacy_rules, "context", lambda: {})
    monkeypatch.setattr(mail_privacy, "decision_of", lambda *a, **k: {"level": "full"})
    old = {"id": "m-letter", "received": "2026-11-10T12:00:00Z", "status": "ok",
           "result": {"ask": "Write and submit the recommendation via the secure link.",
                      "state": "todo", "needs_reply": True}}
    index = {"by_conv": {"c-letter": [old]}}
    brief = mail_privacy.thread_brief("c-letter", index, user=set())
    assert brief["response_mode"] == "submission" and brief["needs_reply"] is False
    previous = dict(brief, needs_reply=True)
    previous.pop("response_mode")
    assert mail_privacy._brief_stale(previous, brief)
    assert old["result"]["needs_reply"] is True  # the source record remains intact


def test_an_incorrect_planner_draft_is_held_as_work_without_a_reply_question():
    candidate = {"title": "Letter", "ask": "Upload the letter to the portal.",
                 "closes_on": "submission", "from": "portal@example.org"}
    gate = remote_briefing.gate(candidate, "Thanks, I will submit it.")
    assert gate["rule"] == "non_email"
    assert gate["line"] == candidate["ask"] and gate["choices"] == []
