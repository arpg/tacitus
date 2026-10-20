#!/usr/bin/env python3
"""Triage-detector filters that keep the "get done today" queue to work
that is actually the user's.

  1. LIST MAIL IS NOT AN OBLIGATION. A digest or newsletter otherwise
     mints a `response-` ledger row and can carry the needs-response
     delta. Mail from a list named in list-digests.json is skipped by
     every inbox detector; the list-digest agent owns it.

  2. ONE DEADLINE PER THREAD, AND NOT THE USER'S OWN ASK. A deadline the
     user set for other people, repeated across every reply in the
     thread, otherwise fills every today_top slot as OVERDUE.
     detect_deadlines keeps the newest message per conversation and
     marks such a date `owed_to_user`; build_today_top keeps those out
     of the queue.

Usage:
    python3 tests/test_triage_filters.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sandbox BEFORE the import: preprocess.py touches EA_DIR at import time
# and list_digest.py derives its config path from it.
EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-triage-"))
os.environ["EA_DIR"] = str(EA_DIR)
(EA_DIR / "config").mkdir(parents=True, exist_ok=True)
(EA_DIR / "state").mkdir(parents=True, exist_ok=True)
(EA_DIR / "config" / "list-digests.json").write_text(json.dumps({
    "lists": [
        {"name": "robotics-worldwide",
         "senders": ["robotics-worldwide-request@lists.example.edu"],
         "list_address": "robotics-worldwide@lists.example.edu",
         "format": "digest", "mode": "filter"},
        {"name": "ceas-rso", "senders": ["rso@com.example.edu"],
         "format": "newsletter", "mode": "prioritize"},
    ]
}))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))

from preprocessor import preprocess as pp  # noqa: E402

NOW = datetime.now(timezone.utc)

try:
    import pytest
except ImportError:
    pytest = None
if pytest is not None:
    @pytest.fixture(autouse=True, scope="module")
    def _this_files_lists():
        """Under pytest another file may import list_digest first, and its
        config path is fixed then; read the lists this file wrote."""
        import list_digest
        orig = list_digest.configured_lists
        list_digest.configured_lists = (
            lambda path=EA_DIR / "config" / "list-digests.json": orig(path))
        yield
        list_digest.configured_lists = orig


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(mid, subject, *, frm="sol.dunmore11@example.edu", name="A Colleague",
         to=("wren.fairbanks84@example.edu",), cc=(), conv=None,
         received=None, preview="Could you reply by Friday? Thanks.",
         importance="normal"):
    return {
        "id": mid,
        "conversationId": conv or f"conv-{mid}",
        "subject": subject,
        "bodyPreview": preview,
        "from": {"emailAddress": {"address": frm, "name": name}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "receivedDateTime": _iso(received or (NOW - timedelta(days=5))),
        "importance": importance,
        "flag": {"flagStatus": "notFlagged"},
    }


def _sent(subject, *, conv, at, frm="wren.fairbanks84@example.edu",
          to=("someone@example.edu",)):
    return {
        "id": f"sent-{conv}-{at.timestamp():.0f}",
        "conversationId": conv,
        "subject": subject,
        "sentDateTime": _iso(at),
        "receivedDateTime": _iso(at),
        "from": {"emailAddress": {"address": frm, "name": "Kit"}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }


DIGEST = _msg("d1", "robotics-worldwide Digest Tue, 01 Sep 2026 (1/1)",
              frm="robotics-worldwide-request@lists.example.edu",
              name="robotics-worldwide-request",
              to=("robotics-worldwide@lists.example.edu",),
              preview="Deadline extended: submit by 9/15. RSVP for the workshop. "
                      "Action required.")
NEWSLETTER = _msg("n1", "Research Support Office Funding Opportunities for Sept. 2, 2026",
                  frm="rso@com.example.edu", name="College Research Support Office",
                  preview="Due 10/12: Research & Innovation Seed Grant. Reminder: "
                          "Action required, RSVP by Friday.")
HUMAN = _msg("h1", "Quick question about the syllabus",
             preview="Could you confirm the due date by Friday? Deadline 9/12.")


# ── 1. list mail is not an obligation ────────────────────────────────

def test_list_mail_is_recognised_from_the_configured_lists():
    assert pp._is_list_mail(DIGEST)
    assert pp._is_list_mail(NEWSLETTER)
    assert not pp._is_list_mail(HUMAN)
    # a post sent TO the list address by a member is list mail too
    post = _msg("p1", "[robotics-worldwide] [Meetings] Workshop CFP",
                frm="someone@uni.edu", to=("robotics-worldwide@lists.example.edu",))
    assert pp._is_list_mail(post)


def test_needs_response_and_dropped_threads_skip_list_mail():
    inbox = [DIGEST, NEWSLETTER, HUMAN]
    needs = pp.detect_needs_response(inbox, [])
    assert [n["message_id"] for n in needs] == ["h1"], needs
    dropped = pp.detect_dropped_threads(inbox, [], stale_days=3)
    assert [d["message_id"] for d in dropped] == ["h1"], dropped


def test_the_own_domain_signal_is_anchored_on_the_host():
    assert pp._on_domain("bo@example.edu", "example.edu")
    assert pp._on_domain("Bo <bo@CS.Example.edu>", "example.edu")
    assert not pp._on_domain("bo@notexample.edu", "example.edu")
    assert not pp._on_domain("bo@example.edu.evil.com", "example.edu")
    assert not pp._on_domain("bo@example.edu", "")
    orig = pp._institution.own_domain
    pp._institution.own_domain = lambda: "example.edu"
    try:
        own = _msg("o1", "Syllabus", frm="sol.dunmore11@example.edu")
        lookalike = _msg("o2", "Syllabus", frm="sol.dunmore11@notexample.edu")
        score = {n["message_id"]: n["response_signals"]
                 for n in pp.detect_needs_response([own, lookalike], [])}
        assert score["o1"] == score["o2"] + 1, score
    finally:
        pp._institution.own_domain = orig


def test_deadlines_rsvps_and_priority_skip_list_mail():
    inbox = [DIGEST, NEWSLETTER, HUMAN]
    assert [d["message_id"] for d in pp.detect_deadlines(inbox)] == ["h1"]
    assert all(r["message_id"] != "d1" and r["message_id"] != "n1"
               for r in pp.detect_pending_rsvps(inbox))
    recent = [dict(m, receivedDateTime=_iso(NOW - timedelta(minutes=30)))
              for m in (DIGEST, NEWSLETTER, HUMAN)]
    vip = {"rso@com.example.edu", "robotics-worldwide-request@lists.example.edu"}
    hits = pp.detect_priority_emails(recent, vip, sent=[], hours_back=4)
    assert all(h["message_id"] == "h1" for h in hits), hits


def test_list_mail_check_survives_a_missing_config():
    """No list-digests.json means nothing is list mail, and no detector
    dies for it."""
    cfg = EA_DIR / "config" / "list-digests.json"
    saved = cfg.read_text()
    cfg.unlink()
    try:
        import list_digest
        list_digest._LISTS_CACHE = None
        assert not pp._is_list_mail(DIGEST)
        # (the detector orders by response signals, so compare as a set)
        assert {n["message_id"] for n in pp.detect_needs_response([DIGEST, HUMAN], [])} == {"d1", "h1"}
    finally:
        cfg.write_text(saved)
        list_digest._LISTS_CACHE = None
    assert pp._is_list_mail(DIGEST)


# ── 2. one deadline per thread, and not the user's own ask ───────────

CHARGE = "Curriculum working group: charge and questions, answers by Sep 2"


def _working_group_thread():
    conv = "conv-curriculum"
    opened = NOW - timedelta(days=9)
    sent = [_sent(CHARGE, conv=conv, at=opened,
                  to=("indra.galloway9@example.edu", "ash.pemberton20@example.edu"))]
    inbox = []
    for i, (who, days) in enumerate([("Harper Jamison", 7), ("Casey Whitlock", 5),
                                      ("Lior Okonkwo", 3), ("Ari Abernathy", 2),
                                      ("Ari Abernathy", 1)]):
        inbox.append(_msg(f"r{i}", "Re: " + CHARGE, frm=f"{who.split()[0].lower()}@example.edu",
                          name=who, conv=conv, received=NOW - timedelta(days=days),
                          preview="Here are my answers to the charge, due by Sep 2."))
    return inbox, sent


def test_one_deadline_item_per_conversation_keeps_the_newest():
    inbox, sent = _working_group_thread()
    items = pp.detect_deadlines(inbox, sent)
    assert len(items) == 1, [i["message_id"] for i in items]
    assert items[0]["message_id"] == "r4"          # the newest reply
    assert items[0]["conversation_id"] == "conv-curriculum"


def test_a_date_the_user_set_for_others_is_owed_to_user():
    inbox, sent = _working_group_thread()
    item = pp.detect_deadlines(inbox, sent)[0]
    assert item["owed_to_user"] is True
    # the same thread without the user's charge in Sent: nothing says
    # the user opened it, so the date stays the user's
    assert pp.detect_deadlines(inbox)[0]["owed_to_user"] is False
    assert pp.detect_deadlines(inbox, [])[0]["owed_to_user"] is False
    # a thread somebody ELSE opened, that the user merely replied to
    other = _msg("x1", "Re: Proposal budget due Sep 12", conv="conv-other",
                 received=NOW - timedelta(days=1),
                 preview="Reminder: the budget is due by Sep 12.")
    reply = _sent("Re: Proposal budget due Sep 12", conv="conv-other",
                  at=NOW - timedelta(days=2))
    assert pp.detect_deadlines([other], [reply])[0]["owed_to_user"] is False


def test_the_thread_root_is_found_by_subject_when_the_conversation_id_differs():
    """A sent root whose conversationId Outlook assigned differently
    (a message moved between mailboxes) still names the user as the
    opener through the bare subject."""
    inbox, sent = _working_group_thread()
    sent[0]["conversationId"] = "conv-outlook-renumbered"
    assert pp.detect_deadlines(inbox, sent)[0]["owed_to_user"] is True
    # but a REPLY by the user is not opening the thread
    sent[0]["subject"] = "RE: " + CHARGE
    assert pp.detect_deadlines(inbox, sent)[0]["owed_to_user"] is False


def test_a_reminder_the_user_answered_in_its_thread_is_not_overdue_work():
    """A department reminder with a passed date: the user answered in its
    thread after it arrived, so the date is history. Before the answer it
    is still the user's overdue work."""
    remind = _msg("rem1", "Final reminder: spring appointment requests due Oct 18",
                  conv="conv-appts", received=NOW - timedelta(days=4),
                  preview="Spring appointment requests are due by Oct 18.")
    answer = _sent("RE: Final reminder: spring appointment requests due Oct 18",
                   conv="conv-appts", at=NOW - timedelta(days=1))
    earlier = _sent("RE: spring appointments", conv="conv-appts", at=NOW - timedelta(days=6))
    [item] = pp.detect_deadlines([remind], [answer])
    assert item["answered_at"] and item["owed_to_user"] is False
    item["extracted_date"] = (NOW - timedelta(days=2)).strftime("%B %-d")
    top, rest = pp.build_today_top([], [], [item], [], [], [], cap=5, floor=30)
    assert top == [] and rest[0]["score"] == 5, (top, rest)
    [item] = pp.detect_deadlines([remind], [earlier])
    assert "answered_at" not in item
    item["extracted_date"] = (NOW - timedelta(days=2)).strftime("%B %-d")
    top, _ = pp.build_today_top([], [], [item], [], [], [], cap=5, floor=30)
    assert top and "OVERDUE" in top[0]["reason"]


def test_norm_subject_strips_every_reply_prefix():
    assert pp._norm_subject("Re: RE: Fwd:  Curriculum  group") == "curriculum group"
    assert pp._norm_subject("AW: Sv: hello") == "hello"
    assert pp._norm_subject("Regarding: hello") == "regarding: hello"
    assert pp._REPLY_PREFIX_RE.match("Re: x") and not pp._REPLY_PREFIX_RE.match("Rex: x")


def test_today_top_keeps_the_users_own_ask_out_of_the_queue():
    inbox, sent = _working_group_thread()
    deadlines = pp.detect_deadlines(inbox, sent)
    # force a parseable, overdue date: without the guard this scores +100
    deadlines[0]["extracted_date"] = (NOW - timedelta(days=1)).strftime("%B %-d")
    top, rest = pp.build_today_top([], [], deadlines, [], [], [], cap=5, floor=30)
    assert top == [], top
    assert rest and rest[0]["source"] == "deadline" and rest[0]["score"] == 5
    # the same item, not the user's ask, is the overdue queue entry it was
    deadlines[0]["owed_to_user"] = False
    top, _ = pp.build_today_top([], [], deadlines, [], [], [], cap=5, floor=30)
    assert len(top) == 1 and top[0]["reason"].startswith("OVERDUE since"), top


def test_row_parked_for_today_is_todays_item():
    """A ledger row parked with follow_up_after = today was parked FOR
    today: it scores like a due-today item even with no deadline, and a
    clock time in its description rides in the reason. The show tickets
    (on sale Tue 10:00am, parked until that Tuesday, deadline cleared)
    were absent from that morning's briefing."""
    today = datetime.now().astimezone().date().isoformat()
    row = {"id": "tix", "status": "open", "type": "task", "deadline": None,
           "follow_up_after": today, "priority": "medium", "days_stale": 0,
           "description": "Buy 2 tickets for the show. Tickets go on sale online Tue at 10:00am MT."}
    top, rest = pp.build_today_top([], [], [], [], [row], [], cap=5, floor=30)
    assert len(top) == 1 and top[0]["ref"] == "tix", (top, rest)
    assert "window opens today at 10:00am" in top[0]["reason"], top[0]["reason"]
    # parked until tomorrow: not raised; parked until yesterday: ordinary row
    from datetime import date as _date
    row["follow_up_after"] = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    top, rest = pp.build_today_top([], [], [], [], [row], [], cap=5, floor=30)
    assert top == [] and rest == [], (top, rest)
    row["follow_up_after"] = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    top, rest = pp.build_today_top([], [], [], [], [row], [], cap=5, floor=30)
    assert top == [] and rest and rest[0]["score"] == 5, (top, rest)


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
