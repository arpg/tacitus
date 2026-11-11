"""Tests for the chat package's types, text helpers, config and factory."""
from __future__ import annotations

import json
import os
import pwd
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_config  # noqa: E402
from chat import ChatError, load_frontend, private_dir, prompt_strings_for_config  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.text import (  # noqa: E402
    is_ported,
    mrkdwn_to_markdown,
    ported_header,
    smart_chunk,
)
from chat.types import THREAD_KEY_RE, Status  # noqa: E402


def _old_smart_chunk():
    """The bot's own _smart_chunk, exec'd from its source."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    m = re.search(r"^def _smart_chunk\(.*?(?=^\S)", src, re.S | re.M)
    assert m, "_smart_chunk not found in ea-chat-bot.py"
    ns: dict = {}
    exec(compile(m.group(0), "ea-chat-bot.py[_smart_chunk]", "exec"), ns)
    return ns["_smart_chunk"]


@pytest.mark.parametrize("key", ["1712345678.123456", "stream:12:topic:Hello%20there",
                                 "dm:5:c3", "m1"])
def test_thread_key_accepts(key):
    assert THREAD_KEY_RE.match(key)


@pytest.mark.parametrize("key", ["a b", "x]y", "", ":lead", "a" * 401, "a\n"])
def test_thread_key_rejects(key):
    assert not THREAD_KEY_RE.fullmatch(key)


_CHUNK_CASES = [
    ("short text", 3000),
    ("", 10),
    ("para one\n\npara two\n\npara three " * 40, 300),
    ("line\n" * 500, 97),
    ("word " * 2000, 250),
    ("x" * 7001, 3000),
    ("abc\n\n" + "y" * 40 + " tail\nmore", 20),
    ("  lead\n\n  spaced   \n\n" * 30, 50),
]


@pytest.mark.parametrize("text,limit", _CHUNK_CASES)
def test_smart_chunk_matches_bot(text, limit):
    assert smart_chunk(text, limit) == _old_smart_chunk()(text, limit)


def test_smart_chunk_respects_limit():
    for text, limit in _CHUNK_CASES:
        assert all(len(c) <= limit for c in smart_chunk(text, limit))


@pytest.mark.parametrize("src,want", [
    ("*bold*", "**bold**"),
    ("a *b c* d", "a **b c** d"),
    ("_it_", "*it*"),
    ("~gone~", "~~gone~~"),
    ("see <https://x.org/a_b_c|the page>", "see [the page](https://x.org/a_b_c)"),
    ("<https://x.org/a_b_c>", "https://x.org/a_b_c"),
    ("in <#C0EXAMPLE01>", "in #C0EXAMPLE01"),
    ("in <#C0EXAMPLE01|ea>", "in #ea"),
    ("hi <@U123ABC>", "hi @**U123ABC**"),
    ("a &amp; b &lt;c&gt;", "a & b <c>"),
    ("&amp;lt;", "&lt;"),
    ("done :white_check_mark: :x: :tada:", "done :white_check_mark: :x: :tada:"),
    ("run `*not bold*` now", "run `*not bold*` now"),
    ("```\n*keep* _this_ <https://a|b>\n```\n*x*", "```\n*keep* _this_ <https://a|b>\n```\n**x**"),
    ("snake_case_name stays", "snake_case_name stays"),
    ("2*3*4", "2*3*4"),
    ("**already**", "**already**"),
    ("", ""),
])
def test_mrkdwn_to_markdown(src, want):
    assert mrkdwn_to_markdown(src) == want


def test_mrkdwn_channel_mention_function():
    out = mrkdwn_to_markdown("go to <#C1> and <#C2|two>", mention=lambda c: f"[#room-{c}](u)")
    assert out == "go to [#room-C1](u) and [#room-C2](u)"


def test_mrkdwn_keeps_slack_emoji_names():
    # The web chat draws Slack's names, the names reactions use.
    text = ":mailbox_with_no_mail: :pushpin: :ok_hand: :clock3:"
    assert mrkdwn_to_markdown(text) == text


def test_mrkdwn_inert_span_becomes_a_code_span():
    assert mrkdwn_to_markdown("a \ue000*x* <@U1>\ue001 b") == "a `*x* <@U1>` b"
    assert mrkdwn_to_markdown("a `\ue000x`y\ue001` b") == "a ``x`y`` b"
    # The converter's own placeholders cannot come from the text.
    assert mrkdwn_to_markdown("\x010\x01 \x000\x00 *b*") == "0 0 **b**"


def test_ported_header_marks_only_its_own_first_line():
    head = ported_header("Kit", "2026-06-12 09:30")
    assert head == "**Kit** · 2026-06-12 09:30 · from Slack"
    assert is_ported(head) and is_ported(head + "\nbody")
    assert not is_ported("body\n" + head)
    assert not is_ported("**Kit** · 2026-06-12 09:30\nbody")
    assert not is_ported("")


def test_status_values():
    assert [s.value for s in Status] == [
        "eyes", "hourglass_flowing_sand", "white_check_mark", "x",
        "warning", "double_vertical_bar", "question"]
    assert Status.DONE == "white_check_mark"


def test_get_chat_defaults_missing_file(tmp_path):
    cfg = ea_config.get_chat(tmp_path / "none.json")
    assert cfg == ea_config.DEFAULTS["chat"]
    assert cfg["frontend"] == "slack"
    assert "zulip" not in cfg
    cfg["web"]["port"] = 1
    assert ea_config.DEFAULTS["chat"]["web"]["port"] != 1


def test_get_chat_deep_merge(tmp_path):
    path = tmp_path / "ea-config.json"
    path.write_text(json.dumps({"chat": {
        "frontend": "web",
        "web": {"port": 9001, "public_url": "https://chat.example"},
        "zulip": {"site": "https://z.example"}}}))
    cfg = ea_config.get_chat(path)
    assert cfg["frontend"] == "web"
    w = cfg["web"]
    assert w["port"] == 9001
    assert w["public_url"] == "https://chat.example"
    assert w["base_path"] == ea_config.DEFAULTS["chat"]["web"]["base_path"]
    assert w["sched"] is True
    assert "zulip" not in cfg          # a zulip key in the file is ignored


def test_template_has_chat_block():
    tpl = json.loads((ROOT / "config" / "ea-config.json").read_text())
    assert tpl["chat"] == ea_config.DEFAULTS["chat"]


def _ea_dir(tmp_path, chat):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": chat}))
    return tmp_path


def test_load_frontend_memory(tmp_path):
    fe = load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "memory"}), env={})
    assert isinstance(fe, MemoryFrontend)
    assert fe.name == "memory"
    assert fe.supports("codes") and not fe.supports("home")
    assert prompt_strings_for_config(tmp_path)["platform"]


def test_load_frontend_memory_config(tmp_path):
    fe = load_frontend(ea_dir=_ea_dir(tmp_path, {
        "frontend": "memory",
        "memory": {"owner_id": "U9", "card_registry": str(tmp_path / "priv" / "cards.json")}}),
        env={})
    assert fe.owner_id == "U9"
    assert fe.card_registry is not None


def test_load_frontend_unknown(tmp_path):
    with pytest.raises(ChatError, match="unknown chat frontend 'irc'"):
        load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "irc"}), env={})
    with pytest.raises(ChatError):
        prompt_strings_for_config(tmp_path)


def test_private_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "p"))
    assert private_dir() == tmp_path / "p"
    assert not (tmp_path / "p").exists()
    monkeypatch.delenv("EA_PRIVATE_DIR")
    # owner_trust.private_dir: `.ea-private` in the euid's passwd home
    assert private_dir() == Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea-private"


def test_feature_chat_aliases_slack(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ea_config.py", "feature", "chat"])
    monkeypatch.setattr(ea_config, "profile_features",
                        lambda *a, **k: {"slack": False, "buttons": True})
    assert ea_config._main() == 1
    monkeypatch.setattr(ea_config, "profile_features",
                        lambda *a, **k: {"slack": True, "buttons": False})
    assert ea_config._main() == 0


def test_load_frontend_slack_until_installed(tmp_path):
    import importlib.util
    if importlib.util.find_spec("chat.slack_frontend") is not None:
        pytest.skip("slack_frontend is installed")
    with pytest.raises(ChatError, match="frontend 'slack' is not installed yet"):
        load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "slack"}), env={})
