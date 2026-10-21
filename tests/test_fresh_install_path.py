"""The fresh-install path for a user who follows the README quick start
with the email profile: no chat server, no remote model.

Each test runs the real shell out of scripts/install/setup.sh or run-agent.sh in a scratch
directory with stubs on PATH, or reads the scripts and docs the install
skill points at. Nothing here touches ~/.ea, sudo or a service.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SETUP = (REPO / "scripts/install/setup.sh").read_text()
RUN_AGENT = (REPO / "run-agent.sh").read_text()


def _segment(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


# ── scripts/install/setup.sh: the config copy and the profile record ─────────────────

def _run_config_block(ea_dir: Path, profile: str) -> subprocess.CompletedProcess:
    block = _segment(SETUP, 'OWNER_IO="$SCRIPT_DIR/scripts/owner-io.sh"', "# Make scripts executable")
    script = ("set -euo pipefail\n"
              f"SCRIPT_DIR={shlex.quote(str(REPO))}\nEA_DIR={shlex.quote(str(ea_dir))}\n"
              f"PROFILE={profile}\nHAS_SLACK=0\n" + block)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "EA_DIR": str(ea_dir)})


def test_setup_copies_every_regular_template_and_skips_the_examples_dir(tmp_path):
    ea_dir = tmp_path / ".ea"
    (ea_dir / "config").mkdir(parents=True)
    r = _run_config_block(ea_dir, "email")
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    templates = sorted(p.name for p in (REPO / "config").iterdir() if p.is_file())
    assert templates, "config/ holds the templates"
    assert (REPO / "config" / "examples").is_dir(), "the case this guards: a directory in config/"
    for name in templates:
        if name != "ea-config.json":
            assert (ea_dir / "config" / name).read_bytes() == (REPO / "config" / name).read_bytes(), name
    assert not (ea_dir / "config" / "examples").exists()
    # ea-config.json is the template plus the recorded profile
    cfg = json.loads((ea_dir / "config" / "ea-config.json").read_text())
    template = json.loads((REPO / "config" / "ea-config.json").read_text())
    assert cfg == {**template, "profile": "email"}
    assert "Profile recorded: email" in r.stdout
    # a second run rewrites nothing and says so
    before = {p.name: p.stat().st_mtime_ns for p in (ea_dir / "config").iterdir()}
    r2 = _run_config_block(ea_dir, "email")
    assert r2.returncode == 0, r2.stderr[-800:]
    assert "Profile already recorded: email" in r2.stdout
    assert {p.name: p.stat().st_mtime_ns for p in (ea_dir / "config").iterdir()} == before
    # a profile change rewrites only ea-config.json, keeping its other keys
    (ea_dir / "config" / "ea-config.json").write_text(json.dumps({"profile": "email", "log_level": "DEBUG"}))
    r3 = _run_config_block(ea_dir, "assistant")
    assert r3.returncode == 0, r3.stderr[-800:]
    assert json.loads((ea_dir / "config" / "ea-config.json").read_text()) == {
        "profile": "assistant", "log_level": "DEBUG"}


def test_setup_names_the_fix_when_the_config_dir_is_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    ea_dir = tmp_path / ".ea"
    (ea_dir / "config").mkdir(parents=True)
    os.chmod(ea_dir / "config", 0o500)
    try:
        r = _run_config_block(ea_dir, "email")
    finally:
        os.chmod(ea_dir / "config", 0o700)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "scripts/install/setup.sh: cannot write" in r.stderr and "scripts/install/setup-ea-user.sh" in r.stderr
    assert "./scripts/install/setup.sh --profile email" in r.stderr


def test_setup_no_longer_links_the_code_into_ea_dir():
    for stale in ('"$EA_DIR/preprocessor"', '"$EA_DIR/skills"', '"$EA_DIR/CLAUDE.md"'):
        assert stale not in SETUP, stale
    assert 'for link in "$EA_BOT_EA_DIR/preprocessor"' not in (REPO / "scripts/install/setup-ea-user.sh").read_text()


# ── run-agent.sh: the install overlay and the no-chat block ──────────

def _skill_content(tmp_path: Path, has_slack: str, facts: str | None) -> str:
    code = tmp_path / "code"
    (code / "scripts").mkdir(parents=True)
    (code / "preprocessor").mkdir()
    # owner-io.sh read, as a stub: the owner path case is a plain cat
    (code / "scripts" / "owner-io.sh").write_text('#!/bin/sh\n[ "$1" = read ] && exec cat -- "$2"\nexit 2\n')
    (code / "scripts" / "owner-io.sh").chmod(0o755)
    (code / "preprocessor" / "preferences.py").write_text(
        'print("## User rules\\n\\n- Never show me newsletters.")\n')
    ea_dir = tmp_path / "ea"
    (ea_dir / "config").mkdir(parents=True)
    if facts is not None:
        (ea_dir / "config" / "CLAUDE.install.md").write_text(facts)
    skill = tmp_path / "skill.md"
    skill.write_text("# Skill: Demo\n\nDo the demo thing.\n")
    block = _segment(RUN_AGENT, 'SKILL_CONTENT=$(cat "$SKILL_FILE")', 'PROMPT="You are running as the EA agent')
    script = ("set -euo pipefail\n"
              f"PYTHON3={shlex.quote(sys.executable)}\nSCRIPT_DIR={shlex.quote(str(code))}\n"
              f"EA_DIR={shlex.quote(str(ea_dir))}\nSKILL_FILE={shlex.quote(str(skill))}\n"
              f"LOG_FILE={shlex.quote(str(tmp_path / 'run.log'))}\nHAS_SLACK={has_slack}\n"
              "PROFILE=email\nAGENT=morning-briefing\n"
              'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
              + block + '\nprintf "%s" "$SKILL_CONTENT"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout


def test_cron_prompt_carries_the_install_overlay_after_the_skill_and_before_the_rules(tmp_path):
    facts = "# Install facts\n\nGenerated by install_overlay.py.\n\n## The user\n\n- Name: Ana Lopez\n"
    out = _skill_content(tmp_path, "1", facts)
    assert out.startswith("# Skill: Demo")
    assert "## Install facts (CLAUDE.local.md)" in out and "- Name: Ana Lopez" in out
    assert out.count("Install facts") == 1, "the overlay's own title line is dropped under the heading"
    assert out.index("Do the demo thing.") < out.index("## Install facts") < out.index("## User rules")
    assert "## No chat channel" not in out
    # no rendered overlay: the prompt is the skill and the rules, and the log says why
    out2 = _skill_content(tmp_path / "none", "1", None)
    assert "Install facts" not in out2 and "## User rules" in out2
    assert "No install overlay" in (tmp_path / "none" / "run.log").read_text()


def test_cron_prompt_says_there_are_no_buttons_without_a_chat_channel(tmp_path):
    out = _skill_content(tmp_path, "0", "# Install facts\n\n- Install profile: `email`.\n")
    assert "## No chat channel" in out
    block = out[out.index("## No chat channel"):out.index("## User rules")]
    for must in ("pending-event.json", "pending-schedule-send.json", "Drafts folder",
                 "recent_slack_messages", "morning-briefing-latest.md", "profile email"):
        assert must in block, must
    assert "click Send" in block
    assert out.index("## Install facts") < out.index("## No chat channel") < out.index("## User rules")
    # the whole prompt string still evaluates with every agent's real skill
    # (tests/test_run_agent_prompt_assembly.py); the blocks ride in SKILL_CONTENT
    assert "${SKILL_CONTENT}" in _segment(RUN_AGENT, 'PROMPT="You are running', 'Begin now."')


def test_run_agent_reads_the_profile_feature_by_its_neutral_name():
    assert 'ea_config.py" feature chat' in RUN_AGENT
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "ea_config.py"), "feature", "chat"],
                       env={**os.environ, "EA_DIR": tempfile.mkdtemp(prefix="ea-feature-")},
                       capture_output=True)
    assert r.returncode == 0, "the default profile has chat"


def test_now_flag_skips_the_morning_gate_and_the_nothing_new_skip():
    assert "--now) NOW=true; shift ;;" in RUN_AGENT
    gate = _segment(RUN_AGENT, "# Local-time gate for the morning briefing", "TIMESTAMP=$(date")
    assert 'if [[ "$AGENT" == "morning-briefing" && "$NOW" != true ]]; then' in gate
    assert "Mountain" not in RUN_AGENT, "the comment names no author's timezone"
    assert '"$DELTA_SIZE" -eq 0 && "$NOW" != true' in RUN_AGENT
    for doc in (".claude/skills/setup/SKILL.md", "docs/SETUP.md", "scripts/install/setup.sh"):
        assert "run-agent.sh morning-briefing --now" in (REPO / doc).read_text(), doc


def test_briefing_model_follows_the_installs_pin():
    assert 'MODEL="${EA_MODEL:-claude-opus-5-5}"' in RUN_AGENT


# ── the doctor on an email profile ───────────────────────────────────

def _doctor(ea_dir: Path, profile: str) -> dict:
    env = dict(os.environ, EA_DIR=str(ea_dir), EA_PRIVATE_DIR=tempfile.mkdtemp(prefix="ea-doctor-private-"))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "ea-doctor.py"), "--ea-dir", str(ea_dir),
                        "--profile", profile, "--json"], capture_output=True, text=True, env=env, timeout=120)
    return {x["name"]: x for x in json.loads(r.stdout)["results"]}


def test_doctor_warns_not_fails_on_the_calendar_when_the_profile_has_no_buttons(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "scheduling-preferences.json").write_text(json.dumps({"ea_managed_calendar": {}}))
    email = _doctor(tmp_path, "email")
    full = _doctor(tmp_path, "assistant")
    assert email["EA-Managed calendar"]["status"] == "WARN"
    assert "optional on this profile" in email["EA-Managed calendar"]["detail"]
    assert full["EA-Managed calendar"]["status"] == "FAIL"
    assert "The EA-Managed calendar" in full["EA-Managed calendar"]["fix"]


def test_doctor_fix_texts_name_sections_that_exist():
    doctor = (REPO / "scripts" / "ea-doctor.py").read_text()
    setup_md = (REPO / "docs/SETUP.md").read_text()
    for stale in ("3c/3d", "3a/3b", "step 3d", "docs/SETUP.md 3a)", "docs/SETUP.md 3c)", "project dashboard"):
        assert stale not in doctor, stale
    headings = {ln.lstrip("#").strip() for ln in setup_md.splitlines() if ln.startswith("#")}
    for heading in ("The Slack app", "The app-level token", "The private channel",
                    "The Slack environment files", "The EA-Managed calendar",
                    "The deferred-send signing key", "The service account", "Config files", "Services"):
        assert f"'{heading}'" in doctor and heading in headings, heading
    # the overlay fix is the owner's own render until the ea-bot user exists
    assert "the ea-bot user does not exist yet" in doctor
    assert 'f"sudo -u ea-bot /usr/bin/python3 {code}/preprocessor/install_overlay.py render"' in doctor


# ── harden-permissions.sh without a bot unit ─────────────────────────

def test_harden_skips_the_bot_unit_steps_when_there_is_no_unit():
    harden = (REPO / "deploy" / "harden-permissions.sh").read_text()
    assert "unit_installed ea-chat-bot && HAS_BOT=1" in harden
    assert 'if [ "$HAS_BOT" = 1 ]; then\necho "[harden] 5/7' in harden
    assert "skipped (no unit)" in harden
    assert '"$EA_TREE/.env"' not in _segment(harden, "for p in", "done"), "the bot's .env is not required"
    assert 'for svc in $UNITS; do' in harden
    assert "no unit to restart" in harden


def test_macos_runs_the_services_and_mediation_on_every_profile():
    """macOS has no harden-permissions.sh and scripts/install/setup-ea-user-macos.sh leaves
    mediation alone, so the skill runs install-services.sh (the mediator
    always, the bot only with chat) and enable-mail-mediation.sh on every
    profile, in section 8, not in the assistant-only section 6."""
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    sec8 = _segment(skill, "## 8. Acceptance", "## 9. First week")
    assert "On macOS, on every profile" in sec8
    assert sec8.index("`sudo bash deploy/macos/install-services.sh`") \
        < sec8.index("`sudo bash deploy/enable-mail-mediation.sh`")
    assert "already did this" not in skill
    services = (REPO / "deploy" / "macos" / "install-services.sh").read_text()
    assert 'ea_config.py" feature chat' in services and 'JOBS="$MED $BOT"' in services
    assert 'for j in $JOBS; do' in services and '"$EA_TREE/.env" \\' not in services
    assert "enable-mail-mediation.sh" in (REPO / "scripts/install/setup-ea-user-macos.sh").read_text()


def test_the_email_profile_promises_no_holds():
    """With no chat platform nothing reads pending-event.json (the no-chat
    block in run-agent.sh forbids staging it), so no doc says the email
    profile holds time on a calendar."""
    assert "Do not write pending-event.json" in RUN_AGENT
    for doc in ("README.md", "docs/SETUP.md"):
        text = (REPO / doc).read_text()
        row = next(ln for ln in text.splitlines() if ln.startswith("| `email`"))
        assert "no holds" in row and "EA holds on your Outlook calendar" not in row, doc
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    assert "holds time on its own calendar" not in skill


def test_one_bot_unit_path_the_system_unit_harden_writes():
    """scripts/install/setup.sh writes no unit; harden-permissions.sh says which unit it
    found, writes the system unit on a chat profile with none, reports a
    user unit with the step that retires it (never a second bot beside it),
    and the docs describe that one path."""
    setup = SETUP
    harden = (REPO / "deploy" / "harden-permissions.sh").read_text()
    assert "systemd/user" not in setup and "systemctl --user" not in setup
    assert "deploy/harden-permissions.sh" in _segment(setup, "# 8. The chat bot service", "# 9. Next steps")
    assert "found the system unit ea-chat-bot.service" in harden
    assert 'elif [ -e "$USER_UNIT" ]; then' in harden and "systemctl --user disable --now ea-chat-bot" in harden
    user_branch = _segment(harden, 'elif [ -e "$USER_UNIT" ]; then', "elif as_owner")
    assert "HAS_BOT=1" not in user_branch, "no system unit beside a user unit"
    assert 'ea_config.py" feature chat' in harden
    assert "cat > /etc/systemd/system/ea-chat-bot.service <<EOF" in harden
    assert "SupplementaryGroups=ea" in harden and "systemctl enable ea-chat-bot.service" in harden
    assert "/etc/sudoers.d/ea-chat-bot" in harden
    setup_md = (REPO / "docs/SETUP.md").read_text()
    services = _segment(setup_md, "## Services", "## Cron schedule")
    assert "Prefer a **system** unit" not in services and ".venv/bin/python3" not in services
    assert "harden-permissions.sh` writes it" in services
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    assert "installs the bot unit" not in skill and "`sudo bash deploy/harden-permissions.sh`" in skill


# ── the docs the quick start reads ───────────────────────────────────

def test_docs_put_setup_sh_before_the_service_account_and_name_the_report_file():
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    setup_md = (REPO / "docs/SETUP.md").read_text()
    readme = (REPO / "README.md").read_text()
    sec5 = _segment(skill, "## 5. Service account", "## 6. Chat platform")
    assert sec5.index("./scripts/install/setup.sh --profile <name>") < sec5.index("sudo ./scripts/install/setup-ea-user.sh")
    assert "## 0. Resume" in skill and "picks up at this section" not in skill
    for text, name in ((skill, "SKILL.md"), (setup_md, "docs/SETUP.md"), (readme, "README.md")):
        assert "web board" not in text, name
        assert "reports/<agent>-latest.md" in text, name
    assert "holds on the EA-Managed calendar" not in setup_md
    assert "opik/docker-compose.yml" not in setup_md
    assert "GRAPH_AUTH_METHOD=device_code" in setup_md and "scripts/install/user_init.py --phase auth" in setup_md
    assert "`scripts/install/setup.sh` writes these entries for you" not in setup_md
    assert "ALL=(ea-bot) NOPASSWD: ALL" in setup_md
    assert "assistant` profile it also checks new mail every 10 minutes" in readme
    # the text this change added carries no em-dash
    added = (sec5, _segment(skill, "## 0. Resume", "## 1. Profile"),
             _segment(setup_md, "## Cron schedule", "### The deferred-send signing key"),
             _segment(RUN_AGENT, 'SKILL_CONTENT=$(cat "$SKILL_FILE")', 'PROMPT="You are running'),
             _segment(SETUP, 'OWNER_IO="$SCRIPT_DIR/scripts/owner-io.sh"', "# Make scripts executable"))
    for text in added:
        assert chr(0x2014) not in text
