"""scripts/install/setup.sh installs a crontab that keeps the owner's jobs off ea-bot space.

The cron jobs are owner processes (docs/SECURITY.md, "Owner trust rules").
The crontab scripts/install/setup.sh writes sends their output to the owner-private log
directory (~/.ea-private/logs, mode 700), runs them from the deployed tree
(/opt/ea-system, EA_LIVE_DIR here) when it holds the job scripts and from
the checkout otherwise, and takes a plugin manifest's fields (ea-bot space)
into a cron line only when each has its plain shape.

Each test runs the real scripts/install/setup.sh from a scratch copy of the checkout with
stub pip, crontab and systemctl on PATH and a scratch HOME, so no host
state is read or written. The installed crontab is judged by the daily
audit's own cron parser (scripts/boundary-audit.py) and by boundary_lint.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import boundary_lint as bl  # noqa: E402

_spec = importlib.util.spec_from_file_location("boundary_audit_for_setup",
                                               ROOT / "scripts" / "boundary-audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules["boundary_audit_for_setup"] = audit   # dataclasses look their module up
_spec.loader.exec_module(audit)

STUBS = {
    "pip": "#!/bin/sh\nexit 0\n",
    "systemctl": "#!/bin/sh\nexit 0\n",
    # `crontab -l` prints the current table (exit 1 when there is none);
    # `crontab -` replaces it once its input ends, as the real one does
    "crontab": ('#!/bin/sh\n'
                'if [ "$1" = "-l" ]; then cat "$CRONTAB_FILE" 2>/dev/null; exit $?; fi\n'
                'if [ "$1" = "-" ]; then\n'
                '    cat > "$CRONTAB_FILE.new" && mv "$CRONTAB_FILE.new" "$CRONTAB_FILE"; exit $?\n'
                'fi\n'
                'exit 2\n'),
}
EARLIER = ("0 1 * * * /usr/bin/true  # the owner's own job\n"
           "0 * * * 1-5 /old/run-agent.sh morning-briefing >> /old/.ea/logs/morning.log 2>&1\n")


def _checkout(base: Path) -> Path:
    """A scratch checkout: the real scripts/install/setup.sh beside stand-ins for what it
    runs, plus the real owner-io.sh and ea_config.py it writes config and
    reads the profile's features through."""
    repo = base / "repo"
    for sub in ("config", "preprocessor", "skills", "scripts", "scripts/install"):
        (repo / sub).mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install/setup.sh", repo / "scripts/install/setup.sh")
    shutil.copy2(ROOT / "scripts" / "owner-io.sh", repo / "scripts" / "owner-io.sh")
    shutil.copy2(ROOT / "preprocessor" / "ea_config.py", repo / "preprocessor" / "ea_config.py")
    (repo / "requirements.txt").write_text("")
    (repo / "CLAUDE.md").write_text("# EA\n")
    (repo / "config" / "ea-config.json").write_text("{}\n")
    (repo / "preprocessor" / "graph_client.py").write_text("")
    for script in ("run-agent.sh", "run-plugin.sh"):
        (repo / script).write_text("#!/bin/sh\n")
        (repo / script).chmod(0o755)
    return repo


def _live_tree(base: Path) -> Path:
    live = base / "opt-ea-system"
    live.mkdir()
    for script in ("run-agent.sh", "run-plugin.sh"):
        (live / script).write_text("#!/bin/sh\n")
        (live / script).chmod(0o755)
    return live


def _plugin(ea_dir: Path, folder: str, manifest) -> None:
    d = ea_dir / "plugins" / folder
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))


def _run_setup(base: Path, live: Path, answer: str = "y", profile: str = "assistant",
               extra_env: dict | None = None, extra_args: list[str] | None = None):
    """scripts/install/setup.sh with stdin from a pipe, so non-interactive by itself: a
    "y" answer is --install-cron, as the setup skill runs it."""
    repo = base / "repo" if (base / "repo").exists() else _checkout(base)
    home = base / "home"
    home.mkdir(exist_ok=True)
    bindir = base / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.path.dirname(shutil.which('python3'))}:/usr/bin:/bin",
        "HOME": str(home),
        "EA_DIR": str(home / ".ea"),
        "EA_LIVE_DIR": str(live),
        "CRONTAB_FILE": str(base / "crontab"),
        "LANG": "C.UTF-8",
        **(extra_env or {}),
    }
    args = ["bash", str(repo / "scripts/install/setup.sh"), "--profile", profile, *(extra_args or [])]
    if answer == "y":
        args.append("--install-cron")
    r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       env=env, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return repo, home, r


def _jobs(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _in(root: Path):
    def inside(path: str) -> bool:
        p = os.path.abspath(path)
        return p == str(root) or p.startswith(str(root) + "/")
    return inside


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_the_template_names_no_resident_log_sink():
    hits = [h for h in bl.lint_shell("scripts/install/setup.sh", (ROOT / "scripts/install/setup.sh").read_text())
            if h.rule == "cron-log"]
    assert not hits, "\n".join(h.render() for h in hits)


def test_jobs_run_from_the_deployed_tree_and_log_to_the_owner_private_dir(tmp_path):
    ea_dir = tmp_path / "home" / ".ea"
    _plugin(ea_dir, "demo", {"name": "demo", "enabled": True, "schedule": "0 12 * * *",
                             "extra_schedules": [{"label": "nightly", "cron": "0 2 * * *",
                                                  "args": "--enrich-pending"}]})
    _plugin(ea_dir, "off", {"name": "off", "enabled": False, "schedule": "0 3 * * *"})
    (tmp_path / "crontab").write_text(EARLIER)
    live = _live_tree(tmp_path)
    repo, home, _ = _run_setup(tmp_path, live)
    crontab = (tmp_path / "crontab").read_text()
    logs = home / ".ea-private" / "logs"

    # the daily audit finds nothing: no job writes into, or runs from, ea-bot space
    assert audit.cron_findings(crontab, _in(ea_dir)) == []
    assert audit.cron_findings(crontab, _in(repo)) == [], "the jobs do not run the checkout"

    jobs = [j for j in _jobs(crontab) if "run-agent.sh" in j or "run-plugin.sh" in j
            or "preprocess.py" in j]
    assert len(jobs) == 8, jobs      # 6 agent lines, the plugin and its nightly pass
    assert not any("preprocess.py" in j for j in jobs), "run-agent.sh runs the preprocessor"
    for job in _jobs(crontab):
        if "/usr/bin/true" in job:
            continue
        sinks = re.findall(r">>\s*(\S+)", job)
        assert sinks and all(_in(logs)(s) for s in sinks), job
        assert f"{live}/run-agent.sh " in job or f"{live}/run-plugin.sh " in job \
            or f"python3 {live}/preprocessor/" in job or f"python3 {live}/scripts/" in job, job
    # the maintenance lines every profile gets, and the ones the chat profile adds
    for script in ("ledger_sync.py check", "closure_scrubber.py check --json", "quiet_hours.py flush",
                   "scheduled_sends.py check", "calendar_reconciler.py --apply", "style_profiles.py sync"):
        assert f"python3 {live}/preprocessor/{script}" in crontab, script
    assert (f"40 6 * * *         python3 {live}/scripts/persistence-digest.py "
            f">> {logs}/persistence-digest.log 2>&1") in crontab
    assert f"{live}/run-agent.sh intake-check" in crontab
    assert f"{live}/run-plugin.sh demo -- --enrich-pending >> {logs}/plugins/demo/" in crontab
    assert "run-plugin.sh off" not in crontab
    # an earlier EA line is replaced; the owner's own line stays
    assert "/old/" not in crontab and "/usr/bin/true  # the owner's own job" in crontab

    assert _mode(home / ".ea-private") == 0o700 and _mode(logs) == 0o700
    assert _mode(logs / "plugins") == 0o700 and _mode(logs / "plugins" / "demo") == 0o700
    assert not (ea_dir / "logs" / "plugins").exists(), "no plugin log dir in ea-bot space"


def test_jobs_fall_back_to_the_checkout_before_the_tree_is_deployed(tmp_path):
    (tmp_path / "home" / ".ea-private").mkdir(parents=True, mode=0o755)
    (tmp_path / "home" / ".ea-private").chmod(0o755)
    repo, home, r = _run_setup(tmp_path, tmp_path / "not-deployed")
    crontab = (tmp_path / "crontab").read_text()
    jobs = _jobs(crontab)
    assert jobs and all(f"{repo}/" in j for j in jobs), jobs
    assert "not-deployed/run" not in crontab
    assert "the jobs run from this checkout" in r.stdout
    assert audit.cron_findings(crontab, _in(home / ".ea")) == []
    assert _mode(home / ".ea-private") == 0o700, "an existing private dir is made the owner's alone"
    assert _mode(home / ".ea-private" / "logs") == 0o700


def test_a_manifest_field_that_is_not_plain_never_becomes_a_cron_line(tmp_path):
    ea_dir = tmp_path / "home" / ".ea"
    _plugin(ea_dir, "shell", {"name": "shell", "enabled": True,
                              "schedule": "* * * * * touch /tmp/owned #"})
    _plugin(ea_dir, "pct", {"name": "pct", "enabled": True, "schedule": "0 1 * * %"})
    _plugin(ea_dir, "badname", {"name": "x\n* * * * * touch /tmp/owned", "enabled": True,
                                "schedule": "0 12 * * *"})
    _plugin(ea_dir, "dots", {"name": "../../bin", "enabled": True, "schedule": "0 12 * * *"})
    _plugin(ea_dir, "extra", {"name": "extra", "enabled": True, "schedule": "0 12 * * *",
                              "extra_schedules": [
                                  {"label": "a\n* * * * * touch /tmp/owned", "cron": "0 2 * * *"},
                                  {"cron": "0 3 * * *", "args": "--x; touch /tmp/owned"},
                                  {"cron": "0 4 * * * touch /tmp/owned"},
                                  {"cron": "0 5 * * *", "args": ["--x"]},
                                  "not a dict"]})
    _plugin(ea_dir, "list", "[1, 2]")
    _plugin(ea_dir, "broken", "{not json")
    live = _live_tree(tmp_path)
    _, home, _ = _run_setup(tmp_path, live, profile="email")
    crontab = (tmp_path / "crontab").read_text()
    assert "owned" not in "\n".join(_jobs(crontab)), crontab
    # the email profile: no chat checks, no intake, no deferred sends, no
    # reconciler; the ledger sync, scrubber, quiet-hours flush and style
    # sync stay, and the profile is recorded
    jobs = "\n".join(_jobs(crontab))
    for absent in ("scheduling-check", "priority-check", "intake-check",
                   "scheduled_sends.py", "calendar_reconciler.py"):
        assert absent not in jobs, absent
    for present in ("ledger_sync.py check", "closure_scrubber.py check", "quiet_hours.py flush",
                    "style_profiles.py sync", "morning-briefing", "afternoon-catchup", "weekly-audit",
                    "scripts/persistence-digest.py"):
        assert present in jobs, present
    assert json.loads((home / ".ea" / "config" / "ea-config.json").read_text())["profile"] == "email"
    plugin_jobs = [j for j in _jobs(crontab) if "run-plugin.sh" in j]
    # the plain plugin line and the one plain extra schedule (its label made one line)
    assert [j.split(">>")[0].split() for j in plugin_jobs] == [
        ["0", "12", "*", "*", "*", f"{live}/run-plugin.sh", "extra"],
        ["0", "2", "*", "*", "*", f"{live}/run-plugin.sh", "extra"],
    ], plugin_jobs
    assert "# EA Plugin: extra (a?* * * * * touch /tmp/owned)" in crontab
    assert audit.cron_findings(crontab, _in(ea_dir)) == []
    assert not (home / ".ea-private" / "bin").exists(), "a name never steers the log dir"


def test_a_rerun_replaces_the_maintenance_lines_instead_of_doubling_them(tmp_path):
    """The kept-entries filter strips every line scripts/install/setup.sh emits, the
    python3 maintenance lines included, so a second run (a profile change,
    or pointing the jobs at the deployed tree) leaves one copy of each."""
    live = _live_tree(tmp_path)
    _run_setup(tmp_path, live, profile="email")
    first = (tmp_path / "crontab").read_text()
    _run_setup(tmp_path, live, profile="assistant")
    second = (tmp_path / "crontab").read_text()
    jobs = _jobs(second)
    assert len(jobs) == len(set(jobs)), "a line is doubled"
    assert sum("ledger_sync.py check" in j for j in jobs) == 1
    assert sum("quiet_hours.py flush" in j for j in jobs) == 1
    assert len(jobs) > len(_jobs(first)), "assistant adds the chat lines"
    assert "profile: assistant" in second and "profile: email" not in second


def test_skipped_entries_are_saved_where_only_the_owner_can_change_them(tmp_path):
    live = _live_tree(tmp_path)
    _, home, r = _run_setup(tmp_path, live, answer="n", profile="email")
    saved = home / ".ea-private" / "crontab-template.txt"
    assert saved.is_file() and _mode(saved) == 0o600
    assert not (home / ".ea" / "crontab-template.txt").exists()
    assert not (tmp_path / "crontab").exists(), "nothing is installed"
    assert audit.cron_findings(saved.read_text(), _in(home / ".ea")) == []
    assert str(saved) in r.stdout


def test_no_terminal_and_no_token_exits_0_after_printing_the_sign_in_command(tmp_path):
    """Claude Code's Bash tool gives scripts/install/setup.sh no terminal. With no Graph
    token the run must not try the sign-in (it would stop at a prompt
    nobody answers): it prints the one command the user runs and goes on."""
    live = _live_tree(tmp_path)
    repo = _checkout(tmp_path)
    # a sign-in attempt would fail the run, as the real one does with no token
    (repo / "preprocessor" / "graph_client.py").write_text(
        "raise RuntimeError('Graph token expired and no refresh token available')\n")
    home = tmp_path / "home"
    (home / ".ea").mkdir(parents=True)
    (home / ".ea" / ".env").write_text("GRAPH_AUTH_METHOD=device_code\nGRAPH_CLIENT_ID=abc\n")
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="email")
    assert "Non-interactive run (standard input is not a terminal)" in r.stdout
    assert "No Graph sign-in yet. Sign in yourself" in r.stdout
    assert f"python3 {repo}/preprocessor/graph_client.py" in r.stdout
    assert "RuntimeError" not in r.stdout + r.stderr
    assert "Install these cron entries?" not in r.stdout
    assert not (tmp_path / "crontab").exists(), "no terminal and no --install-cron: nothing installed"
    assert (home / ".ea-private" / "crontab-template.txt").is_file()


def test_a_git_checkout_gets_the_deploy_hooks(tmp_path):
    """git pull deploys only through the post-merge hook, so scripts/install/setup.sh points
    core.hooksPath at .githooks when the checkout names no hooks directory,
    says so, and leaves a value that is already set alone."""
    live = _live_tree(tmp_path)
    repo = _checkout(tmp_path)
    (repo / ".githooks").mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="email")
    got = subprocess.run(git + ["config", "--get", "core.hooksPath"], capture_output=True, text=True)
    assert got.stdout.strip() == ".githooks"
    assert "core.hooksPath set to .githooks" in r.stdout
    subprocess.run(git + ["config", "core.hooksPath", "/elsewhere"], check=True)
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="email")
    got = subprocess.run(git + ["config", "--get", "core.hooksPath"], capture_output=True, text=True)
    assert got.stdout.strip() == "/elsewhere" and "already /elsewhere" in r.stdout
    guide = (ROOT / "docs/INSTALL.md").read_text()
    upgrade = guide[guide.index("## Upgrade"):guide.index("## Uninstall")]
    assert "git pull" in upgrade and "post-merge" in upgrade and "ea-doctor.py" in upgrade
    uninstall = guide[guide.index("## Uninstall"):guide.index("## If something fails")]
    for step in ("install-services.sh --remove", "/etc/sudoers.d/ea-bot", "userdel", "/opt/ea-system"):
        assert step in uninstall, step


STUB_REGISTRY = '''
import types

LOCK = {"v2demo": [
    {"label": "default", "cron": "0 12 * * *", "args": []},
    {"label": "weekly", "cron": "0 1 * * 1", "args": ["--coverage"]},
    {"label": "bad", "cron": "0 3 * * *", "args": ["--x;", "touch", "/tmp/owned"]}]}


def get_plugin(name):
    if name not in LOCK:
        return None
    return types.SimpleNamespace(name=name, manifest={"capabilities": {"schedules": LOCK[name]}})
'''


def test_a_v2_plugin_takes_its_cron_lines_from_the_owners_lock(tmp_path):
    """A v2 manifest says nothing about enabled or schedules; the owner's
    lock (plugin_registry.get_plugin) does, one cron line per schedule."""
    ea_dir = tmp_path / "home" / ".ea"
    repo = _checkout(tmp_path)
    (repo / "preprocessor" / "plugin_registry.py").write_text(STUB_REGISTRY)
    _plugin(ea_dir, "v2demo", {"manifest_version": 2, "name": "v2demo", "enabled": False,
                               "capabilities": {"schedules": [{"cron": "* * * * *"}]}})
    _plugin(ea_dir, "v2off", {"manifest_version": 2, "name": "v2off", "enabled": True,
                              "capabilities": {"schedules": [{"cron": "0 9 * * *"}]}})
    live = _live_tree(tmp_path)
    _run_setup(tmp_path, live)
    crontab = (tmp_path / "crontab").read_text()
    plugin_jobs = [j.split(">>")[0].split() for j in _jobs(crontab) if "run-plugin.sh" in j]
    assert plugin_jobs == [
        ["0", "12", "*", "*", "*", f"{live}/run-plugin.sh", "v2demo"],
        ["0", "1", "*", "*", "1", f"{live}/run-plugin.sh", "v2demo", "--", "--coverage"],
    ], plugin_jobs
    assert "# EA Plugin: v2demo (weekly)" in crontab
    jobs = "\n".join(_jobs(crontab))
    assert "owned" not in jobs and "v2off" not in jobs


def test_acknowledged_records_the_notice_date_once(tmp_path):
    """--acknowledged writes the date the user read the responsibility
    notice beside the profile; a later run without the flag keeps it,
    and a later run with it does not move the date."""
    live = _live_tree(tmp_path)
    repo, home, r = _run_setup(tmp_path, live, answer="n")
    cfg = home / ".ea" / "config" / "ea-config.json"
    assert "acknowledged" not in json.loads(cfg.read_text())
    _, _, r = _run_setup(tmp_path, live, answer="n", extra_args=["--acknowledged"])
    assert "responsibility notice acknowledged" in r.stdout
    doc = json.loads(cfg.read_text())
    when = doc["acknowledged"]["responsibility"]
    assert doc["profile"] == "assistant" and len(when) == 10 and when[4] == "-"
    doc["acknowledged"]["responsibility"] = "2026-01-02"
    cfg.write_text(json.dumps(doc))
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="email", extra_args=["--acknowledged"])
    doc = json.loads(cfg.read_text())
    assert doc["profile"] == "email" and doc["acknowledged"]["responsibility"] == "2026-01-02"
    _, _, r = _run_setup(tmp_path, live, answer="n", profile="email")
    assert "Profile already recorded" in r.stdout
    assert json.loads(cfg.read_text())["acknowledged"]["responsibility"] == "2026-01-02"

