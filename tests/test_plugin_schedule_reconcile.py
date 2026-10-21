#!/usr/bin/env python3
"""Tests for `run-plugin.sh --check-schedules`.

`scripts/install/setup.sh` emits plugin cron lines only when an operator re-runs it by
hand, so `plugin.json` and the crontab drift in both directions: an
enabled plugin with no cron line never runs, and a disabled plugin with a
cron line keeps firing. `--check-schedules` is the detector. These tests drive the real shell
entry point with a synthetic plugins dir and a stub `crontab` on PATH,
so no host state is read or written.

Usage:
    python3 tests/test_plugin_schedule_reconcile.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_PLUGIN = REPO / "run-plugin.sh"


def _run(manifests: dict, crontab_text: str) -> subprocess.CompletedProcess:
    """Run `run-plugin.sh --check-schedules` against synthetic state."""
    tmp = Path(tempfile.mkdtemp(prefix="plugin-sched-"))
    ea = tmp / "ea"
    for name, manifest in manifests.items():
        d = ea / "plugins" / name
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps(manifest))
    (ea / "logs").mkdir(parents=True, exist_ok=True)

    # Stub `crontab` so nothing reads or writes the operator's real one.
    binf = tmp / "bin"
    binf.mkdir()
    stub = binf / "crontab"
    stub.write_text("#!/bin/sh\ncat <<'CRONEOF'\n" + crontab_text + "\nCRONEOF\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["EA_DIR"] = str(ea)
    env["HOME"] = str(tmp)
    env["PATH"] = f"{binf}:{env['PATH']}"
    return subprocess.run([str(RUN_PLUGIN), "--check-schedules"],
                          capture_output=True, text=True, env=env, timeout=60)


ENABLED = {"name": "alpha", "enabled": True, "schedule": "0 12 * * *"}
DISABLED = {"name": "beta", "enabled": False, "schedule": "30 3 * * 1-5"}


def test_clean_install_exits_zero():
    r = _run({"alpha": ENABLED},
             "0 12 * * *  /opt/ea-system/run-plugin.sh alpha >> /dev/null 2>&1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "every manifest agrees" in r.stdout, r.stdout


def test_enabled_but_never_scheduled_is_drift():
    """An enabled plugin that no cron line invokes is drift."""
    r = _run({"alpha": ENABLED}, "0 2 * * *  /opt/something-else.sh")
    assert r.returncode == 1, r.stdout
    assert "NO cron line invokes it" in r.stdout, r.stdout


def test_disabled_but_still_cronned_is_drift():
    """ff-watcher: a no-op run every weekday."""
    r = _run({"beta": DISABLED},
             "30 3 * * 1-5  /opt/ea-system/run-plugin.sh beta >> /dev/null 2>&1")
    assert r.returncode == 1, r.stdout
    assert "DISABLED in plugin.json" in r.stdout, r.stdout


def test_disabled_and_not_cronned_is_clean():
    r = _run({"beta": DISABLED}, "0 2 * * *  /opt/something-else.sh")
    assert r.returncode == 0, r.stdout + r.stderr


def test_schedule_edited_in_manifest_only_is_drift():
    """Manifest says 09:00, cron still fires at 12:00."""
    r = _run({"alpha": dict(ENABLED, schedule="0 9 * * *")},
             "0 12 * * *  /opt/ea-system/run-plugin.sh alpha")
    assert r.returncode == 1, r.stdout
    assert "no matching cron line" in r.stdout, r.stdout


def test_extra_schedules_are_counted():
    """A plugin's nightly --enrich-pending pass is a second required
    line, not an optional one."""
    manifest = dict(ENABLED, extra_schedules=[
        {"label": "nightly-enrich", "cron": "0 2 * * *",
         "args": "--enrich-pending"}])
    both = ("0 12 * * *  /opt/ea-system/run-plugin.sh alpha\n"
            "0 2 * * *  /opt/ea-system/run-plugin.sh alpha -- --enrich-pending")
    assert _run({"alpha": manifest}, both).returncode == 0

    only_one = "0 12 * * *  /opt/ea-system/run-plugin.sh alpha"
    r = _run({"alpha": manifest}, only_one)
    assert r.returncode == 1, r.stdout
    assert "0 2 * * *" in r.stdout, r.stdout


def test_commented_out_cron_line_does_not_count():
    r = _run({"alpha": ENABLED},
             "#0 12 * * *  /opt/ea-system/run-plugin.sh alpha")
    assert r.returncode == 1, r.stdout
    assert "NO cron line invokes it" in r.stdout, r.stdout


def test_name_prefix_is_not_a_match():
    """`run-plugin.sh alpha-beta` must not satisfy plugin `alpha`."""
    r = _run({"alpha": ENABLED},
             "0 12 * * *  /opt/ea-system/run-plugin.sh alpha-beta")
    assert r.returncode == 1, r.stdout
    assert "NO cron line invokes it" in r.stdout, r.stdout


def test_interactive_only_plugin_needs_no_cron():
    """A manifest with no `schedule` is Slack-triggered only."""
    r = _run({"alpha": {"name": "alpha", "enabled": True}}, "")
    assert r.returncode == 0, r.stdout + r.stderr


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
