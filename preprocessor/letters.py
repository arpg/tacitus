#!/usr/bin/env python3
"""Letters someone asked the owner to write: one case per person, tracked
to submission.

A letter case is a work case (cases.py) of kind `letter`
(case_kinds/letter.py): one person the owner was asked to write for (a
recommendation, a reference, a nomination, a letter of support or a
character letter, for a student, an employee, a colleague, a tenant or a
client). Its parts are DESTINATIONS (a program, an employer, an award, a
portal), each with its own due date, how it is submitted and a state:

    asked -> accepted (or declined) -> drafted -> ready -> submitted

This module keeps the letter API and CLI of the first letter release as a
thin layer over cases.py: the same functions, the same dict shapes
(`person_name`, `destinations`, `submitted_at`) and the same verbs, so the
work lane (letters_work.py) and the surfaces (letter_surface.py) read and
write letter cases as they did. The store, the closure rules, joining,
questions and detection are cases.py's and the letter kind's.

    letters.py list [--all]
    letters.py get --case <id>
    letters.py accept --case <id> [--quote "<owner's words>"] [--date YYYY-MM-DD]
    letters.py decline --case <id> [--quote "<owner's words>"]
    letters.py set-date --case <id> --date YYYY-MM-DD   (one date for every place)
    letters.py lead --case <id> --days N   (back on his lists N days before the date)
    letters.py start --case <id>           (back on his lists now)
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
    letters.py answer --question <id> --choice "<the answer>" [--date YYYY-MM-DD]
    letters.py migrate [--days 21] [--apply] [--report <path>]

`migrate` here reads the ledger's letter rows of before letter cases into
cases (the first release's step); the move of the letter tables into the
case store is `cases.py migrate`.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import cases as _cases
    from case_kinds import letter as _kind
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import cases as _cases
    from preprocessor.case_kinds import letter as _kind

EA_DIR = _cases.EA_DIR
KINDS = _kind.KINDS
METHODS = _kind.METHODS
STATES = _kind.KIND.states
OPEN_STATES = _cases.OPEN_STATES
DONE_STATES = _kind.KIND.done_states
MATERIAL_KINDS = _kind.MATERIAL_KINDS
ROLES = ("case", "destination")
QUESTION_KINDS = ("write", "join")
REOPEN_DAYS = _cases.REOPEN_DAYS
WRITE_CHOICES = _kind.WRITE_CHOICES
JOIN_SAME, JOIN_OTHER = _cases.JOIN_SAME, _cases.JOIN_OTHER
SUBMITTED_SIGNAL, DECLINED_SIGNAL, CASE_DONE_SIGNAL = _kind.KIND.signal_names
LETTER_SIGNALS = frozenset(_kind.KIND.signal_names)
REPLY_CARD_NOTE = _cases.REPLY_CARD_NOTE
DRAFT_HANDLED_NOTE = _cases.DRAFT_HANDLED_NOTE
NOT_LETTER_DONE_NOTES = _cases.NOT_DONE_NOTES
SEND_SIGNALS = _cases.SEND_SIGNALS
REQUEST_DAYS = _cases.REQUEST_DAYS
CONFIRM_DAYS = _cases.CONFIRM_DAYS
LETTER_ROLES = _kind.LETTER_ROLES

# The letter kind's detection and the shared name rules, as before.
detect = _kind.detect
letterish = _kind.letterish
letter_kind = _kind.letter_kind
find_person = _kind.find_person
find_destination = _kind.find_destination
material_kind = _kind.material_kind
real_url = _cases.real_url
norm_name = _cases.norm_name
name_match = _cases.name_match
norm_dest = _cases.norm_part
_now = _cases._now
_day = _cases._ck.day
_tokens = _cases._ck.name_tokens
_alias_keys = _cases.alias_keys
_OPEN_LEVELS = _cases._OPEN_LEVELS


def _ledger():
    return _cases._ledger()


def _closure():
    return _cases._closure()


def ensure_schema(conn: sqlite3.Connection) -> None:
    _cases.ensure_schema(conn)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """The ledger database with the store's tables, rows as sqlite3.Row."""
    return _cases.connect(db_path)


# ── the letter shape of a case ─────────────────────────────────────────

def _dest(d: dict) -> dict:
    return {"id": d["id"], "case": d["case"], "name": d["name"], "due": d["due"],
            "own_due": d.get("own_due", d["due"]), "named": d.get("named", bool(d["name"])),
            "method": d["method"], "link": d["link"], "state": d["state"],
            "ledger_id": d["ledger_id"], "conversation_id": d["conversation_id"],
            "sender": d["sender"], "draft": d["draft"],
            "submitted_at": d.get("delivered_at") or "", "evidence": d["evidence"]}


def _question(q: dict) -> dict:
    return {"id": q["id"], "kind": q["kind"], "case": q["case"], "destination": q.get("part", ""),
            "ledger_id": q["ledger_id"], "text": q["text"], "choices": q["choices"],
            "payload": q["payload"], "asked_at": q["asked_at"], "answer": q["answer"],
            "answered_at": q["answered_at"]}


def letter_case(c: dict | None) -> dict | None:
    """A store case of kind letter in the first release's shape."""
    if c is None or c.get("kind") != "letter":
        return None
    return {"id": c["id"], "person_name": c["counterpart_name"],
            "person_email": c["counterpart_email"], "kind": c["subkind"], "status": c["status"],
            "decision": c["decision"], "opened": c["opened"], "closed_at": c["closed_at"],
            "materials": c["materials"], "base_letter": c["base_doc"], "notes": c["notes"],
            "aliases": c["aliases"], "keep_level": c["keep_level"],
            "case_ledger_id": c["case_ledger_id"],
            "do_by": c.get("do_by", ""), "do_by_from": c.get("do_by_from", ""),
            "lead_days": c.get("lead_days"), "start_on": c.get("start_on", ""),
            "destinations": [_dest(d) for d in c["parts"]],
            "questions": [_question(q) for q in c["questions"]],
            "messages": [dict(m, destination=m.get("part", "")) for m in c.get("messages") or []]}


def _general(c: dict) -> dict:
    """A letter-shaped case as the store's dict (for cases.summary_line)."""
    if "parts" in c:
        return c
    return {"id": c.get("id"), "kind": "letter", "subkind": c.get("kind", ""),
            "counterpart_name": c.get("person_name", ""),
            "counterpart_email": c.get("person_email", ""), "title": "",
            "status": c.get("status"), "decision": c.get("decision"),
            "keep_level": c.get("keep_level"), "questions": c.get("questions") or [],
            "do_by": c.get("do_by", ""), "lead_days": c.get("lead_days"),
            "start_on": c.get("start_on", ""),
            "parts": [dict(d, delivered_at=d.get("submitted_at", "")) for d in
                      c.get("destinations") or []]}


# ── the store, as letters ──────────────────────────────────────────────

def cases(conn, open_only: bool = True) -> list[dict]:
    return [letter_case(c) for c in _cases.cases(conn, kind="letter", open_only=open_only)]


def case(conn, case_id: str) -> dict | None:
    return letter_case(_cases.case(conn, case_id))


def destination(conn, dest_id: str) -> dict | None:
    d = _cases.part(conn, dest_id)
    return _dest(d) if d else None


def case_for_row(conn, ledger_id: str) -> dict | None:
    return letter_case(_cases.case_for_row(conn, ledger_id))


def case_for_conversation(conn, conversation_id: str, *, open_only: bool = True) -> dict | None:
    return letter_case(_cases.case_for_conversation(conn, conversation_id, open_only=open_only,
                                                    kind="letter"))


def ensure_case(conn, person_name: str, person_email: str = "", kind: str = "recommendation",
                *, keep_level: str = "full", actor: str = "") -> str:
    return _cases.ensure_case(conn, "letter", person_name, person_email, kind,
                              keep_level=keep_level, actor=actor)


def find_case(conn, person_name: str, person_email: str = "", *, open_only: bool = True) -> dict:
    return _cases.find_case(conn, "letter", person_name, person_email, open_only=open_only)


def link_row(conn, ledger_id: str, case_id: str, dest_id: str = "", *,
             role: str = "destination") -> bool:
    return _cases.link_row(conn, ledger_id, case_id, dest_id,
                           role="case" if role == "case" else "part")


def add_destination(conn, case_id: str, name: str, due: str = "", method: str = "unknown",
                    link: str = "", ledger_id: str = "", *, conversation_id: str = "",
                    sender: str = "", message_id: str = "", actor: str = "") -> str:
    return _cases.add_part(conn, case_id, name, due, method, link, ledger_id,
                           conversation_id=conversation_id, sender=sender,
                           message_id=message_id, actor=actor)


def same_destination(conn, case_id: str, name: str, *, sender: str = "", link: str = "") -> str:
    return _cases.same_part(conn, case_id, name, sender=sender, link=link)


def set_due(conn, dest_id: str, due: str, *, actor: str = "") -> dict:
    out = _cases.set_due(conn, dest_id, due, actor=actor)
    return {"dest": out["part"], "due": out["due"], "was": out["was"]}


def add_material(conn, case_id: str, kind: str, path_or_message_id: str, *, actor: str = "") -> bool:
    return _cases.add_material(conn, case_id, kind, path_or_message_id, actor=actor)


def _dest_result(out: dict) -> dict:
    out = dict(out)
    if "part" in out:
        out["dest"] = out.pop("part")
    return out


def set_state(conn, dest_id: str, state: str, evidence: str = "", actor: str = "") -> dict:
    return _dest_result(_cases.set_state(conn, dest_id, state, evidence, actor))


def accept(conn, case_id: str, *, actor: str = "", quote: str = "", do_by: str = "") -> dict:
    return _cases.accept(conn, case_id, actor=actor, quote=quote, do_by=do_by)


def set_date(conn, case_id: str, due: str, *, actor: str = "") -> dict:
    """The case's one date: every destination with no date of its own is
    due by it (cases.set_date)."""
    return _cases.set_date(conn, case_id, due, actor=actor)


def set_lead(conn, case_id: str, days, *, actor: str = "") -> dict:
    return _cases.set_lead(conn, case_id, days, actor=actor)


def start_now(conn, case_id: str, *, actor: str = "") -> dict:
    return _cases.start_now(conn, case_id, actor=actor)


def decline(conn, case_id: str, *, actor: str = "", quote: str = "") -> dict:
    return _cases.decline(conn, case_id, actor=actor, quote=quote)


def record_draft(conn, case_id: str, path: str, dest_id: str = "", *, actor: str = "") -> dict:
    return _cases.record_draft(conn, case_id, path, dest_id, actor=actor)


def merge(conn, from_id: str, into_id: str, *, actor: str = "") -> dict:
    return _cases.merge(conn, from_id, into_id, actor=actor)


def remember(conn, key: str, case_id: str, answer_: str) -> None:
    _cases.remember(conn, key, case_id, answer_)


def question_dict(q) -> dict:
    return _question(_cases.question_dict(q))


def ask(conn, kind: str, case_id: str, text: str, choices, *, dest_id: str = "",
        ledger_id: str = "", payload: dict | None = None) -> str:
    if kind not in QUESTION_KINDS:
        raise ValueError(f"question kind must be one of {', '.join(QUESTION_KINDS)}")
    return _cases.ask(conn, kind, case_id, text, choices, part_id=dest_id, ledger_id=ledger_id,
                      payload=payload)


def questions(conn, *, open_only: bool = True) -> list[dict]:
    return [_question(q) for q in _cases.questions(conn, open_only=open_only, kind="letter")]


def answer(conn, question_id: str, choice: str, *, actor: str = "owner", do_by: str = "") -> dict:
    return _dest_result(_cases.answer(conn, question_id, choice, actor=actor, do_by=do_by))


def ask_write(conn, case_id: str, *, ledger_id: str = "") -> str:
    return _cases.ask_decision(conn, case_id, ledger_id=ledger_id)


def ask_join(conn, case_id: str, candidate: str, name: str, email: str = "", *,
             dest_id: str = "", ledger_id: str = "") -> str:
    return _cases.ask_join(conn, case_id, candidate, name, email, part_id=dest_id,
                           ledger_id=ledger_id)


# ── the hooks every reader asks ────────────────────────────────────────

def is_letter_row(row: dict | None) -> bool:
    """True for a ledger row of a letter case (its metadata's `letter`).
    Every other kind of work case: cases.is_case_row."""
    return _cases.case_link(row).get("kind") == "letter"


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
    ask_ = str(item.get("ask") or rec.get("ask") or brief.get("ask") or "")
    # The ask decides when there is one; a title alone only without it.
    title = "" if ask_ else str(item.get("title") or item.get("subject") or brief.get("subject")
                                or item.get("description") or "")
    found = detect({"subject": title, "from": str(item.get("from") or item.get("from_email") or ""),
                    "result": {"ask": ask_, "summary": "", "links": rec.get("links") or [],
                               "confidential_kind": rec.get("confidential_kind") or ""}},
                   plugin_senders=([], []))
    return bool(found) and found["role"] in ("request", "reminder")


def letter_role(row: dict | None) -> str:
    link = _cases.case_link(row)
    if link.get("kind") != "letter":
        return ""
    return "case" if link["role"] == "case" else "destination"


def letter_signal_allowed(signal: str, entry: dict, *, note: str = "") -> bool:
    """Whether closure evidence may act on a letter row at all (close or
    propose): cases.signal_allowed for a letter row, True for any other."""
    if not is_letter_row(entry):
        return True
    return _cases.signal_allowed(signal, entry, note=note)


# ── reading a case ─────────────────────────────────────────────────────

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
    return _cases.summary_line(_general(c), level=level, today=today)


# ── ingest ─────────────────────────────────────────────────────────────

def _legacy_result(res: dict) -> dict:
    """An ingest result in the first release's words."""
    out = dict(res)
    if out.get("action") == "part":
        out["action"] = "destination"
    if "part" in out:
        out["destination"] = out.pop("part")
    out.pop("kind", None)
    return out


def ingest_record(conn, rec: dict, *, ledger_id: str = "", keep_level: str = "full",
                  found: dict | None = None, actor: str = "intake") -> dict:
    """Act on one mail record as a letter (cases.ingest_record)."""
    if found is None:
        found = detect(rec)
    if not found:
        return {"action": "none"}
    found = dict(found, case_kind="letter")
    found.setdefault("counterpart", found.get("person") or "")
    found.setdefault("part", found.get("destination") or "")
    found.setdefault("parts", found.get("destinations") or [])
    return _legacy_result(_cases.ingest_record(conn, rec, ledger_id=ledger_id,
                                               keep_level=keep_level, found=found, actor=actor))


def ingest(records: list[dict], *, rows_by_message: dict | None = None,
           levels: dict | None = None, db_path: Path | None = None, actor: str = "intake",
           now: datetime | None = None) -> list[dict]:
    """cases.ingest over a run's mail records for the letter kind."""
    return [_legacy_result(r) for r in _cases.ingest(
        records, rows_by_message=rows_by_message, levels=levels, db_path=db_path, actor=actor,
        now=now, kinds_=["letter"])]


# ── migration ────────────────────────────────────────────────────────

_ACCEPTED_WORDS = re.compile(r"\b(?:agreed to write|will write|i'?ll write|accepted|said yes|"
                             r"agreement to write|happy to write|glad to write|"
                             r"letter writing and submission remain)\b", re.I)
# A place and its date as a row's notes list them: "Westmoor College (Dec 1)".
_LISTED = re.compile(r"([A-Z][\w&'\u2019.-]*(?:\s+(?:[A-Z][\w&'\u2019.-]*|of|for|at|and|the)){0,7})"
                     r"\s*\((?:by\s+|due\s+)?([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\)")


def listed_destinations(text: str, today: date) -> list[dict]:
    """[{name, due}] for each "Place (Mon D)" in a row's own words."""
    led = _cases._ledger()
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
            not found.get("person") or _cases._is_senders_name(found["person"], rec["from"], rec)):
        found["person"] = (found.get("person") or str(row.get("from_name") or "")
                           or _cases.name_from_address(rec["from"]))
        found["person_is_sender"] = True
        born = str(row.get("date_identified") or row.get("created_at") or "")[:10]
        try:
            today = date.fromisoformat(born)
        except ValueError:
            today = date.today()
        found["destinations"] = found.get("destinations") or listed_destinations(notes, today)
    return found


_name_from_address = _cases.name_from_address


def migration_plan(conn, *, days: int = 21, today: date | None = None) -> dict:
    """How the open and recently closed letter rows become cases: a dry
    run that writes nothing. Each row: its reading, the case (by person) and
    the role it takes; closed rows say whether their close holds (a
    submission or the owner's word) or was a reply's or a tap's."""
    led = _cases._ledger()
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
    led = _cases._ledger()
    counts = {"cases": 0, "destinations": 0, "case_rows": 0, "reopened": 0, "submitted": 0}
    for g in plan.get("cases") or []:
        cid = ensure_case(conn, g["person"], g.get("email") or "", g.get("kind") or "recommendation",
                          actor=actor)
        counts["cases"] += 1
        for x in g["rows"]:
            r = _cases._row(conn, x["id"])
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
                conn.execute("UPDATE case_parts SET state = 'submitted', delivered_at = ?, "
                             "evidence = ? WHERE id = ?", (r["closed_at"], "closed before letter "
                                                           "cases: " + (x["closed_by"] or "?"), did))
                counts["submitted"] += 1
                continue
            if reopen_it:
                conn.execute("UPDATE ledger SET status = 'open', closed_at = NULL, updated_at = ? "
                             "WHERE id = ?", (_now(), x["id"]))
                _cases._closure().record_event(conn, x["id"], "reopen", signal=x["closed_by"], actor=actor,
                                        note="a letter closes on its submission, not on a reply")
                counts["reopened"] += 1
            add_destination(conn, cid, x["destination"], x["due"], x["method"], x["link"], x["id"],
                            actor=actor, **where)
            counts["destinations"] += 1
        if g.get("accepted"):
            accept(conn, cid, actor=actor, quote="recorded before letter cases")
        if _cases._maybe_close_case(conn, cid, actor=actor) is None and not g.get("accepted"):
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
        if name == "accept":
            s.add_argument("--date", default="", help="the case's one date, YYYY-MM-DD")
    s = sub.add_parser("set-date", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--date", required=True, help="YYYY-MM-DD, or \"\" for no date")
    s = sub.add_parser("lead", allow_abbrev=False)
    s.add_argument("--case", required=True)
    s.add_argument("--days", required=True)
    s = sub.add_parser("start", allow_abbrev=False)
    s.add_argument("--case", required=True)
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
    s.add_argument("--date", default="")
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
            res = accept(conn, args.case, actor=actor, quote=args.quote, do_by=args.date)
        elif args.cmd == "set-date":
            res = set_date(conn, args.case, args.date, actor=actor)
        elif args.cmd == "lead":
            res = set_lead(conn, args.case, args.days, actor=actor)
        elif args.cmd == "start":
            res = start_now(conn, args.case, actor=actor)
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
            res = answer(conn, args.question, args.choice, actor=actor, do_by=args.date)
        elif args.cmd == "migrate":
            plan = migration_plan(conn, days=args.days)
            if args.report:
                Path(args.report).write_text(plan_report(plan, public=False) + "\n")
            res = apply_migration(conn, plan, reopen=args.reopen, actor=actor)
        else:
            return _err(f"unknown command {args.cmd}")
        conn.commit()
        return _out({"ok": True, **res})
    except (ValueError, _cases.MigrationPending) as e:
        return _err(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
