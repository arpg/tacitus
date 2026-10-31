#!/usr/bin/env bash
#
# harden-permissions.sh — keep the owner's services from trusting anything in
# the ea-bot user's tree. Run once, as root:  sudo bash deploy/harden-permissions.sh
#
# The EA runs its own subprocesses as the `ea-bot` user (isolation), while the
# bot and the mail mediator run as the OWNER (the human who installed the EA).
# Everything under ea-bot's home is ea-bot space: ea-bot owns the directories
# above every path there and can rename an entry away and put its own in its
# place, whoever owns the file. This closes the places where an owner process
# trusted a path there:
#
#   Gap 1 (env files): the ea-chat-bot unit loaded EnvironmentFile= from
#     ea-bot's ~/.ea/.env, and both units from ~/.ea/.env.secrets; systemd
#     reads them without an ownership check. The drop-ins reset the list and
#     load only owner-private files: EA_MODEL from bot.env, the secrets from
#     secrets.env, copied once from the legacy file after the ownership checks
#     (owner_trust.py seed). The mediator gets secrets.env only when it holds a
#     GRAPH_* setting, the only values the mediator reads from its environment.
#
#   Gap 2 (code dir): both services ran code (and the mediator its interpreter)
#     from ea-bot's ~/ea-system, whose parent ea-bot owns and could rename.
#     They now run from /opt/ea-system (parent /opt is root-owned) with the
#     interpreter from the owner's private home. The deploy hook keeps
#     /opt/ea-system in step on every commit; this script seeds it once.
#
#   Chat trust settings: the bot decides which server it talks to and whose
#     actions count as the owner's from the chat block of ea-config.json,
#     which ea-bot writes. The seed copies that block to the owner-private
#     chat.json, which the owner's processes then read instead (they refuse
#     to run a frontend without it). It shows every value and writes the
#     file only after the operator types back frontend, site, bot_email and
#     owner_user_id (or passes --chat-* flags; see owner_trust.py).
#
#   Harvested token files: under mail mediation only the owner's mediator
#     reads them, so each existing one is set to 0600 (owner_trust.py
#     token-modes).
#
#   Scheduled persistence: ea-bot gets no crontab and no at job of its own
#     (/etc/cron.deny and /etc/at.deny, or its line dropped from a
#     cron.allow), and an existing crontab is saved to the private dir and
#     removed. Its linger is switched off unless the deck builder, which
#     needs it, is enabled. scripts/boundary-audit.py reports all of it.
#
#   The code tree and updates: with an update channel on (update.channel in
#     ea-config.json or the owner-private update.json; a new install's
#     template says auto), /opt/ea-system becomes the updater's release
#     layout, a root-owned link into the owner's /opt/ea-system.releases
#     (scripts/update.py layout), and the hourly tacitus-update.timer runs
#     the updater as the owner. A layout already there is the updater's, and
#     its code is left alone. With no channel the tree stays the plain
#     directory the post-commit hook keeps at HEAD (docs/updates.md).
#
#   The bot unit: the chat bot runs as a system unit, ea-chat-bot.service,
#     as the owner with SupplementaryGroups=ea (a user unit keeps the groups
#     its user manager started with, and loses the ea group it needs). On a
#     profile with a chat platform and no unit yet, this script writes it
#     (the drop-in below gives it its ExecStart) and the restart grant the
#     deploy hook uses. An existing system unit is kept as it is. A user
#     unit left by an older scripts/install/setup.sh is reported with the step that retires
#     it, and no system unit is written beside it, since two bots would
#     both consume the chat events.
#
# Gap 3b (state writes) is fixed in code (atomic_io, random temp names); the
# auth directory and token files are checked in code (owner_trust.py). Gap 3a
# (cron-log appends) is fixed by the owner-only log directory (log_paths.py).
#
# Owner-agnostic: it derives the owner from $SUDO_USER (or --owner NAME), so a
# new install hardens for whoever set it up, not a hard-coded account.
# Idempotent and reversible (drop-ins + owner-private files; the shipped units
# are never edited, and the legacy files are left in place). Revert: rm the
# two .d/harden.conf files, daemon-reload, restart; the units then load the
# legacy files again, which are unchanged.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo bash deploy/harden-permissions.sh" >&2
    exit 1
fi

# --- who owns the services (the human account), and ea-bot (the EA user) ----
OWNER="${SUDO_USER:-}"
if [ "${1:-}" = "--owner" ] && [ -n "${2:-}" ]; then OWNER="$2"; fi
if [ -z "$OWNER" ] || [ "$OWNER" = root ]; then
    echo "cannot tell which account owns the services; re-run with --owner NAME" >&2
    exit 1
fi
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
EABOT_HOME="$(getent passwd ea-bot | cut -d: -f6)"
[ -n "$OWNER_HOME" ] || { echo "no home for owner '$OWNER'" >&2; exit 1; }
[ -n "$EABOT_HOME" ] || { echo "the ea-bot user does not exist (run scripts/install/setup-ea-user.sh first)" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EABOT_TREE="$EABOT_HOME/ea-system"
EA_TREE="$EABOT_HOME/.ea"
LIVE=/opt/ea-system
PRIV="$OWNER_HOME/.ea-private"
BOT_ENV="$OWNER_HOME/.ea-private/bot.env"
OWNER_SECRETS="$PRIV/secrets.env"
OWNER_CHAT="$PRIV/chat.json"
# The mediator unit's sandbox, shared with deploy/enable-mail-mediation.sh.
. "$REPO_DIR/deploy/mediator-sandbox.sh"

# Which units exist. The mediator unit comes from
# deploy/enable-mail-mediation.sh. The code tree, the owner-private files
# and the token modes are hardened either way; the drop-in, restart and
# check of a unit happen only when that unit is installed (or, for the
# bot, written below).
unit_installed() {
    case "$(systemctl show -p LoadState --value "$1" 2>/dev/null)" in
        ""|not-found) return 1 ;;
        *) return 0 ;;
    esac
}
HAS_BOT=0; unit_installed ea-chat-bot && HAS_BOT=1
HAS_MEDIATOR=0; unit_installed ea-mail-mediator && HAS_MEDIATOR=1
USER_UNIT="$OWNER_HOME/.config/systemd/user/ea-chat-bot.service"
[ "$HAS_MEDIATOR" = 1 ] || echo "[harden] no ea-mail-mediator unit: its steps are skipped"

# The interpreter: take the one the running unit already uses (owner's venv),
# so we never guess a venv location. Fall back to the owner's ea-system venv,
# then the python3 install.sh chose (its shim), then the python3 on PATH.
VENV_PY="$(systemctl show -p ExecStart --value ea-chat-bot 2>/dev/null \
    | grep -oE '/[^ ]*/python3?' | head -1 || true)"
for _py in "$VENV_PY" "$OWNER_HOME/ea-system/.venv/bin/python3" \
           "$OWNER_HOME/.local/share/ea-system/bin/python3" "$(command -v python3 || true)"; do
    if [ -n "$_py" ] && [ -x "$_py" ]; then VENV_PY="$_py"; break; fi
done

for p in "$EABOT_TREE/ea-chat-bot.py" "$VENV_PY" \
         "$REPO_DIR/preprocessor/owner_trust.py"; do
    [ -e "$p" ] || { echo "missing expected path: $p" >&2; exit 1; }
done
echo "[harden] owner=$OWNER  interpreter=$VENV_PY  live=$LIVE  private=$PRIV"
OWNER_GROUP="$(id -gn "$OWNER")"

# Owner-side steps run AS the owner, never as root: they read files in ea-bot
# space, and the owner's ownership checks are what decide what is copied.
as_owner() {
    runuser -u "$OWNER" -- env -i HOME="$OWNER_HOME" PATH=/usr/bin:/bin \
        EA_DIR="$EA_TREE" EA_PRIVATE_DIR="$PRIV" "$@"
}

# The bot unit: say what is there, and write the system unit when the
# profile has a chat platform and no unit exists yet.
BOT_NEW=0; BOT_RETIRE=0
if [ "$HAS_BOT" = 1 ]; then
    echo "[harden] found the system unit ea-chat-bot.service: its drop-in is written below"
elif [ -e "$USER_UNIT" ]; then
    echo "[harden] found a user unit, $USER_UNIT, and no system unit."
    echo "[harden]   The bot runs as a system unit. Retire the user unit as $OWNER, then run this script again:"
    echo "[harden]   systemctl --user disable --now ea-chat-bot && rm $USER_UNIT && systemctl --user daemon-reload"
    echo "[harden]   The bot steps are skipped this run."
    BOT_RETIRE=1
elif as_owner "$VENV_PY" "$REPO_DIR/preprocessor/ea_config.py" feature chat >/dev/null 2>&1; then
    echo "[harden] no ea-chat-bot unit yet and the profile has a chat platform: writing /etc/systemd/system/ea-chat-bot.service"
    cat > /etc/systemd/system/ea-chat-bot.service <<EOF
# Written by deploy/harden-permissions.sh. ExecStart, WorkingDirectory and
# the environment files come from ea-chat-bot.service.d/harden.conf.
[Unit]
Description=EA chat bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$OWNER
Group=$OWNER_GROUP
SupplementaryGroups=ea
Environment=HOME=$OWNER_HOME
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 /etc/systemd/system/ea-chat-bot.service
    HAS_BOT=1; BOT_NEW=1
else
    echo "[harden] no ea-chat-bot unit (no chat platform on this profile): its steps are skipped"
fi

echo "[harden] 1/7  owner-owned code tree at $LIVE"
# The code comes from the owner's own repo (the one this script is in), as
# `git archive` of its HEAD run as the owner, never from ea-bot's copy.
# export_head DIR: that export into DIR (an empty directory).
export_head() {
    ( set -o pipefail
      runuser -u "$OWNER" -- git -C "$REPO_DIR" -c tar.umask=022 archive --format=tar HEAD \
          | tar -x -C "$1" -f - )
}
# The update channel decides the tree's shape (docs/updates.md): latest or
# stable makes it the updater's release layout; off (or no channel on a
# tree the hook keeps) leaves it the plain directory.
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
        # The first release is the checkout's HEAD; the version file names it
        # when HEAD is a release tag, and the updater moves on from there.
        SEED="$(mktemp -d)"
        chmod 755 "$SEED"
        export_head "$SEED" \
            || { rm -rf "$SEED"; echo "git archive of $REPO_DIR failed; $LIVE unchanged" >&2; exit 1; }
        runuser -u "$OWNER" -- git -C "$REPO_DIR" rev-parse --short HEAD > "$SEED/.deployed-commit" 2>/dev/null || true
        runuser -u "$OWNER" -- git -C "$REPO_DIR" describe --tags --exact-match --match 'v[0-9]*' HEAD \
            > "$SEED/.version" 2>/dev/null || rm -f "$SEED/.version"
        "$VENV_PY" -I "$REPO_DIR/scripts/update.py" --live "$LIVE" layout --owner "$OWNER" --seed "$SEED" \
            || { rm -rf "$SEED"; echo "the release layout failed; $LIVE unchanged" >&2; exit 1; }
        rm -rf "$SEED"
    fi
    UPDATER=1
else
    install -d -o "$OWNER" -g ea -m 750 "$LIVE"
    EXPORT="$(mktemp -d)"
    chmod 755 "$EXPORT"
    export_head "$EXPORT" \
        || { rm -rf "$EXPORT"; echo "git archive of $REPO_DIR failed; $LIVE unchanged" >&2; exit 1; }
    rsync -a --delete --exclude .git "$EXPORT/" "$LIVE/"
    rm -rf "$EXPORT"
    runuser -u "$OWNER" -- git -C "$REPO_DIR" rev-parse --short HEAD > "$LIVE/.deployed-commit" 2>/dev/null || true
    chown -R "$OWNER":ea "$LIVE"
fi

echo "[harden] 2/7  owner-owned env file for the bot ($BOT_ENV)"
install -d -o "$OWNER" -g "$OWNER" -m 700 "$OWNER_HOME/.ea-private"
MODEL_LINE="$(grep -E '^EA_MODEL=' "$EA_TREE/.env" 2>/dev/null || true)"
{ echo "# EA bot process environment (owner-owned; not writable by ea-bot)."
  [ -n "$MODEL_LINE" ] && echo "$MODEL_LINE"; } > "$BOT_ENV"
chown "$OWNER":"$OWNER" "$BOT_ENV"; chmod 600 "$BOT_ENV"

echo "[harden] 3/7  owner-private secrets and chat settings ($OWNER_SECRETS, $OWNER_CHAT)"
# Creates each file only when it is missing: secrets.env from the legacy
# .env.secrets if that passes the ownership checks, chat.json from the chat
# block of ea-config.json, once the operator types each trusted value back.
if ! as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" seed --ea-dir "$EA_TREE"; then
    echo "[harden] the owner-private seed failed; nothing else was changed after step 2" >&2
    exit 1
fi

echo "[harden] 4/7  harvested token file modes"
as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" token-modes --ea-dir "$EA_TREE" \
    || echo "[harden] WARNING: could not set the mode of a token file in $EA_TREE/auth (see above)" >&2

echo "[harden] 4b/7 ea-bot's own scheduling (cron, at, linger)"
# The EA schedules nothing of its own: every job is the owner's crontab
# (crontab.master) and runs ea-bot work through sudo. A crontab or at job
# ea-bot installs would run a steered session's code daily and outlive it,
# so ea-bot is denied both. cron.allow, when present, decides alone.
EABOT_CRONTAB="$(crontab -l -u ea-bot 2>/dev/null || true)"
if [ -n "$(printf '%s\n' "$EABOT_CRONTAB" | grep -vE '^[[:space:]]*(#|$)' || true)" ]; then
    SAVED="$PRIV/ea-bot-crontab.removed.$(date +%Y%m%d%H%M%S)"
    printf '%s\n' "$EABOT_CRONTAB" > "$SAVED"
    chown "$OWNER":"$OWNER" "$SAVED"; chmod 600 "$SAVED"
    crontab -r -u ea-bot
    echo "  removed ea-bot's crontab (nothing in the install writes one); a copy is in $SAVED"
fi
for kind in cron at; do
    [ "$kind" = at ] && ! command -v at >/dev/null 2>&1 && continue
    allow="/etc/$kind.allow"; deny="/etc/$kind.deny"
    if [ -f "$allow" ]; then
        if grep -qx 'ea-bot' "$allow"; then
            sed -i '/^ea-bot$/d' "$allow"
            echo "  $allow no longer lists ea-bot"
        else
            echo "  $allow exists and does not list ea-bot"
        fi
    elif ! grep -qx 'ea-bot' "$deny" 2>/dev/null; then
        [ -e "$deny" ] || install -m 644 -o root -g root /dev/null "$deny"
        echo 'ea-bot' >> "$deny"
        echo "  $deny lists ea-bot"
    else
        echo "  $deny already lists ea-bot"
    fi
done
# Linger keeps ea-bot's user manager (and any user unit it enables) running
# with no login. The deck builder needs it, so a deck's static server
# survives a bot restart (deploy/enable-deck-builder.sh); without the deck
# builder nothing does, and it is switched off. Either way the daily
# boundary audit reports ea-bot's user units and its linger.
if grep -q '# ea-deck-builder' /etc/fstab 2>/dev/null; then
    echo "  linger for ea-bot kept: the deck builder needs it (the boundary audit watches its user units)"
elif loginctl show-user ea-bot -p Linger 2>/dev/null | grep -qx 'Linger=yes'; then
    loginctl disable-linger ea-bot && echo "  linger for ea-bot switched off (no deck builder on this install)"
else
    echo "  ea-bot does not linger"
fi

if [ "$HAS_BOT" = 1 ]; then
echo "[harden] 5/7  ea-chat-bot drop-in (environment: owner-private files only)"
install -d -m 755 /etc/systemd/system/ea-chat-bot.service.d
cat > /etc/systemd/system/ea-chat-bot.service.d/harden.conf <<EOF
[Service]
WorkingDirectory=$LIVE
ExecStart=
ExecStart=$VENV_PY $LIVE/ea-chat-bot.py
EnvironmentFile=
EnvironmentFile=$BOT_ENV
EnvironmentFile=-$OWNER_SECRETS
EOF
else
echo "[harden] 5/7  ea-chat-bot drop-in: skipped (no unit)"
fi

if [ "$HAS_MEDIATOR" = 1 ]; then
echo "[harden] 6/7  ea-mail-mediator drop-in"
# The mediator's owner processes find their chat and alert secrets in the
# private dir themselves (owner_trust); from its environment it reads only
# the GRAPH_* settings, so it loads secrets.env only when that file has one.
MEDIATOR_ENV=""
if [ -f "$OWNER_SECRETS" ] && grep -qE '^[[:space:]]*(export[[:space:]]+)?GRAPH_[A-Z0-9_]*=' "$OWNER_SECRETS"; then
    MEDIATOR_ENV="EnvironmentFile=-$OWNER_SECRETS"
    echo "  the mediator loads $OWNER_SECRETS (it holds a GRAPH_* setting)"
else
    echo "  the mediator loads no environment file (no GRAPH_* setting in $OWNER_SECRETS)"
fi
# The sandbox (deploy/mediator-sandbox.sh): the file system read-only but
# for the resident's home, /tmp, the owner-private directory and the staging
# root. Those two exist before the unit starts, since a read-only home cannot
# create them.
mediator_sandbox_dirs "$OWNER" "$OWNER_HOME"
install -d -m 755 /etc/systemd/system/ea-mail-mediator.service.d
cat > /etc/systemd/system/ea-mail-mediator.service.d/harden.conf <<EOF
[Service]
WorkingDirectory=$LIVE
ExecStart=
ExecStart=$VENV_PY $LIVE/preprocessor/mail_mediator.py
EnvironmentFile=
$MEDIATOR_ENV
$(mediator_sandbox "$OWNER_HOME" "$EABOT_HOME")
EOF
else
echo "[harden] 6/7  ea-mail-mediator drop-in: skipped (no unit)"
fi

if [ "$UPDATER" = 1 ]; then
echo "[harden] 6b/7 the update timer (tacitus-update.timer, hourly)"
# The updater runs as the owner with the ea group, as the bot does: it
# writes the release directory, the owner-private status and, as ea-bot,
# the EA's copy of the status. It restarts the bot through the grant
# below, the one the deploy hook uses.
cat > /etc/systemd/system/tacitus-update.service <<UNIT
# Written by deploy/harden-permissions.sh: scripts/update.py installs the
# newest signed release on the channel (docs/updates.md).
[Unit]
Description=Tacitus updater (signed releases)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$OWNER
Group=$OWNER_GROUP
SupplementaryGroups=ea
Environment=HOME=$OWNER_HOME
ExecStart=$VENV_PY $LIVE/scripts/update.py run
Nice=10
TimeoutStartSec=45min
UNIT
cat > /etc/systemd/system/tacitus-update.timer <<UNIT
# Written by deploy/harden-permissions.sh.
[Unit]
Description=Tacitus updater, hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=55min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
chmod 644 /etc/systemd/system/tacitus-update.service /etc/systemd/system/tacitus-update.timer
systemctl daemon-reload
if systemctl enable --now tacitus-update.timer >/dev/null 2>&1; then
    echo "  enabled: systemctl list-timers tacitus-update.timer; each run is in journalctl -u tacitus-update"
else
    echo "  WARNING: could not enable tacitus-update.timer" >&2
fi
else
echo "[harden] 6b/7 the update timer: skipped (updates off; the post-commit hook deploys this tree)"
fi

UNITS=""
[ "$HAS_BOT" = 1 ] && UNITS="ea-chat-bot"
[ "$HAS_MEDIATOR" = 1 ] && UNITS="${UNITS:+$UNITS }ea-mail-mediator"
if [ -z "$UNITS" ]; then
    echo "[harden] 7/7  no unit to restart"
    echo "[harden] done. The code tree is at $LIVE and the owner-private files are seeded; no service runs on this install."
    echo "[harden] then run: $VENV_PY $REPO_DIR/scripts/ea-doctor.py"
    exit 0
fi
echo "[harden] 7/7  reload and restart"
systemctl daemon-reload
if [ "$BOT_NEW" = 1 ]; then
    systemctl enable ea-chat-bot.service
fi
if [ "$HAS_BOT" = 1 ] && [ ! -e /etc/sudoers.d/ea-chat-bot ]; then
    # The deploy hook restarts the bot after it syncs $LIVE; this command only.
    SYSTEMCTL="$(command -v systemctl)"
    echo "$OWNER ALL=(root) NOPASSWD: $SYSTEMCTL restart ea-chat-bot.service" > /etc/sudoers.d/ea-chat-bot.tmp
    chmod 440 /etc/sudoers.d/ea-chat-bot.tmp
    if visudo -cf /etc/sudoers.d/ea-chat-bot.tmp >/dev/null; then
        mv -f /etc/sudoers.d/ea-chat-bot.tmp /etc/sudoers.d/ea-chat-bot
        echo "  restart grant for the deploy hook: /etc/sudoers.d/ea-chat-bot"
    else
        rm -f /etc/sudoers.d/ea-chat-bot.tmp
        echo "  WARNING: the restart grant did not pass visudo; the deploy hook will ask for a manual restart" >&2
    fi
fi
[ "$HAS_BOT" = 1 ] && systemctl restart ea-chat-bot.service
[ "$HAS_MEDIATOR" = 1 ] && { systemctl restart ea-mail-mediator.service 2>/dev/null || true; }

# The bot connects to the chat server on start (retries with backoff), so it
# can sit in "activating" for up to a minute; poll rather than snap-judge.
# A Type=simple unit reads "active" the moment its process starts, also when
# that process then exits on a missing setting and Restart= brings it back,
# so a unit counts as up only when the same process stays active for 15s.
ok=1
for svc in $UNITS; do
    state=""
    for _ in $(seq 1 30); do
        state="$(systemctl is-active "$svc" 2>/dev/null || true)"
        [ "$state" = active ] || [ "$state" = failed ] && break
        sleep 2
    done
    if [ "$state" = active ]; then
        pid="$(systemctl show -p MainPID --value "$svc" 2>/dev/null || true)"
        for _ in 1 2 3 4 5; do
            sleep 3
            now="$(systemctl is-active "$svc" 2>/dev/null || true)"
            if [ "$now" != active ] || [ "$(systemctl show -p MainPID --value "$svc" 2>/dev/null)" != "$pid" ]; then
                state="restarting"
                break
            fi
        done
    fi
    if [ "$state" != active ]; then
        echo "  $svc did not stay up; its last log lines:" >&2
        journalctl -u "$svc" -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/    /' >&2 || true
    fi
    exe="$(systemctl show -p ExecStart --value "$svc" 2>/dev/null | grep -o "$LIVE[^ ;]*" | head -1 || true)"
    envs="$(systemctl show -p EnvironmentFiles --value "$svc" 2>/dev/null | grep -oE '^/[^ ]+' | tr '\n' ' ' || true)"
    printf '  %-18s %s  exec=%s  env=%s\n' "$svc" "$state" "${exe:-?}" "${envs:-none}"
    [ "$state" = active ] && [ -n "$exe" ] || ok=0
    if [ "$svc" = ea-mail-mediator ]; then
        sandbox="$(systemctl show -p ProtectSystem --value "$svc" 2>/dev/null || true)"
        echo "  $svc sandbox: ProtectSystem=${sandbox:-?}"
        [ "$sandbox" = strict ] || { ok=0; echo "  $svc runs without its sandbox" >&2; }
    fi
    case " $envs " in *" $EABOT_HOME/"*) ok=0; echo "  $svc still loads an environment file in ea-bot space" >&2 ;; esac
done

if [ "$ok" = 1 ]; then
    echo "[harden] done. The services ($UNITS) run from $LIVE with owner-private environment files only."
    [ "$HAS_BOT" = 0 ] || echo "[harden] confirm the chat values printed in step 3."
    echo "[harden] then run: $VENV_PY $REPO_DIR/scripts/ea-doctor.py"
    echo "[harden] revert: rm the two .d/harden.conf files, systemctl daemon-reload, restart."
    [ "$BOT_RETIRE" = 0 ] || echo "[harden] the bot is not hardened yet: retire its user unit (above) and run this script again."
else
    echo "[harden] WARNING: a service is not active on the new path (its log lines are above). The bot needs its chat settings: docs/SETUP.md \"Zulip server\" or \"Slack\"." >&2
    exit 1
fi
