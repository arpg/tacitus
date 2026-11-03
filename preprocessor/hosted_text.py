#!/usr/bin/env python3
"""One tool-less hosted call, on Claude or on Codex.

Three routing tasks make a tool-less call to a hosted model:
`thread_state` (web_thread_state: the web chat's conversation states),
`review` (remote_briefing: the briefing's still-open check;
ledger_reconcile: the nightly reconcile; sched_agent: the backstop on
local scheduling writes) and `tagging` (mail_tagger: Outlook categories).
Each one asks `model_for(task)` which hosted model the owner's routing
table names for it (model_routing.hosted_model; Claude unless the table
says Codex) and runs it:

  claude  mail_privacy._run_claude(prompt, model, "") as the resident,
          as before (the tagger keeps its own owner-side Claude run);
  codex   `codex exec` with every tool off (CODEX_OFF: the shell, the
          apps, the plugins, the hooks, the image viewer), read-only,
          ephemeral, on the account's own model. As the resident by
          default, in an empty working folder of its own (TEXT_CWD); with
          `as_owner` as the owner, which the tagger needs: its prompt
          carries the mail text, which never enters the resident's space.

A Codex run that gives no answer (a harness error, a timeout, an empty
reply) hands the call to Claude once (`run`), and the result says so
(`fell_back`), so a night's reconcile or a tick's states never stop on
Codex. The result is _run_claude's shape: {"text", "cost_usd", "rc",
"stderr"}, plus "model_used" (claude | codex) and "fell_back".

    hosted_text.py probe [--as-owner]   one tool-less Codex call; exit 0 on an answer
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Every Codex tool off: a tool-less call reads its prompt and answers, as
# a Claude run with `--tools ""` does. With the shell off the model runs
# no command, so even the owner's run reads nothing the prompt does not
# carry.
CODEX_OFF = ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "view_image")
# The line before the prompt: it has no tools, and its answer is the text.
CODEX_PREAMBLE = ("Answer from the text below alone. You have no tools: run nothing and read "
                  "nothing. Reply with the answer the text asks for and nothing else.")
CODEX_TIMEOUT_S = 600


def model_for(task: str) -> str:
    """`claude` or `codex`: the hosted model the routing table names for
    `task`; `claude` when the table cannot be read."""
    try:
        import model_routing
        return model_routing.hosted_model(task)
    except Exception as e:
        print(f"hosted_text: the routing table is unread, {task} runs on Claude: {e}", file=sys.stderr)
        return "claude"


def _resident():
    import codex_ghost
    return codex_ghost


def text_cwd() -> Path:
    """The resident's empty working folder for tool-less Codex calls:
    Codex cannot start in the read-only deployed tree, and in the
    resident's home it would see every file there."""
    return _resident().BOT_HOME / ".ea" / "state" / "codex-text"


def _ensure_text_cwd(runner=None) -> None:
    """Make TEXT_CWD as the resident, mode 750 like the rest of the state
    (this process starts Codex from inside it, through the group); never
    raises."""
    cmd = _resident()._bot_env() + ["install", "-d", "-m", "750", str(text_cwd())]
    run = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=30))
    try:
        run(cmd)
    except Exception:
        pass


def owner_binary() -> str:
    """The owner's codex: EA_CODEX_BIN or `codex` on PATH, else the newest
    one an nvm install put under ~/.nvm (cron's PATH has none); '' when
    there is none."""
    import codex_cli
    found = codex_cli.binary()
    if found:
        return found
    paths = sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "codex")))
    return paths[-1] if paths else ""


def codex(prompt: str, *, timeout: float = CODEX_TIMEOUT_S, as_owner: bool = False,
          runner=None, scratch_runner=None) -> dict:
    """One tool-less Codex call. Returns {"text", "cost_usd", "rc",
    "stderr", "usage"}: rc 0 with the text on an answer, else rc 1 and
    the reason in stderr."""
    import codex_cli
    run = runner or codex_cli.run
    text = f"{CODEX_PREAMBLE}\n\n{prompt}"
    kw = {"sandbox": "read-only", "timeout": int(timeout), "ephemeral": True, "disable": CODEX_OFF}
    if as_owner:
        exe = owner_binary()
        if not exe:
            return {"text": "", "cost_usd": None, "rc": 127, "stderr": "codex not found for the owner",
                    "usage": {}}
        # An nvm install's codex is a node script: its own folder holds node.
        env = {**os.environ, "PATH": f"{Path(exe).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        with tempfile.TemporaryDirectory(prefix="ea-codex-text-") as td:
            res = run(text, cwd=td, codex_bin=exe, env=env, **kw)
    else:
        g = _resident()
        _ensure_text_cwd(runner=scratch_runner)
        res = run(text, cwd=text_cwd(), prefix=g._bot_env({"EA_BOT_HOME": str(g.BOT_HOME)}),
                  last_message_dir=g.BOT_HOME / ".local" / "state" / "ea-lanes", **kw)
    err = res.get("harness_error") or ""
    if not err and res.get("tool_calls"):
        err = "codex ran a tool in a tool-less call"
    out = str(res.get("output") or "").strip()
    if err or not out:
        return {"text": "", "cost_usd": None, "rc": 1,
                "stderr": (err or "codex gave no answer")[-300:], "usage": res.get("usage") or {}}
    return {"text": out, "cost_usd": None, "rc": 0, "stderr": "", "usage": res.get("usage") or {}}


def run(task: str, prompt: str, model: str, *, timeout: float | None = None,
        claude=None, codex_call=None) -> dict:
    """One tool-less call for `task` on the hosted model the table names:
    `model` is the Claude model, which a Codex run does not use (it runs
    on the account's own). A Codex run with no answer hands the call to
    Claude once."""
    if claude is None:
        import mail_privacy

        def claude(p, m, **kw):
            return mail_privacy._run_claude(p, m, "", **kw)
    ckw = {"timeout": timeout} if timeout else {}
    if model_for(task) == "codex":
        r = (codex_call or codex)(prompt, **({"timeout": timeout} if timeout else {}))
        if not r.get("rc") and r.get("text"):
            return {**r, "model_used": "codex", "fell_back": ""}
        why = str(r.get("stderr") or "no answer")[:200]
        print(f"hosted_text: Codex did not answer for {task} ({why}); Claude answers", file=sys.stderr)
        out = claude(prompt, model, **ckw)
        return {**out, "model_used": "claude", "fell_back": why}
    return {**claude(prompt, model, **ckw), "model_used": "claude", "fell_back": ""}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="one tool-less Codex call; exit 0 on an answer")
    p.add_argument("--as-owner", action="store_true")
    a = ap.parse_args(argv)
    r = codex('Reply with exactly the word OK and nothing else.', timeout=180, as_owner=a.as_owner)
    print(r["text"] or r["stderr"])
    return 0 if r["rc"] == 0 and "OK" in r["text"].upper() else 1


if __name__ == "__main__":
    raise SystemExit(main())
