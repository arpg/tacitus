"""Tests for chat cards, the card registry, the resolvers, the handler
registry and the in-memory frontend."""
from __future__ import annotations

import json
import re
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat.base import HandlerRegistry, RoomNameTaken, AlreadyArchived  # noqa: E402
from chat.cards import (  # noqa: E402
    CardAction,
    CardRegistry,
    ConfirmationCard,
    action_reactions,
    cancel_action,
    parse_code,
    primary_action,
    resolve_code,
    resolve_reaction,
)
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.types import CODE_ALPHABET, ActionEvent, InboundMessage, MessageRef, ViewEvent  # noqa: E402

OWNER = "U_OWNER"


def _card(kind="send"):
    return ConfirmationCard(kind=kind, title="Send this draft?", actions=[
        CardAction("edit_draft", "Edit", {"draft_id": "d1"}, word="edit"),
        CardAction("approve_send", "Send", {"draft_id": "d1", "n": 1}, style="primary",
                   word="send"),
        CardAction("cancel_send", "Cancel", "d1", style="danger", word="cancel"),
        CardAction("send_all", "Send all", {"all": True}, word="send all"),
    ])


def _registry(tmp_path):
    return CardRegistry(tmp_path / "priv" / "chat-cards.json")


def _ref(mid="m1", channel="D_OWNER", thread="1700000000.000100", at=None):
    return MessageRef(channel, mid, thread, time.time() if at is None else at)


# cards


def test_primary_and_cancel_action():
    card = _card()
    assert primary_action(card).id == "approve_send"
    assert cancel_action(card).id == "cancel_send"
    plain = ConfirmationCard("room", "Pick", actions=[
        CardAction("a", "A", "1"), CardAction("b", "B", "2", word="cancel")])
    assert primary_action(plain).id == "a"
    assert cancel_action(plain).id == "b"
    assert cancel_action(ConfirmationCard("x", "t", actions=[CardAction("a", "A", "1")])) is None
    assert primary_action(ConfirmationCard("x", "t")) is None


def _withheld_card():
    """A draft card that withheld its Send: Edit comes first."""
    return ConfirmationCard(kind="send", title="Send this draft?", no_default=True, actions=[
        CardAction("edit_draft", ":pencil2: Edit", "d1", word="edit"),
        CardAction("cancel_send", "Cancel", "d1", style="danger", word="cancel"),
        CardAction("draft_handled", ":white_check_mark: Handled", "d1", word="handled"),
    ])


def test_a_card_with_no_default_gives_a_thumbs_up_to_nothing(tmp_path):
    """A thumbs up means Send. On a card that withheld its Send it must not
    pick the first action left (Edit): no chip, no reaction, no bare code."""
    card = _withheld_card()
    assert primary_action(card) is None and cancel_action(card).id == "cancel_send"
    assert action_reactions(card) == [("pencil2", "edit_draft"), ("-1", "cancel_send"),
                                      ("white_check_mark", "draft_handled")]
    reg = _registry(tmp_path)
    row = reg.add(_ref(), card, code="K7QX")
    assert row["no_default"] is True
    for name in ("+1", "thumbsup", "check"):
        assert resolve_reaction(row, name, OWNER, OWNER) is None, name
    assert resolve_reaction(row, "-1", OWNER, OWNER).action_id == "cancel_send"
    thread = row["card_thread"]
    assert resolve_code("K7QX", reg, OWNER, OWNER, thread_key=thread) is None
    assert resolve_code("edit K7QX", reg, OWNER, OWNER, thread_key=thread).action_id == "edit_draft"
    # without the flag the first action stays the default (a picker of rooms)
    plain = reg.add(_ref("m2"), ConfirmationCard("room", "Pick", actions=[
        CardAction("a", "A", "1"), CardAction("b", "B", "2", word="cancel")]), code="P2RT")
    assert resolve_reaction(plain, "+1", OWNER, OWNER).action_id == "a"


# registry


def test_registry_dir_and_file_modes(tmp_path):
    reg = _registry(tmp_path)
    assert stat.S_IMODE((tmp_path / "priv").stat().st_mode) == 0o700
    reg.add(_ref(), _card(), code="ABCD")
    assert stat.S_IMODE(reg.path.stat().st_mode) == 0o600
    assert [p.name for p in (tmp_path / "priv").iterdir()] == ["chat-cards.json"]


def test_registry_reasserts_dir_mode(tmp_path):
    (tmp_path / "priv").mkdir(mode=0o755)
    (tmp_path / "priv").chmod(0o755)
    _registry(tmp_path)
    assert stat.S_IMODE((tmp_path / "priv").stat().st_mode) == 0o700


def test_registry_add_lookup_retire(tmp_path):
    reg = _registry(tmp_path)
    ref = _ref(at=1234.5)
    row = reg.add(ref, _card(), code="abcd")
    assert row["code"] == "ABCD" and row["status"] == "live"
    assert row["actions"]["approve_send"] == {"value": {"draft_id": "d1", "n": 1},
                                              "word": "send", "style": "primary",
                                              "label": "Send"}
    assert reg.by_ref("D_OWNER", "m1")["kind"] == "send"
    assert reg.by_ref("D_OWNER", "nope") is None
    assert reg.by_code("abcd", thread_key="1700000000.000100")["msg_id"] == "m1"
    assert reg.by_code("ABCD", thread_key="1700000000.000100")["msg_id"] == "m1"
    assert reg.by_ref("D_OWNER", "m1")["card_thread"] == "1700000000.000100"
    assert reg.posted_at(ref) == 1234.5
    assert reg.posted_at(_ref("m9")) is None
    # a second registry on the same file sees the row
    assert CardRegistry(reg.path).by_code("ABCD", thread_key="1700000000.000100") is not None
    assert reg.retire(ref)
    assert reg.by_code("ABCD", thread_key="1700000000.000100") is None
    assert reg.by_ref("D_OWNER", "m1")["status"] == "retired"
    assert not reg.retire(_ref("m9"))


def test_registry_no_code_row(tmp_path):
    reg = _registry(tmp_path)
    reg.add(_ref(), _card(), code=None)
    assert reg.by_ref("D_OWNER", "m1")["code"] is None
    assert reg.by_code("", thread_key="1700000000.000100") is None


def test_registry_sweep(tmp_path):
    reg = _registry(tmp_path)
    reg.add(_ref("old"), _card(), code="AAAA")
    reg.add(_ref("new"), _card(), code="BBBB")
    reg._rows["D_OWNER|old"]["created"] = time.time() - 8 * 86400
    assert reg.sweep() == 1
    assert reg.by_ref("D_OWNER", "old") is None
    assert reg.by_ref("D_OWNER", "new") is not None
    assert "D_OWNER|old" not in json.loads(reg.path.read_text())
    assert reg.sweep() == 0


def test_new_code_alphabet_and_unique(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    for _ in range(200):
        code = reg.new_code()
        assert len(code) == 4 and set(code) <= set(CODE_ALPHABET)
    reg.add(_ref(), _card(), code="AAAA")
    import chat.cards as cards_mod
    seq = iter("AAAA" + "AAAA" + "BBBB")
    monkeypatch.setattr(cards_mod.secrets, "choice", lambda alphabet: next(seq))
    assert reg.new_code() == "BBBB"


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "priv" / "chat-cards.json"
    path.parent.mkdir(mode=0o700)
    path.write_text("{not json")
    assert CardRegistry(path).by_code("ABCD", thread_key="1700000000.000100") is None


# resolvers


def test_resolve_reaction(tmp_path):
    reg = _registry(tmp_path)
    ref = _ref(at=99.0)
    row = reg.add(ref, _card(), code="ABCD")
    ev = resolve_reaction(row, "white_check_mark", OWNER, OWNER)
    assert ev.action_id == "approve_send"
    assert ev.value == {"draft_id": "d1", "n": 1}
    assert ev.source == "reaction"
    assert ev.card == MessageRef("D_OWNER", "m1", "m1", 99.0)
    assert resolve_reaction(row, "check", OWNER, OWNER).action_id == "approve_send"
    assert resolve_reaction(row, "+1", OWNER, OWNER).action_id == "approve_send"
    assert resolve_reaction(row, "cross_mark", OWNER, OWNER).action_id == "cancel_send"
    assert resolve_reaction(row, "x", OWNER, OWNER).value == "d1"
    assert resolve_reaction(row, "check", "U_OTHER", OWNER) is None
    assert resolve_reaction(row, "check", "", "") is None
    assert resolve_reaction(row, "tada", OWNER, OWNER) is None
    assert resolve_reaction(None, "check", OWNER, OWNER) is None
    reg.retire(ref)
    assert resolve_reaction(reg.by_ref("D_OWNER", "m1"), "check", OWNER, OWNER) is None


def test_resolve_reaction_thread_key(tmp_path):
    reg = _registry(tmp_path)
    card = _card()
    card.thread_ts = "1700000000.000100"
    row = reg.add(_ref(), card, code=None)
    assert resolve_reaction(row, "check", OWNER, OWNER).card.thread_key == "1700000000.000100"


@pytest.mark.parametrize("text,want", [
    ("ABCD", (None, "ABCD")),
    ("abcd", (None, "ABCD")),
    ("  cancel abcd. ", ("cancel", "ABCD")),
    ("Send  All   ABCD!", ("send all", "ABCD")),
    ("send ABC2Z9", ("send", "ABC2Z9")),
    ("ABC", None),
    ("ABCDEFG", None),
    ("OOOO", None),
    ("I11I", None),
    ("please send the draft now", None),
    ("", None),
])
def test_parse_code(text, want):
    assert parse_code(text) == want


def test_resolve_code(tmp_path):
    reg = _registry(tmp_path)
    reg.add(_ref(), _card(), code="ABCD")
    ev = resolve_code("abcd", reg, OWNER, OWNER, thread_key="1700000000.000100")
    assert ev.action_id == "approve_send" and ev.source == "code"
    assert ev.value == {"draft_id": "d1", "n": 1}
    assert resolve_code("cancel ABCD", reg, OWNER, OWNER, thread_key="1700000000.000100").action_id == "cancel_send"
    assert resolve_code("send all abcd", reg, OWNER, OWNER, thread_key="1700000000.000100").action_id == "send_all"
    assert resolve_code("frobnicate ABCD", reg, OWNER, OWNER, thread_key="1700000000.000100") is None
    assert resolve_code("ABCD", reg, "U_OTHER", OWNER, thread_key="1700000000.000100") is None
    assert resolve_code("WXYZ", reg, OWNER, OWNER, thread_key="1700000000.000100") is None
    assert resolve_code("hello there", reg, OWNER, OWNER, thread_key="1700000000.000100") is None
    reg.retire(_ref())
    assert resolve_code("ABCD", reg, OWNER, OWNER, thread_key="1700000000.000100") is None


def test_a_code_counts_only_in_its_cards_thread(tmp_path):
    """A code typed in another thread (another card's topic, the EA
    channel, a project room) is an ordinary message: it confirms nothing,
    so text the model gets the owner to type elsewhere cannot fire a card."""
    reg = _registry(tmp_path)
    reg.add(_ref(thread="stream:9:topic:lock%20ABCD%20x"), _card(), code="ABCD")
    here = "stream:9:topic:lock%20ABCD%20x"
    assert resolve_code("ABCD", reg, OWNER, OWNER, thread_key=here).action_id == "approve_send"
    for elsewhere in ("stream:9:topic:other", "stream:3:topic:lock%20ABCD%20x", "dm:7", "", None):
        assert resolve_code("ABCD", reg, OWNER, OWNER, thread_key=elsewhere) is None, elsewhere
        assert reg.by_code("ABCD", thread_key=elsewhere) is None
    # a row from before card_thread was recorded resolves by no code
    reg._rows["D_OWNER|m1"].pop("card_thread")
    assert resolve_code("ABCD", reg, OWNER, OWNER, thread_key=here) is None
    # a moved topic takes its card's code along
    reg.add(_ref("m2", thread="stream:9:topic:a"), _card(), code="WXYZ")
    assert reg.move_thread("stream:9:topic:a", "stream:9:topic:b") >= 1
    assert reg.by_code("WXYZ", thread_key="stream:9:topic:b") is not None
    assert reg.by_code("WXYZ", thread_key="stream:9:topic:a") is None


def test_the_bots_own_reaction_never_resolves(tmp_path):
    reg = _registry(tmp_path)
    row = reg.add(_ref(), _card(), code="ABCD")
    assert resolve_reaction(row, "check", OWNER, OWNER, self_id="U_BOT").action_id
    assert resolve_reaction(row, "check", "U_BOT", "U_BOT", self_id="U_BOT") is None


# handler registry


def test_action_dispatch_exact_then_pattern():
    reg = HandlerRegistry()
    seen = []

    @reg.action("approve_send")
    def exact(ev):
        seen.append(("exact", ev.action_id))

    @reg.action(re.compile(r"approve_.*"))
    def pat(ev):
        seen.append(("pattern", ev.action_id))

    card = _ref()
    assert reg.dispatch_action(ActionEvent("approve_send", OWNER, card))
    assert reg.dispatch_action(ActionEvent("approve_event", OWNER, card))
    assert not reg.dispatch_action(ActionEvent("x_approve_event", OWNER, card))
    assert seen == [("exact", "approve_send"), ("pattern", "approve_event")]
    exact(ActionEvent("direct", OWNER, card))
    assert seen[-1] == ("exact", "direct")


def test_message_view_event_dispatch():
    reg = HandlerRegistry()
    seen = []

    @reg.event("message")
    def on_msg(msg):
        seen.append(("msg", msg.text))

    @reg.event("app_home_opened")
    def on_home(payload):
        seen.append(("home", payload))

    @reg.event(re.compile(r"member_.*"))
    def on_member(payload):
        seen.append(("member", payload))

    @reg.view("edit_draft_modal")
    def on_view(ev):
        seen.append(("view", ev.callback_id))

    msg = InboundMessage("C_MAIN", "m1", "hi", OWNER, 1.0)
    assert reg.dispatch_message(msg)
    assert not reg.dispatch_event("message", msg)
    assert reg.dispatch_event("app_home_opened", {"u": 1})
    assert reg.dispatch_event("member_joined_channel", "j")
    assert not reg.dispatch_event("reaction_added", {})
    assert reg.dispatch_view(ViewEvent("edit_draft_modal", OWNER, "", {}))
    assert not reg.dispatch_view(ViewEvent("other", OWNER, "", {}))
    assert seen == [("msg", "hi"), ("home", {"u": 1}), ("member", "j"),
                    ("view", "edit_draft_modal")]
    assert not HandlerRegistry().dispatch_message(msg)


def test_legacy_bridge():
    reg = HandlerRegistry()
    bound = []
    reg.bind_legacy = lambda kind, key, fn: bound.append((kind, key, fn.__name__))

    @reg.action("old_style")
    def old_handler(ack, body, client):
        return "called"

    @reg.view("old_view")
    def old_view(ack, body, view):
        return None

    @reg.action("new_style")
    def new_handler(ev):
        return None

    assert bound == [("action", "old_style", "old_handler"),
                     ("view", "old_view", "old_view")]
    assert old_handler(None, None, None) == "called"
    assert not reg.dispatch_action(ActionEvent("old_style", OWNER, _ref()))
    assert reg.dispatch_action(ActionEvent("new_style", OWNER, _ref()))


def test_legacy_bridge_unset_records():
    reg = HandlerRegistry()
    got = []

    @reg.action("old_style")
    def old_handler(body=None, ack=None):
        got.append(body)

    assert reg.dispatch_action(ActionEvent("old_style", OWNER, _ref()))
    assert len(got) == 1


# memory frontend


def _memory(tmp_path):
    return MemoryFrontend(card_registry=_registry(tmp_path))


def test_memory_post_and_react(tmp_path):
    fe = _memory(tmp_path)
    ref = fe.post("C_MAIN", "hello")
    assert ref.id == "m1" and ref.thread_key == "m1" and ref.posted_at
    reply = fe.post("C_MAIN", "in thread", thread_ts="m1", unfurl=False)
    assert reply.id == "m2" and reply.thread_key == "m1"
    assert fe.posts[1]["unfurl"] is False
    assert fe.message_posted_at(ref) == ref.posted_at
    assert fe.permalink("C_MAIN", "m1") == "memory://C_MAIN/m1"
    from chat.types import Status
    fe.react("C_MAIN", "m1", Status.EYES)
    fe.unreact("C_MAIN", "m1", Status.EYES)
    fe.react("C_MAIN", "m1", "white_check_mark")
    assert [(r["op"], r["name"]) for r in fe.reactions] == [
        ("add", "eyes"), ("remove", "eyes"), ("add", "white_check_mark")]
    assert fe.dm_channel(OWNER) == "D_OWNER" and fe.is_dm("D_OWNER")
    assert not fe.is_dm("C_MAIN")
    assert fe.channel_info("C_MAIN").is_private
    assert fe.channel_info("D_OWNER").is_dm
    assert fe.channel_info("C_NOPE").exists is False
    assert fe.ping()
    fe.start()
    fe.run_forever()


def test_memory_inject_message_dispatches(tmp_path):
    fe = _memory(tmp_path)
    seen = []
    fe.registry.event("message")(seen.append)
    msg = fe.inject_message(text="usage", channel="D_OWNER")
    assert seen == [msg]
    assert msg.is_dm and msg.user_id == OWNER and not msg.is_self


def test_memory_card_click_reaction_code(tmp_path):
    fe = _memory(tmp_path)
    seen = []

    @fe.registry.action("approve_send")
    def approve(ev):
        seen.append((ev.source, ev.action_id, ev.value))

    @fe.registry.action("cancel_send")
    def cancel(ev):
        seen.append((ev.source, ev.action_id, ev.value))

    card = _card()
    ref = fe.post_card("D_OWNER", card, thread_ts="1700000000.000100")
    assert fe.cards[0]["card"] is card
    fe.card_registry.add(ref, card, code=fe.card_registry.new_code())
    code = fe.card_registry.by_ref("D_OWNER", ref.id)["code"]

    fe.click(ref, "approve_send", OWNER)
    fe.click(ref, "approve_send", OWNER, value={"override": 1})
    assert fe.react_confirm(ref, "check", "U_OTHER") is None
    assert fe.react_confirm(ref, "check", OWNER).action_id == "approve_send"
    assert fe.react_confirm(ref, "cross_mark", OWNER).action_id == "cancel_send"
    here = ref.thread_key
    assert fe.type_code(f"cancel {code.lower()}", OWNER, here).action_id == "cancel_send"
    assert fe.type_code(code, OWNER, here).source == "code"
    assert fe.type_code(code, "U_OTHER", here) is None
    assert fe.type_code(f"bogus {code}", OWNER, here) is None
    assert fe.type_code(code, OWNER, "elsewhere") is None
    value = {"draft_id": "d1", "n": 1}
    assert seen == [
        ("button", "approve_send", value),
        ("button", "approve_send", {"override": 1}),
        ("reaction", "approve_send", value),
        ("reaction", "cancel_send", "d1"),
        ("code", "cancel_send", "d1"),
        ("code", "approve_send", value),
    ]
    assert fe.replace_card_with_status(ref, "Sent")
    assert fe.updates[-1] == {"ref": ref, "status": "Sent"}


def test_memory_rooms(tmp_path):
    fe = _memory(tmp_path)
    room = fe.create_private_room("ea-demo", "Demo project")
    assert room.id == "C_1" and room.name == "ea-demo"
    with pytest.raises(RoomNameTaken):
        fe.create_private_room("ea-demo")
    fe.invite(room.id, OWNER)
    fe.set_description(room.id, "about")
    fe.archive(room.id)
    with pytest.raises(AlreadyArchived):
        fe.archive(room.id)
    assert fe.archived_map() == {"C_MAIN": False, "C_1": True}
    fe.unarchive(room.id)
    assert [op["op"] for op in fe.room_ops] == ["create", "invite", "describe",
                                                "archive", "unarchive"]
    assert fe.mention(room.id) == "<#C_1>"


def test_memory_history_and_files(tmp_path):
    fe = _memory(tmp_path)
    fe.history = [
        InboundMessage("C_MAIN", "t1", "top", OWNER, 100.0),
        InboundMessage("C_MAIN", "r1", "reply", OWNER, 101.0, thread_key="t1", is_reply=True),
        InboundMessage("C_MAIN", "t2", "later", OWNER, 200.0),
        InboundMessage("C_OTHER", "t3", "elsewhere", OWNER, 300.0),
    ]
    assert [m.msg_id for m in fe.thread_replies("C_MAIN", "t1")] == ["t1", "r1"]
    assert [m.msg_id for m in fe.recent_messages("C_MAIN", 150.0)] == ["t2"]
    f = fe.upload("C_MAIN", tmp_path / "a.pdf", thread_ts="t1", title="A")
    assert f.name == "a.pdf" and f.title == "A"
    assert fe.download(f, tmp_path) is None
    assert fe.llm_tool_grants() == ""
    keys = {"platform", "format_rule_main", "format_rule_reply",
            "confirm_location", "confirm_sentence", "history_note", "room_word",
            "continuation_header", "output_only", "output_only_resume",
            "format_rule_closing", "confirm_sentence_wrapped"}
    assert set(fe.prompt_strings()) == keys


def test_cross_reaction_never_picks_the_primary_on_a_delete_card(tmp_path):
    """The delete card styles Delete as danger and leaves Keep unstyled; a
    cross reaction must resolve to Keep, and a check reaction to Delete."""
    from chat.cards import CardAction, CardRegistry, ConfirmationCard, resolve_reaction
    from chat.types import MessageRef
    reg = CardRegistry(tmp_path / "cards.json")
    card = ConfirmationCard(kind="event_delete", title="t", actions=[
        CardAction("approve_event_delete", "Delete", {"a": 1}, style="danger", word="delete"),
        CardAction("cancel_event_delete", "Keep", {"a": 2}, style="", word="keep"),
    ])
    ref = MessageRef("D_OWNER", "m9", "m9", 1.0)
    row = reg.add(ref, card, code="ABCD")
    cross = resolve_reaction(row, "cross_mark", "U_OWNER", "U_OWNER")
    check = resolve_reaction(row, "check", "U_OWNER", "U_OWNER")
    assert cross is not None and cross.action_id == "cancel_event_delete"
    assert check is not None and check.action_id == "approve_event_delete"
    # a card with no cancel word and only a danger primary has no cancel action
    solo = ConfirmationCard(kind="x", title="t", actions=[
        CardAction("go", "Go", "v", style="danger", word="go")])
    row2 = reg.add(MessageRef("D_OWNER", "m10", "m10", 1.0), solo, code="EFGH")
    assert resolve_reaction(row2, "cross_mark", "U_OWNER", "U_OWNER") is None


def test_move_thread_repoints_rows_and_action_values(tmp_path):
    reg = _registry(tmp_path)
    old, new = "stream:7:topic:old", "stream:9:topic:new"
    card = ConfirmationCard(kind="send", title="Send?", thread_ts=old, ea_channel="stream:7",
                            actions=[CardAction("approve_send", "Send",
                                                {"draft_id": "d1", "thread_ts": old,
                                                 "ea_channel": "stream:7"}, style="primary"),
                                     CardAction("cancel_send", "Cancel", "d1", style="danger")])
    reg.add(_ref("m1"), card, code="AB12")
    reg.add(_ref("m2"), _card(), code="CD34")
    assert reg.move_thread(old, new, "stream:9") == 1
    row = CardRegistry(tmp_path / "priv" / "chat-cards.json").by_ref("D_OWNER", "m1")
    assert row["thread_ts"] == new and row["ea_channel"] == "stream:9"
    val = row["actions"]["approve_send"]["value"]
    assert val["thread_ts"] == new and val["ea_channel"] == "stream:9"
    assert row["actions"]["cancel_send"]["value"] == "d1"
    assert reg.by_ref("D_OWNER", "m2")["thread_ts"] == _card().thread_ts
    assert reg.move_thread("stream:7:topic:none", new) == 0
