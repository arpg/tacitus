"""The owner-run model suites take nothing from the resident's space.

The nightly full-mode evals (tests/evals/provider.py, run by
run_full_if_changed.sh) start a tool-using `claude -p` as the owner, and
the weekly release tests (tests/release/run_weekly.sh) run the same
provider for their briefing lane. These tests pin that run: its command
line (no settings, no MCP, only the listed tools, the shared deny list, a
permission mode that refuses the rest), its working directory (a scratch
dir outside any checkout), its inputs (repository files, never
~/.ea/config) and its gates (the owner's private dir, never ea-config.json
or ~/.ea/state). No test here starts a model.

    python3 -m pytest tests/test_eval_lane_isolation.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "evals"))
sys.path.insert(0, str(REPO / "tests" / "release"))
sys.path.insert(0, str(REPO / "preprocessor"))

import eval_gates  # noqa: E402
import provider  # noqa: E402

FIXTURE = REPO / "tests" / "evals" / "fixtures" / "empty-calm-day.json"

# The guard's walks up a path see the test's own directories, not the
# host's temp dir above them (conftest.walks_stop_at_tmp_path).
pytestmark = pytest.mark.usefixtures("walks_stop_at_tmp_path")


def _arg(cmd: list[str], flag: str) -> str:
    assert flag in cmd, f"{flag} missing from {cmd}"
    return cmd[cmd.index(flag) + 1]


def _deny_rows() -> list[str]:
    text = (REPO / "scripts" / "disallowed-tools.txt").read_text()
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


@pytest.fixture
def no_owner_flags(monkeypatch, tmp_path):
    """No owner claude.json reaches the command line under test, and the
    run's scratch dir is a test directory (the system temp dir sits above
    it, so the owner-only ancestor check is stubbed)."""
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "no-private"))
    monkeypatch.setenv("EA_CLAUDE_PROBE", "off")
    monkeypatch.setenv("EA_EVAL_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(provider, "_ancestors_owner_only", lambda d: "")


# ── the command line ───────────────────────────────────────────────────

@pytest.mark.parametrize("stages", [False, True])
def test_full_mode_command_line_is_pinned(tmp_path, no_owner_flags, stages):
    env_paths = provider._setup_test_env({"briefing": {}}, tmp_path / "run")
    tools, allowed = provider.eval_tools(env_paths, stages)
    cmd = provider.claude_command("PROMPT", "sonnet", tools, allowed)

    assert cmd[0] == "claude" and "-p" in cmd
    assert "--restricted" in cmd
    assert _arg(cmd, "--setting-sources") == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(_arg(cmd, "--mcp-config")) == {"mcpServers": {}}
    assert _arg(cmd, "--permission-mode") == "dontAsk"
    assert _arg(cmd, "--permission-prompts") == "none"
    assert "--no-session-persistence" in cmd

    want_tools = {"Bash", "Read"} | ({"Write", "Edit"} if stages else set())
    assert set(_arg(cmd, "--tools").split(",")) == want_tools

    staged = f"/{env_paths['state_dir']}/pending-event.json"
    want_allowed = {"Read"}
    for verb in ("list", "get", "search", "stats", "types", "proposals", "add", "update", "propose"):
        for exe in (f"{provider.PREPROCESS_DIR}/ledger.py", "preprocessor/ledger.py"):
            want_allowed |= {f"Bash(python3 {exe} {verb})", f"Bash(python3 {exe} {verb} *)"}
    if stages:
        want_allowed |= {f"Write({staged})", f"Edit({staged})"}
    assert set(_arg(cmd, "--allowedTools").split(",")) == want_allowed

    denied = _arg(cmd, "--disallowedTools").split(",")
    missing = [r for r in _deny_rows() if r not in denied]
    assert not missing, f"shared deny rows missing: {missing[:3]}"
    assert "Read(//home/ea-bot/**)" in denied and "Read(~/.ea/**)" in denied

    for bad in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                "bypassPermissions", "--add-dir", "--settings", "--plugin-dir"):
        assert bad not in cmd


def test_full_run_starts_in_scratch_with_repository_inputs(tmp_path, monkeypatch, no_owner_flags):
    """call_api in full mode: the CLI starts in a scratch dir outside any
    checkout, which is the run's EA_DIR, with the repository's config and
    CLAUDE.md, while a poisoned ~/.ea/config sits in the owner's HOME."""
    home = tmp_path / "home"
    (home / ".ea" / "config").mkdir(parents=True)
    poison = "POISON-FROM-RESIDENT-SPACE"
    for name in provider.CONFIG_FILES:
        (home / ".ea" / "config" / name).write_text(json.dumps({"name": poison}))
    monkeypatch.setenv("HOME", str(home))

    seen = {}

    def fake_run(cmd, **kw):
        cwd = Path(kw["cwd"])
        seen["cmd"], seen["cwd"], seen["env"] = cmd, cwd, kw["env"]
        seen["config"] = {n: (cwd / "config" / n).read_text()
                          for n in provider.CONFIG_FILES if (cwd / "config" / n).exists()}
        seen["claude_md"] = (cwd / "CLAUDE.md").read_text()
        seen["preprocessor"] = os.path.realpath(cwd / "preprocessor")
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": ":sunny: *Morning Briefing*"})
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(provider.subprocess, "run", fake_run)
    r = provider.call_api("x", {"config": {"mode": "full", "model": "sonnet"}},
                          {"vars": {"fixture_path": str(FIXTURE)}})
    assert r["metadata"]["harness_error"] == ""

    cwd = seen["cwd"]
    assert cwd != REPO and REPO not in cwd.parents
    assert provider._work_tree_of(cwd) is None, "the run's cwd is in a git work tree"
    assert cwd.parent == Path(os.path.realpath(os.environ["EA_EVAL_SCRATCH"]))
    assert seen["env"]["EA_DIR"] == str(cwd)
    assert not cwd.exists(), "the scratch dir is removed after the run"

    assert seen["config"], "the run got no config"
    for name, text in seen["config"].items():
        assert poison not in text
        assert text == provider.config_source(name).read_text()
    assert seen["config"]["user-profile.json"] == (
        REPO / "tests" / "evals" / "config" / "user-profile.json").read_text()
    assert seen["claude_md"] == (REPO / "CLAUDE.md").read_text()
    assert seen["preprocessor"] == str(provider.PREPROCESS_DIR)

    prompt = _arg(seen["cmd"], "-p")
    assert str(home) not in prompt
    assert f"{cwd}/CLAUDE.md" in prompt and f"{cwd}/config" in prompt


def test_config_sources_are_repository_files():
    for name in provider.CONFIG_FILES:
        src = provider.config_source(name)
        assert src is not None, name
        assert REPO in src.resolve().parents, src
    profile = json.loads(provider.config_source("user-profile.json").read_text())
    assert profile["email"].endswith("@example.edu")


def _scratch_here(monkeypatch, d):
    monkeypatch.setenv("EA_EVAL_SCRATCH", str(d))
    monkeypatch.setattr(provider, "_ancestors_owner_only", lambda d: "")


def test_scratch_parent_refuses_a_checkout_or_work_tree(tmp_path, monkeypatch):
    _scratch_here(monkeypatch, REPO / "tests" / "no-such-scratch")
    with pytest.raises(RuntimeError):
        provider.scratch_parent()
    (REPO / "tests" / "no-such-scratch").rmdir()
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    _scratch_here(monkeypatch, tree / "scratch")
    with pytest.raises(RuntimeError, match="work tree"):
        provider.scratch_parent()
    plain = tmp_path / "plain"
    _scratch_here(monkeypatch, plain)
    assert provider.scratch_parent() == Path(os.path.realpath(plain))
    assert plain.stat().st_mode & 0o777 == 0o700


def test_a_marker_above_the_test_dir_is_the_hosts_and_one_inside_it_is_refused(tmp_path, monkeypatch):
    """These tests hold whatever sits in the host's temp dir above them (an
    empty `.git` folder, a CLAUDE.md), and the guard still refuses the same
    markers inside the test's own directory."""
    above = tmp_path.parent
    (above / ".git").mkdir()
    (above / "CLAUDE.md").write_text("the host's")
    try:
        plain = tmp_path / "plain"
        _scratch_here(monkeypatch, plain)
        assert provider.scratch_parent() == Path(os.path.realpath(plain))
        (tmp_path / "CLAUDE.md").write_text("planted")
        with pytest.raises(RuntimeError, match="memory file"):
            provider.scratch_parent()
        (tmp_path / ".git").mkdir()
        with pytest.raises(RuntimeError, match="work tree"):
            provider.scratch_parent()
    finally:
        (above / ".git").rmdir()
        (above / "CLAUDE.md").unlink()


def test_scratch_parent_refuses_the_resident_home(tmp_path, monkeypatch):
    home = tmp_path / "resident"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(provider, "resident_home", lambda: Path(os.path.realpath(home)))
    _scratch_here(monkeypatch, home / "tmp")
    with pytest.raises(RuntimeError, match="resident"):
        provider.scratch_parent()


def test_the_scratch_dir_is_not_the_system_temp_dir(monkeypatch):
    """By default the scratch dir is owner-only in the owner's home: the
    resident can write the system temp dir, and a CLAUDE.md there would load
    into every run below it."""
    import tempfile
    monkeypatch.delenv("EA_EVAL_SCRATCH", raising=False)
    root = provider.scratch_root()
    tmp = Path(os.path.realpath(tempfile.gettempdir()))
    assert root.name == provider.SCRATCH_NAME
    assert tmp != root and tmp not in root.parents
    src = (REPO / "tests" / "evals" / "provider.py").read_text()
    assert "gettempdir" not in src


def test_scratch_parent_refuses_a_dir_others_can_write_above(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    monkeypatch.setenv("EA_EVAL_SCRATCH", str(shared / "scratch"))
    with pytest.raises(RuntimeError, match="not owner-only"):
        provider.scratch_parent()
    assert "writable by another user" in provider._ancestors_owner_only(shared / "scratch")


def test_scratch_parent_refuses_a_link_or_a_memory_file_on_the_path(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "link")
    _scratch_here(monkeypatch, tmp_path / "link")
    with pytest.raises(RuntimeError, match="plain directory"):
        provider.scratch_parent()
    planted = tmp_path / "planted"
    (planted / "scratch").mkdir(parents=True)
    (planted / "CLAUDE.md").write_text("instructions from someone else")
    _scratch_here(monkeypatch, planted / "scratch")
    with pytest.raises(RuntimeError, match="memory file"):
        provider.scratch_parent()


def test_a_run_with_a_memory_file_above_it_never_starts(tmp_path, monkeypatch, no_owner_flags):
    """call_api checks the run's path right before the model starts: only
    the repository CLAUDE.md in the run's own directory may be on it."""
    import claude_cli
    started = []
    monkeypatch.setattr(provider.subprocess, "run", lambda *a, **k: started.append(a))
    real = claude_cli.memory_on_path

    def planted(d):
        found = real(d)
        return found + [Path(d) / "CLAUDE.local.md"]
    monkeypatch.setattr(claude_cli, "memory_on_path", planted)
    r = provider.call_api("x", {"config": {"mode": "full", "model": "sonnet"}},
                          {"vars": {"fixture_path": str(FIXTURE)}})
    assert "memory file" in r["metadata"]["harness_error"]
    assert started == []


def test_run_memory_error_accepts_only_the_copied_claude_md(tmp_path):
    run = tmp_path / "parent" / "run"
    run.mkdir(parents=True)
    (run / "CLAUDE.md").write_text("repo core")
    assert provider.run_memory_error(run) == ""
    (run / "CLAUDE.local.md").write_text("x")
    assert "unexpected memory file" in provider.run_memory_error(run)
    (run / "CLAUDE.local.md").unlink()
    (tmp_path / "parent" / "CLAUDE.md").write_text("x")
    assert "above the run" in provider.run_memory_error(run)


def test_the_ledger_grant_names_no_repair_close_or_database_verb(tmp_path, no_owner_flags):
    env_paths = provider._setup_test_env({"briefing": {}}, tmp_path / "run")
    _tools, allowed = provider.eval_tools(env_paths, True)
    rules = allowed.split(",")
    for bad in ("repair-ids", "normalize", "import-json", "close", "confirm", "reject", "--db", "dump"):
        assert not [r for r in rules if f"ledger.py {bad}" in r or bad == "--db" and bad in r], bad
    assert not [r for r in rules if r.endswith("ledger.py *)")], "a ledger rule open to any verb"


def test_codex_harness_is_refused_where_the_resident_space_is_readable(tmp_path, monkeypatch):
    import codex_cli
    monkeypatch.setattr(provider, "resident_space_readable", lambda: True)
    monkeypatch.setattr(codex_cli, "run", lambda *a, **k: pytest.fail("codex started"))
    env_paths = provider._setup_test_env({"briefing": {}}, tmp_path / "run")
    r = provider._invoke_codex(env_paths, "morning-briefing", "", 10)
    assert "refused" in r["harness_error"]


def test_release_judge_stays_tool_less(monkeypatch):
    import run_release as rr
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok"}), "")

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    assert rr.claude_text("PROMPT", "m")["text"] == "ok"
    cmd = seen["cmd"]
    assert _arg(cmd, "--tools") == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(_arg(cmd, "--mcp-config")) == {"mcpServers": {}}
    assert "--allowedTools" not in cmd and "--dangerously-skip-permissions" not in cmd


# ── the gates ──────────────────────────────────────────────────────────

@pytest.fixture
def private(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    return d


def test_gates_live_in_the_private_dir(private):
    assert eval_gates.settings_path() == private / "evals.json"
    assert eval_gates.last_commit_path() == private / "evals" / "last-full-commit"


def test_gates_default_on_and_follow_the_owner_file(private):
    assert eval_gates.enabled("full") and eval_gates.enabled("release")
    p = private / "evals.json"
    p.write_text(json.dumps({"eval_full_enabled": False}))
    os.chmod(p, 0o600)
    assert eval_gates.enabled("full") is False
    assert eval_gates.enabled("release") is True


def test_a_gate_file_that_fails_the_owner_checks_reads_as_off(private, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"eval_full_enabled": True}))
    os.symlink(elsewhere, private / "evals.json")
    assert eval_gates.enabled("full") is False
    os.unlink(private / "evals.json")
    p = private / "evals.json"
    p.write_text("{}")
    os.chmod(p, 0o666)
    assert eval_gates.enabled("full") is False
    os.chmod(p, 0o600)
    p.write_text("not json")
    assert eval_gates.enabled("full") is False


def test_the_postcommit_switch_is_the_owners(private, monkeypatch):
    """The post-commit test wall reads its switch from the environment or
    the owner's evals.json, and an untrusted file runs the tests."""
    monkeypatch.delenv("EA_POSTCOMMIT_TESTS", raising=False)
    assert eval_gates.enabled("postcommit") is True
    p = private / "evals.json"
    p.write_text(json.dumps({"eval_postcommit_enabled": False}))
    os.chmod(p, 0o600)
    assert eval_gates.enabled("postcommit") is False
    monkeypatch.setenv("EA_POSTCOMMIT_TESTS", "1")
    assert eval_gates.enabled("postcommit") is True
    monkeypatch.setenv("EA_POSTCOMMIT_TESTS", "0")
    assert eval_gates.enabled("postcommit") is False
    monkeypatch.delenv("EA_POSTCOMMIT_TESTS")
    os.chmod(p, 0o666)
    assert eval_gates.enabled("postcommit") is True     # not the owner's alone: tests run
    os.chmod(p, 0o600)
    p.write_text("not json")
    assert eval_gates.enabled("postcommit") is True


def test_the_post_commit_hook_reads_no_switch_from_resident_space():
    hook = (REPO / ".githooks" / "post-commit").read_text()
    code = "\n".join(l for l in hook.splitlines() if not l.lstrip().startswith("#"))
    assert "ea-config.json" not in code
    assert "eval_gates.py\" enabled postcommit" in code


def test_the_post_commit_switch_ignores_ea_config(private, tmp_path, monkeypatch):
    """A resident-written ea-config.json saying false leaves the wall on."""
    home = tmp_path / "home"
    (home / ".ea" / "config").mkdir(parents=True)
    (home / ".ea" / "config" / "ea-config.json").write_text(
        json.dumps({"eval_postcommit_enabled": False}))
    env = _script_env(private, tmp_path)
    env.pop("EA_POSTCOMMIT_TESTS", None)
    r = subprocess.run([sys.executable, "-I", str(REPO / "tests" / "evals" / "eval_gates.py"),
                        "enabled", "postcommit"], capture_output=True, text=True, env=env,
                       timeout=30)
    assert r.stdout.strip() == "True", r.stderr


def test_last_commit_round_trip(private):
    assert eval_gates.last_commit() == ""
    eval_gates.set_last_commit("0123abcd")
    assert eval_gates.last_commit() == "0123abcd"
    assert (private / "evals").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        eval_gates.set_last_commit("--output=/tmp/x")
    eval_gates.clear_last_commit()
    assert eval_gates.last_commit() == ""
    eval_gates.clear_last_commit()


def test_last_commit_ignores_anything_but_a_commit_id(private):
    (private / "evals").mkdir(mode=0o700)
    p = private / "evals" / "last-full-commit"
    p.write_text("--output=/tmp/x\n")
    os.chmod(p, 0o600)
    assert eval_gates.last_commit() == ""


def test_the_cron_scripts_read_no_gate_from_resident_space():
    nightly = (REPO / "tests" / "evals" / "run_full_if_changed.sh").read_text()
    weekly = (REPO / "tests" / "release" / "run_weekly.sh").read_text()
    for text in (nightly, weekly):
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert "ea-config.json" not in code
        assert ".ea}/" not in code and "~/.ea/" not in code and "/.ea/state" not in code
        assert "EA_DIR" not in code
        assert "owner-io.sh" not in code
        assert "eval_gates.py" in code


def _script_env(private: Path, tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(EA_PRIVATE_DIR=str(private), HOME=str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def test_nightly_script_honours_the_owner_off_switch(private, tmp_path):
    p = private / "evals.json"
    p.write_text(json.dumps({"eval_full_enabled": False}))
    os.chmod(p, 0o600)
    r = subprocess.run(["bash", str(REPO / "tests" / "evals" / "run_full_if_changed.sh")],
                       capture_output=True, text=True, timeout=60,
                       env=_script_env(private, tmp_path))
    assert r.returncode == 0, r.stderr
    assert "disabled" in r.stdout


def test_nightly_script_skips_the_commit_the_owner_state_names(private, tmp_path):
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                          text=True, env=_script_env(private, tmp_path)).stdout.strip()
    if not head:
        pytest.skip("not a git checkout")
    eval_gates.set_last_commit(head)
    r = subprocess.run(["bash", str(REPO / "tests" / "evals" / "run_full_if_changed.sh")],
                       capture_output=True, text=True, timeout=60,
                       env=_script_env(private, tmp_path))
    assert r.returncode == 0, r.stderr
    assert "No new commits" in r.stdout
