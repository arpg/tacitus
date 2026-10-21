"""install.sh, the one-command front door.

Each test runs the real script: the prerequisite table on this host and
on a bare PATH, and the whole pre-launch path from a temporary copy of
the checkout with a stub claude and a stub python3 on PATH, so nothing
is installed and no session starts. Nothing here touches ~/.ea, sudo's
configuration or the network.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
SCRIPT = INSTALL.read_text()
BASH = "/bin/bash"
PREREQS = ["git", "curl", "python3", "pip", "sudo", "Claude Code"]


# A sudo that answers yes, first on PATH for the runs that use this host's
# tools: the sudo-rights check depends on the account, and a host whose
# test user has sudo only to ea-bot (the Docker image) would read as
# missing. The rights logic has its own test below.
_SUDO_OK = None


def _sudo_ok_dir() -> str:
    global _SUDO_OK
    if _SUDO_OK is None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="install-sh-sudo-"))
        _stub(d, "sudo", "exit 0\n")
        _SUDO_OK = str(d)
    return _SUDO_OK


def _run(args, cwd=None, path=None, env=None):
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if path is None:
        full["PATH"] = _sudo_ok_dir() + ":" + full.get("PATH", "/usr/bin:/bin")
    full.pop("EA_SRC", None)
    full.pop("EA_REPO", None)
    full.pop("EA_PYTHON", None)
    if path is not None:
        full["PATH"] = path
    if env:
        full.update(env)
    return subprocess.run([BASH, "install.sh", *args], cwd=str(cwd or REPO), env=full,
                          capture_output=True, text=True, timeout=120)


def _stub(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def copy_with_stubs(tmp_path):
    """A temporary copy of the checkout's shape (install.sh, scripts/install/setup.sh,
    preprocessor/, requirements.txt), a stub claude that logs every call,
    a stub python3 that is new enough and already has every package."""
    copy = tmp_path / "ea-system"
    (copy / "preprocessor").mkdir(parents=True)
    (copy / "scripts" / "install").mkdir(parents=True)
    for name in ("install.sh", "scripts/install/setup.sh", "requirements.txt"):
        (copy / name).write_bytes((REPO / name).read_bytes())
    (copy / "install.sh").chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "claude.log"
    _stub(stubs, "claude",
          f'echo "$@" >> {log}\n[ "$1" = --version ] && echo "9.9.9 (Claude Code)"\nexit 0\n')
    _stub(stubs, "python3", '[ "$1" = --version ] && echo "Python 3.12.0"\nexit 0\n')
    _stub(stubs, "sudo", "exit 0\n")
    return copy, f"{stubs}:/usr/bin:/bin", log


def test_bash_syntax():
    r = subprocess.run([BASH, "-n", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert INSTALL.stat().st_mode & stat.S_IXUSR, "install.sh is executable"


def test_check_on_this_host_names_every_prerequisite_and_passes():
    r = _run(["--check"])
    assert r.returncode == 0, r.stdout + r.stderr
    for name in PREREQS:
        assert name in r.stdout, name
    assert "MISSING" not in r.stdout
    assert re.search(r"python3\s+3\.\d+", r.stdout), "names the python version it found"


def test_check_on_a_bare_path_reports_git_and_python_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(["--check"], path=str(empty))
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"MISSING\s+git\s", r.stdout)
    # python3 is also looked for at fixed paths (Homebrew, python3.1x), so a
    # bare PATH may still find one; git and curl are PATH-only.
    assert re.search(r"\bpython3\b", r.stdout)
    assert "run install.sh again" in r.stdout
    # the one install command sits on the row
    assert re.search(r"MISSING\s+git\s+.*(apt-get|dnf|pacman|package manager|brew|xcode-select)", r.stdout)


def test_check_changes_nothing_and_does_not_call_claude(copy_with_stubs):
    copy, path, log = copy_with_stubs
    before = sorted(p.relative_to(copy) for p in copy.rglob("*"))
    r = _run(["--check"], cwd=copy, path=path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.relative_to(copy) for p in copy.rglob("*")) == before
    calls = log.read_text() if log.exists() else ""
    assert calls.strip() in ("", "--version"), calls


def test_no_launch_prints_the_hand_off_and_never_starts_claude(copy_with_stubs):
    copy, path, log = copy_with_stubs
    r = _run(["--no-launch"], cwd=copy, path=path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Checkout: {copy}" in r.stdout
    assert "Claude Code: already installed" in r.stdout
    assert "Python packages: already installed" in r.stdout
    assert f"cd {copy}" in r.stdout
    assert 'claude "/setup"' in r.stdout
    calls = log.read_text() if log.exists() else ""
    assert "/setup" not in calls, calls
    assert "Starting Claude Code" not in r.stdout


def test_a_second_run_says_what_is_done_and_moves_on(copy_with_stubs):
    copy, path, log = copy_with_stubs
    first = _run(["--no-launch"], cwd=copy, path=path)
    second = _run(["--no-launch"], cwd=copy, path=path)
    assert first.returncode == 0 and second.returncode == 0
    assert second.stdout == first.stdout
    assert second.stdout.count("already") >= 2


def test_piped_run_finds_the_checkout_at_ea_src(copy_with_stubs, tmp_path):
    """`curl ... | bash` has no BASH_SOURCE and runs from any directory."""
    copy, path, log = copy_with_stubs
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"PATH": path, "EA_SRC": str(copy)})
    r = subprocess.run([BASH, "-s", "--", "--no-launch"], input=SCRIPT, cwd=str(elsewhere),
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Checkout: already cloned at {copy}" in r.stdout
    assert 'claude "/setup"' in r.stdout
    assert "/setup" not in (log.read_text() if log.exists() else "")


def test_piped_run_refuses_a_path_that_is_not_a_checkout(copy_with_stubs, tmp_path):
    copy, path, log = copy_with_stubs
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "notes.txt").write_text("mine\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"PATH": path, "EA_SRC": str(taken)})
    r = subprocess.run([BASH, "-s", "--", "--no-launch"], input=SCRIPT, cwd=str(tmp_path),
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "not an ea-system checkout" in r.stderr
    assert (taken / "notes.txt").read_text() == "mine\n"


def test_unknown_option_is_refused():
    r = _run(["--bogus"])
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_no_bash_4_only_constructs():
    """macOS ships bash 3.2."""
    bad = re.findall(r"mapfile|readarray|declare -A|\$\{[a-zA-Z_]+,,\}|\$\{[a-zA-Z_]+\^\^?\}|\|&|;;&", SCRIPT)
    assert not bad, bad
    assert "set -eu" in SCRIPT, "the script runs under set -e and set -u"


def test_script_has_no_em_dash():
    assert chr(0x2014) not in SCRIPT


def test_readme_names_the_one_command():
    readme = (REPO / "README.md").read_text()
    assert "./install.sh" in readme
    assert "install.sh | bash" in readme
    assert "--check" in readme


def test_an_interpreter_newer_than_the_wheels_is_passed_over(tmp_path):
    """python3 on PATH is 3.15, which ortools publishes no wheel for: the
    bootstrap takes the python3.12 beside it, and names 3.15 as too new
    when nothing in range exists."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("git", "curl", "sudo", "id", "dirname", "uname", "sed", "mktemp", "ln", "mkdir"):
        real = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if real:
            os.symlink(real, bin_ / tool)
    _stub(bin_, "python3", 'case "$1" in --version) echo "Python 3.15.0";; *) exit 0;; esac')
    _stub(bin_, "python3.12", 'case "$1" in --version) echo "Python 3.12.9";; *) exit 0;; esac')
    r = _run(["--check"], path=str(bin_))
    assert re.search(r"ok\s+python3\s+3\.12\.9 \(.*python3\.12\)", r.stdout), r.stdout
    os.remove(bin_ / "python3.12")
    fixed = [f for f in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3") if os.path.exists(f)]
    if fixed:  # the search also tries these fixed paths, which this host has
        return
    r = _run(["--check"], path=str(bin_))
    assert r.returncode == 1
    assert re.search(r"MISSING\s+python3\s+3\.10 to 3\.14; .*3\.15\.0.* newer than the requirements support", r.stdout), r.stdout


def test_sudo_without_rights_is_missing(tmp_path):
    """A sudo binary that asks for a password, on an account outside the
    admin groups, is no sudo at all for the service-account step."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("git", "curl", "python3", "dirname", "uname", "sed", "mktemp", "ln", "mkdir"):
        real = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if real:
            os.symlink(real, bin_ / tool)
    _stub(bin_, "sudo", "exit 1\n")
    _stub(bin_, "id", '[ "$1" = -nG ] && echo "alice users"\nexit 0\n')
    r = _run(["--check"], path=str(bin_))
    assert re.search(r"MISSING\s+sudo\s+your account has no sudo rights", r.stdout), r.stdout
    _stub(bin_, "id", '[ "$1" = -nG ] && echo "alice sudo"\nexit 0\n')
    r = _run(["--check"], path=str(bin_))
    assert re.search(r"ok\s+sudo\s+prompts for your password", r.stdout), r.stdout
