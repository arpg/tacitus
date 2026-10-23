#!/usr/bin/env python3
"""invite_eval: the scheduling pipeline's invites, scored against the
thread and the calendar, nightly.

For each live `invite` turn the pipeline recorded in the window:

  time       the event's start is the time the thread accepted
             (sched_thread_state: the user's offer, the other side's pick)
  duration   the event's length is the duration the intent read
  attendee   the sender is on the event
  mode       a thread that asked for a phone call got no online meeting
  zone       the event's offset is the user's zone on that date
  created    an event with that subject or start is on the calendar
  calendar   the created event's start and end equal the staged ones

A check is True, False or None (not decidable from the data). The
window's counts and every failing invite go to a JSON file under the
owner's store and, with --post, one line to the health channel. Owner
only: it reads the pipeline's records, the mailbox and the calendar.

  python3 preprocessor/invite_eval.py [--days 1] [--post] [--json path]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

TURNS = "pipeline-turns.jsonl"
OUT_DIR = "invite-eval"
CHECKS = ("time", "duration", "attendee", "mode", "zone", "created", "calendar")
PHONE_WORDS = ("phone", "call me", "call you", "dial")


def _dt(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _same(a, b) -> bool:
    da, db = _dt(a), _dt(b)
    return bool(da and db and da.astimezone(timezone.utc) == db.astimezone(timezone.utc))


def thread_wants_phone(messages: list[dict], user_addrs: set) -> bool | None:
    """True when an inbound message asks for a phone call and names no
    video platform; False when one names video; None with neither."""
    import sched_thread_state as st
    asked, video = False, False
    for m in messages or []:
        if st._own(m, user_addrs):
            continue
        t = st._text(m).lower()
        if any(w in t for w in PHONE_WORDS):
            asked = True
        if any(w in t for w in ("zoom", "teams", "meet.google", "video")):
            video = True
    if asked and not video:
        return True
    if video:
        return False
    return None


def score_invite(rec: dict, thread_state: dict, *, wants_phone: bool | None, calendar: list[dict],
                 user_tz: ZoneInfo) -> dict:
    """The checks for one invite record (pure)."""
    ev = rec.get("event") or {}
    intent = rec.get("intent") or {}
    out = {"run_id": rec.get("run_id"), "ts": rec.get("ts"), "thread_ts": rec.get("thread_ts"),
           "subject": ev.get("subject") or "", "start": ev.get("start") or "", "end": ev.get("end") or "",
           "checks": {}, "notes": []}
    c = out["checks"]
    start, end = _dt(ev.get("start")), _dt(ev.get("end"))
    accepted = (thread_state or {}).get("accepted") or {}
    if accepted.get("start") and start:
        c["time"] = _same(accepted["start"], start)
        if not c["time"]:
            out["notes"].append(f"thread accepted {accepted['start']}, event starts {ev.get('start')}")
    else:
        c["time"] = None
    dur = intent.get("duration_minutes")
    if start and end and dur:
        c["duration"] = int((end - start).total_seconds() // 60) == int(dur)
        if not c["duration"]:
            out["notes"].append(f"event is {int((end - start).total_seconds() // 60)} min, intent said {dur}")
    else:
        c["duration"] = None
    sender = str(intent.get("sender_email") or "").lower()
    attendees = [a.strip().lower() for a in str(ev.get("attendees") or "").split(",") if a.strip()]
    c["attendee"] = (sender in attendees) if sender and attendees else None
    if c["attendee"] is False:
        out["notes"].append("the sender is not on the event")
    if wants_phone is None:
        c["mode"] = None
    else:
        c["mode"] = (not ev.get("online")) if wants_phone else True
        if c["mode"] is False:
            out["notes"].append("the thread asked for a phone call; the event carries an online meeting")
    if start:
        local = start.astimezone(user_tz)
        c["zone"] = start.utcoffset() == local.utcoffset()
        if not c["zone"]:
            out["notes"].append(f"event offset {start.utcoffset()} is not the user's {local.utcoffset()}")
    else:
        c["zone"] = None
    match = None
    for e in calendar or []:
        es = _dt((e.get("start") or {}).get("dateTime", "") + "+00:00" if isinstance(e.get("start"), dict)
                 and not str((e.get("start") or {}).get("dateTime", "")).endswith(("Z", "+00:00")) else
                 (e.get("start") or {}).get("dateTime", "") if isinstance(e.get("start"), dict) else e.get("start"))
        subj = str(e.get("subject") or "").casefold()
        if (start and es and es == start.astimezone(timezone.utc)) or (subj and subj == str(ev.get("subject") or "").casefold()):
            match = e
            break
    c["created"] = match is not None
    if match is not None:
        ms = match.get("start") or {}
        me = match.get("end") or {}
        ms = ms.get("dateTime") if isinstance(ms, dict) else ms
        me = me.get("dateTime") if isinstance(me, dict) else me
        ms_dt, me_dt = _dt(ms), _dt(me)
        # Graph returns calendarView times in UTC without an offset
        if ms_dt and ms_dt.tzinfo == timezone.utc and start and end and me_dt:
            c["calendar"] = (ms_dt == start.astimezone(timezone.utc) and me_dt == end.astimezone(timezone.utc))
        else:
            c["calendar"] = None
        if c["calendar"] is False:
            out["notes"].append(f"calendar has {ms} to {me}")
    else:
        c["calendar"] = None
    out["ok"] = all(v is not False for v in c.values())
    return out


def summarize(scores: list[dict]) -> dict:
    counts = {k: {"true": 0, "false": 0, "none": 0} for k in CHECKS}
    for s in scores:
        for k in CHECKS:
            v = s["checks"].get(k)
            counts[k]["true" if v is True else "false" if v is False else "none"] += 1
    return {"invites": len(scores), "failing": [s for s in scores if not s["ok"]], "counts": counts}


def render(summary: dict, day: str) -> str:
    n = summary["invites"]
    if not n:
        return f":calendar: invite eval {day}: no invites"
    bad = len(summary["failing"])
    parts = [f"{k} {c['true']}/{c['true'] + c['false']}" for k, c in summary["counts"].items()
             if c["true"] + c["false"]]
    line = f":calendar: invite eval {day}: {n} invite{'s' if n != 1 else ''}, {bad} with a failed check; " + ", ".join(parts)
    for s in summary["failing"][:5]:
        line += f"\n• {s['subject'][:50]} ({str(s['start'])[:16]}): " + "; ".join(s["notes"][:3])
    return line


# ── owner-side run ────────────────────────────────────────────────────

def _user_addrs() -> set:
    ea = Path(os.environ.get("EA_DIR", "/home/ea-bot/.ea"))
    try:
        prof = json.loads((ea / "config" / "user-profile.json").read_text())
    except (OSError, ValueError):
        return set()
    return {str(prof.get("email") or "").lower()} | {str(a).lower() for a in prof.get("aliases") or []}


def run(days: int, *, post: bool, json_path: str) -> dict:
    import remote_llm
    import graph_client as graph
    import sched_thread_state as st
    from sched_agent import DEFAULT_TZ
    tz = ZoneInfo(DEFAULT_TZ)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recs = [r for r in remote_llm.read(TURNS)
            if r.get("kind") == "invite" and r.get("live") and (_dt(r.get("ts")) or since) >= since]
    users = _user_addrs()
    scores = []
    for rec in recs:
        conv = rec.get("conversation_id") or ""
        msgs, state, phone = [], {}, None
        if conv:
            try:
                got = graph.get_conversation_messages(conv, top=50)
                msgs = got[0] if isinstance(got, tuple) else got
                msgs = [m for m in (msgs or []) if isinstance(m, dict)]
            except Exception as e:
                print(f"  thread {conv[-10:]} not read: {e}", file=sys.stderr)
            if msgs:
                state = st.analyze(msgs, users, tz=tz)
                phone = thread_wants_phone(msgs, users)
        cal = []
        start = _dt((rec.get("event") or {}).get("start"))
        if start:
            try:
                cal = graph.get_calendar_view(start - timedelta(hours=2), start + timedelta(hours=4))
            except Exception as e:
                print(f"  calendar not read: {e}", file=sys.stderr)
        scores.append(score_invite(rec, state, wants_phone=phone, calendar=cal, user_tz=tz))
    summary = summarize(scores)
    day = datetime.now(tz).strftime("%Y-%m-%d")
    out_path = Path(json_path) if json_path else (remote_llm.store_dir() / OUT_DIR / f"{day}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"day": day, "days": days, "summary": summary}, indent=1, default=str))
    text = render(summary, day)
    print(text)
    if post:
        try:
            import chat_notify
            chat_notify.post(chat_notify.health_channel_key(), text)
        except Exception as e:
            print(f"  health post failed: {e}", file=sys.stderr)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--post", action="store_true", help="post the line to the health channel")
    ap.add_argument("--json", default="", help="write the scores here (default: the owner's store)")
    args = ap.parse_args(argv)
    run(args.days, post=args.post, json_path=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
