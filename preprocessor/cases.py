#!/usr/bin/env python3
"""Work cases: a piece of work someone asked the owner for that ends with
something delivered, not with a reply.

A CASE has a KIND (case_kinds/: letter, review, form, report, and any a
plugin declares), a counterpart (who asked, or who it is about), PARTS
(each with its own due date, how it is delivered and a state),
materials, open questions for the owner and a history of events:

    asked -> accepted (or declined) -> drafted -> ready -> <delivered>

`<delivered>` is the kind's word for the last state ("submitted" for a
letter). Only delivery evidence finishes a part: a service's confirmation
mail, a sent mail that carries the deliverable, the upload tool's
success, or the owner's word or tap. A reply in the thread never closes a
case row and never raises a close card (closure.py asks
`signal_allowed`). A decline closes every part of the case.

Store: tables `cases`, `case_parts`, `case_materials`, `case_questions`,
`case_aliases` and `case_events` in the ledger database. Each open part
has one open ledger row with closes_on=submission and metadata naming the
case: `letter: {case, destination, role}` for a letter (the shape the
letter tables had), `work_case: {kind, case, part, role}` for every other
kind. The counterpart's own request, when there is one, is the case's row
(role `case`): it carries the first decision and closes with the case.
Every reader asks `is_case_row(row)`: the reply queue, the Replies card,
the closure policy, the draft gate and the briefing.

Detection (`detect`) reads one mail record with each kind whose detection
is on (letter first): the local model's `work` (or `letter`) field, then
the kind's rules. No institution or vendor is named in core; a plugin adds
sender patterns and kinds through its manifest.

Privacy: a case keeps the strictest keep level of the mail it came from.
`view()` and `summary_line()` drop names, titles, part names, links and
notes below `summary`, so a hosted prompt never sees what the level holds
back.

The letter tables of the first letter release move here with `migrate
--apply` (the owner's step). Until then they are read as they are and the
store refuses writes, so nothing is lost or written twice.

    cases.py kinds
    cases.py list [--kind K] [--all]
    cases.py get --case <id>
    cases.py open --kind K --who "<counterpart>" [--email <addr>] [--title "<what>"]
                  [--part "<name>"] [--due YYYY-MM-DD] [--method M] [--link <url>]
                  [--ledger-id <id>]
    cases.py accept --case <id> [--quote "<owner's words>"]
    cases.py decline --case <id> [--quote "<owner's words>"]
    cases.py add-part --case <id> --name <name> [--due D] [--method M] [--link L] [--ledger-id <id>]
    cases.py set-due --part <id> --due YYYY-MM-DD
    cases.py materials --case <id> --kind <kind> --ref <path or message id>
    cases.py record-draft --case <id> --file <path> [--part <id>]
    cases.py ready --part <id>
    cases.py record-delivered --part <id> --evidence "<what shows it>"
    cases.py merge --from <case> --into <case>
    cases.py questions [--kind K]
    cases.py answer --question <id> --choice "<the answer>"
    cases.py migrate [--apply] [--report <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import case_kinds as _ck
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import case_kinds as _ck

Kind = _ck.Kind
norm_name = _ck.norm_name
name_match = _ck.name_match
real_url = _ck.real_url

EA_DIR = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))

OPEN_STATES = _ck.OPEN_STATES
METHODS = _ck.METHODS
ROLES = ("case", "part")
# A closed case reopens for the same counterpart's new request within this
# many days (one season of the same work); later, a new case starts.
REOPEN_DAYS = 120
JOIN_SAME, JOIN_OTHER = "Same person", "Someone else"
NONE_OF_THESE = "None of these"
# A run reads the whole inbox: a request older than this opens nothing,
# and a confirmation older than CONFIRM_DAYS proves nothing new.
REQUEST_DAYS = 14
CONFIRM_DAYS = 30
# The most parts a "which one?" question names.
PART_CHOICES = 6
# The ledger metadata key that names a case, by kind.
LETTER_KEY, WORK_KEY = "letter", "work_case"

# The Replies card's "Not needed" means "no email reply", and Handled on a
# draft's card means the draft is dealt with; neither says the work is
# delivered. A case row is not on the Replies card, and a close with either
# note is refused too.
REPLY_CARD_NOTE = "No reply needed: the owner's click on the Replies card"
DRAFT_HANDLED_NOTE = "User marked handled in chat"
NOT_DONE_NOTES = (REPLY_CARD_NOTE, DRAFT_HANDLED_NOTE)
# A send that names the row (the EA's draft, or the owner's own send of
# it) delivers the work only when the part goes by email.
SEND_SIGNALS = frozenset({"linked_send", "user_sent_myself"})
# Why closure evidence did nothing to a case row of a kind other than a
# letter (closure.py names the letter's own).
CASE_REFUSAL = ("this work closes only when it is delivered, declined or the owner says so; "
                "a reply in its thread does not finish it")
MIGRATE_LINE = ("Letters wait for one step on the computer: python3 "
                "/home/ea-bot/ea-system/preprocessor/cases.py migrate --apply (from the owner's shell)")


class MigrationPending(RuntimeError):
    """The letter tables of the first letter release are not moved yet."""


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS cases (
        id                TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,
        subkind           TEXT NOT NULL DEFAULT '',
        counterpart_name  TEXT NOT NULL DEFAULT '',
        counterpart_email TEXT NOT NULL DEFAULT '',
        title             TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'open',
        decision          TEXT NOT NULL DEFAULT '',
        opened            TEXT NOT NULL,
        closed_at         TEXT,
        base_doc          TEXT NOT NULL DEFAULT '',
        notes             TEXT NOT NULL DEFAULT '',
        aliases           TEXT NOT NULL DEFAULT '[]',
        keep_level        TEXT NOT NULL DEFAULT 'full',
        case_ledger_id    TEXT NOT NULL DEFAULT '',
        updated_at        TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_cases_kind ON cases(kind, status)",
    """CREATE TABLE IF NOT EXISTS case_parts (
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
        delivered_at    TEXT,
        evidence        TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_case_parts_case ON case_parts(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_case_parts_ledger ON case_parts(ledger_id)",
    "CREATE INDEX IF NOT EXISTS idx_case_parts_conv ON case_parts(conversation_id)",
    """CREATE TABLE IF NOT EXISTS case_materials (
        seq      INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id  TEXT NOT NULL,
        kind     TEXT NOT NULL DEFAULT 'other',
        ref      TEXT NOT NULL,
        at       TEXT NOT NULL,
        actor    TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_case_materials_case ON case_materials(case_id)",
    """CREATE TABLE IF NOT EXISTS case_questions (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        case_id     TEXT NOT NULL DEFAULT '',
        part_id     TEXT NOT NULL DEFAULT '',
        ledger_id   TEXT NOT NULL DEFAULT '',
        text        TEXT NOT NULL,
        choices     TEXT NOT NULL DEFAULT '[]',
        payload     TEXT NOT NULL DEFAULT '{}',
        asked_at    TEXT NOT NULL,
        answer      TEXT NOT NULL DEFAULT '',
        answered_at TEXT,
        actor       TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS case_aliases (
        key      TEXT NOT NULL,
        case_id  TEXT NOT NULL,
        answer   TEXT NOT NULL,
        at       TEXT NOT NULL,
        PRIMARY KEY (key, case_id))""",
    """CREATE TABLE IF NOT EXISTS case_events (
        seq      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        case_id  TEXT NOT NULL DEFAULT '',
        part_id  TEXT NOT NULL DEFAULT '',
        kind     TEXT NOT NULL,
        state    TEXT NOT NULL DEFAULT '',
        actor    TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_case_events_case ON case_events(case_id)",
)

# The letter tables as views over the store, so a reader of the first
# letter release's tables (a query, a test) still finds them.
_LETTER_CASES = "SELECT id FROM cases WHERE kind = 'letter'"
LETTER_VIEWS = {
    "letter_cases": (
        "SELECT c.id, c.counterpart_name AS person_name, c.counterpart_email AS person_email, "
        "c.subkind AS kind, c.status, c.decision, c.opened, c.closed_at, "
        "COALESCE((SELECT json_group_array(json_object('kind', m.kind, 'ref', m.ref, 'at', m.at)) "
        "FROM case_materials m WHERE m.case_id = c.id), '[]') AS materials, "
        "c.base_doc AS base_letter, c.notes, c.aliases, c.keep_level, c.case_ledger_id, c.updated_at "
        "FROM cases c WHERE c.kind = 'letter'"),
    "letter_destinations": (
        "SELECT id, case_id, name, due, method, link, state, ledger_id, conversation_id, sender, "
        "draft, delivered_at AS submitted_at, evidence, created_at, updated_at FROM case_parts "
        f"WHERE case_id IN ({_LETTER_CASES})"),
    "letter_questions": (
        "SELECT id, kind, case_id, part_id AS dest_id, ledger_id, text, choices, payload, asked_at, "
        f"answer, answered_at, actor FROM case_questions WHERE case_id IN ({_LETTER_CASES})"),
    "letter_aliases": (
        f"SELECT key, case_id, answer, at FROM case_aliases WHERE case_id IN ({_LETTER_CASES})"),
    "letter_events": (
        "SELECT seq, ts, case_id, part_id AS dest_id, kind, state, actor, evidence FROM case_events "
        f"WHERE case_id IN ({_LETTER_CASES})"),
}
# The first letter release's tables read as the store's, for the time
# between an update and the owner's `migrate --apply` (temporary views on
# that one connection: reads see both, and every write fails).
_LEGACY_SHADOW = {
    "cases": (
        "SELECT id, 'letter' AS kind, kind AS subkind, person_name AS counterpart_name, "
        "person_email AS counterpart_email, '' AS title, status, decision, opened, closed_at, "
        "base_letter AS base_doc, notes, aliases, keep_level, case_ledger_id, updated_at "
        "FROM main.letter_cases"),
    "case_parts": (
        "SELECT id, case_id, name, due, method, link, state, ledger_id, conversation_id, sender, "
        "draft, submitted_at AS delivered_at, evidence, created_at, updated_at "
        "FROM main.letter_destinations"),
    "case_materials": (
        "SELECT NULL AS seq, l.id AS case_id, json_extract(j.value, '$.kind') AS kind, "
        "json_extract(j.value, '$.ref') AS ref, json_extract(j.value, '$.at') AS at, '' AS actor "
        "FROM main.letter_cases l, json_each(l.materials) j"),
    "case_questions": (
        "SELECT id, kind, case_id, dest_id AS part_id, ledger_id, text, choices, payload, asked_at, "
        "answer, answered_at, actor FROM main.letter_questions"),
    "case_aliases": "SELECT key, case_id, answer, at FROM main.letter_aliases",
    "case_events": (
        "SELECT seq, ts, case_id, dest_id AS part_id, kind, state, actor, evidence "
        "FROM main.letter_events"),
}


# ── modules beside this one ────────────────────────────────────────────

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


def _json(text, default):
    try:
        val = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def _id(prefix: str, *parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
    return f"{prefix}-{h}"


# ── the kinds ──────────────────────────────────────────────────────────

_KINDS_MEMO: list = [0.0, None]
KINDS_TTL_S = 60


def kinds() -> dict:
    """{name: Kind}: the core kinds (case_kinds/), then the kinds the
    enabled plugins declare (a plugin never replaces a core kind). Read
    again after KINDS_TTL_S: closure asks it for every row it judges."""
    import time
    if _KINDS_MEMO[1] is not None and time.monotonic() - _KINDS_MEMO[0] < KINDS_TTL_S:
        return dict(_KINDS_MEMO[1])
    out = dict(_ck.core_kinds())
    try:
        try:
            import plugin_registry
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import plugin_registry
        declared = plugin_registry.case_kinds()
    except Exception:
        declared = {}
    for plugin, block in sorted(declared.items()):
        for name, spec in sorted((block or {}).items()):
            k = _ck.from_manifest(plugin, name, spec)
            if k is not None and k.name not in out:
                out[k.name] = k
    _KINDS_MEMO[:] = [time.monotonic(), out]
    return dict(out)


def kind(name: str) -> Kind | None:
    return kinds().get(str(name or ""))


def _kind_of(name: str) -> Kind:
    k = kind(name)
    if k is None:
        raise ValueError(f"no kind of work named {name!r}: cases.py kinds lists them")
    return k


def detection_on(k: Kind) -> bool:
    """Whether intake opens cases of this kind from mail. The kind's
    default, unless the owner's config names the kind
    (`ea-config.json` "case_detection": {"review": true}). A kind that is
    off is opened only in chat (`cases.py open`)."""
    try:
        cfg = json.loads((EA_DIR / "config" / "ea-config.json").read_text())
        said = (cfg.get("case_detection") or {}).get(k.name)
    except (OSError, ValueError, AttributeError):
        said = None
    return bool(k.detect_default if said is None else said)


# ── the schema ─────────────────────────────────────────────────────────

def _objects(conn, schema: str = "main") -> dict:
    try:
        return {r[0]: r[1] for r in conn.execute(
            f"SELECT name, type FROM {schema}.sqlite_master WHERE type IN ('table', 'view')")}
    except sqlite3.Error:
        return {}


def legacy_tables(conn) -> list[str]:
    """The first letter release's tables that are still tables (not yet
    moved by `migrate --apply`)."""
    objs = _objects(conn)
    return [t for t in LETTER_VIEWS if objs.get(t) == "table"]


def pending_migration(conn) -> bool:
    """True while the letter tables of the first letter release hold rows
    the store has not taken in: reads see them, writes wait."""
    if "letter_cases" not in legacy_tables(conn):
        return False
    try:
        return bool(conn.execute("SELECT COUNT(*) FROM main.letter_cases").fetchone()[0])
    except sqlite3.Error:
        return False


def ensure_schema(conn: sqlite3.Connection) -> None:
    """The store's tables on any connection (a read-only one creates
    nothing and reads what is there). The letter tables become views over
    the store; while the old ones still hold rows, this connection reads
    them through temporary views and cannot write (`migrate --apply`)."""
    writable = True
    for stmt in SCHEMA:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            writable = False
            break
    legacy = legacy_tables(conn)
    if legacy and writable:
        empty = True
        for t in legacy:
            try:
                if conn.execute(f"SELECT 1 FROM main.{t} LIMIT 1").fetchone():
                    empty = False
            except sqlite3.Error:
                empty = False
        if empty:
            # An install that had the letter tables but never a case: they
            # hold nothing to move, so the views take their place now.
            for t in legacy:
                conn.execute(f"DROP TABLE main.{t}")
            legacy = []
    if not legacy:
        if writable:
            for name, sql in LETTER_VIEWS.items():
                if _objects(conn).get(name) is None:
                    conn.execute(f"CREATE VIEW IF NOT EXISTS {name} AS {sql}")
        return
    if "letter_cases" in legacy:
        _shadow(conn)


def _shadow(conn) -> None:
    temp = _objects(conn, "temp")
    main = _objects(conn)
    for name, legacy_sql in _LEGACY_SHADOW.items():
        if temp.get(name):
            continue
        sql = legacy_sql
        if main.get(name) == "table":
            sql = (f"SELECT * FROM main.{name} UNION ALL {legacy_sql}"
                   if name in ("case_materials", "case_events")
                   else f"SELECT * FROM main.{name} UNION ALL SELECT * FROM ({legacy_sql}) "
                        f"WHERE {'id' if name != 'case_aliases' else 'key || case_id'} NOT IN "
                        f"(SELECT {'id' if name != 'case_aliases' else 'key || case_id'} "
                        f"FROM main.{name})")
        conn.execute(f"CREATE TEMP VIEW IF NOT EXISTS {name} AS {sql}")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """The ledger database with the store's tables, rows as sqlite3.Row."""
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


def _writable(conn) -> None:
    if _objects(conn, "temp").get("cases") == "view":
        raise MigrationPending(MIGRATE_LINE)


def _event(conn, case_id: str, kind_: str, *, part_id: str = "", state: str = "",
           actor: str = "", evidence="") -> None:
    if not isinstance(evidence, str):
        evidence = json.dumps(evidence, sort_keys=True, default=str)
    conn.execute("INSERT INTO case_events (ts, case_id, part_id, kind, state, actor, evidence) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (_now(), case_id, part_id, kind_, state, actor, (evidence or "")[:2000]))


# ── reading the store ──────────────────────────────────────────────────

def part_dict(r) -> dict:
    return {"id": r["id"], "case": r["case_id"], "name": r["name"], "due": r["due"],
            "method": r["method"], "link": r["link"], "state": r["state"],
            "ledger_id": r["ledger_id"], "conversation_id": r["conversation_id"],
            "sender": r["sender"], "draft": r["draft"],
            "delivered_at": r["delivered_at"] or "", "evidence": r["evidence"]}


def question_dict(q) -> dict:
    return {"id": q["id"], "kind": q["kind"], "case": q["case_id"], "part": q["part_id"],
            "ledger_id": q["ledger_id"], "text": q["text"], "choices": _json(q["choices"], []),
            "payload": _json(q["payload"], {}), "asked_at": q["asked_at"],
            "answer": q["answer"], "answered_at": q["answered_at"] or ""}


def _materials(conn, case_id: str) -> list[dict]:
    rows = conn.execute("SELECT kind, ref, at FROM case_materials WHERE case_id = ? "
                        "ORDER BY at, seq", (case_id,)).fetchall()
    return [{"kind": m["kind"], "ref": m["ref"], "at": m["at"]} for m in rows][-40:]


def _case_dict(conn, r) -> dict:
    parts = [part_dict(d) for d in conn.execute(
        "SELECT * FROM case_parts WHERE case_id = ? ORDER BY "
        "CASE WHEN due = '' THEN 1 ELSE 0 END, due, created_at", (r["id"],))]
    qs = [question_dict(q) for q in conn.execute(
        "SELECT * FROM case_questions WHERE case_id = ? AND answer = '' ORDER BY asked_at",
        (r["id"],))]
    return {"id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
            "counterpart_name": r["counterpart_name"], "counterpart_email": r["counterpart_email"],
            "title": r["title"], "status": r["status"], "decision": r["decision"],
            "opened": r["opened"], "closed_at": r["closed_at"] or "",
            "materials": _materials(conn, r["id"]), "base_doc": r["base_doc"],
            "notes": r["notes"], "aliases": _json(r["aliases"], []),
            "keep_level": r["keep_level"] or "full", "case_ledger_id": r["case_ledger_id"],
            "parts": parts, "questions": qs, "messages": _messages(conn, r["id"])}


MESSAGES_CAP = 20


def _messages(conn, case_id: str) -> list[dict]:
    """The inbound mail on the case's threads from anyone but the owner,
    oldest first (a `message` event per mail, written at intake)."""
    out = []
    for e in conn.execute("SELECT evidence FROM case_events WHERE case_id = ? AND kind = 'message' "
                          "ORDER BY seq", (case_id,)):
        m = _json(e["evidence"], {})
        if m.get("message_id"):
            out.append({k: str(m.get(k) or "") for k in ("at", "from", "name", "conversation_id",
                                                         "message_id", "part")})
    return sorted(out, key=lambda m: m["at"])[-MESSAGES_CAP:]


def note_message(conn, rec: dict) -> bool:
    """Record a mail on an open case's thread from anyone but the owner as
    the case's `message` event (once per message): a new message from them
    shows on the case, since its rows owe no reply. True when recorded."""
    if rec.get("own") or (rec.get("result") or {}).get("own"):
        return False
    conv = str(rec.get("conversation_id") or "")
    mid = str(rec.get("id") or rec.get("message_id") or "")
    if not conv or not mid:
        return False
    c = case_for_conversation(conn, conv)
    if c is None:
        return False
    if conn.execute("SELECT 1 FROM case_events WHERE case_id = ? AND kind = 'message' AND "
                    "evidence LIKE ?", (c["id"], f'%"message_id": {json.dumps(mid)}%')).fetchone():
        return False
    pid = next((d["id"] for d in c["parts"] if d["conversation_id"] == conv), "")
    _event(conn, c["id"], "message", part_id=pid, actor="intake",
           evidence={"at": str(rec.get("received") or ""), "from": str(rec.get("from") or "").lower(),
                     "name": str(rec.get("from_name") or ""), "conversation_id": conv,
                     "message_id": mid, "part": pid})
    return True


def cases(conn, kind: str | None = None, open_only: bool = True) -> list[dict]:
    ensure_schema(conn)
    sql, args = "SELECT * FROM cases WHERE 1 = 1", []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if open_only:
        sql += " AND status = 'open'"
    return [_case_dict(conn, r) for r in conn.execute(sql + " ORDER BY opened", args)]


def case(conn, case_id: str) -> dict | None:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return _case_dict(conn, r) if r else None


def part(conn, part_id: str) -> dict | None:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM case_parts WHERE id = ?", (part_id,)).fetchone()
    return part_dict(r) if r else None


def case_link(row: dict | None) -> dict:
    """{kind, case, part, role} for a ledger row of a case, else {}. Pure:
    the row as ledger._row_to_dict gives it (metadata merged in) or a
    sqlite row's dict with `metadata` as text or a dict."""
    if not isinstance(row, dict):
        return {}
    meta = row.get("metadata")
    meta = _json(meta, {}) if isinstance(meta, str) else (meta if isinstance(meta, dict) else {})
    for key in (LETTER_KEY, WORK_KEY):
        link = row.get(key) if isinstance(row.get(key), dict) else meta.get(key)
        if isinstance(link, dict) and link.get("case"):
            if key == LETTER_KEY:
                role = str(link.get("role") or "destination")
                return {"kind": "letter", "case": str(link["case"]),
                        "part": str(link.get("destination") or ""),
                        "role": "case" if role == "case" else "part"}
            return {"kind": str(link.get("kind") or ""), "case": str(link["case"]),
                    "part": str(link.get("part") or ""),
                    "role": "case" if link.get("role") == "case" else "part"}
    return {}


def is_case_row(row: dict | None) -> bool:
    """True for a ledger row of a work case of any kind: its metadata names
    a case. Pure. Every reader asks this one function."""
    return bool(case_link(row))


def case_role(row: dict | None) -> str:
    return case_link(row).get("role", "")


def case_for_row(conn, ledger_id: str) -> dict | None:
    """The case a ledger row belongs to: its metadata's link, or a part or
    case that names the row."""
    ensure_schema(conn)
    r = conn.execute("SELECT metadata FROM ledger WHERE id = ?", (ledger_id,)).fetchone()
    cid = case_link({"metadata": r["metadata"]}).get("case", "") if r else ""
    if not cid:
        d = conn.execute("SELECT case_id FROM case_parts WHERE ledger_id = ?",
                         (ledger_id,)).fetchone()
        c = conn.execute("SELECT id FROM cases WHERE case_ledger_id = ?", (ledger_id,)).fetchone()
        cid = (d["case_id"] if d else "") or (c["id"] if c else "")
    return case(conn, cid) if cid else None


def case_for_conversation(conn, conversation_id: str, *, open_only: bool = True,
                          kind: str | None = None) -> dict | None:
    """The case that has a row (or a part) on this conversation."""
    if not conversation_id:
        return None
    ensure_schema(conn)

    def fits(c):
        return c and (c["status"] == "open" or not open_only) and (not kind or c["kind"] == kind)
    for r in conn.execute("SELECT id, status, metadata FROM ledger WHERE conversation_id = ? "
                          "AND source IS NULL", (conversation_id,)):
        if open_only and r["status"] == "completed":
            continue
        cid = case_link({"metadata": r["metadata"]}).get("case")
        if cid:
            c = case(conn, cid)
            if fits(c):
                return c
    for d in conn.execute("SELECT case_id FROM case_parts WHERE conversation_id = ?",
                          (conversation_id,)).fetchall():
        c = case(conn, d["case_id"])
        if fits(c):
            return c
    return None


def version(conn, case_id: str) -> str:
    """A digest that changes when anything on the case changes: a field, a
    part, a material, a question or a new event."""
    c = case(conn, case_id)
    if c is None:
        return ""
    last = conn.execute("SELECT MAX(seq) FROM case_events WHERE case_id = ?",
                        (case_id,)).fetchone()[0]
    return hashlib.sha1((json.dumps(c, sort_keys=True, default=str) + f"|{last}").encode()
                        ).hexdigest()[:16]


# ── counterparts ───────────────────────────────────────────────────────

_TITLE_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|reminder|final reminder|completed|voided|"
                           r"declined|action required|complete with \w+|please (?:review and )?sign)"
                           r"\s*:\s*)+", re.I)


def title_key(subject: str, sender: str, *, notice: bool = False) -> str:
    """The key that joins a service's mails about one item when they carry
    no id: the subject without its "Reminder:" or "Completed:" and the
    sender's organisation. "" for a subject too short to trust. A notice
    (a confirmation) has one only when such a prefix stood before the
    item's own title ("Completed: <title>"); a notice whose subject is its
    own words ("Evaluation received") names no item."""
    subject = str(subject or "")
    t = _TITLE_PREFIX.sub("", subject)
    if notice and t == subject:
        return ""
    t = " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())
    return f"title:{_org(sender)}|{t}" if len(t) >= 12 and sender else ""


def alias_keys(name: str = "", email: str = "", item: str = "") -> list[str]:
    keys = []
    if item:
        keys.append("item:" + re.sub(r"\s+", " ", item.strip().lower()))
    if email:
        keys.append("email:" + email.strip().lower())
    if norm_name(name):
        keys.append("name:" + norm_name(name))
    return keys


def find_case(conn, kind: str, name: str = "", email: str = "", *, item: str = "",
              title: str = "", open_only: bool = True) -> dict:
    """{"case": id} for a sure match, {"maybe": [ids]} for a person's name
    that may be the same, {} for none. The owner's earlier answers decide
    first: a name he said is someone else never matches that case again.
    A person's case (a letter) matches by address, then by name; any other
    kind matches only by the item's own id (a manuscript or form number)
    or by its title key (title_key: the same subject from the same
    organisation)."""
    ensure_schema(conn)
    k = _kind_of(kind)
    keys = alias_keys(name, email, item) + ([title] if title else [])
    said = {}
    for key in keys:
        for a in conn.execute("SELECT case_id, answer FROM case_aliases WHERE key = ?", (key,)):
            said[a["case_id"]] = a["answer"]
    rows = conn.execute("SELECT * FROM cases WHERE kind = ?" + (" AND status = 'open'" if open_only
                                                                 else "") + " ORDER BY opened DESC",
                        (kind,)).fetchall()
    if k.counterpart != "person":
        hits = [r["id"] for r in rows if said.get(r["id"]) in ("same", "item")]
        return {"case": hits[0]} if hits else {}
    sure, maybe = [], []
    mail = (email or "").strip().lower()
    for r in rows:
        if said.get(r["id"]) == "other":
            continue
        if said.get(r["id"]) == "same":
            sure.append(r["id"])
            continue
        aliases = _json(r["aliases"], [])
        addrs = {r["counterpart_email"]} | {a for a in aliases if "@" in a}
        if mail and mail in addrs:
            sure.append(r["id"])
            continue
        names = [r["counterpart_name"]] + [a for a in aliases if "@" not in a]
        verdicts = {name_match(name, n) for n in names if n}
        if "same" in verdicts:
            sure.append(r["id"])
        elif "maybe" in verdicts:
            maybe.append(r["id"])
    if len(sure) == 1:
        return {"case": sure[0]}
    if len(sure) > 1:
        # Two cases the facts both name: the one with this address, else
        # the oldest (`merge` folds the other by hand).
        by_mail = [cid for cid in sure if mail and any(
            r["id"] == cid and (r["counterpart_email"] == mail or mail in _json(r["aliases"], []))
            for r in rows)]
        return {"case": (by_mail or sure)[-1]}
    return {"maybe": maybe} if maybe else {}


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


def _absorb_identity(conn, cid: str, name: str, email: str, *, keep_level: str = "full") -> None:
    """Keep the other spellings of the counterpart's name and address on
    the case, and the strictest keep level of the mail it came from."""
    r = conn.execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()
    if not r:
        return
    aliases = _json(r["aliases"], [])
    changed = False
    for v in ((name or "").strip(), (email or "").strip().lower()):
        if v and v not in aliases and v not in (r["counterpart_name"], r["counterpart_email"]):
            aliases.append(v[:200])
            changed = True
    level = _stricter(r["keep_level"] or "full", keep_level or "full")
    sets, params = [], []
    if changed:
        sets.append("aliases = ?")
        params.append(json.dumps(aliases[:20]))
    if email and not r["counterpart_email"]:
        sets.append("counterpart_email = ?")
        params.append(email.strip().lower())
    if name and not r["counterpart_name"]:
        sets.append("counterpart_name = ?")
        params.append(name.strip()[:120])
    if level != (r["keep_level"] or "full"):
        sets.append("keep_level = ?")
        params.append(level)
    if sets:
        sets.append("updated_at = ?")
        params += [_now(), cid]
        conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", params)


def ensure_case(conn, kind: str, counterpart_name: str = "", counterpart_email: str = "",
                subkind: str = "", *, title: str = "", item: str = "", title_key_: str = "",
                keep_level: str = "full", actor: str = "") -> str:
    """The open case of this kind for this counterpart (a person by address
    or a sure name; any other kind by the item's id), or the same one
    closed this season, reopened, or a new one. Returns its id. A name that
    only may match opens a new case; ingest_record asks the owner."""
    ensure_schema(conn)
    _writable(conn)
    k = _kind_of(kind)
    found = find_case(conn, kind, counterpart_name, counterpart_email, item=item, title=title_key_)
    if not found.get("case"):
        old = find_case(conn, kind, counterpart_name, counterpart_email, item=item,
                        title=title_key_, open_only=False)
        r = conn.execute("SELECT * FROM cases WHERE id = ?", (old.get("case") or "",)).fetchone()
        if r is not None and r["status"] == "closed" and r["decision"] != "declined" and \
                (r["closed_at"] or "") >= (datetime.now(timezone.utc)
                                           - timedelta(days=REOPEN_DAYS)).strftime("%Y-%m-%d"):
            conn.execute("UPDATE cases SET status = 'open', closed_at = NULL, updated_at = ? "
                         "WHERE id = ?", (_now(), r["id"]))
            _event(conn, r["id"], "reopen", actor=actor)
            found = {"case": r["id"]}
    if found.get("case"):
        cid = found["case"]
        _absorb_identity(conn, cid, counterpart_name if k.counterpart == "person" else "",
                         counterpart_email if k.counterpart == "person" else "",
                         keep_level=keep_level)
        return cid
    sub = subkind if subkind in k.subkinds else (k.default_subkind if k.subkinds else
                                                 (subkind or "")[:40])
    now = _now()
    cid = _id(k.prefixes[0], norm_name(counterpart_name) or (counterpart_email or "").lower()
              or item or title, now, os.urandom(4).hex())
    conn.execute("INSERT INTO cases (id, kind, subkind, counterpart_name, counterpart_email, title, "
                 "opened, keep_level, aliases, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (cid, k.name, sub, (counterpart_name or "").strip()[:120],
                  (counterpart_email or "").strip().lower()[:200], (title or "").strip()[:200],
                  now, keep_level or "full", "[]", now))
    for key in alias_keys(item=item) + ([title_key_] if title_key_ else []):
        remember(conn, key, cid, "item")
    _event(conn, cid, "open", actor=actor, evidence={"kind": k.name, "subkind": sub})
    return cid


def remember(conn, key: str, case_id: str, answer: str) -> None:
    conn.execute("INSERT OR REPLACE INTO case_aliases (key, case_id, answer, at) VALUES (?, ?, ?, ?)",
                 (key, case_id, answer, _now()))


# ── rows and parts ─────────────────────────────────────────────────────

def _row(conn, ledger_id: str):
    return conn.execute("SELECT * FROM ledger WHERE id = ?", (ledger_id,)).fetchone() if ledger_id else None


def _kind_for_case(conn, case_id: str) -> Kind:
    r = conn.execute("SELECT kind FROM cases WHERE id = ?", (case_id,)).fetchone()
    if r is None:
        raise ValueError(f"no case {case_id}")
    return _kind_of(r["kind"])


def link_row(conn, ledger_id: str, case_id: str, part_id: str = "", *, role: str = "part") -> bool:
    """Mark a ledger row as this case's: its metadata names the case, and a
    part's row closes on its submission. The row's type stays one of the
    nine (a row a reply would finish becomes a `task`). A plugin's row is
    never a core case's."""
    r = _row(conn, ledger_id)
    if r is None or str(r["source"] or "").startswith("plugin:"):
        return False
    _writable(conn)
    k = _kind_for_case(conn, case_id)
    role = "case" if role == "case" else "part"
    meta = _json(r["metadata"], {})
    if k.name == "letter":
        meta[LETTER_KEY] = {"case": case_id, "destination": part_id,
                            "role": "case" if role == "case" else "destination"}
    else:
        meta[WORK_KEY] = {"kind": k.name, "case": case_id, "part": part_id, "role": role}
    meta["closes_on"] = "submission" if role == "part" else "manual"
    meta["response_mode"] = k.response_mode if role == "part" else meta.get("response_mode", "other")
    meta.pop("close_proposal", None)
    led = _ledger()
    rtype = r["type"]
    if led.canonical_type(rtype) in led.REPLY_TYPES:
        rtype = "task"
    conn.execute("UPDATE ledger SET metadata = ?, type = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(meta, sort_keys=True, default=str), rtype, _now(), ledger_id))
    if role == "case":
        conn.execute("UPDATE cases SET case_ledger_id = ?, updated_at = ? WHERE id = ?",
                     (ledger_id, _now(), case_id))
    return True


_OPEN_LEVELS = ("full", "summary")


def _new_row(conn, case_row, part_name: str, due: str, *, conversation_id: str = "",
             message_id: str = "", sender: str = "") -> str:
    """A ledger row for a part that has none (a service's request the
    intake left out, a part from the counterpart's own list)."""
    k = _kind_of(case_row["kind"])
    key = message_id or f"{case_row['id']}|{part_name}"
    lid = _id(k.name if k.name != "letter" else "letter", key)
    if _row(conn, lid) is None:
        now = _now()
        level = case_row["keep_level"] or "full"
        meta = {}
        if level in _OPEN_LEVELS:
            who = case_row["counterpart_name"] or case_row["title"] or (
                "someone" if k.counterpart == "person" else "a request")
            desc = f"{k.head(who)}: {part_name or k.part_noun}"
        else:
            # A kept case's row names no one: hosted lanes read the ledger.
            desc = k.kept_row_words or f"{k.noun} request (kept on your own model)"
            meta["keep_level"] = level
        conn.execute(
            "INSERT INTO ledger (id, type, status, description, from_email, source_message_id, "
            "conversation_id, date_identified, deadline, last_action, notes, metadata, "
            "created_at, updated_at) VALUES (?, 'task', 'open', ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)",
            (lid, desc[:200], sender or None, message_id or None, conversation_id or None, now,
             due or None, json.dumps(meta), now, now))
    return lid


def norm_part(name: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\b(?:the|of|at|for|program|programme|graduate|school|admissions?|"
                  r"application|phd|ms|department)\b", " ", text)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def _link_key(url: str) -> str:
    from urllib.parse import urlparse
    u = real_url(url)
    p = urlparse(u)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}?{p.query}"


def same_part(conn, case_id: str, name: str, *, sender: str = "", link: str = "",
              reuse_done: bool = True) -> str:
    """The part of this case the facts name: the same link, the same name,
    or the same sender for a part with no name. `reuse_done` False skips
    parts already delivered or declined (a request sent again is new
    work)."""
    rows = conn.execute("SELECT * FROM case_parts WHERE case_id = ?", (case_id,)).fetchall()
    if not reuse_done:
        rows = [d for d in rows if d["state"] in OPEN_STATES]
    key = norm_part(name)
    for d in rows:
        if link and d["link"] and _link_key(link) == _link_key(d["link"]):
            return d["id"]
    for d in rows:
        dk = norm_part(d["name"])
        if key and dk and (key == dk or (len(key) > 5 and (key in dk or dk in key))):
            return d["id"]
    for d in rows:
        # One service often serves many places: its address alone joins two
        # mails only when neither names a place and they carry no two links.
        if (sender and d["sender"] == sender.lower() and not key and not norm_part(d["name"])
                and not (link and d["link"])):
            return d["id"]
    # A part name the owner tied to a part before (a "which one?" answer).
    for key_ in ([f"part:{case_id}|{key}"] if key else []):
        a = conn.execute("SELECT answer FROM case_aliases WHERE key = ?", (key_,)).fetchone()
        if a and any(d["id"] == a["answer"] for d in rows):
            return a["answer"]
    return ""


def _sync_row_date(conn, part_id: str) -> None:
    """The part's row carries the part's due date."""
    d = conn.execute("SELECT * FROM case_parts WHERE id = ?", (part_id,)).fetchone()
    if not d or not d["ledger_id"] or not d["due"]:
        return
    r = _row(conn, d["ledger_id"])
    if r is not None and r["status"] != "completed" and not r["deadline"]:
        conn.execute("UPDATE ledger SET deadline = ?, updated_at = ? WHERE id = ?",
                     (d["due"], _now(), d["ledger_id"]))


def add_part(conn, case_id: str, name: str, due: str = "", method: str = "unknown",
             link: str = "", ledger_id: str = "", *, conversation_id: str = "",
             sender: str = "", message_id: str = "", actor: str = "",
             reuse_done: bool = True) -> str:
    """A part on the case, with its one open ledger row: the row given
    (linked here), else a new one. A part of the same name already on the
    case gets the new facts instead (a reminder, a service's mail for a
    place the counterpart listed). Returns its id."""
    ensure_schema(conn)
    _writable(conn)
    c = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no case {case_id}")
    k = _kind_of(c["kind"])
    method = method if method in k.methods else "unknown"
    due = _ck.day(due)
    now = _now()
    noun = k.noun.lower()
    existing = same_part(conn, case_id, name, sender=sender, link=link, reuse_done=reuse_done)
    if existing:
        d = conn.execute("SELECT * FROM case_parts WHERE id = ?", (existing,)).fetchone()
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
                # The part came from the counterpart's list (a row with no
                # mail behind it); the service's own mail row takes its
                # place, so evidence on the service's thread reaches it.
                sets["ledger_id"] = ledger_id
                link_row(conn, ledger_id, case_id, existing)
                if old_row["deadline"] and not new_row["deadline"]:
                    conn.execute("UPDATE ledger SET deadline = ? WHERE id = ?",
                                 (old_row["deadline"], ledger_id))
                _closure().settle(conn, lid, signal="duplicate_of", actor=actor or "cases",
                                  note=f"the same {noun} {k.part_noun} as {ledger_id}",
                                  evidence={"duplicate_of": ledger_id})
            else:
                # A second row for the same part (a reminder's own row): it
                # folds into the part's row.
                link_row(conn, ledger_id, case_id, existing)
                _closure().settle(conn, ledger_id, signal="duplicate_of", actor=actor or "cases",
                                  note=f"the same {noun} {k.part_noun} as {lid}",
                                  evidence={"duplicate_of": lid})
        sets["updated_at"] = now
        conn.execute(f"UPDATE case_parts SET {', '.join(x + ' = ?' for x in sets)} WHERE id = ?",
                     (*sets.values(), existing))
        _sync_row_date(conn, existing)
        return existing
    did = _id(k.prefixes[1], case_id, norm_part(name) or sender or link or now, now,
              os.urandom(4).hex())
    lid = ledger_id if ledger_id and _row(conn, ledger_id) is not None else ""
    if lid and str(_row(conn, lid)["source"] or "").startswith("plugin:"):
        lid = ""  # a plugin's row stays the plugin's
    if not lid:
        lid = _new_row(conn, c, name, due, conversation_id=conversation_id,
                       message_id=message_id, sender=sender)
    state = "accepted" if c["decision"] == "accepted" else "asked"
    r = _row(conn, lid)
    if r is not None and r["status"] == "completed":
        # Its row closed before the case knew it (the owner's word, a tap):
        # the part is done.
        state = k.delivered
    conn.execute("INSERT INTO case_parts (id, case_id, name, due, method, link, state, "
                 "ledger_id, conversation_id, sender, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (did, case_id, (name or "")[:160], due, method, (link or "")[:600], state, lid,
                  conversation_id, (sender or "").lower(), now, now))
    link_row(conn, lid, case_id, did)
    _event(conn, case_id, "part" if k.name != "letter" else "destination", part_id=did,
           state=state, actor=actor,
           evidence={"name": name, "due": due, "method": method, "ledger_id": lid})
    _sync_row_date(conn, did)
    return did


def set_due(conn, part_id: str, due: str, *, actor: str = "") -> dict:
    """A part's new date (YYYY-MM-DD, or "" for none): its row's deadline
    follows, and the change is an event with its actor (a moved date
    starts its own reminder ladder). Returns {"part", "due", "was"}."""
    ensure_schema(conn)
    _writable(conn)
    d = conn.execute("SELECT * FROM case_parts WHERE id = ?", (part_id,)).fetchone()
    if d is None:
        raise ValueError(f"no part {part_id}")
    new = _ck.day(due)
    if due and not new:
        raise ValueError("due must be YYYY-MM-DD")
    now = _now()
    conn.execute("UPDATE case_parts SET due = ?, updated_at = ? WHERE id = ?", (new, now, part_id))
    r = _row(conn, d["ledger_id"])
    if r is not None and r["status"] != "completed":
        conn.execute("UPDATE ledger SET deadline = ?, updated_at = ? WHERE id = ?",
                     (new or None, now, d["ledger_id"]))
    _event(conn, d["case_id"], "due", part_id=part_id, state=new, actor=actor,
           evidence={"from": d["due"], "to": new})
    return {"part": part_id, "due": new, "was": d["due"]}


def add_material(conn, case_id: str, kind: str, ref: str, *, actor: str = "") -> bool:
    ensure_schema(conn)
    _writable(conn)
    c = conn.execute("SELECT kind FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        return False
    k = _kind_of(c["kind"])
    kind = kind if kind in k.material_kinds else "other"
    ref = str(ref or "").strip()[:400]
    if not ref or conn.execute("SELECT 1 FROM case_materials WHERE case_id = ? AND kind = ? "
                               "AND ref = ?", (case_id, kind, ref)).fetchone():
        return False
    now = _now()
    conn.execute("INSERT INTO case_materials (case_id, kind, ref, at, actor) VALUES (?, ?, ?, ?, ?)",
                 (case_id, kind, ref, now, actor))
    conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
    _event(conn, case_id, "material", actor=actor, evidence={"kind": kind, "ref": ref})
    return True


# ── states ─────────────────────────────────────────────────────────────

def set_state(conn, part_id: str, state: str, evidence: str = "", actor: str = "") -> dict:
    """Move a part to `state`. The kind's delivered state closes its ledger
    row (closure.settle, the kind's delivered signal) and, when nothing is
    left open, the case and its own row. `declined` closes the row on the
    owner's decision. Returns {"part", "state", "closed": [ids],
    "case_closed": bool}."""
    ensure_schema(conn)
    _writable(conn)
    d = conn.execute("SELECT * FROM case_parts WHERE id = ?", (part_id,)).fetchone()
    if d is None:
        raise ValueError(f"no part {part_id}")
    k = _kind_for_case(conn, d["case_id"])
    if state not in k.states:
        raise ValueError(f"state must be one of {', '.join(k.states)}")
    now = _now()
    out = {"part": part_id, "state": state, "closed": [], "case_closed": False}
    sets = ["state = ?", "updated_at = ?"]
    params = [state, now]
    if state == k.delivered:
        sets += ["delivered_at = ?", "evidence = ?"]
        params += [now, (evidence or "")[:600]]
    params.append(part_id)
    conn.execute(f"UPDATE case_parts SET {', '.join(sets)} WHERE id = ?", params)
    _event(conn, d["case_id"], "state", part_id=part_id, state=state, actor=actor, evidence=evidence)
    if state in ("drafted", "ready", k.delivered):
        # Work written or delivered means he said yes: the question is moot.
        c = conn.execute("SELECT decision FROM cases WHERE id = ?", (d["case_id"],)).fetchone()
        if c is not None and not c["decision"]:
            conn.execute("UPDATE cases SET decision = 'accepted', updated_at = ? WHERE id = ?",
                         (now, d["case_id"]))
            if k.decision:
                _answer_open(conn, d["case_id"], k.decision.qkind, k.decision.yes[0],
                             actor or "cases")
    if state in k.done_states and d["ledger_id"]:
        delivered_sig, declined_sig, _done = k.signal_names
        sig = delivered_sig if state == k.delivered else declined_sig
        what = k.delivered if state == k.delivered else "declined"
        res = _closure().settle(conn, d["ledger_id"], signal=sig, actor=actor or "cases",
                                note=f"{k.noun} {what}: {d['name'] or k.part_noun}"
                                     + (f" ({evidence})" if evidence else ""),
                                evidence={k.name if k.name == "letter" else "part": part_id,
                                          "evidence": evidence}, sweep=False)
        if res.get("action") == "closed":
            out["closed"] += res.get("ids") or []
    done = _maybe_close_case(conn, d["case_id"], actor=actor)
    out["case_closed"] = done is not None
    out["closed"] += done or []
    return out


def record_delivered(conn, part_id: str, evidence: str, *, actor: str = "") -> dict:
    """The part is delivered: its kind's last state, with what shows it."""
    d = part(conn, part_id)
    if d is None:
        raise ValueError(f"no part {part_id}")
    return set_state(conn, part_id, _kind_for_case(conn, d["case"]).delivered, evidence, actor)


def _maybe_close_case(conn, case_id: str, *, actor: str = "") -> list[str] | None:
    """A case with every part finished closes itself, with its own row.
    Returns the ledger ids closed, or None when the case stays open."""
    c = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None or c["status"] != "open":
        return None
    k = _kind_of(c["kind"])
    states = [r["state"] for r in conn.execute(
        "SELECT state FROM case_parts WHERE case_id = ?", (case_id,))]
    if not states or any(s not in k.done_states for s in states):
        return None
    status = "declined" if all(s == "declined" for s in states) else "closed"
    conn.execute("UPDATE cases SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                 (status, _now(), _now(), case_id))
    _event(conn, case_id, "close", state=status, actor=actor)
    closed = []
    if c["case_ledger_id"]:
        _d, declined_sig, done_sig = k.signal_names
        sig = declined_sig if status == "declined" else done_sig
        res = _closure().settle(conn, c["case_ledger_id"], signal=sig, actor=actor or "cases",
                                note=(f"{k.noun} declined" if status == "declined"
                                      else f"Every {k.noun.lower()} of this case is {k.delivered}"),
                                evidence={"case": case_id}, sweep=False)
        if res.get("action") == "closed":
            closed += res.get("ids") or []
    return closed


def _ledger_note(conn, ledger_id: str, line: str) -> None:
    r = _row(conn, ledger_id)
    if r is None:
        return
    text = f"{line}. {r['last_action'] or ''}".strip()[:2000]
    conn.execute("UPDATE ledger SET last_action = ?, updated_at = ? WHERE id = ?",
                 (text, _now(), ledger_id))


def accept(conn, case_id: str, *, actor: str = "", quote: str = "") -> dict:
    """The owner will do it: every asked part is accepted. The case stays
    open; its rows are work with dates, not replies owed."""
    ensure_schema(conn)
    _writable(conn)
    c = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no case {case_id}")
    k = _kind_of(c["kind"])
    now = _now()
    conn.execute("UPDATE cases SET decision = 'accepted', updated_at = ? WHERE id = ?", (now, case_id))
    conn.execute("UPDATE case_parts SET state = 'accepted', updated_at = ? "
                 "WHERE case_id = ? AND state = 'asked'", (now, case_id))
    if k.decision:
        _answer_open(conn, case_id, k.decision.qkind, k.decision.yes[0], actor)
    _event(conn, case_id, "accept", actor=actor, evidence={"quote": quote} if quote else "")
    if c["case_ledger_id"]:
        said = "you will write it" if k.name == "letter" else "you said yes"
        _ledger_note(conn, c["case_ledger_id"], f"{now[:10]}: {said}"
                     + (f" (\"{quote[:80]}\")" if quote else ""))
    return {"case": case_id, "decision": "accepted"}


def decline(conn, case_id: str, *, actor: str = "", quote: str = "") -> dict:
    """The owner will not do it: every open part is declined and its row
    closes, and the case closes with its own row. The reply that says so
    is his decision too: no lane drafts it on its own."""
    ensure_schema(conn)
    _writable(conn)
    c = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None:
        raise ValueError(f"no case {case_id}")
    k = _kind_of(c["kind"])
    conn.execute("UPDATE cases SET decision = 'declined', updated_at = ? WHERE id = ?",
                 (_now(), case_id))
    if k.decision:
        _answer_open(conn, case_id, k.decision.qkind, k.decision.no[0], actor)
    closed = []
    for d in conn.execute("SELECT id, state FROM case_parts WHERE case_id = ?",
                          (case_id,)).fetchall():
        if d["state"] in OPEN_STATES:
            closed += set_state(conn, d["id"], "declined", quote or "the owner declined",
                                actor=actor)["closed"]
    left = conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    if left and left["status"] == "open":
        # No part yet: the case and its own row close here.
        conn.execute("UPDATE cases SET status = 'declined', closed_at = ?, updated_at = ? "
                     "WHERE id = ? AND status = 'open'", (_now(), _now(), case_id))
        if c["case_ledger_id"]:
            res = _closure().settle(conn, c["case_ledger_id"], signal=k.signal_names[1],
                                    actor=actor or "cases", note=f"{k.noun} declined",
                                    evidence={"case": case_id, "quote": quote}, sweep=False)
            if res.get("action") == "closed":
                closed += res.get("ids") or []
    _event(conn, case_id, "decline", actor=actor, evidence={"quote": quote} if quote else "")
    return {"case": case_id, "decision": "declined", "closed": closed}


def record_draft(conn, case_id: str, path: str, part_id: str = "", *, actor: str = "") -> dict:
    """A text exists: the case's base document (or one part's copy), and
    the parts not yet past `drafted` move to it."""
    ensure_schema(conn)
    _writable(conn)
    if not conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone():
        raise ValueError(f"no case {case_id}")
    now = _now()
    moved = []
    if part_id:
        conn.execute("UPDATE case_parts SET draft = ?, updated_at = ? WHERE id = ? AND case_id = ?",
                     (path, now, part_id, case_id))
        targets = [part_id]
    else:
        conn.execute("UPDATE cases SET base_doc = ?, updated_at = ? WHERE id = ?", (path, now, case_id))
        targets = [r["id"] for r in conn.execute("SELECT id FROM case_parts WHERE case_id = ?",
                                                 (case_id,))]
    for t in targets:
        d = conn.execute("SELECT state FROM case_parts WHERE id = ?", (t,)).fetchone()
        if d and d["state"] in ("asked", "accepted"):
            set_state(conn, t, "drafted", path, actor=actor)
            moved.append(t)
    _event(conn, case_id, "draft", part_id=part_id, actor=actor, evidence={"path": path})
    return {"case": case_id, "path": path, "drafted": moved}


def merge(conn, from_id: str, into_id: str, *, actor: str = "") -> dict:
    """Fold case `from_id` into `into_id` (the same kind): its parts,
    materials, names and questions move; the owner's answer is remembered
    so the next mail under either name joins `into_id`."""
    ensure_schema(conn)
    _writable(conn)
    a = conn.execute("SELECT * FROM cases WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM cases WHERE id = ?", (into_id,)).fetchone()
    if a is None or b is None or from_id == into_id or a["kind"] != b["kind"]:
        raise ValueError("merge needs two different cases of the same kind")
    k = _kind_of(b["kind"])
    now = _now()
    conn.execute("UPDATE case_parts SET case_id = ?, updated_at = ? WHERE case_id = ?",
                 (into_id, now, from_id))
    key = LETTER_KEY if k.name == "letter" else WORK_KEY
    for r in conn.execute("SELECT id, metadata FROM ledger WHERE metadata LIKE ?",
                          (f'%"{from_id}"%',)).fetchall():
        meta = _json(r["metadata"], {})
        if isinstance(meta.get(key), dict) and meta[key].get("case") == from_id:
            meta[key]["case"] = into_id
            conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?",
                         (json.dumps(meta, sort_keys=True, default=str), r["id"]))
    for m in conn.execute("SELECT kind, ref FROM case_materials WHERE case_id = ?",
                          (from_id,)).fetchall():
        if conn.execute("SELECT 1 FROM case_materials WHERE case_id = ? AND kind = ? AND ref = ?",
                        (into_id, m["kind"], m["ref"])).fetchone():
            conn.execute("DELETE FROM case_materials WHERE case_id = ? AND kind = ? AND ref = ?",
                         (from_id, m["kind"], m["ref"]))
    conn.execute("UPDATE case_materials SET case_id = ? WHERE case_id = ?", (into_id, from_id))
    _absorb_identity(conn, into_id, a["counterpart_name"], a["counterpart_email"],
                     keep_level=a["keep_level"])
    for alias in _json(a["aliases"], []):
        _absorb_identity(conn, into_id, alias if "@" not in alias else "",
                         alias if "@" in alias else "")
    sets, params = ["updated_at = ?"], [now]
    if not b["case_ledger_id"] and a["case_ledger_id"]:
        sets.append("case_ledger_id = ?")
        params.append(a["case_ledger_id"])
    elif a["case_ledger_id"] and b["case_ledger_id"]:
        _closure().settle(conn, a["case_ledger_id"], signal="duplicate_of", actor=actor or "cases",
                          note=f"the same {k.noun.lower()} case as {b['case_ledger_id']}",
                          evidence={"duplicate_of": b["case_ledger_id"]})
    if b["decision"] == "" and a["decision"]:
        sets.append("decision = ?")
        params.append(a["decision"])
    params.append(into_id)
    conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", params)
    conn.execute("UPDATE case_questions SET case_id = ? WHERE case_id = ?", (into_id, from_id))
    for key_ in alias_keys(a["counterpart_name"], a["counterpart_email"]):
        remember(conn, key_, into_id, "same")
    for al in conn.execute("SELECT key FROM case_aliases WHERE case_id = ? AND answer = 'item'",
                           (from_id,)).fetchall():
        remember(conn, al["key"], into_id, "item")
    conn.execute("UPDATE cases SET status = 'merged', closed_at = ?, notes = ?, "
                 "updated_at = ? WHERE id = ?", (now, f"merged into {into_id}", now, from_id))
    _event(conn, into_id, "merge", actor=actor, evidence={"from": from_id})
    _event(conn, from_id, "merged", actor=actor, evidence={"into": into_id})
    return {"into": into_id, "from": from_id}


# ── questions for the owner ────────────────────────────────────────────

def ask(conn, qkind: str, case_id: str, text: str, choices, *, part_id: str = "",
        ledger_id: str = "", payload: dict | None = None, once: str = "") -> str:
    """One question for the owner, asked once: the same open question on
    the same case and part is not asked again; `once` (a message id) keeps
    a question about one mail from coming back after its answer."""
    ensure_schema(conn)
    _writable(conn)
    payload = dict(payload or {})
    if once:
        payload["once"] = once
        have = conn.execute("SELECT id FROM case_questions WHERE kind = ? AND payload LIKE ?",
                            (qkind, f'%"once": {json.dumps(once)}%')).fetchone()
        if have:
            return have["id"]
    have = conn.execute("SELECT id FROM case_questions WHERE kind = ? AND case_id = ? "
                        "AND part_id = ? AND answer = ''", (qkind, case_id, part_id)).fetchone()
    if have and not once:
        return have["id"]
    prefix = "q"
    if case_id:
        try:
            prefix = _kind_for_case(conn, case_id).prefixes[2]
        except ValueError:
            pass
    qid = _id(prefix, qkind, case_id, part_id, _now(), os.urandom(4).hex())
    conn.execute("INSERT INTO case_questions (id, kind, case_id, part_id, ledger_id, text, choices, "
                 "payload, asked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (qid, qkind, case_id, part_id, ledger_id, text[:400], json.dumps(list(choices)),
                  json.dumps(payload, default=str), _now()))
    return qid


def questions(conn, *, open_only: bool = True, kind: str | None = None) -> list[dict]:
    ensure_schema(conn)
    sql = "SELECT q.* FROM case_questions q LEFT JOIN cases c ON c.id = q.case_id WHERE 1 = 1"
    args: list = []
    if open_only:
        sql += " AND q.answer = ''"
    if kind:
        sql += " AND (c.kind = ? OR (q.case_id = '' AND q.payload LIKE ?))"
        args += [kind, f'%"kind": "{kind}"%']
    return [question_dict(q) for q in conn.execute(sql + " ORDER BY q.asked_at", args)]


def _answer_open(conn, case_id: str, qkind: str, answer_: str, actor: str) -> None:
    conn.execute("UPDATE case_questions SET answer = ?, answered_at = ?, actor = ? "
                 "WHERE case_id = ? AND kind = ? AND answer = ''",
                 (answer_, _now(), actor, case_id, qkind))


def answer(conn, question_id: str, choice: str, *, actor: str = "owner") -> dict:
    """The owner's tap on a question's button.
      decision (the kind's first question): a `yes` choice accepts, a `no`
        choice declines, and a choice with a next step says it (`next`).
      join: Same person merges the new case into the one named, Someone
        else keeps them apart; both are remembered.
      part ("which one?" for a confirmation that matched no part for
        sure): the part named is delivered, the mail's own words for it
        are remembered for that part; None of these delivers nothing."""
    ensure_schema(conn)
    _writable(conn)
    q = conn.execute("SELECT * FROM case_questions WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        raise ValueError(f"no question {question_id}")
    if q["answer"]:
        return {"question": question_id, "answer": q["answer"], "already": True}
    choices = _json(q["choices"], [])
    pick = next((c for c in choices if c.lower() == str(choice or "").strip().lower()), "")
    if not pick:
        raise ValueError(f"choice must be one of: {', '.join(choices)}")
    out: dict = {"question": question_id, "answer": pick}
    payload = _json(q["payload"], {})
    if q["kind"] == "join":
        other = str(payload.get("candidate") or "")
        name, email = str(payload.get("name") or ""), str(payload.get("email") or "")
        if pick == JOIN_SAME and other:
            out.update(merge(conn, q["case_id"], other, actor=actor))
        elif other:
            for key in alias_keys(name, email):
                remember(conn, key, other, "other")
    elif q["kind"] == "part":
        target = (payload.get("parts") or {}).get(pick, "")
        if target and part(conn, target):
            said = str(payload.get("said") or "")
            d = part(conn, target)
            if said and norm_part(said):
                remember(conn, f"part:{d['case']}|{norm_part(said)}", d["case"], target)
            if payload.get("sender"):
                remember(conn, f"sender:{d['case']}|{payload['sender']}", d["case"], target)
            out.update(record_delivered(conn, target, str(payload.get("evidence") or
                                                          "the owner said which one"),
                                        actor=actor))
    else:
        k = _kind_for_case(conn, q["case_id"])
        dec = k.decision
        if dec and pick in dec.no:
            out.update(decline(conn, q["case_id"], actor=actor, quote=pick))
        else:
            out.update(accept(conn, q["case_id"], actor=actor, quote=pick))
            if dec and dec.then.get(pick):
                out["next"] = dec.then[pick]
    conn.execute("UPDATE case_questions SET answer = ?, answered_at = ?, actor = ? WHERE id = ?",
                 (pick, _now(), actor, question_id))
    return out


def _short_day(d: str) -> str:
    try:
        x = date.fromisoformat(d)
    except ValueError:
        return d
    return f"{x:%b} {x.day}"


def ask_decision(conn, case_id: str, *, ledger_id: str = "") -> str:
    """The one decision a new case brings (the kind's question). Its text
    names what the request already carried and the dates. "" for a kind
    with no first decision, or a case already decided."""
    c = case(conn, case_id)
    if c is None or c["decision"]:
        return ""
    k = _kind_of(c["kind"])
    if not k.decision:
        return ""
    have = sorted({m["kind"] for m in c["materials"]})
    dues = sorted({d["due"] for d in c["parts"] if d["due"]})
    what = SUBKIND_WORDS.get(k.name, {}).get(c["subkind"]) or f"a {k.noun.lower()}"
    if c["counterpart_name"] or k.counterpart == "person":
        lead = f"{what[0].upper() + what[1:]} for {c['counterpart_name'] or 'this person'}."
    else:
        lead = f"{k.noun}: {c['title'] or 'a request'}."
    texts = [f"{lead} {k.decision.question}"]
    if have:
        words = MATERIAL_WORDS.get(k.name, {})
        texts.append("They sent: " + ", ".join(words.get(x, x) for x in have) + ".")
    else:
        texts.append("They sent no files yet.")
    if dues:
        texts.append("Due " + ", ".join(_short_day(x) for x in dues[:3]) + ".")
    first = next((d["ledger_id"] for d in c["parts"] if d["ledger_id"]), "")
    return ask(conn, k.decision.qkind, case_id, " ".join(texts), k.decision.choices,
               ledger_id=ledger_id or c["case_ledger_id"] or first)


SUBKIND_WORDS = {"letter": {"recommendation": "a recommendation letter", "reference": "a reference",
                            "nomination": "a nomination letter", "support": "a letter of support",
                            "character": "a character letter", "evaluation": "an evaluation letter"}}
MATERIAL_WORDS = {"letter": {"cv": "a CV", "statement": "a statement", "list": "a list of places",
                             "draft": "a draft letter", "transcript": "a transcript",
                             "sample": "a paper", "other": "other files"}}


def ask_join(conn, case_id: str, candidate: str, name: str, email: str = "", *,
             part_id: str = "", ledger_id: str = "") -> str:
    """Ask once whether a new mail's person is a case's person."""
    other = case(conn, candidate)
    if other is None:
        return ""
    noun = _kind_of(other["kind"]).noun.lower()
    text = (f"Is {name or 'this person'} the same person as "
            f"{other['counterpart_name'] or 'the person'} (an open {noun} case)?")
    return ask(conn, "join", case_id, text, (JOIN_SAME, JOIN_OTHER), part_id=part_id,
               ledger_id=ledger_id, payload={"candidate": candidate, "name": name, "email": email})


def ask_part(conn, candidates: list[dict], *, message_id: str, sender: str = "", said: str = "",
             evidence: str = "", kind: str = "") -> str:
    """A confirmation that matched no part for sure: ask once which part it
    was, with the open parts it may belong to (nearest date first), and
    None of these. Never a guess: nothing is delivered until he answers."""
    if not candidates:
        return ""
    labels: dict = {}
    for d in candidates[:PART_CHOICES]:
        c = case(conn, d["case"])
        k = _kind_of(c["kind"])
        level = c.get("keep_level") or "full"
        if level in _OPEN_LEVELS:
            who = c["counterpart_name"] or c["title"] or k.noun
            label = f"{who}: {d['name'] or k.part_noun}"
        else:
            label = f"{k.noun} {len(labels) + 1}"
        if d["due"]:
            label += f" (due {_short_day(d['due'])})"
        while label in labels:
            label += "."
        labels[label[:80]] = d["id"]
    first = case(conn, candidates[0]["case"])
    k = _kind_of(first["kind"])
    level = min((case(conn, d["case"]).get("keep_level") or "full" for d in candidates),
                key=lambda x: ("local", "facts", "summary", "full").index(x)
                if x in ("local", "facts", "summary", "full") else 3)
    frm = sender.split("@")[-1] if sender and level in _OPEN_LEVELS else "a service"
    text = (f"A {k.delivered} notice from {frm} matches no {k.part_noun} for sure. "
            f"Which one was it?")
    one_case = len({d["case"] for d in candidates}) == 1
    return ask(conn, "part", candidates[0]["case"] if one_case else "", text,
               list(labels) + [NONE_OF_THESE],
               payload={"parts": labels, "sender": sender, "said": said[:120],
                        "evidence": evidence[:300], "kind": kind or k.name},
               once=message_id or f"{sender}|{said}")


# ── closure: what may finish a case row ────────────────────────────────

def signal_allowed(signal: str, entry: dict, *, note: str = "") -> bool:
    """Whether closure evidence may act on a case row at all (close or
    propose). The owner's word or click, the kind's delivery, decline or
    case-done signal, and a repeat may; a reply in the thread, an archive,
    the date passing and a model's reading may not. A linked send closes a
    part whose method the kind lets a send deliver (the draft that names
    it carries the work)."""
    link = case_link(entry)
    if not link:
        return True
    k = kind(link["kind"]) or kind("letter")
    try:
        c = _closure()
        user = c.USER_SIGNALS
    except Exception:
        user = frozenset()
    if signal in SEND_SIGNALS:
        return bool(k) and _part_method(link) in k.send_delivers
    if signal in user:
        return not (signal == "user_handled" and str(note or "").startswith(NOT_DONE_NOTES))
    if signal == "duplicate_of" or (k and signal in k.signal_names):
        return True
    return False


def _part_method(link: dict) -> str:
    pid = link.get("part") or ""
    if not pid or link.get("role") == "case":
        return ""
    try:
        conn = connect()
    except Exception:
        return ""
    try:
        d = part(conn, pid)
    except sqlite3.Error:
        d = None
    finally:
        conn.close()
    return (d or {}).get("method") or ""


def refusal(entry: dict) -> str:
    """Why closure did nothing to this case row, in plain words."""
    link = case_link(entry)
    if link.get("kind") == "letter":
        return ("a letter closes only on its submission, a decline or the owner's word; "
                "a reply in its thread does not finish it")
    return CASE_REFUSAL


# ── reading a case for a prompt or a line ──────────────────────────────

def view(c: dict, level: str | None = None) -> dict:
    """The case as a hosted prompt may see it at its keep level (default:
    the case's own). Below `summary`: no counterpart, no title, no part
    names, links, notes, materials' file names or document paths, and no
    questions (their words name people); the counts, dates, kinds and
    states stay."""
    level = level or c.get("keep_level") or "full"
    if level in _OPEN_LEVELS:
        out = dict(c)
        if level == "summary":
            out["notes"] = ""
        return out
    parts = [{"id": d["id"], "due": d["due"], "method": d["method"], "state": d["state"],
              "ledger_id": d["ledger_id"], "delivered_at": d.get("delivered_at", "")}
             for d in c.get("parts") or []]
    return {"id": c["id"], "kind": c.get("kind"), "subkind": c.get("subkind", ""),
            "status": c.get("status"), "decision": c.get("decision"), "opened": c.get("opened"),
            "keep_level": level, "counterpart_name": "", "counterpart_email": "", "title": "",
            "notes": "", "materials": [{"kind": m.get("kind")} for m in c.get("materials") or []],
            "base_doc": "", "aliases": [], "case_ledger_id": c.get("case_ledger_id", ""),
            "parts": parts, "questions": [],
            "messages": [{k: m.get(k, "") for k in ("at", "conversation_id", "message_id", "part")}
                         for m in c.get("messages") or []]}


def _head(c: dict, k: Kind) -> str:
    """"Letter for <person>", "Form for <who asked>", or with no one
    named, "Form: <its title>"."""
    if k.counterpart == "person":
        return k.head(c.get("counterpart_name") or c.get("counterpart_email")
                      or "a person not named yet")
    if c.get("counterpart_name"):
        return k.head(c["counterpart_name"])
    if c.get("title"):
        return f"{k.noun}: {c['title'][:80]}"
    return k.head("someone not named yet")


def summary_line(c: dict, *, level: str | None = None, today: date | None = None) -> str:
    """One line per case. A letter: "Letter for <person>: 3 of 9 submitted,
    next due Dec 1 (2 destinations)." A one-part case of another kind:
    "Form for <office>: form due Dec 1." Below `summary` no one is named."""
    k = kind(c.get("kind") or "") or _kind_of("letter")
    level = level or c.get("keep_level") or "full"
    head = _head(c, k) if level in _OPEN_LEVELS else k.kept_head()
    parts = [d for d in c.get("parts") or [] if d.get("state") != "declined"]
    if c.get("status") == "declined" or c.get("decision") == "declined":
        return f"{head}: you said no."
    if c.get("status") == "closed":
        return f"{head}: all {len(parts)} {k.delivered}." if parts else f"{head}: done."
    asks = k.decision is not None and not c.get("decision") and (
        any(q.get("kind") == k.decision.qkind for q in c.get("questions") or []) or not parts)
    lead = (k.decision.question[0].lower() + k.decision.question[1:]) if asks else ""
    if not parts:
        return f"{head}: {lead or k.no_parts_words or f'no {k.part_nouns} yet.'}"
    today = today or date.today()
    if k.name != "letter" and len(parts) == 1:
        d = parts[0]
        if d.get("state") == k.delivered:
            body = f"{k.part_noun} {k.delivered}"
        elif d.get("due"):
            late = d["due"] < today.isoformat()
            body = f"{k.part_noun} {'overdue since' if late else 'due'} {_short_day(d['due'])}"
        else:
            body = f"{k.part_noun} with no date"
        return f"{head}: {lead + ' ' if lead else ''}{body}."
    done = sum(1 for d in parts if d.get("state") == k.delivered)
    bits = [f"{done} of {len(parts)} {k.delivered}"]
    open_dues = sorted(d["due"] for d in parts if d.get("state") != k.delivered and d.get("due"))
    if open_dues:
        nxt = open_dues[0]
        n = sum(1 for x in open_dues if x == nxt)
        late = nxt < today.isoformat()
        bits.append(f"{'overdue since' if late else 'next due'} {_short_day(nxt)}"
                    + (f" ({n} {k.part_nouns})" if n > 1 else ""))
    undated = sum(1 for d in parts if d.get("state") != k.delivered and not d.get("due"))
    if undated:
        bits.append(f"{undated} with no date")
    return f"{head}: {lead + ' ' if lead else ''}{', '.join(bits)}."


def moves(c: dict) -> list[dict]:
    """The next steps this case allows now, for buttons a briefing offers
    (each validated against this list): {"move", "part"?}. `accept` /
    `decline` while undecided; per open part `draft`, `ready`, `delivered`,
    `set_due` (no date yet) and `open_link` (a link); `ask_materials` when
    the kind wants materials the case lacks; `decline` while open."""
    k = kind(c.get("kind") or "")
    if k is None or c.get("status") != "open":
        return []
    out: list[dict] = []
    if k.decision and not c.get("decision"):
        out += [{"move": "accept"}, {"move": "decline"}]
    have = {m.get("kind") for m in c.get("materials") or []}
    if k.wanted_materials and not set(k.wanted_materials) <= have:
        out.append({"move": "ask_materials"})
    for d in c.get("parts") or []:
        if d.get("state") not in OPEN_STATES:
            continue
        if d["state"] in ("asked", "accepted"):
            out.append({"move": "draft", "part": d["id"]})
        if d["state"] == "drafted":
            out.append({"move": "ready", "part": d["id"]})
        out.append({"move": "delivered", "part": d["id"]})
        if not d.get("due"):
            out.append({"move": "set_due", "part": d["id"]})
        if d.get("link"):
            out.append({"move": "open_link", "part": d["id"]})
    if c.get("decision") and not any(m["move"] == "decline" for m in out):
        out.append({"move": "decline"})
    return out


# ── detection ──────────────────────────────────────────────────────────

WORK_ROLES = ("none", "request", "reminder", "confirmation")


def _record(rec: dict) -> tuple[dict, str, str]:
    res = rec.get("result") if isinstance(rec.get("result"), dict) else rec
    subject = str(rec.get("subject") or res.get("subject") or "")
    sender = str(rec.get("from") or res.get("from") or "").lower()
    return res, subject, sender


def model_work(res: dict) -> dict | None:
    """The local model's `work` field when it names a kind and a role."""
    w = res.get("work") if isinstance(res, dict) else None
    if isinstance(w, dict) and w.get("role") in WORK_ROLES[1:] and \
            w.get("kind") not in (None, "", "none"):
        return w
    return None


def _with_model(k: Kind, rec: dict, found: dict | None, w: dict | None) -> dict | None:
    """The kind's rules and the model's work field together. The model's
    field fills facts the rules leave out (who asked, the item's id, the
    date); it opens a case on its own only for a kind that trusts it (one
    whose model field was measured: Kind.trust_model)."""
    if not w or w.get("kind") not in (k.name,) + tuple(k.model_kinds):
        return found
    if found is None:
        if not k.trust_model:
            return None
        res, subject, sender = _record(rec)
        found = {"role": w["role"], "case_kind": k.name, "kind": "", "source": "model",
                 "counterpart": "", "part": "", "item": "", "due": "", "method": "unknown",
                 "link": "", "automated": bool(_ck.AUTO_SENDER.search(sender + "@")),
                 "sender": sender, "subject": subject[:200], "attachments": []}
    found = dict(found)
    for key in ("counterpart", "part", "item"):
        if not found.get(key) and w.get(key):
            found[key] = str(w[key])[:120]
    if not found.get("due") and w.get("due"):
        found["due"] = _ck.day(w["due"])
    return found


def rule_detect(k: Kind, rec: dict) -> dict | None:
    """A kind's declared rules (Kind.rules) over one record: None, or the
    found dict ({"role", "case_kind", "kind" (the subkind), "counterpart",
    "part", "item", "due", "method", "link", "automated", "sender",
    "subject", "source"}). A request reads as one in the subject or the
    ask; a confirmation counts only from a service's own notice."""
    rules = k.rules
    if rules is None:
        return None
    res, subject, sender = _record(rec)
    if rec.get("own") or res.get("own"):
        return None
    ask_ = str(res.get("ask") or "")
    summary = str(res.get("summary") or "")
    asked = f"{subject} {ask_}"
    every = f"{asked} {summary}"
    links = res.get("links") or []
    automated = bool(_ck.AUTO_SENDER.search(sender + "@"))
    fresh = not _ck.REPLY_SUBJECT.match(subject)
    conf_kind = str(res.get("confidential_kind") or "")
    if conf_kind and conf_kind in rules.blocked_kinds:
        return None
    if rules.exclude and rules.rx("exclude").search(every):
        return None
    role = ""
    confirm_rx = rules.rx("confirm")
    # A service's notice says so in its subject (a stale ask the model kept
    # does not undo it); in the summary only when nothing is asked.
    if _ck.sender_in(sender, rules.confirm_senders) or (confirm_rx and (automated or fresh) and (
            confirm_rx.search(subject) or (confirm_rx.search(summary) and not ask_.strip()))):
        role = "confirmation"
    else:
        req_rx = rules.rx("request")
        by_sender = _ck.sender_in(sender, rules.senders)
        if not (by_sender or (req_rx and req_rx.search(asked))):
            return None
        if rules.automated_only and not (automated or by_sender):
            return None
        if rules.fresh_only and not fresh and not by_sender:
            return None
        if rules.states and str(res.get("state") or "") not in rules.states:
            return None
        role = "reminder" if _ck.REMINDER.search(asked) else "request"
    item = ""
    if rules.item:
        m = rules.rx("item").search(every)
        item = (m.group(1) if m and m.groups() else (m.group(0) if m else "")) or ""
    link = ""
    for raw in links:
        u = real_url(raw if isinstance(raw, str) else (raw or {}).get("url", ""))
        if u.startswith("http") and not re.search(r"help|faq|unsubscribe|privacy", u, re.I):
            link = u
            break
    return {"role": role, "case_kind": k.name, "kind": "", "source": "rules",
            "counterpart": str(rec.get("from_name") or ""),
            "part": "", "item": item.strip()[:80], "due": _ck.day(res.get("deadline") or ""),
            "method": "portal" if link else "unknown", "link": link, "automated": automated,
            "sender": sender, "subject": subject[:200], "attachments": [
                a.get("name") if isinstance(a, dict) else str(a)
                for a in res.get("attachments") or []][:10]}


def _org_name(addr: str) -> str:
    dom = (addr or "").rsplit("@", 1)[-1].lower()
    parts = [p for p in dom.split(".") if p not in ("www", "mail", "email", "e", "mg", "em")]
    return ".".join(parts[-2:]) if len(parts) >= 2 else dom


def detect(rec: dict, *, kinds_: list[str] | None = None, plugin_senders: tuple | None = None
           ) -> dict | None:
    """What one mail record says about a piece of work: None, or the found
    dict of the first kind that reads it (letter first). `kinds_` names the
    kinds to try (default: those whose detection is on). The model's
    `work` field decides its kind when it names one; a record the model
    called no work at all is left to the letter rules alone (the letter
    field decides there). The owner's own mail is never a request."""
    registry = kinds()
    names = kinds_ if kinds_ is not None else [n for n, k in registry.items() if detection_on(k)]
    res, _subject, _sender = _record(rec)
    if rec.get("own") or res.get("own"):
        return None
    w = model_work(res)
    if w is not None and w.get("kind") in names:
        names = [w["kind"]] + [n for n in names if n != w["kind"]]
    for name in names:
        k = registry.get(name)
        if k is None:
            continue
        if k.detect is not None:
            found = (k.detect(rec, plugin_senders=plugin_senders) if k.name == "letter"
                     else k.detect(rec))
        else:
            found = _with_model(k, rec, rule_detect(k, rec), w)
        if found:
            found = dict(found)
            found["case_kind"] = k.name
            if k.name == "letter":
                found.setdefault("counterpart", found.get("person") or "")
                found.setdefault("part", found.get("destination") or "")
                found.setdefault("parts", found.get("destinations") or [])
            return found
    return None


def is_case_request(item: dict | None) -> bool:
    """True for anything a lane may want to reply to that is a work
    request: a case row, a letter request (the letter rules), or a mail
    item whose record's `work` field says request or reminder for a kind
    whose detection is on. Pure but for the kinds' config."""
    if not isinstance(item, dict):
        return False
    if is_case_row(item):
        return True
    for holder in (item, item.get("remote_record"), item.get("brief"), item.get("thread_brief"),
                   item.get("record")):
        w = model_work(holder) if isinstance(holder, dict) else None
        if w and w.get("role") in ("request", "reminder"):
            k = kind(w.get("kind"))
            if k is not None and detection_on(k):
                return True
    return False


# ── ingest ─────────────────────────────────────────────────────────────

def _is_senders_name(person: str, sender: str, rec: dict) -> bool:
    """The person a case is about is the mail's own sender: the sender's
    display name matches, or the address carries the last name."""
    shown = str(rec.get("from_name") or "")
    if shown and name_match(person, shown) == "same":
        return True
    toks = _ck.name_tokens(person)
    local = re.split(r"[._+-]", (sender or "").split("@")[0].lower())
    return bool(toks) and len(toks[-1]) > 2 and toks[-1] in local


def _sender_name(rec: dict) -> str:
    for p in (rec.get("result") or {}).get("people") or []:
        if isinstance(p, dict) and p.get("name") and str(p.get("role") or "").lower() in (
                "student", "colleague", "staff", "external", "employee", "applicant", ""):
            return str(p["name"])
    return str(rec.get("from_name") or "")


def name_from_address(addr: str) -> str:
    local = (addr or "").split("@")[0]
    parts = [p for p in re.split(r"[._]", local) if p.isalpha() and len(p) > 1]
    return " ".join(p.capitalize() for p in parts[:3]) if len(parts) >= 2 else ""


def material_kind(k: Kind, filename: str) -> str:
    try:
        from case_kinds import letter as _letter
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor.case_kinds import letter as _letter
    if k.name == "letter":
        return _letter.material_kind(filename)
    low = re.sub(r"[_.\-]+", " ", str(filename or "").lower())
    for kind_, pat in (("form", r"\bform\b"), ("instructions", r"\b(?:instructions?|guidelines?|"
                                                             r"template)\b")):
        if kind_ in k.material_kinds and re.search(pat, low):
            return kind_
    return "document" if "document" in k.material_kinds else "other"


def ingest_record(conn, rec: dict, *, ledger_id: str = "", keep_level: str = "full",
                  found: dict | None = None, actor: str = "intake") -> dict:
    """Act on one mail record: a request or a reminder joins (or opens) its
    case and its part, with the mail's ledger row linked (or a new one when
    intake left the mail out: a service's automated request); a
    confirmation marks its part delivered, which closes the row, or asks
    which part when it matches none for sure. Returns what it did:
    {"action": none | case | part | delivered | ask_join | ask_part |
    unmatched, ...}."""
    ensure_schema(conn)
    found = found if found is not None else detect(rec)
    if not found:
        return {"action": "none"}
    _writable(conn)
    k = _kind_of(found.get("case_kind") or "letter")
    row = _row(conn, ledger_id)
    if row is not None and str(row["source"] or "").startswith("plugin:"):
        return {"action": "none", "why": "a plugin's row"}
    sender = str(rec.get("from") or "").lower()
    conv = str(rec.get("conversation_id") or "")
    mid = str(rec.get("id") or rec.get("message_id") or "")
    if found["role"] == "confirmation":
        return _ingest_confirmation(conn, k, found, sender=sender, conversation_id=conv,
                                    message_id=mid, actor=actor)
    if k.counterpart != "person":
        return _ingest_work(conn, k, rec, found, ledger_id=ledger_id, keep_level=keep_level,
                            sender=sender, conv=conv, mid=mid, actor=actor)
    person = found.get("counterpart") or found.get("person") or ""
    email = ""
    if found.get("person_is_sender"):
        person = person or _sender_name(rec) or name_from_address(sender)
        email = sender
    elif person and not found.get("automated") and _is_senders_name(person, sender, rec):
        # The person writes for their own case: their address joins it, so
        # a later mail of theirs that names no one still finds it.
        email = sender
    if not person and not email:
        return {"action": "unmatched", "why": "no person named"}
    match = find_case(conn, k.name, person, email)
    new_case = not match.get("case")
    cid = ensure_case(conn, k.name, person, email, found.get("kind") or "",
                      keep_level=keep_level, actor=actor)
    out: dict = {"action": "case" if new_case else "part", "case": cid}
    if new_case and match.get("maybe"):
        out["question"] = ask_join(conn, cid, match["maybe"][0], person, email, ledger_id=ledger_id)
        out["action"] = "ask_join"
    for name in found.get("attachments") or []:
        add_material(conn, cid, material_kind(k, name), name, actor=actor)
    if email and not found.get("automated"):
        # The person's own mail: it is the case's row (the decision, then
        # the umbrella), and its listed places are parts with dates.
        c = conn.execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()
        if ledger_id and not c["case_ledger_id"]:
            link_row(conn, ledger_id, cid, role="case")
        elif ledger_id and c["case_ledger_id"] and ledger_id != c["case_ledger_id"]:
            link_row(conn, ledger_id, cid, role="case")
            _closure().settle(conn, ledger_id, signal="duplicate_of", actor=actor,
                              note=f"the same {k.noun.lower()} case as {c['case_ledger_id']}",
                              evidence={"duplicate_of": c["case_ledger_id"]})
        for d in found.get("parts") or []:
            add_part(conn, cid, d["name"], d.get("due", ""), actor=actor)
        if new_case:
            out["question"] = out.get("question") or ask_decision(conn, cid)
        return out
    did = add_part(conn, cid, found.get("part") or "", found.get("due", ""),
                   found.get("method", "unknown"), found.get("link", ""), ledger_id,
                   conversation_id=conv, sender=sender, message_id=mid, actor=actor)
    out["part"] = did
    if new_case and not out.get("question"):
        out["question"] = ask_decision(conn, cid)
    return out


def _ingest_work(conn, k: Kind, rec: dict, found: dict, *, ledger_id: str, keep_level: str,
                 sender: str, conv: str, mid: str, actor: str) -> dict:
    """A request of a kind whose counterpart is not a person (a review, a
    form, a report): the case of the same item (by its id, or a part on
    the same thread), else a new one, with one part for the deliverable."""
    item = found.get("item") or ""
    c = case_for_conversation(conn, conv, kind=k.name)
    cid = c["id"] if c else ""
    new_case = False
    tkey = title_key(found.get("subject") or "", sender)
    if not cid and (item or tkey):
        cid = find_case(conn, k.name, item=item, title=tkey).get("case") or ""
    if not cid:
        title = _TITLE_PREFIX.sub("", found.get("subject") or "").strip()
        cid = ensure_case(conn, k.name, found.get("counterpart") or "", sender, found.get("kind") or "",
                          title=title, item=item, title_key_=tkey, keep_level=keep_level,
                          actor=actor)
        new_case = True
    else:
        _absorb_identity(conn, cid, "", "", keep_level=keep_level)
    for name in found.get("attachments") or []:
        if name and not re.match(r"image\d+\.(?:png|jpe?g|gif)$", name, re.I):
            add_material(conn, cid, material_kind(k, name), name, actor=actor)
    name, link = found.get("part") or "", found.get("link", "")
    open_parts = _open_parts(conn, k.name, cid)
    if not name and len(open_parts) == 1:
        # One thing to return: a reminder or a second notice is about it.
        name, link = open_parts[0]["name"], open_parts[0]["link"] or link
    did = add_part(conn, cid, name, found.get("due", ""), found.get("method", "unknown"), link,
                   ledger_id, conversation_id=conv, sender=sender, message_id=mid, actor=actor,
                   reuse_done=False)
    out = {"action": "case" if new_case else "part", "case": cid, "part": did}
    if new_case:
        q = ask_decision(conn, cid)
        if q:
            out["question"] = q
    return out


def _org(addr: str) -> str:
    """The organisation part of an address's domain: `a.b.example.edu` and
    `example.edu` are one."""
    dom = (addr or "").rsplit("@", 1)[-1].lower()
    parts = dom.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else dom


def _open_parts(conn, kind_: str, case_id: str = "") -> list:
    sql = ("SELECT d.* FROM case_parts d JOIN cases c ON c.id = d.case_id WHERE c.status = 'open' "
           "AND c.kind = ? AND d.state IN ('asked','accepted','drafted','ready')")
    args = [kind_]
    if case_id:
        sql += " AND d.case_id = ?"
        args.append(case_id)
    return conn.execute(sql + " ORDER BY CASE WHEN d.due = '' THEN 1 ELSE 0 END, d.due",
                        args).fetchall()


def _ingest_confirmation(conn, k: Kind, found: dict, *, sender: str, conversation_id: str = "",
                         message_id: str = "", actor: str) -> dict:
    """A service's "received": the part it names, by the counterpart and the
    service (its sender's organisation, the part's name, the thread, the
    item's id, or an earlier answer of the owner's). Only a sure match
    marks it delivered; anything less asks the owner which part, once."""
    evidence = f"confirmation from {sender}"
    said = found.get("part") or found.get("destination") or ""

    def deliver(pid, cid):
        res = set_state(conn, pid, k.delivered, evidence, actor=actor)
        return {"action": "delivered" if k.name != "letter" else "submitted", "case": cid, **res}

    def ask_which(cands, why):
        if not cands:
            return {"action": "unmatched", "why": why}
        qid = ask_part(conn, [part_dict(d) for d in cands], message_id=message_id, sender=sender,
                       said=said, evidence=evidence, kind=k.name)
        return {"action": "ask_part", "question": qid, "why": why}

    # The thread or the item's own id name the case for sure.
    by_thread = case_for_conversation(conn, conversation_id, kind=k.name) if conversation_id else None
    tkey = (title_key(found.get("subject") or "", sender, notice=True)
            if k.counterpart != "person" else "")
    if by_thread is None and (found.get("item") or tkey):
        hit = find_case(conn, k.name, item=found.get("item") or "", title=tkey)
        by_thread = case(conn, hit["case"]) if hit.get("case") else None
    if by_thread is not None:
        rows = _open_parts(conn, k.name, by_thread["id"])
        if len(rows) == 1:
            return deliver(rows[0]["id"], by_thread["id"])
        pid = same_part(conn, by_thread["id"], said, sender=sender, link=found.get("link") or "")
        if pid and any(r["id"] == pid for r in rows):
            return deliver(pid, by_thread["id"])
        return ask_which(rows, "more than one open part on the thread")
    person = found.get("counterpart") or found.get("person") or ""
    if k.counterpart == "person" and person:
        match = find_case(conn, k.name, person)
        cids = [match["case"]] if match.get("case") else []
        if not cids:
            maybe = [r for cid in match.get("maybe") or [] for r in _open_parts(conn, k.name, cid)]
            return ask_which(maybe, "no case of that person for sure")
        cid = cids[0]
        dest = same_part(conn, cid, said, sender=sender, link=found.get("link") or "")
        rows = _open_parts(conn, k.name, cid)
        if not dest:
            remembered = conn.execute("SELECT answer FROM case_aliases WHERE key = ?",
                                      (f"sender:{cid}|{sender}",)).fetchone()
            same_org = [r["id"] for r in rows if r["sender"] and _org(r["sender"]) == _org(sender)]
            if len(same_org) == 1:
                dest = same_org[0]
            elif remembered and any(r["id"] == remembered["answer"] for r in rows) and \
                    not norm_part(said):
                dest = ""  # an earlier answer names a part, but a new notice may be another
        if dest and any(r["id"] == dest for r in rows):
            return deliver(dest, cid)
        if dest:
            return {"action": "unmatched", "why": f"its {k.part_noun} is no longer open"}
        return ask_which(rows, f"no {k.part_noun} of the case matches")
    if k.counterpart != "person" and (tkey or found.get("item")):
        return {"action": "unmatched", "why": "its item matches no open case"}
    # No name in the confirmation: an open part whose sender is this
    # service's address, when exactly one waits; else ask among them.
    rows = [r for r in _open_parts(conn, k.name) if r["sender"] == sender]
    if len(rows) == 1:
        return deliver(rows[0]["id"], rows[0]["case_id"])
    if rows:
        return ask_which(rows, "more than one open part from this service")
    if tkey or found.get("item"):
        # It names its own item, and no open case has it: not ours.
        return {"action": "unmatched", "why": "its item matches no open case"}
    same_org = [r for r in _open_parts(conn, k.name) if r["sender"] and _org(r["sender"]) == _org(sender)]
    if same_org:
        return ask_which(same_org, "no part from this address, some from its organisation")
    return {"action": "unmatched", "why": "no person named and no single part"}


def ingest(records: list[dict], *, rows_by_message: dict | None = None,
           levels: dict | None = None, db_path: Path | None = None, actor: str = "intake",
           now: datetime | None = None, kinds_: list[str] | None = None) -> list[dict]:
    """ingest_record over a run's mail records, oldest first. Each message
    is read once (case_events). While the letter tables wait for `migrate
    --apply`, nothing is read: the mail stays unread for the run after it."""
    rows_by_message = rows_by_message or {}
    levels = levels or {}
    now = now or datetime.now(timezone.utc)
    out = []
    conn = connect(db_path)
    try:
        if pending_migration(conn):
            return [{"action": "waiting", "why": MIGRATE_LINE}]
        senders = None
        try:
            from case_kinds import letter as _letter
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor.case_kinds import letter as _letter
        senders = _letter._plugin_senders()
        for rec in sorted(records, key=lambda r: str(r.get("received") or "")):
            found = detect(rec, kinds_=kinds_, plugin_senders=senders)
            if not found or found["role"] != "confirmation":
                note_message(conn, rec)
            if not found:
                continue
            got = str(rec.get("received") or "")
            limit = CONFIRM_DAYS if found["role"] == "confirmation" else REQUEST_DAYS
            if got and got < (now - timedelta(days=limit)).strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue
            mid = str(rec.get("id") or rec.get("message_id") or "")
            if mid and conn.execute("SELECT 1 FROM case_events WHERE evidence LIKE ?",
                                    (f'%"message": "{mid}"%',)).fetchone():
                continue
            res = ingest_record(conn, rec, ledger_id=rows_by_message.get(mid, ""),
                                keep_level=levels.get(mid, "full"), found=found, actor=actor)
            if res.get("action") != "none":
                _event(conn, res.get("case") or "", "mail", part_id=res.get("part") or "",
                       actor=actor, evidence={"message": mid, "role": found["role"],
                                              "kind": found["case_kind"],
                                              "action": res["action"]})
            out.append({"message": mid, "kind": found["case_kind"], **res})
        conn.commit()
    finally:
        conn.close()
    return out


def open_case(conn, kind_: str, who: str, *, email: str = "", title: str = "", part_name: str = "",
              due: str = "", method: str = "unknown", link: str = "", ledger_id: str = "",
              actor: str = "") -> dict:
    """A case the owner or the EA opens in chat (any kind, detection on or
    off): the case, one part with its row (the row named, or a new one),
    and the kind's first decision unless the owner already said yes (a
    case opened at his word starts accepted)."""
    k = _kind_of(kind_)
    cid = ensure_case(conn, k.name, who, email, title=title, actor=actor)
    out = {"case": cid}
    row = _row(conn, ledger_id)
    if part_name or ledger_id or due or k.counterpart != "person":
        out["part"] = add_part(conn, cid, part_name, due, method, link, ledger_id,
                               conversation_id=(row["conversation_id"] or "") if row else "",
                               sender=(row["from_email"] or "") if row else "", actor=actor)
    accept(conn, cid, actor=actor, quote="opened in chat")
    return out


# ── migration of the first letter release's tables ─────────────────────

def migration_plan(conn) -> dict:
    """What `migrate --apply` moves: the rows of the letter tables that the
    store does not hold yet, per table, and each case with its parts.
    Reads only."""
    legacy = legacy_tables(conn)
    objs = _objects(conn)
    have = set()
    if objs.get("cases") == "table":
        have = {r[0] for r in conn.execute("SELECT id FROM main.cases")}
    plan = {"legacy": legacy, "tables": {}, "cases": [], "done": not legacy}
    if not legacy:
        return plan
    for t in legacy:
        plan["tables"][t] = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
    if "letter_cases" in legacy:
        for r in conn.execute("SELECT * FROM main.letter_cases ORDER BY opened").fetchall():
            dests = conn.execute("SELECT state, due, ledger_id FROM main.letter_destinations "
                                 "WHERE case_id = ?", (r["id"],)).fetchall() \
                if "letter_destinations" in legacy else []
            plan["cases"].append({
                "id": r["id"], "person": r["person_name"], "status": r["status"],
                "decision": r["decision"], "keep_level": r["keep_level"],
                "materials": len(_json(r["materials"], [])), "parts": len(dests),
                "states": sorted(d["state"] for d in dests), "in_store": r["id"] in have,
                "rows": [d["ledger_id"] for d in dests if d["ledger_id"]]
                + ([r["case_ledger_id"]] if r["case_ledger_id"] else [])})
    return plan


def plan_report(plan: dict, *, public: bool = True) -> str:
    if plan.get("done"):
        return "Nothing to move: the letter tables are views over the case store already."
    lines = ["Work cases migration: the letter tables move into the case store",
             "Rows to move: " + ", ".join(f"{t} {n}" for t, n in plan["tables"].items())]
    for n, c in enumerate(plan["cases"], 1):
        who = f"case {n}" if public else f"{c['person']} ({c['id']})"
        lines.append(f"- {who}: {c['status']}, decision {c['decision'] or 'to ask'}, "
                     f"{c['parts']} destinations ({', '.join(c['states']) or 'none'}), "
                     f"{c['materials']} materials, keep level {c['keep_level']}"
                     + (", already in the store" if c["in_store"] else ""))
    lines.append("The ledger rows stay as they are; their metadata already names these case ids.")
    lines.append("The old tables are kept as letter_*_v1 beside the store.")
    return "\n".join(lines)


def apply_migration(conn) -> dict:
    """Move the letter tables' rows into the store in one transaction, keep
    the old tables as `letter_*_v1`, and put the views in their place.
    Idempotent: a second run finds nothing to move; a failed run moves
    nothing. Returns the counts moved."""
    for t in ("cases", "case_parts", "case_materials", "case_questions", "case_aliases",
              "case_events"):
        conn.execute(f"DROP VIEW IF EXISTS temp.{t}")
    for stmt in SCHEMA:
        conn.execute(stmt)
    legacy = legacy_tables(conn)
    counts = {"cases": 0, "parts": 0, "materials": 0, "questions": 0, "aliases": 0, "events": 0}
    if not legacy:
        return {"moved": counts, "already": True}
    conn.execute("BEGIN IMMEDIATE")
    try:
        def run(sql, key):
            counts[key] += conn.execute(sql).rowcount or 0
        if "letter_cases" in legacy:
            run("INSERT OR IGNORE INTO main.cases (id, kind, subkind, counterpart_name, "
                "counterpart_email, title, status, decision, opened, closed_at, base_doc, notes, "
                "aliases, keep_level, case_ledger_id, updated_at) SELECT id, 'letter', kind, "
                "person_name, person_email, '', status, decision, opened, closed_at, base_letter, "
                "notes, aliases, keep_level, case_ledger_id, updated_at FROM main.letter_cases",
                "cases")
            run("INSERT INTO main.case_materials (case_id, kind, ref, at, actor) SELECT l.id, "
                "COALESCE(json_extract(j.value, '$.kind'), 'other'), json_extract(j.value, '$.ref'), "
                "COALESCE(json_extract(j.value, '$.at'), l.opened), 'migration' "
                "FROM main.letter_cases l, json_each(l.materials) j "
                "WHERE json_extract(j.value, '$.ref') IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM main.case_materials m WHERE m.case_id = l.id "
                "AND m.kind = COALESCE(json_extract(j.value, '$.kind'), 'other') "
                "AND m.ref = json_extract(j.value, '$.ref'))", "materials")
        if "letter_destinations" in legacy:
            run("INSERT OR IGNORE INTO main.case_parts (id, case_id, name, due, method, link, state, "
                "ledger_id, conversation_id, sender, draft, delivered_at, evidence, created_at, "
                "updated_at) SELECT id, case_id, name, due, method, link, state, ledger_id, "
                "conversation_id, sender, draft, submitted_at, evidence, created_at, updated_at "
                "FROM main.letter_destinations", "parts")
        if "letter_questions" in legacy:
            run("INSERT OR IGNORE INTO main.case_questions (id, kind, case_id, part_id, ledger_id, "
                "text, choices, payload, asked_at, answer, answered_at, actor) SELECT id, kind, "
                "case_id, dest_id, ledger_id, text, choices, payload, asked_at, answer, "
                "answered_at, actor FROM main.letter_questions", "questions")
        if "letter_aliases" in legacy:
            run("INSERT OR IGNORE INTO main.case_aliases (key, case_id, answer, at) SELECT key, "
                "case_id, answer, at FROM main.letter_aliases", "aliases")
        if "letter_events" in legacy:
            run("INSERT INTO main.case_events (ts, case_id, part_id, kind, state, actor, evidence) "
                "SELECT ts, case_id, dest_id, kind, state, actor, evidence FROM main.letter_events "
                "ORDER BY seq", "events")
        for t in legacy:
            backup = f"{t}_v1"
            if _objects(conn).get(backup):
                backup = f"{t}_v1_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
            conn.execute(f"ALTER TABLE main.{t} RENAME TO {backup}")
        for name, sql in LETTER_VIEWS.items():
            conn.execute(f"CREATE VIEW IF NOT EXISTS main.{name} AS {sql}")
        _event(conn, "", "migrate", actor="migration", evidence=counts)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"moved": counts, "already": False}


# ── CLI ────────────────────────────────────────────────────────────────

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
    p = argparse.ArgumentParser(description="Work cases: work someone asked for, tracked to delivery.",
                                allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("kinds", allow_abbrev=False)
    s = sub.add_parser("list", allow_abbrev=False)
    s.add_argument("--kind", default="")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("get", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s = sub.add_parser("open", allow_abbrev=False)
    s.add_argument("--kind", required=True)
    s.add_argument("--who", required=True)
    s.add_argument("--email", default="")
    s.add_argument("--title", default="")
    s.add_argument("--part", default="")
    s.add_argument("--due", default="")
    s.add_argument("--method", default="unknown", choices=METHODS)
    s.add_argument("--link", default="")
    s.add_argument("--ledger-id", default="")
    for name in ("accept", "decline"):
        s = sub.add_parser(name, allow_abbrev=False)
        s.add_argument("--case", required=True)
        s.add_argument("--quote", default="")
    s = sub.add_parser("add-part", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--due", default="")
    s.add_argument("--method", default="unknown", choices=METHODS)
    s.add_argument("--link", default="")
    s.add_argument("--ledger-id", default="")
    s = sub.add_parser("set-due", allow_abbrev=False)
    s.add_argument("--part", required=True)
    s.add_argument("--due", required=True, help="YYYY-MM-DD, or \"\" for no date")
    s = sub.add_parser("materials", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--kind", required=True)
    s.add_argument("--ref", required=True)
    s = sub.add_parser("record-draft", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--part", default="")
    s = sub.add_parser("ready", allow_abbrev=False)
    s.add_argument("--part", required=True)
    s = sub.add_parser("record-delivered", allow_abbrev=False)
    s.add_argument("--part", required=True)
    s.add_argument("--evidence", required=True)
    s = sub.add_parser("merge", allow_abbrev=False)
    s.add_argument("--from", dest="from_id", required=True)
    s.add_argument("--into", required=True)
    s = sub.add_parser("questions", allow_abbrev=False)
    s.add_argument("--kind", default="")
    s = sub.add_parser("answer", allow_abbrev=False)
    s.add_argument("--question", required=True)
    s.add_argument("--choice", required=True)
    s = sub.add_parser("migrate", allow_abbrev=False)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--report", default="", help="write the full report (with names) to this file")
    args = p.parse_args(argv)
    actor = _actor()
    if args.cmd == "kinds":
        return _out({"kinds": [
            {"name": k.name, "noun": k.noun, "part": k.part_noun, "delivered": k.delivered,
             "decision": k.decision.question if k.decision else "", "detection": detection_on(k),
             "guide": k.guide, "source": k.source} for k in kinds().values()]})
    if args.cmd == "migrate" and args.apply and not _is_owner():
        return _err("migrate --apply is the owner's step: run it from the owner's shell")
    if args.cmd == "decline" and not args.quote and not _is_owner():
        return _err("decline needs --quote with the owner's own words")
    if args.cmd == "migrate" and not args.apply:
        # A dry run opens the ledger read-only: it writes nothing at all.
        led = _ledger()
        conn = led.guarded_connect(led.DB_PATH, read_only=True)
        conn.row_factory = sqlite3.Row
        try:
            plan = migration_plan(conn)
        finally:
            conn.close()
        if args.report:
            Path(args.report).write_text(plan_report(plan, public=False) + "\n")
        return _out({"dry_run": True, "report": plan_report(plan).splitlines()})
    if args.cmd == "migrate":
        led = _ledger()
        conn = led.guarded_connect(led.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            plan = migration_plan(conn)
            if args.report:
                Path(args.report).write_text(plan_report(plan, public=False) + "\n")
            return _out({"ok": True, **apply_migration(conn)})
        finally:
            conn.close()
    conn = connect()
    try:
        if args.cmd == "list":
            found = cases(conn, kind=args.kind or None, open_only=not args.all)
            return _out({"count": len(found), "cases": [
                {"id": c["id"], "kind": c["kind"], "line": summary_line(c),
                 "decision": c["decision"], "parts": len(c["parts"])} for c in found]})
        if args.cmd == "get":
            c = case(conn, args.case)
            return _out({"case": c, "line": summary_line(c), "moves": moves(c),
                         "version": version(conn, args.case)}) if c else _err(f"no case {args.case}")
        if args.cmd == "open":
            res = open_case(conn, args.kind, args.who, email=args.email, title=args.title,
                            part_name=args.part, due=args.due, method=args.method, link=args.link,
                            ledger_id=args.ledger_id, actor=actor)
        elif args.cmd == "accept":
            res = accept(conn, args.case, actor=actor, quote=args.quote)
        elif args.cmd == "decline":
            res = decline(conn, args.case, actor=actor, quote=args.quote)
        elif args.cmd == "add-part":
            res = {"part": add_part(conn, args.case, args.name, args.due, args.method, args.link,
                                    args.ledger_id, actor=actor)}
        elif args.cmd == "set-due":
            res = set_due(conn, args.part, args.due, actor=actor)
        elif args.cmd == "materials":
            res = {"added": add_material(conn, args.case, args.kind, args.ref, actor=actor)}
        elif args.cmd == "record-draft":
            res = record_draft(conn, args.case, args.file, args.part, actor=actor)
        elif args.cmd == "ready":
            res = set_state(conn, args.part, "ready", actor=actor)
        elif args.cmd == "record-delivered":
            res = record_delivered(conn, args.part, args.evidence, actor=actor)
        elif args.cmd == "merge":
            res = merge(conn, args.from_id, args.into, actor=actor)
        elif args.cmd == "questions":
            return _out({"questions": questions(conn, kind=args.kind or None)})
        elif args.cmd == "answer":
            res = answer(conn, args.question, args.choice, actor=actor)
        else:
            return _err(f"unknown command {args.cmd}")
        conn.commit()
        return _out({"ok": True, **res})
    except (ValueError, MigrationPending) as e:
        return _err(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
