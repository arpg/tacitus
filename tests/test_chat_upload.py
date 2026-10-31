#!/usr/bin/env python3
"""preprocessor/chat_upload.py: the LLM-runnable upload and thread-post CLI.

Two paths. With no credential reachable (the resident, as ea-bot) the job
is staged through slack_outbox exactly as slack_upload always did: the old
slack_upload is executed from git and both staged jobs are compared.
With a credential the job goes through the configured frontend, checked
on the memory frontend. The credential lookup follows the owner_trust
secrets rule, each case in a sandbox EA tree and private dir.
"""
import importlib.util
import io
import json
import subprocess
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import chat  # noqa: E402
import chat_upload  # noqa: E402
import ea_config  # noqa: E402
import owner_trust  # noqa: E402
import slack_outbox  # noqa: E402
import slack_upload  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402

BASE_COMMIT = "28a3633"
SC = slack_outbox.SIDECAR_SUFFIX


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """No credentials anywhere; EA_DIR, the private dir and the config
    point at tmp. The private dir does not exist, and the sandbox tree is
    not the owner's install tree, so the process is not an owner process."""
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    cfg = ea / "config" / "ea-config.json"
    cfg.write_text("{}")
    private = tmp_path / "private"
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(private))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    for name in owner_trust.SECRET_KEYS:
        monkeypatch.delenv(name, raising=False)
    nowhere = [tmp_path / "none" / n for n in (".env.secrets", ".env")]
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    return types.SimpleNamespace(ea=ea, cfg=cfg, private=private, src=src,
                                 nowhere=nowhere, tmp=tmp_path)


def _fresh():
    """chat_upload imported anew once the sandbox environment is set, so a
    location it computes at import time lands in the sandbox as well as one
    it computes at call time."""
    spec = importlib.util.spec_from_file_location(
        "chat_upload_fresh", REPO / "preprocessor" / "chat_upload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def _run(main, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["chat_upload.py", *argv])
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main()
    return rc, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _old_upload(sandbox):
    try:
        src = subprocess.run(["git", "-C", str(REPO), "show",
                              f"{BASE_COMMIT}:preprocessor/slack_upload.py"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{BASE_COMMIT} not available in this checkout")
    mod = types.ModuleType("old_slack_upload")
    exec(compile(src, "old_slack_upload", "exec"), mod.__dict__)
    mod.SECRET_FILES = sandbox.nowhere
    return mod


def _drain_outbox():
    """The staged jobs, as comparable data, then clear the outbox."""
    d = slack_outbox.outbox_dir()
    jobs = []
    for sidecar in sorted(d.glob("*" + SC)):
        meta = json.loads(sidecar.read_text())
        paired = Path(str(sidecar)[:-len(SC)])
        jobs.append((meta, paired.read_bytes() if paired.is_file() else None,
                     paired.name.split(".", 1)[1] if paired.is_file() else None))
        sidecar.unlink()
        if paired.is_file():
            paired.unlink()
    return jobs


def _normalise(lines):
    return [{k: v for k, v in line.items() if k not in ("staged", "sidecar")}
            for line in lines]


@pytest.mark.parametrize("argv", [
    ["--channel", "C0EXAMPLE01", "--file", "{src}", "--thread-ts", "1779902187.528899",
     "--comment", "the report", "--title", "Report"],
    ["--channel", "C0EXAMPLE01", "--thread-ts", "1779902187.528899", "--text", "done"],
    ["--channel", "C0EXAMPLE01", "--thread-ts", "1779902187.528899", "--comment", "c only"],
])
def test_staging_path_unchanged(sandbox, monkeypatch, argv):
    argv = [a.format(src=sandbox.src) for a in argv]
    old = _old_upload(sandbox)
    rc_old, out_old = _run(old.main, argv, monkeypatch)
    jobs_old = _drain_outbox()
    rc_new, out_new = _run(chat_upload.main, argv, monkeypatch)
    jobs_new = _drain_outbox()
    assert rc_old == rc_new == 0
    assert _normalise(out_old) == _normalise(out_new)
    assert [set(line) for line in out_old] == [set(line) for line in out_new]
    assert jobs_old == jobs_new and jobs_new


def test_staging_requires_thread(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["chat_upload.py", "--channel", "C1",
                                      "--file", str(sandbox.src)])
    assert chat_upload.main() == 1
    assert "--thread-ts is required" in capsys.readouterr().err


def test_bad_thread_key_rejected(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["chat_upload.py", "--channel", "C1",
                                      "--thread-ts", "next monday", "--text", "x"])
    with pytest.raises(SystemExit) as e:
        chat_upload.main()
    assert e.value.code == 2


def test_zulip_thread_key_stages(sandbox, monkeypatch):
    key = "stream:12:topic:Hello%20there"
    rc, out = _run(chat_upload.main, ["--channel", "stream:12", "--thread-ts", key,
                                      "--text", "hi"], monkeypatch)
    assert rc == 0 and out[0]["thread_ts"] == key
    (job,) = _drain_outbox()
    assert job[0]["thread_ts"] == key


def _memory(monkeypatch):
    fe = MemoryFrontend()
    seen = {}

    def load(**kw):
        seen.update(kw)
        return fe
    monkeypatch.setattr(chat, "load_frontend", load)
    return fe, seen


def test_credential_path_calls_the_frontend(sandbox, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    fe, seen = _memory(monkeypatch)
    second = sandbox.tmp / "b.png"
    second.write_bytes(b"png")
    rc, out = _run(chat_upload.main, ["--channel", "C9", "--file", str(sandbox.src),
                                      "--file", str(second), "--thread-ts", "1.2",
                                      "--comment", "first only"], monkeypatch)
    assert rc == 0
    assert seen["for_cli"] is True and seen["env"]["SLACK_BOT_TOKEN"] == "xoxb-test"
    assert [(u["channel"], u["thread_ts"], u["filename"], u["title"], u["comment"])
            for u in fe.uploads] == [("C9", "1.2", "report.pdf", "report.pdf", "first only"),
                                     ("C9", "1.2", "b.png", "b.png", "")]
    assert out == [{"file_id": "F1", "permalink": "memory://files/F1", "filename": "report.pdf"},
                   {"file_id": "F2", "permalink": "memory://files/F2", "filename": "b.png"}]
    assert list(slack_outbox.outbox_dir().glob("*" + SC)) == []

    rc, out = _run(chat_upload.main, ["--channel", "C9", "--thread-ts", "1.2",
                                      "--text", "hello"], monkeypatch)
    assert rc == 0
    assert fe.posts[-1]["channel"] == "C9" and fe.posts[-1]["text"] == "hello"
    assert fe.posts[-1]["thread_ts"] == "1.2"
    assert out == [{"ts": fe.posts[-1]["id"], "thread_ts": "1.2"}]


TEXT_JOB = ["--channel", "C9", "--thread-ts", "1.2", "--text", "x"]


def test_credential_from_the_legacy_secrets_file(sandbox, monkeypatch):
    """The checked legacy .env.secrets supplies the credential; ~/.ea/.env
    supplies the Slack settings, as data."""
    _write(sandbox.ea / ".env.secrets", 'SLACK_BOT_TOKEN="xoxb-file"\n')
    _write(sandbox.ea / ".env", "EA_SLACK_CHANNEL_ID=C0F\nEA_SLACK_CHANNEL_NAME=ea\n"
                                "EA_SLACK_OWNER_ID=U1\nPATH=/x\n", mode=0o664)
    fe, seen = _memory(monkeypatch)
    rc, _ = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert rc == 0 and fe.posts
    env = seen["env"]
    assert env["SLACK_BOT_TOKEN"] == "xoxb-file"
    assert (env["EA_SLACK_CHANNEL_ID"], env["EA_SLACK_CHANNEL_NAME"],
            env["EA_SLACK_OWNER_ID"]) == ("C0F", "ea", "U1")
    assert env.get("PATH") != "/x"


def test_credential_from_the_private_file_for_an_owner_process(sandbox, monkeypatch):
    sandbox.private.mkdir(mode=0o700)
    _write(sandbox.private / owner_trust.SECRETS_FILE, "SLACK_BOT_TOKEN=xoxb-private\n")
    monkeypatch.setattr(owner_trust, "install_ea_dir", lambda: sandbox.ea)
    fe, seen = _memory(monkeypatch)
    rc, _ = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert rc == 0 and fe.posts
    assert seen["env"]["SLACK_BOT_TOKEN"] == "xoxb-private"


def test_private_file_is_read_only_by_an_owner_process(sandbox, monkeypatch):
    """A process that is not the owner of the install (here its EA tree is
    not the owner's) takes nothing from the private secrets.env, even a
    file it owns; the job is staged."""
    sandbox.private.mkdir(mode=0o700)
    _write(sandbox.private / owner_trust.SECRETS_FILE, "SLACK_BOT_TOKEN=xoxb-private\n")
    fe, _ = _memory(monkeypatch)
    rc, out = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and not fe.posts
    _drain_outbox()


def test_private_file_is_checked_on_the_descriptor(sandbox, monkeypatch, capsys):
    """A group-writable private secrets.env supplies no secret, even when a
    clean file takes its place before a check made by path afterwards. The
    owner process then has no credential, and the outbox refuses to stage
    for it, so nothing is sent."""
    sandbox.private.mkdir(mode=0o700)
    target = _write(sandbox.private / owner_trust.SECRETS_FILE,
                    "SLACK_BOT_TOKEN=xoxb-loose\n", mode=0o660)
    monkeypatch.setattr(owner_trust, "install_ea_dir", lambda: sandbox.ea)
    real_check = owner_trust.check_owner_file

    def swap_then_check(path, **kw):
        if Path(path) == target:
            clean = _write(sandbox.private / "clean", "SLACK_BOT_TOKEN=xoxb-clean\n")
            clean.replace(target)
        return real_check(path, **kw)
    monkeypatch.setattr(owner_trust, "check_owner_file", swap_then_check)
    fe, _ = _memory(monkeypatch)
    rc, out = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert not fe.posts and out == []
    assert rc == 1 and "owner process sends directly" in capsys.readouterr().err


@pytest.mark.parametrize("frontend,channel", [("slack", "C9"), ("zulip", "stream:1")])
def test_dotenv_never_supplies_a_secret(sandbox, monkeypatch, frontend, channel):
    """~/.ea/.env passes every ownership check here (this user's file, mode
    600) and still supplies no credential: the job is staged."""
    sandbox.cfg.write_text(json.dumps({"chat": {"frontend": frontend}}))
    _write(sandbox.ea / ".env", "SLACK_BOT_TOKEN=xoxb-dotenv\nZULIP_API_KEY=zkey-dotenv\n"
                                "EA_SLACK_CHANNEL_ID=C0F\n")
    fe, _ = _memory(monkeypatch)
    rc, out = _run(_fresh().main, ["--channel", channel, "--thread-ts", "1.2",
                                   "--text", "x"], monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and not fe.posts
    _drain_outbox()


def test_zulip_credential_probe(sandbox, monkeypatch):
    sandbox.cfg.write_text(json.dumps({"chat": {"frontend": "zulip"}}))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-ignored")
    fe, seen = _memory(monkeypatch)
    rc, out = _run(chat_upload.main, ["--channel", "stream:1", "--thread-ts", "1.2",
                                      "--text", "x"], monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and not fe.posts   # staged
    _drain_outbox()
    monkeypatch.setenv("ZULIP_API_KEY", "zkey")
    rc, out = _run(chat_upload.main, ["--channel", "stream:1", "--thread-ts", "1.2",
                                      "--text", "x"], monkeypatch)
    assert rc == 0 and fe.posts and seen["env"]["ZULIP_API_KEY"] == "zkey"


def test_frontend_error_is_reported(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class _Broken(MemoryFrontend):
        def post(self, *a, **k):
            raise chat.ChatError("not_in_channel")
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: _Broken())
    monkeypatch.setattr(sys, "argv", ["chat_upload.py", "--channel", "C9",
                                      "--thread-ts", "1.2", "--text", "x"])
    assert chat_upload.main() == 1
    assert "not_in_channel" in capsys.readouterr().err


def test_slack_upload_is_a_shim():
    assert slack_upload.main is chat_upload.main


def test_legacy_secret_only_from_an_owner_file(sandbox, monkeypatch):
    """A legacy .env.secrets that other users can write, or a symlink at its
    name (even to a clean file), supplies no secret; the job is staged."""
    legacy = sandbox.ea / ".env.secrets"
    fe, _ = _memory(monkeypatch)

    _write(legacy, "SLACK_BOT_TOKEN=xoxb-loose\n", mode=0o664)
    rc, out = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and not fe.posts
    _drain_outbox()

    legacy.unlink()
    legacy.symlink_to(_write(sandbox.tmp / "clean", "SLACK_BOT_TOKEN=xoxb-clean\n"))
    rc, out = _run(_fresh().main, TEXT_JOB, monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and not fe.posts
    _drain_outbox()


# ── the web chat alone ("frontend": "web") ────────────────────────────

def _web_bot(store):
    """The bot's web front end on the store at `store`, its EA channel and
    DM made, with a thread the owner opened in the DM."""
    from chat.web_frontend import WebFrontend
    bot = WebFrontend.from_config({}, store_path=store, writer_only=True)
    bot.start()
    bot.store.upsert_channel("main", "ea", "main")
    bot.store.upsert_channel("dm", "dm", "dm")
    return bot, bot.post("dm", "send me the report")


def test_a_resident_upload_reaches_the_web_bot_through_the_outbox(sandbox, monkeypatch):
    """The resident cannot open the owner's store, and the copy it could
    open is one the bot never reads: it stages the job, and the bot's drain
    posts it through its own web front end, in the channel that holds the
    thread."""
    sandbox.cfg.write_text(json.dumps({"chat": {"frontend": "web"}}))
    bot, asked = _web_bot(sandbox.tmp / "owner" / "webchat.sqlite")
    built = []
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: built.append(kw))
    rc, out = _run(chat_upload.main, ["--channel", "dm", "--thread-ts", asked.thread_key,
                                      "--file", str(sandbox.src), "--comment", "the report"],
                   monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued" and built == []
    rc, out = _run(chat_upload.main, ["--channel", "dm", "--thread-ts", asked.thread_key,
                                      "--text", "done"], monkeypatch)
    assert rc == 0 and out[0]["status"] == "queued"
    assert not list(sandbox.ea.rglob("webchat.sqlite"))

    # the bot's drain (resident_router_loop): the channel comes from the
    # store, never from the job
    def upload(channel, thread_ts, file, filename, title, initial_comment):
        bot.upload(bot.thread_channel(thread_ts) or channel, file, thread_ts=thread_ts,
                   filename=filename, title=title, comment=initial_comment or "")

    def post(channel, thread_ts, text):
        bot.post(bot.thread_channel(thread_ts) or channel, text, thread_ts=thread_ts)
    results = slack_outbox.drain(upload, channel=bot.main_channel, post_fn=post)
    assert sorted(r["status"] for r in results) == ["sent", "sent"]
    thread = bot.store.thread("dm", asked.thread_key)
    assert thread[0]["text"] == "send me the report"
    assert sorted((m["author"], m["kind"], m["text"]) for m in thread[1:]) == [
        ("bot", "file", "the report"), ("bot", "message", "done")]
    (sent,) = [m for m in thread if m["kind"] == "file"]
    assert sent["file"]["name"] == "report.pdf"
    assert Path(bot.store.file(sent["file"]["id"])["path"]).read_bytes() == b"%PDF-1.4 test"
    assert bot.store.threads("main") == []
    assert list(slack_outbox.outbox_dir().glob("*" + SC)) == []


def test_an_owner_upload_writes_the_web_store_directly(sandbox, monkeypatch):
    """An owner process holds the store the bot serves: the upload goes in
    at once, and nothing is staged."""
    sandbox.private.mkdir(mode=0o700)
    _write(sandbox.private / owner_trust.CHAT_FILE, json.dumps({"frontend": "web"}))
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    bot, asked = _web_bot(sandbox.private / "webchat.sqlite")
    rc, out = _run(chat_upload.main, ["--channel", "dm", "--thread-ts", asked.thread_key,
                                      "--file", str(sandbox.src)], monkeypatch)
    assert rc == 0 and out[0]["filename"] == "report.pdf"
    thread = bot.store.thread("dm", asked.thread_key)
    assert [m["kind"] for m in thread] == ["message", "file"]
    assert list(slack_outbox.outbox_dir().glob("*" + SC)) == []
