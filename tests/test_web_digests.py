"""The mail digests and the direct message on the web chat alone
(`"frontend": "web"`): each digest lands in the digest channel, made on
demand, never in the EA channel that the briefing's chat reader reads;
the briefing preview goes there too; a direct message goes to the DM.
Every test drives the real writer-only web front end against a store in
a temporary private dir, as an owner process does."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import chat  # noqa: E402
import chat_notify  # noqa: E402
import chat_read  # noqa: E402
import ea_config  # noqa: E402
import mail_gate as mg  # noqa: E402
import mail_privacy as mp  # noqa: E402
import owner_trust  # noqa: E402
import remote_briefing as rb  # noqa: E402
import remote_digests as rd  # noqa: E402
import remote_llm as rl  # noqa: E402
from chat import direct  # noqa: E402
from chat.web_frontend import WebStore  # noqa: E402

NOW = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
ME = "owner@example.edu"
DIGESTS = chat_notify.WEB_DIGEST_ID


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A web-only chat block in the owner-private chat.json, the store
    beside it, read by an owner process; the quiet-hours gate holds every
    post it is asked about, so a digest that reaches the store skipped it."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    cfg = tmp_path / "ea-config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [])
    held = []
    monkeypatch.setitem(sys.modules, "quiet_hours", types.SimpleNamespace(
        should_defer=lambda priority: True, defer=lambda *a: held.append(a)))

    def block(extra: dict | None = None) -> None:
        (priv / "chat.json").write_text(json.dumps(
            {"frontend": "web", "web": {"port": 0, **(extra or {})}}))
        (priv / "chat.json").chmod(0o600)
    block()
    return types.SimpleNamespace(priv=priv, store=lambda: WebStore(priv / "webchat.sqlite"),
                                 block=block, held=held)


def _texts(store: WebStore, channel: str) -> list[str]:
    return [m["text"] for m in store.threads(channel)]


# ── the digest channel ──

def test_a_digest_opens_the_digest_channel_and_never_touches_the_ea_channel(web):
    assert chat_notify.post_digest("mail-digest", "*Kept* mail", topic="Mail digest Fri Oct 9")
    store = web.store()
    row = store.channel(DIGESTS)
    assert (row["name"], row["kind"]) == ("Mail digests", "digest")
    assert "Claude does not read" in row["description"]
    (thread,) = store.threads(DIGESTS)
    assert (thread["author"], thread["text"]) == ("bot", "**Kept** mail")
    assert store.thread_origin(DIGESTS, thread["thread_key"]) == "mail-digest"
    assert store.channel("main") is None or _texts(store, "main") == []
    assert web.held == []


def test_a_digest_key_goes_to_the_digest_channel_whatever_the_notify_map_says(web):
    web.block({"notify": {"list-digest": {"channel": "main"},
                          "mail-digest": {"channel": "dm"}}})
    assert chat_notify.post("list-digest", "list items")
    assert chat_notify.post("mail-digest", "held mail")
    store = web.store()
    assert sorted(_texts(store, DIGESTS)) == ["held mail", "list items"]
    assert _texts(store, "main") == [] and _texts(store, "dm") == []
    assert web.held == []                          # a digest never waits in the resident's queue
    assert chat_notify.post("ea-assistant", "a briefing")
    assert len(web.held) == 1                      # any other key still meets the gate


def test_the_bots_digest_channel_row_stands(web):
    store = web.store()
    store.upsert_channel(DIGESTS, "Digests (renamed)", "digest", True)
    assert chat_notify.post_digest("admissions-digest", "a week of inquiries", topic="t")
    row = web.store().channel(DIGESTS)
    assert (row["name"], row["archived"]) == ("Digests (renamed)", True)


# ── the three senders ──

@pytest.fixture
def gate(web, monkeypatch):
    """mail_gate in enforce mode on the same private dir."""
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": False}}
    owner_trust.write_owner_file(web.priv / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {ME})
    monkeypatch.setattr(mg, "_list_senders", lambda: set())
    mp._INDEX_CACHE.update(mtime=None, index=None)
    for mid, kept in (("f1", ""), ("k1", "listed domain mil")):
        rl.append(mp.EXTRACTIONS, {
            "key": f"k-{mid}", "id": mid, "conversation_id": f"c-{mid}",
            "received": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from": "alice@example.edu", "subject": f"subject {mid}", "status": "ok",
            "keep_from_anthropic": kept,
            "result": {"state": "fyi", "needs_reply": False, "summary": f"summary of {mid}",
                       "ask": "", "deadline": "", "offered_times": [], "people": [], "tone": [],
                       "domain": "research", "scheduling": "none", "sensitivity": "normal"}})
    mp._INDEX_CACHE.update(mtime=None, index=None)
    return web


def test_the_mail_gate_digest_posts_on_the_web_chat_and_records_the_run(gate):
    assert mg.digest(now=NOW) == {"posted": True, "rows": 2}
    (thread,) = gate.store().threads(DIGESTS)
    assert "summary of f1" in thread["text"] and "summary of k1" in thread["text"]
    assert gate.store().thread_origin(DIGESTS, thread["thread_key"]) == "mail-digest"
    assert _texts(gate.store(), "main") == []
    last = rl.read(mg.DIGEST_STATE)[-1]
    assert last["until"] == NOW.isoformat(timespec="seconds") and last["rows"] == 2
    # the next run starts where this one ended
    assert mg.digest(now=NOW + timedelta(minutes=5)) == {"posted": False, "rows": 0}


def test_the_list_and_admissions_digests_post_in_the_digest_channel(web):
    assert rd.deliver("list-digest", ":newspaper: *robo-list digest*", has_chat=True)
    assert rd.deliver("admissions-digest", ":mortar_board: *Admissions*", has_chat=True)
    store = web.store()
    origins = sorted(store.thread_origin(DIGESTS, t["thread_key"]) for t in store.threads(DIGESTS))
    assert origins == ["admissions-digest", "list-digest"]
    assert _texts(store, "main") == []


def test_the_briefing_preview_posts_in_the_digest_channel(web):
    assert rb._preview("*Morning briefing, Fri Oct 9*\n1. Send the slides")
    store = web.store()
    (thread,) = store.threads(DIGESTS)
    assert thread["text"].startswith("**Morning briefing, Fri Oct 9**")
    assert store.thread_origin(DIGESTS, thread["thread_key"]) == "briefing-preview"
    assert _texts(store, "main") == []


# ── the briefing's chat reader ──

def test_the_briefing_reader_never_reads_a_digest_channel(web, capsys):
    assert chat_notify.post_digest("mail-digest", "kept mail summary", topic="t")
    web.held.clear()
    assert chat_notify.post("ea-assistant", "the morning briefing", _bypass_quiet=True)
    got = chat_read.fetch_recent_messages(hours_back=24)
    assert [m["text"] for m in got] == ["the morning briefing"]
    (digest,) = web.store().threads(DIGESTS)
    assert chat_read.fetch_thread(DIGESTS, digest["thread_key"]) == []
    # by kind, not by id: a main channel recorded as a digest is not read either
    web.store().set_channel("main", kind="digest")
    assert chat_read.fetch_recent_messages(hours_back=24) == []
    assert "digest channel; not read" in capsys.readouterr().err
    web.store().set_channel("main", kind="main")
    assert [m["text"] for m in chat_read.fetch_recent_messages(hours_back=24)] == \
        ["the morning briefing"]


# ── the direct message ──

def test_a_direct_message_goes_to_the_dm_unless_personal_is_mapped(web):
    fe = direct.frontend()
    assert direct.is_web(fe)
    assert direct.send_direct("Tickets go on sale at 10:00.", fe) == {"posted": True}
    store = web.store()
    (row,) = store.threads("dm")
    assert row["text"] == "Tickets go on sale at 10:00." and store.channel("dm")["kind"] == "dm"
    assert store.thread_origin("dm", row["thread_key"]) == "personal"
    assert _texts(store, "main") == []
    # a `personal` entry in the notify map still wins
    web.block({"notify": {"personal": {"channel": "main"}}})
    assert direct.send_direct("Registration opens now.", fe) == {"posted": True}
    assert _texts(web.store(), "main") == ["Registration opens now."]
    # the notifier's own `personal` key keeps its EA-channel default
    web.block()
    web.held.clear()
    assert chat_notify.post("personal", "a weekly check-in", _bypass_quiet=True)
    assert "a weekly check-in" in _texts(web.store(), "main")


def test_a_direct_message_that_fails_raises(web, monkeypatch):
    fe = direct.frontend()
    monkeypatch.setattr(chat_notify, "web_direct", lambda text: False)
    with pytest.raises(RuntimeError, match="DM failed"):
        direct.send_direct("x", fe)
