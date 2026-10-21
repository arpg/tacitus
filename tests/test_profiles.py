#!/usr/bin/env python3
"""The install profile (email | review-only | assistant) is one setting that
every lane reads: the config module defines it, the launchers add the
review-only deny list, the cron lane writes reports to files and posts to
Slack only when the profile has it, scripts/install/setup.sh and the doctor take it as a
flag, and the /setup skill asks for it first."""

import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_config  # noqa: E402


def _cfg(profile: str | None) -> Path:
    d = Path(tempfile.mkdtemp(prefix="ea-test-profile-")) / "config"
    d.mkdir()
    p = d / "ea-config.json"
    p.write_text(json.dumps({"profile": profile} if profile else {}))
    return p


def test_profile_defaults_to_assistant_and_rejects_unknown_values():
    assert ea_config.get_profile(_cfg(None)) == "assistant"
    assert ea_config.get_profile(_cfg("email")) == "email"
    assert ea_config.get_profile(_cfg("gmail-first")) == "assistant"
    assert set(ea_config.PROFILE_FEATURES) == set(ea_config.PROFILES)
    keys = {frozenset(v) for v in ea_config.PROFILE_FEATURES.values()}
    assert len(keys) == 1, "every profile declares the same feature keys"
    assert not ea_config.profile_features("email")["slack"]
    assert ea_config.profile_features("email")["drafting"]
    assert not ea_config.profile_features("review-only")["drafting"]
    assert all(ea_config.profile_features("assistant").values())


def test_config_cli_answers_for_shell_callers():
    env = {**os.environ, "EA_DIR": str(_cfg("review-only").parent.parent)}
    cli = [sys.executable, str(ROOT / "preprocessor" / "ea_config.py")]
    assert subprocess.run(cli + ["profile"], capture_output=True, text=True, env=env).stdout.strip() == "review-only"
    assert subprocess.run(cli + ["feature", "slack"], env=env).returncode == 1
    assert subprocess.run(cli + ["feature", "drafting"], env=env).returncode == 1
    env["EA_DIR"] = str(_cfg(None).parent.parent)
    assert subprocess.run(cli + ["feature", "slack"], env=env).returncode == 0


def test_review_only_denies_every_drafting_verb_in_every_launcher():
    frag = ROOT / "scripts" / "disallowed-tools.review-only.txt"
    entries = [ln.strip() for ln in frag.read_text().splitlines()
               if ln.strip() and not ln.startswith("#")]
    blob = "\n".join(entries)
    for verb in ("graph_actions.py create-draft", "graph_actions.py create-reply",
                 "graph_actions.py update-draft", "gmail_client.py create-draft",
                 "gmail_client.py update-draft"):
        assert verb in blob, verb
    assert "mcp__claude_ai_Gmail__create_draft" in entries
    for launcher in ("run-agent.sh", "run-plugin.sh", "scripts/ea-bot-shell.sh",
                     "scripts/ea-resident-inner.sh"):
        src = (ROOT / launcher).read_text()
        assert "disallowed-tools.review-only.txt" in src, launcher
        assert 'review-only' in src, launcher


def test_cron_lane_writes_the_report_and_posts_only_with_slack():
    src = (ROOT / "run-agent.sh").read_text()
    assert 'ea_config.py" profile' in src and 'ea_config.py" feature chat' in src
    assert 'REPORT_DIR="$STATE_DIR/reports"' in src
    assert '"$REPORT_DIR/${AGENT}-latest.md"' in src
    assert 'if [[ "$HAS_SLACK" == 1 ]]; then' in src
    # the report file is written before the Slack decision
    assert src.index('${AGENT}-latest.md') < src.index("slack.post_to_slack('ea-assistant', msg)")


def test_setup_and_doctor_take_the_profile():
    setup = (ROOT / "scripts/install/setup.sh").read_text()
    assert "--profile)" in setup and 'HAS_SLACK=1' in setup
    assert 'd["profile"] = profile' in setup, "scripts/install/setup.sh records the profile in ea-config.json"
    assert 'for path in "$SCRIPT_DIR"/config/*' in setup, "every template is copied"
    assert "active-projects.md with your grants" not in setup
    r = subprocess.run(["bash", str(ROOT / "scripts/install/setup.sh"), "--profile", "gmail-first"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "unknown profile" in r.stderr
    doctor = [sys.executable, str(ROOT / "scripts" / "ea-doctor.py")]
    bare = tempfile.mkdtemp(prefix="ea-test-doctor-")
    # a scratch private dir, so the host's real webhook file is not found
    env = dict(os.environ, EA_PRIVATE_DIR=tempfile.mkdtemp(prefix="ea-test-private-"))
    email = subprocess.run(doctor + ["--ea-dir", bare, "--profile", "email", "--json"],
                           capture_output=True, text=True, env=env)
    full = subprocess.run(doctor + ["--ea-dir", bare, "--profile", "assistant", "--json"],
                          capture_output=True, text=True, env=env)
    e = json.loads(email.stdout); f = json.loads(full.stdout)
    e_fail = {r["name"] for r in e["results"] if r["status"] == "FAIL"}
    f_fail = {r["name"] for r in f["results"] if r["status"] == "FAIL"}
    assert "config: slack_webhooks.json" in f_fail and "config: slack_webhooks.json" not in e_fail
    assert "~/.ea-private/secrets.env" in f_fail and "~/.ea-private/secrets.env" not in e_fail
    assert e_fail <= f_fail, "the email profile never fails a check the full profile passes"
    assert any(r["name"] == "profile" and r["detail"] == "email" for r in e["results"])


def test_setup_skill_exists_and_the_core_points_at_it():
    skill = ROOT / ".claude" / "skills" / "setup" / "SKILL.md"
    text = skill.read_text()
    assert text.startswith("---\nname: setup\n")
    for must in ("email", "review-only", "assistant", "AskUserQuestion",
                 "ea-doctor.py --profile", "scripts/install/setup.sh --profile", "docs/it-request.md"):
        assert must in text, must
    core = (ROOT / "CLAUDE.md").read_text()
    assert "/setup" in core and ".claude/skills/setup/SKILL.md" in core
    assert "## Choose a profile" in (ROOT / "docs/SETUP.md").read_text()
    in_repo = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                             capture_output=True).returncode == 0
    if in_repo:  # the Docker image carries the tree without its .git
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(skill)]).returncode
        assert ignored == 1, ".claude/skills must be tracked so the skill deploys"


def test_setup_asks_for_the_institution_and_installs_the_overlay():
    """The skill and docs/SETUP.md both write the institution block, then render
    the overlay as ea-bot and install it in the deployed tree. Neither
    names one institution except as the pointer to the worked example."""
    skill = (ROOT / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    setup = (ROOT / "docs/SETUP.md").read_text()
    for name, text in (("SKILL.md", skill), ("docs/SETUP.md", setup)):
        for must in ("`institution` block", "safety_rail_domain", "video.platform", "video.link",
                     "zoom_link", "sudo -u ea-bot /usr/bin/python3 "
                     "/home/ea-bot/ea-system/preprocessor/install_overlay.py render",
                     "install_overlay.py install --tree /home/ea-bot/ea-system",
                     "config/examples/cu-boulder/"):
            assert must in text, (name, must)
        assert text.index("safety_rail_domain") < text.index("install_overlay.py render"), name
        for para in text.split("\n\n"):
            if re.search(r"\bCU\b|Boulder|\bOIT\b", para):
                assert "config/examples/cu-boulder/" in para, (name, para[:80])
    assert "### The install overlay" in setup


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
