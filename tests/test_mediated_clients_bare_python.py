#!/usr/bin/env python3
"""Every mediated client reaches forward_or_none on a bare interpreter.

ea-bot's own python3 need not hold requirements.txt: a mediated verb runs
in the mediator, with the owner's interpreter. That holds only while the
client forwards before its first third-party import. google_calendar_client
and gmail_client forwarded from main(), after `from google...`, so on a host
whose system python3 lacked the Google packages (macOS, Homebrew) every bot
call died with "No module named 'google'" and never reached the mediator.

`python3 -S` drops site-packages, which is that interpreter on any host.
An unknown verb is refused by forward_or_none before anything is queued, so
reaching the refusal proves the forward came first.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from mail_mediator import TOOLS  # noqa: E402


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_client_forwards_before_third_party_imports(tool):
    home = tempfile.mkdtemp(prefix="ea-test-bare-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
           "EA_DIR": str(Path(home) / ".ea"), "EA_FORCE_MEDIATE": "1"}
    r = subprocess.run([sys.executable, "-S", str(TOOLS[tool]), "no-such-verb"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr
    assert "unknown or unmediated verb" in r.stderr, r.stderr
    assert r.returncode == 2
