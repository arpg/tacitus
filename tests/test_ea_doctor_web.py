#!/usr/bin/env python3
"""scripts/ea-doctor.py on a web chat install (docs/webchat.md, Config).

With "frontend": "web" the doctor reports its web chat rows, each a PASS
or a WARN with its fix: the settings (from the owner-private chat.json, a
port, a base path, a public_url with the chat's own HTTPS port under it),
the sign-in (the Tailscale login or a private pairing token) and the
store (a directory that is the owner's alone, outside the resident's
tree, and once it exists its size, message count and newest message).
Then the nightly snapshot (warns past two days), the push subscriptions
(warns on a browser that keeps failing) and the relay as the running bot
reports it (warns when it has not been connected for ten minutes, and
while a device waits for approval). Beside Slack (shadow mode) it reports
the Slack checks and these.
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
    assert rows["chat frontend"]["status"] == "PASS"           # web is a known frontend
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


def test_shadow_mode_reports_slack_and_the_web_chat(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "slack", "shadow": "web",
                                "slack": {"owner_user_id": "U0OWNER01"},
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    rows = _rows(ea, priv)
    assert rows["chat frontend"]["detail"] == "slack"
    assert "secret: SLACK_BOT_TOKEN" in rows and "env: EA_SLACK_OWNER_ID" in rows
    assert rows["web chat: settings"]["status"] == "PASS"
    assert rows["web chat: store"]["status"] == "PASS"


def test_the_reviewer_mode_checks_no_web_chat(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "web", "web": {}})
    rows = _rows(ea, priv, profile="reviewer")
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


# The rows on what the web chat holds: the store's contents, the nightly
# snapshot, the push subscriptions and the relay as the running bot sees it.

sys.path.insert(0, str(REPO / "botcore"))
TOKEN = "t" * 43


def _store(priv: Path, *, messages: int = 3, subs: tuple = ()) -> Path:
    """A store under `priv` with `messages` bot messages and the push
    subscriptions `subs` ((last_ok, failures) each)."""
    import time as _time
    from botcore.chat.web_frontend import WebStore
    path = priv / "webchat.sqlite"
    store = WebStore(path)
    store.upsert_channel("main", "ea", "main")
    for n in range(messages):
        store.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                          text=f"note {n}", posted_at=_time.time() - 600 + n)
    for i, (last_ok, failures) in enumerate(subs):
        store.push_subscribe(f"https://fcm.googleapis.com/fcm/send/x{i}",
                             {"p256dh": "k", "auth": "a"})
        with store._conn:
            store._conn.execute("UPDATE push_subscriptions SET last_ok=?, failures=? "
                                "WHERE endpoint=?", (last_ok, failures,
                                                     f"https://fcm.googleapis.com/fcm/send/x{i}"))
    store.close()
    return path


def _snapshot(priv: Path, name: str, age_s: float = 0.0) -> Path:
    import time as _time
    root = priv / "backups" / "webchat"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (priv / "backups").chmod(0o700)
    root.chmod(0o700)
    snap = root / name
    snap.mkdir(mode=0o700)
    manifest = snap / "MANIFEST.tsv"
    manifest.write_text("webchat.sqlite\tx\n")
    t = _time.time() - age_s
    os.utime(manifest, (t, t))
    return snap


def test_the_store_row_says_what_it_holds_and_the_backup_row_its_age(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "web",
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    _store(priv, messages=4)
    rows = _rows(ea, priv)
    store = rows["web chat: store"]
    assert store["status"] == "PASS", store
    assert "4 messages" in store["detail"] and "the last" in store["detail"]
    assert "mode 0600" in store["detail"] and "KB" in store["detail"]
    backup = rows["web chat: backup"]
    assert backup["status"] == "WARN" and "no snapshot" in backup["detail"]
    assert "webchat-backup.sh" in backup["fix"] and "Cron schedule" in backup["fix"]
    _snapshot(priv, "2026-01-01", age_s=3 * 86400)
    backup = _rows(ea, priv)["web chat: backup"]
    assert backup["status"] == "WARN" and "3 days old" in backup["detail"], backup
    _snapshot(priv, "2026-01-02-cutover", age_s=3600)
    backup = _rows(ea, priv)["web chat: backup"]
    assert backup["status"] == "PASS" and "2026-01-02-cutover" in backup["detail"], backup
    assert "2 snapshot(s)" in backup["detail"]


def test_no_store_yet_has_no_backup_or_push_row(tmp_path):
    ea, priv = _install(tmp_path, {"frontend": "web",
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    rows = _rows(ea, priv)
    assert "made on the bot start" in rows["web chat: store"]["detail"]
    assert "web chat: backup" not in rows and "web chat: push" not in rows
    assert rows["web chat: relay"]["status"] == "PASS"
    assert "not configured" in rows["web chat: relay"]["detail"]


def test_the_push_row_counts_browsers_and_warns_on_one_that_keeps_failing(tmp_path):
    import time as _time
    ea, priv = _install(tmp_path, {"frontend": "web",
                                "web": {"public_url": URL, "tailscale_login": LOGIN}})
    _store(priv, subs=())
    push = _rows(ea, priv)["web chat: push"]
    assert push["status"] == "PASS" and "no browser" in push["detail"]
    (priv / "webchat.sqlite").unlink()
    _store(priv, subs=((_time.time() - 7200, 0), (None, 1)))
    push = _rows(ea, priv)["web chat: push"]
    assert push["status"] == "PASS", push
    assert "2 browser(s)" in push["detail"] and "2 h ago" in push["detail"]
    assert "1 failure(s)" in push["detail"]
    (priv / "webchat.sqlite").unlink()
    _store(priv, subs=((_time.time() - 60, 0), (None, 5)))
    push = _rows(ea, priv)["web chat: push"]
    assert push["status"] == "WARN" and "1 browser(s) failed 3 or more" in push["detail"]
    assert "revoke" in push["fix"]


class _FakeBot:
    """The bot's /api/relay/status on a loopback port: it answers the owner's
    cookie with `status` and anyone else with 401."""

    def __init__(self, status: dict) -> None:
        import http.server
        import threading
        self.status = status
        self.seen: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({"path": self.path, "cookie": self.headers.get("Cookie"),
                                   "host": self.headers.get("Host")})
                if self.path != "/chat/api/relay/status":
                    self.send_response(404)
                    self.end_headers()
                    return
                ok = self.headers.get("Cookie") == f"ea_web={TOKEN}"
                body = json.dumps(outer.status if ok else {"error": "not the owner"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _relay_install(tmp_path, port: int, **relay) -> tuple:
    ea, priv = _install(tmp_path, {"frontend": "web", "web": {
        "public_url": URL, "tailscale_login": LOGIN, "port": port,
        "relay": {"url": "https://relay.example.test", **relay}}})
    (priv / "webchat-pair.token").write_text(TOKEN + "\n")
    (priv / "webchat-pair.token").chmod(0o600)
    key = priv / "webchat-relay.json"
    key.write_text("{}")
    key.chmod(0o600)
    return ea, priv, key


def test_the_relay_row_reads_the_bots_state(tmp_path):
    import time as _time
    bot = _FakeBot({"enabled": True, "relay_url": "https://relay.example.test",
                    "connected": True, "last_ok": _time.time() - 20, "last_error": "",
                    "devices": 2, "pending": 0})
    try:
        ea, priv, key = _relay_install(tmp_path, bot.port)
        relay = _rows(ea, priv)["web chat: relay"]
        assert relay["status"] == "PASS", relay
        assert "connected, the last exchange" in relay["detail"]
        assert "2 device(s), 0 waiting" in relay["detail"] and "mode 0600" in relay["detail"]
        assert bot.seen and bot.seen[-1]["cookie"] == f"ea_web={TOKEN}"
        assert TOKEN not in json.dumps(relay)
        # a device waiting for approval
        bot.status.update(pending=1, devices=3)
        relay = _rows(ea, priv)["web chat: relay"]
        assert relay["status"] == "WARN" and "1 waiting for approval" in relay["detail"]
        assert "pairing code" in relay["fix"]
        # down for a minute is the long poll's churn; for twenty it warns
        bot.status.update(pending=0, connected=False, last_ok=_time.time() - 60,
                          last_error="relay: HTTP 502")
        assert _rows(ea, priv)["web chat: relay"]["status"] == "PASS"
        bot.status.update(last_ok=_time.time() - 1200)
        relay = _rows(ea, priv)["web chat: relay"]
        assert relay["status"] == "WARN" and "not connected" in relay["detail"]
        assert "20 min ago" in relay["detail"] and "HTTP 502" in relay["detail"]
        # the bot runs no relay client
        bot.status.clear()
        bot.status.update(enabled=False)
        relay = _rows(ea, priv)["web chat: relay"]
        assert relay["status"] == "WARN" and "no relay client" in relay["detail"]
        # an open key file
        key.chmod(0o644)
        relay = _rows(ea, priv)["web chat: relay"]
        assert relay["status"] == "WARN" and "0o644" in relay["detail"]
        assert f"chmod 600 {key}" in relay["fix"]
    finally:
        bot.close()


def test_the_relay_row_warns_when_the_bot_does_not_answer(tmp_path):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ea, priv, key = _relay_install(tmp_path, port)
    relay = _rows(ea, priv)["web chat: relay"]
    assert relay["status"] == "WARN" and "does not answer" in relay["detail"]
    assert "start the bot" in relay["fix"]
    # a pinned install id with no key file: the bot leaves the relay off
    key.unlink()
    block = json.loads((priv / "chat.json").read_text())
    block["web"]["relay"]["install_id"] = "t7iapcqjg3nnr2a3nj6p3dmafl"
    (priv / "chat.json").write_text(json.dumps(block))
    relay = _rows(ea, priv)["web chat: relay"]
    assert relay["status"] == "WARN" and "install_id is pinned" in relay["detail"]
