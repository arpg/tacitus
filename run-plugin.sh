#!/usr/bin/env bash
#
# EA Plugin Orchestrator
#
# Runs one personal plugin on its own schedule, with isolated logs,
# scoped context, and per-plugin Slack output. Plugins live at
# ~/.ea/plugins/<name>/ (a clone of <your account>/ea-plugin-<short-name>).
#
# Usage:
#   ./run-plugin.sh <plugin-name> [-- <collect.py args>]
#   ./run-plugin.sh --check-schedules   # reconcile manifests vs crontab
#
# See docs/plugins.md for the contract.
#
set -euo pipefail

# -------------------------------------------------------------------
# Bootstrap (mirrors run-agent.sh)
# -------------------------------------------------------------------
EA_DIR="${EA_DIR:-$HOME/.ea}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Every file under EA_DIR is ea-bot space: this script reads and writes it
# through scripts/owner-io.sh, which does so as ea-bot. EA_DIR resolves
# through the owner's own symlinks only.
OWNER_IO="$SCRIPT_DIR/scripts/owner-io.sh"
_ea_dir="$("$OWNER_IO" check "$EA_DIR" | cut -f2-)" || _ea_dir=""
if [[ -n "$_ea_dir" ]]; then
    EA_DIR="$_ea_dir"
fi
unset _ea_dir
# Config and secrets load as data, never as shell (owner-io.sh env).
while IFS= read -r -d '' _env_key && IFS= read -r -d '' _env_val; do
    if [[ "$_env_key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
        export "$_env_key=$_env_val"
    fi
done < <("$OWNER_IO" env "$EA_DIR" "${EA_PRIVATE_DIR:-$HOME/.ea-private}")
unset _env_key _env_val

if [[ -f "$SCRIPT_DIR/.venv/bin/python3" ]]; then
    export PATH="$SCRIPT_DIR/.venv/bin:$PATH"
fi
PYTHON3="$(command -v python3)"

# -------------------------------------------------------------------
# Schedule reconciliation:  run-plugin.sh --check-schedules
# -------------------------------------------------------------------
# scripts/install/setup.sh emits cron lines only when an operator re-runs it by hand, so
# flipping `enabled` or editing `schedule` in a manifest changes nothing
# on its own. This mode detects the resulting drift in both directions:
# an enabled plugin with no cron line, and a disabled plugin still fired
# by one. It reads only; it never edits cron. Exit 0 clean, 1 on drift,
# so a cron/sentinel caller can alert on it.
if [[ "${1:-}" == "--check-schedules" ]]; then
    "$PYTHON3" - "$EA_DIR" "$SCRIPT_DIR" <<'PYEOF'
import json
import re
import subprocess
import sys
from pathlib import Path

ea_dir = Path(sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[2]) / "preprocessor"))
plugins_dir = ea_dir / "plugins"
manifests = sorted(plugins_dir.glob("*/plugin.json"))
if not manifests:
    print(f"no plugin manifests under {plugins_dir}")
    sys.exit(0)

try:
    cron = subprocess.run(["crontab", "-l"], capture_output=True,
                          text=True).stdout
except FileNotFoundError:
    print("crontab not available on this host; cannot reconcile")
    sys.exit(0)


def schedule_of(line):
    """The schedule field of a crontab line ('@daily' or the 5 fields)."""
    parts = line.split()
    if not parts:
        return ""
    if parts[0].startswith("@"):
        return parts[0]
    return " ".join(parts[:5])


problems = []
rows = []
for mp in manifests:
    try:
        m = json.loads(mp.read_text())
    except Exception as e:
        problems.append(f"{mp}: unreadable manifest ({e})")
        continue
    name = m.get("name") or mp.parent.name
    wanted = []
    if m.get("manifest_version") == 2:
        # A v2 plugin: the owner's lock decides (docs/plugins.md).
        import plugin_registry
        lp = plugin_registry.get_plugin(name)
        enabled = lp is not None
        scheds = lp.manifest["capabilities"]["schedules"] if lp else []
        schedule = scheds[0]["cron"] if scheds else ""
        wanted = [s["cron"] for s in scheds]
    else:
        enabled = bool(m.get("enabled"))
        schedule = (m.get("schedule") or "").strip()
        if enabled and schedule:
            wanted.append(schedule)
            for ex in (m.get("extra_schedules") or []):
                if ex.get("cron"):
                    wanted.append(ex["cron"].strip())

    hit = re.compile(r"run-plugin\.sh\s+" + re.escape(name) + r"(?:\s|$)")
    found = [schedule_of(ln) for ln in cron.splitlines()
             if hit.search(ln) and not ln.lstrip().startswith("#")]

    state = "enabled" if enabled else "disabled"
    rows.append((name, state, schedule or "-", len(wanted), len(found)))

    if not enabled and found:
        problems.append(
            f"{name}: DISABLED in plugin.json but {len(found)} cron line(s) "
            f"still fire it ({', '.join(found)}) — every run is a no-op")
    if enabled and schedule and not found:
        problems.append(
            f"{name}: enabled with schedule {schedule!r} but NO cron line "
            f"invokes it — its scheduled pass never runs")
    for w in wanted:
        if found and w not in found:
            problems.append(
                f"{name}: manifest schedule {w!r} has no matching cron line "
                f"(cron has {', '.join(found)})")
    for f in found:
        if f not in wanted and enabled:
            problems.append(
                f"{name}: cron line {f!r} matches no manifest schedule")

print(f"  {'plugin':24s} {'state':9s} {'schedule':16s} want  cron")
for name, state, schedule, nw, nf in rows:
    print(f"  {name:24s} {state:9s} {schedule:16s} {nw:4d}  {nf:4d}")

if problems:
    print(f"\n  {len(problems)} schedule drift(s):")
    for p in problems:
        print(f"    - {p}")
    print("\n  Re-run scripts/install/setup.sh to regenerate the plugin cron block, or fix "
          "the offending lines with `crontab -e`.")
    sys.exit(1)
print("\n  OK: every manifest agrees with the crontab")
PYEOF
    exit $?
fi

# Master EA switch — same gate as run-agent.sh. A user disabling EA also
# pauses plugins, since they share infrastructure.
EA_ENABLED=$("$PYTHON3" -c "
import json, sys; from pathlib import Path
p = Path(sys.argv[1])
print(json.loads(p.read_text()).get('ea_enabled', True) if p.exists() else True)
" "$EA_DIR/config/ea-config.json" 2>/dev/null || echo "True")
if [[ "$EA_ENABLED" == "False" ]]; then
    echo "[$(date +%H:%M:%S)] EA paused (ea_enabled=false in ea-config.json). Skipping plugin."
    exit 0
fi

PLUGIN_NAME="${1:?Usage: run-plugin.sh <plugin-name> [-- collect.py args...]}"
shift || true
# Remaining args (if any) are forwarded verbatim to the plugin's collect.py.
# A leading "--" is optional and stripped. Used so a second schedule can
# call `run-plugin.sh <name> -- --nightly`.
if [[ "${1:-}" == "--" ]]; then
    shift
fi
# `${arr[@]+"${arr[@]}"}` below: on bash 3.2 (macOS) with set -u an empty
# array expands as an unbound variable; that form expands to nothing.
COLLECT_EXTRA_ARGS=("$@")

PLUGIN_DIR="$EA_DIR/plugins/$PLUGIN_NAME"
MANIFEST="$PLUGIN_DIR/plugin.json"

if [[ ! -f "$MANIFEST" ]]; then
    echo "FATAL: plugin manifest not found: $MANIFEST" >&2
    exit 1
fi

# -------------------------------------------------------------------
# Parse manifest with python (jq is not guaranteed present)
# -------------------------------------------------------------------
# The manifest is ea-bot space and its fields reach the prompt and the
# Claude arguments, so it is read once, as ea-bot, and parsed from memory.
MANIFEST_JSON=$("$OWNER_IO" read "$MANIFEST") || {
    echo "FATAL: plugin manifest not readable: $MANIFEST" >&2
    exit 1
}
# A plugin the owner's lock names runs from the manifest the owner
# accepted, whatever the live plugin.json says now: the live file decides
# nothing for a locked plugin (docs/plugins.md). A locked plugin that is
# disabled, or whose files no longer match the lock, has no manifest here
# and is skipped below.
LOCKED=0
if "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" locked --plugin "$PLUGIN_NAME" 2>/dev/null; then
    LOCKED=1
    # The files must match the hashes the owner's lock recorded at install,
    # or nothing of the plugin runs: its collect step and the scripts its
    # allow list grants are ea-bot space. A mismatch posts to the health
    # channel (once per problem set) and fails the run.
    set +e
    VERIFY_ERR=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" verify \
        --plugin "$PLUGIN_NAME" --alert 2>&1 >/dev/null)
    VERIFY_RC=$?
    set -e
    if [[ "$VERIFY_RC" == "3" ]]; then
        echo "[$(date +%H:%M:%S)] REFUSED: plugin $PLUGIN_NAME does not match the owner's lock:" >&2
        printf '%s\n' "$VERIFY_ERR" | head -10 >&2
        exit 1
    fi
    if LOCK_MANIFEST=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" manifest --plugin "$PLUGIN_NAME" 2>/dev/null); then
        MANIFEST_JSON="$LOCK_MANIFEST"
    else
        echo "[$(date +%H:%M:%S)] Plugin $PLUGIN_NAME is in the owner's lock but not enabled there (or its files changed). Skipping."
        exit 0
    fi
fi
read_manifest_field() {
    printf '%s' "$MANIFEST_JSON" | "$PYTHON3" -c "
import json, sys
m = json.load(sys.stdin)
keys = sys.argv[1].split('.')
v = m
for k in keys:
    v = v.get(k) if isinstance(v, dict) else None
    if v is None:
        sys.exit(0)
if isinstance(v, (list, dict)):
    print(json.dumps(v))
else:
    print(v)
" "$1"
}

# A v2 manifest (docs/plugins.md) runs from the owner's lock: the lock
# decides whether it is enabled, and the schedule that matches the extra
# arguments gives the model, the timeout, the skill and which model writes
# the post. A v1 manifest decides all of that itself.
LLM_MODE="claude"
SKILL_REL="skill.md"
if [[ "$LOCKED" == "1" || "$(read_manifest_field "manifest_version")" == "2" ]]; then
    SCHED_JSON=$(printf '%s' "$MANIFEST_JSON" | "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" \
        schedule-config --plugin "$PLUGIN_NAME" -- ${COLLECT_EXTRA_ARGS[@]+"${COLLECT_EXTRA_ARGS[@]}"} 2>/dev/null) || SCHED_JSON='{}'
    sched_field() {
        printf '%s' "$SCHED_JSON" | "$PYTHON3" -c "
import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print('' if v is None else v)
" "$1"
    }
    ENABLED=$(sched_field enabled)
    LLM_MODE=$(sched_field llm)
    SKILL_REL=$(sched_field skill)
    [[ -n "$SKILL_REL" ]] || SKILL_REL="skill.md"
else
    ENABLED=$(read_manifest_field "enabled")
fi
if [[ "$ENABLED" != "True" && "$ENABLED" != "true" ]]; then
    echo "[$(date +%H:%M:%S)] Plugin $PLUGIN_NAME is disabled (plugin.json: enabled=$ENABLED). Skipping."
    exit 0
fi

if [[ -n "${SCHED_JSON:-}" ]]; then
    MODEL=$(sched_field model)
    TIMEOUT=$(sched_field timeout_seconds)
else
    MODEL=$(read_manifest_field "model")
    TIMEOUT=$(read_manifest_field "timeout_seconds")
fi
[[ -z "$MODEL" ]] && MODEL="sonnet"
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || TIMEOUT=1800
CHANNEL_KEY=$(read_manifest_field "output.channel_key")
ANCHORS_JSON=$(read_manifest_field "output.format_anchors")

# -------------------------------------------------------------------
# Logging — per-plugin, per-day
# -------------------------------------------------------------------
# Owner-written per-run plugin logs -> owner-private dir (ea-bot cannot reach).
LOG_DIR="$HOME/.ea-private/logs/plugins/$PLUGIN_NAME"
mkdir -p "$LOG_DIR"
chmod 700 "$HOME/.ea-private/logs" 2>/dev/null || true
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "Starting plugin run: $PLUGIN_NAME (model=$MODEL, channel=$CHANNEL_KEY)"

# -------------------------------------------------------------------
# Resolve ea-bot home (subprocess user)
# -------------------------------------------------------------------
EA_BOT_HOME=$("$PYTHON3" -c '
import pwd, sys
try: print(pwd.getpwnam("ea-bot").pw_dir)
except KeyError: sys.exit(1)' 2>/dev/null) || {
    log "FATAL: Unix user 'ea-bot' does not exist."
    exit 2
}
EA_BOT_SCRIPT_DIR="$EA_BOT_HOME/ea-system"
EA_BOT_PLUGIN_DIR="$EA_BOT_HOME/.ea/plugins/$PLUGIN_NAME"
EA_BOT_PLUGIN_STATE="$EA_BOT_PLUGIN_DIR/state"
EA_BOT_COLLECT_OUT="$EA_BOT_PLUGIN_STATE/last-collect.json"

# Defense-in-depth: deny gated send/event subcommands. Plugins go through
# their own narrow allowedTools whitelist (manifest context grants), but a
# malformed manifest could grant broader graph_actions access; this stays a
# hard deny regardless. See scripts/disallowed-tools.txt for the canonical
# list. The lists and the profile reader come from this script's own tree:
# code run here runs as the owner, and a deny list from a tree ea-bot can
# replace would let it drop its own denials.
DISALLOWED_FILE="$SCRIPT_DIR/scripts/disallowed-tools.txt"
if [[ -f "$DISALLOWED_FILE" ]]; then
    DISALLOWED_TOOLS=$(grep -v '^[[:space:]]*#' "$DISALLOWED_FILE" \
        | sed '/^[[:space:]]*$/d' | paste -sd ',' -)
else
    DISALLOWED_TOOLS=""
    log "WARNING: $DISALLOWED_FILE missing; running without --disallowedTools"
fi
# The review-only profile also denies every drafting verb.
if [[ "$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/ea_config.py" profile 2>/dev/null)" == "review-only" \
      && -f "$SCRIPT_DIR/scripts/disallowed-tools.review-only.txt" ]]; then
    DISALLOWED_TOOLS+=",$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/scripts/disallowed-tools.review-only.txt" \
        | sed '/^[[:space:]]*$/d' | paste -sd ',' -)"
fi
# Denials the enabled plugins declare (docs/plugins.md), from the owner's lock.
PLUGIN_DENIED=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" denied-tools 2>/dev/null || true)
[[ -n "$PLUGIN_DENIED" ]] && DISALLOWED_TOOLS+="${DISALLOWED_TOOLS:+,}$PLUGIN_DENIED"
# A scheduled plugin run has no web tools of the model's own: no manifest
# can grant one, and they are denied too, since ea-bot's settings run auto
# mode. A plugin that reads a page uses fetch_url.py (context.fetch_url),
# whose every request is logged and reviewed daily.
CRON_WEB_DENY="WebFetch,WebSearch"
DISALLOWED_TOOLS+="${DISALLOWED_TOOLS:+,}$CRON_WEB_DENY"

# Resolve a python3 path that ea-bot can execute. ea-bot cannot traverse
# the owner's home, so $PYTHON3 (which `command -v` may resolve to the source-
# tree venv under the owner's home) is unusable from the sudo subshell.
# Preference order: ea-bot's dedicated venv (plugin-specific deps such as
# playwright live there), then a real venv under the deployed tree, then
# system python3.
if [[ -x "$EA_BOT_HOME/.local/venv/bin/python3" ]]; then
    EA_BOT_PYTHON3="$EA_BOT_HOME/.local/venv/bin/python3"
elif [[ -x "$EA_BOT_SCRIPT_DIR/.venv/bin/python3" ]]; then
    EA_BOT_PYTHON3="$EA_BOT_SCRIPT_DIR/.venv/bin/python3"
else
    EA_BOT_PYTHON3="/usr/bin/python3"
fi

# -------------------------------------------------------------------
# Step 1: Run collect.py (the plugin's preprocessor)
# -------------------------------------------------------------------
log "Running collect.py..."
"$OWNER_IO" mkdir "$EA_BOT_PLUGIN_STATE" 2>/dev/null || true

# Plugin-local secrets from .env.secrets, exposed as env vars to collect.py
# and Claude. The file is ea-bot space, so it is read as ea-bot and parsed
# as data (owner-io.sh kv): each KEY=VALUE line becomes one `env` argument,
# with no word splitting or globbing, and the loader and interpreter
# variables are refused.
PLUGIN_SECRETS_ENV=()
while IFS= read -r -d '' _kv; do
    PLUGIN_SECRETS_ENV+=("$_kv")
done < <("$OWNER_IO" kv "$EA_BOT_PLUGIN_DIR/.env.secrets" 2>>"$LOG_FILE")
unset _kv

if (( ${#COLLECT_EXTRA_ARGS[@]} )); then
    log "collect.py extra args: ${COLLECT_EXTRA_ARGS[*]}"
fi
# The JSON goes to ea-bot's state dir, written as ea-bot.
if ! sudo -u ea-bot env \
    HOME="$EA_BOT_HOME" \
    EA_DIR="$EA_DIR" \
    EA_PLUGIN_NAME="$PLUGIN_NAME" \
    EA_PLUGIN_DIR="$EA_BOT_PLUGIN_DIR" \
    PYTHONPATH="$EA_BOT_SCRIPT_DIR/preprocessor:$EA_BOT_SCRIPT_DIR" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    "${PLUGIN_SECRETS_ENV[@]}" \
    "$EA_BOT_PYTHON3" "$EA_BOT_PLUGIN_DIR/collect.py" ${COLLECT_EXTRA_ARGS[@]+"${COLLECT_EXTRA_ARGS[@]}"} \
    2>>"$LOG_FILE" \
    | "$OWNER_IO" write "$EA_BOT_COLLECT_OUT" 2>>"$LOG_FILE"; then
    log "collect.py FAILED. See log for details. Aborting (no digest posted)."
    # A failed collect posts no digest, so say that it failed, at most once
    # a day per plugin; a success clears the mark. The text goes through the
    # environment so quotes in an error cannot break the Python literal.
    ALERT_MARK="$EA_BOT_PLUGIN_STATE/.collect-failed-alerted"
    if [[ ! -f "$ALERT_MARK" || -n "$(find "$ALERT_MARK" -mmin +1440 2>/dev/null)" ]]; then
        LAST_ERR=$(grep -E "Error|Exception" "$LOG_FILE" 2>/dev/null | tail -1 | cut -c1-200)
        ALERT_TEXT=":warning: *plugin:$PLUGIN_NAME could not collect its data*, so it posted no digest this run. ${LAST_ERR} Log: $LOG_FILE" \
        ALERT_KEY="$CHANNEL_KEY" "$PYTHON3" -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
slack.post_to_slack(os.environ['ALERT_KEY'], os.environ['ALERT_TEXT'])
" 2>&1 | tee -a "$LOG_FILE" || true
        "$OWNER_IO" touch "$ALERT_MARK" 2>/dev/null || true
    fi
    exit 3
fi
"$OWNER_IO" rm "$EA_BOT_PLUGIN_STATE/.collect-failed-alerted" 2>/dev/null || true

COLLECT_SIZE=$(stat -c%s "$EA_BOT_COLLECT_OUT" 2>/dev/null || echo 0)
log "collect.py produced ${COLLECT_SIZE} bytes of JSON."

# Deterministic short-circuit: if collect.py emitted `"should_post": false`,
# skip the Claude invocation entirely. Saves tokens AND avoids the model's
# tendency to write meta-commentary about why it's "supposed to" stay silent.
# Plugins opt in by including this field in their JSON; absence is treated
# as should_post=true (default to running Claude).
SHOULD_POST=$("$PYTHON3" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print('false' if d.get('should_post') is False else 'true')
except Exception:
    print('true')
" "$EA_BOT_COLLECT_OUT" 2>/dev/null)
if [[ "$SHOULD_POST" == "false" ]]; then
    SKIP_REASON=$("$PYTHON3" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('should_post_reason', '(no reason given)'))
except Exception:
    print('(unparseable)')
" "$EA_BOT_COLLECT_OUT" 2>/dev/null)
    log "Plugin signaled no-op via should_post=false: $SKIP_REASON"
    log "Skipping Claude invocation and Slack post."
    # No-op runs still commit pending state: there's nothing to confirm
    # via Slack, so any pending state is equivalent to committed state.
    # Leaving the pending file would just accumulate stale data.
    PENDING_STATE="$EA_BOT_PLUGIN_STATE/state.pending.json"
    COMMITTED_STATE="$EA_BOT_PLUGIN_STATE/state.json"
    if [[ -f "$PENDING_STATE" ]]; then
        if "$OWNER_IO" mv "$PENDING_STATE" "$COMMITTED_STATE" 2>>"$LOG_FILE"; then
            log "Committed plugin state (no-op run)."
        else
            log "WARNING: mv $PENDING_STATE -> $COMMITTED_STATE failed."
        fi
    fi
    exit 0
fi

# -------------------------------------------------------------------
# Step 1b: a v2 schedule whose post Claude does not write (llm none,
# remote or remote-first; docs/plugins.md). plugin_llm.py writes and
# posts it from private copies of the skill and the collected JSON. Its
# exit 3 hands a remote-first post to Claude below.
# -------------------------------------------------------------------
if [[ "$LLM_MODE" == "none" || "$LLM_MODE" == "remote" || "$LLM_MODE" == "remote-first" ]]; then
    LLM_DIR=$(mktemp -d "$LOG_DIR/.llm.XXXXXX")
    "$OWNER_IO" read "$EA_BOT_COLLECT_OUT" > "$LLM_DIR/collected.json" 2>>"$LOG_FILE" || true
    LLM_SKILL_ARGS=()
    if [[ "$LLM_MODE" != "none" ]]; then
        "$OWNER_IO" read "$EA_BOT_PLUGIN_DIR/$SKILL_REL" > "$LLM_DIR/skill.md" 2>>"$LOG_FILE" || true
        LLM_SKILL_ARGS=(--skill-file "$LLM_DIR/skill.md")
    fi
    set +e
    EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_llm.py" run \
        --plugin "$PLUGIN_NAME" --mode "$LLM_MODE" --channel-key "$CHANNEL_KEY" \
        --collected-file "$LLM_DIR/collected.json" "${LLM_SKILL_ARGS[@]}" \
        --anchors "${ANCHORS_JSON:-[]}" 2>>"$LOG_FILE"
    LLM_RC=$?
    set -e
    rm -rf "$LLM_DIR"
    if [[ "$LLM_RC" == "0" ]]; then
        log "Post written without Claude (llm=$LLM_MODE)."
        PENDING_STATE="$EA_BOT_PLUGIN_STATE/state.pending.json"
        COMMITTED_STATE="$EA_BOT_PLUGIN_STATE/state.json"
        if [[ -f "$PENDING_STATE" ]]; then
            "$OWNER_IO" mv "$PENDING_STATE" "$COMMITTED_STATE" 2>>"$LOG_FILE" \
                && log "Committed plugin state." \
                || log "WARNING: mv $PENDING_STATE -> $COMMITTED_STATE failed."
        fi
        exit 0
    elif [[ "$LLM_RC" == "3" && "$LLM_MODE" == "remote-first" ]]; then
        log "The remote model could not write the post; Claude writes it."
    else
        log "llm=$LLM_MODE run failed (rc=$LLM_RC); see the log."
        exit 0
    fi
fi

# -------------------------------------------------------------------
# Step 2: Build the Claude prompt from the skill
# -------------------------------------------------------------------
SKILL_FILE="$EA_BOT_PLUGIN_DIR/$SKILL_REL"
if [[ ! -f "$SKILL_FILE" ]]; then
    log "FATAL: $SKILL_REL not found at $SKILL_FILE"
    exit 4
fi
# The skill text goes into the prompt, so it is read as ea-bot.
SKILL_CONTENT=$("$OWNER_IO" read "$SKILL_FILE")

# The user's own rules for the scopes this plugin declares: "scopes" in
# plugin.json and/or a <!-- scopes: ... --> header in skill.md (see
# preprocessor/preferences.py). Empty when neither declares anything.
# A scope is one word of plain characters; PLUGIN_SCOPES is split into
# words below with globbing off, so a scope never expands into file names.
PLUGIN_SCOPES=$(printf '%s' "$MANIFEST_JSON" | "$PYTHON3" -c "
import json, re, sys
m = json.load(sys.stdin)
print(' '.join('--scope ' + str(s) for s in m.get('scopes', [])
               if re.fullmatch(r'[A-Za-z0-9:._*-]+', str(s))))
" 2>/dev/null || true)
set -f
# shellcheck disable=SC2086  # PLUGIN_SCOPES is a list of --scope flags
USER_RULES=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/preferences.py" render --skill "$SKILL_FILE" $PLUGIN_SCOPES 2>>"$LOG_FILE" || true)
set +f
if [[ -n "$USER_RULES" ]]; then
    log "User rules injected for plugin scopes"
    SKILL_CONTENT="${SKILL_CONTENT}"$'\n\n'"${USER_RULES}"
fi

# Render the anchor list for the prompt (e.g. ":scales:, :red_circle:")
ANCHORS_STR=$("$PYTHON3" -c "
import json, sys
v = json.loads(sys.argv[1]) if sys.argv[1] else [':sparkles:']
print(', '.join(v))
" "${ANCHORS_JSON:-[]}")

PROMPT="You are running as the EA plugin '${PLUGIN_NAME}' at $(date).

Execute the following skill instructions:

${SKILL_CONTENT}

The collected data is at: ${EA_BOT_COLLECT_OUT}
Your plugin directory is: ${EA_BOT_PLUGIN_DIR}

CRITICAL OUTPUT RULE: claude -p only captures text from the FINAL assistant turn.
Any text you write BEFORE a tool call is SILENTLY DROPPED. Do ALL tool calls
(reading files, ledger writes) FIRST, then write the Slack message text as
your FINAL output with NO tool calls after it.

MANDATORY FORMAT: The FIRST line of your text output MUST start with one of
these Slack shortcode emojis: ${ANCHORS_STR}

If today is a no-op (no new items to report and the plugin's skill allows
quiet runs), output nothing. The runner will skip the Slack post.

FORBIDDEN PATTERNS — meta-commentary that MUST NEVER appear:
- 'State updated' / 'Done' / 'Update complete'
- 'here is a summary' / 'here is what was covered'
- Any text describing what you did rather than being the Slack message itself

Use Slack emoji shortcodes (:sunny:, :email:) not unicode emoji.
Use *single asterisks* for bold (Slack mrkdwn), NOT **double**.

Begin now."

# -------------------------------------------------------------------
# Step 3: Build --allowedTools dynamically from manifest context grants
# -------------------------------------------------------------------
PLUGIN_RUN_GRANTS=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" \
    run-grants --plugin "$PLUGIN_NAME" --dir "$EA_BOT_PLUGIN_DIR" 2>/dev/null || true)
ALLOWED_TOOLS=$(printf '%s' "$MANIFEST_JSON" | "$PYTHON3" -c "
import json, sys
m = json.load(sys.stdin)
ctx = m.get('context', {})
plugin_name = m['name']
ea_bot_scripts = sys.argv[1] + '/preprocessor'
plugin_dir = sys.argv[2]

allowed = []
# The personalization store is readable and writable from every lane
# (CLAUDE.md "Personal rules"); a plugin run may record a calibration line.
allowed.append(f'Bash(python3 {ea_bot_scripts}/preferences.py *)')

# Always-allowed: Read scoped to plugin dir + ea_plugins.ledger CLI shim.
# Read tool is global in claude -p; we trust the plugin's skill not to
# read outside its scope (the manifest is the contract).
allowed.append('Read')

# ledger access
if ctx.get('ledger_read') or ctx.get('ledger_write'):
    allowed.append(f'Bash(python3 -m ea_plugins.ledger *)')

# graph_actions: list of allowed subcommands
ga = ctx.get('graph_actions') or []
for sub in ga:
    allowed.append(f'Bash(python3 {ea_bot_scripts}/graph_actions.py {sub} *)')

# fetch_url
if ctx.get('fetch_url'):
    allowed.append(f'Bash(python3 {ea_bot_scripts}/fetch_url.py *)')

# Google Calendar / Drive MCP: read verbs only, spelled out. The same
# calendar list as run-agent.sh; every write verb is on the deny list and
# a wildcard would grant it back.
if ctx.get('google_calendar'):
    allowed += ['mcp__claude_ai_Google_Calendar__list_calendars',
                'mcp__claude_ai_Google_Calendar__list_events',
                'mcp__claude_ai_Google_Calendar__get_event',
                'mcp__claude_ai_Google_Calendar__search_events']
if ctx.get('google_drive'):
    allowed += ['mcp__claude_ai_Google_Drive__search_files',
                'mcp__claude_ai_Google_Drive__list_recent_files',
                'mcp__claude_ai_Google_Drive__get_file_metadata',
                'mcp__claude_ai_Google_Drive__get_file_permissions',
                'mcp__claude_ai_Google_Drive__read_file_content',
                'mcp__claude_ai_Google_Drive__download_file_content']

# Plugin-local Bash: one rule per entry point (plugin_registry.py
# run-grants). A locked (v2) plugin gets the grants the owner accepted
# (capabilities.tools) over the files its lock hashed; a v1 plugin gets
# python3 on each file of its directory. A rule never names a file under a
# state dir: the lane may write there, and a glob would match it.
allowed += [g for g in sys.argv[3].split(',') if g]
allowed.append(f'Bash(cat {plugin_dir}/*)')
allowed.append(f'Bash(ls {plugin_dir}/*)')

print(','.join(allowed))
" "$EA_BOT_SCRIPT_DIR" "$EA_BOT_PLUGIN_DIR" "$PLUGIN_RUN_GRANTS")
log "Allowed tools: $ALLOWED_TOOLS"

# -------------------------------------------------------------------
# Step 4: Invoke Claude with timeout, capture output, log cost
# -------------------------------------------------------------------
log "Invoking Claude (timeout ${TIMEOUT}s)..."

# The flags that keep the run on the user's subscription login
# (preprocessor/claude_cli.py; none today), probed as ea-bot from this
# script's own tree. NUL-separated, read without mapfile so the script
# stays bash 3.2 safe.
CLAUDE_PROBE_BIN="$EA_BOT_HOME/.local/bin/claude"
[[ -x "$CLAUDE_PROBE_BIN" ]] || CLAUDE_PROBE_BIN=claude
CLAUDE_FLAGS=()
while IFS= read -r -d '' _flag; do
    CLAUDE_FLAGS+=("$_flag")
done < <("$PYTHON3" "$SCRIPT_DIR/preprocessor/claude_cli.py" flags \
    --claude-bin "$CLAUDE_PROBE_BIN" --as-ea-bot 2>>"$LOG_FILE" || true)

# The command line itself carries `--permission-mode dontAsk`
# (claude_cli.LANE_PERMISSION_MODE): ea-bot's settings run auto mode, and
# under dontAsk a tool ALLOWED_TOOLS does not name is refused instead of
# going to the auto-mode classifier. A run whose manifest grants no
# connector verb loads no MCP server and no claude.ai connector.
MCP_ENV=()
MCP_FLAGS=()
if [[ "$ALLOWED_TOOLS" != *mcp__* ]]; then
    MCP_ENV=(ENABLE_CLAUDEAI_MCP_SERVERS=false)
    MCP_FLAGS=(--strict-mcp-config)
fi

# Use coreutils `timeout` to bound the Claude run. Pass PROMPT / MODEL /
# ALLOWED_TOOLS as positional args to the inner `bash -c` (single-quoted
# so the prompt's parens, asterisks, etc. aren't interpreted by the
# outer shell). Mirrors run-agent.sh's invocation shape.
set +e
CLAUDE_OUTPUT=$(timeout "${TIMEOUT}" sudo -u ea-bot env \
    HOME="$EA_BOT_HOME" \
    EA_DIR="$EA_DIR" \
    EA_PLUGIN_NAME="$PLUGIN_NAME" \
    EA_PLUGIN_DIR="$EA_BOT_PLUGIN_DIR" \
    PATH="$EA_BOT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    "${PLUGIN_SECRETS_ENV[@]}" \
    ${MCP_ENV[@]+"${MCP_ENV[@]}"} \
    bash -c 'cd "$0" && claude -p "$1" --model "$2" --allowedTools "$3" --disallowedTools "$4" --permission-mode dontAsk --output-format stream-json --verbose "${@:5}"' \
    "$EA_BOT_HOME" "$PROMPT" "$MODEL" "$ALLOWED_TOOLS" "$DISALLOWED_TOOLS" \
    ${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"} \
    ${MCP_FLAGS[@]+"${MCP_FLAGS[@]}"} \
    2>>"$LOG_FILE" \
  | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/log_claude_cost.py" \
        --agent "plugin:$PLUGIN_NAME" --model "$MODEL" 2>>"$LOG_FILE")
rc="${PIPESTATUS[0]}"
set -e
# Every chat post below takes its channel key (from the ea-bot-writable
# manifest) and its text through the environment, never inside the
# Python source, so neither can inject code into the owner's Python.
if [[ "$rc" == "124" ]]; then
    log "Claude TIMED OUT after ${TIMEOUT}s."
    FAIL_TEXT=":warning: *plugin:$PLUGIN_NAME timed out* after ${TIMEOUT}s; no output produced."
    POST_KEY="$CHANNEL_KEY" POST_TEXT="$FAIL_TEXT" "$PYTHON3" -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
slack.post_to_slack(os.environ['POST_KEY'], os.environ['POST_TEXT'])
" 2>&1 | tee -a "$LOG_FILE" || true
    exit "$rc"
fi
if [[ "$rc" != "0" ]]; then
    log "Claude invocation failed (rc=$rc)."
    exit "$rc"
fi

# Strip leading preamble: Claude often emits reasoning paragraphs before
# the format anchor. The scan is bounded to the first lines, the same as
# run-agent.sh: an unbounded scan would keep only the tail of a report
# whose first :shortcode: anchor sits deep in the body.
STRIP_PREAMBLE_MAX_LINES="${STRIP_PREAMBLE_MAX_LINES:-12}"
CLAUDE_OUTPUT=$(STRIP_PREAMBLE_MAX_LINES="$STRIP_PREAMBLE_MAX_LINES" "$PYTHON3" -c "
import os, re, sys
text = sys.stdin.read()
lines = text.splitlines()
limit = int(os.environ.get('STRIP_PREAMBLE_MAX_LINES') or 12)
anchor = re.compile(r'^\s*:[a-z0-9_]+:')
for i, ln in enumerate(lines[:limit]):
    if anchor.match(ln):
        sys.stdout.write('\n'.join(lines[i:]))
        sys.exit(0)
sys.stdout.write(text)
" <<< "$CLAUDE_OUTPUT")

# -------------------------------------------------------------------
# Step 5: Validate output against manifest's format_anchors
# -------------------------------------------------------------------
# Empty output = no-op run (e.g. quiet day per the skill). Skip Slack.
if [[ -z "$(echo "$CLAUDE_OUTPUT" | tr -d '[:space:]')" ]]; then
    log "Empty output; skipping Slack post (no-op run)."
    exit 0
fi

VALIDATION=$("$PYTHON3" -c "
import json, re, sys
out = sys.stdin.read()
anchors = json.loads(sys.argv[1]) if sys.argv[1] else []
# Meta-commentary check
lower = out[:200].lower()
for pat in ['state updated', 'here is a summary', 'here is what was',
            'update complete', 'plugin run complete', 'posted to slack']:
    if pat in lower:
        print(f'meta-commentary: {pat}')
        sys.exit(0)
# Format-anchor check: first non-blank line must start with one of the
# declared anchors (after optional whitespace).
first_line = next((ln for ln in out.splitlines() if ln.strip()), '')
if anchors:
    pattern = '|'.join(re.escape(a) for a in anchors)
    if not re.match(r'\\s*(' + pattern + ')', first_line):
        print(f'missing-format-anchor: first line did not start with any of {anchors}')
        sys.exit(0)
print('ok')
" "${ANCHORS_JSON:-[]}" <<< "$CLAUDE_OUTPUT")

if [[ "$VALIDATION" != "ok" ]]; then
    log "OUTPUT VALIDATION FAILED: $VALIDATION"
    DEBUG_DUMP="$LOG_DIR/${PLUGIN_NAME}-failed-$(date +%Y%m%d-%H%M%S).txt"
    printf '%s' "$CLAUDE_OUTPUT" > "$DEBUG_DUMP"
    log "Failed-attempt output written to: $DEBUG_DUMP"
    log "First 300 chars: $(printf '%s' "$CLAUDE_OUTPUT" | head -c 300 | tr '\n' ' ')"
    log "Skipping Slack post of malformed output; posting :warning: instead."
    WARN_TEXT=":warning: \`plugin:$PLUGIN_NAME\` produced output that failed validation: \`$VALIDATION\`. See \`~/.ea/logs/plugins/$PLUGIN_NAME/\` for details. No content posted."
    POST_KEY="$CHANNEL_KEY" POST_TEXT="$WARN_TEXT" "$PYTHON3" -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
slack.post_to_slack(os.environ['POST_KEY'], os.environ['POST_TEXT'])
" 2>&1 | tee -a "$LOG_FILE" || true
    log "Done (validation failure)."
    exit 0
fi

# -------------------------------------------------------------------
# Step 6: Post to Slack via slack_notifier
# -------------------------------------------------------------------
log "Posting to Slack channel '$CHANNEL_KEY'..."
set +e
POST_KEY="$CHANNEL_KEY" "$PYTHON3" -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
msg = sys.stdin.read().strip()
if not msg:
    sys.exit(0)
ok = slack.post_to_slack(os.environ['POST_KEY'], msg)
# slack_notifier.post_to_slack returns truthy on success, None/False on failure.
sys.exit(0 if ok else 1)
" <<< "$CLAUDE_OUTPUT" 2>&1 | tee -a "$LOG_FILE"
POST_RC="${PIPESTATUS[0]}"
set -e

# -------------------------------------------------------------------
# Step 7: Promote pending state to committed state, but ONLY if the
# Slack post succeeded. Plugins write state.pending.json from their
# collect.py; this rename is the single commit point for the run.
# If Slack failed, leave state.pending.json in place — collect.py will
# overwrite it next run, and the same message IDs will be re-surfaced
# until they actually get posted.
# -------------------------------------------------------------------
PENDING_STATE="$EA_BOT_PLUGIN_STATE/state.pending.json"
COMMITTED_STATE="$EA_BOT_PLUGIN_STATE/state.json"
if [[ -f "$PENDING_STATE" ]]; then
    if [[ "$POST_RC" == "0" ]]; then
        if "$OWNER_IO" mv "$PENDING_STATE" "$COMMITTED_STATE" 2>>"$LOG_FILE"; then
            log "Committed plugin state (post rc=0)."
        else
            log "WARNING: mv $PENDING_STATE -> $COMMITTED_STATE failed; state NOT committed."
        fi
    else
        log "Slack post failed (rc=$POST_RC); leaving $PENDING_STATE in place for retry."
    fi
fi

log "Done."
