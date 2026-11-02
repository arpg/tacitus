#!/usr/bin/env python3
"""Mail privacy (experimental): per-message extraction on the remote LLM,
the calendar shim, and the measurements of the shadow run
(docs/remote-llm.md).

The question the shadow run answers: if Claude saw extracted fields and
stubs instead of raw mail, what would it lose? Nothing here changes what
the Claude lanes see or do. Every verb records and measures.

Verbs (owner process). tick, backfill, shadow-briefing and claude-sample
do nothing while the `mail` switch in remote-llm.json is off; report,
final-report, status and purge read what is stored.

    tick                 extract mail received since the last tick (owner cron)
    backfill --days N    the same over the last N days
    shadow-briefing      build the reduced briefing beside the real one and
                         record what shrank
    claude-sample        run one briefing prompt on the real and on the
                         reduced briefing (Read only, nothing posted), store
                         both and a judge's comparison
    report [--post]      the numbers so far (counts only); --post sends them to
                         the health channel, and from shadow_until on it also
                         writes the final report
    final-report         the full report and the grading sheets (private)
    status               switch, counts, last tick
    redo-withheld        re-extract the messages an earlier version held back
    sync-briefs          write a thread brief onto every open ledger row with a
                         conversation id (the tick does this too, when mail is
                         enforced)
    sync-kept-rows       a ledger row for every kept message that asks for
                         something (subject, the remote model's summary, ask
                         and deadline; no text), closed from the thread's
                         records (the tick does this too, when mail is enforced)
    brief --conversation-id <id>
                         print one thread's brief from the store
    purge                delete everything this experiment stored

Per message, `tick` sends the new text of the message (quoted history
cut, 8000 chars) to the remote model, which returns a record in a fixed
schema: sensitivity and its kind, whether a reply is needed, the ask, the
deadline, scheduling content, people and their roles, tone flags, the
Outlook state and domain tags, a summary. Every message goes to the remote
model. A message whose participants hit the keep_from_anthropic list, or
one from outside the institution that the admissions filter would move, is labelled
`keep_from_anthropic`: an enforce mode would let only the remote model see
it, so the reduced briefing stubs it and the reports count Claude's reads
of it.

The mediator calls `note_release()` after each mail read it serves, so the
report can say how often a chat turn read a thread the shadow classed
confidential: the friction an enforce mode would add. In the `enforce`
mode its `release_precheck()` and `release_filter()` keep listed threads
from the cron lanes; in the `ghost` mode `release_filter()` also seals
each read on the side and records both for the ghost week
(ghost_week.py); in the `sealed` mode it hands every lane
`seal_output()`: the remote model's record of each message in place of
its text. `thread_brief()` composes one thread's records into
the brief `sync_briefs()` writes onto the ledger row, so a Claude session
can act on an item from `ledger.py get` alone.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm  # noqa: E402
import claude_cli  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
EA_BOT_HOME = Path(os.environ.get("EA_BOT_HOME", "/home/ea-bot"))
REPO = Path(__file__).resolve().parent.parent

STATE_FILE = "mail-state.json"
EXTRACTIONS = "extractions.jsonl"
RELEASES = "releases.jsonl"
BRIEFINGS = "briefing-shadow.jsonl"
SAMPLES = "samples.jsonl"

TICK_OVERLAP = timedelta(minutes=10)
FIRST_LOOKBACK = timedelta(hours=1)
TICK_CAP = 40
RETRY_LIMIT = 3
SEEN_TTL = timedelta(days=40)
REPLY_WINDOW = timedelta(days=3)
COLD_MS = 60_000

SENSITIVE_KINDS = ("none", "student-record", "personnel", "health", "letter",
                   "manuscript-review", "legal-financial", "admissions", "other")
SCHED_KINDS = ("none", "request", "proposes-times", "reschedule", "booking-link",
               "poll", "confirmation", "cancellation")
# Outlook's own meeting responses ("Accepted: <subject>"): the model reads
# them as confirmations or requests, and they ask nothing of the user.
CALENDAR_RESPONSE_SUBJECT = re.compile(
    r"^\s*(?:accepted|tentative|tentatively accepted|declined|canceled|cancelled|new time proposed)\s*:", re.I)
TONES = ("distress", "escalation", "withdrawal", "urgent", "complaint")
STATES = ("needs-reply", "todo", "decide", "scheduling", "awaiting-reply", "fyi")
DOMAINS = ("research", "service", "teaching", "professional", "personal")
ACTION_STATES = {"needs-reply", "todo", "decide", "scheduling"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sensitivity": {"type": "string", "enum": ["normal", "confidential"]},
        "confidential_kind": {"type": "string", "enum": list(SENSITIVE_KINDS)},
        "needs_reply": {"type": "boolean"},
        "ask": {"type": "string"},
        "deadline": {"type": "string"},
        "deadline_text": {"type": "string"},
        "scheduling": {"type": "string", "enum": list(SCHED_KINDS)},
        "offered_times": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
            "required": ["name", "role"]}},
        "tone": {"type": "array", "items": {"type": "string", "enum": list(TONES)}},
        "state": {"type": "string", "enum": list(STATES)},
        "domain": {"type": "string", "enum": list(DOMAINS)},
        "summary": {"type": "string"},
    },
    "required": ["sensitivity", "confidential_kind", "needs_reply", "ask", "deadline",
                 "deadline_text", "scheduling", "offered_times", "people", "tone",
                 "state", "domain", "summary"],
}

PROMPT = """You read one email for a professor's assistant and fill a JSON record. Answer with JSON only, in the given schema.

{profile}
Today is {today}.

Fields:
- sensitivity: "confidential" when the message carries a student's records or grades, a personnel or hiring matter about a named person, health or family-medical information, a letter of recommendation or what it says, a confidential manuscript or proposal review, a legal or money matter about a person, or a prospective student's application details. Otherwise "normal". confidential_kind names which one ("none" when normal).
- needs_reply: true only when the sender asks the user himself to answer or act in writing: a question put to him, a request of him, a proposal waiting on his word. False for a sympathy note, a thank-you, an acknowledgement, a status report, a message that answers the user's own question, and a message whose question or request is addressed to someone else (the user in Cc, or a named third person asked to act).
- ask: what the sender wants from the user, one sentence; "" when nothing, and "" when the request is addressed to someone else.
- deadline: the date the ask is due, YYYY-MM-DD; "" when none. deadline_text: the words that state it; "" when none.
- scheduling: the meeting-time content of the message; "none" when it is not about a meeting time.
- offered_times: each meeting time the sender states with its day, ISO 8601 local time; [] when none. Never infer a date from words like "most days", "next week" or "mornings": those go in the summary. A day with no clock time is not an offered time.
- people: each person who matters to the ask, with their role relative to the user (student, colleague, staff, dean, program officer, external, family). At most 8.
- tone: the flags that apply; [] when none.
- state: needs-reply, todo (the user must do something other than reply), decide, scheduling, awaiting-reply (the user is waiting on the sender), or fyi (nothing is owed by the user: a notification, a confirmation, a thank-you, a group discussion with no ask to him). A reminder that the user owes something by a date (a recommendation, a report, a form) is todo with that deadline, even when it asks for no reply.
- domain: research, service, teaching, professional or personal.
- summary: what the message says, at most two sentences of plain fact."""

_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ── small helpers ─────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _addr(rec) -> str:
    return ((rec or {}).get("emailAddress") or {}).get("address", "").strip().lower()


def _addrs(recs) -> list[str]:
    return [a for a in (_addr(r) for r in recs or []) if a]


def message_key(msg: dict) -> str:
    """Stable across folder moves (Outlook mints a new id on every move)."""
    return f"{msg.get('conversationId') or ''}|{msg.get('receivedDateTime') or ''}"


def _pct(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _user_addresses() -> set[str]:
    try:
        import mail_tagger
        return mail_tagger._user_addresses()
    except Exception:
        return set()


def _profile_line() -> str:
    try:
        import resident_io
        prof = json.loads(resident_io.read_text(EA_DIR / "config" / "user-profile.json") or "{}")
    except Exception:
        prof = {}
    bits = [prof.get("name"), prof.get("title") or prof.get("role"),
            prof.get("department"), prof.get("institution") or prof.get("university")]
    bits = [str(b) for b in bits if b]
    return "The user: " + ", ".join(bits) + "." if bits else "The user is a university professor."


# ── state ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        st = json.loads(_owner_trust.read_owner_file(remote_llm.store_dir() / STATE_FILE))
        if isinstance(st, dict):
            st.setdefault("seen", {})
            st.setdefault("attempts", {})
            return st
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"mail_privacy: state unreadable, starting fresh: {e}", file=sys.stderr)
    return {"last_tick": "", "seen": {}, "attempts": {}}


def save_state(st: dict) -> None:
    cutoff = _now() - SEEN_TTL
    st["seen"] = {k: v for k, v in st.get("seen", {}).items()
                  if (_parse(v) or _now()) >= cutoff}
    st["attempts"] = {k: v for k, v in st.get("attempts", {}).items() if k not in st["seen"]}
    _owner_trust.write_owner_file(remote_llm.store_dir() / STATE_FILE,
                                  (json.dumps(st) + "\n").encode(), mode=0o600)


class _Busy(Exception):
    pass


class _tick_lock:
    """One tick or backfill at a time; a second one leaves at once."""

    def __enter__(self):
        self.fd = os.open(remote_llm.store_dir() / "mail-tick.lock",
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                          0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise _Busy() from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


# ── extraction ────────────────────────────────────────────────────────

def validate(obj: dict) -> dict:
    """The record with every field coerced into its schema: closed
    vocabularies checked, lists capped, strings trimmed. A value outside a
    vocabulary falls back to its neutral value."""
    def s(key, cap):
        v = obj.get(key)
        return v.strip()[:cap] if isinstance(v, str) else ""

    def pick(key, allowed, default):
        v = obj.get(key)
        return v if v in allowed else default

    deadline = s("deadline", 10)
    try:
        date.fromisoformat(deadline)
    except ValueError:
        deadline = ""
    people = []
    for p in obj.get("people") or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip():
            people.append({"name": p["name"].strip()[:80],
                           "role": str(p.get("role") or "").strip()[:40]})
    rec = {
        "sensitivity": pick("sensitivity", ("normal", "confidential"), "normal"),
        "confidential_kind": pick("confidential_kind", SENSITIVE_KINDS, "none"),
        "needs_reply": bool(obj.get("needs_reply")),
        "ask": s("ask", 300),
        "deadline": deadline,
        "deadline_text": s("deadline_text", 120),
        "scheduling": pick("scheduling", SCHED_KINDS, "none"),
        "offered_times": [t.strip()[:40] for t in obj.get("offered_times") or []
                          if isinstance(t, str) and t.strip()][:12],
        "people": people[:8],
        "tone": [t for t in obj.get("tone") or [] if t in TONES],
        "state": pick("state", STATES, "fyi"),
        "domain": pick("domain", DOMAINS, "professional"),
        "summary": s("summary", 500),
    }
    for key, cap in (("links", LINKS_CAP), ("quotes", QUOTES_CAP)):
        vals = obj.get(key)
        if isinstance(vals, list):
            rec[key] = [str(v)[:QUOTE_CHARS] for v in vals if isinstance(v, str) and v.strip()][:cap]
    if isinstance(obj.get("attachments"), list):
        rec["attachments"] = [a for a in obj["attachments"] if isinstance(a, dict)][:ATTACH_CAP]
    if rec["sensitivity"] == "normal":
        rec["confidential_kind"] = "none"
    elif rec["confidential_kind"] == "none":
        rec["confidential_kind"] = "other"
    # A dated reminder of something the user owes is a todo, whatever the
    # model called it: "your recommendation is due in two days" filed as
    # fyi carries a deadline the lanes would otherwise never track.
    if rec["state"] == "fyi" and rec["deadline"] and _DUE_WORDS.search(
            f"{rec['deadline_text']} {rec['ask']}"):
        rec["state"] = "todo"
    return rec


_DUE_WORDS = re.compile(r"\b(?:due|overdue|deadline|late)\b", re.IGNORECASE)


def keep_reason(msg: dict, body: str, cfg: dict) -> str:
    """Why this message belongs to the remote model alone, or ''. Every
    participant is checked against the keep_from_anthropic list; with
    `admissions` on, a message from outside the institution that the admissions filter
    would move counts too. It labels the record and gates nothing."""
    people = [_addr(msg.get("from"))] + _addrs(msg.get("toRecipients")) + _addrs(msg.get("ccRecipients"))
    for a in people:
        why = remote_llm.keep_reason(a, cfg)
        if why:
            return why
    if (cfg.get("keep_from_anthropic") or {}).get("admissions", True):
        try:
            import admissions_filter as af
            frm = _addr(msg.get("from"))
            if frm and not af.is_internal(frm):
                score, _ = af.score_text(msg.get("subject") or "", body)
                if score >= af.MATCH_SCORE:
                    return "admissions"
        except Exception:
            pass
    return ""


_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_QUOTE_WORTH = re.compile(r"\d|\$|%|\?|https?://")
LINKS_CAP = 10
QUOTES_CAP = 3
QUOTE_CHARS = 220
ATTACH_CAP = 8
ATTACH_TEXT_CHARS = 2500
ATTACH_PDF_MAX = 2_000_000
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def links_in(text: str) -> list[str]:
    """The URLs the text names, in order, without duplicates."""
    out = []
    for u in _URL.findall(text or ""):
        u = u.rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out[:LINKS_CAP]


def quotes_in(text: str, *, skip: str = "") -> list[str]:
    """Up to three verbatim sentences that carry a figure, an amount, a
    question or a link: what a paraphrase loses. Sentences already
    repeated in `skip` (the summary) are left out."""
    out = []
    low = (skip or "").lower()
    for sent in _SENT_SPLIT.split(text or ""):
        sent = " ".join(sent.split())
        if len(sent) < 12 or sent.startswith(">") or not _QUOTE_WORTH.search(sent):
            continue
        if sent.lower()[:60] in low:
            continue
        out.append(sent[:QUOTE_CHARS])
        if len(out) >= QUOTES_CAP:
            break
    return out


def _verbatim_times(times: list[str], text: str) -> list[str]:
    """Only the offered times whose day the text names: by day number,
    weekday or month and day. A time the model inferred from "most days"
    or "next week" has no such anchor and is dropped."""
    low = (text or "").lower()
    out = []
    for t in times:
        try:
            d = datetime.fromisoformat(t[:19])
        except ValueError:
            continue
        day_num = re.search(rf"\b0?{d.day}(?:st|nd|rd|th)?\b", low)
        weekday = _WEEKDAYS[d.weekday()] in low or _WEEKDAYS[d.weekday()][:3] in re.findall(r"\b[a-z]{3}\b", low)
        month_day = re.search(rf"\b{_MONTHS[d.month - 1][:3]}[a-z]*\.?\s+0?{d.day}\b", low) or \
            re.search(rf"\b{d.month}/{d.day}\b", low) or (d.strftime("%Y-%m-%d") in low)
        if month_day or (day_num and weekday) or (weekday and len(times) <= 4) or day_num:
            out.append(t)
    return out


def attachments_of(msg: dict, graph) -> tuple[list[dict], str]:
    """(attachment list, text) for a message with attachments: name, type
    and size for each, and the text of up to two PDFs of 2 MB or less,
    for the model to read beside the body."""
    if not msg.get("hasAttachments") or not msg.get("id"):
        return [], ""
    try:
        atts = graph.get_attachments(msg["id"])
    except Exception as e:
        return [{"name": "(attachments unreadable)", "error": str(e)[:80]}], ""
    out, texts, pdfs = [], [], 0
    for a in atts[:ATTACH_CAP]:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "attachment")[:120]
        ctype = str(a.get("contentType") or "")[:60]
        size = int(a.get("size") or 0)
        item = {"name": name, "type": ctype, "size": size}
        is_pdf = ctype.lower() == "application/pdf" or name.lower().endswith(".pdf")
        if is_pdf and pdfs < 2 and 0 < size <= ATTACH_PDF_MAX:
            try:
                import base64
                import pdf_utils
                raw = a.get("contentBytes")
                data = base64.b64decode(raw) if raw else graph.download_attachment(msg["id"], a.get("id"))
                text = " ".join((pdf_utils.extract_text_from_pdf_bytes(data, max_pages=6) or "").split())
                if text:
                    item["text_chars"] = len(text)
                    texts.append(f"[Attachment {name}]\n{text[:ATTACH_TEXT_CHARS]}")
                    pdfs += 1
            except Exception as e:
                item["error"] = str(e)[:80]
        out.append(item)
    return out, "\n\n".join(texts)


# What the model is told about the user's own message. Its `ask` is then
# what he asks the recipients, which _own_result keeps as asked_of_them.
OWN_NOTE = ("(This message was sent by the user himself. In ask, write what he asks the "
            "recipients to answer, decide or send him, one sentence; \"\" when he asks them "
            "nothing. What he promises to do himself is not an ask.)")


def _own_result(res: dict) -> dict:
    """The record of the user's own message: nothing is asked of him by
    it, so needs_reply, ask and deadline are cleared; what he offered or
    promised stays in offered_times and the summary. What he asked the
    recipients is kept as asked_of_them, and awaits_reply says the next
    move in the thread is theirs."""
    res = dict(res)
    asked = str(res.get("ask") or "").strip()
    res["asked_of_them"] = asked
    # Only a stated ask: the model also calls a plain "thanks" awaiting-reply,
    # and a row that waits leaves the do-list.
    res["awaits_reply"] = bool(asked)
    res.update({"needs_reply": False, "ask": "", "deadline": "", "deadline_text": "",
                "state": "scheduling" if res.get("scheduling") not in (None, "none") else "fyi"})
    return res


def _message_text(msg: dict, body: str) -> str:
    frm = msg.get("from") or {}
    ea = frm.get("emailAddress") or {}
    return (f"From: {ea.get('name') or ''} <{ea.get('address') or ''}>\n"
            f"To: {', '.join(_addrs(msg.get('toRecipients'))[:10])}\n"
            f"Cc: {', '.join(_addrs(msg.get('ccRecipients'))[:10])}\n"
            f"Date: {msg.get('receivedDateTime') or ''}\n"
            f"Subject: {msg.get('subject') or ''}\n"
            f"Attachments: {'yes' if msg.get('hasAttachments') else 'no'}\n\n"
            f"{body}")


def extract(msg: dict, body: str, cfg: dict, *, chat=None) -> tuple[dict, dict]:
    """(validated record, call metadata). Raises remote_llm.RemoteLLMError."""
    chat = chat or remote_llm.chat_json
    system = PROMPT.format(profile=_profile_line(),
                           today=datetime.now().astimezone().strftime("%A %Y-%m-%d"))
    obj, meta = chat([{"role": "system", "content": system},
                      {"role": "user", "content": _message_text(msg, body)}],
                     purpose="mail-extract", schema=SCHEMA, schema_name="mail_record",
                     max_tokens=900, cfg=cfg)
    return validate(obj), meta


def _base_record(msg: dict) -> dict:
    frm = _addr(msg.get("from"))
    return {
        "ts": _iso(_now()),
        "key": message_key(msg),
        "id": msg.get("id") or "",
        "conversation_id": msg.get("conversationId") or "",
        "received": msg.get("receivedDateTime") or "",
        "from": frm,
        "from_domain": frm.rsplit("@", 1)[-1] if frm else "",
        "subject": (msg.get("subject") or "")[:200],
    }


def process(msg: dict, *, graph, cfg: dict, chat=None, full: dict | None = None,
            own: bool = False) -> dict:
    """One message: fetch its text, label it, extract. Returns the record
    to store. `full` is the message with its body when the caller already
    holds it; `own` marks the user's own message (the sealed reads and the
    thread briefs extract those too, the tick does not)."""
    rec = _base_record(msg)
    if own:
        rec["own"] = True
    try:
        import admissions_filter as af
        body = af.body_text(full if full is not None else graph.get_message_full(msg["id"]))
    except Exception as e:
        rec.update(status="error", error=f"fetch failed: {str(e)[:200]}")
        return rec
    rec["keep_from_anthropic"] = keep_reason(msg, body, cfg)
    rec["body_chars"] = len(body)
    attachments, att_text = attachments_of(msg, graph)
    model_body = body + ("\n\n" + att_text if att_text else "")
    if own:
        model_body = OWN_NOTE + "\n\n" + model_body
    try:
        fz = _frozen_setup()
        if fz:
            # The setup the tuning harness froze (mail_tuning.py freeze):
            # its instructions, thread context and worked examples.
            import mail_tuning
            # Not for his own message: given the thread, the model takes
            # the ask he answered for the one he made.
            conv = (mail_tuning.conversation(graph, msg.get("conversationId") or "")
                    if fz.get("context") and msg.get("conversationId") and not own else [])
            row = mail_tuning.live_row(msg, model_body, conv, mail_tuning._user_addresses())
            ans, meta = mail_tuning.run_frozen(fz, row, chat=chat)
            result = validate(mail_tuning.to_record(ans))
            for k in ("action", "urgency"):
                if k in ans:
                    result[k] = ans[k]
            rec["setup"] = fz.get("candidate", "")
        else:
            result, meta = extract(msg, model_body, cfg, chat=chat)
    except remote_llm.RemoteLLMError as e:
        rec.update(status="error", error=str(e)[:300])
        return rec
    except Exception as e:
        rec.update(status="error", error=f"frozen setup failed: {str(e)[:250]}")
        return rec
    # What a paraphrase loses, taken from the text in code: the links,
    # a few verbatim sentences with figures or questions, the files.
    result["links"] = links_in(model_body)
    result["quotes"] = quotes_in(body, skip=result.get("summary", ""))
    result["attachments"] = attachments
    result["offered_times"] = _verbatim_times(result.get("offered_times") or [], model_body)
    if own:
        result = _own_result(result)
    rec.update(status="ok", latency_ms=meta.get("latency_ms"), result=result)
    return rec


def _frozen_setup() -> dict | None:
    try:
        import mail_tuning
        return mail_tuning.frozen()
    except Exception:
        return None


def _default_graph():
    import graph_client
    return graph_client


def _received_since(graph, since: datetime) -> list[dict]:
    """Every message received since `since`, any folder, oldest first;
    drafts left out."""
    rows = graph.graph_get_all("/me/messages", {
        "$select": graph.MAIL_FIELDS,
        "$filter": f"receivedDateTime ge {_iso(since)}",
        "$orderby": "receivedDateTime desc",
        "$top": 100,
    })
    rows = [r for r in rows if not r.get("isDraft")]
    rows.sort(key=lambda r: r.get("receivedDateTime") or "")
    return rows


def run_pass(msgs: list[dict], *, graph, cfg: dict, state: dict, cap: int | None,
             user: set[str], chat=None, log=print) -> dict:
    counts = {"ok": 0, "error": 0, "skipped": 0, "kept": 0}
    done = 0
    for msg in msgs:
        if cap is not None and done >= cap:
            break
        key = message_key(msg)
        if key in state["seen"] or _addr(msg.get("from")) in user:
            counts["skipped"] += 1
            continue
        if (load_index()["by_key"].get(key) or {}).get("status") == "ok":
            # The mail gate read it first, inside a preprocessor run.
            state["seen"][key] = _iso(_now())
            counts["skipped"] += 1
            continue
        rec = process(msg, graph=graph, cfg=cfg, chat=chat)
        done += 1
        counts[rec["status"]] += 1
        counts["kept"] += bool(rec.get("keep_from_anthropic"))
        remote_llm.append(EXTRACTIONS, rec)
        if rec["status"] == "error":
            n = state["attempts"].get(key, 0) + 1
            state["attempts"][key] = n
            if n < RETRY_LIMIT:
                continue
        state["seen"][key] = rec["ts"]
    return counts


OWN_SENT_KEEP = timedelta(days=30)
OWN_SENT_BOOTSTRAP = timedelta(days=7)


def _note_own_sends(st: dict, msgs: list[dict], user: set[str], *, graph,
                    now: datetime) -> None:
    """Keep {conversation id: the user's latest send} in the tick state, so
    sync_briefs refetches a thread he wrote in. The listing spans every
    folder, Sent Items included; the first tick after an upgrade reads a
    week of Sent Items to start the map."""
    sent = st.get("own_sent")
    if not isinstance(sent, dict):
        sent = {}
        try:
            for m in _sent_since(graph, now - OWN_SENT_BOOTSTRAP):
                conv, at = m.get("conversationId") or "", m.get("sentDateTime") or ""
                if conv and at > str(sent.get(conv) or ""):
                    sent[conv] = at
        except Exception as e:
            print(f"mail_privacy: sent items unreadable: {str(e)[:120]}", file=sys.stderr)
    for m in msgs:
        if m.get("isDraft") or _addr(m.get("from")) not in user:
            continue
        conv = m.get("conversationId") or ""
        at = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        if conv and at > str(sent.get(conv) or ""):
            sent[conv] = at
    cutoff = _iso(now - OWN_SENT_KEEP)
    st["own_sent"] = {c: t for c, t in sent.items() if str(t) >= cutoff}


def tick(*, graph=None, chat=None, now: datetime | None = None) -> dict:
    cfg = remote_llm.config()
    if not remote_llm.reading("mail", cfg):
        return {"skipped": "mail switch is off"}
    graph = graph or _default_graph()
    now = now or _now()
    try:
        with _tick_lock():
            st = load_state()
            last = _parse(st.get("last_tick") or "")
            since = (last - TICK_OVERLAP) if last else (now - FIRST_LOOKBACK)
            msgs = _received_since(graph, since)
            user = _user_addresses()
            _note_own_sends(st, msgs, user, graph=graph, now=now)
            counts = run_pass(msgs, graph=graph, cfg=cfg, state=st, cap=TICK_CAP,
                              user=user, chat=chat)
            # A capped pass leaves the clock where it was, so the next
            # tick picks up the rest.
            if counts["ok"] + counts["error"] < TICK_CAP:
                st["last_tick"] = _iso(now)
            save_state(st)
    except _Busy:
        return {"skipped": "another pass is running"}
    out = {"listed": len(msgs), **counts}
    if remote_llm.enforcing("mail", cfg):
        try:
            out["kept_rows"] = sync_kept_rows(now=now)
        except Exception as e:
            out["kept_rows"] = {"error": str(e)[:200]}
        try:
            out["briefs"] = sync_briefs(graph=graph, chat=chat, now=now,
                                        own_sent=st.get("own_sent") or {})
        except Exception as e:
            out["briefs"] = {"error": str(e)[:200]}
    try:
        import ghost_week
        tw = ghost_week.tripwire(now=now, cfg=cfg)
        if tw.get("alerts"):
            out["tripwire"] = tw
    except Exception as e:
        out["tripwire"] = {"error": str(e)[:200]}
    return out


def backfill(days: int, *, graph=None, chat=None) -> dict:
    cfg = remote_llm.config()
    if not remote_llm.reading("mail", cfg):
        return {"skipped": "mail switch is off"}
    graph = graph or _default_graph()
    start = _now()
    try:
        with _tick_lock():
            st = load_state()
            # Ticks resume from the moment the backfill began, so mail
            # that arrives while it runs is not skipped.
            if not st.get("last_tick"):
                st["last_tick"] = _iso(start)
            save_state(st)
            msgs = _received_since(graph, start - timedelta(days=days))
            counts = {"ok": 0, "error": 0, "skipped": 0, "kept": 0}
            user = _user_addresses()
            for i in range(0, len(msgs), 25):
                part = run_pass(msgs[i:i + 25], graph=graph, cfg=cfg, state=st, cap=None,
                                user=user, chat=chat)
                for k in counts:
                    counts[k] += part[k]
                save_state(st)
    except _Busy:
        return {"skipped": "another pass is running"}
    return {"listed": len(msgs), **counts}


def redo_withheld(days: int = 8, *, graph=None, chat=None) -> dict:
    """Re-extract every message an earlier version held back from the
    remote model: their keys leave the seen set, then a backfill over
    `days` picks them up. The new record supersedes the old one."""
    if not remote_llm.reading("mail"):
        return {"skipped": "mail switch is off"}
    keys = {k for k, r in load_index()["by_key"].items() if r.get("status") == "withheld"}
    try:
        with _tick_lock():
            st = load_state()
            for k in keys:
                st["seen"].pop(k, None)
            save_state(st)
    except _Busy:
        return {"skipped": "another pass is running"}
    res = backfill(days, graph=graph, chat=chat)
    res["redone"] = len(keys)
    return res


# ── the index of what has been extracted ──────────────────────────────

_INDEX_CACHE: dict = {"mtime": None, "index": None}


def load_index() -> dict:
    """{"by_id": {id: rec}, "by_conv": {conv: [rec, ...]}} over the
    latest record per message key. Cached by file mtime for the
    long-running mediator."""
    path = remote_llm.store_dir() / EXTRACTIONS
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"by_id": {}, "by_conv": {}, "by_key": {}}
    # The key is the modification time and the size: two appends inside
    # one timestamp tick (a filesystem with coarse stamps, a fast run)
    # would otherwise serve the earlier index for the later file.
    mtime = (st.st_mtime_ns, st.st_size)
    if _INDEX_CACHE["mtime"] == mtime and _INDEX_CACHE["index"] is not None:
        return _INDEX_CACHE["index"]
    by_key = {}
    for rec in remote_llm.read(EXTRACTIONS):
        k = rec.get("key")
        if k:
            prev = by_key.get(k)
            if prev is None or prev.get("status") != "ok" or rec.get("status") == "ok":
                by_key[k] = rec
    by_id, by_conv = {}, {}
    for rec in by_key.values():
        if rec.get("id"):
            by_id[rec["id"]] = rec
        if rec.get("conversation_id"):
            by_conv.setdefault(rec["conversation_id"], []).append(rec)
    index = {"by_id": by_id, "by_conv": by_conv, "by_key": by_key}
    _INDEX_CACHE.update(mtime=mtime, index=index)
    return index


def _kept(rec: dict) -> bool:
    return bool(rec.get("keep_from_anthropic")) or rec.get("status") == "withheld"


# ── kept mail that asks for something: a ledger row ───────────────────
# A cron lane never sees a kept message (release_filter prunes it), so an
# ask in one would reach nothing but the remote digest. The owner's tick
# writes a row for it instead, from the record alone: the subject, the
# remote model's one-line summary, its ask and its deadline, typed by the
# record's state. The message text stays out of Claude's reach. The row
# is not a detector row, so an archive does not close it; it closes when
# the user answers in the thread, when the thread's latest inbound record
# asks nothing more, or by the user's word.

KEPT_PREFIX = "kept-"
KEPT_ROW_LOOKBACK = timedelta(days=14)
KEPT_NOTE_CHARS = 700
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv)\s*:\s*)+", re.IGNORECASE)


def kept_row_type(res: dict) -> str:
    """The ledger type for a kept record's ask."""
    state = res.get("state")
    if state == "scheduling" or res.get("scheduling") not in (None, "none"):
        return "scheduling"
    if res.get("deadline"):
        return "deadline"
    if state in ("todo", "decide"):
        return "task"
    return "response_needed"


def _subject_key(subject: str) -> str:
    return re.sub(r"\s+", " ", _REPLY_PREFIX.sub("", subject or "")).strip().lower()


def _domain(addr: str) -> str:
    return (addr or "").rsplit("@", 1)[-1].lower()


def kept_row_fields(rec: dict) -> dict:
    """What a kept record's row carries: the subject, the remote model's
    summary, the ask and the deadline; never the text."""
    res = rec.get("result") or {}
    reason = rec.get("keep_from_anthropic") or "withheld"
    notes = [f"Kept from Claude ({reason}); written by the remote model."]
    if res.get("summary"):
        notes.append(str(res["summary"]).strip())
    if res.get("ask"):
        notes.append(f"Ask: {str(res['ask']).strip()}")
    if res.get("deadline"):
        notes.append(f"Due {res['deadline']}" + (f" ({res['deadline_text']})" if res.get("deadline_text") else "") + ".")
    notes.append("The text is in the mail digest; ask in #ea to act on it.")
    return {"type": kept_row_type(res),
            "description": (rec.get("subject") or "(no subject)").strip()[:300],
            "notes": " ".join(notes)[:KEPT_NOTE_CHARS],
            "deadline": res.get("deadline") or "",
            "from_email": rec.get("from") or "",
            "source_message_id": rec.get("id") or "",
            "conversation_id": rec.get("conversation_id") or "",
            "metadata": {"kept_from_claude": reason, "remote_state": res.get("state"),
                         "kept_received": rec.get("received") or "", "written_by": "remote model"}}


def _kept_row_id(rec: dict) -> str:
    key = rec.get("conversation_id") or rec.get("id") or ""
    return f"{KEPT_PREFIX}{key[-12:]}"


def _latest_in_thread(recs: list[dict]) -> tuple[dict | None, dict | None]:
    """(latest inbound record, latest own record) of one conversation."""
    inbound = [r for r in recs if not r.get("own") and r.get("status") == "ok"]
    own = [r for r in recs if r.get("own")]
    key = lambda r: r.get("received") or ""  # noqa: E731
    return (max(inbound, key=key) if inbound else None, max(own, key=key) if own else None)


def kept_row_plan(index: dict, rows: list[dict], *, user: set[str], now: datetime,
                  lookback: timedelta = KEPT_ROW_LOOKBACK) -> dict:
    """Pure: what sync_kept_rows should write. `rows` are the ledger rows
    that could already cover a kept thread (any status; id, status,
    conversation_id, source_message_id, from_email, description,
    deadline, metadata). Returns {"create": [fields], "update": [(id,
    fields)], "close": [(id, note, signal, evidence_ts)]}."""
    import mail_gate
    by_conv = index.get("by_conv") or {}
    tracked_conv = {r.get("conversation_id") for r in rows if r.get("conversation_id")}
    tracked_msg = {r.get("source_message_id") for r in rows if r.get("source_message_id")}
    open_kept = [r for r in rows if str(r.get("id", "")).startswith(KEPT_PREFIX)
                 and r.get("status") not in ("completed", "closed")]
    plan = {"create": [], "update": [], "close": []}

    # The newest actionable kept record per conversation, inside the window.
    # Mail kept for the admissions rule is the admissions filter's: it is
    # moved to its folder and digested weekly, and is not an obligation.
    latest: dict[str, dict] = {}
    for rec in (index.get("by_key") or {}).values():
        if not _kept(rec) or rec.get("status") != "ok" or rec.get("own"):
            continue
        if rec.get("keep_from_anthropic") == "admissions":
            continue
        if (rec.get("from") or "").lower() in user or not mail_gate.actionable(rec):
            continue
        got = _parse(rec.get("received") or "")
        if not got or now - got > lookback:
            continue
        res = rec.get("result") or {}
        # A meeting notice whose date has passed is over, not owed.
        if kept_row_type(res) == "scheduling" and res.get("deadline") and \
                str(res["deadline"])[:10] < now.date().isoformat():
            continue
        conv = rec.get("conversation_id") or rec.get("id") or ""
        if conv not in latest or (rec.get("received") or "") > (latest[conv].get("received") or ""):
            latest[conv] = rec

    for conv, rec in sorted(latest.items(), key=lambda kv: kv[1].get("received") or ""):
        fields = kept_row_fields(rec)
        if conv in tracked_conv or rec.get("id") in tracked_msg:
            # A row covers the thread. A kept row of ours learns the newer ask.
            for r in open_kept:
                if r.get("conversation_id") == conv and \
                        (rec.get("received") or "") > str((r.get("metadata") or {}).get("kept_received") or ""):
                    plan["update"].append((r["id"], {"notes": fields["notes"], "deadline": fields["deadline"],
                                                     "kept_received": rec.get("received") or "",
                                                     "last_action": f"{now.date().isoformat()}: a newer message in the thread; the note is its record"}))
            continue
        # The same automated sender nudging again under the same subject
        # (one conversation per notice) folds onto the open row.
        twin = next((r for r in open_kept
                     if _domain(r.get("from_email", "")) == _domain(fields["from_email"])
                     and _subject_key(r.get("description", "")) == _subject_key(fields["description"])), None)
        if twin is not None:
            plan["update"].append((twin["id"], {"notes": fields["notes"], "deadline": fields["deadline"] or twin.get("deadline") or "",
                                                "kept_received": rec.get("received") or "",
                                                "last_action": f"{now.date().isoformat()}: nudged again ({rec.get('received', '')[:10]}); the note is the newer record"}))
            continue
        fields["id"] = _kept_row_id(rec)
        plan["create"].append(fields)
        tracked_conv.add(conv)
        open_kept.append({"id": fields["id"], "status": "open", "conversation_id": conv,
                          "from_email": fields["from_email"], "description": fields["description"],
                          "deadline": fields["deadline"], "metadata": fields["metadata"]})

    # Closure, from the thread's records alone.
    for r in open_kept:
        if r.get("id") in {c["id"] for c in plan["create"]}:
            continue
        recs = by_conv.get(r.get("conversation_id") or "") or []
        inbound, own = _latest_in_thread(recs)
        since = str((r.get("metadata") or {}).get("kept_received") or "")
        # Each is a signal for the closure policy, which proposes: a record
        # names no recipients, so the user's message may be a forward, and
        # "asks nothing" is the remote model's reading.
        # A reply that asks them something (a question, a slot length) has
        # put the next move on their side: the row waits, it is not done.
        if own and (own.get("received") or "") > since and \
                not (own.get("result") or {}).get("awaits_reply"):
            plan["close"].append((r["id"], f"the user replied in the thread on {str(own.get('received'))[:10]}",
                                  "user_wrote_in_thread", own.get("received") or ""))
        elif inbound and (inbound.get("received") or "") > since and not mail_gate.actionable(inbound):
            plan["close"].append((r["id"], f"the thread moved on: the latest message ({str(inbound.get('received'))[:10]}) asks nothing",
                                  "llm:mail-tick", inbound.get("received") or ""))
    return plan


def _ledger_rows_for_kept() -> list[dict]:
    import ledger
    con = ledger.guarded_connect(ledger.DB_PATH, read_only=True)
    try:
        con.row_factory = __import__("sqlite3").Row
        out = []
        for r in con.execute("SELECT id, status, conversation_id, source_message_id, from_email, "
                             "description, deadline, metadata FROM ledger"):
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except (TypeError, ValueError):
                d["metadata"] = {}
            out.append(d)
        return out
    finally:
        con.close()


def sync_kept_rows(*, now: datetime | None = None, index: dict | None = None,
                   rows: list[dict] | None = None, user: set[str] | None = None,
                   write=None) -> dict:
    """Write the kept-mail rows the plan says to (the tick does this when
    mail is enforced). `write` is a seam: (verb, id, fields) -> None."""
    import ledger
    now = now or _now()
    plan = kept_row_plan(index or load_index(), rows if rows is not None else _ledger_rows_for_kept(),
                         user=user if user is not None else _user_addresses(), now=now)
    done = {"created": [], "updated": [], "closed": [], "errors": []}

    def apply(verb: str, rid: str, fields: dict) -> None:
        if write is not None:
            write(verb, rid, fields)
            return
        if verb == "create":
            ledger.add_entry(rid, fields["type"], fields["description"], from_email=fields["from_email"],
                             source_message_id=fields["source_message_id"], conversation_id=fields["conversation_id"],
                             notes=fields["notes"], deadline=fields["deadline"] or None, metadata=fields["metadata"])
            return
        db = ledger._get_db()
        try:
            ts = ledger._now()
            if verb == "update":
                db.execute("UPDATE ledger SET notes = ?, deadline = COALESCE(NULLIF(?, ''), deadline), "
                           "last_action = ?, updated_at = ? WHERE id = ?",
                           (fields["notes"], fields.get("deadline") or "", fields["last_action"], ts, rid))
            else:
                import closure
                closure.settle(db, rid, signal=fields.get("signal") or "llm:mail-tick",
                               actor="mail_privacy", evidence_ts=fields.get("evidence_ts") or "",
                               note=f"{now.date().isoformat()}: auto-closed by the mail tick: {fields['note']}")
            db.commit()
        finally:
            db.close()
        if verb == "update":
            ledger.set_metadata(rid, kept_received=fields.get("kept_received") or "")

    for fields in plan["create"]:
        try:
            apply("create", fields["id"], fields)
            done["created"].append(fields["id"])
        except Exception as e:
            done["errors"].append(f"{fields['id']}: {str(e)[:120]}")
    for rid, fields in plan["update"]:
        try:
            apply("update", rid, fields)
            done["updated"].append(rid)
        except Exception as e:
            done["errors"].append(f"{rid}: {str(e)[:120]}")
    for rid, note, *rest in plan["close"]:
        try:
            apply("close", rid, {"note": note, "signal": rest[0] if rest else "",
                                 "evidence_ts": rest[1] if len(rest) > 1 else ""})
            done["closed"].append(rid)
        except Exception as e:
            done["errors"].append(f"{rid}: {str(e)[:120]}")
    return done


def conversation_class(conv_id: str, index: dict) -> str:
    """kept | confidential | normal | unknown for one thread. `kept` means
    an enforce mode would let only the remote model see it."""
    recs = index["by_conv"].get(conv_id) or []
    if not recs:
        return "unknown"
    if any(_kept(r) for r in recs):
        return "kept"
    if any((r.get("result") or {}).get("sensitivity") == "confidential" for r in recs):
        return "confidential"
    if any(r.get("status") == "ok" for r in recs):
        return "normal"
    return "unknown"


# ── the mediator hook ─────────────────────────────────────────────────

RELEASE_VERBS = {"get-message", "get-conversation", "search-mail", "list-mail",
                 "get-attachments", "scan-attachments", "download-attachment"}


def _walk_ids(obj, conv: str = "", out=None) -> list[tuple[str, str]]:
    """(message id, conversation id) pairs anywhere in a verb's JSON."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        conv = str(obj.get("conversation_id") or obj.get("conversationId") or conv or "")
        mid = obj.get("id") or obj.get("message_id") or ""
        if isinstance(mid, str) and len(mid) > 40:
            out.append((mid, conv))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_ids(v, conv, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_ids(v, conv, out)
    return out


def note_release(tool: str, verb: str, argv: list, run_id: str, stdout: str) -> None:
    """Record which threads a mediated mail read handed to a lane, and
    their shadow class. Called by the mediator after it has answered;
    never raises and never changes what the caller received."""
    try:
        if tool != "graph_actions" or verb not in RELEASE_VERBS:
            return
        if not remote_llm.reading("mail"):
            return
        try:
            data = json.loads(stdout or "")
        except ValueError:
            data = None
        pairs = _walk_ids(data) if data is not None else []
        index = load_index()
        convs = set()
        for mid, conv in pairs:
            if not conv:
                rec = index["by_id"].get(mid)
                conv = (rec or {}).get("conversation_id", "")
            if conv:
                convs.add(conv)
        for flag in ("--conversation-id", "--message-id"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 < len(argv):
                    v = str(argv[i + 1])
                    if flag == "--conversation-id":
                        convs.add(v)
                    else:
                        rec = index["by_id"].get(v)
                        if rec and rec.get("conversation_id"):
                            convs.add(rec["conversation_id"])
        classes: dict[str, int] = {}
        flagged = []
        for c in convs:
            cls = conversation_class(c, index)
            classes[cls] = classes.get(cls, 0) + 1
            if cls in ("confidential", "kept"):
                flagged.append(c)
        rid = str(run_id or "")[:64]
        remote_llm.append(RELEASES, {
            "ts": _iso(_now()), "verb": verb, "run_id": rid,
            "shadow": rid.startswith("shadow-"),
            "sealed": remote_llm.mode("mail") == "sealed",
            "messages": len(pairs), "threads": len(convs),
            "classes": classes, "flagged_threads": flagged[:20],
        })
    except Exception as e:
        print(f"mail_privacy.note_release: {e}", file=sys.stderr)


# ── enforce and sealed: what a lane's mail read returns ──────────────
# In the `enforce` mode a cron lane (run id `cron-...`) reads no thread on
# the keep_from_anthropic list: a read of one such message or thread is
# refused before it runs, and search and list results lose those rows.
# Bot runs (a room, a thread, the DM) read as before.
#
# In the `ghost` mode the lanes get what `enforce` gives them, and each
# read is sealed on the side, in a thread of its own, so the ghost week
# can grade what sealed mail would have cost (ghost_week.py).
#
# In the `sealed` mode the cron rules hold, and every lane's read of mail
# text comes back as the remote model's record instead: get-message and
# get-conversation carry the summary, the ask, the deadline and the offered
# times in place of the body; search-mail and list-mail lose their previews
# and gain the summary. A message the remote model has not read yet is
# extracted here, inside the mediator, within SEAL_BUDGET_S; past the
# budget it comes back as sender, subject and ids with `remote_unread`.
# The user's own messages in a thread are extracted the same way, so a
# brief of a thread says what he answered. Any failure while sealing
# refuses the read: raw text never passes by accident.

KEPT_REFUSAL = ("This thread is kept from Claude (keep_from_anthropic). A cron lane "
                "does not read it; the remote digest covers it, and the user can ask "
                "for it in a room or thread.")
SEALED_NOTE = ("Sealed mail: the text of each message is replaced by the remote model's "
               "record (summary, ask, deadline, offered times). A message marked "
               "remote_unread has not been read by it yet; try again in a minute or ask "
               "the user. The ledger row's thread_brief carries the same facts.")
_HEADER_KEYS = ("from", "to", "cc", "sender", "recipients", "participants")
SEAL_VERBS = {"get-message", "get-conversation", "search-mail", "list-mail"}
SEAL_BUDGET_S = 75.0
SEAL_INLINE_CAP = 12        # threads fetched per sync_briefs pass
INLINE_MSG_CAP = 40         # messages one sealed read extracts inline, within the budget


def _cron_lane(run_id: str) -> bool:
    return str(run_id or "").startswith("cron-")


def _flag_value(argv: list, flag: str) -> str:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return str(argv[i + 1])
        if str(a).startswith(flag + "="):
            return str(a).split("=", 1)[1]
    return ""


def _row_kept(row: dict, index: dict, cfg: dict) -> bool:
    mid = row.get("id") or row.get("message_id") or ""
    rec = index["by_id"].get(mid)
    if rec is not None and _kept(rec):
        return True
    conv = row.get("conversation_id") or row.get("conversationId") or (rec or {}).get("conversation_id") or ""
    if conv and conversation_class(conv, index) == "kept":
        return True
    heads = json.dumps({k: row.get(k) for k in _HEADER_KEYS if k in row}, default=str)
    return any(remote_llm.keep_reason(a, cfg) for a in _ADDR.findall(heads))


def release_precheck(tool: str, verb: str, argv: list, run_id: str) -> str:
    """'' or the refusal for a cron lane's read of one kept message or
    thread, decided before the read runs."""
    if tool != "graph_actions" or verb not in RELEASE_VERBS or not _cron_lane(run_id):
        return ""
    if not remote_llm.enforcing("mail"):
        return ""
    try:
        index = load_index()
        conv = _flag_value(argv, "--conversation-id")
        mid = _flag_value(argv, "--message-id")
        if mid:
            rec = index["by_id"].get(mid)
            if rec is not None and _kept(rec):
                return KEPT_REFUSAL
            conv = conv or (rec or {}).get("conversation_id", "")
        if conv and conversation_class(conv, index) == "kept":
            return KEPT_REFUSAL
        return ""
    except Exception as e:
        return f"The mail gate could not check this read ({str(e)[:120]}); refused for a cron lane."


def _prune_kept(data, index: dict, cfg: dict) -> tuple[object, str]:
    """(data without kept rows, refusal) for a cron lane's read after it
    ran: kept rows are removed from a list; a single kept message or
    thread is refused."""
    if isinstance(data, list):
        rows = [r for r in data if not (isinstance(r, dict) and r.get("id") and _row_kept(r, index, cfg))]
        if len(rows) != len(data):
            rows.append({"_note": f"{len(data) - len(rows)} result(s) kept from Claude were left out."})
        return rows, ""
    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list) and any(isinstance(m, dict) and _row_kept(m, index, cfg) for m in msgs):
            return None, KEPT_REFUSAL
        if data.get("id") and _row_kept(data, index, cfg):
            return None, KEPT_REFUSAL
    return data, ""


def _ghost(verb: str, argv: list, run_id: str, stdout: str) -> None:
    """Seal this served read on the side and record both (ghost_week).
    Runs in its own thread; never raises and never changes the answer."""
    try:
        import ghost_week
        ghost_week.ghost_read_async(verb, list(argv), run_id, stdout)
    except Exception as e:
        print(f"mail_privacy: ghost read: {str(e)[:120]}", file=sys.stderr)


def _ghost_record(verb: str, argv: list, run_id: str, stdout: str, **kw) -> None:
    try:
        import ghost_week
        ghost_week.record_read(verb, list(argv), run_id, stdout, served="sealed", **kw)
    except Exception as e:
        print(f"mail_privacy: ghost record: {str(e)[:120]}", file=sys.stderr)


def release_filter(tool: str, verb: str, argv: list, run_id: str, stdout: str) -> tuple[str, str]:
    """(stdout, refusal) for a lane's read after it ran. enforce: a cron
    lane loses kept rows and is refused a kept message or thread. ghost:
    the same, and what the lane got is sealed on the side and recorded
    for the ghost week. sealed: the cron rules, and every lane gets the
    remote model's records in place of message text; each sealed read is
    recorded the same way."""
    if tool != "graph_actions" or verb not in RELEASE_VERBS:
        return stdout, ""
    m = remote_llm.mode("mail")
    if m not in ("enforce", "ghost", "sealed"):
        return stdout, ""
    cron = _cron_lane(run_id)
    if m == "enforce" and not cron:
        return stdout, ""
    if m == "ghost" and not cron:
        if verb in SEAL_VERBS:
            _ghost(verb, argv, run_id, stdout)
        return stdout, ""
    try:
        if not (stdout or "").strip():
            return stdout, ""
        data = json.loads(stdout)
        index, cfg = load_index(), remote_llm.config()
        changed = False
        if cron:
            pruned, refusal = _prune_kept(data, index, cfg)
            if refusal:
                return "", refusal
            changed = pruned is not data or (isinstance(pruned, list) and len(pruned) != len(data))
            data = pruned
        out = json.dumps(data, indent=2, default=str) if changed else stdout
        if m == "ghost":
            if verb in SEAL_VERBS:
                _ghost(verb, argv, run_id, out)
            return out, ""
        if m == "sealed" and verb in SEAL_VERBS:
            # A fresh parse: seal_output edits in place, and the record
            # keeps the text the lane would have had.
            t0 = time.monotonic()
            try:
                sealed = seal_output(verb, json.loads(out), index=index, cfg=cfg)
            except Exception as e:
                _ghost_record(verb, argv, run_id, out, seal_s=time.monotonic() - t0,
                              seal_error=str(e), index=index)
                raise
            _ghost_record(verb, argv, run_id, out, sealed=sealed, seal_s=time.monotonic() - t0,
                          index=index)
            return json.dumps(sealed, indent=2, default=str), ""
        return out, ""
    except Exception as e:
        return "", f"The mail gate could not check this read ({str(e)[:120]}); refused."


# ── sealed: the remote model's record in place of the text ───────────

def _sealed_fields(rec: dict | None) -> dict:
    """The record's fields a lane gets with a sealed message."""
    if not rec:
        return {"remote_unread": True, "remote_text": ""}
    if rec.get("status") != "ok":
        out = {"remote_unread": True, "remote_text": ""}
        if _kept(rec):
            out["kept_from_claude"] = rec.get("keep_from_anthropic") or "withheld"
        return out
    import mail_gate
    res = rec.get("result") or {}
    out = {"remote_text": mail_gate.remote_text(rec),
           "remote_record": {k: res.get(k) for k in (
               "state", "needs_reply", "ask", "deadline", "scheduling",
               "offered_times", "people", "tone", "domain", "sensitivity")}}
    for k in ("links", "quotes", "attachments"):
        if res.get(k):
            out["remote_record"][k] = res[k]
    if rec.get("own"):
        out["own"] = True
    if rec.get("keep_from_anthropic"):
        out["kept_from_claude"] = rec["keep_from_anthropic"]
    return out


def _inline_records(msgs: list[dict], *, graph, cfg: dict, chat=None,
                    budget_s: float = SEAL_BUDGET_S, user: set[str] | None = None) -> None:
    """Extract these fetched messages now, a few at a time, for at most
    budget_s and at most INLINE_MSG_CAP messages (a cap of twelve left the
    rest of every long thread unread in the backtest); each record lands
    in the store as it finishes. Workers are daemon threads, so a late
    answer never holds the caller."""
    import queue
    import threading
    if not msgs:
        return
    user = user if user is not None else _user_addresses()
    todo: queue.Queue = queue.Queue()
    for m in msgs[:INLINE_MSG_CAP]:
        todo.put(m)
    left = [min(len(msgs), INLINE_MSG_CAP)]
    lock = threading.Lock()
    done = threading.Event()

    def work():
        while True:
            try:
                m = todo.get_nowait()
            except queue.Empty:
                return
            try:
                rec = process(m, graph=graph, cfg=cfg, chat=chat, full=m,
                              own=_addr(m.get("from")) in user)
                remote_llm.append(EXTRACTIONS, rec)
            except Exception as e:
                print(f"mail_privacy: inline read failed: {str(e)[:120]}", file=sys.stderr)
            with lock:
                left[0] -= 1
                if left[0] == 0:
                    done.set()

    for _ in range(min(remote_llm.max_parallel(cfg), left[0])):
        threading.Thread(target=work, daemon=True).start()
    done.wait(timeout=budget_s)


def _graph_message(m: dict) -> dict:
    """A get-message or get-conversation row as the Graph message the
    extractor reads (the row carries the text, so no second fetch)."""
    frm = m.get("from")
    if isinstance(frm, dict) and "emailAddress" not in frm:
        frm = {"emailAddress": frm}
    elif isinstance(frm, str):
        frm = {"emailAddress": {"address": frm}}
    text = m.get("text") if m.get("text") is not None else m.get("body") or ""
    return {"id": m.get("id") or "", "conversationId": m.get("conversation_id") or "",
            "receivedDateTime": m.get("received") or m.get("sent") or "",
            "subject": m.get("subject") or "", "from": frm,
            "toRecipients": [{"emailAddress": (a if isinstance(a, dict) else {"address": a})}
                             for a in m.get("to") or []],
            "ccRecipients": [{"emailAddress": (a if isinstance(a, dict) else {"address": a})}
                             for a in m.get("cc") or []],
            "hasAttachments": bool(m.get("has_attachments")),
            "body": {"contentType": "text", "content": str(text)}}


def seal_output(verb: str, data, *, index: dict | None = None, cfg: dict | None = None,
                graph=None, chat=None, budget_s: float = SEAL_BUDGET_S):
    """The read verb's JSON with every message text replaced by the remote
    model's record. Messages the model has not read are extracted here
    first, within the budget."""
    index = index if index is not None else load_index()
    cfg = cfg if cfg is not None else remote_llm.config()
    if isinstance(data, dict) and data.get("error"):
        return data
    if verb == "get-message" and isinstance(data, dict):
        rows = [data]
    elif verb == "get-conversation" and isinstance(data, dict):
        rows = [m for m in data.get("messages") or [] if isinstance(m, dict)]
        for m in rows:
            m.setdefault("conversation_id", data.get("conversation_id") or "")
    elif isinstance(data, list):
        rows = [m for m in data if isinstance(m, dict) and m.get("id")]
    else:
        return data

    text_rows = [m for m in rows if verb in ("get-message", "get-conversation")]
    unread = [m for m in text_rows if m.get("id") and index["by_id"].get(m["id"]) is None
              and not m.get("is_draft")]
    if unread:
        _inline_records([_graph_message(m) for m in unread], graph=graph or _default_graph(),
                        cfg=cfg, chat=chat, budget_s=budget_s)
        index = load_index()

    for m in rows:
        rec = index["by_id"].get(m.get("id") or "")
        fields = _sealed_fields(rec)
        for k in ("text", "body", "body_type", "body_text", *PREVIEW_KEYS):
            m.pop(k, None)
        m["text" if verb == "get-conversation" else "body" if verb == "get-message"
          else "preview"] = fields.pop("remote_text")
        m.update(fields)
    if isinstance(data, dict):
        data["sealed"] = True
        data["_note"] = SEALED_NOTE
    elif rows:
        data.append({"_note": SEALED_NOTE})
    return data


# ── the thread brief: the ledger row's account of its thread ─────────
# A thread brief is composed in code from the remote model's records of
# every message in a conversation, the user's own included: one line per
# message, then the latest ask, deadline, offered times and people. It is
# written into the ledger row's metadata as `thread_brief`, so a Claude
# session reads `ledger.py get --id <id>` and needs no mail read at all.

def thread_brief(conv_id: str, index: dict | None = None, *, user: set[str] | None = None,
                 now: datetime | None = None) -> dict | None:
    index = index if index is not None else load_index()
    user = user if user is not None else _user_addresses()
    recs = sorted((index["by_conv"].get(conv_id) or []), key=lambda r: r.get("received") or "")
    if not recs:
        return None
    msgs, people, kept = [], [], ""
    latest_in = None
    for r in recs:
        res = r.get("result") or {}
        own = bool(r.get("own")) or (r.get("from") or "") in user
        m = {"id": r.get("id") or "", "from": r.get("from") or "", "received": r.get("received") or "",
             "own": own}
        if r.get("status") == "ok":
            m["gist"] = res.get("summary") or ""
            for k in ("links", "quotes"):
                if res.get(k):
                    m[k] = list(res[k])
            if res.get("attachments"):
                m["attachments"] = [a.get("name") for a in res["attachments"] if isinstance(a, dict)]
            if not own:
                m.update({"state": res.get("state"), "ask": res.get("ask") or "",
                          "deadline": res.get("deadline") or "",
                          "offered_times": list(res.get("offered_times") or [])})
                latest_in = (r, res)
            elif res.get("awaits_reply"):
                m.update({"awaits_reply": True, "asked": res.get("asked_of_them") or ""})
            for p in res.get("people") or []:
                if isinstance(p, dict) and p.get("name") and p not in people:
                    people.append({"name": p["name"], "role": p.get("role", "")})
        else:
            m["unread"] = True
        if _kept(r):
            kept = kept or r.get("keep_from_anthropic") or "withheld"
        msgs.append(m)
    own_msgs = [m for m in msgs if m["own"]]
    last_in = max((m for m in msgs if not m["own"]), key=lambda m: m["received"], default=None)
    brief = {"updated": _iso(now or _now()), "conversation_id": conv_id,
             "subject": recs[0].get("subject") or "", "messages": msgs,
             "message_count": len(msgs), "latest": recs[-1].get("received") or "",
             "last_inbound": (last_in or {}).get("received", ""),
             "last_from_user": own_msgs[-1]["received"] if own_msgs else "",
             "user_replied_after_latest": bool(own_msgs and last_in
                                               and own_msgs[-1]["received"] > last_in["received"]),
             "people": people[:12], "written_by": "remote model"}
    # The thread ends on the user's own question: the next move is theirs,
    # and the row waits (ledger.waiting_on_reply) until they answer.
    last_own = own_msgs[-1] if own_msgs else None
    if last_own and last_own.get("awaits_reply") and (
            not last_in or last_own["received"] > last_in["received"]):
        brief["waiting_on_reply"] = {"since": last_own["received"],
                                     "asked": last_own.get("asked") or ""}
    links = []
    for m in msgs:
        for u in m.get("links") or []:
            if u not in links:
                links.append(u)
    if links:
        brief["links"] = links[:LINKS_CAP]
    if latest_in:
        r, res = latest_in
        brief.update({"ask": res.get("ask") or "", "state": res.get("state"),
                      "needs_reply": bool(res.get("needs_reply")),
                      "deadline": res.get("deadline") or "",
                      "offered_times": list(res.get("offered_times") or []),
                      "scheduling": res.get("scheduling") or "none",
                      "domain": res.get("domain") or ""})
    if kept:
        brief["kept_from_claude"] = kept
    return brief


def _brief_stale(prev: dict | None, brief: dict) -> bool:
    if not prev:
        return True
    return (prev.get("latest") != brief.get("latest")
            or prev.get("message_count") != brief.get("message_count"))


def sync_briefs(*, graph=None, chat=None, cap: int = SEAL_INLINE_CAP, now: datetime | None = None,
                rows: list[dict] | None = None, write=None,
                own_sent: dict | None = None) -> dict:
    """Write a thread brief onto every open ledger row that has a
    conversation id. The user's own messages in those threads are
    extracted here (at most `cap` threads fetched per call), since the tick
    skips them. `own_sent` is {conversation id: the user's latest send}
    (the tick's state): a thread he wrote in after its brief is fetched
    again even when nothing came in. Owner process; a no-op unless mail is
    enforced."""
    cfg = remote_llm.config()
    if not remote_llm.enforcing("mail", cfg):
        return {"skipped": "mail switch is not enforce or sealed"}
    import ledger as ledger_mod
    rows = rows if rows is not None else ledger_mod.get_open_entries()
    write = write or ledger_mod.set_metadata
    user = _user_addresses()
    index = load_index()
    if own_sent is None:
        own_sent = load_state().get("own_sent") or {}
    out = {"rows": 0, "written": 0, "fetched": 0, "own_extracted": 0, "errors": 0}
    for row in rows:
        conv = (row.get("conversation_id") or "").strip()
        if not conv:
            continue
        out["rows"] += 1
        prev = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else None
        recs = index["by_conv"].get(conv) or []
        latest = max((r.get("received") or "" for r in recs), default="")
        # The thread is fetched once per state of the row: when no brief
        # exists yet, or when mail arrived since the last one. That is when
        # the user's own replies may be missing from the store.
        # So is a thread the user wrote in after the brief: his reply is
        # the newest message, and no inbound mail will come to show it.
        own_checked = bool(prev and prev.get("own_checked") and prev.get("latest") == latest
                           and str(own_sent.get(conv) or "") <= str(prev.get("updated") or ""))
        if not own_checked and out["fetched"] < cap:
            try:
                graph = graph or _default_graph()
                msgs, _trunc = graph.get_conversation_messages(conv, top=50)
                out["fetched"] += 1
                own_new = [m for m in msgs if not m.get("isDraft")
                           and _addr(m.get("from")) in user
                           and (index["by_key"].get(message_key(m)) or {}).get("status") != "ok"]
                if own_new:
                    _inline_records(own_new, graph=graph, cfg=cfg, chat=chat, user=user)
                    out["own_extracted"] += len(own_new)
                    index = load_index()
                own_checked = True
            except Exception as e:
                out["errors"] += 1
                print(f"mail_privacy.sync_briefs: {conv[-12:]}: {str(e)[:120]}", file=sys.stderr)
        brief = thread_brief(conv, index, user=user, now=now)
        if brief is None:
            continue
        brief["own_checked"] = own_checked
        if _brief_stale(prev, brief) or own_checked != bool((prev or {}).get("own_checked")):
            try:
                write(row["id"], thread_brief=brief)
                out["written"] += 1
            except Exception as e:
                out["errors"] += 1
                print(f"mail_privacy.sync_briefs: write {row.get('id')}: {str(e)[:120]}", file=sys.stderr)
    return out


# ── the calendar shim ─────────────────────────────────────────────────

PERSONAL_CATEGORIES = {"personal", "family", "private", "medical", "health"}


def shim_event(ev: dict) -> dict:
    """One Graph event reduced to what scheduling and triage need: times,
    a title (\"personal\" for a personal or private event), how it shows,
    attendee count, online flag, EA hold flag and its id."""
    cats = [str(c) for c in ev.get("categories") or []]
    personal = (any(c.lower() in PERSONAL_CATEGORIES for c in cats)
                or str(ev.get("sensitivity") or "").lower() in ("private", "personal", "confidential"))

    def when(v):
        if isinstance(v, dict):
            return v.get("dateTime", "")[:19]
        return str(v or "")[:19]

    return {
        "id": ev.get("id", ""),
        "start": when(ev.get("start")),
        "end": when(ev.get("end")),
        "all_day": bool(ev.get("isAllDay")),
        "title": "personal" if personal else (ev.get("subject") or "")[:120],
        "show_as": ev.get("showAs", ""),
        "attendees": len(ev.get("attendees") or []),
        "online": bool(ev.get("onlineMeeting") or ev.get("isOnlineMeeting")),
        "ea_hold": "EA Hold" in cats,
        "recurring": bool(ev.get("seriesMasterId") or ev.get("recurrence")),
    }


# ── the reduced briefing ──────────────────────────────────────────────

PREVIEW_KEYS = ("body_preview", "bodyPreview", "preview", "snippet")
EVENT_SECTIONS = ("todays_events", "tomorrows_events")


def _stub(rec: dict) -> dict:
    res = rec.get("result") or {}
    if res.get("sensitivity") == "confidential":
        return {"confidential": res.get("confidential_kind", "other"),
                "needs_reply": res.get("needs_reply"),
                "deadline": res.get("deadline", "")}
    return {k: res.get(k) for k in ("ask", "needs_reply", "deadline", "scheduling",
                                     "people", "tone", "summary")}


def reduce_briefing(briefing: dict, index: dict) -> tuple[dict, dict]:
    """(reduced copy, stats): the briefing as an enforce mode would hand it
    to Claude. Every mail preview becomes the extracted record (a stub for
    a confidential thread). A thread kept from Anthropic keeps only who
    sent it and whether it needs a reply by a date; its subject goes too.
    Mail never extracted keeps its preview. Events go through the shim."""
    stats = {"previews": 0, "replaced": 0, "confidential": 0, "kept": 0, "unknown": 0,
             "events": 0}

    def find(item):
        mid = item.get("message_id") or item.get("id") or ""
        rec = index["by_id"].get(mid)
        if rec:
            return rec
        conv = item.get("conversation_id") or item.get("conversationId") or ""
        recs = [r for r in index["by_conv"].get(conv) or [] if r.get("status") == "ok"]
        return recs[-1] if recs else (index["by_conv"].get(conv) or [None])[-1]

    def walk(obj):
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if not isinstance(obj, dict):
            return obj
        out = {k: walk(v) for k, v in obj.items()}
        keys = [k for k in PREVIEW_KEYS if isinstance(obj.get(k), str)]
        if keys:
            stats["previews"] += 1
            rec = find(obj)
            if rec is None:
                stats["unknown"] += 1
            elif _kept(rec):
                for k in keys:
                    out.pop(k, None)
                if "subject" in out:
                    out["subject"] = "(kept from Claude)"
                res = rec.get("result") or {}
                out["extracted"] = {"kept_from_anthropic": True,
                                    "needs_reply": res.get("needs_reply"),
                                    "deadline": res.get("deadline", "")}
                stats["kept"] += 1
            elif rec.get("status") == "ok":
                for k in keys:
                    out.pop(k, None)
                out["extracted"] = _stub(rec)
                stats["replaced"] += 1
                if (rec.get("result") or {}).get("sensitivity") == "confidential":
                    stats["confidential"] += 1
            else:
                stats["unknown"] += 1
        return out

    reduced = {}
    for k, v in briefing.items():
        if k in EVENT_SECTIONS and isinstance(v, list):
            reduced[k] = [shim_event(e) if isinstance(e, dict) else e for e in v]
            stats["events"] += len(v)
        else:
            reduced[k] = walk(v)
    return reduced, stats


def _size(obj) -> int:
    return len(json.dumps(obj, default=str))


SHADOW_DIR = EA_DIR / "state" / "remote-llm-shadow"
REDUCED_BRIEFING = SHADOW_DIR / "briefing-reduced.json"
REAL_BRIEFING = EA_DIR / "state" / "briefing-latest.json"


def _write_reduced(payload: str) -> None:
    """The reduced copy holds less than the real briefing beside it, so it
    goes in the resident's space, where the sample runs read it."""
    import resident_io
    resident_io.make_dir(REDUCED_BRIEFING.parent)
    resident_io.update_text(REDUCED_BRIEFING, lambda _old: payload)


def shadow_briefing() -> dict:
    cfg = remote_llm.config()
    if remote_llm.mode("mail", cfg) != "shadow":
        return {"skipped": "mail switch is off"}
    import resident_io
    text = resident_io.read_text(REAL_BRIEFING)
    if not text:
        return {"skipped": "no briefing"}
    briefing = json.loads(text)
    done = remote_llm.read(BRIEFINGS)
    if done and done[-1].get("briefing_generated") == briefing.get("generated_at", ""):
        return {"skipped": "this briefing is already reduced"}
    reduced, stats = reduce_briefing(briefing, load_index())
    sections = {}
    for k in briefing:
        a, b = _size(briefing[k]), _size(reduced.get(k))
        if a > 2000 or a != b:
            sections[k] = [a, b]
    _write_reduced(json.dumps(reduced, indent=1, default=str))
    rec = {"ts": _iso(_now()), "briefing_generated": briefing.get("generated_at", ""),
           "real_bytes": _size(briefing), "reduced_bytes": _size(reduced),
           "sections": sections, **stats}
    remote_llm.append(BRIEFINGS, rec)
    return rec


# ── the Claude sample runs ────────────────────────────────────────────

SAMPLE_MODEL = "claude-opus-5-5"
JUDGE_MODEL = "sonnet"
SAMPLE_TIMEOUT_S = 900

SAMPLE_PROMPT = """SHADOW SAMPLE RUN. Nothing you write is posted and you must not act.
You are the executive assistant writing the user's morning briefing. Follow
the skill below. Read the briefing JSON at {path} with the Read tool; you
have no other tool. Write the briefing text you would post, then stop.

=== SKILL ===
{skill}
=== END SKILL ==="""

JUDGE_PROMPT = """Two morning briefings were written from the same day's data. A saw the full data;
B saw a reduced copy in which email previews were replaced by extracted fields. Compare them.
Answer with JSON only: {{"missing_in_b": [items A surfaces that B leaves out],
"extra_in_b": [items B surfaces that A does not], "wrong_in_b": [facts B states differently
from A], "verdict": "same" | "minor" | "major"}}. Each item is one short line.

=== A ===
{a}
=== B ===
{b}"""


def _claude_bin() -> str:
    for p in (EA_BOT_HOME / ".local" / "bin" / "claude", Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return "claude"


def _run_claude(prompt: str, model: str, tools: str, *,
                timeout: float = SAMPLE_TIMEOUT_S) -> dict:
    claude_bin = _claude_bin()
    args = ["-p", prompt, "--model", model, "--output-format", "json",
            "--strict-mcp-config", "--no-session-persistence"]
    if tools:
        args += ["--allowedTools", tools,
                 "--disallowedTools", "Bash,Write,Edit,WebFetch,WebSearch,Agent,NotebookEdit"]
    else:
        args += ["--tools", ""]
    # No MCP server and no claude.ai connector; claude_cli.apply adds the
    # lane permission mode, so a tool outside `tools` is refused.
    cmd = (["sudo", "-n", "-u", "ea-bot", "env", f"HOME={EA_BOT_HOME}", f"EA_DIR={EA_DIR}",
            f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}"]
           + [f"{k}={v}" for k, v in claude_cli.no_connector_env(args).items()] + [claude_bin]
           + claude_cli.apply(args, claude_bin=claude_bin, as_ea_bot=True))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=str(EA_BOT_HOME))
    out = p.stdout or ""
    try:
        data = json.loads(out[out.find("{"):out.rfind("}") + 1])
    except ValueError:
        data = {}
    return {"text": str(data.get("result") or ""), "cost_usd": data.get("total_cost_usd"),
            "rc": p.returncode, "stderr": (p.stderr or "")[-300:]}


def claude_sample() -> dict:
    cfg = remote_llm.config()
    if remote_llm.mode("mail", cfg) != "shadow":
        return {"skipped": "mail switch is off"}
    day = datetime.now().strftime("%Y-%m-%d")
    if any(s.get("day") == day for s in remote_llm.read(SAMPLES)):
        return {"skipped": "today's sample is done"}
    briefs = remote_llm.read(BRIEFINGS)
    if not briefs or not str(briefs[-1].get("ts", "")).startswith(_iso(_now())[:10]):
        return {"skipped": "no reduced briefing yet today"}
    # Both runs must read the same day's data: the real briefing must be
    # the one the reduced copy was built from.
    import resident_io
    try:
        real = json.loads(resident_io.read_text(REAL_BRIEFING) or "{}")
    except ValueError:
        real = {}
    if real.get("generated_at", "") != briefs[-1].get("briefing_generated"):
        return {"skipped": "the real briefing changed since the reduced copy was built"}
    skill = (REPO / "skills" / "morning-briefing.md").read_text()
    a = _run_claude(SAMPLE_PROMPT.format(path=REAL_BRIEFING, skill=skill), SAMPLE_MODEL, "Read")
    b = _run_claude(SAMPLE_PROMPT.format(path=REDUCED_BRIEFING, skill=skill), SAMPLE_MODEL, "Read")
    verdict = {}
    if a["text"] and b["text"]:
        j = _run_claude(JUDGE_PROMPT.format(a=a["text"], b=b["text"]), JUDGE_MODEL, "")
        t = j["text"]
        try:
            verdict = json.loads(t[t.find("{"):t.rfind("}") + 1])
        except ValueError:
            verdict = {"verdict": "unparsed", "raw": t[:500]}
        verdict["cost_usd"] = j.get("cost_usd")
    sdir = remote_llm.store_dir() / "samples"
    sdir.mkdir(mode=0o700, exist_ok=True)
    for name, body in ((f"{day}-full.md", a["text"]), (f"{day}-reduced.md", b["text"]),
                       (f"{day}-judge.json", json.dumps(verdict, indent=1))):
        _owner_trust.write_owner_file(sdir / name, body.encode(), mode=0o600)
    rec = {"ts": _iso(_now()), "day": day,
           "full_ok": bool(a["text"]), "reduced_ok": bool(b["text"]),
           "verdict": verdict.get("verdict"),
           "missing": len(verdict.get("missing_in_b") or []),
           "extra": len(verdict.get("extra_in_b") or []),
           "wrong": len(verdict.get("wrong_in_b") or []),
           "cost_usd": [a.get("cost_usd"), b.get("cost_usd"), verdict.get("cost_usd")]}
    remote_llm.append(SAMPLES, rec)
    return rec


# ── measurement ───────────────────────────────────────────────────────

def _sent_since(graph, since: datetime) -> list[dict]:
    return graph.graph_get_all("/me/mailFolders/sentitems/messages", {
        "$select": "id,conversationId,sentDateTime",
        "$filter": f"sentDateTime ge {_iso(since)}",
        "$orderby": "sentDateTime desc",
        "$top": 200,
    })


def _ledger_rows(since: datetime) -> list[dict]:
    try:
        import ledger
        con = ledger.guarded_connect(ledger.DB_PATH, read_only=True)
    except Exception as e:
        print(f"mail_privacy: ledger unreadable: {e}", file=sys.stderr)
        return []
    try:
        cur = con.execute(
            "SELECT id, type, deadline, conversation_id, created_at FROM ledger "
            "WHERE created_at >= ? AND (source IS NULL OR source = '') "
            "AND conversation_id IS NOT NULL AND conversation_id != ''",
            (since.strftime("%Y-%m-%d %H:%M:%S"),))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def _tagger_states() -> dict:
    """message id -> (state tag, domain tag) from the Haiku tagger's log."""
    out = {}
    try:
        path = _owner_trust.private_dir() / "mail-tagger.jsonl"
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("event") != "tagged" or not r.get("id"):
                    continue
                cats = r.get("cats") or []
                st = next((c for c in cats if c in STATES), "")
                dom = next((c for c in cats if c in DOMAINS), "")
                out[r["id"]] = (st, dom)
    except OSError:
        pass
    return out


def measure(*, graph=None, now: datetime | None = None, with_mail: bool = True) -> dict:
    """Every number the reports use, from the store plus three reads:
    sent mail (did the user reply), the ledger and the tagger log."""
    now = now or _now()
    cfg = remote_llm.config()
    index = load_index()
    recs = list(index["by_key"].values())
    fz = _frozen_setup()
    if fz and fz.get("frozen_at"):
        # After a freeze the numbers cover the test window only: mail that
        # arrived once the tuned setup was live.
        recs = [r for r in recs if str(r.get("received") or "") >= fz["frozen_at"]]
    ok = [r for r in recs if r.get("status") == "ok"]
    out: dict = {"messages": len(recs),
                 "ok": len(ok),
                 "errors": sum(1 for r in recs if r.get("status") == "error"),
                 "not_yet_redone": sum(1 for r in recs if r.get("status") == "withheld")}
    reasons: dict[str, int] = {}
    for r in recs:
        if _kept(r):
            why = r.get("keep_from_anthropic") or r.get("reason", "")
            key = "admissions" if why == "admissions" else "list"
            reasons[key] = reasons.get(key, 0) + 1
    out["kept"] = sum(reasons.values())
    out["kept_by_reason"] = reasons
    kinds: dict[str, int] = {}
    for r in ok:
        res = r["result"]
        if res.get("sensitivity") == "confidential":
            kinds[res.get("confidential_kind", "other")] = kinds.get(res.get("confidential_kind", "other"), 0) + 1
    out["confidential"] = sum(kinds.values())
    out["confidential_kinds"] = kinds
    out["tone_flags"] = sum(1 for r in ok if r["result"].get("tone"))

    calls = [c for c in remote_llm.read("calls.jsonl") if c.get("purpose") == "mail-extract"]
    lat = [c["latency_ms"] / 1000 for c in calls if c.get("ok") and c.get("latency_ms") is not None]
    out["calls"] = len(calls)
    out["call_errors"] = sum(1 for c in calls if not c.get("ok"))
    out["latency_p50_s"] = _quantile(lat, 0.5)
    out["latency_p95_s"] = _quantile(lat, 0.95)
    out["cold_starts"] = sum(1 for c in calls if (c.get("latency_ms") or 0) >= COLD_MS)
    out["chars_sent"] = sum(c.get("prompt_chars") or 0 for c in calls)

    if ok and with_mail:
        earliest = min((_parse(r.get("received")) or now) for r in ok)
        # Did the user reply within three days of each message?
        try:
            sent = _sent_since(graph or _default_graph(), earliest)
        except Exception as e:
            sent = None
            out["reply_check_error"] = str(e)[:200]
        if sent is not None:
            by_conv: dict[str, list[datetime]] = {}
            for s in sent:
                t = _parse(s.get("sentDateTime"))
                if t and s.get("conversationId"):
                    by_conv.setdefault(s["conversationId"], []).append(t)
            replied = flagged = both = 0
            for r in ok:
                got = _parse(r.get("received"))
                if not got or now - got < REPLY_WINDOW:
                    continue
                did = any(got < t <= got + REPLY_WINDOW
                          for t in by_conv.get(r.get("conversation_id"), []))
                nr = bool(r["result"].get("needs_reply"))
                replied += did
                flagged += nr
                both += did and nr
            out["replied"] = replied
            out["needs_reply_recall"] = _pct(both, replied)
            out["needs_reply_precision"] = _pct(both, flagged)
        # The ledger rows created from those threads.
        rows = _ledger_rows(earliest)
        by_conv_ok: dict[str, list[dict]] = {}
        for r in ok:
            by_conv_ok.setdefault(r.get("conversation_id"), []).append(r["result"])
        matched = acted = dl_rows = dl_hit = dl_same = 0
        for row in rows:
            res = by_conv_ok.get(row["conversation_id"])
            if not res:
                continue
            matched += 1
            if any(x.get("needs_reply") or x.get("state") in ACTION_STATES or x.get("ask") for x in res):
                acted += 1
            if row.get("deadline"):
                dl_rows += 1
                dls = [x.get("deadline") for x in res if x.get("deadline")]
                if dls:
                    dl_hit += 1
                    if str(row["deadline"])[:10] in dls:
                        dl_same += 1
        out["ledger_rows"] = matched
        out["ledger_recall"] = _pct(acted, matched)
        out["deadline_rows"] = dl_rows
        out["deadline_recall"] = _pct(dl_hit, dl_rows)
        out["deadline_same_date"] = _pct(dl_same, dl_rows)
    tags = _tagger_states()
    pairs = [(tags[r["id"]], r["result"]) for r in ok if r.get("id") in tags]
    out["tagger_pairs"] = len(pairs)
    out["state_agreement"] = _pct(sum(1 for (st, _), res in pairs if st and st == res["state"]),
                                  sum(1 for (st, _), _res in pairs if st))
    out["domain_agreement"] = _pct(sum(1 for (_, d), res in pairs if d and d == res["domain"]),
                                   sum(1 for (_, d), _res in pairs if d))

    rel = [r for r in remote_llm.read(RELEASES) if not r.get("shadow")]
    out["chat_reads"] = len(rel)
    out["chat_reads_flagged"] = sum(1 for r in rel if r.get("flagged_threads"))
    out["chat_flagged_threads"] = len({t for r in rel for t in r.get("flagged_threads") or []})

    briefs = remote_llm.read(BRIEFINGS)
    if briefs:
        b = briefs[-1]
        out["briefing_real_bytes"] = b.get("real_bytes")
        out["briefing_reduced_bytes"] = b.get("reduced_bytes")
        out["briefing_previews_replaced"] = _pct(b.get("replaced", 0), b.get("previews", 0))
        ev = (b.get("sections") or {})
        out["calendar_bytes"] = [sum(ev.get(k, [0, 0])[0] for k in EVENT_SECTIONS),
                                 sum(ev.get(k, [0, 0])[1] for k in EVENT_SECTIONS)]
    samples = remote_llm.read(SAMPLES)
    out["samples"] = [{"day": s.get("day"), "verdict": s.get("verdict"),
                       "missing": s.get("missing"), "wrong": s.get("wrong")} for s in samples]
    out["shadow_until"] = cfg.get("shadow_until")
    out["setup"] = (fz or {}).get("candidate", "live prompt")
    out["window_from"] = (fz or {}).get("frozen_at", "")
    return out


def _sched_report() -> dict:
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).with_name("sched_agent.py")),
                            "report", "--json"], capture_output=True, text=True, timeout=120)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
    except Exception:
        return {}


def _draft_report() -> dict:
    try:
        import draft_ab
        return draft_ab.report()
    except Exception:
        return {}


def _fmt(v, pct=False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.0f}%"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def _switch_text(cfg: dict) -> str:
    """The mail and scheduling switches with the date each window ends,
    for the report's header."""
    parts = []
    ghost = cfg.get("ghost") if isinstance(cfg.get("ghost"), dict) else {}
    for part in ("mail", "scheduling"):
        mode = remote_llm.mode(part, cfg)
        win = ghost.get(part) if isinstance(ghost.get(part), dict) else {}
        until = ""
        if mode in ("ghost", "shadow"):
            until = win.get("until") or cfg.get("shadow_until") or ""
        parts.append(f"{part} {mode}" + (f" to {until}" if until else ""))
    return ", ".join(parts)


def summary_text(m: dict, s: dict, day_n: int, cfg: dict | None = None) -> str:
    """The report's lines. A line whose measure is not running (the
    briefing reduction outside `shadow`, the drafting A/B once the remote
    writer is the default, the shadow scheduling counters while the ghost
    week reports the same turns) is left out."""
    cfg = cfg if cfg is not None else remote_llm.config()
    lines = [f"*Remote LLM shadow run, day {day_n}* ({_switch_text(cfg)}; numbers only)"]
    lines.append(
        f"Mail: {m['messages']} messages. {m['ok']} extracted, {m['errors']} errors. "
        f"Warm p95 {_fmt(m.get('latency_p95_s'))} s. {m.get('cold_starts', 0)} cold starts.")
    kr = m.get("kept_by_reason") or {}
    lines.append(
        f"Kept from Claude in an enforce mode: {m.get('kept', 0)} "
        f"(list {kr.get('list', 0)}, admissions {kr.get('admissions', 0)}).")
    lines.append(
        f"Confidential: {m['confidential']}. Tone flags: {m['tone_flags']}. "
        f"Chat reads of a kept or confidential thread: {m['chat_reads_flagged']} of {m['chat_reads']}.")
    lines.append(
        f"Needs-reply recall {_fmt(m.get('needs_reply_recall'), True)} "
        f"(replied n={m.get('replied', 'n/a')}). Ledger recall {_fmt(m.get('ledger_recall'), True)}. "
        f"Deadline recall {_fmt(m.get('deadline_recall'), True)}. "
        f"Tag agreement {_fmt(m.get('state_agreement'), True)}.")
    # The briefing reduction runs only under `shadow` (shadow_briefing);
    # its numbers are stale under any other value.
    if m.get("briefing_real_bytes") and remote_llm.mode("mail", cfg) == "shadow":
        r, d = m["briefing_real_bytes"], m["briefing_reduced_bytes"]
        lines.append(f"Briefing: {r // 1000} KB to {d // 1000} KB. "
                     f"Previews replaced {_fmt(m.get('briefing_previews_replaced'), True)}.")
    if m.get("samples") and remote_llm.mode("mail", cfg) == "shadow":
        v = ", ".join(f"{x['day'][5:]} {x['verdict']}" for x in m["samples"][-4:])
        lines.append(f"Briefing samples: {v}.")
    d = _draft_report()
    w = d.get("remote_writer") or {}
    if w.get("drafts"):
        lines.append(f"Remote writer since {w['since'][:10]}: {w['drafts']} drafts, "
                     f"{w.get('fell_back_to_claude', 0)} fell back to Claude; {w.get('sent', 0)} sent, "
                     f"{w.get('unchanged', 0)} unchanged, mean edit {_fmt(w.get('edit_ratio_mean'))}, "
                     f"{w.get('cancelled', 0)} cancelled.")
    # The drafting A/B draws only under `drafting = ab`.
    if d.get("drafts") and cfg.get("drafting") == "ab":
        def arm(name, label):
            a = d.get(name) or {}
            return (f"{label} {a.get('sent', 0)} sent, {a.get('unchanged', 0)} unchanged, "
                    f"mean edit {_fmt(a.get('edit_ratio_mean'))}, {a.get('cancelled', 0)} cancelled")
        lo, hi = d.get("edit_ratio_diff_ci") or (None, None)
        lines.append(f"Drafting A/B ({d['drafts']} drafts, {d.get('fallbacks', 0)} fell back): "
                     f"{arm('claude', 'Claude')}; {arm('qwen', 'remote')}. "
                     f"Remote minus Claude edit: {_fmt(lo)} to {_fmt(hi)}.")
    # Under `shadow` or `enforce` the ghost week's scheduling line counts
    # the same turns over its window, so this one is left out.
    if s and remote_llm.mode("scheduling", cfg) not in ("shadow", "enforce"):
        lines.append(
            f"Scheduling: {s.get('turns', 0)} turns. Invented times {s.get('invented_times', 'n/a')}. "
            f"Errors {s.get('errors', 'n/a')}. "
            f"p95 {_fmt((s.get('shadow_latency_s') or {}).get('p95'))} s "
            f"(Claude {_fmt((s.get('claude_latency_s') or {}).get('p95'))} s).")
    return "\n".join(lines)


def _day_numbers(cfg: dict) -> tuple[int, int]:
    first = None
    for r in remote_llm.read("calls.jsonl")[:1]:
        first = _parse(r.get("ts"))
    start = (first or _now()).astimezone().date()
    try:
        until = date.fromisoformat(cfg.get("shadow_until") or "")
    except ValueError:
        until = start
    today = datetime.now().astimezone().date()
    return (today - start).days + 1, (until - start).days + 1


def report(post: bool = False) -> dict:
    cfg = remote_llm.config()
    m = measure()
    s = _sched_report()
    day_n, _days = _day_numbers(cfg)
    text = summary_text(m, s, day_n, cfg=cfg)
    try:
        import ghost_week
        ghost = ghost_week.daily(cfg=cfg) if post else {"lines": ghost_week.summary_lines(cfg=cfg)}
        if ghost["lines"]:
            text += "\n" + "\n".join(ghost["lines"])
    except Exception as e:
        text += f"\nGhost week: report failed ({str(e)[:120]})."
    try:
        import model_arms
        ab_lines = model_arms.summary_lines(cfg)
        if ab_lines:
            text += "\n" + "\n".join(ab_lines)
    except Exception as e:
        text += f"\nModel A/B: report failed ({str(e)[:120]})."
    try:
        # The Codex ghost arm (codex_ghost.py): its lines while a window is
        # set, so the day's verdict per kind reaches the same report.
        import codex_ghost
        if codex_ghost.window():
            text += "\n" + codex_ghost.report()
    except Exception as e:
        text += f"\nCodex ghost: report failed ({str(e)[:120]})."
    try:
        import model_tiers
        tier_lines = model_tiers.summary_lines()
        if tier_lines:
            text += "\n" + "\n".join(tier_lines)
    except Exception as e:
        text += f"\nTiers: report failed ({str(e)[:120]})."
    try:
        # The closure detectors against the owner's labels (closure_eval.py):
        # labels collected, gold, the rules', model's and ledger's F1, and
        # the local and the hosted model's F1 and agreement.
        import closure_eval
        closure_lines = closure_eval.summary_lines()
        if closure_lines:
            text += "\n" + "\n".join(closure_lines)
    except Exception as e:
        text += f"\nClosure labels: report failed ({str(e)[:120]})."
    if post:
        on = remote_llm.reading("mail", cfg) or remote_llm.reading("scheduling", cfg)
        try:
            until = date.fromisoformat(cfg.get("shadow_until") or "")
        except ValueError:
            until = None
        today = datetime.now().astimezone().date()
        last_day = until is not None and today == until
        if on or last_day:
            try:
                import slack_notifier
                slack_notifier.post_to_slack(slack_notifier.health_channel_key(), text)
            except Exception as e:
                print(f"mail_privacy: post failed: {e}", file=sys.stderr)
        if last_day:
            path = final_report(m, s)
            try:
                import slack_notifier
                slack_notifier.post_to_slack(
                    slack_notifier.health_channel_key(),
                    "The shadow run ended today. The final report and the grading sheets "
                    f"are in {path.parent} (private).")
            except Exception:
                pass
    return {"text": text, "metrics": m, "scheduling": s}


def grading_sheet(n: int = 40, seed: int = 7) -> Path:
    """Every confidential message plus a random set of normal ones, up to
    `n`, each with a blank grade line."""
    index = load_index()
    ok = [r for r in index["by_key"].values() if r.get("status") == "ok"]
    conf = [r for r in ok if r["result"].get("sensitivity") == "confidential"]
    normal = [r for r in ok if r["result"].get("sensitivity") != "confidential"]
    random.Random(seed).shuffle(normal)
    picked = conf[:n] + normal[:max(0, n - len(conf))]
    lines = ["# Mail sensitivity grading", "",
             "Mark each line: correct / should be confidential / should be normal.", ""]
    for i, r in enumerate(picked, 1):
        res = r["result"]
        cls = res["sensitivity"] + ("" if res["sensitivity"] == "normal" else f" ({res['confidential_kind']})")
        lines += [f"## {i}. {r.get('subject') or '(no subject)'}",
                  f"From {r.get('from')}, {str(r.get('received'))[:10]}. Shadow class: **{cls}**.",
                  f"Summary: {res.get('summary')}", "", "Grade: ", ""]
    gdir = remote_llm.store_dir() / "grading"
    gdir.mkdir(mode=0o700, exist_ok=True)
    path = gdir / "mail-sensitivity.md"
    _owner_trust.write_owner_file(path, "\n".join(lines).encode(), mode=0o600)
    return path


def final_report(m: dict | None = None, s: dict | None = None) -> Path:
    m = m if m is not None else measure()
    s = s if s is not None else _sched_report()
    sheet = grading_sheet()
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name("sched_agent.py")),
                        "grading-sheet", "--n", "20"], capture_output=True, timeout=300)
    except Exception:
        pass
    lines = ["# Remote LLM shadow run: final report", "",
             f"Generated {datetime.now().astimezone():%Y-%m-%d %H:%M}.", "",
             "## Mail", "", "```json", json.dumps(m, indent=1, default=str), "```", "",
             "## Scheduling", "", "```json", json.dumps(s, indent=1, default=str), "```", "",
             "## Drafting (A/B and remote writer)", "", "```json", json.dumps(_draft_report(), indent=1, default=str),
             "```", "",
             "## Pass levels (docs/remote-llm.md)", "",
             f"- Needs-reply recall >= 95%: {_fmt(m.get('needs_reply_recall'), True)}",
             f"- Deadline recall >= 95%: {_fmt(m.get('deadline_recall'), True)}",
             f"- Errors < 2%: {_fmt(_pct(m.get('errors', 0), m.get('messages', 0)), True)}",
             f"- Warm p95 < 30 s: {_fmt(m.get('latency_p95_s'))} s",
             f"- Scheduling invented times = 0: {s.get('invented_times', 'n/a')}",
             "", "## Grading sheets", "", f"- {sheet}",
             f"- {remote_llm.store_dir() / 'grading'} (scheduling)", ""]
    path = remote_llm.store_dir() / "final-report.md"
    _owner_trust.write_owner_file(path, "\n".join(lines).encode(), mode=0o600)
    return path


def status() -> dict:
    cfg = remote_llm.config()
    st = load_state() if _owner_trust.is_owner_process() else {}
    return {"mail": remote_llm.mode("mail", cfg), "scheduling": remote_llm.mode("scheduling", cfg),
            "shadow_until": cfg.get("shadow_until"), "last_tick": st.get("last_tick"),
            "seen": len(st.get("seen", {})), "pending_retries": len(st.get("attempts", {})),
            "extractions": len(remote_llm.read(EXTRACTIONS)),
            "releases": len(remote_llm.read(RELEASES))}


def purge() -> dict:
    removed = remote_llm.purge()
    try:
        _write_reduced("{}")
    except Exception:
        pass
    return {"removed": removed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    p = sub.add_parser("backfill")
    p.add_argument("--days", type=int, default=7)
    sub.add_parser("shadow-briefing")
    sub.add_parser("claude-sample")
    p = sub.add_parser("report")
    p.add_argument("--post", action="store_true")
    p.add_argument("--json", action="store_true")
    sub.add_parser("final-report")
    sub.add_parser("status")
    p = sub.add_parser("redo-withheld")
    p.add_argument("--days", type=int, default=8)
    p = sub.add_parser("sync-briefs")
    p.add_argument("--cap", type=int, default=SEAL_INLINE_CAP)
    sub.add_parser("sync-kept-rows")
    p = sub.add_parser("brief")
    p.add_argument("--conversation-id", required=True)
    sub.add_parser("purge")
    a = ap.parse_args(argv)
    if not _owner_trust.is_owner_process():
        print("mail_privacy runs in the owner's processes only", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    if a.cmd == "tick":
        res = tick()
    elif a.cmd == "backfill":
        res = backfill(a.days)
    elif a.cmd == "shadow-briefing":
        res = shadow_briefing()
    elif a.cmd == "claude-sample":
        res = claude_sample()
    elif a.cmd == "report":
        res = report(post=a.post)
        if not a.json:
            print(res["text"])
            return 0
    elif a.cmd == "final-report":
        res = {"path": str(final_report())}
    elif a.cmd == "status":
        res = status()
    elif a.cmd == "redo-withheld":
        res = redo_withheld(a.days)
    elif a.cmd == "sync-briefs":
        res = sync_briefs(cap=a.cap)
    elif a.cmd == "sync-kept-rows":
        res = sync_kept_rows()
    elif a.cmd == "brief":
        res = thread_brief(a.conversation_id) or {"error": "no records for that thread"}
    else:
        res = purge()
    if isinstance(res, dict):
        res.setdefault("elapsed_s", round(time.monotonic() - t0, 1))
    print(json.dumps(res, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
