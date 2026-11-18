#!/usr/bin/env python3
"""The usage tracker reports the account the EA runs on.

What must hold: from any process that is not the resident, get_usage()
runs the tracker as the resident and returns what it prints; the
resident's own process (or an explicit token) reads its own credentials;
a failed delegation is an error, not a silent fallback to the caller's
account; the report names the account; the time helpers are sane.

The footer and the `usage` report come from the plan's windows a Claude
run reported (rate_limits.py) before the usage endpoint: a fresh reading
makes no API call; a stale one shows its age; a malformed event saves
nothing and raises nothing; the endpoint is the last fallback, asked at
most once per gap from the footer path; only the report starts a refresh
run.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

# No test may touch the real owner-private dir (conftest does the same
# under pytest; the wall runs this file as a script).
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
if not os.environ.get("EA_PRIVATE_DIR"):
    os.environ["EA_PRIVATE_DIR"] = tempfile.mkdtemp(prefix="usage-private-")

import claude_usage as cu  # noqa: E402
import rate_limits as rl  # noqa: E402

# The shape Claude Code 2.1 prints, with made-up numbers and one extra window.
EVENT = {"type": "rate_limit_event",
         "rate_limit_info": {"status": "allowed", "resetsAt": 1795029000, "rateLimitType": "five_hour",
                             "overageStatus": "rejected", "isUsingOverage": False,
                             "unifiedWindows": {
                                 "five_hour": {"utilization": 0.12, "resetsAt": 1795029000,
                                               "status": "allowed"},
                                 "seven_day": {"utilization": 0.37, "resetsAt": 1795338000,
                                               "status": "allowed"},
                                 "seven_day_opus": {"utilization": 0.05, "resetsAt": 1795338000,
                                                    "status": "allowed"}}},
         "uuid": "00000000-0000-0000-0000-000000000000", "session_id": "s-1"}


def _event(session=0.12, weekly=0.37, *, hours=3, days=4, status="allowed"):
    """A rate_limit_event whose windows reset `hours` and `days` from now."""
    now = datetime.now(timezone.utc)
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "unifiedWindows": {
        "five_hour": {"utilization": session, "resetsAt": (now + timedelta(hours=hours)).timestamp(),
                      "status": status},
        "seven_day": {"utilization": weekly, "resetsAt": (now + timedelta(days=days)).timestamp(),
                      "status": "allowed"}}}}


def _snapshot_file(d: Path, *, age_s=0, **kw) -> Path:
    path = d / rl.FILE_NAME
    taken = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    assert rl.save(rl.parse_event(_event(**kw)), source="chat:mono", path=path, now=taken)
    return path


def _no_call(*a, **k):
    raise AssertionError("no API call is expected here")


def _endpoint_ok():
    now = datetime.now(timezone.utc)
    return {"session_percent_used": 40.0, "session_reset": (now + timedelta(hours=2)).isoformat(),
            "daily_percent_used": 61.0, "daily_reset": (now + timedelta(days=3)).isoformat(),
            "error": None, "account": "kit.lowell@example.com", "plan": "max"}


def test_should_delegate_only_from_another_user_with_a_resident_present():
    assert cu.should_delegate("resident", "crh", Path("/home/ea-bot"))
    assert not cu.should_delegate("resident", cu.RESIDENT_USER, Path("/home/ea-bot"))
    assert not cu.should_delegate("resident", "crh", None)
    assert not cu.should_delegate("self", "crh", Path("/home/ea-bot"))


def test_delegation_returns_the_residents_payload_and_fails_closed():
    real = cu.subprocess.run
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"session_percent_used": 2.0, "session_reset": "2026-09-12T14:39:59+00:00",
             "daily_percent_used": 11.0, "daily_reset": "2026-09-17T11:59:59+00:00",
             "error": None, "account": "jo.ambrose4@example.com", "plan": "max"}))
    cu.subprocess.run = fake_run
    try:
        out = cu._usage_as_resident()
    finally:
        cu.subprocess.run = real
    assert out["account"] == "jo.ambrose4@example.com" and out["session_percent_used"] == 2.0
    cmd = seen[0]
    assert cmd[:4] == ["sudo", "-n", "-u", cu.RESIDENT_USER] and "--self" in cmd and "--json" in cmd
    assert cmd[5].endswith("claude_usage.py")

    def failing_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stderr="sudo: a password is required", stdout="")
    cu.subprocess.run = failing_run
    try:
        out = cu._usage_as_resident()
    finally:
        cu.subprocess.run = real
    assert out["error"] and cu.RESIDENT_USER in out["error"]
    assert "session_percent_used" not in out, "never fall back to the caller's account"


def test_get_usage_with_a_token_reads_self_and_names_the_account():
    real_get = cu.requests.get
    real_info = cu.account_info
    cu.requests.get = lambda url, **kw: types.SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"five_hour": {"utilization": 11.0, "resets_at": "2026-09-12T19:19:59+00:00"},
                      "seven_day": {"utilization": 52.0, "resets_at": "2026-09-13T08:59:59+00:00"}})
    cu.account_info = lambda: {"account": "wren.fairbanks84@example.edu", "plan": "team"}
    try:
        u = cu.get_usage(token="tok")
    finally:
        cu.requests.get = real_get
        cu.account_info = real_info
    assert u["session_percent_used"] == 11.0 and u["daily_percent_used"] == 52.0
    assert u["account"].endswith("example.edu") and u["plan"] == "team" and u["error"] is None
    report = cu.format_usage_report(u)
    assert "*Account:* wren.fairbanks84@example.edu (team)" in report
    assert "Session (5h rolling):* 11% used" in report and "Weekly (7d rolling):* 52% used" in report


def test_time_helpers():
    now = datetime.now(timezone.utc)
    assert cu._relative_time((now + timedelta(hours=4, minutes=53, seconds=30)).isoformat()) == "4h 53m"
    assert cu._relative_time((now + timedelta(days=2, hours=3, seconds=30)).isoformat()) == "2d 3h"
    assert cu._relative_time((now - timedelta(minutes=1)).isoformat()) == "now"
    assert cu._relative_time("garbage") == "unknown"
    today = cu._absolute_time((now + timedelta(minutes=5)).isoformat())
    assert today and " " in today and not today.split()[0].isalpha()
    later = cu._absolute_time((now + timedelta(days=3)).isoformat())
    assert later.split()[0] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    footer = cu.format_usage_footer({"session_percent_used": 2.0, "session_reset": (now + timedelta(hours=1, seconds=30)).isoformat(),
                                     "daily_percent_used": 11.0, "daily_reset": (now + timedelta(days=5, seconds=30)).isoformat()}, 12.0)
    assert "2% session (resets in 1h 0m)" in footer and "11% weekly (resets in 5d)" in footer
    assert cu.format_usage_footer({"error": "x"}, 3.0) == "\n\n---\n_EA: 3s_"
    assert cu.format_usage_footer({"error": "x"}, 3.0, label="EA resident") == \
        "\n\n---\n_EA resident: 3s_"
    both = cu.format_usage_footer({"session_percent_used": 9.0, "daily_percent_used": 2.0},
                                  20.0, label="EA resident")
    assert both.startswith("\n\n---\n_EA resident: 20s · 9% session") and "2% weekly" in both


def test_parse_event_reads_the_real_shape():
    r = rl.parse_event(EVENT)
    assert r["status"] == "allowed"
    w = r["windows"]
    assert w["five_hour"]["percent_used"] == 12.0 and w["seven_day"]["percent_used"] == 37.0
    assert w["seven_day_opus"]["percent_used"] == 5.0
    assert w["five_hour"]["resets_at"] == datetime.fromtimestamp(1795029000, timezone.utc).isoformat()
    assert w["seven_day"]["status"] == "allowed"


def test_a_malformed_event_gives_no_reading_and_never_raises():
    bad = [None, [], "x", {"type": "result"}, {"type": "rate_limit_event"},
           {"type": "rate_limit_event", "rate_limit_info": "full"},
           {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": ["five_hour"]}},
           {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
               "five_hour": {"utilization": "12%"}, "seven_day": {"utilization": True},
               "seven_day_opus": {"utilization": float("nan")}, "x": {"utilization": -0.1},
               "five*hour": {"utilization": 0.5}, "y": {"utilization": 99.0}, "z": None}}}]
    for ev in bad:
        assert rl.parse_event(ev) is None, ev
        assert rl.note_stream_event(ev, path=Path(tempfile.mkdtemp()) / rl.FILE_NAME) is False
    # A window with no reset time, or one in milliseconds or ISO, still reads.
    odd = rl.parse_event({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.25, "resetsAt": 1795338000000},
        "seven_day_sonnet": {"utilization": 0, "resetsAt": "2026-12-01T10:00:00Z", "status": "Bad Status!"}}}})
    assert odd["windows"]["five_hour"] == {"percent_used": 50.0, "resets_at": "", "status": ""}
    assert odd["windows"]["seven_day"]["resets_at"].startswith(
        datetime.fromtimestamp(1795338000, timezone.utc).isoformat()[:16])
    assert odd["windows"]["seven_day_sonnet"]["resets_at"] == "2026-12-01T10:00:00+00:00"
    assert odd["windows"]["seven_day_sonnet"]["status"] == ""
    # Only the binding window at the top level, as an older CLI may print.
    top = rl.parse_event({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.81,
        "resetsAt": 1795338000}})
    assert list(top["windows"]) == ["seven_day"] and top["windows"]["seven_day"]["percent_used"] == 81.0
    assert top["status"] == "allowed_warning"


def test_a_reading_is_saved_and_loaded_by_account():
    d = Path(tempfile.mkdtemp())
    path = d / rl.FILE_NAME
    taken = datetime.now(timezone.utc) - timedelta(minutes=3)
    assert rl.note_stream_event(EVENT, source="cron:morning", path=path) is True
    assert rl.save(rl.parse_event(EVENT), source="x", account="owner", path=path, now=taken)
    assert not rl.save(rl.parse_event(EVENT), account="someone", path=path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    snap = rl.load(path=path)
    assert snap["source"] == "cron:morning" and snap["windows"]["seven_day"]["percent_used"] == 37.0
    assert rl.load(account="owner", path=path)["ts"] == taken
    assert 170 < rl.age_seconds(rl.load(account="owner", path=path)) < 400
    assert rl.load(path=d / "missing.json") is None and rl.age_seconds(None) is None
    # A file in another shape is no reading, and a save writes over it.
    path.write_text('{"accounts": {"resident": {"ts": "yesterday", "windows": {}}}}')
    assert rl.load(path=path) is None
    path.write_text("not json")
    assert rl.load(path=path) is None
    assert rl.note_stream_event(EVENT, path=path) and rl.load(path=path) is not None
    # A window that went bad on disk is left out on the way in.
    data = json.loads(path.read_text())
    data["accounts"]["resident"]["windows"]["five_hour"]["percent_used"] = "lots"
    data["accounts"]["resident"]["windows"]["bad name!"] = {"percent_used": 3}
    path.write_text(json.dumps(data))
    assert set(rl.load(path=path)["windows"]) == {"seven_day", "seven_day_opus"}


def test_only_an_owner_process_saves_to_the_default_file():
    real = rl.owner_trust.is_owner_process
    rl.owner_trust.is_owner_process = lambda: False
    try:
        assert rl.save(rl.parse_event(EVENT)) is False
        assert rl.note_stream_event(EVENT) is False
    finally:
        rl.owner_trust.is_owner_process = real
    assert not (Path(os.environ["EA_PRIVATE_DIR"]) / rl.FILE_NAME).exists()


def test_the_footer_uses_a_fresh_reading_with_no_api_call():
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=20)
    u = cu.footer_usage(fetch=_no_call, path=path, memo={})
    assert u["session_percent_used"] == 12.0 and u["daily_percent_used"] == 37.0
    assert u["source"] == "run" and u["age_s"] < 60
    footer = cu.format_usage_footer(u, 45.0, label="EA resident")
    assert footer.startswith("\n\n---\n_EA resident: 45s · 12% session (resets in 2h 59m)")
    assert "37% weekly (resets in 3d 23h)" in footer and "checked" not in footer
    # Older than SHOW_AGE_AFTER_S but still fresh: no call, the age shows.
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=15 * 60 + 5)
    u = cu.footer_usage(fetch=_no_call, path=path, memo={})
    assert "· checked 15m ago_" in cu.format_usage_footer(u, 3.0)


def test_the_footer_falls_back_to_the_endpoint_once_per_gap():
    d = Path(tempfile.mkdtemp())
    path = d / rl.FILE_NAME
    calls = []

    def fetch():
        calls.append(1)
        return _endpoint_ok()
    memo = {}
    u = cu.footer_usage(fetch=fetch, path=path, memo=memo)
    assert u["session_percent_used"] == 40.0 and u["source"] == "endpoint" and len(calls) == 1
    # The answer is saved: the next footer reads it, with no call.
    assert rl.load(path=path)["source"] == "endpoint"
    assert cu.footer_usage(fetch=_no_call, path=path, memo={})["daily_percent_used"] == 61.0

    # An endpoint that refuses is asked once per ENDPOINT_RETRY_S, not per reply.
    calls.clear()
    memo = {}

    def refusing():
        calls.append(1)
        return {"error": "429 Too Many Requests"}
    empty = d / "none.json"
    assert cu.footer_usage(fetch=refusing, path=empty, memo=memo) == {"error": "429 Too Many Requests"}
    assert cu.footer_usage(fetch=refusing, path=empty, memo=memo)["error"]
    assert len(calls) == 1
    assert cu.format_usage_footer({"error": "x"}, 7.0) == "\n\n---\n_EA: 7s_"

    # A stale reading and a refusing endpoint: the stale reading, with its age.
    stale = _snapshot_file(Path(tempfile.mkdtemp()), age_s=2 * 3600 + 60)
    u = cu.footer_usage(fetch=refusing, path=stale, memo={})
    assert u["session_percent_used"] == 12.0 and u["age_s"] > 7200
    assert cu.format_usage_footer(u, 9.0).endswith("· checked 2h 1m ago_")
    # A fetch that raises is the same as a refusal.
    assert cu.footer_usage(fetch=_no_call, path=d / "none2.json", memo={})["error"]


def test_a_window_that_reset_since_the_reading_is_not_shown_as_current():
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=40, hours=-1)
    u = cu.footer_usage(fetch=_no_call, path=path, memo={})
    assert u["session_percent_used"] is None and u["ended"] == ["five_hour"]
    footer = cu.format_usage_footer(u, 5.0)
    assert "session" not in footer and "37% weekly" in footer
    report = cu.format_usage_report(u)
    assert "*Session (5h rolling):* it reset at" in report and "No newer reading yet." in report


def test_the_report_order_is_reading_then_refresh_then_endpoint():
    lookup = lambda: {"account": "kit.lowell@example.com", "plan": "max"}  # noqa: E731
    # 1. A fresh reading: no refresh run, no API call.
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=12 * 60)
    u = cu.report_usage(refresh=_no_call, fetch=_no_call, account_lookup=lookup, path=path, memo={})
    report = cu.format_usage_report(u)
    assert "*Account:* kit.lowell@example.com (max)" in report
    assert "*Session (5h rolling):* 12% used" in report and "*Weekly (7d rolling):* 37% used" in report
    assert "*Weekly, Opus models:*" not in report
    assert report.endswith("_From the EA's last Claude run, 12m ago_")

    # 2. A stale reading: the refresh run saves a new one, and no API call.
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=3 * 3600)
    runs = []

    def refresh():
        runs.append(1)
        rl.note_stream_event(EVENT, source="chat:usage", path=path)
    memo = {}
    u = cu.report_usage(refresh=refresh, fetch=_no_call, account_lookup=lambda: {}, path=path, memo=memo)
    assert runs == [1] and u["age_s"] < 60 and u["session_percent_used"] == 12.0
    report = cu.format_usage_report(u)
    assert "*Account:* the Claude login the EA runs on" in report
    assert "*Weekly, Opus models:* 5% used" in report
    assert report.endswith("_From the EA's last Claude run, just now_")

    # 3. The refresh saves nothing: the endpoint. A second ask in the gap
    #    starts no second run.
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=3 * 3600)
    memo = {}
    u = cu.report_usage(refresh=lambda: runs.append(2), fetch=_endpoint_ok, account_lookup=_no_call,
                        path=path, memo=memo)
    assert runs[-1] == 2 and u["source"] == "endpoint" and u["daily_percent_used"] == 61.0
    assert cu.format_usage_report(u).endswith("_Checked just now_")
    cu.report_usage(refresh=lambda: runs.append(3), fetch=_endpoint_ok, path=path, memo=memo)
    assert 3 not in runs

    # 4. The endpoint refuses too: the stale reading, with its age.
    path = _snapshot_file(Path(tempfile.mkdtemp()), age_s=3 * 3600 + 30)
    u = cu.report_usage(refresh=lambda: None, fetch=lambda: {"error": "403 scope"},
                        account_lookup=lambda: {}, path=path, memo={})
    assert u["session_percent_used"] == 12.0
    assert cu.format_usage_report(u).endswith("_From the EA's last Claude run, 3h ago_")

    # 5. Nothing at all: one plain error.
    u = cu.report_usage(refresh=None, fetch=lambda: {"error": "403 scope"},
                        path=Path(tempfile.mkdtemp()) / "none.json", memo={})
    assert u == {"error": "403 scope"}
    assert cu.format_usage_report(u).startswith(":warning: I could not read the Claude usage.")


def test_a_reading_at_the_limit_says_so():
    path = _snapshot_file(Path(tempfile.mkdtemp()), status="rejected")
    u = cu.report_usage(fetch=_no_call, account_lookup=lambda: {}, path=path, memo={})
    assert u["limited"] is True
    assert ":warning: The plan's limit is reached." in cu.format_usage_report(u)


def test_format_age():
    assert cu.format_age(5) == "just now"
    assert cu.format_age(12 * 60 + 5) == "12m ago"
    assert cu.format_age(2 * 3600 + 5 * 60) == "2h 5m ago"
    assert cu.format_age(9 * 3600 + 5 * 60) == "9h ago"
    assert cu.format_age(3 * 86400) == "3d ago"
    assert cu.format_age(None) == "a while ago"


def test_resident_account_runs_as_the_resident_with_no_api_call():
    real_run, real_home, real_user = cu.subprocess.run, cu._resident_home, cu._current_user
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"account": "kit.lowell@example.com", "plan": "team"}))
    cu.subprocess.run = fake_run
    cu._resident_home = lambda: Path("/nonexistent-resident-home")
    cu._current_user = lambda: "someone-else"
    try:
        assert cu.resident_account() == {"account": "kit.lowell@example.com", "plan": "team"}
        cu.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=2, stderr="usage", stdout="")
        assert cu.resident_account() == {"account": "", "plan": ""}
    finally:
        cu.subprocess.run, cu._resident_home, cu._current_user = real_run, real_home, real_user
    cmd = seen[0]
    assert cmd[:4] == ["sudo", "-n", "-u", cu.RESIDENT_USER]
    assert cmd[-3:] == ["--self", "--account", "--json"]


def test_the_cron_pipe_saves_the_reading_and_passes_the_reply_through():
    home = Path(tempfile.mkdtemp())
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        import importlib
        lcc = importlib.import_module("log_claude_cost")
    finally:
        if real_home is not None:
            os.environ["HOME"] = real_home
    saved = []
    real_note, real_argv, real_in, real_out = lcc.rate_limits.note_stream_event, sys.argv, sys.stdin, sys.stdout
    lcc.rate_limits.note_stream_event = lambda ev, **kw: saved.append((ev["type"], kw)) or True
    lcc.LOG_PATH = home / "cost.log"
    lcc.LOG_DIR = home
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init"}, EVENT, {"type": "rate_limit_event"},
        {"type": "result", "subtype": "success", "result": "the briefing", "num_turns": 1}))
    sys.argv = ["log_claude_cost.py", "--agent", "morning-briefing"]
    sys.stdin, sys.stdout = io.StringIO(stream + "\n"), io.StringIO()
    try:
        rc = lcc.main()
        out = sys.stdout.getvalue()
    finally:
        lcc.rate_limits.note_stream_event = real_note
        sys.argv, sys.stdin, sys.stdout = real_argv, real_in, real_out
    assert rc == 0 and out == "the briefing\n"
    assert saved == [("rate_limit_event", {"source": "cron:morning-briefing"}),
                     ("rate_limit_event", {"source": "cron:morning-briefing"})]
    # The real note on a malformed event saves nothing and raises nothing.
    assert real_note({"type": "rate_limit_event", "rate_limit_info": None}) is False


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
