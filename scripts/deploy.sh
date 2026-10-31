#!/bin/bash
# Deploy a commit to the EA user's copy, /home/ea-bot/ea-system.
#
#   scripts/deploy.sh                       # this checkout's HEAD
#   scripts/deploy.sh --from REPO --ref REF [--expect SHA] [--to DIR]
#   scripts/deploy.sh --local --ref SHA [--expect SHA] [--to DIR]
#
# With no option this is the standalone version of Step 3 of
# scripts/install/setup-ea-user.sh, to run after a commit (or any time the
# deployed copy needs a refresh). The updater (scripts/update.py) names the
# repository and ref it verified (--from its cache, --ref refs/tags/vX.Y.Z)
# and the commit the copy must end at (--expect); a rollback resets to a
# commit the copy already holds (--local). --to names another copy (the
# macOS home, a test). It runs as the source-tree owner, with no sudo.
#
# Note: `git fetch` only carries committed history. Uncommitted changes in
# the source checkout will NOT be deployed. Commit first, then deploy.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST=/home/ea-bot/ea-system
REF=HEAD
EXPECT=""
LOCAL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --from) SRC="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --expect) EXPECT="$2"; shift 2 ;;
        --to) DST="$2"; shift 2 ;;
        --local) LOCAL=1; shift ;;
        *) echo "usage: scripts/deploy.sh [--from REPO] [--ref REF] [--expect SHA] [--to DIR] [--local]" >&2; exit 2 ;;
    esac
done
# The name as the check below compares it: the parent with its links
# resolved (a temp directory, a macOS home under /private).
DST="$(cd -P -- "$(dirname -- "$DST")" 2>/dev/null && pwd -P)/$(basename -- "$DST")"

if [ ! -d "$DST/.git" ]; then
    echo "ERROR: $DST is not a git repo. Run scripts/install/setup-ea-user.sh first to bootstrap." >&2
    exit 1
fi

# $DST sits in ea-bot's home, so ea-bot can swap it for its own tree or a
# symlink, and git run here as the owner would then run ea-bot's config and
# hooks as the owner. So the script enters $DST first and checks the
# directory it is in: reached without a symlink, and it and its .git real
# directories owned by this user that no one else can write. Every command
# below then works on that entered directory, never on the name.
cd -P -- "$DST"
tree_ok=1
[ "$(pwd -P)" = "$DST" ] || tree_ok=0
for d in . .git; do
    if [ ! -d "$d" ] || [ -L "$d" ] || [ ! -O "$d" ] \
            || [ $(( 0$(stat -c %a "$d" 2>/dev/null || stat -f %Lp "$d") & 022 )) -ne 0 ]; then
        tree_ok=0
    fi
done
if [ "$tree_ok" != 1 ]; then
    echo "ERROR: $DST and its .git must be real directories (not symlinks), owned by $(id -un) and writable by no one else. Not deploying." >&2
    exit 1
fi
# Only this copy's own repository, whatever the caller's environment says.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_PREFIX
dst_git() { git --git-dir=.git --work-tree=. "$@"; }

if [ "$LOCAL" = 1 ]; then
    echo "Resetting $DST to $REF"
    dst_git reset -q --hard "$REF"
else
    echo "Deploying $SRC $REF -> $DST"
    dst_git fetch -q "$SRC" "$REF"
    dst_git reset -q --hard FETCH_HEAD
fi
# The developer guide stays out of ea-bot's tree: Claude Code would load
# .claude/CLAUDE.md into every EA turn.
dst_git sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md' >/dev/null 2>&1 || echo "warning: .claude/CLAUDE.md may be in $DST (sparse-checkout failed)"

if [ -n "$EXPECT" ]; then
    HEAD_NOW="$(dst_git rev-parse HEAD)"
    case "$HEAD_NOW" in
        "$EXPECT"*) ;;
        *) echo "ERROR: $DST is at $HEAD_NOW, not $EXPECT" >&2; exit 1 ;;
    esac
fi

# Any files git wrote with the default umask may be the owner's group; re-apply ea
# group readability so the ea-bot Unix user can read them.
chgrp -R ea . 2>/dev/null || echo "warning: could not give $DST to group ea"
chmod -R g+rX .
chmod -R g-w .

echo "Deploy complete. HEAD is now $(dst_git rev-parse --short HEAD) ($(dst_git log -1 --format=%s))"
