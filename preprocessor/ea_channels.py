#!/usr/bin/env python3
"""Project channels: which private rooms the EA answers in (Slack
channels, Zulip streams or web chat rooms), and which project or session
each one is about.

The bot answers in the channels listed here (ea-chat-bot._channel_scope
reads this file). This module owns the file, so a channel can be
registered by the bot the moment it creates one ("open a channel for
<project>" in chat) or by hand. The key is the room id the chat frontend
gives (CHANNEL_ID_RE).

Two kinds of channel, told apart by NAME so they read the same in chat,
on the web board and in App Home:

  #ea-<project>   kind "project": scoped to one project; every thread in
                  it is its own conversation with the project pack in
                  hand. The board card and the App Home card carry the
                  same #ea- badge, linking to the channel.
  #tmp-<id8>      kind "session": mirrors ONE open Claude session (an
                  ea-bot shell conversation, or a Slack thread promoted
                  to a room). Every message in the channel, top level or
                  threaded, resumes that one session. Temporary by name:
                  archive it when the conversation is over.

Registry: ~/.ea/config/ea-channels.json
  {channel_id: {"kind": "project"|"session", "project": project_id,
                "session_id": uuid, "name": room_name, "url": deep link,
                "model": str, "effort": str, "created": iso,
                "created_by": "slack|manual"}}

The bot only ever SCOPES a channel that is private and answers only the
owner in it; that check stays in the bot, next to the token. This file
is judgment-free bookkeeping.

Writes are in place under the file's own lock (the config directory is
not group-writable, so a temp-and-rename would fail for the bot's user; the
file itself is mode 664, group ea). The registry is in the resident's space
and both users write it, so reads and writes go through resident_io: the
bot (the owner) changes it with the resident's access, through
resident_io.update_text, which locks the same file a resident writer locks.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import resident_io as _rio

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
REGISTRY = EA_DIR / "config" / "ea-channels.json"
NAME_PREFIX = "ea-"
SESSION_PREFIX = "tmp-"
MAX_NAME = 80  # Slack's limit; a frontend with a shorter one passes max_len
KINDS = ("project", "session")
# A room id as the chat frontend that made it writes it: a Slack channel id
# (C… or G…), a Zulip stream as stream:<id>, or a web chat room as r<n>
# (WebFrontend.create_private_room numbers its rooms from 1). The web chat's
# own fixed channels (main, dm) and its reserved ids (needs) are never rooms.
CHANNEL_ID_RE = re.compile(r"^(?:[CG][A-Z0-9]{6,}|stream:\d+|r[1-9]\d*)$")
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path | None = None) -> dict:
    path = path or REGISTRY
    try:
        raw = json.loads(_rio.read_text(path) or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


def _mutate(fn, path: Path | None = None) -> None:
    """Read-modify-write of the registry INSIDE one exclusive lock, so
    concurrent registrations never overwrite each other. `fn(rows)` edits
    the dict of channel rows in place; `_doc`/`_example` keys ride along
    untouched."""
    path = path or REGISTRY

    def _rewrite(text: str) -> str:
        try:
            current = json.loads(text or "{}")
        except json.JSONDecodeError:
            current = {}
        if not isinstance(current, dict):
            current = {}
        keep = {k: v for k, v in current.items() if k.startswith("_")}
        rows = {k: v for k, v in current.items()
                if not k.startswith("_") and isinstance(v, dict)}
        fn(rows)
        return json.dumps({**keep, **rows}, indent=2, ensure_ascii=False) + "\n"

    if not _rio.update_text(path, _rewrite, 0o664):
        raise OSError(f"could not update the channel registry {path}")


def slug(text: str, max_len: int = MAX_NAME) -> str:
    """A room name for a project: lowercase, dashes, ea- prefix, cut to
    max_len (the frontend's room-name limit)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s or s == NAME_PREFIX.rstrip("-"):
        return NAME_PREFIX + "channel"
    if not s.startswith(NAME_PREFIX):
        s = NAME_PREFIX + s
    return s[:max_len].rstrip("-")


def session_slug(session_id: str, max_len: int = MAX_NAME) -> str:
    """#tmp-<first 8 hex of the session id>, cut to max_len."""
    sid = re.sub(r"[^0-9a-f]", "", (session_id or "").lower())
    if len(sid) < 8:
        raise ValueError(f"not a session id: {session_id!r}")
    return (SESSION_PREFIX + sid[:8])[:max_len]


def register(channel_id: str, project: str = "", name: str = "", *,
             kind: str = "", session_id: str = "", url: str = "",
             model: str = "", effort: str = "", created_by: str = "manual",
             path: Path | None = None) -> dict:
    """Bind a channel to a project (kind "project") or to one Claude
    session (kind "session"). Re-registering re-points it."""
    channel_id = (channel_id or "").strip()
    project = (project or "").strip()
    session_id = (session_id or "").strip().lower()
    if not CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"not a chat channel id: {channel_id!r}")
    kind = kind or ("session" if session_id and not project else "project")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "project" and not project:
        raise ValueError("project id required")
    if kind == "session" and not SESSION_ID_RE.match(session_id):
        raise ValueError(f"session channel needs a session uuid, got {session_id!r}")
    entry = {"kind": kind, "name": name or "", "created": _now(),
             "created_by": created_by}
    if project:
        entry["project"] = project
    if session_id:
        entry["session_id"] = session_id
    if url:
        entry["url"] = url
    if model:
        entry["model"] = model
    if effort:
        entry["effort"] = effort
    def _apply(rows: dict) -> None:
        prev = rows.get(channel_id)
        if prev and prev.get("project") and prev.get("project") != project:
            entry["replaced_project"] = prev.get("project")
        rows[channel_id] = entry

    _mutate(_apply, path)
    return entry


def set_session(channel_id: str, session_id: str, model: str = "",
                effort: str = "", path: Path | None = None) -> bool:
    """Give a project room its own persistent session (the room's
    specialist), optionally pinned to a model and an effort level. Every
    thread in the room is bound to it. False when the channel is not
    registered or the id is malformed."""
    session_id = (session_id or "").strip().lower()
    if not SESSION_ID_RE.match(session_id):
        return False
    done = {"ok": False}

    def _apply(rows: dict) -> None:
        if channel_id in rows:
            rows[channel_id]["session_id"] = session_id
            if model:
                rows[channel_id]["model"] = model
            if effort:
                rows[channel_id]["effort"] = effort
            done["ok"] = True

    _mutate(_apply, path)
    return done["ok"]


def set_effort(channel_id: str, effort: str, path: Path | None = None) -> bool:
    """Pin a room's specialist to an effort level. The threads already
    bound to its session keep their own copy of the effort, so the CLI
    updates those through slack_sessions.set_session_effort. False when
    the channel is not registered."""
    done = {"ok": False}

    def _apply(rows: dict) -> None:
        if channel_id in rows:
            rows[channel_id]["effort"] = effort
            done["ok"] = True

    _mutate(_apply, path)
    return done["ok"]


def resolve(ref: str, path: Path | None = None) -> str:
    """A channel id from an id, a room name (with or without '#') or a
    project id. '' when nothing matches or a name is ambiguous."""
    ref = (ref or "").strip().lstrip("#")
    rows = load(path)
    if ref in rows:
        return ref
    hits = [k for k, v in rows.items()
            if ref and (v.get("name") == ref or v.get("project") == ref)]
    return hits[0] if len(hits) == 1 else ""


def set_archived(channel_id: str, archived: bool, path: Path | None = None,
                 by: str = "") -> bool:
    """Record that a room is archived (or open again) and who archived it:
    "bot" when the hygiene pass did because the project went dormant or
    closed, "user" when the owner did it in Slack. A room the owner
    archived is not reopened by the bot. The registration stays either
    way, so a revived project gets the same room back."""
    done = {"ok": False}

    def _apply(rows: dict) -> None:
        if channel_id in rows:
            rows[channel_id]["archived"] = bool(archived)
            if archived and by:
                rows[channel_id]["archived_by"] = by
            elif not archived:
                rows[channel_id].pop("archived_by", None)
            done["ok"] = True

    _mutate(_apply, path)
    return done["ok"]


def rename(channel_id: str, name: str, path: Path | None = None) -> bool:
    """The user renamed a registered room in Slack: keep the badge in
    step. False when the channel is not registered."""
    name = (name or "").strip().lstrip("#")
    done = {"ok": False}

    def _apply(rows: dict) -> None:
        if channel_id in rows and name:
            rows[channel_id]["name"] = name
            done["ok"] = True

    _mutate(_apply, path)
    return done["ok"]


def unregister(channel_id: str, path: Path | None = None) -> bool:
    found = {"ok": False}

    def _apply(rows: dict) -> None:
        found["ok"] = rows.pop(channel_id, None) is not None

    _mutate(_apply, path)
    return found["ok"]


def for_project(project: str, path: Path | None = None) -> list[str]:
    """Channel ids bound to a project (usually zero or one)."""
    return [cid for cid, e in load(path).items()
            if e.get("project") == project and e.get("kind", "project") == "project"]


def for_session(session_id: str, path: Path | None = None) -> list[str]:
    """Channel ids mirroring a session (usually zero or one)."""
    sid = (session_id or "").lower()
    return [cid for cid, e in load(path).items()
            if (e.get("session_id") or "").lower() == sid]


def scope_of(channel_id: str, path: Path | None = None) -> dict | None:
    e = load(path).get(channel_id)
    if not e:
        return None
    out = dict(e)
    out.setdefault("kind", "project" if e.get("project") else "session")
    return out


def by_project(path: Path | None = None) -> dict:
    """project_id -> {"id", "name", "url"} for the board and App Home
    badges (one channel per project; the newest wins)."""
    out: dict = {}
    for cid, e in load(path).items():
        if e.get("kind", "project") != "project" or not e.get("project"):
            continue
        out[e["project"]] = {"id": cid, "name": e.get("name") or cid,
                             "url": e.get("url") or "",
                             "archived": bool(e.get("archived")),
                             "archived_by": e.get("archived_by") or ""}
    return out


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="project channel registry", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register", allow_abbrev=False)
    p.add_argument("--channel", required=True)
    p.add_argument("--project", default="")
    p.add_argument("--session", default="", help="mirror this session (kind session)")
    p.add_argument("--name", default="")
    p.add_argument("--url", default="")
    p = sub.add_parser("unregister", allow_abbrev=False)
    p.add_argument("--channel", required=True)
    sub.add_parser("list", allow_abbrev=False)
    p = sub.add_parser("slug", allow_abbrev=False)
    p.add_argument("text")
    p = sub.add_parser("set-effort", help="pin a project room's specialist effort", allow_abbrev=False)
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--channel", help="channel id, room name or project id")
    who.add_argument("--all-projects", action="store_true",
                     help="every registered project room")
    p.add_argument("--effort", required=True,
                   choices=("low", "medium", "high", "xhigh", "max"))
    a = ap.parse_args()
    if a.cmd == "set-effort":
        if a.all_projects:
            ids = [k for k, v in load().items() if v.get("kind") == "project"]
        else:
            cid = resolve(a.channel)
            if not cid:
                print(json.dumps({"ok": False, "error": f"no single room matches {a.channel!r}"}))
                return 1
            ids = [cid]
        try:
            import slack_sessions as _sessions
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import slack_sessions as _sessions
        out = []
        for cid in ids:
            ok = set_effort(cid, a.effort)
            sid = (load().get(cid) or {}).get("session_id") or ""
            threads = _sessions.set_session_effort(sid, a.effort) if (ok and sid) else 0
            out.append({"channel": cid, "ok": ok, "threads": threads})
        print(json.dumps({"ok": all(r["ok"] for r in out), "effort": a.effort, "rooms": out}))
        return 0 if all(r["ok"] for r in out) else 1
    if a.cmd == "register":
        try:
            print(json.dumps({"ok": True, "channel": a.channel,
                              **register(a.channel, a.project, a.name,
                                         session_id=a.session, url=a.url)}))
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        return 0
    if a.cmd == "unregister":
        print(json.dumps({"ok": unregister(a.channel)}))
        return 0
    if a.cmd == "slug":
        print(slug(a.text))
        return 0
    print(json.dumps([{"channel": k, **v} for k, v in load().items()], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
