#!/usr/bin/env python3
"""One-time repair: link the open ledger rows that a project room's turn
created before a room's turn linked the rows it adds
(project_store.room_link). Until then such a row had no project link,
and the briefing placed it by its words, sometimes in another room.

Evidence that a row came from a room, strongest first:
  1. its text names a chat thread (`t1234`, a Slack ts) whose session
     belongs to a project room (the session and room registries);
  2. it was created during one turn in a project room, and no turn in
     another room or channel ran at that time (the chat bot's log, in the
     owner's log dir). Only a row a model named (a kebab-case id, no
     plugin source) counts: the mail intake writes rows at any time, with
     ids of its own (`priority-<hash>=`). Here the row's words can veto the
     room: words that name only other projects leave it as it is.
Rows already linked, marked as no project's, or closed are left alone.

A dry run by default: it prints each row it would link, and why, and
writes nothing (the ledger opens read-only). --apply links them and
records each link as a `placed` ledger event. A second run finds
nothing more to link.

Run as the owner:
    python3 scripts/repair_room_links.py [--apply] [--bot-log PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_channels  # noqa: E402
import ledger  # noqa: E402
import log_paths  # noqa: E402
import project_store  # noqa: E402
import slack_sessions  # noqa: E402

# The id of a row a model named in a chat turn; the mail intake's ids
# carry a hash (`priority-<hash>=`, `deadline-<hash>=`).
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# A thread key in a row's text: a web chat thread (t1234) or a Slack ts.
THREAD_RE = re.compile(r"\b(t\d{3,8}|\d{10}\.\d{6})\b")
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ (.*)$")
START_RE = re.compile(r"Session: (?:NEW|RESUME) \S+ for thread (\S+)")
END_RE = re.compile(r"Posted chunk \d+/\d+ to Slack thread (\S+)")
# A turn with no end line in the log (stopped, failed) counts this long.
TURN_MAX = timedelta(minutes=25)
# Slack in the log's clock against the ledger's: the row is written
# inside the turn, a little after its start line and before its post.
SLACK = timedelta(seconds=5)


def room_projects() -> dict[str, str]:
    """{channel id: project} for the project rooms in the registry."""
    out = {}
    for cid, e in ea_channels.load().items():
        if e.get("kind", "project") == "project" and e.get("project"):
            out[cid] = e["project"]
    return out


def thread_channels() -> dict[str, str]:
    """{thread key: channel id} from the session registry."""
    out = {}
    for key, row in slack_sessions._load().items():
        ch = (row.get("meta") or {}).get("channel")
        if ch:
            out[key] = ch
    return out


def turn_windows(log_path: Path) -> list[tuple[datetime, datetime, str]]:
    """(start, end, thread) for each turn in the chat bot's log. The log's
    clock is local time."""
    starts: dict[str, datetime] = {}
    out = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"note: the chat bot's log cannot be read ({e}); thread names only",
              file=sys.stderr)
        return []
    for line in lines:
        m = LOG_RE.match(line)
        if not m:
            continue
        at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        s = START_RE.search(m.group(2))
        if s:
            if s.group(1) in starts:
                out.append((starts[s.group(1)], starts[s.group(1)] + TURN_MAX, s.group(1)))
            starts[s.group(1)] = at
            continue
        e = END_RE.search(m.group(2))
        if e and e.group(1) in starts:
            out.append((starts.pop(e.group(1)), at, e.group(1)))
    out += [(t, t + TURN_MAX, k) for k, t in starts.items()]
    return out


def _when(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def candidates(conn, rooms: dict[str, str], threads: dict[str, str],
               windows: list[tuple[datetime, datetime, str]]) -> list[dict]:
    """Every open row with no project link and no loose mark, with the
    room evidence found for it and the verdict on linking it."""
    linked = {r[0] for r in conn.execute("SELECT ledger_id FROM project_ledger")}
    loose = {r[0] for r in conn.execute("SELECT ledger_id FROM loose_ledger")}
    out = []
    for row in conn.execute("SELECT * FROM ledger WHERE status IN ('open', 'follow_up', 'snoozed') "
                            "ORDER BY created_at"):
        lid = row["id"]
        if lid in linked or lid in loose:
            continue
        text = " ".join(str(row[k] or "") for k in ("description", "notes", "last_action"))
        named = {}
        for key in THREAD_RE.findall(text):
            pid = rooms.get(threads.get(key, ""))
            if pid:
                named[pid] = key
        evidence, project = "", ""
        if len(named) == 1:
            project, key = next(iter(named.items()))
            evidence = f"its text names thread {key}, a thread of the {project} room"
        elif len(named) > 1:
            out.append({"id": lid, "description": row["description"] or "", "project": "",
                        "evidence": f"its text names threads of several rooms: {sorted(named)}",
                        "action": "skip"})
            continue
        elif MODEL_ID_RE.match(lid) and not ledger.is_detector_row(lid) and not row["source"]:
            at = _when(row["created_at"])
            hits = [k for (a, b, k) in windows if at and a - SLACK <= at <= b + SLACK] if at else []
            chans = {threads.get(k, "?") for k in hits}
            if len(chans) == 1 and rooms.get(next(iter(chans))):
                project = rooms[next(iter(chans))]
                evidence = (f"it was created during a turn in the {project} room "
                            f"(thread {hits[0]}), with no other turn running")
            elif len(chans) > 1 and any(rooms.get(c) for c in chans):
                out.append({"id": lid, "description": row["description"] or "", "project": "",
                            "evidence": "it was created while turns ran in several channels: "
                                        + ", ".join(sorted(chans)), "action": "skip"})
                continue
        if not project:
            continue
        verdict = project_store.room_link_verdict(conn, lid, project, "", created=True)
        if "link" in verdict and not named:
            words = f"{row['description'] or ''} {(row['notes'] or '')[:300]}"
            strong = [h["id"] for h in project_store.match_projects(words, conn) if h["score"] >= 3]
            if strong and project not in strong:
                verdict = {"skipped": f"its words name only {', '.join(strong)}"}
        out.append({"id": lid, "description": row["description"] or "", "project": project,
                    "evidence": evidence,
                    "action": "link" if "link" in verdict else "skip",
                    "why_not": verdict.get("skipped", "")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--apply", action="store_true", help="link the rows (default: report only)")
    ap.add_argument("--bot-log", type=Path, default=None,
                    help="the chat bot's log (default: slack-bot.log in the owner's log dir)")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = ap.parse_args()

    log = args.bot_log or (log_paths.owner_log_dir() / "slack-bot.log")
    rooms, threads, windows = room_projects(), thread_channels(), turn_windows(log)
    if args.apply:
        conn = project_store._get_db()
    else:
        conn = ledger.guarded_connect(project_store.DB_PATH, read_only=True)
        conn.row_factory = project_store.sqlite3.Row
    rows = candidates(conn, rooms, threads, windows)
    done = []
    if args.apply:
        import closure
        for r in rows:
            if r["action"] == "link":
                project_store.link_ledger(conn, r["project"], r["id"])
                closure.record_event(conn, r["id"], "placed", actor="owner:repair_room_links",
                                     evidence={"from": [], "to": r["project"]},
                                     note=r["evidence"])
                done.append(r["id"])
        conn.commit()
    if args.json:
        print(json.dumps({"applied": bool(args.apply), "linked": done, "rows": rows}, indent=1))
        return 0
    links = [r for r in rows if r["action"] == "link"]
    print(f"{len(links)} row(s) to link" + (f", {len(done)} linked" if args.apply else
                                             " (a report: nothing written; --apply links them)"))
    for r in rows:
        mark = "LINK" if r["action"] == "link" else "skip"
        print(f"  {mark} {r['id']} -> {r['project'] or '-'}: {r['description'][:80]}")
        print(f"       because {r['evidence']}"
              + (f"; not linked: {r['why_not']}" if r.get("why_not") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
