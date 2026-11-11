#!/usr/bin/env python3
"""Unit tests for tests/evals/judge.py, the assertion functions the
full-mode eval suite scores briefing output with.

A judge with no tests of its own cannot be told apart from the model it
judges: a substring match on "action needed" inside "no action needed"
fails a briefing that was right, night after night, and the failure
looks like the model's.

Usage:
    python3 tests/test_eval_judge.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "evals"))
# The bot's online rule reads the institution's domains from the profile.
_EA = Path(tempfile.mkdtemp(prefix="ea-test-eval-judge-"))
(_EA / "config").mkdir()
(_EA / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
os.environ["EA_DIR"] = str(_EA)

import judge  # noqa: E402

FIXTURE = REPO / "tests" / "evals" / "fixtures" / "already-replied-thread.json"
CTX = {"vars": {"fixture_path": str(FIXTURE)}}


def _run(output: str) -> dict:
    return judge.already_replied_informational(output, CTX)


# ── the regression-002 false positive ────────────────────────────────

def test_negated_action_phrases_are_not_reply_suggestions():
    out = (":sunny: *Morning Briefing — September 5, 2026*\n\n"
           ":dart: *Get done today*\n"
           "1. Reply to Harper Ravensworth about the ICRA workshop fee ($150).\n\n"
           ":white_check_mark: *Quietly handled*: Jo Carrow confirmed "
           "she'll present the LiDAR results Thursday; you already replied, "
           "no action needed. Nothing to respond to there.\n")
    r = _run(out)
    assert r["pass"], r["reason"]


def test_negations_in_every_house_style():
    for tail in ("no action needed", "no response needed", "nothing to respond to",
                 "doesn't need a reply", "you needn't respond to it",
                 "already replied, no reply needed", "not actionable",
                 "no need to reply to Jo", "action required: none"):
        out = ("Harper Ravensworth asks about the fee. Jo Carrow confirmed the "
               f"LiDAR slot ({tail}).")
        r = _run(out)
        assert r["pass"], (tail, r["reason"])


def test_a_real_reply_suggestion_still_fails():
    for tail in ("draft a reply to confirm", "she needs a response by Friday",
                 "respond to her today", "action required: confirm the slot",
                 "reply to Jo about the room"):
        out = f"Harper Ravensworth asks about the fee. Jo Carrow: {tail}."
        r = _run(out)
        assert not r["pass"], (tail, r["reason"])
        assert "jo carrow" in r["reason"] and ":" in r["reason"], r["reason"]


def test_the_question_sender_must_still_be_mentioned():
    r = _run("Jo Carrow confirmed the slot; no action needed.")
    assert not r["pass"] and "harper ravensworth" in r["reason"], r


def test_reply_suggestions_helper_returns_the_snippet():
    hits = judge._reply_suggestions("please respond to her by noon")
    assert hits and "respond to" in hits[0]
    assert judge._reply_suggestions("no need to respond to her") == []
    assert judge._reply_suggestions("she was not asking; nothing for you to reply to") == []
    # a negation in an EARLIER clause does not launder a later suggestion
    assert judge._reply_suggestions("no thanks. draft a reply to her") != []


# ── briefing_format_valid names the emoji it found ───────────────────

def test_unicode_emoji_reason_names_the_character():
    out = ":sunny: *Morning Briefing — September 5, 2026*\n📌 pinned item\n"
    r = judge.briefing_format_valid(out, {})
    assert not r["pass"]
    assert "U+1F4CC" in r["reason"] and "'📌'" in r["reason"], r["reason"]
    clean = ":sunny: *Morning Briefing — September 5, 2026*\n:pushpin: pinned item\n"
    assert judge.briefing_format_valid(clean, {})["pass"]


def test_poll_reply_that_rules_out_reply_all_passes():
    """Every nightly poll-reply failure in September named reply-all only to
    rule it out; that is the behaviour the judge asks for."""
    for text in ("Proposed reply (sender-only, not reply-all): the poll went to 6 people.",
                 "Reply *send lane* to draft it to her only (not reply-all) for the poll.",
                 "Send her your pick alone, not reply-all to the group; the poll closes Friday.",
                 "I drafted it to Lane rather than reply-all on the poll."):
        assert judge.poll_reply_sender_only(text, {})["pass"], text


def test_poll_reply_all_that_is_proposed_fails():
    for text in ("I drafted a reply-all to the poll with your three times.",
                 "Reply all to the scheduling poll with Mon 9-10."):
        r = judge.poll_reply_sender_only(text, {})
        assert not r["pass"] and "reply" in r["reason"].lower(), text


def test_double_asterisk_bold_is_not_a_format_failure():
    """The web chat renders **double** and *single* bold alike
    (mrkdwn_to_markdown), and production posts either, so only real format
    breaches fail."""
    out = (":sunny: *Morning Briefing \u2014 September 24, 2026*\n\n"
           ":question: *Needs you*\n- Say **rerun** once fresh data is in.")
    r = judge.briefing_format_valid(out, {})
    assert r["pass"], r["reason"]
    r = judge.briefing_format_valid("Here is a summary of today.", {})
    assert not r["pass"]


ZOOM_FIXTURE = str(REPO / "tests" / "evals" / "fixtures" / "zoom-not-teams.json")
ZOOM_EVENT = {"subject": "Collaboration chat", "start": "2026-09-29T10:00:00",
              "end": "2026-09-29T10:30:00", "attendees": "avery.calloway88@example.org",
              "location": "", "body": "", "online": True}
BRIEFING = ":sunny: *Morning Briefing \u2014 September 24, 2026*\nStaged the invite."


def _zoom(staged, leaks=()):
    ctx = {"vars": {"fixture_path": ZOOM_FIXTURE}, "providerResponse": {"metadata": {
        "stages_events": True, "staged_event": staged,
        "production_state_writes": list(leaks)}}}
    return judge.zoom_not_teams(BRIEFING, ctx)


def test_a_staged_invite_that_gets_the_zoom_link_passes():
    assert _zoom(ZOOM_EVENT)["pass"]
    assert _zoom([ZOOM_EVENT])["pass"], "a list of events is the multi-event shape"
    # no online key: the bot infers Zoom for an external attendee and no location
    assert _zoom(dict(ZOOM_EVENT, online=None))["pass"]


def test_a_staged_invite_without_zoom_or_naming_teams_fails():
    cases = {
        "no invite staged": None,
        "would not attach zoom": dict(ZOOM_EVENT, online=False),
        "would not attach zoom (physical room)": dict(ZOOM_EVENT, online=None,
                                                      location="Room 831"),
        "names teams": dict(ZOOM_EVENT, location="Microsoft Teams"),
        "does not include": dict(ZOOM_EVENT, attendees="someone@example.edu"),
        "not valid json": {"_raw": "{broken"},
    }
    for label, staged in cases.items():
        r = _zoom(staged)
        assert not r["pass"], label
    assert "production state" in _zoom(ZOOM_EVENT, leaks=["pending-event.json"])["reason"]


def test_the_judge_uses_the_bots_own_online_rule():
    assert judge._bot_infer_online({"attendees": "someone@example.org"}) is True
    assert judge._bot_infer_online({"attendees": "someone@example.edu"}) is False
    assert judge._bot_infer_online({"location": "Zoom", "attendees": "someone@example.edu"}) is True


# ── hard-success-007: "no new draft" is the right answer, not a failure ──

REVISION_CTX = {"vars": {"fixture_path": str(
    REPO / "tests" / "evals" / "fixtures" / "draft-revision-no-new.json")}}


def test_a_denied_new_draft_is_not_a_created_one():
    out = (":sunny: *Morning Briefing — October 15, 2026*\n\n"
           "1. Cy Rasmussen, GPU cluster availability. I will update the same "
           "draft (AAMk-draft-cy-gpu-001) with update-draft, not create-reply.\n\n"
           ":white_check_mark: *Quietly handled*: No new mail, no closures, "
           "no new drafts this run.\n\n"
           ":envelope: *Drafts ready* (0). No new draft was created this run.\n")
    r = judge.draft_revision_uses_update(out, REVISION_CTX)
    assert r["pass"], r["reason"]
    for tail in ("The closing changed on the existing draft; a new draft was not needed.",
                 "Revised in place rather than a new draft.",
                 "Used update-draft instead of create-reply."):
        r = judge.draft_revision_uses_update("1. Cy Rasmussen. " + tail, REVISION_CTX)
        assert r["pass"], (tail, r["reason"])


def test_a_created_new_draft_still_fails():
    for tail in ("I created a new draft to Cy with the new closing.",
                 "Ran create-reply for Cy with 'Thanks, Kit'.",
                 "A new draft is in the Drafts folder."):
        r = judge.draft_revision_uses_update("1. Cy Rasmussen. " + tail, REVISION_CTX)
        assert not r["pass"], tail
        assert "new draft" in r["reason"].lower() or "create-reply" in r["reason"].lower()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
