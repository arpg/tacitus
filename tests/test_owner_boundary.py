#!/usr/bin/env python3
"""The owner's shell scripts treat ea-bot space as untrusted.

The owner (the account that runs the crons, the bot and the mediator) and
the ea-bot user (which runs the LLM) share ~/.ea. These tests pin the
boundary the owner-run scripts keep there, with temp directories standing
in for both sides and a stub `sudo` standing in for the switch to ea-bot:

- an env file is data: a command substitution or backtick in it is never
  run, and only allowlisted keys are loaded (scripts/owner-io.sh env);
- a legacy secrets file that is a symlink, is writable by others or is
  owned by another user is skipped;
- a write into ea-bot space goes through ea-bot, so a symlink planted at
  the target name never turns into a write by the owner;
- the deploy hook runs git only in a checked ea-bot copy, and builds the
  live tree from `git archive` of the committer's repo, never from that copy.
"""
import contextlib
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
import pytest
from conftest import NEEDS_RESIDENT, resident_child_available

REPO = Path(__file__).resolve().parent.parent
OWNER_IO = REPO / "scripts" / "owner-io.sh"
RUN_AGENT = REPO / "run-agent.sh"
RUN_PLUGIN = REPO / "run-plugin.sh"
HOOK = REPO / ".githooks" / "post-commit"

SUDO_STUB = """#!/usr/bin/env bash
# Stands in for sudo: records the call, then refuses (ea-bot has no access
# to the owner's files) or runs the command as the test user.
printf '%s\\n' "$*" >> "$SUDO_LOG"
[ "${SUDO_MODE:-refuse}" = run ] || exit 1
while [ $# -gt 0 ]; do
    case "$1" in
        -n) shift ;;
        -u) shift 2 ;;
        --) shift; break ;;
        -*) shift ;;
        *) break ;;
    esac
done
exec "$@"
"""


def _git_free_env(home: Path, base: dict | None = None) -> dict:
    """base (default: this process's environment) with every GIT_* variable
    removed, HOME at home and an empty global git config there, and no
    system config: a fixture's git reaches only the fixture, never a real
    repository or a real config."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith("GIT_")}
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "gitconfig"
    cfg.touch()
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    return env


def _world(mode: str = "refuse") -> dict:
    """A temp HOME with an owner-only private dir, an ea-bot-like shared
    dir (writable by everyone, not sticky), and a stub sudo first on PATH."""
    root = Path(tempfile.mkdtemp(prefix="owner-boundary-")).resolve()
    private = root / "private"
    shared = root / "shared"
    private.mkdir(mode=0o700)
    shared.mkdir()
    os.chmod(shared, 0o777)
    stub_dir = root / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "sudo"
    stub.write_text(SUDO_STUB)
    stub.chmod(0o755)
    sudo_log = root / "sudo.log"
    sudo_log.write_text("")
    env = _git_free_env(root)
    env.update(EA_PRIVATE_DIR=str(private), SUDO_LOG=str(sudo_log),
               SUDO_MODE=mode, PATH=f"{stub_dir}:{env.get('PATH', '/usr/bin:/bin')}")
    for k in ("EA_DIR", "EA_MODEL", "ZULIP_API_KEY", "SLACK_BOT_TOKEN"):
        env.pop(k, None)
    return {"root": root, "private": private, "shared": shared,
            "sudo_log": sudo_log, "env": env}


def _io(w: dict, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([str(OWNER_IO), *args], input=stdin, capture_output=True,
                          text=True, env=w["env"], timeout=60)


def _env_pairs(stdout: str) -> dict:
    fields = stdout.split("\0")
    assert fields[-1] == "", fields
    fields = fields[:-1]
    assert len(fields) % 2 == 0, fields
    return dict(zip(fields[0::2], fields[1::2]))


def _owner_io_python() -> dict:
    """The Python inside owner-io.sh, loaded as a module namespace."""
    src = OWNER_IO.read_text()
    body = src.split("read -r -d '' _PY <<'PY' || true\n", 1)[1].split("\nPY\n", 1)[0]
    ns = {"__name__": "owner_io_under_test"}
    exec(compile(body, "owner-io.sh:_PY", "exec"), ns)
    return ns


# ── env files are data ──────────────────────────────────────────────────

def test_dotenv_gives_only_allowlisted_non_secret_keys():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    marker = w["root"] / "ran"
    (ea / ".env").write_text("\n".join([
        "# comment",
        "EA_MODEL=opus",
        'GRAPH_SCOPES="Mail.Read Mail.Send"',
        "EA_SLACK_CHANNEL_ID='C123'",
        "export GRAPH_CLIENT_ID=abc-123 # the app id",
        f"EA_SLACK_OWNER_ID=$(touch {marker})",
        f"GRAPH_AUTHORITY=`touch {marker}`",
        "SLACK_BOT_TOKEN=xoxb-must-not-load",
        "PATH=/evil/bin",
        "LD_PRELOAD=/evil/lib.so",
        "BASH_ENV=/evil/rc",
        "PYTHONPATH=/evil",
        "EA_DIR=/evil",
        "EA_PRIVATE_DIR=/evil",
        "this line is not an assignment",
    ]) + "\n")
    r = _io(w, "env", str(ea), str(w["private"]))
    assert r.returncode == 0, r.stderr
    got = _env_pairs(r.stdout)
    assert got == {"EA_MODEL": "opus", "GRAPH_SCOPES": "Mail.Read Mail.Send",
                   "EA_SLACK_CHANNEL_ID": "C123", "GRAPH_CLIENT_ID": "abc-123"}, got
    assert not marker.exists()
    assert "SLACK_BOT_TOKEN" in r.stderr and "xoxb-must-not-load" not in r.stderr
    assert "EA_SLACK_OWNER_ID" in r.stderr and "GRAPH_AUTHORITY" in r.stderr
    assert "1 line(s) that are not KEY=VALUE" in r.stderr


def test_secrets_come_from_the_private_file_first():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    legacy = ea / ".env.secrets"
    legacy.write_text("ZULIP_API_KEY=legacy-key\nSLACK_BOT_TOKEN=xoxb-legacy\n")
    legacy.chmod(0o600)
    private = w["private"] / "secrets.env"
    private.write_text("ZULIP_API_KEY=private-key\n")
    private.chmod(0o600)
    r = _io(w, "env", str(ea), str(w["private"]))
    assert r.returncode == 0, r.stderr
    got = _env_pairs(r.stdout)
    assert got["ZULIP_API_KEY"] == "private-key"
    assert got["SLACK_BOT_TOKEN"] == "xoxb-legacy"
    assert r.stderr == ""


def test_legacy_secrets_symlink_is_ignored():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    real = w["private"] / "elsewhere.env"
    real.write_text("ZULIP_API_KEY=through-a-link\n")
    real.chmod(0o600)
    (ea / ".env.secrets").symlink_to(real)
    r = _io(w, "env", str(ea), str(w["private"]))
    assert r.returncode == 0, r.stderr
    assert "ZULIP_API_KEY" not in _env_pairs(r.stdout)
    assert r.stderr.count("\n") == 1 and "(a symlink)" in r.stderr, r.stderr


def test_legacy_secrets_writable_by_others_is_ignored():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    legacy = ea / ".env.secrets"
    legacy.write_text("ZULIP_API_KEY=group-writable\n")
    legacy.chmod(0o660)
    r = _io(w, "env", str(ea), str(w["private"]))
    assert "ZULIP_API_KEY" not in _env_pairs(r.stdout)
    assert "writable by group or others" in r.stderr, r.stderr


def test_legacy_secrets_owned_by_another_user_is_ignored():
    # A test cannot create a file another user owns, so the owner check
    # runs against a uid that is not the file's.
    ns = _owner_io_python()
    d = Path(tempfile.mkdtemp(prefix="owner-boundary-uid-"))
    f = d / ".env.secrets"
    f.write_text("ZULIP_API_KEY=someone-elses\n")
    f.chmod(0o600)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert ns["read_owner_file"](str(f), uid=os.getuid() + 1) is None
        assert ns["read_owner_file"](str(f)) == "ZULIP_API_KEY=someone-elses\n"
    assert "not owned by the running user" in err.getvalue()


def test_legacy_secrets_fifo_is_skipped_without_blocking():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    os.mkfifo(ea / ".env.secrets", 0o600)
    r = _io(w, "env", str(ea), str(w["private"]))
    assert r.returncode == 0 and "not a regular file" in r.stderr, r.stderr


def test_private_secrets_in_a_shared_dir_are_skipped():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    (w["shared"] / "secrets.env").write_text("ZULIP_API_KEY=planted\n")
    r = _io(w, "env", str(ea), str(w["shared"]))
    assert "ZULIP_API_KEY" not in _env_pairs(r.stdout)
    assert "other users can change its directory" in r.stderr


def _run_script(w: dict, script: Path, *args: str, ea: Path) -> subprocess.CompletedProcess:
    env = dict(w["env"], EA_DIR=str(ea))
    return subprocess.run([str(script), *args], capture_output=True, text=True,
                          env=env, timeout=120)


def test_run_agent_never_runs_env_file_content():
    w = _world()
    ea = w["root"] / "ea"
    ea.mkdir(mode=0o700)
    marks = [w["root"] / f"ran-{i}" for i in range(4)]
    (ea / ".env").write_text(f"EA_MODEL=$(touch {marks[0]})\n"
                             f"EA_SLACK_CHANNEL_ID=`touch {marks[1]}`\n")
    target = w["private"] / "secrets-elsewhere.env"
    target.write_text(f"ZULIP_API_KEY=$(touch {marks[2]})\n")
    target.chmod(0o600)
    (ea / ".env.secrets").symlink_to(target)
    ran = lambda: [m.name for m in marks if m.exists()]
    for script, args in ((RUN_AGENT, ["no-such-agent"]), (RUN_PLUGIN, ["no-such-plugin"])):
        r = _run_script(w, script, *args, ea=ea)
        assert not ran(), f"{script.name} ran env file content: {ran()}"
        assert r.returncode == 1, (script.name, r.returncode, r.stderr[-600:])
        assert "(a symlink)" in r.stderr, (script.name, r.stderr[-600:])
    # a legacy file the owner controls is still loaded, still only as data
    (ea / ".env.secrets").unlink()
    legacy = ea / ".env.secrets"
    legacy.write_text(f"ZULIP_API_KEY=$(touch {marks[3]})\n")
    legacy.chmod(0o600)
    r = _run_script(w, RUN_AGENT, "no-such-agent", ea=ea)
    assert not ran(), f"run-agent.sh ran secrets file content: {ran()}"
    assert r.returncode == 1 and "Unknown agent" in r.stdout, r.stderr[-600:]
    assert "ZULIP_API_KEY" in r.stderr  # refused as not plain data, never run
    assert w["sudo_log"].read_text() == ""


# ── paths: owner space and ea-bot space ────────────────────────────────

def test_paths_are_sorted_into_owner_and_shared():
    w = _world()
    kind = lambda p: _io(w, "check", str(p)).stdout.rstrip("\n").split("\t")
    assert kind(w["private"] / "a" / "b") == ["owner", str(w["private"] / "a" / "b")]
    assert kind(w["shared"] / "x")[0] == "shared"
    # a link planted in shared space is never followed by the owner
    (w["shared"] / "reports").symlink_to(w["private"])
    assert kind(w["shared"] / "reports" / "r.md") == ["shared", str(w["shared"] / "reports" / "r.md")]
    # an owner link into shared space resolves up to the shared dir, so
    # ea-bot gets a path it can reach (the ~/.ea -> ea-bot home case)
    (w["private"] / "ea").symlink_to(w["shared"])
    assert kind(w["private"] / "ea" / "state" / "s.json") == ["shared", str(w["shared"] / "state" / "s.json")]
    # an owner link to an owner dir resolves fully
    (w["private"] / "logs").mkdir()
    (w["private"] / "l").symlink_to(w["private"] / "logs")
    assert kind(w["private"] / "l" / "x.log") == ["owner", str(w["private"] / "logs" / "x.log")]
    # a sticky dir (like /tmp) keeps others off our own entries
    sticky = w["root"] / "sticky"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    assert kind(sticky / "f")[0] == "owner"


def test_a_shared_write_goes_through_ea_bot_and_never_through_a_planted_link():
    w = _world("refuse")
    secret = w["private"] / "target.txt"
    secret.write_text("ORIGINAL")
    link = w["shared"] / "last-collect.json"
    link.symlink_to(secret)
    r = _io(w, "write", str(link), stdin="PLANTED")
    assert r.returncode != 0
    assert secret.read_text() == "ORIGINAL"
    assert f"-n -u ea-bot sh -c umask 002 && exec cat > \"$1\" sh {link}" in w["sudo_log"].read_text()


def test_owner_paths_stay_with_the_owner():
    w = _world("refuse")
    p = w["private"]
    assert _io(w, "mkdir", str(p / "d" / "e")).returncode == 0
    assert _io(w, "write", str(p / "d" / "f.json"), stdin="{}").returncode == 0
    assert _io(w, "read", str(p / "d" / "f.json")).stdout == "{}"
    assert _io(w, "touch", str(p / "d" / "mark")).returncode == 0
    assert _io(w, "mv", str(p / "d" / "f.json"), str(p / "d" / "g.json")).returncode == 0
    assert (p / "d" / "g.json").read_text() == "{}" and not (p / "d" / "f.json").exists()
    # rm removes a link itself, never the file it points to
    (p / "d" / "link").symlink_to(p / "d" / "g.json")
    assert _io(w, "rm", str(p / "d" / "link")).returncode == 0
    assert not (p / "d" / "link").is_symlink() and (p / "d" / "g.json").exists()
    assert w["sudo_log"].read_text() == ""


def test_shared_paths_are_handled_as_ea_bot():
    w = _world("run")
    s = w["shared"]
    assert _io(w, "mkdir", str(s / "state")).returncode == 0
    assert _io(w, "write", str(s / "state" / "x.json"), stdin="[1]").returncode == 0
    assert _io(w, "read", str(s / "state" / "x.json")).stdout == "[1]"
    assert _io(w, "mv", str(s / "state" / "x.json"), str(s / "state" / "y.json")).returncode == 0
    assert _io(w, "rm", str(s / "state" / "y.json")).returncode == 0
    assert not (s / "state" / "y.json").exists()
    log = w["sudo_log"].read_text()
    assert log.count("-n -u ea-bot") == 5, log
    # a FIFO planted in shared space is refused, not read
    os.mkfifo(s / "state" / "fifo")
    assert _io(w, "read", str(s / "state" / "fifo")).returncode != 0
    # a move between the two kinds is refused
    assert _io(w, "mv", str(w["private"] / "a"), str(s / "b")).returncode != 0


def test_plugin_secrets_parse_as_data():
    w = _world()
    f = w["private"] / "plugin.env.secrets"
    f.write_text("API_KEY=abc$def\nTOKEN=\"quoted\"\nGLOB=*\nPATH=/evil\n"
                 "LD_PRELOAD=/evil.so\nPYTHONPATH=/evil\nEA_DIR=/evil\n"
                 "SPACED=a b\n")
    r = _io(w, "kv", str(f))
    entries = r.stdout.split("\0")[:-1]
    assert entries == ["API_KEY=abc$def", "TOKEN=quoted", "GLOB=*"], entries
    assert "EA_DIR, LD_PRELOAD, PATH, PYTHONPATH" in r.stderr and "SPACED" in r.stderr


# ── run-agent.sh and run-plugin.sh write ea-bot space as ea-bot ─────────

def _report_block() -> str:
    src = RUN_AGENT.read_text()
    start = src.index("# Post to Slack via bot token")
    return src[start:src.index("# list-digest: archive the digests only once", start)]


def _run_report_block(w: dict, state: Path, agent: str = "weekly-audit") -> subprocess.CompletedProcess:
    log_file = w["private"] / "run.log"
    script = (
        "set -euo pipefail\n"
        f"PYTHON3={shlex.quote(sys.executable)}\nSCRIPT_DIR={shlex.quote(str(REPO))}\n"
        f"STATE_DIR={shlex.quote(str(state))}\nLOG_FILE={shlex.quote(str(log_file))}\n"
        f"AGENT={agent}\nCLAUDE_OUTPUT='ATTACKER TEXT'\nHAS_SLACK=0\nPROFILE=reviewer\n"
        'VALIDATION=ok\nLIST_DIGEST_DISPOSITION=""\n'
        'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
        + _report_block()
    )
    env = dict(w["env"], EA_DIR=str(w["root"] / "ea"))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env, timeout=120)


def test_run_agent_never_writes_a_report_through_a_planted_link():
    w = _world("refuse")
    state = w["shared"]
    (state / "reports").mkdir()
    os.chmod(state / "reports", 0o777)
    report_target = w["private"] / "owner-file.txt"
    report_target.write_text("ORIGINAL")
    (state / "reports" / "weekly-audit-latest.md").symlink_to(report_target)
    marker_target = w["private"] / "owner-marker.json"
    marker_target.write_text("ORIGINAL")
    (state / "agent-post-success.json").symlink_to(marker_target)
    r = _run_report_block(w, state)
    assert r.returncode == 0, r.stderr[-800:]
    assert report_target.read_text() == "ORIGINAL"
    assert marker_target.read_text() == "ORIGINAL"
    log = w["sudo_log"].read_text()
    assert f"exec cat > \"$1\" sh {state}/reports/weekly-audit-latest.md" in log, log
    assert f"exec cat > \"$1\" sh {state}/agent-post-success.json" in log, log


def test_run_agent_report_and_marker_land_in_shared_space_as_ea_bot():
    w = _world("run")
    state = w["shared"]
    r = _run_report_block(w, state)
    assert r.returncode == 0, r.stderr[-800:]
    assert (state / "reports" / "weekly-audit-latest.md").read_text() == "ATTACKER TEXT\n"
    marker = json.loads((state / "agent-post-success.json").read_text())
    assert marker["weekly-audit"]["validation"] == "ok"
    assert "Report written to" in (w["private"] / "run.log").read_text()
    assert "-n -u ea-bot" in w["sudo_log"].read_text()


def _collect_block() -> str:
    src = RUN_PLUGIN.read_text()
    start = src.index('log "Running collect.py..."')
    return src[start:src.index("COLLECT_SIZE=", start)]


def test_run_plugin_collect_passes_secrets_as_data_and_writes_as_ea_bot():
    w = _world("run")
    plugin = w["shared"] / "plugins" / "demo"
    (plugin / "state").mkdir(parents=True)
    for d in (w["shared"] / "plugins", plugin, plugin / "state"):
        os.chmod(d, 0o777)
    (plugin / "collect.py").write_text(
        "import json, os\n"
        "print(json.dumps({k: os.environ.get(k) for k in ('PLUGIN_TOKEN', 'GLOB', 'PATH', 'EA_PLUGIN_NAME')}))\n")
    (plugin / ".env.secrets").write_text("PLUGIN_TOKEN=tok$en\nGLOB=*\nPATH=/evil\n")
    log_file = w["private"] / "plugin.log"
    script = (
        "set -euo pipefail\n"
        f"PYTHON3={shlex.quote(sys.executable)}\nSCRIPT_DIR={shlex.quote(str(REPO))}\n"
        f"OWNER_IO={shlex.quote(str(OWNER_IO))}\nEA_DIR={shlex.quote(str(w['shared']))}\n"
        f"EA_BOT_HOME={shlex.quote(str(w['shared']))}\nEA_BOT_SCRIPT_DIR={shlex.quote(str(REPO))}\n"
        f"EA_BOT_PLUGIN_DIR={shlex.quote(str(plugin))}\n"
        f"EA_BOT_PLUGIN_STATE={shlex.quote(str(plugin / 'state'))}\n"
        f"EA_BOT_COLLECT_OUT={shlex.quote(str(plugin / 'state' / 'last-collect.json'))}\n"
        f"EA_BOT_PYTHON3={shlex.quote(sys.executable)}\nPLUGIN_NAME=demo\nCHANNEL_KEY=demo\n"
        f"LOG_FILE={shlex.quote(str(log_file))}\nCOLLECT_EXTRA_ARGS=()\n"
        'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
        + _collect_block()
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=w["env"], timeout=120)
    assert r.returncode == 0, (r.stderr[-800:], log_file.read_text()[-800:] if log_file.exists() else "")
    got = json.loads((plugin / "state" / "last-collect.json").read_text())
    assert got == {"PLUGIN_TOKEN": "tok$en", "GLOB": "*",
                   "PATH": "/usr/local/bin:/usr/bin:/bin", "EA_PLUGIN_NAME": "demo"}, got
    log = w["sudo_log"].read_text()
    assert "exec cat > \"$1\" sh " + str(plugin / "state" / "last-collect.json") in log, log


def test_runner_scripts_keep_ea_bot_values_out_of_code():
    for script in (RUN_AGENT, RUN_PLUGIN):
        src = script.read_text()
        assert not re.search(r"^\s*(source|\.)\s", src, re.M), script.name
        assert 'owner-io.sh" env "$EA_DIR"' in src or '"$OWNER_IO" env "$EA_DIR"' in src, script.name
        # No shell value is spliced into inline Python source as a call
        # argument, a subscript, an assignment or a triple-quoted string
        # (the owner's own SCRIPT_DIR in sys.path.insert is the one splice).
        spliced = re.findall(r"(?:\(\s*|\[| = |'')'\$\{?[A-Z_]+", src)
        assert not spliced, (script.name, spliced)
        # deny lists come from the owner's own tree
        assert '"$SCRIPT_DIR/scripts/disallowed-tools.txt"' in src, script.name
        assert "$EA_BOT_SCRIPT_DIR/scripts/disallowed" not in src, script.name
    plugin = RUN_PLUGIN.read_text()
    assert '> "$EA_BOT_COLLECT_OUT"' not in plugin
    assert "$EA_BOT_SCRIPT_DIR/preprocessor/ea_config.py" not in plugin


# ── the deploy hook ─────────────────────────────────────────────────────

def _git(*args: str, cwd: Path) -> str:
    """git in a fixture repo only: no inherited GIT_* variable, the fixture's
    own HOME and global config, no hooks, identity on the command line."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True,
                          env=_git_free_env(cwd.parent / "git-home")).stdout.strip()


def _repo(path: Path, files: dict) -> str:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    for name, (text, mode) in files.items():
        (path / name).write_text(text)
        (path / name).chmod(mode)
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "c", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _hook_functions() -> str:
    hook = HOOK.read_text()
    ok = re.search(r"^deploy_tree_ok\(\) \{\n.*?^\}\n", hook, re.M | re.S)
    git = re.search(r"^deploy_git\(\) \(\n.*?^\)\n", hook, re.M | re.S)
    assert ok and git, "deploy_tree_ok / deploy_git not found in the hook"
    return ok.group(0) + git.group(0)


def _deploy_git(deploy_dir: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    script = (f"DEPLOY_DIR={shlex.quote(str(deploy_dir))}\n" + _hook_functions()
              + "deploy_git " + " ".join(shlex.quote(a) for a in args) + "\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env or _git_free_env(deploy_dir.parent / "git-home"), timeout=60)


def test_hook_runs_git_only_in_a_checked_ea_bot_copy():
    root = Path(tempfile.mkdtemp(prefix="owner-boundary-hook-")).resolve()
    head = _repo(root / "copy", {"a.txt": ("a", 0o644)})
    for d in (root / "copy", root / "copy" / ".git"):
        os.chmod(d, 0o755)
    ok = _deploy_git(root / "copy", "rev-parse", "HEAD")
    assert ok.returncode == 0 and ok.stdout.strip() == head, ok.stderr
    # the hook's own repository variables cannot point git elsewhere
    other = _repo(root / "other", {"b.txt": ("b", 0o644)})
    env = dict(_git_free_env(root / "git-home"), GIT_DIR=str(root / "other" / ".git"),
               GIT_WORK_TREE=str(root / "other"))
    assert _deploy_git(root / "copy", "rev-parse", "HEAD", env=env).stdout.strip() == head != other
    # a symlink where the copy should be
    (root / "swapped").symlink_to(root / "copy")
    assert _deploy_git(root / "swapped", "rev-parse", "HEAD").returncode == 97
    # a .git that is a symlink
    (root / "linked").mkdir(mode=0o755)
    (root / "linked" / ".git").symlink_to(root / "copy" / ".git")
    assert _deploy_git(root / "linked", "rev-parse", "HEAD").returncode == 97
    # a copy others can write
    os.chmod(root / "copy", 0o775)
    assert _deploy_git(root / "copy", "rev-parse", "HEAD").returncode == 97
    os.chmod(root / "copy", 0o755)
    os.chmod(root / "copy" / ".git", 0o757)
    assert _deploy_git(root / "copy", "rev-parse", "HEAD").returncode == 97
    hook = HOOK.read_text()
    assert 'git -C "$DEPLOY_DIR"' not in hook, "every git call in the copy goes through deploy_git"
    assert hook.index("UNSAFE DEPLOY TREE") < hook.index('deploy_git fetch "$REPO_ROOT" HEAD')
    assert subprocess.run(["bash", "-n", str(HOOK)]).returncode == 0


@pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)
def test_hook_builds_the_live_tree_from_the_committers_repo():
    root = Path(tempfile.mkdtemp(prefix="owner-boundary-live-")).resolve()
    src = root / "src"
    head = _repo(src, {"code.py": ("v1\n", 0o664), "run.sh": ("#!/bin/sh\n", 0o775)})
    (src / "code.py").write_text("uncommitted edit\n")
    (src / "untracked.txt").write_text("not in the commit\n")
    copy = root / "ea-bot-copy"
    copy.mkdir()
    (copy / "planted.py").write_text("import os  # planted in ea-bot's copy\n")
    live = root / "live"
    live.mkdir()
    (live / "stale.txt").write_text("from an older deploy\n")
    hook = HOOK.read_text()
    start = hook.index('    if [ -d "$LIVE_DIR" ]; then')
    block = hook[start:hook.index("    # Restart only when code", start)]
    script = (f"LIVE_DIR={shlex.quote(str(live))}\nREPO_ROOT={shlex.quote(str(src))}\n"
              f"DEPLOY_DIR={shlex.quote(str(copy))}\nREPO_HEAD={head}\n" + block + "echo mirrored\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                       env=_git_free_env(root / "git-home"))
    assert r.returncode == 0 and "mirrored" in r.stdout, r.stdout + r.stderr
    names = sorted(p.name for p in live.iterdir())
    assert names == [".deployed-commit", "code.py", "run.sh"], names
    assert (live / "code.py").read_text() == "v1\n"
    assert (live / ".deployed-commit").read_text().strip() == _git("rev-parse", "--short", "HEAD", cwd=src)
    assert stat.S_IMODE((live / "code.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((live / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE(live.stat().st_mode) == 0o755
    assert '"$EXPORT_DIR/" "$LIVE_DIR/"' in block and '"$DEPLOY_DIR/"' not in block


def test_hook_runs_tests_and_evals_without_the_hooks_git_variables():
    hook = HOOK.read_text()
    fn = re.search(r"^without_git_env\(\) \(\n.*?^\)\n", hook, re.M | re.S)
    assert fn, "without_git_env not found in the hook"
    # Stand-in values only: nothing here runs git.
    planted = {"GIT_DIR": "/repo/.git", "GIT_INDEX_FILE": "/repo/.git/index",
               "GIT_WORK_TREE": "/repo", "GIT_PREFIX": "sub/", "GIT_COMMON_DIR": "/repo/.git",
               "GIT_OBJECT_DIRECTORY": "/repo/.git/objects", "GIT_CONFIG": "/repo/.git/config",
               "GIT_CONFIG_PARAMETERS": "'user.name'='t'", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true",
               "GIT_CONFIG_GLOBAL": "/home/someone/.gitconfig"}
    env = dict(os.environ, KEEP_ME="yes", **planted)
    r = subprocess.run(["bash", "-c", fn.group(0) + "without_git_env env\n"],
                       capture_output=True, text=True, env=env, timeout=30)
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert got.get("KEEP_ME") == "yes"
    git_vars = {k for k in got if k.startswith("GIT_")}
    assert git_vars == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}, git_vars
    assert got["GIT_CONFIG_NOSYSTEM"] == "1"
    assert got["GIT_CONFIG_GLOBAL"] != planted["GIT_CONFIG_GLOBAL"]
    assert not Path(got["GIT_CONFIG_GLOBAL"]).exists(), "the private global config is removed after the run"
    assert 'WALL_OUT=$(without_git_env timeout 900 python3 "$REPO_ROOT/tests/run_wall.py"' in hook
    assert 'RESULT=$(without_git_env python3 "$EVAL_RUNNER" --json' in hook


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
