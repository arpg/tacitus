#!/usr/bin/env python3
"""The scheduling pipeline: code owns every number, the model picks and phrases.

One scheduling-channel request runs as fixed steps:

  1. locate   code finds the thread (search-mail by the names in the
              request) and takes a hit only when a name in the request is
              on that conversation's parties; the model is asked when
              several threads fit. Nothing is guessed: no name, no match
              or no answer is a `need`, a question for the user.
  2. intent   the model reads the request and the thread once and answers
              a JSON schema: who, how long, which dates, their time zone,
              what the sender prefers or ruled out, whether they named
              exact times, which mode (propose, validate, invite,
              reschedule, other) and, for an invite, the meeting title.
  3. solve    code builds the solver call from the intent (window dates,
              attendee time zone and stated days) and runs it. The solver
              labels every option with an id and each attendee's clock
              (sched_render). Exact times the other side named are
              checked first (validate-draft); when none is free, code
              proposes instead and the reply says so (a counter-offer).
              A window that yields nothing is retried without the CC'd
              attendees, then widened past a single day; the record says
              which. A request for "availability" lists up to eight
              options, two per day.
  invite      when the time is already agreed in the thread and the
              request asks for the invite: code builds the event (title,
              start, end, attendees, the institution's video link) and
              stages it for the Create button (sched_agent.live_write
              stage-event); nothing is created without the click. The
              time is the one the other side agreed to (named in their
              latest message, or the only one offered); several in play
              or a time already past is a `need`. "my calendar" with no
              one else stages a hold instead. A meeting already on the
              calendar at that time (an event there that names the other
              person or lists an invitee) is said, and nothing is staged.
  4. pick     the model sees the option lines with ids and answers a JSON
              schema: up to three ids, whether the sender's preferred day
              has one, an opening and a closing sentence with no clock
              time in them.
  5. check    code verifies the picks (valid ids, distinct days, inside
              the window, the preferred day used when it has an option,
              no clock in the prose) and repairs what it can.
  6. render   code assembles the reply from the option lines and the
              model's sentences (sched_render.render_body).

Shadow by default: the record goes to the store and nothing is written.
`live=True` creates the reply draft through sched_agent.live_write; the
bot's Send button stays the gate. Prompts are the defaults below or the
tuned set in <store>/tuning/sched-pipeline-prompts.json (sched_evolve
writes it); a caller can pass its own for a variant.

CLI:
  sched_pipeline.py run --text "Follow up with Sol with times that work that week" [--conversation-id <id>] [--live]
  sched_pipeline.py report [--days 14]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_llm as _rl  # noqa: E402
import sched_agent as _sa  # noqa: E402
import sched_render as _sr  # noqa: E402

TURNS = "pipeline-turns.jsonl"
PROMPTS_FILE = Path("tuning") / "sched-pipeline-prompts.json"
PURPOSE = "sched-pipeline"
DEFAULT_TZ = _sa.DEFAULT_TZ
THREAD_MESSAGES = 8
THREAD_CHARS = 1500
MAX_PICKS = 3
OPTIONS_TO_SHOW = 40
STOPWORDS = {"follow", "up", "with", "times", "that", "work", "week", "the", "and", "for",
             "reply", "to", "email", "draft", "find", "time", "meeting", "schedule", "set",
             "send", "invite", "him", "her", "them", "this", "next", "please", "can", "you",
             "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "january", "february", "march", "april", "may", "june", "july", "august",
             "september", "october", "november", "december", "zoom", "call", "chat", "coffee",
             "nov", "oct", "dec", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
             # a request's first word, capitalised by position, not a name
             "get", "handle", "respond", "check", "put", "give", "update", "make", "any", "also",
             "here", "two", "one", "just", "note", "says", "ask", "tell", "book", "hold", "move",
             "confirm", "cancel", "see", "look", "let", "add", "does", "did", "what", "when",
             "where", "which", "who", "how", "why", "yes", "okay",
             # imperatives and meeting words that are never a name
             "meet", "arrange", "propose", "offer", "suggest", "share", "forward", "reschedule",
             "pencil", "grab", "catch", "review", "help", "coordinate", "organise", "organize",
             "plan", "fix", "start", "open", "close", "create", "read", "search", "show", "list",
             "post", "run", "try", "keep", "take", "bring", "pull", "push", "drop", "write",
             "sort", "figure", "connect", "introduce", "intro", "loop", "circle", "touch", "reach",
             "lunch", "dinner", "breakfast", "sync", "setup", "slot", "block", "clear", "free",
             "busy", "available", "availability", "dean", "chair", "group", "team", "committee",
             "student", "students", "postdoc", "everyone", "someone", "anyone", "nobody",
             "the", "a", "an", "my", "our", "his", "their", "its", "me", "us", "it", "we", "i"}
# Verbs that are also given names ("Ping", "Mark", "Chase"): dropped only
# as the request's first word, where position capitalises them.
FIRST_WORD_ONLY = {"ping", "mark", "chase", "grant", "will", "sue", "bill", "pat", "art",
                   "don", "ray", "book", "rob", "nick", "pierce", "skip", "chip", "chuck"}

# ── schemas ───────────────────────────────────────────────────────────

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["propose", "validate", "invite", "reschedule", "other"]},
        "sender_name": {"type": "string"},
        "sender_email": {"type": "string"},
        "sender_timezone": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "preferred_days": {"type": "array", "items": {"type": "string"}},
        "excluded_days": {"type": "array", "items": {"type": "string"}},
        "offered_times": {"type": "array", "items": {"type": "string"}},
        "other_attendees": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"},
        "note_to_user": {"type": "string"},
    },
    "required": ["mode", "sender_name", "sender_email", "sender_timezone", "duration_minutes",
                 "window_start", "window_end", "preferred_days", "excluded_days",
                 "offered_times", "other_attendees", "subject", "note_to_user"],
    "additionalProperties": False,
}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "items": {"type": "string"}},
        "preferred_day_available": {"type": "boolean"},
        "opening": {"type": "string"},
        "closing": {"type": "string"},
        "note_to_user": {"type": "string"},
    },
    "required": ["picks", "preferred_day_available", "opening", "closing", "note_to_user"],
    "additionalProperties": False,
}

LOCATE_SCHEMA = {
    "type": "object",
    "properties": {"conversation_id": {"type": "string"}, "why": {"type": "string"}},
    "required": ["conversation_id", "why"],
    "additionalProperties": False,
}

# ── prompts (the defaults; sched_evolve tunes them) ───────────────────

INTENT_PROMPT = """You turn one scheduling request and the email thread it points at into a JSON scheduling intent for {user_name} ({user_email}), whose clock is {user_zone}. Today is {today}.

{weeks}

Fields:
- mode: "propose" when {user_first} must offer times; "validate" when the other person named exact clock times and {user_first} must say which work; "invite" when a time is already agreed (in the thread or in the request) and {user_first} asks to send the calendar invite, put it on his calendar or hold it; "reschedule" when an existing meeting moves; "other" when the request is not about finding a time.
- sender_name, sender_email: the OTHER person in the thread, the one {user_first} is writing to. Never {user_first}'s own address.
- sender_timezone: an IANA zone (America/New_York, Europe/London) from their affiliation, signature or city; "" when nothing in the thread says where they are. A .edu in Massachusetts is America/New_York; northeastern.edu, mit.edu, harvard.edu, bu.edu are Boston.
- duration_minutes: 30 unless the request or the thread says otherwise.
- window_start, window_end: the first and last date (YYYY-MM-DD) a meeting may fall on, from the request and the thread ("the first week of November" is that week's Monday to Friday; "next week" is the week after today's). Empty when the mode is "other".
- preferred_days: dates (YYYY-MM-DD) inside the window the other person said they prefer ("Friday would be best"). A general "any day works" adds nothing.
- excluded_days: dates inside the window either side ruled out.
- offered_times: the exact clock times the other person proposed, as ISO date-times on {user_first}'s clock ("2026-11-03T14:00:00"); [] when they only named days or preferences. For "invite", the one agreed time.
- other_attendees: addresses of anyone else who must be on the meeting, besides the sender and {user_first}. Someone only CC'd on the thread is not an attendee unless the request or the thread says they attend.
- subject: for "invite", the meeting title as the thread names it (a short noun phrase, no date); "" otherwise.
- note_to_user: one short sentence for {user_first} about anything odd (travel named in the thread, a question only he can answer); "" when nothing.

Answer with the JSON object only."""

PICK_PROMPT = """You choose meeting options for {user_first} to offer. Today is {today}. The thread is with {sender_name}{sender_where}. The request from {user_first}: "{request}"

Below are the free options the solver returned, each with an id. These are the only times that exist. {preference_line}

Rules:
- picks: up to {max_picks} ids, on different days, earliest first. Honour the other person's preferred day when it has an option; otherwise say so in the opening.
- An option marked [ranked lower: ...] goes against one of {user_first}'s standing preferences. On a day that also has an unmarked option, pick the unmarked one; take a marked option only when its day has no other.
- preferred_day_available: true only when a pick falls on a day they preferred.
- opening: one or two sentences in {user_first}'s voice that introduce the times (and say plainly when the preferred day does not work). No clock times, no dates: the lines carry them.
- closing: one sentence asking them to pick one, in {user_first}'s voice ("Let me know which works and I will send a calendar invite."). No clock times.
- note_to_user: one short sentence for {user_first} only; "" when nothing.
Write plainly, no em-dash, no closing clause about why it matters.

Answer with the JSON object only."""

# Lines a tuned intent prompt (sched_evolve) written before a field existed
# gets appended, so code-owned fields reach every prompt set.
INTENT_ADDENDA = (
    ("invite", '- mode "invite": a time is already agreed (in the thread or the request) and {user_first} asks to send the calendar invite, put it on his calendar or hold it; offered_times then holds that one time.'),
    ("subject", '- subject: for "invite", the meeting title as the thread names it (a short noun phrase, no date); "" otherwise.'),
    ("CC", "- other_attendees: someone only CC'd on the thread is not an attendee unless the request or the thread says they attend."),
)

LOCATE_PROMPT = """Which of these email conversations is the one this scheduling request is about? Request from {user_first}: "{request}"

Conversations (newest message first):
{rows}

Answer with the JSON object only: the conversation_id, or "" when none fits."""


# ── helpers ───────────────────────────────────────────────────────────

def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def _iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_prompts(store: Path | None = None) -> dict:
    """The tuned prompts when sched_evolve wrote them, else the defaults."""
    out = {"intent": INTENT_PROMPT, "pick": PICK_PROMPT, "locate": LOCATE_PROMPT, "name": "default"}
    try:
        path = (store or _rl.store_dir()) / PROMPTS_FILE
        d = json.loads(path.read_text())
        for k in ("intent", "pick", "locate"):
            if isinstance(d.get(k), str) and d[k].strip():
                out[k] = d[k]
        out["name"] = str(d.get("name") or "tuned")
    except Exception:
        pass
    out["intent"] = with_addenda(out["intent"])
    return out


def with_addenda(intent_prompt: str) -> str:
    """The intent prompt with every code-owned line it lacks appended
    before the closing instruction."""
    missing = [line for key, line in INTENT_ADDENDA if key not in intent_prompt]
    if not missing:
        return intent_prompt
    tail = "\n\nAnswer with the JSON object only."
    body = intent_prompt[:-len(tail)] if intent_prompt.endswith(tail) else intent_prompt
    return body + "\n" + "\n".join(missing) + tail


class ProfileError(RuntimeError):
    """The user profile could not be read or names no user: a reply
    written without it would greet "Hi," and sign nothing."""


def user_profile(reader=None) -> dict:
    """{name, first, email, zone} from the user profile the resident
    reads. Raises ProfileError when the file is unreadable or has no name
    or address; run() turns that into a `need`."""
    reader = reader or _sa._resident_text
    path = _sa.EA_DIR / "config" / "user-profile.json"
    try:
        text = reader(path)
        d = json.loads(text) if text else None
    except Exception as e:
        raise ProfileError(f"could not read {path}: {str(e)[:80]}") from e
    if not isinstance(d, dict):
        raise ProfileError(f"could not read {path}")
    name = str(d.get("name") or d.get("full_name") or "").strip()
    email = str(d.get("email") or "").strip()
    if not name or not email:
        raise ProfileError(f"{path} has no name or email")
    return {"name": name, "first": name.split()[0], "email": email,
            "zone": str(d.get("timezone") or DEFAULT_TZ)}


def weeks_table(today: date, n: int = 8) -> str:
    """'Week of Mon Oct 19: Oct 19-23' for the next n weeks, so a model
    can map 'the first week of November' to dates without arithmetic."""
    mon = today - timedelta(days=today.weekday())
    lines = ["Calendar (Monday to Friday of each week):"]
    for i in range(n):
        a = mon + timedelta(days=7 * i)
        b = a + timedelta(days=4)
        lines.append(f"  week of Mon {a.strftime('%b %-d')}: {a.isoformat()} to {b.isoformat()}"
                     + ("  (this week)" if i == 0 else "  (next week)" if i == 1 else ""))
    return "\n".join(lines)


def thread_text(messages: list, *, n: int = THREAD_MESSAGES, chars: int = THREAD_CHARS) -> str:
    rows = []
    for m in (messages or [])[-n:]:
        frm = m.get("from")
        if isinstance(frm, dict):
            frm = frm.get("address") or (frm.get("emailAddress") or {}).get("address") or frm.get("name")
        rows.append({"from": frm, "to": m.get("to"), "cc": m.get("cc"), "sent": m.get("sent"),
                     "own": bool(m.get("own")), "is_draft": bool(m.get("is_draft")),
                     "text": str(m.get("text") or m.get("body") or "")[:chars]})
    return json.dumps(rows, indent=1, default=str)


def _addr(v) -> str:
    if isinstance(v, dict):
        return str(v.get("address") or (v.get("emailAddress") or {}).get("address") or "").lower()
    return str(v or "").lower()


def reply_target(messages: list, user_email: str) -> dict:
    """The latest message not written by the user: the one a reply answers."""
    for m in reversed(messages or []):
        if m.get("is_draft"):
            continue
        if m.get("own") or (user_email and _addr(m.get("from")) == user_email.lower()):
            continue
        return {"message_id": str(m.get("id") or ""), "conversation_id": str(m.get("conversation_id") or ""),
                "from": _addr(m.get("from")), "ref": m.get("ref"), "conversation_ref": m.get("conversation_ref")}
    return {}


_AFTER_PREP = re.compile(r"\b(?:with|to|on|for|from|about|re|and|cc)\s+([a-z][a-z'’-]{2,})\b")
# A short capitalised name after a preposition ("with Bo", "to Al").
_SHORT_NAME = re.compile(r"\b(?:with|to|for|from|about|and|cc)\s+([A-Z][a-z]{1,2})\b")
_NOT_NAMES = STOPWORDS | {"back", "them", "that", "this", "those", "these", "there", "here", "what",
                          "when", "where", "which", "while", "paper", "deck", "draft", "note", "calendar",
                          "office", "campus", "home", "friday", "monday", "today", "tomorrow", "morning",
                          "afternoon", "evening", "week", "month", "year", "hour", "minutes", "zoom",
                          "teams", "webex", "both", "each", "every", "some", "none", "their", "your",
                          "mine", "ours", "thread", "reply", "times", "time", "options", "invite",
                          "meeting", "request", "him", "her", "you", "all", "any", "our", "out", "now",
                          "its", "one", "two", "six", "ten", "may", "but", "not", "are", "was", "has",
                          "had", "can", "got", "use", "way", "yet", "too", "via", "per", "off", "own",
                          "new", "old", "few", "end", "day", "pay", "buy", "bit", "lot", "job", "lab",
                          "pick", "know", "talk", "join", "come", "visit", "stop", "swing", "lock",
                          "work", "say", "go", "do", "and", "the", "him", "then", "than", "into",
                          "onto", "over", "under", "after", "before", "until", "about", "around",
                          "them", "being", "have", "been", "will", "would", "could", "should",
                          "myself", "himself", "herself", "whoever", "whatever", "another", "other"}
_SHORT_NOT_NAMES = {"me", "us", "it", "my", "an", "in", "on", "at", "by", "or", "so", "no", "up",
                    "dr", "mr", "ms", "ok", "am", "pm", "cc", "re", "ea", "eu", "the", "him", "her",
                    "you", "all", "any", "our", "out", "now", "its", "one", "two", "six", "ten",
                    "may", "not", "see", "get", "put", "set", "say", "ask", "let", "use", "try",
                    "run", "fix", "pay", "buy", "add", "end", "top", "day", "new", "old"}


def name_tokens(request: str) -> list[str]:
    """The words of a request that look like names: the capitalised ones
    (a verb that is also a name, "Ping", counts only after the first
    word), a two or three letter capitalised word after "with", "to" or
    "and" ("with Bo"), and a lowercase word after a preposition (a name
    typed in a hurry, "the ball on frazer", "with sam")."""
    text = request or ""
    out = []
    first = re.match(r"\s*([A-Z][a-zA-Z'’-]*)", text)
    first_word = first.group(1) if first else ""
    for m in re.finditer(r"\b[A-Z][a-zA-Z'’-]{2,}\b", text):
        w = m.group(0)
        if w.lower() in STOPWORDS or w in out:
            continue
        if w == first_word and m.start() == first.start(1) and w.lower() in FIRST_WORD_ONLY:
            continue
        out.append(w)
    for w in _SHORT_NAME.findall(text):
        if w.lower() not in _SHORT_NOT_NAMES and w.lower() not in STOPWORDS and w not in out:
            out.append(w)
    for w in _AFTER_PREP.findall(text):
        if w not in _NOT_NAMES and w.capitalize() not in out and w not in out:
            out.append(w)
    return out


def last_conversation_for(thread_ts: str, rows: list | None = None) -> str:
    """The conversation the last pipeline turn in this chat thread was
    about, so a follow-up that names no one ("send the invite too")
    lands on the same thread. "" when none."""
    if not thread_ts:
        return ""
    rows = rows if rows is not None else _rl.read(TURNS)
    for r in reversed(rows):
        if not r.get("conversation_id"):
            continue
        ts = r.get("thread_ts") or (r.get("shadow_of") or {}).get("thread_ts")
        if ts == thread_ts:
            return str(r["conversation_id"])
    return ""


# ── tools through the resident ────────────────────────────────────────

def read_tool(tool: str, args: dict, run_id: str, runner=None) -> tuple[dict | list | None, str]:
    """One read verb as the resident runs it (sched_agent's argv rules);
    (parsed JSON or None, error text)."""
    runner = runner or _sa.run_resident
    script, _verb, _spec = _sa.READ_TOOLS[tool]
    argv = _sa._argv_for(tool, args)
    rc, out, err = runner(_sa._resident_cmd(script, argv, run_id))
    parsed = _sa._parse_json_out(out)
    if rc != 0 and parsed is None:
        return None, (err or out or f"rc={rc}")[-400:]
    return parsed, ""


def _conversation_rows(rows: list) -> list[dict]:
    """search-mail rows folded to one per conversation, newest first."""
    by = {}
    for r in rows or []:
        cid = str(r.get("conversation_id") or r.get("conversationId") or "")
        if not cid:
            continue
        cur = by.get(cid)
        if cur is None or str(r.get("received") or r.get("sent") or "") > str(cur.get("received") or cur.get("sent") or ""):
            by[cid] = r
    return sorted(by.values(), key=lambda r: str(r.get("received") or r.get("sent") or ""), reverse=True)


def _parties(row: dict) -> str:
    """The addresses and display names on a search-mail row, lowercased,
    in one string."""
    parts = []
    for v in [row.get("from"), row.get("from_name"), row.get("sender")] + list(row.get("to") or []) + list(row.get("cc") or []):
        if isinstance(v, dict):
            parts.append(_addr(v))
            parts.append(str(v.get("name") or (v.get("emailAddress") or {}).get("name") or ""))
        elif v:
            parts.append(str(v))
    return " ".join(p for p in parts if p).lower()


def party_matches(row: dict, names: list[str]) -> bool:
    """True when a name from the request starts a word in the row's
    addresses or display names ("Bo" on bo.chen@..., not on robot@)."""
    hay = _parties(row)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n.lower()), hay) for n in names if n)


def _cid(row: dict) -> str:
    return str(row.get("conversation_id") or row.get("conversationId") or "")


def _subject_key(row: dict) -> str:
    return _RE_PREFIX.sub("", str(row.get("subject") or "")).strip().lower()


def locate_thread(request: str, *, run_id: str, runner=None, llm=None, user: dict,
                  prompts: dict) -> tuple[str, dict]:
    """The conversation id the request is about; ('', detail) when none,
    with detail["need"] the question for the user. Code searches by the
    names in the request and takes a conversation on its own only when a
    name is on its parties (one such conversation, or several that are
    one thread by subject); otherwise the model chooses among the hits.
    Nothing falls back to the newest hit: a failed locate step or a
    model that names no listed conversation is a need."""
    names = name_tokens(request)
    if not names:
        return "", {"why": "no name in the request",
                    "need": "Which email thread is this about? The request names no one."}
    rows = []
    for q in names[:3]:
        parsed, err = read_tool("search-mail", {"query": q, "top": 10}, run_id, runner)
        if isinstance(parsed, list):
            rows += parsed
    convs = _conversation_rows(rows)
    who = ", ".join(names[:3])
    if not convs:
        return "", {"why": f"search-mail found nothing for {names[:3]}",
                    "need": f"I found no email thread for {who}. Which thread is this about?"}
    matched = [r for r in convs if party_matches(r, names)]
    if len(matched) == 1 or (matched and len({_subject_key(r) for r in matched}) == 1):
        r = matched[0]
        return _cid(r), {"why": "one conversation with a named party", "subject": r.get("subject"),
                         "candidates": len(convs)}
    candidates = matched or convs
    llm = llm or _rl.chat_json
    listing = "\n".join(
        f"- conversation_id {_cid(r)}: subject {r.get('subject')!r}, "
        f"from {_addr(r.get('from'))}, {str(r.get('received') or r.get('sent') or '')[:10]}, "
        f"preview {str(r.get('body_preview') or r.get('preview') or r.get('text') or '')[:160]!r}"
        for r in candidates[:8])
    try:
        obj, _meta = llm([{"role": "user", "content": prompts["locate"].format(
            user_first=user["first"], request=request, rows=listing)}],
            purpose=PURPOSE, schema=LOCATE_SCHEMA, max_tokens=400)
    except Exception as e:
        return "", {"why": f"locate step failed ({str(e)[:80]})", "candidates": len(candidates),
                    "need": f"I found {len(candidates)} threads for {who} and could not tell which one this is about. Which one?"}
    cid = str((obj or {}).get("conversation_id") or "")
    if cid not in {_cid(r) for r in candidates}:
        return "", {"why": "the model named no listed conversation", "candidates": len(candidates),
                    "need": f"None of the {len(candidates)} threads I found for {who} clearly fits. Which thread is this about?"}
    return cid, {"why": str(obj.get("why") or "")[:200], "candidates": len(candidates)}


# ── the model steps ───────────────────────────────────────────────────

def extract_intent(request: str, messages: list, *, today: date, user: dict, prompts: dict,
                   llm=None) -> tuple[dict, dict]:
    llm = llm or _rl.chat_json
    system = prompts["intent"].format(
        user_name=user["name"], user_email=user["email"], user_first=user["first"],
        user_zone=user["zone"], today=today.strftime("%A %Y-%m-%d"), weeks=weeks_table(today))
    content = f"REQUEST from {user['first']}: {request}\n\nTHREAD (oldest first, `own` marks {user['first']}'s messages):\n{thread_text(messages)}"
    obj, meta = llm([{"role": "system", "content": system}, {"role": "user", "content": content}],
                    purpose=PURPOSE, schema=INTENT_SCHEMA, max_tokens=700)
    return normalize_intent(obj, messages, user), {"latency_ms": meta.get("latency_ms"), "model": meta.get("model")}


def _date(s) -> str:
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        return ""


def normalize_intent(obj: dict, messages: list, user: dict) -> dict:
    """The intent as code can use it: dates parsed, a bad zone dropped,
    the sender never the user, a duration in a sane range."""
    out = dict(obj or {})
    out["mode"] = out.get("mode") if out.get("mode") in ("propose", "validate", "invite", "reschedule", "other") else "other"
    out["sender_email"] = str(out.get("sender_email") or "").strip().lower()
    if not out["sender_email"] or out["sender_email"] == user["email"].lower():
        t = reply_target(messages, user["email"])
        out["sender_email"] = t.get("from") or out["sender_email"]
        out["sender_fixed"] = True
    tz = str(out.get("sender_timezone") or "").strip()
    try:
        ZoneInfo(tz) if tz else None
    except Exception:
        tz = ""
    # a colleague on the user's own domain is on the user's clock
    domain = user["email"].split("@")[-1].lower() if user.get("email") else ""
    if not tz and domain and out["sender_email"].endswith("@" + domain):
        tz = user.get("zone") or DEFAULT_TZ
    out["sender_timezone"] = tz
    try:
        d = int(out.get("duration_minutes") or 30)
    except (TypeError, ValueError):
        d = 30
    out["duration_minutes"] = d if 10 <= d <= 240 else 30
    out["window_start"], out["window_end"] = _date(out.get("window_start")), _date(out.get("window_end"))
    if out["window_start"] and out["window_end"] and out["window_end"] < out["window_start"]:
        out["window_start"], out["window_end"] = out["window_end"], out["window_start"]
    for k in ("preferred_days", "excluded_days"):
        out[k] = sorted({_date(x) for x in (out.get(k) or []) if _date(x)})
    out["offered_times"] = [str(x) for x in (out.get("offered_times") or []) if _sa._parse_iso(str(x)) is not None]
    out["other_attendees"] = sorted({str(x).strip().lower() for x in (out.get("other_attendees") or [])
                                     if "@" in str(x) and str(x).strip().lower() != user["email"].lower()})
    out["subject"] = str(out.get("subject") or "").strip()[:120]
    out["note_to_user"] = str(out.get("note_to_user") or "")[:300]
    return out


_HOLD_RE = re.compile(r"\b(?:my (?:own )?calendar|hold(?: it| the time)?|block (?:it|the time|my calendar)|"
                      r"put (?:it |this |the \w+ )?(?:on|in|into) my)\b", re.I)
_INVITE_RE = re.compile(r"\b(?:send|set up|setup|create|make|issue)\b[^.]{0,40}\binvite\b|\bcalendar invite\b|"
                        r"\binvite\b[^.]{0,20}\bwith (?:a |the )?(?:zoom|teams|meet|webex|online|video)\b", re.I)


def nudge_mode(intent: dict, request: str) -> tuple[dict, str]:
    """(the intent, why its mode changed or ""). Two shapes the request
    states in words code can read, which the model has missed in the
    shadow: an availability request is a proposal, and "send the invite"
    or "put it on my calendar" with a time in hand is an invite."""
    mode = intent.get("mode")
    if mode == "other" and wants_listing(request):
        return dict(intent, mode="propose"), "availability request"
    if mode in ("other", "propose", "validate") and intent.get("offered_times") and (
            _INVITE_RE.search(request or "") or _HOLD_RE.search(request or "")):
        return dict(intent, mode="invite"), "invite or hold asked for with a time in hand"
    return intent, ""


_WIDE_RE = re.compile(r"\b(?:full|all|complete|my|post|send|give)\b[^.]{0,30}\bavailab|\btimes? (?:i'?m|i am) (?:free|available)|\ball (?:my )?(?:free|open) (?:times|slots)", re.I)
WIDE_PICKS = 8
WIDEN_DAYS = 7


def wants_listing(request: str) -> bool:
    """True when the request asks for availability rather than a few
    options ("post times I'm available", "full availability")."""
    return bool(_WIDE_RE.search(request or ""))


def sender_only(intent: dict) -> dict:
    """The intent with the CC'd attendees dropped (the retry when no slot
    fits everyone)."""
    return dict(intent, other_attendees=[])


def widened(intent: dict, days: int = WIDEN_DAYS) -> dict:
    """The intent with its window extended `days` past its end (the
    retry when a single day has nothing)."""
    end = date.fromisoformat(intent["window_end"]) + timedelta(days=days)
    return dict(intent, window_end=end.isoformat())


def solver_args(intent: dict, *, today: date, user_email: str) -> tuple[str, dict]:
    """The solver call code builds from the intent: ('propose-meeting',
    args) or ('validate-draft', args). The attendee carries the zone and
    the days the sender ruled out (`excluded_days`, which the solver
    composes with the zone's working hours); the window is the intent's
    dates (default: the next ten weekdays)."""
    duration = int(intent.get("duration_minutes") or 30)
    if intent.get("mode") == "validate" and intent.get("offered_times"):
        return "validate-draft", {"times": list(intent["offered_times"]), "duration": duration}
    start = intent.get("window_start") or (today + timedelta(days=1)).isoformat()
    end = intent.get("window_end") or (date.fromisoformat(start) + timedelta(days=13)).isoformat()
    att = {"email": intent.get("sender_email") or "unknown@example.org",
           "name": intent.get("sender_name") or "", "type": "external"}
    if user_email and att["email"].endswith("@" + user_email.split("@")[-1]):
        att["type"] = "internal"
    if intent.get("sender_timezone"):
        att["timezone"] = intent["sender_timezone"]
    attendees = [att] + [{"email": a, "name": "", "type": "internal" if user_email and a.endswith("@" + user_email.split("@")[-1]) else "external"}
                         for a in intent.get("other_attendees") or []]
    excluded = sorted(d for d in set(intent.get("excluded_days") or []) if start <= d <= end)
    if excluded:
        att["excluded_days"] = excluded
    return "propose-meeting", {"duration": duration, "earliest_date": start, "latest_date": end,
                               "num_options": OPTIONS_TO_SHOW, "attendees": attendees}


# validate-draft issue types: a conflict makes a time unavailable; these
# are kept as warnings on an available time; the rest are information.
HARD_ISSUES = ("conflict",)
SOFT_ISSUES = ("preference_warning", "all_day_event", "attendee_tentative")


def fetch_error_need(out: dict) -> str:
    """The need a validate-draft answer carries when a calendar could not
    be read: nothing about availability can be said. '' when every
    calendar was read."""
    errors = out.get("calendar_fetch_errors") or []
    names = []
    for e in errors:
        name = e.get("calendar") or e.get("account") or e.get("name") or e.get("source") if isinstance(e, dict) else str(e)
        if name and str(name) not in names:
            names.append(str(name))
    if not names:
        for i in out.get("issues") or []:
            if isinstance(i, dict) and i.get("type") == "calendar_fetch_error":
                names.append(str(i.get("detail") or "a calendar")[:120])
                break
    if not names:
        return ""
    return (f"I could not read the calendar {', '.join(names)}, so I cannot say whether the offered"
            f" times are free. Check the calendar, or tell me the times are fine.")


def options_from_validate(out: dict, intent: dict, attendees: list, *, zone: str = "") -> list[dict]:
    """The sender's offered times that validate-draft found free, labelled
    like solver options. Only a hard conflict makes a time unavailable; a
    soft issue (a preference warning, an all-day event) stays on the
    option as `warnings`, for the pick prompt and the user's line."""
    duration = int(intent.get("duration_minutes") or 30)
    zone = zone or DEFAULT_TZ
    bad, warnings = set(), {}
    for i in out.get("issues") or []:
        if not isinstance(i, dict):
            continue
        t = str(i.get("proposed_time") or "")
        if i.get("type") in HARD_ISSUES:
            bad.add(t)
        elif i.get("type") in SOFT_ISSUES:
            warnings.setdefault(t, []).append(str(i.get("detail") or i.get("type") or "")[:160])
    opts = []
    for t in intent.get("offered_times") or []:
        if str(t) in bad:
            continue
        try:
            s = _sa._parse_iso(str(t), ZoneInfo(zone))
            if s is None:
                continue
        except Exception:
            continue
        opts.append({"start": s.isoformat(), "end": (s + timedelta(minutes=duration)).isoformat(), "notes": "",
                     "warnings": warnings.get(str(t), [])})
    _sr.label_options(opts, attendees, user_zone=zone)
    return opts


def option_warnings(o: dict) -> list[str]:
    """The soft warnings on an option (validate-draft's words)."""
    w = o.get("warnings")
    return [str(x) for x in w if str(x).strip()] if isinstance(w, list) else []


def option_prefs(o: dict) -> list[str]:
    """The user's token preferences (p1/p2 on the EA-Managed calendar)
    that penalised this solver option, as the solver labels them ("p2
    Prefer mornings"). Empty for an option nothing ranked lower."""
    ap = o.get("applied_preferences")
    if not isinstance(ap, dict):
        return []
    return [str(k) for k in ap if str(k).strip()]


def option_listing(options: list[dict]) -> str:
    """The options as the pick model sees them: 'o5: <line>', with
    '[ranked lower: p2 Prefer mornings]' on an option a preference
    penalised. The marks carry the solver's words, not the model's."""
    rows = []
    for o in options:
        if not o.get("id"):
            continue
        prefs = option_prefs(o)
        warns = option_warnings(o)
        rows.append(f"{o['id']}: {o['line']}" + (f"  [ranked lower: {'; '.join(prefs)}]" if prefs else "")
                    + (f"  [warning: {'; '.join(warns)}]" if warns else ""))
    return "\n".join(rows)


def pick_context(intent: dict, options: list[dict], *, counter: int = 0, wide: bool = False,
                 widened_from: str = "", dropped: list[str] | None = None) -> str:
    """The situation line the pick prompt carries: the preferred day,
    and what code found on the way (a counter-offer, a listing, a
    widened window, attendees dropped)."""
    pref = intent.get("preferred_days") or []
    if pref:
        pretty = ", ".join(date.fromisoformat(d).strftime("%a %b %-d") for d in pref)
        has = [d for d in pref if any(str(o.get("start", ""))[:10] == d for o in options)]
        line = (f"They said {pretty} would be best; "
                + ("that day has options below." if has else "that day has no option below, so say so."))
    else:
        line = "They named no preferred day."
    if counter:
        line += (f" They offered {counter} time{'s' if counter > 1 else ''} and none is free on the user's side:"
                 " the opening says so plainly, then offers the options below instead.")
    if widened_from:
        line += (f" Nothing is free on {date.fromisoformat(widened_from).strftime('%a %b %-d')}, the day asked about:"
                 " the opening says so, then offers the nearest days below.")
    if dropped:
        # The reply goes to the sender alone (run() sets sender_only), so
        # the opening speaks to the two of them and names no one else.
        line += (f" No slot fits everyone on the thread, so the options below are free for the user and"
                 f" {intent.get('sender_name') or 'the sender'} only, and this reply goes to them alone;"
                 " the opening says these are times for the two of them.")
    if wide:
        line += (f" This is an availability request: pick up to {WIDE_PICKS} options, at most two per day,"
                 " so they can choose; the opening says these are the open times.")
    return line


def pick_options(intent: dict, options: list[dict], request: str, *, today: date, user: dict,
                 prompts: dict, llm=None, context: str = "", max_picks: int = MAX_PICKS) -> tuple[dict, dict]:
    llm = llm or _rl.chat_json
    preference_line = context or pick_context(intent, options)
    where = f" in {intent['sender_timezone'].rsplit('/', 1)[-1].replace('_', ' ')}" if intent.get("sender_timezone") else ""
    system = prompts["pick"].format(
        user_first=user["first"], today=today.strftime("%A %Y-%m-%d"), sender_name=intent.get("sender_name") or "them",
        sender_where=where, request=request, preference_line=preference_line, max_picks=max_picks)
    listing = option_listing(options)
    obj, meta = llm([{"role": "system", "content": system},
                     {"role": "user", "content": f"OPTIONS:\n{listing or '(none)'}"}],
                    purpose=PURPOSE, schema=PICK_SCHEMA, max_tokens=500)
    return obj, {"latency_ms": meta.get("latency_ms"), "model": meta.get("model")}


# ── checks and repair ─────────────────────────────────────────────────

def check_pick(pick: dict, intent: dict, options: list[dict], *, max_picks: int = MAX_PICKS,
               per_day: int = 1) -> tuple[list[str], list[str]]:
    """(problems the model's answer has, the ids the reply uses). The
    reply uses the model's valid picks; code fills or trims to distinct
    days (`per_day` options on one day for a listing) and the preferred
    day when the model did not."""
    by_id = {o["id"]: o for o in options if o.get("id")}
    picks = [str(p) for p in (pick.get("picks") or [])]
    problems = []
    valid = [p for p in picks if p in by_id]
    if len(valid) < len(picks):
        problems.append("unknown-id")
    if len(picks) > max_picks:
        problems.append("too-many")
    days = [str(by_id[p]["start"])[:10] for p in valid]
    if any(days.count(d) > per_day for d in set(days)):
        problems.append("same-day")
    pref = intent.get("preferred_days") or []
    pref_opts = [o["id"] for o in options if str(o.get("start", ""))[:10] in pref]
    if pref_opts and not any(p in pref_opts for p in valid):
        problems.append("preferred-day-skipped")
    if bool(pick.get("preferred_day_available")) != bool(any(p in pref_opts for p in valid)):
        problems.append("preferred-flag-wrong")
    for k in ("opening", "closing"):
        if _sr.names_a_clock(str(pick.get(k) or "")):
            problems.append(f"clock-in-{k}")
    if not options:
        return problems, []
    if not problems:
        return problems, valid
    # repair: distinct days; the preferred day first (the model's pick on
    # it, else the solver's first option there), then the model's picks,
    # then solver order
    chosen, seen = [], {}
    for p in [p for p in valid if p in pref_opts] + pref_opts[:1] + valid + [o["id"] for o in options]:
        d = str(by_id[p]["start"])[:10]
        if seen.get(d, 0) >= per_day or p in chosen:
            continue
        chosen.append(p)
        seen[d] = seen.get(d, 0) + 1
        if len(chosen) >= max_picks:
            break
    if chosen != valid[:len(chosen)]:
        problems.append("repaired")
    return problems, chosen


def _invite_word() -> str:
    """'a Zoom invite' on an install whose platform is Zoom, else 'a
    calendar invite': the fallback closing names the user's platform."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        platform = institution.video_platform()
    except Exception:
        platform = ""
    return f"a {platform} invite" if platform else "a calendar invite"


def render_reply(pick: dict, ids: list[str], options: list[dict], intent: dict, user: dict) -> str:
    by_id = {o["id"]: o for o in options if o.get("id")}
    first = (intent.get("sender_name") or "").split()[0] if intent.get("sender_name") else ""
    lines = [by_id[i]["line"] for i in ids if i in by_id]
    closing = str(pick.get("closing") or "")
    if not closing.strip():
        closing = f"Let me know which works and I will send {_invite_word()}."
    return _sr.render_body(first_name=first, opening=str(pick.get("opening") or ""), lines=lines,
                           closing=closing, signature=user["first"])


# ── the run ───────────────────────────────────────────────────────────

def run(request: str, *, conversation_id: str = "", run_id: str = "", today: date | None = None,
        user: dict | None = None, prompts: dict | None = None, llm=None, runner=None,
        live: bool = False, job: dict | None = None, record: bool = True,
        messages: list | None = None, as_of: str = "") -> dict:
    """One request through the pipeline; the turn record (also appended
    to the store unless record=False). `messages` is a thread already
    read (a replay); `as_of` (ISO) drops messages sent after that moment,
    so a replay sees the thread as the original turn did."""
    t0 = time.monotonic()
    run_id = run_id or f"pipe-{int(time.time())}"
    rec = {"ts": _iso(), "run_id": run_id, "request": request, "prompts": "default",
           "live": bool(live), "status": "ok", "steps": [], "problems": [], "body": "", "write": None}
    try:
        user = user or user_profile()
    except ProfileError as e:
        # no profile, no reply: a body with no greeting and no signature
        # is never drafted
        return _need(rec, f"The user profile could not be read ({e}), so no reply can be written.", t0, record)
    prompts = prompts or load_prompts()
    rec["prompts"] = prompts.get("name", "default")
    tz = ZoneInfo(user.get("zone") or DEFAULT_TZ)
    real_today = _now(tz).date()
    today = today or real_today
    # the clock a replay runs on: midnight of its `today`
    now = _now(tz) if today == real_today else datetime(today.year, today.month, today.day, tzinfo=tz)

    def step(name, **kw):
        rec["steps"].append({"step": name, **kw})

    # 1. locate
    detail = {}
    rec["thread_ts"] = str((job or {}).get("thread_ts") or "")
    if messages is None:
        if not conversation_id:
            conversation_id, detail = locate_thread(request, run_id=run_id, runner=runner, llm=llm, user=user, prompts=prompts)
            if not conversation_id and rec["thread_ts"] and not name_tokens(request):
                # a follow-up that names no one stays on the thread the
                # last turn in this chat thread was about; a request that
                # names someone never falls back to another thread
                conversation_id = last_conversation_for(rec["thread_ts"])
                if conversation_id:
                    detail = dict(detail, fallback="the previous turn in this chat thread")
                    detail.pop("need", None)
        step("locate", conversation_id=conversation_id[:24], **{k: v for k, v in detail.items() if k != "need"})
        if not conversation_id:
            if detail.get("need"):
                return _need(rec, detail["need"], t0, record)
            rec["status"] = "no-thread"
            return _finish(rec, t0, record)
        parsed, err = read_tool("get-conversation", {"conversation_id": conversation_id, "max_chars": THREAD_CHARS}, run_id, runner)
        messages = (parsed.get("messages") if isinstance(parsed, dict) else parsed) or []
        if not messages:
            rec["status"] = "no-thread"
            step("read", error=err[:200])
            return _finish(rec, t0, record)
    if as_of:
        messages = [m for m in messages if str(m.get("sent") or m.get("received") or "") <= as_of]
        rec["as_of"] = as_of
    if not messages:
        rec["status"] = "no-thread"
        return _finish(rec, t0, record)
    conversation_id = conversation_id or str(messages[-1].get("conversation_id") or "")
    step("read", messages=len(messages))
    rec["conversation_id"] = conversation_id
    rec["target"] = reply_target(messages, user["email"])

    # 2. intent
    try:
        intent, meta = extract_intent(request, messages, today=today, user=user, prompts=prompts, llm=llm)
    except Exception as e:
        rec["status"], rec["error"] = "intent-failed", str(e)[:300]
        return _finish(rec, t0, record)
    intent, nudged = nudge_mode(intent, request)
    rec["intent"] = intent
    step("intent", **meta, **({"nudged": nudged} if nudged else {}))
    if intent["mode"] in ("other", "reschedule"):
        rec["status"] = f"handoff-{intent['mode']}"
        return _finish(rec, t0, record)
    if intent["mode"] == "invite":
        return _run_invite(rec, intent, request, messages, run_id=run_id, user=user, live=live,
                           job=job, runner=runner, t0=t0, record=record, now=now)

    # 3. solve. Exact times the other side named are checked first; when
    # none is free, code proposes instead and the reply says so. An empty
    # window is retried without the CC'd attendees, then widened past a
    # single day; the record and the user's line carry what was done.
    options, counter, dropped, widened_from = [], 0, [], ""
    wide = wants_listing(request)
    if intent.get("offered_times"):
        args = {"times": list(intent["offered_times"]), "duration": int(intent.get("duration_minutes") or 30)}
        rec["solver_call"] = {"tool": "validate-draft", "args": args}
        out, err = read_tool("validate-draft", args, run_id, runner)
        if not isinstance(out, dict):
            rec["status"], rec["error"] = "solver-failed", err[:300]
            return _finish(rec, t0, record)
        need = fetch_error_need(out)
        if need:
            # a calendar that could not be read looks free: say nothing
            # about availability
            step("validate", offered=len(intent["offered_times"]), unread_calendar=True)
            return _need(rec, need, t0, record)
        options = options_from_validate(out, intent, [{"email": intent["sender_email"], "timezone": intent.get("sender_timezone")}],
                                        zone=user.get("zone") or DEFAULT_TZ)
        step("validate", offered=len(intent["offered_times"]), free=len(options),
             warned=sum(1 for o in options if option_warnings(o)))
        if not options:
            counter = len(intent["offered_times"])
            if not intent.get("window_start"):
                first = min(intent["offered_times"])[:10]
                intent = dict(intent, window_start=first,
                              window_end=(date.fromisoformat(first) + timedelta(days=WIDEN_DAYS)).isoformat())
    if not options:
        solve = dict(intent, mode="propose", offered_times=[])
        for _attempt in range(3):
            tool, args = solver_args(solve, today=today, user_email=user["email"])
            rec["solver_call"] = {"tool": tool, "args": args}
            out, err = read_tool(tool, args, run_id, runner)
            if not isinstance(out, dict):
                rec["status"], rec["error"] = "solver-failed", err[:300]
                return _finish(rec, t0, record)
            options = [o for o in out.get("options") or [] if o.get("id")]
            step("solve", tool=tool, options=len(options), warnings=(out.get("warnings") or [])[:3],
                 **({"dropped": dropped} if dropped else {}),
                 **({"widened_from": widened_from} if widened_from else {}))
            if options:
                break
            if solve.get("other_attendees"):
                dropped, solve = list(solve["other_attendees"]), sender_only(solve)
            elif solve.get("window_start") and solve["window_start"] == solve.get("window_end") and not widened_from:
                widened_from, solve = solve["window_start"], widened(solve)
            else:
                break
    rec["options"] = [{"id": o["id"], "start": o["start"], "line": o["line"], "prefs": option_prefs(o),
                       "warnings": option_warnings(o)} for o in options]
    rec.update(counter=counter, dropped=dropped, widened_from=widened_from, wide=wide)
    if not options:
        rec["status"] = "no-options"
        return _finish(rec, t0, record)

    # 4. pick
    context = pick_context(intent, options, counter=counter, wide=wide, widened_from=widened_from, dropped=dropped)
    max_picks, per_day = (WIDE_PICKS, 2) if wide else (MAX_PICKS, 1)
    try:
        pick, meta = pick_options(intent, options, request, today=today, user=user, prompts=prompts, llm=llm,
                                  context=context, max_picks=max_picks)
    except Exception as e:
        rec["status"], rec["error"] = "pick-failed", str(e)[:300]
        return _finish(rec, t0, record)
    rec["pick"] = pick
    step("pick", **meta)

    # 5. check, 6. render
    problems, ids = check_pick(pick, intent, options, max_picks=max_picks, per_day=per_day)
    rec["problems"], rec["picked"] = problems, ids
    rec["body"] = render_reply(pick, ids, options, intent, user)
    rec["reply"] = _user_line(rec, intent, pick, ids, options)

    if live and rec["target"].get("message_id"):
        args = {"message_id": rec["target"]["message_id"], "conversation_id": conversation_id, "body": rec["body"]}
        if dropped:
            # the times fit the sender alone, so the reply goes to them alone
            args["sender_only"] = True
        obs, _cmd = _sa.live_write("create-reply", args, job or {"run_id": run_id})
        rec["write"] = obs
    return _finish(rec, t0, record)


def _need(rec: dict, need: str, t0: float, record: bool) -> dict:
    """The record of a turn the pipeline cannot finish on its own: status
    `need`, the question in `need`, `error` and `reply`. The bot's
    hand-off path reads the status and Claude takes the turn with the
    question in hand; a shadow or CLI run prints it."""
    rec["status"] = "need"
    rec["need"] = rec["error"] = need
    rec["reply"] = f":question: {need}"
    return _finish(rec, t0, record)


def _user_line(rec: dict, intent: dict, pick: dict, ids: list[str], options: list[dict]) -> str:
    """What the thread hears: one line, the times as the solver wrote
    them, and on a time one of the user's preferences ranked lower or a
    validate warning sits, which one (the email body never carries
    that)."""
    by_id = {o["id"]: o for o in options}
    who = intent.get("sender_name") or intent.get("sender_email") or "them"

    def marks(o: dict) -> str:
        out = []
        if option_prefs(o):
            out.append(f" (ranked lower: {'; '.join(option_prefs(o))})")
        if option_warnings(o):
            out.append(f" (warning: {'; '.join(option_warnings(o))})")
        return "".join(out)

    lines = "\n".join(f"• {by_id[i]['line']}{marks(by_id[i])}" for i in ids if i in by_id)
    facts = []
    if rec.get("counter"):
        n = rec["counter"]
        facts.append(f"None of the {n} time{'s' if n > 1 else ''} they offered is free, so this is a counter-offer.")
    if rec.get("widened_from"):
        facts.append(f"Nothing is free on {date.fromisoformat(rec['widened_from']).strftime('%a %b %-d')};"
                     f" the window was widened by a week.")
    if rec.get("dropped"):
        facts.append(f"No slot fit everyone on the thread; {', '.join(rec['dropped'])} were left out of the search"
                     f" and the reply goes to {who} only.")
    notes = " ".join(s for s in facts + [intent.get("note_to_user"), pick.get("note_to_user")] if s)
    head = (f":email: I made a reply draft to {who}." if rec.get("live")
            else f"Reply to {who} (shadow, not written):")
    return "\n".join(p for p in (head, lines, notes) if p)


# ── the invite shape ──────────────────────────────────────────────────

_RE_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*|\[(?:external|ext)\]\s*)+", re.I)


def thread_subject(messages: list) -> str:
    for m in reversed(messages or []):
        subj = _RE_PREFIX.sub("", str(m.get("subject") or "")).strip()
        if subj:
            return subj[:120]
    return ""


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_WORD = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)


def _names_day(text: str, dt: datetime) -> bool | None:
    """True or False when the text names days and the option's day is or
    is not among them; None when the text names no day."""
    days = {m.group(1).lower() for m in _DAY_WORD.finditer(text)}
    dates = {(m.group(1).lower(), int(m.group(2))) for m in _MONTH_DAY.finditer(text)}
    if not days and not dates:
        return None
    return (dt.strftime("%a").lower() in days) or ((dt.strftime("%b").lower(), dt.day) in dates)


def _names_clock(text: str, dt: datetime) -> bool:
    """True when the text names the option's clock ('9:00', '9 AM', '21:00')."""
    h12, mm, h24 = dt.strftime("%-I"), dt.strftime("%M"), dt.strftime("%H")
    pat = rf"(?<![\d:]){h12}:{mm}\b|(?<![\d:]){h12}(?::{mm})?\s*[ap]\.?m\b|(?<![\d:]){h24}:{mm}\b"
    return bool(re.search(pat, text, re.I))


def agreed_time(times: list[str], messages: list, user: dict, *, tz: ZoneInfo,
                sender_tz: str = "") -> tuple[str, str]:
    """(the ISO time the other side agreed to, or '' and why). One
    offered time is the agreed one. With several in play, the latest
    message from someone other than the user settles it: the one time it
    names by clock (on the user's clock or the sender's), else the one
    time on the day it names. Anything else is ambiguous."""
    if not times:
        return "", "no agreed time in the thread or the request"
    if len(times) == 1:
        return times[0], ""
    text = ""
    for m in reversed(messages or []):
        if m.get("is_draft") or m.get("own") or (user.get("email") and _addr(m.get("from")) == user["email"].lower()):
            continue
        text = str(m.get("text") or m.get("body") or "")
        break
    parsed = []
    for t in times:
        dt = _sa._parse_iso(str(t), tz)
        if dt is not None:
            parsed.append((t, dt))
    zones = [tz]
    try:
        if sender_tz:
            zones.append(ZoneInfo(sender_tz))
    except Exception:
        pass
    by_clock = [t for t, dt in parsed if any(
        _names_clock(text, dt.astimezone(z)) and _names_day(text, dt.astimezone(z)) is not False for z in zones)]
    if len(by_clock) == 1:
        return by_clock[0], ""
    if not by_clock:
        by_day = [t for t, dt in parsed if _names_day(text, dt.astimezone(tz))]
        if len(by_day) == 1:
            return by_day[0], ""
    pretty = ", ".join(dt.astimezone(tz).strftime("%a %b %-d %-I:%M %p") for _t, dt in parsed)
    return "", (f"{len(parsed)} times are in play ({pretty}) and the thread does not say which one"
                " they agreed to. Which one is the invite for?")


def build_event(intent: dict, request: str, *, user: dict, messages: list,
                now: datetime | None = None) -> tuple[dict | None, str]:
    """(the event to stage, or None and why). Code owns every field: the
    agreed time (agreed_time: the one the other side accepted, never
    simply the first offered), the title from the intent or the thread's
    subject, the attendees from the thread, the institution's video link
    (`online`) when anyone else is on it. A time already past is a
    `why`, not an event. "My calendar" or "hold it" is a hold on the
    user's own calendar, which carries no attendees and mails no one,
    whoever is on the thread."""
    tz = ZoneInfo(user.get("zone") or DEFAULT_TZ)
    agreed, why = agreed_time(intent.get("offered_times") or [], messages, user, tz=tz,
                              sender_tz=intent.get("sender_timezone") or "")
    if not agreed:
        return None, why
    start = _sa._parse_iso(str(agreed), tz)
    if start is None:
        return None, f"the agreed time {agreed!r} is not a date-time"
    if now is not None and start < now:
        return None, (f"the agreed time {start.astimezone(tz).strftime('%a %b %-d %-I:%M %p')} has passed;"
                      " which time is the invite for?")
    end = start + timedelta(minutes=int(intent.get("duration_minutes") or 30))
    # "my calendar", "hold it": the user's own entry, whoever is on the
    # thread, unless the same request also asks to send the invite.
    hold = bool(_HOLD_RE.search(request or "")) and not _INVITE_RE.search(request or "")
    others = {a for a in [intent.get("sender_email", "")] + list(intent.get("other_attendees") or [])
              if a and "@" in a and a.lower() != user["email"].lower()}
    sender_first = (intent.get("sender_name") or "").split()[0] if intent.get("sender_name") else ""
    subject = intent.get("subject") or thread_subject(messages) or (
        f"{user['first']} / {sender_first}" if sender_first else "Meeting")
    event = {"subject": subject, "start": start.isoformat(), "end": end.isoformat()}
    if hold or not others:
        event["hold"] = True
    else:
        event["attendees"] = ",".join(sorted(others))
        event["online"] = True
    return event, ""


def _event_names(intent: dict, request: str, user: dict) -> list[str]:
    """The other people's names an event subject would carry: the names in
    the request and the sender's last name, never the user's own."""
    own = {w.lower() for w in str(user.get("name") or "").split()}
    names = []
    for n in name_tokens(request) + str(intent.get("sender_name") or "").split()[-1:]:
        n = n.strip(".,()")
        if len(n) >= 3 and n.lower() not in own and n.lower() not in STOPWORDS and n not in names:
            names.append(n)
    return names


def _names_in(text: str, names: list[str]) -> bool:
    low = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])", low) for n in names)


MAX_EVENT_READS = 3


def already_on_calendar(event: dict, intent: dict, request: str, *, run_id: str, user: dict,
                        runner=None) -> dict | None:
    """The calendar event that is this meeting already, or None. An event
    overlapping the agreed time is the meeting when its subject names one
    of the other people (or, for a hold, is the hold's own subject), or
    when an invitee is among its attendees. The EA's tentative holds,
    all-day entries and time marked free are never the meeting. A calendar
    that cannot be read is None: the stage goes ahead as before."""
    out, _err = read_tool("calendar-view", {"start": event["start"], "end": event["end"]}, run_id, runner)
    events = out.get("events") if isinstance(out, dict) else None
    if not isinstance(events, list):
        return None
    names = _event_names(intent, request, user)
    invitees = {a.strip().lower() for a in str(event.get("attendees") or "").split(",") if a.strip()}
    own_subject = str(event.get("subject") or "").strip().lower()
    timed = [e for e in events if isinstance(e, dict) and not e.get("ea_hold") and not e.get("all_day")
             and str(e.get("show_as") or "").lower() != "free"]

    def found(e: dict) -> dict:
        utc = lambda s: (s + "+00:00") if s and len(s) == 19 else s
        return {"id": str(e.get("id") or ""), "subject": str(e.get("subject") or ""),
                "start": e.get("start_local") or utc(str(e.get("start") or "")),
                "end": e.get("end_local") or utc(str(e.get("end") or ""))}

    # The subjects first, which cost nothing; then the attendees of the
    # rest, one read each.
    for e in timed:
        subject = str(e.get("subject") or "")
        if _names_in(subject, names) or (event.get("hold") and subject.strip().lower() == own_subject):
            return found(e)
    if not invitees:
        return None
    for e in [e for e in timed if e.get("id")][:MAX_EVENT_READS]:
        full, _err = read_tool("get-event", {"event_id": e["id"]}, run_id, runner)
        people = {_addr(a.get("emailAddress") or {}) for a in (full or {}).get("attendees") or []
                  if isinstance(a, dict)} if isinstance(full, dict) else set()
        if people & invitees:
            return found(e)
    return None


def _run_invite(rec: dict, intent: dict, request: str, messages: list, *, run_id: str, user: dict,
                live: bool, job: dict | None, runner, t0: float, record: bool,
                now: datetime | None = None) -> dict:
    """The invite shape: the agreed time checked against the calendar (a
    conflict is said, not fatal: the user agreed to it), the event built
    by code and, live, staged for the Create button. Nothing is created
    without the click. No agreed time is a hand-off; several in play or
    a time already past is a need (a question for the user). A meeting
    already on the calendar at that time is said, and nothing is staged."""
    rec["kind"] = "invite"
    event, why = build_event(intent, request, user=user, messages=messages, now=now)
    if event is None:
        if not intent.get("offered_times"):
            rec["status"] = "handoff-invite"
            rec["error"] = why
            return _finish(rec, t0, record)
        return _need(rec, why, t0, record)
    attendee_zones = ([{"email": intent.get("sender_email") or "", "timezone": intent.get("sender_timezone")}]
                      if event.get("attendees") else [])
    existing = already_on_calendar(event, intent, request, run_id=run_id, user=user, runner=runner)
    rec["steps"].append({"step": "calendar", "existing": bool(existing)})
    if existing:
        labelled = [{"start": existing["start"], "end": existing["end"]}]
        _sr.label_options(labelled, attendee_zones, user_zone=user.get("zone") or DEFAULT_TZ)
        line = labelled[0].get("line") or existing["start"]
        rec["existing"] = existing
        rec["event_line"] = line
        rec["reply"] = (f":calendar: You already have a time: {existing['subject']}, {line}. "
                        "I staged no invite.")
        rec["status"] = "ok"
        return _finish(rec, t0, record)
    conflicts = []
    out, _err = read_tool("validate-draft", {"times": [event["start"]],
                                             "duration": int(intent.get("duration_minutes") or 30)}, run_id, runner)
    if isinstance(out, dict):
        conflicts = [str(i.get("conflict_with") or i.get("detail") or "an event") for i in out.get("issues") or []
                     if isinstance(i, dict) and i.get("type") == "conflict"]
    rec["step_note"] = {"conflicts": conflicts[:3]}
    rec["steps"].append({"step": "validate", "conflicts": len(conflicts)})
    labelled = [{"start": event["start"], "end": event["end"]}]
    _sr.label_options(labelled, attendee_zones, user_zone=user.get("zone") or DEFAULT_TZ)
    line = labelled[0].get("line") or event["start"]
    rec["event"] = event
    rec["event_line"] = line
    kind = "a hold" if event.get("hold") else f"an invite to {event['attendees']}"
    if live:
        obs, _cmd = _sa.live_write("stage-event", {"event": event}, job or {"run_id": run_id})
        rec["write"] = obs
        if obs.get("status") != "staged":
            rec["status"] = "write-failed"
            rec["error"] = str(obs.get("message") or obs.get("status") or "")[:200]
            return _finish(rec, t0, record)
        head = f":calendar: I staged {kind}: {event['subject']}, {line}."
    else:
        head = f"Would stage {kind}: {event['subject']}, {line} (shadow, not written)."
    notes = []
    if conflicts:
        notes.append("That time overlaps " + "; ".join(conflicts[:2]) + " on your calendar.")
    if intent.get("note_to_user"):
        notes.append(intent["note_to_user"])
    rec["reply"] = "\n".join([head] + notes)
    rec["status"] = "ok"
    return _finish(rec, t0, record)


def _finish(rec: dict, t0: float, record: bool) -> dict:
    rec["seconds"] = round(time.monotonic() - t0, 1)
    if record:
        try:
            _rl.append(TURNS, rec)
        except Exception:
            pass
    return rec


# ── shadow beside a live Claude turn ──────────────────────────────────

VARIANTS_FILE = Path("tuning") / "sched-pipeline-variants.json"


def load_variants(store: Path | None = None) -> list[dict]:
    """The prompt sets the shadow runs on every scheduling turn: the
    variants file (a list of {name, intent?, pick?, locate?}, each
    completed from the tuned or default set), else the one loaded set."""
    base = load_prompts(store)
    try:
        d = json.loads(((store or _rl.store_dir()) / VARIANTS_FILE).read_text())
        out = []
        for i, v in enumerate(d if isinstance(d, list) else d.get("variants") or []):
            if isinstance(v, dict):
                out.append(dict(base, **{k: v[k] for k in ("intent", "pick", "locate") if isinstance(v.get(k), str) and v[k].strip()},
                                name=str(v.get("name") or f"variant-{i + 1}")))
        if out:
            return out
    except Exception:
        pass
    return [base]


def _overlap(rec: dict, claude_reply: str, claude_drafts: list, today: date, tz: ZoneInfo) -> dict:
    """How the pipeline's picked starts compare with the times Claude
    named on the same turn (its reply and drafts, parsed)."""
    by_id = {o["id"]: o for o in rec.get("options") or []}
    mine = {_sr.parse_dt(by_id[i]["start"]).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
            for i in rec.get("picked") or [] if i in by_id}
    theirs = _sa._starts(_sa.side_times(claude_reply or "", [str(d) for d in claude_drafts or []], [], today, tz))
    both = mine | theirs
    return {"pipeline": sorted(mine), "claude": sorted(theirs),
            "overlap": round(len(mine & theirs) / len(both), 3) if both else None}


def shadow(job: dict, *, variants: list[dict] | None = None, llm=None, runner=None, record: bool = True) -> list[dict]:
    """The pipeline's answer to a scheduling turn Claude just took, once
    per prompt variant, written nowhere but the store. Each record
    carries Claude's times and the overlap with the pipeline's."""
    user = user_profile()
    tz = ZoneInfo(user.get("zone") or DEFAULT_TZ)
    ts = str(job.get("ts") or "")
    try:
        today = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()
    except ValueError:
        today = _now(tz).date()
    claude = job.get("claude") or {}
    out = []
    for prompts in (variants or load_variants()):
        rec = run(str(job.get("user_msg") or ""), run_id=f"shadow-{str(job.get('run_id') or '')[:40]}",
                  today=today, user=user, prompts=prompts, llm=llm, runner=runner, as_of=ts, record=False)
        rec["shadow_of"] = {"run_id": job.get("run_id"), "thread_ts": job.get("thread_ts"), "lane": job.get("lane")}
        rec["claude"] = {"reply": str(claude.get("reply") or "")[:2000], "elapsed_s": claude.get("elapsed_s")}
        if rec.get("body"):
            rec["compare"] = _overlap(rec, claude.get("reply") or "", claude.get("drafts") or [], today, tz)
        if record:
            try:
                _rl.append(TURNS, rec)
            except Exception:
                pass
        out.append(rec)
    return out


# ── report ────────────────────────────────────────────────────────────

def report(days: int = 14) -> str:
    rows = [r for r in _rl.read(TURNS) if str(r.get("ts", ""))[:10] >= (date.today() - timedelta(days=days)).isoformat()]
    if not rows:
        return "Pipeline: no turns recorded."
    by = {}
    for r in rows:
        b = by.setdefault(r.get("prompts") or "default", {"n": 0, "ok": 0, "clean": 0, "problems": {}, "status": {}})
        b["n"] += 1
        b["status"][r.get("status")] = b["status"].get(r.get("status"), 0) + 1
        if r.get("status") == "ok":
            b["ok"] += 1
            if not r.get("problems"):
                b["clean"] += 1
            ov = (r.get("compare") or {}).get("overlap")
            if ov is not None:
                b.setdefault("overlaps", []).append(ov)
            for p in r.get("problems") or []:
                b["problems"][p] = b["problems"].get(p, 0) + 1
    out = [f"Pipeline, last {days} days: {len(rows)} turns."]
    for name, b in sorted(by.items()):
        probs = ", ".join(f"{k} {v}" for k, v in sorted(b["problems"].items(), key=lambda kv: -kv[1])) or "none"
        ovs = b.get("overlaps") or []
        same = f"; times shared with Claude {sum(ovs) / len(ovs):.2f} over {len(ovs)} shadow turns" if ovs else ""
        out.append(f"  {name}: replies {b['ok']}/{b['n']} ({b['clean']} with no check problem); "
                   f"problems {probs}; statuses {b['status']}{same}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="one request through the pipeline")
    p.add_argument("--text", required=True)
    p.add_argument("--conversation-id", default="")
    p.add_argument("--live", action="store_true", help="create the reply draft (the Send button still gates it)")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--thread-file", default="", help="a get-conversation JSON to use instead of reading the thread")
    p.add_argument("--as-of", default="", help="ISO moment; messages after it are dropped (a replay)")
    p.add_argument("--today", default="", help="YYYY-MM-DD the run should believe is today (a replay)")
    p = sub.add_parser("report")
    p.add_argument("--days", type=int, default=14)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        messages = None
        if a.thread_file:
            d = json.loads(Path(a.thread_file).read_text())
            messages = (d.get("messages") if isinstance(d, dict) else d) or []
        rec = run(a.text, conversation_id=a.conversation_id, live=a.live, record=not a.no_record,
                  messages=messages, as_of=a.as_of,
                  today=date.fromisoformat(a.today) if a.today else None)
        print(json.dumps({k: rec.get(k) for k in ("status", "seconds", "steps", "intent", "solver_call",
                                                   "picked", "problems", "body", "reply", "error")},
                         indent=1, default=str))
        return 0 if rec.get("status") == "ok" else 1
    print(report(a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
