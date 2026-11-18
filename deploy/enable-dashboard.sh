#!/usr/bin/env bash
#
# enable-dashboard.sh: the project dashboard (preprocessor/project_dashboard.py)
# as a systemd USER service of the owner. Run as the owner, no root:
#
#     bash deploy/enable-dashboard.sh            # install + start
#     bash deploy/enable-dashboard.sh --status   # is it up, and where
#     bash deploy/enable-dashboard.sh --rollback # stop + remove
#
# The dashboard is the optional drag-and-drop board over the project store
# (docs/SETUP.md 'Project dashboard'). It runs as the owner and writes the
# store in ea-bot's tree through the ea group, so the process must hold that
# group. A user unit gets the groups its user manager started with: the
# script checks the running process and says what to do when the group is
# missing (the manager started before the install added you to ea).
#
# It binds EA_DASH_BIND, else the Tailscale IP, else 127.0.0.1, on
# EA_DASH_PORT (8377). A non-loopback address needs EA_DASH_TOKEN
# (scripts/set-secret.sh EA_DASH_TOKEN); without it the server serves
# 127.0.0.1 only and the doctor says so. EA_DASH_BIND and EA_DASH_PORT, when
# set as this script runs, go into the unit.
#
# Runs from the deployed copy (/opt/ea-system when present, else this tree),
# with the interpreter the chat bot's unit uses when there is one. The
# post-commit hook restarts it after a deploy. The owner's user manager must
# linger (`loginctl enable-linger <owner>`) for the service to survive
# logout; the script says so when it does not.
set -uo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY=/opt/ea-system; [ -d "$DEPLOY/preprocessor" ] || DEPLOY="$SRC"
EA_BOT_HOME="$(getent passwd ea-bot | cut -d: -f6)"; EA_BOT_HOME="${EA_BOT_HOME:-/home/ea-bot}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ea-dashboard.service"
PORT="${EA_DASH_PORT:-8377}"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }

[ "$(id -u)" != 0 ] || fail "run as the owner, not root"
[ "$(id -un)" != ea-bot ] || fail "run as the owner, not ea-bot"
command -v systemctl >/dev/null || fail "no systemd here (macOS and Docker have no dashboard service yet)"

# The interpreter: the one the bot's unit runs, else the repo venv, else the
# python3 install.sh chose, else the python3 on PATH (as harden-permissions.sh).
pick_python(){
  local py
  py="$(systemctl show -p ExecStart --value ea-chat-bot 2>/dev/null | grep -oE '/[^ ]*/python3?' | head -1)"
  for py in "$py" "$SRC/.venv/bin/python3" "$HOME/.local/share/ea-system/bin/python3" \
            "$(command -v python3 || true)"; do
    if [ -n "$py" ] && [ -x "$py" ]; then echo "$py"; return; fi
  done
}

# Whether the running server lacks the ea gid: true only when its groups
# were read and the gid is not among them.
lacks_ea_group(){
  local pid gid groups
  pid="$(systemctl --user show -p MainPID --value ea-dashboard.service 2>/dev/null)"
  gid="$(getent group ea | cut -d: -f3)"
  [ -n "$pid" ] && [ "$pid" != 0 ] && [ -n "$gid" ] || return 1
  groups="$(grep -E '^Groups:' "/proc/$pid/status" 2>/dev/null)" || return 1
  ! printf '%s\n' "$groups" | tr -s ' \t' '\n' | grep -qx "$gid"
}

status(){
  systemctl --user --no-pager status ea-dashboard.service 2>&1 | head -5
  journalctl --user -u ea-dashboard.service -n 20 --no-pager 2>/dev/null \
    | grep -E 'project dashboard: (http|WARNING)' | tail -2
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --rollback)
    systemctl --user disable --now ea-dashboard.service 2>/dev/null
    rm -f "$UNIT"; systemctl --user daemon-reload
    grn "removed ea-dashboard.service"; exit 0 ;;
  "") ;;
  *) fail "unknown option $1" ;;
esac

PYBIN="$(pick_python)"
[ -n "$PYBIN" ] || fail "no python3 found"
[ -f "$DEPLOY/preprocessor/project_dashboard.py" ] || fail "$DEPLOY/preprocessor/project_dashboard.py is missing"
[ -d "$EA_BOT_HOME/.ea/state" ] || fail "$EA_BOT_HOME/.ea/state is missing: run the install (docs/SETUP.md) first"
id -nG | tr ' ' '\n' | grep -qx ea \
  || getent group ea | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)" \
  || fail "$(id -un) is not in the ea group: run  sudo bash $SRC/deploy/harden-permissions.sh  first"

EXTRA_ENV=""
[ -n "${EA_DASH_BIND:-}" ] && EXTRA_ENV+="Environment=EA_DASH_BIND=$EA_DASH_BIND"$'\n'
[ -n "${EA_DASH_PORT:-}" ] && EXTRA_ENV+="Environment=EA_DASH_PORT=$EA_DASH_PORT"$'\n'

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<UNIT
# Written by deploy/enable-dashboard.sh; run it again to rewrite this file.
[Unit]
Description=EA project dashboard (drag-and-drop board over the project store)
After=network-online.target tailscaled.service

[Service]
Type=simple
WorkingDirectory=$DEPLOY
Environment=HOME=$HOME
Environment=EA_DIR=$EA_BOT_HOME/.ea
${EXTRA_ENV}ExecStart=$PYBIN $DEPLOY/preprocessor/project_dashboard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable ea-dashboard.service >/dev/null 2>&1
systemctl --user restart ea-dashboard.service || fail "the service did not start (journalctl --user -u ea-dashboard)"
sleep 2
systemctl --user is-active --quiet ea-dashboard.service \
  || { status; fail "the service stopped after it started (journalctl --user -u ea-dashboard)"; }
status
grn "installed $UNIT"

if lacks_ea_group; then
  ylw "the server runs without the ea group, so it cannot write the project store."
  ylw "your user manager started before the install added you to ea. Restart it, then run this script again:"
  ylw "  sudo systemctl restart user@$(id -u).service    (or reboot)"
fi
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
  ylw "your user manager does not linger: run  sudo loginctl enable-linger $(id -un)  or the board stops at logout"
fi
if ! "$PYBIN" -c "import sys; sys.path.insert(0, sys.argv[1]); import owner_trust; \
sys.exit(0 if owner_trust.owner_secret('EA_DASH_TOKEN', env={}).strip() else 1)" "$DEPLOY/preprocessor" 2>/dev/null; then
  case "${EA_DASH_BIND:-}" in
    127.0.0.1|localhost|::1) ;;
    *) ylw "no EA_DASH_TOKEN: the board serves 127.0.0.1:$PORT only. For your tailnet address:"
       ylw "  $SRC/scripts/set-secret.sh EA_DASH_TOKEN    (a long random value), then  systemctl --user restart ea-dashboard" ;;
  esac
else
  grn "open the board once as  http://<address>:$PORT/?t=<your EA_DASH_TOKEN>"
fi
