#!/usr/bin/env python3
"""The invite for an accepted time, staged by code with no model.

The other side took a time the user offered. The acceptance is grounded
when the reply's own words name one of the times in the user's last offer
in the thread (sched_thread_state), or when the remote record's
"confirmation" names one (preprocess.detect_scheduling_requests). For a
grounded acceptance, build_event writes the event in the shape the
Create card takes (pending-event.json; docs/ea/scheduling.md):

  subject     the thread's subject without its Re:/Fwd: prefixes, else
              "<user first> / <their first>"
  start, end  the accepted slot on the user's clock (the profile's zone).
              Its length: the end the reply names; else the offered
              slot's end, cut to the length the offer states when the
              slot is a longer window; else the length the offer states
              ("a 30-minute Zoom"); else the user's
              preferred_duration_minutes (scheduling-preferences.json)
  attendees   the one who accepted, who is the one person the offer was
              addressed to; optional: everyone else on the offer's and the
              reply's Cc and To lines. Never the user, never a no-reply
              address.
  online      true, unless the reply asks for a phone call or the offer or
              the reply says in person, and neither names a video platform
  timezone, conversation_id, ledger_id, source "sched-acceptance"

What is not one plain yes from the one person asked is not staged, and the
reason opens the scheduling thread a model then works (`handoff`): the
offer went to several people or not to the one who answered, the reply
hedges ("probably", "instead"), it named a window longer than the
meeting, no length is known, the time has passed, the meeting is already
on the calendar. A reply that names several offered times or other times
never reaches here: the detector files it as times proposed, with its
own reason.

stage() checks the slot on the user's calendars (scheduling_engine
validate-draft, run as this process). A conflict does not stop the
invite: the user offered the time and the other side took it, so the
thread says what it overlaps and the Create click decides. An event on
the calendar at that time that is this meeting (its subject, or the other
person's name in it) stops it. Each acceptance staged or handed off is
one line in the owner store (LOG): invite_eval scores the staged ones
nightly, sched_backtest counts them, and a later run does not stage the
same acceptance again.

Nothing here reads or writes mail. The offer's text stays in the owner
process; the queue entry carries the event, the reason and short notes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

SOURCE = "sched-acceptance"
LOG = "sched-acceptances.jsonl"
GRACE = timedelta(minutes=15)       # a slot that starts sooner than this has passed
MAX_MINUTES = 240
VALIDATE_TIMEOUT_S = 120
NOT_AN_ATTENDEE = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|postmaster@", re.I)

_MINUTES = re.compile(r"\b(\d{1,3})[- ]?(?:minutes?|mins?)\b", re.I)
_HOURS = re.compile(r"\b(an|one|1|1\.5|two|2)[- ]?hours?\b|\b(half)[- ]an?[- ]hour\b|\b(half)[- ]hour\b", re.I)
_VIDEO = re.compile(r"\b(?:zoom|teams|google meet|meet\.google|webex|video ?call|videoconference|virtual)\b", re.I)
_PHONE = re.compile(r"\b(?:phone|call me|call you|dial in|my cell)\b", re.I)
_IN_PERSON = re.compile(r"\b(?:in[- ]person|in my office|at my office|stop by|come by|swing by|"
                        r"(?:grab|over|for) (?:coffee|lunch))\b", re.I)
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv)\s*:\s*|\[[^\]]{0,40}\]\s*)+", re.I)
# A reply that takes a time but not plainly: "probably works", "or Thursday
# instead?". The thread decides those.
_HEDGE = re.compile(r"\b(instead|rather|alternatively|probably|tentative(?:ly)?|pencil)\b", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the user's side ───────────────────────────────────────────────────

def _read_resident(path: Path) -> str | None:
    """A config file in the resident's tree, read with the resident's
    access (the owner takes nothing from that tree on trust)."""
    try:
        import resident_io
        return resident_io.read_text(path)
    except Exception:
        return None


def _json(text: str | None) -> dict:
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def context(ea_dir: Path | None = None, reader=None) -> dict:
    """{"addrs", "first", "tz", "default_minutes"}: the user's addresses,
    first name, zone and default meeting length, from the profile and the
    scheduling preferences."""
    ea = Path(ea_dir or os.environ.get("EA_DIR") or (Path.home() / ".ea"))
    read = reader or _read_resident
    prof = _json(read(ea / "config" / "user-profile.json"))
    prefs = _json(read(ea / "config" / "scheduling-preferences.json"))
    addrs = set()
    for k in ("email", "secondary_email", "gmail", "personal_email"):
        v = prof.get(k)
        if isinstance(v, str) and "@" in v:
            addrs.add(v.strip().lower())
    for key in ("aliases", "calendar_accounts", "gmail_accounts"):
        vals = prof.get(key)
        for v in vals if isinstance(vals, list) else []:
            if isinstance(v, str) and "@" in v:
                addrs.add(v.strip().lower())
    try:
        import user_tz
        zone = user_tz.name(ea)
    except Exception:
        zone = "UTC"
    try:
        minutes = int((prefs.get("slot_selection") or {}).get("preferred_duration_minutes") or 0) or None
    except (TypeError, ValueError):
        minutes = None
    name = str(prof.get("name") or "").strip()
    return {"addrs": addrs, "first": name.split()[0] if name else "", "tz": zone, "default_minutes": minutes}


# ── reading the offer and the reply ───────────────────────────────────

def stated_minutes(text: str) -> int | None:
    """The meeting length an offer states ("a 30-minute Zoom", "an hour"),
    or None."""
    found = []
    for m in _MINUTES.finditer(text or ""):
        n = int(m.group(1))
        if 10 <= n <= MAX_MINUTES:
            found.append((m.start(), n))
    for m in _HOURS.finditer(text or ""):
        word = (m.group(1) or "").lower()
        n = 30 if (m.group(2) or m.group(3)) else {"an": 60, "one": 60, "1": 60, "1.5": 90, "two": 120, "2": 120}[word]
        found.append((m.start(), n))
    return min(found)[1] if found else None


def meeting_mode(offer_text: str, reply_text: str) -> tuple[bool, str]:
    """(online, note): an online meeting unless the reply asks for a phone
    call or either says in person, with no video platform named."""
    video = bool(_VIDEO.search(offer_text or "") or _VIDEO.search(reply_text or ""))
    if _PHONE.search(reply_text or "") and not _VIDEO.search(reply_text or ""):
        return False, "They asked for a phone call, so the invite has no video link."
    if not video and (_IN_PERSON.search(offer_text or "") or _IN_PERSON.search(reply_text or "")):
        return False, "The thread says in person, so the invite has no video link."
    return True, ""


def subject_line(subject: str, *, first: str = "", their_name: str = "") -> str:
    s = _SUBJECT_PREFIX.sub("", " ".join(str(subject or "").split())).strip()
    if s:
        return s[:120]
    theirs = (their_name or "").split()[0] if their_name else ""
    if first and theirs:
        return f"{first} / {theirs}"
    return "Meeting"


def attendee_sets(*, sender: str, offer_to: list, offer_cc: list, reply_to: list, reply_cc: list,
                  user: set) -> tuple[list[str], list[str]]:
    """(required, optional): the one who accepted and the offer's To line;
    everyone else on either message is optional. The user and no-reply
    addresses are never on it."""
    skip = {a.lower() for a in user}

    def ok(a: str) -> bool:
        return bool(a) and "@" in a and a not in skip and not NOT_AN_ATTENDEE.search(a)

    required: list[str] = []
    for a in [sender] + list(offer_to or []):
        a = str(a or "").strip().lower()
        if ok(a) and a not in required:
            required.append(a)
    optional: list[str] = []
    for a in list(offer_cc or []) + list(reply_to or []) + list(reply_cc or []):
        a = str(a or "").strip().lower()
        if ok(a) and a not in required and a not in optional:
            optional.append(a)
    return required, optional


def _dt(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d


def _label(start: datetime, end: datetime, zone: str) -> str:
    try:
        import sched_render
        return sched_render.option_label(start, end, zone)
    except Exception:
        return start.strftime("%a %b %-d, %-I:%M %p")


def build_event(slot: dict, *, offer: dict, reply: dict, ctx: dict, conversation_id: str = "",
                now: datetime | None = None) -> dict:
    """{"event": {...} | None, "why": "", "notes": [...], "when": "", "minutes": n}.

    `slot` is the accepted slot (sched_thread_state: start, end, named_end,
    window). `offer` is the user's offer (offer_of: subject, to, cc,
    text); `reply` the acceptance (from, from_name, to, cc, subject,
    text). `ctx` is context()."""
    tz = ZoneInfo(ctx.get("tz") or "UTC")
    out: dict = {"event": None, "why": "", "notes": [], "when": "", "minutes": 0}
    start = _dt(slot.get("start"))
    if start is None:
        out["why"] = "The accepted time could not be read."
        return out
    start = (start if start.tzinfo else start.replace(tzinfo=tz)).astimezone(tz)
    window = slot.get("window") or {}
    named_end = _dt(slot.get("end")) if slot.get("named_end") else None
    offered_end = _dt(window.get("end")) if window.get("end") else None
    stated = stated_minutes(offer.get("text") or "")
    if named_end is not None:
        named_end = named_end.astimezone(tz)
        length = int((named_end - start).total_seconds() // 60)
        if stated and length > stated:
            out["why"] = (f"They took the {_label(start, named_end, ctx.get('tz') or 'UTC')} window,"
                          f" and the meeting is {stated} minutes. The start is open.")
            return out
        end = named_end
    elif offered_end is not None:
        offered_end = offered_end.astimezone(tz)
        length = int((offered_end - start).total_seconds() // 60)
        end = start + timedelta(minutes=stated) if stated and length > stated else offered_end
    else:
        minutes = stated or ctx.get("default_minutes")
        if not minutes:
            out["why"] = "The offer gives no length for the meeting, and no default length is set."
            return out
        end = start + timedelta(minutes=int(minutes))
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0 or minutes > MAX_MINUTES:
        out["why"] = f"The accepted slot is {minutes} minutes long."
        return out
    now = now or _now()
    when = _label(start, end, ctx.get("tz") or "UTC")
    out.update(when=when, minutes=minutes)
    if start < now + GRACE:
        out["why"] = f"The accepted time, {when}, has passed."
        return out
    user = set(ctx.get("addrs") or set()) | ({offer["from"]} if offer.get("from") else set())
    sender = str(reply.get("from") or "").lower()
    asked = [a for a in (offer.get("to") or []) if a not in user]
    if asked and sender not in asked:
        out["why"] = "They were not among the people the offer went to."
        return out
    if len(asked) > 1:
        out["why"] = f"The offer went to {len(asked)} people, and one has answered."
        return out
    hedge = _HEDGE.search(reply.get("text") or "")
    if hedge:
        out["why"] = f'Their reply is not a plain yes ("{hedge.group(1).lower()}").'
        return out
    required, optional = attendee_sets(
        sender=sender, offer_to=offer.get("to") or [], offer_cc=offer.get("cc") or [],
        reply_to=reply.get("to") or [], reply_cc=reply.get("cc") or [], user=user)
    if not required:
        out["why"] = "No one is left to invite."
        return out
    online, note = meeting_mode(offer.get("text") or "", reply.get("text") or "")
    if note:
        out["notes"].append(note)
    event = {"subject": subject_line(reply.get("subject") or offer.get("subject") or "",
                                     first=ctx.get("first") or "", their_name=reply.get("from_name") or ""),
             "start": start.isoformat(), "end": end.isoformat(),
             "attendees": ",".join(required), "online": online,
             "timezone": ctx.get("tz") or "UTC", "conversation_id": conversation_id, "source": SOURCE}
    if optional:
        event["optional_attendees"] = ",".join(optional)
    out["event"] = event
    return out


# ── the calendar check and the stage ──────────────────────────────────

def run_engine(argv: list[str]) -> tuple[int, str, str]:
    """scheduling_engine as this process (the owner reads its own
    calendars). Tests replace this."""
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "scheduling_engine.py")] + argv,
                           capture_output=True, text=True, timeout=VALIDATE_TIMEOUT_S, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except OSError as e:
        return -1, "", str(e)


def _parse_json(out: str):
    i = (out or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(out[i:])
    except ValueError:
        return None


def validate(event: dict, runner=None) -> dict:
    """{"conflicts": [subjects], "fetch_errors": bool, "checked": bool}
    for the event's slot on the user's own calendars."""
    runner = runner or run_engine
    start, end = _dt(event.get("start")), _dt(event.get("end"))
    minutes = int((end - start).total_seconds() // 60) if start and end else 30
    rc, out, _err = runner(["validate-draft", "--times", json.dumps([event.get("start")]),
                            "--duration", str(minutes)])
    parsed = _parse_json(out)
    if not isinstance(parsed, dict) or "issues" not in parsed:
        return {"conflicts": [], "fetch_errors": False, "checked": False}
    conflicts = []
    for i in parsed.get("issues") or []:
        if isinstance(i, dict) and i.get("type") == "conflict":
            subj = str(i.get("conflict_with") or "an event")
            if subj not in conflicts:
                conflicts.append(subj)
    return {"conflicts": conflicts, "fetch_errors": bool(parsed.get("calendar_fetch_errors")), "checked": True}


def same_meeting(conflict: str, event: dict, names: list[str]) -> bool:
    """An overlapping event that is this meeting already: the same title,
    or a title that names the other person."""
    c = (conflict or "").strip().lower()
    if not c:
        return False
    if c == str(event.get("subject") or "").strip().lower():
        return True
    return any(len(n) >= 3 and re.search(r"(?<![a-z])" + re.escape(n.lower()) + r"(?![a-z])", c) for n in names)


def _key(item: dict) -> str:
    return f"{item.get('conversation_id') or ''}|{str(item.get('received') or '')[:16]}"


def _store_read() -> list[dict]:
    try:
        import remote_llm
        return remote_llm.read(LOG)
    except Exception:
        return []


def _store_append(rec: dict) -> None:
    try:
        import remote_llm
        remote_llm.append(LOG, rec)
    except Exception as e:
        print(f"  acceptance log not written: {e}", file=sys.stderr)


def seen(item: dict, records: list[dict] | None = None) -> bool:
    """True when this acceptance was staged or handed off before (the same
    conversation and minute, or the same message)."""
    recs = _store_read() if records is None else records
    key, mid = _key(item), item.get("message_id") or ""
    return any(r.get("key") == key or (mid and r.get("message_id") == mid) for r in recs)


def stage(items: list[dict], *, runner=None, set_last_action=None, now: datetime | None = None,
          record: bool = True) -> list[dict]:
    """Stage the invite for each item whose `acceptance` carries an event;
    give the others their `handoff` reason. Returns the items to queue: a
    kept item that ends without an event is dropped, since no model may
    read it. Each item leaves with `staged_event`, `stage_when` and
    `stage_notes`, or with `handoff`; its `acceptance` is removed."""
    now = now or _now()
    if set_last_action is None:
        def set_last_action(row_id: str, text: str) -> None:
            try:
                import ledger
                ledger.set_last_action(row_id, text)
            except Exception as e:
                print(f"  ledger row {row_id} not updated: {e}", file=sys.stderr)
    out = []
    for it in items or []:
        acc = it.pop("acceptance", None)
        if not isinstance(acc, dict):
            out.append(it)
            continue
        base = {"ts": now.isoformat(), "key": _key(it), "message_id": it.get("message_id") or "",
                "conversation_id": it.get("conversation_id") or "", "received": it.get("received") or "",
                "sender": it.get("from") or "", "kept": bool(it.get("kept")), "source": SOURCE}
        event = acc.get("event")
        why = acc.get("why") or ""
        notes = list(acc.get("notes") or [])
        check: dict = {}
        if event:
            check = validate(event, runner)
            names = [w for w in str(it.get("from_name") or "").replace(",", " ").split() if w]
            same = [c for c in check["conflicts"] if same_meeting(c, event, names)]
            if same:
                why = f"A meeting at that time is already on your calendar: {same[0]}."
                event = None
            else:
                if check["conflicts"]:
                    notes.append("That time now overlaps " + "; ".join(check["conflicts"][:2]) + " on your calendar.")
                if check["fetch_errors"]:
                    notes.append("A calendar could not be read, so conflicts are not fully checked.")
                elif not check["checked"]:
                    notes.append("The calendar check did not run.")
        if event:
            row_id = it.get("row_id") or ""
            event = dict(event, ledger_id=row_id)
            it.update(staged_event=event, stage_when=acc.get("when") or "", stage_notes=notes)
            if row_id:
                set_last_action(row_id, f"{now.date().isoformat()}: the invite for {acc.get('when') or event['start']}"
                                        " is staged from their acceptance; the Create button is in the DM.")
            if record:
                _store_append(dict(base, status="staged", event=event, minutes=acc.get("minutes") or 0,
                                   conflicts=check.get("conflicts") or [], notes=notes))
            out.append(it)
            continue
        if record:
            _store_append(dict(base, status="handoff", why=why))
        if it.get("kept"):
            continue
        it["handoff"] = why
        out.append(it)
    return out


# ── a whole thread (the backtest's replay, the eval) ──────────────────

def from_thread(messages: list[dict], ctx: dict, *, conversation_id: str = "",
                now: datetime | None = None) -> dict:
    """The thread-state reading of one thread and, for an acceptance, the
    event code would stage: {"state", "accepted", "event", "why", "notes",
    "when", "inbound_at"}. `now` defaults to the reply's own time, so a
    replay judges the time as it stood then."""
    import sched_thread_state as st
    tz = ZoneInfo(ctx.get("tz") or "UTC")
    a = st.analyze(messages, ctx.get("addrs") or set(), tz=tz)
    out = {"state": a["state"], "accepted": a.get("accepted"), "event": None, "why": "", "notes": [],
           "when": "", "inbound_at": a.get("inbound_at") or ""}
    if a["state"] != "accepted":
        return out
    msgs = st.thread_order(messages)
    om, rm = msgs[a["offer_index"]], msgs[a["inbound_index"]]
    offer = st.offer_of(om, a["offered"])
    reply = {"from": st._addr(rm), "from_name": st._name(rm), "to": st.addrs(rm, "to"), "cc": st.addrs(rm, "cc"),
             "subject": str(rm.get("subject") or ""), "text": st._text(rm)}
    at = now or _dt(a.get("inbound_at")) or _now()
    plan = build_event(a["accepted"], offer=offer, reply=reply, ctx=ctx, conversation_id=conversation_id, now=at)
    out.update(event=plan["event"], why=plan["why"], notes=plan["notes"], when=plan["when"])
    return out
