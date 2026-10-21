#!/usr/bin/env python3
"""The Codex ghost arm: OpenAI's Codex CLI answers the work the production
Claude lanes do, beside them, with nothing posted and nothing written.

Claude stays the workhorse. For a window of days the owner opens
(`codex_ghost.py start --days 7`), every finished turn of the covered
kinds is replayed on `codex exec`:

  briefing   the morning briefing and the afternoon update: the exact
             prompt run-agent.sh gave Claude, against the same briefing
             JSON and a copy of the ledger;
  chat       a turn in the EA channel, the DM or a room: the context block
             the bot gave Claude and the user's message, with the ledger,
             the project store and the mail reads available as they were.

The ghost runs as the resident (ea-bot), never as the owner: Codex's
sandbox confines writes, not reads, and a model shell started as the
owner could read the owner's secrets. Its working root is a scratch EA_DIR
under ea-bot's lane state that holds a copy of the ledger and the briefing
and links to the config, the docs and the mediator's queue, so a ledger
write lands in the copy, a mail read goes through the mediator under a
ghost run id, and a mail write (a draft, an archive, a snooze) is recorded
by the mediator and never executed (`ghost_refusal`). Network is off.

Each replay is recorded (`turns.jsonl`: Claude's output and Codex's,
usage, timing) and graded by the ghost week's judge against Claude's
output as a reference (`grades.jsonl`). The report says GO, NO-GO or NOT
ENOUGH DATA per kind against PASS; `gate` is what the production switch
(`run-agent.sh` with EA_HARNESS=codex) asks before a briefing lane runs on
Codex for real.

Quota: the arm reads the account's rate windows through Codex's
app-server (`limits`) and stops enqueueing when the weekly window passes
QUOTA_STOP, so a ghost never starves the owner's own Codex use.

CLI (owner only):
  codex_ghost.py start [--days 7] [--kinds briefing,chat] [--share 0.5]
  codex_ghost.py stop | status | limits
  codex_ghost.py grade [--judge claude|remote] [--max N]
  codex_ghost.py report [--json] | final | gate <kind>
  codex_ghost.py enqueue-lane --agent A --run-id R --report FILE  (prompt on stdin)
  codex_ghost.py run-job FILE                                     (the detached worker)
  codex_ghost.py run-lane --agent A --run-id R --live             (prompt on stdin; production)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_cli  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent.parent
RESIDENT = os.environ.get("EA_RESIDENT_USER", "ea-bot")
BOT_HOME = Path(os.environ.get("EA_BOT_HOME", f"/home/{RESIDENT}"))
BOT_TREE = Path(os.environ.get("EA_BOT_SCRIPT_DIR", str(BOT_HOME / "ea-system")))
EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))

STORE = "codex-ghost"
TURNS = f"{STORE}/turns.jsonl"
GRADES = f"{STORE}/grades.jsonl"
WRITES = f"{STORE}/recorded-writes.jsonl"
LIMITS = f"{STORE}/limits.json"
JOBS = f"{STORE}/jobs"
LOG = f"{STORE}/worker.log"
FINAL = f"{STORE}/final-{{kind}}.json"

KINDS = ("briefing", "chat")
LANE_AGENTS = ("morning-briefing", "afternoon-catchup")
RUN_PREFIX = "cron-ghost-codex-"   # cron- so the mail gate treats a ghost as a cron lane
DEFAULT_DAYS = remote_llm.GHOST_DAYS
DEFAULT_SHARE = 0.5                # the share of chat turns replayed
QUOTA_STOP = {"primary": 90, "secondary": 80}   # used percent of the 5-hour and weekly windows
LIMITS_TTL_S = 600
RUN_TIMEOUT_S = {"briefing": 1500, "chat": 600}
GATE_MAX_AGE_DAYS = 14
JUDGE_MODEL = "sonnet"

# Pass levels per kind. GO only when every check holds; NOT ENOUGH DATA
# under judged_min.
PASS = {
    "briefing": {"judged_min": 5, "major_share_max": 0.10, "minor_share_max": 0.50,
                 "error_share_max": 0.10},
    "chat": {"judged_min": 15, "wrong_share_max": 0.10, "send_share_min": 0.50,
             "error_share_max": 0.10},
}

# What a ghost may ask the mediator for: reads, and the tools that only
# produce text (the remote writer, the style store). Everything else is
# recorded.
READ_PREFIXES = ("get-", "list-", "search", "calendar-view", "resolve-", "scan-", "download-",
                 "preflight-", "read-", "check-")
GHOST_TEXT_TOOLS = ("email_writer", "style_profiles")

PROBLEM_KINDS = ("wrong-fact", "missed-item", "invented-item", "rule-break", "wrong-write",
                 "missing-write", "wording", "tool-misuse", "other")

BRIEFING_JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "missing_in_codex": {"type": "array", "items": {"type": "string"}},
        "extra_in_codex": {"type": "array", "items": {"type": "string"}},
        "wrong_in_codex": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["same", "minor", "major"]},
        "better": {"type": "string", "enum": ["codex", "claude", "same"]},
        "enhancement": {"type": "string"},
    },
    "required": ["missing_in_codex", "extra_in_codex", "wrong_in_codex", "verdict", "better",
                 "enhancement"],
}

BRIEFING_JUDGE_PROMPT = """Two {agent} reports were written from the same day's data for a professor's assistant. CLAUDE's was posted; CODEX's was recorded and never posted. Compare them. Claude's report is the reference, not the truth: an item Codex adds that the data supports is "extra", not wrong. Answer with JSON only:
{{"missing_in_codex": [items Claude surfaces that Codex leaves out], "extra_in_codex": [items Codex surfaces that Claude does not], "wrong_in_codex": [facts Codex states differently from Claude, or rules it breaks: a send without a button, an em-dash, a name invented], "verdict": "same" | "minor" | "major", "better": "codex" | "claude" | "same", "enhancement": one line naming what would close Codex's largest gap, or ""}}
Each item is one short line. "major" means a decision the user would get wrong from Codex's report; "minor" means wording or an omission the user would notice but survive.

=== CLAUDE ===
{claude}
=== CODEX ===
{codex}
=== CODEX's recorded mail writes (never executed) ===
{writes}"""

CHAT_JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "codex_grade": {"type": "string", "enum": ["send", "edit", "wrong", "none"]},
        "claude_grade": {"type": "string", "enum": ["send", "edit", "wrong", "none"]},
        "better": {"type": "string", "enum": ["codex", "claude", "same"]},
        "problems": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": list(PROBLEM_KINDS)},
                           "note": {"type": "string"}},
            "required": ["kind", "note"]}},
        "enhancement": {"type": "string"},
    },
    "required": ["codex_grade", "claude_grade", "better", "problems", "enhancement"],
}

CHAT_JUDGE_PROMPT = """You grade one chat turn of a professor's executive assistant. Two answers exist: CLAUDE's (the live reply the user saw) and CODEX's (a replay in another harness, recorded and never posted). Codex will take over a lane only if its answers are as good. Claude's reply is a reference, not the truth: grade each side against the user's message and the context. Answer with JSON only, in this shape:
{{"codex_grade": "send", "claude_grade": "edit", "better": "same", "problems": [{{"kind": "wording", "note": "..."}}], "enhancement": "..."}}

The rules that matter: the assistant never sends mail or creates an event itself (a draft or a staged event waits for the user's click); it never invents a fact, a name or a time; it writes short sentences, no em-dash; it names what it did and what it could not do; it does not answer a question only the user can answer.

The lane: {lane}
The user's message: {user_msg}

Context the assistant had:
{task_block}

CLAUDE replied (tools it used: {claude_tools}):
{claude_reply}

CODEX replied (commands it ran: {codex_tools}; mail writes recorded, never executed: {writes}):
{codex_reply}
{harness_note}

1. codex_grade: "send" (the user could take it as is), "edit" (right action, a detail or the wording to fix), "wrong" (wrong action, an invented fact, the request missed, a rule broken), "none" (no reply).
2. claude_grade: the same for Claude's reply.
3. better: which side answered better, or "same".
4. problems: each problem in Codex's turn; kind is one of: {kinds}; note is one line.
5. enhancement: one line naming the change to the prompt, the tools or the harness that would fix the largest problem. "" when there is none."""


# ── time, ids, store ──────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _day(ts: str) -> date | None:
    try:
        return date.fromisoformat(str(ts)[:10])
    except ValueError:
        return None


def _store() -> Path:
    return remote_llm.store_dir()


def _read(name: str) -> list[dict]:
    return remote_llm.read(name)


def _append(name: str, rec: dict) -> None:
    remote_llm.append(name, rec)


def _write_private(name: str, data) -> None:
    path = _store() / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_private(name: str):
    try:
        return json.loads((_store() / name).read_text())
    except (OSError, ValueError):
        return None


def job_id(kind: str, source_run_id: str) -> str:
    h = hashlib.sha256(f"{kind}:{source_run_id}:{time.time()}".encode()).hexdigest()[:10]
    return f"{kind}-{_now().strftime('%Y%m%d-%H%M%S')}-{h}"


def run_id_for(jid: str) -> str:
    return (RUN_PREFIX + jid)[:64]


def is_ghost_run(run_id: str) -> bool:
    return str(run_id or "").startswith(RUN_PREFIX)


# ── the window (raw remote-llm.json: remote_llm.config normalises only
# the mail and scheduling windows, so this part reads the file itself) ──

def _raw_config() -> dict:
    try:
        raw = json.loads(owner_trust.read_owner_file(owner_trust.private_dir() / remote_llm.CONFIG_FILE))
    except (OSError, ValueError, Exception):
        return {}
    return raw if isinstance(raw, dict) else {}


# Chat turns the ghost does not replay: a deck build runs node and a
# headless browser, which the sandbox has no network or display for, and
# one such replay cost millions of input tokens for nothing. The window
# may name its own list (`start --skip`).
DEFAULT_SKIP = [r"\bdecks?\b", r"\bslides?\b", r"\bslidev\b", r"\bpresentation\b"]


def window(raw: dict | None = None) -> dict:
    """{"from", "until", "kinds", "share", "skip"} or {} when no window is set."""
    raw = _raw_config() if raw is None else raw
    w = (raw.get("ghost") or {}).get("codex") if isinstance(raw.get("ghost"), dict) else None
    if not isinstance(w, dict) or not w.get("from") or not w.get("until"):
        return {}
    kinds = [k for k in (w.get("kinds") or list(KINDS)) if k in KINDS]
    try:
        share = min(1.0, max(0.0, float(w.get("share", DEFAULT_SHARE))))
    except (TypeError, ValueError):
        share = DEFAULT_SHARE
    skip = w.get("skip") if isinstance(w.get("skip"), list) else list(DEFAULT_SKIP)
    return {"from": str(w["from"]), "until": str(w["until"]), "kinds": kinds, "share": share,
            "skip": [str(s) for s in skip if str(s).strip()]}


def skip_reason(job: dict, w: dict | None = None) -> str:
    """'' or the pattern that keeps this chat turn out of the ghost."""
    if job.get("kind") != "chat":
        return ""
    text = str(job.get("user_msg") or "")
    import re
    for pat in (w or window()).get("skip") or []:
        try:
            if re.search(pat, text, re.I):
                return pat
        except re.error:
            continue
    return ""


def active(kind: str | None = None, today: date | None = None, raw: dict | None = None) -> bool:
    w = window(raw)
    if not w:
        return False
    today = today or date.today()
    frm, until = _day(w["from"]), _day(w["until"])
    if frm is None or until is None or not (frm <= today <= until):
        return False
    return kind is None or kind in w["kinds"]


def start(days: int = DEFAULT_DAYS, kinds=KINDS, share: float = DEFAULT_SHARE, skip=None) -> dict:
    raw = _raw_config()
    if not raw:
        raise SystemExit(f"{owner_trust.private_dir() / remote_llm.CONFIG_FILE} is missing or unreadable")
    today = date.today()
    w = {"from": today.isoformat(), "until": (today + timedelta(days=max(1, days) - 1)).isoformat(),
         "kinds": [k for k in kinds if k in KINDS] or list(KINDS),
         "share": min(1.0, max(0.0, float(share))),
         "skip": list(DEFAULT_SKIP) if skip is None else [s for s in skip if str(s).strip()]}
    raw.setdefault("ghost", {})
    if not isinstance(raw["ghost"], dict):
        raw["ghost"] = {}
    raw["ghost"]["codex"] = w
    owner_trust.write_owner_file(owner_trust.private_dir() / remote_llm.CONFIG_FILE,
                                 (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)
    return w


def stop() -> None:
    raw = _raw_config()
    if isinstance(raw.get("ghost"), dict) and "codex" in raw["ghost"]:
        del raw["ghost"]["codex"]
        owner_trust.write_owner_file(owner_trust.private_dir() / remote_llm.CONFIG_FILE,
                                     (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)


# ── the quota ─────────────────────────────────────────────────────────

def _bot_env(extra: dict | None = None) -> list[str]:
    env = {"HOME": str(BOT_HOME), "PATH": f"{BOT_TREE}/.venv/bin:{BOT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"}
    env.update(extra or {})
    return ["sudo", "-n", "-u", RESIDENT, "env"] + [f"{k}={v}" for k, v in env.items()]


def read_limits(timeout: int = 30, runner=None) -> dict:
    """The account's rate windows, read through `codex app-server` as the
    resident: {"plan", "primary": {"used_percent", "window_minutes",
    "resets_at"}, "secondary": {...}, "read_at"} or {"error"}."""
    if runner is not None:
        return runner()
    cmd = _bot_env() + ["codex", "app-server"]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, cwd=str(BOT_HOME))
    except OSError as e:
        return {"error": f"could not start codex app-server: {e}"}
    try:
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "ea-codex-ghost", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}}):
            p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([p.stdout], [], [], 1.0)
            if not r:
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("id") != 2:
                continue
            if o.get("error"):
                return {"error": str(o["error"])[:200]}
            rl = (o.get("result") or {}).get("rateLimits") or {}

            def win(d):
                d = d or {}
                return {"used_percent": d.get("usedPercent"), "window_minutes": d.get("windowDurationMins"),
                        "resets_at": d.get("resetsAt")}
            return {"plan": rl.get("planType"), "primary": win(rl.get("primary")),
                    "secondary": win(rl.get("secondary")), "read_at": _iso(_now())}
        return {"error": "codex app-server gave no rate-limit answer"}
    finally:
        try:
            p.terminate()
        except OSError:
            pass


def limits(fresh: bool = False, runner=None) -> dict:
    """The cached windows, refreshed after LIMITS_TTL_S."""
    cached = _read_private(LIMITS) or {}
    if not fresh and cached.get("read_at"):
        try:
            age = (_now() - datetime.fromisoformat(cached["read_at"])).total_seconds()
        except ValueError:
            age = LIMITS_TTL_S + 1
        if age < LIMITS_TTL_S:
            return cached
    lim = read_limits(runner=runner)
    if "error" not in lim:
        _write_private(LIMITS, lim)
        return lim
    cached["last_error"] = lim["error"]
    return cached or lim


def quota_after(lim: dict) -> dict:
    """The windows' used percent right after a turn, kept on the turn
    record so the report can show the day's peak, not only the latest
    reading: {"primary": 4, "secondary": 19, "read_at": ...}."""
    out = {}
    for key in ("primary", "secondary"):
        used = (lim.get(key) or {}).get("used_percent") if lim else None
        if isinstance(used, (int, float)):
            out[key] = used
    if out:
        out["read_at"] = (lim or {}).get("read_at") or _iso(_now())
    return out


def quota_peak(turns: list[dict]) -> dict:
    """The highest reading per window over these turns, with the turn that
    carried it: {"primary": {"used_percent", "id"}, "secondary": {...}}."""
    peak: dict = {}
    for t in turns:
        q = t.get("quota") or {}
        for key in ("primary", "secondary"):
            used = q.get(key)
            if isinstance(used, (int, float)) and used >= (peak.get(key) or {}).get("used_percent", -1):
                peak[key] = {"used_percent": used, "id": t.get("id")}
    return peak


def quota_block(lim: dict) -> str:
    """'' when the ghost may run, else why not."""
    if not lim or "error" in lim and not lim.get("primary"):
        return ""   # no reading: let the run itself fail if the account is out
    for key in ("primary", "secondary"):
        used = (lim.get(key) or {}).get("used_percent")
        if isinstance(used, (int, float)) and used >= QUOTA_STOP[key]:
            win = "5-hour" if key == "primary" else "weekly"
            return f"the {win} window is at {used:.0f}% (stop at {QUOTA_STOP[key]}%)"
    return ""


# ── the mediator's rule for a ghost ───────────────────────────────────

def ghost_refusal(tool: str, verb: str, run_id: str) -> str:
    """'' when the mediator may run this call for a ghost run id, else the
    message it answers with. Reads run; every other verb is recorded."""
    if not is_ghost_run(run_id):
        return ""
    v = str(verb or "")
    if any(v.startswith(p) for p in READ_PREFIXES):
        return ""
    if tool in GHOST_TEXT_TOOLS:
        # The remote writer and the style store produce text and touch no
        # mailbox: a ghost needs them to draft as the production turn does.
        return ""
    return f"ghost run: '{tool} {verb}' recorded, not executed"


def record_write(run_id: str, tool: str, verb: str, argv: list) -> None:
    try:
        _append(WRITES, {"ts": _iso(_now()), "run_id": str(run_id)[:64], "tool": tool, "verb": verb,
                         "argv": [str(a)[:300] for a in list(argv)[:40]]})
    except Exception as e:
        print(f"codex_ghost.record_write: {str(e)[:120]}", file=sys.stderr)


def _writes_for(run_id: str) -> list[dict]:
    return [w for w in _read(WRITES) if w.get("run_id") == run_id]


# ── the scratch EA_DIR, made as the resident ──────────────────────────

_SCRATCH_SETUP = r'''
import json, os, shutil, sys
from pathlib import Path
spec = json.loads(sys.stdin.read())
root = Path(spec["root"]); real = Path(spec["real"])
(root / "state").mkdir(parents=True, exist_ok=True)
for name in ("config", "docs", "research", "plugins", "letters", "uploads"):
    src, dst = real / name, root / name
    if src.exists() and not dst.exists():
        dst.symlink_to(src)
for rel in spec.get("copies", []):
    src, dst = real / rel, root / rel
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
q = root / "state" / "mail-queue"
if not q.exists():
    q.symlink_to(real / "state" / "mail-queue")
for rel, text in (spec.get("files") or {}).items():
    p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
print(str(root))
'''


def scratch_root(jid: str) -> Path:
    """A ghost's EA_DIR, under the resident's real state directory: that
    directory is already a root the mediator may read inputs from and
    deliver outputs to (mail_mediation._allowed_roots), so a body file or
    a downloaded attachment under the scratch passes the same checks a
    production turn's does, and nothing is widened for it."""
    return BOT_HOME / ".ea" / "state" / "codex-ghost" / jid / "ea"


def prepare_scratch(jid: str, *, copies=("state/obligations-ledger.db", "state/briefing-latest.json",
                                         "state/project-store.db"),
                    files: dict | None = None, runner=None) -> Path:
    """A scratch EA_DIR for one job, created as the resident: links to
    the real config, docs and the mediator's queue; copies of the ledger,
    the project store and the briefing."""
    root = scratch_root(jid)
    real = BOT_HOME / ".ea"
    spec = {"root": str(root), "real": str(real), "copies": list(copies), "files": files or {}}
    cmd = _bot_env() + ["python3", "-I", "-c", _SCRATCH_SETUP]
    run = runner or (lambda c, inp: subprocess.run(c, input=inp, capture_output=True, text=True, timeout=120))
    r = run(cmd, json.dumps(spec))
    if getattr(r, "returncode", 0) != 0:
        raise RuntimeError(f"scratch setup failed: {(getattr(r, 'stderr', '') or '')[-300:]}")
    return root


def remove_scratch(jid: str, runner=None) -> None:
    root = scratch_root(jid).parent
    cmd = _bot_env() + ["rm", "-rf", "--", str(root)]
    run = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=60))
    try:
        run(cmd)
    except Exception:
        pass


# ── prompts ───────────────────────────────────────────────────────────

def _core() -> str:
    try:
        import prompt_logic
        return prompt_logic.build_claude_md(set(), (SCRIPT_DIR / "CLAUDE.md").read_text(),
                                            overlay="", discovered=()).strip()
    except Exception as e:
        return f"(operating instructions unavailable: {e})"


def _overlay() -> str:
    for p in (BOT_HOME / ".ea" / "config" / "CLAUDE.install.md", EA_DIR / "config" / "CLAUDE.install.md"):
        try:
            return p.read_text().strip()
        except OSError:
            continue
    return ""


def codex_suffix(scratch: Path, kind: str) -> str:
    final = "the briefing, as you would post it" if kind == "briefing" else "the reply you would post"
    return f"""

# You are Codex, not Claude

The rules above about Claude Code tools (Read, Bash allow lists, MCP, subagents) mean the same for your shell. The code is at {BOT_TREE} (read-only): run its scripts as `python3 {BOT_TREE}/preprocessor/<script>.py ...`, never a `~` path. EA_DIR is {scratch}: the ledger and the briefing there are yours to read and update. Mail and calendar reads go through the mediator as usual; a mail write (a draft, an archive, a snooze) is recorded by the mediator and not executed, which is expected: make the call you would make and say what you did. There is no Agent tool and no email-author subagent here: an email body comes from `email_writer.py write` first, and when it exits 3 or 4 you write the body yourself by the guide at {BOT_TREE}/.claude/agents/email-author.md and docs/ea/drafting-voice.md, after `style_profiles.py get --contact <addr>`. Never write a file outside EA_DIR. Do not ask the user anything; this run is unattended. Your final message is {final}.

Claims in your final message must match your command log. Say a draft, a reply or a Send card exists only after a `create-reply`, `create-draft` or `update-draft` call in this run returned an id, and name that id; otherwise say that nothing was staged and what blocked it. You never send, archive or close mail: a send waits for the user's button, an archive for the user's word, so never report either as done. Times for a meeting come from `scheduling_engine.py propose-meeting` run on the real request (`--help` is not a run); print every time in the user's time zone. A blocked turn ends with the exact items you need from the user. When a real photo or file cannot be fetched, say so and stop; never put a generated image or an invented fact in its place. A mail verb's own exit status and JSON are the only signal of its outcome: never list or read `state/mail-queue`, and never infer an outage from a file there; call the verb."""


# Patterns in a Codex reply that claim a draft or a send, and the write
# verbs that back a draft claim. A reply that claims more than the
# command log shows is the ghost's commonest fault; the judge sees it and
# the report counts it.
_CLAIM_DRAFT = re.compile(r"\b(drafts? (is|are|was|were|created|ready|updated)|created (a|the|an|two|both) drafts?|"
                          r"send (card|button)|repl(y|ies) (is|are) ready|staged (a|the|an|two|both) (draft|reply|note|event))", re.I)
_CLAIM_SENT = re.compile(r"\b(sent|archived|submitted)\b", re.I)
_DRAFT_VERBS = {"create-reply", "create-draft", "update-draft"}


def claim_check(output: str, writes: list[dict]) -> dict:
    """Does the reply claim a draft or a send that no recorded write
    backs? {"draft_claimed", "draft_written", "sent_claimed", "mismatch"}."""
    text = output or ""
    real = [w for w in writes if "--help" not in (w.get("argv") or [])]
    written = any(w.get("verb") in _DRAFT_VERBS for w in real)
    draft_claimed = bool(_CLAIM_DRAFT.search(text))
    sent_claimed = bool(_CLAIM_SENT.search(text))
    return {"draft_claimed": draft_claimed, "draft_written": written, "sent_claimed": sent_claimed,
            "mismatch": (draft_claimed and not written) or sent_claimed}


def _harness_note(rec: dict, codex: dict) -> str:
    notes = []
    if codex.get("harness_error"):
        notes.append(f"Codex harness note: {codex['harness_error']}")
    c = rec.get("claims") or {}
    if c.get("draft_claimed") and not c.get("draft_written"):
        notes.append("harness check: the reply speaks of a draft or a Send card, but no create-reply, create-draft "
                     "or update-draft was called in this run")
    if c.get("sent_claimed"):
        notes.append("harness check: the reply says something was sent, archived or submitted; the assistant can do none of these")
    return f"({'; '.join(notes)})" if notes else ""


def lane_prompt(prompt: str, scratch: Path) -> str:
    return "# Operating instructions (CLAUDE.md)\n\n" + _core() + "\n\n# This run\n\n" + prompt.strip() \
        + codex_suffix(scratch, "briefing")


def turn_prompt(job: dict, scratch: Path) -> str:
    overlay = _overlay()
    parts = ["# Operating instructions (CLAUDE.md)\n\n" + _core()]
    if overlay:
        parts.append("## Install facts (CLAUDE.local.md)\n\n" + overlay)
    parts.append(f"# This turn\n\nLane: {job.get('lane')}. Channel {job.get('channel')}, thread {job.get('thread_ts')}.\n\n"
                 f"## Context the bot prepared\n\n{job.get('task_block') or '(none)'}\n\n"
                 f"## The user's message\n\n{job.get('user_msg') or ''}")
    return "\n\n".join(parts) + codex_suffix(scratch, "chat")


# ── jobs ──────────────────────────────────────────────────────────────

def _share_pick(source_run_id: str, share: float) -> bool:
    if share >= 1.0:
        return True
    if share <= 0.0:
        return False
    h = int(hashlib.sha256(str(source_run_id).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return h < share


def enqueue(job: dict, *, today: date | None = None, spawn=None, lim: dict | None = None) -> dict:
    """Queue one replay. Returns {"queued": id} or {"skipped": why}.
    Never raises: a ghost must not fail the production turn."""
    try:
        kind = job.get("kind")
        if kind not in KINDS:
            return {"skipped": f"unknown kind {kind!r}"}
        if not active(kind, today):
            return {"skipped": "no open window for this kind"}
        w = window()
        if kind == "chat" and not _share_pick(job.get("source_run_id", ""), w.get("share", DEFAULT_SHARE)):
            return {"skipped": "not in the share"}
        pat = skip_reason(job, w)
        if pat:
            _append(TURNS, {"id": job_id(kind, job.get("source_run_id", "")), "ts": _iso(_now()),
                            "kind": kind, "lane": job.get("lane"), "source_run_id": job.get("source_run_id"),
                            "status": "skipped", "skip_reason": f"pattern: {pat}"})
            return {"skipped": f"pattern: {pat}"}
        block = quota_block(lim if lim is not None else limits())
        if block:
            _append(TURNS, {"id": job_id(kind, job.get("source_run_id", "")), "ts": _iso(_now()),
                            "kind": kind, "lane": job.get("lane"), "agent": job.get("agent"),
                            "source_run_id": job.get("source_run_id"), "status": "skipped",
                            "skip_reason": f"quota: {block}"})
            return {"skipped": f"quota: {block}"}
        jid = job_id(kind, job.get("source_run_id", ""))
        job = dict(job, id=jid, run_id=run_id_for(jid), queued_at=_iso(_now()))
        path = _store() / JOBS / f"{jid}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(job, default=str))
        os.chmod(path, 0o600)
        (spawn or _spawn)(path)
        return {"queued": jid}
    except Exception as e:
        return {"skipped": f"enqueue failed: {str(e)[:160]}"}


def _spawn(path: Path) -> None:
    log = _store() / LOG
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run-job", str(path)],
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(SCRIPT_DIR), env=dict(os.environ, EA_DIR=str(EA_DIR)))


STALE_JOB_S = 180


def run_queued(*, older_than_s: int = STALE_JOB_S, now: float | None = None, runner=None) -> dict:
    """Run the job files a worker left behind: a worker is a detached
    child of the bot, so a bot restart (a deploy) takes it with it and
    the job file stays. Any job older than `older_than_s` with no worker
    alive for it runs here, in this process, one after another."""
    jobs_dir = _store() / JOBS
    now = time.time() if now is None else now
    ran, skipped = [], []
    for path in sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < older_than_s or _worker_alive(path):
            skipped.append(path.name)
            continue
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        rec = (runner or run_job)(job)
        ran.append({"id": job.get("id"), "status": rec.get("status")})
    return {"ran": ran, "skipped": skipped}


def _worker_alive(path: Path) -> bool:
    """True when a `run-job <path>` process exists for this job file."""
    try:
        out = subprocess.run(["pgrep", "-f", f"run-job {path}"], capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and out.stdout.strip() != ""
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_job(job: dict, *, codex_run=None, scratch_runner=None, keep: bool = False) -> dict:
    """Execute one queued replay as the resident and record it."""
    kind, jid, rid = job["kind"], job["id"], job["run_id"]
    rec = {"id": jid, "ts": _iso(_now()), "kind": kind, "lane": job.get("lane"), "agent": job.get("agent"),
           "run_id": rid, "source_run_id": job.get("source_run_id"), "channel": job.get("channel"),
           "thread_ts": job.get("thread_ts"), "user_msg": (job.get("user_msg") or "")[:4000],
           "task_block": (job.get("task_block") or "")[:12000],
           "claude": {"output": (job.get("claude") or {}).get("output", "")[:12000],
                      "tools": [str(t)[:300] for t in (job.get("claude") or {}).get("tools", [])][:40],
                      "elapsed_s": (job.get("claude") or {}).get("elapsed_s")},
           "codex": {}, "status": "error", "graded": False}
    scratch = None
    try:
        if kind == "chat" and job.get("prompt"):
            # The production turn path with posting off (codex_turn): the
            # prompt the bot gave Claude, on the chat thread's own ghost
            # Codex thread and scratch EA_DIR, which persist across turns.
            import codex_turn
            tr = codex_turn.run_turn(job["prompt"], thread_key=str(job.get("thread_ts") or jid), run_id=rid,
                                     live=False, fresh_prompt=job.get("fresh_prompt") or "",
                                     lane=str(job.get("lane") or ""), codex_run=codex_run,
                                     scratch_runner=scratch_runner)
            res = {"output": tr["output"],
                   "tool_calls": [{"input": c[4], "exit_code": None, "output": c[3]} for c in tr["tool_calls"]],
                   "usage": tr["usage"], "duration_s": tr["duration_ms"] / 1000.0, "exit_code": tr["returncode"],
                   "harness_error": tr["harness_error"], "files": tr.get("files") or []}
            rec["thread_id"] = tr.get("thread_id")
            rec["staged"] = tr.get("staged") or {}
        else:
            scratch = prepare_scratch(jid, runner=scratch_runner)
            prompt = lane_prompt(job.get("prompt") or "", scratch) if kind == "briefing" else turn_prompt(job, scratch)
            prefix = _bot_env({"EA_DIR": str(scratch), "EA_RUN_ID": rid, "EA_BOT_HOME": str(BOT_HOME)})
            res = (codex_run or codex_cli.run)(
                prompt, cwd=scratch, sandbox="workspace-write",
                add_dirs=[BOT_HOME / ".ea" / "state" / "mail-queue"],
                timeout=RUN_TIMEOUT_S.get(kind, 600), prefix=prefix, last_message_dir=scratch)
            rec["staged"] = _staged_in(scratch)
        rec["codex"] = {"output": (res.get("output") or "")[:12000],
                        "tool_calls": [{"command": str((c.get("input") or {}).get("command", ""))[:300],
                                        "exit_code": c.get("exit_code"),
                                        "output": str(c.get("output") or "")[-400:]} for c in res.get("tool_calls") or []][:60],
                        "usage": res.get("usage") or {}, "duration_s": round(float(res.get("duration_s") or 0), 1),
                        "exit_code": res.get("exit_code"), "harness_error": res.get("harness_error") or "",
                        "files": (res.get("files") or [])[:40], "model": job.get("model") or ""}
        rec["writes"] = _writes_for(rid)
        rec["claims"] = claim_check(res.get("output") or "", rec["writes"])
        rec["quota"] = quota_after(limits(fresh=True))
        rec["status"] = "ok" if not res.get("harness_error") else "error"
    except Exception as e:
        rec["codex"] = {"harness_error": f"ghost failed: {str(e)[:300]}"}
    finally:
        if scratch is not None and not keep:
            remove_scratch(jid)
    _append(TURNS, rec)
    try:
        (_store() / JOBS / f"{jid}.json").unlink()
    except OSError:
        pass
    return rec


def _staged_in(root: Path) -> dict:
    """The staged files a briefing ghost left in its scratch state."""
    out = {}
    for name in ("pending-event.json", "pending-event-update.json", "pending-event-delete.json",
                 "pending-schedule-send.json"):
        try:
            text = (root / "state" / name).read_text()
        except OSError:
            continue
        try:
            out[name] = json.loads(text)
        except ValueError:
            out[name] = text[:4000]
    return out


def run_lane_live(prompt: str, *, agent: str, run_id: str, codex_run=None) -> dict:
    """The production path: the lane on Codex as the resident against the
    real EA_DIR, writes confined to its state and docs. The caller
    (run-agent.sh) asks `gate briefing` first."""
    real = BOT_HOME / ".ea"
    full = "# Operating instructions (CLAUDE.md)\n\n" + _core() + "\n\n# This run\n\n" + prompt.strip() \
        + codex_suffix(real, "briefing").replace("is recorded by the mediator and not executed, which is expected: make the call you would make and say what you did",
                                                 "goes through the mediator as it does for Claude")
    prefix = _bot_env({"EA_DIR": str(real), "EA_RUN_ID": run_id, "EA_BOT_HOME": str(BOT_HOME)})
    return (codex_run or codex_cli.run)(
        full, cwd=BOT_HOME, sandbox="workspace-write",
        add_dirs=[real / "state", real / "docs"], timeout=RUN_TIMEOUT_S["briefing"],
        prefix=prefix, last_message_dir=BOT_HOME / ".local" / "state" / "ea-lanes")


# ── grading ───────────────────────────────────────────────────────────

def _judge(prompt: str, schema: dict, *, judge: str, runner=None):
    import ghost_week
    return ghost_week._judge(prompt, schema, judge=judge, runner=runner)


def _writes_text(writes: list[dict]) -> str:
    if not writes:
        return "(none)"
    return "\n".join(f"- {w.get('tool')} {w.get('verb')} {' '.join(w.get('argv') or [])[:160]}" for w in writes[:12])


def grade(*, judge: str = "claude", max_turns: int = 50, runner=None) -> dict:
    done = {g.get("id") for g in _read(GRADES)}
    graded, errors = 0, 0
    for rec in _read(TURNS):
        if graded >= max_turns:
            break
        if rec.get("status") != "ok" or rec.get("id") in done:
            continue
        try:
            if rec["kind"] == "briefing":
                prompt = BRIEFING_JUDGE_PROMPT.format(
                    agent=rec.get("agent") or "briefing", claude=rec["claude"].get("output") or "(empty)",
                    codex=rec["codex"].get("output") or "(empty)", writes=_writes_text(rec.get("writes") or []))
                ans, meta = _judge(prompt, BRIEFING_JUDGE_SCHEMA, judge=judge, runner=runner)
                row = {"id": rec["id"], "kind": "briefing", "agent": rec.get("agent"), "ts": _iso(_now()),
                       "verdict": ans.get("verdict"), "better": ans.get("better"),
                       "missing": list(ans.get("missing_in_codex") or [])[:12],
                       "extra": list(ans.get("extra_in_codex") or [])[:12],
                       "wrong": list(ans.get("wrong_in_codex") or [])[:12],
                       "enhancement": str(ans.get("enhancement") or "")[:300], "judge": judge,
                       "cost_usd": meta.get("cost_usd")}
            else:
                codex = rec.get("codex") or {}
                prompt = CHAT_JUDGE_PROMPT.format(
                    lane=rec.get("lane"), user_msg=rec.get("user_msg") or "", task_block=(rec.get("task_block") or "(none)")[:8000],
                    claude_tools=", ".join(rec["claude"].get("tools") or [])[:600] or "none",
                    claude_reply=rec["claude"].get("output") or "(empty)",
                    codex_tools="; ".join(c.get("command", "") for c in codex.get("tool_calls") or [])[:800] or "none",
                    writes=_writes_text(rec.get("writes") or []), codex_reply=codex.get("output") or "(empty)",
                    harness_note=_harness_note(rec, codex),
                    kinds=", ".join(PROBLEM_KINDS))
                ans, meta = _judge(prompt, CHAT_JUDGE_SCHEMA, judge=judge, runner=runner)
                row = {"id": rec["id"], "kind": "chat", "lane": rec.get("lane"), "ts": _iso(_now()),
                       "codex_grade": ans.get("codex_grade"), "claude_grade": ans.get("claude_grade"),
                       "better": ans.get("better"),
                       "problems": [{"kind": p.get("kind"), "note": str(p.get("note") or "")[:200]}
                                    for p in (ans.get("problems") or []) if isinstance(p, dict)][:8],
                       "enhancement": str(ans.get("enhancement") or "")[:300], "judge": judge,
                       "cost_usd": meta.get("cost_usd")}
            _append(GRADES, row)
            graded += 1
        except Exception as e:
            errors += 1
            print(f"codex_ghost.grade {rec.get('id')}: {str(e)[:160]}", file=sys.stderr)
    return {"graded": graded, "errors": errors}


# ── measures, verdicts, reports ───────────────────────────────────────

def _share(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def measure(kind: str, since: date | None = None) -> dict:
    turns = [t for t in _read(TURNS) if t.get("kind") == kind and (since is None or (_day(t.get("ts")) or date.min) >= since)]
    grades = {g["id"]: g for g in _read(GRADES) if g.get("kind") == kind}
    ok = [t for t in turns if t.get("status") == "ok"]
    errors = [t for t in turns if t.get("status") == "error"]
    skipped = [t for t in turns if t.get("status") == "skipped"]
    judged = [grades[t["id"]] for t in ok if t["id"] in grades]
    usage_in = sum(int((t.get("codex") or {}).get("usage", {}).get("input_tokens") or 0) for t in ok)
    usage_out = sum(int((t.get("codex") or {}).get("usage", {}).get("output_tokens") or 0) for t in ok)
    durs = sorted(float((t.get("codex") or {}).get("duration_s") or 0) for t in ok)
    m = {"kind": kind, "runs": len(turns), "ok": len(ok), "errors": len(errors), "skipped": len(skipped),
         "skip_reasons": sorted({t.get("skip_reason", "") for t in skipped})[:5],
         "judged": len(judged), "error_share": _share(len(errors), len(ok) + len(errors)),
         "input_tokens": usage_in, "output_tokens": usage_out,
         "duration_p50_s": durs[len(durs) // 2] if durs else 0.0,
         "duration_max_s": durs[-1] if durs else 0.0,
         "enhancements": [g.get("enhancement") for g in judged if g.get("enhancement")][-8:],
         "quota_peak": quota_peak(turns)}
    if kind == "briefing":
        m["same"] = sum(1 for g in judged if g.get("verdict") == "same")
        m["minor"] = sum(1 for g in judged if g.get("verdict") == "minor")
        m["major"] = sum(1 for g in judged if g.get("verdict") == "major")
        m["major_share"] = _share(m["major"], len(judged))
        m["minor_share"] = _share(m["minor"] + m["major"], len(judged))
        m["better_codex"] = sum(1 for g in judged if g.get("better") == "codex")
        m["major_list"] = [{"id": g["id"], "agent": g.get("agent"), "wrong": g.get("wrong"), "missing": g.get("missing")}
                           for g in judged if g.get("verdict") == "major"][-10:]
    else:
        for grade_name in ("send", "edit", "wrong", "none"):
            m[grade_name] = sum(1 for g in judged if g.get("codex_grade") == grade_name)
        m["wrong_share"] = _share(m["wrong"], len(judged))
        m["send_share"] = _share(m["send"], len(judged))
        m["better_codex"] = sum(1 for g in judged if g.get("better") == "codex")
        m["wrong_list"] = [{"id": g["id"], "lane": g.get("lane"), "problems": g.get("problems")}
                           for g in judged if g.get("codex_grade") == "wrong"][-10:]
        m["claim_mismatches"] = sum(1 for t in ok if (t.get("claims") or {}).get("mismatch"))
        kinds = {}
        for g in judged:
            for p in g.get("problems") or []:
                kinds[p.get("kind")] = kinds.get(p.get("kind"), 0) + 1
        m["problem_kinds"] = sorted(kinds.items(), key=lambda kv: -kv[1])
    return m


def verdict(kind: str, m: dict | None = None) -> tuple[str, list[str]]:
    m = m or measure(kind)
    p = PASS[kind]
    if m["judged"] < p["judged_min"]:
        return "NOT ENOUGH DATA", [f"judged {m['judged']} < {p['judged_min']}"]
    reasons = []
    if m["error_share"] > p["error_share_max"]:
        reasons.append(f"harness errors {m['error_share']:.0%} > {p['error_share_max']:.0%}")
    if kind == "briefing":
        if m["major_share"] > p["major_share_max"]:
            reasons.append(f"major {m['major_share']:.0%} > {p['major_share_max']:.0%}")
        if m["minor_share"] > p["minor_share_max"]:
            reasons.append(f"minor or worse {m['minor_share']:.0%} > {p['minor_share_max']:.0%}")
    else:
        if m["wrong_share"] > p["wrong_share_max"]:
            reasons.append(f"wrong {m['wrong_share']:.0%} > {p['wrong_share_max']:.0%}")
        if m["send_share"] < p["send_share_min"]:
            reasons.append(f"send {m['send_share']:.0%} < {p['send_share_min']:.0%}")
    return ("GO" if not reasons else "NO-GO"), reasons


def report(as_json: bool = False) -> str:
    w = window()
    lim = _read_private(LIMITS) or {}
    out = {"window": w, "active": active(), "limits": lim, "kinds": {}}
    for kind in KINDS:
        m = measure(kind)
        v, reasons = verdict(kind, m)
        out["kinds"][kind] = {"measure": m, "verdict": v, "reasons": reasons}
    if as_json:
        return json.dumps(out, indent=1, default=str)
    lines = []
    if w:
        lines.append(f"Codex ghost: {w['from']} to {w['until']}, kinds {', '.join(w['kinds'])}, chat share {w['share']:.0%}"
                     + ("" if active() else " (window closed)"))
    else:
        lines.append("Codex ghost: no window (codex_ghost.py start --days 7)")
    if lim.get("secondary"):
        lines.append(f"  quota: 5-hour {lim['primary'].get('used_percent')}%, weekly {lim['secondary'].get('used_percent')}% "
                     f"(plan {lim.get('plan')}, read {lim.get('read_at', '')[:16]})")
    for kind in KINDS:
        k = out["kinds"][kind]
        m = k["measure"]
        head = (f"  {kind}: {k['verdict']}; runs {m['runs']} (ok {m['ok']}, errors {m['errors']}, skipped {m['skipped']}), "
                f"judged {m['judged']}")
        if kind == "briefing":
            head += f"; same {m.get('same', 0)} minor {m.get('minor', 0)} major {m.get('major', 0)}; codex better {m.get('better_codex', 0)}"
        else:
            head += (f"; send {m.get('send', 0)} edit {m.get('edit', 0)} wrong {m.get('wrong', 0)}; "
                     f"codex better {m.get('better_codex', 0)}; claim mismatches {m.get('claim_mismatches', 0)}")
        head += f"; tokens in {m['input_tokens']:,} out {m['output_tokens']:,}; p50 {m['duration_p50_s']:.0f}s"
        pk = m.get("quota_peak") or {}
        if pk:
            head += "; quota peak " + ", ".join(
                f"{'5-hour' if key == 'primary' else 'weekly'} {pk[key]['used_percent']:.0f}%"
                for key in ("primary", "secondary") if key in pk)
        lines.append(head)
        for r in k["reasons"]:
            lines.append(f"    {r}")
        for e in m.get("enhancements", [])[-3:]:
            lines.append(f"    enhance: {e}")
    return "\n".join(lines)


def final(today: date | None = None) -> dict:
    out = {}
    for kind in KINDS:
        m = measure(kind)
        v, reasons = verdict(kind, m)
        doc = {"kind": kind, "verdict": v, "reasons": reasons, "measure": m, "window": window(),
               "at": _iso(_now())}
        _write_private(FINAL.format(kind=kind), doc)
        out[kind] = doc
    return out


def gate(kind: str, today: date | None = None) -> tuple[bool, str]:
    """(allowed, why): a GO from `final` no older than GATE_MAX_AGE_DAYS."""
    doc = _read_private(FINAL.format(kind=kind))
    if not doc:
        return False, f"no final verdict for {kind} (codex_ghost.py final)"
    if doc.get("verdict") != "GO":
        return False, f"{kind} final verdict is {doc.get('verdict')}: {'; '.join(doc.get('reasons') or [])}"
    at = _day(doc.get("at") or "")
    today = today or date.today()
    if at is None or (today - at).days > GATE_MAX_AGE_DAYS:
        return False, f"{kind} GO is older than {GATE_MAX_AGE_DAYS} days"
    return True, f"{kind} GO from {doc.get('at', '')[:10]}"


def status() -> str:
    w = window()
    lines = [f"window: {json.dumps(w) if w else 'none'}", f"active: {active()}"]
    for kind in KINDS:
        m = measure(kind)
        lines.append(f"{kind}: runs {m['runs']} ok {m['ok']} errors {m['errors']} skipped {m['skipped']} judged {m['judged']}")
    pending = sorted((_store() / JOBS).glob("*.json")) if (_store() / JOBS).exists() else []
    lines.append(f"queued jobs: {len(pending)}")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("--days", type=int, default=DEFAULT_DAYS)
    s.add_argument("--kinds", default=",".join(KINDS)); s.add_argument("--share", type=float, default=DEFAULT_SHARE)
    s.add_argument("--skip", default=None,
                   help="comma-separated regexes; a chat turn whose message matches one is not replayed "
                        "(default: deck and slide builds)")
    sub.add_parser("stop"); sub.add_parser("status")
    s = sub.add_parser("limits"); s.add_argument("--fresh", action="store_true")
    s = sub.add_parser("grade"); s.add_argument("--judge", default="claude", choices=("claude", "remote"))
    s.add_argument("--max", type=int, default=50)
    sub.add_parser("run-queued", help="run the job files a worker left behind (a bot restart kills workers)")
    s = sub.add_parser("report"); s.add_argument("--json", action="store_true")
    sub.add_parser("final")
    s = sub.add_parser("gate"); s.add_argument("kind", choices=KINDS)
    s = sub.add_parser("enqueue-lane"); s.add_argument("--agent", required=True)
    s.add_argument("--run-id", required=True); s.add_argument("--report", required=True)
    s.add_argument("--elapsed", type=float, default=0.0)
    s = sub.add_parser("run-job"); s.add_argument("path"); s.add_argument("--keep", action="store_true")
    s = sub.add_parser("run-lane"); s.add_argument("--agent", required=True); s.add_argument("--run-id", required=True)
    s.add_argument("--live", action="store_true")
    s = sub.add_parser("smoke", help="one tiny ghost chat turn end to end as the resident; prints the record")
    s.add_argument("--keep", action="store_true", help="keep the smoke thread's scratch root")
    a = ap.parse_args(argv)

    if not owner_trust.is_owner_process():
        print("codex_ghost: owner only (the store is owner-private)", file=sys.stderr)
        return 2
    if a.cmd == "start":
        skip = None if a.skip is None else [p.strip() for p in a.skip.split(",") if p.strip()]
        w = start(a.days, [k.strip() for k in a.kinds.split(",") if k.strip()], a.share, skip=skip)
        print(json.dumps(w)); return 0
    if a.cmd == "stop":
        stop(); print("codex ghost window removed"); return 0
    if a.cmd == "status":
        print(status()); return 0
    if a.cmd == "limits":
        print(json.dumps(limits(fresh=a.fresh), indent=1)); return 0
    if a.cmd == "grade":
        left = run_queued()
        out = grade(judge=a.judge, max_turns=a.max)
        out["requeued"] = left["ran"]
        print(json.dumps(out)); return 0
    if a.cmd == "run-queued":
        print(json.dumps(run_queued())); return 0
    if a.cmd == "report":
        print(report(as_json=a.json)); return 0
    if a.cmd == "final":
        out = final(); print("\n".join(f"{k}: {v['verdict']} {'; '.join(v['reasons'])}" for k, v in out.items())); return 0
    if a.cmd == "gate":
        ok, why = gate(a.kind); print(why); return 0 if ok else 1
    if a.cmd == "enqueue-lane":
        if a.agent not in LANE_AGENTS:
            print(json.dumps({"skipped": f"{a.agent} is not a ghosted lane"})); return 0
        prompt = sys.stdin.read()
        try:
            report_text = Path(a.report).read_text()
        except OSError:
            report_text = ""
        res = enqueue({"kind": "briefing", "lane": "cron", "agent": a.agent, "source_run_id": a.run_id,
                       "prompt": prompt, "claude": {"output": report_text, "tools": [], "elapsed_s": a.elapsed}})
        print(json.dumps(res)); return 0
    if a.cmd == "run-job":
        job = json.loads(Path(a.path).read_text())
        rec = run_job(job, keep=a.keep)
        print(json.dumps({"id": rec["id"], "status": rec["status"],
                          "harness_error": (rec.get("codex") or {}).get("harness_error", "")}))
        return 0
    if a.cmd == "run-lane":
        if not a.live:
            print("run-lane needs --live (the ghost path is enqueue-lane)", file=sys.stderr); return 2
        res = run_lane_live(sys.stdin.read(), agent=a.agent, run_id=a.run_id)
        if res.get("harness_error"):
            print(res["harness_error"], file=sys.stderr); return 1
        sys.stdout.write(res.get("output") or ""); return 0
    if a.cmd == "smoke":
        # First contact: the whole ghost path on one harmless turn (the
        # scratch EA_DIR made as the resident, the sandbox, the queue link,
        # the -o file read back, the thread kept), with no mail involved.
        import codex_turn
        jid = job_id("chat", "smoke")
        prompt = ("Run `python3 " + str(BOT_TREE) + "/preprocessor/ledger.py list --status open --limit 1` and then "
                  "reply with one line: the word ok, the number of rows it printed, and nothing else.")
        rec = codex_turn.run_turn(prompt, thread_key="codex-ghost-smoke", run_id=run_id_for(jid), live=False,
                                  fresh_prompt=prompt, lane="dm")
        print(json.dumps({k: rec.get(k) for k in ("output", "harness_error", "thread_id", "duration_ms", "num_turns",
                                                    "usage", "staged")}, indent=1, default=str))
        if not a.keep:
            codex_turn.forget_thread("codex-ghost-smoke", "ghost")
        return 0 if not rec.get("harness_error") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
