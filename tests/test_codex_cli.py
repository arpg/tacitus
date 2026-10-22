#!/usr/bin/env python3
"""preprocessor/codex_cli.py: the one builder for a non-interactive Codex
run, its event parser, its probe, and the eval provider's harness switch.
A fake `codex` on PATH stands in for the real one."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests" / "evals"))

import codex_cli as X  # noqa: E402

EVENTS = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t1"}),
    json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "error",
                "message": "Ignoring unknown `features` requirement from requirements layers: x"}}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                "text": "Running it."}}),
    json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                "command": "/bin/bash -c 'python3 x/ledger.py list'", "aggregated_output": "[]\n",
                "exit_code": 0, "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {"id": "i3", "type": "file_change",
                "changes": [{"path": "/tmp/x/out.txt", "kind": "add"}], "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {"id": "i4", "type": "agent_message",
                "text": ":sunny: *Morning Briefing*"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
]) + "\n"


def _fake_codex(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    f = bin_dir / "codex"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return f


def test_build_puts_the_prompt_on_stdin_and_names_the_sandbox(tmp_path):
    cmd = X.build(cwd=tmp_path, model="m1", sandbox="workspace-write", add_dirs=[tmp_path / "s"],
                  last_message_file=tmp_path / "last")
    assert cmd[1:3] == ["exec", "--skip-git-repo-check"]
    assert cmd[-1] == "-" and "--json" in cmd and "--ephemeral" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert cmd[cmd.index("-m") + 1] == "m1"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "s")
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "last")
    with pytest.raises(ValueError):
        X.build(cwd=tmp_path, sandbox="danger-full-access")


def test_parse_events_reads_messages_commands_files_and_drops_the_config_notice():
    p = X.parse_events(EVENTS)
    assert p["messages"] == ["Running it.", ":sunny: *Morning Briefing*"]
    assert p["tool_calls"][0]["tool"] == "Bash"
    assert "ledger.py list" in p["tool_calls"][0]["input"]["command"]
    assert p["files"] == ["/tmp/x/out.txt"]
    assert p["turns"] == 1 and p["usage"]["output_tokens"] == 2
    assert p["errors"] == []
    assert X.parse_events("not json\n")["messages"] == []


def test_run_returns_the_last_message_and_the_tool_calls(tmp_path, monkeypatch):
    script = ('for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
              'cat > /dev/null\n'
              f"printf '%s' '{EVENTS.replace(chr(39), chr(39)+chr(34)+chr(39)+chr(34)+chr(39))}'\n"
              'printf ":sunny: *Morning Briefing*" > "$out"\n')
    fake = _fake_codex(tmp_path, script)
    monkeypatch.setattr(X, "CODEX_BIN", str(fake))
    res = X.run("hello", cwd=tmp_path, sandbox="read-only", timeout=30)
    assert res["harness_error"] == "", res
    assert res["output"] == ":sunny: *Morning Briefing*"
    assert res["tool_calls"] and res["num_turns"] == 1 and res["exit_code"] == 0


def test_an_advisory_error_item_does_not_fail_a_run_that_answered(tmp_path, monkeypatch):
    events = "\n".join([
        json.dumps({"type": "item.completed", "item": {"id": "e", "type": "error",
                    "message": "Model metadata for `x` not found. Defaulting to fallback metadata"}}),
        json.dumps({"type": "item.completed", "item": {"id": "m", "type": "agent_message",
                    "text": "answer"}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    script = ('for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
              'cat > /dev/null\n'
              f"printf '%s' '{events}'\n"
              'printf "answer" > "$out"\n')
    fake = _fake_codex(tmp_path, script)
    monkeypatch.setattr(X, "CODEX_BIN", str(fake))
    res = X.run("hello", cwd=tmp_path)
    assert res["harness_error"] == "" and res["output"] == "answer"
    assert res["warnings"] and "Model metadata" in res["warnings"][0]
    # The same advisory with no answer is a harness error.
    fake = _fake_codex(tmp_path, 'cat > /dev/null\n' + f"printf '%s' '{events.splitlines()[0]}'\n")
    res = X.run("hello", cwd=tmp_path)
    assert res["harness_error"].startswith("codex error")


def test_run_reports_a_missing_binary_and_a_silent_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "CODEX_BIN", str(tmp_path / "nope"))
    res = X.run("hello", cwd=tmp_path)
    assert res["exit_code"] == 127 and "not found" in res["harness_error"]
    fake = _fake_codex(tmp_path, "cat > /dev/null\nexit 3\n")
    monkeypatch.setattr(X, "CODEX_BIN", str(fake))
    res = X.run("hello", cwd=tmp_path)
    assert res["harness_error"].startswith("codex exit 3") and res["output"] == ""


def test_probe_wants_the_word_ok(tmp_path, monkeypatch):
    ok_script = ('for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
                 'cat > /dev/null\nprintf "OK" > "$out"\n')
    fake = _fake_codex(tmp_path, ok_script)
    monkeypatch.setattr(X, "CODEX_BIN", str(fake))
    ok, _detail = X.probe(timeout=30)
    assert ok
    _fake_codex(tmp_path, ok_script.replace('printf "OK"', 'printf "nah"'))
    ok, detail = X.probe(timeout=30)
    assert not ok and "unexpected answer" in detail
    assert X.main(["probe"]) == 1
    (tmp_path / "bin" / "codex").write_text("not a program")
    ok, detail = X.probe(timeout=30)
    assert not ok and "could not start" in detail


def test_a_thread_is_started_persistent_and_resumed_with_its_sandbox_restated(tmp_path):
    first = X.build(cwd=tmp_path, sandbox="workspace-write", add_dirs=[tmp_path / "q"], ephemeral=False)
    assert "--ephemeral" not in first and first[1] == "exec" and "resume" not in first
    again = X.build(cwd=tmp_path, sandbox="workspace-write", add_dirs=[tmp_path / "q"], ephemeral=False,
                    resume="01a1-thread", model="m")
    assert again[1:4] == ["exec", "resume", "01a1-thread"] and "--json" in again
    assert "-C" not in again and "--add-dir" not in again and "--sandbox" not in again, "resume takes none of these"
    assert 'sandbox_mode="workspace-write"' in again
    roots = again[again.index('sandbox_mode="workspace-write"') + 2]
    assert roots.startswith("sandbox_workspace_write.writable_roots=[") and str(tmp_path / "q") in roots
    assert again[-1] == "-" and again[again.index("-m") + 1] == "m"
    assert X.parse_events(EVENTS)["thread_id"] == "t1"


def test_run_hands_back_the_thread_id_and_passes_resume_through(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        Path(cmd[cmd.index("-o") + 1]).write_text("ok")

        class R:
            returncode = 0
            stdout = EVENTS
            stderr = ""
        return R()
    monkeypatch.setattr(X.subprocess, "run", fake_run)
    monkeypatch.setattr(X, "binary", lambda: "/usr/bin/codex")
    res = X.run("hi", cwd=tmp_path, ephemeral=False, resume="t1")
    assert res["thread_id"] == "t1" and seen["cmd"][1:4] == ["exec", "resume", "t1"]
    assert seen["cwd"] == str(tmp_path), "the resumed thread's root is the process cwd"


def test_the_provider_switches_harness_and_refuses_an_unknown_one(tmp_path, monkeypatch):
    import provider
    monkeypatch.setenv("EA_EVAL_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(provider, "_ancestors_owner_only", lambda d: "")
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"_meta": {"id": "x"}, "briefing": {}, "ledger_entries": []}))
    r = provider.call_api("p", {"config": {"mode": "full", "harness": "gemini"}},
                          {"vars": {"fixture_path": str(fixture)}})
    assert "unknown harness" in r["metadata"]["harness_error"]
    seen = {}
    monkeypatch.setattr(provider, "_invoke_codex",
                        lambda *a, **k: seen.update(called=True) or
                        {"output": "x", "tool_calls": [], "exit_code": 0, "duration_s": 0.1,
                         "harness_error": ""})
    monkeypatch.setattr(provider, "_setup_test_env",
                        lambda fixture, tmpdir: {"tmpdir": str(tmpdir), "briefing": str(tmpdir / "b"),
                                                 "ledger_db": str(tmpdir / "l.db"),
                                                 "state_dir": str(tmpdir)})
    monkeypatch.setattr(provider, "_read_ledger_state", lambda p: [])
    r = provider.call_api("p", {"config": {"mode": "full", "harness": "codex"}},
                          {"vars": {"fixture_path": str(fixture)}})
    assert seen.get("called") and r["metadata"]["harness"] == "codex"


def test_the_codex_prompt_carries_the_core_and_the_shared_framing(tmp_path, monkeypatch):
    import provider
    captured = {}

    def fake_run(prompt, **kw):
        captured["prompt"] = prompt
        captured["kw"] = kw
        return {"output": "ok", "tool_calls": [], "exit_code": 0, "duration_s": 0.1,
                "harness_error": "", "num_turns": 1, "usage": {}, "files": [], "stderr": ""}

    monkeypatch.setattr(X, "run", fake_run)
    # A host where this user cannot read the resident's space (a dev box,
    # CI); elsewhere the provider refuses the codex harness.
    monkeypatch.setattr(provider, "resident_space_readable", lambda: False)
    env_paths = {"tmpdir": str(tmp_path), "briefing": str(tmp_path / "b.json"),
                 "ledger_db": str(tmp_path / "l.db"), "state_dir": str(tmp_path),
                 "config_dir": str(tmp_path / "config")}
    res = provider._invoke_codex(env_paths, "morning-briefing", "", 60)
    assert res["output"] == "ok"
    p = captured["prompt"]
    assert p.startswith("# Operating instructions (CLAUDE.md)")
    assert "Execute the following skill instructions" in p and "Begin now." in p
    assert "You are Codex" in p
    assert captured["kw"]["sandbox"] == "workspace-write"
    assert captured["kw"]["cwd"] == str(tmp_path)
    assert captured["kw"]["env"]["EA_DIR"] == str(tmp_path)


def test_run_streams_each_event_when_asked_and_reads_the_same_result(tmp_path, monkeypatch):
    """With on_event the run is read line by line, every JSONL event handed
    over as it arrives, and the result is what the whole-read run gives."""
    script = ('for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
              'cat > /dev/null\n'
              f"printf '%s' '{EVENTS.replace(chr(39), chr(39)+chr(34)+chr(39)+chr(34)+chr(39))}'\n"
              'printf ":sunny: *Morning Briefing*" > "$out"\n')
    fake = _fake_codex(tmp_path, script)
    monkeypatch.setattr(X, "CODEX_BIN", str(fake))
    seen = []
    res = X.run("hello", cwd=tmp_path, sandbox="read-only", timeout=30, on_event=seen.append)
    assert res["harness_error"] == "", res
    assert res["output"] == ":sunny: *Morning Briefing*" and res["tool_calls"] and res["exit_code"] == 0
    assert any(e.get("type") == "item.completed" for e in seen), "the events arrived as they were printed"
    assert [e.get("type") for e in seen] == [json.loads(l)["type"] for l in EVENTS.splitlines() if l.strip()]

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
