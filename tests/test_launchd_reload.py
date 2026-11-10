"""deploy/macos/launchd-reload.sh: bootout, wait for launchd to forget the job, bootstrap.

A fake `launchctl` on PATH plays launchd: `print system/<label>` succeeds
while the old instance is still being torn down (the first N calls) and
fails after; `bootstrap` fails with EIO while `print` would still
succeed, as launchd does for a label that is not gone yet (2026-11-09).
Every call is logged, so the tests check the order, not just the exit.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RELOAD = REPO / "deploy" / "macos" / "launchd-reload.sh"
EIO = "Bootstrap failed: 5: Input/output error"


def _fake_launchctl(bindir: Path, gone_after: int) -> Path:
    """`print` succeeds for the first `gone_after` calls; `bootstrap` fails
    with EIO while `print` still succeeds. Calls go to bindir/calls.log."""
    state = bindir / "state"
    state.mkdir()
    (state / "prints").write_text("0")
    script = bindir / "launchctl"
    script.write_text(f"""#!/bin/bash
echo "$*" >> "{bindir}/calls.log"
prints="$(cat "{state}/prints")"
case "$1" in
  bootout) exit 0 ;;
  print)
    echo $((prints + 1)) > "{state}/prints"
    if [ "$prints" -lt {gone_after} ]; then echo "state = running"; exit 0; fi
    echo "Could not find service" >&2; exit 113 ;;
  bootstrap)
    if [ "$prints" -lt {gone_after} ]; then echo "{EIO}" >&2; exit 5; fi
    exit 0 ;;
esac
exit 1
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bindir / "calls.log"


def _run(bindir: Path, plist: Path, *args: str):
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
    return subprocess.run(["bash", str(RELOAD), "org.example.job", str(plist), *args],
                          capture_output=True, text=True, env=env)


def test_the_bootstrap_waits_until_launchd_has_forgotten_the_job(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    log = _fake_launchctl(bindir, gone_after=3)
    plist = tmp_path / "org.example.job.plist"; plist.write_text("<plist/>")
    r = _run(bindir, plist, "--poll", "0.05", "--wait", "10")
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "bootout system/org.example.job"
    prints = [i for i, c in enumerate(calls) if c.startswith("print ")]
    boots = [i for i, c in enumerate(calls) if c.startswith("bootstrap ")]
    assert len(prints) == 4, calls           # three "still here", one "gone"
    assert boots == [len(calls) - 1], calls  # exactly one bootstrap, after the last print
    assert EIO not in r.stderr
    assert "has exited" in r.stdout


def test_a_job_that_never_goes_away_is_reported_with_the_command_to_run(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    log = _fake_launchctl(bindir, gone_after=10 ** 6)
    plist = tmp_path / "org.example.job.plist"; plist.write_text("<plist/>")
    r = _run(bindir, plist, "--poll", "0.05", "--wait", "1", "--tries", "2")
    assert r.returncode == 1
    assert "still being torn down" in r.stderr
    assert "NOT loaded" in r.stderr
    assert f"sudo launchctl bootstrap system {plist}" in r.stderr
    calls = log.read_text().splitlines()
    assert sum(c.startswith("bootstrap ") for c in calls) == 2, calls
    # The retry wait is real (2 s); the test bounds it by --tries 2.


def test_the_old_sequence_fails_on_the_same_fake(tmp_path):
    """bootout then an immediate bootstrap, as the installer did: EIO."""
    bindir = tmp_path / "bin"; bindir.mkdir()
    _fake_launchctl(bindir, gone_after=3)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
    r = subprocess.run(["bash", "-c", 'launchctl bootout system/x 2>/dev/null; launchctl bootstrap system p'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 5 and EIO in r.stderr


def test_the_installer_loads_every_job_through_the_helper():
    services = (REPO / "deploy" / "macos" / "install-services.sh").read_text()
    assert RELOAD.exists() and os.access(RELOAD, os.X_OK)
    assert 'RELOAD="$REPO_DIR/deploy/macos/launchd-reload.sh"' in services
    # No bare bootstrap is left: each would race the old instance.
    assert not re.search(r"^\s*launchctl bootstrap", services, re.M), "a bare launchctl bootstrap remains"
    assert '"$RELOAD" "$j" "$(plist_of "$j")" || ok=0' in services
    assert '"$RELOAD" "$UPD" "$(plist_of "$UPD")" || exit 1' in services
    # The helper is one of the paths checked before any step runs.
    assert '"$RELOAD"; do' in services


def test_the_helper_refuses_a_missing_plist(tmp_path):
    bindir = tmp_path / "bin"; bindir.mkdir()
    _fake_launchctl(bindir, gone_after=0)
    r = _run(bindir, tmp_path / "none.plist")
    assert r.returncode == 1 and "is not a file" in r.stderr
