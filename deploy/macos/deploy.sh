#!/bin/bash
# deploy.sh (macOS): deploy the committed HEAD to ea-bot's clone and to
# /opt/ea-system, then restart the services whose in-memory code changed.
#
# The macOS half of .githooks/post-commit, which execs this after its
# "main checkout on main" check; also run by hand, as the owner, no sudo:
#     bash deploy/macos/deploy.sh
# Restarts go through the sudoers grant install-services.sh writes
# (/etc/sudoers.d/ea-services: two `launchctl kickstart` commands only).
set -uo pipefail

[ "$(uname -s)" = Darwin ] || { echo "[deploy] macOS only" >&2; exit 1; }
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
EABOT_HOME="$(dscl . -read /Users/ea-bot NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[ -n "$EABOT_HOME" ] || { echo "[deploy] no ea-bot user" >&2; exit 1; }
DEPLOY_DIR="$(cd -P -- "$EABOT_HOME" && pwd -P)/ea-system"
LIVE_DIR=/opt/ea-system
PRIV="${EA_PRIVATE_DIR:-$HOME/.ea-private}"
# launchd labels: a new install uses the neutral prefix; an install
# deployed under the legacy prefix keeps it (preprocessor/launchd_labels.py).
LABEL_PREFIX=org.ea-system
for _n in ea-chat-bot ea-mail-mediator; do
    if [ -f "/Library/LaunchDaemons/edu.colorado.$_n.plist" ]; then LABEL_PREFIX=edu.colorado; fi  # legacy-launchd-label
done
BOT=$LABEL_PREFIX.ea-chat-bot
MED=$LABEL_PREFIX.ea-mail-mediator
red() { printf '\033[31m%s\033[0m\n' "$*"; }

# Same rule as the Linux hook: ea-bot can rename its home's contents, so git
# runs only in a directory the shell has entered and checked: reached with no
# symlink, it and its .git real directories owned by us that no one else can
# write.
deploy_tree_ok() {
    [ "$(pwd -P)" = "$DEPLOY_DIR" ] || return 1
    local d
    for d in . .git; do
        [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ] || return 1
        [ $(( 0$(stat -f %Lp -- "$d") & 022 )) -eq 0 ] || return 1
    done
}
deploy_git() (
    cd -P -- "$DEPLOY_DIR" 2>/dev/null && deploy_tree_ok || exit 97
    for v in $(compgen -e); do case "$v" in GIT_*) unset "$v" ;; esac; done
    exec git --git-dir=.git --work-tree=. "$@"
)

REPO_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
OLD_HEAD="$(deploy_git rev-parse HEAD 2>/dev/null)"
if [ $? -eq 97 ]; then
    red "[deploy] UNSAFE DEPLOY TREE: $DEPLOY_DIR and its .git must be real directories owned by $(id -un), writable by no one else. NOT deploying."
    exit 1
fi
deploy_git fetch -q "$REPO_ROOT" HEAD || { red "[deploy] FETCH FAILED; nothing deployed, nothing restarted."; exit 1; }
deploy_git reset -q --hard FETCH_HEAD || { red "[deploy] RESET FAILED; fix $DEPLOY_DIR by hand before restarting."; exit 1; }
# The developer guide stays out of ea-bot's tree: Claude Code would load
# .claude/CLAUDE.md into every EA turn.
deploy_git sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md' >/dev/null 2>&1 || red "[deploy] .claude/CLAUDE.md may be in $DEPLOY_DIR (sparse-checkout failed)"
[ "$(deploy_git rev-parse HEAD)" = "$REPO_HEAD" ] || { red "[deploy] HEAD MISMATCH; not restarting."; exit 1; }
( cd -P -- "$DEPLOY_DIR" && deploy_tree_ok && chgrp -R ea . && chmod -R g+rX,g-w . ) \
    || red "[deploy] could not reset group modes in $DEPLOY_DIR"
echo "[deploy] $DEPLOY_DIR at $(git -C "$REPO_ROOT" rev-parse --short HEAD)"

if [ -d "$LIVE_DIR" ]; then
    EXPORT="$(mktemp -d)" || { red "[deploy] NO TEMP DIR; not restarting."; exit 1; }
    # rsync -a copies the source directory's own mode and group onto
    # $LIVE_DIR; mktemp -d is 0700, which locked ea-bot (group ea) out.
    chmod 755 "$EXPORT"
    if ! ( set -o pipefail; git -C "$REPO_ROOT" -c tar.umask=022 archive --format=tar "$REPO_HEAD" | tar -x -C "$EXPORT" -f - ); then
        rm -rf "$EXPORT"; red "[deploy] EXPORT FAILED; not restarting."; exit 1
    fi
    if ! rsync -a --delete --exclude .git "$EXPORT/" "$LIVE_DIR/"; then
        rm -rf "$EXPORT"; red "[deploy] LIVE SYNC FAILED ($LIVE_DIR); not restarting."; exit 1
    fi
    rm -rf "$EXPORT"
    git -C "$REPO_ROOT" rev-parse --short HEAD > "$LIVE_DIR/.deployed-commit"
    # The modes install-services.sh sets: owner:ea, the top 0750, nothing
    # group-writable. The mediator's resident helper reads this tree as ea-bot.
    if ! { chgrp -R ea "$LIVE_DIR" && chmod 750 "$LIVE_DIR" && chmod -R g-w,o-w "$LIVE_DIR"; }; then
        red "[deploy] could not reset $LIVE_DIR to $(id -un):ea 0750; ea-bot may not read it"
    fi
    echo "[deploy] $LIVE_DIR at $(cat "$LIVE_DIR/.deployed-commit")"
else
    echo "[deploy] no $LIVE_DIR yet (deploy/macos/install-services.sh creates it); services not restarted"
    exit 0
fi

CHANGED="$(git -C "$REPO_ROOT" diff --name-only "${OLD_HEAD:-$REPO_HEAD}" "$REPO_HEAD" 2>/dev/null)"
job_up() { launchctl print "system/$1" 2>/dev/null | grep -q '^[[:space:]]*pid = '; }
restart() {   # $1 label
    if sudo -n /bin/launchctl kickstart -k "system/$1" 2>/dev/null; then
        for _ in $(seq 1 30); do job_up "$1" && { echo "[deploy] $1 restarted"; return 0; }; sleep 2; done
        red "[deploy] $1 did not come back; see $PRIV/logs/$1.launchd.log"
    else
        red "[deploy] could not restart $1 (no sudoers grant?). Run: sudo launchctl kickstart -k system/$1"
    fi
}

# The bot: only when code it holds in memory changed, and only once no
# per-thread run is in flight (same rule and 1200 s limit as the Linux hook).
if grep -qE '^(ea-chat-bot\.py|preprocessor/.*\.py|botcore/botcore/.*\.py|scripts/disallowed-tools[^/]*\.txt|requirements\.txt)$' <<<"$CHANGED"; then
    INFLIGHT="$PRIV/bot-inflight.json"
    for i in $(seq 1 300); do
        BUSY=$(python3 -c "
import json
d = json.load(open('$INFLIGHT'))
print(sum(1 for v in d.values() if v.get('lane') not in ('resident', 'deferred')))" 2>/dev/null || echo 0)
        [ "$BUSY" = 0 ] && break
        [ $((i % 15)) = 1 ] && echo "[deploy] waiting for $BUSY in-flight dispatch(es) ($((i * 4))s of 1200s)..."
        sleep 4
    done
    restart "$BOT"
else
    echo "[deploy] no runtime code changed; the bot keeps running"
fi
if grep -qE '^preprocessor/(mail_mediat(ion|or)|mail_privacy|remote_llm)\.py$' <<<"$CHANGED"; then
    echo "[deploy] mediation code changed"
    restart "$MED"
fi
echo "[deploy] done"
