"""Per-Slack-thread Claude session registry.

Each Slack thread maps to one persistent Claude session:

  - First message of a thread: the bot generates a UUID, runs
    `claude -p --session-id <uuid>` with the full system prompt, and
    records thread_ts -> session here.
  - Replies in that thread: `claude -p --resume <uuid>` with ONLY the
    new user message (plus a short reminder header). The session carries
    the instructions, the tool history and the draft ids.
  - Resume failure (session file gone, CLI error): the bot invalidates
    the mapping and falls back to a fresh full-prompt session.

Sessions older than MAX_AGE_DAYS are not resumed (week-old briefing data
baked into the conversation outweighs the memory benefit) and are pruned
from the registry.

Adoption. A conversation held in the ea-bot shell
(scripts/ea-bot-shell.sh) is a Claude session in the same project
directory, as the same user, as the ones the bot resumes: the only thing
that makes it "a Slack thread's session" is a row here. `adopt()` writes
that row, so "adopt session <uuid>" in a Slack thread continues the shell
conversation there with its whole memory, and the shell can take it back
with `ea-bot-shell.sh --resume <uuid>` (or `--thread <ts>`). One driver at
a time: two live processes on one session both append to its file, and
the interactive one does not see turns added behind it until restarted.

Registry: ~/.ea/state/slack-sessions.json
  {thread_ts: {"session_id": uuid, "created": iso, "last_used": iso,
               "meta": {"use_mcp": bool, "model": str, "adopted_from": str}}}

The registry is in the resident's space and the bot, an owner process,
writes it through the resident (docs/SECURITY.md, "Owner trust rules").
It is read with the resident's access (resident_io), so a symlink or a
hard link at its name is refused, never followed. Every write is a
read-modify-write inside resident_io's exclusive lock (_mutate), so the
dispatch threads, a room rotation that moves many rows at once and the
shell's adopt never lose each other's rows. Only rows in the shape this
module writes are kept: an object with a session id and, when present, a
meta object. A registry that is there but cannot be read (a link at its
name among them) reads as empty and is never written over; a lookup then
mints a session it does not record.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import uuid
try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import resident_io as _rio
from datetime import datetime, timedelta, timezone
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
REGISTRY = EA_DIR / "state" / "slack-sessions.json"
MAX_AGE_DAYS = 7


def ea_bot_home() -> str:
    """The ea-bot account's home: /home/ea-bot on Linux, /Users/ea-bot on
    macOS. Read from the account, as the bot reads it at startup."""
    try:
        return pwd.getpwnam("ea-bot").pw_dir
    except KeyError:
        return "/home/ea-bot"


def project_dir_name(cwd: str) -> str:
    """Claude Code's folder name under ~/.claude/projects for a working
    directory: the path with every character outside [A-Za-z0-9] turned
    into '-'. Pass the physical path; the CLI names the folder after it."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# Where the ea-bot CLI keeps session transcripts for the project directory
# every lane runs in (~ea-bot/ea-system). The bot cannot read them (0600
# ea-bot) but can see that they exist, through the g+x the installer puts
# on ~ea-bot/.claude; that is all a resume or an adoption needs to check.
SESSIONS_DIR = Path(os.environ.get("EA_CLAUDE_SESSIONS_DIR") or os.path.join(
    ea_bot_home(), ".claude", "projects",
    project_dir_name(os.path.realpath(os.path.join(ea_bot_home(), "ea-system")))))
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# The lanes whose prompt is the whole of their instructions (the scheduling
# prompt, the slim reply prompt) run outside the deployed tree, so Claude
# Code adds neither CLAUDE.md nor CLAUDE.local.md to them. Each has its own
# empty working directory in the resident's state directory, where no
# memory file sits on the way up to the root (the EA tree's own CLAUDE.md
# copy in ~/.ea is why it is not under ~/.ea). Only the project subagents
# (.claude/agents) are mirrored in. A session keeps the directory it began
# in: its transcript lives in that directory's project folder, which is
# also how session_cwd() finds it again.
LANES = ("scheduling", "reply")
LANE_ROOT = Path(os.environ.get("EA_LANE_ROOT") or os.path.join(
    ea_bot_home(), ".local", "state", "ea-lanes"))


def lane_dir(lane: str) -> Path:
    if lane not in LANES:
        raise ValueError(f"not a lane: {lane!r}")
    return LANE_ROOT / lane


def _sessions_dir_for(cwd) -> Path:
    """The project folder beside SESSIONS_DIR for a working directory."""
    return SESSIONS_DIR.parent / project_dir_name(os.path.realpath(str(cwd)))


def lane_sessions_dirs() -> list[Path]:
    return [_sessions_dir_for(lane_dir(lane)) for lane in LANES]


def session_cwd(session_id: str) -> Path | None:
    """The lane directory a session began in, found by where its
    transcript lives; None for the deployed tree (every other session)."""
    if (SESSIONS_DIR / f"{session_id}.jsonl").exists():
        return None
    for lane in LANES:
        d = lane_dir(lane)
        if (_sessions_dir_for(d) / f"{session_id}.jsonl").exists():
            return d
    return None


def prepare_lane_dir(lane: str, agents_src: Path) -> Path | None:
    """The lane's working directory, made (as the resident) when missing,
    with the project subagents in `agents_src` mirrored into its
    .claude/agents. None when it cannot be made or a memory file sits in
    it or above it; the caller then runs the lane in the tree.

    `agents_src` is in ea-bot space, so it is listed and read with the
    resident's access: an owner process copying by path would follow a
    link there to a file only the owner can read."""
    try:
        import claude_cli as _cli
    except ModuleNotFoundError:
        from preprocessor import claude_cli as _cli
    d = lane_dir(lane)
    agents = d / ".claude" / "agents"
    if not (_rio.make_dir(d, 0o2775) and _rio.make_dir(agents, 0o2775)):
        return None
    for kind, name in _rio.list_dir(agents_src) or []:
        if kind != "f" or not name.endswith(".md"):
            continue
        text = _rio.read_text(Path(agents_src) / name)
        if text is not None and _rio.read_text(agents / name) != text:
            _rio.replace_text(agents / name, text, 0o664)
    if _cli.memory_on_path(d) or not os.access(d, os.X_OK):
        return None
    return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Unreadable(dict):
    """The empty rows that stand for a registry that is there but could not
    be read. _save never writes them over it."""


def _rows(data) -> dict:
    """The rows of a parsed registry that have the shape this module
    writes; the rest are left out."""
    if not isinstance(data, dict):
        return {}
    return {ts: row for ts, row in data.items()
            if isinstance(row, dict)
            and isinstance(row.get("session_id"), str)
            and SESSION_ID_RE.match(row["session_id"])
            and isinstance(row.get("meta", {}), dict)}


def _load() -> dict:
    """The registry's rows, read with the resident's access: {} when there
    is no registry or it holds no JSON object, _Unreadable() when it is
    there but could not be read."""
    data = _rio.read_bytes(REGISTRY)
    if data is None:
        return _Unreadable() if os.path.lexists(REGISTRY) else {}
    try:
        return _rows(json.loads(data.decode("utf-8")))
    except ValueError:
        return {}


def _mutate(fn) -> bool:
    """Read-modify-write of the registry inside one exclusive lock
    (resident_io.update_text, the lock ea_channels uses too), so two
    threads or processes writing at once never lose each other's rows.
    `fn(rows)` edits the dict of rows in place and returns True to write
    or False to keep the file. A registry that is there but is not a
    plain file (a link at its name) is refused by the update itself and
    never written over. Returns whether a write happened."""
    wrote = []

    def _rewrite(text: str):
        try:
            rows = _rows(json.loads(text)) if text.strip() else {}
        except ValueError:
            rows = {}
        if not fn(rows):
            return None
        wrote.append(True)
        return json.dumps(rows, indent=2)

    try:
        if not _rio.make_dir(REGISTRY.parent):
            return False
        if _rio.update_text(REGISTRY, _rewrite, 0o664):
            return bool(wrote)
        # The update reads the file as UTF-8 under its lock; bytes that do
        # not decode fail it. A registry like that is started over from
        # empty, as a read of it already is, and the update runs once
        # more. Nothing else is checked outside the lock: a half-written
        # JSON text is valid UTF-8 and waits for the lock like any other.
        raw = _rio.read_bytes(REGISTRY)
        if raw is None:
            return False
        try:
            raw.decode("utf-8")
            return False
        except UnicodeDecodeError:
            pass
        if not _rio.replace_text(REGISTRY, "{}", 0o664):
            return False
        return bool(_rio.update_text(REGISTRY, _rewrite, 0o664)) and bool(wrote)
    except Exception:
        return False


def _new_row() -> dict:
    return {"session_id": str(uuid.uuid4()),
            "created": _now().isoformat(),
            "last_used": _now().isoformat()}


def _live(entry: dict | None) -> bool:
    """Whether a row's session is younger than MAX_AGE_DAYS."""
    if not entry or not entry.get("session_id"):
        return False
    try:
        last = datetime.fromisoformat(entry.get("last_used") or entry.get("created"))
        return _now() - last < timedelta(days=MAX_AGE_DAYS)
    except Exception:
        return False


def lookup(thread_ts: str) -> tuple[str, bool, dict]:
    """Return (session_id, resumable, meta).

    resumable=True  -> a session exists for this thread and is younger
                       than MAX_AGE_DAYS: run with --resume <id>.
    resumable=False -> new session id minted (and registered): run with
                       --session-id <id> and the full initial prompt.
    meta: extra keys remembered for the session (e.g., use_mcp) so
    per-invocation flags stay consistent across resumes — a session that
    started with Gmail MCP tools must keep them on follow-ups, or the
    model reaches for tools that silently vanished.
    """
    entry = _load().get(thread_ts)
    if _live(entry):
        return entry["session_id"], True, entry.get("meta") or {}
    # Aged out (or unreadable): a fresh session, but the old id, title and
    # model ride along on the new row so `resume this conversation` can
    # re-adopt it. Aging out is a freshness default, not a deletion. The
    # mint happens inside the lock, and a row another thread minted in
    # the meantime is kept.
    result: dict = {}

    def mint(rows: dict) -> bool:
        current = rows.get(thread_ts)
        if _live(current):
            result.update(session_id=current["session_id"], resumable=True,
                          meta=current.get("meta") or {})
            return False
        old = dict(current or {})
        row = _new_row()
        if old.get("session_id"):
            meta = dict(old.get("meta") or {})
            keep = {k: v for k, v in meta.items()
                    if k in ("title", "channel", "model", "effort", "tier")}
            keep["expired_session_id"] = old["session_id"]
            keep["expired_last_used"] = old.get("last_used") or old.get("created") or ""
            row["meta"] = keep
        rows[thread_ts] = row
        result.update(session_id=row["session_id"], resumable=False, meta=row.get("meta") or {})
        return True

    if not _mutate(mint) and not result:
        # Unwritable registry: a session it does not record.
        return str(uuid.uuid4()), False, {}
    return result["session_id"], result["resumable"], result["meta"]


# A side session: a turn a card starts in a thread runs in a session of its
# own beside the thread's, so the turns of several cards run at the same
# time (the Replies card's Draft it, one session per ledger row). Its row
# is keyed by the thread's key, SIDE_SEP and a name; its posts go to the
# thread. A thread key never holds SIDE_SEP.
SIDE_SEP = "~"


def side_key(thread_ts: str, name: str) -> str:
    return f"{thread_ts}{SIDE_SEP}{name}"


def sides(thread_ts: str) -> list[str]:
    """The keys of the side sessions in a thread."""
    prefix = side_key(thread_ts, "")
    return [k for k in _load() if k.startswith(prefix)]


# A staging token: a session stages its button files (pending-event.json
# and the rest) under a token of its own, the first 8 hex characters of its
# id, so two conversations staging at the same time never write over each
# other's file.
STAGE_TOKEN_RE = re.compile(r"[0-9a-f]{8}")


def stage_token(session_id: str) -> str:
    """The session's staging token, or '' for an id that gives none."""
    token = (session_id or "").replace("-", "").lower()[:8]
    return token if STAGE_TOKEN_RE.fullmatch(token) else ""


def by_stage_token(token: str) -> tuple[str, dict]:
    """(thread key, meta) of the one session whose staging token is
    `token`; a side session gives its thread's key. ('', {}) when no
    session has it, or more than one does."""
    if not STAGE_TOKEN_RE.fullmatch(token or ""):
        return "", {}
    hits = [(key, row) for key, row in _load().items()
            if stage_token(row.get("session_id") or "") == token]
    if len(hits) != 1:
        return "", {}
    key, row = hits[0]
    return key.split(SIDE_SEP, 1)[0], dict(row.get("meta") or {})


def has(thread_ts: str) -> bool:
    """Read-only: does this thread already live on the per-thread session
    path (fresh entry exists)? Unlike lookup(), never mints a session;
    used by the resident-bridge router for sticky path assignment."""
    return _live(_load().get(thread_ts))


def mark_used(thread_ts: str) -> None:
    def fn(rows: dict) -> bool:
        if thread_ts not in rows:
            return False
        rows[thread_ts]["last_used"] = _now().isoformat()
        return True
    _mutate(fn)


def remember(thread_ts: str, **meta) -> None:
    """Merge metadata keys into the session entry (e.g., use_mcp=True)."""
    def fn(rows: dict) -> bool:
        if thread_ts not in rows:
            return False
        rows[thread_ts].setdefault("meta", {}).update(meta)
        return True
    _mutate(fn)


def invalidate(thread_ts: str) -> None:
    """Drop the mapping (resume failed / session corrupt). The next
    lookup() mints a fresh session."""
    _mutate(lambda rows: rows.pop(thread_ts, None) is not None)


def session_file(session_id: str) -> Path:
    """The session's transcript: in the tree's project folder, or in the
    folder of the lane directory it began in (session_cwd)."""
    d = session_cwd(session_id)
    return (_sessions_dir_for(d) if d else SESSIONS_DIR) / f"{session_id}.jsonl"


def adopt(thread_ts: str, session_id: str, *, source: str = "shell",
          model: str = "", require_file: bool = True, **meta) -> dict:
    """Bind `thread_ts` to an EXISTING Claude session. The thread's next
    message resumes it with `--resume <session_id>` and only the new
    text, exactly as a thread's own session would be resumed.

    Refuses a malformed id, and (by default) an id with no transcript
    on disk, so a typo cannot bind a thread to a session that resumes
    into nothing. A thread that already had a session is re-pointed;
    the old session file is left alone (the CLI owns it)."""
    session_id = (session_id or "").strip().lower()
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    if require_file and not session_file(session_id).exists():
        raise FileNotFoundError(
            f"no transcript for session {session_id} under {SESSIONS_DIR}")
    entry = {"session_id": session_id,
             "created": _now().isoformat(),
             "last_used": _now().isoformat(),
             "meta": {"adopted_from": source, **meta}}
    if model:
        entry["meta"]["model"] = model

    def fn(rows: dict) -> bool:
        prev = (rows.get(thread_ts) or {}).get("session_id", "")
        if prev and prev != session_id:
            entry["meta"]["replaced"] = prev
        rows[thread_ts] = entry
        return True

    _mutate(fn)
    return entry


def repoint_session(old: str, new: str) -> int:
    """Move every thread bound to session `old` onto session `new` (a
    project room's rotation). Each row keeps its meta and records the
    session it left. Returns how many rows moved."""
    old = (old or "").strip().lower()
    new = (new or "").strip().lower()
    if not (SESSION_ID_RE.match(old) and SESSION_ID_RE.match(new)) or old == new:
        return 0
    count = [0]

    def fn(rows: dict) -> bool:
        for entry in rows.values():
            if (entry.get("session_id") or "").lower() == old:
                entry["session_id"] = new
                entry["last_used"] = _now().isoformat()
                entry.setdefault("meta", {})["rotated_from"] = old
                count[0] += 1
        return count[0] > 0

    _mutate(fn)
    return count[0]


def set_session_effort(session_id: str, effort: str) -> int:
    """Pin `effort` on every thread bound to `session_id` (a room's
    threads carry the room's effort from the moment they were bound).
    Returns how many thread rows changed."""
    session_id = (session_id or "").strip().lower()
    if not SESSION_ID_RE.match(session_id):
        return 0
    count = [0]

    def fn(rows: dict) -> bool:
        for entry in rows.values():
            if (entry.get("session_id") or "").lower() == session_id:
                meta = entry.setdefault("meta", {})
                if meta.get("effort") != effort:
                    meta["effort"] = effort
                    count[0] += 1
        return count[0] > 0

    _mutate(fn)
    return count[0]


def describe(thread_ts: str) -> dict | None:
    """The registry row for a thread, or None."""
    return _load().get(thread_ts)


def find_thread(session_id: str) -> str:
    """The thread bound to `session_id`, or '' (for the shell's --resume
    by thread and for the ack line)."""
    session_id = (session_id or "").strip().lower()
    for ts, entry in _load().items():
        if (entry.get("session_id") or "").lower() == session_id:
            return ts
    return ""


def set_title(thread_ts: str, title: str) -> bool:
    """Name a conversation so it can be found again ("name this
    conversation X", or the first message's opening words by default)."""
    title = " ".join((title or "").split())[:120]
    if not title:
        return False

    def fn(rows: dict) -> bool:
        if thread_ts not in rows:
            return False
        rows[thread_ts].setdefault("meta", {})["title"] = title
        return True

    return _mutate(fn)


def title_from_message(text: str, limit: int = 72) -> str:
    """The default title: the message's first line, minus any bracketed
    routing prefix the bot prepended, cut at a word boundary."""
    body = re.sub(r"^\s*\[[^\]]{0,400}\]\s*", "", text or "", count=1)
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut or line[:limit]).rstrip(",;:") + "…"


def recent(limit: int = 12, days: int = 14) -> list[dict]:
    """The conversation index: registry rows, newest first, with their
    titles, channels and ages. Rows older than `days` are left out, and
    so are side sessions: each is part of its thread's conversation."""
    cutoff = _now() - timedelta(days=days)
    rows = []
    for ts, entry in _load().items():
        if SIDE_SEP in ts:
            continue
        try:
            last = datetime.fromisoformat(entry.get("last_used")
                                          or entry.get("created"))
        except Exception:
            continue
        if last < cutoff:
            continue
        meta = entry.get("meta") or {}
        rows.append({"thread_ts": ts, "session_id": entry.get("session_id", ""),
                     "title": meta.get("title", ""), "channel": meta.get("channel", ""),
                     "model": meta.get("model", ""),
                     "adopted_from": meta.get("adopted_from", ""),
                     "expired_session_id": meta.get("expired_session_id", ""),
                     "last_used": last})
    rows.sort(key=lambda r: r["last_used"], reverse=True)
    return rows[:limit]


def prune(max_age_days: int = 14) -> int:
    """Remove registry entries idle longer than max_age_days. Returns
    the number removed. (Claude's own session files are tiny and are
    managed by the CLI; this is registry hygiene only.)"""
    cutoff = _now() - timedelta(days=max_age_days)
    removed = [0]

    def fn(rows: dict) -> bool:
        stale = []
        for ts, entry in rows.items():
            try:
                last = datetime.fromisoformat(entry.get("last_used")
                                              or entry.get("created"))
                if last < cutoff:
                    stale.append(ts)
            except Exception:
                stale.append(ts)
        for ts in stale:
            del rows[ts]
        removed[0] = len(stale)
        return bool(stale)

    _mutate(fn)
    return removed[0]


# ── CLI ────────────────────────────────────────────────────────────────
# python3 slack_sessions.py adopt --thread <ts> --session <uuid> [--model M]
# python3 slack_sessions.py show --thread <ts> | --session <uuid>
# python3 slack_sessions.py list
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Slack thread <-> Claude session registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("adopt", help="bind a thread to an existing session")
    p.add_argument("--thread", required=True, help="Slack thread_ts")
    p.add_argument("--session", required=True, help="Claude session uuid")
    p.add_argument("--model", default="", help="model the thread should resume with")
    p.add_argument("--source", default="shell")
    p.add_argument("--no-check", action="store_true",
                   help="skip the transcript-exists check")
    p = sub.add_parser("show", help="print a thread's row, or the thread for a session")
    p.add_argument("--thread", default="")
    p.add_argument("--session", default="")
    sub.add_parser("list", help="every registered thread")
    a = ap.parse_args()
    if a.cmd == "adopt":
        try:
            entry = adopt(a.thread, a.session, source=a.source, model=a.model,
                          require_file=not a.no_check)
        except (ValueError, FileNotFoundError) as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        print(json.dumps({"ok": True, "thread": a.thread, **entry}))
        return 0
    if a.cmd == "show":
        if a.session:
            ts = find_thread(a.session)
            print(json.dumps({"session_id": a.session, "thread": ts,
                              "entry": describe(ts) if ts else None}))
            return 0 if ts else 1
        entry = describe(a.thread)
        print(json.dumps({"thread": a.thread, "entry": entry}))
        return 0 if entry else 1
    reg = _load()
    print(json.dumps([{"thread": ts, **e} for ts, e in reg.items()], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
