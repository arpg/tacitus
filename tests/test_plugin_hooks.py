#!/usr/bin/env python3
"""The core reads plugin capabilities: mediated owner verbs, verb aliases,
mail rules, network endpoints, the non-Claude schedule posts and the bot's
claim protocol for plugin cards."""
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT))

import closure_scrubber  # noqa: E402
import mail_mediation as M  # noqa: E402
import owner_trust  # noqa: E402
import plugin_llm  # noqa: E402
import plugin_registry  # noqa: E402
import plugin_verbs  # noqa: E402
import resident_netpolicy as NP  # noqa: E402


# ── mediation ─────────────────────────────────────────────────────────

def _fake_verbs(plugin, verb, lock=None):
    return {"get-x": {"class": "safe"}, "post-x": {"class": "gated"}}.get(verb)


def test_the_resident_forwards_plugin_verbs_and_the_owner_decides(monkeypatch):
    monkeypatch.setattr(plugin_registry, "owner_verb", _fake_verbs)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    assert M.is_safe("plugin:p", "anything") and not M.is_gated("plugin:p", "anything")
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    assert M.is_safe("plugin:p", "get-x") and not M.is_gated("plugin:p", "get-x")
    assert M.is_gated("plugin:p", "post-x") and not M.is_safe("plugin:p", "post-x")
    assert not M.is_safe("plugin:p", "undeclared") and not M.is_gated("plugin:p", "undeclared")
    assert M.is_allowed("plugin:p", "get-x", ["get-x"])
    # Core tools are untouched.
    assert M.is_safe("graph_actions", "search-mail") and M.is_gated("graph_actions", "send-draft")


def test_the_mediator_runs_a_plugin_verb_from_the_owners_lock(monkeypatch, tmp_path):
    import mail_mediator as MM
    script = tmp_path / "reader.py"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setattr(plugin_registry, "owner_verb_command",
                        lambda p, v, lock=None: ([sys.executable, str(script)], tmp_path, 30))
    seen = {}
    monkeypatch.setattr(MM, "_respond", lambda rid, out, err, rc: seen.update(out=out, err=err, rc=rc))
    for name in ("_audit", "_note_release"):
        monkeypatch.setattr(MM, name, lambda *a, **k: None)
    monkeypatch.setattr(MM, "_cleanup", lambda path: None)
    monkeypatch.setattr(MM, "_gate_filter", lambda tool, verb, argv, run_id, out: (out, ""))
    MM._execute("r1", "plugin:p", "get-x", ["get-x", "--days", "2"], "", ["get-x", "--days", "2"],
                None, [], [], str(tmp_path / "req.json"))
    assert seen["rc"] == 0 and json.loads(seen["out"]) == ["get-x", "--days", "2"]

    def refuse(p, v, lock=None):
        raise PermissionError("integrity check failed")
    monkeypatch.setattr(plugin_registry, "owner_verb_command", refuse)
    MM._execute("r2", "plugin:p", "get-x", ["get-x"], "", ["get-x"], None, [], [],
                str(tmp_path / "req.json"))
    assert seen["rc"] == 2 and "integrity check failed" in seen["err"]


def test_an_alias_runs_the_plugin_verb_and_a_core_verb_wins(monkeypatch, tmp_path):
    hint = tmp_path / "plugin-verbs.json"
    hint.write_text(json.dumps({"graph_actions": {
        "get-x": {"plugin": "p", "verb": "get-x"},
        "search-mail": {"plugin": "p", "verb": "get-x"},
        "BAD verb": {"plugin": "p", "verb": "get-x"}}}))
    assert plugin_registry.verb_aliases("graph_actions", hint_path=hint) == {
        "get-x": ("p", "get-x"), "search-mail": ("p", "get-x")}
    monkeypatch.setattr(plugin_registry, "verb_aliases",
                        lambda tool, lock=None, hint_path=None: {"get-x": ("p", "get-x"),
                                                                 "search-mail": ("p", "get-x")})
    assert plugin_verbs.forward_alias("graph_actions", ["search-mail", "--q", "x"]) is None
    assert plugin_verbs.forward_alias("graph_actions", ["list-mail"]) is None
    calls = []
    monkeypatch.setattr(M, "should_mediate", lambda: False)
    monkeypatch.setattr(plugin_registry, "_is_owner", lambda: True)
    monkeypatch.setattr(plugin_registry, "run_owner_verb",
                        lambda plugin, argv, lock=None: calls.append((plugin, argv)) or 0)
    with pytest.raises(SystemExit) as e:
        plugin_verbs.forward_alias("graph_actions", ["get-x", "--days", "3"])
    assert e.value.code == 0 and calls == [("p", ["get-x", "--days", "3"])]


def test_a_resident_without_a_mediator_cannot_run_plugin_verbs(monkeypatch):
    monkeypatch.setattr(M, "should_mediate", lambda: False)
    monkeypatch.setattr(plugin_registry, "_is_owner", lambda: False)
    with pytest.raises(SystemExit) as e:
        plugin_verbs.run("p", ["get-x"])
    assert e.value.code == 2


def test_owner_commands_get_the_core_on_their_path(tmp_path):
    env = plugin_registry.owner_command_env("p", tmp_path, {"PYTHONPATH": "/elsewhere", "HOME": "/h"})
    assert env["EA_PLUGIN_NAME"] == "p" and env["EA_PLUGIN_DIR"] == str(tmp_path)
    assert env["EA_CORE_DIR"] == str(ROOT) and env["HOME"] == "/h"
    assert env["PYTHONPATH"].split(":") == [str(ROOT / "preprocessor"), str(ROOT), "/elsewhere"]
    assert plugin_registry.owner_command_env("p", tmp_path, {})["PYTHONPATH"].split(":") == [
        str(ROOT / "preprocessor"), str(ROOT)]


def test_the_mediator_runs_a_plugin_verb_with_the_core_importable(monkeypatch, tmp_path):
    import mail_mediator as MM
    script = tmp_path / "reader.py"
    script.write_text("import json, os, sys\nimport owner_trust\n"
                      "print(json.dumps([os.environ['EA_PLUGIN_NAME'], os.environ['EA_CORE_DIR'],\n"
                      "                  os.path.dirname(owner_trust.__file__), os.getcwd()]))\n")
    monkeypatch.setattr(plugin_registry, "owner_verb_command",
                        lambda p, v, lock=None: ([sys.executable, str(script)], tmp_path, 30))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    seen = {}
    monkeypatch.setattr(MM, "_respond", lambda rid, out, err, rc: seen.update(out=out, err=err, rc=rc))
    for name in ("_audit", "_note_release", "_cleanup"):
        monkeypatch.setattr(MM, name, lambda *a, **k: None)
    monkeypatch.setattr(MM, "_gate_filter", lambda tool, verb, argv, run_id, out: (out, ""))
    MM._execute("r1", "plugin:p", "get-x", ["get-x"], "", ["get-x"], None, [], [],
                str(tmp_path / "req.json"))
    assert seen["rc"] == 0, seen["err"]
    assert json.loads(seen["out"]) == ["p", str(ROOT), str(ROOT / "preprocessor"), str(tmp_path)]


def test_call_alias_returns_the_verb_output_or_none(monkeypatch):
    monkeypatch.setattr(plugin_registry, "verb_aliases", lambda tool, lock=None, hint_path=None: {})
    assert plugin_verbs.call_alias("graph_actions", "get-x", ["--days", "1"]) is None
    monkeypatch.setattr(plugin_registry, "verb_aliases",
                        lambda tool, lock=None, hint_path=None: {"get-x": ("p", "get-y")})
    # An owner process runs the plugin's verb here, after the checks.
    monkeypatch.setattr(plugin_registry, "_is_owner", lambda: True)
    monkeypatch.setattr(M, "should_mediate", lambda: False)
    calls = []
    monkeypatch.setattr(plugin_registry, "owner_verb_process", lambda plugin, argv, lock=None: (
        calls.append((plugin, argv)) or subprocess.CompletedProcess(argv, 0, '{"ok": 1}', "")))
    assert plugin_verbs.call_alias("graph_actions", "get-x", ["--days", 1]) == (0, '{"ok": 1}', "")
    assert calls == [("p", ["get-y", "--days", "1"])]
    # Any other process runs it as the resident would: this script, then the mediator.
    monkeypatch.setattr(plugin_registry, "_is_owner", lambda: False)
    ran = []
    monkeypatch.setattr(plugin_verbs.subprocess, "run", lambda cmd, **kw: (
        ran.append(cmd) or subprocess.CompletedProcess(cmd, 3, "", "refused")))
    assert plugin_verbs.call_alias("graph_actions", "get-x") == (3, "", "refused")
    assert ran == [[sys.executable, str(ROOT / "preprocessor" / "plugin_verbs.py"), "p", "get-y"]]


def test_the_preprocessor_reads_teams_dms_through_the_plugin_verb(monkeypatch):
    import preprocess
    seen = []

    def alias(tool, verb, args=(), timeout=180):
        seen.append((tool, verb, list(args)))
        return answers.pop(0)
    answers = [None, (0, json.dumps({"status": "ok", "chats": [{"chat_id": "c1"}]}), ""),
               (2, "", "integrity check failed"), (0, "not json", "")]
    monkeypatch.setattr(plugin_verbs, "call_alias", alias)
    graph_reads = []
    monkeypatch.setattr(preprocess.graph, "get_graph_teams_chats",
                        lambda days_back=1: graph_reads.append(days_back))
    assert preprocess.get_teams_dms(days_back=1) == []          # no plugin, no Graph consent
    assert preprocess.get_teams_dms(days_back=2) == [{"chat_id": "c1"}]
    assert preprocess.get_teams_dms() == []                     # the verb failed
    assert preprocess.get_teams_dms() == []                     # unreadable output
    assert seen[1] == ("graph_actions", "get-teams-dms", ["--days", "2"])
    # Graph is read only when no plugin declares the verb: the plugin wins.
    assert graph_reads == [1]


def test_without_the_plugin_the_preprocessor_reads_teams_through_graph(monkeypatch):
    import preprocess
    monkeypatch.setattr(plugin_verbs, "call_alias", lambda *a, **k: None)
    monkeypatch.setattr(preprocess.graph, "get_graph_teams_chats",
                        lambda days_back=1: [{"chat_id": "g1"}])
    assert preprocess.get_teams_dms() == [{"chat_id": "g1"}]

    def boom(days_back=1):
        raise RuntimeError("503")
    monkeypatch.setattr(preprocess.graph, "get_graph_teams_chats", boom)
    assert preprocess.get_teams_dms() == []


# ── mail rules ────────────────────────────────────────────────────────

def test_plugin_confirmation_senders_and_exclusions():
    rules = {"closure_confirm_senders": ["noreply@notes.example.org"],
             "closure_exclude_senders": ["noreply@hotcrp.com"]}
    assert closure_scrubber.is_confirmation_sender("noreply@notes.example.org", rules)
    assert not closure_scrubber.is_confirmation_sender("noreply@hotcrp.com", rules)
    assert closure_scrubber.is_confirmation_sender("noreply@hotcrp.com", {})
    assert not closure_scrubber.is_confirmation_sender("someone@example.org", rules)


# ── network policy ────────────────────────────────────────────────────

def _facts(plugin_allows):
    return NP.facts_from(4242, "nameserver 127.0.0.53\n", [], [], deck_ports=(9400, 9409),
                         gateway_port=3070, plugin_allows=plugin_allows)


def test_plugin_endpoints_join_the_allowlist_after_their_own_checks():
    facts = _facts([{"name": "plugin:notes", "daddr": "127.0.0.1", "proto": "tcp", "ports": "9411"},
                    {"name": "plugin:Bad Name", "daddr": "127.0.0.1", "proto": "tcp", "ports": "1"},
                    {"name": "plugin:x", "daddr": "not-an-ip", "proto": "tcp", "ports": "1"},
                    {"name": "plugin:y", "daddr": "127.0.0.1", "proto": "icmp", "ports": "1"}])
    allow = NP.allowlist(facts)
    plug = [e for e in allow if e["name"].startswith("plugin:")]
    assert plug == [{"name": "plugin:notes", "daddr": "127.0.0.1", "proto": "tcp", "ports": "9411"}]
    text = NP.render(facts)
    assert 'comment "plugin:notes"' in text and "the notes plugin declares" in text
    stored = NP.parse(text)
    assert NP.drift(stored, _facts([])) == [
        'the allowlist names the plugin endpoint {"daddr": "127.0.0.1", "name": "plugin:notes", '
        '"ports": "9411", "proto": "tcp"}, which no enabled plugin declares']


def test_no_plugin_endpoint_leaves_the_facts_as_they_were():
    assert "plugin_allows" not in _facts([])
    assert "plugin_allows" not in NP._stored(_facts([]))


# ── schedule posts without Claude ─────────────────────────────────────

def test_llm_none_posts_the_collected_post():
    posts = []
    rc = plugin_llm.run("p", "none", "ea", [], {"collected": json.dumps({"post": ":memo: hi"})},
                        post=lambda k, t: posts.append((k, t)) or True)
    assert rc == 0 and posts == [("ea", ":memo: hi")]
    assert plugin_llm.run("p", "none", "ea", [], {"collected": "{}"}, post=lambda k, t: 1 / 0) == 0


def test_remote_first_hands_back_to_claude_and_remote_warns(monkeypatch):
    import remote_llm
    monkeypatch.setattr(remote_llm, "config", lambda: {"endpoint": "", "model": ""})
    posts = []
    post = lambda k, t: posts.append(t) or True  # noqa: E731
    assert plugin_llm.run("p", "remote-first", "ea", [":memo:"], {}, post=post) == 3
    assert posts == []
    assert plugin_llm.run("p", "remote", "ea", [":memo:"], {}, post=post) == 4
    assert posts and "could not write the post" in posts[0]


def test_a_remote_post_is_checked_before_it_goes_out(monkeypatch):
    import remote_llm
    monkeypatch.setattr(remote_llm, "config", lambda: {"endpoint": "https://x", "model": "m"})
    posts = []
    post = lambda k, t: posts.append(t) or True  # noqa: E731
    good = lambda msgs, **kw: {"content": "Thinking first.\n:memo: Two new notes."}  # noqa: E731
    bad = lambda msgs, **kw: {"content": "Here is a summary of the notes."}  # noqa: E731
    assert plugin_llm.run("p", "remote-first", "ea", [":memo:"],
                          {"skill": "s", "collected": "{}"}, post=post, chat=good) == 0
    assert posts == [":memo: Two new notes."]
    assert plugin_llm.run("p", "remote-first", "ea", [":memo:"], {}, post=post, chat=bad) == 3
    assert plugin_llm.run("p", "remote", "ea", [":memo:"], {}, post=post, chat=bad) == 4
    assert "failed validation" in posts[-1]


# ── the bot's claim protocol ──────────────────────────────────────────

def _claim_ns(tmp: Path) -> dict:
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("# === staged-action claim protocol")
    end = src.index("def _handle_context_export")
    ns = {"re": re, "json": json, "os": __import__("os"), "time": __import__("time"),
          "uuid": __import__("uuid"), "Path": Path, "EA_DIR": tmp,
          "_chat": types.SimpleNamespace(private_dir=lambda: tmp / "private"),
          "log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                       info=lambda *a, **k: None,
                                       error=lambda *a, **k: None)}
    exec(compile(src[start:end], "ea-chat-bot.py", "exec"), ns)
    return ns


def test_plugin_card_files_join_the_claim_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_registry, "card_pending_files", lambda lock=None: [
        ("plugin_card:notes:publish", "pending-notes.json"),
        ("plugin_card:evil:steal", "pending-event.json")])
    import preprocessor.plugin_registry as PR
    monkeypatch.setattr(PR, "card_pending_files", plugin_registry.card_pending_files)
    ns = _claim_ns(tmp_path)
    assert ("plugin_card:notes:publish", "pending-notes.json") in ns["STAGED_ACTION_FILES"]
    assert ns["PLUGIN_ACTION_FILES"] == (("plugin_card:notes:publish", "pending-notes.json"),)
    # A plugin cannot take over a core staged file.
    assert dict(ns["STAGED_ACTION_FILES"])["event_create"] == "pending-event.json"
    acts = ns["_staged_actions_from"]("plugin_card:notes:publish", [{"title": "A"}, {}, "x"])
    assert acts == [{"action": "plugin_card:notes:publish", "items": [{"title": "A"}]}]


def test_run_plugin_sh_parses():
    assert subprocess.run(["bash", "-n", str(ROOT / "run-plugin.sh")]).returncode == 0


# ── --plugin-dir for the chat lanes ──────────────────────────────────

def test_plugin_dirs_become_flags_for_the_chat_lanes(monkeypatch):
    import claude_cli
    monkeypatch.setattr(plugin_registry, "claude_plugin_dirs",
                        lambda lock=None: [Path("/p/one"), Path("/p/two")])
    assert claude_cli.plugin_dir_flags() == ["--plugin-dir", "/p/one", "--plugin-dir", "/p/two"]
    monkeypatch.setattr(plugin_registry, "claude_plugin_dirs", lambda lock=None: [])
    assert claude_cli.plugin_dir_flags() == []
    src = (ROOT / "ea-chat-bot.py").read_text()
    body = src[src.index("def _claude_args_for"):src.index("run_kw = {\"extra_env\": arm_env")]
    assert "_claude_cli.plugin_dir_flags()" in body and '"--bare" not in args' in body
