#!/bin/bash
# Deploy the current HEAD of the owner's source checkout to /home/ea-bot/ea-system.
#
# This is the standalone version of Step 3 of scripts/install/setup-ea-user.sh, suitable to
# run after every commit (or any time the deployed copy needs to be refreshed).
# It runs as the source-tree owner, with no sudo.
#
# Note: `git fetch` only carries committed history. Uncommitted changes in
# the source checkout will NOT be deployed. Commit first, then deploy.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST=/home/ea-bot/ea-system

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

echo "Deploying $SRC -> $DST"
dst_git fetch "$SRC" HEAD
dst_git reset --hard FETCH_HEAD
# The developer guide stays out of ea-bot's tree: Claude Code would load
# .claude/CLAUDE.md into every EA turn.
dst_git sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md' >/dev/null 2>&1 || echo "warning: .claude/CLAUDE.md may be in $DST (sparse-checkout failed)"

# Any files git wrote with the default umask may be the owner's group; re-apply ea
# group readability so the ea-bot Unix user can read them.
chgrp -R ea .
chmod -R g+rX .
chmod -R g-w .

echo "Deploy complete. HEAD is now $(dst_git rev-parse --short HEAD) ($(dst_git log -1 --format=%s))"
