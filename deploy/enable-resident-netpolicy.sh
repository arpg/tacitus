#!/usr/bin/env bash
#
# enable-resident-netpolicy.sh: a per-user network policy for the ea-bot
# service account. Run once, as root:  sudo bash deploy/enable-resident-netpolicy.sh
#
# The resident (ea-bot) runs the prompt-injectable LLM. On this host an
# injected turn would otherwise meet every service that trusts "anything
# local": the owner's dev servers, the board, Ollama, Zulip's web login,
# Samba, CUPS, the Docker bridges and the tailnet peers. This loads an
# nftables table (inet ea_resident) whose output chain matches the resident's
# sockets (meta skuid) and refuses their connections to this host and to
# local networks, IPv4 and IPv6, except an allowlist of what the EA needs.
# The internet stays open, and no other user's traffic is touched.
#
# preprocessor/resident_netpolicy.py renders the rules and names every range
# and every allowlist entry with its reason. The allowlist:
#   dns    udp and tcp 53 to each nameserver in /etc/resolv.conf
#   decks  tcp 127.0.0.1:3050-3069, the EA's own deck servers (deck_serve.PORT_RANGE)
#   gateway tcp 127.0.0.1 on the tool gateway's port
# This host's tailnet addresses are refused like the rest of it: tcp 443
# there fronts every tailscale serve route, the owner's too. build-deck.sh's
# closing check runs through scripts/deck-bin/curl, which fetches the deck's
# loopback port after a mediated route check. A listener of the resident
# answers loopback clients only (tailscale serve reaches the deck servers
# there). A refused connect fails within a second
# (ICMP admin-prohibited), and the kernel log records it at a limited rate.
#
# Persistent: the rules file /etc/ea/resident-netpolicy.nft and the oneshot
# unit ea-resident-netpolicy.service, which loads it before network-pre.target
# at every boot. The table is the policy's own and nothing else in the
# ruleset is touched, so Docker's and Tailscale's rules stay as they are. Do
# not enable the stock nftables.service with the shipped /etc/nftables.conf:
# it starts with `flush ruleset`, which removes this table and Docker's rules
# alike (the unit orders itself after nftables.service, so a boot still ends
# with the table loaded).
#
# The rules name what this host has when the script runs: the resolver and
# the on-link prefixes. Re-run it after one of those
# changes; --status and the daily boundary audit report the drift.
#
# Owner-agnostic: it depends on the ea-bot account and on this host, not on
# who installed the EA. Idempotent: a re-run renders again and replaces the
# table in one nft transaction (systemctl reload), with no moment unfiltered.
# Reversible: --rollback removes the table, the unit and the rules file.
#
#     sudo bash deploy/enable-resident-netpolicy.sh             # install or refresh
#     sudo bash deploy/enable-resident-netpolicy.sh --status    # unit, table, counters, drift, probes
#     sudo bash deploy/enable-resident-netpolicy.sh --rollback  # remove it all
#     bash deploy/enable-resident-netpolicy.sh --print [--facts FILE]
#                                                  # the rules it would load; no root, changes nothing
#
set -euo pipefail

RESIDENT=ea-bot
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY_PY="$REPO_DIR/preprocessor/resident_netpolicy.py"
RULES_DIR=/etc/ea
RULES="$RULES_DIR/resident-netpolicy.nft"
UNIT_NAME=ea-resident-netpolicy.service
UNIT="/etc/systemd/system/$UNIT_NAME"
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3 || true)"
TMP=""
trap '[ -z "$TMP" ] || rm -f -- "$TMP"' EXIT

say(){ echo "[netpolicy] $*"; }
die(){ echo "[netpolicy] $*" >&2; exit 1; }
need_root(){
    [ "$(id -u)" -eq 0 ] || die "run as root: sudo bash deploy/enable-resident-netpolicy.sh ${1:-}"
}
policy(){ "$PY" -I "$POLICY_PY" "$@"; }

# Root runs the renderer from this checkout, so the checkout must not be one
# the resident can replace: nothing under its home.
trusted_repo(){
    local home
    [ -n "$PY" ] && [ -x "$PY" ] || die "python3 is required"
    [ -f "$POLICY_PY" ] || die "missing $POLICY_PY"
    home="$(getent passwd "$RESIDENT" | cut -d: -f6 || true)"
    [ -n "$home" ] || die "the $RESIDENT user does not exist (run scripts/install/setup-ea-user.sh first)"
    case "$(readlink -f "$REPO_DIR")/" in
        "$(readlink -f "$home")"/*)
            die "refusing to run code from $REPO_DIR, which is in the resident's home;" \
                "run the script from the owner's checkout or /opt/ea-system" ;;
    esac
}

nft_bin(){ command -v nft || die "nft is not installed (apt install nftables)"; }

write_unit(){  # $1 = the nft binary
    cat > "$UNIT" <<EOF
[Unit]
Description=EA resident network policy (nftables table inet ea_resident for $RESIDENT)
Documentation=file:$RULES
DefaultDependencies=no
Wants=network-pre.target
Before=network-pre.target
After=local-fs.target nftables.service
ConditionPathExists=$RULES

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$1 -f $RULES
ExecReload=$1 -f $RULES
ExecStop=-$1 delete table inet ea_resident

[Install]
WantedBy=sysinit.target
EOF
    chmod 644 "$UNIT"
}

enable(){
    need_root
    trusted_repo
    local nft
    nft="$(nft_bin)"
    command -v systemctl >/dev/null || die "systemd is required for the boot-time unit"
    say "resident=$RESIDENT (uid $(id -u "$RESIDENT"))  rules=$RULES  unit=$UNIT_NAME"

    say "1/4  render and check the rules"
    TMP="$(mktemp)"
    policy render > "$TMP" || die "rendering failed; nothing changed"
    "$nft" -c -f "$TMP" || die "nft refused the rendered rules; nothing changed"
    sed -n 's/^# \{1,3\}\(dns\|decks\|gateway\|answer\) /  allow \1 /p' "$TMP"

    say "2/4  install $RULES"
    install -d -o root -g root -m 755 "$RULES_DIR"
    install -o root -g root -m 644 "$TMP" "$RULES"

    say "3/4  load it now and at every boot ($UNIT_NAME)"
    write_unit "$nft"
    systemctl daemon-reload
    systemctl enable "$UNIT_NAME" >/dev/null
    # A reload replaces the table in one transaction; a restart would stop
    # (delete the table) before it starts again.
    if systemctl is-active --quiet "$UNIT_NAME"; then
        systemctl reload "$UNIT_NAME"
    else
        systemctl start "$UNIT_NAME"
    fi
    "$nft" list table inet ea_resident >/dev/null || die "the table is not loaded; see journalctl -u $UNIT_NAME"

    say "4/4  probe as $RESIDENT (a connect per target, no bytes sent; one resolver query)"
    if policy probe | sed 's/^/  /'; then
        say "done. $RESIDENT reaches the internet and its allowlist; this host and local networks refuse it."
        say "the daily boundary audit (scripts/boundary-audit.py) checks it from now on."
        say "status: sudo bash $0 --status    rollback: sudo bash $0 --rollback"
    else
        say "WARNING: a probe failed (above). The table is loaded. If the EA's own needs are refused," >&2
        say "  roll back with: sudo bash $0 --rollback" >&2
        exit 1
    fi
}

status(){
    need_root --status
    trusted_repo
    echo "== unit =="
    echo "  $UNIT_NAME: $(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)," \
         "$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
    echo "== table (counters) =="
    if nft list chain inet ea_resident resident >/dev/null 2>&1; then
        nft list chain inet ea_resident resident | sed 's/^/  /'
    else
        echo "  not loaded"
    fi
    echo "== rules file =="
    if [ -f "$RULES" ]; then
        stat -c '  %n  %U:%G %a  %y' "$RULES"
        policy drift --file "$RULES" | sed 's/^/  /' || true
    else
        echo "  $RULES missing"
    fi
    echo "== probes as $RESIDENT =="
    policy probe | sed 's/^/  /' || true
    echo "== recent refusals (kernel log) =="
    journalctl -k -q --no-pager -g 'ea-resident refused' -n 10 2>/dev/null | sed 's/^/  /' || true
}

rollback(){
    need_root --rollback
    say "rolling back the resident's network policy"
    systemctl disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
    if command -v nft >/dev/null && nft list table inet ea_resident >/dev/null 2>&1; then
        nft delete table inet ea_resident
    fi
    rm -f "$UNIT" "$RULES"
    rmdir "$RULES_DIR" 2>/dev/null || true
    systemctl daemon-reload
    if command -v nft >/dev/null && nft list table inet ea_resident >/dev/null 2>&1; then
        die "the table inet ea_resident is still loaded"
    fi
    say "done: the table, the unit and $RULES are gone; $RESIDENT's connections are as before."
}

case "${1:-}" in
    ""|--enable) enable ;;
    --status)    status ;;
    --rollback)  rollback ;;
    --print)     shift; [ "$(id -u)" -ne 0 ] || trusted_repo; policy render "$@" ;;
    *) echo "usage: sudo bash $0 [--enable|--status|--rollback] | bash $0 --print [--facts FILE]" >&2; exit 2 ;;
esac
