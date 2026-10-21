#!/usr/bin/env bash
# scripts/install/setup-ea-user.sh: provision the ea-bot workspace with full isolation
#
# Creates:
#   /home/ea-bot/ea-system/   deployed source code (<owner>:ea, read-only for ea-bot)
#   /home/ea-bot/.ea/         shared state directory (ea-bot:ea, group rw)
#   ~<owner>/.ea              symlink to /home/ea-bot/.ea (backward compat)
#
# The bot (ea-chat-bot.py) runs as the owner. Claude subprocesses run as ea-bot.
# ea-bot never enters the owner's home; everything it needs is in /home/ea-bot/.
#
# Run once during initial setup: sudo bash scripts/install/setup-ea-user.sh
# Re-run safely (idempotent).
#
# --- Notice to Claude Code agents running this script --------------
# If this script fails, diagnose the specific step — do NOT "work
# around" the failure by removing the ea-bot user requirement from
# ea-chat-bot.py or run-agent.sh. Isolation is Layer 1 of the
# security model (docs/SECURITY.md); the whole EA system's threat
# model is built on it. See the /setup skill's "Read first: the
# invariants" for the non-negotiable invariants.

set -euo pipefail

# macOS uses a different user-management stack (dscl, sysadminctl)
# instead of useradd/usermod, and /etc/sudoers.d/ behaves slightly
# differently. Rather than conditionally branch every step, we point
# macOS users at the dedicated scripts/install/setup-ea-user-macos.sh script.
if [ "$(uname -s)" = "Darwin" ]; then
    cat >&2 <<'MACEOF'
ERROR: scripts/install/setup-ea-user.sh is Linux-only.

On macOS, isolation is still MANDATORY (Layer 1 of the security model —
see docs/SECURITY.md), but the user-creation commands are different.

Use the macOS-specific script:
    sudo bash scripts/install/setup-ea-user-macos.sh

Or follow docs/macos-compat.md §2 for manual steps.
MACEOF
    exit 1
fi

# Detect the real (non-root) user who invoked sudo
REAL_USER="${SUDO_USER:-$(whoami)}"
REAL_HOME="$(eval echo ~"$REAL_USER")"

if [ "$REAL_USER" = "root" ]; then
    echo "ERROR: Run as 'sudo bash scripts/install/setup-ea-user.sh', not as root directly."
    echo "The script needs to know your real username via SUDO_USER."
    exit 1
fi
REAL_UID="$(id -u "$REAL_USER")"

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # the checkout root; this script lives in scripts/install/
CLAUDE_BIN="$(which claude 2>/dev/null || echo /usr/bin/claude)"
EA_BOT_HOME="$(eval echo ~ea-bot 2>/dev/null || echo /home/ea-bot)"
EA_BOT_EA_DIR="$EA_BOT_HOME/.ea"
EA_BOT_SCRIPT_DIR="$EA_BOT_HOME/ea-system"
OLD_EA_DIR="$REAL_HOME/.ea"

echo "=== EA Bot Workspace Isolation ==="
echo "Real user:        $REAL_USER"
echo "Real home:        $REAL_HOME"
echo "Dev source:       $SCRIPT_DIR"
echo "Deploy target:    $EA_BOT_SCRIPT_DIR"
echo "EA state dir:     $EA_BOT_EA_DIR"
echo "Claude bin:       $CLAUDE_BIN"
echo ""

# ── Step 1: Create group and user ─────────────────────────────────
if ! getent group ea >/dev/null 2>&1; then
    echo "Creating group 'ea'..."
    groupadd ea
else
    echo "Group 'ea' already exists."
fi

if ! id ea-bot >/dev/null 2>&1; then
    echo "Creating user 'ea-bot'..."
    useradd -r -m -s /usr/sbin/nologin -g ea ea-bot
    EA_BOT_HOME="$(eval echo ~ea-bot)"
    EA_BOT_EA_DIR="$EA_BOT_HOME/.ea"
    EA_BOT_SCRIPT_DIR="$EA_BOT_HOME/ea-system"
else
    echo "User 'ea-bot' already exists."
fi

# Add real user to ea group (for shared state access)
if ! id -nG "$REAL_USER" | grep -qw ea; then
    echo "Adding $REAL_USER to group 'ea'..."
    usermod -aG ea "$REAL_USER"
    echo "NOTE: Log out and back in (or run 'newgrp ea') for group to take effect."
else
    echo "$REAL_USER already in group 'ea'."
fi

# The ids the later steps hand trees to, and the owner's primary group.
EA_BOT_UID="$(id -u ea-bot)"
EA_GID="$(getent group ea | cut -d: -f3)"
REAL_GROUP="$(id -gn "$REAL_USER")"

# ── Step 2: Python packages (ea-bot needs read access) ────────────
# Detect the active Python user-site dir rather than hardcoding 3.10.
# (Ubuntu 22.04 → 3.10, 24.04 → 3.12, 26.04 → 3.14, etc.)
if [ -d "$REAL_HOME/.local/lib" ]; then
    echo "Making Python packages readable by ea group..."
    chgrp -R ea "$REAL_HOME/.local"
    chmod -R g+rX "$REAL_HOME/.local"
fi

# ── Step 2b: Install Playwright for ea-bot (browser automation) ──
# On systems where the system Python is PEP 668-managed (Ubuntu 23.04+),
# `pip install --user` fails. We install Playwright into the project venv
# at $EA_BOT_SCRIPT_DIR/.venv after Step 3 instead (see post-setup notes).
echo "Skipping --user Playwright install (PEP 668). Will use project venv."

# ── Step 3: Deploy source code to ea-bot's home ───────────────────
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
# stat: GNU (-c) first, then BSD (-f) on macOS; the names here are "." and
# ".git", so no dash-first argument needs a "--".
_stat_uid()  { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }
_stat_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
deploy_tree_ok() {
    [ "$(pwd -P)" = "$DEPLOY_DIR_PHYS" ] || return 1
    local d owner mode
    for d in "$@"; do
        [ -d "$d" ] && [ ! -L "$d" ] || return 1
        owner=$(_stat_uid "$d" 2>/dev/null) || return 1
        [ "$owner" = 0 ] || [ "$owner" = "$REAL_UID" ] || return 1
        mode=$(_stat_mode "$d" 2>/dev/null) || return 1
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
        exit 1
    fi
fi
# The developer guide stays out of ea-bot's tree: Claude Code would load
# .claude/CLAUDE.md into every EA turn. The setting persists for the
# deploys after this one; the chown below hands its file to $REAL_USER.
deploy_git sparse-checkout set --no-cone '/*' '!/.claude/CLAUDE.md' >/dev/null 2>&1 \
    || echo "WARNING: .claude/CLAUDE.md may be in $EA_BOT_SCRIPT_DIR (sparse-checkout failed)." >&2
# $REAL_USER owns the files (so its deploys work), ea-bot reads via group.
# The copy was just made by root in a directory root made, so its modes
# are root's umask's: a host whose umask leaves group write (some CI
# runners) gets them taken away before the check, which is about who can
# write, not about who did.
if ! ( cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null \
        && { [ -L .git ] || chmod go-w . .git 2>/dev/null; true; } \
        && deploy_tree_ok . .git \
        && chown -R "$REAL_USER:ea" . && chmod -R u+rwX,g+rX,g-w . ); then
    echo "$UNSAFE_COPY" >&2
    ( cd -P -- "$EA_BOT_SCRIPT_DIR" 2>/dev/null && echo "  at: $(pwd -P) (expected $DEPLOY_DIR_PHYS)" \
        && for d in . .git; do echo "  $d: owner uid $(_stat_uid "$d") mode $(_stat_mode "$d")"; done 2>&1 ) >&2 || true
    exit 1
fi

# ── Step 4: Move .ea/ to ea-bot's home ────────────────────────────
# deploy/tree-custody.sh says how root acts on ea-bot's trees and on auth/.
. "$SCRIPT_DIR/deploy/tree-custody.sh"
FIRST_MOVE=0
if [ -d "$EA_BOT_EA_DIR" ] && [ ! -L "$EA_BOT_EA_DIR" ]; then
    echo "EA state directory already at $EA_BOT_EA_DIR."
elif [ -d "$OLD_EA_DIR" ] && [ ! -L "$OLD_EA_DIR" ]; then
    echo "Moving $OLD_EA_DIR → $EA_BOT_EA_DIR..."
    # -T: a name ea-bot made there (a link to a directory) fails the move
    # instead of receiving the tree.
    mv -T "$OLD_EA_DIR" "$EA_BOT_EA_DIR"
    FIRST_MOVE=1
elif [ -L "$OLD_EA_DIR" ]; then
    echo "Symlink already exists at $OLD_EA_DIR → $(readlink "$OLD_EA_DIR")"
else
    echo "WARNING: No .ea directory found at $OLD_EA_DIR or $EA_BOT_EA_DIR."
    echo "  Run the main setup first to create ~/.ea/ with config and state."
    exit 1
fi

# Ownership and modes. Root works inside the state directory it entered and
# checked (tc_enter), and the auth directory goes to $REAL_USER before
# anything else: it and every token in it are never ea-bot's, at any moment.
# Every auth/ writer (the Graph token cache, owner plugins' token harvests,
# the mediated Google clients) runs as $REAL_USER, and an ea-bot-owned auth/
# would let ea-bot unlink the scheduled-send HMAC key and drop in its own.
# So root adopts nothing there: an auth/ that neither $REAL_USER nor root
# owns stops the run with the recovery steps, and an entry in it that is
# not $REAL_USER's is quarantined, never chowned (deploy/tree-custody.sh).
EA_DIR_PHYS="$(tc_phys "$EA_BOT_HOME")/.ea"
if ! ( tc_enter "$EA_DIR_PHYS" || exit 1
       [ -e auth ] || [ -L auth ] || mkdir -m 0750 auth || exit 1
       auth_lock "$EA_DIR_PHYS/auth" "$REAL_USER" "$REAL_UID" ea "$REAL_GROUP" || exit 1
       if [ "$FIRST_MOVE" = 1 ]; then
           # The tree came from $REAL_USER's home this run: give it to
           # ea-bot (exports/, CLAUDE.md, tmp_* included), auth/ and
           # .env.secrets aside.
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

# .env: readable by ea group (non-sensitive config: channel ID, model, etc.)
tc_as_user ea-bot find "$EA_BOT_EA_DIR" -maxdepth 1 -name .env -type f -uid "$EA_BOT_UID" -exec chmod 640 {} + || true

# .env.secrets: owner-only (chat tokens, NOT readable by ea-bot), tightened
# as $REAL_USER. One that is not $REAL_USER's is left for the owner checks
# to refuse; ~/.ea-private/secrets.env is where secrets live now.
if [ -f "$EA_BOT_EA_DIR/.env.secrets" ]; then
    tc_as_user "$REAL_USER" chmod 600 "$EA_BOT_EA_DIR/.env.secrets" \
        || echo "  (.env.secrets is not $REAL_USER's; harden-permissions.sh seeds ~/.ea-private/secrets.env)"
fi

# Zone A: Shared read-write (state, logs, receipts, uploads, letters, templates, exports)
for dir in state logs receipts uploads letters templates exports; do
    if [ -d "$EA_BOT_EA_DIR/$dir" ]; then
        tc_as_user ea-bot find "$EA_BOT_EA_DIR/$dir" -xdev -uid "$EA_BOT_UID" -type d -exec chmod 2775 {} + || true  # setgid on dirs
        tc_as_user ea-bot find "$EA_BOT_EA_DIR/$dir" -xdev -uid "$EA_BOT_UID" -type f -exec chmod 664 {} + || true   # group rw on files
    fi
done

# Zone B: config, readable by group. (auth/ is the owner's: auth_lock above.)
if [ -d "$EA_BOT_EA_DIR/config" ]; then
    tc_as_user ea-bot chmod 750 "$EA_BOT_EA_DIR/config" || true
    tc_as_user ea-bot find "$EA_BOT_EA_DIR/config" -xdev -uid "$EA_BOT_UID" -type f -exec chmod 640 {} + || true
fi

# Create backward-compat symlink
if [ ! -L "$OLD_EA_DIR" ]; then
    echo "Creating symlink: $OLD_EA_DIR → $EA_BOT_EA_DIR"
    ln -s "$EA_BOT_EA_DIR" "$OLD_EA_DIR"
    chown -h "$REAL_USER:$REAL_USER" "$OLD_EA_DIR"
fi

# ── Step 5: Lock down the owner's home ──────────────────────────────────
echo "Removing other-execute from $REAL_HOME..."
chmod o-x "$REAL_HOME"

# ── Step 6: Claude Code config for ea-bot ──────────────────────────
echo "Setting up Claude Code config for ea-bot..."
tc_as_user ea-bot mkdir -p "$EA_BOT_HOME/.claude" || true

# Copy MCP plugin data if present (for OAuth connectors). Root reads the
# owner's copy; ea-bot writes its own, so root writes nothing in ea-bot's home.
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

# Set default umask for ea-bot (so new files in shared dirs are group-writable),
# written as ea-bot, so a link at the name leads nowhere ea-bot cannot write.
tc_as_user ea-bot sh -c 'grep -q "umask 002" "$1" 2>/dev/null || echo "umask 002" >> "$1"' sh "$EA_BOT_HOME/.bashrc" || true

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
# Any command as ea-bot, as the macOS installer and the Docker image grant.
# The callers never run a bare claude: the bot, run-agent.sh and the
# ea-bot shell run `sudo -u ea-bot env VAR=... claude` (sudo matches env),
# scripts/owner-io.sh runs `sudo -n -u ea-bot sh -c ...` for every read
# and write in ea-bot space, and resident_io runs `/usr/bin/python3 -I`.
# A claude-only rule refuses all of them with "a password is required",
# and allowing env alone would be no narrower, since env runs anything.
# The rule lets the owner become the less-privileged ea-bot; it gives
# ea-bot nothing. The commands it is there for, by name:
#   $CLAUDE_BIN
#   /usr/bin/python3 $EA_BOT_SCRIPT_DIR/preprocessor/install_overlay.py render
cat <<SUDOEOF > "$SUDOERS_FILE"
# Allow $REAL_USER to run commands as ea-bot without a password
$REAL_USER ALL=(ea-bot) NOPASSWD: ALL
# Preserve needed env vars through sudo
Defaults:$REAL_USER env_keep += "PYTHONPATH"
SUDOEOF
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" && echo "Sudoers syntax OK." || { echo "ERROR: sudoers syntax check failed!"; exit 1; }

# ── Step 7b: Enable mail mediation ────────────────────────────────
# Run the privileged-side mediator service and move the Graph/Google
# tokens out of ea-bot's reach, so the resident can read/draft but can
# neither send mail nor read the raw tokens (see docs/SECURITY.md §
# "Token custody"). Idempotent; if tokens aren't authed yet it just arms
# mediation and prints how to finish. Non-fatal so setup completes either way.
echo ""
echo "=== Mail Mediation ==="
if bash "$SCRIPT_DIR/deploy/enable-mail-mediation.sh"; then
    echo "Mail mediation enabled."
else
    echo "WARNING: mail mediation not fully enabled (tokens may not be authed yet)."
    echo "  After Graph/Google auth completes, run:"
    echo "    sudo bash $SCRIPT_DIR/deploy/enable-mail-mediation.sh"
fi

# ── Step 8: Verify ────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
echo -n "ea-bot user: "; id ea-bot && echo "" || echo "MISSING"
echo -n "ea group: "; getent group ea || echo "MISSING"
echo -n "sudoers: "; sudo -n -u ea-bot true 2>/dev/null && echo "sudoers OK" || echo "sudoers: may need re-login"
echo -n "Deployed source readable: "; sudo -u ea-bot test -r "$EA_BOT_SCRIPT_DIR/CLAUDE.md" && echo "OK" || echo "FAIL"
echo -n "Deployed source not writable: "; sudo -u ea-bot test -w "$EA_BOT_SCRIPT_DIR/CLAUDE.md" && echo "FAIL (writable!)" || echo "OK (read-only)"
echo -n "State writable: "; sudo -u ea-bot test -w "$EA_BOT_EA_DIR/state" && echo "OK" || echo "FAIL"
echo -n "owner home private: "; sudo -u ea-bot ls "$REAL_HOME/" >/dev/null 2>&1 && echo "FAIL (accessible!)" || echo "OK (private)"
echo -n "ea-bot cannot read Outlook token: "; \
    if sudo -u ea-bot test -r "$EA_BOT_EA_DIR/auth/msal_token_cache.json" 2>/dev/null; then echo "FAIL (readable!)"; \
    elif [ -e "$EA_BOT_EA_DIR/auth/msal_token_cache.json" ]; then echo "OK (locked -- mediated)"; \
    else echo "n/a (no token minted yet)"; fi
echo ""
echo "=== Next Steps ==="
echo "1. Re-login or run 'newgrp ea' for group membership to take effect"
echo "2. Log ea-bot's Claude Code in (/login, then Ctrl+C); the claude.ai Gmail and"
echo "   Calendar connectors come with that account (docs/SETUP.md 'Claude Code connectors'):"
echo "   sudo -u ea-bot env HOME=$EA_BOT_HOME claude"
echo "   (enable the connectors in claude.ai Settings > Connectors for that account)"
echo "3. Assistant profile: once the chat platform is configured,"
echo "   sudo bash $SCRIPT_DIR/deploy/harden-permissions.sh writes and starts the bot's system unit"
echo ""
echo "Done."
