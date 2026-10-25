#!/usr/bin/env bash
#
# A year-long Claude login for the ea-bot user.
#
# ea-bot's browser login (`/login`) keeps a short-lived access token that
# Claude Code renews every few hours with a refresh token. When that
# renewal is refused, the credentials are wiped and every chat turn and
# cron lane answers "Not logged in" until someone logs in again
# (docs/SETUP.md "Common failures"). `claude setup-token` makes a token
# that lasts about a year instead. Stored as env.CLAUDE_CODE_OAUTH_TOKEN
# in ea-bot's Claude settings (mode 600), it is read at every start by
# the bot, the cron lanes and scripts/ea-bot-shell.sh alike, with no
# renewal step in between. docs/macos-claude-binary.md does the same by
# hand on macOS; deploy/docker/entrypoint.sh does it in the image.
#
# Interactive, in a terminal, as the owner:
#   1. `claude setup-token` runs as ea-bot and prints a link; open it,
#      approve with the EA's Claude account, paste the code back. It then
#      prints the token.
#   2. Paste the token here (not echoed). It is written as ea-bot, so no
#      owner process writes into ea-bot's space by name.
#
# Renew about a year later: run this again and the value is replaced.
# `scripts/ea-doctor.py` reports the login; the sentinel's `ea_bot_login`
# check alerts when it lapses.
set -euo pipefail

EA_BOT_USER="${EA_BOT_USER:-ea-bot}"
EA_BOT_HOME="$(getent passwd "$EA_BOT_USER" 2>/dev/null | cut -d: -f6)"
if [[ -z "$EA_BOT_HOME" ]] && command -v dscl >/dev/null 2>&1; then
    EA_BOT_HOME="$(dscl . -read "/Users/$EA_BOT_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
fi
[[ -n "$EA_BOT_HOME" ]] || { echo "ea-bot-token: no user $EA_BOT_USER on this host (scripts/install/setup-ea-user.sh creates it)" >&2; exit 2; }

if [[ ! -t 0 ]]; then
    echo "ea-bot-token: needs a terminal (the sign-in and the token paste both read it)" >&2
    exit 2
fi

# The Claude binary ea-bot runs, in the bot's order (ea-chat-bot.py,
# _detect_claude_bin): its own install, then the system-wide ones.
CLAUDE=""
for c in "$EA_BOT_HOME/.local/bin/claude" /usr/local/bin/claude /usr/bin/claude; do
    if (cd / && sudo -n -u "$EA_BOT_USER" test -x "$c") 2>/dev/null; then CLAUDE="$c"; break; fi
done
[[ -n "$CLAUDE" ]] || { echo "ea-bot-token: no claude $EA_BOT_USER can run (docs/SETUP.md 'The service account')" >&2; exit 2; }

SETTINGS="$EA_BOT_HOME/.claude/settings.json"

echo "1/2  claude setup-token as $EA_BOT_USER. Open the link it prints, approve with the EA's"
echo "     Claude account, paste the code back. It then prints the token."
echo ""
(cd / && sudo -u "$EA_BOT_USER" env HOME="$EA_BOT_HOME" "$CLAUDE" setup-token) || {
    echo "ea-bot-token: claude setup-token did not finish; nothing was stored" >&2
    exit 1
}
echo ""
printf '2/2  Paste the token (not echoed), then Enter: '
read -rs TOKEN
echo ""
TOKEN="${TOKEN//[[:space:]]/}"
if [[ -z "$TOKEN" ]]; then
    echo "ea-bot-token: no token pasted; nothing was stored" >&2
    exit 1
fi

# The write runs as ea-bot: the settings file is ea-bot's, and an owner
# write into ea-bot's space by name would follow a planted link.
printf '%s' "$TOKEN" | (cd / && sudo -u "$EA_BOT_USER" env HOME="$EA_BOT_HOME" python3 -c '
import json, os, sys
path = sys.argv[1]
token = sys.stdin.read().strip()
try:
    d = json.load(open(path))
except (OSError, ValueError):
    d = {}
if not isinstance(d, dict):
    d = {}
d.setdefault("env", {})["CLAUDE_CODE_OAUTH_TOKEN"] = token
os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(d, indent=2) + "\n")
os.replace(path + ".tmp", path)
os.chmod(path, 0o600)
print("stored: env.CLAUDE_CODE_OAUTH_TOKEN in " + path + " (mode 600)")
' "$SETTINGS")
unset TOKEN

echo ""
echo "Claude Code reads the token at every start. The login as the CLI sees it now:"
(cd / && sudo -u "$EA_BOT_USER" env HOME="$EA_BOT_HOME" "$CLAUDE" auth status 2>/dev/null) | grep -E '"(loggedIn|authMethod)"' || true
echo "Renew in about a year: run this script again. The doctor (scripts/ea-doctor.py) reports the login."
