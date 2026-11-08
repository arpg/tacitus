#!/usr/bin/env python3
"""The scheduling state of one mail thread, read from the times in it.

No model: when the user's last message in a thread names clock times, the
thread is awaiting a pick, and the next inbound either takes one of those
times (an acceptance), names others (a counter) or says nothing with a
clock in it. sched_agent.extract_times reads the times.

  analyze(messages, user_addrs)  -> {"state": "none" | "awaiting-pick" | "accepted" | "counter" | "several",
                                     "offered": [...], "offered_at": iso, "accepted": {...} | None,
                                     "counter": [...], "inbound_at": iso}
  offers_from_sent(sent_full)    -> {conversation_id: {"at", "times", "id", "subject", "from", "to", "cc", "text"}}
  classify_reply(text, offered)  -> ("accepted" | "counter" | "several" | "none", picks, others)
  match_offered(times, offered)  -> the offered time one of `times` names, or None
  new_text(text)                 -> the message's own words: HTML made text, quoted history cut

Only a message's own words count. A reply quotes the offer under an "On
<date> <name> wrote:" line or an Outlook header block; the quoted times
are the offer's, not a pick, and the attribution's own clock is no time
at all. An offered window ("2:00 to 3:00 pm") is taken by a reply that
names it or a time inside it ("2:30 works"); the pick then carries the
window it fell in.

`messages` is get-conversation's shape (from.address, sent, text) or
Graph's (from.emailAddress.address, sentDateTime/receivedDateTime, body
or bodyPreview); both are read. The CLI prints the analysis for a thread:

  python3 sched_thread_state.py analyze --thread-file <get-conversation json> [--user <addr>]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

OFFER_TEXT_CHARS = 4000


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


def _name(m: dict) -> str:
    f = m.get("from") or {}
    if isinstance(f, dict):
        inner = f.get("emailAddress") if isinstance(f.get("emailAddress"), dict) else f
        return str(inner.get("name") or "")
    return ""


def addrs(m: dict, key: str) -> list[str]:
    """The addresses on one recipient line ("to", "cc"), lower case, in
    either message shape."""
    graph_key = {"to": "toRecipients", "cc": "ccRecipients"}.get(key, key)
    out = []
    for r in m.get(graph_key) or m.get(key) or []:
        if isinstance(r, dict):
            a = r.get("emailAddress", {}).get("address") if isinstance(r.get("emailAddress"), dict) else r.get("address")
        else:
            a = r
        a = str(a or "").strip().lower()
        if a and "@" in a and a not in out:
            out.append(a)
    return out


def _when(m: dict) -> str:
    return str(m.get("sent") or m.get("sentDateTime") or m.get("received") or m.get("receivedDateTime") or "")


_HTML_HINT = re.compile(r"<(?:html|body|div|p|br|span|table|td|li)\b", re.I)
_BLOCK_END = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>")
_TAG = re.compile(r"<[^>]+>")
# Where quoted history starts: a ">" line, an Outlook header block or its
# rule, a forwarded or original-message marker, or an attribution line. A
# preview cuts an attribution short, so an "On ..." line that carries an
# address bracket counts without its "wrote:".
_QUOTE_START = re.compile(
    r"^[ \t]*(?:>|From:[ \t]|-{2,}[ \t]*(?:Original|Forwarded) [Mm]essage|_{8,}[ \t]*$|Sent from my\b)"
    r"|^[ \t]*On[ \t][^\n]{4,200}?(?:wrote:[ \t]*$|<[^\n]*$)",
    re.M)


def new_text(text: str) -> str:
    """The words a message adds: HTML made text, quoted history cut."""
    text = str(text or "")
    if _HTML_HINT.search(text):
        text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
        text = re.sub(r"(?is)<blockquote.*", " ", text)
        text = re.sub(r"(?is)<div[^>]*(?:id=\"?(?:divRplyFwdMsg|appendonsend)|class=\"?gmail_quote).*", " ", text)
        text = html.unescape(_TAG.sub(" ", _BLOCK_END.sub("\n", text)))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _QUOTE_START.search(text)
    return text[:m.start()] if m else text


def _text(m: dict) -> str:
    body = m.get("body")
    if isinstance(body, dict):
        body = body.get("content")
    return new_text(str(m.get("text") or body or m.get("bodyPreview") or ""))


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


def _dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _same_instant(a: str, b: str) -> bool:
    da, db = _dt(a), _dt(b)
    return bool(da and db and da.astimezone(timezone.utc) == db.astimezone(timezone.utc))


def _taken(t: dict, offered: list[dict]) -> dict | None:
    """The slot `t` takes from the offer, or None: the offered time it
    names, or a time inside an offered window. {"start", "end", "named_end",
    "window"}: `end` is the end `t` names, else the offered slot's end for
    a named offered start; `named_end` says which; `window` is the offered
    slot it fell in."""
    named = bool(t.get("end"))
    for o in offered or []:
        if _same_instant(t["start"], o["start"]):
            return {"start": o["start"], "end": t.get("end") or o.get("end"), "named_end": named,
                    "window": {"start": o["start"], "end": o.get("end")}}
    s, e = _dt(t["start"]), _dt(t.get("end") or "")
    for o in offered or []:
        os_, oe = _dt(o["start"]), _dt(o.get("end") or "")
        if s and os_ and oe and os_ < s < oe and (e is None or e <= oe):
            return {"start": t["start"], "end": t.get("end"), "named_end": named,
                    "window": {"start": o["start"], "end": o["end"]}}
    return None


def match_offered(times: list, offered: list[dict], *, tz: ZoneInfo | None = None) -> dict | None:
    """The offered slot that one of `times` names, or None. `times` are
    ISO strings as the remote record gives them; one with no offset is
    the user's local time. A time inside an offered window takes it."""
    zone = tz or _tz()
    for t in times or []:
        dt = _dt(t)
        if dt is None:
            continue
        dt = dt if dt.tzinfo else dt.replace(tzinfo=zone)
        slot = _taken({"start": dt.isoformat(), "end": None}, offered)
        if slot is not None:
            return slot
    return None


def classify_reply(text: str, offered: list[dict], *, today=None, tz: ZoneInfo | None = None) -> tuple[str, list[dict], list[dict]]:
    """What a reply does with the offered times: ("accepted", [the slot],
    others), ("several", [slots], others), ("counter", [], [others]) or
    ("none", [], []). Only the reply's own words are read."""
    named = times_in(new_text(text), today=today, tz=tz)
    picks, others = [], []
    for t in named:
        slot = _taken(t, offered)
        if slot is None:
            others.append(t)
        elif not any(_same_instant(slot["start"], p["start"]) for p in picks):
            picks.append(slot)
    if len(picks) == 1:
        return "accepted", picks, others
    if len(picks) > 1:
        return "several", picks, others
    if others:
        return "counter", [], others
    return "none", [], []


# ── the thread ────────────────────────────────────────────────────────

def thread_order(messages: list[dict]) -> list[dict]:
    """The thread's messages, oldest first: the order analyze indexes."""
    return sorted((m for m in messages or [] if isinstance(m, dict)), key=_when)


def analyze(messages: list[dict], user_addrs: set | list, *, today=None, tz: ZoneInfo | None = None) -> dict:
    """The thread's state from its times, oldest message first. With an
    acceptance, `offer_index` and `inbound_index` name the offer and the
    reply in thread_order(messages)."""
    user = {a.lower() for a in (user_addrs or [])}
    msgs = thread_order(messages)
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
    for j, m in enumerate(msgs[offer_i + 1:], offer_i + 1):
        if _own(m, user):
            # The user wrote again after the offer without new times: the
            # offer stands. With new times the loop above moved the offer.
            continue
        kind, picks, others = classify_reply(_text(m), out["offered"], today=today, tz=tz)
        out["inbound_at"] = _when(m)
        if kind == "accepted":
            out.update(state="accepted", accepted=picks[0], counter=others,
                       offer_index=offer_i, inbound_index=j)
            return out
        if kind == "several":
            out.update(state="several", counter=picks + others)
            return out
        if kind == "counter":
            out.update(state="counter", counter=others)
            return out
    return out


def offer_of(m: dict, times: list[dict]) -> dict:
    """The parts of the user's offer message that staging an invite reads:
    when, the times, the headers and its own words (capped). It stays in
    the owner process; nothing here goes into a queue."""
    return {"at": _when(m), "times": times, "id": m.get("id") or "",
            "subject": str(m.get("subject") or ""), "from": _addr(m),
            "to": addrs(m, "to"), "cc": addrs(m, "cc"), "text": _text(m)[:OFFER_TEXT_CHARS]}


def offers_from_sent(sent: list[dict], *, today=None, tz: ZoneInfo | None = None) -> dict:
    """The user's latest message with times per conversation, from a
    Sent-folder fetch with bodies: {conversation_id: offer_of(...)}."""
    out: dict = {}
    for m in sorted((m for m in sent or [] if isinstance(m, dict)), key=_when):
        conv = m.get("conversationId") or m.get("conversation_id") or ""
        if not conv:
            continue
        ts = times_in(_text(m), today=today, tz=tz)
        if ts:
            out[conv] = offer_of(m, ts)
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
