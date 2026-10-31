#!/usr/bin/env python3
"""The weekly audit on the remote model (docs/llm-lanes.md, "Briefings").

run-agent.sh calls this for weekly-audit after the weekly preprocessor
has written the briefing JSON, and posts what it prints as it posts a
Claude lane's text.

    remote_audit.py run --briefing <json> --run-id <cron-...> [--dry-run]

Switch: `"audits": "remote"` in remote-llm.json. Off, or outside an owner
process, `run` exits 3 and run-agent.sh runs the Claude lane on
skills/weekly-audit.md.

The run:
  1. Code gathers every fact the post shows: the open and snoozed ledger
     rows (each a ref), the dropped threads, the rows closed in the last
     seven days, the open reimbursement rows, the project hygiene
     candidates and the closure-automation numbers (closure_metrics).
  2. One remote call, under a strict schema, picks by ref: the critical
     and important lists with a short why, up to four pattern lines, and
     up to three items to hand to Claude (a whole-thread read, a report or
     proposal draft, a reply to a program officer). A ref the call invents
     is dropped; a row kept from Claude is never handed to it.
  3. Code renders the skill's template. The pre-send audit runs with the
     remote model's verdicts (remote_briefing.audit_post); rows it closes
     or parks leave the post.
  4. The hand-offs go to the bot's queue (remote_briefing.queue_escalations);
     the bot gives a run's hand-offs to one Claude session.
The remote model sees no calendar, so the post makes no calendar claim.
A scrubber rule change needs backtest numbers, which this lane does not
run: the post names the signal and leaves the proposal to a Claude turn.
When the remote model cannot answer, the lists come from code (deadlines
first) and the post says so.

--dry-run does the reads and the remote call, then queues nothing and
closes nothing and prints counts only; the post is in the run record.

Store: <private_dir>/remote-llm/audits.jsonl, one record per run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger as _ledger  # noqa: E402
import remote_briefing as rb  # noqa: E402
import remote_llm  # noqa: E402

RUNS = "audits.jsonl"
AGENT = "weekly-audit"
EXIT_OFF = 3
MAX_CRITICAL, MAX_IMPORTANT, MAX_PATTERNS, MAX_HANDOFFS = 8, 8, 4, 3
CAPS = {"L": 60, "D": 10}
_PENDING = ("open", "snoozed", "follow_up")


def active(cfg: dict | None = None) -> bool:
    """The audits switch at `remote`, or the routing table's `audit` entry
    (model_routing.py) where it names local or hosted."""
    cfg = cfg if cfg is not None else remote_llm.config()
    return remote_llm.local_for("audit", cfg.get("audits") == "remote", cfg)


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


# ── the facts ─────────────────────────────────────────────────────────

def candidates(rows: list[dict], b: dict, *, today: date) -> dict[str, dict]:
    """{ref: item}: the pending ledger rows, soonest deadline first, then
    the dropped threads no row covers."""
    pending = [o for o in rows if (o.get("status") or "open") in _PENDING]
    far = date.max

    def order(o):
        return (_date(o.get("deadline")) or far, -(int(o.get("days_stale") or 0)))
    out: dict[str, dict] = {}
    convs: set[str] = set()
    for n, o in enumerate(sorted(pending, key=order)[:CAPS["L"]], 1):
        due = _date(o.get("deadline"))
        brief = o.get("thread_brief") if isinstance(o.get("thread_brief"), dict) else None
        if o.get("conversation_id"):
            convs.add(o["conversation_id"])
        out[f"L{n}"] = {
            "kind": "ledger", "ledger_id": o.get("id") or "",
            "title": o.get("description") or o.get("id") or "",
            "who": o.get("from_name") or o.get("from_email") or "",
            "status": o.get("status") or "open", "type": o.get("type") or "",
            "due": due.isoformat() if due else "",
            "days_left": (due - today).days if due else None,
            "last_action": rb._last_action(o), "summary": (o.get("notes") or "")[:300],
            "ask": (brief or {}).get("ask") or "",
            "conversation_id": o.get("conversation_id") or "",
            "message_id": o.get("source_message_id") or "",
            "kept": bool(o.get("kept_from_claude")), "brief": brief}
    n = 0
    for t in b.get("new_dropped_threads") or []:
        conv = t.get("conversation_id") or ""
        if not conv or conv in convs or n >= CAPS["D"]:
            continue
        n += 1
        rec = t.get("remote_record") or {}
        out[f"D{n}"] = {
            "kind": "dropped", "ledger_id": "", "title": t.get("subject") or "(no subject)",
            "who": t.get("from_name") or t.get("from") or "", "status": "dropped",
            "due": rec.get("deadline") or "", "days_left": None,
            "days_stale": t.get("days_stale"), "summary": t.get("body_preview") or "",
            "ask": rec.get("ask") or "", "conversation_id": conv,
            "message_id": t.get("message_id") or "", "kept": False, "brief": None}
    return out


def _closure_numbers() -> dict:
    try:
        import closure_metrics
        return closure_metrics.build_report(7)
    except Exception as e:
        print(f"remote_audit: closure numbers skipped: {str(e)[:200]}", file=sys.stderr)
        return {}


def _hygiene(b: dict) -> list[dict]:
    cands = ((b.get("project_hygiene") or {}).get("candidates")
             if isinstance(b.get("project_hygiene"), dict) else None)
    if cands is not None:
        return list(cands)
    try:
        import project_store
        return project_store.dormancy_candidates(days=30)
    except Exception:
        return []


# ── the remote call ───────────────────────────────────────────────────

AUDIT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "critical": rb._items("ref", "why"), "important": rb._items("ref", "why"),
    "patterns": {"type": "array", "items": {"type": "string"}},
    "handoffs": rb._items("ref", "why", "task")},
    "required": ["critical", "important", "patterns", "handoffs"]}

AUDIT_PROMPT = """You write the weekly audit of a professor's open work for the assistant. Answer with JSON only, in the schema.

{profile}
It is {now}.

Use only the items given, and pick them by ref. Write short plain sentences. Never write an em-dash.

- critical: at most 8 refs that need action this week: a deadline within seven days, a consequence if it slips, a sponsor or program officer waiting. `why` is one short sentence from the item's facts.
- important: at most 8 more refs that need action within about a month, each with a short why.
- patterns: at most 4 short observations that the items show across the week: kinds of mail that wait, senders who wait long, work that keeps slipping. Say nothing the items do not show.
- handoffs: at most 3 refs that need more than these facts: a whole-thread read to know whether a reply is needed, a report or proposal to draft, a reply to a program officer. Give why, and the task for the assistant.
Never state anything about the calendar: you cannot see it. Never call an item done; the ledger decides that.

{rules}"""


def _prompt_items(cands: dict[str, dict]) -> list[dict]:
    keep = ("kind", "title", "who", "status", "type", "due", "days_left", "days_stale",
            "last_action", "summary", "ask")
    return [{"ref": r, **{k: c[k] for k in keep if c.get(k) not in (None, "", [])}}
            for r, c in cands.items()]


def ask_plan(cands: dict[str, dict], closed: list[dict], *, now: datetime, chat=None) -> dict:
    """The remote model's picks, refs checked. Raises remote_llm.RemoteLLMError
    when it cannot answer."""
    chat = chat or remote_llm.chat_json
    local = now.astimezone()
    system = AUDIT_PROMPT.format(profile=rb._profile(), now=f"{local:%A %Y-%m-%d}",
                                 rules=rb._rules() or "")
    user = {"items": _prompt_items(cands),
            "closed_this_week": [rb._clean(c.get("description"), 120) for c in closed][:30]}
    obj, _meta = chat([{"role": "system", "content": system},
                       {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
                      purpose="weekly-audit", schema=AUDIT_SCHEMA, schema_name="weekly_audit",
                      max_tokens=3000)
    return check_plan(obj, cands)


def check_plan(obj: dict, cands: dict[str, dict]) -> dict:
    """Unknown refs dropped, caps applied, a ref in one list only, and no
    hand-off without a thread or of a row kept from Claude."""
    def picks(key, cap, used):
        out = []
        for p in (obj or {}).get(key) or []:
            if isinstance(p, dict) and p.get("ref") in cands and p["ref"] not in used:
                used.add(p["ref"])
                out.append(p)
        return out[:cap]
    used: set[str] = set()
    plan = {"critical": picks("critical", MAX_CRITICAL, used),
            "important": picks("important", MAX_IMPORTANT, used)}
    plan["handoffs"] = [p for p in picks("handoffs", MAX_HANDOFFS, set())
                        if cands[p["ref"]].get("conversation_id") and not cands[p["ref"]].get("kept")]
    plan["patterns"] = [rb._clean(s, 200) for s in (obj or {}).get("patterns") or []
                        if isinstance(s, str) and s.strip()][:MAX_PATTERNS]
    return plan


def fallback_plan(cands: dict[str, dict]) -> dict:
    """Deadlines from code, when the remote model cannot answer."""
    crit, imp = [], []
    for r, c in cands.items():
        left = c.get("days_left")
        if left is None:
            continue
        if left <= 7:
            crit.append({"ref": r, "why": "overdue" if left < 0 else f"due in {left} day(s)"})
        elif left <= 30:
            imp.append({"ref": r, "why": f"due in {left} days"})
    return {"critical": crit[:MAX_CRITICAL], "important": imp[:MAX_IMPORTANT],
            "patterns": [], "handoffs": []}


# ── the post ──────────────────────────────────────────────────────────

def _line(c: dict, why: str) -> str:
    who = f" ({rb._clean(c.get('who'), 60)})" if c.get("who") else ""
    due = f", due {c['due']}" if c.get("due") else ""
    return f"• *{rb._clean(c.get('title'), 120)}*{who}{due}: {rb._clean(why, 160)}"


def _closure_block(m: dict) -> list[str]:
    if not m:
        return [":gear: Closure automation (last 7d): the numbers could not be read this run."]
    rate = m.get("hit_rate")
    rate_s = f"{round(rate * 100)}%" if isinstance(rate, (int, float)) else "n/a"
    prov = m.get("provenance") or {}
    lines = [":gear: Closure automation (last 7d):",
             f"• Hit rate: {rate_s} ({m.get('autonomous_closures', 0)} autonomous / "
             f"{m.get('manual_closures', 0)} manual)",
             "• Provenance: " + (", ".join(f"{k} {v}" for k, v in sorted(prov.items())) or "none"),
             f"• User-stated closures logged: {m.get('user_stated_closures_logged', 0)}"]
    fps = m.get("false_positive_candidates") or []
    lines.append("• Suspected false positives: "
                 + (", ".join(str(f.get("id") if isinstance(f, dict) else f) for f in fps[:6]) or "none"))
    sigs = [(s, n) for s, n in (m.get("suggested_signals") or {}).items() if n >= 2] \
        if isinstance(m.get("suggested_signals"), dict) else []
    if fps or sigs:
        named = ", ".join(f"{s} ({n})" for s, n in sigs[:3]) or "the suspected false positives"
        lines.append(f"• A scrubber rule change may be due ({named}); it needs a backtest first, "
                     "so ask for one in the EA channel.")
    else:
        lines.append("• No rule changes proposed.")
    return lines


def render(cands: dict[str, dict], plan: dict, *, closed: list[dict], reimb: list[dict],
           hygiene: list[dict], closure: dict, counts: dict, now: datetime,
           handed: list[dict], drop: set[str] = frozenset(), fallback: str = "") -> str:
    local = now.astimezone()
    monday = local.date() - timedelta(days=local.weekday())
    out = [f"📊 Weekly Audit, week of {monday:%b} {monday.day}"]
    if fallback:
        out.append(f"_{fallback}_")

    def section(title, picks):
        rows = [_line(cands[p["ref"]], p.get("why") or "") for p in picks
                if cands[p["ref"]].get("ledger_id") not in drop]
        out.extend(["", title] + (rows or ["• Nothing this week."]))
    section("🔴 Critical (action this week):", plan.get("critical") or [])
    section("🟡 Important (action soon):", plan.get("important") or [])
    out.extend(["", "🟢 Completed this week:"]
               + ([f"• {rb._clean(c.get('description'), 120)}" for c in closed[:15]]
                  or ["• Nothing closed this week."]))
    out.extend(["", "💰 Reimbursement status:"]
               + ([f"• {rb._clean(r.get('description'), 100)}: "
                   f"{rb._clean(r.get('last_action') or 'no action recorded', 120)}" for r in reimb]
                  or ["• No open reimbursement items."]))
    if plan.get("patterns"):
        out.extend(["", "📈 Patterns noticed:"] + [f"• {p}" for p in plan["patterns"]])
    if hygiene:
        out.extend(["", ":file_cabinet: *Project hygiene*"]
                   + [f"• {rb._clean(h.get('id'), 60)}: {rb._clean(h.get('name'), 80)}, "
                      f"last activity {rb._clean(h.get('last_activity'), 20) or 'unknown'}"
                      for h in hygiene[:8]]
                   + ["Reply `dormant <id>` (or `dormant all`) to apply, `keep <id>` to leave it active."])
    out.extend([""] + _closure_block(closure))
    if handed:
        out.extend(["", "🤖 Handed to Claude (one session in the EA channel):"]
                   + [f"• {rb._clean(cands[h['ref']].get('title'), 120)}: {rb._clean(h.get('task'), 140)}"
                      for h in handed])
    out.extend(["", f"📋 Obligations: {counts.get('open', 0)} open, {counts.get('snoozed', 0)} snoozed, "
                    f"{len(closed)} closed this week"])
    return "\n".join(out)


# ── the run ───────────────────────────────────────────────────────────

def run(briefing: dict, *, run_id: str, dry_run: bool = False, now: datetime | None = None,
        rows: list[dict] | None = None, closed: list[dict] | None = None, closure: dict | None = None,
        chat=None, audit=None, queue=None) -> tuple[str, dict]:
    """(the post, the run record)."""
    now = now or datetime.now(timezone.utc)
    t0 = time.monotonic()
    today = now.astimezone().date()
    rows = rows if rows is not None else _ledger.get_open_entries()
    closed = closed if closed is not None else _ledger.get_recently_closed(7)
    closure = closure if closure is not None else _closure_numbers()
    cands = candidates(rows, briefing, today=today)
    reimb = [o for o in rows if (o.get("type") == "reimbursement")
             and (o.get("status") or "open") in _PENDING]
    counts = {"open": sum(1 for o in rows if (o.get("status") or "open") in ("open", "follow_up")),
              "snoozed": sum(1 for o in rows if o.get("status") == "snoozed")}
    rec: dict = {"ts": now.isoformat(timespec="seconds"), "agent": AGENT, "run_id": run_id,
                 "dry_run": dry_run, "candidates": len(cands)}
    fallback = ""
    try:
        plan = ask_plan(cands, closed, now=now, chat=chat)
    except remote_llm.RemoteLLMError as e:
        plan = fallback_plan(cands)
        fallback = "The remote model did not answer; these lists come from code, deadlines first."
        rec["fallback"] = str(e)[:200]
    handed = list(plan.get("handoffs") or [])
    if handed and not dry_run:
        (queue or rb.queue_escalations)([rb._escalation(cands[p["ref"]], p, now) for p in handed],
                                        agent=AGENT, now=now)
    kw = dict(closed=closed, reimb=reimb, hygiene=_hygiene(briefing), closure=closure,
              counts=counts, now=now, handed=handed, fallback=fallback)
    text = render(cands, plan, **kw)
    checked = (audit or rb.audit_post)(AGENT, text, briefing, dry_run=dry_run, chat=None)
    drop = set((checked or {}).get("closed") or []) | set((checked or {}).get("parked") or [])
    if drop:
        text = render(cands, plan, drop=drop, **kw)
    rec.update(plan={k: len(v) for k, v in plan.items()}, handed=len(handed),
               audit={k: (len(v) if isinstance(v, list) else v) for k, v in (checked or {}).items()},
               seconds=round(time.monotonic() - t0, 1), post=text)
    return text, rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="write the weekly audit on the remote model")
    r.add_argument("--briefing", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--dry-run", action="store_true", help="no queue or closures; counts only")
    a = ap.parse_args(argv)
    if not active():
        return EXIT_OFF
    try:
        briefing = rb._load(a.briefing)
    except Exception as e:
        print(f"remote_audit: {e}", file=sys.stderr)
        return 1
    text, rec = run(briefing, run_id=a.run_id, dry_run=a.dry_run)
    remote_llm.append(RUNS, rec)
    if a.dry_run:
        print(json.dumps({k: v for k, v in rec.items() if k != "post"}))
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
