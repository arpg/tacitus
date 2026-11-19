#!/usr/bin/env python3
"""ghost_week: the week before a cutover, measured, and the week after.

Two cutovers take a lane off Claude. `mail = sealed` makes every lane's
mail read return the remote model's record of each message instead of
its text; `scheduling = enforce` makes the remote agent answer the
scheduling channel. Each has a ghost value that changes nothing the user
sees but records what the cutover would have done: `mail = ghost` seals
every read on the side, after the lane has its text; `scheduling =
shadow` runs the agent beside Claude. This module turns those records
into one signal.

For mail, each read a lane made is paired with what sealed mail would
have returned (`record_read`), and a judge is shown the text the lane
saw, the record it would have seen, and what the lane then did (its
reply, the drafts it wrote, the events it staged). It names each fact
the turn used that the record lacks, each field of the record that
contradicts the text, and a verdict: `same`, `degraded` or `blocked`.
For scheduling, each shadow turn is graded against the request, the
solver's windows and the scheduling rules; Claude's reply is shown as a
reference, not as the truth, and the user's own Create and Reschedule
buttons stay the strongest signal (did the agent name the time the user
chose). Counters that need no judge (seal failures, unread messages,
latency, invented times, errors) come from the records themselves.

The week ends in a verdict per part: GO, NO-GO or NOT ENOUGH DATA,
against the pass levels in MAIL_PASS and SCHED_PASS, with the judge's
enhancement lines ranked so the next change to the extractor's schema or
the agent's skill is named, not guessed. `remote_llm.py set mail sealed`
and `set scheduling enforce` refuse without a GO unless forced. The daily
mail line leads with the record's error rate by field, the measure of the
on-prem record every lane reads, and leaves the sealed verdict to
`report` and `final`.

After a cutover the records keep coming (sealed reads carry
`served: sealed`; live turns carry `live: true`), the judge for sealed
reads runs on the remote model (the text never went to Claude), and the
same report shows the week after beside the week before. A tripwire in
the ten-minute tick posts to the health channel when sealed reads fail
or come back unread in bulk.

    status                       the windows, counts, the last verdicts
    report [--part P] [--json]   the numbers for the window (both parts)
    grade [--part P] [--cap N] [--judge claude|remote]
                                 judge the ungraded reads and turns (owner cron)
    final --part P               write the final report and verdict for the part
    purge-raw                    delete the stored read texts (ghost-raw/)

The store is the private remote-llm directory. `ghost-reads.jsonl` holds
counts and ids, never text; `ghost-raw/<id>.json` holds one read's text
and its sealed copy until the judge has seen it (then it is deleted), or
for RAW_TTL_DAYS at most. `ghost-turns.jsonl` holds the bot's turns (the
user's message, the reply, the tools) while a ghost or cutover value is
set, so a read can be joined to what the turn did.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm  # noqa: E402

READS = "ghost-reads.jsonl"
TURNS = "ghost-turns.jsonl"
MAIL_GRADES = "ghost-mail-grades.jsonl"
SCHED_GRADES = "ghost-sched-grades.jsonl"
RAW_DIR = "ghost-raw"
STATE = "ghost-state.json"
FINAL_JSON = "ghost-final-{part}.json"
FINAL_MD = "ghost-final-{part}.md"
SCHED_TURNS = "sched-turns.jsonl"
SCHED_OUTCOMES = "event-outcomes.jsonl"

PARTS = ("mail", "scheduling")
RAW_CAP = 200_000
RAW_TTL_DAYS = 14
MAIL_GRADE_CAP = 30
SCHED_GRADE_CAP = 20
JUDGE_MODEL = "sonnet"
PROMPT_RAW_CHARS = 24_000
PROMPT_MSG_CHARS = 2_500
PROMPT_SEALED_CHARS = 12_000
PROMPT_ARTIFACT_CHARS = 8_000
FINAL_MAX_AGE_DAYS = 14
TRIPWIRE_EVERY_S = 6 * 3600
TRIPWIRE_WINDOW_S = 3600

# Pass levels. A ghost week ends GO only when every check holds; NOT
# ENOUGH DATA when the judged count is under its minimum.
MAIL_PASS = {
    "judged_min": 30,
    "blocked_share_max": 0.05,
    "critical_error_share_max": 0.05,
    "seal_failure_share_max": 0.01,
    "unread_share_max": 0.05,
    "seal_p95_max_s": 60.0,
}
SCHED_PASS = {
    "judged_min": 15,
    "invented_times_max": 0,
    "wrong_share_max": 0.10,
    "send_share_min": 0.60,
    "error_share_max": 0.10,
    "latency_p95_max_s": 120.0,
    "outcome_named_min": 0.80,
    "outcome_events_min": 3,
}
CRITICAL_FIELDS = ("deadline", "offered_times", "people", "needs_reply", "ask")
FACT_CATEGORIES = ("url", "number", "date", "time", "name", "quote", "attachment",
                   "question", "decision", "commitment", "nuance", "other")
PROBLEM_KINDS = ("wrong-time", "missed-request", "wrong-mode", "rule-break", "wrong-write",
                 "missing-write", "wording", "tool-misuse", "other")

# A reply that asks the user for what sealed mail withheld.
ASKED_FOR_TEXT = re.compile(
    r"(?:raw|full|original|complete) (?:text|body|email|message)|paste (?:the|that|me) "
    r"(?:email|message|thread)|could not read (?:the|that) (?:message|email|thread)|"
    r"remote_unread|not (?:yet )?been read by|has not read (?:it|that|this) yet", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(s: str) -> date | None:
    dt = _parse(s)
    return dt.astimezone().date() if dt else None


def _pct(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def lane_of(run_id: str) -> str:
    """cron (a run-agent lane), shadow (the shadow agent's own reads),
    bot (a room, thread, DM or scheduling-channel turn) or none (a
    shell, with no run id)."""
    rid = str(run_id or "")
    if rid.startswith("cron-"):
        return "cron"
    if rid.startswith("shadow-"):
        return "shadow"
    return "bot" if rid else "none"


# ── windows ───────────────────────────────────────────────────────────

def window(part: str, cfg: dict | None = None, today: date | None = None) -> tuple[date, date]:
    """(from, until) of the part's ghost week; without a window in the
    config (`remote_llm.py set ... --days`), the seven days ending today."""
    cfg = cfg if cfg is not None else remote_llm.config()
    today = today or date.today()
    frm, until = remote_llm.ghost_window(part, cfg)
    if frm and until:
        return frm, until
    return today - timedelta(days=remote_llm.GHOST_DAYS - 1), today


def day_number(part: str, cfg: dict | None = None, today: date | None = None) -> tuple[int, int]:
    frm, until = window(part, cfg, today)
    today = today or date.today()
    return (today - frm).days + 1, (until - frm).days + 1


def _in_window(ts: str, frm: date, until: date) -> bool:
    d = _day(ts)
    return d is not None and frm <= d <= until


# ── the reads ─────────────────────────────────────────────────────────

def _rows(verb: str, data) -> list[dict]:
    if isinstance(data, dict) and data.get("error"):
        return []
    if verb == "get-message" and isinstance(data, dict):
        return [data]
    if verb == "get-conversation" and isinstance(data, dict):
        msgs = data.get("messages")
        return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict) and m.get("id")]
    return []


def _has_text(m: dict) -> bool:
    return bool(m.get("text") or m.get("body") or m.get("body_text"))


def _raw_path(read_id: str) -> Path:
    return remote_llm.store_dir() / RAW_DIR / f"{read_id}.json"


def record_read(verb: str, argv: list, run_id: str, raw_text: str, *, served: str,
                sealed=None, seal_s: float | None = None, seal_error: str = "",
                index: dict | None = None, now: datetime | None = None) -> dict | None:
    """Record one mail read: what the lane got (`served`: text or sealed),
    the sealed copy, how long sealing took, and how many messages the
    remote model had not read. Counts go to the reads file; the text and
    the sealed copy go to one raw file for the judge. Returns the record,
    or None for a read that carries no messages."""
    try:
        data = json.loads(raw_text or "")
    except ValueError:
        data = None
    rows = _rows(verb, data)
    if not rows:
        return None
    if index is None:
        import mail_privacy
        index = mail_privacy.load_index()
    text_rows = [m for m in rows if _has_text(m)]
    unread_before = sum(1 for m in text_rows if index["by_id"].get(m.get("id") or "") is None)
    sealed_rows = _rows(verb, sealed) if sealed is not None else []
    # Unread is counted over the messages that carried text: a search or
    # list row has none, so the record store's view of it says nothing
    # about what the seal budget left unread.
    text_ids = {str(m.get("id") or "") for m in text_rows}
    unread_after = (sum(1 for m in sealed_rows
                        if m.get("remote_unread") and str(m.get("id") or "") in text_ids)
                    if sealed is not None else None)
    convs = sorted({str(m.get("conversation_id") or m.get("conversationId") or
                        (index["by_id"].get(m.get("id") or "") or {}).get("conversation_id") or "")
                    for m in rows} - {""})
    now = now or _now()
    rec = {"id": f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}", "ts": _iso(now),
           "verb": verb, "run_id": str(run_id or "")[:64], "lane": lane_of(run_id),
           "served": served, "messages": len(rows), "text_messages": len(text_rows),
           "conversations": convs[:20], "unread_before": unread_before,
           "unread_after": unread_after,
           "inline_extracted": (max(0, unread_before - unread_after) if unread_after is not None else 0),
           "seal_s": round(seal_s, 1) if seal_s is not None else None,
           "seal_error": str(seal_error or "")[:200],
           "raw_bytes": len(raw_text or ""),
           "sealed_bytes": len(json.dumps(sealed, default=str)) if sealed is not None else None}
    remote_llm.append(READS, rec)
    try:
        d = remote_llm.store_dir() / RAW_DIR
        d.mkdir(mode=0o700, exist_ok=True)
        _owner_trust.write_owner_file(_raw_path(rec["id"]), json.dumps({
            "id": rec["id"], "verb": verb, "raw": (raw_text or "")[:RAW_CAP],
            "sealed": sealed}, default=str).encode(), mode=0o600)
    except Exception as e:
        print(f"ghost_week: raw file: {str(e)[:120]}", file=sys.stderr)
    return rec


def ghost_read(verb: str, argv: list, run_id: str, stdout: str) -> dict | None:
    """Seal one served read on the side and record both. Called in a
    thread of its own by `ghost_read_async`, after the lane has its
    answer, so the read is never slower for it."""
    if lane_of(run_id) == "shadow":
        return None
    import mail_privacy
    try:
        data = json.loads(stdout or "")
    except ValueError:
        return None
    if not _rows(verb, data):
        return None
    index = mail_privacy.load_index()
    cfg = remote_llm.config()
    t0 = time.monotonic()
    sealed, err = None, ""
    try:
        sealed = mail_privacy.seal_output(verb, json.loads(stdout), index=index, cfg=cfg)
    except Exception as e:
        err = str(e)[:200]
    return record_read(verb, argv, run_id, stdout, served="text", sealed=sealed,
                       seal_s=time.monotonic() - t0, seal_error=err, index=index)


def ghost_read_async(verb: str, argv: list, run_id: str, stdout: str) -> threading.Thread:
    def work():
        try:
            ghost_read(verb, list(argv), run_id, stdout)
        except Exception as e:
            print(f"ghost_week.ghost_read: {str(e)[:160]}", file=sys.stderr)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


# ── the turns ─────────────────────────────────────────────────────────

def recording(cfg: dict | None = None) -> bool:
    """True while a ghost or cutover value is set for either part."""
    cfg = cfg if cfg is not None else remote_llm.config()
    return (remote_llm.mode("mail", cfg) in ("ghost", "sealed")
            or remote_llm.mode("scheduling", cfg) in ("shadow", "enforce"))


def note_turn(rec: dict) -> None:
    """The bot's record of one finished turn: its run id, room, the
    user's message, the reply and the tool lines. A no-op unless a ghost
    or cutover value is set; never raises."""
    try:
        if not recording() or not isinstance(rec, dict) or not rec.get("run_id"):
            return
        row = {"ts": _iso(_now()), "run_id": str(rec.get("run_id"))[:64],
               "channel": str(rec.get("channel") or "")[:80],
               "thread_ts": str(rec.get("thread_ts") or "")[:200],
               "room": str(rec.get("room") or "")[:80], "lane": str(rec.get("lane") or "")[:30],
               "model": str(rec.get("model") or "")[:60],
               "user_msg": str(rec.get("user_msg") or "")[:4000],
               "reply": str(rec.get("reply") or "")[:6000],
               "tools": [str(t)[:300] for t in (rec.get("tools") or [])][:40],
               "asked_for_text": bool(ASKED_FOR_TEXT.search(str(rec.get("reply") or "")))}
        remote_llm.append(TURNS, row)
    except Exception as e:
        print(f"ghost_week.note_turn: {str(e)[:120]}", file=sys.stderr)


def _journal_drafts(run_id: str) -> list[str]:
    try:
        import draft_events
        ids = []
        for ev in draft_events.read_since(0):
            if ev.get("run_id") == run_id and ev.get("action") in ("created", "updated"):
                if ev.get("draft_id") and ev["draft_id"] not in ids:
                    ids.append(ev["draft_id"])
        return ids
    except Exception:
        return []


def _draft_bodies(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    try:
        import draft_capture
        want = set(ids)
        return [{"to": r.get("to") or [], "subject": r.get("subject") or "",
                 "body": str(r.get("body") or "")[:4000]}
                for r in draft_capture.load_all() if r.get("draft_id") in want]
    except Exception:
        return []


def _sched_turn(run_id: str) -> dict | None:
    for t in remote_llm.read(SCHED_TURNS):
        if t.get("run_id") == run_id:
            return t
    return None


def turn_artifacts(run_id: str) -> dict:
    """What the turn did: its posted reply, the drafts it wrote (bodies
    from the draft capture), and a scheduling turn's writes."""
    turn = None
    for t in remote_llm.read(TURNS):
        if t.get("run_id") == run_id:
            turn = t
    st = _sched_turn(run_id)
    return {"turn": turn, "drafts": _draft_bodies(_journal_drafts(run_id)),
            "sched_writes": (st or {}).get("writes") or []}


# ── the judge ─────────────────────────────────────────────────────────

MAIL_JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "facts_used": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"fact": {"type": "string"}, "in_record": {"type": "boolean"},
                           "category": {"type": "string", "enum": list(FACT_CATEGORIES)}},
            "required": ["fact", "in_record", "category"]}},
        "record_errors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"field": {"type": "string"}, "record": {"type": "string"},
                           "thread": {"type": "string"}},
            "required": ["field", "record", "thread"]}},
        "verdict": {"type": "string", "enum": ["same", "degraded", "blocked"]},
        "enhancement": {"type": "string"},
    },
    "required": ["facts_used", "record_errors", "verdict", "enhancement"],
}

MAIL_JUDGE_PROMPT = """You grade one mail read for an assistant that is moving from reading email text to reading a model's record of each message. Answer with JSON only, in this shape:
{{"facts_used": [{{"fact": "...", "in_record": true, "category": "url"}}], "record_errors": [{{"field": "deadline", "record": "...", "thread": "..."}}], "verdict": "same", "enhancement": "..."}}

The read: `{verb}` in a {lane} lane.

A. What the assistant saw (the text):
{raw}

B. What it would see instead (the record):
{sealed}

C. What the assistant then did in that turn (its reply, the drafts it wrote, the events it staged). Empty when nothing is on file:
{artifacts}

1. facts_used: each fact from A that C relies on, or that the assistant needed to produce C. One short line each. in_record is true when B states it or gives enough to infer it. category is one of: {categories}. When C is empty, list the facts an assistant would need to act on the latest message in A, at most 6.
2. record_errors: each place where B contradicts A. field is one of: ask, deadline, offered_times, people, state, needs_reply, summary, scheduling, other. record is B's value, thread is what A says, both short. A field B leaves empty that A supports is a contradiction only when the field is deadline, offered_times or needs_reply.
3. verdict: "same" when C could have been produced from B alone; "degraded" when C would lose detail or precision but stay correct; "blocked" when C could not be produced from B, or B would lead to a wrong action.
4. enhancement: one line naming what the record should carry, or how a field should change, to close the largest gap. "" when there is none."""

SCHED_JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "shadow_grade": {"type": "string", "enum": ["send", "edit", "wrong", "none"]},
        "claude_grade": {"type": "string", "enum": ["send", "edit", "wrong", "none"]},
        "better": {"type": "string", "enum": ["shadow", "claude", "same"]},
        "problems": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": list(PROBLEM_KINDS)},
                           "note": {"type": "string"}},
            "required": ["kind", "note"]}},
        "enhancement": {"type": "string"},
    },
    "required": ["shadow_grade", "claude_grade", "better", "problems", "enhancement"],
}

SCHED_JUDGE_PROMPT = """You grade one scheduling turn for a professor's assistant. Two answers exist: Claude's (the live reply the user saw) and a remote agent's (recorded, never posted). The agent will take over the scheduling channel only if its answers are as good. Claude's reply is a reference, not the truth: grade each side against the request, the solver's windows and the rules. Answer with JSON only, in this shape:
{{"shadow_grade": "send", "claude_grade": "edit", "better": "same", "problems": [{{"kind": "wording", "note": "..."}}], "enhancement": "..."}}

The rules that matter: times come only from the solver's options below (any other time is invented); a poll reply goes to the sender only; weekday and date must agree; no em-dash; the assistant never declines a meeting on the user's behalf, never sends anything, never names tools; a real event or a reply to the requester waits for the user's choice, a hold on the EA-Managed calendar does not.

The request: {user_msg}

Context the assistant had:
{task_block}

The solver's options this turn (start, end):
{solver_options}

Code checks (invented = times the agent named that the solver did not return; validity = checked against the live calendar):
{checks}

CLAUDE replied:
{claude_reply}
Claude's drafts and staged events:
{claude_writes}

THE AGENT replied:
{shadow_reply}
The agent's writes ({writes_note}):
{writes}

1. shadow_grade: "send" (the user could post it as is), "edit" (right action, a detail or the wording to fix), "wrong" (wrong action, a wrong or invented time, the request missed, a rule broken), "none" (no reply).
2. claude_grade: the same for Claude's reply.
3. better: which side answered the request better, or "same".
4. problems: each problem in the agent's turn. kind is one of: {kinds}. note is one line.
5. enhancement: one line naming the change to the agent's skill text, tools or prompt that would fix the largest problem. "" when there is none."""


def _parse_json(text: str) -> dict:
    t = str(text or "")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON object in the judge's answer")
    return json.loads(t[a:b + 1])


def _judge(prompt: str, schema: dict, *, judge: str, runner=None) -> tuple[dict, dict]:
    """(answer, meta). `claude` runs the judge model as ea-bot with no
    tools; `remote` runs the remote model with the schema."""
    if runner is not None:
        return runner(prompt, schema, judge)
    if judge == "remote":
        obj, meta = remote_llm.chat_json([{"role": "user", "content": prompt}],
                                         purpose="ghost-judge", schema=schema)
        return obj, {"cost_usd": None, "latency_ms": meta.get("latency_ms")}
    import mail_privacy
    r = mail_privacy._run_claude(prompt, JUDGE_MODEL, "")
    if r.get("rc") or not r.get("text"):
        raise RuntimeError(f"judge failed (rc={r.get('rc')}): {r.get('stderr', '')[-160:]}")
    return _parse_json(r["text"]), {"cost_usd": r.get("cost_usd")}


def _render_raw(verb: str, raw_text: str) -> str:
    try:
        data = json.loads(raw_text or "")
    except ValueError:
        return (raw_text or "")[:PROMPT_RAW_CHARS]
    rows = _rows(verb, data)
    out = []
    for m in rows:
        frm = m.get("from")
        if isinstance(frm, dict):
            frm = frm.get("address") or (frm.get("emailAddress") or {}).get("address") or ""
        text = m.get("text") or m.get("body") or m.get("body_text") or m.get("body_preview") or \
            m.get("preview") or ""
        head = (f"From: {frm} | To: {', '.join(str(a) for a in (m.get('to') or [])[:6])} | "
                f"Date: {m.get('received') or m.get('sent') or ''} | Subject: {m.get('subject') or ''}"
                f"{' | attachments' if m.get('has_attachments') else ''}")
        out.append(head + "\n" + str(text)[:PROMPT_MSG_CHARS])
    return ("\n---\n".join(out))[:PROMPT_RAW_CHARS]


def _render_sealed(sealed) -> str:
    if sealed is None:
        return "(sealing failed; no record)"
    s = json.dumps(sealed, indent=1, default=str)
    s = re.sub(r'\n\s*"_note": ".*?",?', "", s)
    return s[:PROMPT_SEALED_CHARS]


def _render_artifacts(art: dict) -> str:
    out = []
    t = art.get("turn") or {}
    if t.get("user_msg"):
        out.append("THE USER ASKED: " + str(t["user_msg"])[:1500])
    if t.get("reply"):
        out.append("REPLY POSTED: " + str(t["reply"])[:3000])
    for d in art.get("drafts") or []:
        out.append(f"DRAFT to {', '.join(str(a) for a in d.get('to') or [])} re {d.get('subject')}:\n"
                   f"{d.get('body')}")
    for w in art.get("sched_writes") or []:
        out.append(f"WRITE {w.get('tool')}: {json.dumps(w.get('args'), default=str)[:800]}")
    return ("\n\n".join(out))[:PROMPT_ARTIFACT_CHARS]


def _pick_mail_reads(reads: list[dict], graded: set[str], now: datetime) -> list[list[dict]]:
    """Groups of ungraded reads, one group per (run, threads), each
    group's reads oldest first; groups with a turn on file come first."""
    turns = {t.get("run_id") for t in remote_llm.read(TURNS)}
    cutoff = now - timedelta(days=RAW_TTL_DAYS)
    groups: dict[tuple, list[dict]] = {}
    for r in reads:
        if r["id"] in graded or r.get("lane") not in ("bot", "cron") or r.get("seal_error"):
            continue
        if r.get("served") not in ("text", "sealed") or not _raw_path(r["id"]).exists():
            continue
        if (_parse(r.get("ts")) or now) < cutoff:
            continue
        key = (r.get("run_id"), tuple(r.get("conversations") or [r["id"]]))
        groups.setdefault(key, []).append(r)
    rank = {"get-conversation": 0, "get-message": 1, "search-mail": 2, "list-mail": 3}

    def score(g):
        best = min(rank.get(r["verb"], 9) for r in g)
        return (0 if g[0].get("run_id") in turns else 1, best, g[0]["ts"])
    return sorted(groups.values(), key=score)


def grade_mail(*, cap: int = MAIL_GRADE_CAP, judge: str = "auto", runner=None,
               now: datetime | None = None) -> dict:
    """Judge the ungraded reads, one per turn and thread (the fullest
    read of the group), up to `cap`; the group's other reads are marked
    as covered. Every raw file the judge has seen, or that is older than
    RAW_TTL_DAYS, is deleted."""
    now = now or _now()
    reads = remote_llm.read(READS)
    # A read whose judge call failed is tried again while its raw file lasts.
    graded = {g.get("read_id") for g in remote_llm.read(MAIL_GRADES)
              if g.get("verdict") or g.get("covered_by")}
    out = {"judged": 0, "covered": 0, "errors": 0, "verdicts": {}, "cost_usd": 0.0}
    for group in _pick_mail_reads(reads, graded, now)[:cap]:
        rank = {"get-conversation": 0, "get-message": 1, "search-mail": 2, "list-mail": 3}
        main = min(group, key=lambda r: (rank.get(r["verb"], 9), -r.get("text_messages", 0)))
        try:
            raw = json.loads(_raw_path(main["id"]).read_text())
        except Exception as e:
            remote_llm.append(MAIL_GRADES, {"ts": _iso(now), "read_id": main["id"],
                                            "run_id": main.get("run_id"), "error": f"raw: {str(e)[:120]}"})
            out["errors"] += 1
            continue
        art = turn_artifacts(main.get("run_id") or "")
        which = judge if judge in ("claude", "remote") else ("remote" if main.get("served") == "sealed" else "claude")
        prompt = MAIL_JUDGE_PROMPT.format(
            verb=main["verb"], lane=main.get("lane"), raw=_render_raw(main["verb"], raw.get("raw", "")),
            sealed=_render_sealed(raw.get("sealed")), artifacts=_render_artifacts(art) or "(none)",
            categories=", ".join(FACT_CATEGORIES))
        rec = {"ts": _iso(now), "read_id": main["id"], "run_id": main.get("run_id"),
               "lane": main.get("lane"), "verb": main["verb"], "served": main.get("served"),
               "judge": which, "has_artifacts": bool(art.get("turn") or art.get("drafts") or art.get("sched_writes")),
               "conversations": main.get("conversations")}
        try:
            ans, meta = _judge(prompt, MAIL_JUDGE_SCHEMA, judge=which, runner=runner)
            facts = [f for f in ans.get("facts_used") or [] if isinstance(f, dict)]
            errors = [e for e in ans.get("record_errors") or [] if isinstance(e, dict)]
            rec.update({
                "verdict": ans.get("verdict") if ans.get("verdict") in ("same", "degraded", "blocked") else "degraded",
                "facts": len(facts),
                "facts_missing": [{"fact": str(f.get("fact"))[:200],
                                   "category": f.get("category") if f.get("category") in FACT_CATEGORIES else "other"}
                                  for f in facts if not f.get("in_record")][:12],
                "record_errors": [{"field": str(e.get("field"))[:30], "record": str(e.get("record"))[:160],
                                   "thread": str(e.get("thread"))[:160]} for e in errors][:12],
                "critical_errors": sum(1 for e in errors if str(e.get("field")) in CRITICAL_FIELDS),
                "enhancement": str(ans.get("enhancement") or "")[:300],
                "cost_usd": meta.get("cost_usd")})
            out["judged"] += 1
            out["verdicts"][rec["verdict"]] = out["verdicts"].get(rec["verdict"], 0) + 1
            out["cost_usd"] += float(meta.get("cost_usd") or 0)
        except Exception as e:
            rec["error"] = str(e)[:200]
            out["errors"] += 1
        remote_llm.append(MAIL_GRADES, rec)
        if rec.get("error"):
            continue
        for r in group:
            if r["id"] != main["id"]:
                remote_llm.append(MAIL_GRADES, {"ts": _iso(now), "read_id": r["id"],
                                                "run_id": r.get("run_id"), "covered_by": main["id"]})
                out["covered"] += 1
        for r in group:
            try:
                _raw_path(r["id"]).unlink()
            except OSError:
                pass
    out["raw_purged"] = purge_raw(older_than_days=RAW_TTL_DAYS, now=now)
    out["cost_usd"] = round(out["cost_usd"], 3)
    return out


def _sched_candidates(turns: list[dict], graded: set[str], now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=RAW_TTL_DAYS)
    out = []
    for t in turns:
        if t.get("run_id") in graded or not t.get("run_id"):
            continue
        if t.get("live"):
            if not (t.get("reply") or t.get("writes")):
                continue
        elif t.get("status") not in ("ok", "budget"):
            continue
        if (_parse(t.get("ts")) or now) < cutoff:
            continue
        out.append(t)
    return out


def grade_sched(*, cap: int = SCHED_GRADE_CAP, judge: str = "auto", runner=None,
                now: datetime | None = None) -> dict:
    """Judge the ungraded scheduling turns: shadow turns against Claude's
    answer as a reference (the judge runs on Claude, which saw that turn
    already), live turns on their own (the judge runs on the remote
    model)."""
    now = now or _now()
    turns = remote_llm.read(SCHED_TURNS)
    graded = {g.get("run_id") for g in remote_llm.read(SCHED_GRADES) if g.get("shadow_grade")}
    out = {"judged": 0, "errors": 0, "grades": {}, "cost_usd": 0.0}
    for t in _sched_candidates(turns, graded, now)[:cap]:
        live = bool(t.get("live"))
        claude = t.get("claude") or {}
        which = judge if judge in ("claude", "remote") else ("remote" if live else "claude")
        checks = t.get("checks") or {}
        prompt = SCHED_JUDGE_PROMPT.format(
            user_msg=str(t.get("user_msg") or "")[:1500],
            task_block=str(t.get("task_block") or "")[:6000] or "(not on file)",
            solver_options=json.dumps(t.get("solver_options") or [], default=str)[:2000],
            checks=json.dumps({k: checks.get(k) for k in ("shadow", "claude", "mode_agree", "overlap")},
                              default=str)[:2000],
            claude_reply=(str(claude.get("reply") or "")[:3000] or "(no Claude answer on file: a live turn)"),
            claude_writes=json.dumps({"drafts": claude.get("drafts") or [],
                                      "staged": checks.get("claude", {}).get("staged")}, default=str)[:2000],
            shadow_reply=str(t.get("reply") or "")[:3000] or "(no reply)",
            writes_note="run for real" if live else "recorded, not run",
            writes=json.dumps(t.get("writes") or [], default=str)[:3000],
            kinds=", ".join(PROBLEM_KINDS))
        rec = {"ts": _iso(now), "run_id": t.get("run_id"), "turn_ts": t.get("ts"),
               "lane": t.get("lane"), "live": live, "judge": which}
        try:
            ans, meta = _judge(prompt, SCHED_JUDGE_SCHEMA, judge=which, runner=runner)
            ok = ("send", "edit", "wrong", "none")
            rec.update({
                "shadow_grade": ans.get("shadow_grade") if ans.get("shadow_grade") in ok else "none",
                "claude_grade": ans.get("claude_grade") if ans.get("claude_grade") in ok else "none",
                "better": ans.get("better") if ans.get("better") in ("shadow", "claude", "same") else "same",
                "problems": [{"kind": p.get("kind") if p.get("kind") in PROBLEM_KINDS else "other",
                              "note": str(p.get("note"))[:200]}
                             for p in (ans.get("problems") or []) if isinstance(p, dict)][:10],
                "enhancement": str(ans.get("enhancement") or "")[:300],
                "cost_usd": meta.get("cost_usd")})
            out["judged"] += 1
            out["grades"][rec["shadow_grade"]] = out["grades"].get(rec["shadow_grade"], 0) + 1
            out["cost_usd"] += float(meta.get("cost_usd") or 0)
        except Exception as e:
            rec["error"] = str(e)[:200]
            out["errors"] += 1
        remote_llm.append(SCHED_GRADES, rec)
    out["cost_usd"] = round(out["cost_usd"], 3)
    return out


def purge_raw(older_than_days: int | None = None, now: datetime | None = None) -> int:
    d = remote_llm.store_dir() / RAW_DIR
    if not d.exists():
        return 0
    now = now or _now()
    n = 0
    for p in d.glob("*.json"):
        try:
            if older_than_days is None or \
                    datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < now - timedelta(days=older_than_days):
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


# ── measures ──────────────────────────────────────────────────────────

def _count(items, key) -> dict:
    out: dict = {}
    for it in items:
        k = key(it)
        if k is None:
            continue
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], str(kv[0]))))


def _draft_outcomes(run_ids: set[str]) -> dict:
    """What the user did with the drafts those turns wrote: sent (and
    unchanged), cancelled, mean edit ratio; from the writer's send
    measure, joined through the draft journal."""
    ids: set[str] = set()
    try:
        import draft_events
        for ev in draft_events.read_since(0):
            if ev.get("run_id") in run_ids and ev.get("action") == "created" and ev.get("draft_id"):
                ids.add(ev["draft_id"])
    except Exception:
        return {"drafts": 0}
    outcomes = {}
    for r in remote_llm.read("draft-ab-outcomes.jsonl"):
        if r.get("draft_id") in ids:
            outcomes[r["draft_id"]] = r
    sent = [o for o in outcomes.values() if o.get("outcome") == "sent"]
    edits = [float(o.get("edit_ratio")) for o in sent if o.get("edit_ratio") is not None]
    return {"drafts": len(ids), "sent": len(sent),
            "unchanged": sum(1 for o in sent if (o.get("edit_ratio") or 0) == 0),
            "cancelled": sum(1 for o in outcomes.values() if o.get("outcome") == "cancelled"),
            "edit_ratio_mean": round(sum(edits) / len(edits), 3) if edits else None}


def measure_mail(frm: date, until: date, *, served: str = "text") -> dict:
    """The mail numbers for reads served as `served` (text: the ghost
    week; sealed: after the cutover) in the window."""
    reads = [r for r in remote_llm.read(READS) if r.get("served") == served
             and _in_window(r.get("ts"), frm, until)]
    lanes = [r for r in reads if r.get("lane") in ("bot", "cron")]
    text_msgs = sum(r.get("text_messages") or 0 for r in lanes)
    seal = [float(r["seal_s"]) for r in lanes if r.get("seal_s") is not None and not r.get("seal_error")]
    import mail_privacy
    out = {"served": served, "from": frm.isoformat(), "until": until.isoformat(),
           "reads": len(lanes), "reads_by_lane": _count(reads, lambda r: r.get("lane")),
           "reads_by_verb": _count(lanes, lambda r: r.get("verb")),
           "messages": sum(r.get("messages") or 0 for r in lanes), "text_messages": text_msgs,
           "unread_before": sum(r.get("unread_before") or 0 for r in lanes),
           # A read's unread count never exceeds its text rows.
           "unread_after": sum(min(r.get("unread_after") or 0, r.get("text_messages") or 0) for r in lanes),
           "inline_extracted": sum(r.get("inline_extracted") or 0 for r in lanes),
           "seal_failures": sum(1 for r in lanes if r.get("seal_error")),
           "seal_p50_s": _quantile(seal, 0.5), "seal_p95_s": _quantile(seal, 0.95),
           "budget_hits": sum(1 for s in seal if s >= mail_privacy.SEAL_BUDGET_S),
           "raw_kb": sum(r.get("raw_bytes") or 0 for r in lanes) // 1000,
           "sealed_kb": sum(r.get("sealed_bytes") or 0 for r in lanes) // 1000}
    out["seal_failure_share"] = _pct(out["seal_failures"], out["reads"])
    out["unread_share"] = _pct(out["unread_after"], text_msgs)
    ids = {r["id"] for r in lanes}
    grades = [g for g in remote_llm.read(MAIL_GRADES) if g.get("read_id") in ids and g.get("verdict")]
    out["judged"] = len(grades)
    out["judged_with_artifacts"] = sum(1 for g in grades if g.get("has_artifacts"))
    out["verdicts"] = _count(grades, lambda g: g.get("verdict"))
    out["blocked_share"] = _pct(out["verdicts"].get("blocked", 0), len(grades))
    out["facts"] = sum(g.get("facts") or 0 for g in grades)
    out["facts_missing"] = sum(len(g.get("facts_missing") or []) for g in grades)
    out["missing_by_category"] = _count([m for g in grades for m in g.get("facts_missing") or []],
                                        lambda m: m.get("category"))
    out["record_errors"] = sum(len(g.get("record_errors") or []) for g in grades)
    out["errors_by_field"] = _count([e for g in grades for e in g.get("record_errors") or []],
                                    lambda e: e.get("field"))
    out["reads_with_critical_error"] = sum(1 for g in grades if g.get("critical_errors"))
    out["critical_error_share"] = _pct(out["reads_with_critical_error"], len(grades))
    out["verdicts_by_lane"] = {}
    for g in grades:
        d = out["verdicts_by_lane"].setdefault(g.get("lane") or "?", {})
        d[g["verdict"]] = d.get(g["verdict"], 0) + 1
    out["verdicts_by_verb"] = {}
    for g in grades:
        d = out["verdicts_by_verb"].setdefault(g.get("verb") or "?", {})
        d[g["verdict"]] = d.get(g["verdict"], 0) + 1
    out["blocked"] = [{"read_id": g["read_id"], "run_id": g.get("run_id"), "verb": g.get("verb"),
                       "lane": g.get("lane"), "missing": [m["fact"] for m in g.get("facts_missing") or []][:4],
                       "errors": [e["field"] for e in g.get("record_errors") or []]}
                      for g in grades if g.get("verdict") == "blocked"]
    out["enhancements"] = [{"ts": g["ts"], "verdict": g["verdict"], "line": g["enhancement"]}
                           for g in grades if g.get("enhancement")]
    out["judge_cost_usd"] = round(sum(float(g.get("cost_usd") or 0) for g in grades), 3)
    turns = [t for t in remote_llm.read(TURNS) if _in_window(t.get("ts"), frm, until)]
    read_runs = {r.get("run_id") for r in lanes}
    with_reads = [t for t in turns if t.get("run_id") in read_runs]
    out["turns_with_reads"] = len(with_reads)
    out["asked_for_text"] = sum(1 for t in with_reads if t.get("asked_for_text"))
    out["draft_outcomes"] = _draft_outcomes(read_runs)
    return out


def measure_sched(frm: date, until: date, *, live: bool = False) -> dict:
    """The scheduling numbers for shadow turns (the ghost week) or live
    turns (after the cutover) in the window."""
    import sched_agent
    turns = [t for t in remote_llm.read(SCHED_TURNS) if bool(t.get("live")) == live
             and _in_window(t.get("ts"), frm, until)]
    answered = [t for t in turns if t.get("status") in ("ok", "budget")]
    scored = [t for t in turns if "shadow" in (t.get("checks") or {})]
    lat = [float(t.get("latency_s") or 0) for t in answered]
    cl_lat = [float((t.get("claude") or {}).get("elapsed_s") or 0) for t in turns
              if (t.get("claude") or {}).get("elapsed_s")]
    out = {"live": live, "from": frm.isoformat(), "until": until.isoformat(),
           "turns": len(turns), "by_status": _count(turns, lambda t: t.get("status")),
           "by_lane": _count(turns, lambda t: t.get("lane")),
           "errors": sum(1 for t in turns if t.get("status") == "error"),
           "invented_times": sum(len(t["checks"]["shadow"].get("invented") or []) for t in scored),
           "turns_with_invented": sum(1 for t in scored if t["checks"]["shadow"].get("invented")),
           "times_named": sum((t["checks"]["shadow"].get("validity") or {}).get("named", 0) for t in scored),
           "times_valid": sum((t["checks"]["shadow"].get("validity") or {}).get("valid", 0) for t in scored),
           "times_checked": sum((t["checks"]["shadow"].get("validity") or {}).get("checked", 0) for t in scored),
           "breaks": _count([b for t in scored for b in t["checks"]["shadow"].get("breaks") or []], lambda b: b),
           "mode_agreement": _pct(sum(1 for t in scored if t["checks"].get("mode_agree")), len(scored)),
           "latency_p50_s": _quantile(lat, 0.5), "latency_p95_s": _quantile(lat, 0.95),
           "claude_latency_p95_s": _quantile(cl_lat, 0.95),
           "cold_starts": sum(1 for t in turns if t.get("cold_start")),
           "outcomes": sched_agent._outcome_match(turns, remote_llm.read(SCHED_OUTCOMES))}
    if live:
        out["fallbacks"] = _count(turns, lambda t: t.get("fallback"))
        out["writes"] = sum(1 for t in turns for w in t.get("writes") or []
                            if w.get("executed") and w.get("result") not in ("error", "refused"))
    out["error_share"] = _pct(out["errors"], len(turns))
    ids = {t.get("run_id") for t in turns}
    grades = [g for g in remote_llm.read(SCHED_GRADES) if g.get("run_id") in ids and g.get("shadow_grade")]
    out["judged"] = len(grades)
    out["grades"] = _count(grades, lambda g: g.get("shadow_grade"))
    out["claude_grades"] = _count(grades, lambda g: g.get("claude_grade"))
    out["better"] = _count(grades, lambda g: g.get("better"))
    out["send_share"] = _pct(out["grades"].get("send", 0), len(grades))
    out["wrong_share"] = _pct(out["grades"].get("wrong", 0), len(grades))
    out["problems_by_kind"] = _count([p for g in grades for p in g.get("problems") or []], lambda p: p.get("kind"))
    out["grades_by_lane"] = {}
    for g in grades:
        d = out["grades_by_lane"].setdefault(g.get("lane") or "?", {})
        d[g["shadow_grade"]] = d.get(g["shadow_grade"], 0) + 1
    out["wrong"] = [{"run_id": g["run_id"], "lane": g.get("lane"),
                     "problems": [p["kind"] + ": " + p["note"] for p in g.get("problems") or []][:3]}
                    for g in grades if g.get("shadow_grade") == "wrong"]
    out["enhancements"] = [{"ts": g["ts"], "grade": g["shadow_grade"], "line": g["enhancement"]}
                           for g in grades if g.get("enhancement")]
    out["judge_cost_usd"] = round(sum(float(g.get("cost_usd") or 0) for g in grades), 3)
    return out


# ── verdicts ──────────────────────────────────────────────────────────

def _check(name, value, limit, ok, *, pct=False) -> dict:
    return {"name": name, "value": value, "limit": limit, "ok": bool(ok), "pct": pct}


def verdict_mail(m: dict) -> dict:
    p = MAIL_PASS
    checks = [
        _check("judged reads", m["judged"], f">= {p['judged_min']}", m["judged"] >= p["judged_min"]),
        _check("blocked share", m["blocked_share"], f"<= {p['blocked_share_max']:.0%}",
               (m["blocked_share"] or 0) <= p["blocked_share_max"], pct=True),
        _check("reads with a critical record error", m["critical_error_share"],
               f"<= {p['critical_error_share_max']:.0%}",
               (m["critical_error_share"] or 0) <= p["critical_error_share_max"], pct=True),
        _check("seal failures", m["seal_failure_share"], f"<= {p['seal_failure_share_max']:.0%}",
               (m["seal_failure_share"] or 0) <= p["seal_failure_share_max"], pct=True),
        _check("messages unread after the budget", m["unread_share"], f"<= {p['unread_share_max']:.0%}",
               (m["unread_share"] or 0) <= p["unread_share_max"], pct=True),
        _check("seal p95", m["seal_p95_s"], f"<= {p['seal_p95_max_s']:.0f} s",
               m["seal_p95_s"] is None or m["seal_p95_s"] <= p["seal_p95_max_s"]),
    ]
    if m["judged"] < p["judged_min"]:
        v = "NOT ENOUGH DATA"
    else:
        v = "GO" if all(c["ok"] for c in checks) else "NO-GO"
    return {"verdict": v, "checks": checks}


def verdict_sched(s: dict) -> dict:
    p = SCHED_PASS
    oc = s.get("outcomes") or {}
    named = _pct(oc.get("shadow_named", 0), oc.get("events", 0))
    enough_events = (oc.get("events") or 0) >= p["outcome_events_min"]
    checks = [
        _check("judged turns", s["judged"], f">= {p['judged_min']}", s["judged"] >= p["judged_min"]),
        _check("invented times", s["invented_times"], f"= {p['invented_times_max']}",
               s["invented_times"] <= p["invented_times_max"]),
        _check("wrong share", s["wrong_share"], f"<= {p['wrong_share_max']:.0%}",
               (s["wrong_share"] or 0) <= p["wrong_share_max"], pct=True),
        _check("send-as-is share", s["send_share"], f">= {p['send_share_min']:.0%}",
               (s["send_share"] or 0) >= p["send_share_min"], pct=True),
        _check("turn errors", s["error_share"], f"<= {p['error_share_max']:.0%}",
               (s["error_share"] or 0) <= p["error_share_max"], pct=True),
        _check("latency p95", s["latency_p95_s"], f"<= {p['latency_p95_max_s']:.0f} s",
               s["latency_p95_s"] is None or s["latency_p95_s"] <= p["latency_p95_max_s"]),
        _check(f"user's chosen time named by the agent ({oc.get('events', 0)} events)", named,
               f">= {p['outcome_named_min']:.0%} once {p['outcome_events_min']} events exist",
               (not enough_events) or (named or 0) >= p["outcome_named_min"], pct=True),
    ]
    if s["judged"] < p["judged_min"]:
        v = "NOT ENOUGH DATA"
    else:
        v = "GO" if all(c["ok"] for c in checks) else "NO-GO"
    return {"verdict": v, "checks": checks}


def _fmt(v, pct=False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.0f}%"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def _rank_lines(items: list[dict], n: int = 12) -> list[str]:
    """The enhancement lines, duplicates folded, most repeated first."""
    def key(line: str) -> str:
        return re.sub(r"\W+", " ", line.lower()).strip()[:80]
    seen: dict[str, int] = {}
    first: dict[str, str] = {}
    for it in items:
        line = str(it.get("line") or "")
        k = key(line)
        if not k:
            continue
        seen[k] = seen.get(k, 0) + 1
        first.setdefault(k, line)
    ranked = sorted(seen, key=lambda k: -seen[k])
    return [f"{seen[k]}x  {first[k]}" for k in ranked[:n]]


# ── reports ───────────────────────────────────────────────────────────

def report(part: str, *, cfg: dict | None = None, today: date | None = None) -> dict:
    cfg = cfg if cfg is not None else remote_llm.config()
    today = today or date.today()
    frm, until = window(part, cfg, today)
    day_n, days = day_number(part, cfg, today)
    mode = remote_llm.mode(part, cfg, today)
    if part == "mail":
        ghost = measure_mail(frm, until, served="text")
        after = measure_mail(frm, until, served="sealed")
        v = verdict_mail(ghost)
    else:
        ghost = measure_sched(frm, until, live=False)
        after = measure_sched(frm, until, live=True)
        v = verdict_sched(ghost)
    return {"part": part, "mode": mode, "from": frm.isoformat(), "until": until.isoformat(),
            "day": day_n, "days": days, "ghost": ghost, "after": after, **v}


def summary_lines(parts=PARTS, *, cfg: dict | None = None, today: date | None = None) -> list[str]:
    """The lines the daily report posts: one or two per part that is
    recording, numbers only."""
    cfg = cfg if cfg is not None else remote_llm.config()
    lines = []
    for part in parts:
        mode = remote_llm.mode(part, cfg, today)
        if part == "mail" and mode not in ("ghost", "sealed"):
            continue
        if part == "scheduling" and mode not in ("shadow", "enforce"):
            continue
        r = report(part, cfg=cfg, today=today)
        g, a = r["ghost"], r["after"]
        if part == "mail":
            # The record is what the on-prem model hands every lane (the
            # ledger brief, the briefing, the digests), so its error rate
            # by field is the headline; the sealed-cutover verdict stays
            # in `report` and `final` for the gate.
            lines.append(
                f"Record quality, mail (day {r['day']} of {r['days']}): judged {g['judged']} reads, "
                f"critical record error on {_fmt(g['critical_error_share'], True)} of them; errors by field "
                f"{json.dumps(g['errors_by_field'])}; verdicts {json.dumps(g['verdicts'])}; losses by kind "
                f"{json.dumps(g['missing_by_category'])}. Reads: {g['reads']} sealed on the side (lanes "
                f"{json.dumps(g['reads_by_lane'])}), {g['messages']} messages, seal failures {g['seal_failures']}, "
                f"unread after the budget {g['unread_after']} of {g['text_messages']}, seal p95 "
                f"{_fmt(g['seal_p95_s'])} s, text {g['raw_kb']} KB vs records {g['sealed_kb']} KB.")
            if a["reads"]:
                lines.append(
                    f"Sealed mail live: {a['reads']} reads; seal failures {a['seal_failures']}; unread after "
                    f"the budget {a['unread_after']} of {a['text_messages']}; p95 {_fmt(a['seal_p95_s'])} s; "
                    f"replies asking for the text {a['asked_for_text']} of {a['turns_with_reads']}; judged "
                    f"{a['judged']} (remote judge): {json.dumps(a['verdicts'])}; drafts {json.dumps(a['draft_outcomes'])}.")
        else:
            lines.append(
                f"Ghost week, scheduling (day {r['day']} of {r['days']}): {g['turns']} turns "
                f"{json.dumps(g['by_status'])}; invented times {g['invented_times']}; valid times "
                f"{g['times_valid']}/{g['times_checked']}; p95 {_fmt(g['latency_p95_s'])} s "
                f"(Claude {_fmt(g['claude_latency_p95_s'])} s). Judged {g['judged']}: {json.dumps(g['grades'])}; "
                f"Claude {json.dumps(g['claude_grades'])}; better {json.dumps(g['better'])}; problems "
                f"{json.dumps(g['problems_by_kind'])}; user's time named {json.dumps(g['outcomes'])}. "
                f"Verdict so far: {r['verdict']}.")
            if a["turns"]:
                lines.append(
                    f"Scheduling agent live: {a['turns']} turns {json.dumps(a['by_status'])}; fallbacks "
                    f"{json.dumps(a.get('fallbacks'))}; writes {a.get('writes')}; invented times "
                    f"{a['invented_times']}; p95 {_fmt(a['latency_p95_s'])} s; judged {a['judged']}: "
                    f"{json.dumps(a['grades'])}.")
    return lines


def final(part: str, *, cfg: dict | None = None, today: date | None = None, write: bool = True) -> dict:
    """The final report for one part: the numbers, each pass level, the
    verdict, and the enhancements ranked. Written to the store as JSON
    (what `gate` reads) and Markdown."""
    r = report(part, cfg=cfg, today=today)
    r["generated"] = _iso(_now())
    g = r["ghost"]
    lines = [f"# Ghost week: {part}", "",
             f"Window {r['from']} to {r['until']}, generated {r['generated']}. "
             f"Switch now: `{r['mode']}`.", "",
             f"## Verdict: {r['verdict']}", ""]
    for c in r["checks"]:
        lines.append(f"- {'PASS' if c['ok'] else 'FAIL'}  {c['name']}: {_fmt(c['value'], c.get('pct'))} "
                     f"(pass level {c['limit']})")
    lines += ["", "## Enhancements the judge named, ranked", ""]
    lines += [f"- {ln}" for ln in _rank_lines(g.get("enhancements") or [])] or ["- (none)"]
    if part == "mail":
        lines += ["", "## Where the record fell short", "",
                  f"- Facts the turns used: {g['facts']}; missing from the record: {g['facts_missing']} "
                  f"{json.dumps(g['missing_by_category'])}",
                  f"- Record errors: {g['record_errors']} by field {json.dumps(g['errors_by_field'])}; "
                  f"reads with a critical error: {g['reads_with_critical_error']}",
                  f"- Verdicts by lane: {json.dumps(g['verdicts_by_lane'])}",
                  f"- Verdicts by verb: {json.dumps(g['verdicts_by_verb'])}",
                  f"- Judged with the turn's output on file: {g['judged_with_artifacts']} of {g['judged']}",
                  "", "## Blocked reads", ""]
        lines += [f"- {b['run_id']} {b['verb']} ({b['lane']}): missing {b['missing']}; errors {b['errors']}"
                  for b in g.get("blocked") or []] or ["- (none)"]
    else:
        lines += ["", "## Where the agent fell short", "",
                  f"- Problems by kind: {json.dumps(g['problems_by_kind'])}",
                  f"- Rule breaks (code): {json.dumps(g['breaks'])}",
                  f"- Grades by lane: {json.dumps(g['grades_by_lane'])}",
                  f"- Claude's own grades on the same turns: {json.dumps(g['claude_grades'])}; "
                  f"better side {json.dumps(g['better'])}",
                  "", "## Turns graded wrong", ""]
        lines += [f"- {w['run_id']} ({w['lane']}): {'; '.join(w['problems'])}" for w in g.get("wrong") or []] \
            or ["- (none)"]
    lines += ["", "## Numbers", "", "```json", json.dumps(g, indent=1, default=str), "```", ""]
    if (r["after"].get("reads") if part == "mail" else r["after"].get("turns")):
        lines += ["## After the cutover (same window)", "", "```json",
                  json.dumps(r["after"], indent=1, default=str), "```", ""]
    r["markdown"] = "\n".join(lines)
    if write:
        store = remote_llm.store_dir()
        _owner_trust.write_owner_file(store / FINAL_JSON.format(part=part),
                                      json.dumps({k: v for k, v in r.items() if k != "markdown"},
                                                 indent=1, default=str).encode(), mode=0o600)
        _owner_trust.write_owner_file(store / FINAL_MD.format(part=part), r["markdown"].encode(), mode=0o600)
        r["path"] = str(store / FINAL_MD.format(part=part))
    return r


def gate(part: str, *, today: date | None = None) -> tuple[bool, str]:
    """(ok, why) for the cutover of one part: the latest final report
    must say GO and be at most FINAL_MAX_AGE_DAYS old."""
    path = remote_llm.store_dir() / FINAL_JSON.format(part=part)
    try:
        r = json.loads(path.read_text())
    except FileNotFoundError:
        return False, (f"no ghost week report for {part}; run `ghost_week.py final --part {part}` "
                       f"after a week of `{'mail ghost' if part == 'mail' else 'scheduling shadow'}`")
    except ValueError as e:
        return False, f"the ghost week report for {part} is unreadable ({str(e)[:80]})"
    gen = _parse(r.get("generated") or "")
    today = today or date.today()
    if gen is None or (today - gen.astimezone().date()).days > FINAL_MAX_AGE_DAYS:
        return False, f"the ghost week report for {part} is older than {FINAL_MAX_AGE_DAYS} days"
    if r.get("verdict") != "GO":
        failed = [c["name"] for c in r.get("checks") or [] if not c.get("ok")]
        return False, (f"the ghost week for {part} ended {r.get('verdict')} on "
                       f"{gen.astimezone().date().isoformat()}"
                       + (f" (failed: {', '.join(failed)})" if failed else ""))
    return True, f"the ghost week for {part} ended GO on {gen.astimezone().date().isoformat()}"


def daily(*, post=None, cfg: dict | None = None, today: date | None = None) -> dict:
    """The daily step: the summary lines for the health channel, and on a
    window's last day the final report. Returns what it produced."""
    cfg = cfg if cfg is not None else remote_llm.config()
    today = today or date.today()
    out = {"lines": summary_lines(cfg=cfg, today=today), "finals": {}}
    for part in PARTS:
        mode = remote_llm.mode(part, cfg, today)
        ghosting = (part == "mail" and mode == "ghost") or (part == "scheduling" and mode == "shadow")
        if not ghosting:
            continue
        _frm, until = window(part, cfg, today)
        if today == until:
            r = final(part, cfg=cfg, today=today)
            out["finals"][part] = r.get("verdict")
            out["lines"].append(f"The ghost week for {part} ended today: {r['verdict']}. "
                                f"The report is {r.get('path')} (private).")
    if post and out["lines"]:
        try:
            post("\n".join(out["lines"]))
        except Exception as e:
            print(f"ghost_week.daily: post failed: {e}", file=sys.stderr)
    return out


# ── the tripwire after a cutover ──────────────────────────────────────

def _state() -> dict:
    try:
        return json.loads((remote_llm.store_dir() / STATE).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _owner_trust.write_owner_file(remote_llm.store_dir() / STATE, json.dumps(st).encode(), mode=0o600)


def tripwire(*, now: datetime | None = None, post=None, cfg: dict | None = None) -> dict:
    """After a cutover: the last hour's sealed reads and live turns. Three
    seal failures, half the messages unread across four or more reads,
    or three live-turn errors post one alert, at most every six hours.
    Nothing flips a switch; the user decides."""
    cfg = cfg if cfg is not None else remote_llm.config()
    now = now or _now()
    since = now - timedelta(seconds=TRIPWIRE_WINDOW_S)
    alerts = []
    if remote_llm.mode("mail", cfg) == "sealed":
        reads = [r for r in remote_llm.read(READS) if r.get("served") == "sealed"
                 and r.get("lane") in ("bot", "cron") and (_parse(r.get("ts")) or since) >= since]
        fails = sum(1 for r in reads if r.get("seal_error"))
        msgs = sum(r.get("text_messages") or 0 for r in reads)
        unread = sum(r.get("unread_after") or 0 for r in reads)
        if fails >= 3:
            alerts.append(f"sealed mail: {fails} of {len(reads)} reads failed to seal in the last hour")
        if len(reads) >= 4 and msgs and unread / msgs >= 0.5:
            alerts.append(f"sealed mail: {unread} of {msgs} messages came back unread in the last hour "
                          "(the remote model may be down)")
    if remote_llm.mode("scheduling", cfg) == "enforce":
        turns = [t for t in remote_llm.read(SCHED_TURNS) if t.get("live")
                 and (_parse(t.get("ts")) or since) >= since]
        errs = sum(1 for t in turns if t.get("status") == "error")
        if errs >= 3:
            alerts.append(f"live scheduling: {errs} of {len(turns)} turns errored in the last hour")
    out = {"alerts": alerts, "posted": False}
    if alerts:
        st = _state()
        last = _parse(st.get("last_alert") or "")
        if last is None or (now - last).total_seconds() >= TRIPWIRE_EVERY_S:
            # An alert: mail that does not seal is mail no lane can read, so
            # it posts whether or not debug mode is on (chat_notify, Debug mode);
            # on the web chat with debug mode off the status light keeps it
            # until the owner marks it read (chat_notify, `light`).
            text = ":warning: Ghost-week tripwire. " + " ".join(alerts) + \
                " Turn back with `remote_llm.py set mail enforce` or `set scheduling shadow`."
            try:
                if post is None:
                    import slack_notifier
                    slack_notifier.post_to_slack(slack_notifier.health_channel_key(), text,
                                                 light="tripwire")
                else:
                    post(text)
                out["posted"] = True
                st["last_alert"] = _iso(now)
                _save_state(st)
            except Exception as e:
                print(f"ghost_week.tripwire: post failed: {e}", file=sys.stderr)
    return out


def status() -> dict:
    cfg = remote_llm.config()
    out = {"mail": remote_llm.mode("mail", cfg), "scheduling": remote_llm.mode("scheduling", cfg),
           "windows": {p: {"from": window(p, cfg)[0].isoformat(), "until": window(p, cfg)[1].isoformat(),
                           "day": day_number(p, cfg)[0], "days": day_number(p, cfg)[1]} for p in PARTS},
           "reads": len(remote_llm.read(READS)), "turns": len(remote_llm.read(TURNS)),
           "mail_grades": sum(1 for g in remote_llm.read(MAIL_GRADES) if g.get("verdict")),
           "sched_grades": sum(1 for g in remote_llm.read(SCHED_GRADES) if g.get("shadow_grade")),
           "raw_files": len(list((remote_llm.store_dir() / RAW_DIR).glob("*.json")))
           if (remote_llm.store_dir() / RAW_DIR).exists() else 0,
           "gate": {p: gate(p) for p in PARTS}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("report")
    p.add_argument("--part", choices=PARTS + ("both",), default="both")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("grade")
    p.add_argument("--part", choices=PARTS + ("both",), default="both")
    p.add_argument("--cap", type=int, default=None)
    p.add_argument("--judge", choices=("auto", "claude", "remote"), default="auto")
    p = sub.add_parser("final")
    p.add_argument("--part", choices=PARTS, required=True)
    sub.add_parser("purge-raw")
    a = ap.parse_args(argv)
    if not _owner_trust.is_owner_process():
        print("ghost_week runs in the owner's processes only", file=sys.stderr)
        return 2
    if a.cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
    elif a.cmd == "report":
        parts = PARTS if a.part == "both" else (a.part,)
        if a.json:
            print(json.dumps({p: report(p) for p in parts}, indent=2, default=str))
        else:
            for p in parts:
                r = report(p)
                print(f"{p}: day {r['day']} of {r['days']} ({r['from']} to {r['until']}), "
                      f"switch {r['mode']}: {r['verdict']}")
                for c in r["checks"]:
                    print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']}: {_fmt(c['value'], c.get('pct'))} "
                          f"(pass level {c['limit']})")
                for ln in summary_lines((p,)):
                    print("  " + ln)
    elif a.cmd == "grade":
        out = {}
        if a.part in ("mail", "both"):
            out["mail"] = grade_mail(cap=a.cap or MAIL_GRADE_CAP, judge=a.judge)
        if a.part in ("scheduling", "both"):
            out["scheduling"] = grade_sched(cap=a.cap or SCHED_GRADE_CAP, judge=a.judge)
        print(json.dumps(out, default=str))
    elif a.cmd == "final":
        r = final(a.part)
        print(json.dumps({"verdict": r["verdict"], "path": r.get("path"),
                          "checks": r["checks"]}, indent=1, default=str))
    else:
        print(json.dumps({"removed": purge_raw()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
