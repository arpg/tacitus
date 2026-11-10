#!/usr/bin/env python3
"""A prompt names the resident's real home, not always /home/ea-bot.

CLAUDE.md, the skills and the topic guides write every command under
/home/ea-bot, the account's home on Linux. A lane's allowlist is built
from the account's real home and matches the command line literally. On a
macOS install (home /Users/ea-bot) a command copied from the skill was
therefore refused, the model improvised a `cd ... && python3 ...`, that was
refused too, and on 2026-11-10 a morning briefing went out saying the
ledger could not be read.

The cron launchers and the chat lane's prompt builder now write the real
home into the text they hand the model, and the install facts say it for
a run that loads CLAUDE.md from the deployed tree by itself. On Linux
nothing changes.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import install_overlay  # noqa: E402
import prompt_logic  # noqa: E402

SKILL = ("- Check OPEN obligations: `python3 /home/ea-bot/ea-system/"
         "preprocessor/ledger.py list --status open`\n"
         "All `~/.ea/` paths resolve to `/home/ea-bot/.ea/`.\n")
REWRITE = re.compile(r"^if \[\[ \"\$EA_BOT_HOME\" != /home/ea-bot \]\]; then\n"
                     r"    PROMPT=.*\nfi$", re.M)


def test_another_home_replaces_the_linux_path():
    out = prompt_logic.resident_paths(SKILL, "/Users/ea-bot")
    assert "/home/ea-bot" not in out
    assert "python3 /Users/ea-bot/ea-system/preprocessor/ledger.py list" in out
    assert "`/Users/ea-bot/.ea/`" in out


def test_the_linux_home_changes_nothing():
    assert prompt_logic.resident_paths(SKILL, "/home/ea-bot") == SKILL
    assert prompt_logic.resident_paths(SKILL, "/home/ea-bot/") == SKILL


def test_another_account_with_the_same_prefix_is_left_alone():
    text = "see /home/ea-bot-archive/notes and /home/ea-bot2/x\n"
    assert prompt_logic.resident_paths(text, "/Users/ea-bot") == text


def test_the_chat_prompt_names_the_real_home(monkeypatch):
    monkeypatch.setattr(prompt_logic, "resident_home", lambda: "/Users/ea-bot")
    out = prompt_logic.build_claude_md(set(), SKILL, overlay="")
    assert "/home/ea-bot" not in out
    assert "/Users/ea-bot/ea-system/preprocessor/ledger.py" in out


@pytest.mark.parametrize("script", ["run-agent.sh", "run-plugin.sh"])
@pytest.mark.parametrize("home,want", [
    ("/Users/ea-bot", SKILL.replace("/home/ea-bot/", "/Users/ea-bot/")),
    ("/home/ea-bot", SKILL),
])
def test_the_cron_prompt_names_the_real_home(script, home, want):
    """The launcher's own rewrite, run through bash on a prompt that
    carries a skill's commands."""
    found = REWRITE.findall((ROOT / script).read_text())
    assert len(found) == 1, f"{script}: the rewrite after PROMPT is gone"
    r = subprocess.run(
        ["bash", "-c", 'set -euo pipefail\nEA_BOT_HOME=$1\nPROMPT=$2\n'
         + found[0] + '\nprintf "%s" "$PROMPT"\n', "bash", home, SKILL],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout == want


def test_the_install_facts_name_another_home():
    lines = install_overlay._paths_section("/Users/ea-bot")
    assert lines[0] == "## Paths"
    assert "`/Users/ea-bot`" in lines[-1]
    # The sentence survives the prompt rewrite: it names the Linux home
    # with no path below it.
    text = "\n".join(lines)
    assert prompt_logic.resident_paths(text, "/Users/ea-bot") == text


def test_the_install_facts_say_nothing_on_linux():
    assert install_overlay._paths_section("/home/ea-bot") == []
