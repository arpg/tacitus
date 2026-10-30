"""The clock stops a turn only when its stream goes quiet (a hang) or at
the turn's ceiling; a turn that keeps calling tools runs on, and the
heartbeat tells the user how to stop it. A letter turn that had written
its file and updated the ledger was cut at 600 s, two seconds before its
reply, on 2026-10-30."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import turn_watch as tw  # noqa: E402

BOT = (ROOT / "ea-chat-bot.py").read_text()


def _watch(ceiling, stall, now):
    stopped = []
    w = tw.Watchdog(proc="p", ceiling=ceiling, stall=stall, stopper=stopped.append,
                    clock=lambda: now[0], tick=1000)
    return w, stopped


def test_a_working_run_outlives_the_stall_clock():
    now = [0.0]
    w, stopped = _watch(ceiling=3600, stall=600, now=now)
    for minute in range(1, 50):
        now[0] = minute * 60.0
        w.alive()                      # a stream line each minute
        assert w.check() is False
    assert not stopped and not w.expired.is_set()


def test_a_quiet_stream_stops_the_run_as_a_stall():
    now = [0.0]
    w, stopped = _watch(ceiling=3600, stall=600, now=now)
    now[0] = 300.0
    w.alive()
    now[0] = 899.0
    assert w.check() is False
    now[0] = 900.0
    assert w.check() is True
    assert stopped == ["p"] and w.expired.is_set() and w.stalled and w.limit == 600


def test_the_ceiling_stops_a_run_that_is_still_working():
    now = [0.0]
    w, stopped = _watch(ceiling=3600, stall=600, now=now)
    for minute in range(1, 60):
        now[0] = minute * 60.0
        w.alive()
        assert w.check() is False
    now[0] = 3600.0
    w.alive()
    assert w.check() is True
    assert stopped == ["p"] and not w.stalled and w.limit == 3600


def test_no_stall_limit_leaves_only_the_ceiling():
    now = [0.0]
    w, stopped = _watch(ceiling=100, stall=None, now=now)
    now[0] = 99.0
    assert w.check() is False
    now[0] = 100.0
    assert w.check() is True and not w.stalled and w.limit == 100


def test_a_stopper_that_fails_still_marks_the_run_expired():
    def boom(_p):
        raise RuntimeError("gone")
    now = [0.0]
    w = tw.Watchdog(proc="p", ceiling=10, stall=None, stopper=boom, clock=lambda: now[0], tick=1000)
    now[0] = 10.0
    assert w.check() is True and w.expired.is_set()


def test_the_bot_runs_every_turn_on_the_two_clocks():
    """The runner feeds the stream to the watchdog and reports a stall
    apart from a timeout; the turn handler words each; the heartbeat says
    how to stop a long turn; a batched hand-off has no clock of its own."""
    assert "\nSTALL_SECONDS = 600\n" in BOT and "\nTIMEOUT_SECONDS = 3600\n" in BOT
    assert "\nCANCEL_HINT_S = 540\n" in BOT
    runner = BOT[BOT.index("\ndef _run_claude_streaming("):BOT.index("\n# ----", BOT.index("\ndef _run_claude_streaming("))]
    assert 'stall: "float | None" = STALL_SECONDS' in runner
    assert "watchdog = _turn_watch.Watchdog(proc, timeout, stall, _stop_claude)" in runner
    assert "watchdog.alive()" in runner.split("for line in proc.stdout:")[1].split("line = line.strip()")[0]
    assert "raise subprocess.TimeoutExpired(cmd, watchdog.limit)" in runner
    assert "stalled=stalled," in runner and "threading.Timer" not in runner
    assert "stalled: bool = False" in BOT
    handler = BOT[BOT.index("\ndef handle_message_async("):BOT.index("\ndef ", BOT.index("\ndef handle_message_async(") + 1)]
    assert "raise subprocess.TimeoutExpired([\"claude\"], STALL_SECONDS if cr.stalled else run_timeout)" in handler
    assert "except subprocess.TimeoutExpired as e:" in handler
    assert "stalled = e.timeout is not None and e.timeout < run_timeout" in handler
    assert "No output for {int(e.timeout) // 60} minutes" in handler
    assert "Stopped at the {run_timeout // 60} minute limit" in handler
    assert "Reply `continue` to pick up where it stopped." in handler
    heartbeat = handler[handler.index("def _heartbeat():"):handler.index("hb_thread = ")]
    assert "if time.monotonic() - progress.started >= CANCEL_HINT_S:" in heartbeat
    assert 'text += " Say `cancel` to stop."' in heartbeat
    tick = BOT[BOT.index("\ndef _remote_escalation_tick("):BOT.index("\ndef ", BOT.index("\ndef _remote_escalation_tick(") + 1)]
    assert "timeout_s" not in tick and "REMOTE_ESCALATION_TIMEOUT_MAX_S" not in BOT
