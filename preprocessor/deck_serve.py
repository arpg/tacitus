#!/usr/bin/env python3
"""deck_serve: the tailnet route for an EA-built deck, executed owner-side.

The EA (the ea-bot user) authors decks in its own workspace and runs their
static server itself. Adding or removing a `tailscale serve` route is a
tailscaled write, and tailscaled takes writes only from root or the
operator user (the owner). So the route is the one deck step that crosses the
user boundary, and it crosses it the way every other mediated verb does:
run as ea-bot, this CLI forwards through mail_mediation to the owner-side
mediator, which re-runs it as the owner; run as the owner, it validates and calls
tailscale.

Three verbs, all tailnet-only by construction (there is no funnel verb):

    deck_serve.py serve  --slug <slug> --port <port>   map https://<node>/<slug>/
    deck_serve.py off    --slug <slug>                 remove that route
    deck_serve.py status [--slug <slug>]               the routes, as JSON

The owner side refuses, whatever the caller says:
  * a slug that is not a plain lowercase kebab-case name;
  * a port outside PORT_RANGE, the EA's own range;
  * a target port with no LISTENING socket owned by ea-bot on 127.0.0.1,
    so a route can only ever point at the resident's own static server;
  * `serve` or `off` on a path whose current route is not the resident's
    (another user's staged deck keeps its route);
  * `serve` while any Funnel is enabled on the node. Funnel applies per
    host:port, so one funneled route would make every deck public;
  * `serve` on the HTTPS port the web chat uses (its `public_url`, or a
    route there to the chat's loopback port). One app per origin: a deck
    on the chat's origin could call the chat's API as the owner, so the
    chat has its own `--https=<port>` (docs/SETUP.md, Web chat).
After a `serve` the config is re-read; a funnel flag on the node removes
the new route again and fails the call.

Output is one JSON object on stdout. A refusal is {"status": "error", ...}
on stderr with exit 2, the mediator's own refusal shape.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__":
    from mail_mediation import forward_or_none
    forward_or_none("deck_serve", sys.argv[1:])

RESIDENT_USER = "ea-bot"
# The EA's decks take ports from here; the owner's own build-deck.sh default
# is 3033-3049, so the two users never pick the same port.
PORT_RANGE = (3050, 3069)
HTTPS_PORT = 443
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
PROC_NET_TCP = Path("/proc/net/tcp")
_LISTEN_STATE = "0A"
_LOOPBACK_OR_ANY = {"0100007F", "00000000"}


class Refused(Exception):
    """The request is outside what the EA may do to the node's routes."""


# --- tailscale ---------------------------------------------------------------

def _tailscale_bin() -> str:
    """The real CLI. Fixed locations first: this code runs owner-side, and a
    PATH lookup must never land on the EA's own shim."""
    for cand in ("/usr/bin/tailscale", "/usr/local/bin/tailscale"):
        if os.access(cand, os.X_OK):
            return cand
    found = shutil.which("tailscale")
    if not found:
        raise Refused("tailscale CLI not found")
    return found


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([_tailscale_bin(), *args], capture_output=True,
                          text=True, timeout=timeout)


def serve_config() -> dict:
    r = _run(["serve", "status", "--json"])
    if r.returncode != 0:
        raise Refused(f"tailscale serve status failed: {r.stderr.strip()[:200]}")
    text = (r.stdout or "").strip()
    if not text:
        return {}
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        raise Refused("tailscale serve status returned no JSON")
    return cfg if isinstance(cfg, dict) else {}


def dns_name() -> str:
    r = _run(["status", "--json"])
    if r.returncode != 0:
        raise Refused(f"tailscale status failed: {r.stderr.strip()[:200]}")
    try:
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except (KeyError, TypeError, ValueError):
        raise Refused("tailscale reports no DNS name (MagicDNS off?)")


# --- pure readers (unit-tested) ---------------------------------------------

def _proxy_port(proxy: str) -> int | None:
    m = re.match(r"^https?://(?:127\.0\.0\.1|localhost):(\d{1,5})(?:/|$)", proxy or "")
    return int(m.group(1)) if m else None


def routes(cfg: dict) -> dict[str, int | None]:
    """{'/slug': local port} for every path handler on the HTTPS port.
    A handler that is not a loopback proxy maps to None."""
    out: dict[str, int | None] = {}
    for hostport, web in (cfg.get("Web") or {}).items():
        if not str(hostport).endswith(f":{HTTPS_PORT}"):
            continue
        for path, handler in ((web or {}).get("Handlers") or {}).items():
            out[path] = _proxy_port((handler or {}).get("Proxy", ""))
    return out


def funnel_active(cfg: dict) -> bool:
    """True when any host:port on the node is opened to the public internet."""
    return any(bool(v) for v in (cfg.get("AllowFunnel") or {}).values())


def parse_listeners(text: str) -> dict[int, int]:
    """{port: uid} for every LISTEN socket bound to loopback or every address,
    from /proc/net/tcp text. The uid is the socket owner's, which is what
    ties a port to a user without root."""
    out: dict[int, int] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        local, state, uid = fields[1], fields[3], fields[7]
        if state != _LISTEN_STATE:
            continue
        addr, _, port_hex = local.partition(":")
        if addr not in _LOOPBACK_OR_ANY:
            continue
        try:
            out[int(port_hex, 16)] = int(uid)
        except ValueError:
            continue
    return out


def listening_uid(port: int) -> int | None:
    """The uid owning the LISTEN socket on `port`, or None."""
    try:
        text = PROC_NET_TCP.read_text()
    except OSError:
        return None
    return parse_listeners(text).get(port)


def resident_uid() -> int:
    try:
        return pwd.getpwnam(RESIDENT_USER).pw_uid
    except KeyError:
        raise Refused(f"no {RESIDENT_USER} user on this host")


# --- the rules (unit-tested with injected cfg / uid lookup) ------------------

def check_slug(slug: str) -> str:
    if not SLUG_RE.match(slug or ""):
        raise Refused(f"slug {slug!r} is not a lowercase kebab-case name")
    return slug


def check_port(port: int) -> int:
    lo, hi = PORT_RANGE
    if not isinstance(port, int) or not lo <= port <= hi:
        raise Refused(f"port {port!r} is outside the EA range {lo}-{hi}")
    return port


def is_resident_route(port: int | None, uid_of, resident: int) -> bool:
    """A route the resident may touch: an EA-range port whose listener is
    the resident, or an EA-range port nobody listens on any more (stale)."""
    if port is None or not PORT_RANGE[0] <= port <= PORT_RANGE[1]:
        return False
    uid = uid_of(port)
    return uid is None or uid == resident


def chat_web() -> tuple[int | None, int]:
    """(the HTTPS port of the web chat's public_url or None, the chat's
    loopback port) from the chat block this process trusts. A block that
    cannot be read gives (None, the default port): no web chat runs on it,
    and the route check still covers the default."""
    try:
        import ea_config
    except ModuleNotFoundError:
        from preprocessor import ea_config
    default = int(ea_config.DEFAULTS["chat"]["web"]["port"])
    try:
        block = ea_config.get_chat()
        web = block.get("web") or {}
    except Exception as e:
        print(f"deck_serve: the chat block could not be read ({e}); taking the "
              f"web chat's default port", file=sys.stderr)
        return None, default
    try:
        local = int(web.get("port") or default)
    except (TypeError, ValueError):
        local = default
    url = str(web.get("public_url") or "").strip()
    if not url or "web" not in (block.get("frontend"), block.get("shadow")):
        return None, local
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None, local
    if parts.scheme != "https":
        return None, local
    return (port if port is not None else 443), local


def check_chat_origin(cfg: dict, chat: tuple[int | None, int]) -> None:
    """Refuse a deck route on the web chat's origin: the chat's public
    port is HTTPS_PORT, or a route on HTTPS_PORT already proxies to the
    chat's loopback port."""
    public, local = chat
    rule = ("one app per origin: a page on the chat's origin can call the chat's API "
            "as the owner, so the web chat needs its own port (tailscale serve "
            f"--https=<port>, docs/SETUP.md) and decks stay on {HTTPS_PORT}")
    if public == HTTPS_PORT:
        raise Refused(f"the web chat's public_url is on https port {HTTPS_PORT}, the port "
                      f"decks use; {rule}")
    for path, target in routes(cfg).items():
        if target == local:
            raise Refused(f"{path} on https port {HTTPS_PORT} proxies to the web chat's "
                          f"port {local}; {rule}")


def validate_serve(slug: str, port: int, cfg: dict, uid_of, resident: int,
                   chat: tuple[int | None, int] | None = None) -> None:
    check_slug(slug)
    check_port(port)
    if chat is not None:
        check_chat_origin(cfg, chat)
    if funnel_active(cfg):
        raise Refused("a Funnel is enabled on this node, so a route added now "
                      "would be public; turn Funnel off first")
    uid = uid_of(port)
    if uid is None:
        raise Refused(f"nothing listens on 127.0.0.1:{port}; start the deck's "
                      "static server first")
    if uid != resident:
        raise Refused(f"the listener on 127.0.0.1:{port} belongs to uid {uid}, "
                      f"not {RESIDENT_USER}; a route may only point at the EA's "
                      "own server")
    existing = routes(cfg)
    path = f"/{slug}"
    if path in existing and existing[path] != port \
            and not is_resident_route(existing[path], uid_of, resident):
        raise Refused(f"{path} is already served by another user; pick another slug")


def validate_off(slug: str, cfg: dict, uid_of, resident: int) -> int | None:
    """The port the route currently proxies to, or None when there is no
    route (nothing to do). Refuses a route that is not the resident's."""
    check_slug(slug)
    existing = routes(cfg)
    path = f"/{slug}"
    if path not in existing:
        return None
    current = existing[path]
    # A handler that is present but is not a loopback proxy (or not on an
    # EA port with the resident's listener) is somebody else's.
    if not is_resident_route(current, uid_of, resident):
        raise Refused(f"{path} is served by another user; the EA does not take "
                      "down routes it did not create")
    return current


# --- verbs -------------------------------------------------------------------

def cmd_serve(slug: str, port: int) -> dict:
    check_slug(slug)          # the argument rules first: no tailscale call
    check_port(port)          # for a request that is malformed on its face
    resident = resident_uid()
    cfg = serve_config()
    validate_serve(slug, port, cfg, listening_uid, resident, chat_web())
    target = f"http://127.0.0.1:{port}/{slug}"
    r = _run(["serve", "--bg", f"--https={HTTPS_PORT}", f"--set-path=/{slug}", target])
    if r.returncode != 0:
        raise Refused(f"tailscale serve failed: {r.stderr.strip()[:300]}")
    after = serve_config()
    if funnel_active(after):
        # Not ours to switch off; it is the owner's setting. Withdraw the deck.
        _run(["serve", f"--https={HTTPS_PORT}", f"--set-path=/{slug}", "off"])
        raise Refused("a Funnel is enabled on this node; the route was removed "
                      "again so the deck is not public")
    if routes(after).get(f"/{slug}") != port:
        raise Refused(f"/{slug} is not in the serve config after the call")
    return {"status": "ok", "verb": "serve", "slug": slug, "port": port,
            "url": f"https://{dns_name()}/{slug}/", "tailnet_only": True}


def cmd_off(slug: str) -> dict:
    check_slug(slug)
    resident = resident_uid()
    cfg = serve_config()
    current = validate_off(slug, cfg, listening_uid, resident)
    if current is None:
        return {"status": "ok", "verb": "off", "slug": slug, "removed": False,
                "note": f"no route for /{slug}"}
    r = _run(["serve", f"--https={HTTPS_PORT}", f"--set-path=/{slug}", "off"])
    if r.returncode != 0:
        raise Refused(f"tailscale serve off failed: {r.stderr.strip()[:300]}")
    if f"/{slug}" in routes(serve_config()):
        raise Refused(f"/{slug} is still in the serve config after the call")
    return {"status": "ok", "verb": "off", "slug": slug, "removed": True,
            "port": current}


def cmd_status(slug: str | None) -> dict:
    resident = resident_uid()
    cfg = serve_config()
    rows = []
    for path, port in sorted(routes(cfg).items()):
        if slug and path != f"/{slug}":
            continue
        rows.append({"path": path, "port": port,
                     "resident": is_resident_route(port, listening_uid, resident),
                     "listening": port is not None and listening_uid(port) is not None})
    try:
        host = dns_name()
    except Refused:
        host = ""
    return {"status": "ok", "verb": "status", "dns_name": host,
            "funnel_active": funnel_active(cfg), "port_range": list(PORT_RANGE),
            "routes": rows}


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("serve", help="map https://<node>/<slug>/ to a local port", allow_abbrev=False)
    p.add_argument("--slug", required=True)
    p.add_argument("--port", required=True, type=int)
    p = sub.add_parser("off", help="remove the deck's route", allow_abbrev=False)
    p.add_argument("--slug", required=True)
    p = sub.add_parser("status", help="the routes on the node", allow_abbrev=False)
    p.add_argument("--slug")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mm_child.check_child_parse(ap, a)  # the verb the mediator checked
    try:
        if a.verb == "serve":
            out = cmd_serve(a.slug, a.port)
        elif a.verb == "off":
            out = cmd_off(a.slug)
        else:
            out = cmd_status(a.slug)
    except Refused as e:
        print(json.dumps({"status": "error", "message": str(e)}), file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(json.dumps({"status": "error", "message": "tailscale did not answer"}),
              file=sys.stderr)
        return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
