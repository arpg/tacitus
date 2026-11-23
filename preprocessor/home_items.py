"""A Home item opened in place (docs/webchat.md, Home items): its quick
briefing, its choices, and what a choice does.

The owner taps an item on Home ("Left for you", or a line of "So far
today") and it opens where it is, like a block in Action requests:

  GET  /api/home/item?ref=<ref>   item(src, ref): the item as the app
                                  draws it, built in code at once
  POST /api/home/act              Acts.act: one choice
  POST /api/home/undo             Acts.undo: take a Done, Not needed,
                                  Waiting or Later back

The quick briefing is built here, in code alone, from the ledger row, its
thread's record (the brief the local model keeps, never the mail), the
owner's earlier word on the thread (`owner_said`), the reply queue's
record and the Action request; no model writes a word of it, and an item
kept from hosted models (keep_gate: `local`, `facts`) gets none at all:
its title is keep_gate's fixed line and its one choice opens it in
Outlook. "What Tacitus would do next" is chosen by code from the row's
type and state, never from a guess at the facts.

The choices, by kind:

  reply      Draft a reply, Tell Tacitus what to say, Not needed, Later
  item       Start it with Tacitus, Done, Waiting on someone, Later
  question   the answers an earlier run offered (the Replies card's
             buttons), Other answer, Not needed, Later; a question with
             no reply behind it: Answer, Later
  request    Open it in Action requests (Send and Create stay there)
  closed     Not done yet (a row closed today)
  drafted, answered
             Open it in Action requests
  kept       Open in Outlook (a kept request: Open it in Action requests)

What each choice runs is the machinery that already does it:

  Draft a reply, Tell Tacitus what to say, an answer
      the Replies card's Draft it turn (the bot's _dispatch_reply_draft,
      through Hooks.draft): one turn for the row, its Send card in
      Action requests, the owner's line or answer quoted to the turn as
      his instruction for this reply alone
  Not needed
      the Replies card's Not needed (Hooks.not_needed): the row and the
      thread's other reply rows close on the owner's click (signal
      `user_handled`), its draft is deleted, its mail archived. No
      missed-closure line: no signal could have seen that no reply was
      wanted
  Done
      the web chat's Mark done (web_thread_state.run_writeback): a row
      that carries a close proposal is confirmed; any other closes on the
      owner's word ("marked done in the web chat") and is logged with
      log_missed_closure.py, since a closure the owner had to state is a
      closure the pipeline missed
  Later      ledger.py update --status snoozed --snooze-until <day>, as
             the Replies card's Skip
  Waiting    ledger.py update --status waiting --follow-up-after <day>
  Start it with Tacitus, an answer to a question with no reply behind it
      a new conversation in Ask Tacitus (or the row's project room) that
      starts with the owner's message naming the item, sent at once, as
      if he typed it (WebFrontend.web_message)
  Not done yet
      the web chat's Needs me on a closed row (run_writeback's reopen)

Done, Not needed, Waiting and Later say what they did at once and the
item leaves Home at once; the ledger write waits UNDO_S seconds, so Undo
in that time takes it back exactly (nothing was written yet, no mail was
moved, no draft deleted). After it Undo is refused in plain words. The
pending choice is kept in Home's own store, so a restart in between still
writes it (Home's tick). When the write comes, the item is read again:
one that changed meanwhile (a new message, a close from elsewhere) is
left as it is, and the owner sees it again.

A choice is checked against the item as it is now: the app sends the
item's `version` (a digest of what the briefing and the choices are built
from), and an act on an item that changed since the app read it, or with
a choice the item no longer offers, is refused in plain words with the
item as it is now. Home never sends mail, never creates an event and never
holds a confirmation token: Send and Create are buttons in Action
requests, and the owner presses them there.

The deeper briefing is home_deeper's (one hook, read lazily, and
"unavailable" while the module is not there): its block in the item, its
start, and its next steps (`deeper.steps`, after the code-built choices).
A step that names one of the choices above runs here as that tap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("chat.web.home.items")

# How long Undo takes back a Done, Not needed, Waiting or Later; the ledger
# write waits this long.
UNDO_S = 30
# The longest line the owner writes on Tell Tacitus what to say or an
# answer (the cards' own bound, chat.cards.ACTION_TEXT_CHARS).
TEXT_MAX = 300
# Bounds on the briefing's words.
FIELD_MAX = 200
STANDING_MAX = 4
SAID_MAX = 140
# A choice marked as being written this long ago was cut short (a crash, a
# restart): the next tick writes it again.
WRITE_STALE_S = 600
# Later: the furthest day a snooze may name.
LATER_MAX_DAYS = 366
# The closing note of Not needed on Home; ledger.NO_REPLY_RE reads it as
# "no reply goes out", so a later row on the thread carries it.
NOT_NEEDED_NOTE = "No reply needed: the owner's tap on Home"
# What the briefing names a row by its type.
TYPE_WORDS = {"response_needed": "A reply you owe", "follow_up": "A follow-up",
              "scheduling": "A meeting to set up", "task": "A task", "deadline": "A deadline",
              "waiting": "Something others owe you", "reimbursement": "A reimbursement",
              "dispute": "A dispute", "draft_pending": "A document to finish"}
# The thread record's fields the version reads: what the other side and
# the owner wrote, and when.
THREAD_KEYS = ("latest", "last_inbound", "last_from_user", "message_count", "needs_reply",
               "user_replied_after_latest", "waiting_on_reply", "ask", "deadline", "state",
               "scheduling", "subject")
ROW_KEYS = ("status", "type", "deadline", "snooze_until", "follow_up_after", "description",
            "last_action", "closes_on", "from_name", "from_email", "notes")
DEFERRED = ("done", "not_needed", "later", "waiting")
# The web chat's store file (web_frontend.STORE_FILE), read by pending_rows.
STORE_FILE = "webchat.sqlite"

# The choices, as the app draws them.
DRAFT = {"id": "draft", "label": "Draft a reply", "style": "primary"}
SAY = {"id": "say", "label": "Tell Tacitus what to say", "input": "text"}
NOT_NEEDED = {"id": "not_needed", "label": "Not needed"}
START = {"id": "start", "label": "Start it with Tacitus", "style": "primary"}
DONE = {"id": "done", "label": "Done"}
WAITING = {"id": "waiting", "label": "Waiting on someone"}
OPEN = {"id": "open", "label": "Open it in Action requests", "style": "primary"}
OUTLOOK = {"id": "outlook", "label": "Open in Outlook", "style": "primary"}
REOPEN = {"id": "reopen", "label": "Not done yet"}

GONE = "This is not on your list any more."
CHANGED = "This changed since you opened it. Here it is as it is now."
NO_CHOICE = "That choice is not there any more."
TOO_LATE = "It is too late to undo that. Ask Tacitus to bring it back."


def _hs():
    """home_screen, beside this module (imported when Home first asks, so
    neither module imports the other as it loads)."""
    if __package__:
        from . import home_screen as mod
    else:
        import home_screen as mod
    return mod


def _wts():
    """web_thread_state: the web chat's Mark done and Needs me write-back,
    and its CLI runner (RUN), which tests replace with a fake ledger."""
    if __package__:
        from . import web_thread_state as mod
    else:
        import web_thread_state as mod
    return mod


def _ledger():
    if __package__:
        from . import ledger as mod
    else:
        import ledger as mod
    return mod


def _plain(text: Any, limit: int = FIELD_MAX) -> str:
    return _hs()._plain(text, limit)


def _sentence(text: Any, limit: int = FIELD_MAX) -> str:
    """A record's words as one sentence: plain, capitalized, ending in a stop."""
    t = _plain(text, limit)
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".?!…" else t + "."


@dataclass
class Hooks:
    """What only the bot can run (ea-chat-bot.py sets them on Home.hooks):

    draft(row id, words, answer) -> {"ok", "said", "channel"?, "id"?}
        the Replies card's Draft it turn for the row; `words` the owner's
        line, `answer` one of the answers the row's question offers.
        `channel` and `id` name the line that opens the turn's thread.
    not_needed(row id, note) -> {"ok", "said"}
        the Replies card's Not needed, with `note` as the closing note.
    outlook(conversation id) -> link or ""
        the Outlook link of the thread's newest message.
    letter(case id, answer, destination id, words) -> {"ok", "said", "channel"?, "id"?}
        a letter case's answer, as a press on its card in Action requests
        runs it (letter_surface.answer, its turn on a new line there).
    case(case id, answer, part id, words) -> {"ok", "said", "channel"?, "id"?}
        the same for a case of any other kind (a review, a form, a
        report: case_surface.answer).
    """
    draft: Callable[[str, str, str], dict] | None = None
    not_needed: Callable[[str, str], dict] | None = None
    outlook: Callable[[str], str] | None = None
    letter: Callable[[str, str, str, str], dict] | None = None
    case: Callable[[str, str, str, str], dict] | None = None


# ── finding the item ────────────────────────────────────────────────

@dataclass
class Found:
    """One Home item and what the bot knows of it."""
    item: dict
    lid: str = ""
    conv: str = ""
    row: dict | None = None
    reply: dict | None = None
    request: dict | None = None
    view: dict | None = None
    closed: dict | None = None
    keep: str = ""


def find(src, ref: str) -> Found | None:
    """The item `ref` names on Home now (what is left, held or not, and
    what got done today), or None."""
    hs = _hs()
    if not ref:
        return None
    hit = next((x for x in hs.left(src) if x.get("id") == ref), None)
    if hit is None:
        hit = next((x for x in hs.done(src) if x.get("id") == ref), None)
    if hit is None:
        return None
    rows = src.rows or {}
    lid, conv, rid = str(hit.get("_lid") or ""), str(hit.get("_conv") or ""), str(hit.get("_rid") or "")
    kind = hit.get("kind")
    reqs = src.requests or {}
    request = next((r for r in list(reqs.get("waiting") or []) + list(reqs.get("done") or [])
                    if isinstance(r, dict) and rid and str(r.get("id") or "") == rid), None)
    # the reply-queue entry of the row (a reply, its question, or an item of
    # the post's list whose row owes a reply)
    reply = next((r for r in src.replies or [] if isinstance(r, dict) and lid and r.get("id") == lid),
                 None) if kind not in ("request", "closed", "drafted", "answered") else None
    view = next((it for v in hs.views(src) for it in v.get("items") or []
                 if (lid and it.get("ledger_id") == lid) or (not lid and conv and it.get("conversation_id") == conv)),
                None) if str(hit.get("id") or "").startswith("item:") else None
    closed = next((r for r in src.closed or [] if isinstance(r, dict) and str(r.get("id") or "") == lid),
                  None) if kind == "closed" else None
    f = Found(item=hit, lid=lid, conv=conv, row=rows.get(lid) if lid else None,
              reply=reply, request=request, view=view, closed=closed)
    level = hit.get("kept") or ""
    for obj in (f.reply, f.row, f.request, f.view, f.closed):
        if isinstance(obj, dict) and hs.is_kept(obj.get("keep_level")):
            level = level or obj["keep_level"]
    f.keep = level if hs.is_kept(level) else ""
    if not f.conv:
        f.conv = str((f.row or {}).get("conversation_id") or (f.reply or {}).get("conversation_id") or "")
    return f


def _thread(f: Found) -> dict:
    brief = (f.row or {}).get("thread_brief")
    return brief if isinstance(brief, dict) else {}


def _queued(f: Found) -> dict:
    q = (f.reply or {}).get("queued")
    if isinstance(q, dict) and q:
        return q
    q = (f.row or {}).get("reply_queue")
    return q if isinstance(q, dict) else {}


def _decision(f: Found) -> bool:
    return _queued(f).get("outcome") == "decision"


def _picks(f: Found) -> list[str]:
    """The answers the row's question offers, as the Replies card shows them."""
    if not _decision(f):
        return []
    try:
        return _ledger().reply_choices(_queued(f).get("choices") or ())
    except Exception:
        return []


def _owner_said(f: Found) -> dict:
    said = (f.reply or {}).get("owner_said") or (f.row or {}).get("owner_said") or []
    return next((d for d in said if isinstance(d, dict) and d.get("said")), {})


def _open_row(f: Found) -> bool:
    """A ledger verb may act on the item: it names a row that is open now
    (the replies owed come from open rows)."""
    if not f.lid:
        return False
    if f.row is not None:
        return str(f.row.get("status") or "open") in ("open", "follow_up", "snoozed")
    return f.reply is not None


# The row's own fields a briefing of it is written from.
ROW_VERSION_KEYS = ("type", "status", "deadline", "description", "last_action", "closes_on", "notes")
_WAITED = re.compile(r"\s*·?\s*Waiting \d+ days?")


def _digest(key: dict) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


def row_version(row: dict | None) -> str:
    """A digest of what a briefing of the row is written from: its own fields,
    its thread's record (what each side wrote and when), the reply queue's
    record and a close proposal; never anything that moves with the clock.
    The deeper briefing is stored against it, at ingest from the row as the
    ledger reads it (ledger.get_entry), and is stale once it differs. A
    brief refreshed with the same facts keeps it."""
    row = row or {}
    thread = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else {}
    queued = row.get("reply_queue") if isinstance(row.get("reply_queue"), dict) else {}
    prop = row.get("close_proposal")
    return _digest({"row": {k: row.get(k) for k in ROW_VERSION_KEYS},
                    "thread": {k: thread.get(k) for k in THREAD_KEYS}, "queued": queued,
                    "proposal": prop.get("why") if isinstance(prop, dict) else bool(prop)})


def version(f: Found) -> str:
    """The item's own digest: its row's (row_version), the list's words for
    it, the reply-queue entry, the owner's word on the thread and the
    request. An act on an item whose version moved since the app read it
    is refused. How long it has waited is left out: the clock alone never
    changes it."""
    reply, req = f.reply or {}, f.request or {}
    item = {k: f.item.get(k) for k in ("kind", "label", "title", "kept", "due", "href", "link", "by", "at")}
    item["detail"] = _WAITED.sub("", str(f.item.get("detail") or ""))
    return _digest({
        "item": item, "row": row_version(f.row), "row_keys": {k: (f.row or {}).get(k) for k in ROW_KEYS},
        "queued": _queued(f), "said": _owner_said(f),
        "thread": {k: reply.get(k) for k in THREAD_KEYS} if not _thread(f) else {},
        "reply": {k: reply.get(k) for k in ("since", "ask", "subject", "from", "from_name")},
        "request": {k: req.get(k) for k in ("updated_at", "kind", "text", "what", "who", "when", "waiting",
                                            "keep_level")},
        "keep": f.keep})


# ── the quick briefing ──────────────────────────────────────────────

def _today(src) -> date:
    return datetime.fromtimestamp(src.now, src.zone).date()


def _date_of(value: Any) -> date | None:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _day_words(d: date, today: date) -> str:
    """"today", "tomorrow", "yesterday", a weekday within a week either way,
    else "November 30"."""
    gap = (d - today).days
    if gap in (0, 1, -1):
        return ("today", "tomorrow", "yesterday")[gap]
    if abs(gap) < 7:
        return d.strftime("%A")
    return f"{d.strftime('%B')} {d.day}"


def _when(d: date, today: date) -> str:
    """"today", "tomorrow", "yesterday", "on Monday", "on November 30"."""
    word = _day_words(d, today)
    return word if word in ("today", "tomorrow", "yesterday") else f"on {word}"


def _due_words(d: date | None, today: date) -> str:
    if d is None:
        return ""
    if d < today:
        return f"Overdue since {_day_words(d, today)}."
    return f"Due {_day_words(d, today)}."


def _ago(ts: float | None, src) -> str:
    """"today", "yesterday", "3 days ago" on the owner's clock."""
    if ts is None:
        return ""
    days = (_today(src) - datetime.fromtimestamp(ts, src.zone).date()).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _since_words(ts: float | None, src) -> str:
    """"It came in today.", "It has waited since Friday."."""
    if ts is None:
        return ""
    d, today = datetime.fromtimestamp(ts, src.zone).date(), _today(src)
    if d >= today:
        return "It came in today."
    return f"It has waited since {_day_words(d, today)}."


def _who(f: Found) -> str:
    reply, row, thread = f.reply or {}, f.row or {}, _thread(f)
    name = reply.get("from_name") or row.get("from_name")
    if not name:
        people = reply.get("people") or thread.get("people") or []
        first = people[0] if people and isinstance(people[0], dict) else {}
        name = first.get("name")
    return _plain(name or reply.get("from") or row.get("from_email"), 60)


def _subject(f: Found) -> str:
    return _plain((f.reply or {}).get("subject") or _thread(f).get("subject"), 100)


def _standing(f: Found, src) -> list[str]:
    """Where it stands: how long it has waited, the owner's last message and
    his earlier word, what Tacitus did, a close it proposed, a meeting asked."""
    hs = _hs()
    out: list[str] = []
    reply, row, thread, queued = f.reply or {}, f.row or {}, _thread(f), _queued(f)
    if f.item.get("kind") in ("reply", "question") and reply:
        age = int(reply.get("age_days") or 0)
        out.append("It came in today." if age <= 0 else
                   f"It has waited {hs._n(age, 'day')} for your reply.")
    mine = hs._ts(thread.get("last_from_user"))
    if mine:
        out.append(f"You last wrote on it {_ago(mine, src)}.")
    wait = thread.get("waiting_on_reply") if isinstance(thread.get("waiting_on_reply"), dict) else {}
    if wait.get("since"):
        out.append("You asked them something and wait for their answer.")
    said = _owner_said(f)
    if said:
        out.append(f'You said earlier: "{_plain(said.get("said"), SAID_MAX)}"')
    outcome = queued.get("outcome")
    note = _plain(queued.get("note"), SAID_MAX)
    if outcome == "drafted":
        out.append("Tacitus made a draft for it.")
    elif outcome == "blocked" and note:
        out.append(f"Tacitus could not write it: {note}")
    elif outcome == "left" and note:
        out.append(f"Tacitus found no reply was needed: {note}")
    prop = row.get("close_proposal") if isinstance(row.get("close_proposal"), dict) else None
    if prop:
        why = _plain(prop.get("why"), SAID_MAX)
        out.append(f"Tacitus thinks it is done: {why}" if why else "Tacitus thinks it is done.")
    sched = str(thread.get("scheduling") or reply.get("scheduling") or "none")
    if sched not in ("", "none"):
        times = thread.get("offered_times") or []
        out.append(f"They offered {hs._n(len(times), 'time')} to meet." if times else "It is about a meeting.")
    last = _last_step(row.get("last_action"), src)
    if last and not outcome and len(out) < STANDING_MAX:
        out.append(last)
    return out[:STANDING_MAX]


_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ][\d:.]+Z?)?\s*[:-]\s*")


# A last action that is a lane's bookkeeping, not a step the owner would
# name: a tool's tag at its end ("(reconcile)"), an id, a hash, a path, a
# shouted word, or a code closer's note.
_NOT_A_STEP = re.compile(
    r"\([a-z_ -]{3,20}\)\.?$|\b(?=[A-Za-z0-9_=+/-]*\d)[A-Za-z0-9_=+/-]{16,}|\b[0-9a-f]{7,40}\b|[~/]\w+/|"
    r"^(?:auto|co-closed|closed|reopened)\b|\bsnooze|\bskipped\b", re.I)
_SHOUTED = re.compile(r"\b[A-Z]{5,}\b")
_LANE_TAG = re.compile(r"\([a-z_ -]{3,20}\)\.?$")
_ISO_DAY = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)")


def _last_step(text: Any, src) -> str:
    """The row's last action as one plain sentence: "Last step yesterday:
    asked the committee for its template." (its leading date in day words);
    '' for a lane's bookkeeping (_NOT_A_STEP), which the owner never named."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    when = ""
    m = _DATED.match(raw)
    if m:
        d = _date_of(m.group(1))
        raw = raw[m.end():]
        if d is not None:
            when = f" {_when(d, _today(src))}"
    raw = _a_step(raw)
    return _sentence(f"Last step{when}: {raw}", SAID_MAX + 30) if raw else ""


def _a_step(raw: str) -> str:
    """The first sentence of a note the EA wrote, when it reads as plain
    words; '' for a lane's bookkeeping (_NOT_A_STEP and the rest)."""
    if not raw or _LANE_TAG.search(raw):
        return ""
    first = _FIRST_SENTENCE.match(raw)
    raw = first.group(1) if first else raw
    if not raw or len(raw) > SAID_MAX or _NOT_A_STEP.search(raw) or _SHOUTED.search(raw) \
            or _ISO_DAY.search(raw) or raw.count("(") != raw.count(")") or raw.count("'") % 2:
        return ""
    return _plain(second_person(raw, owner_names()), SAID_MAX)


# ── the owner in the third person ────────────────────────────────────
# The EA writes its notes about the owner by name ("Chris confirmed the
# date."); the card speaks to him: "You confirmed the date." Code rewrites
# the name (the profile's, at a word boundary) to "you", and a subject's
# verb to agree with it, for the common verbs; a note it cannot rewrite
# cleanly is not shown. The stored note is never changed.

_BE_HAVE_DO = {"is": "are", "was": "were", "has": "have", "does": "do", "isn't": "aren't",
               "wasn't": "weren't", "hasn't": "haven't", "doesn't": "don't"}
_PRESENT = {w + "s": w for w in (
    "ask", "want", "need", "say", "plan", "think", "prefer", "agree", "expect", "owe", "know", "write",
    "send", "approve", "like", "hope", "intend", "lead", "review", "keep", "get", "make", "take", "give",
    "see", "decline", "confirm", "request", "handle", "own", "attend", "join", "sign", "chair", "serve",
    "use", "mean", "plan", "read", "hold", "help", "share", "check", "call", "meet", "visit", "travel",
    "return", "accept", "remember", "recall", "insist", "suggest", "propose", "offer", "promise")}
_PRESENT.update({"goes": "go", "teaches": "teach", "wishes": "wish", "reaches": "reach",
                 "watches": "watch", "finishes": "finish", "replies": "reply", "tries": "try",
                 "applies": "apply", "carries": "carry"})
_PAST = frozenset("said made sent wrote took gave got went told met put read set left kept thought "
                  "found chose spoke saw came ran paid heard held built brought bought began forgot "
                  "knew sat stood won lost meant led drove flew rode understood".split())
_MODALS = frozenset("will would can could should may might must shall won't can't couldn't wouldn't "
                    "shouldn't didn't".split())
_ADVERBS = frozenset("also still then now already just later again first never often".split())
_THIRD = re.compile(r"\b(?:he|him|his|himself)\b", re.I)
_PROFILE_TTL_S = 300
_PROFILE: dict = {}


def owner_names() -> tuple[str, ...]:
    """The owner's full name and first name from user-profile.json (read
    with the resident's access, as the file is in the resident's space);
    () when it cannot be read."""
    import time as _time
    now = _time.monotonic()
    if _PROFILE and now - _PROFILE.get("at", 0) < _PROFILE_TTL_S:
        return _PROFILE["names"]
    names: tuple[str, ...] = ()
    try:
        if __package__:
            from . import resident_io
        else:
            import resident_io
        text = resident_io.read_text(_hs()._ea_dir() / "config" / "user-profile.json")
        name = " ".join(str((json.loads(text) if text else {}).get("name") or "").split())
        first = name.split(" ")[0] if name else ""
        if len(first) >= 2 and first.isalpha():
            names = tuple(dict.fromkeys(x for x in (name, first) if x))
    except Exception as e:
        log.debug("home: the owner's name could not be read: %s", e)
    _PROFILE.update(at=now, names=names)
    return names


def _verb(word: str) -> str | None:
    """`word` as it reads after "You" (its trailing stop kept), or None
    when it is not a verb this knows."""
    core = word.rstrip(",;:.")
    tail, low = word[len(core):], core.lower()
    if low in _BE_HAVE_DO:
        return _BE_HAVE_DO[low] + tail
    if low in _PRESENT:
        return _PRESENT[low] + tail
    if low in _PAST or low in _MODALS or (low.endswith("ed") and len(low) > 3):
        return word
    return None


_NEXT_WORDS = re.compile(r"(\s+)(\S+)(?:(\s+)(\S+))?")


def second_person(text: str, names: tuple[str, ...] = ()) -> str:
    """`text` with the owner's name as "you": "Chris confirmed" -> "You
    confirmed", "Chris asks" -> "You ask", "asks Chris to" -> "asks you
    to", "Chris's" -> "your". '' when a use cannot be rewritten cleanly: a
    subject whose verb is not one this knows, or "he", "him" or "his" left
    after he was the subject. `text` unchanged when it does not name him."""
    if not names or not text:
        return text
    pat = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")(?:'s|\u2019s|')?(?![\w'])")
    if not pat.search(text):
        return text
    out: list[str] = []
    pos, subject = 0, False
    for m in pat.finditer(text):
        if m.start() < pos:
            continue
        out.append(text[pos:m.start()])
        so_far = "".join(out)
        cap = not so_far.strip() or bool(re.search(r"[.!?]\s*$", so_far))
        at_start = cap or bool(re.search(r"[;:]\s*$", so_far))
        if m.group(0).endswith(("'s", "\u2019s", "'")):
            alone = not re.match(r"\s*[A-Za-z0-9]", text[m.end():])  # "not Chris's." -> "not yours."
            out.append(("Your" if cap else "your") + ("s" if alone else ""))
            pos = m.end()
            continue
        if not at_start:
            out.append("you")
            pos = m.end()
            continue
        nxt = _NEXT_WORDS.match(text, m.end())
        if not nxt:
            return ""
        first = nxt.group(2)
        if first.lower().rstrip(",;:") in _ADVERBS and nxt.group(4):
            verb = _verb(nxt.group(4))
            words = f"{nxt.group(1)}{first}{nxt.group(3)}{verb}" if verb else None
        else:
            verb = _verb(first)
            words = f"{nxt.group(1)}{verb}" if verb else None
            nxt_end = nxt.start(3) if nxt.group(3) else nxt.end()
        if words is None:
            return ""
        out.append(("You" if cap else "you") + words)
        pos = nxt.end() if first.lower().rstrip(",;:") in _ADVERBS and nxt.group(4) else nxt_end
        subject = True
    out.append(text[pos:])
    result = "".join(out)
    if subject and _THIRD.search(result):
        return ""
    return result


def _next(f: Found) -> str:
    """What Tacitus would do next, chosen by code from the row's type and
    state; '' when nothing fits."""
    kind = f.item.get("kind")
    row, thread, queued = f.row or {}, _thread(f), _queued(f)
    reply = f.reply or {}
    if kind == "request":
        card = (f.request or {}).get("card") if isinstance((f.request or {}).get("card"), dict) else {}
        ck = str(card.get("kind") or "")
        if ck in ("send", "bulk", "schedule"):
            return "Nothing goes out until you tap Send in Action requests."
        if ck in ("event", "event_update", "event_delete"):
            return "Your calendar does not change until you tap its button in Action requests."
        return "It waits for your tap in Action requests."
    if kind == "question":
        if _decision(f):
            return "Once you answer, Tacitus writes the reply. You check it before it goes."
        return "Once you answer, Tacitus works on it with you."
    work = _work(f)
    if kind == "reply":
        if work:
            return WORK_NEXT[work]
        if queued.get("outcome") == "drafted":
            return "Bring its draft back to Action requests for your Send."
        sched = str(thread.get("scheduling") or reply.get("scheduling") or "none")
        if sched == "proposes-times" and thread.get("offered_times"):
            return "Draft a reply that takes one of the times they offered, if your calendar is free."
        if sched not in ("", "none"):
            return "Draft a reply with two or three times from your calendar."
        return "Draft a short reply for you to check. Nothing goes out without your Send."
    if kind == "item":
        if isinstance(row.get("close_proposal"), dict):
            return "Close it once you say it is done."
        if _ledger().is_waiting(row):
            return "Wait for their answer. It stays off your list until then."
        if str(thread.get("scheduling") or "none") not in ("", "none"):
            return "Find times on your calendar once you start it."
        if work in ITEM_WORK_NEXT:
            return ITEM_WORK_NEXT[work]
        return "Nothing on its own. Start it when you are ready."
    return ""


# What Tacitus would do next for a request that needs work, not only an
# email (mail_completion.response_mode, #334's signal): on a reply, and on
# a to-do. "other" is a row whose type is work the owner delivers
# (ledger.types_with("deliverable"), not a reply type) with no clearer mode.
WORK_NEXT = {"submission": "This needs a submission, not just a reply. Ask Tacitus to start it.",
             "form": "This needs a form filled in, not just a reply. Ask Tacitus to start it.",
             "document": "This needs a document written, not just a reply. Ask Tacitus to start it.",
             "other": "This needs work, not just a reply. Ask Tacitus to start it."}
ITEM_WORK_NEXT = {"submission": "Work on the submission with you once you start it.",
                  "form": "Fill in the form with you once you start it.",
                  "document": "Write the document with you once you start it."}


def _work(f: Found) -> str:
    """The work the request asks for beyond an email: submission, form,
    document, other; '' for none."""
    row = dict(f.row or {})
    if not row and f.reply:
        row = {"ask": f.reply.get("ask"), "state": f.reply.get("state"), "type": f.reply.get("type")}
    if not row:
        return ""
    try:
        if __package__:
            from . import mail_completion
        else:
            import mail_completion
        mode = mail_completion.response_mode(row)
    except Exception:
        mode = "unknown"
    if mode in ("submission", "form", "document", "other"):
        return mode
    led = _ledger()
    rtype = led.canonical_type(row.get("type"))
    if rtype in led.types_with("deliverable") and rtype not in led.types_with("closes_on_reply"):
        return "other"
    return ""


def pending_rows() -> dict[str, str]:
    """{ledger id: choice} for the Done, Not needed, Waiting and Later on
    Home that wait for their undo window, read from Home's store by another
    owner process (the briefing's pre-send review), so it treats them as
    done: the choice is the owner's, and it is written within seconds. {}
    when this process is not the owner's or the store cannot be read."""
    try:
        if __package__:
            from . import chat_notify as _cn, owner_trust
        else:
            import chat_notify as _cn
            import owner_trust
        if not owner_trust.is_owner_process():
            return {}
        # the web chat's store as the chat package finds it for the owner
        # (chat._web_store_path): the block's store_path, else the private dir's
        try:
            web = _cn._web_cfg()
        except Exception:  # no chat settings: the default place
            web = {}
        path = Path(str(web["store_path"])) if web.get("store_path") else \
            owner_trust.private_dir() / STORE_FILE
        if not path.exists():
            return {}
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            got = conn.execute("SELECT value FROM meta WHERE key = ?", ("home:" + Acts.STORE,)).fetchone()
        finally:
            conn.close()
        acts = json.loads(got[0]) if got and got[0] else {}
    except Exception as e:
        log.debug("home: the choices that wait for an undo could not be read: %s", e)
        return {}
    return {str(v["lid"]): str(v["op"]) for v in (acts.values() if isinstance(acts, dict) else [])
            if isinstance(v, dict) and v.get("lid") and v.get("op") in DEFERRED}


# The item kinds that stand for a work case: a letter, or a case of any
# other kind (case_surface).
CASE_ITEMS = ("letter", "case")


def _case(f: Found):
    """(case_surface, the case a `letter` or `case` item stands for) or
    (module, None)."""
    if __package__:
        from . import case_surface as cs
    else:
        import case_surface as cs
    cid = str(f.item.get("_case") or "")
    if not cid:
        return cs, None
    kinds = ["letter"] if f.item.get("kind") == "letter" else cs.other_kinds()
    return cs, cs.find(cs.load_cases(kinds=kinds), cid)


def brief(f: Found, src) -> dict:
    """The quick briefing: {"what", "asked", "due", "standing", "next"}, each
    "" (or []) where nothing is known. A kept item has none of it."""
    out = {"what": "", "asked": "", "due": "", "standing": [], "next": ""}
    if f.keep:
        return out
    if f.item.get("kind") in CASE_ITEMS:
        cs, case = _case(f)
        if not case:
            return out
        try:
            news = cs.news(case, src.rows or {}, now=datetime.fromtimestamp(float(src.now), timezone.utc))
        except Exception:
            news = []
        return cs.home_brief(case, _today(src), news=news)
    hs = _hs()
    kind = f.item.get("kind")
    today = _today(src)
    row, thread, reply = f.row or {}, _thread(f), f.reply or {}
    who, subject = _who(f), _subject(f)
    if kind == "request":
        req = f.request or {}
        action, what = _plain(req.get("action"), 40), _plain(req.get("what") or req.get("text"), 120)
        who_ = _plain(req.get("who"), 80)
        out["what"] = _sentence(": ".join(x for x in (action, what) if x)
                                + (f", {who_[0].lower() + who_[1:]}" if who_ else ""))
        if req.get("why") in ("due", "starts") and req.get("at"):
            d = datetime.fromtimestamp(float(req["at"]), src.zone).date()
            out["due"] = _due_words(d, today) if req["why"] == "due" else \
                f"It starts {_when(d, today)} at {hs.clock(float(req['at']), src.zone)}."
        posted = hs._ts(req.get("posted_at"))
        out["standing"] = [x for x in (_since_words(posted, src),
                                       f"Project: {_plain(req.get('project'), 60)}." if req.get("project")
                                       else "") if x]
        out["next"] = _next(f)
        return out
    if kind in ("closed", "drafted", "answered"):
        at = f.item.get("at")
        clock = hs.clock(float(at), src.zone) if at else ""
        by = "You" if f.item.get("by") == "you" else "Tacitus"
        out["what"] = {"closed": f"{by} closed it at {clock}.",
                       "drafted": f"Tacitus drafted it at {clock}. It waits for your Send.",
                       "answered": f"You answered it at {clock}."}[kind]
        note = _a_step(" ".join(str((f.closed or {}).get("last_action") or "").split()))
        if note and kind == "closed":
            out["standing"] = [_sentence(f"Why: {note}", SAID_MAX + 6)]
        return out
    title = _plain(f.item.get("title"), 120)
    if subject and subject.lower() == title.lower():
        subject = ""  # the title says it already
    if kind == "reply":
        out["what"] = _sentence(f"A reply you owe {who or 'them'}" + (f' about "{subject}"' if subject else ""))
    elif kind == "question":
        out["what"] = _sentence((f"Tacitus needs your answer before it writes to {who or 'them'}"
                                 + (f' about "{subject}"' if subject else "")) if _decision(f)
                                else _plain(f.item.get("detail"), 120) or "A question for you")
    else:
        rtype = _ledger().canonical_type(row.get("type")) if row else ""
        head = TYPE_WORDS.get(rtype, "Something to do")
        out["what"] = _sentence(f"{head}, from {who}" if who and not _ledger().is_waiting(row) else head)
    ask = thread.get("ask") or reply.get("ask")
    if not ask and kind == "item" and not thread:
        ask = row.get("notes")
    if kind == "question" and not _decision(f):
        ask = ""  # the question is the title
    out["asked"] = _sentence(ask) if ask else ""
    due = _date_of(row.get("deadline")) or _date_of(thread.get("deadline"))
    if due is None and f.item.get("due"):
        due = datetime.fromtimestamp(float(f.item["due"]), src.zone).date()
    out["due"] = _due_words(due, today)
    out["standing"] = _standing(f, src)
    out["next"] = _next(f)
    return out


# ── the choices ─────────────────────────────────────────────────────

def later_choice(today: date) -> dict:
    """Later with its days: tomorrow, Monday (when that is not tomorrow) and
    a day the owner picks (`arg` "date" asks the app for one)."""
    tomorrow = today + timedelta(days=1)
    monday = today + timedelta(days=(7 - today.weekday()) or 7)
    opts = [{"arg": "tomorrow", "label": "Tomorrow", "day": tomorrow.isoformat()}]
    if monday != tomorrow:
        opts.append({"arg": "monday", "label": "Monday", "day": monday.isoformat()})
    opts.append({"arg": "date", "label": "Pick a date", "min": tomorrow.isoformat(),
                 "max": (today + timedelta(days=LATER_MAX_DAYS)).isoformat()})
    return {"id": "later", "label": "Later", "input": "later", "options": opts}


def choices(f: Found, src) -> list[dict]:
    """The code-built choices, in their order; the deeper briefing's steps
    go after them."""
    kind = f.item.get("kind")
    today = _today(src)
    if kind == "request":
        return [dict(OPEN)]
    if kind in ("drafted", "answered"):
        return [dict(OPEN)] if f.item.get("href") else []
    if kind == "closed":
        return [dict(REOPEN)] if f.lid and not f.keep else []
    if kind in CASE_ITEMS:
        # a work case's own answers (case_surface.home_choices): no mail
        # text in them, so a kept case has them too, in fixed words
        cs, case = _case(f)
        return cs.home_choices(case, today, f.keep or "full") if case else []
    acts = _open_row(f)
    if f.keep:  # keep_gate's line alone: open it in Outlook, or settle it here
        return ([dict(OUTLOOK)] if (f.item.get("link") or f.conv) else []) + \
            ([dict(DONE), later_choice(today)] if acts else [])
    if kind == "reply":
        return [dict(DRAFT), dict(SAY)] + ([dict(NOT_NEEDED), later_choice(today)] if acts else [])
    if kind == "question":
        if _decision(f):
            picks = _picks(f)
            out = [{"id": f"answer:{i + 1}", "label": text} for i, text in enumerate(picks)]
            out.append({"id": "answer", "label": "Other answer" if picks else "Answer", "input": "text",
                        **({} if picks else {"style": "primary"})})
            return out + ([dict(NOT_NEEDED), later_choice(today)] if acts else [])
        return [{"id": "answer", "label": "Answer", "input": "text", "style": "primary"}] + \
            ([later_choice(today)] if acts else [])
    out = [dict(START)]
    if acts:
        waits = _ledger().is_waiting(f.row or {})
        out += [dict(DONE)] + ([] if waits else [dict(WAITING)]) + [later_choice(today)]
    return out


def _goto(f: Found) -> str:
    if f.item.get("href"):
        return str(f.item["href"])
    if f.keep and f.item.get("link"):
        return str(f.item["link"])
    return ""


def item(src, ref: str, *, fe=None, pending: dict | None = None) -> dict | None:
    """The item as GET /api/home/item answers it, or None when Home does not
    list it now. `pending`: a choice that waits for its undo window (the item
    is shown with what it said, and no choices)."""
    f = find(src, ref)
    if f is None:
        return None
    return shape(f, src, fe=fe, pending=pending)


def shape(f: Found, src, *, fe=None, pending: dict | None = None) -> dict:
    """The item as the app draws it; `deeper` is the hook's block, read at
    once (it never waits on a model, and never starts one)."""
    v = version(f)
    x = f.item
    out = {"ref": x["id"], "kind": x.get("kind"), "label": x.get("label") or "",
           "kept": f.keep, "title": x.get("title") or "", "detail": "" if f.keep else (x.get("detail") or ""),
           "version": v, "row_version": row_version(f.row) if f.row is not None else "",
           "brief": brief(f, src), "choices": choices(f, src)}
    goto = _goto(f)
    if goto:
        out["goto"] = goto
    if x.get("ask") and not f.keep:
        out["ask"] = x["ask"]
    if pending:
        out["choices"] = []
        out["pending"] = {"said": pending.get("said") or "", "undo": pending.get("token") or "",
                          "until": pending.get("due")}
        out["deeper"] = {"state": "none"}
        return out
    out["deeper"] = deeper_state(fe, f, out)
    return out


# ── the deeper briefing's hook ──────────────────────────────────────
# preprocessor/home_deeper.py builds the deeper briefing; until it is
# there each item reads {"state": "unavailable"}. Three calls, each safe:
# a failure reads as "unavailable" or a plain refusal. Its next steps stay
# in `deeper.steps`, after the code-built choices; a step chosen is
# "step:<id>", and a step that names one of these choices runs it here, so
# the undo, the ledger verbs and the closure rule stay this module's.

DEEPER_FAILED = "Tacitus could not do that. Try again."


def _deeper_mod():
    try:
        if __package__:
            from . import home_deeper as mod
        else:  # preprocessor/ on sys.path
            import home_deeper as mod
    except ImportError:
        return None
    return mod


# A work case's briefing (a letter, a review, a form, a report) is
# case_briefing's: what was asked, its parts, what came in and what is
# missing, what changed since the owner last looked, and its next steps,
# each the answer of a button on the case's card. Without the module, its
# quick briefing is all there is.
LETTER_DEEPER = {"state": "unavailable", "note": "Everything Tacitus knows about this letter is above."}
CASE_DEEPER = {"state": "unavailable", "note": "Everything Tacitus knows about it is above."}
CASE_FAILED = "Tacitus could not read this briefing. Try again in a minute."
# A press while the case store waits for its owner's migration step: the
# words a press on the case's card shows (ea-chat-bot's _press_case).
MIGRATION_SAID = "I could not do that. Ask me in Ask Tacitus."


def _case_none(it: dict) -> dict:
    """A case item's block with no briefing module."""
    return dict(LETTER_DEEPER if it.get("kind") == "letter" else CASE_DEEPER)


def _case_mod():
    try:
        if __package__:
            from . import case_briefing as mod
        else:  # preprocessor/ on sys.path
            import case_briefing as mod
    except ImportError:
        return None
    return mod


def _case_call(fn: str, fe, f: Found, it: dict, *args, **kw) -> dict | None:
    """case_briefing's `fn` for a case item (kind `letter` or `case`) by
    its case id (the surface's case goes along for a store out of reach);
    None without the module. A failure here is a fault in code, never a
    state of the case: it is logged with its trace and comes back as
    {"failed": True}, which the card shows as a failure."""
    mod = _case_mod()
    if mod is None:
        return None
    try:
        _cs, case = _case(f)
        got = getattr(mod, fn)(fe, it["ref"], *args, item={k: v for k, v in it.items() if k != "deeper"},
                               case=case, case_id=str(f.item.get("_case") or ""), **kw)
    except Exception:
        log.exception("home: the case briefing's %s failed on %s", fn, it.get("ref"))
        return {"failed": True}
    if not isinstance(got, dict):
        log.error("home: the case briefing's %s gave %r on %s", fn, type(got).__name__, it.get("ref"))
        return {"failed": True}
    return got


def deeper_state(fe, f: Found, it: dict) -> dict:
    """The item's `deeper` block (home_deeper.state), read at once."""
    if it.get("kind") in CASE_ITEMS:
        got = _case_call("state", fe, f, it)
        if got is None:
            return _case_none(it)
        return got if got.get("state") else {"state": "failed", "note": CASE_FAILED}
    mod = _deeper_mod()
    if mod is None:
        return {"state": "unavailable"}
    try:
        got = mod.state(fe, it["ref"], {k: v for k, v in it.items() if k != "deeper"}, row=f.row)
    except Exception as e:
        log.warning("home: the deeper briefing's state failed: %s", e)
        return {"state": "unavailable"}
    return got if isinstance(got, dict) and got.get("state") else {"state": "unavailable"}


def deeper_start(fe, f: Found, it: dict) -> dict:
    """POST /api/home/deeper (home_deeper.start): {"ok", "state", "note"?}."""
    if it.get("kind") in CASE_ITEMS:
        got = _case_call("start", fe, f, it)
        if got is None:
            return {"ok": False, **_case_none(it)}
        return {"ok": False, "state": "failed", "note": CASE_FAILED} if got.get("failed") else got
    mod = _deeper_mod()
    if mod is None:
        return {"ok": False, "state": "unavailable"}
    try:
        got = mod.start(fe, it["ref"], {k: v for k, v in it.items() if k != "deeper"}, row=f.row)
    except Exception as e:
        log.warning("home: the deeper briefing did not start: %s", e)
        return {"ok": False, "said": DEEPER_FAILED}
    return got if isinstance(got, dict) else {"ok": False, "said": DEEPER_FAILED}


def deeper_step(fe, f: Found, it: dict, step_id: str, arg: str) -> dict:
    """A deeper step chosen (home_deeper.step): {"choice", "arg"} for one of
    this module's choices, or a finished {"ok", "said", "goto"?}. A work
    case's step names the answer of a button on its card, `move`."""
    if it.get("kind") in CASE_ITEMS:
        got = _case_call("step", fe, f, it, step_id, arg=arg)
        if got is None:
            return {"ok": False, "said": NO_CHOICE}   # no briefing, so no step of one
        return {"ok": False, "said": DEEPER_FAILED} if got.get("failed") else got
    mod = _deeper_mod()
    if mod is None:
        return {"ok": False, "said": NO_CHOICE}
    try:
        got = mod.step(fe, it["ref"], step_id, {k: v for k, v in it.items() if k != "deeper"},
                       row=f.row, arg=arg)
    except Exception as e:
        log.warning("home: the deeper step %s failed: %s", step_id[:40], e)
        return {"ok": False, "said": DEEPER_FAILED}
    return got if isinstance(got, dict) else {"ok": False, "said": DEEPER_FAILED}


# ── what a choice does ──────────────────────────────────────────────

def later_day(arg: str, today: date) -> date | None:
    """The day Later names: tomorrow, Monday or YYYY-MM-DD (after today,
    within LATER_MAX_DAYS); None for anything else."""
    a = str(arg or "").strip().lower()
    if a == "tomorrow":
        return today + timedelta(days=1)
    if a == "monday":
        return today + timedelta(days=(7 - today.weekday()) or 7)
    d = _date_of(a) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a) else None
    if d is None or d <= today or (d - today).days > LATER_MAX_DAYS:
        return None
    return d


def one_line(text: Any, limit: int = TEXT_MAX) -> str:
    return " ".join(str(text or "").split())[:limit].strip()


def _route(fe, channel: str, mid: str) -> str:
    """The app's route to a message the bot posted: its block in Action
    requests, or the thread it opens; Action requests itself when this
    store does not hold it (a shadow)."""
    try:
        row = fe.store.message(channel, mid) if channel and mid else None
    except Exception:
        row = None
    if row:
        q = lambda s: quote(str(s), safe="")  # noqa: E731
        return f"#/{q(row.get('channel') or channel)}/{q(row.get('thread_key') or mid)}/{q(mid)}"
    try:
        return f"#/{quote(str(fe.dm_channel(fe.owner_id)), safe='')}"
    except Exception:
        return ""


def _room(fe, f: Found) -> str:
    """The project room of the item's row, where it has one and rooms are on;
    else Ask Tacitus."""
    main = getattr(fe, "main_channel", "main")
    if not f.lid and not f.conv:
        return main
    try:
        if callable(getattr(fe, "rooms_on", None)) and not fe.rooms_on():
            return main
        pid = _project_of(f.lid, f.conv)
        if not pid:
            return main
        if __package__:
            from . import ea_channels
        else:
            import ea_channels
        for cid in ea_channels.for_project(pid):
            ch = fe.store.channel(cid)
            if ch and not ch.get("archived"):
                return cid
    except Exception as e:
        log.debug("home: no project room for an item: %s", e)
    return main


def _project_of(lid: str, conv: str) -> str:
    led = _ledger()
    conn = led.guarded_connect(led.DB_PATH, read_only=True)
    try:
        for sql, val in (("SELECT project_id FROM project_ledger WHERE ledger_id = ?", lid),
                         ("SELECT project_id FROM project_messages WHERE conversation_id = ?", conv)):
            if not val:
                continue
            try:
                got = conn.execute(sql, (val,)).fetchone()
            except sqlite3.Error:
                got = None
            if got and got[0]:
                return str(got[0])
    finally:
        conn.close()
    return ""


def start_text(f: Found, b: dict, words: str = "") -> str:
    """The owner's message that opens a conversation about the item: what it
    is, then the ledger item it names. `words`: his answer to its question."""
    title = _plain(f.item.get("title"), 200)
    if words:
        lines = [f'My answer to "{title}": {words}']
    else:
        lines = [f"Let's start this: {title}"]
        lines += [x for x in (b.get("what"), b.get("asked"), b.get("due")) if x]
    if f.lid:
        lines.append(f"(item `{f.lid}`)")
    return "\n".join(lines)


class Acts:
    """The choices made on Home's items: the immediate ones (a draft, a
    conversation, a link) and the ones that wait UNDO_S seconds for an
    Undo (Done, Not needed, Waiting, Later), kept in Home's store as
    `acts` until written."""

    STORE = "acts"

    def __init__(self, home) -> None:
        self.home = home
        self._lock = threading.Lock()

    # the store

    def _all(self) -> dict:
        got = self.home._meta(self.STORE)
        return {k: v for k, v in got.items() if isinstance(v, dict)}

    def _save(self, acts: dict) -> None:
        self.home._meta_set(self.STORE, acts)

    def pending(self) -> dict:
        """{ref: record} of the choices that wait for their undo window."""
        return {v.get("ref"): {**v, "token": k} for k, v in self._all().items() if v.get("ref")}

    # a choice

    def act(self, ref: str, choice: str, arg: str = "", seen: str = "") -> tuple[dict, Callable | None]:
        """One choice on one item: (the answer, a call to run once it is
        sent or None). `seen` is the item's version as the app read it."""
        home = self.home
        ref, choice = str(ref or ""), str(choice or "")
        if ref in self.pending():
            return {"ok": False, "said": "You already chose for this one. Undo it first to choose again."}, None
        src = home.sources()
        f = find(src, ref)
        if f is None:
            return {"ok": False, "said": GONE}, None
        now_item = shape(f, src, fe=home.fe)
        if seen and seen != now_item["version"]:
            return {"ok": False, "said": CHANGED, "item": now_item}, None
        move = False
        if choice.startswith("step:"):
            steps = {str(x.get("id") or "") for x in now_item["deeper"].get("steps") or [] if isinstance(x, dict)}
            if not choice[5:] or choice[5:] not in steps:
                return {"ok": False, "said": NO_CHOICE, "item": now_item}, None
            got = deeper_step(home.fe, f, now_item, choice[5:], one_line(arg))
            if not got.get("choice"):
                return {k: v for k, v in got.items() if k in ("ok", "said", "goto", "item")}, None
            # the step names one of the code-built choices: it runs as that tap
            choice, arg = str(got["choice"]), str(got.get("arg") or "")
            if choice == "draft" and arg:
                choice = "say"
            # a work case's step names the answer of a button on its card
            # (case_briefing checked it against the case's moves): it runs as
            # that press, Home's choices or not
            move = bool(got.get("move")) and f.item.get("kind") in CASE_ITEMS and \
                choice.startswith(f"{f.item.get('kind')}:")
        offered = {c["id"] for c in now_item["choices"]}
        if choice not in offered and not (choice == "say" and "draft" in offered) and not move:
            return {"ok": False, "said": NO_CHOICE, "item": now_item}, None
        if choice in DEFERRED:
            return self._defer(f, src, now_item, choice, arg), None
        return self._now(f, src, now_item, choice, arg)

    def _now(self, f: Found, src, it: dict, choice: str, arg: str) -> tuple[dict, Callable | None]:
        home, fe = self.home, self.home.fe
        hooks = getattr(home, "hooks", None) or Hooks()
        if choice == "open":
            return {"ok": True, "said": "Opening it in Action requests.", "goto": it.get("goto") or ""}, None
        if choice == "outlook":
            link = str(f.item.get("link") or "")
            if not link and f.conv and hooks.outlook is not None:
                try:
                    link = str(hooks.outlook(f.conv) or "")
                except Exception as e:
                    log.warning("home: the Outlook link was not found: %s", e)
                    link = ""
            if not re.match(r"^https://[^\s<>|]+$", link):
                return {"ok": False, "said": "Tacitus could not find its link. Look for it in Outlook."}, None
            return {"ok": True, "said": "Opening it in Outlook.", "goto": link}, None
        if choice == "reopen":
            done = _wts().run_writeback({"ops": [("reopen", f.lid)]})
            ok = any(d.get("ok") for d in done)
            home.changed()
            return {"ok": ok, "said": "Opened again. It is back on your list." if ok
                    else "Tacitus could not open it again. Try again in a minute."}, None
        words = one_line(arg)
        if choice.startswith(("letter:", "case:")):
            cs, case = _case(f)
            kind = str(f.item.get("kind") or "")
            answer, dest = cs.home_choice(choice, (kind,))
            if case is None or not answer:
                return {"ok": False, "said": GONE}, None
            if answer == "date" and not words:
                return {"ok": False, "said": "Write the date first."}, None
            hook = hooks.letter if kind == "letter" else hooks.case
            if hook is None:
                return {"ok": False, "said": "Tacitus cannot do that from here."}, None
            try:
                got = hook(str(case.get("id")), answer, dest, words) or {}
            except Exception as e:
                # by its name: the store is imported as `cases` and as
                # `preprocessor.cases`, two classes of one name
                if type(e).__name__ != "MigrationPending":
                    raise
                # the store waits for its owner's one step (cases.py migrate
                # --apply): as a press on the card says, nothing was done
                log.warning("home: %s on %s waits for the store's migration: %s", choice, case.get("id"), e)
                return {"ok": False, "said": MIGRATION_SAID}, None
            out = {"ok": bool(got.get("ok")), "said": _plain(got.get("said"), 200) or "Nothing was done."}
            route = _route(fe, str(got.get("channel") or ""), str(got.get("id") or "")) \
                if got.get("channel") else ""
            if route:
                out["goto"] = route
            home.changed()
            return out, None
        if choice in ("draft", "say") or (choice.startswith("answer") and _decision(f)):
            answer = ""
            if choice == "say" and not words:
                return {"ok": False, "said": "Write what the reply must say first."}, None
            if choice == "answer" and not words:
                return {"ok": False, "said": "Write your answer first."}, None
            if choice.startswith("answer:"):
                answer = next((c["label"] for c in it["choices"] if c["id"] == choice), "")
                words = ""
            if choice == "draft":
                words = ""
            if hooks.draft is None:
                return {"ok": False, "said": "Tacitus cannot start a draft from here."}, None
            got = hooks.draft(f.lid, words, answer) or {}
            out = {"ok": bool(got.get("ok")), "said": _plain(got.get("said"), 200) or
                   ("Tacitus is writing the reply. Its Send card comes to Action requests." if got.get("ok")
                    else "Tacitus could not start the draft. Try again in a minute.")}
            route = _route(fe, str(got.get("channel") or ""), str(got.get("id") or "")) \
                if got.get("ok") or got.get("channel") else ""
            if route:
                out["goto"] = route
            log.info("home: %s on %s: %s", choice, f.lid, "started" if out["ok"] else "refused")
            return out, None
        if choice in ("start", "answer"):
            if choice == "answer" and not words:
                return {"ok": False, "said": "Write your answer first."}, None
            channel = _room(fe, f)
            text = start_text(f, it["brief"], words if choice == "answer" else "")
            try:
                row, after = fe.web_message(channel, text, None, None)
            except Exception as e:
                log.warning("home: %s did not open a conversation: %s", choice, e)
                return {"ok": False, "said": "Tacitus did not take it. Try again in a moment."}, None
            route = f"#/{quote(str(row.get('channel') or channel), safe='')}/" \
                    f"{quote(str(row.get('thread_key') or row.get('id') or ''), safe='')}"
            log.info("home: %s opened a conversation for %s", choice, f.lid or f.item.get("id"))
            return {"ok": True, "said": "Opened a conversation with Tacitus about it.", "goto": route}, after
        return {"ok": False, "said": NO_CHOICE}, None

    def _defer(self, f: Found, src, it: dict, choice: str, arg: str) -> dict:
        today = _today(src)
        rec = {"ref": it["ref"], "op": choice, "lid": f.lid, "version": it["version"],
               "at": self.home.clock(), "due": self.home.clock() + UNDO_S}
        if choice == "later":
            day = later_day(arg, today)
            if day is None:
                return {"ok": False, "said": "Pick a day: tomorrow, Monday or a date within a year."}
            rec["day"] = day.isoformat()
            rec["said"] = f"Later. It comes back {_when(day, today)}."
        elif choice == "waiting":
            # a day a deeper step named, else the ledger's own wait
            day = later_day(arg, today) if arg else today + timedelta(days=_ledger().WAITING_DAYS)
            if day is None:
                return {"ok": False, "said": "Pick a day within a year."}
            rec["day"] = day.isoformat()
            rec["said"] = "Waiting on someone else. It is off your list."
        elif choice == "not_needed":
            rec["said"] = "Not needed. No reply goes out."
        else:
            rec["proposal"] = isinstance((f.row or {}).get("close_proposal"), dict)
            rec["said"] = "Done. It is off your list."
        token = secrets.token_urlsafe(12)
        with self._lock:
            acts = self._all()
            acts[token] = rec
            self._save(acts)
        self.home.changed()
        if getattr(self.home, "background", False):
            t = threading.Timer(UNDO_S + 0.5, self.commit_due)
            t.daemon = True
            t.start()
        log.info("home: %s on %s waits %ds for an undo", choice, f.lid, UNDO_S)
        return {"ok": True, "said": rec["said"], "undo": token, "undo_until": rec["due"]}

    # undo and the write

    def undo(self, token: str) -> dict:
        with self._lock:
            acts = self._all()
            rec = acts.get(str(token or ""))
            if rec is None or rec.get("writing") or self.home.clock() >= float(rec.get("due") or 0):
                return {"ok": False, "said": TOO_LATE}
            acts.pop(token)
            self._save(acts)
        self.home.changed()
        log.info("home: %s on %s undone", rec.get("op"), rec.get("lid"))
        return {"ok": True, "said": "Undone. It is back on your list."}

    def commit_due(self, now: float | None = None) -> list[dict]:
        """Write each choice whose undo window has passed; one result each.
        A choice is marked as being written while it is, and leaves the store
        after: one a crash cut short is written again by the next tick, and
        the item read again first finds a row already closed and leaves it."""
        now = self.home.clock() if now is None else now
        with self._lock:
            acts = self._all()
            due = {k: v for k, v in acts.items() if float(v.get("due") or 0) <= now
                   and now - float(v.get("writing") or 0) > WRITE_STALE_S}
            if not due:
                return []
            for k in due:
                acts[k]["writing"] = now
            self._save(acts)
        out = []
        for token, rec in sorted(due.items(), key=lambda kv: float(kv[1].get("at") or 0)):
            out.append(self._commit(rec))
            with self._lock:
                acts = self._all()
                acts.pop(token, None)
                self._save(acts)
        self.home.changed()
        return out

    def _commit(self, rec: dict) -> dict:
        op, lid, ref = rec.get("op"), str(rec.get("lid") or ""), str(rec.get("ref") or "")
        try:
            src = self.home.sources()
            f = find(src, ref)
        except Exception as e:
            log.warning("home: %s on %s not written: the item could not be read: %s", op, lid, e)
            return {"op": op, "id": lid, "ok": False, "why": "unread"}
        if f is None or not _open_row(f) or f.lid != lid:
            log.info("home: %s on %s not written: it left Home before the write", op, lid)
            return {"op": op, "id": lid, "ok": False, "why": "gone"}
        if version(f) != rec.get("version"):
            log.info("home: %s on %s not written: it changed before the write", op, lid)
            return {"op": op, "id": lid, "ok": False, "why": "changed"}
        today = _today(src).isoformat()
        wts = _wts()
        try:
            if op == "done":
                proposal = isinstance((f.row or {}).get("close_proposal"), dict)
                done = wts.run_writeback({"ops": [("confirm" if proposal else "close", lid)]})
                ok = any(d.get("ok") for d in done)
            elif op == "not_needed":
                hooks = getattr(self.home, "hooks", None) or Hooks()
                if hooks.not_needed is None:
                    log.warning("home: not needed on %s not written: no bot to run it", lid)
                    return {"op": op, "id": lid, "ok": False, "why": "no hook"}
                got = hooks.not_needed(lid, NOT_NEEDED_NOTE) or {}
                ok = bool(got.get("ok"))
            elif op == "later":
                res = wts.RUN("ledger.py", "update", "--id", lid, "--status", "snoozed", "--snooze-until",
                              rec["day"], "--last-action", f"{today}: snoozed until {rec['day']} on Home")
                ok = (res.get("entry") or {}).get("status") == "snoozed"
            elif op == "waiting":
                res = wts.RUN("ledger.py", "update", "--id", lid, "--status", "waiting", "--follow-up-after",
                              rec["day"], "--last-action",
                              f"{today}: waiting on someone else, per the owner on Home")
                ok = _ledger().is_waiting(res.get("entry") or {})
            else:
                return {"op": op, "id": lid, "ok": False, "why": "unknown"}
        except Exception as e:
            log.warning("home: %s on %s failed: %s", op, lid, e)
            return {"op": op, "id": lid, "ok": False, "why": "error"}
        log.info("home: %s on %s written: %s", op, lid, "ok" if ok else "refused")
        return {"op": op, "id": lid, "ok": ok}
