#!/usr/bin/env python3
"""Tests for ea-chat-bot.py's scheduler trace + dump-file helpers.

The helpers consult `args_full`, the un-truncated args dict. Reading
`args_summary` instead silently no-ops, because that value is truncated
at 60 chars per value and cuts off a subcommand name like
`propose-meeting`.

Slack-bot tests run via the inline-exec-helper-block pattern below to
avoid importing the full ea-chat-bot.py module (which pulls slack_bolt
at module load).

Run:
    python3 tests/test_scheduler_trace.py
"""

import json
import re
import sys
import tempfile
import traceback
import types
from pathlib import Path


def _load_helpers():
    """Exec the trace+dump helper block out of ea-chat-bot.py into an
    isolated namespace. Returns the namespace dict."""
    src_path = Path(__file__).resolve().parent.parent / "ea-chat-bot.py"
    src = src_path.read_text()
    start = src.index("_SCHEDULER_TOOL_PATTERNS = [")
    end = src.index("\n\n\n# Track in-flight requests", start)
    helper_src = src[start:end]
    ns = {
        "re": re, "json": json, "Path": Path,
        "log": types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
        # mkdtemp, not a fixed name: a fixed world-writable /tmp path could
        # be pre-created (or symlinked) by another local user.
        "EA_DIR": Path(tempfile.mkdtemp(prefix="ea-test-scheduler-trace-")),
    }
    exec(compile(helper_src, str(src_path), "exec"), ns)
    return ns


HELPERS = _load_helpers()


# --------------------------------------------------------------------------
# Production-shape truncation
# --------------------------------------------------------------------------

def _production_shape_tc(command_str, result_snippet='{"status":"ok"}',
                         elapsed=1.0):
    """Build a tool_calls 5-tuple in the exact shape _run_claude_streaming
    produces: args_summary truncated 60 chars per value, full args dict
    preserved.

    Storing only the truncated summary severs the subcommand name from
    `Bash(command=python3 …/scheduling_engine.py propose-meeting …)`.
    """
    full = {"command": command_str, "description": "test description"}
    summary = " ".join(f"{k}={str(v)[:60]}" for k, v in full.items())[:150]
    return ("Bash", elapsed, summary, result_snippet, full)


def test_identify_scheduler_call_with_truncated_summary():
    """The args_summary truncates BEFORE the subcommand name. The bot
    must still identify the call as propose-meeting via args_full."""
    cmd = (
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py "
        "propose-meeting --duration 60 --days-ahead 40 --num-options 30 "
        "--attendees '[{\"email\":\"poll@x\",\"name\":\"Ava\",\"type\":\"external\"}]'"
    )
    tc = _production_shape_tc(cmd, '{"options":[{"rank":1},{"rank":2}]}')

    # Sanity: verify the truncated summary actually loses the subcommand
    summary = tc[2]
    assert "scheduling_engine.py propose-meeting" not in summary, (
        "test precondition failed: truncated summary should have cut the "
        "subcommand name. Got: " + repr(summary)
    )

    ident = HELPERS["_identify_scheduler_call"](tc)
    assert ident is not None, (
        "_identify_scheduler_call returned None for a real propose-meeting "
        "call. THIS IS THE 2026-05-23 BUG. Helpers must consult args_full."
    )
    label, kv = ident
    assert label == "propose-meeting"
    assert kv["duration"] == "60"
    assert kv["days-ahead"] == "40"
    assert kv["num-options"] == "30"
    assert kv["attendees"] == "1"


def test_identify_scheduler_call_validate_draft():
    """validate-draft on a specific time — same truncation issue."""
    cmd = (
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py "
        "validate-draft --times '[\"2026-05-26T09:30:00-06:00\"]' --duration 30"
    )
    tc = _production_shape_tc(cmd, '{"valid": true}')
    ident = HELPERS["_identify_scheduler_call"](tc)
    assert ident is not None
    assert ident[0] == "validate-draft"


def test_identify_scheduler_call_non_scheduler_returns_none():
    """A Bash call that ISN'T a scheduler tool must return None — the
    trace footer is meaningful only because non-scheduling exchanges
    don't get it."""
    cmd = (
        "python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py "
        "search-mail --query 'subject:foo'"
    )
    tc = _production_shape_tc(cmd, "[]")
    assert HELPERS["_identify_scheduler_call"](tc) is None


def test_identify_scheduler_call_legacy_4tuple_no_full_args():
    """During a rolling deploy old code paths may still emit 4-tuples
    (no args_full). The helper must gracefully return None rather than
    crash, even if the summary happens to contain the substring."""
    # 4-tuple — no full args dict
    tc = ("Bash", 1.0, "command=python3 /home/ea-bot/ea-system/preprocessor/scheduling_engin", "{}")
    # Should not crash. Returns None because the summary is truncated
    # below the subcommand name.
    assert HELPERS["_identify_scheduler_call"](tc) is None


def test_format_scheduler_trace_with_production_shape():
    """End-to-end: production-shape calls produce a non-empty
    trace footer."""
    cmd = (
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py "
        "propose-meeting --duration 30 --days-ahead 7 --num-options 3"
    )
    tc = _production_shape_tc(
        cmd, '{"options":[{"rank":1},{"rank":2},{"rank":3}]}'
    )
    trace = HELPERS["_format_scheduler_trace"]([tc])
    assert trace is not None, "trace should not be None"
    assert "─────" in trace
    assert ":calendar: propose-meeting" in trace
    assert "3 options" in trace


def test_the_summary_comes_from_the_whole_result():
    """A chat turn keeps 300 characters of each result, which cuts a
    solver answer off before its second option. The capture stores the
    summary of the whole result as a sixth element, and the footer uses it."""
    cmd = ("python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py "
           "propose-meeting --duration 30 --num-options 5")
    full = ("  Google Calendar a@example.com: 33 events\n  EA-Managed calendar: 9 holds\n"
            + json.dumps({"status": "ok", "options": [
                {"rank": i, "notes": "x" * 400} for i in range(1, 6)]}))
    summary = HELPERS["_summarize_scheduler_result"]("propose-meeting", full)
    assert summary == "5 options", summary
    tc = _production_shape_tc(cmd, full[:300]) + (summary,)
    assert "→ 5 options" in HELPERS["_format_scheduler_trace"]([tc])


def test_a_help_call_is_not_a_solver_run():
    cmd = ("python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py "
           "propose-meeting --help")
    assert HELPERS["_identify_scheduler_call"](_production_shape_tc(cmd)) is None


def test_booking_link_propose_is_traced():
    cmd = ("python3 /home/ea-bot/ea-system/preprocessor/booking_link.py propose "
           "--attendees '[{\"email\":\"a@example.edu\",\"booking_link\":\"https://calendly.com/a/30min\"}]'")
    trace = HELPERS["_format_scheduler_trace"](
        [_production_shape_tc(cmd, '{"options":[{"rank":1},{"rank":2}]}')])
    assert ":calendar: booking-propose (attendees=1) → 2 options" in trace, trace


def test_format_scheduler_trace_skips_when_no_scheduling_tool():
    """Replies that didn't touch the scheduler get no footer."""
    cmd = (
        "python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py "
        "search-mail --query foo"
    )
    tc = _production_shape_tc(cmd, "[]")
    assert HELPERS["_format_scheduler_trace"]([tc]) is None
    assert HELPERS["_format_scheduler_trace"]([]) is None


def test_format_scheduler_trace_multiple_calls_in_order():
    """When several scheduler tools fire in one exchange, the footer
    shows them all in invocation order."""
    cmds = [
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py validate-draft --times '[\"2026-05-26T09:30:00-06:00\"]' --duration 30",
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting --duration 60 --num-options 3",
        "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py log-decision --event foo --action kept",
    ]
    results = ['{"valid":true}', '{"options":[{"rank":1}]}', '{"status":"ok"}']
    tcs = [_production_shape_tc(c, r) for c, r in zip(cmds, results)]
    trace = HELPERS["_format_scheduler_trace"](tcs)
    assert trace
    lines = [ln for ln in trace.splitlines() if ln.startswith(":calendar:")]
    assert len(lines) == 3
    assert "validate-draft" in lines[0]
    assert "propose-meeting" in lines[1]
    assert "log-decision" in lines[2]


# --------------------------------------------------------------------------
# Standalone runner
# --------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
