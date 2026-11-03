#!/usr/bin/env python3
"""autonomy_tiers.py: a task's autonomy tier, in ~/.ea/config/autonomy-tiers.json.

    autonomy_tiers.py promote --task <task> --tier <tier> [--contact <addr>] --quote "<the user's words>"
    autonomy_tiers.py show [--task <task>]

A promotion is the user's, in chat ("for LoR uploads, just do them and
tell me"; "for scheduling with Dr. Smith, just send it"): the quote is
their words. With --contact the task gets a per-contact override, which
the task must allow (`promotable_per_contact`); without it the task's own
tier changes. A button-required task is never promoted, and no task is
promoted to button-required: the buttons are code, not a tier.

The write goes through resident_io. From a Codex turn, whose sandbox
holds no config folder, it is staged the way a preference is
(preferences.stage) and applied after the turn when the quote is the
user's words in that turn's message.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import resident_io as _resident_io  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
TIERS_FILE = EA_DIR / "config" / "autonomy-tiers.json"
TIERS = ("button-required", "ask-first", "do-and-tell", "fire-and-forget")
SOURCES = ("chat", "edit")
SANDBOX_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS)


def load(path: Path | None = None) -> dict:
    path = TIERS_FILE if path is None else path
    text = _resident_io.read_text(path)
    if not text:
        raise OSError(f"autonomy_tiers: could not read {path}")
    return json.loads(text)


def promote_in(data: dict, *, task: str, tier: str, contact: str = "", quote: str = "",
               source: str = "chat", today: str = "") -> dict:
    """Change `data` in place; returns what changed. ValueError when the
    promotion is not one the config allows."""
    tasks = data.get("task_tiers") or {}
    if task not in tasks or not isinstance(tasks[task], dict):
        raise ValueError(f"no task {task!r}; tasks: {', '.join(k for k in tasks if not k.startswith('_'))}")
    if tier not in TIERS:
        raise ValueError(f"no tier {tier!r}; tiers: {', '.join(TIERS)}")
    row = tasks[task]
    if row.get("tier") == "button-required" or tier == "button-required":
        raise ValueError(f"{task} is button-required: a button is code, not a tier, and cannot be promoted or set")
    quote = " ".join((quote or "").split())
    if not quote:
        raise ValueError("--quote is required: the user's words")
    stamp = {"on": today or date.today().isoformat(), "quote": quote, "source": source}
    if contact:
        if not row.get("promotable_per_contact"):
            raise ValueError(f"{task} has no per-contact tier (promotable_per_contact is not set)")
        addr = contact.strip().lower()
        overrides = data.setdefault("per_contact_overrides", {})
        entry = overrides.get(addr)
        if not isinstance(entry, dict):
            entry = overrides[addr] = {}
        before = entry.get(task)
        entry[task] = tier
        entry.setdefault("_promoted", {})[task] = stamp
        return {"task": task, "contact": addr, "tier": tier, "was": before}
    before = row.get("tier")
    row["tier"] = tier
    row["promoted"] = dict(stamp, **{"from": before})
    return {"task": task, "tier": tier, "was": before}


def promote(path: Path | None = None, **kw) -> dict:
    """The promotion written to the config under resident_io's lock."""
    path = TIERS_FILE if path is None else path
    box: dict = {}

    def fn(text: str) -> str:
        data = json.loads(text) if text.strip() else {}
        box["result"] = promote_in(data, **kw)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if not _resident_io.update_text(path, fn):
        raise OSError(f"autonomy_tiers: could not update {path}")
    return box["result"]


def _cmd_promote(args, argv: list[str]) -> int:
    refused = _resident_io.write_refused(TIERS_FILE)
    if refused in SANDBOX_ERRNOS:
        # A Codex turn: the config folder is not among its writable roots.
        # Staged, applied after the turn when the quote is the user's words.
        try:
            import preferences as _prefs
        except ModuleNotFoundError:
            from preprocessor import preferences as _prefs
        _prefs.stage("autonomy", argv)
        print(json.dumps({"status": "staged", "file": str(_prefs.STAGED_FILE),
                          "note": "applied after this turn when the quote is the user's words"}))
        return 0
    try:
        res = promote(task=args.task, tier=args.tier, contact=args.contact or "",
                      quote=args.quote, source=args.source)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps({"promoted": res, "file": str(TIERS_FILE)}, ensure_ascii=False))
    return 0


def _cmd_show(args, argv: list[str]) -> int:
    data = load()
    tasks = {k: v for k, v in (data.get("task_tiers") or {}).items() if not k.startswith("_")}
    if args.task:
        if args.task not in tasks:
            print(json.dumps({"error": f"no task {args.task!r}"}))
            return 2
        tasks = {args.task: tasks[args.task]}
    overrides = {k: v for k, v in (data.get("per_contact_overrides") or {}).items() if not k.startswith("_")}
    print(json.dumps({"task_tiers": tasks, "per_contact_overrides": overrides}, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A task's autonomy tier", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote", help="change a task's tier, or a contact's tier for it", allow_abbrev=False)
    p.add_argument("--task", required=True)
    p.add_argument("--tier", required=True, choices=TIERS)
    p.add_argument("--contact", help="a per-contact override for this task")
    p.add_argument("--quote", required=True, help="the user's exact words")
    p.add_argument("--source", default="chat", choices=SOURCES)
    p = sub.add_parser("show", help="the tiers on file", allow_abbrev=False)
    p.add_argument("--task")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)
    return {"promote": _cmd_promote, "show": _cmd_show}[args.cmd](args, argv)


if __name__ == "__main__":
    sys.exit(main())
