#!/usr/bin/env python3
"""Every headless `claude -p` lane runs in dontAsk mode, and a lane that
needs no connector loads none.

ea-bot's Claude settings run auto mode, where a tool outside
`--allowedTools` goes to the auto-mode classifier and can be approved.
Under `--permission-mode dontAsk` the CLI refuses it, so the allow list is
the boundary. These tests pin the mode on each lane's command line, the
`--strict-mcp-config` (a flag with no value) and the connector switch on
each lane that grants no connector tool, the deny list's connector and
session-file rules for the bypass lanes, and the TWOCAPTCHA_API_KEY that no
lane receives.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import claude_cli  # noqa: E402
import resident_write_paths as RW  # noqa: E402

MODE = ["--permission-mode", "dontAsk"]
NO_CONNECTORS = "ENABLE_CLAUDEAI_MCP_SERVERS=false"
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
BYPASS_LANES = ("scripts/ea-resident-inner.sh", "scripts/ea-bot-shell.sh")


def _deny_entries() -> list[str]:
    return [ln.strip() for ln in (ROOT / "scripts" / "disallowed-tools.txt").read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _bot_fn(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    m = re.compile(r"\n(?:def |class |@)").search(BOT_SRC, start + 1)
    return BOT_SRC[start:m.start() if m else len(BOT_SRC)]


def _mode_of(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--permission-mode") + 1] if "--permission-mode" in cmd else None


# ── the builder ───────────────────────────────────────────────────────

def test_the_builder_ends_every_command_in_dont_ask():
    assert claude_cli.LANE_PERMISSION_MODE == "dontAsk"
    assert claude_cli.apply(["-p", "x"])[-2:] == MODE
    assert claude_cli.apply(["-p", "x", "--tools", "Bash", "--bare"])[-2:] == MODE


@pytest.mark.parametrize("flag", [
    ["--permission-mode", "auto"], ["--permission-mode", "dontAsk"],
    ["--permission-mode=bypassPermissions"], ["--dangerously-skip-permissions"],
    ["--allow-dangerously-skip-permissions"]])
def test_the_builder_refuses_a_command_that_picks_its_own_mode(flag):
    with pytest.raises(ValueError):
        claude_cli.apply(["-p", "x"] + flag)
    with pytest.raises(ValueError):
        claude_cli.apply(["-p", "x", "--bare"] + flag)


def test_a_prompt_spelled_like_a_mode_flag_is_refused():
    """-p takes no value, so the CLI would read such a prompt as the flag."""
    with pytest.raises(ValueError):
        claude_cli.apply(["-p", "--dangerously-skip-permissions"])


def test_no_configured_login_flag_sets_a_mode(monkeypatch):
    monkeypatch.setattr(claude_cli, "configured_flags",
                        lambda: ["--dangerously-skip-permissions", "--permission-mode=auto", "--x"])
    assert claude_cli.login_flags() == ["--x"]


def test_the_connector_switch_follows_strict_mcp_config():
    assert claude_cli.no_connector_env(["-p", "x", "--strict-mcp-config"]) == \
        {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    assert claude_cli.no_connector_env(["-p", "x"]) == {}


# ── the chat bot ──────────────────────────────────────────────────────

def _sudo_builder():
    """The bot's _sudo_claude_cmd, run with stand-ins for its globals."""
    ns = {"os": os, "EA_BOT_HOME": "/home/ea-bot", "EA_DIR": "/home/ea-bot/.ea",
          "EA_BOT_SCRIPT_DIR": Path("/home/ea-bot/ea-system"),
          "EA_BOT_USER": "ea-bot", "CLAUDE_BIN": "/home/ea-bot/.local/bin/claude",
          "_claude_cli": claude_cli, "_mediation_enabled": lambda: True}
    exec(_bot_fn("_sudo_claude_cmd"), ns)
    return ns["_sudo_claude_cmd"]


def test_every_bot_lane_runs_in_dont_ask(monkeypatch):
    """The chat, room and DM turns, the remote arm and the context export
    all build through _sudo_claude_cmd."""
    build = _sudo_builder()
    lanes = {
        "chat with MCP": ["--model", "m", "--allowedTools", "Read", "--session-id", "s"],
        "chat without MCP": ["--model", "m", "--allowedTools", "Read", "--strict-mcp-config"],
        "remote arm": ["--model", "m", "--allowedTools", "Read", "--tools", "Bash,Read,Write",
                       "--bare", "--strict-mcp-config"],
        "context export": ["-p", "x", "--model", "m", "--allowedTools", "Read", "--strict-mcp-config"],
    }
    for lane, args in lanes.items():
        cmd = build(args)
        assert _mode_of(cmd) == "dontAsk", lane
        claude = cmd.index("/home/ea-bot/.local/bin/claude")
        env = cmd[:claude]
        if "--strict-mcp-config" in args:
            assert NO_CONNECTORS in env, lane
        else:
            assert NO_CONNECTORS not in env, lane
        assert "{}" not in cmd, lane


def test_the_bot_lanes_pass_strict_mcp_config_without_a_value():
    assert 'args += ["--strict-mcp-config"]' in BOT_SRC
    export = _bot_fn("_handle_context_export")
    assert '"Read" + _resident_write_grants(), "--strict-mcp-config"]' in export
    assert "_sudo_claude_cmd(manifest_claude_args)" in export
    assert 'REMOTE_ARM_ARGS = ("--tools", "Bash,Read,Write", "--bare")' in BOT_SRC
    # Every claude run in the bot goes through the sudo builder.
    assert BOT_SRC.count("_claude_cli.apply(") == 1


def test_every_lane_runs_python_from_ea_bots_own_venv(monkeypatch):
    """A tool call's bare `python3` is ea-bot's venv, never the service's
    first PATH entry: that is the owner's venv, behind a home ea-bot cannot
    enter, so the next system python3 answered, without requirements.txt."""
    monkeypatch.setenv("PATH", "/home/owner/ea-system/.venv/bin:/usr/bin:/bin")
    cmd = _sudo_builder()(["-p", "x"])
    path = next(c for c in cmd if c.startswith("PATH=")).split("=", 1)[1].split(":")
    assert path[:2] == ["/home/ea-bot/ea-system/.venv/bin", "/home/ea-bot/.local/bin"]
    assert path[2:] == ["/home/owner/ea-system/.venv/bin", "/usr/bin", "/bin"]


def test_no_lane_receives_the_captcha_key(monkeypatch):
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "k-test")
    cmd = _sudo_builder()(["-p", "x"])
    assert not any(c.startswith("TWOCAPTCHA_API_KEY=") for c in cmd)
    assert "TWOCAPTCHA_API_KEY" not in _bot_fn("_sudo_claude_cmd").split('"""')[2]


# ── no stray value after --strict-mcp-config ──────────────────────────

_STRAY = re.compile(r"""--strict-mcp-config["']?,?\s*["']\{\}["']""")


def test_no_command_line_gives_strict_mcp_config_a_value():
    hits = []
    for path in list(ROOT.glob("*.py")) + list(ROOT.glob("*.sh")) + list(ROOT.glob("preprocessor/*.py")) \
            + list(ROOT.glob("scripts/*")) + list(ROOT.glob("tests/**/*.py")):
        if path.is_file() and path.name != Path(__file__).name and _STRAY.search(path.read_text(errors="ignore")):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []


# ── the owner tools and the backtest ─────────────────────────────────

def _capture(monkeypatch, module):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok"}), "")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


def test_the_mail_privacy_sample_runs_in_dont_ask_with_no_connector(monkeypatch):
    import mail_privacy
    for tools in ("", "Read"):
        seen = _capture(monkeypatch, mail_privacy)
        mail_privacy._run_claude("p", "haiku", tools)
        cmd = seen["cmd"]
        assert _mode_of(cmd) == "dontAsk" and "--strict-mcp-config" in cmd and "{}" not in cmd
        assert NO_CONNECTORS in cmd[:cmd.index("-p")]


def test_the_mail_tuning_reference_runs_in_dont_ask_with_no_connector(monkeypatch):
    import mail_privacy
    import mail_tuning
    seen = _capture(monkeypatch, mail_tuning)
    monkeypatch.setattr(mail_privacy, "_claude_bin", lambda: "/x/claude")
    mail_tuning.run_claude("p", "haiku")
    cmd = seen["cmd"]
    assert _mode_of(cmd) == "dontAsk" and "--strict-mcp-config" in cmd and "{}" not in cmd
    assert NO_CONNECTORS in cmd[:cmd.index("/x/claude")]


def test_the_mail_tagger_runs_in_dont_ask_with_no_connector(monkeypatch, tmp_path):
    import mail_tagger
    seen = _capture(monkeypatch, mail_tagger)
    monkeypatch.setattr(mail_tagger, "_remote_mode", lambda: False)
    monkeypatch.setattr(mail_tagger, "_owner_state_dir", lambda: tmp_path)
    mail_tagger.ask_model("p", "haiku")
    cmd = seen["cmd"]
    assert _mode_of(cmd) == "dontAsk" and "--strict-mcp-config" in cmd and "{}" not in cmd
    assert seen["kw"]["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


def test_the_arm_backtest_runs_in_dont_ask(monkeypatch):
    import arm_backtest as ab
    monkeypatch.setattr(ab, "_lane_env", lambda i, variant="": ["HOME=/home/ea-bot"])
    cmds = []
    ab.run_arm("p", 1, runner=lambda cmd, prompt: cmds.append(cmd) or (0, "", ""))
    cmd = cmds[0]
    assert _mode_of(cmd) == "dontAsk" and "--bare" in cmd and "--strict-mcp-config" in cmd
    assert NO_CONNECTORS in cmd[:cmd.index(ab.CLAUDE_BIN)]


def test_the_doctor_probe_runs_in_dont_ask(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor_mode", ROOT / "scripts" / "ea-doctor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RESULTS.clear()
    monkeypatch.setattr(m, "_as_ea_bot", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_dir="/home/ea-bot"))
    seen = []
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "OK"}), ""))
    m.check_claude_probe()
    assert _mode_of(seen[0]) == "dontAsk" and NO_CONNECTORS in seen[0]


# ── the shell lanes ───────────────────────────────────────────────────

def _code(path: str) -> str:
    return "\n".join(ln for ln in (ROOT / path).read_text().splitlines()
                     if not ln.lstrip().startswith("#"))


def test_every_shell_lane_writes_the_mode_into_its_command_line():
    """The mode is in the command string itself, not in the flags the
    `claude_cli.py flags` call prints, so a failed call cannot drop it."""
    for path, count in (("run-agent.sh", 3), ("run-plugin.sh", 1)):
        runs = [ln for ln in _code(path).splitlines() if "bash -c" in ln and "claude -p" in ln]
        assert len(runs) == count, path
        for ln in runs:
            assert "--permission-mode dontAsk" in ln, (path, ln)
            assert "--dangerously-skip-permissions" not in ln


def test_the_tool_less_cron_call_loads_no_connector():
    fn = _code("run-agent.sh")
    body = fn[fn.index("invoke_claude() {"):fn.index("\n}", fn.index("invoke_claude() {"))]
    assert 'if [[ -z "$3" ]]; then' in body
    assert "mcp_env=(ENABLE_CLAUDEAI_MCP_SERVERS=false)" in body
    assert "mcp_flags=(--strict-mcp-config)" in body
    assert '${mcp_env[@]+"${mcp_env[@]}"}' in body and '${mcp_flags[@]+"${mcp_flags[@]}"}' in body


def test_a_plugin_run_loads_connectors_only_when_its_manifest_grants_one():
    src = _code("run-plugin.sh")
    assert 'if [[ "$ALLOWED_TOOLS" != *mcp__* ]]; then' in src
    assert "MCP_ENV=(ENABLE_CLAUDEAI_MCP_SERVERS=false)" in src
    assert "MCP_FLAGS=(--strict-mcp-config)" in src
    run = src[src.index("CLAUDE_OUTPUT=$(timeout"):]
    assert '${MCP_ENV[@]+"${MCP_ENV[@]}"}' in run and '${MCP_FLAGS[@]+"${MCP_FLAGS[@]}"}' in run


def test_the_plugin_connector_switch_runs_for_real():
    """The guard and the guarded expansions, run in bash: a manifest with
    no connector verb gets both, one with a verb gets neither."""
    script = r'''
set -euo pipefail
for ALLOWED_TOOLS in "Read,Bash(ls *)" "Read,mcp__claude_ai_Google_Drive__search_files"; do
  MCP_ENV=(); MCP_FLAGS=()
  if [[ "$ALLOWED_TOOLS" != *mcp__* ]]; then
      MCP_ENV=(ENABLE_CLAUDEAI_MCP_SERVERS=false); MCP_FLAGS=(--strict-mcp-config)
  fi
  echo "${MCP_ENV[@]+"${MCP_ENV[@]}"}|${MCP_FLAGS[@]+"${MCP_FLAGS[@]}"}"
done
'''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["ENABLE_CLAUDEAI_MCP_SERVERS=false|--strict-mcp-config", "|"]


def test_every_command_line_builder_is_a_known_lane():
    """A file that hands the CLI an allow list or a tool set either builds
    through claude_cli.apply (or the bot's sudo builder), writes the mode
    into its command line, or is one of the two bypass lanes."""
    builders = []
    for path in list(ROOT.glob("*.py")) + list(ROOT.glob("*.sh")) + list(ROOT.glob("preprocessor/*.py")) \
            + list(ROOT.glob("scripts/*.py")) + list(ROOT.glob("scripts/*.sh")):
        text = path.read_text(errors="ignore")
        if not re.search(r"""["']--(allowedTools|tools)["']|--allowedTools \\?["']|--disallowed-tools \\?["']""",
                         text):
            continue
        rel = str(path.relative_to(ROOT))
        if rel in BYPASS_LANES or rel == "preprocessor/claude_cli.py":
            continue
        if not ("claude_cli.apply(" in text or "_sudo_claude_cmd(" in text
                or "--permission-mode dontAsk" in text):
            builders.append(rel)
    assert builders == []


def test_the_bypass_lanes_load_the_shared_deny_list():
    for path in BYPASS_LANES:
        src = (ROOT / path).read_text()
        assert "disallowed-tools.txt" in src and "--disallowed-tools" in src, path
        assert "--dangerously-skip-permissions" in src, path


# ── the deny list for the bypass lanes ────────────────────────────────

def _granted_connector_reads() -> set[str]:
    grants = set(re.findall(r"mcp__claude_ai_[A-Za-z_]+__[a-z_]+", _bot_fn("_plugin_tool_grants")))
    for text in (BOT_SRC[BOT_SRC.index("_MCP_TOOLS = ("):BOT_SRC.index("def _plugin_tool_grants(")],
                 _code("run-agent.sh"), _code("run-plugin.sh")):
        grants |= set(re.findall(r"mcp__claude_ai_[A-Za-z_]+__[a-z_]+", text))
    import chat_notify
    grants |= {g for g in chat_notify.SLACK_LLM_GRANTS.split(",") if g}
    return grants


def _denied(tool: str, rules: list[str]) -> bool:
    for r in rules:
        if not r.startswith("mcp__"):
            continue
        if tool == r or tool.startswith(r + "__") or fnmatch.fnmatchcase(tool, r):
            return True
    return False


def test_no_connector_deny_rule_takes_a_granted_read():
    reads = _granted_connector_reads()
    assert {"mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__list_labels",
            "mcp__claude_ai_Google_Calendar__list_events",
            "mcp__claude_ai_Google_Drive__read_file_content"} <= reads
    rules = _deny_entries()
    assert [t for t in sorted(reads) if _denied(t, rules)] == []


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Asana__asana_create_task", "mcp__claude_ai_Asana__asana_add_task_followers",
    "mcp__claude_ai_Notion__notion-create-pages", "mcp__claude_ai_Linear__save_issue",
    "mcp__claude_ai_Claude_Docs__update", "mcp__claude_ai_Claude_Docs__batch",
    "mcp__claude_ai_Box__authenticate", "mcp__claude_ai_Figma__complete_authentication",
    "mcp__claude_ai_Gmail__delete_draft", "mcp__claude_ai_Gmail__label_thread",
    "mcp__claude_ai_Gmail__update_message_labels", "mcp__claude_ai_Gmail__untrash_message",
    "mcp__claude_ai_Google_Drive__share_file",
    # A connector the account adds later, caught by verb.
    "mcp__claude_ai_Trello__trello_add_comment", "mcp__claude_ai_Jira__create_issue",
    "mcp__claude_ai_Teams__send_chat_message", "mcp__claude_ai_Dropbox__dropbox_upload_file",
])
def test_the_connector_write_verbs_are_denied(tool):
    assert _denied(tool, _deny_entries()), tool


def test_the_steering_files_are_denied_in_every_lane():
    lines = set(_deny_entries())
    for rule in RW.STEERING_RULES:
        assert rule in lines, rule
    for rule in ("Edit(~/.claude.json)", "Edit(~/.claude/projects/*/memory/**)",
                 "Edit(~/.claude/rules/**)", "Edit(~/.claude/output-styles/**)",
                 "Edit(//**/.claude/settings*.json)", "Edit(//**/.mcp.json)",
                 "Edit(~/.ea/docs/**/CLAUDE.md)", "Edit(~/.ea/research/**/CLAUDE.md)",
                 "Edit(~/.local/state/ea-lanes/**)"):
        assert rule in RW.STEERING_RULES, rule


def test_the_lane_root_is_the_one_the_deny_list_names():
    import slack_sessions
    assert str(slack_sessions.LANE_ROOT).endswith("/.local/state/ea-lanes") or \
        os.environ.get("EA_LANE_ROOT")

def test_the_chat_grants_add_filters_but_no_interpreter():
    """jq replaces `| python3 -c` for JSON; uniq is granted only as a filter
    (`uniq IN OUT` writes a file); no grant names a bare interpreter, shell or
    network client."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_BASH_ONLY_TOOLS = (")
    grants = src[start:src.index("\n)\n", start)]
    assert '"Bash(jq *),Bash(uniq),Bash(uniq -c),"' in grants
    for bad in ("Bash(uniq *)", "Bash(python3 *)", "Bash(python3 -c", "Bash(bash", "Bash(sh ",
                "Bash(curl", "Bash(wget", "Bash(sed -i"):
        assert bad not in grants, bad
    mcp = src[src.index("_MCP_TOOLS = ("):]
    mcp = mcp[:mcp.index("\n)\n")]
    assert "mcp__claude_ai_Google_Drive__read_file_content" in mcp
    assert "Google_Drive__create" not in mcp and "Google_Drive__update" not in mcp


def test_the_doc_review_prompt_names_the_lanes_tree():
    """The lane's grant names the lanes' tree; a command on the bot's own
    tree (/opt/ea-system) matches no rule and is refused."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    body = src[src.index("def _doc_review_prompt"):]
    body = body[:body.index("\ndef ")]
    assert 'f"python3 {EA_BOT_SCRIPT_DIR}/preprocessor/doc_review.py"' in body
    assert "{SCRIPT_DIR}/preprocessor/doc_review.py" not in body


def test_the_cron_skills_use_absolute_paths():
    """The cron lane runs in ea-bot's home, where a relative script path
    neither resolves nor matches a grant."""
    for name in ("morning-briefing.md", "afternoon-catchup.md", "weekly-audit.md"):
        assert "`python3 preprocessor/" not in (ROOT / "skills" / name).read_text(), name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
