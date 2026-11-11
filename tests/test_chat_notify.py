#!/usr/bin/env python3
"""preprocessor/chat_notify.py: the notifier every cron agent and plugin uses.

These pin: the Slack webhook payloads are the same bytes the old
slack_notifier sent (the old function is executed from git and both
paths record requests.post); the quiet-hours gate defers before any
routing; the web chat maps a channel key to its channel and opens a new
thread per post; post_as_bot on Slack sends the fields scheduled_sends
always sent; the LLM grants name tools, never a wildcard.
"""
import ast
import io
import json
import os
import subprocess
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import chat_notify  # noqa: E402
import ea_config  # noqa: E402
import slack_notifier  # noqa: E402

BASE_COMMIT = "28a3633"


def _git_show(path: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_COMMIT}:{path}"],
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{BASE_COMMIT} not available in this checkout")


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = status == 200
        self._body = body if body is not None else {"ok": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _PostRecorder:
    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Resp(self.status, self.body)

    def wire(self):
        """What went over the wire: url, headers and the JSON body bytes."""
        return [(a, k.get("headers"), k.get("timeout"),
                 json.dumps(k.get("json"), ensure_ascii=False).encode())
                for a, k in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch config (slack by default), webhooks file and private dir."""
    cfg = tmp_path / "ea-config.json"
    cfg.write_text("{}")
    hooks = tmp_path / "slack_webhooks.json"
    hooks.write_text(json.dumps({"ea-assistant": "https://hooks.example/A",
                                 "personal": "https://hooks.example/P"}))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(chat_notify, "WEBHOOKS_FILE", hooks)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    qh = types.SimpleNamespace(should_defer=lambda priority: False,
                               defer=lambda *a: None)
    monkeypatch.setitem(sys.modules, "quiet_hours", qh)
    chat_notify.MEMORY_POSTS.clear()
    return types.SimpleNamespace(cfg=cfg, hooks=hooks, tmp=tmp_path, qh=qh)


def _set_chat(env, chat: dict):
    env.cfg.write_text(json.dumps({"chat": chat}))


def _old_notifier(env):
    """The pre-migration slack_notifier, executed from git."""
    mod = types.ModuleType("old_slack_notifier")
    exec(compile(_git_show("preprocessor/slack_notifier.py"), "old_slack_notifier", "exec"),
         mod.__dict__)
    mod.WEBHOOKS_FILE = env.hooks
    return mod


CASES = [
    ("ea-assistant", "plain *bold* text", None),
    ("personal", "one\n" + chat_notify.SPLIT_MARKER + "\ntwo :lock: é", None),
    ("ea-assistant", f"a{chat_notify.SPLIT_MARKER}  {chat_notify.SPLIT_MARKER}b",
     [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]),
    ("ea-assistant", "", None),
    ("ea-assistant", chat_notify.SPLIT_MARKER, [{"type": "divider"}]),
    ("personal", "with blocks", [{"type": "divider"}]),
]


@pytest.mark.parametrize("key,text,blocks", CASES)
def test_slack_webhook_payloads_byte_identical(env, monkeypatch, key, text, blocks):
    old = _old_notifier(env)
    rec_old, rec_new = _PostRecorder(), _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_old)
    r_old = old.post_to_slack(key, text, blocks=blocks)
    monkeypatch.setattr(requests, "post", rec_new)
    r_new = chat_notify.post(key, text, blocks=blocks)
    assert r_old == r_new
    assert rec_old.wire() == rec_new.wire()
    # The shim goes the same way.
    rec_shim = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_shim)
    assert slack_notifier.post_to_slack(key, text, blocks) == r_old
    assert rec_shim.wire() == rec_old.wire()


def test_slack_failures_match_old(env, monkeypatch):
    old = _old_notifier(env)
    for status in (500, 200):
        rec_old, rec_new = _PostRecorder(status), _PostRecorder(status)
        monkeypatch.setattr(requests, "post", rec_old)
        r_old = old.post_to_slack("nope", "x")          # unknown key
        r_old2 = old.post_to_slack("personal", "x")
        monkeypatch.setattr(requests, "post", rec_new)
        assert chat_notify.post("nope", "x") is r_old is False
        assert chat_notify.post("personal", "x") == r_old2 == (status == 200)
        assert rec_old.wire() == rec_new.wire()


def _private(env, hooks: dict):
    """The owner-private dir as an owner process finds it: chat.json (an
    owner process takes the chat block only from there) and the webhooks
    file, owner-only."""
    private = env.tmp / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    for name, doc in (("chat.json", {"frontend": "slack"}), ("slack_webhooks.json", hooks)):
        (private / name).write_text(json.dumps(doc))
        (private / name).chmod(0o600)
    return private


def test_private_dir_webhooks_win(env, monkeypatch):
    _private(env, {"personal": "https://hooks.example/PRIVATE"})
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    assert chat_notify.post("personal", "hi")
    assert rec.calls[0][0] == ("https://hooks.example/PRIVATE",)


def test_the_shim_reads_webhooks_through_the_same_lookup(env):
    """slack_notifier's webhook path and loader are chat_notify's: the
    owner-private copy."""
    assert slack_notifier._webhooks_path is chat_notify._webhooks_path
    private = _private(env, {"personal": "P"})
    assert slack_notifier._webhooks_path() == private / "slack_webhooks.json"
    assert slack_notifier._load_webhooks() == {"personal": "P"}


def test_quiet_hours_defers_before_routing(env, monkeypatch):
    deferred = []
    env.qh.should_defer = lambda priority: priority == "normal"
    env.qh.defer = lambda key, text, priority: deferred.append((key, text, priority))
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    assert chat_notify.post("ea-assistant", "night note") is True
    assert slack_notifier.post_to_slack("personal", "via shim") is True
    assert deferred == [("ea-assistant", "night note", "normal"),
                        ("personal", "via shim", "normal")]
    assert rec.calls == []
    # Urgent is not deferred by this gate, and _bypass_quiet skips it.
    assert chat_notify.post("ea-assistant", "urgent", priority="urgent")
    assert chat_notify.post("ea-assistant", "flush", _bypass_quiet=True)
    assert len(rec.calls) == 2 and len(deferred) == 2


def test_quiet_hours_gate_error_fails_open(env, monkeypatch):
    def boom(priority):
        raise RuntimeError("bad tz")
    env.qh.should_defer = boom
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_notify.post("ea-assistant", "x")
    assert "[quiet-hours] gate skipped (bad tz)" in out.getvalue()
    assert len(rec.calls) == 1


def test_quiet_hours_flush_calls_through_the_shim_name():
    import quiet_hours as qh
    assert qh.slack_notifier.post_to_slack is slack_notifier.post_to_slack
    assert qh.slack_notifier.SPLIT_MARKER == chat_notify.SPLIT_MARKER


def test_scheduling_channel_key(env):
    assert chat_notify.scheduling_channel_key() == "ea-assistant"
    env.hooks.write_text(json.dumps({"scheduling": "https://hooks.example/S"}))
    assert chat_notify.scheduling_channel_key() == "scheduling"
    assert slack_notifier.scheduling_channel_key() == "scheduling"


def test_a_block_that_names_zulip_posts_nothing(env, monkeypatch, capsys):
    """Zulip is no chat front end any more: a block that still names it
    posts nowhere, says so, and its notify map routes nothing."""
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    _set_chat(env, {"frontend": "zulip",
                    "zulip": {"notify": {"scheduling": {"stream": "ea"}}}})
    assert chat_notify.post("ea-assistant", "x") is False
    assert "unknown chat frontend 'zulip'" in capsys.readouterr().out
    assert chat_notify.post_digest("mail-digest", "x") is None
    assert chat_notify.post_as_bot("C1", "x") is False
    assert chat_notify.scheduling_channel_key() == "ea-assistant"
    assert rec.calls == [] and "zulip" not in ea_config.get_chat(env.cfg)


def test_llm_grants_per_frontend(env):
    grants = chat_notify.llm_tool_grants()
    assert grants.split(",") == ["mcp__claude_ai_Slack__slack_read_channel",
                                 "mcp__claude_ai_Slack__slack_read_thread",
                                 "mcp__claude_ai_Slack__slack_read_user_profile"]
    assert "*" not in grants
    for name in ("web", "memory"):
        _set_chat(env, {"frontend": name})
        assert chat_notify.llm_tool_grants() == ""


def test_cli_llm_grants(env):
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_notify.main(["llm-grants"]) == 0
    assert out.getvalue().strip() == chat_notify.SLACK_LLM_GRANTS


def test_cli_post_memory(env):
    _set_chat(env, {"frontend": "memory"})
    assert chat_notify.main(["post", "--key", "personal", "--text", "hello"]) == 0
    assert chat_notify.MEMORY_POSTS == [("personal", "hello", None)]


def _old_function(source: str, name: str) -> types.FunctionType:
    """One function from an old module's source, compiled alone."""
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"os": __import__("os")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), name, "exec"), ns)
    return ns[name]


def test_post_as_bot_matches_scheduled_sends(env, monkeypatch):
    old_post = _old_function(_git_show("preprocessor/scheduled_sends.py"), "_post_slack")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("EA_SLACK_CHANNEL_ID", "C0TEST")
    import scheduled_sends
    rec_old, rec_new = _PostRecorder(), _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_old)
    old_post(":warning: Scheduled send held - x")
    monkeypatch.setattr(requests, "post", rec_new)
    scheduled_sends._post_slack(":warning: Scheduled send held - x")
    assert rec_old.calls == rec_new.calls
    assert rec_new.calls == [(("https://slack.com/api/chat.postMessage",),
                              {"headers": {"Authorization": "Bearer xoxb-test"},
                               "json": {"channel": "C0TEST",
                                        "text": ":warning: Scheduled send held - x"},
                               "timeout": 10})]


def test_post_as_bot_slack_no_token_posts_nothing(env, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    reasons = []
    assert chat_notify.post_as_bot("C0TEST", "x", log=reasons.append) is False
    assert rec.calls == [] and reasons


def test_health_key_uses_the_health_channel_when_configured(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "web")
    monkeypatch.setattr(chat_notify, "_chat_cfg",
                        lambda: {"web": {"notify": {"health": {"channel": "r9"}}}})
    assert chat_notify.health_channel_key() == "health"
    assert slack_notifier.health_channel_key() == "health"


def test_health_key_falls_back_to_the_main_channel(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "web")
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: {"web": {"notify": {}}})
    assert chat_notify.health_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {})
    assert chat_notify.health_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {"health": "https://hooks.example/H"})
    assert chat_notify.health_channel_key() == "health"


def test_calendar_key_prefers_calendar_then_scheduling_then_main(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "web")
    # No `"frontend": "web"` in the block, so no Scheduling channel of its own.
    cfg = {"web": {"notify": {"calendar": {"channel": "sched"},
                              "scheduling": {"channel": "main"}}}}
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: cfg)
    assert chat_notify.calendar_channel_key() == "calendar"
    assert slack_notifier.calendar_channel_key() == "calendar"
    del cfg["web"]["notify"]["calendar"]
    assert chat_notify.calendar_channel_key() == "scheduling"
    cfg["web"]["notify"] = {}
    assert chat_notify.calendar_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {})
    assert chat_notify.calendar_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {"calendar": "https://hooks.example/C"})
    assert chat_notify.calendar_channel_key() == "calendar"


def test_calendar_reconciler_routes_through_the_calendar_key():
    root = Path(__file__).resolve().parent.parent
    src = (root / "preprocessor/calendar_reconciler.py").read_text()
    assert "calendar_channel_key()" in src and 'post_to_slack("personal"' not in src


def test_the_health_senders_route_through_the_health_key():
    root = Path(__file__).resolve().parent.parent
    for f in ("preprocessor/sentinel.py", "scripts/boundary-audit.py",
              "scripts/outbound-review.py", "scripts/preferences-digest.py",
              "tests/evals/run_full_if_changed.sh"):
        assert "health_channel_key()" in (root / f).read_text(), f


# ── the web chat alone ("frontend": "web") ────────────────────────────

WEB_LOGIN = "owner@example.test"


@pytest.fixture
def web_env(env, monkeypatch):
    """A web-only chat block in the owner-private chat.json, read by an
    owner process, with the store beside it; tailscaled is this process,
    so the stream below signs in with the login header."""
    import chat
    from chat import web_frontend as wf
    private = env.tmp / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(env.tmp / "ea"))
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [])
    monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: os.geteuid())
    if not wf.PROC_NET[0].exists():
        monkeypatch.setattr(wf, "peer_uid", lambda peer, local: os.geteuid())

    def block(web: dict) -> None:
        (private / "chat.json").write_text(json.dumps({"frontend": "web", "web": web}))
        (private / "chat.json").chmod(0o600)
    block({"port": 0, "tailscale_login": WEB_LOGIN})
    return types.SimpleNamespace(private=private, store=private / "webchat.sqlite",
                                 block=block, chat=chat, wf=wf, cfg=env.cfg)


class _WebStream:
    """The bot's event stream, opened as the owner before anything is
    posted, so what it reads went out live (the tail thread), not from a
    replay of the log."""

    def __init__(self, fe) -> None:
        import http.client
        self.conn = http.client.HTTPConnection("127.0.0.1", fe.port, timeout=5)
        self.conn.request("GET", f"{fe.base_path}/api/events",
                          headers={"Tailscale-User-Login": WEB_LOGIN})
        self.resp = self.conn.getresponse()
        while self.resp.fp.readline().decode().rstrip("\n") != "retry: 3000":
            pass

    def events(self, kind: str = "message", count: int = 1) -> list:
        got, cur = [], {}
        while len(got) < count:
            line = self.resp.fp.readline().decode().rstrip("\n")
            if line == "" and "data" in cur:
                if cur.get("event") == kind:
                    got.append(json.loads(cur["data"]))
                cur = {}
                continue
            key, _, val = line.partition(": ")
            if key in ("event", "data"):
                cur[key] = val
        return got

    def close(self) -> None:
        self.conn.close()


def test_a_web_only_post_lands_in_the_store_and_the_bots_stream_delivers_it(web_env):
    bot = web_env.chat.load_frontend(env={})
    assert bot.trusted_config is True and not bot.writer_only
    bot.start()
    stream = None
    try:
        stream = _WebStream(bot)
        text = "*Morning* briefing" + chat_notify.SPLIT_MARKER + "second part"
        assert chat_notify.post("ea-assistant", text, _bypass_quiet=True)
        first, second = stream.events(count=2)
        assert (first["channel"], first["author"], first["text"]) == ("main", "bot",
                                                                      "**Morning** briefing")
        assert (second["channel"], second["thread_key"], second["text"]) == \
            ("main", first["thread_key"], "second part")
        # post_as_bot ignores the Slack channel id and goes to the EA channel
        assert chat_notify.post_as_bot("C_IGNORED", ":warning: held")
        (alert,) = [m for m in web_env.wf.WebStore(web_env.store).threads("main")
                    if m["text"] == ":warning: held"]
        assert alert["author"] == "bot" and alert["thread_key"] != first["thread_key"]
    finally:
        if stream is not None:
            stream.close()
        bot.stop()


def test_a_web_notify_entry_names_the_channel_and_the_keys_follow_it(web_env, capsys):
    web_env.block({"notify": {"personal": {"channel": "dm"}, "health": {"channel": "r9"},
                              "scheduling": {"channel": "main"}}})
    assert chat_notify.post("personal", "a note for you", _bypass_quiet=True)
    store = web_env.wf.WebStore(web_env.store)
    (row,) = store.threads("dm")
    assert row["text"] == "a note for you" and store.channel("dm")["kind"] == "dm"
    # the thread is known as a notifier post (the thread-state rules read it)
    assert store.thread_origin("dm", row["thread_key"]) == "personal"
    # a room the bot never made posts nothing
    assert chat_notify.post("health", "x", _bypass_quiet=True) is False
    assert "'r9'" in capsys.readouterr().out and store.channel("r9") is None
    assert (chat_notify.scheduling_channel_key(), chat_notify.calendar_channel_key(),
            chat_notify.health_channel_key()) == ("scheduling", "scheduling", "health")
    # with no map, scheduling output goes to Scheduling while it is on
    web_env.block({})
    assert (chat_notify.scheduling_channel_key(), chat_notify.calendar_channel_key(),
            chat_notify.health_channel_key()) == ("scheduling", "scheduling", "ea-assistant")
    web_env.block({"sched": False})
    assert (chat_notify.scheduling_channel_key(), chat_notify.calendar_channel_key(),
            chat_notify.health_channel_key()) == ("ea-assistant",) * 3
    assert chat_notify.llm_tool_grants() == ""


def test_a_web_post_returns_its_thread_and_a_push_can_open_it(web_env, monkeypatch):
    key = chat_notify.post("ea-assistant", "a tip" + chat_notify.SPLIT_MARKER + "more",
                           _bypass_quiet=True)
    store = web_env.wf.WebStore(web_env.store)
    (row,) = store.threads("main")
    assert isinstance(key, str) and key == row["thread_key"]
    digest = chat_notify.post_digest("mail-digest", "kept mail")
    assert digest == store.threads(chat_notify.WEB_DIGEST_ID)[0]["thread_key"]
    assert chat_notify.post("list-digest", "list items")    # through post(): the key, truthy
    assert chat_notify.web_direct("a note") is True         # its own contract stays a bool
    from chat import direct
    url = direct.web_thread_url(key)
    assert url.startswith("/") and url.endswith(f"/#/main/{key}")
    assert direct.web_thread_url("t-nowhere") == "" and direct.web_thread_url("") == ""
    opened = []
    monkeypatch.setattr(web_env.wf.WebFrontend, "notify",
                        lambda self, title, body="", url="", tag="", **kw:
                        opened.append(url) or {"sent": 1})
    assert direct.web_notify("a tip", tag="usage-tip", urgency="normal", url=url) == {"sent": 1}
    direct.web_notify("an alert")
    assert opened[0] == url and opened[1].endswith("/") and "#" not in opened[1]


def test_scheduling_posts_land_in_scheduling_on_the_web_chat_alone(web_env):
    """The `scheduling` key goes to the Scheduling channel while the store
    holds it (the bot makes it at start, and archives it when the web
    block turns it off), else to the EA channel; a `web.notify` entry for
    the key still wins."""
    store = web_env.wf.WebStore(web_env.store)

    def texts(cid):
        return [r["text"] for r in store.threads(cid)]
    # before the bot made Scheduling: the EA channel
    assert chat_notify.post("scheduling", "Three times for the review.", _bypass_quiet=True)
    assert texts("main") == ["Three times for the review."] and store.channel("sched") is None
    store.upsert_channel("sched", "Scheduling", "sched")
    key = chat_notify.scheduling_channel_key()
    assert key == "scheduling"
    assert chat_notify.post(key, "The panel picked Tuesday.", _bypass_quiet=True)
    (row,) = store.threads("sched")
    assert row["text"] == "The panel picked Tuesday."
    assert store.thread_origin("sched", row["thread_key"]) == "scheduling"
    # other keys stay in the EA channel
    assert chat_notify.post("ea-assistant", "The weekly audit is green.", _bypass_quiet=True)
    assert texts("sched") == ["The panel picked Tuesday."]
    # an explicit map wins
    web_env.block({"port": 0, "notify": {"scheduling": {"channel": "main"}}})
    assert chat_notify.post("scheduling", "Mapped to the EA channel.", _bypass_quiet=True)
    assert "Mapped to the EA channel." in texts("main") and len(texts("sched")) == 1
    # an archived Scheduling (turned off) sends it back to the EA channel
    web_env.block({"port": 0})
    store.set_channel("sched", archived=True)
    assert chat_notify.post("scheduling", "While Scheduling is off.", _bypass_quiet=True)
    assert "While Scheduling is off." in texts("main") and len(texts("sched")) == 1


def test_a_web_post_from_a_process_that_is_not_the_owners_posts_nothing(web_env, monkeypatch,
                                                                        capsys):
    monkeypatch.setattr(web_env.chat.owner_trust, "is_owner_process", lambda: False)
    _set_chat(web_env, {"frontend": "web"})
    assert chat_notify.post("ea-assistant", "x", _bypass_quiet=True) is False
    assert chat_notify.post_as_bot("C1", "x") is False
    assert "store is the owner's" in capsys.readouterr().out
    assert not list(web_env.private.parent.rglob("webchat.sqlite"))


# ── mail digests ──────────────────────────────────────────────────────

def test_a_digest_on_slack_needs_its_own_webhook_and_skips_quiet_hours(env, monkeypatch):
    env.qh.should_defer = lambda priority: True
    env.qh.defer = lambda *a: pytest.fail("a digest was held in the resident's queue")
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    # no `mail-digest` webhook: no place for it, and nothing goes to ea-assistant
    assert chat_notify.post_digest("mail-digest", "kept mail") is None
    assert rec.calls == []
    env.hooks.write_text(json.dumps({"ea-assistant": "https://hooks.example/A",
                                     "mail-digest": "https://hooks.example/D"}))
    assert chat_notify.post_digest("list-digest", "list items") is True
    assert [a[0] for a, _k in rec.calls] == ["https://hooks.example/D"]
    assert chat_notify.post("admissions-digest", "a week") is True
    assert len(rec.calls) == 2


def test_a_digest_on_memory_is_recorded_and_elsewhere_has_no_place(env):
    _set_chat(env, {"frontend": "memory"})
    assert chat_notify.post_digest("briefing-preview", "preview") is True
    assert chat_notify.MEMORY_POSTS == [("briefing-preview", "preview", None)]
    _set_chat(env, {"frontend": "carrier-pigeon"})
    assert chat_notify.post_digest("mail-digest", "x") is None
