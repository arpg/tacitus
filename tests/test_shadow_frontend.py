"""ShadowFrontend: a primary front end mirrored into a shadow, and the
load_frontend wiring that builds it from the chat block."""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import chat  # noqa: E402
import ea_config  # noqa: E402
from chat import (  # noqa: E402
    ActionEvent,
    CardAction,
    CardRegistry,
    ChatError,
    ConfirmationCard,
    FileRef,
    InboundMessage,
    MessageRef,
    Status,
)
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.shadow_frontend import WEB_PREFIX, ShadowFrontend, _to_mrkdwn  # noqa: E402
from chat.text import mrkdwn_to_zulip  # noqa: E402

MIRRORS = ("mirror_channel", "mirror_post", "mirror_update", "mirror_card",
           "mirror_card_status", "mirror_reaction", "mirror_file")


class WebShadow(MemoryFrontend):
    """A MemoryFrontend with the web front end's mirror entry points: each
    records its call and keeps the row it would store, keyed (channel, id)."""

    name = "web"

    def __init__(self) -> None:
        super().__init__()
        self.inbound = None
        self.calls: list[tuple] = []
        self.channels: dict[str, dict] = {}
        self.descriptions: dict[str, str] = {}
        self.rows: dict[tuple[str, str], dict] = {}
        self.down: set[str] = set()
        self.started = False

    def start(self) -> None:
        if "start" in self.down:
            raise RuntimeError("the web server did not bind")
        self.started = True

    def _call(self, verb: str, *args) -> None:
        if verb in self.down:
            raise RuntimeError(f"{verb} is down")
        self.calls.append((verb, *args))

    def _row(self, ref: MessageRef) -> dict:
        return self.rows.setdefault((ref.channel, ref.id), {"reactions": set()})

    def mirror_channel(self, cid, name, kind, archived, *, description=None):
        self._call("mirror_channel", cid, name, kind, archived)
        self.channels[cid] = {"name": name, "kind": kind, "archived": archived}
        if description is not None:
            self.descriptions[cid] = description

    def mirror_move(self, old_channel, old_key, new_channel, new_key):
        self._call("mirror_move", old_channel, old_key, new_channel, new_key)

    def mirror_post(self, ref, text, author):
        self._call("mirror_post", ref, text, author)
        self._row(ref).update(kind="message", text=text, author=author,
                              thread_key=ref.thread_key)

    def mirror_update(self, ref, text):
        self._call("mirror_update", ref, text)
        self._row(ref)["text"] = text

    def mirror_card(self, ref, card):
        self._call("mirror_card", ref, card)
        self._row(ref).update(kind="card", card=card, author="bot", thread_key=ref.thread_key)

    def mirror_card_status(self, ref, text):
        self._call("mirror_card_status", ref, text)
        self._row(ref).update(kind="status", text=text)

    def mirror_reaction(self, channel, msg_id, status, on):
        self._call("mirror_reaction", channel, msg_id, status, on)
        got = self._row(MessageRef(channel, msg_id, msg_id))["reactions"]
        (got.add if on else got.discard)(status)

    def mirror_file(self, ref, file, author, *, path=None, text=""):
        self._call("mirror_file", ref, file, author)
        self._row(ref).update(kind="file", file=file, author=author, thread_key=ref.thread_key,
                              text=text, path=path, data=Path(path).read_bytes() if path else None)


class ContractShadow(WebShadow):
    """A shadow whose mirror_file has the contract's three parameters only."""

    def mirror_file(self, ref, file, author):
        self._call("mirror_file", ref, file, author)
        self._row(ref).update(kind="file", file=file, author=author, thread_key=ref.thread_key)


def _pair(tmp_path=None):
    primary = MemoryFrontend(CardRegistry(tmp_path / "cards.json") if tmp_path else None)
    shadow = WebShadow()
    fe = ShadowFrontend(primary, shadow)
    fe.start()
    return fe, primary, shadow


def _card(title="Send the draft?", value=None):
    value = {"draft_id": "d-1"} if value is None else value
    return ConfirmationCard(
        kind="send", title=title,
        actions=[CardAction("approve_send", "Send", value, "primary", "send"),
                 CardAction("cancel_send", "Cancel", value, "danger", "cancel")],
        block_id="b1", fallback_text=title)


# ── outbound: both sides hold every message under the primary's ids ──

def test_start_mirrors_the_channels_the_primary_names():
    fe, primary, shadow = _pair()
    assert shadow.started
    assert shadow.channels["C_MAIN"] == {"name": "ea", "kind": "main", "archived": False}
    assert shadow.channels["D_OWNER"]["kind"] == "dm"
    room = primary.create_private_room("ea-atlas")
    primary.archive(room.id)
    assert fe.mirror_channels() == 3
    assert shadow.channels[room.id] == {"name": "ea-atlas", "kind": "room", "archived": True}


def test_post_update_and_reactions_land_on_both_sides_with_the_same_ids():
    fe, primary, shadow = _pair()
    ref = fe.post("C_MAIN", "*Done.* See <https://example.org/a|the page>.")
    assert primary.posts[-1]["id"] == ref.id
    row = shadow.rows[("C_MAIN", ref.id)]
    assert row["text"] == "*Done.* See <https://example.org/a|the page>."    # as the core wrote it
    assert (row["author"], row["thread_key"]) == ("bot", ref.thread_key)

    reply = fe.post("C_MAIN", "in the thread", thread_ts=ref.thread_key)
    assert primary.posts[-1]["thread_ts"] == ref.thread_key
    assert shadow.rows[("C_MAIN", reply.id)]["thread_key"] == ref.thread_key

    assert fe.update(ref, "_edited_")
    assert primary.updates[-1] == {"ref": ref, "text": "_edited_"}
    assert shadow.rows[("C_MAIN", ref.id)]["text"] == "_edited_"

    assert fe.react("C_MAIN", ref.id, Status.EYES)
    assert primary.reactions[-1] == {"op": "add", "channel": "C_MAIN", "msg_id": ref.id,
                                     "name": "eyes"}
    assert shadow.rows[("C_MAIN", ref.id)]["reactions"] == {"eyes"}
    assert fe.unreact("C_MAIN", ref.id, Status.EYES)
    assert primary.reactions[-1]["op"] == "remove"
    assert shadow.rows[("C_MAIN", ref.id)]["reactions"] == set()
    assert ("mirror_reaction", "C_MAIN", ref.id, "eyes", True) in shadow.calls


def test_a_reaction_the_primary_refuses_stays_off_the_shadow(monkeypatch):
    fe, primary, shadow = _pair()
    ref = fe.post("C_MAIN", "hi")
    monkeypatch.setattr(primary, "react", lambda *a: False)
    assert fe.react("C_MAIN", ref.id, Status.DONE) is False
    assert shadow.rows[("C_MAIN", ref.id)]["reactions"] == set()


def test_cards_and_their_status_land_on_both_sides():
    fe, primary, shadow = _pair()
    top = fe.post("D_OWNER", "a draft is ready")
    card = _card()
    cref = fe.post_card("D_OWNER", card, thread_ts=top.thread_key)
    assert primary.cards[-1]["id"] == cref.id
    row = shadow.rows[("D_OWNER", cref.id)]
    assert row["card"] is card and row["thread_key"] == top.thread_key

    card2 = _card("Send the revised draft?")
    assert fe.update_card(cref, card2)
    assert primary.updates[-1] == {"ref": cref, "card": card2}
    assert shadow.rows[("D_OWNER", cref.id)]["card"] is card2

    assert fe.replace_card_with_status(cref, ":white_check_mark: *Sent*")
    assert primary.updates[-1] == {"ref": cref, "status": ":white_check_mark: *Sent*"}
    row = shadow.rows[("D_OWNER", cref.id)]
    assert row["kind"] == "status" and row["text"] == ":white_check_mark: *Sent*"


def test_an_upload_lands_on_both_sides(tmp_path):
    fe, primary, shadow = _pair()
    top = fe.post("C_MAIN", "the export")
    f = tmp_path / "export.md"
    f.write_text("notes")
    fref = fe.upload("C_MAIN", f, thread_ts=top.thread_key, filename="export.md",
                     title="Export", comment="*Here* it is")
    assert primary.uploads[-1]["file"] == fref
    row = shadow.rows[("C_MAIN", f"file:{fref.id}")]
    assert row["file"] == fref and row["author"] == "bot" and row["data"] == b"notes"
    assert row["text"] == "*Here* it is" and row["thread_key"] == top.thread_key
    assert [c[0] for c in shadow.calls].count("mirror_post") == 1      # the top post only

    bare = fe.upload("C_MAIN", f)
    row = shadow.rows[("C_MAIN", f"file:{bare.id}")]
    assert row["text"] == "" and row["thread_key"] == f"file:{bare.id}"


def test_a_shadow_without_file_options_gets_the_text_first_and_a_link(tmp_path):
    primary, shadow = MemoryFrontend(), ContractShadow()
    fe = ShadowFrontend(primary, shadow)
    fe.start()
    f = tmp_path / "export.md"
    f.write_text("notes")
    fref = fe.upload("C_MAIN", f, comment="the export")
    row = shadow.rows[("C_MAIN", f"file:{fref.id}")]
    assert row["text"] == "the export"
    assert row["file"] == dataclasses.replace(fref, id="") and row["file"].url
    assert [c[0] for c in shadow.calls][-2:] == ["mirror_post", "mirror_file"]


def test_rooms_act_on_the_primary_and_mirror_the_row():
    fe, primary, shadow = _pair()
    room = fe.create_private_room("ea-atlas", "the atlas project")
    assert primary.room_ops[-1]["op"] == "create"
    assert shadow.channels[room.id] == {"name": "ea-atlas", "kind": "room", "archived": False}
    fe.archive(room.id)
    assert primary.rooms[room.id].is_archived
    assert shadow.channels[room.id]["archived"] is True
    fe.unarchive(room.id)
    assert shadow.channels[room.id]["archived"] is False
    fe.set_description(room.id, "renamed purpose")
    assert primary.room_ops[-1]["op"] == "describe"
    assert shadow.channels[room.id]["name"] == "ea-atlas"


def test_a_post_to_a_channel_the_shadow_lacks_mirrors_its_row_first():
    fe, primary, shadow = _pair()
    room = primary.create_private_room("ea-late")
    fe.post(room.id, "first post")
    verbs = [c[0] for c in shadow.calls]
    assert verbs[-2:] == ["mirror_channel", "mirror_post"]
    assert shadow.channels[room.id]["name"] == "ea-late"


# ── attributes and reads come from the primary ──

def test_attributes_and_reads_are_the_primarys(tmp_path):
    fe, primary, shadow = _pair()
    assert fe.name == "memory+web"
    assert fe.registry is primary.registry
    assert (fe.owner_id, fe.bot_user_id, fe.main_channel) == ("U_OWNER", "U_BOT", "C_MAIN")
    assert fe.limits == primary.limits and fe.supports("codes")
    assert fe.dm_channel("U_OWNER") == "D_OWNER" and fe.is_dm("D_OWNER")
    assert fe.prompt_strings() == primary.prompt_strings()
    assert fe.permalink("C_MAIN", "m1") == "memory://C_MAIN/m1"
    assert fe.channel_top("C_MAIN") is None
    assert fe.posts is primary.posts             # a platform's own attribute

    def namer(text, channel):
        return "title"
    fe.thread_namer = namer
    reg = CardRegistry(tmp_path / "cards.json")
    fe.card_registry = reg
    assert primary.thread_namer is namer and primary.card_registry is reg
    fe.run_forever()                             # both loops return at once here


# ── inbound from the primary ──

def test_an_owner_message_on_the_primary_is_mirrored_before_the_core_sees_it():
    fe, primary, shadow = _pair()
    seen = []

    @fe.registry.event("message")
    def on_message(msg):
        seen.append((msg, dict(shadow.rows.get((msg.channel, msg.msg_id)) or {})))

    msg = primary.inject_message(text="draft a reply to the committee")
    got, row_then = seen[-1]
    assert got is msg
    assert row_then["author"] == "owner" and row_then["text"] == msg.text
    assert row_then["thread_key"] == msg.msg_id

    primary.inject_message(text="the bot's own post", user_id="U_BOT")
    primary.inject_message(text="someone else", user_id="U_OTHER")
    primary.inject_message(channel="C_ELSEWHERE", text="a channel the bot does not serve")
    assert len(seen) == 4
    assert [r["text"] for r in shadow.rows.values() if r.get("author") == "owner"] == \
        [msg.text]


def test_an_owner_file_on_the_primary_is_mirrored():
    fe, primary, shadow = _pair()
    f = FileRef("scan.pdf", "memory://files/up1", "up1", "application/pdf")
    msg = primary.inject_message(channel="D_OWNER", text="", subtype="file_share", files=[f])
    row = shadow.rows[("D_OWNER", msg.msg_id)]
    # no bytes reach the shadow: it links to the primary's absolute url
    assert row["file"] == FileRef("scan.pdf", "memory://files/up1", "", "application/pdf")
    assert row["author"] == "owner" and row["data"] is None


# ── inbound from the shadow ──

def test_a_web_message_reaches_the_core_as_the_owner_under_the_primary_id():
    fe, primary, shadow = _pair()
    seen = []

    @fe.registry.event("message")
    def on_message(msg):
        seen.append(msg)

    ref = shadow.inbound("message", channel="C_MAIN", text="what is on today?",
                         thread_key=None, files=[])
    post = primary.posts[-1]
    assert post["text"] == "*You (web):* what is on today?"       # mrkdwn, as post() takes
    assert post["id"] == ref.id and post["thread_ts"] is None
    msg = seen[-1]
    assert (msg.user_id, msg.msg_id, msg.thread_key) == ("U_OWNER", ref.id, ref.thread_key)
    assert msg.text == "what is on today?"
    assert not (msg.is_self or msg.is_bot or msg.is_reply)
    row = shadow.rows[("C_MAIN", ref.id)]
    assert (row["author"], row["text"]) == ("owner", "what is on today?")
    assert sum(1 for c in shadow.calls if c[0] == "mirror_post" and c[1] == ref) == 1

    again = shadow.inbound("message", channel="C_MAIN", text="and tomorrow?",
                           thread_key=ref.thread_key, files=[])
    assert primary.posts[-1]["thread_ts"] == ref.thread_key
    assert seen[-1].is_reply and seen[-1].msg_id == again.id
    assert seen[-1].thread_key == ref.thread_key


def test_a_web_message_in_the_dm_is_a_dm(tmp_path):
    fe, primary, shadow = _pair()
    seen = []
    fe.registry.event("message")(seen.append)
    shadow.inbound("message", channel="D_OWNER", text="quick ask")
    assert seen[-1].is_dm


def test_a_web_file_goes_up_through_the_primary(tmp_path):
    fe, primary, shadow = _pair()
    seen = []
    fe.registry.event("message")(seen.append)
    stored = tmp_path / "f1"
    stored.write_bytes(b"%PDF-1.4")
    own = FileRef("scan.pdf", "/chat/api/files/f1", "f1", "application/pdf")
    ref = shadow.inbound("message", channel="D_OWNER", text="", thread_key=None,
                         files=[{"path": str(stored), "name": "scan.pdf", "file": own}])
    assert mrkdwn_to_zulip(primary.posts[-1]["text"]) == WEB_PREFIX + "scan.pdf"
    up = primary.uploads[-1]
    assert (up["path"], up["filename"], up["thread_ts"]) == (str(stored), "scan.pdf",
                                                             ref.thread_key)
    msg = seen[-1]
    assert msg.files == [up["file"]] and msg.subtype == "file_share"
    assert msg.msg_id == ref.id and msg.user_id == "U_OWNER"
    row = shadow.rows[("D_OWNER", ref.id)]
    assert row["file"] == own and row["author"] == "owner"
    assert row["data"] is None                     # the shadow holds these bytes already


def test_a_web_file_given_as_a_fileref_is_read_through_the_shadow(tmp_path, monkeypatch):
    fe, primary, shadow = _pair()
    src = tmp_path / "kept.txt"
    src.write_text("hello")

    def download(ref, dest_dir):
        out = Path(dest_dir) / ref.name
        out.write_bytes(src.read_bytes())
        return str(out)
    monkeypatch.setattr(shadow, "download", download)
    own = FileRef("notes.txt", "/chat/api/files/f2", "f2")
    shadow.inbound("message", channel="C_MAIN", text="see the notes", files=[own])
    up = primary.uploads[-1]
    assert up["filename"] == "notes.txt"
    assert not Path(up["path"]).exists()           # the copy is removed after the upload


def test_a_web_click_reaches_the_action_handler_on_the_primary_card(tmp_path):
    fe, primary, shadow = _pair(tmp_path)
    card = _card(value={"draft_id": "d-7"})
    cref = fe.post_card("D_OWNER", card)
    fe.card_registry.add(cref, card, code=None)        # as the core records it
    seen = []

    @fe.registry.action("approve_send")
    def approve(ev):
        seen.append(ev)

    web_ref = MessageRef(cref.channel, cref.id, cref.thread_key)
    ev = ActionEvent(action_id="approve_send", user_id="owner", card=web_ref,
                     value={"draft_id": "forged"})
    assert shadow.inbound("action", event=ev) is True
    got = seen[-1]
    assert got.user_id == "U_OWNER"
    assert (got.card.channel, got.card.id) == (cref.channel, cref.id)
    assert got.value == {"draft_id": "d-7"}        # the registry's, not the request's
    assert got.raw["source"] == "web"

    assert shadow.inbound("action", event=ActionEvent(
        action_id="no_such_action", user_id="owner", card=web_ref)) is False


def test_the_web_frontends_own_inbound_shape_is_taken(tmp_path):
    fe, primary, shadow = _pair(tmp_path)
    seen, clicks = [], []
    fe.registry.event("message")(seen.append)
    fe.registry.action("approve_send")(clicks.append)
    top = fe.post("D_OWNER", "a draft is ready")
    web = InboundMessage(channel="D_OWNER", msg_id="", text="send it", user_id="owner",
                         posted_at=0.0, thread_key=top.thread_key, is_reply=True,
                         raw={"source": "web"})
    ref = shadow.inbound(web)
    assert isinstance(ref, MessageRef) and primary.posts[-1]["thread_ts"] == top.thread_key
    assert (seen[-1].user_id, seen[-1].msg_id) == ("U_OWNER", ref.id)

    stored = tmp_path / "incoming-1"
    stored.write_bytes(b"bytes")
    up = InboundMessage(channel="D_OWNER", msg_id="", text="", user_id="owner",
                        posted_at=0.0, subtype="file_share", files=[FileRef("plan.pdf")],
                        raw={"source": "web", "path": str(stored)})
    ref = shadow.inbound(up)
    assert primary.uploads[-1]["filename"] == "plan.pdf"
    assert seen[-1].files == [primary.uploads[-1]["file"]]
    assert shadow.rows[("D_OWNER", ref.id)]["data"] == b"bytes"

    card = _card()
    cref = fe.post_card("D_OWNER", card)
    assert shadow.inbound(ActionEvent(action_id="approve_send", user_id="owner",
                                      card=MessageRef(cref.channel, cref.id, cref.thread_key),
                                      value={"draft_id": "d-1"})) is True
    assert clicks[-1].user_id == "U_OWNER"


def test_the_owners_markdown_reaches_the_primary_as_typed():
    for text in ("**bold** and *italic* and ~~gone~~", "see [the page](https://example.org/a_b)",
                 "keep `*code*` and\n```\n**fence**\n```", "snake_case_name * 2", "plain"):
        assert mrkdwn_to_zulip(_to_mrkdwn(text)) == text, text
    assert _to_mrkdwn(WEB_PREFIX + "*now*") == "*You (web):* _now_"


def test_an_unknown_inbound_kind_raises():
    fe, primary, shadow = _pair()
    with pytest.raises(ValueError):
        shadow.inbound("typing", channel="C_MAIN")
    with pytest.raises(ChatError):
        shadow.inbound("message", channel="C_MAIN", text="  ")


# ── a shadow failure never reaches the core ──

def test_a_shadow_failure_is_logged_and_the_primary_result_returned(tmp_path, caplog):
    primary, shadow = MemoryFrontend(), WebShadow()
    shadow.down = {"start", *MIRRORS}
    fe = ShadowFrontend(primary, shadow)
    caplog.set_level(logging.WARNING, logger="chat.shadow")
    fe.start()
    ref = fe.post("C_MAIN", "hello")
    assert ref == primary.posts[-1]["ref"]
    assert fe.update(ref, "edited") is True
    assert fe.react("C_MAIN", ref.id, Status.EYES) is True
    assert fe.unreact("C_MAIN", ref.id, Status.EYES) is True
    cref = fe.post_card("C_MAIN", _card())
    assert cref.id == primary.cards[-1]["id"]
    assert fe.update_card(cref, _card()) is True
    assert fe.replace_card_with_status(cref, "Sent") is True
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert fe.upload("C_MAIN", f) == primary.uploads[-1]["file"]
    room = fe.create_private_room("ea-x")
    fe.archive(room.id)
    seen = []
    fe.registry.event("message")(seen.append)
    primary.inject_message(text="still handled")
    assert seen and seen[-1].text == "still handled"
    shadow.inbound("message", channel="C_MAIN", text="from the web")
    assert seen[-1].text == "from the web"
    assert shadow.rows == {} and shadow.channels == {}
    assert "not mirrored" in caplog.text and "did not start" in caplog.text


# ── load_frontend and the chat block ──

def _ea_dir(tmp_path, block):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": block}))
    return tmp_path


def test_get_chat_defaults_the_shadow_to_none(tmp_path):
    assert ea_config.get_chat(tmp_path / "none.json")["shadow"] == ""


def test_load_frontend_wraps_the_primary_when_a_shadow_is_named(tmp_path, monkeypatch):
    built = []

    def build(cfg, **kw):
        built.append((cfg, kw))
        return WebShadow()
    monkeypatch.setattr(chat, "_build_web", build)
    ea_dir = _ea_dir(tmp_path, {"frontend": "memory", "shadow": "web",
                                "web": {"port": 8471}})
    fe = chat.load_frontend(ea_dir=ea_dir, env={})
    assert isinstance(fe, ShadowFrontend) and fe.name == "memory+web"
    assert isinstance(fe.primary, MemoryFrontend) and fe.shadow.inbound == fe._from_shadow
    web, kw = built[-1][0]["web"], built[-1][1]
    assert web["port"] == 8471 and kw == {"writer_only": False}
    assert web["store_path"] == str(tmp_path / "state" / "webchat.sqlite")
    # the core's wording stays the primary's
    assert chat.prompt_strings_for_config(ea_dir)["platform"] == fe.prompt_strings()["platform"]
    # a command-line sender that is not the owner's gets the primary alone
    # (tests/test_web_cli_senders.py has the owner's)
    assert type(chat.load_frontend(ea_dir=ea_dir, env={}, for_cli=True)) is MemoryFrontend


def test_an_unknown_shadow_is_refused(tmp_path):
    with pytest.raises(ChatError, match="unknown chat shadow 'irc'"):
        chat.load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "memory",
                                                     "shadow": "irc"}), env={})


def test_a_shadow_that_does_not_build_leaves_the_primary_running(tmp_path, monkeypatch, caplog):
    def broken(cfg, **kw):
        raise OSError("the store is not writable")
    monkeypatch.setattr(chat, "_build_web", broken)
    ea_dir = _ea_dir(tmp_path, {"frontend": "memory", "shadow": "web"})
    assert type(chat.load_frontend(ea_dir=ea_dir, env={})) is MemoryFrontend
    assert "did not build" in caplog.text


def test_the_owners_bot_keeps_the_web_files_in_the_private_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "priv"))
    cfg = chat._with_web_store({"frontend": "zulip", "shadow": "web", "web": {"port": 1}}, None)
    assert cfg["web"] == {"port": 1, "store_path": str(tmp_path / "priv" / "webchat.sqlite")}
    given = {"frontend": "zulip", "web": {"store_path": "/srv/given.sqlite"}}
    assert chat._with_web_store(given, None)["web"]["store_path"] == "/srv/given.sqlite"


# ── a Zulip primary (the fake client of test_zulip_frontend) ──

def test_a_zulip_primary_mirrors_the_owner_and_takes_web_input(tmp_path):
    zt = pytest.importorskip("test_zulip_frontend")
    primary, client = zt.make(tmp_path, start=False)
    shadow = WebShadow()
    fe = ShadowFrontend(primary, shadow)
    fe.start()
    assert fe.name == "zulip+web"
    main, dm = f"stream:{zt.EA}", f"stream:{zt.DM}"
    assert shadow.channels[main]["kind"] == "main" and shadow.channels[dm]["kind"] == "dm"
    seen = []
    fe.registry.event("message")(seen.append)
    client.messages = {**zt.OK, "messages": [], "found_oldest": True}

    primary.handle_event(zt.fx("event_stream_message"))      # the owner, in the EA stream
    msg = seen[-1]
    row = shadow.rows[(main, msg.msg_id)]
    assert (row["author"], row["text"], row["thread_key"]) == ("owner", msg.text, msg.thread_key)
    primary.handle_event(zt.fx("event_bot_self_message"))    # the bot's own echo
    assert len(seen) == 2 and (dm, seen[-1].msg_id) not in shadow.rows

    ref = shadow.inbound("message", channel=dm, text="what is *next*?", thread_key=None)
    sent = client.named("send_message")[-1]
    assert sent["content"] == WEB_PREFIX + "what is *next*?"
    got = seen[-1]
    assert (got.user_id, got.msg_id, got.thread_key) == (str(zt.OWNER), ref.id, ref.thread_key)
    assert got.is_dm and not got.is_self
    assert shadow.rows[(dm, ref.id)]["author"] == "owner"

    assert fe.react(dm, ref.id, Status.EYES)
    assert client.named("add_reaction")[-1]["message_id"] == int(ref.id)
    assert shadow.rows[(dm, ref.id)]["reactions"] == {"eyes"}


# ── the primary's own writes, renames and thread moves ──

def test_the_primarys_own_writes_reach_the_shadow_through_its_hook(caplog):
    fe, primary, shadow = _pair()
    assert primary.on_outbound == fe._primary_outbound
    ref = primary.post("C_MAIN", "*posted* on its own")
    primary._outbound("post", ref=ref, text="*posted* on its own")
    assert shadow.rows[("C_MAIN", ref.id)]["text"] == "*posted* on its own"
    primary._outbound("update", ref=ref, text="edited")
    assert shadow.rows[("C_MAIN", ref.id)]["text"] == "edited"
    primary._outbound("reaction", channel="C_MAIN", msg_id=ref.id, status=Status.DONE, on=True)
    assert shadow.rows[("C_MAIN", ref.id)]["reactions"] == {"white_check_mark"}

    card = _card()
    cref = primary.post_card("D_OWNER", card)
    primary._outbound("card", ref=cref, card=card)
    assert shadow.rows[("D_OWNER", cref.id)]["card"] is card
    primary._outbound("card_status", ref=cref, text="Sent")
    assert shadow.rows[("D_OWNER", cref.id)]["kind"] == "status"

    code = InboundMessage(channel="D_OWNER", msg_id="m90", text="send ABC123",
                          user_id="U_OWNER", posted_at=time.time(), thread_key=cref.thread_key)
    primary._outbound("owner_message", msg=code)
    row = shadow.rows[("D_OWNER", "m90")]
    assert (row["author"], row["text"], row["thread_key"]) == ("owner", "send ABC123",
                                                               cref.thread_key)
    primary._outbound("owner_message", msg=dataclasses.replace(code, msg_id="m91",
                                                              user_id="U_OTHER"))
    assert ("D_OWNER", "m91") not in shadow.rows

    room = primary.create_private_room("ea-elsewhere")
    primary._outbound("channel", channel=room.id, archived=True)
    assert room.id not in shadow.channels          # a room the shadow does not hold
    primary._outbound("channel", channel="C_MAIN", description="the EA channel")
    assert shadow.descriptions["C_MAIN"] == "the EA channel"
    primary._outbound("typing", channel="C_MAIN")  # a verb the composite does not know

    shadow.down = {"mirror_post"}
    caplog.set_level(logging.WARNING, logger="chat.shadow")
    primary._outbound("post", ref=ref, text="lost")
    assert "not mirrored" in caplog.text


def test_a_failing_outbound_hook_never_reaches_the_frontend(caplog):
    primary = MemoryFrontend()
    primary._outbound("post", text="no hook set")

    def boom(verb, **args):
        raise RuntimeError("the shadow is gone")
    primary.on_outbound = boom
    caplog.set_level(logging.WARNING, logger="chat")
    primary._outbound("post", text="x")
    assert "outbound hook failed on post" in caplog.text


def test_a_rename_on_the_primary_renames_the_shadow_row():
    fe, primary, shadow = _pair()
    seen = []
    fe.registry.event("message")(seen.append)
    primary.inject_message(channel="C_MAIN", user_id="", subtype="channel_rename",
                           new_name="ea-renamed")
    assert shadow.channels["C_MAIN"]["name"] == "ea-renamed"
    assert seen[-1].subtype == "channel_rename"
    primary.inject_message(channel="C_ELSEWHERE", user_id="", subtype="channel_rename",
                           new_name="not ours")
    assert "C_ELSEWHERE" not in shadow.channels and len(seen) == 2


def test_a_thread_move_rekeys_the_shadow_rows(tmp_path):
    primary, web = MemoryFrontend(), _web(tmp_path)
    fe = ShadowFrontend(primary, web)
    fe.mirror_channels()
    seen = []
    fe.registry.event("thread_moved")(seen.append)
    top = fe.post("C_MAIN", "a thread")
    reply = fe.post("C_MAIN", "a reply", thread_ts=top.thread_key)
    payload = {"type": "thread_moved", "old": top.thread_key, "new": "T_NEW",
               "channel": "C_MAIN", "old_channel": "C_MAIN", "message_ids": [],
               "whole": True, "user_id": "U_OWNER"}
    before = web.store.max_event_seq()
    assert primary.registry.dispatch_event("thread_moved", payload)
    assert seen == [payload]
    assert [m["id"] for m in web.store.thread("C_MAIN", "T_NEW")] == [top.id, reply.id]
    assert web.store.thread("C_MAIN", top.thread_key) == []
    assert [t for _, t, _ in web.store.events_since(before)] == ["update", "update"]
    # a move to a channel the shadow does not hold (a card filed away) leaves the rows
    primary.registry.dispatch_event("thread_moved", dict(payload, old="T_NEW", new="T_DONE",
                                                         channel="C_DONE"))
    assert len(web.store.thread("C_MAIN", "T_NEW")) == 2 and len(seen) == 2


def test_the_room_filter_picks_the_rooms_start_copies(caplog):
    primary, shadow = MemoryFrontend(), WebShadow()
    keep = primary.create_private_room("ea-atlas")
    other = primary.create_private_room("a-shared-stream")
    fe = ShadowFrontend(primary, shadow)
    fe.room_filter = lambda cid: cid == keep.id
    fe.start()
    assert keep.id in shadow.channels and other.id not in shadow.channels
    assert shadow.channels["C_MAIN"]["kind"] == "main"     # the fixed channels always go
    fe.post(other.id, "the core posts there")              # a room in use is copied then
    assert shadow.channels[other.id]["name"] == "a-shared-stream"

    def broken(cid):
        raise OSError("the room record is unreadable")
    primary2, shadow2 = MemoryFrontend(), WebShadow()
    room = primary2.create_private_room("ea-atlas")
    fe2 = ShadowFrontend(primary2, shadow2)
    fe2.room_filter = broken
    caplog.set_level(logging.WARNING, logger="chat.shadow")
    fe2.start()
    assert room.id not in shadow2.channels and "room filter failed" in caplog.text


def test_descriptions_reach_the_shadow():
    primary, shadow = MemoryFrontend(), WebShadow()
    old = primary.create_private_room("ea-old", "the old purpose")
    fe = ShadowFrontend(primary, shadow)
    fe.start()
    assert shadow.descriptions[old.id] == "the old purpose"     # read at start
    room = fe.create_private_room("ea-atlas", "the atlas project")
    assert shadow.descriptions[room.id] == "the atlas project"
    fe.set_description(room.id, "a new purpose")
    assert shadow.descriptions[room.id] == "a new purpose"
    fe.archive(room.id)
    assert shadow.channels[room.id]["archived"] is True
    assert shadow.descriptions[room.id] == "a new purpose"


# ── history: the copy a start makes ──

DAY = 86400.0


def _web(tmp_path):
    from chat.web_frontend import WebFrontend
    return WebFrontend({"port": 0}, store_path=tmp_path / "web" / "webchat.sqlite",
                       webui_dir=tmp_path / "webui")


def _said(primary, channel, mid, text, when, *, by="owner", thread=None, files=()):
    """A message in the primary's history, by the owner or by the bot."""
    bot = by == "bot"
    msg = InboundMessage(channel=channel, msg_id=mid, text=text,
                         user_id=primary.bot_user_id if bot else primary.owner_id,
                         posted_at=when, thread_key=thread or mid, is_reply=thread is not None,
                         is_self=bot, is_bot=bot, files=list(files))
    primary.history.append(msg)
    return msg


def _history(primary, now):
    """Two threads in the EA channel and one in the DM inside the window,
    and one thread older than the window."""
    _said(primary, "C_MAIN", "h1", "what is on *today*?", now - 2 * DAY)
    _said(primary, "C_MAIN", "h2", "Three meetings, the *first* at nine.", now - 2 * DAY + 60,
          by="bot", thread="h1")
    _said(primary, "C_MAIN", "h3", "thanks", now - 2 * DAY + 120, thread="h1")
    # a card, as the text the primary shows for it
    _said(primary, "C_MAIN", "h4", ":lock: *Send this reply?*\nTo: someone", now - DAY, by="bot")
    _said(primary, "D_OWNER", "h5", "quick ask", now - 3 * DAY)
    _said(primary, "D_OWNER", "h6", "Done.", now - 3 * DAY + 30, by="bot", thread="h5")
    _said(primary, "C_MAIN", "old1", "last month", now - 30 * DAY)


def _rows(web):
    return [(r["channel"], r["id"]) for r in web.store._all(
        "SELECT channel, id FROM messages ORDER BY seq")]


def _setup(tmp_path, **kw):
    primary, web = MemoryFrontend(), _web(tmp_path)
    fe = ShadowFrontend(primary, web, backfill_days=7, **kw)
    now = time.time()
    _history(primary, now)
    fe.mirror_channels()
    return fe, primary, web, now


def test_the_history_copy_takes_recent_threads_quietly(tmp_path):
    fe, primary, web, now = _setup(tmp_path)
    before = web.store.max_event_seq()
    report = fe.backfill()
    assert report == {"copied": 6, "channels": {"C_MAIN": 4, "D_OWNER": 2}, "failed": [],
                      "capped": False}
    assert fe.backfill_report is report
    thread = web.store.thread("C_MAIN", "h1")
    assert [(m["id"], m["author"]) for m in thread] == [("h1", "owner"), ("h2", "bot"),
                                                        ("h3", "owner")]
    assert thread[0]["text"] == "what is on *today*?"                    # as typed
    assert thread[1]["text"] == "Three meetings, the **first** at nine."  # as post() converts
    assert thread[0]["posted_at"] == pytest.approx(now - 2 * DAY)
    assert all(m["updated_at"] == m["posted_at"] for m in thread)        # never an edit
    card = web.store.message("C_MAIN", "h4")
    assert (card["kind"], card["card"], card["author"]) == ("message", None, "bot")
    assert web.store.message("D_OWNER", "h6")["thread_key"] == "h5"
    assert web.store.message("C_MAIN", "old1") is None                   # outside the window
    # one channel event per channel that took rows, none per row
    events = [(t, json.loads(p)["id"]) for _, t, p in web.store.events_since(before)]
    assert events == [("channel", "C_MAIN"), ("channel", "D_OWNER")]
    assert [c["unread"] for c in web.store.channels()] == [0, 0]
    assert web.store.channel("C_MAIN")["last_at"] == pytest.approx(now - DAY)
    fe.post("C_MAIN", "a new briefing")                                  # live traffic counts
    assert web.store.channel("C_MAIN")["unread"] == 1


def test_a_second_copy_adds_nothing_but_what_came_since(tmp_path):
    fe, primary, web, now = _setup(tmp_path)
    fe.backfill()
    rows, seq = _rows(web), web.store.max_event_seq()
    again = fe.backfill()
    assert again["copied"] == 0 and again["channels"] == {"C_MAIN": 0, "D_OWNER": 0}
    assert _rows(web) == rows and web.store.max_event_seq() == seq
    _said(primary, "C_MAIN", "h7", "one more", now - 60, thread="h1")    # while the bot was away
    assert fe.backfill()["copied"] == 1
    assert [m["id"] for m in web.store.thread("C_MAIN", "h1")] == ["h1", "h2", "h3", "h7"]


def test_the_copy_stops_at_its_cap(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="chat.shadow")
    fe, primary, web, now = _setup(tmp_path, backfill_cap=3)
    report = fe.backfill()
    assert report == {"copied": 3, "channels": {"C_MAIN": 3}, "failed": [], "capped": True}
    assert _rows(web) == [("C_MAIN", "h1"), ("C_MAIN", "h2"), ("C_MAIN", "h3")]
    assert "stopped at the cap of 3" in caplog.text


def test_a_read_the_primary_fails_ends_that_channels_copy(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chat.shadow")
    fe, primary, web, now = _setup(tmp_path)
    recent, replies = primary.recent_messages, primary.thread_replies

    def no_main(channel, since_s, limit=200, **kw):
        if channel == "C_MAIN":
            raise ChatError("the server is down")
        return recent(channel, since_s, limit, **kw)
    monkeypatch.setattr(primary, "recent_messages", no_main)
    report = fe.backfill()
    assert report["failed"] == ["C_MAIN"] and report["channels"] == {"C_MAIN": 0, "D_OWNER": 2}
    assert "history read failed" in caplog.text

    def no_card_thread(channel, ts, limit=200):
        if ts == "h4":
            raise ChatError("timeout")
        return replies(channel, ts, limit)
    monkeypatch.setattr(primary, "recent_messages", recent)
    monkeypatch.setattr(primary, "thread_replies", no_card_thread)
    report = fe.backfill()
    assert report["failed"] == ["C_MAIN"] and report["channels"] == {"C_MAIN": 3, "D_OWNER": 0}
    assert web.store.message("C_MAIN", "h4") is None
    assert "stopped on a read the primary failed" in caplog.text


def test_history_authors_and_files(tmp_path):
    primary, web = MemoryFrontend(), _web(tmp_path)
    fe = ShadowFrontend(primary, web, backfill_days=7)
    now = time.time()
    # the owner's web message, which the primary holds as the bot's
    _said(primary, "C_MAIN", "w1", _to_mrkdwn(WEB_PREFIX + "typed on the web"), now - 100,
          by="bot")
    primary.history.append(InboundMessage(channel="C_MAIN", msg_id="x1", text="someone else",
                                          user_id="U_OTHER", posted_at=now - 90,
                                          thread_key="x1"))
    scan = FileRef("scan.pdf", "memory://files/up1", "up1", "application/pdf")
    _said(primary, "C_MAIN", "f1", "the scan", now - 80, files=[scan])
    _said(primary, "C_MAIN", "j1", "joined", now - 70, by="bot").subtype = "other:channel_join"
    fe.mirror_channels()
    assert fe.backfill()["copied"] == 3
    web_row = web.store.message("C_MAIN", "w1")
    assert (web_row["author"], web_row["text"]) == ("owner", "typed on the web")
    assert web.store.message("C_MAIN", "x1")["author"] == "bot"
    row = web.store.message("C_MAIN", "f1")
    assert (row["kind"], row["author"], row["text"]) == ("file", "owner", "the scan")
    assert row["file"]["id"] == "" and row["file"]["url"] == "memory://files/up1"
    assert web.store.message("C_MAIN", "j1") is None


def test_history_from_a_markdown_primary_is_kept_as_written(tmp_path):
    primary, web = MemoryFrontend(), _web(tmp_path)
    primary.history_markdown = True          # as Zulip's history reads
    fe = ShadowFrontend(primary, web, backfill_days=7)
    now = time.time()
    _said(primary, "C_MAIN", "z1", "Three **meetings**, the *first* at nine.", now - 100,
          by="bot")
    _said(primary, "C_MAIN", "z2", WEB_PREFIX + "and *tomorrow*?", now - 50, by="bot",
          thread="z1")
    fe.mirror_channels()
    fe.backfill()
    assert web.store.message("C_MAIN", "z1")["text"] == "Three **meetings**, the *first* at nine."
    z2 = web.store.message("C_MAIN", "z2")
    assert (z2["author"], z2["text"]) == ("owner", "and *tomorrow*?")


def test_history_older_than_a_live_row_stays_out_of_its_thread(tmp_path):
    fe, primary, web, now = _setup(tmp_path)
    live = fe.post("C_MAIN", "a late reply", thread_ts="h1")      # mirrored before the copy
    fe.backfill()
    assert [m["id"] for m in web.store.thread("C_MAIN", "h1")] == [live.id]
    assert web.store.message("C_MAIN", "h4") is not None


def test_start_copies_on_a_thread_and_live_writes_wait_for_it(tmp_path, monkeypatch):
    fe, primary, web, now = _setup(tmp_path)
    gate = threading.Event()
    replies = primary.thread_replies

    def slow(channel, ts, limit=200):
        gate.wait(5)
        return replies(channel, ts, limit)
    monkeypatch.setattr(primary, "thread_replies", slow)
    try:
        fe.start()
        t = fe._backfill_thread
        assert t is not None and t.daemon and t.is_alive()     # the start did not wait
        live = fe.post("C_MAIN", "the bot wakes and answers", thread_ts="h1")
        assert web.store.message("C_MAIN", live.id) is None     # waits for the copy
        gate.set()
        t.join(5)
        assert not t.is_alive() and fe.backfill_report["copied"] == 6
        assert [m["id"] for m in web.store.thread("C_MAIN", "h1")] == ["h1", "h2", "h3", live.id]
        events = [(typ, json.loads(p).get("id")) for _, typ, p in web.store.events_since(0)]
        assert ("message", live.id) in events
        assert not {"h1", "h2", "h3", "h4", "h5", "h6"} & {i for _, i in events}
        later = fe.post("C_MAIN", "after the copy")
        assert web.store.message("C_MAIN", later.id) is not None
    finally:
        gate.set()
        fe.stop()


def test_live_writes_stop_waiting_when_the_hold_runs_out(tmp_path, monkeypatch):
    fe, primary, web, now = _setup(tmp_path)
    fe.backfill_hold_s = 0.0
    gate = threading.Event()
    replies = primary.thread_replies

    def slow(channel, ts, limit=200):
        gate.wait(5)
        return replies(channel, ts, limit)
    monkeypatch.setattr(primary, "thread_replies", slow)
    try:
        fe.start()
        live = fe.post("C_MAIN", "now")
        assert web.store.message("C_MAIN", live.id) is not None
    finally:
        gate.set()
        fe._backfill_thread.join(5)
        fe.stop()


def test_no_copy_without_days_or_without_a_quiet_shadow(tmp_path):
    fe, primary, shadow = _pair()                     # backfill_days 0: the default here
    assert fe._backfill_thread is None and fe.backfill_report is None
    primary, shadow = MemoryFrontend(), WebShadow()   # its mirror_post takes no `quiet`
    fe = ShadowFrontend(primary, shadow, backfill_days=7)
    fe.start()
    assert fe._backfill_thread is None and fe.backfill_report is None


def test_load_frontend_passes_the_days_of_history(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "_build_web", lambda cfg: WebShadow())
    for block, days in (({}, 7), ({"backfill_days": 2}, 2.0), ({"backfill_days": 0}, 0.0),
                        ({"backfill_days": "soon"}, 7)):
        ea_dir = tmp_path / f"d{len(list(tmp_path.iterdir()))}"
        fe = chat.load_frontend(ea_dir=_ea_dir(ea_dir, {"frontend": "memory", "shadow": "web",
                                                        "web": block}), env={})
        assert fe.backfill_days == days, block
