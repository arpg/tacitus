#!/usr/bin/env python3
"""The one builder for a non-interactive Codex run (`codex exec`), the
Codex twin of claude_cli for the lanes and tests that can run on either
harness.

Codex loads no CLAUDE.md, so a prompt built for it carries the operating
instructions itself (prompt_logic.build_claude_md with nothing
discovered). It runs model commands in its own sandbox: `read-only`
reads anywhere and writes nowhere; `workspace-write` writes only under
the working root (`-C`) and the `--add-dir` directories. A lane that
must write the EA state passes that directory, never the code tree.
Network is off inside the sandbox.

    codex_cli.py probe            exit 0 when codex answers a trivial prompt
    codex_cli.py version          the installed version, or exit 1

`run()` returns the same shape provider._invoke_claude returns, so an
eval or a release case reads either harness's result the same way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CODEX_BIN = os.environ.get("EA_CODEX_BIN", "codex")
PROBE_TIMEOUT_S = 120
SANDBOXES = ("read-only", "workspace-write")


def binary() -> str | None:
    """The codex binary on PATH (or EA_CODEX_BIN), None when absent."""
    return shutil.which(CODEX_BIN)


def version() -> str:
    b = binary()
    if not b:
        return ""
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip()


def build(*, cwd, model: str = "", sandbox: str = "read-only", add_dirs=(),
          last_message_file=None, ephemeral: bool = True) -> list[str]:
    """The command line for one run. The prompt goes on stdin (`-`), so
    no instruction text ever sits in a process listing."""
    if sandbox not in SANDBOXES:
        raise ValueError(f"sandbox must be one of {SANDBOXES}")
    cmd = [binary() or CODEX_BIN, "exec", "--skip-git-repo-check", "--color", "never",
           "--json", "--sandbox", sandbox, "-C", str(cwd)]
    if ephemeral:
        cmd.append("--ephemeral")
    if model:
        cmd += ["-m", model]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if last_message_file:
        cmd += ["-o", str(last_message_file)]
    cmd.append("-")
    return cmd


def parse_events(stdout: str) -> dict:
    """What the JSONL event stream says: the agent's messages in order,
    the commands it ran, the files it changed, the usage, and any error
    items that are not the enterprise-config notices codex prints on
    every run."""
    messages, tool_calls, files, errors = [], [], [], []
    usage: dict = {}
    turns = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "turn.completed":
            turns += 1
            usage = ev.get("usage") or usage
            continue
        if t != "item.completed":
            continue
        item = ev.get("item") or {}
        kind = item.get("type")
        if kind == "agent_message":
            messages.append(str(item.get("text") or ""))
        elif kind == "command_execution":
            tool_calls.append({"tool": "Bash", "input": {"command": str(item.get("command") or "")},
                               "exit_code": item.get("exit_code"),
                               "output": str(item.get("aggregated_output") or "")[-2000:]})
        elif kind == "file_change":
            files += [str(c.get("path") or "") for c in item.get("changes") or []]
        elif kind == "error":
            msg = str(item.get("message") or "")
            if "requirements layers" not in msg:
                errors.append(msg)
    return {"messages": messages, "tool_calls": tool_calls, "files": files,
            "usage": usage, "turns": turns, "errors": errors}


def run(prompt: str, *, cwd, model: str = "", sandbox: str = "read-only", add_dirs=(),
        timeout: int = 600, env: dict | None = None, prefix=(), last_message_dir=None) -> dict:
    """One non-interactive run. Returns {"output", "tool_calls",
    "exit_code", "duration_s", "harness_error", "num_turns", "usage",
    "files", "stderr"}: `output` is the agent's final message (the `-o`
    file, else the last agent_message event); `harness_error` names why
    the run was not measured (no binary, a non-zero exit with no message,
    a timeout, an error item).

    `prefix` goes before the command (`sudo -u ea-bot env ...` runs codex
    as the resident; the binary is then the resident's, found on the PATH
    the prefix sets, so the owner's `binary()` check is skipped);
    `last_message_dir` is where the `-o` file goes, a directory that user
    can write (the owner's temp dir is not)."""
    prefix = list(prefix or ())
    b = binary()
    if not b and not prefix:
        return {"output": "", "tool_calls": [], "exit_code": 127, "duration_s": 0.0,
                "harness_error": f"codex not found ({CODEX_BIN})", "num_turns": 0,
                "usage": {}, "files": [], "stderr": ""}
    # The -o file: the owner's temp dir when codex runs as the owner; a file
    # under `last_message_dir` (which the run's user can write and this
    # process can read) when a prefix runs it as another user.
    td_ctx = None if last_message_dir else tempfile.TemporaryDirectory(prefix="ea-codex-")
    try:
        if last_message_dir:
            last = Path(last_message_dir) / f"last-message-{os.getpid()}-{int(time.time())}.txt"
        else:
            last = Path(td_ctx.name) / "last-message.txt"
        cmd = build(cwd=cwd, model=model, sandbox=sandbox, add_dirs=add_dirs,
                    last_message_file=last)
        if prefix:
            cmd = prefix + [CODEX_BIN] + cmd[1:]
        start = time.time()
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=timeout, cwd=str(cwd) if not prefix else None, env=env)
        except subprocess.TimeoutExpired:
            return {"output": "", "tool_calls": [], "exit_code": -1, "duration_s": float(timeout),
                    "harness_error": f"codex timed out after {timeout}s", "num_turns": 0,
                    "usage": {}, "files": [], "stderr": ""}
        except OSError as e:
            return {"output": "", "tool_calls": [], "exit_code": 126, "duration_s": 0.0,
                    "harness_error": f"codex could not start: {e}", "num_turns": 0,
                    "usage": {}, "files": [], "stderr": ""}
        duration = time.time() - start
        parsed = parse_events(r.stdout)
        output = ""
        try:
            output = last.read_text().strip()
        except OSError:
            pass
        if not output and parsed["messages"]:
            output = parsed["messages"][-1].strip()
    finally:
        if td_ctx is not None:
            td_ctx.cleanup()
    # An error item ends the run only when no final message came out of
    # it: codex also reports advisories as error items (unknown model
    # metadata, a fallback), and a run that still answered was measured.
    harness_error = ""
    if not output:
        if parsed["errors"]:
            harness_error = "codex error: " + "; ".join(parsed["errors"])[:300]
        elif r.returncode != 0:
            harness_error = f"codex exit {r.returncode}: {(r.stderr or '').strip()[-300:]}"
        else:
            harness_error = "codex produced no final message"
    return {"output": "" if harness_error else output, "tool_calls": parsed["tool_calls"],
            "exit_code": r.returncode, "duration_s": duration, "harness_error": harness_error,
            "warnings": parsed["errors"] if output else [],
            "num_turns": parsed["turns"], "usage": parsed["usage"], "files": parsed["files"],
            "stderr": (r.stderr or "")[-500:]}


def probe(timeout: int = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """(ok, detail): whether codex answers a trivial prompt, read-only, in
    an empty directory. The detail names the version or the failure."""
    if not binary():
        return False, f"codex not on PATH ({CODEX_BIN})"
    with tempfile.TemporaryDirectory(prefix="ea-codex-probe-") as td:
        res = run("Reply with exactly the word OK and nothing else.", cwd=td,
                  sandbox="read-only", timeout=timeout)
    if res["harness_error"]:
        return False, res["harness_error"]
    if "OK" not in res["output"].upper():
        return False, f"unexpected answer: {res['output'][:80]!r}"
    return True, version() or "codex"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["probe"]:
        ok, detail = probe()
        print(json.dumps({"ok": ok, "detail": detail}))
        return 0 if ok else 1
    if argv[:1] == ["version"]:
        v = version()
        print(v or "codex not found")
        return 0 if v else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
