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
    """A loopback request; a change (POST, PATCH, DELETE) carries this
    server's loopback Origin unless `headers` names one (None leaves it out)."""
    data = json.dumps(body).encode() if body is not None else raw
    h = {"Content-Type": "application/json"} if body is not None else {}
    if method != "GET":
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
    the public Host and, on a change, the public Origin."""
    h = {**OWNER_H, "Host": PUBLIC_HOST}
    if method != "GET":
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


def multipart(fields, boundary: str = "b0undary") -> tuple[bytes, str]:
    """A multipart body from a dict, or from (name, value) pairs where a
    name comes more than once."""
    out = b""
    for name, val in (fields.items() if isinstance(fields, dict) else fields):
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
        f.mirror_channel("C0EA", "ea", "main")
        row = f.mirror_post(MessageRef("C0EA", "1", "t-x", 1000.0), "x")
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
                                             "word": "send", "ask": "", "date": {}}
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
        assert f.archived_map() == {"main": False, "sched": False, "r1": True, "r2": False}
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
    assert ps["confirm_location"] == "the Send button is on the card in Action requests"
    assert ps["card_place"] == "Action requests" and "Action requests" in ps["confirm_sentence"]
    assert "web chat" in ps["platform"] and "Markdown" in ps["format_rule_main"]
    assert f.llm_tool_grants() == ""
    assert f.capabilities == {"buttons", "select", "files", "private_rooms", "reactions"}
    assert tuple(f.limits) == (20000, 20000, 200, 0)
    assert (f.owner_id, f.bot_user_id, f.main_channel, f.sched_channel) == \
        ("owner", "bot", "main", "sched")
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
        f.mirror_channel("C0EA", "ea", "main")
        f.mirror_channel("C0EA", "ea", "main")
        ref = MessageRef("C0EA", "1001", "t-hello", 1000.0)
        start = f.store.max_event_seq()
        f.mirror_post(ref, "*hi*")
        f.mirror_post(ref, "*hi*")
        row = f.store.message("C0EA", "1001")
        assert row["text"] == "**hi**" and row["thread_key"] == "t-hello"
        assert row["posted_at"] == 1000.0 and row["author"] == "bot"
        f.mirror_update(ref, "*edited*")
        f.mirror_reaction("C0EA", "1001", Status.EYES, True)
        f.mirror_reaction("C0EA", "1001", Status.EYES, True)
        types = [t for _s, t, _p in f.store.events_since(start)]
        assert types == ["message", "update", "reaction"]
        cref = MessageRef("D0DM", "1002", "t-lock", 1001.0)
        f.mirror_card(cref, card())
        f.mirror_card(cref, card())
        f.mirror_card_status(cref, "Sent")
        assert f.store.message("D0DM", "1002")["kind"] == "status"
        assert f.store.channel("D0DM")["kind"] == "room"   # a placeholder row
        f.mirror_channel("D0DM", "ea-dm", "dm")
        assert f.store.channel("D0DM")["kind"] == "dm"
        f.mirror_post(MessageRef("C0EA", "1003", "t-hello"), "typed", "owner")
        assert f.store.message("C0EA", "1003")["author"] == "owner"
        assert [m["id"] for m in f.store.thread("C0EA", "t-hello")] == \
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
        ref = MessageRef("C0EA", "1100", "t-files")
        row = f.mirror_file(ref, FileRef("pic.png", url="https://chat.example.test/user_uploads/1/pic.png",
                                         id="/user_uploads/1/pic.png"), path=src)
        assert row["file"]["url"] == "/chat/api/files/%2Fuser_uploads%2F1%2Fpic.png"
        status, headers, body = owner(f, "GET", "/api/files/%2Fuser_uploads%2F1%2Fpic.png")
        assert status == 200 and body == b"\x89PNG data"
        ref2 = MessageRef("C0EA", "1101", "t-files")
        row2 = f.mirror_file(ref2, FileRef("b.txt", url="https://chat.example.test/u/b.txt",
                                           id="/u/b.txt"))
        # no bytes here: no id, and the primary's absolute url
        assert row2["file"]["id"] == "" and row2["file"]["url"] == "https://chat.example.test/u/b.txt"
    finally:
        f.stop()


def test_quiet_history_rows_have_no_event_and_are_never_unread(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("C0EA", "ea", "main")
    key = "t-plans"
    start = f.store.max_event_seq()
    row = f.mirror_post(MessageRef("C0EA", "10", key, 1000.0), "*the plan*", quiet=True)
    assert row["text"] == "**the plan**" and row["posted_at"] == row["updated_at"] == 1000.0
    f.mirror_post(MessageRef("C0EA", "11", key, 1010.0), "fine", "owner", quiet=True)
    f.mirror_post(MessageRef("C0EA", "12", key, 1020.0), "Done, *at nine*.", quiet=True)
    assert f.store.message("C0EA", "12")["text"] == "Done, **at nine**."
    f.mirror_file(MessageRef("C0EA", "13", key, 1030.0),
                  FileRef("a.pdf", url="https://chat.example.test/u/a.pdf"), quiet=True,
                  text="the file")
    assert f.store.events_since(start) == []
    assert f.store.channel("C0EA")["unread"] == 0
    assert [m["id"] for m in f.store.thread("C0EA", key)] == ["10", "11", "12", "13"]
    # a row it holds is left as it is
    assert f.mirror_post(MessageRef("C0EA", "10", key, 1000.0), "changed", quiet=True) is None
    assert f.mirror_file(MessageRef("C0EA", "13", key, 1030.0), FileRef("b.pdf"),
                         quiet=True) is None
    assert f.store.message("C0EA", "10")["text"] == "**the plan**"
    # an older message never goes in after a newer one of its thread
    assert f.mirror_post(MessageRef("C0EA", "9", key, 990.0), "older", quiet=True) is None
    # a live post still counts, and a history row from the owner does not reset it
    f.mirror_post(MessageRef("C0EA", "20", key, 2000.0), "live")
    f.mirror_post(MessageRef("C0EA", "21", "t-other", 2100.0), "old ask",
                  "owner", quiet=True)
    assert f.store.channel("C0EA")["unread"] == 1


def test_mirror_backfilled_sends_one_channel_event(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("C0EA", "ea", "main", description="the EA channel")
    f.mirror_post(MessageRef("C0EA", "10", "k", 1000.0), "old", quiet=True)
    start = f.store.max_event_seq()
    row = f.mirror_backfilled("C0EA")
    assert row["last_at"] == 1000.0 and row["unread"] == 0
    assert [t for _s, t, _p in f.store.events_since(start)] == ["channel"]
    assert f.mirror_backfilled("C0NONE") is None


def test_a_pointer_to_the_primarys_card_opens_the_card_here(tmp_path):
    """In shadow mode the core builds the pointer "... is in your DM: <link|open
    it>" with the primary's permalink, which would open Slack from the web
    app. A link to a message the store holds points at its copy here: as the
    pointer is mirrored, and, for a pointer stored ahead of its card, once the
    card's history is copied. The owner's own text keeps its links."""
    f = make(tmp_path)
    f.mirror_channel("C0EA", "ea", "main")
    f.mirror_channel("D0DM", "ea-dm", "dm")
    slack = "https://x.slack.com/archives/D0DM/p{}"
    f.mirror_card(MessageRef("D0DM", "1700000000.006232", "1700000000.006232", 1000.0), card())
    f.mirror_post(MessageRef("C0EA", "1700000000.006233", "t-reply", 1001.0),
                  ":lock: The Send button for this draft is in your DM: "
                  f"<{slack.format('1700000000006232')}|open it>")
    text = f.store.message("C0EA", "1700000000.006233")["text"]
    assert text.endswith(f"[open it]({f.permalink('D0DM', '1700000000.006232')})"), text
    assert [r["id"] for r in f.store.linked_cards("C0EA", "t-reply")] == ["1700000000.006232"]
    # a pointer copied before its card keeps the primary's link until the card is here
    f.mirror_post(MessageRef("C0EA", "1700000000.006301", "t-later", 1002.0),
                  ":lock: The Create button for this event is in your DM: "
                  f"<{slack.format('1700000000006300')}|open it>", quiet=True)
    assert f.store.message("C0EA", "1700000000.006301")["text"].endswith(
        f"({slack.format('1700000000006300')})")
    f.mirror_card(MessageRef("D0DM", "1700000000.006300", "1700000000.006300", 1003.0), card())
    f.mirror_backfilled("D0DM")
    assert f.store.message("C0EA", "1700000000.006301")["text"].endswith(
        f"({f.permalink('D0DM', '1700000000.006300')})")
    assert f.relink() == 0, "nothing left to point here"
    f.mirror_post(MessageRef("C0EA", "1700000000.006302", "t-later"),
                  f"see [this]({slack.format('1700000000006232')})", "owner")
    assert f.store.message("C0EA", "1700000000.006302")["text"] == \
        f"see [this]({slack.format('1700000000006232')})"


def test_mirror_channel_keeps_a_description_not_given(tmp_path):
    f = make(tmp_path)
    f.mirror_channel("C0ATLAS", "ea-atlas", "room", description="the atlas project")
    f.mirror_channel("C0ATLAS", "ea-atlas", "room", True)
    row = f.store.channel("C0ATLAS")
    assert row["description"] == "the atlas project" and row["archived"] is True
    f.mirror_channel("C0ATLAS", "ea-atlas", "room", True, description="")
    assert f.store.channel("C0ATLAS")["description"] == ""


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


# read state (docs/webchat.md, Unread)


def said(store, author, text, thread=None, *, history=False) -> dict:
    """A message in `main`, as the store keeps it."""
    return store.add_message("main", mid=None, thread_key=thread, author=author, kind="message",
                             text=text, history=history)


def test_each_thread_counts_its_own_and_the_owners_message_reads_only_its_thread(tmp_path):
    store = wf.WebStore(tmp_path / "w.sqlite")
    store.upsert_channel("main", "ea", "main")
    a = said(store, "bot", "a")
    said(store, "bot", "a2", a["thread_key"])
    b = said(store, "bot", "b")
    row = store.channel("main")
    # the counts take in every event so far, and an app counts each later one itself
    assert (row["unread"], row["unread_threads"], row["as_of"]) == \
        (3, {a["thread_key"]: 2, b["thread_key"]: 1}, store.max_event_seq())
    said(store, "owner", "Thanks.", a["thread_key"])
    assert store.channel("main")["unread_threads"] == {b["thread_key"]: 1}
    said(store, "bot", "a3", a["thread_key"])
    assert store.channel("main")["unread_threads"] == {a["thread_key"]: 1, b["thread_key"]: 1}
    # a history row counts neither as unread nor as the owner's last message
    said(store, "bot", "an old answer", b["thread_key"], history=True)
    said(store, "owner", "an old question", b["thread_key"], history=True)
    said(store, "bot", "an old thread", history=True)
    row = store.channel("main")
    assert (row["unread"], row["unread_threads"]) == (2, {a["thread_key"]: 1, b["thread_key"]: 1})
    store.close()


def test_a_thread_mark_moves_forward_to_what_was_shown_and_lifts_the_floor(tmp_path):
    store = wf.WebStore(tmp_path / "w.sqlite")
    store.upsert_channel("main", "ea", "main")
    a = said(store, "bot", "a")
    a2 = said(store, "bot", "a2", a["thread_key"])
    b = said(store, "bot", "b")
    start = store.max_event_seq()
    # the app showed `a` alone: `a2` stays unread
    out = store.mark_thread_read("main", a["thread_key"], upto=a["seq"])
    assert (out["channel"], out["thread_key"], out["unread"], out["as_of"]) == \
        ("main", a["thread_key"], 2, start)
    assert out["unread_threads"] == {a["thread_key"]: 1, b["thread_key"]: 1}
    (ev,) = store.events_since(start)
    assert ev[1] == "read" and json.loads(ev[2]) == out
    # never back, and a mark that does not move tells no one
    for older in (0, a["seq"]):
        assert store.mark_thread_read("main", a["thread_key"], upto=older) == {**out, "as_of": ev[0]}
    assert store.events_since(ev[0]) == []
    # the whole thread: the floor rises to just under the oldest unread message
    assert store.mark_thread_read("main", a["thread_key"])["unread_threads"] == {b["thread_key"]: 1}
    assert store._one("SELECT seq FROM reads WHERE channel='main'")["seq"] == b["seq"] - 1 == a2["seq"]
    assert store._all("SELECT * FROM thread_reads") == []
    assert store.mark_thread_read("main", b["thread_key"])["unread"] == 0
    assert store._one("SELECT seq FROM reads WHERE channel='main'")["seq"] == b["seq"]
    # a new message after the floor counts as before
    said(store, "bot", "a3", a["thread_key"])
    assert store.channel("main")["unread_threads"] == {a["thread_key"]: 1}
    # a move that changes no count (the owner wrote last there) tells no one either
    mine = said(store, "owner", "A question of mine.")
    start = store.max_event_seq()
    assert store.mark_thread_read("main", mine["thread_key"])["unread_threads"] == {a["thread_key"]: 1}
    assert store.events_since(start) == []
    assert store.mark_thread_read("main", "t999") is None
    # the whole channel at once: the floor, with a `read` event of its own
    start = store.max_event_seq()
    assert store.mark_read("main")["unread"] == 0
    assert [(t, json.loads(p)["thread_key"]) for _s, t, p in store.events_since(start)] == [("read", "")]
    store.close()


def test_a_store_from_before_thread_marks_keeps_its_channel_floor(tmp_path):
    """The channel's read mark from before is the floor: nothing it covers
    turns unread when the store takes the thread marks."""
    import sqlite3
    path = tmp_path / "old" / "webchat.sqlite"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, id TEXT, "
        "thread_key TEXT, author TEXT, kind TEXT, text TEXT, card TEXT, file TEXT, "
        "reactions TEXT, posted_at REAL, updated_at REAL, history INT DEFAULT 0, UNIQUE(channel, id));"
        "CREATE TABLE reads(channel TEXT PRIMARY KEY, seq INT);"
        "INSERT INTO messages(channel, id, thread_key, author, kind, text, reactions, posted_at, "
        "updated_at) VALUES ('main', 'w1', 't1', 'bot', 'message', 'read', '[]', 1.0, 1.0), "
        "('main', 'w2', 't2', 'bot', 'message', 'read too', '[]', 2.0, 2.0), "
        "('main', 'w3', 't1', 'bot', 'message', 'new since', '[]', 3.0, 3.0);"
        "INSERT INTO reads(channel, seq) VALUES ('main', 2);")
    conn.commit()
    conn.close()
    store = wf.WebStore(path)
    store.upsert_channel("main", "ea", "main")
    assert store.channel("main")["unread_threads"] == {"t1": 1}
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


def test_the_owners_header_on_a_host_without_proc_gets_a_401_that_says_to_pair(tmp_path, monkeypatch):
    """peer_uid reads /proc/net/tcp*; a host without /proc (macOS) can never
    take the Tailscale header, so the owner's own login from tailscale serve
    was a bare 401 "not the owner" (2026-11-09). The body now carries a hint
    to pair by token; a request with no header, or on a host with /proc,
    gets the bare 401 as before, and pairing still signs the browser in."""
    monkeypatch.setattr(wf, "PROC_NET", (tmp_path / "no" / "tcp", tmp_path / "no" / "tcp6"))
    f = make(tmp_path, tailscaled=False)
    f.start()
    try:
        status, _h, body = owner(f, "GET", "/api/me")
        got = json.loads(body)
        assert status == 401 and got["error"] == "not the owner"
        assert "Linux only" in got.get("hint", "") and "pairing token" in got["hint"]
        assert got["hint"] == wf.HEADER_OFF_HINT
        # no header: no hint (nothing was refused that a pairing would fix differently)
        status, _h, body = call(f, "GET", "/api/me")
        assert status == 401 and json.loads(body) == {"error": "not the owner"}
        # another login's header: no hint either
        status, _h, body = call(f, "GET", "/api/me", headers={"Tailscale-User-Login": "x@example.test"})
        assert status == 401 and json.loads(body) == {"error": "not the owner"}
        # the token pairs the browser, and the cookie then signs it in
        token = f.pair_token()
        status, h, _b = call(f, "POST", "/api/pair", {"token": token})
        assert status == 200
        assert call(f, "GET", "/api/me", headers={"Cookie": f"ea_web={token}"})[0] == 200
    finally:
        f.stop()
    # with /proc present the peer check ran and said no: a bare 401
    (tmp_path / "proc").mkdir()
    for name in ("tcp", "tcp6"):
        (tmp_path / "proc" / name).write_text("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
    monkeypatch.setattr(wf, "PROC_NET", (tmp_path / "proc" / "tcp", tmp_path / "proc" / "tcp6"))
    f = make(tmp_path / "second", tailscaled=False)
    f.start()
    try:
        status, _h, body = owner(f, "GET", "/api/me")
        assert status == 401 and json.loads(body) == {"error": "not the owner"}
    finally:
        f.stop()


def test_a_send_with_a_nonce_is_taken_once_and_a_refused_hand_in_is_a_503(fe):
    """The browser retries a send the host did not answer, with the same
    nonce: the second post answers the first row and dispatches nothing.
    A hand-in the composite returns nothing for (the bot restarting) is a
    503, never a silent ok that loses the message."""
    got, ev = catcher(fe)
    body = {"text": "once please", "nonce": "n-1"}
    s1, _h, b1 = owner(fe, "POST", "/api/channels/main/messages", body)
    assert s1 == 200 and ev.wait(5)
    ev.clear()
    s2, _h, b2 = owner(fe, "POST", "/api/channels/main/messages", body)
    assert s2 == 200 and json.loads(b2)["id"] == json.loads(b1)["id"]
    assert not ev.wait(1) and len(got) == 1, "the retry dispatched nothing"
    s3, _h, b3 = owner(fe, "POST", "/api/channels/main/messages", {"text": "once please", "nonce": "n-2"})
    assert s3 == 200 and json.loads(b3)["id"] != json.loads(b1)["id"], "another nonce is another message"
    assert owner(fe, "POST", "/api/channels/main/messages", {"text": "x", "nonce": "n" * 65})[0] == 400
    assert owner(fe, "POST", "/api/channels/main/messages", {"text": "x", "nonce": 5})[0] == 400
    fe.inbound = lambda ev_: None
    try:
        status, _h, b = owner(fe, "POST", "/api/channels/main/messages", {"text": "lost?", "nonce": "n-3"})
    finally:
        fe.inbound = None
    assert status == 503 and "restarting" in json.loads(b)["error"]
    status, _h, b = owner(fe, "POST", "/api/channels/main/messages", {"text": "lost?", "nonce": "n-3"})
    assert status == 200, "the retry after the 503 is taken"


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
    assert got[-1].channel == "main" and not got[-1].is_dm  # Action requests takes taps only
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
    reply = json.loads(body)
    assert status == 200 and reply["ok"] is True and reply["trigger"].startswith("web:")
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


def test_a_card_click_carries_the_owners_line_only_for_an_action_that_asks(fe):
    got, ev = catcher(fe)
    ref = fe.post_card("dm", card(
        actions=[CardAction("approve_send", "Draft it", {"row": "r1"}, "primary", "draft",
                            ask="what the reply must say"),
                 CardAction("pick", "Skip", {"row": "r1"}, "", "skip")]))
    path = f"/api/cards/dm/{ref.id}/actions"
    shown = fe.store.message("dm", ref.id)["card"]              # what the page gets
    assert [a.get("ask", "") for a in shown["actions"]] == ["what the reply must say", ""]
    for body, want in [
            ({"action_id": "approve_send", "text": "  Say yes,\n after the 20th. "},
             "Say yes, after the 20th."),
            ({"action_id": "approve_send", "text": "x" * 500}, "x" * 300),
            ({"action_id": "approve_send"}, ""),
            ({"action_id": "approve_send", "text": ["not", "a", "line"]}, ""),
            ({"action_id": "pick", "text": "this action takes no line"}, "")]:
        ev.clear()
        assert owner(fe, "POST", path, body)[0] == 200
        assert ev.wait(5)
        assert (got[-1].action_id, got[-1].text, got[-1].value) == \
            (body["action_id"], want, {"row": "r1"})


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


def test_several_files_post_as_one_message_in_one_thread(fe, tmp_path, monkeypatch):
    """One request with several `file` parts: the first file's row carries
    the text, each other file follows it in the same thread, and the core
    gets one message with every file. A nonce sent again answers the same
    row and dispatches nothing; more than MAX_UPLOAD_FILES is refused."""
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    got, ev = catcher(fe)
    body, ctype = multipart([("text", "both of these"), ("nonce", "n-1"),
                             ("file", ("a.txt", "text/plain", b"first")),
                             ("file", ("b.pdf", "application/pdf", b"%PDF-1.4 second"))])
    status, _h, out = owner(fe, "POST", "/api/channels/main/files", raw=body,
                            headers={"Content-Type": ctype})
    row = json.loads(out)
    assert status == 200 and row["text"] == "both of these" and row["file"]["name"] == "a.txt"
    rows = json.loads(owner(fe, "GET", f"/api/channels/main/threads/{row['thread_key']}")[2])
    assert [(r["text"], r["file"]["name"], r["author"]) for r in rows] == \
        [("both of these", "a.txt", "owner"), ("", "b.pdf", "owner")]
    assert ev.wait(5)
    time.sleep(0.2)
    assert len(got) == 1
    msg = got[0]
    assert (msg.msg_id, msg.text, msg.subtype) == (row["id"], "both of these", "file_share")
    assert [f.name for f in msg.files] == ["a.txt", "b.pdf"]
    assert [owner(fe, "GET", "/api/files/" + f.id)[2] for f in msg.files] == [b"first", b"%PDF-1.4 second"]
    # The same nonce again (an answer lost on the way back): the same row, nothing new.
    status, _h, again = owner(fe, "POST", "/api/channels/main/files", raw=body,
                              headers={"Content-Type": ctype})
    assert status == 200 and json.loads(again)["id"] == row["id"]
    assert len(json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])) == 1
    time.sleep(0.2)
    assert len(got) == 1
    # A reply puts every file in the thread it names.
    body, ctype = multipart([("thread_key", row["thread_key"]), ("file", ("c.txt", "text/plain", b"c")),
                             ("file", ("d.txt", "text/plain", b"d"))])
    assert owner(fe, "POST", "/api/channels/main/files", raw=body, headers={"Content-Type": ctype})[0] == 200
    rows = json.loads(owner(fe, "GET", f"/api/channels/main/threads/{row['thread_key']}")[2])
    assert [r["file"]["name"] for r in rows] == ["a.txt", "b.pdf", "c.txt", "d.txt"]
    body, ctype = multipart([("file", (f"{n}.txt", "text/plain", b"x")) for n in range(wf.MAX_UPLOAD_FILES + 1)])
    status, _h, out = owner(fe, "POST", "/api/channels/main/files", raw=body, headers={"Content-Type": ctype})
    assert status == 400 and json.loads(out)["error"] == f"Send at most {wf.MAX_UPLOAD_FILES} files at once."


def test_parse_multipart_keeps_every_part_of_a_repeated_name():
    body, ctype = multipart([("file", ("a.txt", "text/plain", b"1")), ("text", "hi"),
                             ("file", ("b.txt", "text/plain", b"2"))])
    parts = wf.multipart_parts(ctype, body)
    assert [(n, p["filename"], p["data"]) for n, p in parts] == \
        [("file", "a.txt", b"1"), ("text", None, b"hi"), ("file", "b.txt", b"2")]
    assert parse_multipart(ctype, body)["file"]["filename"] == "b.txt"  # the last, as before


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
    # a list or a thread read by the app is not the owner reading: no mark moves
    chans = {c["id"]: c for c in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert chans["main"]["unread"] == 3 and chans["main"]["kind"] == "main"
    assert chans["main"]["unread_threads"] == {a.thread_key: 2, b.thread_key: 1}
    fe.post("main", "c")
    chans = {c["id"]: c for c in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert chans["main"]["unread"] == 4 and chans["main"]["as_of"] == fe.store.max_event_seq()


def test_encoded_channel_and_thread_ids_route(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        # ids as history imported from Zulip names them: a colon, percent escapes
        f.mirror_channel("stream:7", "ea", "main")
        key = "stream:7:topic:a%2Fb%20c"
        f.mirror_post(MessageRef("stream:7", "1", key), "x")
        q = urllib.parse.quote
        status, _h, body = owner(f, "GET", f"/api/channels/{q('stream:7', safe='')}/threads/"
                                           f"{q(key, safe='')}")
        assert status == 200 and json.loads(body)[0]["thread_key"] == key
    finally:
        f.stop()


def test_reading_a_thread_is_a_post_and_the_stream_tells_every_app(fe):
    a = fe.post("main", "a")
    fe.post("main", "a2", thread_ts=a.thread_key)
    b = fe.post("main", "b")
    first = fe.store.message("main", a.id)["seq"]
    path = f"/api/channels/main/threads/{a.thread_key}/read"
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        status, _h, body = owner(fe, "POST", path, {"upto": first})
        out = json.loads(body)
        assert status == 200 and (out["channel"], out["thread_key"], out["unread"]) == \
            ("main", a.thread_key, 2)
        assert out["unread_threads"] == {a.thread_key: 1, b.thread_key: 1}
        (ev,) = s.events(1)
        assert ev["event"] == "read" and json.loads(ev["data"]) == out
    finally:
        s.close()
    # no bound: the thread's newest message; a message's id names its thread
    out = json.loads(owner(fe, "POST", f"/api/channels/main/threads/{a.id}/read", {})[2])
    assert out["unread_threads"] == {b.thread_key: 1}
    chans = {c["id"]: c for c in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert (chans["main"]["unread"], chans["main"]["unread_threads"]) == (1, {b.thread_key: 1})
    for bad in ("x", True, -1, 1.5, None):
        assert owner(fe, "POST", path, {"upto": bad})[0] == (200 if bad is None else 400), bad
    assert owner(fe, "POST", "/api/channels/main/threads/t999/read", {})[0] == 404
    assert owner(fe, "POST", "/api/channels/nope/threads/t1/read", {})[0] == 404
    assert owner(fe, "GET", path)[0] == 404
    assert call(fe, "POST", path, {})[0] == 401
    assert owner(fe, "POST", path, {}, {"Origin": "https://evil.example.test"})[0] == 403


def test_a_relayed_device_that_may_chat_may_mark_a_thread_read(fe):
    """Reading a thread sends nothing: a chat action, which the relay client
    forwards from a device at "chat", as it does a message."""
    a = fe.post("main", "a")
    relay = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "deny"}
    path = f"/api/channels/main/threads/{a.thread_key}/read"
    status, _h, body = owner(fe, "POST", path, {}, relay)
    assert status == 200 and json.loads(body)["unread"] == 0
    from botcore.chat import relay_client
    assert relay_client.path_allowed(path)
    assert relay_client.access_allows("chat", "POST", path)


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
    fe.mirror_channel("C0EA", "ea", "main")
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        w = writer(fe)
        t0 = time.monotonic()
        w.mirror_post(MessageRef("C0EA", "501", "t-brief", 1000.0),
                      "*morning*")
        ev = s.events(1)[0]
        assert time.monotonic() - t0 < 2.5            # one tail_s read away
        row = json.loads(ev["data"])
        assert ev["event"] == "message" and int(ev["id"]) == w.store.max_event_seq()
        assert (row["id"], row["thread_key"], row["text"]) == \
            ("501", "t-brief", "**morning**")
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


def test_an_event_right_after_the_stream_subscribes_is_not_skipped(fe, monkeypatch):
    """The stream counts live events from its subscription. A watermark read
    after the first line went out took an event that landed in between for
    an old one and skipped it, so a client that acts as soon as it reads
    the first line could wait forever for its event."""
    store = fe.store
    real = store.subscribe

    def subscribe():
        sub = real()
        assert sub.start == store.max_event_seq()
        fe.post("main", "landed as the stream opened")
        return sub
    monkeypatch.setattr(store, "subscribe", subscribe)
    s = Stream(fe)
    try:
        seen: list[str] = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(x.startswith("data: ") for x in seen):
            seen.append(s.resp.fp.readline().decode().rstrip("\n"))
        data = [json.loads(x[6:]) for x in seen if x.startswith("data: ")]
        assert data and data[0]["text"] == "landed as the stream opened"
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


def test_a_message_typed_to_action_requests_starts_a_conversation_in_ask_tacitus(fe, tmp_path,
                                                                                 monkeypatch):
    """Action requests (the DM) takes button presses only. A message an
    older app posts there, top level or under a request, and an upload,
    start a new conversation in the EA channel: the bot's line opens it and
    names the request (its title, its first fields, a link), the owner's
    words follow, and the core reads them there. Nothing lands in the DM,
    and a retry with the same nonce posts once."""
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    got, ev = catcher(fe)
    req = fe.post_card("dm", card())
    status, _h, body = owner(fe, "POST", "/api/channels/dm/messages",
                             {"text": "make it shorter", "thread_key": req.thread_key, "nonce": "n-1"})
    row = json.loads(body)
    assert status == 200 and row["channel"] == "main" and row["author"] == "owner"
    assert row["text"] == "make it shorter" and row["thread_key"] != req.thread_key
    opener, typed = fe.store.thread("main", row["thread_key"])
    assert typed["id"] == row["id"] and opener["author"] == "bot"
    assert opener["text"].startswith("You wrote this about a request in Action requests: [Send this draft?](")
    assert "To: someone@example.test" in opener["text"] and f"#/dm/{req.thread_key}/{req.id}" in opener["text"]
    assert ev.wait(5)
    msg = got[-1]
    assert (msg.channel, msg.thread_key, msg.msg_id, msg.is_dm, msg.is_reply) == \
        ("main", row["thread_key"], row["id"], False, True)
    assert [m["id"] for m in fe.store.thread("dm", req.thread_key)] == [req.id]  # the request holds no words
    assert wf.is_conversation("main", "message", "bot", opener["text"], True)  # Recent finds it
    assert owner(fe, "POST", "/api/channels/dm/messages", {"text": "make it shorter",
                 "thread_key": req.thread_key, "nonce": "n-1"})[2] == body  # a retry: the same row
    assert len(fe.store.thread("main", row["thread_key"])) == 2
    # top level: a line that says why, then the words
    ev.clear()
    row = json.loads(owner(fe, "POST", "/api/channels/dm/messages", {"text": "what is on today?"})[2])
    opener = fe.store.thread("main", row["thread_key"])[0]
    assert opener["text"] == "You wrote this in Action requests, which takes taps only, so it starts here."
    assert ev.wait(5) and got[-1].channel == "main"
    # an upload goes the same way
    body, ctype = multipart({"text": "the figure", "file": ("fig.txt", "text/plain", b"bytes")})
    row = json.loads(owner(fe, "POST", "/api/channels/dm/files", raw=body,
                           headers={"Content-Type": ctype})[2])
    assert row["channel"] == "main" and row["kind"] == "file" and row["text"] == "the figure"
    assert not [m for m in fe.store.thread("dm", req.thread_key)[1:]]


def test_shadow_mode_takes_words_typed_to_the_dm_to_the_ea_channel(tmp_path):
    """In shadow mode the words go to the primary's EA channel as a new
    conversation, with no line of the web store's own."""
    f = make(tmp_path)
    handed: list = []

    def composite(ev):
        handed.append(ev)
        ref = MessageRef(ev.channel, str(9000 + len(handed)), ev.thread_key or "t-new", 5.0)
        f.mirror_post(ref, ev.text, "owner")
        return ref
    f.inbound = composite
    f.start()
    try:
        f.mirror_channel("C0EA", "ea", "main")
        f.mirror_channel("D0DM", "dm", "dm")
        status, _h, body = owner(f, "POST", "/api/channels/D0DM/messages", {"text": "hi"})
        assert status == 200 and json.loads(body)["channel"] == "C0EA"
        assert (handed[0].channel, handed[0].thread_key, handed[0].is_dm) == ("C0EA", None, False)
        assert f.store.thread("D0DM", "t-new") == []
    finally:
        f.stop()


def test_shadow_mode_hands_web_input_to_the_composite(tmp_path):
    f = make(tmp_path)
    handed: list = []

    def composite(ev):
        handed.append(ev)
        if isinstance(ev, InboundMessage):
            ref = MessageRef(ev.channel, str(9000 + len(handed)),
                             ev.thread_key or "t-new", 5.0)
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
        f.mirror_channel("C0EA", "ea", "main")
        status, _h, body = owner(f, "POST", "/api/channels/C0EA/messages", {"text": "hi"})
        row = json.loads(body)
        assert status == 200 and row["id"] == "9001" and row["author"] == "owner"
        assert isinstance(handed[0], InboundMessage) and handed[0].msg_id == ""
        assert handed[0].user_id == "owner" and handed[0].raw == {"source": "web"}
        body, ctype = multipart({"file": ("n.txt", "text/plain", b"bytes")})
        status, _h, out = owner(f, "POST", "/api/channels/C0EA/files", raw=body,
                                headers={"Content-Type": ctype})
        row = json.loads(out)
        assert status == 200 and row["file"]["id"] == "C0EA:9002"
        assert row["file"]["url"] == "/chat/api/files/C0EA%3A9002"
        # the composite kept no bytes; the frontend stored them under the row
        assert owner(f, "GET", "/api/files/C0EA%3A9002")[2] == b"bytes"
        assert not list((tmp_path / "priv" / "webchat-files" / "incoming").iterdir())
        cref = MessageRef("C0EA", "9100", "t-lock", 6.0)
        f.mirror_card(cref, card())
        assert owner(f, "POST", "/api/cards/C0EA/9100/actions",
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
        "the Send button is on the card in Action requests"
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
                        "backfill_days", "sched"}
    assert web["backfill_days"] == 7 and web["trust_tailscale_header"] is True
    assert web["sched"] is True
    assert ea_config.get_chat(tmp_path / "missing.json")["web"]["port"] == 8470


# ── the Settings screen (/api/settings/<name>) ─────────────────────────

sys.path.insert(0, str(ROOT / "tests"))
from plain_words import JARGON  # noqa: E402


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
    # rooms: yes unless the host says the install's mode has none
    assert json.loads(body)["rooms"] is True
    fe.rooms_enabled = lambda: False
    assert json.loads(owner(fe, "GET", "/api/me")[2])["rooms"] is False
    fe.rooms_enabled = lambda: 1 / 0
    assert json.loads(owner(fe, "GET", "/api/me")[2])["rooms"] is True
    fe.rooms_enabled = None
    assert owner(fe, "GET", path)[0] == 404  # no page until the host registers one
    fe.settings_pages["routing"] = page
    assert json.loads(owner(fe, "GET", "/api/me")[2])["settings"] == ["routing"]
    status, _h, body = owner(fe, "GET", path)
    data = json.loads(body)
    assert status == 200 and data["local"]["model"] == "local-27b"
    assert [r["task"] for r in data["rows"]][0] == "mail" and data["values"] == ["local", "hosted", "auto"]
    # the screen's words are plain (tests/test_model_routing.py scans every case)
    assert data["rows"][0]["label"] == "Reading your mail"
    shown = [data["local"]["detail"]] + [r[k] for r in data["rows"] for k in ("label", "what", "why", "floor")]
    assert not [t for t in shown if JARGON.search(t)]
    status, _h, body = owner(fe, "POST", path, {"tasks": {"briefing": "local", "drafting": "hosted"}})
    out = json.loads(body)
    assert status == 200 and out["saved"] is True
    assert {r["task"]: r["value"] for r in out["rows"]}["briefing"] == "local"
    saved = json.loads((priv / "routing.json").read_text())["tasks"]
    assert saved["briefing"] == "local" and saved["drafting"] == "hosted"
    assert (priv / "routing.json").stat().st_mode & 0o777 == 0o600
    assert owner(fe, "GET", "/api/settings/other")[0] == 404
    # the change log names the credential that signed the save in
    log = [json.loads(ln) for ln in (priv / "remote-llm" / "routing-changes.jsonl").read_text().splitlines()]
    assert log[-1]["via"] == "web" and log[-1]["credential"] == "header" and "device" not in log[-1]
    assert log[-1]["changed"]["briefing"] == ["auto", "local"]


def test_a_settings_save_hands_the_page_who_made_it(fe):
    """A page whose save takes `who` gets the credential (the pairing
    cookie here) and, on a relayed request, the device; a page without
    the parameter is called as before."""
    got = []

    class Page:
        def get(self):
            return {}

        def save(self, body, who=None):
            got.append(who)
            return {"saved": True}

    class Plain:
        def get(self):
            return {}

        def save(self, body):
            got.append("plain")
            return {"saved": True}
    fe.settings_pages.update(page=Page(), plain=Plain())
    token = fe.pair_token()
    cookie = {"Cookie": f"{wf.COOKIE}={token}", "Host": PUBLIC_HOST, "Origin": PUBLIC_ORIGIN}
    assert call(fe, "POST", "/api/settings/page", {"x": 1}, cookie)[0] == 200
    assert owner(fe, "POST", "/api/settings/plain", {"x": 1})[0] == 200
    assert got == [{"credential": "cookie"}, "plain"]


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
    assert not JARGON.search(json.loads(raw)["error"])
    assert owner(fe, "POST", path, raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400
    assert not (priv / "routing.json").exists()


def test_a_floored_row_is_refused_and_nothing_is_written(fe, routing_page, monkeypatch):
    page, priv = routing_page
    fe.settings_pages["routing"] = page
    said = []
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"review": "local"}})
    assert status == 400 and json.loads(raw)["error"] == \
        '"Checking its own work" always stays on Claude or Codex: these are judgment calls, so your own model never makes them.'
    said.append(json.loads(raw)["error"])
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"chat": "fable"}})
    assert status == 400 and json.loads(raw)["error"] == '"Talking with you" takes your own model, Claude or Codex, or Auto.'
    said.append(json.loads(raw)["error"])
    # the chat floor: talking with you stays off the owner's own model, in one plain line
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"chat": "local"}})
    assert status == 400 and json.loads(raw)["error"] == (
        "Your own model does not hold up in conversation yet. Talking with you and finding meeting "
        "times stay on Claude or Codex for now.")
    said.append(json.loads(raw)["error"])
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"other": 1})
    assert status == 400 and json.loads(raw)["error"] == \
        "Choose your own model, Claude or Codex, or Auto for at least one task."
    said.append(json.loads(raw)["error"])
    assert not (priv / "routing.json").exists()
    # the keep floor: hosted on mail is refused while the keep list has entries
    import remote_llm
    kept = {**remote_llm.config(), "keep_from_anthropic": {"domains": ["contractor.example.com"],
                                                            "addresses": [], "admissions": True}}
    monkeypatch.setattr(remote_llm, "config", lambda: kept)
    status, _h, raw = owner(fe, "POST", "/api/settings/routing", {"tasks": {"mail": "hosted"}})
    assert status == 400 and json.loads(raw)["error"] == \
        '"Reading your mail" cannot use Claude or Codex while you keep some mail away from hosted models.'
    said.append(json.loads(raw)["error"])
    assert not (priv / "routing.json").exists()
    status, _h, raw = owner(fe, "GET", "/api/settings/other")
    assert status == 404
    said.append(json.loads(raw)["error"])
    assert not [t for t in said if JARGON.search(t)]


@pytest.fixture
def notify_page(tmp_path, monkeypatch):
    """The Notifications page (web_notify_settings.NotifySettings) in an
    owner process whose private dir holds a chat.json with a made-up web
    block that turns room pushes off."""
    import owner_trust
    import web_notify_settings
    priv = tmp_path / "notify-priv"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    block = {"frontend": "web", "web": {"port": 8470, "tailscale_login": LOGIN,
                                        "notify_push": {"rooms": False, "extra": "kept"}}}
    (priv / "chat.json").write_text(json.dumps(block))
    (priv / "chat.json").chmod(0o600)
    return web_notify_settings, priv


def test_the_notify_page_reads_and_writes_the_notify_push_block(fe, notify_page, caplog):
    """GET is the settings in force; POST merges the keys it names over the
    web block's notify_push in chat.json (every other key kept, mode 0600),
    and the running front end pushes by the new ones at once."""
    mod, priv = notify_page
    fe.push_settings = wf.web_push_policy.Settings.from_config({"rooms": False})
    fe.settings_pages["notify"] = mod.NotifySettings(fe)
    assert json.loads(owner(fe, "GET", "/api/me")[2])["settings"] == ["notify"]
    status, _h, body = owner(fe, "GET", "/api/settings/notify")
    assert status == 200 and json.loads(body) == {"briefings": True, "replies": True,
                                                  "rooms": False, "quiet": "queue"}
    caplog.set_level("INFO", logger="chat.web")
    status, _h, body = owner(fe, "POST", "/api/settings/notify", {"replies": False, "quiet": "send"})
    assert status == 200 and json.loads(body) == {"briefings": True, "replies": False, "rooms": False,
                                                  "quiet": "send", "saved": True}
    assert fe.push_settings == wf.web_push_policy.Settings(replies=False, rooms=False, quiet="send")
    saved = json.loads((priv / "chat.json").read_text())
    assert saved["frontend"] == "web" and saved["web"]["port"] == 8470
    assert saved["web"]["notify_push"] == {"rooms": False, "extra": "kept", "replies": False,
                                           "quiet": "send"}
    assert (priv / "chat.json").stat().st_mode & 0o777 == 0o600
    assert any("notification settings saved (header)" in r.getMessage() for r in caplog.records)
    status, _h, body = owner(fe, "POST", "/api/settings/notify", {"rooms": True})
    assert json.loads(body)["rooms"] is True and fe.push_settings.rooms is True
    # a front end built from the file (a command-line sender's next run) reads the same
    web = json.loads((priv / "chat.json").read_text())["web"]
    assert WebFrontend.from_config(web, store_path=priv / "w.sqlite").push_settings == fe.push_settings


def test_the_notify_page_refuses_in_plain_words_and_never_from_the_relay(fe, notify_page, monkeypatch):
    mod, priv = notify_page
    fe.settings_pages["notify"] = mod.NotifySettings(fe)
    before = (priv / "chat.json").read_text()
    path = "/api/settings/notify"
    said = []
    for body, words in (({"rooms": "off"}, '"Rooms" takes on or off.'),
                        ({"quiet": "later"}, 'Quiet hours take "Hold until morning" or "Send anyway".'),
                        ({"sound": True}, "Tacitus has no notification setting named 'sound'."),
                        ({}, "Choose at least one notification setting to change.")):
        status, _h, raw = owner(fe, "POST", path, body)
        assert (status, json.loads(raw)["error"]) == (400, words), body
        said.append(words)
    # through the relay a phone may read the switches, never change them
    assert owner(fe, "GET", path, headers={"X-Tacitus-Relay": "1"})[0] == 200
    status, _h, raw = owner(fe, "POST", path, {"rooms": True}, {"X-Tacitus-Relay": "1"})
    assert status == 403 and "own web chat" in json.loads(raw)["error"]
    assert call(fe, "POST", path, {"rooms": True})[0] == 401
    assert owner(fe, "POST", path, {"rooms": True}, {"Origin": "https://evil.example.test"})[0] == 403
    assert (priv / "chat.json").read_text() == before
    # a process that is not the owner's, or a file it cannot read, saves nothing
    import owner_trust
    with monkeypatch.context() as m:
        m.setattr(owner_trust, "is_owner_process", lambda: False)
        status, _h, raw = owner(fe, "POST", path, {"rooms": True})
    assert status == 400 and json.loads(raw)["error"].startswith("Tacitus could not save")
    said.append(json.loads(raw)["error"])
    (priv / "chat.json").write_text("{not json")
    status, _h, raw = owner(fe, "POST", path, {"rooms": True})
    assert (status, json.loads(raw)["error"]) == (400, "Tacitus could not read its settings file.")
    said.append(json.loads(raw)["error"])
    assert fe.push_settings == wf.web_push_policy.Settings()
    assert not [t for t in said if JARGON.search(t)]


def test_the_bot_registers_the_notify_page_beside_the_routing_table():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert '_web.settings_pages["notify"] = _web_notify_settings.NotifySettings(_web)' in bot


# ── the state of a conversation (docs/webchat-state.md) ─────────────────

def _state(f, ch, key, state, by="rule", reason="r"):
    return f.store.set_thread_state(ch, key, state, by, reason,
                                    judged_seq=f.store.newest_seq(ch, key))


def test_threads_carry_their_state(fe):
    a = fe.post("main", "a")
    b = fe.post("main", "b")
    _state(fe, "main", b.thread_key, "needs_you", reason="The card still has its buttons.")
    rows = {r["thread_key"]: r for r in json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])}
    assert {k: rows[a.thread_key][k] for k in ("state", "state_by", "state_reason", "state_at",
                                               "stale")} == \
        {"state": "none", "state_by": None, "state_reason": "", "state_at": None, "stale": False}
    row = rows[b.thread_key]
    assert (row["state"], row["state_by"], row["state_reason"]) == \
        ("needs_you", "rule", "The card still has its buttons.")
    assert row["state_at"] == fe.store.thread_state("main", b.thread_key)["at"]


def test_needs_lists_what_waits_on_the_owner_needs_you_first(fe, monkeypatch):
    fe.create_private_room("ea-old")
    room = next(c["id"] for c in fe.store.channels() if c["name"] == "ea-old")
    t = {}
    for name, ch in (("w1", "main"), ("n1", "main"), ("d1", "dm"), ("w2", "dm"), ("n2", "dm"),
                     ("x1", room), ("done", "main"), ("none", "main")):
        t[name] = fe.post(ch, name).thread_key
    for name, state in (("w1", "waiting"), ("n1", "needs_you"), ("w2", "waiting"),
                        ("n2", "needs_you"), ("x1", "needs_you"), ("done", "done"),
                        ("none", "none")):
        ch = "main" if name in ("w1", "n1", "done", "none") else room if name == "x1" else "dm"
        _state(fe, ch, t[name], state)
    fe.archive(room)
    # a reply makes w1 the newest thread
    fe.post("main", "later", thread_ts=t["w1"])
    status, _h, body = owner(fe, "GET", "/api/needs")
    got = json.loads(body)["threads"]
    # n2 and w2 wait in Action requests, which the bot opened: they are its, not Needs you's
    assert status == 200 and [r["text"] for r in got] == ["n1", "w1"]
    first = got[0]
    assert first["channel"] == "main" and first["channel_name"] == "ea" and first["state"] == "needs_you"
    assert {"seq", "id", "thread_key", "reply_count", "last_at", "state_by", "state_reason",
            "state_at", "tap"} <= set(first)
    assert got[1]["reply_count"] == 1 and got[1]["channel_name"] == "ea"
    monkeypatch.setattr(wf, "NEEDS_CAP", 1)
    assert len(json.loads(owner(fe, "GET", "/api/needs")[2])["threads"]) == 1
    assert call(fe, "GET", "/api/needs")[0] == 401


def test_the_owner_sets_a_thread_s_state_and_the_stream_says_so(fe):
    a = fe.post("main", "a")
    fe.post("main", "a2", thread_ts=a.thread_key)
    path = f"/api/threads/main/{a.thread_key}/state"
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        status, _h, body = owner(fe, "POST", path, {"state": "done"})
        row = json.loads(body)
        # the answer is the thread's row, as the threads route gives it
        assert status == 200 and (row["id"], row["thread_key"], row["reply_count"]) == \
            (a.id, a.thread_key, 1)
        assert (row["state"], row["state_by"], row["state_reason"]) == \
            ("done", "owner", "You marked this done.")
        assert row["channel"] == "main" and row["channel_name"] == "ea"
        listed = {r["thread_key"]: r for r in
                  json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])}[a.thread_key]
        assert {k: listed[k] for k in listed} == {k: row[k] for k in listed}
        ev = s.events(1)[0]
        assert ev["event"] == "thread_state" and int(ev["id"]) > 0
        assert json.loads(ev["data"]) == {"channel": "main", "thread_key": a.thread_key,
                                          "state": "done", "by": "owner",
                                          "reason": "You marked this done.",
                                          "at": row["state_at"], "stale": False, "tap": False}
    finally:
        s.close()
    stored = fe.store.thread_state("main", a.thread_key)
    assert stored["by"] == "owner" and stored["judged_seq"] == fe.store.newest_seq("main", a.thread_key)
    # each override is a labelled example beside the store: no text
    labels = fe.store_path.parent / wf.LABELS_FILE
    (line,) = [json.loads(x) for x in labels.read_text().splitlines()]
    assert line == {"at": row["state_at"], "channel": "main", "thread_key": a.thread_key, "state": "done",
                    "previous_state": "none", "previous_by": None, "message_count": 2}
    assert labels.stat().st_mode & 0o777 == 0o600
    # a message id names its thread too
    assert json.loads(owner(fe, "POST", f"/api/threads/main/{a.id}/state",
                            {"state": "needs_you"})[2])["state"] == "needs_you"
    second = json.loads(labels.read_text().splitlines()[1])
    assert (second["state"], second["previous_state"], second["previous_by"]) == \
        ("needs_you", "done", "owner")
    for bad in ({"state": "working"}, {"state": "none"}, {"state": "later"}, {"state": 1}, {}):
        status, _h, raw = owner(fe, "POST", path, bad)
        assert status == 400 and "done, needs_you or waiting" in json.loads(raw)["error"], bad
    assert owner(fe, "POST", "/api/threads/main/t999/state", {"state": "done"})[0] == 404
    assert owner(fe, "POST", f"/api/threads/nope/{a.thread_key}/state", {"state": "done"})[0] == 404
    assert call(fe, "POST", path, {"state": "done"})[0] == 401
    assert owner(fe, "POST", path, {"state": "done"}, {"Origin": "https://evil.example.test"})[0] == 403
    assert owner(fe, "GET", path)[0] == 404


# ── Recent (docs/webchat.md, Recent) ─────────────────────────────────────

def _recent(f, query=""):
    status, _h, body = owner(f, "GET", "/api/recent" + query)
    assert status == 200, body
    return json.loads(body)


def _say(f, ch, text, key=None):
    """The owner's message, stored as the web route stores it (no dispatch)."""
    return f.store.add_message(ch, mid=None, thread_key=key, author="owner", kind="message", text=text)


def test_recent_lists_the_latest_conversations_of_every_place_newest_first(fe):
    fe.create_private_room("ea-trip")
    fe.create_private_room("ea-old")
    rooms = {c["name"]: c["id"] for c in fe.store.channels()}
    a = _say(fe, "main", "a")
    _say(fe, "sched", "s")
    _say(fe, rooms["ea-trip"], "r")
    _say(fe, rooms["ea-old"], "o")
    fe.archive(rooms["ea-old"])
    _say(fe, "dm", "d")  # typed in the DM before it took taps only: a conversation
    fe.post("main", "a2", thread_ts=a["thread_key"])  # a reply makes `a` the newest
    before = owner(fe, "GET", "/api/channels")[2]
    out = _recent(fe)
    assert [t["text"] for t in out["threads"]] == ["a", "d", "r", "s"]  # the archived room's left out
    first = out["threads"][0]
    assert (first["channel"], first["channel_name"], first["reply_count"], first["unread"],
            first["state"]) == ("main", "ea", 1, 1, "none")
    assert "needs_tap" not in first
    assert first["last_at"] == fe.store.thread("main", a["thread_key"])[-1]["posted_at"]
    assert [t["text"] for t in _recent(fe, "?limit=2")["threads"]] == ["a", "d"]
    for bad in ("0", "51", "x"):
        assert owner(fe, "GET", f"/api/recent?limit={bad}")[0] == 400
    assert owner(fe, "GET", "/api/channels")[2] == before  # a read of the list reads no conversation
    assert call(fe, "GET", "/api/recent")[0] == 401


def test_recent_holds_conversations_only_never_a_request(fe):
    """Recent lists a thread the owner wrote in and a briefing the bot
    opened. A card's thread (waiting or answered, read or unread, the owner
    typing in it too), a notice the bot opened, a pointer, a thread in
    Action requests the bot opened, a digest and a muted place's stay out."""
    waiting = fe.post_card("dm", card())
    answered = fe.post_card("dm", card(title="Create the review meeting?"))
    fe.replace_card_with_status(answered, ":white_check_mark: Created.")
    _say(fe, "dm", "make it shorter", waiting.thread_key)  # typed under a card, from an older app
    alert = fe.post("dm", "Your flight leaves at nine.")
    _say(fe, "dm", "thanks", alert.thread_key)  # a notice the owner answered in the DM: still not one
    notice = fe.post("main", "The calendar check found no clash.")
    brief = fe.post("main", "*Morning briefing*\n\nTwo meetings today.")
    talk = _say(fe, "main", "Reply to the lab and say Tuesday works.")
    fe.post("main", ":lock: The Send button for this draft is in Action requests: [open it](#/dm/x/y)",
            thread_ts=talk["thread_key"])
    fe.store.upsert_channel("digests", "Mail digests", "digest")  # a digest place starts muted
    fe.post("digests", "a digest")
    fe.create_private_room("ea-quiet")
    quiet = next(c["id"] for c in fe.store.channels() if c["name"] == "ea-quiet")
    _say(fe, quiet, "a muted room's talk")
    fe.store.set_channel(quiet, muted=True)
    got = [(t["channel"], t["thread_key"]) for t in _recent(fe)["threads"]]
    assert got == [("main", talk["thread_key"]), ("main", brief.thread_key)]
    fe.store.mark_thread_read("dm", waiting.thread_key)  # read or unread, a card stays out
    assert ("dm", waiting.thread_key) not in [(t["channel"], t["thread_key"]) for t in _recent(fe)["threads"]]
    _say(fe, "main", "Good.", notice.thread_key)  # the owner writes in the notice: now it is one
    assert _recent(fe)["threads"][0]["thread_key"] == notice.thread_key


@pytest.mark.parametrize("kind,first_kind,author,text,owner_in,want", [
    ("main", "message", "owner", "hi", True, True),
    ("sched", "message", "bot", "Finding a time.", True, True),
    ("room", "message", "bot", "*Morning briefing*\n\nx", False, False),  # a slice: not until answered
    ("room", "message", "bot", "*Radar quote and 2 more*\n\nx", True, True),
    ("sched", "message", "bot", "Morning briefing", False, False),
    ("main", "message", "bot", "Afternoon update: two items", False, True),
    ("main", "message", "bot", "A notice", False, False),
    ("main", "card", "bot", "", True, False),
    ("main", "status", "bot", "Sent.", True, False),
    ("dm", "message", "owner", "quick ask", True, True),
    ("dm", "message", "bot", "Your flight", True, False),
    ("dm", "card", "bot", "", True, False),
    ("digest", "message", "bot", "Morning briefing", False, False),
])
def test_is_conversation_is_the_rule_of_recent(kind, first_kind, author, text, owner_in, want):
    assert wf.is_conversation(kind, first_kind, author, text, owner_in) is want


def test_recent_leaves_out_the_health_room_while_debug_mode_hides_it(tmp_path):
    """While debug mode is off, Recent leaves out the hidden health room's
    conversations; turned on, they come back."""
    store = wf.WebStore(tmp_path / "priv" / "webchat.sqlite")
    store.upsert_channel("r5", "ea-health", "room")
    store.close()
    f = make(tmp_path, notify={"health": {"channel": "r5"}})
    f.start()
    try:
        _say(f, "main", "a")
        f.store.set_channel("r5", muted=False)  # muted by default; the hiding is what this tests
        report = f.post("r5", "The mail gate held two.")
        _say(f, "r5", "Which two?", report.thread_key)
        debug = {"on": False}
        f.debug_on = lambda: debug["on"]
        assert _hidden(f) == ["r5"]
        assert [t["channel"] for t in _recent(f)["threads"]] == ["main"]
        debug["on"] = True
        assert [t["channel"] for t in _recent(f)["threads"]] == ["r5", "main"]
    finally:
        f.stop()


def test_recent_is_one_snapshot_the_same_on_every_device(fe):
    """Two devices that ask with no event in between get the same answer,
    and its counts are the channel rows' counts at the same as_of."""
    a = fe.post("main", "a")
    _say(fe, "main", "a1", a.thread_key)
    _say(fe, "dm", "b")
    first, second = _recent(fe), _recent(fe)
    assert first == second and first["as_of"] == fe.store.max_event_seq()
    fe.post("main", "a2", thread_ts=a.thread_key)
    third = _recent(fe)
    assert third["as_of"] > first["as_of"] and third["threads"][0]["thread_key"] == a.thread_key
    rows = {r["id"]: r for r in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert rows["main"]["as_of"] == third["as_of"]
    for t in third["threads"]:
        assert t["unread"] == rows[t["channel"]]["unread_threads"].get(t["thread_key"], 0)
    read = json.loads(owner(fe, "POST", f"/api/channels/main/threads/{a.thread_key}/read", {})[2])
    after = _recent(fe)
    assert after["as_of"] >= read["as_of"] and after["threads"][0]["unread"] == 0


def test_the_owner_s_names_title_recent_threads_needs_and_search(fe):
    a = fe.post("main", "Budget question for the trip")
    _say(fe, "main", "And the hotel?", a.thread_key)
    _state(fe, "main", a.thread_key, "needs_you")
    fe.conversation_names = lambda: {a.thread_key: "  Trip \n  budget  ", "t999": "Elsewhere"}
    assert _recent(fe)["threads"][0]["title"] == "Trip budget"
    assert json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])[0]["title"] == "Trip budget"
    assert json.loads(owner(fe, "GET", "/api/needs")[2])["threads"][0]["title"] == "Trip budget"
    hit = json.loads(owner(fe, "GET", "/api/search?q=budget")[2])["results"][0]
    assert hit["thread_title"] == "Trip budget"

    def broken():
        raise OSError("the registry is away")
    fe.conversation_names = broken
    assert "title" not in _recent(fe)["threads"][0]  # the routes answer, with no names


def test_the_bot_names_conversations_in_the_web_chat():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "_web.conversation_names = lambda: _conversation_names()" in bot
    assert "_NAMES_CACHE[0] = None" in bot  # a new name shows at the next read


def test_needs_and_threads_carry_stale_and_the_stream_says_so(fe):
    old = fe.post("main", "old question")
    new = fe.post("main", "new question")
    for ref in (old, new):
        _state(fe, "main", ref.thread_key, "needs_you", reason="A question waits.")
    # the shelf counts from the conversation: its last message is 8 days old
    fe.store._conn.execute("UPDATE messages SET posted_at=? WHERE thread_key=?",
                           (time.time() - 8 * 86400, old.thread_key))
    fe.store._conn.commit()
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        assert fe.store.mark_stale(time.time() - 7 * 86400) == 1
        ev = s.events(1)[0]
        assert ev["event"] == "thread_state"
        data = json.loads(ev["data"])
        assert (data["thread_key"], data["state"], data["stale"]) == (old.thread_key, "needs_you", True)
    finally:
        s.close()
    needs = {r["thread_key"]: r for r in json.loads(owner(fe, "GET", "/api/needs")[2])["threads"]}
    assert (needs[old.thread_key]["stale"], needs[new.thread_key]["stale"]) == (True, False)
    rows = {r["thread_key"]: r for r in json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])}
    assert (rows[old.thread_key]["stale"], rows[new.thread_key]["stale"]) == (True, False)
    # the owner's Mark done takes it off the shelf: the answer and the list say so
    row = json.loads(owner(fe, "POST", f"/api/threads/main/{old.thread_key}/state",
                           {"state": "done"})[2])
    assert (row["state"], row["stale"]) == ("done", False)
    assert [r["thread_key"] for r in json.loads(owner(fe, "GET", "/api/needs")[2])["threads"]] == \
        [new.thread_key]


def test_a_card_and_its_pointer_link_the_conversation_it_came_from(fe):
    ask = fe.post("main", "Draft a reply to the visiting speaker.")
    c = fe.post_card("dm", card(thread_ts=ask.thread_key, ea_channel="main"))
    # the pointer as the bot writes it, with this front end's own permalink
    fe.post("main", f":lock: The Send button for this draft is in your DM: "
                    f"<{fe.permalink('dm', c.id)}|open it>", thread_ts=ask.thread_key, unfurl=False)
    linked = fe.store.linked_cards("main", ask.thread_key)
    assert [(r["channel"], r["id"], r["kind"]) for r in linked] == [("dm", c.id, "card")]
    assert fe.store._one("SELECT COUNT(*) AS n FROM card_link")["n"] == 1   # one link, two sources
    assert fe.store.card_origins([("dm", c.id)]) == [("main", ask.thread_key)]
    fe.replace_card_with_status(c, ":white_check_mark: Sent.")
    assert fe.store.linked_cards("main", ask.thread_key)[0]["kind"] == "status"
    # a card asked for from the DM's own thread is no link
    dm_ask = fe.post("dm", "And one from here.")
    fe.post_card("dm", card(thread_ts=dm_ask.thread_key, ea_channel="dm"), thread_ts=dm_ask.thread_key)
    assert fe.store.linked_cards("dm", dm_ask.thread_key) == []


def test_the_nudge_posts_as_the_bot_in_the_web_store_alone(tmp_path):
    from chat.memory_frontend import MemoryFrontend
    from chat.shadow_frontend import ShadowFrontend
    primary = MemoryFrontend()
    web = WebFrontend({"port": 0}, store_path=tmp_path / "web" / "webchat.sqlite",
                      webui_dir=tmp_path / "webui", trusted_config=True)
    shadow = ShadowFrontend(primary, web)
    shadow.mirror_channels()
    states = web.enable_thread_state()
    top = shadow.post("C_MAIN", "Which room for the review?")
    posts = len(primary.posts)
    web.store.mark_read("C_MAIN")
    row = states.nudge("C_MAIN", top.thread_key, "Still need this? Reply here, or tap *Mark done*.")
    assert row["author"] == "bot" and row["thread_key"] == top.thread_key
    assert row["text"] == "Still need this? Reply here, or tap **Mark done**."   # as post() writes it
    assert len(primary.posts) == posts                     # the primary's thread is left alone
    assert web.store.channel("C_MAIN")["unread"] == 1      # a bot message, unread like any other
    assert web.store.nudge_ids("C_MAIN", top.thread_key) == {row["id"]}
    assert states.nudge("C_MAIN", top.thread_key, "again") is None
    assert web.store.post_nudge("C_MAIN", "no-such-thread", "x") is None


def test_needs_is_a_reserved_channel_id(fe):
    for cid in ("needs", "Needs"):
        with pytest.raises(ValueError, match="reserved"):
            fe.mirror_channel(cid, "x", "room")
    assert fe.store.channel("needs") is None
    # the Needs you view reads without moving a read mark
    fe.post("main", "unread")
    owner(fe, "GET", "/api/needs")
    assert {c["id"]: c for c in fe.store.channels()}["main"]["unread"] == 1


def test_a_relayed_device_that_may_chat_may_set_a_state(fe):
    """The override sends nothing and changes no mail: a chat action, not a
    card button, so a relay-origin device kept off the cards may take it."""
    a = fe.post("main", "a")
    relay = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "deny"}
    status, _h, body = owner(fe, "POST", f"/api/threads/main/{a.thread_key}/state",
                             {"state": "waiting"}, relay)
    assert status == 200 and json.loads(body)["state"] == "waiting"
    c = fe.post_card("dm", card())
    assert owner(fe, "POST", f"/api/cards/dm/{c.id}/actions", {"action_id": "approve_send"},
                 relay)[0] == 403
    # the relay client forwards it from a device at "chat", as a message
    from botcore.chat import relay_client
    path = f"/api/threads/main/{a.thread_key}/state"
    assert relay_client.path_allowed(path)
    assert relay_client.access_allows("chat", "POST", path)
    assert not relay_client.access_allows("read", "POST", path)
    assert relay_client.path_allowed("/api/needs")
    assert relay_client.access_allows("read", "GET", "/api/needs")
    status, _h, body = owner(fe, "GET", "/api/needs", headers=relay)
    assert status == 200 and [r["state"] for r in json.loads(body)["threads"]] == ["waiting"]


# ── rooms: the owner renames, archives and brings back a room ─────────

def _new_events(f: WebFrontend, since: int) -> list[tuple[str, dict]]:
    return [(t, json.loads(p)) for _s, t, p in f.store.events_since(since)]



def test_a_room_has_a_color_the_owner_picks_from_the_grid(fe, tmp_path):
    """A new room gets one of the grid's first 24 colors at random; a room
    from a store before colors gets one at start (color_rooms). PATCH with
    a color alone sets one of ROOM_COLORS, in any case, and every open app
    hears it as a `channel` event; the same color again tells nobody. Any
    other color, a fixed place or an unknown place is refused in plain
    words, and a name in the same request is no color change. The color set
    here is a grey, which no new room gets at random, so the change is
    always a change."""
    got, _ev = catcher(fe)
    fe.create_private_room("ea-atlas")
    assert fe.store.channel("r1")["color"] in wf.ROOM_COLORS[:24]
    assert fe.store.channels()[[c["id"] for c in fe.store.channels()].index("r1")]["color"]
    path = "/api/channels/r1"
    assert call(fe, "PATCH", path, {"color": "#D6D5D5"})[0] == 401
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "PATCH", path, {"color": "#d6d5d5"})
    assert status == 200 and json.loads(body)["color"] == "#D6D5D5"
    assert [(t, e["id"], e["color"]) for t, e in _new_events(fe, since)] == [("channel", "r1", "#D6D5D5")]
    since = fe.store.max_event_seq()
    assert owner(fe, "PATCH", path, {"color": "#D6D5D5"})[0] == 200
    assert _new_events(fe, since) == []
    said = []
    for where, body, want in (("r1", {"color": "#123456"}, 400), ("r1", {"color": ""}, 400),
                              ("main", {"color": "#D6D5D5"}, 400), ("r9", {"color": "#D6D5D5"}, 404)):
        status, _h, raw = owner(fe, "PATCH", f"/api/channels/{where}", body)
        assert status == want, (where, body)
        said.append(json.loads(raw)["error"])
    assert said[:3] == ["Pick one of the colors in the room's menu."] * 2 + ["Only a room has a color."]
    assert not [e for e in said if JARGON.search(e)], said
    assert fe.store.channel("r1")["color"] == "#D6D5D5" and not got
    # a name and a color together is a rename, as before
    assert owner(fe, "PATCH", path, {"name": "atlas", "color": "#000000"})[0] == 200
    assert fe.store.channel("r1")["color"] == "#D6D5D5"
    # a room from before colors gets one at start
    fe.store._conn.execute("UPDATE channels SET color=NULL WHERE id='r1'")
    fe.store._conn.commit()
    assert fe.store.color_rooms() == 1 and fe.store.channel("r1")["color"] in wf.ROOM_COLORS[:24]
    assert fe.store.color_rooms() == 0
    assert not fe.store.channel("main")["color"]

def test_the_owner_renames_a_room_and_the_core_hears_it(fe):
    got, ev = catcher(fe)
    fe.create_private_room("ea-atlas", "the atlas project")
    fe.create_private_room("ea-lumen")
    path = "/api/channels/r1"
    # the owner only, from the app's own origin
    assert call(fe, "PATCH", path, {"name": "x"})[0] == 401
    assert owner(fe, "PATCH", path, {"name": "x"}, {"Tailscale-User-Login": "x@example.test"})[0] == 401
    for origin in ("https://evil.example.test", None):
        assert owner(fe, "PATCH", path, {"name": "x"}, {"Origin": origin})[0] == 403, origin
    assert fe.store.channel("r1")["name"] == "ea-atlas" and not got
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "PATCH", path, {"name": "  #atlas   2027 "})
    row = json.loads(body)
    assert status == 200 and (row["id"], row["name"], row["kind"]) == ("r1", "atlas 2027", "room")
    assert fe.store.channel("r1")["name"] == "atlas 2027"
    assert fe.store.channel("r1")["description"] == "the atlas project"
    assert [(t, e["name"]) for t, e in _new_events(fe, since)] == [("channel", "atlas 2027")]
    # the core hears it as a Slack rename: a channel_rename from the owner
    assert ev.wait(2)
    msg = got[-1]
    assert isinstance(msg, InboundMessage)
    assert (msg.channel, msg.subtype, msg.new_name, msg.user_id) == ("r1", "channel_rename",
                                                                     "atlas 2027", "owner")
    # the same name again changes nothing and tells nobody
    since = fe.store.max_event_seq()
    assert owner(fe, "PATCH", path, {"name": "atlas 2027"})[0] == 200
    assert _new_events(fe, since) == [] and len(got) == 1
    # refusals, each in plain words
    refused = []
    for body, want in (({"name": "EA-LUMEN"}, 409), ({"name": ""}, 400), ({"name": " # "}, 400),
                       ({"name": "x" * 61}, 400), ({"name": "a\u202eb"}, 400), ({"name": 7}, 400),
                       ({}, 400)):
        status, _h, out = owner(fe, "PATCH", path, body)
        assert status == want, (body, status, out)
        refused.append(json.loads(out)["error"])
    assert owner(fe, "PATCH", path, {"name": "y" * 60})[0] == 200
    for cid, want in (("main", 400), ("dm", 400), ("sched", 400), ("r9", 404)):
        status, _h, out = owner(fe, "PATCH", f"/api/channels/{cid}", {"name": "mine"})
        assert status == want, cid
        refused.append(json.loads(out)["error"])
    assert fe.store.channel("main")["name"] == "ea" and fe.store.channel("r2")["name"] == "ea-lumen"
    assert not [e for e in refused if JARGON.search(e)], refused
    assert "Another room has that name." in refused


def test_the_owner_archives_a_room_and_brings_it_back(fe):
    fe.create_private_room("ea-atlas")
    assert call(fe, "POST", "/api/channels/r1/archive")[0] == 401
    assert owner(fe, "POST", "/api/channels/r1/archive", headers={"Origin": None})[0] == 403
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "POST", "/api/channels/r1/archive")
    assert status == 200 and json.loads(body)["archived"] is True
    assert [(t, e["archived"]) for t, e in _new_events(fe, since)] == [("channel", True)]
    # the bot's room hygiene reads the flag; the room takes no new message
    assert fe.archived_map()["r1"] is True
    status, _h, body = owner(fe, "POST", "/api/channels/r1/messages", {"text": "hi"})
    assert status == 400 and json.loads(body)["error"] == (
        "This room is archived. Bring it back from its menu to write here.")
    # archiving it again answers the row as it is, with no event
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "POST", "/api/channels/r1/archive")
    assert status == 200 and json.loads(body)["archived"] is True
    assert _new_events(fe, since) == []
    status, _h, body = owner(fe, "POST", "/api/channels/r1/unarchive")
    assert status == 200 and json.loads(body)["archived"] is False
    assert fe.archived_map()["r1"] is False
    assert [t for t, _e in _new_events(fe, since)] == ["channel"]
    # the fixed places refuse
    for cid, want in (("main", 400), ("dm", 400), ("sched", 400), ("r9", 404)):
        assert owner(fe, "POST", f"/api/channels/{cid}/archive")[0] == want, cid
    assert not fe.store.channel("main")["archived"] and not fe.store.channel("dm")["archived"]


def test_the_owner_mutes_a_channel_and_the_ea_channel_and_the_dm_refuse(fe):
    fe.create_private_room("ea-atlas")
    assert fe.store.channel("r1")["muted"] is False
    assert call(fe, "POST", "/api/channels/r1/mute")[0] == 401
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "POST", "/api/channels/r1/mute")
    assert status == 200 and json.loads(body)["muted"] is True
    assert [(t, e["muted"]) for t, e in _new_events(fe, since)] == [("channel", True)]
    # muting it again answers the row as it is, with no event
    since = fe.store.max_event_seq()
    assert json.loads(owner(fe, "POST", "/api/channels/r1/mute")[2])["muted"] is True
    assert _new_events(fe, since) == []
    # a rename or an archive keeps the mute
    fe.archive("r1")
    fe.unarchive("r1")
    assert fe.store.channel("r1")["muted"] is True
    status, _h, body = owner(fe, "POST", "/api/channels/r1/unmute")
    assert status == 200 and json.loads(body)["muted"] is False
    # Scheduling may be muted; the EA channel and the DM refuse in plain words
    assert owner(fe, "POST", "/api/channels/sched/mute")[0] == 200
    for cid, want in (("main", 400), ("dm", 400), ("r9", 404)):
        status, _h, out = owner(fe, "POST", f"/api/channels/{cid}/mute")
        assert status == want, cid
        assert not JARGON.search(json.loads(out)["error"])
    assert not fe.store.channel("main")["muted"] and not fe.store.channel("dm")["muted"]


def test_a_digest_channel_starts_muted_and_the_health_channel_once(tmp_path):
    """A new digest channel starts muted. The health channel the web block
    names (notify.health) is muted the first time the bot starts with it,
    and an unmute after that stays through a restart; the EA channel named
    as the health channel is never muted."""
    store = wf.WebStore(tmp_path / "priv" / "webchat.sqlite")
    assert store.upsert_channel("digests", "Mail digests", "digest")["muted"] is True
    assert store.upsert_channel("r5", "ea-health", "room")["muted"] is False
    store.close()
    f = make(tmp_path, notify={"health": {"channel": "r5"}})
    f.start()
    try:
        assert f.store.channel("r5")["muted"] is True
        f.store.set_channel("r5", muted=False)
    finally:
        f.stop()
    f = make(tmp_path, notify={"health": {"channel": "r5"}})
    f.start()
    try:
        assert f.store.channel("r5")["muted"] is False
        assert f.store.channel("digests")["muted"] is True
    finally:
        f.stop()
    f = make(tmp_path / "other", notify={"health": {"channel": "main"}})
    f.start()
    try:
        assert f.store.channel("main")["muted"] is False
    finally:
        f.stop()


def test_a_store_from_before_the_mute_mutes_its_digests(tmp_path):
    import sqlite3
    path = tmp_path / "old" / "webchat.sqlite"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE channels(id TEXT PRIMARY KEY, name TEXT, kind TEXT, archived INT DEFAULT 0, "
        "description TEXT DEFAULT '');"
        "INSERT INTO channels(id, name, kind) VALUES ('digests', 'Mail digests', 'digest');"
        "INSERT INTO channels(id, name, kind) VALUES ('r1', 'ea-atlas', 'room');")
    conn.commit()
    conn.close()
    store = wf.WebStore(path)
    assert store.channel("digests")["muted"] is True and store.channel("r1")["muted"] is False
    store.close()


def _hidden(f) -> list[str]:
    rows = json.loads(owner(f, "GET", "/api/channels")[2])
    return [r["id"] for r in rows if r.get("hidden")]


def test_debug_mode_off_hides_the_health_room_and_keeps_its_threads(tmp_path):
    """An existing store with the health room's reports: while the host's
    debug mode is off, /api/channels marks the room `hidden` and its threads
    stay; debug_changed() tells every open app with a `channel` event. With
    no host switch (botcore alone) or one that cannot be read, nothing is
    hidden, and a fixed place named as the health channel never is."""
    store = wf.WebStore(tmp_path / "priv" / "webchat.sqlite")
    store.upsert_channel("r5", "ea-health", "room")
    store.add_message("r5", mid=None, thread_key=None, author=wf.BOT, kind="message",
                      text=":broom: Reconcile: 3 rows in 1 group.")
    store.close()
    f = make(tmp_path, notify={"health": {"channel": "r5"}})
    f.start()
    try:
        assert f.diagnostic_channels() == {"r5"} and _hidden(f) == []
        debug = {"on": False}
        f.debug_on = lambda: debug["on"]
        assert _hidden(f) == ["r5"]
        status, _h, body = owner(f, "GET", "/api/channels/r5/threads")
        assert status == 200 and [t["text"] for t in json.loads(body)] == [
            ":broom: Reconcile: 3 rows in 1 group."]
        since = f.store.max_event_seq()
        debug["on"] = True
        f.debug_changed()
        assert [(t, e["id"], e["hidden"]) for t, e in _new_events(f, since)] == [
            ("channel", "r5", False)]
        assert _hidden(f) == []
        since = f.store.max_event_seq()
        debug["on"] = False
        f.debug_changed()
        assert [(t, e["id"], e["hidden"]) for t, e in _new_events(f, since)] == [
            ("channel", "r5", True)]
        f.debug_on = lambda: 1 / 0
        assert _hidden(f) == []
    finally:
        f.stop()
    f = make(tmp_path / "other", notify={"health": {"channel": "main"}})
    f.start()
    try:
        f.debug_on = lambda: False
        assert f.diagnostic_channels() == set() and _hidden(f) == []
    finally:
        f.stop()


def test_the_debug_page_saves_the_switch_and_tells_every_open_app(fe, notify_page, caplog):
    """GET is debug mode as chat.json says it (off for a block from before
    the switch); POST {"on": bool} writes the chat block's `diagnostics`,
    keeps every other key, and shows or hides the health room at once. A
    refusal is in plain words, and a relayed request changes nothing."""
    mod, priv = notify_page
    fe.cfg["notify"] = {"health": {"channel": "r5"}}
    fe.store.upsert_channel("r5", "ea-health", "room")
    fe.settings_pages["debug"] = mod.DebugSettings(fe)
    fe.debug_on = mod.debug_on
    assert "debug" in json.loads(owner(fe, "GET", "/api/me")[2])["settings"]
    path = "/api/settings/debug"
    assert json.loads(owner(fe, "GET", path)[2]) == {"on": False}
    assert _hidden(fe) == ["r5"]
    caplog.set_level("INFO", logger="chat.web")
    since = fe.store.max_event_seq()
    status, _h, body = owner(fe, "POST", path, {"on": True})
    assert status == 200 and json.loads(body) == {"on": True, "saved": True}
    saved = json.loads((priv / "chat.json").read_text())
    assert saved["diagnostics"] is True and saved["web"]["notify_push"]["extra"] == "kept"
    assert (priv / "chat.json").stat().st_mode & 0o777 == 0o600
    assert any("debug mode saved (header): on" in r.getMessage() for r in caplog.records)
    assert [(t, e["id"], e["hidden"]) for t, e in _new_events(fe, since)] == [("channel", "r5", False)]
    assert json.loads(owner(fe, "GET", path)[2]) == {"on": True} and _hidden(fe) == []
    assert json.loads(owner(fe, "POST", path, {"on": False})[2])["on"] is False
    assert _hidden(fe) == ["r5"]
    before = (priv / "chat.json").read_text()
    said = []
    for body in ({"on": "yes"}, {}, {"on": True, "extra": 1}):
        status, _h, raw = owner(fe, "POST", path, body)
        assert (status, json.loads(raw)["error"]) == (400, "Debug mode takes on or off."), body
        said.append(json.loads(raw)["error"])
    status, _h, raw = owner(fe, "POST", path, {"on": True}, {"X-Tacitus-Relay": "1"})
    assert status == 403 and "own web chat" in json.loads(raw)["error"]
    assert call(fe, "POST", path, {"on": True})[0] == 401
    assert (priv / "chat.json").read_text() == before
    assert not [t for t in said if JARGON.search(t)]



# ── a reply's agent and model details (docs/webchat.md) ───────────────

def _scheduler_trace(tool_calls):
    """ea-chat-bot.py's own _format_scheduler_trace, run out of its helper
    block (as tests/test_scheduler_trace.py does: the module pulls in the
    whole bot at load)."""
    import re as _re
    import tempfile
    import types
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_SCHEDULER_TOOL_PATTERNS = [")
    block = src[start:src.index("\n\n\n# Track in-flight requests", start)]
    ns = {"re": _re, "json": json, "Path": Path, "EA_DIR": Path(tempfile.mkdtemp(prefix="ea-test-details-")),
          "log": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)}
    exec(compile(block, "ea-chat-bot.py", "exec"), ns)
    return ns["_format_scheduler_trace"](tool_calls)


def test_a_reply_marks_where_its_agent_and_model_details_begin(fe):
    """The usage line the bot appends to a reply, its rate-limit warning and
    the scheduler's tool trace, as the functions that write them write them
    (either order, or the trace alone), are the reply's details:
    `details_at` is where they begin, in the app's string index (UTF-16, so
    an emoji before it counts twice). The owner's words, a card, a reply
    with a rule of its own and a reply with no details carry none."""
    from claude_usage import format_usage_footer
    body = "Tuesday works \U0001F4C5 for the review.\n\n---\nOne more thing: _EA_ is our lab's name."
    cut = len(body.encode("utf-16-le")) // 2
    usage = format_usage_footer({"session_percent_used": 10, "session_reset": "",
                                 "daily_percent_used": 12}, 45.2, label="EA \u00b7 opus")
    plain = format_usage_footer({"error": "fetch failed"}, 11, label="EA resident")
    warned = plain + "\n_:warning: Rate limit near: 90% of the session used_"
    cmd = "python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting --duration 30"
    trace = _scheduler_trace([("Bash", 1.7, "", '{"status":"ok","options":[]}',
                               {"command": cmd, "description": "find times"})])
    assert trace and trace.startswith("\u2500")
    for text, want in ((body + usage, cut), (body + warned, cut), (body + usage + "\n\n" + trace, cut),
                       (body + "\n\n" + trace, cut), (trace, 0), (body, None), (usage.strip(), 0)):
        assert wf.details_at(text) == want, text
        key = fe.post("main", text).thread_key
        got = json.loads(owner(fe, "GET", f"/api/channels/main/threads/{key}")[2])[0]
        assert got.get("details_at") == want, text
    mine = fe.store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                                text=body + usage)
    assert "details_at" not in mine


def test_the_details_page_saves_the_switch_off_by_default(fe, notify_page, caplog):
    """GET is the web block's `show_details` (off for a block from before
    the switch); POST {"on": bool} writes it and keeps every other key. A
    refusal is in plain words; a relayed request reads it and changes
    nothing. The bot registers the page beside debug mode."""
    mod, priv = notify_page
    fe.settings_pages["details"] = mod.DetailsSettings()
    assert "details" in json.loads(owner(fe, "GET", "/api/me")[2])["settings"]
    path = "/api/settings/details"
    assert json.loads(owner(fe, "GET", path)[2]) == {"on": False}
    caplog.set_level("INFO", logger="chat.web")
    status, _h, body = owner(fe, "POST", path, {"on": True})
    assert status == 200 and json.loads(body) == {"on": True, "saved": True}
    saved = json.loads((priv / "chat.json").read_text())
    assert saved["web"]["show_details"] is True and saved["web"]["notify_push"]["extra"] == "kept"
    assert (priv / "chat.json").stat().st_mode & 0o777 == 0o600
    assert any("model details saved (header): on" in r.getMessage() for r in caplog.records)
    assert json.loads(owner(fe, "GET", path, headers={"X-Tacitus-Relay": "1"})[2]) == {"on": True}
    before = (priv / "chat.json").read_text()
    said = []
    for body in ({"on": "yes"}, {}, {"on": True, "extra": 1}):
        status, _h, raw = owner(fe, "POST", path, body)
        assert (status, json.loads(raw)["error"]) == (400, "Show agent and model details takes on or off.")
        said.append(json.loads(raw)["error"])
    status, _h, raw = owner(fe, "POST", path, {"on": False}, {"X-Tacitus-Relay": "1"})
    assert status == 403 and "own web chat" in json.loads(raw)["error"]
    assert (priv / "chat.json").read_text() == before
    assert not [t for t in said if JARGON.search(t)]
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert '_web.settings_pages["details"] = _web_notify_settings.DetailsSettings()' in bot

def test_a_relayed_device_that_may_chat_may_rename_and_archive(fe):
    """Room admin changes no mail and sends nothing: a chat action, which a
    device the relay lets chat may take, as a message."""
    from botcore.chat import relay_client
    fe.create_private_room("ea-atlas")
    relay = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "deny"}
    assert owner(fe, "PATCH", "/api/channels/r1", {"name": "atlas"}, relay)[0] == 200
    assert owner(fe, "POST", "/api/channels/r1/archive", headers=relay)[0] == 200
    assert fe.store.channel("r1")["name"] == "atlas" and fe.store.channel("r1")["archived"]
    for method, path in (("PATCH", "/api/channels/r1"), ("POST", "/api/channels/r1/archive"),
                         ("POST", "/api/channels/r1/unarchive")):
        assert relay_client.path_allowed(path)
        assert relay_client.access_allows("chat", method, path)
        assert not relay_client.access_allows("read", method, path)


def _shadow(tmp_path, primary):
    """A composite over `primary` with this web front end as its shadow,
    started, with one room the primary holds and the shadow copies."""
    from chat.shadow_frontend import ShadowFrontend
    web = make(tmp_path)
    composite = ShadowFrontend(primary, web)
    room = primary.create_private_room("ea-atlas")
    composite.start()
    return composite, web, room.id


def test_shadow_mode_renames_and_archives_the_room_on_the_primary(tmp_path):
    from chat.memory_frontend import MemoryFrontend

    class Renames(MemoryFrontend):
        name = "renamer"

        def rename(self, channel, name):
            if any(i.name == name for c, i in self.rooms.items() if c != channel):
                raise RoomNameTaken(name)
            self._room(channel).name = name
            self.room_ops.append({"op": "rename", "channel": channel, "name": name})

    primary = Renames()
    seen = []
    primary.registry.event("message")(seen.append)
    composite, web, cid = _shadow(tmp_path, primary)
    try:
        enc = urllib.parse.quote(cid, safe="")
        assert web.store.channel(cid)["kind"] == "room"
        status, _h, body = owner(web, "PATCH", f"/api/channels/{enc}", {"name": "atlas 2027"})
        assert status == 200 and json.loads(body)["name"] == "atlas 2027"
        assert primary.rooms[cid].name == "atlas 2027"
        assert web.store.channel(cid)["name"] == "atlas 2027"
        # the core hears the rename from the primary's owner, under the primary's id
        assert [(m.channel, m.subtype, m.new_name, m.user_id) for m in seen] == [
            (cid, "channel_rename", "atlas 2027", primary.owner_id)]
        primary.create_private_room("ea-lumen")
        status, _h, body = owner(web, "PATCH", f"/api/channels/{enc}", {"name": "ea-lumen"})
        assert status == 409 and web.store.channel(cid)["name"] == "atlas 2027"
        status, _h, body = owner(web, "POST", f"/api/channels/{enc}/archive")
        assert status == 200 and json.loads(body)["archived"] is True
        assert primary.rooms[cid].is_archived and web.store.channel(cid)["archived"]
        # archived on the primary by someone else first: the row follows, no error
        status, _h, body = owner(web, "POST", f"/api/channels/{enc}/unarchive")
        assert status == 200 and not primary.rooms[cid].is_archived
        primary.archive(cid)
        status, _h, body = owner(web, "POST", f"/api/channels/{enc}/archive")
        assert status == 200 and json.loads(body)["archived"] is True
        # a refusal of the primary's leaves the room as it was
        primary.unarchive = lambda channel: (_ for _ in ()).throw(ChatError("server error"))
        status, _h, body = owner(web, "POST", f"/api/channels/{enc}/unarchive")
        assert status == 502 and web.store.channel(cid)["archived"]
        assert not JARGON.search(json.loads(body)["error"])
    finally:
        web.stop()


def test_shadow_mode_refuses_a_rename_the_primary_cannot_make(tmp_path):
    from chat.memory_frontend import MemoryFrontend

    class Slackish(MemoryFrontend):
        name = "slack"

    primary = Slackish()
    seen = []
    primary.registry.event("message")(seen.append)
    composite, web, cid = _shadow(tmp_path, primary)
    try:
        enc = urllib.parse.quote(cid, safe="")
        status, _h, body = owner(web, "PATCH", f"/api/channels/{enc}", {"name": "atlas"})
        error = json.loads(body)["error"]
        assert status == 400 and error == ("Tacitus cannot rename a room in Slack. "
                                           "Rename it in Slack itself.")
        assert not JARGON.search(error)
        assert primary.rooms[cid].name == "ea-atlas" and web.store.channel(cid)["name"] == "ea-atlas"
        assert seen == []
        # archiving needs no rename: it goes through
        assert owner(web, "POST", f"/api/channels/{enc}/archive")[0] == 200
        assert primary.rooms[cid].is_archived
    finally:
        web.stop()


# the owner's own actions: search, reactions, edit and delete


def _search_store(tmp_path, monkeypatch, mode):
    """A started frontend whose store searches by `mode` (fts5 or like)."""
    if mode == "like":
        monkeypatch.setattr(wf, "fts5_available", lambda conn: False)
    f = make(tmp_path)
    f.start()
    if f.store.search_mode != mode:
        f.stop()
        pytest.skip("this SQLite has no FTS5")
    return f


@pytest.mark.parametrize("mode", ["fts5", "like"])
def test_search_finds_every_word_newest_first_in_either_mode(tmp_path, monkeypatch, mode):
    f = _search_store(tmp_path, monkeypatch, mode)
    try:
        a = f.post("main", "Weekly plan: the budget table is due Friday.")
        b = f.post("main", "Budget " + "filler words " * 40 + "the travel table is here", thread_ts=a.thread_key)
        c = f.post_card("dm", card(title=":lock: Send the budget table?", fallback_text=""))
        f.post("dm", "Nothing about money here.")
        owner_row = f.store.add_message("dm", mid=None, thread_key=None, author="owner", kind="message",
                                        text="Where is the budget table?")
        hits = f.store.search("budget table")
        assert [h["id"] for h in hits] == [owner_row["id"], c.id, b.id, a.id]  # a card's text is its title
        assert set(hits[0]) == {"channel", "channel_name", "thread_key", "thread_title", "id", "author",
                                "kind", "posted_at", "snippet"}
        assert hits[1]["thread_title"] == ":lock: Send the budget table?"  # a card names its conversation
        assert hits[2]["thread_title"] == "Weekly plan: the budget table is due Friday."  # a reply, by its first
        assert hits[2]["channel_name"] == "ea" and hits[0]["author"] == "owner"
        long = hits[2]["snippet"]
        assert len(long) <= 160 and long.startswith("Budget") and long.endswith("\u2026")
        assert [h["id"] for h in f.store.search("travel table")] == [b.id]
        mid = f.store.search("travel")[0]["snippet"]
        assert mid.startswith("\u2026") and "travel" in mid and len(mid) <= 160
        assert [h["id"] for h in f.store.search("budget", channel="dm")] == [owner_row["id"], c.id]
        assert len(f.store.search("budget", limit=1)) == 1
        assert f.store.search("budget missingword") == []
        # An edit is found by its new words alone; a removed message is not found.
        f.store.owner_edit("dm", owner_row["id"], "Where is the travel plan?")
        assert owner_row["id"] not in [h["id"] for h in f.store.search("budget table")]
        assert [h["id"] for h in f.store.search("travel plan")] == [owner_row["id"]]
        f.store.owner_remove("dm", owner_row["id"])
        assert f.store.search("travel plan") == []
        f.update(a, "Weekly plan: the cost sheet is due Friday.")
        assert [h["id"] for h in f.store.search("cost sheet")] == [a.id]
        r = f.post("main", "The CAFÉ receipts are filed.")
        if mode == "fts5":  # a word matches the start of a word, in any case, with or without accents
            assert [h["id"] for h in f.store.search("cafe RECEI")] == [r.id]
            assert f.store.search("ceipts") == []
            assert f.store.search('" OR * NEAR(') == []  # the index's own syntax is plain words here
        else:  # anywhere in the text, in any ASCII case
            assert [h["id"] for h in f.store.search("CEIPTS")] == [r.id]
            assert f.store.search("%") == f.store.search("_") == []
    finally:
        f.stop()


def test_an_index_that_missed_rows_is_built_again_on_open(tmp_path):
    path = tmp_path / "priv" / "webchat.sqlite"
    store = wf.WebStore(path)
    if store.search_mode != "fts5":
        store.close()
        pytest.skip("this SQLite has no FTS5")
    store.upsert_channel("main", "ea", "main")
    store.add_message("main", mid=None, thread_key=None, author="bot", kind="message", text="kept apple")
    store.close()
    import sqlite3
    conn = sqlite3.connect(path)  # a process without the index writes a row
    conn.execute("INSERT INTO messages(channel, id, thread_key, author, kind, text, posted_at, updated_at) "
                 "VALUES ('main', 'w9', 't9', 'bot', 'message', 'written apart', 1, 1)")
    conn.commit()
    conn.close()
    store = wf.WebStore(path)
    assert store.search_mode == "fts5"
    assert [h["id"] for h in store.search("apart")] == ["w9"]
    assert [h["id"] for h in store.search("apple")] == ["w1"]
    store.close()


def test_the_search_route_answers_the_owner_alone(fe):
    a = fe.post("main", "the budget table")
    fe.post("dm", "the budget sheet")
    status, _h, body = owner(fe, "GET", "/api/search?q=budget&limit=50")
    out = json.loads(body)
    assert status == 200 and out["mode"] == fe.store.search_mode
    assert [r["channel"] for r in out["results"]] == ["dm", "main"]
    out = json.loads(owner(fe, "GET", "/api/search?q=budget&channel=main")[2])
    assert [r["id"] for r in out["results"]] == [a.id]
    assert owner(fe, "GET", "/api/search?q=budget&channel=nope")[0] == 404
    assert owner(fe, "GET", "/api/search")[0] == 400
    assert owner(fe, "GET", "/api/search?q=%20")[0] == 400
    assert owner(fe, "GET", "/api/search?q=" + "x" * 201)[0] == 400
    assert owner(fe, "GET", "/api/search?q=x&limit=0")[0] == 400
    assert owner(fe, "GET", "/api/search?q=x&limit=201")[0] == 400
    assert call(fe, "GET", "/api/search?q=budget")[0] == 401
    # Through the relay: a GET, which a device that may only read may make.
    from botcore.chat import relay_client
    assert relay_client.path_allowed("/api/search?q=budget%20table&limit=50")
    assert relay_client.access_allows("read", "GET", "/api/search?q=budget")
    relay = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "deny"}
    assert owner(fe, "GET", "/api/search?q=budget", headers=relay)[0] == 200


def test_the_owner_reacts_apart_from_the_bot_s_marks(fe):
    ref = fe.post("main", "three times work")
    fe.react("main", ref.id, Status.EYES)
    before = fe.store.message("main", ref.id)
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        path = f"/api/messages/main/{ref.id}/react"
        status, _h, body = owner(fe, "POST", path, {"emoji": "eyes"})
        row = json.loads(body)
        assert status == 200 and row["owner_reactions"] == ["eyes"] and row["reactions"] == ["eyes"]
        ev = s.events(1)[0]
        assert ev["event"] == "reaction" and json.loads(ev["data"])["owner_reactions"] == ["eyes"]
        assert json.loads(owner(fe, "POST", path, {"emoji": "\U0001f44d"})[2])["owner_reactions"] == ["eyes", "+1"]
        assert json.loads(owner(fe, "POST", path, {"emoji": "\u2764"})[2])["owner_reactions"] == ["eyes", "+1", "heart"]
        assert json.loads(owner(fe, "POST", path, {"emoji": "eyes"})[2])["owner_reactions"] == ["+1", "heart"]
    finally:
        s.close()
    row = fe.store.message("main", ref.id)
    # The bot's marks, which the thread states read, and the edit time stay as they were.
    assert row["reactions"] == before["reactions"] and row["updated_at"] == before["updated_at"]
    # A link made before history was imported from Zulip names the channel's old id.
    fe.store.redirects["stream:9"] = "main"
    assert owner(fe, "POST", f"/api/messages/stream%3A9/{ref.id}/react", {"emoji": "joy"})[0] == 200
    assert fe.store.message("main", ref.id)["owner_reactions"] == ["+1", "heart", "joy"]
    assert owner(fe, "GET", "/api/search?q=three&channel=stream%3A9")[0] == 200
    assert owner(fe, "POST", path, {"emoji": "rocket"})[0] == 400
    assert owner(fe, "POST", path, {})[0] == 400
    assert owner(fe, "POST", "/api/messages/main/w999/react", {"emoji": "eyes"})[0] == 404
    assert call(fe, "POST", path, {"emoji": "eyes"})[0] == 401
    assert owner(fe, "POST", path, {"emoji": "eyes"}, {"Origin": "https://evil.example.test"})[0] == 403


def test_the_owner_edits_and_removes_only_their_own_message_in_its_first_15_minutes(fe):
    got, ev = catcher(fe)
    row = json.loads(owner(fe, "POST", "/api/channels/main/messages", {"text": "Plese send it"})[2])
    assert ev.wait(5)
    ev.clear()
    path = f"/api/messages/main/{row['id']}"
    s = Stream(fe)
    try:
        s.lines(lambda seen: "retry: 3000" in seen)
        status, _h, body = owner(fe, "PATCH", path, {"text": "Please send it"})
        new = json.loads(body)
        assert status == 200 and new["text"] == "Please send it" and new["updated_at"] > new["posted_at"]
        e = s.events(1)[0]
        assert e["event"] == "update" and json.loads(e["data"])["text"] == "Please send it"
    finally:
        s.close()
    stored = fe.store._one("SELECT edits FROM messages WHERE id=?", (row["id"],))
    assert [x["text"] for x in json.loads(stored["edits"])] == ["Plese send it"]
    # The bot's later turns read the thread as it is now; the edit dispatches nothing.
    assert [m.text for m in fe.thread_replies("main", row["thread_key"])] == ["Please send it"]
    assert not ev.wait(0.3) and len(got) == 1
    assert owner(fe, "PATCH", path, {"text": ""})[0] == 400
    assert owner(fe, "PATCH", path, {"text": "x" * 20001})[0] == 400
    assert owner(fe, "PATCH", path, {"text": "x"}, {"Origin": None})[0] == 403
    assert call(fe, "PATCH", path, {"text": "x"})[0] == 401
    # Only the owner's own text message.
    bot = fe.post("main", "the bot's words")
    assert owner(fe, "PATCH", f"/api/messages/main/{bot.id}", {"text": "x"})[0] == 403
    assert owner(fe, "DELETE", f"/api/messages/main/{bot.id}")[0] == 403
    assert owner(fe, "PATCH", "/api/messages/main/w999", {"text": "x"})[0] == 404
    up = fe.store.add_message("main", mid=None, thread_key=None, author="owner", kind="file", text="",
                              file={"id": "f1", "name": "a.txt", "url": "", "mimetype": "text/plain", "size": 1})
    assert owner(fe, "DELETE", f"/api/messages/main/{up['id']}")[0] == 400
    # Past 15 minutes: a plain refusal.
    old = fe.store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                               text="old words", posted_at=time.time() - 16 * 60)
    status, _h, body = owner(fe, "PATCH", f"/api/messages/main/{old['id']}", {"text": "new"})
    assert status == 409 and json.loads(body)["error"] == "You can change a message only in its first 15 minutes."
    status, _h, body = owner(fe, "DELETE", f"/api/messages/main/{old['id']}")
    assert status == 409 and "remove a message only in its first 15 minutes" in json.loads(body)["error"]
    # Removed: the row stays, with no text, no history and no reactions of the owner's.
    owner(fe, "POST", f"{path}/react", {"emoji": "heart"})
    status, _h, body = owner(fe, "DELETE", path)
    gone = json.loads(body)
    assert status == 200 and gone["deleted"] and gone["text"] == "" and gone["owner_reactions"] == []
    assert fe.store._one("SELECT edits FROM messages WHERE id=?", (row["id"],))["edits"] is None
    assert owner(fe, "DELETE", path)[0] == 200  # once more: as it is
    assert owner(fe, "PATCH", path, {"text": "back"})[0] == 409
    assert owner(fe, "POST", f"{path}/react", {"emoji": "heart"})[0] == 409
    # A later change from elsewhere (a mirror) writes no text back.
    fe.mirror_update(MessageRef("main", row["id"], row["thread_key"]), "resurrected")
    assert fe.store.message("main", row["id"])["text"] == ""
    # A device the relay lets chat may edit, as it may post; one that only reads may not.
    from botcore.chat import relay_client
    assert relay_client.access_allows("chat", "PATCH", path) and relay_client.access_allows("chat", "DELETE", path)
    assert not relay_client.access_allows("read", "PATCH", path)
    assert not relay_client.access_allows("read", "DELETE", path)
    assert {"PATCH", "DELETE"} <= set(relay_client.METHODS)


def test_shadow_mode_keeps_the_owner_s_changes_in_the_web_store(tmp_path):
    """The composite takes messages and clicks only: an edit, a removal and
    a reaction change the web store and reach nothing else."""
    f = make(tmp_path)
    handed: list = []

    def composite(ev):
        handed.append(ev)
        ref = MessageRef(ev.channel, "9001", ev.thread_key or "t-new", time.time())
        f.mirror_post(ref, ev.text, "owner")
        return ref
    f.inbound = composite
    f.start()
    try:
        f.mirror_channel("C0EA", "ea", "main")
        owner(f, "POST", "/api/channels/C0EA/messages", {"text": "hi"})
        assert owner(f, "PATCH", "/api/messages/C0EA/9001", {"text": "hello"})[0] == 200
        assert owner(f, "POST", "/api/messages/C0EA/9001/react", {"emoji": "joy"})[0] == 200
        assert owner(f, "DELETE", "/api/messages/C0EA/9001")[0] == 200
        assert len(handed) == 1 and f.store.message("C0EA", "9001")["deleted"]
    finally:
        f.stop()



# ── Action requests (docs/webchat.md, Action requests) ───────────────────

def test_action_requests_counts_the_cards_that_wait_and_needs_you_leaves_them_to_it(fe):
    """The DM's row counts the cards whose buttons still act (`waiting`),
    and its threads rows each theirs: never an answered card, an undo note,
    a card past the registry's age or a plain message. Needs you leaves out
    what waits on a tap: a request in the DM, and a conversation whose only
    wait is its request; the owner's own Needs me stays."""
    from dataclasses import asdict
    from chat import thread_state as ts
    live = fe.post_card("dm", card())
    done = fe.post_card("dm", card(title="Create the review meeting?"))
    fe.replace_card_with_status(done, ":white_check_mark: Created.")
    note = fe.post_card("dm", card(title="Closed at once", undo=True))
    old = fe.store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card",
                               text="Old", card=asdict(card()), posted_at=time.time() - 8 * 86400)
    fe.post("dm", "Your flight leaves at nine.")
    rows = {r["id"]: r for r in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert rows["dm"]["waiting"] == 1 and "waiting" not in rows["main"]
    assert rows["dm"]["unread"] == 5  # unread messages are still counted apart
    per = {t["thread_key"]: t["waiting"] for t in json.loads(owner(fe, "GET", "/api/channels/dm/threads")[2])}
    assert per[live.thread_key] == 1
    assert per[done.thread_key] == per[note.thread_key] == per[old["thread_key"]] == 0
    talk = _say(fe, "main", "Reply to the lab and say Tuesday works.")
    other = _say(fe, "main", "Which budget line is it?")
    _state(fe, "main", talk["thread_key"], "needs_you", reason=ts.CARD_WAITS)
    _state(fe, "main", other["thread_key"], "needs_you", by="model", reason="A question waits for you.")
    _state(fe, "dm", live.thread_key, "needs_you", reason='The card "Send this draft?" still has its buttons.')
    needs = json.loads(owner(fe, "GET", "/api/needs")[2])["threads"]
    assert [t["thread_key"] for t in needs] == [other["thread_key"]]
    rows = {t["thread_key"]: t for t in json.loads(owner(fe, "GET", "/api/channels/main/threads")[2])}
    assert rows[talk["thread_key"]]["tap"] is True and rows[other["thread_key"]]["tap"] is False
    # the owner's own Needs me is a conversation that needs the owner, whatever its card
    owner(fe, "POST", f"/api/threads/main/{talk['thread_key']}/state", {"state": "needs_you"})
    needs = json.loads(owner(fe, "GET", "/api/needs")[2])["threads"]
    assert {t["thread_key"] for t in needs} == {talk["thread_key"], other["thread_key"]}


def test_each_front_end_names_the_place_where_cards_wait():
    """One source names the place: the front end's prompt strings
    (`card_place`). The web chat says Action requests in its pointer, its
    prompts and its states; Slack keeps "your DM"."""
    from chat.slack_frontend import SlackFrontend
    from chat import thread_state as ts
    web, slack = WebFrontend.PROMPT_STRINGS, SlackFrontend.PROMPT_STRINGS
    assert web["card_place"] == ts.CARD_PLACE == "Action requests"
    assert slack["card_place"] == SLACK_LIKE_PROMPT_STRINGS["card_place"] == "your DM"
    assert "Action requests" in web["confirm_location"] and "your DM" not in web["confirm_location"]
    assert "Action requests" in web["confirm_sentence"] and "Action requests" in web["confirm_sentence_wrapped"]
    assert "your DM" in slack["confirm_location"] and "Action requests" not in " ".join(slack.values())
    assert ts.CARD_WAITS == "Its request in Action requests waits for you." and ts.LINKED_PLACE == ts.CARD_PLACE
    assert "A card in Action requests" in ts.PROMPT and "Direct messages" not in ts.PROMPT + ts.BATCH_PROMPT
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "is in {_PS['card_place']}" in bot  # the pointer line takes the front end's name
    assert '"dm": "Action requests"' in bot
    assert WebFrontend.dm_takes_text is False and SlackFrontend.dm_takes_text is True
    # the Replies card's own lines: "this DM" on Slack, the web chat's name on the web chat
    import types
    expr = bot[bot.index("_CARDS_HERE = (") + len("_CARDS_HERE = "):]
    expr = expr[:expr.index('["card_place"])') + len('["card_place"])')]
    web_fe = types.SimpleNamespace(dm_takes_text=False, prompt_strings=lambda: dict(web))
    slack_fe = types.SimpleNamespace(prompt_strings=lambda: dict(slack))
    assert eval(expr, {"FRONTEND": web_fe}) == "Action requests"
    assert eval(expr, {"FRONTEND": slack_fe}) == "this DM"
    assert "in {_CARDS_HERE}." in bot and "in this DM." not in bot


def test_the_posters_take_the_place_name_from_the_configured_front_end(tmp_path, monkeypatch):
    """chat_notify.card_place reads the configured front end's name for the
    place: "your DM" on Slack (and with no chat block), "Action requests" on
    the web chat; the posters' lines carry it."""
    sys.path.insert(0, str(ROOT / "preprocessor"))
    import chat_notify
    import sched_agent
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    cfg = ea / "config" / "ea-config.json"
    seen = {}
    for frontend, want in (("slack", "your DM"), ("web", "Action requests")):
        cfg.write_text(json.dumps({"chat": {"frontend": frontend}}))
        monkeypatch.setattr(chat_notify, "_chat_cfg", lambda f=frontend: {"frontend": f})
        monkeypatch.setattr(chat_pkg.ea_config, "get_chat", lambda *a, f=frontend, **k: {"frontend": f, "web": {}})
        seen[frontend] = chat_notify.card_place()
        line = sched_agent.fallback_reply({"writes": [{"tool": "create-reply", "executed": True}]})
        assert f"the Send button is in {want}." in line, line
    assert seen == {"slack": "your DM", "web": "Action requests"}


def test_the_requests_route_gives_each_request_its_block_and_urgency(fe):
    """GET /api/requests: every waiting card of the DM (most urgent first,
    then newest) with its folded words, its urgency and its project (the
    host's, else the room its conversation is in); the rest (an answered
    card, a notice) under `done`, newest first, at most `limit`. A host
    whose context fails gives no project and no deadline."""
    from dataclasses import asdict
    from datetime import date, datetime, timedelta
    fe.create_private_room("ea-grant")
    room = next(c["id"] for c in fe.store.channels() if c["name"] == "ea-grant")
    talk = _say(fe, room, "Reply to the panel about the figures.")
    over = fe.post_card("dm", card(title=":lock: Send this reply?", ea_channel=room, thread_ts=talk["thread_key"],
                                   fields=[CardField("To", "a@example.test, b@example.test, c@example.test, "
                                                           "d@example.test"), CardField("Subject", "Re: Figures")]))
    soon = fe.post_card("dm", card(kind="event", title=":lock: Create this event?",
                                   fields=[CardField("Subject", "Review panel"), CardField("When", "Thu 10:00"),
                                           CardField("Attendees", "s@example.test")]))
    calm = fe.post_card("dm", card(kind="plugin", title=":lock: *Upload the letter?*", fields=[]))
    done = fe.post_card("dm", card(title="Sent one"))
    fe.replace_card_with_status(done, ":white_check_mark: Sent.")
    old = fe.store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card", text="",
                               card=asdict(card(title="An old one")), posted_at=time.time() - 6.5 * 86400)
    notice = fe.post("dm", "Your flight leaves at nine.")
    asked = []

    def ctx(items):
        asked.append(sorted(i["id"] for i in items))
        return {over.id: {"due": (date.today() - timedelta(days=1)).isoformat()},
                soon.id: {"starts": (datetime.now() + timedelta(hours=5)).isoformat(), "project": "review-panel"}}
    fe.request_context = ctx
    status, _h, body = owner(fe, "GET", "/api/requests")
    out = json.loads(body)
    assert status == 200 and out["as_of"] == fe.store.max_event_seq()
    assert [j["id"] for j in out["waiting"]] == [over.id, old["id"], soon.id, calm.id]
    assert [j["id"] for j in out["done"]] == [notice.id, done.id]
    w = {j["id"]: j for j in out["waiting"]}
    assert {k: w[over.id][k] for k in ("action", "who", "what", "urgency", "why", "project", "waiting")} == \
        {"action": "Send email", "who": "To a@example.test, b@example.test and 2 more", "what": "Re: Figures",
         "urgency": "overdue", "why": "due", "project": "grant", "waiting": True}  # the room's name, no "ea-"
    assert w[over.id]["origin"] == {"channel": room, "thread_key": talk["thread_key"], "kind": "room", "name": "ea-grant"}
    assert w[over.id]["card"]["fields"][0]["value"].startswith("a@example.test")  # the full card, for opening
    assert (w[old["id"]]["urgency"], w[old["id"]]["why"]) == ("overdue", "expires")
    assert {k: w[soon.id][k] for k in ("action", "who", "when", "urgency", "why", "project")} == \
        {"action": "Create meeting", "who": "Attendees s@example.test", "when": "Thu 10:00", "urgency": "soon",
         "why": "starts", "project": "review-panel"}
    assert (w[calm.id]["action"], w[calm.id]["what"], w[calm.id]["urgency"]) == ("Upload the letter?", "", "calm")
    d = {j["id"]: j for j in out["done"]}
    assert (d[done.id]["action"], d[done.id]["kind"], d[done.id]["urgency"]) == ("Send email", "status", "calm")
    assert d[notice.id]["action"] == "Notice"
    assert asked == [sorted([over.id, soon.id, calm.id, old["id"], done.id, notice.id])]
    assert [j["id"] for j in json.loads(owner(fe, "GET", "/api/requests?limit=1")[2])["done"]] == [notice.id]
    assert owner(fe, "GET", "/api/requests?limit=0")[0] == 400
    assert call(fe, "GET", "/api/requests")[0] == 401

    def broken(items):
        raise OSError("the ledger is away")
    fe.request_context = broken
    out = json.loads(owner(fe, "GET", "/api/requests")[2])
    assert {j["id"]: j["project"] for j in out["waiting"]}[over.id] == "grant"  # the room still names it
    assert {j["id"]: j["urgency"] for j in out["waiting"]}[over.id] == "calm"  # no deadline known


def test_a_request_being_acted_on_stays_on_top_and_its_answer_links_on(fe):
    """A card whose status line says Tacitus is at work on the press
    (Writing the reply) is listed with the waiting ones, `working`, never
    counted as waiting; its final status line that links to the draft's
    Send request carries that request's id as `next`."""
    from chat.cards import ConfirmationCard
    row = fe.post_card("dm", ConfirmationCard(kind="replies", title="Review request", actions=[
        CardAction("reply_draft", "Draft it", {"row": "response-x"}, "primary", "draft", ask="your answer")],
        question={"text": "Will you review it?", "from": "Pat", "since": "", "about": "Review request"}))
    first = json.loads(owner(fe, "GET", "/api/requests")[2])["waiting"][0]
    assert (first["id"], first["action"], first["what"], first["who"]) == \
        (row.id, "Answer needed", "Will you review it?", "From Pat")
    assert first["card"]["question"]["text"] == "Will you review it?"
    fe.replace_card_with_status(row, ":hourglass_flowing_sand: Writing the reply: Yes.")
    out = json.loads(owner(fe, "GET", "/api/requests")[2])
    assert [(j["id"], j["working"], j["waiting"]) for j in out["waiting"]] == [(row.id, True, False)]
    rows = {r["id"]: r for r in json.loads(owner(fe, "GET", "/api/channels")[2])}
    assert rows["dm"]["waiting"] == 0  # at work is not waiting for a tap
    send = fe.post_card("dm", card())
    fe.replace_card_with_status(row, ":white_check_mark: The reply is drafted. Its Send request: "
                                     f"<{fe.permalink('dm', send.id)}|open it>")
    out = json.loads(owner(fe, "GET", "/api/requests")[2])
    assert [j["id"] for j in out["waiting"]] == [send.id]
    assert {j["id"]: j.get("next") for j in out["done"]}[row.id] == send.id


def test_posts_made_inside_tagging_carry_the_tag_and_a_request_takes_its_own_lines(fe):
    """WebFrontend.tagging(): this thread's posts inside it carry the tag
    (nested, the inner wins and the outer comes back); the requests route
    gives each request the lines with its own tag; lines_tagged reads them."""
    from chat import thread_state as ts  # noqa: F401
    parent = fe.post_card("dm", card(title="Two replies wait"))
    with fe.tagging("reply:a"):
        a = fe.post_card("dm", card(title="Reply to A"), thread_ts=parent.thread_key)
        fe.post("dm", "Writing the reply to A.", thread_ts=parent.thread_key)
        with fe.tagging("reply:b"):
            b = fe.post_card("dm", card(title="Reply to B"), thread_ts=parent.thread_key)
        fe.post("dm", "Its Send request is up.", thread_ts=parent.thread_key)
    with fe.tagging("reply:b"):
        fe.post("dm", "Writing the reply to B.", thread_ts=parent.thread_key)
    fe.post("dm", "Still working... (3m)", thread_ts=parent.thread_key)
    items = {j["id"]: j for j in fe.requests()["waiting"]}
    assert [x["text"] for x in items[a.id]["lines"]] == ["Writing the reply to A.", "Its Send request is up."]
    assert [x["text"] for x in items[b.id]["lines"]] == ["Writing the reply to B."]
    assert items[parent.id]["lines"] == []          # a shared thread's untagged line belongs to none
    assert [x["text"] for x in fe.lines_tagged("dm", parent.thread_key, "reply:b")] == ["Writing the reply to B."]
    out = json.loads(owner(fe, "GET", "/api/requests")[2])
    assert {j["id"]: len(j["lines"]) for j in out["waiting"]}[a.id] == 2
