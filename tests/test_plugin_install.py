#!/usr/bin/env python3
"""ea-plugin's install, consent, lock, integrity, update and the registry
reads that act on the lock. Every test builds its own git repositories from
the made-up fixture plugins and its own private and plugins directories."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import plugin_install as I  # noqa: E402
import plugin_registry as R  # noqa: E402

FIX = HERE / "fixtures" / "plugins"


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
    for k in [k for k in env if k.startswith("GIT_DIR") or k == "GIT_INDEX_FILE"]:
        env.pop(k)
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(tmp_path, name) -> Path:
    repo = tmp_path / "repos" / name
    shutil.copytree(FIX / name, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _commit(repo, msg="next"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    agent = tmp_path / "ea" / "plugins"
    agent.mkdir(parents=True)
    lines = []
    monkeypatch.setattr(R, "PLUGINS_DIR", agent)
    monkeypatch.setattr(R, "_is_owner", lambda: True)
    # Writes into the scratch resident tree go straight through resident_io.
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(I._rio, "_owner_check", lambda: False)
    c = I.Context(private=private, agent_dir=agent, ea_dir=tmp_path / "ea",
                  local_copy=True, out=lines.append)
    c.lines = lines
    R._LOCK_CACHE["key"] = None
    R._VERIFY_CACHE.clear()
    return c


NO_POLICY = {"trusted_sources": [], "blocked_sources": []}


def test_parse_source():
    assert I.parse_source("git:https://h/x/p.git@v1") == ("https://h/x/p.git", "v1")
    assert I.parse_source("git:git@example.com:o/p.git") == ("git@example.com:o/p.git", "")
    assert I.parse_source("git:git@example.com:o/p.git#abc") == ("git@example.com:o/p.git", "abc")
    with pytest.raises(I.PluginError):
        I.parse_source("npm:x")


def _rev(repo, ref) -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_install_checks_out_the_ref_a_tag_a_branch_or_a_commit(tmp_path, ctx):
    """`git:<url>@<ref>` installs that ref. The clone carries no tags, so
    the ref is fetched from the source by name, a tag, a branch or a commit
    alike; a checkout of the bare tag name found nothing in the clone and
    read it as a path (2026-11-09)."""
    repo = _repo(tmp_path, "example-notes")
    _git(repo, "tag", "-a", "v1", "-m", "v1")
    tagged = _rev(repo, "v1^{commit}")
    with (repo / "skill.md").open("a") as f:
        f.write("\nA later line, on main only.\n")
    _commit(repo)
    head = _rev(repo, "HEAD")
    assert tagged != head

    entry = I.install(ctx, f"git:{repo}@v1", accept=True, policy=NO_POLICY)
    assert entry["source"] == {"url": str(repo), "ref": "v1", "commit": tagged}
    entry = I.update(ctx, "example-notes", ref="main", accept=True, policy=NO_POLICY)
    assert entry["source"]["commit"] == head
    entry = I.update(ctx, "example-notes", ref=tagged, accept=True, policy=NO_POLICY)
    assert entry["source"]["commit"] == tagged


def test_install_asks_for_consent_then_records_everything(tmp_path, ctx):
    repo = _repo(tmp_path, "example-notes")
    with pytest.raises(I.PluginError) as e:
        I.install(ctx, f"git:{repo}", policy=NO_POLICY)
    assert e.value.code == I.EXIT_CONSENT
    assert any("confirmation card publish-note" in line for line in ctx.lines)
    assert not (ctx.agent_dir / "example-notes").exists()

    entry = I.install(ctx, f"git:{repo}", accept=True, policy=NO_POLICY)
    dest = ctx.agent_dir / "example-notes"
    assert (dest / "card_exec.py").is_file() and (dest / "state").is_dir()
    assert not (dest / ".git").exists()
    lock = json.loads(ctx.lock_path.read_text())
    e = lock["plugins"]["example-notes"]
    assert e["source"]["url"] == str(repo) and len(e["source"]["commit"]) == 40
    assert e["accepted"]["how"] == "accepted" and e["accepted"]["digest"] == e["capability_digest"]
    assert "plugin.json" in e["files"] and ".claude-plugin/plugin.json" in e["files"]
    assert oct(ctx.lock_path.stat().st_mode & 0o777) == "0o600"
    assert entry["enabled"] is True
    with pytest.raises(I.PluginError):
        I.install(ctx, f"git:{repo}", accept=True, policy=NO_POLICY)


def test_a_trusted_source_skips_consent_unless_it_runs_owner_code(tmp_path, ctx):
    notes, reader = _repo(tmp_path, "example-notes"), _repo(tmp_path, "example-chat-reader")
    policy = {"trusted_sources": [str(tmp_path / "repos") + "/*"], "blocked_sources": []}
    assert I.install(ctx, f"git:{notes}", policy=policy)["accepted"]["how"] == "trusted-source"
    with pytest.raises(I.PluginError) as e:
        I.install(ctx, f"git:{reader}", policy=policy)
    assert e.value.code == I.EXIT_CONSENT


def test_a_blocked_source_never_installs(tmp_path, ctx):
    repo = _repo(tmp_path, "example-notes")
    with pytest.raises(I.PluginError) as e:
        I.install(ctx, f"git:{repo}", accept=True,
                  policy={"trusted_sources": [], "blocked_sources": [str(repo)]})
    assert e.value.code == I.EXIT_BLOCKED


def test_verify_finds_changed_added_and_missing_files(tmp_path, ctx):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    dest = ctx.agent_dir / "example-notes"
    assert I.verify(ctx) == {"example-notes": []}
    (dest / "state" / "data.json").write_text("{}")      # state is the plugin's own
    (dest / "run.log").write_text("x")
    assert I.verify(ctx) == {"example-notes": []}
    (dest / "collect.py").write_text("print('changed')\n")
    (dest / "extra.py").write_text("")
    (dest / "skill.md").unlink()
    problems = I.verify(ctx)["example-notes"]
    assert "collect.py: changed since install" in problems
    assert "extra.py: not in the installed release" in problems
    assert "skill.md: missing" in problems


def test_update_asks_again_when_capabilities_change_and_keeps_state(tmp_path, ctx):
    repo = _repo(tmp_path, "example-notes")
    I.install(ctx, f"git:{repo}", accept=True, policy=NO_POLICY)
    dest = ctx.agent_dir / "example-notes"
    (dest / "state" / "kept.json").write_text('{"k": 1}')

    (repo / "skill.md").write_text("# Skill: changed wording only\n")
    _commit(repo)
    I.update(ctx, "example-notes", policy=NO_POLICY)          # no new capability
    assert (dest / "skill.md").read_text().startswith("# Skill: changed")
    assert (dest / "state" / "kept.json").read_text() == '{"k": 1}'

    raw = json.loads((repo / "plugin.json").read_text())
    raw["capabilities"]["chat"]["keywords"].append("another trigger")
    (repo / "plugin.json").write_text(json.dumps(raw))
    _commit(repo)
    with pytest.raises(I.PluginError) as e:
        I.update(ctx, "example-notes", policy=NO_POLICY)
    assert e.value.code == I.EXIT_CONSENT
    I.update(ctx, "example-notes", accept=True, policy=NO_POLICY)
    assert I.verify(ctx) == {"example-notes": []}


def test_enable_disable_uninstall(tmp_path, ctx):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    I.set_enabled(ctx, "example-notes", False)
    assert R.managed_plugins(lock=ctx.lock_path) == []
    I.set_enabled(ctx, "example-notes", True)
    assert [p.name for p in R.managed_plugins(lock=ctx.lock_path)] == ["example-notes"]
    with pytest.raises(I.PluginError):
        I.uninstall(ctx, "example-notes")
    I.uninstall(ctx, "example-notes", yes=True)
    assert not (ctx.agent_dir / "example-notes").exists()
    assert json.loads(ctx.lock_path.read_text())["plugins"] == {}


def test_a_lock_manifest_edited_after_consent_is_ignored(tmp_path, ctx):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    lock = json.loads(ctx.lock_path.read_text())
    lock["plugins"]["example-notes"]["manifest"]["capabilities"]["tools"].append(
        "Bash(python3 {plugin_dir}/other/*.py *)")
    ctx.lock_path.write_text(json.dumps(lock))
    R._LOCK_CACHE["key"] = None
    assert R.managed_plugins(lock=ctx.lock_path) == []


def test_the_registry_reads_the_agent_capabilities(tmp_path, ctx):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    lock = ctx.lock_path
    dest = ctx.agent_dir / "example-notes"
    R._CACHE["sig"] = None
    names = [p.name for p in R.discover_interactive_plugins(ctx.agent_dir, lock=lock)]
    assert names == ["example-notes"]
    assert R.plugin_tool_grants(ctx.agent_dir, lock=lock) == [
        f"Bash(python3 {dest}/card_exec.py *)", f"Bash(python3 {dest}/collect.py *)"]
    assert R.card_pending_files(lock) == [("plugin_card:example-notes:publish-note",
                                           "pending-example-notes.json")]
    lp, spec = R.card_spec("plugin_card:example-notes:publish-note", lock)
    assert lp.dir == dest and spec["id"] == "publish-note"
    assert R.mail_rules(lock)["closure_exclude_senders"] == ["robot@notes.example.org"]
    assert R.netpolicy_allows(lock) == [{"name": "plugin:example-notes", "daddr": "127.0.0.1",
                                         "proto": "tcp", "ports": "9411"}]
    assert R.denied_tools(lock) == ["Bash(python3 *notes_tool.py publish *)"]
    assert R.claude_plugin_dirs(lock) == [dest.resolve()]
    raw = json.loads((dest / "plugin.json").read_text())
    assert R.schedule_config(raw, ["--sweep"], lock=lock)["llm"] == "none"
    assert R.schedule_config(raw, [], lock=lock) == {
        "enabled": True, "label": "daily", "llm": "remote-first", "model": "sonnet",
        "timeout_seconds": 600, "skill": "skill.md"}
    I.set_enabled(ctx, "example-notes", False)
    assert R.schedule_config(raw, [], lock=lock)["enabled"] is False


def test_v1_discovery_is_untouched_by_a_v2_manifest(tmp_path, ctx):
    old = ctx.agent_dir / "old"
    old.mkdir()
    (old / "plugin.json").write_text(json.dumps(
        {"name": "old", "enabled": True, "slack": {"skill": "skill.md", "keywords": ["x"]}}))
    (old / "skill.md").write_text("s")
    shutil.copytree(FIX / "example-notes", ctx.agent_dir / "example-notes")
    R._CACHE["sig"] = None
    # No lock passed and a plugins dir given: the v2 plugin does not act.
    assert [p.name for p in R.discover_interactive_plugins(ctx.agent_dir)] == ["old"]
    assert R.schedule_config({"name": "old", "enabled": False, "schedule": "0 1 * * *"})[
        "enabled"] is False


def test_owner_zone_verbs_auth_sources_and_cron(tmp_path, ctx):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-chat-reader')}", accept=True, policy=NO_POLICY)
    lock = ctx.lock_path
    dest = ctx.private / "plugins" / "example-chat-reader"
    assert oct(dest.stat().st_mode & 0o777) == "0o700" and (dest / ".git").is_dir()
    hint = json.loads((ctx.ea_dir / "state" / R.ALIAS_HINT_FILE).read_text())
    assert hint == {"graph_actions": {"get-example-dms": {"plugin": "example-chat-reader",
                                                          "verb": "get-example-dms"}}}
    lp = R.get_plugin("example-chat-reader", lock=lock, private=ctx.private)
    assert R.owner_verb("example-chat-reader", "get-example-dms", lock=lock)["class"] == "safe"
    assert R.owner_verb("example-chat-reader", "nope", lock=lock) is None
    assert R.verb_aliases("graph_actions", lock=lock) == {
        "get-example-dms": ("example-chat-reader", "get-example-dms")}
    R._require_owner_code(lp)
    cron = I.cron_lines(ctx, Path("/opt/ea/scripts/ea-plugin.py"), ctx.private / "logs")
    assert len(cron) == 1 and "run-owner example-chat-reader refresh" in cron[0]
    (dest / "reader.py").write_text("print('tampered')\n")
    R._VERIFY_CACHE.clear()
    with pytest.raises(PermissionError):
        R._require_owner_code(lp)


def test_owner_auth_tokens_and_verb_command_after_the_checks(tmp_path, ctx, monkeypatch):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-chat-reader')}", accept=True, policy=NO_POLICY)
    monkeypatch.setattr(R._owner_trust, "private_dir", lambda: ctx.private)
    lock = ctx.lock_path
    assert list(R.owner_auth_tokens("graph", lock=lock)) == ["example-token-value"]
    argv, cwd, timeout = R.owner_verb_command("example-chat-reader", "get-example-dms", lock=lock)
    assert argv == [sys.executable, str(ctx.private / "plugins" / "example-chat-reader" / "reader.py")]
    assert timeout == 60
    out = subprocess.run(argv + ["get-example-dms", "--days", "2"], cwd=cwd,
                         capture_output=True, text=True).stdout
    assert json.loads(out) == {"verb": "get-example-dms", "args": ["--days", "2"], "messages": []}


def test_adopt_records_a_v1_plugin_in_place(tmp_path, ctx):
    old = ctx.agent_dir / "old"
    (old / ".git" / "refs" / "heads").mkdir(parents=True)
    (old / "plugin.json").write_text(json.dumps({"name": "old", "enabled": True, "schedule": "0 1 * * *"}))
    (old / ".git" / "config").write_text('[remote "origin"]\n\turl = git@example.org:o/old.git\n')
    (old / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (old / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    entry = I.adopt(ctx, "old")
    assert entry["source"]["url"] == "git@example.org:o/old.git" and entry["source"]["commit"] == "a" * 40
    assert entry["manifest"]["manifest_version"] == 1
    assert R.managed_plugins(lock=ctx.lock_path) == []      # v1 keeps deciding for itself
    assert I.verify(ctx, ["old"]) == {"old": []}
    rows = I.listing(ctx)
    assert rows[0]["name"] == "old" and rows[0]["managed"]


def _runs(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")
    monkeypatch.setattr(I, "_run", run)
    return calls


def test_plugin_tests_refuse_a_plugin_that_fails_the_lock(tmp_path, ctx, monkeypatch):
    """`ea-plugin.py test` runs a plugin's code. It runs only what the
    owner's lock recorded: a changed or added file is refused, unrun."""
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    dest = ctx.agent_dir / "example-notes"
    (dest / "tests").mkdir()
    (dest / "tests" / "test_x.py").write_text("import os\n")
    calls = _runs(monkeypatch)
    rc, tail = I.run_tests(ctx, HERE.parent, ["example-notes"])["example-notes"]
    assert rc == I.EXIT_BLOCKED and "does not match the lock" in tail and "test_x.py" in tail
    assert calls == []


def test_an_agent_zone_suite_runs_as_the_resident_for_the_owner(tmp_path, ctx, monkeypatch):
    I.install(ctx, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    dest = ctx.agent_dir / "example-notes"
    monkeypatch.setattr(R, "verify", lambda lp, use_cache=True: [])
    (dest / "tests").mkdir()
    calls = _runs(monkeypatch)
    monkeypatch.setattr(I, "_resident_exists", lambda: True)
    monkeypatch.setattr(I._owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(I.pwd, "getpwnam",
                        lambda n: type("P", (), {"pw_dir": str(tmp_path / "rh")})())
    assert I.run_tests(ctx, HERE.parent, ["example-notes"])["example-notes"][0] == 0
    argv, kw = calls[-1]
    assert argv[:6] == ["sudo", "-n", "-u", "ea-bot", "env", "-i"] and kw["cwd"] == "/"
    assert str(dest / "tests") in argv and "no:cacheprovider" in argv
    # not an owner process (a machine with no resident split): the caller runs it
    monkeypatch.setattr(I._owner_trust, "is_owner_process", lambda: False)
    I.run_tests(ctx, HERE.parent, ["example-notes"])
    argv, kw = calls[-1]
    assert argv[0] == sys.executable and kw["cwd"] == str(dest)
