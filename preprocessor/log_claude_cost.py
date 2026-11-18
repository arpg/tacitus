#!/usr/bin/env python3
"""
Pass-through cost logger for `claude -p --output-format stream-json --verbose`.

Reads stream-json events on stdin, writes the final assistant text on stdout
(so the rest of the orchestrator keeps working unchanged), and appends a
one-line cost record per invocation to ~/.ea/logs/claude-cost.log. A
`rate_limit_event` in the stream is saved as the plan's latest usage reading
(rate_limits.py), which the chat bot's reply footer and `usage` command read.

Log line format (parseable; mirrors ea-chat-bot's `[mono] done` summary):

  YYYY-MM-DD HH:MM:SS INFO [agent=X model=Y] done in Ns | turns=N |
      tokens=A+Bcache->C | cost=$X.XXXX | tools=N | rc=N

One line per Claude invocation. Aggregate monthly cost with:

  grep '^' ~/.ea/logs/claude-cost.log | awk -F'cost=\\$' '{print $2}' \\
      | awk '{s+=$1} END {print s}'
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_paths import owner_log  # noqa: E402
import web_tool_log  # noqa: E402
try:
    import rate_limits  # noqa: E402
except Exception:  # a usage reading must never break the pipe
    rate_limits = None
# Written only by the owner's run-agent/run-plugin pipe, so keep it out of
# ea-bot's reach (a fixed name in the shared dir could be symlink-swapped).
LOG_PATH = owner_log("claude-cost.log")
LOG_DIR = LOG_PATH.parent


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True, help="Agent name (e.g., morning-briefing)")
    p.add_argument("--model", default="?", help="Model identifier passed to claude")
    args = p.parse_args()

    result_event: dict | None = None
    tool_count = 0

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON line (rare). Pass through verbatim so we don't drop
            # error text on the floor.
            sys.stdout.write(raw)
            continue

        etype = ev.get("type", "")
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_count += 1
                    # The cron lanes deny the web tools; a call that still
                    # happens lands in the outbound review's owner-side log.
                    if block.get("name") in web_tool_log.WEB_TOOLS:
                        web_tool_log.note(block.get("name"), block.get("input"),
                                          lane=f"cron:{args.agent}")
        elif etype == "rate_limit_event" and rate_limits is not None:
            # Never raises; saved only by an owner process.
            rate_limits.note_stream_event(
                ev, source=args.agent if ":" in args.agent else f"cron:{args.agent}")
        elif etype == "result":
            result_event = ev

    if result_event is None:
        # No result event — claude exited abnormally. Emit nothing on stdout
        # but log a partial record so the gap is visible.
        _append_log(args, duration_ms=0, n_turns=0, usage={},
                    cost=0.0, tool_count=tool_count, rc=1)
        return 1

    final_text = result_event.get("result", "")
    sys.stdout.write(final_text)
    if not final_text.endswith("\n"):
        sys.stdout.write("\n")

    _append_log(
        args,
        duration_ms=int(result_event.get("duration_ms", 0)),
        n_turns=int(result_event.get("num_turns", 0)),
        usage=result_event.get("usage", {}),
        cost=float(result_event.get("total_cost_usd", 0.0)),
        tool_count=tool_count,
        rc=0,
    )
    return 0


def _append_log(args, *, duration_ms: int, n_turns: int, usage: dict,
                cost: float, tool_count: int, rc: int) -> None:
    inp = usage.get("input_tokens", 0)
    cache = usage.get("cache_read_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts} INFO [agent={args.agent} model={args.model}] "
        f"done in {duration_ms / 1000:.1f}s | turns={n_turns} | "
        f"tokens={inp}+{cache}cache->{out} | cost=${cost:.4f} | "
        f"tools={tool_count} | rc={rc}\n"
    )
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line)
    except OSError:
        # Logging failure must never break the agent pipeline.
        pass


if __name__ == "__main__":
    sys.exit(main())
