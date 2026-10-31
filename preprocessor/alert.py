#!/usr/bin/env python3
"""Timed alerts for ledger rows: a direct message to the owner, scheduled
on the chat server, before a window opens or closes.

A row whose moment is a clock time (tickets on sale at 10:00, a
registration that opens at noon, a submission portal that closes at
5 pm) is not served by the morning briefing alone: the briefing is one
stream post at 04:00. The alert is a direct message, scheduled in
advance for `--lead` minutes before the moment, delivered by the Zulip
server whether or not the bot is up. Each alert is recorded on the row
(`alert_at`, `alert_scheduled_id`, `alert_lead_min`) so it is not set
twice and can be cancelled.

  alert.py schedule --id <ledger id> --at 2026-10-06T10:00 [--lead 15] [--text ...]
  alert.py list
  alert.py cancel --id <ledger id>
  alert.py flush            # schedule alerts staged by lanes without the chat credential

The web chat (when the chat block runs it) also gets the alert as a push
notification at the same moment (chat.direct.web_notify), queued under
the tag `alert:<ledger id>`, which `cancel` and `--force` replace.

`--at` is the user's local time (user_tz) unless it carries an offset. A lane
that cannot reach the owner's chat credential (the resident runs as
ea-bot) stages the request in state/pending-alerts.jsonl and prints
{"staged": true}; the owner's cron runs `flush` every few minutes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import resident_io  # noqa: E402  (writes into resident space go through it)
import user_tz  # noqa: E402
EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
LOCAL_TZ = user_tz.zone(EA_DIR)
PENDING = EA_DIR / "state" / "pending-alerts.jsonl"
LEDGER = [sys.executable, str(HERE / "ledger.py")]
_LEDGER_TAG_RE = re.compile(r"Ledger `([^`]+)`")


def parse_at(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _ledger(args: list[str]) -> dict:
    r = subprocess.run(LEDGER + args, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "EA_DIR": str(EA_DIR)})
    out = r.stdout.strip()
    try:
        return json.loads(out[out.find("{"):]) if "{" in out else {}
    except Exception:
        return {}


def _row(ledger_id: str) -> dict:
    d = _ledger(["get", "--id", ledger_id])
    return d.get("entry", d) if isinstance(d, dict) else {}


def _alert_on(row: dict) -> dict:
    """The alert fields the row carries. ledger.py merges metadata keys
    into the row itself, so they are read at the top level; an empty
    string (a cleared alert) counts as none."""
    return {k: row.get(k) for k in ("alert_at", "alert_scheduled_id", "alert_lead_min",
                                     "alert_fires_at") if row.get(k) not in (None, "")}


def _stage(job: dict) -> bool:
    """Append one staged alert for `flush` (resident space, via resident_io)."""
    resident_io.make_dir(PENDING.parent)
    return resident_io.append_text(PENDING, json.dumps(job) + "\n")


def _rewrite_pending(jobs: list[dict]) -> bool:
    """Replace the staged alerts with `jobs` (the ones still to retry)."""
    return resident_io.replace_text(PENDING, "".join(json.dumps(j) + "\n" for j in jobs))


def default_text(row: dict, ledger_id: str, at: datetime, lead: int) -> str:
    desc = (row.get("description") or ledger_id).strip()
    url = ""
    m = re.search(r"https?://\S+", (row.get("notes") or "") + " " + desc)
    if m:
        url = m.group(0).rstrip(").,;")
    when = at.astimezone(LOCAL_TZ).strftime("%-I:%M %p").lower()
    text = (f":rotating_light: In {lead} minutes, at {when} MT: {desc}")
    if url and url not in desc:
        text += f"\n{url}"
    return text + f"\nLedger `{ledger_id}`."


def schedule(ledger_id: str, at: datetime, lead: int, text: str | None,
             force: bool = False) -> dict:
    row = _row(ledger_id)
    if not row:
        return {"error": f"ledger row {ledger_id} not found"}
    meta = _alert_on(row)
    if meta.get("alert_scheduled_id") and not force:
        return {"already": True, "alert_at": meta.get("alert_at"),
                "scheduled_id": meta.get("alert_scheduled_id"),
                "hint": "use --force to replace it"}
    when = at - timedelta(minutes=lead)
    now = datetime.now(timezone.utc)
    body = text or default_text(row, ledger_id, at, lead)
    if f"Ledger `{ledger_id}`" not in body:
        body = body.rstrip() + f"\nLedger `{ledger_id}`."
    if at <= now:
        return {"error": f"{at.isoformat()} is in the past"}
    try:
        from chat import direct  # type: ignore
        fe = direct.frontend()
    except Exception as e:  # no credential in this lane: stage for flush
        if not _stage({"id": ledger_id, "at": at.isoformat(), "lead": lead,
                       "text": body, "staged_at": now.isoformat(), "force": force}):
            return {"error": "could not stage the alert", "text": body}
        return {"staged": True, "reason": f"no chat credential here ({type(e).__name__})",
                "flush": "alert.py flush runs as the owner"}
    tag = f"alert:{ledger_id}"
    if when <= now:
        res = direct.send_direct(body, fe)
        sid = res.get("id", 0)
        kind = "sent now (lead already passed)"
        direct.web_cancel(tag)  # a push queued for this row before is superseded
        web = direct.web_notify(body, tag=tag)
    else:
        sid = direct.schedule_direct(body, when, fe)
        kind = "scheduled"
        web = direct.web_notify(body, tag=tag, when=when)
    if meta.get("alert_scheduled_id") and force:
        try:
            direct.cancel_scheduled(int(meta["alert_scheduled_id"]), fe)
        except Exception:
            pass
    _ledger(["update", "--id", ledger_id, "--set",
             f"alert_at={at.isoformat()}", f"alert_scheduled_id={sid}",
             f"alert_lead_min={lead}", f"alert_fires_at={when.isoformat()}"])
    return {"ok": True, "kind": kind, "ledger_id": ledger_id, "scheduled_id": sid,
            "fires_at": when.astimezone(LOCAL_TZ).isoformat(), "text": body, "web": web}


def cancel(ledger_id: str) -> dict:
    row = _row(ledger_id)
    sid = _alert_on(row).get("alert_scheduled_id")
    if not sid:
        return {"error": f"no alert recorded on {ledger_id}"}
    from chat import direct  # type: ignore
    ok = direct.cancel_scheduled(int(sid))
    direct.web_cancel(f"alert:{ledger_id}")
    _ledger(["update", "--id", ledger_id, "--set", "alert_scheduled_id=",
             "alert_at=", "alert_fires_at="])
    return {"ok": ok, "cancelled": sid}


def list_alerts() -> dict:
    from chat import direct  # type: ignore
    out = []
    for m in direct.list_scheduled():
        tag = _LEDGER_TAG_RE.search(m["content"])
        out.append({"scheduled_id": m["id"], "fires_at": m["at"].astimezone(LOCAL_TZ).isoformat(),
                    "ledger_id": tag.group(1) if tag else "", "text": m["content"][:160]})
    staged = []
    for line in (resident_io.read_text(PENDING) or "").splitlines():
        try:
            staged.append(json.loads(line))
        except Exception:
            continue
    return {"scheduled": out, "staged": staged}


def flush() -> dict:
    text = resident_io.read_text(PENDING)
    if not text:
        return {"flushed": 0}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    done, failed = [], []
    for ln in lines:
        try:
            job = json.loads(ln)
            res = schedule(job["id"], parse_at(job["at"]), int(job.get("lead", 15)),
                           job.get("text"), force=bool(job.get("force")))
            (done if res.get("ok") or res.get("already") else failed).append({**job, "result": res})
        except Exception as e:
            failed.append({"line": ln[:200], "error": str(e)})
    _rewrite_pending([f for f in failed if "line" not in f])
    return {"flushed": len(done), "failed": len(failed), "done": done, "failed_jobs": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("schedule", allow_abbrev=False)
    p.add_argument("--id", required=True, help="ledger row id")
    p.add_argument("--at", required=True, help="the moment, ISO local time, e.g. 2026-10-06T10:00")
    p.add_argument("--lead", type=int, default=15, help="minutes before the moment (default 15)")
    p.add_argument("--text", default=None, help="message text; default is built from the row")
    p.add_argument("--force", action="store_true", help="replace an alert already on the row")
    p = sub.add_parser("cancel", allow_abbrev=False)
    p.add_argument("--id", required=True)
    sub.add_parser("list", allow_abbrev=False)
    sub.add_parser("flush", allow_abbrev=False)
    a = ap.parse_args()
    if a.cmd == "schedule":
        out = schedule(a.id, parse_at(a.at), a.lead, a.text, force=a.force)
    elif a.cmd == "cancel":
        out = cancel(a.id)
    elif a.cmd == "list":
        out = list_alerts()
    else:
        out = flush()
    print(json.dumps(out, indent=1, default=str))
    return 0 if not out.get("error") else 2


if __name__ == "__main__":
    sys.exit(main())
