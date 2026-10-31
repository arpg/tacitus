#!/bin/bash
# install-services.sh: the owner-side services on macOS, hardened from the start.
#
# The macOS counterpart of the systemd units plus deploy/harden-permissions.sh
# (docs/SETUP.md "Services" and "Hardening"). Installs two root LaunchDaemons that run as the
# owner, never as ea-bot:
#
#   <prefix>.ea-chat-bot        /opt/ea-system/ea-chat-bot.py
#   <prefix>.ea-mail-mediator   /opt/ea-system/preprocessor/mail_mediator.py
#
# <prefix> is org.ea-system on a new install; an install deployed under the
# legacy prefix keeps that one.
#
# With an update channel on (docs/updates.md), /opt/ea-system becomes the
# updater's release layout (scripts/update.py layout) and a third
# LaunchDaemon, <prefix>.tacitus-update, runs the updater as the owner every
# hour. Without one, the tree stays the plain directory deploy.sh keeps.
#
# Same guarantees as the hardened Linux units: code from /opt/ea-system (an
# owner-owned export of the owner's repo under root-owned /opt, not ea-bot's
# clone), the interpreter from the owner's private home, and environment only
# from owner-private files (~/.ea-private/bot.env, secrets.env), loaded by the
# root-owned launcher /usr/local/libexec/ea-service-run.sh. Every file here is
# root- or owner-owned; ea-bot can rewrite none of it.
#
# Run AFTER the chat platform is configured (docs/SETUP.md "Zulip server" or "Slack"): step 3 below
# seeds ~/.ea-private/chat.json from ea-config.json's chat block only when the
# file is missing, so running earlier would freeze the template values.
# On a profile with no chat platform (email, review-only) it installs the
# mediator alone; deploy/enable-mail-mediation.sh then turns mediation on.
#
#   sudo bash deploy/macos/install-services.sh            # install / re-install
#   sudo bash deploy/macos/install-services.sh --status
#   sudo bash deploy/macos/install-services.sh --remove   # leaves /opt and ~/.ea-private
#
# Design and troubleshooting: docs/macos-services.md
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only; on Linux use deploy/harden-permissions.sh." >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo." >&2; exit 1; }
OWNER="${SUDO_USER:-}"
[ -n "$OWNER" ] && [ "$OWNER" != root ] || { echo "ERROR: run with sudo from the owner account." >&2; exit 1; }

OWNER_UID="$(id -u "$OWNER")"
OWNER_GROUP="$(id -gn "$OWNER")"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
EABOT_HOME="$(dscl . -read /Users/ea-bot NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)"
[ -n "$EABOT_HOME" ] || { echo "ERROR: no ea-bot user; run scripts/install/setup-ea-user-macos.sh first." >&2; exit 1; }
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
EA_TREE="$EABOT_HOME/.ea"
LIVE=/opt/ea-system
PRIV="$OWNER_HOME/.ea-private"
BOT_ENV="$PRIV/bot.env"
SECRETS="$PRIV/secrets.env"
# The interpreter: the checkout's venv, else the python3 install.sh chose
# (its shim), else Homebrew's. Apple's /usr/bin/python3 is too old.
VENV_PY=""
for _py in "$REPO_DIR/.venv/bin/python3" "$OWNER_HOME/.local/share/ea-system/bin/python3" \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$_py" ]; then VENV_PY="$_py"; break; fi
done
[ -n "$VENV_PY" ] || { echo "ERROR: no python3 3.10+ for the services; run ./install.sh first." >&2; exit 1; }
LAUNCHER=/usr/local/libexec/ea-service-run.sh
SUDOERS=/etc/sudoers.d/ea-services
# launchd labels: a new install uses the neutral prefix; an install
# deployed under the legacy prefix keeps it (preprocessor/launchd_labels.py).
LABEL_PREFIX=org.ea-system
for _n in ea-chat-bot ea-mail-mediator; do
    if [ -f "/Library/LaunchDaemons/edu.colorado.$_n.plist" ]; then LABEL_PREFIX=edu.colorado; fi  # legacy-launchd-label
done
BOT=$LABEL_PREFIX.ea-chat-bot
MED=$LABEL_PREFIX.ea-mail-mediator
UPD=$LABEL_PREFIX.tacitus-update
PATH_LIST="$REPO_DIR/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

plist_of() { echo "/Library/LaunchDaemons/$1.plist"; }
log_of()   { echo "$PRIV/logs/$1.launchd.log"; }

job_line() {   # one line of state for a job
    local out pid state code
    out="$(launchctl print "system/$1" 2>/dev/null)" || { echo "not loaded"; return; }
    state="$(awk -F' = ' '/^\tstate = /{print $2; exit}' <<<"$out")"
    pid="$(awk -F' = ' '/^\tpid = /{print $2; exit}' <<<"$out")"
    code="$(awk -F' = ' '/last exit code = /{print $2; exit}' <<<"$out")"
    echo "state=${state:-?} pid=${pid:--} last_exit=${code:--}"
}

status() {
    echo "== jobs"
    for j in "$BOT" "$MED" "$UPD"; do printf '  %-32s %s\n' "$j" "$(job_line "$j")"; done
    echo "== code"
    echo "  live:  $(cat "$LIVE/.version" 2>/dev/null || true) $(cat "$LIVE/.deployed-commit" 2>/dev/null || echo none)  ($LIVE)"
    echo "  repo:  $(sudo -u "$OWNER" git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo ?)  ($REPO_DIR)"
    echo "== files"
    ls -l "$LAUNCHER" "$(plist_of "$BOT")" "$(plist_of "$MED")" "$(plist_of "$UPD")" "$SUDOERS" 2>&1 | sed 's/^/  /'
    ls -l "$BOT_ENV" "$SECRETS" "$PRIV/chat.json" 2>&1 | sed 's/^/  /'
}

if [ "${1:-}" = --status ]; then status; exit 0; fi
if [ "${1:-}" = --remove ]; then
    for j in "$BOT" "$MED" "$UPD"; do
        launchctl bootout "system/$j" 2>/dev/null || true
        rm -f "$(plist_of "$j")"
    done
    rm -f "$LAUNCHER" "$SUDOERS"
    echo "Removed the LaunchDaemons, the launcher and $SUDOERS."
    echo "Left in place: $LIVE and $PRIV (bot.env, secrets.env, chat.json)."
    exit 0
fi

for p in "$VENV_PY" "$EABOT_HOME/ea-system/ea-chat-bot.py" \
         "$REPO_DIR/preprocessor/owner_trust.py" "$REPO_DIR/deploy/macos/ea-service-run.sh"; do
    [ -e "$p" ] || { echo "ERROR: missing expected path: $p" >&2; exit 1; }
done
[ -d "$PRIV" ] && [ ! -L "$PRIV" ] && [ "$(stat -f %u "$PRIV")" = "$OWNER_UID" ] \
    && [ "$(stat -f %Lp "$PRIV")" = 700 ] \
    || { echo "ERROR: $PRIV must be a 0700 directory owned by $OWNER." >&2; exit 1; }
echo "[services] owner=$OWNER  interpreter=$VENV_PY  live=$LIVE  private=$PRIV"

# Owner-side steps run as the owner with a clean environment, as
# harden-permissions.sh does with runuser.
as_owner() {
    sudo -u "$OWNER" env -i HOME="$OWNER_HOME" PATH=/usr/bin:/bin \
        EA_DIR="$EA_TREE" EA_PRIVATE_DIR="$PRIV" "$@"
}

# The jobs this profile runs: the mediator always, the bot only with a
# chat platform. A bot job left from an earlier profile is removed.
JOBS="$MED"
if as_owner "$VENV_PY" "$REPO_DIR/preprocessor/ea_config.py" feature chat >/dev/null 2>&1; then
    JOBS="$MED $BOT"
    echo "[services] the profile has a chat platform: the bot and the mediator"
else
    echo "[services] no chat platform on this profile: the mediator only, no bot job"
    launchctl bootout "system/$BOT" 2>/dev/null || true
    rm -f "$(plist_of "$BOT")"
fi

echo "[services] 1/7  owner-owned code tree at $LIVE (git archive of $REPO_DIR HEAD)"
# export_head DIR: the owner's HEAD into DIR (an empty directory).
export_head() {
    ( set -o pipefail
      sudo -u "$OWNER" git -C "$REPO_DIR" -c tar.umask=022 archive --format=tar HEAD \
          | tar -x -C "$1" -f - )
}
# The update channel decides the tree's shape (docs/updates.md), as in
# deploy/harden-permissions.sh.
CHANNEL="$(as_owner "$VENV_PY" "$REPO_DIR/scripts/update.py" --live "$LIVE" channel 2>/dev/null || echo off)"
UPDATER=0
if [ -L "$LIVE" ]; then
    echo "  $LIVE is the updater's release layout (channel $CHANNEL); its code is the updater's to change"
    "$VENV_PY" -I "$REPO_DIR/scripts/update.py" --live "$LIVE" layout --owner "$OWNER"
    UPDATER=1
elif [ "$CHANNEL" = latest ] || [ "$CHANNEL" = stable ]; then
    echo "  update channel $CHANNEL: $LIVE becomes the updater's release layout (docs/updates.md)"
    if [ -d "$LIVE" ]; then
        "$VENV_PY" -I "$REPO_DIR/scripts/update.py" --live "$LIVE" layout --owner "$OWNER"
    else
        SEED="$(mktemp -d)"
        chmod 755 "$SEED"
        export_head "$SEED" || { rm -rf "$SEED"; echo "ERROR: git archive failed; $LIVE unchanged" >&2; exit 1; }
        sudo -u "$OWNER" git -C "$REPO_DIR" rev-parse --short HEAD > "$SEED/.deployed-commit"
        sudo -u "$OWNER" git -C "$REPO_DIR" describe --tags --exact-match --match 'v[0-9]*' HEAD \
            > "$SEED/.version" 2>/dev/null || rm -f "$SEED/.version"
        "$VENV_PY" -I "$REPO_DIR/scripts/update.py" --live "$LIVE" layout --owner "$OWNER" --seed "$SEED" \
            || { rm -rf "$SEED"; echo "ERROR: the release layout failed; $LIVE unchanged" >&2; exit 1; }
        rm -rf "$SEED"
    fi
    UPDATER=1
else
    install -d -o "$OWNER" -g ea -m 750 "$LIVE"
    EXPORT="$(mktemp -d)"
    chmod 755 "$EXPORT"
    export_head "$EXPORT" || { rm -rf "$EXPORT"; echo "ERROR: git archive failed; $LIVE unchanged" >&2; exit 1; }
    rsync -a --delete --exclude .git "$EXPORT/" "$LIVE/"
    rm -rf "$EXPORT"
    sudo -u "$OWNER" git -C "$REPO_DIR" rev-parse --short HEAD > "$LIVE/.deployed-commit"
    chown -R "$OWNER:ea" "$LIVE"
    chmod 750 "$LIVE"
fi

echo "[services] 2/7  owner-owned bot environment ($BOT_ENV)"
MODEL_LINE="$(grep -E '^EA_MODEL=' "$EA_TREE/.env" 2>/dev/null || true)"
{ echo "# EA bot process environment (owner-owned; not writable by ea-bot)."
  echo "CLAUDE_BIN=$EABOT_HOME/.local/bin/claude"
  [ -n "$MODEL_LINE" ] && echo "$MODEL_LINE"; } > "$BOT_ENV.tmp"
chown "$OWNER:$OWNER_GROUP" "$BOT_ENV.tmp"; chmod 600 "$BOT_ENV.tmp"
mv -f "$BOT_ENV.tmp" "$BOT_ENV"

echo "[services] 3/7  owner-private secrets and chat settings (seeded only when missing)"
as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" seed --ea-dir "$EA_TREE" \
    || { echo "ERROR: the owner-private seed failed; nothing after step 2 was changed" >&2; exit 1; }

echo "[services] 4/7  harvested token file modes"
as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" token-modes --ea-dir "$EA_TREE" \
    || echo "[services] WARNING: could not set the mode of a token file (see above)" >&2

echo "[services] 5/7  root-owned launcher ($LAUNCHER)"
install -d -o root -g wheel -m 755 /usr/local/libexec
install -o root -g wheel -m 755 "$REPO_DIR/deploy/macos/ea-service-run.sh" "$LAUNCHER"

echo "[services] 6/7  LaunchDaemons and the restart grant"
write_plist() {   # $1 label, $2 program (under $LIVE), $3 extra env XML, $4 throttle
    local plist log
    plist="$(plist_of "$1")"; log="$(log_of "$1")"
    install -d -o "$OWNER" -g "$OWNER_GROUP" -m 700 "$PRIV/logs"
    [ -e "$log" ] || install -o "$OWNER" -g "$OWNER_GROUP" -m 600 /dev/null "$log"
    cat > "$plist.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Written by deploy/macos/install-services.sh. Do not edit; re-run it. -->
<plist version="1.0">
<dict>
    <key>Label</key><string>$1</string>
    <key>UserName</key><string>$OWNER</string>
    <key>InitGroups</key><true/>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$LAUNCHER</string>
        <string>--env</string><string>$BOT_ENV</string>
        <string>--env</string><string>$SECRETS</string>
        <string>--</string>
        <string>$VENV_PY</string>
        <string>$LIVE/$2</string>
    </array>
    <key>WorkingDirectory</key><string>$LIVE</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$OWNER_HOME</string>
        <key>PATH</key><string>$PATH_LIST</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>$3
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>$4</integer>
    <key>StandardOutPath</key><string>$log</string>
    <key>StandardErrorPath</key><string>$log</string>
</dict>
</plist>
EOF
    plutil -lint "$plist.tmp" >/dev/null
    chown root:wheel "$plist.tmp"; chmod 644 "$plist.tmp"
    mv -f "$plist.tmp" "$plist"
}
case " $JOBS " in *" $BOT "*) write_plist "$BOT" ea-chat-bot.py "" 10 ;; esac
write_plist "$MED" preprocessor/mail_mediator.py "
        <key>EA_DIR</key><string>$EA_TREE</string>" 5

# The updater: hourly, as the owner, with its own wait of up to 15 minutes
# first (launchd has no randomized delay). Not KeepAlive: each run ends.
if [ "$UPDATER" = 1 ]; then
    install -d -o "$OWNER" -g "$OWNER_GROUP" -m 700 "$PRIV/logs"
    [ -e "$PRIV/logs/update.log" ] || install -o "$OWNER" -g "$OWNER_GROUP" -m 600 /dev/null "$PRIV/logs/update.log"
    cat > "$(plist_of "$UPD").tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Written by deploy/macos/install-services.sh. Do not edit; re-run it. -->
<plist version="1.0">
<dict>
    <key>Label</key><string>$UPD</string>
    <key>UserName</key><string>$OWNER</string>
    <key>InitGroups</key><true/>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PY</string>
        <string>$LIVE/scripts/update.py</string>
        <string>run</string>
        <string>--jitter</string><string>900</string>
    </array>
    <key>WorkingDirectory</key><string>$OWNER_HOME</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$OWNER_HOME</string>
        <key>PATH</key><string>$PATH_LIST</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
    </dict>
    <key>StartInterval</key><integer>3600</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$PRIV/logs/update.log</string>
    <key>StandardErrorPath</key><string>$PRIV/logs/update.log</string>
</dict>
</plist>
PLIST
    plutil -lint "$(plist_of "$UPD").tmp" >/dev/null
    chown root:wheel "$(plist_of "$UPD").tmp"; chmod 644 "$(plist_of "$UPD").tmp"
    mv -f "$(plist_of "$UPD").tmp" "$(plist_of "$UPD")"
    launchctl bootout "system/$UPD" 2>/dev/null || true
    launchctl bootstrap system "$(plist_of "$UPD")"
    echo "  $UPD: hourly; output in $PRIV/logs/update.log"
else
    launchctl bootout "system/$UPD" 2>/dev/null || true
    rm -f "$(plist_of "$UPD")"
fi

# The post-commit deploy restarts the two jobs; these exact commands only.
cat > "$SUDOERS.tmp" <<EOF
# Written by deploy/macos/install-services.sh: the deploy hook restarts the
# EA services after it syncs $LIVE. These two commands only.
$OWNER ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/$BOT, /bin/launchctl kickstart -k system/$MED
EOF
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || { rm -f "$SUDOERS.tmp"; echo "ERROR: sudoers syntax check failed" >&2; exit 1; }
mv -f "$SUDOERS.tmp" "$SUDOERS"

echo "[services] 7/7  load and verify"
ok=1
for j in $JOBS; do
    launchctl bootout "system/$j" 2>/dev/null || true
    launchctl bootstrap system "$(plist_of "$j")"
done
for j in $JOBS; do
    pid=""
    for _ in $(seq 1 30); do
        pid="$(launchctl print "system/$j" 2>/dev/null | awk -F' = ' '/^\tpid = /{print $2; exit}')"
        [ -n "$pid" ] && break
        sleep 2
    done
    if [ -z "$pid" ]; then
        printf '  %-32s NOT RUNNING  %s\n' "$j" "$(job_line "$j")"; ok=0
        tail -5 "$(log_of "$j")" 2>/dev/null | sed 's/^/    /'
        continue
    fi
    user="$(ps -o user= -p "$pid" | tr -d ' ')"
    cmd="$(ps -o command= -p "$pid")"
    printf '  %-32s running pid=%s user=%s\n' "$j" "$pid" "$user"
    [ "$user" = "$OWNER" ] || { echo "    runs as $user, not $OWNER" >&2; ok=0; }
    case "$cmd" in *"$LIVE/"*) ;; *) echo "    not running from $LIVE: $cmd" >&2; ok=0 ;; esac
    case "$cmd" in *"$EABOT_HOME/"*) echo "    command line names ea-bot space: $cmd" >&2; ok=0 ;; esac
done

if [ "$ok" = 1 ]; then
    echo "[services] done. The jobs ($JOBS) run as $OWNER from $LIVE with owner-private environment files only."
    echo "[services] next, mail mediation: sudo bash $REPO_DIR/deploy/enable-mail-mediation.sh"
    echo "[services] confirm the chat values printed in step 3; then run: $VENV_PY $REPO_DIR/scripts/ea-doctor.py"
else
    echo "[services] WARNING: a job is not running as expected. Logs: $PRIV/logs/*.launchd.log" >&2
    exit 1
fi
