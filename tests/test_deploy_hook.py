#!/usr/bin/env python3
"""The deploy hook restarts the bot only when code the running process
holds in memory changed. Everything else (skills, docs, CLAUDE.md, config
templates, agent definitions) is read per dispatch from the deployed tree."""

import re
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "post-commit"


def _pattern() -> str:
    m = re.search(r"grep -E '(\^\(.*?\)\$)'", HOOK.read_text())
    assert m, "runtime pattern not found in the hook"
    return m.group(1)


def _restarts(files: list[str]) -> bool:
    r = subprocess.run(["grep", "-E", _pattern()], input="\n".join(files) + "\n",
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def test_docs_skills_and_config_do_not_restart():
    assert not _restarts(["docs/ARCHITECTURE.md", "CLAUDE.md", "skills/morning-briefing.md",
                          "config/ea-config.json", ".claude/agents/email-author.md",
                          "tests/test_bot_wiring.py", "docs/SETUP.md"])


def test_runtime_code_restarts():
    assert _restarts(["docs/ARCHITECTURE.md", "ea-chat-bot.py"])
    assert _restarts(["preprocessor/draft_events.py"])
    assert _restarts(["scripts/disallowed-tools.txt"])
    assert _restarts(["scripts/disallowed-tools.review-only.txt"])
    assert _restarts(["requirements.txt"])


def test_hook_diffs_from_the_commit_the_copy_ran_not_the_last_commit_only():
    # A pull fast-forwards several commits and fires post-merge once; the
    # restart decision must see every commit since the deployed copy's
    # previous head, else runtime code in all but the last is missed.
    hook = HOOK.read_text()
    assert "HEAD~1 HEAD" not in hook
    assert hook.count('"${OLD_HEAD:-HEAD~1}" HEAD') == 2
    assert hook.index('OLD_HEAD="$(deploy_git rev-parse HEAD') < hook.index('deploy_git fetch "$REPO_ROOT" HEAD')
    assert hook.index('deploy_git fetch "$REPO_ROOT" HEAD') < hook.index("RUNTIME_CHANGED=")


def test_hook_waits_for_an_idle_bot_before_restarting():
    hook = HOOK.read_text()
    i = hook.index("RUNTIME_CHANGED=")
    j = hook.index('systemctl restart "$BOT_UNIT.service"')
    assert i < hook.index("bot-inflight.json") < j
    assert subprocess.run(["bash", "-n", str(HOOK)]).returncode == 0


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
