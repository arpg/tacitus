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
EA_BOT_HOME="$(getent passwd ea-bot | cut -d: -f6)"
[ -n "$EA_BOT_HOME" ] || { echo "no ea-bot user; run setup-ea-user.sh first" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${EA_CODEX_SRC:-}" ]; then
    OWNER_CODEX="$(command -v codex || true)"
    [ -n "$OWNER_CODEX" ] || { echo "codex is not on the owner's PATH; install it or set EA_CODEX_SRC" >&2; exit 1; }
    # The npm package's vendor directory: bin/codex beside codex-resources.
    REAL="$(readlink -f "$OWNER_CODEX")"
    EA_CODEX_SRC="$(dirname "$(dirname "$REAL")")"
    if [ ! -x "$EA_CODEX_SRC/bin/codex" ]; then
        EA_CODEX_SRC="$(dirname "$REAL")"
    fi
fi
[ -x "$EA_CODEX_SRC/bin/codex" ] || [ -x "$EA_CODEX_SRC/codex" ] || { echo "no codex binary under $EA_CODEX_SRC" >&2; exit 1; }
BIN_DIR="$EA_CODEX_SRC/bin"; [ -d "$BIN_DIR" ] || BIN_DIR="$EA_CODEX_SRC"

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
as_bot python3 "$SCRIPT_DIR/preprocessor/codex_cli.py" probe
echo "[codex] done. The ghost arm: python3 preprocessor/codex_ghost.py status"
