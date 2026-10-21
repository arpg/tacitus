#!/usr/bin/env python3
"""Obligations ledger backed by SQLite.

CLI tool for the EA agent to manage tracked items (emails needing response,
scheduling requests, reimbursements, follow-ups, etc.). Each operation is
atomic — the LLM never edits the database directly.

Usage:
    python3 ledger.py add --id <id> --type <type> --description <desc> [--from <email>] ...
    python3 ledger.py update --id <id> [--status <s>] [--last-action <a>] [--notes <n>] ...
    python3 ledger.py close --id <id> [--note <reason>] [--user-said "<words>" | --duplicate-of <id>]
    python3 ledger.py propose --id <id> --why "<why it looks done>"
    python3 ledger.py proposals | reject --id <id> | closure-report [--days 30]
    python3 ledger.py get --id <id>
    python3 ledger.py list [--status open] [--type response_needed] [--stale <days>] [--limit 20]
    python3 ledger.py search --query <text>
    python3 ledger.py stats
    python3 ledger.py dump [--status open]
    python3 ledger.py import-json <path>
    python3 ledger.py types
    python3 ledger.py normalize [--apply]

Closing follows closure.py: the user's word, a standing rule, a duplicate
or decisive evidence closes; any other close is recorded as a proposal the
user confirms in chat, and the row stays open.

A main-EA row's --type is one of TYPES (`ledger.py types` lists them with
their meaning); other spellings fold into them and anything else is
refused. --status is open, follow_up, snoozed or completed.

All output is JSON for easy parsing by the LLM.
"""

import argparse
import json
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as _url_quote

try:
    import owner_trust as _owner_trust
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import owner_trust as _owner_trust

EA_DIR = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))
DB_PATH = EA_DIR / "state" / "obligations-ledger.db"

# ── Guarded open of a shared database ───────────────────────────────────
# The ledger database (the project store shares it) and the style-profile
# database sit in the resident's state directory, and both users write them
# by design: the resident through the CLIs, the owner through the bot, the
# crons and the preprocessor. The resident owns every directory on the way,
# so it can put a link at the database's name, or at the names SQLite opens
# beside it (-wal, -shm, -journal), where an owner open would create,
# rewrite or truncate whatever the link names. guarded_connect() is how an
# owner process opens such a database:
#
#   1. The directory is opened by walking its resolved path from / with
#      O_NOFOLLOW on every component, and for a database under the install
#      tree it must be the tree's own directory (no link below the owner's
#      ~/.ea). Every check below is made relative to that descriptor.
#   2. The database must already exist as a regular, single-linked file
#      owned by the resident or the owner and not writable by others. An
#      owner process never creates it: a missing database is created empty
#      with the resident's access (resident_io.ensure_file), then opened
#      with mode=rw, which never creates a file.
#   3. Each sidecar is absent, or passes the same test.
#   4. The connection gets trusted_schema=OFF and SQLite's defensive mode,
#      so schema the resident wrote (views, triggers) cannot call into
#      application functions. No caller registers functions or extensions
#      on these connections.
#   5. After one read (SQLite now holds the WAL files), the database and
#      sidecars are checked again, and the descriptor this process holds on
#      the database must be the file checked in step 2.
#
# Any failed check raises UnsafeDatabase and nothing is written.
#
# Residual race: between step 3 and SQLite's own open of a sidecar there is
# a window Python's sqlite3 module cannot close (it offers no hook on the
# files SQLite opens). A link planted in that window is caught by step 5,
# after SQLite has opened it. The race-free design is an owner process that
# reaches the database only with the resident's access (the verb run as the
# resident, the way the bot runs doc_review.py).

SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
RESIDENT_USER = "ea-bot"
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class UnsafeDatabase(sqlite3.DatabaseError):
    """An owner process refused to open a shared database."""


def _default_is_owner() -> bool:
    return _owner_trust.is_owner_process()


def _default_install_tree() -> Path:
    return _owner_trust.install_ea_dir()


# Seams for tests.
_is_owner = _default_is_owner
_install_tree = _default_install_tree


def _allowed_uids() -> set[int]:
    uids = {os.geteuid()}
    try:
        uids.add(pwd.getpwnam(RESIDENT_USER).pw_uid)
    except KeyError:
        pass
    return uids


def _open_dir_nofollow(real_dir: str) -> int:
    """A descriptor for `real_dir` (an absolute, resolved path), walked from
    / without following a link at any component."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    try:
        for comp in real_dir.strip("/").split("/"):
            if not comp:
                continue
            try:
                nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC,
                              dir_fd=fd)
            except OSError as e:
                raise UnsafeDatabase(f"{real_dir}: component {comp!r} is not a plain "
                                     f"directory ({e.strerror})") from None
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _expected_parent(path: Path) -> str | None:
    """For a path under the install tree (named through the owner's ~/.ea
    or its resolved target), the resolved directory it must be in; None for
    a path outside the tree."""
    try:
        tree = _install_tree()
        tree_real = os.path.realpath(tree)
    except Exception:
        return None
    parent = os.path.abspath(os.fspath(path.parent))
    for root in {os.path.abspath(os.fspath(tree)), tree_real}:
        if parent == root or parent.startswith(root.rstrip(os.sep) + os.sep):
            rel = os.path.relpath(parent, root)
            return os.path.normpath(os.path.join(tree_real, rel))
    return None


def _check_db_entry(dfd: int, name: str, shown: str, required: bool):
    """The lstat of `name` in `dfd`, after the plain-file checks; None when
    it is absent and not required."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise UnsafeDatabase(f"{shown} does not exist") from None
        return None
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeDatabase(f"{shown} is not a regular file (a link or special file); "
                             f"an owner process does not open it")
    if st.st_nlink != 1:
        raise UnsafeDatabase(f"{shown} has {st.st_nlink} hard links")
    if st.st_uid not in _allowed_uids():
        raise UnsafeDatabase(f"{shown} is owned by uid {st.st_uid}, neither the owner "
                             f"nor {RESIDENT_USER}")
    if st.st_mode & stat.S_IWOTH:
        raise UnsafeDatabase(f"{shown} is writable by others")
    return st


def _held_inodes(target: str) -> set[tuple[int, int]] | None:
    """(st_dev, st_ino) of every descriptor this process holds on `target`;
    None when /proc/self/fd cannot be read."""
    out = set()
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return None
    for n in names:
        try:
            if os.readlink(f"/proc/self/fd/{n}") == target:
                st = os.fstat(int(n))
                out.add((st.st_dev, st.st_ino))
        except (OSError, ValueError):
            continue
    return out


def _check_all(dfd: int, name: str, real_path: str):
    db = _check_db_entry(dfd, name, real_path, required=True)
    for suffix in SQLITE_SIDECARS:
        _check_db_entry(dfd, name + suffix, real_path + suffix, required=False)
    return db


def guarded_connect(path, *, timeout: float = 10.0,
                    read_only: bool = False) -> sqlite3.Connection:
    """Open the SQLite database at `path`. In an owner process the checks
    above run first and the connection never creates a file; any other
    process (the resident, a test on a scratch tree) opens it directly.
    Raises UnsafeDatabase when a check fails."""
    path = Path(path)
    try:
        owner = bool(_is_owner())
    except Exception:
        owner = True
    if not owner:
        if read_only:
            return sqlite3.connect(f"file:{_url_quote(str(path))}?mode=ro", uri=True,
                                   timeout=timeout)
        return sqlite3.connect(str(path), timeout=timeout)

    real_parent = os.path.realpath(path.parent)
    want = _expected_parent(path)
    if want is not None and real_parent != want:
        raise UnsafeDatabase(f"{path.parent} resolves to {real_parent}, not {want}: "
                             f"a link below the install tree")
    name = path.name
    real_path = os.path.join(real_parent, name)
    dfd = _open_dir_nofollow(real_parent)
    try:
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            if read_only:
                raise UnsafeDatabase(f"{real_path} does not exist") from None
            try:
                import resident_io as _rio
            except ModuleNotFoundError:
                from preprocessor import resident_io as _rio
            if not _rio.ensure_file(real_path):
                raise UnsafeDatabase(f"{real_path} does not exist and could not be "
                                     f"created with the resident's access") from None
        before = _check_all(dfd, name, real_path)
        mode = "ro" if read_only else "rw"
        conn = sqlite3.connect(f"file:{_url_quote(real_path)}?mode={mode}", uri=True,
                               timeout=timeout)
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            conn.execute("PRAGMA schema_version").fetchone()
            after = _check_all(dfd, name, real_path)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise UnsafeDatabase(f"{real_path} was replaced while it was opened")
            held = _held_inodes(real_path)
            if held is not None and (before.st_dev, before.st_ino) not in held:
                raise UnsafeDatabase(f"the open database is not the file checked at "
                                     f"{real_path}")
        except BaseException:
            conn.close()
            raise
        return conn
    finally:
        os.close(dfd)

# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    description     TEXT,
    from_email      TEXT,
    from_name       TEXT,
    source_message_id TEXT,
    conversation_id TEXT,
    date_identified TEXT,
    deadline        TEXT,
    follow_up_after TEXT,
    snooze_until    TEXT,
    last_action     TEXT,
    notes           TEXT,
    priority        TEXT,
    email_archived  INTEGER NOT NULL DEFAULT 0,
    source          TEXT,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger(type);
CREATE INDEX IF NOT EXISTS idx_ledger_from ON ledger(from_email);
CREATE INDEX IF NOT EXISTS idx_ledger_deadline ON ledger(deadline);
-- Note: idx_ledger_source is created in MIGRATIONS so it runs after the
-- ALTER TABLE ADD COLUMN for `source` on pre-existing databases.
"""

# Migrations for existing databases (ALTER TABLE is idempotent-safe via try/except)
MIGRATIONS = [
    "ALTER TABLE ledger ADD COLUMN conversation_id TEXT",
    "ALTER TABLE ledger ADD COLUMN email_archived INTEGER NOT NULL DEFAULT 0",
    # `source` tags plugin-originated rows so EA core can filter them out.
    # NULL = main EA. 'plugin:<name>' = written by the ea_plugins.ledger
    # helper from a plugin's collect.py or skill run. See
    # docs/plugin-architecture.md.
    "ALTER TABLE ledger ADD COLUMN source TEXT",
    "CREATE INDEX IF NOT EXISTS idx_ledger_source ON ledger(source)",
    # Gmail thread ID — symmetric counterpart to Outlook's conversation_id.
    # Populated when an item originates from a Gmail mailbox (a primary or
    # a plugin-monitored co-owner account). Used by
    # closure_scrubber.py to detect when the user has replied via Gmail
    # to a thread the ledger is still tracking.
    "ALTER TABLE ledger ADD COLUMN gmail_thread_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_ledger_gmail_thread ON ledger(gmail_thread_id)",
]

# The id prefixes of the rows preprocess.py's inbox detectors write: one
# row per mail that asks for something, so several can describe one
# obligation. Every other row is work the EA tracks, linked to its thread
# as evidence, and is its own obligation.
DETECTOR_PREFIXES = ("response-", "priority-", "sched-", "thread-")


def is_detector_row(entry_id: str) -> bool:
    return str(entry_id).startswith(DETECTOR_PREFIXES)


# ── Vocabulary ──────────────────────────────────────────────────────────
# One word for each kind of row a reader acts on. Writers fold the other
# spellings in TYPE_ALIASES into these, and a main-EA row of any other type
# is refused, so a rule written against a type sees every row of that kind.
# A plugin row ('plugin:<name>' in source) keeps its own type: the plugin
# owns its rows and queries them by that type.
TYPES = {
    "response_needed": "a reply to someone is owed",
    "follow_up": "the user owes someone a nudge or a follow-up message",
    "scheduling": "a meeting to arrange or attend",
    "task": "work the user owes: a document, form, purchase, introduction, decision",
    "deadline": "a dated submission, registration or report",
    "waiting": "someone else owes the user a deliverable",
    "reimbursement": "receipts and reimbursement paperwork",
    "dispute": "a billing or refund dispute",
    "draft_pending": "a staged draft waiting for the user to send it",
}
TYPE_ALIASES = {
    "needs_response": "response_needed", "priority_alert": "response_needed",
    "dropped_thread": "response_needed",
    "followup": "follow_up",
    "scheduling_request": "scheduling", "scheduling_needed": "scheduling",
    "meeting_request": "scheduling", "meeting": "scheduling",
    "awaiting": "waiting", "waiting_on": "waiting",
    "travel_reimbursement": "reimbursement", "watch": "reimbursement",
    **{t: "task" for t in (
        "action_item", "action_needed", "action_required", "deliverable",
        "project", "project_task", "admin", "purchase", "proposal", "decision",
        "grant_action", "grant_opportunity", "opportunity", "collaboration_lead",
        "networking", "relationship_building", "outreach", "intro",
        "obligation", "phone_call", "property", "email_draft", "email_to_send",
        "outbound_email", "note", "informational", "reminder", "test")},
}

# A row is open, parked until a date (follow_up: follow_up_after; snoozed:
# snooze_until), or done. Readers still accept 'closed' for done.
OPEN_STATUSES = ("open", "follow_up", "snoozed")
DONE_STATUS = "completed"
STATUSES = OPEN_STATUSES + (DONE_STATUS,)
STATUS_ALIASES = {
    "closed": DONE_STATUS, "done": DONE_STATUS, "complete": DONE_STATUS,
    "in_progress": "open", "pending_confirm": "open", "response_needed": "open",
    "active": "open", "followup": "follow_up",
}
# A status that names a kind of row: the row is open, and its type is this.
STATUS_IS_TYPE = {"waiting": "waiting", "awaiting": "waiting", "waiting_on": "waiting"}


def _word(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _status_filter(text: str) -> list[str]:
    """A --status filter in canonical words; done still matches 'closed'."""
    out: list[str] = []
    for part in text.split(","):
        if not part.strip():
            continue
        w = _word(part)
        s = "open" if w in STATUS_IS_TYPE else STATUS_ALIASES.get(w, w)
        out += [s] + (["closed"] if s == DONE_STATUS else [])
    return sorted(set(out))


def canonical_type(value: str | None) -> str:
    """The type a spelling means: a TYPES key, or a plugin's own type."""
    w = _word(value)
    return TYPE_ALIASES.get(w, w)


def is_waiting(entry: dict) -> bool:
    """Someone else owes this row's deliverable. The user's own send in the
    thread (a nudge, a thank-you, an answer to a side question) never
    finishes it; only their delivery does."""
    return canonical_type(entry.get("type")) == "waiting"


# Types whose deliverable is the user's own message in the row's thread.
REPLY_TYPES = ("response_needed", "follow_up", "draft_pending")

# What finishes a row, declared when it is written (`add --closes-on`):
#   reply       the user's reply to its correspondent (a task that is an
#               email: "send Zion the doc", "confirm the date")
#   submission  an acknowledgement of the submission in its own thread
#   event       the meeting it asks for is on the calendar or answered
#   manual      only the user's word, or a draft or event that names it
# A row without one falls back on its kind: a detector row and a reply
# type close on a reply; everything else needs the user or a link.
CLOSES_ON = ("reply", "submission", "event", "manual")


def closes_on(entry: dict) -> str:
    how = str(entry.get("closes_on") or "").strip().lower()
    return how if how in CLOSES_ON else ""


def closes_on_reply(entry: dict) -> bool:
    """A send in this row's thread finishes it: a row declared to close on
    a reply, a detector row (the "needs a reply" row) or a row whose type
    is a reply. Any other row is finished by a send only when the draft
    names it (`--ledger-id`); a reply in the same thread about something
    else leaves it open."""
    if is_waiting(entry):
        return False
    how = closes_on(entry)
    if how:
        return how == "reply"
    return (is_detector_row(entry.get("id") or "")
            or canonical_type(entry.get("type")) in REPLY_TYPES)


def checked_type(value: str | None, source: str | None) -> str:
    """The canonical type for a write; ValueError for a main-EA row whose
    type is not one of TYPES."""
    t = canonical_type(value)
    if source and str(source).startswith("plugin:") and t:
        return t
    if t not in TYPES:
        raise ValueError(f"unknown type {value!r}; use one of: "
                         + ", ".join(f"{k} ({v})" for k, v in TYPES.items()))
    return t


def checked_status(value: str | None) -> tuple[str, str]:
    """(status, type it implies or ''); ValueError for an unknown status."""
    w = _word(value)
    if w in STATUS_IS_TYPE:
        return "open", STATUS_IS_TYPE[w]
    s = STATUS_ALIASES.get(w, w)
    if s not in STATUSES:
        raise ValueError(f"unknown status {value!r}; use one of: {', '.join(STATUSES)}")
    return s, ""


def iso_utc(value: str | None) -> str | None:
    """A timestamp as YYYY-MM-DDTHH:MM:SSZ in UTC; a bare date as midnight
    UTC. Anything unparseable is returned unchanged."""
    if not value:
        return value
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def field_name(key: str) -> str:
    """A --set key as the column or metadata key it names: hyphens are
    underscores, and the old JSON names map to their columns."""
    k = key.strip().replace("-", "_")
    return FIELD_ALIASES.get(k, k)


# Core columns that have their own DB columns (everything else goes in metadata)
CORE_COLUMNS = {
    "id", "type", "status", "description", "from_email", "from_name",
    "source_message_id", "conversation_id", "gmail_thread_id",
    "date_identified", "deadline",
    "follow_up_after", "snooze_until", "last_action", "notes", "priority",
    "email_archived", "source", "metadata", "created_at", "updated_at", "closed_at",
}

# Map from old JSON field names to DB column names
FIELD_ALIASES = {
    "from": "from_email",
    "due_by": "deadline",
    "due_date": "deadline",
    "completed_date": "closed_at",
}


def _source_filter_sql(source: str | None = None,
                       include_plugins: bool = False) -> tuple[str, list]:
    """Build a source-column filter clause for ledger queries.

    Returns (sql_fragment, params) where sql_fragment is either empty,
    " AND source IS NULL" (default: main-EA-only rows), " AND source = ?"
    (a specific plugin), or empty (include_plugins=True: no filter).

    The fragment begins with " AND " so callers can append it directly
    to an existing WHERE clause. Callers wanting it as a standalone
    WHERE should strip the leading " AND".

    The CLI accepts --source NULL as a synonym for the default
    (main-EA-only) so scripts can be explicit; it is normalized here.
    """
    if source is not None and source.upper() in ("NULL", "NONE"):
        source = None
    if source is not None:
        return (" AND source = ?", [source])
    if not include_plugins:
        return (" AND source IS NULL", [])
    return ("", [])


def _get_db() -> sqlite3.Connection:
    """Open (and initialize if needed) the ledger database."""
    if not _is_owner():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = guarded_connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # Column already exists
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a clean dict, merging metadata."""
    d = dict(row)
    meta = {}
    if d.get("metadata"):
        try:
            meta = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    del d["metadata"]

    d = {k: v for k, v in d.items() if v is not None}

    if "date_identified" in d and d.get("status") not in ("completed", "closed"):
        try:
            identified = datetime.fromisoformat(d["date_identified"].replace("Z", "+00:00"))
            d["days_stale"] = (datetime.now(timezone.utc) - identified).days
        except (ValueError, TypeError):
            pass

    d.update(meta)
    return d


def _output(data, pretty=False):
    """Print JSON to stdout."""
    indent = 2 if pretty else None
    print(json.dumps(data, indent=indent, default=str))


def _graph_cli(*args) -> dict:
    """Run the mediation-aware graph_actions CLI and parse its JSON tail.

    The CLI, never a graph_client import: under mail mediation the ea-bot
    caller cannot import graph_client (the token is owner-only, so the import
    itself PermissionErrors), and the CLI forwards SAFE verbs to the owner-side
    mediator. A library import here makes every resident-side close skip
    its archive in silence."""
    cmd = [sys.executable, str(Path(__file__).parent / "graph_actions.py"),
           *[str(a) for a in args]]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        out = p.stdout or ""
        i = out.find("{")
        if i < 0:
            return {"error": f"no JSON from graph_actions (rc={p.returncode}): "
                             f"{(p.stderr or out)[:160]}"}
        r = json.loads(out[i:])
        if isinstance(r, dict) and r.get("status") == "error":
            return {"error": r.get("message", "unknown graph_actions error")}
        return r
    except Exception as e:
        return {"error": str(e)}


def _archive_email(message_id: str) -> dict:
    """Archive an email (mediated CLI). {"archived": True} or {"error": ...}."""
    r = _graph_cli("archive-message", "--message-id", message_id)
    if r.get("error"):
        return {"archived": False, "error": r["error"]}
    return {"archived": True, "new_id": r.get("new_id", r.get("message_id",
                                                              message_id))}


def _archive_conversation(conversation_id: str) -> dict:
    """Archive ALL inbox messages of a conversation (mediated CLI). Kills
    re-derivation: without this, sibling messages left in the inbox re-create
    the obligation the morning after the user closes it."""
    r = _graph_cli("archive-conversation", "--conversation-id", conversation_id)
    if r.get("error"):
        return {"archived": False, "error": r["error"]}
    return {"archived": True, **{k: v for k, v in r.items() if k != "status"}}


# ── Commands ────────────────────────────────────────────────────────────

def _checked_closes_on(value: str) -> str:
    how = str(value or "").strip().lower()
    if how not in CLOSES_ON:
        raise ValueError(f"unknown --closes-on {value!r}; use one of: {', '.join(CLOSES_ON)}")
    return how


def cmd_add(args) -> int:
    """Add a new ledger entry."""
    if not args.id or not args.type:
        _output({"error": "--id and --type are required"})
        return 1

    now = _now()
    db = _get_db()

    existing = db.execute("SELECT id FROM ledger WHERE id = ?", (args.id,)).fetchone()
    if existing:
        _output({"error": f"Entry already exists: {args.id}", "hint": "Use 'update' to modify"})
        db.close()
        return 1

    metadata = {}
    core_extra = {}
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = field_name(k)
            if k in CORE_COLUMNS:
                core_extra[k] = v
            else:
                metadata[k] = v

    try:
        status, implied_type = checked_status(args.status or core_extra.get("status") or "open")
        entry_type = checked_type(implied_type or args.type, core_extra.get("source"))
        how = getattr(args, "closes_on", None) or metadata.get("closes_on")
        if how:
            metadata["closes_on"] = _checked_closes_on(how)
    except ValueError as e:
        _output({"error": str(e)})
        db.close()
        return 1

    from_email = args.from_email or core_extra.get("from_email", "")
    from_name = args.from_name or core_extra.get("from_name", "")

    if len(args.description or "") > TITLE_CHARS:
        print(f"NOTE: ledger row {args.id!r} has a {len(args.description)}-character description. "
              f"A description is a title (under {TITLE_CHARS} characters): briefings show it cut. "
              f"Put the detail in --notes.", file=sys.stderr)
    db.execute("""
        INSERT INTO ledger (id, type, status, description, from_email, from_name,
                           source_message_id, conversation_id, gmail_thread_id,
                           date_identified,
                           deadline, follow_up_after, last_action, notes, priority,
                           source, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        args.id,
        entry_type,
        status,
        args.description or "",
        from_email,
        from_name,
        args.source_message_id or core_extra.get("source_message_id", ""),
        args.conversation_id or core_extra.get("conversation_id", ""),
        getattr(args, "gmail_thread_id", None) or core_extra.get("gmail_thread_id"),
        iso_utc(args.date_identified or core_extra.get("date_identified", now)),
        args.deadline or core_extra.get("deadline"),
        args.follow_up_after or core_extra.get("follow_up_after"),
        args.last_action or core_extra.get("last_action", ""),
        args.notes or core_extra.get("notes", ""),
        args.priority or core_extra.get("priority"),
        core_extra.get("source"),  # NULL = main EA; "plugin:<name>" via --set
        json.dumps(metadata),
        now,
        now,
    ))
    db.commit()

    row = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    db.close()
    _output({"ok": True, "action": "added", "entry": _row_to_dict(row)})
    return 0


def cmd_update(args) -> int:
    """Update fields on an existing entry."""
    if not args.id:
        _output({"error": "--id is required"})
        return 1

    db = _get_db()
    row = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    if not row:
        _output({"error": f"Entry not found: {args.id}"})
        db.close()
        return 1

    now = _now()
    sets = ["updated_at = ?"]
    params = [now]

    core_set: dict[str, str] = {}
    meta_updates = {}
    for pair in args.set or []:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = field_name(k)
        if k in CORE_COLUMNS and k not in ("id", "created_at", "updated_at", "metadata"):
            core_set[k] = v
        else:
            meta_updates[k] = v

    raw_status = args.status or core_set.pop("status", None)
    raw_type = core_set.pop("type", None)
    try:
        how = getattr(args, "closes_on", None) or meta_updates.get("closes_on")
        if how:
            meta_updates["closes_on"] = _checked_closes_on(how)
        status, implied_type = checked_status(raw_status) if raw_status else ("", "")
        new_type = implied_type or raw_type
        if new_type:
            new_type = checked_type(new_type, row["source"])
    except ValueError as e:
        _output({"error": str(e)})
        db.close()
        return 1
    if "date_identified" in core_set:
        core_set["date_identified"] = iso_utc(core_set["date_identified"])

    # A close by update goes through the same policy as `close`: the other
    # fields are written here, and the status change is closure.settle's.
    settle_close = bool(status == DONE_STATUS and row["status"] in OPEN_STATUSES)
    if status and not settle_close:
        sets.append("status = ?")
        params.append(status)
        if status == DONE_STATUS:
            sets.append("closed_at = ?")
            params.append(now)
    if new_type:
        sets.append("type = ?")
        params.append(new_type)
    if args.last_action:
        sets.append("last_action = ?")
        params.append(args.last_action)
    if args.notes:
        sets.append("notes = ?")
        params.append(args.notes)
    if args.description:
        sets.append("description = ?")
        params.append(args.description)
    if args.deadline:
        sets.append("deadline = ?")
        params.append(args.deadline)
    if args.follow_up_after:
        sets.append("follow_up_after = ?")
        params.append(args.follow_up_after)
    if args.snooze_until:
        sets.append("snooze_until = ?")
        params.append(args.snooze_until)
    if args.priority:
        sets.append("priority = ?")
        params.append(args.priority)
    if args.conversation_id:
        sets.append("conversation_id = ?")
        params.append(args.conversation_id)
    if getattr(args, "gmail_thread_id", None):
        sets.append("gmail_thread_id = ?")
        params.append(args.gmail_thread_id)
    if args.email_archived:
        sets.append("email_archived = ?")
        params.append(1)

    for k, v in core_set.items():
        sets.append(f"{k} = ?")
        params.append(v)

    # Reopening a row that an archive closed shows the archive was not the
    # answer to it: tell ledger_sync never to close it on an archive again.
    # Any reopen also stamps reopened_at: the evidence up to now did not
    # finish the work, so the scrubber counts only evidence after it.
    reopening = row["status"] in ("completed", "closed") and status in OPEN_STATUSES
    if reopening:
        import closure
        try:
            prior_meta = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            prior_meta = {}
        # The event, and a veto on the signal that closed it: that evidence
        # was not the answer, and only newer evidence may close it again.
        reopened = closure.note_reopen(db, row, actor="cli:" + _lane(), meta=prior_meta)
        meta_updates["reopened_at"] = reopened["reopened_at"]
        meta_updates["close_vetoes"] = reopened.get("close_vetoes", {})
        closing = f"{row['last_action'] or ''} {prior_meta.get('closed_note') or ''}"
        if ("Auto-closed: email archived from inbox" in closing
                or "(user archived conversation email)" in closing):
            meta_updates["archive_not_evidence"] = f"{now[:10]}: reopened after an archive closed it"

    # An update to a closed row does not reopen it. Say so, with how it was
    # closed, so the caller does not report as open work the ledger has
    # closed; and keep the closing note, which a new last_action replaces.
    warning = ""
    if row["status"] in ("completed", "closed") and status not in OPEN_STATUSES:
        closing = (row["last_action"] or "").strip()
        warning = (f"entry {args.id} is {row['status']} (closed_at {row['closed_at'] or '?'}"
                   f"{': ' + closing[:160] if closing else ''}). This update does not "
                   f"reopen it; pass --status open if the work is still owed.")
        print(f"NOTE: {warning}", file=sys.stderr)
        if args.last_action and closing:
            try:
                prior = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                prior = {}
            if not prior.get("closed_note"):
                meta_updates["closed_note"] = closing

    if meta_updates:
        existing_meta = {}
        try:
            existing_meta = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        existing_meta.update(meta_updates)
        sets.append("metadata = ?")
        params.append(json.dumps(existing_meta))

    params.append(args.id)
    db.execute(f"UPDATE ledger SET {', '.join(sets)} WHERE id = ?", params)
    db.commit()

    settled = None
    if settle_close:
        import closure
        signal, evidence, _err = _close_signal(
            argparse.Namespace(id=args.id, duplicate_of=None, user_said=None,
                               user_rule=None, signal=None, evidence=None), row)
        settled = closure.settle(db, args.id, signal=signal,
                                 actor="cli:" + (_lane() if not _is_owner() else "owner"),
                                 note=args.last_action or "closed", evidence=evidence or None)
        db.commit()

    updated = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    db.close()
    out = {"ok": True, "action": "updated", "entry": _row_to_dict(updated)}
    if settled is not None:
        out["close"] = settled["action"]
        if settled["action"] == "proposed":
            out["message"] = ("The close is recorded as a proposal; the row stays open "
                              "until the user confirms. Use `close --user-said` when "
                              "the user told you it is done.")
    if warning:
        out["warning"] = warning
    _output(out)
    return 0


# Signals only an owner process may name on `close --signal`: the bot's
# buttons, the dashboard, item feedback and the code closers run as the
# owner. A model lane runs as the resident and closes on the user's word
# (--user-said), a standing rule (--user-rule) or a duplicate
# (--duplicate-of); anything else it closes becomes a proposal.
RESIDENT_SIGNALS = ("user_stated", "user_rule", "duplicate_of")


def _lane() -> str:
    """The model lane a resident close comes from, for its signal name."""
    run = os.environ.get("EA_RUN_ID", "") or ""
    if run.startswith("cron-"):
        parts = run[5:].split("-")
        while parts and parts[-1].isdigit():
            parts.pop()
        return "-".join(parts) or "cron"
    return "chat" if run else "cli"


def _close_signal(args, row) -> tuple[str, dict, str]:
    """(signal, evidence, error) for a close request."""
    from closure import USER_SIGNALS, LINKED_SIGNALS, REPLY_SIGNALS, \
        ARCHIVE_SIGNALS, STRUCTURAL_SIGNALS
    known = USER_SIGNALS | LINKED_SIGNALS | REPLY_SIGNALS | ARCHIVE_SIGNALS | STRUCTURAL_SIGNALS
    evidence: dict = {}
    if getattr(args, "evidence", None):
        try:
            evidence = json.loads(args.evidence)
            if not isinstance(evidence, dict):
                evidence = {"evidence": evidence}
        except ValueError:
            evidence = {"evidence": args.evidence}
    dup = getattr(args, "duplicate_of", None)
    if dup:
        if dup == args.id:
            return "", evidence, "--duplicate-of names the row itself"
        if not entry_exists(dup):
            return "", evidence, f"--duplicate-of {dup}: no such row"
        evidence["duplicate_of"] = dup
        return "duplicate_of", evidence, ""
    if getattr(args, "user_said", None):
        evidence["quote"] = args.user_said
        return "user_stated", evidence, ""
    if getattr(args, "user_rule", None):
        evidence["rule"] = args.user_rule
        return "user_rule", evidence, ""
    sig = (getattr(args, "signal", None) or "").strip()
    if sig:
        if sig.startswith("llm:"):
            return sig, evidence, ""
        if sig not in known:
            return "", evidence, f"unknown --signal {sig!r}"
        if sig in RESIDENT_SIGNALS or _is_owner():
            return sig, evidence, ""
        evidence["asked_signal"] = sig
    if _is_owner():
        # The owner at a shell: the close is their own word.
        evidence["via"] = "owner process"
        return "user_stated", evidence, ""
    return f"llm:{_lane()}", evidence, ""


def _archive_after_close(db, row, closed_ids: list[str], skip: bool) -> dict:
    """Move the closed rows' mail out of the inbox. The whole thread goes
    only when nothing open is left on it; otherwise only the closed rows'
    own messages, so an open row's mail stays where the user sees it."""
    out: dict = {}
    if skip:
        return out
    msg_id = row["source_message_id"]
    if msg_id and not row["email_archived"]:
        res = _archive_email(msg_id)
        out["email_archive"] = res
        if res.get("archived"):
            db.execute("UPDATE ledger SET email_archived = 1 WHERE id = ?", (row["id"],))
    conv_id = (row["conversation_id"] or "").strip()
    if not conv_id:
        return out
    still_open = db.execute(
        "SELECT id FROM ledger WHERE conversation_id = ? AND status IN "
        "('open', 'snoozed', 'follow_up') AND source IS NULL", (conv_id,)).fetchall()
    if not still_open:
        out["conversation_archive"] = _archive_conversation(conv_id)
        return out
    out["conversation_kept"] = [r[0] for r in still_open][:10]
    for cid in closed_ids[1:]:
        sib = db.execute("SELECT source_message_id, email_archived FROM ledger WHERE id = ?",
                         (cid,)).fetchone()
        if sib and sib[0] and not sib[1]:
            _archive_email(sib[0])
    return out


def cmd_close(args) -> int:
    """Close an entry on decisive evidence, or record a proposal.

    closure.settle applies the policy to the request's signal: the user's
    word (--user-said), a standing rule (--user-rule), a duplicate
    (--duplicate-of) or a signal an owner process names (--signal) can
    close; anything else a model lane asks for is recorded as a proposal
    the user confirms in one click, and the row stays open."""
    import closure
    if not args.id:
        _output({"error": "--id is required"})
        return 1

    db = _get_db()
    row = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    if not row:
        _output({"error": f"Entry not found: {args.id}"})
        db.close()
        return 1
    signal, evidence, err = _close_signal(args, row)
    if err:
        _output({"error": err})
        db.close()
        return 1
    note = args.note or "closed"
    actor = "cli:" + (_lane() if not _is_owner() else "owner")
    res = closure.settle(db, args.id, signal=signal, actor=actor, note=note,
                         evidence=evidence or None,
                         evidence_ts=getattr(args, "evidence_ts", None) or "",
                         sweep=False if getattr(args, "no_sweep", False) else None)
    db.commit()
    out: dict = {"ok": res["action"] in ("closed", "proposed"), "action": res["action"],
                 "signal": signal}
    if res["action"] == "closed":
        out.update(_archive_after_close(db, row, res["ids"], args.skip_archive))
        db.commit()
        if len(res["ids"]) > 1:
            out["co_closed_siblings"] = res["ids"][1:]
    elif res["action"] == "proposed":
        out["message"] = (
            "Recorded as a proposal; the row stays open. The user confirms it in "
            "chat with one click. If the user told you it is done, close it with "
            "--user-said \"<their words>\"; if it repeats another row, with "
            "--duplicate-of <id>.")
    else:
        out["why"] = res.get("why", "")
    updated = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    db.close()
    out["entry"] = _row_to_dict(updated)
    return _output(out) or 0


def cmd_propose(args) -> int:
    """Record that a row looks done, without closing it."""
    import closure
    db = _get_db()
    if not db.execute("SELECT 1 FROM ledger WHERE id = ?", (args.id,)).fetchone():
        _output({"error": f"Entry not found: {args.id}"})
        db.close()
        return 1
    sig = (args.signal or "").strip() or f"llm:{_lane()}"
    evidence = {"evidence": args.evidence} if args.evidence else None
    res = closure.propose(db, args.id, signal=sig, actor="cli:" + _lane(),
                          note=args.why or "", evidence=evidence,
                          evidence_ts=args.evidence_ts or "")
    db.commit()
    db.close()
    _output({"ok": res["action"] == "proposed", **res})
    return 0


def cmd_confirm(args) -> int:
    """The user agreed a proposed row is done: close it. The bot's card
    click runs this as the owner; a model lane that heard the user say so
    closes with `close --user-said` instead, which records their words."""
    import closure
    if not _is_owner():
        _output({"error": "confirm is the user's click on the card; when the user "
                          "told you the row is done, use close --user-said \"<their words>\""})
        return 1
    db = _get_db()
    res = closure.confirm(db, args.id, actor=args.actor or "user")
    db.commit()
    out = {"ok": res["action"] == "closed", **res}
    if res["action"] == "closed":
        row = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
        out.update(_archive_after_close(db, row, res["ids"], args.skip_archive))
        db.commit()
    db.close()
    _output(out)
    return 0


def cmd_reject(args) -> int:
    """The user says a proposed row is still open: keep it, veto the signal."""
    import closure
    db = _get_db()
    res = closure.reject(db, args.id, actor=args.actor or "user")
    db.commit()
    db.close()
    _output({"ok": res["action"] == "kept_open", **res})
    return 0


def cmd_proposals(args) -> int:
    """Open rows that carry a close proposal. `--mark-carded` notes that
    the listed ids are on a confirm card, so the next card skips them."""
    import closure
    db = _get_db()
    if getattr(args, "mark_carded", None):
        closure.mark_carded(db, args.mark_carded)
        db.commit()
    rows = closure.pending_proposals(db)
    db.close()
    _output({"count": len(rows), "entries": [
        {"id": r["id"], "type": r.get("type"), "description": r.get("description"),
         "from_email": r.get("from_email"), "conversation_id": r.get("conversation_id"),
         "proposal": r["close_proposal"]} for r in rows]})
    return 0


def cmd_closure_report(args) -> int:
    """Per signal: closes, false closes (reopened), proposals and their fate."""
    import closure
    db = _get_db()
    since = ""
    if args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    rep = closure.report(db, since=since)
    db.close()
    _output(rep, pretty=True)
    return 0


def cmd_get(args) -> int:
    """Get a single entry by ID."""
    if not args.id:
        _output({"error": "--id is required"})
        return 1

    db = _get_db()
    row = db.execute("SELECT * FROM ledger WHERE id = ?", (args.id,)).fetchone()
    db.close()

    if not row:
        _output({"error": f"Entry not found: {args.id}"})
        return 1

    _output(_row_to_dict(row))
    return 0


def cmd_list(args) -> int:
    """List entries with optional filters."""
    db = _get_db()
    where = []
    params = []

    if args.status:
        statuses = _status_filter(args.status)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)

    if args.type:
        types = sorted({canonical_type(t) for t in args.type.split(",") if t.strip()})
        where.append(f"type IN ({','.join('?' * len(types))})")
        params.extend(types)

    if args.stale:
        # Items older than N days that are still open
        where.append("status NOT IN ('completed', 'closed')")
        where.append("date_identified IS NOT NULL")
        where.append("""
            julianday('now') - julianday(
                CASE WHEN date_identified LIKE '%T%'
                     THEN substr(date_identified, 1, 10)
                     ELSE date_identified END
            ) >= ?
        """)
        params.append(args.stale)

    if args.from_email:
        where.append("from_email LIKE ?")
        params.append(f"%{args.from_email}%")

    if args.has_deadline:
        where.append("deadline IS NOT NULL")
        # Default to open items only (like --stale) unless status already filtered
        if not args.status:
            where.append("status NOT IN ('completed', 'closed')")

    # Source filter: defaults to main-EA-only. Plugins pass --source
    # plugin:<name> via the ea_plugins.ledger helper; admin paths can
    # pass --include-plugins to see everything.
    src_sql, src_params = _source_filter_sql(
        getattr(args, "source", None),
        getattr(args, "include_plugins", False),
    )
    if src_sql:
        # _source_filter_sql returns " AND ..."; this list is joined with
        # " AND " of its own, so the fragment goes in bare.
        where.append(src_sql.removeprefix(" AND "))
        params.extend(src_params)

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = min(args.limit or 50, 200)

    rows = db.execute(
        f"SELECT * FROM ledger{where_clause} ORDER BY date_identified DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()

    entries = [_row_to_dict(r) for r in rows]
    _output({"count": len(entries), "entries": entries})
    return 0


def cmd_search(args) -> int:
    """Search entries by text, by conversation id, or both.

    `--conversation-id` matches the COLUMN exactly. The LIKE below covers
    text columns only, so a conversation id passed as --query matches
    nothing: callers looking a thread up by id need the exact-column form.
    """
    conv_id = (getattr(args, "conversation_id", None) or "").strip()
    if not args.query and not conv_id:
        _output({"error": "--query or --conversation-id is required"})
        return 1

    db = _get_db()
    clauses = []
    params: list = []

    if args.query:
        q = f"%{args.query}%"
        clauses.append("(description LIKE ? OR notes LIKE ? OR from_name LIKE ?"
                       " OR from_email LIKE ? OR last_action LIKE ?)")
        params.extend([q, q, q, q, q])

    if conv_id:
        clauses.append("conversation_id = ?")
        params.append(conv_id)

    text_match = " AND ".join(clauses)

    if args.status:
        statuses = _status_filter(args.status)
        status_filter = f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    else:
        status_filter = ""

    src_sql, src_params = _source_filter_sql(
        getattr(args, "source", None),
        getattr(args, "include_plugins", False),
    )
    params.extend(src_params)

    rows = db.execute(f"""
        SELECT * FROM ledger
        WHERE {text_match}{status_filter}{src_sql}
        ORDER BY date_identified DESC LIMIT ?
    """, params + [args.limit or 20]).fetchall()
    db.close()

    entries = [_row_to_dict(r) for r in rows]
    out = {"count": len(entries), "query": args.query, "entries": entries}
    if conv_id:
        out["conversation_id"] = conv_id
    _output(out)
    return 0


def cmd_stats(args) -> int:
    """Quick summary statistics."""
    db = _get_db()
    src_sql, src_params = _source_filter_sql(
        getattr(args, "source", None),
        getattr(args, "include_plugins", False),
    )
    # _source_filter_sql returns " AND ..."; for queries with no WHERE
    # clause, swap leading AND for WHERE. For the queries below that
    # already have a WHERE, just append the fragment.
    src_where_only = src_sql.replace(" AND ", " WHERE ", 1) if src_sql else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM ledger{src_where_only}",
        src_params
    ).fetchone()[0]
    by_status = db.execute(
        f"SELECT status, COUNT(*) as n FROM ledger{src_where_only} "
        f"GROUP BY status ORDER BY n DESC",
        src_params
    ).fetchall()
    by_type = db.execute(
        f"SELECT type, COUNT(*) as n FROM ledger "
        f"WHERE status NOT IN ('completed','closed'){src_sql} "
        f"GROUP BY type ORDER BY n DESC",
        src_params
    ).fetchall()
    stale_7 = db.execute(f"""
        SELECT COUNT(*) FROM ledger
        WHERE status NOT IN ('completed','closed')
        AND date_identified IS NOT NULL
        AND julianday('now') - julianday(
            CASE WHEN date_identified LIKE '%T%'
                 THEN substr(date_identified, 1, 10)
                 ELSE date_identified END
        ) >= 7
        {src_sql}
    """, src_params).fetchone()[0]
    upcoming_deadlines = db.execute(f"""
        SELECT id, description, deadline FROM ledger
        WHERE status NOT IN ('completed','closed')
        AND deadline IS NOT NULL
        AND deadline >= date('now')
        {src_sql}
        ORDER BY deadline LIMIT 5
    """, src_params).fetchall()
    db.close()

    _output({
        "total": total,
        "by_status": {r[0]: r[1] for r in by_status},
        "open_by_type": {r[0]: r[1] for r in by_type},
        "stale_7_days": stale_7,
        "upcoming_deadlines": [
            {"id": r[0], "description": brief_title(r[1], 60), "deadline": r[2]}
            for r in upcoming_deadlines
        ],
    })
    return 0


def cmd_dump(args) -> int:
    """Dump all entries as JSON (for backup/debug)."""
    db = _get_db()
    where = ""
    params = []
    if args.status:
        statuses = _status_filter(args.status)
        where = f" WHERE status IN ({','.join('?' * len(statuses))})"
        params = statuses

    rows = db.execute(
        f"SELECT * FROM ledger{where} ORDER BY date_identified DESC", params
    ).fetchall()
    db.close()

    entries = [_row_to_dict(r) for r in rows]
    _output(entries, pretty=True)
    return 0


def cmd_import_json(args) -> int:
    """Import entries from the legacy JSON ledger file."""
    path = Path(args.path)
    if not path.exists():
        _output({"error": f"File not found: {path}"})
        return 1

    with open(path) as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        _output({"error": "Expected a JSON array"})
        return 1

    db = _get_db()
    now = _now()
    imported = 0
    skipped = 0
    errors = []

    for entry in entries:
        entry_id = entry.get("id", "")
        if not entry_id:
            errors.append(f"Entry missing 'id': {str(entry)[:80]}")
            continue

        if db.execute("SELECT 1 FROM ledger WHERE id = ?", (entry_id,)).fetchone():
            skipped += 1
            continue

        entry_type = canonical_type(entry.get("type")) or "task"
        try:
            status = checked_status(entry.get("status", "open"))[0]
        except ValueError:
            status = "open"
        if status in ("pending", "pending_reply", "pending_approval", "waiting"):
            status = "open"

        description = entry.get("description", "")
        from_email = entry.get("from", "")
        from_name = entry.get("from_name", "")
        source_message_id = entry.get("source_message_id", "")
        conversation_id = entry.get("conversation_id", "")
        date_identified = entry.get("date_identified", entry.get("created", ""))
        deadline = entry.get("deadline") or entry.get("due_by") or entry.get("due_date")
        follow_up_after = entry.get("follow_up_after")
        snooze_until = entry.get("snooze_until")
        last_action = entry.get("last_action", "")
        notes = entry.get("notes", "")
        priority = entry.get("priority")
        closed_at = entry.get("closed_at") or entry.get("completed_date")

        # Everything else goes in metadata
        skip_keys = {
            "id", "type", "status", "description", "from", "from_name",
            "source_message_id", "conversation_id", "date_identified", "created",
            "deadline", "due_by", "due_date", "follow_up_after", "snooze_until",
            "last_action", "notes", "priority", "closed_at", "completed_date",
            "days_stale", "completed_note", "completion_note",
        }
        metadata = {k: v for k, v in entry.items() if k not in skip_keys}
        # Fold completion notes into last_action if present
        comp_note = entry.get("completed_note") or entry.get("completion_note")
        if comp_note and not last_action:
            last_action = comp_note

        try:
            db.execute("""
                INSERT INTO ledger (id, type, status, description, from_email, from_name,
                                   source_message_id, conversation_id, date_identified,
                                   deadline, follow_up_after, snooze_until, last_action,
                                   notes, priority, metadata, created_at, updated_at,
                                   closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry_id, entry_type, status, description, from_email, from_name,
                source_message_id, conversation_id, date_identified, deadline,
                follow_up_after, snooze_until, last_action, notes,
                priority, json.dumps(metadata), now, now, closed_at,
            ))
            imported += 1
        except Exception as e:
            errors.append(f"{entry_id}: {e}")

    db.commit()
    db.close()

    result = {"ok": True, "imported": imported, "skipped": skipped}
    if errors:
        result["errors"] = errors[:10]
    _output(result)
    return 0


# ── Programmatic API (for import by preprocess.py etc.) ─────────────────

def get_open_entries(db_path: Path | None = None,
                     source: str | None = None,
                     include_plugins: bool = False) -> list[dict]:
    """Return all non-closed entries. For use by preprocessor.

    By default returns only main-EA rows (source IS NULL). Set
    include_plugins=True for cross-cutting reads, or pass source="plugin:<name>"
    for a plugin-scoped read.
    """
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(
        f"SELECT * FROM ledger WHERE status NOT IN ('completed','closed'){src_sql} "
        f"ORDER BY date_identified DESC",
        src_params
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def set_metadata(entry_id: str, db_path: Path | None = None, **fields) -> bool:
    """Merge fields into one row's metadata without touching updated_at
    (a brief refresh is not activity on the item). For owner-side
    writers such as mail_privacy.sync_briefs. False when the row is
    missing."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    try:
        row = conn.execute("SELECT metadata FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return False
        try:
            meta = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(fields)
        conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?",
                     (json.dumps(meta, sort_keys=True, default=str), entry_id))
        conn.commit()
        return True
    finally:
        conn.close()


def get_entry(entry_id: str, db_path: Path | None = None) -> dict | None:
    """Get a single entry by ID. For use by preprocessor."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def entry_exists(entry_id: str, db_path: Path | None = None) -> bool:
    """Check if an entry exists. For use by preprocessor."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    exists = conn.execute("SELECT 1 FROM ledger WHERE id = ?", (entry_id,)).fetchone() is not None
    conn.close()
    return exists


def parked_until(entry: dict, today=None) -> str:
    """The date before which this row must not be raised, or ''.

    A future `follow_up_after` or `snooze_until` parks a row whatever its
    status: the user or the EA set it to say "not before this day". The
    later of the two wins. `today` is a local date (the dates are written
    in the user's own terms); it defaults to the machine's local today."""
    if today is None:
        today = datetime.now().astimezone().date()
    best = ""
    for field in ("follow_up_after", "snooze_until"):
        raw = str(entry.get(field) or "").strip()[:10]
        if len(raw) == 10 and raw > today.isoformat() and raw > best:
            best = raw
    return best


# How long a row whose thread ends on the user's own question stays off
# the do-list: after this the briefing raises it again as unanswered.
WAITING_DAYS = 5


def waiting_on_reply(entry: dict, today=None) -> dict:
    """{"since", "asked"} when the row's thread ends on the user's own
    message asking the other side something, sent within WAITING_DAYS;
    else {}. The thread brief (mail_privacy.thread_brief) records it; a
    newer inbound message clears it there. `today` is a local date."""
    brief = entry.get("thread_brief")
    wait = brief.get("waiting_on_reply") if isinstance(brief, dict) else None
    if not isinstance(wait, dict) or not wait.get("since"):
        return {}
    if today is None:
        today = datetime.now().astimezone().date()
    try:
        since = datetime.fromisoformat(str(wait["since"]).replace("Z", "+00:00"))
    except ValueError:
        return {}
    if (today - since.astimezone().date()).days > WAITING_DAYS:
        return {}
    return {"since": wait["since"], "asked": str(wait.get("asked") or "")}


def unanswered_since(entry: dict, today=None) -> str:
    """The local date of the user's own question that has waited past
    WAITING_DAYS with no answer in the thread, or ''."""
    brief = entry.get("thread_brief")
    wait = brief.get("waiting_on_reply") if isinstance(brief, dict) else None
    if not isinstance(wait, dict) or not wait.get("since") or waiting_on_reply(entry, today):
        return ""
    try:
        since = datetime.fromisoformat(str(wait["since"]).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return since.astimezone().date().isoformat()


def resurrect_due_follow_ups(db_path: Path | None = None,
                             source: str | None = None,
                             include_plugins: bool = False) -> list[dict]:
    """Reopen follow_up items whose follow_up_after date has arrived.

    Changes status from 'follow_up' to 'open' and returns the list of
    resurrected entries. Called by the preprocessor before briefing
    generation; scoped to main-EA rows by default so plugins manage their
    own resurrection.
    """
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    now = _now()
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT * FROM ledger
        WHERE status = 'follow_up'
        AND follow_up_after IS NOT NULL
        AND follow_up_after <= date('now')
        {src_sql}
    """, src_params).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(f"""
            UPDATE ledger SET status = 'open', updated_at = ?,
                   last_action = 'Auto-resurrected: follow_up_after date reached'
            WHERE id IN ({','.join('?' * len(ids))})
        """, [now] + ids)
        conn.commit()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_closed_conversation_ids(db_path: Path | None = None,
                                source: str | None = None,
                                include_plugins: bool = False) -> set[str]:
    """Return conversation_ids of all closed/completed ledger entries.

    Used by the preprocessor to filter out items that have already been
    handled — prevents stale items from appearing in briefings.
    """
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT DISTINCT conversation_id FROM ledger
        WHERE status IN ('completed', 'closed')
        AND conversation_id IS NOT NULL AND conversation_id != ''
        {src_sql}
    """, src_params).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_closed_source_message_ids(db_path: Path | None = None,
                                  source: str | None = None,
                                  include_plugins: bool = False) -> set[str]:
    """Return source_message_ids of all closed/completed ledger entries.

    Used by the preprocessor to filter out items whose specific email
    has been handled, even if the conversation_id is shared.
    """
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT DISTINCT source_message_id FROM ledger
        WHERE status IN ('completed', 'closed')
        AND source_message_id IS NOT NULL AND source_message_id != ''
        {src_sql}
    """, src_params).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_all_source_message_ids(db_path: Path | None = None,
                               source: str | None = None,
                               include_plugins: bool = False) -> set[str]:
    """Return source_message_ids from ALL ledger entries (any status).

    Used by priority-check and scheduling-check to avoid re-notifying
    about emails already tracked in the ledger.
    """
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT DISTINCT source_message_id FROM ledger
        WHERE source_message_id IS NOT NULL AND source_message_id != ''
        {src_sql}
    """, src_params).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_closed_conversation_times(db_path: Path | None = None,
                                  source: str | None = None,
                                  include_plugins: bool = False
                                  ) -> dict[str, str]:
    """Map conversation_id -> latest closed_at ISO for completed/closed entries.

    Used by the preprocessor to SUPPRESS re-derivation: an inbox message in a
    conversation the user already closed must not become a fresh
    needs_response/dropped item unless it arrived AFTER the closure.

    Source-scoped like every other getter here, main-EA-only by default.
    Without the filter a plugin closing its OWN row puts that conversation
    into the map, and closure_scrubber's closed-sibling signal then sweeps
    the main-EA rows of the same thread."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT conversation_id, MAX(COALESCE(closed_at, updated_at))
        FROM ledger
        WHERE status IN ('completed', 'closed')
          AND conversation_id IS NOT NULL AND conversation_id != ''
          {src_sql}
        GROUP BY conversation_id
    """, src_params).fetchall()
    conn.close()
    return {r[0]: (r[1] or "") for r in rows}


def get_closed_entries_brief(db_path: Path | None = None,
                             source: str | None = None,
                             include_plugins: bool = False,
                             since: str | None = None) -> list[dict]:
    """Completed/closed rows with the fields a closure signal needs to
    judge them: id, type, description, notes (first 400 chars),
    last_action, conversation_id, created_at, closed_at, from_email.

    closure_scrubber's identifier-sibling signal reads these: a hand-made
    row that names manuscript or proposal ids closes once every row that
    carries the same id was closed by the user. `since` (ISO) bounds
    closed_at from below. Source-scoped like every other getter here,
    main-EA-only by default."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    params = list(src_params)
    since_sql = ""
    if since:
        since_sql = " AND COALESCE(closed_at, updated_at) >= ?"
        params.append(since)
    rows = conn.execute(f"""
        SELECT id, type, description, substr(COALESCE(notes, ''), 1, 400),
               COALESCE(last_action, ''), COALESCE(conversation_id, ''),
               COALESCE(created_at, ''), COALESCE(closed_at, updated_at, ''),
               lower(COALESCE(from_email, ''))
        FROM ledger
        WHERE status IN ('completed', 'closed'){src_sql}{since_sql}
    """, params).fetchall()
    conn.close()
    return [{
        "id": r[0], "type": r[1] or "", "description": r[2] or "",
        "notes": r[3] or "", "last_action": r[4], "conversation_id": r[5],
        "created_at": r[6], "closed_at": r[7], "from_email": r[8],
    } for r in rows]


def get_recently_closed(days: int = 7,
                        db_path: Path | None = None) -> list[dict]:
    """Entries closed in the last N days: {id, description, closed_at, note}.

    Included in the briefing JSON as `recently_closed` so the model KNOWS
    what was just resolved and never re-surfaces it from carried-forward
    narrative."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, description, COALESCE(closed_at, updated_at) AS closed_at,
               last_action, conversation_id
        FROM ledger
        WHERE status IN ('completed', 'closed')
          AND COALESCE(closed_at, updated_at) >= ?
        ORDER BY closed_at DESC
    """, (cutoff,)).fetchall()
    conn.close()
    return [{"id": r["id"], "description": brief_title(r["description"], 120),
             "closed_at": r["closed_at"], "note": brief_title(r["last_action"], 120),
             "conversation_id": r["conversation_id"] or ""}
            for r in rows]


# A description is a title. Where a brief shows one in a fixed width,
# the cut lands on a word and says it is a cut, so a reader never sees
# "send the CSCI 5848 recomme".
TITLE_CHARS = 140


def brief_title(text: str, limit: int = 120) -> str:
    """`text` whole when it fits, else its longest word-boundary prefix
    under `limit` with an ellipsis."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def _derive_conversation_id(source_message_id: str) -> str:
    """conversation_id for a message id, or "" if it cannot be had.

    Every email closure signal joins on conversation_id, so a row without
    one can never be closed by evidence at any revision of
    closure_scrubber. Deriving it from source_message_id is one Graph read
    and no guessing; matching an id-less row to a thread by description
    tokens is not attempted, because it is wrong more often than right.

    Never raises and never blocks the add: a ledger row without a
    conversation id is worse than one with, but far better than none.
    """
    if not source_message_id:
        return ""
    r = _graph_cli("get-message", "--message-id", source_message_id)
    if r.get("error"):
        return ""
    return (r.get("conversation_id") or "").strip()


def add_entry(entry_id: str, entry_type: str, description: str,
              from_email: str = "", from_name: str = "",
              source_message_id: str = "", conversation_id: str = "",
              gmail_thread_id: str | None = None,
              notes: str = "", priority: str | None = None,
              source: str | None = None,
              db_path: Path | None = None,
              deadline: str | None = None,
              metadata: dict | None = None) -> bool:
    """Programmatic API to add a ledger entry. Returns True if added, False if duplicate.

    `source` is NULL for main-EA rows or 'plugin:<name>' when called by
    the ea_plugins.ledger helper. Callers from EA core leave it None.
    `deadline` is YYYY-MM-DD or None; `metadata` is the row's initial
    metadata object (default empty).
    """
    entry_type = checked_type(entry_type, source)
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT 1 FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    if existing:
        conn.close()
        return False
    # Capture the join key the closure signals need, while we still have
    # the message in hand.
    if source_message_id and not conversation_id:
        conversation_id = _derive_conversation_id(source_message_id)
    if not conversation_id and not source_message_id and not gmail_thread_id:
        print(f"NOTE: ledger row {entry_id!r} has no conversation_id, "
              f"source_message_id or gmail_thread_id. No email evidence can "
              f"ever auto-close it; it will stay open until closed by hand. "
              f"If this came from an email, pass --source-message-id.",
              file=sys.stderr)
    if len(description or "") > TITLE_CHARS:
        print(f"NOTE: ledger row {entry_id!r} has a {len(description)}-character description. "
              f"A description is a title (under {TITLE_CHARS} characters): briefings show it cut. "
              f"Put the detail in --notes.", file=sys.stderr)
    now = _now()
    conn.execute("""
        INSERT INTO ledger (id, type, status, description, from_email, from_name,
                           source_message_id, conversation_id, gmail_thread_id,
                           date_identified, deadline,
                           last_action, notes, priority, source, metadata,
                           created_at, updated_at)
        VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry_id, entry_type, description, from_email, from_name,
        source_message_id, conversation_id, gmail_thread_id, now,
        deadline or None,
        f"Auto-added by {entry_type} check", notes, priority, source,
        json.dumps(metadata or {}), now, now,
    ))
    conn.commit()
    conn.close()
    return True


def get_stale_entries(days: int = 7, db_path: Path | None = None,
                      source: str | None = None,
                      include_plugins: bool = False) -> list[dict]:
    """Return open entries older than N days. For preprocessor briefing."""
    path = db_path or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    src_sql, src_params = _source_filter_sql(source, include_plugins)
    rows = conn.execute(f"""
        SELECT * FROM ledger
        WHERE status NOT IN ('completed', 'closed')
        AND date_identified IS NOT NULL
        AND julianday('now') - julianday(
            CASE WHEN date_identified LIKE '%T%'
                 THEN substr(date_identified, 1, 10)
                 ELSE date_identified END
        ) >= ?
        {src_sql}
        ORDER BY date_identified ASC
    """, [days] + src_params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── CLI entrypoint ──────────────────────────────────────────────────────

def normalize_rows(conn: sqlite3.Connection) -> dict:
    """Fold every row onto the vocabulary, in place; the caller commits.

    Types and statuses take their canonical words (a status that names a
    kind of row, such as 'waiting', becomes that type and 'open'). A
    metadata key that spells a column moves into the column when the
    column is empty; a later date wins for follow_up_after and
    snooze_until; any other text is kept by appending it to notes.
    date_identified takes one format. A main-EA type that maps to nothing
    is reported and left alone. Running it twice changes nothing."""
    conn.row_factory = sqlite3.Row
    report = {"rows": 0, "changed": 0, "types": {}, "statuses": {},
              "moved_keys": [], "dates": 0, "unknown_types": []}
    for row in conn.execute("SELECT * FROM ledger").fetchall():
        report["rows"] += 1
        upd: dict = {}
        plugin = str(row["source"] or "").startswith("plugin:")
        try:
            status, implied = checked_status(row["status"])
        except ValueError:
            status, implied = row["status"], ""
        new_type = implied or canonical_type(row["type"])
        if not plugin and new_type not in TYPES:
            report["unknown_types"].append([row["id"], row["type"]])
            new_type = row["type"]
        if new_type != row["type"]:
            upd["type"] = new_type
            key = f"{row['type']} -> {new_type}"
            report["types"][key] = report["types"].get(key, 0) + 1
        if status != row["status"]:
            upd["status"] = status
            key = f"{row['status']} -> {status}"
            report["statuses"][key] = report["statuses"].get(key, 0) + 1

        try:
            meta = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        notes = row["notes"] or ""
        meta_changed = False
        for key in list(meta):
            name = field_name(key)
            if name == key and name not in CORE_COLUMNS:
                continue
            value = meta.pop(key)
            meta_changed = True
            if name not in CORE_COLUMNS or name in ("id", "type", "status", "metadata",
                                                     "created_at", "updated_at"):
                if name not in CORE_COLUMNS:
                    meta.setdefault(name, value)
                continue
            if value in (None, "", "None"):
                continue
            current = upd.get(name, row[name])
            if current in (None, "", "None"):
                upd[name] = value
            elif str(current) == str(value):
                pass
            elif name in ("follow_up_after", "snooze_until"):
                upd[name] = max(str(current), str(value))
            else:
                notes = f"{notes}\nEarlier note, written to metadata as {key}: {value}".strip()
            report["moved_keys"].append([row["id"], key, name])
        if meta_changed:
            upd["metadata"] = json.dumps(meta)
        if notes != (row["notes"] or ""):
            upd["notes"] = notes

        date = iso_utc(row["date_identified"])
        if date != row["date_identified"]:
            upd["date_identified"] = date
            report["dates"] += 1

        if upd:
            report["changed"] += 1
            cols = ", ".join(f"{k} = ?" for k in upd)
            conn.execute(f"UPDATE ledger SET {cols} WHERE id = ?", (*upd.values(), row["id"]))
    return report


def cmd_normalize(args) -> int:
    """Report what normalize_rows would change; write it with --apply."""
    path = args.db and Path(args.db) or DB_PATH
    conn = guarded_connect(path)
    try:
        report = normalize_rows(conn)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    report["applied"] = bool(args.apply)
    _output(report)
    return 0


def cmd_types(args) -> int:
    """The type and status words a row may carry."""
    _output({"types": TYPES, "statuses": list(STATUSES),
             "aliases": {**TYPE_ALIASES, **STATUS_IS_TYPE}})
    return 0


def cmd_repair_ids(args):
    """Backfill conversation_id on rows that carry a source_message_id.

    A row without conversation_id cannot be closed by any email signal, and
    older rows never captured the join key even when the message id was
    right there.

    Only rows that already name a message are touched, and the id is read
    from that message. Nothing is guessed: a row with no message id is
    reported and left alone.
    """
    path = args.db and Path(args.db) or DB_PATH
    conn = guarded_connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source_message_id FROM ledger "
        "WHERE status IN ('open','snoozed','follow_up') "
        "AND (conversation_id IS NULL OR conversation_id = '') "
        "AND source_message_id IS NOT NULL AND source_message_id != ''"
    ).fetchall()
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM ledger "
        "WHERE status IN ('open','snoozed','follow_up') "
        "AND (conversation_id IS NULL OR conversation_id = '') "
        "AND (source_message_id IS NULL OR source_message_id = '')"
    ).fetchone()["c"]

    repaired, failed = [], []
    for r in rows:
        cid = _derive_conversation_id(r["source_message_id"])
        if not cid:
            failed.append(r["id"])
            continue
        if not args.dry_run:
            conn.execute(
                "UPDATE ledger SET conversation_id = ?, updated_at = ? WHERE id = ?",
                (cid, _now(), r["id"]))
        repaired.append(r["id"])
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(json.dumps({
        "mode": "dry-run" if args.dry_run else "applied",
        "candidates": len(rows),
        "repaired": repaired,
        "unresolvable": failed,
        "no_message_id_left_alone": orphans,
        "note": ("Rows with no source_message_id cannot be repaired from "
                 "data and are left open by design."),
    }, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="EA obligations ledger (SQLite-backed)", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Add a new entry", allow_abbrev=False)
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--type", required=True)
    p_add.add_argument("--status", default="open")
    p_add.add_argument("--description", "-d")
    p_add.add_argument("--from-email")
    p_add.add_argument("--from-name")
    p_add.add_argument("--source-message-id")
    p_add.add_argument("--conversation-id")
    p_add.add_argument("--gmail-thread-id")
    p_add.add_argument("--date-identified")
    p_add.add_argument("--deadline")
    p_add.add_argument("--follow-up-after")
    p_add.add_argument("--last-action")
    p_add.add_argument("--notes")
    p_add.add_argument("--priority")
    p_add.add_argument("--closes-on", choices=CLOSES_ON,
                       help="What finishes this row: reply (the user's reply to its "
                            "correspondent), submission (an acknowledgement in its "
                            "thread), event (the meeting is set), manual (only the "
                            "user or a draft/event that names it)")
    p_add.add_argument("--set", nargs="*", metavar="KEY=VALUE",
                       help="Extra fields (core or metadata)")

    # update
    p_upd = sub.add_parser("update", help="Update an existing entry", allow_abbrev=False)
    p_upd.add_argument("--id", required=True)
    p_upd.add_argument("--status")
    p_upd.add_argument("--description")
    p_upd.add_argument("--last-action")
    p_upd.add_argument("--notes")
    p_upd.add_argument("--deadline")
    p_upd.add_argument("--follow-up-after")
    p_upd.add_argument("--snooze-until")
    p_upd.add_argument("--priority")
    p_upd.add_argument("--conversation-id")
    p_upd.add_argument("--gmail-thread-id")
    p_upd.add_argument("--email-archived", action="store_true")
    p_upd.add_argument("--closes-on", choices=CLOSES_ON,
                       help="What finishes this row (see add --closes-on)")
    p_upd.add_argument("--set", nargs="*", metavar="KEY=VALUE",
                       help="Extra fields (core or metadata)")

    # close
    p_close = sub.add_parser("close", help="Close/complete an entry (auto-archives email)", allow_abbrev=False)
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--note", help="Closing reason/note")
    p_close.add_argument("--skip-archive", action="store_true",
                         help="Don't archive the email (e.g., already archived)")
    p_close.add_argument("--user-said",
                         help="The user's own words saying the work is done "
                              "(closes; without this a model's close is a proposal)")
    p_close.add_argument("--duplicate-of",
                         help="Fold this row into another row that tracks the same "
                              "obligation (closes)")
    p_close.add_argument("--user-rule",
                         help="The standing user rule (preferences id or text) "
                              "that says this is no work (closes)")
    p_close.add_argument("--signal",
                         help="Evidence signal (owner processes; see closure.py)")
    p_close.add_argument("--evidence", help="Evidence ids, as JSON or text")
    p_close.add_argument("--evidence-ts",
                         help="When the evidence happened (ISO UTC); detector "
                              "siblings that came later stay open")
    p_close.add_argument("--no-sweep", action="store_true",
                         help="Close this row only, not the detector rows on its thread")

    p_prop = sub.add_parser("propose", help="Record that a row looks done (it stays open)", allow_abbrev=False)
    p_prop.add_argument("--id", required=True)
    p_prop.add_argument("--why", help="One line: why it looks done, in the user's terms")
    p_prop.add_argument("--signal", help="Evidence signal (default llm:<lane>)")
    p_prop.add_argument("--evidence", help="Evidence ids or text")
    p_prop.add_argument("--evidence-ts", help="When the evidence happened (UTC ISO); "
                        "evidence no newer than a 'Still open' on the row proposes nothing")

    p_conf = sub.add_parser("confirm", help="The user confirmed a proposed row is done", allow_abbrev=False)
    p_conf.add_argument("--id", required=True)
    p_conf.add_argument("--actor", default="user")
    p_conf.add_argument("--skip-archive", action="store_true")

    p_rej = sub.add_parser("reject", help="The user says a proposed row is still open", allow_abbrev=False)
    p_rej.add_argument("--id", required=True)
    p_rej.add_argument("--actor", default="user")

    p_props = sub.add_parser("proposals", help="List open rows that carry a close proposal", allow_abbrev=False)
    p_props.add_argument("--mark-carded", nargs="*", metavar="ID",
                         help="Note these ids are on a confirm card")

    p_rep = sub.add_parser("closure-report",
                           help="Closes, false closes and proposals per signal",
                           allow_abbrev=False)
    p_rep.add_argument("--days", type=int, default=30)

    # get
    p_get = sub.add_parser("get", help="Get a single entry", allow_abbrev=False)
    p_get.add_argument("--id", required=True)

    # list
    p_list = sub.add_parser("list", help="List entries with filters", allow_abbrev=False)
    p_list.add_argument("--status", help="Filter by status (comma-separated)")
    p_list.add_argument("--type", help="Filter by type (comma-separated)")
    p_list.add_argument("--stale", type=int, help="Items older than N days")
    p_list.add_argument("--from-email", help="Filter by sender email (substring)")
    p_list.add_argument("--has-deadline", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--source",
                       help="Filter by source ('plugin:<name>' or 'NULL' for main EA)")
    p_list.add_argument("--include-plugins", action="store_true",
                       help="Include plugin-sourced rows (default: main EA only)")

    # search
    p_search = sub.add_parser(
        "search", help="Search entries by text and/or conversation id",
        allow_abbrev=False)
    p_search.add_argument("--query", "-q",
                          help="Substring match on description/notes/from/"
                               "last_action (one of --query or "
                               "--conversation-id is required)")
    p_search.add_argument("--conversation-id",
                          help="Exact match on the conversation_id COLUMN "
                               "(what --query never covers)")
    p_search.add_argument("--status", help="Filter by status (comma-separated)")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--source",
                         help="Filter by source ('plugin:<name>' or 'NULL' for main EA)")
    p_search.add_argument("--include-plugins", action="store_true",
                         help="Include plugin-sourced rows (default: main EA only)")

    # stats
    p_stats = sub.add_parser("stats", help="Summary statistics", allow_abbrev=False)
    p_stats.add_argument("--source",
                        help="Filter by source ('plugin:<name>' or 'NULL' for main EA)")
    p_stats.add_argument("--include-plugins", action="store_true",
                        help="Include plugin-sourced rows (default: main EA only)")

    # dump
    p_dump = sub.add_parser("dump", help="Dump all entries as JSON", allow_abbrev=False)
    p_dump.add_argument("--status", help="Filter by status (comma-separated)")

    # import-json
    p_import = sub.add_parser("import-json", help="Import from legacy JSON file", allow_abbrev=False)
    p_import.add_argument("path", help="Path to obligations-ledger.json")

    # repair-ids
    p_rep = sub.add_parser(
        "repair-ids",
        help="Backfill conversation_id on open rows that name a message. "
             "Without it those rows can never be auto-closed.",
        allow_abbrev=False)
    p_rep.add_argument("--dry-run", action="store_true",
                       help="Report what would change, write nothing")
    p_rep.add_argument("--db", default=None, help="Ledger DB path override")

    p_norm = sub.add_parser(
        "normalize",
        help="Fold every row onto the type and status vocabulary, move "
             "column-named metadata into its column, one date format. "
             "Reports only; --apply writes.",
        allow_abbrev=False)
    p_norm.add_argument("--apply", action="store_true")
    p_norm.add_argument("--db", default=None, help="Ledger DB path override")

    sub.add_parser("types", help="The type and status words a row may carry", allow_abbrev=False)

    args = parser.parse_args()

    commands = {
        "add": cmd_add,
        "update": cmd_update,
        "close": cmd_close,
        "propose": cmd_propose,
        "confirm": cmd_confirm,
        "reject": cmd_reject,
        "proposals": cmd_proposals,
        "closure-report": cmd_closure_report,
        "get": cmd_get,
        "list": cmd_list,
        "search": cmd_search,
        "stats": cmd_stats,
        "dump": cmd_dump,
        "import-json": cmd_import_json,
        "repair-ids": cmd_repair_ids,
        "normalize": cmd_normalize,
        "types": cmd_types,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
