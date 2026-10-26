#!/usr/bin/env bash
# enable-deck-builder.sh: let the EA build and stage slide decks.
#
# The EA (the ea-bot user) authors Slidev decks in its own workspace against
# the owner's theme and playbooks, runs the owner's build-deck.sh from each
# deck, and serves the result on the tailnet. Everything ea-bot needs that it
# does not have by default is arranged here, and nothing more:
#
#   1. read-only bind mounts of the owner's presentations tree (and the figure
#      library) under /srv/ea, a root:ea 0750 directory, so the ea group can
#      read the theme, playbooks, scripts and finished decks while the owner's
#      home directory stays closed;
#   2. a copy of the owner's node for ea-bot at ~ea-bot/.local/node;
#   3. linger for ea-bot, so a deck's static server runs in ea-bot's own
#      systemd slice and survives a bot restart;
#   4. a restart of ea-mail-mediator, so it executes the deck_serve tool (the
#      tailnet route: the one step only the Tailscale operator may take).
#
# The deck tool itself (scaffold, lint, build, render) is a plugin, since
# every owner builds slides their own way; after this script, install one
# with ea-plugin.py and run its own init as ea-bot for the workspace and
# the toolchain template.
#
# Funnel is not granted anywhere: the mediator has no funnel verb, tailscaled
# takes writes only from root or the operator, and the deny list refuses the
# attempt. A deck the EA stages is tailnet-only.
#
#     sudo bash deploy/enable-deck-builder.sh            # enable / re-arm (idempotent)
#     sudo bash deploy/enable-deck-builder.sh --status
#     sudo bash deploy/enable-deck-builder.sh --rollback # unmount, drop fstab lines, disable linger
#
# Rollback leaves the EA's decks and its node copy in place (data), and the
# mediator keeps the tool in its table, which is inert without the mounts.
set -uo pipefail

REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(eval echo ~"$REAL_USER")"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
EA_BOT_USER=ea-bot
EA_BOT_HOME="$(eval echo ~$EA_BOT_USER 2>/dev/null || echo /home/ea-bot)"
DEPLOY="$EA_BOT_HOME/ea-system"
EA_DIR="$EA_BOT_HOME/.ea"
PRES_SRC="${EA_PRESENTATIONS_SRC:-$REAL_HOME/presentations}"
MOUNT_ROOT=/srv/ea
PRES_DST="$MOUNT_ROOT/presentations"
LIB_DST="$MOUNT_ROOT/presentation-library"
NODE_DST="$EA_BOT_HOME/.local/node"
FSTAB_TAG="# ea-deck-builder"
DECK_SERVE="$DEPLOY/preprocessor/deck_serve.py"
SHIM="$DEPLOY/scripts/deck-bin/tailscale"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }
need_root(){ [ "$(id -u)" = 0 ] || fail "run as root:  sudo bash $0 ${1:-}"; }

# The figure library: ~/presentations/library-index is a symlink chain that
# ends in a directory the owner keeps elsewhere; mount its parent.
lib_src(){
  local idx
  idx="$(readlink -f "$PRES_SRC/library-index" 2>/dev/null || true)"
  [ -n "$idx" ] && [ -d "$idx" ] && dirname "$idx"
}

ea_run(){ sudo -u "$EA_BOT_USER" env HOME="$EA_BOT_HOME" EA_DIR="$EA_DIR" \
            PATH=/usr/local/bin:/usr/bin:/bin bash -c "cd '$DEPLOY' && $*"; }

# The Slidev theme new decks use: `deck.theme` in ea-config.json. "default"
# is Slidev's built-in theme and needs no package in the mounted tree; any
# other name is the local package shared/theme-<name>, which ea-bot must be
# able to read. The deck plugin's tool applies the same rule.
deck_theme(){
  python3 - "$EA_DIR/config/ea-config.json" <<'PY'
import json, re, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    cfg = {}
deck = cfg.get("deck") if isinstance(cfg, dict) else None
theme = str((deck or {}).get("theme") or "default").strip() if isinstance(deck, dict) else "default"
print(theme if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", theme) else "default")
PY
}

# 0 when ea-bot can read the configured theme through the mount, or when
# the theme is Slidev's default; prints one line either way.
check_theme(){
  local theme
  theme="$(deck_theme)"
  if [ "$theme" = default ]; then
    grn "  theme: Slidev's default (deck.theme in ea-config.json); no local package needed"
    return 0
  fi
  if sudo -u "$EA_BOT_USER" test -r "$PRES_DST/shared/theme-$theme/package.json"; then
    grn "  theme: $theme (ea-bot reads $PRES_DST/shared/theme-$theme)"
  else
    red "  theme: $theme is deck.theme in ea-config.json, but ea-bot cannot read $PRES_DST/shared/theme-$theme/package.json"
    return 1
  fi
}

bind_ro(){  # $1 = source dir, $2 = mount point
  local src="$1" dst="$2"
  mkdir -p "$dst"
  if ! mountpoint -q "$dst"; then
    mount --bind "$src" "$dst" || return 1
  fi
  mount -o remount,bind,ro "$dst" || return 1
  if ! grep -qsF " $dst " /etc/fstab; then
    printf '%s\n%s %s none bind,ro,nofail 0 0\n' "$FSTAB_TAG" "$src" "$dst" >> /etc/fstab
  fi
}

unbind(){  # $1 = mount point
  local dst="$1"
  mountpoint -q "$dst" && umount "$dst"
  if grep -qsF " $dst " /etc/fstab; then
    # drop the tag line and the mount line that follows it
    python3 - "$dst" <<'PY'
import sys
dst = sys.argv[1]
lines = open("/etc/fstab").read().splitlines()
out, skip_tag = [], False
for i, ln in enumerate(lines):
    if ln.strip() == "# ea-deck-builder" and i + 1 < len(lines) and f" {dst} " in lines[i + 1]:
        continue
    if f" {dst} " in ln and "bind" in ln:
        continue
    out.append(ln)
open("/etc/fstab", "w").write("\n".join(out) + "\n")
PY
  fi
  rmdir "$dst" 2>/dev/null || true
}

install_node(){
  if [ -x "$NODE_DST/bin/node" ]; then
    grn "  node for $EA_BOT_USER: $("$NODE_DST/bin/node" --version) (present)"; return 0
  fi
  local node_bin ver src
  node_bin="$(ls -d "$REAL_HOME"/.nvm/versions/node/v*/bin/node 2>/dev/null | sort -V | tail -1)"
  [ -n "$node_bin" ] || node_bin="$(command -v node || true)"
  [ -n "$node_bin" ] || fail "no node found for $REAL_USER (nvm or PATH); install node first"
  src="$(dirname "$(dirname "$(readlink -f "$node_bin")")")"
  ver="$(basename "$src")"
  sudo -u "$EA_BOT_USER" mkdir -p "$EA_BOT_HOME/.local"
  tar -C "$(dirname "$src")" -cf - "$ver" | sudo -u "$EA_BOT_USER" tar -C "$EA_BOT_HOME/.local" -xf -
  sudo -u "$EA_BOT_USER" ln -sfn "$EA_BOT_HOME/.local/$ver" "$NODE_DST"
  [ -x "$NODE_DST/bin/node" ] || fail "node copy failed"
  grn "  node for $EA_BOT_USER: $("$NODE_DST/bin/node" --version) (copied from $src)"
}

status(){
  echo "== mounts =="
  for d in "$PRES_DST" "$LIB_DST"; do
    if mountpoint -q "$d"; then echo "  $d  $(findmnt -no OPTIONS "$d" | grep -o '\bro\b' || echo 'rw!')"; else echo "  $d  not mounted"; fi
  done
  echo "== theme =="
  check_theme || true
  echo "== can ea-bot write there? =="
  if sudo -u "$EA_BOT_USER" touch "$PRES_DST/.ea-write-probe" 2>/dev/null; then red "  YES (should be read-only)"; rm -f "$PRES_DST/.ea-write-probe"; else grn "  no (read-only)"; fi
  echo "== linger =="; echo "  $(loginctl show-user "$EA_BOT_USER" -p Linger 2>/dev/null || echo 'Linger=no (user not lingering)')"
  echo "== node =="; [ -x "$NODE_DST/bin/node" ] && echo "  $("$NODE_DST/bin/node" --version) at $NODE_DST" || echo "  missing"
  echo "== mediator =="; echo "  $(systemctl is-active ea-mail-mediator 2>/dev/null || echo 'not installed')"
  echo "== deck plugin (as ea-bot) =="
  local found=0 tool
  for tool in "$EA_DIR"/plugins/*/deck_tools.py; do
    [ -f "$tool" ] || continue
    found=1
    echo "  $tool"
    ea_run "python3 $tool doctor" 2>&1 | sed 's/^/  /' || true
  done
  [ "$found" = 1 ] || echo "  no deck plugin installed (ea-plugin.py install git:<url>)"
}

rollback(){
  need_root --rollback
  ylw "Rolling back the deck builder..."
  unbind "$PRES_DST"; unbind "$LIB_DST"
  rmdir "$MOUNT_ROOT" 2>/dev/null || true
  loginctl disable-linger "$EA_BOT_USER" 2>/dev/null || true
  grn "Rolled back: mounts removed, fstab lines dropped, linger off."
  echo "  The EA's decks ($EA_BOT_HOME/presentations) and its node copy were left in place."
  exit 0
}

enable(){
  need_root
  [ -f "$DECK_SERVE" ] || fail "deployed deck_serve.py missing at $DECK_SERVE; deploy first (scripts/deploy.sh)"
  [ -d "$PRES_SRC/playbooks" ] || fail "no presentations workspace at $PRES_SRC (set EA_PRESENTATIONS_SRC)"
  [ -f "$PRES_SRC/shared/scripts/build-deck.sh" ] || fail "$PRES_SRC/shared/scripts/build-deck.sh missing"
  id "$EA_BOT_USER" >/dev/null 2>&1 || fail "no $EA_BOT_USER user; run scripts/install/setup-ea-user.sh first"
  getent group ea >/dev/null || fail "no ea group; run scripts/install/setup-ea-user.sh first"

  ylw "[1/4] read-only mounts under $MOUNT_ROOT"
  mkdir -p "$MOUNT_ROOT"; chown root:ea "$MOUNT_ROOT"; chmod 0750 "$MOUNT_ROOT"
  bind_ro "$PRES_SRC" "$PRES_DST" || fail "could not bind-mount $PRES_SRC read-only at $PRES_DST"
  grn "  $PRES_DST -> $PRES_SRC (ro)"
  LIB_SRC="$(lib_src)"
  if [ -n "$LIB_SRC" ]; then
    bind_ro "$LIB_SRC" "$LIB_DST" || fail "could not bind-mount $LIB_SRC read-only at $LIB_DST"
    grn "  $LIB_DST -> $LIB_SRC (ro)"
  else
    ylw "  no figure library found behind $PRES_SRC/library-index; skipping (figure-find stays unavailable)"
  fi
  check_theme || fail "the configured theme is not readable through $PRES_DST"
  if sudo -u "$EA_BOT_USER" touch "$PRES_DST/.ea-write-probe" 2>/dev/null; then rm -f "$PRES_DST/.ea-write-probe"; fail "ea-bot can WRITE through $PRES_DST; the mount is not read-only"; fi
  grn "  ea-bot reads the tree and cannot write it"

  ylw "[2/4] node for $EA_BOT_USER"
  install_node

  ylw "[3/4] linger for $EA_BOT_USER (deck servers outlive a bot restart)"
  loginctl enable-linger "$EA_BOT_USER" || ylw "  enable-linger failed; builds will run unscoped"
  for _i in 1 2 3 4 5; do [ -S "/run/user/$(id -u "$EA_BOT_USER")/bus" ] && break; sleep 1; done
  if [ -S "/run/user/$(id -u "$EA_BOT_USER")/bus" ]; then grn "  user manager up"; else ylw "  user manager not up yet; builds run unscoped until it is"; fi

  ylw "[4/4] restart the mediator so it executes deck_serve"
  if systemctl is-enabled ea-mail-mediator >/dev/null 2>&1; then
    systemctl restart ea-mail-mediator; sleep 2
    systemctl is-active --quiet ea-mail-mediator || { journalctl -u ea-mail-mediator -n 20 --no-pager; fail "mediator did not come back"; }
    grn "  ea-mail-mediator active"
  else
    ylw "  ea-mail-mediator not installed (deploy/enable-mail-mediation.sh); the route verb has no executor yet"
  fi

  echo; ylw "self-tests"
  out="$(ea_run "timeout 60 python3 $DECK_SERVE status" 2>&1)"
  echo "$out" | grep -q '"verb": "status"' && grn "  deck_serve status answered through the mediator" \
    || { red "  deck_serve status did not answer: ${out:0:200}"; fail "route verb not reachable"; }
  out="$(ea_run "timeout 60 python3 $DECK_SERVE serve --slug ea-selftest --port 3069" 2>&1)"
  echo "$out" | grep -q 'nothing listens' && grn "  serve on an empty port refused owner-side (listener rule)" \
    || { red "  unexpected: ${out:0:200}"; fail "listener rule did not fire"; }
  out="$(ea_run "PATH=$DEPLOY/scripts/deck-bin:\$PATH tailscale funnel --https=443 on" 2>&1)"
  echo "$out" | grep -q refused && grn "  shim refuses funnel" || fail "shim did not refuse funnel: $out"
  out="$(ea_run "PATH=$DEPLOY/scripts/deck-bin:\$PATH tailscale serve status" 2>&1)"
  [ $? -eq 0 ] && grn "  shim passes serve status through" || fail "shim passthrough failed: $out"

  echo; grn "DECK HOSTING ENABLED: the mounts, node, linger and the route verb are in place."
  echo "  next: install a deck plugin (ea-plugin.py install git:<url>), then run its init"
  echo "        as $EA_BOT_USER for the workspace and the toolchain template"
  echo "  rollback: sudo bash $0 --rollback"; echo
  status
}

case "${1:-}" in
  --rollback) rollback ;;
  --status)   status ;;
  ""|--enable) enable ;;
  *) echo "usage: sudo bash $0 [--enable|--rollback|--status]"; exit 2 ;;
esac
