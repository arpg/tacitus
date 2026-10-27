#!/usr/bin/env python3
"""Relational project store: projects, people, and their connective tissue.

The ledger tracks TASKS; this tracks the CONTEXT they belong to. Each
project carries a living summary, the people attached to it (with their
role in THAT project — the same person means different things in
different rooms), links to ledger items, filed email gists, and an
append-only journal of decisions. `pack` renders the whole situation as
one prompt-sized blob so a conversation starts warm instead of grepping
mail and ledger from cold.

Domain taxonomy is the academic's five-way split and is deliberately
orthogonal to the parent/child hierarchy: an AI course sounds like a
research concern but is a TEACHING project; a new degree program
build is SERVICE. Domain answers "which hat", parent_id answers "which
umbrella".

Same database file as the ledger (joins are the point), same access
pattern: atomic CLI verbs, JSON out, the LLM never touches the file.

Usage:
    python3 project_store.py add-project --id field-trial --name "Field trial" --domain research
    python3 project_store.py add-person --email x@example.edu --name "..." [--org --role --notes]
    python3 project_store.py link-person --project field-trial --email x@example.edu --role "POC"
    python3 project_store.py link-ledger --project state-grant --ledger-id state-grant-application
    python3 project_store.py file-message --project survey-paper --conversation-id <id> --subject "..." [--gist "..."]
    python3 project_store.py journal --project state-grant --text "Report submitted" [--kind milestone]
    python3 project_store.py update-project --id survey-paper [--summary ...] [--next-actions ...] [--status dormant]
    python3 project_store.py get --project survey-paper | list [--domain teaching] | tree | people [--project x]
    python3 project_store.py pack --project survey-paper          # the monolithic read
    python3 project_store.py match --text "message text"   # project lookup for dispatch injection
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import ledger as _ledger  # guarded_connect: the shared database's owner-side open
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ledger as _ledger

EA_DIR = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))
DB_PATH = EA_DIR / "state" / "obligations-ledger.db"

DOMAINS = ("research", "teaching", "service", "professional", "personal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    domain      TEXT NOT NULL,
    kind        TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    parent_id   TEXT,
    summary     TEXT,
    next_actions TEXT,
    aliases     TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    email       TEXT PRIMARY KEY,
    name        TEXT,
    org         TEXT,
    role        TEXT,
    notes       TEXT,
    aliases     TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_people (
    project_id      TEXT NOT NULL,
    email           TEXT NOT NULL,
    role_in_project TEXT,
    note            TEXT,
    PRIMARY KEY (project_id, email)
);
CREATE TABLE IF NOT EXISTS project_ledger (
    project_id TEXT NOT NULL,
    ledger_id  TEXT NOT NULL,
    PRIMARY KEY (project_id, ledger_id)
);
CREATE TABLE IF NOT EXISTS project_messages (
    project_id      TEXT NOT NULL,
    message_id      TEXT NOT NULL DEFAULT '',
    conversation_id TEXT DEFAULT '',
    subject         TEXT,
    from_email      TEXT,
    received        TEXT,
    direction       TEXT,
    gist            TEXT,
    filed_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, message_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS project_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    kind       TEXT DEFAULT 'note',
    text       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loose_ledger (
    ledger_id TEXT PRIMARY KEY,
    marked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_vetoes (
    merged_id TEXT NOT NULL,
    entity    TEXT NOT NULL,
    vetoed_at TEXT NOT NULL,
    PRIMARY KEY (merged_id, entity)
);
CREATE TABLE IF NOT EXISTS item_merges (
    merged_id  TEXT PRIMARY KEY,
    primary_id TEXT NOT NULL,
    entity     TEXT NOT NULL,
    merged_at  TEXT NOT NULL,
    auto       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS project_links (
    project_id TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT DEFAULT '',
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, url)
);
CREATE INDEX IF NOT EXISTS idx_pmsg_conv ON project_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_pevents ON project_events(project_id, ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Additive migrations for existing databases (safe to re-run).
MIGRATIONS = [
    "ALTER TABLE projects ADD COLUMN instructions TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN horizon TEXT DEFAULT 'project'",
]

# The PARA distinction the status column cannot express. A project has an
# outcome and an end and can go dormant; an area is a standing
# responsibility and a goal is a long-term direction, and both keep their
# room and never go dormant on their own.
HORIZONS = ("project", "area", "goal")


def _get_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    if not _ledger._is_owner():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = _ledger.guarded_connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def _out(obj) -> int:
    print(json.dumps(obj, indent=2))
    return 0


def _err(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


def _project(conn, pid: str):
    return conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()


def _slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", s))


# ── Mutations ───────────────────────────────────────────────────────────

def cmd_add_project(args) -> int:
    if not _slug_ok(args.id):
        return _err(f"id must be a kebab-case slug, got {args.id!r}")
    if args.domain not in DOMAINS:
        return _err(f"domain must be one of {DOMAINS}")
    horizon = (getattr(args, "horizon", None) or "project").lower()
    if horizon not in HORIZONS:
        return _err(f"horizon must be one of {HORIZONS}")
    conn = _get_db()
    if _project(conn, args.id):
        return _err(f"project {args.id} already exists")
    if args.parent and not _project(conn, args.parent):
        return _err(f"parent {args.parent} does not exist (create it first)")
    now = _now()
    conn.execute(
        "INSERT INTO projects (id,name,domain,kind,status,parent_id,summary,"
        "next_actions,aliases,created_at,updated_at,horizon) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (args.id, args.name, args.domain, args.kind, args.status, args.parent,
         args.summary or "", args.next_actions or "", args.aliases or "",
         now, now, horizon))
    conn.commit()
    return _out({"ok": True, "action": "added", "id": args.id})


def projects_created_since(since_epoch: float, conn=None) -> list[dict]:
    """Projects whose row was created at or after `since_epoch`, newest
    first. The bot asks this after a Slack turn to open a room for a
    project the turn created."""
    conn = conn or _get_db()
    since = datetime.fromtimestamp(since_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id,name,domain,status,created_at,COALESCE(horizon,'project') AS horizon "
        "FROM projects WHERE created_at >= ? ORDER BY created_at DESC", (since,))
    return [dict(r) for r in rows]


def cmd_update_project(args) -> int:
    conn = _get_db()
    row = _project(conn, args.id)
    if not row:
        return _err(f"no project {args.id}")
    fields, vals = [], []
    for col, val in (("name", args.name), ("domain", args.domain),
                     ("kind", args.kind), ("status", args.status),
                     ("horizon", getattr(args, "horizon", None)),
                     ("parent_id", args.parent), ("summary", args.summary),
                     ("next_actions", args.next_actions),
                     ("aliases", args.aliases),
                     ("instructions", getattr(args, "instructions", None))):
        if val is not None:
            if col == "domain" and val not in DOMAINS:
                return _err(f"domain must be one of {DOMAINS}")
            fields.append(f"{col}=?")
            vals.append(val)
    if not fields:
        return _err("nothing to update")
    fields.append("updated_at=?")
    vals += [_now(), args.id]
    conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    return _out({"ok": True, "action": "updated", "id": args.id})


def cmd_add_person(args) -> int:
    conn = _get_db()
    email = args.email.lower()
    now = _now()
    row = conn.execute("SELECT * FROM people WHERE email=?", (email,)).fetchone()
    if row:
        fields, vals = [], []
        for col, val in (("name", args.name), ("org", args.org),
                         ("role", args.role), ("notes", args.notes),
                         ("aliases", args.aliases)):
            if val is not None:
                fields.append(f"{col}=?")
                vals.append(val)
        if fields:
            fields.append("updated_at=?")
            vals += [now, email]
            conn.execute(f"UPDATE people SET {', '.join(fields)} WHERE email=?",
                         vals)
            conn.commit()
        return _out({"ok": True, "action": "updated", "email": email})
    conn.execute(
        "INSERT INTO people (email,name,org,role,notes,aliases,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (email, args.name or "", args.org or "", args.role or "",
         args.notes or "", args.aliases or "", now, now))
    conn.commit()
    return _out({"ok": True, "action": "added", "email": email})


def cmd_link_person(args) -> int:
    conn = _get_db()
    email = args.email.lower()
    if not _project(conn, args.project):
        return _err(f"no project {args.project}")
    if not conn.execute("SELECT 1 FROM people WHERE email=?", (email,)).fetchone():
        return _err(f"no person {email} (add-person first)")
    conn.execute(
        "INSERT INTO project_people (project_id,email,role_in_project,note) "
        "VALUES (?,?,?,?) ON CONFLICT(project_id,email) DO UPDATE SET "
        "role_in_project=excluded.role_in_project, note=excluded.note",
        (args.project, email, args.role or "", args.note or ""))
    conn.commit()
    return _out({"ok": True, "action": "linked", "project": args.project,
                 "email": email})


def cmd_link_ledger(args) -> int:
    conn = _get_db()
    if not _project(conn, args.project):
        return _err(f"no project {args.project}")
    conn.execute(
        "INSERT OR IGNORE INTO project_ledger (project_id,ledger_id) "
        "VALUES (?,?)", (args.project, args.ledger_id))
    conn.commit()
    return _out({"ok": True, "action": "linked", "project": args.project,
                 "ledger_id": args.ledger_id})


def cmd_file_message(args) -> int:
    conn = _get_db()
    if not _project(conn, args.project):
        return _err(f"no project {args.project}")
    conn.execute(
        "INSERT OR REPLACE INTO project_messages (project_id,message_id,"
        "conversation_id,subject,from_email,received,direction,gist,filed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (args.project, args.message_id or "", args.conversation_id or "",
         args.subject or "", (args.from_email or "").lower(),
         args.received or "", args.direction or "in", args.gist or "",
         _now()))
    conn.commit()
    return _out({"ok": True, "action": "filed", "project": args.project})


def cmd_journal(args) -> int:
    conn = _get_db()
    if not _project(conn, args.project):
        return _err(f"no project {args.project}")
    conn.execute(
        "INSERT INTO project_events (project_id,ts,kind,text) VALUES (?,?,?,?)",
        (args.project, _now(), args.kind or "note", args.text))
    conn.execute("UPDATE projects SET updated_at=? WHERE id=?",
                 (_now(), args.project))
    conn.commit()
    return _out({"ok": True, "action": "journaled", "project": args.project})


# ── Reads ───────────────────────────────────────────────────────────────

def cmd_get(args) -> int:
    conn = _get_db()
    row = _project(conn, args.project)
    if not row:
        return _err(f"no project {args.project}")
    d = dict(row)
    d["people"] = [dict(r) for r in conn.execute(
        "SELECT pp.email, pp.role_in_project, pp.note, p.name, p.org "
        "FROM project_people pp LEFT JOIN people p ON p.email=pp.email "
        "WHERE pp.project_id=?", (args.project,))]
    d["ledger_ids"] = [r["ledger_id"] for r in conn.execute(
        "SELECT ledger_id FROM project_ledger WHERE project_id=?",
        (args.project,))]
    d["children"] = [r["id"] for r in conn.execute(
        "SELECT id FROM projects WHERE parent_id=?", (args.project,))]
    return _out(d)


def cmd_list(args) -> int:
    conn = _get_db()
    q = "SELECT id,name,domain,kind,status,parent_id,updated_at,horizon FROM projects WHERE 1=1"
    vals = []
    if args.domain:
        q += " AND domain=?"; vals.append(args.domain)
    if args.status:
        q += " AND status=?"; vals.append(args.status)
    if args.parent:
        q += " AND parent_id=?"; vals.append(args.parent)
    q += " ORDER BY domain, name"
    return _out([dict(r) for r in conn.execute(q, vals)])


def cmd_tree(args) -> int:
    conn = _get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,name,domain,status,parent_id FROM projects ORDER BY domain,name")]
    kids: dict = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    lines = []

    def walk(pid, depth):
        for r in kids.get(pid, []):
            flag = "" if r["status"] == "active" else f" [{r['status']}]"
            lines.append(f"{'  ' * depth}{r['id']}  ({r['domain']}){flag}")
            walk(r["id"], depth + 1)

    walk(None, 0)
    print("\n".join(lines))
    return 0


def cmd_people(args) -> int:
    conn = _get_db()
    if args.project:
        rows = conn.execute(
            "SELECT p.*, pp.role_in_project, pp.note AS project_note "
            "FROM project_people pp JOIN people p ON p.email=pp.email "
            "WHERE pp.project_id=?", (args.project,))
    else:
        rows = conn.execute("SELECT * FROM people ORDER BY name")
    return _out([dict(r) for r in rows])


def _tone_line(email: str) -> str:
    """One line of measured voice for this contact, if the style DB has
    them. Import is lazy and failure is silent: tone is garnish here."""
    try:
        import style_profiles  # noqa: PLC0415
        p = style_profiles.get_profile(email)
        if not p or not p.get("found", True):
            return ""
        return (f"{p.get('greeting', '?')} / sign-off {p.get('sign_off', '?')}"
                f" / ~{p.get('median_words', '?')}w")
    except Exception:
        return ""


def build_pack(pid: str, conn=None, max_messages: int = 8,
               max_events: int = 10) -> dict | None:
    conn = conn or _get_db()
    row = _project(conn, pid)
    if not row:
        return None
    d = dict(row)
    people = [dict(r) for r in conn.execute(
        "SELECT pp.email, pp.role_in_project, pp.note, p.name, p.org, p.notes "
        "AS person_notes FROM project_people pp LEFT JOIN people p "
        "ON p.email=pp.email WHERE pp.project_id=?", (pid,))]
    for p in people:
        tone = _tone_line(p["email"])
        if tone:
            p["tone"] = tone
    try:
        open_items = [dict(r) for r in conn.execute(
            "SELECT l.id, l.type, l.status, l.description, l.deadline, "
            "l.last_action FROM project_ledger pl JOIN ledger l "
            "ON l.id=pl.ledger_id WHERE pl.project_id=? "
            "AND l.status NOT IN ('completed','closed')", (pid,))]
    except sqlite3.OperationalError:
        open_items = []  # ledger table not created yet (fresh install)
    messages = [dict(r) for r in conn.execute(
        "SELECT subject, from_email, received, direction, gist "
        "FROM project_messages WHERE project_id=? "
        "ORDER BY COALESCE(received, filed_at) DESC LIMIT ?",
        (pid, max_messages))]
    events = [dict(r) for r in conn.execute(
        "SELECT ts, kind, text FROM project_events WHERE project_id=? "
        "ORDER BY ts DESC, id DESC LIMIT ?", (pid, max_events))]
    children = [dict(r) for r in conn.execute(
        "SELECT id, name, status FROM projects WHERE parent_id=?", (pid,))]
    links = [dict(r) for r in conn.execute(
        "SELECT url, title FROM project_links WHERE project_id=? "
        "ORDER BY added_at", (pid,))]

    lines = [f"# {d['name']} ({d['domain']}"
             + (f", {d['kind']}" if d.get("kind") else "") + ")"]
    if d.get("parent_id"):
        lines.append(f"Part of: {d['parent_id']}")
    if children:
        lines.append("Children: " + ", ".join(
            f"{c['id']}{'' if c['status'] == 'active' else ' [' + c['status'] + ']'}"
            for c in children))
    if d.get("summary"):
        lines += ["", d["summary"].strip()]
    if d.get("instructions"):
        lines += ["", "Operating notes for this project (follow these):",
                  d["instructions"].strip()]
    if d.get("next_actions"):
        lines += ["", "Next: " + d["next_actions"].strip()]
    if links:
        lines += ["", "Links:"]
        lines += [f"- {li['title'] or li['url']}: {li['url']}" for li in links]
    if people:
        lines += ["", "People:"]
        for p in people:
            bits = [p.get("name") or p["email"]]
            if p.get("role_in_project"):
                bits.append(p["role_in_project"])
            if p.get("org"):
                bits.append(p["org"])
            line = "- " + ", ".join(bits)
            if p.get("person_notes"):
                line += f" ({p['person_notes']})"
            if p.get("tone"):
                line += f" [voice: {p['tone']}]"
            lines.append(line)
    if open_items:
        lines += ["", "Open ledger items:"]
        for o in open_items:
            dl = f" (due {o['deadline']})" if o.get("deadline") else ""
            lines.append(f"- {o['id']}: {o['description'][:100]}{dl}")
    if events:
        lines += ["", "Journal:"]
        for e in reversed(events):
            lines.append(f"- {e['ts'][:10]} [{e['kind']}] {e['text'][:140]}")
    if messages:
        lines += ["", "Recent mail:"]
        for m in messages:
            arrow = "<-" if m["direction"] == "in" else "->"
            lines.append(f"- {str(m['received'])[:10]} {arrow} "
                         f"{m['from_email']}: {m['subject'][:70]}"
                         + (f" — {m['gist'][:90]}" if m["gist"] else ""))
    return {"project": pid, "brief": "\n".join(lines),
            "people": people, "open_items": open_items,
            "events": events, "messages": messages, "links": links}


def cmd_pack(args) -> int:
    pack = build_pack(args.project, max_messages=args.max_messages)
    if pack is None:
        return _err(f"no project {args.project}")
    return _out(pack)


def alias_weight(alias: str) -> int:
    """What one alias hit scores. A name, an acronym, a number or a
    phrase (`Northwind`, `ORION`, `4417`, `foundation repair`) names the
    project: 3, the floor every caller places on. One lowercase word
    (`foundation`, `settlement`) is a common word that turns up in
    unrelated mail (a science foundation, a court settlement): 2, so it
    places an item only with a second signal."""
    return 2 if alias.isalpha() and alias.islower() else 3


def match_projects(text: str, conn=None) -> list:
    """Deterministic project lookup for dispatch injection: name words,
    aliases, and member names/emails, scored. Returns [{id, score}]."""
    conn = conn or _get_db()
    raw = (text or "").lower()
    hay = " " + re.sub(r"\W+", " ", raw) + " "
    scores: dict = {}

    def bump(pid, n):
        scores[pid] = scores.get(pid, 0) + n

    for r in conn.execute("SELECT id,name,aliases,status FROM projects"):
        if r["status"] == "closed":
            continue
        for alias in filter(None, (a.strip() for a in (r["aliases"] or "").split(","))):
            if f" {alias.lower()} " in hay:
                bump(r["id"], alias_weight(alias))
        name_tokens = [t for t in re.split(r"\W+", r["name"].lower())
                       if len(t) >= 4]
        n_hits = sum(1 for t in name_tokens if f" {t} " in hay)
        if name_tokens and n_hits == len(name_tokens):
            bump(r["id"], 3)
        elif n_hits >= 2:
            bump(r["id"], 2)
        if f" {r['id']} " in hay or r["id"] in raw:
            bump(r["id"], 3)
    for r in conn.execute(
            "SELECT pp.project_id, pp.email, p.name FROM project_people pp "
            "LEFT JOIN people p ON p.email=pp.email"):
        if r["email"] and r["email"] in raw:
            bump(r["project_id"], 2)
        last = (r["name"] or "").split()[-1].lower() if r["name"] else ""
        if len(last) >= 4 and f" {last} " in hay:
            bump(r["project_id"], 1)
    return sorted(({"id": k, "score": v} for k, v in scores.items()),
                  key=lambda x: -x["score"])


TRACE = 1   # the least word score that confirms the mail tagger's call


def checked_call(call: str | None, hits: list) -> list | None:
    """The mail tagger's project call on an item (mail_tagger.call_for)
    checked against the item's word match (`hits`, match_projects).
    [call] when the words show any trace of that project (a member, two
    name words, an alias: a score of TRACE or more); [] when they show
    none, or when the tagger named no project; None when the tagger made
    no call (Gmail, a row from chat, the tagger off), and the word match
    decides alone. The model reads the whole message and the words keep
    it honest: a call the words do not echo is most often a guess."""
    if call is None:
        return None
    if call and any(h["id"] == call and h["score"] >= TRACE for h in hits):
        return [call]
    return []


def suggest_projects(text: str, conn=None, limit: int = 3) -> list:
    """Fuzzy candidates for a project the user named loosely: a guessed id,
    a nickname, a misspelling, a fragment of the name. The strict matcher's
    hits rank first; then token overlap (whole tokens and prefixes) and
    difflib similarity against ids, names and aliases. Closed projects are
    skipped. Returns [{id, name, status, score}], best first, at most
    `limit`. An empty list means nothing is close enough to offer."""
    import difflib
    conn = conn or _get_db()
    q = (text or "").strip().lower()
    if not q:
        return []
    q_tokens = {t for t in re.split(r"\W+", q) if len(t) >= 3}
    strict = {h["id"]: h["score"] for h in match_projects(q, conn)}
    out = []
    for r in conn.execute("SELECT id,name,aliases,status FROM projects "
                          "WHERE status != 'closed'"):
        cands = [r["id"], r["name"]] + [a.strip() for a in (r["aliases"] or "").split(",")
                                        if a.strip()]
        best = 0.0
        for c in cands:
            cl = c.lower()
            # whole-string similarity counts only when it is real; below
            # 0.7 difflib pairs almost anything with anything
            ratio = difflib.SequenceMatcher(None, q, cl).ratio()
            ratio = ratio if ratio >= 0.7 else 0.0
            toks = {t for t in re.split(r"\W+", cl) if len(t) >= 3}
            overlap = (len(q_tokens & toks) / len(q_tokens)) if q_tokens else 0.0
            # a token that extends another (fire / fires, foundation /
            # foundations): both at least four letters, and the short one
            # at least half the long one, so "sab" never claims "sabbatical"
            prefixes = sum(1 for a in q_tokens if any(
                a != b and min(len(a), len(b)) >= 4
                and min(len(a), len(b)) * 2 >= max(len(a), len(b))
                and (a.startswith(b) or b.startswith(a)) for b in toks))
            prefix = (prefixes / len(q_tokens)) if q_tokens else 0.0
            best = max(best, ratio, overlap, 0.8 * prefix)
        score = best + 0.5 * strict.get(r["id"], 0)
        if score >= 0.5:
            out.append({"id": r["id"], "name": r["name"], "status": r["status"],
                        "score": round(score, 3)})
    out.sort(key=lambda x: (-x["score"], x["id"]))
    return out[:limit]


def list_active(conn=None) -> list:
    """[{id, name}] for every active project, by name. The room picker's
    menu."""
    conn = conn or _get_db()
    return [{"id": r["id"], "name": r["name"]} for r in conn.execute(
        "SELECT id,name FROM projects WHERE status='active' "
        "ORDER BY name COLLATE NOCASE")]


def cmd_match(args) -> int:
    return _out(match_projects(args.text))


def cmd_suggest(args) -> int:
    return _out(suggest_projects(args.text, limit=args.limit))


def index_brief(conn=None, max_chars: int = 3200, status: str = "active") -> str:
    """Every project on one screen: the cross-project awareness a room
    scoped to ONE project otherwise lacks. One line per project, by
    domain, with its parent, open-item count and next action, so the EA
    in one room can say the work belongs to another project and file it
    there. Cut at `max_chars` on a line boundary."""
    conn = conn or _get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,name,domain,status,parent_id,COALESCE(horizon,'project') AS horizon,"
        "substr(COALESCE(next_actions,''),1,70) AS next_actions "
        "FROM projects WHERE status=? ORDER BY domain,name", (status,))]
    counts: dict = {}
    try:
        for r in conn.execute(
                "SELECT pl.project_id, COUNT(*) AS n FROM project_ledger pl "
                "JOIN ledger l ON l.id=pl.ledger_id "
                "WHERE l.status NOT IN ('completed','closed') GROUP BY pl.project_id"):
            counts[r["project_id"]] = r["n"]
    except sqlite3.OperationalError:
        pass
    lines = [f"PROJECT INDEX ({len(rows)} {status}; id | name | open items | next):"]
    domain = None
    for r in rows:
        if r["domain"] != domain:
            domain = r["domain"]
            lines.append(f"[{domain}]")
        parent = f" (under {r['parent_id']})" if r.get("parent_id") else ""
        hz = f" [{r['horizon']}]" if r.get("horizon") and r["horizon"] != "project" else ""
        nxt = f" | next: {r['next_actions']}" if r.get("next_actions") else ""
        lines.append(f"- {r['id']} | {r['name'][:60]}{hz}{parent} | {counts.get(r['id'], 0)} open{nxt}")
    out: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > max_chars:
            out.append("- ... (cut; `project_store.py index` for the rest)")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def dormancy_candidates(conn=None, days: int = 30) -> list[dict]:
    """Active projects of horizon `project` with no open ledger items and
    no activity within `days`. Activity is a journal entry that is not
    bookkeeping (room opened, dormancy set), a filed message, or a ledger
    item touched; a project with none of those counts from its creation.
    Areas and goals are never candidates."""
    conn = conn or _get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [dict(r) for r in conn.execute(
        "SELECT id,name,domain,created_at,COALESCE(horizon,'project') AS horizon "
        "FROM projects WHERE status='active' ORDER BY domain,name")]
    out: list[dict] = []
    for r in rows:
        if r["horizon"] != "project":
            continue
        try:
            open_n = conn.execute(
                "SELECT COUNT(*) FROM project_ledger pl JOIN ledger l ON l.id=pl.ledger_id "
                "WHERE pl.project_id=? AND l.status NOT IN ('completed','closed')",
                (r["id"],)).fetchone()[0]
            last_item = conn.execute(
                "SELECT MAX(l.updated_at) FROM project_ledger pl JOIN ledger l ON l.id=pl.ledger_id "
                "WHERE pl.project_id=?", (r["id"],)).fetchone()[0] or ""
        except sqlite3.OperationalError:
            open_n, last_item = 0, ""
        if open_n:
            continue
        last_event = conn.execute(
            "SELECT MAX(ts) FROM project_events WHERE project_id=? AND kind!='note' "
            "OR (project_id=? AND kind='note' AND text NOT LIKE 'Slack channel %' "
            "AND text NOT LIKE 'set dormant by%')",
            (r["id"], r["id"])).fetchone()[0] or ""
        last_msg = conn.execute(
            "SELECT MAX(received) FROM project_messages WHERE project_id=?", (r["id"],)).fetchone()[0] or ""
        last = max(x for x in (last_event, last_msg, last_item, r["created_at"] or ""))
        if last >= cutoff:
            continue
        out.append({"id": r["id"], "name": r["name"], "domain": r["domain"],
                    "last_activity": last[:10], "open_items": 0})
    return out


def cmd_dormancy_scan(args) -> int:
    conn = _get_db()
    cands = dormancy_candidates(conn, days=args.days)
    if args.apply:
        now = _now()
        for c in cands:
            conn.execute("UPDATE projects SET status='dormant', updated_at=? WHERE id=?", (now, c["id"]))
            conn.execute("INSERT INTO project_events (project_id,ts,kind,text) VALUES (?,?,?,?)",
                         (c["id"], now, "note",
                          f"set dormant by the dormancy scan: no open items and no activity "
                          f"since {c['last_activity']} ({args.days}-day rule); "
                          f"`update-project --status active` revives it"))
        conn.commit()
    return _out({"days": args.days, "applied": bool(args.apply), "candidates": cands})


def cmd_index(args) -> int:
    print(index_brief(max_chars=args.max_chars, status=args.status))
    return 0


def cmd_add_link(args) -> int:
    conn = _get_db()
    if not _project(conn, args.project):
        return _err(f"no project {args.project}")
    conn.execute("INSERT OR REPLACE INTO project_links (project_id,url,"
                 "title,added_at) VALUES (?,?,?,?)",
                 (args.project, args.url, args.title or "", _now()))
    conn.commit()
    return _out({"ok": True, "action": "linked", "url": args.url})


def cmd_remove_link(args) -> int:
    conn = _get_db()
    conn.execute("DELETE FROM project_links WHERE project_id=? AND url=?",
                 (args.project, args.url))
    conn.commit()
    return _out({"ok": True, "action": "unlinked"})


# ── Entity-level auto-merge ─────────────────────────────────────────────
# Multiple open items about one underlying matter (a ticket, a
# manuscript, an award) arrive as separate emails and become separate
# items. The scan groups OPEN items whose descriptions share an entity
# token and records a view-level aggregation: ledger entries are never
# touched, the merge is a row that surfaces (loudly) in the briefing and
# App Home and can be undone with one command. False positives are
# managed by that visibility, not by silence.

ENTITY_RE = re.compile(
    r"\b((?:[A-Z]{2,8}-\d{2,6}(?:-\d{2,6})+"  # JRNL-2026-0001, XR-26-0001
    r"|[A-Z]{2,8}-?\d{5,10})"                 # CIS123456, DA-123456789
    r"(?:\.R\d+)?)\b")
_ENTITY_BLOCKLIST = {"COVID19", "COVID-19"}


def extract_entities(text: str) -> set:
    return {m.group(1) for m in ENTITY_RE.finditer(text or "")
            if m.group(1) not in _ENTITY_BLOCKLIST}


def auto_merge_scan(conn=None) -> list:
    """Group open ledger items by shared entity token; record NEW merges.

    Primary = the project-filed item if exactly one group member is
    filed, else the oldest. Groups spanning two different projects are
    skipped (ambiguous). Groups larger than 5 are skipped (a token that
    common is probably not an entity). Returns the merges created THIS
    call: [{entity, primary_id, merged_ids}]."""
    conn = conn or _get_db()
    try:
        rows = conn.execute(
            "SELECT id, description, created_at FROM ledger "
            "WHERE status NOT IN ('completed','closed') "
            "AND id NOT IN (SELECT merged_id FROM item_merges)").fetchall()
    except sqlite3.OperationalError:
        return []
    filed = {r["ledger_id"]: r["project_id"] for r in conn.execute(
        "SELECT ledger_id, project_id FROM project_ledger")}
    groups: dict = {}
    for r in rows:
        for ent in extract_entities(r["description"] or ""):
            groups.setdefault(ent, []).append(r)
    created = []
    for ent, members in groups.items():
        if not 2 <= len(members) <= 5:
            continue
        projects = {filed[m["id"]] for m in members if m["id"] in filed}
        if len(projects) > 1:
            continue
        vetoed = {r["merged_id"] for r in conn.execute(
            "SELECT merged_id FROM merge_vetoes WHERE entity=?", (ent,))}
        if any(m["id"] in vetoed for m in members):
            continue  # a human said no to this grouping; never redo it
        # A purpose-built item (plugin or hand-created id) outranks an
        # auto-captured email item, so the merge never hides the deliberate
        # row under a generic capture. Order: the single filed member, else
        # a purpose-built member, else the oldest.
        filed_members = [m for m in members if m["id"] in filed]
        auto_re = re.compile(r"^(response|priority|thread|sched)-")
        purpose = [m for m in members if not auto_re.match(m["id"])]
        if len(filed_members) == 1:
            primary = filed_members[0]
        elif len(purpose) >= 1:
            primary = min(purpose, key=lambda m: m["created_at"] or "z")
        else:
            primary = min(members, key=lambda m: m["created_at"] or "z")
        merged_ids = []
        for m in members:
            if m["id"] == primary["id"]:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO item_merges (merged_id, primary_id, "
                "entity, merged_at, auto) VALUES (?,?,?,?,1)",
                (m["id"], primary["id"], ent, _now()))
            if conn.execute("SELECT changes()").fetchone()[0]:
                merged_ids.append(m["id"])
        if merged_ids:
            created.append({"entity": ent, "primary_id": primary["id"],
                            "primary_desc": (primary["description"] or "")[:90],
                            "merged_ids": merged_ids})
    conn.commit()
    return created


def merge_info_for(conn=None) -> dict:
    """primary_id -> {entity, count} for rendering aggregation badges."""
    conn = conn or _get_db()
    out: dict = {}
    for r in conn.execute(
            "SELECT primary_id, entity, COUNT(*) AS n FROM item_merges "
            "GROUP BY primary_id, entity"):
        out[r["primary_id"]] = {"entity": r["entity"], "count": r["n"]}
    return out


def cmd_auto_merge(args) -> int:
    return _out(auto_merge_scan())


def cmd_merges(args) -> int:
    conn = _get_db()
    return _out([dict(r) for r in conn.execute(
        "SELECT * FROM item_merges ORDER BY merged_at DESC")])


def cmd_unmerge(args) -> int:
    conn = _get_db()
    # Persist the human veto: the scan must never redo a rejected merge.
    for r in conn.execute("SELECT entity FROM item_merges WHERE merged_id=?",
                          (args.ledger_id,)):
        conn.execute("INSERT OR IGNORE INTO merge_vetoes (merged_id, entity, "
                     "vetoed_at) VALUES (?,?,?)",
                     (args.ledger_id, r["entity"], _now()))
    conn.execute("DELETE FROM item_merges WHERE merged_id=?",
                 (args.ledger_id,))
    conn.commit()
    return _out({"ok": True, "action": "unmerged", "ledger_id": args.ledger_id})


def cmd_loose(args) -> int:
    """Mark / unmark ledger items as deliberately project-less. The
    dashboard's unfiled tray shows only items that are NEITHER filed NOR
    loose, so triage converges to empty."""
    conn = _get_db()
    if args.mark:
        conn.execute("INSERT OR REPLACE INTO loose_ledger (ledger_id, "
                     "marked_at) VALUES (?,?)", (args.mark, _now()))
        conn.commit()
        return _out({"ok": True, "action": "loose", "ledger_id": args.mark})
    if args.unmark:
        conn.execute("DELETE FROM loose_ledger WHERE ledger_id=?",
                     (args.unmark,))
        conn.commit()
        return _out({"ok": True, "action": "unloose", "ledger_id": args.unmark})
    return _out([r["ledger_id"] for r in
                 conn.execute("SELECT ledger_id FROM loose_ledger")])


# ── Entry point ─────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="EA project store", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-project", allow_abbrev=False)
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--kind")
    p.add_argument("--status", default="active")
    p.add_argument("--horizon", default="project", choices=HORIZONS,
                   help="project (has an end), area (standing responsibility), goal (long-term direction)")
    p.add_argument("--parent")
    p.add_argument("--summary")
    p.add_argument("--next-actions", dest="next_actions")
    p.add_argument("--aliases", help="comma-separated match aliases; one lowercase "
                   "word is a common word and needs a second signal to place mail")
    p.set_defaults(func=cmd_add_project)

    p = sub.add_parser("update-project", allow_abbrev=False)
    p.add_argument("--id", required=True)
    for f in ("--name", "--domain", "--kind", "--status", "--parent",
              "--summary", "--aliases", "--instructions"):
        p.add_argument(f)
    p.add_argument("--horizon", choices=HORIZONS)
    p.add_argument("--next-actions", dest="next_actions")
    p.set_defaults(func=cmd_update_project)

    p = sub.add_parser("dormancy-scan",
                       help="projects (horizon project) with no open items and no activity for --days",
                       allow_abbrev=False)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--apply", action="store_true", help="set them dormant and journal it")
    p.set_defaults(func=cmd_dormancy_scan)

    p = sub.add_parser("add-link", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--title")
    p.set_defaults(func=cmd_add_link)

    p = sub.add_parser("remove-link", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--url", required=True)
    p.set_defaults(func=cmd_remove_link)

    p = sub.add_parser("add-person", allow_abbrev=False)
    p.add_argument("--email", required=True)
    for f in ("--name", "--org", "--role", "--notes", "--aliases"):
        p.add_argument(f)
    p.set_defaults(func=cmd_add_person)

    p = sub.add_parser("link-person", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role")
    p.add_argument("--note")
    p.set_defaults(func=cmd_link_person)

    p = sub.add_parser("link-ledger", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--ledger-id", dest="ledger_id", required=True)
    p.set_defaults(func=cmd_link_ledger)

    p = sub.add_parser("file-message", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--message-id", dest="message_id")
    p.add_argument("--conversation-id", dest="conversation_id")
    p.add_argument("--subject")
    p.add_argument("--from", dest="from_email")
    p.add_argument("--received")
    p.add_argument("--direction", choices=("in", "out"), default="in")
    p.add_argument("--gist")
    p.set_defaults(func=cmd_file_message)

    p = sub.add_parser("journal", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--kind", choices=("note", "decision", "milestone",
                                      "status"), default="note")
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("get", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("list", allow_abbrev=False)
    p.add_argument("--domain")
    p.add_argument("--status")
    p.add_argument("--parent")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("tree", allow_abbrev=False)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("people", allow_abbrev=False)
    p.add_argument("--project")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("pack", allow_abbrev=False)
    p.add_argument("--project", required=True)
    p.add_argument("--max-messages", dest="max_messages", type=int, default=8)
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("match", allow_abbrev=False)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("suggest", help="fuzzy candidates for a loosely named project", allow_abbrev=False)
    p.add_argument("--text", required=True)
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("index", help="every project on one screen (cross-project awareness)", allow_abbrev=False)
    p.add_argument("--max-chars", type=int, default=3200)
    p.add_argument("--status", default="active")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("loose", allow_abbrev=False)
    p.add_argument("--mark")
    p.add_argument("--unmark")
    p.set_defaults(func=cmd_loose)

    p = sub.add_parser("auto-merge", allow_abbrev=False)
    p.set_defaults(func=cmd_auto_merge)

    p = sub.add_parser("merges", allow_abbrev=False)
    p.set_defaults(func=cmd_merges)

    p = sub.add_parser("unmerge", allow_abbrev=False)
    p.add_argument("--ledger-id", dest="ledger_id", required=True)
    p.set_defaults(func=cmd_unmerge)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
