#!/usr/bin/env python3
"""Tests for preprocessor/retry_policy.py (circuit breaker).

Pins: open-after-threshold, fail-fast while open, half-open probe after
cooldown, close-on-success, reopen-on-probe-failure, cross-instance
state sharing via the JSON file, and open_breakers() surfacing for the
sentinel.
"""

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# The breaker reads its state through resident_io, which runs a resident
# child in an owner process; a sandboxed EA_DIR makes this process a
# resident one, so the calls act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-retry-policy-")

import retry_policy as rp  # noqa: E402


def _setup(tmp: Path):
    rp.STATE_FILE = tmp / "circuit-breakers.json"


def _breaker(service="graph", threshold=3, cooldown=300.0):
    b = rp.CircuitBreaker(service, threshold=threshold, cooldown=cooldown)
    b._cached = None  # defeat the read cache between tests
    return b


def test_closed_by_default():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker()
        assert not b.is_open()
        b.raise_if_open()  # must not raise


def test_opens_after_threshold():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker(threshold=3)
        b.record_failure("e1")
        b.record_failure("e2")
        assert not b.is_open(), "below threshold must stay closed"
        b.record_failure("e3")
        assert b.is_open()
        try:
            b.raise_if_open()
            assert False, "should have raised CircuitOpen"
        except rp.CircuitOpen as e:
            assert "graph" in str(e)
            assert "e3" in str(e)


def test_success_closes_and_resets():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker(threshold=2)
        b.record_failure("x")
        b.record_success()
        b.record_failure("y")
        assert not b.is_open(), "success must reset the consecutive count"


def test_half_open_after_cooldown():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker(threshold=2, cooldown=0.05)
        b.record_failure("a")
        b.record_failure("b")
        assert b.is_open()
        time.sleep(0.08)
        b._cached = None
        assert not b.is_open(), "past cooldown -> half-open, probe allowed"
        # Probe fails -> reopens with fresh opened_at
        b.record_failure("probe failed")
        assert b.is_open()
        # Probe succeeds -> closes
        time.sleep(0.08)
        b._cached = None
        b.record_success()
        assert not b.is_open()


def test_state_shared_across_instances():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        a = _breaker(threshold=2)
        a.record_failure("1")
        a.record_failure("2")
        b = _breaker(threshold=2)  # fresh instance, same file
        assert b.is_open()


def test_open_breakers_surfaces_for_sentinel():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker("graph", threshold=rp.FAILURE_THRESHOLD)
        for i in range(rp.FAILURE_THRESHOLD):
            b.record_failure(f"err{i}")
        open_now = rp.open_breakers()
        assert len(open_now) == 1
        assert open_now[0]["service"] == "graph"
        b.record_success()
        assert rp.open_breakers() == []


def test_state_write_failure_never_raises():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td) / "nonexistent-subdir-removed")
        # Point STATE_FILE at an unwritable path: parent mkdir succeeds in
        # _save_all normally; simulate by pointing at a directory path.
        rp.STATE_FILE = Path(td)  # a directory — write_text will fail
        b = _breaker()
        b.record_failure("x")  # must not raise
        b.record_success()     # must not raise



def test_an_entry_records_its_own_cooldown_and_each_reopening():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker("remote", threshold=2, cooldown=900.0)
        b.record_failure("HTTP 502")
        b.record_failure("HTTP 502")          # opens
        e = rp._load_all()["remote"]
        assert e["cooldown"] == 900.0 and e["opens"] == 1
        b.record_failure("HTTP 502")          # inside the cooldown: no new opening
        assert rp._load_all()["remote"]["opens"] == 1
        # past the cooldown the half-open probe fails: a second opening
        state = rp._load_all()
        state["remote"]["opened_at"] -= 1000
        rp._save_all(state)
        b.record_failure("HTTP 502")
        assert rp._load_all()["remote"]["opens"] == 2
        b.record_success()
        assert "remote" not in rp._load_all()


def test_open_breakers_reads_the_entry_cooldown_not_the_default():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker("remote", threshold=1, cooldown=900.0)
        b.record_failure("HTTP 502")
        state = rp._load_all()
        # 12 minutes since the opening and the last failure: past the
        # module default's window (2 x 5 min), inside this breaker's own
        state["remote"]["opened_at"] -= 720
        state["remote"]["last_failure_ts"] -= 720
        rp._save_all(state)
        assert [s["service"] for s in rp.open_breakers()] == ["remote"]
        # 32 minutes with no probe: past two of its cooldowns, so it drops
        state["remote"]["opened_at"] -= 1200
        state["remote"]["last_failure_ts"] -= 1200
        rp._save_all(state)
        assert rp.open_breakers() == []


def test_a_sustained_outage_stays_listed_until_a_success():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker("remote", threshold=1, cooldown=60.0)
        for _ in range(rp.SUSTAINED_OPENS):
            b.record_failure("HTTP 502")
            state = rp._load_all()
            state["remote"]["opened_at"] -= 100
            rp._save_all(state)
        state = rp._load_all()
        assert state["remote"]["opens"] == rp.SUSTAINED_OPENS
        state["remote"]["last_failure_ts"] -= 6 * 3600   # no probe for six hours
        rp._save_all(state)
        assert [s["service"] for s in rp.open_breakers()] == ["remote"]
        b.record_success()
        assert rp.open_breakers() == []

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
