#!/usr/bin/env bash
# enable-mail-mediation.sh — turn on mail mediation.
#
# Runs the privileged-user-side mediator service, sets mail_mediation.enabled=true,
# and moves the Graph/Google tokens out of the resident (ea-bot) user's reach. After
# this, ea-bot can no longer read the raw tokens or send mail: SAFE ops (read / draft
# / search / reversible inbox) are forwarded to the mediator, GATED ops (send /
# calendar mutate) are refused. See docs/SECURITY.md and docs/mail-mediation-spec.md.
#
# This is NOT a "phase" you bolt on afterward. It is the mediation install step:
#   * scripts/install/setup-ea-user.sh calls it during a fresh hardened install, and
#   * you can run it standalone to migrate a pre-mediation install, or for ops.
# Idempotent and reversible.
#
#     sudo bash deploy/enable-mail-mediation.sh            # enable / re-arm
#     sudo bash deploy/enable-mail-mediation.sh --status   # show state
#     sudo bash deploy/enable-mail-mediation.sh --rollback # undo: mediation off, tokens stay the owner's
#
# The token files are the owner's alone at every step and on every exit: they
# are locked first (deploy/tree-custody.sh: the auth directory to the owner,
# group ea, 0750, then each credential owner-only), and no failure, check or
# rollback gives one back to ea-bot. Nothing is adopted either: an auth
# directory another user owns stops the run with the recovery steps, and an
# entry the owner does not own is quarantined, never chowned, so a file
# ea-bot made can never become an owner trust anchor. A broken forward path
# therefore fails closed: the resident has no mail until the mediator works. On
# a fresh install with no tokens yet, it arms mediation (service + flag) and
# returns; tokens minted later are already owned by the privileged user.
set -uo pipefail

# ── resolve users/paths portably (the privileged bot user is whoever ran sudo) ──
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(eval echo ~"$REAL_USER")"
SRC="$(cd "$(dirname "$0")/.." && pwd)"                 # repo root (this file is deploy/*)
EA_BOT_HOME="$(eval echo ~ea-bot 2>/dev/null || echo /home/ea-bot)"
DEPLOY="$EA_BOT_HOME/ea-system"
EA_DIR="$EA_BOT_HOME/.ea"
AUTH="$EA_DIR/auth"
CONFIG="$EA_DIR/config/ea-config.json"
QUEUE="$EA_DIR/state/mail-queue"
UNIT_DST="/etc/systemd/system/ea-mail-mediator.service"
# The auth directory's physical path, from ea-bot's home (whose parent ea-bot
# cannot change); every write in auth/ happens inside it, entered and checked.
AUTH_PHYS="$(cd -P -- "$EA_BOT_HOME" 2>/dev/null && pwd -P)/.ea/auth"
PERMREC_NAME=.pre-mediation-perms     # what the tokens were before; a record only
MEDIATOR_PY="$DEPLOY/preprocessor/mail_mediator.py"
TOKEN_PROBE="$AUTH/msal_token_cache.json"              # representative token for read checks
# An install whose Graph token comes from an owner plugin's auth source has
# no MSAL cache; another token file in auth/ stands in for it.
if [ ! -e "$TOKEN_PROBE" ]; then
    for _t in "$AUTH"/*token*; do
        if [ -f "$_t" ]; then TOKEN_PROBE="$_t"; break; fi
    done
fi
PYBIN="$SRC/.venv/bin/python3"; [ -x "$PYBIN" ] || PYBIN=/usr/bin/python3
# The unit's sandbox, shared with deploy/harden-permissions.sh.
. "$SRC/deploy/mediator-sandbox.sh"
# tc_enter and the auth lock, shared with the setup scripts.
. "$SRC/deploy/tree-custody.sh"
# The owner's primary group: macOS has no per-user group named after the login.
REAL_GROUP="$(id -gn "$REAL_USER")"
# The owner's uid: the auth lock adopts nothing this uid does not own.
REAL_UID="$(id -u "$REAL_USER")"
# Service control differs by platform; everything else is shared. On macOS the
# mediator is a LaunchDaemon installed by deploy/macos/install-services.sh.
DARWIN=0; [ "$(uname -s)" = Darwin ] && DARWIN=1
# The mediator's launchd label: the neutral prefix on a new install, the
# legacy one on an install deployed under it (preprocessor/launchd_labels.py).
MED_LABEL=org.ea-system.ea-mail-mediator
for _n in ea-chat-bot ea-mail-mediator; do
    if [ -f "/Library/LaunchDaemons/edu.colorado.$_n.plist" ]; then MED_LABEL=edu.colorado.ea-mail-mediator; fi  # legacy-launchd-label
done
MED_LOG="$REAL_HOME/.ea-private/logs/$MED_LABEL.launchd.log"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }
need_root(){ [ "$(id -u)" = 0 ] || fail "run as root:  sudo bash $0 ${1:-}"; }

# token files that must become privileged-user-only (skip any that don't exist)
# (every file with "token" in its name: the MSAL cache, the Google tokens and
# whatever an owner plugin's harvest writes). Names relative to auth/; a link
# is never one.
tokens_here(){
  local f
  for f in ./*token* ./google_client_secret.json; do
    if [ -f "$f" ] && [ ! -L "$f" ]; then echo "$f"; fi
  done
}
have_tokens(){ [ -n "$( (tc_enter "$AUTH_PHYS" && tokens_here) 2>/dev/null)" ]; }

# run a command as ea-bot with the same env the live resident gets
ea_run(){ cd / && sudo -u ea-bot env HOME="$EA_BOT_HOME" EA_DIR="$EA_DIR" \
            PATH="$DEPLOY/.venv/bin:/usr/local/bin:/usr/bin:/bin" bash -c "cd '$DEPLOY' && $*"; }

# GNU timeout where it exists; macOS ships none, so perl's alarm stands in.
if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout"
else TIMEOUT="perl -e 'alarm shift; exec @ARGV'"; fi

# stat in the "%n %U %G %a" shape of the pre-mediation perm record.
perm_line(){ if [ "$DARWIN" = 1 ]; then stat -f '%N %Su %Sg %Lp' "$1"; else stat -c '%n %U %G %a' "$1"; fi; }

svc_state(){   # active | inactive | not installed
  if [ "$DARWIN" = 1 ]; then
    [ -f "/Library/LaunchDaemons/$MED_LABEL.plist" ] || { echo "not installed"; return; }
    launchctl print "system/$MED_LABEL" 2>/dev/null | grep -q '^[[:space:]]*pid = ' && echo active || echo inactive
  else
    systemctl is-active ea-mail-mediator 2>/dev/null || echo "not installed"
  fi
}
svc_logs(){ if [ "$DARWIN" = 1 ]; then tail -20 "$MED_LOG" 2>/dev/null; else journalctl -u ea-mail-mediator -n 20 --no-pager; fi; }
svc_stop(){ if [ "$DARWIN" = 1 ]; then launchctl bootout "system/$MED_LABEL" 2>/dev/null || true; else systemctl stop ea-mail-mediator 2>/dev/null || true; fi; }

install_unit(){  # generate the systemd unit for THIS user/paths (no hardcoded user)
  mediator_sandbox_dirs "$REAL_USER" "$REAL_HOME"   # write roots exist before the unit starts
  cat > "$UNIT_DST" <<UNIT
[Unit]
Description=EA Mail Mediator (${REAL_USER}-side executor for the resident's SAFE mail ops)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$REAL_USER
SupplementaryGroups=ea
WorkingDirectory=$DEPLOY
Environment=HOME=$REAL_HOME
Environment=EA_DIR=$EA_DIR
# Only the owner-private secrets file: everything under $EA_DIR is ea-bot
# space, so no environment file there is loaded into this owner process.
EnvironmentFile=-$REAL_HOME/.ea-private/secrets.env
ExecStart=$PYBIN $MEDIATOR_PY
Restart=always
RestartSec=5
# Sandbox: deploy/mediator-sandbox.sh says what each line allows and why.
$(mediator_sandbox "$REAL_HOME" "$EA_BOT_HOME")

[Install]
WantedBy=multi-user.target
UNIT
}

set_flag(){  # $1 = true|false ; written as ea-bot so the config stays ea-bot:ea
  sudo -u ea-bot /usr/bin/env python3 - "$CONFIG" "$1" <<'PY'
import json, os, sys
path, val = sys.argv[1], sys.argv[2] == "true"
try: cfg = json.load(open(path))
except Exception: cfg = {}
mm = cfg.get("mail_mediation")
mm = mm if isinstance(mm, dict) else {}
mm["enabled"] = val
cfg["mail_mediation"] = mm
tmp = path + ".tmp"
open(tmp, "w").write(json.dumps(cfg, indent=2))
os.replace(tmp, path)
print("  mail_mediation.enabled =", val)
PY
}

selftest(){  # $1 = tag; verify the forward path via the mediator's audit log (no spurious pass)
  # The mediator's audit is an owner log (log_paths.owner_log, since
  # 2026-09-28): ~/.ea-private/logs of the owner, not $EA_DIR/logs.
  local out before after log="$REAL_HOME/.ea-private/logs/mail-mediator.log"
  before=$(wc -l < "$log" 2>/dev/null || echo 0)
  out=$(ea_run "$TIMEOUT 45 python3 preprocessor/graph_actions.py search-mail --query 'mediation-selftest-$1' --top 1" 2>&1)
  after=$(wc -l < "$log" 2>/dev/null || echo 0)
  if echo "$out" | grep -q "mail mediator did not respond"; then red "  selftest: mediator not draining the queue"; return 1; fi
  if [ "${after:-0}" -le "${before:-0}" ]; then red "  selftest: request did NOT reach the mediator (ran direct?) out=${out:0:120}"; return 1; fi
  grn "  selftest: search-mail forwarded through the mediator (audit +$((after-before)))"
  return 0
}

send_refused(){  # capture first: a correct refusal exits 2, and `set -o pipefail`
                 # would otherwise make `ea_run | grep` return that 2 even on a match
  local out
  out=$(ea_run "python3 preprocessor/graph_actions.py send-draft --message-id selftest" 2>&1)
  printf '%s' "$out" | grep -q BLOCKED
}

# lock_tokens_here: in the checked auth directory, hand the directory to the
# owner (only when the owner or root owns it already; another owner stops
# here with the recovery steps), record the tokens' modes once (only now, so
# the record cannot be a planted link), then make every credential the
# owner's alone and quarantine any entry that is not the owner's. Nothing
# gives a token back to ea-bot, and nothing of ea-bot's becomes the owner's:
# the record is history, not a restore list.
lock_tokens_here(){
  local f
  auth_dir_lock_here "$REAL_USER" "$REAL_UID" ea "$AUTH" || return
  if [ ! -e "$PERMREC_NAME" ] && [ ! -L "$PERMREC_NAME" ] && [ -n "$(tokens_here)" ]; then
    ( umask 077; for f in $(tokens_here); do perm_line "$f"; done > "$PERMREC_NAME" ) \
      && chown "$REAL_USER:$REAL_GROUP" "$PERMREC_NAME"
  fi
  auth_entries_lock_here "$REAL_USER" "$REAL_UID" ea "$REAL_GROUP"
}
lock_tokens(){
  [ -e "$AUTH_PHYS" ] || [ -L "$AUTH_PHYS" ] || return 0
  if ( tc_enter "$AUTH_PHYS" || { red "  $AUTH is not a real directory at $AUTH_PHYS (a link, or moved)"; exit 1; }
       lock_tokens_here ); then
    grn "  token files are ${REAL_USER}-only; the auth directory is ${REAL_USER}:ea 0750"
  else
    red "  could not lock $AUTH (see above)"; return 1
  fi
}

status(){
  echo "== ea-mail-mediator service =="
  echo "  $(svc_state)"
  echo "== mail_mediation flag =="
  # read the value directly -- set_flag writes indented JSON, so a line-based
  # grep for '"mail_mediation"...}' never matches (the object spans lines)
  python3 -c "import json;v=json.load(open('$CONFIG')).get('mail_mediation');print('  enabled =', v.get('enabled') if isinstance(v,dict) else v)" 2>/dev/null || echo "  (unreadable / no config yet)"
  echo "== token ownership =="
  ls -l "$AUTH"/msal_token_cache.json "$AUTH"/google_token_*.json 2>/dev/null | awk '{print "  "$1" "$3":"$4" "$NF}'
  echo "== can ea-bot read the Outlook token? =="
  if sudo -u ea-bot test -r "$TOKEN_PROBE" 2>/dev/null; then red "  YES  (not locked -- mediation not fully applied)"; else grn "  no   (locked -- ea-bot cannot read it)"; fi
}

rollback(){
  need_root --rollback
  ylw "Rolling back mail mediation..."
  set_flag false
  # The ea-bot-owned config flag is advisory (mediation fails closed while the
  # unit exists); the owner-owned kill file is what actually turns it off.
  lock_tokens || fail "the auth directory is not locked; the kill file was not written"
  ( tc_enter "$AUTH_PHYS" && install -o "$REAL_USER" -g "$REAL_GROUP" -m 600 /dev/null ./mediation-disabled ) \
    || fail "could not write the kill file in $AUTH"
  svc_stop
  grn "Rolled back: mediation off, service stopped. The tokens stay ${REAL_USER}-only, so the"
  grn "resident has no mail access until mediation is on again (re-run without --rollback)."
  exit 0
}

enable(){
  need_root
  [ -f "$MEDIATOR_PY" ] || fail "deployed mail_mediator.py missing at $MEDIATOR_PY -- deploy the source first (scripts/install/setup-ea-user.sh Step 3)"

  ylw "[0/3] the auth directory and every token in it to ${REAL_USER} (the old modes recorded once)"
  lock_tokens || fail "the tokens could not be locked; nothing else changed"

  ylw "[1/3] install + (re)start the mediator service (${REAL_USER}-side)"
  if [ "$DARWIN" = 1 ]; then
    [ -f "/Library/LaunchDaemons/$MED_LABEL.plist" ] \
      || fail "no $MED_LABEL LaunchDaemon -- run: sudo bash deploy/macos/install-services.sh"
    launchctl kickstart -k "system/$MED_LABEL"   # restart so a freshly deployed mediator.py is loaded
  else
    install_unit
    systemctl daemon-reload
    systemctl enable ea-mail-mediator >/dev/null 2>&1 || true
    systemctl restart ea-mail-mediator        # restart so a freshly deployed mediator.py is loaded
  fi
  sleep 2
  [ "$(svc_state)" = active ] || { svc_logs; fail "mediator service did not start"; }
  # Made as ea-bot, in ea-bot's tree: a link at the name leads root nowhere.
  ( cd / && sudo -u ea-bot sh -c 'mkdir -p "$1" && chgrp ea "$1" && chmod 2770 "$1"' sh "$QUEUE" ) \
    || ylw "  could not set up $QUEUE as ea-bot (see above)"
  grn "  ea-mail-mediator is active"

  ylw "[2/3] enable the flag"
  set_flag true
  ( tc_enter "$AUTH_PHYS" && rm -f ./mediation-disabled )   # clear any rollback kill file

  if ! have_tokens; then
    ylw "  no tokens in $AUTH yet -- mediation is ARMED (service + flag on)."
    ylw "  tokens minted later are owned by $REAL_USER; re-run this after auth to verify."
    echo; grn "MAIL MEDIATION ENABLED (armed; awaiting first auth)"; echo; status; return 0
  fi

  ylw "[3/3] verify: tokens unreadable by ea-bot, reads through the mediator, send refused"
  # The tokens are the owner's since [0/3]; a failure here leaves them so, and
  # the resident without mail (fail closed), never with a token.
  if sudo -u ea-bot test -r "$TOKEN_PROBE"; then fail "ea-bot can STILL read $TOKEN_PROBE (an ACL or a group grant?); tokens left ${REAL_USER}-owned"; fi
  if ! selftest post; then set_flag false; fail "forward path broken; flag disabled, tokens left ${REAL_USER}-only (the resident has no mail until the mediator works)"; fi
  if send_refused; then grn "  ea-bot: token unreadable, reads/drafts work via mediator, send refused"; else set_flag false; fail "send-draft was NOT refused; flag disabled, tokens left ${REAL_USER}-only"; fi

  echo
  grn "MAIL MEDIATION ENABLED -- the resident (ea-bot) can no longer send mail or read the raw tokens."
  echo "  mediator audit: $REAL_HOME/.ea-private/logs/mail-mediator.log"
  echo "  rollback anytime:  sudo bash $0 --rollback"
  echo
  status
}

case "${1:-}" in
  --rollback) rollback ;;
  --status)   status ;;
  ""|--enable) enable ;;
  *) echo "usage: sudo bash $0 [--enable|--rollback|--status]"; exit 2 ;;
esac
