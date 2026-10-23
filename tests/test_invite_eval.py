"""invite_eval: an invite is scored against the thread's accepted time, the
intent's duration, the sender, the phone-or-video ask, the user's zone and
the calendar; the summary counts each check and names the failures."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import invite_eval as ie  # noqa: E402

TZ = ZoneInfo("America/Denver")
REC = {"run_id": "r1", "ts": "2026-10-22T20:00:00-06:00", "thread_ts": "stream:5:topic:x", "live": True, "kind": "invite",
       "event": {"subject": "Interview: Kit / Morgan", "start": "2026-11-04T14:45:00-07:00", "end": "2026-11-04T15:15:00-07:00",
                 "attendees": "morgan.vasquez41@example.org", "online": False},
       "intent": {"sender_email": "morgan.vasquez41@example.org", "duration_minutes": 30}}
STATE = {"state": "accepted", "accepted": {"start": "2026-11-04T14:45:00-07:00", "end": "2026-11-04T15:15:00-07:00"}}
CAL = [{"subject": "Interview: Kit / Morgan", "start": {"dateTime": "2026-11-04T21:45:00.0000000"},
        "end": {"dateTime": "2026-11-04T22:15:00.0000000"}}]


def test_a_right_invite_passes_every_check():
    s = ie.score_invite(REC, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)
    assert s["ok"] and s["checks"] == {"time": True, "duration": True, "attendee": True, "mode": True,
                                        "zone": True, "created": True, "calendar": True}, s


def test_each_wrong_thing_fails_its_check_and_is_named():
    wrong_time = dict(REC, event=dict(REC["event"], start="2026-11-04T15:45:00-07:00", end="2026-11-04T16:15:00-07:00"))
    s = ie.score_invite(wrong_time, STATE, wants_phone=None, calendar=[], user_tz=TZ)
    assert s["checks"]["time"] is False and "thread accepted" in s["notes"][0] and not s["ok"]
    assert s["checks"]["mode"] is None and s["checks"]["created"] is False and s["checks"]["calendar"] is None
    long = dict(REC, event=dict(REC["event"], end="2026-11-04T15:45:00-07:00"))
    assert ie.score_invite(long, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)["checks"]["duration"] is False
    no_sender = dict(REC, event=dict(REC["event"], attendees="someone.else@example.org"))
    assert ie.score_invite(no_sender, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)["checks"]["attendee"] is False
    online = dict(REC, event=dict(REC["event"], online=True))
    assert ie.score_invite(online, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)["checks"]["mode"] is False
    assert ie.score_invite(online, STATE, wants_phone=False, calendar=CAL, user_tz=TZ)["checks"]["mode"] is True
    utc = dict(REC, event=dict(REC["event"], start="2026-11-04T21:45:00+00:00", end="2026-11-04T22:15:00+00:00"))
    assert ie.score_invite(utc, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)["checks"]["zone"] is False
    moved = [{"subject": "Interview: Kit / Morgan", "start": {"dateTime": "2026-11-04T22:00:00.0000000"},
              "end": {"dateTime": "2026-11-04T22:30:00.0000000"}}]
    s = ie.score_invite(REC, STATE, wants_phone=True, calendar=moved, user_tz=TZ)
    assert s["checks"]["created"] is True and s["checks"]["calendar"] is False
    assert ie.score_invite(REC, {"state": "awaiting-pick"}, wants_phone=True, calendar=CAL, user_tz=TZ)["checks"]["time"] is None


def test_the_thread_says_phone_or_video():
    me = {"kit.calloway@example.edu"}
    msgs = [{"from": {"address": "kit.calloway@example.edu"}, "sent": "1", "text": "By phone or Zoom."},
            {"from": {"address": "morgan.vasquez41@example.org"}, "sent": "2", "text": "Phone is easier; can I call you directly?"}]
    assert ie.thread_wants_phone(msgs, me) is True
    msgs[1]["text"] = "Zoom works, send the link."
    assert ie.thread_wants_phone(msgs, me) is False
    msgs[1]["text"] = "Sounds good."
    assert ie.thread_wants_phone(msgs, me) is None


def test_the_summary_counts_and_the_line_names_the_failures():
    good = ie.score_invite(REC, STATE, wants_phone=True, calendar=CAL, user_tz=TZ)
    bad = ie.score_invite(dict(REC, event=dict(REC["event"], online=True)), STATE, wants_phone=True, calendar=CAL, user_tz=TZ)
    s = ie.summarize([good, bad])
    assert s["invites"] == 2 and len(s["failing"]) == 1 and s["counts"]["mode"] == {"true": 1, "false": 1, "none": 0}
    line = ie.render(s, "2026-10-22")
    assert line.startswith(":calendar: invite eval 2026-10-22: 2 invites, 1 with a failed check")
    assert "phone call" in line and "Interview: Kit / Morgan" in line
    assert ie.render(ie.summarize([]), "2026-10-22").endswith("no invites")
