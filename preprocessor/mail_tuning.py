#!/usr/bin/env python3
"""Mail tuning (experimental): the harness that finds the extraction setup
the remote model answers best with (docs/remote-llm.md, "Tuning").

The remote model has two jobs per message. It decides: does the message
need a reply, by when, what should the assistant do, how urgent is it, is
it sensitive. And it represents: a brief that Claude would read in place
of the mail. The decisions are fixed questions, scored against what
happened (the user's own replies, the ledger) and against reference labels
Claude writes from the raw mail. Everything else evolves: the
instructions, the brief, how much of the thread the model sees, worked
examples, the model, thinking and voting.

Verbs (owner process; all data stays in the private store, tuning/):
    dataset [--since YYYY-MM-DD]  cache the text and thread of each extracted message
    outcomes                      direct-reply and ledger labels per message
    label [--limit N]             Claude reference labels (batches of 10)
    seed                          the starting candidates
    eval --candidate ID [--split dev|val|test] [--limit N]
    evolve [--rounds N] [--children N]   reflective evolution on dev, finalists on val
    board [--split dev|val|test]  candidates ranked by score
    freeze --candidate ID         write extract-prompt.json, which the live tick then uses

Splits are by date received: dev before VAL_FROM, val from VAL_FROM until
the freeze, test after it. A reply label is mature three days after the
message arrived.

Mail labelled keep_from_anthropic never goes to Claude here: it gets no
reference label and never appears among the failures Claude reflects on.
Candidates are scored on it by the outcome label alone
(nr_recall_outcome_kept), which needs no Claude.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm  # noqa: E402
import claude_cli  # noqa: E402

TUNE = "tuning"
MESSAGES = f"{TUNE}/messages.jsonl"
OUTCOMES = f"{TUNE}/outcomes.json"
LABELS = f"{TUNE}/labels.jsonl"
CANDIDATES = f"{TUNE}/candidates.jsonl"
RESULTS = f"{TUNE}/results.jsonl"
FROZEN = "extract-prompt.json"

VAL_FROM = "2026-10-06"
REPLY_WINDOW = timedelta(days=3)
BODY_CHARS = 8000
THREAD_CHARS = 1200
DEV_SAMPLE = 150
VAL_SAMPLE = 100
TEACHER_MODEL = "sonnet"
REFLECT_MODEL = "claude-opus-5-5"
CLAUDE_TIMEOUT_S = 900

ACTIONS = ("reply", "do", "decide", "schedule", "read", "none")
URGENCY = ("low", "normal", "high")
SENSITIVE_KINDS = ("none", "student-record", "personnel", "health", "letter",
                   "manuscript-review", "legal-financial", "admissions", "other")
SCHED_KINDS = ("none", "request", "proposes-times", "reschedule", "booking-link",
               "poll", "confirmation", "cancellation")
TONES = ("distress", "escalation", "withdrawal", "urgent", "complaint")
STATES = ("needs-reply", "todo", "decide", "scheduling", "awaiting-reply", "fyi")
DOMAINS = ("research", "service", "teaching", "professional", "personal")

# What every candidate returns. The decision fields keep their names and
# meanings so they can be scored; `brief` is free text whose content and
# length the instructions decide.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "needs_reply": {"type": "boolean"},
        "reply_by": {"type": "string"},
        "ask": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "urgency": {"type": "string", "enum": list(URGENCY)},
        "sensitive": {"type": "boolean"},
        "sensitive_kind": {"type": "string", "enum": list(SENSITIVE_KINDS)},
        "scheduling": {"type": "string", "enum": list(SCHED_KINDS)},
        "offered_times": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
            "required": ["name", "role"]}},
        "tone": {"type": "array", "items": {"type": "string", "enum": list(TONES)}},
        "state": {"type": "string", "enum": list(STATES)},
        "domain": {"type": "string", "enum": list(DOMAINS)},
        "brief": {"type": "string"},
    },
    "required": ["needs_reply", "reply_by", "ask", "action", "urgency", "sensitive",
                 "sensitive_kind", "scheduling", "offered_times", "people", "tone",
                 "state", "domain", "brief"],
}

FIELD_GUIDE = """Fields (names and meanings are fixed):
- needs_reply, reply_by (YYYY-MM-DD or ""), ask (one sentence, "" when none)
- action: reply | do (a task other than writing back) | decide | schedule (find or confirm a meeting time) | read (worth reading, nothing to do) | none (no attention needed)
- urgency: low | normal | high
- sensitive, sensitive_kind: student-record | personnel | health | letter | manuscript-review | legal-financial | admissions | other ("none" when not sensitive)
- scheduling: none | request | proposes-times | reschedule | booking-link | poll | confirmation | cancellation; offered_times: ISO 8601 local times the sender states with their day (never a date inferred from "most days" or "next week"; a day with no clock time is not an offered time)
- people: who matters to the ask, with their role relative to the user; tone: distress | escalation | withdrawal | urgent | complaint
- state: needs-reply | todo | decide | scheduling | awaiting-reply | fyi; domain: research | service | teaching | professional | personal
- brief: what the user's assistant needs to know to act, in place of reading the email"""

BASELINE_INSTRUCTIONS = """You read one email for a professor's assistant and fill a JSON record. Answer with JSON only.

- needs_reply: true when the sender expects the user to answer or act in writing.
- ask: what the sender wants from the user, one sentence.
- reply_by: the date the ask is due.
- sensitive: true when the message carries a student's records or grades, a personnel or hiring matter about a named person, health information, a letter of recommendation, a confidential manuscript or proposal review, a legal or money matter about a person, or a prospective student's application details.
- brief: what the message says, at most two sentences of plain fact."""

V1_INSTRUCTIONS = """You triage one email for a busy professor's executive assistant. The assistant will act on your record instead of reading the email, so a missed request is the worst error you can make. Answer with JSON only.

needs_reply is true when the user personally should write back to someone because of this message: a direct question, a request for information, files, feedback, a decision, a signature or a confirmation, an invitation that needs an answer, a scheduling ask, or a sender who is plainly waiting on him. A short ask inside a thank-you still counts. It is false for announcements, newsletters, mailing lists, automated notices and receipts; for a thread where the user is only copied and someone else owns the answer; and for a message that only closes a loop (thanks, confirmed, see you then) with nothing left open. Read the earlier thread messages: a message that answers a question the user asked usually needs no reply unless it asks something back.

action: reply when needs_reply is true and writing back is the main step; schedule when the main step is finding or confirming a time; do for a task (submit, review, register, pay, upload); decide when the user must choose before anything else happens; read for something worth his attention with nothing to do; none for everything he can skip.

reply_by: a date the sender gives or clearly implies for the answer or the task, as YYYY-MM-DD; "" when none.
urgency: high when the deadline is within two days, the sender is a dean, program officer or VIP waiting on him, or the tone shows distress; low for mail he can skip; normal otherwise.
sensitive: true for a student's records, grades or wellbeing; a personnel, hiring or promotion matter about a named person; health or family-medical information; a letter of recommendation or what it says; a confidential manuscript or proposal review; a legal or money matter about a person; a prospective student's application details.

brief: two to four plain sentences for the assistant: who wants what, by when, what the user has already said in the thread, and anything the assistant would otherwise have to open the email to learn (a link, a number, a room, a document name). No opinions."""


# ── small helpers ─────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _addr(rec) -> str:
    return ((rec or {}).get("emailAddress") or {}).get("address", "").strip().lower()


def _name(rec) -> str:
    return ((rec or {}).get("emailAddress") or {}).get("name", "").strip()


def _addrs(recs) -> list[str]:
    return [a for a in (_addr(r) for r in recs or []) if a]


def _private_dir(rel: str) -> Path:
    d = remote_llm.store_dir() / rel
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _write(rel: str, text: str) -> None:
    _private_dir(str(Path(rel).parent))
    _owner_trust.write_owner_file(remote_llm.store_dir() / rel, text.encode(), mode=0o600)


def _read_json(rel: str, default):
    try:
        return json.loads(_owner_trust.read_owner_file(remote_llm.store_dir() / rel))
    except FileNotFoundError:
        return default


def _rewrite_jsonl(rel: str, rows: list[dict]) -> None:
    _write(rel, "".join(json.dumps(r, default=str) + "\n" for r in rows))


def _today_local() -> str:
    return datetime.now().astimezone().strftime("%A %Y-%m-%d")


def _user_addresses() -> set[str]:
    try:
        import mail_tagger
        return mail_tagger._user_addresses()
    except Exception:
        return set()


def _profile_line() -> str:
    try:
        import mail_privacy
        return mail_privacy._profile_line()
    except Exception:
        return "The user is a university professor."


# ── the dataset ───────────────────────────────────────────────────────

def _text(msg: dict, cap: int) -> str:
    import admissions_filter as af
    body = af.body_text(msg)
    return body[:cap]


def conversation(graph, conv_id: str) -> list[dict]:
    """Every message of one thread, any folder, oldest first, with body."""
    # The filter goes in params: graph_client refuses a path with a query.
    rows = graph.graph_get_all(
        "/me/messages",
        {"$filter": f"conversationId eq '{str(conv_id).replace(chr(39), chr(39) * 2)}'",
         "$select": graph.MAIL_FIELDS_FULL, "$top": 50})
    rows = [r for r in rows if not r.get("isDraft")]
    rows.sort(key=lambda r: r.get("receivedDateTime") or r.get("sentDateTime") or "")
    return rows


def _entry(m: dict, user: set[str], cap: int) -> dict:
    frm = _addr(m.get("from"))
    return {"from": frm, "name": _name(m.get("from")), "own": frm in user,
            "sent": m.get("receivedDateTime") or m.get("sentDateTime") or "",
            "text": _text(m, cap)}


def thread_before(conv: list[dict], received: str, user: set[str], k: int = 6) -> list[dict]:
    """Up to `k` messages of the thread that came before `received`."""
    t0 = _parse(received)
    prior = [m for m in conv if (_parse(m.get("receivedDateTime") or m.get("sentDateTime")) or t0) < t0]
    return [_entry(m, user, THREAD_CHARS) for m in prior[-k:]]


def build_dataset(*, graph=None, since: str | None = None, refresh: bool = False,
                  log=print) -> dict:
    """Cache every extracted message (the live store's records) with its
    text and the thread before it. One Graph read per thread. `refresh`
    reads every thread again, so later replies are seen."""
    import mail_privacy
    graph = graph or mail_privacy._default_graph()
    user = _user_addresses()
    have = set() if refresh else {r["key"] for r in remote_llm.read(MESSAGES)}
    recs = [r for r in mail_privacy.load_index()["by_key"].values()
            if r.get("conversation_id") and r["key"] not in have
            and (not since or str(r.get("received", ""))[:10] >= since)]
    by_conv: dict[str, list[dict]] = {}
    for r in recs:
        by_conv.setdefault(r["conversation_id"], []).append(r)
    added = failed = 0
    for i, (cid, rs) in enumerate(by_conv.items(), 1):
        try:
            conv = conversation(graph, cid)
        except Exception as e:
            failed += len(rs)
            log(f"  thread {i}: {str(e)[:120]}")
            continue
        for r in rs:
            match = [m for m in conv if (m.get("receivedDateTime") or "") == r.get("received")]
            if not match:
                failed += 1
                continue
            m = match[0]
            row = {"key": r["key"], "id": m.get("id"), "conversation_id": cid,
                   "received": r.get("received"),
                   "from": _addr(m.get("from")), "from_name": _name(m.get("from")),
                   "to": _addrs(m.get("toRecipients"))[:12], "cc": _addrs(m.get("ccRecipients"))[:12],
                   "subject": (m.get("subject") or "")[:200],
                   "has_attachments": bool(m.get("hasAttachments")),
                   "body": _text(m, BODY_CHARS),
                   "thread": thread_before(conv, r.get("received"), user),
                   "after": [{"own": _addr(x.get("from")) in user,
                              "sent": x.get("receivedDateTime") or x.get("sentDateTime") or "",
                              "to": (_addrs(x.get("toRecipients")) + _addrs(x.get("ccRecipients")))[:20]
                              if _addr(x.get("from")) in user else []}
                             for x in conv
                             if (_parse(x.get("receivedDateTime") or x.get("sentDateTime")) or _now())
                             > (_parse(r.get("received")) or _now())],
                   "keep_from_anthropic": r.get("keep_from_anthropic", "")}
            remote_llm.append(MESSAGES, row)
            added += 1
        if i % 25 == 0:
            log(f"  {i}/{len(by_conv)} threads, {added} messages")
    if refresh:
        _rewrite_jsonl(MESSAGES, list(messages().values()))
    return {"threads": len(by_conv), "added": added, "failed": failed}


def messages() -> dict[str, dict]:
    """The cached messages by key, the last row per key."""
    out = {}
    for r in remote_llm.read(MESSAGES):
        out[r["key"]] = r
    return out


# ── outcome labels ────────────────────────────────────────────────────

_AUTO_SUBJECT = re.compile(
    r"^\s*(accepted|declined|tentative|tentatively accepted|canceled|cancelled|updated invitation"
    r"|invitation|new time proposed|automatic reply|out of office|undeliverable|delivery status)\b",
    re.I)
_AUTO_FROM = re.compile(r"(no-?reply|do-?not-?reply|notifications?@|mailer-daemon|postmaster@)", re.I)


def automated(row: dict) -> bool:
    """A calendar response, an auto-reply or a robot sender: mail a person
    never answers as such."""
    return bool(_AUTO_SUBJECT.search(row.get("subject") or "")
                or _AUTO_FROM.search(row.get("from") or ""))


def outcome_for(row: dict, now: datetime | None = None) -> dict:
    """What the user did. `replied`: a message of his in the thread within
    three days. `replied_direct`: his first such message came before anyone
    else wrote again, went to this message's sender, and this message is
    not automated mail."""
    now = now or _now()
    t0 = _parse(row.get("received"))
    if not t0:
        return {"mature": False}
    after = sorted(((_parse(a["sent"]), a) for a in row.get("after") or [] if _parse(a.get("sent"))),
                   key=lambda x: x[0])
    own = [(t, a) for t, a in after if a.get("own") and t <= t0 + REPLY_WINDOW]
    auto = automated(row)
    direct = False
    if own and not auto:
        first, first_rec = own[0]
        uninterrupted = not any(not a.get("own") and t < first for t, a in after)
        to = first_rec.get("to")
        addressed = (row.get("from") or "") in to if to is not None and "to" in first_rec else True
        direct = uninterrupted and addressed
    return {"mature": now - t0 >= REPLY_WINDOW, "replied": bool(own),
            "replied_direct": direct, "automated": auto}


def build_outcomes(now: datetime | None = None) -> dict:
    import mail_privacy
    msgs = messages()
    ledger_rows = mail_privacy._ledger_rows(_now() - timedelta(days=60))
    by_conv: dict[str, list[dict]] = {}
    for row in ledger_rows:
        by_conv.setdefault(row["conversation_id"], []).append(row)
    out = {}
    for k, row in msgs.items():
        o = outcome_for(row, now)
        rows = by_conv.get(row["conversation_id"]) or []
        o["ledger_rows"] = len(rows)
        o["ledger_deadline"] = next((str(r["deadline"])[:10] for r in rows if r.get("deadline")), "")
        out[k] = o
    _write(OUTCOMES, json.dumps(out))
    mature = [o for o in out.values() if o.get("mature")]
    return {"messages": len(out), "mature": len(mature),
            "replied_direct": sum(1 for o in mature if o.get("replied_direct"))}


# ── rendering a message for a model ───────────────────────────────────

def render(row: dict, context: int) -> str:
    parts = []
    thread = (row.get("thread") or [])[-context:] if context else []
    if thread:
        parts.append("EARLIER IN THIS THREAD (oldest first):")
        for t in thread:
            who = "the user" if t.get("own") else f"{t.get('name') or ''} <{t.get('from')}>"
            parts.append(f"[{str(t.get('sent'))[:16]}] From {who}:\n{t.get('text')}\n")
        parts.append("THE MESSAGE TO TRIAGE:")
    parts.append(f"From: {row.get('from_name') or ''} <{row.get('from')}>\n"
                 f"To: {', '.join(row.get('to') or [])}\n"
                 f"Cc: {', '.join(row.get('cc') or [])}\n"
                 f"Date: {row.get('received')}\n"
                 f"Subject: {row.get('subject')}\n"
                 f"Attachments: {'yes' if row.get('has_attachments') else 'no'}\n\n"
                 f"{row.get('body')}")
    return "\n".join(parts)


# ── Claude, for reference labels and reflection ───────────────────────

def run_claude(prompt: str, model: str, timeout: float = CLAUDE_TIMEOUT_S) -> str:
    """One tool-less `claude -p` turn as ea-bot, prompt on stdin. Returns
    the result text ('' on failure)."""
    import mail_privacy
    home = mail_privacy.EA_BOT_HOME
    claude_bin = mail_privacy._claude_bin()
    args = ["-p", "--model", model, "--output-format", "json",
            "--strict-mcp-config", "--no-session-persistence", "--tools", ""]
    cmd = (["sudo", "-n", "-u", "ea-bot", "env", f"HOME={home}", f"EA_DIR={mail_privacy.EA_DIR}",
            f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}"]
           + [f"{k}={v}" for k, v in claude_cli.no_connector_env(args).items()] + [claude_bin]
           + claude_cli.apply(args, claude_bin=claude_bin, as_ea_bot=True))
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=str(home))
    except subprocess.TimeoutExpired:
        return ""
    out = p.stdout or ""
    try:
        return str(json.loads(out[out.find("{"):out.rfind("}") + 1]).get("result") or "")
    except ValueError:
        return ""


def _json_in(text: str) -> dict:
    try:
        return json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return {}


TEACHER_PROMPT = """You label email for a professor's executive assistant. {profile}
For each numbered message below (shown with the earlier messages of its thread), answer from the user's point of view AT THE MOMENT THE MESSAGE ARRIVED (its Date line). Judge each message on its own: the messages are unrelated, and you do not know what happened after any of them. Never mark a message as handled or superseded; answer what it asks of the user when it lands.

needs_reply is true when the user personally should write back to someone because of this message: a question, a request for information, files, feedback, a decision, a signature or a confirmation, an invitation that needs an answer, a scheduling ask, or a sender plainly waiting on him, even when the ask is short or sits inside a thank-you. It is false for announcements, newsletters, mailing lists, automated notices and receipts; for threads where he is only copied and someone else owns the answer; and for messages that only close a loop with nothing left open.
action: reply | do (a task other than writing back) | decide | schedule (find or confirm a meeting time) | read (worth his attention, nothing to do) | none (he can skip it).
reply_by: the date the answer or task is due, YYYY-MM-DD, "" when none. urgency: low | normal | high.
sensitive: a student's records, grades or wellbeing; a personnel or hiring matter about a named person; health or family-medical information; a letter of recommendation or its content; a confidential manuscript or proposal review; a legal or money matter about a person; a prospective student's application. sensitive_kind: student-record | personnel | health | letter | manuscript-review | legal-financial | admissions | other | none.
ask: what is wanted from him, one sentence, "" when nothing. brief: two to four plain sentences his assistant needs in place of reading the email.

Answer with JSON only:
{{"labels": [{{"i": 1, "needs_reply": true, "reply_by": "", "ask": "...", "action": "reply", "urgency": "normal", "sensitive": false, "sensitive_kind": "none", "brief": "...", "why": "one short clause"}}]}}

{messages}"""


def label(limit: int = 500, batch: int = 10, log=print) -> dict:
    """Claude reference labels for up to `limit` cached messages that have
    none, dev and val messages first."""
    done = {r["key"] for r in remote_llm.read(LABELS)}
    rows = [r for r in messages().values()
            if r["key"] not in done and not r.get("keep_from_anthropic")]
    rows.sort(key=lambda r: r.get("received") or "")
    rows = rows[:limit]
    made = failed = 0
    for part in _batches(rows, batch):
        body = "\n\n".join(f"=== MESSAGE {j} ===\n{render(r, 4)}" for j, r in enumerate(part, 1))
        prompt = TEACHER_PROMPT.format(profile=_profile_line(), messages=body)
        got = _json_in(run_claude(prompt, TEACHER_MODEL)).get("labels") or []
        by_i = {g.get("i"): g for g in got if isinstance(g, dict)}
        for j, r in enumerate(part, 1):
            g = by_i.get(j)
            if not g:
                failed += 1
                continue
            remote_llm.append(LABELS, {"key": r["key"], "model": TEACHER_MODEL, **_clean_decision(g)})
            made += 1
        log(f"  labelled {made} (failed {failed}) of {len(rows)}")
    return {"labelled": made, "failed": failed}


def _batches(rows: list[dict], size: int, seed_: int = 17) -> list[list[dict]]:
    """Batches that never hold two messages of one thread, in shuffled
    order, so a label cannot lean on what came later in that thread."""
    rest = list(rows)
    random.Random(seed_).shuffle(rest)
    out = []
    while rest:
        part, convs, left = [], set(), []
        for r in rest:
            if len(part) < size and r["conversation_id"] not in convs:
                part.append(r)
                convs.add(r["conversation_id"])
            else:
                left.append(r)
        out.append(part)
        rest = left
    return out


def labels() -> dict[str, dict]:
    out = {}
    for r in remote_llm.read(LABELS):
        out[r["key"]] = r
    return out


def _clean_decision(g: dict) -> dict:
    def pick(v, allowed, default):
        return v if v in allowed else default
    rb = str(g.get("reply_by") or "")[:10]
    try:
        date.fromisoformat(rb)
    except ValueError:
        rb = ""
    out = {
        "needs_reply": bool(g.get("needs_reply")),
        "reply_by": rb,
        "ask": str(g.get("ask") or "")[:300],
        "action": pick(g.get("action"), ACTIONS, "read"),
        "urgency": pick(g.get("urgency"), URGENCY, "normal"),
        "sensitive": bool(g.get("sensitive")),
        "sensitive_kind": pick(g.get("sensitive_kind"), SENSITIVE_KINDS, "none"),
        "brief": str(g.get("brief") or "")[:1500],
    }
    if "why" in g:
        out["why"] = str(g.get("why") or "")[:200]
    if not out["sensitive"]:
        out["sensitive_kind"] = "none"
    return out


# ── candidates ────────────────────────────────────────────────────────

def candidates() -> dict[str, dict]:
    return {c["id"]: c for c in remote_llm.read(CANDIDATES)}


def add_candidate(instructions: str, *, parent: str = "", context: int = 0, shots: int = 0,
                  model: str = "", thinking: bool = False, votes: int = 1, note: str = "") -> dict:
    n = len(remote_llm.read(CANDIDATES))
    c = {"id": f"c{n:03d}", "parent": parent, "created": _iso(_now()),
         "instructions": instructions.strip(), "context": int(context) if context in (0, 2, 4) else 2,
         "shots": int(shots) if shots in (0, 3, 6) else 0,
         "model": model or remote_llm.config().get("model", ""),
         "thinking": bool(thinking), "votes": 3 if votes == 3 else 1, "note": note[:300]}
    remote_llm.append(CANDIDATES, c)
    return c


def seed() -> list[dict]:
    if candidates():
        return list(candidates().values())
    return [add_candidate(BASELINE_INSTRUCTIONS, context=0, note="the live prompt, schema v2"),
            add_candidate(BASELINE_INSTRUCTIONS, context=2, note="the live prompt plus two thread messages"),
            add_candidate(V1_INSTRUCTIONS, context=2, note="hand-written v1"),
            add_candidate(V1_INSTRUCTIONS, context=4, thinking=True, note="v1, more thread, thinking on")]


# ── splits and samples ────────────────────────────────────────────────

def split_of(row: dict, frozen_at: str = "") -> str:
    d = str(row.get("received") or "")
    if frozen_at and d >= frozen_at:
        return "test"
    return "dev" if d[:10] < VAL_FROM else "val"


def sample(split: str, n: int | None = None, seed_: int = 11) -> list[dict]:
    """A labelled sample of one split: every message that needs a reply by
    either label, every sensitive one, then others to fill `n`."""
    frozen_at = _read_json(FROZEN, {}).get("frozen_at", "")
    labs, outs = labels(), _read_json(OUTCOMES, {})
    rows = [r for r in messages().values()
            if split_of(r, frozen_at) == split and r["key"] in labs]
    rows.sort(key=lambda r: r["key"])
    n = n or (DEV_SAMPLE if split == "dev" else VAL_SAMPLE if split == "val" else len(rows))

    def key_rows(r):
        o, lab = outs.get(r["key"], {}), labs[r["key"]]
        return bool(o.get("replied_direct") or lab["needs_reply"] or lab["sensitive"])
    must = [r for r in rows if key_rows(r)]
    rest = [r for r in rows if not key_rows(r)]
    random.Random(seed_).shuffle(must)
    random.Random(seed_).shuffle(rest)
    picked = must[:n] + rest[:max(0, n - len(must))]
    return sorted(picked, key=lambda r: r["key"])


def kept_sample(split: str) -> list[dict]:
    """Kept-from-Anthropic messages of one split whose reply outcome is
    known, scored by that outcome alone."""
    frozen_at = _read_json(FROZEN, {}).get("frozen_at", "")
    outs = _read_json(OUTCOMES, {})
    rows = [r for r in messages().values() if r.get("keep_from_anthropic")
            and split_of(r, frozen_at) == split and outs.get(r["key"], {}).get("mature")]
    return sorted(rows, key=lambda r: r["key"])


def shot_pool(exclude: set[str]) -> list[dict]:
    """Dev messages outside the scored sample whose reference label agrees
    with what the user did, for worked examples."""
    labs, outs = labels(), _read_json(OUTCOMES, {})
    pool = []
    for r in messages().values():
        if (r["key"] in exclude or split_of(r) != "dev" or r["key"] not in labs
                or r.get("keep_from_anthropic")):
            continue
        o = outs.get(r["key"], {})
        if o.get("mature") and bool(o.get("replied_direct")) != labs[r["key"]]["needs_reply"]:
            continue
        pool.append(r)
    pool.sort(key=lambda r: r["key"])
    return pool


def pick_shots(pool: list[dict], k: int, seed_: int = 5) -> list[dict]:
    if not k:
        return []
    labs = labels()
    rng = random.Random(seed_)
    pos = [r for r in pool if labs[r["key"]]["needs_reply"]]
    neg = [r for r in pool if not labs[r["key"]]["needs_reply"]]
    rng.shuffle(pos)
    rng.shuffle(neg)
    return (pos[:(k + 1) // 2] + neg[:k // 2])[:k]


# ── running a candidate ───────────────────────────────────────────────

def _arrival_day(row: dict) -> str:
    t = _parse(row.get("received"))
    return t.astimezone().strftime("%A %Y-%m-%d") if t else _today_local()


def _system(c: dict, row: dict) -> str:
    """The candidate's system prompt. 'Today' is the day the message
    arrived, so old mail is judged as it would have been live."""
    return (f"{c['instructions']}\n\n{FIELD_GUIDE}\n\n{_profile_line()} "
            f"Today is {_arrival_day(row)}.")


def _shot_answer(lab: dict) -> dict:
    return {"needs_reply": lab["needs_reply"], "reply_by": lab["reply_by"], "ask": lab["ask"],
            "action": lab["action"], "urgency": lab["urgency"], "sensitive": lab["sensitive"],
            "sensitive_kind": lab["sensitive_kind"], "scheduling": "none", "offered_times": [],
            "people": [], "tone": [], "state": "needs-reply" if lab["needs_reply"] else "fyi",
            "domain": "professional", "brief": lab["brief"]}


def _shot_messages(c: dict, pairs: list[tuple[dict, dict]]) -> list[dict]:
    out = []
    for row, lab in pairs:
        out += [{"role": "user", "content": render(row, c["context"])},
                {"role": "assistant", "content": json.dumps(_shot_answer(lab))}]
    return out


def shot_pairs(rows: list[dict]) -> list[tuple[dict, dict]]:
    labs = labels()
    return [(r, labs[r["key"]]) for r in rows if r["key"] in labs]


def validate(obj: dict) -> dict:
    """A candidate's answer coerced into the schema."""
    def pick(key, allowed, default):
        v = obj.get(key)
        return v if v in allowed else default
    d = _clean_decision(obj)
    d.update({
        "scheduling": pick("scheduling", SCHED_KINDS, "none"),
        "offered_times": [t.strip()[:40] for t in obj.get("offered_times") or []
                          if isinstance(t, str) and t.strip()][:12],
        "people": [{"name": str(p.get("name"))[:80], "role": str(p.get("role") or "")[:40]}
                   for p in obj.get("people") or [] if isinstance(p, dict) and p.get("name")][:8],
        "tone": [t for t in obj.get("tone") or [] if t in TONES],
        "state": pick("state", STATES, "fyi"),
        "domain": pick("domain", DOMAINS, "professional"),
    })
    d.pop("why", None)
    return d


def run_candidate(c: dict, row: dict, shots: list[tuple[dict, dict]], *,
                  chat=None) -> tuple[dict, dict]:
    """(answer, call metadata) for one message. With votes=3 the decision
    fields are the majority of three samples."""
    chat = chat or remote_llm.chat_json
    msgs = ([{"role": "system", "content": _system(c, row)}] + _shot_messages(c, shots)
            + [{"role": "user", "content": render(row, c["context"])}])
    kw = {"purpose": "mail-tune", "schema": SCHEMA, "schema_name": "triage",
          "max_tokens": 12000 if c.get("thinking") else 1200,
          "model": c.get("model") or None, "thinking": bool(c.get("thinking"))}
    if c.get("votes", 1) == 1:
        obj, meta = chat(msgs, **kw)
        return validate(obj), meta
    # Three samples in one request: the prompt is processed once.
    obj, meta = chat(msgs, temperature=0.7, n=3, **kw)
    answers = [validate(o) for o in (meta.get("objects") or [obj])]
    out = dict(answers[0])
    for f in ("needs_reply", "action", "urgency", "sensitive", "sensitive_kind"):
        out[f] = Counter(a[f] for a in answers).most_common(1)[0][0]
    return out, {"latency_ms": int(meta.get("latency_ms") or 0), "votes": len(answers)}


def outcome_truth(o: dict) -> bool | None:
    """What the user's behaviour says about needing a reply: True for a
    direct reply, False for no reply at all to a non-automated message,
    None otherwise (a reply that went elsewhere, or too recent)."""
    if not o.get("mature"):
        return None
    if o.get("replied_direct"):
        return True
    if not o.get("replied") and not o.get("automated"):
        return False
    return None


def corrected_needs_reply(lab: dict, o: dict) -> bool:
    if o.get("mature") and o.get("replied_direct"):
        return True
    return bool(lab["needs_reply"])


def score(rows: list[dict], answers: dict[str, dict]) -> dict:
    """Every metric for one candidate on one sample."""
    labs, outs = labels(), _read_json(OUTCOMES, {})
    n = len(rows)
    got = [r for r in rows if r["key"] in answers]

    def f1(pred, gold):
        tp = sum(1 for p, g in zip(pred, gold) if p and g)
        fp = sum(1 for p, g in zip(pred, gold) if p and not g)
        fn = sum(1 for p, g in zip(pred, gold) if g and not p)
        return round(2 * tp / (2 * tp + fp + fn), 3) if tp + fp + fn else None

    nr_p = [answers[r["key"]]["needs_reply"] for r in got]
    nr_t = [labs[r["key"]]["needs_reply"] for r in got]
    # The corrected reference: a message the user answered directly needs
    # a reply whatever Claude said. Elsewhere Claude's label stands; no
    # reply is too weak a sign to overrule it.
    nr_c = [corrected_needs_reply(labs[r["key"]], outs.get(r["key"], {})) for r in got]
    # Disputes: where the candidate and Claude disagree, whom the user's
    # own behaviour backs.
    wins = losses = 0
    for r in got:
        a, lab, o = answers[r["key"]], labs[r["key"]], outs.get(r["key"], {})
        if a["needs_reply"] == lab["needs_reply"] or not o.get("mature"):
            continue
        truth = outcome_truth(o)
        if truth is None:
            continue
        wins += a["needs_reply"] == truth
        losses += lab["needs_reply"] == truth
    direct = [r for r in got if outs.get(r["key"], {}).get("mature")
              and outs[r["key"]].get("replied_direct")]
    dated = [r for r in got if labs[r["key"]]["reply_by"]]
    m = {
        "n": n, "answered": len(got),
        "nr_recall_outcome": round(sum(answers[r["key"]]["needs_reply"] for r in direct) / len(direct), 3)
        if direct else None,
        "nr_outcome_n": len(direct),
        # Claude's reference on the same messages: the pass level is to
        # catch at least as many of the user's direct replies as Claude does.
        "nr_recall_reference": round(sum(labs[r["key"]]["needs_reply"] for r in direct) / len(direct), 3)
        if direct else None,
        "nr_f1_teacher": f1(nr_p, nr_t),
        "nr_f1_corrected": f1(nr_p, nr_c),
        "disputes_won": wins, "disputes_lost": losses,
        "action_acc": round(sum(answers[r["key"]]["action"] == labs[r["key"]]["action"] for r in got)
                            / len(got), 3) if got else None,
        "urgency_acc": round(sum(answers[r["key"]]["urgency"] == labs[r["key"]]["urgency"] for r in got)
                             / len(got), 3) if got else None,
        "sens_f1": f1([answers[r["key"]]["sensitive"] for r in got],
                      [labs[r["key"]]["sensitive"] for r in got]),
        "reply_by_match": round(sum(answers[r["key"]]["reply_by"] == labs[r["key"]]["reply_by"]
                                    for r in dated) / len(dated), 3) if dated else None,
        "brief_chars": round(sum(len(answers[r["key"]]["brief"]) for r in got) / len(got)) if got else None,
    }
    parts = [(0.35, m["nr_f1_corrected"]), (0.30, m["nr_recall_outcome"]), (0.15, m["action_acc"]),
             (0.10, m["sens_f1"]), (0.10, m["reply_by_match"])]
    have = [(w, v) for w, v in parts if v is not None]
    comp = sum(w * v for w, v in have) / sum(w for w, _ in have) if have else 0.0
    if (m["brief_chars"] or 0) > 700:
        comp -= 0.02 * ((m["brief_chars"] - 700) / 300)
    if n:
        comp *= len(got) / n
    m["score"] = round(comp, 4)
    return m


def evaluate(cid: str, split: str = "dev", limit: int | None = None, *, chat=None,
             log=print) -> dict:
    c = candidates()[cid]
    rows = sample(split, limit)
    shots = shot_pairs(pick_shots(shot_pool({r["key"] for r in rows}), c["shots"]))
    answers, errors, lat = {}, 0, []
    out_rel = f"{TUNE}/answers/{cid}-{split}.jsonl"
    _private_dir(f"{TUNE}/answers")
    rows_out = []
    t0 = time.monotonic()

    def one(r):
        try:
            a, meta = run_candidate(c, r, shots, chat=chat)
            return r, a, meta, None
        except remote_llm.RemoteLLMError as e:
            return r, None, None, e

    # As many messages in flight as the endpoint has slots; the slots are
    # shared with the live tick, which simply queues for one.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=remote_llm.max_parallel()) as pool:
        for i, (r, a, meta, err) in enumerate(pool.map(one, rows), 1):
            if err is None:
                answers[r["key"]] = a
                lat.append(int(meta.get("latency_ms") or 0))
                rows_out.append({"key": r["key"], **a})
            else:
                errors += 1
                rows_out.append({"key": r["key"], "error": str(err)[:200]})
            if i % 25 == 0:
                log(f"  {cid} {split}: {i}/{len(rows)}")
    _rewrite_jsonl(out_rel, rows_out)
    m = score(rows, answers)
    # The kept mail: the outcome label alone, kept out of the answers file
    # that reflection reads.
    kept_hits = kept_n = 0
    outs = _read_json(OUTCOMES, {})
    kept_rows = [r for r in kept_sample(split) if outs.get(r["key"], {}).get("replied_direct")]
    with ThreadPoolExecutor(max_workers=remote_llm.max_parallel()) as pool:
        for r, a, _meta, err in pool.map(one, kept_rows):
            if err is None:
                kept_n += 1
                kept_hits += bool(a["needs_reply"])
    m["nr_recall_outcome_kept"] = round(kept_hits / kept_n, 3) if kept_n else None
    m["nr_outcome_kept_n"] = kept_n
    lat.sort()
    m.update(candidate=cid, split=split, errors=errors, failed=errors > 0.1 * max(1, len(rows)),
             ts=_iso(_now()),
             latency_p50_s=round(lat[len(lat) // 2] / 1000, 2) if lat else None,
             minutes=round((time.monotonic() - t0) / 60, 1))
    remote_llm.append(RESULTS, m)
    return m


def results(split: str = "dev") -> dict[str, dict]:
    out = {}
    for r in remote_llm.read(RESULTS):
        if r.get("split") == split:
            out[r["candidate"]] = r
    return out


def answers_of(cid: str, split: str) -> dict[str, dict]:
    return {r["key"]: r for r in remote_llm.read(f"{TUNE}/answers/{cid}-{split}.jsonl")
            if "error" not in r}


# ── reflective evolution ──────────────────────────────────────────────

REFLECT_PROMPT = """You are improving the instructions a smaller model follows to triage one email at a time for a professor's executive assistant. The assistant acts on the model's JSON record instead of reading the email. The record's decision fields are fixed: needs_reply, reply_by, ask, action, urgency, sensitive, sensitive_kind (plus scheduling, people, tone, state, domain). The `brief` field is free: you decide what it should contain and how long it is, so that the assistant can act without the email.

The model is {model}{thinking}. It sees {context} earlier thread messages and {shots} worked examples.

CURRENT INSTRUCTIONS:
<<<
{instructions}
>>>

SCORES on {n} messages: {metrics}
The most important number is nr_recall_outcome: of the messages the user actually answered directly, the share the model marked needs_reply. A missed request is the worst error. nr_f1_corrected compares needs_reply with reference labels corrected by the user's direct replies; the reference labels themselves are not always right, and disputes_won / disputes_lost count the disagreements where the user's behaviour backed the model or the reference. action_acc and sens_f1 compare with the reference labels.

FAILURES (the message as the model saw it, its answer, the reference label, and what the user did):
{failures}

Write {k} improved instruction texts. Each is a complete replacement, written to fix the failure patterns you see without breaking what works. For each, also choose context (0, 2 or 4 earlier thread messages) and shots (0, 3 or 6 worked examples). Make the {k} candidates differ in approach. Answer with JSON only:
{{"candidates": [{{"instructions": "...", "context": 2, "shots": 3, "rationale": "one sentence"}}]}}"""


def failures(cid: str, split: str = "dev", k: int = 12) -> list[str]:
    labs, outs = labels(), _read_json(OUTCOMES, {})
    ans = answers_of(cid, split)
    msgs = messages()
    c = candidates()[cid]
    missed, nr_dis, other = [], [], []
    for key, a in ans.items():
        lab, o = labs.get(key), outs.get(key, {})
        if not lab:
            continue
        if o.get("mature") and o.get("replied_direct") and not a["needs_reply"]:
            missed.append(key)
        elif (a["needs_reply"] != lab["needs_reply"]
              and outcome_truth(o) != a["needs_reply"]):
            nr_dis.append(key)
        elif a["action"] != lab["action"] or a["sensitive"] != lab["sensitive"]:
            other.append(key)
    rng = random.Random(3)
    for lst in (missed, nr_dis, other):
        rng.shuffle(lst)
    chosen = (missed[:6] + nr_dis[:4] + other[:k])[:k]
    out = []
    for key in chosen:
        a, lab, o = ans[key], labs[key], outs.get(key, {})
        did = ("answered it directly" if o.get("replied_direct") else
               "replied in the thread" if o.get("replied") else
               "did not reply within 3 days" if o.get("mature") else "too recent to tell")
        model_ans = {f: a[f] for f in ("needs_reply", "reply_by", "ask", "action", "urgency",
                                       "sensitive", "sensitive_kind", "brief")}
        ref = {f: lab[f] for f in ("needs_reply", "reply_by", "ask", "action", "urgency",
                                   "sensitive", "sensitive_kind", "why") if f in lab}
        text = render(msgs[key], c["context"])[:2500]
        out.append(f"--- MESSAGE ---\n{text}\n--- MODEL ---\n{json.dumps(model_ans)}\n"
                   f"--- REFERENCE ---\n{json.dumps(ref)}\n--- THE USER {did.upper()} ---")
    return out


def reflect(cid: str, k: int = 2, split: str = "dev") -> list[dict]:
    c = candidates()[cid]
    m = results(split).get(cid) or {}
    metrics = {key: m.get(key) for key in ("nr_recall_outcome", "nr_recall_reference",
                                           "nr_outcome_n", "nr_f1_corrected", "nr_f1_teacher",
                                           "disputes_won", "disputes_lost",
                                           "action_acc", "urgency_acc", "sens_f1",
                                           "reply_by_match", "brief_chars", "score")}
    prompt = REFLECT_PROMPT.format(
        model=c["model"], thinking=" with thinking on" if c.get("thinking") else "",
        context=c["context"], shots=c["shots"], instructions=c["instructions"],
        n=m.get("n"), metrics=json.dumps(metrics), failures="\n\n".join(failures(cid, split)), k=k)
    got = _json_in(run_claude(prompt, REFLECT_MODEL)).get("candidates") or []
    made = []
    for g in got[:k]:
        if isinstance(g, dict) and str(g.get("instructions") or "").strip():
            made.append(add_candidate(g["instructions"], parent=cid,
                                      context=g.get("context", c["context"]),
                                      shots=g.get("shots", c["shots"]), model=c["model"],
                                      thinking=c.get("thinking", False), votes=c.get("votes", 1),
                                      note=str(g.get("rationale") or "")))
    return made


def _endpoint_down() -> bool:
    b = remote_llm._breaker("mail-tune")
    return bool(b and b.is_open())


def _valid(split: str = "dev") -> list[dict]:
    """Results that ran cleanly, best first."""
    return sorted((r for r in results(split).values() if not r.get("failed")),
                  key=lambda r: -r["score"])


def evolve(rounds: int = 4, children: int = 2, finalists: int = 3, log=print) -> dict:
    """Reflective evolution on dev. A candidate whose run failed is never a
    parent, and the loop stops while the endpoint's breaker is open rather
    than spend reflection calls on setups it cannot score. Stored results
    are re-scored first, so every candidate is judged by the same rules."""
    seed()
    restat()
    for cid in candidates():
        if cid not in results("dev"):
            log(f"seed {cid}: {json.dumps(evaluate(cid, 'dev', log=log))}")
    for rnd in range(1, rounds + 1):
        if _endpoint_down():
            log(f"round {rnd}: the endpoint is failing; stopping")
            break
        parents = [b["candidate"] for b in _valid("dev")[:2]]
        log(f"round {rnd}: parents {parents}")
        for p in parents:
            for child in reflect(p, k=children):
                log(f"  {child['id']} <- {p}: {child['note']}")
                log(f"  {json.dumps(evaluate(child['id'], 'dev', log=log))}")
                if _endpoint_down():
                    break
    board = _valid("dev")
    final = []
    for b in board[:finalists]:
        if b["candidate"] not in results("val"):
            final.append(evaluate(b["candidate"], "val", log=log))
    return {"dev_top": [(b["candidate"], b["score"]) for b in board[:5]],
            "val": [(f["candidate"], f["score"], f.get("nr_recall_outcome")) for f in final]}


def rerun_failed(log=print) -> list[dict]:
    """Score again every candidate whose dev run failed or never ran."""
    out = []
    for cid in candidates():
        r = results("dev").get(cid)
        if r and not r.get("failed") and r.get("answered"):
            continue
        if _endpoint_down():
            log("the endpoint is failing; stopping")
            break
        m = evaluate(cid, "dev", log=log)
        log(f"{cid}: {json.dumps(m)}")
        out.append(m)
    return out


def restat() -> int:
    """Recompute every stored result from its stored answers with the
    current labels and outcome rules. No model calls."""
    rows = remote_llm.read(RESULTS)
    out = []
    msgs = messages()
    for r in rows:
        ans = answers_of(r["candidate"], r["split"])
        if not ans:
            # Nothing answered: the run failed, whatever older code recorded.
            out.append({**r, "failed": True})
            continue
        # The messages this run answered, so a changed sampling rule cannot
        # count unanswered messages against it.
        scored = [msgs[k] for k in sorted(ans) if k in msgs]
        m = score(scored, ans)
        m["n"] = r["n"]
        keep = {k: r[k] for k in ("candidate", "split", "errors", "ts", "latency_p50_s",
                                  "minutes", "nr_recall_outcome_kept", "nr_outcome_kept_n") if k in r}
        keep["failed"] = int(r.get("errors") or 0) > 0.1 * max(1, int(r.get("n") or 0))
        out.append({**m, **keep})
    _rewrite_jsonl(RESULTS, out)
    return len(out)


def board(split: str = "dev") -> list[dict]:
    cs = candidates()
    rows = []
    for cid, r in sorted(results(split).items(), key=lambda kv: -kv[1]["score"]):
        c = cs.get(cid, {})
        rows.append({"candidate": cid, "parent": c.get("parent"), "score": r["score"],
                     "failed": bool(r.get("failed")),
                     "nr_recall_outcome": r.get("nr_recall_outcome"),
                     "nr_recall_reference": r.get("nr_recall_reference"),
                     "nr_outcome_n": r.get("nr_outcome_n"), "nr_f1_teacher": r.get("nr_f1_teacher"),
                     "nr_f1_corrected": r.get("nr_f1_corrected"),
                     "disputes": f"{r.get('disputes_won')}-{r.get('disputes_lost')}",
                     "action_acc": r.get("action_acc"), "sens_f1": r.get("sens_f1"),
                     "reply_by_match": r.get("reply_by_match"), "brief_chars": r.get("brief_chars"),
                     "context": c.get("context"), "shots": c.get("shots"),
                     "thinking": c.get("thinking"), "p50_s": r.get("latency_p50_s"),
                     "note": c.get("note", "")[:80]})
    return rows


# ── freeze ────────────────────────────────────────────────────────────

def freeze(cid: str) -> dict:
    """Write the private extract-prompt.json the live tick reads: the
    candidate's settings and its worked examples. Mail extracted from now
    on is the test split."""
    c = candidates()[cid]
    # The worked examples the candidate was scored with on dev: the same
    # pool (dev outside the scored sample) and the same draw.
    pairs = shot_pairs(pick_shots(shot_pool({r["key"] for r in sample("dev")}), c["shots"]))
    frozen = {"candidate": cid, "frozen_at": _iso(_now()), "instructions": c["instructions"],
              "context": c["context"], "model": c["model"], "thinking": c.get("thinking", False),
              "votes": c.get("votes", 1), "shots": [{"row": r, "label": lab} for r, lab in pairs],
              "dev_score": (results("dev").get(cid) or {}).get("score")}
    _write(FROZEN, json.dumps(frozen, indent=1))
    return {"candidate": cid, "frozen_at": frozen["frozen_at"], "shots": len(pairs)}


def frozen() -> dict | None:
    """The frozen setup, or None while the live tick uses its own prompt."""
    try:
        return _read_json(FROZEN, None)
    except Exception:
        return None


def live_row(msg: dict, body: str, conv: list[dict], user: set[str]) -> dict:
    """A live Graph message in the dataset's shape, with its thread."""
    return {"key": f"{msg.get('conversationId') or ''}|{msg.get('receivedDateTime') or ''}",
            "id": msg.get("id"), "conversation_id": msg.get("conversationId") or "",
            "received": msg.get("receivedDateTime") or "",
            "from": _addr(msg.get("from")), "from_name": _name(msg.get("from")),
            "to": _addrs(msg.get("toRecipients"))[:12], "cc": _addrs(msg.get("ccRecipients"))[:12],
            "subject": (msg.get("subject") or "")[:200],
            "has_attachments": bool(msg.get("hasAttachments")),
            "body": body[:BODY_CHARS],
            "thread": thread_before(conv, msg.get("receivedDateTime") or "", user) if conv else []}


def run_frozen(fz: dict, row: dict, *, chat=None) -> tuple[dict, dict]:
    """The frozen setup on one message: the live tick's path."""
    c = {"instructions": fz["instructions"], "context": int(fz.get("context") or 0),
         "model": fz.get("model") or "", "thinking": bool(fz.get("thinking")),
         "votes": fz.get("votes", 1)}
    pairs = [(s["row"], s["label"]) for s in fz.get("shots") or []]
    return run_candidate(c, row, pairs, chat=chat)


def to_record(a: dict) -> dict:
    """A candidate's answer in the live record's field names (mail_privacy)."""
    return {"sensitivity": "confidential" if a["sensitive"] else "normal",
            "confidential_kind": a["sensitive_kind"] if a["sensitive"] else "none",
            "needs_reply": a["needs_reply"], "ask": a["ask"], "deadline": a["reply_by"],
            "deadline_text": "", "scheduling": a["scheduling"],
            "offered_times": a["offered_times"], "people": a["people"], "tone": a["tone"],
            "state": a["state"], "domain": a["domain"], "summary": a["brief"],
            "action": a["action"], "urgency": a["urgency"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dataset")
    p.add_argument("--since", default=None)
    p.add_argument("--refresh", action="store_true")
    sub.add_parser("rerun-failed")
    sub.add_parser("restat")
    sub.add_parser("outcomes")
    p = sub.add_parser("label")
    p.add_argument("--limit", type=int, default=500)
    sub.add_parser("seed")
    p = sub.add_parser("eval")
    p.add_argument("--candidate", required=True)
    p.add_argument("--split", default="dev", choices=("dev", "val", "test"))
    p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("evolve")
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--children", type=int, default=2)
    p = sub.add_parser("board")
    p.add_argument("--split", default="dev", choices=("dev", "val", "test"))
    p = sub.add_parser("freeze")
    p.add_argument("--candidate", required=True)
    a = ap.parse_args(argv)
    if not _owner_trust.is_owner_process():
        print("mail_tuning runs in the owner's processes only", file=sys.stderr)
        return 2
    log = lambda s: print(s, flush=True)  # noqa: E731
    if a.cmd == "dataset":
        res = build_dataset(since=a.since, refresh=a.refresh, log=log)
    elif a.cmd == "rerun-failed":
        res = [(m["candidate"], m["score"], m["failed"]) for m in rerun_failed(log=log)]
    elif a.cmd == "restat":
        res = {"rescored": restat()}
    elif a.cmd == "outcomes":
        res = build_outcomes()
    elif a.cmd == "label":
        res = label(a.limit, log=log)
    elif a.cmd == "seed":
        res = [c["id"] for c in seed()]
    elif a.cmd == "eval":
        res = evaluate(a.candidate, a.split, a.limit, log=log)
    elif a.cmd == "evolve":
        res = evolve(a.rounds, a.children, log=log)
    elif a.cmd == "board":
        for r in board(a.split):
            print(json.dumps(r))
        return 0
    else:
        res = freeze(a.candidate)
    print(json.dumps(res, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
