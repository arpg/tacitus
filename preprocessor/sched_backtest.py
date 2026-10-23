#!/usr/bin/env python3
"""sched_backtest: did the scheduling mail of the last N days reach the lane?

For every remote record whose scheduling kind asks something of the user
(request, proposes-times, reschedule, confirmation), what happened:

  row          a ledger row on that message (any prefix), or a sched- row
  thread       a proactive scheduling thread opened for its conversation
  card         an event or invite card, or a staged event, in that thread
  answered     the user wrote in the thread after it (handled by hand, or
               by a draft the EA made outside the scheduling lane)

Outlook's own meeting responses ("Accepted: ...") are counted apart: the
model labels them confirmations and they ask nothing. Two records of one
message (the same conversation and minute) count once. The counts per
kind, and the misses: records that got no row, no thread and no card. Owner only: it reads the owner's record store, the ledger,
the proactive seen file and the cards file, and the Sent folder through
Graph (--no-mail skips that join; "answered" is then unknown).

  python3 preprocessor/sched_backtest.py [--days 30] [--no-mail] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS = ("request", "proposes-times", "reschedule", "confirmation")
EA_DIR = Path(os.environ.get("EA_DIR", "/home/ea-bot/.ea"))
PRIVATE = Path(os.environ.get("EA_PRIVATE_DIR", str(Path.home() / ".ea-private")))


# ── the joins, as pure functions over loaded data ─────────────────────

def _iso(ts: str) -> str:
    return (ts or "").replace("Z", "+00:00")


def judge(records: list[dict], *, rows_by_msg: dict, sched_convs: set, seen: dict,
          card_threads: set, sent_times: dict | None, since: str) -> list[dict]:
    """One verdict per scheduling record received since `since`."""
    import mail_privacy
    out, seen_msgs, calendar = [], set(), 0
    for rec in sorted(records, key=lambda r: r.get("received") or ""):
        res = rec.get("result") or {}
        kind = res.get("scheduling")
        received = rec.get("received") or ""
        if kind not in KINDS or received < since or rec.get("status") != "ok":
            continue
        mid, conv = rec.get("id") or "", rec.get("conversation_id") or ""
        if (conv, received[:16]) in seen_msgs:
            continue
        seen_msgs.add((conv, received[:16]))
        if mail_privacy.CALENDAR_RESPONSE_SUBJECT.match(rec.get("subject") or ""):
            calendar += 1
            continue
        thread = (seen.get(conv) or {}).get("thread") if isinstance(seen.get(conv), dict) else None
        last_sent = (sent_times or {}).get(conv, "") if sent_times is not None else None
        v = {
            "id": mid, "conversation_id": conv, "received": received, "kind": kind,
            "subject": rec.get("subject") or "", "from_domain": rec.get("from_domain") or "",
            "offered_times": list(res.get("offered_times") or []), "ask": str(res.get("ask") or "")[:160],
            "row": mid in rows_by_msg or conv in sched_convs,
            "row_ids": sorted(rows_by_msg.get(mid, [])),
            "thread": bool(thread),
            "card": bool(thread and thread in card_threads),
            "answered": (None if last_sent is None else bool(last_sent and last_sent > received)),
        }
        v["reached"] = v["row"] or v["thread"] or v["card"]
        out.append(v)
    if out:
        out[0]["_calendar_responses"] = calendar
    return out


def summarize(verdicts: list[dict]) -> dict:
    by_kind: dict = {}
    for k in KINDS:
        vs = [v for v in verdicts if v["kind"] == k]
        n = len(vs)
        by_kind[k] = {
            "records": n,
            "row": sum(1 for v in vs if v["row"]),
            "thread": sum(1 for v in vs if v["thread"]),
            "card": sum(1 for v in vs if v["card"]),
            "answered": sum(1 for v in vs if v["answered"]),
            "unreached": sum(1 for v in vs if not v["reached"]),
            "unreached_and_unanswered": sum(1 for v in vs if not v["reached"] and v["answered"] is False),
        }
    return {"by_kind": by_kind, "total": len(verdicts),
            "calendar_responses": (verdicts[0].get("_calendar_responses", 0) if verdicts else 0),
            "unreached": sum(1 for v in verdicts if not v["reached"]),
            "misses": [v for v in verdicts if not v["reached"]]}


def render(summary: dict, days: int, mail: bool) -> str:
    lines = [f"Scheduling messages, last {days} days: {summary['total']} "
             f"({'with' if mail else 'without'} the Sent join; "
             f"{summary.get('calendar_responses', 0)} calendar auto-responses set aside)"]
    lines.append(f"{'kind':16} {'messages':>8} {'row':>5} {'thread':>6} {'card':>5} {'answered':>8} "
                 f"{'unreached':>9} {'+unanswered':>11}")
    for k, m in summary["by_kind"].items():
        lines.append(f"{k:16} {m['records']:>8} {m['row']:>5} {m['thread']:>6} {m['card']:>5} "
                     f"{m['answered']:>8} {m['unreached']:>9} {m['unreached_and_unanswered']:>11}")
    lines.append(f"unreached in all: {summary['unreached']} of {summary['total']}")
    return "\n".join(lines)


# ── the loaders (owner-side) ──────────────────────────────────────────

def load_records():
    import remote_llm
    import mail_privacy
    return remote_llm.read(mail_privacy.EXTRACTIONS)


def load_ledger_joins() -> tuple[dict, set]:
    """rows_by_msg: message id -> [row ids]; sched_convs: conversations
    with a sched- row (their source ids may differ from the record's)."""
    import ledger as L
    db = L.guarded_connect(L.DB_PATH)
    db.row_factory = sqlite3.Row
    rows_by_msg: dict = defaultdict(list)
    sched_convs: set = set()
    for r in db.execute("SELECT id, source_message_id, conversation_id FROM ledger"):
        if r["source_message_id"]:
            rows_by_msg[r["source_message_id"]].append(r["id"])
        if str(r["id"]).startswith("sched-") and r["conversation_id"]:
            sched_convs.add(r["conversation_id"])
    db.close()
    return dict(rows_by_msg), sched_convs


def load_seen() -> dict:
    try:
        return json.loads((PRIVATE / "scheduling-proactive-seen.json").read_text())
    except (OSError, ValueError):
        return {}


def load_card_threads() -> set:
    """Threads that got an event or invite card, or a staged event."""
    threads: set = set()
    try:
        cards = json.loads((PRIVATE / "chat-cards.json").read_text())
        for v in cards.values():
            if isinstance(v, dict) and v.get("kind") in ("event", "invite", "event_update") and v.get("thread_ts"):
                threads.add(v["thread_ts"])
    except (OSError, ValueError):
        pass
    for name in ("claude-staged.jsonl", "ab-events.jsonl"):
        try:
            for line in (PRIVATE / "remote-llm" / name).read_text().splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("thread_ts"):
                    threads.add(d["thread_ts"])
        except OSError:
            pass
    return threads


def load_sent_times(days: int) -> dict:
    import graph_client as graph
    out: dict = {}
    for m in graph.get_sent(days_back=days + 2):
        cid = m.get("conversationId")
        ts = m.get("sentDateTime") or m.get("receivedDateTime", "")
        if cid and ts > out.get(cid, ""):
            out[cid] = ts
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-mail", action="store_true", help="skip the Sent-folder join")
    ap.add_argument("--json", default="", help="write the verdicts and the summary here")
    ap.add_argument("--misses", type=int, default=25, help="how many misses to print")
    args = ap.parse_args(argv)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = load_records()
    rows_by_msg, sched_convs = load_ledger_joins()
    seen = load_seen()
    card_threads = load_card_threads()
    sent_times = None if args.no_mail else load_sent_times(args.days)
    verdicts = judge(records, rows_by_msg=rows_by_msg, sched_convs=sched_convs, seen=seen,
                     card_threads=card_threads, sent_times=sent_times, since=since)
    summary = summarize(verdicts)
    print(render(summary, args.days, sent_times is not None))
    print("\nmisses (no row, no thread, no card), newest first:")
    for v in sorted(summary["misses"], key=lambda v: v["received"], reverse=True)[:args.misses]:
        ans = {None: "?", True: "answered", False: "UNANSWERED"}[v["answered"]]
        print(f"  {v['received'][:16]} {v['kind']:15} {ans:10} {v['from_domain'][:22]:22} {v['subject'][:48]}")
    if args.json:
        Path(args.json).write_text(json.dumps({"since": since, "days": args.days, "summary": summary,
                                               "verdicts": verdicts}, indent=1))
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
