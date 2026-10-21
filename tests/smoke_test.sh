#!/usr/bin/env bash
#
# Smoke test: verify a working EA install
# Run after setup to catch common configuration problems.
#
# Usage:
#   ./tests/smoke_test.sh          # local check
#   ./tests/smoke_test.sh --ci     # CI mode (skip live API checks)
#
set -euo pipefail

EA_DIR="${EA_DIR:-$HOME/.ea}"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0
SKIP=0
CI_MODE=false
[[ "${1:-}" == "--ci" ]] && CI_MODE=true

green() { echo -e "\033[32m  PASS\033[0m $1"; PASS=$((PASS+1)); }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; FAIL=$((FAIL+1)); }
skip()  { echo -e "\033[33m  SKIP\033[0m $1"; SKIP=$((SKIP+1)); }

echo "=== EA Smoke Tests ==="
echo ""

# ---- Python imports ----
echo "Python imports:"
for mod in msal requests google.oauth2.credentials googleapiclient.discovery slack_bolt; do
    if python3 -c "import $mod" 2>/dev/null; then
        green "$mod"
    else
        red "$mod (pip install -r requirements.txt)"
    fi
done

echo ""
echo "Internal modules:"
for mod in graph_client google_calendar_client preprocess slack_notifier pdf_utils; do
    if python3 -c "import sys; sys.path.insert(0,'$SCRIPT_DIR/preprocessor'); import $mod" 2>/dev/null; then
        green "preprocessor.$mod"
    else
        red "preprocessor.$mod"
    fi
done

# ---- Config files ----
echo ""
echo "Config files:"
for f in user-profile.json scheduling-preferences.json autonomy-tiers.json; do
    path="$EA_DIR/config/$f"
    if [[ -f "$path" ]]; then
        if python3 -c "import json; json.load(open('$path'))" 2>/dev/null; then
            green "$f (valid JSON)"
        else
            red "$f (invalid JSON)"
        fi
    else
        red "$f (missing: $path)"
    fi
done

# ---- User profile not placeholder ----
if [[ -f "$EA_DIR/config/user-profile.json" ]]; then
    NAME=$(python3 -c "import json; print(json.load(open('$EA_DIR/config/user-profile.json')).get('name',''))")
    if [[ "$NAME" != "Your Name" && -n "$NAME" ]]; then
        green "user-profile.json has real name: $NAME"
    else
        red "user-profile.json still has placeholder name"
    fi
fi

# ---- Env file ----
echo ""
echo "Environment:"
if [[ -f "$EA_DIR/.env" ]]; then
    green ".env file exists"
    perms=$(stat -c '%a' "$EA_DIR/.env" 2>/dev/null || stat -f '%Lp' "$EA_DIR/.env" 2>/dev/null)
    if [[ "$perms" == "600" || "$perms" == "640" ]]; then
        green ".env permissions ($perms)"
    else
        red ".env permissions ($perms, should be 600 or 640)"
    fi
else
    red ".env file missing ($EA_DIR/.env)"
fi

for var in SLACK_BOT_TOKEN SLACK_APP_TOKEN EA_SLACK_CHANNEL_ID EA_SLACK_CHANNEL_NAME; do
    if [[ -f "$EA_DIR/.env" ]] && grep -q "^$var=" "$EA_DIR/.env" 2>/dev/null; then
        val=$(grep "^$var=" "$EA_DIR/.env" | cut -d= -f2)
        if [[ -n "$val" && "$val" != *"your-"* && "$val" != *"YOUR"* ]]; then
            green "$var is set"
        else
            red "$var is placeholder"
        fi
    else
        red "$var not in .env"
    fi
done

# ---- State files ----
echo ""
echo "State:"
for f in obligations-ledger.json; do
    if [[ -f "$EA_DIR/state/$f" ]]; then
        green "$f exists"
    else
        red "$f missing"
    fi
done

# ---- Live API checks (skip in CI) ----
echo ""
echo "API connectivity:"
if $CI_MODE; then
    skip "Graph API (CI mode)"
    skip "Google Calendar (CI mode)"
    skip "Slack (CI mode)"
else
    # Graph API
    if python3 -c "
import sys; sys.path.insert(0,'$SCRIPT_DIR/preprocessor')
import graph_client as g
accounts = g._app.get_accounts()
if accounts:
    r = g._app.acquire_token_silent(g.SCOPES, account=accounts[0])
    if r and 'access_token' in r: sys.exit(0)
if not g._source_token():
    sys.exit(1)
" 2>/dev/null; then
        green "Graph API token valid"
    else
        red "Graph API token expired or missing"
    fi

    # Google Calendar
    if python3 "$SCRIPT_DIR/preprocessor/google_calendar_client.py" --test 2>/dev/null | grep -q "OK\|events"; then
        green "Google Calendar connected"
    else
        skip "Google Calendar (no token or test failed)"
    fi

    # Slack
    if [[ -f "$EA_DIR/.env" ]]; then
        # Settings and secrets as data (scripts/owner-io.sh env), never sourced.
        while IFS= read -r -d '' _env_key && IFS= read -r -d '' _env_val; do
            [[ "$_env_key" =~ ^[A-Z][A-Z0-9_]*$ ]] && export "$_env_key=$_env_val"
        done < <("$SCRIPT_DIR/scripts/owner-io.sh" env "$EA_DIR" "${EA_PRIVATE_DIR:-$HOME/.ea-private}")
        if python3 -c "
import os
from slack_sdk import WebClient
c = WebClient(token=os.environ.get('SLACK_BOT_TOKEN', ''))
r = c.auth_test()
assert r['ok']
" 2>/dev/null; then
            green "Slack bot token valid"
        else
            red "Slack bot token invalid"
        fi
    else
        skip "Slack (no .env)"
    fi
fi

# ---- Systemd service ----
echo ""
echo "Services:"
# The bot is the system unit deploy/harden-permissions.sh writes.
if systemctl is-active ea-chat-bot >/dev/null 2>&1; then
    green "ea-chat-bot systemd service running"
elif systemctl is-enabled ea-chat-bot >/dev/null 2>&1; then
    red "ea-chat-bot enabled but not running"
else
    skip "ea-chat-bot not installed"
fi
if systemctl --user cat ea-chat-bot >/dev/null 2>&1; then
    red "a user unit ea-chat-bot exists beside the system one: retire it (docs/SETUP.md 'Services')"
fi

# ---- Cron ----
if crontab -l 2>/dev/null | grep -q "run-agent.sh"; then
    green "cron jobs installed"
else
    skip "cron jobs not installed"
fi

# ---- Summary ----
echo ""
echo "========================"
TOTAL=$((PASS+FAIL+SKIP))
echo "$PASS passed, $FAIL failed, $SKIP skipped (of $TOTAL)"
[[ $FAIL -eq 0 ]] && echo "All checks passed!" || echo "Fix the failures above."
exit $FAIL
