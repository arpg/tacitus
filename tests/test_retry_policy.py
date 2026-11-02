#!/usr/bin/env python3
"""Tests for preprocessor/retry_policy.py (circuit breaker).

Pins: open-after-threshold, fail-fast while open, half-open probe after
cooldown, close-on-success, reopen-on-probe-failure, cross-instance
state sharing via the JSON file, and open_breakers() surfacing for the
sentinel.

The Graph lanes: a background failure never opens the interactive
breaker; a background call fails fast while its breaker is open, an
interactive call probes once (success closes, failure re-opens); the
state file from before the lanes keeps working; the tick backoff counts
down its skips.
"""

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

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


def _age(service: str, seconds: float) -> None:
    """Move the entry's opening `seconds` into the past. The clock is not
    slept on: a short cooldown lets a slow write on a loaded runner pass
    the cooldown before the next check."""
    state = rp._load_all()
    state[service]["opened_at"] -= seconds
    rp._save_all(state)


def test_half_open_after_cooldown():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker(threshold=2, cooldown=300.0)
        b.record_failure("a")
        b.record_failure("b")
        assert b.is_open()
        _age("graph", 301)
        b._cached = None
        assert not b.is_open(), "past cooldown -> half-open, probe allowed"
        # Probe fails -> reopens with fresh opened_at
        b.record_failure("probe failed")
        assert b.is_open()
        # Probe succeeds -> closes
        _age("graph", 301)
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


# ── Graph lanes ───────────────────────────────────────────────────────

class _Lane:
    """EA_GRAPH_LANE set (None: unset) for a block, restored after it."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.before = os.environ.get(rp.GRAPH_LANE_ENV)
        if self.value is None:
            os.environ.pop(rp.GRAPH_LANE_ENV, None)
        else:
            os.environ[rp.GRAPH_LANE_ENV] = self.value

    def __exit__(self, *exc):
        if self.before is None:
            os.environ.pop(rp.GRAPH_LANE_ENV, None)
        else:
            os.environ[rp.GRAPH_LANE_ENV] = self.before


def test_the_graph_lane_comes_from_the_caller():
    with _Lane(None):
        assert rp.graph_lane() == "background"
        assert rp.graph_breaker_key() == "graph-background"
        assert rp.graph_lane("interactive") == "interactive"
        assert rp.graph_breaker_key("interactive") == "graph"
        # a CLI's own default yields to a lane its caller set
        assert rp.set_graph_lane("interactive", only_if_unset=True) == "interactive"
        assert rp.set_graph_lane("background", only_if_unset=True) == "interactive"
        assert rp.graph_lane() == "interactive"
        # an explicit lane on the call wins over the process's
        assert rp.graph_lane("background") == "background"
        rp.set_graph_lane("background")
        assert rp.graph_lane() == "background"
    with _Lane("nonsense"):
        assert rp.graph_lane() == "background"
    try:
        rp.set_graph_lane("urgent")
        assert False, "an unknown lane must be refused"
    except ValueError:
        pass


def test_a_background_failure_never_opens_the_interactive_breaker():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        background, interactive = _breaker("graph-background"), _breaker("graph")
        for _ in range(3):
            background.record_failure("HTTP 429 after 6 attempts")
        assert background.is_open()
        assert not interactive.is_open()
        assert [b["service"] for b in rp.open_breakers()] == ["graph-background"]


def test_the_state_file_from_before_the_lanes_keeps_working():
    """That file holds one `graph` entry. It stays where it is, as the
    interactive lane's entry; graph-background starts closed; a write for
    one lane carries the other's entry over unchanged."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        now = time.time()
        rp.STATE_FILE.write_text(json.dumps({"graph": {
            "failures": 3, "last_failure_ts": now, "opened_at": now - 30,
            "last_error": "HTTP 429 after 6 attempts"}}))
        interactive, background = _breaker("graph"), _breaker("graph-background")
        assert interactive.is_open()
        assert not background.is_open()
        background.record_failure("HTTP 429 after 6 attempts")
        state = json.loads(rp.STATE_FILE.read_text())
        assert state["graph"]["failures"] == 3 and state["graph"]["opened_at"] == now - 30
        assert state["graph-background"]["failures"] == 1
        interactive._cached = None
        interactive.record_success()
        state = json.loads(rp.STATE_FILE.read_text())
        assert "graph" not in state and state["graph-background"]["failures"] == 1


def test_circuit_open_names_its_service_and_reopen_time():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        b = _breaker("graph-background", threshold=1, cooldown=300.0)
        b.record_failure("HTTP 429 after 6 attempts")
        assert abs(b.reopens_at() - (time.time() + 300)) < 5
        try:
            try:
                b.raise_if_open()
            except rp.CircuitOpen as inner:
                raise RuntimeError("could not list the inbox") from inner
        except RuntimeError as outer:
            opened = rp.find_circuit_open(outer)
        assert opened is not None and opened.service == "graph-background"
        assert abs(opened.reopens_at - (time.time() + 300)) < 5
        assert rp.find_circuit_open(ValueError("x")) is None
        assert _breaker("graph").reopens_at() is None


def test_is_throttled_reads_a_429_through_the_chain():
    class _Resp:
        status_code = 429

    class _HTTPError(Exception):
        response = _Resp()

    assert rp.is_throttled(_HTTPError())
    try:
        try:
            raise _HTTPError()
        except _HTTPError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert rp.is_throttled(outer)
    assert not rp.is_throttled(RuntimeError("x"))
    assert not rp.is_throttled(None)


def test_the_tick_backoff_skips_the_next_three_ticks():
    with tempfile.TemporaryDirectory() as td:
        rp.TICK_BACKOFF_FILE = Path(td) / "tick-backoff.json"
        tagger = rp.TickBackoff("mail_tagger")
        assert tagger.take() == 0
        tagger.throttled("HTTP 429")
        assert tagger.pending() == rp.TICK_SKIPS == 3
        rp.TickBackoff("ledger_sync").throttled()
        assert [tagger.take() for _ in range(4)] == [3, 2, 1, 0]
        # one job's skips are its own
        assert rp.TickBackoff("ledger_sync").pending() == 3
        assert "mail_tagger" not in json.loads(rp.TICK_BACKOFF_FILE.read_text())


def test_a_tick_backoff_file_of_another_shape_reads_as_no_backoff():
    with tempfile.TemporaryDirectory() as td:
        rp.TICK_BACKOFF_FILE = Path(td) / "tick-backoff.json"
        for text in ("not json", "[1, 2]", '{"mail_tagger": {"skip": "3"}}',
                     '{"mail_tagger": {"skip": true}}', '{"mail_tagger": 3}'):
            rp.TICK_BACKOFF_FILE.write_text(text)
            assert rp.TickBackoff("mail_tagger").take() == 0, text


# ── graph_get in each lane ────────────────────────────────────────────

class _Resp:
    def __init__(self, status, headers=None, json_data=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self._json = json_data or {}
        self.text = text
        self.content = b"{}"

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self
            raise err

    def json(self):
        return self._json


def _graph(td):
    """graph_client with a fresh breaker state in `td`."""
    import graph_client as gc
    _setup(Path(td))
    gc._breakers.clear()
    return gc


def _get(gc, responses, lane):
    """graph_get on a queue of fake answers, with no sleeping; returns
    (result or the exception raised, {"calls", "waits"})."""
    state = {"calls": 0, "waits": []}

    def fake_get(*a, **kw):
        r = responses[state["calls"]]
        state["calls"] += 1
        return r
    with patch("requests.get", side_effect=fake_get), \
            patch.object(gc, "_headers", return_value={}), \
            patch.object(gc, "_api_base", return_value="https://graph"), \
            patch("time.sleep", side_effect=state["waits"].append):
        try:
            result = gc.graph_get("/me/messages", lane=lane)
        except Exception as e:
            result = e
    gc._breakers.clear()
    return result, state


def _open(service, age=30):
    """Write `service` open, `age` seconds into its cooldown."""
    now = time.time()
    state = rp._load_all()
    state[service] = {"failures": 3, "last_failure_ts": now, "opened_at": now - age,
                      "last_error": "HTTP 429 after 6 attempts", "cooldown": 300,
                      "threshold": 3, "opens": 1}
    rp._save_all(state)


def test_a_background_call_fails_fast_while_its_breaker_is_open():
    with tempfile.TemporaryDirectory() as td:
        gc = _graph(td)
        _open("graph-background")
        result, state = _get(gc, [_Resp(200, json_data={"ok": True})], "background")
        assert isinstance(result, rp.CircuitOpen) and state["calls"] == 0


def test_an_interactive_call_probes_an_open_breaker_and_a_success_closes_it():
    with tempfile.TemporaryDirectory() as td:
        gc = _graph(td)
        _open("graph")
        _open("graph-background")
        result, state = _get(gc, [_Resp(200, json_data={"ok": True})], "interactive")
        assert result == {"ok": True} and state["calls"] == 1
        after = rp._load_all()
        assert "graph" not in after
        assert after["graph-background"]["failures"] == 3   # the other lane is untouched


def test_a_failed_probe_reopens_the_breaker_after_one_attempt():
    with tempfile.TemporaryDirectory() as td:
        gc = _graph(td)
        _open("graph", age=200)
        before = rp._load_all()["graph"]["opened_at"]
        responses = [_Resp(429, headers={"Retry-After": "5"}), _Resp(200)]
        result, state = _get(gc, responses, "interactive")
        assert isinstance(result, gc.GraphThrottled) and result.response.status_code == 429
        assert state["calls"] == 1 and state["waits"] == []
        entry = rp._load_all()["graph"]
        assert entry["failures"] == 4 and entry["opened_at"] > before + 100
        assert str(result).startswith("Microsoft is limiting mail access right now.")


def test_background_throttling_leaves_the_owner_lane_closed():
    with tempfile.TemporaryDirectory() as td:
        gc = _graph(td)
        for _ in range(3):
            result, state = _get(gc, [_Resp(429, headers={"Retry-After": "2"})] * 6,
                                 "background")
            assert isinstance(result, gc.GraphThrottled) and state["calls"] == 6
        result, state = _get(gc, [_Resp(200)], "background")
        assert isinstance(result, rp.CircuitOpen) and state["calls"] == 0
        assert "graph" not in rp._load_all()
        # the person's call goes through on its full retry ladder
        result, state = _get(gc, [_Resp(429, headers={"Retry-After": "2"}),
                                  _Resp(200, json_data={"value": []})], "interactive")
        assert result == {"value": []} and state["waits"] == [2]


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
