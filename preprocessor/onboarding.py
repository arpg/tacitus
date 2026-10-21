#!/usr/bin/env python3
"""
First-week onboarding: one preference question a day, recorded as a rule.

Setup fills the config once, and the preferences store starts empty, so
the first briefings and drafts are generic. This module asks the bank in
`config/onboarding-questions.json` and turns each answer into a standing
rule in the user's own words, through `preferences.py` (kind and scope
from the question, the answer as the quote, source `onboarding`). The
morning briefing on a chat profile ends with the question of the day
(the preprocessor puts it in the briefing JSON as `onboarding_question`);
the email profile answers the bank in one sitting (`ask --count N`,
`scripts/install/user_init.py --phase preferences`).

State is `~/.ea/state/onboarding.json`, written through resident_io like
the other state files:
    {"version": 1, "done": false,
     "answered": {"<id>": {"at": "<iso>", "rule_id": "onboarding-<id>"}},
     "skipped": {"<id>": "<iso>"},
     "asked": {"<id>": "<iso of the last ask>"}}

A question is asked at most once a day: `next` returns nothing while a
question asked today is still unanswered and unskipped, unless --force.
`onboarding.enabled` in ea-config.json (default true) and the `done` flag
(`stop`) switch the questions off.

Usage:
    onboarding.py next [--count N] [--scope S] [--force] [--peek]
    onboarding.py ask [--count N] [--scope S] [--force]
    onboarding.py answer --id <id> --text "<the user's words>"
    onboarding.py skip --id <id>
    onboarding.py stop
    onboarding.py status [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import ea_config as _ea_config
    import preferences as _prefs
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ea_config as _ea_config
    from preprocessor import preferences as _prefs
    from preprocessor import resident_io as _rio

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
REPO = Path(__file__).resolve().parent.parent
BANK_FILE = REPO / "config" / "onboarding-questions.json"
SCRIPT = Path(__file__).resolve()

REQUIRED_FIELDS = ("id", "scope", "kind", "topic", "question", "why", "example")
SOURCE = "onboarding"
RULE_PREFIX = "onboarding-"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _ea_dir(ea_dir) -> Path:
    return Path(ea_dir) if ea_dir else EA_DIR


def state_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "state" / "onboarding.json"


def prefs_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "config" / "preferences.json"


def config_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "config" / "ea-config.json"


# ---------------------------------------------------------------------------
# The bank
# ---------------------------------------------------------------------------
def load_bank(path: Path | None = None) -> list[dict]:
    """The questions, in the order they are asked."""
    path = BANK_FILE if path is None else Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    questions = raw.get("questions") if isinstance(raw, dict) else raw
    return [q for q in (questions or []) if isinstance(q, dict)]


def validate_bank(questions: list[dict]) -> list[str]:
    """Problems with the bank: ids unique, fields present, scopes and kinds
    ones preferences.py accepts. Empty when the bank is sound."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, q in enumerate(questions):
        label = q.get("id") or f"#{i}"
        for f in REQUIRED_FIELDS:
            if not str(q.get(f) or "").strip():
                problems.append(f"{label}: missing {f}")
        qid = str(q.get("id") or "")
        if qid in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(qid)
        if q.get("kind") not in _prefs.KINDS:
            problems.append(f"{label}: kind {q.get('kind')!r} is not one of {_prefs.KINDS}")
        for scope in _prefs._norm_scopes([q.get("scope", "")]) or ["?"]:
            if scope.split(":", 1)[0] not in _prefs.SCOPES:
                problems.append(f"{label}: scope {scope!r} is not in preferences.SCOPES")
    return problems


def bank_by_id(questions: list[dict] | None = None) -> dict[str, dict]:
    return {q["id"]: q for q in (questions if questions is not None else load_bank())}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _empty_state() -> dict:
    return {"version": 1, "done": False, "answered": {}, "skipped": {}, "asked": {}}


def _parse_state(text: str) -> dict:
    data = _empty_state()
    if not text.strip():
        return data
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return data
    if isinstance(raw, dict):
        for k in ("answered", "skipped", "asked"):
            if isinstance(raw.get(k), dict):
                data[k] = raw[k]
        data["done"] = bool(raw.get("done", False))
    return data


def load_state(ea_dir=None) -> dict:
    """The state, or an empty one when the file is missing or unreadable."""
    text = _rio.read_text(state_file(ea_dir))
    return _parse_state(text or "")


def _update_state(mutator, ea_dir=None):
    """Read-modify-write under resident_io's lock; `mutator(state)` edits
    in place and its return value comes back."""
    box: dict = {}

    def fn(text: str) -> str:
        state = _parse_state(text)
        box["result"] = mutator(state)
        return json.dumps(state, indent=2) + "\n"

    if not _rio.update_text(state_file(ea_dir), fn):
        raise OSError(f"onboarding: could not update {state_file(ea_dir)}")
    return box.get("result")


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def enabled(ea_dir=None) -> bool:
    """`onboarding.enabled` in ea-config.json; missing means on."""
    block = _ea_config.load(config_file(ea_dir)).get("onboarding")
    if isinstance(block, dict):
        return bool(block.get("enabled", True))
    return True


def remaining_questions(questions: list[dict] | None = None, state: dict | None = None,
                        scope: str | None = None, ea_dir=None) -> list[dict]:
    """The questions not yet answered or skipped, in bank order."""
    questions = load_bank() if questions is None else questions
    state = load_state(ea_dir) if state is None else state
    out = []
    for q in questions:
        if q["id"] in state["answered"] or q["id"] in state["skipped"]:
            continue
        if scope and not any(
                s == scope or (s != "*" and (_prefs.scope_matches(s, scope)
                                             or _prefs.scope_matches(scope, s)))
                for s in _prefs._norm_scopes([q.get("scope", "")])):
            continue
        out.append(q)
    return out


def asked_today(state: dict, today: str) -> str | None:
    """The id of a question asked on `today` (YYYY-MM-DD) that is still
    open, or None."""
    for qid, at in state.get("asked", {}).items():
        if str(at)[:10] == today and qid not in state["answered"] and qid not in state["skipped"]:
            return qid
    return None


def next_questions(count: int = 1, scope: str | None = None, force: bool = False,
                   record: bool = True, ea_dir=None, now: datetime | None = None,
                   questions: list[dict] | None = None) -> list[dict]:
    """The next `count` questions to ask, each with `remaining` (how many
    are left, this one included) and `total`. Empty when the questions are
    off, done, or one asked today is still open (unless `force`). With
    `record`, the ask is written to the state so the once-a-day rule holds."""
    if not enabled(ea_dir):
        return []
    state = load_state(ea_dir)
    if state["done"]:
        return []
    today = _now(now)[:10]
    if not force and asked_today(state, today):
        return []
    questions = load_bank() if questions is None else questions
    left = remaining_questions(questions, state, scope)
    picked = left[:max(0, count)]
    if not picked:
        return []
    total = len(questions)
    out = []
    for i, q in enumerate(picked):
        row = dict(q)
        row["remaining"] = len(left) - i
        row["total"] = total
        out.append(row)
    if record:
        stamp = _now(now)

        def mark(s):
            for q in picked:
                s["asked"][q["id"]] = stamp
        _update_state(mark, ea_dir)
    return out


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def rule_text(question: dict, answer: str) -> str:
    return f"{question['topic']}: {answer}"


def record_answer(qid: str, text: str, ea_dir=None, now: datetime | None = None,
                  questions: list[dict] | None = None) -> dict:
    """Record the user's answer as a rule in the preferences store and mark
    the question answered. A second answer to the same question retires
    the first rule. Returns {"question": <bank row>, "rule": <rule>}."""
    bank = bank_by_id(questions)
    if qid not in bank:
        raise KeyError(f"no onboarding question with id {qid!r}")
    answer = " ".join((text or "").split())
    if not answer:
        raise ValueError("--text is required: the answer in the user's words")
    q = bank[qid]
    prior = load_state(ea_dir)["answered"].get(qid, {}).get("rule_id")

    def mutate(data: dict) -> dict:
        if prior:
            try:
                _prefs.retire_rule(data, prior, "replaced by a later onboarding answer")
            except KeyError:
                pass
        rule = _prefs.add_rule(data, kind=q["kind"], scopes=[q["scope"]],
                               text=rule_text(q, answer), quote=answer,
                               source="chat", rule_id=RULE_PREFIX + qid)
        # preferences.py validates against its own source list; the stored
        # source names where this rule came from.
        rule["source"] = SOURCE
        return rule

    rule = _prefs.update(mutate, prefs_file(ea_dir))
    stamp = _now(now)

    def mark(s):
        s["answered"][qid] = {"at": stamp, "rule_id": rule["id"]}
        s["skipped"].pop(qid, None)
    _update_state(mark, ea_dir)
    return {"question": q, "rule": rule}


def skip_question(qid: str, ea_dir=None, now: datetime | None = None,
                  questions: list[dict] | None = None) -> dict:
    bank = bank_by_id(questions)
    if qid not in bank:
        raise KeyError(f"no onboarding question with id {qid!r}")
    stamp = _now(now)
    _update_state(lambda s: s["skipped"].__setitem__(qid, stamp), ea_dir)
    return {"skipped": qid, "at": stamp}


def stop(ea_dir=None) -> dict:
    """The user said stop: the whole bank is done."""
    _update_state(lambda s: s.__setitem__("done", True), ea_dir)
    return {"done": True}


def status(ea_dir=None, now: datetime | None = None,
           questions: list[dict] | None = None) -> dict:
    questions = load_bank() if questions is None else questions
    state = load_state(ea_dir)
    left = remaining_questions(questions, state)
    return {
        "enabled": enabled(ea_dir),
        "done": state["done"],
        "complete": not left,
        "answered": [q["id"] for q in questions if q["id"] in state["answered"]],
        "skipped": [q["id"] for q in questions if q["id"] in state["skipped"]],
        "remaining": [q["id"] for q in left],
        "asked_today": asked_today(state, _now(now)[:10]),
        "total": len(questions),
        "file": str(state_file(ea_dir)),
    }


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def ask_text(questions: list[dict], script: Path | str | None = None) -> str:
    """The questions as text for a prompt or a report: question, why,
    example, and the exact answer and skip commands."""
    script = str(script or SCRIPT)
    if not questions:
        return "No question today.\n"
    left = questions[0].get("remaining", len(questions))
    lines = [f"First-week questions ({len(questions)} of {left} remaining). Answer each with the "
             "command under it, in your own words; a question you leave comes back another day.",
             ""]
    for i, q in enumerate(questions, 1):
        lines += [
            f"{i}. {q['question']}",
            f"   Why: {q['why']}",
            f"   Example: {q['example']}",
            f'   Answer: python3 {script} answer --id {q["id"]} --text "<your words>"',
            f"   Skip:   python3 {script} skip --id {q['id']}",
            "",
        ]
    lines.append(f"Stop the questions for good: python3 {script} stop")
    return "\n".join(lines) + "\n"


def status_text(st: dict) -> str:
    if not st["enabled"]:
        head = "Onboarding is off (onboarding.enabled in ea-config.json)."
    elif st["done"]:
        head = "Onboarding is done (the user said stop)."
    elif st["complete"]:
        head = "Onboarding is complete: every question is answered or skipped."
    else:
        head = f"Onboarding: {len(st['remaining'])} of {st['total']} questions remain."
    lines = [head,
             f"answered: {', '.join(st['answered']) or 'none'}",
             f"skipped: {', '.join(st['skipped']) or 'none'}",
             f"remaining: {', '.join(st['remaining']) or 'none'}"]
    if st.get("asked_today"):
        lines.append(f"asked today, still open: {st['asked_today']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_next(args) -> int:
    rows = next_questions(count=args.count, scope=args.scope, force=args.force,
                          record=not args.peek)
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def _cmd_ask(args) -> int:
    rows = next_questions(count=args.count, scope=args.scope, force=args.force)
    sys.stdout.write(ask_text(rows))
    return 0


def _cmd_answer(args) -> int:
    try:
        res = record_answer(args.id, args.text)
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    rule = res["rule"]
    print(json.dumps({"recorded": rule, "question": args.id, "file": str(_prefs.PREFS_FILE)},
                     ensure_ascii=False))
    print(f"Recorded rule {rule['id']} ({rule['kind']}; {', '.join(rule['scopes'])}): "
          f"{rule['text']}")
    return 0


def _cmd_skip(args) -> int:
    try:
        print(json.dumps(skip_question(args.id)))
    except KeyError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    return 0


def _cmd_stop(args) -> int:
    print(json.dumps(stop()))
    return 0


def _cmd_status(args) -> int:
    st = status()
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        sys.stdout.write(status_text(st))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="First-week onboarding questions", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("next", "the next question(s) as JSON"),
                        ("ask", "the next question(s) as text, with the answer command")):
        p = sub.add_parser(name, help=help_, allow_abbrev=False)
        p.add_argument("--count", type=int, default=1)
        p.add_argument("--scope", help="only questions in this scope")
        p.add_argument("--force", action="store_true",
                       help="ask even when a question asked today is still open")
        if name == "next":
            p.add_argument("--peek", action="store_true", help="do not record the ask")
    p = sub.add_parser("answer", help="record the user's answer as a rule", allow_abbrev=False)
    p.add_argument("--id", required=True)
    p.add_argument("--text", required=True, help="the answer in the user's words")
    p = sub.add_parser("skip", help="mark a question skipped", allow_abbrev=False)
    p.add_argument("--id", required=True)
    sub.add_parser("stop", help="the user said stop: mark the bank done", allow_abbrev=False)
    p = sub.add_parser("status", help="answered, skipped, remaining", allow_abbrev=False)
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return {"next": _cmd_next, "ask": _cmd_ask, "answer": _cmd_answer,
            "skip": _cmd_skip, "stop": _cmd_stop, "status": _cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
