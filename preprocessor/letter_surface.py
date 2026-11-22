#!/usr/bin/env python3
"""Where the owner sees a letter case and acts on it (letters.py holds the
cases): one line per case in the briefings, the reminders by date, the two
cards in Action requests, Home's item, the pre-send review's record of a
case and the name a project room gives it.

A letter case is one person the owner was asked to write for, with its
destinations (a program, an employer, an award, a portal), each with its
own due date and state. Every surface shows the case as ONE item: its
destinations' ledger rows and their mail are never items of their own,
never a reply owed, and a reply in one of their threads is never a sign
that the case is done. Only names, destinations, dates and states show
here, never the letter's text or the person's materials, and those obey
the keep levels of the destinations' mail (keep_gate):

    full, summary   the person, the kind, the destinations by name
    facts           the person (a header), no destination names
    local           fixed words: "A kept letter case", counts and dates

Code builds every line; no model writes one. With no letters.py (an
install from before the store) or a store that cannot be read, every
function here gives nothing and each surface stays as it was.

The reminder ladder, per open destination with a due date: LADDER days
before it (7, 3, 1), the day itself, and the day after when it is still
open. The 7 and 3 day rungs are quiet (the briefing's line, the case
block's urgency edge); the day before, the day itself and the day after
are loud (LOUD): the case block is posted again in Action requests with a
push, as the owner asked for any time window. A rung fires once; a
submitted or declined destination fires nothing.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

OPEN_STATES = ("asked", "accepted", "drafted", "ready")
DONE_STATES = ("submitted", "declined")
# How each kind is named where a list names the case by its kind.
KIND_WORDS = {"recommendation": "Recommendation letter", "reference": "Reference letter",
              "nomination": "Nomination letter", "support": "Support letter",
              "character": "Character letter", "evaluation": "Evaluation letter"}
# What the person sent, by the store's material kinds.
MATERIAL_WORDS = {"cv": "a CV", "statement": "a statement", "list": "a list of places",
                  "draft": "a draft letter", "transcript": "a transcript", "sample": "a paper",
                  "other": "other files"}
KEPT_CASE = "A kept letter case"
# Days before a destination's due date when a reminder fires; 0 is the
# day itself, -1 the day after.
LADDER = (7, 3, 1, 0, -1)
LOUD = frozenset({1, 0, -1})
# The briefing's date banner and the do-list take a case this close.
SOON_DAYS = 3
# Home lists a case due this soon, an overdue one, a new one and one
# with no date; the rest stay on the briefing's Letters line.
HOME_DAYS = 14
HOME_MINUTES = 30
# The most destinations a card names one by one, and buttons it offers.
CARD_DESTS = 9
CARD_BUTTONS = 3
STATE_WORDS = {"asked": "not answered yet", "accepted": "to write", "drafted": "drafted",
               "ready": "ready to submit", "submitted": "submitted", "declined": "declined"}
METHOD_WORDS = {"portal": "portal link", "email": "by email", "upload": "you upload it",
                "unknown": "how to submit is not known"}


# ── the store ──────────────────────────────────────────────────────────

def _letters():
    """letters.py (builder A's store), or None on an install without it."""
    try:
        import letters  # type: ignore
        return letters
    except Exception:
        return None


def _connect():
    """The ledger database, read-only, rows by name (as letters.py reads)."""
    import sqlite3
    import ledger
    conn = ledger.guarded_connect(ledger.DB_PATH, read_only=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_cases(conn=None, *, open_only: bool = True) -> list[dict]:
    """The cases as letters.cases gives them; [] without the module or on
    any read failure (logged), so no surface ever fails on it."""
    mod = _letters()
    if mod is None:
        return []
    own = conn is None
    try:
        conn = conn or _connect()
        got = mod.cases(conn, open_only=open_only)
        return [c for c in got or [] if isinstance(c, dict)]
    except Exception as e:
        print(f"letter_surface: the letter cases could not be read: {str(e)[:160]}", file=sys.stderr)
        return []
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _meta(row: dict) -> dict:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            m = json.loads(raw)
            return m if isinstance(m, dict) else {}
        except ValueError:
            return {}
    return {}


def is_letter_row(row: dict) -> bool:
    """A destination's ledger row (letters.is_letter_row; the row's own
    `letter` record when the module is absent)."""
    if not isinstance(row, dict):
        return False
    mod = _letters()
    if mod is not None and hasattr(mod, "is_letter_row"):
        try:
            return bool(mod.is_letter_row(row))
        except Exception:
            pass
    rec = row.get("letter") if isinstance(row.get("letter"), dict) else _meta(row).get("letter")
    return isinstance(rec, dict) and bool(rec.get("case"))


# ── one case in facts ─────────────────────────────────────────────────

def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def today_local() -> date:
    try:
        import user_tz
        return datetime.now(user_tz.zone()).date()
    except Exception:
        return date.today()


def dests(case: dict) -> list[dict]:
    return [d for d in case.get("destinations") or [] if isinstance(d, dict)]


def open_dests(case: dict) -> list[dict]:
    """The destinations still to do, nearest due date first, undated last;
    the store's own order within one date."""
    out = [d for d in dests(case) if (d.get("state") or "asked") in OPEN_STATES]
    return sorted(out, key=lambda d: _date(d.get("due")) or date.max)


def is_new(case: dict) -> bool:
    """No answer from the owner yet: the case records no decision and
    every destination (if it has any yet) is still `asked`."""
    if case.get("decision"):
        return False
    return all((d.get("state") or "asked") == "asked" for d in dests(case))


def is_open(case: dict) -> bool:
    """An open case with something to do: a destination to finish, or no
    destination yet and no decision (the person's own request)."""
    if (case.get("status") or "open") != "open":
        return False
    return bool(open_dests(case)) or (not dests(case) and not case.get("decision"))


def questions(case: dict, kind: str = "") -> list[dict]:
    """The case's open questions for the owner (letters.ask: `write`, the
    new case's decision; `join`, whether two people are one)."""
    return [q for q in case.get("questions") or [] if isinstance(q, dict)
            and not q.get("answer") and (not kind or q.get("kind") == kind)]


def facts(case: dict, today: date) -> dict:
    """{total, submitted, declined, open, next_due, due_next, overdue,
    undated, new, finished} for one case, in code."""
    ds = dests(case)
    opn = open_dests(case)
    dated = [(d, _date(d.get("due"))) for d in opn]
    nxt = min((due for _d, due in dated if due), default=None)
    return {"total": len(ds),
            "submitted": sum(1 for d in ds if d.get("state") == "submitted"),
            "declined": sum(1 for d in ds if d.get("state") == "declined"),
            "open": opn, "next_due": nxt,
            "due_next": [d for d, due in dated if due and due == nxt],
            "overdue": [d for d, due in dated if due and due < today],
            "undated": [d for d, due in dated if not due],
            "new": is_new(case),
            "finished": bool(ds) and not opn and any(d.get("state") == "submitted" for d in ds)
            and not case.get("decision") == "declined"}


def ledger_ids(case: dict, *, open_only: bool = True) -> list[str]:
    """The ledger rows the case stands for: its destinations' (the open
    ones with `open_only`) and the person's own request (`case_ledger_id`,
    letters.py: it closes with the case)."""
    pool = open_dests(case) if open_only else dests(case)
    ids = [str(d["ledger_id"]) for d in pool if d.get("ledger_id")]
    if case.get("case_ledger_id"):
        ids.append(str(case["case_ledger_id"]))
    return list(dict.fromkeys(ids))


def level(case: dict, rows: dict | None = None, *, records=None) -> str:
    """The case's keep level: the strictest of its own `keep_level` and
    its destinations' rows (keep_gate.levels; `rows` maps ledger id to
    row, `records` as keep_gate takes it, False for the rows' own labels
    alone). A row that cannot be found counts as mail kept on the
    machine when the check cannot run."""
    import keep_gate
    found = [case.get("keep_level") or ""]
    pool = [rows[i] for i in ledger_ids(case, open_only=False) if rows and i in rows]
    if pool:
        try:
            found += list(keep_gate.levels(pool, records=records).values())
        except Exception:
            found.append("local")
    return keep_gate.stricter(*[f for f in found if f])


def _kept(lv: str) -> bool:
    return lv in ("local", "facts")


def _clean(text, cap: int = 120) -> str:
    t = " ".join(str(text or "").split()).replace("—", ", ").replace("–", "-")
    t = t.replace("*", "").replace("`", "").replace("<", "").replace(">", "").replace("|", " ")
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def person(case: dict, lv: str = "full") -> str:
    """The person's name as the case gives it ("" at `local`)."""
    if lv == "local":
        return ""
    return _clean(case.get("person_name") or case.get("person_email") or "", 60)


def name(case: dict, lv: str = "full") -> str:
    """The case named by its person and kind: "Recommendation letter for
    Dana Reyes"; fixed words at `local`."""
    who = person(case, lv)
    if not who:
        return KEPT_CASE
    kind = KIND_WORDS.get(str(case.get("kind") or "").lower(), "Letter")
    return f"{kind} for {who}"


def day_words(d: date | None, today: date) -> str:
    """"today", "tomorrow", "Tue Dec 1"; "" for none."""
    if not d:
        return ""
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d:%a %b} {d.day}"


def _n(count: int, one: str, many: str = "") -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _own_line(case: dict, f: dict, today: date, lv: str) -> str:
    """The line in code's words, when letters.summary_line is absent or
    the case is kept: "Letter for Dana Reyes: 3 of 9 submitted. Next due
    Tue Dec 1: 2 destinations."."""
    who = person(case, lv)
    head = f"Letter for {who}" if who else KEPT_CASE
    if f["new"]:
        opened = _date(case.get("opened"))
        body = f"asked {opened:%b} {opened.day}" if opened else "a new request"
        body += f", {_n(f['total'], 'destination')}" if f["total"] else ", no places named yet"
    else:
        body = f"{f['submitted']} of {f['total'] - f['declined']} submitted"
    out = f"{head}: {body}."
    if f["overdue"]:
        late = min(_date(d.get("due")) for d in f["overdue"])
        out += f" {len(f['overdue'])} {'was' if len(f['overdue']) == 1 else 'were'} due {day_words(late, today)}."
    ahead = sorted(due for due in (_date(d.get("due")) for d in f["open"]) if due and due >= today)
    if ahead:
        n = sum(1 for d in f["open"] if _date(d.get("due")) == ahead[0])
        out += f" Next due {day_words(ahead[0], today)}: {_n(n, 'destination')}."
    elif not f["next_due"]:
        out += " No due date yet."
    return out


def line(case: dict, today: date, *, lv: str = "full") -> str:
    """The case's one line wherever a list names it. letters.summary_line
    gives it for an open case; a kept one, or an install without it, gets
    code's own words at its level. A new case adds where to answer; a
    case with no date asks for one on its first day only."""
    f = facts(case, today)
    base = ""
    mod = _letters()
    if mod is not None and hasattr(mod, "summary_line"):
        try:
            base = _clean(mod.summary_line(case, level=lv, today=today), 300)
        except TypeError:
            base = "" if _kept(lv) else _clean(mod.summary_line(case), 300)
        except Exception:
            base = ""
        if _kept(lv) and person(case, "full") and person(case, "full") in base:
            base = ""  # a store's line that names the person never shows for kept mail
    base = base or _own_line(case, f, today, lv)
    if not base.endswith("."):
        base += "."
    if f["new"]:
        base += (" Answer in Action requests." if "will you write it" in base.lower()
                 else " Will you write it? Answer in Action requests.")
    elif f["undated"] and _asks_date(case, today):
        base += " Tell me the due date."
    return base


def _asks_date(case: dict, today: date) -> bool:
    """A case with an undated destination asks for the date once: on the
    day it opened or the day after (the first briefing that sees it)."""
    opened = _date(case.get("opened"))
    return bool(opened) and 0 <= (today - opened).days <= 1


# ── the briefing JSON (preprocess.py) ─────────────────────────────────

def briefing_view(open_rows: list[dict], *, today: date | None = None, cases=None,
                  closed=None) -> dict:
    """What the briefing lanes read about letters, built once in the
    preprocessor: {"cases": [entry], "finished": [entry], "hide": {"ids",
    "convs", "mids"}}, {} with no open case. Each entry is the case's
    line, name, dates and counts at its keep level (the rows carry their
    level, preprocess._mark_keep); `hide` names every row and thread
    the cases stand for."""
    today = today or today_local()
    cases = load_cases() if cases is None else cases
    rows = {str(r.get("id")): r for r in open_rows or [] if isinstance(r, dict) and r.get("id")}
    hide_ids = {i for i, r in rows.items() if is_letter_row(r)}
    entries = []
    for c in cases:
        if not is_open(c):
            continue
        entries.append(entry(c, today, rows))
        hide_ids |= set(ledger_ids(c, open_only=False))
    if closed is None:
        closed = [c for c in load_cases(open_only=False) if facts(c, today)["finished"]]
    finished = []
    for c in closed or []:
        when = max((_date(d.get("submitted_at")) for d in dests(c) if _date(d.get("submitted_at"))),
                   default=None) or _date(c.get("closed_at"))
        if facts(c, today)["finished"] and when and (today - when).days <= 1:
            lv = level(c, rows, records=False)
            finished.append({"case": str(c.get("id") or ""), "name": name(c, lv),
                             "total": facts(c, today)["submitted"]})
    if not entries and not finished and not hide_ids:
        return {}
    convs = {str(rows[i].get("conversation_id")) for i in hide_ids
             if i in rows and rows[i].get("conversation_id")}
    convs |= {str(d["conversation_id"]) for c in cases for d in dests(c) if d.get("conversation_id")}
    mids = {str(rows[i].get("source_message_id")) for i in hide_ids
            if i in rows and rows[i].get("source_message_id")}
    for c in cases:
        for m in c.get("materials") or []:
            ref = str((m or {}).get("ref") or "") if isinstance(m, dict) else ""
            if ref and "/" not in ref and not ref.endswith((".pdf", ".docx", ".doc", ".txt")):
                mids.add(ref)  # the person's own mail, kept as a message id
    entries.sort(key=lambda e: (e["next_due"] or "9999-99-99", e["name"]))
    return {"cases": entries, "finished": finished,
            "hide": {"ids": sorted(hide_ids), "convs": sorted(convs), "mids": sorted(mids)}}


def entry(case: dict, today: date, rows: dict | None = None, *, records=False) -> dict:
    """One case as the briefing lanes read it."""
    lv = level(case, rows or {}, records=records)
    f = facts(case, today)
    ids = ledger_ids(case)
    out = {"case": str(case.get("id") or ""), "line": line(case, today, lv=lv), "name": name(case, lv),
           "person": person(case, lv), "kind": str(case.get("kind") or ""),
           "next_due": f["next_due"].isoformat() if f["next_due"] else "",
           "due_in": (f["next_due"] - today).days if f["next_due"] else None,
           "overdue": len(f["overdue"]), "submitted": f["submitted"], "total": f["total"],
           "undated": len(f["undated"]), "new": f["new"], "keep_level": lv if _kept(lv) else "",
           "ledger_ids": ids,
           "conversation_ids": sorted({str((rows or {}).get(i, {}).get("conversation_id"))
                                       for i in ids if (rows or {}).get(i, {}).get("conversation_id")}),
           "destinations": [{"name": _clean(d.get("name"), 80) if lv in ("full", "summary") else "",
                             "due": str(d.get("due") or "")[:10], "state": d.get("state") or "asked"}
                            for d in open_dests(case)][:CARD_DESTS]}
    return out


def _hidden(item: dict, hide: dict) -> bool:
    ids, convs, mids = (set(hide.get(k) or ()) for k in ("ids", "convs", "mids"))
    return (str(item.get("id") or item.get("ledger_id") or "") in ids
            or str(item.get("ref") or "") in ids | mids
            or str(item.get("conversation_id") or "") in convs - {""}
            or str(item.get("message_id") or item.get("source_message_id") or "") in mids - {""}
            or is_letter_row(item))


def without(view: dict, *lists):
    """Each list without the items a letter case stands for (its rows,
    its threads, its mail): the inputs the ranking reads."""
    hide = (view or {}).get("hide") or {}
    if not hide:
        return lists
    return tuple([x for x in (lst or []) if not (isinstance(x, dict) and _hidden(x, hide))]
                 for lst in lists)


# The briefing JSON's lists the cases stand in for.
_STRIP = ("new_needs_response", "new_dropped_threads", "deadline_items", "open_obligations",
          "stale_obligations", "resurrected_follow_ups", "reply_backlog", "today_top",
          "everything_else", "handled_by_reply")


def score(e: dict) -> tuple[int, str]:
    """A case's place in today_top, as preprocess.build_today_top scores
    a dated obligation: overdue, due within 2 days, within 7."""
    n = e.get("due_in")
    if e.get("overdue"):
        return 75, f"letter overdue ({e['overdue']} destination{'s' if e['overdue'] != 1 else ''})"
    if n is None:
        return 5, "letter with no due date"
    if n <= 2:
        return 65, f"letter due {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'}"
    if n <= 7:
        return 30, f"letter due in {n} days"
    return 5, f"letter due in {n} days (not urgent yet)"


def apply_briefing(briefing: dict, view: dict, *, cap: int = 5) -> dict:
    """The briefing JSON with the cases in it: `letter_cases` (one entry
    per open case), `letter_cases_finished` (the cases whose last
    destination was submitted since yesterday), the rows and threads the
    cases stand for taken out of every list, and the cases due soon
    ranked into today_top (ref `letter:<case id>`)."""
    if not view:
        return briefing
    hide = view.get("hide") or {}
    for key in _STRIP:
        if isinstance(briefing.get(key), list):
            briefing[key] = [x for x in briefing[key] if not (isinstance(x, dict) and _hidden(x, hide))]
    top = list(briefing.get("today_top") or [])
    for e in view.get("cases") or []:
        s, why = score(e)
        if s >= 30:
            top.append({"source": "letter", "title": e["line"][:100], "ref": f"letter:{e['case']}",
                        "counterparty": e.get("person") or "", "score": s, "reason": why})
    top.sort(key=lambda t: -int(t.get("score") or 0))
    briefing["today_top"], rest = top[:cap], top[cap:]
    if rest:
        briefing["everything_else"] = [{"source": t.get("source"), "title": str(t.get("title"))[:80],
                                        "score": t.get("score")} for t in rest] \
            + list(briefing.get("everything_else") or [])
    briefing["letter_cases"] = list(view.get("cases") or [])
    briefing["letter_cases_finished"] = list(view.get("finished") or [])
    return briefing


# ── the briefing post (remote_briefing.py, remote_audit.py) ───────────

def candidate(e: dict) -> dict:
    """A briefing candidate for one `letter_cases` entry. Its line is
    code's (post_line), its date the case's nearest open due date; it has
    no row and no thread of its own, so nothing marks, drafts or closes
    through it."""
    return {"kind": "letter", "case": e.get("case") or "", "letter": e,
            "title": e.get("name") or KEPT_CASE, "who": "", "from": "", "ledger_id": "",
            "conversation_id": "", "message_id": "", "summary": e.get("line") or "",
            "due": e.get("next_due") or "", "tags": [], "ask": "", "type": "",
            "keep_level": e.get("keep_level") or "full", "owner_said": [],
            "letter_rows": list(e.get("ledger_ids") or []),
            "received": "", "subject": ""}


def is_case(c: dict) -> bool:
    return isinstance(c, dict) and c.get("kind") == "letter" and isinstance(c.get("letter"), dict)


def post_line(c: dict, when: str = "") -> str:
    """The case's line in a post: its name in bold, then the line after
    the name. `when` is the do-list's window ("in the 1-2:30 gap")."""
    e = c.get("letter") or {}
    text = str(e.get("line") or "")
    head, _sep, rest = text.partition(": ")
    out = f"*{head}*: {rest}" if rest else f"*{text}*"
    if when:
        out += f" Work on it {_clean(when, 60)}."
    return out


def letters_section(cands: dict, shown: set) -> list[tuple[str, str]]:
    """The post's Letters list: every case no other section names,
    nearest due date first, a case with no date last."""
    refs = [r for r, c in cands.items() if is_case(c) and r not in shown]
    refs.sort(key=lambda r: (cands[r].get("due") or "9999-99-99", cands[r].get("title") or ""))
    return [(r, post_line(cands[r])) for r in refs]


def finished_words(view_finished: list[dict]) -> str:
    """The Quietly handled clause for the cases that finished: "1 letter
    finished: all its destinations are submitted (Recommendation letter
    for Dana Reyes)"."""
    if not view_finished:
        return ""
    names = "; ".join(_clean(f.get("name"), 60) for f in view_finished[:3])
    n = len(view_finished)
    return (f"{n} letter{'s' if n != 1 else ''} finished: all "
            f"{'its' if n == 1 else 'their'} destinations are submitted ({names})")


# ── reminders by date ─────────────────────────────────────────────────

def rung(due: date | None, today: date) -> int | None:
    """The ladder's rung for a due date today: days left when that is on
    LADDER, else None."""
    if not due:
        return None
    left = (due - today).days
    return left if left in LADDER else None


def reminders(cases: list[dict], today: date, sent: dict | None = None) -> list[dict]:
    """The reminders due today that have not fired: [{case, dest, rung,
    loud, due, key}], one per open destination with a dated rung today;
    `sent` is {key: when} of the rungs that fired (key
    "<dest id>:<rung>:<due>", so a moved date starts its ladder again).
    A submitted or declined destination is never reminded."""
    sent = sent or {}
    out = []
    for c in cases:
        for d in open_dests(c):
            due = _date(d.get("due"))
            r = rung(due, today)
            if r is None:
                continue
            key = f"{d.get('id')}:{r}:{due.isoformat()}"
            if key in sent:
                continue
            out.append({"case": str(c.get("id") or ""), "dest": str(d.get("id") or ""), "rung": r,
                        "loud": r in LOUD, "due": due.isoformat(), "key": key})
    return out


def reminder_text(case: dict, items: list[dict], today: date, lv: str) -> str:
    """The loud reminder's first line (the push and the card's title):
    "Letter for Dana Reyes: 2 destinations due tomorrow (North
    University, Coastal Institute)."."""
    who = person(case, lv)
    head = f"Letter for {who}" if who else KEPT_CASE
    by_id = {str(d.get("id")): d for d in dests(case)}
    named = [by_id[i["dest"]] for i in items if i["dest"] in by_id]
    due = _date(items[0]["due"]) if items else None
    when = ("was due " + day_words(due, today)) if due and due < today else ("due " + day_words(due, today))
    names = ", ".join(_clean(d.get("name"), 50) for d in named[:3]) if lv in ("full", "summary") else ""
    return f"{head}: {_n(len(named), 'destination')} {when}" + (f" ({names})." if names else ".")


# ── the cards in Action requests (ea-chat-bot.py) ─────────────────────
# Card specs as plain data; the bot turns one into a ConfirmationCard.
# Every value names the case and its open rows as `rows` (web_requests
# reads them for the project and the urgency edge); never `ledger_id`,
# which the web chat's Mark done would close.

def _value(case: dict, **extra) -> dict:
    return {"case": str(case.get("id") or ""), "rows": ledger_ids(case), **extra}


def _dest_line(d: dict, today: date, lv: str, n: int) -> str:
    nm = _clean(d.get("name"), 80) if lv in ("full", "summary") else f"Destination {n}"
    due = _date(d.get("due"))
    when = ("was due " + day_words(due, today)) if due and due < today \
        else ("due " + day_words(due, today)) if due else "no due date"
    how = METHOD_WORDS.get(str(d.get("method") or ""), "")
    state = STATE_WORDS.get(d.get("state") or "asked", d.get("state") or "")
    return f"{nm}: {when}, {state}" + (f", {how}" if how and lv in ("full", "summary") else "")


def _materials(case: dict, lv: str) -> str:
    """What the request included, by file name where the level lets a
    reader see it: "The request included: cv.pdf, statement.pdf."."""
    names = [str(a.get("name") or "") for a in case.get("request_attachments") or []
             if isinstance(a, dict) and a.get("name")]
    if not names:
        # The store keeps what the person sent by kind (letters.add_material).
        names = list(dict.fromkeys(MATERIAL_WORDS.get(str(m.get("kind") or ""), str(m.get("kind") or ""))
                                   for m in case.get("materials") or []
                                   if isinstance(m, dict) and m.get("kind")))
    names = [n for n in names if n]
    if not names:
        return "The request named no materials."
    if lv == "local":
        return f"The request included {_n(len(names), 'file')}."
    return "The request included: " + ", ".join(_clean(n, 60) for n in names[:6]) \
        + (f", and {len(names) - 6} more" if len(names) > 6 else "") + "."


def _links(case: dict, lv: str) -> list[str]:
    if lv not in ("full", "summary"):
        return []
    out = []
    for d in open_dests(case):
        url = str(d.get("link") or "")
        if url.startswith("https://") and not any(ch in url for ch in " <>|"):
            out.append(f"<{url}|Open the portal for {_clean(d.get('name'), 60)}>")
    return out[:CARD_DESTS]


def decision_spec(case: dict, today: date, lv: str = "full") -> dict:
    """The new-case card: Will you write it? Yes / Yes, ask for materials /
    No, with what the request included and the dates. No reaction answers
    it (`no_default`, no danger style on No)."""
    f = facts(case, today)
    nm = name(case, lv)
    lines = [f"{nm}." if nm != KEPT_CASE else f"{KEPT_CASE}: a new request."]
    shown = open_dests(case)[:CARD_DESTS]
    lines += [f"{i}. {_dest_line(d, today, lv, i)}" for i, d in enumerate(shown, 1)]
    if f["total"] > len(shown):
        lines.append(f"And {f['total'] - len(shown)} more.")
    if not shown:
        lines.append("It names no places to send it yet.")
    lines.append(_materials(case, lv))
    q = next(iter(questions(case, "write")), None)
    value = _value(case, **({"question": str(q["id"])} if q and q.get("id") else {}))
    return {"kind": "letter_ask", "title": ":memo: *A letter request.* Will you write it?",
            "lines": lines,
            "fields": [("From", person(case, lv) or "A kept message"), ("Subject", nm),
                       ("When", ("Next due " + day_words(f["next_due"], today)) if f["next_due"]
                        else "No due date yet")],
            "actions": [("letter_yes", ":white_check_mark: Yes", value, "primary", "yes", ""),
                        ("letter_yes_ask", ":page_facing_up: Yes, ask for materials", value, "",
                         "ask", ""),
                        ("letter_no", ":no_entry_sign: No", value, "", "no", "")],
            "no_default": True, "block_id": f"letter_ask_{value['case']}"}


def case_spec(case: dict, today: date, lv: str = "full", *, reminder: str = "") -> dict:
    """The case block: the person, the kind, each open destination with
    its date and state, what is missing, a link line per portal, and the
    actions that make sense now: Draft the letter (Change the letter
    once a draft exists), Submitted per destination, Set the due date,
    Decline. `reminder` opens the title when a loud rung posts it."""
    f = facts(case, today)
    nm = name(case, lv)
    head = f":memo: *{nm}*: {f['submitted']} of {f['total'] - f['declined']} submitted."
    title = (f":alarm_clock: *{_clean(reminder, 200)}*\n" if reminder else "") + head
    shown = f["open"][:CARD_DESTS]
    lines = [f"{i}. {_dest_line(d, today, lv, i)}" for i, d in enumerate(shown, 1)]
    if len(f["open"]) > len(shown):
        lines.append(f"And {len(f['open']) - len(shown)} more to do.")
    missing = []
    if not case.get("base_letter") and not any(d.get("state") in ("drafted", "ready") for d in f["open"]):
        missing.append("the letter")
    if f["undated"]:
        missing.append(f"the due date for {_n(len(f['undated']), 'destination')}")
    if not (case.get("materials") or case.get("request_attachments")):
        missing.append("the materials (CV, statement)")
    if missing:
        lines.append("Missing: " + ", ".join(missing) + ".")
    lines += _links(case, lv)
    value = _value(case)
    drafted = bool(case.get("base_letter")) or any(d.get("state") in ("drafted", "ready")
                                                   for d in f["open"])
    actions = [("letter_draft", ":pencil2: Change the letter" if drafted else ":pencil2: Draft the letter",
                value, "primary", "draft", "what to say in it")]
    picks = f["open"][:CARD_BUTTONS]
    for d in picks:
        nm_d = _clean(d.get("name"), 40) if lv in ("full", "summary") else \
            f"destination {f['open'].index(d) + 1}"
        actions.append(("letter_submitted", f":white_check_mark: Submitted: {nm_d}",
                        _value(case, dest=str(d.get("id") or "")), "", "", ""))
    select = None
    if len(f["open"]) > CARD_BUTTONS:
        select = ("letter_submitted", "Submitted another one",
                  [(_clean(d.get("name"), 60) if lv in ("full", "summary") else f"Destination {i}",
                    str(d.get("id") or "")) for i, d in enumerate(f["open"], 1)][CARD_BUTTONS:])
    if f["undated"]:
        extra = {"dest": str(f["undated"][0].get("id") or "")} if len(f["undated"]) == 1 else {}
        actions.append(("letter_date", ":calendar: Set the due date", _value(case, **extra), "", "date",
                        "the date, like Dec 1"))
    actions.append(("letter_decline", "Decline", value, "", "decline", ""))
    return {"kind": "letter", "title": title, "lines": lines,
            "fields": [("From", person(case, lv) or "A kept message"), ("Subject", nm),
                       ("When", ("Next due " + day_words(f["next_due"], today)) if f["next_due"]
                        else "No due date yet")],
            "actions": actions, "select": select, "no_default": True,
            "block_id": f"letter_{value['case']}_{today.isoformat()}"}


def join_spec(case: dict, q: dict, lv: str = "full") -> dict:
    """A `join` question (letters.ask_join): is the person of a new mail
    the person of an open case? Same person / Someone else, asked once;
    no reaction answers it. Kept mail gets fixed words."""
    text = _clean(q.get("text"), 300) if lv in ("full", "summary") else \
        "Two letter requests may be from the same person."
    value = _value(case, question=str(q.get("id") or ""))
    return {"kind": "letter_ask", "title": ":memo: *Is this the same person?*",
            "lines": [text, "Your answer is kept, so I do not ask again."],
            "fields": [("Subject", name(case, lv))],
            "actions": [("letter_join_same", ":link: Same person", value, "", "same", ""),
                        ("letter_join_other", "Someone else", value, "", "other", "")],
            "no_default": True, "block_id": f"letter_join_{q.get('id')}"}


def card_plan(cases: list[dict], cards: dict, today: date, sent: dict | None = None) -> list[dict]:
    """What the bot posts this tick: [{case, what, reminder?, keys?}] with
    `what` "ask" (a new case with no card yet), "case" (an open case with
    an answer and no live case block) or "remind" (a loud rung fired: the
    block posts again, its old card retired), and "retire" for a card
    whose case is no longer open. `cards` is the bot's record {case id:
    {"kind", "card", "channel", "live"}}."""
    out = []
    open_ids = set()
    due = {}
    for r in reminders(cases, today, sent):
        due.setdefault(r["case"], []).append(r)
    for c in cases:
        cid = str(c.get("id") or "")
        if not cid or not is_open(c):
            continue
        open_ids.add(cid)
        joins = questions(c, "join")
        for q in joins:
            key = f"q:{q.get('id')}"
            open_ids.add(key)
            if key not in cards:
                out.append({"case": cid, "what": "join", "question": str(q.get("id") or ""), "key": key})
        if joins:
            continue  # who it is comes first: the case's own card follows the answer
        rec = cards.get(cid) or {}
        live = bool(rec.get("live"))
        todays = due.get(cid, [])
        loud = [r for r in todays if r["loud"]]
        keys = [r["key"] for r in todays]
        new = is_new(c)
        if new and not rec:
            # The one decision: asked once; a loud rung puts it up again.
            out.append({"case": cid, "what": "ask", "keys": keys})
        elif loud:
            out.append({"case": cid, "what": "remind", "kind": "ask" if new else "case",
                        "items": loud, "keys": keys})
        elif not new and (not live or rec.get("kind") != "case") and _in_window(c, today):
            out.append({"case": cid, "what": "case", "keys": keys})
        elif keys:
            out.append({"case": cid, "what": "mark", "keys": keys})
    for cid, rec in cards.items():
        if cid not in open_ids and rec.get("live"):
            out.append({"case": cid, "what": "retire"})  # a closed case, an answered question
    return out


def _in_window(case: dict, today: date) -> bool:
    """A case Action requests holds a block for: one overdue, due within
    HOME_DAYS or with an undated destination (it needs its date). A case
    further out gets its block when its date comes that close; until then
    it is on the briefing's Letters list."""
    f = facts(case, today)
    return bool(f["overdue"] or f["undated"] or not f["open"]
                or (f["next_due"] and (f["next_due"] - today).days <= HOME_DAYS))


def find(cases: list[dict], case_id: str) -> dict | None:
    return next((c for c in cases if str(c.get("id") or "") == str(case_id or "")), None)


# ── answers (the bot's handler; builder B's letters_work decides) ─────

CHOICES = ("yes", "yes_ask", "no", "draft", "submitted", "date", "decline", "join_same", "join_other")
ACTION_CHOICE = {"letter_yes": "yes", "letter_yes_ask": "yes_ask", "letter_no": "no",
                 "letter_draft": "draft", "letter_submitted": "submitted", "letter_date": "date",
                 "letter_decline": "decline", "letter_join_same": "join_same",
                 "letter_join_other": "join_other"}
# The store's own answers to its questions (letters.WRITE_CHOICES, JOIN_*).
STORE_ANSWERS = {"yes": "Yes", "yes_ask": "Yes, ask for materials", "no": "No",
                 "join_same": "Same person", "join_other": "Someone else"}


def _turn_prompt(case: dict, choice: str, dest: str, text: str) -> str:
    """The fallback turn's prompt when letters_work is absent: the case
    by id, the owner's choice and words; the EA reads the store and the
    letters guide in docs/ea itself."""
    cid = str(case.get("id") or "")
    said = f' The owner added: "{_clean(text, 300)}".' if text else ""
    what = {
        "yes": "The owner will write this letter.",
        "yes_ask": ("The owner will write this letter and wants the materials from the person. Ask "
                    "only for what the request did not already include, and name what it included."),
        "draft": "The owner asked for the letter's draft. Draft it from the materials.",
        "date": ("The owner gives the due date for the destination without one. With no date in "
                 "the owner's words, ask for it in one line."),
    }.get(choice, "")
    dest_s = f" Destination id: {dest}." if dest else ""
    return (f"Letter case {cid} (python3 /home/ea-bot/ea-system/preprocessor/letters.py get --case {cid}). "
            f"{what}{dest_s}{said} Follow the letters guide (docs/ea/letters.md; docs/ea/lor.md "
            "on an older install). A letter's text never goes in a channel post.")


def answer(case: dict, choice: str, *, dest: str = "", text: str = "", question: str = "",
           runner=None) -> dict:
    """One answer on a letter card: {"ok", "said", "turn"?}. A `join`
    answer is the store's own (letters.answer). Any other goes to
    letters_work.answer when builder B's module is there; else the
    store's verbs do what needs no model (Yes, No and Decline record the
    owner's decision and send nothing; a submission is recorded), and one
    EA turn does the rest (the materials request, the draft, the date).
    Never a close on a reply, never a decline drafted on its own."""
    if choice not in CHOICES:
        return {"ok": False, "said": "That choice is not known. Nothing was done."}
    cid = str(case.get("id") or "")
    run = runner or _cli

    def done(out: dict, yes: str) -> dict:
        ok = bool(out) and not out.get("error")
        return {"ok": ok, "said": yes if ok else "I could not record your answer. Ask me in Ask Tacitus."}
    if choice in ("join_same", "join_other"):
        if not question:
            return {"ok": False, "said": "This question changed since the card. Nothing was done."}
        return done(run(["answer", "--question", question, "--choice", STORE_ANSWERS[choice]]),
                    "Recorded: one letter case now." if choice == "join_same"
                    else "Recorded: two people, two letter cases.")
    try:
        import letters_work  # type: ignore
    except Exception:
        letters_work = None
    if letters_work is not None and hasattr(letters_work, "answer"):
        got = letters_work.answer(cid, choice, dest=dest, text=text, actor="owner")
        return got if isinstance(got, dict) else {"ok": False, "said": "Nothing was done."}
    if choice in ("yes", "yes_ask", "no"):
        argv = (["answer", "--question", question, "--choice", STORE_ANSWERS[choice]] if question
                else ["decline" if choice == "no" else "accept", "--case", cid])
        got = done(run(argv), {"yes": "You will write it. It is on your list with its dates.",
                               "yes_ask": "I am writing the request for the materials.",
                               "no": "You will not write it. Nothing is sent."}[choice])
        if got["ok"] and choice == "yes_ask":
            got["turn"] = _turn_prompt(case, choice, dest, text)
        return got
    if choice == "decline":
        return done(run(["decline", "--case", cid]), "Declined. Nothing is sent.")
    if choice == "submitted":
        if not dest:
            return {"ok": False, "said": "Pick the destination you submitted."}
        return done(run(["record-submitted", "--dest", dest, "--evidence",
                         "the owner said so in Action requests"]), "Marked as submitted.")
    words = {"draft": "I am writing the letter's draft.", "date": "I am recording the date."}[choice]
    return {"ok": True, "said": words, "turn": _turn_prompt(case, choice, dest, text)}


def _cli(argv: list[str]) -> dict:
    """One letters.py verb (the module's own file, beside this one)."""
    import subprocess
    mod = _letters()
    path = Path(getattr(mod, "__file__", "") or HERE / "letters.py")
    r = subprocess.run([sys.executable, str(path), *argv], capture_output=True, text=True, timeout=60)
    out = r.stdout or ""
    try:
        got = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        got = {}
    if r.returncode and not got.get("error"):
        got["error"] = (r.stderr or "failed")[-200:]
    return got


# ── Home (home_screen.py) ─────────────────────────────────────────────

def _home_due(d: date | None, zone) -> float | None:
    if not d:
        return None
    return datetime.combine(d, datetime.max.time().replace(microsecond=0), zone).timestamp()


def home_items(src, cases=None, *, rows: dict | None = None) -> tuple[list[dict], set]:
    """(Home's items for the open cases, the request ids they stand for).
    One item per case, kind `letter`: a new case (its decision card), a
    case overdue, due within HOME_DAYS or with no date. A case's waiting
    card in Action requests is the item's `href` and leaves the list as a
    request of its own."""
    cases = load_cases() if cases is None else cases
    if not cases:
        return [], set()
    zone = src.zone
    now = float(src.now)
    today = datetime.fromtimestamp(now, zone).date()
    cards: dict[str, dict] = {}
    for r in (src.requests or {}).get("waiting") or []:
        if not isinstance(r, dict):
            continue
        card = r.get("card") if isinstance(r.get("card"), dict) else {}
        if card.get("kind") not in ("letter", "letter_ask"):
            continue
        for a in card.get("actions") or []:
            v = a.get("value") if isinstance(a, dict) else None
            if isinstance(v, str) and v.startswith("{"):
                try:
                    v = json.loads(v)
                except ValueError:
                    v = None
            if isinstance(v, dict) and v.get("case"):
                cards.setdefault(str(v["case"]), r)
                break
    rows = rows if rows is not None else (src.rows or {})
    out, taken = [], set()
    for c in cases:
        cid = str(c.get("id") or "")
        f = facts(c, today)
        if not cid or not is_open(c):
            continue
        n = (f["next_due"] - today).days if f["next_due"] else None
        card = cards.get(cid)
        if not (f["new"] or f["overdue"] or f["undated"] or card
                or (n is not None and n <= HOME_DAYS)):
            continue
        lv = level(c, rows, records=False)
        due = _home_due(min([_date(d.get("due")) for d in f["overdue"]] or [f["next_due"]])
                        if (f["overdue"] or f["next_due"]) else None, zone)
        urgency = "overdue" if f["overdue"] else ("soon" if due and due - now < 2 * 86400 else "calm")
        nm = name(c, lv)
        title = f"Will you write it? {nm}." if f["new"] else nm
        full = line(c, today, lv=lv)
        detail = full.partition(": ")[2] or full
        item = {"id": f"letter:{cid}", "kind": "letter", "label": "Letter request" if f["new"] else "Letter",
                "title": title, "detail": detail, "urgency": urgency, "due": due,
                "fire": urgency == "overdue" or (due is not None and due - now < 86400),
                "mins": HOME_MINUTES, "_case": cid}
        if _kept(lv):
            item["kept"] = lv
        else:
            item["ask"] = f'About the {nm[0].lower() + nm[1:]}: '
        if card is not None:
            from urllib.parse import quote
            item["href"] = (f"#/{quote(str(card.get('channel') or src.dm), safe='')}/"
                            f"{quote(str(card.get('thread_key') or ''), safe='')}/"
                            f"{quote(str(card.get('id') or ''), safe='')}")
            item["_rid"] = str(card.get("id") or "")
            taken.add(f"request:{card.get('id')}")
        out.append(item)
    return out, taken


def home_left(out: list[dict], src, *, cases=None) -> list[dict]:
    """Home's hook: `out` with one item per open case in place of the
    case's rows, threads and cards (home_screen.left). An item carries
    `_case` (and `_rid` for its card) for Home's item and act; no ledger
    id goes to the app."""
    cases = load_cases() if cases is None else cases
    try:
        items, taken = home_items(src, cases)
    except Exception as e:
        print(f"letter_surface: no letter items on Home: {str(e)[:160]}", file=sys.stderr)
        return out
    if not items:
        return out
    shown = {it["_case"] for it in items}
    ids = {i for c in (cases if cases is not None else load_cases())
           if str(c.get("id") or "") in shown for i in ledger_ids(c, open_only=False)}
    hide = {"ids": sorted(ids), "convs": [], "mids": []}
    rows = getattr(src, "rows", None) or {}
    convs = {str(rows[i].get("conversation_id")) for i in ids if i in rows and rows[i].get("conversation_id")}
    hide["convs"] = sorted(convs)
    kept = []
    for x in out:
        lid = str(x.get("_lid") or "") or (str(x.get("id") or "").split(":", 1)[1]
                                           if ":" in str(x.get("id") or "") else "")
        if x.get("id") in taken or lid in ids or (x.get("_conv") and x.get("_conv") in convs) \
                or str(x.get("id") or "").startswith("item:letter:"):
            continue  # the case's rows, threads, cards and its today_top entry
        kept.append(x)
    return kept + items


def home_brief(case: dict, today: date, lv: str = "full") -> dict:
    """The quick briefing for a letter item (the Home cards contract's
    five fields), code-built: what it is, what was asked and by when,
    where it stands, what Tacitus would do next."""
    f = facts(case, today)
    nm = name(case, lv)
    standing = []
    if f["new"]:
        standing.append("You have not answered yet.")
    else:
        standing.append(f"{f['submitted']} of {f['total'] - f['declined']} destinations submitted.")
    if f["overdue"]:
        standing.append(f"{_n(len(f['overdue']), 'destination')} past its due date.")
    if f["undated"]:
        standing.append(f"{_n(len(f['undated']), 'destination')} with no due date.")
    drafted = bool(case.get("base_letter")) or any(d.get("state") in ("drafted", "ready") for d in f["open"])
    if f["new"]:
        nxt = "Tacitus waits for your answer: will you write it?"
    elif f["undated"] and not f["next_due"]:
        nxt = "Tacitus needs the due date."
    elif not drafted:
        nxt = "Tacitus can draft the letter from the materials."
    else:
        nxt = "Tacitus can help you submit it to the next destination."
    asked = (f"Write and submit it to {_n(f['total'], 'destination')}." if f["total"]
             else "Write it. No places to send it are named yet.")
    return {"what": f"{nm}.", "asked": asked,
            "due": (f"Next due {day_words(f['next_due'], today)}." if f["next_due"] else "No due date yet."),
            "standing": standing[:4], "next": nxt}


def home_choices(case: dict, today: date, lv: str = "full") -> list[dict]:
    """The choices for a letter item, in the Home cards contract's shapes:
    the new case's three answers (the card's own), or for an open case
    Draft the letter (a line of the owner's words), Submitted per
    destination (the CARD_BUTTONS nearest; the card's menu has the rest),
    Set the due date (a line) and Open it in Action requests. A choice
    `letter:<choice>[:<dest id>]` runs letter_surface.answer as a press
    on the card does. No Done, Not needed or Later: a case closes only on
    its submissions, and its dates move its reminders."""
    f = facts(case, today)
    if f["new"]:
        return [{"id": "letter:yes", "label": "Yes, I will write it", "style": "primary"},
                {"id": "letter:yes_ask", "label": "Yes, ask for materials"},
                {"id": "letter:no", "label": "No"}]
    drafted = bool(case.get("base_letter")) or any(d.get("state") in ("drafted", "ready") for d in f["open"])
    out = [{"id": "letter:draft", "label": "Change the letter" if drafted else "Draft the letter",
            "style": "primary", "input": "text"}]
    for i, d in enumerate(f["open"][:CARD_BUTTONS], 1):
        nm = _clean(d.get("name"), 40) if lv in ("full", "summary") else f"destination {i}"
        out.append({"id": f"letter:submitted:{d.get('id')}", "label": f"Submitted: {nm}"})
    if f["undated"]:
        out.append({"id": "letter:date", "label": "Set the due date", "input": "text"})
    out.append({"id": "open", "label": "Open it in Action requests"})
    return out


def home_choice(choice_id: str) -> tuple[str, str]:
    """(answer, destination id) for a Home choice id `letter:<choice>[:<dest
    id>]`; ("", "") for any other id."""
    head, _sep, rest = str(choice_id or "").partition(":")
    if head != "letter":
        return "", ""
    choice, _sep, dest = rest.partition(":")
    return (choice, dest) if choice in CHOICES else ("", "")


# ── the pre-send review (briefing_review.py) ──────────────────────────

def review_record(c: dict) -> dict | None:
    """What the reviewer reads for a case item: the person (at its level),
    the kind and each open destination with its date and state, and the
    rule: a reply in a thread is no submission."""
    if not is_case(c):
        return None
    e = c["letter"]
    return {"case": e.get("case") or "", "kind": e.get("kind") or "",
            "submitted": e.get("submitted") or 0, "total": e.get("total") or 0,
            "destinations": [{k: v for k, v in d.items() if v} for d in e.get("destinations") or []],
            "rule": "One letter the owner writes. It is done only when every destination is submitted. "
                    "A reply in its thread is not a submission."}


def review_refusal(rec: dict, action: str, kind: str = "") -> str:
    """Why the reviewer may not take this action on a case item, or "":
    a case with an open destination is never done, someone else's, a
    duplicate or only an event, and its line is code's."""
    case = (rec or {}).get("letter_case")
    if not isinstance(case, dict):
        return ""
    if action == "drop":
        return "a letter case leaves the post only when every destination is submitted"
    if action == "rewrite-line":
        return "a letter case's line is code's own"
    return ""


# ── a project room's name (briefing_rooms.py) ─────────────────────────

def room_title(c: dict) -> str:
    """A case's title in a room post and the room's list of threads: its
    person and kind, or the fixed words for kept mail."""
    if not is_case(c):
        return ""
    return str(c["letter"].get("name") or KEPT_CASE)


def collapse_rows(rows: list[dict], cases=None, *, today: date | None = None,
                  open_only: bool = True) -> list[dict]:
    """`rows` with the destinations of one case as one row: the first
    destination row of each case stands for it, named by the case
    (briefing_rooms' Also open list, the weekly audit's Completed list;
    `open_only` False reads the finished cases too)."""
    if not any(is_letter_row(r) for r in rows if isinstance(r, dict)):
        return rows
    today = today or today_local()
    cases = load_cases(open_only=open_only) if cases is None else cases
    by_case = {}
    for c in cases:
        for i in ledger_ids(c, open_only=False):
            by_case[i] = c
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict) or not is_letter_row(r):
            out.append(r)
            continue
        rec = r.get("letter") if isinstance(r.get("letter"), dict) else _meta(r).get("letter") or {}
        c = by_case.get(str(r.get("id"))) or find(cases, str(rec.get("case") or ""))
        key = str((c or {}).get("id") or rec.get("case") or r.get("id"))
        if key in seen:
            continue
        seen.add(key)
        if c is None:
            out.append(r)
            continue
        lv = level(c, {str(r.get("id")): r}, records=False)
        out.append(dict(r, description=f"{name(c, lv)}: {line(c, today, lv=lv).partition(': ')[2]}"))
    return out
