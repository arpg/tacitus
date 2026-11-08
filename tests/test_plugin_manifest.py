#!/usr/bin/env python3
"""plugin_manifest: v1 and v2 manifests, the checks, and the consent summary."""
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import plugin_manifest as PM  # noqa: E402

FIX = HERE / "fixtures" / "plugins"


def _raw(name="example-notes") -> dict:
    return json.loads((FIX / name / "plugin.json").read_text())


def test_the_example_plugins_pass_the_checks():
    for name in ("example-notes", "example-chat-reader"):
        norm, errors = PM.load(FIX / name)
        assert errors == [], (name, errors)
        assert norm["manifest_version"] == 2 and norm["name"] == name


def test_v2_normalizes_every_capability():
    norm, _ = PM.load(FIX / "example-notes")
    caps = norm["capabilities"]
    assert [s["label"] for s in caps["schedules"]] == ["daily", "sweep"]
    assert caps["schedules"][1]["args"] == ["--sweep"] and caps["schedules"][1]["llm"] == "none"
    assert caps["chat"]["keywords"] == ["example note"]
    card = caps["cards"][0]
    assert card["pending_file"] == "pending-example-notes.json"
    assert card["approve"]["user"] == "resident" and card["cancel"]["timeout_seconds"] == 30
    # The fixture names the keep domains under the old key on purpose: a
    # manifest written before the rename parses into the new one.
    assert caps["mail_rules"]["keep_from_frontier_domains"] == ["notes.example.org"]
    assert "keep_from_anthropic_domains" not in caps["mail_rules"]
    assert caps["network"]["local"][0]["ports"] == "9411"
    assert caps["claude_plugin"] == "."


def test_v1_reads_as_before():
    raw = {"name": "old", "enabled": True, "schedule": "0 12 * * *", "model": "sonnet",
           "extra_schedules": [{"label": "nightly", "cron": "0 2 * * *", "args": "--enrich-pending"}],
           "slack": {"skill": "s.md", "keywords": ["Thing"]}}
    norm, errors = PM.normalize(raw, "old")
    assert errors == []
    assert norm["manifest_version"] == 1 and norm["enabled"] is True and norm["zone"] == "agent"
    assert [s["cron"] for s in norm["capabilities"]["schedules"]] == ["0 12 * * *", "0 2 * * *"]
    assert norm["capabilities"]["schedules"][1]["args"] == ["--enrich-pending"]
    assert norm["capabilities"]["chat"]["keywords"] == ["thing"]
    assert norm["capabilities"]["tools"] == ["Bash(python3 {plugin_dir}/*.py *)"]


def _errors_for(mutate) -> list:
    raw = _raw()
    mutate(raw)
    return PM.normalize(raw)[1]


def test_a_tool_grant_must_stay_inside_the_plugin():
    errs = _errors_for(lambda r: r["capabilities"].__setitem__("tools", ["Bash(*)"]))
    assert any("plugin_dir" in e for e in errs), errs


def test_owner_hooks_need_the_owner_zone():
    def m(r):
        r["capabilities"]["owner_hooks"] = {"verbs": [
            {"name": "x", "class": "safe", "command": ["python3", "{plugin_dir}/x.py"]}]}
    assert any("owner-zone" in e for e in _errors_for(m))


def test_an_agent_card_cannot_run_as_the_owner():
    assert any("owner-zone" in e for e in _errors_for(
        lambda r: r["capabilities"]["cards"][0]["approve"].__setitem__("user", "owner")))


def test_bad_values_are_refused():
    cases = [
        lambda r: r["capabilities"]["cards"][0].__setitem__("pending_file", "../x.json"),
        lambda r: r["capabilities"]["mail_rules"].__setitem__("closure_confirm_senders", ["a.b"]),
        lambda r: r["capabilities"]["network"]["local"][0].__setitem__("daddr", "localhost"),
        lambda r: r["capabilities"]["cards"][0]["approve"].__setitem__("command", ["/bin/sh", "-c", "x"]),
        lambda r: r["capabilities"]["cards"][0]["approve"].__setitem__(
            "command", ["python3", "{plugin_dir}/x.py", "{home}"]),
        lambda r: r["capabilities"]["schedules"][0].__setitem__("cron", "every day"),
        lambda r: r.__setitem__("name", "Bad Name"),
        lambda r: r["capabilities"].__setitem__("secrets", ["lower"]),
    ]
    for i, mutate in enumerate(cases):
        assert _errors_for(mutate), f"case {i} passed the checks"


def test_a_missing_file_the_manifest_names_is_an_error(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    raw = _raw()
    raw["capabilities"]["claude_plugin"] = ""
    (d / "plugin.json").write_text(json.dumps(raw))
    _norm, errors = PM.load(d)
    assert any("skill.md" in e for e in errors) and any("chat_skill.md" in e for e in errors)


def test_the_digest_follows_every_capability():
    norm, _ = PM.load(FIX / "example-notes")
    base = PM.capability_digest(PM.capability_summary(norm))
    assert base == PM.capability_digest(PM.capability_summary(copy.deepcopy(norm)))
    changed = copy.deepcopy(norm)
    changed["capabilities"]["chat"]["keywords"].append("another")
    assert PM.capability_digest(PM.capability_summary(changed)) != base
    changed = copy.deepcopy(norm)
    changed["capabilities"]["cards"][0]["approve"]["command"].append("--force")
    assert PM.capability_digest(PM.capability_summary(changed)) != base
    changed = copy.deepcopy(norm)
    changed["description"] = "other words"
    assert PM.capability_digest(PM.capability_summary(changed)) == base


def test_consent_text_and_who_needs_it():
    agent, _ = PM.load(FIX / "example-notes")
    owner, _ = PM.load(FIX / "example-chat-reader")
    a, o = PM.capability_summary(agent), PM.capability_summary(owner)
    assert not PM.needs_explicit_consent(a)
    assert PM.needs_explicit_consent(o)
    text = "\n".join(PM.describe(a))
    assert "confirmation card publish-note" in text and "9411" in text
    text = "\n".join(PM.describe(o))
    assert "owner verb get-example-dms [SAFE]" in text and "graph_actions get-example-dms" in text
    assert "[GATED]" in text and "owner cron refresh" in text
