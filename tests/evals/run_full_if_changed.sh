#!/usr/bin/env bash
#
# Run full-mode evals (invokes Claude, uses tokens) only if prompt-affecting
# files changed since the last successful run.
#
# Designed for nightly cron. Skips silently if nothing changed.
#
# The gates are the owner's (tests/evals/eval_gates.py): the on/off
# setting is `eval_full_enabled` in <EA_PRIVATE_DIR>/evals.json, and the
# commit the last run claimed is <EA_PRIVATE_DIR>/evals/last-full-commit.
# Neither lives in ~/.ea, which the resident writes: the run is a
# tool-using `claude -p` as the owner (tests/evals/provider.py).
#
# Usage:
#   ./tests/evals/run_full_if_changed.sh              # default: sonnet
#   ./tests/evals/run_full_if_changed.sh --model opus  # use opus
#   ./tests/evals/run_full_if_changed.sh --force       # ignore last-commit check
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# This script's logs and gates are the owner's alone, in the private dir.
GATES="$SCRIPT_DIR/eval_gates.py"
LOG_DIR="${EA_PRIVATE_DIR:-$HOME/.ea-private}/logs"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true

# The owner's on/off setting. A helper that fails reads as off: no quota
# is spent on a gate nobody can read.
EVAL_ENABLED=$(python3 "$GATES" enabled full || echo "False")

if [[ "$EVAL_ENABLED" != "True" ]]; then
    echo "[$(date +%H:%M:%S)] Full-mode evals disabled (eval_full_enabled in the owner's evals.json, or an unreadable gate). Skipping."
    exit 0
fi

MODEL="sonnet"
FORCE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) MODEL="$2"; shift 2 ;;
        --force) FORCE=true; shift ;;
        *) shift ;;
    esac
done

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/eval-full-$TIMESTAMP.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# Files that affect LLM output quality. Same list as post-commit hook.
PROMPT_PATTERNS="skills/ CLAUDE.md preprocessor/ ea-chat-bot.py run-agent.sh"

HEAD_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")

# Record the commit we are about to evaluate. This MUST happen before the
# eval run, not after it: the write used to sit downstream of
# `RESULT=$(run_evals.py … | tee)`, and because run_evals.py exits 1 on any
# fixture failure, pipefail + errexit killed the script at that assignment
# every single night. The state file was therefore never written, LAST_COMMIT
# was always empty, the gate above always read "first run", and the full
# 19-fixture Sonnet suite burned quota nightly whether or not anything had
# changed (B9-01: 20 consecutive logs, zero state writes).
#
# Writing it up front also encodes the intended policy: one full run per
# prompt-affecting commit. A failing run still counts as that commit's run —
# re-running the same commit cannot produce a different verdict, and the
# failure is reported below rather than retried.
#
# The value reaches `git diff` as a revision, so only a bare commit id is
# taken from it (eval_gates.py checks it too); anything else reads as no
# state at all.
read_state() {
    local v
    v=$(python3 "$GATES" last-commit 2>/dev/null | head -c 100 | tr -d '[:space:]') || v=""
    if [[ "$v" =~ ^[0-9a-f]{7,64}$ ]]; then
        printf '%s' "$v"
    fi
}
PREV_STATE=$(read_state)
write_state() {
    [[ -n "$HEAD_COMMIT" ]] || return 0
    python3 "$GATES" set-last-commit "$HEAD_COMMIT" \
        || log "Could not record commit ${HEAD_COMMIT:0:8} in the owner's eval state."
}

# Un-claim the commit. A run that could not be MEASURED (quota block, auth
# failure, every fixture timing out) has not evaluated this commit, so leaving
# the claim in place would skip it forever: the gate above would see
# HEAD == LAST_COMMIT the next night and exit 0 without ever testing the code.
# Restore whatever the state file held before this run.
unclaim_state() {
    if [[ -n "$PREV_STATE" ]]; then
        python3 "$GATES" set-last-commit "$PREV_STATE" || true
    else
        python3 "$GATES" clear-last-commit || true
    fi
}

# Send a failure to the health channel (the main channel when none is
# configured), where the other checks report. The cron line is
# `… >> ~/.ea-private/logs/eval-full.log 2>&1`, so cron mails nothing and a non-zero
# exit reaches no one; without this, a real regression stays in a log file.
# Deferred during quiet hours by slack_notifier and delivered in the morning
# digest, which is the right cadence for a 2 AM job. A diagnostic: it posts
# only in debug mode (chat_notify, Debug mode), and the log keeps it either way.
alert() {
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/preprocessor')
import slack_notifier as slack
slack.post_to_slack(slack.health_channel_key(), sys.stdin.read(), diagnostic=True)
" <<< "$1" >>"$LOG_FILE" 2>&1 || echo "[alert] Slack notify failed (see $LOG_FILE)"
}

if ! $FORCE; then
    # Check if anything changed since last successful run
    LAST_COMMIT=$(read_state)

    if [[ "$HEAD_COMMIT" == "$LAST_COMMIT" ]]; then
        log "No new commits since last full eval ($LAST_COMMIT). Skipping."
        exit 0
    fi

    # Check if prompt-affecting files changed
    if [[ -n "$LAST_COMMIT" ]]; then
        CHANGED=$(git -C "$REPO_ROOT" diff --name-only "$LAST_COMMIT" HEAD 2>/dev/null || echo "")
    else
        # First run — assume everything changed
        CHANGED="CLAUDE.md"
    fi

    PROMPT_CHANGED=false
    for pattern in $PROMPT_PATTERNS; do
        if echo "$CHANGED" | grep -q "^$pattern"; then
            PROMPT_CHANGED=true
            break
        fi
    done

    if ! $PROMPT_CHANGED; then
        log "No prompt-affecting files changed since $LAST_COMMIT. Skipping."
        write_state
        exit 0
    fi
fi

log "Running full-mode evals (model=$MODEL, commit=${HEAD_COMMIT:0:8})..."

# Claim the commit BEFORE spending the quota. If the run dies (OOM, killed
# mid-suite, claude unavailable), the next night must not silently spend it
# again on the same unchanged tree.
write_state

# The remote model writes the briefings while remote_llm `briefings` is
# `remote`: the briefing's staleness cases run on it too
# (remote_briefing_eval.py, a dry run). Before the Claude run, whose exits
# below would skip it. 1 is a failed check and alerts; 2 is a model that did
# not answer, which is logged and does not alert.
BRIEFINGS=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/preprocessor')
import remote_llm
print(remote_llm.config().get('briefings') or 'claude')
" 2>>"$LOG_FILE" || echo "claude")
if [[ "$BRIEFINGS" == "remote" ]]; then
    log "Running the remote briefing evals..."
    set +e
    REMOTE_OUT=$(python3 "$SCRIPT_DIR/remote_briefing_eval.py" 2>>"$LOG_FILE")
    REMOTE_RC=$?
    set -e
    printf '%s\n' "$REMOTE_OUT" | tee -a "$LOG_FILE"
    if [[ "$REMOTE_RC" == "1" ]]; then
        alert ":warning: *Remote briefing evals failed* (commit ${HEAD_COMMIT:0:8}).
\`\`\`
$(printf '%s' "$REMOTE_OUT" | head -15)
\`\`\`
Log: \`$LOG_FILE\`"
    elif [[ "$REMOTE_RC" != "0" ]]; then
        log "Remote briefing evals not measured (exit $REMOTE_RC): the remote model did not answer."
    fi
fi

# Run full evals. `set +e` is load-bearing: run_evals.py exits 1 whenever any
# fixture fails, pipefail propagates that through `| tee`, and errexit would
# abort the script right here — which is exactly how every failure since
# 2026-08-02 went unreported.
#
# stderr goes straight to the log rather than through `2>&1`, so RESULT holds
# only the JSON document. Merged stderr would make json.load fail on any
# provider warning and turn every such run into a spurious harness alert.
set +e
RESULT=$(python3 "$SCRIPT_DIR/run_evals.py" --mode full --model "$MODEL" --json \
    2>>"$LOG_FILE" | tee -a "$LOG_FILE")
EVAL_RC=$?
set -e

# Parse results. Distinguish "the suite ran and some fixtures failed" from
# "the suite produced no parseable JSON", the way .githooks/post-commit does —
# they need different responses (fix a regression vs. fix the harness).
SUMMARY=$(echo "$RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['passed'], d['failed'], d.get('errors', 0), d['total'])
" 2>/dev/null || echo "")

if [[ -z "$SUMMARY" ]]; then
    log "❌ EVAL HARNESS FAILURE: run_evals.py exited $EVAL_RC with no parseable JSON."
    log "   Full output is in $LOG_FILE."
    unclaim_state
    alert ":rotating_light: *Full-mode evals could not run* (commit ${HEAD_COMMIT:0:8}).
run_evals.py exited $EVAL_RC and produced no JSON. Log: \`$LOG_FILE\`"
    exit 1
fi

read -r PASSED FAILED ERRORS TOTAL <<< "$SUMMARY"

ERROR_LINES=$(echo "$RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for r in d['results']:
    if r.get('error'):
        print(f\"  🚨 [{r['fixture']['category']}] {r['fixture']['name']}: {r['error']}\")
" 2>/dev/null || echo "")

# Most of the suite errored: it never reached the model. This is the shape of
# the 2026-08-27 02:00 run, which finished in 50 seconds because `claude -p`
# answered every fixture with "You've hit your weekly limit · resets 6am" and
# the old harness scored that string as 19 fixture failures. A quota block also
# arrives mid-suite (a 07:00 validation run this same day errored on 11 of 19
# with "You've hit your session limit · resets 11am"), so the threshold is a
# majority rather than all: either way the commit was not evaluated, so it
# takes the harness path and goes back on the queue for tomorrow. An isolated
# error keeps the claim, so one flaky fixture does not re-spend the suite.
if [[ "$TOTAL" != "0" ]] && (( ERRORS * 2 >= TOTAL )); then
    log "❌ EVAL HARNESS FAILURE: $ERRORS/$TOTAL fixtures errored before reaching the model."
    printf '%s\n' "$ERROR_LINES" | head -5 | tee -a "$LOG_FILE"
    unclaim_state
    alert ":rotating_light: *Full-mode evals could not run* (commit ${HEAD_COMMIT:0:8}, model $MODEL).
$ERRORS of $TOTAL fixtures errored before reaching the model — quota, auth or CLI failure, not a regression.
\`\`\`
$(printf '%s' "$ERROR_LINES" | head -3)
\`\`\`
Log: \`$LOG_FILE\`"
    exit 2
fi

if [[ "$FAILED" == "0" && "$ERRORS" == "0" ]]; then
    log "✅ All $TOTAL full-mode evals pass."
    log "Done. Log: $LOG_FILE"
    exit 0
fi

FAIL_LINES=$(echo "$RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for r in d['results']:
    if r['status'] not in ('PASS', 'ERROR'):
        print(f\"  ❌ [{r['fixture']['category']}] {r['fixture']['name']}: {r['status']}\")
" 2>/dev/null || echo "")

if [[ "$FAILED" != "0" ]]; then
    log "⚠ EVAL FAILURES: $FAILED/$TOTAL failed!"
    [[ -n "$FAIL_LINES" ]] && printf '%s\n' "$FAIL_LINES" | tee -a "$LOG_FILE"
fi
if [[ "$ERRORS" != "0" ]]; then
    log "🚨 $ERRORS/$TOTAL fixtures could not be measured (harness errors)."
    [[ -n "$ERROR_LINES" ]] && printf '%s\n' "$ERROR_LINES" | tee -a "$LOG_FILE"
fi

alert ":warning: *Full-mode evals: $FAILED failed, $ERRORS could not run, of $TOTAL* (commit ${HEAD_COMMIT:0:8}, model $MODEL).
\`\`\`
$(printf '%s\n%s' "$FAIL_LINES" "$ERROR_LINES" | head -20)
\`\`\`
Log: \`$LOG_FILE\`"

log "Done. Log: $LOG_FILE"
# Non-zero so anyone running this by hand, or wiring it into CI or a
# pre-push hook, sees the failure. The cron line redirects stdout+stderr to
# a file, so this exit code reaches no human on its own — the Slack alert
# above is the channel that does.
exit 1
