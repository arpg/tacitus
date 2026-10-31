#!/bin/bash
# Install or update the Tacitus web chat relay on a public host. Run as root.
# Idempotent: run it again with the next release's commit to update.
#
# Usage: install.sh --commit SHA [--install-id ID]... [--with-app] [--repo URL]
#        install.sh --tag vX.Y.Z [...]
#   --commit SHA      the tacitus commit to install, all 40 hex characters; the
#                     clone is checked against it. Never the tip of a branch: the
#                     repository takes outside pull requests.
#   --tag TAG         a release tag instead, taken only when `git verify-tag`
#                     passes against the release key (an SSH public key) in
#                     /etc/tacitus-relay/release-key.pub. Releases are not signed
#                     yet; until they are, use --commit.
#   --install-id ID   add this install id to the allow file (repeatable)
#   --with-app        also serve the web app (--static), for phones paired with
#                     "Pair through the relay". Only on a hostname of the relay's
#                     own: any other page on that origin could use a paired phone's
#                     key. Only at the release the bots it serves run: a phone takes
#                     the copy only when it matches its bot's signed app bundle
#                     (docs/webchat-relay.md, Running the relay).
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
COMMIT=""
TAG=""
WITH_APP=0
IDS=()
die() { echo "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) COMMIT="${2:-}"; shift 2 ;;
        --tag) TAG="${2:-}"; shift 2 ;;
        --install-id) IDS+=("${2:-}"); shift 2 ;;
        --with-app) WITH_APP=1; shift ;;
        --repo) REPO="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --- what to install: checked before anything changes ----------------------
if [ -n "$COMMIT" ] && [ -n "$TAG" ]; then
    die "give --commit or --tag, not both"
elif [ -n "$COMMIT" ]; then
    [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "--commit takes a full commit id (40 hex characters)"
elif [ -n "$TAG" ]; then
    [[ "$TAG" =~ ^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$ ]] || die "--tag takes a release tag such as v1.2.0"
    [ -f "$KEY" ] || die "--tag needs the release key in $KEY; releases are not signed yet, so use --commit"
    [[ "$(head -c 4096 "$KEY")" =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\ [A-Za-z0-9+/=]+ ]] \
        || die "$KEY is not an SSH public key"
else
    die "give --commit SHA: the relay installs a commit you name, never the tip of a branch"
fi
for id in "${IDS[@]:-}"; do
    [ -z "$id" ] || [[ "$id" =~ ^[a-z2-7]{26}$ ]] || die "not an install id (26 characters a-z, 2-7): $id"
done
[[ "$REPO" =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$ ]] || die "--repo takes an https URL"

[ "$(id -u)" = 0 ] || die "run as root"
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
    git -C $CLONE fetch -q --depth 1 --no-tags origin "+refs/tags/$TAG:refs/tags/$TAG"
    printf 'tacitus-release namespaces="git" %s\n' "$(head -n 1 "$KEY")" > $CLONE/.git/allowed_signers
    git -C $CLONE -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=$CLONE/.git/allowed_signers \
        verify-tag "$TAG" || die "tag $TAG is not signed by the release key in $KEY"
    COMMIT="$(git -C $CLONE rev-parse "refs/tags/$TAG^{commit}")"
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
"$PY" -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8471/healthz", timeout=5).read().decode())' \
    || echo "the relay did not answer /healthz" >&2

echo
echo "Installed $COMMIT under $BASE$([ $WITH_APP = 1 ] && echo ', with the web app' || true)."
echo "Allowed installs: $(grep -c . $ETC/installs.txt || true)."
echo "Monitor http://127.0.0.1:8471/healthz on this host; through the proxy it answers 404."
echo "Now put relay/snippets/nginx-http.conf in the http block and relay/snippets/nginx.conf"
echo "in the HTTPS server block, run nginx -t, and reload nginx."
