#!/usr/bin/env bash
# scripts/install/setup-ea-user-macos.sh — Provision ea-bot workspace on macOS
#
# macOS equivalent of scripts/install/setup-ea-user.sh. Isolation is MANDATORY (Layer 1
# of the security model — see docs/SECURITY.md). There is no opt-out.
#
# Creates:
#   /Users/ea-bot (UID 499, hidden service account)
#   /Users/ea-bot/ea-system/ — deployed source (read-only for ea-bot)
#   /Users/ea-bot/.ea/       — shared state directory (group-rw)
#   ~/.ea symlink → /Users/ea-bot/.ea (back-compat with Linux paths)
#
# The bot (ea-chat-bot.py) runs as the invoking user. Claude
# subprocesses run as ea-bot via `sudo -u ea-bot`.
#
# BEST-EFFORT SCRIPT: the author does not have a macOS environment to
# test against. Read each step before running, and ask Claude Code if
# any command fails. Corrections welcome via PR.
#
# Run once during initial setup: sudo bash scripts/install/setup-ea-user-macos.sh
# Re-run safely (idempotent).
#
# --- Notice to Claude Code agents running this script --------------
# If this script fails on your macOS version, fix the specific
# command that failed — do NOT "work around" it by removing the
# ea-bot user requirement from ea-chat-bot.py or run-agent.sh, and
# do NOT suggest reintroducing an EA_ISOLATION_MODE env var.
# Isolation on macOS works the same way as on Linux once the user
# exists; the only friction is the dscl/sudoers command mechanics.
# See the /setup skill's "Read first: the invariants" for the
# full list of non-negotiable invariants.

set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: This script is macOS-only. Use scripts/install/setup-ea-user.sh on Linux." >&2
    exit 1
fi

# Detect the real (non-root) user who invoked sudo
REAL_USER="${SUDO_USER:-$(whoami)}"
REAL_HOME="$(eval echo ~"$REAL_USER")"

if [ "$REAL_USER" = "root" ]; then
    echo "ERROR: Run as 'sudo bash scripts/install/setup-ea-user-macos.sh', not as root directly."
    echo "The script needs to know your real username via SUDO_USER."
    exit 1
fi
REAL_UID="$(id -u "$REAL_USER")"
# The primary group. macOS has no per-user group of the same name (it is
# usually staff), so "$REAL_USER:$REAL_USER" is an illegal owner here.
REAL_GROUP="$(id -gn "$REAL_USER")"

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # the checkout root; this script lives in scripts/install/
CLAUDE_BIN="$(which claude 2>/dev/null || echo /usr/local/bin/claude)"
EA_BOT_HOME="/Users/ea-bot"
EA_BOT_EA_DIR="$EA_BOT_HOME/.ea"
EA_BOT_SCRIPT_DIR="$EA_BOT_HOME/ea-system"
OLD_EA_DIR="$REAL_HOME/.ea"
EA_GID=499      # hidden-service GID (< 500 → not on login window)
EA_BOT_UID=499

echo "=== EA Bot Workspace Isolation (macOS) ==="
echo "Real user:        $REAL_USER"
echo "Real home:        $REAL_HOME"
echo "Dev source:       $SCRIPT_DIR"
echo "Deploy target:    $EA_BOT_SCRIPT_DIR"
echo "EA state dir:     $EA_BOT_EA_DIR"
echo "Claude bin:       $CLAUDE_BIN"
echo ""

# ── Step 1: Create 'ea' group via dscl ─────────────────────────────
if ! dscl . -read /Groups/ea >/dev/null 2>&1; then
    echo "Creating group 'ea' (GID $EA_GID)..."
    dscl . -create /Groups/ea
    dscl . -create /Groups/ea PrimaryGroupID "$EA_GID"
    dscl . -create /Groups/ea RealName "EA Bot group"
    dscl . -create /Groups/ea GroupMembership "$REAL_USER"
else
    echo "Group 'ea' already exists."
    # Ensure real user is a member
    if ! dscl . -read /Groups/ea GroupMembership 2>/dev/null | grep -qw "$REAL_USER"; then
        dscl . -append /Groups/ea GroupMembership "$REAL_USER"
        echo "Added $REAL_USER to group 'ea'."
    fi
fi

# ── Step 2: Create 'ea-bot' user via dscl ──────────────────────────
if ! dscl . -read /Users/ea-bot >/dev/null 2>&1; then
    echo "Creating user 'ea-bot' (UID $EA_BOT_UID, hidden)..."
    dscl . -create /Users/ea-bot
    dscl . -create /Users/ea-bot UserShell /usr/bin/false
    dscl . -create /Users/ea-bot RealName "EA Bot"
    dscl . -create /Users/ea-bot UniqueID "$EA_BOT_UID"
    dscl . -create /Users/ea-bot PrimaryGroupID "$EA_GID"
    dscl . -create /Users/ea-bot NFSHomeDirectory "$EA_BOT_HOME"
    dscl . -create /Users/ea-bot IsHidden 1
else
    echo "User 'ea-bot' already exists."
fi
# Login disabled: '*' in the Password attribute, with no
# AuthenticationAuthority. `dscl -passwd` runs the password-quality policy,
# which rejects '*' on current macOS and stopped the first A414 run halfway.
# Outside the create branch so a half-made user from such a run is finished.
dscl . -create /Users/ea-bot Password '*'
mkdir -p "$EA_BOT_HOME"
chown ea-bot:ea "$EA_BOT_HOME"

# ── Step 3: Deploy source code to ea-bot's home ────────────────────
# The copy sits in ea-bot's home, so ea-bot can rename it and put its own
# tree, or a symlink, in its place. git run by root in a tree ea-bot
# controls runs ea-bot's config and hooks as root, and a chown of such a
# tree hands ea-bot's files to $REAL_USER, whose deploys then trust them.
# So no step here acts on the copy by name. Each one enters it and checks
# the directory it is in, as the deploy hook and scripts/deploy.sh do:
# after `cd -P`, `pwd -P` proves no symlink led there, and the entered
# directory stays the same one however the name is changed afterwards.
# The directory and its .git must be real directories owned by root or
# $REAL_USER (ea-bot can make neither) that no one else can write.
DEPLOY_DIR_PHYS="$(cd -P -- "$EA_BOT_HOME" && pwd -P)/ea-system"
deploy_tree_ok() {
    [ "$(pwd -P)" = "$DEPLOY_DIR_PHYS" ] || return 1
    local d owner mode
    for d in "$@"; do
        [ -d "$d" ] && [ ! -L "$d" ] || return 1
        # GNU stat answers -c and rejects nothing silently; BSD stat rejects -c, so
        # the fallback is unambiguous whichever stat is first on PATH.
        owner=$(stat -c %u "$d" 2>/dev/null || stat -f %u "$d" 2>/dev/null) || return 1
        [ "$owner" = 0 ] || [ "$owner" = "$REAL_UID" ] || return 1
        mode=$(stat -c %a "$d" 2>/dev/null || stat -f %Lp "$d" 2>/dev/null) || return 1
        [ $(( 0$mode & 022 )) -eq 0 ] || return 1
    done
}
# git in the copy: in a subshell that has entered and checked it, with
# every GIT_* variable cleared (the repository ones and any inherited -c
# settings), so git works on that copy and nothing else. `deploy_git
# clone` fills a new, empty copy from $SCRIPT_DIR, which has no .git yet.
deploy_git() (
    cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null || exit 97
    if [ "$1" = clone ]; then
        deploy_tree_ok . || exit 97
        set -- clone -- "$SCRIPT_DIR" .
    else
        deploy_tree_ok . .git || exit 97
        set -- --git-dir=.git --work-tree=. "$@"
    fi
    for v in $(compgen -e); do
        case "$v" in GIT_*) unset "$v" ;; esac
    done
    umask 022
    exec git "$@"
)
UNSAFE_COPY="ERROR: $EA_BOT_SCRIPT_DIR and its .git must be real directories (not symlinks), owned by root or $REAL_USER and writable by no one else. Move it aside and re-run for a fresh copy."

if [ -e "$EA_BOT_SCRIPT_DIR" ] || [ -L "$EA_BOT_SCRIPT_DIR" ]; then
    echo "Source already deployed at $EA_BOT_SCRIPT_DIR. Updating..."
    if ! ( cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null && deploy_tree_ok . .git ); then
        echo "$UNSAFE_COPY" >&2
        exit 1
    fi
    # A reset follows only a fetch that worked, so the copy never goes back
    # to an older FETCH_HEAD. The copy is checked, so setup goes on.
    if ! { deploy_git fetch "$SCRIPT_DIR" HEAD && deploy_git reset --hard FETCH_HEAD; }; then
        echo "WARNING: $EA_BOT_SCRIPT_DIR was not updated. Run scripts/deploy.sh as $REAL_USER after setup." >&2
    fi
else
    echo "Cloning source to $EA_BOT_SCRIPT_DIR..."
    # mkdir fails when anything, a symlink too, has the name, so the clone
    # starts in a directory root has just made.
    mkdir -m 755 -- "$EA_BOT_SCRIPT_DIR"
    if ! deploy_git clone; then
        echo "ERROR: the clone into $EA_BOT_SCRIPT_DIR failed or the directory changed under it." >&2
        ( cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null && echo "  at: $(pwd -P) (expected $DEPLOY_DIR_PHYS)" && echo "  .: owner uid $(stat -c %u . 2>/dev/null || stat -f %u .) mode $(stat -c %a . 2>/dev/null || stat -f %Lp .)" ) >&2 || true
        exit 1
    fi
fi
# The developer guide stays out of ea-bot's tree: Claude Code would load
# .claude/CLAUDE.md into every EA turn. The setting persists for the
# deploys after this one; the chown below hands its file to $REAL_USER.
deploy_git sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md' >/dev/null 2>&1 \
    || echo "WARNING: .claude/CLAUDE.md may be in $EA_BOT_SCRIPT_DIR (sparse-checkout failed)." >&2
# $REAL_USER owns the files (so its deploys work), ea-bot reads via group.
if ! ( cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null && deploy_tree_ok . .git \
        && chown -R "$REAL_USER:ea" . && chmod -R u+rwX,g+rX,g-w . ); then
    echo "$UNSAFE_COPY" >&2
    exit 1
fi

# ── Step 4: Move .ea/ to ea-bot's home ─────────────────────────────
# deploy/tree-custody.sh says how root acts on ea-bot's trees and on auth/.
. "$SCRIPT_DIR/deploy/tree-custody.sh"
FIRST_MOVE=0
if [ -d "$EA_BOT_EA_DIR" ] && [ ! -L "$EA_BOT_EA_DIR" ]; then
    echo "EA state directory already at $EA_BOT_EA_DIR."
elif [ -d "$OLD_EA_DIR" ] && [ ! -L "$OLD_EA_DIR" ]; then
    # BSD mv has no -T: a name ea-bot made there (a link to a directory)
    # would receive the tree, so any entry at the name stops the move.
    if [ -e "$EA_BOT_EA_DIR" ] || [ -L "$EA_BOT_EA_DIR" ]; then
        echo "ERROR: $EA_BOT_EA_DIR exists and is not a directory; move it aside and re-run." >&2
        exit 1
    fi
    echo "Moving $OLD_EA_DIR → $EA_BOT_EA_DIR..."
    mv "$OLD_EA_DIR" "$EA_BOT_EA_DIR"
    FIRST_MOVE=1
elif [ -L "$OLD_EA_DIR" ]; then
    echo "Symlink already exists at $OLD_EA_DIR → $(readlink "$OLD_EA_DIR")"
else
    echo "WARNING: No .ea directory found at $OLD_EA_DIR or $EA_BOT_EA_DIR."
    echo "  Run './scripts/install/setup.sh --profile <name>' first (docs/SETUP.md 'The service account')."
    exit 1
fi

# Ownership and modes. Root works inside the state directory it entered and
# checked (tc_enter), and the auth directory goes to $REAL_USER before
# anything else: it and every token in it are never ea-bot's, at any moment.
# Owner processes (the Outlook harvest, Google token rotation, the mediator)
# write tokens here, and the owner checks (owner_trust.py) refuse a
# directory or token another user owns. So root adopts nothing there: an
# auth/ that neither $REAL_USER nor root owns stops the run with the
# recovery steps, and an entry in it that is not $REAL_USER's, or a file
# with more than one link, is quarantined, never chowned
# (deploy/tree-custody.sh). macOS has no fs.protected_hardlinks, so ea-bot
# can link any file it can reach: the link count is what keeps another
# name of a file out of auth/ here.
EA_DIR_PHYS="$(tc_phys "$EA_BOT_HOME")/.ea"
if ! ( tc_enter "$EA_DIR_PHYS" || exit 1
       [ -e auth ] || [ -L auth ] || mkdir -m 0750 auth || exit 1
       auth_lock "$EA_DIR_PHYS/auth" "$REAL_USER" "$REAL_UID" ea "$REAL_GROUP" || exit 1
       if [ "$FIRST_MOVE" = 1 ]; then
           # The tree came from $REAL_USER's home this run: give it to
           # ea-bot, auth/ and .env.secrets aside. A file with more than
           # one link is left out, as on Linux: with no protected_hardlinks
           # its other name may be any file ea-bot could reach.
           tc_give_moved_tree "$EA_BOT_UID" "$EA_GID" || exit 1
       fi ); then
    echo "ERROR: setting the owners in $EA_BOT_EA_DIR stopped (see above). It must be a real directory, not a link, at $EA_DIR_PHYS." >&2
    exit 1
fi
if [ "$FIRST_MOVE" != 1 ]; then
    n="$(tc_not_resident "$EA_BOT_EA_DIR" "$EA_BOT_UID")"
    [ "$n" = 0 ] || echo "NOTE: $n entries in $EA_BOT_EA_DIR are not ea-bot's (files the owner's processes wrote); left as they are."
fi

# Modes and groups, as ea-bot: it can change only what it owns.
tc_as_user ea-bot chmod 750 "$EA_BOT_EA_DIR" || true
tc_as_user ea-bot find "$EA_BOT_EA_DIR" -xdev \( -path "$EA_BOT_EA_DIR/auth" -o -path "$EA_BOT_EA_DIR/.env.secrets" \) -prune \
    -o -uid "$EA_BOT_UID" ! -gid "$EA_GID" -exec chgrp -h "$EA_GID" {} + || true
tc_as_user ea-bot find "$EA_BOT_EA_DIR" -maxdepth 1 -name .env -type f -uid "$EA_BOT_UID" -exec chmod 640 {} + || true
# .env.secrets: owner-only, tightened as $REAL_USER. One that is not
# $REAL_USER's is left for the owner checks to refuse.
if [ -f "$EA_BOT_EA_DIR/.env.secrets" ]; then
    tc_as_user "$REAL_USER" chmod 600 "$EA_BOT_EA_DIR/.env.secrets" \
        || echo "  (.env.secrets is not $REAL_USER's; secrets belong in ~/.ea-private/secrets.env)"
fi

# Zone A: shared read-write
for dir in state logs receipts uploads letters templates; do
    if [ -d "$EA_BOT_EA_DIR/$dir" ]; then
        tc_as_user ea-bot find "$EA_BOT_EA_DIR/$dir" -xdev -uid "$EA_BOT_UID" -type d -exec chmod 2775 {} + || true
        tc_as_user ea-bot find "$EA_BOT_EA_DIR/$dir" -xdev -uid "$EA_BOT_UID" -type f -exec chmod 664 {} + || true
    fi
done

# Zone B: config (read-only for group). auth/ is the owner's: auth_lock above.
if [ -d "$EA_BOT_EA_DIR/config" ]; then
    tc_as_user ea-bot chmod 750 "$EA_BOT_EA_DIR/config" || true
    tc_as_user ea-bot find "$EA_BOT_EA_DIR/config" -xdev -uid "$EA_BOT_UID" -type f -exec chmod 640 {} + || true
fi

# Back-compat symlink
if [ ! -L "$OLD_EA_DIR" ]; then
    echo "Creating symlink: $OLD_EA_DIR → $EA_BOT_EA_DIR"
    ln -s "$EA_BOT_EA_DIR" "$OLD_EA_DIR"
fi
# Outside the branch, so a link left root-owned by an interrupted run is fixed.
chown -h "$REAL_USER:$REAL_GROUP" "$OLD_EA_DIR"

# ── Step 5: Lock down invoking user's home ─────────────────────────
echo "Removing other-execute from $REAL_HOME..."
chmod o-x "$REAL_HOME"

# ── Step 6: Claude Code config for ea-bot ──────────────────────────
echo "Setting up Claude Code config for ea-bot..."
tc_as_user ea-bot mkdir -p "$EA_BOT_HOME/.claude" || true
# Root reads the owner's copy; ea-bot writes its own, so root writes nothing
# in ea-bot's home.
if [ -d "$REAL_HOME/.claude/plugins" ]; then
    tar -C "$REAL_HOME/.claude" -cf - plugins | tc_as_user ea-bot tar -C "$EA_BOT_HOME/.claude" -xf - || true
fi
if [ -f "$REAL_HOME/.claude/mcp-needs-auth-cache.json" ]; then
    tc_as_user ea-bot sh -c 'cat > "$1"' sh "$EA_BOT_HOME/.claude/mcp-needs-auth-cache.json" \
        < "$REAL_HOME/.claude/mcp-needs-auth-cache.json" || true
fi
# 710: the bot (owner, group ea) must see that a session transcript
# exists before it resumes one (slack_sessions.session_file); g+x
# lets it stat a known name, never list or read. Files stay 600.
tc_as_user ea-bot chmod 710 "$EA_BOT_HOME/.claude" || true
tc_as_user ea-bot find "$EA_BOT_HOME/.claude" -xdev -uid "$EA_BOT_UID" ! -gid "$EA_GID" -exec chgrp -h "$EA_GID" {} + || true
tc_as_user ea-bot find "$EA_BOT_HOME/.claude" -xdev -uid "$EA_BOT_UID" -type f -exec chmod 600 {} + || true
n="$(tc_not_resident "$EA_BOT_HOME/.claude" "$EA_BOT_UID")"
[ "$n" = 0 ] || echo "WARNING: $n entries in $EA_BOT_HOME/.claude are not ea-bot's; left as they are." >&2

# ── Step 6b: Claude Code for ea-bot, in its own home ──────────────
# The bot runs Claude as ea-bot, so the binary must be one ea-bot can
# execute. The owner's copy ($(which claude), usually under the owner's
# home) is not: the owner's home stays private (no o+x), and granting a
# path into it would open the whole home. So ea-bot gets its own native
# install at $EA_BOT_HOME/.local/bin/claude, which updates itself; nothing
# is copied from the owner's install, now or later.
EA_BOT_CLAUDE="$EA_BOT_HOME/.local/bin/claude"
if sudo -u ea-bot test -x "$EA_BOT_CLAUDE"; then
    echo "Claude Code for ea-bot: $EA_BOT_CLAUDE present"
else
    echo "Installing Claude Code for ea-bot (native installer, as ea-bot)..."
    # From /, not the caller's directory, which ea-bot may not be able to
    # enter (the installer fails on getcwd).
    if ( cd / && sudo -u ea-bot env HOME="$EA_BOT_HOME" bash -c 'cd "$HOME" && curl -fsSL https://claude.ai/install.sh -o "$HOME/.claude-install.sh" && bash "$HOME/.claude-install.sh"; rc=$?; rm -f "$HOME/.claude-install.sh"; exit $rc' ); then
        echo "Claude Code for ea-bot: installed at $EA_BOT_CLAUDE"
    else
        echo "WARNING: the install did not finish. Run it later, then re-run this script:"
        echo "  cd / && sudo -u ea-bot env HOME=$EA_BOT_HOME bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash'"
    fi
fi
CLAUDE_BIN="$EA_BOT_CLAUDE"

# ── Step 7: Sudoers entry ──────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/ea-bot"
echo "Configuring sudoers..."
# Any command as ea-bot, not only claude: the bot and run-agent.sh both run
# `sudo -u ea-bot env VAR=... claude`, so the command sudo matches is env,
# and a claude-only rule refuses them. Allowing env alone would be no
# narrower, since env runs anything. The rule lets the
# owner become the less-privileged ea-bot; it gives ea-bot nothing.
cat <<SUDOEOF > "$SUDOERS_FILE"
# Allow $REAL_USER to run commands as ea-bot without a password
$REAL_USER ALL=(ea-bot) NOPASSWD: ALL
# Preserve needed env vars through sudo
Defaults:$REAL_USER env_keep += "PYTHONPATH"
SUDOEOF
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" && echo "Sudoers syntax OK." || {
    echo "ERROR: sudoers syntax check failed!"
    rm -f "$SUDOERS_FILE"
    exit 1
}

# ── Step 8: Verify ────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
echo -n "ea-bot user: "; id ea-bot && echo "" || echo "MISSING"
echo -n "ea group: "; dscl . -read /Groups/ea PrimaryGroupID 2>/dev/null || echo "MISSING"
echo -n "sudoers: "; sudo -n -u ea-bot true 2>/dev/null && echo "sudoers OK" || echo "sudoers: may need re-login"
echo -n "Deployed source readable: "; sudo -u ea-bot test -r "$EA_BOT_SCRIPT_DIR/CLAUDE.md" && echo "OK" || echo "FAIL"
echo -n "Deployed source not writable: "; sudo -u ea-bot test -w "$EA_BOT_SCRIPT_DIR/CLAUDE.md" && echo "FAIL (writable!)" || echo "OK (read-only)"
echo -n "State writable: "; sudo -u ea-bot test -w "$EA_BOT_EA_DIR/state" && echo "OK" || echo "FAIL"
echo -n "Invoking home private: "; sudo -u ea-bot ls "$REAL_HOME/" >/dev/null 2>&1 && echo "FAIL (accessible!)" || echo "OK (private)"
echo ""
echo "=== Next Steps ==="
echo "1. Re-login to pick up 'ea' group membership"
echo "2. Log ea-bot's Claude Code in (/login, then Ctrl+C); the claude.ai Gmail and"
echo "   Calendar connectors come with that account (docs/SETUP.md 'Claude Code connectors'):"
echo "   sudo -u ea-bot env HOME=$EA_BOT_HOME claude"
echo "   (enable the connectors in claude.ai Settings > Connectors for that account)"
echo "3. Every profile (after the chat platform on assistant): the services, then mail mediation:"
echo "   sudo bash $SCRIPT_DIR/deploy/macos/install-services.sh"
echo "   sudo bash $SCRIPT_DIR/deploy/enable-mail-mediation.sh"
echo "4. On assistant, confirm 'isolation: ENFORCED' in ~/.ea-private/logs/*ea-chat-bot.launchd.log"
echo ""
echo "Done."
