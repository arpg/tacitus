#!/usr/bin/env python3
"""The plugin trust and bot fixes from the release review.

A locked plugin is read only through the owner's lock, whatever its live
manifest says; the install policy comes only from the owner-private file;
an adopted agent-zone plugin needs explicit consent; a tool grant has one
shape; the context grants are part of the accepted manifest; a card token
lives owner-private on a mediated install and a resident executor's check
goes through the mediator; the dash rewrite leaves code spans alone; a
plugin card is claimed once; the session registry writes under a lock.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO))
# A sandboxed EA_DIR makes this process a resident one for resident_io, so
# its writes act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-review-fixes-")

import plugin_cards as C  # noqa: E402
import plugin_install as I  # noqa: E402
import plugin_manifest as PM  # noqa: E402
import plugin_registry as R  # noqa: E402

BOT_SRC = (REPO / "ea-chat-bot.py").read_text()


def _v2(name: str, **extra) -> dict:
    m = {"manifest_version": 2, "name": name, "zone": "agent",
         "capabilities": {"chat": {"skill": "skill.md", "keywords": ["go"]},
                          "schedules": [{"label": "daily", "cron": "0 1 * * *"}]}}
    m.update(extra)
    return m


def _lock_with(name: str, manifest: dict, enabled: bool = True) -> dict:
    norm, errors = PM.normalize(manifest, name)
    assert not errors, errors
    summary = PM.capability_summary(norm)
    digest = PM.capability_digest(summary)
    return {"lock_version": 1, "plugins": {name: {
        "name": name, "zone": "agent", "enabled": enabled, "manifest": norm,
        "capability_digest": digest, "accepted": {"digest": digest, "how": "accepted"},
        "files": {}}}}


# ── a locked directory is never a v1 plugin ───────────────────────────

def test_a_v1_rewrite_of_a_locked_plugin_is_ignored(tmp_path, monkeypatch):
    pdir = tmp_path / "plugins"
    d = pdir / "locked-one"
    d.mkdir(parents=True)
    (d / "skill.md").write_text("skill")
    # The resident downgrades the manifest to v1 with a chat trigger.
    (d / "plugin.json").write_text(json.dumps(
        {"name": "locked-one", "enabled": True, "slack": {"skill": "skill.md", "keywords": ["x"]}}))
    lock = _lock_with("locked-one", _v2("locked-one"), enabled=False)
    monkeypatch.setattr(R, "managed_plugins", lambda lock=None, **k: [])
    R._CACHE["sig"] = None
    found = R.discover_interactive_plugins(pdir, lock=lock)
    assert [p.name for p in found] == []
    assert R.plugin_tool_grants(pdir, lock=lock) == []
    # With no lock at all the same directory is an ordinary v1 plugin.
    R._CACHE["sig"] = None
    assert [p.name for p in R.discover_interactive_plugins(pdir)] == ["locked-one"]


def test_schedule_config_follows_the_lock_for_a_locked_name(monkeypatch):
    lock = _lock_with("locked-one", _v2("locked-one"), enabled=False)
    live_v1 = {"name": "locked-one", "enabled": True, "schedule": "0 1 * * *"}
    monkeypatch.setattr(R, "get_plugin", lambda name, lock=None, **k: None)
    assert R.schedule_config(live_v1, (), name="locked-one", lock=lock)["enabled"] is False
    assert R.schedule_config({"name": "free-one", "enabled": True}, (), name="free-one",
                             lock=lock)["enabled"] is True


def test_locked_names_lists_every_entry_enabled_or_not():
    lock = _lock_with("a", _v2("a"), enabled=False)
    assert R.locked_names(lock) == {"a"}
    assert R.locked_names({"plugins": {}}) == set()
    assert R.locked_names({"junk": 1}) == set()


# ── the install policy has one source ─────────────────────────────────

def test_policy_never_falls_back_to_the_residents_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ea-config.json").write_text(json.dumps({"plugins": {"trusted_sources": ["*"]}}))
    monkeypatch.setattr(R, "EA_DIR", tmp_path)
    monkeypatch.setattr(R, "_is_owner", lambda: False)
    assert R.install_policy() == {"trusted_sources": [], "blocked_sources": []}
    monkeypatch.setattr(R, "_is_owner", lambda: True)
    monkeypatch.setattr(R._owner_trust, "private_dir", lambda: tmp_path / "nowhere")
    assert R.install_policy() == {"trusted_sources": [], "blocked_sources": []}


# ── adopt needs consent for the agent zone ────────────────────────────

def test_adopting_an_agent_zone_plugin_needs_explicit_consent(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    agent = tmp_path / "plugins"
    d = agent / "mine"
    d.mkdir(parents=True)
    (d / "skill.md").write_text("skill")
    (d / "plugin.json").write_text(json.dumps(_v2("mine")))
    out = []
    ctx = I.Context(private=private, agent_dir=agent, ea_dir=tmp_path / "ea", out=out.append)
    policy = {"trusted_sources": ["*"], "blocked_sources": []}
    with pytest.raises(I.PluginError) as e:
        I.adopt(ctx, "mine", zone="agent", accept=False, policy=policy)
    assert e.value.code == I.EXIT_CONSENT
    entry = I.adopt(ctx, "mine", zone="agent", accept=True, policy=policy)
    assert entry["accepted"]["how"] in ("accepted", "trusted-source")
    assert ctx.read_lock()["plugins"]["mine"]["enabled"] is True


# ── one shape of tool grant ───────────────────────────────────────────

@pytest.mark.parametrize("grant", [
    "Bash(python3 {plugin_dir}/../../bin/x *)", "Bash(*)", "Bash(bash {plugin_dir}/x.sh)",
    "Read({plugin_dir}/*)", "Bash(python3 {plugin_dir}/x.py; rm -rf /)",
])
def test_a_tool_grant_outside_the_one_shape_is_an_error(grant):
    _norm, errors = PM.normalize(_v2("p", capabilities={
        "chat": {"skill": "skill.md", "keywords": ["go"]}, "tools": [grant]}), "p")
    assert any("capabilities.tools" in e for e in errors), errors


def test_the_usual_grant_passes():
    _norm, errors = PM.normalize(_v2("p", capabilities={
        "chat": {"skill": "skill.md", "keywords": ["go"]},
        "tools": ["Bash(python3 {plugin_dir}/*.py *)", "Bash(python3 {plugin_dir}/collect.py)"]}), "p")
    assert errors == []


# ── context grants ride in the accepted manifest ──────────────────────

def test_context_grants_are_normalised_and_summarised_only_when_present():
    plain, errors = PM.normalize(_v2("p"), "p")
    assert errors == [] and plain["context"] == {}
    assert "context" not in PM.capability_summary(plain)
    withctx, errors = PM.normalize(_v2("p", context={
        "ledger_read": "own", "ledger_write": False, "graph_actions": ["search-mail", "get-message"],
        "fetch_url": True, "google_calendar": False}), "p")
    assert errors == []
    assert withctx["context"] == {"ledger_read": "own", "fetch_url": True,
                                  "graph_actions": ["get-message", "search-mail"]}
    summary = PM.capability_summary(withctx)
    assert summary["context"] == withctx["context"]
    assert PM.capability_digest(summary) != PM.capability_digest(PM.capability_summary(plain))
    assert any("search-mail" in line for line in PM.describe(summary))
    _n, errors = PM.normalize(_v2("p", context={"ledger_read": "all", "graph_actions": ["X Y"]}), "p")
    assert len(errors) == 2


# ── card tokens ───────────────────────────────────────────────────────

def test_the_token_file_is_owner_private_on_a_mediated_install(tmp_path, monkeypatch):
    monkeypatch.setattr(C._owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(C._owner_trust, "private_dir", lambda: tmp_path / "private")
    monkeypatch.setattr(C, "mediated_install", lambda: True)
    assert C.token_path() == tmp_path / "private" / C.TOKEN_FILE
    monkeypatch.setattr(C, "mediated_install", lambda: False)
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    assert C.token_path() == tmp_path / "ea" / "state" / C.TOKEN_FILE
    # An explicit directory wins either way.
    assert C.token_path(tmp_path / "x") == tmp_path / "x" / "state" / C.TOKEN_FILE


def test_a_resident_executor_asks_the_mediator(monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stdout = '{"ok": true}\n'

    def run(argv, **kw):
        seen["argv"] = argv
        return _P()

    monkeypatch.setattr(C, "_is_resident", lambda: True)
    monkeypatch.setattr(C, "mediated_install", lambda: True)
    monkeypatch.setattr(C.subprocess, "run", run)
    assert C.consume_token("tok", "p", "c", {"x": 1}) is True
    argv = seen["argv"]
    assert argv[1].endswith("plugin_cards.py") and argv[2] == C.CONSUME_VERB
    assert argv[argv.index("--action") + 1] == C.action_of("p", "c", {"x": 1})
    assert argv[argv.index("--token") + 1] == "tok"
    _P.returncode, _P.stdout = 1, '{"ok": false}\n'
    assert C.consume_token("tok", "p", "c", {"x": 1}) is False


def test_the_cli_takes_a_token_once_as_the_owner(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state" / C.TOKEN_FILE
    raw = C.mint_token("p", "c", {"x": 1}, path=path)
    monkeypatch.setattr(C, "token_path", lambda ea_dir=None: path)

    class _MM:
        @staticmethod
        def forward_or_none(tool, argv):
            return None

        @staticmethod
        def check_child_parse(parser, args):
            return None

    monkeypatch.setitem(sys.modules, "mail_mediation", _MM)
    action = C.action_of("p", "c", {"x": 1})
    assert C.main([C.CONSUME_VERB, "--action", action, "--token", raw]) == 0
    assert C.main([C.CONSUME_VERB, "--action", action, "--token", raw]) == 1
    assert C.main([C.CONSUME_VERB, "--action", "card:bad", "--token", raw]) == 2
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[0]) == {"ok": True} and json.loads(out[1]) == {"ok": False}


def test_the_mediator_lists_the_verb():
    import mail_mediation as M
    import mail_mediator as MM
    assert M.SAFE["plugin_cards"] == {"consume-token"}
    assert MM.TOOLS["plugin_cards"].name == "plugin_cards.py"
    assert M.is_safe("plugin_cards", "consume-token")
    assert not M.is_safe("plugin_cards", "mint-token")


# ── the bot ───────────────────────────────────────────────────────────

def test_the_bot_mints_without_naming_a_store_and_claims_each_card_once():
    i = BOT_SRC.index("def _run_plugin_card(")
    body = BOT_SRC[i:BOT_SRC.index("\n\n\n", i)]
    assert "_pcards.mint_token(lp.name, spec[\"id\"], item)" in body
    assert "TOKEN_FILE" not in body
    for handler in ("def handle_approve_plugin_card(", "def handle_cancel_plugin_card("):
        j = BOT_SRC.index(handler)
        hb = BOT_SRC[j:BOT_SRC.index("\n\n\n", j)]
        assert "_reject_if_stale_confirmation(ev" in hb
        assert "if not _claim_plugin_card(ev):" in hb


def test_a_plugin_card_claim_is_one_shot():
    start = BOT_SRC.index("_PLUGIN_CARD_CLAIMS: set")
    end = BOT_SRC.index("@app.action(\"plugin_card_approve\")", start)

    class _Log:
        def info(self, *a, **k):
            pass

    ns = {"threading": threading, "log": _Log()}
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[claims]", "exec"), ns)

    class _Card:
        channel, id = "C1", "m1"

    class _Ev:
        card = _Card()

    assert ns["_claim_plugin_card"](_Ev()) is True
    assert ns["_claim_plugin_card"](_Ev()) is False


def test_the_dash_rewrite_leaves_code_spans_alone():
    ns = {"re": re, "html": __import__("html")}
    import ast
    tree = ast.parse(BOT_SRC)
    picked = []
    for node in tree.body:
        names = set()
        if isinstance(node, ast.FunctionDef):
            names = {node.name}
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    names |= {e.id for e in t.elts if isinstance(e, ast.Name)}
        if any(("dash" in n.lower() or "mask" in n.lower() or n.isupper()
                or n.startswith("_TIME")) for n in names):
            picked.append(node)
    for _ in range(3):
        for node in picked:
            try:
                exec(compile(ast.Module([node], []), "x", "exec"), ns)
            except Exception:
                pass
    f = ns["_replace_dashes"]
    for text in ("Run <code>git log --oneline</code> now",
                 "Use `--flag` here", "<pre>a -- b</pre>",
                 "```\nmake --jobs 4\n```"):
        assert f(text) == text, text
    assert f("See `--flag` -- then stop") == "See `--flag`, then stop"


def test_the_rotation_holds_the_old_session_lock_and_journals():
    start = BOT_SRC.index("def _maybe_rotate_room_session(")
    end = BOT_SRC.index("# ---- Verify before you assert", start)
    body = BOT_SRC[start:end]
    assert "old_lock.acquire(blocking=False)" in body
    assert "old_lock.release()" in body
    assert "_journal_room_rotation(room, old, new, size)" in body
    assert "project_store.py" in body
    i = BOT_SRC.index("with _room_rotate_lock(channel):\n            if not sched_lane:")
    j = BOT_SRC.index("got_session_lock = session_lock.acquire(blocking=False)", i)
    assert j - i < 900


def test_the_reply_check_second_pass_carries_the_code_evidence():
    start = BOT_SRC.index("def _reply_check_prompt(")
    body = BOT_SRC[start:BOT_SRC.index("\n\n\n", start)]
    ns: dict = {}
    exec(compile(body, "ea-chat-bot.py[prompt]", "exec"),
         {"EA_BOT_SCRIPT_DIR": "/t", **ns}, ns)
    code = {"results": [{"kind": "ledger", "claim": "X is submitted", "ref": "x-1",
                         "outcome": "contradicted", "why": "status open"},
                        {"kind": "mail", "claim": "They replied", "ref": "c",
                         "outcome": "held", "why": ""}]}
    text = ns["_reply_check_prompt"]({"ledger": ["X is submitted"], "mail": ["They replied"],
                                      "negative": []}, code)
    assert "X is submitted: ledger read of x-1 found status open" in text
    assert "They replied" in text and "found" not in text.split("They replied")[-1][:40]


# ── the session registry writes under a lock ──────────────────────────

def test_registry_writers_do_not_lose_each_others_rows(tmp_path, monkeypatch):
    import slack_sessions as ss
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setattr(ss, "REGISTRY", tmp_path / "state" / "slack-sessions.json")
    threads = [f"t-{i}" for i in range(40)]
    workers = [threading.Thread(target=ss.lookup, args=(t,)) for t in threads]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    reg = json.loads(ss.REGISTRY.read_text())
    assert set(reg) == set(threads)
    assert len({r["session_id"] for r in reg.values()}) == 40
    sid = reg["t-0"]["session_id"]
    ss.remember("t-0", title="first")
    ss.mark_used("t-1")
    assert ss.describe("t-0")["meta"]["title"] == "first"
    assert ss.lookup("t-0")[0] == sid
    assert "_atomic" not in [n for n in dir(ss) if n == "_atomic"]


def test_a_registry_the_resident_cannot_read_is_never_written(tmp_path, monkeypatch):
    import slack_sessions as ss
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"t-x": {"session_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12}}))
    os.symlink(victim, state / "slack-sessions.json")
    monkeypatch.setattr(ss, "REGISTRY", state / "slack-sessions.json")
    sid, resumable, meta = ss.lookup("t-x")
    assert not resumable and meta == {}
    assert json.loads(victim.read_text())["t-x"]["session_id"].startswith("00000000")
    assert ss.set_title("t-x", "nope") is False



class _NoTerminal:
    def isatty(self):
        return False


class _NoAccounts:
    def get_accounts(self):
        return []


def test_the_graph_sign_in_command_runs_with_no_terminal(monkeypatch):
    """A `!` command in Claude Code has no terminal. The library refuses an
    interactive sign-in there (a headless subprocess would hang on it), but
    graph_client.py's own command line is a sign-in the user asked for, and
    its flows read nothing from the keyboard."""
    msal = pytest.importorskip("msal")  # noqa: F841
    import graph_client as gc
    monkeypatch.setattr(gc.sys, "stdin", _NoTerminal())
    monkeypatch.setattr(gc, "_app", _NoAccounts())
    monkeypatch.setattr(gc, "_source_token", lambda: None)
    monkeypatch.setattr(gc, "_use_confidential", lambda: False)
    monkeypatch.setattr(gc, "_interactive_seed", lambda *a, **k: "signed-in")
    monkeypatch.setattr(gc, "_published_seed", lambda *a, **k: "signed-in")
    monkeypatch.setattr(gc, "_SIGN_IN_REQUESTED", False)
    with pytest.raises(RuntimeError, match="no refresh token"):
        gc.authenticate()
    monkeypatch.setattr(gc, "_SIGN_IN_REQUESTED", True)
    assert gc.authenticate() == "signed-in"
    src = (REPO / "preprocessor" / "graph_client.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("_SIGN_IN_REQUESTED = True") < main.index("token = authenticate()")


def test_the_bot_names_a_missing_chat_setting():
    src = (REPO / "ea-chat-bot.py").read_text()
    assert "try:\n    FRONTEND = _chat.load_frontend()\nexcept KeyError as _missing:\n    sys.exit(" in src
    assert "the chat setting {_missing.args[0]} is not set" in src


def test_harden_counts_a_unit_up_only_when_its_process_stays():
    """Type=simple reads active the moment a crashing bot restarts; the
    check holds the same MainPID for 15s before it reports the unit up."""
    harden = (REPO / "deploy" / "harden-permissions.sh").read_text()
    loop = harden[harden.index("for svc in $UNITS; do"):]
    assert 'pid="$(systemctl show -p MainPID --value "$svc"' in loop
    assert 'state="restarting"' in loop and 'journalctl -u "$svc" -n 6' in loop
    assert 'UNITS="${UNITS:+$UNITS }ea-mail-mediator"' in harden
    assert "BOT_RETIRE=1" in harden and "the bot is not hardened yet" in harden



def _bot_function(name: str, ns: dict):
    """One function of ea-chat-bot.py, run in ns (its decorator left out)."""
    import ast
    src = (REPO / "ea-chat-bot.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.get_source_segment(src, node), "ea-chat-bot.py", "exec"), ns)
            return ns[name]
    raise AssertionError(f"no function {name}")


class _NoModalFrontend:
    def __init__(self):
        self.posts = []

    def supports(self, what):
        return False

    def post(self, channel, text, *, thread_ts=None, unfurl=True):
        self.posts.append((channel, thread_ts, text))
        return types.SimpleNamespace(id=f"m{len(self.posts)}")

    def permalink(self, channel, msg_id):
        return f"https://chat.example/{channel}/{msg_id}"


def test_an_edit_tap_on_a_chip_card_answers_under_the_card():
    """With no modal, the question goes to the conversation that owns the
    draft, and a line under the card links there, so the tap is answered
    where the owner made it."""
    fe = _NoModalFrontend()
    ns = {"FRONTEND": fe, "_verify_button_user": lambda ev: True,
          "_delivery_channel": lambda key: "stream:1",
          "_card_value": lambda ev: {"draft_id": "d1"},
          "_fetch_draft_body": lambda did: {"subject": "Lab visit", "body": "x"},
          "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
    handle = _bot_function("handle_edit_draft", ns)
    card = types.SimpleNamespace(thread_key="stream:1:topic:lab", channel="stream:9", id="77")
    handle(types.SimpleNamespace(card=card))
    (ch1, key1, ask), (ch2, key2, pointer) = fe.posts
    assert (ch1, key1) == ("stream:1", "stream:1:topic:lab")
    assert "What should I change in the draft to *Lab visit*?" in ask and "new Send" in ask
    assert (ch2, key2) == ("stream:9", "77")
    assert "https://chat.example/stream:1/m1" in pointer


def test_an_edit_tap_in_the_conversation_itself_posts_once():
    fe = _NoModalFrontend()
    ns = {"FRONTEND": fe, "_verify_button_user": lambda ev: True,
          "_delivery_channel": lambda key: "stream:9",
          "_card_value": lambda ev: {"draft_id": "d1"},
          "_fetch_draft_body": lambda did: {"error": "offline"},
          "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
    handle = _bot_function("handle_edit_draft", ns)
    card = types.SimpleNamespace(thread_key="", channel="stream:9", id="77")
    handle(types.SimpleNamespace(card=card))
    assert len(fe.posts) == 1 and "What should I change in the draft?" in fe.posts[0][2]
    assert fe.posts[0][:2] == ("stream:9", "77"), "asked under the card"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
