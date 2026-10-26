#!/usr/bin/env python3
"""resident_netpolicy: the resident's network policy, as nftables rules and probes.

The resident (`ea-bot`) runs the prompt-injectable LLM. On this host it
would otherwise meet every service that trusts "anything local": the
owner's dev servers, the board, Ollama, Zulip's web login, Samba, CUPS, the
Docker bridges and the tailnet peers. deploy/enable-resident-netpolicy.sh
loads the ruleset this module renders; scripts/boundary-audit.py checks every
day, with the probes below run as the resident, that it holds.

What the ruleset does to a packet whose socket belongs to the resident (an
nftables output chain matched on `meta skuid`; every other user passes):

  allow    ALLOWLIST: each entry below, with its reason.
  answer   replies on a connection that a loopback client opened to one of
           the resident's listeners (tailscale serve reaches the deck
           servers that way). A listener's answer to any other local
           address is refused, so a listener is no door from the LAN or the
           tailnet.
  refuse   every address of this host (fib daddr type local), broadcast and
           multicast; DENY4 and DENY6; the on-link prefixes of this host's
           interfaces; the NAT64 and 6to4 forms of every refused IPv4 range.
           The refusal is ICMP admin-prohibited, so a connect fails within a
           second (EHOSTUNREACH, or EACCES on IPv6) and a UDP send at once
           (EPERM); it is logged to the kernel log at a limited rate.
  pass     everything else: the internet stays open.

The allowlist, worked out from the code, skills and plugins:

  dns    udp and tcp 53 to each nameserver in /etc/resolv.conf (the
         systemd-resolved stub, 127.0.0.53, on a stock Ubuntu host): every
         lane resolves names through it.
  decks  tcp 127.0.0.1:<deck_serve.PORT_RANGE>: the EA's own deck servers.
         The deck plugin's PDF export and slide previews drive Chromium
         against them, and they bind 127.0.0.1 only.
  gateway tcp 127.0.0.1:<tool_gateway.PORT>: the owner's tool gateway, the
         on-prem model with tool calling, for a harness (Claude Code through
         ANTHROPIC_BASE_URL, opencode) the resident runs. Bearer token, no
         key on the resident's side.
  plugin:<name>  a local endpoint an enabled plugin declares in its
         manifest and the owner accepted (docs/plugins.md).

This host's tailnet addresses are refused like every other address of it.
tcp 443 there is tailscale serve, which fronts every route on the node (the
owner's own routes too), and an HTTP path is not something a packet filter
sees, so there is no narrower opening. build-deck.sh's closing check,
`curl https://<node>/<slug>/`, runs through scripts/deck-bin/curl, which
asks the mediated `deck_serve.py status` for the route and fetches the
deck's own loopback port (decks) instead.

Nothing else on this host is the resident's to reach. The mail mediator and
resident_io are a file queue and sudo children; chat posts go through the
owner's bot (a lane's stdout, the outbox); tailscaled's LocalAPI (`tailscale
serve status`), D-Bus, tmux and the resolver's varlink socket are Unix
sockets, which the policy does not touch. Every other service the EA uses
(Anthropic, Microsoft Graph behind the mediator, Google, GitHub, npm,
Hugging Face, and the hosts each plugin lists in its manifest) is on the
internet. An
IPv4-mapped IPv6 address needs no entry: the kernel sends it as IPv4, where
the IPv4 rules apply.

    resident_netpolicy.py render [--facts FILE]   the ruleset for this host (or for FILE's facts)
    resident_netpolicy.py facts                   this host's facts, as JSON
    resident_netpolicy.py drift --file PATH       what changed since PATH was rendered (exit 1 on drift)
    resident_netpolicy.py probe                   probe as the resident, one line per target
                                                  (exit 1 when a probe fails)

The probe program imports the standard library only. It opens one TCP
connection per target and closes it without sending a byte, sends one byte
to a UDP port nobody holds, and asks the resolver for `localhost`.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RESIDENT_USER = "ea-bot"
TABLE = "ea_resident"
RULES_FILE = Path("/etc/ea/resident-netpolicy.nft")
UNIT = "ea-resident-netpolicy.service"
RESOLV_CONF = Path("/etc/resolv.conf")
TAILNET_DEV = "tailscale0"
LOG_PREFIX = "ea-resident refused: "
FACTS_MARK = "# facts: "
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"

# Local IPv4 ranges the resident may not reach, each with its reason.
DENY4 = (
    ("0.0.0.0/8", "this network: a connect to 0.0.0.0 reaches this host"),
    ("10.0.0.0/8", "private (RFC 1918)"),
    ("100.64.0.0/10", "shared address space: the tailnet, 100.100.100.100 included"),
    ("127.0.0.0/8", "loopback"),
    ("169.254.0.0/16", "link-local, the cloud metadata address included"),
    ("172.16.0.0/12", "private (RFC 1918): the Docker bridges"),
    ("192.168.0.0/16", "private (RFC 1918)"),
    ("224.0.0.0/4", "multicast"),
    ("240.0.0.0/4", "reserved, and the limited broadcast 255.255.255.255"),
)
# Local IPv6 ranges, each with its reason.
DENY6 = (
    ("::/127", "unspecified and loopback: a connect to :: reaches this host"),
    ("64:ff9b:1::/48", "local-use NAT64 (RFC 8215)"),
    ("fc00::/7", "unique local: the tailnet (fd7a:115c:a1e0::/48) included"),
    ("fe80::/10", "link-local"),
    ("fec0::/10", "site-local (deprecated)"),
    ("ff00::/8", "multicast"),
)
NAT64 = ipaddress.IPv6Network("64:ff9b::/96")      # the well-known prefix (RFC 6052)
SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")   # RFC 3056

REASONS = {
    "dns": "name resolution through a nameserver in /etc/resolv.conf",
    "decks": "the EA's own deck servers (deck_serve.PORT_RANGE), which the deck "
             "plugin's PDF export and previews drive Chromium against",
    "gateway": "the owner's tool gateway (tool_gateway.py), the on-prem model with tool "
               "calling for a harness the resident runs",
}
COMMENTS = {
    "dns": "dns: a nameserver in /etc/resolv.conf",
    "decks": "decks: the EA's own deck servers",
    "gateway": "gateway: the owner's tool gateway for the on-prem model",
}

# Probe results, as the probe program names them.
REFUSED = frozenset({"EHOSTUNREACH", "EACCES", "EPERM"})     # the policy's refusal
REACHED = frozenset({"open", "ECONNREFUSED", "ECONNRESET"})   # a stack answered
NO_ROUTE = frozenset({"ENETUNREACH", "EADDRNOTAVAIL", "EAFNOSUPPORT", "EINVAL"})
FREE_PORTS = (9,) + tuple(range(47000, 47100))
# Addresses in local ranges that are not this host's: with the policy loaded a
# probe of one never leaves the host.
REMOTE_PROBES = (
    ("10.255.255.254", "private 10.0.0.0/8"),
    ("172.31.255.254", "private 172.16.0.0/12"),
    ("192.168.255.254", "private 192.168.0.0/16"),
    ("169.254.169.254", "link-local, the cloud metadata address"),
    ("100.100.100.100", "the tailnet's service address"),
)
TAILNET_V6_PROBE = ("fd7a:115c:a1e0::53", "the tailnet's IPv6 service address")
PROBE_TIMEOUT_S = 3
RUN_TIMEOUT_S = 60
MAX_OUTPUT = 256 * 1024


class ProbeError(Exception):
    """The probe did not run, or did not answer in the expected shape."""


# ── ranges ────────────────────────────────────────────────────────────

def _net(text: str):
    return ipaddress.ip_network(text, strict=False)


def v4_in_v6(net4: ipaddress.IPv4Network) -> list[ipaddress.IPv6Network]:
    """The NAT64 and 6to4 forms of an IPv4 range: a gateway that translates
    them delivers to the IPv4 range itself."""
    a = int(net4.network_address)
    return [ipaddress.IPv6Network((int(NAT64.network_address) | a, 96 + net4.prefixlen)),
            ipaddress.IPv6Network((int(SIX_TO_FOUR.network_address) | (a << 80),
                                   16 + net4.prefixlen))]


def static_ranges() -> tuple[list, list]:
    return [_net(n) for n, _ in DENY4], [_net(n) for n, _ in DENY6]


def _covered(net, ranges) -> bool:
    return any(net.version == r.version and net.subnet_of(r) for r in ranges)


def extra_onlink(prefixes) -> list:
    """The on-link prefixes that no static range covers (a public LAN, a
    global IPv6 prefix), collapsed."""
    s4, s6 = static_ranges()
    out4, out6 = [], []
    for p in prefixes:
        try:
            net = _net(p)
        except ValueError:
            continue
        if net.prefixlen == 0 or _covered(net, s4 + s6):
            continue
        (out4 if net.version == 4 else out6).append(net)
    return (list(ipaddress.collapse_addresses(out4))
            + list(ipaddress.collapse_addresses(out6)))


def deny_sets(facts: dict) -> tuple[list, list]:
    """The refused IPv4 and IPv6 ranges for these facts, collapsed, so the
    interval sets hold no overlapping element."""
    s4, s6 = static_ranges()
    extra = extra_onlink(facts.get("onlink", []))
    d4 = list(ipaddress.collapse_addresses(s4 + [n for n in extra if n.version == 4]))
    mapped = [m for n in d4 for m in v4_in_v6(n)]
    d6 = list(ipaddress.collapse_addresses(s6 + mapped + [n for n in extra if n.version == 6]))
    return d4, d6


def allowlist(facts: dict) -> list[dict]:
    """The allowed (address, protocol, port) entries; REASONS says why each
    name is on the list."""
    out = []
    for ns in facts.get("nameservers", []):
        for proto in ("udp", "tcp"):
            out.append({"name": "dns", "daddr": ns, "proto": proto, "ports": "53"})
    lo, hi = facts["deck_ports"]
    out.append({"name": "decks", "daddr": "127.0.0.1", "proto": "tcp", "ports": f"{lo}-{hi}"})
    if facts.get("gateway_port"):
        out.append({"name": "gateway", "daddr": "127.0.0.1", "proto": "tcp",
                    "ports": str(int(facts["gateway_port"]))})
    out.extend(_plugin_entries(facts))
    return out


_PLUGIN_ENTRY_NAME = re.compile(r"^plugin:[a-z0-9][a-z0-9-]{0,63}$")
_PLUGIN_PORTS = re.compile(r"^\d{1,5}(-\d{1,5})?$")


def _plugin_entries(facts: dict) -> list[dict]:
    """The local endpoints enabled plugins declare (docs/plugins.md), each
    checked again here: an IP address, tcp or udp, a port or a range, and a
    name of the form plugin:<name>."""
    out = []
    for e in facts.get("plugin_allows") or []:
        try:
            ipaddress.ip_address(str(e["daddr"]))
            name, proto, ports = str(e["name"]), str(e["proto"]), str(e["ports"])
        except (KeyError, TypeError, ValueError):
            continue
        if _PLUGIN_ENTRY_NAME.match(name) and proto in ("tcp", "udp") and _PLUGIN_PORTS.match(ports):
            out.append({"name": name, "daddr": str(e["daddr"]), "proto": proto, "ports": ports})
    return out


def _port_in(port: int, ports: str) -> bool:
    lo, _, hi = ports.partition("-")
    return int(lo) <= port <= int(hi or lo)


def allowed(facts: dict, host: str, proto: str, port: int) -> bool:
    """True when the allowlist passes the resident's packet to host:port."""
    ip = ipaddress.ip_address(host.partition("%")[0])
    return any(ipaddress.ip_address(e["daddr"]) == ip and e["proto"] == proto
               and _port_in(port, e["ports"]) for e in allowlist(facts))


def refused_range(facts: dict, host: str) -> bool:
    """True when the address is this host's or falls in a refused range."""
    ip = ipaddress.ip_address(host.partition("%")[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    local = {a["addr"].partition("%")[0] for a in facts.get("addresses", [])}
    if str(ip) in local:
        return True
    d4, d6 = deny_sets(facts)
    return any(ip in n for n in (d4 if ip.version == 4 else d6))


# ── the ruleset ───────────────────────────────────────────────────────

def _stored(facts: dict) -> dict:
    """The facts a rules file keeps, for the drift check."""
    return {"user": facts.get("user", RESIDENT_USER), "uid": int(facts["uid"]),
            "nameservers": list(facts.get("nameservers", [])),
            "onlink": [str(n) for n in extra_onlink(facts.get("onlink", []))],
            "deck_ports": list(facts["deck_ports"]),
            "gateway_port": int(facts.get("gateway_port") or 0),
            **({"plugin_allows": _plugin_entries(facts)} if facts.get("plugin_allows") else {})}


def _elements(nets) -> list[str]:
    items = [str(n) for n in nets]
    return ["\t\telements = {"] + [f"\t\t\t{x}," for x in items[:-1]] + [f"\t\t\t{items[-1]}", "\t\t}"]


def render(facts: dict) -> str:
    """The nftables file for these facts. It replaces the table in one
    transaction and touches nothing else in the ruleset."""
    stored = _stored(facts)
    uid = stored["uid"]
    d4, d6 = deny_sets(facts)
    allow = allowlist(facts)
    head = [
        "# The resident's network policy, generated by deploy/enable-resident-netpolicy.sh",
        "# from preprocessor/resident_netpolicy.py. A re-run replaces this file; change",
        "# those instead.",
        "#",
        f"# The resident is {stored['user']} (uid {uid}). Its connections to this host and to local",
        "# networks are refused, except the allowlist; its internet access stays open.",
        "#",
        "# allow:",
    ]
    for e in allow:
        why = REASONS.get(e["name"]) or f"an endpoint the {e['name'][len('plugin:'):]} plugin declares"
        head.append(f"#   {e['name']:<6} {e['proto']} {e['daddr']} port {e['ports']}: {why}")
    head += ["#   answer replies to loopback clients of the resident's listeners",
             "# refuse:",
             "#   this host's own addresses, broadcast and multicast (fib daddr type)"]
    head += [f"#   {n:<16} {why}" for n, why in DENY4 + DENY6]
    head += [f"#   {n:<16} on-link here" for n in stored["onlink"]]
    head += ["#   the NAT64 (64:ff9b::/96) and 6to4 (2002::/16) forms of every IPv4 range",
             FACTS_MARK + json.dumps(stored, sort_keys=True), ""]
    rules = []
    for e in allow:
        fam = "ip6" if ipaddress.ip_address(e["daddr"]).version == 6 else "ip"
        rules.append(f"\t\t{fam} daddr {e['daddr']} {e['proto']} dport {e['ports']} accept "
                     f"comment \"{COMMENTS.get(e['name'], e['name'])}\"")
    rules += [
        "\t\tct direction reply ip daddr 127.0.0.0/8 accept comment \"answers to loopback clients\"",
        "\t\tct direction reply ip6 daddr ::1 accept comment \"answers to loopback clients\"",
        "\t\tfib daddr type { local, broadcast, multicast } counter goto refuse "
        "comment \"this host's own addresses, broadcast, multicast\"",
        "\t\tip daddr @deny4 counter goto refuse comment \"local IPv4 ranges\"",
        "\t\tip6 daddr @deny6 counter goto refuse comment \"local IPv6 ranges\"",
    ]
    body = [
        f"table inet {TABLE} {{",
        "}",
        f"delete table inet {TABLE}",
        "",
        f"table inet {TABLE} {{",
        "\tset deny4 {", "\t\ttype ipv4_addr", "\t\tflags interval", *_elements(d4), "\t}",
        "",
        "\tset deny6 {", "\t\ttype ipv6_addr", "\t\tflags interval", *_elements(d6), "\t}",
        "",
        "\tchain output {",
        "\t\ttype filter hook output priority filter; policy accept;",
        f"\t\tmeta skuid {uid} jump resident",
        "\t}",
        "",
        "\tchain resident {",
        *rules,
        "\t}",
        "",
        "\tchain refuse {",
        f"\t\tlimit rate 10/minute burst 20 packets log prefix \"{LOG_PREFIX}\" level info",
        "\t\treject with icmpx admin-prohibited",
        "\t}",
        "}",
    ]
    return "\n".join(head + body) + "\n"


def parse(text: str) -> dict | None:
    """The facts a rules file was rendered from, or None."""
    for line in text.splitlines():
        if line.startswith(FACTS_MARK):
            try:
                data = json.loads(line[len(FACTS_MARK):])
            except ValueError:
                return None
            return data if isinstance(data, dict) and "uid" in data else None
    return None


def drift(stored: dict, live: dict) -> list[str]:
    """How this host differs from the facts a rules file was rendered from."""
    out = []
    if int(stored.get("uid", -1)) != int(live["uid"]):
        out.append(f"the rules name uid {stored.get('uid')}, the resident is uid {live['uid']}")
    for ns in live.get("nameservers", []):
        if ns not in stored.get("nameservers", []):
            out.append(f"the nameserver {ns} is not on the allowlist (name resolution fails)")
    s4, s6 = static_ranges()
    covered = [_net(n) for n in stored.get("onlink", [])] + s4 + s6
    for n in extra_onlink(live.get("onlink", [])):
        if not _covered(n, covered):
            out.append(f"the on-link prefix {n} is not refused")
    # A rules file that names tailnet addresses opens tcp 443 to them (an
    # older render); that opening is the drift.
    for addr in stored.get("tailnet", []):
        out.append(f"the rules allow tcp 443 to the tailnet address {addr} (every tailscale "
                   "serve route); re-run deploy/enable-resident-netpolicy.sh to close it")
    if list(stored.get("deck_ports", [])) != list(live["deck_ports"]):
        out.append(f"the deck ports are {live['deck_ports'][0]}-{live['deck_ports'][1]}, the rules "
                   f"allow {'-'.join(str(p) for p in stored.get('deck_ports', []))}")
    if int(stored.get("gateway_port") or 0) != int(live.get("gateway_port") or 0):
        out.append(f"the tool gateway's port is {live.get('gateway_port')}, the rules "
                   f"allow {stored.get('gateway_port') or 'none'}")
    want = {json.dumps(e, sort_keys=True) for e in _plugin_entries(live)}
    have = {json.dumps(e, sort_keys=True) for e in _plugin_entries(stored)}
    for e in sorted(want - have):
        out.append(f"the plugin endpoint {e} is not on the allowlist")
    for e in sorted(have - want):
        out.append(f"the allowlist names the plugin endpoint {e}, which no enabled plugin declares")
    return out


# ── this host ─────────────────────────────────────────────────────────

def nameservers(text: str) -> list[str]:
    """The nameservers of a resolv.conf, zone ids dropped, in order."""
    out = []
    for line in text.splitlines():
        words = line.split("#", 1)[0].split(";", 1)[0].split()
        if len(words) >= 2 and words[0] == "nameserver":
            host = words[1].partition("%")[0]
            try:
                host = str(ipaddress.ip_address(host))
            except ValueError:
                continue
            if host not in out:
                out.append(host)
    return out


def onlink_prefixes(routes: list) -> list[str]:
    """Destinations of the main table's routes that need no gateway, from
    `ip -j route` output: the networks this host reaches directly."""
    out = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        dst = r.get("dst")
        if (not dst or dst == "default" or r.get("gateway") or r.get("nexthops")
                or r.get("dev") == "lo" or r.get("type") not in (None, "unicast")):
            continue
        try:
            net = str(_net(dst))
        except ValueError:
            continue
        if net not in out:
            out.append(net)
    return out


def local_addresses(links: list) -> list[dict]:
    """[{dev, addr, scope, up}] for every address on this host's interfaces,
    from `ip -j addr` output; a link-local IPv6 address carries its zone."""
    out = []
    for link in links:
        if not isinstance(link, dict):
            continue
        dev = str(link.get("ifname") or "")
        up = str(link.get("operstate") or "") in ("UP", "UNKNOWN")
        for info in link.get("addr_info") or []:
            addr = info.get("local")
            if not addr or info.get("family") not in ("inet", "inet6"):
                continue
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip.version == 6 and ip.is_link_local:
                addr = f"{addr}%{dev}"
            out.append({"dev": dev, "addr": addr, "scope": str(info.get("scope") or ""), "up": up})
    return out


def facts_from(uid: int, resolv_text: str, routes: list, links: list,
               deck_ports=None, gateway_port=None, plugin_allows=None) -> dict:
    """The facts the ruleset and the probes need, from the resident's uid,
    resolv.conf text, `ip -j route` and `ip -j addr` output, the deck
    port range (deck_serve.PORT_RANGE when not given), the tool
    gateway's port (tool_gateway.PORT when not given) and the local
    endpoints enabled plugins declare (plugin_registry.netpolicy_allows()
    when not given; empty outside the owner's process, so a root render
    takes them from a facts file the owner wrote)."""
    if deck_ports is None:
        from deck_serve import PORT_RANGE
        deck_ports = PORT_RANGE
    if gateway_port is None:
        import tool_gateway
        gateway_port = tool_gateway.PORT
    if plugin_allows is None:
        try:
            import plugin_registry
            plugin_allows = plugin_registry.netpolicy_allows()
        except Exception:
            plugin_allows = []
    addrs = local_addresses(links)
    tailnet = [a["addr"] for a in addrs if a["dev"] == TAILNET_DEV and a["scope"] == "global"]
    facts = {"user": RESIDENT_USER, "uid": int(uid), "nameservers": nameservers(resolv_text),
             "onlink": onlink_prefixes(routes), "tailnet": tailnet,
             "addresses": [a for a in addrs if a["dev"] != "lo"],
             "deck_ports": [int(deck_ports[0]), int(deck_ports[1])],
             "gateway_port": int(gateway_port)}
    if plugin_allows:
        facts["plugin_allows"] = list(plugin_allows)
    return facts


def _ip_bin() -> str:
    for cand in ("/usr/sbin/ip", "/usr/bin/ip", "/sbin/ip", "/bin/ip"):
        if os.access(cand, os.X_OK):
            return cand
    return shutil.which("ip") or "ip"


def _ip_json(*args: str) -> list:
    try:
        r = subprocess.run([_ip_bin(), "-j", *args], capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return data if isinstance(data, list) else []


def system_facts() -> dict:
    """This host's facts: the resident's uid, the resolver, the on-link
    prefixes, the tailnet addresses, every local address, the deck ports.
    KeyError when there is no resident."""
    uid = pwd.getpwnam(RESIDENT_USER).pw_uid
    try:
        resolv = RESOLV_CONF.read_text(errors="replace")
    except OSError:
        resolv = ""
    routes = (_ip_json("-4", "route", "show", "table", "main")
              + _ip_json("-6", "route", "show", "table", "main"))
    return facts_from(uid, resolv, routes, _ip_json("addr", "show"))


def _hex_addr(text: str) -> str:
    """An address from /proc/net/{tcp,udp}{,6}: 32-bit words in host order."""
    raw = bytes.fromhex(text)
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)] if sys.byteorder == "little" \
        else [raw[i:i + 4] for i in range(0, len(raw), 4)]
    return str(ipaddress.ip_address(b"".join(words)))


def parse_sockets(text: str, listening_only: bool) -> list[tuple[str, int, int]]:
    """(address, port, uid) for each socket in /proc/net/{tcp,udp}{,6} text;
    TCP LISTEN sockets only when `listening_only`."""
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or (listening_only and f[3] != "0A"):
            continue
        addr, _, port = f[1].rpartition(":")
        try:
            out.append((_hex_addr(addr), int(port, 16), int(f[7])))
        except ValueError:
            continue
    return out


def sockets() -> dict:
    """{"tcp": TCP listeners, "udp": bound UDP sockets}, each (addr, port, uid)."""
    out = {"tcp": [], "udp": []}
    for proto, names in (("tcp", ("tcp", "tcp6")), ("udp", ("udp", "udp6"))):
        for n in names:
            try:
                text = Path(f"/proc/net/{n}").read_text()
            except OSError:
                continue
            out[proto] += parse_sockets(text, listening_only=proto == "tcp")
    return out


# ── probes ────────────────────────────────────────────────────────────

# Run as the resident with `python3 -I -c`. argv[1] is a JSON list of
# {id, host, port, proto, dns}; stdout is a JSON list of {id, result}.
PROBE = r'''
import errno, json, socket, sys
from concurrent.futures import ThreadPoolExecutor
QUERY = b"\xea\x5a\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x09localhost\x00\x00\x01\x00\x01"
def one(t):
    host = str(t["host"])
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    kind = socket.SOCK_STREAM if t["proto"] == "tcp" else socket.SOCK_DGRAM
    try:
        s = socket.socket(fam, kind)
    except OSError as e:
        return {"id": t["id"], "result": errno.errorcode.get(e.errno, "OSError")}
    s.settimeout(float(t.get("timeout", 3)))
    try:
        addr = socket.getaddrinfo(host, int(t["port"]), fam, kind, 0, socket.AI_NUMERICHOST)[0][4]
        s.connect(addr)
        if kind == socket.SOCK_DGRAM:
            s.send(QUERY if t.get("dns") else b"\0")
            if t.get("dns"):
                s.recv(512)
        result = "open"
    except socket.timeout:
        result = "timeout"
    except socket.gaierror:
        result = "EINVAL"
    except OSError as e:
        result = errno.errorcode.get(e.errno, "OSError")
    finally:
        s.close()
    return {"id": t["id"], "result": result}
targets = json.loads(sys.argv[1])
with ThreadPoolExecutor(max_workers=16) as pool:
    print(json.dumps(list(pool.map(one, targets))))
'''


def _free_port(taken: set[int]) -> int:
    return next(p for p in FREE_PORTS if p not in taken)


def probe_targets(facts: dict, socks: dict) -> list[dict]:
    """The probes for these facts. Each: host, port, proto, expect ("refused"
    or "reached"), kind ("host", "net" or "allow"), label, and dns for the
    resolver query. Refused probes use a port nobody listens on, so none of
    them reaches a service even when the policy is missing. This host's
    tailnet addresses are among its addresses, so they are probed (and
    refused) like the others; tcp 443 there is not opened."""
    tcp_free = _free_port({p for _, p, _ in socks.get("tcp", [])})
    udp_free = _free_port({p for _, p, _ in socks.get("udp", [])})
    out = []

    def add(host, port, proto, expect, kind, label, dns=False):
        out.append({"host": host, "port": port, "proto": proto, "expect": expect,
                    "kind": kind, "label": label, "dns": dns})

    for host, label in (("127.0.0.1", "loopback"), ("::1", "IPv6 loopback"),
                        ("0.0.0.0", "the unspecified address"), ("::", "the IPv6 unspecified address")):
        add(host, tcp_free, "tcp", "refused", "host", label)
    add("127.0.0.1", udp_free, "udp", "refused", "host", "loopback over UDP")
    # Every address of this host; of the IPv6 link-local ones, which every
    # container's veth carries, only the first on an interface that is up.
    seen, link_local = {"127.0.0.1", "::1"}, False
    for a in facts.get("addresses", []):
        if a["addr"] in seen:
            continue
        if "%" in a["addr"]:
            if link_local or not a.get("up", True):
                continue
            link_local = True
        seen.add(a["addr"])
        add(a["addr"], tcp_free, "tcp", "refused", "host", f"this host's address on {a['dev']}")
    for host, label in REMOTE_PROBES:
        add(host, tcp_free, "tcp", "refused", "net", label)
    if any(":" in t for t in facts.get("tailnet", [])):
        add(TAILNET_V6_PROBE[0], tcp_free, "tcp", "refused", "net", TAILNET_V6_PROBE[1])
    for ns in facts.get("nameservers", []):
        if refused_range(facts, ns):
            add(ns, 53, "tcp", "reached", "allow", "dns over TCP")
            add(ns, 53, "udp", "reached", "allow", "dns: a query for localhost", dns=True)
    lo, hi = facts["deck_ports"]
    ours = sorted(p for a, p, u in socks.get("tcp", [])
                  if lo <= p <= hi and u == int(facts["uid"]) and a in ("127.0.0.1", "0.0.0.0"))
    add("127.0.0.1", ours[0] if ours else lo, "tcp", "reached", "allow", "an EA deck port")
    return out


def probe_argv(targets: list[dict]) -> list[str]:
    """The interpreter command line that probes `targets` (no sudo part)."""
    wire = [{"id": i, "host": t["host"], "port": t["port"], "proto": t["proto"],
             "dns": bool(t.get("dns")), "timeout": PROBE_TIMEOUT_S}
            for i, t in enumerate(targets)]
    return [PYTHON, "-I", "-c", PROBE, json.dumps(wire, separators=(",", ":"))]


def _first(*cands: str) -> str:
    return next((c for c in cands if os.access(c, os.X_OK)), os.path.basename(cands[0]))


def as_resident_prefix() -> list[str]:
    """runuser as root, else sudo; the caller asks the sudo policy first."""
    if os.geteuid() == 0:
        return [_first("/usr/sbin/runuser", "/usr/bin/runuser", "/sbin/runuser"),
                "-u", RESIDENT_USER, "--"]
    return [_first("/usr/bin/sudo", "/bin/sudo"), "-n", "-u", RESIDENT_USER]


def run_probes(targets: list[dict], prefix: list[str]) -> list[str]:
    """The result of each probe, in order, from one run of PROBE as whoever
    `prefix` runs it as ([] runs it as this process's user)."""
    argv = list(prefix) + probe_argv(targets)
    try:
        r = subprocess.run(argv, capture_output=True, timeout=RUN_TIMEOUT_S, cwd="/",
                           env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(f"the probe did not run ({type(e).__name__})")
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeError(f"the probe exited {r.returncode}" + (f": {err[-1][:160]}" if err else ""))
    if len(r.stdout) > MAX_OUTPUT:
        raise ProbeError("the probe's answer is too long")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise ProbeError("the probe's answer is not JSON")
    got = {}
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict) and isinstance(d.get("id"), int) and isinstance(d.get("result"), str):
            got[d["id"]] = d["result"][:32]
    if len(got) != len(targets) or set(got) != set(range(len(targets))):
        raise ProbeError("the probe's answer does not cover every target")
    return [got[i] for i in range(len(targets))]


def verdict(expect: str, result: str, kind: str = "net") -> str:
    """"pass", "fail" or "unknown" for one probe; a refused target that has
    no route passes as "noroute" (it is not reached, and not evidence).
    For one of this host's own addresses (kind "host"), a timeout is a
    refusal: the probe port is free, so without the policy the kernel
    answers at once, and a link-local target's reject notice is not
    delivered back to the socket, so the policy's drop shows as a timeout."""
    if expect == "refused":
        if result in REFUSED:
            return "pass"
        if result in NO_ROUTE:
            return "noroute"
        if result == "timeout" and kind == "host":
            return "pass"
        if result in REACHED or result == "timeout":
            return "fail"
        return "unknown"
    if result in REACHED:
        return "pass"
    if result in REFUSED:
        return "fail"
    return "unknown"


def describe(t: dict, result: str) -> str:
    host = f"[{t['host']}]" if ":" in t["host"] else t["host"]
    how = {"timeout": "no answer: the packet left the host"}.get(result, result)
    return f"{t['proto']} {host}:{t['port']} ({t['label']}): {how}"


def judge(targets: list[dict], results: list[str]) -> dict:
    """The probes sorted into what failed and what could not be judged, and
    whether the policy shows: its refusal on loopback, where no network can
    answer in its place, and no probe of this host that got through."""
    out = {"deny_fail": [], "allow_fail": [], "deny_unknown": [], "allow_unknown": [],
           "noroute": 0, "host": 0, "net": 0, "allow": 0, "refused": 0}
    loopback, host_failed = [], False
    for t, r in zip(targets, results):
        v = verdict(t["expect"], r, t.get("kind", "net"))
        side = "allow" if t["expect"] == "reached" else "deny"
        out[t["kind"]] += 1
        out["refused"] += v == "pass" and side == "deny"
        if t["kind"] == "host" and t["host"] in ("127.0.0.1", "::1"):
            loopback.append(r in REFUSED)
        if v == "fail":
            host_failed = host_failed or t["kind"] == "host"
            out[f"{side}_fail"].append(describe(t, r))
        elif v == "unknown":
            out[f"{side}_unknown"].append(describe(t, r))
        elif v == "noroute":
            out["noroute"] += 1
    out["loaded"] = bool(loopback) and all(loopback) and not host_failed
    return out


def other_listeners(facts: dict, socks: dict) -> list[str]:
    """Listeners of other users on the loopback ports the allowlist opens."""
    lo, hi = facts["deck_ports"]
    uid = int(facts["uid"])
    return sorted({f"{a}:{p} (uid {u})" for a, p, u in socks.get("tcp", [])
                   if lo <= p <= hi and u != uid and a in ("127.0.0.1", "0.0.0.0", "::")})


# ── CLI ───────────────────────────────────────────────────────────────

def _load_facts(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text())
    return system_facts()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The resident's network policy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="print the nftables file")
    p.add_argument("--facts", help="a JSON file of facts instead of this host's")
    sub.add_parser("facts", help="print this host's facts")
    p = sub.add_parser("drift", help="compare a rules file with this host")
    p.add_argument("--file", default=str(RULES_FILE))
    sub.add_parser("probe", help="probe as the resident")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "render":
            sys.stdout.write(render(_load_facts(a.facts)))
            return 0
        if a.cmd == "facts":
            print(json.dumps(system_facts(), indent=1))
            return 0
        if a.cmd == "drift":
            stored = parse(Path(a.file).read_text())
            if stored is None:
                print(f"{a.file} names no facts")
                return 1
            lines = drift(stored, system_facts())
            print("\n".join(lines) if lines else "no drift: the rules match this host")
            return 1 if lines else 0
        facts = system_facts()
        socks = sockets()
        targets = probe_targets(facts, socks)
        results = run_probes(targets, as_resident_prefix())
    except KeyError:
        print(f"no {RESIDENT_USER} user", file=sys.stderr)
        return 1
    except (OSError, ValueError, ProbeError) as e:
        print(f"resident_netpolicy: {e}", file=sys.stderr)
        return 1
    bad = 0
    for t, r in zip(targets, results):
        v = verdict(t["expect"], r, t.get("kind", "net"))
        bad += v == "fail"
        print(f"{v:<8} expect {t['expect']:<8} {describe(t, r)}")
    for line in other_listeners(facts, socks):
        print(f"warn     another user listens on an allowed port: {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
