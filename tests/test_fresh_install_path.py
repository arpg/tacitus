"""The fresh-install path for a user who follows the README quick start
in the reviewer mode: no chat server, no remote model.

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
              f"PROFILE={profile}\nPROFILE_ACTION=record\nPROFILE_DRAFTING=\nREPORTS=\n"
              "HAS_SLACK=0\n" + block)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "EA_DIR": str(ea_dir)})


def test_setup_copies_every_regular_template_and_skips_the_examples_dir(tmp_path):
    ea_dir = tmp_path / ".ea"
    (ea_dir / "config").mkdir(parents=True)
    r = _run_config_block(ea_dir, "reviewer")
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
    assert cfg == {**template, "profile": "reviewer", "profile_version": 2}
    assert "Profile recorded: reviewer" in r.stdout
    # a second run rewrites nothing and says so
    before = {p.name: p.stat().st_mtime_ns for p in (ea_dir / "config").iterdir()}
    r2 = _run_config_block(ea_dir, "reviewer")
    assert r2.returncode == 0, r2.stderr[-800:]
    assert "Profile already recorded: reviewer" in r2.stdout
    assert {p.name: p.stat().st_mtime_ns for p in (ea_dir / "config").iterdir()} == before
    # a profile change rewrites only ea-config.json, keeping its other keys
    (ea_dir / "config" / "ea-config.json").write_text(json.dumps({"profile": "reviewer", "log_level": "DEBUG"}))
    r3 = _run_config_block(ea_dir, "assistant")
    assert r3.returncode == 0, r3.stderr[-800:]
    assert json.loads((ea_dir / "config" / "ea-config.json").read_text()) == {
        "profile": "assistant", "log_level": "DEBUG", "profile_version": 2}


def test_setup_names_the_fix_when_the_config_dir_is_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    ea_dir = tmp_path / ".ea"
    (ea_dir / "config").mkdir(parents=True)
    os.chmod(ea_dir / "config", 0o500)
    try:
        r = _run_config_block(ea_dir, "reviewer")
    finally:
        os.chmod(ea_dir / "config", 0o700)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "scripts/install/setup.sh: cannot write" in r.stderr and "scripts/install/setup-ea-user.sh" in r.stderr
    assert "./scripts/install/setup.sh --profile reviewer" in r.stderr


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
              "PROFILE=email\nAGENT=morning-briefing\nEA_BOT_SCRIPT_DIR=/srv/ea\n"
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
    line = next(l for l in RUN_AGENT.splitlines() if "morning-briefing|afternoon-catchup" in l
                and "MODEL=" in l)
    pick = line.split(")", 1)[1].split(";;")[0].strip()

    def model(**env):
        return subprocess.run(["bash", "-c", f'{pick}; printf %s "$MODEL"'], capture_output=True,
                              text=True, env={"PATH": os.environ["PATH"], **env}).stdout

    assert model() == "claude-opus-5-5"
    assert model(EA_MODEL="m-chat") == "m-chat"
    assert model(EA_MODEL="m-chat", EA_BRIEFING_MODEL="m-brief") == "m-brief"



def _literal_rule(**env) -> tuple[str, str]:
    """(RULE_COUNT, LITERAL_RULE) from run-agent.sh's own gate block."""
    start = RUN_AGENT.index("RULE_COUNT=four")
    gate = RUN_AGENT[start:RUN_AGENT.index("\nfi\n", start) + 4]
    out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + gate
                          + 'printf "%s|%s" "$RULE_COUNT" "$LITERAL_RULE"'],
                         capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "EA_BOT_SCRIPT_DIR": "/srv/ea", **env})
    assert out.returncode == 0, out.stderr
    count, rule = out.stdout.split("|", 1)
    return count, rule


def test_the_literal_command_rule_is_on_by_default():
    """Unset, the cron prompt says five rules and carries rule 5;
    EA_LITERAL_COMMANDS=0 turns it off, and a Codex lane never gets it."""
    count, rule = _literal_rule()
    assert count == "five" and rule.startswith("5. EVERY Bash CALL")
    assert "/srv/ea/preprocessor/<script>.py" in rule and "$(...)" in rule
    # mail goes through the unified CLI the lane grants, threads through the provider's
    assert "mail_actions.py --provider" in rule and "never retry it" in rule
    assert _literal_rule(EA_LITERAL_COMMANDS="1") == (count, rule)
    assert _literal_rule(EA_LITERAL_COMMANDS="0") == ("four", "")
    # Codex runs in its sandbox with no allowlist: the rule would not be true there
    assert _literal_rule(CODEX_LANE="1") == ("four", "")
    assert "${RULE_COUNT:-four} rules from CLAUDE.md" in RUN_AGENT and "${LITERAL_RULE:-}" in RUN_AGENT
    # the harness is chosen before the prompt that depends on it is built
    assert RUN_AGENT.index("\nCODEX_LANE=0\n") < RUN_AGENT.index("RULE_COUNT=four")
    assert RUN_AGENT.index("RULE_COUNT=four") < RUN_AGENT.index('PROMPT="You are running as the EA agent')


def _rule_pattern_matches(pattern: str, command: str) -> bool:
    """A Bash(<pattern>) permission rule: `*` matches any text."""
    import re
    return re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), command, re.S) is not None


def _bash_rules(lines) -> list[str]:
    return [ln[len("Bash("):-1] for ln in (l.strip() for l in lines)
            if ln.startswith("Bash(") and ln.endswith(")")]


def _rule_commands(rule: str) -> list[str]:
    """One sample command line for each command the rule tells the lane to run."""
    import re
    flat = " ".join(rule.split())
    prefix = re.search(r"python3 (\S+)/preprocessor/<script>\.py", flat).group(1)
    cmds = [f"python3 {prefix}/preprocessor/ledger.py list --status open"]
    plain = {"jq '<filter>' <file>": "jq .items /tmp/b.json", "cat <file>": "cat /tmp/b.json",
             "ls <dir>": "ls /tmp"}
    for shape, sample in plain.items():
        assert shape in flat, shape
        cmds.append(sample)
    verbs = re.search(r"mail_actions\.py --provider outlook\|gmail \(([^)]*)\)", flat).group(1)
    for verb in verbs.split(";")[0].split(","):
        for provider in ("outlook", "gmail"):
            cmds.append(f"python3 {prefix}/preprocessor/mail_actions.py {verb.strip()} "
                        f"--provider {provider} --message-id m1")
    for script, verb in re.findall(r"(graph_actions\.py|gmail_client\.py) (get-\w+)", flat):
        cmds.append(f"python3 {prefix}/preprocessor/{script} {verb} --conversation-id c1")
    return cmds


def test_the_literal_command_rule_names_only_what_the_lane_grants():
    """Every command rule 5 names matches a grant of the cron lane's
    ALLOWED_TOOLS and no deny rule; with drafting off it names no verb the
    no-drafting list denies. A rule that named a refused command would
    cause the refusals it exists to stop."""
    sys.path.insert(0, str(REPO / "tests"))
    from test_allowed_tools import parse_run_agent_allowed_tools
    grants, problems = parse_run_agent_allowed_tools()
    assert not problems, problems
    allow = _bash_rules(grants)
    deny = _bash_rules((REPO / "scripts/disallowed-tools.txt").read_text().splitlines())
    no_drafting = _bash_rules((REPO / "scripts/disallowed-tools.no-drafting.txt").read_text().splitlines())
    script_dir = "/home/ea-bot/ea-system"  # the parse's own stand-in for EA_BOT_SCRIPT_DIR
    for drafting, denied in (("1", deny), ("0", deny + no_drafting)):
        _, rule = _literal_rule(EA_BOT_SCRIPT_DIR=script_dir, HAS_DRAFTING=drafting)
        cmds = _rule_commands(rule)
        assert len(cmds) >= 9, cmds
        for cmd in cmds:
            assert any(_rule_pattern_matches(g, cmd) for g in allow), f"not granted: {cmd}"
            hit = [d for d in denied if _rule_pattern_matches(d, cmd)]
            assert not hit, f"drafting={drafting}: the rule names {cmd!r}, denied by {hit}"
    # with drafting on the draft verbs are named; off, they are gone and the rule says why
    on, off = _literal_rule()[1], _literal_rule(HAS_DRAFTING="0")[1]
    assert "create-reply" in on.split("Threads:")[0]
    assert "create-reply" not in off.split("Threads:")[0] and "drafting is off" in off


def test_the_plugin_lane_carries_the_shell_rule():
    """run-plugin.sh's allowlist is literal too, and its grants differ per
    plugin, so its rule names no command; EA_LITERAL_COMMANDS=0 removes it."""
    src = (REPO / "run-plugin.sh").read_text()
    start = src.index('SHELL_RULE=""')
    gate = src[start:src.index("\nfi\n", start) + 4]

    def shell_rule(**env):
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + gate + 'printf "%s" "$SHELL_RULE"'],
                           capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env})
        assert r.returncode == 0, r.stderr
        return r.stdout

    rule = shell_rule()
    assert rule.startswith("SHELL RULE: every Bash call is one command") and "no $(...)" in rule
    assert rule.endswith("\n\n") and "jq" not in rule and "Bash(" not in rule
    assert shell_rule(EA_LITERAL_COMMANDS="0") == ""
    assert "${SHELL_RULE}CRITICAL OUTPUT RULE:" in src
    assert src.index('SHELL_RULE=""') < src.index('PROMPT="You are running as the EA plugin')


# ── the doctor in the reviewer mode ──────────────────────────────────

def _doctor(ea_dir: Path, profile: str) -> dict:
    env = dict(os.environ, EA_DIR=str(ea_dir), EA_PRIVATE_DIR=tempfile.mkdtemp(prefix="ea-doctor-private-"))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "ea-doctor.py"), "--ea-dir", str(ea_dir),
                        "--profile", profile, "--json"], capture_output=True, text=True, env=env, timeout=120)
    return {x["name"]: x for x in json.loads(r.stdout)["results"]}


def test_doctor_warns_not_fails_on_the_calendar_when_the_profile_has_no_buttons(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "scheduling-preferences.json").write_text(json.dumps({"ea_managed_calendar": {}}))
    reviewer = _doctor(tmp_path, "reviewer")
    full = _doctor(tmp_path, "assistant")
    assert reviewer["EA-Managed calendar"]["status"] == "WARN"
    assert "optional on this profile" in reviewer["EA-Managed calendar"]["detail"]
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


def test_every_deploy_or_scripts_file_the_doctor_and_docs_name_exists():
    """A fix text that names a file the repository does not ship sends an
    installer looking for it (the doctor named a dashboard unit file that
    was never committed)."""
    import re
    pat = re.compile(r"(?<![\w/.~-])((?:deploy|scripts)/[\w./-]*\w\."
                     r"(?:sh|py|service|timer|plist|conf)(?:\.in)?)(?![\w-])")
    texts = [REPO / "scripts/ea-doctor.py", REPO / "CLAUDE.md", *sorted((REPO / "docs").rglob("*.md"))]
    named, missing = 0, []
    for path in texts:
        for rel in sorted(set(pat.findall(path.read_text()))):
            named += 1
            if not (REPO / rel).exists():
                missing.append(f"{path.relative_to(REPO)}: {rel}")
    assert named > 50, named
    assert not missing, missing
    doctor = (REPO / "scripts/ea-doctor.py").read_text()
    assert "deploy/enable-dashboard.sh" in doctor and '"ea-dashboard",)' in doctor


# ── harden-permissions.sh without a bot unit ─────────────────────────

def test_harden_skips_the_bot_unit_steps_when_there_is_no_unit():
    harden = (REPO / "deploy" / "harden-permissions.sh").read_text()
    assert "unit_installed ea-chat-bot && HAS_BOT=1" in harden
    assert 'if [ "$HAS_BOT" = 1 ]; then\necho "[harden] 5/7' in harden
    assert "skipped (no unit)" in harden
    assert '"$EA_TREE/.env"' not in _segment(harden, "for p in", "done"), "the bot's .env is not required"
    assert 'for svc in $UNITS; do' in harden
    assert "no unit to restart" in harden


def test_macos_runs_the_services_and_mediation_in_every_mode():
    """macOS has no harden-permissions.sh and scripts/install/setup-ea-user-macos.sh leaves
    mediation alone, so the skill runs install-services.sh (the mediator
    always, the bot only with chat) and enable-mail-mediation.sh in every
    mode, in section 8, not in the chat-only section 6."""
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    sec8 = _segment(skill, "## 8. Acceptance", "## 9. First week")
    assert "On macOS, in every mode" in sec8
    assert sec8.index("`sudo bash deploy/macos/install-services.sh`") \
        < sec8.index("`sudo bash deploy/enable-mail-mediation.sh`")
    assert "already did this" not in skill
    services = (REPO / "deploy" / "macos" / "install-services.sh").read_text()
    assert 'ea_config.py" feature chat' in services and 'JOBS="$MED $BOT"' in services
    assert 'for j in $JOBS; do' in services and '"$EA_TREE/.env" \\' not in services
    assert "enable-mail-mediation.sh" in (REPO / "scripts/install/setup-ea-user-macos.sh").read_text()


def test_the_reviewer_mode_promises_no_holds():
    """With no chat platform nothing reads pending-event.json (the no-chat
    block in run-agent.sh forbids staging it), so no doc says the reviewer
    mode holds time on a calendar."""
    assert "Do not write pending-event.json" in RUN_AGENT
    for doc in ("README.md", "docs/SETUP.md"):
        text = (REPO / doc).read_text()
        row = next(ln for ln in text.splitlines() if ln.startswith("| `reviewer`"))
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
    assert "assistant and collaborator modes it also checks new mail every 10 minutes" in readme
    # the text this change added carries no em-dash
    added = (sec5, _segment(skill, "## 0. Resume", "## 1. Mode"),
             _segment(setup_md, "## Cron schedule", "### The deferred-send signing key"),
             _segment(RUN_AGENT, 'SKILL_CONTENT=$(cat "$SKILL_FILE")', 'PROMPT="You are running'),
             _segment(SETUP, 'OWNER_IO="$SCRIPT_DIR/scripts/owner-io.sh"', "# Make scripts executable"))
    for text in added:
        assert chr(0x2014) not in text
