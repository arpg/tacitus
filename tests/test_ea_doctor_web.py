#!/usr/bin/env python3
"""scripts/ea-doctor.py on a web chat install (docs/webchat.md, Config).

With "frontend": "web" the doctor reports three rows, each a PASS or a
WARN with its fix: the settings (from the owner-private chat.json, a
port, a base path, a public_url with the chat's own HTTPS port under it),
the sign-in (the Tailscale login or a private pairing token) and the
store (a directory that is the owner's alone, outside the resident's
tree). Beside Zulip (shadow mode) it reports the Zulip checks and these.
In a container (EA_CONTAINER=docker, set by compose.yaml) the chat sits
behind a TLS proxy on a host name of its own: a public_url without a
port passes, a loopback bind warns, and the fixes name docker compose.
Every run is against scratch directories.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCTOR = REPO / "scripts" / "ea-doctor.py"
URL = "https://host.tail0000.ts.net:8444/chat"
LOGIN = "owner@example.test"


def _install(root: Path, block: dict | None, *, in_config: dict | None = None):
    """A scratch EA tree and private dir (0700) under `root`, with `block`
    as the owner-private chat.json (none when None) and `in_config` as
    ea-config.json's chat block."""
    ea, priv = root / "ea", root / "private"
    (ea / "config").mkdir(parents=True)
    priv.mkdir(mode=0o700)
    (ea / "config" / "ea-config.json").write_text(json.dumps(
        {"profile": "assistant", "chat": in_config or block or {}}))
    if block is not None:
        (priv / "chat.json").write_text(json.dumps(block))
        (priv / "chat.json").chmod(0o600)
    return ea, priv


def _rows(ea: Path, priv: Path, profile: str = "assistant", *, container: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "EA_CONTAINER"}
    env.update({"EA_DIR": str(ea), "EA_PRIVATE_DIR": str(priv)})
    if container:
        env["EA_CONTAINER"] = "docker"
    r = subprocess.run([sys.executable, str(DOCTOR), "--json", "--profile", profile],
                       capture_output=True, text=True, env=env, timeout=120)
    return {x["name"]: x for x in json.loads(r.stdout)["results"]}


def test_a_coherent_web_block_passes(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "web",
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    rows = _rows(ea, priv)
    for name in ("web chat: settings", "web chat: sign-in", "web chat: store"):
        assert rows[name]["status"] == "PASS", rows[name]
    assert "127.0.0.1:8470/chat" in rows["web chat: settings"]["detail"]
    assert LOGIN in rows["web chat: sign-in"]["detail"]
    assert "chat: frontend" not in rows                       # web is a known frontend
    assert rows["chat frontend"]["detail"] == "web"
    assert not [n for n in rows if n.startswith(("slack:", "secret:", "env: EA_SLACK"))]
    assert URL in rows["owner trust: chat settings"]["detail"]
    # a pairing token alone is a sign-in too
    (priv / "chat.json").write_text(json.dumps({"frontend": "web", "web": {"public_url": URL}}))
    (priv / "webchat-pair.token").write_text("t" * 43 + "\n")
    (priv / "webchat-pair.token").chmod(0o600)
    sign_in = _rows(ea, priv)["web chat: sign-in"]
    assert sign_in["status"] == "PASS" and "pairing token" in sign_in["detail"]


def test_each_incoherent_part_warns_with_its_fix(tmp_path):
    store_dir = tmp_path / "open"
    store_dir.mkdir(mode=0o755)
    store_dir.chmod(0o755)
    ea, priv = _install(tmp_path, {"frontend": "web", "web": {
        "public_url": "https://host.tail0000.ts.net/chat", "port": "eighty",
        "store_path": str(store_dir / "w.sqlite")}})
    rows = _rows(ea, priv)
    settings = rows["web chat: settings"]
    assert settings["status"] == "WARN"
    assert "no port" in settings["detail"] and "'eighty'" in settings["detail"]
    assert "8444" in settings["fix"] and "set web.port to 8470" in settings["fix"]
    sign_in = rows["web chat: sign-in"]
    assert sign_in["status"] == "WARN" and "tailscale whois" in sign_in["fix"]
    store = rows["web chat: store"]
    assert store["status"] == "WARN" and "0o755" in store["detail"]
    assert f"chmod 700 {store_dir}" in store["fix"]
    assert all(r["status"] != "FAIL" for n, r in rows.items() if n.startswith("web chat"))
    # a public_url under another path, or plain http
    for url, said in (("https://host.tail0000.ts.net:8444/other", "not the base_path"),
                      ("http://host.tail0000.ts.net:8444/chat", "not an https address")):
        (priv / "chat.json").write_text(json.dumps({"frontend": "web", "web": {
            "public_url": url, "tailscale_login": LOGIN}}))
        got = _rows(ea, priv)["web chat: settings"]
        assert got["status"] == "WARN" and said in got["detail"], (url, got)


def test_a_store_or_token_in_the_residents_tree_warns(tmp_path):
    ea = tmp_path / "ea"
    block = {"frontend": "web", "web": {
        "public_url": URL, "tailscale_login": LOGIN,
        "store_path": str(ea / "state" / "webchat.sqlite"),
        "pair_token_file": str(ea / "webchat-pair.token")}}
    ea, priv = _install(tmp_path, block)
    (ea / "webchat-pair.token").write_text("t" * 43)
    rows = _rows(ea, priv)
    assert rows["web chat: store"]["status"] == "WARN"
    assert "inside" in rows["web chat: store"]["detail"]
    assert "leave web.store_path empty" in rows["web chat: store"]["fix"]
    assert rows["web chat: sign-in"]["status"] == "WARN"
    assert "inside" in rows["web chat: sign-in"]["detail"]
    # a short or open token file is no sign-in either
    block["web"].pop("store_path")
    block["web"]["pair_token_file"] = str(priv / "short.token")
    (priv / "chat.json").write_text(json.dumps(block))
    (priv / "short.token").write_text("short")
    (priv / "short.token").chmod(0o600)
    got = _rows(ea, priv)["web chat: sign-in"]
    assert got["status"] == "WARN" and "shorter than 16" in got["detail"]
    (priv / "short.token").write_text("t" * 43)
    (priv / "short.token").chmod(0o644)
    got = _rows(ea, priv)["web chat: sign-in"]
    assert got["status"] == "WARN" and "0o644" in got["detail"] and "mode 600" in got["fix"]


def test_a_web_block_only_in_ea_config_warns_that_the_bot_will_not_serve_it(tmp_path):
    ea, priv = _install(tmp_path, None, in_config={"frontend": "web",
                                                "web": {"public_url": URL,
                                                        "tailscale_login": LOGIN}})
    rows = _rows(ea, priv)
    got = rows["web chat: settings"]
    assert got["status"] == "WARN" and "comes from ea-config.json" in got["detail"]
    assert "owner_trust.py seed --chat" in got["fix"]
    assert rows["owner trust: chat settings"]["status"] == "FAIL"


def test_shadow_mode_reports_zulip_and_the_web_chat(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "zulip", "shadow": "web",
                                "zulip": {"site": "https://zulip.example.test"},
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    rows = _rows(ea, priv)
    assert rows["chat frontend"]["detail"] == "zulip"
    assert "secret: ZULIP_API_KEY" in rows
    assert rows["web chat: settings"]["status"] == "PASS"
    assert rows["web chat: store"]["status"] == "PASS"


def test_the_email_profile_checks_no_web_chat(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "web", "web": {}})
    rows = _rows(ea, priv, profile="email")
    assert not [n for n in rows if n.startswith("web chat")]


def test_a_container_install_behind_a_tls_proxy(tmp_path):
    """The block deploy/docker/chat_seed.py writes: every interface of the
    container, the identity header off, a host name of its own on 443."""
    web = {"bind": "0.0.0.0", "port": 8470, "base_path": "/chat",
           "public_url": "https://chat.example.test/chat", "trust_tailscale_header": False}
    ea, priv = _install(tmp_path, {"frontend": "web", "shadow": "", "web": web})
    rows = _rows(ea, priv, container=True)
    settings = rows["web chat: settings"]
    assert settings["status"] == "PASS", settings
    assert "0.0.0.0:8470/chat" in settings["detail"] and "nothing else" in settings["detail"]
    sign_in = rows["web chat: sign-in"]
    assert sign_in["status"] == "WARN" and "docker compose" in sign_in["fix"]
    assert "tailscale" not in sign_in["fix"]
    (priv / "webchat-pair.token").write_text("t" * 43 + "\n")
    (priv / "webchat-pair.token").chmod(0o600)
    assert _rows(ea, priv, container=True)["web chat: sign-in"]["status"] == "PASS"
    # a host block in a container: loopback is the container's own
    (priv / "chat.json").write_text(json.dumps({"frontend": "web", "web": {
        "public_url": "https://chat.example.test/chat"}}))
    got = _rows(ea, priv, container=True)["web chat: settings"]
    assert got["status"] == "WARN" and "bind 127.0.0.1" in got["detail"]
    assert "0.0.0.0" in got["fix"] and "docker compose restart bot" in got["fix"]
    # the same block on a host still wants the chat's own port
    got = _rows(ea, priv)["web chat: settings"]
    assert got["status"] == "WARN" and "no port" in got["detail"]
    # no chat.json, a web block only in ea-config.json: the container's seed is the fix
    (priv / "chat.json").unlink()
    (ea / "config" / "ea-config.json").write_text(json.dumps(
        {"profile": "assistant", "chat": {"frontend": "web", "web": web}}))
    got = _rows(ea, priv, container=True)["web chat: settings"]
    assert got["status"] == "WARN" and "chat_seed.py web" in got["fix"]
