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


ROOM = ("[From the private #ea-demo channel, scoped to project 'demo'. Reply in this "
        "thread; the project pack follows below. Anything you draft or journal belongs "
        "to this project unless said otherwise.]\n\n")
DM = ("[From your direct message with the EA. No project scope: treat this as a quick "
      "ask, reply here, and file anything you produce under the project it belongs to.]\n\n")
MIRROR = ("[From #tmp-1a2b3c4d, the chat room that mirrors your session 1a2b3c4d. Every "
          "message here continues that one conversation. Reply here.]\n\n")


@pytest.mark.parametrize("head", [ROOM, DM, MIRROR])
def test_a_cancel_in_a_room_or_the_dm_is_read_after_the_header(head):
    assert tc.is_cancel(head + "cancel that")
    assert tc.is_cancel(head + "stop")
    assert not tc.is_cancel(head + "cancel the meeting with the dean on Friday please")
    assert tc.own_words(head + "cancel that") == "cancel that"


def test_own_words_keeps_a_message_without_a_header():
    assert tc.own_words("stop it") == "stop it"
    assert tc.own_words("") == ""


def test_deleted_ids_reads_both_event_shapes():
    assert tc.deleted_ids({"type": "delete_message", "message_id": 5945}) == ["5945"]
    assert tc.deleted_ids({"type": "delete_message", "message_ids": [1, 2]}) == ["1", "2"]
    assert tc.deleted_ids({}) == []


def test_withdrawn_keeps_the_newest_ids():
    w = tc.Withdrawn()
    w.KEEP = 3
    for i in range(5):
        w.add(i)
    w.add("")
    assert not w.has(0) and not w.has(1)
    assert w.has(2) and w.has("4") and not w.has("")


def test_the_bot_drops_a_deleted_message_that_waited():
    assert '@app.event("delete_message")' in BOT
    i = BOT.index("def on_delete_message(")
    assert "_WITHDRAWN.add(" in BOT[i:i + 400]
    j = BOT.index("held_session_lock = session_lock")
    block = BOT[j:j + 400]
    assert "_WITHDRAWN.has(msg_ts)" in block and "return" in block, \
        "the check runs after the wait, under the held lock, so finally releases it"
    assert block.index("_WITHDRAWN.has(msg_ts)") < block.index('_swap_reaction(channel, msg_ts, "hourglass_flowing_sand", "eyes")'), \
        "a deleted message gets no reactions"


def test_the_stop_notice_names_the_users_words():
    i = BOT.index("def _cancel_thread_runs(")
    assert "_turn_cancel.own_words(" in BOT[i:BOT.index("\ndef ", i + 1)]


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


# --- a stopped request is withdrawn on the thread's next turn --------------

import slack_sessions as ss  # noqa: E402


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "REGISTRY", tmp_path / "slack-sessions.json")
    return ss


def test_the_note_quotes_the_stopped_words_and_points_at_the_latest_message():
    assert tc.withdrawn_note("") == "" and tc.withdrawn_note("   ") == ""
    note = tc.withdrawn_note("draft a reply to the  committee\nabout Friday")
    assert "> draft a reply to the committee about Friday" in note
    assert "withdrawn" in note and "Do not finish it" in note
    assert "latest message" in note


def test_the_stopped_words_drop_the_header_and_keep_a_bounded_length():
    assert tc.stopped_words("[From your direct message, 10:02]\n\nfind a time with Kit") == \
        "find a time with Kit"
    long = tc.stopped_words("word " * 400)
    assert len(long) == tc.STOPPED_MAX_CHARS and long.endswith("…")


def test_a_side_session_stop_is_written_on_the_thread_too():
    assert tc.stopped_session_keys({"thread_ts": "t1"}) == ["t1"]
    assert tc.stopped_session_keys({"thread_ts": "t1", "session_key": "t1~row9"}) == ["t1~row9", "t1"]
    assert tc.stopped_session_keys({}) == []


def test_a_stop_is_read_by_the_next_turn_only(registry):
    registry.lookup("t1")
    assert tc.record_stop(registry, ["t1"], "send Kit the slides") == []
    _, resumable, meta = registry.lookup("t1")
    assert resumable and meta[tc.STOPPED_KEY] == "send Kit the slides"
    note = tc.take_stop_note(registry, "t1", meta)
    assert "> send Kit the slides" in note
    _, _, meta = registry.lookup("t1")
    assert tc.STOPPED_KEY not in meta, "the next turn takes the request off the row"
    assert tc.take_stop_note(registry, "t1", meta) == "", "a later turn adds no note"


def test_forget_meta_leaves_the_other_keys(registry):
    registry.lookup("t1")
    registry.remember("t1", use_mcp=True, **{tc.STOPPED_KEY: "x"})
    registry.forget_meta("t1", tc.STOPPED_KEY, "absent")
    _, _, meta = registry.lookup("t1")
    assert meta == {"use_mcp": True}
    registry.forget_meta("no-such-thread", tc.STOPPED_KEY)   # no row: nothing happens


def test_the_bot_withdraws_the_stopped_request():
    i = BOT.index("def _cancel_thread_runs(")
    body = BOT[i:BOT.index("\ndef ", i + 1)]
    assert body.index("_turn_cancel.record_stop(") < body.index("_LIVE_RUNS.stop("), \
        "the request is written before the stop, so a waiting message reads it"
    assert "_turn_cancel.forget_stop(" in body, "a run that was not live leaves no note"
    j = BOT.index("held_session_lock = session_lock")
    k = BOT.index("stopped_note = _turn_cancel.take_stop_note(_sessions, skey, session_meta)")
    assert j < k, "the note is taken after the wait, from the meta the last lookup read"
    assert "task_block = task_block + \"\\n\\n\" + stopped_note" in BOT, "a fresh prompt carries it"
    r = BOT.index("{stopped_block}The user replied:")
    assert k < r, "a resumed session reads the note before the reply"
