"""A `stop` or `cancel` in a busy thread ends that thread's running turn:
the cancel words, the live-run registry, and the bot's wiring (the
process registered, the cancel branch before Claude runs, the cancelled
result posting no reply but surfacing its drafts)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import turn_cancel as tc  # noqa: E402

BOT = (ROOT / "ea-chat-bot.py").read_text()


@pytest.mark.parametrize("text", ["stop", "Cancel", "cancel that", "stop it", "abort", "never mind",
                                  "nevermind", "wrong channel", "please stop", "cancel the last request",
                                  "Stop!", "disregard that"])
def test_cancel_words_are_recognised(text):
    assert tc.is_cancel(text)


@pytest.mark.parametrize("text", ["stop sending the newsletter to Sam", "cancel the meeting with the dean",
                                  "please cancel my subscription and tell them why", "", "   ",
                                  "stopwatch", "the stop sign", "can you stop by my office tomorrow"])
def test_instructions_that_mention_stopping_are_not_cancels(text):
    assert not tc.is_cancel(text)


class _Proc:
    def __init__(self):
        self.stopped = False


def test_the_registry_stops_a_live_run_once_and_marks_it_cancelled():
    runs = tc.LiveRuns()
    p = _Proc()
    runs.register("r1", p)
    assert runs.live() == ["r1"] and not runs.cancelled("r1")

    def stopper(proc):
        proc.stopped = True
    assert runs.stop("r1", stopper) and p.stopped and runs.cancelled("r1")
    assert not runs.stop("missing", stopper), "nothing live under that id"
    runs.unregister("r1")
    assert runs.live() == [] and runs.cancelled("r1"), "the flag outlives the process so the lane reads it"
    runs.forget("r1")
    assert not runs.cancelled("r1")
    runs.register("", p)
    assert runs.live() == [], "a run without an id is not tracked"


def test_the_bot_wires_the_cancel():
    assert "_LIVE_RUNS.register(run_id, proc)" in BOT, "the runner registers each live process"
    assert "_LIVE_RUNS.cancelled(run_id)" in BOT and "cancelled=True" in BOT
    assert "cancelled: bool = False" in BOT, "ClaudeResult carries the flag"
    assert "_turn_cancel.is_cancel(user_msg)" in BOT, "the message handler checks before it runs Claude"
    assert "def _cancel_thread_runs(" in BOT and "_LIVE_RUNS.stop(" in BOT
    assert "raise _TurnCancelled()" in BOT and "except _TurnCancelled:" in BOT
    i = BOT.index("except _TurnCancelled:")
    block = BOT[i:i + 1200]
    assert "_surface_run_drafts(" in block, "a stopped turn's drafts still get their cards"
    assert "not cr.cancelled" in BOT or "if cr.cancelled:\n            raise _TurnCancelled()" in BOT
