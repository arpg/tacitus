#!/usr/bin/env python3
"""The owner-private chat.json of a container install. Runs as the owner.

The bot takes its chat block only from `<private dir>/chat.json`
(preprocessor/owner_trust.py CHAT_FILE; docs/webchat.md, Config), which
lives on the ea-private volume. The entrypoint's setup role writes it
through this script, as the owner, and the owner runs it again for a
change later (docs/docker.md, Web chat):

  chat_seed.py write < block.json         the whole block, from stdin
  chat_seed.py web [--public-url URL] [--shadow]
                                          the web chat, alone, or with
                                          --shadow beside the front end
                                          chat.json names (Zulip, Slack)
  chat_seed.py relay --url URL [--install-id ID]
  chat_seed.py relay --off                the relay block in the web block
  chat_seed.py show                       the file as it stands

`--public-url` defaults to WEBCHAT_PUBLIC_URL, which compose passes from
its .env; an empty value keeps the address chat.json has, if any.

The web block a container needs (`web_block`): the server binds every
interface of the bot container, so a TLS proxy on the compose network,
or on the host through the port compose publishes on the host's
loopback, reaches it, while the relay client and the health check still
reach it on the container's own loopback. A proxied request comes from
the compose network, never from loopback, so it never counts as a
loopback client. No tailscaled runs in the stack, so the identity header
is off and the pairing token is the sign-in. The store and the token
keep their defaults beside chat.json in the private directory, where the
bot makes them mode 0600 in the 0700 directory. Every other key of an
existing block stays as it is.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))

import owner_trust  # noqa: E402

# The web chat inside the container: every interface, on the port compose
# publishes (WEBCHAT_PORT is the host's side), under the default base path.
WEB_BIND = "0.0.0.0"
WEB_PORT = 8470
WEB_BASE_PATH = "/chat"
ENV_PUBLIC_URL = "WEBCHAT_PUBLIC_URL"
_PLAIN_PATH = re.compile(r"(/[A-Za-z0-9._~-]+)*")


def log(msg: str) -> None:
    print(f"[chat_seed] {msg}", file=sys.stderr)


def check_public_url(url: str) -> tuple[str, str]:
    """(public_url, base_path) for the address the owner opens the chat
    at. The base path is the URL's path, `/chat` when it has none (and the
    URL then gains it): the proxy passes the path as sent, so the two
    agree. Raises ValueError for anything but a plain http(s) URL."""
    url = (url or "").strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError as e:
        raise ValueError(f"{url!r} is not a URL: {e}") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError(f"{url!r} is not an https:// address with a host name")
    if parts.username is not None or parts.query or parts.fragment:
        raise ValueError(f"{url!r} carries a user, a query or a fragment")
    path = parts.path.rstrip("/")
    if not _PLAIN_PATH.fullmatch(path):
        raise ValueError(f"{url!r}: the path {parts.path!r} is not a plain path")
    if not path:
        path = WEB_BASE_PATH
        url = f"{url}{path}"
    return url, path


def web_block(current: dict | None, public_url: str = "") -> dict:
    """The `web` block for the container: `current` with the bind, port,
    base path, public_url and the identity header set; every other key
    (relay, notify, push_subject, backfill_days) kept. An empty public_url
    keeps the one the block has, if any."""
    web = json.loads(json.dumps(current)) if isinstance(current, dict) else {}
    web.update({"bind": WEB_BIND, "port": WEB_PORT, "trust_tailscale_header": False})
    if public_url:
        url, base = check_public_url(public_url)
        web.update({"public_url": url, "base_path": base})
    else:
        web.setdefault("base_path", WEB_BASE_PATH)
    return web


def chat_block(current: dict | None, public_url: str = "", *, shadow: bool = False) -> dict:
    """The chat block with the container's web chat: the only front end
    (`"frontend": "web"`), or with `shadow` the mirror of the one the block
    names already."""
    block = json.loads(json.dumps(current)) if isinstance(current, dict) else {}
    if shadow:
        primary = str(block.get("frontend") or "")
        if primary in ("", "web"):
            raise ValueError("--shadow mirrors the front end chat.json names (zulip or slack); "
                             "it names none")
        block["shadow"] = "web"
    else:
        block["frontend"] = "web"
        block["shadow"] = ""
    block["web"] = web_block(block.get("web"), public_url)
    return block


def relay_block(block: dict, url: str = "", install_id: str = "", *, off: bool = False) -> dict:
    """The chat block with the relay (docs/webchat-relay.md, Config) in its
    web block: `url` and an optional `install_id` pin; the key file keeps
    its default beside the store. `off` removes the relay block."""
    block = json.loads(json.dumps(block))
    web = block.get("web") if isinstance(block.get("web"), dict) else {}
    if off:
        web.pop("relay", None)
    else:
        parts = urllib.parse.urlsplit((url or "").strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"the relay url {url!r} is not an https:// address")
        relay = web.get("relay") if isinstance(web.get("relay"), dict) else {}
        relay = {**relay, "url": url.strip().rstrip("/")}
        relay.pop("enabled", None)
        if install_id:
            if not re.fullmatch(r"[a-z2-7]{26}", install_id):
                raise ValueError(f"{install_id!r} is not an install id (26 base32 characters)")
            relay["install_id"] = install_id
        web["relay"] = relay
    block["web"] = web
    return block


def chat_path() -> Path:
    return owner_trust.private_dir() / owner_trust.CHAT_FILE


def refuse_others(priv: Path) -> None:
    """The file is the owner's: this runs only as the user that owns the
    private directory, never as root (a root-owned chat.json is one the
    bot refuses)."""
    try:
        st = os.lstat(priv)
    except OSError as e:
        raise SystemExit(f"chat_seed: {priv}: {e.strerror or e}")
    if os.geteuid() == 0 or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise SystemExit(f"chat_seed: run it as the owner of {priv} (docker compose run --rm "
                         f"owner ...), not as uid {os.geteuid()}")


def read(path: Path) -> dict | None:
    """The block in `path` after the owner checks; None when there is none."""
    try:
        block = json.loads(owner_trust.read_owner_file(path))
    except FileNotFoundError:
        return None
    if not isinstance(block, dict):
        raise SystemExit(f"chat_seed: {path} does not hold a JSON object")
    return block


def write(path: Path, block: dict) -> None:
    """`block` into `path`, mode 0600, through the owner checks on the
    directory and on any file there (owner_trust.write_owner_file)."""
    owner_trust.write_owner_file(path, json.dumps(block, indent=2) + "\n", mode=0o600)
    log(f"chat settings -> {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", type=Path, default=None,
                    help="the chat.json to write (default: the private directory's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("write", help="the whole block, a JSON object on stdin")
    w = sub.add_parser("web", help="the container's web chat")
    w.add_argument("--public-url", default=None,
                   help=f"https://<host>/chat (default: ${ENV_PUBLIC_URL}; empty: none yet)")
    w.add_argument("--shadow", action="store_true",
                   help="mirror the front end chat.json names instead of replacing it")
    r = sub.add_parser("relay", help="the relay block (docs/webchat-relay.md)")
    r.add_argument("--url", default="")
    r.add_argument("--install-id", default="")
    r.add_argument("--off", action="store_true")
    sub.add_parser("show", help="print chat.json")
    a = ap.parse_args(argv)
    path = a.path or chat_path()
    refuse_others(path.parent)
    try:
        current = read(path)
    except owner_trust.OwnerTrustError as e:
        log(str(e))
        return 1
    try:
        if a.cmd == "show":
            print(json.dumps(current or {}, indent=2))
            return 0
        if a.cmd == "write":
            block = json.load(sys.stdin)
            if not isinstance(block, dict):
                raise ValueError("stdin must hold the chat block as a JSON object")
        elif a.cmd == "web":
            url = a.public_url if a.public_url is not None else os.environ.get(ENV_PUBLIC_URL, "")
            block = chat_block(current, url, shadow=a.shadow)
            web = block["web"]
            if not web.get("public_url"):
                log(f"no public_url yet: set {ENV_PUBLIC_URL} in compose's .env and run "
                    "this again (docs/docker.md, Web chat)")
            elif not web["public_url"].startswith("https://"):
                log(f"{web['public_url']} is not https: the browser keeps the pairing cookie "
                    "only over https, and the app installs and takes notifications only there")
        else:
            if current is None or "web" not in (current.get("frontend"), current.get("shadow")):
                raise ValueError(f"{path} runs no web chat: run `chat_seed.py web` first")
            if not a.off and not a.url:
                raise ValueError("relay takes --url https://<relay> or --off")
            block = relay_block(current, a.url, a.install_id, off=a.off)
    except ValueError as e:
        log(str(e))
        return 2
    try:
        write(path, block)
    except owner_trust.OwnerTrustError as e:
        log(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
