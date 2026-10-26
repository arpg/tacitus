#!/usr/bin/env python3
"""Guard against allowlist drift between preprocessor helpers and the
Bash-tool allowlist the Claude subprocess is handed.

The invariant: every preprocessor helper script whose skill is
registered in `prompt_logic.SKILL_SIGNALS` (or which exposes a CLI
`if __name__ == "__main__"`) must appear in the Bash-tool allowlist
string. If this fails, open ea-chat-bot.py and append the missing
script to `_BASH_ONLY_TOOLS`.

The cron lane enforces its own allowlist, since the resident launches
with `--dangerously-skip-permissions`. This file also parses
run-agent.sh's `ALLOWED_TOOLS` block and checks it against the helper
set DERIVED from the skills its cron arms reference, so adding an agent
arm or a helper call to a cron skill fails the wall until the grant
follows.

Usage:
    python3 tests/test_allowed_tools.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT = REPO / "ea-chat-bot.py"
PREPROC = REPO / "preprocessor"
SKILLS = REPO / "skills"
RUN_AGENT = REPO / "run-agent.sh"
DENY_FILE = REPO / "scripts" / "disallowed-tools.txt"

# --- Gate invariants -------------------------------------------------
# An MCP wildcard grant also matches the write verbs (send_message,
# reply, forward, trash_*, create_event, update_event, delete_event,
# respond_to_event), which no confirm-token or mediator can stop. Two
# invariants keep that shut:
#   1. No allowlist in run-agent.sh may use an MCP wildcard.
#   2. Every write-capable MCP verb is named on the shared deny list.
MCP_TOKEN_RE = re.compile(r"mcp__claude_ai_[A-Za-z_]+__[A-Za-z_]*\*?")

REQUIRED_DENIED_TOOLS = [
    # Gmail: anything that puts mail in front of a human, or removes it.
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Gmail__trash_message",
    "mcp__claude_ai_Gmail__trash_thread",
    # Google Calendar: every write verb (button-required tier).
    "mcp__claude_ai_Google_Calendar__create_event",
    "mcp__claude_ai_Google_Calendar__update_event",
    "mcp__claude_ai_Google_Calendar__delete_event",
    "mcp__claude_ai_Google_Calendar__respond_to_event",
    # Google Drive: plugins get the Drive wildcard; nothing may change or share a file.
    "mcp__claude_ai_Google_Drive__create_file",
    "mcp__claude_ai_Google_Drive__update_file",
    "mcp__claude_ai_Google_Drive__copy_file",
    "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Google_Drive__share_file",
    # Slack MCP is read-only by design.
    "mcp__claude_ai_Slack__slack_send_message",
]

# Bash levers with external effect that are NOT graph_actions.py verbs.
# The resident runs under --dangerously-skip-permissions, where this file
# is the only layer left, so each needs an entry.
REQUIRED_DENIED_BASH = [
    "gmail_client.py send-draft",
    # google_calendar_client.py create-event is deliberately allowed: the
    # EA-Managed auto-hold carve-out (mail_mediation.is_ea_managed_autohold)
    # is the documented un-gated path, and the verb has no attendee
    # parameter so it cannot invite anyone.
    "scheduled_sends.py add",
    "graph_client",  # direct `python3 -c "import graph_client"` imports
    # Publishing outside the host runs only on the owner's click
    # (publish_gate.py); the lanes never call the verbs themselves.
    "site_tools.py publish",
    "overleaf_tools.py push",
    "git push",
]

# The cron lanes run with no one watching. The model's own web tools are
# neither granted there nor left to auto mode: each runner denies them.
CRON_LANE_WEB_TOOLS = ("WebFetch", "WebSearch")

# Helpers that MUST be in the allowlist because skills or direct
# Slack workflows depend on them. Keep this list explicit rather than
# auto-discovered: not every .py in preprocessor/ is user-facing, and
# we want this to break loudly if someone removes a helper.
REQUIRED_HELPERS = [
    "graph_actions.py",
    "google_calendar_client.py",
    "scheduling_engine.py",
    "fetch_url.py",
    "email_to_pdf.py",
    # The only reader for a .docx: dontAsk lanes refuse unzip and python3 -c.
    "doc_text.py",
    "poll_filler.py",
    "booking_link.py",
    "compile_letter.py",
    "ledger.py",
    # Both are MANDATED by CLAUDE.md prose, so a missing grant is a live
    # permission-gate failure: log_missed_closure.py by the
    # "User-stated closure is a bug" section, slack_upload.py as the
    # documented thread-attachment/staging path.
    "log_missed_closure.py",
    "slack_upload.py",
    # The same staging path under its frontend-neutral name.
    "chat_upload.py",
    # The conversation index every room is told to consult (CLAUDE.md,
    # "Channels, threads, DMs and the shell").
    "session_index.py",
    # The personalization store: CLAUDE.md "Personal rules" tells every lane
    # to record user corrections with `preferences.py add`.
    "preferences.py",
    "onboarding.py",
    # docs/ea/overleaf.md runs every Overleaf git step through this one CLI.
    "overleaf_tools.py",
]


def _deny_entries() -> list[str]:
    """Parse scripts/disallowed-tools.txt the way the runtime does."""
    if not DENY_FILE.exists():
        return []
    return [ln.strip() for ln in DENY_FILE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# --- run-agent.sh ALLOWED_TOOLS --------------------------------------
# The block is multi-statement shell (MCP_GMAIL / MCP_GCAL are
# built with `+=` and then concatenated into ALLOWED_TOOLS, which is
# itself built with `+=`), so parse it as assignments and expand the
# variable references. Anything unexpected fails loudly rather than
# silently matching nothing.
_SHELL_ASSIGN_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\+?=)\s*"([^"]*)"\s*$')
_SHELL_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')

# Shell variables set elsewhere in run-agent.sh; the value does not matter
# to this test, only that the grant resolves to a path under them.
_EXTERNAL_VARS = {
    "EA_BOT_SCRIPT_DIR": "/home/ea-bot/ea-system",
    "EA_DIR": "/home/ea-bot/.ea",
    "EA_BOT_HOME": "/home/ea-bot",
    "SCRIPT_DIR": "/home/crh/ea-system",
    # `MCP_CHAT=$(chat_notify.py llm-grants)`: the Slack frontend's grants.
    "MCP_CHAT": ("mcp__claude_ai_Slack__slack_read_channel,"
                 "mcp__claude_ai_Slack__slack_read_thread,"
                 "mcp__claude_ai_Slack__slack_read_user_profile"),
}

# CLIs in preprocessor/ that the LLM never runs: they hold the chat token
# (crh-side) and serve the cron notifier and the briefing reader.
NOT_AGENT_CLIS = {"chat_read.py", "chat_notify.py"}

# Minimum plausible size of the parsed block. A structural change that
# breaks the parse would otherwise show up as "zero grants, all invariants
# vacuously satisfied".
_MIN_EXPECTED_GRANTS = 15


def parse_run_agent_allowed_tools() -> tuple[list[str], list[str]]:
    """Return (grants, problems) for run-agent.sh's ALLOWED_TOOLS.

    Evaluates the assignment statements in order — `NAME="..."` and
    `NAME+="..."` — resolving ${VAR} references against what has been
    assigned so far plus _EXTERNAL_VARS. Comment lines are skipped, so
    the commented-out wildcards named in the file's own history are not
    read as grants.
    """
    problems: list[str] = []
    if not RUN_AGENT.exists():
        return [], [f"{RUN_AGENT} not found"]

    env: dict[str, str] = dict(_EXTERNAL_VARS)
    seen_allowed = False
    for raw in RUN_AGENT.read_text().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = _SHELL_ASSIGN_RE.match(raw)
        if not m:
            continue
        name, op, value = m.group(1), m.group(2), m.group(3)
        if name != "ALLOWED_TOOLS" and not name.startswith("MCP_"):
            continue

        def _sub(mm, _env=env, _problems=problems):
            var = mm.group(1) or mm.group(2)
            if var not in _env:
                _problems.append(
                    f"run-agent.sh ALLOWED_TOOLS references ${{{var}}}, "
                    f"which this test cannot resolve — add it to "
                    f"_EXTERNAL_VARS in tests/test_allowed_tools.py")
                return ""
            return _env[var]

        expanded = _SHELL_VAR_RE.sub(_sub, value)
        env[name] = (env.get(name, "") + expanded) if op == "+=" else expanded
        if name == "ALLOWED_TOOLS":
            seen_allowed = True

    if not seen_allowed:
        problems.append(
            "could not locate any ALLOWED_TOOLS= assignment in run-agent.sh "
            "(was the block restructured? this test needs updating)")
        return [], problems

    grants = [g.strip() for g in env["ALLOWED_TOOLS"].split(",") if g.strip()]
    if len(grants) < _MIN_EXPECTED_GRANTS:
        problems.append(
            f"parsed only {len(grants)} grant(s) from run-agent.sh "
            f"ALLOWED_TOOLS (expected >= {_MIN_EXPECTED_GRANTS}) — the "
            f"parse is probably broken, not the allowlist")
    return grants, problems


def cron_agent_skills() -> list[Path]:
    """Skill files reachable from a run-agent.sh case arm.

    Read out of the script rather than hardcoded, so an added or removed
    agent arm changes the required helper set automatically.
    """
    src = RUN_AGENT.read_text()
    names = sorted(set(re.findall(
        r'SKILL_FILE="\$SKILL_DIR/([A-Za-z0-9._-]+\.md)"', src)))
    return [SKILLS / n for n in names if (SKILLS / n).exists()]


_SKILL_INVOCATION_RE = re.compile(
    r"""python3?\s+[^\s`"']*?([a-z_][a-z0-9_]*\.py)""")


def required_cron_helpers() -> dict[str, list[str]]:
    """{helper.py: [skills that invoke it]} for the cron lane.

    Derived from what the skills behind run-agent.sh's arms actually run.
    Only preprocessor/ scripts are required here: plugin scripts (a
    plugin's collect.py) get one rule per entry point from
    `plugin_registry.py tool-grants` and live outside the repo.
    """
    out: dict[str, list[str]] = {}
    for skill in cron_agent_skills():
        for helper in sorted(set(_SKILL_INVOCATION_RE.findall(
                skill.read_text()))):
            if (PREPROC / helper).exists():
                out.setdefault(helper, []).append(skill.name)
    return out


def check_run_agent_allowlist() -> list[str]:
    """(b)+(a) for run-agent.sh: nothing denied is granted, and every
    helper the cron skills invoke is granted."""
    grants, problems = parse_run_agent_allowed_tools()
    if not grants:
        return problems

    blob = "\n".join(grants)
    for tool in REQUIRED_DENIED_TOOLS:
        if tool in grants:
            problems.append(
                f"run-agent.sh grants {tool}, which the deny list forbids")

    required = required_cron_helpers()
    for helper, skills in sorted(required.items()):
        if f"preprocessor/{helper}" not in blob:
            problems.append(
                f"run-agent.sh ALLOWED_TOOLS omits preprocessor/{helper}, "
                f"invoked by {', '.join(skills)} — a cron agent "
                f"(claude -p, no human to approve) would hit the "
                f"permission gate")

    # CLAUDE.md mandates these two in prose for every lane.
    for helper in ("log_missed_closure.py", "slack_upload.py"):
        if f"preprocessor/{helper}" not in blob:
            problems.append(
                f"run-agent.sh ALLOWED_TOOLS omits preprocessor/{helper}, "
                f"which CLAUDE.md mandates")

    if not required:
        problems.append(
            "derived zero required helpers from the cron-arm skills — the "
            "SKILL_FILE parse or the skills/ layout changed")
    return problems


PLUGIN_RUNNER = REPO / "run-plugin.sh"
DRIVE_READ_VERBS = {
    "mcp__claude_ai_Google_Drive__search_files",
    "mcp__claude_ai_Google_Drive__list_recent_files",
    "mcp__claude_ai_Google_Drive__get_file_metadata",
    "mcp__claude_ai_Google_Drive__get_file_permissions",
    "mcp__claude_ai_Google_Drive__read_file_content",
    "mcp__claude_ai_Google_Drive__download_file_content",
}


def check_plugin_lane_mcp_grants(problems: list) -> None:
    """run-plugin.sh grants MCP verbs the same way run-agent.sh does: read
    verbs spelled out, never a wildcard that would hand back the denied
    write verbs."""
    src = PLUGIN_RUNNER.read_text()
    for m in re.finditer(r"mcp__[A-Za-z0-9_]+__\*", src):
        problems.append(f"run-plugin.sh grants an MCP wildcard: {m.group(0)}")
    granted = set(re.findall(r"'(mcp__[A-Za-z0-9_]+)'", src))
    agent_reads = set(re.findall(r"mcp__claude_ai_Google_Calendar__[a-z_]+", RUN_AGENT.read_text()))
    for verb in sorted(granted):
        if verb.startswith("mcp__claude_ai_Google_Calendar__") and verb not in agent_reads:
            problems.append(f"run-plugin.sh grants a calendar verb run-agent.sh does not: {verb}")
        if verb.startswith("mcp__claude_ai_Google_Drive__") and verb not in DRIVE_READ_VERBS:
            problems.append(f"run-plugin.sh grants a Drive verb that is not a read: {verb}")


def check_cron_lane_web_tools() -> list[str]:
    """run-agent.sh grants no web tool of the model's own, and both cron
    runners add WebFetch and WebSearch to the deny list they pass."""
    problems = []
    grants, _ = parse_run_agent_allowed_tools()
    for tool in CRON_LANE_WEB_TOOLS:
        if tool in grants:
            problems.append(f"run-agent.sh grants {tool}; a cron skill reads a page "
                            f"with fetch_url.py, which is logged")
    for runner in (RUN_AGENT, PLUGIN_RUNNER):
        code = "\n".join(ln for ln in runner.read_text().splitlines()
                         if not ln.lstrip().startswith("#"))
        m = re.search(r'^CRON_WEB_DENY="([^"]*)"$', code, re.M)
        if not m or set(m.group(1).split(",")) != set(CRON_LANE_WEB_TOOLS):
            problems.append(f"{runner.name} does not set CRON_WEB_DENY to "
                            f"{','.join(CRON_LANE_WEB_TOOLS)}")
        if 'DISALLOWED_TOOLS+="${DISALLOWED_TOOLS:+,}$CRON_WEB_DENY"' not in code:
            problems.append(f"{runner.name} does not add CRON_WEB_DENY to DISALLOWED_TOOLS")
        if '"$DISALLOWED_TOOLS"' not in code:
            problems.append(f"{runner.name} does not pass DISALLOWED_TOOLS to claude")
    return problems


def check_gates() -> list[str]:
    """Return a list of gate violations (empty == healthy)."""
    problems = []

    deny = _deny_entries()
    if not deny:
        return [f"{DENY_FILE} is missing or empty — every gated verb is open"]
    deny_blob = "\n".join(deny)

    for tool in REQUIRED_DENIED_TOOLS:
        if tool not in deny:
            problems.append(
                f"{tool} is not on scripts/disallowed-tools.txt")
    for frag in REQUIRED_DENIED_BASH:
        if frag not in deny_blob:
            problems.append(
                f"no scripts/disallowed-tools.txt entry covers '{frag}'")

    if not RUN_AGENT.exists():
        problems.append(f"{RUN_AGENT} not found")
        return problems

    # Comment lines name the removed wildcards on purpose; only scan code.
    code = "\n".join(ln for ln in RUN_AGENT.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    granted = set(MCP_TOKEN_RE.findall(code))
    for tool in sorted(granted):
        if tool.endswith("__*") or tool.endswith("*"):
            problems.append(
                f"run-agent.sh grants the MCP wildcard {tool}; enumerate "
                f"the read-only verbs instead")
        elif tool in REQUIRED_DENIED_TOOLS:
            problems.append(
                f"run-agent.sh grants {tool}, which the deny list forbids")

    if PLUGIN_RUNNER.exists():
        check_plugin_lane_mcp_grants(problems)

    return problems


def main():
    bot_src = BOT.read_text()

    # Extract the _BASH_ONLY_TOOLS tuple. The inner Bash(...) entries
    # contain nested parentheses, so use a line-anchored terminator
    # rather than a greedy .*? which would stop at the first ")".
    m = re.search(
        r"_BASH_ONLY_TOOLS\s*=\s*\((.*?)\n\)",
        bot_src, flags=re.DOTALL,
    )
    if not m:
        print("FAIL: could not locate _BASH_ONLY_TOOLS in ea-chat-bot.py")
        print("       (was the name changed? this test needs updating)")
        sys.exit(1)
    allowlist = m.group(1)

    missing = [h for h in REQUIRED_HELPERS if h not in allowlist]
    unknown_helpers = []

    # Also flag any .py in preprocessor/ that isn't documented here;
    # they're either safe to omit (import-only library modules) or
    # should be added to REQUIRED_HELPERS. Either way: surface it.
    for p in sorted(PREPROC.glob("*.py")):
        if p.name in ("__init__.py",):
            continue
        text = p.read_text(errors="ignore")
        is_cli = "__name__" in text and "__main__" in text and (
            "argparse" in text or "sub.add_parser" in text
        )
        if is_cli and p.name not in REQUIRED_HELPERS and p.name not in NOT_AGENT_CLIS:
            unknown_helpers.append(p.name)

    if missing:
        print(f"FAIL: {len(missing)} required helper(s) missing from "
              f"_BASH_ONLY_TOOLS in ea-chat-bot.py:")
        for h in missing:
            print(f"    - {h}")
        print()
        print("Add each to _BASH_ONLY_TOOLS as:")
        print('    "Bash(python3 {script_dir}/preprocessor/<name> *),"')

    if unknown_helpers:
        print(f"NOTE: {len(unknown_helpers)} CLI helper(s) in preprocessor/ "
              f"not listed in tests/test_allowed_tools.py REQUIRED_HELPERS:")
        for h in unknown_helpers:
            print(f"    - {h}")
        print("  If they're reachable from a skill, add them here and to "
              "_BASH_ONLY_TOOLS.")
        print("  If they're internal (utility scripts, not agent-reachable), "
              "leave this note as-is.")

    gate_problems = (check_gates() + check_run_agent_allowlist()
                     + check_cron_lane_web_tools())
    if gate_problems:
        print(f"FAIL: {len(gate_problems)} tool-gate violation(s):")
        for p in gate_problems:
            print(f"    - {p}")
        print()
        print("The deny list is scripts/disallowed-tools.txt; the cron "
              "allowlist is the ALLOWED_TOOLS block in run-agent.sh.")

    if missing or gate_problems:
        sys.exit(1)
    grants, _ = parse_run_agent_allowed_tools()
    required = required_cron_helpers()
    print(f"OK: all {len(REQUIRED_HELPERS)} required helpers are allowlisted")
    print(f"OK: {len(REQUIRED_DENIED_TOOLS)} write-capable MCP verbs denied, "
          f"no MCP wildcard in run-agent.sh")
    print("OK: the cron lanes (run-agent.sh, run-plugin.sh) grant and leave open "
          "no WebFetch or WebSearch")
    print(f"OK: run-agent.sh ALLOWED_TOOLS parsed ({len(grants)} grants); "
          f"all {len(required)} helper(s) the cron-arm skills invoke "
          f"({', '.join(sorted(required))}) are granted")
    if unknown_helpers:
        # Non-fatal: we warn but pass.
        sys.exit(0)


if __name__ == "__main__":
    main()
