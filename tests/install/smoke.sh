#!/usr/bin/env bash
# tests/install/smoke.sh: the install, end to end, with no accounts.
#
# Runs the fresh-install path a new user follows (docs/SETUP.md, the /setup
# skill) into THIS user's home, with nothing read from the terminal:
#
#   1. the config, from an answers file (scripts/install/user_init.py --answers, one
#      --phase at a time, the way /setup section 2 does it);
#   2. ./scripts/install/setup.sh --profile <profile>, non-interactive (templates, the
#      profile, the cron template; the sign-ins are skipped);
#   3. the service-account installer with sudo (scripts/install/setup-ea-user.sh, or
#      scripts/install/setup-ea-user-macos.sh on macOS): the ea-bot user, the deployed
#      tree, ~/.ea moved into ea-bot's home, the sudoers rule;
#   4. the install overlay, rendered as ea-bot and installed as the owner
#      (docs/SETUP.md "The install overlay");
#   5. scripts/ea-doctor.py for the profile, with --expect-missing for the
#      checks no account can satisfy. The exit status is the doctor's.
#
# It needs a machine it may install into: passwordless sudo, no ~/.ea
# yet, the network (pip, the Claude Code installer for ea-bot). That is a
# GitHub runner (.github/workflows/ci.yml, ubuntu and macOS) or a
# throwaway container (tests/install/docker-smoke.sh). Never run it on a
# machine with a live install: it refuses when ~/.ea exists.
#
# Settings (environment):
#   EA_SMOKE_PROFILE         email (default), review-only or assistant
#   EA_SMOKE_ANSWERS         the answers file (default: answers-email.json)
#   EA_SMOKE_EXPECT_MISSING  the doctor's --expect-missing value
#   EA_SMOKE_PYTHON          the python3 to run the owner-side steps with
#
# docs/install-testing.md describes the three tiers and the names.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILE="${EA_SMOKE_PROFILE:-email}"
ANSWERS="${EA_SMOKE_ANSWERS:-$REPO/tests/install/answers-email.json}"
# The three misses an install with no accounts has: ea-bot's Claude login,
# the Graph token, the EA-Managed calendar (a WARN on the email and
# review-only profiles, where naming it is harmless).
EXPECT="${EA_SMOKE_EXPECT_MISSING:-ea-bot: Claude login,auth: msal_token_cache.json,EA-Managed calendar}"
ME="$(id -un)"
OS="$(uname -s)"

log() { printf '\n=== smoke: %s ===\n' "$*"; }
die() { echo "smoke: $*" >&2; exit 2; }

[ "$(id -u)" != 0 ] || die "run as the user who will own the install, not as root"
[ ! -e "$HOME/.ea" ] || die "$HOME/.ea exists: the smoke installs into a home with no EA"
[ -f "$ANSWERS" ] || die "answers file $ANSWERS is missing"
sudo -n true 2>/dev/null || die "passwordless sudo is required (the service-account installer)"

# The owner-side python3. On macOS Apple's /usr/bin/python3 is 3.9, below
# the floor, so the Homebrew interpreter is used, and it goes first on
# PATH so scripts/install/setup.sh's own `python3` is the same one. ea-bot runs the
# overlay render with the interpreter the installers name on each OS.
case "$OS" in
    Darwin)
        PY="${EA_SMOKE_PYTHON:-}"
        if [ -z "$PY" ]; then
            for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
                if [ -x "$candidate" ]; then PY="$candidate"; break; fi
            done
        fi
        PY="${PY:-$(command -v python3)}"
        RENDER_PY="$PY"
        EA_BOT_HOME=/Users/ea-bot
        USER_SCRIPT=scripts/install/setup-ea-user-macos.sh
        ;;
    Linux)
        PY="${EA_SMOKE_PYTHON:-$(command -v python3)}"
        RENDER_PY=/usr/bin/python3
        EA_BOT_HOME=/home/ea-bot
        USER_SCRIPT=scripts/install/setup-ea-user.sh
        ;;
    *)
        die "unsupported OS: $OS (Linux and macOS only; there is no Windows install)"
        ;;
esac
export PATH="$(dirname "$PY"):$PATH"
log "profile $PROFILE, answers $ANSWERS, python $("$PY" --version 2>&1) at $PY, $OS"

# 1. The config, one phase at a time. tone (reads sent mail) and summary
# are not config phases; auth writes the Graph keys and skips the sign-in.
log "1/5 config from the answers file"
PHASE_LOG="$(mktemp)"
for phase in identity scheduling inbox autonomy groups deadlines style auth reimbursement vip push state; do
    if "$PY" "$REPO/scripts/install/user_init.py" --answers "$ANSWERS" --phase "$phase" < /dev/null > "$PHASE_LOG" 2>&1; then
        echo "  phase $phase: ok"
    else
        cat "$PHASE_LOG"
        die "scripts/install/user_init.py --phase $phase failed"
    fi
done
rm -f "$PHASE_LOG"

# 2. scripts/install/setup.sh: the templates, the profile, the cron template, no sign-in.
# Through bash, as the service-account script runs: a checkout that came
# in as a tar or a zip may carry no executable bit.
log "2/5 ./scripts/install/setup.sh --profile $PROFILE --non-interactive"
(cd "$REPO" && EA_NONINTERACTIVE=1 bash ./scripts/install/setup.sh --profile "$PROFILE" < /dev/null)

# 3. The service account. The script wants `sudo bash <script>` from the
# owner's shell: SUDO_USER names the owner, and git reads the owner's
# checkout as root because SUDO_UID owns it.
log "3/5 sudo bash $USER_SCRIPT"
(cd "$REPO" && sudo -n bash "./$USER_SCRIPT" < /dev/null)

# 4. The install overlay. The render runs as ea-bot from its deployed
# tree (docs/SETUP.md "The install overlay"). The install, and every later step, run through
# `sudo -u <me>` so the process carries the ea group the installer just
# added: this shell's group list predates it.
log "4/5 install overlay: render as ea-bot, install as $ME"
sudo -n -u ea-bot "$RENDER_PY" "$EA_BOT_HOME/ea-system/preprocessor/install_overlay.py" render
sudo -n -u "$ME" -H "$PY" "$REPO/preprocessor/install_overlay.py" install --tree "$EA_BOT_HOME/ea-system"

# 5. The doctor. Its exit status is the result.
log "5/5 scripts/ea-doctor.py --profile $PROFILE --expect-missing \"$EXPECT\""
rc=0
sudo -n -u "$ME" -H "$PY" "$REPO/scripts/ea-doctor.py" --profile "$PROFILE" --expect-missing "$EXPECT" || rc=$?
log "doctor exit $rc"
exit "$rc"
