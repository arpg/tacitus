#!/usr/bin/env python3
"""Letters someone asked the owner to write: one case per person, tracked
to submission.

A LETTER CASE is one person the owner was asked to write for: a
recommendation, a reference, a nomination, a letter of support or a
character letter, for a student, an employee, a colleague, a tenant or a
client. A case has DESTINATIONS (a program, an employer, an award, a
portal), each with its own due date, how it is submitted (a portal link,
an email to an address, an upload, unknown) and a state:

    asked -> accepted (or declined) -> drafted -> ready -> submitted

Only a submission finishes a destination: a portal's confirmation mail,
the upload tool's success, a sent mail that carries the letter to the
destination, or the owner's word. A reply in the thread never closes a
letter row and never raises a close card (closure.py asks
`letter_signal_allowed`). A decline closes every destination of the case.

Store: three tables in the ledger database (`letter_cases`,
`letter_destinations`, `letter_questions`, with `letter_events` for the
history and `letter_aliases` for the owner's answers about who is who).
Each open destination has one open ledger row with closes_on=submission
and metadata `letter: {case, destination, role: "destination"}`. The
person's own request, when there is one, is the case's row (`role:
"case"`): it carries the first decision and closes with the case. Every
reader asks `is_letter_row(row)`: the reply queue, the Replies card, the
closure policy, the draft gate and the briefing.

Detection (`detect`) reads one mail record: the local model's `letter`
field first, then rules over the record's subject, ask, summary, links
and sender for the common shape of a request mail (a person's own ask, a
portal's request on the person's behalf, a reminder, a confirmation).
No institution or vendor is named here; a plugin adds sender patterns
through `capabilities.mail_rules.letter_request_senders` and
`letter_confirm_senders` (plugin_registry.mail_rules).

Joining (`ingest_record`): a mail joins the case of the same address, or
of the same name when the name matches without doubt. A name that may or
may not be the same person asks the owner once (a `join` question with
answer buttons); the answer is remembered (`letter_aliases`). `merge`
folds one case into another by hand.

Privacy: a case keeps the strictest keep level of the mail it came from
(`keep_level`). `view()` and `summary_line()` drop the person's name, the
destination names, the links and the notes below `summary`, so a hosted
prompt or a hosted-read channel never sees what the keep level holds back.

    letters.py list [--all]
    letters.py get --case <id>
    letters.py accept --case <id> [--quote "<owner's words>"]
    letters.py decline --case <id> [--quote "<owner's words>"]
    letters.py add-destination --case <id> --name <name> [--due YYYY-MM-DD]
                               [--method portal|email|upload|unknown] [--link <url>]
                               [--ledger-id <id>]
    letters.py set-due --dest <id> --due YYYY-MM-DD
    letters.py materials --case <id> --kind <cv|statement|list|draft|transcript|other>
                         --ref <path or message id>
    letters.py record-draft --case <id> --file <path> [--dest <id>]
    letters.py ready --dest <id>
    letters.py record-submitted --dest <id> --evidence "<what shows it>"
    letters.py merge --from <case> --into <case>
    letters.py questions
    letters.py answer --question <id> --choice "<the answer>"
    letters.py migrate [--days 21] [--apply] [--report <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

EA_DIR = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))

KINDS = ("recommendation", "reference", "nomination", "support", "character", "evaluation", "other")
METHODS = ("portal", "email", "upload", "unknown")
STATES = ("asked", "accepted", "declined", "drafted", "ready", "submitted")
OPEN_STATES = ("asked", "accepted", "drafted", "ready")
DONE_STATES = ("declined", "submitted")
MATERIAL_KINDS = ("cv", "statement", "list", "draft", "transcript", "sample", "other")
ROLES = ("case", "destination")
QUESTION_KINDS = ("write", "join")
# A closed case reopens for the same person's new request within this
# many days (one application season); later, a new case starts.
REOPEN_DAYS = 120
WRITE_CHOICES = ("Yes", "Yes, ask for materials", "No")
JOIN_SAME, JOIN_OTHER = "Same person", "Someone else"

# The closure signals a letter row hears. A letter row closes on its
# submission (the portal's confirmation, the upload tool, a sent mail that
# carries the letter), on the owner's word or click, on a decline, or as a
# repeat folded into another row. Nothing else closes or proposes it: a
# reply in its thread, an archive, the date passing, a model's reading.
SUBMITTED_SIGNAL = "letter_submitted"
DECLINED_SIGNAL = "letter_declined"
CASE_DONE_SIGNAL = "letter_case_done"
LETTER_SIGNALS = frozenset({SUBMITTED_SIGNAL, DECLINED_SIGNAL, CASE_DONE_SIGNAL})
# The Replies card's "Not needed" means "no email reply", and Handled on a
# draft's card means the draft is dealt with; neither says the letter is
# done. A letter row is not on the Replies card, and a close with either
# note is refused here too.
REPLY_CARD_NOTE = "No reply needed: the owner's click on the Replies card"
DRAFT_HANDLED_NOTE = "User marked handled in chat"
NOT_LETTER_DONE_NOTES = (REPLY_CARD_NOTE, DRAFT_HANDLED_NOTE)
# A send that names the row (the EA's draft, or the owner's own send of
# it) delivers a letter only when the letter goes by email.
SEND_SIGNALS = frozenset({"linked_send", "user_sent_myself"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS letter_cases (
    id            TEXT PRIMARY KEY,
    person_name   TEXT NOT NULL DEFAULT '',
    person_email  TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'recommendation',
    status        TEXT NOT NULL DEFAULT 'open',
    decision      TEXT NOT NULL DEFAULT '',
    opened        TEXT NOT NULL,
    closed_at     TEXT,
    materials     TEXT NOT NULL DEFAULT '[]',
    base_letter   TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    aliases       TEXT NOT NULL DEFAULT '[]',
    keep_level    TEXT NOT NULL DEFAULT 'full',
    case_ledger_id TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS letter_destinations (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    due             TEXT NOT NULL DEFAULT '',
    method          TEXT NOT NULL DEFAULT 'unknown',
    link            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'asked',
    ledger_id       TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    sender          TEXT NOT NULL DEFAULT '',
    draft           TEXT NOT NULL DEFAULT '',
    submitted_at    TEXT,
    evidence        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_letter_dest_case ON letter_destinations(case_id);
CREATE INDEX IF NOT EXISTS idx_letter_dest_ledger ON letter_destinations(ledger_id);
CREATE TABLE IF NOT EXISTS letter_questions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    dest_id     TEXT NOT NULL DEFAULT '',
    ledger_id   TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    choices     TEXT NOT NULL DEFAULT '[]',
    payload     TEXT NOT NULL DEFAULT '{}',
    asked_at    TEXT NOT NULL,
    answer      TEXT NOT NULL DEFAULT '',
    answered_at TEXT,
    actor       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS letter_aliases (
    key      TEXT NOT NULL,
    case_id  TEXT NOT NULL,
    answer   TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (key, case_id)
);
CREATE TABLE IF NOT EXISTS letter_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    case_id  TEXT NOT NULL,
    dest_id  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    state    TEXT NOT NULL DEFAULT '',
    actor    TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT ''
);
"""


def _ledger():
    try:
        import ledger
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import ledger
    return ledger


def _closure():
    try:
        import closure
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import closure
    return closure


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """The ledger database with the letter tables, rows as sqlite3.Row."""
    led = _ledger()
    path = Path(db_path) if db_path else led.DB_PATH
    conn = led.guarded_connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(led.SCHEMA)
    for migration in led.MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass
    ensure_schema(conn)
    _closure().ensure_schema(conn)
    return conn


def _json(text, default):
    try:
        val = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def _id(prefix: str, *parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
    return f"{prefix}-{h}"


def _event(conn, case_id: str, kind: str, *, dest_id: str = "", state: str = "",
           actor: str = "", evidence="") -> None:
    if not isinstance(evidence, str):
        evidence = json.dumps(evidence, sort_keys=True, default=str)
    conn.execute("INSERT INTO letter_events (ts, case_id, dest_id, kind, state, actor, evidence) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (_now(), case_id, dest_id, kind, state, actor, (evidence or "")[:2000]))


# ── names ────────────────────────────────────────────────────────────

_HONORIFIC = re.compile(r"^(?:dr|prof|professor|mr|mrs|ms|mx|miss)\.?\s+", re.I)


def norm_name(name: str) -> str:
    """A person's name for comparison: accents off, case folded, honorifics
    and punctuation gone, "Last, First" turned around."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = text.strip()
    if text.count(",") == 1 and "@" not in text:
        last, first = (p.strip() for p in text.split(","))
        if last and first:
            text = f"{first} {last}"
    text = _HONORIFIC.sub("", text)
    text = re.sub(r"[^A-Za-z\s'-]", " ", text).lower()
    return " ".join(text.replace("'", "").split())


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s-]+", norm_name(name)) if t]


def name_match(a: str, b: str) -> str:
    """'same', 'maybe' or 'no' for two names of a person.

    same: the same words in any order, or the same first and last word with
    middle names left out ("Ana M. Ruiz" and "Ana Ruiz"), or a compound
    last name written with or without its hyphen.
    maybe: the same last name and a first name that is an initial or a
    prefix of the other ("A. Ruiz", "Alex Ruiz" and "Alexandra Ruiz"), or
    the same first name and only one of the last names of a compound one.
    no: anything else, and any name of one word."""
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return "no"
    if ta == tb or sorted(ta) == sorted(tb):
        return "same"
    na, nb = norm_name(a).replace("-", " ").split(), norm_name(b).replace("-", " ").split()
    if na == nb:
        return "same"
    fa, fb = ta[0], tb[0]
    la, lb = ta[-1], tb[-1]
    if fa == fb and la == lb:
        return "same"
    if la == lb and (fa[0] == fb[0]) and (fa.startswith(fb) or fb.startswith(fa) or
                                          len(fa) == 1 or len(fb) == 1):
        return "maybe"
    if fa == fb and (set(ta[1:]) & set(tb[1:])):
        return "maybe"
    return "no"


def _alias_keys(name: str = "", email: str = "") -> list[str]:
    keys = []
    if email:
        keys.append("email:" + email.strip().lower())
    if norm_name(name):
        keys.append("name:" + norm_name(name))
    return keys


# ── the store ────────────────────────────────────────────────────────

def _dest_dict(r) -> dict:
    return {"id": r["id"], "case": r["case_id"], "name": r["name"], "due": r["due"],
            "method": r["method"], "link": r["link"], "state": r["state"],
            "ledger_id": r["ledger_id"], "conversation_id": r["conversation_id"],
            "sender": r["sender"], "draft": r["draft"],
            "submitted_at": r["submitted_at"] or "", "evidence": r["evidence"]}


def _case_dict(conn, r) -> dict:
    dests = [_dest_dict(d) for d in conn.execute(
        "SELECT * FROM letter_destinations WHERE case_id = ? ORDER BY "
        "CASE WHEN due = '' THEN 1 ELSE 0 END, due, created_at", (r["id"],))]
    qs = [question_dict(q) for q in conn.execute(
        "SELECT * FROM letter_questions WHERE case_id = ? AND answer = '' ORDER BY asked_at",
        (r["id"],))]
    return {"id": r["id"], "person_name": r["person_name"], "person_email": r["person_email"],
            "kind": r["kind"], "status": r["status"], "decision": r["decision"],
            "opened": r["opened"], "closed_at": r["closed_at"] or "",
            "materials": _json(r["materials"], []), "base_letter": r["base_letter"],
            "notes": r["notes"], "aliases": _json(r["aliases"], []),
            "keep_level": r["keep_level"] or "full", "case_ledger_id": r["case_ledger_id"],
            "destinations": dests, "questions": qs}


def cases(conn, open_only: bool = True) -> list[dict]:
    ensure_schema(conn)
    sql = "SELECT * FROM letter_cases"
    if open_only:
        sql += " WHERE status = 'open'"
    return [_case_dict(conn, r) for r in conn.execute(sql + " ORDER BY opened")]


def case(conn, case_id: str) -> dict | None:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    return _case_dict(conn, r) if r else None


def destination(conn, dest_id: str) -> dict | None:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM letter_destinations WHERE id = ?", (dest_id,)).fetchone()
    return _dest_dict(r) if r else None


def case_for_row(conn, ledger_id: str) -> dict | None:
    """The case a ledger row belongs to: its metadata's `letter.case`, or a
    destination or case that names the row."""
    ensure_schema(conn)
    r = conn.execute("SELECT metadata FROM ledger WHERE id = ?", (ledger_id,)).fetchone()
    meta = _json(r["metadata"], {}) if r else {}
    link = meta.get("letter") if isinstance(meta.get("letter"), dict) else {}
    cid = str(link.get("case") or "")
    if not cid:
        d = conn.execute("SELECT case_id FROM letter_destinations WHERE ledger_id = ?",
                         (ledger_id,)).fetchone()
        c = conn.execute("SELECT id FROM letter_cases WHERE case_ledger_id = ?",
                         (ledger_id,)).fetchone()
        cid = (d["case_id"] if d else "") or (c["id"] if c else "")
    return case(conn, cid) if cid else None


def case_for_conversation(conn, conversation_id: str, *, open_only: bool = True) -> dict | None:
    """The case that has a row (or a destination) on this conversation."""
    if not conversation_id:
        return None
    ensure_schema(conn)
    for r in conn.execute("SELECT id, status, metadata FROM ledger WHERE conversation_id = ? "
                          "AND source IS NULL", (conversation_id,)):
        if open_only and r["status"] == "completed":
            continue
        link = _json(r["metadata"], {}).get("letter")
        if isinstance(link, dict) and link.get("case"):
            c = case(conn, str(link["case"]))
            if c and (c["status"] == "open" or not open_only):
                return c
    d = conn.execute("SELECT case_id FROM letter_destinations WHERE conversation_id = ?",
                     (conversation_id,)).fetchone()
    c = case(conn, d["case_id"]) if d else None
    return c if c and (c["status"] == "open" or not open_only) else None


def ensure_case(conn, person_name: str, person_email: str = "", kind: str = "recommendation",
                *, keep_level: str = "full", actor: str = "") -> str:
    """The open case for this person (by address, then by a name that
    matches without doubt or that the owner said is the same person), or a
    new one. Returns its id. A name that only may match opens a new case;
    ingest_record() asks the owner about it (ask_join)."""
    ensure_schema(conn)
    found = find_case(conn, person_name, person_email)
    if not found.get("case"):
        # The same person's case from this season, closed since: a new
        # place to send the letter reopens it rather than starting over.
        old = find_case(conn, person_name, person_email, open_only=False)
        r = conn.execute("SELECT * FROM letter_cases WHERE id = ?",
                         (old.get("case") or "",)).fetchone()
        if r is not None and r["status"] == "closed" and r["decision"] != "declined" and \
                (r["closed_at"] or "") >= (datetime.now(timezone.utc)
                                           - timedelta(days=REOPEN_DAYS)).strftime("%Y-%m-%d"):
            conn.execute("UPDATE letter_cases SET status = 'open', closed_at = NULL, updated_at = ? "
                         "WHERE id = ?", (_now(), r["id"]))
            _event(conn, r["id"], "reopen", actor=actor)
            found = {"case": r["id"]}
    if found.get("case"):
        cid = found["case"]
        _absorb_identity(conn, cid, person_name, person_email, keep_level=keep_level)
        return cid
    kind = kind if kind in KINDS else "recommendation"
    now = _now()
    cid = _id("lc", norm_name(person_name) or person_email.lower(), now, os.urandom(4).hex())
    conn.execute("INSERT INTO letter_cases (id, person_name, person_email, kind, opened, "
                 "keep_level, aliases, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (cid, (person_name or "").strip()[:120], (person_email or "").strip().lower()[:200],
                  kind, now, keep_level or "full", "[]", now))
    _event(conn, cid, "open", actor=actor, evidence={"kind": kind})
    return cid


def _absorb_identity(conn, cid: str, name: str, email: str, *, keep_level: str = "full") -> None:
    """Keep the other spellings of a person's name and address on the case,
    and the strictest keep level of the mail it came from."""
    r = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (cid,)).fetchone()
    if not r:
        return
    aliases = _json(r["aliases"], [])
    changed = False
    for v in (name.strip(), email.strip().lower()):
        if v and v not in aliases and v not in (r["person_name"], r["person_email"]):
            aliases.append(v[:200])
            changed = True
    level = _stricter(r["keep_level"] or "full", keep_level or "full")
    sets, params = [], []
    if changed:
        sets.append("aliases = ?")
        params.append(json.dumps(aliases[:20]))
    if email and not r["person_email"]:
        sets.append("person_email = ?")
        params.append(email.strip().lower())
    if name and not r["person_name"]:
        sets.append("person_name = ?")
        params.append(name.strip()[:120])
    if level != (r["keep_level"] or "full"):
        sets.append("keep_level = ?")
        params.append(level)
    if sets:
        sets.append("updated_at = ?")
        params += [_now(), cid]
        conn.execute(f"UPDATE letter_cases SET {', '.join(sets)} WHERE id = ?", params)


def _stricter(a: str, b: str) -> str:
    try:
        import privacy_rules
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import privacy_rules
    try:
        return privacy_rules.stricter(a or "full", b or "full")
    except Exception:
        order = ("local", "facts", "summary", "full")
        return min((a or "full", b or "full"), key=lambda x: order.index(x) if x in order else 3)


def find_case(conn, person_name: str, person_email: str = "", *, open_only: bool = True) -> dict:
    """{"case": id} for a sure match, {"maybe": [ids]} for a name that may
    be the same person, {} for none. The owner's earlier answers decide
    first: a name he said is someone else never matches that case again."""
    ensure_schema(conn)
    keys = _alias_keys(person_name, person_email)
    said = {}
    for k in keys:
        for a in conn.execute("SELECT case_id, answer FROM letter_aliases WHERE key = ?", (k,)):
            said[a["case_id"]] = a["answer"]
    rows = conn.execute("SELECT * FROM letter_cases" + (" WHERE status = 'open'" if open_only else "")
                        + " ORDER BY opened DESC").fetchall()
    sure, maybe = [], []
    email = (person_email or "").strip().lower()
    for r in rows:
        if said.get(r["id"]) == "other":
            continue
        if said.get(r["id"]) == "same":
            sure.append(r["id"])
            continue
        aliases = _json(r["aliases"], [])
        addrs = {r["person_email"]} | {a for a in aliases if "@" in a}
        if email and email in addrs:
            sure.append(r["id"])
            continue
        names = [r["person_name"]] + [a for a in aliases if "@" not in a]
        verdicts = {name_match(person_name, n) for n in names if n}
        if "same" in verdicts:
            sure.append(r["id"])
        elif "maybe" in verdicts:
            maybe.append(r["id"])
    if len(sure) == 1:
        return {"case": sure[0]}
    if len(sure) > 1:
        # Two cases the facts both name: the one with this address, else
        # the oldest (`merge` folds the other by hand).
        by_mail = [cid for cid in sure if email and any(
            r["id"] == cid and (r["person_email"] == email or email in _json(r["aliases"], []))
            for r in rows)]
        return {"case": (by_mail or sure)[-1]}
    return {"maybe": maybe} if maybe else {}


def _row(conn, ledger_id: str):
    return conn.execute("SELECT * FROM ledger WHERE id = ?", (ledger_id,)).fetchone() if ledger_id else None


def link_row(conn, ledger_id: str, case_id: str, dest_id: str = "", *, role: str = "destination") -> bool:
    """Mark a ledger row as this case's: metadata `letter`, and for a
    destination row closes_on=submission. The row's type stays one of the
    nine (a `task` for work, kept as it is otherwise)."""
    r = _row(conn, ledger_id)
    if r is None:
        return False
    meta = _json(r["metadata"], {})
    meta["letter"] = {"case": case_id, "destination": dest_id, "role": role}
    meta["closes_on"] = "submission" if role == "destination" else "manual"
    meta["response_mode"] = "submission" if role == "destination" else meta.get("response_mode", "other")
    meta.pop("close_proposal", None)
    led = _ledger()
    rtype = r["type"]
    if led.canonical_type(rtype) in led.REPLY_TYPES:
        # A row whose type a reply finishes becomes work: the letter.
        rtype = "task"
    conn.execute("UPDATE ledger SET metadata = ?, type = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(meta, sort_keys=True, default=str), rtype, _now(), ledger_id))
    if role == "case":
        conn.execute("UPDATE letter_cases SET case_ledger_id = ?, updated_at = ? WHERE id = ?",
                     (ledger_id, _now(), case_id))
    return True


def _new_row(conn, case_row, dest_name: str, due: str, *, conversation_id: str = "",
             message_id: str = "", sender: str = "") -> str:
    """A ledger row for a destination that has none (a portal's request
    the intake left out, a destination from the person's list)."""
    key = message_id or f"{case_row['id']}|{dest_name}"
    lid = _id("letter", key)
    if _row(conn, lid) is None:
        now = _now()
        level = case_row["keep_level"] or "full"
        meta = {}
        if level in _OPEN_LEVELS:
            who = case_row["person_name"] or "someone"
            desc = f"Letter for {who}: {dest_name or 'destination'}"
        else:
            # A kept case's row names no one: hosted lanes read the ledger.
            desc = "Letter request (kept on your own model)"
            meta["keep_level"] = level
        conn.execute(
            "INSERT INTO ledger (id, type, status, description, from_email, source_message_id, "
            "conversation_id, date_identified, deadline, last_action, notes, metadata, "
            "created_at, updated_at) VALUES (?, 'task', 'open', ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)",
            (lid, desc[:200], sender or None, message_id or None, conversation_id or None, now,
             due or None, json.dumps(meta), now, now))
    return lid


def add_destination(conn, case_id: str, name: str, due: str = "", method: str = "unknown",
                    link: str = "", ledger_id: str = "", *, conversation_id: str = "",
                    sender: str = "", message_id: str = "", actor: str = "") -> str:
    """A destination on the case, with its one open ledger row: the row
    given (linked here), else a new one. A destination of the same name
    already on the case gets the new facts instead (a reminder, a portal
    mail for a program the person listed). Returns its id."""
    ensure_schema(conn)
    c = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no letter case {case_id}")
    method = method if method in METHODS else "unknown"
    due = _day(due)
    now = _now()
    existing = same_destination(conn, case_id, name, sender=sender, link=link)
    if existing:
        d = conn.execute("SELECT * FROM letter_destinations WHERE id = ?", (existing,)).fetchone()
        sets = {"due": d["due"] or due, "method": d["method"] if d["method"] != "unknown" else method,
                "link": d["link"] or link, "conversation_id": d["conversation_id"] or conversation_id,
                "sender": d["sender"] or sender, "name": d["name"] or (name or "")[:160]}
        lid = d["ledger_id"]
        old_row, new_row = _row(conn, lid), _row(conn, ledger_id)
        if ledger_id and ledger_id != lid and new_row is not None:
            if not lid or old_row is None or (old_row["status"] == "completed"
                                              and d["state"] in OPEN_STATES):
                sets["ledger_id"] = ledger_id
                link_row(conn, ledger_id, case_id, existing)
            elif not old_row["source_message_id"] and new_row["source_message_id"]:
                # The destination came from the person's list (a row with no
                # mail behind it); the portal's own mail row takes its place,
                # so evidence on the portal's thread reaches it.
                sets["ledger_id"] = ledger_id
                link_row(conn, ledger_id, case_id, existing)
                if old_row["deadline"] and not new_row["deadline"]:
                    conn.execute("UPDATE ledger SET deadline = ? WHERE id = ?",
                                 (old_row["deadline"], ledger_id))
                _closure().settle(conn, lid, signal="duplicate_of", actor=actor or "letters",
                                  note=f"the same letter destination as {ledger_id}",
                                  evidence={"duplicate_of": ledger_id})
            else:
                # A second row for the same destination (a reminder's own
                # row): it folds into the destination's row.
                link_row(conn, ledger_id, case_id, existing)
                _closure().settle(conn, ledger_id, signal="duplicate_of", actor=actor or "letters",
                                  note=f"the same letter destination as {lid}",
                                  evidence={"duplicate_of": lid})
        sets["updated_at"] = now
        conn.execute(f"UPDATE letter_destinations SET {', '.join(k + ' = ?' for k in sets)} "
                     "WHERE id = ?", (*sets.values(), existing))
        _sync_row_date(conn, existing)
        return existing
    did = _id("ld", case_id, norm_dest(name) or sender or link or now, now, os.urandom(4).hex())
    lid = ledger_id if ledger_id and _row(conn, ledger_id) is not None else ""
    if not lid:
        lid = _new_row(conn, c, name, due, conversation_id=conversation_id,
                       message_id=message_id, sender=sender)
    state = "accepted" if c["decision"] == "accepted" else "asked"
    r = _row(conn, lid)
    if r is not None and r["status"] == "completed":
        # Its row closed before the case knew it (the owner's word, a tap):
        # the destination is done; `migrate --reopen` corrects a wrong close.
        state = "submitted"
    conn.execute("INSERT INTO letter_destinations (id, case_id, name, due, method, link, state, "
                 "ledger_id, conversation_id, sender, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (did, case_id, (name or "")[:160], due, method, (link or "")[:600], state, lid,
                  conversation_id, (sender or "").lower(), now, now))
    link_row(conn, lid, case_id, did)
    _event(conn, case_id, "destination", dest_id=did, state=state, actor=actor,
           evidence={"name": name, "due": due, "method": method, "ledger_id": lid})
    _sync_row_date(conn, did)
    return did


def norm_dest(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\b(?:the|of|at|for|program|programme|graduate|school|admissions?|"
                  r"application|phd|ms|department)\b", " ", text)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def same_destination(conn, case_id: str, name: str, *, sender: str = "", link: str = "") -> str:
    """The destination of this case the facts name: the same portal link,
    the same name, or the same sender for a destination with no name."""
    rows = conn.execute("SELECT * FROM letter_destinations WHERE case_id = ?", (case_id,)).fetchall()
    key = norm_dest(name)
    for d in rows:
        if link and d["link"] and _link_key(link) == _link_key(d["link"]):
            return d["id"]
    for d in rows:
        dk = norm_dest(d["name"])
        if key and dk and (key == dk or (len(key) > 5 and (key in dk or dk in key))):
            return d["id"]
    for d in rows:
        # One service often serves many places: its address alone joins two
        # mails only when neither names a place and they carry no two links.
        if (sender and d["sender"] == sender.lower() and not key and not norm_dest(d["name"])
                and not (link and d["link"])):
            return d["id"]
    return ""


def _link_key(url: str) -> str:
    u = real_url(url)
    p = urlparse(u)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}?{p.query}"


def _day(value: str) -> str:
    v = str(value or "").strip()[:10]
    try:
        date.fromisoformat(v)
    except ValueError:
        return ""
    return v


def _sync_row_date(conn, dest_id: str) -> None:
    """The destination's row carries the destination's due date."""
    d = conn.execute("SELECT * FROM letter_destinations WHERE id = ?", (dest_id,)).fetchone()
    if not d or not d["ledger_id"] or not d["due"]:
        return
    r = _row(conn, d["ledger_id"])
    if r is not None and r["status"] != "completed" and not r["deadline"]:
        conn.execute("UPDATE ledger SET deadline = ?, updated_at = ? WHERE id = ?",
                     (d["due"], _now(), d["ledger_id"]))


def set_due(conn, dest_id: str, due: str, *, actor: str = "") -> dict:
    """A destination's new date (YYYY-MM-DD, or "" for none): its row's
    deadline follows, and the change is an event with its actor (a moved
    date starts its own reminder ladder). Returns {"dest", "due", "was"}."""
    ensure_schema(conn)
    d = conn.execute("SELECT * FROM letter_destinations WHERE id = ?", (dest_id,)).fetchone()
    if d is None:
        raise ValueError(f"no letter destination {dest_id}")
    new = _day(due)
    if due and not new:
        raise ValueError("due must be YYYY-MM-DD")
    now = _now()
    conn.execute("UPDATE letter_destinations SET due = ?, updated_at = ? WHERE id = ?",
                 (new, now, dest_id))
    r = _row(conn, d["ledger_id"])
    if r is not None and r["status"] != "completed":
        conn.execute("UPDATE ledger SET deadline = ?, updated_at = ? WHERE id = ?",
                     (new or None, now, d["ledger_id"]))
    _event(conn, d["case_id"], "due", dest_id=dest_id, state=new, actor=actor,
           evidence={"from": d["due"], "to": new})
    return {"dest": dest_id, "due": new, "was": d["due"]}


def add_material(conn, case_id: str, kind: str, path_or_message_id: str, *, actor: str = "") -> bool:
    ensure_schema(conn)
    r = conn.execute("SELECT materials FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if r is None:
        return False
    mats = _json(r["materials"], [])
    kind = kind if kind in MATERIAL_KINDS else "other"
    ref = str(path_or_message_id or "").strip()[:400]
    if not ref or any(m.get("ref") == ref and m.get("kind") == kind for m in mats):
        return False
    mats.append({"kind": kind, "ref": ref, "at": _now()})
    conn.execute("UPDATE letter_cases SET materials = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(mats[-40:]), _now(), case_id))
    _event(conn, case_id, "material", actor=actor, evidence={"kind": kind, "ref": ref})
    return True


MATERIAL_WORDS = (("cv", r"\b(?:cv|resume|curriculum)\b"),
                  ("statement", r"\b(?:sop|statement|purpose|personal)\b"),
                  ("transcript", r"\btranscript"),
                  ("list", r"\b(?:programs?|schools?|list|deadlines?|positions?)\b"),
                  ("draft", r"\b(?:draft|template)\b"),
                  ("sample", r"\b(?:paper|manuscript|sample|thesis)\b"))


def material_kind(filename: str) -> str:
    """The kind of a file the person sent, from its name alone."""
    low = re.sub(r"[_.\-]+", " ", str(filename or "").lower())
    for kind, pat in MATERIAL_WORDS:
        if re.search(pat, low):
            return kind
    return "other"


def set_state(conn, dest_id: str, state: str, evidence: str = "", actor: str = "") -> dict:
    """Move a destination to `state`. `submitted` closes its ledger row
    (closure.settle, signal letter_submitted) and, when nothing is left
    open, the case and its own row. `declined` closes the row on the
    owner's decision. Returns {"dest", "state", "closed": [ids],
    "case_closed": bool}."""
    ensure_schema(conn)
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    d = conn.execute("SELECT * FROM letter_destinations WHERE id = ?", (dest_id,)).fetchone()
    if d is None:
        raise ValueError(f"no letter destination {dest_id}")
    now = _now()
    out = {"dest": dest_id, "state": state, "closed": [], "case_closed": False}
    if d["state"] in DONE_STATES and state not in DONE_STATES:
        # Reopening a finished destination is the owner's correction.
        pass
    sets = ["state = ?", "updated_at = ?"]
    params = [state, now]
    if state == "submitted":
        sets += ["submitted_at = ?", "evidence = ?"]
        params += [now, (evidence or "")[:600]]
    params.append(dest_id)
    conn.execute(f"UPDATE letter_destinations SET {', '.join(sets)} WHERE id = ?", params)
    _event(conn, d["case_id"], "state", dest_id=dest_id, state=state, actor=actor, evidence=evidence)
    if state in ("drafted", "ready", "submitted"):
        # A letter written or sent means he said yes: the question is moot.
        c = conn.execute("SELECT decision FROM letter_cases WHERE id = ?", (d["case_id"],)).fetchone()
        if c is not None and not c["decision"]:
            conn.execute("UPDATE letter_cases SET decision = 'accepted', updated_at = ? WHERE id = ?",
                         (now, d["case_id"]))
            _answer_open(conn, d["case_id"], "write", "Yes", actor or "letters")
    if state in DONE_STATES and d["ledger_id"]:
        sig = SUBMITTED_SIGNAL if state == "submitted" else DECLINED_SIGNAL
        what = "submitted" if state == "submitted" else "declined"
        res = _closure().settle(conn, d["ledger_id"], signal=sig, actor=actor or "letters",
                                note=f"Letter {what}: {d['name'] or 'destination'}"
                                     + (f" ({evidence})" if evidence else ""),
                                evidence={"letter": dest_id, "evidence": evidence}, sweep=False)
        if res.get("action") == "closed":
            out["closed"] += res.get("ids") or []
    done = _maybe_close_case(conn, d["case_id"], actor=actor)
    out["case_closed"] = done is not None
    out["closed"] += done or []
    return out


def _maybe_close_case(conn, case_id: str, *, actor: str = "") -> list[str] | None:
    """A case with every destination finished closes itself, with its own
    row. Returns the ledger ids closed, or None when the case stays open."""
    c = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if c is None or c["status"] != "open":
        return None
    states = [r["state"] for r in conn.execute(
        "SELECT state FROM letter_destinations WHERE case_id = ?", (case_id,))]
    if not states or any(s not in DONE_STATES for s in states):
        return None
    status = "declined" if all(s == "declined" for s in states) else "closed"
    conn.execute("UPDATE letter_cases SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                 (status, _now(), _now(), case_id))
    _event(conn, case_id, "close", state=status, actor=actor)
    closed = []
    if c["case_ledger_id"]:
        sig = DECLINED_SIGNAL if status == "declined" else CASE_DONE_SIGNAL
        res = _closure().settle(conn, c["case_ledger_id"], signal=sig, actor=actor or "letters",
                                note=("Letter declined" if status == "declined"
                                      else "Every letter of this case is submitted"),
                                evidence={"case": case_id}, sweep=False)
        if res.get("action") == "closed":
            closed += res.get("ids") or []
    return closed


def accept(conn, case_id: str, *, actor: str = "", quote: str = "") -> dict:
    """The owner will write it: every asked destination is accepted. The
    case stays open; its rows are work with dates, not replies owed."""
    ensure_schema(conn)
    c = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no letter case {case_id}")
    now = _now()
    conn.execute("UPDATE letter_cases SET decision = 'accepted', updated_at = ? WHERE id = ?",
                 (now, case_id))
    conn.execute("UPDATE letter_destinations SET state = 'accepted', updated_at = ? "
                 "WHERE case_id = ? AND state = 'asked'", (now, case_id))
    _answer_open(conn, case_id, "write", "Yes", actor)
    _event(conn, case_id, "accept", actor=actor, evidence={"quote": quote} if quote else "")
    if c["case_ledger_id"]:
        _ledger_note(conn, c["case_ledger_id"], f"{now[:10]}: you will write it"
                     + (f" (\"{quote[:80]}\")" if quote else ""))
    return {"case": case_id, "decision": "accepted"}


def decline(conn, case_id: str, *, actor: str = "", quote: str = "") -> dict:
    """The owner will not write it: every open destination is declined and
    its row closes, and the case closes with its own row. The reply that
    says so is his decision too: no lane drafts it on its own."""
    ensure_schema(conn)
    c = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no letter case {case_id}")
    conn.execute("UPDATE letter_cases SET decision = 'declined', updated_at = ? WHERE id = ?",
                 (_now(), case_id))
    _answer_open(conn, case_id, "write", "No", actor)
    closed = []
    for d in conn.execute("SELECT id, state FROM letter_destinations WHERE case_id = ?",
                          (case_id,)).fetchall():
        if d["state"] in OPEN_STATES:
            closed += set_state(conn, d["id"], "declined", quote or "the owner declined",
                                actor=actor)["closed"]
    left = conn.execute("SELECT status FROM letter_cases WHERE id = ?", (case_id,)).fetchone()
    if left and left["status"] == "open":
        # No destination yet: the case and its own row close here.
        conn.execute("UPDATE letter_cases SET status = 'declined', closed_at = ?, updated_at = ? "
                     "WHERE id = ? AND status = 'open'", (_now(), _now(), case_id))
        if c["case_ledger_id"]:
            res = _closure().settle(conn, c["case_ledger_id"], signal=DECLINED_SIGNAL,
                                    actor=actor or "letters", note="Letter declined",
                                    evidence={"case": case_id, "quote": quote}, sweep=False)
            if res.get("action") == "closed":
                closed += res.get("ids") or []
    _event(conn, case_id, "decline", actor=actor, evidence={"quote": quote} if quote else "")
    return {"case": case_id, "decision": "declined", "closed": closed}


def record_draft(conn, case_id: str, path: str, dest_id: str = "", *, actor: str = "") -> dict:
    """A letter text exists: the case's base letter (or one destination's
    variant), and the destinations not yet past `drafted` move to it."""
    ensure_schema(conn)
    if not conn.execute("SELECT 1 FROM letter_cases WHERE id = ?", (case_id,)).fetchone():
        raise ValueError(f"no letter case {case_id}")
    now = _now()
    moved = []
    if dest_id:
        conn.execute("UPDATE letter_destinations SET draft = ?, updated_at = ? WHERE id = ? "
                     "AND case_id = ?", (path, now, dest_id, case_id))
        targets = [dest_id]
    else:
        conn.execute("UPDATE letter_cases SET base_letter = ?, updated_at = ? WHERE id = ?",
                     (path, now, case_id))
        targets = [r["id"] for r in conn.execute(
            "SELECT id FROM letter_destinations WHERE case_id = ?", (case_id,))]
    for t in targets:
        d = conn.execute("SELECT state FROM letter_destinations WHERE id = ?", (t,)).fetchone()
        if d and d["state"] in ("asked", "accepted"):
            set_state(conn, t, "drafted", path, actor=actor)
            moved.append(t)
    _event(conn, case_id, "draft", dest_id=dest_id, actor=actor, evidence={"path": path})
    return {"case": case_id, "path": path, "drafted": moved}


def merge(conn, from_id: str, into_id: str, *, actor: str = "") -> dict:
    """Fold case `from_id` into `into_id`: its destinations, materials,
    names and questions move; the owner's answer is remembered so the next
    mail under either name joins `into_id`."""
    ensure_schema(conn)
    a = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (into_id,)).fetchone()
    if a is None or b is None or from_id == into_id:
        raise ValueError("merge needs two different letter cases")
    now = _now()
    conn.execute("UPDATE letter_destinations SET case_id = ?, updated_at = ? WHERE case_id = ?",
                 (into_id, now, from_id))
    for r in conn.execute("SELECT id, metadata FROM ledger WHERE metadata LIKE ?",
                          (f'%"{from_id}"%',)).fetchall():
        meta = _json(r["metadata"], {})
        if isinstance(meta.get("letter"), dict) and meta["letter"].get("case") == from_id:
            meta["letter"]["case"] = into_id
            conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?",
                         (json.dumps(meta, sort_keys=True, default=str), r["id"]))
    mats = _json(b["materials"], []) + [m for m in _json(a["materials"], [])
                                        if m not in _json(b["materials"], [])]
    _absorb_identity(conn, into_id, a["person_name"], a["person_email"], keep_level=a["keep_level"])
    for alias in _json(a["aliases"], []):
        _absorb_identity(conn, into_id, alias if "@" not in alias else "",
                         alias if "@" in alias else "")
    sets = ["materials = ?", "updated_at = ?"]
    params = [json.dumps(mats[-40:]), now]
    if not b["case_ledger_id"] and a["case_ledger_id"]:
        sets.append("case_ledger_id = ?")
        params.append(a["case_ledger_id"])
    elif a["case_ledger_id"] and b["case_ledger_id"]:
        _closure().settle(conn, a["case_ledger_id"], signal="duplicate_of", actor=actor or "letters",
                          note=f"the same letter case as {b['case_ledger_id']}",
                          evidence={"duplicate_of": b["case_ledger_id"]})
    if b["decision"] == "" and a["decision"]:
        sets.append("decision = ?")
        params.append(a["decision"])
    params.append(into_id)
    conn.execute(f"UPDATE letter_cases SET {', '.join(sets)} WHERE id = ?", params)
    conn.execute("UPDATE letter_questions SET case_id = ? WHERE case_id = ?", (into_id, from_id))
    for k in _alias_keys(a["person_name"], a["person_email"]):
        remember(conn, k, into_id, "same")
    conn.execute("UPDATE letter_cases SET status = 'merged', closed_at = ?, notes = ?, "
                 "updated_at = ? WHERE id = ?", (now, f"merged into {into_id}", now, from_id))
    _event(conn, into_id, "merge", actor=actor, evidence={"from": from_id})
    _event(conn, from_id, "merged", actor=actor, evidence={"into": into_id})
    return {"into": into_id, "from": from_id}


def remember(conn, key: str, case_id: str, answer: str) -> None:
    conn.execute("INSERT OR REPLACE INTO letter_aliases (key, case_id, answer, at) VALUES (?, ?, ?, ?)",
                 (key, case_id, answer, _now()))


def _ledger_note(conn, ledger_id: str, line: str) -> None:
    r = _row(conn, ledger_id)
    if r is None:
        return
    text = f"{line}. {r['last_action'] or ''}".strip()[:2000]
    conn.execute("UPDATE ledger SET last_action = ?, updated_at = ? WHERE id = ?",
                 (text, _now(), ledger_id))


# ── questions for the owner ──────────────────────────────────────────

def question_dict(q) -> dict:
    return {"id": q["id"], "kind": q["kind"], "case": q["case_id"], "destination": q["dest_id"],
            "ledger_id": q["ledger_id"], "text": q["text"], "choices": _json(q["choices"], []),
            "payload": _json(q["payload"], {}), "asked_at": q["asked_at"],
            "answer": q["answer"], "answered_at": q["answered_at"] or ""}


def ask(conn, kind: str, case_id: str, text: str, choices, *, dest_id: str = "",
        ledger_id: str = "", payload: dict | None = None) -> str:
    """One question for the owner, asked once: the same open question on
    the same case and destination is not asked again."""
    ensure_schema(conn)
    if kind not in QUESTION_KINDS:
        raise ValueError(f"question kind must be one of {', '.join(QUESTION_KINDS)}")
    have = conn.execute("SELECT id FROM letter_questions WHERE kind = ? AND case_id = ? "
                        "AND dest_id = ? AND answer = ''", (kind, case_id, dest_id)).fetchone()
    if have:
        return have["id"]
    qid = _id("lq", kind, case_id, dest_id, _now(), os.urandom(4).hex())
    conn.execute("INSERT INTO letter_questions (id, kind, case_id, dest_id, ledger_id, text, choices, "
                 "payload, asked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (qid, kind, case_id, dest_id, ledger_id, text[:400], json.dumps(list(choices)),
                  json.dumps(payload or {}, default=str), _now()))
    return qid


def questions(conn, *, open_only: bool = True) -> list[dict]:
    ensure_schema(conn)
    sql = "SELECT * FROM letter_questions" + (" WHERE answer = ''" if open_only else "")
    return [question_dict(q) for q in conn.execute(sql + " ORDER BY asked_at")]


def _answer_open(conn, case_id: str, kind: str, answer: str, actor: str) -> None:
    conn.execute("UPDATE letter_questions SET answer = ?, answered_at = ?, actor = ? "
                 "WHERE case_id = ? AND kind = ? AND answer = ''",
                 (answer, _now(), actor, case_id, kind))


def answer(conn, question_id: str, choice: str, *, actor: str = "owner") -> dict:
    """The owner's tap on a question's button. `write`: Yes accepts, No
    declines, "Yes, ask for materials" accepts and says the next step
    (`next: ask_materials`, the work lane drafts that request: the tap is
    the owner asking for it). `join`: Same person merges the new case into
    the one named, Someone else keeps them apart; both are remembered."""
    ensure_schema(conn)
    q = conn.execute("SELECT * FROM letter_questions WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        raise ValueError(f"no letter question {question_id}")
    if q["answer"]:
        return {"question": question_id, "answer": q["answer"], "already": True}
    choices = _json(q["choices"], [])
    pick = next((c for c in choices if c.lower() == str(choice or "").strip().lower()), "")
    if not pick:
        raise ValueError(f"choice must be one of: {', '.join(choices)}")
    out: dict = {"question": question_id, "answer": pick}
    if q["kind"] == "write":
        if pick == "No":
            out.update(decline(conn, q["case_id"], actor=actor, quote="No"))
        else:
            out.update(accept(conn, q["case_id"], actor=actor, quote=pick))
            if pick == WRITE_CHOICES[1]:
                out["next"] = "ask_materials"
    else:
        payload = _json(q["payload"], {})
        other = str(payload.get("candidate") or "")
        mine = q["case_id"]
        name, email = str(payload.get("name") or ""), str(payload.get("email") or "")
        if pick == JOIN_SAME and other:
            out.update(merge(conn, mine, other, actor=actor))
        elif other:
            for k in _alias_keys(name, email):
                remember(conn, k, other, "other")
    conn.execute("UPDATE letter_questions SET answer = ?, answered_at = ?, actor = ? WHERE id = ?",
                 (pick, _now(), actor, question_id))
    return out


def ask_write(conn, case_id: str, *, ledger_id: str = "") -> str:
    """The one decision a new case brings: will you write it? Its text
    names what the request already carried and the dates."""
    c = case(conn, case_id)
    if c is None or c["decision"]:
        return ""
    who = c["person_name"] or "this person"
    have = sorted({m["kind"] for m in c["materials"]})
    dues = sorted({d["due"] for d in c["destinations"] if d["due"]})
    parts = [f"{_kind_word(c['kind']).capitalize()} for {who}. Will you write it?"]
    if have:
        parts.append("They sent: " + ", ".join(_MATERIAL_WORDS.get(k, k) for k in have) + ".")
    else:
        parts.append("They sent no files yet.")
    if dues:
        parts.append("Due " + ", ".join(_short_day(d) for d in dues[:3]) + ".")
    first = next((d["ledger_id"] for d in c["destinations"] if d["ledger_id"]), "")
    return ask(conn, "write", case_id, " ".join(parts), WRITE_CHOICES,
               ledger_id=ledger_id or c["case_ledger_id"] or first)


def ask_join(conn, case_id: str, candidate: str, name: str, email: str = "", *,
             dest_id: str = "", ledger_id: str = "") -> str:
    """Ask once whether a new mail's person is a case's person."""
    other = case(conn, candidate)
    if other is None:
        return ""
    text = (f"Is {name or 'this person'} the same person as {other['person_name'] or 'the person'} "
            f"(an open letter case)?")
    return ask(conn, "join", case_id, text, (JOIN_SAME, JOIN_OTHER), dest_id=dest_id,
               ledger_id=ledger_id, payload={"candidate": candidate, "name": name, "email": email})


_MATERIAL_WORDS = {"cv": "a CV", "statement": "a statement", "list": "a list of places",
                   "draft": "a draft letter", "transcript": "a transcript", "sample": "a paper",
                   "other": "other files"}


def _kind_word(kind: str) -> str:
    return {"recommendation": "a recommendation letter", "reference": "a reference",
            "nomination": "a nomination letter", "support": "a letter of support",
            "character": "a character letter", "evaluation": "an evaluation letter"}.get(
        kind, "a letter")


def _short_day(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d:%b} {d.day}"


# ── the hooks every reader asks ──────────────────────────────────────

def is_letter_row(row: dict | None) -> bool:
    """True for a ledger row of a letter case: one whose metadata names a
    case (`letter`). Pure: the row as ledger._row_to_dict gives it (its
    metadata merged in) or a sqlite row's dict with `metadata` as text."""
    if not isinstance(row, dict):
        return False
    link = row.get("letter")
    if link is None and isinstance(row.get("metadata"), str):
        link = _json(row["metadata"], {}).get("letter")
    if link is None and isinstance(row.get("metadata"), dict):
        link = row["metadata"].get("letter")
    return isinstance(link, dict) and bool(link.get("case"))


def is_letter_request(item: dict | None) -> bool:
    """True for anything a lane may want to reply to that is a letter
    request: a letter row, or a mail item, a thread brief or a briefing
    candidate whose record says request or reminder (the local model's
    `letter` field, or the rules over its title and ask). Pure."""
    if not isinstance(item, dict):
        return False
    if is_letter_row(item):
        return True
    for holder in (item, item.get("remote_record"), item.get("brief"), item.get("thread_brief"),
                   item.get("record")):
        if isinstance(holder, dict) and isinstance(holder.get("letter"), dict):
            role = holder["letter"].get("role")
            if role in ("request", "reminder"):
                return True
            if role in ("none", "confirmation") and holder["letter"].get("person") is not None:
                return False
    rec = item.get("remote_record") if isinstance(item.get("remote_record"), dict) else {}
    brief = item.get("brief") if isinstance(item.get("brief"), dict) else (
        item.get("thread_brief") if isinstance(item.get("thread_brief"), dict) else {})
    ask = str(item.get("ask") or rec.get("ask") or brief.get("ask") or "")
    # The ask decides when there is one; a title alone only without it.
    title = "" if ask else str(item.get("title") or item.get("subject") or brief.get("subject")
                               or item.get("description") or "")
    found = detect({"subject": title, "from": str(item.get("from") or item.get("from_email") or ""),
                    "result": {"ask": ask, "summary": "", "links": rec.get("links") or [],
                               "confidential_kind": rec.get("confidential_kind") or ""}},
                   plugin_senders=([], []))
    return bool(found) and found["role"] in ("request", "reminder")


def letter_role(row: dict | None) -> str:
    if not is_letter_row(row):
        return ""
    link = row.get("letter") if isinstance(row.get("letter"), dict) else (
        _json(row.get("metadata"), {}).get("letter") if isinstance(row.get("metadata"), str)
        else (row.get("metadata") or {}).get("letter"))
    return str((link or {}).get("role") or "destination")


def letter_signal_allowed(signal: str, entry: dict, *, note: str = "") -> bool:
    """Whether closure evidence may act on a letter row at all (close or
    propose). The owner's word or click, a submission, a decline and a
    repeat may; a reply in the thread, an archive, the date passing and a
    model's reading may not. A linked send closes a destination whose
    letter goes by email (the draft that names it carries the letter)."""
    if not is_letter_row(entry):
        return True
    try:
        c = _closure()
        user = c.USER_SIGNALS
        structural = c.STRUCTURAL_SIGNALS
    except Exception:
        user, structural = frozenset(), frozenset()
    if signal in SEND_SIGNALS:
        return _dest_method(entry) == "email"
    if signal in user:
        return not (signal == "user_handled"
                    and str(note or "").startswith(NOT_LETTER_DONE_NOTES))
    if signal in LETTER_SIGNALS or signal == "duplicate_of":
        return True
    if signal in structural:
        return False  # a detector sibling's sweep never reaches a letter row
    return False


def _dest_method(entry: dict) -> str:
    link = entry.get("letter") if isinstance(entry.get("letter"), dict) else {}
    did = str(link.get("destination") or "")
    if not did:
        return ""
    try:
        conn = connect()
    except Exception:
        return ""
    try:
        d = destination(conn, did)
    except sqlite3.Error:
        d = None
    finally:
        conn.close()
    return (d or {}).get("method") or ""


# ── reading a case ───────────────────────────────────────────────────

_OPEN_LEVELS = ("full", "summary")


def view(c: dict, level: str | None = None) -> dict:
    """The case as a hosted prompt may see it at its keep level (default:
    the case's own). Below `summary`: no person's name, no destination
    names, links, notes, materials' file names or letter paths; the counts,
    dates, kinds and states stay."""
    level = level or c.get("keep_level") or "full"
    if level in _OPEN_LEVELS:
        out = dict(c)
        if level == "summary":
            out["notes"] = ""
        return out
    dests = [{"id": d["id"], "due": d["due"], "method": d["method"], "state": d["state"],
              "ledger_id": d["ledger_id"], "submitted_at": d.get("submitted_at", "")}
             for d in c.get("destinations") or []]
    return {"id": c["id"], "kind": c.get("kind"), "status": c.get("status"),
            "decision": c.get("decision"), "opened": c.get("opened"),
            "keep_level": level, "person_name": "", "person_email": "", "notes": "",
            "materials": [{"kind": m.get("kind")} for m in c.get("materials") or []],
            "base_letter": "", "aliases": [], "case_ledger_id": c.get("case_ledger_id", ""),
            "destinations": dests, "questions": []}


def summary_line(c: dict, *, level: str | None = None, today: date | None = None) -> str:
    """One line per case: "Letter for <person>: 3 of 9 submitted, next due
    Dec 1 (2 destinations)". Below `summary` the person is not named."""
    level = level or c.get("keep_level") or "full"
    if level in _OPEN_LEVELS:
        head = f"Letter for {c.get('person_name') or c.get('person_email') or 'a person not named yet'}"
    else:
        head = "Letter (kept on your own model)"
    dests = [d for d in c.get("destinations") or [] if d.get("state") != "declined"]
    if c.get("status") == "declined" or c.get("decision") == "declined":
        return f"{head}: you said no."
    if c.get("status") == "closed":
        return f"{head}: all {len(dests)} submitted." if dests else f"{head}: done."
    if not c.get("decision") and any(q.get("kind") == "write" for q in c.get("questions") or []):
        lead = "will you write it?"
    elif not c.get("decision") and not dests:
        lead = "will you write it?"
    else:
        lead = ""
    if not dests:
        return f"{head}: {lead or 'no places to send it yet.'}"
    done = sum(1 for d in dests if d.get("state") == "submitted")
    parts = [f"{done} of {len(dests)} submitted"]
    open_dues = sorted(d["due"] for d in dests if d.get("state") != "submitted" and d.get("due"))
    if open_dues:
        nxt = open_dues[0]
        n = sum(1 for x in open_dues if x == nxt)
        today = today or date.today()
        late = nxt < today.isoformat()
        parts.append(f"{'overdue since' if late else 'next due'} {_short_day(nxt)}"
                     + (f" ({n} destinations)" if n > 1 else ""))
    undated = sum(1 for d in dests if d.get("state") != "submitted" and not d.get("due"))
    if undated:
        parts.append(f"{undated} with no date")
    line = ", ".join(parts)
    return f"{head}: {lead + ' ' if lead else ''}{line}."


# ── detection ────────────────────────────────────────────────────────

LETTER_ROLES = ("none", "request", "reminder", "confirmation")

# A letter about a person, in the words of a request. Case matters only
# where a name follows (a capital letter after "for").
_NOUNS = r"(?:recommendation|reference|nomination|support|endorsement|character|evaluation)"
LETTER_RE_I = re.compile(
    r"\bletters?\s+of\s+(?:recommendation|reference|support|nomination|evaluation|endorsement)\b"
    r"|\b" + _NOUNS + r"\s+letters?\b"
    r"|\b(?:recommendation|reference|referee|evaluation)\s+(?:request|form|questionnaire|survey)s?\b"
    r"|\b(?:independent|online|confidential)\s+(?:recommendation|reference|evaluation)\b"
    r"|\brecommender\b|\breferee\s+(?:request|form|report)\b"
    r"|\b(?:submit|complete|upload|provide|write)\s+(?:a|an|the|your|my)?\s*(?:\w+\s+)?"
    r"(?:recommendation|reference)\b(?!\s+(?:on|about|to)\b)"
    r"|\b(?:listed|named|identified|selected|added)\s+you\s+as\s+(?:a|an|their|his|her)\s+"
    r"(?:reference|referee|recommender)\b", re.I)
LETTER_NAMED = re.compile(r"\b(?i:recommendations?|references?|nomination)\s+"
                          r"(?i:for|on behalf of)\s+[A-Z][a-z]")
LETTER_ABBR = re.compile(r"\b(?:LoR|LOR)s?\b")
_LNOUN = (r"(?:recommendation|reference|letter of (?:recommendation|reference|support|nomination|"
          r"evaluation)|(?:recommendation|reference|nomination|support|evaluation) letter|letter)")
_CONFIRM = re.compile(
    r"\b(?:thank(?:s| you)\s+(?:you\s+)?for\s+(?:submitting|uploading|providing|completing|sending)"
    r"(?:\s+(?:your|a|the))?\s+(?:\w+\s+)?" + _LNOUN +
    r"|" + _LNOUN + r"\s+(?:\w+\s+){0,3}(?:has|have)\s+been\s+(?:received|submitted|uploaded|completed)"
    r"|" + _LNOUN + r"s?\s+(?:was\s+|is\s+)?(?:submitted|received|uploaded)\b"
    r"|(?:" + _LNOUN + r"|evaluation)\s+(?:upload(?:ing)?|submission)\s+(?:confirm\w*|received|complete)"
    r"|confirm(?:s|ation)?\s+(?:of\s+)?(?:your\s+|the\s+)?(?:" + _LNOUN + r"|evaluation)\s+"
    r"(?:submission|upload|received)"
    r"|(?:we(?:'ve|’ve| have)\s+)?received\s+(?:the|your)\s+(?:\w+\s+)?" + _LNOUN +
    r"\s+(?:you\s+)?(?:submitted|uploaded|for))", re.I)
_REQUEST_VERB = re.compile(
    r"\b(?:request\w*|submit\w*|complete|upload\w*|write|writ\w+|provide|asks?\b|asked|"
    r"listed you|named you|identified you|selected you|as a (?:recommender|reference|referee)|"
    r"on behalf of|would you be (?:willing|able)|will you|could you|can you|willing to|"
    r"decide whether to (?:write|accept|serve)|confirm whether you (?:will|can|would) write)\b", re.I)
_REMINDER = re.compile(r"\b(?:reminder|remind\w*|still (?:need|waiting)|not yet (?:received|submitted)|"
                       r"outstanding|overdue|past due|due (?:soon|in \d+ days?|tomorrow|today))\b", re.I)
# Mail about letters that asks the owner for none: a call for
# nominations that names no person, a manuscript decision, a committee's
# own recommendation, advice asked for, letters about a program.
_NOT_A_REQUEST = re.compile(
    r"\b(?:call for nominations?|nominations? (?:are |is )?(?:due|requested|open|sought|invited)|"
    r"(?:solicit\w*|seeking|now accepting) nominations?|nominate (?:a|an|your|one|up to)\b|"
    r"manuscript|reviewer|associate editor|recommendation (?:to|of) (?:accept|reject|revise|"
    r"the editor)|editor'?s? recommendation|committee'?s?\s+(?:\w+\s+)?recommendation|"
    r"recommendations? (?:for|on|about) (?:commercial|robots?|tools?|software|vendors?|courses?|"
    r"books?|papers?|which|the (?:course|class|committee|department))|"
    r"letters? of (?:support|endorsement) for (?:the|our|a|an) (?:\w+\s+){0,3}"
    r"(?:program|proposal|major|minor|degree|center|grant|initiative)|"
    r"offer letter|initiat\w* (?:the |all |any )?(?:letters|references|reference checks)|"
    r"(?:letters|references)\b[^.]{0,40}\binitiat\w*|"
    r"request(?:ing)? (?:the )?(?:letters|references) from)", re.I)
_AUTO_SENDER = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|donotreply|notifications?|"
                          r"mailer|admissions?|admit|apply|applications?|applicants?|recommend\w*|"
                          r"references?|referee|lor|support|system|portal|careers?|gradadm\w*|"
                          r"registration)(?:[._+-]|@)", re.I)
_LINK_WORDS = re.compile(r"(?:refer|recommend|reference|referee|lor\b|/lor|letter|evaluat|"
                         r"recommender|rec\b)", re.I)
_NAME_WORD = r"(?:[A-Z][a-z][A-Za-z'’]*(?:-[A-Z]?[a-z][A-Za-z'’]*)*|[A-Z]\.)"
_PERSON = (r"(" + _NAME_WORD + r"(?:\s+(?:" + _NAME_WORD + r"|[A-Z](?=\s)|de|da|van|von|del|della|"
           r"la|le|bin|al|ibn|dos|das)){1,4})")
_PERSON_RES = (
    re.compile(r"(?i:recommendation|reference|nomination|letter|evaluation|support|referee)s?\s+"
               r"(?i:(?:request|form|questionnaire|check|letter)\s+)?(?i:for|on behalf of|about)\s+"
               r"(?i:applicant\s+|candidate\s+|(?:your|my|our)\s+(?:former\s+|current\s+)?"
               r"(?:student|colleague|employee|tenant|client|report|intern)\s+)?" + _PERSON),
    re.compile(r"\b(?i:request|recommendation|reference|letter)\s+(?i:from)\s+" + _PERSON),
    re.compile(r"\b(?i:on behalf of)\s+" + _PERSON),
    re.compile(r"\b(?i:for)\s+(?i:applicant|candidate)\s+" + _PERSON),
    re.compile(_PERSON + r"\s+(?:has|have)\s+(?:listed|named|identified|selected|asked|requested|"
               r"invited|added)\s+you"),
    re.compile(r"\b(?i:applicant|candidate)[:\s]+" + _PERSON),
)
_DEST_RES = (
    re.compile(r"\bto\s+(?:the\s+)?([A-Z][\w&'’.,-]*(?:\s+(?:[A-Z][\w&'’.,-]*|of|for|"
               r"at|and|in|the|de)){0,9})\s+(?:via|through|using|by|before|at\b|on\b|portal|"
               r"online|with)"),
    re.compile(r"\bfor\s+(?:the\s+)?((?:University|College|Institute|School)[\w\s&'’.,-]{2,60}?)"
               r"(?:\s+(?:via|through|by|graduate|portal)|[,.;]|$)"),
    re.compile(r"\bfor\s+(?:the\s+)?([A-Z][\w&'’.-]*(?:\s+[A-Z][\w&'’.-]*){0,6})\s*$"),
)
_NOT_NAMES = {"the", "a", "an", "your", "our", "this", "that", "phd", "ms", "graduate", "admissions",
              "university", "college", "program", "school", "department", "recommendation",
              "letter", "reference", "fall", "spring", "summer", "winter", "dear", "professor",
              "dr", "prof", "you", "submit", "please", "request", "online", "no", "yes", "re",
              "fw", "fwd", "via", "for", "to", "and", "or", "of", "on", "in", "at", "by",
              "applicant", "candidate", "student", "reminder", "confirmation", "thank"}


def letterish(text: str) -> bool:
    """The words of a request for a letter about a person."""
    t = text or ""
    return bool(LETTER_RE_I.search(t) or LETTER_NAMED.search(t) or LETTER_ABBR.search(t))


_KIND_WORDS = (("nomination", r"\bnominat"), ("support", r"\b(?:letters? of support|support letters?)\b"),
               ("character", r"\bcharacter (?:letter|reference)\b"),
               ("reference", r"\b(?:reference|referee)\b"),
               ("evaluation", r"\bevaluation\b"),
               ("recommendation", r"\brecommend"))


def real_url(url: str) -> str:
    """A link with any mail scanner's wrapper taken off (a `url=` or `u=`
    query that holds the real address)."""
    u = str(url or "").strip()
    for _ in range(2):
        try:
            p = urlparse(u)
        except ValueError:
            return u
        q = parse_qs(p.query)
        inner = (q.get("url") or q.get("u") or q.get("target") or [""])[0]
        if inner.startswith("http"):
            u = unquote(inner)
            continue
        break
    return u


def _portal_link(links) -> str:
    for raw in links or []:
        u = real_url(raw if isinstance(raw, str) else (raw or {}).get("url", ""))
        if not u.startswith("http"):
            continue
        p = urlparse(u)
        tail = f"{p.path}?{p.query}"
        if _LINK_WORDS.search(tail) and not re.search(r"help|faq|unsubscribe|privacy", tail, re.I):
            return u
    return ""


def _clean_person(name: str) -> str:
    """A person's name out of a matched phrase: cut at a possessive or at a
    word that is no part of a name; two to five words."""
    out = []
    for raw in str(name or "").split():
        if re.fullmatch(r"[A-Z]\.?", raw) and (out or raw.endswith(".")):
            out.append(raw.rstrip("."))  # an initial
            continue
        w = raw.strip(".,;:")
        poss = re.match(r"^(.+?)['’]s?$", w)
        if poss and len(w) > 2:
            w = poss.group(1)
        else:
            poss = None
        if not w or w.lower() in _NOT_NAMES or (w.isupper() and len(w) > 1):
            break
        out.append(w)
        if poss:
            break
    while out and len(out[-1]) <= 2 and out[-1].endswith(".") is False and out[-1].islower():
        out.pop()
    if len(out) < 2 or any(w.lower() in ("university", "college", "institute", "school") for w in out):
        return ""
    return " ".join(out)[:80]


def find_person(text: str) -> str:
    for rx in _PERSON_RES:
        for m in rx.finditer(text or ""):
            name = _clean_person(m.group(1))
            if name:
                return name
    return ""


def find_destination(*texts: str) -> str:
    for text in texts:
        for rx in _DEST_RES:
            m = rx.search(text or "")
            if m:
                dest = m.group(1).strip(" ,.-")
                dest = re.sub(r"\s+(?:via|through|by|graduate admissions)$", "", dest, flags=re.I)
                if len(dest) > 3 and dest.lower() not in _NOT_NAMES:
                    return dest[:120]
    return ""


def letter_kind(text: str) -> str:
    for kind, pat in _KIND_WORDS:
        if re.search(pat, text or "", re.I):
            return kind
    return "recommendation"


def _plugin_senders() -> tuple[list[str], list[str]]:
    """(request senders, confirmation senders) the enabled plugins add."""
    try:
        try:
            import plugin_registry
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import plugin_registry
        rules = plugin_registry.mail_rules()
    except Exception:
        return [], []
    return (list(rules.get("letter_request_senders") or []),
            list(rules.get("letter_confirm_senders") or []))


def _sender_in(addr: str, pats) -> bool:
    a = (addr or "").lower()
    return any(p and (a == p or (p.startswith("@") and a.endswith(p)) or p in a) for p in pats)


# The local model's sensitivity kinds under which a letter request about
# a person can sit: a request with no name in it needs one of these.
_PERSON_KINDS = ("letter", "personnel", "student-record", "admissions")


def _rule_detect(subject: str, ask: str, summary: str, sender: str, conf_kind: str,
                 automated: bool, req_pats, conf_pats) -> dict | None:
    """The rules for a record with no `letter` field. A request reads as one
    in its subject or its ask (the summary only names the person); a
    confirmation may say so anywhere."""
    asked = f"{subject} {ask}"
    every = f"{subject} {ask} {summary}"
    plugin_req, plugin_conf = _sender_in(sender, req_pats), _sender_in(sender, conf_pats)
    person = find_person(ask) or find_person(subject) or find_person(summary)
    fresh = not re.match(r"\s*(?:re|fw|fwd|aw|sv)\s*:", subject, re.I)
    if plugin_conf or (_CONFIRM.search(every) and (automated or fresh)):
        # Only a service's own notice counts: a person's "thanks for
        # sending it" in a thread is no proof of which place got it.
        role = "confirmation"
    elif _CONFIRM.search(every) and not automated and not _REQUEST_VERB.search(ask):
        return None
    elif plugin_req:
        role = "reminder" if _REMINDER.search(asked) else "request"
    elif not letterish(asked):
        return None
    elif conf_kind == "manuscript-review" or (_NOT_A_REQUEST.search(asked)
                                              and not find_person(asked)):
        return None
    elif not (_REQUEST_VERB.search(asked) or automated):
        return None
    elif not person and not automated and conf_kind and conf_kind not in _PERSON_KINDS:
        # A person's own ask names no one but comes as a letter matter;
        # letters talked about in other mail (a committee, a program) do not.
        return None
    else:
        role = "reminder" if _REMINDER.search(asked) else "request"
    return {"role": role, "source": "plugin" if plugin_req or plugin_conf else "rules",
            "kind": letter_kind(asked or every), "person": person,
            "destination": find_destination(ask, subject), "due": "", "method": "unknown",
            "destinations": []}


def detect(rec: dict, *, plugin_senders: tuple | None = None) -> dict | None:
    """What one mail record says about a letter: None, or {"role":
    request | reminder | confirmation, "kind", "person", "destination",
    "due", "method", "link", "attachments", "automated", "source": model |
    rules | plugin, "destinations": [{name, due}]}.

    `rec` is an extraction record (subject, from, own, result) or a
    record's fields with `subject` and `from` beside them. The local
    model's `letter` field decides when it is there; rules fill what it
    leaves out and decide for older records. The owner's own mail is
    never a request."""
    res = rec.get("result") if isinstance(rec.get("result"), dict) else rec
    if rec.get("own") or res.get("own"):
        return None
    subject = str(rec.get("subject") or res.get("subject") or "")
    sender = str(rec.get("from") or res.get("from") or "").lower()
    ask = str(res.get("ask") or "")
    summary = str(res.get("summary") or "")
    text = " ".join((subject, ask, summary))
    links = res.get("links") or []
    atts = [a.get("name") if isinstance(a, dict) else str(a) for a in res.get("attachments") or []]
    atts = [a for a in atts if a and not re.match(r"image\d+\.(?:png|jpe?g|gif)$", a, re.I)]
    model = res.get("letter") if isinstance(res.get("letter"), dict) else None
    req_pats, conf_pats = plugin_senders if plugin_senders is not None else _plugin_senders()
    link = _portal_link(links)
    automated = bool(_AUTO_SENDER.search(sender + "@")) or bool(link)
    out = None
    if model and model.get("role") in LETTER_ROLES[1:]:
        out = {"role": model["role"], "source": "model",
               "kind": model.get("kind") if model.get("kind") in KINDS else letter_kind(text),
               "person": _clean_person(model.get("person") or "") or find_person(text),
               "destination": str(model.get("destination") or "")[:120] or find_destination(ask, subject),
               "due": _day(model.get("due") or res.get("deadline") or ""),
               "method": model.get("method") if model.get("method") in METHODS else "unknown",
               "destinations": [{"name": str(d.get("name") or "")[:120], "due": _day(d.get("due"))}
                                for d in model.get("destinations") or []
                                if isinstance(d, dict) and d.get("name")][:12]}
    elif model and model.get("role") == "none" and not (_sender_in(sender, req_pats + conf_pats)):
        return None
    if out is None:
        out = _rule_detect(subject, ask, summary, sender, res.get("confidential_kind") or "",
                           automated, req_pats, conf_pats)
        if out is None:
            return None
        out["due"] = _day(res.get("deadline") or "")
    if out.get("destination") and out.get("person") and (
            norm_name(out["person"]) in norm_name(out["destination"])):
        out["destination"] = ""
    if out["role"] in ("request", "reminder"):
        if link:
            out["method"] = "portal"
        elif out["method"] == "unknown" and re.search(r"\b(?:portal|online|secure link|via the "
                                                      r"(?:provided )?link|upload)\b", text, re.I):
            out["method"] = "portal"
        elif out["method"] == "unknown" and re.search(r"\b(?:e-?mail (?:it|the letter|your letter)|"
                                                      r"send (?:it|the letter) to)\b", text, re.I):
            out["method"] = "email"
    out.update(link=link, attachments=atts[:10], automated=automated, sender=sender,
               subject=subject[:200])
    if not out["person"] and not automated and out["role"] in ("request", "reminder"):
        out["person_is_sender"] = True
    return out


# ── ingest ───────────────────────────────────────────────────────────

def _is_senders_name(person: str, sender: str, rec: dict) -> bool:
    """The person a letter is about is the mail's own sender: the sender's
    display name matches, or the address carries the last name."""
    shown = str(rec.get("from_name") or "")
    if shown and name_match(person, shown) == "same":
        return True
    toks = _tokens(person)
    local = re.split(r"[._+-]", (sender or "").split("@")[0].lower())
    return bool(toks) and len(toks[-1]) > 2 and toks[-1] in local


def _sender_name(rec: dict) -> str:
    for p in (rec.get("result") or {}).get("people") or []:
        if isinstance(p, dict) and p.get("name") and str(p.get("role") or "").lower() in (
                "student", "colleague", "staff", "external", "employee", "applicant", ""):
            return str(p["name"])
    return str(rec.get("from_name") or "")


def ingest_record(conn, rec: dict, *, ledger_id: str = "", keep_level: str = "full",
                  found: dict | None = None, actor: str = "intake") -> dict:
    """Act on one mail record: a request or a reminder joins (or opens) its
    person's case and its destination, with the mail's ledger row linked
    (or a new one when intake left the mail out: a portal's automated
    request); a confirmation marks its destination submitted, which closes
    the row. Returns what it did: {"action": none | case | destination |
    submitted | ask_join | unmatched, ...}."""
    ensure_schema(conn)
    found = found if found is not None else detect(rec)
    if not found:
        return {"action": "none"}
    sender = str(rec.get("from") or "").lower()
    conv = str(rec.get("conversation_id") or "")
    mid = str(rec.get("id") or rec.get("message_id") or "")
    person = found.get("person") or ""
    email = ""
    if found.get("person_is_sender"):
        person = person or _sender_name(rec) or _name_from_address(sender)
        email = sender
    elif person and not found.get("automated") and _is_senders_name(person, sender, rec):
        # The person writes for their own letter: their address joins the
        # case, so a later mail of theirs that names no one still finds it.
        email = sender
    if found["role"] == "confirmation":
        return _ingest_confirmation(conn, found, sender=sender, actor=actor)
    if not person and not email:
        return {"action": "unmatched", "why": "no person named"}
    match = find_case(conn, person, email)
    new_case = not match.get("case")
    cid = ensure_case(conn, person, email, found.get("kind") or "recommendation",
                      keep_level=keep_level, actor=actor)
    out: dict = {"action": "case" if new_case else "destination", "case": cid}
    if new_case and match.get("maybe"):
        out["question"] = ask_join(conn, cid, match["maybe"][0], person, email, ledger_id=ledger_id)
        out["action"] = "ask_join"
    for name in found.get("attachments") or []:
        add_material(conn, cid, material_kind(name), name, actor=actor)
    if email and not found.get("automated"):
        # The person's own mail: it is the case's row (the decision, then
        # the umbrella), and its listed places are destinations with dates.
        c = conn.execute("SELECT * FROM letter_cases WHERE id = ?", (cid,)).fetchone()
        if ledger_id and not c["case_ledger_id"]:
            link_row(conn, ledger_id, cid, role="case")
        elif ledger_id and c["case_ledger_id"] and ledger_id != c["case_ledger_id"]:
            link_row(conn, ledger_id, cid, role="case")
            _closure().settle(conn, ledger_id, signal="duplicate_of", actor=actor,
                              note=f"the same letter case as {c['case_ledger_id']}",
                              evidence={"duplicate_of": c["case_ledger_id"]})
        # The places the person lists, with their dates; the portals' own
        # mails then join these by name, and fill in how to submit.
        for d in found.get("destinations") or []:
            add_destination(conn, cid, d["name"], d.get("due", ""), actor=actor)
        if new_case:
            out["question"] = out.get("question") or ask_write(conn, cid)
        return out
    did = add_destination(conn, cid, found.get("destination") or "", found.get("due", ""),
                          found.get("method", "unknown"), found.get("link", ""), ledger_id,
                          conversation_id=conv, sender=sender, message_id=mid, actor=actor)
    out["destination"] = did
    if new_case and not out.get("question"):
        out["question"] = ask_write(conn, cid)
    return out


def _ingest_confirmation(conn, found: dict, *, sender: str, actor: str) -> dict:
    """A portal's "your letter was received": the destination it names, by
    the person and the portal (its sender's domain or the destination's
    name). Only a sure match marks it submitted."""
    person = found.get("person") or ""
    match = find_case(conn, person) if person else {}
    cids = [match["case"]] if match.get("case") else []
    if not cids and not person:
        # No name in the confirmation: an open destination whose sender is
        # this portal's address, when exactly one waits.
        rows = conn.execute("SELECT d.id, d.case_id FROM letter_destinations d JOIN letter_cases c "
                            "ON c.id = d.case_id WHERE c.status = 'open' AND d.sender = ? AND "
                            "d.state IN ('asked','accepted','drafted','ready')", (sender,)).fetchall()
        if len(rows) == 1:
            res = set_state(conn, rows[0]["id"], "submitted",
                            f"confirmation from {sender}", actor=actor)
            return {"action": "submitted", "case": rows[0]["case_id"], **res}
        return {"action": "unmatched", "why": "no person named and no single destination"}
    for cid in cids:
        dest = same_destination(conn, cid, found.get("destination") or "", sender=sender,
                                link=found.get("link") or "")
        if not dest:
            dom = sender.rsplit("@", 1)[-1]
            rows = conn.execute("SELECT id, sender FROM letter_destinations WHERE case_id = ? AND "
                                "state IN ('asked','accepted','drafted','ready')", (cid,)).fetchall()
            same_dom = [r["id"] for r in rows if r["sender"] and _org(r["sender"]) == _org(sender)]
            if len(same_dom) == 1:
                dest = same_dom[0]
            elif not dom:
                dest = ""
        if dest:
            res = set_state(conn, dest, "submitted", f"confirmation from {sender}", actor=actor)
            return {"action": "submitted", "case": cid, **res}
    return {"action": "unmatched", "why": "no destination of the case matches"}


def _org(addr: str) -> str:
    """The organisation part of an address's domain: `a.b.example.edu` and
    `example.edu` are one."""
    dom = (addr or "").rsplit("@", 1)[-1].lower()
    parts = dom.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else dom


# A run reads the whole inbox: a request older than this opens nothing
# (its letter was likely dealt with before letter cases), and a
# confirmation older than CONFIRM_DAYS proves nothing new.
REQUEST_DAYS = 14
CONFIRM_DAYS = 30


def ingest(records: list[dict], *, rows_by_message: dict | None = None,
           levels: dict | None = None, db_path: Path | None = None, actor: str = "intake",
           now: datetime | None = None) -> list[dict]:
    """ingest_record over a run's mail records, oldest first, in one
    transaction. `rows_by_message` maps a message id to the ledger row the
    intake made for it; `levels` a message id to its keep level. Each
    message is read once (letter_events)."""
    rows_by_message = rows_by_message or {}
    levels = levels or {}
    now = now or datetime.now(timezone.utc)
    out = []
    conn = connect(db_path)
    try:
        senders = _plugin_senders()
        for rec in sorted(records, key=lambda r: str(r.get("received") or "")):
            found = detect(rec, plugin_senders=senders)
            if not found:
                continue
            got = str(rec.get("received") or "")
            limit = CONFIRM_DAYS if found["role"] == "confirmation" else REQUEST_DAYS
            if got and got < (now - timedelta(days=limit)).strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue
            mid = str(rec.get("id") or rec.get("message_id") or "")
            if mid and conn.execute("SELECT 1 FROM letter_events WHERE evidence LIKE ?",
                                    (f'%"message": "{mid}"%',)).fetchone():
                continue
            res = ingest_record(conn, rec, ledger_id=rows_by_message.get(mid, ""),
                                keep_level=levels.get(mid, "full"), found=found, actor=actor)
            if res.get("case"):
                _event(conn, res["case"], "mail", dest_id=res.get("destination") or "",
                       actor=actor, evidence={"message": mid, "role": found["role"],
                                              "action": res["action"]})
            out.append({"message": mid, **res})
        conn.commit()
    finally:
        conn.close()
    return out


# ── migration ────────────────────────────────────────────────────────

_ACCEPTED_WORDS = re.compile(r"\b(?:agreed to write|will write|i'?ll write|accepted|said yes|"
                             r"agreement to write|happy to write|glad to write|"
                             r"letter writing and submission remain)\b", re.I)
# A place and its date as a row's notes list them: "Westmoor College (Dec 1)".
_LISTED = re.compile(r"([A-Z][\w&'\u2019.-]*(?:\s+(?:[A-Z][\w&'\u2019.-]*|of|for|at|and|the)){0,7})"
                     r"\s*\((?:by\s+|due\s+)?([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\)")


def listed_destinations(text: str, today: date) -> list[dict]:
    """[{name, due}] for each "Place (Mon D)" in a row's own words."""
    led = _ledger()
    out = []
    for m in _LISTED.finditer(text or ""):
        month = led._MONTHS.get(m.group(2).lower())
        if not month:
            continue
        day = led._next_day_of(month, int(m.group(3)), today)
        name = re.sub(r"^(?:and|or|the)\s+", "", m.group(1).strip(), flags=re.I)
        if day and name and name.lower() not in _NOT_NAMES:
            out.append({"name": name[:120], "due": day.isoformat()})
    return out[:12]
_REPLY_CLOSE_SIGNALS = ("user_replied_outlook", "user_replied_gmail", "send_reply_row",
                        "reply_in_thread", "user_archived", "detector_sibling", "lapsed",
                        "date_passed")


def row_detection(row: dict) -> dict | None:
    """detect() on a ledger row's own words and its thread brief: the
    migration's reading of a row made before letter cases existed."""
    brief = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else {}
    notes = str(row.get("notes") or "")
    conf = "letter" if "[confidential: letter]" in notes else ""
    ask = ""
    m = re.search(r"Ask:\s*(.+?)(?:\s+Ask:|$)", notes)
    if m:
        ask = m.group(1)
    rec = {"subject": " ".join(x for x in (str(row.get("description") or ""),
                                          str(brief.get("subject") or "")) if x),
           "from": str(row.get("from_email") or ""), "from_name": str(row.get("from_name") or ""),
           "result": {"ask": ask or str(brief.get("ask") or ""),
                      "summary": f"{row.get('description') or ''}. {notes[:400]}",
                      "confidential_kind": conf, "deadline": str(row.get("deadline") or "")[:10],
                      "links": re.findall(r"https?://\S+", notes)[:4]}}
    found = detect(rec, plugin_senders=([], []))
    if found and not found.get("automated") and (
            not found.get("person") or _is_senders_name(found["person"], rec["from"], rec)):
        found["person"] = (found.get("person") or str(row.get("from_name") or "")
                           or _name_from_address(rec["from"]))
        found["person_is_sender"] = True
        born = str(row.get("date_identified") or row.get("created_at") or "")[:10]
        try:
            today = date.fromisoformat(born)
        except ValueError:
            today = date.today()
        found["destinations"] = found.get("destinations") or listed_destinations(notes, today)
    return found


def _name_from_address(addr: str) -> str:
    local = (addr or "").split("@")[0]
    parts = [p for p in re.split(r"[._]", local) if p.isalpha() and len(p) > 1]
    return " ".join(p.capitalize() for p in parts[:3]) if len(parts) >= 2 else ""


def migration_plan(conn, *, days: int = 21, today: date | None = None) -> dict:
    """How the open and recently closed letter rows become cases: a dry
    run that writes nothing. Each row: its reading, the case (by person) and
    the role it takes; closed rows say whether their close holds (a
    submission or the owner's word) or was a reply's or a tap's."""
    led = _ledger()
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT * FROM ledger WHERE source IS NULL AND (status != 'completed' "
                        "OR closed_at >= ?) ORDER BY created_at", (since,)).fetchall()
    people: dict[str, dict] = {}
    plan_rows = []
    for r in rows:
        entry = led._row_to_dict(r)
        if is_letter_row(entry):
            continue
        found = row_detection(entry)
        if not found or found["role"] == "confirmation":
            continue
        person = found.get("person") or ""
        key = norm_name(person) or (entry.get("from_email") or "").lower()
        if not key:
            continue
        group = None
        for k, g in people.items():
            if name_match(person, g["person"]) == "same" or k == key:
                group = g
                break
        if group is None:
            group = people.setdefault(key, {"person": person, "email": "", "rows": [],
                                            "kind": found["kind"]})
        if found.get("person_is_sender") and not group["email"]:
            group["email"] = (entry.get("from_email") or "").lower()
        closed_by = entry.get("closed_by") if isinstance(entry.get("closed_by"), dict) else {}
        signal = closed_by.get("signal") or ""
        role = "case" if found.get("person_is_sender") else "destination"
        verdict = "open"
        if r["status"] == "completed":
            if signal in _REPLY_CLOSE_SIGNALS or (signal == "user_handled" and REPLY_CARD_NOTE
                                                   in str(entry.get("last_action") or "")):
                verdict = "closed by a reply or a tap: reopen" if role == "destination" else \
                    "closed by a reply: case row stays closed"
            elif signal in ("user_confirmed",) and role == "case":
                verdict = "decision answered"
            else:
                verdict = "closed on the owner's word: submitted" if role == "destination" else \
                    "closed on the owner's word"
        item = {"id": r["id"], "status": r["status"], "role": role, "verdict": verdict,
                "listed": found.get("destinations") or [] if role == "case" else [],
                "closed_by": signal, "destination": found.get("destination") or "",
                "due": found.get("due") or (str(r["deadline"] or "")[:10]),
                "method": found.get("method") or "unknown", "link": found.get("link") or "",
                "accepted": bool(_ACCEPTED_WORDS.search(f"{r['last_action'] or ''} {r['notes'] or ''}")),
                "description": r["description"] or ""}
        group["rows"].append(item)
        plan_rows.append(item)
    cases_out = []
    for g in people.values():
        open_dests = [x for x in g["rows"] if x["role"] == "destination"
                      and (x["status"] != "completed" or x["verdict"].endswith("reopen"))]
        cases_out.append({"person": g["person"], "email": g["email"], "kind": g["kind"],
                          "accepted": any(x["accepted"] for x in g["rows"])
                          or any(x["verdict"] == "decision answered" for x in g["rows"]),
                          "rows": g["rows"], "open_destinations": len(open_dests)})
    return {"since": since, "cases": cases_out, "rows": len(plan_rows)}


def apply_migration(conn, plan: dict, *, reopen: bool = False, actor: str = "migration") -> dict:
    """Write the plan: a case per person, its rows linked (the person's own
    open row as the case row, each destination row as a destination); a
    destination row closed by a reply or a tap reopens only with
    `reopen`. Returns the counts."""
    led = _ledger()
    counts = {"cases": 0, "destinations": 0, "case_rows": 0, "reopened": 0, "submitted": 0}
    for g in plan.get("cases") or []:
        cid = ensure_case(conn, g["person"], g.get("email") or "", g.get("kind") or "recommendation",
                          actor=actor)
        counts["cases"] += 1
        for x in g["rows"]:
            r = _row(conn, x["id"])
            if r is None:
                continue
            if x["role"] == "case":
                if r["status"] != "completed":
                    link_row(conn, x["id"], cid, role="case")
                    counts["case_rows"] += 1
                    for d in x.get("listed") or []:
                        add_destination(conn, cid, d["name"], d["due"], actor=actor)
                        counts["destinations"] += 1
                continue
            reopen_it = r["status"] == "completed" and x["verdict"].endswith("reopen") and reopen
            where = {"conversation_id": r["conversation_id"] or "",
                     "sender": (r["from_email"] or "").lower()}
            if r["status"] == "completed" and not reopen_it:
                did = add_destination(conn, cid, x["destination"], x["due"], x["method"], x["link"],
                                      x["id"], actor=actor, **where)
                conn.execute("UPDATE letter_destinations SET state = 'submitted', submitted_at = ?, "
                             "evidence = ? WHERE id = ?", (r["closed_at"], "closed before letter "
                                                           "cases: " + (x["closed_by"] or "?"), did))
                counts["submitted"] += 1
                continue
            if reopen_it:
                conn.execute("UPDATE ledger SET status = 'open', closed_at = NULL, updated_at = ? "
                             "WHERE id = ?", (_now(), x["id"]))
                _closure().record_event(conn, x["id"], "reopen", signal=x["closed_by"], actor=actor,
                                        note="a letter closes on its submission, not on a reply")
                counts["reopened"] += 1
            add_destination(conn, cid, x["destination"], x["due"], x["method"], x["link"], x["id"],
                            actor=actor, **where)
            counts["destinations"] += 1
        if g.get("accepted"):
            accept(conn, cid, actor=actor, quote="recorded before letter cases")
        if _maybe_close_case(conn, cid, actor=actor) is None and not g.get("accepted"):
            if ask_write(conn, cid):
                counts["questions"] = counts.get("questions", 0) + 1
    del led
    return counts


def plan_report(plan: dict, *, public: bool = True) -> str:
    """The plan in plain lines. `public` drops names, descriptions and
    links, and shows ids only for rows whose id names no one."""
    lines = [f"Letter migration plan: rows open or closed since {plan['since']}",
             f"{len(plan['cases'])} cases from {plan['rows']} rows"]
    for n, g in enumerate(plan["cases"], 1):
        who = f"case {n}" if public else f"{g['person']} <{g.get('email') or '-'}>"
        lines.append(f"- {who}: {len(g['rows'])} rows, {g['open_destinations']} open destinations, "
                     f"decision {'accepted' if g['accepted'] else 'to ask'}")
        for x in g["rows"]:
            rid = x["id"] if (not public or _id_is_anonymous(x["id"])) else "(id names a person)"
            extra = "" if public else f" | {x['destination'] or '-'} | {x['description'][:70]}"
            listed = f", lists {len(x['listed'])} places" if x.get("listed") else ""
            lines.append(f"    {rid} {x['status']} {x['role']}: {x['verdict']}"
                         f"{' due ' + x['due'] if x['due'] else ''}{listed}{extra}")
    return "\n".join(lines)


def _id_is_anonymous(row_id: str) -> bool:
    """An intake id (a prefix and a hash) names no one; a hand-made id may."""
    return bool(re.fullmatch(r"(?:task|response|priority|deadline|thread|sched|kept|letter)-"
                             r"[A-Za-z0-9_=+/-]{6,}", row_id)) and not re.search(r"[a-z]{4,}-[a-z]{4,}",
                                                                              row_id.split("-", 1)[1])


# ── CLI ──────────────────────────────────────────────────────────────

def _out(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def _err(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


def _is_owner() -> bool:
    try:
        return bool(_ledger()._is_owner())
    except Exception:
        return False


def _actor() -> str:
    try:
        led = _ledger()
        return "cli:" + ("owner" if led._is_owner() else led._lane())
    except Exception:
        return "cli"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Letter cases: one per person, tracked to submission.",
                                allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", allow_abbrev=False)
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("get", allow_abbrev=False)
    s.add_argument("--case", required=True)
    for name in ("accept", "decline"):
        s = sub.add_parser(name, allow_abbrev=False)
        s.add_argument("--case", required=True)
        s.add_argument("--quote", default="")
    s = sub.add_parser("add-destination", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--due", default="")
    s.add_argument("--method", default="unknown", choices=METHODS)
    s.add_argument("--link", default="")
    s.add_argument("--ledger-id", default="")
    s = sub.add_parser("set-due", allow_abbrev=False)
    s.add_argument("--dest", required=True)
    s.add_argument("--due", required=True, help="YYYY-MM-DD, or \"\" for no date")
    s = sub.add_parser("materials", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--kind", required=True, choices=MATERIAL_KINDS)
    s.add_argument("--ref", required=True)
    s = sub.add_parser("record-draft", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--dest", default="")
    s = sub.add_parser("ready", allow_abbrev=False)
    s.add_argument("--dest", required=True)
    s = sub.add_parser("record-submitted", allow_abbrev=False)
    s.add_argument("--dest", required=True)
    s.add_argument("--evidence", required=True)
    s = sub.add_parser("merge", allow_abbrev=False)
    s.add_argument("--from", dest="from_id", required=True)
    s.add_argument("--into", required=True)
    sub.add_parser("questions", allow_abbrev=False)
    s = sub.add_parser("answer", allow_abbrev=False)
    s.add_argument("--question", required=True)
    s.add_argument("--choice", required=True)
    s = sub.add_parser("migrate", allow_abbrev=False)
    s.add_argument("--days", type=int, default=21)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--reopen", action="store_true",
                   help="with --apply: reopen destination rows a reply or a tap closed")
    s.add_argument("--report", default="", help="write the full report (with names) to this file")
    args = p.parse_args(argv)
    actor = _actor()
    if args.cmd == "migrate" and args.apply and not _is_owner():
        return _err("migrate --apply is the owner's step: run it from the owner's shell")
    if args.cmd == "decline" and not args.quote and not _is_owner():
        return _err("decline needs --quote with the owner's own words")
    if args.cmd == "migrate" and not args.apply:
        # A dry run opens the ledger read-only: it writes nothing at all,
        # not even the letter tables.
        led = _ledger()
        conn = led.guarded_connect(led.DB_PATH, read_only=True)
        conn.row_factory = sqlite3.Row
        try:
            plan = migration_plan(conn, days=args.days)
        finally:
            conn.close()
        if args.report:
            Path(args.report).write_text(plan_report(plan, public=False) + "\n")
        return _out({"dry_run": True, "report": plan_report(plan).splitlines()})
    conn = connect()
    try:
        if args.cmd == "list":
            found = cases(conn, open_only=not args.all)
            return _out({"count": len(found), "cases": [
                {"id": c["id"], "line": summary_line(c), "decision": c["decision"],
                 "destinations": len(c["destinations"])} for c in found]})
        if args.cmd == "get":
            c = case(conn, args.case)
            return _out({"case": c, "line": summary_line(c)}) if c else _err(f"no case {args.case}")
        if args.cmd == "accept":
            res = accept(conn, args.case, actor=actor, quote=args.quote)
        elif args.cmd == "decline":
            res = decline(conn, args.case, actor=actor, quote=args.quote)
        elif args.cmd == "add-destination":
            res = {"destination": add_destination(conn, args.case, args.name, args.due, args.method,
                                                  args.link, args.ledger_id, actor=actor)}
        elif args.cmd == "set-due":
            res = set_due(conn, args.dest, args.due, actor=actor)
        elif args.cmd == "materials":
            res = {"added": add_material(conn, args.case, args.kind, args.ref, actor=actor)}
        elif args.cmd == "record-draft":
            res = record_draft(conn, args.case, args.file, args.dest, actor=actor)
        elif args.cmd == "ready":
            res = set_state(conn, args.dest, "ready", actor=actor)
        elif args.cmd == "record-submitted":
            res = set_state(conn, args.dest, "submitted", args.evidence, actor=actor)
        elif args.cmd == "merge":
            res = merge(conn, args.from_id, args.into, actor=actor)
        elif args.cmd == "questions":
            return _out({"questions": questions(conn)})
        elif args.cmd == "answer":
            res = answer(conn, args.question, args.choice, actor=actor)
        elif args.cmd == "migrate":
            plan = migration_plan(conn, days=args.days)
            if args.report:
                Path(args.report).write_text(plan_report(plan, public=False) + "\n")
            res = apply_migration(conn, plan, reopen=args.reopen, actor=actor)
        else:
            return _err(f"unknown command {args.cmd}")
        conn.commit()
        return _out({"ok": True, **res})
    except ValueError as e:
        return _err(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
