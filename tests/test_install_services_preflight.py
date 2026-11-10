"""deploy/macos/install-services.sh refuses a bad run before it touches anything.

The script is root-only and macOS-only, so the tests play its host: fakes
for `uname`, `id`, `dscl`, `sudo` and `launchctl` on PATH, SUDO_USER set,
scratch homes for the owner and ea-bot, and a copy of the script inside a
scratch "repository" (the script takes the repository from its own
location). `launchctl` logs every call: the point of each test is that
the log stays empty.

Observed 2026-11-09 on a macOS install: run from /opt/ea-system (the
deployed copy, which has no .git and no venv) the script took a bare
Homebrew python3, ran the chat check on it, and failed at step 1 with
"not a git repository". Had the chat check failed on that interpreter,
the branch for "no chat platform" would have unloaded the bot and removed
its plist before the step-1 error.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "macos" / "install-services.sh"


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _host(tmp_path: Path, *, git: bool, python_ok: bool, sudo_stderr: str = ""):
    """A scratch repository with the script in it, two homes, and the fakes."""
    repo = tmp_path / "repo"
    (repo / "deploy" / "macos").mkdir(parents=True)
    (repo / "preprocessor").mkdir()
    shutil.copy(SCRIPT, repo / "deploy" / "macos" / "install-services.sh")
    (repo / "deploy" / "macos" / "ea-service-run.sh").write_text("#!/bin/bash\n")
    (repo / "deploy" / "macos" / "launchd-reload.sh").write_text("#!/bin/bash\n")
    (repo / "preprocessor" / "owner_trust.py").write_text("")
    (repo / "preprocessor" / "ea_config.py").write_text("")
    if git:
        (repo / ".git").mkdir()
    owner = tmp_path / "owner"
    (owner / ".ea-private").mkdir(parents=True, mode=0o700)
    shim = owner / ".local" / "share" / "ea-system" / "bin"
    shim.mkdir(parents=True)
    flags = "" if python_ok else "-S"   # -S: no site-packages, so no `requests`
    _exe(shim / "python3", f'#!/bin/bash\nexec "{sys.executable}" {flags} "$@"\n')
    eabot = tmp_path / "eabot"
    (eabot / "ea-system").mkdir(parents=True)
    (eabot / "ea-system" / "ea-chat-bot.py").write_text("")
    (eabot / ".ea").mkdir()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = bindir / "launchctl.log"
    _exe(bindir / "uname", "#!/bin/bash\necho Darwin\n")
    _exe(bindir / "id", '#!/bin/bash\n'
         'if [ "$1" = -u ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi\n'   # root
         'if [ "$1" = -u ]; then exec /usr/bin/id -u; fi\n'                # the owner: us
         'if [ "$1" = -gn ]; then exec /usr/bin/id -gn; fi\n'
         'exec /usr/bin/id "$@"\n')
    _exe(bindir / "dscl", '#!/bin/bash\n'
         f'case "$3" in /Users/ea-bot) echo "NFSHomeDirectory: {eabot}" ;; *) echo "NFSHomeDirectory: {owner}" ;; esac\n')
    _exe(bindir / "launchctl", f'#!/bin/bash\necho "$*" >> "{calls}"\nexit 0\n')
    _exe(bindir / "sudo", '#!/bin/bash\n'
         f'[ -n "{sudo_stderr}" ] && echo "{sudo_stderr}" >&2\nexit 1\n')
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "SUDO_USER": "owner", "HOME": str(owner)}
    return repo, calls, env


def _run(repo: Path, env: dict):
    return subprocess.run(["bash", str(repo / "deploy" / "macos" / "install-services.sh")],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)


def test_run_from_a_tree_with_no_git_it_stops_before_anything(tmp_path):
    repo, calls, env = _host(tmp_path, git=False, python_ok=True)
    r = _run(repo, env)
    assert r.returncode == 1
    assert "is not a git checkout (no .git)" in r.stderr
    assert "Run the script from the checkout" in r.stderr
    assert not calls.exists(), calls.read_text()


def test_an_interpreter_without_the_requirements_is_refused(tmp_path):
    repo, calls, env = _host(tmp_path, git=True, python_ok=False)
    r = _run(repo, env)
    assert r.returncode == 1
    assert "has no 'requests'" in r.stderr
    assert "requirements.txt" in r.stderr
    assert not calls.exists(), calls.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="the private-dir check uses BSD stat")
def test_a_chat_check_that_could_not_run_does_not_unload_the_bot(tmp_path):
    repo, calls, env = _host(tmp_path, git=True, python_ok=True,
                             sudo_stderr="Traceback (most recent call last): boom")
    r = _run(repo, env)
    assert r.returncode == 1
    assert "the chat check (ea_config.py feature chat) failed" in r.stderr
    assert "boom" in r.stderr
    assert not calls.exists(), calls.read_text()   # no bootout of the bot


def test_the_checks_come_before_the_first_side_effect():
    s = SCRIPT.read_text()
    # The first thing the install path changes: the bot's job, in the
    # branch for a profile with no chat platform.
    first_effect = s.index('launchctl bootout "system/$BOT"')
    assert s.index('"$REPO_DIR/.git"') < first_effect
    assert s.index("import requests") < first_effect
    assert s.index('feature chat 2>&1 >/dev/null') < first_effect
