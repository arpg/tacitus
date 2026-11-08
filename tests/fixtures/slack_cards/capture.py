#!/usr/bin/env python3
"""Capture golden Block Kit output from the bot's current card builders.

Each builder is sliced out of ea-chat-bot.py as source text and exec'd into
a namespace whose Slack client records every call's kwargs. Inputs are fixed,
every nondeterministic dependency is stubbed, so a re-run against unchanged
source writes identical files.

    .venv/bin/python3 tests/fixtures/slack_cards/capture.py          # write
    .venv/bin/python3 tests/fixtures/slack_cards/capture.py --check  # compare

The script fails loudly (SystemExit, nonzero) when a builder is missing or its
signature no longer matches the one the inputs were written for. README.md in
this directory lists the stubs and inputs per case.
"""

import argparse
import copy
import inspect
import json
import os
import re
import sys
import threading
import time
import types
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

# Fixed inputs shared by every case.
THREAD_TS = "1700000000.000100"
CHANNEL = "C_MAIN"
DM = "D_OWNER"
DRAFT_ID = "AAMkDRAFT1"
CONFIRM_TS = "1700000001.000200"
PERMALINK = "https://example.slack.com/archives/D_OWNER/p1700000001000200"
PAYLOAD_REF = "0123456789abcdef0123456789abcdef"
BATCH_HEX = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
ZOOM = "https://example.zoom.us/j/1234567890"

# The signatures the inputs below are written for. A mismatch stops the run.
EXPECTED_SIGNATURES = {
    "_build_send_confirmation":
        "(channel: str, thread_ts: str, draft_id: str, email_system: str = 'outlook', "
        "gmail_email: str = '') -> tuple",
    "_post_schedule_send_confirmation":
        "(channel: str, thread_ts: str | None, draft_id: str, send_after: str, "
        "description: str = '', ledger_id: str = '')",
    "_post_bulk_card": "() -> None",
    "_post_event_confirmation":
        "(channel: str, thread_ts: str, subject: str, start: str, end: str, "
        "attendees: str, location: str = '', body: str = '', online: bool = False, "
        "repeat: dict | None = None, hold: bool = False)",
    "_post_event_update_confirmation":
        "(channel: str, thread_ts: str, event_id: str, subject: str, new_start: str = '', "
        "new_end: str = '', attendees: str = '', location: str = '', original_start: str = '')",
    "_post_event_delete_confirmation":
        "(channel: str, thread_ts: str, event_id: str, subject: str, start: str = '', "
        "end: str = '')",
    "_room_picker": "(channel: str, thread_ts: str, query: str, candidates: list) -> dict",
    "_replace_buttons_with_status": "(channel: str, message_ts: str, status_text: str)",
    "_post_dm_breadcrumb":
        "(ea_channel: str, thread_ts: str | None, dm_channel: str, dm_ts: str | None, "
        "what: str) -> None",
}

# Real helpers exec'd alongside the builders (pure, or made pure by stubs).
HELPERS = ("_format_send_after", "_repeat_summary", "_resolve_online_location",
           "_deliver_confirmation")


def die(msg: str) -> None:
    raise SystemExit(f"capture.py: {msg}")


def fn_src(name: str) -> str:
    """One top-level function's source, up to the next top-level def or
    @app.action/@app.view decorator."""
    marker = f"\ndef {name}("
    if BOT_SRC.count(marker) != 1:
        die(f"{name} is defined {BOT_SRC.count(marker)} times in ea-chat-bot.py, expected 1")
    start = BOT_SRC.index(marker)
    ends = [BOT_SRC.index("\ndef ", start + 1)]
    for deco in ("@app.action(", "@app.view(", "@app.event("):
        nxt = BOT_SRC.find("\n" + deco, start + 1)
        if nxt > 0:
            ends.append(nxt)
    return BOT_SRC[start:min(ends)]


def block_src(start_marker: str, end_marker: str) -> str:
    start = BOT_SRC.index(start_marker)
    return BOT_SRC[start:BOT_SRC.index(end_marker, start)]


class Recorder:
    """The fake Slack client: every call appends (method, kwargs)."""

    def __init__(self):
        self.calls: list = []
        self.permalink_error = False

    def chat_postMessage(self, **kw):
        self.calls.append({"method": "chat_postMessage", "kwargs": copy.deepcopy(kw)})
        return {"ok": True, "ts": CONFIRM_TS}

    def chat_update(self, **kw):
        self.calls.append({"method": "chat_update", "kwargs": copy.deepcopy(kw)})
        return {"ok": True}

    def chat_getPermalink(self, **kw):
        self.calls.append({"method": "chat_getPermalink", "kwargs": copy.deepcopy(kw)})
        if self.permalink_error:
            raise RuntimeError("permalink lookup refused")
        return {"ok": True, "permalink": PERMALINK}


def _quiet(*a, **k):
    return None


def build_ns(*, draft=None, sched_warning="", dm=DM, stash_ref=PAYLOAD_REF,
             confirms=None, bulk=None, active=None, permalink_error=False):
    rec = Recorder()
    rec.permalink_error = permalink_error
    ns = {
        "json": json, "os": os, "re": re, "time": time, "threading": threading,
        "datetime": datetime, "Path": Path, "EA_DIR": Path("/nonexistent-ea-dir"),
        "log": types.SimpleNamespace(info=_quiet, debug=_quiet, warning=_quiet, error=_quiet),
        "app": types.SimpleNamespace(client=rec),
        # draft fetch and live-calendar preflight
        "_fetch_draft_body": lambda draft_id, email_system="outlook", gmail_email="": copy.deepcopy(draft),
        "_scheduling_preflight": lambda body: sched_warning,
        # delivery side effects that are not part of the card
        "_resolve_dm_channel": lambda: dm,
        "_note_run_post": lambda kind: None,
        "_note_draft_confirmation": lambda *a, **k: None,
        "_retire_confirmation_message": lambda channel, message_ts: None,
        "_zoom_link": lambda: ZOOM,
    }
    # _ONLINE_PLACEHOLDERS comes from the source so a new placeholder shows up.
    exec(compile(block_src("_ONLINE_PLACEHOLDERS = ", "\n\n\n"), "bot[const]", "exec"), ns)
    # The staged-payload store, then a fixed key in place of uuid4 + the file.
    exec(compile(block_src("# ── Staged button payloads", "\ndef _post_event_confirmation("),
                 "bot[staged]", "exec"), ns)
    ns["_stash_button_payload"] = lambda payload: stash_ref
    # The bulk card's state, in memory.
    store = {"batches": {}}
    ns.update({
        # the picker is a collaborator-mode card: rooms on
        "_rooms_on": lambda: True, "ROOMS_OFF_TEXT": "",
        "_BULK_LOCK": threading.Lock(),
        "_BULK_PENDING": {"ids": list((bulk or {}).get("ids", [])), "timer": None,
                          "dm_channel": (bulk or {}).get("dm_channel", "")},
        "_load_draft_confirms": lambda: copy.deepcopy(confirms or {}),
        "_load_bulk_batches": lambda: copy.deepcopy(store["batches"]),
        "_save_bulk_batches": lambda data: store.update(batches=copy.deepcopy(data)),
        "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=BATCH_HEX)),
    })
    for name in HELPERS + ("_post_dm_breadcrumb",) + tuple(EXPECTED_SIGNATURES):
        exec(compile(fn_src(name), f"bot[{name}]", "exec"), ns)
    for name, want in EXPECTED_SIGNATURES.items():
        got = str(inspect.signature(ns[name]))
        if got != want:
            die(f"{name} signature changed:\n  expected {want}\n  found    {got}\n"
                f"update the inputs and EXPECTED_SIGNATURES together")
    fake_ps = types.ModuleType("preprocessor.project_store")
    fake_ps.list_active = lambda: copy.deepcopy(active or [])
    return ns, rec, fake_ps


def call_room_picker(ns, fake_ps, *args):
    """_room_picker imports preprocessor.project_store; hand it the fake."""
    saved = {k: sys.modules.get(k) for k in ("preprocessor", "preprocessor.project_store")}
    pkg = types.ModuleType("preprocessor")
    pkg.project_store = fake_ps
    sys.modules["preprocessor"] = pkg
    sys.modules["preprocessor.project_store"] = fake_ps
    try:
        return ns["_room_picker"](*args)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

DRAFT_BASIC = {"to": "Alex Rivera <alex.rivera@example.edu>",
               "cc": "Sam Lee <sam.lee@example.edu>",
               "subject": "Re: Thursday lab meeting",
               "body": "Hi Alex,\n\nThursday at 2 works. I will bring the draft figures.\n\nKit",
               "conversation_id": "AAQkCONV1"}
DRAFT_NO_CC = {"to": "pat@example.org", "cc": "", "subject": "Quick question",
               "body": "Hi Pat,\n\nCan you send the slides?\n\nKit",
               "conversation_id": ""}
LONG_BODY = "".join(f"Paragraph {i:03d}: the long body keeps going past the Slack cap. "
                    for i in range(60))  # 3780 characters
DRAFT_LONG = {**DRAFT_BASIC, "subject": "Re: Long reply", "body": LONG_BODY}
DRAFT_EMPTY = {**DRAFT_NO_CC, "subject": "Empty", "body": ""}
SCHED_WARNING = (":warning: *Scheduling check:* Tue Oct 6, 2:00 PM is no longer free "
                 "(conflicts with Faculty meeting).")
AGENDA = "Agenda item that runs long. " * 120

CANDIDATES = [{"id": "wildfire-sensor-grid", "name": "Wildfire Sensor Grid", "score": 5},
              {"id": "garage-roof-replacement", "name": "Garage roof replacement", "score": 3},
              {"id": "robo5000-coteach", "name": "ROBO 5000 co-teach", "score": 2}]
ACTIVE = [{"id": "wildfire-sensor-grid", "name": "Wildfire Sensor Grid"},
          {"id": "garage-roof-replacement", "name": "Garage roof replacement"},
          {"id": "robo5000-coteach", "name": "ROBO 5000 co-teach"},
          {"id": "budget-plan-2027", "name": "Budget plan 2027-28"},
          {"id": "unnamed-project", "name": ""}]


def cases():
    """(builder, case, stub kwargs for build_ns, call args) for every golden."""
    b = "_build_send_confirmation"
    yield b, "basic", {"draft": DRAFT_BASIC}, (CHANNEL, THREAD_TS, DRAFT_ID)
    yield b, "no_cc", {"draft": DRAFT_NO_CC}, (CHANNEL, THREAD_TS, DRAFT_ID)
    yield b, "long_body", {"draft": DRAFT_LONG}, (CHANNEL, THREAD_TS, DRAFT_ID)
    yield b, "empty_body", {"draft": DRAFT_EMPTY}, (CHANNEL, THREAD_TS, DRAFT_ID)
    yield b, "sched_warning", {"draft": DRAFT_BASIC, "sched_warning": SCHED_WARNING}, \
        (CHANNEL, THREAD_TS, DRAFT_ID)
    # Gmail skips the preflight even when it would warn.
    yield b, "gmail", {"draft": DRAFT_NO_CC, "sched_warning": SCHED_WARNING}, \
        (CHANNEL, THREAD_TS, "r-1234567890123", "gmail", "jo.ambrose4@example.com")

    b = "_post_schedule_send_confirmation"
    yield b, "basic", {"draft": DRAFT_BASIC}, \
        (CHANNEL, THREAD_TS, DRAFT_ID, "2030-04-21T09:00", "Reply to Alex Monday", "ledger-abc123")
    yield b, "sched_warning_long_body", {"draft": DRAFT_LONG, "sched_warning": SCHED_WARNING}, \
        (CHANNEL, THREAD_TS, DRAFT_ID, "2030-04-21T09:00", "x" * 400, "")
    yield b, "no_dm", {"draft": DRAFT_NO_CC, "dm": None}, \
        (CHANNEL, THREAD_TS, DRAFT_ID, "2030-04-21T09:00", "", "")

    b = "_post_bulk_card"
    live = {"live": {"status": "live"}, "sent": {"status": "sent"}}
    yield b, "two_live", {"bulk": {"ids": ["AAMkDRAFT1", "AAMkDRAFT2"], "dm_channel": DM},
                          "confirms": {"AAMkDRAFT1": live["live"], "AAMkDRAFT2": live["live"]}}, ()
    yield b, "three_live_one_sent", {
        "bulk": {"ids": ["AAMkDRAFT1", "AAMkDRAFT2", "AAMkDRAFT3", "AAMkDRAFT4"], "dm_channel": DM},
        "confirms": {"AAMkDRAFT1": live["live"], "AAMkDRAFT2": live["live"],
                     "AAMkDRAFT3": live["live"], "AAMkDRAFT4": live["sent"]}}, ()

    b = "_post_event_confirmation"
    yield b, "basic", {}, (CHANNEL, THREAD_TS, "Project sync", "2026-10-06T14:00",
                           "2026-10-06T14:30", "alex.rivera@example.edu,sam.lee@example.edu")
    yield b, "full", {}, (CHANNEL, THREAD_TS, "Weekly group meeting", "2026-10-06T14:00",
                          "2026-10-06T15:00", "alex.rivera@example.edu", "Zoom", AGENDA, True,
                          {"repeat": "weekly", "repeat_days": "TU,TH", "repeat_interval": 2,
                           "repeat_until": "2026-12-03"}, False)
    yield b, "hold", {}, (CHANNEL, THREAD_TS, "Hold: NSF panel prep", "2026-10-07T09:00",
                          "2026-10-07T10:00", "", "Room 717", "", False, None, True)
    yield b, "no_dm", {"dm": None}, (CHANNEL, THREAD_TS, "Project sync", "2026-10-06T14:00",
                                     "2026-10-06T14:30", "alex.rivera@example.edu")
    yield b, "inline_fallback", {"stash_ref": ""}, (CHANNEL, THREAD_TS, "Project sync",
                                                    "2026-10-06T14:00", "2026-10-06T14:30",
                                                    "alex.rivera@example.edu", "", AGENDA)

    b = "_post_event_update_confirmation"
    seven = ",".join(f"person{i}@example.edu" for i in range(1, 8))
    yield b, "full", {}, (CHANNEL, THREAD_TS, "AAMkEVENT1", "Project sync", "2026-10-08T10:00",
                          "2026-10-08T10:30", seven, "Room 717", "2026-10-06T14:00")
    yield b, "minimal", {}, (CHANNEL, THREAD_TS, "AAMkEVENT1", "Project sync")

    b = "_post_event_delete_confirmation"
    yield b, "with_time", {}, (CHANNEL, THREAD_TS, "AAMkEVENT1", "Project sync",
                               "2026-10-06T14:00", "2026-10-06T14:30")
    yield b, "no_time", {}, (CHANNEL, THREAD_TS, "AAMkEVENT1", "Project sync")

    b = "_room_picker"
    yield b, "query_three_candidates", {"active": ACTIVE}, \
        (CHANNEL, THREAD_TS, "wildfire sensor", CANDIDATES)
    yield b, "thread_three_candidates", {"active": ACTIVE}, (CHANNEL, THREAD_TS, "", CANDIDATES)
    yield b, "query_no_candidates_no_active", {"active": []}, \
        (CHANNEL, THREAD_TS, "brand new thing", [])

    b = "_replace_buttons_with_status"
    yield b, "short", {}, (DM, CONFIRM_TS, ":white_check_mark: Sent to alex.rivera@example.edu.")
    yield b, "long", {}, (DM, CONFIRM_TS, ":x: " + "status detail " * 300)

    b = "_post_dm_breadcrumb"
    yield b, "with_permalink", {}, (CHANNEL, THREAD_TS, DM, CONFIRM_TS,
                                    "The Send button for this draft")
    yield b, "permalink_error", {"permalink_error": True}, \
        (CHANNEL, THREAD_TS, DM, CONFIRM_TS, "The Send button for this draft")
    yield b, "no_dm_ts", {}, (CHANNEL, THREAD_TS, DM, None, "The Create button for this event")
    yield b, "skipped_same_channel", {}, (DM, THREAD_TS, DM, CONFIRM_TS,
                                          "The Send button for this draft")


def _card_call(calls):
    """The call that carries the card: the first post or update with blocks,
    else the first post or update."""
    for c in calls:
        if c["method"] != "chat_getPermalink" and "blocks" in c["kwargs"]:
            return c
    for c in calls:
        if c["method"] != "chat_getPermalink":
            return c
    return None


def run_case(builder, case, stubs, args):
    ns, rec, fake_ps = build_ns(**stubs)
    params = list(inspect.signature(ns[builder]).parameters)
    inputs = {"args": dict(zip(params, args)),
              "stubs": {k: v for k, v in stubs.items()}}
    out = {"inputs": inputs}
    if builder == "_build_send_confirmation":
        text, blocks = ns[builder](*args)
        if text is None:
            die(f"{builder}/{case} returned an error: {blocks}")
        out.update(text=text, blocks=blocks, kwargs={})
    elif builder == "_room_picker":
        res = call_room_picker(ns, fake_ps, *args)
        out.update(text=res["text"], blocks=res["blocks"],
                   kwargs={}, result={k: v for k, v in res.items() if k not in ("text", "blocks")})
    else:
        ns[builder](*args)
        card = _card_call(rec.calls)
        kw = card["kwargs"] if card else {}
        out.update(text=kw.get("text"), blocks=kw.get("blocks", []), kwargs=kw)
    out["calls"] = rec.calls
    return out


def render(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="compare against the files on disk; write nothing")
    ap.add_argument("--out", default=str(HERE))
    a = ap.parse_args()
    out_dir = Path(a.out)
    seen, diffs = set(), []
    for builder, case, stubs, args in cases():
        name = f"{builder.lstrip('_')}__{case}.json"
        if name in seen:
            die(f"duplicate case {name}")
        seen.add(name)
        text = render(run_case(builder, case, stubs, args))
        path = out_dir / name
        if a.check:
            if not path.exists() or path.read_text() != text:
                diffs.append(name)
        else:
            path.write_text(text)
        blocks = json.loads(text)["blocks"]
        print(f"{name}: {len(blocks)} blocks")
    covered = {n.split("__")[0] for n in seen}
    missing = {b.lstrip("_") for b in EXPECTED_SIGNATURES} - covered
    if missing:
        die(f"builders with no case: {sorted(missing)}")
    if diffs:
        print("differs from the golden: " + ", ".join(diffs), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
