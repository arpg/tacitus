#!/usr/bin/env python3
"""The container stack (Dockerfile, compose.yaml, deploy/docker/) keeps the
host install's split and pins what it downloads.

- crontab.py writes the jobs of each install profile, with the code in the
  image and the logs in the owner-private directory, takes a v1 plugin
  manifest's fields only when they have their plain shape, schedules a v2
  plugin from the owner's lock alone, prints the owner plugins' cron jobs
  the way ea-plugin.py does, and uses the owner's extra lines only from a
  file the owner alone can change.
- The Dockerfile pins the base image by digest and Claude Code and
  supercronic by checksum, loads no floating frontend, and gives the
  image's interpreter the /usr/bin/python3 name the host tools use.
- compose.yaml publishes no port except Zulip's, on loopback, pins every
  outside image by digest, starts only init, the mediator and the
  scheduler without a profile, and derives the owner's home from one
  OWNER_USER value.
- The Python lock holds exact versions for every top-level package.
- The entrypoint runs every owner role from /opt/ea-system, never from
  the tree in ea-bot's home, renders the install overlay as ea-bot and
  installs it in the deployed tree as the owner, at the start of each
  role that runs Claude there or checks it.
- The setup service runs the entrypoint's setup role once, on its own
  profile with a terminal and the three volumes; the role runs each
  config phase as ea-bot, writes ~/.ea/.env as ea-bot at 0640 and the
  secrets as the owner at 0600, logs in as the right user and ends with
  the doctor; the docker smoke runs that role with the answers file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKER = ROOT / "deploy" / "docker"
FIXTURE_PLUGINS = ROOT / "tests" / "fixtures" / "plugins"
sys.path.insert(0, str(ROOT / "preprocessor"))

import plugin_manifest as PM  # noqa: E402

_spec = importlib.util.spec_from_file_location("ea_docker_crontab", DOCKER / "crontab.py")
crontab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crontab)

CODE = Path("/opt/ea-system")
LOGS = Path("/home/owner/.ea-private/logs")


def _ea_dir(config: dict | None = None) -> Path:
    """A scratch EA directory with a private directory beside it, mode
    0700, as the owner checks on the plugin lock want it."""
    base = Path(tempfile.mkdtemp(prefix="ea-test-docker-"))
    ea = base / "ea"
    (ea / "config").mkdir(parents=True)
    (base / "private").mkdir(mode=0o700)
    if config is not None:
        (ea / "config" / "ea-config.json").write_text(json.dumps(config))
    return ea


def _private(ea: Path) -> Path:
    return ea.parent / "private"


def _render(profile: str, ea: Path, extra: Path | None = None, log_dir: Path = LOGS) -> str:
    return crontab.render(profile, CODE, ea, log_dir, extra or ea / "no-such-extra",
                          _private(ea))


def _lock_entry(plugin_dir: Path, enabled: bool = True) -> dict:
    """A lock entry for a v2 plugin in place, accepted as it stands: what
    ea-plugin.py adopt --accept-capabilities records."""
    norm, errors = PM.load(plugin_dir)
    assert not errors, errors
    digest = PM.capability_digest(PM.capability_summary(norm))
    return {"name": norm["name"], "zone": norm["zone"], "enabled": enabled, "manifest": norm,
            "capability_digest": digest, "accepted": {"digest": digest, "how": "accepted"},
            "files": {}}


def _write_lock(private: Path, entries: list[dict]) -> None:
    lock = {"lock_version": 1, "plugins": {e["name"]: e for e in entries}}
    path = private / "plugins.lock.json"
    path.write_text(json.dumps(lock))
    path.chmod(0o600)


def _jobs(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def test_every_profile_gets_the_briefings_and_maintenance():
    for profile in crontab.PROFILES:
        jobs = _jobs(_render(profile, _ea_dir({"profile": profile})))
        text = "\n".join(jobs)
        for must in ("run-agent.sh morning-briefing", "run-agent.sh afternoon-catchup",
                     "run-agent.sh weekly-audit", "run-agent.sh intake-check",
                     "ledger_sync.py check", "closure_scrubber.py check",
                     "calendar_reconciler.py --apply", "style_profiles.py sync",
                     f"python3 {CODE}/scripts/persistence-digest.py"):
            assert must in text, (profile, must)
        for job in jobs:
            assert f" {CODE}/" in job, job
            assert job.endswith("2>&1") and f">> {LOGS}/" in job, job
            assert "/home/ea-bot" not in job and "/.ea/" not in job, job


def test_chat_jobs_only_in_the_assistant_profile():
    chat = ("scheduling-check", "priority-check", "scheduled_sends.py", "quiet_hours.py",
            "style_analysis.py")
    for profile in ("email", "review-only"):
        text = _render(profile, _ea_dir({"profile": profile}))
        for job in chat:
            assert job not in text, (profile, job)
    text = _render("assistant", _ea_dir({"profile": "assistant"}))
    for job in chat:
        assert job in text, job


def test_admissions_jobs_follow_the_config_switch():
    off = _render("email", _ea_dir({"profile": "email"}))
    assert "admissions_filter.py" not in off and "admissions-digest" not in off
    on = _render("email", _ea_dir({"profile": "email", "admissions_filter": {"enabled": True}}))
    assert "admissions_filter.py sweep" in on and "run-agent.sh admissions-digest" in on


def test_plugin_lines_take_only_plain_fields():
    ea = _ea_dir({"profile": "email"})
    logs = Path(tempfile.mkdtemp(prefix="ea-test-docker-logs-"))
    plugins = ea / "plugins"
    manifests = {
        "good": {"name": "good", "enabled": True, "schedule": "0 12 * * *",
                 "extra_schedules": [{"label": "nightly", "cron": "0 2 * * *",
                                      "args": "--enrich-pending"},
                                     {"label": "bad", "cron": "0 3 * * *", "args": "x; rm -rf /"}]},
        "off": {"name": "off", "enabled": False, "schedule": "0 1 * * *"},
        "badname": {"name": "bad name; true", "enabled": True, "schedule": "0 1 * * *"},
        "badcron": {"name": "badcron", "enabled": True, "schedule": "0 1 * * * ; id"},
    }
    for folder, m in manifests.items():
        (plugins / folder).mkdir(parents=True)
        (plugins / folder / "plugin.json").write_text(json.dumps(m))
    (plugins / "broken").mkdir()
    (plugins / "broken" / "plugin.json").write_text("{not json")
    text = _render("email", ea, log_dir=logs)
    jobs = _jobs(text)
    plugin_jobs = [j for j in jobs if "run-plugin.sh" in j]
    assert plugin_jobs == [
        f"0 12 * * *  {CODE}/run-plugin.sh good >> {logs}/plugins/good/plugin.log 2>&1",
        f"0 2 * * *  {CODE}/run-plugin.sh good -- --enrich-pending >> {logs}/plugins/good/plugin.log 2>&1",
    ], plugin_jobs
    assert (logs / "plugins" / "good").is_dir()
    assert "rm -rf" not in "\n".join(jobs)
    for warn in ("badname", "badcron", "broken", "'good' (bad)"):
        assert any(warn in ln for ln in text.splitlines() if ln.startswith("# WARN")), warn


def test_v1_manifest_schedules_from_its_own_fields():
    ea = _ea_dir({"profile": "email"})
    logs = Path(tempfile.mkdtemp(prefix="ea-test-docker-logs-"))
    (ea / "plugins" / "plain").mkdir(parents=True)
    (ea / "plugins" / "plain" / "plugin.json").write_text(json.dumps(
        {"name": "plain", "enabled": True, "schedule": "30 6 * * *"}))
    jobs = [j for j in _jobs(_render("email", ea, log_dir=logs)) if "run-plugin.sh" in j]
    assert jobs == [f"30 6 * * *  {CODE}/run-plugin.sh plain >> {logs}/plugins/plain/plugin.log 2>&1"]


def _v2_agent_plugin(ea: Path) -> Path:
    dest = ea / "plugins" / "example-notes"
    shutil.copytree(FIXTURE_PLUGINS / "example-notes", dest)
    return dest


def test_v2_manifest_with_an_accepted_lock_schedules_from_the_lock():
    ea = _ea_dir({"profile": "email"})
    logs = Path(tempfile.mkdtemp(prefix="ea-test-docker-logs-"))
    dest = _v2_agent_plugin(ea)
    _write_lock(_private(ea), [_lock_entry(dest)])
    # The live manifest changes after the lock was accepted: the lock wins.
    live = json.loads((dest / "plugin.json").read_text())
    live["capabilities"]["schedules"].append({"label": "planted", "cron": "* * * * *"})
    (dest / "plugin.json").write_text(json.dumps(live))
    text = _render("email", ea, log_dir=logs)
    jobs = [j for j in _jobs(text) if "run-plugin.sh" in j]
    log = f"{logs}/plugins/example-notes/plugin.log"
    assert jobs == [
        f"0 9 * * 1-5  {CODE}/run-plugin.sh example-notes >> {log} 2>&1",
        f"0 2 * * *  {CODE}/run-plugin.sh example-notes -- --sweep >> {log} 2>&1",
    ], jobs
    assert "planted" not in text
    assert "# plugin example-notes (daily)" in text


def test_v2_manifest_without_a_lock_entry_schedules_nothing():
    ea = _ea_dir({"profile": "email"})
    logs = Path(tempfile.mkdtemp(prefix="ea-test-docker-logs-"))
    dest = _v2_agent_plugin(ea)
    text = _render("email", ea, log_dir=logs)
    assert "run-plugin.sh" not in text
    assert "no enabled, accepted entry" in text
    # A lock that holds the plugin disabled, or with capabilities accepted
    # for another manifest, schedules nothing either.
    _write_lock(_private(ea), [_lock_entry(dest, enabled=False)])
    assert "run-plugin.sh" not in _render("email", ea, log_dir=logs)
    stale = _lock_entry(dest)
    stale["accepted"]["digest"] = "0" * 64
    _write_lock(_private(ea), [stale])
    assert "run-plugin.sh" not in _render("email", ea, log_dir=logs)


def test_owner_plugin_cron_jobs_come_from_ea_plugin():
    ea = _ea_dir({"profile": "email"})
    logs = Path(tempfile.mkdtemp(prefix="ea-test-docker-logs-"))
    private = _private(ea)
    dest = private / "plugins" / "example-chat-reader"
    shutil.copytree(FIXTURE_PLUGINS / "example-chat-reader", dest)
    _write_lock(private, [_lock_entry(dest)])
    text = _render("email", ea, log_dir=logs)
    jobs = [j for j in _jobs(text) if "run-owner" in j]
    assert jobs == [f"0 */12 * * *  {sys.executable} {CODE}/scripts/ea-plugin.py run-owner "
                    f"example-chat-reader refresh >> {logs}/plugin-example-chat-reader-refresh.log 2>&1"]
    assert "# Owner plugins (ea-plugin.py cron-lines)" in text
    # The same function ea-plugin.py cron-lines calls, so both agree.
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ea-plugin.py"), "cron-lines"],
                       capture_output=True, text=True,
                       env=dict(os.environ, EA_PRIVATE_DIR=str(private), EA_DIR=str(ea),
                                EA_PLUGIN_LOCAL_COPY="1"))
    assert r.returncode == 0, r.stderr
    assert "run-owner example-chat-reader refresh" in r.stdout
    # Without a lock there is no owner section at all.
    assert "Owner plugins" not in _render("email", _ea_dir({"profile": "email"}), log_dir=logs)


def test_extra_lines_only_from_an_owner_only_file():
    ea = _ea_dir({"profile": "email"})
    extra = ea / "crontab.extra"
    extra.write_text("*/2 * * * *  python3 /opt/ea-system/preprocessor/mail_tagger.py tick\n")
    extra.chmod(0o600)
    assert "mail_tagger.py tick" in _render("email", ea, extra)
    extra.chmod(0o664)
    text = _render("email", ea, extra)
    assert "mail_tagger.py tick" not in text
    assert "# WARN" in text and "writable by no one else" in text


def test_main_reads_the_profile_from_the_config():
    ea = _ea_dir({"profile": "assistant"})
    r = subprocess.run([sys.executable, str(DOCKER / "crontab.py"), "--ea-dir", str(ea),
                        "--private-dir", str(_private(ea)), "--log-dir", str(ea / "logs"),
                        "--extra", str(ea / "none")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "for the assistant profile" in r.stdout and "priority-check" in r.stdout
    bad = _ea_dir({"profile": "everything"})
    r = subprocess.run([sys.executable, str(DOCKER / "crontab.py"), "--ea-dir", str(bad),
                        "--private-dir", str(_private(bad))],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "unknown profile" in r.stderr


def test_dockerfile_pins_its_downloads():
    text = (ROOT / "Dockerfile").read_text()
    base = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", text, re.M)
    assert base and re.search(r"@sha256:[0-9a-f]{64}$", base.group(1)), "base image digest"
    for arg in ("CLAUDE_CODE_SHA256_X64", "CLAUDE_CODE_SHA256_ARM64"):
        assert re.search(rf"^ARG {arg}=[0-9a-f]{{64}}$", text, re.M), arg
    for arg in ("SUPERCRONIC_SHA1_AMD64", "SUPERCRONIC_SHA1_ARM64"):
        assert re.search(rf"^ARG {arg}=[0-9a-f]{{40}}$", text, re.M), arg
    assert re.search(r"^ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert re.search(r"^ARG SUPERCRONIC_VERSION=v\d+\.\d+\.\d+$", text, re.M)
    assert "sha256sum -c" in text and "sha1sum -c" in text
    assert "syntax=" not in text, "a syntax directive pulls a floating frontend image"
    assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", text), "no piped installers"
    assert "pip install --no-deps -r /tmp/requirements.lock" in text


def test_dockerfile_provides_usr_bin_python3():
    """The host tools name /usr/bin/python3 (ea-doctor.py, run-plugin.sh,
    plugin_cards.py, the setup skill); the slim base image has only
    /usr/local/bin/python3, so the image links the name to the same
    interpreter rather than install a second one."""
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"ln -s\S* /usr/local/bin/python3 /usr/bin/python3", text), \
        "the Dockerfile must give /usr/bin/python3 to the image's interpreter"
    assert not re.search(r"apt-get install[^\n\\]*\bpython3\b", text), \
        "a Debian python3 would be a second interpreter without the pinned packages"


def test_compose_ports_images_and_profiles():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = doc["services"]
    always = sorted(n for n, s in services.items() if not s.get("profiles"))
    assert always == ["init", "mediator", "scheduler"], always
    assert services["bot"]["profiles"] == ["assistant"]
    for name in ("agent", "owner", "signin"):
        assert services[name]["profiles"] == ["tools"], name
    ea_image = services["init"]["image"]
    for name, s in services.items():
        image = s.get("image", "")
        if image != ea_image:
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), (name, image)
        for port in s.get("ports") or []:
            assert name == "zulip" and str(port).startswith("127.0.0.1:"), (name, port)
    mounts = services["mediator"]["volumes"]
    assert "ea-data:/home/ea-bot/.ea" in mounts
    # One OWNER_USER value names the owner in the image and its home, so
    # the private volume's mount point follows it.
    assert "ea-private:/home/${OWNER_USER:-owner}/.ea-private" in mounts, mounts
    assert services["mediator"]["build"]["args"]["OWNER_USER"] == "${OWNER_USER:-owner}"
    assert "ea-bot-claude:/home/ea-bot/.claude" in mounts


def test_compose_settings_are_in_the_example():
    compose = (ROOT / "compose.yaml").read_text()
    example = (DOCKER / "env.example").read_text()
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
    used |= set(re.findall(r'environment: "([A-Z][A-Z0-9_]*)"', compose))
    defined = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    assert used - {"EA_IMAGE"} <= defined, sorted(used - defined)


def test_python_lock_is_exact_and_covers_the_top_level():
    def pins(path: Path) -> dict[str, str]:
        out = {}
        for ln in path.read_text().splitlines():
            if ln.strip() and not ln.startswith("#"):
                m = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9A-Za-z.+-]+)", ln.strip())
                assert m, f"{path.name}: not an exact pin: {ln}"
                out[m.group(1).lower().replace("_", "-")] = m.group(2)
        return out
    lock = pins(DOCKER / "requirements.lock")
    top = pins(DOCKER / "requirements.top")
    for name, version in top.items():
        assert lock.get(name) == version, (name, version, lock.get(name))
    wanted = set()
    for ln in (ROOT / "requirements.txt").read_text().splitlines():
        m = re.match(r"([A-Za-z0-9_.-]+)", ln.strip())
        if m and not ln.startswith("#"):
            wanted.add(m.group(1).lower().replace("_", "-"))
    assert wanted - {"pypdf"} <= set(top), sorted(wanted - set(top))


def test_shell_scripts_parse_and_new_files_have_no_dashes():
    for script in ("entrypoint.sh", "lock.sh"):
        r = subprocess.run(["bash", "-n", str(DOCKER / script)], capture_output=True, text=True)
        assert r.returncode == 0, (script, r.stderr)
    files = [ROOT / "Dockerfile", ROOT / "compose.yaml", ROOT / ".dockerignore",
             ROOT / "docs" / "docker.md", *sorted(p for p in DOCKER.iterdir() if p.is_file())]
    for f in files:
        text = f.read_text()
        assert "—" not in text and "–" not in text, f


def test_entrypoint_keeps_the_host_split():
    text = (DOCKER / "entrypoint.sh").read_text()
    assert 'setpriv --reuid="$OWNER_USER"' in text
    assert "setpriv --reuid=ea-bot --regid=ea" in text
    assert "owner-io.sh\" env" in text, "the owner's env comes from the host install's loader"
    assert 'runuser -u ea-bot -- python3 "$CODE_DIR/deploy/docker/prepare_ea_dir.py"' in text
    # root changes nothing in ea-bot space by name: no chown, chmod, install
    # or redirect on a path under ea-bot's home
    for ln in text.splitlines():
        code = ln.split("#", 1)[0]
        if re.search(r"\b(chown|chmod|install)\b|>\s*\"?\$", code):
            assert "$EA_DIR" not in code and "$EA_BOT_HOME" not in code, ln
    docker = (ROOT / "Dockerfile").read_text()
    assert "ALL=(ea-bot) NOPASSWD: ALL" in docker and "visudo -cf" in docker
    for path in ("$ea/auth", "$ea/state/mail-queue", "/home/ea-bot/.claude",
                 '"/home/$OWNER_USER/.ea-private"'):
        assert path in docker, path


def test_owner_roles_run_from_the_owner_copy():
    """The bot, the mediator and the scheduler run as the owner, who holds
    the tokens, so their command lines name /opt/ea-system (parent root)
    and never the tree in ea-bot's home, whose parent ea-bot owns and could
    rename aside to plant code (docs/SECURITY.md, owner services off the
    ea-bot tree). The overlay install, which legitimately names the
    ea-bot tree, lives in its own function, not in a role's command."""
    text = (DOCKER / "entrypoint.sh").read_text()
    assert 'CODE_DIR=/opt/ea-system' in text
    for role in ("bot", "mediator", "scheduler", "owner"):
        body = _role(text, role)
        commands = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
        for bad in ("/home/ea-bot", "$DEPLOY_DIR", "$EA_BOT_HOME", "ea-system"):
            assert bad not in commands, (role, bad, commands)
    for role in ("bot", "mediator", "scheduler"):
        assert '"$CODE_DIR' in _role(text, role), role


def _shell_fn(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{\n") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _role(text: str, name: str) -> str:
    start = text.index(f"\n    {name})\n")
    return text[start:text.index("\n        ;;\n", start)]


def test_entrypoint_renders_and_installs_the_overlay_as_the_right_users():
    text = (DOCKER / "entrypoint.sh").read_text()
    render = _shell_fn(text, "render_overlay")
    install = _shell_fn(text, "install_overlay")
    assert "runuser -u ea-bot -- " in render and "install_overlay.py\" render" in render
    assert 'setpriv --reuid="$OWNER_USER"' in install
    assert 'install_overlay.py" install --tree "$DEPLOY_DIR"' in install
    assert "runuser" not in install.split("render_overlay")[-1], "the owner installs, not ea-bot"
    init = _role(text, "init")
    assert init.index("prepare_volumes") < init.index("render_overlay")
    for role in ("bot", "agent", "owner"):
        body = _role(text, role)
        assert body.index("install_overlay") < body.index("cd "), role
    for role in ("mediator", "scheduler"):
        assert "overlay" not in _role(text, role), role
    assert "CLAUDE.local.md" in (ROOT / ".dockerignore").read_text().splitlines()


def test_the_overlay_steps_write_the_tree_copy(tmp_path=None):
    """Run the entrypoint's install_overlay with runuser and setpriv
    stubbed to record the user and run the rest as this user. Makes its
    own temp dir, so the file's script runner can call it too."""
    tmp_path = Path(tmp_path or tempfile.mkdtemp(prefix="docker-overlay-"))
    text = (DOCKER / "entrypoint.sh").read_text()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "runuser").write_text('#!/bin/bash\necho "runuser $2" >> "$CALLS"\nshift 3\nexec "$@"\n')
    (bindir / "setpriv").write_text('#!/bin/bash\necho "setpriv $1" >> "$CALLS"\n'
                                    'while [ "$1" != env ]; do shift; done\nexec "$@"\n')
    for stub in bindir.iterdir():
        stub.chmod(0o755)
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps(
        {"name": "Test User", "email": "test@example.edu",
         "institution": {"name": "Example University", "domains": ["example.edu"]}}))
    tree = tmp_path / "tree"
    tree.mkdir(mode=0o755)
    tree.chmod(0o755)
    calls = tmp_path / "calls.log"
    script = ("set -euo pipefail\nlog() { printf '%s\\n' \"$*\" >&2; }\n"
              + _shell_fn(text, "render_overlay") + _shell_fn(text, "install_overlay")
              + "install_overlay\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               CALLS=str(calls), EA_DIR=str(ea), EA_BOT_HOME=str(tmp_path),
               OWNER_HOME=str(tmp_path), OWNER_USER="owner", CODE_DIR=str(ROOT),
               DEPLOY_DIR=str(tree), EA_PRIVATE_DIR=str(tmp_path / "no-private"))
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert calls.read_text().splitlines() == ["runuser ea-bot", "setpriv --reuid=owner"]
    rendered = (ea / "config" / "CLAUDE.install.md").read_text()
    assert "Test User" in rendered and "example.edu" in rendered
    assert (tree / "CLAUDE.local.md").read_text() == rendered


def _prepare_module():
    spec = importlib.util.spec_from_file_location("ea_docker_prepare", DOCKER / "prepare_ea_dir.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _volume_tree() -> tuple[Path, Path, Path]:
    base = Path(tempfile.mkdtemp(prefix="ea-test-docker-vol-"))
    ea, claude, code = base / ".ea", base / ".claude", base / "code"
    for d in (ea / "config", ea / "auth", claude, code / "config"):
        d.mkdir(parents=True)
    (code / "config" / "ea-config.json").write_text(json.dumps({"profile": "assistant", "x": 1}))
    (code / "config" / "vip_senders.txt").write_text("# none\n")
    return ea, claude, code


def test_prepare_fills_a_new_volume():
    prep = _prepare_module()
    ea, claude, code = _volume_tree()
    prep.prepare(ea, claude, code, "email", True)
    cfg = json.loads((ea / "config" / "ea-config.json").read_text())
    assert cfg["profile"] == "email" and cfg["x"] == 1
    assert cfg["mail_mediation"] == {"enabled": True}
    assert (ea / "config" / "vip_senders.txt").read_text() == "# none\n"
    assert oct((ea / "config" / "vip_senders.txt").stat().st_mode & 0o777) == "0o640"
    for d in prep.SHARED_DIRS:
        assert (ea / d).is_dir(), d
    assert (ea / "state" / "mail-queue").is_dir()
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert oct((claude / "settings.json").stat().st_mode & 0o777) == "0o600"


def test_prepare_keeps_an_existing_config():
    prep = _prepare_module()
    ea, claude, code = _volume_tree()
    (ea / "config" / "ea-config.json").write_text(json.dumps({"profile": "assistant"}))
    (claude / "settings.json").write_text('{"model": "mine"}\n')
    (ea / "auth" / "mediation-disabled").write_text("")
    prep.prepare(ea, claude, code, "email", True)
    cfg = json.loads((ea / "config" / "ea-config.json").read_text())
    assert cfg == {"profile": "assistant"}, "existing profile kept, rollback file respected"
    assert (claude / "settings.json").read_text() == '{"model": "mine"}\n'
    (ea / "auth" / "mediation-disabled").unlink()
    prep.prepare(ea, claude, code, None, False)
    assert "mail_mediation" not in json.loads((ea / "config" / "ea-config.json").read_text())


def test_prepare_never_writes_through_a_planted_link():
    prep = _prepare_module()
    ea, claude, code = _volume_tree()
    target = ea.parent / "outside.txt"
    target.write_text("untouched\n")
    (ea / "config" / "vip_senders.txt").symlink_to(target)
    prep.prepare(ea, claude, code, "email", False)
    assert target.read_text() == "untouched\n"
    assert (ea / "config" / "vip_senders.txt").is_symlink()


def test_adopt_file_keeps_the_content():
    prep = _prepare_module()
    ea, _claude, _code = _volume_tree()
    f = ea / "config" / "user-profile.json"
    f.write_text('{"name": "A"}\n')
    f.chmod(0o644)
    prep.adopt_file(f, 0o640)
    assert f.read_text() == '{"name": "A"}\n'
    assert oct(f.stat().st_mode & 0o777) == "0o640"
    assert not list(f.parent.glob(".*.adopt"))


def test_compose_setup_service_runs_the_role_once_with_a_terminal():
    """`docker compose run --rm setup` is the first configuration: its own
    profile keeps it out of `up`, a terminal carries the questions and the
    logins, the three volumes take what it writes, and the host's network
    lets a confidential-client redirect to localhost:8400 reach it."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / "compose.yaml").read_text())
    s = doc["services"]["setup"]
    assert s["profiles"] == ["setup"]
    assert s["entrypoint"] == ["/usr/local/bin/ea-entrypoint", "setup"]
    assert s["command"] == []
    assert s["stdin_open"] is True and s["tty"] is True
    for mount in ("ea-data:/home/ea-bot/.ea",
                  "ea-private:/home/${OWNER_USER:-owner}/.ea-private",
                  "ea-bot-claude:/home/ea-bot/.claude"):
        assert mount in s["volumes"], mount
    assert s["network_mode"] == "host"
    assert "init" in s["depends_on"]
    assert "restart" not in s


def test_entrypoint_setup_role_runs_the_phases_as_ea_bot_in_order():
    """The config phases run as ea-bot in its deployed tree, in the order a
    full scripts/install/user_init.py run asks them; auth, tone, push and summary are not
    among them (the role's own steps, the owner's push, nothing). The
    owner's push phase names no ea-bot step."""
    text = (DOCKER / "entrypoint.sh").read_text()
    assert 'setup_role "$@"' in _role(text, "setup")
    sys.path.insert(0, str(ROOT / "scripts" / "install"))
    import user_init
    m = re.search(r'^SETUP_PHASES="([^"]+)"$', text, re.M)
    assert m, "the entrypoint lists the phases in SETUP_PHASES"
    listed = m.group(1).split()
    assert set(listed) <= set(user_init.ALL_PHASES), set(listed) - set(user_init.ALL_PHASES)
    assert listed == [p for p in user_init.ALL_PHASES if p in listed], "a full run's order"
    for must in ("identity", "scheduling", "inbox", "autonomy", "groups", "deadlines",
                 "style", "reimbursement", "vip"):
        assert must in listed, must
    for absent in ("auth", "tone", "push", "summary", "state"):
        assert absent not in listed, absent
    ea_bot = _shell_fn(text, "run_as_ea_bot")
    assert "setpriv --reuid=ea-bot --regid=ea --init-groups" in ea_bot
    assert "umask 027" in ea_bot and 'cd "$1"' in ea_bot and '"$DEPLOY_DIR"' in ea_bot
    phases = _shell_fn(text, "setup_config_phases")
    assert ('run_as_ea_bot python3 "$DEPLOY_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}" '
            '--phase "$phase"') in phases
    assert "for phase in $SETUP_PHASES; do" in phases
    state = _shell_fn(text, "setup_state_phase")
    assert 'run_as_ea_bot python3 "$DEPLOY_DIR/scripts/install/user_init.py"' in state and "--phase state" in state
    assert '"${SETUP_CHAT_ARGS[@]}"' in state, "the Zulip chat block rides on the state phase"
    push = _shell_fn(text, "setup_push_phase")
    assert 'run_as_owner python3 "$CODE_DIR/scripts/install/user_init.py"' in push and "--phase push" in push
    assert "ea_bot" not in push and "ea-bot" not in push


def test_entrypoint_setup_role_writes_env_as_ea_bot_0640_and_secrets_as_owner_0600():
    text = (DOCKER / "entrypoint.sh").read_text()
    env = _shell_fn(text, "write_ea_env")
    assert "run_as_ea_bot python3" in env and "env_file.py" in env
    assert '--path "$EA_DIR/.env" --mode 0640' in env
    assert "run_as_owner" not in env
    secret = _shell_fn(text, "write_owner_secret")
    assert "run_as_owner python3" in secret and "env_file.py" in secret
    assert '--path "$PRIV_DIR/secrets.env" --mode 0600' in secret
    assert "ea_bot" not in secret
    # the secret travels on stdin, never in an argument
    assert "printf '%s\\n' \"$2\" | jq -R" in secret
    graph = _shell_fn(text, "setup_graph_env")
    assert "write_owner_secret GRAPH_CLIENT_SECRET" in graph and "write_ea_env" in graph
    for key in ("GRAPH_AUTH_METHOD", "GRAPH_CLIENT_ID", "GRAPH_AUTHORITY", "GRAPH_REDIRECT_URI"):
        assert key in graph, key
    # ea-bot's copy of the answers has no secret in it
    m = re.search(r"^SETUP_SECRET_KEYS='(\[.*\])'$", text, re.M)
    assert m, "SETUP_SECRET_KEYS names the answer keys ea-bot never reads"
    keys = json.loads(m.group(1))
    for key in ("graph-client-secret", "slack-bot-token", "slack-app-token", "zulip-api-key"):
        assert key in keys, key
    copy = _shell_fn(text, "setup_answers_copy")
    assert "delpaths" in copy and "$SETUP_SECRET_KEYS" in copy
    chat = _shell_fn(text, "setup_chat_env")
    assert '[ "$SETUP_PROFILE" = assistant ] || return 0' in chat, "chat values only with chat"
    for key in ("EA_SLACK_CHANNEL_ID", "EA_SLACK_CHANNEL_NAME", "EA_SLACK_OWNER_ID",
                "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ZULIP_API_KEY", "--chat zulip"):
        assert key in chat, key


def test_entrypoint_setup_role_logs_in_as_the_right_users_and_ends_with_the_doctor():
    text = (DOCKER / "entrypoint.sh").read_text()
    login = _shell_fn(text, "setup_claude_login")
    assert "run_as_ea_bot claude auth login" in login
    assert "run_as_ea_bot claude setup-token" in login
    assert "run_as_owner" not in login
    assert 'if [ -n "$SETUP_ANSWERS" ]' in login, "--answers skips the login"
    graph = _shell_fn(text, "setup_graph_signin")
    assert 'run_as_owner_with_env python3 "$CODE_DIR/preprocessor/graph_client.py"' in graph
    assert "ea_bot" not in graph
    doctor = _shell_fn(text, "setup_doctor")
    assert 'run_as_owner_with_env python3 "$CODE_DIR/scripts/ea-doctor.py"' in doctor
    assert '--profile "$SETUP_PROFILE" --expect-missing "$expect"' in doctor
    assert 'expect="EA-Managed calendar"' in doctor
    assert "ea-bot: Claude login" in doctor and "auth: msal_token_cache.json" in doctor
    role = _shell_fn(text, "setup_role")
    order = ["prepare_volumes", "setup_answers_copy", "setup_config_phases", "setup_graph_env",
             "setup_chat_env", "setup_state_phase", "setup_push_phase", "setup_claude_login",
             "setup_graph_signin", "setup_tone_phase", "setup_google_signin", "install_overlay",
             "setup_doctor"]
    positions = [role.index(step) for step in order]
    assert positions == sorted(positions), "the role's steps run in the documented order"
    assert role.rstrip().endswith("setup_doctor\n}"), "the doctor's status is the role's"
    assert '[ ! -t 0 ]' in role, "without --answers the role needs a terminal"


def test_entrypoint_setup_role_reads_tone_as_the_owner_and_writes_it_as_ea_bot():
    """The owner holds the mail token, so it computes the style baseline to
    a file; ea-bot writes that file into its own config."""
    text = (DOCKER / "entrypoint.sh").read_text()
    tone = _shell_fn(text, "setup_tone_phase")
    assert '[ "$SETUP_GRAPH_SIGNED_IN" = 1 ] || return 0' in tone, "no token, no tone"
    assert ('run_as_owner_with_env python3 "$CODE_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}"'
            in tone)
    assert '--phase tone --out "$dir/baseline.json"' in tone
    assert 'chown "$OWNER_USER" "$dir"' in tone and 'rm -rf "$dir"' in tone
    assert "run_as_ea_bot sh -c" in tone
    assert '"$EA_DIR/config/writing-style-baseline.json" < "$dir/baseline.json"' in tone
    assert tone.index("run_as_owner_with_env") < tone.index("run_as_ea_bot")


def test_docker_smoke_runs_the_setup_role_with_the_answers_file():
    smoke = ROOT / "tests" / "install" / "docker-smoke.sh"
    r = subprocess.run(["bash", "-n", str(smoke)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = smoke.read_text()
    assert '"$IMAGE" setup --answers /answers.json' in text
    assert "answers-email.json" in text
    for mount in (":/home/ea-bot/.ea", "/.ea-private", ":/home/ea-bot/.claude",
                  '"$ANSWERS:/answers.json:ro"'):
        assert mount in text, mount
    assert "docker volume create" in text and "docker volume rm" in text
    assert "trap cleanup EXIT" in text
    assert "no unexpected FAIL" in text, "the doctor's verdict decides"
    # the host path it ran before is still there
    assert "tests/install/smoke.sh" in text and "useradd" in text


def _env_file_module():
    spec = importlib.util.spec_from_file_location("ea_docker_env_file", DOCKER / "env_file.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_file_merges_in_place_sets_the_mode_and_refuses_a_link():
    ef = _env_file_module()
    d = Path(tempfile.mkdtemp(prefix="ea-test-docker-env-"))
    p = d / ".env"
    p.write_text("# note\nGRAPH_CLIENT_ID=old\nEA_MODEL=x\n")
    ef.write(p, {"GRAPH_CLIENT_ID": "new", "GRAPH_AUTH_METHOD": "device_code"}, 0o640)
    assert p.read_text() == "# note\nGRAPH_CLIENT_ID=new\nEA_MODEL=x\nGRAPH_AUTH_METHOD=device_code\n"
    assert oct(p.stat().st_mode & 0o777) == "0o640"
    r = subprocess.run([sys.executable, str(DOCKER / "env_file.py"), "--path", str(p),
                        "--mode", "0600"], input='{"EA_MODEL": "y"}', capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    assert "EA_MODEL=y" in p.read_text() and oct(p.stat().st_mode & 0o777) == "0o600"
    new = d / "sub" / "secrets.env"
    ef.write(new, {"GRAPH_CLIENT_SECRET": "s"}, 0o600)
    assert new.read_text() == "GRAPH_CLIENT_SECRET=s\n"
    assert oct(new.stat().st_mode & 0o777) == "0o600"
    target = d / "target"
    target.write_text("untouched\n")
    link = d / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        ef.write(link, {"A": "b"}, 0o600)
    assert target.read_text() == "untouched\n"
    for bad in ({"bad key": "x"}, {"A": "x\ny"}):
        with pytest.raises(SystemExit):
            ef.write(p, bad, 0o600)


def test_docs_say_what_setup_asks_and_how_its_network_works():
    """docs/docker.md says the setup role runs the tone phase (it does,
    after the mail sign-in) and that setup and signin need host
    networking, which Docker Desktop turns on only by a setting, with the
    device-code flow as the way around it."""
    docs = (ROOT / "docs" / "docker.md").read_text()
    entry = (DOCKER / "entrypoint.sh").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    assert "setup_tone_phase\n" in entry and "`tone` is not asked" not in docs
    assert "The tone phase" in docs
    assert compose.count("network_mode: host") == 2, "setup and signin"
    assert "network_mode: host" in docs and "Enable host networking" in docs
    assert "device_code" in docs[docs.index("network_mode: host"):][:900]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except pytest.skip.Exception as e:
            print(f"  ⏭️ {t.__name__}: skipped ({e})")
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
