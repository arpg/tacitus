#!/usr/bin/env python3
"""Project-room slices of a briefing.

The morning briefing and the afternoon update post whole to the EA
channel. An item that belongs to a project with a room (#ea-<project>) is
also posted in that room: one message per room per run, as a new thread
(a new topic on Zulip), so the user can act on it there with the room's specialist and the
project pack. A room with no item gets no post.

Which project an item belongs to, strongest first:
  1. its ledger row is linked to a project (project_store link-ledger);
  2. its thread was filed to a project (project_store file-message);
  3. the strict matcher (project_store.match_projects) on its title and
     summary, at MATCH_FLOOR or more. Projects tied at the top score all
     get it, at most MAX_PROJECTS.
An item with no project stays in the EA post only.

A room gets a post only when the registry binds it to the project, it is
not archived, and the owner's own record (owner_rooms) names it. The
post goes through chat_notify.post with the key `room:<channel id>`:
quiet hours hold it like the EA post, the owner record is checked again,
and the configured chat frontend posts it (a new topic on Zulip, a new
message on Slack).

Switch: `"briefing_rooms": false` in remote-llm.json turns the slices off.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MATCH_FLOOR = 3
MAX_PROJECTS = 2
ALSO_OPEN = 5
MAX_ITEMS = 12

# The section an item came from, as the room post names it.
SECTIONS = {
    "top": ":dart: Do today",
    "needs": ":question: Needs you",
    "draft": ":envelope: Draft ready (Send is in your DM)",
    "escalated": ":arrow_up: Sent to Claude",
    "flex": ":leaves: Flex",
    "due": ":alarm_clock: Due",
    "since": ":new: Since this morning",
    "pending": ":hourglass: Pending",
}


def enabled(cfg: dict | None = None) -> bool:
    import remote_llm
    cfg = cfg if cfg is not None else remote_llm.config()
    return cfg.get("briefing_rooms") is not False


# ── which project ─────────────────────────────────────────────────────

def index(conn) -> dict:
    """The store's explicit links: {"rows": {ledger id: [project]},
    "threads": {conversation id: [project]}}, active projects only."""
    live = {r["id"] for r in conn.execute("SELECT id, status FROM projects")
            if r["status"] != "closed"}
    rows: dict[str, list[str]] = {}
    for r in conn.execute("SELECT project_id, ledger_id FROM project_ledger"):
        if r["project_id"] in live:
            rows.setdefault(r["ledger_id"], []).append(r["project_id"])
    threads: dict[str, list[str]] = {}
    for r in conn.execute("SELECT project_id, conversation_id FROM project_messages "
                          "WHERE conversation_id != ''"):
        if r["project_id"] in live and r["project_id"] not in threads.get(r["conversation_id"], []):
            threads.setdefault(r["conversation_id"], []).append(r["project_id"])
    return {"rows": rows, "threads": threads}


def projects_for(item: dict, idx: dict, conn, *, matcher=None) -> list[str]:
    """The projects an item belongs to, strongest evidence first; [] for
    none. `item` carries ledger_id, conversation_id, title, summary."""
    lid = item.get("ledger_id") or ""
    if lid and idx["rows"].get(lid):
        return idx["rows"][lid][:MAX_PROJECTS]
    conv = item.get("conversation_id") or ""
    if conv and idx["threads"].get(conv):
        return idx["threads"][conv][:MAX_PROJECTS]
    if matcher is None:
        import project_store
        matcher = project_store.match_projects
    hits = matcher(f"{item.get('title') or ''} {(item.get('summary') or '')[:300]}", conn)
    if not hits or hits[0]["score"] < MATCH_FLOOR:
        return []
    best = hits[0]["score"]
    return [h["id"] for h in hits if h["score"] == best][:MAX_PROJECTS]


# ── which rooms ───────────────────────────────────────────────────────

def rooms(*, registry: dict | None = None, owner: dict | None = None) -> dict[str, dict]:
    """{project: {"channel", "room"}} for the rooms a post may go to:
    bound to the project in the registry, not archived, and named in the
    owner's record under the same name."""
    import ea_channels
    import owner_rooms
    registry = registry if registry is not None else ea_channels.load()
    if owner is None:
        try:
            owner = owner_rooms.load() or {}
        except Exception as e:
            print(f"briefing_rooms: owner room record unreadable ({str(e)[:120]}); no room posts",
                  file=sys.stderr)
            owner = {}
    out: dict[str, dict] = {}
    for cid, e in registry.items():
        pid = e.get("project") or ""
        if e.get("kind", "project") != "project" or not pid or e.get("archived"):
            continue
        row = owner.get(cid)
        if not row or row.get("archived_by_bot") or row.get("name") != e.get("name"):
            continue
        out[pid] = {"channel": cid, "room": row["name"]}
    return out


# ── the slices ────────────────────────────────────────────────────────

def slices(entries: list[dict], targets: dict[str, dict], conn, *, matcher=None,
           idx: dict | None = None) -> dict[str, list[dict]]:
    """{project: [entry]} for the projects that have a room. Each entry
    is {ref, section, line, ledger_id, conversation_id, title, summary};
    an entry goes to every room its projects have."""
    idx = idx if idx is not None else index(conn)
    out: dict[str, list[dict]] = {}
    for e in entries:
        for pid in projects_for(e, idx, conn, matcher=matcher):
            if pid in targets and len(out.get(pid, [])) < MAX_ITEMS:
                out.setdefault(pid, []).append(e)
    return out


def also_open(pid: str, open_rows: list[dict], shown: set[str], conn, *,
              matcher=None, idx: dict | None = None) -> list[str]:
    """Descriptions of the project's other open rows, the post's own
    items left out, at most ALSO_OPEN."""
    idx = idx if idx is not None else index(conn)
    out = []
    for o in open_rows:
        lid = o.get("id") or ""
        if not lid or lid in shown:
            continue
        item = {"ledger_id": lid, "conversation_id": o.get("conversation_id") or "",
                "title": o.get("description") or "", "summary": o.get("notes") or ""}
        if pid in projects_for(item, idx, conn, matcher=matcher):
            out.append(o.get("description") or lid)
        if len(out) >= ALSO_OPEN:
            break
    return out


def _cut(text: str, cap: int, clean) -> str:
    """`clean`, cut at the last whole word that fits."""
    t = clean(text, 10_000)
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:(") + "..."


def render(heading: str, project_name: str, entries: list[dict], others: list[str],
           clean) -> str:
    lines = [f"*{heading}: {clean(project_name, 80)}*"]
    for sec in SECTIONS:
        for e in (x for x in entries if x["section"] == sec):
            lines.append(f"{SECTIONS[sec]}: {e['line']}")
    if others:
        lines += ["", f"Also open here ({len(others)}): "
                  + "; ".join(_cut(o, 90, clean) for o in others)]
    lines += ["", "_From the briefing in the EA channel. Reply in this topic to act on it here._"]
    return "\n".join(lines)


def plan_posts(entries: list[dict], *, heading: str, open_rows: list[dict], conn,
               targets: dict[str, dict], clean, matcher=None) -> list[dict]:
    """[{project, channel, room, text, refs}], one per room with items."""
    idx = index(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}
    posts = []
    for pid, items in slices(entries, targets, conn, matcher=matcher, idx=idx).items():
        shown = {e["ledger_id"] for e in items if e.get("ledger_id")}
        others = also_open(pid, open_rows, shown, conn, matcher=matcher, idx=idx)
        posts.append({"project": pid, "channel": targets[pid]["channel"],
                      "room": targets[pid]["room"],
                      "text": render(heading, names.get(pid) or pid, items, others, clean),
                      "refs": [e["ref"] for e in items]})
    posts.sort(key=lambda p: p["room"])
    return posts


def pointer_line(posts: list[dict], *, mention=None) -> list[str]:
    """The EA post's line that names the rooms that got a slice, each as a
    link in the frontend's own syntax (chat_notify.room_mention)."""
    if not posts:
        return []
    if mention is None:
        import chat_notify
        mention = chat_notify.room_mention
    return ["", ":house: Also in project rooms: " + ", ".join(
        f"{mention(p['channel'])} ({len(p['refs'])})" for p in posts)]


def send(posts: list[dict], *, poster=None) -> list[dict]:
    """Post each slice; returns [{room, ok}]."""
    import chat_notify
    poster = poster or chat_notify.post
    out = []
    for p in posts:
        try:
            ok = bool(poster(chat_notify.room_key(p["channel"]), p["text"]))
        except Exception as e:
            print(f"briefing_rooms: post to {p['room']} failed: {str(e)[:160]}", file=sys.stderr)
            ok = False
        out.append({"room": p["room"], "ok": ok})
    return out
