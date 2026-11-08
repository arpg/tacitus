#!/usr/bin/env bash
#
# Open an interactive Claude Code session as the ea-bot user, with the
# same MCP servers, credentials, allowedTools, and cwd as the Slack bot
# uses — BUT with the externally-visible "send" actions blacklisted via
# --disallowedTools. Drafts can still be created and reviewed; the user
# sends them through the normal Slack-button or Outlook-UI path.
#
# Usage:
#   bash scripts/ea-bot-shell.sh              # blank session
#   bash scripts/ea-bot-shell.sh "<prompt>"   # one-shot prompt mode
#   bash scripts/ea-bot-shell.sh --resume <uuid>      # continue a session
#   bash scripts/ea-bot-shell.sh --thread <thread_ts> # continue a Slack
#                                                     # thread's session
#
# A new interactive session prints its id first. In Slack, "adopt session
# <uuid>" in any thread continues that conversation there; --resume or
# --thread brings it back here. One driver at a time (see
# preprocessor/slack_sessions.py).
#
# Why this exists:
#   The button-required confirm-token gate (graph_actions.py:cmd_send_draft
#   etc.) is enforced by code, not by the LLM allowlist. An interactive
#   session running as ea-bot CAN technically mint a token via
#   `request-confirm` and then call `send-draft` directly. This wrapper
#   denies both subcommands so the only path to actually sending an email
#   (or sending a calendar invite to others) remains the Slack :lock:
#   button flow the bot enforces. Reversible / no-external-effect actions
#   (create-draft, update-draft, archive-message, get-message, ledger
#   ops, etc.) are still freely usable.
set -euo pipefail

# ea-bot's home from the user database (/home/ea-bot on Linux, /Users/ea-bot
# on macOS). Every `python3 -c` here runs isolated (-I), so a module in the
# caller's working directory is never imported.
EA_BOT_HOME=$(cd / && python3 -I -c 'import pwd; print(pwd.getpwnam("ea-bot").pw_dir)') \
    || { echo "ea-bot-shell: no ea-bot user" >&2; exit 1; }

# This script and the owner-side code it runs (the deny lists,
# ea_config.py, plugin_registry.py, slack_sessions.py) come from the tree it
# sits in. ea-bot owns the directory above its ~/ea-system and can swap that
# tree, so the owner never runs it from there: run the deployed copy in
# /opt/ea-system, whose parent root owns (deploy/harden-permissions.sh), or
# your own checkout.
OWNER_CODE_DIR=/opt/ea-system
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
CODE_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
EA_BOT_REAL="$(cd "$EA_BOT_HOME" 2>/dev/null && pwd -P || printf '%s' "$EA_BOT_HOME")"
case "$CODE_DIR/" in
    "$EA_BOT_REAL"/*)
        echo "ea-bot-shell: refusing to run owner-side code from $CODE_DIR, ea-bot's tree." >&2
        if [[ -f "$OWNER_CODE_DIR/scripts/ea-bot-shell.sh" ]]; then
            echo "  Run: bash $OWNER_CODE_DIR/scripts/ea-bot-shell.sh $*" >&2
        else
            echo "  Run deploy/harden-permissions.sh to create $OWNER_CODE_DIR, then" >&2
            echo "  bash $OWNER_CODE_DIR/scripts/ea-bot-shell.sh" >&2
        fi
        exit 1 ;;
esac

# Load the shared disallow list. Single source of truth at
# scripts/disallowed-tools.txt — see that file for rationale.
DISALLOWED_FILE="$SCRIPT_DIR/disallowed-tools.txt"
if [[ ! -f "$DISALLOWED_FILE" ]]; then
    echo "ea-bot-shell: missing $DISALLOWED_FILE" >&2
    exit 1
fi
# Strip comments + blank lines, then join with commas.
DISALLOWED_CSV=$(grep -v '^[[:space:]]*#' "$DISALLOWED_FILE" \
    | sed '/^[[:space:]]*$/d' \
    | paste -sd ',' -)
# "drafting": false in ea-config.json (any mode) also denies every drafting
# verb. A failed read denies them too.
if ! python3 "$CODE_DIR/preprocessor/ea_config.py" feature drafting >/dev/null 2>&1; then
    DISALLOWED_CSV+=",$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/disallowed-tools.no-drafting.txt" \
        | sed '/^[[:space:]]*$/d' | paste -sd ',' -)"
fi
# Denials the enabled plugins declare (docs/plugins.md), from the owner's lock.
PLUGIN_DENIED=$(python3 "$CODE_DIR/preprocessor/plugin_registry.py" denied-tools 2>/dev/null || true)
[[ -n "$PLUGIN_DENIED" ]] && DISALLOWED_CSV+="${DISALLOWED_CSV:+,}$PLUGIN_DENIED"

# Use `sudo -u ea-bot bash -c` (NOT `sudo -i`) because ea-bot's login
# shell is /usr/sbin/nologin by design (isolation hardening). We set
# the env ea-bot needs explicitly. cwd is the ea-system clone so claude
# finds CLAUDE.md and the right .claude/ context.
#
# --dangerously-skip-permissions: the ea-bot UNIX USER is the sandbox,
# the same argument scripts/ea-resident-inner.sh makes for the resident
# pane.
# The --disallowed-tools deny list STILL applies in bypass mode, and the
# send/create-event verbs are gated by code-level confirm tokens on top,
# so bypass changes how often you press Enter, not what the session can
# reach. Set EA_SHELL_MODEL to pin a model (e.g. claude-opus-4-8);
# unset, claude's own default applies.
#
# Prompt arg (if any) is passed as $0 / $1 to the inner bash so we
# don't have to wrestle with nested-quote expansion. Empty prompt =
# interactive session.
# The claude ea-bot runs, in the bot's order (ea-chat-bot.py,
# _detect_claude_bin): its own install, else a system-wide one.
EA_BOT_CLAUDE=""
for c in "$EA_BOT_HOME/.local/bin/claude" /usr/local/bin/claude /usr/bin/claude; do
    if (cd / && sudo -n -u ea-bot test -x "$c") 2>/dev/null; then EA_BOT_CLAUDE="$c"; break; fi
done
[[ -n "$EA_BOT_CLAUDE" ]] || { echo "ea-bot-shell: no claude ea-bot can run" >&2; exit 1; }
RESUME_ID=""
case "${1:-}" in
    --resume)
        RESUME_ID="${2:?--resume needs a session uuid}"; shift 2 ;;
    --thread)
        THREAD_TS="${2:?--thread needs a Slack thread_ts}"; shift 2
        RESUME_ID=$(EA_DIR="$EA_BOT_HOME/.ea" python3 \
            "$CODE_DIR/preprocessor/slack_sessions.py" show \
            --thread "$THREAD_TS" 2>/dev/null \
            | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print((d.get("entry") or {}).get("session_id",""))')
        if [[ -z "$RESUME_ID" ]]; then
            echo "ea-bot-shell: no session registered for thread $THREAD_TS" >&2
            exit 1
        fi
        # The registry is ea-bot space; only a uuid goes on to claude.
        if [[ ! "$RESUME_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
            echo "ea-bot-shell: thread $THREAD_TS maps to no valid session id" >&2
            exit 1
        fi ;;
esac
PROMPT_ARG="${*:-}"
MODEL_ARG="${EA_SHELL_MODEL:-}"
# Session identity: --resume continues one; otherwise mint the id here so
# it can be printed (the CLI does not announce it) and adopted from Slack.
if [[ -n "$RESUME_ID" ]]; then
    SESSION_FLAG="--resume"; SESSION_ID="$RESUME_ID"
else
    SESSION_FLAG="--session-id"
    SESSION_ID=$(python3 -I -c 'import uuid; print(uuid.uuid4())')
fi
if [[ -z "$PROMPT_ARG" ]]; then
    echo "ea-bot-shell: session $SESSION_ID"
    echo "  in Slack, in any thread:  adopt session $SESSION_ID"
    echo "  as its own Slack room:    open a channel for session $SESSION_ID   (#tmp-${SESSION_ID:0:8})"
    echo "  back here later:          bash scripts/ea-bot-shell.sh --resume $SESSION_ID"
fi
cd /
exec sudo -u ea-bot env \
    HOME="$EA_BOT_HOME" \
    EA_DIR="$EA_BOT_HOME/.ea" \
    PATH="$EA_BOT_HOME/ea-system/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin:$EA_BOT_HOME/.local/bin" \
    EA_BOT_CLAUDE="$EA_BOT_CLAUDE" \
    bash -c '
        cd "$HOME/ea-system" || exit 1
        MODEL_FLAGS=()
        if [[ -n "$2" ]]; then MODEL_FLAGS=(--model "$2"); fi
        if [[ -n "$1" ]]; then
            exec "$EA_BOT_CLAUDE" \
                --disallowed-tools "$0" \
                --dangerously-skip-permissions \
                "${MODEL_FLAGS[@]}" \
                "$3" "$4" \
                -p "$1"
        else
            exec "$EA_BOT_CLAUDE" \
                --disallowed-tools "$0" \
                --dangerously-skip-permissions \
                "${MODEL_FLAGS[@]}" \
                "$3" "$4"
        fi
    ' "$DISALLOWED_CSV" "$PROMPT_ARG" "$MODEL_ARG" "$SESSION_FLAG" "$SESSION_ID"
