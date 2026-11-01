"""Command-line senders and the web chat (docs/webchat.md, CLI senders).

A sender in an owner process (the briefing's chat_notify, chat_upload, a
cron lane's room or direct post) writes the store the bot serves, its web
side writer-only, and the bot's event stream pushes what it wrote. Any
other process posts to the primary alone and says so once.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import chat  # noqa: E402
import chat_notify  # noqa: E402
import chat_upload  # noqa: E402
import ea_config  # noqa: E402
from chat import direct  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.shadow_frontend import ShadowFrontend  # noqa: E402
from chat.types import MessageRef  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from chat.web_frontend import WebFrontend, WebStore  # noqa: E402
from chat.zulip_frontend import thread_key_for  # noqa: E402

LOGIN = "owner@example.test"


@pytest.fixture(autouse=True)
def _tailscaled_is_this_process(monkeypatch):
    """The streams below sign in with the login header; take this test
    process's connections as tailscaled's (the uid still comes from
    /proc/net/tcp; without /proc, as on macOS, every peer is this user's)."""
    monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: os.geteuid())
    if not wf.PROC_NET[0].exists():
        monkeypatch.setattr(wf, "peer_uid", lambda peer, local: os.geteuid())


def _ea_dir(tmp_path: Path, block: dict) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": block}))
    return tmp_path


def _owner(monkeypatch, yes: bool = True) -> None:
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: yes)
    monkeypatch.setattr(chat, "_CLI_ALONE_LOGGED", [])


def _private_chat(tmp_path: Path, monkeypatch, block: dict) -> Path:
    """An owner's private dir holding chat.json, as an owner process reads it."""
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    (priv / "chat.json").write_text(json.dumps(block))
    (priv / "chat.json").chmod(0o600)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    return priv


class Stream:
    """The bot's event stream, read as the owner."""

    def __init__(self, f: WebFrontend) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", f.port, timeout=5)
        self.conn.request("GET", f"{f.base_path}/api/events",
                          headers={"Tailscale-User-Login": LOGIN})
        self.resp = self.conn.getresponse()
        while self.resp.fp.readline().decode().rstrip("\n") != "retry: 3000":
            pass

    def event(self, kind: str = "message") -> dict:
        """The next event of that kind (a channel row may come first)."""
        cur: dict = {}
        while True:
            line = self.resp.fp.readline().decode().rstrip("\n")
            if line == "" and "data" in cur:
                if cur.get("event") == kind:
                    return cur
                cur = {}
                continue
            key, _, val = line.partition(": ")
            if key in ("id", "event", "data"):
                cur[key] = val

    def close(self) -> None:
        self.conn.close()


# ── load_frontend for a command-line sender ──

def test_an_owner_cli_gets_the_composite_with_a_writer_only_web_side(tmp_path, monkeypatch):
    _owner(monkeypatch)
    ea = _ea_dir(tmp_path, {"frontend": "memory", "shadow": "web"})
    fe = chat.load_frontend(ea_dir=ea, env={}, for_cli=True)
    assert isinstance(fe, ShadowFrontend) and isinstance(fe.primary, MemoryFrontend)
    web = fe.shadow
    assert isinstance(web, WebFrontend) and web.writer_only
    assert web._server is None and web._tail is None
    assert web.store_path == tmp_path / "state" / "webchat.sqlite" and web.store_path.exists()
    ref = fe.post("C_MAIN", "*brief*")
    row = web.store.message("C_MAIN", ref.id)
    assert (row["author"], row["text"], row["thread_key"]) == ("bot", "**brief**", ref.thread_key)
    # the bot's own build serves
    bot = chat.load_frontend(ea_dir=ea, env={})
    assert isinstance(bot, ShadowFrontend) and bot.shadow.writer_only is False


def test_a_cli_that_is_not_the_owners_posts_to_the_primary_alone(tmp_path, monkeypatch, caplog):
    _owner(monkeypatch, False)
    caplog.set_level(logging.WARNING, logger="chat")
    ea = _ea_dir(tmp_path, {"frontend": "memory", "shadow": "web"})
    assert type(chat.load_frontend(ea_dir=ea, env={}, for_cli=True)) is MemoryFrontend
    assert type(chat.load_frontend(ea_dir=ea, env={}, for_cli=True)) is MemoryFrontend
    assert chat.web_writer(ea_dir=ea) is None
    assert caplog.text.count("not the owner's") == 1
    assert not (tmp_path / "state" / "webchat.sqlite").exists()


def test_a_store_that_does_not_open_leaves_the_cli_on_the_primary(tmp_path, monkeypatch, caplog):
    _owner(monkeypatch)
    (tmp_path / "blocked").write_text("a file, not a directory")
    ea = _ea_dir(tmp_path, {"frontend": "memory", "shadow": "web",
                            "web": {"store_path": str(tmp_path / "blocked" / "w.sqlite")}})
    assert type(chat.load_frontend(ea_dir=ea, env={}, for_cli=True)) is MemoryFrontend
    assert chat.web_writer(ea_dir=ea) is None
    assert "did not build" in caplog.text and "did not open" in caplog.text


def test_web_writer_needs_the_web_shadow(tmp_path, monkeypatch):
    _owner(monkeypatch)
    assert chat.web_writer(ea_dir=_ea_dir(tmp_path, {"frontend": "memory"})) is None
    w = chat.web_writer(ea_dir=_ea_dir(tmp_path, {"frontend": "memory", "shadow": "web"}))
    assert isinstance(w, WebFrontend) and w.writer_only and w.store_path.exists()


# ── from the sender to the bot's open stream ──

def test_an_owner_cli_post_reaches_the_bots_open_stream(tmp_path, monkeypatch):
    # the owner's bot reads its block from the private chat.json and keeps
    # the store beside it, as on a hardened host
    _owner(monkeypatch)
    _private_chat(tmp_path, monkeypatch, {"frontend": "memory", "shadow": "web",
                                          "web": {"port": 0, "tailscale_login": LOGIN}})
    bot = chat.load_frontend(env={})
    assert bot.shadow.trusted_config is True
    bot.start()
    try:
        assert bot.shadow_off is None
        assert bot.shadow.store_path == tmp_path / "priv" / "webchat.sqlite"
        s = Stream(bot.shadow)
        try:
            cli = chat.load_frontend(env={}, for_cli=True)
            t0 = time.monotonic()
            ref = cli.post("C_MAIN", "the morning briefing")
            ev = s.event()
            assert time.monotonic() - t0 < 2.5
            row = json.loads(ev["data"])
            assert (row["channel"], row["id"], row["thread_key"]) == \
                ("C_MAIN", ref.id, ref.thread_key)
        finally:
            s.close()
    finally:
        bot.stop()


def test_a_writer_in_another_process_reaches_the_stream(tmp_path):
    path = tmp_path / "priv" / "webchat.sqlite"
    bot = WebFrontend.from_config({"port": 0, "tailscale_login": LOGIN}, store_path=path,
                                  trusted_config=True)
    bot.start()
    try:
        bot.mirror_channel("stream:7", "ea", "main")
        s = Stream(bot)
        try:
            script = textwrap.dedent("""
                import sys
                sys.path.insert(0, sys.argv[1])
                from botcore.chat.types import MessageRef
                from botcore.chat.web_frontend import WebFrontend
                w = WebFrontend.from_config({}, store_path=sys.argv[2], writer_only=True)
                w.start()
                w.mirror_post(MessageRef("stream:7", "777", "stream:7:topic:cron", 1.0),
                              "from a cron lane")
            """)
            subprocess.run([sys.executable, "-c", script, str(ROOT / "botcore"), str(path)],
                           check=True, timeout=60)
            t0 = time.monotonic()
            row = json.loads(s.event()["data"])
            assert time.monotonic() - t0 < 2.5
            assert (row["id"], row["text"]) == ("777", "from a cron lane")
        finally:
            s.close()
    finally:
        bot.stop()


# ── chat_notify: the briefing's Zulip post ──

class FakeZulip:
    streams = ["ea", "ea-dm", "lab"]

    def __init__(self, email, api_key, site):
        self.sent: list = []

    def get_stream_id(self, name):
        return {"result": "success", "stream_id": self.streams.index(name) + 10}

    def get_stream_topics(self, stream_id):
        return {"result": "success", "topics": []}

    def send_message(self, request):
        self.sent.append(request)
        return {"result": "success", "id": 900 + len(self.sent)}


ZULIP_BLOCK = {"frontend": "zulip", "shadow": "web",
               "zulip": {"site": "https://zulip.example.test", "bot_email": "bot@example.test",
                         "stream": "ea", "dm_stream": "ea-dm",
                         "notify": {"ea-assistant": {"stream": "ea"},
                                    "lab": {"stream": "lab", "topic": "pinned"}}}}


@pytest.fixture
def notify_env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zulip", types.SimpleNamespace(Client=FakeZulip))
    monkeypatch.setenv("ZULIP_API_KEY", "zkey")
    monkeypatch.setattr(chat_notify, "_WEB", [])
    return tmp_path


def test_an_owner_notify_post_is_mirrored_under_the_zulip_ids(notify_env, monkeypatch):
    _owner(monkeypatch)
    priv = _private_chat(notify_env, monkeypatch, ZULIP_BLOCK)
    text = "*Morning* briefing" + chat_notify.SPLIT_MARKER + "second part"
    assert chat_notify.post("ea-assistant", text, _bypass_quiet=True)
    assert chat_notify.post("lab", "a pinned note", _bypass_quiet=True)
    store = WebStore(priv / "webchat.sqlite")
    main = store.channel("stream:10")
    assert (main["name"], main["kind"]) == ("ea", "main")
    assert store.channel("stream:12")["kind"] == "room"
    key = thread_key_for(10, "Morning briefing")
    assert [(m["id"], m["text"]) for m in store.thread("stream:10", key)] == \
        [("901", "**Morning** briefing"), ("902", "second part")]
    (lab,) = store.thread("stream:12", thread_key_for(12, "pinned"))
    assert (lab["id"], lab["author"]) == ("901", "bot")
    # each thread is known as a notifier post, under its channel key
    assert store.thread_origin("stream:10", key) == "ea-assistant"
    assert store.thread_origin("stream:12", thread_key_for(12, "pinned")) == "lab"
    assert [t for _s, t, _p in store.events_since(0)] == \
        ["channel", "message", "message", "channel", "message"]


def test_the_bots_channel_row_wins_over_the_notifiers(notify_env, monkeypatch):
    _owner(monkeypatch)
    priv = _private_chat(notify_env, monkeypatch, ZULIP_BLOCK)
    WebStore(priv / "webchat.sqlite").upsert_channel("stream:12", "lab-renamed", "room", True)
    assert chat_notify.post("lab", "x", _bypass_quiet=True)
    row = WebStore(priv / "webchat.sqlite").channel("stream:12")
    assert (row["name"], row["archived"]) == ("lab-renamed", True)


def test_a_notify_post_that_is_not_the_owners_is_not_mirrored(notify_env, monkeypatch):
    _owner(monkeypatch, False)
    cfg = notify_env / "ea-config.json"
    cfg.write_text(json.dumps({"chat": {**ZULIP_BLOCK, "web": {
        "store_path": str(notify_env / "w.sqlite")}}}))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    assert chat_notify.post("ea-assistant", "x", _bypass_quiet=True)
    assert chat_notify._WEB == [None] and not (notify_env / "w.sqlite").exists()


def test_a_failed_mirror_leaves_the_post_standing(notify_env, monkeypatch, capsys):
    _owner(monkeypatch)
    _private_chat(notify_env, monkeypatch, ZULIP_BLOCK)
    monkeypatch.setattr(FakeZulip, "get_stream_id", lambda self, name: {"result": "error"})
    assert chat_notify.post("lab", "x", _bypass_quiet=True)
    assert "Web chat mirror failed" in capsys.readouterr().out


# ── chat_upload and the direct-message helpers ──

def test_an_owner_upload_lands_in_the_web_store_with_its_bytes(tmp_path, monkeypatch, capsys):
    _owner(monkeypatch)
    priv = _private_chat(tmp_path, monkeypatch, {"frontend": "memory", "shadow": "web"})
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 bytes")
    monkeypatch.setattr(sys, "argv", ["chat_upload.py", "--channel", "C_MAIN", "--file", str(f),
                                      "--comment", "the report"])
    assert chat_upload.main() == 0
    assert json.loads(capsys.readouterr().out)["file_id"] == "F1"
    store = WebStore(priv / "webchat.sqlite")
    row = store.message("C_MAIN", "file:F1")
    assert (row["kind"], row["text"], row["file"]["id"]) == ("file", "the report", "F1")
    assert Path(store.file("F1")["path"]).read_bytes() == b"%PDF-1.4 bytes"


def test_the_direct_helpers_still_see_zulip_through_the_composite(tmp_path):
    primary = MemoryFrontend()
    primary.client = types.SimpleNamespace(send_message=lambda req: {"result": "success",
                                                                    "id": 5})
    primary._call = lambda *a: {"result": "success"}
    primary.owner_id = "42"
    web = WebFrontend.from_config({}, store_path=tmp_path / "w.sqlite", writer_only=True)
    fe = ShadowFrontend(primary, web)
    assert direct.is_zulip(fe)
    assert direct.send_direct("now", fe) == {"id": 5}
    assert not (tmp_path / "w.sqlite").exists()        # a direct message is not mirrored
