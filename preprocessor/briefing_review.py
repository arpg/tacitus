#!/usr/bin/env python3
"""The pre-send review of a briefing by a hosted model (docs/llm-lanes.md,
"Briefings", "The pre-send review").

The local model writes the morning briefing, the afternoon update and
the weekly audit (remote_briefing.py, remote_audit.py). Before any of
them posts, and before the project rooms get their slices, this module
checks the post against what the system knows about each item, and
changes only what it can prove from those records:

  1. Code collects the post's items by ref (the EA post's sections, the
     follow-up lines, the rows each room lists under "Also open"), and
     for each item its ledger row (type, status, deadline, last action,
     notes), the row's history (ledger_events), the other rows on the
     same conversation, closed ones too, with the owner's own words, the
     rows whose history names this row, the thread brief (the remote
     model's records, never mail text), the project rooms it is headed
     to, a staged reply (how many people it goes to, its subject, its
     body), and the mail evidence the open-claims audit finds
     (open_claims_audit.prepare, kept mail removed). Today's calendar and
     the owner's rules for the briefing and drafting go with them.
  2. Keep levels hold (mail_privacy): an item whose thread is kept on the
     machine, or kept at facts, goes with its line and its row's type,
     status and dates only; the post's footer says how many went so. When
     the keep check cannot run, every item from mail goes that way.
  3. Code checks first, with no model (`hard_checks`): a draft on a
     conversation the owner closed in their own words is held; an item
     whose row is closed or snoozed leaves the post; a row shown twice
     shows once; audit text inside a line ("Evidence: ...; still open?")
     is cut. Code's verdicts stand over the model's.
  4. One tool-less call to the hosted model the routing table names for
     `review` (hosted_text: Claude or Codex) answers by ref, under a
     strict shape: keep, drop (done, not the owner's, a duplicate, on the
     calendar), move (another section or room), hold-draft, rewrite-line,
     each with a one-line reason, and a few notes on the whole post. The
     model only picks among the refs and actions given: a ref it invents,
     a move to a place that does not exist, a rewrite with a number the
     records do not hold, is dropped. It cannot add an item, close a row
     (a "done" becomes a close proposal, which the owner confirms on its
     card) or send anything.
  5. Code applies the verdicts: dropped items leave the post and the
     rooms, a held reply is never staged (no Send card; it becomes a
     question under "Needs you"), a dropped hand-off is not queued, and
     the post ends with what the check changed and why.

The wait is bounded (TIMEOUT_S). When the hosted model fails, times out
or the plan's limit is reached, the briefing falls back to the narrow
still-open audit it used before (remote_briefing.audit_post), and the
post says in one line that it was not checked. An owner who routes
`review` to the local model gets it there, and the post says so.

Every run is one line in <private_dir>/remote-llm/briefing-reviews.jsonl
(what was asked, the verdicts, what changed, the time and the size), and
the last prompt and answer are in briefing-review-latest.json beside it.
briefing-review-state.json counts each day's live checks; the status
light reads it (`failing_days`), and a check that failed on FAIL_DAYS
days in a row turns the light yellow.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

REVIEW_LOG = "briefing-reviews.jsonl"
LATEST = "briefing-review-latest.json"
# Per day, how many live checks ran and failed: what the status light reads.
STATE = "briefing-review-state.json"
STATE_DAYS = 14
REVIEW_MODEL = "opus"
# The longest the briefing waits for the hosted model, per call.
TIMEOUT_S = 240
# Days in a row with a failed check before the status light shows it.
FAIL_DAYS = 3

ACTIONS = ("keep", "drop", "move", "hold-draft", "rewrite-line")
DROP_KINDS = ("done", "not-owners", "duplicate", "on-calendar")
# The sections a move may name, by lane: a review can demote or move an
# item, never make a draft or a hand-off.
MOVES = {"morning-briefing": ("top", "flex", "needs"),
         "afternoon-catchup": ("since", "pending"),
         "weekly-audit": ("critical", "important")}
SECTION_NAMES = {
    "top": "Get done today", "flex": "Flexes", "needs": "Needs you",
    "draft": "Drafts ready (the owner gets a Send card)", "escalated": "Sent to Claude",
    "due": "the date banner at the top", "followup": "a Follow-up due line",
    "since": "Since this morning", "pending": "Pending your response",
    "critical": "Critical", "important": "Important", "also": "Also open here (a room post)",
}
# Highest first: of two items on one row, the one in the earlier section stays.
SECTION_RANK = ("top", "needs", "escalated", "critical", "since", "flex", "important",
                "pending", "due", "followup")
# The owner's own act on a row: a close with one of these is their decision.
USER_CLOSE_SIGNALS = ("user_stated", "user_handled", "user_dashboard", "user_rule")
# How far back the owner's own close of a thread holds a new draft on it.
USER_CLOSE_DAYS = 45
CLOSED = ("completed", "closed")

# Size bounds. The prompt stays under PROMPT_MAX_CHARS (about 4 characters
# a token); what does not fit is cut, and the record and the post say so.
PROMPT_MAX_CHARS = 90_000
MAX_ITEMS = 45
CAP = {"notes": 400, "last_action": 220, "description": 200, "event_note": 160,
       "quote": 200, "gist": 180, "body": 1200, "evidence": 200, "line": 300}
MAX_EVENTS, MAX_SIBLINGS, MAX_NAMING, MAX_MESSAGES, MAX_EVIDENCE = 6, 4, 3, 4, 4
RULES_MAX = 3000
POST_MAX = 12_000
# Each step down sheds detail before any item is cut.
SHED = ({}, {"evidence": 0, "body": 600, "notes": 250},
        {"evidence": 0, "body": 300, "notes": 150, "events": 3, "messages": 2},
        {"evidence": 0, "body": 0, "notes": 100, "events": 2, "messages": 0, "siblings": 2})

_AUDIT_TEXT = re.compile(r"\s*(?:Evidence:\s.*$|\bstill open\?\s*$|\(reconcile\)\s*$)", re.I)
_NUM = re.compile(r"\d+")
_JSON = re.compile(r"\{.*\}", re.S)

PROMPT = """You check a professor's {what} before it goes out. His assistant's local model wrote it from the assistant's records. It can be wrong: work that is finished, work that is someone else's, an item in the wrong project room, a reply that contradicts what he decided, the same thing twice, notes not meant for him. You find those. You do not write the post. You choose an action for an item by its ref, and code applies it.

{profile}
It is {now}.

The actions:
- keep: the item is right. You need not list kept items.
- drop: leave the item out. Give `kind`:
  - done: the records show the work is finished. The owner said so; a closed row or an event in its history shows it; or it prepared for a one-time event whose date passed. Code asks the owner to confirm the close; it does not close it.
  - not-owners: the work or the deadline is someone else's (a collaborator, a reviewer, a student, an office). The owner only waits, or the mail only informs him.
  - duplicate: another item in this post is the same obligation.
  - on-calendar: the item is only an event already on today's calendar, with no work for him before it.
- move: put the item in another place. `to` is a section key from `sections_you_can_move_to`, or a room key from `rooms` ("room:<key>"), or "room:none" to keep it out of every project room. Use it for an item in the wrong project room, or a do-today item that only waits on others. For an item in `also` (a room's "Also open here" list) only "room:none" applies.
- hold-draft: only for an item in the `draft` section. The staged reply must not go to him for Send: it contradicts what he decided (his words on a row of the same thread outrank everything), it answers a choice only he can make (yes or no, accept or decline, what he plans or wants), or it says to many people something the records do not show he said. Code turns it into a question for him.
- rewrite-line: the line claims something the records do not support (a due date that is not his, an action he does not have to take), or carries notes not meant for him. `line` is the new text after the item's title, in short plain words, with facts from that item's records only.

Rules:
- Use only the records given. Never add an item. Never invent a fact, a date, a number or a name.
- His own words (a `quote`) outrank the assistant's notes.
- When the records do not settle it, keep the item.
- `why` is one short sentence for him, in plain words.
- `notes` is at most 3 short sentences about the post as a whole; [] when there is nothing to say.

Answer with one JSON object and nothing else:
{{"items": [{{"ref": "<ref>", "action": "drop|move|hold-draft|rewrite-line|keep", "kind": "<for drop>", "to": "<for move>", "line": "<for rewrite-line>", "why": "<one sentence>"}}], "notes": []}}

{rules}"""

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"ref": {"type": "string"},
                       "action": {"type": "string", "enum": list(ACTIONS)},
                       "kind": {"type": "string"}, "to": {"type": "string"},
                       "line": {"type": "string"}, "why": {"type": "string"}},
        "required": ["ref", "action", "kind", "to", "line", "why"]}},
    "notes": {"type": "array", "items": {"type": "string"}}},
    "required": ["items", "notes"]}

WHAT = {"morning-briefing": "morning briefing", "afternoon-catchup": "afternoon update",
        "weekly-audit": "weekly audit"}
# The reason a verdict of each kind gives in the post when the model's own
# words cannot be shown.
KIND_WORDS = {"done": "it looks done", "not-owners": "it is someone else's work",
              "duplicate": "it is listed twice", "on-calendar": "it is on your calendar",
              "closed": "it is closed", "snoozed": "it is set aside until later"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one(text, cap: int) -> str:
    """Text on one line, no em-dash, at most `cap` characters, cut on a word."""
    t = " ".join(str(text or "").split()).replace("—", ", ").replace("–", "-")
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def strip_audit_text(line: str) -> str:
    """`line` without the still-open audit's clause ("Evidence: ...; still
    open?") or a "(reconcile)" tag: bookkeeping, never words for the owner."""
    out = str(line or "")
    for _ in range(3):
        cut, n = _AUDIT_TEXT.subn("", out)
        if not n:
            break
        out = cut.rstrip(" ;,")
    return out


# ── the items ─────────────────────────────────────────────────────────

def collect_items(lists: dict, cands: dict, *, follow_ups=(), room_posts=()) -> list[dict]:
    """The post's items, in the order a reader meets them: each section's
    (ref, line) pairs, the follow-up lines (refs F1..) and each room's
    "Also open here" rows (refs A1..). Each item carries its ledger id
    and conversation id where it has them, and the rooms its ref goes to."""
    order = [s for s in ("due", "top", "since", "critical", "flex", "important", "draft", "needs",
                         "pending", "escalated") if s in lists] + \
            [s for s in lists if s not in ("due", "top", "since", "critical", "flex", "important",
                                           "draft", "needs", "pending", "escalated")]
    rooms_of: dict[str, list[str]] = {}
    for p in room_posts or []:
        for r in p.get("refs") or []:
            rooms_of.setdefault(r, []).append(p.get("project") or "")
    items: list[dict] = []
    by_ref: dict[str, dict] = {}
    for sec in order:
        for ref, line in lists.get(sec) or []:
            if ref in by_ref:
                # One item in two places by design (the date banner and a
                # staged draft): one record, both lines.
                by_ref[ref]["sections"].append(sec)
                by_ref[ref].setdefault("also_shown", []).append({"section": sec, "line": line})
                continue
            c = cands.get(ref) or {}
            items.append({"ref": ref, "section": sec, "sections": [sec], "line": line,
                          "ledger_id": c.get("ledger_id") or "",
                          "conversation_id": c.get("conversation_id") or "",
                          "message_id": c.get("message_id") or "",
                          "title": c.get("title") or "", "kind": c.get("kind") or "",
                          "rooms": rooms_of.get(ref, [])})
            by_ref[ref] = items[-1]
    for n, f in enumerate(follow_ups or [], 1):
        if isinstance(f, dict) and f.get("id"):
            items.append({"ref": f"F{n}", "section": "followup", "sections": ["followup"],
                          "ledger_id": f["id"],
                          "line": f"Follow-up due: {_one(f.get('description'), 120)}",
                          "conversation_id": f.get("conversation_id") or "", "message_id": "",
                          "title": f.get("description") or "", "kind": "ledger", "rooms": []})
    n = 0
    for p in room_posts or []:
        for lid, desc in zip(p.get("also") or [], p.get("also_text") or []):
            n += 1
            items.append({"ref": f"A{n}", "section": "also", "sections": ["also"], "ledger_id": lid,
                          "line": _one(desc, 120), "conversation_id": "", "message_id": "",
                          "title": desc, "kind": "ledger", "rooms": [p.get("project") or ""]})
    return items


# ── the records ───────────────────────────────────────────────────────

def _connect():
    import ledger
    return ledger.guarded_connect(ledger.DB_PATH, read_only=True)


def _meta(row: dict) -> dict:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return raw
    try:
        m = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _as_of(row: dict, as_of: str) -> dict:
    """The row as it stood at `as_of`: a close or a snooze written after
    it is not yet there (a replay of an earlier run; in a live run
    `as_of` is now and nothing changes)."""
    if not as_of:
        return row
    out = dict(row)
    if (out.get("closed_at") or "") > as_of:
        out.update(status="open", closed_at=None)
    if (out.get("updated_at") or "") > as_of:
        # Its text changed after `as_of` too: the briefing's own copy of
        # the row, where there is one, is what the run saw.
        out["_newer"] = True
        if out.get("status") == "snoozed":
            out["status"] = "open"
    return out


def _row(lid: str, ledger: dict, snapshots: dict) -> dict:
    """The row behind an item: the ledger's, over the briefing's copy;
    the briefing's alone when the ledger's changed after the run."""
    db = ledger["rows"].get(lid) or {}
    snap = snapshots.get(lid) or {}
    if db.get("_newer") and snap:
        return dict(snap)
    return dict(snap, **db)


def read_ledger(ids, convs, *, conn=None, as_of: str = "") -> dict:
    """The records behind the items, read-only: {"rows": {id: row},
    "events": {id: [event]}, "by_conv": {conv: [row]}, "naming": {id:
    [event of another row that names it]}}. Events after `as_of` are
    left out."""
    ids = sorted({i for i in ids if i})
    convs = sorted({c for c in convs if c})
    own = conn is None
    conn = conn or _connect()
    factory, conn.row_factory = conn.row_factory, _dict_row
    cut = as_of or "9999"
    try:
        rows: dict[str, dict] = {}
        by_conv: dict[str, list[dict]] = {}
        for chunk in _chunks(ids):
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT * FROM ledger WHERE id IN ({q})", chunk):
                rows[r["id"]] = _as_of(r, as_of)
        for chunk in _chunks(convs):
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT * FROM ledger WHERE conversation_id IN ({q}) "
                                  f"ORDER BY created_at", chunk):
                if (r.get("created_at") or "") <= cut:
                    by_conv.setdefault(r["conversation_id"], []).append(_as_of(r, as_of))
        every = sorted(set(ids) | {r["id"] for rs in by_conv.values() for r in rs})
        events: dict[str, list[dict]] = {}
        try:
            for chunk in _chunks(every):
                q = ",".join("?" * len(chunk))
                for e in conn.execute(f"SELECT * FROM ledger_events WHERE entry_id IN ({q}) "
                                      f"AND ts <= ? ORDER BY seq", [*chunk, cut]):
                    events.setdefault(e["entry_id"], []).append(e)
            naming: dict[str, list[dict]] = {}
            for i in ids:
                for e in conn.execute("SELECT * FROM ledger_events WHERE entry_id != ? AND ts <= ? "
                                      "AND (note LIKE ? OR evidence LIKE ?) ORDER BY seq DESC LIMIT ?",
                                      (i, cut, f"%{i}%", f"%{i}%", MAX_NAMING)):
                    naming.setdefault(i, []).append(e)
        except Exception:   # a ledger from before the events table
            naming = {}
        return {"rows": rows, "events": events, "by_conv": by_conv, "naming": naming}
    finally:
        conn.row_factory = factory
        if own:
            conn.close()


def gather(items: list[dict], snapshots: dict | None = None, *, conn=None, as_of: str = "") -> dict:
    """read_ledger for the items, then for the threads their rows name:
    an item from the RSVP list carries no conversation id of its own."""
    ids = [i["ledger_id"] for i in items if i.get("ledger_id")]
    convs = {i.get("conversation_id") for i in items} - {"", None}
    ledger = read_ledger(ids, convs, conn=conn, as_of=as_of)
    extra = ({r.get("conversation_id") for r in ledger["rows"].values()}
             | {(snapshots or {}).get(i, {}).get("conversation_id") for i in ids}) - convs - {"", None}
    if extra:
        more = read_ledger([], extra, conn=conn, as_of=as_of)
        ledger["by_conv"].update(more["by_conv"])
        for k, v in more["events"].items():
            ledger["events"].setdefault(k, v)
    return ledger


def _dict_row(cursor, row) -> dict:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _chunks(seq, n: int = 400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def projects_of(ids, *, conn=None) -> dict[str, list[str]]:
    """{ledger id: [project name]} from the store's explicit links."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    own = conn is None
    conn = conn or _connect()
    factory, conn.row_factory = conn.row_factory, _dict_row
    out: dict[str, list[str]] = {}
    try:
        for chunk in _chunks(ids):
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT l.ledger_id, p.name FROM project_ledger l JOIN projects p "
                                  f"ON p.id = l.project_id WHERE l.ledger_id IN ({q})", chunk):
                out.setdefault(r["ledger_id"], []).append(r["name"])
    except Exception:
        return {}
    finally:
        conn.row_factory = factory
        if own:
            conn.close()
    return out


def room_labels(rooms: dict, *, conn=None) -> dict[str, str]:
    """{project: "<project name> (#<room>)"} for the rooms the model may
    name: the project's own name says what belongs there."""
    if not rooms:
        return {}
    own = conn is None
    names: dict[str, str] = {}
    try:
        conn = conn or _connect()
        for chunk in _chunks(sorted(rooms)):
            q = ",".join("?" * len(chunk))
            for pid, name in conn.execute(f"SELECT id, name FROM projects WHERE id IN ({q})", chunk):
                names[pid] = name
    except Exception:
        pass
    finally:
        if own and conn is not None:
            conn.close()
    out = {}
    for pid, room in rooms.items():
        name = names.get(pid) or ""
        out[pid] = f"{name} (#{room})" if name and room and name != room else (name or room or pid)
    return out


def _quote(ev: dict) -> str:
    try:
        e = json.loads(ev.get("evidence") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(e.get("quote") or "") if isinstance(e, dict) else ""


def user_close(row: dict, events: list[dict]) -> dict | None:
    """The owner's own close of `row` ({"at", "signal", "quote", "note"}),
    or None: a close event with a user signal, else a closed row whose
    record says the owner closed it."""
    for e in reversed(events or []):
        if e.get("kind") == "close" and e.get("signal") in USER_CLOSE_SIGNALS:
            return {"at": e.get("ts") or "", "signal": e.get("signal"), "quote": _quote(e),
                    "note": e.get("note") or ""}
    if (row.get("status") or "") in CLOSED:
        by = _meta(row).get("closed_by")
        sig = (by.get("signal") if isinstance(by, dict) else by) or ""
        if sig in USER_CLOSE_SIGNALS:
            return {"at": row.get("closed_at") or "", "signal": sig,
                    "quote": (by.get("quote") if isinstance(by, dict) else "") or "",
                    "note": row.get("last_action") or ""}
    return None


# ── keep levels ───────────────────────────────────────────────────────

def keep_levels(rows: list[dict]) -> dict[str, str]:
    """{row id: keep level} for rows from mail; a row from no mail is
    `full`. The strictest of the thread's records, the row's own
    message, its brief's level and its kept label (a label alone reads
    `facts`). Fails closed: when the gate cannot say, every row from mail
    reads `local`."""
    out: dict[str, str] = {}
    try:
        import mail_privacy
        import privacy_rules
        import remote_llm
        index = mail_privacy.load_index()
        ctx = mail_privacy.keep_context(remote_llm.config())
    except Exception as e:
        print(f"briefing_review: keep check unavailable, mail rows withheld: {str(e)[:120]}",
              file=sys.stderr)
        for r in rows:
            out[r.get("id") or ""] = "local" if (r.get("conversation_id") or r.get("source_message_id")) \
                else "full"
        return out
    for r in rows:
        rid = r.get("id") or ""
        level = "full"
        try:
            conv, mid = r.get("conversation_id") or "", r.get("source_message_id") or ""
            if conv:
                level = privacy_rules.stricter(level, mail_privacy.conversation_level(conv, index, ctx))
            rec = index["by_id"].get(mid) if mid else None
            if rec is not None:
                level = privacy_rules.stricter(level, mail_privacy.level_of(rec, index=index, ctx=ctx))
            brief = _meta(r).get("thread_brief") or r.get("thread_brief") or {}
            if isinstance(brief, dict) and brief.get("keep_level") in privacy_rules.LEVELS:
                level = privacy_rules.stricter(level, brief["keep_level"])
            if mail_privacy.kept_label(r) or mail_privacy.kept_label(_meta(r)) or \
                    (isinstance(brief, dict) and mail_privacy.kept_label(brief)):
                level = privacy_rules.stricter(level, "facts")
        except Exception:
            level = "local" if (r.get("conversation_id") or r.get("source_message_id")) else "full"
        out[rid] = level
    return out


def _open(level: str) -> bool:
    return level in ("summary", "full")


# ── the packet ────────────────────────────────────────────────────────

def _event_view(e: dict, cap: dict) -> dict:
    out = {"at": (e.get("ts") or "")[:16], "what": e.get("kind") or "",
           "signal": e.get("signal") or "", "by": e.get("actor") or ""}
    if e.get("note") and cap.get("event_note"):
        out["note"] = _one(e["note"], cap["event_note"])
    q = _quote(e)
    if q:
        out["quote"] = _one(q, CAP["quote"])
    return out


def _brief_view(brief: dict, n_msgs: int, cap: dict) -> dict:
    out = {k: brief.get(k) for k in ("subject", "ask", "deadline", "state", "scheduling",
                                     "user_replied_after_latest", "last_inbound", "last_from_user")
           if brief.get(k) not in (None, "", [])}
    msgs = [m for m in brief.get("messages") or [] if isinstance(m, dict)]
    if n_msgs:
        out["messages"] = [{"at": str(m.get("received") or "")[:16], "own": bool(m.get("own")),
                            "from": "owner" if m.get("own") else str(m.get("from") or "")[:60],
                            "gist": _one(m.get("gist"), cap["gist"])}
                           for m in msgs[-n_msgs:]]
    if len(msgs) > n_msgs:
        out["earlier_messages_cut"] = len(msgs) - n_msgs
    return out


def item_record(it: dict, ledger: dict, *, level: str, projects: list[str], draft: dict | None,
                evidence: list[str], shed: dict, snapshot: dict | None = None) -> dict:
    """One item of the packet. Below `summary`, only the line and the
    row's type, status and dates go."""
    cap = {**CAP, **{k: v for k, v in shed.items() if k in CAP}}
    n_events = shed.get("events", MAX_EVENTS)
    n_msgs = shed.get("messages", MAX_MESSAGES)
    n_sib = shed.get("siblings", MAX_SIBLINGS)
    lid = it.get("ledger_id") or ""
    row = _row(lid, ledger, {lid: snapshot} if snapshot else {}) if lid else {}
    out = {"ref": it["ref"], "section": it["section"], "line": _one(it.get("line"), CAP["line"])}
    if it.get("also_shown"):
        out["also_shown"] = [{"section": a["section"], "line": _one(a["line"], CAP["line"])}
                             for a in it["also_shown"]]
    if it.get("rooms"):
        out["rooms"] = [f"room:{p}" for p in it["rooms"] if p]
    if projects:
        out["projects"] = projects[:3]
    if not lid:
        out["row"] = None
        if draft:
            out["draft"] = draft
        return out
    facts = {"id": lid, "type": row.get("type") or "", "status": row.get("status") or "open",
             "deadline": row.get("deadline") or "", "created": (row.get("date_identified")
                                                               or row.get("created_at") or "")[:10]}
    for k in ("follow_up_after", "snooze_until", "closed_at"):
        if row.get(k):
            facts[k] = str(row[k])[:10]
    if not _open(level):
        out["row"] = facts
        out["withheld"] = "kept mail: only the line and the row's dates are shown"
        return out
    facts["description"] = _one(row.get("description"), cap["description"])
    if row.get("last_action") and cap.get("last_action"):
        facts["last_action"] = _one(row["last_action"], cap["last_action"])
    if row.get("notes") and cap.get("notes"):
        facts["notes"] = _one(row["notes"], cap["notes"])
    if row.get("from_name") or row.get("from_email"):
        facts["from"] = _one(row.get("from_name") or row.get("from_email"), 60)
    out["row"] = facts
    evs = ledger["events"].get(lid) or []
    if evs and n_events:
        out["history"] = [_event_view(e, cap) for e in evs[-n_events:]]
    conv = row.get("conversation_id") or it.get("conversation_id") or ""
    sibs = [r for r in ledger["by_conv"].get(conv) or [] if r.get("id") != lid] if conv else []
    if sibs and n_sib:
        view = []
        for r in sorted(sibs, key=lambda r: (r.get("status") in CLOSED, r.get("closed_at") or ""),
                        reverse=True)[:n_sib]:
            s = {"id": r.get("id"), "type": r.get("type") or "", "status": r.get("status") or "",
                 "description": _one(r.get("description"), cap["description"])}
            if r.get("closed_at"):
                s["closed_at"] = str(r["closed_at"])[:16]
            uc = user_close(r, ledger["events"].get(r.get("id")) or [])
            if uc:
                s["closed_by_owner"] = {"at": uc["at"][:16], "quote": _one(uc["quote"], CAP["quote"]),
                                        "note": _one(uc["note"], cap["event_note"] or 120)}
            elif r.get("last_action"):
                s["last_action"] = _one(r["last_action"], cap["event_note"] or 120)
            view.append(s)
        out["same_thread"] = view
        if len(sibs) > n_sib:
            out["same_thread_cut"] = len(sibs) - n_sib
    named = ledger["naming"].get(lid) or []
    if named:
        out["named_by"] = [{"row": e.get("entry_id"), "at": (e.get("ts") or "")[:16],
                            "what": e.get("kind") or "", "note": _one(e.get("note"), cap["event_note"] or 120)}
                           for e in named[:MAX_NAMING]]
    brief = _meta(row).get("thread_brief") or row.get("thread_brief")
    if isinstance(brief, dict) and brief:
        out["thread"] = _brief_view(brief, n_msgs, cap)
    if draft:
        d = dict(draft)
        if level != "full" or not cap.get("body"):
            d.pop("body", None)
        elif d.get("body"):
            d["body"] = _one(d["body"], cap["body"])
        out["draft"] = d
    if evidence and cap.get("evidence"):
        out["mail_evidence"] = [_one(e, cap["evidence"]) for e in evidence[:MAX_EVIDENCE]]
    return out


def build_packet(*, agent: str, post: str, items: list[dict], ledger: dict, levels: dict,
                 projects: dict, drafts: dict, evidence: dict, calendar: list[str],
                 rooms: dict, rules: str, snapshots: dict | None = None) -> tuple[dict, dict]:
    """(the packet the model reads, what was cut and withheld). Detail is
    shed step by step (SHED) until the packet fits PROMPT_MAX_CHARS; then
    items are cut from the end."""
    snapshots = snapshots or {}
    shown = items[:MAX_ITEMS]
    cut = [i["ref"] for i in items[MAX_ITEMS:]]
    moves = list(MOVES.get(agent, ()))
    base = {"lane": agent, "sections": {k: v for k, v in SECTION_NAMES.items()
                                        if k in {i["section"] for i in shown} | set(moves)},
            "sections_you_can_move_to": moves,
            "rooms": {f"room:{pid}": name for pid, name in sorted(rooms.items())},
            "calendar_today": calendar[:16],
            "post": post[:POST_MAX]}
    if rules:
        base["owner_rules"] = rules[:RULES_MAX]
    step = 0
    for step, shed in enumerate(SHED):
        recs = [item_record(i, ledger, level=levels.get(i.get("ledger_id") or "", "full"),
                            projects=projects.get(i.get("ledger_id") or "", []),
                            draft=drafts.get(i["ref"]), evidence=evidence.get(i.get("ledger_id") or "", []),
                            shed=shed, snapshot=snapshots.get(i.get("ledger_id") or ""))
                for i in shown]
        packet = dict(base, items=recs)
        if len(json.dumps(packet, ensure_ascii=False)) <= PROMPT_MAX_CHARS:
            break
    else:
        while recs and len(json.dumps(dict(base, items=recs), ensure_ascii=False)) > PROMPT_MAX_CHARS:
            cut.insert(0, recs.pop()["ref"])
        packet = dict(base, items=recs)
    withheld = [r["ref"] for r in packet["items"] if r.get("withheld")]
    return packet, {"cut": cut, "withheld": withheld, "shed_step": step}


def prompt_text(agent: str, packet: dict, *, profile: str, now: datetime, rules: str = "") -> str:
    head = PROMPT.format(what=WHAT.get(agent, "briefing"), profile=profile or "",
                         now=f"{now:%A %Y-%m-%d %H:%M}", rules="")
    return head.rstrip() + "\n\nThe post and its items:\n" + json.dumps(packet, ensure_ascii=False)


# ── code's checks ─────────────────────────────────────────────────────

def hard_checks(items: list[dict], ledger: dict, *, today: date, snapshots: dict | None = None,
                keep_parked: bool = False) -> dict:
    """{ref: verdict} from code alone, before any model: a draft on a
    conversation the owner closed in their own words is held; an item
    whose row is closed or snoozed leaves the post (`keep_parked`: the
    weekly audit lists snoozed rows on purpose); a row shown twice stays
    in its first section only; audit text in a line is cut."""
    snapshots = snapshots or {}
    out: dict[str, dict] = {}
    seen: dict[str, str] = {}
    rank = {s: i for i, s in enumerate(SECTION_RANK)}
    for it in sorted(items, key=lambda i: rank.get(i["section"], len(rank))):
        ref, lid, sec = it["ref"], it.get("ledger_id") or "", it["section"]
        row = _row(lid, ledger, snapshots) if lid else {}
        status = row.get("status") or "open"
        if "draft" in (it.get("sections") or [sec]):
            conv = row.get("conversation_id") or it.get("conversation_id") or ""
            since = (today - timedelta(days=USER_CLOSE_DAYS)).isoformat()
            for r in ledger["by_conv"].get(conv) or [] if conv else []:
                uc = user_close(r, ledger["events"].get(r.get("id")) or [])
                if uc and (r.get("status") or "") in CLOSED and (uc["at"] or "")[:10] >= since:
                    said = f': "{_one(uc["quote"], 90)}"' if uc["quote"] else ""
                    out[ref] = {"action": "hold-draft", "by": "code",
                                "why": f"you closed this thread on {_day(uc['at'])}{said}"}
                    break
            if ref not in out and lid and status in CLOSED:
                out[ref] = {"action": "hold-draft", "by": "code", "why": "its row is closed"}
            continue
        if lid and sec != "also":
            if status in CLOSED:
                out[ref] = {"action": "drop", "kind": "closed", "by": "code", "why": "it is closed"}
                continue
            parked = "" if keep_parked else _parked(row, today)
            if (status == "snoozed" and not keep_parked) or parked:
                when = f" until {_day(parked)}" if parked else ""
                out[ref] = {"action": "drop", "kind": "snoozed", "by": "code",
                            "why": f"it is set aside{when}"}
                continue
            if lid in seen:
                out[ref] = {"action": "drop", "kind": "duplicate", "by": "code",
                            "why": f"it is already under {SECTION_NAMES.get(seen[lid], seen[lid])}"}
                continue
            seen[lid] = sec
        clean = strip_audit_text(it.get("line") or "")
        if clean != (it.get("line") or "") and sec != "also":
            out[ref] = {"action": "strip", "by": "code", "line": clean, "why": "audit text in the line"}
    return out


def _parked(row: dict, today: date) -> str:
    for k in ("snooze_until", "follow_up_after"):
        d = _date(row.get(k))
        if d and d > today:
            return d.isoformat()
    return ""


def _day(iso: str) -> str:
    d = _date(iso)
    return f"{d:%b} {d.day}" if d else (iso or "")[:10]


# ── the model's answer ────────────────────────────────────────────────

def parse_answer(text: str) -> dict:
    """The answer object out of a reply, tolerant of fences and prose
    around it; {} when there is none."""
    m = _JSON.search(text or "")
    if not m:
        return {}
    raw = m.group(0)
    for cand in (raw, _balanced(raw)):
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else {}
        except (TypeError, ValueError):
            continue
    return {}


def _balanced(raw: str) -> str:
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[:i + 1]
    return raw


def _numbers_known(text: str, record: dict) -> bool:
    """Every number in `text` is in the item's own records."""
    blob = json.dumps(record, ensure_ascii=False)
    return all(n in blob for n in _NUM.findall(text or ""))


def check_verdicts(obj: dict, packet: dict, *, agent: str) -> tuple[dict, list[dict], list[str]]:
    """({ref: verdict}, rejected, notes): only refs in the packet, only
    the actions each section allows, a move only to a place the packet
    names, a rewrite or a reason only with numbers its item's records
    hold. The first verdict for a ref counts."""
    recs = {r["ref"]: r for r in packet.get("items") or []}
    rooms = set(packet.get("rooms") or {}) | {"room:none"}
    moves = set(MOVES.get(agent, ()))
    out: dict[str, dict] = {}
    rejected: list[dict] = []

    def no(v, why):
        rejected.append({"ref": str(v.get("ref") or "")[:40], "action": str(v.get("action") or "")[:20],
                         "why": why})

    for v in (obj or {}).get("items") or []:
        if not isinstance(v, dict):
            continue
        ref, act = str(v.get("ref") or ""), str(v.get("action") or "").strip().lower()
        rec = recs.get(ref)
        if rec is None:
            no(v, "unknown ref")
            continue
        if ref in out:
            continue
        if act not in ACTIONS:
            no(v, "unknown action")
            continue
        why = _one(v.get("why"), 160)
        if why and not _numbers_known(why, rec):
            why = ""
        sec = rec["section"]
        secs = {sec} | {a["section"] for a in rec.get("also_shown") or []}
        if act == "keep":
            out[ref] = {"action": "keep", "why": why}
        elif act == "drop":
            kind = str(v.get("kind") or "").strip().lower().replace("_", "-").replace(" ", "-")
            kind = {"not-the-owners": "not-owners", "not-owner": "not-owners",
                    "calendar": "on-calendar"}.get(kind, kind)
            if kind not in DROP_KINDS:
                no(v, "drop without a known kind")
                continue
            if sec == "also":
                out[ref] = {"action": "move", "to": "room:none", "why": why}
                continue
            out[ref] = {"action": "drop", "kind": kind, "why": why}
        elif act == "move":
            to = str(v.get("to") or "").strip()
            if sec == "also":
                if to != "room:none":
                    no(v, "an Also open row can only leave its room")
                    continue
            elif to.startswith("room:"):
                if to not in rooms:
                    no(v, "unknown room")
                    continue
            elif to not in moves or to == sec or secs & {"draft", "followup"}:
                no(v, "a move to a section this item cannot take")
                continue
            out[ref] = {"action": "move", "to": to, "why": why}
        elif act == "hold-draft":
            if "draft" not in secs:
                no(v, "hold-draft on an item that is not a draft")
                continue
            out[ref] = {"action": "hold-draft", "why": why}
        elif act == "rewrite-line":
            line = strip_audit_text(_one(v.get("line"), 200)).strip()
            if not line or sec in ("also", "followup"):
                no(v, "an empty rewrite, or one on a line that has none")
                continue
            if not _numbers_known(line, rec):
                no(v, "a number the records do not hold")
                continue
            out[ref] = {"action": "rewrite-line", "line": line, "why": why}
    notes = [_one(n, 200) for n in (obj or {}).get("notes") or [] if isinstance(n, str) and n.strip()]
    blob = json.dumps(packet, ensure_ascii=False)
    notes = [n for n in notes if all(x in blob for x in _NUM.findall(n))][:3]
    return out, rejected, notes


def merge(hard: dict, model: dict) -> dict:
    """Code's verdicts stand over the model's. Cutting audit text is no
    verdict of its own (apply_lists cuts it from every line): the model's
    verdict on that ref stands."""
    out = {r: dict(v, by="model") for r, v in model.items()}
    for ref, v in hard.items():
        if v["action"] == "strip":
            out.setdefault(ref, {"action": "keep", "by": "code", "why": v["why"]})
            continue
        out[ref] = v
    return out


# ── the call ──────────────────────────────────────────────────────────

def judge() -> str:
    """Who reviews: the routing table's `review` (remote_briefing.audit_judge):
    "claude", "codex", or "remote" for the owner's own model."""
    import remote_briefing as rb
    return rb.audit_judge()


def _is_base(who: str) -> bool:
    try:
        import model_routing
        return model_routing.is_base(who)
    except Exception:
        return False


def _name(model: str) -> str:
    """The hosted model's own name for the owner (its registry entry):
    "Claude", "Codex"."""
    try:
        import model_routing
        m = model_routing.for_model(model)
        return (m.name if m else "") or "the hosted model"
    except Exception:
        return "the hosted model"


def _plan_full() -> bool:
    """The plan's saved reading says its limit is reached now."""
    try:
        import rate_limits
        reading = rate_limits.load("resident")
    except Exception:
        return False
    if not reading or (datetime.now(timezone.utc) - reading["ts"]).total_seconds() > 12 * 3600:
        return False
    now = time.time()
    for w in reading["windows"].values():
        resets = w.get("resets_at")
        try:
            r = datetime.fromisoformat(str(resets).replace("Z", "+00:00")).timestamp() if resets else None
        except ValueError:
            r = None
        if r is not None and r <= now:
            continue
        if w.get("status") == "rejected" or float(w.get("percent_used") or 0) >= 100:
            return True
    return False


_LIMIT = re.compile(r"usage limit|rate.?limit|limit reached|\b429\b|quota|out of credits", re.I)
_TIMEOUT = re.compile(r"timed? ?out|timeout", re.I)


def _why_failed(text: str) -> str:
    if _LIMIT.search(text or ""):
        return "limit"
    if _TIMEOUT.search(text or ""):
        return "timeout"
    return "error"


def ask(prompt: str, *, who: str, timeout: float = TIMEOUT_S, call=None, chat=None) -> dict:
    """One review call. {"ok", "text", "model", "reason", "usage",
    "cost_usd", "fell_back", "seconds"}; `reason` is limit, timeout,
    error or unreadable when it fails. Never raises."""
    t0 = time.monotonic()
    out = {"ok": False, "text": "", "model": who, "reason": "", "usage": {}, "cost_usd": None,
           "fell_back": ""}
    try:
        if who == "remote":
            import remote_llm
            obj, meta = (chat or remote_llm.chat_json)(
                [{"role": "system", "content": "You check a briefing before it goes out. Answer in the schema."},
                 {"role": "user", "content": prompt}],
                purpose="briefing-review", schema=SCHEMA, schema_name="briefing_review",
                max_tokens=4000, timeout=timeout)
            out.update(ok=True, text=json.dumps(obj), usage=(meta or {}).get("usage") or {})
        else:
            # The saved plan reading is the base model's (Claude's) plan.
            if call is None and _is_base(who) and _plan_full():
                out["reason"] = "limit"
                return out
            if call is None:
                import ea_config
                import hosted_text
                model = ea_config.get_models().get("briefing_review") or REVIEW_MODEL
                r = hosted_text.run("review", prompt, model, timeout=timeout)
            else:
                r = call(prompt, timeout=timeout)
            out.update(model=r.get("model_used") or who, usage=r.get("usage") or {},
                       cost_usd=r.get("cost_usd"), fell_back=r.get("fell_back") or "")
            if r.get("rc") or not r.get("text"):
                out["reason"] = _why_failed(f"{r.get('stderr') or ''} {r.get('text') or ''}")
            else:
                out.update(ok=True, text=str(r["text"]))
    except Exception as e:
        out["reason"] = "timeout" if "Timeout" in type(e).__name__ else _why_failed(str(e))
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        out["seconds"] = round(time.monotonic() - t0, 1)
    return out


# ── applying it ───────────────────────────────────────────────────────

def apply_lists(lists: dict, review: dict | None, head=None) -> dict:
    """`lists` ({section: [(ref, line)]}) with the review applied: dropped
    refs out, held drafts out of `draft`, moved refs at the end of their
    new section, rewritten lines after the item's title (`head(ref)`),
    and audit text cut from every line whatever else happened."""
    rv = review or {}
    drop = set(rv.get("drop") or {}) | set(rv.get("hold") or {})
    moves = rv.get("move") or {}
    rewrite = rv.get("rewrite") or {}
    out: dict = {k: [] for k in lists}
    moved: list[tuple[str, str, str]] = []
    for sec, items in lists.items():
        for ref, line in items:
            if ref in drop and (sec == "draft" or ref not in (rv.get("hold") or {})):
                continue
            text = line
            if ref in rewrite:
                h = head(ref) if head else ""
                text = f"{h}: {rewrite[ref]}" if h and line.startswith(h) else rewrite[ref]
            text = strip_audit_text(text)
            to = moves.get(ref)
            if to and to in lists and to != sec:
                moved.append((to, ref, text))
                continue
            out[sec].append((ref, text))
    for to, ref, text in moved:
        out[to].append((ref, text))
    return out


def room_moves(review: dict | None) -> dict[str, list[str]]:
    """{ref: [project]} for the refs a review moved to a room ([] for none)."""
    out = {}
    for ref, to in ((review or {}).get("rooms") or {}).items():
        out[ref] = [] if to == "room:none" else [to.split(":", 1)[1]]
    return out


def room_skip(review: dict | None, cands: dict) -> dict[str, list[str]]:
    """{project: [ledger id]} for briefing_rooms.plan_posts: the rows the
    review took off a room's Also open list, and under "*" the rows it
    dropped or moved, which no room lists again as Also open."""
    rv = review or {}
    out = {k: list(v) for k, v in (rv.get("also_skip") or {}).items()}
    moved = set(rv.get("drop") or {}) | set(rv.get("rooms") or {})
    out["*"] = sorted({(cands.get(r) or {}).get("ledger_id") for r in moved} - {None, ""})
    return out


def gone_refs(review: dict | None) -> set[str]:
    """Refs the review took out of the post (dropped or held)."""
    rv = review or {}
    return set(rv.get("drop") or {}) | set(rv.get("hold") or {})


def decide(items: list[dict], verdicts: dict) -> dict:
    """The review dict the renderers read: {drop: {ref: {kind, why, by}},
    hold: {ref: why}, move: {ref: section}, rooms: {ref: room key},
    rewrite: {ref: line}, also_skip: {project: [ledger id]}, hide_tail:
    [ledger id], done: [ledger id]}."""
    by_ref = {i["ref"]: i for i in items}
    rv: dict = {"drop": {}, "hold": {}, "move": {}, "rooms": {}, "rewrite": {}, "also_skip": {},
                "hide_tail": [], "done": []}
    for ref, v in verdicts.items():
        it = by_ref.get(ref)
        if not it:
            continue
        act = v["action"]
        if act == "drop":
            if it["section"] == "followup":
                rv["hide_tail"].append(it["ledger_id"])
            rv["drop"][ref] = {"kind": v.get("kind") or "", "why": v.get("why") or "",
                               "by": v.get("by") or "model"}
            if v.get("kind") == "done" and it.get("ledger_id"):
                rv["done"].append(it["ledger_id"])
        elif act == "hold-draft":
            rv["hold"][ref] = v.get("why") or ""
        elif act == "move":
            to = v.get("to") or ""
            if it["section"] == "also":
                pid = (it.get("rooms") or [""])[0]
                rv["also_skip"].setdefault(pid, []).append(it["ledger_id"])
            elif to.startswith("room:"):
                rv["rooms"][ref] = to
            else:
                rv["move"][ref] = to
                rv.setdefault("whys", {})[ref] = v.get("why") or ""
        elif act == "rewrite-line":
            rv["rewrite"][ref] = v["line"]
    return rv


# ── the post's footer ─────────────────────────────────────────────────

_LABEL = re.compile(r"^\s*(?:Due \d{4}-\d{2}-\d{2}:|Task:|Needs response:|Scheduling request:|"
                    r"Priority email:)\s*", re.I)


def sentence(text: str) -> str:
    """`text` ending in one full stop."""
    t = str(text or "").strip()
    return t if re.search(r"[.!?][\"')]?$", t) else t + "."


def _title(it: dict) -> str:
    return _one(_LABEL.sub("", str(it.get("title") or it.get("line") or ""), count=1), 60)


def _reason(v: dict) -> str:
    return _one((v.get("why") or KIND_WORDS.get(v.get("kind") or "", "")).rstrip("."), 110)


def footer(review: dict, items: list[dict]) -> list[str]:
    """The post's lines on the check: whether it ran and on what model,
    one line per change with its reason, and what it did not see."""
    st = review.get("status")
    if st == "off":
        return []
    name = _name(review.get("model") or "")
    by_ref = {i["ref"]: i for i in items}
    t = {r: _title(i) for r, i in by_ref.items()}
    if st == "local":
        head = (":mag: *Checked before sending* by your own model, not by Claude or Codex. "
                "Your Models setting sends this check there.")
    elif st == "ok":
        head = f":mag: *Checked before sending* by {name}."
    else:
        why = {"limit": "your plan's limit is reached",
               "timeout": f"{name} did not answer in time",
               "unreadable": f"{name} gave an answer Tacitus could not read"}.get(
            review.get("reason") or "", "the check could not run")
        head = f":warning: *Not checked before sending*: {why}. Tacitus's own checks ran."
    lines: list[str] = []
    by = review.get("by") or {}
    for r, v in (review.get("drop") or {}).items():
        if v.get("kind") == "done" or by_ref.get(r, {}).get("section") == "also":
            continue
        if v.get("kind") == "duplicate" and v.get("by") == "code":
            continue   # it still shows once
        lines.append(f"Left out *{t.get(r, r)}*: {_reason(v)}.")
    for r, why in (review.get("hold") or {}).items():
        lines.append(f"Held the reply to *{t.get(r, r)}*: {sentence(why or 'it needs you')} "
                     "It is under Needs you, with no Send card.")
    for r, to in (review.get("move") or {}).items():
        lines.append(f"Moved *{t.get(r, r)}* to {SECTION_NAMES.get(to, to)}"
                     + (f": {_reason({'why': (review.get('whys') or {}).get(r, '')})}."
                        if (review.get("whys") or {}).get(r) else "."))
    for r, to in (review.get("rooms") or {}).items():
        where = ("out of the project rooms" if to == "room:none"
                 else f"to the {(review.get('room_names') or {}).get(to, to)} room")
        lines.append(f"Moved *{t.get(r, r)}* {where}.")
    skipped = sum(len(v) for v in (review.get("also_skip") or {}).values())
    if skipped:
        lines.append(f"Took {skipped} {'row' if skipped == 1 else 'rows'} off a room's Also open list.")
    done = [r for r, v in (review.get("drop") or {}).items() if v.get("kind") == "done"]
    if done:
        lines.append(f"{len(done)} {'item looks' if len(done) == 1 else 'items look'} done. "
                     "See Looks done?")
    if review.get("rewrite"):
        n = len(review["rewrite"])
        lines.append(f"Reworded {n} {'line' if n == 1 else 'lines'} to match the records.")
    if st == "ok" and not lines:
        head += " Nothing to change."
    if review.get("withheld"):
        n = len(review["withheld"])
        who = name if st in ("ok", "failed") else "your own model"
        lines.append(f"To keep their mail private, {who} did not see the records of "
                     f"{n} {'item' if n == 1 else 'items'}.")
    if review.get("cut"):
        n = len(review["cut"])
        lines.append(f"{n} {'item was' if n == 1 else 'items were'} not checked: the post was too long.")
    for n in (review.get("notes") or [])[:2]:
        lines.append(f"Note: {sentence(n)}")
    return ["", head] + [f"- {x}" for x in lines[:10]]


# ── the record ────────────────────────────────────────────────────────

def record(rec: dict, latest: dict | None = None, *, appender=None, state=None) -> None:
    """One line in the review log, the last prompt and answer beside it,
    and the day's count in the state file the status light reads;
    best-effort. `appender` and `state` stand in for the store."""
    try:
        import remote_llm
        (appender or remote_llm.append)(REVIEW_LOG, rec)
        if appender is None:
            import owner_trust
            if latest is not None:
                owner_trust.write_owner_file(remote_llm.store_dir() / LATEST,
                                             json.dumps(latest, ensure_ascii=False, indent=1).encode(),
                                             mode=0o600)
        if not rec.get("dry_run") and rec.get("status") in ("ok", "local", "failed"):
            path = state if state is not None else (None if appender is not None
                                                    else remote_llm.store_dir() / STATE)
            if path is not None:
                note_day(path, rec)
    except Exception as e:
        print(f"briefing_review: run record not written: {str(e)[:160]}", file=sys.stderr)


def note_day(path: Path, rec: dict) -> dict:
    """Add one live check to its day in the state file (an owner file,
    0600), keeping STATE_DAYS days."""
    import owner_trust
    try:
        data = json.loads(owner_trust.read_owner_file(path))
    except FileNotFoundError:
        data = {}
    days = data.get("days") if isinstance(data, dict) and isinstance(data.get("days"), dict) else {}
    day = str(rec.get("ts") or _now_iso())[:10]
    try:
        local = datetime.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00")).astimezone()
        day = local.date().isoformat()
    except (TypeError, ValueError):
        pass
    d = days.setdefault(day, {"ok": 0, "failed": 0})
    if rec.get("status") == "failed":
        d["failed"] = int(d.get("failed") or 0) + 1
        d["reason"] = str(rec.get("reason") or "error")[:20]
        d["at"] = str(rec.get("ts") or "")[:25]
    else:
        d["ok"] = int(d.get("ok") or 0) + 1
    keep = sorted(days)[-STATE_DAYS:]
    out = {"days": {k: days[k] for k in keep}, "updated": _now_iso()}
    owner_trust.write_owner_file(path, json.dumps(out).encode(), mode=0o600)
    return out


def failing_days(state: dict | None, *, today: date | None = None) -> tuple[int, dict | None]:
    """(days in a row, ending today or yesterday, with a failed live check
    and no good one; that run of days' latest day's counts)."""
    today = today or datetime.now().astimezone().date()
    days = (state or {}).get("days") if isinstance((state or {}).get("days"), dict) else {}

    def bad(d: date) -> dict | None:
        v = days.get(d.isoformat())
        return v if isinstance(v, dict) and int(v.get("failed") or 0) > 0 and not int(v.get("ok") or 0) \
            else None
    day = today if days.get(today.isoformat()) else today - timedelta(days=1)
    n, last = 0, None
    while bad(day):
        last = last or bad(day)
        n += 1
        day -= timedelta(days=1)
    return n, last


# ── the whole check for a remote briefing ─────────────────────────────

def enabled(cfg: dict | None = None) -> bool:
    """On unless remote-llm.json says `"briefing_review": false`."""
    try:
        import remote_llm
        cfg = cfg if cfg is not None else remote_llm.config()
    except Exception:
        return True
    return cfg.get("briefing_review") is not False


def _calendar_lines(b: dict, today: date) -> list[str]:
    import remote_briefing as rb
    return rb._calendar(b, today)


def _recipients(c: dict, b: dict, *, headers=None) -> int | None:
    """How many people a reply-all to the item's message reaches: from the
    briefing's own copy of its headers, else one header read; None when
    neither can say."""
    mid = c.get("message_id") or ""
    if not mid:
        return None
    for it in b.get("new_needs_response") or []:
        if it.get("message_id") == mid and ("to" in it or "cc" in it):
            people = {str(a).lower() for a in (it.get("to") or []) + (it.get("cc") or []) if a}
            people.add(str(it.get("from") or "").lower())
            return len(people - _owner_addrs() - {""})
    try:
        return (headers or _graph_recipients)(mid)
    except Exception:
        return None


def _owner_addrs() -> set[str]:
    try:
        import mail_privacy
        return {a.lower() for a in mail_privacy._user_addresses()}
    except Exception:
        return set()


def _graph_recipients(mid: str) -> int | None:
    import graph_client
    msg = graph_client.get_message_full(mid)
    people = set()
    for f in ("toRecipients", "ccRecipients"):
        for r in msg.get(f) or []:
            people.add((((r or {}).get("emailAddress") or {}).get("address") or "").lower())
    people.add((((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower())
    return len(people - _owner_addrs() - {""})


def draft_records(drafts: list[dict], cands: dict, b: dict, *, bodies: dict | None = None,
                  headers=None) -> dict[str, dict]:
    """{ref: {"to_count", "subject", "says", "body"}} for each staged reply."""
    out = {}
    for d in drafts:
        if not d.get("ok"):
            continue
        c = cands.get(d["ref"]) or {}
        rec = {"subject": _one(c.get("title"), 160), "says": _one(d.get("says"), 300),
               "reply_all": True}
        n = _recipients(c, b, headers=headers)
        if n is not None:
            rec["to_count"] = n
        body = (bodies or {}).get(d["ref"]) or d.get("body") or ""
        rec["body"] = body or "(not written yet in a dry run)"
        out[d["ref"]] = rec
    return out


def mail_evidence(agent: str, post: str, b: dict, ids: list[str], *, no_fetch: bool = False,
                  prepare=None) -> tuple[dict[str, list[str]], list[str]]:
    """({ledger id: [evidence line]}, parked ids) from the open-claims
    audit's own search, kept mail removed."""
    try:
        import open_claims_audit as oca
        rec = (prepare or oca.prepare)(agent=agent, post=post, briefing=b, rendered=ids,
                                       claude_safe=True, no_fetch=no_fetch)
    except Exception as e:
        print(f"briefing_review: no mail evidence: {str(e)[:160]}", file=sys.stderr)
        return {}, []
    ev = {c["id"]: list(c.get("evidence") or []) for c in rec.get("claims") or [] if c.get("id")}
    return ev, [p["id"] for p in rec.get("parked") or [] if p.get("id")]


def review_briefing(agent: str, text: str, b: dict, *, cands: dict, lists: dict, drafts: list[dict],
                    now: datetime, dry_run: bool, run_id: str = "", room_posts=(), room_names=None,
                    bodies=None, headers=None, ledger=None, levels=None, projects=None,
                    evidence=None, call=None, chat=None, who=None, propose=None, appender=None,
                    as_of: str = "", fallback=None, no_fetch: bool = False, conn=None,
                    state=None, timeout: float = TIMEOUT_S) -> dict:
    """The whole check for one post. Returns the `checked` dict the
    renderers read: the still-open audit's keys (closed, closed_rows,
    unsure_rows, parked, asked, judge) and `review`. On a failed call
    `fallback` (the narrow audit) gives the still-open keys, and code's
    own checks still apply. Every injectable reads live state when left
    out; nothing here posts."""
    t0 = time.monotonic()
    import remote_briefing as rb
    local = now.astimezone(rb._tz())
    today = local.date()
    follow_ups = b.get("resurrected_follow_ups") or [] if agent == "morning-briefing" else []
    items = collect_items(lists, cands, follow_ups=follow_ups, room_posts=room_posts)
    ids = [i["ledger_id"] for i in items if i.get("ledger_id")]
    snapshots = {o.get("id"): o for o in (b.get("open_obligations") or []) + (b.get("stale_obligations") or [])
                 if isinstance(o, dict) and o.get("id")}
    rec: dict = {"ts": now.isoformat(timespec="seconds"), "agent": agent, "run_id": run_id,
                 "dry_run": dry_run, "items": len(items)}
    try:
        if ledger is None:
            ledger = gather(items, snapshots, conn=conn, as_of=as_of)
        rows = [_row(i, ledger, snapshots) for i in ids]
        levels = levels if levels is not None else keep_levels(rows)
        projects = projects if projects is not None else projects_of(ids, conn=conn)
    except Exception as e:
        print(f"briefing_review: records unreadable: {str(e)[:200]}", file=sys.stderr)
        rec.update(status="failed", reason="error", error=f"records: {str(e)[:160]}")
        ledger = {"rows": {}, "events": {}, "by_conv": {}, "naming": {}}
        levels, projects = {i: "local" for i in ids}, {}
    hard = hard_checks(items, ledger, today=today, snapshots=snapshots,
                       keep_parked=agent == "weekly-audit")
    who = who or judge()
    review: dict = {"model": who}
    checked: dict = {}
    if rec.get("status") != "failed":
        ev, parked = (evidence, []) if evidence is not None else \
            mail_evidence(agent, text, b, ids, no_fetch=no_fetch)
        for it in items:
            if it.get("ledger_id") in parked and it["ref"] not in hard and it["section"] not in ("also",):
                hard[it["ref"]] = {"action": "drop", "kind": "snoozed", "by": "code",
                                   "why": "it is set aside until later"}
        dr = draft_records(drafts, cands, b, bodies=bodies, headers=headers)
        packet, meta = build_packet(agent=agent, post=text, items=items, ledger=ledger, levels=levels,
                                    projects=projects, drafts=dr, evidence=ev,
                                    calendar=_calendar_lines(b, today) if agent != "weekly-audit" else [],
                                    rooms=room_labels(room_names or {}, conn=conn),
                                    rules=_rules(),
                                    snapshots=snapshots)
        prompt = prompt_text(agent, packet, profile=rb._profile(), now=local)
        rec.update(prompt_chars=len(prompt), est_prompt_tokens=len(prompt) // 4,
                   asked=[r["ref"] for r in packet["items"]], cut=meta["cut"],
                   withheld=meta["withheld"], shed_step=meta["shed_step"], judge=who)
        answer = ask(prompt, who=who, timeout=timeout, call=call, chat=chat)
        rec.update(model=answer["model"], call_seconds=answer["seconds"], usage=answer["usage"],
                   cost_usd=answer["cost_usd"], fell_back=answer["fell_back"],
                   answer_chars=len(answer["text"] or ""),
                   est_answer_tokens=len(answer["text"] or "") // 4)
        model_v, rejected, notes = {}, [], []
        if answer["ok"]:
            obj = parse_answer(answer["text"])
            if not obj:
                answer.update(ok=False, reason="unreadable")
            else:
                model_v, rejected, notes = check_verdicts(obj, packet, agent=agent)
        review.update(model=answer["model"], withheld=meta["withheld"], cut=meta["cut"])
        if answer["ok"]:
            review.update(status="local" if who == "remote" else "ok", notes=notes)
        else:
            review.update(status="failed", reason=answer["reason"])
            rec.update(reason=answer["reason"], error=answer.get("error", ""))
        rec.update(status=review["status"], verdicts=model_v, rejected=rejected, notes=notes)
        record_latest = {"ts": rec["ts"], "agent": agent, "run_id": run_id, "prompt": prompt,
                         "answer": answer["text"]}
    else:
        review.update(status="failed", reason="error")
        record_latest = None
        model_v = {}
    final = merge(hard, model_v)
    rv = decide(items, final)
    review.update(rv)
    review["by"] = {r: v.get("by", "model") for r, v in final.items()}
    review["room_names"] = {f"room:{k}": v for k, v in (room_names or {}).items()}
    # A "done" is the owner's to confirm: a close proposal, never a close.
    proposed: list[dict] = []
    desc = {i["ledger_id"]: i.get("title") or "" for i in items if i.get("ledger_id")}
    for ref, v in rv["drop"].items():
        lid = next((i["ledger_id"] for i in items if i["ref"] == ref), "")
        if v.get("kind") != "done" or not lid:
            continue
        ok = True if dry_run else (propose or _propose)(lid, f"pre-send review ({agent}): {v['why'][:200]}")
        if ok:
            proposed.append({"id": lid, "why": v.get("why") or "", "description": desc.get(lid, "")})
    if review["status"] == "failed" and fallback is not None:
        try:
            checked = fallback() or {}
        except Exception as e:
            print(f"briefing_review: the fallback audit failed: {str(e)[:160]}", file=sys.stderr)
            checked = {}
    checked = dict(checked)
    checked["closed"] = list(dict.fromkeys(list(checked.get("closed") or []) + [p["id"] for p in proposed]))
    seen = {r.get("id") for r in checked.get("closed_rows") or []}
    checked["closed_rows"] = list(checked.get("closed_rows") or []) + [p for p in proposed if p["id"] not in seen]
    checked.setdefault("parked", [])
    checked.setdefault("unsure_rows", [])
    checked.setdefault("judge", who)
    review["footer"] = footer(review, items)
    checked["review"] = review
    changes = {"dropped": {r: v for r, v in rv["drop"].items()}, "held": rv["hold"],
               "moved": rv["move"], "rooms": rv["rooms"], "rewritten": rv["rewrite"],
               "also_skip": rv["also_skip"], "hide_tail": rv["hide_tail"],
               "proposed": [p["id"] for p in proposed]}
    rec.update(hard={r: v for r, v in hard.items()}, changes=changes,
               seconds=round(time.monotonic() - t0, 1))
    checked["review_record"] = rec
    record(rec, record_latest, appender=appender, state=state)
    return checked


def _rules() -> str:
    try:
        import preferences
        return preferences.render(["briefing", "drafting", "mail-triage"])
    except Exception:
        return ""


def _propose(ledger_id: str, why: str) -> bool:
    """A close proposal through the ledger CLI, as the still-open audit
    makes one (open_claims_audit._close_row), and its missed-closure line."""
    import open_claims_audit as oca
    res = oca._close_row(ledger_id, why)
    if res.get("ok"):
        try:
            import log_missed_closure as lmc
            lmc.log_missed_closure(ledger_id=ledger_id, user_message=why, source="pre-send-review",
                                   no_fetch=True)
        except Exception as e:
            print(f"briefing_review: missed-closure log failed for {ledger_id}: {e}", file=sys.stderr)
    return bool(res.get("ok"))


# ── replies written before the check, staged after it ─────────────────

def _key(c: dict) -> tuple:
    return (c.get("message_id") or "", c.get("ledger_id") or "", c.get("conversation_id") or "")


class PendingDrafts:
    """The drafter a live run uses while the check is on: it runs the
    writer and stages nothing, so a reply the check holds never becomes a
    draft and never gets a Send card. `settle` stages the rest. Its `mark`
    wraps the reply queue's recorder: a "drafted" note waits for the
    draft's id, and a held reply is recorded as a decision instead."""

    def __init__(self, *, write=None):
        self._write = write
        self.items: dict[tuple, dict] = {}
        self.marks: list[tuple] = []

    def __call__(self, c: dict, says: str, run_id: str = "") -> dict:
        import remote_briefing as rb
        w = rb.write_reply(c, says, write=self._write)
        if not w.get("ok"):
            return w
        self.items[_key(c)] = {"cand": c, "body": w.get("body") or "", "says": says}
        return {"ok": True, "draft_id": "", "pending": True}

    def body(self, c: dict) -> str:
        return (self.items.get(_key(c)) or {}).get("body") or ""

    def wrap(self, base):
        def mark(ledger_id, inbound, outcome, *, now, draft_id="", note="", choices=()):
            if outcome == "drafted" and not draft_id:
                self.marks.append((ledger_id, inbound, now, note))
                return True
            return base(ledger_id, inbound, outcome, now=now, draft_id=draft_id, note=note,
                        choices=choices)
        return mark


def settle_drafts(drafts: list[dict], cands: dict, checked: dict, *, run_id: str, dry_run: bool,
                  pending: PendingDrafts | None = None, mark=None, stage=None,
                  withdraw=None) -> dict:
    """Act on the check for each reply this run wrote. A held one becomes
    a decision for the owner (under "Needs you", with why) and is never
    staged; one staged before the check (a stand-in drafter) is deleted
    from Drafts. The rest are staged now. Returns counts."""
    import remote_briefing as rb
    hold = ((checked or {}).get("review") or {}).get("hold") or {}
    stage = stage or rb.stage_reply
    out = {"held": 0, "staged": 0, "failed": 0, "withdrawn": 0}
    final: dict[tuple, tuple] = {}
    for d in drafts:
        if not d.get("ok"):
            continue
        c = cands.get(d["ref"]) or {}
        if d["ref"] in hold:
            why = hold[d["ref"]].rstrip(".") or "it needs you"
            if d.get("draft_id") and not dry_run:
                (withdraw or _withdraw)(d["draft_id"])
                out["withdrawn"] += 1
            d.update(ok=False, held=why, draft_id="",
                     decision=f"I held the reply I wrote: {sentence(why)} Tell me here what to say, "
                              f"or that no reply is needed.")
            out["held"] += 1
            final[_key(c)] = ("decision", "", d["decision"])
            continue
        if dry_run or pending is None or d.get("draft_id"):
            continue
        item = pending.items.get(_key(c))
        if item is None:
            continue
        res = stage(c, item["body"], run_id=run_id)
        if res.get("ok"):
            d.update(draft_id=res.get("draft_id") or "", pending=False)
            out["staged"] += 1
            final[_key(c)] = ("drafted", d["draft_id"], item["says"])
        else:
            d.update(ok=False, failed=True,
                     decision=f"The draft could not be made ({_one(res.get('why'), 80)}). "
                              f"{_one(d.get('says'), 120)}")
            out["failed"] += 1
    if pending is not None and mark is not None:
        keys = {}
        for c in cands.values():
            if c.get("ledger_id"):
                keys.setdefault(c["ledger_id"], set()).update(rb._thread_keys(c))
        for lid, inbound, now, note in pending.marks:
            mine = keys.get(lid, {f"ledger_id:{lid}"})
            hit = next((v for k, v in final.items()
                        if rb._thread_keys({"message_id": k[0], "ledger_id": k[1],
                                            "conversation_id": k[2]}) & mine), None)
            if hit is None:
                continue   # not staged: the next run tries again
            outcome, draft_id, text = hit
            mark(lid, inbound, outcome, now=now, draft_id=draft_id, note=text)
    return out


def _withdraw(draft_id: str) -> bool:
    """Delete a draft the check held (mail_actions delete-draft: no
    button, the owner's own Drafts folder); the bot retires its card when
    the journal records the delete."""
    import remote_briefing as rb
    rc, out = rb._run_json([str(HERE / "mail_actions.py"), "delete-draft", "--provider", "outlook",
                            "--draft-id", draft_id], env=dict(os.environ))
    return rc == 0


# ── the remote briefing's entry point ─────────────────────────────────

def review_post(agent: str, text: str, b: dict, *, cands: dict, plan: dict, drafts: list[dict],
                escalated: list[dict], now: datetime, dry_run: bool, run_id: str = "",
                rooms=None, pending: PendingDrafts | None = None, rendered=None, **kw) -> dict:
    """The check for remote_briefing.run: the post's sections, the rooms
    each item is headed to (the room slices planned, not sent), the bodies
    the writer wrote, then review_briefing with the still-open audit as
    its fallback. Off (`"briefing_review": false`), the audit alone runs."""
    import remote_briefing as rb

    def audit():
        return rb.audit_post(agent, text, b, dry_run=dry_run, chat=None, rendered=rendered)
    if not enabled():
        return audit()
    today = now.astimezone(rb._tz()).date()
    lists = (rb._morning_lists(cands, plan, drafts, escalated, {}, today) if agent == "morning-briefing"
             else rb._afternoon_lists(cands, plan, {}, drafts))
    try:
        posts = (rooms or rb.room_slices)(agent, cands, plan, drafts, escalated, {}, b,
                                          now=now, dry_run=True) or []
    except Exception as e:
        print(f"briefing_review: room slices not planned: {str(e)[:160]}", file=sys.stderr)
        posts = []
    names = {p["project"]: p.get("room") or p["project"] for p in posts if p.get("project")}
    if rooms is None:
        try:
            import briefing_rooms
            names.update({pid: t.get("room") or pid for pid, t in briefing_rooms.rooms().items()})
        except Exception:
            pass
    bodies = {d["ref"]: pending.body(cands[d["ref"]]) for d in drafts
              if pending is not None and d.get("ok") and d["ref"] in cands}
    return review_briefing(agent, text, b, cands=cands, lists=lists, drafts=drafts, now=now,
                           dry_run=dry_run, run_id=run_id, room_posts=posts, room_names=names,
                           bodies=bodies, fallback=audit, **kw)


def summary(checked: dict | None) -> dict:
    """The run record's line on the check: counts, the model, size and time."""
    rv = (checked or {}).get("review") or {}
    rec = (checked or {}).get("review_record") or {}
    if not rv:
        return {"status": "off"}
    return {"status": rv.get("status"), "model": rv.get("model"), "reason": rv.get("reason", ""),
            "dropped": len(rv.get("drop") or {}), "held": len(rv.get("hold") or {}),
            "moved": len(rv.get("move") or {}) + len(rv.get("rooms") or {}),
            "rewritten": len(rv.get("rewrite") or {}), "withheld": len(rv.get("withheld") or []),
            "cut": len(rv.get("cut") or []), "prompt_chars": rec.get("prompt_chars"),
            "est_prompt_tokens": rec.get("est_prompt_tokens"), "seconds": rec.get("seconds"),
            "call_seconds": rec.get("call_seconds")}
