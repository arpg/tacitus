#!/usr/bin/env bash
# webchat-backup.sh: a snapshot of the web chat's own data in the owner-private
# directory, from the owner's crontab each night (docs/SETUP.md, Web chat).
#
#   scripts/webchat-backup.sh                  today's snapshot; those past 7 days go
#   scripts/webchat-backup.sh --quiet          the same with no line on success
#   scripts/webchat-backup.sh --label cutover  a snapshot the rotation never removes
#   scripts/webchat-backup.sh --list           the snapshots there are
#   scripts/webchat-backup.sh --restore <name> the steps that put one back; changes nothing
#
# Each snapshot is <private dir>/backups/webchat/<date>[-<label>]/, mode 0700,
# its files 0600: the store (webchat.sqlite, copied through SQLite's backup API,
# so a store the bot is writing comes out whole), the uploads beside it
# (webchat-files/), the owner's state overrides (thread-state-labels.jsonl), the
# push key and the owner's web message ids, the relay key file, the pairing
# token, chat.json, the room record (owner-rooms.json), the card registry
# (chat-cards.json) and the closure labels (evals/). MANIFEST.tsv names where
# each came from. A second run on one day replaces that day's snapshot. The
# paths follow the web block of chat.json (store_path, pair_token_file,
# relay.key_file) as the bot reads them. Run it as the owner: everything it
# reads and writes is in the owner-private directory.
set -euo pipefail
umask 077

PRIV="${EA_PRIVATE_DIR:-$HOME/.ea-private}"
OUT_ROOT="$PRIV/backups/webchat"
KEEP_DAYS="${WEBCHAT_BACKUP_KEEP_DAYS:-7}"
QUIET=0
LABEL=""
MODE=backup
RESTORE=""

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --quiet|-q) QUIET=1 ;;
        --label) LABEL="${2:-}"; shift ;;
        --list) MODE=list ;;
        --restore) MODE=restore; RESTORE="${2:-}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "webchat-backup: unknown argument $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$LABEL" ] && ! [[ "$LABEL" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
    echo "webchat-backup: a label is lower-case letters, digits and dashes" >&2
    exit 2
fi

# The store, the pairing token and the relay key file, one per line, as the
# bot finds them: the web block of chat.json when the file is the owner's and
# no one else can write it, else the defaults beside the store.
PY_PATHS='
import json, os, stat, sys
from pathlib import Path
priv = Path(sys.argv[1])
web = {}
try:
    p = priv / "chat.json"
    st = os.lstat(p)
    if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o022:
        block = json.loads(p.read_text())
        if isinstance(block, dict) and isinstance(block.get("web"), dict):
            web = block["web"]
except (OSError, ValueError):
    pass
store = Path(str(web.get("store_path") or "") or priv / "webchat.sqlite")
relay = web.get("relay") if isinstance(web.get("relay"), dict) else {}
print(store)
print(str(web.get("pair_token_file") or "") or store.parent / "webchat-pair.token")
print(str(relay.get("key_file") or "") or store.parent / "webchat-relay.json")
'
# A consistent copy of a live SQLite file: the backup API reads it under
# SQLite's own locks, page by page, so a write in between is never half in.
PY_SQLITE='
import os, sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA quick_check").fetchone()[0]
dst.close()
src.close()
os.chmod(sys.argv[2], 0o600)
sys.exit(0 if ok == "ok" else "the copy of the store fails its check: " + str(ok))
'

{ read -r STORE; read -r TOKEN; read -r RELAY_KEY; } < <(python3 -c "$PY_PATHS" "$PRIV")
STORE_DIR="$(dirname "$STORE")"

if [ "$MODE" = list ]; then
    [ -d "$OUT_ROOT" ] || { echo "no web chat backups in $OUT_ROOT"; exit 0; }
    for d in "$OUT_ROOT"/*/; do
        [ -f "$d/MANIFEST.tsv" ] || continue
        printf '%s\t%s\n' "$(basename "$d")" "$(du -sh "$d" | cut -f1)"
    done
    exit 0
fi

if [ "$MODE" = restore ]; then
    SNAP="$OUT_ROOT/$RESTORE"
    if [ -z "$RESTORE" ] || [[ "$RESTORE" == */* ]] || [ ! -f "$SNAP/MANIFEST.tsv" ]; then
        echo "webchat-backup: no snapshot named '$RESTORE' in $OUT_ROOT (--list shows them)" >&2
        exit 1
    fi
    ASIDE="$PRIV/webchat-before-restore-$(date +%Y%m%d-%H%M%S)"
    if [ "$(uname -s)" = Darwin ]; then
        STOP="sudo launchctl bootout system/org.ea-system.ea-chat-bot"
        START="sudo launchctl bootstrap system /Library/LaunchDaemons/org.ea-system.ea-chat-bot.plist"
    else
        STOP="sudo systemctl stop ea-chat-bot"
        START="sudo systemctl start ea-chat-bot"
    fi
    echo "# Put the web chat back as it was in $SNAP."
    echo "# Nothing has changed yet: run these lines yourself, in this order."
    echo "# 1. Stop the bot, so nothing writes the store while it is replaced"
    echo "#    (in Docker: docker compose stop bot):"
    echo "$STOP"
    echo "# 2. Move what is there now aside (keep it until the restore works):"
    echo "mkdir -m 700 '$ASIDE'"
    while IFS=$'\t' read -r item src; do
        [ -n "$item" ] || continue
        if [ "$item" = webchat.sqlite ]; then
            for side in "$src-journal" "$src-wal" "$src-shm"; do
                if [ -e "$side" ]; then echo "mv '$side' '$ASIDE/'"; fi
            done
        fi
        if [ -e "$src" ]; then echo "mv '$src' '$ASIDE/'"; fi
    done < "$SNAP/MANIFEST.tsv"
    echo "# 3. Copy the snapshot back, owner-only:"
    while IFS=$'\t' read -r item src; do
        [ -n "$item" ] || continue
        if [ -d "$SNAP/$item" ]; then
            echo "cp -pR '$SNAP/$item' '$src' && chmod -R go-rwx '$src'"
        else
            echo "cp -p '$SNAP/$item' '$src' && chmod 600 '$src'"
        fi
    done < "$SNAP/MANIFEST.tsv"
    echo "# 4. Start the bot and check the web chat's rows:"
    echo "$START"
    echo "python3 /opt/ea-system/scripts/ea-doctor.py | grep 'web chat'"
    exit 0
fi

if [ ! -f "$STORE" ]; then
    echo "webchat-backup: no web chat store at $STORE" >&2
    exit 1
fi
mkdir -p "$OUT_ROOT"
chmod 700 "$PRIV/backups" "$OUT_ROOT"
NAME="$(date +%F)${LABEL:+-$LABEL}"
DEST="$OUT_ROOT/$NAME"
TMP="$OUT_ROOT/.$NAME.tmp.$$"
rm -rf "$TMP"
mkdir -m 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

python3 -c "$PY_SQLITE" "$STORE" "$TMP/webchat.sqlite"
printf 'webchat.sqlite\t%s\n' "$STORE" > "$TMP/MANIFEST.tsv"
keep() {  # keep <source> <name in the snapshot>: a file or a directory, when it is there
    local src="$1" name="$2"
    if [ -f "$src" ] && [ ! -L "$src" ]; then
        cp -p "$src" "$TMP/$name"
        chmod 600 "$TMP/$name"
    elif [ -d "$src" ] && [ ! -L "$src" ]; then
        cp -pR "$src" "$TMP/$name"
        chmod -R go-rwx "$TMP/$name"
    else
        return 0
    fi
    printf '%s\t%s\n' "$name" "$src" >> "$TMP/MANIFEST.tsv"
}
keep "$STORE_DIR/webchat-files" webchat-files
keep "$STORE_DIR/thread-state-labels.jsonl" thread-state-labels.jsonl
keep "$STORE_DIR/webchat-owner-ids.json" webchat-owner-ids.json
keep "$STORE_DIR/webchat-vapid.json" webchat-vapid.json
keep "$RELAY_KEY" webchat-relay.json
keep "$TOKEN" webchat-pair.token
keep "$PRIV/chat.json" chat.json
keep "$PRIV/owner-rooms.json" owner-rooms.json
keep "$PRIV/chat-cards.json" chat-cards.json
keep "$PRIV/evals" evals
chmod 600 "$TMP/MANIFEST.tsv"

rm -rf "$DEST"
mv "$TMP" "$DEST"
trap - EXIT
# The rotation: dated snapshots past KEEP_DAYS go; a labelled one stays.
find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' -mtime +"$KEEP_DAYS" -exec rm -rf {} +
if [ "$QUIET" = 0 ]; then
    echo "$(date -Is 2>/dev/null || date) web chat backup written: $DEST ($(du -sh "$DEST" | cut -f1))"
fi
