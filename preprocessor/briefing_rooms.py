#!/usr/bin/env python3
"""Project-room slices of a briefing.

The morning briefing and the afternoon update post whole to the EA
channel. An item that belongs to a project with a room (#ea-<project>) is
also posted in that room: one message per room per run, as a new thread,
so the user can act on it there with the room's specialist and the
project pack. A room with no item gets no post.

Which project an item belongs to, strongest first:
  1. its ledger row is linked to a project (project_store link-ledger);
  2. its thread was filed to a project (project_store file-message);
  3. the mail tagger's project call on its mail (mail_tagger.project_votes),
     when the strict matcher (project_store.match_projects) finds any
     trace of that project in its title and summary
     (project_store.checked_call). A call the words do not echo, or a
     call of no project, places nothing;
  4. with no call (Gmail, a row from chat, the tagger off), the strict
     matcher alone, at MATCH_FLOOR or more. Projects tied at the top
     score all get it, at most MAX_PROJECTS, unless one of them is there
     on its name words alone (project_store.name_only): a tie that name
     words decide places nothing.
A row the owner marked as no project's (project_store move-ledger
--project none, which marks it loose) places nothing past stage 1. An
item with no project stays in the EA post only.

A post opens with its name, the words that title its thread wherever a
list shows it (the web chat's Recent once the owner answers, the room's
own list of threads, Slack's channel): the first item's title in plain
words, with "and N more" for the rest (conversation_name; an item kept
from hosted models is keep_gate's fixed words). Its last line
says which briefing it comes from. On the web chat a post the owner has
not answered stays out of Recent (web_frontend.is_conversation).

A room gets a post only when the registry binds it to the project, it is
not archived, and the owner's own record (owner_rooms) names it. The
post goes through chat_notify.post with the key `room:<channel id>`:
quiet hours hold it like the EA post, the owner record is checked again,
and the configured chat frontend posts it (a new thread on the web chat,
a new message on Slack). A room gets one line per thread: two items on one
thread (a staged draft and its do-list line, two rows of one conversation)
show once, under the first section in SECTIONS order (one_per_thread).

Switch: `"briefing_rooms"` in remote-llm.json. On the web chat the slices
are off unless it says `true`: Home lists every item of the post, and a
room keeps its conversations (docs/webchat.md, Home). On Slack, with no
Home, they are on unless it says `false`.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keep_gate  # noqa: E402

MATCH_FLOOR = 3
MAX_PROJECTS = 2
ALSO_OPEN = 5
MAX_ITEMS = 12
# The longest name a post opens with, "and N more" included: the
# distinctive words come first, since a phone's list shows about 25.
NAME_MAX = 60

# The section an item came from, as the room post names it.
SECTIONS = {
    "top": ":dart: Do today",
    "needs": ":question: Needs you",
    "draft": ":envelope: Draft ready (Send is in {place})",
    "escalated": ":arrow_up: Sent to Claude",
    "flex": ":leaves: Flex",
    "due": ":alarm_clock: Due",
    "since": ":new: Since this morning",
    "pending": ":hourglass: Pending",
    "letters": ":memo: Letter",
}

def _card_place() -> str:
    """Where the cards wait, as the chat front end names it
    (chat_notify.card_place): "your DM" on Slack, "Action requests" on the
    web chat."""
    try:
        import chat_notify
    except ImportError:
        from preprocessor import chat_notify
    return chat_notify.card_place()



def enabled(cfg: dict | None = None, *, frontend: str | None = None) -> bool:
    """Whether the briefings post room slices: the switch when the file
    sets it (true or false), else off on the web chat and on elsewhere
    (`frontend`, the configured chat frontend unless given; off when the
    chat settings cannot be read, as no post could go out then)."""
    import remote_llm
    cfg = cfg if cfg is not None else remote_llm.config()
    if isinstance(cfg.get("briefing_rooms"), bool):
        return cfg["briefing_rooms"]
    if frontend is None:
        try:
            try:
                import chat_notify
            except ImportError:
                from preprocessor import chat_notify
            frontend = chat_notify.frontend_name()
        except Exception:
            return False
    return frontend != "web"


# ── which project ─────────────────────────────────────────────────────

def index(conn, votes: dict | None = None) -> dict:
    """The store's explicit links: {"rows": {ledger id: [project]},
    "threads": {conversation id: [project]}}, active projects only; with
    "sources" {ledger id: source message id}, the mail tagger's "votes"
    (mail_tagger.project_votes, read here unless given) and "loose", the
    rows the owner said belong to no project."""
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
    sources: dict[str, str] = {}
    try:
        for r in conn.execute("SELECT id, source_message_id FROM ledger "
                              "WHERE COALESCE(source_message_id, '') != ''"):
            sources[r["id"]] = r["source_message_id"]
    except sqlite3.OperationalError:
        pass
    loose: set[str] = set()
    try:
        loose = {r["ledger_id"] for r in conn.execute("SELECT ledger_id FROM loose_ledger")}
    except sqlite3.OperationalError:
        pass
    if votes is None:
        import mail_tagger
        votes = mail_tagger.project_votes(live)
    return {"rows": rows, "threads": threads, "sources": sources, "votes": votes,
            "loose": loose}


def projects_for(item: dict, idx: dict, conn, *, matcher=None) -> list[str]:
    """The projects an item belongs to, strongest evidence first; [] for
    none. `item` carries ledger_id, conversation_id, title, summary."""
    return placement(item, idx, conn, matcher=matcher)["projects"]


def placement(item: dict, idx: dict, conn, *, matcher=None) -> dict:
    """Why an item goes where it goes: {"projects", "by", "hits"}. `by` is
    link, thread, loose, tagger, words or none; `hits` is the word match
    when one was made (project_store.match_projects)."""
    lid = item.get("ledger_id") or ""
    if lid and idx["rows"].get(lid):
        return {"projects": idx["rows"][lid][:MAX_PROJECTS], "by": "link", "hits": []}
    if item.get("kind") in ("letter", "case"):
        # A work case (case_surface) goes where its parts' rows are
        # linked, and nowhere on words: a person's name is no project.
        linked = [p for i in item.get("letter_rows") or [] for p in idx["rows"].get(i) or []]
        return {"projects": list(dict.fromkeys(linked))[:MAX_PROJECTS],
                "by": "link" if linked else "none", "hits": []}
    if lid and lid in (idx.get("loose") or ()):
        return {"projects": [], "by": "loose", "hits": []}
    conv = item.get("conversation_id") or ""
    if conv and idx["threads"].get(conv):
        return {"projects": idx["threads"][conv][:MAX_PROJECTS], "by": "thread", "hits": []}
    import mail_tagger
    import project_store
    matcher = matcher or project_store.match_projects
    hits = matcher(f"{item.get('title') or ''} {(item.get('summary') or '')[:300]}", conn)
    call = mail_tagger.call_for(idx.get("votes") or {},
                                (idx.get("sources") or {}).get(lid, ""), conv)
    checked = project_store.checked_call(call, hits)
    if checked is not None:
        return {"projects": checked, "by": "tagger", "call": call, "hits": hits}
    if not hits or hits[0]["score"] < MATCH_FLOOR:
        return {"projects": [], "by": "none", "hits": hits}
    best = hits[0]["score"]
    top = [h for h in hits if h["score"] == best]
    if len(top) > 1 and any(project_store.name_only(h) for h in top):
        return {"projects": [], "by": "none", "tie": [h["id"] for h in top], "hits": hits}
    return {"projects": [h["id"] for h in top][:MAX_PROJECTS], "by": "words", "hits": hits}


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
    an entry goes to every room its projects have. An entry that carries
    `projects` (the pre-send review moved it) goes to those alone."""
    idx = idx if idx is not None else index(conn)
    out: dict[str, list[dict]] = {}
    for e in entries:
        pids = e["projects"] if "projects" in e else projects_for(e, idx, conn, matcher=matcher)
        for pid in pids:
            if pid in targets:
                out.setdefault(pid, []).append(e)
    return {pid: one_per_thread(items)[:MAX_ITEMS] for pid, items in out.items()}


def _thread_keys(e: dict) -> set[str]:
    keys = {f"row:{i}" for i in [e.get("ledger_id")] + list(e.get("letter_rows") or ()) if i}
    return keys | ({f"conv:{e['conversation_id']}"} if e.get("conversation_id") else set())


def one_per_thread(entries: list[dict]) -> list[dict]:
    """The entries in SECTIONS order, each thread once: an entry whose
    ledger row or conversation an earlier one names is left out, so a
    thread with a staged draft and a do-list line is one line."""
    seen: set[str] = set()
    out = []
    for e in _in_order(entries):
        keys = _thread_keys(e)
        if keys & seen:
            continue
        seen |= keys
        out.append(e)
    return out


def also_open(pid: str, open_rows: list[dict], shown: set[str], conn, *,
              matcher=None, idx: dict | None = None, ids: list | None = None) -> list[str]:
    """Descriptions of the project's other open rows, the post's own
    items left out, at most ALSO_OPEN; their ids go into `ids`."""
    idx = idx if idx is not None else index(conn)
    out = []
    import case_surface
    for o in case_surface.collapse_rows(open_rows):
        lid = o.get("id") or ""
        if not lid or lid in shown:
            continue
        item = {"ledger_id": lid, "conversation_id": o.get("conversation_id") or "",
                "title": o.get("description") or "", "summary": o.get("notes") or ""}
        if pid in projects_for(item, idx, conn, matcher=matcher):
            # A row kept from hosted models is named in code's fixed words
            # (keep_gate): room sessions read this post.
            out.append(keep_gate.KEPT_NAME if keep_gate.kept(keep_gate.label_level(o))
                       else o.get("description") or lid)
            if ids is not None:
                ids.append(lid)
        if len(out) >= ALSO_OPEN:
            break
    return out


def _cut(text: str, cap: int, clean) -> str:
    """`clean`, cut at the last whole word that fits."""
    t = clean(text, 10_000)
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:(") + "..."


# What opens a title but names no topic: a reply or forward mark, a mail
# system's tag ("[EXT]"), the ledger's own labels.
_TITLE_NOISE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*:|\[[^\]]{1,24}\]|(?:due \d{4}-\d{2}-\d{2}|task|"
    r"needs response|scheduling request|follow[- ]up|deadline|reminder)\s*:)\s*", re.I)
# Small words a cut title must not end on.
_TAIL_WORDS = frozenset("a an and as at by for from in of on or the to with".split())


def plain_title(title: str, clean) -> str:
    """An item's title in plain words: no reply marks, tags, ledger labels,
    markup or emoji codes."""
    t = re.sub(r"(?<!\w):[a-z][a-z0-9_+-]*:(?!\w)", " ", clean(title, 10_000))
    t = re.sub(r"(?<!\w)[_~]+|[_~]+(?!\w)", "", t)
    while True:
        bare = _TITLE_NOISE.sub("", t, count=1)
        if bare == t:
            break
        t = bare
    return re.sub(r"\s+([,;:])", r"\1", " ".join(t.split())).strip(" ,;:-")


def _fit(text: str, cap: int) -> str:
    """`text` cut at the last whole word that fits in `cap` characters,
    with no small word left at its end, and "…" where it is cut."""
    if len(text) <= cap:
        return text
    cut = text[:cap - 1]
    if text[cap - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]  # not the word the cut went through
    words = cut.split()
    while len(words) > 1 and words[-1].lower().strip(",;:-") in _TAIL_WORDS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-(") + "…"


def _in_order(entries: list[dict]) -> list[dict]:
    """The entries as the post shows them: by section, in SECTIONS order."""
    return [e for sec in SECTIONS for e in entries if e.get("section") == sec]


def conversation_name(entries: list[dict], clean) -> str:
    """The name a slice's post opens with: the first item's title in plain
    words, "and N more" when the post has more items, at most NAME_MAX
    characters, cut at a word. A first item kept from hosted models
    (keep_gate, its `keep_level`) is named in code's fixed words, never
    by its subject or sender: room sessions and lists read the name."""
    shown = _in_order(entries)
    if not shown:
        return ""
    if keep_gate.kept(shown[0].get("keep_level")):
        first = keep_gate.KEPT_NAME
    else:
        first = plain_title(shown[0].get("title") or "", clean) \
            or plain_title(shown[0].get("line") or "", clean)
    more = f" and {len(shown) - 1} more" if len(shown) > 1 else ""
    return _fit(first or "Items from the briefing", NAME_MAX - len(more)) + more


def render(heading: str, entries: list[dict], others: list[str], clean) -> str:
    """One room's post: its name, its items under their sections, the
    project's other open rows, and a line that names the briefing it
    comes from (`heading`, "Morning briefing, Mon Oct 12")."""
    lines = [f"*{conversation_name(entries, clean)}*"]
    for e in _in_order(entries):
        sec = e["section"]
        head = SECTIONS[sec].format(place=_card_place()) if sec == "draft" else \
            (e.get("label") or SECTIONS[sec]) if sec == "letters" else SECTIONS[sec]
        lines.append(f"{head}: {e['line']}")
    if others:
        lines += ["", f"Also open here ({len(others)}): "
                  + "; ".join(_cut(o, 90, clean) for o in others)]
    source = heading[:1].lower() + heading[1:]
    lines += ["", f"_From the {source}. Reply in this thread to act on it here._"]
    return "\n".join(lines)


def plan_posts(entries: list[dict], *, heading: str, open_rows: list[dict], conn,
               targets: dict[str, dict], clean, matcher=None,
               votes: dict | None = None, skip: dict | None = None) -> list[dict]:
    """[{project, channel, room, text, refs, also, also_text}], one per
    room with items; `also` is the ids of its "Also open here" rows.
    `skip` ({project: [ledger id]}, "*" for every room) keeps rows off
    a room's list."""
    idx = index(conn, votes)
    posts = []
    for pid, items in slices(entries, targets, conn, matcher=matcher, idx=idx).items():
        shown = {e["ledger_id"] for e in items if e.get("ledger_id")} | \
            {i for e in items for i in e.get("letter_rows") or ()} | \
            set((skip or {}).get(pid) or ()) | set((skip or {}).get("*") or ())
        ids: list[str] = []
        others = also_open(pid, open_rows, shown, conn, matcher=matcher, idx=idx, ids=ids)
        posts.append({"project": pid, "channel": targets[pid]["channel"],
                      "room": targets[pid]["room"],
                      "text": render(heading, items, others, clean),
                      "refs": [e["ref"] for e in items], "also": ids, "also_text": others})
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
