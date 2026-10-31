#!/bin/bash
# Install or update the Tacitus web chat relay on a public host. Run as root.
# Idempotent: run it again after a release to update the relay and the app.
#
# Usage: install.sh [--install-id ID] [--ref main]
#   --install-id ID   add this install id to the allow file (repeatable)
#   --ref REF         the tacitus branch or tag to install (default main)
#
# The relay is one standard-library Python process on 127.0.0.1:8471. TLS
# stays with the host's web server: add relay/snippets/nginx.conf (or the
# matching block for another server) to its HTTPS vhost and reload it. The
# relay keeps no state worth backing up; the allow file is the only input.
set -euo pipefail

REPO_HTTPS="https://github.com/arpg/tacitus.git"
BASE=/opt/tacitus-relay
ETC=/etc/tacitus-relay
CLONE=$BASE/src
REF=main
IDS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --install-id) IDS+=("$2"); shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
command -v git >/dev/null || { echo "git is needed" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "python3 3.9 or newer is needed" >&2; exit 1; }

# --- user and directories ----------------------------------------------------
id tacitus-relay >/dev/null 2>&1 \
    || useradd --system --home-dir $BASE --shell /usr/sbin/nologin tacitus-relay
mkdir -p $BASE $ETC
chown root:root $BASE
chmod 755 $BASE

# --- the code: a shallow clone of the public repository -----------------------
if [ ! -d $CLONE/.git ]; then
    git clone -q --depth 1 --branch "$REF" "$REPO_HTTPS" $CLONE
else
    git -C $CLONE fetch -q --depth 1 origin "$REF"
    git -C $CLONE checkout -q FETCH_HEAD
fi
install -m 755 $CLONE/relay/relay_server.py $BASE/relay_server.py
rm -rf $BASE/webui
cp -r $CLONE/botcore/botcore/chat/webui $BASE/webui
chmod -R a+rX $BASE/webui

# --- the allow file: one install id per line -----------------------------------
touch $ETC/installs.txt
for id in "${IDS[@]:-}"; do
    [ -n "$id" ] || continue
    grep -qx "$id" $ETC/installs.txt || echo "$id" >> $ETC/installs.txt
done
chown root:tacitus-relay $ETC/installs.txt
chmod 640 $ETC/installs.txt

# --- the unit -----------------------------------------------------------------
install -m 644 $CLONE/relay/tacitus-relay.service /etc/systemd/system/tacitus-relay.service
systemctl daemon-reload
systemctl enable --now tacitus-relay.service
systemctl restart tacitus-relay.service
sleep 1
systemctl --no-pager --lines=0 status tacitus-relay.service | grep -E "●|Active:"
curl -fsS http://127.0.0.1:8471/healthz || echo "the relay did not answer /healthz" >&2

echo
echo "Installed $(git -C $CLONE rev-parse --short HEAD) under $BASE."
echo "Allowed installs: $(grep -c . $ETC/installs.txt)."
echo "Now add relay/snippets/nginx.conf to the HTTPS vhost, reload it, and check https://<host>/relay/healthz."
