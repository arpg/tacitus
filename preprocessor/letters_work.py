#!/usr/bin/env python3
"""letters_work: the work on a letter case once it exists (docs/ea/letters.md).

A letter case (preprocessor/letters.py) is one person the owner was asked
to write for: a recommendation, reference, nomination, support or
character letter, for a student, an employee, a colleague, a tenant or a
client. It has destinations (a program, an employer, an award, a portal),
each with its own due date, its submission method and a state: asked,
accepted or declined, drafted, ready, submitted. letters.py keeps the
store; this tool is the steps the EA takes on a case:

    letters_work.py status   --case C
        the case: each destination's state and next step, what is
        missing (materials, dates, methods), open questions, the tiers,
        and the one decision to post when nobody has answered yet
    letters_work.py decide   --case C --answer yes|materials|no --owner-said "<words>"
                             [--dest D] [--want cv,statement] [--reply] [--no-reply]
        the owner's answer to "Will you write it?". yes: accepted, no
        email unless --reply. materials: accepted, and a reply brief that
        asks only for what the request did not already include. no: the
        destinations are declined and a decline reply brief is written
        (the owner sends it with the Send button; nothing sends here).
        Every answer needs the owner's words: the EA never decides a case.
    letters_work.py collect  --case C [--message-id M ...] [--provider outlook|gmail] [--account A]
        save the request's attachments into the case folder as materials
    letters_work.py extend   --case C --dest D
        a destination that arrived after the owner said yes joins the case
    letters_work.py require  --case C --dest D [--length L] [--format F] [--addressee A]
                             [--program P] [--organization O]
                             [--question "<text>" [--options "a|b|c"] [--rating]]
        what a destination asks for: length, form, names, form questions
    letters_work.py answered --case C --dest D --question Q --value V --owner-said "<words>"
        the owner's answer to a form or rating question
    letters_work.py brief    --case C [--dest D] [--notes-file F] [--owner-said "<words>"]
        the brief for the email-author subagent, written into the case folder
    letters_work.py check    --case C --file F [--dest D] [--record]
        questions left in a draft, placeholders, another destination's
        name; --record records the draft when nothing leaks
    letters_work.py variants --case C --base F [--base-dest D]
        one copy per destination, only where a name must change
    letters_work.py render   --case C --file F [--dest D] [--docx] [--template T]
        PDF on the owner's letterhead when one is set up, plain otherwise
    letters_work.py approve  --case C [--dest D] --owner-said "<words>"
        the owner approved the text: the destinations become ready
    letters_work.py submit   --case C --dest D
        how this destination's letter goes out, step by step

Recording a submission is `letters.py record-submitted --dest D
--evidence "..."` (the store's own verb).

The bot's Action requests buttons call answer() from the owner's process:
it reads and writes the store only, never a file in the resident's
space, and returns the line for the card and, when work follows, the
prompt for one EA turn.

A letter's text is the owner's evaluation of a person and stays in the
case folder (~/.ea/letters/<case>/): no output here carries it. Output is
one JSON object on stdout; a refusal is {"error": ...} with exit 2.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
SCRIPT_DIR = Path(__file__).resolve().parent
PY = "python3"

STATES = ("asked", "accepted", "declined", "drafted", "ready", "submitted")
OPEN_STATES = ("asked", "accepted", "drafted", "ready")
TERMINAL = ("declined", "submitted")
ANSWERS = ("yes", "materials", "no")
CHOICES = ("yes", "yes_ask", "yes_date", "no", "draft", "submitted", "all", "date", "decline")

DRAFT_TASK = "draft_letter"
UPLOAD_TASK = "upload_letter_of_recommendation"
DEFAULT_TIERS = {DRAFT_TASK: "ask-first", UPLOAD_TASK: "do-and-tell"}

MATERIALS_DIR = "materials"
REQUIREMENTS_FILE = "requirements.json"
CASE_MARK = "case.json"
EXCERPT_CHARS = 6000
EXCERPTS_TOTAL = 24000
EARLIER_LETTERS = 3

# ── words ─────────────────────────────────────────────────────────────

KIND_WORDS = {
    "recommendation": "recommendation letter",
    "reference": "reference letter",
    "nomination": "nomination letter",
    "support": "support letter",
    "character": "character reference",
}
SUBJECT_WORDS = {
    "recommendation": "Letter of recommendation",
    "reference": "Reference letter",
    "nomination": "Nomination letter",
    "support": "Letter of support",
    "character": "Character reference",
}

# Material kinds, each with the words of a file name that mark it and the
# plain words for asking for it.
MATERIAL_WORDS = {
    "cv": "a CV or resume",
    "statement": "the statement they will send with the application",
    "description": "the description of the position, program or award",
    "list": "the list of places, with each due date and how each letter is submitted",
    "transcript": "a transcript",
    "draft": "a list of points the letter should cover",
    "form": "the form that goes with the letter",
}
MATERIAL_NAMES = {
    "cv": "the CV", "statement": "the statement", "description": "the description",
    "list": "the list of places", "transcript": "the transcript",
    "draft": "the points for the letter", "form": "the form", "other": "a file",
}
_NAME_KINDS = (
    ("cv", r"cv|resume|r[eé]sum[eé]|vita|curriculum"),
    ("transcript", r"transcripts?|grades|marksheet"),
    ("statement", r"statements?|sop|essay|purpose|personal|motivation|cover"),
    ("list", r"list|programs?|schools|universities|destinations|places|positions|deadlines"),
    ("draft", r"draft|points|talking|brag|highlights|accomplishments"),
    ("description", r"description|posting|job|call|announcement|advert|ad"),
    ("form", r"form|waiver|release|consent"),
)
# The materials a case wants when the owner names none, by kind.
WANTED = {
    "recommendation": ("cv", "statement", "list"),
    "reference": ("cv", "description", "list"),
    "nomination": ("cv", "description", "list"),
    "support": ("description", "list"),
    "character": ("description", "list"),
}
_WANT_ALIASES = {
    "cv": "cv", "resume": "cv", "vita": "cv", "c.v.": "cv",
    "statement": "statement", "sop": "statement", "essay": "statement",
    "personal statement": "statement", "statement of purpose": "statement",
    "transcript": "transcript", "transcripts": "transcript", "grades": "transcript",
    "list": "list", "programs": "list", "schools": "list", "places": "list",
    "deadlines": "list", "destinations": "list", "dates": "list",
    "description": "description", "job description": "description", "posting": "description",
    "call": "description", "announcement": "description",
    "points": "draft", "draft": "draft", "talking points": "draft", "brag sheet": "draft",
    "form": "form",
}

# A question the drafter could not answer from what it was given, left in
# the text: [ASK: ...], and the older [VERIFY] mark of compose-letter.
_ASK_RE = re.compile(r"\[(?:ASK|QUESTION|VERIFY)\s*:?\s*([^\]]*)\]", re.I)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
PLACEHOLDERS = ("program", "destination", "addressee", "organization")
_DASHES = ("\u2014", "\u2013")
_CLOSING_RE = re.compile(r"^(sincerely|best( regards)?|regards|respectfully|yours( truly| sincerely)?"
                         r"|with (best|kind|warm) regards|warm regards|kind regards|cordially)\b.*,?\s*$", re.I)
_SALUTATION_RE = re.compile(r"^(dear|to)\b.*[,:]?\s*$", re.I)


class LetterError(Exception):
    pass


def _io():
    try:
        import resident_io  # noqa: PLC0415
    except ModuleNotFoundError:
        from preprocessor import resident_io  # noqa: PLC0415
    return resident_io


def write_text(path: Path, text: str) -> None:
    """A file in the EA's own folders, through resident_io (atomic; a link
    at the name is replaced, never followed)."""
    io = _io()
    if not io.make_dir(Path(path).parent) or not io.replace_text(path, text):
        raise LetterError(f"could not write {path}")


def make_dir(path: Path) -> Path:
    if not _io().make_dir(path):
        raise LetterError(f"could not make {path}")
    return Path(path)


def _today() -> date:
    return date.today()


def kind_words(kind: str) -> str:
    kind = (kind or "").strip().lower()
    return KIND_WORDS.get(kind, f"{kind} letter" if kind else "letter")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48].strip("-") or "letter"


def _due_words(due: str) -> str:
    if not due:
        return "no date"
    try:
        d = date.fromisoformat(str(due)[:10])
    except ValueError:
        return str(due)
    return f"{d:%b} {d.day}"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def material_kind(name: str) -> str:
    """The kind of material a file name marks: cv, transcript, statement,
    destinations, draft, description, form, else other."""
    stem = Path(name or "").stem.lower()
    words = set(re.split(r"[^a-z0-9éè]+", stem)) - {""}
    joined = " ".join(sorted(words))
    for kind, pattern in _NAME_KINDS:
        if any(re.fullmatch(pattern, w) for w in words) or re.search(rf"\b(?:{pattern})\b", joined):
            return kind
    return "other"


def parse_want(raw: str) -> list[str]:
    """The owner's list ("cv, statement and transcript") as material kinds;
    a word with no kind stays as written."""
    out: list[str] = []
    for part in re.split(r"\s*(?:,|;|/|\band\b|\+)\s*", (raw or "").strip().lower()):
        part = re.sub(r"^(?:a|an|the|their|your|his|her|my|updated|current)\s+", "", part.strip(" ."))
        if not part:
            continue
        kind = _WANT_ALIASES.get(part) or _WANT_ALIASES.get(part.rstrip("s")) or part
        if kind not in out:
            out.append(kind)
    return out


# ── the store ─────────────────────────────────────────────────────────

def _letters_module():
    try:
        import letters as mod  # noqa: PLC0415
    except ModuleNotFoundError:
        from preprocessor import letters as mod  # noqa: PLC0415
    return mod


class Store:
    """The calls this tool makes on the letters store (letters.py). Tests
    hand in their own object with the same methods."""

    def __init__(self, conn, mod):
        self.conn, self.mod = conn, mod

    def case(self, case_id):
        return self.mod.case(self.conn, case_id)

    def set_state(self, dest_id, state, evidence, actor):
        out = self.mod.set_state(self.conn, dest_id, state, evidence, actor)
        self.conn.commit()
        return out

    def add_material(self, case_id, kind, ref):
        out = self.mod.add_material(self.conn, case_id, kind, ref)
        self.conn.commit()
        return out

    def accept(self, case_id, actor, quote):
        out = self.mod.accept(self.conn, case_id, actor=actor, quote=quote)
        self.conn.commit()
        return out

    def decline(self, case_id, actor, quote):
        out = self.mod.decline(self.conn, case_id, actor=actor, quote=quote)
        self.conn.commit()
        return out

    def record_draft(self, case_id, path, dest_id=""):
        out = self.mod.record_draft(self.conn, case_id, path, dest_id, actor="ea")
        self.conn.commit()
        return out

    def set_due(self, dest_id, due) -> bool:
        """The destination's due date: letters.set_due when the store has
        one; else, for a destination with no date yet, add_destination
        with its own name and link, which fills an empty date."""
        fn = getattr(self.mod, "set_due", None)
        if fn is not None:
            fn(self.conn, dest_id, due)
            self.conn.commit()
            return True
        d = self.mod.destination(self.conn, dest_id) if hasattr(self.mod, "destination") else None
        if not d or d.get("due"):
            return False
        self.mod.add_destination(self.conn, d["case"], d.get("name") or "", due=due,
                                 link=d.get("link") or "", method=d.get("method") or "unknown")
        self.conn.commit()
        return True

    def set_date(self, case_id, due) -> bool:
        """The case's one date (letters.set_date); False on a store
        without it."""
        fn = getattr(self.mod, "set_date", None)
        if fn is None:
            return False
        fn(self.conn, case_id, due, actor="owner")
        self.conn.commit()
        return True

    def view(self, case):
        fn = getattr(self.mod, "view", None)
        return fn(case) if fn is not None else case


def open_store() -> Store:
    """The store on the ledger database. letters.connect() when the module
    has one; else the ledger's own guarded connection, which in an owner
    process checks the database file before it opens it."""
    mod = _letters_module()
    connect = getattr(mod, "connect", None)
    if connect is not None:
        conn = connect()
    else:
        import ledger  # noqa: PLC0415
        conn = ledger.guarded_connect(ledger.DB_PATH)
        conn.row_factory = sqlite3.Row
    return Store(conn, mod)


def _ledger_row(ledger_id: str) -> dict:
    """The destination's ledger row (its source message and thread), or {}."""
    if not ledger_id:
        return {}
    try:
        import ledger  # noqa: PLC0415
        return ledger.get_entry(ledger_id) or {}
    except Exception:
        return {}


def _case(store, case_id) -> dict:
    case = store.case(case_id)
    if not case:
        raise LetterError(f"no letter case {case_id!r}: letters.py list shows the open ones")
    return case


def _dests(case: dict, dest: str = "", states=None) -> list[dict]:
    out = []
    for d in case.get("destinations") or []:
        if dest and str(d.get("id")) != str(dest):
            continue
        if states is not None and (d.get("state") or "asked") not in states:
            continue
        out.append(d)
    if dest and not out and not any(str(d.get("id")) == str(dest) for d in case.get("destinations") or []):
        raise LetterError(f"case {case.get('id')} has no destination {dest!r}")
    return out


def _person(case: dict) -> str:
    if kept(case):
        return "this person"
    return (case.get("person_name") or "").strip() or (case.get("person_email") or "").strip() or "this person"


def _next_due(case: dict) -> tuple[str, list[dict]]:
    """(the nearest due date among the open destinations, those due then).
    Sorted by the date alone: two destinations due the same day must never
    compare their dicts."""
    dated = sorted(((str(d.get("due"))[:10], d) for d in _dests(case, states=OPEN_STATES)
                    if d.get("due")), key=lambda x: x[0])
    if not dated:
        return "", []
    first = dated[0][0]
    return first, [d for due, d in dated if due == first]


def _accepted(case: dict) -> bool:
    return case.get("decision") == "accepted" or any(
        (d.get("state") or "") in ("accepted", "drafted", "ready", "submitted")
        for d in case.get("destinations") or [])


KEPT_LEVELS = ("local", "facts")


def kept(case: dict) -> bool:
    """The case's mail is kept from hosted models (letters.py keep_level):
    its names, files and text stay on the owner's own model."""
    return (case.get("keep_level") or "full") in KEPT_LEVELS


def _refuse_kept(case: dict) -> None:
    if kept(case):
        raise LetterError("this case is kept on the owner's own model: a hosted model does not read its "
                          "files or write its letter. Tell the owner it waits for them.")


# ── the case folder ───────────────────────────────────────────────────

def letters_dir() -> Path:
    return EA_DIR / "letters"


def case_dir(case: dict, make: bool = True) -> Path:
    """The case's folder under ~/.ea/letters: the base letter's folder when
    one is recorded, else the folder whose case.json names this case, else
    a new one named after the person (with -2, -3 when that is taken)."""
    root = letters_dir()
    base = case.get("base_letter") or ""
    if base:
        p = Path(base).expanduser().parent
        if root in p.parents or p == root:
            return p
    stem = slug(case.get("person_name") or case.get("person_email") or f"case-{case.get('id')}")
    for n in range(1, 50):
        d = root / (stem if n == 1 else f"{stem}-{n}")
        mark = d / CASE_MARK
        if mark.exists():
            try:
                if str(json.loads(_io().read_text(mark) or "{}").get("case")) == str(case.get("id")):
                    return d
            except ValueError:
                pass
            continue
        if d.exists() and any(d.iterdir()):
            continue
        if make:
            write_text(mark, json.dumps({"case": case.get("id"), "person": case.get("person_name") or ""}) + "\n")
        return d
    raise LetterError("no free folder name for this case")


def _requirements(cdir: Path) -> dict:
    try:
        data = json.loads((cdir / REQUIREMENTS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_requirements(cdir: Path, data: dict) -> None:
    write_text(cdir / REQUIREMENTS_FILE, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _dest_req(reqs: dict, dest_id) -> dict:
    row = reqs.get(str(dest_id))
    return row if isinstance(row, dict) else {}


def open_questions(reqs: dict, case: dict) -> list[dict]:
    """The form and rating questions no one has answered, for open destinations."""
    out = []
    for d in _dests(case, states=OPEN_STATES):
        for q in _dest_req(reqs, d.get("id")).get("questions") or []:
            if not q.get("answer"):
                out.append({"dest": d.get("id"), "destination": d.get("name"), "id": q.get("id"),
                            "question": q.get("text"), "options": q.get("options") or [],
                            "rating": bool(q.get("rating"))})
    return out


# ── materials ─────────────────────────────────────────────────────────

def present_materials(case: dict) -> dict[str, list[str]]:
    """{kind: [file names]} of what the case already holds: its recorded
    materials and what the request mail included."""
    out: dict[str, list[str]] = {}
    seen: set[str] = set()   # a file named in the mail and saved here is one file

    def add(kind: str, name: str) -> None:
        key = _safe_name(name).lower()
        if not name or key in seen:
            return
        seen.add(key)
        out.setdefault(kind, []).append(name)
    for m in case.get("materials") or []:
        ref = str(m.get("ref") or "")
        name = Path(ref.split("#")[-1]).name or ref
        kind = (m.get("kind") or "").strip().lower() or material_kind(name)
        add(material_kind(name) if kind == "other" else kind, name)
    for a in case.get("request_attachments") or []:
        name = str((a or {}).get("name") or "") if isinstance(a, dict) else str(a)
        add(material_kind(name), name)
    return out


def destination_gaps(case: dict) -> dict:
    """What the open destinations lack: dates and submission methods."""
    dests = _dests(case, states=OPEN_STATES)
    return {"none": not (case.get("destinations") or []),
            "dates": [d.get("name") for d in dests if not d.get("due")],
            "methods": [d.get("name") for d in dests
                        if (d.get("method") or "unknown").lower() in ("", "unknown")]}


def materials_plan(case: dict, want: list[str] | None = None, extra_present=None) -> dict:
    """What to ask for: the wanted materials the case does not hold yet.
    `want` is the owner's own list; without one, the kind's defaults.
    The destinations list counts as held when every open destination has
    a date and a method."""
    present = present_materials(case)
    for name in extra_present or ():
        present.setdefault(material_kind(name), []).append(name)
    wanted = list(want) if want else list(WANTED.get((case.get("kind") or "").lower(),
                                                      WANTED["recommendation"]))
    gaps = destination_gaps(case)
    missing, asks = [], []
    for kind in wanted:
        if kind == "list":
            if gaps["none"]:
                missing.append(kind)
                asks.append(MATERIAL_WORDS["list"])
            elif gaps["dates"] or gaps["methods"]:
                missing.append(kind)
                parts = []
                if gaps["dates"]:
                    parts.append(f"the due date for {_join(gaps['dates'])}")
                if gaps["methods"]:
                    parts.append(f"how the letter for {_join(gaps['methods'])} is submitted")
                asks.append(_join(parts))
            continue
        if present.get(kind):
            continue
        missing.append(kind)
        asks.append(MATERIAL_WORDS.get(kind, kind))
    if kept(case):
        already = [MATERIAL_NAMES.get(k, k) for k, v in present.items() if v and k != "other"]
        if present.get("other"):
            already.append(f"{len(present['other'])} other file(s)")
    else:
        already = [f"{MATERIAL_NAMES.get(k, k)} ({_join(v)})" for k, v in present.items() if v and k != "other"]
        if present.get("other"):
            already.append(f"other files ({_join(present['other'])})")
    return {"wanted": wanted, "missing": missing, "asks": asks, "already": already,
            "present": present}


# ── tiers and rules ───────────────────────────────────────────────────

def tier(task: str) -> str:
    """The task's tier in autonomy-tiers.json, else its default here."""
    try:
        data = json.loads((EA_DIR / "config" / "autonomy-tiers.json").read_text())
        row = (data.get("task_tiers") or {}).get(task)
        if isinstance(row, dict) and row.get("tier"):
            return str(row["tier"])
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_TIERS.get(task, "ask-first")


def owner_rules() -> list[str]:
    """The owner's standing rules for letters (scopes letters and drafting
    that name a letter), so a decision sees them."""
    try:
        import preferences  # noqa: PLC0415
        data = preferences.load()
    except Exception:
        return []
    out = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or r.get("status") == "retired":
            continue
        scopes = r.get("scopes") or [r.get("scope")]
        text = str(r.get("text") or "")
        if "letters" in scopes or (("drafting" in scopes or "mail-triage" in scopes)
                                   and re.search(r"\bletters?\b|\brec(?:ommendation)?s?\b", text, re.I)):
            out.append(text)
    return out


# ── the owner's answers ───────────────────────────────────────────────

def _set_all(store, case: dict, from_states, state: str, evidence: str, actor: str, dest: str = "") -> list:
    changed = []
    for d in _dests(case, dest, states=from_states):
        store.set_state(d.get("id"), state, evidence, actor)
        changed.append({"dest": d.get("id"), "state": state})
    return changed


def _accept(store, case: dict, quote: str, actor: str, dest: str = "") -> list:
    """Yes: the whole case (letters.accept), or one destination."""
    if dest:
        return _set_all(store, case, ("asked",), "accepted", f"{actor}: {quote}", actor, dest)
    before = [d.get("id") for d in _dests(case, states=("asked",))]
    store.accept(case.get("id"), actor, quote)
    return [{"dest": i, "state": "accepted"} for i in before]


def _decline(store, case: dict, quote: str, actor: str, dest: str = "") -> list:
    """No: the whole case (letters.decline closes every open destination and
    the case's own row), or one destination."""
    if dest:
        return _set_all(store, case, OPEN_STATES, "declined", f"{actor}: {quote}", actor, dest)
    before = [d.get("id") for d in _dests(case, states=OPEN_STATES)]
    store.decline(case.get("id"), actor, quote)
    return [{"dest": i, "state": "declined"} for i in before]


def _reply_target(case: dict) -> dict:
    """The mail to reply to: the person's own request (the case's row, or a
    row from their address), else the first row with a source message."""
    ids = [case.get("case_ledger_id")] + [d.get("ledger_id") for d in case.get("destinations") or []]
    rows = [_ledger_row(i) for i in ids if i]
    rows = [r for r in rows if r.get("source_message_id")]
    email = (case.get("person_email") or "").lower()
    own = [r for r in rows if email and (r.get("from_email") or "").lower() == email]
    row = (own or rows or [{}])[0]
    return {"message_id": row.get("source_message_id") or "",
            "conversation_id": row.get("conversation_id") or "",
            "from_email": row.get("from_email") or ""}


def _brief_path(case: dict, what: str) -> Path:
    return EA_DIR / "state" / f"letter-{slug(str(case.get('id')))}-{what}-brief.txt"


def _reply_steps(case: dict, brief: Path, target: dict) -> list[str]:
    to = case.get("person_email") or target.get("from_email") or "<their address>"
    body = EA_DIR / "state" / "reply-body.txt"
    conv = f" --conversation-id {target['conversation_id']}" if target.get("conversation_id") else ""
    write = f"{PY} {SCRIPT_DIR}/email_writer.py write --to {to} --brief-file {brief} --out {body}{conv}"
    if target.get("message_id"):
        draft = (f"{PY} {SCRIPT_DIR}/mail_actions.py create-reply --provider outlook "
                 f"--message-id {target['message_id']}{conv} --body-file {body}")
    else:
        draft = (f"{PY} {SCRIPT_DIR}/mail_actions.py create-draft --provider outlook --to {to} "
                 f"--subject \"Re: {kind_words(case.get('kind'))}\" --body-file {body}")
    return [write, draft + "   (no --ledger-id: a reply is not the letter, and a send must not close it)"]


def _write_reply_brief(case: dict, what: str, say: list[str], know: list[str]) -> Path:
    path = _brief_path(case, what)
    lines = [f"To: {case.get('person_email') or ''} ({_person(case)})",
             f"Thread: their request for a {kind_words(case.get('kind'))}.",
             "SAY:"] + [f"- {s}" for s in say] + ["KNOW:"] + [f"- {k}" for k in know]
    write_text(path, "\n".join(lines) + "\n")
    return path


def _no_reply_why(case: dict) -> str:
    if kept(case):
        return "the case is kept on your own model, so I do not write to them"
    if not case.get("person_email"):
        return "the request has no address to answer"
    return ""


def decide(store, case_id, answer: str, owner_said: str, *, dest: str = "", want=None,
           reply: bool = False, no_reply: bool = False, after_yes: bool = False,
           actor: str = "owner", look_in_mail: bool = True) -> dict:
    """The owner's answer to "Will you write it?". See the module doc."""
    if answer not in ANSWERS:
        raise LetterError(f"--answer is one of {', '.join(ANSWERS)}")
    owner_said = " ".join((owner_said or "").split())
    case = _case(store, case_id)
    who = _person(case)
    if not owner_said:
        raise LetterError("a case is the owner's to decide: pass --owner-said with their words, "
                          "or post the decision: " + decision_line(case))
    out = {"case": case.get("id"), "person": who, "answer": answer, "email": "none", "rules": owner_rules()}
    if answer == "no":
        told_yes = after_yes or _accepted(case)
        out["states"] = _decline(store, case, owner_said, actor, dest)
        why = "you said not to reply" if no_reply else _no_reply_why(case)
        if why:
            out["owner_line"] = f"Declined: the letter for {who}. No reply is drafted: {why}."
            return out
        target = _reply_target(case)
        say = ["Say no to writing the letter, kindly and in two or three sentences."]
        if told_yes:
            say.append("Earlier the owner said yes: say they can no longer write it.")
        know = [f"The owner's words: \"{owner_said}\". Give a reason only if those words give one.",
                "Do not offer another writer or advice unless the owner's words do."]
        brief = _write_reply_brief(case, "decline", say, know)
        out.update(email="decline-brief", brief_file=str(brief), reply_to=target,
                   next=_reply_steps(case, brief, target),
                   owner_line=f"Declined: the letter for {who}. The reply that says no waits for your Send.")
        return out

    out["states"] = _accept(store, case, owner_said, actor, dest)
    case = _case(store, case_id)
    due, due_dests = _next_due(case)
    due_line = (f" Next due {_due_words(due)}" + ("" if kept(case) else
                f" ({_join([d.get('name') for d in due_dests])})") + "."
                if due else " No due date yet.")
    if answer == "yes":
        why = _no_reply_why(case)
        if reply and not why:
            target = _reply_target(case)
            brief = _write_reply_brief(case, "yes", ["Yes, I will write the letter."],
                                       ["Do not list dates or materials unless the owner's words do."])
            out.update(email="yes-brief", brief_file=str(brief), reply_to=target,
                       next=_reply_steps(case, brief, target))
            out["owner_line"] = f"Accepted: the letter for {who}.{due_line} A short yes waits for your Send."
        else:
            out["owner_line"] = f"Accepted: the letter for {who}.{due_line} No email sent."
        return out

    plan = materials_plan(case, want)
    if plan["missing"] and look_in_mail and not present_materials(case) and not kept(case):
        # Nothing recorded from the request: read its attachment list, so a
        # file the person already sent is never asked for again.
        target = _reply_target(case)
        if target.get("message_id"):
            rows, err = _list_attachments("outlook", "", target["message_id"])
            names = [a.get("name") for a in rows
                     if a.get("name") and not a.get("is_inline") and not (a.get("is_image") and a.get("small"))]
            out["checked_mail"] = {"message_id": target["message_id"], "files": names, "error": err}
            if names:
                plan = materials_plan(case, want, extra_present=names)
    out.update(already_received=plan["already"], asks_for=plan["asks"], missing=plan["missing"])
    have = f" {who} already sent {_join(plan['already'])}." if plan["already"] else ""
    if not plan["missing"]:
        out["owner_line"] = f"Accepted: the letter for {who}.{have} Nothing to ask for, so no email.{due_line}"
        return out
    why = _no_reply_why(case)
    if why:
        out["owner_line"] = (f"Accepted: the letter for {who}.{have} Still missing: {_join(plan['asks'])}. "
                             f"No email is drafted: {why}.")
        return out
    target = _reply_target(case)
    say = ["Yes, I will write the letter.", f"Please send: {_join(plan['asks'])}."]
    know = ["Ask only for the items in SAY."]
    if plan["already"]:
        know.append(f"Already received, never ask for them again: {_join(plan['already'])}.")
    brief = _write_reply_brief(case, "materials", say, know)
    out.update(email="materials-brief", brief_file=str(brief), reply_to=target,
               next=_reply_steps(case, brief, target),
               owner_line=(f"Accepted: the letter for {who}.{have} The reply asks only for "
                           f"{_join(plan['asks'])}. It waits for your Send."))
    return out


def decision_line(case: dict) -> str:
    """The one decision a new case gets, in plain words."""
    who = _person(case)
    dests = _dests(case, states=OPEN_STATES)
    if kept(case):
        places = f"{len(dests)} place(s)" if dests else ""
    else:
        places = _join([f"{d.get('name')} ({_due_words(d.get('due'))})" for d in dests[:4]])
        if len(dests) > 4:
            places += f" and {len(dests) - 4} more"
    present = present_materials(case)
    names = [n for v in present.values() for n in v]
    parts = [f"{who[:1].upper() + who[1:]} asks for a {kind_words(case.get('kind'))}."]
    if places:
        parts.append(f"For: {places}.")
    if kept(case):
        parts.append(f"The request included {len(names)} file(s).")
    else:
        parts.append(f"The request included: {_join(names)}." if names else "The request included no files.")
    parts.append("Will you write it? Yes / Yes, ask for materials / No")
    return " ".join(parts)


# ── the Action requests buttons (owner process) ───────────────────────

def _turn(case: dict, what: str) -> str:
    return (f"Letter case {case.get('id')} ({kind_words(case.get('kind'))} for {_person(case)}). "
            f"{what} Follow docs/ea/letters.md. Never paste the letter text in chat.")


def answer(case_id, choice: str, *, dest: str = "", text: str = "", actor: str = "owner",
           store=None) -> dict:
    """A button on the case's card (letters-surface's callback): {"ok",
    "said", "turn"?}. Reads and writes the store only, never a file in the
    resident's space; `turn` is the prompt for one EA turn when work
    follows, and no turn is started for a case kept on the owner's model."""
    if choice not in CHOICES:
        return {"ok": False, "said": "That choice is not known."}
    try:
        store = store or open_store()
        case = _case(store, case_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "said": "I could not read this letter case.", "error": str(e)[:200]}
    who = _person(case)
    text = " ".join((text or "").split())
    by = _dates().parse_words(text, _today()) if choice in ("yes", "yes_ask", "yes_date", "date") else None
    if choice == "date" and not text:
        by = _dates().proposed(case)  # the date card's "By Tue Dec 1": the earliest a place states
    quote = f"{choice if by else (text or choice)} (Action requests)"

    def turn(what: str) -> dict:
        return {} if kept(case) else {"turn": _turn(case, what)}
    try:
        if choice in ("yes_date", "date") and not by:
            # A date answer with no date in it changes nothing and asks
            # again with the picker; it never loops on free text.
            return {"ok": False, "said": PICK_A_DATE}
        if choice in ("yes", "yes_date"):
            _accept(store, case, quote, actor, dest)
            case = _case(store, case_id)
            if dest:
                due, _ = _next_due(case)
                return {"ok": True, "said": "You said yes. No email sent."
                                            + (f" Next due {_due_words(due)}." if due else "")}
            when, rest = _dated(store, case_id, by)
            return {"ok": True, "said": f"You said yes, by {_dates().day_words(when)}.{rest}" if when
                    else "You said yes. Pick its date on the next card."}
        if choice == "date":
            when, rest = _dated(store, case_id, by)
            if not when:
                return {"ok": False, "said": "I could not record the date. Ask me in Ask Tacitus."}
            return {"ok": True, "said": f"The date is {_dates().day_words(when)}.{rest}"}
        if choice == "yes_ask":
            _accept(store, case, quote, actor, dest)
            case = _case(store, case_id)
            yes = "You said yes."
            if not dest:
                when, _rest = _dated(store, case_id, by)
                yes = f"You said yes, by {_dates().day_words(when)}." if when else yes
                case = _case(store, case_id)
            plan = materials_plan(case)
            have = f" {who[:1].upper() + who[1:]} already sent {_join(plan['already'])}." if plan["already"] else ""
            if not plan["missing"]:
                return {"ok": True, "said": f"{yes}{have} Nothing to ask for, so no email."}
            if kept(case):
                return {"ok": True, "said": f"{yes}{have} Still missing: {_join(plan['asks'])}. "
                                            "This case stays on your own model, so ask them yourself."}
            return {"ok": True,
                    "said": f"{yes}{have} I will draft a short ask for {_join(plan['asks'])}.",
                    **turn("The owner chose Yes, ask for materials. Run letters_work.py decide "
                           f"--case {case.get('id')} --answer materials --owner-said \"Yes, ask for "
                           "materials\", write the reply through the writer from the brief it gives, "
                           "and create the reply draft. Tell the owner what the request already included.")}
        if choice in ("no", "decline"):
            told_yes = _accepted(case)
            _decline(store, case, quote, actor, dest)
            if dest:
                return {"ok": True, "said": "No letter for that place. The rest stay open."}
            why = _no_reply_why(case)
            if why:
                return {"ok": True, "said": f"You said no. The letter for {who} is off your list. "
                                            f"No reply is drafted: {why}."}
            return {"ok": True,
                    "said": f"You said no. The letter for {who} is off your list. A short reply that says no is next.",
                    **turn("The owner said no" + (" after an earlier yes" if told_yes else "") + ". The "
                           "case is declined already. Draft a short, kind reply that says no: "
                           f"letters_work.py decide --case {case.get('id')} --answer no --owner-said \"No\""
                           + (" --after-yes" if told_yes else "") + " writes the brief. Create the reply "
                           "draft; it waits for the owner's Send. Never send it.")}
        if choice == "draft":
            if kept(case):
                return {"ok": True, "said": "This case stays on your own model. I do not draft its letter."}
            ask = f" The owner said: \"{text}\"." if text else ""
            return {"ok": True, "said": "I will draft the letter. You get it back as a file to read.",
                    **turn(f"The owner asked for the letter draft.{ask} Run letters_work.py brief "
                           f"--case {case.get('id')} --owner-said \"{text or 'Draft the letter'}\", give the "
                           "brief to the email-author subagent in the foreground, then check, render and "
                           "name the PDF path in your reply.")}
        if choice == "all":
            dests = _dests(case, states=OPEN_STATES)
            if not dests:
                return {"ok": False, "said": "Nothing was done: every place is already submitted or closed."}
            for d in dests:
                store.set_state(d.get("id"), "submitted", "the owner said all are submitted, in "
                                "Action requests", actor)
            return {"ok": True, "said": f"Submitted: all. The letter for {who} is off your list."}
        if choice == "submitted":
            dests = _dests(case, dest or text, states=OPEN_STATES)
            if not dests:
                return {"ok": False, "said": "Nothing was done: that place is already submitted or closed."}
            for d in dests:
                store.set_state(d.get("id"), "submitted", "the owner said so in Action requests", actor)
            case = _case(store, case_id)
            alld = [d for d in case.get("destinations") or [] if (d.get("state") or "") != "declined"]
            done = [d for d in alld if (d.get("state") or "") == "submitted"]
            if done and len(done) == len(alld):
                return {"ok": True, "said": f"Submitted: all. The letter for {who} is off your list."}
            return {"ok": True, "said": f"Submitted. {len(done)} of {len(alld)} done."}
    except (LetterError, ValueError) as e:
        return {"ok": False, "said": str(e)[:200]}
    return {"ok": False, "said": "That choice is not known."}


# A date answer with no date in it (a code typed with other words).
PICK_A_DATE = "Pick a date with the date button. Nothing was changed."


def _dates():
    try:
        import case_dates  # noqa: PLC0415
    except ModuleNotFoundError:
        from preprocessor import case_dates  # noqa: PLC0415
    return case_dates


def _dated(store, case_id, by) -> tuple:
    """Set the case's one date (the one given, else the earliest a
    destination states). Returns (the date set, or None when there is none
    to set; what follows it in the owner's line: an earlier place's own
    date, and when the case comes back: " I bring it back on Tue Nov
    24.")."""
    D = _dates()
    case = _case(store, case_id)
    when = by or D.proposed(case)
    if not when or not (hasattr(store, "set_date") and store.set_date(case_id, when.isoformat())):
        return None, ""
    case = _case(store, case_id)
    out = ""
    early = sorted(((D.own_due(d), d.get("name") or "") for d in _dests(case, states=OPEN_STATES)
                    if D.own_due(d) and D.own_due(d) < when), key=lambda x: x[0])
    if early and not kept(case):
        out += f" {early[0][1]} asks by {D.day_words(early[0][0])}."
    back = D.wake(dict(case, case_kind="letter"))
    if back and back > _today():
        out += f" I bring it back on {D.day_words(back)}."
    else:
        out += " It is time to start, so it stays on your list."
    return when, out


def parse_due(text: str) -> str:
    """A date in the owner's words as YYYY-MM-DD ("" for none): the common
    forms (case_dates.parse_words), then the ledger's own reader."""
    d = _dates().parse_words(text, _today())
    if d:
        return d.isoformat()
    text = (text or "").strip()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return ""
    try:
        import ledger  # noqa: PLC0415
        iso, _words = ledger.date_in_text("due " + text, _today())
        return iso
    except Exception:
        return ""


# ── materials from mail ───────────────────────────────────────────────

def run_tool(script: str, args: list[str]) -> tuple[int, object]:
    """One call of another EA tool; (exit code, its JSON or {"error": text})."""
    proc = subprocess.run([sys.executable, str(SCRIPT_DIR / script), *args],
                          capture_output=True, text=True, timeout=180)
    out = (proc.stdout or "").strip()
    try:
        return proc.returncode, json.loads(out) if out else {"error": (proc.stderr or "")[-300:]}
    except ValueError:
        return proc.returncode, {"error": (out or proc.stderr or "")[-300:]}


def _list_attachments(provider: str, account: str, message_id: str) -> tuple[list[dict], str]:
    if provider == "gmail":
        rc, data = run_tool("gmail_client.py", ["list-attachments", "--email", account, "--message-id", message_id])
        rows = (data or {}).get("attachments") if isinstance(data, dict) else None
        if rc or rows is None:
            return [], str((data or {}).get("error") if isinstance(data, dict) else data)[:200]
        return [{"id": r.get("attachment_id"), "name": r.get("filename") or "", "size": r.get("size"),
                 "is_inline": r.get("is_inline"), "is_image": r.get("is_image"), "small": r.get("small")}
                for r in rows], ""
    rc, data = run_tool("graph_actions.py", ["get-attachments", "--message-id", message_id])
    if rc or not isinstance(data, list):
        return [], str(data.get("error") if isinstance(data, dict) else data)[:200]
    return data, ""


def _download(provider: str, account: str, message_id: str, att_id: str, out: Path) -> str:
    if provider == "gmail":
        args = ["download-attachment", "--email", account, "--message-id", message_id,
                "--attachment-id", att_id, "--output", str(out)]
        rc, data = run_tool("gmail_client.py", args)
    else:
        rc, data = run_tool("graph_actions.py", ["download-attachment", "--message-id", message_id,
                                                 "--attachment-id", att_id, "--output", str(out)])
    if rc or (isinstance(data, dict) and data.get("error")):
        return str(data.get("error") if isinstance(data, dict) else data)[:200] or "download failed"
    return ""


def _safe_name(name: str) -> str:
    p = Path(name or "file")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", p.stem).strip("-.") or "file"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", p.suffix)[:10]
    return stem[:80] + suffix


def collect(store, case_id, message_ids=(), provider: str = "outlook", account: str = "") -> dict:
    """Save the attachments of the request mails into the case folder."""
    if provider == "gmail" and not account:
        raise LetterError("--account is required for Gmail")
    case = _case(store, case_id)
    _refuse_kept(case)
    mids = [m for m in message_ids if m]
    if not mids:
        rows = [case.get("case_ledger_id")] + [d.get("ledger_id") for d in case.get("destinations") or []]
        for row_id in rows:
            mid = _ledger_row(row_id).get("source_message_id") if row_id else ""
            if mid and mid not in mids:
                mids.append(mid)
    if not mids:
        raise LetterError("no request mail on this case: pass --message-id")
    cdir = case_dir(case)
    mdir = make_dir(cdir / MATERIALS_DIR)
    known = {str(m.get("ref")) for m in case.get("materials") or []}
    saved, skipped, errors = [], [], []
    for mid in mids:
        rows, err = _list_attachments(provider, account, mid)
        if err:
            errors.append({"message_id": mid, "error": err})
            continue
        for a in rows:
            name = a.get("name") or ""
            if a.get("is_inline") or (a.get("is_image") and a.get("small")) or not a.get("id"):
                skipped.append({"name": name, "why": "an image in the mail body"})
                continue
            target = mdir / _safe_name(name)
            if str(target) in known or (target.exists() and a.get("size") and target.stat().st_size == a["size"]):
                skipped.append({"name": name, "why": "already saved", "path": str(target)})
                if str(target) not in known:
                    store.add_material(case.get("id"), material_kind(name), str(target))
                    known.add(str(target))
                continue
            n = 2
            while target.exists():
                safe = Path(_safe_name(name))
                target = mdir / f"{safe.stem}-{n}{safe.suffix}"
                n += 1
            err = _download(provider, account, mid, a["id"], target)
            if err:
                errors.append({"name": name, "error": err})
                continue
            kind = material_kind(name)
            store.add_material(case.get("id"), kind, str(target))
            known.add(str(target))
            saved.append({"name": name, "kind": kind, "path": str(target)})
    case = _case(store, case_id)
    return {"case": case.get("id"), "folder": str(cdir), "saved": saved, "skipped": skipped,
            "errors": errors, "materials": present_materials(case)}


# ── status ────────────────────────────────────────────────────────────

def _dest_file(cdir: Path, case: dict, d: dict) -> str:
    """The letter text a destination gets: its own copy (the store's
    `draft`, else <case folder>/<destination>.md), else the base letter."""
    if d.get("draft"):
        return str(d["draft"])
    v = cdir / f"{slug(d.get('name') or str(d.get('id')))}.md"
    if v.exists():
        return str(v)
    return case.get("base_letter") or ""


def _next_step(case: dict, d: dict, reqs: dict, cdir: Path | None) -> str:
    state = d.get("state") or "asked"
    if state == "asked":
        return "accept: the case is accepted (extend)" if _accepted(case) else "the owner decides (decide)"
    if state == "accepted":
        return "draft the letter (brief)"
    if state == "drafted":
        qs = [q for q in _dest_req(reqs, d.get("id")).get("questions") or [] if not q.get("answer")]
        if qs:
            return f"the owner answers {len(qs)} form question(s) (answered), then approves (approve)"
        return "render it and the owner approves the text (render, approve)"
    if state == "ready":
        return "submit it (submit)"
    return ""


def status(store, case_id) -> dict:
    case = _case(store, case_id)
    if kept(case):
        # Counts, dates, kinds and states only (letters.view).
        case = {**store.view(case), "keep_level": case.get("keep_level")}
    cdir = case_dir(case, make=False)
    reqs = _requirements(cdir) if cdir.exists() and not kept(case) else {}
    dests = []
    for d in case.get("destinations") or []:
        dests.append({"id": d.get("id"), "name": d.get("name"), "due": d.get("due") or "",
                      "method": d.get("method") or "unknown", "link": d.get("link") or "",
                      "state": d.get("state") or "asked",
                      "file": _dest_file(cdir, case, d) if cdir.exists() else (case.get("base_letter") or ""),
                      "next": _next_step(case, d, reqs, cdir)})
    plan = materials_plan(case)
    gaps = destination_gaps(case)
    out = {"case": case.get("id"), "person": _person(case), "kind": case.get("kind") or "",
           "folder": str(cdir), "destinations": dests,
           "materials": plan["present"], "missing": {"materials": [k for k in plan["missing"] if k != "list"],
                                                     "dates": gaps["dates"], "methods": gaps["methods"],
                                                     "no_destinations": gaps["none"]},
           "questions": open_questions(reqs, case),
           "tiers": {"draft": tier(DRAFT_TASK), "upload": tier(UPLOAD_TASK)},
           "kept": kept(case)}
    if case.get("destinations") and not _accepted(case) and \
            all((d.get("state") or "asked") == "asked" for d in case["destinations"]):
        out["decision"] = decision_line(case)
    return out


def extend(store, case_id, dest: str) -> dict:
    """A destination that came after the owner's yes joins the case."""
    case = _case(store, case_id)
    _refuse_kept(case)
    if not _accepted(case):
        raise LetterError("the owner has not said yes to this case: post the decision")
    targets = _dests(case, dest, states=("asked", "accepted"))
    if not targets:
        raise LetterError("that destination has its letter already, or is closed")
    d = targets[0]
    if (d.get("state") or "asked") == "asked":
        store.set_state(d.get("id"), "accepted", "joins a case the owner accepted", "ea")
    out = {"case": case.get("id"), "dest": d.get("id"), "state": "accepted",
           "owner_line": (f"New place for the letter for {_person(case)}: {d.get('name')}, "
                          f"due {_due_words(d.get('due'))}. Say if it should not get a letter.")}
    base = case.get("base_letter") or ""
    if base and Path(base).exists():
        text = Path(base).read_text()
        if _PLACEHOLDER_RE.search(text):
            out["variant"] = variants(store, case_id, base, only=str(d.get("id")))
        else:
            out["variant"] = {"note": "the base letter names no destination: it serves this one too"}
        out["next"] = "render the letter for this place and ask the owner to approve it"
    return out


# ── requirements and questions ────────────────────────────────────────

def require(store, case_id, dest: str, *, length="", fmt="", addressee="", program="",
            organization="", question="", options="", rating=False) -> dict:
    case = _case(store, case_id)
    _refuse_kept(case)
    d = _dests(case, dest)[0]
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    row = reqs.setdefault(str(d.get("id")), {})
    for key, value in (("length", length), ("format", fmt), ("addressee", addressee),
                       ("program", program), ("organization", organization)):
        if value:
            row[key] = " ".join(value.split())
    if question:
        qs = row.setdefault("questions", [])
        opts = [o.strip() for o in (options or "").split("|") if o.strip()]
        qid = f"q{len(qs) + 1}"
        qs.append({"id": qid, "text": " ".join(question.split()), "options": opts,
                   "rating": bool(rating), "answer": None})
        row["questions"] = qs
    _save_requirements(cdir, reqs)
    return {"case": case.get("id"), "dest": d.get("id"), "requirements": row}


def answered(store, case_id, dest: str, qid: str, value: str, owner_said: str) -> dict:
    owner_said = " ".join((owner_said or "").split())
    if not owner_said:
        raise LetterError("an answer to a form question is the owner's: pass --owner-said with their words")
    case = _case(store, case_id)
    _refuse_kept(case)
    d = _dests(case, dest)[0]
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    qs = _dest_req(reqs, d.get("id")).get("questions") or []
    q = next((q for q in qs if q.get("id") == qid), None)
    if q is None:
        raise LetterError(f"no question {qid!r} for {d.get('name')}")
    value = " ".join((value or "").split())
    if q.get("options"):
        match = [o for o in q["options"] if o.lower() == value.lower()]
        if not match:
            raise LetterError(f"the answer is one of: {' | '.join(q['options'])}")
        value = match[0]
    q["answer"] = {"value": value, "owner_said": owner_said, "on": _today().isoformat()}
    _save_requirements(cdir, reqs)
    return {"case": case.get("id"), "dest": d.get("id"), "question": qid, "answer": value,
            "open_questions": open_questions(reqs, case)}


# ── the brief for the letter ──────────────────────────────────────────

def _profile() -> dict:
    try:
        return json.loads((EA_DIR / "config" / "user-profile.json").read_text())
    except (OSError, ValueError):
        return {}


def _role(profile: dict) -> str:
    role = profile.get("role") or profile.get("title") or profile.get("role_description") or ""
    role = " ".join(str(role).split())
    first = re.split(r"(?<=[.!?])\s", role)[0] if role else ""
    return first[:240]


def earlier_letters(case: dict, limit: int = EARLIER_LETTERS) -> dict:
    """Letters already written: for this person (by name in the file path),
    then the newest of the same kind, and the letterhead's example."""
    root = letters_dir()
    if not root.exists():
        return {"same_person": [], "same_kind": [], "example": ""}
    name_bits = [b for b in slug(case.get("person_name") or "").split("-") if len(b) > 2]
    kind = (case.get("kind") or "").lower()
    kind_bits = {"recommendation": ("rec", "recommend"), "reference": ("ref", "reference"),
                 "nomination": ("nomination", "nominate"), "support": ("support", "los"),
                 "character": ("character", "reference")}.get(kind, (kind,) if kind else ())
    this = case_dir(case, make=False)
    same_person, same_kind = [], []
    files = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".tex", ".md", ".txt", ".docx"):
            continue
        rel = p.relative_to(root)
        if MATERIALS_DIR in rel.parts or p.name.endswith("brief.txt") or p.name.startswith("."):
            continue
        if this in p.parents:
            continue
        files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        low = str(p.relative_to(root)).lower()
        if name_bits and all(b in low for b in name_bits[-1:]) and any(b in low for b in name_bits):
            same_person.append(str(p))
        elif kind_bits and any(b in low for b in kind_bits):
            same_kind.append(str(p))
    example = ""
    try:
        import institution  # noqa: PLC0415
        ex = institution.letterhead()["template_dir"] / "example-recommendation.tex"
        example = str(ex) if ex.exists() else ""
    except Exception:
        pass
    return {"same_person": same_person[:limit], "same_kind": same_kind[:limit], "example": example}


def _excerpt(path: str) -> str:
    try:
        import doc_text  # noqa: PLC0415
        p = doc_text.allowed_path(path)
        text = doc_text.document_text(p)
    except Exception as e:  # noqa: BLE001
        return f"(not read: {str(e)[:120]})"
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text[:EXCERPT_CHARS] + ("\n(cut here)" if len(text) > EXCERPT_CHARS else "")


def brief(store, case_id, *, dest: str = "", notes_file: str = "", owner_said: str = "") -> dict:
    case = _case(store, case_id)
    _refuse_kept(case)
    who = _person(case)
    owner_said = " ".join((owner_said or "").split())
    t = tier(DRAFT_TASK)
    if not owner_said and t == "ask-first":
        due, _ = _next_due(case)
        return {"case": case.get("id"), "status": "ask-first", "tier": t,
                "ask": f"Shall I draft the letter for {who}?"
                       + (f" Next due {_due_words(due)}." if due else "")}
    targets = _dests(case, dest, states=("accepted", "drafted", "ready"))
    if not targets:
        raise LetterError("no accepted destination: the owner says yes first (decide)")
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    profile = _profile()
    multi = len(targets) > 1
    lines = [f"LETTER: a {kind_words(case.get('kind'))} for {who}"
             + (f" ({case.get('person_email')})" if case.get("person_email") else "") + "."]
    writer = profile.get("name") or "the owner"
    role = _role(profile)
    lines.append(f"WRITER: {writer}" + (f", {role}" if role else "") + ". The letter is in their voice.")
    lines.append("FOR:")
    for d in targets:
        r = _dest_req(reqs, d.get("id"))
        bits = [f"- {d.get('name')}", f"due {_due_words(d.get('due'))}"]
        for key in ("program", "addressee", "organization", "length", "format"):
            if r.get(key):
                bits.append(f"{key}: {r[key]}")
        lines.append("; ".join(bits))
    lengths = {(_dest_req(reqs, d.get("id")).get("length") or "") for d in targets} - {""}
    lines.append(f"LENGTH: {_join(sorted(lengths))}." if lengths else "LENGTH: one page.")
    if multi:
        lines.append("NAMES: write {{program}} where the program or position goes and {{addressee}} "
                     "in the salutation, so one letter serves every place. Never write a place's name.")
    lines.append("SAY (the owner's points, in their words):")
    notes = ""
    if notes_file:
        try:
            notes = Path(notes_file).expanduser().read_text().strip()
        except OSError as e:
            raise LetterError(f"--notes-file not read: {e}") from e
    if notes:
        lines += [f"  {ln}" for ln in notes.splitlines()]
    if case.get("notes"):
        lines.append(f"  (case notes) {' '.join(str(case['notes']).split())}")
    if owner_said:
        lines.append(f"  (the owner asked) {owner_said}")
    if not notes and not case.get("notes"):
        lines.append("  None yet. Draft from KNOW and mark every judgment you would need with [ASK: ...].")
    lines.append("KNOW (facts for accuracy; the letter uses only what is relevant):")
    present = present_materials(case)
    saved = {_safe_name(Path(str(m.get("ref"))).name).lower() for m in case.get("materials") or []
             if m.get("ref") and Path(str(m["ref"])).expanduser().exists()}
    unsaved, total = [], 0
    for m in case.get("materials") or []:
        ref = str(m.get("ref") or "")
        p = Path(ref).expanduser()
        if not ref or not p.exists():
            name = Path(ref.split("#")[-1]).name or ref
            if ref and _safe_name(name).lower() not in saved and name not in unsaved:
                unsaved.append(name)
            continue
        kind = m.get("kind") or material_kind(p.name)
        lines.append(f"- {MATERIAL_NAMES.get(kind, kind)}: {p}")
        if total < EXCERPTS_TOTAL:
            ex = _excerpt(str(p))
            total += len(ex)
            lines += [f"    {ln}" for ln in ex.splitlines()]
    for a in case.get("request_attachments") or []:
        name = a.get("name") if isinstance(a, dict) else str(a)
        if name and _safe_name(name).lower() not in saved and name not in unsaved:
            unsaved.append(name)
    prior = earlier_letters(case)
    if prior["same_person"]:
        lines.append("- Earlier letters by the owner for this person (facts and judgments the owner "
                     "already stated; reuse them): " + ", ".join(prior["same_person"]))
    voice = prior["same_kind"] + ([prior["example"]] if prior["example"] else [])
    if voice:
        lines.append("- Earlier letters of this kind, for voice and length only (never their facts): "
                     + ", ".join(voice))
    asks = open_questions(reqs, case)
    if asks:
        lines.append("FORM QUESTIONS (the owner answers these in the portal; never answer them in the letter):")
        lines += [f"- {q['destination']}: {q['question']}" for q in asks]
    lines += [
        "RULES:",
        "- This is the owner's evaluation of a person. Every claim about the person comes from SAY or KNOW.",
        "- A fact or judgment the letter needs and SAY and KNOW do not give: write [ASK: <the question>]"
        " in its place. Never invent a project, a number, a comparison or a rank.",
        "- Never state a rating, a percentile or a ranking unless SAY gives it.",
        "- No em-dash or en-dash. State the fact and stop.",
        "- Output: the letter body only, from the salutation to the sign-off, as plain text.",
    ]
    stem = "brief" if not dest else f"brief-{slug(targets[0].get('name') or str(dest))}"
    path = cdir / f"{stem}.txt"
    write_text(path, "\n".join(lines) + "\n")
    out_file = cdir / ("letter.md" if not dest else f"{slug(targets[0].get('name') or str(dest))}.md")
    return {"case": case.get("id"), "status": "brief", "tier": t, "brief": str(path), "out": str(out_file),
            "materials": present, "unsaved_materials": unsaved, "earlier_letters": prior,
            "questions": asks,
            "next": [f"Agent email-author, in the foreground: \"Write the letter in {path}. Read every "
                     "file it names. Return the letter body only.\"",
                     f"Write its text to {out_file} (the Write tool)",
                     f"{PY} {SCRIPT_DIR}/letters_work.py check --case {case.get('id')} --file {out_file} --record"]
            + ([f"{PY} {SCRIPT_DIR}/letters_work.py collect --case {case.get('id')}   (first: "
                f"{len(unsaved)} file(s) from the request are not saved yet)"] if unsaved else [])}


# ── checks, variants, rendering ───────────────────────────────────────

def _place(d: dict) -> str:
    """A destination's name as its mail gave it; "" for a name the store
    made up so the owner can tell places apart (a sender's domain, "Place
    2"), which never goes into a letter."""
    return str(d.get("name") or "") if d.get("named", True) else ""


def _phrases(case: dict, reqs: dict, d: dict) -> list[str]:
    r = _dest_req(reqs, d.get("id"))
    return [p for p in (_place(d), r.get("program"), r.get("addressee"), r.get("organization"))
            if p and len(str(p).strip()) > 2]


def _leaks(text: str, case: dict, reqs: dict, d: dict | None) -> list[str]:
    """Other destinations' names in a text meant for `d` (None: the base
    letter, which may name no destination at all when there are several)."""
    own = _phrases(case, reqs, d) if d else []
    scrub = text
    for p in sorted(own, key=len, reverse=True):
        scrub = re.sub(re.escape(p), " ", scrub, flags=re.I)
    found = []
    others = [o for o in case.get("destinations") or [] if not d or str(o.get("id")) != str(d.get("id"))]
    if d is None and len(others) <= 1:
        return []
    for o in others:
        for p in _phrases(case, reqs, o):
            if p.lower() in {x.lower() for x in own}:
                continue
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(p)}(?![A-Za-z0-9])", scrub, re.I):
                found.append(p)
    return sorted(set(found))


def check_text(text: str, case: dict, reqs: dict, d: dict | None) -> dict:
    asks = [" ".join(a.split()) or "(a point to check)" for a in _ASK_RE.findall(text)]
    holders = sorted(set(_PLACEHOLDER_RE.findall(text)))
    unknown = [h for h in holders if h not in PLACEHOLDERS]
    leaks = _leaks(text, case, reqs, d)
    dashes = sum(text.count(c) for c in _DASHES)
    words = len(re.findall(r"\b\w+\b", text))
    problems = []
    if leaks:
        problems.append(f"names another place: {_join(leaks)}")
    if d is not None and holders:
        problems.append(f"placeholders left: {_join(holders)}")
    if unknown:
        problems.append(f"unknown placeholders: {_join(unknown)}")
    if dashes:
        problems.append(f"{dashes} em-dash or en-dash")
    return {"questions": asks, "placeholders": holders, "other_destinations": leaks,
            "problems": problems, "words": words, "ok": not problems}


def check(store, case_id, file: str, *, dest: str = "", record: bool = False) -> dict:
    case = _case(store, case_id)
    _refuse_kept(case)
    path = Path(file).expanduser()
    try:
        text = path.read_text()
    except OSError as e:
        raise LetterError(f"--file not read: {e}") from e
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    d = _dests(case, dest)[0] if dest else None
    res = check_text(text, case, reqs, d)
    out = {"case": case.get("id"), "file": str(path), **res}
    if record:
        if res["other_destinations"] or any(p.startswith("unknown") for p in res["problems"]):
            out["recorded"] = False
            out["why"] = "fix the problems first"
            return out
        store.record_draft(case.get("id"), str(path), str(d.get("id")) if d else "")
        out["recorded"] = True
    if res["questions"]:
        out["owner_line"] = ("The draft has questions only you can answer: "
                             + " / ".join(res["questions"][:6]))
    return out


def _fill(text: str, case: dict, reqs: dict, d: dict) -> str:
    r = _dest_req(reqs, d.get("id"))
    values = {"program": r.get("program") or _place(d),
              "destination": _place(d),
              "addressee": r.get("addressee") or "Selection Committee",
              "organization": r.get("organization") or _place(d)}
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def variants(store, case_id, base: str, *, base_dest: str = "", only: str = "") -> dict:
    """One copy of the base letter per destination, only where a name must
    change: the base's {{program}}/{{addressee}} placeholders filled, or the
    base destination's own names swapped (--base-dest). Each copy is
    checked: no other destination's name may survive in it."""
    case = _case(store, case_id)
    _refuse_kept(case)
    path = Path(base).expanduser()
    try:
        text = path.read_text()
    except OSError as e:
        raise LetterError(f"--base not read: {e}") from e
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    # An approved (ready) copy is never rewritten here.
    dests = _dests(case, states=("accepted", "drafted"))
    if only:
        dests = [d for d in dests if str(d.get("id")) == str(only)]
    holders = _PLACEHOLDER_RE.findall(text)
    src = None
    if base_dest:
        src = _dests(case, base_dest)[0]
    elif not holders:
        return {"case": case.get("id"), "variants": [], "base": str(path),
                "note": "the base letter names no destination: one letter serves every place"}
    made, refused = [], []
    for d in dests:
        if src is not None and str(d.get("id")) == str(src.get("id")):
            body = text
        elif src is not None:
            body = text
            mine = {"name": _place(d), **_dest_req(reqs, d.get("id"))}
            theirs = {"name": _place(src), **_dest_req(reqs, src.get("id"))}
            for key in ("program", "addressee", "organization", "name"):
                old, new = theirs.get(key), mine.get(key) or (_place(d) if key != "addressee" else "")
                if old and new and old != new:
                    body = re.sub(re.escape(old), new.replace("\\", r"\\"), body)
        else:
            body = _fill(text, case, reqs, d)
        res = check_text(body, case, reqs, d)
        if not res["ok"] and (res["other_destinations"] or res["placeholders"]):
            refused.append({"dest": d.get("id"), "name": d.get("name"), "problems": res["problems"]})
            continue
        out = cdir / f"{slug(d.get('name') or str(d.get('id')))}.md"
        write_text(out, body)
        store.record_draft(case.get("id"), str(out), str(d.get("id")))
        made.append({"dest": d.get("id"), "name": d.get("name"), "file": str(out),
                     "questions": res["questions"]})
    return {"case": case.get("id"), "base": str(path), "variants": made, "refused": refused}


_LATEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                  "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def latex_escape(text: str) -> str:
    return "".join(_LATEX_SPECIAL.get(c, c) for c in text)


def _inline_latex(text: str) -> str:
    out = latex_escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\\textit{\1}", out)
    return out


def split_letter(text: str) -> dict:
    """The salutation, the body paragraphs and the closing of a plain letter."""
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    salutation = ""
    if lines and _SALUTATION_RE.match(lines[0].strip()) and len(lines[0]) < 120:
        salutation = lines.pop(0).strip()
    closing, sign = "", []
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        if _CLOSING_RE.match(lines[i].strip()):
            closing = lines[i].strip()
            sign = [ln.strip() for ln in lines[i + 1:] if ln.strip()]
            lines = lines[:i]
            break
    paras, cur = [], []
    for ln in lines:
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return {"salutation": salutation, "paragraphs": paras, "closing": closing or "Sincerely,",
            "signature": sign}


PLAIN_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.8em}
\pagestyle{empty}
\begin{document}
<<DATE>>

<<RECIPIENT_BLOCK>>

<<SALUTATION>>

<<BODY>>

<<CLOSING>>

\vspace{2.5em}
<<SIGNATURE>>
\end{document}
"""


def _letterhead_template() -> Path | None:
    try:
        import institution  # noqa: PLC0415
        t = institution.letterhead()["template_dir"] / "template.tex"
        return t if t.exists() else None
    except Exception:
        return None


def _compile(tex: Path, pdf: Path) -> bool:
    import compile_letter  # noqa: PLC0415
    return compile_letter.compile_letter(str(tex), str(pdf))


def to_latex(parts: dict, template: str, recipient: list[str], signature: list[str]) -> str:
    rec = r" \\ ".join(latex_escape(x) for x in recipient if x) or r"$\quad$"
    body = "\n\n".join(_inline_latex(p) for p in parts["paragraphs"])
    sig = r" \\ ".join(latex_escape(x) for x in signature if x)
    today = _today()
    values = {"<<RECIPIENT_BLOCK>>": rec, "<<SALUTATION>>": latex_escape(parts["salutation"] or "To whom it may concern,"),
              "<<BODY>>": body, "<<CLOSING>>": latex_escape(parts["closing"]),
              "<<SIGNATURE>>": sig, "<<DATE>>": f"{today:%B} {today.day}, {today.year}"}
    out = template
    for k, v in values.items():
        out = out.replace(k, v)
    return out


def render(store, case_id, file: str, *, dest: str = "", docx: bool = False, template: str = "") -> dict:
    case = _case(store, case_id)
    _refuse_kept(case)
    path = Path(file).expanduser()
    try:
        text = path.read_text()
    except OSError as e:
        raise LetterError(f"--file not read: {e}") from e
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    d = _dests(case, dest)[0] if dest else None
    res = check_text(text, case, reqs, d)
    if res["other_destinations"] or res["placeholders"]:
        raise LetterError("not rendered: " + "; ".join(
            res["problems"] or ["placeholders left: make one copy per place first (variants)"]))
    stem = path.with_suffix("")
    if docx:
        import doc_write  # noqa: PLC0415
        out = stem.with_suffix(".docx")
        tpl = doc_write.template_path(template) if template else None
        doc_write.build(text, out, template=tpl)
        return {"case": case.get("id"), "file_for_owner": str(out), "docx": str(out), "letterhead": bool(tpl),
                "questions": res["questions"]}
    parts = split_letter(text)
    r = _dest_req(reqs, d.get("id")) if d else {}
    recipient = [r.get("addressee") or "", r.get("organization") or ""] if d else []
    lh = _letterhead_template()
    profile = _profile()
    if lh is not None:
        tpl_text = lh.read_text()
        signature = []
    else:
        tpl_text = PLAIN_TEMPLATE if any(recipient) else PLAIN_TEMPLATE.replace("<<RECIPIENT_BLOCK>>\n\n", "")
        inst = profile.get("institution") if isinstance(profile.get("institution"), dict) else {}
        # The letter's own sign-off lines; a bare name gets the profile's
        # department and institution under it.
        signature = parts["signature"]
        if len(signature) < 2:
            signature = (signature or [profile.get("name") or ""]) + [
                profile.get("department") or "", inst.get("name") or profile.get("university") or ""]
    tex = stem.with_suffix(".tex")
    write_text(tex, to_latex(parts, tpl_text, recipient, signature))
    pdf = stem.with_suffix(".pdf")
    if not _compile(tex, pdf):
        raise LetterError(f"the PDF did not build from {tex}: fix the LaTeX (often an unescaped character) "
                          f"and run render again")
    return {"case": case.get("id"), "file_for_owner": str(pdf), "pdf": str(pdf), "tex": str(tex),
            "letterhead": lh is not None, "questions": res["questions"],
            "owner_line": (f"The letter for {_person(case)} is attached: {pdf}. Say what to change, "
                           "or say it is ready.")}


# ── approval and submission ───────────────────────────────────────────

def _rendered(path: str) -> str:
    if not path:
        return ""
    for suffix in (".pdf", ".docx"):
        p = Path(path).with_suffix(suffix)
        if p.exists():
            return str(p)
    return ""


def approve(store, case_id, owner_said: str, *, dest: str = "") -> dict:
    owner_said = " ".join((owner_said or "").split())
    if not owner_said:
        raise LetterError("approval is the owner's: pass --owner-said with their words")
    case = _case(store, case_id)
    _refuse_kept(case)
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    ready, blocked = [], []
    for d in _dests(case, dest, states=("drafted", "ready")):
        f = _dest_file(cdir, case, d)
        why = []
        if not f or not Path(f).exists():
            why.append("no letter file")
        else:
            res = check_text(Path(f).read_text(), case, reqs, d if f != case.get("base_letter")
                             or len(case.get("destinations") or []) == 1 else None)
            why += res["problems"]
            if res["questions"]:
                why.append(f"{len(res['questions'])} question(s) left in the text")
            if f == case.get("base_letter") and res["placeholders"]:
                why.append("the letter still has placeholders: make the copies (variants)")
            if not _rendered(f):
                why.append("not rendered yet (render)")
        qs = [q for q in _dest_req(reqs, d.get("id")).get("questions") or [] if not q.get("answer")]
        if qs:
            why.append(f"{len(qs)} form question(s) not answered")
        if why:
            blocked.append({"dest": d.get("id"), "name": d.get("name"), "why": why})
            continue
        if (d.get("state") or "") != "ready":
            store.set_state(d.get("id"), "ready", f"owner: {owner_said}", "owner")
        ready.append({"dest": d.get("id"), "name": d.get("name"), "file": _rendered(f)})
    return {"case": case.get("id"), "ready": ready, "blocked": blocked}


def submit(store, case_id, dest: str) -> dict:
    case = _case(store, case_id)
    _refuse_kept(case)
    d = _dests(case, dest)[0]
    if (d.get("state") or "") != "ready":
        raise LetterError(f"{d.get('name')} is {d.get('state') or 'asked'}: the owner approves the text first")
    cdir = case_dir(case)
    reqs = _requirements(cdir)
    pdf = _rendered(_dest_file(cdir, case, d))
    if not pdf:
        raise LetterError("no rendered letter for this place: render it first")
    who = _person(case)
    method = (d.get("method") or "unknown").lower()
    link = (d.get("link") or "").strip()
    qs = _dest_req(reqs, d.get("id")).get("questions") or []
    answers = [{"question": q.get("text"), "answer": (q.get("answer") or {}).get("value")} for q in qs]
    out = {"case": case.get("id"), "dest": d.get("id"), "name": d.get("name"), "method": method,
           "file": pdf, "form_answers": answers,
           "record": f"{PY} {SCRIPT_DIR}/letters.py record-submitted --dest {d.get('id')} --evidence \"<what shows it>\""}
    addr = link[7:] if link.lower().startswith("mailto:") else link
    if method == "email" or (method == "unknown" and re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", addr or "")):
        subject = f"{SUBJECT_WORDS.get((case.get('kind') or '').lower(), 'Letter')} for {who}"
        brief = _brief_path(case, f"cover-{slug(d.get('name') or '')}")
        write_text(brief, "\n".join([
            f"To: {addr} ({d.get('name')})",
            f"Thread: none; a new email that carries the owner's {kind_words(case.get('kind'))}.",
            "SAY:", f"- Attached is my {kind_words(case.get('kind'))} for {who}"
            + (f" for {_place(d)}." if _place(d) else "."),
            "KNOW:", "- The letter is attached. Do not describe it or praise the person here.",
        ]) + "\n")
        body = EA_DIR / "state" / "reply-body.txt"
        out.update(tier="button-required", steps=[
            f"{PY} {SCRIPT_DIR}/email_writer.py write --to {addr} --brief-file {brief} --out {body}",
            f"{PY} {SCRIPT_DIR}/mail_actions.py create-draft --provider outlook --to {addr} "
            f"--subject \"{subject}\" --body-file {body} --ledger-id {d.get('ledger_id')}",
            f"{PY} {SCRIPT_DIR}/graph_actions.py attach-file --message-id <draft id> --file {pdf}",
            "The owner's Send closes this place; nothing else is needed.",
        ], owner_line=f"The letter for {d.get('name')} is on a draft to {addr}. It waits for your Send.")
        return out
    if method == "portal" and link:
        blocked = [a["question"] for a in answers if not a["answer"]]
        out["tier"] = tier(UPLOAD_TASK)
        if blocked:
            out.update(blocked=blocked, owner_line=(f"{d.get('name')} asks questions only you can answer: "
                                                    + " / ".join(blocked[:6])))
            return out
        out["steps"] = [
            f"{PY} {SCRIPT_DIR}/web_browser.py read --url {link}   (see the form; a sign-in page means "
            "the owner uploads it)",
            f"{PY} {SCRIPT_DIR}/web_browser.py fill --url {link} --actions-json '<the form answers, "
            f"and an upload action with path {pdf}>'   (stages the owner's confirm card; nothing is entered "
            "before the click)",
            "Or a plugin's upload verb for this portal, when its skill names one.",
        ]
        out["owner_line"] = (f"The letter for {d.get('name')} is ready to upload. "
                             f"Link: {link}. File: {pdf}.")
        if out["tier"] == "ask-first":
            out["owner_line"] += " Shall I fill the form for you to confirm?"
        return out
    out.update(tier="owner", steps=["Give the owner the link and the file; name the full PDF path so it is attached."],
               owner_line=(f"Submit the letter for {d.get('name')} yourself"
                           + (f": {link}." if link else ". I have no link for it.")
                           + f" The file: {pdf}. Tell me when it is in."))
    return out


# ── CLI ───────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="The work on a letter case (docs/ea/letters.md)",
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)

    def verb(name, help_text):
        p = sub.add_parser(name, help=help_text, allow_abbrev=False)
        p.add_argument("--case", required=True)
        return p

    verb("status", "the case, what is missing and the next steps")
    p = verb("decide", "the owner's answer to: will you write it?")
    p.add_argument("--answer", required=True, choices=ANSWERS)
    p.add_argument("--owner-said", required=True, help="the owner's words")
    p.add_argument("--dest", default="")
    p.add_argument("--want", default="", help="what the owner asked for: cv, statement, ...")
    p.add_argument("--reply", action="store_true", help="the owner's words or rule ask for a yes email")
    p.add_argument("--no-reply", action="store_true", help="decline without a reply")
    p.add_argument("--after-yes", action="store_true", help="the owner had said yes before")
    p = verb("collect", "save the request's attachments into the case folder")
    p.add_argument("--message-id", action="append", default=[])
    p.add_argument("--provider", default="outlook", choices=("outlook", "gmail"))
    p.add_argument("--account", default="")
    p = verb("extend", "a later destination joins an accepted case")
    p.add_argument("--dest", required=True)
    p = verb("require", "what a destination asks for")
    p.add_argument("--dest", required=True)
    p.add_argument("--length", default="")
    p.add_argument("--format", default="")
    p.add_argument("--addressee", default="")
    p.add_argument("--program", default="")
    p.add_argument("--organization", default="")
    p.add_argument("--question", default="")
    p.add_argument("--options", default="", help="a|b|c")
    p.add_argument("--rating", action="store_true")
    p = verb("answered", "the owner's answer to a form question")
    p.add_argument("--dest", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--owner-said", required=True)
    p = verb("brief", "the brief for the email-author subagent")
    p.add_argument("--dest", default="")
    p.add_argument("--notes-file", default="")
    p.add_argument("--owner-said", default="")
    p = verb("check", "questions, placeholders and other names in a draft")
    p.add_argument("--file", required=True)
    p.add_argument("--dest", default="")
    p.add_argument("--record", action="store_true")
    p = verb("variants", "one copy per destination where a name must change")
    p.add_argument("--base", required=True)
    p.add_argument("--base-dest", default="")
    p = verb("render", "the PDF (or .docx) of a letter")
    p.add_argument("--file", required=True)
    p.add_argument("--dest", default="")
    p.add_argument("--docx", action="store_true")
    p.add_argument("--template", default="")
    p = verb("approve", "the owner approved the text")
    p.add_argument("--dest", default="")
    p.add_argument("--owner-said", required=True)
    p = verb("submit", "how this destination's letter goes out")
    p.add_argument("--dest", required=True)
    return ap


def run(args, store) -> dict:
    v = args.verb
    if v == "status":
        return status(store, args.case)
    if v == "decide":
        return decide(store, args.case, args.answer, args.owner_said, dest=args.dest,
                      want=parse_want(args.want) or None, reply=args.reply, no_reply=args.no_reply,
                      after_yes=args.after_yes)
    if v == "collect":
        return collect(store, args.case, args.message_id, args.provider, args.account)
    if v == "extend":
        return extend(store, args.case, args.dest)
    if v == "require":
        return require(store, args.case, args.dest, length=args.length, fmt=args.format,
                       addressee=args.addressee, program=args.program, organization=args.organization,
                       question=args.question, options=args.options, rating=args.rating)
    if v == "answered":
        return answered(store, args.case, args.dest, args.question, args.value, args.owner_said)
    if v == "brief":
        return brief(store, args.case, dest=args.dest, notes_file=args.notes_file, owner_said=args.owner_said)
    if v == "check":
        return check(store, args.case, args.file, dest=args.dest, record=args.record)
    if v == "variants":
        return variants(store, args.case, args.base, base_dest=args.base_dest)
    if v == "render":
        return render(store, args.case, args.file, dest=args.dest, docx=args.docx, template=args.template)
    if v == "approve":
        return approve(store, args.case, args.owner_said, dest=args.dest)
    if v == "submit":
        return submit(store, args.case, args.dest)
    raise LetterError(f"no verb {v}")


def main(argv=None, store=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if store is None:
            try:
                store = open_store()
            except ImportError as e:
                raise LetterError(f"the letters store (letters.py) is not installed: {e}") from e
        out = run(args, store)
    except LetterError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
