"""sched_backtest: each scheduling record is judged by what the ledger, the
proactive seen file and the cards say, the Sent join marks what the user
answered by hand, and the summary counts per kind."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import sched_backtest as sb  # noqa: E402


def _rec(mid, conv, received, kind, status="ok", **res):
    return {"id": mid, "conversation_id": conv, "received": received, "status": status,
            "subject": f"Re: {mid}", "from_domain": "example.edu",
            "result": {"scheduling": kind, "offered_times": [], "ask": "", **res}}


def test_records_are_judged_against_rows_threads_cards_and_the_sent_join():
    records = [
        _rec("m-accept", "c1", "2026-10-22T22:53:00Z", "confirmation", offered_times=["2026-11-04T14:45:00-07:00"]),
        _rec("m-req", "c2", "2026-10-21T10:00:00Z", "request"),
        _rec("m-resched", "c3", "2026-10-20T10:00:00Z", "reschedule"),
        _rec("m-old", "c4", "2026-09-01T10:00:00Z", "request"),          # before the window
        _rec("m-none", "c5", "2026-10-21T10:00:00Z", "none"),            # not scheduling
        _rec("m-bad", "c6", "2026-10-21T10:00:00Z", "request", status="error"),
        _rec("m-dup", "c2", "2026-10-21T10:00:30Z", "request"),         # the same message, recorded twice
        dict(_rec("m-cal", "c7", "2026-10-21T11:00:00Z", "confirmation"), subject="Accepted: Lab meeting"),
    ]
    seen = {"c3": {"thread": "stream:5:topic:move", "messages": ["m-resched"]}}
    verdicts = sb.judge(records, rows_by_msg={"m-req": ["response-c2"]}, sched_convs={"c3"}, seen=seen,
                        card_threads={"stream:5:topic:move"},
                        sent_times={"c1": "2026-10-23T01:30:00Z", "c2": "2026-10-20T09:00:00Z"},
                        since="2026-09-23T00:00:00Z")
    by = {v["id"]: v for v in verdicts}
    assert set(by) == {"m-accept", "m-req", "m-resched"}
    a = by["m-accept"]
    assert not a["row"] and not a["thread"] and not a["card"] and not a["reached"]
    assert a["answered"] is True, "the user wrote after it, by hand"
    assert by["m-req"]["row"] and by["m-req"]["reached"] and by["m-req"]["answered"] is False
    r = by["m-resched"]
    assert r["row"] and r["thread"] and r["card"] and r["reached"]
    s = sb.summarize(verdicts)
    assert s["total"] == 3 and s["unreached"] == 1 and s["misses"][0]["id"] == "m-accept"
    assert s["calendar_responses"] == 1, "Outlook's Accepted: reply is set aside, not a confirmation"
    assert s["by_kind"]["confirmation"] == {"records": 1, "row": 0, "thread": 0, "card": 0, "answered": 1,
                                             "unreached": 1, "unreached_and_unanswered": 0}
    text = sb.render(s, 30, True)
    assert "confirmation" in text and "unreached in all: 1 of 3" in text


def test_without_the_sent_join_answered_is_unknown():
    records = [_rec("m1", "c1", "2026-10-22T22:53:00Z", "proposes-times")]
    v = sb.judge(records, rows_by_msg={}, sched_convs=set(), seen={}, card_threads=set(),
                 sent_times=None, since="2026-10-01T00:00:00Z")[0]
    assert v["answered"] is None
    assert sb.summarize([v])["by_kind"]["proposes-times"]["answered"] == 0


def test_the_replay_counts_what_code_would_stage_for_each_accepted_time():
    """sched_acceptance.from_thread over each conversation: an offer and a
    reply that takes one of its times stages; a reply from someone the
    offer did not go to goes to a thread with its reason; a thread with no
    acceptance, or one before the window, counts nothing. The log's counts
    sit beside it."""
    me, them, other = "kit.calloway@example.edu", "morgan.vasquez41@example.org", "rowan.pike58@example.org"
    offer = ("Hi Morgan, any of these for a 30-minute Zoom? Tue Nov 3, 2026, 3:30-4:00 PM MT (5:30-6:00 PM ET) "
             "or Wed Nov 4, 2026, 3:15-3:45 PM MT (5:15-5:45 PM ET). Kit")

    def thread(reply_from, reply_at, reply_text):
        return [{"from": {"address": me}, "sent": "2026-11-02T19:14:28Z", "subject": "Re: Plans", "to": [them],
                 "text": offer},
                {"from": {"address": reply_from}, "sent": reply_at, "subject": "Re: Plans", "to": [me],
                 "text": reply_text}]
    threads = {
        "c-yes": thread(them, "2026-11-02T21:00:24Z", "Tue Nov 3, 2026, 3:30-4:00 PM MT (5:30-6:00 PM ET)\nSee you then."),
        "c-other": thread(other, "2026-11-02T21:30:00Z", "Wed Nov 4, 2026, 3:15-3:45 PM MT works for me."),
        "c-none": thread(them, "2026-11-02T22:00:00Z", "Let me check and get back to you."),
        "c-old": thread(them, "2026-10-01T10:00:00Z", "Tue Nov 3, 2026, 3:30-4:00 PM MT works."),
    }
    ctx = {"addrs": {me}, "first": "Kit", "tz": "America/Denver", "default_minutes": 30}
    r = sb.replay_acceptances(threads, ctx, since="2026-10-25T00:00:00Z")
    assert (r["accepted"], r["would_stage"], r["handoff"]) == (2, 1, 1), r
    assert r["reasons"] == {"They were not among the people the offer went to.": 1}
    staged = next(i for i in r["items"] if i["staged"])
    assert staged["conversation_id"] == "c-yes" and staged["when"] == "Tue Nov 3, 3:30-4:00 PM MT"
    logged = sb.logged_acceptances([{"ts": "2026-11-02T21:10:00+00:00", "status": "staged"},
                                    {"ts": "2026-11-02T21:40:00+00:00", "status": "handoff"},
                                    {"ts": "2026-10-01T00:00:00+00:00", "status": "staged"}],
                                   "2026-10-25T00:00:00Z")
    assert logged == {"staged": 1, "handoff": 1}
    text = sb.render_replay(r, logged)
    assert text.startswith("accepted times replayed: 2; code would stage an invite for 1, hand 1 to a thread")
    assert "staged by code in the window (the log): 1, handed to a thread: 1" in text
