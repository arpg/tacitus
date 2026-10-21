#!/usr/bin/env python3
"""arm_backtest: replay past scheduling turns through the on-prem arm.

The model A/B (model_arms.py) runs a topic's turns on the on-prem model
in the Claude Code harness through the tool gateway. Before live topics
draw that arm, this replays the scheduling turns Claude already took:
each fresh scheduling-channel session's first prompt (the whole lane
prompt, prompts/CLAUDE-scheduling.md with the profile, the guides and the task
block, as the bot built it that day) is run again as ea-bot on the
on-prem arm, with the read tools and the solver, and no write: the
draft and staging tools are withheld and the arm is told to end its
reply with the draft body and the event JSON it would have written.
Nothing is created, staged or sent, and the reads run under a shadow
run id.

A Claude judge (tool-less, as ea-bot) then grades the arm's turn against
Claude's recorded reply, drafts and staged events: send / edit / wrong,
the problems by kind, invented times (times the arm named that its own
solver output did not offer), whether the backstop would have withheld
it, and which side did better. The report counts those, and the arm's
own failures (an error, an empty answer, the gateway's loop guard, a
timeout), which in the live A/B become fallbacks to Claude.

The calendar has moved since those turns, so a time the arm proposes
is judged against the solver output the arm itself saw, not against
Claude's times; the mode (open proposal, poll, reschedule), the
recipients, the content and the rules are judged against Claude's.

  python3 preprocessor/arm_backtest.py run [--days 45] [--max 40] [--stamp S]
  python3 preprocessor/arm_backtest.py report --stamp S

Owner process; output under remote-llm/backtest/arm-<stamp>/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_cli as _claude_cli  # noqa: E402
import ghost_backtest as _gb  # noqa: E402
import remote_llm as _rl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EA_BOT_HOME = Path(os.environ.get("EA_BOT_HOME", "/home/ea-bot"))
EA_BOT_TREE = EA_BOT_HOME / "ea-system"
CLAUDE_BIN = str(EA_BOT_HOME / ".local" / "bin" / "claude")
SCHED_HEADER = "# Scheduling Assistant"
DEFAULT_DAYS = 45
DEFAULT_MAX = 40
USER_CAP = 400_000
TURN_TIMEOUT_S = 600
RESULT_CHARS = 6000
JUDGE_MODEL = "sonnet"
OUT_DIR = "backtest"

# The smoke set: ten past turns, one per failure shape the full replay
# found, plus three the arm handled, for a quick regression on a change.
SMOKE = [
    ("2026-09-29T19:14", "handled: event already on the calendar, graded send"),
    ("2026-09-29T19:11", "handled: event exists, graded edit"),
    ("2026-09-28T19:40", "handled: open proposal from the solver, graded edit"),
    ("2026-09-29T19:08", "solver loop: propose-meeting re-run 22 times"),
    ("2026-09-29T17:57", "search loop: 103 searches to the timeout"),
    ("2026-09-28T20:15", "search loop on a thread now buried (replay confound)"),
    ("2026-09-21T03:34", "parse miss: the call written in another form"),
    ("2026-09-15T21:47", "stale id: get-message 400 retried"),
    ("2026-09-25T01:28", "judgement: UTC read as local, window inverted"),
    ("2026-09-21T15:49", "judgement: the wrong meeting bounced"),
]

READ_VERBS = {
    "graph_actions": ("get-message", "get-conversation", "search-mail", "list-mail", "calendar-view",
                      "get-event", "resolve-contact", "get-attachments", "scan-attachments"),
    "scheduling_engine": ("*",),
    "booking_link": ("*",),
    "ledger": ("list", "get", "search", "types"),
    "style_profiles": ("get",),
    "project_store": ("pack", "match", "list", "get"),
    "google_calendar_client": ("list-events", "list-calendars", "list-ea-managed", "get-event"),
    "session_index": ("*",),
}
DISALLOWED_EXTRA = (
    "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "TodoWrite",
    "Bash(python3 *graph_actions.py create-reply *)", "Bash(python3 *graph_actions.py create-draft *)",
    "Bash(python3 *graph_actions.py update-draft *)", "Bash(python3 *graph_actions.py delete-draft *)",
    "Bash(python3 *graph_actions.py archive-message *)", "Bash(python3 *graph_actions.py snooze-message *)",
    "Bash(python3 *gmail_client.py create-draft *)", "Bash(python3 *gmail_client.py update-draft *)",
    "Bash(python3 *email_writer.py *)", "Bash(python3 *ledger.py add *)", "Bash(python3 *ledger.py update *)",
    "Bash(python3 *ledger.py close *)", "Bash(python3 *project_store.py journal *)",
    "Bash(python3 *scheduling_engine.py log-decision *)", "Bash(python3 *google_calendar_client.py create-event *)",
    "Bash(python3 *google_calendar_client.py update-event *)", "Bash(python3 *google_calendar_client.py delete-event *)",
    "Bash(python3 *graph_actions.py clear-hold *)", "Bash(python3 *poll_filler.py *)",
)

SANDBOX_NOTE = """

=== SANDBOX REPLAY (read this first) ===
This is a replay of an earlier request, for evaluation. Do everything as the instructions above say, with one change: the draft and staging tools are not available in this run (create-reply, create-draft, update-draft, the email writer, Write, pending-event.json). Read the mail and the calendar, run the scheduling engine as the instructions say, decide what you would do, and then END your reply with these two sections, exactly:
=== DRAFT ===
To: <addresses> | Cc: <addresses or none> | Reply-all or sender-only: <which>
<the exact email body you would have put in the draft, or the word none>
=== EVENT ===
<the exact JSON you would have written to pending-event.json, or the word none>
=== ARCHIVE ===
<the message ids you would have archived, or the word none>
Do not say a Send or Create button exists. Do not ask the user to run anything. Do not retry a tool that fails the same way twice; say what failed."""

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["grade", "better", "mode_agree", "withhold", "invented_times", "problems", "note", "enhancement"],
    "properties": {
        "grade": {"type": "string", "enum": ["send", "edit", "wrong"]},
        "better": {"type": "string", "enum": ["claude", "arm", "same"]},
        "mode_agree": {"type": "boolean"},
        "withhold": {"type": "boolean"},
        "invented_times": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["kind", "note"], "properties": {
                         "kind": {"type": "string", "enum": [
                             "wrong-time", "invented-time", "wrong-recipients", "wrong-mode", "missing-write",
                             "extra-write", "wrong-content", "rule-break", "wrong-name", "tool-misuse",
                             "no-answer", "other"]},
                         "note": {"type": "string"}}}},
        "note": {"type": "string"},
        "enhancement": {"type": "string"},
    },
}
JUDGE_PROMPT = """You grade one scheduling turn taken by an on-prem model (the ARM) against the turn Claude took on the same request (the REFERENCE). Both had the same instructions and the same tools; the arm could not create drafts or stage events, so it wrote what it would have drafted and staged at the end of its reply. The calendar has moved since Claude's turn, so judge the arm's TIMES against the solver output the arm itself saw (below), not against Claude's times; judge the mode (open proposal, poll answer, reschedule, confirm), the recipients, the content, the names and the rules against the reference and the request.

The arm ran in a sandbox: it could not create, update or archive anything, and a refused write (a permission message) is the sandbox, not a fault. Its DRAFT, EVENT and ARCHIVE sections stand for the writes it would have made; judge those as if made.

Grades: `send` = the arm's draft and event could go out as written; `edit` = right decision, a wording, recipient or detail fix first; `wrong` = the wrong decision, a time not in its solver output, the wrong people, a missing or extra write, or no usable answer. `withhold` = a careful reviewer would stop this before a Send button (anything a `wrong` grade names, or a time or attendee that does not check out). `better` = which side's turn serves the user better; `same` when both would.

## The request (the end of the prompt both sides got)
{request}

## REFERENCE: Claude's turn
Tools Claude ran:
{ref_tools}
Claude's reply:
{ref_reply}
Claude's draft bodies and staged events (as written; "missing" when the body came from the writer and was not captured):
{ref_writes}

## ARM: the on-prem model's turn
Outcome: {arm_status} ({arm_turns} tool steps, {arm_seconds} s)
Tools the arm ran:
{arm_tools}
Solver and calendar outputs the arm saw (trimmed):
{arm_results}
The arm's reply (with its DRAFT and EVENT sections):
{arm_reply}

Answer with one JSON object and nothing else: {{"grade": "send|edit|wrong", "better": "claude|arm|same", "mode_agree": true|false, "withhold": true|false, "invented_times": ["..."], "problems": [{{"kind": "...", "note": "..."}}], "note": "one line", "enhancement": "the one change to the arm's instructions or tools that would have fixed the worst problem, or empty"}}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def out_dir(stamp: str) -> Path:
    d = _rl.store_dir() / OUT_DIR / f"arm-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── the exchanges ─────────────────────────────────────────────────────

def collect(days: int = DEFAULT_DAYS, *, runner=None) -> list[dict]:
    """The fresh scheduling-lane turns of the last `days`: every session
    whose first prompt is the scheduling lane's, with Claude's reply,
    drafts, writes and tool commands."""
    if runner is None:
        def runner(argv):
            p = subprocess.run(["sudo", "-n", "-u", "ea-bot", "python3", "-I", "-", *argv],
                               input=_gb.EXTRACTOR, capture_output=True, text=True, timeout=600)
            if p.returncode:
                raise RuntimeError(f"collector failed (rc={p.returncode}): {p.stderr[-300:]}")
            return p.stdout
    out = runner([str(days), str(_gb.READ_OUT_CAP), str(_gb.REPLY_CAP), str(USER_CAP), str(_gb.BODY_CAP)])
    found = []
    for line in out.splitlines():
        try:
            ex = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ex, dict):
            continue
        user = str(ex.get("user") or "")
        if not user.lstrip().startswith(SCHED_HEADER):
            continue
        ex["reads"] = [{"verb": r.get("verb"), "cmd": r.get("cmd")} for r in ex.get("reads") or []]
        found.append(ex)
    return found


def _request_tail(prompt: str, chars: int = 7000) -> str:
    """The task block: what follows the tools block in the lane prompt."""
    i = prompt.rfind("The user posted")
    if i < 0:
        i = prompt.rfind("USER MESSAGE")
    tail = prompt[i:] if i >= 0 else prompt[-chars:]
    return tail[-chars:]


# ── the arm's run ─────────────────────────────────────────────────────

def allowed_tools() -> str:
    grants = ["Read"]
    for script, verbs in READ_VERBS.items():
        for v in verbs:
            grants.append(f"Bash(python3 {EA_BOT_TREE}/preprocessor/{script}.py{'' if v == '*' else ' ' + v} *)")
    return ",".join(grants)


def disallowed_tools() -> str:
    path = REPO / "scripts" / "disallowed-tools.txt"
    rows = [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")] \
        if path.exists() else []
    return ",".join(rows + list(DISALLOWED_EXTRA))


def _lane_env(i: int, variant: str = "") -> list[str]:
    import tool_gateway
    env = {"HOME": str(EA_BOT_HOME), "EA_DIR": str(EA_BOT_HOME / ".ea"),
           "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "EA_RUN_ID": f"shadow-armbt-{i}"}
    env.update(tool_gateway.lane_env(variant_name=variant))
    return [f"{k}={v}" for k, v in env.items()]


def run_arm(prompt: str, i: int, *, runner=None, timeout: float = TURN_TIMEOUT_S,
            variant: str = "") -> dict:
    """One turn on the on-prem arm, as ea-bot through the gateway, the
    prompt on stdin, writes withheld. `variant` selects the gateway's
    decoding (`think`, `greedy`, ...). Returns the reply, the tool
    commands with their results (trimmed), steps, seconds and status."""
    # claude_cli.apply adds the lane permission mode (dontAsk): a tool the
    # allow list does not name is refused, not sent to the classifier.
    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--tools", "Bash,Read,Write", "--bare", "--allowedTools", allowed_tools(),
            "--disallowedTools", disallowed_tools(),
            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config", "--no-session-persistence"]
    cmd = (["sudo", "-n", "-u", "ea-bot", "env"] + _lane_env(i, variant)
           + [f"{k}={v}" for k, v in _claude_cli.no_connector_env(args).items()]
           + [CLAUDE_BIN] + _claude_cli.apply(args, claude_bin=CLAUDE_BIN, as_ea_bot=True))
    t0 = time.monotonic()
    if runner is not None:
        rc, out, err = runner(cmd, prompt)
    else:
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                               cwd=str(EA_BOT_TREE))
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            rc, out, err = -1, (e.stdout or b"").decode(errors="ignore") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    seconds = round(time.monotonic() - t0, 1)
    tools, results, reply, pending = [], [], "", {}
    for line in (out or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    desc = str(inp.get("command") or inp.get("file_path") or json.dumps(inp))[:300]
                    tools.append(desc)
                    pending[b.get("id")] = desc
                elif b.get("type") == "text" and b.get("text"):
                    reply = str(b["text"])
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else " ".join(
                        str(x.get("text") or "") for x in (c or []) if isinstance(x, dict))
                    desc = pending.get(b.get("tool_use_id"), "?")
                    results.append({"tool": desc, "error": bool(b.get("is_error")),
                                    "text": str(text)[:RESULT_CHARS]})
        elif t == "result":
            reply = str(ev.get("result") or reply)
    status = "ok"
    if rc == -1:
        status = "timeout"
    elif rc != 0:
        status = "error"
    elif not reply.strip():
        status = "empty"
    elif reply.strip().startswith("I stopped: the same call"):
        status = "loop"
    return {"status": status, "rc": rc, "reply": reply, "tools": tools, "results": results,
            "steps": len(tools), "seconds": seconds, "stderr": (err or "")[-400:]}


# ── the judge ─────────────────────────────────────────────────────────

def _judge(prompt: str, schema: dict) -> tuple[dict, dict]:
    import ghost_week
    return ghost_week._judge(prompt, schema, judge="claude")


def _ref_writes(ex: dict) -> str:
    out = []
    for w in ex.get("writes") or []:
        path = str(w.get("path") or "")
        if "reply-body" in path or "brief" in path or "pending-event" in path:
            out.append(f"[{Path(path).name}]\n{str(w.get('content') or '')[:3000]}")
    if not out and ex.get("drafts"):
        out.append("missing (the draft was made; its body came from the writer)")
    return "\n\n".join(out) or "none"


def judge_exchange(ex: dict, arm: dict, *, judge=None) -> tuple[dict, dict]:
    judge = judge or _judge
    sol = [r for r in arm.get("results") or []
           if any(k in r["tool"] for k in ("scheduling_engine", "calendar-view", "booking_link", "google_calendar"))]
    prompt = JUDGE_PROMPT.format(
        request=_request_tail(str(ex.get("user") or "")),
        ref_tools="\n".join(f"- {d.get('cmd') or d.get('verb')}" for d in ex.get("reads") or [])[:3000] or "- (none recorded)",
        ref_reply=str(ex.get("reply") or "")[:3000] or "(empty)",
        ref_writes=_ref_writes(ex),
        arm_status=arm.get("status"), arm_turns=arm.get("steps"), arm_seconds=arm.get("seconds"),
        arm_tools="\n".join(f"- {t}" for t in arm.get("tools") or [])[:3000] or "- (none)",
        arm_results="\n\n".join(f"[{r['tool'][:120]}]{' ERROR' if r['error'] else ''}\n{r['text'][:2500]}"
                                for r in sol)[:12000] or "(none)",
        arm_reply=str(arm.get("reply") or "")[:6000] or "(empty)")
    obj, meta = judge(prompt, JUDGE_SCHEMA)
    out = {"grade": obj.get("grade") if obj.get("grade") in ("send", "edit", "wrong") else "wrong",
           "better": obj.get("better") if obj.get("better") in ("claude", "arm", "same") else "claude",
           "mode_agree": bool(obj.get("mode_agree")), "withhold": bool(obj.get("withhold")),
           "invented_times": [str(x)[:60] for x in obj.get("invented_times") or []][:10],
           "problems": [{"kind": str(p.get("kind") or "other").replace("_", "-").lower()[:24],
                         "note": str(p.get("note") or "")[:240]}
                        for p in obj.get("problems") or [] if isinstance(p, dict)][:10],
           "note": str(obj.get("note") or "")[:300], "enhancement": str(obj.get("enhancement") or "")[:300]}
    return out, meta


# ── the run ───────────────────────────────────────────────────────────

def run(days: int = DEFAULT_DAYS, max_n: int = DEFAULT_MAX, *, stamp: str = "", collector=None,
        arm_runner=None, judge=None, progress=None, variant: str = "", select: list[str] | None = None,
        parallel: int = 1, prompt: str = "lane") -> dict:
    """Replay and judge. `select` keeps the exchanges whose timestamp
    starts with one of its entries (the smoke set); `parallel` runs that
    many turns at once (the gateway holds the endpoint's slots); `prompt`
    is `lane` (the recorded prompt as Claude got it) or `short` (the
    compact prompt the on-prem arm gets, rebuilt from the recording)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M")
    d = out_dir(stamp)
    log = progress or (lambda s: None)
    exchanges = collect(days, runner=collector)
    exchanges.sort(key=lambda e: e.get("ts") or "", reverse=True)
    if select:
        exchanges = [e for e in exchanges if any(str(e.get("ts") or "").startswith(s) for s in select)]
    exchanges = exchanges[:max_n]
    done = {}
    path = d / "exchanges.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                done[(r.get("session"), r.get("ts"))] = r
            except ValueError:
                pass
    cost = [0.0]
    lock = threading.Lock()

    def one(i: int, ex: dict) -> None:
        lane_prompt = str(ex.get("user") or "")
        if prompt == "short":
            import remote_sched_prompt
            lane_prompt = remote_sched_prompt.from_lane_prompt(lane_prompt)
        log(f"{i}/{len(exchanges)} {str(ex.get('ts'))[:16]} prompt {len(lane_prompt)} chars")
        arm = run_arm(lane_prompt + SANDBOX_NOTE, i, runner=arm_runner, variant=variant)
        rec = {"session": ex.get("session"), "ts": ex.get("ts"), "variant": variant, "prompt": prompt,
               "prompt_chars": len(lane_prompt),
               "ref_reply": str(ex.get("reply") or "")[:3000],
               "ref_drafts": len(ex.get("drafts") or []), "ref_tools": len(ex.get("reads") or []),
               "arm": {k: v for k, v in arm.items() if k != "results"},
               "arm_results": [{"tool": r["tool"][:160], "error": r["error"], "text": r["text"][:1500]}
                               for r in arm.get("results") or []][:20]}
        try:
            verdict, meta = judge_exchange(ex, arm, judge=judge)
            rec["verdict"] = verdict
            with lock:
                cost[0] += float((meta or {}).get("cost_usd") or 0)
        except Exception as e:
            rec["judge_error"] = str(e)[:200]
        log(f"   {str(ex.get('ts'))[:16]} arm {arm['status']} steps={arm['steps']} {arm['seconds']}s -> "
            f"{(rec.get('verdict') or {}).get('grade', '?')} {(rec.get('verdict') or {}).get('note', '')[:90]}")
        with lock:
            with path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            done[(ex.get("session"), ex.get("ts"))] = rec

    todo = [(i, ex) for i, ex in enumerate(exchanges, 1) if (ex.get("session"), ex.get("ts")) not in done]
    if parallel > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=int(parallel)) as pool:
            list(pool.map(lambda t: one(*t), todo))
    else:
        for i, ex in todo:
            one(i, ex)
    rep = summarize(list(done.values()), stamp=stamp, collected=len(exchanges), cost=cost[0])
    (d / "report.json").write_text(json.dumps(rep, indent=1))
    (d / "report.md").write_text(_report_md(rep, list(done.values())))
    rep["path"] = str(d / "report.md")
    return rep


def summarize(rows: list[dict], *, stamp: str = "", collected: int = 0, cost: float = 0.0) -> dict:
    judged = [r for r in rows if r.get("verdict")]
    def count(key, items):
        out = {}
        for it in items:
            out[it] = out.get(it, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
    problems = count("kind", [p["kind"] for r in judged for p in r["verdict"]["problems"]])
    secs = sorted(float(r["arm"]["seconds"]) for r in rows)
    return {
        "stamp": stamp, "collected": collected, "replayed": len(rows), "judged": len(judged),
        "judge_cost_usd": round(cost, 3),
        "arm_status": count("s", [r["arm"]["status"] for r in rows]),
        "grades": count("g", [r["verdict"]["grade"] for r in judged]),
        "better": count("b", [r["verdict"]["better"] for r in judged]),
        "withhold": sum(1 for r in judged if r["verdict"]["withhold"]),
        "mode_agree": sum(1 for r in judged if r["verdict"]["mode_agree"]),
        "invented_times": sum(len(r["verdict"]["invented_times"]) for r in judged),
        "turns_with_invented": sum(1 for r in judged if r["verdict"]["invented_times"]),
        "problems": problems,
        "steps_mean": round(sum(r["arm"]["steps"] for r in rows) / len(rows), 1) if rows else None,
        "seconds_p50": secs[len(secs) // 2] if secs else None,
        "seconds_p95": secs[min(len(secs) - 1, int(0.95 * (len(secs) - 1) + 0.5))] if secs else None,
    }


def _report_md(rep: dict, rows: list[dict]) -> str:
    lines = [f"# Arm backtest {rep['stamp']}", "",
             f"Replayed {rep['replayed']} fresh scheduling-lane turns (of {rep['collected']} collected) on the "
             f"on-prem arm; judged {rep['judged']}; judge cost ${rep['judge_cost_usd']}.", "",
             f"- Arm outcomes: {json.dumps(rep['arm_status'])} (anything but ok is a fallback to Claude in the live A/B)",
             f"- Grades: {json.dumps(rep['grades'])}; better: {json.dumps(rep['better'])}; "
             f"mode agreement {rep['mode_agree']}/{rep['judged']}",
             f"- Backstop would withhold: {rep['withhold']}/{rep['judged']}; invented times {rep['invented_times']} "
             f"(in {rep['turns_with_invented']} turns)",
             f"- Problems by kind: {json.dumps(rep['problems'])}",
             f"- Steps mean {rep['steps_mean']}; seconds p50 {rep['seconds_p50']}, p95 {rep['seconds_p95']}", "",
             "## Turns", ""]
    for r in sorted(rows, key=lambda r: r.get("ts") or ""):
        v = r.get("verdict") or {}
        lines.append(f"### {str(r.get('ts'))[:16]} · {r['arm']['status']} · {v.get('grade', '?')} · better {v.get('better', '?')}"
                     f"{' · WITHHOLD' if v.get('withhold') else ''}")
        lines.append(f"- {v.get('note', r.get('judge_error', ''))}")
        for p in v.get("problems") or []:
            lines.append(f"- {p['kind']}: {p['note']}")
        if v.get("invented_times"):
            lines.append(f"- invented times: {', '.join(v['invented_times'])}")
        if v.get("enhancement"):
            lines.append(f"- enhancement: {v['enhancement']}")
        lines.append(f"- arm: {r['arm']['steps']} steps, {r['arm']['seconds']} s; tools: "
                     + "; ".join(t[:70] for t in r['arm'].get('tools') or [])[:600])
        lines.append("")
    enh = {}
    for r in rows:
        e = (r.get("verdict") or {}).get("enhancement")
        if e:
            enh[e] = enh.get(e, 0) + 1
    if enh:
        lines += ["## Enhancements named", ""] + [f"- {e}" for e, _n in sorted(enh.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def compare(stamps: list[str]) -> str:
    """One line per turn across runs: status, steps, seconds and grade."""
    runs = {}
    for st in stamps:
        p = out_dir(st) / "exchanges.jsonl"
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
        runs[st] = {str(r.get("ts"))[:16]: r for r in rows}
    ts_all = sorted({ts for rows in runs.values() for ts in rows})
    width = max(len(s) for s in stamps) if stamps else 8
    lines = ["turn             " + "  ".join(f"{s:<{max(width, 26)}}" for s in stamps)]
    tallies = {s: {"ok": 0, "send": 0, "edit": 0, "wrong": 0, "n": 0} for s in stamps}
    for ts in ts_all:
        cells = []
        for st in stamps:
            r = runs[st].get(ts)
            if not r:
                cells.append(f"{'-':<{max(width, 26)}}")
                continue
            v = r.get("verdict") or {}
            g = v.get("grade", "?")
            t = tallies[st]
            t["n"] += 1
            t["ok"] += 1 if r["arm"]["status"] == "ok" else 0
            if g in t:
                t[g] += 1
            cell = f"{r['arm']['status']:7s} {r['arm']['steps']:3d}st {r['arm']['seconds']:5.0f}s {g}"
            cells.append(f"{cell:<{max(width, 26)}}")
        lines.append(f"{ts}  " + "  ".join(cells))
    lines.append("tally            " + "  ".join(
        f"{'ok ' + str(t['ok']) + '/' + str(t['n']) + ' send ' + str(t['send']) + ' edit ' + str(t['edit']) + ' wrong ' + str(t['wrong']):<{max(width, 26)}}"
        for s, t in tallies.items()))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run")
    s.add_argument("--days", type=int, default=DEFAULT_DAYS)
    s.add_argument("--max", type=int, default=DEFAULT_MAX)
    s.add_argument("--stamp", default="")
    s.add_argument("--variant", default="", help="gateway variant suffixes, e.g. think or greedy")
    s.add_argument("--smoke", action="store_true", help="the ten-turn smoke set (SMOKE)")
    s.add_argument("--select", default="", help="comma-separated timestamp prefixes to replay")
    s.add_argument("--parallel", type=int, default=1)
    s.add_argument("--prompt", choices=("lane", "short"), default="lane",
                   help="lane = the recorded prompt; short = the on-prem arm's compact prompt")
    s = sub.add_parser("compare", help="turns side by side across runs")
    s.add_argument("--stamps", required=True, help="comma-separated stamps")
    s = sub.add_parser("report")
    s.add_argument("--stamp", required=True)
    s = sub.add_parser("collect")
    s.add_argument("--days", type=int, default=DEFAULT_DAYS)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        ex = collect(a.days)
        print(json.dumps({"found": len(ex), "ts": [e.get("ts") for e in ex],
                          "prompt_chars": [len(str(e.get("user") or "")) for e in ex],
                          "with_drafts": sum(1 for e in ex if e.get("drafts"))}))
        return 0
    if a.cmd == "run":
        select = [t for t, _why in SMOKE] if a.smoke else [x.strip() for x in a.select.split(",") if x.strip()]
        rep = run(a.days, a.max, stamp=a.stamp, variant=a.variant, select=select or None, parallel=a.parallel,
                  prompt=a.prompt, progress=lambda s: print(s, file=sys.stderr, flush=True))
        print(json.dumps({k: v for k, v in rep.items() if k != "path"}, indent=1))
        print(f"Report: {rep['path']}", file=sys.stderr)
        return 0
    if a.cmd == "compare":
        print(compare([x.strip() for x in a.stamps.split(",") if x.strip()]))
        return 0
    if a.cmd == "report":
        d = out_dir(a.stamp)
        rows = [json.loads(l) for l in (d / "exchanges.jsonl").read_text().splitlines() if l.strip()]
        rep = summarize(rows, stamp=a.stamp, collected=len(rows))
        print(_report_md(rep, rows))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
