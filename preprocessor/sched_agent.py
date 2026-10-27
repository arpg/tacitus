#!/usr/bin/env python3
"""Scheduling agent: the remote-model twin of the scheduling lane, run in
shadow beside Claude (docs/remote-llm.md).

When remote_llm's `scheduling` switch reads `shadow`, the bot hands every
scheduling turn it has just answered with Claude to `enqueue_shadow()`.
This module replays the same turn on the remote model: the same rules
(skills/sched-agent.md, the scheduling rules, preferences and staged-event
guide), the same task text, the same solver. It posts nothing and changes
nothing. Reads run for real; writes are recorded and answered with a
synthetic success, so the conversation goes on as it would have.

When the switch reads `enforce`, the bot calls `run_live()` for a turn in
the scheduling channel or from the proactive scheduler BEFORE it would
start Claude, and posts the agent's reply as the turn's answer. Writes
then run for real, as the resident and under the turn's own run id: a
draft lands in the draft journal and gets its Send card, a staged event
lands in the resident's state directory and gets its Create button, an
EA-Managed hold goes on that calendar. Filling a poll stays recorded only
(ask-first). A live turn that ends without a reply, or fails before it
wrote anything, falls back to the Claude lane in the bot; one that wrote
something and then stopped gets a short code-written reply instead, so
nothing is done twice. A thread's earlier live turns are given back to
the model as history, since the agent keeps no session.

Privileges. The model call runs here, in the owner process that holds the
key. Every tool runs as the resident (`sudo -n -u ea-bot`), exactly like
Claude's tools: mail and calendar verbs reach the mailbox only through the
resident's shim and the owner-side mediator, so the agent has the scheduling
lane's privileges and none of the owner's. Write verbs are never run at
all. Reads carry EA_RUN_ID=shadow-<run_id>, so the mediator log tells
them apart from Claude's.

Keep from Anthropic. The remote model sees every turn. A turn whose task
text or tool output names an address on the keep_from_anthropic list is
labelled with the reason: it is a turn an enforce mode would leave to the
remote model alone, and the report counts them.

The step protocol. The server has no native tool calling, so each model
answer is one JSON object constrained by a strict schema: `action` (a tool
name or `reply`), `args_json` (a JSON object as a string, checked here per
tool) and `note`. A bad step comes back as an error observation and costs
a step. Twelve steps and ten minutes per turn.

Store (remote_llm.store_dir(), owner-private):
  sched-jobs/<run_id>.json        queued jobs, removed once run
  sched-turns.jsonl               one compact record per turn, with checks;
                                  a live turn carries live: true
  sched-transcripts/<run_id>.json the full conversation, observations included
  claude-staged.jsonl             what Claude staged (note_claude_staged)
  event-outcomes.jsonl            what the user's buttons did (note_event_outcome)
  grading/                        side-by-side sheets for the user to grade

Checks. Each turn is scored the same way for both sides right after it
runs: every concrete time each side named is checked with a fresh
`validate-draft`; the shadow's times must also come from a solver output
of that turn (an invented time is a hard failure); rule breaks (dashes,
`**`, Teams, weekday/date mismatch, declining, tool talk, reply-all on a
poll); the mode each side took; and the overlap of their proposed times.

Usage:
    sched_agent.py run-job <path>
    sched_agent.py run-text --text "..." [--force]
    sched_agent.py report [--json] [--since YYYY-MM-DD]
    sched_agent.py grading-sheet [--n 10]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import remote_llm as _rl  # noqa: E402
import user_tz as _user_tz_mod  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SKILL_FILE = REPO / "skills" / "sched-agent.md"
RULES_FILE = REPO / "skills" / "scheduling-rules.md"
STAGED_GUIDE = REPO / "docs" / "ea" / "scheduling.md"
VOICE_GUIDE = REPO / "docs" / "ea" / "drafting-voice.md"

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
EA_BOT_USER = "ea-bot"
EA_BOT_HOME = Path(os.environ.get("EA_BOT_HOME", f"/home/{EA_BOT_USER}"))
TOOL_DIR = EA_BOT_HOME / "ea-system" / "preprocessor"
# The user's zone (user_tz.py: the profile, the scheduling preferences,
# then the system's zone); a profile's own `timezone` wins in _user_tz.
DEFAULT_TZ = _user_tz_mod.name(EA_DIR)

STEP_BUDGET = 12
TURN_TIMEOUT_S = 600
TOOL_TIMEOUT_S = 200
OBS_CHARS = 12_000
CONV_CHARS = 300_000
BACKLOG_S = 1800
COLD_MS = 60_000
CHECK_TIMES_CAP = 6
STEP_MAX_TOKENS = 3000

JOBS_DIR = "sched-jobs"
TRANSCRIPTS_DIR = "sched-transcripts"
TURNS = "sched-turns.jsonl"
CLAUDE_STAGED = "claude-staged.jsonl"
OUTCOMES = "event-outcomes.jsonl"
GRADING_DIR = "grading"

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"{_iso()} sched_agent: {msg}", file=sys.stderr, flush=True)


def _shadow_on() -> bool:
    try:
        return _rl.mode("scheduling") == "shadow"
    except Exception:
        return False


def live_on() -> bool:
    """True when the agent answers the scheduling lanes itself."""
    try:
        return _rl.mode("scheduling") == "enforce"
    except Exception:
        return False


LIVE_LANES = ("sched-channel", "proactive")
STAGED_FILES = {"stage-event": "pending-event.json",
                "stage-event-update": "pending-event-update.json",
                "stage-event-delete": "pending-event-delete.json"}
HISTORY_TURNS = 6


def _private_write(path: Path, data: str) -> None:
    """Create or replace one private file (0600), never through a link."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)


# ── the interface the bot calls ───────────────────────────────────────

def _strip_private(d: dict) -> dict:
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def enqueue_shadow(job: dict) -> None:
    """Queue one turn for the shadow agent and start a detached runner.
    A no-op unless the scheduling switch reads `shadow`; never raises."""
    try:
        if not _shadow_on() or not isinstance(job, dict):
            return
        run_id = _SAFE_ID.sub("_", str(job.get("run_id") or ""))[:64] or \
            f"job-{int(time.time() * 1000)}"
        job = dict(job, run_id=run_id, queued_at=_iso())
        path = _rl.store_dir() / JOBS_DIR / f"{run_id}.json"
        _private_write(path, json.dumps(job, default=str))
        import log_paths
        logf = open(log_paths.owner_log("sched-agent.log"), "a")
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "run-job", str(path)],
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, cwd=str(REPO))
        finally:
            logf.close()
    except Exception as e:
        try:
            _log(f"enqueue_shadow failed: {e}")
        except Exception:
            pass


def note_claude_staged(channel: str, thread_ts: str | None, action: dict) -> None:
    """Record one action Claude staged (the bot calls this as it posts the
    buttons). A no-op unless the scheduling switch reads `shadow`."""
    try:
        if not _shadow_on():
            return
        _rl.append(CLAUDE_STAGED, {"ts": _iso(), "channel": channel,
                                   "thread_ts": thread_ts or "",
                                   "action": _strip_private(action)})
    except Exception:
        pass


def note_event_outcome(kind: str, thread_ts: str, event: dict, ok: bool) -> None:
    """Record what one of the user's Create / Reschedule / Delete buttons
    did. A no-op unless the scheduling switch reads `shadow`."""
    try:
        if not _shadow_on():
            return
        _rl.append(OUTCOMES, {"ts": _iso(), "kind": kind, "thread_ts": thread_ts or "",
                              "event": _strip_private(event), "ok": bool(ok)})
    except Exception:
        pass


# ── tools ─────────────────────────────────────────────────────────────
# Each argument is (cli flag, kind, required). Kinds: int, str, json (a
# list or object passed as JSON text), csv (a list or a comma string),
# multi (a list, one flag per item), flag (a boolean switch).

READ_TOOLS = {
    "propose-meeting": ("scheduling_engine", "propose-meeting", {
        "duration": ("--duration", "int", False),
        "days_ahead": ("--days-ahead", "int", False),
        "earliest_date": ("--earliest-date", "str", False),
        "latest_date": ("--latest-date", "str", False),
        "num_options": ("--num-options", "int", False),
        "attendees": ("--attendees", "json", True),
        "constraints": ("--constraints", "json", False),
        "ignore_event": ("--ignore-event", "multi", False)}),
    "validate-draft": ("scheduling_engine", "validate-draft", {
        "times": ("--times", "json", True),
        "duration": ("--duration", "int", False),
        "ignore_event": ("--ignore-event", "multi", False)}),
    "check-attendees": ("scheduling_engine", "check-attendees", {
        "attendees": ("--attendees", "csv", True),
        "start": ("--start", "str", True),
        "end": ("--end", "str", True),
        "ignore_event": ("--ignore-event", "multi", False)}),
    "reschedule-brief": ("scheduling_engine", "reschedule-brief", {
        "conversation_id": ("--conversation-id", "str", False),
        "message_id": ("--message-id", "str", False),
        "sender": ("--sender", "str", False),
        "times": ("--times", "json", False),
        "duration": ("--duration", "int", False),
        "days_ahead": ("--days-ahead", "int", False)}),
    "review-patterns": ("scheduling_engine", "review-patterns", {}),
    "get-conversation": ("graph_actions", "get-conversation", {
        "conversation_id": ("--conversation-id", "str", True),
        "top": ("--top", "int", False),
        "max_chars": ("--max-chars", "int", False)}),
    "get-message": ("graph_actions", "get-message", {
        "message_id": ("--message-id", "str", True)}),
    "search-mail": ("graph_actions", "search-mail", {
        "query": ("--query", "str", True),
        "top": ("--top", "int", False),
        "folder": ("--folder", "str", False)}),
    "calendar-view": ("graph_actions", "calendar-view", {
        "start": ("--start", "str", False),
        "end": ("--end", "str", False),
        "days": ("--days", "int", False)}),
    "get-event": ("graph_actions", "get-event", {
        "event_id": ("--event-id", "str", True)}),
    "resolve-contact": ("graph_actions", "resolve-contact", {
        "name": ("--name", "str", True),
        "top": ("--top", "int", False)}),
    "booking-read": ("booking_link", "read", {
        "url": ("--url", "str", True),
        "duration": ("--duration", "int", False),
        "days_ahead": ("--days-ahead", "int", False)}),
    "booking-propose": ("booking_link", "propose", {
        "attendees": ("--attendees", "json", True),
        "duration": ("--duration", "int", False),
        "days_ahead": ("--days-ahead", "int", False),
        "num_options": ("--num-options", "int", False),
        "constraints": ("--constraints", "json", False),
        "ignore_event": ("--ignore-event", "multi", False)}),
    "read-poll": ("poll_filler", "read-poll", {
        "url": ("--url", "str", True)}),
    "list-ea-managed": ("google_calendar_client", "list-ea-managed", {
        "days": ("--days", "int", False)}),
    "ledger-list": ("ledger", "list", {
        "status": ("--status", "str", False),
        "type": ("--type", "str", False),
        "from_email": ("--from-email", "str", False),
        "has_deadline": ("--has-deadline", "flag", False),
        "limit": ("--limit", "int", False)}),
    "ledger-search": ("ledger", "search", {
        "query": ("--query", "str", False),
        "conversation_id": ("--conversation-id", "str", False),
        "status": ("--status", "str", False),
        "limit": ("--limit", "int", False)}),
    "style-profile": ("style_profiles", "get", {
        "contact": ("--contact", "str", True)}),
}

# Recorded, never run. Each names its required keys.
WRITE_TOOLS = {
    "create-reply": ("body",),
    "create-draft": ("to", "subject", "body"),
    "update-draft": ("draft_id", "body"),
    "stage-event": ("event",),
    "stage-event-update": ("event",),
    "stage-event-delete": ("event",),
    "ea-managed-hold": ("op",),
    "clear-hold": ("event_id",),
    "log-decision": ("event", "action"),
    "fill-poll": ("url",),
}

LOCAL_TOOLS = ("check-date",)
ACTIONS = tuple(READ_TOOLS) + tuple(WRITE_TOOLS) + LOCAL_TOOLS + ("reply",)

TOOL_HELP = {
    "propose-meeting": "solver: ranked free slots for a new meeting",
    "validate-draft": "solver: is each exact time free and within the rules (times: list of ISO)",
    "check-attendees": "solver: can these people make this fixed window (attendees: list of emails)",
    "reschedule-brief": "solver: everything for a 'can we move it' email, one call",
    "review-patterns": "solver: patterns in logged decisions",
    "get-conversation": "mail: the whole thread, oldest first",
    "get-message": "mail: one message with its body",
    "search-mail": "mail: search by text",
    "calendar-view": "calendar: Outlook events with ids in a range",
    "get-event": "calendar: one Outlook event",
    "resolve-contact": "mail: a person's address by name",
    "booking-read": "a cal.com or Calendly link's open windows (read only)",
    "booking-propose": "solver with cal.com/Calendly links as availability (attendees may carry booking_link)",
    "read-poll": "a When2Meet or Doodle poll's slots (read only)",
    "list-ea-managed": "the EA-Managed calendar's events with ids",
    "ledger-list": "obligations ledger rows (read only)",
    "ledger-search": "obligations ledger search (read only)",
    "style-profile": "how the user writes to one contact",
    "check-date": "the weekday of a date: {date: YYYY-MM-DD, weekday?: name to compare}",
    "create-reply": "draft a reply: {message_id, conversation_id, body, sender_only?}",
    "create-draft": "draft a new email: {to, subject, body, cc?}",
    "update-draft": "revise your draft: {draft_id, body}",
    "stage-event": "stage a new event for the Create button: {event: object or list}",
    "stage-event-update": "stage a move for the Reschedule button: {event: {event_id, start, end, ...}}",
    "stage-event-delete": "stage a delete for the Delete button: {event: {event_id, subject, start, end}}",
    "ea-managed-hold": "EA-Managed hold: {op: create|update|delete, event_id?, summary?, start?, end?}",
    "clear-hold": "release your own Outlook hold: {event_id}",
    "log-decision": "record what the user chose: {event, action, for?, contact?, contact_type?, comment?}",
    "fill-poll": "fill a poll: {url, name, available}",
    "reply": "END the turn: {text: the message you post in the thread}",
}


class StepError(ValueError):
    """A step the agent must redo: unknown tool or bad arguments."""


def _argv_for(tool: str, args: dict) -> list[str]:
    script, verb, spec = READ_TOOLS[tool]
    unknown = set(args) - set(spec)
    if unknown:
        raise StepError(f"{tool} takes no {', '.join(sorted(unknown))}; "
                        f"it takes {', '.join(spec) or 'nothing'}")
    argv = [verb]
    for key, (flag, kind, required) in spec.items():
        if key not in args or args[key] in (None, ""):
            if required:
                raise StepError(f"{tool} needs {key}")
            continue
        val = args[key]
        if kind == "int":
            try:
                argv += [flag, str(int(val))]
            except (TypeError, ValueError):
                raise StepError(f"{key} must be a whole number") from None
        elif kind == "str":
            if not isinstance(val, (str, int, float)):
                raise StepError(f"{key} must be text")
            argv += [flag, str(val)]
        elif kind == "json":
            if isinstance(val, str):
                try:
                    json.loads(val)
                except ValueError:
                    raise StepError(f"{key} must be JSON") from None
                argv += [flag, val]
            elif isinstance(val, (list, dict)):
                argv += [flag, json.dumps(val)]
            else:
                raise StepError(f"{key} must be a list or object")
        elif kind == "csv":
            if isinstance(val, list):
                val = ",".join(str(v).strip() for v in val if str(v).strip())
            if not isinstance(val, str) or not val:
                raise StepError(f"{key} must be a list of addresses")
            argv += [flag, val]
        elif kind == "multi":
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if not isinstance(v, str) or not v:
                    raise StepError(f"{key} must be a list of ids")
                argv += [flag, v]
        elif kind == "flag":
            if val is True:
                argv.append(flag)
    return argv


def _resident_cmd(script: str, argv: list[str], run_id: str, *, live: bool = False) -> list[str]:
    """The tool as the resident runs it, with the env of the bot's Claude
    runs. The Graph client secret is never passed: under mediation the
    owner-side mediator holds it. A live turn carries the dispatch's own run id,
    so the draft journal and the event claims name this turn; a shadow
    turn's reads carry a shadow- prefix the mediator log tells apart.

    With a canned calendar (EA_CANNED_CALENDAR, the release suite) the
    tool runs as the caller, from the tree this module is in, with the
    caller's EA_DIR: the fixture needs no token and no resident user, so
    the suite runs on a machine with neither."""
    run_env = f"EA_RUN_ID={run_id if live else 'shadow-' + run_id}"
    canned = os.environ.get("EA_CANNED_CALENDAR", "").strip()
    if canned:
        return (["env", run_env, f"EA_CANNED_CALENDAR={canned}", sys.executable,
                 str(Path(__file__).resolve().parent / f"{script}.py")] + argv)
    env = [
        f"HOME={EA_BOT_HOME}",
        f"EA_DIR={EA_BOT_HOME / '.ea'}",
        f"PATH={os.environ.get('PATH', '/usr/bin:/usr/local/bin')}",
        f"PYTHONPATH={os.environ.get('PYTHONPATH', '')}",
        run_env,
    ]
    for var in ("GRAPH_CLIENT_ID", "GRAPH_AUTHORITY", "GRAPH_AUTH_METHOD",
                "GRAPH_REDIRECT_URI", "GRAPH_SCOPES"):
        if os.environ.get(var):
            env.append(f"{var}={os.environ[var]}")
    return (["sudo", "-n", "-u", EA_BOT_USER, "env"] + env
            + ["python3", str(TOOL_DIR / f"{script}.py")] + argv)


def run_resident(cmd: list[str], timeout: float = TOOL_TIMEOUT_S) -> tuple[int, str, str]:
    """Run one resident command; (returncode, stdout, stderr). Tests
    replace this."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    except OSError as e:
        return -1, "", str(e)


def resident_write(path: str, text: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Create or replace one file in the resident's own tree, as the
    resident (a staged-event file the bot then claims). Tests replace
    this."""
    try:
        p = subprocess.run(["sudo", "-n", "-u", EA_BOT_USER, "tee", path], input=text,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, "", p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    except OSError as e:
        return -1, "", str(e)


def resident_unlink(path: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Remove one file in the resident's own tree, as the resident (a
    staged-event file the backstop withdraws). Tests replace this."""
    try:
        p = subprocess.run(["sudo", "-n", "-u", EA_BOT_USER, "rm", "-f", "--", path],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, "", p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    except OSError as e:
        return -1, "", str(e)


def _ea_managed_email() -> str:
    try:
        cfg = json.loads(_resident_text(EA_DIR / "config" / "scheduling-preferences.json") or "{}")
        return str((cfg.get("ea_managed_calendar") or {}).get("email") or "")
    except Exception:
        return ""


def _str_args(args: dict, keys: tuple[str, ...]) -> list[str]:
    """Flags for the keys present, dashes for underscores, values as text."""
    out = []
    for k in keys:
        v = args.get(k)
        if v in (None, "", [], {}):
            continue
        flag = "--" + k.replace("_", "-")
        out += [flag, json.dumps(v) if isinstance(v, (dict, list)) else str(v)]
    return out


def live_write(action: str, args: dict, job: dict, *, runner=None, writer=None) -> tuple[dict, str]:
    """Run one write tool for real. Returns (observation, command summary).
    The resident runs the draft, hold and decision verbs under the turn's
    run id; a staged event is written to the resident's state directory
    with this thread's routing keys, for the bot's button; a poll fill is
    never run."""
    runner = runner or run_resident
    writer = writer or resident_write
    run_id = str(job.get("run_id") or "manual")
    if action in STAGED_FILES:
        event = args.get("event")
        route = {k: job.get(k) for k in ("thread_ts", "channel") if job.get(k)}
        if isinstance(event, list):
            event = [dict(e, **route) for e in event if isinstance(e, dict)]
        elif isinstance(event, dict):
            event = dict(event, **route)
        else:
            raise StepError(f"{action} needs event as an object or a list of objects")
        path = str(EA_BOT_HOME / ".ea" / "state" / STAGED_FILES[action])
        rc, _out, err = writer(path, json.dumps(event, indent=1))
        if rc != 0:
            return {"status": "error", "message": f"could not stage: {(err or '')[-300:]}"}, action
        return {"status": "staged", "file": STAGED_FILES[action],
                "note": "staged; the user's button decides. Say so in one line."}, action
    if action == "fill-poll":
        return {"status": "refused",
                "note": "Filling a poll needs the user's approval first. Say in your "
                        "reply which slots you would mark and ask."}, action
    if action in ("create-reply", "create-draft", "update-draft"):
        script, verb = "graph_actions", action
        if action == "create-reply":
            argv = ["--message-id", str(args.get("message_id") or ""),
                    "--conversation-id", str(args.get("conversation_id") or ""),
                    "--body", str(args["body"])]
            if args.get("sender_only"):
                argv.append("--sender-only")
        elif action == "create-draft":
            argv = ["--to", str(args["to"]), "--subject", str(args["subject"]),
                    "--body", str(args["body"])] + _str_args(args, ("cc",))
        else:
            argv = ["--message-id", str(args["draft_id"]), "--body", str(args["body"])]
    elif action == "clear-hold":
        script, verb, argv = "graph_actions", "clear-hold", ["--event-id", str(args["event_id"])]
    elif action == "log-decision":
        script, verb = "scheduling_engine", "log-decision"
        argv = ["--event", str(args["event"]), "--action", str(args["action"])]
        if args.get("for"):
            argv += ["--for", str(args["for"])]
        argv += _str_args(args, ("comment",))
    elif action == "ea-managed-hold":
        import mail_mediation
        script = "google_calendar_client"
        op = str(args.get("op") or "")
        if op == "create":
            for k in ("summary", "start", "end"):
                if not args.get(k):
                    raise StepError(f"ea-managed-hold create needs {k}")
            verb = "create-event"
            argv = ["--email", _ea_managed_email(), "--calendar-id", mail_mediation.ea_managed_calendar_id()
                    ] + _str_args(args, ("summary", "start", "end"))
        elif op in ("update", "delete"):
            if not args.get("event_id"):
                raise StepError(f"ea-managed-hold {op} needs event_id")
            verb = "update-event" if op == "update" else "delete-event"
            argv = ["--event-id", str(args["event_id"])] + (
                _str_args(args, ("summary", "start", "end")) if op == "update" else [])
        else:
            raise StepError("ea-managed-hold op is create, update or delete")
    else:
        raise StepError(f"{action} is not a write tool")
    cmd = _resident_cmd(script, [verb] + argv, run_id, live=True)
    rc, out, err = runner(cmd)
    parsed = _parse_json_out(out)
    summary = (f"{script}.py {verb} " + " ".join(argv))[:300]
    if rc != 0:
        return {"status": "error", "exit": rc, "message": (err or out or "")[-600:]}, summary
    return (parsed if isinstance(parsed, dict) else {"status": "ok", "output": (out or "")[:600]}), summary


def _parse_json_out(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for opener in ("{", "["):
        i = text.find(opener)
        if i >= 0:
            try:
                return json.loads(text[i:])
            except ValueError:
                continue
    return None


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _check_date(args: dict) -> dict:
    try:
        d = date.fromisoformat(str(args.get("date") or ""))
    except ValueError:
        raise StepError("check-date needs date as YYYY-MM-DD") from None
    out = {"date": d.isoformat(), "weekday": d.strftime("%A")}
    want = str(args.get("weekday") or "").strip().lower()
    if want:
        out["matches"] = d.strftime("%A").lower().startswith(want[:3])
    return out


def _validate_write(tool: str, args: dict) -> None:
    for key in WRITE_TOOLS[tool]:
        if args.get(key) in (None, "", [], {}):
            raise StepError(f"{tool} needs {key}")
    if tool in ("stage-event", "stage-event-update", "stage-event-delete"):
        events = args["event"] if isinstance(args["event"], list) else [args["event"]]
        for ev in events:
            if not isinstance(ev, dict):
                raise StepError("event must be an object or a list of objects")
            need = {"stage-event": ("subject", "start", "end"),
                    "stage-event-update": ("event_id", "start", "end"),
                    "stage-event-delete": ("event_id",)}[tool]
            for k in need:
                if not ev.get(k):
                    raise StepError(f"each event needs {k}")
            for k in ("start", "end"):
                if ev.get(k) and _parse_iso(str(ev[k])) is None:
                    raise StepError(f"{k} must be an ISO date-time")
    if tool == "ea-managed-hold" and args.get("op") not in ("create", "update", "delete"):
        raise StepError("op must be create, update or delete")
    if tool in ("create-reply", "create-draft", "update-draft") and not str(args.get("body")).strip():
        raise StepError("body must not be empty")


# ── time parsing ──────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_ISO_DT = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
                     r"(Z|[+-]\d{2}:?\d{2})?")
_WD = r"(?P<wd>mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MON = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_WORDS = re.compile(
    rf"(?:\b{_WD}\.?,?\s+)?(?:\b{_MON}\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?\b"
    rf"|\b(?P<nmon>\d{{1,2}})/(?P<nday>\d{{1,2}})(?:/(?P<nyear>\d{{2,4}}))?\b)",
    re.I)
_CLOCK = re.compile(
    r"(?<![\d/:-])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?"
    r"(?:\s*(?:-|to|until)\s*(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<ap2>[ap]\.?m\.?)?)?"
    r"(?![\d/])",
    re.I)


def _parse_iso(s: str, tz: ZoneInfo | None = None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or ZoneInfo(DEFAULT_TZ))
    return dt


def _hour24(h: int, ap: str | None, ap_other: str | None) -> int | None:
    marker = (ap or ap_other or "").lower().replace(".", "")
    if h > 23:
        return None
    if marker == "pm" and h < 12:
        return h + 12
    if marker == "am" and h == 12:
        return 0
    if marker:
        return h
    if h >= 13:
        return h
    # No marker: a working-day reading (7-11 morning, 12-6 afternoon).
    if 7 <= h <= 11:
        return h
    if h == 12 or 1 <= h <= 6:
        return h + 12 if h != 12 else 12
    return None


def _resolve_year(month: int, day: int, year: int | None, today: date) -> date | None:
    try:
        if year:
            return date(year if year > 99 else 2000 + year, month, day)
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today - timedelta(days=60):
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d


def extract_times(text: str, today: date | None = None,
                  tz: ZoneInfo | None = None) -> tuple[list[dict], list[str]]:
    """Concrete times named in free text. Returns (times, mismatches):
    times are {"start": datetime, "end": datetime | None}; mismatches name
    each "<weekday> <date>" pair whose weekday is wrong."""
    tz = tz or ZoneInfo(DEFAULT_TZ)
    today = today or datetime.now(tz).date()
    text = text or ""
    found: list[dict] = []
    mismatches: list[str] = []
    seen: set = set()

    def add(start: datetime, end: datetime | None):
        key = (start.astimezone(timezone.utc).isoformat(),)
        if key not in seen:
            seen.add(key)
            found.append({"start": start, "end": end})

    iso_spans = []
    for m in _ISO_DT.finditer(text):
        dt = _parse_iso(m.group(0), tz)
        if dt is not None:
            add(dt, None)
            iso_spans.append(m.span())

    dates = [m for m in _DATE_WORDS.finditer(text)
             if not any(a <= m.start() < b for a, b in iso_spans)]
    for i, m in enumerate(dates):
        if m.group("mon"):
            month = _MONTHS[m.group("mon")[:3].lower()]
            day = int(m.group("day"))
            year = int(m.group("year")) if m.group("year") else None
        else:
            month, day = int(m.group("nmon")), int(m.group("nday"))
            year = int(m.group("nyear")) if m.group("nyear") else None
            if not (1 <= month <= 12):
                continue
        d = _resolve_year(month, day, year, today)
        if d is None:
            continue
        if m.group("wd"):
            wd = m.group("wd").lower()[:3]
            if d.strftime("%a").lower() != wd:
                mismatches.append(f"{m.group('wd')} {d.isoformat()} is a {d.strftime('%A')}")
        stop = dates[i + 1].start() if i + 1 < len(dates) else len(text)
        window = text[m.end():min(stop, m.end() + 160)]
        for c in _CLOCK.finditer(window):
            if not (c.group("m") or c.group("ap") or c.group("h2")):
                continue
            h = _hour24(int(c.group("h")), c.group("ap"), c.group("ap2"))
            if h is None:
                continue
            start = datetime(d.year, d.month, d.day, h, int(c.group("m") or 0), tzinfo=tz)
            end = None
            if c.group("h2"):
                h2 = _hour24(int(c.group("h2")), c.group("ap2"), c.group("ap"))
                if h2 is not None:
                    end = datetime(d.year, d.month, d.day, h2, int(c.group("m2") or 0), tzinfo=tz)
                    if end <= start:
                        end = None
            add(start, end)
    return found, mismatches


# ── checks ────────────────────────────────────────────────────────────

_RULES = (
    ("dash", re.compile("[—–]")),
    ("double-asterisk", re.compile(r"\*\*")),
    ("teams", re.compile(r"\b(?:MS |Microsoft )?Teams\b")),
)
# Talk about the machinery is a rule for the thread reply only; an email
# body may well mention tools.
_TOOL_TALK = re.compile(r"\b(?:tool calls?|tools?|sandbox|permissions?|allowlist|args_json|json|"
                        r"scheduling_engine|graph_actions|[a-z_]+\.py)\b", re.I)
# Declining on the user's behalf is a rule for the email bodies; a thread
# reply that says "Lane can't make Tuesday" reports, it does not decline.
_DECLINES = re.compile(r"\b(?:declin(?:e|es|ed|ing)|(?:I|we) (?:can(?:'|no)t|cannot|won't be able to|"
                       r"am unable to|are unable to) (?:make|attend|join|meet))\b", re.I)


def _video_platform() -> str:
    """The institution's video platform from the profile, '' when unknown.
    Naming Teams breaks a rule only when the user's platform is another."""
    try:
        import institution
        return institution.video()["platform"]
    except Exception:
        return ""


def rule_breaks(text: str, *, drafts: list[str] | None = None, today: date | None = None,
                tz: ZoneInfo | None = None) -> list[str]:
    """Codes of the scheduling lane's writing rules that the thread reply
    `text` and the email `drafts` break."""
    blob = "\n".join([text or ""] + list(drafts or []))
    out = [code for code, rx in _RULES if rx.search(blob)]
    if "teams" in out and "teams" in _video_platform().lower():
        out.remove("teams")
    if _TOOL_TALK.search(text or ""):
        out.append("tool-talk")
    if any(_DECLINES.search(d or "") for d in drafts or []):
        out.append("declines")
    _, mismatches = extract_times(blob, today, tz)
    if mismatches:
        out.append("weekday-mismatch")
    return out


def _mode_from_names(names: list[str], blob: str = "") -> str:
    joined = " ".join(names) + " " + blob
    if "reschedule-brief" in joined:
        return "reschedule"
    if "booking" in joined:
        return "booking"
    if "read-poll" in joined or "fill-poll" in joined or "poll@placeholder" in joined \
            or "sender-only" in joined or "sender_only" in joined:
        return "poll"
    if "propose-meeting" in joined:
        return "propose"
    if "validate-draft" in joined or "check-attendees" in joined:
        return "validate"
    return "other"


def shadow_mode(steps: list[dict], writes: list[dict]) -> str:
    names = [s.get("action", "") for s in steps]
    blob = json.dumps([s.get("args") for s in steps] + writes, default=str)
    return _mode_from_names(names, blob)


def claude_mode(tool_calls: list) -> str:
    summaries = [str(t[2]) if isinstance(t, (list, tuple)) and len(t) > 2 else str(t)
                 for t in tool_calls or []]
    return _mode_from_names(summaries)


def solver_windows(results: list[dict]) -> list[tuple[datetime, datetime]]:
    """Every window a solver output of this turn sanctioned: propose-meeting
    and booking-propose options and conditional options, reschedule-brief
    options and its valid proposed times, validate-draft times that came
    back valid, and a check-attendees window everyone can make."""
    wins: list[tuple[datetime, datetime]] = []

    def opt_windows(obj):
        for key in ("options", "conditional_options"):
            for o in (obj or {}).get(key) or []:
                if isinstance(o, dict):
                    s, e = _parse_iso(str(o.get("start", ""))), _parse_iso(str(o.get("end", "")))
                    if s and e:
                        wins.append((s, e))

    for r in results:
        tool, args, out = r.get("tool"), r.get("args") or {}, r.get("parsed")
        if not isinstance(out, dict):
            continue
        dur = int(args.get("duration") or out.get("duration_min") or 30)
        if tool in ("propose-meeting", "booking-propose", "reschedule-brief"):
            opt_windows(out)
        if tool == "reschedule-brief":
            for p in out.get("proposed_times") or []:
                s = _parse_iso(str(p.get("time", ""))) if p.get("valid") else None
                if s:
                    wins.append((s, s + timedelta(minutes=dur)))
        if tool == "validate-draft" and out.get("valid"):
            times = args.get("times")
            if isinstance(times, str):
                try:
                    times = json.loads(times)
                except ValueError:
                    times = []
            for t in times or []:
                s = _parse_iso(str(t))
                if s:
                    wins.append((s, s + timedelta(minutes=dur)))
        if tool == "check-attendees" and out.get("all_attendees_free"):
            s, e = _parse_iso(str(args.get("start", ""))), _parse_iso(str(args.get("end", "")))
            if s and e:
                wins.append((s, e))
    return wins


def _inside(t: dict, wins: list[tuple[datetime, datetime]]) -> bool:
    s = t["start"]
    e = t.get("end")
    for ws, we in wins:
        if ws <= s < we and (e is None or e <= we + timedelta(minutes=1)):
            return True
    return False


def _event_times(events) -> list[dict]:
    out = []
    for ev in events if isinstance(events, list) else [events]:
        if isinstance(ev, dict):
            for sub in ev.get("events") or [ev]:
                if isinstance(sub, dict):
                    s = _parse_iso(str(sub.get("start", "")))
                    e = _parse_iso(str(sub.get("end", "")))
                    if s:
                        out.append({"start": s, "end": e})
    return out


def side_times(text: str, drafts: list[str], events: list, today: date | None,
               tz: ZoneInfo) -> list[dict]:
    times: list[dict] = []
    seen = set()
    blobs = [text or ""] + [d or "" for d in drafts]
    for blob in blobs:
        for t in extract_times(blob, today, tz)[0]:
            k = t["start"].astimezone(timezone.utc).isoformat()
            if k not in seen:
                seen.add(k)
                times.append(t)
    for t in _event_times(events):
        k = t["start"].astimezone(timezone.utc).isoformat()
        if k not in seen:
            seen.add(k)
            times.append(t)
    return times


def _validate_times(times: list[dict], run_id: str, runner) -> dict:
    """One fresh validate-draft per time (capped): free and within rules?"""
    checked, valid, invalid = 0, 0, []
    for t in times[:CHECK_TIMES_CAP]:
        dur = 30
        if t.get("end"):
            dur = max(5, int((t["end"] - t["start"]).total_seconds() // 60))
        argv = ["validate-draft", "--times", json.dumps([t["start"].isoformat()]),
                "--duration", str(dur)]
        rc, out, _ = runner(_resident_cmd("scheduling_engine", argv, run_id))
        parsed = _parse_json_out(out)
        if rc != 0 or not isinstance(parsed, dict) or "valid" not in parsed:
            continue
        checked += 1
        if parsed.get("valid"):
            valid += 1
        else:
            invalid.append(t["start"].isoformat())
    return {"named": len(times), "checked": checked, "valid": valid, "invalid": invalid}


def _starts(times: list[dict]) -> set[str]:
    return {t["start"].strftime("%Y-%m-%dT%H:%M") for t in times}


def _claude_staged_for(job: dict) -> list[dict]:
    """What Claude staged in this thread around its turn."""
    thread = str(job.get("thread_ts") or "")
    if not thread:
        return []
    t_end = _parse_iso(str(job.get("ts") or "")) or _now()
    t_start = t_end - timedelta(seconds=float((job.get("claude") or {}).get("elapsed_s") or 0) + 60)
    out = []
    for rec in _rl.read(CLAUDE_STAGED):
        if str(rec.get("thread_ts") or "") != thread:
            continue
        ts = _parse_iso(str(rec.get("ts") or ""))
        if ts and t_start <= ts <= t_end + timedelta(minutes=10):
            out.append(rec.get("action") or {})
    return out


def score_turn(job: dict, turn: dict, results: list[dict], *, runner=None,
               today: date | None = None, tz: ZoneInfo | None = None,
               validate: bool = True) -> dict:
    """The checks for both sides of one turn."""
    tz = tz or ZoneInfo(DEFAULT_TZ)
    runner = runner or run_resident
    claude = job.get("claude") or {}
    writes = turn.get("writes") or []

    sh_drafts = [str((w.get("args") or {}).get("body") or "") for w in writes
                 if w.get("tool") in ("create-reply", "create-draft", "update-draft")]
    sh_events = [(w.get("args") or {}).get("event") for w in writes
                 if w.get("tool") in ("stage-event", "stage-event-update")]
    sh_times = side_times(turn.get("reply", ""), sh_drafts, sh_events, today, tz)
    cl_staged = _claude_staged_for(job)
    cl_events = [a for a in cl_staged if str(a.get("action", "")).startswith("event_")
                 and a.get("action") != "event_delete"]
    cl_drafts = [str(d) for d in claude.get("drafts") or [] if d]
    cl_times = side_times(str(claude.get("reply") or ""), cl_drafts, cl_events, today, tz)

    wins = solver_windows(results)
    invented = [t["start"].isoformat() for t in sh_times if not _inside(t, wins)]

    sh_breaks = rule_breaks(turn.get("reply", ""), drafts=sh_drafts, today=today, tz=tz)
    cl_breaks = rule_breaks(str(claude.get("reply") or ""), drafts=cl_drafts, today=today, tz=tz)

    sh_mode = shadow_mode(turn.get("steps") or [], writes)
    cl_mode = claude_mode(claude.get("tool_calls") or [])
    if sh_mode == "poll" and any(w.get("tool") == "create-reply"
                                 and not (w.get("args") or {}).get("sender_only")
                                 for w in writes):
        sh_breaks.append("reply-all-on-poll")
    cl_summ = " ".join(str(t[2]) for t in claude.get("tool_calls") or []
                       if isinstance(t, (list, tuple)) and len(t) > 2)
    if cl_mode == "poll" and "create-reply" in cl_summ and "--sender-only" not in cl_summ:
        cl_breaks.append("reply-all-on-poll")

    a, b = _starts(sh_times), _starts(cl_times)
    overlap = (len(a & b) / len(a | b)) if (a | b) else None

    out = {
        "shadow": {"times": sorted(a), "invented": invented, "breaks": sh_breaks,
                   "mode": sh_mode},
        "claude": {"times": sorted(b), "breaks": cl_breaks, "mode": cl_mode,
                   "staged": len(cl_staged)},
        "mode_agree": sh_mode == cl_mode,
        "overlap": overlap,
    }
    if validate:
        rid = str(job.get("run_id") or "check")
        out["shadow"]["validity"] = _validate_times(sh_times, rid, runner)
        out["claude"]["validity"] = _validate_times(cl_times, rid, runner)
    return out


# ── prompt ────────────────────────────────────────────────────────────

def _repo_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _resident_text(path: Path) -> str:
    try:
        import resident_io
        return resident_io.read_text(path) or ""
    except Exception:
        return ""


def _user_tz(profile_text: str) -> ZoneInfo:
    try:
        name = (json.loads(profile_text) or {}).get("timezone") or DEFAULT_TZ
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _tool_catalog() -> str:
    lines = []
    for name in ACTIONS:
        if name in READ_TOOLS:
            keys = ", ".join(f"{k}{'' if req else '?'}" for k, (_, _, req) in READ_TOOLS[name][2].items())
            lines.append(f"- {name} ({keys}): {TOOL_HELP[name]}")
        else:
            lines.append(f"- {name}: {TOOL_HELP[name]}")
    return "\n".join(lines)


def build_prompt(job: dict) -> tuple[list[dict], ZoneInfo]:
    profile = _resident_text(EA_DIR / "config" / "user-profile.json")
    prefs = _resident_text(EA_DIR / "config" / "scheduling-preferences.json")
    tz = _user_tz(profile)
    try:
        import preferences
        user_rules = preferences.render(["scheduling"])
    except Exception:
        user_rules = ""
    now = datetime.now(tz)
    system = "\n\n".join(p for p in (
        _repo_text(SKILL_FILE),
        f"=== USER PROFILE ===\n{profile or '(not found)'}\n=== END USER PROFILE ===",
        f"SCHEDULING RULES (authoritative source):\n{_repo_text(RULES_FILE)}",
        f"Scheduling preferences (JSON):\n{prefs}" if prefs else "",
        f"STAGED EVENTS (JSON shapes and workflows):\n{_repo_text(STAGED_GUIDE)}",
        f"DRAFTING VOICE:\n{_repo_text(VOICE_GUIDE)}",
        user_rules,
        f"TOOLS (action names; '?' marks an optional key):\n{_tool_catalog()}",
        f"Now: {now.strftime('%A %Y-%m-%d %H:%M %Z')}. Thread: {job.get('thread_ts') or 'new'}.",
    ) if p)
    lane = job.get("lane") or "sched-channel"
    task = str(job.get("task_block") or job.get("user_msg") or "")
    history = str(job.get("history") or "")
    user = (f"Lane: {lane}. Answer this as the scheduling assistant, one JSON step at a time.\n\n"
            + (f"{history}\n\n" if history else "") + f"{task}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], tz


STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "args_json": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["action", "args_json", "note"],
    "additionalProperties": False,
}


def _trim(messages: list[dict]) -> None:
    """Keep the conversation under CONV_CHARS by blanking the oldest
    observations first."""
    total = sum(len(m["content"]) for m in messages)
    for m in messages[2:]:
        if total <= CONV_CHARS:
            return
        if m["role"] == "user" and m["content"].startswith("OBSERVATION") \
                and not m["content"].endswith("[trimmed]"):
            head = m["content"].split("\n", 1)[0]
            total -= len(m["content"]) - len(head) - 10
            m["content"] = head + "\n[trimmed]"


def _kept_in(text: str) -> str:
    cfg = _rl.config()
    for addr in sorted(set(_EMAIL.findall(text or ""))):
        why = _rl.keep_reason(addr, cfg)
        if why:
            return why
    return ""


def run_agent(job: dict, *, chat=None, runner=None, clock=time.monotonic, live: bool = False,
              writer=None) -> tuple[dict, list[dict], list[dict]]:
    """Run one turn. Returns (turn record, tool results, messages). With
    `live`, write tools run for real (live_write)."""
    chat = chat or _rl.chat_json
    runner = runner or run_resident
    run_id = str(job.get("run_id") or "manual")
    turn = {"run_id": run_id, "ts": _iso(), "thread_ts": job.get("thread_ts", ""),
            "lane": job.get("lane", ""), "mixed": bool(job.get("mixed")), "live": bool(live),
            "status": "ok", "steps": [], "reply": "", "writes": [],
            "solver_options": [], "latency_s": 0.0, "cold_start": False,
            "schema_failures": 0, "error": "", "keep_from_anthropic": ""}
    results: list[dict] = []
    turn["keep_from_anthropic"] = _kept_in(
        str(job.get("task_block") or "") + "\n" + str(job.get("user_msg") or ""))

    messages, _tz = build_prompt(job)
    t0 = clock()
    drafts = 0
    for _step in range(STEP_BUDGET):
        if clock() - t0 > TURN_TIMEOUT_S:
            turn["status"] = "timeout"
            break
        try:
            obj, meta = chat(messages, purpose="sched-agent", schema=STEP_SCHEMA,
                             schema_name="step", max_tokens=STEP_MAX_TOKENS)
        except _rl.RemoteLLMError as e:
            turn["status"], turn["error"] = "error", str(e)[:300]
            break
        latency = int((meta or {}).get("latency_ms") or 0)
        if latency > COLD_MS:
            turn["cold_start"] = True
        action = str(obj.get("action") or "")
        step = {"action": action, "args": None, "ok": False, "latency_ms": latency,
                "note": str(obj.get("note") or "")[:200]}
        messages.append({"role": "assistant", "content": json.dumps(obj)})
        try:
            if action not in ACTIONS:
                raise StepError(f"unknown action {action!r}")
            try:
                args = json.loads(obj.get("args_json") or "{}")
            except ValueError:
                raise StepError("args_json is not valid JSON") from None
            if not isinstance(args, dict):
                raise StepError("args_json must be a JSON object")
            step["args"] = json.dumps(args)[:300]

            if action == "reply":
                text = str(args.get("text") or "").strip()
                if not text:
                    raise StepError("reply needs text")
                turn["reply"] = text
                step["ok"] = True
                turn["steps"].append(step)
                break

            if action in WRITE_TOOLS:
                _validate_write(action, args)
                if live:
                    obs, step["cmd"] = live_write(action, args, job, runner=runner, writer=writer)
                    turn["writes"].append({"tool": action, "args": args, "executed": True,
                                           "result": str(obs.get("status") or "")[:40],
                                           "ids": {k: str(obs[k])[:200] for k in ("id", "draft_id", "event_id")
                                                   if obs.get(k)}})
                else:
                    turn["writes"].append({"tool": action, "args": args})
                    obs = {"status": "recorded"}
                    if action in ("create-reply", "create-draft"):
                        drafts += 1
                        obs["draft_id"] = f"shadow-draft-{drafts}"
                    if action.startswith("stage-event"):
                        obs["note"] = "staged; the user's button decides"
                obs_text = json.dumps(obs)
            elif action == "check-date":
                obs_text = json.dumps(_check_date(args))
            else:
                script = READ_TOOLS[action][0]
                argv = _argv_for(action, args)
                cmd = _resident_cmd(script, argv, run_id, live=live)
                step["cmd"] = f"{script}.py " + " ".join(argv)[:300]
                rc, out, err = runner(cmd)
                parsed = _parse_json_out(out)
                results.append({"tool": action, "args": args, "rc": rc, "parsed": parsed})
                obs_text = out if rc == 0 else f"exit {rc}\n{out}\n{(err or '')[-1500:]}"
                if not turn["keep_from_anthropic"]:
                    turn["keep_from_anthropic"] = _kept_in(obs_text)
            step["ok"] = True
        except StepError as e:
            turn["schema_failures"] += 1
            step["error"] = str(e)[:200]
            obs_text = json.dumps({"error": str(e)})
        turn["steps"].append(step)
        if len(obs_text) > OBS_CHARS:
            obs_text = obs_text[:OBS_CHARS] + f"\n[truncated {len(obs_text) - OBS_CHARS} chars]"
        messages.append({"role": "user", "content": f"OBSERVATION ({action}):\n{obs_text}"})
        _trim(messages)
    else:
        turn["status"] = "budget"

    if turn["status"] == "ok" and not turn["reply"]:
        turn["status"] = "budget"
    turn["latency_s"] = round(clock() - t0, 1)
    turn["solver_options"] = [[s.isoformat(), e.isoformat()] for s, e in solver_windows(results)]
    return turn, results, messages


# ── live turns ────────────────────────────────────────────────────────

def thread_history(thread_ts: str, n: int = HISTORY_TURNS) -> str:
    """This thread's earlier live turns, as text for the model: what the
    user wrote and what the agent answered. '' for a new thread."""
    if not thread_ts:
        return ""
    turns = [t for t in _rl.read(TURNS) if t.get("live") and t.get("thread_ts") == thread_ts
             and t.get("reply")][-n:]
    if not turns:
        return ""
    lines = ["Earlier in this thread (your own previous turns, oldest first; what is true now "
             "comes from the tools):"]
    for t in turns:
        if t.get("user_msg"):
            lines.append(f"User: {str(t['user_msg'])[:600]}")
        lines.append(f"You: {str(t['reply'])[:800]}")
        done = [w.get("tool") for w in t.get("writes") or [] if w.get("executed")]
        if done:
            lines.append(f"(you ran: {', '.join(done)})")
    return "\n".join(lines)


def _wrote_something(turn: dict) -> bool:
    return any(w.get("executed") and w.get("result") not in ("error", "refused")
               for w in turn.get("writes") or [])


def fallback_reply(turn: dict) -> str:
    """A short reply for a live turn that wrote something and then
    stopped, so the bot posts the buttons it earned and does not run
    Claude over the same request."""
    done = [w for w in turn.get("writes") or [] if w.get("executed")
            and w.get("result") not in ("error", "refused")]
    parts = []
    if any(w["tool"] in ("create-reply", "create-draft", "update-draft") for w in done):
        parts.append("I drafted the reply; the Send button is in your DM.")
    if any(w["tool"].startswith("stage-event") for w in done):
        parts.append("I staged the event; the button is in your DM.")
    if any(w["tool"] in ("ea-managed-hold", "clear-hold") for w in done):
        parts.append("I updated the holds.")
    parts.append("I could not finish the rest of this request. Tell me what else you need.")
    return " ".join(parts)


# ── the Claude backstop ───────────────────────────────────────────────
#
# Every write a live turn makes (a draft, a staged event, a hold) is
# reviewed by Claude before the user sees its card. The review reads
# what the agent read (its tool observations), what it wrote and the
# scheduling rules, and answers ok or withhold. Withheld writes are
# undone as the resident (the draft deleted, the staged file removed,
# the hold deleted) and Claude takes the turn from the start. A review
# that fails withholds too: nothing defective reaches a Send button.

BACKSTOP_MODEL = "opus"
BACKSTOP_OBS_CHARS = 8000
BACKSTOP_MATERIAL_CHARS = 60_000
BACKSTOP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "issues", "note"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "withhold"]},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "detail"],
            "properties": {"kind": {"type": "string", "enum": [
                "time", "attendee", "recipient", "content", "rule", "name", "other"]},
                "detail": {"type": "string"}}}},
        "note": {"type": "string"},
    },
}
BACKSTOP_PROMPT = """You are the backstop on a scheduling assistant. Another model took one turn for the user and wrote the drafts and staged events below. The user will see a Send or Create button for each. Decide whether they can go out as they are.

Withhold when any of these holds:
- a proposed or confirmed time is not among the solver's options, or the calendar shows a conflict for the user or a named attendee;
- a date, weekday or clock time in a draft does not match the thread or the staged event;
- the reply goes to the wrong people: reply-all on a poll or on a message that asked only the user, a missing or extra attendee on the event, a wrong address;
- the draft does not answer what the sender asked, invents a fact, or misnames someone;
- the staged event and the draft disagree, or the event lacks a field the rules require;
- a scheduling rule below is broken.
Say ok when the writes would pass a careful assistant's own check. Do not withhold for style.
When the agent wrote nothing, review its reply the same way: every time it names must appear as free in the solver or calendar output it saw; the question the user or sender asked must be answered; a reply that asks the user for something the agent could have read from its own tool output, or that says it could not find a thread it had in its results, is withheld.

## Scheduling rules
{rules}

## The request
{request}

## What the agent read (its tool calls and their results)
{reads}

## What the agent wrote
{writes}

## The agent's reply to the user
{reply}

Answer with one JSON object and nothing else: {{"verdict": "ok" or "withhold", "issues": [{{"kind": "time|attendee|recipient|content|rule|name|other", "detail": "..."}}], "note": "one line"}}"""


def backstop_on(cfg: dict | None = None) -> bool:
    """On unless remote-llm.json says `"sched_backstop": false`."""
    cfg = cfg if cfg is not None else _rl.config()
    return cfg.get("sched_backstop") is not False


def backstop_model() -> str:
    try:
        import ea_config
        return ea_config.get_models().get("scheduler") or BACKSTOP_MODEL
    except Exception:
        return BACKSTOP_MODEL


def _backstop_material(job: dict, turn: dict, results: list[dict]) -> dict:
    reads = []
    for r in results:
        body = (json.dumps(r.get("parsed"), default=str) if r.get("parsed") is not None
                else "(no JSON output)")
        reads.append(f"### {r.get('tool')} {json.dumps(r.get('args') or {})[:400]}\n"
                     f"{body[:BACKSTOP_OBS_CHARS]}")
    writes = []
    for w in turn.get("writes") or []:
        if not w.get("executed") or w.get("result") in ("error", "refused"):
            continue
        writes.append(f"### {w.get('tool')} -> {w.get('result')}\n"
                      f"{json.dumps(w.get('args') or {}, indent=1, default=str)[:BACKSTOP_OBS_CHARS]}")
    request = "\n\n".join(s for s in (str(job.get("history") or ""), str(job.get("task_block") or ""),
                                      "User: " + str(job.get("user_msg") or "")) if s.strip())
    return {"rules": _repo_text(RULES_FILE)[:20_000],
            "request": request[:BACKSTOP_MATERIAL_CHARS // 3],
            "reads": "\n\n".join(reads)[:BACKSTOP_MATERIAL_CHARS],
            "writes": "\n\n".join(writes) or "(nothing)",
            "reply": str(turn.get("reply") or "")[:2000]}


def _claude_json(prompt: str, schema: dict, model: str) -> tuple[dict, dict]:
    """One tool-less Claude run as the resident; (answer, meta)."""
    import mail_privacy
    r = mail_privacy._run_claude(prompt, model, "")
    if r.get("rc") or not r.get("text"):
        raise RuntimeError(f"claude rc={r.get('rc')}: {(r.get('stderr') or '')[-160:]}")
    text = str(r["text"]).strip()
    try:
        obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        raise RuntimeError("the backstop's answer is not JSON") from None
    return obj, {"cost_usd": r.get("cost_usd")}


def backstop_review(job: dict, turn: dict, results: list[dict], *, judge=None) -> dict:
    """Claude's verdict on a live turn's writes: {"verdict", "issues",
    "note", "model", "cost_usd"}. `judge(prompt, schema, model)` runs
    the review; tests replace it."""
    judge = judge or _claude_json
    model = backstop_model()
    obj, meta = judge(BACKSTOP_PROMPT.format(**_backstop_material(job, turn, results)),
                      BACKSTOP_SCHEMA, model)
    verdict = "ok" if obj.get("verdict") == "ok" else "withhold"
    # A reason is kept whole: the user reads it in the channel, and a
    # sentence cut mid-word reads as a fault of its own.
    issues = [{"kind": str(i.get("kind") or "other")[:20], "detail": str(i.get("detail") or "")[:1000]}
              for i in (obj.get("issues") or []) if isinstance(i, dict)][:12]
    return {"verdict": verdict, "issues": issues, "note": str(obj.get("note") or "")[:600],
            "model": model, "cost_usd": (meta or {}).get("cost_usd")}


def withdraw_writes(turn: dict, job: dict, *, runner=None, remover=None) -> list[dict]:
    """Undo a live turn's writes before the user sees them: a created
    draft is deleted, a staged event file removed, a created hold
    deleted, each as the resident under the turn's run id (the draft
    journal records the deletion, so no card is posted). An updated
    draft, a logged decision and a cleared hold stay as they are and
    are reported."""
    runner = runner or run_resident
    remover = remover or resident_unlink
    run_id = str(job.get("run_id") or "manual")
    done = []
    for w in turn.get("writes") or []:
        if not w.get("executed") or w.get("result") in ("error", "refused"):
            continue
        tool, ids = str(w.get("tool") or ""), w.get("ids") or {}
        rec = {"tool": tool, "ok": True, "how": "left as is"}
        if tool in ("create-reply", "create-draft") and ids.get("id"):
            cmd = _resident_cmd("graph_actions", ["delete-draft", "--message-id", ids["id"]], run_id, live=True)
            rc, _out, err = runner(cmd)
            rec.update(ok=rc == 0, how=f"delete-draft {ids['id'][:16]}")
        elif tool in STAGED_FILES:
            rc, _out, err = remover(str(EA_BOT_HOME / ".ea" / "state" / STAGED_FILES[tool]))
            rec.update(ok=rc == 0, how=f"removed {STAGED_FILES[tool]}")
        elif tool == "ea-managed-hold" and (w.get("args") or {}).get("op") == "create" and ids.get("id"):
            cmd = _resident_cmd("google_calendar_client", ["delete-event", "--event-id", ids["id"]],
                                run_id, live=True)
            rc, _out, err = runner(cmd)
            rec.update(ok=rc == 0, how=f"delete-event {ids['id'][:16]}")
        else:
            done.append(rec)
            continue
        if not rec["ok"]:
            rec["error"] = (err or "")[-200:]
        done.append(rec)
    return done


def run_live(job: dict, *, chat=None, runner=None, writer=None, validate: bool = True,
             judge=None, remover=None) -> dict:
    """Answer one scheduling turn for real. Returns the turn record; the
    bot posts `reply` when status is ok, or falls back to Claude when
    the turn wrote nothing and has no reply, or when the backstop
    withheld what it wrote. Owner process."""
    job = dict(job, history=thread_history(str(job.get("thread_ts") or "")))
    turn, results, messages = run_agent(job, chat=chat, runner=runner, live=True, writer=writer)
    turn["user_msg"] = str(job.get("user_msg") or "")[:600]
    if turn["status"] != "ok" or not turn["reply"]:
        if _wrote_something(turn):
            turn["reply"] = fallback_reply(turn)
            turn["fallback"] = "code reply"
        else:
            turn["fallback"] = "claude"
    if _wrote_something(turn) and backstop_on():
        try:
            review = backstop_review(job, turn, results, judge=judge)
        except Exception as e:
            review = {"verdict": "withhold", "note": "the review failed; fail closed",
                      "issues": [{"kind": "other", "detail": f"review failed: {e}"[:300]}],
                      "model": backstop_model(), "cost_usd": None}
        turn["backstop"] = review
        if review["verdict"] != "ok":
            turn["withdrawn"] = withdraw_writes(turn, job, runner=runner, remover=remover)
            turn["fallback"] = "claude"
            turn["reply"] = ""
            _log(f"{job.get('run_id')} backstop withheld: "
                 + ("; ".join(i["detail"][:100] for i in review["issues"]) or review["note"]))
    if validate:
        try:
            turn["checks"] = score_turn(job, turn, results, runner=runner, validate=validate)
        except Exception as e:
            turn["checks"] = {"error": str(e)[:200]}
    _rl.append(TURNS, turn)
    rid = _SAFE_ID.sub("_", str(job.get("run_id") or "manual"))[:64]
    _private_write(_rl.store_dir() / TRANSCRIPTS_DIR / f"{rid}.json",
                   json.dumps({"job": job, "turn": turn, "messages": messages,
                               "results": results}, default=str))
    return turn


# ── the runner ────────────────────────────────────────────────────────

def _job_age_s(job: dict) -> float:
    ts = _parse_iso(str(job.get("queued_at") or job.get("ts") or ""))
    return (_now() - ts).total_seconds() if ts else 0.0


def execute_job(job: dict, *, chat=None, runner=None, validate: bool = True) -> dict:
    """Run, score and record one job. Returns the turn record."""
    claude = job.get("claude") or {}
    if _job_age_s(job) > BACKLOG_S:
        turn = {"run_id": job.get("run_id"), "ts": _iso(), "thread_ts": job.get("thread_ts", ""),
                "lane": job.get("lane", ""), "mixed": bool(job.get("mixed")),
                "status": "skipped-backlog", "steps": [], "reply": "", "writes": []}
        results, messages = [], []
    else:
        turn, results, messages = run_agent(job, chat=chat, runner=runner)
    turn["claude"] = {"reply": str(claude.get("reply") or "")[:4000],
                      "drafts": [str(d)[:4000] for d in claude.get("drafts") or []][:5],
                      "tool_calls": [list(t)[:3] for t in claude.get("tool_calls") or []][:40],
                      "elapsed_s": claude.get("elapsed_s"),
                      "returncode": claude.get("returncode")}
    if turn["status"] in ("ok", "budget", "timeout"):
        try:
            turn["checks"] = score_turn(job, turn, results, runner=runner, validate=validate)
        except Exception as e:
            turn["checks"] = {"error": str(e)[:200]}
    _rl.append(TURNS, turn)
    # The pipeline (code owns the numbers, the model picks) shadows the
    # same turn, once per prompt variant; its records stand beside this
    # one in pipeline-turns.jsonl.
    if turn["status"] != "skipped-backlog":
        try:
            import sched_pipeline
            sched_pipeline.shadow(job, runner=runner)
        except Exception as e:
            _log(f"pipeline shadow failed: {e}")
    if messages:
        rid = _SAFE_ID.sub("_", str(job.get("run_id") or "manual"))[:64]
        _private_write(_rl.store_dir() / TRANSCRIPTS_DIR / f"{rid}.json",
                       json.dumps({"job": job, "turn": turn, "messages": messages,
                                   "results": results}, default=str))
    return turn


def run_job(path: str) -> int:
    """The detached runner's entry: one job at a time, in arrival order."""
    p = Path(path)
    try:
        jobs_dir = (_rl.store_dir() / JOBS_DIR).resolve()
        if p.resolve().parent != jobs_dir:
            _log(f"refused job outside the queue: {path}")
            return 2
    except OSError:
        return 2
    lock_fd = os.open(_rl.store_dir() / "sched-agent.lock",
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            job = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            _log(f"job unreadable: {e}")
            return 1
        if not _shadow_on():
            p.unlink(missing_ok=True)
            return 0
        turn = execute_job(job)
        _log(f"{turn.get('run_id')} {turn.get('status')} steps={len(turn.get('steps') or [])} "
             f"{turn.get('latency_s', 0)}s")
        p.unlink(missing_ok=True)
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# ── report ────────────────────────────────────────────────────────────

def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def _outcome_match(turns: list[dict], outcomes: list[dict]) -> dict:
    """For each event the user created or moved by button in a shadowed
    thread: did each side name that start time in the thread?"""
    by_thread: dict[str, dict[str, set]] = {}
    for t in turns:
        th = str(t.get("thread_ts") or "")
        ch = t.get("checks") or {}
        if not th or "shadow" not in ch:
            continue
        d = by_thread.setdefault(th, {"shadow": set(), "claude": set()})
        d["shadow"] |= set(ch["shadow"].get("times") or [])
        d["claude"] |= set(ch["claude"].get("times") or [])
    n = sh = cl = 0
    for o in outcomes:
        if not o.get("ok") or o.get("kind") not in ("create", "update"):
            continue
        th = str(o.get("thread_ts") or "")
        if th not in by_thread:
            continue
        s = _parse_iso(str((o.get("event") or {}).get("start") or ""))
        if not s:
            continue
        key = s.astimezone(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%dT%H:%M")
        n += 1
        sh += key in by_thread[th]["shadow"]
        cl += key in by_thread[th]["claude"]
    return {"events": n, "shadow_named": sh, "claude_named": cl}


def report(since: date | None = None) -> dict:
    turns = _rl.read(TURNS)
    if since:
        turns = [t for t in turns if (_parse_iso(str(t.get("ts") or "")) or _now()).date() >= since]
    live = [t for t in turns if t.get("live")]
    turns = [t for t in turns if not t.get("live")]
    by_status: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    for t in turns:
        by_status[t.get("status", "?")] = by_status.get(t.get("status", "?"), 0) + 1
        by_lane[t.get("lane", "?")] = by_lane.get(t.get("lane", "?"), 0) + 1
    scored = [t for t in turns if "shadow" in (t.get("checks") or {})]
    live_status: dict[str, int] = {}
    for t in live:
        k = t.get("fallback") or t.get("status", "?")
        live_status[k] = live_status.get(k, 0) + 1
    live_lat = [float(t.get("latency_s") or 0) for t in live]

    def side_sum(side: str) -> dict:
        named = checked = valid = 0
        breaks: dict[str, int] = {}
        for t in scored:
            c = t["checks"][side]
            v = c.get("validity") or {}
            named += v.get("named", 0)
            checked += v.get("checked", 0)
            valid += v.get("valid", 0)
            for b in c.get("breaks") or []:
                breaks[b] = breaks.get(b, 0) + 1
        return {"times_named": named, "times_checked": checked, "times_valid": valid,
                "breaks": breaks}

    invented = sum(len(t["checks"]["shadow"].get("invented") or []) for t in scored)
    overlaps = [t["checks"]["overlap"] for t in scored if t["checks"].get("overlap") is not None]
    sh_lat = [float(t.get("latency_s") or 0) for t in turns if t.get("status") in ("ok", "budget")]
    cl_lat = [float((t.get("claude") or {}).get("elapsed_s") or 0) for t in turns
              if (t.get("claude") or {}).get("elapsed_s")]
    return {
        "turns": len(turns),
        "by_status": by_status,
        "by_lane": by_lane,
        "live": {"turns": len(live), "by_outcome": live_status,
                 "writes": sum(1 for t in live for w in t.get("writes") or []
                               if w.get("executed") and w.get("result") not in ("error", "refused")),
                 "invented_times": sum(len(((t.get("checks") or {}).get("shadow") or {}).get("invented") or [])
                                       for t in live),
                 "latency_s": {"p50": _pct(live_lat, 0.5), "p95": _pct(live_lat, 0.95)},
                 "backstop": {"ok": sum(1 for t in live if (t.get("backstop") or {}).get("verdict") == "ok"),
                              "withheld": sum(1 for t in live
                                              if (t.get("backstop") or {}).get("verdict") == "withhold")}},
        "mixed": sum(1 for t in turns if t.get("mixed")),
        "invented_times": invented,
        "turns_with_invented": sum(1 for t in scored if t["checks"]["shadow"].get("invented")),
        "shadow": side_sum("shadow"),
        "claude": side_sum("claude"),
        "mode_agreement": (round(sum(1 for t in scored if t["checks"].get("mode_agree")) / len(scored), 3)
                           if scored else None),
        "option_overlap_mean": round(sum(overlaps) / len(overlaps), 3) if overlaps else None,
        "outcomes": _outcome_match(turns, _rl.read(OUTCOMES)),
        "shadow_latency_s": {"p50": _pct(sh_lat, 0.5), "p95": _pct(sh_lat, 0.95)},
        "claude_latency_s": {"p50": _pct(cl_lat, 0.5), "p95": _pct(cl_lat, 0.95)},
        "cold_starts": sum(1 for t in turns if t.get("cold_start")),
        "schema_failures": sum(int(t.get("schema_failures") or 0) for t in turns),
        "errors": by_status.get("error", 0),
        "kept_from_anthropic": sum(1 for t in turns if t.get("keep_from_anthropic")),
    }


def _report_text(r: dict) -> str:
    sh, cl = r["shadow"], r["claude"]
    lines = [
        f"Scheduling shadow: {r['turns']} turns {json.dumps(r['by_status'])}, lanes {json.dumps(r['by_lane'])}",
        f"Invented times: {r['invented_times']} (in {r['turns_with_invented']} turns)",
        f"Valid times: shadow {sh['times_valid']}/{sh['times_checked']}, "
        f"Claude {cl['times_valid']}/{cl['times_checked']}",
        f"Rule breaks: shadow {json.dumps(sh['breaks'])}, Claude {json.dumps(cl['breaks'])}",
        f"Mode agreement: {r['mode_agreement']}; option overlap: {r['option_overlap_mean']}",
        f"Outcomes: {json.dumps(r['outcomes'])}",
        f"Latency s: shadow {json.dumps(r['shadow_latency_s'])}, Claude {json.dumps(r['claude_latency_s'])}; "
        f"cold starts {r['cold_starts']}; schema failures {r['schema_failures']}; errors {r['errors']}",
    ]
    lv = r.get("live") or {}
    if lv.get("turns"):
        lines.append(f"Live turns: {lv['turns']} {json.dumps(lv['by_outcome'])}; writes {lv['writes']}; "
                     f"invented times {lv['invented_times']}; latency s {json.dumps(lv['latency_s'])}; "
                     f"backstop {json.dumps(lv.get('backstop') or {})}")
    return "\n".join(lines)


def grading_sheet(n: int = 10) -> Path:
    """A private markdown sheet of the last n answered turns, side by side."""
    turns = [t for t in _rl.read(TURNS) if t.get("status") in ("ok", "budget")][-n:]
    lines = ["# Scheduling shadow: grading sheet", "",
             "Grade each reply: send-as-is / needs-edit / wrong.", ""]
    for i, t in enumerate(turns, 1):
        claude = t.get("claude") or {}
        lines += [f"## {i}. {t.get('ts', '')} · {t.get('lane', '')} · thread {t.get('thread_ts', '')}", "",
                  "### Claude", "", str(claude.get("reply") or "(no reply)"), "",
                  "Grade: ____", "",
                  "### Shadow", "", str(t.get("reply") or "(no reply)"), ""]
        for w in t.get("writes") or []:
            lines.append(f"- would {w.get('tool')}: {json.dumps(w.get('args'), default=str)[:400]}")
        lines += ["", "Grade: ____", ""]
    path = _rl.store_dir() / GRADING_DIR / f"sched-{_now().strftime('%Y%m%d-%H%M')}.md"
    _private_write(path, "\n".join(lines) + "\n")
    return path


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run-job")
    p.add_argument("path")
    p = sub.add_parser("run-text", help="one manual turn from a typed request")
    p.add_argument("--text", required=True)
    p.add_argument("--force", action="store_true", help="run with the switch off")
    p = sub.add_parser("report")
    p.add_argument("--json", action="store_true")
    p.add_argument("--since", default=None)
    p = sub.add_parser("grading-sheet")
    p.add_argument("--n", type=int, default=10)
    a = ap.parse_args(argv)

    if a.cmd == "run-job":
        return run_job(a.path)
    if a.cmd == "run-text":
        if not a.force and not _shadow_on():
            print("the scheduling switch is off; pass --force to run anyway", file=sys.stderr)
            return 1
        job = {"run_id": f"manual-{int(time.time())}", "ts": _iso(), "lane": "manual",
               "thread_ts": "", "user_msg": a.text,
               "task_block": f"The user posted this NEW message:\n\n> {a.text}", "claude": {}}
        turn = execute_job(job)
        print(json.dumps({k: turn.get(k) for k in ("status", "latency_s", "cold_start",
                                                    "steps", "reply", "writes", "checks")},
                         indent=2, default=str))
        return 0
    if a.cmd == "report":
        since = date.fromisoformat(a.since) if a.since else None
        r = report(since)
        print(json.dumps(r, indent=2) if a.json else _report_text(r))
        return 0
    if a.cmd == "grading-sheet":
        print(grading_sheet(a.n))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
