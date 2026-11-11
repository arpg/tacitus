"""scripts/ea-doctor.py reports each owner trust item: the secrets, the chat
trust settings, the auth directory, the scheduled-send key, the token files,
the harvested tokens' modes and browser profiles left in the auth directory,
each as resolving outside ea-bot space or passing the ownership checks of
preprocessor/owner_trust.py.

Drives the live checks against a scratch EA tree and a scratch private dir
(never the host's), with the memory chat frontend so nothing reaches the
network. The checks read metadata only; nothing is written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "ea-doctor.py"


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def install(tmp_path):
    tree, priv = tmp_path / "ea", tmp_path / "private"
    (tree / "config").mkdir(parents=True)
    auth = tree / "auth"
    auth.mkdir()
    auth.chmod(0o750)
    priv.mkdir(mode=0o700)
    chat = {"frontend": "memory", "shadow": ""}
    _write(tree / "config" / "ea-config.json", json.dumps(
        {"profile": "assistant", "chat": chat, "mail_mediation": {"enabled": True}}), 0o644)
    _write(priv / "chat.json", json.dumps(chat))
    _write(priv / "secrets.env", "SLACK_BOT_TOKEN=k\n")
    _write(auth / "scheduled-send-hmac.key", "x" * 48)
    _write(auth / "msal_token_cache.json", "{}")
    _write(auth / "google_token_a_at_b_c.json", "{}")
    _write(auth / "example_chat_token.txt", "eyJ.a.b", 0o640)
    return tree, priv


def _results(tree: Path, priv: Path) -> dict:
    env = dict(os.environ, EA_DIR=str(tree), EA_PRIVATE_DIR=str(priv))
    r = subprocess.run([sys.executable, str(DOCTOR), "--json", "--profile", "assistant",
                        "--ea-dir", str(tree)], capture_output=True, text=True, env=env, timeout=120)
    return {x["name"]: x for x in json.loads(r.stdout)["results"]}


def _status(res: dict, name: str) -> str:
    assert name in res, sorted(n for n in res if n.startswith("owner trust"))
    return res[name]["status"]


def test_a_hardened_install_passes_each_item(install):
    tree, priv = install
    res = _results(tree, priv)
    for name in ("owner trust: secrets", "owner trust: chat settings", "owner trust: auth dir",
                 "owner trust: scheduled-send key", "owner trust: token files"):
        assert _status(res, name) == "PASS", res[name]
    assert "owner trust: chat settings copy" not in res
    # mail mediation is on, so group read on a harvested token is flagged
    assert _status(res, "owner trust: harvested token modes") == "WARN"
    assert "owner_trust.py token-modes" in res["owner trust: harvested token modes"]["fix"]
    (tree / "auth" / "example_chat_token.txt").chmod(0o600)
    res = _results(tree, priv)
    assert _status(res, "owner trust: harvested token modes") == "PASS"
    assert _status(res, "owner trust: browser profiles") == "PASS"


def test_a_browser_profile_left_in_the_auth_dir_warns(install):
    tree, priv = install
    (tree / "auth" / "example-profile").mkdir(mode=0o700)
    res = _results(tree, priv)
    assert _status(res, "owner trust: browser profiles") == "WARN"
    assert "example-profile" in res["owner trust: browser profiles"]["detail"]
    (priv / "example-profile").mkdir(mode=0o700)          # already moved: the copy left is stale
    assert _status(_results(tree, priv), "owner trust: browser profiles") == "PASS"


def test_secrets_only_in_ea_bot_space_warn_and_an_unsafe_legacy_file_fails(install):
    tree, priv = install
    (priv / "secrets.env").unlink()
    legacy = _write(tree / ".env.secrets", "SLACK_BOT_TOKEN=k\n")
    res = _results(tree, priv)
    assert _status(res, "owner trust: secrets") == "WARN"
    assert "harden-permissions" in res["owner trust: secrets"]["fix"]
    legacy.chmod(0o660)
    res = _results(tree, priv)
    assert _status(res, "owner trust: secrets") == "FAIL"
    assert "group" in res["owner trust: secrets"]["detail"]


def test_chat_settings_missing_or_diverged(install):
    tree, priv = install
    doc = json.loads((tree / "config" / "ea-config.json").read_text())
    doc["chat"].update(frontend="slack", shadow="web")
    _write(tree / "config" / "ea-config.json", json.dumps(doc), 0o644)
    res = _results(tree, priv)
    assert _status(res, "owner trust: chat settings") == "PASS"
    copy = res["owner trust: chat settings copy"]
    assert copy["status"] == "WARN"
    assert "frontend, shadow" in copy["detail"] and "chat.json" in copy["fix"]
    (priv / "chat.json").unlink()
    # without chat.json the bot refuses to start: a failure, naming the seed
    res = _results(tree, priv)
    assert _status(res, "owner trust: chat settings") == "FAIL"
    assert "seed --chat" in str(res["owner trust: chat settings"])


def test_a_swapped_auth_dir_or_a_writable_key_fails(install, tmp_path):
    tree, priv = install
    (tree / "auth" / "scheduled-send-hmac.key").chmod(0o660)
    res = _results(tree, priv)
    assert _status(res, "owner trust: scheduled-send key") == "FAIL"
    (tree / "auth").rename(tmp_path / "auth-away")
    (tree / "auth").symlink_to(tmp_path / "auth-away")
    res = _results(tree, priv)
    assert _status(res, "owner trust: auth dir") == "FAIL"
    assert "symlink" in res["owner trust: auth dir"]["detail"]
