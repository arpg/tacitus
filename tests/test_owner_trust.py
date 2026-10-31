"""preprocessor/owner_trust.py and the owner processes that use it.

The resident (ea-bot) owns every directory above ~/.ea, so it can rename an
entry there away and put its own in its place. These tests pin what an owner
process takes from such a path: an object this euid owns, not a symlink,
writable by no other user, checked on the descriptor that is then read or
replaced; secrets only from the environment, the owner-private secrets.env or
a legacy .env.secrets that passes those checks, never from .env; the chat
trust settings from the owner-private chat.json. Each case below fails on the
behaviour before owner_trust (plain path reads that follow a swapped entry,
.env read for secrets, the chat block taken from ea-config.json).

Nothing here touches live state: EA_DIR, EA_PRIVATE_DIR and the owner's
install tree are scratch directories for every test.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

import pytest

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-owner-trust-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import owner_trust as ot  # noqa: E402
import ea_config  # noqa: E402
import chat  # noqa: E402
import chat_notify  # noqa: E402
import scheduled_sends  # noqa: E402

KEY = b"0123456789abcdef0123456789abcdef0123456789"


def _write(path: Path, text, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner process: this euid owns the private dir, and the EA tree in
    use is the one the owner's ~/.ea names."""
    tree = tmp_path / "ea"
    (tree / "config").mkdir(parents=True)
    (tree / "auth").mkdir(mode=0o750)
    (tree / "auth").chmod(0o750)
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(tree))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tree)
    for name in ("ZULIP_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GRAPH_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    ot._WARNED.clear()
    assert ot.is_owner_process()
    return types.SimpleNamespace(tree=tree, priv=priv, auth=tree / "auth", tmp=tmp_path)


# ── the checks ────────────────────────────────────────────────────────

def test_check_owner_file_refuses_symlink_type_and_writable_modes(tmp_path):
    good = _write(tmp_path / "good", "x", 0o600)
    assert ot.check_owner_file(good)
    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.check_owner_file(link)
    with pytest.raises(ot.OwnerTrustError, match="not a regular file"):
        ot.check_owner_file(tmp_path)
    for mode, what in ((0o602, "others"), (0o620, "group")):
        good.chmod(mode)
        with pytest.raises(ot.OwnerTrustError, match=what):
            ot.check_owner_file(good)
    good.chmod(0o644)                         # readable is fine, writable is not
    ot.check_owner_file(good)


def test_check_owner_dir_refuses_symlink_and_group_write(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o750)
    ot.check_owner_dir(d)
    (tmp_path / "dl").symlink_to(d)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.check_owner_dir(tmp_path / "dl")
    d.chmod(0o770)
    with pytest.raises(ot.OwnerTrustError, match="group"):
        ot.check_owner_dir(d)


def test_private_group_allowance_depends_on_who_is_in_the_group(tmp_path, monkeypatch):
    f = _write(tmp_path / "tok", "x", 0o660)
    monkeypatch.setattr(ot, "_group_has_others", lambda gid, uid: False)
    ot.check_owner_file(f, allow_private_group=True)
    with pytest.raises(ot.OwnerTrustError):
        ot.check_owner_file(f)                # the default is strict
    monkeypatch.setattr(ot, "_group_has_others", lambda gid, uid: True)
    with pytest.raises(ot.OwnerTrustError, match="group"):
        ot.check_owner_file(f, allow_private_group=True)
    f.chmod(0o662)
    monkeypatch.setattr(ot, "_group_has_others", lambda gid, uid: False)
    with pytest.raises(ot.OwnerTrustError, match="others"):
        ot.check_owner_file(f, allow_private_group=True)


def test_a_file_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    f = _write(tmp_path / "f", "x", 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 4242)
    with pytest.raises(ot.OwnerTrustError, match="owned by"):
        ot.check_owner_file(f)
    with pytest.raises(ot.OwnerTrustError, match="owned by"):
        ot.read_owner_file(f, check_dir=False)


def test_read_owner_file_does_not_follow_a_swapped_directory_or_file(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    _write(real / "k", "secret-bytes", 0o600)
    assert ot.read_owner_file(real / "k") == b"secret-bytes"
    # the auth directory renamed away and a symlink put in its place
    (tmp_path / "auth").symlink_to(real)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.read_owner_file(tmp_path / "auth" / "k")
    (real / "k2").symlink_to(real / "k")
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.read_owner_file(real / "k2")


def test_write_owner_file_sets_the_mode_and_never_writes_through_a_link(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    ot.write_owner_file(d / "t.json", "{}", mode=0o640)
    assert (d / "t.json").read_text() == "{}"
    assert stat.S_IMODE((d / "t.json").stat().st_mode) == 0o640
    assert [p.name for p in d.iterdir()] == ["t.json"]      # no temp file left
    victim = _write(tmp_path / "victim", "untouched", 0o600)
    (d / "l.json").symlink_to(victim)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.write_owner_file(d / "l.json", "overwrite")
    assert victim.read_text() == "untouched"
    (tmp_path / "dl").symlink_to(d)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        ot.write_owner_file(tmp_path / "dl" / "x", "y")
    assert not (d / "x").exists()


def test_set_owner_file_mode_changes_the_checked_file_only(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    f = _write(d / "f", "x", 0o640)
    ot.set_owner_file_mode(f, 0o600)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    victim = _write(tmp_path / "v", "x", 0o644)
    (d / "l").symlink_to(victim)
    with pytest.raises(ot.OwnerTrustError):
        ot.set_owner_file_mode(d / "l", 0o600)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_parse_env_is_data_only_with_an_allowlist():
    text = ("# c\nexport ZULIP_API_KEY='k1'\nSLACK_BOT_TOKEN=$(touch /tmp/owned)\n"
            "EA_PRIVATE_DIR=/home/ea-bot/fake\nPATH=/evil\nnot a line\n"
            'GRAPH_CLIENT_SECRET="s p"\n')
    got = ot.parse_env(text)
    assert got == {"ZULIP_API_KEY": "k1", "SLACK_BOT_TOKEN": "$(touch /tmp/owned)",
                   "GRAPH_CLIENT_SECRET": "s p"}
    assert "EA_PRIVATE_DIR" not in got and "PATH" not in got


# ── which processes are the owner's ──────────────────────────────────

def test_the_resident_is_never_an_owner_process(monkeypatch, tmp_path):
    """The process whose own home holds the EA tree is the resident, whatever
    it keeps at its own .ea-private. (The private dir's default moved into
    the euid's home for v1.0.0, and a resident with such a directory passed
    the owner test: ledger.py then ran the owner-only checks as ea-bot.)"""
    home = tmp_path / "resident-home"
    tree = home / ".ea"
    tree.mkdir(parents=True)
    (home / ".ea-private").mkdir(mode=0o700)
    monkeypatch.setattr(ot, "owner_home", lambda: home)
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tree)
    monkeypatch.setenv("EA_DIR", str(tree))
    monkeypatch.delenv("EA_PRIVATE_DIR", raising=False)
    assert ot.private_dir() == home / ".ea-private"
    assert ot.hosts_ea_tree()
    assert not ot.is_owner_process()
    # the owner: ~/.ea is a link into the resident's home, .ea-private its own
    ohome = tmp_path / "owner-home"
    ohome.mkdir()
    (ohome / ".ea").symlink_to(tree)
    (ohome / ".ea-private").mkdir(mode=0o700)
    monkeypatch.setattr(ot, "owner_home", lambda: ohome)
    monkeypatch.setattr(ot, "install_ea_dir", lambda: ohome / ".ea")
    monkeypatch.setenv("EA_DIR", str(ohome / ".ea"))
    assert not ot.hosts_ea_tree()
    assert ot.is_owner_process()


def test_is_owner_process(owner, monkeypatch, tmp_path):
    assert ot.is_owner_process()
    monkeypatch.setenv("EA_DIR", str(tmp_path / "other-tree"))     # a scratch tree
    assert not ot.is_owner_process()
    monkeypatch.setenv("EA_DIR", str(owner.tree))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "missing"))
    assert not ot.is_owner_process()
    (tmp_path / "plink").symlink_to(owner.priv)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "plink"))
    assert not ot.is_owner_process()


# ── the secrets rule ──────────────────────────────────────────────────

def test_owner_secret_order_env_private_legacy(owner, monkeypatch):
    legacy = _write(owner.tree / ".env.secrets", "ZULIP_API_KEY=legacy\nSLACK_BOT_TOKEN=lslack\n")
    assert ot.owner_secret("ZULIP_API_KEY") == "legacy"
    _write(owner.priv / "secrets.env", "ZULIP_API_KEY=private\n")
    assert ot.owner_secret("ZULIP_API_KEY") == "private"
    assert ot.owner_secret("SLACK_BOT_TOKEN") == "lslack"          # per key
    monkeypatch.setenv("ZULIP_API_KEY", "env")
    assert ot.owner_secret("ZULIP_API_KEY") == "env"
    assert ot.owner_secret("ZULIP_API_KEY", env={}) == "private"
    assert legacy.exists()
    with pytest.raises(ValueError):
        ot.owner_secret("EA_DIR")


def test_legacy_secrets_must_pass_the_contract(owner, tmp_path):
    legacy = owner.tree / ".env.secrets"
    _write(legacy, "ZULIP_API_KEY=legacy\n", 0o660)                 # group-writable
    assert ot.owner_secret("ZULIP_API_KEY") == ""
    legacy.unlink()
    elsewhere = _write(tmp_path / "planted", "ZULIP_API_KEY=planted\n", 0o600)
    legacy.symlink_to(elsewhere)                                     # a symlink
    assert ot.owner_secret("ZULIP_API_KEY") == ""
    legacy.unlink()
    _write(legacy, "ZULIP_API_KEY=legacy\n", 0o600)
    assert ot.owner_secret("ZULIP_API_KEY") == "legacy"


def test_dotenv_is_never_a_source_of_secrets(owner, monkeypatch):
    _write(owner.tree / ".env", "ZULIP_API_KEY=from-dotenv\nSLACK_BOT_TOKEN=xoxb-dotenv\n", 0o640)
    assert ot.owner_secret("ZULIP_API_KEY") == ""
    monkeypatch.setattr(chat_notify, "EA_DIR", owner.tree)
    assert chat_notify._secret("ZULIP_API_KEY") == ""
    assert chat_notify._secret("SLACK_BOT_TOKEN") == ""


def test_chat_notify_secret_takes_the_private_file(owner, monkeypatch):
    monkeypatch.setattr(chat_notify, "EA_DIR", owner.tree)
    _write(owner.tree / ".env.secrets", "ZULIP_API_KEY=legacy\n", 0o664)   # refused
    assert chat_notify._secret("ZULIP_API_KEY") == ""
    _write(owner.priv / "secrets.env", "ZULIP_API_KEY=private\n")
    assert chat_notify._secret("ZULIP_API_KEY") == "private"


def test_a_group_writable_private_secrets_file_is_refused(owner):
    _write(owner.priv / "secrets.env", "ZULIP_API_KEY=private\n", 0o620)
    assert ot.owner_secret("ZULIP_API_KEY") == ""


def test_load_frontend_gets_the_zulip_key_by_the_rule(owner, monkeypatch):
    _write(owner.tree / "config" / "ea-config.json", json.dumps({"chat": {"frontend": "zulip"}}), 0o644)
    made = {}

    class FakeZulip:
        def __init__(self, cfg, *, api_key, private_dir):
            made.update(cfg=cfg, api_key=api_key)

    monkeypatch.setattr(chat, "_platform_module", lambda name: types.SimpleNamespace(ZulipFrontend=FakeZulip))
    _write(owner.tree / ".env", "ZULIP_API_KEY=from-dotenv\n", 0o640)
    with pytest.raises(KeyError, match="ZULIP_API_KEY"):
        chat.load_frontend(ea_dir=owner.tree, env={})
    _write(owner.priv / "secrets.env", "ZULIP_API_KEY=private\n")
    chat.load_frontend(ea_dir=owner.tree, env={})
    assert made["api_key"] == "private"
    chat.load_frontend(ea_dir=owner.tree, env={"ZULIP_API_KEY": "env"})
    assert made["api_key"] == "env"


# ── the chat trust settings ───────────────────────────────────────────

def _shared_chat(owner, monkeypatch, block):
    cfg = _write(owner.tree / "config" / "ea-config.json", json.dumps({"chat": block}), 0o644)
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)


def test_owner_takes_the_chat_block_from_the_private_copy(owner, monkeypatch, capsys):
    _shared_chat(owner, monkeypatch, {"frontend": "zulip", "zulip": {
        "site": "https://attacker.example", "bot_email": "blake.kestrel40@example.com",
        "owner_user_id": "666"}})
    # no chat.json yet: the owner refuses, naming the seed step, and never
    # runs on ea-bot's copy
    with pytest.raises(ot.OwnerTrustError, match="seed --chat"):
        ea_config.get_chat()
    with pytest.raises(ot.OwnerTrustError, match="chat.json is missing"):
        chat.load_frontend()
    _write(owner.priv / "chat.json", json.dumps({"frontend": "zulip", "zulip": {
        "site": "https://zulip.mine", "bot_email": "hollis.ashcombe67@example.com", "owner_user_id": "8"}}))
    got = ea_config.get_chat()
    assert (got["zulip"]["site"], got["zulip"]["bot_email"], got["zulip"]["owner_user_id"]) == \
        ("https://zulip.mine", "hollis.ashcombe67@example.com", "8")
    assert got["zulip"]["dm_stream"] == "ea-dm"          # defaults still merge
    # an explicit path is read as given (tests, the doctor)
    assert ea_config.get_chat(owner.tree / "config" / "ea-config.json")["zulip"]["site"] == \
        "https://attacker.example"


def test_the_bot_takes_the_slack_owner_id_only_from_the_private_copy(owner, monkeypatch):
    """~/.ea/.env is ea-bot's to rewrite, so the owner id that decides whose
    click approves a card comes from chat.json."""
    _shared_chat(owner, monkeypatch, {"frontend": "slack"})
    made = {}

    class FakeSlack:
        @classmethod
        def from_env(cls, env, *, for_cli=False):
            made.update(owner=env.get("EA_SLACK_OWNER_ID"), for_cli=for_cli)

    monkeypatch.setattr(chat, "_platform_module", lambda name: types.SimpleNamespace(SlackFrontend=FakeSlack))
    env = {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y", "EA_SLACK_OWNER_ID": "U_FROM_DOTENV"}
    _write(owner.priv / "chat.json", json.dumps({"frontend": "slack"}))
    with pytest.raises(ot.OwnerTrustError, match="slack.owner_user_id"):
        chat.load_frontend(env=env)
    chat.load_frontend(env=env, for_cli=True)            # a sender approves nothing
    assert made == {"owner": "", "for_cli": True}
    _write(owner.priv / "chat.json", json.dumps({"frontend": "slack", "slack": {"owner_user_id": "U_MINE"}}))
    chat.load_frontend(env=env)
    assert made == {"owner": "U_MINE", "for_cli": False}


def test_the_chat_seed_confirms_the_slack_owner_id(owner, monkeypatch):
    _write(owner.tree / "config" / "ea-config.json", json.dumps({"chat": {"frontend": "slack"}}), 0o644)
    _write(owner.tree / ".env", "EA_SLACK_OWNER_ID=U_MINE\n", 0o640)
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--chat", "--chat-frontend", "slack") == 1      # owner id unconfirmed
    assert not (owner.priv / "chat.json").exists()
    assert _seed(owner, "--chat", "--chat-frontend", "slack", "--chat-owner-id", "U_MINE") == 0
    block = json.loads((owner.priv / "chat.json").read_text())
    assert block["slack"] == {"owner_user_id": "U_MINE"} and "zulip" not in block
    # a chat.json written before the owner id moved here gets it added
    _write(owner.priv / "chat.json", json.dumps({"frontend": "slack"}))
    assert _seed(owner, "--chat") == 1
    assert _seed(owner, "--chat", "--chat-owner-id", "U_MINE") == 0
    assert json.loads((owner.priv / "chat.json").read_text())["slack"]["owner_user_id"] == "U_MINE"


def test_an_untrusted_private_chat_file_fails_closed(owner, monkeypatch):
    _shared_chat(owner, monkeypatch, {"frontend": "slack"})
    _write(owner.priv / "chat.json", json.dumps({"frontend": "zulip"}), 0o620)
    with pytest.raises(ot.OwnerTrustError):
        ea_config.get_chat()
    _write(owner.priv / "chat.json", "[1, 2]", 0o600)
    with pytest.raises(ot.OwnerTrustError, match="JSON object"):
        ea_config.get_chat()


def test_a_non_owner_process_reads_the_shared_config(owner, monkeypatch, tmp_path):
    _shared_chat(owner, monkeypatch, {"frontend": "memory"})
    _write(owner.priv / "chat.json", json.dumps({"frontend": "zulip"}))
    assert ea_config.get_chat()["frontend"] == "zulip"
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "unreachable"))
    assert ea_config.get_chat()["frontend"] == "memory"


# ── the scheduled-send key ────────────────────────────────────────────

@pytest.fixture
def sched(owner, monkeypatch):
    monkeypatch.setattr(scheduled_sends, "HMAC_KEY_FILE", owner.auth / "scheduled-send-hmac.key")
    return owner


def test_the_key_reads_when_the_owner_checks_pass(sched):
    _write(scheduled_sends.HMAC_KEY_FILE, KEY, 0o600)
    tok = scheduled_sends.make_send_token("D1", "2020-01-01T00:00", "sha256:ab")
    assert scheduled_sends.verify_send_token(tok, "D1", "2020-01-01T00:00", fingerprint="sha256:ab")


def test_a_swapped_auth_dir_or_key_fails_closed(sched, tmp_path):
    # a real-looking key the resident controls, reached through a swapped entry
    theirs = tmp_path / "theirs"
    theirs.mkdir(mode=0o700)
    _write(theirs / "scheduled-send-hmac.key", KEY, 0o600)
    real = sched.auth
    real.rename(tmp_path / "auth-away")
    real.symlink_to(theirs)
    with pytest.raises(scheduled_sends.ApprovalKeyError, match="refused"):
        scheduled_sends.make_send_token("D1", "2020-01-01T00:00", "sha256:ab")
    real.unlink()
    (tmp_path / "auth-away").rename(real)
    (real / "scheduled-send-hmac.key").symlink_to(theirs / "scheduled-send-hmac.key")
    with pytest.raises(scheduled_sends.ApprovalKeyError, match="refused"):
        scheduled_sends.make_send_token("D1", "2020-01-01T00:00", "sha256:ab")
    (real / "scheduled-send-hmac.key").unlink()
    _write(real / "scheduled-send-hmac.key", KEY, 0o660)             # no group allowance
    with pytest.raises(scheduled_sends.ApprovalKeyError, match="refused"):
        scheduled_sends.make_send_token("D1", "2020-01-01T00:00", "sha256:ab")
    forged = "pre-approved:v2:" + "0" * 64 + ":2020-01-01T00:00"
    assert not scheduled_sends.verify_send_token(forged, "D1", "2020-01-01T00:00", fingerprint="sha256:ab")


def test_a_key_or_token_with_a_second_name_is_refused(sched, tmp_path):
    """macOS has no protected_hardlinks, so the resident can give any file it
    can reach a second name; a trust anchor must have one link."""
    key = _write(scheduled_sends.HMAC_KEY_FILE, KEY, 0o600)
    os.link(key, tmp_path / "other-name")
    with pytest.raises(scheduled_sends.ApprovalKeyError, match="2 links"):
        scheduled_sends.make_send_token("D1", "2020-01-01T00:00", "sha256:ab")
    token = _write(sched.auth / "msal_token_cache.json", "{}", 0o600)
    os.link(token, tmp_path / "token-other-name")
    with pytest.raises(ot.OwnerTrustError, match="links"):
        ot.read_owner_file(token)
    (tmp_path / "token-other-name").unlink()
    assert ot.read_owner_file(token) == b"{}"


def test_a_refusal_never_says_to_chown_the_file(tmp_path):
    """The checks see the owner, not the writer: a fix that chowns another
    user's file to the owner would make its contents a trust anchor."""
    st = os.lstat(_write(tmp_path / "f", "x", 0o600))
    other = types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1, st_gid=st.st_gid,
                                  st_nlink=1)
    with pytest.raises(ot.OwnerTrustError) as e:
        ot._check_stat(other, tmp_path / "f", "file", False)
    assert "Never chown it" in str(e.value) and "chown it to the owner" not in str(e.value)


def test_check_env_loads_no_secret_or_path_from_dotenv(sched, monkeypatch):
    monkeypatch.setattr(scheduled_sends, "EA_DIR", sched.tree)
    for k in ("EA_SLACK_CHANNEL_ID", "GRAPH_AUTHORITY", "GRAPH_CLIENT_ID", "PYTHONSTARTUP",
              "EA_TEST_INJECTED"):
        monkeypatch.delenv(k, raising=False)
    _write(sched.tree / ".env", "\n".join([
        "EA_SLACK_CHANNEL_ID=C0CHAN", "SLACK_BOT_TOKEN=xoxb-dotenv",
        "GRAPH_AUTHORITY=https://evil.example/common", "GRAPH_CLIENT_ID=abc",
        "PYTHONSTARTUP=/home/ea-bot/evil.py", "EA_TEST_INJECTED=1"]) + "\n", 0o640)
    _write(sched.priv / "secrets.env", "SLACK_BOT_TOKEN=xoxb-private\n")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    scheduled_sends._load_env()
    assert os.environ["EA_SLACK_CHANNEL_ID"] == "C0CHAN"
    assert os.environ["GRAPH_CLIENT_ID"] == "abc"
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-private"          # never the .env copy
    for k in ("GRAPH_AUTHORITY", "PYTHONSTARTUP", "EA_TEST_INJECTED"):
        assert k not in os.environ, k
    # the Microsoft sign-in host is accepted
    _write(sched.tree / ".env", "GRAPH_AUTHORITY=https://login.microsoftonline.com/tenant\n", 0o640)
    scheduled_sends._load_env()
    assert os.environ["GRAPH_AUTHORITY"] == "https://login.microsoftonline.com/tenant"


# ── token files (Google, Graph, harvested) ────────────────────────────

GOOGLE_TOKEN = json.dumps({"token": "at", "refresh_token": "rt", "client_id": "cid",
                           "client_secret": "cs", "token_uri": "https://oauth2.googleapis.com/token"})


@pytest.mark.parametrize("modname", ["gmail_client", "google_calendar_client"])
def test_google_token_reads_refuse_a_swapped_file(owner, monkeypatch, modname, tmp_path):
    mod = __import__(modname)
    monkeypatch.setattr(mod, "AUTH_DIR", owner.auth)
    tok = mod._token_path("a@b.c")
    planted = _write(tmp_path / "planted.json", GOOGLE_TOKEN, 0o600)
    tok.symlink_to(planted)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        mod._load_token(tok)
    assert mod.authenticate_account("a@b.c", interactive=False) is None   # refused, no creds
    tok.unlink()
    _write(tok, GOOGLE_TOKEN, 0o600)
    assert mod._load_token(tok).refresh_token == "rt"


@pytest.mark.parametrize("modname", ["gmail_client", "google_calendar_client"])
def test_google_token_writes_go_through_the_checked_dir(owner, monkeypatch, modname, tmp_path):
    mod = __import__(modname)
    monkeypatch.setattr(mod, "AUTH_DIR", owner.auth)
    tok = mod._token_path("a@b.c")
    creds = types.SimpleNamespace(to_json=lambda: GOOGLE_TOKEN)
    mod._save_token(tok, creds)
    assert json.loads(tok.read_text())["refresh_token"] == "rt"
    # The token file rule, and never group write: a 0660 token whose
    # group holds another user is refused on its next read.
    assert stat.S_IMODE(tok.stat().st_mode) == ot.token_file_mode()[0]
    assert not stat.S_IMODE(tok.stat().st_mode) & stat.S_IWGRP
    mod._save_token(tok, creds)          # a rewrite passes the checks too
    assert json.loads(tok.read_text())["refresh_token"] == "rt"
    # an auth dir swapped for a directory the resident controls gets nothing
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    owner.auth.rename(tmp_path / "auth-away")
    owner.auth.symlink_to(theirs)
    with pytest.raises(ot.OwnerTrustError, match="symlink"):
        mod._save_token(tok, creds)
    assert list(theirs.iterdir()) == []


def test_non_owner_google_reads_are_unchanged(monkeypatch, tmp_path):
    import gmail_client
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "unreachable"))
    seen = []
    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file",
                        classmethod(lambda cls, path, scopes: seen.append(path) or "creds"))
    assert gmail_client._load_token(tmp_path / "t.json") == "creds"
    assert seen == [str(tmp_path / "t.json")]


@pytest.mark.skipif(sys.platform == "darwin",
                    reason="the 0660 token layout needs a per-user private group; macOS "
                           "users share staff, and the macOS installer puts tokens in group ea")
def test_graph_token_reads_and_cache_writes(owner, monkeypatch, tmp_path):
    import graph_client as gc
    cache = owner.auth / "msal_token_cache.json"
    monkeypatch.setattr(gc, "CACHE_FILE", cache)
    gc._write_cache_file('{"AccessToken": {}}')
    assert json.loads(cache.read_text()) == {"AccessToken": {}}
    assert stat.S_IMODE(cache.stat().st_mode) == 0o660
    assert gc._read_auth_text(cache) == '{"AccessToken": {}}'
    linked = owner.auth / "example_token.txt"
    planted = _write(tmp_path / "planted", "eyJ.x.y", 0o600)
    linked.symlink_to(planted)
    with pytest.raises(ot.OwnerTrustError):
        gc._read_auth_text(linked)
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    owner.auth.rename(tmp_path / "auth-away")
    owner.auth.symlink_to(theirs)
    with pytest.raises(ot.OwnerTrustError):
        gc._write_cache_file('{"RefreshToken": {"k": {"secret": "r"}}}')
    assert list(theirs.iterdir()) == []


def test_token_modes_follow_mediation(owner, monkeypatch):
    tok = _write(owner.auth / "example_token.txt", "eyJ.a.b\n", 0o640)
    other = _write(owner.auth / "msal_token_cache.json", "{}", 0o640)
    monkeypatch.setattr(ot, "_resident_reads_tokens", lambda: False)      # mediated
    assert ot.token_file_mode() == (0o600, None)
    done, problems = ot.fix_token_modes(owner.auth)
    assert done == [tok] and problems == []
    assert stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert tok.read_text() == "eyJ.a.b\n"                                  # not rewritten
    assert stat.S_IMODE(other.stat().st_mode) == 0o640                    # JSON stores keep theirs
    monkeypatch.setattr(ot, "_resident_reads_tokens", lambda: True)       # no mediation
    monkeypatch.setattr(ot, "_ea_gid", lambda: None)
    assert ot.token_file_mode() == (0o640, None)
    ot.fix_token_modes(owner.auth)
    assert stat.S_IMODE(tok.stat().st_mode) == 0o640


def test_token_modes_refuse_a_link_and_the_cli_says_so(owner, monkeypatch, tmp_path, capsys):
    planted = _write(tmp_path / "planted.txt", "x", 0o644)
    (owner.auth / "example_token.txt").symlink_to(planted)
    monkeypatch.setattr(ot, "_resident_reads_tokens", lambda: False)
    done, problems = ot.fix_token_modes(owner.auth)
    assert done == [] and len(problems) == 1
    assert stat.S_IMODE(planted.stat().st_mode) == 0o644                  # the target is untouched
    assert ot.main(["token-modes", "--ea-dir", str(owner.tree)]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_mediation_state_unknown_counts_as_mediated(monkeypatch):
    fake = types.ModuleType("mail_mediation")

    def boom():
        raise RuntimeError("unreadable")
    fake.mediation_active_for_resident = boom
    monkeypatch.setitem(sys.modules, "mail_mediation", fake)
    assert ot._resident_reads_tokens() is False
    assert ot.token_file_mode() == (0o600, None)


# ── the seed the root step runs ───────────────────────────────────────

def _seed(owner, *args):
    return ot.main(["seed", "--ea-dir", str(owner.tree), *args])


def test_seed_copies_a_checked_legacy_file_and_the_chat_block(owner, capsys):
    legacy = _write(owner.tree / ".env.secrets", "ZULIP_API_KEY=k\nGRAPH_CLIENT_SECRET=s\n")
    _write(owner.tree / "config" / "ea-config.json", json.dumps({"profile": "assistant", "chat": {
        "frontend": "zulip", "zulip": {"site": "https://z.mine", "bot_email": "hale.stavros17@example.com",
                                       "owner_user_id": "8"}}}), 0o644)
    assert _seed(owner, "--chat-frontend", "zulip", "--chat-site", "https://z.mine",
                 "--chat-bot-email", "hale.stavros17@example.com", "--chat-owner-id", "8") == 0
    out = capsys.readouterr().out
    sec, chat_file = owner.priv / "secrets.env", owner.priv / "chat.json"
    assert sec.read_bytes() == legacy.read_bytes()
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600
    assert json.loads(chat_file.read_text())["zulip"]["site"] == "https://z.mine"
    assert stat.S_IMODE(chat_file.stat().st_mode) == 0o600
    for line in ("site          = https://z.mine", "bot_email     = hale.stavros17@example.com", "owner_user_id = 8"):
        assert line in out
    assert "=k" not in out and "=s" not in out                  # values never printed
    # idempotent: existing files are kept, even when the sources change
    _write(legacy, "ZULIP_API_KEY=other\n")
    assert _seed(owner) == 0
    assert "ZULIP_API_KEY=k" in sec.read_text()


def _chat_tree(owner, **zulip):
    z = {"site": "https://z.mine", "bot_email": "hale.stavros17@example.com",
         "owner_user_id": "8", "stream": "ea", **zulip}
    _write(owner.tree / "config" / "ea-config.json",
           json.dumps({"chat": {"frontend": "zulip", "zulip": z}}), 0o644)


def test_the_chat_seed_writes_nothing_unconfirmed(owner, monkeypatch, capsys):
    """chat.json is copied from ea-bot space, so the seed shows every value
    and writes only what the operator types back or passes as a flag."""
    _chat_tree(owner)
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--chat") == 1                     # no terminal, no flags
    captured = capsys.readouterr()
    assert "zulip.stream = ea" in captured.out and "zulip.owner_user_id = 8" in captured.out
    assert "--chat-owner-id" in captured.err and "--chat-stream" in captured.err
    assert not (owner.priv / "chat.json").exists()
    typed = iter(["zulip", "https://z.mine", "hale.stavros17@example.com", "9", "ea"])
    monkeypatch.setattr(ot, "_interactive", lambda: True)
    monkeypatch.setattr(ot, "_prompt", lambda q: next(typed))
    assert _seed(owner, "--chat") == 1                     # one value typed differently
    assert not (owner.priv / "chat.json").exists()
    typed = iter(["zulip", "https://z.mine", "hale.stavros17@example.com", "8", "ea"])
    assert _seed(owner, "--chat") == 0
    block = json.loads((owner.priv / "chat.json").read_text())
    assert block["zulip"]["owner_user_id"] == "8" and block["zulip"]["stream"] == "ea"


def test_a_flag_supplies_the_trusted_value(owner, monkeypatch):
    _chat_tree(owner, owner_user_id="666", site="https://attacker.example")
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--chat", "--chat-frontend", "zulip", "--chat-site", "https://z.mine",
                 "--chat-bot-email", "hale.stavros17@example.com", "--chat-owner-id", "8",
                 "--chat-stream", "ea") == 0
    z = json.loads((owner.priv / "chat.json").read_text())["zulip"]
    assert (z["site"], z["owner_user_id"]) == ("https://z.mine", "8")


def test_the_chat_seed_confirms_every_stream_the_bot_posts_to(owner, monkeypatch, capsys):
    """The DM stream gets every card with its full draft body, and each
    stream key routes posts; ea-bot can write its copy, so a routing value
    is confirmed like the server and the owner id."""
    _chat_tree(owner, sched_stream="ea-scheduling", dm_stream="leak-dm",
               notify={"health": {"stream": "ea-health", "topic": "alerts"},
                       "personal": {"stream": "public-feed"}})
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    trusted = ["--chat-frontend", "zulip", "--chat-site", "https://z.mine",
               "--chat-bot-email", "hale.stavros17@example.com", "--chat-owner-id", "8",
               "--chat-stream", "ea", "--chat-sched-stream", "ea-scheduling"]
    # the DM stream and the notify streams are not confirmed: nothing is written
    assert _seed(owner, "--chat", *trusted) == 1
    err = capsys.readouterr().err
    assert "--chat-dm-stream" in err and "--chat-notify health=<stream>" in err
    assert not (owner.priv / "chat.json").exists()
    # flags put the owner's own streams in place of ea-bot's
    assert _seed(owner, "--chat", *trusted, "--chat-dm-stream", "ea-dm",
                 "--chat-notify", "health=ea-health", "--chat-notify", "personal=ea") == 0
    z = json.loads((owner.priv / "chat.json").read_text())["zulip"]
    assert (z["stream"], z["sched_stream"], z["dm_stream"]) == ("ea", "ea-scheduling", "ea-dm")
    assert z["notify"] == {"health": {"stream": "ea-health", "topic": "alerts"},
                           "personal": {"stream": "ea"}}
    out = capsys.readouterr().out
    assert "dm_stream     = ea-dm" in out and "notify.personal.stream = ea" in out


def test_the_chat_seed_types_back_a_notify_stream(owner, monkeypatch):
    _chat_tree(owner, notify={"health": {"stream": "ea-health"}})
    typed = iter(["zulip", "https://z.mine", "hale.stavros17@example.com", "8", "ea",
                  "other-stream"])
    monkeypatch.setattr(ot, "_interactive", lambda: True)
    monkeypatch.setattr(ot, "_prompt", lambda q: next(typed))
    assert _seed(owner, "--chat") == 1
    assert not (owner.priv / "chat.json").exists()


WEB_URL = "https://host.tail0000.ts.net:8444/chat"
WEB_LOGIN = "owner@example.test"
WEB_FLAGS = ["--chat-frontend", "web", "--chat-public-url", WEB_URL, "--chat-port", "8470",
             "--chat-tailscale-login", WEB_LOGIN]


def _web_tree(owner, web: dict, **top):
    block = {"frontend": "web", "web": web, **top}
    _write(owner.tree / "config" / "ea-config.json", json.dumps({"chat": block}), 0o644)


def test_the_chat_seed_confirms_the_web_chat_values(owner, monkeypatch, capsys):
    """The web block says which address the bot answers to, whose login is
    the owner and where the pairing token lives; ea-bot can write its copy,
    so each value is typed back or passed with its flag."""
    token = str(owner.priv / "pair.token")
    _web_tree(owner, {"public_url": WEB_URL, "port": 8470, "tailscale_login": WEB_LOGIN,
                      "pair_token_file": token})
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--chat") == 1
    captured = capsys.readouterr()
    assert f"web.public_url = {WEB_URL}" in captured.out
    for flag in ("--chat-frontend", "--chat-public-url", "--chat-port",
                 "--chat-tailscale-login", "--chat-pair-token-file"):
        assert flag in captured.err, flag
    assert not (owner.priv / "chat.json").exists()
    # one value typed differently: nothing is written
    monkeypatch.setattr(ot, "_interactive", lambda: True)
    typed = iter(["web", WEB_URL, "8470", "someone.else@example.test", token])
    monkeypatch.setattr(ot, "_prompt", lambda q: next(typed))
    assert _seed(owner, "--chat") == 1
    assert not (owner.priv / "chat.json").exists()
    typed = iter(["web", WEB_URL, "8470", WEB_LOGIN, token])
    assert _seed(owner, "--chat") == 0
    block = json.loads((owner.priv / "chat.json").read_text())
    assert block["frontend"] == "web"
    assert block["web"] == {"public_url": WEB_URL, "port": 8470, "tailscale_login": WEB_LOGIN,
                            "pair_token_file": token}
    out = capsys.readouterr().out
    for line in (f"web.public_url = {WEB_URL}", "web.port      = 8470",
                 f"web.tailscale_login = {WEB_LOGIN}", f"web.pair_token_file = {token}"):
        assert line in out, line
    for key, _, _ in ot.CHAT_WEB:
        assert ot._help(key), key


def test_a_web_value_from_a_flag_wins_and_unset_ones_take_the_defaults(owner, monkeypatch,
                                                                       capsys):
    _web_tree(owner, {"public_url": "https://elsewhere.example:8444/chat",
                      "tailscale_login": "someone.else@example.test"})
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--chat", *WEB_FLAGS) == 0
    web = json.loads((owner.priv / "chat.json").read_text())["web"]
    assert (web["public_url"], web["port"], web["tailscale_login"]) == (WEB_URL, 8470, WEB_LOGIN)
    assert "pair_token_file" not in web
    assert "web.pair_token_file = (the default, beside the store)" in capsys.readouterr().out


def test_the_chat_seed_refuses_a_web_store_or_token_in_the_residents_tree(owner, monkeypatch,
                                                                         capsys):
    """The bot serves the web chat only from a store and a token outside the
    resident's tree; the seed refuses such a block before anything is
    asked, whether ea-bot's copy or a flag names the path."""
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    base = {"public_url": WEB_URL, "port": 8470, "tailscale_login": WEB_LOGIN}
    no_port = WEB_FLAGS[:4] + WEB_FLAGS[6:]
    for planted, flags in (({"store_path": str(owner.tree / "state" / "webchat.sqlite")},
                            WEB_FLAGS),
                           ({"pair_token_file": str(owner.tree / "webchat-pair.token")},
                            WEB_FLAGS),
                           ({"pair_token_file": "webchat-pair.token"}, WEB_FLAGS),
                           ({"port": "8470; other"}, no_port)):
        _web_tree(owner, {**base, **planted})
        assert _seed(owner, "--chat", *flags) == 1, planted
        assert "NOT written: web." in capsys.readouterr().err
        assert not (owner.priv / "chat.json").exists()
    _web_tree(owner, base)
    assert _seed(owner, "--chat", *WEB_FLAGS,
                 "--chat-pair-token-file", str(owner.tree / "token")) == 1
    assert "inside" in capsys.readouterr().err
    # beside a Zulip primary the web shadow takes the same rule
    _write(owner.tree / "config" / "ea-config.json", json.dumps({"chat": {
        "frontend": "zulip", "shadow": "web", "zulip": {"site": "https://z.mine"},
        "web": {**base, "store_path": str(owner.tree / "w.sqlite")}}}), 0o644)
    assert _seed(owner, "--chat") == 1
    assert "web.store_path" in capsys.readouterr().err
    assert not (owner.priv / "chat.json").exists()
    # the owner-private directory is where the token belongs
    _web_tree(owner, base)
    assert _seed(owner, "--chat", *WEB_FLAGS,
                 "--chat-pair-token-file", str(owner.priv / "pair.token")) == 0


def test_the_seed_and_the_web_chat_name_the_same_resident_tree(owner):
    assert chat.untrusted_roots() == ot.resident_roots()
    assert owner.tree.resolve() in ot.resident_roots()
    assert ot.inside_roots(owner.tree / "state" / "x", ot.resident_roots()) == \
        owner.tree.resolve()
    assert ot.inside_roots(owner.priv / "x", ot.resident_roots()) is None


def test_a_notify_flag_needs_a_key():
    with pytest.raises(ValueError):
        ot.parse_notify_flags(["ea-health"])
    assert ot.parse_notify_flags(["health=ea-health"]) == {"notify.health.stream": "ea-health"}


def test_the_calendar_seed_needs_the_id_typed_back(owner, monkeypatch, capsys):
    _write(owner.tree / "config" / "scheduling-preferences.json",
           json.dumps({"ea_managed_calendar": {"calendar_id": "cal-1@example.com"}}), 0o644)
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--calendar") == 1
    assert not (owner.priv / "calendars.json").exists()
    assert ot._seed_calendar(owner.priv, owner.tree, required=False) == 0   # the bare seed notes it
    assert not (owner.priv / "calendars.json").exists()
    assert _seed(owner, "--calendar", "--calendar-id", "cal-1@example.com") == 0
    assert json.loads((owner.priv / "calendars.json").read_text()) == \
        {"ea_managed_calendar_id": "cal-1@example.com"}


def test_an_owner_reads_webhooks_only_from_the_private_copy(owner, monkeypatch):
    shared = _write(owner.tree / "config" / "slack_webhooks.json",
                    json.dumps({"personal": "https://hooks.attacker.example/P"}), 0o644)
    monkeypatch.setattr(chat_notify, "WEBHOOKS_FILE", shared)
    assert chat_notify._webhooks_path() == owner.priv / "slack_webhooks.json"
    assert chat_notify._load_webhooks() == {}
    _write(owner.priv / "slack_webhooks.json", json.dumps({"personal": "https://hooks.mine/P"}))
    assert chat_notify._load_webhooks() == {"personal": "https://hooks.mine/P"}
    (owner.priv / "slack_webhooks.json").chmod(0o622)       # writable by others: refused
    assert chat_notify._load_webhooks() == {}


def test_seed_refuses_a_legacy_file_that_fails_the_contract(owner, tmp_path, capsys):
    planted = _write(tmp_path / "planted", "ZULIP_API_KEY=planted\n", 0o600)
    (owner.tree / ".env.secrets").symlink_to(planted)
    assert _seed(owner, "--secrets") == 1
    assert not (owner.priv / "secrets.env").exists()
    (owner.tree / ".env.secrets").unlink()
    _write(owner.tree / ".env.secrets", "ZULIP_API_KEY=x\n", 0o660)
    assert _seed(owner, "--secrets") == 1
    assert not (owner.priv / "secrets.env").exists()


def test_seed_does_not_follow_a_symlinked_config(owner, tmp_path):
    other = _write(tmp_path / "other.json", json.dumps({"chat": {"frontend": "zulip"}}), 0o600)
    (owner.tree / "config" / "ea-config.json").symlink_to(other)
    assert _seed(owner, "--chat") == 1
    assert not (owner.priv / "chat.json").exists()


# ── the root step and the switch script, read as text ─────────────────

HARDEN = (ROOT / "deploy" / "harden-permissions.sh").read_text()
SWITCH = (ROOT / "scripts" / "zulip_switch.sh").read_text()


def _dropin(unit: str) -> str:
    start = HARDEN.index(f"/etc/systemd/system/{unit}.service.d/harden.conf <<EOF")
    return HARDEN[start:HARDEN.index("\nEOF\n", start)]


def test_harden_points_both_units_at_owner_private_files_only():
    bot, med = _dropin("ea-chat-bot"), _dropin("ea-mail-mediator")
    for d in (bot, med):
        assert "EnvironmentFile=\n" in d                      # the list is reset first
        assert ".env.secrets" not in d and "$SEC" not in d and "$EA_TREE" not in d
    assert "EnvironmentFile=$BOT_ENV" in bot and "EnvironmentFile=-$OWNER_SECRETS" in bot
    assert "$MEDIATOR_ENV" in med
    assert 'MEDIATOR_ENV="EnvironmentFile=-$OWNER_SECRETS"' in HARDEN
    assert "GRAPH_" in HARDEN[HARDEN.index("MEDIATOR_ENV=\"\""):HARDEN.index('MEDIATOR_ENV="EnvironmentFile')]
    assert 'PRIV="$OWNER_HOME/.ea-private"' in HARDEN and 'OWNER_SECRETS="$PRIV/secrets.env"' in HARDEN


def test_harden_seeds_and_fixes_modes_as_the_owner():
    assert "runuser -u \"$OWNER\"" in HARDEN
    assert 'as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" seed --ea-dir "$EA_TREE"' in HARDEN
    assert 'as_owner "$VENV_PY" "$REPO_DIR/preprocessor/owner_trust.py" token-modes --ea-dir "$EA_TREE"' in HARDEN
    # the seed comes before any unit is changed, and a failed seed stops the run
    assert HARDEN.index("owner_trust.py\" seed") < HARDEN.index("ea-chat-bot.service.d/harden.conf <<EOF")
    assert "the owner-private seed failed" in HARDEN


def test_zulip_switch_writes_both_copies_and_reads_the_key_by_the_rule():
    assert "grep '^ZULIP_API_KEY=' \"$SECRETS\"" not in SWITCH
    assert '>> "$SECRETS"' not in SWITCH
    assert 'bot_key() { $PY "$OWNER_TRUST" secret ZULIP_API_KEY; }' in SWITCH
    body = SWITCH[SWITCH.index("set_chat() {"):SWITCH.index("count_key()")]
    assert "/home/ea-bot/.ea/config/ea-config.json" in body and "$OWNER_CHAT" in body
    for call in ("set_chat frontend slack", "set_chat zulip.site \"$SITE\"", "set_chat frontend zulip"):
        assert call in SWITCH
    assert SWITCH.count('d["chat"]["frontend"] = ') == 0            # no one-copy flip left


def test_seed_copies_the_display_name_once_and_the_owner_reads_it(owner, capsys):
    assert ot.owner_profile_name() is None                          # no file yet
    _write(owner.tree / "config" / "user-profile.json", json.dumps({"name": "Robin Vale"}), 0o644)
    assert _seed(owner, "--profile", "--profile-name", "Robin Vale") == 0
    assert "name = Robin Vale" in capsys.readouterr().out
    target = owner.priv / "profile.json"
    assert json.loads(target.read_text()) == {"name": "Robin Vale"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert ot.owner_profile_name() == "Robin Vale"
    # ea-bot rewriting its copy changes nothing owner-side
    _write(owner.tree / "config" / "user-profile.json", json.dumps({"name": "Other"}), 0o644)
    assert _seed(owner, "--profile") == 0
    assert ot.owner_profile_name() == "Robin Vale"


def test_the_profile_seed_needs_the_name_confirmed(owner, monkeypatch, capsys):
    """ea-bot writes user-profile.json, and the name is the one the outbound
    review lets onto an outside poll: it is typed back or passed as a flag."""
    _write(owner.tree / "config" / "user-profile.json", json.dumps({"name": "Mallory Planted"}),
           0o644)
    monkeypatch.setattr(ot, "_interactive", lambda: False)
    assert _seed(owner, "--profile") == 1
    assert "--profile-name" in capsys.readouterr().err
    assert not (owner.priv / "profile.json").exists()
    assert ot._seed_profile(owner.priv, owner.tree) == 0            # the bare seed notes it
    assert not (owner.priv / "profile.json").exists()
    monkeypatch.setattr(ot, "_interactive", lambda: True)
    monkeypatch.setattr(ot, "_prompt", lambda q: "Robin Vale")
    assert _seed(owner, "--profile") == 1                           # typed differently
    assert not (owner.priv / "profile.json").exists()
    monkeypatch.setattr(ot, "_prompt", lambda q: "Mallory Planted")
    assert _seed(owner, "--profile") == 0
    assert ot.owner_profile_name() == "Mallory Planted"


def test_the_profile_seed_never_fails_the_root_step(owner, capsys):
    assert _seed(owner, "--profile") == 0                           # no user-profile.json
    assert not (owner.priv / "profile.json").exists()
    _write(owner.tree / "config" / "user-profile.json", json.dumps({"email": "a@b.example"}), 0o644)
    assert _seed(owner, "--profile") == 0
    assert not (owner.priv / "profile.json").exists()


def test_a_resident_process_has_no_owner_profile_name(owner, monkeypatch):
    _write(owner.priv / "profile.json", json.dumps({"name": "Robin Vale"}))
    assert ot.owner_profile_name() == "Robin Vale"
    monkeypatch.setattr(ot, "is_owner_process", lambda: False)
    assert ot.owner_profile_name() is None


def test_every_trusted_value_is_explained_before_it_is_asked(monkeypatch, capsys):
    """The operator is told what each value is and what a wrong one does."""
    keys = [k for k, _, _ in ot.CHAT_TRUSTED] + [
        "stream", "sched_stream", "dm_stream", "notify.health.stream",
        "ea_managed_calendar_id", "ea_managed_account", "name"]
    for k in keys:
        assert ot._help(k), k
    monkeypatch.setattr(ot, "_interactive", lambda: True)
    monkeypatch.setattr(ot, "_prompt", lambda q: "cal-1")
    got = ot._confirm("calendar", [("ea_managed_calendar_id", "cal-1")],
                               [("ea_managed_calendar_id", "cal-1", "--calendar-id")], {})
    out = capsys.readouterr().out
    assert got == {"ea_managed_calendar_id": "cal-1"}
    assert "EA-Managed calendar" in out and "Why it matters" in out and "every 30 minutes" in out
