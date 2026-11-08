#!/usr/bin/env bash
#
# Inner launcher for the resident EA Claude pane. Runs INSIDE the tmux
# pane (as the owner) and execs an interactive Claude Code session as ea-bot,
# with the same trust envelope as the Slack bot's subprocesses:
#   - scripts/disallowed-tools.txt denies the send/create-event verbs
#     (the :lock: button flow stays the only path to external effects)
#   - the same --allowedTools list the bot passes, so tools run without
#     interactive permission prompts (a prompt would stall the bridge;
#     anything OUTSIDE the allowlist still prompts — attach to approve:
#     tmux attach -t ea-resident)
#
# Session identity comes from ~/.ea/state/resident-pane.json (written by
# ea-resident-pane.sh). First boot uses --session-id <uuid>; later boots
# --resume <uuid>, so the resident conversation survives pane restarts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="${EA_STATE_FILE:-/home/ea-bot/.ea/state/resident-pane.json}"

DISALLOWED_CSV=$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/disallowed-tools.txt" \
    | sed '/^[[:space:]]*$/d' | paste -sd ',' -)
# "drafting": false in ea-config.json (any mode) also denies every drafting
# verb. A failed read denies them too.
if ! python3 "$SCRIPT_DIR/../preprocessor/ea_config.py" feature drafting >/dev/null 2>&1; then
    DISALLOWED_CSV+=",$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/disallowed-tools.no-drafting.txt" \
        | sed '/^[[:space:]]*$/d' | paste -sd ',' -)"
fi
# Denials the enabled plugins declare (docs/plugins.md), from the owner's lock.
PLUGIN_DENIED=$(python3 "$SCRIPT_DIR/../preprocessor/plugin_registry.py" denied-tools 2>/dev/null || true)
[[ -n "$PLUGIN_DENIED" ]] && DISALLOWED_CSV+="${DISALLOWED_CSV:+,}$PLUGIN_DENIED"

# The state file is ea-bot space and the id goes to ea-bot's claude, so the
# file is read as ea-bot and the id must be a uuid.
SESSION_ID=$("$SCRIPT_DIR/owner-io.sh" read "$STATE_FILE" | python3 -c "
import json,sys
print(json.load(sys.stdin)['session_id'])
")
if [[ ! "$SESSION_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    echo "[resident] $STATE_FILE holds no valid session id; not starting" >&2
    exit 1
fi

TRANSCRIPT="/home/ea-bot/.claude/projects/-home-ea-bot-ea-system/${SESSION_ID}.jsonl"
if sudo -n -u ea-bot test -f "$TRANSCRIPT"; then
    SESSION_FLAGS=(--resume "$SESSION_ID")
    echo "[resident] resuming session ${SESSION_ID:0:8}…"
else
    SESSION_FLAGS=(--session-id "$SESSION_ID")
    echo "[resident] starting NEW session ${SESSION_ID:0:8}…"
fi

# --dangerously-skip-permissions: the ea-bot UNIX USER is the sandbox,
# which is the point of the user separation. An interactive permission
# prompt has nobody at the keyboard and stalls the whole dispatch lane.
# The --disallowed-tools deny list STILL applies in bypass mode, and the
# send/create-event verbs are additionally gated by code-level confirm
# tokens. There is no allow list in this lane: bypass mode makes one moot.
exec sudo -n -u ea-bot env \
    HOME=/home/ea-bot \
    SHELL=/bin/bash \
    EA_DIR=/home/ea-bot/.ea \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin:/home/ea-bot/.local/bin \
    bash -c '
        cd /home/ea-bot/ea-system || exit 1
        exec /home/ea-bot/.local/bin/claude \
            "$1" "$2" \
            --disallowed-tools "$0" \
            --dangerously-skip-permissions \
            --model "$3"
    ' "$DISALLOWED_CSV" "${SESSION_FLAGS[@]}" "${EA_RESIDENT_MODEL:-claude-opus-5-5[1m]}"
