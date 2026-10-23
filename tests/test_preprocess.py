#!/usr/bin/env python3
"""
Tests for preprocessor scheduling detection heuristics.

Covers SCHEDULING_PATTERN (positive detection) and SCHEDULING_DONE_PATTERNS
(false-positive suppression for already-scheduled emails).

Usage:
    python3 tests/test_preprocess.py
    python3 tests/test_preprocess.py --verbose
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# No test may touch production state. preprocess.py does STATE_DIR.mkdir()
# at import, so EA_DIR must be a sandbox BEFORE the import below; unset, it
# resolves to the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-preprocess-")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from preprocessor.preprocess import (
    SCHEDULING_PATTERN,
    SCHEDULING_DONE_PATTERNS,
    detect_scheduling_requests,
)


# -------------------------------------------------------------------
# Pattern-level tests
# -------------------------------------------------------------------

# Emails where scheduling is DONE — should be suppressed
SCHEDULING_DONE_CASES = [
    ("harper-when2meet-done",
     "Good morning, and thank you to those who filled out the When2Meet. "
     "I went ahead and scheduled a meeting for tomorrow at 2 p.m.",
     "Real case: Harper reporting meeting already scheduled"),
    ("meeting-has-been-scheduled",
     "The meeting has been scheduled for Friday at 3pm. See you there.",
     "Passive voice: already done"),
    ("i-have-scheduled",
     "I have scheduled a call with the team for next Monday.",
     "Present perfect: already done"),
    ("already-scheduled",
     "I already scheduled the review meeting, calendar invite incoming.",
     "Explicit 'already'"),
    ("thanks-filling-out-doodle",
     "Thanks everyone for filling out the doodle! The best time is Wed 10am.",
     "Thanking for completed poll"),
    ("went-ahead-and-booked",
     "I went ahead and booked a room for Tuesday's discussion.",
     "Went ahead and booked"),
    ("defense-is-scheduled",
     "The defense is scheduled for July 8 at 2pm in ECCS 1B12.",
     "Event is scheduled for [date]"),
    ("filled-out-the-poll",
     "Everyone has filled out the poll so I'm picking Thursday 3pm.",
     "Filled out the poll (past tense)"),
    ("meeting-is-set",
     "Meeting is set for tomorrow at noon. Zoom link in the invite.",
     "Meeting is set"),
    ("we-have-scheduled",
     "We have scheduled the kickoff for April 15.",
     "We have scheduled"),
    ("went-ahead-set-up",
     "I went ahead and set up a time for us to meet on Friday.",
     "Went ahead and set up"),
    ("already-filled-out-whenisgood",
     "I already filled out the WhenIsGood, looks like Monday works best.",
     "Already filled out [platform]"),
    ("ive-scheduled",
     "I've scheduled us for 2pm on Thursday. Let me know if that doesn't work.",
     "Contraction: I've scheduled"),
]

# Emails with real scheduling REQUESTS — should NOT be suppressed
SCHEDULING_REQUEST_CASES = [
    ("fill-out-when2meet",
     "Can you fill out the when2meet for the committee meeting? "
     "https://www.when2meet.com/?12345-abcde",
     "Direct request to fill poll"),
    ("when-are-you-free",
     "When are you free next week? Let me know your available times.",
     "Asking for availability"),
    ("can-we-schedule",
     "Can we schedule a meeting to discuss the proposal?",
     "Requesting to schedule"),
    ("check-availability-doodle",
     "Please check your availability for a doodle poll I created.",
     "Request to check poll"),
    ("what-times-work",
     "What times work for you? I set up a when2meet.",
     "Asking for times + poll link"),
    ("would-tuesday-work",
     "Would Tuesday at 2pm work for you?",
     "Proposing a specific time"),
    ("find-a-time",
     "Let's find a time to meet about the grant. Are you open this week?",
     "Find a time"),
    ("set-up-meeting",
     "I'd like to set up a meeting with you and the grad students.",
     "Set up a meeting (future intent)"),
    ("open-on-calendar",
     "Do you have anything open on your calendar Wednesday afternoon?",
     "Checking calendar openings"),
    ("please-fill-out-timeful",
     "Hi Kit, please fill out https://timeful.app/e/abc123 for my defense scheduling.",
     "Direct request with Timeful link"),
]


def run_pattern_tests(verbose: bool = False) -> tuple[int, int, list]:
    """Test SCHEDULING_DONE_PATTERNS precision.

    Returns (passed, total, failures).
    """
    passed = 0
    total = 0
    failures = []

    # Done cases: SCHEDULING_PATTERN should match (they contain keywords),
    # AND SCHEDULING_DONE_PATTERNS should also match (suppressing them).
    for case_id, text, note in SCHEDULING_DONE_CASES:
        total += 1
        preview = text.lower()
        triggers = bool(SCHEDULING_PATTERN.search(preview))
        suppressed = bool(SCHEDULING_DONE_PATTERNS.search(preview))

        if not triggers:
            # Doesn't even trigger scheduling detection — not relevant to test
            passed += 1
            if verbose:
                print(f"  SKIP  {case_id}: no scheduling trigger (OK)")
            continue

        if suppressed:
            passed += 1
            if verbose:
                print(f"  PASS  {case_id}: correctly suppressed")
        else:
            failures.append({
                "case_id": case_id,
                "expected": "suppressed (scheduling done)",
                "actual": "NOT suppressed (would false-positive)",
                "note": note,
                "text": text[:80],
            })
            if verbose:
                print(f"  FAIL  {case_id}: should be suppressed but wasn't")

    # Request cases: SCHEDULING_DONE_PATTERNS should NOT match
    for case_id, text, note in SCHEDULING_REQUEST_CASES:
        total += 1
        preview = text.lower()
        suppressed = bool(SCHEDULING_DONE_PATTERNS.search(preview))

        if not suppressed:
            passed += 1
            if verbose:
                print(f"  PASS  {case_id}: correctly kept as request")
        else:
            failures.append({
                "case_id": case_id,
                "expected": "kept (real request)",
                "actual": "SUPPRESSED (false negative)",
                "note": note,
                "text": text[:80],
            })
            if verbose:
                print(f"  FAIL  {case_id}: real request incorrectly suppressed")

    return passed, total, failures


# -------------------------------------------------------------------
# End-to-end detect_scheduling_requests tests
# -------------------------------------------------------------------

def _mock_email(msg_id: str, subject: str, body_preview: str,
                from_addr: str = "someone@example.edu",
                conv_id: str = "") -> dict:
    """Build a mock Outlook email object."""
    return {
        "id": msg_id,
        "subject": subject,
        "bodyPreview": body_preview,
        "from": {"emailAddress": {"address": from_addr, "name": "Someone"}},
        "receivedDateTime": "2026-04-07T14:00:00Z",
        "conversationId": conv_id or f"conv-{msg_id}",
    }


E2E_CASES = [
    {
        "id": "e2e-done-not-detected",
        "inbox": [_mock_email(
            "msg-done-1", "Re: Program review / Ashcombe / Congrats",
            "Good morning, and thank you to those who filled out the When2Meet. "
            "I went ahead and scheduled a meeting for tomorrow at 2 p.m.",
            from_addr="ira.thornbury32@example.edu",
        )],
        "sent": [],
        "expect_count": 0,
        "note": "Already-scheduled email should not appear as scheduling request",
    },
    {
        "id": "e2e-real-request-detected",
        "inbox": [_mock_email(
            "msg-req-1", "PhD Defense Scheduling",
            "Hi Kit, please fill out the when2meet for my defense: "
            "https://www.when2meet.com/?12345",
            from_addr="ode.stavros28@example.edu",
        )],
        "sent": [],
        "expect_count": 1,
        "note": "Real scheduling request should be detected",
    },
    {
        "id": "e2e-already-replied-skipped",
        "inbox": [_mock_email(
            "msg-replied-1", "Meeting scheduling",
            "When are you free next week?",
            from_addr="sol.dunmore11@example.edu",
            conv_id="conv-already-replied",
        )],
        "sent": [{"conversationId": "conv-already-replied"}],
        "expect_count": 0,
        "note": "Email in a conversation we already replied to should be skipped",
    },
    {
        "id": "e2e-noreply-skipped",
        "inbox": [_mock_email(
            "msg-noreply-1", "Your meeting is scheduled",
            "Schedule a meeting with your team using Calendly",
            from_addr="noreply@calendly.com",
        )],
        "sent": [],
        "expect_count": 0,
        "note": "Noreply sender should be skipped",
    },
    {
        "id": "e2e-mixed-inbox",
        "inbox": [
            _mock_email(
                "msg-done-2", "Re: Committee meeting",
                "I've scheduled the committee meeting for Friday. Thanks for the When2Meet responses.",
                from_addr="nico.lindqvist3@example.edu",
            ),
            _mock_email(
                "msg-req-2", "Scheduling our 1:1",
                "Can we find a time to meet next week? What times work for you?",
                from_addr="emerson.tremaine39@example.edu",
            ),
            _mock_email(
                "msg-unrelated", "Paper draft",
                "Here is the latest draft of the paper. Please review by Friday.",
                from_addr="yuki.ambrose32@example.edu",
            ),
        ],
        "sent": [],
        "expect_count": 1,
        "note": "Only the real request should be detected, not the done email or unrelated",
    },
]


def run_e2e_tests(verbose: bool = False) -> tuple[int, int, list]:
    """Test detect_scheduling_requests end-to-end with mock emails."""
    passed = 0
    total = 0
    failures = []

    for case in E2E_CASES:
        total += 1
        result = detect_scheduling_requests(case["inbox"], case["sent"])
        actual_count = len(result)
        expected_count = case["expect_count"]
        by_id = {m["id"]: m for m in case["inbox"]}
        idless = [r for r in result
                  if r.get("conversation_id") != by_id[r["message_id"]].get("conversationId", "")]
        if idless:
            failures.append({"case_id": case["id"] + "/conversation_id",
                             "expected": "every row carries its message's conversationId",
                             "actual": f"{len(idless)} row(s) without it", "note": case["note"],
                             "text": ""})

        if actual_count == expected_count:
            passed += 1
            if verbose:
                print(f"  PASS  {case['id']}: {actual_count} requests (expected {expected_count})")
        else:
            failures.append({
                "case_id": case["id"],
                "expected": f"{expected_count} scheduling requests",
                "actual": f"{actual_count} scheduling requests",
                "note": case["note"],
                "text": str([m["subject"] for m in case["inbox"]]),
            })
            if verbose:
                print(f"  FAIL  {case['id']}: got {actual_count}, expected {expected_count}")
                for r in result:
                    print(f"        detected: {r['subject']}")

    return passed, total, failures


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Preprocessor scheduling detection tests")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    print("Preprocessor Scheduling Detection Tests")
    print("=" * 60)

    all_passed = 0
    all_total = 0
    all_failures = []

    # Pattern tests
    print("\nPattern tests (SCHEDULING_DONE_PATTERNS):")
    p, t, f = run_pattern_tests(verbose=args.verbose)
    all_passed += p
    all_total += t
    all_failures.extend(f)
    print(f"  {p}/{t} passed")

    # E2E tests
    print("\nEnd-to-end tests (detect_scheduling_requests):")
    p, t, f = run_e2e_tests(verbose=args.verbose)
    all_passed += p
    all_total += t
    all_failures.extend(f)
    print(f"  {p}/{t} passed")

    # Summary
    print()
    print("=" * 60)
    pct = (all_passed / all_total * 100) if all_total else 0
    status = "PASS" if not all_failures else "FAIL"
    print(f"OVERALL: {all_passed}/{all_total} ({pct:.1f}%)  [{status}]")

    if all_failures:
        print()
        print("FAILURES:")
        print("-" * 60)
        for f in all_failures:
            print(f"\n  [{f['case_id']}]")
            print(f"    expected: {f['expected']}")
            print(f"    actual:   {f['actual']}")
            print(f"    note: {f['note']}")
        print()

    sys.exit(1 if all_failures else 0)



def test_reschedule_requests_are_detected_and_tagged():
    """A "can we move Tuesday?" email is a scheduling request (it used to
    miss the keyword list entirely) and carries kind="reschedule"; a fresh
    "when are you free?" stays kind="new"."""
    from preprocessor.preprocess import classify_scheduling_kind
    assert classify_scheduling_kind("Sorry, Tuesday no longer works for me. Could we reschedule?") == "reschedule"
    assert classify_scheduling_kind("Something came up, can we push our meeting to Thursday?") == "reschedule"
    assert classify_scheduling_kind("I can't make it tomorrow, is there another time?") == "reschedule"
    assert classify_scheduling_kind("When are you free next week for a quick chat?") == "new"
    inbox = [{
        "id": "msg-move-1", "conversationId": "conv-move-1",
        "subject": "Re: Oakley / Kit", "receivedDateTime": "2026-09-20T15:00:00Z",
        "bodyPreview": "Hi Kit, I need to reschedule our Tuesday meeting, a conflict came up.",
        "from": {"emailAddress": {"address": "wren.abernathy53@example.edu", "name": "Oakley Stavros"}},
    }, {
        "id": "msg-new-1", "conversationId": "conv-new-1",
        "subject": "Coffee?", "receivedDateTime": "2026-09-20T15:05:00Z",
        "bodyPreview": "When are you free next week? Would love to catch up.",
        "from": {"emailAddress": {"address": "flo.thornbury67@example.org", "name": "A"}},
    }]
    found = {r["message_id"]: r for r in detect_scheduling_requests(inbox, [])}
    assert found["msg-move-1"]["kind"] == "reschedule"
    assert found["msg-new-1"]["kind"] == "new"


def test_proactive_queue_merges_and_expires():
    """The queue is a union by conversation id, never an overwrite (two
    writers minutes apart must not drop an unopened entry), entries older
    than three days fall off, and nothing is written without a scheduling
    channel configured."""
    from preprocessor.preprocess import write_proactive_queue
    from datetime import datetime, timezone
    with tempfile.TemporaryDirectory() as td:
        q = Path(td) / "queue.json"
        now = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
        old_env = os.environ.get("EA_SCHED_CHANNEL_ID")
        os.environ["EA_SCHED_CHANNEL_ID"] = ""
        try:
            assert write_proactive_queue([{"conversation_id": "c1", "received": "2026-09-20T17:00:00Z"}], q, now) == 0
            assert not q.exists()
            os.environ["EA_SCHED_CHANNEL_ID"] = "C123"
            assert write_proactive_queue([{"conversation_id": "c1", "received": "2026-09-20T17:00:00Z"}], q, now) == 1
            n = write_proactive_queue([{"conversation_id": "c2", "received": "2026-09-20T17:30:00Z"},
                                       {"conversation_id": "c1", "received": "2026-09-20T17:00:00Z", "kind": "reschedule"}], q, now)
            assert n == 2
            data = {r["conversation_id"]: r for r in json.loads(q.read_text())}
            assert set(data) == {"c1", "c2"} and data["c1"]["kind"] == "reschedule"   # later write wins the row
            n = write_proactive_queue([{"conversation_id": "c3", "received": "2026-09-16T17:00:00Z"}], q, now)
            assert n == 2 and "c3" not in {r["conversation_id"] for r in json.loads(q.read_text())}
        finally:
            if old_env is None:
                os.environ.pop("EA_SCHED_CHANNEL_ID", None)
            else:
                os.environ["EA_SCHED_CHANNEL_ID"] = old_env

def test_scheduling_channel_follows_the_chat_frontend():
    """On Zulip the scheduling stream in the chat config is the channel; the
    Slack channel id in .env counts only on Slack."""
    from preprocessor.preprocess import sched_channel_configured
    old_env = os.environ.get("EA_SCHED_CHANNEL_ID")
    os.environ["EA_SCHED_CHANNEL_ID"] = "C123"
    try:
        zulip = {"frontend": "zulip", "zulip": {"sched_stream": "ea-scheduling"}}
        assert sched_channel_configured(zulip)
        assert not sched_channel_configured({"frontend": "zulip", "zulip": {"sched_stream": ""}})
        assert sched_channel_configured({"frontend": "slack"})
        os.environ["EA_SCHED_CHANNEL_ID"] = ""
        assert not sched_channel_configured({"frontend": "slack"})
        assert sched_channel_configured(zulip)
    finally:
        if old_env is None:
            os.environ.pop("EA_SCHED_CHANNEL_ID", None)
        else:
            os.environ["EA_SCHED_CHANNEL_ID"] = old_env


def test_mail_row_kind_types_the_row_by_the_remote_record():
    """Without a record the row is the response row it always was. With one,
    a dated todo is a deadline row, an undated todo a task row, a plain ask
    a response row; a nudge about owed work tracks the work, not the nudge."""
    from preprocessor.preprocess import mail_row_kind, MAIL_ROW_PREFIXES
    base = {"subject": "Communication regarding Manuscript ID JRNL-26-7385",
            "body_preview": "Pat Lee asks for the recommendation."}
    k = mail_row_kind(base)
    assert (k["prefix"], k["type"], k["deadline"]) == ("response-", "response_needed", "")
    assert k["description"].startswith("Needs response: ")
    k = mail_row_kind(dict(base, remote_record={"state": "todo", "needs_reply": True,
                                                 "deadline": "2026-10-15", "ask": "Submit the recommendation."}))
    assert (k["prefix"], k["type"], k["deadline"]) == ("deadline-", "deadline", "2026-10-15")
    assert k["description"] == "Due 2026-10-15: Communication regarding Manuscript ID JRNL-26-7385"
    assert k["notes"].endswith("Ask: Submit the recommendation.")
    # a dated reminder that asks for no reply still tracks the date
    k = mail_row_kind(dict(base, remote_record={"state": "fyi", "needs_reply": False, "deadline": "2026-10-07"}))
    assert (k["prefix"], k["type"]) == ("deadline-", "deadline")
    # a dated question put to the user is a reply owed by then, not a task
    k = mail_row_kind(dict(base, remote_record={"state": "needs-reply", "needs_reply": True, "deadline": "2026-10-07"}))
    assert (k["prefix"], k["type"]) == ("response-", "response_needed")
    k = mail_row_kind(dict(base, remote_record={"state": "decide", "needs_reply": False, "deadline": ""}))
    assert (k["prefix"], k["type"], k["description"]) == ("task-", "task", "Task: " + base["subject"])
    assert set(MAIL_ROW_PREFIXES) == {"response-", "task-", "deadline-"}


def test_a_record_that_accepts_an_offered_time_is_a_scheduling_request_even_after_the_user_wrote():
    """The keyword pass skips a thread the user has written to, so an
    acceptance of a time the user offered never surfaced. With the mail
    gate on, the remote record's scheduling kind makes it a request: kind
    "confirmation" with the accepted time; a message the user answered
    afterwards is not one; a record of kind "none" adds nothing."""
    from preprocessor.preprocess import _sched_description
    inbox = [{
        "id": "msg-accept-1", "conversationId": "conv-intro-1",
        "subject": "Re: Intro", "receivedDateTime": "2026-10-22T22:53:00Z",
        "bodyPreview": "Wed Nov 4, 2:45-3:15 PM MT is best; I probably just need 15 min.",
        "from": {"emailAddress": {"address": "morgan.vasquez41@example.edu", "name": "Morgan"}},
    }, {
        "id": "msg-answered-1", "conversationId": "conv-old-1",
        "subject": "Re: Coffee", "receivedDateTime": "2026-10-20T10:00:00Z",
        "bodyPreview": "Tuesday at 2 works.",
        "from": {"emailAddress": {"address": "flo.thornbury67@example.org", "name": "Flo"}},
    }, {
        "id": "msg-fyi-1", "conversationId": "conv-fyi-1",
        "subject": "Notes", "receivedDateTime": "2026-10-22T12:00:00Z", "bodyPreview": "Here are the notes.",
        "from": {"emailAddress": {"address": "sam.okafor12@example.org", "name": "Sam"}},
    }, {
        "id": "msg-cal-1", "conversationId": "conv-cal-1",
        "subject": "Accepted: Lab meeting", "receivedDateTime": "2026-10-22T12:30:00Z", "bodyPreview": "",
        "from": {"emailAddress": {"address": "sam.okafor12@example.org", "name": "Sam"}},
    }]
    sent = [{"conversationId": "conv-intro-1", "sentDateTime": "2026-10-21T22:32:00Z"},
            {"conversationId": "conv-old-1", "sentDateTime": "2026-10-20T11:00:00Z"}]
    records = {"msg-accept-1": {"state": "scheduling", "scheduling": "confirmation",
                                "offered_times": ["2026-11-04T14:45:00-07:00"],
                                "ask": "Confirm the Wed Nov 4 call and share the phone number."},
               "msg-answered-1": {"state": "scheduling", "scheduling": "confirmation", "offered_times": ["2026-10-21T14:00:00-06:00"]},
               "msg-fyi-1": {"state": "fyi", "scheduling": "none"},
               "msg-cal-1": {"state": "scheduling", "scheduling": "confirmation", "offered_times": ["2026-10-23T10:00:00-06:00"]}}

    class Gate:
        on = True
        def record_fields(self, item):
            return records.get(item.get("message_id"))
    assert detect_scheduling_requests(inbox, sent) == [], "the keyword pass sees nothing here"
    found = detect_scheduling_requests(inbox, sent, gate=Gate())
    assert [r["message_id"] for r in found] == ["msg-accept-1"], "Outlook's Accepted: reply is not a request"
    r = found[0]
    assert r["kind"] == "confirmation" and r["offered_times"] == ["2026-11-04T14:45:00-07:00"]
    assert r["from_record"] and "phone number" in r["ask"]
    assert _sched_description("confirmation", "Re: Intro") == "Time accepted: Re: Intro"
    assert _sched_description("proposes-times", "Re: Intro") == "Times proposed: Re: Intro"
    assert _sched_description("new", "Coffee") == "Scheduling request: Coffee"

    class Off:
        on = False
    assert detect_scheduling_requests(inbox, sent, gate=Off()) == []


def test_a_closed_threads_row_reopens_on_a_newer_message():
    """A needs-response row is keyed by the conversation; a second ask in a
    thread whose row was closed found the id taken and was dropped. Now a
    message newer than the close reopens the row on the new message, with
    closure's reopen record; an older one leaves it closed."""
    import sqlite3
    from preprocessor import ledger as L
    from preprocessor.preprocess import update_ledger_from_analysis
    db = L._get_db()
    db.execute("DELETE FROM ledger WHERE id = 'response-convreopen12'")
    db.execute("""INSERT INTO ledger (id, type, status, description, from_email, source_message_id,
                  conversation_id, date_identified, last_action, notes, metadata, created_at, updated_at, closed_at)
                  VALUES ('response-convreopen12', 'response_needed', 'completed', 'Needs response: Re: Intro',
                  'morgan.vasquez41@example.edu', 'msg-old', 'abcdconvreopen12', '2026-10-20T21:10:16Z',
                  'Sent from Outlook', 'old ask', '{}', '2026-10-20T21:10:16Z', '2026-10-21T22:33:32Z', '2026-10-21T22:33:32Z')""")
    db.commit(); db.close()
    item = {"message_id": "msg-newer", "conversation_id": "abcdconvreopen12", "subject": "Re: Intro",
            "from": "morgan.vasquez41@example.edu", "received": "2026-10-22T22:53:00Z",
            "body_preview": "Wed Nov 4 is best."}
    update_ledger_from_analysis([], [], [], needs_response=[item], scheduling_requests=None)
    row = L.get_entry("response-convreopen12")
    assert row["status"] == "open" and row["source_message_id"] == "msg-newer" and not row.get("closed_at")
    assert "reopened" in (row["last_action"] or "")
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else json.loads(row.get("metadata") or "{}")
    assert row.get("reopened_at") or meta.get("reopened_at"), "closure's reopen record is on the row"
    # an older message than the close changes nothing
    db = L._get_db()
    db.execute("UPDATE ledger SET status = 'completed', closed_at = '2026-10-23T00:00:00Z', source_message_id = 'msg-newer' WHERE id = 'response-convreopen12'")
    db.commit(); db.close()
    update_ledger_from_analysis([], [], [], needs_response=[dict(item, message_id="msg-stale", received="2026-10-22T23:00:00Z")])
    assert L.get_entry("response-convreopen12")["status"] == "completed"


if __name__ == "__main__":
    main()
