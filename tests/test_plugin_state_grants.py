#!/usr/bin/env python3
"""No lane may run a script it can write under a plugin's state dir.

The chat and cron lanes Edit-grant `<ea>/plugins/*/state/**`
(resident_write_paths.py), and the integrity hash skips the state dirs. A
permission rule's `*` matches across `/`, so a tool grant written as a glob
on a plugin directory would also run a script placed in its state dir.
These pin:
  * plugin_registry expands every tool template into one rule per file,
    from the lock's hashed files for a v2 plugin and from disk for a v1
    plugin, and names no file under a state dir, no symlink and no file
    whose name holds a glob character;
  * no rule the bot, run-agent.sh or run-plugin.sh grants matches a script
    under a state dir, even read as a glob that crosses `/`;
  * run-agent.sh and run-plugin.sh take their plugin grants from the
    registry and grant no glob on a plugin directory.
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import plugin_install as I  # noqa: E402
import plugin_registry as R  # noqa: E402

FIX = HERE / "fixtures" / "plugins"
NO_POLICY = {"trusted_sources": [], "blocked_sources": []}


def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"})
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(tmp_path, name) -> Path:
    repo = tmp_path / "repos" / name
    shutil.copytree(FIX / name, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    agent = tmp_path / "ea" / "plugins"
    agent.mkdir(parents=True)
    monkeypatch.setattr(R, "PLUGINS_DIR", agent)
    monkeypatch.setattr(R, "_is_owner", lambda: True)
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(I._rio, "_owner_check", lambda: False)
    c = I.Context(private=private, agent_dir=agent, ea_dir=tmp_path / "ea",
                  local_copy=True, out=lambda *_: None)
    R._LOCK_CACHE["key"] = None
    R._VERIFY_CACHE.clear()
    R._INTEGRITY_FAILED.clear()
    R._INTEGRITY_CHECKED.clear()
    R._CACHE["sig"] = None
    I.install(c, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    c.dest = agent / "example-notes"
    return c


def _rule_matches(rule: str, command: str) -> bool:
    """A permission rule read the widest way: `*` matches anything, `/`
    included."""
    m = re.match(r"^Bash\((.*)\)$", rule)
    return bool(m) and fnmatch.fnmatchcase(command, m.group(1))


def _plant(state: Path) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    evil = state / "evil.py"
    evil.write_text("import os; os.system('id')\n")
    return evil


def test_a_locked_plugin_gets_one_rule_per_locked_entry_point(ctx):
    grants = R.plugin_tool_grants(ctx.agent_dir, lock=ctx.lock_path)
    assert grants == [f"Bash(python3 {ctx.dest}/card_exec.py *)",
                      f"Bash(python3 {ctx.dest}/collect.py *)"]


def test_a_script_in_a_locked_plugins_state_dir_is_never_granted(ctx):
    evil = _plant(ctx.dest / "state")
    # The state dir is outside the integrity hash, so the plugin still passes.
    grants = R.plugin_tool_grants(ctx.agent_dir, lock=ctx.lock_path)
    assert grants
    for g in grants:
        assert "/state/" not in g
        assert not _rule_matches(g, f"python3 {evil} --go")
    run = R.run_tool_grants("example-notes", str(ctx.dest), lock=ctx.lock_path)
    assert run == grants
    assert not any(_rule_matches(g, f"python3 {evil}") for g in run)


def test_a_locked_plugin_that_fails_the_check_gets_no_run_grants(ctx):
    (ctx.dest / "collect.py").write_text("changed\n")
    assert R.run_tool_grants("example-notes", str(ctx.dest), lock=ctx.lock_path) == []


def test_a_v1_plugin_grants_skip_its_state_dir(tmp_path):
    pdir = tmp_path / "plugins" / "old"
    (pdir / "lib").mkdir(parents=True)
    (pdir / "plugin.json").write_text(json.dumps(
        {"name": "old", "enabled": True, "slack": {"skill": "skill.md", "keywords": ["x"]}}))
    (pdir / "skill.md").write_text("s")
    (pdir / "collect.py").write_text("")
    (pdir / "lib" / "helper.py").write_text("")
    evil = _plant(pdir / "state")
    R._CACHE["sig"] = None
    chat = R.plugin_tool_grants(tmp_path / "plugins")
    assert chat == [f"Bash(python3 {pdir}/collect.py *)"]
    run = R.run_tool_grants("old", str(pdir), lock={"plugins": {}})
    assert f"Bash(python3 {pdir}/collect.py *)" in run
    assert f"Bash(python3 {pdir}/lib/helper.py *)" in run
    for g in chat + run:
        assert not _rule_matches(g, f"python3 {evil} x")


def test_a_plugin_reached_through_a_symlink_runs_by_its_real_path_too(tmp_path):
    """The bot runs as the owner, whose ~/.ea links to the resident's; the
    grant read /home/<owner>/.ea/plugins/... while the skill told the
    resident to run /home/<resident>/.ea/plugins/..., and every plugin
    call from chat was denied. The rule names both paths; the state dir
    stays out under either."""
    real = tmp_path / "resident" / ".ea"
    pdir = real / "plugins" / "decks"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(json.dumps(
        {"name": "decks", "enabled": True, "slack": {"skill": "skill.md", "keywords": ["x"]}}))
    (pdir / "skill.md").write_text("s")
    (pdir / "deck_tools.py").write_text("")
    evil = _plant(pdir / "state")
    owner_ea = tmp_path / "owner" / ".ea"
    owner_ea.parent.mkdir()
    os.symlink(real, owner_ea)
    R._CACHE["sig"] = None
    chat = R.plugin_tool_grants(owner_ea / "plugins")
    assert chat == [f"Bash(python3 {owner_ea}/plugins/decks/deck_tools.py *)",
                    f"Bash(python3 {pdir}/deck_tools.py *)"]
    assert any(_rule_matches(g, f"python3 {pdir}/deck_tools.py build --slug x") for g in chat)
    for g in chat:
        assert not _rule_matches(g, f"python3 {evil} x")
        assert not _rule_matches(g, f"python3 {owner_ea}/plugins/decks/state/evil.py x")


def test_expansion_refuses_globs_symlinks_and_parent_paths(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "ok.py").write_text("")
    (root / "a*b.py").write_text("")
    os.symlink("/bin/sh", root / "link.py")
    files = R.plugin_files_on_disk(root)
    assert "link.py" not in files
    grants = R.expand_tool_grants(["Bash(python3 {plugin_dir}/*.py *)"], root, files)
    assert grants == [f"Bash(python3 {root}/ok.py *)"]
    assert R.expand_tool_grants(["Bash(python3 {plugin_dir}/../*.py *)"], root, files) == []
    assert R.expand_tool_grants(["Bash(python3 {plugin_dir}/**)"], root,
                                ["state/x.py", "ok.py"]) == [f"Bash(python3 {root}/ok.py)"]
    assert R.expand_tool_grants(["Bash(python3 {plugin_dir}/*.py *)", "Bash(rm -rf /)"],
                                root, ["sub/deep.py"]) == []


def test_the_cron_lanes_take_plugin_grants_from_the_registry():
    agent = (ROOT / "run-agent.sh").read_text()
    code = "\n".join(ln for ln in agent.splitlines() if not ln.lstrip().startswith("#"))
    assert "plugins/*" not in code
    assert 'plugin_registry.py" tool-grants' in code
    plugin = (ROOT / "run-plugin.sh").read_text()
    code = "\n".join(ln for ln in plugin.splitlines() if not ln.lstrip().startswith("#"))
    assert "Bash(python3 {plugin_dir}/*)" not in code
    assert "t.replace('{plugin_dir}'" not in code
    assert "run-grants --plugin" in code


def test_the_tool_grants_cli_prints_a_comma_list(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["EA_PRIVATE_DIR"] = str(tmp_path / "none")
    env["EA_DIR"] = str(tmp_path / "ea")
    for argv in (["tool-grants"], ["run-grants", "--plugin", "x", "--dir", str(tmp_path)]):
        r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "plugin_registry.py"),
                            *argv], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert "*)" not in r.stdout.replace(" *)", "")
