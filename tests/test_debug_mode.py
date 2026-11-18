#!/usr/bin/env python3
"""Debug mode (the chat block's `diagnostics`, docs/webchat.md, Debug mode).

These pin: the switch is off on a fresh install and on one from before
it, and an owner process reads it from its own chat.json alone; a post
marked diagnostic reaches no frontend and no quiet-hours queue while it
is off, on the memory, Slack and web paths, and returns True so a cursor
moves on; an alert reaches the user either way, on the web chat in the
EA channel while the health room is out of the drawer; each poster is
classified, and every poster to the health key or the digest place is.
"""
import ast
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO))

import chat_notify  # noqa: E402
import ea_config  # noqa: E402
import owner_trust  # noqa: E402
import quiet_hours as real_quiet_hours  # noqa: E402
import slack_notifier  # noqa: E402
from preprocessor import chat_notify as pkg_chat_notify  # noqa: E402

HEALTH_HOOK = "https://hooks.example/H"
DIGEST_HOOK = "https://hooks.example/D"


class _Posts:
    """requests.post on the Slack path: the URL of each call."""

    def __init__(self):
        self.urls = []

    def __call__(self, url, **kw):
        self.urls.append(url)
        return types.SimpleNamespace(status_code=200, ok=True, text="ok")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch ea-config.json (the memory frontend, debug mode as the
    template has it) read by a process that is not the owner's, webhooks for
    the Slack path, and a quiet-hours gate that holds nothing."""
    cfg = tmp_path / "ea-config.json"
    hooks = tmp_path / "slack_webhooks.json"
    hooks.write_text(json.dumps({"ea-assistant": "https://hooks.example/A",
                                 "health": HEALTH_HOOK, "mail-digest": DIGEST_HOOK}))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    monkeypatch.setattr(chat_notify, "WEBHOOKS_FILE", hooks)
    monkeypatch.setattr(pkg_chat_notify, "WEBHOOKS_FILE", hooks)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    qh = types.SimpleNamespace(should_defer=lambda priority: False, defer=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "quiet_hours", qh)
    for mod in (chat_notify, pkg_chat_notify):
        mod.MEMORY_POSTS.clear()

    def chat(**block) -> None:
        cfg.write_text(json.dumps({"chat": {"frontend": "memory", **block}}))
    chat()
    return types.SimpleNamespace(tmp=tmp_path, cfg=cfg, chat=chat, qh=qh)


def _sent() -> list[tuple[str, str]]:
    """What the memory frontend took, through either name of the notifier."""
    return [(k, t) for k, t, _b in chat_notify.MEMORY_POSTS + pkg_chat_notify.MEMORY_POSTS]


# ── the switch ────────────────────────────────────────────────────────

def test_debug_mode_is_off_on_a_fresh_install_and_on_one_from_before_the_switch(env):
    assert ea_config.DEFAULTS["chat"]["diagnostics"] is False
    template = json.loads((REPO / "config" / "ea-config.json").read_text())
    assert template["chat"]["diagnostics"] is False
    env.cfg.write_text("{}")                                    # no chat block at all
    assert chat_notify.diagnostics_on() is False
    env.cfg.write_text(json.dumps({"chat": {"frontend": "slack", "web": {"port": 8470}}}))
    assert chat_notify.diagnostics_on() is False                # a block from before the switch
    for value, on in ((True, True), ("true", False), (1, False), (False, False), (None, False)):
        env.chat(diagnostics=value)
        assert chat_notify.diagnostics_on() is on, value


def test_an_owner_process_reads_debug_mode_from_its_own_chat_json_alone(env, monkeypatch):
    private = env.tmp / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)

    def own(block: dict) -> None:
        (private / "chat.json").write_text(json.dumps(block))
        (private / "chat.json").chmod(0o600)
    own({"frontend": "memory"})
    env.chat(diagnostics=True)                                  # the resident's copy says on
    assert chat_notify.diagnostics_on() is False
    own({"frontend": "memory", "diagnostics": True})
    assert chat_notify.diagnostics_on() is True


# ── the gate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("debug", [False, True])
def test_a_diagnostic_posts_only_in_debug_mode_and_an_alert_always(env, capsys, debug):
    env.chat(diagnostics=debug)
    assert chat_notify.post("health", "*Remote LLM report*: 3 kept", diagnostic=True) is True
    assert chat_notify.post_digest("mail-digest", "*Held back*: a note from Avery Example",
                                   diagnostic=True) is True
    assert chat_notify.post("health", ":rotating_light: the bot is not listening") is True
    assert chat_notify.post_digest("list-digest", "*robo-list digest*") is True
    alerts = [("health", ":rotating_light: the bot is not listening"),
              ("list-digest", "*robo-list digest*")]
    out = capsys.readouterr().out
    if debug:
        assert _sent() == [("health", "*Remote LLM report*: 3 kept"),
                           ("mail-digest", "*Held back*: a note from Avery Example"), *alerts]
        assert "[debug mode off]" not in out
    else:
        assert _sent() == alerts
        # one line each in the cron log, and no word of the post: it can carry mail
        assert out.count("[debug mode off] diagnostic for ") == 2
        assert "Remote LLM" not in out and "Avery" not in out


def test_on_slack_a_diagnostic_with_debug_mode_off_is_no_post(env, monkeypatch):
    posts = _Posts()
    monkeypatch.setattr(requests, "post", posts)
    env.cfg.write_text(json.dumps({"chat": {"frontend": "slack"}}))
    key = slack_notifier.health_channel_key()
    assert key == "health"
    assert slack_notifier.post_to_slack(key, "an eval line", diagnostic=True) is True
    assert chat_notify.post_digest("mail-digest", "the mail held back", diagnostic=True) is True
    assert posts.urls == []
    # an alert goes to the health webhook as before
    assert slack_notifier.post_to_slack(key, "the sign-in broke", priority="urgent", _bypass_quiet=True)
    assert posts.urls == [HEALTH_HOOK]
    env.cfg.write_text(json.dumps({"chat": {"frontend": "slack", "diagnostics": True}}))
    assert slack_notifier.post_to_slack(key, "an eval line", diagnostic=True) is True
    assert chat_notify.post_digest("mail-digest", "the mail held back", diagnostic=True) is True
    assert posts.urls == [HEALTH_HOOK, HEALTH_HOOK, DIGEST_HOOK]


def test_the_command_line_marks_a_post_as_a_diagnostic(env):
    assert chat_notify.main(["post", "--key", "health", "--text", "a review line",
                             "--diagnostic"]) == 0
    assert chat_notify.main(["post", "--key", "health", "--text", "an alert"]) == 0
    assert _sent() == [("health", "an alert")]


# ── quiet hours ───────────────────────────────────────────────────────

def test_quiet_hours_hold_no_diagnostic_while_debug_mode_is_off(env):
    held = []
    env.qh.should_defer = lambda priority: True
    env.qh.defer = lambda *a, **kw: held.append((a, kw))
    assert chat_notify.post("health", "a review line", diagnostic=True) is True
    assert held == [] and _sent() == []
    assert chat_notify.post("health", "a rule you did not set") is True   # an alert waits as before
    assert held == [(("health", "a rule you did not set", "normal"), {})]
    env.chat(diagnostics=True)
    assert chat_notify.post("health", "a review line", diagnostic=True) is True
    assert held[-1] == (("health", "a review line", "normal"), {"diagnostic": True})


def test_a_flush_after_debug_mode_went_off_drops_the_held_diagnostics(env, monkeypatch):
    queue = [{"ts_utc": "2026-11-18T09:00:00+00:00", "channel_key": "health",
              "text": "a review line", "priority": "normal", "diagnostic": True},
             {"ts_utc": "2026-11-18T09:05:00+00:00", "channel_key": "health",
              "text": "a rule you did not set", "priority": "normal"}]
    qh = real_quiet_hours
    dropped, posted = [], []
    monkeypatch.setattr(qh, "_load_queue", lambda: [dict(i) for i in queue])
    monkeypatch.setattr(qh, "_drop_from_queue", lambda items: dropped.extend(items))
    monkeypatch.setattr(qh, "active_timezone", lambda: "UTC")
    monkeypatch.setattr(qh.slack_notifier, "post_to_slack",
                        lambda key, text, **kw: posted.append((key, text)) or True)
    assert qh.slack_notifier.chat_notify.diagnostics_on() is False
    assert qh.flush_if_clear(force=True) == 1
    assert len(posted) == 1 and "a rule you did not set" in posted[0][1]
    assert "a review line" not in posted[0][1]
    assert sorted(i["text"] for i in dropped) == ["a review line", "a rule you did not set"]
    # in debug mode both go out
    env.chat(diagnostics=True)
    dropped.clear()
    posted.clear()
    assert qh.flush_if_clear(force=True) == 2 and "a review line" in posted[0][1]


# ── the web chat ──────────────────────────────────────────────────────

def test_on_the_web_chat_an_alert_goes_to_the_ea_channel_while_the_health_room_is_hidden(
        env, monkeypatch):
    import chat
    from chat import web_frontend as wf
    private = env.tmp / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(env.tmp / "ea"))
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [])

    def block(debug: bool, health: str = "r5") -> None:
        chat_notify._ROOM_FE.clear()
        (private / "chat.json").write_text(json.dumps(
            {"frontend": "web", "diagnostics": debug,
             "web": {"port": 0, "notify": {"health": {"channel": health}}}}))
        (private / "chat.json").chmod(0o600)
    store = wf.WebStore(private / "webchat.sqlite")
    store.upsert_channel("r5", "ea-health", "room")

    def texts(cid: str) -> list[str]:
        return [m["text"] for m in store.threads(cid)]
    block(False)
    assert chat_notify.post("health", "Outbound web review: nothing looked off.",
                            diagnostic=True, _bypass_quiet=True) is True
    assert chat_notify.post("health", "The Outlook sign-in stopped working.", _bypass_quiet=True)
    assert texts("r5") == [] and texts("main") == ["The Outlook sign-in stopped working."]
    block(True)
    assert chat_notify.post("health", "Outbound web review: nothing looked off.",
                            diagnostic=True, _bypass_quiet=True)
    assert chat_notify.post("health", "The Outlook sign-in works again.", _bypass_quiet=True)
    assert sorted(texts("r5")) == ["Outbound web review: nothing looked off.",
                                   "The Outlook sign-in works again."]
    # the health key named on a fixed place (the DM) stays there: the drawer never hides it
    block(False, health="dm")
    assert chat_notify.post("health", "The bot is not listening.", _bypass_quiet=True)
    assert texts("dm") == ["The bot is not listening."]
    assert len(texts("main")) == 1
    store.close()


# ── each poster ───────────────────────────────────────────────────────
# The classification docs/webchat.md (Debug mode) lists. "diagnostic": every
# post call carries diagnostic=True; "alert" and "content": none does;
# "both": the call decides by what the run found.

POSTERS = {
    "preprocessor/mail_gate.py": "diagnostic",          # the mail held back
    "preprocessor/remote_briefing.py": "diagnostic",    # the briefing preview
    "preprocessor/mail_privacy.py": "diagnostic",       # the remote model's report
    "preprocessor/invite_eval.py": "diagnostic",
    "preprocessor/ledger_reconcile.py": "diagnostic",
    "preprocessor/closure_policy.py": "diagnostic",
    "tests/release/run_release.py": "diagnostic",
    "tests/evals/run_full_if_changed.sh": "diagnostic",
    "scripts/outbound-review.py": "both",
    "preprocessor/sentinel.py": "alert",
    "scripts/boundary-audit.py": "alert",
    "preprocessor/plugin_registry.py": "alert",
    "preprocessor/ghost_week.py": "alert",
    "preprocessor/persistence_digest.py": "alert",
    "scripts/preferences-digest.py": "alert",
    "preprocessor/remote_digests.py": "content",        # the list and admissions digests
}
POST_VERBS = ("post", "post_to_slack", "post_digest")


def _post_calls(path: Path) -> list:
    """The `diagnostic` keyword of each notifier post call in a file: True,
    False, an expression (its source) or None when it has none."""
    if path.suffix == ".sh":
        calls = re.findall(r"post_to_slack\(([^\n]*)\)", path.read_text())
        return [True if "diagnostic=True" in c else None for c in calls]
    out = []
    src = path.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in POST_VERBS:
            kw = {k.arg: k.value for k in node.keywords}
            val = kw.get("diagnostic")
            out.append(None if val is None else val.value if isinstance(val, ast.Constant)
                       else ast.get_source_segment(src, val))
    return out


@pytest.mark.parametrize("name,kind", sorted(POSTERS.items()))
def test_each_poster_is_classified(name, kind):
    calls = _post_calls(REPO / name)
    assert calls, name
    if kind == "diagnostic":
        assert all(c is True for c in calls), (name, calls)
    elif kind == "both":
        assert calls and all(isinstance(c, str) for c in calls), (name, calls)
    else:
        assert all(c is None for c in calls), (name, calls)


def test_every_poster_to_the_health_key_or_the_digest_place_is_classified():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py", "*.sh"],
                         capture_output=True, text=True)
    files = out.stdout.split() if out.returncode == 0 else []
    if not files:  # the Docker image has no .git: the tree itself
        files = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*")
                 if p.suffix in (".py", ".sh") and p.is_file()
                 and not {".git", ".venv", "node_modules"} & set(p.parts)]
    if not files:
        pytest.skip("no source files found")
    found = set()
    for f in files:
        if f.startswith("tests/test_") or f in ("preprocessor/chat_notify.py",
                                                 "preprocessor/slack_notifier.py"):
            continue
        try:
            text = (REPO / f).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "health_channel_key()" in text or "post_digest(" in text:
            found.add(f)
    assert found == set(POSTERS), sorted(found ^ set(POSTERS))


def _script(name: str):
    mod_name = name.removesuffix(".py").replace("-", "_")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, REPO / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod   # dataclasses look their module up
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("debug", [False, True])
def test_the_posters_on_the_memory_frontend(env, monkeypatch, debug):
    """Each poster's own post helper, run: a diagnostic goes out in debug
    mode alone, an alert either way."""
    env.chat(diagnostics=debug)
    import closure_policy
    import ledger_reconcile
    import mail_gate
    import persistence_digest
    import plugin_registry
    import remote_briefing
    import sentinel
    sys.path.insert(0, str(REPO / "tests" / "release"))
    import run_release
    from datetime import datetime, timezone
    monkeypatch.setattr(sentinel.time, "sleep", lambda s: None)
    audit = _script("boundary-audit.py")
    monkeypatch.setattr(audit.time, "sleep", lambda s: None)

    diagnostics = [
        lambda: closure_policy._post(["Closure policy: a signal now proposes."]),
        lambda: ledger_reconcile._post(":broom: Reconcile: 3 rows in 1 group."),
        lambda: mail_gate._post_digest("*Mail held back*", datetime.now(timezone.utc)),
        lambda: remote_briefing._preview("*Morning briefing preview*"),
        lambda: run_release._alert(":red_circle: Release tests: red"),
    ]
    alerts = [
        lambda: sentinel._post_alert(":rotating_light: the bot is not listening"),
        lambda: audit._post(":shield: a boundary check fails"),
        lambda: plugin_registry._post_health("a plugin's files changed; it does not act"),
        lambda: persistence_digest._post(":mag: a change in the stores"),
    ]
    for call in diagnostics + alerts:
        call()
    texts = [t for _k, t in _sent()]
    assert len(texts) == (len(diagnostics) + len(alerts) if debug else len(alerts)), texts
    assert all(any(w in t for t in texts) for w in ("not listening", "boundary check",
                                                     "does not act", "change in the stores"))
