#!/usr/bin/env python3
"""The scheduling state of one mail thread, read from the times in it.

No model: when the user's last message in a thread names clock times, the
thread is awaiting a pick, and the next inbound either takes one of those
times (an acceptance), names others (a counter) or says nothing with a
clock in it. sched_agent.extract_times reads the times.

  analyze(messages, user_addrs)  -> {"state": "none" | "awaiting-pick" | "accepted" | "counter" | "several",
                                     "offered": [...], "offered_at": iso, "accepted": {...} | None,
                                     "counter": [...], "inbound_at": iso}
  offers_from_sent(sent_full)    -> {conversation_id: {"at", "times", "text"}}
  classify_reply(text, offered)  -> ("accepted" | "counter" | "several" | "none", picks, others)

`messages` is get-conversation's shape (from.address, sent, text) or
Graph's (from.emailAddress.address, sentDateTime/receivedDateTime, body
or bodyPreview); both are read. The CLI prints the analysis for a thread:

  python3 sched_thread_state.py analyze --thread-file <get-conversation json> [--user <addr>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

def _tz(name: str | None = None) -> ZoneInfo:
    """The user's zone: the name given, else the scheduling agent's
    default, which comes from the install's config."""
    from sched_agent import DEFAULT_TZ
    try:
        return ZoneInfo(name or os.environ.get("EA_TZ") or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


# ── one message, whatever its shape ───────────────────────────────────

def _addr(m: dict) -> str:
    f = m.get("from") or {}
    if isinstance(f, dict):
        if isinstance(f.get("emailAddress"), dict):
            return str(f["emailAddress"].get("address") or "").lower()
        return str(f.get("address") or "").lower()
    return str(f or "").lower()


def _when(m: dict) -> str:
    return str(m.get("sent") or m.get("sentDateTime") or m.get("received") or m.get("receivedDateTime") or "")


def _text(m: dict) -> str:
    body = m.get("body")
    if isinstance(body, dict):
        body = body.get("content")
    return str(m.get("text") or body or m.get("bodyPreview") or "")


def _own(m: dict, user_addrs: set) -> bool:
    return bool(m.get("own")) or _addr(m) in user_addrs


# ── times ─────────────────────────────────────────────────────────────

def times_in(text: str, *, today=None, tz: ZoneInfo | None = None) -> list[dict]:
    """[{"start": iso, "end": iso | None}] for the clock times a text names."""
    from sched_agent import extract_times
    found, _ = extract_times(text or "", today=today, tz=tz or _tz())
    out = []
    for t in found:
        out.append({"start": t["start"].isoformat(), "end": t["end"].isoformat() if t.get("end") else None})
    return out


def _same_instant(a: str, b: str) -> bool:
    try:
        return datetime.fromisoformat(a).astimezone(timezone.utc) == datetime.fromisoformat(b).astimezone(timezone.utc)
    except ValueError:
        return False


def classify_reply(text: str, offered: list[dict], *, today=None, tz: ZoneInfo | None = None) -> tuple[str, list[dict], list[dict]]:
    """What a reply does with the offered times: ("accepted", [the one],
    []), ("several", [picks], others), ("counter", [], [others]) or
    ("none", [], [])."""
    named = times_in(text, today=today, tz=tz)
    picks = [t for t in named if any(_same_instant(t["start"], o["start"]) for o in offered)]
    others = [t for t in named if t not in picks]
    if len(picks) == 1:
        return "accepted", picks, others
    if len(picks) > 1:
        return "several", picks, others
    if others:
        return "counter", [], others
    return "none", [], []


# ── the thread ────────────────────────────────────────────────────────

def analyze(messages: list[dict], user_addrs: set | list, *, today=None, tz: ZoneInfo | None = None) -> dict:
    """The thread's state from its times, oldest message first."""
    user = {a.lower() for a in (user_addrs or [])}
    msgs = sorted((m for m in messages or [] if isinstance(m, dict)), key=_when)
    out = {"state": "none", "offered": [], "offered_at": "", "accepted": None, "counter": [], "inbound_at": ""}
    offer_i = None
    for i, m in enumerate(msgs):
        if _own(m, user):
            ts = times_in(_text(m), today=today, tz=tz)
            if ts:
                offer_i, out["offered"], out["offered_at"] = i, ts, _when(m)
    if offer_i is None:
        return out
    out["state"] = "awaiting-pick"
    for m in msgs[offer_i + 1:]:
        if _own(m, user):
            # The user wrote again after the offer without new times: the
            # offer stands. With new times the loop above moved the offer.
            continue
        kind, picks, others = classify_reply(_text(m), out["offered"], today=today, tz=tz)
        out["inbound_at"] = _when(m)
        if kind == "accepted":
            out.update(state="accepted", accepted=picks[0], counter=others)
            return out
        if kind == "several":
            out.update(state="several", counter=picks + others)
            return out
        if kind == "counter":
            out.update(state="counter", counter=others)
            return out
    return out


def offers_from_sent(sent: list[dict], *, today=None, tz: ZoneInfo | None = None) -> dict:
    """The user's latest message with times per conversation, from a
    Sent-folder fetch with bodies: {conversation_id: {"at", "times"}}."""
    out: dict = {}
    for m in sorted((m for m in sent or [] if isinstance(m, dict)), key=_when):
        conv = m.get("conversationId") or m.get("conversation_id") or ""
        if not conv:
            continue
        ts = times_in(_text(m), today=today, tz=tz)
        if ts:
            out[conv] = {"at": _when(m), "times": ts}
    return out


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze", allow_abbrev=False)
    p.add_argument("--thread-file", required=True, help="a get-conversation JSON")
    p.add_argument("--user", action="append", default=[], help="the user's address (repeatable)")
    p.add_argument("--tz", default="")
    args = ap.parse_args(argv)
    d = json.loads(Path(args.thread_file).read_text())
    msgs = d.get("messages") if isinstance(d, dict) else d
    users = set(args.user)
    if not users:
        try:
            prof = json.loads((Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "config" / "user-profile.json").read_text())
            users = {str(prof.get("email") or "").lower()} | {str(a).lower() for a in prof.get("aliases") or []}
        except (OSError, ValueError):
            users = set()
    print(json.dumps(analyze(msgs, users, tz=_tz(args.tz or None)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
