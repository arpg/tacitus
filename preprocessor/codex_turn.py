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

Codex threads: every turn is a new Codex thread on the bot's fresh
prompt, which carries the chat thread's history (`codex exec resume`
drops the sandbox's writable roots; run_turn says how). The registry,
one row per chat thread and mode, keeps the last thread id and the turn
count; a ghost row also keeps its scratch root, which the thread's next
ghost turn reuses. The ghost arm passes `persist=False`: each replay is
kept nowhere, and the scratch is removed after the turn.

A ghost's thread id and a production thread id are never the same
registry row: the ghost's scratch root is that thread's root.

What Claude Code loads by itself, Codex gets in the prompt: the
instruction files on the Claude run's path and the memory index, for the
directory that run would have used (`context_block`). The memory is
read-only to Codex, as it is to the Claude lanes. A live turn writes
where a Claude turn's file tools and scripts write (`live_write_paths`),
from a working directory of its own (LIVE_CWD).
"""
from __future__ import annotations

import json
import os
import re
import logging
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
STAGED_FILES = ("pending-event.json", "pending-event-update.json", "pending-event-delete.json",
                "pending-schedule-send.json", "pending-channel.json", "pending-publish.json")
# A live turn's working directory: an empty folder of its own inside the
# EA state, which a Claude turn may write too. A Claude turn runs in the
# deployed tree, read-only to the resident; Codex cannot start there (its
# sandbox makes a guard folder in the working directory), and in the
# resident's home it would make every file there writable to the model.
LIVE_CWD = REAL_EA_DIR / "state" / "codex-live"
# What the scripts a Claude turn may run write outside the file tools'
# paths (resident_write_paths): the preference rules and the tier
# promotions in config (by an atomic replace, so the whole folder), the
# logs (missed-closures.log, item-feedback.jsonl, fetch-url.log), the chat
# uploads. Claude's rules confine its Write and Edit tools and leave the
# allowlisted scripts to the resident's own permissions; Codex's sandbox
# confines every write, the scripts' included, so their folders are named.
log = logging.getLogger("codex_turn")

STAGED_FILE = REAL_EA_DIR / "state" / "preferences-staged.jsonl"
SCRIPT_WRITE_DIRS = ("logs", "uploads")
# The config folder is not a root: a folder root would cover ea-config.json,
# github.json and CLAUDE.install.md, which steer every later run. The two
# verbs that write there from a turn (preferences.py, onboarding.py) stage
# the write instead, and the bot applies it after the turn (_apply_staged).
NO_WRITE_DIRS = ("config",)
# Claude Code loads the first 200 lines of a project's MEMORY.md.
MEMORY_INDEX_LINES = 200


def live_write_paths() -> list[Path]:
    """The folders a live turn may write: the paths a Claude turn's Write
    and Edit tools may change (resident_write_paths: globs expanded, a file
    by its folder) and SCRIPT_WRITE_DIRS, existing ones only, in order.
    /tmp needs no entry: every Codex sandbox can write it."""
    import resident_write_paths
    pats = resident_write_paths.paths(str(BOT_HOME), str(REAL_EA_DIR))
    pats += [str(REAL_EA_DIR / d) for d in SCRIPT_WRITE_DIRS]
    closed = [REAL_EA_DIR / d for d in NO_WRITE_DIRS]
    out: list[Path] = []
    for pat in pats:
        pat = pat[:-3] if pat.endswith("/**") else pat
        if pat in ("", "/tmp"):
            continue
        hits = sorted(Path("/").glob(pat.lstrip("/"))) if "*" in pat else [Path(pat)]
        for h in hits:
            d = h if h.is_dir() else (h.parent if h.is_file() else None)
            if d is not None and d not in out and d not in closed:
                out.append(d)
    return out


def _apply_staged(user_text: str, runner=None) -> dict:
    """After a live turn: replay the preference and onboarding writes the
    sandbox refused (preferences.py apply-staged), as the resident, outside
    the sandbox, with the user's message of this turn on stdin so a rule
    is applied only when the words it quotes are the user's. Returns the
    verb's {"applied", "refused"}, or {} when nothing was staged."""
    try:
        if STAGED_FILE.stat().st_size == 0:
            return {}
    except FileNotFoundError:
        return {}
    except OSError:
        pass
    cmd = codex_ghost._bot_env({"EA_DIR": str(REAL_EA_DIR)}) + [
        "python3", str(BOT_TREE / "preprocessor" / "preferences.py"), "apply-staged", "--user-text-file", "-"]
    run = runner or (lambda c, inp: subprocess.run(c, input=inp, capture_output=True, text=True, timeout=60))
    try:
        res = run(cmd, user_text or "")
        out = json.loads((getattr(res, "stdout", "") or "").strip() or "{}")
    except Exception as e:
        log.warning("codex turn: apply-staged failed: %s", e)
        return {}
    for rec in out.get("refused") or []:
        log.warning("codex turn: a staged %s write was refused: %s", rec.get("tool"), rec.get("why"))
    return out


# ── what Claude Code loads by itself ──────────────────────────────────
#
# A Claude run loads, from its working directory and the ones above it,
# CLAUDE.md, CLAUDE.local.md and .claude/CLAUDE.md, plus the user's
# ~/.claude/CLAUDE.md and the project's memory index; the bot's prompt
# leaves out what the run loads (claude_cli.discovered_memory) and says it
# is loaded. Codex loads none of it, so each Codex turn gets exactly that
# set for the directory the Claude run would have used: the deployed tree
# for most turns, a lane directory (which loads nothing) for the
# scheduling and slim reply prompts. The memory is read-only, as it is to
# a Claude lane (resident_write_paths.STEERING_RULES).

_MEMORY_HEAD = """Your memory is the folder {dir}. The Claude sessions of this EA wrote there what they learned on earlier turns, and MEMORY.md, below, is its index, one line per note. When a line bears on the task, read that note with `cat` before you act. The folder is read-only, as it is to the Claude lanes: a standing preference or rule the user states goes to `preferences.py add` (the operating instructions, "Personal rules")."""


def memory_dir(claude_cwd) -> Path:
    """The Claude memory folder for a working directory (the project folder
    slack_sessions.project_dir_name names)."""
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(str(claude_cwd)))
    return BOT_HOME / ".claude" / "projects" / name / "memory"


def context_files(claude_cwd) -> list[Path]:
    """The instruction files a Claude run started in `claude_cwd` loads by
    itself, the most general first: the user's, then the ones on the path
    from the root down (claude_cli.memory_on_path)."""
    import claude_cli
    user = BOT_HOME / ".claude" / "CLAUDE.md"
    found = [user] if user.is_file() else []
    return found + list(reversed(claude_cli.memory_on_path(claude_cwd)))


def _resident_text(path: Path) -> str:
    """A file's text read as the resident, the way a Claude run reads it
    (resident_io: no owner read of a resident path), or ''."""
    try:
        import resident_io
        text = resident_io.read_text(path) or ""
    except Exception as e:
        log.warning("codex context: %s not read as the resident: %s", path, e)
        return ""
    if not text:
        log.warning("codex context: %s is empty or unreadable", path)
    return text


def context_block(claude_cwd=None) -> str:
    """What a Claude run in `claude_cwd` (default: the deployed tree) loads
    by itself, in the fences the bot's own prompts use; '' when that is
    nothing."""
    cwd = claude_cwd or BOT_TREE
    parts = []
    for f in context_files(cwd):
        text = _resident_text(f).strip()
        if text:
            parts.append(f"=== INSTRUCTIONS ({f}) ===\n{text}\n=== END INSTRUCTIONS ===")
    mem = memory_dir(cwd)
    index = "\n".join(_resident_text(mem / "MEMORY.md").splitlines()[:MEMORY_INDEX_LINES]).strip()
    if index:
        parts.append(f"=== YOUR MEMORY ===\n{_MEMORY_HEAD.format(dir=mem)}\n\n{index}\n=== END YOUR MEMORY ===")
    return "\n\n".join(parts)


def home_roots() -> list[Path]:
    """The folders under the resident's home a Claude turn may write whole
    (resident_write_paths: presentations, the doc-review and Overleaf
    clones). A Claude turn makes one on first use; a Codex turn cannot,
    since its sandbox writes only the folders it was given."""
    import resident_write_paths
    return [BOT_HOME / rel for base, rel, recursive in resident_write_paths.WRITE_PATHS
            if base == "home" and recursive]


def _ensure_live_cwd(runner=None) -> None:
    """Make LIVE_CWD and the home roots as the resident, mode 750 like the
    rest of the state (the bot starts Codex from inside LIVE_CWD, through
    the group); never raises."""
    cmd = codex_ghost._bot_env() + ["install", "-d", "-m", "750", str(LIVE_CWD),
                                    *map(str, home_roots())]
    run = runner or (lambda c, inp: subprocess.run(c, input=inp, capture_output=True, text=True, timeout=30))
    try:
        run(cmd, None)
    except Exception:
        pass


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
             keep_scratch: bool = False, claude_cwd=None, user_text: str = "") -> dict:
    """One turn. `fresh_prompt` is the fresh prompt the bot built for this
    turn (it carries the chat thread's history); `prompt`, the prompt it
    would send a resumed Claude session, stands in when there is none.
    `claude_cwd` is the directory the Claude run would have started in
    (default: the deployed tree), which decides the context block.
    `user_text` is the message the user sent this turn: a live turn's
    staged preference writes are applied after it only when the words they
    quote are in it. Returns
    the bot's result shape (`_shape`); `harness_error` set means Codex did
    not answer and the caller lets Claude take the turn.

    Every turn is a new Codex thread. `codex exec resume` drops the
    writable roots (0.166: the resumed turn's sandbox policy has none,
    `-c sandbox_workspace_write.writable_roots` is ignored and resume takes
    no --add-dir), so a resumed turn could write nothing but its working
    directory and /tmp: no ledger, no draft, no mail request."""
    mode = "live" if live else "ghost"
    persist = persist or live
    timeout = timeout or (ROOM_TIMEOUT_S if lane == "room" else TURN_TIMEOUT_S)
    row = thread_row(thread_key, mode) if persist else {}
    if live:
        root = LIVE_CWD
        _ensure_live_cwd(runner=scratch_runner)
        add_dirs = live_write_paths()
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
    text = fresh_prompt or prompt
    block = context_block(claude_cwd)
    if block:
        text = block + "\n\n" + text
    extra = {"on_event": on_event} if on_event is not None else {}
    res = runner(wrap(text), cwd=root, sandbox="workspace-write", add_dirs=add_dirs, model=model,
                 timeout=timeout, prefix=prefix, last_message_dir=last_dir, ephemeral=False, resume="", **extra)
    tid = res.get("thread_id") or ""
    if tid and persist and not res.get("harness_error"):
        remember_thread(thread_key, mode, tid, root)
    prefs = _apply_staged(user_text, runner=scratch_runner) if live else {}
    staged = None
    if not live:
        staged = read_staged(root)
        if not persist:
            if not keep_scratch:
                codex_ghost.remove_scratch(jid, runner=clear_runner)
        elif staged:
            clear_staged(root, runner=clear_runner)
    shaped = _shape(res, tid, mode, staged)
    if prefs:
        shaped["preferences"] = prefs
    return shaped


def ghost_scratch_id(thread_key: str) -> str:
    """The scratch job id for a ghost thread key (its root is
    codex_ghost.scratch_root of this id)."""
    return "chat-" + codex_ghost.hashlib.sha256(thread_key.encode()).hexdigest()[:12]
