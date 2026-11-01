#!/usr/bin/env python3
"""Briefings on the remote model: the morning briefing and the afternoon
update without a Claude run (docs/llm-lanes.md, "Briefings").

run-agent.sh calls this for morning-briefing and afternoon-catchup after
the preprocessor has written the lane's JSON, and posts what it prints as
it posts a Claude lane's text. The weekly audit has its own lane (remote_audit.py).

    remote_briefing.py run --agent morning-briefing|afternoon-catchup
                           --briefing <json> --run-id <cron-...>
                           [--stale] [--dry-run] [--preview]

Switch: `"briefings": "remote"` in remote-llm.json. Off, or outside an
owner process, `run` exits 3 and run-agent.sh runs the Claude lane.

The morning run:
  1. Code gives every candidate item a ref: today's ranked queue, new mail
     that needs a reply, dropped threads, RSVPs, and the open ledger rows
     (deadlines, follow-ups, stale rows). Mail kept from Claude is not a
     candidate: the post lands in the EA stream, which Claude lanes read.
  2. One remote call, under a strict schema, picks by ref: the ordered
     "get done today" list with when and why, the flexes, the replies to
     draft and what each must say, the decisions for the user, and the
     items to hand to Claude. A ref the call invents is dropped.
  3. Code applies the rails: no draft to a dean or a program officer, on
     money, a contract or a legal matter, for a decline, or to someone
     outside the institution's safety-rail domain the user never wrote
     to. Each becomes a
     decision for the user.
  4. Each draft: email_writer writes the body; graph_actions create-reply
     makes the draft under this run's id, so the bot posts its Send block
     as for any cron draft; the ledger row gets its last_action; draft_ab
     records the draft for the send measure.
  5. Escalations go to the bot's queue (ESCALATIONS). The bot opens one EA
     topic for a briefing's items, and one Claude session reads their threads.
  6. Code renders the post in the skill's shape, naming each item once.
     The open-claims audit asks about every ledger row the post shows,
     with Claude's verdicts (the remote model's when Claude fails); rows
     it closes or parks leave the post, and the post is rendered again.
  7. Each project room with items in the post gets its slice, in a new
     topic (briefing_rooms); the EA post names those rooms in one line.
The afternoon run picks what is new since the morning and what is still
pending; it drafts and escalates nothing. When the remote model cannot
answer, code renders the ranked queue alone and the post says so.

--dry-run does the reads and the remote call, then makes no draft, queues
nothing, closes nothing and prints counts only; the post is in the run
record. --preview (with --dry-run) also posts it for the user where the
mail digests go: the private mail-digest stream on Zulip, the digest
channel on the web chat (chat_notify.post_digest).

Store: <private_dir>/remote-llm/briefings.jsonl, one record per run, and
escalated.jsonl, the threads handed to Claude (one hand-off per thread a
week).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import institution as _institution  # noqa: E402
import ledger as _ledger  # noqa: E402
import remote_llm  # noqa: E402
import sched_render  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
ESCALATIONS = EA_DIR / "state" / "remote-escalations.json"
RUNS = "briefings.jsonl"
ESCALATED = "escalated.jsonl"
EXIT_OFF = 3
AGENTS = ("morning-briefing", "afternoon-catchup")
MAX_TOP, MAX_FLEX, MAX_DRAFTS, MAX_ESCALATE = 5, 4, 8, 3
CAPS = {"R": 30, "D": 15, "V": 10, "L": 40}
SOON_DAYS = 3
ESCALATE_AGAIN = timedelta(days=7)
QUEUE_MAX_AGE = timedelta(hours=72)
_MONEY = re.compile(r"\$\s?\d|\b(budget|invoice|payment|reimburs\w*|contract|agreement|legal|"
                    r"lawyer|nda|salary|offer letter|purchase order)\b", re.I)
_ROLES = re.compile(r"\b(dean|program officer|program manager|provost|chancellor)\b", re.I)
_DECLINE = re.compile(r"\b(declin\w*|turn (it|this|them) down|not able to|unable to|can(no|')t)\b", re.I)


def active(cfg: dict | None = None) -> bool:
    """The briefings switch at `remote`, or the routing table's `briefing`
    entry (model_routing.py) where it names local or hosted."""
    cfg = cfg if cfg is not None else remote_llm.config()
    return remote_llm.local_for("briefing", cfg.get("briefings") == "remote", cfg)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tz():
    """The user's zone (user_tz.py: the profile, the scheduling
    preferences, then the system's zone). Every day and clock time the
    briefing shows is on it, never on the machine's clock."""
    try:
        import user_tz
        return user_tz.zone()
    except Exception:
        return None   # astimezone(None): the system's zone


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def _clean(text, cap: int = 300) -> str:
    """Model or mail text as one post line: no dashes the user does not
    write, no markup that could break the post, at most `cap` characters."""
    t = " ".join(str(text or "").split())
    t = t.replace("—", ", ").replace("–", "-").replace("*", "").replace("`", "")
    return t[:cap].rstrip(" ,;")


# ── the candidates ────────────────────────────────────────────────────

# The briefing's own guards against stale items, held here in code since
# the remote model only picks refs: a row that is not live today (parked,
# snoozed, follow-up, closed) is never a candidate; mail in a conversation
# a row closed this week is not news; a draft is never made for an item
# the user has already answered (check_plan); a draft claim the preprocessor
# found stale is shown as stale. A message in a thread he answered earlier
# is news only when it asks something new: that is the model's call, told
# by `replied_earlier`, since a reply after the message never gets here.
_NOT_LIVE = ("snoozed", "follow_up", "completed", "closed")
_RECONCILED = "[auto-reconciled-drafts"


def _live_row(o: dict, today: date) -> bool:
    return o.get("status", "open") not in _NOT_LIVE and not _ledger.parked_until(o, today)


def _last_action(o: dict) -> str:
    """The row's last action, with preprocess.reconcile_draft_claims' note
    first when it found the draft the text promises gone: the note is
    appended at the end and would fall past the cut."""
    la = str(o.get("last_action") or "")
    if _RECONCILED in la:
        note = la[la.index(_RECONCILED):].split("]", 1)[-1].strip()
        return f"(note: {note}) {la[:la.index(_RECONCILED)].strip()}"[:200]
    return la[:200]


def candidates(b: dict, *, today: date) -> dict[str, dict]:
    """{ref: item}. Every fact the post shows comes from these items; the
    remote model only picks refs and writes the short lines."""
    opens = [o for o in b.get("open_obligations") or [] if _live_row(o, today)]
    closed_convs = {r.get("conversation_id") for r in b.get("recently_closed") or []
                    if r.get("conversation_id")}
    by_mid = {o.get("source_message_id"): o for o in opens if o.get("source_message_id")}
    by_conv = {o.get("conversation_id"): o for o in opens if o.get("conversation_id")}
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}

    def add(prefix: str, item: dict) -> None:
        if counts.get(prefix, 0) >= CAPS[prefix]:
            return
        counts[prefix] = counts.get(prefix, 0) + 1
        out[f"{prefix}{counts[prefix]}"] = item

    seen_rows: set[str] = set()
    for prefix, key, kind in (("R", "new_needs_response", "reply"),
                              ("D", "new_dropped_threads", "dropped"),
                              ("V", "pending_rsvps", "rsvp")):
        for it in b.get(key) or []:
            mid, conv = it.get("message_id") or "", it.get("conversation_id") or ""
            if conv and conv in closed_convs:
                continue  # a row on this thread closed this week
            row = by_mid.get(mid) or by_conv.get(conv) or {}
            rec = it.get("remote_record") or {}
            due, tags = str(rec.get("deadline") or "")[:10], []
            d = _date(due)
            if d and d < today and str(rec.get("scheduling") or "").lower() in _ledger.MEETING_KINDS:
                # The record's date is the meeting's; held, it is history.
                tags, due = [f"the meeting was {d:%b} {d.day}"], ""
            item = {"kind": kind, "title": it.get("subject") or "(no subject)",
                    "who": it.get("from_name") or it.get("from") or "", "from": it.get("from") or "",
                    "message_id": mid, "conversation_id": conv,
                    "summary": it.get("body_preview") or "", "ask": rec.get("ask") or "",
                    "people": rec.get("people") or [], "due": due, "tags": tags,
                    "cc_only": bool(it.get("user_is_cc_only")),
                    "in_slack": bool(it.get("mentioned_in_slack")),
                    # Sent mail checked in code (preprocess.split_replied_items):
                    # the user wrote to this sender, in another thread, after
                    # their message. Not a reply, but not untouched news.
                    "wrote_since": it.get("wrote_to_since") or None,
                    # He answered an earlier message in this thread; this one
                    # came after (a reply after it never reaches the briefing).
                    "replied_earlier": bool(it.get("user_already_replied_in_thread")),
                    "unread_by_remote": bool(it.get("remote_unread")),
                    "ledger_id": row.get("id") or "",
                    "brief": row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else None}
            if item["ledger_id"]:
                seen_rows.add(item["ledger_id"])
            if isinstance(row.get("close_proposal"), dict):
                continue  # looks done: on the "Looks done?" line, not an ask
            add(prefix, item)
    stale = {o.get("id") for o in b.get("stale_obligations") or []}
    resurrected = {o.get("id") for o in b.get("resurrected_follow_ups") or []}
    rows = list(opens) + [o for o in (b.get("stale_obligations") or []) if o.get("id") not in
                          {r.get("id") for r in opens} and _live_row(o, today)]
    for o in rows:
        lid = o.get("id") or ""
        if not lid or lid in seen_rows or isinstance(o.get("close_proposal"), dict):
            continue
        seen_rows.add(lid)
        if _ledger.waiting_on_reply(o, today):
            continue  # his question is out: on the "Waiting on replies" line
        tags = [t for t, on in (("stale", lid in stale), ("follow-up due", lid in resurrected)) if on]
        unanswered = _ledger.unanswered_since(o, today)
        if unanswered:
            tags.append(f"no answer to your question since {unanswered}")
        due = str(o.get("deadline") or "")[:10]
        spent = _ledger.date_spent(o, today)
        if spent:
            # A passed date that is not overdue work (the meeting was
            # held; the user wrote back after it) leaves the date banner
            # and reaches the model as history, not as a due date.
            d = _date(due)
            tags.append(f"its {d:%b} {d.day} date passed; {spent}")
            due = ""
        brief = o.get("thread_brief") if isinstance(o.get("thread_brief"), dict) else None
        add("L", {"kind": "ledger", "title": o.get("description") or lid,
                  "who": o.get("from_name") or o.get("from_email") or "",
                  "from": o.get("from_email") or "", "ledger_id": lid,
                  "brief": brief,
                  "wrote_since": ({"at": "", "subject": "this thread"}
                                  if brief and brief.get("user_replied_after_latest") else None),
                  "message_id": o.get("source_message_id") or "",
                  "conversation_id": o.get("conversation_id") or "",
                  "summary": (o.get("notes") or "")[:400],
                  "last_action": _last_action(o),
                  "due": due, "tags": tags,
                  "type": o.get("type") or ""})
    return out


def top_refs(b: dict, cands: dict[str, dict]) -> list[dict]:
    """today_top in code's order, each entry pointed at its candidate."""
    out = []
    for t in b.get("today_top") or []:
        ref = next((k for k, c in cands.items()
                    if t.get("ref") and t.get("ref") in (c.get("message_id"), c.get("ledger_id"))), "")
        if ref:
            out.append({"ref": ref, "reason": t.get("reason") or ""})
    return out


# ── the remote call ───────────────────────────────────────────────────

def _items(*props) -> dict:
    return {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {p: {"type": "string"} for p in props}, "required": list(props)}}


MORNING_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "get_done_today": _items("ref", "when", "why"), "flexes": _items("ref", "line"),
    "drafts": _items("ref", "reply_must_say"), "needs_you": _items("ref", "decision"),
    "escalate": _items("ref", "why", "task")},
    "required": ["get_done_today", "flexes", "drafts", "needs_you", "escalate"]}
AFTERNOON_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "since_morning": _items("ref", "line"), "pending": _items("ref", "line")},
    "required": ["since_morning", "pending"]}

MORNING_PROMPT = """You plan a professor's morning briefing for his assistant. Answer with JSON only, in the schema.

{profile}
It is {now}.

He wants an ordered do-list fitted to the gaps in his day, then the flexes, then one-line bookkeeping. Every line tells him what to do. Write short plain sentences. Never write an em-dash. Use only the items given, and pick them by ref.

- get_done_today: at most 5 refs, in the order to do them. Start from today_top's order. Drop an item whose summary or last action shows it is settled. An item with `wrote_since` (he has written to that person since their mail) or `in_slack` goes in only if the ask is plainly still open, and the why says what he already did. An item with `replied_earlier` is a new message in a thread he already answered: it goes in, or gets a draft, only when it asks him something new. You may promote an item the ranking cannot see, and the why says so. `when` is a window from today's free time ("before your 9:30", "in the 1-2:30 gap", "first thing"). `why` is a few words.
- flexes: at most 4 more refs, one short line each.
- drafts: replies he would send that the item's summary and ask already answer: a yes, an answer he gave before, a link or a form, an intro, a meeting he already agreed. For each, what the reply must say in one or two sentences. When their ask is a question only he can answer (what he plans, what he needs, his use case, his preference), the reply only thanks them and says he will follow up; never answer for him, and never repeat their own message back to them. Never draft for money, a contract or a legal matter; a dean or a program officer; a decline; a decision only he can make; mail where he is only copied. Those go to needs_you.
- needs_you: refs with the decision in one line, and what happens once he decides.
- escalate: at most 3 refs that need more than a reply or a decision: a document to write, a multi-step task, a reply the summary does not say enough to write. Give why, and the task for the assistant.
A ref goes in at most one of drafts, needs_you and escalate.

{rules}"""

AFTERNOON_PROMPT = """You write a professor's short afternoon update for his assistant. Answer with JSON only, in the schema.

{profile}
It is {now}. The morning briefing already covered the day.

- since_morning: at most 4 refs of mail that came in since the morning (R, D and V refs) and needs him, one short line each. Leave out what can wait. Never an item with `wrote_since` or `in_slack`: his sent mail or the EA channel already shows him on it, and the update lists those separately.
- pending: at most 4 open items (any ref) with no action today that still need him, one short line each.
Write short plain sentences. Never write an em-dash. Use only the items given, and pick them by ref.

{rules}"""


_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _at(value, zone: str = "") -> datetime | None:
    """A calendar time on the user's clock (`_tz`). Graph's calendar view
    prints UTC wall-clock with no offset and timeZone "UTC"; a free slot
    carries its offset. A time with neither is UTC."""
    m = _TIME.match(str(value or "").strip())
    if not m:
        return None
    try:
        if m.group(2):
            dt = datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(2) == "Z" else m.group(2)))
        else:
            try:
                src = ZoneInfo(zone) if zone and zone.upper() != "UTC" else timezone.utc
            except Exception:
                src = timezone.utc
            dt = datetime.fromisoformat(m.group(1)).replace(tzinfo=src)
    except ValueError:
        return None
    return dt.astimezone(_tz())


def _event_times(e: dict) -> tuple[datetime | None, datetime | None]:
    st, en = e.get("start") or {}, e.get("end") or {}
    return _at(st.get("dateTime"), st.get("timeZone") or ""), _at(en.get("dateTime"), en.get("timeZone") or "")


def _span(start: datetime | None, end: datetime | None) -> str:
    """'9:30-10:30 AM' on the user's clock."""
    if start and end:
        return sched_render.clock_range(start, end)
    return sched_render.clock(start) if start else ""


def _calendar(b: dict, today: date) -> list[str]:
    out = []
    for e in b.get("todays_events") or []:
        if e.get("isAllDay"):
            out.append(f"all day: {_clean(e.get('subject'), 80)}")
            continue
        start, end = _event_times(e)
        if start:
            out.append(f"{_span(start, end)} {_clean(e.get('subject'), 80)}")
    return out[:14]


def _free(b: dict, today: date) -> list[str]:
    out = []
    for s in b.get("availability_merged") or []:
        start, end = _at(s.get("start")), _at(s.get("end"))
        if start and start.date() == today:
            out.append(_span(start, end))
    return out[:10]


def _wrote_since_line(c: dict) -> str:
    w = c.get("wrote_since") or {}
    if not w:
        return ""
    when = str(w.get("at") or "")[:10]
    return f"he wrote to them{' on ' + when if when else ''} since, subject: {w.get('subject') or ''}".strip()


def in_motion(c: dict) -> str:
    """Why this item is not news: what the sent folder or the EA channel
    already shows. Empty when nothing does."""
    if c.get("wrote_since"):
        return _wrote_since_line(c)
    if c.get("in_slack"):
        return "already in the EA channel"
    return ""


def _prompt_items(cands: dict[str, dict]) -> list[dict]:
    keep = ("kind", "title", "who", "summary", "ask", "due", "cc_only", "in_slack",
            "replied_earlier", "last_action", "tags", "type")
    out = []
    for r, c in cands.items():
        d = {"ref": r, **{k: c[k] for k in keep if c.get(k)}}
        if c.get("wrote_since"):
            d["wrote_since"] = _wrote_since_line(c)
        out.append(d)
    return out


def ask_plan(agent: str, b: dict, cands: dict[str, dict], *, rules: str, now: datetime,
             chat=None) -> dict:
    """The remote model's picks, refs checked against the candidates.
    Raises remote_llm.RemoteLLMError when it cannot answer."""
    chat = chat or remote_llm.chat_json
    local = now.astimezone(_tz())
    morning = agent == "morning-briefing"
    system = (MORNING_PROMPT if morning else AFTERNOON_PROMPT).format(
        profile=_profile(), now=f"{local:%A %Y-%m-%d %H:%M}", rules=rules or "")
    user = {"today_top": top_refs(b, cands) if morning else [],
            "items": _prompt_items(cands),
            "today_calendar": _calendar(b, local.date()), "free_today": _free(b, local.date()),
            "recently_closed": [_clean(r.get("description"), 120)
                                for r in (b.get("recently_closed") or [])][:20]}
    obj, _meta = chat([{"role": "system", "content": system},
                       {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
                      purpose=f"briefing-{'morning' if morning else 'afternoon'}",
                      schema=MORNING_SCHEMA if morning else AFTERNOON_SCHEMA,
                      schema_name="briefing_plan", max_tokens=3000)
    return check_plan(obj, cands, morning=morning)


def check_plan(obj: dict, cands: dict[str, dict], *, morning: bool) -> dict:
    """Unknown refs dropped, caps applied, a ref used once among drafts,
    decisions and escalations."""
    def picks(key, cap):
        out, seen = [], set()
        for p in (obj or {}).get(key) or []:
            if isinstance(p, dict) and p.get("ref") in cands and p["ref"] not in seen:
                seen.add(p["ref"])
                out.append(p)
        return out[:cap]
    if not morning:
        # Code holds the sent-mail check, not the prompt: whatever the
        # model picked, an item he has written to since (or that is in
        # the EA channel) leaves "since this morning" for its own line.
        since, motion = [], []
        for p in picks("since_morning", 8):
            (motion if in_motion(cands[p["ref"]]) else since).append(p)
        return {"since_morning": since[:4], "pending": picks("pending", 4), "in_motion": motion[:4]}
    # No reply is drafted to an item he has already answered, in its thread
    # or by writing to the sender since, or that is in the EA channel: the
    # draft would answer a question that is settled.
    plan = {"get_done_today": picks("get_done_today", MAX_TOP), "flexes": picks("flexes", MAX_FLEX),
            "drafts": [p for p in picks("drafts", MAX_DRAFTS)
                       if cands[p["ref"]]["kind"] in ("reply", "dropped", "rsvp")
                       and not in_motion(cands[p["ref"]])]}
    used = {p["ref"] for p in plan["drafts"]}
    plan["needs_you"] = [p for p in picks("needs_you", 8) if p["ref"] not in used]
    used |= {p["ref"] for p in plan["needs_you"]}
    plan["escalate"] = [p for p in picks("escalate", MAX_ESCALATE) if p["ref"] not in used]
    return plan


def _profile() -> str:
    try:
        import mail_privacy
        return mail_privacy._profile_line()
    except Exception:
        return "The user is a university professor."


def _rules() -> str:
    try:
        import preferences
        return preferences.render(["briefing", "mail-triage"])
    except Exception:
        return ""


# ── the rails ─────────────────────────────────────────────────────────

def _wrote_to_before(addr: str) -> bool:
    try:
        import draft_ab
        prof = json.loads(draft_ab._style_profile(addr) or "{}")
        return bool(prof.get("found"))
    except Exception:
        return False


# An ask for facts only the user has: what he plans, needs or prefers.
# A reply that "answers" it from the thread's own words repeats the
# sender's mail back to them (2026-10-13, a colleague's question about
# the use case for data she sent came back as her own summary).
_ASKS_FOR_FACTS = re.compile(
    r"\b(?:provide|share|send|give|clarify|describe|explain)\b[^.]{0,60}?\b(?:details?|specifics|use case|"
    r"what|how|which|why|your (?:plans?|thoughts|input|feedback|needs?|requirements?|priorities|goals?))\b|"
    r"\blet (?:me|us|her|him|them) know\s+(?:what|how|which|whether|if|why|your)\b|"
    r"\bwhat (?:do|will|would|are|is) (?:you|your)\b|\btell (?:me|us|her|him|them) (?:what|how|which|about)\b|"
    r"\b(?:your|the|his) (?:intended|specific|planned) use\b|\bwhat (?:he|you) (?:plans?|needs?|wants?|intends?)\b",
    re.I)
_ACK_ONLY = re.compile(r"^\s*(?:thanks|thank you|many thanks|great|got it|received|appreciate)[^.]*\.?\s*"
                       r"(?:(?:i|i'll|i will|we)[^.]*(?:follow up|get back|come back|keep|in touch|talk|discuss|meeting|invite)[^.]*\.?\s*)?$",
                       re.I)


def asks_for_facts(cand: dict) -> bool:
    """True when the sender asks the user for something only he knows,
    and the item is not a scheduling exchange (those have their own lane)."""
    if (cand.get("scheduling") or "none") not in ("", "none"):
        return False
    ask = cand.get("ask") or ""
    return bool(_ASKS_FOR_FACTS.search(ask))


def acknowledgement_brief(cand: dict, reply_must_say: str) -> str:
    """What the writer is told instead of the planner's answer: thanks
    for what they sent, and that the user will follow up on the
    question. The planner's own sentence stands only when it already
    says no more than that."""
    if _ACK_ONLY.match(reply_must_say or ""):
        return reply_must_say
    ask = _clean(cand.get("ask") or "", 160)
    return ("Thank them for what they sent and say the user will follow up on their question"
            + (f" ({ask})" if ask else "")
            + ". Do not answer the question, do not guess the user's plans, and do not restate their "
              "message back to them. Two short sentences at most.")


def rail(cand: dict, reply_must_say: str, *, wrote_before=_wrote_to_before) -> str:
    """'' or why this reply is a decision for the user, not a draft."""
    text = " ".join([cand.get("title") or "", cand.get("summary") or "", cand.get("ask") or ""])
    roles = " ".join(f"{p.get('role', '')} {p.get('name', '')}" for p in cand.get("people") or []
                     if isinstance(p, dict))
    if _ROLES.search(roles) or _ROLES.search(cand.get("who") or ""):
        return "a dean or a program officer"
    if _MONEY.search(text) or _MONEY.search(reply_must_say):
        return "money, a contract or a legal matter"
    if _DECLINE.search(reply_must_say):
        return "a decline"
    if cand.get("cc_only"):
        return "you are only copied"
    addr = (cand.get("from") or "").lower()
    rail = _institution.safety_rail_domain()
    inside = bool(rail) and (addr.endswith("@" + rail) or addr.endswith("." + rail))
    if addr and not inside and not wrote_before(addr):
        return _institution.safety_rail_phrase()
    return ""


# ── the actions ───────────────────────────────────────────────────────

def _run_json(argv: list[str], *, env: dict, timeout: int = 180) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=timeout,
                       env=env, stdin=subprocess.DEVNULL)
    out: dict = {}
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if not out:
        try:
            out = json.loads(p.stdout or "{}")
        except ValueError:
            out = {"error": (p.stderr or p.stdout or "")[-300:]}
    return p.returncode, out if isinstance(out, dict) else {}


def make_draft(cand: dict, reply_must_say: str, *, run_id: str, write=None, runner=None,
               note=None) -> dict:
    """One reply draft: the writer's body, the draft under this run's id,
    the ledger note, the send-measure record. Returns what happened."""
    import email_writer
    write = write or email_writer.write
    runner = runner or _run_json
    to = (cand.get("from") or "").lower()
    ask = cand.get("ask") or ""
    ask_line = f"Their ask: {ask}\n" if ask and ask not in (cand.get("summary") or "") else ""
    brief = (f"Recipient: {to} ({cand.get('who') or to}).\n"
             f"Thread: {cand.get('title')}. {cand.get('summary')}\n"
             f"{ask_line}The reply must say: {reply_must_say}")
    with tempfile.TemporaryDirectory(prefix="remote-briefing-") as td:
        body_path = str(Path(td) / "body.txt")
        answer, code = write(to, brief, body_path, conversation_id=cand.get("conversation_id") or "")
        if code != 0:
            return {"ok": False, "why": "the writer " + str(answer.get("reason") or "failed")[:120],
                    "need": answer.get("need", "")}
        env = dict(os.environ, EA_RUN_ID=run_id)
        # The row this reply answers: its Send closes exactly that row.
        link = ["--ledger-id", cand["ledger_id"]] if cand.get("ledger_id") else []
        rc, out = runner([str(HERE / "graph_actions.py"), "create-reply",
                          "--message-id", cand["message_id"],
                          "--conversation-id", cand.get("conversation_id") or "",
                          "--body", "x", "--body-file", body_path, *link], env=env)
    draft_id = out.get("id") or ""
    if rc != 0 or not draft_id:
        return {"ok": False, "why": "create-reply: " + str(out.get("error") or out.get("status")
                                                         or "failed")[:160]}
    if cand.get("ledger_id"):
        runner([str(HERE / "ledger.py"), "update", "--id", cand["ledger_id"], "--last-action",
                f"{datetime.now(_tz()).date().isoformat()}: reply drafted by the remote briefing, awaiting Send"],
               env=env)
    try:
        import draft_ab
        (note or draft_ab.note_writer_draft)(draft_id, run_id)
    except Exception:
        pass
    return {"ok": True, "draft_id": draft_id, "status": out.get("status", "")}


def _escalated_recently(conv: str, now: datetime) -> bool:
    for r in remote_llm.read(ESCALATED):
        if r.get("conversation_id") == conv:
            try:
                if now - datetime.fromisoformat(r["ts"]) < ESCALATE_AGAIN:
                    return True
            except (KeyError, ValueError):
                continue
    return False


def queue_escalations(items: list[dict], *, agent: str, now: datetime) -> int:
    """Merge the hand-offs into the bot's queue, one per thread, and record
    them. Returns how many were queued."""
    import atomic_io
    import resident_io
    fresh = [i for i in items if i.get("conversation_id")
             and not _escalated_recently(i["conversation_id"], now)]
    if not fresh:
        return 0
    try:
        existing = json.loads(resident_io.read_text(ESCALATIONS) or "[]")
        existing = existing if isinstance(existing, list) else []
    except Exception:
        existing = []
    merged: dict[str, dict] = {}
    for e in existing + fresh:
        try:
            if now - datetime.fromisoformat(e.get("queued_at", "")) > QUEUE_MAX_AGE:
                continue
        except ValueError:
            continue
        merged[e["conversation_id"]] = e
    atomic_io.write_json(ESCALATIONS, list(merged.values()), indent=None)
    for i in fresh:
        remote_llm.append(ESCALATED, {"ts": now.isoformat(timespec="seconds"),
                                      "conversation_id": i["conversation_id"], "agent": agent})
    return len(fresh)


def _escalation(cand: dict, pick: dict, now: datetime) -> dict:
    return {"conversation_id": cand.get("conversation_id") or "",
            "message_id": cand.get("message_id") or "", "ledger_id": cand.get("ledger_id") or "",
            "subject": _clean(cand.get("title"), 160), "from_name": _clean(cand.get("who"), 80),
            "why": _clean(pick.get("why"), 300), "task": _clean(pick.get("task"), 400),
            "brief": cand.get("brief") if isinstance(cand.get("brief"), dict) else None,
            "queued_at": now.isoformat(timespec="seconds")}


# ── the post ──────────────────────────────────────────────────────────

def _line_of(c: dict) -> str:
    who = f" ({_clean(c.get('who'), 60)})" if c.get("who") else ""
    return f"*{_clean(c.get('title'), 120)}*{who}"


def _soon_refs(cands: dict[str, dict], today: date, skip: set[str]) -> list[str]:
    out = []
    for r, c in cands.items():
        d = _date(c.get("due"))
        if d and r not in skip and (d < today or (d - today).days <= SOON_DAYS):
            out.append(r)
    return out[:6]


def shown_refs(cands: dict[str, dict], plan: dict, drafts: list[dict], escalated: list[dict],
               *, now: datetime, morning: bool) -> list[str]:
    """Every ref the post shows: the pre-send audit asks about each."""
    today = now.astimezone(_tz()).date()
    if morning:
        listed = [p["ref"] for k in ("get_done_today", "flexes", "needs_you")
                  for p in plan.get(k) or []]
        listed += [d["ref"] for d in drafts] + [e["ref"] for e in escalated]
        listed += _soon_refs(cands, today, set(listed))
    else:
        listed = [p["ref"] for k in ("since_morning", "in_motion", "pending")
                  for p in plan.get(k) or []]
        tomorrow = today + timedelta(days=1)
        listed += [r for r, c in cands.items() if _date(c.get("due")) == tomorrow]
    return [r for r in dict.fromkeys(listed) if r in cands]


def _looks_done(b: dict, audit: dict) -> list[str]:
    """The rows that look done and wait on the user's confirm card: the
    ledger's proposals, then the ones this run's pre-send audit added."""
    rows = [(p.get("id"), p.get("description"), p.get("why"))
            for p in b.get("close_proposals") or []]
    seen = {r[0] for r in rows}
    rows += [(d.get("id"), d.get("description"), d.get("why"))
             for d in audit.get("closed_rows") or [] if d.get("id") not in seen]
    return [_clean(desc or rid, 80) + (f" ({_clean(why, 70)})" if why else "")
            for rid, desc, why in rows if desc or rid]


def _words(text, cap: int) -> str:
    """_clean, cut at the last whole word that fits."""
    t = _clean(text, 10_000)
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def _waiting_line(b: dict, today: date) -> list[str]:
    """The rows whose thread ends on the user's own question, still inside
    ledger.WAITING_DAYS: off the do-list, named here once."""
    rows = []
    for o in b.get("open_obligations") or []:
        wait = _ledger.waiting_on_reply(o, today)
        if not wait:
            continue
        asked = f": {_words(wait['asked'], 110)}" if wait.get("asked") else ""
        try:   # the user's own day: an evening send is the next day in UTC
            when = datetime.fromisoformat(str(wait["since"]).replace("Z", "+00:00")).astimezone(_tz())
        except ValueError:
            when = None
        day = f" {when:%b} {when.day}" if when else ""
        rows.append(f"{_words(o.get('description') or o.get('id'), 100)} (you asked{day}{asked})")
    if not rows:
        return []
    return ["", f":hourglass: *Waiting on replies* ({len(rows)}): " + "; ".join(rows[:6]) + "."]


def _looks_done_line(b: dict, audit: dict) -> list[str]:
    looks = _looks_done(b, audit)
    if not looks:
        return []
    return ["", f":grey_question: *Looks done?* ({len(looks)}) Close or keep each on the "
                f"card in your DM: " + "; ".join(looks[:8]) + "."]


def _morning_lists(cands: dict[str, dict], plan: dict, drafts: list[dict],
                   escalated: list[dict], audit: dict, today: date) -> dict:
    """The morning post's sections as {key: [(ref, line)]}, each item
    named once: the do-list wins, and the date banner ("due") names only
    what no other section shows. A line is the item and its detail, with
    no section marker; the EA post and the room slices both use them."""
    gone = set(audit.get("closed") or []) | set(audit.get("parked") or [])
    unsure = {u["id"]: u.get("why", "") for u in audit.get("unsure_rows") or []}
    skip = {r for r, c in cands.items() if c.get("ledger_id") in gone}

    def mark(r: str) -> str:
        why = unsure.get(cands[r].get("ledger_id") or "")
        return f" Evidence: {_clean(why, 120)}; still open?" if why else ""

    top = [p for p in plan.get("get_done_today") or [] if p["ref"] not in skip]
    on_list = {p["ref"] for p in top}
    flex = [p for p in plan.get("flexes") or [] if p["ref"] not in skip | on_list]
    needs = [p for p in plan.get("needs_you") or [] if p["ref"] not in skip]
    needs += [{"ref": d["ref"], "decision": d["decision"]} for d in drafts if not d.get("ok")]
    needs = [p for p in needs if p["ref"] not in on_list]
    escalated = [e for e in escalated if e["ref"] not in on_list]
    elsewhere = on_list | {p["ref"] for p in flex} | {p["ref"] for p in needs} \
        | {e["ref"] for e in escalated}
    due = []
    for r in _soon_refs(cands, today, skip | elsewhere):
        d = _date(cands[r].get("due"))
        due.append((r, f"{_line_of(cands[r])}, " + (f"was due {d:%a %b} {d.day}" if d < today
                                                     else f"{d:%a %b} {d.day}")))
    return {
        "due": due,
        "top": [(p["ref"], _line_of(cands[p["ref"]])
                 + (f": {_clean(p.get('when'), 60)}" if p.get("when") else "")
                 + (f". {_clean(p.get('why'), 140)}" if p.get("why") else "") + mark(p["ref"]))
                for p in top],
        "flex": [(p["ref"], f"{_line_of(cands[p['ref']])}: {_clean(p.get('line'), 140)}{mark(p['ref'])}")
                 for p in flex],
        "draft": [(d["ref"], f"{_line_of(cands[d['ref']])}: "
                   + (f"thanks only; they ask: {_clean(cands[d['ref']].get('ask'), 140)}" if d.get("ack")
                      else _clean(d.get("says"), 140)))
                  for d in drafts if d.get("ok")],
        "needs": [(p["ref"], f"{_line_of(cands[p['ref']])}: {_clean(p.get('decision'), 160)}")
                  for p in needs],
        "escalated": [(e["ref"], f"{_line_of(cands[e['ref']])}: {_clean(e.get('why'), 140)}")
                      for e in escalated],
    }


def _afternoon_lists(cands: dict[str, dict], plan: dict, audit: dict) -> dict:
    gone = set(audit.get("closed") or []) | set(audit.get("parked") or [])
    skip = {r for r, c in cands.items() if c.get("ledger_id") in gone}
    return {k: [(p["ref"], f"{_line_of(cands[p['ref']])}: {_clean(p.get('line'), 140)}")
                for p in plan.get(key) or [] if p["ref"] not in skip]
            for k, key in (("since", "since_morning"), ("pending", "pending"))}


def room_entries(cands: dict[str, dict], lists: dict) -> list[dict]:
    """The post's items as briefing_rooms entries."""
    out = []
    for sec, items in lists.items():
        for ref, line in items:
            c = cands[ref]
            out.append({"ref": ref, "section": sec, "line": line,
                        "ledger_id": c.get("ledger_id") or "",
                        "conversation_id": c.get("conversation_id") or "",
                        "title": c.get("title") or "", "summary": c.get("summary") or ""})
    return out


def render_morning(b: dict, cands: dict[str, dict], plan: dict, *, now: datetime,
                   drafts: list[dict], escalated: list[dict], audit: dict | None = None,
                   stale: bool = False, fallback: str = "") -> str:
    audit = audit or {}
    local = now.astimezone(_tz())
    today = local.date()
    lists = _morning_lists(cands, plan, drafts, escalated, audit, today)

    out = [f":sunny: *Morning Briefing, {local:%a %b} {local.day}*"]
    if stale:
        out.append(":warning: The mail sync failed this run; this briefing uses older data.")
    for err in b.get("calendar_fetch_errors") or []:
        acct = err.get("account") if isinstance(err, dict) else err
        out.append(f":warning: Calendar source {_clean(acct, 60)} failed to load; free time below may be wrong.")
    if fallback:
        out.append(f":warning: {fallback}")
    for r, line in lists["due"]:
        overdue = _date(cands[r].get("due")) < today
        out.append((":red_circle: OVERDUE: " if overdue else ":alarm_clock: DUE SOON: ") + line)
    out.append("")
    out.append(":dart: *Get done today* (in this order, fitted to your gaps)")
    if lists["top"]:
        out += [f"{i}. {line}" for i, (_r, line) in enumerate(lists["top"], 1)]
    else:
        out.append("Nothing is pressing today.")
    if lists["flex"]:
        out += ["", ":leaves: *Flexes* (fine today, fine tomorrow)"]
        out += [f"- {line}" for _r, line in lists["flex"]]
    cal = _calendar(b, today)
    if cal:
        out += ["", ":clipboard: " + " · ".join(cal)]
    handled = []
    n_reply = len(b.get("handled_by_reply") or [])
    if n_reply:
        handled.append(f"{n_reply} answered by your replies")
    ok = lists["draft"]
    if ok:
        handled.append(f"{len(ok)} draft(s) staged")
    if handled:
        out += ["", ":white_check_mark: *Quietly handled*: " + "; ".join(handled) + "."]
    out += _waiting_line(b, today)
    out += _looks_done_line(b, audit)
    for m in b.get("auto_merged") or []:
        out.append(f":link: auto-merged {m.get('count', '')} emails about "
                   f"{_clean(m.get('entity'), 60)}; say *unmerge {m.get('id', '')}* if wrong")
    if ok:
        out += ["", f":envelope: *Drafts ready* ({len(ok)}): Send, Edit or Handled on each "
                    "block in your DM; one Send-all card when several are ready"]
        out += [f"- {line}" for _r, line in ok]
    if lists["needs"]:
        out += ["", f":question: *Needs you* ({len(lists['needs'])})"]
        out += [f"- {line}" for _r, line in lists["needs"]]
    if lists["escalated"]:
        out += ["", f":arrow_up: *Sent to Claude* ({len(lists['escalated'])}): one topic each in the EA channel"]
        out += [f"- {line}" for _r, line in lists["escalated"]]
    tail = []
    n_sched = len(b.get("new_scheduling_requests") or [])
    if n_sched and b.get("scheduling_channel_active"):
        tail.append(f":calendar: {n_sched} scheduling request(s): handling in the scheduling channel.")
    for f in b.get("resurrected_follow_ups") or []:
        tail.append(f":alarm_clock: Follow-up due: {_clean(f.get('description'), 120)}")
    for d in b.get("woken_snoozed") or []:
        tail.append(f":email: Snoozed email returned: {_clean(d, 120)}")
    for s in b.get("scheduled_sends_due_today") or []:
        tail.append(f":alarm_clock: *Scheduled send today:* {_clean(s.get('description'), 120)}")
    if tail:
        out += [""] + tail
    ds = b.get("delta_summary") or {}
    by = ds.get("obligations_by_list")
    snoozed = f" · Snoozed: {by['snoozed']}" if by and by.get("snoozed") else ""
    counts = (f"Open: {by.get('open', 0)} · Follow-up: {by.get('follow_up', 0)}{snoozed}"
              f" · Waiting on others: {by.get('waiting', 0)}") if by else \
        f"Open obligations: {ds.get('open_obligations', len(b.get('open_obligations') or []))}"
    out += ["", f"{counts} | Inbox: {ds.get('total_inbox', 0)} messages | _written by the remote model_"]
    return "\n".join(out)


def render_afternoon(b: dict, cands: dict[str, dict], plan: dict, *, now: datetime,
                     audit: dict | None = None, stale: bool = False, fallback: str = "") -> str:
    audit = audit or {}
    gone = set(audit.get("closed") or []) | set(audit.get("parked") or [])
    skip = {r for r, c in cands.items() if c.get("ledger_id") in gone}
    local = now.astimezone(_tz())
    out = [f":coffee: *Afternoon Update, {local:%a %b} {local.day}*"]
    if stale:
        out.append(":warning: The mail sync failed this run; this update uses older data.")
    if fallback:
        out.append(f":warning: {fallback}")
    since = [p for p in plan.get("since_morning") or [] if p["ref"] not in skip]
    out += ["", "*Since this morning:*"]
    out += ([f"- {_line_of(cands[p['ref']])}: {_clean(p.get('line'), 140)}" for p in since]
            or ["- Nothing new that needs you."])
    motion = [p for p in plan.get("in_motion") or [] if p["ref"] not in skip]
    if motion:
        out += ["", "*Already in motion* (checked against your sent mail and the channel):"]
        out += [f"- {_line_of(cands[p['ref']])}: {in_motion(cands[p['ref']])}" for p in motion]
    rest = []
    for e in b.get("todays_events") or []:
        start, _end = _event_times(e)
        if start and not e.get("isAllDay") and start >= now:
            rest.append(f"- {sched_render.clock(start)} {_clean(e.get('subject'), 80)}")
    if rest:
        out += ["", "*Remaining today:*"] + rest[:8]
    tom = []
    for e in b.get("tomorrows_events") or []:
        start, _end = _event_times(e)
        when = "all day" if e.get("isAllDay") else (sched_render.clock(start) if start else "")
        tom.append(f"- {when} {_clean(e.get('subject'), 80)}")
    tomorrow = local.date() + timedelta(days=1)
    tom += [f"- Due tomorrow: {_line_of(c)}" for r, c in cands.items()
            if _date(c.get("due")) == tomorrow and r not in skip]
    if tom:
        out += ["", "*Tomorrow preview:*"] + tom[:10]
    pend = [p for p in plan.get("pending") or [] if p["ref"] not in skip]
    if pend:
        out += ["", "*Pending your response:*"]
        out += [f"- {_line_of(cands[p['ref']])}: {_clean(p.get('line'), 140)}" for p in pend]
    out += _waiting_line(b, local.date())
    out += _looks_done_line(b, audit)
    out += ["", "_written by the remote model_"]
    return "\n".join(out)


def fallback_plan(b: dict, cands: dict[str, dict], *, morning: bool) -> dict:
    """The ranked queue from code, when the remote model cannot answer."""
    top = top_refs(b, cands)
    if not morning:
        return {"since_morning": [{"ref": r, "line": "new since the morning"}
                                  for r in cands if r[0] in "RDV"][:4], "pending": []}
    return {"get_done_today": [{"ref": t["ref"], "when": "", "why": t["reason"]} for t in top[:MAX_TOP]],
            "flexes": [], "drafts": [], "needs_you": [], "escalate": []}


# ── the pre-send audit ────────────────────────────────────────────────

AUDIT_SYSTEM = ("You check a professor's assistant's briefing. For each item, decide from the "
                "evidence whether it is still open. Answer exactly in the format the request asks.")
AUDIT_MODEL = "opus"


def audit_judge(cfg: dict | None = None) -> str:
    """Who gives the verdicts: "claude" unless remote-llm.json says
    `"briefing_audit": "remote"`. Whether an item is still open is a
    judgment, so it stays with Claude; kept mail never reaches the prompt
    (open_claims_audit.without_kept). The check is the routing table's
    `review` task, which the stakes floor holds at hosted."""
    cfg = cfg if cfg is not None else remote_llm.config()
    return "remote" if remote_llm.local_for("review", cfg.get("briefing_audit") == "remote", cfg) \
        else "claude"


def _claude_verdicts(prompt: str) -> str:
    """One tool-less Claude run as the resident; its answer text."""
    import ea_config
    import mail_privacy
    model = ea_config.get_models().get("briefing_audit") or AUDIT_MODEL
    r = mail_privacy._run_claude(f"{AUDIT_SYSTEM}\n\n{prompt}", model, "")
    if r.get("rc") or not r.get("text"):
        raise RuntimeError(f"claude rc={r.get('rc')}: {(r.get('stderr') or '')[-160:]}")
    return str(r["text"])


def _remote_verdicts(prompt: str, chat=None) -> str:
    r = (chat or remote_llm.chat)([{"role": "system", "content": AUDIT_SYSTEM},
                                   {"role": "user", "content": prompt}],
                                  purpose="briefing-audit", max_tokens=2000)
    return r.get("content") or ""


def shown_rows(cands: dict[str, dict], refs) -> list[str]:
    """The ledger ids behind the refs a post shows, in order."""
    return list(dict.fromkeys(cands[r]["ledger_id"] for r in refs
                              if r in cands and cands[r].get("ledger_id")))


def audit_post(agent: str, text: str, b: dict, *, dry_run: bool, chat=None,
               rendered: list[str] | None = None, judge=None) -> dict:
    """open_claims_audit on the post: every row in `rendered` (the rows
    the post shows) and every other row its words name with evidence.
    Claude gives the verdicts (audit_judge); the remote model answers
    when Claude cannot. Returns {closed, closed_rows, unsure_rows,
    parked, asked, judge}; {} on any failure, which leaves the post as
    it is."""
    try:
        import open_claims_audit as oca
        who = audit_judge()
        rec = oca.prepare(agent=agent, post=text, briefing=b, rendered=rendered,
                          claude_safe=(who == "claude"))
        out = {"asked": rec.get("asked", 0), "parked": [p["id"] for p in rec.get("parked") or []],
               "closed": [], "closed_rows": [], "unsure_rows": [], "judge": who}
        if not rec.get("asked") or dry_run:
            return out
        answer = ""
        if who == "claude":
            try:
                answer = (judge or _claude_verdicts)(rec["prompt"])
            except Exception as e:
                print(f"remote_briefing: Claude audit failed, the remote model answers: "
                      f"{str(e)[:160]}", file=sys.stderr)
                out["judge"] = "remote (Claude failed)"
        if not answer:
            answer = _remote_verdicts(rec["prompt"], chat)
        verdicts = oca.parse_verdicts(answer)
        summary = oca.apply(audit=rec, verdicts=verdicts)
        why = {v["id"]: v.get("why", "") for v in verdicts}
        out["closed"] = summary.get("closed") or []
        desc = {c["id"]: c.get("description") or "" for c in rec.get("claims") or []}
        out["closed_rows"] = [{"id": i, "why": why.get(i, ""), "description": desc.get(i, "")}
                              for i in out["closed"]]
        out["unsure_rows"] = [{"id": i, "why": why.get(i, "")} for i in summary.get("unsure") or []]
        return out
    except Exception as e:
        print(f"remote_briefing: audit skipped: {str(e)[:200]}", file=sys.stderr)
        return {}


# ── the run ───────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    import resident_io
    text = resident_io.read_text(path)
    if text is None:
        raise OSError(f"cannot read {path}")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


PREVIEW_KEY = "briefing-preview"
PREVIEW_TOPIC = "Briefing preview"


def _preview(text: str) -> bool:
    """Post a dry run's briefing where the digests go
    (chat_notify.post_digest): on Zulip the mail-digest stream, topic
    "Briefing preview"; on the web chat a thread of its own in the digest
    channel; on Slack the `mail-digest` webhook. The briefing's chat
    reader reads none of them, so a preview never reads as a briefing that
    went out. False when there is no such place or the post failed."""
    import chat_notify
    return bool(chat_notify.post_digest(PREVIEW_KEY, text, topic=PREVIEW_TOPIC))


def room_slices(agent: str, cands: dict[str, dict], plan: dict, drafts: list[dict],
                escalated: list[dict], audit: dict, briefing: dict, *, now: datetime,
                dry_run: bool, poster=None, conn=None, targets=None, open_rows=None,
                matcher=None) -> list[dict]:
    """Post each project room its slice of this post (briefing_rooms);
    returns the posts, each with `ok` once sent. A dry run plans them and
    sends nothing. Off with `"briefing_rooms": false` in remote-llm.json."""
    import briefing_rooms
    if not briefing_rooms.enabled():
        return []
    morning = agent == "morning-briefing"
    local = now.astimezone(_tz())
    lists = (_morning_lists(cands, plan, drafts, escalated, audit, local.date()) if morning
             else _afternoon_lists(cands, plan, audit))
    entries = room_entries(cands, lists)
    if not entries:
        return []
    if conn is None:
        import project_store
        conn = project_store._get_db()
    targets = targets if targets is not None else briefing_rooms.rooms()
    if not targets:
        return []
    heading = f"{'Morning briefing' if morning else 'Afternoon update'}, {local:%a %b} {local.day}"
    gone = set(audit.get("closed") or []) | set(audit.get("parked") or [])
    if open_rows is None:
        try:   # the whole ledger: the briefing JSON carries only a slice of it
            open_rows = _ledger.get_open_entries()
        except Exception:
            open_rows = briefing.get("open_obligations") or []
    open_rows = [o for o in open_rows if _live_row(o, local.date()) and o.get("id") not in gone]
    posts = briefing_rooms.plan_posts(entries, heading=heading, open_rows=open_rows, conn=conn,
                                      targets=targets, clean=_clean, matcher=matcher)
    if not dry_run:
        for p, r in zip(posts, briefing_rooms.send(posts, poster=poster)):
            p["ok"] = r["ok"]
    return posts


def run(agent: str, briefing: dict, *, run_id: str, stale: bool = False, dry_run: bool = False,
        now: datetime | None = None, chat=None, audit=None, drafter=None,
        queue=None, rooms=None) -> tuple[str, dict]:
    """(the post, the run record)."""
    now = now or _now()
    morning = agent == "morning-briefing"
    t0 = time.monotonic()
    cands = candidates(briefing, today=now.astimezone(_tz()).date())
    rec: dict = {"ts": now.isoformat(timespec="seconds"), "agent": agent, "run_id": run_id,
                 "dry_run": dry_run, "candidates": len(cands)}
    fallback = ""
    try:
        plan = ask_plan(agent, briefing, cands, rules=_rules(), now=now, chat=chat)
    except remote_llm.RemoteLLMError as e:
        plan = fallback_plan(briefing, cands, morning=morning)
        fallback = ("The remote model did not answer; this is the ranked queue from code."
                    + (" No drafts were made." if morning else ""))
        rec["fallback"] = str(e)[:200]
    drafts: list[dict] = []
    escalated: list[dict] = []
    if morning:
        os.environ["EA_RUN_ID"] = run_id
        for p in plan.get("drafts") or []:
            c = cands[p["ref"]]
            why = rail(c, p.get("reply_must_say") or "")
            if why:
                drafts.append({"ref": p["ref"], "ok": False,
                               "decision": f"Reply needs you ({why}): {_clean(p.get('reply_must_say'), 120)}"})
                continue
            says, ack = p.get("reply_must_say") or "", False
            if asks_for_facts(c):
                # the question is his to answer: the draft thanks them and
                # says he will follow up, and the post says what they asked
                says, ack = acknowledgement_brief(c, says), True
            if dry_run:
                drafts.append({"ref": p["ref"], "ok": True, "says": says, "ack": ack,
                               "dry_run": True})
                continue
            res = (drafter or make_draft)(c, says, run_id=run_id)
            if res.get("ok"):
                drafts.append({"ref": p["ref"], "ok": True, "says": says, "ack": ack,
                               "draft_id": res.get("draft_id")})
            else:
                decision = (f"The draft could not be made ({_clean(res.get('why'), 80)}). "
                            + (f"Question: {_clean(res.get('need'), 120)}" if res.get("need")
                               else _clean(p.get("reply_must_say"), 120)))
                drafts.append({"ref": p["ref"], "ok": False, "decision": decision})
        handoffs = [(p, cands[p["ref"]]) for p in plan.get("escalate") or []
                    if cands[p["ref"]].get("conversation_id")]
        # An item with no thread cannot be handed off; it is the user's call.
        plan["needs_you"] = list(plan.get("needs_you") or []) + [
            {"ref": p["ref"], "decision": f"Needs more than a reply: {_clean(p.get('task') or p.get('why'), 140)}"}
            for p in plan.get("escalate") or [] if not cands[p["ref"]].get("conversation_id")]
        if handoffs and not dry_run:
            (queue or queue_escalations)([_escalation(c, p, now) for p, c in handoffs],
                                         agent=agent, now=now)
        escalated = [{"ref": p["ref"], "why": p.get("why")} for p, _c in handoffs]
    render = (lambda a: render_morning(briefing, cands, plan, now=now, drafts=drafts,
                                       escalated=escalated, audit=a, stale=stale,
                                       fallback=fallback)) if morning else \
             (lambda a: render_afternoon(briefing, cands, plan, now=now, audit=a, stale=stale,
                                         fallback=fallback))
    text = render(None)
    shown = shown_refs(cands, plan, drafts, escalated, now=now, morning=morning)
    checked = (audit or audit_post)(agent, text, briefing, dry_run=dry_run, chat=None,
                                    rendered=shown_rows(cands, shown))
    if checked and (checked.get("closed") or checked.get("parked") or checked.get("unsure_rows")):
        text = render(checked)
    try:
        posts = (rooms or room_slices)(agent, cands, plan, drafts, escalated, checked or {},
                                       briefing, now=now, dry_run=dry_run)
    except Exception as e:
        print(f"remote_briefing: project-room slices skipped: {str(e)[:200]}", file=sys.stderr)
        posts = []
    if posts:
        import briefing_rooms
        sent = [p for p in posts if p.get("ok", dry_run)]
        try:
            text = "\n".join([text] + briefing_rooms.pointer_line(sent))
        except Exception as e:
            print(f"remote_briefing: room pointer line skipped: {str(e)[:160]}", file=sys.stderr)
        rec["rooms"] = [{"room": p["room"], "items": len(p["refs"]), "ok": p.get("ok")}
                        for p in posts]
    rec.update(plan={k: len(v) for k, v in plan.items()},
               drafts=sum(1 for d in drafts if d.get("ok")),
               decisions_from_rails=sum(1 for d in drafts if not d.get("ok")),
               escalated=len(escalated), audit={k: (len(v) if isinstance(v, list) else v)
                                                for k, v in (checked or {}).items()},
               seconds=round(time.monotonic() - t0, 1), post=text)
    return text, rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="write the briefing on the remote model")
    r.add_argument("--agent", required=True, choices=AGENTS)
    r.add_argument("--briefing", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--stale", action="store_true", help="the preprocessor failed this run")
    r.add_argument("--dry-run", action="store_true", help="no drafts, queue or closures; counts only")
    r.add_argument("--preview", action="store_true",
                   help="with --dry-run, post where the mail digests go")
    a = ap.parse_args(argv)
    if not active():
        return EXIT_OFF
    try:
        briefing = _load(a.briefing)
    except Exception as e:
        print(f"remote_briefing: {e}", file=sys.stderr)
        return 1
    text, rec = run(a.agent, briefing, run_id=a.run_id, stale=a.stale, dry_run=a.dry_run)
    remote_llm.append(RUNS, rec)
    if a.dry_run:
        if a.preview:
            rec["preview_posted"] = _preview(text)
        print(json.dumps({k: v for k, v in rec.items() if k != "post"}))
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
