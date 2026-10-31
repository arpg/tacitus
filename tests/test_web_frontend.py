"""Tests for the web chat frontend: the store and the ChatFrontend verbs,
the mirror entry points, and the HTTP API on an ephemeral loopback port."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat import load_frontend, prompt_strings_for_config  # noqa: E402
import chat as chat_pkg  # noqa: E402
from chat.base import AlreadyArchived, ChatError, NotArchived, RoomNameTaken  # noqa: E402
from chat.cards import CardAction, CardField, CardSelect, ConfirmationCard  # noqa: E402
from chat.memory_frontend import SLACK_LIKE_PROMPT_STRINGS  # noqa: E402
from chat.types import THREAD_KEY_RE, ActionEvent, FileRef, InboundMessage, MessageRef, Status  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from chat.web_frontend import WebFrontend, parse_multipart  # noqa: E402
import ea_config  # noqa: E402

LOGIN = "owner@example.test"
OWNER_H = {"Tailscale-User-Login": LOGIN}
PUBLIC_URL = "https://host.example.test:8444/chat"
PUBLIC_HOST = "host.example.test:8444"
PUBLIC_ORIGIN = "https://host.example.test:8444"


def as_tailscaled(f: WebFrontend) -> WebFrontend:
    """Take this test process's own connections as tailscaled's: the
    client's uid still comes from /proc/net/tcp, as on the host. Without
    /proc (macOS, where the header path is off) every loopback peer is
    taken as tailscaled's, so the routes are tested there too."""
    uid = os.geteuid()
    if wf.PROC_NET[0].exists():
        f.peer_is_tailscaled = lambda peer, local: wf.peer_uid(peer, local) == uid
    else:
        f.peer_is_tailscaled = lambda peer, local: True
    return f


def make(tmp_path, *, tailscaled: bool = True, **cfg) -> WebFrontend:
    """A frontend as the owner's bot builds it: a trusted chat block, a
    private store, and (unless `tailscaled` is False) this process's
    connections taken as tailscaled's."""
    base = {"port": 0, "tailscale_login": LOGIN, "public_url": PUBLIC_URL}
    base.update(cfg)
    f = WebFrontend.from_config(base, store_path=tmp_path / "priv" / "webchat.sqlite",
                                webui_dir=tmp_path / "webui", trusted_config=True)
    f.poll_s = f.pair_delay_s = 0.02
    return as_tailscaled(f) if tailscaled else f


def card(**kw) -> ConfirmationCard:
    args = dict(kind="send", title=":lock: Send this draft?",
                fields=[CardField("To", "someone@example.test")], body="Hello there.",
                actions=[CardAction("approve_send", "Send", {"token": "t-1"}, "primary", "send"),
                         CardAction("cancel_send", "Cancel", {"token": "t-1"}, "", "cancel")],
                block_id="blk")
    args.update(kw)
    return ConfirmationCard(**args)


@pytest.fixture
def fe(tmp_path):
    f = make(tmp_path)
    f.ping_s = 0.2
    f.start()
    yield f
    f.stop()


def call(f: WebFrontend, method: str, path: str, body=None, headers=None, raw=None):
    """A loopback request; a POST carries this server's loopback Origin
    unless `headers` names one (None leaves it out)."""
    data = json.dumps(body).encode() if body is not None else raw
    h = {"Content-Type": "application/json"} if body is not None else {}
    if method == "POST":
        h["Origin"] = f"http://127.0.0.1:{f.port}"
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    req = urllib.request.Request(f.url + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def owner(f, method, path, body=None, headers=None, raw=None):
    """A request as tailscale serve forwards the owner's: the login header,
    the public Host and, on a POST, the public Origin."""
    h = {**OWNER_H, "Host": PUBLIC_HOST}
    if method == "POST":
        h["Origin"] = PUBLIC_ORIGIN
    return call(f, method, path, body, {**h, **(headers or {})}, raw)


def catcher(f: WebFrontend):
    """Record every dispatched message and action; `ev` is set on each."""
    got: list = []
    ev = threading.Event()

    def on_msg(m):
        got.append(m)
        ev.set()

    def on_action(a):
        got.append(a)
        ev.set()
    f.registry.event("message")(on_msg)
    f.registry.action("approve_send")(on_action)
    f.registry.action("pick")(on_action)
    return got, ev


def multipart(fields: dict, boundary: str = "b0undary") -> tuple[bytes, str]:
    out = b""
    for name, val in fields.items():
        if isinstance(val, tuple):
            fname, ctype, data = val
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                    f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data
        else:
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{val}").encode()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


class Stream:
    """An open event stream read line by line."""

    def __init__(self, f: WebFrontend, query: str = "", headers=None) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", f.port, timeout=5)
        self.conn.request("GET", f"{f.base_path}/api/events{query}",
                          headers={**OWNER_H, **(headers or {})})
        self.resp = self.conn.getresponse()

    def lines(self, until):
        """Read lines until `until(lines)` is true; return them."""
        seen: list[str] = []
        while not until(seen):
            seen.append(self.resp.fp.readline().decode().rstrip("\n"))
        return seen

    def events(self, n: int) -> list[dict]:
        out, cur = [], {}
        while len(out) < n:
            line = self.resp.fp.readline().decode().rstrip("\n")
            if line == "":
                if "data" in cur:
                    out.append(cur)
                cur = {}
                continue
            key, _, val = line.partition(": ")
            if key in ("id", "event", "data"):
                cur[key] = val
        return out

    def close(self) -> None:
        self.conn.close()


# the store and the verbs


def test_post_opens_a_thread_and_a_reply_stays_in_it(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        ref = f.post("main", "first")
        assert (ref.channel, ref.id, ref.thread_key) == ("main", "w1", "t1")
        assert THREAD_KEY_RE.match(ref.thread_key) and THREAD_KEY_RE.match(ref.id)
        reply = f.post("main", "second", thread_ts=ref.thread_key)
        by_id = f.post("main", "third", thread_ts=ref.id)
        assert reply.thread_key == by_id.thread_key == ref.thread_key
        msgs = f.thread_replies("main", ref.thread_key)
        assert [m.text for m in msgs] == ["first", "second", "third"]
        assert [m.is_reply for m in msgs] == [False, True, True]
        assert msgs[0].reply_count == 2 and msgs[0].is_self and msgs[0].user_id == "bot"
        assert f.thread_replies("main", ref.id)[0].msg_id == ref.id
        assert f.message_posted_at(ref) == pytest.approx(ref.posted_at)
        assert ref.permalink.endswith(f"#/main/{ref.thread_key}/{ref.id}")
    finally:
        f.stop()


def test_recent_messages_are_thread_openers_after_a_time_or_an_id(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        a = f.post("main", "a")
        f.post("main", "a reply", thread_ts=a.thread_key)
        b = f.post("main", "b")
        f.post("dm", "elsewhere")
        assert [m.msg_id for m in f.recent_messages("main", 0)] == [a.id, b.id]
        assert [m.msg_id for m in f.recent_messages("main", 0, since_id=a.id)] == [b.id]
        assert f.recent_messages("main", b.posted_at + 1) == []
        assert all(not m.is_reply for m in f.recent_messages("main", 0))
    finally:
        f.stop()


def test_bot_text_is_converted_to_the_markdown_the_app_renders(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        ref = f.post("main", "*bold* and _it_ see <https://example.test/x|the page> :x:")
        row = f.store.message("main", ref.id)
        assert row["text"] == ("**bold** and *it* see [the page](https://example.test/x) :x:")
        assert f.update(ref, "now *done*")
        assert f.store.message("main", ref.id)["text"] == "now **done**"
        assert "[#ea](" in f.mention("main")
    finally:
        f.stop()


def test_updated_at_starts_at_posted_at_and_rises_on_a_change(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        f.mirror_channel("stream:7", "ea", "main")
        row = f.mirror_post(MessageRef("stream:7", "1", "stream:7:topic:x", 1000.0), "x")
        assert row["posted_at"] == row["updated_at"] == 1000.0
    finally:
        f.stop()
    f = make(tmp_path / "b")
    f.start()
    try:
        ref = f.post("main", "x")
        c = f.post_card("dm", card())
        assert f.store.message("main", ref.id)["updated_at"] == ref.posted_at
        f.react("main", ref.id, Status.EYES)
        assert f.store.message("main", ref.id)["updated_at"] == ref.posted_at
        f.update(ref, "y")
        f.update_card(c, card(title="t2"))
        f.replace_card_with_status(c, "done")
        assert f.store.message("main", ref.id)["updated_at"] > ref.posted_at
        assert f.store.message("dm", c.id)["updated_at"] > c.posted_at
    finally:
        f.stop()


def test_reactions_toggle(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        ref = f.post("main", "x")
        assert f.react("main", ref.id, Status.EYES)
        assert f.react("main", ref.id, "hourglass_flowing_sand")
        assert f.react("main", ref.id, Status.EYES)
        assert f.store.message("main", ref.id)["reactions"] == ["eyes", "hourglass_flowing_sand"]
        assert f.unreact("main", ref.id, Status.EYES)
        assert f.store.message("main", ref.id)["reactions"] == ["hourglass_flowing_sand"]
        assert not f.react("main", "w999", Status.DONE)
    finally:
        f.stop()


def test_cards_are_stored_as_json_and_settle_to_a_status(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        c = card(select=CardSelect("pick", "Pick one", [("A", "a"), ("B", "b")]))
        assert f.card_fits(card(body="x" * 100000))
        ref = f.post_card("dm", c)
        row = f.store.message("dm", ref.id)
        assert row["kind"] == "card" and row["card"]["title"] == c.title
        assert row["card"]["actions"][0] == {"id": "approve_send", "label": "Send",
                                             "value": {"token": "t-1"}, "style": "primary",
                                             "word": "send"}
        assert row["card"]["select"]["options"] == [["A", "a"], ["B", "b"]]
        assert f.update_card(ref, card(title="Changed"))
        assert f.store.message("dm", ref.id)["card"]["title"] == "Changed"
        assert f.replace_card_with_status(ref, ":white_check_mark: *Sent*")
        row = f.store.message("dm", ref.id)
        # the status line stays as the core wrote it: the app renders it as card text
        assert row["kind"] == "status" and row["text"] == ":white_check_mark: *Sent*"
        assert not f.update_card(MessageRef("dm", "w999", "t999"), c)
    finally:
        f.stop()


def test_rooms(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        room = f.create_private_room("ea-proj", "A project")
        assert room.id == "r1" and room.name == "ea-proj" and room.url.endswith("#/r1")
        with pytest.raises(RoomNameTaken):
            f.create_private_room("EA-proj")
        assert f.create_private_room("ea-other").id == "r2"
        info = f.channel_info("r1")
        assert info.is_private and info.is_member and not info.is_archived and not info.is_dm
        assert f.channel_info("dm").is_dm and f.is_dm("dm") and not f.is_dm("main")
        assert not f.channel_info("nope").exists
        assert f.channel_members("r1") == {"owner", "bot"} and f.channel_members("nope") is None
        f.set_description("r1", "new purpose")
        assert f.store.channel("r1")["description"] == "new purpose"
        f.archive("r1")
        with pytest.raises(AlreadyArchived):
            f.archive("r1")
        assert f.archived_map() == {"main": False, "r1": True, "r2": False}
        f.unarchive("r1")
        with pytest.raises(NotArchived):
            f.unarchive("r1")
        with pytest.raises(ChatError):
            f.archive("nope")
        with pytest.raises(ChatError):
            f.post("nope", "x")
        assert f.dm_channel("owner") == "dm"
    finally:
        f.stop()


def test_upload_and_download(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    f = make(tmp_path)
    f.start()
    try:
        src = tmp_path / "report.pdf"
        src.write_bytes(b"%PDF-1 \x00\xff")
        main = f.post("main", "here")
        ref = f.upload("main", src, thread_ts=main.thread_key, comment="the *report*")
        assert ref.id == "f1" and ref.name == "report.pdf" and ref.mimetype == "application/pdf"
        assert ref.url == "/chat/api/files/f1"
        row = f.thread_replies("main", main.thread_key)[-1]
        assert row.files[0].id == "f1" and row.subtype == "file_share"
        assert row.text == "the **report**"
        got = f.download(ref, tmp_path / "uploads")
        assert Path(got).read_bytes() == b"%PDF-1 \x00\xff" and got.endswith("_report.pdf")
        assert f.download(FileRef("x", url="/chat/api/files/f1"), tmp_path / "uploads")
        assert f.download(FileRef("x", id="f99"), tmp_path / "uploads") is None
    finally:
        f.stop()


def test_every_write_appends_an_event(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        start = f.store.max_event_seq()
        ref = f.post("main", "x")
        f.update(ref, "y")
        f.react("main", ref.id, Status.EYES)
        c = f.post_card("dm", card())
        f.replace_card_with_status(c, "done")
        f.create_private_room("ea-r")
        types = [t for _s, t, _p in f.store.events_since(start)]
        assert types == ["message", "update", "reaction", "card", "status", "channel"]
        assert set(types) <= set(wf.EVENT_TYPES)
    finally:
        f.stop()


def test_prompt_strings_and_attributes(tmp_path):
    f = make(tmp_path)
    ps = f.prompt_strings()
    assert set(ps) == set(SLACK_LIKE_PROMPT_STRINGS)
    assert ps["confirm_location"] == "the Send button is on the card in your DM thread"
    assert "web chat" in ps["platform"] and "Markdown" in ps["format_rule_main"]
    assert f.llm_tool_grants() == ""
    assert f.capabilities == {"buttons", "select", "files", "private_rooms", "reactions"}
    assert tuple(f.limits) == (20000, 20000, 200, 0)
    assert (f.owner_id, f.bot_user_id, f.main_channel, f.sched_channel) == \
        ("owner", "bot", "main", "")
    src = (ROOT / "botcore/botcore/chat/web_frontend.py").read_text()
    assert "\u2014" not in src and "\u2013" not in src


def test_from_config_needs_a_store(tmp_path):
    with pytest.raises(ChatError):
        WebFrontend.from_config({})
    f = WebFrontend.from_config({"store_path": str(tmp_path / "s.sqlite")})
    assert f.store_path == tmp_path / "s.sqlite" and f.port == 8470
    assert f.base_path == "/chat" and f.bind == "127.0.0.1"
    assert WebFrontend.from_config({"base_path": "/"}, store_path=tmp_path / "t").base_path == ""


def test_parse_multipart_keeps_binary_bytes():
    data = bytes(range(256)) + b"\r\n--b0und\r\n\n\r"
    body, ctype = multipart({"text": "hi", "file": ("a.bin", "application/octet-stream", data)})
    parts = parse_multipart(ctype, body)
    assert parts["file"]["data"] == data and parts["file"]["filename"] == "a.bin"
    assert parts["text"]["data"] == b"hi" and parts["text"]["filename"] is None
    with pytest.raises(ValueError):
        parse_multipart("application/json", b"{}")


# the mirror entry points (shadow mode)


def test_mirrors_store_under_the_primary_ids_and_are_idempotent(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        assert f.store.channels() == []
        f.mirror_channel("stream:7", "ea", "main")
        f.mirror_channel("stream:7", "ea", "main")
        ref = MessageRef("stream:7", "1001", "stream:7:topic:hello", 1000.0)
        start = f.store.max_event_seq()
        f.mirror_post(ref, "*hi*")
        f.mirror_post(ref, "*hi*")
        row = f.store.message("stream:7", "1001")
        assert row["text"] == "**hi**" and row["thread_key"] == "stream:7:topic:hello"
        assert row["posted_at"] == 1000.0 and row["author"] == "bot"
        f.mirror_update(ref, "*edited*")
        f.mirror_reaction("stream:7", "1001", Status.EYES, True)
        f.mirror_reaction("stream:7", "1001", Status.EYES, True)
        types = [t for _s, t, _p in f.store.events_since(start)]
        assert types == ["message", "update", "reaction"]
        cref = MessageRef("stream:8", "1002", "stream:8:topic:lock", 1001.0)
        f.mirror_card(cref, card())
        f.mirror_card(cref, card())
        f.mirror_card_status(cref, "Sent")
        assert f.store.message("stream:8", "1002")["kind"] == "status"
        assert f.store.channel("stream:8")["kind"] == "room"   # a placeholder row
        f.mirror_channel("stream:8", "ea-dm", "dm")
        assert f.store.channel("stream:8")["kind"] == "dm"
        f.mirror_post(MessageRef("stream:7", "1003", "stream:7:topic:hello"), "typed", "owner")
        assert f.store.message("stream:7", "1003")["author"] == "owner"
        assert [m["id"] for m in f.store.thread("stream:7", "stream:7:topic:hello")] == \
            ["1001", "1003"]
    finally:
        f.stop()


def test_mirror_file_keeps_the_bytes_when_given_a_path(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        src = tmp_path / "pic.png"
        src.write_bytes(b"\x89PNG data")
        ref = MessageRef("stream:7", "1100", "stream:7:topic:files")
        row = f.mirror_file(ref, FileRef("pic.png", url="https://chat.example.test/user_uploads/1/pic.png",
                                         id="/user_uploads/1/pic.png"), path=src)
        assert row["file"]["url"] == "/chat/api/files/%2Fuser_uploads%2F1%2Fpic.png"
        status, headers, body = owner(f, "GET", "/api/files/%2Fuser_uploads%2F1%2Fpic.png")
        assert status == 200 and body == b"\x89PNG data"
        ref2 = MessageRef("stream:7", "1101", "stream:7:topic:files")
        row2 = f.mirror_file(ref2, FileRef("b.txt", url="https://chat.example.test/u/b.txt",
                                           id="/u/b.txt"))
        # no bytes here: no id, and the primary's absolute url
        assert row2["file"]["id"] == "" and row2["file"]["url"] == "https://chat.example.test/u/b.txt"
    finally:
        f.stop()


def test_quiet_history_rows_have_no_event_and_are_never_unread(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("stream:7", "ea", "main")
    key = "stream:7:topic:plans"
    start = f.store.max_event_seq()
    row = f.mirror_post(MessageRef("stream:7", "10", key, 1000.0), "*the plan*", quiet=True)
    assert row["text"] == "**the plan**" and row["posted_at"] == row["updated_at"] == 1000.0
    f.mirror_post(MessageRef("stream:7", "11", key, 1010.0), "fine", "owner", quiet=True)
    f.mirror_post(MessageRef("stream:7", "12", key, 1020.0), "Done, *at nine*.",
                  quiet=True, markdown=True)
    assert f.store.message("stream:7", "12")["text"] == "Done, *at nine*."   # kept as written
    f.mirror_file(MessageRef("stream:7", "13", key, 1030.0),
                  FileRef("a.pdf", url="https://chat.example.test/u/a.pdf"), quiet=True,
                  text="the file")
    assert f.store.events_since(start) == []
    assert f.store.channel("stream:7")["unread"] == 0
    assert [m["id"] for m in f.store.thread("stream:7", key)] == ["10", "11", "12", "13"]
    # a row it holds is left as it is
    assert f.mirror_post(MessageRef("stream:7", "10", key, 1000.0), "changed", quiet=True) is None
    assert f.mirror_file(MessageRef("stream:7", "13", key, 1030.0), FileRef("b.pdf"),
                         quiet=True) is None
    assert f.store.message("stream:7", "10")["text"] == "**the plan**"
    # an older message never goes in after a newer one of its thread
    assert f.mirror_post(MessageRef("stream:7", "9", key, 990.0), "older", quiet=True) is None
    # a live post still counts, and a history row from the owner does not reset it
    f.mirror_post(MessageRef("stream:7", "20", key, 2000.0), "live")
    f.mirror_post(MessageRef("stream:7", "21", "stream:7:topic:other", 2100.0), "old ask",
                  "owner", quiet=True)
    assert f.store.channel("stream:7")["unread"] == 1


def test_mirror_backfilled_sends_one_channel_event(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("stream:7", "ea", "main", description="the EA stream")
    f.mirror_post(MessageRef("stream:7", "10", "k", 1000.0), "old", quiet=True)
    start = f.store.max_event_seq()
    row = f.mirror_backfilled("stream:7")
    assert row["last_at"] == 1000.0 and row["unread"] == 0
    assert [t for _s, t, _p in f.store.events_since(start)] == ["channel"]
    assert f.mirror_backfilled("stream:404") is None


def test_mirror_channel_keeps_a_description_not_given(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("stream:9", "ea-atlas", "room", description="the atlas project")
    f.mirror_channel("stream:9", "ea-atlas", "room", True)
    row = f.store.channel("stream:9")
    assert row["description"] == "the atlas project" and row["archived"] is True
    f.mirror_channel("stream:9", "ea-atlas", "room", True, description="")
    assert f.store.channel("stream:9")["description"] == ""


def test_mirror_move_rekeys_a_thread(tmp_path):
    f = make(tmp_path)
    old, new = "stream:7:topic:draft", "stream:7:topic:draft%20v2"
    for i in range(2):
        f.mirror_post(MessageRef("stream:7", f"3{i}", old, 1000.0 + i), f"m{i}")
    start = f.store.max_event_seq()
    assert f.mirror_move("stream:7", old, "stream:7", new) == 2
    assert [m["id"] for m in f.store.thread("stream:7", new)] == ["30", "31"]
    assert f.store.thread("stream:7", old) == []
    assert [t for _s, t, _p in f.store.events_since(start)] == ["update", "update"]
    # to another stream, where one of the messages is held already
    f.mirror_post(MessageRef("stream:8", "31", "stream:8:topic:x", 1001.0), "m1")
    assert f.mirror_move("stream:7", new, "stream:8", "stream:8:topic:x") == 1
    assert [m["id"] for m in f.store.thread("stream:8", "stream:8:topic:x")] == ["30", "31"]
    assert f.store.message("stream:7", "31") is None


def test_a_store_from_before_history_rows_takes_the_column(tmp_path):
    import sqlite3
    path = tmp_path / "old" / "webchat.sqlite"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, id TEXT, "
        "thread_key TEXT, author TEXT, kind TEXT, text TEXT, card TEXT, file TEXT, "
        "reactions TEXT, posted_at REAL, updated_at REAL, UNIQUE(channel, id));"
        "INSERT INTO messages(channel, id, thread_key, author, kind, text, reactions, posted_at, "
        "updated_at) VALUES ('main', '1', 't', 'bot', 'message', 'kept', '[]', 1.0, 1.0);")
    conn.commit()
    conn.close()
    store = wf.WebStore(path)
    store.upsert_channel("main", "ea", "main")
    assert store.message("main", "1")["text"] == "kept"
    assert store.channel("main")["unread"] == 1
    store.close()


# HTTP


def test_api_needs_the_owner_and_static_files_do_not(tmp_path):
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<!doctype html><title>t</title>")
    (tmp_path / "webui" / "app.js").write_text("console.log(1)")
    f = make(tmp_path)
    f.start()
    try:
        status, _h, body = call(f, "GET", "/api/me")
        assert status == 401 and json.loads(body) == {"error": "not the owner"}
        assert call(f, "GET", "/api/channels", headers={"Tailscale-User-Login": "x@example.test"})[0] == 401
        assert call(f, "GET", "/api/channels", headers={"Cookie": "ea_web=wrong"})[0] == 401
        status, _h, body = owner(f, "GET", "/api/me")
        assert status == 200 and json.loads(body)["owner"] == "owner"
        status, h, body = call(f, "GET", "/")
        assert status == 200 and b"<title>t</title>" in body
        csp = h["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "style-src 'self';" in csp and "unsafe" not in csp
        assert "form-action 'none'" in csp and h["Cross-Origin-Resource-Policy"] == "same-origin"
        status, h, _b = call(f, "GET", "/app.js")
        assert status == 200 and h["Content-Type"].startswith("text/javascript")
        conn = http.client.HTTPConnection("127.0.0.1", f.port, timeout=5)
        conn.request("GET", "/chat")
        resp = conn.getresponse()
        assert resp.status == 301 and resp.getheader("Location") == "/chat/"
        conn.close()
        (tmp_path / "webui" / "sw.js").write_text("self.addEventListener('fetch', () => {})")
        (tmp_path / "webui" / "manifest.webmanifest").write_text("{}")
        assert call(f, "GET", "/sw.js")[1]["Content-Type"].startswith("text/javascript")
        assert call(f, "GET", "/manifest.webmanifest")[1]["Content-Type"] == \
            "application/manifest+json"
        assert call(f, "GET", "/some/route")[0] == 200          # an app route
        assert call(f, "GET", "/missing.css")[0] == 404
        assert call(f, "GET", "/..%2Fpriv%2Fwebchat.sqlite")[0] == 404
        assert call(f, "GET", "/.hidden")[0] == 404
        req = urllib.request.Request(f.url.rsplit("/chat", 1)[0] + "/other")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)
    finally:
        f.stop()


def test_the_shipped_app_installs_from_the_base_path(tmp_path):
    """The shipped app's manifest and Home Screen icons answer beside
    index.html under the base path, with the types a phone installs from;
    the manifest's scope and start page are relative to it, so the base path."""
    f = as_tailscaled(WebFrontend.from_config({"port": 0, "tailscale_login": LOGIN, "public_url": PUBLIC_URL},
                                              store_path=tmp_path / "priv" / "webchat.sqlite",
                                              trusted_config=True))
    f.start()
    try:
        status, _h, body = call(f, "GET", "/")
        assert status == 200 and b'href="manifest.webmanifest"' in body and b'href="icon-180.png"' in body
        status, h, body = call(f, "GET", "/manifest.webmanifest")
        assert status == 200 and h["Content-Type"] == "application/manifest+json"
        man = json.loads(body)
        assert man["scope"] == man["start_url"] == "./"
        for name in ("icon-180.png", *(i["src"] for i in man["icons"] if i["type"] == "image/png")):
            status, h, _b = call(f, "GET", "/" + name)
            assert status == 200 and h["Content-Type"] == "image/png", name
    finally:
        f.stop()


def test_pairing_sets_the_cookie(tmp_path):
    f = make(tmp_path, tailscale_login="", public_url="https://host.example.test/chat")
    f.start()
    try:
        token = f.pair_token()
        assert token and (tmp_path / "priv" / "webchat-pair.token").stat().st_mode & 0o077 == 0
        assert call(f, "POST", "/api/pair", {"token": "nope"})[0] == 401
        status, h, _b = call(f, "POST", "/api/pair", {"token": token})
        cookie = h["Set-Cookie"]
        assert status == 200 and cookie.startswith(f"ea_web={token};")
        for attr in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/chat"):
            assert attr in cookie
        assert call(f, "GET", "/api/me", headers={"Cookie": f"ea_web={token}"})[0] == 200
        # the header counts only when a login is configured
        assert call(f, "GET", "/api/me", headers={"Tailscale-User-Login": ""})[0] == 401
    finally:
        f.stop()


def test_a_post_from_another_origin_is_refused(fe):
    for origin in ("https://evil.example.test", "https://host.example.test",
                   "https://host.example.test:443", "http://host.example.test:8444",
                   "https://host.example.test:8445", f"http://127.0.0.1:{fe.port}", None, "null"):
        status, _h, _b = owner(fe, "POST", "/api/channels/main/messages", {"text": "x"},
                               {"Origin": origin})
        assert status == 403, origin
    status, _h, _b = owner(fe, "POST", "/api/channels/main/messages", {"text": "x"})
    assert status == 200


def test_posting_a_message_dispatches_an_inbound_message(fe):
    got, ev = catcher(fe)
    status, _h, body = owner(fe, "POST", "/api/channels/main/messages", {"text": "hello bot"})
    row = json.loads(body)
    assert status == 200 and row["author"] == "owner" and row["text"] == "hello bot"
    assert ev.wait(5)
    msg = got[0]
    assert isinstance(msg, InboundMessage)
    assert (msg.channel, msg.msg_id, msg.thread_key, msg.user_id) == \
        ("main", row["id"], row["thread_key"], "owner")
    assert not msg.is_reply and not msg.is_bot and not msg.is_self and not msg.is_dm
    ev.clear()
    status, _h, body = owner(fe, "POST", "/api/channels/dm/messages", {"text": "x"})
    ev.wait(5)
    assert got[-1].is_dm
    ev.clear()
    owner(fe, "POST", "/api/channels/main/messages",
          {"text": "more", "thread_key": row["thread_key"]})
    assert ev.wait(5)
    assert got[-1].is_reply and got[-1].thread_key == row["thread_key"]
    assert owner(fe, "POST", "/api/channels/main/messages", {"text": ""})[0] == 400
    assert owner(fe, "POST", "/api/channels/nope/messages", {"text": "x"})[0] == 404
    assert owner(fe, "POST", "/api/channels/main/messages",
                 {"text": "x", "thread_key": "t999"})[0] == 404
    assert owner(fe, "POST", "/api/channels/main/messages", raw=b"not json",
                 headers={"Content-Type": "application/json"})[0] == 400


def test_a_card_click_dispatches_an_action_with_the_stored_value(fe):
    got, ev = catcher(fe)
    ref = fe.post_card("dm", card(select=CardSelect("pick", "Pick", [("A", "a"), ("B", "b")])))
    path = f"/api/cards/dm/{ref.id}/actions"
    status, _h, body = owner(fe, "POST", path, {"action_id": "approve_send",
                                                "value": {"token": "forged"}})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert ev.wait(5)
    a = got[0]
    assert isinstance(a, ActionEvent)
    assert (a.action_id, a.user_id, a.source, a.value, a.block_id) == \
        ("approve_send", "owner", "button", {"token": "t-1"}, "blk")
    assert (a.card.channel, a.card.id, a.card.thread_key) == ("dm", ref.id, ref.thread_key)
    ev.clear()
    assert owner(fe, "POST", path, {"action_id": "pick", "selected": "b"})[0] == 200
    assert ev.wait(5)
    assert (got[-1].source, got[-1].selected) == ("select", "b")
    assert owner(fe, "POST", path, {"action_id": "pick", "selected": "zzz"})[0] == 400
    assert owner(fe, "POST", path, {"action_id": "nope"})[0] == 400
    assert owner(fe, "POST", "/api/cards/dm/w999/actions", {"action_id": "approve_send"})[0] == 404
    fe.replace_card_with_status(ref, "Sent")
    assert owner(fe, "POST", path, {"action_id": "approve_send"})[0] == 400


def test_an_upload_stores_the_file_and_dispatches_a_file_ref(fe, tmp_path, monkeypatch):
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    got, ev = catcher(fe)
    data = b"%PDF-1.4\r\n\x00\x01binary\r\n"
    body, ctype = multipart({"text": "please read",
                             "file": ("notes v2.pdf", "application/pdf", data)})
    status, _h, out = owner(fe, "POST", "/api/channels/dm/files", raw=body,
                            headers={"Content-Type": ctype})
    row = json.loads(out)
    assert status == 200 and row["kind"] == "file" and row["text"] == "please read"
    assert row["file"]["name"] == "notes v2.pdf" and row["file"]["size"] == len(data)
    assert ev.wait(5)
    msg = got[0]
    assert msg.subtype == "file_share" and msg.text == "please read"
    ref = msg.files[0]
    assert ref.url == row["file"]["url"] and ref.mimetype == "application/pdf"
    status, h, got_bytes = owner(fe, "GET", "/api/files/" + ref.id)
    assert status == 200 and got_bytes == data
    assert h["Content-Disposition"].startswith("attachment;")
    assert "sandbox" in h["Content-Security-Policy"]
    assert Path(fe.download(ref, tmp_path / "uploads")).read_bytes() == data
    assert call(fe, "GET", "/api/files/" + ref.id)[0] == 401
    assert owner(fe, "GET", "/api/files/f999")[0] == 404
    body, ctype = multipart({"text": "no file"})
    assert owner(fe, "POST", "/api/channels/dm/files", raw=body,
                 headers={"Content-Type": ctype})[0] == 400


def test_threads_list_newest_first_and_a_thread_oldest_first(fe):
    a = fe.post("main", "a")
    b = fe.post("main", "b")
    fe.post("main", "a2", thread_ts=a.thread_key)
    status, _h, body = owner(fe, "GET", "/api/channels/main/threads")
    rows = json.loads(body)
    assert status == 200 and [r["id"] for r in rows] == [b.id, a.id]
    assert rows[1]["reply_count"] == 1 and rows[1]["last_at"] >= rows[1]["posted_at"]
    assert {"seq", "thread_key", "reply_count", "last_at"} <= set(rows[0])
    # before= is the smallest first-message seq the app holds
    status, _h, body = owner(fe, "GET", f"/api/channels/main/threads?limit=1&before={rows[0]['seq']}")
    assert [r["id"] for r in json.loads(body)] == [a.id]
    status, _h, body = owner(fe, "GET", f"/api/channels/main/threads/{a.thread_key}")
    assert [r["text"] for r in json.loads(body)] == ["a", "a2"]
    assert owner(fe, "GET", "/api/channels/main/threads/t999")[0] == 404
    assert owner(fe, "GET", "/api/channels/nope/threads")[0] == 404
    assert owner(fe, "GET", "/api/channels/main/threads?limit=x")[0] == 400
    chans = {c["id"]: c for c in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert chans["main"]["unread"] == 0 and chans["main"]["kind"] == "main"
    fe.post("main", "c")
    chans = {c["id"]: c for c in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert chans["main"]["unread"] == 1


def test_encoded_channel_and_thread_ids_route(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        f.mirror_channel("stream:7", "ea", "main")
        key = "stream:7:topic:a%2Fb%20c"
        f.mirror_post(MessageRef("stream:7", "1", key), "x")
        q = urllib.parse.quote
        status, _h, body = owner(f, "GET", f"/api/channels/{q('stream:7', safe='')}/threads/"
                                           f"{q(key, safe='')}")
        assert status == 200 and json.loads(body)[0]["thread_key"] == key
    finally:
        f.stop()


def test_events_replay_from_a_seq_and_deliver_new_ones(fe):
    a = fe.post("main", "one")
    seq_after_a = fe.store.max_event_seq()
    fe.post("main", "two", thread_ts=a.thread_key)
    s = Stream(fe, "?since=" + str(seq_after_a))
    try:
        assert s.resp.status == 200
        assert s.resp.getheader("Content-Type").startswith("text/event-stream")
        first = s.events(1)[0]
        assert first["event"] == "message" and json.loads(first["data"])["text"] == "two"
        fe.react("main", a.id, Status.EYES)
        nxt = s.events(1)[0]
        assert nxt["event"] == "reaction" and int(nxt["id"]) > int(first["id"])
        assert json.loads(nxt["data"])["reactions"] == ["eyes"]
    finally:
        s.close()
    # Last-Event-ID wins over a since= the browser keeps in the URL
    s = Stream(fe, "?since=0", headers={"Last-Event-ID": str(seq_after_a)})
    try:
        assert json.loads(s.events(1)[0]["data"])["text"] == "two"
    finally:
        s.close()


def test_a_seq_from_another_store_still_gets_live_events(fe):
    s = Stream(fe, "?since=999999")
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        fe.post("main", "after a reset")
        assert json.loads(s.events(1)[0]["data"])["text"] == "after a reset"
    finally:
        s.close()


def test_a_chunked_body_is_read(fe):
    got, ev = catcher(fe)
    conn = http.client.HTTPConnection("127.0.0.1", fe.port, timeout=5)
    try:
        conn.request("POST", "/chat/api/channels/main/messages",
                     body=iter([b'{"text": ', b'"in chunks"}']),
                     headers={**OWNER_H, "Content-Type": "application/json",
                              "Origin": PUBLIC_ORIGIN, "Transfer-Encoding": "chunked"},
                     encode_chunked=True)
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read())["text"] == "in chunks"
    finally:
        conn.close()
    assert ev.wait(5) and got[0].text == "in chunks"


def test_an_idle_stream_gets_ping_comments(fe):
    s = Stream(fe)
    try:
        lines = s.lines(lambda seen: ": ping" in seen)
        assert lines[0] == "retry: 3000"
    finally:
        s.close()
    assert call(fe, "GET", "/api/events")[0] == 401


def test_stop_closes_open_streams(tmp_path):
    f = make(tmp_path)
    f.start()
    s = Stream(f)
    s.lines(lambda seen: "retry: 3000" in seen)
    f.stop()
    assert s.resp.fp.readline() in (b"", b"\n")
    s.close()


# several processes, one store (docs/webchat.md, CLI senders)


def writer(f: WebFrontend) -> WebFrontend:
    """A command-line sender's writer-only copy of f's store: its own
    SQLite connection, as another process has."""
    w = WebFrontend.from_config({}, store_path=f.store_path, writer_only=True)
    w.start()
    return w


def test_a_writer_only_frontend_opens_the_store_and_serves_nothing(tmp_path):
    # untrusted too: a writer-only side checks nothing and serves nothing
    w = WebFrontend.from_config({"port": 0}, store_path=tmp_path / "priv" / "webchat.sqlite",
                                writer_only=True)
    w.start()
    w.start()
    try:
        assert w.writer_only and (tmp_path / "priv" / "webchat.sqlite").exists()
        assert w._server is None and w._thread is None and w._tail is None and w.port == 0
        assert not (tmp_path / "priv" / wf.PAIR_FILE).exists()
        assert w.store.channels() == []           # the bot makes the channels
        assert w.ping()
    finally:
        w.stop()
    assert make(tmp_path).writer_only is False


def test_the_tail_hands_on_only_what_another_process_wrote(tmp_path):
    path = tmp_path / "priv" / "webchat.sqlite"
    own, other = wf.WebStore(path), wf.WebStore(path)
    sub = own.subscribe()

    def drain() -> list:
        out = []
        while not sub.q.empty():
            out.append(sub.q.get_nowait())
        return out

    def texts(items) -> list:
        return [json.loads(p)["text"] for _s, t, p in items if t == "message"]
    own.upsert_channel("main", "ea", "main")
    assert [t for _s, t, _p in drain()] == ["channel"]
    assert own.deliver_new() == 0 and drain() == []     # its own events go out once
    row = other.add_message("main", mid=None, thread_key=None, author="bot",
                            kind="message", text="from a cron lane")
    assert drain() == []                                # until the tail reads the log
    assert own.deliver_new() == 1
    (got,) = drain()
    assert got == other.events_since(got[0] - 1)[0]     # the same seq, type and JSON
    assert json.loads(got[2])["id"] == row["id"]
    assert own.deliver_new() == 0 and drain() == []
    # another process's event logged before this one's goes out first, at once
    other.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                      text="first")
    own.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                    text="second")
    got = drain()
    assert texts(got) == ["first", "second"] and got == sorted(got)
    assert own.deliver_new() == 0 and drain() == []
    # with no stream open, new events are passed over: a stream replays the log
    own.unsubscribe(sub)
    other.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                      text="unseen")
    assert own.deliver_new() == 0
    sub = own.subscribe()
    other.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                      text="seen")
    assert own.deliver_new() == 1 and texts(drain()) == ["seen"]


def test_a_writer_only_copy_reaches_the_served_stream(fe):
    fe.mirror_channel("stream:7", "ea", "main")
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        w = writer(fe)
        t0 = time.monotonic()
        w.mirror_post(MessageRef("stream:7", "501", "stream:7:topic:brief", 1000.0),
                      "*morning*")
        ev = s.events(1)[0]
        assert time.monotonic() - t0 < 2.5            # one tail_s read away
        row = json.loads(ev["data"])
        assert ev["event"] == "message" and int(ev["id"]) == w.store.max_event_seq()
        assert (row["id"], row["thread_key"], row["text"]) == \
            ("501", "stream:7:topic:brief", "**morning**")
    finally:
        s.close()


def test_a_stream_gets_each_event_once_in_seq_order(fe):
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        w = writer(fe)
        fe.post("main", "one")
        w.mirror_post(MessageRef("main", "c1", "c1", 1000.0), "from the cli")
        fe.post("main", "three")                      # catches up on the cli's first
        evs = s.events(3)
        assert [json.loads(e["data"])["text"] for e in evs] == ["one", "from the cli", "three"]
        time.sleep(fe.tail_s + 0.5)                   # a tail read finds nothing new
        fe.post("main", "four")
        nxt = s.events(1)[0]
        assert json.loads(nxt["data"])["text"] == "four"
        ids = [int(e["id"]) for e in [*evs, nxt]]
        assert ids == sorted(set(ids))
    finally:
        s.close()


def test_stop_ends_the_tail_thread(tmp_path):
    f = make(tmp_path)
    f.start()
    tail = f._tail
    assert tail is not None and tail.is_alive()
    f.stop()
    assert f._tail is None and not tail.is_alive()


def test_a_file_id_another_process_took_is_minted_again(tmp_path):
    path = tmp_path / "priv" / "webchat.sqlite"
    own, other = wf.WebStore(path), wf.WebStore(path)
    real = own.next_number
    calls = []

    def stale(table, prefix):
        calls.append(table)
        # the first mint reads the table before the other process's row lands
        return 1 if len(calls) == 1 else real(table, prefix)
    assert other.add_file(None, "main", "a.txt", b"theirs", "text/plain")["id"] == "f1"
    own.next_number = stale
    row = own.add_file(None, "main", "b.txt", b"mine", "text/plain")
    assert row["id"] == "f2" and len(calls) == 2
    assert Path(own.file("f1")["path"]).read_bytes() == b"theirs"
    assert Path(row["path"]).read_bytes() == b"mine"


def test_shadow_mode_hands_web_input_to_the_composite(tmp_path):
    f = make(tmp_path)
    handed: list = []

    def composite(ev):
        handed.append(ev)
        if isinstance(ev, InboundMessage):
            ref = MessageRef(ev.channel, str(9000 + len(handed)),
                             ev.thread_key or "stream:7:topic:new", 5.0)
            if ev.files:
                assert Path(ev.raw["path"]).read_bytes() == b"bytes"
                f.mirror_file(ref, FileRef(ev.files[0].name, id="/user_uploads/9.txt"),
                              "owner", text=ev.text)
            else:
                f.mirror_post(ref, ev.text, "owner")
            return ref
        return None
    f.inbound = composite
    got, _ev = catcher(f)
    f.start()
    try:
        f.mirror_channel("stream:7", "ea", "main")
        status, _h, body = owner(f, "POST", "/api/channels/stream%3A7/messages", {"text": "hi"})
        row = json.loads(body)
        assert status == 200 and row["id"] == "9001" and row["author"] == "owner"
        assert isinstance(handed[0], InboundMessage) and handed[0].msg_id == ""
        assert handed[0].user_id == "owner" and handed[0].raw == {"source": "web"}
        body, ctype = multipart({"file": ("n.txt", "text/plain", b"bytes")})
        status, _h, out = owner(f, "POST", "/api/channels/stream%3A7/files", raw=body,
                                headers={"Content-Type": ctype})
        row = json.loads(out)
        assert status == 200 and row["file"]["id"] == "stream:7:9002"
        assert row["file"]["url"] == "/chat/api/files/stream%3A7%3A9002"
        # the composite kept no bytes; the frontend stored them under the row
        assert owner(f, "GET", "/api/files/stream%3A7%3A9002")[2] == b"bytes"
        assert not list((tmp_path / "priv" / "webchat-files" / "incoming").iterdir())
        cref = MessageRef("stream:7", "9100", "stream:7:topic:lock", 6.0)
        f.mirror_card(cref, card())
        assert owner(f, "POST", "/api/cards/stream%3A7/9100/actions",
                     {"action_id": "approve_send"})[0] == 200
        click = handed[-1]
        assert isinstance(click, ActionEvent) and click.card.id == "9100"
        assert click.value == {"token": "t-1"}
        assert got == []                   # nothing went to the registry
        assert f.store.channel("main") is None
    finally:
        f.stop()


# wiring


def _ea_dir(tmp_path, chat):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": chat}))
    return tmp_path


def test_load_frontend_builds_the_web_frontend(tmp_path):
    assert "web" in chat_pkg.FRONTENDS
    ea = _ea_dir(tmp_path, {"frontend": "web", "web": {"port": 0, "base_path": "/c"}})
    f = load_frontend(ea_dir=ea, env={})
    f.poll_s = 0.02
    assert isinstance(f, WebFrontend) and f.name == "web"
    assert f.store_path == tmp_path / "state" / "webchat.sqlite"
    assert f.base_path == "/c" and f.bind == "127.0.0.1"
    assert prompt_strings_for_config(ea)["confirm_location"] == \
        "the Send button is on the card in your DM thread"
    # ea-config.json is not the owner's file: built, but it serves nothing
    # (tests/test_web_hardening.py has the owner's block)
    assert f.trusted_config is False
    with pytest.raises(ChatError, match="not serving"):
        f.start()
    assert f._server is None and f.refused and not f.ping()
    assert not (tmp_path / "state" / "webchat.sqlite").exists()


def test_load_frontend_takes_the_store_path_key(tmp_path):
    ea = _ea_dir(tmp_path, {"frontend": "web", "web": {"store_path": str(tmp_path / "x.db")}})
    assert load_frontend(ea_dir=ea, env={}).store_path == tmp_path / "x.db"


def test_an_owner_process_keeps_the_store_in_the_private_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_pkg.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_pkg, "private_dir", lambda: tmp_path / "priv")
    assert chat_pkg._web_store_path({}, None) == tmp_path / "priv" / "webchat.sqlite"
    assert chat_pkg._web_store_path({}, tmp_path / "ea") == \
        tmp_path / "ea" / "state" / "webchat.sqlite"


def test_get_chat_defaults_the_web_block(tmp_path):
    p = tmp_path / "ea-config.json"
    p.write_text(json.dumps({"chat": {"frontend": "web", "web": {"port": 9000}}}))
    web = ea_config.get_chat(p)["web"]
    assert web["port"] == 9000 and web["bind"] == "127.0.0.1" and web["base_path"] == "/chat"
    assert set(web) == {"bind", "port", "base_path", "public_url", "tailscale_login",
                        "trust_tailscale_header", "pair_token_file", "store_path",
                        "backfill_days"}
    assert web["backfill_days"] == 7 and web["trust_tailscale_header"] is True
    assert ea_config.get_chat(tmp_path / "missing.json")["web"]["port"] == 8470


# ── the Settings screen (/api/settings/<name>) ─────────────────────────

@pytest.fixture
def routing_page(tmp_path, monkeypatch):
    """The EA's routing page (model_routing.WebSettings) in an owner
    process with a scratch private dir and a made-up local model."""
    import model_routing
    import owner_trust
    import remote_llm
    priv = tmp_path / "routing-priv"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(remote_llm, "_owner", lambda: True)
    monkeypatch.setattr(remote_llm, "_breaker", lambda *a: None)
    monkeypatch.setattr(remote_llm, "api_key", lambda: "k")
    monkeypatch.setattr(remote_llm, "config", lambda: {
        **remote_llm._off(), "endpoint": "https://llm.example.test/v1", "model": "local-27b"})
    return model_routing.WebSettings(), priv


def test_the_settings_routes_answer_the_owner_and_save_the_routing_table(fe, routing_page):
    page, priv = routing_page
    path = "/api/settings/routing"
    status, _h, body = owner(fe, "GET", "/api/me")
    assert status == 200 and json.loads(body)["settings"] == []
    assert owner(fe, "GET", path)[0] == 404  # no page until the host registers one
    fe.settings_pages["routing"] = page
    assert json.loads(owner(fe, "GET", "/api/me")[2])["settings"] == ["routing"]
    status, _h, body = owner(fe, "GET", path)
    data = json.loads(body)
    assert status == 200 and data["local"]["model"] == "local-27b"
    assert [r["task"] for r in data["rows"]][0] == "mail" and data["values"] == ["local", "hosted", "auto"]
    status, _h, body = owner(fe, "POST", path, {"tasks": {"chat": "local", "drafting": "hosted"}})
    out = json.loads(body)
    assert status == 200 and out["saved"] is True
    assert {r["task"]: r["value"] for r in out["rows"]}["chat"] == "local"
    saved = json.loads((priv / "routing.json").read_text())["tasks"]
    assert saved["chat"] == "local" and saved["drafting"] == "hosted"
    assert (priv / "routing.json").stat().st_mode & 0o777 == 0o600
    assert owner(fe, "GET", "/api/settings/other")[0] == 404


def test_the_settings_routes_need_the_owner_and_the_app_s_own_origin(fe, routing_page):
    page, priv = routing_page
    fe.settings_pages["routing"] = page
    path, body = "/api/settings/routing", {"tasks": {"chat": "local"}}
    assert call(fe, "GET", path)[0] == 401
    assert call(fe, "POST", path, body)[0] == 401
    assert call(fe, "GET", path, headers={"Tailscale-User-Login": "x@example.test"})[0] == 401
    for origin in ("https://evil.example.test", None, "null"):
        assert owner(fe, "POST", path, body, {"Origin": origin})[0] == 403, origin
    assert owner(fe, "POST", path, body, {"Host": "evil.example.test:8444"})[0] == 421
    assert owner(fe, "GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    # through the relay a phone may read the screen, never change it
    assert owner(fe, "GET", path, headers={"X-Tacitus-Relay": "1"})[0] == 200
    status, _h, raw = owner(fe, "POST", path, body, {"X-Tacitus-Relay": "1"})
    assert status == 403 and "own web chat" in json.loads(raw)["error"]
    assert owner(fe, "POST", path, raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400
    assert not (priv / "routing.json").exists()


def test_a_floored_row_is_refused_and_nothing_is_written(fe, routing_page, monkeypatch):
    page, priv = routing_page
    fe.settings_pages["routing"] = page
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"review": "local"}})
    assert status == 400 and json.loads(raw)["error"] == "review is hosted only (the stakes floor)"
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"chat": "fable"}})
    assert status == 400 and "chat takes local, hosted or auto" in json.loads(raw)["error"]
    assert owner(fe, "POST", "/api/settings/routing", {"other": 1})[0] == 400
    assert not (priv / "routing.json").exists()
    # the keep floor: hosted on mail is refused while the keep list has entries
    import remote_llm
    kept = {**remote_llm.config(), "keep_from_anthropic": {"domains": ["contractor.example.com"],
                                                            "addresses": [], "admissions": True}}
    monkeypatch.setattr(remote_llm, "config", lambda: kept)
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"mail": "hosted"}})
    assert status == 400 and "keep floor" in json.loads(raw)["error"]
    assert not (priv / "routing.json").exists()
