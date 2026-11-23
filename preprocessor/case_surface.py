#!/usr/bin/env python3
"""Where the owner sees a piece of work someone asked of him and acts on
it: one line per case in the briefings, the reminders by date, the two
cards in Action requests, Home's item, the pre-send review's record of a
case and the name a project room gives it.

A work case (cases.py keeps them; letters.py keeps the letters, the first
kind) is one piece of work that ends with something delivered, not with a
reply: a letter for a person, a review or a referee report, a form to
fill in, a report or a section owed by a date. It has a counterpart (who
asked, or who it is about) and PARTS, each with its own due date, its way
of delivery and a state. Every surface shows the case as ONE item: its
parts' ledger rows and their mail are never items of their own, never a
reply owed, and a reply in one of their threads is never a sign that the
case is done. A new message from the other side on one of its threads is
shown on the case (news), since nothing else would show it. Only names,
parts, dates and states show here, never the work's text or the
materials, and those obey the keep levels of the parts' mail (keep_gate):

    full, summary   the counterpart, the kind, the parts by name
    facts           the counterpart (a header), no part names
    local           fixed words ("A kept review"), counts and dates

A kind is a declaration (case_kinds, builder A): its noun, its part noun,
its last state, its first decision and its reminder ladder. Every other
word the owner reads is here, in WORDS, one entry per core kind; a kind a
plugin declares gets words built from its declaration. Letters keep their
words exactly as they were (letter_surface.py is their thin layer, and it
keeps the hooks the letters' tests and lanes patch).

Code builds every line; no model writes one. With no store (an install
from before the cases, or one that cannot be read) every function here
gives nothing and each surface stays as it was.

One date per case, then quiet until it is time (case_dates.py): the
owner's yes carries the case's one date; a yes with no date takes the
earliest date its parts state (the store's, with one line that tells
him); only a case with parts and no date anywhere gets one date card. A dated case is quiet until its lead time (letters 7 days,
forms 3, reviews 10, reports 7): no card, no Home item, no line in the
daily briefing (the weekly review lists it). Then its block posts once,
"Time to write the letter", and again only when what it shows changed
(block_sig), on a loud rung of its nearest date (the day before, the day
itself, the day after) or for a new message. Once the work is ready the
block is the delivery list. A rung fires once; a finished case fires
nothing.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

OPEN_STATES = ("asked", "accepted", "drafted", "ready")
DECLINED = "declined"
# Home lists a case due this soon, an overdue one, a new one and one
# with no date; the rest stay on the briefing's list. Action requests
# holds a block for a case this close.
HOME_DAYS = 14
HOME_MINUTES = 30
SOON_DAYS = 3
# The most parts a card names one by one, and the buttons it offers.
CARD_DESTS = 9
CARD_BUTTONS = 3
# A message on a case's thread is news for this long, unless the owner
# wrote on that thread after it.
NEWS_HOURS = 48
# One heading for a list that holds cases of more than one kind.
MIXED_HEADING = "Work you owe"
MIXED_HEADING_DUE = "Work due"
# A sender that is a system: its mail is no news from a person.
_AUTO = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|donotreply|notifications?|"
                   r"mailer-daemon|postmaster)(?:[._+-]|@)", re.I)

_METHODS = {"portal": "portal link", "email": "by email", "upload": "you upload it",
            "unknown": "how to send it is not known"}
_MATERIALS = {"cv": "a CV", "statement": "a statement", "list": "a list of places",
              "draft": "a draft", "transcript": "a transcript", "sample": "a paper",
              "paper": "a paper", "document": "a document", "form": "a form",
              "instructions": "instructions", "template": "a template", "data": "data",
              "other": "other files"}


# ── the words per kind ────────────────────────────────────────────────

@dataclass(frozen=True)
class Words:
    """What the owner reads about a case of one kind. `prefix` names the
    card kinds, the action ids, Home's item kind and today_top's source:
    "letter" for letters (as they were), "case" for every other kind."""
    kind: str
    prefix: str = "case"
    noun: str = "Work"
    plural: str = "Work"
    kept: str = "A kept work item"
    subject: str = "a work item"            # the review's refusals: "a review"
    subkinds: dict = field(default_factory=dict)
    part: str = "part"
    parts: str = "parts"
    part_in_line: str = ""                   # a one-part line: "report due Dec 1"
    compact: bool = True                     # a one-part case gets the short line
    delivered: str = "delivered"             # the last state (A's Kind.delivered)
    delivered_word: str = "delivered"
    deliver_choice: str = "delivered"        # the answer and its action id
    part_key: str = "part"                   # a card value's key for a part
    no_parts: str = "no parts named yet"
    no_parts_card: str = "It names nothing to deliver yet."
    state_words: dict = field(default_factory=dict)
    method_words: dict = field(default_factory=lambda: dict(_METHODS))
    material_words: dict = field(default_factory=lambda: dict(_MATERIALS))
    question: str = "Will you do it?"
    # The first decision's buttons: (answer, the store's choice text).
    choices: tuple = (("yes", "Yes"), ("no", "No"))
    ask_title: str = ":memo: *A request for your work.* Will you do it?"
    home_yes: str = "Yes, I will do it"
    draft: str = "Draft it"
    change: str = "Change the draft"
    draft_ask: str = "what to say in it"
    delivered_button: str = "Delivered: {name}"
    delivered_another: str = "Delivered another one"
    missing_work: str = "the draft"
    missing_materials: str = ""
    link: str = "Open the link for {name}"
    heading: str = "Work you owe"
    heading_due: str = "Work due"
    home_label: str = "Work"
    home_label_new: str = "Work request"
    asked_many: str = "Do it and deliver {n}."
    asked_none: str = "Do it. No parts are named yet."
    next_draft: str = "Tacitus can draft it from the materials."
    next_deliver: str = "Tacitus can help you deliver it."
    join_title: str = ":memo: *Is this the same request?*"
    join_kept: str = "Two requests may be the same one."
    join_choices: tuple = ("Same one", "A different one")
    join_said: tuple = ("Recorded: one request now.", "Recorded: two requests.")
    rule: str = ("One piece of work the owner delivers. It is done only when every part is "
                 "delivered. A reply in its thread is not a delivery.")
    said: dict = field(default_factory=dict)
    decline_turn: bool = True                # a No writes a decline for the owner's Send
    ladder: tuple = (7, 3, 1, 0, -1)
    loud: frozenset = frozenset({1, 0, -1})
    guide: str = ""
    cli: str = "cases.py"
    # One date per case (case_dates): what the owner reads about it.
    place: str = ""                          # a part where the owner sends it ("" = `part`)
    places: str = ""
    work: str = "work"                       # what he makes: "the letter is ready"
    verb: str = "do it"                      # "Time to write the letter"
    when_q: str = "When will you do it"      # the date card's question


_STATES = {"asked": "not answered yet", "accepted": "to do", "drafted": "drafted",
           "ready": "ready to send", "declined": "declined"}

LETTER = Words(
    kind="letter", prefix="letter", noun="Letter", plural="Letters", kept="A kept letter case",
    subject="a letter case",
    subkinds={"recommendation": "Recommendation letter", "reference": "Reference letter",
              "nomination": "Nomination letter", "support": "Support letter",
              "character": "Character letter", "evaluation": "Evaluation letter"},
    part="destination", parts="destinations", compact=False,
    delivered="submitted", delivered_word="submitted", deliver_choice="submitted", part_key="dest",
    no_parts="no places named yet", no_parts_card="It names no places to send it yet.",
    state_words={"asked": "not answered yet", "accepted": "to write", "drafted": "drafted",
                 "ready": "ready to submit", "submitted": "submitted", "declined": "declined"},
    method_words={"portal": "portal link", "email": "by email", "upload": "you upload it",
                  "unknown": "how to submit is not known"},
    material_words={"cv": "a CV", "statement": "a statement", "list": "a list of places",
                    "draft": "a draft letter", "transcript": "a transcript", "sample": "a paper",
                    "other": "other files"},
    question="Will you write it?",
    choices=(("yes", "Yes"), ("yes_ask", "Yes, ask for materials"), ("no", "No")),
    ask_title=":memo: *A letter request.* Will you write it?", home_yes="Yes, I will write it",
    draft="Draft the letter", change="Change the letter",
    delivered_button="Submitted: {name}", delivered_another="Submitted another one",
    missing_work="the letter", missing_materials="the materials (CV, statement)",
    link="Open the portal for {name}",
    heading="Letters you are writing", heading_due="Letters due",
    home_label="Letter", home_label_new="Letter request",
    asked_many="Write and submit it to {n}.", asked_none="Write it. No places to send it are named yet.",
    next_draft="Tacitus can draft the letter from the materials.",
    next_deliver="Tacitus can help you submit it to the next destination.",
    join_title=":memo: *Is this the same person?*",
    join_kept="Two letter requests may be from the same person.",
    join_choices=("Same person", "Someone else"),
    join_said=("Recorded: one letter case now.", "Recorded: two people, two letter cases."),
    rule=("One letter the owner writes. It is done only when every destination is submitted. "
          "A reply in its thread is not a submission."),
    ladder=(7, 3, 1, 0, -1), guide="letters.md", cli="letters.py",
    place="place", places="places", work="letter", verb="write the letter",
    when_q="When will you write the letter")

REVIEW = Words(
    kind="review", noun="Review", plural="Reviews", kept="A kept review", subject="a review",
    subkinds={"paper": "Paper review", "manuscript": "Paper review", "proposal": "Proposal review",
              "contract": "Contract review", "draft": "Draft review", "thesis": "Thesis review",
              "application": "Application review"},
    part="report", parts="reports", part_in_line="report",
    delivered="returned", delivered_word="returned",
    no_parts="nothing to return named yet", no_parts_card="It names nothing to return yet.",
    state_words=dict(_STATES, accepted="to do", ready="ready to return", returned="returned"),
    question="Will you do this review?", choices=(("yes", "Yes"), ("no", "No")),
    ask_title=":memo: *A review request.* Will you do this review?", home_yes="Yes, I will review it",
    draft="Draft the report", change="Change the report", draft_ask="your points for the report",
    delivered_button="Returned: {name}", delivered_another="Returned another one",
    missing_work="the report", missing_materials="the files to review",
    link="Open the review site for {name}",
    heading="Reviews you are doing", heading_due="Reviews due",
    home_label="Review", home_label_new="Review request",
    asked_many="Review it and return {n}.", asked_none="Review it. Nothing to return is named yet.",
    next_draft="Tacitus can draft the report from your points.",
    next_deliver="Tacitus can help you return it.",
    join_title=":memo: *Is this the same review?*", join_kept="Two review requests may be the same one.",
    join_said=("Recorded: one review now.", "Recorded: two reviews."),
    rule=("One review the owner does. It is done only when every report is returned. "
          "A reply in its thread is not a return."),
    said={"draft": "I am writing a draft of the report from your points."},
    ladder=(14, 7, 3, 1, 0, -1), guide="reviews.md",
    work="report", verb="do the review", when_q="When will you do the review")

FORM = Words(
    kind="form", noun="Form", plural="Forms", kept="A kept form", subject="a form",
    subkinds={"survey": "Survey", "training": "Training", "registration": "Registration",
              "signature": "Signature", "disclosure": "Disclosure form", "evaluation": "Evaluation form"},
    part="form", parts="forms", part_in_line="",
    delivered="submitted", delivered_word="submitted",
    no_parts="nothing to fill in named yet", no_parts_card="It names nothing to fill in yet.",
    state_words=dict(_STATES, accepted="to fill in", drafted="filled in, not checked",
                     ready="ready to submit", submitted="submitted"),
    question="Will you fill it in?", choices=(("yes", "Yes"), ("no", "No")),
    ask_title=":memo: *A form to fill in.* Will you fill it in?", home_yes="Yes, I will fill it in",
    draft="Fill it in", change="Change the answers", draft_ask="anything to put in it",
    delivered_button="Submitted: {name}", delivered_another="Submitted another one",
    missing_work="the answers",
    heading="Forms to fill in", heading_due="Forms due",
    home_label="Form", home_label_new="Form request",
    asked_many="Fill in and submit {n}.", asked_none="Fill it in. Nothing to submit is named yet.",
    next_draft="Tacitus can fill in what your records show.",
    next_deliver="Tacitus can help you submit it.",
    join_title=":memo: *Is this the same form?*", join_kept="Two form requests may be the same one.",
    join_said=("Recorded: one form now.", "Recorded: two forms."),
    rule=("One form the owner fills in. It is done only when every form is submitted. "
          "A reply in its thread is not a submission."),
    said={"draft": "I am filling in what I can."},
    decline_turn=False, ladder=(7, 3, 1, 0, -1), guide="forms.md",
    work="form", verb="fill in the form", when_q="When will you fill it in")

REPORT = Words(
    kind="report", noun="Report", plural="Reports", kept="A kept report", subject="a report",
    subkinds={"progress": "Progress report", "annual": "Annual report", "final": "Final report",
              "section": "Section", "slides": "Slides", "deck": "Slides", "budget": "Budget"},
    part="part", parts="parts", part_in_line="",
    delivered="delivered", delivered_word="delivered",
    no_parts="no parts named yet", no_parts_card="It names no parts yet.",
    state_words=dict(_STATES, accepted="to write", drafted="drafted", ready="ready to send",
                     delivered="delivered"),
    question="Will you do it?",
    choices=(("yes", "Yes"), ("yes_ask", "Yes, ask for materials"), ("no", "No")),
    ask_title=":memo: *A report request.* Will you do it?", home_yes="Yes, I will do it",
    draft="Draft it", change="Change the draft",
    delivered_button="Delivered: {name}", delivered_another="Delivered another one",
    missing_work="the draft",
    heading="Reports you are writing", heading_due="Reports due",
    home_label="Report", home_label_new="Report request",
    asked_many="Write it and deliver {n}.", asked_none="Write it. No parts are named yet.",
    next_draft="Tacitus can draft it from the materials.",
    next_deliver="Tacitus can help you deliver it.",
    join_title=":memo: *Is this the same report?*", join_kept="Two report requests may be the same one.",
    join_said=("Recorded: one report now.", "Recorded: two reports."),
    rule=("One report the owner writes. It is done only when every part is delivered. "
          "A reply in its thread is not a delivery."),
    said={"draft": "I am writing the draft."},
    ladder=(14, 7, 3, 1, 0, -1), guide="reports.md",
    work="report", verb="write the report", when_q="When will you write the report")

WORDS = {w.kind: w for w in (LETTER, REVIEW, FORM, REPORT)}
_DECL: dict = {}


_NO_STORE: list = []


def _cases():
    """cases.py (the store for every kind), or None on an install without
    it (looked for once: a list of rows asks for it per row)."""
    mod = sys.modules.get("cases")
    if mod is not None:
        return mod
    if _NO_STORE:
        return None
    try:
        import cases  # type: ignore
        return cases
    except Exception:
        _NO_STORE.append(True)
        return None


def _dates():
    """case_dates: the one-date rules every surface shares."""
    try:
        import case_dates  # type: ignore
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import case_dates  # type: ignore
    return case_dates


def stage(case: dict, today: date) -> str:
    """The case's stage today (case_dates.STAGES): new, undated, quiet,
    due, ready or closed."""
    return _dates().stage(_tag(case, kind_of(case)), today)


def wake(case: dict) -> date | None:
    """The day a dated case comes back to the owner's lists."""
    return _dates().wake(_tag(case, kind_of(case)))


def waits(case: dict, today: date) -> bool:
    """A case off Home and the daily lists today: quiet (dated, before its
    lead time, not overdue), or a yes with no place named and no date (its
    one date card asked; the weekly review lists it)."""
    st = stage(case, today)
    if st == "quiet":
        return not facts(case, today)["overdue"]
    return st == "undated" and not parts(case)


def dated_yes(w: "Words") -> bool:
    """Whether the kind's yes carries the case's one date (its
    declaration's `Decision.dated`; every core kind's does)."""
    dec = _get(_decl(w.kind), "decision")
    texts = _get(dec, "dated")
    yes = dict(w.choices).get("yes", "Yes")
    return yes in (texts if texts is not None else ("Yes",))


def place_words(w: "Words") -> tuple[str, str]:
    """What the owner calls a part where the work goes: (one, many)."""
    return (w.place or w.part, w.places or w.parts)


def _ls():
    """letter_surface: the letters' own layer, imported the way this module
    was (a test or a lane patches its hooks there)."""
    if __package__:
        from . import letter_surface as mod
    else:
        import letter_surface as mod
    return mod


def _decl(kind: str):
    """A's declaration of a kind (case_kinds.Kind), or None; read once."""
    if kind in _DECL:
        return _DECL[kind]
    got = None
    mod = _cases()
    try:
        if mod is not None and hasattr(mod, "kind"):
            got = mod.kind(kind)
    except Exception:
        got = None
    if got is None:
        try:
            import case_kinds  # type: ignore
            got = case_kinds.core_kinds().get(kind)
        except Exception:
            got = None
    _DECL[kind] = got
    return got


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _choices_of(decision) -> tuple:
    """A declaration's first decision as the cards' buttons: a choice in its
    `no` declines; one with a next step (`then`) is the asking yes."""
    texts = [str(c) for c in _get(decision, "choices") or () if str(c).strip()]
    if not texts:
        return ()
    no = {str(c) for c in _get(decision, "no") or texts[-1:]}
    then = _get(decision, "then") or {}
    out, used = [], set()
    for t in texts:
        a = "no" if t in no else ("yes_ask" if t in then else "yes")
        if a in used:
            continue
        used.add(a)
        out.append((a, t))
    return tuple(out) if any(a == "yes" for a, _t in out) else ()


def words(kind: str) -> Words:
    """The words for a kind: the core kinds' own, a plugin kind's built
    from its declaration; the declaration's data (its last state, its
    decision, its ladder) wins over the defaults."""
    kind = str(kind or "letter")
    base = WORDS.get(kind)
    d = _decl(kind)
    if base is None:
        noun = str(_get(d, "noun") or kind.replace("_", " ").title())
        part = str(_get(d, "part_noun") or "part")
        base = Words(kind=kind, noun=noun, plural=noun + "s", kept=f"A kept {noun.lower()}",
                     subject=f"a {noun.lower()}", part=part,
                     parts=str(_get(d, "part_nouns") or part + "s"),
                     heading=f"{noun}s you owe", heading_due=f"{noun}s due", home_label=noun,
                     home_label_new=f"{noun} request",
                     ask_title=f":memo: *A {noun.lower()} request.* Will you do it?",
                     join_title=f":memo: *Is this the same {noun.lower()}?*",
                     rule=(f"One {noun.lower()} the owner delivers. It is done only when every "
                           f"{part} is delivered. A reply in its thread is not a delivery."),
                     guide=str(_get(d, "guide") or ""))
    if d is None:
        return base
    over = {}
    delivered = str(_get(d, "delivered") or "")
    if delivered and delivered != base.delivered:
        over["delivered"] = delivered
        if base.kind not in WORDS:
            over["delivered_word"] = delivered.replace("_", " ")
    dec = _get(d, "decision")
    if dec is not None:
        q = str(_get(dec, "question") or "")
        if q and base.kind not in WORDS:
            over["question"] = q
            over["ask_title"] = f":memo: *A {base.noun.lower()} request.* {q}"
        picks = _choices_of(dec)
        if picks:
            over["choices"] = picks
    ladder = tuple(int(x) for x in _get(d, "ladder") or () if isinstance(x, int))
    if ladder:
        over["ladder"] = ladder
    loud = _get(d, "loud")
    if loud:
        over["loud"] = frozenset(int(x) for x in loud if isinstance(x, int))
    return replace(base, **over) if over else base


def names() -> list[str]:
    """Every kind the install knows: the store's registry, else the core
    kinds here."""
    mod = _cases()
    try:
        got = mod.kinds() if mod is not None and hasattr(mod, "kinds") else None
    except Exception:
        got = None
    if isinstance(got, dict):
        out = [str(k) for k in got]
    elif isinstance(got, (list, tuple)):
        out = [str(_get(k, "name") or k) for k in got]
    else:
        out = list(WORDS)
    return list(dict.fromkeys(["letter"] + out))


def kind_of(case: dict) -> str:
    """The case's kind: its own `case_kind` (set as it is loaded), a known
    kind in `kind`, or a letter (letters.py names the letter's own kind,
    recommendation or reference, in `kind`)."""
    if not isinstance(case, dict):
        return "letter"
    k = str(case.get("case_kind") or "")
    if k:
        return k
    k = str(case.get("kind") or "")
    if "destinations" in case or "person_name" in case:
        return "letter"
    if k in WORDS or (k and k in names()):
        return k
    return "letter"


def words_of(case: dict) -> Words:
    return words(kind_of(case))


def subkind(case: dict) -> str:
    """The case's own kind within its kind: the letter's (recommendation),
    the review's (paper)."""
    sub = str(case.get("subkind") or "")
    if sub:
        return sub.lower()
    k = str(case.get("kind") or "")
    return k.lower() if kind_of(case) == "letter" and k != "letter" else ""


# ── the store ──────────────────────────────────────────────────────────

def _connect():
    """The ledger database, read-only, rows by name (as cases.py reads)."""
    import sqlite3
    import ledger
    conn = ledger.guarded_connect(ledger.DB_PATH, read_only=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tag(c: dict, kind: str) -> dict:
    return c if c.get("case_kind") == kind else dict(c, case_kind=kind)


def load_cases(conn=None, *, open_only: bool = True, kinds=None) -> list[dict]:
    """The cases of every kind (or of `kinds`), each with its `case_kind`:
    letters as letters.cases gives them (through letter_surface), every
    other kind as cases.cases gives it. [] for a kind whose store is
    missing or cannot be read (logged), so no surface ever fails on it."""
    want = set(kinds) if kinds else None
    out: list[dict] = []
    if want is None or "letter" in want:
        try:
            out += [_tag(c, "letter") for c in _ls().load_cases(conn, open_only=open_only) or []
                    if isinstance(c, dict)]
        except Exception as e:
            print(f"case_surface: the letter cases could not be read: {str(e)[:160]}", file=sys.stderr)
    others = [k for k in names() if k != "letter" and (want is None or k in want)]
    mod = _cases() if others else None
    if mod is None or not hasattr(mod, "cases"):
        return out
    own = conn is None
    try:
        conn = conn or _connect()
        for k in others:
            got = mod.cases(conn, kind=k, open_only=open_only)
            out += [_tag(c, k) for c in got or [] if isinstance(c, dict) and (c.get("case_kind") or k) == k]
    except Exception as e:
        print(f"case_surface: the work cases could not be read: {str(e)[:160]}", file=sys.stderr)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def other_kinds() -> list[str]:
    return [k for k in names() if k != "letter"]


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


# The keys a ledger row's metadata names its case under (cases.py).
ROW_KEYS = ("work_case", "letter")


def _row_case(row: dict) -> dict:
    """The case record on a row (`work_case` for a work case, `letter` for
    a letter), {} for none."""
    for key in ROW_KEYS:
        rec = row.get(key) if isinstance(row.get(key), dict) else _meta(row).get(key)
        if isinstance(rec, dict) and (rec.get("case") or rec.get("id")):
            return rec
    return {}


def is_case_row(row: dict) -> bool:
    """A ledger row of a case of any kind: a letter's (letter_surface), or
    cases.is_case_row, or the row's own case record when it is absent."""
    if not isinstance(row, dict):
        return False
    try:
        if _ls().is_letter_row(row):
            return True
    except Exception:
        pass
    mod = _cases()
    if mod is not None and hasattr(mod, "is_case_row"):
        try:
            return bool(mod.is_case_row(row))
        except Exception:
            pass
    return bool(_row_case(row))


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


def parts(case: dict) -> list[dict]:
    got = case.get("parts")
    if not isinstance(got, list):
        got = case.get("destinations")
    return [d for d in got or [] if isinstance(d, dict)]


def open_parts(case: dict) -> list[dict]:
    """The parts still to do, nearest due date first (a part's own date,
    else the case's one date), undated last; the store's own order within
    one date."""
    D = _dates()
    out = [d for d in parts(case) if (d.get("state") or "asked") in OPEN_STATES]
    return sorted(out, key=lambda d: D.due_of(d, case) or date.max)


def _delivered(d: dict) -> bool:
    s = d.get("state") or "asked"
    return s not in OPEN_STATES and s != DECLINED


def is_new(case: dict) -> bool:
    """No answer from the owner yet: the case records no decision and
    every part (if it has any yet) is still `asked`."""
    if case.get("decision"):
        return False
    return all((d.get("state") or "asked") == "asked" for d in parts(case))


def is_open(case: dict) -> bool:
    """An open case with something to do: a part to finish, or no part yet
    (the request alone, or a yes with no places named yet)."""
    if (case.get("status") or "open") != "open" or case.get("decision") == "declined":
        return False
    return bool(open_parts(case)) or not parts(case)


def questions(case: dict, kind: str = "") -> list[dict]:
    """The case's open questions for the owner (the first decision, and
    `join`: whether two requests are one)."""
    return [q for q in case.get("questions") or [] if isinstance(q, dict)
            and not q.get("answer") and (not kind or q.get("kind") == kind)]


def decision_question(case: dict) -> dict | None:
    """The open question of the case's first decision (letters: `write`)."""
    return next((q for q in questions(case) if q.get("kind") not in ("join", "part")), None)


def facts(case: dict, today: date) -> dict:
    """{total, submitted (the parts delivered), declined, open, next_due,
    due_next, overdue, undated, new, finished} for one case, in code. A
    part's date is its own, else the case's one date (case_dates)."""
    D = _dates()
    ds = parts(case)
    opn = open_parts(case)
    dated = [(d, D.due_of(d, case, today)) for d in opn]
    nxt = min((due for _d, due in dated if due), default=None)
    if nxt is None and not ds:
        nxt = D.do_by(case)
    done = sum(1 for d in ds if _delivered(d))
    return {"total": len(ds), "submitted": done, "delivered": done,
            "declined": sum(1 for d in ds if d.get("state") == DECLINED),
            "open": opn, "next_due": nxt,
            "due_next": [d for d, due in dated if due and due == nxt],
            "overdue": [d for d, due in dated if due and due < today],
            "undated": [d for d, due in dated if not due],
            "new": is_new(case),
            "finished": bool(ds) and not opn and done > 0 and not case.get("decision") == "declined"}


def ledger_ids(case: dict, *, open_only: bool = True) -> list[str]:
    """The ledger rows the case stands for: its parts' (the open ones with
    `open_only`) and the request's own row (`case_ledger_id`: it closes
    with the case)."""
    pool = open_parts(case) if open_only else parts(case)
    ids = [str(d["ledger_id"]) for d in pool if d.get("ledger_id")]
    if case.get("case_ledger_id"):
        ids.append(str(case["case_ledger_id"]))
    return list(dict.fromkeys(ids))


def level(case: dict, rows: dict | None = None, *, records=None) -> str:
    """The case's keep level: the strictest of its own `keep_level` and
    its parts' rows (keep_gate.levels; `rows` maps ledger id to row,
    `records` as keep_gate takes it, False for the rows' own labels
    alone). A row that cannot be checked counts as mail kept on the
    machine."""
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
    """The counterpart's name as the case gives it ("" at `local`)."""
    if lv == "local":
        return ""
    return _clean(case.get("counterpart_name") or case.get("person_name")
                  or case.get("counterpart_email") or case.get("person_email") or "", 60)


def _label(case: dict, lv: str, w: Words, noun: str) -> str:
    """`noun` for the counterpart ("Review for Pat Lee"). A letter names
    its person (or an address); any other kind names who asked, or with
    no one named its title ("Form: Annual disclosure", as cases.py does),
    which a level below summary holds back. Fixed words when kept."""
    if w.kind == "letter":
        who = person(case, lv)
        return f"{noun} for {who}" if who else w.kept
    who = _clean(case.get("counterpart_name") or "", 60) if lv != "local" else ""
    if who:
        return f"{noun} for {who}"
    title = _clean(case.get("title"), 80) if lv in ("full", "summary") else ""
    if title:
        return f"{noun}: {title}"
    return w.kept if _kept(lv) else f"{noun} for someone not named yet"


def name(case: dict, lv: str = "full", w: Words | None = None) -> str:
    """The case named by its kind and counterpart: "Recommendation letter
    for Dana Reyes", "Paper review for Pat Lee"; fixed words at `local`."""
    w = w or words_of(case)
    return _label(case, lv, w, w.subkinds.get(subkind(case), w.noun))


def head(case: dict, lv: str, w: Words) -> str:
    """The line's head: "Letter for Dana Reyes", "Review for Pat Lee"."""
    return _label(case, lv, w, w.noun)


def rest_of(text: str, hd: str) -> str:
    """A line without its head: what follows "<head>: "."""
    if hd and text.startswith(hd + ": "):
        return text[len(hd) + 2:]
    return text.partition(": ")[2]


def _from(case: dict, lv: str, w: Words) -> str:
    """A card's From field: who asked, or fixed words."""
    who = person(case, lv) if w.kind == "letter" else (
        _clean(case.get("counterpart_name") or "", 60) if lv != "local" else "")
    return who or ("A kept message" if _kept(lv) or w.kind == "letter" else "Not named yet")


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


def _part_phrase(d: dict, today: date, w: Words) -> str:
    """One part in a short line: "report due Tue Dec 1", "due Fri Nov 27,
    drafted", "report, no due date yet"."""
    due = _date(d.get("due"))
    lead = f"{w.part_in_line} " if w.part_in_line else ""
    if due and due < today:
        out = f"{lead}was due {day_words(due, today)}"
    elif due:
        out = f"{lead}due {day_words(due, today)}"
    else:
        out = (f"{w.part_in_line}, no due date yet" if w.part_in_line else "no due date yet")
    state = d.get("state") or "asked"
    if state in ("drafted", "ready"):
        out += f", {w.state_words.get(state, state)}"
    return out


def _own_line(case: dict, f: dict, today: date, lv: str, w: Words | None = None) -> str:
    """The line in code's words: "Letter for Dana Reyes: 3 of 9 submitted.
    Next due Tue Dec 1: 2 destinations."; a one-part case of a compact
    kind: "Review for Pat Lee: report due Tue Dec 1."."""
    w = w or words_of(case)
    hd = head(case, lv, w)
    if w.compact and f["total"] == 1 and (f["open"] or f["new"]):
        d = (f["open"] or parts(case))[0]
        body = _part_phrase(d, today, w)
        if f["new"]:
            opened = _date(case.get("opened"))
            body = (f"asked {opened:%b} {opened.day}, " if opened else "a new request, ") + body
        return f"{hd}: {body}."
    if f["new"]:
        opened = _date(case.get("opened"))
        body = f"asked {opened:%b} {opened.day}" if opened else "a new request"
        body += f", {_n(f['total'], w.part, w.parts)}" if f["total"] else f", {w.no_parts}"
    else:
        body = f"{f['submitted']} of {f['total'] - f['declined']} {w.delivered_word}"
    out = f"{hd}: {body}."
    if f["overdue"]:
        late = min(_date(d.get("due")) for d in f["overdue"])
        out += f" {len(f['overdue'])} {'was' if len(f['overdue']) == 1 else 'were'} due {day_words(late, today)}."
    ahead = sorted(due for due in (_date(d.get("due")) for d in f["open"]) if due and due >= today)
    if ahead:
        n = sum(1 for d in f["open"] if _date(d.get("due")) == ahead[0])
        out += f" Next due {day_words(ahead[0], today)}: {_n(n, w.part, w.parts)}."
    elif not f["next_due"]:
        out += " No due date yet."
    return out


def _store_line(case: dict, today: date, lv: str) -> str:
    """letters.summary_line's words for a letter case ("" when the store has
    none, or for kept mail whose line would name the person)."""
    mod = _ls()._letters()
    if mod is None or not hasattr(mod, "summary_line"):
        return ""
    try:
        base = _clean(mod.summary_line(case, level=lv, today=today), 300)
    except TypeError:
        base = "" if _kept(lv) else _clean(mod.summary_line(case), 300)
    except Exception:
        base = ""
    if _kept(lv) and person(case, "full") and person(case, "full") in base:
        base = ""  # a store's line that names the person never shows for kept mail
    return base


def line(case: dict, today: date, *, lv: str = "full", news=None, w: Words | None = None) -> str:
    """The case's one line wherever a list names it. A letter takes
    letters.summary_line's words when the store has them; every other
    case, a kept one, or an install without it, gets code's own words at
    its level. A new case adds where to answer; a case with no date asks
    for one on its first day only; a quiet case says its date and the day
    it comes back; `news` (news()) ends it with who wrote."""
    w = w or words_of(case)
    f = facts(case, today)
    st = stage(case, today)
    if st == "quiet":
        base = quiet_line(case, today, lv, w)
    else:
        base = _store_line(case, today, lv) if w.kind == "letter" else ""
        base = base or _own_line(case, f, today, lv, w)
    if not base.endswith("."):
        base += "."
    if f["new"]:
        q = w.question
        base += (" Answer in Action requests." if q.lower().rstrip("?") in base.lower()
                 else f" {q} Answer in Action requests.")
    elif st == "undated" and _asks_date(case, today):
        base += " Pick its date in Action requests."
    if news:
        base += " " + news_words(news, lv)
    return base


def quiet_line(case: dict, today: date, lv: str = "full", w: Words | None = None) -> str:
    """A dated case out of sight: "Letter for Dana Reyes: by Tue Dec 1, 4
    places. Back on your list Tue Nov 24."."""
    w = w or words_of(case)
    f = facts(case, today)
    one, many = place_words(w)
    n = f["total"] - f["declined"]
    by = _dates().do_by(case) or f["next_due"]
    body = f"by {day_words(by, today)}" if by else "no date yet"
    if n:
        body += f", {_n(n, one, many)}"
    if f["submitted"]:
        body += f", {f['submitted']} {w.delivered_word}"
    back = wake(case)
    out = f"{head(case, lv, w)}: {body}."
    if back and back > today:
        out += f" Back on your list {day_words(back, today)}."
    return out


def _asks_date(case: dict, today: date) -> bool:
    """A case with an undated part asks for the date once: on the day it
    opened or the day after (the first briefing that sees it)."""
    opened = _date(case.get("opened"))
    return bool(opened) and 0 <= (today - opened).days <= 1


# ── news: a new message from the other side ───────────────────────────

def _when(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def threads(case: dict, rows: dict | None = None) -> set[str]:
    """The conversation ids of the case's mail: its parts' and its rows'."""
    out = {str(d.get("conversation_id")) for d in parts(case) if d.get("conversation_id")}
    for i in ledger_ids(case, open_only=False):
        r = (rows or {}).get(i) or {}
        if r.get("conversation_id"):
            out.add(str(r["conversation_id"]))
    return out


def messages(case: dict, rows: dict | None = None) -> list[dict]:
    """The inbound mail on the case's threads, oldest first: the case's own
    record (`messages`, cases.py) when it has one, else what its rows'
    thread briefs hold. [{"at", "from", "name", "conversation_id",
    "message_id"}]."""
    if isinstance(case.get("messages"), list):
        got = [m for m in case["messages"] if isinstance(m, dict) and m.get("at")]
        return sorted(got, key=lambda m: str(m.get("at")))
    out, seen = [], set()
    for i in ledger_ids(case, open_only=False):
        brief = ((rows or {}).get(i) or {}).get("thread_brief")
        if not isinstance(brief, dict):
            continue
        conv = str(brief.get("conversation_id") or (rows or {}).get(i, {}).get("conversation_id") or "")
        for m in brief.get("messages") or []:
            if not isinstance(m, dict) or m.get("own") or not m.get("received"):
                continue
            key = m.get("id") or (conv, m.get("received"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"at": str(m["received"]), "from": str(m.get("from") or ""), "name": "",
                        "conversation_id": conv, "message_id": str(m.get("id") or "")})
    return sorted(out, key=lambda m: m["at"])


def _mail_item(m: dict) -> dict:
    """A run's inbox message (Graph's shape or the briefing's flat one) as
    a news entry."""
    frm = m.get("from")
    addr = name_ = ""
    if isinstance(frm, dict):
        ea = frm.get("emailAddress") if isinstance(frm.get("emailAddress"), dict) else frm
        addr, name_ = str(ea.get("address") or ""), str(ea.get("name") or "")
    else:
        addr, name_ = str(frm or ""), str(m.get("from_name") or "")
    return {"at": str(m.get("receivedDateTime") or m.get("received") or ""), "from": addr,
            "name": name_, "conversation_id": str(m.get("conversationId") or m.get("conversation_id") or ""),
            "message_id": str(m.get("id") or m.get("message_id") or "")}


def news(case: dict, rows: dict | None = None, *, since="", lv: str = "full", mail=None,
         now: datetime | None = None) -> list[dict]:
    """The messages on the case's threads after `since` (an ISO time; the
    last NEWS_HOURS by default) that the owner has not written after on
    that thread, from a person (not a system address). `mail` adds a
    run's new inbox messages. Each is {"from": the sender's name at the
    case's keep level ("" at local), "at", "conversation_id",
    "message_id"}, oldest first."""
    convs = threads(case, rows)
    if not convs:
        return []
    now = now or datetime.now(timezone.utc)
    cut = _when(since) or (now - timedelta(hours=NEWS_HOURS))
    pool = messages(case, rows) + [x for x in (_mail_item(m) for m in mail or [] if isinstance(m, dict))
                                   if x["conversation_id"] in convs]
    owner_last: dict[str, datetime] = {}
    for i in ledger_ids(case, open_only=False):
        brief = ((rows or {}).get(i) or {}).get("thread_brief")
        if isinstance(brief, dict) and _when(brief.get("last_from_user")):
            conv = str(brief.get("conversation_id") or (rows or {}).get(i, {}).get("conversation_id") or "")
            at = _when(brief["last_from_user"])
            if conv and (conv not in owner_last or at > owner_last[conv]):
                owner_last[conv] = at
    mine = str(case.get("counterpart_email") or case.get("person_email") or "").lower()
    out, seen = [], set()
    for m in sorted(pool, key=lambda m: str(m.get("at"))):
        at = _when(m.get("at"))
        if at is None or at <= cut:
            continue
        conv = str(m.get("conversation_id") or "")
        if conv and conv not in convs:
            continue
        if owner_last.get(conv) and owner_last[conv] >= at:
            continue  # he wrote on that thread after it
        addr = str(m.get("from") or "")
        if _AUTO.search(addr):
            continue
        key = m.get("message_id") or (conv, m.get("at"))
        if key in seen:
            continue
        seen.add(key)
        who = ""
        if lv != "local":
            who = (person(case, lv) if mine and addr.lower() == mine else
                   _clean(m.get("name") or "", 60) or _clean(addr, 60))
        out.append({"from": who, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "conversation_id": conv,
                    "message_id": str(m.get("message_id") or "")})
    return out


def news_words(items: list[dict], lv: str = "full") -> str:
    """"New message from Avery Quinn."; "New messages from A and B.";
    fixed words when the case is kept on the machine."""
    if not items:
        return ""
    many = len(items) > 1
    who = list(dict.fromkeys(i.get("from") or "" for i in items if i.get("from")))
    if lv == "local" or not who:
        return "New messages came." if many else "A new message came."
    names_ = who[0] if len(who) == 1 else (f"{who[0]} and {who[1]}" if len(who) == 2
                                           else f"{who[0]} and {len(who) - 1} others")
    return f"New message{'s' if many else ''} from {names_}."


def news_marks(cases: list[dict], rows: dict | None = None, *, levels: dict | None = None,
               now: datetime | None = None) -> dict:
    """{case id: {"at": the newest message's time, "from", "words"}} for the
    cases with news (the bot posts a case's block again on one)."""
    out = {}
    for c in cases:
        cid = str(c.get("id") or "")
        if not cid or not is_open(c):
            continue
        lv = (levels or {}).get(cid) or level(c, rows or {}, records=False)
        got = news(c, rows, lv=lv, now=now)
        if got:
            out[cid] = {"at": got[-1]["at"], "from": got[-1]["from"], "words": news_words(got, lv),
                        "items": got}
    return out


# ── the briefing JSON (preprocess.py) ─────────────────────────────────

def briefing_view(open_rows: list[dict], *, today: date | None = None, cases=None,
                  closed=None, mail=None, row_test=None, now: datetime | None = None) -> dict:
    """What the briefing lanes read about the cases, built once in the
    preprocessor: {"cases": [entry], "finished": [entry], "hide": {"ids",
    "convs", "mids"}}, {} with no open case. Each entry is the case's
    line, name, dates and counts at its keep level (the rows carry their
    level, preprocess._mark_keep), and its news (`mail`: the run's new
    inbox messages); `hide` names every row and thread the cases stand
    for."""
    today = today or today_local()
    cases = load_cases() if cases is None else cases
    test = row_test or is_case_row
    rows = {str(r.get("id")): r for r in open_rows or [] if isinstance(r, dict) and r.get("id")}
    hide_ids = {i for i, r in rows.items() if test(r)}
    entries = []
    for c in cases:
        if not is_open(c):
            continue
        entries.append(entry(c, today, rows, mail=mail, now=now))
        hide_ids |= set(ledger_ids(c, open_only=False))
    if closed is None:
        closed = [c for c in load_cases(open_only=False) if facts(c, today)["finished"]]
    finished = []
    for c in closed or []:
        when = max((_date(d.get("submitted_at") or d.get("delivered_at")) for d in parts(c)
                    if _date(d.get("submitted_at") or d.get("delivered_at"))),
                   default=None) or _date(c.get("closed_at"))
        if facts(c, today)["finished"] and when and (today - when).days <= 1:
            lv = level(c, rows, records=False)
            w = words_of(c)
            got = {"case": str(c.get("id") or ""), "name": name(c, lv, w), "total": facts(c, today)["submitted"]}
            if w.kind != "letter":
                got["case_kind"] = w.kind
            finished.append(got)
    if not entries and not finished and not hide_ids:
        return {}
    convs = {str(rows[i].get("conversation_id")) for i in hide_ids
             if i in rows and rows[i].get("conversation_id")}
    convs |= {str(d["conversation_id"]) for c in cases for d in parts(c) if d.get("conversation_id")}
    mids = {str(rows[i].get("source_message_id")) for i in hide_ids
            if i in rows and rows[i].get("source_message_id")}
    for c in cases:
        for m in c.get("materials") or []:
            ref = str((m or {}).get("ref") or "") if isinstance(m, dict) else ""
            if ref and "/" not in ref and not ref.endswith((".pdf", ".docx", ".doc", ".txt")):
                mids.add(ref)  # the counterpart's own mail, kept as a message id
    entries.sort(key=lambda e: (e["next_due"] or "9999-99-99", e["name"]))
    return {"cases": entries, "finished": finished,
            "hide": {"ids": sorted(hide_ids), "convs": sorted(convs), "mids": sorted(mids)}}


def entry(case: dict, today: date, rows: dict | None = None, *, records=False, mail=None,
          since="", now: datetime | None = None) -> dict:
    """One case as the briefing lanes read it."""
    w = words_of(case)
    lv = level(case, rows or {}, records=records)
    f = facts(case, today)
    ids = ledger_ids(case)
    got_news = news(case, rows, since=since, lv=lv, mail=mail, now=now)
    key = "destinations" if w.kind == "letter" else "parts"
    out = {"case": str(case.get("id") or ""), "line": line(case, today, lv=lv, news=got_news, w=w),
           "name": name(case, lv, w), "person": person(case, lv), "head": head(case, lv, w),
           "kind": str(case.get("kind") or "") if w.kind == "letter" else w.kind,
           "case_kind": w.kind,
           "next_due": f["next_due"].isoformat() if f["next_due"] else "",
           "due_in": (f["next_due"] - today).days if f["next_due"] else None,
           "overdue": len(f["overdue"]), "submitted": f["submitted"], "total": f["total"],
           "undated": len(f["undated"]), "new": f["new"], "keep_level": lv if _kept(lv) else "",
           "ledger_ids": ids,
           "conversation_ids": sorted({str((rows or {}).get(i, {}).get("conversation_id"))
                                       for i in ids if (rows or {}).get(i, {}).get("conversation_id")}),
           key: [{"name": _clean(d.get("name"), 80) if lv in ("full", "summary") else "",
                  "due": str(d.get("due") or "")[:10], "state": d.get("state") or "asked"}
                 for d in open_parts(case)][:CARD_DESTS]}
    st = stage(case, today)
    back = wake(case)
    out.update(stage=st, do_by=str(case.get("do_by") or ""),
               back_on=back.isoformat() if back and st == "quiet" else "", waits=waits(case, today))
    if w.kind != "letter":
        out["subkind"] = subkind(case)
    if got_news:
        out["news"] = [{"from": n["from"], "at": n["at"]} for n in got_news][-3:]
    return out


def _hidden(item: dict, hide: dict, test=None) -> bool:
    ids, convs, mids = (set(hide.get(k) or ()) for k in ("ids", "convs", "mids"))
    return (str(item.get("id") or item.get("ledger_id") or "") in ids
            or str(item.get("ref") or "") in ids | mids
            or str(item.get("conversation_id") or "") in convs - {""}
            or str(item.get("message_id") or item.get("source_message_id") or "") in mids - {""}
            or (test or is_case_row)(item))


def without(view: dict, *lists, test=None):
    """Each list without the items a case stands for (its rows, its
    threads, its mail): the inputs the ranking reads."""
    hide = (view or {}).get("hide") or {}
    if not hide:
        return lists
    return tuple([x for x in (lst or []) if not (isinstance(x, dict) and _hidden(x, hide, test))]
                 for lst in lists)


# The briefing JSON's lists the cases stand in for.
_STRIP = ("new_needs_response", "new_dropped_threads", "deadline_items", "open_obligations",
          "stale_obligations", "resurrected_follow_ups", "reply_backlog", "today_top",
          "everything_else", "handled_by_reply")


def score(e: dict) -> tuple[int, str]:
    """A case's place in today_top, as preprocess.build_today_top scores
    a dated obligation: overdue, due within 2 days, within 7."""
    w = words(e.get("case_kind") or "letter")
    noun = w.noun.lower()
    n = e.get("due_in")
    if e.get("overdue"):
        return 75, f"{noun} overdue ({_n(e['overdue'], w.part, w.parts)})"
    if n is None:
        return 5, f"{noun} with no due date"
    if n <= 2:
        return 65, f"{noun} due {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'}"
    if n <= 7:
        return 30, f"{noun} due in {n} days"
    return 5, f"{noun} due in {n} days (not urgent yet)"


def apply_briefing(briefing: dict, view: dict, *, cap: int = 5, test=None) -> dict:
    """The briefing JSON with the cases in it: `letter_cases` (one entry
    per open letter case) and `work_cases` (every other kind), their
    `_finished` lists (the cases whose last part was delivered since
    yesterday), `quiet_cases` (dated cases before their lead time, every
    kind: the weekly review's), the rows and threads every case stands for
    taken out of every list, and the cases due soon ranked into today_top
    (ref `letter:<case id>` or `case:<case id>`)."""
    if not view:
        return briefing
    hide = view.get("hide") or {}
    quiet = [e for e in view.get("cases") or [] if e.get("waits")]
    view = dict(view, cases=[e for e in view.get("cases") or [] if not e.get("waits")])
    for key in _STRIP:
        if isinstance(briefing.get(key), list):
            briefing[key] = [x for x in briefing[key] if not (isinstance(x, dict) and _hidden(x, hide, test))]
    top = list(briefing.get("today_top") or [])
    for e in view.get("cases") or []:
        s, why = score(e)
        if s >= 30:
            prefix = words(e.get("case_kind") or "letter").prefix
            top.append({"source": prefix, "title": e["line"][:100], "ref": f"{prefix}:{e['case']}",
                        "counterparty": e.get("person") or "", "score": s, "reason": why})
    top.sort(key=lambda t: -int(t.get("score") or 0))
    briefing["today_top"], rest = top[:cap], top[cap:]
    if rest:
        briefing["everything_else"] = [{"source": t.get("source"), "title": str(t.get("title"))[:80],
                                        "score": t.get("score")} for t in rest] \
            + list(briefing.get("everything_else") or [])
    every = list(view.get("cases") or [])
    done = list(view.get("finished") or [])
    briefing["letter_cases"] = [e for e in every if (e.get("case_kind") or "letter") == "letter"]
    briefing["letter_cases_finished"] = [e for e in done if (e.get("case_kind") or "letter") == "letter"]
    work = [e for e in every if (e.get("case_kind") or "letter") != "letter"]
    work_done = [e for e in done if (e.get("case_kind") or "letter") != "letter"]
    if work or "work_cases" in briefing:
        briefing["work_cases"] = work
    if work_done or "work_cases_finished" in briefing:
        briefing["work_cases_finished"] = work_done
    # Dated and not started: on no daily list until its lead time (the
    # weekly review lists them by date); their rows stay hidden above.
    briefing["quiet_cases"] = sorted(quiet, key=lambda e: (e.get("back_on") or "9999", e.get("name") or ""))
    return briefing


def briefing_entries(b: dict, *, quiet: bool = False) -> list[dict]:
    """Every case entry a briefing JSON carries: letters, then the rest;
    with `quiet`, the quiet cases after them (the weekly review lists
    those by date; a daily post never does)."""
    keys = ("letter_cases", "work_cases") + (("quiet_cases",) if quiet else ())
    return [e for key in keys for e in b.get(key) or []
            if isinstance(e, dict) and e.get("case")]


def finished_entries(b: dict) -> list[dict]:
    return [e for key in ("letter_cases_finished", "work_cases_finished") for e in b.get(key) or []
            if isinstance(e, dict)]


# ── the briefing post (remote_briefing.py, remote_audit.py) ───────────

def candidate(e: dict) -> dict:
    """A briefing candidate for one case entry. Its line is code's
    (post_line), its date the case's nearest open due date; it has no row
    and no thread of its own, so nothing marks, drafts or closes through
    it. A letter is kind `letter` (its entry under `letter`); any other
    kind is kind `case` (its entry under `work`)."""
    w = words(e.get("case_kind") or "letter")
    base = {"case": e.get("case") or "", "title": e.get("name") or w.kept, "who": "", "from": "",
            "ledger_id": "", "conversation_id": "", "message_id": "", "summary": e.get("line") or "",
            "due": e.get("next_due") or "", "tags": [], "ask": "", "type": "",
            "keep_level": e.get("keep_level") or "full", "owner_said": [],
            "received": "", "subject": ""}
    if w.kind == "letter":
        return {"kind": "letter", "letter": e, **base, "letter_rows": list(e.get("ledger_ids") or [])}
    return {"kind": "case", "work": e, "case_kind": w.kind, **base,
            "case_rows": list(e.get("ledger_ids") or [])}


def is_case(c: dict) -> bool:
    return isinstance(c, dict) and ((c.get("kind") == "letter" and isinstance(c.get("letter"), dict))
                                    or (c.get("kind") == "case" and isinstance(c.get("work"), dict)))


def entry_of(c: dict) -> dict:
    if not is_case(c):
        return {}
    return c["letter"] if c.get("kind") == "letter" else c["work"]


def case_rows(c: dict) -> list[str]:
    """The ledger rows a case candidate (or a room entry) stands for."""
    return list(c.get("letter_rows") or c.get("case_rows") or [])


def top_ref(c: dict) -> str:
    """The today_top ref a case candidate answers to."""
    if not is_case(c):
        return ""
    return f"{'letter' if c.get('kind') == 'letter' else 'case'}:{c.get('case') or ''}"


def post_line(c: dict, when: str = "") -> str:
    """The case's line in a post: its head in bold, then the line after
    it. `when` is the do-list's window ("in the 1-2:30 gap")."""
    e = entry_of(c)
    text = str(e.get("line") or "")
    hd = str(e.get("head") or "")
    if not (hd and text.startswith(hd + ": ")):
        hd = text.partition(": ")[0]
    rest = rest_of(text, hd)
    out = f"*{hd}*: {rest}" if rest else f"*{text}*"
    if when:
        out += f" Work on it {_clean(when, 60)}."
    return out


def cases_section(cands: dict, shown: set) -> list[tuple[str, str]]:
    """The post's list of cases: every case no other section names,
    nearest due date first, a case with no date last."""
    refs = [r for r, c in cands.items() if is_case(c) and r not in shown]
    refs.sort(key=lambda r: (cands[r].get("due") or "9999-99-99", cands[r].get("title") or ""))
    return [(r, post_line(cands[r])) for r in refs]


def heading(cands: dict, refs, *, due: bool = False) -> str:
    """The list's heading in plain words: the kind's own ("Letters you are
    writing", "Reviews due") when every case on it is of one kind, else
    MIXED_HEADING ("Work you owe", "Work due")."""
    kinds = {(entry_of(cands[r]).get("case_kind") or "letter") for r in refs if r in cands}
    if len(kinds) == 1:
        w = words(kinds.pop())
        return w.heading_due if due else w.heading
    return MIXED_HEADING_DUE if due else MIXED_HEADING


def finished_words(view_finished: list[dict]) -> str:
    """The Quietly handled clause for the cases that finished: "1 letter
    finished: all its destinations are submitted (Recommendation letter
    for Dana Reyes)"; one clause per kind."""
    if not view_finished:
        return ""
    groups: dict[str, list[dict]] = {}
    for f in view_finished:
        groups.setdefault(f.get("case_kind") or "letter", []).append(f)
    out = []
    for kind, items in groups.items():
        w = words(kind)
        names_ = "; ".join(_clean(f.get("name"), 60) for f in items[:3])
        n = len(items)
        out.append(f"{n} {w.noun.lower()}{'s' if n != 1 else ''} finished: all "
                   f"{'its' if n == 1 else 'their'} {w.parts} are {w.delivered_word} ({names_})")
    return "; ".join(out)


# ── reminders by date ─────────────────────────────────────────────────

def rung(due: date | None, today: date, ladder=LETTER.ladder) -> int | None:
    """The ladder's rung for a due date today: days left when that is on
    the ladder, else None."""
    if not due:
        return None
    left = (due - today).days
    return left if left in ladder else None


def reminders(cases: list[dict], today: date, sent: dict | None = None) -> list[dict]:
    """The loud rungs due today that have not fired: [{case, dest, rung,
    loud, due, key}], one per case: its nearest date (a part's own, else
    the case's one date) on its kind's loud rungs (the day before, the day,
    the day after). `sent` is {key: when} of the rungs that fired (key
    "<case id>:<rung>:<due>", so a moved date starts again). The lead time
    is no rung: a case comes back when its stage turns due (card_plan). A
    finished case is never reminded."""
    sent = sent or {}
    out = []
    for c in cases:
        w = words_of(c)
        f = facts(c, today)
        due = f["next_due"]
        r = rung(due, today, tuple(w.loud)) if f["open"] else None
        if r is None:
            continue
        key = f"{c.get('id')}:{r}:{due.isoformat()}"
        if key in sent:
            continue
        first = f["due_next"][0] if f["due_next"] else {}
        out.append({"case": str(c.get("id") or ""), "dest": str(first.get("id") or ""), "rung": r,
                    "loud": True, "due": due.isoformat(), "key": key,
                    "parts": [str(d.get("id") or "") for d in f["due_next"]]})
    return out


def reminder_text(case: dict, items: list[dict], today: date, lv: str) -> str:
    """The loud reminder's first line (the push and the card's title):
    "Letter for Dana Reyes: 2 places due tomorrow (North University,
    Coastal Institute)."."""
    w = words_of(case)
    hd = head(case, lv, w)
    by_id = {str(d.get("id")): d for d in parts(case)}
    ids = [i for it in items for i in (it.get("parts") or [it.get("dest")])]
    named = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    due = _date(items[0]["due"]) if items else None
    when = ("was due " + day_words(due, today)) if due and due < today else ("due " + day_words(due, today))
    one, many = place_words(w)
    names_ = ", ".join(_clean(d.get("name"), 50) for d in named[:3]) if lv in ("full", "summary") else ""
    return f"{hd}: {_n(len(named), one, many)} {when}" + (f" ({names_})." if names_ else ".")


# ── the cards in Action requests (ea-chat-bot.py) ─────────────────────
# Card specs as plain data; the bot turns one into a ConfirmationCard.
# Every value names the case and its open rows as `rows` (web_requests
# reads them for the project and the urgency edge); never `ledger_id`,
# which the web chat's Mark done would close. A field's value is plain
# text: no frontend markup goes in one.

def _value(case: dict, **extra) -> dict:
    return {"case": str(case.get("id") or ""), "rows": ledger_ids(case), **extra}


def _part_line(d: dict, today: date, lv: str, n: int, w: Words, case: dict | None = None) -> str:
    """One part on a card: its name, its date when it has one of its own
    that the case's one date does not cover (an earlier one; on the
    delivery list, its own or the case's), its state and how it goes in."""
    nm = _clean(d.get("name"), 80) if lv in ("full", "summary") else f"{w.part.capitalize()} {n}"
    D = _dates()
    cby = D.do_by(case) if case else None
    own = D.own_due(d) if case else _date(d.get("due"))
    if case and cby and D.is_ready(case):
        due = own or cby                    # the delivery list: each place's own date, else the case's
    else:
        due = own if not cby else (own if own and own < cby else None)
    if due:
        when = ("was due " if due < today else "due ") + day_words(due, today)
    else:
        when = "" if cby else "no due date"
    how = w.method_words.get(str(d.get("method") or ""), "")
    state = w.state_words.get(d.get("state") or "asked", d.get("state") or "")
    bits = [b for b in (when, state, how if lv in ("full", "summary") else "") if b]
    return f"{nm}: {', '.join(bits)}"


def _materials(case: dict, lv: str, w: Words) -> str:
    """What the request included, by file name where the level lets a
    reader see it: "The request included: cv.pdf, statement.pdf."."""
    names_ = [str(a.get("name") or "") for a in case.get("request_attachments") or []
              if isinstance(a, dict) and a.get("name")]
    if not names_:
        # The store keeps what came by kind (add_material).
        names_ = list(dict.fromkeys(w.material_words.get(str(m.get("kind") or ""), str(m.get("kind") or ""))
                                    for m in case.get("materials") or []
                                    if isinstance(m, dict) and m.get("kind")))
    names_ = [n for n in names_ if n]
    if not names_:
        return "The request named no materials."
    if lv == "local":
        return f"The request included {_n(len(names_), 'file')}."
    return "The request included: " + ", ".join(_clean(n, 60) for n in names_[:6]) \
        + (f", and {len(names_) - 6} more" if len(names_) > 6 else "") + "."


def _links(case: dict, lv: str, w: Words) -> list[str]:
    if lv not in ("full", "summary"):
        return []
    out = []
    for d in open_parts(case):
        url = str(d.get("link") or "")
        if url.startswith("https://") and not any(ch in url for ch in " <>|"):
            out.append(f"<{url}|{w.link.format(name=_clean(d.get('name'), 60))}>")
    return out[:CARD_DESTS]


def _when_field(f: dict, today: date, case: dict | None = None) -> str:
    by = _dates().do_by(case) if case else None
    if by:
        return "By " + day_words(by, today)
    return ("Next due " + day_words(f["next_due"], today)) if f["next_due"] else "No due date yet"


_ICONS = {"yes": ":white_check_mark:", "yes_ask": ":page_facing_up:", "no": ":no_entry_sign:"}
# A date action's line (CardAction.ask): the frontends draw a date picker
# for it (CardAction.date) and send the picked day, never free words.
DATE_ASK = "The date"
# The delivery list's verb, by the kind's delivered word.
_SEND = {"submitted": "Submit", "returned": "Return", "delivered": "Deliver"}
_WORD = {"yes": "yes", "yes_ask": "ask", "no": "no"}


def decision_spec(case: dict, today: date, lv: str = "full", w: Words | None = None) -> dict:
    """The new-case card: the kind's one question (Will you write it? Will
    you do this review?) with its answers, what the request included and
    the dates. Its yes carries the case's one date in the same tap: "Yes,
    by Tue Dec 1" (the earliest date the records state) and "Yes, by
    another date" (a date picker); with no date stated, the yes itself
    asks for one with the picker. No reaction answers it (`no_default`, no
    danger style on No)."""
    w = w or words_of(case)
    f = facts(case, today)
    nm = name(case, lv, w)
    lines = [f"{nm}." if nm != w.kept else f"{w.kept}: a new request."]
    shown = open_parts(case)[:CARD_DESTS]
    lines += [f"{i}. {_part_line(d, today, lv, i, w, case)}" for i, d in enumerate(shown, 1)]
    if f["total"] > len(shown):
        lines.append(f"And {f['total'] - len(shown)} more.")
    if not shown:
        lines.append(w.no_parts_card)
    lines.append(_materials(case, lv, w))
    q = decision_question(case)
    value = _value(case, **({"question": str(q["id"])} if q and q.get("id") else {}))
    D = _dates()
    prop = D.proposed(case)
    pick = D.date_spec(today, prop)
    p = w.prefix
    actions = []
    if not dated_yes(w):
        prop, pick = None, {}
    for a, text in w.choices:
        if a == "yes" and prop:
            actions.append((f"{p}_yes", f"{_ICONS['yes']} Yes, by {day_words(prop, today)}",
                            dict(value, by=prop.isoformat()), "primary", "yes", ""))
            actions.append((f"{p}_yes_date", ":calendar: Yes, by another date", value, "", "date",
                            DATE_ASK, pick))
        elif a == "yes":
            actions.append((f"{p}_yes", f"{_ICONS['yes']} {text}", value, "primary", "yes",
                            DATE_ASK if pick else "", pick))
        elif a == "yes_ask":
            actions.append((f"{p}_yes_ask", f"{_ICONS[a]} {text}",
                            dict(value, by=prop.isoformat()) if prop else value, "", _WORD[a], ""))
        else:
            actions.append((f"{p}_{a}", f"{_ICONS[a]} {text}", value, "", _WORD[a], ""))
    return {"kind": f"{w.prefix}_ask", "title": w.ask_title, "lines": lines,
            "fields": [("From", _from(case, lv, w)), ("Subject", nm),
                       ("When", _when_field(f, today, case))],
            "actions": actions, "no_default": True, "block_id": f"{w.prefix}_ask_{value['case']}"}


def date_spec(case: dict, today: date, lv: str = "full", w: Words | None = None) -> dict:
    """The one card that asks a decided case for its date (a yes with no
    date in it, or a case from before one date per case): "By Tue Dec 1"
    (the earliest date its places state) and "Another date" (the picker),
    or "Pick the date" alone. Asked once; every part goes under the date."""
    w = w or words_of(case)
    f = facts(case, today)
    nm = name(case, lv, w)
    who = person(case, lv) if w.kind == "letter" else (
        _clean(case.get("counterpart_name") or "", 60) if lv != "local" else "")
    title = f":calendar: *{w.when_q}{' for ' + who if who else ''}?*"
    one, many = place_words(w)
    shown = f["open"][:CARD_DESTS]
    lines = [f"{nm}." if nm != w.kept else f"{w.kept}."]
    lines += [f"{i}. {_part_line(d, today, lv, i, w)}" for i, d in enumerate(shown, 1)]
    if len(f["open"]) > len(shown):
        lines.append(f"And {len(f['open']) - len(shown)} more.")
    lead = _dates().lead(_tag(case, w.kind))
    lines.append(f"Every {one} goes under this one date. I bring it back {_n(lead, 'day')} before it.")
    D = _dates()
    prop = D.proposed(case)
    pick = D.date_spec(today, prop)
    value = _value(case)
    p = w.prefix
    if prop:
        actions = [(f"{p}_date", f"{_ICONS['yes']} By {day_words(prop, today)}",
                    dict(value, by=prop.isoformat()), "primary", "date", ""),
                   (f"{p}_date_pick", ":calendar: Another date", value, "", "pick", DATE_ASK, pick)]
    else:
        actions = [(f"{p}_date_pick", ":calendar: Pick the date", value, "primary", "pick", DATE_ASK, pick)]
    return {"kind": f"{w.prefix}_ask", "title": title, "lines": lines,
            "fields": [("From", _from(case, lv, w)), ("Subject", nm),
                       ("When", _when_field(f, today, case))],
            "actions": actions, "no_default": True, "block_id": f"{w.prefix}_date_{value['case']}"}


def has_draft(case: dict, f: dict | None = None) -> bool:
    opn = (f or {}).get("open") if f else open_parts(case)
    return bool(case.get("base_letter") or case.get("base_doc") or case.get("draft")) or any(
        d.get("state") in ("drafted", "ready") for d in opn or [])


def case_spec(case: dict, today: date, lv: str = "full", *, reminder: str = "", news=None,
              w: Words | None = None) -> dict:
    """The case block, once its time came: "Time to write the letter for
    Dana Reyes: due Tue Dec 1, 4 places." with each open part, what is
    missing and a link line per part with one, and Draft (Change once a
    draft exists), Change the date, Decline and a menu for a part already
    delivered. Once the work is ready it is the delivery list (ready_spec).
    `reminder` opens the title when a loud rung posts it; `news` (news())
    is its first line."""
    w = w or words_of(case)
    if stage(case, today) == "ready":
        return ready_spec(case, today, lv, reminder=reminder, news=news, w=w)
    f = facts(case, today)
    nm = name(case, lv, w)
    one, many = place_words(w)
    who = _who(case, lv, w)
    by = _dates().do_by(case)
    n = f["total"] - f["declined"]
    if by:
        hd = (f":memo: *Time to {w.verb}{' for ' + who if who else ''}*: due {day_words(f['next_due'] or by, today)}"
              + (f", {_n(n, one, many)}" if n else "") + ".")
    else:
        hd = f":memo: *{nm}*: {f['submitted']} of {n} {w.delivered_word}."
    title = (f":alarm_clock: *{_clean(reminder, 200)}*\n" if reminder else "") + hd
    shown = f["open"][:CARD_DESTS]
    lines = [news_words(news, lv)] if news else []
    if by and nm != w.kept:
        lines.append(f"{nm}.")
    lines += [f"{i}. {_part_line(d, today, lv, i, w, case)}" for i, d in enumerate(shown, 1)]
    if len(f["open"]) > len(shown):
        lines.append(f"And {len(f['open']) - len(shown)} more to do.")
    if by and f["submitted"]:
        lines.append(f"{f['submitted']} of {n} {w.delivered_word} already.")
    if not f["total"]:
        lines.append(w.no_parts_card)
    drafted = has_draft(case, f)
    missing = []
    if not drafted:
        missing.append(w.missing_work)
    if not by:
        missing.append("the date")
    if w.missing_materials and not (case.get("materials") or case.get("request_attachments")):
        missing.append(w.missing_materials)
    if missing:
        lines.append("Missing: " + ", ".join(missing) + ".")
    lines += _links(case, lv, w)
    value = _value(case)
    D = _dates()
    actions = [(f"{w.prefix}_draft", ":pencil2: " + (w.change if drafted else w.draft),
                value, "primary", "draft", w.draft_ask)]
    if by:
        actions.append((f"{w.prefix}_date_pick", ":calendar: Change the date", value, "", "date",
                        DATE_ASK, D.date_spec(today, by)))
    else:
        actions.append((f"{w.prefix}_date_pick", ":calendar: Pick the date", value, "", "date",
                        DATE_ASK, D.date_spec(today, D.proposed(case))))
    actions.append((f"{w.prefix}_decline", "Decline", value, "", "decline", ""))
    act = f"{w.prefix}_{w.deliver_choice}"
    select = None
    if f["open"]:
        select = (act, f"Already {w.delivered_word} one",
                  [(_clean(d.get("name"), 60) if lv in ("full", "summary") else f"{w.part.capitalize()} {i}",
                    str(d.get("id") or "")) for i, d in enumerate(f["open"], 1)])
    return {"kind": w.prefix, "title": title, "lines": lines,
            "fields": [("From", _from(case, lv, w)), ("Subject", nm),
                       ("When", _when_field(f, today, case))],
            "actions": actions, "select": select, "no_default": True,
            "block_id": f"{w.prefix}_{value['case']}_{today.isoformat()}"}


def ready_spec(case: dict, today: date, lv: str = "full", *, reminder: str = "", news=None,
               w: Words | None = None) -> dict:
    """The delivery list, once the work is ready: every open place with its
    link, a button per place ("Submitted: North University"), "All
    submitted" and Change. One tap per place, or one for all."""
    w = w or words_of(case)
    f = facts(case, today)
    nm = name(case, lv, w)
    one, many = place_words(w)
    who = _who(case, lv, w)
    send = _SEND.get(w.delivered_word, "Send")
    n = len(f["open"])
    by = f["next_due"]
    hd = (f":outbox_tray: *The {w.work}{' for ' + who if who else ''} is ready.* {send} it"
          + (f" to {_n(n, one, many)}" if n > 1 or w.kind == "letter" else "")
          + (f" by {day_words(by, today)}" if by else "") + ".")
    title = (f":alarm_clock: *{_clean(reminder, 200)}*\n" if reminder else "") + hd
    lines = [news_words(news, lv)] if news else []
    if nm != w.kept:
        lines.append(f"{nm}.")
    shown = f["open"][:CARD_DESTS]
    lines += [f"{i}. {_part_line(d, today, lv, i, w, case)}" for i, d in enumerate(shown, 1)]
    if n > len(shown):
        lines.append(f"And {n - len(shown)} more.")
    if f["submitted"]:
        lines.append(f"{f['submitted']} of {f['total'] - f['declined']} {w.delivered_word} already.")
    lines += _links(case, lv, w)
    value = _value(case)
    act = f"{w.prefix}_{w.deliver_choice}"
    actions = []
    for i, d in enumerate(f["open"][:CARD_BUTTONS], 1):
        nm_d = _clean(d.get("name"), 40) if lv in ("full", "summary") else f"{w.part} {i}"
        actions.append((act, ":white_check_mark: " + w.delivered_button.format(name=nm_d),
                        _value(case, **{w.part_key: str(d.get("id") or "")}), "", "", ""))
    select = None
    if n > CARD_BUTTONS:
        select = (act, w.delivered_another,
                  [(_clean(d.get("name"), 60) if lv in ("full", "summary") else f"{w.part.capitalize()} {i}",
                    str(d.get("id") or "")) for i, d in enumerate(f["open"], 1)][CARD_BUTTONS:])
    if n > 1:
        actions.append((f"{w.prefix}_all", f":white_check_mark: All {w.delivered_word}", value,
                        "", "all", ""))
    actions.append((f"{w.prefix}_draft", ":pencil2: " + w.change, value, "", "draft", w.draft_ask))
    return {"kind": w.prefix, "title": title, "lines": lines,
            "fields": [("From", _from(case, lv, w)), ("Subject", nm),
                       ("When", _when_field(f, today, case))],
            "actions": actions, "select": select, "no_default": True,
            "block_id": f"{w.prefix}_{value['case']}_{today.isoformat()}"}


def _who(case: dict, lv: str, w: Words) -> str:
    """Who the work is for, in a title: the person (a letter) or who asked
    (any other kind); "" when kept or not named."""
    if w.kind == "letter":
        return person(case, lv)
    return _clean(case.get("counterpart_name") or "", 60) if lv != "local" else ""


def block_sig(case: dict, today: date) -> str:
    """What the owner must see on a case's block, as a digest: its stage,
    its date, its open parts with their states and shown dates, whether
    work exists, how many are done. A block is posted again only when this
    changed (or a loud rung or a new message calls for it): never for the
    same state, and never after a press that changed nothing."""
    import hashlib
    D = _dates()
    f = facts(case, today)
    body = {"stage": stage(case, today), "by": str(case.get("do_by") or ""),
            "open": [[str(d.get("id") or ""), d.get("state") or "", str(d.get("name") or ""),
                      str((D.due_of(d, case, today) or "") and D.due_of(d, case, today).isoformat())]
                     for d in f["open"]],
            "done": f["submitted"], "draft": has_draft(case, f)}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]


def join_choices(q: dict, w: Words) -> tuple:
    got = [str(c) for c in (q or {}).get("choices") or [] if str(c).strip()]
    return tuple(got[:2]) if len(got) >= 2 else w.join_choices


def join_spec(case: dict, q: dict, lv: str = "full", w: Words | None = None) -> dict:
    """A `join` question: is a new mail part of an open case (the same
    person, the same review)? Two answers, asked once; no reaction
    answers it. Kept mail gets fixed words."""
    w = w or words_of(case)
    text = _clean(q.get("text"), 300) if lv in ("full", "summary") else w.join_kept
    value = _value(case, question=str(q.get("id") or ""))
    same, other = join_choices(q, w) if w.kind != "letter" else w.join_choices
    return {"kind": f"{w.prefix}_ask", "title": w.join_title,
            "lines": [text, "Your answer is kept, so I do not ask again."],
            "fields": [("Subject", name(case, lv, w))],
            "actions": [(f"{w.prefix}_join_same", f":link: {same}", value, "", "same", ""),
                        (f"{w.prefix}_join_other", other, value, "", "other", "")],
            "no_default": True, "block_id": f"{w.prefix}_join_{q.get('id')}"}


# ── "which one was it?" (cases.ask_part) ──────────────────────────────
# A delivery notice that matches no part for sure is a question for the
# owner, never a guess: nothing is marked delivered until he answers. The
# store words the question and its choices at the cases' keep level; a
# question may span cases (no case of its own), so it is read from the
# store's question list, not from a case.

def part_questions(conn=None) -> list[dict]:
    """The open "which one?" questions (cases.questions, kind `part`); []
    without the store or on a read failure."""
    mod = _cases()
    if mod is None or not hasattr(mod, "questions"):
        return []
    own = conn is None
    try:
        conn = conn or _connect()
        return [q for q in mod.questions(conn, open_only=True) or []
                if isinstance(q, dict) and q.get("kind") == "part" and not q.get("answer")]
    except Exception as e:
        print(f"case_surface: the open questions could not be read: {str(e)[:160]}", file=sys.stderr)
        return []
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def which_spec(q: dict) -> dict:
    """The card for a "which one?" question: its text and one button per
    choice (the nearest CARD_BUTTONS, the rest and "None of these" in the
    menu). A value names the question and the choice by its place, never
    by its words."""
    choices = [str(c) for c in q.get("choices") or [] if str(c).strip()]
    value = {"case": str(q.get("case") or ""), "rows": [], "question": str(q.get("id") or "")}
    actions = [("case_which", f":white_check_mark: {_clean(c, 70)}", dict(value, pick=i), "", "", "")
               for i, c in enumerate(choices[:CARD_BUTTONS])]
    rest = [(_clean(c, 70), str(i)) for i, c in enumerate(choices) if i >= CARD_BUTTONS]
    select = ("case_which", "Another one", rest) if rest else None
    return {"kind": "case_ask", "title": ":memo: *Which one was it?*",
            "lines": [_clean(q.get("text"), 300), "Nothing is marked done until you answer."],
            "fields": [("Subject", "A delivery notice")],
            "actions": actions, "select": select, "no_default": True,
            "block_id": f"case_which_{value['question']}"}


def which_plan(questions_: list[dict], cards: dict) -> list[dict]:
    """[{"what": "which", "question", "key"}] for a question with no card
    yet, and {"what": "retire", "case": key} for a live card whose
    question is answered."""
    out, open_keys = [], set()
    for q in questions_:
        key = f"q:{q.get('id')}"
        open_keys.add(key)
        if key not in cards:
            out.append({"case": key, "what": "which", "question": str(q.get("id") or ""), "key": key})
    for key, rec in cards.items():
        if key.startswith("q:") and rec.get("kind") == "which" and key not in open_keys and rec.get("live"):
            out.append({"case": key, "what": "retire"})
    return out


def answer_which(q: dict, pick, *, runner=None) -> dict:
    """The owner's pick on a "which one?" card: {"ok", "said"}."""
    choices = [str(c) for c in (q or {}).get("choices") or []]
    try:
        text = choices[int(pick)]
    except (TypeError, ValueError, IndexError):
        return {"ok": False, "said": "This question changed since the card. Nothing was done."}
    out = (runner or _cli)(["answer", "--question", str(q.get("id") or ""), "--choice", text])
    if not out or out.get("error"):
        return {"ok": False, "said": "I could not record your answer. Ask me in Ask Tacitus."}
    none = text == choices[-1] and text.lower().startswith("none")
    return {"ok": True, "said": "Recorded. Nothing is marked done." if none
            else "Recorded. That one is marked done."}


def _epoch(value) -> float:
    d = _when(value)
    return d.timestamp() if d else 0.0


def card_plan(cases: list[dict], cards: dict, today: date, sent: dict | None = None,
              news: dict | None = None) -> list[dict]:
    """What the bot does this tick, one step per case: [{case, what, ...}].

      ask      a new case's one decision, posted once (a loud rung puts it
               up again: what "remind", kind "ask")
      date     a decided case with no date: the date card, posted once
               (`asked_date` in its record); never asked twice
      quiet    a dated case before its lead time with a live card from
               before: the card gives way to the line that says when it
               comes back; then nothing until its time
      case     its time came (or the work began): the block, posted when
               there is none live and what it shows changed since the last
               one (block_sig): never again in the same state, never after
               a press that changed nothing
      update   a live block whose content changed: redrawn in place, no push
      remind   a loud rung fired: the block posts again, its old card retired
      news     a message came on the case after its block was posted
      note     one small line: a place joined after the work was ready, an
               earlier date moved the case's date, a message came while
               the case is quiet
      mark     rungs and facts to record, nothing posted
      retire   a card whose case is no longer open

    `cards` is the bot's record {case id: {"kind", "card", "channel",
    "live", "posted", "news_at", "sig", "asked_date", "do_by", "parts"}};
    `news` is news_marks()."""
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
        out += _plan_one(c, cards.get(cid) or {}, today, due.get(cid, []), (news or {}).get(cid) or {})
    for cid, rec in cards.items():
        if cid not in open_ids and rec.get("live"):
            out.append({"case": cid, "what": "retire"})  # a closed case, an answered question
    return out


def _plan_one(c: dict, rec: dict, today: date, todays: list[dict], fresh: dict) -> list[dict]:
    """card_plan's steps for one open case (see there)."""
    cid = str(c.get("id") or "")
    st = stage(c, today)
    live = bool(rec.get("live"))
    loud = [r for r in todays if r["loud"]]
    keys = [r["key"] for r in todays]
    f = facts(c, today)
    ids = [str(d.get("id") or "") for d in f["open"]]
    by = str(c.get("do_by") or "")
    track = {"do_by": by, "parts": ids}
    news_due = bool(fresh.get("at")) and rec.get("news_at") != fresh["at"] and \
        _epoch(fresh["at"]) > float(rec.get("posted") or 0)
    if st == "new":
        if not rec:
            return [{"case": cid, "what": "ask", "keys": keys}]
        if loud:
            return [{"case": cid, "what": "remind", "kind": "ask", "items": loud, "keys": keys}]
        return [{"case": cid, "what": "mark", "keys": keys}] if keys else []
    if st == "undated":
        # A place states a date: the store dates the case with it, and the
        # owner gets one line (write_stated_dates, stated_line). No place
        # yet: silent until a place or a date comes (the weekly review).
        # Places and no date anywhere: the one date card, once.
        if not rec.get("asked_date") and parts(c) and not _dates().proposed(c):
            return [{"case": cid, "what": "date", "keys": keys, **track}]
        if loud:
            return [{"case": cid, "what": "remind", "kind": "case", "items": loud, "keys": keys, **track}]
        return [{"case": cid, "what": "mark", "keys": keys}] if keys else []
    steps: list[dict] = []
    moved = _moved_earlier(c, rec)
    if moved:
        steps.append({"case": cid, "what": "note", "text_key": "moved", **track, "part": moved})
    if st == "quiet":
        if live and rec.get("kind") in ("case", "ask", "date"):
            steps.append({"case": cid, "what": "quiet", "keys": keys, **track})
        elif news_due:
            steps.append({"case": cid, "what": "note", "text_key": "news", "at": fresh["at"],
                          "items": fresh.get("items") or [], **track})
        elif keys or rec.get("do_by") != by or rec.get("parts") != ids:
            steps.append({"case": cid, "what": "mark", "keys": keys, **track})
        return steps
    sig = block_sig(c, today)
    joined = [i for i in ids if rec.get("parts") is not None and i not in rec["parts"]]
    if st == "ready" and joined and rec.get("kind") == "case" and rec.get("sig"):
        steps.append({"case": cid, "what": "note", "text_key": "joined", **track, "joined": joined})
    if loud:
        steps.append({"case": cid, "what": "remind", "kind": "case", "items": loud, "keys": keys,
                      "sig": sig, **track})
    elif news_due and (live or rec.get("sig")):
        steps.append({"case": cid, "what": "news", "kind": "case", "at": fresh["at"], "keys": keys,
                      "sig": sig, **track})
    elif not live or rec.get("kind") != "case":
        if rec.get("sig") != sig:
            steps.append({"case": cid, "what": "case", "keys": keys, "sig": sig, **track})
        elif keys:
            steps.append({"case": cid, "what": "mark", "keys": keys, **track})
    elif rec.get("sig") and rec.get("sig") != sig:
        steps.append({"case": cid, "what": "update", "keys": keys, "sig": sig, **track})
    elif keys or rec.get("parts") != ids:
        steps.append({"case": cid, "what": "mark", "keys": keys, **track})
    return steps


def _moved_earlier(c: dict, rec: dict) -> str:
    """The open part whose own date moved the case's date earlier since the
    bot last recorded it, or ""."""
    was, now_ = str(rec.get("do_by") or ""), str(c.get("do_by") or "")
    if not (was and now_ and now_ < was):
        return ""
    D = _dates()
    return next((str(d.get("id") or "") for d in open_parts(c)
                 if D.own_due(d) and D.own_due(d).isoformat() == now_), "")


def write_stated_dates() -> list[dict]:
    """The store's pass (cases.stated_dates): every case he said yes to
    with no date takes the earliest date its places state, with no
    question. [{"case", "do_by"}] of the cases dated; [] without the store
    or on a failure (logged)."""
    mod = _cases()
    if mod is None or not hasattr(mod, "stated_dates"):
        return []
    conn = None
    try:
        conn = mod.connect()
        got = mod.stated_dates(conn, actor="bot")
        conn.commit()
        return got
    except Exception as e:
        print(f"case_surface: the stated dates were not set: {str(e)[:160]}", file=sys.stderr)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def told_dates(cases: list[dict], cards: dict) -> list[dict]:
    """The cases whose date the store set from what they asked
    (`do_by_from` "stated") that the owner has not been told of yet (the
    bot's record `told`)."""
    return [c for c in cases if is_open(c) and c.get("do_by") and c.get("do_by_from") == "stated"
            and (cards.get(str(c.get("id") or "")) or {}).get("told") != c.get("do_by")]


def stated_line(items: list[tuple[dict, str]], today: date) -> str:
    """The one line that tells him the dates set from what they asked, for
    every case at once, with nothing to answer: "I set dates for 3
    letters from what they asked: Dana Reyes by Tue Dec 1, Pat Lee by Tue
    Dec 8, Sam Ortiz by Tue Dec 1. Change one from its block when it comes
    back." `items` is [(case, keep level)]."""
    if not items:
        return ""
    counts: dict[str, int] = {}
    named = []
    for c, lv in items:
        w = words_of(c)
        counts[w.kind] = counts.get(w.kind, 0) + 1
        who = (person(c, lv) if w.kind == "letter" else name(c, lv, w)) or w.kept.lower()
        named.append(f"{who} by {day_words(_dates().do_by(c), today)}")
    kinds = [f"{n} {words(k).noun.lower()}{'s' if n != 1 else ''}" for k, n in counts.items()]
    what = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]
    one = len(items) == 1
    return (f":calendar: I set {'the date' if one else 'dates'} for {what} from what they asked: "
            + ", ".join(named) + f". Change {'it' if one else 'one'} from its block when it comes back.")


def note_text(c: dict, step: dict, today: date, lv: str = "full") -> str:
    """The small line a `note` step posts: no card, no buttons.
      moved   "Coastal Institute asks by Mon Nov 30. The letter for Dana
              Reyes is now due Mon Nov 30."
      joined  "Coastal Institute also asked. The letter is ready: <link>"
      news    "New message from Dana Reyes. The letter for Dana Reyes stays
              set for Tue Dec 1."
    Names only where the case's level shows them."""
    w = words_of(c)
    who = _who(c, lv, w)
    work = f"The {w.work}" + (f" for {who}" if who else "")
    by = _dates().do_by(c)
    open_ = lv in ("full", "summary")
    if step.get("text_key") == "moved":
        d = find_part(c, step.get("part") or "") or {}
        nm = _clean(d.get("name"), 60) if open_ and d else f"A {place_words(w)[0]}"
        return f":calendar: {nm} asks by {day_words(by, today)}. {work} is now due {day_words(by, today)}."
    if step.get("text_key") == "joined":
        out = []
        for pid in step.get("joined") or []:
            d = find_part(c, pid) or {}
            nm = _clean(d.get("name"), 60) if open_ and d else f"Another {place_words(w)[0]}"
            url = str(d.get("link") or "")
            link = (f" <{url}|Open its link>" if open_ and url.startswith("https://")
                    and not any(ch in url for ch in " <>|") else "")
            out.append(f":inbox_tray: {nm} also asked. The {w.work} is ready:{link or ' it is on its block.'}")
        return "\n".join(out)
    if step.get("text_key") == "news":
        return (f":envelope: {news_words(step.get('items') or [{}], lv)} {work} stays set for "
                f"{day_words(by, today)}.")
    return ""


def quiet_words(c: dict, today: date) -> str:
    """The status line a card gives way to when its case goes quiet."""
    back = wake(c)
    by = _dates().do_by(c)
    return (f":zzz: Set for {day_words(by, today)}. I bring it back on {day_words(back, today)}."
            if back and by else ":zzz: Set. It is off your lists until its time.")


def _in_window(case: dict, today: date) -> bool:
    """A case Action requests holds a block for: one overdue, due within
    HOME_DAYS or with an undated part (it needs its date). A case further
    out gets its block when its date comes that close; until then it is
    on the briefing's list."""
    f = facts(case, today)
    return bool(f["overdue"] or f["undated"] or not f["open"]
                or (f["next_due"] and (f["next_due"] - today).days <= HOME_DAYS))


def find(cases: list[dict], case_id: str) -> dict | None:
    return next((c for c in cases if str(c.get("id") or "") == str(case_id or "")), None)


def find_part(case: dict, part_id: str) -> dict | None:
    return next((d for d in open_parts(case) if str(d.get("id")) == str(part_id or "")), None)


# ── answers (the bot's handler) ───────────────────────────────────────
# A letter's answer is letter_surface.answer (letters_work decides). Every
# other kind's runs the store's own verbs for what needs no model (the
# decision, a delivery, a date) and one EA turn for the rest (a request
# for materials, a draft, a decline written for the owner's Send).

CASE_CHOICES = ("yes", "yes_ask", "yes_date", "no", "draft", "delivered", "all", "date", "decline",
                "join_same", "join_other")
CASE_ACTIONS = {f"case_{c}": c for c in CASE_CHOICES}
CASE_ACTIONS["case_date_pick"] = "date"
# The answers that carry the case's one date (a picked day, or the date the
# button names in its value as `by`).
DATED = ("yes", "yes_ask", "yes_date", "date")
PICK_A_DATE = "Pick a date with the date button. Nothing was changed."
# The words of a press, by answer (a kind's own `said` goes first).
SAID = {"yes": "You said yes. It is on your list with its date.",
        "yes_ask": "I am writing the request for the materials.",
        "no": "You said no. I am writing a decline for you to check and send.",
        "no_quiet": "You said no. Nothing is sent.",
        "decline": "Declined. I am writing a decline for you to check and send.",
        "decline_quiet": "Declined. Nothing is sent.",
        "draft": "I am writing the draft.",
        "date": "I am recording the date.",
        "all": "Marked as {done}. The case is closed."}
_PRIVATE = {"review": "A review's text never goes in a channel post, a room or the doc-review repo.",
            "form": "A form's personal data never goes in a channel post, a room or the doc-review repo."}


def action_choice(action_id: str) -> str:
    """The answer a card button names (`case_draft` -> "draft")."""
    return CASE_ACTIONS.get(str(action_id or ""), "")


def _said(w: Words, key: str) -> str:
    if key == "delivered":
        return f"Marked as {w.delivered_word}."
    if key == "all":
        return f"All {w.delivered_word}. The case is closed."
    return w.said.get(key) or SAID.get(key, "Done.")


def picked_day(text: str, today: date) -> date | None:
    """The day a date answer names: the picker's YYYY-MM-DD, else the
    owner's words in a common form (case_dates.parse_words); None for
    none."""
    D = _dates()
    return D.picked(text, today) or D.parse_words(text, today)


def dated_words(case: dict, by: date, today: date) -> str:
    """What follows the date in a dated answer's line: an earlier place's
    own date, and when the case comes back. " I bring it back on Tue Nov
    24." or " It is time to start, so it stays on your list."."""
    w = words_of(case)
    D = _dates()
    c = dict(_tag(case, w.kind), do_by=by.isoformat())
    early = sorted(((D.own_due(d), d.get("name") or "") for d in open_parts(c)
                    if D.own_due(d) and D.own_due(d) < by), key=lambda x: x[0])
    out = f" {_clean(early[0][1], 60)} asks by {day_words(early[0][0], today)}." if early else ""
    back = D.wake(c)
    return out + (f" I bring it back on {day_words(back, today)}." if back and back > today
                  else " It is time to start, so it stays on your list.")


def turn_prompt(case: dict, choice: str, part: str = "", text: str = "") -> str:
    """The EA turn's prompt for an answer on a case: the case by id, the
    owner's choice and words, the guide; the EA reads the store itself."""
    w = words_of(case)
    cid = str(case.get("id") or "")
    who = person(case, "full") or "them"
    said = f' The owner added: "{_clean(text, 300)}".' if text else ""
    what = {
        "yes_ask": (f"The owner will do it and wants the missing materials from {who}. Ask only for "
                    "what the request did not already include, and name what it included. The "
                    "email is a draft that waits for his Send."),
        "draft": {"review": ("The owner asked for a draft of the report. Draft it from his points and "
                             "the files. His judgment, scores and recommendation are his: ask for each "
                             "one he did not give; never write one."),
                  "form": ("The owner asked you to fill in the form. Fill in only what his records "
                           "show. A signature, or a fact about him the records do not show, is a "
                           "question for him."),
                  }.get(w.kind, ("The owner asked for a draft. Draft it from the materials. A fact the "
                                 "materials do not support is a question for him.")),
        "date": (f"The owner gives the due date for the {w.part} without one. With no date in the "
                 "owner's words, ask for it in one line."),
        "no": (f"The owner said no, and the case is declined. Write a short decline to {who} as a "
               "draft that waits for his Send, in the request's thread; for an invitation in a "
               "portal, give him its decline link instead. Never send it."),
    }
    what["decline"] = what["no"]
    part_s = f" Part id: {part}." if part else ""
    guide = f" Follow the {w.noun.lower()} guide (docs/ea/{w.guide})." if w.guide else ""
    return (f"{w.noun} case {cid} (python3 /home/ea-bot/ea-system/preprocessor/{w.cli} get --case {cid}). "
            f"{what.get(choice, '')}{part_s}{said}{guide} {_PRIVATE.get(w.kind, '')}").rstrip()


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                       "oct", "nov", "dec"), 1)}


def parse_day(text: str, today: date) -> date | None:
    """A due date from the owner's words (case_dates.parse_words): "2026-12-01",
    "Dec 1", "December 1st", "12/1", "today", "tomorrow", "next Friday"; a
    month and day without a year is the next one to come. None for
    anything else."""
    return _dates().parse_words(text, today)


def answer(case: dict, choice: str, *, dest: str = "", text: str = "", question: str = "",
           runner=None) -> dict:
    """One answer on a case card or a Home item: {"ok", "said", "turn"?}.
    A letter's goes to letter_surface.answer as it always has. Never a
    close on a reply, never a decline sent on its own."""
    w = words_of(case)
    if w.kind == "letter":
        kw = {"dest": dest, "text": text, "question": question}
        if runner is not None:
            kw["runner"] = runner
        return _ls().answer(case, "submitted" if choice == "delivered" else choice, **kw)
    if choice not in CASE_CHOICES:
        return {"ok": False, "said": "That choice is not known. Nothing was done."}
    cid = str(case.get("id") or "")
    run = runner or _cli
    part = dest

    def done(out: dict, yes: str) -> dict:
        ok = bool(out) and not out.get("error")
        return {"ok": ok, "said": yes if ok else "I could not record your answer. Ask me in Ask Tacitus."}
    if choice in ("join_same", "join_other"):
        if not question:
            return {"ok": False, "said": "This question changed since the card. Nothing was done."}
        q = next((x for x in case.get("questions") or [] if str((x or {}).get("id")) == question), {})
        same, other = join_choices(q, w)
        return done(run(["answer", "--question", question, "--choice", same if choice == "join_same" else other]),
                    w.join_said[0] if choice == "join_same" else w.join_said[1])
    today = today_local()
    by = None
    if choice in DATED:
        # No words: the date the button stands for (the earliest the
        # records state); "another date" always names its own.
        by = picked_day(text, today) if text else (None if choice == "yes_date" else _dates().proposed(case))
        if (text or choice == "yes_date") and not by:
            return {"ok": False, "said": PICK_A_DATE}
    if choice in ("yes", "yes_ask", "yes_date", "no"):
        texts = dict(w.choices)
        q = next((x for x in case.get("questions") or [] if str((x or {}).get("id")) == question), {})
        picks = dict(_choices_of(q)) if q.get("choices") else {}
        key = "yes" if choice == "yes_date" else choice
        pick = picks.get(key) or texts.get(key) or ("No" if choice == "no" else "Yes")
        argv = (["answer", "--question", question, "--choice", pick] if question
                else ["decline" if choice == "no" else "accept", "--case", cid])
        if by:
            argv += ["--date", by.isoformat()]
        quiet = choice == "no" and not w.decline_turn
        said = _said(w, "no_quiet" if quiet else key)
        if by and key == "yes":
            said = f"You said yes, by {day_words(by, today)}." + dated_words(case, by, today)
        elif by and key == "yes_ask":
            said = f"You said yes, by {day_words(by, today)}. {said}"
        elif key == "yes":
            said = "You said yes. Pick its date on the next card."
        got = done(run(argv), said)
        if got["ok"] and (choice == "yes_ask" or (choice == "no" and not quiet)):
            got["turn"] = turn_prompt(case, choice, "", text if choice == "no" else "")
        return got
    if choice == "decline":
        quiet = not w.decline_turn
        got = done(run(["decline", "--case", cid]), _said(w, "decline_quiet" if quiet else "decline"))
        if got["ok"] and not quiet:
            got["turn"] = turn_prompt(case, "decline", part, text)
        return got
    if choice == "delivered":
        if not part:
            return {"ok": False, "said": f"Pick the {w.part} you {w.delivered_word}."}
        return done(run(["record-delivered", "--part", part, "--evidence",
                         "the owner said so in Action requests"]), _said(w, "delivered"))
    if choice == "all":
        opn = open_parts(case)
        if not opn:
            return {"ok": False, "said": "This changed since the card. Nothing was done."}
        for d in opn:
            got = done(run(["record-delivered", "--part", str(d.get("id") or ""), "--evidence",
                            f"the owner said all are {w.delivered_word}, in Action requests"]), "")
            if not got["ok"]:
                return got
        return {"ok": True, "said": _said(w, "all")}
    if choice == "date":
        # One date for the whole case: never a date per part, never a turn
        # to ask which part, never a loop on words that hold no date.
        if not by:
            return {"ok": False, "said": PICK_A_DATE}
        return done(run(["set-date", "--case", cid, "--date", by.isoformat()]),
                    f"The date is {day_words(by, today)}." + dated_words(case, by, today))
    return {"ok": True, "said": _said(w, "draft"), "turn": turn_prompt(case, "draft", part, text)}


def _cli(argv: list[str]) -> dict:
    """One cases.py verb (the module's own file, beside this one)."""
    import subprocess
    mod = _cases()
    path = Path(getattr(mod, "__file__", "") or HERE / "cases.py")
    if not path.is_file():
        return {"error": "the cases store is not installed"}
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


CARD_KINDS = ("letter", "letter_ask", "case", "case_ask")


def home_items(src, cases=None, *, rows: dict | None = None) -> tuple[list[dict], set]:
    """(Home's items for the open cases, the request ids they stand for).
    One item per case, kind `letter` for a letter, kind `case` for any
    other kind, while the case needs the owner: a new case (its decision),
    a case with no date (its date), and a case whose time came (due, ready
    or overdue). A quiet case (dated, before its lead time) is not on
    Home. A case's waiting card in Action requests is the item's `href`
    and leaves the list as a request of its own."""
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
        if card.get("kind") not in CARD_KINDS:
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
        st = stage(c, today)
        card = cards.get(cid)
        if st == "closed" or waits(c, today):
            if card is not None:
                taken.add(f"request:{card.get('id')}")  # a card from before gives way on the next tick
            continue
        w = words_of(c)
        lv = level(c, rows, records=False)
        due = _home_due(min([_dates().due_of(d, c, today) for d in f["overdue"]] or [f["next_due"]])
                        if (f["overdue"] or f["next_due"]) else None, zone)
        urgency = "overdue" if f["overdue"] else ("soon" if due and due - now < 2 * 86400 else "calm")
        nm = name(c, lv, w)
        title = f"{w.question} {nm}." if f["new"] else (
            f"{w.when_q}{' for ' + _who(c, lv, w) if _who(c, lv, w) else ''}?" if st == "undated" else nm)
        got_news = news(c, rows, lv=lv, now=datetime.fromtimestamp(now, timezone.utc)) if rows else []
        full = line(c, today, lv=lv, news=got_news, w=w)
        detail = rest_of(full, head(c, lv, w)) or full
        item = {"id": f"{w.prefix}:{cid}", "kind": w.prefix,
                "label": w.home_label_new if f["new"] else w.home_label,
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
        print(f"case_surface: no case items on Home: {str(e)[:160]}", file=sys.stderr)
        return out
    if not items:
        return out
    shown = {it["_case"] for it in items}
    ids = {i for c in cases if str(c.get("id") or "") in shown for i in ledger_ids(c, open_only=False)}
    rows = getattr(src, "rows", None) or {}
    convs = {str(rows[i].get("conversation_id")) for i in ids if i in rows and rows[i].get("conversation_id")}
    kept = []
    for x in out:
        xid = str(x.get("id") or "")
        lid = str(x.get("_lid") or "") or (xid.split(":", 1)[1] if ":" in xid else "")
        if xid in taken or lid in ids or (x.get("_conv") and x.get("_conv") in convs) \
                or xid.startswith(("item:letter:", "item:case:")):
            continue  # the case's rows, threads, cards and its today_top entry
        kept.append(x)
    return kept + items


def home_brief(case: dict, today: date, lv: str = "full", news=None) -> dict:
    """The quick briefing for a case item (the Home cards contract's five
    fields), code-built: what it is, what was asked and by when, where it
    stands (and who wrote since), what Tacitus would do next."""
    w = words_of(case)
    f = facts(case, today)
    st = stage(case, today)
    nm = name(case, lv, w)
    one, many = place_words(w)
    standing = []
    if f["new"]:
        standing.append("You have not answered yet.")
    else:
        standing.append(f"{f['submitted']} of {f['total'] - f['declined']} {w.parts} {w.delivered_word}.")
    if f["overdue"]:
        standing.append(f"{_n(len(f['overdue']), w.part, w.parts)} past its due date.")
    if st == "undated":
        standing.append("It has no date yet.")
    if news:
        standing.append(news_words(news, lv))
    q = w.question
    if f["new"]:
        nxt = f"Tacitus waits for your answer: {q[0].lower() + q[1:]}"
    elif st == "undated":
        nxt = f"Pick one date. Every {one} goes under it, and it stays off your list until near then."
    elif st == "ready":
        nxt = w.next_deliver
    elif not has_draft(case, f):
        nxt = w.next_draft
    else:
        nxt = w.next_deliver
    asked = (w.asked_many.format(n=_n(f["total"], w.part, w.parts)) if f["total"] else w.asked_none)
    by, nd = _dates().do_by(case), f["next_due"]
    due = (f"Due {day_words(nd, today)}." if nd and nd == by else
           f"Next due {day_words(nd, today)}." if nd else "No due date yet.")
    return {"what": f"{nm}.", "asked": asked, "due": due, "standing": standing[:4], "next": nxt}


def home_choices(case: dict, today: date, lv: str = "full") -> list[dict]:
    """The choices for a case item, in the Home cards contract's shapes,
    the card's own answers by stage: a new case's yes with its date ("Yes,
    by Tue Dec 1"; "Yes, by another date" or a yes with no date stated:
    `input: "date"`), its other answers; a case with no date: "By Tue Dec
    1" and "Another date"; a case whose time came: Draft (a line of the
    owner's words), Change the date; the delivery list: Delivered per part
    (the CARD_BUTTONS nearest), All delivered, Change. Then Open it in
    Action requests. A choice `<prefix>:<answer>[:<part id>]` runs
    answer() as a press on the card does; a date choice posts the picked
    day as its `arg`. No Done, Not needed or Later: a case closes only on
    its deliveries, and its date keeps it off the list until its time."""
    w = words_of(case)
    f = facts(case, today)
    st = stage(case, today)
    p = w.prefix
    D = _dates()
    prop = D.proposed(case)
    pick = D.date_spec(today, prop)
    if f["new"]:
        out = []
        if not dated_yes(w):
            prop, pick = None, {}
        for a, text in w.choices:
            if a == "yes" and prop:
                out += [{"id": f"{p}:yes", "label": f"Yes, by {day_words(prop, today)}", "style": "primary"},
                        {"id": f"{p}:yes_date", "label": "Yes, by another date", "input": "date", **pick}]
            elif a == "yes":
                out.append({"id": f"{p}:yes", "label": w.home_yes, "style": "primary",
                            **({"input": "date", **pick} if pick else {})})
            else:
                out.append({"id": f"{p}:{a}", "label": text})
        return out
    if st == "undated":
        out = ([{"id": f"{p}:date", "label": f"By {day_words(prop, today)}", "style": "primary"},
                {"id": f"{p}:date_pick", "label": "Another date",
                 "input": "date", **pick}] if prop else
               [{"id": f"{p}:date_pick", "label": "Pick the date", "style": "primary", "input": "date", **pick}])
        return out + [{"id": "open", "label": "Open it in Action requests"}]
    drafted = has_draft(case, f)
    if st == "ready":
        out = []
        for i, d in enumerate(f["open"][:CARD_BUTTONS], 1):
            nm = _clean(d.get("name"), 40) if lv in ("full", "summary") else f"{w.part} {i}"
            out.append({"id": f"{p}:{w.deliver_choice}:{d.get('id')}",
                        "label": w.delivered_button.format(name=nm)})
        if len(f["open"]) > 1:
            out.append({"id": f"{p}:all", "label": f"All {w.delivered_word}"})
        out.append({"id": f"{p}:draft", "label": w.change, "input": "text"})
        return out + [{"id": "open", "label": "Open it in Action requests"}]
    by = D.do_by(case)
    out = [{"id": f"{p}:draft", "label": w.change if drafted else w.draft, "style": "primary", "input": "text"},
           {"id": f"{p}:date_pick", "label": "Change the date" if by else "Pick the date", "input": "date",
            **D.date_spec(today, by or prop)}]
    return out + [{"id": "open", "label": "Open it in Action requests"}]


def home_choice(choice_id: str, prefixes=("letter", "case")) -> tuple[str, str]:
    """(answer, part id) for a Home choice id `<prefix>:<answer>[:<part
    id>]`; ("", "") for any other id."""
    hd, _sep, rest = str(choice_id or "").partition(":")
    if hd not in prefixes:
        return "", ""
    choice, _sep, part = rest.partition(":")
    choice = "date" if choice == "date_pick" else choice   # the picker's own button
    valid = _ls().CHOICES if hd == "letter" else CASE_CHOICES
    return (choice, part) if choice in valid else ("", "")


# ── the pre-send review (briefing_review.py) ──────────────────────────

def review_key(c: dict) -> str:
    """The key a case's record goes under in the review's packet."""
    return "letter_case" if c.get("kind") == "letter" else "work_case"


def review_record(c: dict) -> dict | None:
    """What the reviewer reads for a case item: the counterpart (at its
    level), the kind and each open part with its date and state, and the
    rule: a reply in a thread is no delivery."""
    if not is_case(c):
        return None
    e = entry_of(c)
    w = words(e.get("case_kind") or "letter")
    if w.kind == "letter":
        return {"case": e.get("case") or "", "kind": e.get("kind") or "",
                "submitted": e.get("submitted") or 0, "total": e.get("total") or 0,
                "destinations": [{k: v for k, v in d.items() if v} for d in e.get("destinations") or []],
                "rule": w.rule}
    return {"case": e.get("case") or "", "kind": w.kind, "subkind": e.get("subkind") or "",
            "delivered": e.get("submitted") or 0, "total": e.get("total") or 0,
            "parts": [{k: v for k, v in d.items() if v} for d in e.get("parts") or []],
            "rule": w.rule}


def review_refusal(rec: dict, action: str, kind: str = "") -> str:
    """Why the reviewer may not take this action on a case item, or "":
    a case with an open part is never done, someone else's, a duplicate
    or only an event, and its line is code's."""
    rec = rec or {}
    case = rec.get("letter_case") if isinstance(rec.get("letter_case"), dict) else rec.get("work_case")
    if not isinstance(case, dict):
        return ""
    w = LETTER if isinstance(rec.get("letter_case"), dict) else words(case.get("kind") or "")
    if action == "drop":
        return f"{w.subject} leaves the post only when every {w.part} is {w.delivered_word}"
    if action == "rewrite-line":
        return f"{w.subject}'s line is code's own"
    return ""


# ── a project room (briefing_rooms.py) ────────────────────────────────

def room_title(c: dict) -> str:
    """A case's title in a room post and the room's list of threads: its
    kind and counterpart, or the fixed words for kept mail."""
    if not is_case(c):
        return ""
    e = entry_of(c)
    return str(e.get("name") or words(e.get("case_kind") or "letter").kept)


def room_label(c: dict) -> str:
    """The section label a room post gives a case item: ":memo: Letter",
    ":memo: Review"."""
    if not is_case(c):
        return ""
    return f":memo: {words(entry_of(c).get('case_kind') or 'letter').noun}"


def collapse_rows(rows: list[dict], cases=None, *, today: date | None = None,
                  open_only: bool = True, test=None) -> list[dict]:
    """`rows` with the parts of one case as one row: the first part row of
    each case stands for it, named by the case (briefing_rooms' Also open
    list, the weekly audit's Completed list; `open_only` False reads the
    finished cases too)."""
    test = test or is_case_row
    if not any(test(r) for r in rows if isinstance(r, dict)):
        return rows
    today = today or today_local()
    cases = load_cases(open_only=open_only) if cases is None else cases
    by_case = {}
    for c in cases:
        for i in ledger_ids(c, open_only=False):
            by_case[i] = c
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict) or not test(r):
            out.append(r)
            continue
        rec = _row_case(r)
        c = by_case.get(str(r.get("id"))) or find(cases, str(rec.get("case") or rec.get("id") or ""))
        key = str((c or {}).get("id") or rec.get("case") or rec.get("id") or r.get("id"))
        if key in seen:
            continue
        seen.add(key)
        if c is None:
            out.append(r)
            continue
        lv = level(c, {str(r.get("id")): r}, records=False)
        w = words_of(c)
        out.append(dict(r, description=f"{name(c, lv, w)}: {rest_of(line(c, today, lv=lv, w=w), head(c, lv, w))}"))
    return out
