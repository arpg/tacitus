"""Tests for the Zulip frontend on a fake client and API fixtures."""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat.base import (  # noqa: E402
    AlreadyArchived,
    ChatError,
    Limits,
    NotArchived,
    NotPermitted,
    NotSupported,
    RoomNameTaken,
)
from chat.cards import CardAction, CardField, CardRegistry, ConfirmationCard  # noqa: E402
from chat.types import THREAD_KEY_RE, FileRef, MessageRef, Status  # noqa: E402
from chat import zulip_frontend as zf  # noqa: E402
from chat.zulip_frontend import ZulipFrontend  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "zulip"
BOT, OWNER, OTHER = 50, 9, 12
EA, DM = 7, 8
SITE = "https://chat.example.org"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


OK = {"result": "success", "msg": ""}


class FakeZulipClient:
    """Answers the read calls from canned dicts and records every write."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.profile = fx("api_profile")
        self.register_res = fx("api_register")
        self.subs = fx("api_subscriptions")
        self.streams = fx("api_streams")
        self.stream_ids = {s["name"]: s["stream_id"] for s in self.streams["streams"]}
        self.events: list[dict] = []
        self.messages: dict | None = None
        self.messages_by_anchor: dict[int, dict] = {}
        self.send_results: list[dict] = []
        self.update_results: list[dict] = []
        self.reaction_results: dict[str, dict] = {}
        self.update_stream_result: dict = dict(OK)
        self.delete_stream_result: dict = dict(OK)
        self.add_subs_result: dict = dict(OK)
        self.upload_result = fx("api_upload")
        self.topics: dict[int, list[str]] = {}
        # stream id -> subscriber ids; a stream with no row has the bot and the owner
        self.members: dict[int, list[int]] = {}
        self._next_id = 300
        self.registers = 0

    def _rec(self, name, arg=None):
        self.calls.append((name, copy.deepcopy(arg)))

    def named(self, name):
        return [a for n, a in self.calls if n == name]

    # reads
    def get_profile(self, request=None):
        self._rec("get_profile")
        return self.profile

    def register(self, event_types=None, narrow=None, **kw):
        self._rec("register", {"event_types": event_types, **kw})
        self.registers += 1
        res = dict(self.register_res)
        res["queue_id"] = f"q-{self.registers}"
        return res

    def get_stream_id(self, name):
        self._rec("get_stream_id", name)
        if name in self.stream_ids:
            return {**OK, "stream_id": self.stream_ids[name]}
        return fx("api_stream_id_missing")

    def get_streams(self, **kw):
        self._rec("get_streams", kw)
        return self.streams

    def get_stream_topics(self, stream_id):
        self._rec("get_stream_topics", stream_id)
        return {**OK, "topics": [{"name": n, "max_id": 1}
                                 for n in self.topics.get(int(stream_id), [])]}

    def get_subscriptions(self, request=None):
        self._rec("get_subscriptions")
        return self.subs

    def call_endpoint(self, url=None, method="POST", request=None, **kw):
        self._rec("call_endpoint", {"url": url, "method": method, "request": request})
        if url and url.startswith("streams/") and url.endswith("/members") and method == "GET":
            sid = int(url.split("/")[1])
            return {**OK, "subscribers": self.members.get(sid, [BOT, OWNER])}
        if url and url.startswith("streams/") and method == "GET":
            sid = int(url.split("/")[1])
            for s in self.streams["streams"]:
                if s["stream_id"] == sid:
                    return {**OK, "stream": s}
            return {"result": "error", "code": "BAD_REQUEST", "msg": "Invalid channel ID"}
        return dict(OK)

    def get_messages(self, req):
        self._rec("get_messages", req)
        if req.get("num_before") == 0 and req.get("num_after") == 0:
            m = self.messages_by_anchor.get(int(req["anchor"]))
            return {**OK, "messages": [m] if m else []}
        if self.messages is not None:
            return self.messages
        return {**OK, "messages": [], "found_oldest": True}

    def get_events(self, **kw):
        self._rec("get_events", kw)
        if self.events:
            return self.events.pop(0)
        return {**OK, "events": []}

    # writes
    def send_message(self, req):
        self._rec("send_message", req)
        if self.send_results:
            return self.send_results.pop(0)
        self._next_id += 1
        return {**OK, "id": self._next_id}

    def update_message(self, req):
        self._rec("update_message", req)
        return self.update_results.pop(0) if self.update_results else dict(OK)

    def add_reaction(self, req):
        self._rec("add_reaction", req)
        res = self.reaction_results.get(req["emoji_name"], dict(OK))
        if isinstance(res, list):  # one result per call, then success
            return res.pop(0) if res else dict(OK)
        return res

    def remove_reaction(self, req):
        self._rec("remove_reaction", req)
        return self.reaction_results.get(req["emoji_name"], dict(OK))

    def add_subscriptions(self, streams, **kw):
        self._rec("add_subscriptions", {"streams": streams, **kw})
        if self.add_subs_result.get("result") == "success":
            for s in streams:
                if s["name"] not in self.stream_ids:
                    sid = 40 + len(self.stream_ids)
                    self.stream_ids[s["name"]] = sid
                    self.streams["streams"].append({"stream_id": sid, "name": s["name"],
                                                    "invite_only": kw.get("invite_only", False),
                                                    "is_archived": False})
        return self.add_subs_result

    def update_stream(self, req):
        self._rec("update_stream", req)
        return self.update_stream_result

    def delete_stream(self, sid):
        self._rec("delete_stream", sid)
        return self.delete_stream_result

    def upload_file(self, f):
        self._rec("upload_file", {"name": f.name, "data": f.read()})
        return self.upload_result


CFG = {"site": SITE, "bot_email": "ea-bot@example.org", "owner_user_id": str(OWNER),
       "stream": "ea", "sched_stream": "", "dm_stream": "ea-dm"}


def make(tmp_path, client=None, start=True, **cfg):
    client = client or FakeZulipClient()
    fe = ZulipFrontend({**CFG, **cfg}, api_key="KEY", private_dir=tmp_path / "priv",
                       client=client)
    fe._run_later = lambda fn: fn()
    fe._sleep = lambda secs: client.calls.append(("sleep", secs))
    fe.scheduled = []
    fe._later = lambda secs, fn: fe.scheduled.append((secs, fn))
    if start:
        fe.start()
    return fe, client


def _card(**kw):
    return ConfirmationCard(
        kind="send", title="Send this reply?",
        fields=[CardField("To", "uma.vasquez18@example.edu"), CardField("Subject", "Re: review")],
        lines=["Draft *ready* for <https://x.org/d|the thread>"],
        body="Hi Maria,\n\nThe section is attached.\n\nKit",
        actions=[CardAction("approve_send", "Send", {"draft_id": "d1"}, style="primary",
                            word="send"),
                 CardAction("cancel_send", "Cancel", "d1", style="danger", word="cancel"),
                 CardAction("draft_handled", "Handled", "d1", word="handled"),
                 CardAction("edit_draft", "Edit", "d1")],
        **kw)


class Recorder:
    def __init__(self, fe):
        self.messages, self.actions, self.events = [], [], []
        fe.registry.event("message")(self.messages.append)
        fe.registry.action(__import__("re").compile(".*"))(self.actions.append)
        fe.registry.event(__import__("re").compile(".*"))(
            lambda p: self.events.append(p))


# startup and limits


def test_start_resolves_streams_and_limits(tmp_path):
    fe, c = make(tmp_path)
    assert fe.main_channel == f"stream:{EA}"
    assert fe.main_channel_name == "ea"
    assert fe.dm_channel(str(OWNER)) == f"stream:{DM}"
    assert fe.is_dm(f"stream:{DM}") and not fe.is_dm(f"stream:{EA}")
    assert fe.bot_user_id == str(BOT) and fe.owner_id == str(OWNER)
    assert fe.limits == Limits(9800, 2800, 60, 60)
    reg = c.named("register")[0]
    assert reg["apply_markdown"] is False
    assert "realm" in reg["fetch_event_types"]
    assert set(zf.EVENT_TYPES) <= set(reg["event_types"])


def test_limits_from_register_caps_message_length():
    assert zf.limits_from_register({"max_message_length": 50000, "max_topic_length": 80,
                                    "max_stream_name_length": 70}) == Limits(9800, 2800, 70, 80)
    assert zf.limits_from_register({"max_message_length": 4000}).message_chars == 3800


def test_start_rejects_public_stream(tmp_path):
    c = FakeZulipClient()
    c.streams["streams"][0]["invite_only"] = False
    with pytest.raises(ChatError, match="public"):
        make(tmp_path, c)


def test_start_rejects_unsubscribed_stream(tmp_path):
    c = FakeZulipClient()
    c.subs["subscriptions"] = [s for s in c.subs["subscriptions"] if s["stream_id"] != DM]
    with pytest.raises(ChatError, match="not subscribed.*ea-dm"):
        make(tmp_path, c)


def test_start_rejects_missing_stream(tmp_path):
    with pytest.raises(ChatError, match="not found"):
        make(tmp_path, sched_stream="ea-scheduling")


def test_api_key_only_from_constructor(tmp_path, monkeypatch):
    monkeypatch.setenv("ZULIP_API_KEY", "FROM-ENV")
    captured = {}

    class FakeModule:
        @staticmethod
        def Client(**kw):
            captured.update(kw)
            return FakeZulipClient()

    monkeypatch.setitem(sys.modules, "zulip", FakeModule)
    ZulipFrontend(CFG, api_key="FROM-ARG", private_dir=tmp_path / "p")
    assert captured == {"email": "ea-bot@example.org", "api_key": "FROM-ARG", "site": SITE}


def test_state_files_live_in_private_dir(tmp_path):
    fe, _ = make(tmp_path)
    priv = tmp_path / "priv"
    assert fe.queue_path.parent == priv and fe.card_registry.path.parent == priv
    assert stat.S_IMODE(fe.queue_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(priv.stat().st_mode) == 0o700


# identity


@pytest.mark.parametrize("topic", ["Reply to Maria", "", "a/b?c&d#e", "tilde~x",
                                   "café \U0001f600", "lock ABCD Re: review",
                                   "100% done. (really)"])
def test_thread_key_round_trip(topic):
    key = zf.thread_key_for(EA, topic)
    assert THREAD_KEY_RE.match(key), key
    assert zf.parse_thread_key(key) == (f"stream:{EA}", topic)


def test_parse_thread_key_dm_and_errors():
    assert zf.parse_thread_key("dm:9") == ("dm:9", None)
    for bad in ("", "stream:x:topic:a", "C123", "dm:x", "1700000000.0001"):
        with pytest.raises(ValueError):
            zf.parse_thread_key(bad)


# inbound translation


def _seen(value):
    return lambda key, mid: value


def test_inbound_stream_message():
    m = fx("event_stream_message")["message"]
    msg = zf.inbound_from_message(m, BOT, OWNER, DM, _seen(False), SITE)
    assert msg.channel == f"stream:{EA}" and msg.msg_id == "101"
    assert msg.thread_key == zf.thread_key_for(EA, "Reply to Maria about the review")
    assert msg.user_id == str(OWNER) and msg.posted_at == 1790000000.0
    assert not msg.is_reply and not msg.is_dm and not msg.is_self and not msg.is_bot
    assert msg.subtype == "" and msg.text == "please draft a *reply* to Maria"
    assert zf.inbound_from_message(m, BOT, OWNER, DM, _seen(True)).is_reply


def test_inbound_dm_stream_is_dm():
    msg = zf.inbound_from_message(fx("event_dm_stream_message")["message"], BOT, OWNER, DM,
                                  _seen(False))
    assert msg.channel == f"stream:{DM}" and msg.is_dm


def test_inbound_private_message_rolling_key():
    msg = zf.inbound_from_message(fx("event_private_message")["message"], BOT, OWNER, DM,
                                  _seen(True))
    assert msg.channel == f"dm:{OWNER}" and msg.thread_key == f"dm:{OWNER}"
    assert msg.is_dm and msg.is_reply and msg.subtype == ""


def test_inbound_group_dm_is_other():
    msg = zf.inbound_from_message(fx("event_group_dm")["message"], BOT, OWNER, DM, _seen(False))
    assert msg.subtype == "other:group_dm" and not msg.is_dm
    assert msg.channel == f"dm:{OWNER}-{OTHER}"
    assert THREAD_KEY_RE.match(msg.thread_key)


def test_inbound_bot_messages():
    own = zf.inbound_from_message(fx("event_bot_self_message")["message"], BOT, OWNER, DM,
                                  _seen(False))
    assert own.is_self and own.is_bot
    other = zf.inbound_from_message(fx("event_other_bot_message")["message"], BOT, OWNER, DM,
                                    _seen(False))
    assert other.is_bot and not other.is_self


def test_inbound_file_share():
    msg = zf.inbound_from_message(fx("event_file_message")["message"], BOT, OWNER, DM,
                                  _seen(False), SITE)
    assert msg.subtype == "file_share"
    assert msg.files == [FileRef(name="IMG_2041.jpg",
                                 url=f"{SITE}/user_uploads/2/ab/cdEF/IMG_2041.jpg",
                                 id="/user_uploads/2/ab/cdEF/IMG_2041.jpg",
                                 title="IMG_2041.jpg")]


def test_inbound_empty_topic():
    msg = zf.inbound_from_message(fx("event_empty_topic_message")["message"], BOT, OWNER, DM,
                                  _seen(False))
    assert msg.thread_key == f"stream:{EA}:topic:"


def test_inbound_unknown_type():
    assert zf.inbound_from_message({"id": 1, "type": "weird"}, BOT, OWNER, DM, _seen(False)) is None


def test_rename_event():
    msg = zf.rename_from_stream_event(fx("event_stream_rename"))
    assert msg.subtype == "channel_rename" and msg.new_name == "ea-new-name"
    assert msg.channel == "stream:21"
    assert zf.rename_from_stream_event(fx("event_stream_description")) is None


def test_rename_event_dispatched_and_name_cached(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.handle_event(fx("event_stream_rename"))
    assert rec.messages[0].subtype == "channel_rename"
    assert fe.mention("stream:21") == "#**ea-new-name**"
    fe.handle_event(fx("event_stream_description"))
    assert rec.events and rec.events[-1]["property"] == "description"


def test_message_event_dispatch_and_is_reply(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    c.messages = {**OK, "messages": [], "found_oldest": True}
    fe.handle_event(fx("event_stream_message"))
    ev2 = fx("event_stream_message")
    ev2["message"]["id"] = 110
    fe.handle_event(ev2)
    assert [m.is_reply for m in rec.messages] == [False, True]
    assert len(c.named("get_messages")) == 1
    assert fe.last_message_id == 110


# a post in general chat starts its own topic


def test_owner_post_in_general_chat_starts_its_own_topic(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    c.topics[EA] = ["General chat line", "briefings"]
    fe.handle_event(fx("event_empty_topic_message"))
    assert c.named("update_message")[-1] == {
        "message_id": 108, "topic": "general chat line (2)", "propagate_mode": "change_one",
        "send_notification_to_old_thread": False, "send_notification_to_new_thread": False}
    (msg,) = rec.messages
    assert msg.thread_key == zf.thread_key_for(EA, "general chat line (2)")
    assert msg.msg_id == "108" and not msg.is_reply
    fe.post(f"stream:{EA}", "on it", thread_ts=msg.thread_key)
    assert c.named("send_message")[-1]["topic"] == "general chat line (2)"


def test_new_thread_takes_the_namer_title(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    seen = []
    fe.thread_namer = lambda text, channel: seen.append((text, channel)) or "Maria review reply"
    c.topics[EA] = ["maria review reply"]
    fe.handle_event(fx("event_empty_topic_message"))
    assert seen == [("general chat line", f"stream:{EA}")]
    assert c.named("update_message")[-1]["topic"] == "Maria review reply (2)"
    assert rec.messages[0].thread_key == zf.thread_key_for(EA, "Maria review reply (2)")


@pytest.mark.parametrize("namer", [lambda t, c: None, lambda t, c: "  ",
                                   lambda t, c: 1 / 0])
def test_new_thread_falls_back_to_first_words(tmp_path, namer):
    fe, c = make(tmp_path)
    fe.thread_namer = namer
    fe.handle_event(fx("event_empty_topic_message"))
    assert c.named("update_message")[-1]["topic"] == "general chat line"


def test_slow_title_does_not_hold_the_event_loop(tmp_path):
    import threading
    fe, c = make(tmp_path)
    fe._run_later = ZulipFrontend._in_thread
    rec = Recorder(fe)
    release, done = threading.Event(), threading.Event()

    def namer(text, channel):
        release.wait(5)
        return "Slow title"

    fe.thread_namer = namer
    fe.registry.event("message")(lambda m: done.set())
    fe.handle_event(fx("event_empty_topic_message"))
    assert rec.messages == []
    release.set()
    assert done.wait(5)
    assert rec.messages[0].thread_key == zf.thread_key_for(EA, "Slow title")


def test_general_chat_under_its_display_name_is_moved_too(tmp_path):
    fe, c = make(tmp_path)
    ev = fx("event_empty_topic_message")
    ev["message"]["subject"] = zf.GENERAL_CHAT
    fe.handle_event(ev)
    assert c.named("update_message")[-1]["topic"] == "general chat line"


def test_other_posts_stay_where_they_are(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    c.messages = {**OK, "messages": [], "found_oldest": True}
    fe.handle_event(fx("event_stream_message"))
    other = fx("event_empty_topic_message")
    other["message"]["sender_id"] = OTHER
    fe.handle_event(other)
    own = fx("event_empty_topic_message")
    own["message"].update(id=109, sender_id=BOT)
    fe.handle_event(own)
    assert c.named("update_message") == []
    assert [m.thread_key for m in rec.messages] == [
        zf.thread_key_for(EA, "Reply to Maria about the review"),
        zf.thread_key_for(EA, ""), zf.thread_key_for(EA, "")]


def test_a_card_code_in_general_chat_confirms_nothing(tmp_path):
    """A code counts only in its card's own topic: typed in general chat it
    is an ordinary post, which starts a thread like any other."""
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    ev = _code_event(fe.last_card_row["code"])
    ev["message"]["subject"] = ""
    fe.handle_event(ev)
    assert rec.actions == [] and len(rec.messages) == 1


def test_refused_move_keeps_the_post_in_general_chat(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    c.update_results = [{"result": "error", "code": "BAD_REQUEST",
                         "msg": "You don't have permission to edit this message"}]
    fe.handle_event(fx("event_empty_topic_message"))
    assert rec.messages[0].thread_key == zf.thread_key_for(EA, "")


def test_bot_post_without_thread_avoids_a_topic_in_use(tmp_path):
    fe, c = make(tmp_path)
    c.topics[EA] = ["Morning briefing"]
    fe.post(f"stream:{EA}", "*Morning briefing*\nthree items")
    assert c.named("send_message")[-1]["topic"] == "Morning briefing (2)"


def test_channel_top_posts_in_general_chat(tmp_path):
    fe, c = make(tmp_path)
    c.topics[EA] = ["Partner Mapping"]
    top = fe.channel_top(f"stream:{EA}")
    ref = fe.post(f"stream:{EA}", ":file_folder: *Partner Mapping*  (`mapping`, research)",
                  thread_ts=top)
    assert c.named("send_message")[-1]["topic"] == ""
    assert ref.thread_key == top == f"stream:{EA}:topic:"
    assert fe.channel_top(f"dm:{OWNER}") is None


# moved threads

OLD_KEY = zf.thread_key_for(EA, "Reply to Maria about the review")
NEW_KEY = zf.thread_key_for(EA, "Maria review reply")


def test_thread_move_from_event_shapes():
    mv = zf.thread_move_from_event(fx("event_topic_move"))
    assert mv == {"type": "thread_moved", "old": OLD_KEY, "new": NEW_KEY,
                  "channel": f"stream:{EA}", "old_channel": f"stream:{EA}",
                  "message_ids": [101, 102], "whole": True, "user_id": str(OWNER)}
    # a whole topic moved to another stream keeps its name
    ev = fx("event_topic_move")
    del ev["subject"]
    ev["new_stream_id"] = DM
    mv = zf.thread_move_from_event(ev)
    assert mv["new"] == zf.thread_key_for(DM, "Reply to Maria about the review")
    assert mv["channel"] == f"stream:{DM}"
    # the owner's post leaving general chat, a move into it, an edit: none
    ev = fx("event_topic_move")
    ev.update(orig_subject=zf.GENERAL_CHAT, propagate_mode="change_one")
    assert zf.thread_move_from_event(ev) is None
    ev = fx("event_topic_move")
    ev["subject"] = zf.GENERAL_CHAT
    assert zf.thread_move_from_event(ev) is None
    assert zf.thread_move_from_event(fx("event_update_message")) is None


def test_whole_topic_move_reaches_the_core_and_late_posts_follow(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.handle_event(fx("event_stream_message"))
    fe.handle_event(fx("event_topic_move"))
    moved = [e for e in rec.events if e.get("type") == "thread_moved"]
    assert [(e["old"], e["new"]) for e in moved] == [(OLD_KEY, NEW_KEY)]
    assert json.loads((tmp_path / "priv" / zf.MOVES_FILE).read_text()) == {OLD_KEY: NEW_KEY}
    # a reply aimed at the old key, or at a moved message, lands in the new topic
    ref = fe.post(f"stream:{EA}", "late reply", thread_ts=OLD_KEY)
    assert c.named("send_message")[-1]["topic"] == "Maria review reply"
    assert ref.thread_key == NEW_KEY
    fe.post(f"stream:{EA}", "by id", thread_ts="101")
    assert c.named("send_message")[-1]["topic"] == "Maria review reply"
    # the map survives a restart
    fe2, c2 = make(tmp_path)
    fe2.post(f"stream:{EA}", "after restart", thread_ts=OLD_KEY)
    assert c2.named("send_message")[-1]["topic"] == "Maria review reply"


def test_a_move_by_anyone_but_the_owner_or_the_bot_rekeys_nothing(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.handle_event(fx("event_stream_message"))
    for who in (OTHER, None):
        ev = fx("event_topic_move")
        ev["new_stream_id"] = 30                       # into a public stream
        if who is None:
            del ev["user_id"]
        else:
            ev["user_id"] = who
        fe.handle_event(ev)
    assert not [e for e in rec.events if e.get("type") == "thread_moved"]
    assert not (tmp_path / "priv" / zf.MOVES_FILE).exists()
    fe.post(f"stream:{EA}", "stays", thread_ts=OLD_KEY)
    send = c.named("send_message")[-1]
    assert send["to"] == EA and send["topic"] == "Reply to Maria about the review"
    fe.post(f"stream:{EA}", "by id", thread_ts="101")
    send = c.named("send_message")[-1]
    assert send["to"] == EA and send["topic"] == "Reply to Maria about the review"
    # the bot's own move is followed
    ev = fx("event_topic_move")
    ev["user_id"] = BOT
    fe.handle_event(ev)
    assert [e["user_id"] for e in rec.events if e.get("type") == "thread_moved"] == [str(BOT)]


def test_start_refuses_a_stream_with_a_third_subscriber(tmp_path):
    """The DM stream gets every card with its full draft body, and the EA
    stream the briefings and drafts: the bot does not start while anyone
    besides the owner and the bot is subscribed to one."""
    for sid in (EA, DM):
        c = FakeZulipClient()
        c.members[sid] = [BOT, OWNER, OTHER]
        with pytest.raises(zf.ChannelNotPrivate, match=f"subscriber\\(s\\) {OTHER} besides"):
            make(tmp_path, client=c)
    c = FakeZulipClient()
    c.members[DM] = [BOT]                              # the owner left: a warning only
    make(tmp_path, client=c)


def test_start_refuses_when_the_subscribers_cannot_be_read(tmp_path):
    c = FakeZulipClient()
    real = c.call_endpoint

    def call(url=None, method="POST", request=None, **kw):
        if url and url.endswith("/members"):
            return {"result": "error", "msg": "no"}
        return real(url=url, method=method, request=request, **kw)
    c.call_endpoint = call
    with pytest.raises(ChatError, match="cannot read its subscribers"):
        make(tmp_path, client=c)


def test_start_checks_the_scheduling_stream_too(tmp_path):
    c = FakeZulipClient()
    c.members[21] = [BOT, OWNER, OTHER]
    c.subs["subscriptions"].append({"invite_only": True, "is_archived": False,
                                    "name": "ea-proj", "stream_id": 21})
    with pytest.raises(zf.ChannelNotPrivate, match="ea-proj"):
        make(tmp_path, client=c, sched_stream="ea-proj")


def test_a_public_stream_is_a_refusal_not_a_retry(tmp_path):
    c = FakeZulipClient()
    for s in c.streams["streams"]:
        if s["stream_id"] == EA:
            s["invite_only"] = False
    with pytest.raises(zf.ChannelNotPrivate, match="public"):
        make(tmp_path, client=c)


def test_chained_moves_and_a_move_back(tmp_path):
    fe, c = make(tmp_path)
    fe.handle_event(fx("event_topic_move"))
    ev = fx("event_topic_move")
    ev.update(orig_subject="Maria review reply", subject="Maria")
    fe.handle_event(ev)
    fe.post(f"stream:{EA}", "x", thread_ts=OLD_KEY)
    assert c.named("send_message")[-1]["topic"] == "Maria"
    ev = fx("event_topic_move")
    ev.update(orig_subject="Maria", subject="Reply to Maria about the review")
    fe.handle_event(ev)
    fe.post(f"stream:{EA}", "y", thread_ts=OLD_KEY)
    assert c.named("send_message")[-1]["topic"] == "Reply to Maria about the review"


def test_move_to_another_stream_posts_there(tmp_path):
    fe, c = make(tmp_path)
    ev = fx("event_topic_move")
    ev["new_stream_id"] = DM
    fe.handle_event(ev)
    fe.post(f"stream:{EA}", "late", thread_ts=OLD_KEY)
    send = c.named("send_message")[-1]
    assert send["to"] == DM and send["topic"] == "Maria review reply"


def test_partial_move_is_a_split_unless_it_emptied_the_topic(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    ev = fx("event_topic_move")
    ev["propagate_mode"] = "change_later"
    c.topics[EA] = ["reply to maria about the review", "Maria review reply"]
    fe.handle_event(ev)
    assert not [e for e in rec.events if e.get("type") == "thread_moved"]
    fe.post(f"stream:{EA}", "stays", thread_ts=OLD_KEY)
    assert c.named("send_message")[-1]["topic"] == "Reply to Maria about the review"
    c.topics[EA] = ["Maria review reply"]
    fe.handle_event(ev)
    assert [e["new"] for e in rec.events if e.get("type") == "thread_moved"] == [NEW_KEY]


def test_a_new_message_under_a_moved_name_revives_it(tmp_path):
    fe, c = make(tmp_path)
    fe.handle_event(fx("event_topic_move"))
    m = fx("event_stream_message")
    m["message"]["id"] = 150
    fe.handle_event(m)
    fe.post(f"stream:{EA}", "fresh", thread_ts=OLD_KEY)
    assert c.named("send_message")[-1]["topic"] == "Reply to Maria about the review"
    assert json.loads((tmp_path / "priv" / zf.MOVES_FILE).read_text()) == {}


def test_heartbeat_ignored(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.handle_event(fx("event_heartbeat"))
    assert rec.messages == [] and rec.events == []


# cards: rendering and registry


def test_card_rendering_and_registry_row(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(fe.dm_channel(str(OWNER)), _card())
    row = fe.last_card_row
    code = row["code"]
    assert row == fe.card_registry.by_ref(ref.channel, ref.id) | {"created": row["created"]}
    assert row["channel"] == f"stream:{DM}" and row["status"] == "live"
    send = c.named("send_message")[-1]
    assert send["type"] == "stream" and send["to"] == DM
    assert send["topic"] == f"lock {code} uma.vasquez18@example.edu"
    # The fields, the lines and the legend come first; the body after the
    # second rule. A field value is a code span; a link shows its target.
    assert send["content"] == (
        ":locked: **Send this reply?**\n"
        "**To:** `uma.vasquez18@example.edu`\n"
        "**Subject:** `Re: review`\n"
        "Draft **ready** for the thread (https://x.org/d)\n\n"
        "----\n"
        "Tap a reaction below: :+1: Send · :-1: Cancel\n"
        f"Or reply `<word> {code}` in this topic: send, cancel, handled.\n\n"
        "----\n"
        "**Body:**\n"
        "> Hi Maria,\n>\n> The section is attached.\n>\n> Kit")
    assert ref.thread_key == zf.thread_key_for(DM, send["topic"])
    assert row["card_thread"] == ref.thread_key


def _chip_card():
    return ConfirmationCard(
        kind="send", title="Send this reply?", fields=[CardField("To", "uma.vasquez18@example.edu")],
        actions=[CardAction("approve_send", ":outbox_tray: Send", {"draft_id": "d1"},
                            style="primary", word="send"),
                 CardAction("cancel_send", "Cancel", "d1", style="danger", word="cancel"),
                 CardAction("edit_draft", ":pencil2: Edit", "d1", word="edit"),
                 CardAction("draft_handled", ":white_check_mark: Handled", "d1", word="handled"),
                 CardAction("sent_myself", ":incoming_envelope: Sent myself", "d1", word="sent")])


def test_a_card_carries_one_reaction_chip_per_action(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    chips = [r["emoji_name"] for r in c.named("add_reaction") if r["message_id"] == int(ref.id)]
    assert chips == ["+1", "-1", "pencil2", "white_check_mark", "incoming_envelope"]
    row = fe.last_card_row
    assert row["chips"] == chips and row["reactions"]["pencil2"] == "edit_draft"
    text = c.named("send_message")[-1]["content"]
    assert ("Tap a reaction below: :+1: Send · :-1: Cancel · :pencil: Edit · "
            ":check: Handled · :mail_received: Sent myself") in text


def _tap(fe, ref, name, code="", user=OWNER):
    ev = {"type": "reaction", "op": "add", "user_id": user, "message_id": int(ref.id),
          "emoji_name": name, "emoji_code": code, "reaction_type": "unicode_emoji"}
    return zf.reaction_action(ev, str(OWNER), fe._card_row_for, str(BOT))


def test_a_tap_on_a_chip_picks_its_action(tmp_path):
    fe, _ = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    assert _tap(fe, ref, "+1", "1f44d").action_id == "approve_send"
    assert _tap(fe, ref, "pencil", "270f").action_id == "edit_draft"
    assert _tap(fe, ref, "mail_received", "1f4e8").action_id == "sent_myself"
    # the check chip means Handled on this card, never Send
    assert _tap(fe, ref, "check", "2705").action_id == "draft_handled"
    assert _tap(fe, ref, "cross_mark", "274c").action_id == "cancel_send"  # legacy cancel
    assert _tap(fe, ref, "pencil", "270f", user=BOT) is None, "the bot's own chip is not a tap"
    # Even under an owner id misconfigured to the bot's own, its chips are no tap.
    ev = {"type": "reaction", "op": "add", "user_id": BOT, "message_id": int(ref.id),
          "emoji_name": "+1", "emoji_code": "1f44d", "reaction_type": "unicode_emoji"}
    assert zf.reaction_action(ev, str(BOT), fe._card_row_for, str(BOT)) is None
    assert zf.reaction_action(ev, str(BOT), fe._card_row_for, "") is None


def test_a_card_that_withheld_send_offers_no_thumbs_up(tmp_path):
    """The Send card for a draft longer than a card shows has no Send and no
    default: its legend offers no thumbs up, and a thumbs up picks nothing
    (it once opened Edit, the first action left)."""
    import dataclasses
    fe, c = make(tmp_path)
    card = _chip_card()
    card = dataclasses.replace(card, actions=card.actions[2:3] + card.actions[1:2] + card.actions[3:],
                               no_default=True)
    ref = fe.post_card(f"stream:{DM}", card)
    chips = [r["emoji_name"] for r in c.named("add_reaction") if r["message_id"] == int(ref.id)]
    assert "+1" not in chips and chips[0] == "pencil2"
    text = c.named("send_message")[-1]["content"]
    assert ":+1:" not in text and ":pencil: Edit" in text
    assert _tap(fe, ref, "+1", "1f44d") is None
    assert _tap(fe, ref, "pencil", "270f").action_id == "edit_draft"


def test_start_refuses_an_owner_id_equal_to_the_bots(tmp_path):
    from chat.base import ChatError
    fe, _ = make(tmp_path, start=False, owner_user_id=BOT)
    with pytest.raises(ChatError, match="bot's own user id"):
        fe.start()


def test_an_answered_card_loses_the_bots_chips(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    fe.replace_card_with_status(ref, "Sent.")
    removed = [r["emoji_name"] for r in c.named("remove_reaction") if r["message_id"] == int(ref.id)]
    assert removed == ["+1", "-1", "pencil2", "white_check_mark", "incoming_envelope"]


def test_chips_go_on_after_the_card_has_gone_out_alone(tmp_path):
    # The web client drops a reaction that shares an event batch with its
    # new message, and a background window polls up to a second late, so
    # the first chip must come more than a second after the card.
    fe, c = make(tmp_path)
    fe.post_card(f"stream:{DM}", _chip_card())
    names = [n for n, _ in c.calls]
    sent = len(names) - 1 - names[::-1].index("send_message")
    first_chip = names.index("add_reaction", sent)
    waits = [a for n, a in c.calls[sent:first_chip] if n == "sleep"]
    assert waits and sum(waits) > 1.0 and zf.CHIP_DELAY > 1.0


def test_a_card_without_chips_does_not_wait(tmp_path):
    fe, c = make(tmp_path)
    card = _card()
    card.actions = []  # any action gets a chip: the primary one gets +1
    fe.post_card(f"stream:{DM}", card)
    assert not c.named("sleep") and not c.named("add_reaction")


def test_a_card_answered_during_the_wait_gets_no_chips(tmp_path):
    fe, c = make(tmp_path)

    def answered(secs):
        fe.card_registry.retire(MessageRef(f"stream:{DM}", str(c._next_id), ""))

    fe._sleep = answered
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    assert not c.named("add_reaction")
    assert fe.card_registry.by_ref(ref.channel, ref.id)["status"] == "retired"


def test_chips_are_put_back_later_while_the_card_is_live(tmp_path):
    # A hidden desktop window polls once a minute and pulls the card and
    # its chips in one batch, which the web client drops. Later passes take
    # each chip off and put it back, on a message the client now holds.
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    assert [s for s, _ in fe.scheduled] == list(zf.CHIP_REFRESH_AT)
    assert min(zf.CHIP_REFRESH_AT) > 60.0, "after a throttled client's next poll"
    chips = fe.last_card_row["chips"]
    before = len(c.calls)
    first, second = (fn for _, fn in fe.scheduled)
    assert first() == len(chips)
    moves = [(n, r["emoji_name"]) for n, r in c.calls[before:]
             if n in ("remove_reaction", "add_reaction") and r["message_id"] == int(ref.id)]
    assert moves == [m for name in chips for m in (("remove_reaction", name), ("add_reaction", name))]
    # an answered card is left as it is
    fe.card_registry.retire(ref)
    before = len(c.calls)
    assert second() == 0 and c.calls[before:] == []


def test_a_card_without_chips_schedules_no_refresh(tmp_path):
    fe, c = make(tmp_path)
    card = _card()
    card.actions = []
    fe.post_card(f"stream:{DM}", card)
    assert fe.scheduled == []


def test_a_rate_limited_chip_is_tried_again(tmp_path):
    fe, c = make(tmp_path)
    limited = {"result": "error", "code": "RATE_LIMIT_HIT", "retry-after": 0.4,
               "msg": "API usage exceeded rate limit"}
    c.reaction_results["+1"] = [dict(limited), dict(limited)]
    c.reaction_results["-1"] = [dict(limited)] * (zf.RATE_RETRIES + 1)
    ref = fe.post_card(f"stream:{DM}", _chip_card())
    tries = [r["emoji_name"] for r in c.named("add_reaction") if r["message_id"] == int(ref.id)]
    assert tries.count("+1") == 3 and tries.count("-1") == zf.RATE_RETRIES + 1
    assert c.named("sleep").count(0.4) == 2 + zf.RATE_RETRIES
    chips = fe.last_card_row["chips"]
    assert "+1" in chips and "-1" not in chips, "a chip still limited after the retries is skipped"


def test_card_topic_cut_to_limit():
    card = ConfirmationCard("send", "T", fields=[CardField("Subject", "word " * 40)])
    topic = zf.card_topic(card, "ABCD", 60)
    assert len(topic) <= 60 and topic.startswith("lock ABCD word")


def test_update_card_keeps_code(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _card())
    code = fe.last_card_row["code"]
    card2 = _card()
    card2.title = "Send this revised reply?"
    assert fe.update_card(ref, card2)
    upd = c.named("update_message")[-1]
    assert upd["message_id"] == int(ref.id)
    assert "Send this revised reply?" in upd["content"] and f"`<word> {code}`" in upd["content"]
    assert fe.card_registry.by_ref(ref.channel, ref.id)["code"] == code


def test_replace_card_with_status_retires_row(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _card())
    code = fe.last_card_row["code"]
    assert fe.replace_card_with_status(ref, ":white_check_mark: *Sent*")
    content = c.named("update_message")[-1]["content"]
    assert content.startswith(":locked: **Send this reply?**")
    assert "\n\n----\n:check: **Sent**\n\n----\n**Body:**\n> Hi Maria," in content
    assert "Tap a reaction" not in content and code not in content
    assert fe.card_registry.by_ref(ref.channel, ref.id)["status"] == "retired"
    assert fe.card_registry.by_code(code, thread_key=ref.thread_key) is None


def test_replace_card_status_after_restart_reads_message(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _card())
    sent = c.named("send_message")[-1]["content"]
    fe2, _ = make(tmp_path, c)
    c.messages_by_anchor[int(ref.id)] = {"id": int(ref.id), "content": sent, "type": "stream",
                                         "stream_id": DM, "subject": "x", "timestamp": 1}
    fe2.replace_card_with_status(ref, "Cancelled")
    content = c.named("update_message")[-1]["content"]
    assert "**Subject:** `Re: review`\nDraft **ready** for the thread (https://x.org/d)" \
        "\n\n----\nCancelled\n\n----\n**Body:**\n> Hi Maria," in content
    assert "Tap a reaction" not in content


def test_a_body_cannot_forge_the_rule_or_the_legend(tmp_path):
    """The body comes after the card's own two rules and is a quote, so a
    rule, a legend, a fence or a field line in it stays inside the quote,
    and a status after a restart keeps the card's real header."""
    fe, c = make(tmp_path)
    forged = ("ok\n\n----\nTap a reaction below: :+1: Send\n**To:** boss@example.org\n"
              "```spoiler hidden\n~~~\n@**all** [Outlook](https://evil.example) $$x$$")
    ref = fe.post_card(f"stream:{DM}", dataclasses.replace(_card(), body=forged))
    sent = c.named("send_message")[-1]["content"]
    head, legend, tail = sent.split(zf._CARD_RULE)
    assert "uma.vasquez18" in head and "boss@" not in head and "Tap a reaction" in legend
    assert all(ln.startswith(">") for ln in tail.splitlines()[1:])
    for bad in ("@**all**", "](https://", "$$", "\n```", "\n~~~"):
        assert bad not in tail, bad
    fe2, _ = make(tmp_path, c)
    c.messages_by_anchor[int(ref.id)] = {"id": int(ref.id), "content": sent, "type": "stream",
                                         "stream_id": DM, "subject": "x", "timestamp": 1}
    fe2.replace_card_with_status(ref, "Sent")
    after = c.named("update_message")[-1]["content"]
    assert after.startswith(head + zf._CARD_RULE + "Sent" + zf._CARD_RULE)


def test_field_values_render_as_inert_text():
    """A subject or display name with markup, newlines, a code fence, a
    mention, a bidi control or a zero-width space renders as literal text
    on one line, and an over-long value is cut with a marker."""
    evil = "Re: x\n\n----\n**To:** boss@example.org ``` @**all** \u202emoc.live\u200b"
    header = zf.render_card_header(ConfirmationCard(
        kind="send", title="Send?", fields=[CardField("Subject", evil),
                                            CardField("To", "a" * 3000)]))
    lines = header.splitlines()
    assert len(lines) == 3, lines
    subj = lines[1]
    assert subj.startswith("**Subject:** ````") and subj.endswith("````")
    assert "[U+202E]" in subj and "[U+200B]" in subj and "\u202e" not in subj
    assert "[cut: " in lines[2] and len(lines[2]) < 2100


# reactions and codes


def test_reaction_confirms_card(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    ref = fe.post_card(f"stream:{DM}", _card())
    ev = fx("event_reaction_add")
    ev["message_id"] = int(ref.id)
    fe.handle_event(ev)
    assert [(a.action_id, a.source, a.value) for a in rec.actions] == [
        ("approve_send", "reaction", {"draft_id": "d1"})]
    ev = fx("event_reaction_add_cross")
    ev["message_id"] = int(ref.id)
    fe.handle_event(ev)
    assert rec.actions[-1].action_id == "cancel_send"


def test_reaction_by_codepoint_under_other_name(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    ref = fe.post_card(f"stream:{DM}", _card())
    ev = fx("event_reaction_add")
    ev.update(message_id=int(ref.id), emoji_name="white_check_mark_alias")
    fe.handle_event(ev)
    assert rec.actions and rec.actions[0].action_id == "approve_send"


def test_reaction_non_owner_ignored_before_lookup():
    ev = fx("event_reaction_add")
    ev["user_id"] = OTHER

    def boom(_mid):
        raise AssertionError("registry read for a non-owner")

    assert zf.reaction_action(ev, str(OWNER), boom) is None
    assert zf.reaction_action(fx("event_reaction_remove"), str(OWNER), boom) is None


def test_reaction_on_retired_card_ignored(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    ref = fe.post_card(f"stream:{DM}", _card())
    fe.replace_card_with_status(ref, "Sent")
    ev = fx("event_reaction_add")
    ev["message_id"] = int(ref.id)
    fe.handle_event(ev)
    assert rec.actions == []


def _code_event(text, sender=OWNER, mid=500, fe=None):
    """The owner's message in the DM stream; with `fe`, in the topic of the
    card it posted last, where its code counts."""
    ev = fx("event_dm_stream_message")
    ev["message"].update(id=mid, content=text, sender_id=sender)
    if fe is not None:
        ev["message"]["subject"] = zf.parse_thread_key(fe.last_card_row["card_thread"])[1]
    return ev


def test_code_reply_consumed(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    code = fe.last_card_row["code"]
    fe.handle_event(_code_event(f"cancel {code.lower()}", fe=fe))
    assert rec.messages == []
    assert [(a.action_id, a.source) for a in rec.actions] == [("cancel_send", "code")]
    react = c.named("add_reaction")[-1]
    assert react["message_id"] == 500 and react["emoji_name"] == "white_check_mark"


def test_bare_code_selects_primary(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    fe.handle_event(_code_event(fe.last_card_row["code"], fe=fe))
    assert rec.actions[0].action_id == "approve_send"


def test_a_code_in_another_topic_is_a_message(tmp_path):
    """A code confirms only in its card's topic: in another topic (another
    card's, or a conversation the model is in) it is an ordinary message."""
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    first = fe.last_card_row["code"]
    fe.post_card(f"stream:{DM}", _card())
    fe.handle_event(_code_event(first, fe=fe))   # in the second card's topic
    assert rec.actions == [] and len(rec.messages) == 1


def test_code_from_non_owner_is_a_message(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    fe.handle_event(_code_event(f"send {fe.last_card_row['code']}", sender=OTHER))
    assert rec.actions == [] and len(rec.messages) == 1


def test_code_from_bot_itself_ignored(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    fe.handle_event(_code_event(f"send {fe.last_card_row['code']}", sender=BOT))
    assert rec.actions == [] and rec.messages[0].is_self


def test_code_action_checks_owner_before_registry():
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("registry read")

    msg = zf.inbound_from_message(_code_event("send ABCD", sender=OTHER)["message"], BOT, OWNER,
                                  DM, _seen(False))
    assert zf.code_action(msg, Boom(), str(OWNER), str(BOT)) is None
    msg = zf.inbound_from_message(_code_event("send ABCD", sender=BOT)["message"], BOT, OWNER,
                                  DM, _seen(False))
    assert zf.code_action(msg, Boom(), str(OWNER), str(BOT)) is None
    assert zf.code_action(msg, Boom(), str(BOT), str(BOT)) is None


def test_unknown_word_gets_reply(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.post_card(f"stream:{DM}", _card())
    code = fe.last_card_row["code"]
    fe.handle_event(_code_event(f"archive {code}", fe=fe))
    assert rec.actions == [] and rec.messages == []
    reply = c.named("send_message")[-1]
    assert reply["content"] == (f"Card **{code}** takes `send {code}`, `cancel {code}`, "
                                f"`handled {code}`.")
    assert reply["topic"] == zf.parse_thread_key(fe.last_card_row["card_thread"])[1]


def test_code_like_word_without_card_passes_through(tmp_path):
    fe, _ = make(tmp_path)
    rec = Recorder(fe)
    fe.handle_event(_code_event("help"))
    assert rec.actions == [] and rec.messages[0].text == "help"


# outbound messages


def test_post_topic_derivation_and_markup(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post(f"stream:{EA}", "*Briefing* for <https://a.org/x|today> :white_check_mark:\nmore")
    send = c.named("send_message")[-1]
    assert send == {"type": "stream", "to": EA, "topic": "Briefing for today",
                    "content": "**Briefing** for [today](https://a.org/x) :check:\nmore"}
    assert ref.thread_key == zf.thread_key_for(EA, "Briefing for today")
    assert ref.id == "301" and ref.channel == f"stream:{EA}"
    assert ref.permalink == f"{SITE}/#narrow/channel/7-ea/topic/Briefing.20for.20today/near/301"


def test_post_in_thread_and_channel_mention(tmp_path):
    fe, c = make(tmp_path)
    key = zf.thread_key_for(EA, "alpha topic")
    fe.post(f"stream:{EA}", "see <#stream:8>", thread_ts=key)
    send = c.named("send_message")[-1]
    assert send["topic"] == "alpha topic" and send["content"] == "see #**ea-dm**"
    with pytest.raises(ChatError):
        fe.post(f"stream:{DM}", "x", thread_ts=key)


def test_post_dm(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post(f"dm:{OWNER}", "hi _there_")
    assert c.named("send_message")[-1] == {"type": "private", "to": [OWNER],
                                           "content": "hi *there*"}
    assert ref.thread_key == f"dm:{OWNER}"
    assert ref.permalink == f"{SITE}/#narrow/dm/{BOT}-dm/near/{ref.id}"


def test_post_bare_message_id_thread(tmp_path):
    fe, c = make(tmp_path)
    first = fe.post(f"stream:{EA}", "opening line")
    fe.post(f"stream:{EA}", "reply", thread_ts=first.id)
    assert c.named("send_message")[-1]["topic"] == "opening line"


def test_update_edit_window_falls_back_to_reply(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post(f"stream:{EA}", "status: working")
    c.update_results = [fx("api_edit_time_limit")]
    assert fe.update(ref, "status: *done*") is False
    send = c.named("send_message")[-1]
    assert send["topic"] == "status: working" and send["content"] == "status: **done**"
    assert fe.update(ref, "again") is True


def test_status_reaction_fallback_and_cache(tmp_path):
    fe, c = make(tmp_path)
    c.reaction_results = {"white_check_mark": fx("api_invalid_emoji"),
                          "hourglass_flowing_sand": fx("api_invalid_emoji")}
    assert fe.react(f"stream:{EA}", "101", Status.DONE)
    assert [r["emoji_name"] for r in c.named("add_reaction")] == ["white_check_mark", "check"]
    c.calls.clear()
    assert fe.react(f"stream:{EA}", "102", Status.DONE)
    assert [r["emoji_name"] for r in c.named("add_reaction")] == ["check"]
    assert fe.react(f"stream:{EA}", "102", Status.WAITING)
    assert c.named("add_reaction")[-1]["emoji_name"] == "hourglass"
    assert fe.unreact(f"stream:{EA}", "102", Status.DONE)
    assert c.named("remove_reaction")[-1]["emoji_name"] == "check"
    c.reaction_results["eyes"] = fx("api_reaction_exists")
    assert fe.react(f"stream:{EA}", "102", Status.EYES)


def test_react_other_error_stops(tmp_path):
    fe, c = make(tmp_path)
    c.reaction_results = {"eyes": {"result": "error", "code": "BAD_REQUEST",
                                   "msg": "Invalid message(s)"}}
    assert fe.react(f"stream:{EA}", "999", Status.EYES) is False


# permalinks and urls


def test_encode_hash_component():
    assert zf.encode_hash_component("a b.c(d)%e/f") == "a.20b.2Ec.28d.29.25e.2Ff"


def test_room_url_and_permalink(tmp_path):
    fe, c = make(tmp_path)
    assert fe.room_url(f"stream:{EA}") == f"{SITE}/#narrow/channel/7-ea"
    c.messages_by_anchor[77] = {"id": 77, "type": "stream", "stream_id": DM,
                                "subject": "lock AB2C Re: (x)", "timestamp": 5}
    assert fe.permalink(f"stream:{DM}", "77") == (
        f"{SITE}/#narrow/channel/8-ea-dm/topic/lock.20AB2C.20Re.3A.20.28x.29/near/77")
    assert fe.mention(f"stream:{EA}") == "#**ea**"


def test_message_posted_at(tmp_path):
    fe, c = make(tmp_path)
    c.messages_by_anchor[77] = {"id": 77, "timestamp": 1790000000}
    assert fe.message_posted_at(MessageRef(f"stream:{EA}", "77", "k")) == 1790000000.0
    assert fe.message_posted_at(MessageRef(f"stream:{EA}", "78", "k")) is None


# history


def test_thread_replies(tmp_path):
    fe, c = make(tmp_path)
    c.messages = fx("api_messages_topic")
    out = fe.thread_replies(f"stream:{EA}", zf.thread_key_for(EA, "alpha"), limit=50)
    req = c.named("get_messages")[-1]
    assert req["narrow"] == [{"operator": "channel", "operand": EA},
                             {"operator": "topic", "operand": "alpha"}]
    assert req["anchor"] == "newest" and req["num_before"] == 50
    assert req["apply_markdown"] is False
    assert [m.msg_id for m in out] == ["202", "203", "205"]
    assert [m.is_reply for m in out] == [False, True, True]
    assert out[1].is_self


def test_recent_messages_groups_by_topic(tmp_path):
    fe, c = make(tmp_path)
    c.messages = fx("api_messages_channel")
    out = fe.recent_messages(f"stream:{EA}", 1790000000)
    assert [(m.msg_id, m.reply_count) for m in out] == [("202", 2), ("204", 0), ("206", 0)]
    assert all(not m.is_reply for m in out)
    out = fe.recent_messages(f"stream:{EA}", 0, since_id="203")
    assert [(m.msg_id, m.reply_count) for m in out] == [("204", 0), ("205", 0), ("206", 0)]
    assert c.named("get_messages")[-1]["num_before"] == 200


# rooms


def test_create_private_room(tmp_path):
    fe, c = make(tmp_path)
    room = fe.create_private_room("ea-proj-new", "Project room")
    sub = c.named("add_subscriptions")[-1]
    assert sub == {"streams": [{"name": "ea-proj-new", "description": "Project room"}],
                   "invite_only": True, "principals": ["ea-bot@example.org"]}
    assert room.id.startswith("stream:") and room.name == "ea-proj-new"
    assert room.url.startswith(f"{SITE}/#narrow/channel/")
    with pytest.raises(RoomNameTaken):
        fe.create_private_room("ea")


def test_a_settled_card_is_filed_out_of_the_inbox(tmp_path):
    """A card settled outside chat moves, with its topic and without a
    notice, to a private done stream the bot makes on first use."""
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _card())
    assert fe.file_card(ref) is True
    sub = c.named("add_subscriptions")[-1]
    assert sub == {"streams": [{"name": "ea-dm-done", "description": "Confirmation cards "
                                "settled outside chat (sent or deleted in the mail client)."}],
                   "invite_only": True, "history_public_to_subscribers": True,
                   "principals": ["ea-bot@example.org"]}
    done = c.stream_ids["ea-dm-done"]
    moves = [u for u in c.named("update_message") if "stream_id" in u]
    assert moves == [{"message_id": int(ref.id), "stream_id": done, "propagate_mode": "change_all",
                      "send_notification_to_old_thread": False,
                      "send_notification_to_new_thread": False}]
    ref2 = fe.post_card(f"stream:{DM}", _card())
    assert fe.file_card(ref2) is True
    assert len(c.named("add_subscriptions")) == 1, "the stream is made once"


def test_a_card_is_not_filed_where_a_third_person_reads(tmp_path):
    fe, c = make(tmp_path)
    ref = fe.post_card(f"stream:{DM}", _card())
    fe.file_card(ref)                                  # makes the stream
    done = c.stream_ids["ea-dm-done"]
    c.members[done] = [BOT, OWNER, 77]
    before = len(c.named("update_message"))
    assert fe.file_card(fe.post_card(f"stream:{DM}", _card())) is False
    assert not [u for u in c.named("update_message")[before:] if "stream_id" in u]
    c.members[done] = [BOT, OWNER]
    c.update_results = [{"result": "error", "msg": "The time limit for editing this message has past"}]
    assert fe.file_card(fe.post_card(f"stream:{DM}", _card())) is False
    assert fe.file_card(MessageRef(f"dm:{OWNER}", "5", f"dm:{OWNER}")) is False


def test_create_room_not_permitted(tmp_path):
    fe, c = make(tmp_path)
    c.add_subs_result = {"result": "error", "code": "BAD_REQUEST",
                         "msg": "Insufficient permission"}
    with pytest.raises(NotPermitted):
        fe.create_private_room("ea-x")


def test_invite_describe_archive(tmp_path):
    fe, c = make(tmp_path)
    fe.invite("stream:21", str(OWNER))
    assert c.named("add_subscriptions")[-1] == {"streams": [{"name": "ea-proj"}],
                                                "principals": [OWNER]}
    fe.set_description("stream:21", "desc")
    assert c.named("update_stream")[-1] == {"stream_id": 21, "description": "desc"}
    with pytest.raises(AlreadyArchived):
        fe.archive("stream:21")
    fe.archive(f"stream:{EA}")
    assert c.named("delete_stream") == [EA]


def test_unarchive(tmp_path):
    fe, c = make(tmp_path)
    fe.unarchive("stream:21")
    assert c.named("update_stream")[-1] == {"stream_id": 21, "is_archived": False}
    with pytest.raises(NotArchived):
        fe.unarchive(f"stream:{EA}")
    c.update_stream_result = {**OK, "ignored_parameters_unsupported": ["is_archived"]}
    with pytest.raises(NotSupported):
        fe.unarchive("stream:21")
    c.update_stream_result = {"result": "error", "code": "BAD_REQUEST",
                              "msg": "Invalid parameter 'is_archived'"}
    with pytest.raises(NotSupported):
        fe.unarchive("stream:21")


def test_archived_map_and_channel_info(tmp_path):
    fe, c = make(tmp_path)
    assert fe.archived_map() == {f"stream:{EA}": False, "stream:21": True}
    assert c.named("get_streams")[-1] == {"include_all": True, "exclude_archived": False}
    info = fe.channel_info(f"stream:{EA}")
    assert info.is_private and info.is_member and not info.is_archived and not info.is_dm
    info = fe.channel_info("stream:21")
    assert info.is_archived and not info.is_member
    assert fe.channel_info(f"stream:{DM}").is_dm
    assert not fe.channel_info("stream:999").exists
    dm = fe.channel_info(f"dm:{OWNER}")
    assert dm.is_dm and dm.is_private and dm.is_member


# files


def test_upload_posts_link(tmp_path):
    fe, c = make(tmp_path)
    f = tmp_path / "local.pdf"
    f.write_bytes(b"%PDF")
    ref = fe.upload(f"stream:{EA}", f, filename="report.pdf", title="The report",
                    comment="Here is the *report*")
    assert c.named("upload_file")[-1] == {"name": "report.pdf", "data": b"%PDF"}
    send = c.named("send_message")[-1]
    assert send["content"] == ("Here is the **report**\n"
                               "[The report](/user_uploads/2/ab/xYz/report.pdf)")
    assert send["topic"] == "Here is the report"
    assert ref.url == f"{SITE}/user_uploads/2/ab/xYz/report.pdf" and ref.name == "report.pdf"


def test_upload_to_thread_outside_channel_uploads_nothing(tmp_path):
    fe, c = make(tmp_path)
    f = tmp_path / "outline.md"
    f.write_bytes(b"# Outline")
    with pytest.raises(ChatError, match="is not in"):
        fe.upload(f"stream:{EA}", f,
                  thread_ts=zf.thread_key_for(30, "general chat"))
    assert c.named("upload_file") == []
    assert c.named("send_message") == []


def test_download_uses_basic_auth_and_stays_on_site(tmp_path, monkeypatch):
    fe, _ = make(tmp_path)
    # A sandboxed EA_DIR: resident_io writes the download directly.
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    got = {}

    class Resp:
        status_code = 200
        content = b"data"

    class Req:
        @staticmethod
        def get(url, auth=None, timeout=None):
            got.update(url=url, auth=auth)
            return Resp()

    monkeypatch.setitem(sys.modules, "requests", Req)
    path = fe.download(FileRef("a b.jpg", id="/user_uploads/2/x/a b.jpg"), tmp_path / "dl")
    assert Path(path).read_bytes() == b"data" and Path(path).name == "a_b.jpg"
    assert got == {"url": f"{SITE}/user_uploads/2/x/a b.jpg",
                   "auth": ("ea-bot@example.org", "KEY")}
    assert fe.download(FileRef("x", url="https://evil.example/x"), tmp_path / "dl") is None


# the event loop


def test_queue_persistence_and_resume(tmp_path):
    fe, c = make(tmp_path)
    c.events = [{**OK, "events": [fx("event_heartbeat") | {"id": 4},
                                   fx("event_dm_stream_message") | {"id": 5}]}]
    assert fe.poll_once() == 2
    saved = json.loads(fe.queue_path.read_text())
    assert saved == {"queue_id": "q-1", "last_event_id": 5, "last_message_id": 102,
                     "limits": [9800, 2800, 60, 60]}
    assert c.named("get_events")[-1] == {"queue_id": "q-1", "last_event_id": -1,
                                         "dont_block": False}
    fe2, _ = make(tmp_path, c)
    assert c.registers == 1
    fe2.poll_once()
    assert c.named("get_events")[-1]["last_event_id"] == 5
    assert fe2.queue_id == "q-1" and fe2.last_message_id == 102


def test_bad_event_queue_reregisters(tmp_path):
    fe, c = make(tmp_path)
    rec = Recorder(fe)
    fe.last_message_id = 150
    c.events = [fx("api_bad_queue")]
    assert fe.poll_once() == 0
    assert c.registers == 2 and fe.queue_id == "q-2"
    assert rec.events[-1] == {"last_message_id": 150}
    assert json.loads(fe.queue_path.read_text())["queue_id"] == "q-2"


def test_run_forever_stops(tmp_path):
    fe, c = make(tmp_path)
    orig = c.get_events

    def once(**kw):
        fe.stop()
        return orig(**kw)

    c.get_events = once
    fe.run_forever()


# LLM-facing


def test_prompt_strings():
    from chat.memory_frontend import SLACK_LIKE_PROMPT_STRINGS
    ps = ZulipFrontend.prompt_strings_for()
    assert set(ps) == set(SLACK_LIKE_PROMPT_STRINGS)
    assert ps["confirm_location"] == ("the confirmation card is in your ea-dm stream: react "
                                      "with :+1: or reply with its code")
    # an install that renames the DM stream is told its own name
    renamed = ZulipFrontend.prompt_strings_for("e2e-dm")
    for key in ("confirm_location", "confirm_sentence", "confirm_sentence_wrapped"):
        assert "your e2e-dm stream" in renamed[key] and "ea-dm" not in renamed[key], key
    assert not any("{dm_stream}" in v for v in renamed.values())
    assert ps["history_note"] == "" and ps["room_word"] == "stream"
    assert "mrkdwn" in ps["format_rule_main"]



def test_prompt_strings_name_the_configured_dm_stream(tmp_path):
    fe, _ = make(tmp_path, start=False, dm_stream="e2e-dm")
    assert "your e2e-dm stream" in fe.prompt_strings()["confirm_sentence"]
    import chat as chat_pkg
    cfg_dir = tmp_path / "ea" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "ea-config.json").write_text(json.dumps(
        {"chat": {"frontend": "zulip", "zulip": {**CFG, "dm_stream": "e2e-dm"}}}))
    ps = chat_pkg.prompt_strings_for_config(tmp_path / "ea")
    assert "your e2e-dm stream" in ps["confirm_location"]

def test_no_dashes_in_module():
    src = "".join((ROOT / p).read_text() for p in (
        "botcore/botcore/chat/zulip_frontend.py", "scripts/zulip_smoke.py",
        "tests/test_zulip_frontend.py"))
    assert "\u2014" not in src and "\u2013" not in src



def test_channel_ids_are_known_at_construction(tmp_path):
    """The core reads its channel constants at import, before start(), so
    the adapter resolves the stream ids as soon as it has a client."""
    fe, _client = make(tmp_path, start=False, sched_stream="ea-scheduling")
    assert fe.main_channel.startswith("stream:")
    # the fake realm has no scheduling stream: the best-effort lookup leaves it empty
    assert fe.sched_channel == ""
    assert fe.dm_stream_id is not None
    assert fe.dm_channel(fe.owner_id) == f"stream:{fe.dm_stream_id}"


def test_cards_mention_the_owner_when_the_name_is_known(tmp_path):
    """A card notifies the owner on every client: it opens with the owner's
    mention when the user lookup succeeds, and without it otherwise."""
    from chat.cards import CardAction, ConfirmationCard
    client = FakeZulipClient()
    client.get_user_by_id = lambda uid: {"result": "success", "user": {"user_id": uid, "full_name": "Kit Owner"}}
    fe, _ = make(tmp_path, client=client)
    card = ConfirmationCard(kind="send", title=":lock: **Send?**", actions=[
        CardAction("approve_send", "Send", {"d": 1}, style="primary", word="send"),
        CardAction("cancel_send", "Cancel", {"d": 1}, style="danger", word="cancel")])
    fe.post_card(fe.dm_channel(fe.owner_id), card)
    sent = client.named("send_message")[-1]["content"]
    assert sent.startswith(f"@**Kit Owner|{fe.owner_id}** ")


def test_card_title_keeps_one_lock_and_one_bold():
    """The bot writes card titles for Slack: `:lock: *Confirm send*`. The
    Zulip header supplies its own lock and bold, so the title's are
    dropped first; a title with bold of its own is not wrapped again."""
    assert zf.card_title_line(":lock: *Confirm send*") == ":locked: **Confirm send**"
    assert zf.card_title_line("Send this reply?") == ":locked: **Send this reply?**"
    assert zf.card_title_line(":lock: **Send?**") == ":locked: **Send?**"
    dash, arrow = chr(0x2014), chr(0x2192)
    inv = zf.card_title_line(f":lock: *Send reviewer invitation* {dash} *Gus Yarborough* {arrow} `JRNL-2026-9008`")
    assert inv == f":locked: **Send reviewer invitation** {dash} **Gus Yarborough** {arrow} `JRNL-2026-9008`"
    header = zf.render_card_header(ConfirmationCard(
        kind="event", title=":lock: *Confirm calendar invite*",
        fields=[CardField("Subject", "Robot stack access")]))
    assert header.startswith(":locked: **Confirm calendar invite**\n**Subject:** `Robot stack access`")
    assert "****" not in header and header.count(":locked:") == 1


def test_a_rendered_card_uses_only_zulip_emoji_names():
    """Zulip shows a Slack-only shortcode as plain text. The header's lock is
    built after the converter runs, so it must already be Zulip's name;
    every other name in the card has been through the converter."""
    from chat.text import EMOJI_ALIASES
    card = ConfirmationCard(
        kind="event", title=":lock: *Confirm calendar invite*",
        fields=[CardField("Subject", "Sync")],
        lines=[":warning: *Invitees not free then:* x :white_check_mark:"],
        actions=[CardAction("approve_event", ":calendar: Create", {"d": 1}, style="primary",
                            word="create"),
                 CardAction("cancel_event", "Cancel", {"d": 1}, style="danger", word="cancel")])
    text = zf.render_card(card, "AB12")
    names = re.findall(r":([a-z0-9_+-]+):", text)
    assert "locked" in names
    slack_only = [n for n in names if n in EMOJI_ALIASES]
    assert not slack_only, slack_only


# shadow mode: the writes this frontend makes on its own reach the shadow


def _shadowed(tmp_path):
    """A started Zulip frontend inside a ShadowFrontend whose shadow is a
    real web chat store (no server); the channel rows are copied."""
    from chat.shadow_frontend import ShadowFrontend
    from chat.web_frontend import WebFrontend
    fe, c = make(tmp_path)
    web = WebFrontend({"port": 0}, store_path=tmp_path / "web" / "webchat.sqlite",
                      webui_dir=tmp_path / "webui")
    comp = ShadowFrontend(fe, web)
    comp.mirror_channels()
    return comp, fe, c, web


def test_a_typed_code_reaches_the_shadow_through_the_hook(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    dm = f"stream:{DM}"
    answered = []

    @comp.registry.action("cancel_send")
    def cancel(ev):
        answered.append(ev)
        comp.replace_card_with_status(ev.card, ":x: Cancelled")

    cref = comp.post_card(dm, _card())
    assert web.store.message(dm, cref.id)["reactions"] == []     # the chips stay on Zulip
    code = fe.last_card_row["code"]
    fe.handle_event(_code_event(f"cancel {code}", fe=fe))
    assert [(a.action_id, a.source) for a in answered] == [("cancel_send", "code")]
    row = web.store.message(dm, "500")
    assert (row["author"], row["text"], row["thread_key"]) == ("owner", f"cancel {code}",
                                                               cref.thread_key)
    assert row["reactions"] == ["white_check_mark"]                # the done mark
    card_row = web.store.message(dm, cref.id)
    assert card_row["kind"] == "status" and card_row["text"] == ":x: Cancelled"

    comp.post_card(dm, _card())
    code2 = fe.last_card_row["code"]
    fe.handle_event(_code_event(f"archive {code2}", mid=501, fe=fe))
    assert web.store.message(dm, "501")["author"] == "owner"
    reply = web.store.message(dm, str(c._next_id))                # the frontend's answer
    assert reply["author"] == "bot" and reply["text"].startswith(f"Card **{code2}** takes")
    assert reply["thread_key"] == web.store.message(dm, "501")["thread_key"]


def test_a_chip_tap_whose_status_edit_is_refused_reaches_the_shadow(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    dm = f"stream:{DM}"

    @comp.registry.action("approve_send")
    def send(ev):
        assert comp.replace_card_with_status(ev.card, ":white_check_mark: Sent") is False

    cref = comp.post_card(dm, _chip_card())
    chips = [r["emoji_name"] for r in c.named("add_reaction")]
    assert chips and web.store.message(dm, cref.id)["reactions"] == []
    c.update_results = [fx("api_edit_time_limit")]
    fe.handle_event(dict(fx("event_reaction_add"), emoji_name="+1", emoji_code="1f44d",
                         message_id=int(cref.id)))
    sent = c.named("send_message")[-1]
    assert sent["content"].endswith(":check: Sent")                # a new message on Zulip
    row = web.store.message(dm, cref.id)                           # the same row on the web
    assert row["kind"] == "status" and row["text"] == ":white_check_mark: Sent"
    assert web.store.message(dm, str(c._next_id)) is None          # and no second one


def test_edits_past_the_edit_window_reach_the_shadow(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    ea, dm = f"stream:{EA}", f"stream:{DM}"
    ref = comp.post(ea, "status: working")
    c.update_results = [fx("api_edit_time_limit")]
    assert comp.update(ref, "status: *done*") is False
    assert web.store.message(ea, ref.id)["text"] == "status: **done**"
    cref = comp.post_card(dm, _card())
    revised = dataclasses.replace(_card(), title="Send the revised reply?")
    c.update_results = [fx("api_edit_time_limit")]
    assert comp.update_card(cref, revised) is False
    assert web.store.message(dm, cref.id)["card"]["title"] == "Send the revised reply?"


def test_stream_changes_reach_the_shadow(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    assert web.store.channel("stream:21")["archived"] is True      # ea-proj, from the start
    fe.handle_event(fx("event_stream_description"))
    assert web.store.channel("stream:21")["description"] == "Updated description"
    fe.handle_event({"id": 15, "type": "stream", "op": "create",
                     "streams": [{"stream_id": 21, "name": "ea-proj"}]})
    assert web.store.channel("stream:21")["archived"] is False
    fe.handle_event({"id": 16, "type": "stream", "op": "delete",
                     "streams": [{"stream_id": 21, "name": "ea-proj"}]})
    assert web.store.channel("stream:21")["archived"] is True
    fe.handle_event(fx("event_stream_rename"))
    assert web.store.channel("stream:21")["name"] == "ea-new-name"
    fe.handle_event(dict(fx("event_stream_description"), stream_id=30))
    assert web.store.channel("stream:30") is None                  # not a stream it holds


def test_channel_changes_from_event_shapes():
    assert zf.channel_changes_from_event(fx("event_stream_description")) == [
        ("stream:21", {"description": "Updated description"})]
    assert zf.channel_changes_from_event(fx("event_stream_rename")) == []
    assert zf.channel_changes_from_event({"type": "stream", "op": "update", "stream_id": 4,
                                          "property": "is_archived", "value": True}) == [
        ("stream:4", {"archived": True})]
    assert zf.channel_changes_from_event({"type": "stream", "op": "delete", "streams": [
        {"stream_id": 4}, {"stream_id": 5}, "junk"]}) == [
        ("stream:4", {"archived": True}), ("stream:5", {"archived": True})]
    assert zf.channel_changes_from_event(fx("event_stream_message")) == []


def test_a_topic_move_rekeys_the_shadow(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    ea = f"stream:{EA}"
    first = comp.post(ea, "the draft", thread_ts=OLD_KEY)
    second = comp.post(ea, "a revision", thread_ts=OLD_KEY)
    fe.handle_event(fx("event_topic_move"))          # the owner's whole-topic move
    assert [m["id"] for m in web.store.thread(ea, NEW_KEY)] == [first.id, second.id]
    assert web.store.thread(ea, OLD_KEY) == []


def test_zulip_history_reaches_the_shadow_as_written(tmp_path):
    comp, fe, c, web = _shadowed(tmp_path)
    assert fe.history_markdown is True
    msgs = fx("api_messages_channel")
    for m in msgs["messages"]:
        if m["id"] == 203:
            m["content"] = "Done, *at nine*."                     # Zulip's italic
    c.messages = msgs
    report = comp.backfill(days=3650)
    assert report["channels"][f"stream:{EA}"] == len(msgs["messages"])
    assert report["channels"][f"stream:{DM}"] == 0                # another stream's rows
    row = web.store.message(f"stream:{EA}", "203")
    assert (row["author"], row["text"]) == ("bot", "Done, *at nine*.")
    assert web.store.message(f"stream:{EA}", "202")["author"] == "owner"
    assert row["thread_key"] == zf.thread_key_for(EA, "alpha")
    assert comp.backfill(days=3650)["copied"] == 0


def test_channel_info_carries_the_description(tmp_path):
    fe, c = make(tmp_path)
    c.streams["streams"][0]["description"] = "The EA stream"
    assert fe.channel_info(f"stream:{EA}").description == "The EA stream"


def test_a_rate_limited_history_read_is_tried_again(tmp_path):
    fe, c = make(tmp_path)
    limited = {"result": "error", "code": "RATE_LIMIT_HIT", "retry-after": 2.0,
               "msg": "API usage exceeded rate limit"}
    answers = [dict(limited), dict(limited), {**OK, "messages": [], "found_oldest": True}]
    c.get_messages = lambda req: answers.pop(0)
    msgs, oldest = fe._history([{"operator": "channel", "operand": 4}], 200)
    assert msgs == [] and oldest and not answers
    assert c.named("sleep") == [2.0, 2.0]
    # a read limited past every retry raises, as a plain failure does
    c.get_messages = lambda req: dict(limited)
    with pytest.raises(ChatError):
        fe._history([{"operator": "channel", "operand": 4}], 200)
    assert len(c.named("sleep")) == 2 + zf.HISTORY_RATE_RETRIES
