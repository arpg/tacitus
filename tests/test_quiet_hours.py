#!/usr/bin/env python3
"""Tests for preprocessor/quiet_hours.py (timezone-aware quiet hours + travel mode).

A proactive Slack post that would land during the user's local night is
held and flushed as a morning digest, without dropping anything. The
server runs Mountain Time; the user travels.
"""

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# Owner-side file access goes through resident_io, which runs a resident
# child in an owner process; a sandboxed EA_DIR makes this process a
# resident one, so the calls act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-quiethours-")

import ea_config       # noqa: E402
import quiet_hours as qh  # noqa: E402

UTC = ZoneInfo("UTC")


def _setup(tmp: Path, *, quiet=None, travel=None, home_tz="America/Denver"):
    """Point quiet_hours at temp config/prefs/queue and return paths."""
    cfg = {}
    if quiet is not None:
        cfg["quiet_hours"] = quiet
    if travel is not None:
        cfg["travel_mode"] = travel
    cfg_file = tmp / "ea-config.json"
    cfg_file.write_text(json.dumps(cfg))
    prefs_file = tmp / "scheduling-preferences.json"
    prefs_file.write_text(json.dumps({"timezone": home_tz}))
    queue_file = tmp / "deferred-posts.json"

    ea_config.CONFIG_FILE = cfg_file
    qh.PREFS_FILE = prefs_file
    qh.QUEUE_FILE = queue_file
    # No state-file override in tests -> _get_travel() falls back to the
    # travel_mode block we wrote into ea-config.json above.
    qh.TRAVEL_STATE_FILE = tmp / "travel-mode.json"
    qh.LAST_RUN_FILE = tmp / "agent-last-run.json"
    qh.HOUR_OVERRIDE_FILE = tmp / "agent-hour-overrides.json"
    return queue_file


_QH = {"enabled": True, "start": "22:00", "end": "07:30", "allow_urgent": False}
_TRAVEL_VIENNA = {"enabled": True, "timezone": "Europe/Vienna", "until": "2099-12-31"}


# --- active timezone ---

def test_active_tz_home_when_no_travel():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False}, home_tz="America/Denver")
        assert qh.active_timezone() == "America/Denver"


def test_active_tz_travel_when_enabled():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        assert qh.active_timezone() == "Europe/Vienna"


def test_active_tz_reverts_after_until():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH,
               travel={"enabled": True, "timezone": "Europe/Vienna", "until": "2020-01-01"})
        assert qh.active_timezone() == "America/Denver"  # trip over -> home


# --- in_quiet_hours (overnight wrap 22:00-07:30) ---

def test_quiet_window_overnight_wrap_vienna():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        # Vienna is UTC+2 in June. 23:00 Vienna = 21:00 UTC.
        assert qh.in_quiet_hours(datetime(2026, 6, 2, 21, 0, tzinfo=UTC)) is True   # 23:00
        assert qh.in_quiet_hours(datetime(2026, 6, 3, 1, 0, tzinfo=UTC)) is True    # 03:00
        assert qh.in_quiet_hours(datetime(2026, 6, 2, 12, 0, tzinfo=UTC)) is False  # 14:00
        assert qh.in_quiet_hours(datetime(2026, 6, 2, 19, 59, tzinfo=UTC)) is False # 21:59
        assert qh.in_quiet_hours(datetime(2026, 6, 2, 20, 0, tzinfo=UTC)) is True   # 22:00 start


def test_quiet_off_at_home_by_default():
    # only_when_traveling (default): at home the EA posts immediately even at
    # 3am, so the 4am briefing fires at 4am (pre-trip behavior).
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td),
               quiet={"enabled": True, "start": "22:00", "end": "07:30",
                      "allow_urgent": False, "only_when_traveling": True},
               travel={"enabled": False}, home_tz="America/Denver")
        night = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)  # 03:00 MDT
        assert qh.in_quiet_hours(night) is False
        assert qh.should_defer("normal", now=night) is False


def test_quiet_on_at_home_when_only_when_traveling_false():
    # Opt back in to holding overnight at home.
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td),
               quiet={"enabled": True, "start": "22:00", "end": "07:30",
                      "allow_urgent": False, "only_when_traveling": False},
               travel={"enabled": False}, home_tz="America/Denver")
        assert qh.in_quiet_hours(datetime(2026, 6, 3, 9, 0, tzinfo=UTC)) is True


def test_quiet_still_engages_while_traveling():
    # only_when_traveling=True must still hold during travel.
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td),
               quiet={"enabled": True, "start": "22:00", "end": "07:30",
                      "allow_urgent": False, "only_when_traveling": True},
               travel=_TRAVEL_VIENNA)
        assert qh.in_quiet_hours(datetime(2026, 6, 3, 1, 0, tzinfo=UTC)) is True  # 03:00 Vienna


def test_quiet_disabled_never_quiet():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet={"enabled": False, "start": "22:00", "end": "07:30"},
               travel=_TRAVEL_VIENNA)
        assert qh.in_quiet_hours(datetime(2026, 6, 3, 1, 0, tzinfo=UTC)) is False


# --- should_defer ---

def test_should_defer_normal_in_quiet():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        night = datetime(2026, 6, 3, 1, 0, tzinfo=UTC)
        assert qh.should_defer("normal", now=night) is True
        # allow_urgent False -> urgent also holds (user chose "hold everything")
        assert qh.should_defer("urgent", now=night) is True


def test_urgent_breaks_through_when_allowed():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td),
               quiet={"enabled": True, "start": "22:00", "end": "07:30", "allow_urgent": True},
               travel=_TRAVEL_VIENNA)
        night = datetime(2026, 6, 3, 1, 0, tzinfo=UTC)
        assert qh.should_defer("urgent", now=night) is False
        assert qh.should_defer("normal", now=night) is True


def test_no_defer_during_day():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        day = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)  # 14:00 Vienna
        assert qh.should_defer("normal", now=day) is False


# --- defer + flush roundtrip ---

def test_defer_then_flush_force_delivers_and_clears():
    with tempfile.TemporaryDirectory() as td:
        qfile = _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        posted = []
        orig = qh.slack_notifier.post_to_slack
        qh.slack_notifier.post_to_slack = lambda ch, text, **kw: posted.append((ch, text)) or True
        try:
            qh.defer("ea-assistant", "first normal", "normal")
            qh.defer("ea-assistant", "second urgent", "urgent")
            assert len(json.loads(qfile.read_text())) == 2
            n = qh.flush_if_clear(force=True)
            assert n == 2, n
            assert json.loads(qfile.read_text()) == []   # cleared
            assert len(posted) == 1                       # one digest
            body = posted[0][1]
            # urgent sorts before normal in the digest
            assert body.index("second urgent") < body.index("first normal")
            assert "Held while you were offline (2 items)" in body
        finally:
            qh.slack_notifier.post_to_slack = orig


def test_flush_holds_during_quiet_hours():
    with tempfile.TemporaryDirectory() as td:
        qfile = _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        posted = []
        orig = qh.slack_notifier.post_to_slack
        qh.slack_notifier.post_to_slack = lambda *a, **k: posted.append(a) or True
        # Force "now" into quiet hours by setting in_quiet_hours via a stub.
        orig_iqh = qh.in_quiet_hours
        qh.in_quiet_hours = lambda now=None: True
        try:
            qh.defer("ea-assistant", "held item", "normal")
            n = qh.flush_if_clear()       # still in quiet hours -> hold
            assert n == 0
            assert len(json.loads(qfile.read_text())) == 1
            assert posted == []
        finally:
            qh.slack_notifier.post_to_slack = orig
            qh.in_quiet_hours = orig_iqh


def test_local_hour_in_active_tz():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        # 21:00 UTC = 23:00 Vienna (UTC+2 in June)
        assert qh.local_hour(datetime(2026, 6, 2, 21, 0, tzinfo=UTC)) == 23
        assert qh.local_hour(datetime(2026, 6, 3, 2, 0, tzinfo=UTC)) == 4  # 04:00 Vienna


def test_due_local_fires_once_at_4am_local():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        four_am = datetime(2026, 6, 3, 2, 0, tzinfo=UTC)   # 04:00 Vienna
        five_am = datetime(2026, 6, 3, 3, 0, tzinfo=UTC)   # 05:00 Vienna
        next_day = datetime(2026, 6, 4, 2, 0, tzinfo=UTC)  # 04:00 Vienna, next day
        assert qh.due_local("morning-briefing", 4, now=four_am) is True   # fires + marks
        assert qh.due_local("morning-briefing", 4, now=four_am) is False  # already ran today
        assert qh.due_local("morning-briefing", 4, now=five_am) is False  # wrong hour
        assert qh.due_local("morning-briefing", 4, now=next_day) is True  # new local day


# --- one-day hour overrides -------------------------------------------
# "Just for tomorrow, run the briefing at 6am MT." The override names the
# local date it applies to, so it expires without a revert step.

def test_hour_override_moves_the_run_for_that_date_only():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False},
               home_tz="America/Denver")
        qh.set_hour_override("morning-briefing", "2026-08-27", 6)
        four_am = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)   # 04:00 MDT
        six_am = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)    # 06:00 MDT
        # Moved, not added: the default hour no longer fires on that date.
        assert qh.due_local("morning-briefing", 4, now=four_am) is False
        assert qh.due_local("morning-briefing", 4, now=six_am) is True
        # ... and only once.
        assert qh.due_local("morning-briefing", 4, now=six_am) is False


def test_hour_override_expires_by_itself_the_next_day():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False},
               home_tz="America/Denver")
        qh.set_hour_override("morning-briefing", "2026-08-27", 6)
        # the next day: back to 4am, with nobody having removed anything.
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC)) is False
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 8, 28, 10, 0, tzinfo=UTC)) is True


def test_hour_override_of_midnight_is_a_real_value():
    """0 is falsy; an `or default` would silently ignore a midnight run."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False},
               home_tz="America/Denver")
        qh.set_hour_override("morning-briefing", "2026-08-27", 0)
        assert qh.hour_override(
            "morning-briefing",
            now=datetime(2026, 8, 27, 6, 0, tzinfo=UTC)) == 0
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 8, 27, 6, 0, tzinfo=UTC)) is True


def test_override_is_per_agent():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False},
               home_tz="America/Denver")
        qh.set_hour_override("morning-briefing", "2026-08-27", 6)
        six_am = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        assert qh.due_local("afternoon-catchup", 15, now=six_am) is False


def test_a_malformed_override_does_not_stop_the_briefing():
    """A bad override must fail to MOVE the run, never to cancel it."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False},
               home_tz="America/Denver")
        for bad in ('{"morning-briefing": {"date": "2026-08-27"}}',
                    '{"morning-briefing": {"date": "2026-08-27", "hour": 99}}',
                    '{"morning-briefing": {"date": "2026-08-27", "hour": "six"}}',
                    '{"morning-briefing": "6am"}',
                    'not json at all'):
            qh.HOUR_OVERRIDE_FILE.write_text(bad)
            four_am = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
            assert qh.hour_override("morning-briefing", now=four_am) is None, bad
            assert qh.due_local("morning-briefing", 4, now=four_am) is True, bad
            qh.LAST_RUN_FILE.unlink()


def test_set_hour_override_rejects_junk():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False})
        for args in (("morning-briefing", "tomorrow", 6),
                     ("morning-briefing", "2026-08-27", 24),
                     ("morning-briefing", "2026-08-27", -1)):
            try:
                qh.set_hour_override(*args)
            except ValueError:
                continue
            raise AssertionError(f"accepted {args}")


def test_clear_hour_override():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False})
        qh.set_hour_override("morning-briefing", "2026-08-27", 6)
        assert qh.clear_hour_override("morning-briefing") == {}
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 8, 27, 10, 0, tzinfo=UTC)) is True


def test_status_default_hour_matches_run_agent_sh():
    """quiet_hours mirrors the briefing's normal hour so `status` can report
    it. run-agent.sh passes the real one; if that line changes and this
    mirror does not, status starts lying."""
    import re
    sh = (HERE.parent / "run-agent.sh").read_text()
    m = re.search(r"local-due --hour (\d+) --agent morning-briefing", sh)
    assert m, "run-agent.sh no longer calls local-due the documented way"
    assert qh.DEFAULT_AGENT_HOURS["morning-briefing"] == int(m.group(1))


def test_due_local_tracks_active_timezone():
    # At home (MT), 4am-local is a different UTC instant than in Vienna.
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False}, home_tz="America/Denver")
        # 10:00 UTC = 04:00 MDT (UTC-6 in June)
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 6, 3, 10, 0, tzinfo=UTC)) is True
        # 02:00 UTC = 20:00 MDT previous day -> not 4am MT
        assert qh.due_local("morning-briefing", 4,
                            now=datetime(2026, 6, 4, 2, 0, tzinfo=UTC)) is False


def test_flush_empty_queue_is_noop():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        assert qh.flush_if_clear(force=True) == 0


def test_quiet_end_is_the_next_end_of_the_window():
    """The web chat holds a reply's push until the window ends: the same
    morning after midnight, the next morning before it, None outside it."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel=_TRAVEL_VIENNA)
        vienna = ZoneInfo("Europe/Vienna")
        # 21:00 UTC = 23:00 in Vienna (UTC+2 in June): the next morning
        assert qh.quiet_end(datetime(2026, 6, 3, 21, 0, tzinfo=UTC)) == \
            datetime(2026, 6, 4, 7, 30, tzinfo=vienna)
        # 01:00 UTC = 03:00 in Vienna: the same morning
        assert qh.quiet_end(datetime(2026, 6, 4, 1, 0, tzinfo=UTC)) == \
            datetime(2026, 6, 4, 7, 30, tzinfo=vienna)
        # midday: nothing waits
        assert qh.quiet_end(datetime(2026, 6, 4, 10, 0, tzinfo=UTC)) is None


def test_quiet_end_is_none_at_home_by_default():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td), quiet=_QH, travel={"enabled": False}, home_tz="America/Denver")
        # 05:00 UTC = 23:00 MDT, at home: quiet hours hold only while travelling
        assert qh.quiet_end(datetime(2026, 6, 3, 5, 0, tzinfo=UTC)) is None


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
