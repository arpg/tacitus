#!/usr/bin/env python3
"""The stale-briefing warning gets its age on GNU stat and on BSD stat.

When the preprocessor fails, run-agent.sh carries on with the last
briefing file and tells the model how old it is, so the briefing says
that the mail sync failed. The age came from `stat -c %Y`, which is GNU.
BSD stat (macOS) rejects -c, the arithmetic that used the empty result
failed, and PREPROCESS_WARNING stayed empty: on 2026-11-11 a macOS run
with a failed preprocessor posted the morning's data with no warning.

The test runs the script's own lines through bash with a `stat` on PATH
that behaves as each kind does.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

GNU = """#!/bin/sh
[ "$1" = -c ] && [ "$2" = %Y ] && { echo "$FAKE_MTIME"; exit 0; }
echo "stat: unexpected $*" >&2; exit 2
"""
BSD = """#!/bin/sh
[ "$1" = -c ] && { echo "stat: illegal option -- c" >&2; exit 1; }
[ "$1" = -f ] && [ "$2" = %m ] && { echo "$FAKE_MTIME"; exit 0; }
echo "stat: unexpected $*" >&2; exit 2
"""


def _age_lines() -> str:
    """From the stale-briefing log line to the PREPROCESS_WARNING
    assignment, as run-agent.sh has them."""
    text = (ROOT / "run-agent.sh").read_text()
    m = re.search(r'log "Continuing with stale briefing[^\n]*\n(.*?\n'
                  r' *PREPROCESS_WARNING="WARNING: The preprocessor FAILED[^\n]*\n)',
                  text, re.S)
    assert m, "the stale-briefing block is gone from run-agent.sh"
    return m.group(1)


@pytest.mark.parametrize("kind,fake", [("gnu", GNU), ("bsd", BSD)])
def test_the_warning_carries_the_age(kind, fake, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "stat").write_text(fake)
    (bin_dir / "stat").chmod(0o755)
    briefing = tmp_path / "briefing-latest.json"
    briefing.write_text("{}")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin",
           "FAKE_MTIME": str(int(time.time()) - 90 * 60),
           "BRIEFING_FILE": str(briefing)}
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _age_lines()
         + '\nprintf "%s" "$PREPROCESS_WARNING"\n'],
        env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"{kind}: {r.stderr}"
    m = re.search(r"STALE \((\d+) minutes old\)", r.stdout)
    assert m, f"{kind}: no age in the warning: {r.stdout[:200]!r} {r.stderr}"
    assert 89 <= int(m.group(1)) <= 91


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
