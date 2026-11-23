#!/usr/bin/env python3
"""The briefing on a case: one piece of work someone asked the owner for
that ends with something delivered, not with a reply (docs/webchat.md,
Home, "The briefing on a case"). Letters are the first kind
(letters.py); the reading of a case goes through one adapter (below), so
the general case store's kinds take its place without code here.

What the owner reads when he opens a case (its Home item, its block in
Action requests):

  Now              code's, at read time: the nearest date and the days
                   left, his next free time before it
  Since you last   code's, at read time: what changed since he last
  looked           opened it, a new message from them with its gist
  What was asked   code's: the kind, for whom, when, by whom; his answer;
                   their last message
  The places       code's: each part's date, state and how it is
                   delivered, in one list
  What came in,    code's: the materials by kind (file names where the
  Still missing    level lets a reader see them), what the work still
                   needs (letters_work.materials_plan)
  What happened,   the model's, stored: how the work got here, what is
  What is at       at stake by the nearest date, what the records do not
  stake, What      show; and two or three next steps
  Tacitus does
  not know

Facts are code's, read when the card is read, so they are never stale;
the model writes only what needs judgment. Its next steps are chosen
from the moves the case allows now (allowed_moves) and checked in code
(check_step); each one runs the same path as the matching button on the
case's card (the kind's answer: letter_surface.answer through the bot's
Hooks.letter), never a second one:

  draft            draft the work (Draft the letter, Change the letter)
  ask_materials    a short ask for what is missing (the owner confirms;
                   the draft waits for Send)
  set_due          the due date of a part with none: a date the records
                   state, else the owner picks it
  mark_delivered   the owner's word that a part is delivered (confirms;
                   only for a part whose work exists)
  open_link        the part's portal, a link (no post, nothing changes)
  decline          say no to the whole case (confirms; the reply that
                   says no waits for Send)

No step sends, submits or declines on its own: a draft waits for Send in
Action requests, the owner confirms a decline or a delivery, and the keep
gate holds: a kept case (keep_gate `local`, `facts`) gets no draft, no
ask and no conversation, and runs on the owner's own model alone.

What a model reads (build_packet): the kind; the person (from `facts`
up); the parts by number, with their names from `summary` up; dates with
their weekday and month in words; states and methods in words; the
materials by kind, with their file names from `summary` up; what is
missing; the case's events in code's words; the last messages' gists from
the case's mail records (from `summary` up; never mail text, never a
quote); the owner's rules; the moves it may choose. Never a file's
contents (the letter, a statement, a CV), never a rating or a form
answer, never a link or an address: a portal link can carry a key.

Storage and refresh, as home_deeper's row briefings: an owner-private
table beside them (<private_dir>/remote-llm/home-briefings.sqlite, 0600),
keyed by the case version (version: a digest over the case, its parts,
materials, events and its mail's latest records). The owner's own model
writes a briefing when the version moves (refresh, from the mail tick); the
hosted model rewrites the briefings of the cases on Home's list after each
briefing run (hosted_pass, in the run's detached hosted pass); a tap writes
one only when none is stored or it is stale. Kept cases run on the owner's
own model alone, and fail closed: a level that cannot be read is `local`.

    case_briefing.py packet --case <id>          the packet, read-only
    case_briefing.py refresh [--limit N] [--dry-run]
                                                 write the stale briefings
    case_briefing.py sample --case <id> ...      a dry run on the owner's own
                                                 model: the card's text and
                                                 steps, nothing stored or logged
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

log = logging.getLogger("chat.web.home.case")

PURPOSE = "chat-home-case"
LOCAL_TIMEOUT_S = 60
LOCAL_MAX_TOKENS = 900
PROMPT_MAX_CHARS = 24_000
# Writes per mail tick and per day on the owner's own model; the hosted
# rewrite's cases at most and its prompt's size per call.
PER_TICK = 2
DAILY = 40
HOSTED_CASES = 12
HOSTED_PROMPT_MAX = 80_000
# Home lists a case due this soon (letter_surface.HOME_DAYS): the day's list.
HOME_DAYS = 14
# Back after this long, a new visit: "Since you last looked" counts from
# the visit before (the app's own VISIT_MS).
VISIT_S = 30 * 60
# The furthest back "Since you last looked" reaches, and its lines at most.
SINCE_DAYS = 14
SINCE_MAX = 5
STEP_DAYS = 366
LABEL_MAX = 60

NOTE_STALE_STEP = "That step is out of date. Open the item again."
NOTE_STEP_GONE = "That step is not open for this item now."
NOTE_NO_CASE = "Tacitus could not read this case now. Open it again in a minute."


def _mod(name: str):
    """A sibling module, imported as itself or under `preprocessor.`."""
    import importlib
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return importlib.import_module(f"preprocessor.{name}")


def _hd():
    return _mod("home_deeper")


def kept(level: Any) -> bool:
    return level in ("local", "facts")


def _open_level(level: Any) -> bool:
    return level in ("summary", "full")


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _short(d: date) -> str:
    """"Tue, Dec 1": how a card line says a day."""
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"


def _one(text: Any, cap: int) -> str:
    return _hd()._one(text, cap)


def _zone() -> ZoneInfo:
    return _hd()._zone()


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ── the kinds ─────────────────────────────────────────────────────────
# A kind's declaration: its words for the owner, its states, and the moves
# a briefing step may take, each the answer its card's button runs
# (`answer`; `new`: the answer while the owner has not said yes). Home's
# choice id for a move is `<choice>:<answer>[:<part id>]`. When the general
# case store lands, kind() reads its declaration (cases.kind) instead.

LETTER = {
    "name": "letter",
    "choice": "letter",
    "words": {
        "work": "the letter", "part": "place", "parts": "places", "kept": "A kept letter case",
        "what": {"recommendation": "a recommendation letter", "reference": "a reference letter",
                 "nomination": "a nomination letter", "support": "a letter of support",
                 "character": "a character letter", "evaluation": "an evaluation letter"},
        "default": "a letter",
        "states": {"asked": "not answered yet", "accepted": "to write", "drafted": "drafted",
                   "ready": "ready to submit", "submitted": "submitted", "declined": "declined"},
        "methods": {"portal": "portal link", "email": "by email", "upload": "you upload it",
                    "unknown": "how to submit is not known"},
        "materials": {"cv": "a CV", "statement": "a statement", "list": "a list of places",
                      "draft": "points for the letter", "transcript": "a transcript", "sample": "a paper",
                      "description": "a description", "form": "a form", "other": "another file"},
        "labels": {"draft": "Draft the letter", "redraft": "Change the letter",
                   "ask_materials": "Ask for what is missing", "set_due": "Set the due date",
                   "mark_delivered": "Mark it submitted", "open_link": "Open the portal",
                   "decline": "Say no to this letter"},
        "confirm": {"ask_materials": "Tacitus drafts a short ask for {asks}. It waits for your Send.",
                    "mark_delivered": "Is the letter for {part} submitted?",
                    "decline": "Say no to this letter? Its places close. A reply that says no waits "
                               "for your Send."},
        "delivered": "submitted",
        "state_events": {"submitted": "{part}: marked submitted.", "ready": "{part}: ready to submit."},
    },
    "open_states": ("asked", "accepted", "drafted", "ready"),
    # a part whose work exists may be marked delivered
    "work_states": ("drafted", "ready"),
    "moves": {"draft": {"answer": "draft"}, "ask_materials": {"answer": "yes_ask"},
              "set_due": {"answer": "date", "part": True}, "mark_delivered": {"answer": "submitted", "part": True},
              "open_link": {"part": True}, "decline": {"answer": "decline", "new": "no"}},
}
KINDS = {"letter": LETTER}
STEP_KINDS = ("draft", "ask_materials", "set_due", "mark_delivered", "open_link", "decline")


def kind(name: str) -> dict | None:
    """The kind's declaration (the adapter's: cases.kind when it lands)."""
    return KINDS.get(str(name or ""))


# ── the adapter: reading a case ───────────────────────────────────────
# Every read of the store is here, read-only. A case comes back as the
# letters store gives it; view() turns it into the shape the briefing
# reads, at a keep level. Switching to the general store changes these
# functions alone.

def _connect():
    """The ledger database, read-only, rows by name."""
    return _mod("letter_surface")._connect()


def load(conn=None, *, open_only: bool = True) -> list[dict]:
    """The cases ([] without the store or on a read failure)."""
    return _mod("letter_surface").load_cases(conn, open_only=open_only)


def case_kind(case: dict) -> str:
    """The case's kind (a letter case's `kind` names the letter's sort)."""
    return "letter"


def events(conn, case_id: str) -> list[dict]:
    """The case's history, oldest first: {seq, ts, part, kind, state, evidence}."""
    try:
        got = conn.execute("SELECT seq, ts, dest_id, kind, state, actor, evidence FROM letter_events "
                           "WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
    except Exception:
        return []
    out = []
    for r in got:
        try:
            ev = json.loads(r["evidence"]) if str(r["evidence"] or "").startswith("{") else {}
        except ValueError:
            ev = {}
        out.append({"seq": r["seq"], "ts": r["ts"], "part": r["dest_id"] or "", "kind": r["kind"],
                    "state": r["state"] or "", "actor": r["actor"] or "", "evidence": ev})
    return out


def _row_ids(case: dict) -> list[str]:
    ids = [str(d.get("ledger_id")) for d in case.get("destinations") or [] if d.get("ledger_id")]
    if case.get("case_ledger_id"):
        ids.append(str(case["case_ledger_id"]))
    return list(dict.fromkeys(ids))


def rows(conn, case: dict) -> dict[str, dict]:
    """{ledger id: row} for the case's rows, open or closed, in the merged
    shape (the metadata's keys beside the row's)."""
    ids = _row_ids(case)
    if not ids:
        return {}
    try:
        got = conn.execute(f"SELECT * FROM ledger WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except Exception:
        return {}
    out = {}
    for r in got:
        d = {k: r[k] for k in r.keys() if k != "metadata" and r[k] is not None}
        try:
            meta = json.loads(r["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if isinstance(meta, dict):
            d.update(meta)
        out[str(d.get("id"))] = d
    return out


def levels(loaded: list[dict], *, records=None) -> None:
    """Each loaded case's keep level, in place: the strictest of its own and
    its rows' (keep_gate, one read of the mail's records for all); `local`
    when the check fails."""
    kg = _mod("keep_gate")
    pool = [r for x in loaded for r in x["rows"].values()]
    try:
        found = kg.levels(pool, records=records) if pool else {}
    except Exception:
        found = {r.get("id"): "local" for r in pool}
    for x in loaded:
        mine = [found.get(i, "local") for i in x["rows"]]
        x["level"] = kg.stricter(*([x["case"].get("keep_level") or "full"] + mine))


_THREAD_KEYS = ("latest", "message_count", "last_inbound", "last_from_user")


def version(case: dict, rows_: dict, evs: list[dict]) -> str:
    """The case version a briefing is written from (cases.version when the
    general store lands): its own fields, its parts, its materials, its
    open questions, its event count and its mail's latest records (a new
    message in one of its threads moves it); never anything that moves
    with the clock."""
    dests = sorted([str(d.get(k) or "") for k in ("id", "name", "due", "method", "link", "state", "ledger_id",
                                                   "draft", "submitted_at")]
                   for d in case.get("destinations") or [])
    brief = {lid: [(_brief(r).get(k)) for k in _THREAD_KEYS] for lid, r in sorted(rows_.items())}
    key = {"case": [case.get(k) for k in ("kind", "status", "decision", "base_letter", "keep_level", "person_name",
                                           "person_email", "case_ledger_id", "notes")],
           "dests": dests, "materials": sorted([str(m.get("kind")), str(m.get("ref"))]
                                               for m in case.get("materials") or [] if isinstance(m, dict)),
           "questions": sorted(str(q.get("id")) for q in case.get("questions") or [] if isinstance(q, dict)),
           "events": [len(evs), evs[-1]["seq"] if evs else 0], "threads": brief}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


def loaded(case: dict, conn=None) -> dict:
    """{case, rows, events, version} for one case, read-only (level later)."""
    own = conn is None
    conn = conn or _connect()
    try:
        r = rows(conn, case)
        evs = events(conn, str(case.get("id") or ""))
    finally:
        if own:
            conn.close()
    return {"case": case, "rows": r, "events": evs, "version": version(case, r, evs), "level": "local"}


def load_all(*, open_only: bool = True, records=None, conn=None) -> list[dict]:
    """Every open case, loaded, with its level."""
    own = conn is None
    conn = conn or _connect()
    try:
        out = [loaded(c, conn) for c in load(conn, open_only=open_only)]
    finally:
        if own:
            conn.close()
    levels(out, records=records)
    return out


def load_one(case_id: str = "", *, case: dict | None = None, records=None) -> dict | None:
    """One case, loaded, with its level; None when the store has none. A
    case handed in whose rows cannot be read is shown from itself alone,
    at `local` (fails closed)."""
    try:
        conn = _connect()
    except Exception:
        if case is None:
            raise
        return {"case": case, "rows": {}, "events": [], "version": version(case, {}, []), "level": "local"}
    try:
        if case is None:
            case = next((c for c in load(conn, open_only=False) if str(c.get("id")) == str(case_id)), None)
        if case is None:
            return None
        x = loaded(case, conn)
    finally:
        conn.close()
    levels([x], records=records)
    return x


# ── the case as the briefing reads it ─────────────────────────────────

def _brief(row: dict) -> dict:
    b = (row or {}).get("thread_brief")
    return b if isinstance(b, dict) else {}


_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_LINK = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


def _scrub(text: str) -> str:
    """A record's words with no address and no link in them (a portal link
    can carry a key)."""
    return _LINK.sub("a link", _ADDRESS.sub("an address", " ".join(str(text or "").split())))


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", str(name or "").lower()) if len(t) >= 3}


def _who(sender: str, case: dict) -> str:
    """"them" for the person's own mail (their address or a part of their
    name in the sender), else "service" (a portal, an office)."""
    s = str(sender or "").lower()
    email = str(case.get("person_email") or "").lower()
    if email and email in s:
        return "them"
    names = _tokens(case.get("person_name") or "")
    return "them" if names and names & _tokens(s) else "service"


def messages(case: dict, rows_: dict) -> list[dict]:
    """The case's mail from its rows' records, oldest first, one per message:
    {at, own, who, gist, files, part}. Never the mail's text or a quote."""
    part_of = {str(d.get("ledger_id")): str(d.get("id")) for d in case.get("destinations") or [] if d.get("ledger_id")}
    seen, out = set(), []
    for lid, row in rows_.items():
        for m in _brief(row).get("messages") or []:
            if not isinstance(m, dict):
                continue
            key = m.get("id") or (m.get("received"), m.get("gist"))
            if key in seen:
                continue
            seen.add(key)
            own = bool(m.get("own"))
            files = [str(a) for a in m.get("attachments") or [] if isinstance(a, str)]
            out.append({"at": str(m.get("received") or "")[:19], "own": own,
                        "who": "owner" if own else _who(m.get("from"), case),
                        "gist": _scrub(m.get("gist")), "files": [_scrub(f) for f in files],
                        "part": part_of.get(lid, "")})
    return sorted(out, key=lambda m: m["at"])


def last_inbound(case: dict, rows_: dict, level: str) -> dict | None:
    """The newest message not from the owner in the case's mail: {"at",
    "who" ("them" or "service"), "gist" ("" below `summary`), "part"} or
    None. The same message the briefing line's `news` names (work-cases
    contract), with its gist."""
    got = [m for m in messages(case, rows_) if not m["own"] and m["at"]]
    if not got:
        return None
    m = got[-1]
    return {"at": m["at"], "who": m["who"], "gist": _one(m["gist"], 200) if _open_level(level) else "",
            "part": m["part"]}


def view(x: dict, *, today: date) -> dict:
    """The case as the briefing reads it, at its level (x["level"]): what a
    reader at that level may see and nothing more. `local`: no names, places
    by number, materials by kind, no gists; `facts`: the person too;
    `summary`, `full`: place and file names and the mail's gists (`full`
    also the case's notes). Links stay in `link` for code's own use and
    never go into a packet."""
    case, rows_, level = x["case"], x["rows"], x["level"]
    decl = kind(case_kind(case)) or LETTER
    words = decl["words"]
    lw = _mod("letters_work")
    msgs = messages(case, rows_)
    open_ = _open_level(level)
    parts = []
    for i, d in enumerate(case.get("destinations") or [], 1):
        link = str(d.get("link") or "")
        named = _one(d.get("name"), 80) if open_ else ""
        parts.append({"ref": f"p{i}", "id": str(d.get("id") or ""),
                      "name": named or f"{words['part'][0].upper() + words['part'][1:]} {i}",
                      "due": str(d.get("due") or "")[:10], "state": d.get("state") or "asked",
                      "method": d.get("method") or "unknown", "submitted_at": str(d.get("submitted_at") or "")[:10],
                      "link": link if open_ and re.match(r"^https://[^\s<>|]+$", link) else ""})
    files = [f for m in msgs if not m["own"] for f in m["files"]]
    try:
        plan = lw.materials_plan(case, extra_present=files)
    except Exception:
        plan = {"present": {}, "missing": [], "asks": []}
    arrived = []
    for k, names in (plan.get("present") or {}).items():
        word = words["materials"].get(k, words["materials"]["other"])
        names = list(dict.fromkeys(names))
        arrived.append({"kind": k, "word": word, "names": [_one(n, 60) for n in names] if open_ else [],
                        "count": len(names)})
    opn = [p for p in parts if p["state"] in decl["open_states"]]
    drafted = bool(case.get("base_letter")) or any(p["state"] in decl["work_states"] for p in opn)
    missing, asks = [], []
    if not drafted and opn:
        missing.append(words["work"])
    for k in plan.get("missing") or []:
        if k != "list":
            missing.append(words["materials"].get(k, k))
            asks.append(words["materials"].get(k, k))
    undated = [p for p in opn if not p["due"]]
    no_method = [p for p in opn if p["method"] in ("", "unknown")]
    if undated:
        missing.append(f"the due date for {_n(len(undated), words['part'])}")
    if no_method:
        missing.append(f"how to submit to {_n(len(no_method), words['part'])}")
    if not parts and "list" in (plan.get("missing") or []):
        asks.append(words["materials"]["list"])
    elif undated or no_method:
        what = _join((["the due date"] if undated else []) + (["how to submit"] if no_method else []))
        asks.append(f"{what} for {_n(len({p['ref'] for p in undated + no_method}), words['part'])}")
    who = _one(_scrub(case.get("person_name")), 60) if level != "local" else ""  # a name, never an address
    asked_on = min([d for d in [_date(case.get("opened"))] + [_date(m["at"]) for m in msgs if not m["own"]] if d],
                   default=None)
    new = not case.get("decision") and all((p["state"] or "asked") == "asked" for p in parts)
    li = last_inbound(case, rows_, level)
    out = {"id": str(case.get("id") or ""), "kind": decl["name"], "decl": decl, "level": level,
           "what": words["what"].get(str(case.get("kind") or "").lower(), words["default"]),
           "who": who, "asked_on": asked_on.isoformat() if asked_on else "",
           "asked_by": "them" if case.get("case_ledger_id") else "service",
           "status": case.get("status") or "open", "decision": case.get("decision") or "", "new": new,
           "opened": str(case.get("opened") or ""),
           "parts": parts, "arrived": arrived, "missing": missing, "asks": asks,
           "drafted": drafted, "can_write": bool(case.get("person_email")) and not kept(level),
           "messages": [dict(m, gist=m["gist"] if open_ else "", files=m["files"] if open_ else [])
                        for m in msgs],
           "last_inbound": li, "notes": _one(_scrub(case.get("notes")), 200) if level == "full" else "",
           "questions": len([q for q in case.get("questions") or [] if isinstance(q, dict)]),
           "events": x["events"]}
    return out


def _stop(text: str) -> str:
    """A sentence ending in one stop (a line cut short keeps its "…")."""
    t = str(text or "").strip()
    return t if t.endswith(("…", ".", "?", "!")) else t + "."


def _n(count: int, one: str, many: str = "") -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


# ── which moves a case allows ─────────────────────────────────────────

def allowed_moves(v: dict) -> list[dict]:
    """[{"kind", "part"?}]: the moves the case allows now, in its kind's
    words. A closed case allows none. Draft only once the owner said yes,
    and never on a kept case (its letter stays on his own model); the ask
    for materials only when something is missing and there is an address
    to write to; a date only for an open part with none (a date it has
    moves on the card's own button); delivered only for an open part whose
    work exists; its portal only where it has a link and the level shows
    it; decline while anything is open."""
    if v["status"] != "open":
        return []
    decl = v["decl"]
    opn = [p for p in v["parts"] if p["state"] in decl["open_states"]]
    out: list[dict] = []
    if opn and not v["new"] and not kept(v["level"]):
        out.append({"kind": "draft"})
    if v["can_write"] and v["asks"]:
        out.append({"kind": "ask_materials"})
    for p in opn:
        if not p["due"]:  # a date it has moves on the card's own button
            out.append({"kind": "set_due", "part": p["ref"]})
        if p["state"] in decl["work_states"]:
            out.append({"kind": "mark_delivered", "part": p["ref"]})
        if p["link"]:
            out.append({"kind": "open_link", "part": p["ref"]})
    if opn or v["new"]:
        out.append({"kind": "decline"})
    return out


def _move_key(m: dict) -> str:
    return f"{m['kind']} {m['part']}" if m.get("part") else m["kind"]


# ── the code-built lines ──────────────────────────────────────────────

def _due_words(d: date, today: date) -> str:
    left = (d - today).days
    if left == 0:
        return "today"
    if left == 1:
        return "tomorrow"
    if left > 1:
        return f"{_short(d)}, in {left} days"
    return f"{_short(d)}, {-left} day{'s' if left != -1 else ''} ago"


def now_lines(v: dict, *, today: date, now: float, zone: ZoneInfo, briefing: dict | None = None) -> list[str]:
    """The lines that depend on the moment, never stored: the nearest open
    date and the days left (an overdue one first), his next free time
    before it."""
    decl = v["decl"]
    opn = [p for p in v["parts"] if p["state"] in decl["open_states"] and _date(p["due"])]
    if not opn:
        return []
    late = [p for p in opn if _date(p["due"]) < today]
    ahead = sorted([p for p in opn if _date(p["due"]) >= today], key=lambda p: p["due"])
    out = []
    if late:
        first = min(late, key=lambda p: p["due"])
        out.append(f"{first['name']} was due {_due_words(_date(first['due']), today)}"
                   + (f", and {len(late) - 1} more" if len(late) > 1 else "") + ".")
    if ahead:
        d = _date(ahead[0]["due"])
        same = [p["name"] for p in ahead if p["due"] == ahead[0]["due"]]
        out.append(f"Next due {_due_words(d, today)}: {_join(same[:3])}"
                   + (f" and {len(same) - 3} more" if len(same) > 3 else "") + ".")
        try:
            free = _hd()._calendar(briefing or {}, now=now, zone=zone, until=d)["free_slots"]
        except Exception:
            free = []
        if free:
            out.append(f"Your next free time before then: {free[0]}.")
    return out


EVENT_WORDS = {"accept": "You said yes.", "decline": "You said no.", "draft": "{work} was drafted.",
               "merge": "Two requests became this one.", "reopen": "It opened again."}
# A part's new state in words: the kind's own (`state_events`), else these.
STATE_EVENT = {"declined": "{part}: declined.", "drafted": "{part}: drafted."}
MAIL_EVENT = {"request": "A new request came in{for_part}.", "reminder": "A reminder came in{for_part}.",
              "confirmation": "{part} confirmed it got it."}


def _ts(value: Any) -> float | None:
    """Unix seconds of an ISO time; one with no zone is UTC (the store's)."""
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def event_line(e: dict, v: dict) -> str:
    """One event in code's words, at the case's level; "" for one that says
    nothing to the owner."""
    words = v["decl"]["words"]
    by_id = {p["id"]: p for p in v["parts"]}
    part = by_id.get(e.get("part") or "")
    name = part["name"] if part else ""
    k = e.get("kind")
    if k in EVENT_WORDS:
        return EVENT_WORDS[k].format(work=words["work"][0].upper() + words["work"][1:])
    said = {**STATE_EVENT, **words.get("state_events", {})}
    if k == "state" and e.get("state") in said and name:
        return said[e["state"]].format(part=name)
    if k == "due" and name:
        to, was = _date(e.get("state")), _date((e.get("evidence") or {}).get("from"))
        if to and was:
            return f"{name}: due date moved from {_short(was)} to {_short(to)}."
        return f"{name}: due date set to {_short(to)}." if to else f"{name}: due date taken off."
    if k == "material":
        kind_ = str((e.get("evidence") or {}).get("kind") or "other")
        word = words["materials"].get(kind_, words["materials"]["other"])
        return f"{word[0].upper() + word[1:]} came in."
    if k == "destination" and name:
        return f"A new {words['part']}: {name}."
    if k == "mail":
        role = str((e.get("evidence") or {}).get("role") or "")
        if role in MAIL_EVENT and (name or role != "confirmation"):
            return MAIL_EVENT[role].format(part=name, for_part=f" for {name}" if name else "")
    return ""


def since_lines(v: dict, since: float | None, *, now: float) -> list[str]:
    """What changed after `since` (the owner's last look): the case's events
    in code's words and each new message not from the owner, with its gist
    where the level shows it. Never more than SINCE_DAYS back."""
    if since is None:
        return []
    floor = max(since, now - SINCE_DAYS * 86400)
    out: list[str] = []
    for m in v["messages"]:
        t = _ts(m["at"])
        if m["own"] or t is None or t <= floor:
            continue
        d = _date(m["at"])
        who = ("them" if not v["who"] else v["who"]) if m["who"] == "them" else "a service"
        head = f"A new message from {who} on {_short(d)}" if d else f"A new message from {who}"
        out.append(f"{head}: {_stop(_one(m['gist'], 200))}" if m["gist"] else f"{head}.")
    for e in v["events"]:
        t = _ts(e.get("ts"))
        if t is None or t <= floor or e.get("kind") == "mail" and (e.get("evidence") or {}).get("role") != "confirmation":
            continue  # a request or a reminder is its message above
        line = event_line(e, v)
        if line and line not in out:
            out.append(line)
    return out[-SINCE_MAX:]


def ask_lines(v: dict, *, today: date) -> list[str]:
    """What was asked, for whom, when and by whom; his answer; their last
    message (its gist where the level shows it)."""
    who = v["who"]
    what = v["what"][0].upper() + v["what"][1:]
    d = _date(v["asked_on"])
    out = [f"{what}" + (f" for {who}" if who else "") + (f", asked {_short(d)}" if d else "") + "."]
    if v["asked_by"] == "them":
        out.append(f"{who or 'They'} asked you directly.")
    else:
        out.append(f"Each {v['decl']['words']['part']} asked by its own mail.")
    out.append("You have not answered yet." if v["new"] else "You said yes." if v["decision"] == "accepted"
               else "You said no." if v["decision"] == "declined" else "")
    li = v.get("last_inbound")
    if li and li.get("who") == "them" and _date(li["at"]):
        when = _short(_date(li["at"]))
        out.append(f"Their last message, {when}: {_stop(li['gist'])}" if li.get("gist")
                   else f"Their last message came {when}.")
    return [x for x in out if x]


def part_lines(v: dict, *, today: date) -> list[str]:
    """Each part's date, state and how it is delivered, open ones first;
    the finished ones in one line."""
    words = v["decl"]["words"]
    opn = [p for p in v["parts"] if p["state"] in v["decl"]["open_states"]]
    opn.sort(key=lambda p: p["due"] or "9999")
    out = []
    for p in opn[:9]:
        d = _date(p["due"])
        when = (f"was due {_short(d)}" if d < today else f"due {_short(d)}") if d else "no due date"
        how = words["methods"].get(p["method"], "") if _open_level(v["level"]) or p["method"] != "unknown" else ""
        out.append(", ".join(x for x in (f"{p['name']}: {when}", words["states"].get(p["state"], p["state"]), how) if x))
    if len(opn) > 9:
        out.append(f"And {len(opn) - 9} more.")
    done = [p for p in v["parts"] if p["state"] == "submitted"]
    gone = [p for p in v["parts"] if p["state"] == "declined"]
    if done:
        out.append(f"{words['delivered'][0].upper() + words['delivered'][1:]}: {_join([p['name'] for p in done][:6])}"
                   + (f" and {len(done) - 6} more" if len(done) > 6 else "") + ".")
    if gone:
        out.append(f"Declined: {_join([p['name'] for p in gone][:6])}.")
    return out


def material_lines(v: dict) -> tuple[list[str], list[str]]:
    """(what came in, what is still missing)."""
    came = []
    for a in v["arrived"]:
        word = a["word"]
        came.append(f"{word} ({_join(a['names'][:3])})" if a["names"] else
                    (f"{word}" if a["count"] == 1 else f"{word}: {a['count']} files"))
    line = _join(came)
    came_lines = [line[0].upper() + line[1:] + "."] if came else ["Nothing yet."]
    missing = [m[0].upper() + m[1:] + "." for m in v["missing"]]
    return came_lines, missing


def code_sections(v: dict, *, today: date, now: float, zone: ZoneInfo, since: float | None,
                  briefing: dict | None = None, places: bool = True) -> list[tuple[str, list[str]]]:
    """The card's code-built sections, in their order; `places` False for a
    block whose card lists the places and what is missing already."""
    words = v["decl"]["words"]
    out = [("Now", now_lines(v, today=today, now=now, zone=zone, briefing=briefing)),
           ("Since you last looked", since_lines(v, since, now=now)),
           ("What was asked", ask_lines(v, today=today))]
    if places:
        came, missing = material_lines(v)
        out += [(f"The {words['parts']}", part_lines(v, today=today)),
                ("What came in", came), ("Still missing", missing)]
    return [(h, lines) for h, lines in out if lines]


def render(sections: list[tuple[str, list[str]]]) -> str:
    return "\n\n".join(h + "\n" + "\n".join(f"- {x}" for x in lines) for h, lines in sections if lines)


# ── the packet ────────────────────────────────────────────────────────

def _with_words(value: Any) -> str:
    return _hd()._with_words(value)


def build_packet(v: dict, *, today: date, rules: str = "", allowed=(), shed: int = 0) -> dict:
    """The records a model reads for one case (module doc), at its level."""
    words = v["decl"]["words"]
    n_msgs, n_events = ((6, 10), (4, 6), (2, 4))[min(shed, 2)]
    out: dict = {"written_on": f"{today.isoformat()} ({_hd().day_words(today)})",
                 "case": {"what": f"{v['what']} the owner was asked to write",
                          "for": v["who"] or "a person kept on his own model",
                          "asked_on": _with_words(v["asked_on"]) if v["asked_on"] else "",
                          "asked_by": "the person" if v["asked_by"] == "them" else "each place by its own mail",
                          "his_answer": "not answered yet" if v["new"] else
                          {"accepted": "yes", "declined": "no"}.get(v["decision"], ""),
                          "work": f"{words['work']}: " + ("drafted" if v["drafted"] else "not drafted yet")}}
    if v["notes"]:
        out["case"]["notes"] = v["notes"]
    out[words["parts"]] = [{"part": p["ref"], "name": p["name"],
                            "due": _with_words(p["due"]) if p["due"] else "no due date",
                            "state": words["states"].get(p["state"], p["state"]),
                            "how": words["methods"].get(p["method"], p["method"]),
                            **({"submitted_on": _with_words(p["submitted_at"])} if p["submitted_at"] else {})}
                           for p in v["parts"]]
    out["came_in"] = [{"kind": a["kind"], **({"files": a["names"][:4]} if a["names"] else {"count": a["count"]})}
                      for a in v["arrived"]]
    out["still_missing"] = list(v["missing"])
    hist = []
    opened = _ts(v["opened"])
    for e in v["events"]:
        t = _ts(e.get("ts"))
        if e.get("kind") in ("open", "destination", "accept", "decline") and opened and t and abs(t - opened) < 120:
            continue  # the case's own start, restated (its mail carries the real dates; his answer is above)
        line = event_line(e, v)
        if line:
            hist.append({"at": _with_words(str(e.get("ts") or "")[:16]), "what": line})
    if hist:
        out["history"] = hist[-n_events:]
    msgs = [m for m in v["messages"] if m["at"]]
    if msgs:
        out["mail"] = [{"at": _with_words(m["at"][:16]), "from": {"owner": "the owner", "them": "the person"}.get(
            m["who"], "a service"), **({"gist": _one(m["gist"], 200)} if m["gist"] else {}),
            **({"files": [_one(f, 60) for f in m["files"][:4]]} if m["files"] else {})} for m in msgs[-n_msgs:]]
    if rules:
        out["owner_rules"] = rules[:1200 if shed else 2000]
    out["moves_you_may_choose"] = [_move_key(m) for m in allowed]
    return out


def fit_packet(build: Callable[[int], dict]) -> dict:
    for shed in range(3):
        p = build(shed)
        if len(json.dumps(p, ensure_ascii=False)) <= PROMPT_MAX_CHARS:
            return p
    p.pop("owner_rules", None)
    while len(json.dumps(p, ensure_ascii=False)) > PROMPT_MAX_CHARS and p.get("history"):
        p["history"].pop(0)
    return p


PROMPT = """You brief the owner of an assistant on one piece of work someone asked him for: {what}. He reads it when he opens it on the assistant's Home screen, maybe days from now. The records below are the assistant's own: the case, its {parts} with their dates and states, what came in, what is still missing, its history and summaries of its mail. Never the mail itself, and never the files.

The card already lists the {parts}, their dates and states, what came in and what is still missing. Do not list them again. Write three short lists:
- history: how the work got where it is, oldest first, 2 to 4 short sentences.
- stakes: what is at stake by the nearest date, 1 to 3 short sentences.
- unknown: what the records do not show that matters, 0 to 3 short sentences.

Then choose 2 or 3 next steps, each one of `moves_you_may_choose` exactly: its `kind`, and its `part` (p1, p2 ...) where the move names one, else "".
- draft: the assistant drafts {work} from the materials, or changes the draft.
- ask_materials: the assistant drafts a short ask for what is still missing. He confirms it first.
- set_due: set a {part}'s due date. Give `date` (YYYY-MM-DD) only when the records state that date; else leave it "" and he picks it.
- mark_delivered: he says {work} for that {part} went in. Only when the records suggest it did.
- open_link: open that {part}'s portal to submit {work}.
- decline: he says no to the whole case. Only when the records show he cannot or will not do it.
`why` is a few words on why the step fits.

Rules:
- Use only the records. Never invent a fact, a name, a date or a number.
- His own words (`owner_rules`, `his_answer`) outrank the assistant's notes.
- Speak to him as "you"; never call him "the owner".
- Write no relative time: never today, tomorrow, yesterday, this week, next week, ago, or a count of days. Write a date in words as the records give it (November 11), never as digits. When he opens it, the card adds the days left and his free time.
- Short plain sentences of 20 words or fewer. No dash between clauses. No markdown, no links.
- Leave a field you do not use as "".

Answer with one JSON object and nothing else:
{{"history": [], "stakes": [], "unknown": [], "steps": [{{"kind": "", "part": "", "date": "", "why": ""}}]}}

The records:
"""

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "history": {"type": "array", "items": {"type": "string"}},
    "stakes": {"type": "array", "items": {"type": "string"}},
    "unknown": {"type": "array", "items": {"type": "string"}},
    "steps": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"kind": {"type": "string", "enum": list(STEP_KINDS)}, "part": {"type": "string"},
                       "date": {"type": "string"}, "why": {"type": "string"}},
        "required": ["kind", "part", "date", "why"]}}},
    "required": ["history", "stakes", "unknown", "steps"]}


def _instructions(v: dict) -> str:
    w = v["decl"]["words"]
    return PROMPT.format(what=v["what"], parts=w["parts"], part=w["part"], work=w["work"])


def prompt_text(v: dict, packet: dict) -> str:
    return _instructions(v) + json.dumps(packet, ensure_ascii=False)


# ── code's check of the answer ────────────────────────────────────────

def check_step(raw: dict, *, allowed: list[dict], known, today: date) -> tuple[dict | None, str]:
    """(the step, '') when it passes, (None, why) when code refuses it: a
    kind or a part the case does not allow now, a date that is not after
    today's or that the records do not state."""
    k = str((raw or {}).get("kind") or "").strip()
    if k not in STEP_KINDS:
        return None, "unknown kind"
    part = str(raw.get("part") or "").strip().lower()
    wants_part = any(m.get("part") for m in allowed if m["kind"] == k)
    hit = next((m for m in allowed if m["kind"] == k and (m.get("part") or "") == (part if wants_part else "")),
               None)
    if hit is None:
        return None, f"{k} {part}".strip() + " is not open for this case"
    p: dict = {}
    if hit.get("part"):
        p["part"] = hit["part"]
    said = str(raw.get("date") or "").strip()[:10] if k == "set_due" else ""
    if said:
        d = _date(raw.get("date"))
        if not d or d < today or d > today + timedelta(days=STEP_DAYS):
            return None, "date out of range"
        month_day = f"{d.strftime('%B').lower()} {d.day}"
        if d.isoformat() not in known.text and not re.search(rf"\b{month_day}\b", known.text):
            return None, "a date the records do not state"
        p["date"] = d.isoformat()
    return {"kind": k, "params": p}, ""


def check_answer(obj: dict, packet: dict, *, allowed: list[dict], today: date) -> dict:
    """The answer checked: {"text", "steps", "unchecked", "rejected", "ok"},
    the words as home_deeper checks a row's (a number, a day, a month or a
    name the packet does not hold reads "Tacitus could not check this.")."""
    D = _hd()
    known = D.Known(packet)
    sections: dict[str, list[str]] = {}
    unchecked = shown = 0
    for key, _head, cap in D.SECTIONS:
        lines: list[str] = []
        for raw in (obj.get(key) if isinstance(obj.get(key), list) else [])[:cap]:
            line = D._clean_line(raw)
            if not line:
                continue
            if D.checked(line, known):
                lines.append(line)
                shown += 1
            else:
                unchecked += 1
                if not lines or lines[-1] != D.UNCHECKED:
                    lines.append(D.UNCHECKED)
        sections[key] = lines
    steps, rejected, seen = [], [], set()
    for raw in (obj.get("steps") if isinstance(obj.get("steps"), list) else [])[:5]:
        if not isinstance(raw, dict):
            continue
        step, why = check_step(raw, allowed=allowed, known=known, today=today)
        if step is None:
            rejected.append({"kind": str(raw.get("kind") or "")[:20], "why": why})
            continue
        key = (step["kind"], json.dumps(step["params"], sort_keys=True))
        if key in seen or len(steps) >= 3:
            continue
        seen.add(key)
        step["id"] = f"s{len(steps) + 1}"
        steps.append(step)
    return {"text": D.render(sections), "steps": steps, "unchecked": unchecked, "rejected": rejected,
            "ok": shown > 0}


# ── the steps as the card shows them, and what one runs ──────────────

def offered(steps: list[dict], v: dict, *, today: date) -> list[dict]:
    """The stored steps the case still allows now (its parts by id, so a
    part that closed or moved drops its steps), each as the app draws it:
    {id, label, detail?, input?, confirm?, href?} with its move."""
    allowed = {_move_key(m) for m in allowed_moves(v)}
    decl = v["decl"]
    words = decl["words"]
    by_ref = {p["ref"]: p for p in v["parts"]}
    out = []
    for s in steps or []:
        k, p = s.get("kind"), s.get("params") or {}
        if _move_key({"kind": k, "part": p.get("part")}) not in allowed:
            continue
        part = by_ref.get(p.get("part") or "")
        d = _date(p.get("date"))
        if d and d < today:
            continue
        pub: dict = {"id": s["id"], "label": words["labels"].get("redraft" if k == "draft" and v["drafted"] else k, k)}
        if part:
            pub["detail"] = _one(part["name"], LABEL_MAX)
        if k == "set_due":
            if d:
                pub["label"] = _one(f"{pub['label']}: {_short(d)}", LABEL_MAX)
            else:
                pub["input"] = "date"
        if k == "ask_materials":
            asks = _join([a for a in v["asks"]][:3]) or "what is missing"
            pub["detail"] = _one(asks, 120)
            pub["confirm"] = words["confirm"]["ask_materials"].format(asks=asks)
        if k in ("mark_delivered", "decline"):
            pub["confirm"] = words["confirm"][k].format(part=part["name"] if part else "")
        if k == "open_link":
            pub["href"] = part["link"]
        out.append({"pub": pub, "kind": k, "params": p, "part": part})
    return out


def choice_of(s: dict, v: dict, arg: str = "") -> tuple[str, str]:
    """(Home's choice id, its words) for a step: the kind's answer on the
    case's card, `<choice>:<answer>[:<part id>]`."""
    decl = v["decl"]
    move = decl["moves"][s["kind"]]
    answer = move.get("new") if v["new"] and move.get("new") else move.get("answer", "")
    cid = f"{decl['choice']}:{answer}" + (f":{s['part']['id']}" if move.get("part") and s.get("part") else "")
    if s["kind"] != "set_due":
        return cid, ""
    return cid, s["params"].get("date") or arg


# ── where it runs ─────────────────────────────────────────────────────

def route(level: str) -> list[str]:
    return _hd().route(level)


def _ask_local(prompt: str) -> dict:
    """One call to the owner's own model under SCHEMA."""
    rl = _mod("remote_llm")
    try:
        res = rl.chat([{"role": "system", "content": "You brief the owner on one piece of work. Answer in the schema."},
                       {"role": "user", "content": prompt}], purpose=PURPOSE, schema=SCHEMA,
                      schema_name="case_briefing", max_tokens=LOCAL_MAX_TOKENS, timeout=LOCAL_TIMEOUT_S,
                      slot_wait=10.0)
    except Exception as e:
        return {"ok": False, "text": "", "usage": {}, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
    u = res.get("usage") or {}
    return {"ok": True, "text": str(res.get("content") or ""), "reason": "",
            "usage": {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens")}}


def _ask_hosted(prompt: str) -> dict:
    return _hd()._ask_hosted(prompt)


def _ask_hosted_batch(prompt: str) -> dict:
    return _hd()._ask_hosted_batch(prompt)


# The calls; tests put fakes here.
LOCAL: Callable[[str], dict] = _ask_local
HOSTED: Callable[[str], dict] = _ask_hosted
HOSTED_BATCH: Callable[[str], dict] = _ask_hosted_batch


def run_model(prompt: str, order: list[str]) -> dict:
    """home_deeper.run_model on this module's calls."""
    return _hd().run_model(prompt, order, local=LOCAL, hosted_call=HOSTED)


# ── the store ─────────────────────────────────────────────────────────

class Store:
    """The stored case briefings and the owner's last looks, beside the row
    briefings (home_deeper.Store's file). Owner processes only."""

    def __init__(self, path: Path | None = None) -> None:
        self.rows = _hd().Store(path)
        self.path = self.rows.path

    def _conn(self):
        conn = self.rows._conn()
        conn.execute("CREATE TABLE IF NOT EXISTS case_briefings (case_id TEXT PRIMARY KEY, version TEXT, "
                     "by TEXT, at REAL, level TEXT, text TEXT, steps TEXT, model TEXT, note TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS case_seen (case_id TEXT PRIMARY KEY, last REAL, since REAL)")
        return conn

    def get(self, case_id: str) -> dict | None:
        if not case_id or not self.path.exists():
            return None
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM case_briefings WHERE case_id = ?", (case_id,)).fetchone()
        finally:
            conn.close()
        if not r:
            return None
        out = dict(r)
        try:
            out["steps"] = json.loads(out.get("steps") or "[]")
        except ValueError:
            out["steps"] = []
        return out

    def versions(self) -> dict[str, tuple[str, str, str]]:
        """{case id: (version, level, by)}."""
        if not self.path.exists():
            return {}
        conn = self._conn()
        try:
            return {r["case_id"]: (r["version"], r["level"], r["by"])
                    for r in conn.execute("SELECT case_id, version, level, by FROM case_briefings")}
        finally:
            conn.close()

    def put(self, case_id: str, rec: dict) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO case_briefings (case_id, version, by, at, level, text, steps, "
                             "model, note) VALUES (?,?,?,?,?,?,?,?,?)",
                             (case_id, rec.get("version") or "", rec.get("by") or "local", float(rec.get("at") or 0),
                              rec.get("level") or "", rec.get("text") or "", json.dumps(rec.get("steps") or []),
                              rec.get("model") or "", rec.get("note") or ""))
        finally:
            conn.close()

    def drop(self, open_ids: set) -> int:
        """Forget the briefings and looks of cases no longer open."""
        if not self.path.exists():
            return 0
        conn = self._conn()
        try:
            with conn:
                gone = [r["case_id"] for r in conn.execute("SELECT case_id FROM case_briefings")
                        if r["case_id"] not in open_ids]
                conn.executemany("DELETE FROM case_briefings WHERE case_id = ?", [(g,) for g in gone])
                conn.executemany("DELETE FROM case_seen WHERE case_id = ?", [(g,) for g in gone])
            return len(gone)
        finally:
            conn.close()

    def since(self, case_id: str) -> float | None:
        """The owner's last look before this visit, read-only."""
        if not self.path.exists():
            return None
        conn = self._conn()
        try:
            r = conn.execute("SELECT last, since FROM case_seen WHERE case_id = ?", (case_id,)).fetchone()
        finally:
            conn.close()
        return (r["since"] or None) if r else None

    def look(self, case_id: str, now: float) -> float | None:
        """The owner opened the case at `now`: the last look before this
        visit. A look more than VISIT_S after the one before starts a new
        visit, whose "since" is that one; a look within a visit keeps it."""
        conn = self._conn()
        try:
            with conn:
                r = conn.execute("SELECT last, since FROM case_seen WHERE case_id = ?", (case_id,)).fetchone()
                last, since = (r["last"], r["since"]) if r else (None, None)
                if last is None or now - float(last) > VISIT_S:
                    since = last
                conn.execute("INSERT OR REPLACE INTO case_seen (case_id, last, since) VALUES (?,?,?)",
                             (case_id, now, since))
            return float(since) if since else None
        finally:
            conn.close()

    def count(self, day: str, what: str, add: int = 0) -> int:
        return self.rows.count(day, what, add)


# ── one briefing ──────────────────────────────────────────────────────

def _rules() -> str:
    try:
        return _mod("preferences").render(["briefing", "drafting"])
    except Exception:
        return ""


def prepare(x: dict, *, now: float, rules: str | None = None) -> dict:
    """Everything one call needs, read-only: {"view", "allowed", "packet",
    "prompt", "level", "version"}."""
    zone = _zone()
    today = datetime.fromtimestamp(now, zone).date()
    v = view(x, today=today)
    allowed = allowed_moves(v)
    rules = _rules() if rules is None else rules
    packet = fit_packet(lambda shed: build_packet(v, today=today, rules=rules, allowed=allowed, shed=shed))
    return {"view": v, "allowed": allowed, "packet": packet, "prompt": prompt_text(v, packet),
            "level": x["level"], "version": x["version"], "today": today}


def write_one(x: dict, *, now: float, order: list[str] | None = None, store: Store | None = None,
              dry_run: bool = False, prep: dict | None = None) -> dict:
    """Write one case's briefing on the models of `order` (route(level)
    when not given) and store it with the case version, unless `dry_run`.
    {"ok", "via", "model", "calls", "note", "text", "steps", ...}."""
    t0 = time.monotonic()
    prep = prep or prepare(x, now=now)
    level = prep["level"]
    order = route(level) if order is None else order
    if kept(level):
        order = [o for o in order if o == "local"]
    out = {"ok": False, "via": "", "calls": 0, "note": "", "level": level, "unchecked": 0, "rejected": [],
           "usage": {}, "prompt_chars": len(prep["prompt"])}
    if not order:
        out["note"] = _hd().NOTE_KEPT_NO_LOCAL
        return out
    res = run_model(prep["prompt"], order)
    out.update(via=res["via"], model=res["model"], usage=res["usage"], calls=res["calls"], note=res["note"],
               tries=res["tries"])
    if res["ok"]:
        obj = _hd().parse_answer(res["text"])
        got = check_answer(obj, prep["packet"], allowed=prep["allowed"], today=prep["today"]) if obj else None
        if got and got["ok"]:
            if not dry_run:
                (store or Store()).put(prep["view"]["id"], {
                    "version": prep["version"], "by": res["via"], "at": time.time(), "level": level,
                    "text": got["text"], "steps": got["steps"], "model": res["model"], "note": res["note"]})
            out.update(ok=True, unchecked=got["unchecked"], rejected=got["rejected"], text=got["text"],
                       steps=got["steps"])
        else:
            out.update(note=_hd().NOTE_UNCHECKED, unchecked=(got or {}).get("unchecked", 0))
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def _log(rec: dict) -> None:
    try:
        _mod("remote_llm").append(_hd().LOG_NAME, rec)
    except Exception as e:
        log.debug("case briefing: the log line was not kept: %s", e)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ref(case_id: str) -> str:
    return hashlib.sha256(str(case_id).encode()).hexdigest()[:12]


def stale(cases: list[dict], versions: dict) -> list[dict]:
    """The loaded cases whose stored briefing is missing, older than the
    case or written at another level; the nearest due first."""
    out = [x for x in cases if (versions.get(str(x["case"].get("id"))) or ("", "", ""))[:2]
           != (x["version"], x["level"])]

    def due(x):
        dues = [str(d.get("due") or "") for d in x["case"].get("destinations") or []
                if d.get("due") and (d.get("state") or "asked") in LETTER["open_states"]]
        return min(dues) if dues else "9999"
    return sorted(out, key=due)


def refresh(*, limit: int = PER_TICK, daily: int | None = DAILY, store: Store | None = None,
            dry_run: bool = False, cases: list[dict] | None = None) -> dict:
    """The mail tick's step after the row briefings: write the case
    briefings that are missing or stale (a new mail joined, a state, a date
    or a material changed) on the owner's own model, at most `limit` now
    and `daily` a day; kept cases on it alone, others on the hosted model
    only when it does not answer. Nothing without a local model.
    {"open", "stale", "written", "failed", ...}; `dry_run` counts only."""
    t0 = time.monotonic()
    out = {"open": 0, "stale": 0, "written": 0, "failed": 0, "calls": 0, "skipped": "", "seconds": 0.0}
    if not _hd().local_ready():
        out["skipped"] = "no local model"
        return out
    store = store or Store()
    try:
        cases = load_all() if cases is None else cases
    except Exception as e:
        out["skipped"] = f"the cases could not be read: {str(e)[:80]}"
        return out
    out["open"] = len(cases)
    if not dry_run:
        try:
            store.drop({str(x["case"].get("id")) for x in cases})
        except Exception:
            pass
    todo = stale(cases, store.versions())
    out["stale"] = len(todo)
    if dry_run or not todo:
        out["seconds"] = round(time.monotonic() - t0, 1)
        return out
    day = _hd()._today_key()
    left = limit if daily is None else min(limit, max(0, daily - store.count(day, "case-ingest")))
    if not left:
        out["skipped"] = "the day's writes are spent"
    for x in todo[:left]:
        got = write_one(x, now=time.time(), store=store)
        out["calls"] += got.get("calls") or 0
        out["written" if got["ok"] else "failed"] += 1
        store.count(day, "case-ingest", got.get("calls") or 0)
        _log({"ts": _stamp(), "kind": "case-ingest", "ref": _ref(x["case"].get("id")), "level": x["level"],
              "ok": got["ok"], "via": got.get("via"), "seconds": got.get("seconds"), "usage": got.get("usage"),
              "unchecked": got.get("unchecked"), "rejected": got.get("rejected")})
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


# ── the hosted rewrite of the cases on Home's list ────────────────────

HOSTED_PROMPT = """You rewrite the assistant's briefings on the work cases on the owner's list today. For each case below, write the briefing as the instructions say, from that case's own records alone; never carry a fact from one case to another. Each case's `instructions` name its kind of work.

Answer with one JSON object and nothing else:
{"items": [{"ref": "<the case's ref>", "history": [], "stakes": [], "unknown": [], "steps": [{"kind": "", "part": "", "date": "", "why": ""}]}]}

The cases:
"""


def on_home(case: dict, today: date) -> bool:
    """Home lists the case today (letter_surface.home_items' rule, less its
    card): new, overdue, undated or due within HOME_DAYS."""
    ls = _mod("letter_surface")
    if not ls.is_open(case):
        return False
    f = ls.facts(case, today)
    return bool(f["new"] or f["overdue"] or f["undated"]
                or (f["next_due"] and (f["next_due"] - today).days <= HOME_DAYS))


def _hosted_item(prep: dict) -> dict:
    v = prep["view"]
    head = _instructions(v).split("Rules:")[0].split("Then choose")[0]
    return {"ref": f"case:{v['id']}", "instructions": " ".join(head.split())[:600], "records": prep["packet"]}


def hosted_pass(*, cases: list[dict] | None = None, now: float | None = None, store: Store | None = None,
                call=None, dry_run: bool = False, record: dict | None = None) -> dict:
    """The hosted model rewrites the briefings of the cases on Home's list,
    at `summary` or `full` only (kept cases keep the owner's own model's),
    in as few tool-less calls as fit HOSTED_PROMPT_MAX; each answer checked
    against its own packet and stored `by: hosted`. A failed call leaves
    the local briefing. Its record: {"cases", "kept", "calls", "stored",
    "refused", "tokens_in", "tokens_out", "cost_usd", "ok", "reason"}."""
    t0 = time.monotonic()
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now, _zone()).date()
    out = {"cases": 0, "kept": 0, "calls": 0, "stored": 0, "refused": 0, "tokens_in": 0, "tokens_out": 0,
           "cost_usd": 0.0, "ok": False, "reason": "", "seconds": 0.0}
    try:
        cases = load_all() if cases is None else cases
    except Exception as e:
        out["reason"] = f"the cases could not be read: {str(e)[:80]}"
        return out
    preps: dict[str, dict] = {}
    for x in cases:
        if not on_home(x["case"], today):
            continue
        if not _open_level(x["level"]):
            out["kept"] += 1
            continue
        if len(preps) >= HOSTED_CASES:
            break
        preps[str(x["case"].get("id"))] = prepare(x, now=now)
    out["cases"] = len(preps)
    if not preps:
        out.update(ok=True, reason="nothing to rewrite")
        return out
    items = [_hosted_item(p) for p in preps.values()]
    batches, cur = [], []
    for it in items:
        if cur and len(HOSTED_PROMPT + json.dumps(cur + [it], ensure_ascii=False)) > HOSTED_PROMPT_MAX:
            batches.append(cur)
            cur = []
        cur.append(it)
    batches.append(cur)
    if dry_run:
        out.update(ok=True, reason="dry run", batches=len(batches))
        return out
    store = store or Store()
    answered: dict[str, dict] = {}
    for b in batches:
        r = (call or HOSTED_BATCH)(HOSTED_PROMPT + json.dumps(b, ensure_ascii=False))
        out["calls"] += 0 if r.get("reason") == "limit" and not r.get("usage") else 1
        u = r.get("usage") or {}
        out["tokens_in"] += int(u.get("in") or 0)
        out["tokens_out"] += int(u.get("out") or 0)
        out["cost_usd"] += float(u.get("cost_usd") or 0)
        if not r.get("ok"):
            out["reason"] = r.get("reason") or "error"
            continue
        obj = _hd().parse_answer(r.get("text") or "")
        for a in obj.get("items") or [] if isinstance(obj.get("items"), list) else []:
            if isinstance(a, dict) and str(a.get("ref") or "").startswith("case:"):
                answered.setdefault(str(a["ref"])[5:], dict(a, model=r.get("model") or ""))
    for cid, a in answered.items():
        prep = preps.get(cid)
        if prep is None:
            continue
        got = check_answer(a, prep["packet"], allowed=prep["allowed"], today=today)
        if not got["ok"]:
            out["refused"] += 1
            continue
        store.put(cid, {"version": prep["version"], "by": "hosted", "at": time.time(), "level": prep["level"],
                        "text": got["text"], "steps": got["steps"], "model": _hd()._model_name(a.get("model") or ""),
                        "note": ""})
        out["stored"] += 1
    out["ok"] = out["stored"] > 0
    out["seconds"] = round(time.monotonic() - t0, 1)
    out["cost_usd"] = round(out["cost_usd"], 4)
    _log({"ts": _stamp(), "kind": "case-hosted-pass", **(record or {}), **out})
    return out


# ── what the card shows ───────────────────────────────────────────────

def _briefing_now() -> dict:
    try:
        return _hd()._briefing_now()
    except Exception:
        return {}


def card(x: dict, rec: dict | None, *, now: float, since: float | None, places: bool = True) -> dict:
    """The case's briefing as a card shows it: the code-built sections
    (read now), then the stored text; the stored steps the case still
    allows, and Continue at `summary` or `full`; who wrote it and when.
    {"text", "steps", "by"?, "at"?, "model"?, "note"?, "stale"?}."""
    zone = _zone()
    today = datetime.fromtimestamp(now, zone).date()
    v = view(x, today=today)
    head = render(code_sections(v, today=today, now=now, zone=zone, since=since, briefing=_briefing_now(),
                                places=places))
    out: dict = {"text": head, "steps": []}
    if rec:
        out["text"] = "\n\n".join(t for t in (head, rec.get("text") or "") if t)
        out.update(by=rec.get("by") or "local", at=rec.get("at"), model=rec.get("model") or "")
        out["steps"] = [s["pub"] for s in offered(rec.get("steps") or [], v, today=today)]
        if rec.get("note"):
            out["note"] = rec["note"]
        if (rec.get("version"), rec.get("level")) != (x["version"], x["level"]):
            out["stale"] = True
    if _open_level(x["level"]) and rec:
        out["steps"].append({"id": _hd().CONTINUE, "label": _hd().CONTINUE_LABEL})
    return out


def _case_of(case_id: str, case: dict | None):
    return load_one(case_id, case=case)


def state(fe, ref: str, item: dict | None = None, *, case: dict | None = None, case_id: str = "") -> dict:
    """The Home item's `deeper` for a case (home_items for kind `letter`),
    read at once: the stored briefing with the code-built lines first, or
    the code-built lines alone with `none` (the app then asks for the
    model's part). The open counts as the owner's look (Store.look). Never
    a model call."""
    D = _hd()
    try:
        j = D.jobs_for(fe)
        x = _case_of(case_id or str((case or {}).get("id") or ""), case)
        if x is None:
            return {"state": "unavailable", "note": NOTE_NO_CASE}
        store = Store()
        cid = str(x["case"].get("id"))
        try:
            since = store.look(cid, j.clock())
        except Exception:
            since = store.since(cid)
        rec = store.get(cid)
        got = card(x, rec, now=j.clock(), since=since)
        if rec:
            got["state"] = "working" if got.get("stale") and ref in j.running else "ready"
            return got
        job = j.job(ref)
        if kept(x["level"]) and not D.local_ready():
            return {**got, "state": "ready", "note": D.NOTE_KEPT_NO_LOCAL}
        if ref in j.running:
            return {**got, "state": "working"}
        if job.get("sig") == x["version"] and job.get("state") in ("failed", "unavailable"):
            return {**got, "state": job["state"], **({"note": job["note"]} if job.get("note") else {})}
        if j.spent():
            return {**got, "state": "ready", "note": D.NOTE_CAP.format(n=j.cap())}
        return {**got, "state": "none"}
    except Exception as e:
        log.warning("case briefing: the state of %s could not be read: %s", ref, e)
        return {"state": "unavailable", "note": NOTE_NO_CASE}


def start(fe, ref: str, item: dict | None = None, *, case: dict | None = None, case_id: str = "") -> dict:
    """POST /api/home/deeper for a case: the stored briefing as it is when it
    is current, a job at work left to finish, else one job on the owner's
    own model (and the hosted model when it does not answer, never for a
    kept case), within home_deeper's bounds. {ok, state, note?}."""
    D = _hd()
    try:
        j = D.jobs_for(fe)
        x = _case_of(case_id or str((case or {}).get("id") or ""), case)
        if x is None:
            return {"ok": False, "state": "unavailable", "note": NOTE_NO_CASE}
        cid = str(x["case"].get("id"))
        rec = Store().get(cid)
        if rec and (rec.get("version"), rec.get("level")) == (x["version"], x["level"]):
            return {"ok": True, "state": "ready"}
        if kept(x["level"]) and not D.local_ready():
            return {"ok": True, "state": "unavailable", "note": D.NOTE_KEPT_NO_LOCAL}
        with j.lock:
            if ref in j.running:
                return {"ok": True, "state": "working"}
            if j.spent():
                return {"ok": True, "state": "unavailable", "note": D.NOTE_CAP.format(n=j.cap())}
            if len(j.running) >= D.MAX_RUNNING + D.MAX_WAITING:
                return {"ok": True, "state": "failed", "note": D.NOTE_BUSY}
            j.running.add(ref)
            j._put(ref, {"ref": ref, "sig": x["version"], "state": "working", "started": j.clock()})
        j.changed(ref)

        def work() -> None:
            try:
                with j.slots:
                    got = write_one(x, now=j.clock())
                if got.get("calls"):
                    j._count(calls=got["calls"], **{got.get("via") or "failed": 1})
                _log({"ts": _stamp(), "kind": "case-tap", "ref": _ref(cid), "level": x["level"], "ok": got["ok"],
                      "via": got.get("via"), "seconds": got.get("seconds"), "usage": got.get("usage"),
                      "unchecked": got.get("unchecked"), "rejected": got.get("rejected")})
                j._put(ref, {"ref": ref, "sig": x["version"], "at": j.clock(),
                             **({"state": "stored"} if got["ok"] else
                                {"state": "failed", "note": got.get("note") or D.NOTE_FAILED})})
            except Exception as e:
                log.exception("case briefing: the job for %s failed", ref)
                j._put(ref, {"ref": ref, "sig": x["version"], "state": "failed", "note": D.NOTE_FAILED,
                             "at": j.clock(), "error": str(e)[:200]})
            finally:
                with j.lock:
                    j.running.discard(ref)
                j.changed(ref)
        if j.background:
            threading.Thread(target=work, name="webchat-home-case", daemon=True).start()
            return {"ok": True, "state": "working"}
        work()
        got = j.job(ref)
        return {"ok": True, "state": "ready" if got.get("state") == "stored" else got.get("state") or "failed",
                **({"note": got["note"]} if got.get("note") else {})}
    except Exception as e:
        log.warning("case briefing: the job for %s did not start: %s", ref, e)
        return {"ok": False, "state": "failed", "note": D.NOTE_FAILED}


def step(fe, ref: str, step_id: str, item: dict | None = None, *, case: dict | None = None,
         case_id: str = "", arg: Any = None) -> dict:
    """A step the owner tapped on a case: {"choice", "arg", "move": True}
    for the kind's answer (home_items runs it as the matching button on the
    case's card does, Hooks.letter); a finished {ok, said, goto} for the
    portal's link and Continue in a conversation. A step of a briefing older
    than the case, or one the case no longer allows, is refused."""
    D = _hd()
    try:
        j = D.jobs_for(fe)
        x = _case_of(case_id or str((case or {}).get("id") or ""), case)
        if x is None:
            return {"ok": False, "said": NOTE_NO_CASE}
        rec = Store().get(str(x["case"].get("id")))
        if not rec or (rec.get("version"), rec.get("level")) != (x["version"], x["level"]):
            return {"ok": False, "said": NOTE_STALE_STEP}
        today = datetime.fromtimestamp(j.clock(), _zone()).date()
        v = view(x, today=today)
        if step_id == D.CONTINUE:
            if not _open_level(x["level"]):
                return {"ok": False, "said": NOTE_STEP_GONE}
            text = card(x, rec, now=j.clock(), since=None)["text"]
            seed = {"text": f"{text}\n\n({v['kind']} case {v['id']})", "steps": [], "level": x["level"],
                    "title": (item or {}).get("title") or ""}
            return D.open_conversation(fe, ref, seed, item, None)
        s = next((y for y in offered(rec.get("steps") or [], v, today=today) if y["pub"]["id"] == step_id), None)
        if s is None:
            return {"ok": False, "said": NOTE_STEP_GONE}
        if s["kind"] == "open_link":
            return {"ok": True, "said": "Opening the portal.", "goto": s["part"]["link"]}
        words = " ".join(str(arg or "").split())
        if s["kind"] == "set_due" and not s["params"].get("date") and not _date(words):
            return {"ok": False, "said": "Pick the date first."}
        choice, arg_ = choice_of(s, v, words)
        return {"choice": choice, "arg": arg_, "move": True}
    except Exception as e:
        log.warning("case briefing: the step %s on %s failed: %s", step_id, ref, e)
        return {"ok": False, "said": "Tacitus could not do that. Try again."}


_LOADED: dict = {"at": 0.0, "key": None, "cases": []}
# Action requests is read again on each card's event: the cases, their rows
# and their levels are read once in this long while nothing changed.
LOADED_S = 30


def _loaded_cached(now: float) -> list[dict]:
    try:
        led = _mod("ledger")
        key = (Path(led.DB_PATH).stat().st_mtime, _hd()._store_path().stat().st_mtime
               if _hd()._store_path().exists() else 0)
    except Exception:
        key = None
    if key is None or key != _LOADED["key"] or now - _LOADED["at"] > LOADED_S:
        _LOADED.update(cases=load_all(), key=key, at=now)
    return _LOADED["cases"]


def request_views(case_ids, *, now: float | None = None) -> dict[str, dict]:
    """{case id: {"text", "by"?, "at"?, "stale"?}} for Action requests'
    blocks: the same briefing, less the places and what is missing (its
    card lists them), read-only (a block opened there is not a look)."""
    want = {str(c) for c in case_ids or () if c}
    if not want:
        return {}
    now = now if now is not None else time.time()
    out: dict[str, dict] = {}
    try:
        store = Store()
        for x in _loaded_cached(now):
            cid = str(x["case"].get("id"))
            if cid not in want:
                continue
            got = card(x, store.get(cid), now=now, since=store.since(cid), places=False)
            if got["text"]:
                out[cid] = {k: got[k] for k in ("text", "by", "at", "stale") if got.get(k) is not None}
    except Exception as e:
        log.warning("case briefing: Action requests' briefings could not be read: %s", e)
    return out


# ── the CLI ───────────────────────────────────────────────────────────

def sample(case_ids: list[str], *, now: float | None = None) -> list[dict]:
    """A dry run for each case: the packet's level, the moves, the card's
    text and steps the owner's own model would give, read-only. Nothing is
    stored and no log line is written (remote_llm's own call record goes
    where EA_PRIVATE_DIR points: a scratch copy for a run that must write
    nothing live)."""
    now = now if now is not None else time.time()
    out = []
    for cid in case_ids:
        x = load_one(cid)
        if x is None:
            out.append({"case": cid, "error": "no such case"})
            continue
        prep = prepare(x, now=now)
        got = write_one(x, now=now, order=["local"], dry_run=True, prep=prep)
        rec = {"text": got.get("text") or "", "steps": got.get("steps") or [], "by": "local", "at": now,
               "version": x["version"], "level": x["level"]} if got["ok"] else None
        shown = card(x, rec, now=now, since=None)
        out.append({"case": cid, "level": x["level"], "moves": [_move_key(m) for m in prep["allowed"]],
                    "prompt_chars": len(prep["prompt"]), "ok": got["ok"], "note": got.get("note"),
                    "seconds": got.get("seconds"), "rejected": got.get("rejected"), "unchecked": got.get("unchecked"),
                    "card": shown["text"], "steps": shown["steps"]})
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packet", help="the packet for one case, read-only")
    p.add_argument("--case", required=True)
    r = sub.add_parser("refresh", help="write the missing or stale case briefings")
    r.add_argument("--limit", type=int, default=PER_TICK)
    r.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("sample", help="a dry run on the owner's own model: nothing stored")
    s.add_argument("--case", action="append", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "refresh":
        print(json.dumps(refresh(limit=max(0, a.limit), daily=None, dry_run=a.dry_run), indent=1))
        return 0
    if a.cmd == "sample":
        print(json.dumps(sample(a.case), indent=1, ensure_ascii=False))
        return 0
    x = load_one(a.case)
    if x is None:
        print(json.dumps({"error": "no such case"}))
        return 2
    prep = prepare(x, now=time.time())
    print(json.dumps({"level": prep["level"], "version": prep["version"],
                      "moves": [_move_key(m) for m in prep["allowed"]], "route": route(prep["level"]),
                      "chars": len(prep["prompt"]), "packet": prep["packet"]}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
