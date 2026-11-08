"""sched_acceptance: the invite for an accepted time, built and staged by
code with no model turn.

The miss it answers: the user's offer wrote each slot on two clocks (the
user's, then the other side's in parentheses), the parser read the second
clock as another slot on the user's clock, and a reply that quoted the
slot it took (both clocks) named two "offered" times, so it was filed as
times proposed, not as an acceptance. The reply was also kept mail, which
the gate left out of the scheduling lane entirely. A second shape: the
thread's needs-response row already held the reply, so the scheduling
register skipped it.

Covered here: the regression thread, the staged event's shape, the
length and zone rules, the attendee set, the cases that go to a thread
(and say why), stage() with its calendar check, ledger note and log, the
gate's acceptances, and the register's pass for a row that holds the
reply. Every address and text is made up."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-acceptance-"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocessor"))
import sched_acceptance as sa  # noqa: E402
import sched_thread_state as st  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("America/Denver")
TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
ME = "kit.calloway@example.edu"
ALIAS = "kit.c@example.com"
THEM = "morgan.vasquez41@example.org"
CC1 = "sam.okafor12@example.org"
CTX = {"addrs": {ME, ALIAS}, "first": "Kit", "tz": "America/Denver", "default_minutes": 30}

# The miss's shape: three windows, each on the user's clock and then the
# other side's; the reply quotes the one it takes, both clocks, and its
# preview runs into the attribution line of the quoted offer.
OFFER = ("Hi Morgan,\nThanks for sending the materials. I have three windows open for a 30-minute conversation:\n"
         "Monday, November 30, 2026, 9:00 to 10:00 am MT (11:00 am to noon ET)\n"
         "Monday, November 30, 2026, 2:15 to 2:45 pm MT (4:15 to 4:45 pm ET)\n"
         "Tuesday, December 1, 2026, 10:15 to 11:15 am MT (12:15 to 1:15 pm ET)\n"
         "Which time works for you?\nKit")
REPLY = ("[External email - use caution]\r\n\r\nHi Kit,\r\n\r\n"
         "Monday, November 30, 2026, 2:15 to 2:45 pm MT (4:15 to 4:45 pm ET) works for me.\r\n\r\n"
         "Hope to see you soon.\r\n\r\nMorgan\r\n\r\n\r\n"
         "On Mon, Nov 2, 2026 at 20:23 Kit Calloway <kit.callo")


def _sent(text=OFFER, to=(THEM,), cc=(), conv="conv-1", at="2026-11-03T03:23:48Z", subject="Re: [External] Lab visit"):
    return {"id": "m-offer", "conversationId": conv, "sentDateTime": at, "subject": subject,
            "from": {"emailAddress": {"address": ME}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
            "body": {"contentType": "text", "content": text}}


def _reply(text=REPLY, frm=THEM, to=(ME,), cc=(), subject="Re: [External] Lab visit"):
    return {"from": frm, "from_name": "Morgan Vasquez", "to": list(to), "cc": list(cc), "subject": subject,
            "text": st.new_text(text)}


def _offer(**kw):
    return st.offers_from_sent([_sent(**kw)], today=TODAY, tz=TZ)["conv-1"]


def _plan(reply=None, offer=None, ctx=CTX, now=NOW, slot=None):
    offer = offer or _offer()
    if slot is None:
        kind, picks, _ = st.classify_reply((reply or _reply())["text"], offer["times"], today=TODAY, tz=TZ)
        assert kind == "accepted", kind
        slot = picks[0]
    return sa.build_event(slot, offer=offer, reply=reply or _reply(), ctx=ctx, conversation_id="conv-1", now=now)


# ── the regression ────────────────────────────────────────────────────

def test_an_offer_on_two_clocks_names_each_slot_once_and_the_quoting_reply_accepts():
    offer = _offer()
    assert [t["start"] for t in offer["times"]] == [
        "2026-11-30T09:00:00-07:00", "2026-11-30T14:15:00-07:00", "2026-12-01T10:15:00-07:00"], \
        "the other side's clock in parentheses is the same slot, not another"
    kind, picks, others = st.classify_reply(REPLY, offer["times"], today=TODAY, tz=TZ)
    assert kind == "accepted" and others == [], (kind, picks, others)
    assert picks[0]["start"] == "2026-11-30T14:15:00-07:00" and picks[0]["end"] == "2026-11-30T14:45:00-07:00"
    assert offer["to"] == [THEM] and offer["from"] == ME and "30-minute" in offer["text"]


def test_the_whole_thread_reads_accepted_and_code_would_stage_it():
    msgs = [{"from": {"address": THEM}, "sent": "2026-10-30T06:00:48Z", "subject": "Lab visit", "to": [ME],
             "text": "Attached are the materials you asked for."},
            {"from": {"address": ME}, "sent": "2026-11-03T03:23:48Z", "subject": "Re: Lab visit", "to": [THEM],
             "text": OFFER},
            {"from": {"address": THEM}, "sent": "2026-11-03T17:00:24Z", "subject": "Re: Lab visit", "to": [ME],
             "text": REPLY + "way@example.edu> wrote:\nMonday, November 30, 2026, 9:00 to 10:00 am MT"}]
    r = sa.from_thread(msgs, CTX, conversation_id="conv-1")
    assert r["state"] == "accepted" and r["why"] == "", r
    assert r["event"]["start"] == "2026-11-30T14:15:00-07:00" and r["event"]["attendees"] == THEM
    assert r["when"] == "Mon Nov 30, 2:15-2:45 PM MT"


# ── the staged event's shape ──────────────────────────────────────────

def test_the_event_is_the_pending_event_shape():
    plan = _plan(_reply(cc=(CC1,)))
    assert plan["why"] == "" and plan["notes"] == [] and plan["minutes"] == 30
    assert plan["event"] == {
        "subject": "Lab visit", "start": "2026-11-30T14:15:00-07:00", "end": "2026-11-30T14:45:00-07:00",
        "attendees": THEM, "optional_attendees": CC1, "online": True, "timezone": "America/Denver",
        "conversation_id": "conv-1", "source": "sched-acceptance"}
    no_subject = _plan(_reply(subject="Re: "), offer=_offer(subject=""))
    assert no_subject["event"]["subject"] == "Kit / Morgan"


# ── length and zone ───────────────────────────────────────────────────

def test_the_length_comes_from_the_reply_then_the_offer_then_the_default():
    # the reply names its end: that end
    assert _plan()["event"]["end"] == "2026-11-30T14:45:00-07:00"
    # the reply names a start only: the offered window, cut to the stated length
    start_only = _plan(_reply(text="Hi Kit, Monday, November 30, 2026 at 9:00 am works for me."))
    assert (start_only["event"]["start"], start_only["event"]["end"]) == (
        "2026-11-30T09:00:00-07:00", "2026-11-30T09:30:00-07:00")
    # a time inside a window takes it, with the stated length
    inside = _plan(_reply(text="Monday, November 30, 2026 at 9:15 am MT is good for me."))
    assert (inside["event"]["start"], inside["event"]["end"]) == ("2026-11-30T09:15:00-07:00", "2026-11-30T09:45:00-07:00")
    # an offer of bare times states the length, or the default holds
    bare = _offer(text="Hi Morgan, would Tue Dec 1, 2026 at 3 pm MT work for an hour on Zoom? Kit")
    assert len(bare["times"]) == 1 and bare["times"][0]["end"] is None
    hour = _plan(_reply(text="Tue Dec 1, 2026 at 3 pm works."), offer=bare)
    assert hour["event"]["end"] == "2026-12-01T16:00:00-07:00" and hour["minutes"] == 60
    plain = _offer(text="Hi Morgan, would Tue Dec 1, 2026 at 3 pm MT work? Kit")
    assert _plan(_reply(text="Tue Dec 1, 2026 at 3 pm works."), offer=plain)["minutes"] == 30, "the user's default"
    unknown = _plan(_reply(text="Tue Dec 1, 2026 at 3 pm works."), offer=plain, ctx=dict(CTX, default_minutes=None))
    assert unknown["event"] is None and "no length" in unknown["why"]


def test_a_reply_that_takes_a_whole_window_longer_than_the_meeting_goes_to_a_thread():
    plan = _plan(_reply(text="Monday, November 30, 2026, 9:00 to 10:00 am MT works for me."))
    assert plan["event"] is None
    assert plan["why"] == ("They took the Mon Nov 30, 9:00-10:00 AM MT window, and the meeting is 30 minutes."
                           " The start is open.")


def test_the_event_is_on_the_users_clock_across_the_daylight_change():
    offer = _offer(text="Would Fri Oct 30, 2026, 3:00-3:30 PM MT or Mon Nov 2, 2026, 3:00-3:30 PM MT work? Kit")
    early = sa.build_event({"start": "2026-10-30T21:00:00+00:00", "end": None, "named_end": False,
                            "window": offer["times"][0]}, offer=offer, reply=_reply(text="Friday works."),
                           ctx=CTX, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    late = sa.build_event({"start": "2026-11-02T22:00:00+00:00", "end": None, "named_end": False,
                           "window": offer["times"][1]}, offer=offer, reply=_reply(text="Monday works."),
                          ctx=CTX, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert early["event"]["start"] == "2026-10-30T15:00:00-06:00", "daylight time before Nov 1"
    assert late["event"]["start"] == "2026-11-02T15:00:00-07:00" and late["event"]["end"] == "2026-11-02T15:30:00-07:00"


# ── who is invited, and when a thread decides ─────────────────────────

def test_the_attendee_set():
    required, optional = sa.attendee_sets(
        sender=THEM, offer_to=[THEM], offer_cc=[CC1, ALIAS, "no-reply@example.org"],
        reply_to=[ME, "rowan.pike58@example.org"], reply_cc=[CC1], user={ME, ALIAS})
    assert required == [THEM]
    assert optional == [CC1, "rowan.pike58@example.org"], "the user, an alias and a no-reply address are never on it"
    plan = _plan(offer=_offer(cc=(CC1, ALIAS)))
    assert plan["event"]["optional_attendees"] == CC1


@pytest.mark.parametrize("offer_kw,reply_kw,why", [
    ({"to": (THEM, CC1)}, {}, "The offer went to 2 people, and one has answered."),
    ({"to": (CC1,), "cc": (THEM,)}, {}, "They were not among the people the offer went to."),
    ({}, {"text": "Monday, November 30, 2026, 2:15 to 2:45 pm MT probably works best."},
     'Their reply is not a plain yes ("probably").'),
])
def test_what_is_not_one_plain_yes_from_the_one_asked_goes_to_a_thread(offer_kw, reply_kw, why):
    plan = _plan(_reply(**reply_kw), offer=_offer(**offer_kw))
    assert plan["event"] is None and plan["why"] == why


def test_a_time_that_has_passed_goes_to_a_thread():
    plan = _plan(now=datetime(2026, 11, 30, 21, 10, tzinfo=timezone.utc))
    assert plan["event"] is None and plan["why"] == "The accepted time, Mon Nov 30, 2:15-2:45 PM MT, has passed."


def test_phone_or_in_person_means_no_video_link():
    phone = _plan(_reply(text="Monday, November 30, 2026, 2:15 to 2:45 pm MT works. Can I call you by phone?"))
    assert phone["event"]["online"] is False and "phone call" in phone["notes"][0]
    office = _plan(offer=_offer(text=OFFER.replace("conversation:", "conversation in my office:")))
    assert office["event"]["online"] is False and "in person" in office["notes"][0]
    zoom = _plan(offer=_offer(text=OFFER.replace("conversation:", "Zoom conversation, or in person:")))
    assert zoom["event"]["online"] is True and zoom["notes"] == []
    assert sa.stated_minutes("a 45 min chat") == 45 and sa.stated_minutes("half an hour") == 30
    assert sa.stated_minutes("Room 101 at 3") is None


# ── stage(): the calendar check, the ledger note, the log ─────────────

def _engine(conflicts=(), fetch_errors=False, fail=False):
    calls = []

    def runner(argv):
        calls.append(argv)
        if fail:
            return 1, "", "boom"
        issues = [{"type": "interpreted_as"}] + [{"type": "conflict", "conflict_with": c} for c in conflicts]
        return 0, json.dumps({"valid": not conflicts, "issues": issues,
                              "calendar_fetch_errors": ["google"] if fetch_errors else []}), ""
    runner.calls = calls
    return runner


def _item(plan, **kw):
    return {"message_id": "m-accept", "conversation_id": "conv-1", "received": "2026-11-03T17:00:24Z",
            "from": THEM, "from_name": "Morgan Vasquez", "subject": "Re: Lab visit", "kind": "confirmation",
            "row_id": "sched-m-accept", "acceptance": plan, **kw}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    return tmp_path


def test_stage_checks_the_calendar_notes_the_row_and_logs(store):
    notes = []
    runner = _engine(conflicts=("Office hours",))
    out = sa.stage([_item(_plan())], runner=runner, set_last_action=lambda rid, t: notes.append((rid, t)), now=NOW)
    assert len(out) == 1 and "acceptance" not in out[0]
    it = out[0]
    assert it["staged_event"]["ledger_id"] == "sched-m-accept" and it["staged_event"]["source"] == "sched-acceptance"
    assert it["stage_when"] == "Mon Nov 30, 2:15-2:45 PM MT"
    assert it["stage_notes"] == ["That time now overlaps Office hours on your calendar."], "a conflict is said, not fatal"
    assert runner.calls == [["validate-draft", "--times", '["2026-11-30T14:15:00-07:00"]', "--duration", "30"]]
    assert notes == [("sched-m-accept", "2026-11-03: the invite for Mon Nov 30, 2:15-2:45 PM MT is staged from "
                                        "their acceptance; the Create button is in the DM.")]
    log = sa._store_read()
    assert [r["status"] for r in log] == ["staged"] and log[0]["minutes"] == 30 and log[0]["sender"] == THEM
    assert sa.seen(_item(None)) and not sa.seen(_item(None, message_id="m-other", received="2026-11-04T10:00:00Z"))


def test_stage_hands_off_the_meeting_already_on_the_calendar_and_drops_kept_mail(store):
    out = sa.stage([_item(_plan(), message_id="m-1"), _item(_plan(), message_id="m-2", kept=True)],
                   runner=_engine(conflicts=("Kit / Morgan",)), set_last_action=lambda *a: None, now=NOW)
    assert len(out) == 1, "kept mail with no event never reaches a model turn"
    assert out[0]["handoff"] == "A meeting at that time is already on your calendar: Kit / Morgan."
    assert "staged_event" not in out[0]
    assert [r["status"] for r in sa._store_read()] == ["handoff", "handoff"]


def test_stage_without_a_calendar_answer_still_stages_and_says_so(store):
    out = sa.stage([_item(_plan())], runner=_engine(fail=True), set_last_action=lambda *a: None, now=NOW)
    assert out[0]["staged_event"] and out[0]["stage_notes"] == ["The calendar check did not run."]
    out = sa.stage([_item(_plan(), message_id="m-3")], runner=_engine(fetch_errors=True),
                   set_last_action=lambda *a: None, now=NOW)
    assert out[0]["stage_notes"] == ["A calendar could not be read, so conflicts are not fully checked."]
    plain = {"message_id": "m-9", "kind": "new"}
    assert sa.stage([plain], runner=_engine(), now=NOW) == [plain], "an item with no acceptance passes as it is"


def test_the_ledger_row_records_the_staged_invite(tmp_path):
    import sqlite3
    import ledger
    db = tmp_path / "l.db"
    conn = sqlite3.connect(db)
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    assert ledger.add_entry("response-conv1", "response_needed", "Needs response: Lab visit",
                            source_message_id="m-accept", conversation_id="conv-1", db_path=db)
    assert ledger.entry_ids_for_message("m-accept", db_path=db) == ["response-conv1"]
    assert ledger.entry_ids_for_message("m-none", db_path=db) == []
    assert ledger.set_last_action("response-conv1", "2026-11-03: invite staged", db_path=db)
    assert ledger.get_entry("response-conv1", db_path=db)["last_action"] == "2026-11-03: invite staged"
    assert not ledger.set_last_action("missing", "x", db_path=db)


# ── the lane: the gate's acceptances and the register ─────────────────

def test_the_gate_passes_an_fyi_or_kept_acceptance_and_strips_kept_text():
    import mail_gate
    g = mail_gate.Gate.__new__(mail_gate.Gate)
    g.on = True
    classes = {"m-fyi": ("fyi", {"result": {"summary": "Morgan confirmed the time.", "state": "fyi"}}),
               "m-kept": ("kept", {"keep_from_anthropic": "admissions"}),
               "m-kept-noevent": ("kept", {}), "m-action": ("action", {})}
    g.classify = lambda it: classes[it["message_id"]]
    found = [{"message_id": "m-fyi", "body_preview": "raw text", "acceptance": {"event": {"start": "x"}}},
             {"message_id": "m-kept", "body_preview": "raw text", "ask": "record ask", "acceptance": {"event": {"start": "x"}}},
             {"message_id": "m-kept-noevent", "body_preview": "raw", "acceptance": {"event": None, "why": "w"}},
             {"message_id": "m-action", "body_preview": "raw", "acceptance": {"event": {"start": "x"}}}]
    passed = [dict(found[3])]
    extra = {o["message_id"]: o for o in g.acceptances(found, passed)}
    assert set(extra) == {"m-fyi", "m-kept"}
    assert extra["m-fyi"]["body_preview"] == "Morgan confirmed the time."
    assert extra["m-kept"]["body_preview"] == "" and extra["m-kept"]["ask"] == "" and extra["m-kept"]["kept"] is True
    g.on = False
    assert g.acceptances(found, []) == []


def test_register_passes_an_acceptance_a_response_row_holds_once(monkeypatch, store):
    from preprocessor import preprocess as pp
    added = []
    monkeypatch.setattr(pp._ledger_mod, "get_all_source_message_ids", lambda: {"m-accept", "m-old"})
    monkeypatch.setattr(pp._ledger_mod, "entry_ids_for_message",
                        lambda mid: ["response-conv1"] if mid == "m-accept" else [])
    monkeypatch.setattr(pp._ledger_mod, "add_entry", lambda **kw: added.append(kw["entry_id"]) or True)
    acc = {"message_id": "m-accept", "conversation_id": "conv-1", "received": "2026-11-03T17:00:24Z",
           "subject": "Re: Lab visit", "kind": "confirmation", "acceptance": _plan()}
    old = {"message_id": "m-old", "conversation_id": "conv-2", "subject": "Re: Coffee", "kind": "new"}
    fresh = {"message_id": "msg-new-0123456789", "conversation_id": "conv-3", "subject": "Coffee", "kind": "new"}
    new = pp.register_new_scheduling_requests([dict(acc), old, fresh])
    assert [n["message_id"] for n in new] == ["m-accept", "msg-new-0123456789"]
    assert new[0]["row_id"] == "response-conv1", "the row that holds the reply records the invite"
    assert added == ["sched-w-0123456789"] and new[1]["row_id"] == "sched-w-0123456789"
    sa.stage([new[0]], runner=_engine(), set_last_action=lambda *a: None, now=NOW)
    assert [n["message_id"] for n in pp.register_new_scheduling_requests([dict(acc)])] == [], "once"


def test_queue_scheduling_stages_kept_mail_with_no_text_and_only_with_a_channel(monkeypatch, store):
    """The miss's reply was kept mail: the gate leaves it out of every
    lane. Its invite still stages, with no text in the queue; with no
    scheduling channel nothing stages and the kept reply stays out."""
    import mail_gate
    from preprocessor import preprocess as pp
    monkeypatch.setattr(pp._ledger_mod, "get_all_source_message_ids", lambda: set())
    monkeypatch.setattr(pp._ledger_mod, "add_entry", lambda **kw: True)
    monkeypatch.setattr(sa, "run_engine", _engine())
    monkeypatch.setattr(sa, "_now", lambda: NOW)
    monkeypatch.setattr("ledger.set_last_action", lambda *a, **k: True)
    queued: list = []
    monkeypatch.setattr(pp, "write_proactive_queue", lambda reqs: queued.extend(reqs) or len(reqs))
    gate = mail_gate.Gate.__new__(mail_gate.Gate)
    gate.on, gate.counts, gate.held = True, Counter(), set()
    gate.classify = lambda it: ("kept", {"keep_from_anthropic": "admissions"})
    item = {"message_id": "m-accept", "conversation_id": "conv-1", "received": "2026-11-03T17:00:24Z",
            "subject": "Re: Lab visit", "from": THEM, "from_name": "Morgan Vasquez", "kind": "confirmation",
            "body_preview": REPLY, "ask": "Confirm the time.", "acceptance": _plan()}
    monkeypatch.setattr(pp, "sched_channel_configured", lambda *a: True)
    new, n = pp.queue_scheduling([dict(item)], gate)
    assert n == 1 and len(queued) == 1
    q = queued[0]
    assert q["kept"] is True and q["body_preview"] == "" and q["ask"] == "" and "acceptance" not in q
    assert q["staged_event"]["start"] == "2026-11-30T14:15:00-07:00" and q["row_id"] == "sched-m-accept"
    queued.clear()
    monkeypatch.setattr(pp, "sched_channel_configured", lambda *a: False)
    new, n = pp.queue_scheduling([dict(item, message_id="m-other", received="2026-11-04T10:00:00Z")], gate)
    assert new == [] and queued == [], "no channel: the kept reply stays out and nothing stages"


def test_detect_plans_the_regression_thread_end_to_end(monkeypatch):
    from preprocessor import preprocess as pp
    monkeypatch.setattr(sa, "context", lambda *a, **k: CTX)
    monkeypatch.setattr(sa, "_now", lambda: NOW)
    inbox = [{"id": "m-accept", "conversationId": "conv-1", "subject": "Re: [External] Lab visit",
              "receivedDateTime": "2026-11-03T17:00:24Z", "bodyPreview": REPLY,
              "from": {"emailAddress": {"address": THEM, "name": "Morgan Vasquez"}},
              "toRecipients": [{"emailAddress": {"address": ME}}]}]
    found = pp.detect_scheduling_requests(inbox, [], gate=None, sent_full=[_sent()])
    assert len(found) == 1 and found[0]["kind"] == "confirmation" and found[0]["offered_match"]
    plan = found[0]["acceptance"]
    assert plan["why"] == "" and plan["event"]["start"] == "2026-11-30T14:15:00-07:00"
    assert plan["event"]["attendees"] == THEM and plan["event"]["subject"] == "Lab visit"
    # a reply naming two of the offered slots is times proposed, with its reason
    two = dict(inbox[0], id="m-two", bodyPreview="Monday, November 30, 2026 at 9:00 am or 2:15 pm MT both work.")
    found = pp.detect_scheduling_requests([two], [], gate=None, sent_full=[_sent()])
    assert found[0]["kind"] == "proposes-times" and found[0]["handoff"] == "They named 2 of the times you offered."
    assert "acceptance" not in found[0]
