#!/usr/bin/env python3
"""preprocessor/chat_read.py: the briefing's EA-channel reader.

The briefing consumes the dicts this module returns (preprocess.py reads
message_count, user_message_count, messages, and the instruction lists).
These pin that the dicts are the ones slack_reader produced: the old
slack_reader is executed from git against a fake Slack WebClient, the new
reader runs on the memory frontend with the same history injected, and
the outputs must be equal.
"""
import io
import json
import subprocess
import sys
import time
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import chat  # noqa: E402
import chat_read  # noqa: E402
import slack_reader  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.types import FileRef, InboundMessage  # noqa: E402

BASE_COMMIT = "28a3633"
CHANNEL = "C_MAIN"
BOT = "U_BOT"
OWNER = "U_OWNER"


def _history():
    """One scenario as Slack raw messages (oldest first) and the matching
    InboundMessages."""
    now = time.time()
    t = [f"{now - 3600 + i * 60:.6f}" for i in range(7)]
    long_bot = "Here is the plan. " * 20
    slack = [
        {"ts": t[0], "user": BOT, "bot_id": "B1", "text": long_bot, "reply_count": 1,
         "thread_ts": t[0]},
        {"ts": t[1], "user": OWNER, "text": "  please draft the reply to Ana  ",
         "reply_count": 2, "thread_ts": t[1]},
        {"ts": t[2], "user": OWNER, "text": "and cc Mark", "thread_ts": t[0],
         "subtype": "thread_broadcast"},
        {"ts": t[3], "user": OWNER, "text": "",
         "files": [{"name": "receipt.png", "filetype": "png"}]},
        {"ts": t[4], "user": OWNER, "text": "   "},
        {"ts": t[5], "user": OWNER, "text": "what is on Friday?"},
        {"ts": t[6], "bot_id": "B1", "text": "Friday is clear."},
    ]
    inbound = [
        InboundMessage(CHANNEL, t[0], long_bot, BOT, float(t[0]), thread_key=t[0],
                       is_bot=True, is_self=True, reply_count=1),
        InboundMessage(CHANNEL, t[1], "  please draft the reply to Ana  ", OWNER,
                       float(t[1]), thread_key=t[1], reply_count=2),
        InboundMessage(CHANNEL, t[2], "and cc Mark", OWNER, float(t[2]), thread_key=t[0],
                       subtype="thread_broadcast"),
        InboundMessage(CHANNEL, t[3], "", OWNER, float(t[3]), subtype="file_share",
                       files=[FileRef("receipt.png", mimetype="png")]),
        InboundMessage(CHANNEL, t[4], "   ", OWNER, float(t[4])),
        InboundMessage(CHANNEL, t[5], "what is on Friday?", OWNER, float(t[5])),
        InboundMessage(CHANNEL, t[6], "Friday is clear.", "", float(t[6]), is_bot=True),
    ]
    return slack, inbound


class _FakeWebClient:
    """conversations_history newest first, as Slack returns it."""
    history: list = []

    def __init__(self, token):
        self.token = token

    def auth_test(self):
        return {"user_id": BOT}

    def conversations_history(self, channel, oldest, limit):
        return {"messages": list(reversed(self.history))}

    def conversations_replies(self, channel, ts, limit, inclusive):
        return {"messages": [m for m in self.history if m["ts"] == ts][:1]}


def _old_reader(monkeypatch, slack_history):
    try:
        src = subprocess.run(["git", "-C", str(REPO), "show",
                              f"{BASE_COMMIT}:preprocessor/slack_reader.py"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{BASE_COMMIT} not available in this checkout")
    fake_sdk = types.ModuleType("slack_sdk")
    fake_sdk.WebClient = _FakeWebClient
    fake_errors = types.ModuleType("slack_sdk.errors")
    fake_errors.SlackApiError = type("SlackApiError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "slack_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", fake_errors)
    mod = types.ModuleType("old_slack_reader")
    exec(compile(src, "old_slack_reader", "exec"), mod.__dict__)
    mod.SLACK_BOT_TOKEN = "xoxb-test"
    mod.CHANNEL_ID = CHANNEL
    _FakeWebClient.history = slack_history
    return mod


def _memory(monkeypatch, inbound):
    fe = MemoryFrontend()
    fe.history = list(inbound)
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: fe)
    return fe


def test_dict_shape_equals_slack_reader(monkeypatch):
    slack_history, inbound = _history()
    old = _old_reader(monkeypatch, slack_history)
    _memory(monkeypatch, inbound)
    expected = old.fetch_recent_messages(hours_back=24)
    got = chat_read.fetch_recent_messages(hours_back=24)
    assert got == expected
    assert [list(m) for m in got] == [list(m) for m in expected]   # key order too
    assert chat_read.summarize_for_briefing(got) == old.summarize_for_briefing(expected)
    # Spot checks on the scenario itself.
    assert [m["sender"] for m in got] == ["bot", "user", "user", "user", "user", "bot"]
    assert got[2]["thread_context"].startswith("[bot]: Here is the plan.")
    assert got[2]["thread_context"].endswith("...")
    assert got[3]["files"] == [{"name": "receipt.png", "type": "png"}]
    summary = chat_read.summarize_for_briefing(got)
    assert summary["user_message_count"] == 4
    assert [m["text"] for m in summary["handled_instructions"]] == \
        ["please draft the reply to Ana"]


def test_raw_slack_filetype_wins(monkeypatch):
    fe = _memory(monkeypatch, [])
    fe.history = [InboundMessage(CHANNEL, "1.1", "", OWNER, time.time() - 5,
                                 files=[FileRef("a.pdf", mimetype="application/pdf")],
                                 raw={"files": [{"name": "a.pdf", "filetype": "pdf"}]})]
    (entry,) = chat_read.fetch_recent_messages(1)
    assert entry["files"] == [{"name": "a.pdf", "type": "pdf"}]


def test_window_excludes_old_messages(monkeypatch):
    fe = _memory(monkeypatch, [])
    fe.history = [InboundMessage(CHANNEL, "1.1", "old", OWNER, time.time() - 7200),
                  InboundMessage(CHANNEL, "1.2", "new", OWNER, time.time() - 60)]
    assert [m["text"] for m in chat_read.fetch_recent_messages(1)] == ["new"]


def test_frontend_failure_is_empty_not_fatal(monkeypatch, capsys):
    def boom(**kw):
        raise KeyError("SLACK_BOT_TOKEN")
    monkeypatch.setattr(chat, "load_frontend", boom)
    assert chat_read.fetch_recent_messages(24) == []
    assert "Chat reader" in capsys.readouterr().err

    class _Broken(MemoryFrontend):
        def recent_messages(self, *a, **k):
            raise RuntimeError("rate limited")
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: _Broken())
    assert chat_read.fetch_recent_messages(24) == []


def test_ported_slack_history_is_not_recent_chat(monkeypatch):
    """Messages the porter carried over from Slack carry today's Zulip time;
    the briefing must not read them as recent instructions."""
    now = time.time()
    header = chat.text.ported_header("Kit", "2026-06-12 09:30")
    _memory(monkeypatch, [
        InboundMessage(CHANNEL, "1", header + "\nplease draft the reply to Ana", BOT,
                       now - 60, is_bot=True, is_self=True),
        InboundMessage(CHANNEL, "2", "what is on Friday?", OWNER, now - 30),
    ])
    got = chat_read.fetch_recent_messages(hours_back=24)
    assert [m["text"] for m in got] == ["what is on Friday?"]


def test_slack_reader_is_a_shim():
    assert slack_reader.fetch_recent_messages is chat_read.fetch_recent_messages
    assert slack_reader.summarize_for_briefing is chat_read.summarize_for_briefing


def test_cli_hours_and_thread(monkeypatch):
    _, inbound = _history()
    _memory(monkeypatch, inbound)
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_read.main(["--hours", "24"]) == 0
    data = json.loads(out.getvalue())
    assert data["message_count"] == 6
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_read.main(["--thread", inbound[0].msg_id, "--channel", CHANNEL]) == 0
    thread = json.loads(out.getvalue())
    assert [m["ts"] for m in thread] == [inbound[0].msg_id, inbound[2].msg_id]
    assert thread[0]["sender"] == "bot" and thread[1]["sender"] == "user"


def test_a_digest_channel_is_not_read_by_its_kind_in_the_web_side_store(monkeypatch, capsys):
    """The composite's web side records the channel kinds; a channel of a
    kind in UNREAD_KINDS is never read, its id whatever it is."""
    _, inbound = _history()
    fe = _memory(monkeypatch, inbound)
    kinds = {CHANNEL: "main"}
    fe.shadow = types.SimpleNamespace(store=types.SimpleNamespace(
        channel=lambda cid: {"kind": kinds[cid]} if cid in kinds else None))
    assert len(chat_read.fetch_recent_messages(hours_back=24)) == 6
    kinds[CHANNEL] = "digest"
    assert chat_read.fetch_recent_messages(hours_back=24) == []
    assert chat_read.fetch_thread(CHANNEL, inbound[0].msg_id) == []
    assert "digest channel; not read" in capsys.readouterr().err
    assert "digest" in chat_read.UNREAD_KINDS
