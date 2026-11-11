"""Owner processes take only allowlisted settings from the resident's .env.

~/.ea/.env belongs to the resident, so an owner process reads it as data and
keeps only owner_trust.DOTENV_SETTING_KEYS; secrets come by the owner_secret
rule. graph_client accepts a sign-in authority only on Microsoft's login host
and a redirect only on the loopback interface, whatever loaded them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import owner_trust  # noqa: E402


def _clear(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_load_owner_env_keeps_only_setting_keys(tmp_path, monkeypatch):
    ea = tmp_path / "ea"
    ea.mkdir()
    (ea / ".env").write_text(
        "GRAPH_CLIENT_ID=abc\n"
        "PATH=/tmp/x\nPYTHONPATH=/tmp/x\nLD_PRELOAD=/tmp/x.so\n"
        "EA_PRIVATE_DIR=/tmp/p\nEA_DIR=/tmp/e\n"
        "SLACK_BOT_TOKEN=from-dotenv\n"
        "GRAPH_SCOPES=$(id)\n")
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "no-private"))
    _clear(monkeypatch, "GRAPH_CLIENT_ID", "GRAPH_SCOPES", "SLACK_BOT_TOKEN", "LD_PRELOAD",
           "PYTHONPATH")
    path_before = os.environ.get("PATH")
    owner_trust.load_owner_env()
    assert os.environ["GRAPH_CLIENT_ID"] == "abc"
    assert os.environ["GRAPH_SCOPES"] == "$(id)"        # data, never expanded
    assert os.environ.get("PATH") == path_before
    assert "LD_PRELOAD" not in os.environ and "PYTHONPATH" not in os.environ
    assert os.environ["EA_DIR"] == str(ea)
    assert "SLACK_BOT_TOKEN" not in os.environ            # never a secret from .env


def test_load_owner_env_does_not_override(tmp_path, monkeypatch):
    ea = tmp_path / "ea"
    ea.mkdir()
    (ea / ".env").write_text("GRAPH_CLIENT_ID=from-file\n")
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("GRAPH_CLIENT_ID", "from-env")
    owner_trust.load_owner_env()
    assert os.environ["GRAPH_CLIENT_ID"] == "from-env"


def test_graph_authority_only_microsoft(monkeypatch):
    import graph_client as g
    assert g._microsoft_authority("") == "https://login.microsoftonline.com/common"
    assert g._microsoft_authority("https://login.microsoftonline.com/organizations") \
        == "https://login.microsoftonline.com/organizations"
    for bad in ("https://example.com/common", "http://login.microsoftonline.com/common",
                "https://login.microsoftonline.com.example.com/x"):
        assert g._microsoft_authority(bad) == "https://login.microsoftonline.com/common"


def test_graph_redirect_only_loopback():
    import graph_client as g
    assert g._loopback_redirect_uri("") == "http://localhost:8400/callback"
    assert g._loopback_redirect_uri("http://localhost:8765/cb") == "http://localhost:8765/cb"
    assert g._loopback_redirect_uri("http://127.0.0.1:8400/callback") == "http://127.0.0.1:8400/callback"
    for bad in ("https://example.com/cb", "http://localhost.example.com/cb",
                "http://10.0.0.5:8400/callback"):
        assert g._loopback_redirect_uri(bad) == "http://localhost:8400/callback"


def test_bot_takes_only_known_secrets():
    """The bot fills os.environ from owner_trust.owner_secrets, not from a
    file's every line."""
    src = (REPO / "ea-chat-bot.py").read_text()
    head = src[: src.index("FRONTEND = _chat.load_frontend()")]
    assert "_owner_trust.owner_secrets()" in head
    assert ".env.secrets\"" not in head.replace("~/.ea/.env.secrets", "")
