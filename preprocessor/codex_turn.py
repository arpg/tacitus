#!/usr/bin/env python3
"""One chat turn on Codex, the production path and its ghost.

The bot builds a turn's prompt once (the fresh prompt with the operating
instructions, or the slim continuation for a thread with history) and
hands it to Claude Code. `run_turn` runs that same prompt on OpenAI's
Codex CLI as the resident, on a Codex thread kept per chat thread, and
answers in the shape the bot reads from Claude (`output`, `tool_calls`,
`num_turns`, `duration_ms`, `returncode`), so the rest of the turn (the
post, the drafts surfaced by run id, the staged events, the footer) is
the same code.

`live=True` is production: the real EA_DIR, writes confined by the
sandbox to the EA state, docs and research, the run id the bot minted,
drafts and staged events found by the bot as after a Claude turn.

`live=False` is the ghost: the same prompt against a scratch EA_DIR for
the thread (a copy of the ledger, the project store and the briefing,
links to the config and the mediator's queue), the real EA_DIR's path
replaced by the scratch path in the prompt, a run id the mediator treats
as a ghost (mail writes recorded, never executed), nothing posted. What
the turn staged (pending-event*.json, pending-schedule-send.json) is read
from the scratch state and returned as `staged`, so the judge sees it.

Codex threads: the first turn in a chat thread starts a Codex thread
(`codex exec`, not ephemeral) and its id is kept in the owner-private
registry, one per chat thread and mode; later turns resume it
(`codex exec resume <id>`). A resume that fails starts a new thread with
the fresh prompt the caller passes as `fresh_prompt`. The ghost arm
passes `persist=False`: each replay is a new thread on a new scratch,
kept nowhere, and the scratch is removed after the turn.

A ghost's thread id and a production thread id are never the same
registry row: the ghost's scratch root is that thread's root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_cli  # noqa: E402
import codex_ghost  # noqa: E402
import remote_llm  # noqa: E402

BOT_HOME = codex_ghost.BOT_HOME
BOT_TREE = codex_ghost.BOT_TREE
REAL_EA_DIR = BOT_HOME / ".ea"
THREADS = f"{codex_ghost.STORE}/threads.json"
TURN_TIMEOUT_S = 600
ROOM_TIMEOUT_S = 1200
# What a production turn may write: the EA state and the directories the
# EA writes documents into. Never the code, never the config.
LIVE_WRITABLE = ("state", "docs", "research", "letters", "uploads", "receipts")
STAGED_FILES = ("pending-event.json", "pending-event-update.json", "pending-event-delete.json",
                "pending-schedule-send.json", "pending-channel.json", "pending-publish.json")


# ── the thread registry ───────────────────────────────────────────────

def _registry() -> dict:
    return codex_ghost._read_private(THREADS) or {}


def _save_registry(reg: dict) -> None:
    codex_ghost._write_private(THREADS, reg)


def thread_row(thread_key: str, mode: str) -> dict:
    """{"thread_id", "root", "started", "turns"} for this chat thread and
    mode (`live` or `ghost`), or {}."""
    return (_registry().get(thread_key) or {}).get(mode) or {}


def remember_thread(thread_key: str, mode: str, thread_id: str, root: Path) -> None:
    reg = _registry()
    row = reg.setdefault(thread_key, {})
    old = row.get(mode) or {}
    row[mode] = {"thread_id": thread_id, "root": str(root), "started": old.get("started") or codex_ghost._iso(codex_ghost._now()),
                 "turns": int(old.get("turns") or 0) + 1, "last": codex_ghost._iso(codex_ghost._now())}
    _save_registry(reg)


def forget_thread(thread_key: str, mode: str) -> None:
    reg = _registry()
    if thread_key in reg and mode in reg[thread_key]:
        del reg[thread_key][mode]
        if not reg[thread_key]:
            del reg[thread_key]
        _save_registry(reg)


# ── prompts ───────────────────────────────────────────────────────────

def ghost_prompt(prompt: str, scratch: Path) -> str:
    """The production prompt with the real EA_DIR's path replaced by the
    scratch root, so a staged file or a ledger command lands in the
    scratch, and the Codex suffix."""
    text = prompt.replace(str(REAL_EA_DIR), str(scratch))
    return text + codex_ghost.codex_suffix(scratch, "chat")


def live_prompt(prompt: str) -> str:
    return prompt + codex_ghost.codex_suffix(REAL_EA_DIR, "chat", live=True)


# ── staged files in a ghost's scratch ─────────────────────────────────

def read_staged(root: Path) -> dict:
    """{name: parsed JSON or text} for each staged file in the scratch
    state; the owner reads the resident's files through the group."""
    out = {}
    for name in STAGED_FILES:
        p = root / "state" / name
        try:
            text = p.read_text()
        except OSError:
            continue
        try:
            out[name] = json.loads(text)
        except ValueError:
            out[name] = text[:4000]
    return out


def clear_staged(root: Path, runner=None) -> None:
    """Remove a ghost's staged files as the resident, so the next turn in
    the thread starts clean (a production turn's files are the bot's)."""
    paths = [str(root / "state" / n) for n in STAGED_FILES]
    cmd = codex_ghost._bot_env() + ["rm", "-f", "--"] + paths
    run = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=30))
    try:
        run(cmd)
    except Exception:
        pass


# ── the turn ──────────────────────────────────────────────────────────

def _shape(res: dict, thread_id: str, mode: str, staged: dict | None = None) -> dict:
    """The result in the bot's shape, plus the harness fields."""
    calls = []
    for c in res.get("tool_calls") or []:
        cmd = str((c.get("input") or {}).get("command") or "")
        calls.append(("Bash", 0.0, cmd[:600], str(c.get("output") or "")[-300:], {"command": cmd}))
    return {"output": res.get("output") or "", "tool_calls": calls,
            "returncode": 0 if not res.get("harness_error") else (res.get("exit_code") or 1),
            "num_turns": int(res.get("num_turns") or 0), "duration_ms": int(float(res.get("duration_s") or 0) * 1000),
            "usage": res.get("usage") or {}, "harness_error": res.get("harness_error") or "",
            "warnings": res.get("warnings") or [], "thread_id": thread_id, "mode": mode,
            "staged": staged or {}, "files": res.get("files") or []}


def run_turn(prompt: str, *, thread_key: str, run_id: str, live: bool, fresh_prompt: str = "",
             lane: str = "", model: str = "", timeout: int | None = None, codex_run=None,
             scratch_runner=None, clear_runner=None, on_event=None, persist: bool = True,
             keep_scratch: bool = False) -> dict:
    """One turn. `prompt` is what the bot would send Claude this turn
    (fresh or continuation); `fresh_prompt` is the fresh prompt, used when
    this thread has no Codex thread yet or its resume fails. Returns the
    bot's result shape (`_shape`); `harness_error` set means Codex did
    not answer and the caller lets Claude take the turn."""
    mode = "live" if live else "ghost"
    persist = persist or live
    timeout = timeout or (ROOM_TIMEOUT_S if lane == "room" else TURN_TIMEOUT_S)
    row = thread_row(thread_key, mode) if persist else {}
    if live:
        root = BOT_HOME
        add_dirs = [REAL_EA_DIR / d for d in LIVE_WRITABLE]
        ea_dir = REAL_EA_DIR
        wrap = live_prompt
    else:
        jid = ghost_scratch_id(thread_key)
        root = Path(row["root"]) if row.get("root") else codex_ghost.scratch_root(jid)
        if not row.get("root"):
            root = codex_ghost.prepare_scratch(jid, runner=scratch_runner)
        add_dirs = [REAL_EA_DIR / "state" / "mail-queue"]
        ea_dir = root
        wrap = lambda p: ghost_prompt(p, root)  # noqa: E731
    prefix = codex_ghost._bot_env({"EA_DIR": str(ea_dir), "EA_RUN_ID": run_id, "EA_BOT_HOME": str(BOT_HOME)})
    runner = codex_run or codex_cli.run
    last_dir = root if not live else BOT_HOME / ".local" / "state" / "ea-lanes"

    def go(text: str, resume: str) -> dict:
        extra = {"on_event": on_event} if on_event is not None else {}
        return runner(wrap(text), cwd=root, sandbox="workspace-write", add_dirs=add_dirs, model=model,
                      timeout=timeout, prefix=prefix, last_message_dir=last_dir, ephemeral=False,
                      resume=resume, **extra)

    resumed = bool(row.get("thread_id"))
    res = go(prompt if resumed else (fresh_prompt or prompt), row.get("thread_id") or "")
    if resumed and res.get("harness_error") and not res.get("output"):
        # The thread is gone or refused: start over with the fresh prompt.
        forget_thread(thread_key, mode)
        res = go(fresh_prompt or prompt, "")
    tid = res.get("thread_id") or row.get("thread_id") or ""
    if tid and persist and not res.get("harness_error"):
        remember_thread(thread_key, mode, tid, root)
    staged = None
    if not live:
        staged = read_staged(root)
        if not persist:
            if not keep_scratch:
                codex_ghost.remove_scratch(jid, runner=clear_runner)
        elif staged:
            clear_staged(root, runner=clear_runner)
    return _shape(res, tid, mode, staged)


def ghost_scratch_id(thread_key: str) -> str:
    """The scratch job id for a ghost thread key (its root is
    codex_ghost.scratch_root of this id)."""
    return "chat-" + codex_ghost.hashlib.sha256(thread_key.encode()).hexdigest()[:12]
