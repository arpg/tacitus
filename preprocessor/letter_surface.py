#!/usr/bin/env python3
"""Where the owner sees a letter case and acts on it: the letters' layer
over case_surface.py, which holds every surface for every kind of work
case. Letters are its first kind and keep their words, cards, ladder and
answers exactly; this module keeps the hooks the letters' lanes and tests
call and patch (the store, the cases' loading, today's date, an answer)
and names every function the letters' callers know, each one the kind
`letter` of case_surface's.

A letter case is one person the owner was asked to write for, with its
destinations (a program, an employer, an award, a portal), each with its
own due date and state. Every surface shows the case as ONE item: its
destinations' ledger rows and their mail are never items of their own,
never a reply owed, and a reply in one of their threads is never a sign
that the case is done. Only names, destinations, dates and states show,
never the letter's text or the person's materials, and those obey the
keep levels of the destinations' mail (keep_gate).

With no letters.py (an install from before the store) or a store that
cannot be read, every function here gives nothing and each surface stays
as it was.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if __package__:
    from . import case_surface as _cs
else:
    import case_surface as _cs  # type: ignore

W = _cs.LETTER
OPEN_STATES = _cs.OPEN_STATES
DONE_STATES = ("submitted", "declined")
# How each kind is named where a list names the case by its kind.
KIND_WORDS = dict(W.subkinds)
# What the person sent, by the store's material kinds.
MATERIAL_WORDS = dict(W.material_words)
KEPT_CASE = W.kept
# The kind's ladder (its declaration); the loud rungs fire on a case's
# nearest date: 1 the day before, 0 the day itself, -1 the day after.
LADDER = W.ladder
LOUD = W.loud
SOON_DAYS = _cs.SOON_DAYS
HOME_DAYS = _cs.HOME_DAYS
HOME_MINUTES = _cs.HOME_MINUTES
CARD_DESTS = _cs.CARD_DESTS
CARD_BUTTONS = _cs.CARD_BUTTONS
STATE_WORDS = dict(W.state_words)
METHOD_WORDS = dict(W.method_words)


def _w() -> "_cs.Words":
    """The letters' words, with the store's declaration (its ladder, its
    decision) when there is one."""
    return _cs.words("letter")


# ── the store (the hooks: lanes and tests patch these) ────────────────

def _letters():
    """letters.py, or None on an install without it."""
    try:
        import letters  # type: ignore
        return letters
    except Exception:
        return None


def _connect():
    """The ledger database, read-only, rows by name (as letters.py reads)."""
    return _cs._connect()


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


_meta = _cs._meta


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


def today_local() -> date:
    return _cs.today_local()


# ── one case in facts and words ───────────────────────────────────────

_date = _cs._date
_kept = _cs._kept
_clean = _cs._clean
_n = _cs._n
day_words = _cs.day_words


def dests(case: dict) -> list[dict]:
    return _cs.parts(case)


def open_dests(case: dict) -> list[dict]:
    """The destinations still to do, nearest due date first, undated last."""
    return _cs.open_parts(case)


open_parts = open_dests
decision_question = _cs.decision_question


is_new = _cs.is_new
is_open = _cs.is_open
questions = _cs.questions
facts = _cs.facts
ledger_ids = _cs.ledger_ids
level = _cs.level
person = _cs.person
_asks_date = _cs._asks_date


def name(case: dict, lv: str = "full") -> str:
    """"Recommendation letter for Dana Reyes"; fixed words at `local`."""
    return _cs.name(case, lv, _w())


def _own_line(case: dict, f: dict, today: date, lv: str) -> str:
    return _cs._own_line(case, f, today, lv, _w())


def line(case: dict, today: date, *, lv: str = "full", news=None) -> str:
    """The case's one line: letters.summary_line's words for an open case,
    code's own for a kept one or without the store."""
    return _cs.line(case, today, lv=lv, news=news, w=_w())


# A new message from the other side on a case's threads (case_surface).
news = _cs.news
news_words = _cs.news_words
news_marks = _cs.news_marks


# ── the briefing JSON (preprocess.py) ─────────────────────────────────

def briefing_view(open_rows: list[dict], *, today: date | None = None, cases=None,
                  closed=None, mail=None) -> dict:
    """case_surface.briefing_view over the letter cases alone."""
    today = today or today_local()
    cases = [_cs._tag(c, "letter") for c in (load_cases() if cases is None else cases)]
    if closed is None:
        closed = [c for c in load_cases(open_only=False) if facts(c, today)["finished"]]
    return _cs.briefing_view(open_rows, today=today, cases=cases,
                             closed=[_cs._tag(c, "letter") for c in closed], mail=mail,
                             row_test=is_letter_row)


def entry(case: dict, today: date, rows: dict | None = None, *, records=False) -> dict:
    return _cs.entry(_cs._tag(case, "letter"), today, rows, records=records)


def _hidden(item: dict, hide: dict) -> bool:
    return _cs._hidden(item, hide, is_letter_row)


def without(view: dict, *lists):
    """Each list without the items a letter case stands for."""
    return _cs.without(view, *lists, test=is_letter_row)


_STRIP = _cs._STRIP
score = _cs.score


def apply_briefing(briefing: dict, view: dict, *, cap: int = 5) -> dict:
    return _cs.apply_briefing(briefing, view, cap=cap, test=is_letter_row)


# ── the briefing post (remote_briefing.py, remote_audit.py) ───────────

def candidate(e: dict) -> dict:
    return _cs.candidate(dict(e, case_kind="letter") if not e.get("case_kind") else e)


def is_case(c: dict) -> bool:
    return isinstance(c, dict) and c.get("kind") == "letter" and isinstance(c.get("letter"), dict)


post_line = _cs.post_line


def letters_section(cands: dict, shown: set) -> list[tuple[str, str]]:
    """The post's Letters list: every letter case no other section names."""
    return _cs.cases_section({r: c for r, c in cands.items() if is_case(c)}, shown)


finished_words = _cs.finished_words


# ── reminders by date ─────────────────────────────────────────────────

def rung(due: date | None, today: date) -> int | None:
    return _cs.rung(due, today, _w().ladder)


reminders = _cs.reminders


def reminder_text(case: dict, items: list[dict], today: date, lv: str) -> str:
    return _cs.reminder_text(_cs._tag(case, "letter"), items, today, lv)


# ── the cards in Action requests (ea-chat-bot.py) ─────────────────────

_value = _cs._value


def _dest_line(d: dict, today: date, lv: str, n: int) -> str:
    return _cs._part_line(d, today, lv, n, _w())


def _materials(case: dict, lv: str) -> str:
    return _cs._materials(case, lv, _w())


def _links(case: dict, lv: str) -> list[str]:
    return _cs._links(case, lv, _w())


def decision_spec(case: dict, today: date, lv: str = "full") -> dict:
    """The new-case card: Will you write it? Yes / Yes, ask for materials /
    No."""
    return _cs.decision_spec(case, today, lv, _w())


def case_spec(case: dict, today: date, lv: str = "full", *, reminder: str = "", news=None) -> dict:
    """The case block: destinations, dates, states, what is missing."""
    return _cs.case_spec(case, today, lv, reminder=reminder, news=news, w=_w())


def join_spec(case: dict, q: dict, lv: str = "full") -> dict:
    return _cs.join_spec(case, q, lv, _w())


def date_spec(case: dict, today: date, lv: str = "full") -> dict:
    """The one card that asks a decided letter case for its date."""
    return _cs.date_spec(_cs._tag(case, "letter"), today, lv, _w())


def ready_spec(case: dict, today: date, lv: str = "full", *, reminder: str = "", news=None) -> dict:
    """The delivery list once the letter is ready."""
    return _cs.ready_spec(_cs._tag(case, "letter"), today, lv, reminder=reminder, news=news, w=_w())


def stage(case: dict, today: date) -> str:
    return _cs.stage(_cs._tag(case, "letter"), today)


def block_sig(case: dict, today: date) -> str:
    return _cs.block_sig(_cs._tag(case, "letter"), today)


def quiet_words(case: dict, today: date) -> str:
    return _cs.quiet_words(_cs._tag(case, "letter"), today)


def note_text(case: dict, step: dict, today: date, lv: str = "full") -> str:
    return _cs.note_text(_cs._tag(case, "letter"), step, today, lv)


picked_day = _cs.picked_day
PICK_A_DATE = _cs.PICK_A_DATE
told_dates = _cs.told_dates


def card_plan(cases: list[dict], cards: dict, today: date, sent: dict | None = None,
              news: dict | None = None) -> list[dict]:
    return _cs.card_plan([_cs._tag(c, "letter") for c in cases], cards, today, sent, news)


_in_window = _cs._in_window
find = _cs.find


# ── answers (the bot's handler; letters_work decides) ─────────────────

CHOICES = ("yes", "yes_ask", "yes_date", "no", "draft", "submitted", "all", "date", "decline",
           "join_same", "join_other")
ACTION_CHOICE = {"letter_yes": "yes", "letter_yes_ask": "yes_ask", "letter_yes_date": "yes_date",
                 "letter_no": "no", "letter_draft": "draft", "letter_submitted": "submitted",
                 "letter_all": "all", "letter_date": "date", "letter_date_pick": "date",
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
    letters_work.answer when it is there; else the store's verbs do what
    needs no model (Yes, No and Decline record the owner's decision and
    send nothing; a submission is recorded), and one EA turn does the
    rest (the materials request, the draft, the date). Never a close on a
    reply, never a decline drafted on its own."""
    if choice not in CHOICES:
        return {"ok": False, "said": "That choice is not known. Nothing was done."}
    cid = str(case.get("id") or "")
    run = runner or _cli

    def done(out: dict, yes: str) -> dict:
        ok = bool(out) and not out.get("error")
        return {"ok": ok, "said": yes if ok else "Your answer was not recorded. Ask me in Ask Tacitus."}
    if choice in ("join_same", "join_other"):
        if not question:
            return {"ok": False, "said": "It changed since the card. Nothing was done."}
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
    if choice in ("yes", "yes_ask", "yes_date", "no", "date"):
        by = None
        if choice != "no":
            by = _cs.picked_day(text, today_local()) if text else (
                None if choice == "yes_date" else _cs._dates().proposed(case))
            if (text or choice in ("yes_date", "date")) and not by:
                return {"ok": False, "said": _cs.PICK_A_DATE}
        key = "yes" if choice == "yes_date" else choice
        if choice == "date":
            argv = ["set-date", "--case", cid, "--date", by.isoformat()]
        else:
            argv = (["answer", "--question", question, "--choice", STORE_ANSWERS[key]] if question
                    else ["decline" if choice == "no" else "accept", "--case", cid])
            argv += ["--date", by.isoformat()] if by else []
        when = f", by {day_words(by, today_local())}" if by else ""
        got = done(run(argv), {"yes": f"You will write it{when}.",
                               "yes_ask": f"You will write it{when}. I am writing the request for the materials.",
                               "date": f"The date is {day_words(by, today_local()) if by else ''}.",
                               "no": "You will not write it. Nothing is sent."}[key])
        if got["ok"] and choice == "yes_ask":
            got["turn"] = _turn_prompt(case, choice, dest, "")
        return got
    if choice == "decline":
        return done(run(["decline", "--case", cid]), "Declined. Nothing is sent.")
    if choice == "all":
        opn = open_dests(case)
        for d in opn:
            if run(["record-submitted", "--dest", str(d.get("id") or ""), "--evidence",
                    "the owner said all are submitted, in Action requests"]).get("error"):
                return {"ok": False, "said": "Your answer was not recorded. Ask me in Ask Tacitus."}
        return {"ok": bool(opn), "said": "Submitted: all. It is off your list." if opn
                else "It changed since the card. Nothing was done."}
    if choice == "submitted":
        if not dest:
            return {"ok": False, "said": "Pick the destination you submitted."}
        return done(run(["record-submitted", "--dest", dest, "--evidence",
                         "the owner said so in Action requests"]), "Submitted.")
    return {"ok": True, "said": "I am writing the letter's draft.",
            "turn": _turn_prompt(case, choice, dest, text)}


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

_home_due = _cs._home_due


def home_items(src, cases=None, *, rows: dict | None = None) -> tuple[list[dict], set]:
    """(Home's items for the open letter cases, the request ids they stand for)."""
    cases = load_cases() if cases is None else cases
    return _cs.home_items(src, [_cs._tag(c, "letter") for c in cases], rows=rows)


def home_left(out: list[dict], src, *, cases=None) -> list[dict]:
    """Home's hook for the letter cases alone (case_surface.home_left
    holds every kind)."""
    cases = load_cases() if cases is None else cases
    return _cs.home_left(out, src, cases=[_cs._tag(c, "letter") for c in cases])


def home_brief(case: dict, today: date, lv: str = "full", news=None) -> dict:
    return _cs.home_brief(_cs._tag(case, "letter"), today, lv, news)


def home_choices(case: dict, today: date, lv: str = "full") -> list[dict]:
    return _cs.home_choices(_cs._tag(case, "letter"), today, lv)


def home_choice(choice_id: str) -> tuple[str, str]:
    """(answer, destination id) for a Home choice id `letter:<choice>[:<dest
    id>]`; ("", "") for any other id."""
    return _cs.home_choice(choice_id, ("letter",))


# ── the pre-send review (briefing_review.py) ──────────────────────────

def review_record(c: dict) -> dict | None:
    return _cs.review_record(c) if is_case(c) else None


def review_refusal(rec: dict, action: str, kind: str = "") -> str:
    if not isinstance((rec or {}).get("letter_case"), dict):
        return ""
    return _cs.review_refusal(rec, action, kind)


# ── a project room's name (briefing_rooms.py) ─────────────────────────

def room_title(c: dict) -> str:
    return _cs.room_title(c) if is_case(c) else ""


def collapse_rows(rows: list[dict], cases=None, *, today: date | None = None,
                  open_only: bool = True) -> list[dict]:
    """`rows` with the destinations of one case as one row."""
    if not any(is_letter_row(r) for r in rows if isinstance(r, dict)):
        return rows
    cases = load_cases(open_only=open_only) if cases is None else cases
    return _cs.collapse_rows(rows, [_cs._tag(c, "letter") for c in cases], today=today,
                             open_only=open_only, test=is_letter_row)
