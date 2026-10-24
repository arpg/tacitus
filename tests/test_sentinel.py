#!/usr/bin/env python3
"""Tests for preprocessor/sentinel.py (dead-man's switch).

The sentinel is the independent cron observer that notices a Slack bot
left dead or disabled with nothing watching. These tests pin each
check's pass/fail logic, the fingerprint anti-spam, the fallback-channel
path, and the recovery message.
"""

import json
import os
import sys
import tempfile

# A temp path with no symlinked ancestor: the tree checks open each
# component without following a link, and macOS puts /var behind one.
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, and sentinel then reads shared state with the
# resident's access, which cannot reach this test's temp files.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-sentinel-")
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import sentinel  # noqa: E402


def _setup(tmp: Path):
    """Point sentinel at a temp EA_DIR layout."""
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    sentinel.EA_DIR = tmp
    sentinel.STATE_FILE = tmp / "state" / "sentinel-state.json"
    sentinel.BOOTSTRAP_FILE = tmp / "state" / "bootstrap.json"
    sentinel.POST_SUCCESS_FILE = tmp / "state" / "agent-post-success.json"
    sentinel.LAST_RUN_FILE = tmp / "state" / "agent-last-run.json"
    sentinel.DEFERRED_FILE = tmp / "state" / "deferred-posts.json"
    sentinel.LEDGER_SYNC_LOG = tmp / "logs" / "ledger-sync.log"
    sentinel.LOG_DIR = tmp / "logs"
    sentinel.SENTINEL_LOG = tmp / "logs" / "sentinel.log"
    sentinel.CRONTAB_MASTER = tmp / "crontab.master"


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def _fake_systemctl(mapping):
    """Return a fake subprocess.run keyed on the systemctl verb."""
    def run(cmd, **kw):
        if cmd[0] == "crontab":
            return _FakeProc(mapping.get("crontab -l", ""))
        key = " ".join(c for c in cmd[1:] if not c.startswith("-")
                       or c == "--user")
        for k, v in mapping.items():
            if k in " ".join(cmd):
                return _FakeProc(v)
        return _FakeProc("")
    return run


# ---------------------------------------------------------------------------
# Service checks
# ---------------------------------------------------------------------------

def test_service_active_ok():
    orig = sentinel.subprocess.run
    sentinel.subprocess.run = _fake_systemctl({"is-active": "active"})
    try:
        ok, detail = sentinel.check_service_active()
        assert ok, detail
    finally:
        sentinel.subprocess.run = orig


def test_service_inactive_fails():
    orig = sentinel.subprocess.run
    sentinel.subprocess.run = _fake_systemctl({"is-active": "inactive"})
    try:
        ok, detail = sentinel.check_service_active()
        assert not ok
        assert "NOT listening" in detail
    finally:
        sentinel.subprocess.run = orig


def test_service_disabled_fails():
    orig = sentinel.subprocess.run
    sentinel.subprocess.run = _fake_systemctl({"is-enabled": "disabled"})
    try:
        ok, detail = sentinel.check_service_enabled()
        assert not ok
        assert "reboot" in detail
    finally:
        sentinel.subprocess.run = orig


def test_duplicate_user_unit_fails():
    orig = sentinel.subprocess.run
    sentinel.subprocess.run = _fake_systemctl({"--user is-active": "active",
                                               "is-active": "active"})
    try:
        ok, detail = sentinel.check_no_user_unit()
        assert not ok
        assert "duplicate" in detail.lower()
    finally:
        sentinel.subprocess.run = orig


# ---------------------------------------------------------------------------
# Briefing-posted check
# ---------------------------------------------------------------------------

def _fake_local_now(dt):
    return lambda: dt


def test_briefing_new_install_grace():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        # POST_SUCCESS_FILE absent entirely -> pass quietly
        ok, detail = sentinel.check_briefing_posted()
        assert ok
        assert "new install" in detail


def test_briefing_posted_today_ok():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        now = datetime(2026, 6, 10, 10, 0)  # Wednesday 10:00
        sentinel._local_now = _fake_local_now(now)
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"morning-briefing": {"date": "2026-06-10", "ts": "x"}}))
            ok, detail = sentinel.check_briefing_posted()
            assert ok, detail
        finally:
            sentinel._local_now = orig


def test_briefing_ran_but_post_failed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 10, 0))
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"morning-briefing": {"date": "2026-06-09", "ts": "x"}}))
            sentinel.LAST_RUN_FILE.write_text(json.dumps(
                {"morning-briefing": "2026-06-10"}))
            ok, detail = sentinel.check_briefing_posted()
            assert not ok
            assert "post never" in detail.lower() or "post" in detail
            assert "RAN" in detail
        finally:
            sentinel._local_now = orig


def test_briefing_never_ran():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 10, 0))
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"morning-briefing": {"date": "2026-06-09", "ts": "x"}}))
            ok, detail = sentinel.check_briefing_posted()
            assert not ok
            assert "NEVER RAN" in detail
        finally:
            sentinel._local_now = orig


def test_briefing_bootstrap_grace_regression_20260609():
    """A missing key must be excused while the marker file is younger
    than one agent cycle. The first agent to write the file ends the
    per-file grace with only its own key, so every other agent's key is
    absent and would otherwise read as 'RAN but post never
    succeeded'."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 9, 15, 10))  # Tue
        try:
            # marker file exists but has ONLY afternoon-catchup (just ran)
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-09", "ts": "x"}}))
            # the briefing DID run today (gate marker), pre-marker-system
            sentinel.LAST_RUN_FILE.write_text(json.dumps(
                {"morning-briefing": "2026-06-09"}))
            ok, detail = sentinel.check_briefing_posted()
            assert ok, f"bootstrap grace must apply: {detail}"
            assert "grace" in detail
            # the weekly audit check gets the same grace
            ok, detail = sentinel.check_weekly_audit()
            assert ok, f"weekly bootstrap grace must apply: {detail}"
        finally:
            sentinel._local_now = orig


def test_briefing_missing_key_after_grace_fails():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 11, 10, 0))  # Thu
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-10", "ts": "x"}}))
            # bootstrap anchor 30h ago -> daily grace (26h) expired
            sentinel.BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            sentinel.BOOTSTRAP_FILE.write_text(json.dumps(
                {"marker_first_seen_ts": time.time() - 30 * 3600}))
            sentinel.LAST_RUN_FILE.write_text(json.dumps(
                {"morning-briefing": "2026-06-11"}))
            ok, detail = sentinel.check_briefing_posted()
            assert not ok, "grace must expire after one daily cycle"
            assert "RAN" in detail
            # weekly grace (8d) still active at 30h
            ok, detail = sentinel.check_weekly_audit()
            assert ok, detail
        finally:
            sentinel._local_now = orig


def test_weekly_missing_key_after_8d_fails():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
            {"morning-briefing": {"date": "2026-06-20", "ts": "x"}}))
        sentinel.BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        sentinel.BOOTSTRAP_FILE.write_text(json.dumps(
            {"marker_first_seen_ts": time.time() - 9 * 24 * 3600}))
        ok, detail = sentinel.check_weekly_audit()
        assert not ok, "weekly grace must expire after 8 days"


def test_bootstrap_anchor_persists():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        sentinel.POST_SUCCESS_FILE.write_text("{}")
        age1 = sentinel._marker_system_age_hours()
        assert age1 is not None and age1 < 0.1
        anchored = json.loads(sentinel.BOOTSTRAP_FILE.read_text())
        # Second call must reuse the anchor, not re-stamp it
        sentinel._marker_system_age_hours()
        assert json.loads(sentinel.BOOTSTRAP_FILE.read_text()) == anchored


def test_briefing_weekend_skip():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 13, 10, 0))  # Saturday
        try:
            sentinel.POST_SUCCESS_FILE.write_text("{}")
            ok, detail = sentinel.check_briefing_posted()
            assert ok
            assert "weekend" in detail
        finally:
            sentinel._local_now = orig


def test_briefing_before_nine_skip():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 7, 0))
        try:
            sentinel.POST_SUCCESS_FILE.write_text("{}")
            ok, detail = sentinel.check_briefing_posted()
            assert ok
            assert "not due" in detail
        finally:
            sentinel._local_now = orig


# ---------------------------------------------------------------------------
# Heartbeat / queue / failures
# ---------------------------------------------------------------------------

def test_cron_heartbeat_fresh_ok():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.LEDGER_SYNC_LOG.write_text("ok\n")
        ok, detail = sentinel.check_cron_heartbeat()
        assert ok, detail


def test_cron_heartbeat_stale_fails():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.LEDGER_SYNC_LOG.write_text("old\n")
        old = time.time() - 3600
        import os
        os.utime(sentinel.LEDGER_SYNC_LOG, (old, old))
        ok, detail = sentinel.check_cron_heartbeat()
        assert not ok
        assert "STALE" in detail


def test_cron_heartbeat_missing_fails():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ok, detail = sentinel.check_cron_heartbeat()
        assert not ok


def test_deferred_queue_stuck_fails():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.DEFERRED_FILE.write_text(json.dumps([{"text": "x"}]))
        import os
        old = time.time() - 13 * 3600
        os.utime(sentinel.DEFERRED_FILE, (old, old))
        ok, detail = sentinel.check_deferred_queue()
        assert not ok
        assert "STUCK" in detail


def test_deferred_queue_recent_ok():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.DEFERRED_FILE.write_text(json.dumps([{"text": "x"}]))
        ok, detail = sentinel.check_deferred_queue()
        assert ok, detail


def test_recent_failures_detects_fatal():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        (sentinel.LOG_DIR / "morning-briefing-20260610-040001.log").write_text(
            "starting...\nFATAL: preprocessor failed and no stale briefing\n")
        ok, detail = sentinel.check_recent_failures()
        assert not ok
        assert "FATAL" in detail


def test_recent_failures_clean_ok():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        (sentinel.LOG_DIR / "morning-briefing-20260610-040001.log").write_text(
            "starting...\nPosted to Slack.\nDone.\n")
        ok, detail = sentinel.check_recent_failures()
        assert ok, detail


# ---------------------------------------------------------------------------
# Crontab drift
# ---------------------------------------------------------------------------

def test_crontab_drift_detected():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.CRONTAB_MASTER.write_text("0 4 * * 1-5 /x/run.sh a\n")
        orig = sentinel.subprocess.run
        sentinel.subprocess.run = _fake_systemctl(
            {"crontab -l": "0 * * * 1-5 /x/run.sh a\n"})
        try:
            ok, detail = sentinel.check_crontab_drift()
            assert not ok
            assert "DRIFT" in detail
        finally:
            sentinel.subprocess.run = orig


def test_crontab_drift_names_the_lines_and_both_fixes():
    """The alert must say which jobs moved: reinstalling master is the wrong
    fix when the installed crontab holds the intended change."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        # Paths under the checkout the sentinel reads, so the "…/" shortening
        # holds in any worktree.
        root = str(sentinel.REPO_DIR)
        sentinel.CRONTAB_MASTER.write_text(f"0 20 * * 0 {root}/run-agent.sh weekly-audit --model opus\n")
        orig = sentinel.subprocess.run
        sentinel.subprocess.run = _fake_systemctl({"crontab -l": (
            f"0 20 * * 0 {root}/run-agent.sh weekly-audit --model claude-opus-5-5\n"
            f"30 3 * * * TOKEN=s3cret {root}/deploy/zulip/backup.sh\n")})
        try:
            ok, detail = sentinel.check_crontab_drift()
        finally:
            sentinel.subprocess.run = orig
        assert not ok and "2 line(s) only installed, 1 only in master" in detail
        assert "+ installed: 30 3 * * * TOKEN=… …/deploy/zulip/backup.sh" in detail
        assert "- master: 0 20 * * 0 …/run-agent.sh weekly-audit --model opus" in detail
        assert "s3cret" not in detail
        assert "crontab -l >" in detail and "if master is" in detail


def test_deploy_integrity_drift_and_match():
    # Three-legged check: repo HEAD == deployed HEAD == running-instance
    # commit. Uses two throwaway git repos + a fake instance stamp.
    import subprocess as sp
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, dep = tmp / "repo", tmp / "dep"
        for i, d in enumerate((repo, dep)):
            d.mkdir()
            sp.run(["git", "-C", str(d), "init", "-q"], check=True)
            # the deploy tree must be owner-only, as on the live host
            d.chmod(0o755)
            (d / ".git").chmod(0o755)
            # distinct messages: same-second identical empty commits would
            # hash IDENTICALLY and defeat the drift assertion
            sp.run(["git", "-C", str(d), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", f"x{i}"], check=True)
        inst = tmp / "bot-instance.json"
        orig = (sentinel.REPO_DIR, sentinel.DEPLOY_DIR,
                sentinel.BOT_INSTANCE_FILE)
        sentinel.REPO_DIR, sentinel.DEPLOY_DIR = repo, dep
        sentinel.BOT_INSTANCE_FILE = inst
        try:
            # different HEADs (independent repos) -> DRIFT
            ok, msg = sentinel.check_deploy_integrity()
            assert not ok and "DEPLOY DRIFT" in msg
            # same repo for both -> match; no stamp -> soft-pass
            sentinel.DEPLOY_DIR = repo
            ok, msg = sentinel.check_deploy_integrity()
            assert ok and "no bot-instance stamp" in msg
            # stale instance stamp -> FAIL
            head = sp.run(["git", "-C", str(repo), "rev-parse", "--short",
                           "HEAD"], capture_output=True, text=True
                          ).stdout.strip()
            inst.write_text(json.dumps({"commit": "deadbee", "pid": 1}))
            ok, msg = sentinel.check_deploy_integrity()
            assert not ok and "STALE BOT INSTANCE" in msg
            # the deploy hook is waiting for in-flight dispatches before
            # restarting onto this commit -> not stale, no action
            pending = tmp / "deploy-pending.json"
            orig_pending = sentinel.DEPLOY_PENDING_FILE
            sentinel.DEPLOY_PENDING_FILE = pending
            try:
                import time as _t
                pending.write_text(json.dumps({"commit": head, "since": _t.time() - 300}))
                ok, msg = sentinel.check_deploy_integrity()
                assert ok and "restart pending" in msg
                # a stamp for another commit, or one older than the hook's
                # wait, does not excuse the stale instance
                pending.write_text(json.dumps({"commit": "0000000", "since": _t.time()}))
                ok, msg = sentinel.check_deploy_integrity()
                assert not ok and "STALE BOT INSTANCE" in msg
                pending.write_text(json.dumps({"commit": head, "since": _t.time() - 3600}))
                ok, msg = sentinel.check_deploy_integrity()
                assert not ok and "STALE BOT INSTANCE" in msg
            finally:
                sentinel.DEPLOY_PENDING_FILE = orig_pending
            # matching stamp -> OK
            inst.write_text(json.dumps({"commit": head, "pid": 1}))
            ok, msg = sentinel.check_deploy_integrity()
            assert ok and "deploy integrity OK" in msg
        finally:
            (sentinel.REPO_DIR, sentinel.DEPLOY_DIR,
             sentinel.BOT_INSTANCE_FILE) = orig


def test_crontab_drift_ignores_self_managed():
    # A self-managing tool (hft) rewrites its own '# hft-managed' lines; that
    # must NOT register as drift, while a change to a tracked line still would.
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sentinel.CRONTAB_MASTER.write_text(
            "0 4 * * 1-5 /x/run.sh a\n"
            "0 11 * * 1-5 /hft/bin/hft supervise  # hft-managed\n")
        orig = sentinel.subprocess.run
        sentinel.subprocess.run = _fake_systemctl(
            {"crontab -l":
             "0 4 * * 1-5 /x/run.sh a\n"
             "5 8 * * 5 /hft/bin/hft pairs-review  # hft-managed\n"})
        try:
            ok, detail = sentinel.check_crontab_drift()
            assert ok, detail  # hft lines differ but are self-managed
        finally:
            sentinel.subprocess.run = orig


def test_crontab_match_ok():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        content = "# comment\n0 4 * * 1-5 /x/run.sh a\n"
        sentinel.CRONTAB_MASTER.write_text(content)
        orig = sentinel.subprocess.run
        sentinel.subprocess.run = _fake_systemctl(
            {"crontab -l": "# different comment ok\n0 4  * * 1-5  /x/run.sh a\n"})
        try:
            ok, detail = sentinel.check_crontab_drift()
            assert ok, detail  # comments + whitespace normalized away
        finally:
            sentinel.subprocess.run = orig


# ---------------------------------------------------------------------------
# Resident auth check: ea-bot OAuth token lapse
# ---------------------------------------------------------------------------

import types  # noqa: E402


def _resident_enabled(enabled=True):
    """Inject a fake resident_bridge so check_resident_auth runs."""
    fake = types.SimpleNamespace(enabled=lambda: enabled)
    sentinel.sys.modules["resident_bridge"] = fake


def _clear_resident():
    sentinel.sys.modules.pop("resident_bridge", None)


def test_resident_auth_no_refresh_token_fails():
    """The one real unrecoverable state: no refresh token means the
    resident can never re-authenticate. An access-token expiry check
    would instead false-alarm on every idle period."""
    _resident_enabled()
    orig = sentinel._ea_bot_refresh_token_present
    sentinel._ea_bot_refresh_token_present = lambda: False
    try:
        ok, detail = sentinel.check_resident_auth()
        assert not ok
        assert "refresh token" in detail.lower()
        assert "tmux attach -t ea-resident" in detail  # actionable
    finally:
        sentinel._ea_bot_refresh_token_present = orig
        _clear_resident()


def test_resident_auth_present_ok():
    """A present refresh token = healthy, even if the access token is
    expired/near expiry (it auto-refreshes). No more idle false alarms."""
    _resident_enabled()
    orig = sentinel._ea_bot_refresh_token_present
    sentinel._ea_bot_refresh_token_present = lambda: True
    try:
        ok, detail = sentinel.check_resident_auth()
        assert ok, detail
        assert "refresh token" in detail.lower()
    finally:
        sentinel._ea_bot_refresh_token_present = orig
        _clear_resident()


def test_resident_auth_unreadable_skips():
    """Fail-open: a sudo/read hiccup must never page or crash."""
    _resident_enabled()
    orig = sentinel._ea_bot_refresh_token_present
    sentinel._ea_bot_refresh_token_present = lambda: None
    try:
        ok, detail = sentinel.check_resident_auth()
        assert ok
        assert "skipped" in detail
    finally:
        sentinel._ea_bot_refresh_token_present = orig
        _clear_resident()


def test_resident_auth_disabled_skips():
    """No resident mode -> nothing to check, regardless of creds state."""
    _resident_enabled(enabled=False)
    orig = sentinel._ea_bot_refresh_token_present
    sentinel._ea_bot_refresh_token_present = lambda: False
    try:
        ok, detail = sentinel.check_resident_auth()
        assert ok
        assert "disabled" in detail
    finally:
        sentinel._ea_bot_refresh_token_present = orig
        _clear_resident()


# ---------------------------------------------------------------------------
# Alert plumbing: fingerprint anti-spam, fallback, recovery
# ---------------------------------------------------------------------------

class _PostRecorder:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def post_to_slack(self, channel_key, text, blocks=None,
                      priority="normal", _bypass_quiet=False):
        self.calls.append({"channel": channel_key, "text": text,
                           "priority": priority, "bypass": _bypass_quiet})
        return self.results.get(channel_key, True)


def _run_with_failing_check(tmp, recorder, force=False):
    """Run cmd_check with exactly one failing check and a fake notifier."""
    sentinel.sys.modules["slack_notifier"] = recorder
    orig_checks = sentinel.CHECKS
    sentinel.CHECKS = [("fake_fail", lambda: (False, "fake failure"))]
    orig_enabled = sentinel._ea_enabled
    sentinel._ea_enabled = lambda: True
    try:
        return sentinel.cmd_check(as_json=False, force_alert=force)
    finally:
        sentinel.CHECKS = orig_checks
        sentinel._ea_enabled = orig_enabled
        sentinel.sys.modules.pop("slack_notifier", None)


def test_alert_posts_urgent_bypass():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder()
        rc = _run_with_failing_check(Path(td), rec)
        assert rc == 1
        assert len(rec.calls) == 1
        assert rec.calls[0]["channel"] == "ea-assistant"
        assert rec.calls[0]["priority"] == "urgent"
        assert rec.calls[0]["bypass"] is True


def test_antispam_no_realert_within_window():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder()
        _run_with_failing_check(Path(td), rec)
        assert len(rec.calls) == 1
        # Second run, same fingerprint, within 6h -> no new alert
        _run_with_failing_check(Path(td), rec)
        assert len(rec.calls) == 1
        # force overrides
        _run_with_failing_check(Path(td), rec, force=True)
        assert len(rec.calls) == 2


def test_fallback_channel_on_primary_failure():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder(results={"ea-assistant": False, "personal": True})
        orig_sleep = sentinel.time.sleep
        sentinel.time.sleep = lambda s: None
        try:
            _run_with_failing_check(Path(td), rec)
        finally:
            sentinel.time.sleep = orig_sleep
        channels = [c["channel"] for c in rec.calls]
        assert channels.count("ea-assistant") == 2  # initial + retry
        assert channels[-1] == "personal"
        assert "fallback" in rec.calls[-1]["text"]


def test_total_delivery_failure_logs_locally():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder(results={"ea-assistant": False, "personal": False})
        orig_sleep = sentinel.time.sleep
        sentinel.time.sleep = lambda s: None
        try:
            _run_with_failing_check(Path(td), rec)
        finally:
            sentinel.time.sleep = orig_sleep
        log = sentinel.SENTINEL_LOG.read_text()
        assert "alert_delivery_failed" in log


def test_recovery_message_once():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder()
        _run_with_failing_check(Path(td), rec)  # failing -> alert
        assert len(rec.calls) == 1
        # Now all checks pass
        sentinel.sys.modules["slack_notifier"] = rec
        orig_checks = sentinel.CHECKS
        sentinel.CHECKS = [("fake_ok", lambda: (True, "fine"))]
        orig_enabled = sentinel._ea_enabled
        sentinel._ea_enabled = lambda: True
        try:
            rc = sentinel.cmd_check(as_json=False, force_alert=False)
            assert rc == 0
            assert len(rec.calls) == 2
            assert "recovered" in rec.calls[-1]["text"]
            # Second clean run: no more messages
            rc = sentinel.cmd_check(as_json=False, force_alert=False)
            assert rc == 0
            assert len(rec.calls) == 2
        finally:
            sentinel.CHECKS = orig_checks
            sentinel._ea_enabled = orig_enabled
            sentinel.sys.modules.pop("slack_notifier", None)


def test_ea_disabled_skips_everything():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rec = _PostRecorder()
        sentinel.sys.modules["slack_notifier"] = rec
        orig_enabled = sentinel._ea_enabled
        sentinel._ea_enabled = lambda: False
        try:
            rc = sentinel.cmd_check(as_json=False, force_alert=True)
            assert rc == 0
            assert rec.calls == []
        finally:
            sentinel._ea_enabled = orig_enabled
            sentinel.sys.modules.pop("slack_notifier", None)


# ---------------------------------------------------------------------------
# Afternoon catch-up check: timezone-aware and DATE-based. A raw
# server-time window on marker age false-alarms whenever local time
# differs from server time, or the check runs long after the post.
# ---------------------------------------------------------------------------

def test_afternoon_posted_today_ok_is_date_based():
    """Posted today counts even when the post was hours earlier, which an
    age window would fail at an evening check."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 21, 0))  # Wed 9pm
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-10", "ts": "hours-ago"}}))
            ok, detail = sentinel.check_afternoon_posted()
            assert ok, detail
            assert "posted today" in detail
        finally:
            sentinel._local_now = orig


def test_afternoon_before_local_gate_not_due():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 14, 0))  # Wed 2pm
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-09", "ts": "x"}}))
            ok, detail = sentinel.check_afternoon_posted()
            assert ok, detail
            assert "not due yet" in detail
        finally:
            sentinel._local_now = orig


def test_afternoon_weekend_not_expected():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 13, 19, 0))  # Sat
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-09", "ts": "x"}}))
            ok, detail = sentinel.check_afternoon_posted()
            assert ok, detail
            assert "weekend" in detail
        finally:
            sentinel._local_now = orig


def test_afternoon_missed_alarms():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 19, 0))  # Wed 7pm
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-09", "ts": "x"}}))  # stale
            ok, detail = sentinel.check_afternoon_posted()
            assert not ok, detail
            assert "did not post today" in detail
        finally:
            sentinel._local_now = orig


def test_afternoon_deferred_queue_not_alarmed():
    """A post held in the quiet-hours deferral queue is the deferred-queue
    check's concern, not a missed-catch-up alarm."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        orig = sentinel._local_now
        sentinel._local_now = _fake_local_now(datetime(2026, 6, 10, 19, 0))  # Wed 7pm
        try:
            sentinel.POST_SUCCESS_FILE.write_text(json.dumps(
                {"afternoon-catchup": {"date": "2026-06-09", "ts": "x"}}))
            sentinel.DEFERRED_FILE.write_text(json.dumps(
                [{"agent": "afternoon-catchup", "text": "deferred update"}]))
            ok, detail = sentinel.check_afternoon_posted()
            assert ok, detail
            assert "deferral" in detail
        finally:
            sentinel._local_now = orig



def test_deploy_integrity_tolerates_a_docs_only_gap_but_not_a_runtime_one():
    """The deploy hook restarts the bot only for runtime files, so after a
    docs-only commit the running instance is one commit behind by design.
    The sentinel must not page for that, and must still page when a
    runtime file changed in the gap."""
    import subprocess as sp
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        sp.run(["git", "-C", str(repo), "init", "-q"], check=True)
        # the deploy tree must be owner-only, as on the live host
        repo.chmod(0o755)
        (repo / ".git").chmod(0o755)
        git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]

        def commit(path: str, msg: str) -> str:
            f = repo / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(msg)
            sp.run(git + ["add", path], check=True)
            sp.run(git + ["commit", "-q", "-m", msg], check=True)
            return sp.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

        base = commit("ea-chat-bot.py", "runtime v1")
        inst = Path(td) / "bot-instance.json"
        inst.write_text(json.dumps({"commit": base, "pid": 1}))
        orig = (sentinel.REPO_DIR, sentinel.DEPLOY_DIR, sentinel.BOT_INSTANCE_FILE)
        sentinel.REPO_DIR = sentinel.DEPLOY_DIR = repo
        sentinel.BOT_INSTANCE_FILE = inst
        try:
            commit("docs/ARCHITECTURE.md", "docs only")
            commit("tests/test_x.py", "a test")
            commit("skills/foo.md", "a skill")
            ok, msg = sentinel.check_deploy_integrity()
            assert ok and "non-runtime files only" in msg, msg
            commit("preprocessor/sentinel.py", "runtime v2")
            ok, msg = sentinel.check_deploy_integrity()
            assert not ok and "STALE BOT INSTANCE" in msg, msg
            # an instance commit the deployed repo does not know still pages
            inst.write_text(json.dumps({"commit": "deadbee", "pid": 1}))
            ok, msg = sentinel.check_deploy_integrity()
            assert not ok and "STALE BOT INSTANCE" in msg, msg
        finally:
            (sentinel.REPO_DIR, sentinel.DEPLOY_DIR, sentinel.BOT_INSTANCE_FILE) = orig


def test_breaker_detail_names_the_service_and_error_kind_only():
    """The detail is fingerprinted: a failure count or the full error text
    would re-alert on every probe of a dead service."""
    import retry_policy
    orig = retry_policy.open_breakers
    retry_policy.open_breakers = lambda: [
        {"service": "remote-llm", "failures": 15, "opens": 4,
         "last_error": "HTTP 502: <html><title>502 Proxy Error</title>..."},
        {"service": "remote-llm-tune", "failures": 3, "last_error": "timed out after 330s"}]
    try:
        ok, detail = sentinel.check_circuit_breakers()
        assert not ok
        assert detail.startswith("circuit breaker OPEN: remote-llm (HTTP 502), "
                                 "remote-llm-tune (timed out after 330s) ")
        assert "15" not in detail and "<html>" not in detail
        retry_policy.open_breakers = lambda: []
        assert sentinel.check_circuit_breakers() == (True, "no open circuit breakers")
    finally:
        retry_policy.open_breakers = orig


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
