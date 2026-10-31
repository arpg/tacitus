#!/usr/bin/env python3
"""The bot container's health check: the web chat answers.

compose.yaml runs it every minute through the entrypoint's `health`
role, as the owner, from the owner's copy of the code. It reads the chat
block the bot reads (the owner-private chat.json). When that block runs
the web chat, alone or as the shadow, it asks the server on the
container's loopback for `<base>/api/me` with no credential and wants the
API's 401: the server is up, takes its own name in Host, and keeps the
owner check on. The answer carries no data, and the check reads no
token. A block with no web chat passes, since the bot then serves none.

A shadow the bot refused to serve (docs/webchat.md, Identity and auth:
a block, a store or a token that fails the owner checks) does not
answer, so the container reports unhealthy while the bot runs on its
primary alone; `docker compose logs bot` says why.

Exit 0 healthy, 1 unhealthy; one line on stdout either way, which
`docker inspect` keeps.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))

TIMEOUT_S = 5.0
# Where a bind address is reached from inside the container.
_LOCAL = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}


def probe_url(web: dict) -> tuple[str, str]:
    """(the URL of /api/me on this container's loopback, the Host the
    server answers to there) for the chat block's `web` section."""
    bind = str(web.get("bind") or "127.0.0.1")
    host = _LOCAL.get(bind, bind)
    port = int(web.get("port") or 8470)
    base = "/" + str(web.get("base_path") if web.get("base_path") is not None
                     else "/chat").strip().strip("/")
    base = "" if base == "/" else base
    name = f"[{host}]" if ":" in host else host
    return f"http://{name}:{port}{base}/api/me", f"{name}:{port}"


def check(web: dict, timeout: float = TIMEOUT_S) -> tuple[bool, str]:
    """(healthy, the line to print) for a web block the bot serves."""
    url, host = probe_url(web)
    req = urllib.request.Request(url, headers={"Host": host, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return False, f"web chat: {url} answered {r.status} with no credential; want 401"
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(4096) or b"{}")
        except ValueError:
            body = {}
        if e.code == 401 and body.get("error") == "not the owner":
            return True, f"web chat: {url} answers, owner check on"
        return False, f"web chat: {url} answered {e.code}; want 401"
    except (OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return False, (f"web chat: {url} does not answer ({reason}); "
                       "docker compose logs bot says why")


def main() -> int:
    import ea_config
    import owner_trust
    try:
        cfg, trusted = ea_config.get_chat_source()
    except owner_trust.OwnerTrustError as e:
        print(f"chat settings: {e}")
        return 1
    if "web" not in (cfg.get("frontend"), cfg.get("shadow")):
        print(f"no web chat in the chat block (frontend {cfg.get('frontend')}); nothing to check")
        return 0
    if not trusted:
        print("the chat block does not come from the owner-private chat.json, so the bot serves "
              "no web chat (docs/docker.md, Web chat)")
        return 1
    ok, line = check(cfg.get("web") if isinstance(cfg.get("web"), dict) else {})
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
