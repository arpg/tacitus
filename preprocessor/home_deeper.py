#!/usr/bin/env python3
"""The deeper briefing on a Home item (docs/webchat.md, Home, "The deeper
briefing"): what happened, what is at stake, what Tacitus does not know,
and two or three next steps the owner can tap.

Each open ledger row's briefing is written ahead of the tap and stored
(Store, an owner-private file, never the ledger): by the owner's own model
when the row or its thread changes (refresh, from the mail tick; backfill),
and by the hosted model for the items on Home's list after each briefing
run (hosted_pass, in a process of its own; kept mail left out). One model
call, with no tools, reads a packet code builds and answers under a strict
shape; code checks the answer before it is stored. A tap shows the stored
briefing at once, with the lines that depend on the moment added in code
(now_lines); only a missing or stale one (row_version) is written at the
tap (`POST /api/home/deeper`).

The packet (build_packet), read-only:

  the item    its kind, its title and the quick briefing's lines
  the row     type, status, dates, description, last action, notes, sender
  history     the row's last events (ledger_events), with the owner's quotes
  the thread  the thread brief's records (subject, ask, state, the last
              messages' gists; never mail text, never a quote from mail)
  same thread the other rows on the conversation, closed ones too, with the
              owner's own close in his words
  his word    the owner's earlier decision on the thread (owner_said)
  project     the project the row is linked to: its name, summary, next
  calendar    the events ahead and the free slots up to the due date (the
              briefing's data), for a step's date alone
  rules       the owner's rules for briefings and drafting (preferences)
  steps       the step kinds this item allows (allowed_kinds)

Where it runs (route). An item kept from hosted models below Summary
(keep_gate) runs on the owner's own model alone; with none set up the card
shows the quick briefing alone. Any other item runs on the owner's own
model, and on the hosted model (hosted_text: Claude or Codex) when it does
not answer or there is none, and the card says so in one line; a hosted
plan at its limit makes the card say the limit is reached. The day's list
is the hosted model's at briefing time (hosted_pass, the `review` model).

What code checks in the answer (check_answer):

  - the shape: three short lists and at most three steps;
  - every sentence and every step's words: a number, a date word or a
    name the packet does not hold makes the sentence "Tacitus could not
    check this.", and drops the step;
  - each step: its kind is one this item allows (allowed_kinds: never a
    draft that answers a choice that is the owner's, accepts or declines
    an invitation, or goes on a thread the owner said gets no reply; no
    draft, question or conversation on a kept item; a date after today),
    and at the owner's tap, one his item still offers (step).

No step sends anything, creates an event with people on it, or closes a
row by itself: a draft is a draft with its Send in Action requests, a hold
has no attendees, and Done, Waiting and Later go through home_items' own
verbs as if the owner had tapped those choices.

The cost. One job per item at a time, at most MAX_RUNNING at once (more
wait, up to MAX_WAITING), at most daily_cap() model calls a day (the card
says so in plain words when the day's calls are spent), and a prewarm of
the first PREWARM_TOP items of "Left for you" only where the call runs on
the owner's own model, never on a hosted plan. Each job is one line in
<private_dir>/remote-llm/home-deeper.jsonl (no content: the route, the
model, the time, the tokens, what code refused); `usage` prints the day.

The hook (docs/webchat.md, Home): home_items calls state(), start() and
step(); Home.tick calls prewarm(); mail_privacy.tick calls refresh();
ledger.py notes a row it adds or changes (home_briefing_queue);
remote_briefing's run starts the hosted pass (spawn_hosted_pass). A tap's
job state is kept in the web chat's own store (record `deeper`), and the
`home-item` event tells an app that shows the item open to read it again.

    home_deeper.py usage [--days N]     today's and recent calls, by route
    home_deeper.py packet --ref <ref>   the packet for one item, read-only
    home_deeper.py backfill [--limit N] [--dry-run]
                                        write the missing or stale briefings of the
                                        open rows, N at most (20); run it again to go
                                        on (each row takes about 4 s on the owner's
                                        own model); --dry-run counts them
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

log = logging.getLogger("chat.web.home.deeper")

STORE_KEY = "deeper"
LOG_NAME = "home-deeper.jsonl"
PURPOSE = "chat-home-deeper"
TASK = "briefing"
# The Claude model when ea-config.json's `models` names none for
# `home_deeper`: the plan's cheaper model; Codex runs on the account's own.
HOSTED_MODEL = "sonnet"
LOCAL_TIMEOUT_S = 45
HOSTED_TIMEOUT_S = 120
LOCAL_MAX_TOKENS = 900
# The bounds of one call: the packet's size (about 4 characters a token).
PROMPT_MAX_CHARS = 24_000
# The cost bounds.
DAILY_CAP = 120
MAX_RUNNING = 2
MAX_WAITING = 4
PREWARM_TOP = 2
PREWARM_DAILY = 30
JOBS_KEPT = 80
# How far ahead a date in a step may be.
STEP_DAYS = 60

STEP_KINDS = ("draft_reply", "ask_sender", "propose_times", "wait_until", "mark_done",
              "work_on_it", "hold_slot")
# The steps that run as one of the item's own choices (home_items), by the
# choice ids it may offer, first found first: a reply's words go through
# Tell Tacitus what to say (Draft a reply with words reads as it), a date
# through Waiting on someone, else Later; Done, else Not needed.
CHOICE_OF = {"draft_reply": ("say", "draft"), "ask_sender": ("say", "draft"),
             "propose_times": ("say", "draft"), "wait_until": ("waiting", "later"),
             "mark_done": ("done", "not_needed")}
# The steps this module runs itself.
OWN_STEPS = ("work_on_it", "hold_slot")
CONTINUE = "continue"
CONTINUE_LABEL = "Continue in a conversation"

UNCHECKED = "Tacitus could not check this."
SECTIONS = (("history", "What happened", 4), ("stakes", "What is at stake", 3),
            ("unknown", "What Tacitus does not know", 3))
LINE_MAX = 240
SAY_MAX = 160
LABEL_MAX = 60

# What the owner reads, in plain words.
NOTE_KEPT_NO_LOCAL = "This one stays on your own model, which is not set up. Open it in Outlook."
NOTE_CAP = "You reached today's limit of {n} deeper briefings. They start again tomorrow."
NOTE_BUSY = "Tacitus is busy with other briefings. Open this again in a minute."
NOTE_FAILED = "Tacitus could not write this now. Open it again in a minute."
NOTE_LOCAL_FAILED = "Your own model did not answer. Open this again in a minute."
NOTE_LIMIT = "{hosted} is at its plan limit. Open this again after it resets."
NOTE_LIMIT_LOCAL = "{hosted} is at its plan limit, so your own model wrote this."
NOTE_FELL_BACK = "Your own model did not answer, so {hosted} wrote this."
NOTE_UNCHECKED = "Tacitus could not check what the model wrote. Open this again in a minute."
NOTE_STOPPED = "Tacitus stopped before it finished. Open this again to try once more."
NOTE_STALE_STEP = "That step is out of date. Open the item again."
NOTE_STEP_GONE = "That step is not open for this item now."
LOCAL_NAME = "Your own model"

_JSON = re.compile(r"\{.*\}", re.S)


def _mod(name: str):
    """A sibling module, imported as itself or under `preprocessor.`."""
    import importlib
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return importlib.import_module(f"preprocessor.{name}")


# ── small helpers ────────────────────────────────────────────────────

def _one(text: Any, cap: int) -> str:
    """Text on one line, no em-dash or en-dash, at most `cap` characters,
    cut on a word."""
    t = " ".join(str(text or "").split()).replace("\u2014", ", ").replace("\u2013", "-")
    if len(t) <= cap:
        return t
    cut = t[:cap - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "\u2026"


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def day_words(d: date) -> str:
    """"Tuesday, November 24": how the card and the packet say a day."""
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def _with_words(value: Any) -> str:
    """An ISO date (or time) with its day in words beside it, so a model
    that names the weekday or the month names one the packet holds."""
    raw = str(value or "")
    d = _date(raw)
    if not d:
        return raw
    return f"{raw[:16].replace('T', ' ')} ({day_words(d)})"


def _short_day(d: date) -> str:
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"


def _meta(row: dict) -> dict:
    raw = (row or {}).get("metadata")
    if isinstance(raw, dict):
        return raw
    try:
        m = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _brief(row: dict) -> dict:
    for b in ((row or {}).get("thread_brief"), _meta(row).get("thread_brief")):
        if isinstance(b, dict) and b:
            return b
    return {}


def _zone() -> ZoneInfo:
    try:
        return _mod("user_tz").zone()
    except Exception:
        return ZoneInfo("UTC")


def kept(level: Any) -> bool:
    return level in ("local", "facts")


def item_kept(item: dict | None) -> bool:
    """The item is kept below Summary: Home wrote its level (`kept` or
    `keep_level`), or a bare `kept: true`."""
    item = item or {}
    return item.get("kept") is True or kept(item.get("kept")) or kept(item.get("keep_level"))


# ── the item ─────────────────────────────────────────────────────────

def ref_ids(ref: str) -> tuple[str, str]:
    """(the ledger id, the conversation id) a Home ref names, as far as the
    ref alone says: `reply:<id>` a row; `item:<x>` a row or a thread;
    `request:<id>` neither (a card)."""
    kind, _, rest = str(ref or "").partition(":")
    if kind in ("reply", "closed") and rest:
        return rest, ""
    if kind == "item" and rest:
        return rest, rest
    return "", ""


def read_row(ref: str, *, conn=None) -> dict | None:
    """The ledger row behind a Home ref, read-only, in the shape Home reads
    rows (ledger.get_open_entries: metadata merged, the owner's word on the
    thread beside it), so its version is the one Home's row has; None for a
    card or a ref that names no row."""
    lid, conv = ref_ids(ref)
    if not lid and not conv:
        return None
    br = _mod("briefing_review")
    own = conn is None
    try:
        conn = conn or br._connect()
    except Exception as e:
        log.warning("home deeper: the ledger could not be opened: %s", e)
        return None
    factory, conn.row_factory = conn.row_factory, br._dict_row
    try:
        row = conn.execute("SELECT * FROM ledger WHERE id = ?", (lid,)).fetchone() if lid else None
        if row is None and conv:
            row = conn.execute("SELECT * FROM ledger WHERE conversation_id = ? ORDER BY "
                               "CASE WHEN status IN ('completed', 'closed') THEN 1 ELSE 0 END, "
                               "updated_at DESC LIMIT 1", (conv,)).fetchone()
    finally:
        conn.row_factory = factory
        if own:
            conn.close()
    if not row:
        return None
    out = {k: v for k, v in dict(row).items() if k != "metadata" and v is not None}
    out.update(_meta(row))
    try:
        _mod("ledger").with_owner_decisions([out])
    except Exception:
        pass
    return out


def signature(ref: str, item: dict | None, row: dict | None) -> str:
    """What makes a job's result stale: the item's ref and keep mark, and
    the row and thread's version (row_version) or, for an item with no row
    (a card), its title. The quick briefing's day counts are not in it, so
    a result outlives midnight."""
    key = [ref, item_kept(item), row_version(row) if row else str((item or {}).get("title") or "")]
    return hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()[:16]


# ── which steps an item allows ───────────────────────────────────────

CLOSED = ("completed", "closed")


def candidate(row: dict | None, item: dict | None) -> dict:
    """The row as draft_gate.check reads a candidate: title, ask, state,
    scheduling, brief, owner_said, kind, keep_level."""
    row = row or {}
    brief = _brief(row)
    return {"title": brief.get("subject") or row.get("description") or (item or {}).get("title") or "",
            "ask": brief.get("ask") or "", "state": brief.get("state") or "",
            "scheduling": brief.get("scheduling") or "none", "brief": brief or None,
            "owner_said": row.get("owner_said") or _meta(row).get("owner_said") or [],
            "kind": (item or {}).get("kind") or "", "response_mode": brief.get("response_mode"),
            "keep_level": (item or {}).get("kept") or ""}


def allowed_kinds(item: dict, row: dict | None, level: str) -> list[str]:
    """The step kinds this item allows, before the owner's own choices:

    - kept below Summary: Wait and Done alone (no draft, no question, no
      conversation: hosted turns read those);
    - a card in Action requests: start work on it in a conversation;
    - a closed row: start work on it;
    - a reply, a question or a draft only on a mail thread, never on one
      the owner said gets no reply; a draft (and offered times) never
      where the answer is the owner's choice (draft_gate.owner_choice) or
      the ask is work, not a reply (mail_completion.non_email_work);
      offered times only on a meeting thread (draft_gate.meeting_row);
    - an answer needed (a decision): no draft at all, the answer is his."""
    kind = str((item or {}).get("kind") or "")
    if kept(level) or item_kept(item):
        return ["wait_until", "mark_done"]
    if kind == "request":
        return ["work_on_it"]
    row = row or {}
    if row and str(row.get("status") or "") in CLOSED:
        return ["work_on_it"]
    out = ["wait_until", "mark_done", "work_on_it", "hold_slot"]
    conv = row.get("conversation_id") or _brief(row).get("conversation_id")
    if not conv:
        return out
    dg = _mod("draft_gate")
    cand = candidate(row, item)
    try:
        no_reply = bool(dg.no_reply_said(cand["owner_said"]))
    except Exception:
        no_reply = True
    if no_reply:
        return out
    out.insert(0, "ask_sender")
    if kind == "question":
        return out
    try:
        meeting = bool(dg.meeting_row(dict(row, thread_brief=_brief(row))))
        choice = dg.owner_choice(cand["ask"], cand["state"], cand["scheduling"], cand["brief"])
        work = bool(_mod("mail_completion").non_email_work(cand))
    except Exception:
        return out
    if meeting:
        out.insert(0, "propose_times")
    if not choice and not work:
        out.insert(0, "draft_reply")
    return out


# ── the packet ───────────────────────────────────────────────────────

KIND_WORDS = {"reply": "a reply he owes", "item": "something on his list", "question":
              "a decision only he can make", "request": "a request card that waits for his tap",
              "closed": "something closed today"}


def _project(lid: str, *, conn=None) -> dict | None:
    """The project the row is explicitly linked to: {"id", "name",
    "summary", "next"}, read-only; None without a link."""
    if not lid:
        return None
    br = _mod("briefing_review")
    own = conn is None
    try:
        conn = conn or br._connect()
        factory, conn.row_factory = conn.row_factory, br._dict_row
        try:
            r = conn.execute("SELECT p.id, p.name, p.summary, p.next_actions FROM project_ledger l "
                             "JOIN projects p ON p.id = l.project_id WHERE l.ledger_id = ? "
                             "AND COALESCE(p.status, '') != 'closed' LIMIT 1", (lid,)).fetchone()
        finally:
            conn.row_factory = factory
            if own:
                conn.close()
    except Exception:
        return None
    if not r:
        return None
    return {"id": r["id"], "name": _one(r["name"], 80), "summary": _one(r.get("summary"), 400),
            "next": _one(r.get("next_actions"), 200)}


def _calendar(briefing: dict, *, now: float, zone: ZoneInfo, until: date | None) -> dict:
    """Today's and tomorrow's events, and the free slots from now to the
    due date (a week when there is none), from the briefing's data."""
    H = _mod("home_screen")
    today = datetime.fromtimestamp(now, zone).date()
    events = []
    src = H.Sources(now=now, zone=zone, briefing=briefing or {})
    for e in [e for e in H.events(src) if e["end"] > now][:12]:
        d = datetime.fromtimestamp(e["start"], zone)
        if e["all_day"]:
            events.append(f"{day_words(d.date())}, all day: {e['title']}")
        else:
            events.append(f"{day_words(d.date())} {H.clock(e['start'], zone)} to "
                          f"{H.clock(e['end'], zone)}: {e['title']}")
    last = (until or today + timedelta(days=7)) + timedelta(days=1)
    free = []
    for s in (briefing or {}).get("availability_merged") or []:
        if not isinstance(s, dict):
            continue
        start, end = H._ts(s.get("start")), H._ts(s.get("end"))
        if start is None or end is None or end < now:
            continue
        d = datetime.fromtimestamp(start, zone).date()
        if d > last:
            continue
        free.append(f"{day_words(d)} {H.clock(start, zone)} to {H.clock(end, zone)} "
                    f"({int((end - start) // 60)} minutes)")
        if len(free) >= 8:
            break
    return {"events": events, "free_slots": free}


def _waited(row: dict, brief: dict, now: float) -> int | None:
    """Days since the other side last wrote (or since the row began)."""
    for v in (brief.get("last_inbound"), row.get("date_identified"), row.get("created_at")):
        d = _date(v)
        if d:
            return max(0, (datetime.fromtimestamp(now, timezone.utc).date() - d).days)
    return None


def build_packet(ref: str, item: dict, row: dict | None, *, level: str, now: float,
                 zone: ZoneInfo, ledger: dict | None = None, briefing: dict | None = None,
                 project: dict | None = None, rules: str = "", allowed=(),
                 shed: int = 0) -> dict:
    """The records the model reads for one item (module docstring). Mail
    text never goes: the thread is its brief's records (gists), with no
    quote from mail. `ledger` is briefing_review.read_ledger's shape for
    the row and its conversation; `shed` drops detail to fit."""
    br = _mod("briefing_review")
    item = item or {}
    row = row or {}
    today = datetime.fromtimestamp(now, zone)
    n_events, n_msgs, n_sib, n_free = ((6, 6, 4, 8), (4, 4, 3, 5), (2, 2, 2, 3))[min(shed, 2)]
    quick = item.get("brief") if isinstance(item.get("brief"), dict) else {}
    out: dict = {
        "written_on": f"{today:%Y-%m-%d} ({day_words(today.date())})",
        "item": {"ref": ref, "kind": KIND_WORDS.get(str(item.get("kind") or ""), "an item"),
                 "title": _one(item.get("title"), 200)},
        "steps_you_may_choose": list(allowed),
    }
    q = {k: (_one(v, 200) if isinstance(v, str) else [_one(x, 160) for x in v][:4])
         for k, v in quick.items() if k in ("what", "asked", "due", "next") and v}
    if q:
        out["item"]["quick_briefing"] = q
    if not row:
        if rules:
            out["owner_rules"] = rules[:1200 if shed else 2000]
        return out
    lid = row.get("id") or ""
    facts = {"id": lid, "type": row.get("type") or "", "status": row.get("status") or "open",
             "description": _one(row.get("description"), 240)}
    for k in ("deadline", "follow_up_after", "snooze_until", "date_identified", "closed_at"):
        if row.get(k):
            facts[k] = _with_words(row[k])
    for k, cap in (("last_action", 240), ("notes", 400 if not shed else 200)):
        if row.get(k):
            facts[k] = _one(row[k], cap)
    if row.get("from_name") or row.get("from_email"):
        facts["from"] = _one(row.get("from_name") or row.get("from_email"), 80)
    out["row"] = facts
    brief = _brief(row)
    said = row.get("owner_said") or _meta(row).get("owner_said")
    if said:
        try:
            line = _mod("ledger").owner_said_line(said)
        except Exception:
            line = ""
        if line:
            out["owner_said"] = _one(line, 300)
    led = ledger or {"rows": {}, "events": {}, "by_conv": {}}
    evs = led.get("events", {}).get(lid) or []
    if evs:
        out["history"] = [dict(br._event_view(e, {"event_note": 160}),
                               at=_with_words((e.get("ts") or "")[:16])) for e in evs[-n_events:]]
    if brief:
        view = br._brief_view(brief, n_msgs, {"gist": 200})
        for m in view.get("messages") or []:
            m["at"] = _with_words(m.get("at"))
        if view.get("deadline"):
            view["deadline"] = _with_words(view["deadline"])
        out["thread"] = view
    conv = row.get("conversation_id") or brief.get("conversation_id") or ""
    sibs = [r for r in (led.get("by_conv", {}).get(conv) or []) if r.get("id") != lid] if conv else []
    if sibs:
        view = []
        for r in sorted(sibs, key=lambda r: (str(r.get("status")) in CLOSED, r.get("closed_at") or ""),
                        reverse=True)[:n_sib]:
            s = {"type": r.get("type") or "", "status": r.get("status") or "",
                 "description": _one(r.get("description"), 160)}
            if r.get("closed_at"):
                s["closed_at"] = _with_words(r["closed_at"])
            uc = br.user_close(r, led.get("events", {}).get(r.get("id")) or [])
            if uc:
                s["closed_by_owner"] = {"at": _with_words(uc["at"][:16]),
                                        "said": _one(uc["quote"] or uc["note"], 200)}
            view.append(s)
        out["same_thread"] = view
    if project:
        out["project"] = {k: v for k, v in project.items() if k != "id" and v}
    due = _date(row.get("deadline")) or _date(brief.get("deadline"))
    cal = _calendar(briefing or {}, now=now, zone=zone, until=due)
    cal["free_slots"] = cal["free_slots"][:n_free]
    if cal["events"] or cal["free_slots"]:
        out["calendar"] = cal
    if rules:
        out["owner_rules"] = rules[:1200 if shed else 2000]
    return out


def fit_packet(build: Callable[[int], dict]) -> dict:
    """The first packet that fits PROMPT_MAX_CHARS, shedding detail step by
    step; the last step cut to size."""
    for shed in range(3):
        p = build(shed)
        if len(json.dumps(p, ensure_ascii=False)) <= PROMPT_MAX_CHARS:
            return p
    p.pop("calendar", None)
    p.pop("owner_rules", None)
    while len(json.dumps(p, ensure_ascii=False)) > PROMPT_MAX_CHARS and (p.get("history") or []):
        p["history"].pop(0)
    return p


# ── the prompt and the answer's shape ────────────────────────────────

PROMPT = """You brief the owner of an assistant on one item of his. He reads it when he opens the item on the assistant's Home screen, maybe days from now. The records below are the assistant's own: summaries of mail, never the mail itself.

Write three short lists:
- history: what happened so far, oldest first, 2 to 4 short sentences.
- stakes: what is at stake or due, 1 to 3 short sentences.
- unknown: what the records do not show that matters, 0 to 3 short sentences.

Then choose 2 or 3 next steps. Each step has a `kind` from `steps_you_may_choose`, and fills only that kind's fields:
- draft_reply: `say`, what the reply says, in one short line. Never answer a choice that is his to make (yes or no, accept or decline, join or not).
- ask_sender: `question`, one short question to the sender.
- propose_times: no fields; the assistant offers two or three times from his calendar.
- wait_until: `date` (YYYY-MM-DD), when to look at it again, after `written_on`.
- mark_done: no fields; only when the records show it is finished or needs nothing from him.
- work_on_it: `ask`, what the assistant should start on, in one short line.
- hold_slot: `date` (YYYY-MM-DD) and `minutes` (15 to 240), time on his calendar to do the work, before it is due. Only when the work needs a block of time; size it to the work, and pick the free slot that suits it, not always the first.
`why` is a few words on why the step fits.

Rules:
- Use only the records. Never invent a fact, a name, a date or a number.
- His own words (`owner_said`, `closed_by_owner`) outrank the assistant's notes.
- Write no relative time: never today, tomorrow, yesterday, this week, next week, ago, or a count of days. Write dates as the records write them. When he opens it, the card adds how long it has waited and his free time.
- Do not describe his calendar in the lists; use it only to pick a date for a step.
- Short plain sentences of 20 words or fewer. No dash between clauses. No markdown, no links.
- Leave a field you do not use as "" (0 for minutes).

Answer with one JSON object and nothing else:
{"history": [], "stakes": [], "unknown": [], "steps": [{"kind": "", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""}]}

The records:
"""

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "history": {"type": "array", "items": {"type": "string"}},
    "stakes": {"type": "array", "items": {"type": "string"}},
    "unknown": {"type": "array", "items": {"type": "string"}},
    "steps": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"kind": {"type": "string", "enum": list(STEP_KINDS)},
                       "say": {"type": "string"}, "question": {"type": "string"},
                       "date": {"type": "string"}, "minutes": {"type": "integer"},
                       "ask": {"type": "string"}, "why": {"type": "string"}},
        "required": ["kind", "say", "question", "date", "minutes", "ask", "why"]}}},
    "required": ["history", "stakes", "unknown", "steps"]}


def prompt_text(packet: dict) -> str:
    return PROMPT + json.dumps(packet, ensure_ascii=False)


def parse_answer(text: str) -> dict:
    """The answer object out of a reply, tolerant of fences and prose
    around it; {} when there is none."""
    m = _JSON.search(text or "")
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        try:
            obj = json.loads(_mod("briefing_review")._balanced(m.group(0)))
        except (ValueError, TypeError):
            return {}
    return obj if isinstance(obj, dict) else {}


# ── code's check of the words ────────────────────────────────────────

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60})
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_ABBR = {m[:3]: m for m in _MONTHS} | {d[:3]: d for d in _DAYS}
# Capitalized words a sentence may hold that name nothing from the records.
_ALWAYS = frozenset({"I", "Tacitus", "OK", "Outlook", "Home", "Action", "Requests", "Ask", "AM", "PM",
                     "Dr", "Prof", "Mr", "Ms", "Mrs"})
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_BAD = re.compile(r"https?:|www\.|@|[<>`*#\[\]{}]")
# Words that tie a sentence to the moment it was written: a stored
# briefing never says them (the card adds them in code when it is opened).
_RELATIVE = re.compile(r"\b(?:today|tonight|tomorrow|yesterday|this (?:morning|afternoon|evening|week|month)|"
                       r"next (?:week|month)|last (?:week|month)|ago|\d+ (?:more )?days?|"
                       r"(?:one|two|three|four|five|six|seven|eight|nine|ten) (?:more )?days?|"
                       r"overdue by|days? (?:left|late|overdue))\b", re.I)


class Known:
    """What the packet holds, for checking a sentence against it."""

    def __init__(self, packet: dict) -> None:
        self.text = json.dumps(packet, ensure_ascii=False).lower()
        self.digits = {d.lstrip("0") or "0" for d in re.findall(r"\d+", self.text)}
        self.words = set(re.findall(r"[a-z][a-z'\-]*", self.text))

    def number(self, n: str) -> bool:
        return (n.lstrip("0") or "0") in self.digits

    def holds(self, word: str) -> bool:
        w = word.lower().strip("'-")
        if w in self.words or w.rstrip("s") in self.words or w.removesuffix("'s") in self.words:
            return True
        return len(w) > 3 and w in self.text


def checked(sentence: str, known: Known) -> bool:
    """True when every number, day, month and name in `sentence` is in the
    records: a digit the packet does not hold, a number word above one, a
    weekday or month it never names, a capitalized word that does not open
    a sentence (or an acronym anywhere) it does not hold, a link or an
    address, or a word that ties it to the moment it was written (today,
    ago, 3 days: a stored briefing outlives its day) fails it."""
    s = " ".join(str(sentence or "").split())
    if not s or _BAD.search(s) or _RELATIVE.search(s):
        return False
    for n in re.findall(r"\d+", s):
        if not known.number(n):
            return False
    for m in _WORD.finditer(s):
        w = m.group(0)
        lw = w.lower()
        if lw in _NUMBER_WORDS and lw != "one" and not known.number(str(_NUMBER_WORDS[lw])):
            return False
        full = _ABBR.get(lw.rstrip("."), lw) if w[0].isupper() else ""  # "may", "sat": plain words
        if full in _DAYS or full in _MONTHS:
            if full not in known.text:
                return False
            continue
        if w in _ALWAYS:
            continue
        opens = not s[:m.start()].strip() or re.search(r"[.!?:;]\s*$", s[:m.start()]) is not None
        acronym = len(w) >= 2 and w.isupper()
        if (acronym or (w[0].isupper() and not opens)) and not known.holds(w):
            return False
    return True


def _clean_line(text: Any) -> str:
    return _one(str(text or "").replace("\u2014", ", ").replace("\u2013", "-"), LINE_MAX).strip(" -")


def render(sections: dict[str, list[str]]) -> str:
    """The card's text: a heading line per section and its lines as
    bullets, sections split by a blank line."""
    parts = []
    for key, head, _n in SECTIONS:
        lines = sections.get(key) or []
        if lines:
            parts.append(head + "\n" + "\n".join(f"- {x}" for x in lines))
    return "\n\n".join(parts)


# ── code's check of the steps ────────────────────────────────────────

def _step_label(kind: str, p: dict, choice: str = "") -> str:
    if kind == "draft_reply":
        return _one(f"Draft a reply: {p['say']}", LABEL_MAX)
    if kind == "ask_sender":
        return _one(f"Ask them: {p['question']}", LABEL_MAX)
    if kind == "propose_times":
        return "Offer times to meet"
    if kind == "wait_until":
        return f"Wait until {_short_day(_date(p['date']))}"
    if kind == "mark_done":
        return "Not needed" if choice == "not_needed" else "Mark it done"
    if kind == "work_on_it":
        return _one(f"Start: {p['ask']}", LABEL_MAX)
    if kind == "hold_slot":
        return f"Hold {p['minutes']} minutes on {_short_day(_date(p['date']))}"
    return ""


_DECIDES = re.compile(r"\b(?:accept|decline|say (?:yes|no)|tell (?:them|him|her) (?:yes|no)|agree to|"
                      r"turn (?:it|this|them) down|commit to|sign up|consent|approve|authori[sz]e|"
                      r"opt (?:in|out))\b", re.I)


def check_step(raw: dict, *, allowed, known: Known, today: date, due: date | None,
               cand: dict) -> tuple[dict | None, str]:
    """(the step, '') when it passes; (None, why) when code refuses it:
    a kind the item does not allow, a field missing or too long, words the
    records do not hold, a date not after today (or past STEP_DAYS, or for
    a hold past the due date), a draft that answers the owner's choice or
    an invitation (draft_gate.check), a question or a task that does."""
    kind = str((raw or {}).get("kind") or "").strip()
    if kind not in STEP_KINDS:
        return None, "unknown kind"
    if kind not in allowed:
        return None, f"{kind} is not open for this item"
    p: dict = {}
    dg = _mod("draft_gate")
    if kind in ("draft_reply", "ask_sender", "work_on_it"):
        field = {"draft_reply": "say", "ask_sender": "question", "work_on_it": "ask"}[kind]
        text = " ".join(str(raw.get(field) or "").split())
        if not text or len(text) > SAY_MAX:
            return None, f"{field} missing or too long"
        if not checked(text, known):
            return None, f"{field} holds words the records do not"
        if dg.answers_invitation(text) or _DECIDES.search(text):
            return None, f"{field} answers a choice that is the owner's"
        if kind == "draft_reply":
            try:
                held = dg.check(cand, text)
            except Exception:
                held = {"rule": "check failed"}
            if held:
                return None, f"the draft gate holds it ({held.get('rule')})"
        p[field] = text
    if kind in ("wait_until", "hold_slot"):
        d = _date(raw.get("date"))
        if not d or (kind == "wait_until" and d <= today) or d < today \
                or d > today + timedelta(days=STEP_DAYS):
            return None, "date missing or out of range"
        if kind == "hold_slot" and due and d > due:
            return None, "a hold after the due date"
        p["date"] = d.isoformat()
    if kind == "hold_slot":
        try:
            minutes = int(raw.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if not 15 <= minutes <= 240:
            return None, "minutes out of range"
        p["minutes"] = minutes - minutes % 5
    return {"kind": kind, "params": p}, ""


def check_answer(obj: dict, packet: dict, *, allowed, today: date, due: date | None,
                 cand: dict) -> dict:
    """The answer, checked: {"text", "steps", "unchecked", "rejected",
    "ok"}. A sentence code cannot check reads UNCHECKED (once in a row);
    a step code refuses is left out with its reason in `rejected`."""
    known = Known(packet)
    sections: dict[str, list[str]] = {}
    unchecked = shown = 0
    for key, _head, cap in SECTIONS:
        lines: list[str] = []
        for raw in (obj.get(key) if isinstance(obj.get(key), list) else [])[:cap]:
            line = _clean_line(raw)
            if not line:
                continue
            if checked(line, known):
                lines.append(line)
                shown += 1
            else:
                unchecked += 1
                if not lines or lines[-1] != UNCHECKED:
                    lines.append(UNCHECKED)
        sections[key] = lines
    steps, rejected, seen = [], [], set()
    for raw in (obj.get("steps") if isinstance(obj.get("steps"), list) else [])[:5]:
        if not isinstance(raw, dict):
            continue
        step, why = check_step(raw, allowed=allowed, known=known, today=today, due=due, cand=cand)
        if step is None:
            rejected.append({"kind": str(raw.get("kind") or "")[:20], "why": why})
            continue
        key = (step["kind"], json.dumps(step["params"], sort_keys=True))
        if key in seen or len(steps) >= 3:
            continue
        seen.add(key)
        step["id"] = f"s{len(steps) + 1}"
        steps.append(step)
    return {"text": render(sections), "steps": steps, "unchecked": unchecked,
            "rejected": rejected, "ok": shown > 0}


def offered(steps: list[dict], item: dict | None) -> list[dict]:
    """The steps the item offers now, each with its label: a step that runs
    as one of home_items' choices needs that choice on the item (when the
    item lists its choices); the owner's own steps need nothing."""
    ids = {str(c.get("id")) for c in (item or {}).get("choices") or [] if isinstance(c, dict)}
    out = []
    for s in steps or []:
        kind = s.get("kind")
        choice = ""
        if kind in CHOICE_OF:
            hit = [c for c in CHOICE_OF[kind] if c in ids]
            if ids and not hit:
                continue
            choice = hit[0] if hit else CHOICE_OF[kind][0]
        if kind not in CHOICE_OF and kind not in OWN_STEPS:
            continue
        try:
            label = _step_label(kind, s.get("params") or {}, choice)
        except Exception:
            continue
        out.append({"id": s["id"], "label": label, "kind": kind, "choice": choice,
                    "params": s.get("params") or {}})
    return out


# ── where it runs ────────────────────────────────────────────────────

def _remote_cfg() -> dict:
    try:
        return _mod("remote_llm").config()
    except Exception:
        return {}


def local_ready(cfg: dict | None = None) -> bool:
    try:
        return bool(_mod("model_routing").local_ready(cfg if cfg is not None else _remote_cfg()))
    except Exception:
        return False


def hosted_name() -> str:
    """The hosted model the table names for the task, as the owner reads
    it: "Claude", "Codex"."""
    try:
        mr = _mod("model_routing")
        m = mr.for_model(_mod("hosted_text").model_for(TASK))
        return (m.name if m else "") or "the hosted model"
    except Exception:
        return "the hosted model"


def route(level: str, *, cfg: dict | None = None) -> list[str]:
    """The models a write at ingest or at a tap tries, in order: the owner's
    own model first, the hosted model when it does not answer; [] for a
    kept item with no local model. Kept below Summary (or a level not
    known): the owner's own model alone. The hosted rewrite of the day's
    list is hosted_pass, at briefing time."""
    cfg = cfg if cfg is not None else _remote_cfg()
    ready = local_ready(cfg)
    if kept(level) or level not in ("summary", "full"):
        return ["local"] if ready else []
    return ["local", "hosted"] if ready else ["hosted"]


def _ask_local(prompt: str) -> dict:
    """One call to the owner's own model under SCHEMA: {"ok", "text",
    "usage", "reason"}."""
    rl = _mod("remote_llm")
    try:
        res = rl.chat([{"role": "system", "content": "You brief the owner on one item. Answer in the schema."},
                       {"role": "user", "content": prompt}], purpose=PURPOSE, schema=SCHEMA,
                      schema_name="home_deeper", max_tokens=LOCAL_MAX_TOKENS, timeout=LOCAL_TIMEOUT_S,
                      slot_wait=10.0)
    except Exception as e:
        return {"ok": False, "text": "", "usage": {}, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
    u = res.get("usage") or {}
    return {"ok": True, "text": str(res.get("content") or ""), "reason": "",
            "usage": {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens")}}


def _plan_full() -> bool:
    try:
        return bool(_mod("briefing_review")._plan_full())
    except Exception:
        return False


def _ask_hosted(prompt: str) -> dict:
    """One tool-less hosted call (hosted_text, the `briefing` task):
    {"ok", "text", "usage", "reason" (limit, timeout, error), "model"}."""
    br = _mod("briefing_review")
    try:
        ht = _mod("hosted_text")
        who = ht.model_for(TASK)
        if ht.is_base(who) and _plan_full():
            return {"ok": False, "text": "", "usage": {}, "reason": "limit", "model": who}
        model = _mod("ea_config").get_models().get("home_deeper") or HOSTED_MODEL
        r = ht.run(TASK, prompt, model, timeout=HOSTED_TIMEOUT_S)
    except Exception as e:
        return {"ok": False, "text": "", "usage": {}, "reason": br._why_failed(str(e)), "model": ""}
    u = r.get("usage") or {}
    usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens"), "cost_usd": r.get("cost_usd")}
    if r.get("rc") or not r.get("text"):
        return {"ok": False, "text": "", "usage": usage, "model": r.get("model_used") or "",
                "reason": br._why_failed(f"{r.get('stderr') or ''} {r.get('text') or ''}")}
    return {"ok": True, "text": str(r["text"]), "usage": usage, "reason": "",
            "model": r.get("model_used") or ""}


# The calls; tests put fakes here.
LOCAL: Callable[[str], dict] = _ask_local
HOSTED: Callable[[str], dict] = _ask_hosted


def _model_name(key: str) -> str:
    if key == "local":
        return LOCAL_NAME
    try:
        m = _mod("model_routing").for_model(key)
        return (m.name if m else "") or hosted_name()
    except Exception:
        return hosted_name()


def run_model(prompt: str, order: list[str]) -> dict:
    """Try each model of `order` until one answers: {"ok", "text", "model",
    "via" (local | hosted), "usage", "note", "calls", "tries"}. The note
    says so when the answer is not the first choice's."""
    tries, calls = [], 0
    hosted = hosted_name()
    for i, via in enumerate(order):
        r = (LOCAL if via == "local" else HOSTED)(prompt)
        calls += 0 if (via == "hosted" and r.get("reason") == "limit" and not r.get("usage")) else 1
        tries.append({"via": via, "ok": bool(r.get("ok")), "reason": r.get("reason") or ""})
        if r.get("ok"):
            note = ""
            if i:
                prev = tries[0]
                note = (NOTE_LIMIT_LOCAL if prev["via"] == "hosted" and prev["reason"] == "limit"
                        else NOTE_FELL_BACK if prev["via"] == "local" else "").format(hosted=hosted)
            return {"ok": True, "text": r["text"], "via": via, "usage": r.get("usage") or {},
                    "model": _model_name("local" if via == "local" else (r.get("model") or "")),
                    "note": note, "calls": calls, "tries": tries}
    limit = any(t["via"] == "hosted" and t["reason"] == "limit" for t in tries)
    only_local = order == ["local"]
    note = (NOTE_LIMIT.format(hosted=hosted) if limit else NOTE_LOCAL_FAILED if only_local else NOTE_FAILED)
    return {"ok": False, "text": "", "via": "", "usage": {}, "model": "", "note": note, "calls": calls,
            "tries": tries}


# ── the stored briefings ─────────────────────────────────────────────
# One briefing per open ledger row, written ahead of the tap: by the
# owner's own model when the row or its thread changes (refresh, from the
# mail tick), by the hosted model for the day's list at briefing time
# (hosted_pass). Each carries the version of the row and thread it was
# written from (row_version); a tap shows it at once, and asks for a new
# one only when it is missing or older than the thread.
#
# Where: an owner-private SQLite file beside the local model's records
# (<private_dir>/remote-llm/home-briefings.sqlite, 0600), never the
# ledger. The ledger is the resident's: hosted turns read it with
# `ledger.py get`, and a kept thread's briefing, written by the owner's
# own model from its records, must not sit where a hosted turn reads more
# than its level lets it.

STORE_FILE = "home-briefings.sqlite"
# Writes per mail tick, and per day, by the owner's own model.
INGEST_PER_TICK = 4
INGEST_DAILY = 200
# The hosted rewrite at briefing time: items at most, and the prompt's
# size per call (more items go in another call).
HOSTED_ITEMS = 20
HOSTED_PROMPT_MAX = 80_000
HOSTED_TASK = "review"
VERSION_ROW = ("status", "type", "deadline", "follow_up_after", "snooze_until", "description",
               "last_action", "notes")
VERSION_THREAD = ("latest", "message_count", "state", "ask", "needs_reply", "deadline",
                  "user_replied_after_latest", "scheduling")


def row_version(row: dict | None) -> str:
    """The version of a row and its thread a briefing is written from:
    Home's own (home_items.row_version: the row's fields, its thread's
    record, the reply queue's record and a close proposal, never anything
    that moves with the clock), so a stored briefing and the item agree on
    when it is stale. The rows are in the ledger's merged shape
    (ledger.get_open_entries, read_row). Without home_items, the same
    reading in this module."""
    try:
        return _mod("home_items").row_version(row)
    except (ImportError, AttributeError):
        return _own_version(row)


def _own_version(row: dict | None) -> str:
    row = row or {}
    brief = _brief(row)
    key = {"row": {k: row.get(k) for k in VERSION_ROW}, "thread": {k: brief.get(k) for k in VERSION_THREAD}}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ── rows written from chat ───────────────────────────────────────────
# ledger.py's row-written hook is home_briefing_queue, a small module of its
# own (the resident loads it on every ledger write): it notes the row's id
# in the resident's state, and refresh writes the noted rows first.

def queued_ids(*, now: float | None = None) -> list[str]:
    """The ids the resident noted (home_briefing_queue), newest first, read
    with the resident's access (resident_io); [] when there are none."""
    try:
        q = _mod("home_briefing_queue")
        return q.ids(_mod("resident_io").read_text(q.path()) or "", now=now)
    except Exception:
        return []


def _store_path() -> Path:
    return _mod("remote_llm").store_dir() / STORE_FILE


class Store:
    """The stored briefings (module comment above). Owner processes only:
    the mail tick and the briefing write, the bot reads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else _store_path()

    def _conn(self):
        import os
        import sqlite3
        if not self.path.exists():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS briefings (lid TEXT PRIMARY KEY, version TEXT, by TEXT, "
                     "at REAL, level TEXT, text TEXT, steps TEXT, model TEXT, note TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS days (day TEXT, what TEXT, n INTEGER, "
                     "PRIMARY KEY (day, what))")
        return conn

    def get(self, lid: str) -> dict | None:
        if not lid or not self.path.exists():
            return None
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM briefings WHERE lid = ?", (lid,)).fetchone()
        finally:
            conn.close()
        if not r:
            return None
        out = dict(r)
        try:
            out["steps"] = json.loads(out.get("steps") or "[]")
        except ValueError:
            out["steps"] = []
        return out

    def versions(self) -> dict[str, tuple[str, str]]:
        """{ledger id: (version, by)} of every stored briefing."""
        if not self.path.exists():
            return {}
        conn = self._conn()
        try:
            return {r["lid"]: (r["version"], r["by"]) for r in conn.execute("SELECT lid, version, by FROM briefings")}
        finally:
            conn.close()

    def put(self, lid: str, rec: dict) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO briefings (lid, version, by, at, level, text, steps, model, "
                             "note) VALUES (?,?,?,?,?,?,?,?,?)",
                             (lid, rec.get("version") or "", rec.get("by") or "local", float(rec.get("at") or 0),
                              rec.get("level") or "", rec.get("text") or "", json.dumps(rec.get("steps") or []),
                              rec.get("model") or "", rec.get("note") or ""))
        finally:
            conn.close()

    def count(self, day: str, what: str, add: int = 0) -> int:
        conn = self._conn()
        try:
            with conn:
                if add:
                    conn.execute("INSERT INTO days (day, what, n) VALUES (?,?,?) ON CONFLICT(day, what) "
                                 "DO UPDATE SET n = n + excluded.n", (day, what, int(add)))
                r = conn.execute("SELECT n FROM days WHERE day = ? AND what = ?", (day, what)).fetchone()
                conn.execute("DELETE FROM days WHERE day < ?", ((date.fromisoformat(day) - timedelta(days=30))
                                                               .isoformat(),))
            return int(r["n"]) if r else 0
        finally:
            conn.close()

    def drop_closed(self, open_ids: set) -> int:
        """Forget the briefings of rows no longer open."""
        if not self.path.exists():
            return 0
        conn = self._conn()
        try:
            with conn:
                gone = [r["lid"] for r in conn.execute("SELECT lid FROM briefings") if r["lid"] not in open_ids]
                conn.executemany("DELETE FROM briefings WHERE lid = ?", [(g,) for g in gone])
            return len(gone)
        finally:
            conn.close()


def _today_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), _zone()).date().isoformat()


def _kind_of(row: dict) -> str:
    """How Home names a row's item when no item is at hand: a reply when a
    send in its thread finishes it and it is no deliverable (the ledger's
    traits), else something on the list."""
    try:
        lg = _mod("ledger")
        replies = lg.types_with("closes_on_reply") - lg.types_with("deliverable")
        return "reply" if lg.canonical_type(row.get("type")) in replies else "item"
    except Exception:
        return "item"


def write_one(row: dict, *, now: float, level: str | None = None, order: list[str] | None = None,
              store: Store | None = None, kind: str = "", title: str = "", item: dict | None = None,
              prepared: dict | None = None) -> dict:
    """Write one row's briefing on the models of `order` (route(level) when
    not given) and store it with the row's version. {"ok", "via", "model",
    "seconds", "usage", "unchecked", "rejected", "note", "calls"}."""
    t0 = time.monotonic()
    zone = _zone()
    item = item or {"ref": f"item:{row.get('id')}", "kind": kind or _kind_of(row),
                    "title": title or row.get("description") or ""}
    prep = prepared or prepare(item["ref"], item, row, now=now, zone=zone, level=level)
    level = prep["level"]
    order = route(level) if order is None else order
    if not order:
        return {"ok": False, "via": "", "calls": 0, "note": NOTE_KEPT_NO_LOCAL, "level": level,
                "seconds": 0.0, "usage": {}, "unchecked": 0, "rejected": []}
    res = run_model(prep["prompt"], order)
    out = {"ok": False, "via": res["via"], "model": res["model"], "usage": res["usage"], "calls": res["calls"],
           "note": res["note"], "level": level, "unchecked": 0, "rejected": [], "tries": res["tries"],
           "prompt_chars": len(prep["prompt"])}
    if res["ok"]:
        obj = parse_answer(res["text"])
        today = datetime.fromtimestamp(now, zone).date()
        got = check_answer(obj, prep["packet"], allowed=prep["allowed"], today=today, due=prep["due"],
                           cand=prep["cand"]) if obj else None
        if got and got["ok"]:
            (store or Store()).put(str(row.get("id")), {
                "version": row_version(row), "by": res["via"], "at": time.time(), "level": level,
                "text": got["text"], "steps": got["steps"], "model": res["model"], "note": res["note"]})
            out.update(ok=True, unchecked=got["unchecked"], rejected=got["rejected"],
                       text=got["text"], steps=got["steps"])
        else:
            out.update(note=NOTE_UNCHECKED, unchecked=(got or {}).get("unchecked", 0))
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def _open_rows() -> list[dict]:
    """The open rows as Home reads them (ledger.get_open_entries, with the
    owner's word on each thread), read-only."""
    lg = _mod("ledger")
    return lg.with_owner_decisions(lg.get_open_entries())


def stale_rows(rows: list[dict], versions: dict, *, prefer=()) -> list[dict]:
    """The rows whose stored briefing is missing or older than the row and
    its thread: the ids in `prefer` first (Home's list), then the newest."""
    stale = [r for r in rows if r.get("id") and (versions.get(r["id"]) or ("",))[0] != row_version(r)]
    first = {lid: i for i, lid in enumerate(prefer or ())}
    stale.sort(key=lambda r: (first.get(r["id"], len(first)), str(r.get("updated_at") or "")), reverse=False)
    head = [r for r in stale if r["id"] in first]
    rest = sorted([r for r in stale if r["id"] not in first], key=lambda r: str(r.get("updated_at") or ""),
                  reverse=True)
    return head + rest


def refresh(*, limit: int = INGEST_PER_TICK, prefer=(), rows: list[dict] | None = None,
            store: Store | None = None, dry_run: bool = False, daily: int | None = INGEST_DAILY) -> dict:
    """Write the briefings that are missing or stale, on the owner's own
    model, at most `limit` now and `daily` a day: the mail tick's step
    after the thread briefs (a new message makes its row's briefing stale),
    a row written from chat, and the backfill. Kept rows run on the owner's
    own model alone, others fall back to the hosted model only when it
    does not answer. Nothing without a local model. {"stale", "written",
    "failed", "seconds", ...}; `dry_run` counts and writes nothing."""
    t0 = time.monotonic()
    out = {"stale": 0, "written": 0, "failed": 0, "skipped": "", "seconds": 0.0, "tokens_in": 0,
           "tokens_out": 0, "calls": 0}
    if not local_ready():
        out["skipped"] = "no local model"
        return out
    store = store or Store()
    rows = rows if rows is not None else _open_rows()
    prefer = list(prefer or ()) + [lid for lid in queued_ids() if lid not in (prefer or ())]
    try:
        store.drop_closed({r.get("id") for r in rows if r.get("id")}) if not dry_run else None
    except Exception:
        pass
    todo = stale_rows(rows, store.versions(), prefer=prefer)
    out["stale"] = len(todo)
    if dry_run or not todo:
        out["seconds"] = round(time.monotonic() - t0, 1)
        return out
    day = _today_key()
    left = limit
    if daily is not None:
        left = min(left, max(0, daily - store.count(day, "ingest")))
        if not left:
            out["skipped"] = "the day's writes are spent"
    for row in todo[:left]:
        level = _level({}, row)
        got = write_one(row, now=time.time(), level=level, order=["local"] if kept(level) else None, store=store)
        out["calls"] += got.get("calls") or 0
        out["written" if got["ok"] else "failed"] += 1
        u = got.get("usage") or {}
        out["tokens_in"] += int(u.get("in") or 0)
        out["tokens_out"] += int(u.get("out") or 0)
        store.count(day, "ingest", got.get("calls") or 0)
        _log({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "ingest",
              "ref": hashlib.sha256(str(row.get("id")).encode()).hexdigest()[:12], "level": level,
              "ok": got["ok"], "via": got.get("via"), "seconds": got.get("seconds"), "usage": u,
              "unchecked": got.get("unchecked"), "rejected": got.get("rejected")})
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


# ── the hosted rewrite of the day's list ─────────────────────────────

HOSTED_PROMPT = """You rewrite the assistant's briefings for the items on the owner's list today. For each item below, write the briefing as the instructions say, from that item's own records alone; never carry a fact from one item to another.

{instructions}
Answer with one JSON object and nothing else:
{{"items": [{{"ref": "<the item's ref>", "history": [], "stakes": [], "unknown": [], "steps": [{{"kind": "", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""}}]}}]}}

The items:
"""


def _instructions() -> str:
    head, _, rest = PROMPT.partition("Write three short lists:")
    body = rest.split("Answer with one JSON object", 1)[0]
    return "Write three short lists:" + body


def hosted_prompt(packets: list[dict]) -> str:
    return HOSTED_PROMPT.format(instructions=_instructions()) + json.dumps(packets, ensure_ascii=False)


def _batches(packets: list[dict], cap: int) -> list[list[dict]]:
    out, cur = [], []
    for p in packets:
        if cur and len(hosted_prompt(cur + [p])) > cap:
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return out


def hosted_pass(items: list[dict], *, briefing: dict | None = None, rows: list[dict] | None = None,
                now: float | None = None, store: Store | None = None, call=None,
                dry_run: bool = False, record: dict | None = None) -> dict:
    """The hosted model rewrites the briefings of the items on Home's list
    (the post's items after its review, `items` as remote_briefing.home_view
    lists them, and the replies owed a day or more), at most HOSTED_ITEMS,
    in as few tool-less calls as fit HOSTED_PROMPT_MAX. Kept items are left
    out: theirs stays the owner's own model's. Each answer is checked as
    every briefing is, against its own packet, and stored `by: hosted`;
    when the call fails the briefing the owner's own model wrote stands.
    The run record's part: {"items", "kept", "calls", "stored", "seconds",
    "tokens_in", "tokens_out", "cost_usd", "ok", "reason"}."""
    t0 = time.monotonic()
    now = now if now is not None else time.time()
    out = {"items": 0, "kept": 0, "calls": 0, "stored": 0, "refused": 0, "seconds": 0.0, "tokens_in": 0,
           "tokens_out": 0, "cost_usd": 0.0, "ok": False, "reason": ""}
    try:
        rows = rows if rows is not None else _open_rows()
    except Exception as e:
        out["reason"] = f"the ledger could not be read: {str(e)[:80]}"
        return out
    by_id = {r.get("id"): r for r in rows if r.get("id")}
    want: list[str] = []
    for it in items or []:
        lid = str((it or {}).get("ledger_id") or "")
        if it.get("kept"):
            out["kept"] += 1
            continue
        if lid and lid in by_id and lid not in want:
            want.append(lid)
    for e in (briefing or {}).get("reply_backlog") or []:
        lid = str((e or {}).get("id") or "")
        if lid in by_id and lid not in want and int(e.get("age_days") or 0) >= 1 and not e.get("kept"):
            want.append(lid)
    want = want[:HOSTED_ITEMS]
    try:
        levels = _mod("keep_gate").levels([by_id[lid] for lid in want])
    except Exception:
        levels = {lid: "local" for lid in want}
    zone = _zone()
    preps: dict[str, dict] = {}
    for lid in want:
        level = levels.get(lid, "local")
        if level not in ("summary", "full"):
            out["kept"] += 1
            continue
        row = by_id[lid]
        item = {"ref": f"item:{lid}", "title": row.get("description") or "", "kind": _kind_of(row)}
        preps[lid] = prepare(item["ref"], item, row, now=now, zone=zone, level=level, briefing=briefing)
        preps[lid]["packet"] = fit_packet(lambda shed, _i=item, _r=row, _l=level: build_packet(
            _i["ref"], _i, _r, level=_l, now=now, zone=zone, briefing=briefing,
            allowed=preps[lid]["allowed"], rules=preps[lid]["rules"], ledger=preps[lid]["ledger"],
            project=preps[lid]["project"], shed=max(1, shed)))
    out["items"] = len(preps)
    if not preps:
        out.update(ok=True, reason="nothing to rewrite")
        return out
    if dry_run:
        out.update(ok=True, reason="dry run", batches=len(_batches([p["packet"] for p in preps.values()],
                                                                    HOSTED_PROMPT_MAX)))
        return out
    store = store or Store()
    answered: dict[str, dict] = {}
    for batch in _batches([preps[lid]["packet"] for lid in preps], HOSTED_PROMPT_MAX):
        r = (call or HOSTED_BATCH)(hosted_prompt(batch))
        out["calls"] += 0 if r.get("reason") == "limit" and not r.get("usage") else 1
        u = r.get("usage") or {}
        out["tokens_in"] += int(u.get("in") or 0)
        out["tokens_out"] += int(u.get("out") or 0)
        out["cost_usd"] += float(u.get("cost_usd") or 0)
        if not r.get("ok"):
            out["reason"] = r.get("reason") or "error"
            continue
        obj = parse_answer(r.get("text") or "")
        for a in obj.get("items") or [] if isinstance(obj.get("items"), list) else []:
            if isinstance(a, dict) and str(a.get("ref") or "").startswith("item:"):
                answered.setdefault(str(a["ref"])[5:], dict(a, model=r.get("model") or ""))
    today = datetime.fromtimestamp(now, zone).date()
    for lid, a in answered.items():
        prep = preps.get(lid)
        if prep is None:
            continue
        got = check_answer(a, prep["packet"], allowed=prep["allowed"], today=today, due=prep["due"],
                           cand=prep["cand"])
        if not got["ok"]:
            out["refused"] += 1
            continue
        store.put(lid, {"version": row_version(by_id[lid]), "by": "hosted", "at": time.time(),
                        "level": prep["level"], "text": got["text"], "steps": got["steps"],
                        "model": _model_name(a.get("model") or "") , "note": ""})
        out["stored"] += 1
    out["ok"] = out["stored"] > 0
    out["seconds"] = round(time.monotonic() - t0, 1)
    out["cost_usd"] = round(out["cost_usd"], 4)
    _log({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "hosted-pass",
          **(record or {}), **out})
    return out


def spawn_hosted_pass(agent: str, run_id: str, briefing_path: str, *, popen=None) -> dict:
    """Start the hosted rewrite of a briefing run's list in a process of its
    own (`home_deeper.py hosted-pass`), detached, so the run's post never
    waits for the hosted call; its record is a line in home-deeper.jsonl
    with the run id. {"started": bool}. Never when the pre-send review is
    off: that switch keeps hosted calls out of the briefing."""
    import subprocess
    try:
        if not _mod("briefing_review").enabled():
            return {"started": False, "why": "the pre-send review is off"}
    except Exception:
        pass
    cmd = [sys.executable, str(Path(__file__).resolve()), "hosted-pass", "--agent", str(agent),
           "--run-id", str(run_id), "--briefing", str(briefing_path)]
    (popen or subprocess.Popen)(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return {"started": True}


def hosted_pass_for_run(agent: str, run_id: str, briefing_path: str) -> dict:
    """The CLI's hosted-pass: the list the run kept for Home
    (remote_briefing.home_handoff) and the briefing's data, then
    hosted_pass, its record one line with the run id."""
    rb = _mod("remote_briefing")
    view = rb.home_handoff(agent, run_id)
    if not view:
        return {"ok": False, "reason": "no list for Home from this run"}
    try:
        briefing = rb._load(briefing_path)
    except Exception:
        briefing = {}
    return hosted_pass(view.get("items") or [], briefing=briefing if isinstance(briefing, dict) else {},
                       record={"agent": agent, "run_id": run_id})


def _ask_hosted_batch(prompt: str) -> dict:
    """One tool-less hosted call for the day's list (hosted_text, the
    `review` task, as the pre-send review)."""
    br = _mod("briefing_review")
    try:
        ht = _mod("hosted_text")
        who = ht.model_for(HOSTED_TASK)
        if ht.is_base(who) and _plan_full():
            return {"ok": False, "text": "", "usage": {}, "reason": "limit", "model": who}
        model = _mod("ea_config").get_models().get("home_deeper") or HOSTED_MODEL
        r = ht.run(HOSTED_TASK, prompt, model, timeout=br.TIMEOUT_S)
    except Exception as e:
        return {"ok": False, "text": "", "usage": {}, "reason": br._why_failed(str(e)), "model": ""}
    u = r.get("usage") or {}
    usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens"), "cost_usd": r.get("cost_usd")}
    if r.get("rc") or not r.get("text"):
        return {"ok": False, "text": "", "usage": usage, "model": r.get("model_used") or "",
                "reason": br._why_failed(f"{r.get('stderr') or ''} {r.get('text') or ''}")}
    return {"ok": True, "text": str(r["text"]), "usage": usage, "reason": "", "model": r.get("model_used") or ""}


HOSTED_BATCH: Callable[[str], dict] = _ask_hosted_batch


def now_lines(row: dict | None, *, now: float, zone: ZoneInfo, briefing: dict | None = None) -> list[str]:
    """The lines that depend on the moment, added in code when the card is
    read and never stored: how long the other side has waited for him, and
    his next free time before the item is due (the briefing's free slots)."""
    row = row or {}
    brief = _brief(row)
    out = []
    since = _date(brief.get("last_inbound"))
    if since and brief.get("needs_reply", True) and not brief.get("user_replied_after_latest"):
        days = (datetime.fromtimestamp(now, zone).date() - since).days
        if days >= 1:
            out.append(f"They wrote {days} day{'s' if days != 1 else ''} ago and wait for your answer.")
    due = _date(row.get("deadline")) or _date(brief.get("deadline"))
    today = datetime.fromtimestamp(now, zone).date()
    if due and due >= today:
        free = _calendar(briefing or {}, now=now, zone=zone, until=due)["free_slots"]
        if free:
            out.append(f"Your next free time before it is due: {free[0]}.")
        out.append(f"It is due {'today' if due == today else day_words(due)}.")
    return out


_BRIEFING_CACHE: dict = {"mtime": None, "data": {}}


def _briefing_now() -> dict:
    """The briefing's data, read again only when its file changes."""
    try:
        H = _mod("home_screen")
        path = H._ea_dir() / "state" / "briefing-latest.json"
        mtime = path.stat().st_mtime
        if mtime != _BRIEFING_CACHE["mtime"]:
            text = _mod("resident_io").read_text(path)
            data = json.loads(text) if text else {}
            _BRIEFING_CACHE.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    except Exception:
        pass
    return _BRIEFING_CACHE["data"]


# ── the jobs ─────────────────────────────────────────────────────────

class Jobs:
    """The deeper briefings of one web front end: the store's record
    (`deeper`: {"jobs": {ref: job}, "day": {...}}), the jobs at work in
    this process, and the bounds."""

    def __init__(self, fe, *, clock: Callable[[], float] = time.time, background: bool = True) -> None:
        self.fe = fe
        self.clock = clock
        self.background = background
        self.lock = threading.RLock()
        self.running: set[str] = set()
        self.slots = threading.BoundedSemaphore(MAX_RUNNING)

    # the record

    def _load(self) -> dict:
        try:
            rec = self.fe.store.home_get(STORE_KEY)
        except Exception:
            rec = {}
        rec = rec if isinstance(rec, dict) else {}
        rec.setdefault("jobs", {})
        rec.setdefault("day", {})
        return rec

    def _save(self, rec: dict) -> None:
        jobs = rec.get("jobs") or {}
        if len(jobs) > JOBS_KEPT:
            for ref in sorted(jobs, key=lambda r: float(jobs[r].get("at") or jobs[r].get("started") or 0))[
                    :len(jobs) - JOBS_KEPT]:
                jobs.pop(ref, None)
        self.fe.store.home_set(STORE_KEY, rec)

    def job(self, ref: str) -> dict:
        with self.lock:
            return dict(self._load()["jobs"].get(ref) or {})

    def _put(self, ref: str, job: dict) -> None:
        with self.lock:
            rec = self._load()
            rec["jobs"][ref] = job
            self._save(rec)

    def today(self) -> str:
        return datetime.fromtimestamp(self.clock(), _zone()).date().isoformat()

    def day(self) -> dict:
        with self.lock:
            d = self._load()["day"]
            return d if d.get("date") == self.today() else {"date": self.today()}

    def _count(self, **add: int) -> None:
        with self.lock:
            rec = self._load()
            d = rec["day"] if rec["day"].get("date") == self.today() else {"date": self.today()}
            for k, v in add.items():
                d[k] = int(d.get(k) or 0) + int(v)
            rec["day"] = d
            self._save(rec)

    def cap(self) -> int:
        return daily_cap(self.fe)

    def spent(self) -> bool:
        return int(self.day().get("calls") or 0) >= self.cap()

    # the event

    def changed(self, ref: str) -> None:
        emit = getattr(self.fe.store, "home_item_emit", None)
        try:
            if emit is not None:
                emit(ref)
        except Exception as e:
            log.warning("home deeper: the home-item event did not go out: %s", e)


_JOBS: dict[int, Jobs] = {}
_JOBS_LOCK = threading.Lock()


def jobs_for(fe) -> Jobs:
    """The front end's Jobs (one per front end in this process)."""
    with _JOBS_LOCK:
        j = getattr(fe, "_home_deeper", None)
        if j is None:
            j = Jobs(fe)
            try:
                fe._home_deeper = j
            except Exception:
                _JOBS[id(fe)] = j
                j = _JOBS[id(fe)]
        return j


def daily_cap(fe=None) -> int:
    """The day's model calls at most (DAILY_CAP)."""
    return DAILY_CAP


def _level(item: dict, row: dict | None) -> str:
    """The item's keep level: the stricter of what Home wrote on it and the
    shared check on its row (keep_gate.levels); fails closed to `local`
    for anything from mail."""
    kg = _mod("keep_gate")
    item = item or {}
    said = next((v for v in (item.get("kept"), item.get("keep_level")) if kept(v)), "")
    if not said and item.get("kept") is True:
        said = "local"   # kept, with no level said: the strictest
    if not row:
        return kg.stricter(said) if said in kg.LEVELS else "full"
    try:
        found = kg.levels([row]).get(str(row.get("id") or ""), "local")
    except Exception:
        found = "local"
    return kg.stricter(found, said) if said in kg.LEVELS else found


def _public_step(s: dict) -> dict:
    """A step as the app draws it: its id and label, and its whole words
    (`detail`) when the label had to cut them."""
    out = {"id": s["id"], "label": s["label"]}
    words = next((s["params"].get(k) for k in ("say", "question", "ask") if s["params"].get(k)), "")
    if words and words not in s["label"]:
        out["detail"] = words
    return out


def _view(job: dict, item: dict | None) -> dict:
    out: dict = {"state": job.get("state") or "none"}
    if job.get("state") == "ready":
        out["text"] = job.get("text") or ""
        steps = offered(job.get("steps") or [], item)
        out["steps"] = [_public_step(s) for s in steps]
        if not kept(job.get("level")):
            out["steps"].append({"id": CONTINUE, "label": CONTINUE_LABEL})
        out["model"] = job.get("model") or ""
        out["at"] = job.get("at")
    if job.get("note"):
        out["note"] = job["note"]
    return out


# ── the hook: state, start, step, prewarm ────────────────────────────

def _stored(row: dict | None) -> dict | None:
    lid = str((row or {}).get("id") or "")
    if not lid:
        return None
    try:
        return Store().get(lid)
    except Exception as e:
        log.warning("home deeper: the stored briefings could not be read: %s", e)
        return None


def _current(step: dict, today: date) -> bool:
    """A stored step's date still makes sense today."""
    d = _date((step.get("params") or {}).get("date"))
    if not d:
        return True
    return d > today if step.get("kind") == "wait_until" else d >= today


def stored_view(rec: dict, item: dict | None, row: dict | None, *, now: float | None = None) -> dict:
    """A stored briefing as the card shows it: the moment's lines first
    (now_lines, code's, never stored), then the stored text; the steps the
    item offers now whose dates still make sense, and Continue; who wrote
    it (`by`: local or hosted) and when."""
    now = now if now is not None else time.time()
    zone = _zone()
    today = datetime.fromtimestamp(now, zone).date()
    lines = now_lines(row, now=now, zone=zone, briefing=_briefing_now())
    text = rec.get("text") or ""
    if lines:
        text = "Now\n" + "\n".join(f"- {x}" for x in lines) + ("\n\n" + text if text else "")
    steps = [s for s in offered(rec.get("steps") or [], item) if _current(s, today)]
    out = {"state": "ready", "text": text, "steps": [_public_step(s) for s in steps],
           "by": rec.get("by") or "local", "at": rec.get("at"), "model": rec.get("model") or ""}
    if not kept(rec.get("level")) and not item_kept(item):
        out["steps"].append({"id": CONTINUE, "label": CONTINUE_LABEL})
    if rec.get("note"):
        out["note"] = rec["note"]
    return out


def state(fe, ref: str, item: dict | None = None, *, row: dict | None = None) -> dict:
    """The item's `deeper` (the contract's shape) for GET /api/home/item: a
    pure read, never a model call. A stored briefing shows at once, with
    `stale: true` when the row or its thread changed since it was written
    (the app then asks for a new one); with none stored, the tap's job:
    `working`, `failed` (a job this process lost reads failed) or `none`."""
    try:
        j = jobs_for(fe)
        row = row if row is not None else read_row(ref)
        rec = _stored(row)
        if rec:
            out = stored_view(rec, item, row, now=j.clock())
            if rec.get("version") != row_version(row):
                out["stale"] = True
                if ref in j.running:
                    out["state"] = "working"
            return out
        job = j.job(ref)
        sig = signature(ref, item, row)
        if item_kept(item) and not local_ready():
            return {"state": "unavailable", "note": NOTE_KEPT_NO_LOCAL}
        if job and job.get("sig") == sig and job.get("state") != "stored":
            if job.get("state") == "working" and ref not in j.running:
                return {"state": "failed", "note": NOTE_STOPPED}
            return _view(job, item)
        if j.spent():
            return {"state": "unavailable", "note": NOTE_CAP.format(n=j.cap())}
        return {"state": "none"}
    except Exception as e:
        log.warning("home deeper: the state of %s could not be read: %s", ref, e)
        return {"state": "unavailable"}


def start(fe, ref: str, item: dict | None = None, *, row: dict | None = None,
          prewarm: bool = False) -> dict:
    """POST /api/home/deeper, the refresh the app asks for when the stored
    briefing is missing or stale: {ok, state, note?}. A stored briefing for
    the row and thread as they are now is served as it is; a job at work is
    left to finish; else one job starts on the owner's own model (route),
    within the bounds, and stores what it writes."""
    try:
        j = jobs_for(fe)
        row = row if row is not None else read_row(ref)
        sig = signature(ref, item, row)
        rec = _stored(row)
        if rec and rec.get("version") == row_version(row):
            return {"ok": True, "state": "ready"}
        if item_kept(item) and not local_ready():
            return {"ok": True, "state": "unavailable", "note": NOTE_KEPT_NO_LOCAL}
        with j.lock:
            job = j.job(ref)
            if job.get("sig") == sig and job.get("state") == "ready" and not row:
                return {"ok": True, "state": "ready"}
            if prewarm and job.get("sig") == sig:
                return {"ok": True, "state": job.get("state") or "none"}  # one try per version
            if ref in j.running:
                return {"ok": True, "state": "working"}
            if j.spent():
                return {"ok": True, "state": "unavailable", "note": NOTE_CAP.format(n=j.cap())}
            if prewarm and (j.running or int(j.day().get("prewarm") or 0) >= PREWARM_DAILY):
                return {"ok": True, "state": "none"}  # a tap goes first; one prewarm at a time
            if len(j.running) >= MAX_RUNNING + MAX_WAITING:
                return {"ok": True, "state": "failed", "note": NOTE_BUSY}
            j.running.add(ref)
            j._put(ref, {"ref": ref, "sig": sig, "state": "working", "started": j.clock(),
                         "prewarm": prewarm})
        j.changed(ref)
        item_ = dict(item or {})

        def work() -> None:
            try:
                with j.slots:
                    _work(j, ref, item_, row, sig, prewarm=prewarm)
            except Exception as e:
                log.exception("home deeper: the job for %s failed", ref)
                j._put(ref, {"ref": ref, "sig": sig, "state": "failed", "note": NOTE_FAILED,
                             "at": j.clock(), "error": str(e)[:200]})
            finally:
                with j.lock:
                    j.running.discard(ref)
                j.changed(ref)
        if j.background:
            threading.Thread(target=work, name="webchat-home-deeper", daemon=True).start()
            return {"ok": True, "state": "working"}
        work()
        got = j.job(ref)
        st = "ready" if got.get("state") == "stored" else (got.get("state") or "failed")
        return {"ok": True, "state": st, **({"note": got["note"]} if got.get("note") else {})}
    except Exception as e:
        log.warning("home deeper: the job for %s did not start: %s", ref, e)
        return {"ok": False, "state": "failed", "note": NOTE_FAILED}


def _sources() -> tuple[dict, str]:
    """(the briefing's data, the owner's rules) for a packet."""
    briefing: dict = {}
    try:
        H = _mod("home_screen")
        rio = _mod("resident_io")
        text = rio.read_text(H._ea_dir() / "state" / "briefing-latest.json")
        briefing = json.loads(text) if text else {}
    except Exception:
        briefing = {}
    try:
        rules = _mod("preferences").render(["briefing", "drafting"])
    except Exception:
        rules = ""
    return (briefing if isinstance(briefing, dict) else {}), rules


def prepare(ref: str, item: dict, row: dict | None, *, now: float, zone: ZoneInfo,
            level: str | None = None, ledger: dict | None = None, briefing: dict | None = None,
            rules: str | None = None, project: dict | None = None) -> dict:
    """Everything one call needs, read-only: {"level", "allowed", "packet",
    "prompt", "due", "cand"}. The live sources where a part is not given."""
    level = level if level is not None else _level(item, row)
    if row and row.get("owner_said") is None:
        try:
            _mod("ledger").with_owner_decisions([row])
        except Exception:
            pass
    allowed = allowed_kinds(item, row, level)
    if ledger is None and row:
        try:
            ledger = _mod("briefing_review").read_ledger([row.get("id")], [row.get("conversation_id")])
        except Exception:
            ledger = None
    if briefing is None or rules is None:
        b, r = _sources()
        briefing = b if briefing is None else briefing
        rules = r if rules is None else rules
    if project is None and row:
        project = _project(str(row.get("id") or ""))
    packet = fit_packet(lambda shed: build_packet(
        ref, item, row, level=level, now=now, zone=zone, ledger=ledger, briefing=briefing,
        project=project, rules=rules or "", allowed=allowed, shed=shed))
    due = _date((row or {}).get("deadline")) or _date(_brief(row or {}).get("deadline"))
    return {"level": level, "allowed": allowed, "packet": packet, "prompt": prompt_text(packet),
            "due": due, "cand": candidate(row, item), "rules": rules or "", "ledger": ledger,
            "project": project}


def _work(j: Jobs, ref: str, item: dict, row: dict | None, sig: str, *, prewarm: bool) -> None:
    if row and row.get("id"):
        _work_row(j, ref, item, row, sig, prewarm=prewarm)
        return
    t0 = time.monotonic()
    now = j.clock()
    zone = _zone()
    prep = prepare(ref, item, row, now=now, zone=zone)
    level = prep["level"]
    order = route(level)
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "ref": hashlib.sha256(ref.encode()).hexdigest()[:12], "level": level, "route": order,
           "prewarm": prewarm, "prompt_chars": len(prep["prompt"])}
    if prewarm and (not order or order[0] != "local"):
        # A prewarm never spends a hosted plan.
        j._put(ref, {"ref": ref, "sig": sig, "state": "none", "at": now})
        return
    if not order:
        j._put(ref, {"ref": ref, "sig": sig, "state": "unavailable", "note": NOTE_KEPT_NO_LOCAL,
                     "at": now, "level": level})
        _log(dict(rec, ok=False, why="kept, no local model"))
        return
    if prewarm:
        order = ["local"]
    res = run_model(prep["prompt"], order)
    j._count(calls=res["calls"], **{res["via"] or "failed": 1}, **({"prewarm": 1} if prewarm else {}))
    rec.update(via=res["via"], model=res["model"], tries=res["tries"], usage=res["usage"])
    if not res["ok"]:
        j._put(ref, {"ref": ref, "sig": sig, "state": "failed", "note": res["note"], "at": j.clock(),
                     "level": level})
        _log(dict(rec, ok=False, seconds=round(time.monotonic() - t0, 1)))
        return
    obj = parse_answer(res["text"])
    today = datetime.fromtimestamp(now, zone).date()
    got = check_answer(obj, prep["packet"], allowed=prep["allowed"], today=today, due=prep["due"],
                       cand=prep["cand"]) if obj else {"ok": False, "text": "", "steps": [],
                                                       "unchecked": 0, "rejected": []}
    rec.update(unchecked=got["unchecked"], rejected=got["rejected"], steps=[s["kind"] for s in got["steps"]],
               seconds=round(time.monotonic() - t0, 1))
    if not got["ok"]:
        j._put(ref, {"ref": ref, "sig": sig, "state": "failed", "note": NOTE_UNCHECKED, "at": j.clock(),
                     "level": level})
        _log(dict(rec, ok=False, why="unreadable" if not obj else "nothing checked"))
        return
    j._put(ref, {"ref": ref, "sig": sig, "state": "ready", "text": got["text"], "steps": got["steps"],
                 "model": res["model"], "via": res["via"], "note": res["note"], "at": j.clock(),
                 "level": level, "title": _one((item or {}).get("title"), 200),
                 "kind": (item or {}).get("kind") or "", "lid": str((row or {}).get("id") or "")})
    _log(dict(rec, ok=True))


def _work_row(j: Jobs, ref: str, item: dict, row: dict, sig: str, *, prewarm: bool) -> None:
    """A tap's (or a prewarm's) job for an item with a row: write its
    briefing (write_one) into the store, where the next read finds it."""
    now = j.clock()
    zone = _zone()
    prep = prepare(ref, item, row, now=now, zone=zone)
    order = route(prep["level"])
    if prewarm and (not order or order[0] != "local"):
        j._put(ref, {"ref": ref, "sig": sig, "state": "none", "at": now})  # a prewarm never spends a hosted plan
        return
    got = write_one(row, now=now, item=item, prepared=prep, order=["local"] if prewarm else order)
    if got.get("calls"):
        j._count(calls=got["calls"], **{got.get("via") or "failed": 1}, **({"prewarm": 1} if prewarm else {}))
    _log({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "prewarm" if prewarm else "tap",
          "ref": hashlib.sha256(ref.encode()).hexdigest()[:12], "level": got.get("level"), "route": order,
          "ok": got["ok"], "via": got.get("via"), "model": got.get("model"), "seconds": got.get("seconds"),
          "usage": got.get("usage"), "unchecked": got.get("unchecked"), "rejected": got.get("rejected"),
          "prompt_chars": got.get("prompt_chars"), "tries": got.get("tries")})
    if got["ok"]:
        j._put(ref, {"ref": ref, "sig": sig, "state": "stored", "at": j.clock()})
    elif not got.get("calls") and got.get("note") == NOTE_KEPT_NO_LOCAL:
        j._put(ref, {"ref": ref, "sig": sig, "state": "unavailable", "note": NOTE_KEPT_NO_LOCAL, "at": now})
    else:
        j._put(ref, {"ref": ref, "sig": sig, "state": "failed", "note": got.get("note") or NOTE_FAILED,
                     "at": j.clock()})


def _log(rec: dict) -> None:
    try:
        _mod("remote_llm").append(LOG_NAME, rec)
    except Exception as e:
        log.debug("home deeper: the log line was not kept: %s", e)


def step(fe, ref: str, step_id: str, item: dict | None = None, *, row: dict | None = None,
         arg: Any = None) -> dict:
    """A deeper step the owner tapped (POST /api/home/act, `step:<id>`):
    either {"choice", "arg"}, one of the item's own choices to run, or a
    finished {ok, said, goto?} for a step this module runs."""
    try:
        j = jobs_for(fe)
        row = row if row is not None else read_row(ref)
        rec = _stored(row)
        if rec:
            if rec.get("version") != row_version(row):
                return {"ok": False, "said": NOTE_STALE_STEP}
            job = {"state": "ready", "text": rec.get("text") or "", "level": rec.get("level") or "",
                   "lid": str(row.get("id") or ""), "title": (item or {}).get("title") or row.get("description"),
                   "steps": [s for s in rec.get("steps") or []
                             if _current(s, datetime.fromtimestamp(j.clock(), _zone()).date())]}
        else:
            job = j.job(ref)
        if not job or job.get("state") != "ready" or (not rec and job.get("sig") != signature(ref, item, row)):
            return {"ok": False, "said": NOTE_STALE_STEP}
        if step_id == CONTINUE:
            if kept(job.get("level")):
                return {"ok": False, "said": NOTE_STEP_GONE}
            return open_conversation(fe, ref, job, item, row)
        s = next((x for x in offered(job.get("steps") or [], item) if x["id"] == step_id), None)
        if s is None:
            return {"ok": False, "said": NOTE_STEP_GONE}
        p = s["params"]
        kind = s["kind"]
        if kind == "draft_reply":
            return {"choice": s["choice"], "arg": p["say"]}
        if kind == "ask_sender":
            return {"choice": s["choice"], "arg": f"Ask them: {p['question']}"}
        if kind == "propose_times":
            return {"choice": s["choice"], "arg": "Offer two or three times that work for me."}
        if kind == "wait_until":
            return {"choice": s["choice"], "arg": p["date"]}
        if kind == "mark_done":
            return {"choice": s["choice"], "arg": None}
        if kind in OWN_STEPS:
            if kept(job.get("level")):
                return {"ok": False, "said": NOTE_STEP_GONE}
            if kind == "work_on_it":
                ask = p["ask"]
            else:
                ask = (f"Hold {p['minutes']} minutes on {day_words(_date(p['date']))} on my calendar "
                       f"to work on this. Put no one else on the hold.")
            return open_conversation(fe, ref, job, item, row, send=ask)
        return {"ok": False, "said": NOTE_STEP_GONE}
    except Exception as e:
        log.warning("home deeper: the step %s on %s failed: %s", step_id, ref, e)
        return {"ok": False, "said": "Tacitus could not do that. Try again."}


def prewarm(fe, left: list[dict] | None) -> int:
    """Start the deeper briefing of the first PREWARM_TOP items of "Left for
    you" ahead of a tap, only where it runs on the owner's own model (never
    a hosted plan), within PREWARM_DAILY and the day's cap. Returns how
    many it started. Never raises."""
    n = 0
    try:
        if not local_ready():
            return 0
        for x in (left or [])[:PREWARM_TOP]:
            ref = str(x.get("ref") or x.get("id") or "")
            if not ref or x.get("kind") == "request":
                continue
            item = {"ref": ref, "kind": x.get("kind"), "title": x.get("title"), "kept": x.get("kept") or ""}
            got = start(fe, ref, item, prewarm=True)
            n += got.get("state") == "working"
    except Exception as e:
        log.warning("home deeper: the prewarm did not run: %s", e)
    return n


# ── a conversation from the briefing ─────────────────────────────────

def conversation_place(fe, row: dict | None) -> str:
    """Where the conversation opens: where Home's own Start it with Tacitus
    opens one (home_items._room: the project room of the item's row when
    rooms are on and it has one, else Ask Tacitus). Its answer is final, so
    Continue and Start never part ways (with rooms off, never a room). Only
    without home_items: the project room the row is linked to
    (briefing_rooms.rooms), else the EA channel."""
    lid = str((row or {}).get("id") or "")
    main = str(getattr(fe, "main_channel", "") or "main")
    try:
        hi = _mod("home_items")
    except ImportError:
        hi = None
    if hi is not None:
        try:
            conv = str((row or {}).get("conversation_id") or _brief(row or {}).get("conversation_id") or "")
            return str(hi._room(fe, hi.Found(item={}, lid=lid, conv=conv)) or main)
        except Exception as e:
            log.warning("home deeper: Home's room rule failed, so Ask Tacitus: %s", e)
            return main
    proj = _project(lid) if lid else None
    if proj:
        try:
            room = _mod("briefing_rooms").rooms().get(proj["id"])
            if room and room.get("channel"):
                return str(room["channel"])
        except Exception:
            pass
    return main


def seed_text(job: dict, item: dict | None, row: dict | None) -> str:
    """The first message of the conversation, from Tacitus: the item's
    name on the first line (it titles the thread, as briefing_rooms names a
    post), the briefing, the steps it saw, and the row it is about."""
    title = (item or {}).get("title") or job.get("title") or ""
    try:
        br = _mod("briefing_rooms")
        name = br.conversation_name([{"section": "top", "title": title, "line": title}],
                                    lambda t, cap: _one(t, cap))
    except Exception:
        name = _one(title, 60)
    lines = [f"*{name or 'An item from Home'}*", job.get("text") or ""]
    steps = [s["label"] for s in offered(job.get("steps") or [], item)]
    if steps:
        lines += ["", "Next steps Tacitus saw: " + "; ".join(steps) + "."]
    lid = str((row or {}).get("id") or job.get("lid") or "")
    lines += ["", "From Home" + (f" (item {lid})" if lid else "") + ". Write here what to do next."]
    return "\n".join(lines)


def open_conversation(fe, ref: str, job: dict, item: dict | None, row: dict | None, *,
                      send: str = "") -> dict:
    """Open a conversation seeded with the briefing as Tacitus's first
    message; with `send`, the owner's words follow it and the turn starts.
    {ok, said, goto}. Never for a kept item."""
    if kept(job.get("level")) or item_kept(item):
        return {"ok": False, "said": NOTE_STEP_GONE}
    channel = conversation_place(fe, row)
    ref_ = fe.post(channel, seed_text(job, item, row))
    key = getattr(ref_, "thread_key", None) or ""
    if not key:
        try:
            key = fe.store.message(ref_.channel, ref_.id)["thread_key"]
        except Exception:
            key = getattr(ref_, "id", "")
    channel = getattr(ref_, "channel", channel) or channel
    goto = f"#/{quote(str(channel), safe='')}/{quote(str(key), safe='')}"
    if not send:
        return {"ok": True, "said": "Opened a conversation with the briefing.", "goto": goto}
    _row, dispatch = fe.web_message(channel, send, key)
    if dispatch is not None:
        threading.Thread(target=dispatch, name="webchat-home-deeper-turn", daemon=True).start()
    return {"ok": True, "said": "Tacitus started on it in a conversation.", "goto": goto}


# ── the CLI ──────────────────────────────────────────────────────────

def usage(days: int = 7) -> dict:
    """The calls of the last `days` days from the log, by day and route."""
    try:
        recs = _mod("remote_llm").read(LOG_NAME)
    except Exception:
        recs = []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for r in recs:
        day = str(r.get("ts") or "")[:10]
        if day < since:
            continue
        d = out.setdefault(day, {"jobs": 0, "ok": 0, "local": 0, "hosted": 0, "prewarm": 0, "seconds": 0.0,
                                 "tokens_in": 0, "tokens_out": 0})
        d["jobs"] += 1
        d["ok"] += bool(r.get("ok"))
        d[r.get("via") or "hosted"] = d.get(r.get("via") or "hosted", 0) + (1 if r.get("via") else 0)
        d["prewarm"] += bool(r.get("prewarm"))
        d["seconds"] += float(r.get("seconds") or 0)
        u = r.get("usage") or {}
        d["tokens_in"] += int(u.get("in") or 0)
        d["tokens_out"] += int(u.get("out") or 0)
    return {"cap": DAILY_CAP, "days": out}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage", help="today's and recent calls, by route")
    u.add_argument("--days", type=int, default=7)
    p = sub.add_parser("packet", help="the packet for one item, read-only")
    p.add_argument("--ref", required=True)
    p.add_argument("--kind", default="")
    h = sub.add_parser("hosted-pass", help="the hosted rewrite of a briefing run's list (the run starts it)")
    h.add_argument("--agent", required=True)
    h.add_argument("--run-id", required=True)
    h.add_argument("--briefing", required=True)
    b = sub.add_parser("backfill", help="write the missing or stale briefings of the open rows")
    b.add_argument("--limit", type=int, default=20, help="rows this run writes at most (default 20)")
    b.add_argument("--dry-run", action="store_true", help="count the rows that need one; write nothing")
    a = ap.parse_args(argv)
    if a.cmd == "usage":
        print(json.dumps(usage(a.days), indent=1))
        return 0
    if a.cmd == "hosted-pass":
        print(json.dumps(hosted_pass_for_run(a.agent, a.run_id, a.briefing)))
        return 0
    if a.cmd == "backfill":
        got = refresh(limit=max(0, a.limit), dry_run=a.dry_run, daily=None)
        got["left"] = max(0, got["stale"] - got["written"])
        print(json.dumps(got, indent=1))
        return 0
    row = read_row(a.ref)
    kind = a.kind or ("reply" if a.ref.startswith("reply:") else "item")
    item = {"ref": a.ref, "kind": kind, "title": (row or {}).get("description") or ""}
    prep = prepare(a.ref, item, row, now=time.time(), zone=_zone())
    print(json.dumps({"level": prep["level"], "allowed": prep["allowed"], "route": route(prep["level"]),
                      "chars": len(prep["prompt"]), "packet": prep["packet"]}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
