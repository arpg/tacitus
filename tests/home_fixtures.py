"""Made-up days for Home (preprocessor/home_screen.py, docs/webchat.md, Home):
the sources one afternoon could give, and the payload the bot builds from
them, so the browser tests and the bot's own tests draw what the bot
sends. Every name, subject and time here is invented.

`sources(state)` gives the Sources of one state; `payload(state)` builds it.
The states: normal, model (the same day with a line the owner's model
wrote), done, red (the status light is red), overdue, travel, vacation.
The clock is today at 1:10 PM in ZONE, so the app's own times match."""
from __future__ import annotations

import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "botcore"))

import home_screen as H  # noqa: E402

ZONE_NAME = "America/Denver"
ZONE = ZoneInfo(ZONE_NAME)
STATES = ("normal", "model", "done", "red", "overdue", "travel", "vacation")


def at(hour: int, minute: int = 0, days: int = 0) -> float:
    d = datetime.now(ZONE).date() + timedelta(days=days)
    return datetime.combine(d, dtime(hour, minute), ZONE).timestamp()


def utc(ts: float) -> str:
    """Graph's event time: UTC wall clock with no offset."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def event(subject: str, start: float, end: float, show: str = "busy") -> dict:
    return {"id": f"ev-{subject[:6]}-{int(start)}", "subject": subject, "isAllDay": False, "showAs": show,
            "start": {"dateTime": utc(start), "timeZone": "UTC"}, "end": {"dateTime": utc(end), "timeZone": "UTC"}}


def all_day(subject: str, first: date, days: int, show: str = "oof") -> dict:
    return {"id": f"ev-{subject[:6]}", "subject": subject, "isAllDay": True, "showAs": show,
            "start": {"dateTime": f"{first.isoformat()}T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": f"{(first + timedelta(days=days)).isoformat()}T00:00:00.0000000", "timeZone": "UTC"}}


def request(mid: str, kind: str, action: str, what: str, who: str, *, urgency: str = "calm", why: str = "",
            when_: str = "", posted: float | None = None, due: float | None = None, project: str = "",
            dm: str = "dm") -> dict:
    """A waiting Action request as WebFrontend.requests() lists it."""
    return {"id": mid, "channel": dm, "thread_key": f"t{mid}", "kind": "card", "author": "bot",
            "waiting": True, "working": False, "action": action, "what": what, "who": who, "when": when_,
            "urgency": urgency, "why": why, "at": due or 0, "project": project,
            "posted_at": posted or at(8, 30), "updated_at": posted or at(8, 30),
            "card": {"kind": kind, "title": f"{action}?", "fields": [], "actions": [
                {"id": "send", "label": "Send", "value": '{"conversation_id": "conv-%s"}' % mid}]}}


def answered(mid: str, text: str, when_: float, dm: str = "dm") -> dict:
    return {"id": mid, "channel": dm, "thread_key": f"t{mid}", "kind": "status", "author": "bot",
            "waiting": False, "working": False, "text": text, "posted_at": when_ - 600, "updated_at": when_,
            "card": {"kind": "send", "actions": []}}


def closed(rid: str, desc: str, when_: float, signal: str, conv: str = "") -> dict:
    return {"id": rid, "description": desc, "conversation_id": conv,
            "closed_at": datetime.fromtimestamp(when_, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metadata": {"closed_by": {"signal": signal, "actor": "user" if signal.startswith("user") else "cli:intake"}}}


# The report as the store keeps it: Markdown, as a post shows it.
REPORT = (":sunny: **Morning Briefing, today**\n\n:dart: **Get done today** (in this order, fitted to your gaps)\n"
          "1. Answer Sam about the seminar date, before your 2:00.\n"
          "2. Send the workshop review, in the 3:00 to 4:30 gap.\n\n"
          ":leaves: **Flexes**\n- The slides for the committee can wait until Friday.\n\n"
          ":clipboard: 9:00 Group meeting · 2:00 Seminar\n\n"
          ":white_check_mark: **Quietly handled**: 2 rows closed, 1 draft staged.")


def sources(state: str = "normal", dm: str = "dm", main: str = "main") -> H.Sources:
    """One state's sources; `dm` and `main` are the channel ids of Action
    requests and Ask Tacitus (the stub's own, in the browser tests)."""
    today = datetime.now(ZONE).date()
    now = at(13, 10)
    top = [{"source": "obligation", "title": f"Due {(today + timedelta(days=1)).isoformat()}: Send the workshop review",
            "ref": "deadline-w1", "reason": "Due tomorrow", "score": 80},
           {"source": "obligation", "title": "Send the slides to the committee", "ref": "task-s2",
            "reason": "You said you would send them this week", "score": 40}]
    evs = [event("Group meeting", at(9), at(10)), event("Seminar", at(14), at(15)),
           event("Planning call", at(9, 30, 1), at(10, 0, 1))]
    briefing = {"generated_at": datetime.fromtimestamp(at(4, 2), timezone.utc).isoformat(), "today_top": top,
                "todays_events": evs[:2], "tomorrows_events": evs[2:],
                "onboarding_question": {"id": "urgent", "text": "What counts as urgent for you?",
                                        "why": "So Tacitus knows when to interrupt you.",
                                        "example": "A dean, a deadline this week, anything from my lab.",
                                        "remaining": 9}}
    reqs = {"waiting": [request("w41", "send", "Send email", "Re: Lab budget", "To Pat Example", urgency="soon",
                                why="due", due=at(17, 0, 1), project="Lab", dm=dm),
                        request("w42", "event", "Create meeting", "Planning call", "Attendees Jo Rivera",
                                when_="Thu 9:30 AM", posted=at(11, 5), dm=dm)],
            "done": [answered("w40", "Sent to Lee Example.", at(10, 40), dm)]}
    replies = [{"id": "response-r1", "subject": "Seminar date", "from_name": "Sam Rivera", "age_days": 2,
                "conversation_id": "conv-r1"},
               {"id": "response-r2", "subject": "Data access form", "from_name": "Alex Kim", "age_days": 4,
                "conversation_id": "conv-r2"}]
    gone = [closed("row-c1", "Parking permit renewal", at(7, 15), "lapsed"),
            closed("row-c2", "Reply to the visit request", at(10, 40), "linked_send", "conv-w40"),
            closed("row-c3", "Newsletter sign-up", at(12, 2), "detector_sibling"),
            closed("row-c4", "Room booking for Friday", at(12, 30), "user_confirmed")]
    src = H.Sources(now=now, zone=ZONE, briefing=briefing, requests=reqs, replies=replies,
                    open_ids={"deadline-w1", "task-s2", "response-r1", "response-r2"}, closed=gone,
                    light="ok", reports=[{"kind": "morning-briefing", "text": REPORT, "at": at(4, 5)}],
                    checkin={"prev": at(8, 12), "last": now},
                    recent=[{"channel": main, "thread_key": "t7", "unread": 1, "last_at": at(12, 50)}], dm=dm)
    if state == "done":
        src.requests = {"waiting": [], "done": reqs["done"]}
        src.replies = []
        src.briefing = {**briefing, "today_top": [], "onboarding_question": None}
    elif state == "red":
        src.light = "problem"
    elif state == "overdue":
        src.briefing = {**briefing, "today_top": [{**top[0], "title": f"Due {(today - timedelta(days=1)).isoformat()}: "
                                                   "Send the workshop review"}, top[1]]}
    elif state == "travel":
        src.travel = {"enabled": True, "timezone": "Europe/Lisbon", "until": (today + timedelta(days=2)).isoformat()}
        src.device_zone = "Europe/Lisbon"
    elif state == "vacation":
        src.briefing = {**briefing, "todays_events": [all_day("Vacation", today - timedelta(days=1), 5)] + evs[:2],
                        "onboarding_question": None}
    return src


def payload(state: str = "normal", dm: str = "dm", main: str = "main") -> dict:
    p = H.build(sources(state, dm, main))
    if state == "model":
        p["greeting"].update(text="Afternoon. Two replies and a tap stand between you and a quiet evening.",
                             by="model")
    return p
