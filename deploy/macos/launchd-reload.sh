#!/bin/bash
# launchd-reload.sh: unload a LaunchDaemon, wait for it to be gone, load it again.
#
#   launchd-reload.sh <label> <plist> [--wait S] [--poll S] [--tries N]
#
# `launchctl bootout` returns as soon as launchd has sent the job its
# SIGTERM. The process can stay for a while (the chat bot drains in-flight
# work for up to 80 s), and while the old instance is still being torn
# down `launchctl bootstrap` of the same label fails with
# "Bootstrap failed: 5: Input/output error". A bootout followed at once
# by a bootstrap therefore leaves the job unloaded whenever the old
# process does not exit instantly. This script waits until
# `launchctl print system/<label>` no longer knows the job (up to --wait
# seconds, default 90, polled every --poll seconds, default 1), then
# bootstraps, retrying --tries times (default 5, 2 s apart).
#
# Exit 0 when the job is loaded. Exit 1 when it is not, after printing
# the bootstrap command to run by hand once the old process has exited.
# Root only, as the installer that calls it.
set -euo pipefail

usage() { echo "usage: $0 <label> <plist> [--wait S] [--poll S] [--tries N]" >&2; exit 2; }
[ $# -ge 2 ] || usage
LABEL="$1"; PLIST="$2"; shift 2
WAIT=90; POLL=1; TRIES=5
while [ $# -gt 0 ]; do
    case "$1" in
        --wait)  WAIT="${2:?}"; shift 2 ;;
        --poll)  POLL="${2:?}"; shift 2 ;;
        --tries) TRIES="${2:?}"; shift 2 ;;
        *) usage ;;
    esac
done
[ -f "$PLIST" ] || { echo "ERROR: $PLIST is not a file" >&2; exit 1; }

job_known() { launchctl print "system/$LABEL" >/dev/null 2>&1; }

launchctl bootout "system/$LABEL" 2>/dev/null || true

# Wait for launchd to forget the job: until then a bootstrap is refused.
deadline=$(( $(date +%s) + WAIT ))
waited=0
while job_known; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "  $LABEL: the old instance is still being torn down after ${WAIT}s" >&2
        waited=0   # it has not exited: the line below would be false
        break
    fi
    sleep "$POLL"; waited=1
done
[ "$waited" = 1 ] && echo "  $LABEL: the old instance has exited"

n=0
while :; do
    n=$((n + 1))
    if launchctl bootstrap system "$PLIST"; then
        exit 0
    fi
    if [ "$n" -ge "$TRIES" ]; then break; fi
    echo "  $LABEL: bootstrap failed (attempt $n of $TRIES); trying again in 2s" >&2
    sleep 2
done
echo "ERROR: $LABEL is NOT loaded. When the old process has exited, run by hand:" >&2
echo "    sudo launchctl bootstrap system $PLIST" >&2
exit 1
