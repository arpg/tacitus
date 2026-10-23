#!/usr/bin/env python3
"""Nightly ledger reconcile: every open row against the mail's records,
one project at a time, with no message text leaving the machine.

A room session that holds a whole project's rows and threads at once
rewrites the rows well: it reads the threads, cross-links the rows,
and says which look done. The crons judge one row at a time by signal
and never touch a note. This lane does the room's work every night,
under the mail gate's rule: the remote model reads the mail, code
composes each thread into a brief, and Claude judges the briefs.

    1. Code refreshes the thread brief on every open row with a
       conversation id (mail_privacy.sync_briefs: the remote model reads
       any message that has no record yet). Rows with no conversation id
       get candidate threads from the record index, matched by the row's
       own distinctive words, its correspondent and the project's people.
       No mailbox search: the index is a local file.
    2. Code groups the rows by project (project_store), linking an
       unlinked row to the one project its words name. The rest form a
       loose bucket, chunked.
    3. One tool-less Claude call per group judges the briefs: open, done
       or duplicate, a thread for a row that has none, and one line on
       what changed. Refs it invents are dropped. A kept thread (a
       keep_from_anthropic sender) reaches it as counts and dates only.
    4. Code writes: a `done` or `duplicate` verdict is a close PROPOSAL
       (closure.propose; the morning briefing's "Looks done?" line and
       the DM card carry it, one click closes); a note is appended to
       last_action with the date; a thread goes onto the row's
       `conversation_id` when the row's own reply cannot close it
       (ledger.closes_on_reply), else into metadata only; a row the
       words place in a project is linked there and the thread filed.

With the remote model down the night is skipped and the health line
says so. Nothing here is urgent, and a hosted reader would move the
night's mail to Anthropic, which the gate exists to prevent.

Switch: `"reconcile": "nightly"` in remote-llm.json, with `mail` at
enforce (the records exist only then). Off, or outside an owner
process, `run` exits 3.

    ledger_reconcile.py run [--dry-run] [--project <id>] [--post]
    ledger_reconcile.py status

Store: <private_dir>/remote-llm/reconcile.jsonl, one record per run,
counts and timings only. Log: a `[reconcile]` line per written row in
the owner-private `logs/ledger-reconcile.log`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import closure  # noqa: E402
import ledger as _ledger  # noqa: E402
import remote_llm  # noqa: E402

LOG_NAME = "ledger-reconcile.log"
RUNS = "reconcile.jsonl"
EXIT_OFF = 3
SIGNAL = "llm:reconcile"
DUP_SIGNAL = "llm:reconcile-duplicate"
ACTOR = "reconcile"
MODEL = "opus"
VERDICTS = ("open", "done", "duplicate")

GROUP_CAP = 14          # rows per Claude call
LOOSE_LABEL = "loose"
BRIEF_CAP = 120         # threads sync_briefs may fetch in one night
CAND_PER_ROW = 3        # candidate threads per id-less row
CAND_DAYS = 90          # a candidate thread had mail inside this window
MIN_HITS = 2            # distinctive words a thread must share with a row
MSG_CAP = 8             # messages of a thread shown, the latest
GIST_CHARS = 220
NOTE_CHARS = 160
NOTES_CHARS = 400
LINK_SCORE = 3          # project_store.match score that places a row
LINK_GAP = 2            # ... when the runner-up is this far behind
RARE_DF = 2

_STOP = {
    "about", "after", "again", "before", "being", "between", "could", "every",
    "first", "needs", "other", "reply", "response", "request", "should", "their",
    "there", "these", "those", "under", "until", "where", "which", "while", "would",
    "chris", "email", "mail", "sent", "send", "follow", "check", "update", "please",
    "thanks", "week", "next", "last", "still", "open", "done", "task", "waiting",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def active(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else remote_llm.config()
    return cfg.get("reconcile") == "nightly" and remote_llm.enforcing("mail", cfg)


def _log(line: str) -> None:
    """One line per written row, in the owner-private log directory."""
    try:
        import log_paths
        with open(log_paths.owner_log(LOG_NAME), "a") as f:
            f.write(f"{_iso(_now())} {line}\n")
    except OSError:
        pass


# ── words ─────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\-]{3,}", (text or "").lower())
            if t not in _STOP and not t.isdigit()}


def _row_text(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in
                    ("description", "notes", "from_name", "from_email", "last_action"))


def _people_tokens(text: str) -> set[str]:
    """Last names and address local parts, four letters or more."""
    out = set()
    for chunk in re.split(r"[\s,;<>()]+", (text or "").lower()):
        chunk = chunk.split("@", 1)[0]
        for part in re.split(r"[.\-_]", chunk):
            if len(part) >= 4 and part.isalpha():
                out.add(part)
    return out


def distinctive(row: dict, df: dict[str, int]) -> set[str]:
    """The row's words that few other open rows use."""
    return {t for t in _tokens(row.get("description") or "") | _tokens(row.get("notes") or "")
            if (len(t) >= 5 and df.get(t, 0) <= RARE_DF) or (len(t) == 4 and df.get(t, 0) <= 1)
            or re.match(r"^[a-z]{2,8}-\d{2,4}-\d{3,6}$", t)}


def doc_freq(rows: list[dict]) -> dict[str, int]:
    df: dict[str, int] = {}
    for r in rows:
        for t in _tokens(r.get("description") or ""):
            df[t] = df.get(t, 0) + 1
    return df


# ── the threads ───────────────────────────────────────────────────────

def _thread_text(recs: list[dict]) -> str:
    parts = []
    for r in recs:
        parts.append(r.get("subject") or "")
        parts.append(r.get("from") or "")
        res = r.get("result") or {}
        for p in res.get("people") or []:
            if isinstance(p, dict):
                parts.append(p.get("name") or "")
    return " ".join(parts)


def thread_table(index: dict, *, now: datetime, user: set[str]) -> list[dict]:
    """One entry per thread with mail inside CAND_DAYS: its words, its
    people and its senders, computed once for every row's search."""
    since = _iso(now - timedelta(days=CAND_DAYS))
    table = []
    for conv, recs in index["by_conv"].items():
        latest = max((r.get("received") or "" for r in recs), default="")
        if latest < since:
            continue
        text = _thread_text(recs)
        table.append({"conversation_id": conv, "latest": latest, "toks": _tokens(text),
                      "ptoks": _people_tokens(text),
                      "senders": {(r.get("from") or "").lower() for r in recs} - user})
    return table


def candidate_threads(row: dict, table: list[dict], df: dict[str, int], *,
                      people: set[str]) -> list[dict]:
    """Threads that look like this id-less row's: the row's distinctive
    words on the thread's subjects and people, its correspondent as a
    sender, the project's people as senders. Each {conversation_id,
    score, hits, latest}, best first."""
    distinct = distinctive(row, df)
    row_people = _people_tokens(row.get("from_name") or "") | _people_tokens(row.get("from_email") or "")
    frm = (row.get("from_email") or "").lower()
    out = []
    for t in table:
        hits = distinct & t["toks"]
        score = 2 * len(hits)
        by_sender = bool(frm) and frm in t["senders"]
        by_person = bool(row_people & t["ptoks"])
        if by_sender:
            score += 3
        if by_person:
            score += 2
        if people & t["senders"]:
            score += 1
        # The row's own words must be on the thread: a shared person alone
        # (an advisor on every thread) is not a match.
        if len(hits) >= MIN_HITS or (hits and by_sender):
            out.append({"conversation_id": t["conversation_id"], "score": score,
                        "hits": sorted(hits)[:6], "latest": t["latest"]})
    out.sort(key=lambda c: (-c["score"], c["latest"]))
    return out[:CAND_PER_ROW]


def _kept_stub(brief: dict) -> dict:
    return {"kept": True, "message_count": brief.get("message_count"),
            "latest": brief.get("latest"), "last_from_user": brief.get("last_from_user"),
            "user_replied_after_latest": brief.get("user_replied_after_latest")}


def thread_view(brief: dict | None) -> dict | None:
    """What Claude sees of one thread: dates, who (own or not), gists,
    asks. A kept thread shows counts and dates only."""
    if not brief:
        return None
    if brief.get("kept_from_claude"):
        return _kept_stub(brief)
    msgs = sorted(brief.get("messages") or [], key=lambda m: m.get("received") or "")[-MSG_CAP:]
    view = {"subject": brief.get("subject") or "", "message_count": brief.get("message_count"),
            "latest": brief.get("latest") or "", "last_from_user": brief.get("last_from_user") or "",
            "user_replied_after_latest": bool(brief.get("user_replied_after_latest")),
            "messages": []}
    for m in msgs:
        v = {"when": (m.get("received") or "")[:10], "from": "the user" if m.get("own") else
             (m.get("from") or "").split("@")[0]}
        if m.get("gist"):
            v["gist"] = str(m["gist"])[:GIST_CHARS]
        if m.get("ask"):
            v["ask"] = str(m["ask"])[:GIST_CHARS]
        if m.get("state"):
            v["state"] = m["state"]
        if m.get("awaits_reply"):
            v["awaits_reply"] = True
        if m.get("unread"):
            v["unread"] = True
        view["messages"].append(v)
    if brief.get("waiting_on_reply"):
        view["waiting_on_reply"] = brief["waiting_on_reply"]
    if brief.get("ask"):
        view["latest_ask"] = str(brief["ask"])[:GIST_CHARS]
    if brief.get("deadline"):
        view["deadline"] = brief["deadline"]
    return view


# ── the groups ────────────────────────────────────────────────────────

def _store_conn():
    import project_store
    return project_store._get_db()


def project_map(conn) -> tuple[dict[str, dict], dict[str, str]]:
    """({project id: {name, status, people: set of addresses}}, {ledger id:
    project id}) over the projects that are not closed."""
    projects: dict[str, dict] = {}
    for r in conn.execute("SELECT id, name, status FROM projects WHERE status != 'closed'"):
        projects[r["id"]] = {"name": r["name"], "status": r["status"], "people": set()}
    for r in conn.execute("SELECT project_id, email FROM project_people"):
        if r["project_id"] in projects and r["email"]:
            projects[r["project_id"]]["people"].add(r["email"].lower())
    links: dict[str, str] = {}
    for r in conn.execute("SELECT project_id, ledger_id FROM project_ledger"):
        if r["project_id"] in projects:
            links.setdefault(r["ledger_id"], r["project_id"])
    return projects, links


def place_row(row: dict, conn, projects: dict[str, dict] | None = None) -> str:
    """The one ACTIVE project the row's words name clearly, else ''. The
    matcher scores a closed project's people too, so the hits are
    filtered here."""
    import project_store
    projects = projects if projects is not None else project_map(conn)[0]
    hits = [h for h in project_store.match_projects(_row_text(row), conn)
            if (projects.get(h["id"]) or {}).get("status") == "active"]
    if not hits or hits[0]["score"] < LINK_SCORE:
        return ""
    if len(hits) > 1 and hits[0]["score"] - hits[1]["score"] < LINK_GAP:
        return ""
    return hits[0]["id"]


def link_row(conn, project_id: str, ledger_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO project_ledger (project_id, ledger_id) VALUES (?, ?)",
                 (project_id, ledger_id))
    conn.commit()


def file_thread(conn, project_id: str, brief: dict, gist: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO project_messages (project_id, message_id, conversation_id, "
        "subject, from_email, received, direction, gist, filed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, "", brief.get("conversation_id") or "", brief.get("subject") or "",
         "", brief.get("latest") or "", "in", (gist or "")[:300], _iso(_now())))
    conn.commit()


def groups(rows: list[dict], conn, *, only: str = "", link=None) -> tuple[list[dict], list[dict]]:
    """[{label, project, name, people, rows}], and the rows this pass
    linked to a project ([{id, project}]). An unlinked row the words
    place is linked here (do-and-tell); the rest go to the loose
    bucket in chunks."""
    projects, links = project_map(conn)
    linked = []
    by_project: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for row in rows:
        pid = links.get(row["id"]) or ""
        if not pid:
            pid = place_row(row, conn, projects)
            if pid:
                (link or link_row)(conn, pid, row["id"])
                linked.append({"id": row["id"], "project": pid})
        if pid:
            by_project.setdefault(pid, []).append(row)
        else:
            loose.append(row)
    out = []
    for pid, prow in sorted(by_project.items()):
        if only and pid != only:
            continue
        for i in range(0, len(prow), GROUP_CAP):
            out.append({"label": pid if i == 0 else f"{pid}#{i // GROUP_CAP + 1}", "project": pid,
                        "name": projects[pid]["name"], "people": projects[pid]["people"],
                        "rows": prow[i:i + GROUP_CAP]})
    if not only:
        loose.sort(key=lambda r: (r.get("from_email") or "", r.get("id") or ""))
        for i in range(0, len(loose), GROUP_CAP):
            out.append({"label": f"{LOOSE_LABEL}#{i // GROUP_CAP + 1}", "project": "",
                        "name": "", "people": set(), "rows": loose[i:i + GROUP_CAP]})
    return out, linked


# ── the items ─────────────────────────────────────────────────────────

def _prev_thread(row: dict) -> str:
    rec = row.get("reconcile") if isinstance(row.get("reconcile"), dict) else {}
    return str(rec.get("thread") or "")


def items_for(group: dict, *, index: dict, df: dict[str, int], user: set[str],
              now: datetime, brief_of=None, table: list[dict] | None = None
              ) -> tuple[list[dict], dict[str, dict]]:
    """(the items Claude sees, {thread ref: brief}). Each item carries its
    thread's view when the row has one, else up to CAND_PER_ROW
    candidate views by ref (T1, T2, ...)."""
    import mail_privacy
    brief_of = brief_of or (lambda conv: mail_privacy.thread_brief(conv, index, user=user, now=now))
    table = table if table is not None else thread_table(index, now=now, user=user)
    items, threads = [], {}
    tn = 0
    for n, row in enumerate(group["rows"], 1):
        ref = f"R{n}"
        item = {"ref": ref, "title": row.get("description") or row.get("id") or "",
                "type": row.get("type") or "", "status": row.get("status") or "open",
                "who": row.get("from_name") or row.get("from_email") or "",
                "opened": (row.get("date_identified") or "")[:10],
                "due": row.get("deadline") or "", "last_action": (row.get("last_action") or "")[:300],
                "notes": (row.get("notes") or "")[:NOTES_CHARS]}
        wait = _ledger.waiting_on_reply(row, now.astimezone().date())
        if wait:
            item["waiting_on_user_question"] = wait
        conv = (row.get("conversation_id") or "").strip() or _prev_thread(row)
        if conv:
            brief = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else None
            if not brief or brief.get("conversation_id") != conv:
                brief = brief_of(conv)
            view = thread_view(brief)
            if view:
                item["thread"] = view
            elif brief is None:
                item["thread"] = {"no_records": True}
        else:
            cands = candidate_threads(row, table, df, people=group["people"])
            views = []
            for c in cands:
                brief = brief_of(c["conversation_id"])
                view = thread_view(brief)
                if not view:
                    continue
                tn += 1
                tref = f"T{tn}"
                threads[tref] = brief
                views.append({"ref": tref, "matched_on": c["hits"], **view})
            if views:
                item["candidate_threads"] = views
        items.append(item)
    return items, threads


# ── the judge ─────────────────────────────────────────────────────────

SYSTEM = """You reconcile a professor's obligation ledger with his mail for his assistant. Answer with JSON only.

{profile}
It is {now}.

You get one group of open ledger rows ({group}) and, for each, what the mail shows: the row's own thread as a list of messages (date, who, a one-line gist, the ask), or candidate threads (T refs) that may be the row's thread. "the user" is the professor. A thread marked kept shows counts and dates only. Nothing here is the mail's text.

For every row, by its ref, answer:
- verdict: "done" when the thread shows the row's work happened (he replied to the person who asked, the thing was sent, the meeting was placed, the question was answered); "duplicate" when another row in this group (give `of`) carries the same work; else "open". Never say done because time passed, and never because the row's note sounds finished: the thread must show it.
- thread: for a row with candidate threads, the T ref that is the row's own thread, or "" when none is. A thread that merely mentions the topic is not the row's thread.
- note: one sentence, at most {note_chars} characters, on what the mail shows that the row's last_action does not: a reply arrived, the date moved, the thing was sent, who now owns it. Empty when nothing new happened or the last_action already says it; never write "nothing new". Plain words, no em-dash, no advice.
- why: one short sentence for a done or duplicate verdict, naming the message (date and who) that shows it.

Answer with a JSON array of objects {{"ref", "verdict", "of", "thread", "note", "why"}}, one per row, no other text.

{rules}"""


def _profile() -> str:
    try:
        import mail_privacy
        return mail_privacy._profile_line()
    except Exception:
        return "The user is a university professor."


def _rules() -> str:
    try:
        import preferences
        return preferences.render(["briefing", "mail-triage"])
    except Exception:
        return ""


def prompt_for(group: dict, items: list[dict], *, now: datetime) -> str:
    local = now.astimezone()
    label = f"project {group['name']}" if group.get("project") else "rows in no project"
    system = SYSTEM.format(profile=_profile(), now=f"{local:%A %Y-%m-%d}", group=label,
                           note_chars=NOTE_CHARS, rules=_rules() or "")
    return system + "\n\n" + json.dumps({"rows": items}, ensure_ascii=False)


def _claude_judge(prompt: str) -> tuple[str, float | None]:
    import ea_config
    import mail_privacy
    model = ea_config.get_models().get("reconcile") or MODEL
    r = mail_privacy._run_claude(prompt, model, "")
    if r.get("rc") or not r.get("text"):
        raise RuntimeError(f"claude rc={r.get('rc')}: {(r.get('stderr') or '')[-160:]}")
    return str(r["text"]), r.get("cost_usd")


def parse_verdicts(text: str, items: list[dict], threads: dict[str, dict]) -> list[dict]:
    """The model's array, checked: a known ref once, a verdict from the
    list, `of` another ref of this group, `thread` one of the row's own
    candidates, note and why cut to size. Anything else is dropped."""
    text = text or ""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except ValueError:
        return []
    if not isinstance(arr, list):
        return []
    by_ref = {it["ref"]: it for it in items}
    seen: set[str] = set()
    out = []
    for v in arr:
        if not isinstance(v, dict):
            continue
        ref = str(v.get("ref") or "")
        if ref not in by_ref or ref in seen:
            continue
        seen.add(ref)
        verdict = str(v.get("verdict") or "open").lower()
        if verdict not in VERDICTS:
            verdict = "open"
        of = str(v.get("of") or "")
        if verdict == "duplicate" and (of not in by_ref or of == ref):
            verdict = "open"
            of = ""
        if verdict != "duplicate":
            of = ""
        thread = str(v.get("thread") or "")
        own = {c["ref"] for c in by_ref[ref].get("candidate_threads") or []}
        if thread not in own or thread not in threads:
            thread = ""
        note = re.sub(r"\s+", " ", str(v.get("note") or "")).strip()[:NOTE_CHARS]
        why = re.sub(r"\s+", " ", str(v.get("why") or "")).strip()[:300]
        if verdict in ("done", "duplicate") and not why:
            verdict = "open"
        out.append({"ref": ref, "verdict": verdict, "of": of, "thread": thread,
                    "note": note.replace("—", ","), "why": why.replace("—", ",")})
    return out


# ── the writes ────────────────────────────────────────────────────────

def _append_last_action(entry_id: str, line: str) -> None:
    conn = _ledger.guarded_connect(_ledger.DB_PATH)
    try:
        row = conn.execute("SELECT last_action FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return
        prev = (row[0] or "").strip()
        new = f"{prev} {line}".strip() if prev else line
        conn.execute("UPDATE ledger SET last_action = ?, updated_at = ? WHERE id = ?",
                     (new, _ledger._now(), entry_id))
        conn.commit()
    finally:
        conn.close()


def _set_conversation(entry_id: str, conv: str) -> None:
    conn = _ledger.guarded_connect(_ledger.DB_PATH)
    try:
        conn.execute("UPDATE ledger SET conversation_id = ? WHERE id = ? "
                     "AND (conversation_id IS NULL OR conversation_id = '')", (conv, entry_id))
        conn.commit()
    finally:
        conn.close()


def apply(group: dict, items: list[dict], threads: dict[str, dict], verdicts: list[dict], *,
          now: datetime, dry_run: bool, store_conn=None, propose=None, set_meta=None,
          append_action=None, set_conv=None) -> dict:
    """Write what the verdicts allow. Returns counts and the rows touched."""
    propose = propose or _propose
    set_meta = set_meta or _ledger.set_metadata
    append_action = append_action or _append_last_action
    set_conv = set_conv or _set_conversation
    by_ref = {it["ref"]: it for it in items}
    rows = {f"R{n}": r for n, r in enumerate(group["rows"], 1)}
    day = f"{now.astimezone():%Y-%m-%d}"
    out = {"proposed": [], "noted": [], "threaded": [], "filed": 0, "skipped": []}
    for v in verdicts:
        row = rows.get(v["ref"])
        if row is None:
            continue
        prev = row.get("reconcile") if isinstance(row.get("reconcile"), dict) else {}
        rec = {"at": _iso(now), "verdict": v["verdict"], "note": v["note"], "why": v["why"],
               "group": group["label"], "thread": prev.get("thread") or ""}
        if v["verdict"] == "duplicate":
            rec["of"] = rows[v["of"]]["id"]
        touched = False
        # A thread for a row that had none. A reply row's own reply closes
        # it (decisive), so a wrong link would close it silently: the
        # link stays in metadata there, and the next night reads it.
        if v["thread"] and not (row.get("conversation_id") or "").strip():
            conv = threads[v["thread"]].get("conversation_id") or ""
            if conv:
                rec["thread"] = conv
                if not dry_run:
                    if not _ledger.closes_on_reply(row):
                        set_conv(row["id"], conv)
                    if group.get("project") and store_conn is not None:
                        file_thread(store_conn, group["project"], threads[v["thread"]],
                                    by_ref[v["ref"]].get("title") or "")
                        out["filed"] += 1
                out["threaded"].append({"id": row["id"], "conversation_id": conv,
                                        "on_row": not _ledger.closes_on_reply(row)})
                touched = True
        if v["verdict"] in ("done", "duplicate"):
            why = v["why"] if v["verdict"] == "done" else f"duplicate of {rec['of']}: {v['why']}"
            res = {"action": "dry-run"} if dry_run else propose(
                row["id"], signal=SIGNAL if v["verdict"] == "done" else DUP_SIGNAL,
                note=why, evidence={"thread": rec.get("thread") or row.get("conversation_id") or "",
                                    "group": group["label"]},
                evidence_ts=(by_ref[v["ref"]].get("thread") or {}).get("latest") or "")
            rec["proposal"] = res.get("action")
            if res.get("action") in ("proposed", "dry-run"):
                out["proposed"].append({"id": row["id"], "verdict": v["verdict"], "why": why})
            else:
                out["skipped"].append({"id": row["id"], "why": res.get("why") or res.get("action")})
            touched = True
        if v["note"] and v["note"] != (prev.get("note") or ""):
            if not dry_run:
                append_action(row["id"], f"{day}: {v['note']} (reconcile)")
            out["noted"].append({"id": row["id"], "note": v["note"]})
            touched = True
        if not dry_run and (touched or prev.get("verdict") != v["verdict"]):
            set_meta(row["id"], reconcile=rec)
        if touched:
            _log(f"[reconcile] {row['id']} {v['verdict']}"
                 f"{' thread' if v['thread'] else ''}{' note' if v['note'] else ''}")
    return out


def _propose(entry_id: str, **kw) -> dict:
    conn = _ledger.guarded_connect(_ledger.DB_PATH)
    try:
        res = closure.propose(conn, entry_id, actor=ACTOR, **kw)
        conn.commit()
        return res
    finally:
        conn.close()


# ── the run ───────────────────────────────────────────────────────────

def reader_up(cfg: dict | None = None, chat=None) -> bool:
    """One tiny request: the remote model answers, or the night is skipped."""
    try:
        (chat or remote_llm.chat)([{"role": "user", "content": "Reply with the word ok."}],
                                  purpose="reconcile-ping", max_tokens=5, cfg=cfg)
        return True
    except remote_llm.RemoteLLMError:
        return False


def run(*, dry_run: bool = False, only: str = "", now: datetime | None = None,
        rows: list[dict] | None = None, judge=None, sync=None, ping=None,
        store_conn=None, index=None, user: set[str] | None = None, brief_of=None) -> dict:
    """The night's pass. Returns the run record (counts, never text)."""
    import mail_privacy
    now = now or _now()
    t0 = time.monotonic()
    rec: dict = {"ts": now.isoformat(timespec="seconds"), "dry_run": dry_run, "only": only}
    if not (ping or reader_up)():
        rec.update(skipped="the remote model did not answer", seconds=round(time.monotonic() - t0, 1))
        remote_llm.append(RUNS, rec)
        return rec
    synced = (sync or (lambda: mail_privacy.sync_briefs(cap=BRIEF_CAP, now=now)))()
    rec["briefs"] = synced if isinstance(synced, dict) else {}
    rows = rows if rows is not None else _ledger.get_open_entries()
    rows = [r for r in rows if (r.get("status") or "open") in _ledger.OPEN_STATUSES]
    index = index if index is not None else mail_privacy.load_index()
    user = user if user is not None else mail_privacy._user_addresses()
    conn = store_conn if store_conn is not None else _store_conn()
    grp, linked = groups(rows, conn, only=only, link=(lambda *a: None) if dry_run else None)
    df = doc_freq(rows)
    table = thread_table(index, now=now, user=user)
    judge = judge or _claude_judge
    rec.update(rows=len(rows), groups=len(grp), linked=linked, proposed=[], noted=[],
               threaded=[], filed=0, skipped=[], failed=[], cost_usd=0.0)
    for g in grp:
        items, threads = items_for(g, index=index, df=df, user=user, now=now, brief_of=brief_of,
                                   table=table)
        if not any(it.get("thread") or it.get("candidate_threads") for it in items):
            continue        # nothing the mail can say about these rows
        try:
            text, cost = judge(prompt_for(g, items, now=now))
        except Exception as e:
            rec["failed"].append({"group": g["label"], "error": str(e)[:160]})
            print(f"ledger_reconcile: {g['label']}: {str(e)[:200]}", file=sys.stderr)
            continue
        rec["cost_usd"] = round((rec["cost_usd"] or 0) + float(cost or 0), 4)
        verdicts = parse_verdicts(text, items, threads)
        if not verdicts:
            rec["failed"].append({"group": g["label"], "error": "no verdicts parsed"})
            continue
        out = apply(g, items, threads, verdicts, now=now, dry_run=dry_run, store_conn=conn)
        for k in ("proposed", "noted", "threaded", "skipped"):
            rec[k].extend(out[k])
        rec["filed"] += out["filed"]
    rec["seconds"] = round(time.monotonic() - t0, 1)
    remote_llm.append(RUNS, rec)
    return rec


def summary_line(rec: dict) -> str:
    if rec.get("skipped") and isinstance(rec["skipped"], str):
        return f":broom: Reconcile skipped: {rec['skipped']}. Nothing was read or written."
    parts = [f"{rec.get('rows', 0)} rows in {rec.get('groups', 0)} groups"]
    n = len(rec.get("proposed") or [])
    parts.append(f"{n} look{'s' if n == 1 else ''} done (on the morning card)")
    parts.append(f"{len(rec.get('noted') or [])} notes")
    if rec.get("threaded"):
        parts.append(f"{len(rec['threaded'])} rows given their thread")
    if rec.get("linked"):
        parts.append(f"{len(rec['linked'])} rows placed in projects")
    if rec.get("failed"):
        parts.append(f"{len(rec['failed'])} groups failed")
    cost = rec.get("cost_usd")
    tail = f"; ${cost:.2f}" if cost else ""
    head = ":broom: Reconcile (dry run): " if rec.get("dry_run") else ":broom: Reconcile: "
    return head + "; ".join(parts) + tail + "."


def _post(text: str) -> None:
    try:
        import slack_notifier
        slack_notifier.post_to_slack(slack_notifier.health_channel_key(), text)
    except Exception as e:
        print(f"ledger_reconcile: post failed: {str(e)[:160]}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the night's pass")
    r.add_argument("--dry-run", action="store_true", help="judge, write nothing, print counts")
    r.add_argument("--project", default="", help="one project's rows only")
    r.add_argument("--post", action="store_true", help="post the summary line to the health channel")
    sub.add_parser("status", help="the switch and the last run")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        runs = remote_llm.read(RUNS)
        print(json.dumps({"active": active(), "last": runs[-1] if runs else None}, indent=2,
                         default=str))
        return 0
    if not active():
        print("ledger_reconcile: off (reconcile switch, or mail not enforced)", file=sys.stderr)
        return EXIT_OFF
    rec = run(dry_run=a.dry_run, only=a.project)
    line = summary_line(rec)
    print(json.dumps(rec, default=str))
    print(line)
    if a.post:
        _post(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
