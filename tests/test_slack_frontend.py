"""Tests for the Slack frontend: construction, listener registration, event
translation, the Web API wrappers and the prompt wording. No network: the
WebClient is a recording fake and slack_bolt.App is swapped for a fake
that records listener registrations."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat import (  # noqa: E402
    AlreadyArchived,
    ChatError,
    NotArchived,
    NotPermitted,
    RoomNameTaken,
    load_frontend,
    prompt_strings_for_config,
)
from chat.slack_frontend import SlackFrontend  # noqa: E402
from chat.types import ActionEvent, FileRef, InboundMessage, MessageRef, Status, ViewEvent  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "slack"
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class SlackError(Exception):
    """Shaped like slack_sdk's SlackApiError: str() names the error and
    .response carries it."""

    def __init__(self, code: str):
        super().__init__(f"The request to the Slack API failed.\n"
                         f"The server responded with: {{'ok': False, 'error': '{code}'}}")
        self.response = {"ok": False, "error": code}


class FakeClient:
    """Records every Web API call as (method, kwargs). `responses[method]`
    is a dict to return, a list consumed in order, or a callable; an
    exception in `errors[method]` is raised instead."""

    def __init__(self, responses=None, errors=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = dict(responses or {})
        self.errors = dict(errors or {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kw):
            self.calls.append((name, copy.deepcopy(kw)))
            if name in self.errors:
                raise self.errors[name]
            r = self.responses.get(name, {"ok": True})
            if callable(r):
                return r(**kw)
            if isinstance(r, list):
                return r.pop(0)
            return r
        return call

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


class FakeBolt:
    """Stands in for slack_bolt.App: records listeners in registration order."""

    instances: list = []

    def __init__(self, token=None, **kw):
        self.token = token
        self.client = FakeClient({"auth_test": {"ok": True, "user_id": "U_BOT",
                                                "team_id": "T_TEAM"}})
        self.listeners: list[tuple[str, object, object]] = []
        FakeBolt.instances.append(self)

    def _reg(self, kind, key):
        def deco(fn):
            self.listeners.append((kind, key, fn))
            return fn
        return deco

    def action(self, key):
        return self._reg("action", key)

    def view(self, key):
        return self._reg("view", key)

    def event(self, key):
        return self._reg("event", key)

    def find(self, kind, key):
        for k, kk, fn in self.listeners:
            if k == kind and (kk == key or (isinstance(kk, re.Pattern) and kk.pattern == key)):
                return fn
        raise KeyError((kind, key))


ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test",
       "EA_SLACK_CHANNEL_NAME": "ai-assistant", "EA_SLACK_CHANNEL_ID": "C_MAIN",
       "EA_SCHED_CHANNEL_ID": "  C_SCHED \n", "EA_SLACK_OWNER_ID": "U_OWNER"}


@pytest.fixture
def fake_bolt(monkeypatch):
    import slack_bolt
    FakeBolt.instances.clear()
    monkeypatch.setattr(slack_bolt, "App", FakeBolt)
    return FakeBolt


def cli_frontend(client=None, **kw) -> SlackFrontend:
    fe = SlackFrontend("xoxb-test", channel_name="ai-assistant", channel_id="C_MAIN",
                       owner_id="U_OWNER", for_cli=True,
                       client=client if client is not None else FakeClient(), **kw)
    fe.bot_user_id = "U_BOT"
    return fe


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------

def test_from_env_reads(fake_bolt):
    fe = SlackFrontend.from_env(ENV)
    assert fe.name == "slack"
    assert fe.main_channel == "C_MAIN" and fe.main_channel_name == "ai-assistant"
    assert fe.sched_channel == "C_SCHED"
    assert fe.owner_id == "U_OWNER"
    assert isinstance(fe.bolt, FakeBolt) and fe.bolt.token == "xoxb-test"
    assert fe.client is fe.bolt.client
    assert fe.limits == (3000, 2800, 80, 0)
    assert fe.capabilities == {"buttons", "modals", "home_tab", "unfurl", "deep_links",
                               "select", "thread_broadcast", "reactions", "files",
                               "private_rooms"}
    assert fe.supports("buttons") and not fe.supports("codes")


def test_from_env_optional_values(fake_bolt):
    env = {k: v for k, v in ENV.items() if k not in ("EA_SCHED_CHANNEL_ID", "EA_SLACK_OWNER_ID")}
    fe = SlackFrontend.from_env(env)
    assert fe.sched_channel == "" and fe.owner_id == ""


@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
                                     "EA_SLACK_CHANNEL_NAME", "EA_SLACK_CHANNEL_ID"])
def test_from_env_hard_reads(fake_bolt, missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(KeyError, match=missing):
        SlackFrontend.from_env(env)


def test_from_env_for_cli_needs_no_app_token(fake_bolt):
    import slack_sdk
    env = {k: v for k, v in ENV.items() if k != "SLACK_APP_TOKEN"}
    fe = SlackFrontend.from_env(env, for_cli=True)
    assert fe.bolt is None
    assert isinstance(fe.client, slack_sdk.WebClient)
    assert not FakeBolt.instances
    with pytest.raises(KeyError, match="EA_SLACK_CHANNEL_ID"):
        SlackFrontend.from_env({k: v for k, v in env.items() if k != "EA_SLACK_CHANNEL_ID"},
                               for_cli=True)
    with pytest.raises(ChatError):
        fe.run_forever()


def test_load_frontend_builds_slack(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": {"frontend": "slack"}}))
    fe = load_frontend(for_cli=True, ea_dir=tmp_path, env=ENV)
    assert type(fe).__name__ == "SlackFrontend" and fe.bolt is None
    assert prompt_strings_for_config(tmp_path) == SlackFrontend.PROMPT_STRINGS


# ---------------------------------------------------------------------------
# lifecycle and listeners
# ---------------------------------------------------------------------------

def test_start_order_and_dispatch(fake_bolt):
    fe = SlackFrontend.from_env(ENV)
    bolt = fe.bolt

    # A bolt-style handler (has `body`) goes straight to bolt via the bridge.
    @fe.registry.action("legacy_button")
    def legacy(ack, body, client, logger):
        pass

    seen: dict = {}

    @fe.registry.action("approve_send")
    def approve(ev):
        seen["action"] = ev

    @fe.registry.view("edit_draft_modal")
    def modal(ev):
        seen["view"] = ev

    @fe.registry.event("message")
    def on_msg(msg):
        seen["message"] = msg

    @fe.registry.event("home_opened")
    def home(event):
        seen["home"] = event

    @fe.registry.event(re.compile(".*"))
    def other(event):
        seen.setdefault("other", []).append(event)

    assert bolt.listeners == [("action", "legacy_button", legacy)]
    fe.start()
    assert fe.bot_user_id == "U_BOT"
    kinds = [(k, key.pattern if isinstance(key, re.Pattern) else key)
             for k, key, _ in bolt.listeners]
    assert kinds == [("action", "legacy_button"), ("event", "message"),
                     ("event", "app_home_opened"), ("action", ".*"), ("view", ".*"),
                     ("event", ".*")]

    acks = []
    bolt.find("action", ".*")(ack=lambda *a, **k: acks.append("action"),
                              body=fixture("action_click"))
    assert acks == ["action"]
    assert isinstance(seen["action"], ActionEvent)
    assert seen["action"].value["draft_id"] == "AAMkDRAFT1"

    bolt.find("view", ".*")(ack=lambda *a, **k: acks.append("view"),
                            body=fixture("view_submission"))
    assert acks[-1] == "view" and isinstance(seen["view"], ViewEvent)

    bolt.find("event", "message")(event=fixture("channel_message"))
    assert isinstance(seen["message"], InboundMessage) and seen["message"].text == "usage"

    bolt.find("event", "app_home_opened")(event={"type": "app_home_opened", "user": "U_OWNER"})
    assert seen["home"]["user"] == "U_OWNER"

    bolt.find("event", ".*")(event={"type": "member_joined_channel", "channel": "C_MAIN"})
    assert seen["other"][0]["type"] == "member_joined_channel"

    with pytest.raises(ChatError, match="after start"):
        @fe.registry.action("late_legacy")
        def late(ack, body):
            pass


def test_start_survives_auth_failure(fake_bolt):
    fe = SlackFrontend.from_env(ENV)
    fe.client = FakeClient(errors={"auth_test": SlackError("invalid_auth")})
    fe.start()
    assert fe.bot_user_id == ""


def test_run_forever_starts_socket_mode(fake_bolt, monkeypatch):
    import slack_bolt.adapter.socket_mode as sm
    started = []

    class FakeHandler:
        def __init__(self, app, token):
            started.append((app, token))

        def start(self):
            started.append("started")

    monkeypatch.setattr(sm, "SocketModeHandler", FakeHandler)
    fe = SlackFrontend.from_env(ENV)
    fe.run_forever()
    assert started == [(fe.bolt, "xapp-test"), "started"]


def test_ping():
    assert cli_frontend(FakeClient({"auth_test": {"ok": True}})).ping()
    assert not cli_frontend(FakeClient({"auth_test": {"ok": False}})).ping()
    assert not cli_frontend(FakeClient(errors={"auth_test": SlackError("x")})).ping()


# ---------------------------------------------------------------------------
# event translation
# ---------------------------------------------------------------------------

def test_inbound_channel_message():
    m = cli_frontend()._inbound(fixture("channel_message"))
    assert (m.channel, m.msg_id, m.text, m.user_id) == ("C_MAIN", "1700000100.000200",
                                                        "usage", "U_OWNER")
    assert m.thread_key is None and not m.is_reply
    assert not m.is_dm and not m.is_bot and not m.is_self
    assert m.subtype == "" and m.files == [] and m.posted_at == 1700000100.0002
    assert m.raw["team"] == "T_TEAM"


def test_inbound_im_message():
    m = cli_frontend()._inbound(fixture("im_message"))
    assert m.is_dm and m.channel == "D_OWNER" and m.subtype == ""


def test_inbound_file_share():
    m = cli_frontend()._inbound(fixture("file_share"))
    assert m.subtype == "file_share"
    assert m.files[0] == FileRef(
        name="receipt 1.pdf",
        url="https://files.slack.com/files-pri/T_TEAM-F0001/receipt_1.pdf",
        id="F0001", mimetype="application/pdf", title="Hotel receipt")
    assert [f.name for f in m.files] == ["receipt 1.pdf", "photo.jpg"]


def test_inbound_channel_rename():
    m = cli_frontend()._inbound(fixture("channel_rename"))
    assert m.subtype == "channel_rename" and m.new_name == "ea-new-name"
    assert m.channel == "C_ROOM1"


def test_inbound_bot_message():
    m = cli_frontend()._inbound(fixture("bot_message"))
    assert m.is_bot and m.is_self
    assert m.subtype == "other:bot_message"
    assert m.thread_key == "1700000100.000200" and m.is_reply


def test_inbound_thread_reply():
    m = cli_frontend()._inbound(fixture("thread_reply"))
    assert m.thread_key == "1700000100.000200" and m.is_reply
    assert not m.is_bot and not m.is_self


@pytest.mark.parametrize("raw,want", [
    (None, ""), ("file_share", "file_share"), ("thread_broadcast", "thread_broadcast"),
    ("message_changed", "other:message_changed"), ("channel_join", "other:channel_join")])
def test_inbound_subtypes(raw, want):
    ev = {"type": "message", "channel": "C_MAIN", "ts": "1.2", "user": "U_OWNER"}
    if raw is not None:
        ev["subtype"] = raw
    assert cli_frontend()._inbound(ev).subtype == want


def test_action_event_json_value():
    ev = cli_frontend()._action_event(fixture("action_click"))
    assert ev.action_id == "approve_send" and ev.user_id == "U_OWNER"
    assert ev.source == "button" and ev.selected == ""
    assert ev.block_id == "confirm_send_1700000000.000100"
    assert ev.trigger_id == "111.222.abc"
    assert ev.value == {"thread_ts": "1700000000.000100", "draft_id": "AAMkDRAFT1",
                        "conversation_id": "AAQkCONV1", "ea_channel": "C_MAIN",
                        "email_system": "outlook", "gmail_email": ""}
    assert list(ev.value) == ["thread_ts", "draft_id", "conversation_id", "ea_channel",
                              "email_system", "gmail_email"]
    assert ev.card == MessageRef("D_OWNER", "1700000001.000200", "1700000001.000200",
                                 1700000001.0002)


@pytest.mark.parametrize("raw", ["b0b0b0b0b0b0", "[1, 2]", "42", "not json {"])
def test_action_event_raw_value(raw):
    body = fixture("action_click")
    body["actions"][0]["value"] = raw
    assert cli_frontend()._action_event(body).value == raw


def test_action_event_no_value():
    body = fixture("action_click")
    del body["actions"][0]["value"]
    assert cli_frontend()._action_event(body).value == ""


def test_resolve_card_unfurl_redirect():
    body = fixture("unfurl_click")
    ref = SlackFrontend._resolve_card(body)
    assert ref == MessageRef("D_OWNER", "1700000001.000200", "1700000001.000200",
                             1700000001.0002)
    ev = cli_frontend()._action_event(body)
    assert ev.card.channel == "D_OWNER" and ev.action_id == "cancel_send"


def test_resolve_card_plain_click_keeps_thread():
    body = fixture("unfurl_click")
    body["message"]["attachments"] = [{"is_msg_unfurl": False, "channel_id": "D_X", "ts": "9.9"}]
    ref = SlackFrontend._resolve_card(body)
    assert ref == MessageRef("C_MAIN", "1700000002.000300", "1700000000.000100",
                             1700000002.0003)


def test_resolve_card_home_click():
    ref = SlackFrontend._resolve_card({"type": "block_actions", "user": {"id": "U_OWNER"},
                                       "view": {"type": "home"}})
    assert ref == MessageRef("", "", "", None)


def test_action_event_static_select():
    ev = cli_frontend()._action_event(fixture("static_select_click"))
    assert ev.source == "select" and ev.selected == "budget-plan-2027"
    assert ev.action_id == "room_pick_project" and ev.value == ""
    assert ev.block_id.startswith("room-pick::")
    assert ev.card.thread_key == "1700000000.000100"


def test_view_event():
    ev = SlackFrontend._view_event(fixture("view_submission"))
    assert ev.callback_id == "edit_draft_modal" and ev.user_id == "U_OWNER"
    assert json.loads(ev.private_metadata)["draft_id"] == "AAMkDRAFT1"
    assert ev.values["body_block"]["body"]["value"].startswith("Hi Alex")


# ---------------------------------------------------------------------------
# outbound
# ---------------------------------------------------------------------------

def test_post_default_unfurl_kwargs():
    client = FakeClient({"chat_postMessage": {"ok": True, "ts": "1700000009.000100"}})
    ref = cli_frontend(client).post("C_MAIN", "hello", thread_ts="1700000000.000100")
    assert client.calls == [("chat_postMessage", {"channel": "C_MAIN", "text": "hello",
                                                  "thread_ts": "1700000000.000100"})]
    assert ref == MessageRef("C_MAIN", "1700000009.000100", "1700000000.000100",
                             1700000009.0001)


def test_post_top_level_and_no_unfurl():
    client = FakeClient({"chat_postMessage": {"ok": True, "ts": "5.5"}})
    fe = cli_frontend(client)
    ref = fe.post("C_MAIN", "top")
    assert client.calls[0] == ("chat_postMessage", {"channel": "C_MAIN", "text": "top"})
    assert ref.thread_key == "5.5"
    fe.post("C_MAIN", "link", thread_ts="1.1", unfurl=False)
    assert client.calls[1][1] == {"channel": "C_MAIN", "thread_ts": "1.1", "text": "link",
                                  "unfurl_links": False, "unfurl_media": False}


def test_post_reads_slack_response_data():
    resp = SimpleNamespace(data={"ok": True, "ts": "7.7"})
    ref = cli_frontend(FakeClient({"chat_postMessage": resp})).post("C_MAIN", "x")
    assert ref.id == "7.7" and ref.posted_at == 7.7


def test_update():
    client = FakeClient()
    assert cli_frontend(client).update(MessageRef("C_MAIN", "1.1", "1.1"), "new")
    assert client.calls == [("chat_update", {"channel": "C_MAIN", "ts": "1.1", "text": "new"})]


def test_react_and_unreact():
    client = FakeClient()
    fe = cli_frontend(client)
    assert fe.react("C_MAIN", "1.1", Status.EYES)
    assert fe.unreact("C_MAIN", "1.1", "hourglass_flowing_sand")
    assert client.calls == [
        ("reactions_add", {"channel": "C_MAIN", "name": "eyes", "timestamp": "1.1"}),
        ("reactions_remove", {"channel": "C_MAIN", "name": "hourglass_flowing_sand",
                              "timestamp": "1.1"})]


def test_react_swallows_errors():
    fe = cli_frontend(FakeClient(errors={"reactions_add": SlackError("already_reacted"),
                                         "reactions_remove": SlackError("no_reaction")}))
    assert fe.react("C_MAIN", "1.1", Status.DONE) is False
    assert fe.unreact("C_MAIN", "1.1", Status.DONE) is False


def test_replace_card_with_status_swallows_errors():
    fe = cli_frontend(FakeClient(errors={"chat_update": SlackError("msg_too_long")}))
    assert fe.replace_card_with_status(MessageRef("D_OWNER", "1.1", "1.1"), "done") is False


def test_message_posted_at():
    fe = cli_frontend()
    assert fe.message_posted_at(MessageRef("D", "1700000001.000200", "x")) == 1700000001.0002
    assert fe.message_posted_at(MessageRef("D", "", "x")) is None
    assert fe.message_posted_at(MessageRef("D", "m1", "x")) is None
    assert fe.client.calls == []


def test_dm_channel():
    client = FakeClient({"conversations_open": {"ok": True, "channel": {"id": "D_OWNER"}}})
    fe = cli_frontend(client)
    assert fe.dm_channel("U_OWNER") == "D_OWNER"
    assert client.calls == [("conversations_open", {"users": ["U_OWNER"]})]
    assert fe.dm_channel("") == ""
    fe.client = FakeClient(errors={"conversations_open": SlackError("user_not_found")})
    assert fe.dm_channel("U_OWNER") == ""


def test_is_dm_and_mention():
    fe = cli_frontend()
    assert fe.is_dm("D_OWNER") and not fe.is_dm("C_MAIN") and not fe.is_dm("")
    assert fe.mention("C123ABC") == "<#C123ABC>"


def test_channel_info():
    client = FakeClient({"conversations_info": {"ok": True, "channel": {
        "id": "C_MAIN", "name": "ai-assistant", "is_channel": True, "is_private": True,
        "is_member": True, "is_archived": False}}})
    info = cli_frontend(client).channel_info("C_MAIN")
    assert (info.id, info.name, info.is_private, info.is_member, info.exists) == \
        ("C_MAIN", "ai-assistant", True, True, True)
    public = cli_frontend(FakeClient({"conversations_info": {"channel": {
        "id": "C_PUB", "name": "general", "is_channel": True, "is_private": False,
        "is_member": True}}})).channel_info("C_PUB")
    assert public.is_private is False
    im = cli_frontend(FakeClient({"conversations_info": {"channel": {
        "id": "D_OWNER", "is_im": True}}})).channel_info("D_OWNER")
    assert im.is_dm and im.is_private
    gone = cli_frontend(FakeClient(errors={
        "conversations_info": SlackError("channel_not_found")})).channel_info("C_GONE")
    assert gone.exists is False
    with pytest.raises(ChatError):
        cli_frontend(FakeClient(errors={
            "conversations_info": SlackError("ratelimited")})).channel_info("C_MAIN")


def test_room_url_caches_team():
    client = FakeClient({"auth_test": {"ok": True, "team_id": "T_TEAM"}})
    fe = cli_frontend(client)
    assert fe.room_url("C1") == "https://app.slack.com/client/T_TEAM/C1"
    assert fe.room_url("C2") == "https://app.slack.com/client/T_TEAM/C2"
    assert client.methods() == ["auth_test"]
    bad = cli_frontend(FakeClient(errors={"auth_test": SlackError("x")}))
    assert bad.room_url("C1") == "https://slack.com/app_redirect?channel=C1"


def test_permalink():
    client = FakeClient({"chat_getPermalink": {"ok": True, "permalink": "https://x/p1"}})
    assert cli_frontend(client).permalink("D_OWNER", "1.1") == "https://x/p1"
    assert client.calls == [("chat_getPermalink", {"channel": "D_OWNER", "message_ts": "1.1"})]


def test_thread_replies():
    msgs = [dict(fixture("channel_message"), thread_ts="1700000100.000200", reply_count=2),
            fixture("bot_message"), fixture("thread_reply")]
    for m in msgs:
        m.pop("channel", None)
        m.pop("channel_type", None)
    client = FakeClient({"conversations_replies": {"ok": True, "messages": msgs}})
    out = cli_frontend(client).thread_replies("C_MAIN", "1700000100.000200", limit=50)
    assert client.calls == [("conversations_replies",
                             {"channel": "C_MAIN", "ts": "1700000100.000200", "limit": 50})]
    assert [m.msg_id for m in out] == ["1700000100.000200", "1700000500.000600",
                                       "1700000600.000700"]
    assert all(m.channel == "C_MAIN" for m in out)
    assert not out[0].is_reply and out[0].reply_count == 2
    assert out[1].is_self and out[2].is_reply


def test_recent_messages_oldest_first():
    msgs = [{"type": "message", "user": "U_OWNER", "text": "newer", "ts": "20.0"},
            {"type": "message", "user": "U_OWNER", "text": "older", "ts": "10.0",
             "thread_ts": "10.0", "reply_count": 3}]
    client = FakeClient({"conversations_history": {"ok": True, "messages": msgs}})
    out = cli_frontend(client).recent_messages("C_MAIN", 5.5, limit=40)
    assert client.calls == [("conversations_history",
                             {"channel": "C_MAIN", "oldest": "5.5", "limit": 40})]
    assert [m.text for m in out] == ["older", "newer"]
    assert out[0].reply_count == 3 and not out[0].is_reply and out[1].posted_at == 20.0
    client.responses["conversations_history"] = {"ok": True, "messages": msgs}
    later = cli_frontend(client).recent_messages("C_MAIN", 5.5, since_id="10.0")
    assert [m.text for m in later] == ["newer"]


def test_create_private_room():
    client = FakeClient({"conversations_create": {"ok": True, "channel": {"id": "C_NEW"}},
                         "auth_test": {"ok": True, "team_id": "T_TEAM"}})
    room = cli_frontend(client).create_private_room("ea-proj")
    assert client.calls[0] == ("conversations_create", {"name": "ea-proj", "is_private": True})
    assert (room.id, room.name, room.url) == ("C_NEW", "ea-proj",
                                              "https://app.slack.com/client/T_TEAM/C_NEW")
    client2 = FakeClient({"conversations_create": {"ok": True, "channel": {"id": "C_NEW"}}})
    cli_frontend(client2).create_private_room("ea-proj", "EA project channel: P (p)")
    assert ("conversations_setTopic", {"channel": "C_NEW",
                                       "topic": "EA project channel: P (p)"}) in client2.calls


@pytest.mark.parametrize("code,exc", [("name_taken", RoomNameTaken),
                                      ("missing_scope", NotPermitted),
                                      ("restricted_action", ChatError)])
def test_create_private_room_errors(code, exc):
    fe = cli_frontend(FakeClient(errors={"conversations_create": SlackError(code)}))
    with pytest.raises(exc) as ei:
        fe.create_private_room("ea-proj")
    assert code in str(ei.value)


def test_invite_and_set_description():
    client = FakeClient()
    fe = cli_frontend(client)
    fe.invite("C_NEW", "U_OWNER")
    fe.set_description("C_NEW", "topic")
    assert client.calls == [("conversations_invite", {"channel": "C_NEW", "users": "U_OWNER"}),
                            ("conversations_setTopic", {"channel": "C_NEW", "topic": "topic"})]


def test_archive_errors():
    fe = cli_frontend(FakeClient(errors={"conversations_archive": SlackError("already_archived"),
                                         "conversations_unarchive": SlackError("not_archived")}))
    with pytest.raises(AlreadyArchived):
        fe.archive("C1")
    with pytest.raises(NotArchived):
        fe.unarchive("C1")
    other = cli_frontend(FakeClient(errors={"conversations_archive": SlackError("not_in_channel")}))
    with pytest.raises(ChatError) as ei:
        other.archive("C1")
    assert not isinstance(ei.value, AlreadyArchived)
    ok = FakeClient()
    cli_frontend(ok).archive("C1")
    cli_frontend(ok).unarchive("C1")
    assert ok.calls == [("conversations_archive", {"channel": "C1"}),
                        ("conversations_unarchive", {"channel": "C1"})]


def test_archived_map_paginates():
    pages = [{"ok": True, "channels": [{"id": "C1", "is_archived": False},
                                       {"id": "C2", "is_archived": True}],
              "response_metadata": {"next_cursor": "cur2"}},
             {"ok": True, "channels": [{"id": "C3"}], "response_metadata": {"next_cursor": ""}}]
    client = FakeClient({"conversations_list": pages})
    assert cli_frontend(client).archived_map() == {"C1": False, "C2": True, "C3": False}
    assert client.calls == [
        ("conversations_list", {"types": "private_channel", "exclude_archived": False,
                                "limit": 500, "cursor": None}),
        ("conversations_list", {"types": "private_channel", "exclude_archived": False,
                                "limit": 500, "cursor": "cur2"})]
    bad = cli_frontend(FakeClient(errors={"conversations_list": SlackError("missing_scope")}))
    assert bad.archived_map() == {}


def test_upload(tmp_path):
    p = tmp_path / "export.md"
    p.write_text("x")
    client = FakeClient({"files_upload_v2": {"ok": True, "file": {
        "id": "F9", "name": "context-export.md", "title": "Context Export",
        "mimetype": "text/markdown", "permalink": "https://x/files/F9"}}})
    ref = cli_frontend(client).upload("C_MAIN", p, thread_ts="1.1",
                                      filename="context-export.md", title="Context Export",
                                      comment=":package: here")
    assert client.calls == [("files_upload_v2", {
        "channel": "C_MAIN", "thread_ts": "1.1", "file": str(p),
        "filename": "context-export.md", "title": "Context Export",
        "initial_comment": ":package: here"})]
    assert ref == FileRef("context-export.md", "https://x/files/F9", "F9", "text/markdown",
                          "Context Export")


def test_download(tmp_path, monkeypatch):
    import requests
    # A sandboxed EA_DIR: resident_io writes the download directly.
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    got = {}

    def fake_get(url, headers=None, timeout=None):
        got.update(url=url, headers=headers, timeout=timeout)
        return SimpleNamespace(content=b"PDF", raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "get", fake_get)
    ref = FileRef("receipt 1/a.pdf", "https://files.slack.com/F1")
    path = cli_frontend().download(ref, tmp_path / "uploads")
    assert got == {"url": "https://files.slack.com/F1",
                   "headers": {"Authorization": "Bearer xoxb-test"}, "timeout": 30}
    assert re.search(r"/uploads/\d{8}-\d{6}_receipt_1_a\.pdf$", path)
    assert Path(path).read_bytes() == b"PDF"

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert cli_frontend().download(ref, tmp_path / "uploads") is None


def test_home_and_modal():
    client = FakeClient()
    fe = cli_frontend(client)
    assert fe.publish_home("U_OWNER", {"type": "home"})
    assert fe.open_modal("trig", {"type": "modal"})
    assert client.calls == [("views_publish", {"user_id": "U_OWNER", "view": {"type": "home"}}),
                            ("views_open", {"trigger_id": "trig", "view": {"type": "modal"}})]


# ---------------------------------------------------------------------------
# LLM-facing
# ---------------------------------------------------------------------------

PROMPT_KEYS = ("platform", "format_rule_main", "format_rule_reply",
               "confirm_location", "confirm_sentence", "history_note", "room_word",
               "continuation_header", "output_only", "output_only_resume",
               "format_rule_closing", "confirm_sentence_wrapped", "card_place")


def test_prompt_strings_keys():
    ps = cli_frontend().prompt_strings()
    assert set(ps) == set(PROMPT_KEYS)
    assert all(isinstance(v, str) and v.strip() for v in ps.values())
    ps["platform"] = "changed"
    assert SlackFrontend.PROMPT_STRINGS["platform"] == "Slack"


def test_prompt_strings_verbatim_in_bot():
    """Until the prompt sites interpolate these strings, each one must
    appear in the bot's source byte for byte."""
    if "CRITICAL FORMATTING: Your output is posted to Slack" not in BOT_SRC:
        pytest.skip("the bot's prompt sites interpolate the frontend strings now")
    for key in PROMPT_KEYS:
        if key in ("platform", "room_word"):
            continue
        assert SlackFrontend.PROMPT_STRINGS[key] in BOT_SRC, key


def test_every_prompt_key_the_bot_reads_exists():
    """The bot interpolates `_PS['<key>']`; each key it reads is one the
    Slack frontend (and so every frontend) defines."""
    used = set(re.findall(r"_PS\['([a-z_]+)'\]", BOT_SRC))
    assert used, "the bot reads no prompt strings"
    assert used <= set(PROMPT_KEYS), used - set(PROMPT_KEYS)


def test_llm_tool_grants():
    grants = cli_frontend().llm_tool_grants()
    names = grants.split(",")
    assert names == ["mcp__claude_ai_Slack__slack_read_channel",
                     "mcp__claude_ai_Slack__slack_read_thread",
                     "mcp__claude_ai_Slack__slack_read_user_profile"]
    assert "*" not in grants and all(n.strip() == n and n for n in names)
    # the bot appends these to its Gmail-only _MCP_TOOLS
    m = re.search(r"^_MCP_TOOLS = \((.*?)^\)", BOT_SRC, re.S | re.M)
    assert m and not any(n in m.group(1) for n in names)
    assert "FRONTEND.llm_tool_grants()" in BOT_SRC
