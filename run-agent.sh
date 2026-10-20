#!/usr/bin/env bash
#
# EA Agent Orchestrator
# Runs the preprocessor (zero tokens), then invokes Claude Code (Max quota).
#
# Usage:
#   ./run-agent.sh morning-briefing --model sonnet
#   ./run-agent.sh morning-briefing --rerun     # re-surface all items
#   ./run-agent.sh morning-briefing --now       # run now: no 4 AM gate, no skip
#   ./run-agent.sh weekly-audit --model opus
#   ./run-agent.sh list-digest                  # mailing-list digests -> Slack snippets, then archive
#
set -euo pipefail

EA_DIR="${EA_DIR:-$HOME/.ea}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Resolve the owner's own symlinks (~/.ea points into ea-bot's home), so the
# paths handed to ea-bot are ones it can reach. Resolution stops at the
# first name ea-bot controls, so ea-bot cannot re-point EA_DIR elsewhere.
_ea_dir="$("$SCRIPT_DIR/scripts/owner-io.sh" check "$EA_DIR" | cut -f2-)" || _ea_dir=""
if [[ -n "$_ea_dir" ]]; then
    EA_DIR="$_ea_dir"
fi
unset _ea_dir
# Load config and secrets as data, never as shell (scripts/owner-io.sh env).
# ~/.ea is ea-bot space, so its .env is read as ea-bot and supplies only
# non-secret keys; secrets come from the owner-private secrets.env, or from
# a legacy .env.secrets that the owner alone controls.
while IFS= read -r -d '' _env_key && IFS= read -r -d '' _env_val; do
    if [[ "$_env_key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
        export "$_env_key=$_env_val"
    fi
done < <("$SCRIPT_DIR/scripts/owner-io.sh" env "$EA_DIR" "${EA_PRIVATE_DIR:-$HOME/.ea-private}")
unset _env_key _env_val

# Mail mediation holds the Graph client secret owner-side (loaded by the mediator
# service, not this script) and runs graph_client for the ea-bot subprocess.
# Never expose the secret to that subprocess when mediation is on. Fail-secure:
# unset unless mediation is explicitly OFF.
if EA_DIR="$EA_DIR" RA_DIR="$(cd "$(dirname "$0")" && pwd)" python3 - <<'PY'
import os, sys
# mediation_active_for_resident() fails CLOSED and ignores the ea-bot-owned
# config's off-switch: only the owner-owned kill file or a missing unit turns
# mediation off, so a prompt-injected resident cannot flip a flag to get the
# secret handed to its own subprocess.
sys.path.insert(0, os.path.join(os.environ["RA_DIR"], "preprocessor"))
try:
    import mail_mediation
    on = mail_mediation.mediation_active_for_resident()
except Exception:
    on = True  # fail-secure: withhold the secret
sys.exit(0 if on else 1)
PY
then
    unset GRAPH_CLIENT_SECRET
fi

# Use project venv if available (required on macOS PEP 668; good practice on Linux)
if [[ -f "$SCRIPT_DIR/.venv/bin/python3" ]]; then
    export PATH="$SCRIPT_DIR/.venv/bin:$PATH"
fi
PYTHON3="$(command -v python3)"

# The install profile decides where reports go. Without Slack every
# report is a file under ~/.ea/state/reports/ and nothing is posted.
PROFILE="$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/ea_config.py" profile 2>/dev/null || echo assistant)"
# `chat` is the platform-neutral name of the feature (Slack or Zulip).
HAS_SLACK=1
EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/ea_config.py" feature chat >/dev/null 2>&1 || HAS_SLACK=0
PREPROCESS="$SCRIPT_DIR/preprocessor/preprocess.py"
SKILL_DIR="$SCRIPT_DIR/skills"
STATE_DIR="$EA_DIR/state"
# Per-run agent logs are written by this wrapper (the owner), so they live
# in the owner-private log dir ea-bot cannot reach; the shared ~/.ea/logs
# stays for what the ea-bot child writes (e.g. missed-closures.log).
LOG_DIR="$HOME/.ea-private/logs"
BRIEFING_FILE="$STATE_DIR/briefing-latest.json"

mkdir -p "$LOG_DIR"
chmod 700 "$HOME/.ea-private/logs" 2>/dev/null || true
# The state and shared log dirs are ea-bot space: created as ea-bot.
"$SCRIPT_DIR/scripts/owner-io.sh" mkdir "$STATE_DIR"
"$SCRIPT_DIR/scripts/owner-io.sh" mkdir "$EA_DIR/logs"

# Check master EA switch (ea-config.json). Missing file = enabled.
# Paths under EA_DIR reach inline Python as arguments, never inside its
# source text.
EA_ENABLED=$("$PYTHON3" -c "
import json, sys; from pathlib import Path
p = Path(sys.argv[1])
print(json.loads(p.read_text()).get('ea_enabled', True) if p.exists() else True)
" "$EA_DIR/config/ea-config.json" 2>/dev/null || echo "True")

if [[ "$EA_ENABLED" == "False" ]]; then
    echo "[$(date +%H:%M:%S)] EA paused (ea_enabled=false in ea-config.json). Skipping."
    exit 0
fi

AGENT="${1:?Usage: run-agent.sh <agent-name> [--model sonnet|opus]}"
shift

# Parse optional flags. MODEL stays empty here so the per-agent default
# below can tell "explicit --model" apart from "use the default".
MODEL=""
RERUN=false
# --now: a run by hand (the install's acceptance step). It skips the
# morning briefing's local-hour gate and the "nothing new" skip below.
NOW=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) MODEL="$2"; shift 2 ;;
        --rerun) RERUN=true; shift ;;
        --now) NOW=true; shift ;;
        *) shift ;;
    esac
done

# Local-time gate for the morning briefing: it should run at 4 AM in the
# user's CURRENT timezone (the home timezone from scheduling-preferences,
# or the travel timezone when set), not at a fixed server hour. The cron
# fires hourly; this guard lets the briefing run only on the 4 AM-local
# hour, once per local day (dedupe lives in quiet_hours.due_local). Other
# agents are unaffected; --now bypasses it.
if [[ "$AGENT" == "morning-briefing" && "$NOW" != true ]]; then
    if ! "$PYTHON3" "$SCRIPT_DIR/preprocessor/quiet_hours.py" \
            local-due --hour 4 --agent morning-briefing >/dev/null 2>&1; then
        echo "[$(date +%H:%M:%S)] morning-briefing: not 4 AM local (or already ran today). Skipping."
        exit 0
    fi
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/${AGENT}-${TIMESTAMP}.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# -------------------------------------------------------------------
# Map agent name to preprocessor mode and skill file
# -------------------------------------------------------------------
case "$AGENT" in
    morning-briefing)
        PREPROCESS_MODE="full-daily"
        SKILL_FILE="$SKILL_DIR/morning-briefing.md"
        ;;
    afternoon-catchup)
        PREPROCESS_MODE="full-daily"
        SKILL_FILE="$SKILL_DIR/afternoon-catchup.md"
        ;;
    weekly-audit)
        PREPROCESS_MODE="full-weekly"
        SKILL_FILE="$SKILL_DIR/weekly-audit.md"
        ;;
    scheduling-check)
        PREPROCESS_MODE="scheduling-check"
        SKILL_FILE=""  # preprocessor-only, no Claude
        ;;
    priority-check)
        PREPROCESS_MODE="priority-check"
        SKILL_FILE=""  # preprocessor-only, no Claude
        ;;
    intake-check)
        # Intraday capture of ordinary needs-response mail into the
        # ledger (silent; items surface on desk/App Home, not Slack).
        PREPROCESS_MODE="intake-check"
        SKILL_FILE=""  # preprocessor-only, no Claude
        ;;
    list-digest)
        # Mailing-list digests (robotics-worldwide, and whatever else
        # ~/.ea/config/list-digests.json names). The preprocessor splits
        # each digest into items and drops job posts in code; the skill
        # judges the rest against the user's profile and writes the
        # snippets; this script posts them and THEN archives the digests
        # (post first, so a failed run leaves the mail in the inbox for
        # the next attempt). Nothing relevant: no post, silent archive.
        # No digest in the inbox: exit quietly before Claude is invoked.
        PREPROCESS_MODE="list-digest"
        SKILL_FILE="$SKILL_DIR/list-digest.md"
        ;;
    admissions-digest)
        # Weekly triage of the admissions-inquiries Outlook folder, where
        # admissions_filter.py moves prospective-student mail as it
        # arrives. The skill ranks the week's senders and writes one post;
        # nothing is moved, answered or ledgered. An empty week exits
        # quietly before Claude is invoked.
        PREPROCESS_MODE="admissions-digest"
        SKILL_FILE="$SKILL_DIR/admissions-digest.md"
        ;;
    *)
        echo "Unknown agent: $AGENT"
        echo "Available: morning-briefing, afternoon-catchup, weekly-audit,"
        echo "           scheduling-check, priority-check, intake-check,"
        echo "           list-digest, admissions-digest"
        exit 1
        ;;
esac

# Per-agent model default (overridable with --model). The synthesis
# agents write the user-facing daily digests, where triage judgment is
# the whole product: they run on EA_MODEL, the model the install pins in
# its .env for the chat bot too, and with no pin on the opus model named
# here, so a CLI upgrade cannot move them. Everything else defaults to
# the sonnet alias.
if [[ -z "$MODEL" ]]; then
    case "$AGENT" in
        morning-briefing|afternoon-catchup|weekly-audit) MODEL="${EA_MODEL:-claude-opus-5-5}" ;;
        *) MODEL="sonnet" ;;
    esac
fi

# Per-mode output file. Each mode owns its own; briefing-latest.json only
# ever holds the full daily/weekly shape, so a resident dispatch reading
# "the briefing JSON" never gets a stub payload with no obligations, no
# writing_style and no availability.
case "$PREPROCESS_MODE" in
    priority-check)   OUTPUT_FILE="$STATE_DIR/priority-latest.json" ;;
    intake-check)     OUTPUT_FILE="$STATE_DIR/intake-latest.json" ;;
    scheduling-check) OUTPUT_FILE="$STATE_DIR/scheduling-latest.json" ;;
    list-digest)      OUTPUT_FILE="$STATE_DIR/list-digest-latest.json" ;;
    admissions-digest) OUTPUT_FILE="$STATE_DIR/admissions-digest-latest.json" ;;
    *)                OUTPUT_FILE="$BRIEFING_FILE" ;;
esac

# -------------------------------------------------------------------
# Step 1: Run preprocessor (no Claude tokens)
# -------------------------------------------------------------------
if [[ -n "$PREPROCESS_MODE" ]]; then
    ALWAYS_RUN_AGENTS="morning-briefing afternoon-catchup weekly-audit"
    RERUN_FLAG=""
    $RERUN && RERUN_FLAG="--rerun"
    log "Running preprocessor: $PREPROCESS_MODE${RERUN_FLAG:+ (rerun)}"
    PREPROCESS_OK=true
    "$PYTHON3" "$PREPROCESS" --mode "$PREPROCESS_MODE" --output "$OUTPUT_FILE" \
        $RERUN_FLAG 2>&1 | tee -a "$LOG_FILE" || PREPROCESS_OK=false

    PREPROCESS_WARNING=""
    if ! $PREPROCESS_OK; then
        log "WARNING: Preprocessor failed."
        # For always-run agents, continue with stale briefing if one exists
        if echo "$ALWAYS_RUN_AGENTS" | grep -qw "$AGENT" && [[ -f "$BRIEFING_FILE" ]]; then
            log "Continuing with stale briefing for always-run agent: $AGENT"
            BRIEFING_AGE=$(( ($(date +%s) - $(stat -c %Y "$BRIEFING_FILE")) / 60 ))
            PREPROCESS_WARNING="WARNING: The preprocessor FAILED this run. The briefing data is STALE (${BRIEFING_AGE} minutes old). You MUST tell the user that email sync failed and the briefing may be incomplete. Do NOT say 'no new emails' — say the sync failed and you are working from stale data."
        else
            log "FATAL: Preprocessor failed and no stale briefing available. Aborting."
            exit 1
        fi
    fi

    # For preprocessor-only modes, post to Slack and exit. The output file
    # sits in ea-bot space and its content goes to chat, so it is read as
    # ea-bot.
    if [[ -z "$SKILL_FILE" ]]; then
        log "Preprocessor-only mode: posting notifications to Slack"
        "$SCRIPT_DIR/scripts/owner-io.sh" read "$OUTPUT_FILE" | "$PYTHON3" -c "
import json, sys
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack

data = json.load(sys.stdin)
mode = data.get('mode', '')

if mode == 'scheduling-check':
    reqs = data.get('scheduling_requests', [])
    slack.notify_scheduling_requests(reqs, data.get('availability_merged', []))
    # The proactive queue is written by preprocess.write_proactive_queue
    # (a merge by conversation id, shared with the ten-minute intake), so
    # this notifier no longer rewrites it; an overwrite here could drop an
    # entry the bot had not opened yet.
    if reqs:
        print('scheduling check:', len(reqs), 'new request(s) queued by the preprocessor')
elif mode == 'priority-check':
    slack.notify_priority_emails(
        data.get('priority_emails', [])
    )
" 2>&1 | tee -a "$LOG_FILE"
        log "Done (preprocessor-only)."
        exit 0
    fi

    # Skip Claude only for non-briefing agents. Morning and afternoon
    # briefings ALWAYS run: with no new emails they still re-surface
    # stale obligations and give a situational update.
    DELTA_SIZE=$("$PYTHON3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
items = (len(d.get('new_dropped_threads', d.get('dropped_threads', [])))
       + len(d.get('new_needs_response', d.get('needs_response', [])))
       + len(d.get('new_scheduling_requests', d.get('scheduling_requests', [])))
       + len(d.get('travel_reimbursement', []))
       + len(d.get('digests', [])))
print(items)
" "$OUTPUT_FILE" 2>&1)

    # Always run briefings and audits; only skip for other agent types,
    # and never on a --now run.
    if [[ "$DELTA_SIZE" -eq 0 && "$NOW" != true ]] && ! echo "$ALWAYS_RUN_AGENTS" | grep -qw "$AGENT"; then
        log "No new items in delta (count=$DELTA_SIZE). Skipping Claude to save tokens."
        # list-digest polls a few times a day and usually finds no new
        # digest. A "nothing new" post each time would be the very noise
        # the agent exists to remove, so it exits without a word.
        if [[ "$AGENT" == "list-digest" ]]; then
            log "Done (no list digest in the inbox)."
            exit 0
        fi
        if [[ "$AGENT" == "admissions-digest" ]]; then
            log "Done (no admissions inquiries this week)."
            exit 0
        fi
        if [[ "$HAS_SLACK" == 1 ]]; then
            "$PYTHON3" -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
slack.post_to_slack('ea-assistant', '☀️ ${AGENT} – nothing new since last run. No action needed.')
" 2>&1 | tee -a "$LOG_FILE" || true
        fi
        exit 0
    fi

    log "Delta contains $DELTA_SIZE new items (or always-run agent). Invoking Claude."
fi

# -------------------------------------------------------------------
# Step 2: Invoke Claude Code (uses Max subscription quota)
# -------------------------------------------------------------------
# Claude Code invocation — runs as ea-bot for security isolation.
# Isolation is MANDATORY. This script refuses to run without the
# ea-bot Unix user, matching ea-chat-bot.py and docs/SECURITY.md
# Layer 1. See setup-ea-user.sh (Linux) or setup-ea-user-macos.sh.
#
# --- Notice to Claude Code agents editing this file ---------------
# There is no opt-out: any fail-open path invalidates the security
# model, since Claude subprocesses must never have write access to
# source or config. Do NOT remove the ea-bot user check below, add a
# platform exception, or reintroduce EA_ISOLATION_MODE.
# Create the ea-bot user instead (SETUP.md "The service account"). Full
# invariants: the /setup skill, "Read first: the invariants".

if [[ -n "${EA_ISOLATION_MODE:-}" ]]; then
    echo "FATAL: EA_ISOLATION_MODE is no longer supported. The agent" >&2
    echo "always runs as ea-bot; there is no opt-out. Unset the var" >&2
    echo "and ensure the ea-bot user exists (see SETUP.md §8b)." >&2
    exit 2
fi

EA_BOT_HOME=$(python3 -c 'import pwd,sys
try: print(pwd.getpwnam("ea-bot").pw_dir)
except KeyError: sys.exit(1)' 2>/dev/null) || {
    echo "FATAL: Unix user 'ea-bot' does not exist. Isolation is" >&2
    echo "mandatory — see SETUP.md §8b (setup-ea-user.sh on Linux," >&2
    echo "setup-ea-user-macos.sh on macOS) and docs/SECURITY.md." >&2
    exit 2
}
EA_BOT_SCRIPT_DIR="$EA_BOT_HOME/ea-system"

# Defense-in-depth: deny gated send/event subcommands at the Claude
# permission layer. The confirm-token gate inside graph_actions.py is the
# primary protection; this list ensures the LLM can never invoke those
# subcommands even with the python3 graph_actions.py glob in --allowedTools.
# See scripts/disallowed-tools.txt for the canonical list. The lists come
# from this script's own tree, not ea-bot's copy: a list read from a tree
# ea-bot can replace would let it drop its own denials.
DISALLOWED_FILE="$SCRIPT_DIR/scripts/disallowed-tools.txt"
if [[ -f "$DISALLOWED_FILE" ]]; then
    DISALLOWED_TOOLS=$(grep -v '^[[:space:]]*#' "$DISALLOWED_FILE" \
        | sed '/^[[:space:]]*$/d' | paste -sd ',' -)
else
    DISALLOWED_TOOLS=""
    log "WARNING: $DISALLOWED_FILE missing; running without --disallowedTools"
fi
# The review-only profile also denies every drafting verb.
if [[ "$PROFILE" == "review-only" && -f "$SCRIPT_DIR/scripts/disallowed-tools.review-only.txt" ]]; then
    REVIEW_ONLY_DENY=$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/scripts/disallowed-tools.review-only.txt" | sed '/^[[:space:]]*$/d' | paste -sd ',' -)
    DISALLOWED_TOOLS+=",$REVIEW_ONLY_DENY"
fi
# Denials the enabled plugins declare (docs/plugins.md), from the owner's lock.
PLUGIN_DENIED=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" denied-tools 2>/dev/null || true)
[[ -n "$PLUGIN_DENIED" ]] && DISALLOWED_TOOLS+="${DISALLOWED_TOOLS:+,}$PLUGIN_DENIED"
# The model's own web tools are denied as well as left out of ALLOWED_TOOLS:
# the runs below are in dontAsk mode, which refuses them already, and a
# deny rule holds whatever the mode; a subagent inherits the denials.
CRON_WEB_DENY="WebFetch,WebSearch"
DISALLOWED_TOOLS+="${DISALLOWED_TOOLS:+,}$CRON_WEB_DENY"

# The flags that keep each `claude -p` run below on the user's
# subscription login (preprocessor/claude_cli.py; none today), probed as
# ea-bot from this script's own tree. NUL-separated, read without mapfile
# so the script stays bash 3.2 safe.
#
# Each run's command line also carries `--permission-mode dontAsk`
# (claude_cli.LANE_PERMISSION_MODE), written into it here and not taken
# from this call, so a failed call cannot drop it. ea-bot's settings run
# auto mode; under dontAsk a tool ALLOWED_TOOLS does not name is refused,
# connector tools included, instead of going to the auto-mode classifier.
CLAUDE_PROBE_BIN="$EA_BOT_HOME/.local/bin/claude"
[[ -x "$CLAUDE_PROBE_BIN" ]] || CLAUDE_PROBE_BIN=claude
CLAUDE_FLAGS=()
while IFS= read -r -d '' _flag; do
    CLAUDE_FLAGS+=("$_flag")
done < <("$PYTHON3" "$SCRIPT_DIR/preprocessor/claude_cli.py" flags \
    --claude-bin "$CLAUDE_PROBE_BIN" --as-ea-bot 2>>"$LOG_FILE" || true)

# The morning briefing and the afternoon update on the remote model
# (docs/llm-lanes.md, "Briefings"). With `briefings` at `remote` in the
# owner-private remote-llm.json, remote_briefing.py writes the post, its
# drafts, its ledger notes and its hand-offs to Claude, and runs the
# open-claim audit with the remote model's verdicts; no Claude run is
# made. Exit 3 means the switch is off, and the Claude lane below runs as
# before. Any other failure posts a status line, never a Claude run.
REMOTE_BRIEFING=0
case "$AGENT" in
    morning-briefing|afternoon-catchup)
        set +e
        REMOTE_OUTPUT=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/remote_briefing.py" run \
            --agent "$AGENT" --briefing "$OUTPUT_FILE" --run-id "cron-${AGENT}-${TIMESTAMP}" \
            ${PREPROCESS_WARNING:+--stale} 2>>"$LOG_FILE")
        REMOTE_RC=$?
        set -e
        if [[ "$REMOTE_RC" == "0" ]]; then
            REMOTE_BRIEFING=1
            CLAUDE_OUTPUT="$REMOTE_OUTPUT"
            log "Remote briefing written by the remote model; no Claude run."
        elif [[ "$REMOTE_RC" != "3" ]]; then
            REMOTE_BRIEFING=1
            CLAUDE_OUTPUT=":warning: ${AGENT}: the remote briefing failed (rc=${REMOTE_RC}). The run log has the error. No Claude run was made; the next scheduled run tries again."
            log "Remote briefing failed rc=$REMOTE_RC; posting a status line."
        fi
        ;;
    weekly-audit)
        # The weekly audit on the remote model (remote_audit.py, switch
        # `audits`). Exit 3 means the switch is off. The audit runs once a
        # week, so any other failure also falls back to the Claude lane.
        set +e
        REMOTE_OUTPUT=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/remote_audit.py" run \
            --briefing "$OUTPUT_FILE" --run-id "cron-${AGENT}-${TIMESTAMP}" 2>>"$LOG_FILE")
        REMOTE_RC=$?
        set -e
        if [[ "$REMOTE_RC" == "0" ]]; then
            REMOTE_BRIEFING=1
            CLAUDE_OUTPUT="$REMOTE_OUTPUT"
            log "Weekly audit written by the remote model; no Claude run."
        elif [[ "$REMOTE_RC" != "3" ]]; then
            log "Remote audit failed rc=$REMOTE_RC; the Claude lane runs."
        fi
        ;;
esac

[[ "$REMOTE_BRIEFING" == 0 ]] && log "Invoking Claude Code with skill: $(basename "$SKILL_FILE") (model: $MODEL)"

# Build the prompt from the skill file, then append the user's own rules
# for the scopes the skill declares (its <!-- scopes: ... --> header; see
# preprocessor/preferences.py). Appending to SKILL_CONTENT keeps the PROMPT
# string itself unchanged, so the prompt-assembly harness still covers it,
# and a rules failure can only cost the block, never the run.
SKILL_CONTENT=$(cat "$SKILL_FILE")
# The install overlay (preprocessor/install_overlay.py): the facts
# CLAUDE.md leaves out, rendered into ~/.ea/config/CLAUDE.install.md. The
# chat bot embeds it through prompt_logic.build_claude_md; this lane runs
# claude from ea-bot's home, where no CLAUDE.local.md is discovered, so it
# goes into the prompt here, for every agent. The file is ea-bot space,
# so it is read as ea-bot; its own title line is dropped under the heading.
INSTALL_FACTS=$("$SCRIPT_DIR/scripts/owner-io.sh" read "$EA_DIR/config/CLAUDE.install.md" 2>>"$LOG_FILE" \
    | sed '1{/^# Install facts/d;}' || true)
if [[ -n "$INSTALL_FACTS" ]]; then
    SKILL_CONTENT="${SKILL_CONTENT}"$'\n\n'"## Install facts (CLAUDE.local.md)"$'\n\n'"${INSTALL_FACTS}"
else
    log "No install overlay at $EA_DIR/config/CLAUDE.install.md (install_overlay.py render writes it)"
fi
# Without a chat platform (the email and review-only profiles) nothing
# consumes a staged event or a draft card: the bot is the only reader of
# pending-event.json and the only poster of Send buttons. The skills are
# written for the chat case, so the agent is told what changes.
if [[ "$HAS_SLACK" != 1 ]]; then
    # A heredoc, not a quoted string: only PROFILE and AGENT expand in it.
    NO_CHAT_BLOCK=$(cat <<EOF
## No chat channel

This install (profile ${PROFILE}) has no chat platform. There are no buttons and no cards: nobody can click Send, Create, Edit or Handled, and no bot reads a staged file.
- Do not write pending-event.json, pending-event-update.json, pending-event-delete.json or pending-schedule-send.json, and do not stage anything that needs a click.
- A draft you create stays in the Drafts folder; the user sends it from Outlook. Say so in the report; never tell the user to click Send.
- Describe each proposed event and each draft as text in the report: who, when, and what the body says.
- The briefing JSON has no recent_slack_messages; skip the chat-replies step.
- The report is delivered as a file (~/.ea/state/reports/${AGENT}-latest.md) and as a phone push when one is configured. Write it for that: plain text, nothing that assumes a chat thread.
EOF
)
    SKILL_CONTENT="${SKILL_CONTENT}"$'\n\n'"${NO_CHAT_BLOCK}"
fi
USER_RULES=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/preferences.py" render --skill "$SKILL_FILE" 2>>"$LOG_FILE" || true)
if [[ -n "$USER_RULES" ]]; then
    log "User rules injected for the scopes declared in $(basename "$SKILL_FILE")"
    SKILL_CONTENT="${SKILL_CONTENT}"$'\n\n'"${USER_RULES}"
fi

PROMPT="You are running as the EA agent '${AGENT}' at $(date).
${PREPROCESS_WARNING:+
${PREPROCESS_WARNING}
}
Execute the following skill instructions:

${SKILL_CONTENT}

The preprocessed briefing is at: ${OUTPUT_FILE}
The CLAUDE.md with full operating instructions is at: ${EA_BOT_SCRIPT_DIR}/CLAUDE.md
Topic guides (drafting voice, scheduling, reimbursement, briefing data, inbox completion, project store) are in ${EA_BOT_SCRIPT_DIR}/docs/ea/; read the one your task needs.
You run from ${EA_BOT_HOME}, so that CLAUDE.md is NOT loaded for you; the
install's own facts (CLAUDE.local.md) are in the skill text above. These
four rules from CLAUDE.md are the ones this lane gets wrong, so they are
repeated here in full:

1. A SEARCH THAT FINDS NOTHING IS NOT PROOF THAT NOTHING HAPPENED. Never
   write that the user did not reply, did not send, or lost a draft on the
   strength of an empty result. search-mail --folder reads ONE folder, and
   an archived thread takes the user's own sent copy out of sentitems, so
   the reply you are looking for is exactly where you are not looking.
   Re-run with NO --folder, then get-conversation --conversation-id <id> to
   read the thread itself. If you still cannot tell, say you could not
   confirm. Do not write a guess into the ledger as fact.
2. NEVER answer an existing thread with create-draft. Use create-reply
   --message-id <id> --conversation-id <id>, which keeps the quoted history,
   keeps the real subject line, and refuses to draft when the user already
   sent the latest message in that conversation. create-draft with a Re:
   subject is refused.
3. ALWAYS verify a thread before drafting into it. A ledger note describing
   what has or has not happened is only as current as the item's updated_at.
4. ALWAYS pass --source-message-id to ledger.py add when the item comes
   from an email. Every closure signal joins on conversation_id, so a row
   without one can never be closed by evidence and sits open until a human
   closes it by hand. Measured: id-less rows score 0% closure recall at
   every revision ever shipped; the same rows with ids score 100%.
   ledger.py add derives the conversation id from the message id for you.

CRITICAL OUTPUT RULE: claude -p only captures text from the FINAL assistant turn.
Any text you write BEFORE a tool call is SILENTLY DROPPED and the user sees nothing.
Therefore: do ALL tool calls (reading files, updating state, writing ledger) FIRST,
then write the briefing/update text as your FINAL output with NO tool calls after it.

MANDATORY FORMAT: For morning briefings, your FIRST line of text MUST be:
  :sunny: *Morning Briefing — [date]*
For afternoon catchups, your FIRST line of text MUST be:
  :coffee: *Afternoon Update — [date]*
The header is the FIRST CHARACTERS of your final message. Do not write even
one word before it. In particular, after your last tool call do NOT narrate
the transition — no 'Ledger updated.', no 'Both closed successfully.', no
'Now writing the briefing as my final output.' The header itself is how you
begin.

FORBIDDEN PATTERNS — these are meta-commentary and MUST NEVER appear in your output:
- 'State updated' / 'state files updated' / 'Ledger updated'
- 'briefing posted' / 'posted to Slack'
- 'here is a summary' / 'here is what was covered'
- 'Morning briefing complete'
- 'Now writing the briefing' / 'Now the briefing' / 'this is my final output'
- Any text describing what you did rather than being the briefing itself

WORKFLOW ORDER:
1. Read all data (briefing JSON, config, ledger, Slack) — tool calls
2. Update state (ledger add/close) — tool calls
3. Write the briefing text — your FINAL output, NO tool calls after this
The preprocessor has already fetched the last 24h of Slack messages — check
recent_slack_messages in the briefing JSON. Do NOT use Slack MCP tools.
Do NOT use Slack MCP to send/post messages — the orchestrator posts your text output to Slack.
Use Slack emoji shortcodes (:sunny:, :email:) not unicode emoji.
Use *single asterisks* for bold (Slack mrkdwn), NOT **double**.

Begin now."
# -------------------------------------------------------------------
# Tool allowlist
# -------------------------------------------------------------------
# MCP grants are ENUMERATED, never wildcarded. A wildcard also matches
# send_message / reply / forward / trash_message / trash_thread /
# create_event / update_event / delete_event / respond_to_event, none of
# which is covered by the confirm-token gate in graph_actions.py, by
# preprocessor/mail_mediation.py (which only sees Bash shell-outs), or by
# scripts/disallowed-tools.txt: with a wildcard every cron agent could
# send or delete mail and cancel meetings with no :lock: button. Keep this
# list in sync with ea-chat-bot.py `_MCP_TOOLS` and
# scripts/ea-resident-inner.sh.

# Gmail: the same four read verbs the Slack bot grants. Drafts go through
# gmail_client.py; the connector's create_draft rewrote every link as a
# google.com/url?q= redirect, so it and update_draft are denied
# (scripts/disallowed-tools.txt).
MCP_GMAIL="mcp__claude_ai_Gmail__search_threads"
MCP_GMAIL+=",mcp__claude_ai_Gmail__get_thread"
MCP_GMAIL+=",mcp__claude_ai_Gmail__list_drafts"
MCP_GMAIL+=",mcp__claude_ai_Gmail__list_labels"

# Google Calendar: READ verbs only, for the agents that consult the
# personal and family calendars (morning-briefing's and afternoon-catchup's
# supplemental checks, and the weekly audit). Every write verb
# is button-required per config/autonomy-tiers.json, so calendar writes
# go through pending-event.json + the Slack button, or through
# google_calendar_client.py for the EA-Managed calendar. suggest_time is
# left out on purpose: scheduling_engine.py is the only sanctioned source
# of proposed times.
MCP_GCAL="mcp__claude_ai_Google_Calendar__list_calendars"
MCP_GCAL+=",mcp__claude_ai_Google_Calendar__list_events"
MCP_GCAL+=",mcp__claude_ai_Google_Calendar__get_event"
MCP_GCAL+=",mcp__claude_ai_Google_Calendar__search_events"

# Chat: read-only, from the configured frontend. On Slack that is
#   mcp__claude_ai_Slack__slack_read_channel
#   mcp__claude_ai_Slack__slack_read_thread
#   mcp__claude_ai_Slack__slack_read_user_profile
# and on Zulip nothing (no MCP connector). The orchestrator posts the
# agent's text output.
MCP_CHAT=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/chat_notify.py" llm-grants 2>/dev/null) || MCP_CHAT=""

ALLOWED_TOOLS="${MCP_GMAIL},${MCP_GCAL}"
if [[ -n "$MCP_CHAT" ]]; then
    ALLOWED_TOOLS+=",${MCP_CHAT}"
fi
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/graph_actions.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/gmail_client.py *)"
# google_calendar_client.py is the documented replacement for the gcal MCP
# write verbs (CLAUDE.md "For Google Calendar, use the Python client
# directly (NOT MCP)"); the bot and the resident already grant it.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/google_calendar_client.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/scheduling_engine.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/fetch_url.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/email_to_pdf.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/poll_filler.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/booking_link.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/compile_letter.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/ledger.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/project_store.py *)"
# skills/morning-briefing.md tells the agent to cancel a due deferred send
# with `scheduled_sends.py cancel --draft-id <id>`, and that arm is cronned
# hourly. Without the grant the call hits the permission gate in `claude -p`,
# where there is nobody to approve it. `add` stays denied by
# scripts/disallowed-tools.txt — it parks a send the */5 cron later performs,
# and --disallowedTools outranks this list.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/scheduled_sends.py *)"
# Interactive plugins' scripts: one rule per entry point, from the owner's
# lock for a v2 plugin whose files match it (plugin_registry.py), the same
# grants the bot builds. A glob on the plugins dir would also match a
# script the lane writes under a plugin's state dir, which the write grants
# allow, so no glob is granted and nothing under a state dir is named.
PLUGIN_GRANTS=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/plugin_registry.py" tool-grants 2>/dev/null) \
    || { log "WARN: plugin_registry.py tool-grants failed; this run grants no plugin scripts"; PLUGIN_GRANTS=""; }
[[ -n "$PLUGIN_GRANTS" ]] && ALLOWED_TOOLS+=",$PLUGIN_GRANTS"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/closure_metrics.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/style_profiles.py *)"
# The remote email writer (CLAUDE.md "Drafting in the user's voice"). As
# ea-bot it only forwards to the owner-side mediator, which holds the endpoint key.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/email_writer.py write *)"
# CLAUDE.md's "User-stated closure is a bug" section MANDATES this call after
# every user-stated ledger closure; without the grant the agent hits the
# permission gate and the only feedback signal on scrubber gaps is lost.
# It appends one JSONL line to ~/.ea/logs/missed-closures.log
# and reads evidence through graph_actions' read verbs only.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/log_missed_closure.py *)"
# CLAUDE.md documents this as THE way a cron/external invocation attaches a
# file or posts into a Slack thread. Running as ea-bot it holds no Slack
# token, so it stages into ~/.ea/state/outbox/ for the token-holding bot.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/slack_upload.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/chat_upload.py *)"
# JSON is read with jq (no command, no file write); see ea-chat-bot.py.
ALLOWED_TOOLS+=",Bash(jq *)"
# The personalization store (CLAUDE.md "Personal rules"): an agent records a
# calibration line after a run, or a rule the user stated in a thread, with
# `preferences.py add|calibrate`. Reads and in-place writes to one
# ea-bot-owned config file; no external effect.
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/preferences.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/onboarding.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/alert.py *)"
ALLOWED_TOOLS+=",Bash(python3 ${EA_BOT_SCRIPT_DIR}/preprocessor/page_watch.py *)"
# No WebFetch or WebSearch: a scheduled run reads untrusted mail with no one
# watching, and the model's own web tools leave no record on this host. A
# cron skill that reads a page uses fetch_url.py (granted above), whose
# every request is logged and reviewed daily (scripts/outbound-review.py).
# Write and Edit only on the paths the EA writes
# (preprocessor/resident_write_paths.py); none when the helper fails.
WRITE_GRANTS=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/resident_write_paths.py" \
    --home "$EA_BOT_HOME" --ea-dir "$EA_DIR" 2>/dev/null) || WRITE_GRANTS=""
ALLOWED_TOOLS+=",Bash(cat *),Bash(ls *),Read,Agent"
[[ -z "$WRITE_GRANTS" ]] || ALLOWED_TOOLS+=",${WRITE_GRANTS}"

# Capture Claude's output, then post it to Slack via slack_notifier.
# Claude has Slack MCP READ access. Its text output IS the briefing (posted by slack_notifier).
# Stream-json + log_claude_cost.py captures cost telemetry to claude-cost.log
# without changing the rest of the pipeline; the helper passes through only
# the final assistant text on stdout.
#
# `set +e` around the pipeline is load-bearing. Under the script's
# `set -euo pipefail`, a `claude` that dies in <1 s propagates through
# pipefail into this assignment and errexit kills the whole script right
# here: no retry, no validation, no Slack post, no failure line in the log,
# and the run log just stops after "Invoking Claude Code with skill: …".
# Capture the status instead and let the validate/retry ladder below run.
# The lane on Codex (docs/codex-harness.md, "Production"): EA_HARNESS=codex
# runs a briefing lane through codex_ghost.py run-lane --live as ea-bot,
# the same prompt, writes confined to the EA state. It runs only when the
# ghost arm's final verdict for briefings is GO (codex_ghost.py gate), or
# with EA_HARNESS_FORCE=1; otherwise the Claude run below goes ahead and
# the log says why.
CODEX_LANE=0
if [[ "$REMOTE_BRIEFING" == 0 && "${EA_HARNESS:-claude}" == "codex" ]]; then
    case "$AGENT" in
        morning-briefing|afternoon-catchup)
            GATE_WHY=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/codex_ghost.py" gate briefing 2>>"$LOG_FILE")
            GATE_RC=$?
            if [[ "$GATE_RC" == 0 || "${EA_HARNESS_FORCE:-0}" == 1 ]]; then
                CODEX_LANE=1
                log "Harness: codex (${GATE_WHY:-forced})"
            else
                log "Harness: claude; the codex gate said: ${GATE_WHY}"
            fi
            ;;
        *) log "Harness: claude; EA_HARNESS=codex covers the briefing lanes only" ;;
    esac
fi
if [[ "$CODEX_LANE" == 1 ]]; then
    set +e
    CLAUDE_OUTPUT=$(printf '%s' "$PROMPT" | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/codex_ghost.py" run-lane \
        --agent "$AGENT" --run-id "cron-${AGENT}-${TIMESTAMP}" --live 2>>"$LOG_FILE")
    CLAUDE_RC=$?
    set -e
    # The Claude block below is skipped; the output handling further down is shared.
    REMOTE_BRIEFING=1
    [[ "$CLAUDE_RC" == 0 ]] || CLAUDE_OUTPUT=":warning: ${AGENT}: the Codex lane failed (rc=${CLAUDE_RC}). The run log has the error. No Claude run was made; the next scheduled run tries again."
fi

if [[ "$REMOTE_BRIEFING" == 0 ]]; then
set +e
# EA_RUN_ID labels every draft this run creates, so the bot can give
# each its Send block without a Slack dispatch to claim it.
# The prompt goes in on stdin (printf is a builtin, so no exec argument
# limit applies): Linux caps one argument at 128 KiB, and -p "$PROMPT"
# with a large briefing would fail with E2BIG before claude starts.
CLAUDE_OUTPUT=$(printf '%s' "$PROMPT" | sudo -u ea-bot env \
    HOME="$EA_BOT_HOME" \
    EA_DIR="$EA_DIR" \
    EA_RUN_ID="cron-${AGENT}-${TIMESTAMP}" \
    PATH="$EA_BOT_SCRIPT_DIR/.venv/bin:$EA_BOT_HOME/.local/bin:$PATH" \
    ${GRAPH_CLIENT_ID:+GRAPH_CLIENT_ID="$GRAPH_CLIENT_ID"} \
    ${GRAPH_AUTHORITY:+GRAPH_AUTHORITY="$GRAPH_AUTHORITY"} \
    ${GRAPH_AUTH_METHOD:+GRAPH_AUTH_METHOD="$GRAPH_AUTH_METHOD"} \
    ${GRAPH_REDIRECT_URI:+GRAPH_REDIRECT_URI="$GRAPH_REDIRECT_URI"} \
    ${GRAPH_CLIENT_SECRET:+GRAPH_CLIENT_SECRET="$GRAPH_CLIENT_SECRET"} \
    ${GRAPH_SCOPES:+GRAPH_SCOPES="$GRAPH_SCOPES"} \
    bash -c "cd \"$EA_BOT_HOME\" && claude -p --model \"\$1\" --allowedTools \"\$2\" --disallowedTools \"\$3\" --permission-mode dontAsk --output-format stream-json --verbose \"\${@:4}\"" \
    -- "$MODEL" "$ALLOWED_TOOLS" "$DISALLOWED_TOOLS" \
    ${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"} \
    2>>"$LOG_FILE" \
    | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/log_claude_cost.py" \
        --agent "$AGENT" --model "$MODEL" 2>>"$LOG_FILE")
# pipefail is still in force here (set +e only cleared errexit), so this is
# the status of the failing stage, not just of log_claude_cost.py.
CLAUDE_RC=$?
set -e
if [[ "$CLAUDE_RC" != "0" ]]; then
    log "WARNING: Claude invocation exited rc=$CLAUDE_RC (stderr in $LOG_FILE)."
    log "Continuing to output validation; the retry below is the recovery path."
fi
fi

# -------------------------------------------------------------------
# Strip leading preamble: Claude often emits 1-2 paragraphs of reasoning
# ("I'll write the briefing now…", "The ledger command needs approval…")
# before the :sunny: anchor. The prompt forbids it but the model leaks
# anyway. Trim everything before the first line that starts with a Slack
# shortcode emoji so the Slack post is the actual briefing.
#
# The scan is BOUNDED to the first STRIP_PREAMBLE_MAX_LINES lines. An
# unbounded scan turns "trim the preamble" into "keep only the tail" for
# any agent whose output does not open with a shortcode, and
# validate_output anchor-checks only the briefing agents, so such a
# truncated tail still passes validation. A real preamble is 1-2 short
# paragraphs; an anchor further down than that is body text, not a
# header, so we post verbatim instead of truncating.
STRIP_PREAMBLE_MAX_LINES="${STRIP_PREAMBLE_MAX_LINES:-12}"

strip_preamble() {
    STRIP_PREAMBLE_MAX_LINES="$STRIP_PREAMBLE_MAX_LINES" "$PYTHON3" -c "
import os, re, sys
text = sys.stdin.read()
lines = text.splitlines()
limit = int(os.environ.get('STRIP_PREAMBLE_MAX_LINES') or 12)
anchor = re.compile(r'^\s*:[a-z0-9_]+:')
for i, ln in enumerate(lines[:limit]):
    if anchor.match(ln):
        if i:
            sys.stderr.write('preamble-strip: dropped %d line(s) above the '
                             ':shortcode: anchor\n' % i)
        sys.stdout.write('\n'.join(lines[i:]))
        sys.exit(0)
# No anchor in the first \`limit\` lines. Either there is no anchor at all
# (emit verbatim so validate_output sees and flags it) or the first one is
# deep in the body, where stripping would delete the report.
for i, ln in enumerate(lines):
    if anchor.match(ln):
        sys.stderr.write('preamble-strip: first :shortcode: anchor is at line '
                         '%d (> %d); posting verbatim, not truncating\n'
                         % (i + 1, limit))
        break
sys.stdout.write(text)
"
}

CLAUDE_OUTPUT=$(strip_preamble <<< "$CLAUDE_OUTPUT" 2>>"$LOG_FILE")

# -------------------------------------------------------------------
# Step 3: Validate output (catch meta-commentary failures)
# -------------------------------------------------------------------
validate_output() {
    local output="$1"
    local agent="$2"

    # Empty output is always a failure
    [[ -z "$output" ]] && { echo "empty"; return; }

    # Check for meta-commentary patterns (case-insensitive)
    local lower
    lower=$(echo "$output" | head -5 | tr '[:upper:]' '[:lower:]')
    local meta_patterns=(
        "state updated"
        "briefing posted"
        "posted to slack"
        "here's a summary"
        "here is a summary"
        "here's what was"
        "here is what was"
        "morning briefing complete"
        "afternoon catchup complete"
        "briefing is done"
        "briefing above"
    )
    for pat in "${meta_patterns[@]}"; do
        if echo "$lower" | grep -qF "$pat"; then
            echo "meta-commentary: $pat"
            return
        fi
    done

    # For briefing agents, check format anchor.
    # The validator's job is to catch meta-commentary, not to enforce a
    # specific emoji vocabulary. Accept any Slack shortcode emoji at the
    # start of the first line (after optional leading whitespace). Today
    # that covers :sunny: (morning briefing), :coffee: (afternoon catchup),
    # and any other Slack shortcode an agent may legitimately emit.
    # Meta-commentary like "State updated" or "Here is..." still fails
    # because it doesn't start with a :shortcode: pattern.
    case "$agent" in
        morning-briefing|afternoon-catchup|list-digest|admissions-digest)
            if ! echo "$output" | head -1 | grep -qE '^[[:space:]]*:[a-z0-9_]+:'; then
                echo "missing-format-anchor: expected a Slack shortcode emoji on first line"
                return
            fi
            ;;
    esac

    echo "ok"
}

VALIDATION=$(validate_output "$CLAUDE_OUTPUT" "$AGENT")

if [[ "$VALIDATION" != "ok" && "${REMOTE_BRIEFING:-0}" == 1 ]]; then
    log "OUTPUT VALIDATION FAILED for the remote briefing: $VALIDATION (posting it as written; no Claude retry)."
elif [[ "$VALIDATION" != "ok" ]]; then
    log "OUTPUT VALIDATION FAILED: $VALIDATION"
    # Snapshot the failed attempt: this file holds exactly what the model
    # produced ahead of the expected anchor.
    DEBUG_DUMP="$LOG_DIR/${AGENT}-failed-attempt-${TIMESTAMP}.txt"
    printf '%s' "$CLAUDE_OUTPUT" > "$DEBUG_DUMP"
    log "Failed-attempt output written to: $DEBUG_DUMP"
    log "First 200 chars of failed output: $(printf '%s' "$CLAUDE_OUTPUT" | head -c 200 | tr '\n' ' ')"
    log "Retrying Claude invocation (attempt 2/2)..."

    RETRY_PROMPT="$PROMPT

RETRY NOTICE: Your previous attempt failed validation because: $VALIDATION
The user saw useless output instead of the actual briefing.
You MUST write the briefing content as your text output. Start with :sunny: for morning or :coffee: for afternoon."

    # Same `set +e` reasoning as the first invocation: a dying retry must
    # not take the Slack post and the sentinel marker down with it.
    set +e
    CLAUDE_OUTPUT=$(printf '%s' "$RETRY_PROMPT" | sudo -u ea-bot env \
        HOME="$EA_BOT_HOME" \
        EA_DIR="$EA_DIR" \
    EA_RUN_ID="cron-${AGENT}-${TIMESTAMP}" \
        PATH="$EA_BOT_SCRIPT_DIR/.venv/bin:$EA_BOT_HOME/.local/bin:$PATH" \
        ${GRAPH_CLIENT_ID:+GRAPH_CLIENT_ID="$GRAPH_CLIENT_ID"} \
        ${GRAPH_AUTHORITY:+GRAPH_AUTHORITY="$GRAPH_AUTHORITY"} \
        ${GRAPH_AUTH_METHOD:+GRAPH_AUTH_METHOD="$GRAPH_AUTH_METHOD"} \
        ${GRAPH_REDIRECT_URI:+GRAPH_REDIRECT_URI="$GRAPH_REDIRECT_URI"} \
        ${GRAPH_CLIENT_SECRET:+GRAPH_CLIENT_SECRET="$GRAPH_CLIENT_SECRET"} \
        ${GRAPH_SCOPES:+GRAPH_SCOPES="$GRAPH_SCOPES"} \
        bash -c "cd \"$EA_BOT_HOME\" && claude -p --model \"\$1\" --allowedTools \"\$2\" --disallowedTools \"\$3\" --permission-mode dontAsk --output-format stream-json --verbose \"\${@:4}\"" \
        -- "$MODEL" "$ALLOWED_TOOLS" "$DISALLOWED_TOOLS" \
        ${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"} \
        2>>"$LOG_FILE" \
        | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/log_claude_cost.py" \
            --agent "${AGENT}-retry" --model "$MODEL" 2>>"$LOG_FILE")
    RETRY_RC=$?
    set -e
    if [[ "$RETRY_RC" != "0" ]]; then
        log "WARNING: retry invocation exited rc=$RETRY_RC (stderr in $LOG_FILE)."
    fi

    # Apply the same bounded preamble strip to the retry output.
    CLAUDE_OUTPUT=$(strip_preamble <<< "$CLAUDE_OUTPUT" 2>>"$LOG_FILE")

    VALIDATION=$(validate_output "$CLAUDE_OUTPUT" "$AGENT")
    if [[ "$VALIDATION" != "ok" ]]; then
        log "RETRY ALSO FAILED: $VALIDATION — posting whatever we have."
    else
        log "Retry succeeded."
    fi
fi

# Never post a CLI/harness error as if it were the digest. When the model was
# never reached (quota, auth, overload), $CLAUDE_OUTPUT is claude's own error
# string, which must never post as the digest. Post a one-line status
# instead; the failed-attempt file and the sentinel marker still record the
# failed run.
# Pattern list mirrors tests/evals/provider._CLI_FAILURE_MARKERS.
if [[ "$VALIDATION" != "ok" && -n "$CLAUDE_OUTPUT" ]]; then
    if printf '%s' "$CLAUDE_OUTPUT" | head -3 | grep -qiE \
        "hit your (session |weekly |usage )?limit|rate limit|usage limit reached|credit balance is too low|invalid api key|authentication_error|please run /login|oauth token has expired|overloaded_error"; then
        FIRST_LINE=$(printf '%s' "$CLAUDE_OUTPUT" | head -1 | head -c 160)
        log "Harness-level failure; posting a status line, not the raw CLI error."
        CLAUDE_OUTPUT=":warning: ${AGENT} could not run — Claude was unavailable (${FIRST_LINE}). The attempt is logged; the next scheduled run catches up."
    fi
fi

# -------------------------------------------------------------------
# Step 3b: pre-send open-claim audit. A post that asserts which items are
# open must first ask whether they really are. open_claims_audit.py
# finds every open ledger row the draft presents as open, gathers the
# contrary evidence the scrubber's helpers can see (zero tokens), and
# only when some item HAS such evidence asks a tool-less second Claude
# call for a verdict per item. A named row that a future follow-up or
# snooze date parks needs no question: it is dropped from the draft. Rows judged done are closed in the ledger
# and logged to the missed-closure backlog; the agent then rewrites its
# draft ONCE with the corrections. A rewrite that fails validation is
# discarded and the original draft posts (the rows are closed either
# way). Nothing here posts, and nothing here runs for agents that do not
# assert open items.
# -------------------------------------------------------------------
invoke_claude() {
    # $1 prompt  $2 model  $3 allowed tools  $4 disallowed tools  $5 cost tag
    # Same env block as the main invocation above; the caller wraps this
    # in `set +e` for the same reason. The prompt goes in on stdin.
    # A tool-less call (no allowed tools: the audit's verdict) loads no
    # MCP server and no claude.ai connector.
    local -a mcp_env=() mcp_flags=()
    if [[ -z "$3" ]]; then
        mcp_env=(ENABLE_CLAUDEAI_MCP_SERVERS=false)
        mcp_flags=(--strict-mcp-config)
    fi
    printf '%s' "$1" | sudo -u ea-bot env \
        HOME="$EA_BOT_HOME" \
        EA_DIR="$EA_DIR" \
    EA_RUN_ID="cron-${AGENT}-${TIMESTAMP}" \
        PATH="$EA_BOT_SCRIPT_DIR/.venv/bin:$EA_BOT_HOME/.local/bin:$PATH" \
        ${GRAPH_CLIENT_ID:+GRAPH_CLIENT_ID="$GRAPH_CLIENT_ID"} \
        ${GRAPH_AUTHORITY:+GRAPH_AUTHORITY="$GRAPH_AUTHORITY"} \
        ${GRAPH_AUTH_METHOD:+GRAPH_AUTH_METHOD="$GRAPH_AUTH_METHOD"} \
        ${GRAPH_REDIRECT_URI:+GRAPH_REDIRECT_URI="$GRAPH_REDIRECT_URI"} \
        ${GRAPH_CLIENT_SECRET:+GRAPH_CLIENT_SECRET="$GRAPH_CLIENT_SECRET"} \
        ${GRAPH_SCOPES:+GRAPH_SCOPES="$GRAPH_SCOPES"} \
        ${mcp_env[@]+"${mcp_env[@]}"} \
        bash -c "cd \"$EA_BOT_HOME\" && claude -p --model \"\$1\" --allowedTools \"\$2\" --disallowedTools \"\$3\" --permission-mode dontAsk --output-format stream-json --verbose \"\${@:4}\"" \
        -- "$2" "$3" "$4" \
        ${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"} \
        ${mcp_flags[@]+"${mcp_flags[@]}"} \
        2>>"$LOG_FILE" \
        | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/log_claude_cost.py" \
            --agent "$5" --model "$2" 2>>"$LOG_FILE"
}

AUDIT_SUMMARY=""
case "$AGENT" in
    morning-briefing|afternoon-catchup|weekly-audit)
        # A remote briefing ran the audit itself, with the remote model.
        if [[ "$VALIDATION" == "ok" && -n "$CLAUDE_OUTPUT" && "${REMOTE_BRIEFING:-0}" == 0 ]]; then
            # The audit writes these files and reads them back as the owner,
            # so they live in a private temp dir, out of ea-bot's reach. A
            # copy of each lands in the state dir afterwards, written as
            # ea-bot, for the record.
            AUDIT_WORK=$(mktemp -d "${TMPDIR:-/tmp}/ea-audit.XXXXXX")
            AUDIT_DRAFT="$AUDIT_WORK/${AGENT}-draft.txt"
            AUDIT_JSON="$AUDIT_WORK/open-claims-audit-latest.json"
            AUDIT_PROMPT_FILE="$AUDIT_WORK/open-claims-audit-prompt.txt"
            AUDIT_VERDICTS="$AUDIT_WORK/open-claims-audit-verdicts.txt"
            AUDIT_NOTE="$AUDIT_WORK/open-claims-audit-note.txt"
            printf '%s' "$CLAUDE_OUTPUT" > "$AUDIT_DRAFT"
            : > "$AUDIT_NOTE"
            set +e
            AUDIT_COUNTS=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/open_claims_audit.py" prepare \
                --agent "$AGENT" --post-file "$AUDIT_DRAFT" --briefing "$OUTPUT_FILE" \
                --out "$AUDIT_JSON" --prompt-file "$AUDIT_PROMPT_FILE" 2>>"$LOG_FILE")
            AUDIT_PREP_RC=$?
            set -e
            read -r AUDIT_ASKED AUDIT_PARKED _ <<< "${AUDIT_COUNTS:-0 0}"
            AUDIT_ASKED=${AUDIT_ASKED:-0}
            AUDIT_PARKED=${AUDIT_PARKED:-0}
            if [[ "$AUDIT_PREP_RC" == "0" && ( "$AUDIT_ASKED" != "0" || "$AUDIT_PARKED" != "0" ) ]]; then
                : > "$AUDIT_VERDICTS"
                if [[ "$AUDIT_PARKED" != "0" ]]; then
                    log "Open-claim audit: ${AUDIT_PARKED} item(s) in the draft are parked until a later date; removing them."
                fi
                if [[ "$AUDIT_ASKED" != "0" ]]; then
                    log "Open-claim audit: ${AUDIT_ASKED} item(s) presented as open have contrary evidence; asking."
                    AUDIT_PROMPT=$(cat "$AUDIT_PROMPT_FILE")
                    set +e
                    AUDIT_REPLY=$(invoke_claude "$AUDIT_PROMPT" "sonnet" "" "$DISALLOWED_TOOLS" "${AGENT}-audit")
                    set -e
                    printf '%s' "$AUDIT_REPLY" > "$AUDIT_VERDICTS"
                fi
                set +e
                AUDIT_SUMMARY=$(EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/open_claims_audit.py" apply \
                    --audit "$AUDIT_JSON" --verdicts-file "$AUDIT_VERDICTS" --note-file "$AUDIT_NOTE" 2>>"$LOG_FILE")
                AUDIT_APPLY_RC=$?
                set -e
                log "Open-claim audit: ${AUDIT_SUMMARY:-apply failed rc=$AUDIT_APPLY_RC}"
                if [[ "$AUDIT_APPLY_RC" == "0" && -s "$AUDIT_NOTE" ]]; then
                    AUDIT_REWRITE_PROMPT="$PROMPT"$'\n\n'"$(cat "$AUDIT_NOTE")"
                    set +e
                    AUDIT_OUTPUT=$(invoke_claude "$AUDIT_REWRITE_PROMPT" "$MODEL" "$ALLOWED_TOOLS" "$DISALLOWED_TOOLS" "${AGENT}-audit-rewrite")
                    set -e
                    AUDIT_OUTPUT=$(strip_preamble <<< "$AUDIT_OUTPUT" 2>>"$LOG_FILE")
                    if [[ "$(validate_output "$AUDIT_OUTPUT" "$AGENT")" == "ok" ]]; then
                        CLAUDE_OUTPUT="$AUDIT_OUTPUT"
                        log "Open-claim audit: draft rewritten with the corrections."
                    else
                        log "Open-claim audit: the rewrite failed validation; posting the original draft (the rows are closed in the ledger regardless)."
                    fi
                fi
            else
                log "Open-claim audit: nothing to ask (asked=${AUDIT_ASKED:-0}, parked=${AUDIT_PARKED:-0}, rc=${AUDIT_PREP_RC})."
            fi
            for _audit_file in "$AUDIT_DRAFT" "$AUDIT_JSON" "$AUDIT_PROMPT_FILE" "$AUDIT_VERDICTS" "$AUDIT_NOTE"; do
                if [[ -f "$_audit_file" ]]; then
                    "$SCRIPT_DIR/scripts/owner-io.sh" write "$STATE_DIR/${_audit_file##*/}" \
                        < "$_audit_file" 2>>"$LOG_FILE" || true
                fi
            done
            rm -rf -- "$AUDIT_WORK"
        fi
        ;;
esac

# list-digest: the skill answers "nothing worth posting" with a one-line
# sentinel (skills/list-digest.md). Skip the post, but still archive below:
# the digests were judged, and leaving them in the inbox is what the agent
# exists to stop. Anything else that validated is a real post.
LIST_DIGEST_DISPOSITION=""
if [[ "$AGENT" == "list-digest" && "$VALIDATION" == "ok" ]]; then
    if printf '%s' "$CLAUDE_OUTPUT" | head -1 | grep -q "NOTHING_RELEVANT"; then
        log "list-digest: nothing relevant in this digest; not posting."
        LIST_DIGEST_DISPOSITION="silent"
        CLAUDE_OUTPUT=""
    else
        LIST_DIGEST_DISPOSITION="posted"
    fi
fi

# Post to Slack via bot token (runs as the owner, has direct Slack access).
# Every report is written to a file first; the post is for profiles that
# have Slack. The report dir is ea-bot space, so the file is written as
# ea-bot (scripts/owner-io.sh).
POST_RC=""
if [[ -n "$CLAUDE_OUTPUT" ]]; then
    REPORT_DIR="$STATE_DIR/reports"
    "$SCRIPT_DIR/scripts/owner-io.sh" mkdir "$REPORT_DIR" 2>/dev/null || true
    printf '%s\n' "$CLAUDE_OUTPUT" | "$SCRIPT_DIR/scripts/owner-io.sh" write "$REPORT_DIR/${AGENT}-latest.md" 2>/dev/null \
        && log "Report written to $REPORT_DIR/${AGENT}-latest.md" || true
    # The Codex ghost arm (docs/codex-harness.md, "The ghost arm"): with a
    # window open, the same prompt is replayed on Codex off to the side and
    # judged against this report. The enqueue returns at once; a closed
    # window is a one-line skip. Never for a run Codex itself produced.
    if [[ "${CODEX_LANE:-0}" == 0 ]]; then
        GHOST_NOTE=$(printf '%s' "$PROMPT" | EA_DIR="$EA_DIR" "$PYTHON3" "$SCRIPT_DIR/preprocessor/codex_ghost.py" enqueue-lane \
            --agent "$AGENT" --run-id "cron-${AGENT}-${TIMESTAMP}" --report "$REPORT_DIR/${AGENT}-latest.md" 2>>"$LOG_FILE" || true)
        [[ -n "$GHOST_NOTE" ]] && log "Codex ghost: $GHOST_NOTE"
    fi
    if [[ "$HAS_SLACK" == 1 ]]; then
        # `set +e`: without it, errexit + pipefail kill the script the moment
        # the post fails, so POST_RC could only ever be 0 by the time it is
        # tested below and the "Slack post FAILED" branch is unreachable.
        set +e
        "$PYTHON3" -c "
import sys, os
sys.path.insert(0, '$SCRIPT_DIR/preprocessor')
import slack_notifier as slack
msg = sys.stdin.read().strip()
sys.exit(0 if (msg and slack.post_to_slack('ea-assistant', msg)) else 1)
" <<< "$CLAUDE_OUTPUT" 2>&1 | tee -a "$LOG_FILE"
        POST_RC="${PIPESTATUS[0]}"
        set -e
    else
        log "Profile $PROFILE has no Slack; the report file is the delivery."
        POST_RC=0
        # A phone push when the owner set one up (preprocessor/push_notify.py);
        # without push.json it does nothing, and a failed push never fails the run.
        printf '%s\n' "$CLAUDE_OUTPUT" | "$PYTHON3" "$SCRIPT_DIR/preprocessor/push_notify.py" report --agent "$AGENT" >>"$LOG_FILE" 2>&1 \
            || log "Push notification failed; the run log has the reason."
    fi
    # Persist the posted briefing text in the run log. Without this the run
    # logs hold only orchestration noise, and auditing what was actually
    # briefed requires scraping Slack.
    {
        echo "=== POSTED BRIEFING TEXT (verbatim) ==="
        printf '%s\n' "$CLAUDE_OUTPUT"
        echo "=== END POSTED BRIEFING TEXT ==="
    } >> "$LOG_FILE"
    if [[ "$POST_RC" == "0" ]]; then
        [[ "$HAS_SLACK" == 1 ]] && log "Posted to Slack." || log "Report delivered as a file."
        # Record post success so sentinel.py can verify briefings actually
        # landed (a deferral during quiet hours counts as a successful
        # handoff — the flush path delivers it, and the sentinel watches
        # the deferred queue separately). Only the agents the sentinel
        # monitors get a marker. The marker is ea-bot space: the old one is
        # read and the new one written as ea-bot, and a failed merge leaves
        # the old one in place.
        case "$AGENT" in
            morning-briefing|afternoon-catchup|weekly-audit)
                POST_MARKER="$STATE_DIR/agent-post-success.json"
                MARKER_JSON=$({ "$SCRIPT_DIR/scripts/owner-io.sh" read "$POST_MARKER" 2>/dev/null || true; } | "$PYTHON3" -c "
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import quiet_hours
try:
    data = json.loads(sys.stdin.read())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data[sys.argv[2]] = {
    'date': quiet_hours._local_date(),
    'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'validation': sys.argv[3],
}
sys.stdout.write(json.dumps(data, indent=2))
" "$SCRIPT_DIR/preprocessor" "$AGENT" "$VALIDATION" 2>/dev/null) || MARKER_JSON=""
                if [[ -n "$MARKER_JSON" ]]; then
                    printf '%s' "$MARKER_JSON" | "$SCRIPT_DIR/scripts/owner-io.sh" write "$POST_MARKER" 2>/dev/null || true
                fi
                ;;
        esac
    else
        log "ERROR: Slack post FAILED — sentinel will alert."
    fi
elif [[ "$LIST_DIGEST_DISPOSITION" == "silent" ]]; then
    : # judged, nothing relevant, nothing to post
else
    log "WARNING: Claude produced no output."
fi

# list-digest: archive the digests only once their content is either posted
# or judged not worth posting. A Claude failure, a validation failure or a
# failed Slack post all leave them in the inbox, so the next scheduled run
# retries; nothing is archived unseen. Runs as the owner (this script's
# user), the same way the preprocess step read the mail.
if [[ "$AGENT" == "list-digest" ]]; then
    if [[ "$LIST_DIGEST_DISPOSITION" == "silent" || ( "$LIST_DIGEST_DISPOSITION" == "posted" && "$POST_RC" == "0" ) ]]; then
        set +e
        ARCHIVE_OUT=$("$PYTHON3" "$SCRIPT_DIR/preprocessor/list_digest.py" archive \
            --input "$OUTPUT_FILE" --disposition "$LIST_DIGEST_DISPOSITION" 2>&1)
        ARCHIVE_RC=$?
        set -e
        log "list-digest archive (rc=$ARCHIVE_RC): $ARCHIVE_OUT"
        if [[ "$ARCHIVE_RC" != "0" ]]; then
            "$PYTHON3" -c '
import sys
sys.path.insert(0, sys.argv[1])
import slack_notifier as slack
slack.post_to_slack("ea-assistant", sys.argv[2])
' "$SCRIPT_DIR/preprocessor" ":warning: list-digest: a digest was judged but could not be archived; it is still in the inbox. Details: $LOG_FILE" \
                2>&1 | tee -a "$LOG_FILE" || true
        fi
    else
        log "list-digest: digests NOT archived (disposition='${LIST_DIGEST_DISPOSITION:-none}', post rc='${POST_RC:-n/a}'); they stay in the inbox for the next run."
    fi
fi

# Opik trace (best-effort, never blocks)
"$PYTHON3" "$SCRIPT_DIR/preprocessor/opik_trace.py" record-briefing \
    --agent "$AGENT" --briefing-file "$OUTPUT_FILE" \
    --output "${CLAUDE_OUTPUT:0:2000}" --duration "$SECONDS" \
    --model "$MODEL" --validation "$VALIDATION" \
    2>/dev/null || true

log "Done."
