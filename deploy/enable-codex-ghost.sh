#!/usr/bin/env bash
#
# Give the resident (ea-bot) its own Codex CLI and login, for the Codex
# ghost arm (docs/codex-harness.md, "The ghost arm"). Run as the owner,
# in a terminal, once:
#
#   bash deploy/enable-codex-ghost.sh
#
# What it does, and why each step is the owner's:
#   1. Copies the Codex binary and its resources from the owner's install
#      into ~ea-bot/.local/codex-vendor and links ~ea-bot/.local/bin/codex.
#      ea-bot cannot read the owner's home, and nothing in ea-bot's space
#      may come from ea-bot on trust, so the owner copies a binary it ran.
#   2. Writes ~ea-bot/.codex/config.toml: the model, no network inside the
#      sandbox, and trust for the lane directories.
#   3. Signs ea-bot in with a device code (`codex login --device-auth`):
#      the resident holds its own Codex login, as it holds its own Claude
#      login, and never the owner's token file.
#   4. Probes: `codex_cli.py probe` as ea-bot must answer OK.
#
# The ghost runs as ea-bot because Codex's sandbox confines writes, not
# reads: a model shell started as the owner could read the owner's
# secrets. ea-bot cannot, by the two-user layout.
#
#   EA_CODEX_SRC   the directory holding bin/codex (default: resolved from
#                  the owner's `codex` on PATH)
#   EA_CODEX_MODEL the model to write into ea-bot's config (default: the
#                  owner's config.toml model, else gpt-5.6-sol)
set -euo pipefail
EA_BOT_HOME="$(getent passwd ea-bot 2>/dev/null | cut -d: -f6 || true)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Where the real binary is. `codex` on the owner's PATH is usually npm's
# JavaScript launcher (bin/codex.js in the @openai/codex package); the
# static binary and its resources sit in the platform package beside it,
# node_modules/@openai/codex-<platform>/vendor/<triple>/. A Homebrew or
# a direct install puts the binary itself on PATH.
codex_source_dir() {
    local owner real pkg vendor
    owner="$(command -v codex || true)"
    [ -n "$owner" ] || return 1
    real="$(readlink -f "$owner")"
    case "$real" in
        *.js)
            pkg="$(dirname "$(dirname "$real")")"
            for vendor in "$pkg"/node_modules/@openai/codex-*/vendor/*/ "$pkg"/../codex-*/vendor/*/; do
                if [ -x "$vendor/bin/codex" ]; then printf '%s\n' "${vendor%/}"; return 0; fi
            done
            return 1 ;;
        *)
            # A real binary: its directory, with resources beside it when present.
            if [ -d "$(dirname "$real")/codex-resources" ]; then printf '%s\n' "$(dirname "$real")"; return 0; fi
            if [ -d "$(dirname "$(dirname "$real")")/codex-resources" ]; then printf '%s\n' "$(dirname "$(dirname "$real")")"; return 0; fi
            printf '%s\n' "$(dirname "$real")"; return 0 ;;
    esac
}
if [ "${1:-}" = "--print-source" ]; then
    # For a check or a test: the directory the copy would come from, no sudo.
    codex_source_dir || { echo "no codex binary found behind $(command -v codex || echo 'nothing on PATH')" >&2; exit 1; }
    exit 0
fi
[ -n "$EA_BOT_HOME" ] || { echo "no ea-bot user; run setup-ea-user.sh first" >&2; exit 1; }
if [ -z "${EA_CODEX_SRC:-}" ]; then
    EA_CODEX_SRC="$(codex_source_dir || true)"
    [ -n "$EA_CODEX_SRC" ] || { echo "no codex binary found behind the owner's \`codex\` (npm's launcher with no platform package?); set EA_CODEX_SRC to the directory holding bin/codex" >&2; exit 1; }
fi
[ -x "$EA_CODEX_SRC/bin/codex" ] || [ -x "$EA_CODEX_SRC/codex" ] || { echo "no codex binary under $EA_CODEX_SRC" >&2; exit 1; }
BIN_DIR="$EA_CODEX_SRC/bin"; [ -d "$BIN_DIR" ] || BIN_DIR="$EA_CODEX_SRC"
echo "[codex] source: $EA_CODEX_SRC"

as_bot() { sudo -u ea-bot env HOME="$EA_BOT_HOME" PATH="$EA_BOT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

echo "[codex] 1/4 binary -> $EA_BOT_HOME/.local/codex-vendor"
as_bot mkdir -p "$EA_BOT_HOME/.local/bin" "$EA_BOT_HOME/.local/codex-vendor"
for f in "$BIN_DIR"/codex "$BIN_DIR"/codex-code-mode-host; do
    [ -f "$f" ] || continue
    as_bot sh -c "cat > '$EA_BOT_HOME/.local/codex-vendor/$(basename "$f")' && chmod 755 '$EA_BOT_HOME/.local/codex-vendor/$(basename "$f")'" < "$f"
done
for d in codex-resources codex-path; do
    [ -d "$EA_CODEX_SRC/$d" ] || continue
    tar -C "$EA_CODEX_SRC" -cf - "$d" | as_bot tar -C "$EA_BOT_HOME/.local/codex-vendor" -xf -
done
as_bot ln -sfn "$EA_BOT_HOME/.local/codex-vendor/codex" "$EA_BOT_HOME/.local/bin/codex"
as_bot codex --version

MODEL="${EA_CODEX_MODEL:-$(grep -E '^model\s*=' "$HOME/.codex/config.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*"([^"]+)".*/\1/')}"
MODEL="${MODEL:-gpt-5.6-sol}"
echo "[codex] 2/4 config (model $MODEL, no network in the sandbox)"
as_bot mkdir -p "$EA_BOT_HOME/.codex"
as_bot sh -c "cat > '$EA_BOT_HOME/.codex/config.toml'" <<CFG
# Written by deploy/enable-codex-ghost.sh. The ghost arm runs codex exec
# with its own sandbox flags; this file holds the model and the policy.
model = "$MODEL"
model_reasoning_effort = "medium"

[sandbox_workspace_write]
network_access = false

[projects."$EA_BOT_HOME/.local/state/ea-lanes"]
trust_level = "trusted"
CFG
as_bot chmod 600 "$EA_BOT_HOME/.codex/config.toml"

echo "[codex] 3/4 login as ea-bot (a device code; open the link in any browser)"
if as_bot codex login status 2>/dev/null | grep -q "Logged in"; then
    echo "  already logged in"
else
    as_bot codex login --device-auth
fi

echo "[codex] 4/4 probe"
# From the resident's own copy of the code: ea-bot cannot read the
# owner's checkout, and the lanes run from its copy anyway.
BOT_TREE="$EA_BOT_HOME/ea-system"
[ -r "$BOT_TREE/preprocessor/codex_cli.py" ] || BOT_TREE="/opt/ea-system"
as_bot sh -c "cd '$EA_BOT_HOME' && python3 '$BOT_TREE/preprocessor/codex_cli.py' probe"
echo "[codex] done. The ghost arm: python3 preprocessor/codex_ghost.py status"
