"""The bot's claude runner (_run_claude_streaming), run from the bot's own
source against a fake claude.

ea-chat-bot.py is not importable, so the runner, ClaudeResult and the two
process helpers are compiled out of the source into a namespace whose
_sudo_claude_cmd runs FAKE below instead of sudo + claude. The fake
reports what reached it: the argument vector and the prompt on stdin.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

from preprocessor import turn_watch  # noqa: E402

FAKE = textwrap.dedent("""
    import json, os, sys, time
    open(os.environ["FAKE_PID"], "w").write(str(os.getpid()))
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_SILENT"):
        time.sleep(60)
    print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1,
                      "result": json.dumps({"argv": sys.argv[1:], "prompt_len": len(prompt),
                                            "tail": prompt[-12:]})}))
""")


def _src(start: str, end: str) -> str:
    i = BOT_SRC.index(start)
    return BOT_SRC[i:BOT_SRC.index(end, i + len(start))]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE)
    monkeypatch.setenv("FAKE_PID", str(tmp_path / "pid"))

    class _Journal:
        @staticmethod
        def gmail_ids_in(text):
            return []

    ns = {"json": json, "os": os, "subprocess": subprocess, "threading": threading,
          "time": time, "dataclass": dataclass, "field": field,
          "log": logging.getLogger("test-claude-runner"),
          "EA_BOT_SCRIPT_DIR": tmp_path, "_draft_events": _Journal,
          "_turn_watch": turn_watch, "STALL_SECONDS": 600,
          "_is_gmail_draft_tool": lambda *a: False,
          "_NOT_LOGGED_IN": "Not logged in", "_LOGIN_LOST_MARKERS": ("Not logged in",),
          "_login_lost_message": lambda: "login lost",
          "_sudo_claude_cmd": lambda args, run_id="", extra_env=None: [sys.executable, str(fake), *args]}
    code = (_src("@dataclass\nclass ClaudeResult", "\n\n\n_GMAIL_DRAFT_TOOLS")
            + "\n\n" + _src("def _run_claude_streaming(", "\n# ----"))
    exec(compile(code, "ea-chat-bot.py[runner]", "exec"), ns)
    return ns, tmp_path


def _args(prompt):
    return ["-p", prompt, "--model", "haiku", "--output-format", "json"]


def test_the_prompt_goes_in_on_stdin_not_the_argument_vector(runner):
    ns, _ = runner
    cr = ns["_run_claude_streaming"](_args("hello there"), timeout=30, label="t")
    seen = json.loads(cr.output)
    assert seen["prompt_len"] == len("hello there") and seen["tail"] == "hello there"
    assert "hello there" not in seen["argv"]
    assert seen["argv"][:3] == ["-p", "--model", "haiku"]
    assert "stream-json" in seen["argv"] and not cr.timed_out and cr.returncode == 0


def test_a_prompt_over_the_argument_limit_still_runs(runner):
    # Linux caps one argument at 128 KiB (MAX_ARG_STRLEN); as `-p <prompt>`
    # this one would fail with E2BIG before the child started.
    ns, _ = runner
    big = "x" * 300_000 + "END-OF-BRIEF"
    cr = ns["_run_claude_streaming"](_args(big), timeout=30, label="t")
    seen = json.loads(cr.output)
    assert seen["prompt_len"] == len(big) and seen["tail"] == big[-12:]


def test_a_silent_run_is_stopped_at_its_deadline(runner, monkeypatch):
    # No output line ever arrives, so only the watchdog can end the run.
    ns, tmp = runner
    monkeypatch.setenv("FAKE_SILENT", "1")
    t0 = time.monotonic()
    cr = ns["_run_claude_streaming"](_args("wait"), timeout=1, label="t")
    assert cr.timed_out and cr.returncode == -1
    assert time.monotonic() - t0 < 15
    pid = int((tmp / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_quiet_stream_stops_the_run_as_a_stall_before_its_ceiling(runner, monkeypatch):
    # The ceiling is far off; the stall clock ends a run that prints nothing.
    ns, tmp = runner
    monkeypatch.setenv("FAKE_SILENT", "1")
    monkeypatch.setattr(turn_watch, "TICK_S", 0.2)
    t0 = time.monotonic()
    cr = ns["_run_claude_streaming"](_args("wait"), timeout=600, label="t", stall=1)
    assert cr.timed_out and cr.stalled and cr.returncode == -1
    assert cr.output == "No output for 1s"
    assert time.monotonic() - t0 < 15
    pid = int((tmp / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_run_that_cannot_start_is_an_error_not_a_timeout(runner):
    ns, _ = runner
    ns["_sudo_claude_cmd"] = lambda args, run_id="", extra_env=None: ["/nonexistent/claude", *args]
    cr = ns["_run_claude_streaming"](_args("hi"), timeout=5, label="t")
    assert cr.returncode == -1 and not cr.timed_out and cr.output.startswith("Error:")


def test_timeouts_are_sized_for_room_turns():
    # A registration and the event sweeper's busy age outlast a room turn.
    for line in ("STALL_SECONDS = 600", "TIMEOUT_SECONDS = 3600", "ROOM_TIMEOUT_SECONDS = 3600",
                 "DISPATCH_TTL_S = ROOM_TIMEOUT_SECONDS + TIMEOUT_SECONDS + 300",
                 "EVENT_SWEEP_BUSY_AGE_S = ROOM_TIMEOUT_SECONDS + 300"):
        assert f"\n{line}\n" in BOT_SRC, line
    body = _src("def handle_message_async(", "\ndef ")
    assert "run_timeout = ROOM_TIMEOUT_SECONDS" in body
    # The resume, the fresh session, the session minted after a failed
    # resume, and the Claude rerun of a remote-arm turn (model_arms).
    assert body.count('timeout=run_timeout, label="mono"') == 4
    assert "if cr.timed_out:\n            raise subprocess.TimeoutExpired" in body
