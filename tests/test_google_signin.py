#!/usr/bin/env python3
"""Tests for google_signin.py, the Google sign-in a user does from the chat.

A user with no terminal connects a Google account with a link the bot
posts and the address their browser lands on, pasted back. These tests
hold the owner side to that: the link carries PKCE and the account, its
verifier waits owner-private, a pasted address is found in whatever the
chat wraps it in, and finishing says what to do next in every way it can
end. No network: the token trade and the Drive check are stubbed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-signin-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import pytest  # noqa: E402

import google_signin as G  # noqa: E402

CLIENT = {"installed": {"client_id": "123-test.apps.googleusercontent.com",
                        "client_secret": "public-test-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def signin(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(G, "_client_config", lambda: CLIENT)
    monkeypatch.setattr(G, "_client_source", lambda: "file")
    saved = {}
    monkeypatch.setitem(G.SERVICES["drive"], "save", lambda acct, creds: saved.setdefault(acct, creds))
    monkeypatch.setitem(G.SERVICES["drive"], "check", lambda creds: ("you@gmail.com", ""))
    return {"dir": priv / "google-signin", "saved": saved}


class FakeFlow:
    """What finish() needs of a Flow: fetch_token and credentials."""
    seen = {}

    def __init__(self, token=None, error=None):
        self.token, self.error = token, error
        self.credentials = object()

    def fetch_token(self, code):
        FakeFlow.seen["code"] = code
        if self.error:
            raise self.error
        return self.token


def _fake_flow(monkeypatch, token=None, error=None):
    def make(scopes, **kwargs):
        FakeFlow.seen.update(kwargs)
        return FakeFlow(token, error)
    monkeypatch.setattr(G, "_flow", make)


def _redirect(state, code="4/0AbCdEf", extra=""):
    return f"http://localhost:8400/?state={state}&code={code}&scope=https://www.googleapis.com/auth/drive.readonly{extra}"


def test_start_makes_a_pkce_link_and_keeps_the_verifier_owner_private(signin):
    out = G.start("You@Gmail.com")
    q = parse_qs(urlsplit(out["url"]).query)
    assert out["account"] == "you@gmail.com" and out["own_client"] is True
    assert q["login_hint"] == ["you@gmail.com"] and q["state"] == [out["state"]]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["http://localhost:8400"]
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    path = signin["dir"] / f"{out['state']}.json"
    record = json.loads(path.read_text())
    assert record["account"] == "you@gmail.com" and len(record["verifier"]) >= 43
    assert path.stat().st_mode & 0o077 == 0 and signin["dir"].stat().st_mode & 0o077 == 0
    text = G.link_text(out)
    assert out["url"] in text and "Why:" in text and "paste it here" in text
    assert "Advanced" in text and "—" not in text


def test_a_bad_account_or_service_is_refused(signin):
    with pytest.raises(G.SigninError):
        G.start("not-an-address")
    with pytest.raises(G.SigninError):
        G.start("you@gmail.com", service="photos")


@pytest.mark.parametrize("text,account", [
    ("connect Google Drive you@gmail.com", "you@gmail.com"),
    ("Connect my google drive for you@gmail.com.", "you@gmail.com"),
    ("connect drive <you@gmail.com>", "you@gmail.com"),
    ("connect google drive", ""),
])
def test_the_connect_command(text, account):
    assert G.parse_command(text) == account


@pytest.mark.parametrize("text", ["connect the dots", "please connect google drive later today",
                                  "disconnect google drive you@gmail.com"])
def test_other_words_are_not_the_command(text):
    assert G.parse_command(text) is None


@pytest.mark.parametrize("wrap", [
    "{u}", "here it is: {u}", "<{u}>", "<{u}|localhost>", "{u}\nthanks",
    "{amp}",
])
def test_a_pasted_address_is_found_in_what_the_chat_wraps_it_in(wrap):
    state = "S" * 32
    u = _redirect(state)
    text = wrap.format(u=u, amp=u.replace("&", "&amp;"))
    assert G.find_redirect(text) == (state, "4/0AbCdEf", "")
    assert G.is_redirect(text)
    assert G.find_redirect(u.replace("localhost:8400", "127.0.0.1:8400"))[0] == state


def test_ordinary_text_and_links_are_not_a_sign_in():
    assert not G.is_redirect("see https://docs.google.com/document/d/abc/edit?state=x")
    assert not G.is_redirect("http://localhost:8400/ with no query")
    assert not G.is_redirect("meeting at 10")


def test_finish_trades_the_code_with_the_stored_verifier_and_saves_the_token(signin, monkeypatch):
    out = G.start("you@gmail.com")
    record = json.loads((signin["dir"] / f"{out['state']}.json").read_text())
    _fake_flow(monkeypatch, token={"scope": G.SERVICES["drive"]["scopes"]})
    res = G.finish("pasted: " + _redirect(out["state"]))
    assert res["status"] == "ok" and res["account"] == "you@gmail.com"
    assert FakeFlow.seen["code_verifier"] == record["verifier"]
    assert FakeFlow.seen["state"] == out["state"] and FakeFlow.seen["code"] == "4/0AbCdEf"
    assert "you@gmail.com" in signin["saved"]
    assert "connected" in res["text"] and "read only" in res["text"]
    assert not (signin["dir"] / f"{out['state']}.json").exists(), "a link is used once"
    again = G.finish(_redirect(out["state"]))
    assert again["status"] == "error" and "does not match" in again["text"]


def test_a_sign_in_as_another_account_is_filed_under_that_account(signin, monkeypatch):
    monkeypatch.setitem(G.SERVICES["drive"], "check", lambda creds: ("other@gmail.com", ""))
    out = G.start("you@gmail.com")
    _fake_flow(monkeypatch, token={"scope": " ".join(G.SERVICES["drive"]["scopes"])})
    res = G.finish(_redirect(out["state"]))
    assert res["account"] == "other@gmail.com" and "other@gmail.com" in signin["saved"]
    assert "not you@gmail.com" in res["text"]


def test_a_drive_api_that_is_off_saves_the_token_and_gives_the_enable_link(signin, monkeypatch):
    link = "https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=123"
    monkeypatch.setitem(G.SERVICES["drive"], "check", lambda creds: ("", link))
    out = G.start("you@gmail.com")
    _fake_flow(monkeypatch, token={"scope": G.SERVICES["drive"]["scopes"]})
    res = G.finish(_redirect(out["state"]))
    assert res["status"] == "api_off" and link in res["text"] and "Enable" in res["text"]
    assert "you@gmail.com" in signin["saved"] and "do not need to sign in again" in res["text"]


def test_an_unticked_drive_box_is_named(signin, monkeypatch):
    out = G.start("you@gmail.com")
    _fake_flow(monkeypatch, token={"scope": "openid"})
    res = G.finish(_redirect(out["state"]))
    assert res["status"] == "error" and "box ticked" in res["text"] and not signin["saved"]


def test_a_used_or_late_code_asks_for_a_new_link(signin, monkeypatch):
    out = G.start("you@gmail.com")
    _fake_flow(monkeypatch, error=RuntimeError("(invalid_grant) Bad Request"))
    res = G.finish(_redirect(out["state"]))
    assert res["status"] == "error" and "only once" in res["text"]
    assert "connect Google Drive you@gmail.com" in res["text"]


def test_a_refusal_on_the_consent_screen(signin, monkeypatch):
    out = G.start("you@gmail.com")
    res = G.finish(f"http://localhost:8400/?error=access_denied&state={out['state']}")
    assert res["status"] == "error" and "did not allow" in res["text"]


def test_an_unknown_or_old_link_is_refused(signin, monkeypatch):
    assert "does not match" in G.finish(_redirect("Z" * 32))["text"]
    out = G.start("you@gmail.com")
    path = signin["dir"] / f"{out['state']}.json"
    record = json.loads(path.read_text())
    record["created"] = time.time() - G.PENDING_TTL_S - 5
    path.write_text(json.dumps(record))
    assert "two days" in G.finish(_redirect(out["state"]))["text"]
    assert G.finish(_redirect("../../etc/passwd" + "x" * 20))["status"] == "error"


# ---------------------------------------------------------------------------
# The bot's side: the command, the pasted address, the staged request
# ---------------------------------------------------------------------------

BOT = HERE.parent / "ea-chat-bot.py"


def _block(start: str, end: str) -> str:
    src = BOT.read_text()
    i = src.index(start)
    return src[i:src.index(end, i)]


class _Front:
    def __init__(self):
        self.posts = []

    def post(self, channel=None, thread_ts=None, text=""):
        self.posts.append((channel, thread_ts, text))


@pytest.fixture
def bot(signin, monkeypatch):
    import re as _re
    import threading
    front = _Front()
    reactions = []
    ns = {"re": _re, "FRONTEND": front, "_active_lock": threading.Lock(),
          "_active_threads": set(),
          "_swap_reaction": lambda ch, ts, old, new: reactions.append(new),
          "log": type("L", (), {"warning": staticmethod(lambda *a: None),
                                "info": staticmethod(lambda *a: None),
                                "error": staticmethod(lambda *a: None)})}
    exec(_block("def _google_signin_mod", "def _is_bot_command"), ns)
    exec(_block("def _post_google_signin_link", "def _open_channel_action"), ns)
    ns["_google_signin_mod"] = lambda: G
    return {"ns": ns, "posts": front.posts, "reactions": reactions}


def test_the_connect_command_posts_the_link_and_no_model_runs(bot):
    ns = bot["ns"]
    assert ns["_handle_google_signin"]("stream:1", "t1", "m1", "connect Google Drive you@gmail.com")
    [(ch, ts, text)] = bot["posts"]
    assert (ch, ts) == ("stream:1", "t1") and "accounts.google.com" in text and "Why:" in text
    assert bot["reactions"] == ["white_check_mark"]


def test_the_command_without_an_account_asks_which(bot):
    assert bot["ns"]["_handle_google_signin"]("c", "t", "m", "connect google drive")
    assert "Which Google account" in bot["posts"][0][2]


def test_a_pasted_address_finishes_the_sign_in_in_the_bot(bot, monkeypatch):
    out = G.start("you@gmail.com")
    _fake_flow(monkeypatch, token={"scope": G.SERVICES["drive"]["scopes"]})
    assert bot["ns"]["_handle_google_signin"]("c", "t", "m", "here: " + _redirect(out["state"]))
    assert "connected for you@gmail.com" in bot["posts"][-1][2]
    assert bot["reactions"][-1] == "white_check_mark"


def test_other_messages_pass_through_to_the_ea(bot):
    assert not bot["ns"]["_handle_google_signin"]("c", "t", "m", "what is on my calendar?")
    assert not bot["posts"]


def test_the_staged_request_posts_the_link_where_it_was_asked(bot):
    bot["ns"]["_google_signin_action"]("c", "t", {"account": "you@gmail.com", "service": "drive"})
    assert "Connect Google Drive for you@gmail.com" in bot["posts"][0][2]
    bot["ns"]["_google_signin_action"]("c", "t", {})
    assert "without an account" in bot["posts"][1][2]


def test_the_bot_wires_the_sign_in_before_the_ea_and_keeps_the_code_out_of_its_log():
    src = BOT.read_text()
    assert '("google_signin", "pending-google-signin.json")' in src
    assert "+ PUBLISH_ACTION_FILES + GOOGLE_SIGNIN_ACTION_FILES)" in src
    cmd = _block("def _handle_bot_command", "    # \"adopt session <uuid>")
    assert "_handle_google_signin(channel, thread_ts, msg_ts, user_msg)" in cmd
    assert '"[a Google sign-in address]"' in src
    assert "_is_google_signin_text(text)" in _block("def _is_bot_command", "# How long")


def test_the_log_redaction_matches_every_address_the_sign_in_accepts():
    import re as _re
    src = BOT.read_text()
    pattern = _re.search(r're\.search\(r"(https\?://\(\?:localhost[^"]+)"', src).group(1)
    for text in (_redirect("S" * 32), "<" + _redirect("S" * 32).replace("localhost", "127.0.0.1") + ">"):
        assert G.is_redirect(text) and _re.search(pattern, text, _re.I)
    assert not _re.search(pattern, "see https://docs.google.com/x?code=1", _re.I)
