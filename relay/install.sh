#!/bin/bash
# Install or update the Tacitus web chat relay on a public host. Run as root.
# Idempotent: run it again with the next release's commit to update.
#
# Usage: install.sh --commit SHA [--install-id ID]... [--with-app | --without-app]
#                   [--no-update-timer] [--repo URL]
#        install.sh --tag vX.Y.Z [--expect-commit SHA] [...]
#   --commit SHA      the tacitus commit to install, all 40 hex characters; the
#                     clone is checked against it. Never the tip of a branch: the
#                     repository takes outside pull requests.
#   --tag TAG         a release tag instead, taken only when `git verify-tag`
#                     passes against the release key (SSH public keys, one per
#                     line) in /etc/tacitus-relay/release-key.pub, or else in
#                     /var/lib/tacitus-relay/release-key.pub, the key the updater
#                     pinned (ssh-ed25519 keys only). The tag must carry its own
#                     name inside (an older signed tag moved to a new name does
#                     not), and its commit must be in main's history.
#   --expect-commit SHA with --tag: the commit the tag must name. The updater
#                     runs a new release's own install.sh with --tag and the
#                     commit it verified.
#   --install-id ID   add this install id to the allow file (repeatable)
#   --with-app        also serve the web app (--static), for phones paired with
#                     "Phone not on your network". Only on a hostname of the relay's
#                     own: any other page on that origin could use a paired phone's
#                     key. Only at the release the bots it serves run: a phone takes
#                     the copy only when it matches its bot's signed app bundle
#                     (docs/webchat-relay.md, Running the relay).
#   --without-app     stop serving the web app. With neither flag the relay keeps
#                     what its unit does now, so an update keeps the choice.
#   --no-update-timer do not install the updater, and remove it when installed.
#                     By default the script writes tacitus-relay-update.service
#                     and tacitus-relay-update.timer (the text of the files in
#                     relay/) and enables the timer: each hour it installs the
#                     newest signed release (docs/updates.md, The relay host).
#   --repo URL        the repository to clone (https; default the public one)
#
# The relay is one standard-library Python process on 127.0.0.1:8471, under a
# unit this script writes (it never installs one from the clone). TLS stays
# with the host's web server: put relay/snippets/nginx-http.conf in its http
# block and relay/snippets/nginx.conf in the HTTPS server block, then
# `nginx -t` and reload. The relay keeps no state worth backing up; the allow
# file is the only input.
set -euo pipefail

REPO="https://github.com/arpg/tacitus.git"
BASE=/opt/tacitus-relay
ETC=/etc/tacitus-relay
CLONE=$BASE/src
UNIT=/etc/systemd/system/tacitus-relay.service
KEY=$ETC/release-key.pub
# The updater's pinned key (scripts/update.py relay), when $ETC holds none.
PINNED_KEY=/var/lib/tacitus-relay/release-key.pub
UPDATER=/etc/systemd/system/tacitus-relay-update
COMMIT=""
TAG=""
EXPECT=""
WITH_APP=""
UPDATE_TIMER=1
IDS=()
die() { echo "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) COMMIT="${2:-}"; shift 2 ;;
        --tag) TAG="${2:-}"; shift 2 ;;
        --expect-commit) EXPECT="${2:-}"; shift 2 ;;
        --install-id) IDS+=("${2:-}"); shift 2 ;;
        --with-app) WITH_APP=1; shift ;;
        --without-app) WITH_APP=0; shift ;;
        --no-update-timer) UPDATE_TIMER=0; shift ;;
        --repo) REPO="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --- what to install: checked before anything changes ----------------------
if [ -n "$COMMIT" ] && [ -n "$TAG" ]; then
    die "give --commit or --tag, not both"
elif [ -n "$EXPECT" ] && { [ -z "$TAG" ] || ! [[ "$EXPECT" =~ ^[0-9a-f]{40}$ ]]; }; then
    die "--expect-commit takes a full commit id (40 hex characters), with --tag"
elif [ -n "$COMMIT" ]; then
    [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "--commit takes a full commit id (40 hex characters)"
elif [ -n "$TAG" ]; then
    [[ "$TAG" =~ ^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$ ]] || die "--tag takes a release tag such as v1.2.0"
    [ -f "$KEY" ] || KEY=$PINNED_KEY
    [ -f "$KEY" ] || die "--tag needs the release key in $ETC/release-key.pub (copy the repository's .release-key.pub there), or use --commit"
    [[ "$(head -c 4096 "$KEY")" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+ ]] \
        || die "$KEY is not an ssh-ed25519 public key"
else
    die "give --commit SHA: the relay installs a commit you name, never the tip of a branch"
fi
for id in "${IDS[@]:-}"; do
    [ -z "$id" ] || [[ "$id" =~ ^[a-z2-7]{26}$ ]] || die "not an install id (26 characters a-z, 2-7): $id"
done
[[ "$REPO" =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$ ]] || die "--repo takes an https URL"

# >>> verify_tag (tests/test_relay_install.py runs this function alone)
# The commit of tag $2 in the clone $1, printed, when the release key in $3
# (its ssh-ed25519 lines) signed the tag, the tag object carries the name
# $2 itself (an older signed tag moved to a new name carries its old one),
# its commit is $4 when given, and that commit is in main's history
# (refs/remotes/origin/main). Else a reason on stderr and status 1.
verify_tag() {
    local dir="$1" tag="$2" key="$3" expect="$4" head commit
    grep -E '^ssh-ed25519 [A-Za-z0-9+/=]+' "$key" \
        | sed 's/^/tacitus-release namespaces="git" /' > "$dir/.git/allowed_signers" || true
    [ -s "$dir/.git/allowed_signers" ] || { echo "$key holds no ssh-ed25519 key" >&2; return 1; }
    git -C "$dir" -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$dir/.git/allowed_signers" \
        verify-tag "$tag" >/dev/null 2>&1 \
        || { echo "tag $tag is not signed by the release key in $key" >&2; return 1; }
    head="$(git -C "$dir" cat-file tag "refs/tags/$tag" | sed -n '1,/^$/p')"
    [ "$(sed -n 's/^tag //p' <<<"$head")" = "$tag" ] && [ "$(sed -n 's/^type //p' <<<"$head")" = commit ] \
        || { echo "tag $tag carries another name inside: an older release moved" >&2; return 1; }
    commit="$(git -C "$dir" rev-parse "refs/tags/$tag^{commit}")"
    [ -z "$expect" ] || [ "$commit" = "$expect" ] \
        || { echo "tag $tag names $commit, not $expect" >&2; return 1; }
    git -C "$dir" merge-base --is-ancestor "$commit" refs/remotes/origin/main 2>/dev/null \
        || { echo "$commit is not in main's history" >&2; return 1; }
    echo "$commit"
}
# <<< verify_tag

[ "$(id -u)" = 0 ] || die "run as root"
# With neither --with-app nor --without-app, keep what the unit serves now.
if [ -z "$WITH_APP" ]; then
    if grep -q -- " --static " "$UNIT" 2>/dev/null; then WITH_APP=1; else WITH_APP=0; fi
fi
command -v git >/dev/null || die "git is needed"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 is needed"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 3.9 or newer is needed"

# --- user and directories ----------------------------------------------------
id tacitus-relay >/dev/null 2>&1 \
    || useradd --system --home-dir $BASE --shell /usr/sbin/nologin tacitus-relay
mkdir -p $BASE $ETC
chown root:root $BASE $ETC
chmod 755 $BASE $ETC

# --- the code: the named commit, or a tag the release key signed -------------
[ -d $CLONE/.git ] || { rm -rf $CLONE; git init -q $CLONE; }
git -C $CLONE remote remove origin 2>/dev/null || true
git -C $CLONE remote add origin "$REPO"
if [ -n "$COMMIT" ]; then
    git -C $CLONE fetch -q --depth 1 origin "$COMMIT"
else
    UNSHALLOW=()
    [ ! -f $CLONE/.git/shallow ] || UNSHALLOW=(--unshallow)
    git -C $CLONE fetch -q ${UNSHALLOW[@]+"${UNSHALLOW[@]}"} --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG" \
        "+refs/heads/main:refs/remotes/origin/main"
    COMMIT="$(verify_tag $CLONE "$TAG" "$KEY" "$EXPECT")" || die "release $TAG refused"
fi
git -C $CLONE checkout -q --detach "$COMMIT"
[ "$(git -C $CLONE rev-parse HEAD)" = "$COMMIT" ] || die "the clone is not at $COMMIT"

# Each file is copied beside its place and moved in, so a failed copy leaves
# the running version whole.
install -m 755 $CLONE/relay/relay_server.py $BASE/relay_server.py.new
mv -f $BASE/relay_server.py.new $BASE/relay_server.py
STATIC=""
if [ $WITH_APP = 1 ]; then
    rm -rf $BASE/webui.new $BASE/webui.old
    cp -r $CLONE/botcore/botcore/chat/webui $BASE/webui.new
    chmod -R a+rX $BASE/webui.new
    [ ! -d $BASE/webui ] || mv $BASE/webui $BASE/webui.old
    mv $BASE/webui.new $BASE/webui
    rm -rf $BASE/webui.old
    STATIC=" --static $BASE/webui"
else
    rm -rf $BASE/webui  # this relay serves no app
fi

# --- the allow file: one install id per line -----------------------------------
touch $ETC/installs.txt
for id in "${IDS[@]:-}"; do
    [ -n "$id" ] || continue
    grep -qx "$id" $ETC/installs.txt || echo "$id" >> $ETC/installs.txt
done
chown root:tacitus-relay $ETC/installs.txt
chmod 640 $ETC/installs.txt

# --- the unit: written here, never taken from the clone -------------------------
# The relay talks only to the proxy on this host. Its own limits hold well
# under MemoryMax (512 MiB queued at most); TasksMax and LimitNOFILE leave
# room for its 2,048 connections.
cat > $UNIT.new <<UNIT
[Unit]
Description=Tacitus web chat relay (frames only, never plaintext)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tacitus-relay
Group=tacitus-relay
ExecStart=$PY -I $BASE/relay_server.py --bind 127.0.0.1 --port 8471 --allow-file $ETC/installs.txt$STATIC
Restart=always
RestartSec=5
IPAddressDeny=any
IPAddressAllow=localhost
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=768M
TasksMax=2200
LimitNOFILE=4096
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
ReadOnlyPaths=$BASE $ETC
PrivateTmp=yes
PrivateDevices=yes
PrivateUsers=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
RemoveIPC=yes
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 $UNIT.new
mv -f $UNIT.new $UNIT
systemctl daemon-reload
systemctl enable --now tacitus-relay.service
systemctl restart tacitus-relay.service
sleep 1
systemctl --no-pager --lines=0 status tacitus-relay.service | grep -E "●|Active:" || true
# The updater takes a non-zero exit as a failed release and switches back.
HEALTHY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if "$PY" -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8471/healthz", timeout=5).read().decode())' 2>/dev/null; then
        HEALTHY=1
        break
    fi
    sleep 1
done
[ $HEALTHY = 1 ] || echo "the relay did not answer /healthz" >&2

# --- the updater: written here, the text of relay/tacitus-relay-update.* ------
if [ $UPDATE_TIMER = 1 ]; then
    cat > $UPDATER.service.new <<UNIT
# The relay host's updater (docs/updates.md, "The relay host"): the newest
# signed release on the channel in /etc/tacitus-relay/update-channel
# (latest when absent), installed by that release's own
# relay/install.sh --tag. relay/install.sh writes this unit and the timer
# beside it into /etc/systemd/system (--no-update-timer leaves them out) and
# enables the timer.
[Unit]
Description=Tacitus relay updater (signed releases)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PY -I $CLONE/scripts/update.py relay
Nice=10
TimeoutStartSec=30min
UNIT
    cat > $UPDATER.timer.new <<'UNIT'
# Hourly, at a random minute, so relay hosts do not all fetch at once.
[Unit]
Description=Tacitus relay updater, hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=55min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    chmod 644 $UPDATER.service.new $UPDATER.timer.new
    mv -f $UPDATER.service.new $UPDATER.service
    mv -f $UPDATER.timer.new $UPDATER.timer
    systemctl daemon-reload
    systemctl enable --now tacitus-relay-update.timer
    UPDATES="on; each signed release installs within the hour (journalctl -u tacitus-relay-update)"
else
    systemctl disable --now tacitus-relay-update.timer 2>/dev/null || true
    rm -f $UPDATER.service $UPDATER.timer
    systemctl daemon-reload
    UPDATES="off (--no-update-timer); run this script again at each release"
fi

echo
echo "Installed $COMMIT under $BASE$([ $WITH_APP = 1 ] && echo ', with the web app' || true)."
echo "Allowed installs: $(grep -c . $ETC/installs.txt || true)."
echo "Updates: $UPDATES."
echo "Monitor http://127.0.0.1:8471/healthz on this host; through the proxy it answers 404."
echo "Now put relay/snippets/nginx-http.conf in the http block and relay/snippets/nginx.conf"
echo "in the HTTPS server block, run nginx -t, and reload nginx."
[ $HEALTHY = 1 ] || exit 1
