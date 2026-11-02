"""The Scheduling channel of the web chat alone (`"frontend": "web"`).

The web chat makes a fixed `sched` channel named "Scheduling" when it runs
alone, so the proactive scheduler works as on Zulip: the queue writer
(preprocess.write_proactive_queue) takes a scheduling request found in
mail, the bot's proactive tick opens a conversation for it in Scheduling
on the proactive lane, the owner's reply there takes the scheduling lane,
a later message in the same mail conversation (an acceptance) posts into
that thread, and the Create card a turn there stages lands in the DM with
a pointer in the Scheduling thread. `"sched": false` in the web block
turns the channel off. Shadow mode (a Zulip primary, the web chat as its
mirror) keeps the primary's scheduling stream as the sched channel.

The bot is hyphenated and heavy, so its functions are exec'd from its
source into a namespace of stubs, as tests/test_web_rooms.py does."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocessor"))
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

LOGIN = "owner@example.test"
CONV = "AAQkAGI2conv-sched-1"


def _fn_body(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    body = BOT_SRC[start:end]
    # a decorator line of the next function belongs to it, not to this one
    return re.sub(r"\n@app\.[^\n]*\s*$", "\n", body)


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))


class _Thread:
    """threading.Thread for the bot: records the dispatch it would start."""
    started: list = []

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append(self.args)


def _web(tmp_path, **cfg):
    """The web chat as the owner's bot builds it: a trusted chat block and a
    private store; served on a loopback port."""
    from preprocessor.chat.web_frontend import WebFrontend
    block = {"port": 0, "tailscale_login": LOGIN, "public_url": "https://host.example.test/chat"}
    block.update(cfg)
    fe = WebFrontend.from_config(block, store_path=tmp_path / "webchat" / "webchat.sqlite",
                                 webui_dir=tmp_path / "webui", trusted_config=True)
    fe.poll_s = fe.pair_delay_s = 0.02
    return fe


# ── the channel ──

def test_the_web_chat_alone_makes_the_scheduling_channel(tmp_path):
    fe = _web(tmp_path)
    assert fe.sched_channel == "sched"            # before start: the bot reads it at import
    fe.start()
    try:
        row = fe.store.channel("sched")
        assert (row["name"], row["kind"], bool(row["archived"])) == ("Scheduling", "sched", False)
        assert [c["id"] for c in fe.store.channels()] == ["dm", "main", "sched"]
        assert fe.channel_info("sched").name == "Scheduling"
        assert not fe.is_dm("sched")
    finally:
        fe.stop()


def test_sched_false_leaves_it_out_and_archives_the_one_made_before(tmp_path):
    off = _web(tmp_path / "a", sched=False)
    assert off.sched_channel == ""
    off.start()
    try:
        assert off.store.channel("sched") is None
        assert {c["id"] for c in off.store.channels()} == {"main", "dm"}
    finally:
        off.stop()
    # on, then off: the row stays with its threads, archived; on again, back
    on = _web(tmp_path / "b")
    on.start()
    ref = on.post("sched", "a request")
    on.stop()
    off = _web(tmp_path / "b", sched="off")
    off.start()
    try:
        assert off.sched_channel == "" and off.store.channel("sched")["archived"]
        assert off.store.message("sched", ref.id) is not None
    finally:
        off.stop()
    again = _web(tmp_path / "b", sched=True)
    again.start()
    try:
        assert again.sched_channel == "sched" and not again.store.channel("sched")["archived"]
    finally:
        again.stop()


def test_the_switch_reads_alike_in_the_config_and_the_frontend():
    import ea_config
    from preprocessor.chat import web_frontend as wf
    from preprocessor.preprocess import sched_channel_configured
    for val, on in ((None, True), (True, True), ("yes", True), (False, False),
                    ("false", False), ("Off", False), ("no", False), ("0", False), (0, False)):
        web = {} if val is None else {"sched": val}
        block = {"frontend": "web", "web": web}
        assert wf.sched_enabled(web) is on, val
        assert ea_config.web_sched_on(block) is on, val
        assert sched_channel_configured(block) is on, val
    # the defaults: a web-only install gets it
    chat = ea_config.chat_from_block({"frontend": "web"})
    assert chat["web"]["sched"] is True and sched_channel_configured(chat)
    # Zulip with the web chat as its shadow: the scheduling stream decides, as before
    for sched_stream, on in (("", False), ("ea-scheduling", True)):
        block = ea_config.chat_from_block({"frontend": "zulip", "shadow": "web",
                                           "zulip": {"sched_stream": sched_stream}})
        assert sched_channel_configured(block) is on
        assert not ea_config.web_sched_on(block)


def test_shadow_mode_keeps_the_primary_scheduling_stream(tmp_path):
    from preprocessor.chat.memory_frontend import MemoryFrontend
    from preprocessor.chat.shadow_frontend import ShadowFrontend
    primary = MemoryFrontend()
    primary.sched_channel = "stream:7"
    web = _web(tmp_path)
    fe = ShadowFrontend(primary, web)
    assert fe.sched_channel == "stream:7" and web.sched_channel == ""
    fe.start()
    try:
        assert web.refused is None
        assert web.store.channel("stream:7")["kind"] == "sched"
        assert web.store.channel("sched") is None
        assert [c["id"] for c in web.store.channels() if c["kind"] == "sched"] == ["stream:7"]
    finally:
        fe.stop()


# ── the proactive path ──

BOT_FNS = (
    "_sched_seen", "_sched_mark_seen", "_sched_save_seen", "_untrusted_field",
    "_proactive_scheduling_prompt", "_proactive_followup_prompt", "_scheduling_proactive_tick",
    "_turn_lane", "_core_room_names", "_core_channel", "_owner_dm_channels",
    "_owner_destination", "_thread_channel", "_web_thread_channel", "_delivery_channel",
    "_staged_route", "_owner_card_channel", "_post_dm_breadcrumb", "_deliver_confirmation",
    "on_message",
)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """(namespace, frontend, queue path) with the bot's scheduling functions
    exec'd against a started web chat alone."""
    monkeypatch.setitem(sys.modules, "quiet_hours",
                        types.SimpleNamespace(in_quiet_hours=lambda: False))
    fe = _web(tmp_path)
    fe.start()
    queue = tmp_path / "state" / "scheduling-proactive-queue.json"
    queue.parent.mkdir()
    seen = tmp_path / "private" / "scheduling-proactive-seen.json"
    seen.parent.mkdir(mode=0o700)
    _Thread.started = []
    posted_cards: list = []

    def post_card(ch, card, thread_ts=None):
        ref = fe.post_card(ch, card, thread_ts=thread_ts)
        posted_cards.append(ref)
        return ref

    ns = {"json": json, "re": re, "time": time, "log": _Log(), "FRONTEND": fe,
          "CHANNEL_ID": fe.main_channel, "CHANNEL_NAME": fe.main_channel_name,
          "SCHED_CHANNEL_ID": fe.sched_channel, "OWNER_SLACK_USER_ID": fe.owner_id,
          "BOT_USER_ID": fe.bot_user_id, "EA_DIR": tmp_path, "_USER_FIRST_NAME": "Robin",
          "SCHED_PROACTIVE_QUEUE": queue, "SCHED_PROACTIVE_SEEN": seen,
          "_resident_text": lambda p: Path(p).read_text() if Path(p).exists() else None,
          "_atomic": types.SimpleNamespace(
              write_json=lambda path, data, **kw: Path(path).write_text(json.dumps(data))),
          "_resolve_dm_channel": lambda: fe.dm_channel(fe.owner_id),
          "_channel_scope": lambda channel: None,
          "_post_card": post_card, "_note_run_post": lambda kind: None,
          "threading": types.SimpleNamespace(Thread=_Thread, Lock=threading.Lock),
          "_last_message_lock": threading.Lock(), "_last_message_time": 0.0,
          "_active_lock": threading.Lock(), "_active_threads": set(),
          "_is_bot_command": lambda t: False, "handle_message_async": lambda *a: None,
          "posted_cards": posted_cards}
    for name in BOT_FNS:
        exec(compile(_fn_body(name), f"ea-chat-bot.py[{name}]", "exec"), ns)
    fe.registry.event("message")(ns["on_message"])
    yield ns, fe, queue
    fe.stop()


def _queue(monkeypatch, queue: Path, req: dict) -> int:
    """The intake's queue write on a web-only install: the chat block is the
    web chat alone, as setup writes it."""
    from preprocessor import preprocess as pp
    monkeypatch.setattr(pp._ea_config, "get_chat",
                        lambda *a, **k: pp._ea_config.chat_from_block({"frontend": "web"}))
    return pp.write_proactive_queue([req], path=queue)


def test_a_scheduling_request_opens_a_conversation_in_scheduling(bot, monkeypatch):
    ns, fe, queue = bot
    received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    req = {"conversation_id": CONV, "message_id": "m-1", "source_message_id": "m-1",
           "from_name": "Dana Example", "subject": "Meet next week?", "kind": "new",
           "received": received}
    assert _queue(monkeypatch, queue, req) == 1
    ns["_scheduling_proactive_tick"]()
    # the seed opens a new thread in Scheduling
    threads = fe.store.threads("sched", 10)
    assert len(threads) == 1, threads
    seed = fe.store.thread("sched", threads[0]["thread_key"])[0]
    assert seed["author"] == "bot" and "New scheduling request" in seed["text"]
    assert "Dana Example" in seed["text"]
    # the turn runs in that thread on the proactive lane
    assert len(_Thread.started) == 1
    channel, thread, msg_ts, prompt, user_id, is_reply = _Thread.started[0]
    assert (channel, thread, msg_ts, user_id, is_reply) == (
        "sched", seed["thread_key"], seed["id"], "proactive-scheduler", False)
    assert re.fullmatch(r"t\d+", thread)
    assert "[Proactive scheduling task" in prompt and f"--conversation-id {CONV}" in prompt
    assert ns["_turn_lane"](channel, user_id) == "proactive"
    assert json.loads(ns["SCHED_PROACTIVE_SEEN"].read_text())[CONV] == {
        "thread": thread, "messages": ["m-1"]}
    # the next tick opens nothing new
    ns["_scheduling_proactive_tick"]()
    assert len(_Thread.started) == 1

    # the owner's answer in that thread takes the scheduling lane, same thread
    row, after = fe.web_message("sched", "the Tuesday one", thread)
    if after is not None:
        after()
    assert len(_Thread.started) == 2
    channel, thread2, ts, _msg, user_id, is_reply = _Thread.started[1]
    assert (channel, thread2, ts, user_id, is_reply) == ("sched", thread, row["id"],
                                                          fe.owner_id, True)
    assert ns["_turn_lane"](channel, user_id) == "sched-channel"

    # an acceptance in the same mail conversation posts into the same thread
    req2 = dict(req, message_id="m-2", source_message_id="m-2", kind="confirmation",
                offered_times=["Tue 10:00"])
    assert _queue(monkeypatch, queue, req2) == 1
    ns["_scheduling_proactive_tick"]()
    assert len(fe.store.threads("sched", 10)) == 1
    said = [m["text"] for m in fe.store.thread("sched", thread)]
    assert any("New message from Dana Example" in t for t in said), said
    channel, thread3, _ts, prompt, user_id, is_reply = _Thread.started[2]
    assert (channel, thread3, user_id, is_reply) == ("sched", thread, "proactive-scheduler", True)
    assert "[Proactive scheduling follow-up" in prompt
    assert json.loads(ns["SCHED_PROACTIVE_SEEN"].read_text())[CONV]["messages"] == ["m-1", "m-2"]


def test_the_create_card_lands_in_the_dm_with_a_pointer_in_scheduling(bot):
    from preprocessor import chat as _chat
    ns, fe, _queue_path = bot
    seed = fe.post("sched", ":calendar: New scheduling request")
    thread = seed.thread_key
    # Scheduling is one of the bot's own places, never a room to archive
    assert ns["_core_channel"]("sched") and ns["_owner_destination"]("sched")
    # a staged event names this thread: the post goes to Scheduling, from
    # the turn's channel or from the poller that has the key alone
    assert ns["_staged_route"]({"thread_ts": thread}, "sched", thread) == ("sched", thread)
    assert ns["_staged_route"]({"thread_ts": thread}, "main", None) == ("sched", thread)
    assert ns["_delivery_channel"](thread) == "sched"
    card = _chat.ConfirmationCard(
        kind="event", title=":lock: Create this event?",
        actions=[_chat.CardAction("approve_event", "Create", {"token": "t-1"}, "primary", "send"),
                 _chat.CardAction("cancel_event", "Cancel", {"token": "t-1"}, "danger", "cancel")],
        fallback_text=":lock: Create this event?")
    dm, card_ts = ns["_deliver_confirmation"]("sched", thread, card, "The Create card", "event")
    assert dm == "dm" and fe.store.message("dm", card_ts)["kind"] == "card"
    pointer = [m["text"] for m in fe.store.thread("sched", thread) if m["id"] != seed.id]
    assert len(pointer) == 1 and "The Create card is in your DM" in pointer[0], pointer
    assert "#/dm/" in pointer[0]
