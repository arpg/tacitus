#!/usr/bin/env python3
"""Tests for preprocessor/resident_bridge.py (tmux pane bridge).

Pure-function coverage only — no tmux, no sudo, no live pane. Pins the
transcript-parsing semantics the dispatch loop depends on:
  - final_text_of: last pure-text assistant entry wins; a NEWER tool_use
    supersedes earlier text (model still working); meta lines ignored.
  - parse_entries: garbage lines skipped.
  - enabled(): default ON, honors resident_mode=false.
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# Owner-side file access goes through resident_io, which runs a resident
# child in an owner process; a sandboxed EA_DIR makes this process a
# resident one, so the calls act directly on the test's temp paths. The
# private dir is sandboxed too, off the real ~/.ea-private.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-bridge-")
os.environ.setdefault("EA_PRIVATE_DIR", tempfile.mkdtemp(prefix="ea-test-bridge-priv-"))

import resident_bridge as rb  # noqa: E402


def _assistant(blocks):
    return json.dumps({"type": "assistant",
                       "message": {"content": blocks}})


def _user_tool_result():
    return json.dumps({"type": "user",
                       "message": {"content": [{"type": "tool_result",
                                                "content": "ok"}]}})


def test_parse_skips_garbage():
    raw = "not json\n" + _assistant([{"type": "text", "text": "hi"}]) + "\n\n"
    entries = rb.parse_entries(raw)
    assert len(entries) == 1


def test_final_text_simple_reply():
    raw = "\n".join([
        json.dumps({"type": "user", "message": {"content": "q"}}),
        _assistant([{"type": "text", "text": "Done. Draft created."}]),
    ])
    assert rb.final_text_of(rb.parse_entries(raw)) == "Done. Draft created."


def test_tool_use_supersedes_earlier_text():
    # Model wrote text, then called another tool -> NOT final yet.
    raw = "\n".join([
        _assistant([{"type": "text", "text": "Let me check the ledger."}]),
        _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
        _user_tool_result(),
    ])
    assert rb.final_text_of(rb.parse_entries(raw)) is None


def test_text_after_tools_is_final():
    raw = "\n".join([
        _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
        _user_tool_result(),
        _assistant([{"type": "text", "text": "first answer"}]),
        _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
        _user_tool_result(),
        _assistant([{"type": "text", "text": "final answer"}]),
    ])
    assert rb.final_text_of(rb.parse_entries(raw)) == "final answer"


def test_mixed_text_and_tool_block_not_final():
    raw = _assistant([{"type": "text", "text": "Working on it"},
                      {"type": "tool_use", "name": "Bash", "input": {}}])
    assert rb.final_text_of(rb.parse_entries(raw)) is None


def test_meta_lines_ignored():
    raw = "\n".join([
        json.dumps({"type": "mode"}),
        json.dumps({"type": "agent-name"}),
        _assistant([{"type": "text", "text": "reply"}]),
        json.dumps({"type": "permission-mode"}),
    ])
    assert rb.final_text_of(rb.parse_entries(raw)) == "reply"


def test_empty_text_not_final():
    raw = _assistant([{"type": "text", "text": "   "}])
    assert rb.final_text_of(rb.parse_entries(raw)) is None


def test_enabled_default_and_flag():
    import ea_config
    orig = ea_config.load
    try:
        ea_config.load = lambda *a, **k: {}
        assert rb.enabled() is True, "default must be ON"
        ea_config.load = lambda *a, **k: {"resident_mode": False}
        assert rb.enabled() is False
        ea_config.load = lambda *a, **k: {"resident_mode": True}
        assert rb.enabled() is True
    finally:
        ea_config.load = orig


def _user(text, uuid="u0"):
    return json.dumps({"type": "user", "uuid": uuid,
                       "message": {"content": text}})


def _assistant_u(blocks, uuid):
    return json.dumps({"type": "assistant", "uuid": uuid,
                       "message": {"content": blocks}})


def test_segment_reply_matched_to_its_dispatch():
    """Two interleaved dispatches: each reply matched by marker position,
    not arrival order — the core of queue-injection parallel dispatch."""
    m1 = rb.make_marker("1781.100", "1781.101")
    m2 = rb.make_marker("1781.200", "1781.201")
    raw = "\n".join([
        _user(f"{m1} Kit says:\ndo X"),
        _assistant_u([{"type": "tool_use", "name": "Bash", "input": {}}], "a1"),
        _user_tool_result(),
        _assistant_u([{"type": "text", "text": "X is done."}], "a2"),
        _user(f"{m2} Kit says:\ndo Y"),
        _assistant_u([{"type": "text", "text": "Y is done."}], "a3"),
    ])
    entries = rb.parse_entries(raw)
    r1, closed1, _ = rb.find_segment_reply(entries, "1781.101")
    r2, closed2, _ = rb.find_segment_reply(entries, "1781.201")
    assert r1 == "X is done." and closed1 is True
    assert r2 == "Y is done." and closed2 is False  # still open segment


def test_segment_reply_pending_while_tools_running():
    m1 = rb.make_marker("t", "id-1")
    raw = "\n".join([
        _user(f"{m1} do X"),
        _assistant_u([{"type": "text", "text": "Let me look."}], "a1"),
        _assistant_u([{"type": "tool_use", "name": "Bash", "input": {}}], "a2"),
        _user_tool_result(),
    ])
    reply, closed, _ = rb.find_segment_reply(rb.parse_entries(raw), "id-1")
    assert reply is None and closed is False


def test_segment_reply_missing_marker():
    raw = _user("unrelated message")
    assert rb.find_segment_reply(rb.parse_entries(raw), "nope") == \
        (None, False, None)


def test_deliver_tag_on_direct_reply_stripped_and_flagged():
    m1 = rb.make_marker("1781.300", "id-9")
    raw = "\n".join([
        _user(f"{m1} long task"),
        _assistant_u([{"type": "text",
                       "text": "[deliver:1781.300] All finished."}], "a7"),
        _user(rb.make_marker("x", "id-10") + " next"),
    ])
    reply, closed, tagged = rb.find_segment_reply(
        rb.parse_entries(raw), "id-9")
    assert reply == "All finished."
    assert closed is True
    assert tagged == "a7"  # caller marks it so the router won't repost


def test_extract_deliveries():
    raw = "\n".join([
        _assistant_u([{"type": "text",
                       "text": "[deliver:1781.400] Research report:\n- a\n- b"}],
                     "d1"),
        _assistant_u([{"type": "text", "text": "plain reply, no tag"}], "d2"),
        _assistant_u([{"type": "tool_use", "name": "Bash", "input": {}}], "d3"),
    ])
    ds = rb.extract_deliveries(rb.parse_entries(raw))
    assert len(ds) == 1
    assert ds[0]["uuid"] == "d1"
    assert ds[0]["thread_ts"] == "1781.400"
    assert ds[0]["text"].startswith("Research report:")


def test_extract_deliveries_accepts_an_imported_thread_key():
    """A thread the web chat imported from Zulip keeps its old key."""
    key = "stream:12:topic:Hello%20there"
    raw = _assistant_u([{"type": "text", "text": f"[deliver:{key}] Report done."}],
                       "z1")
    ds = rb.extract_deliveries(rb.parse_entries(raw))
    assert len(ds) == 1
    assert ds[0]["thread_ts"] == key
    assert ds[0]["text"] == "Report done."


def test_router_posted_set_roundtrip():
    orig_rs, orig_lk = rb.ROUTER_STATE_FILE, rb.SEND_LOCK_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            rb.ROUTER_STATE_FILE = Path(td) / "router.json"
            rb.SEND_LOCK_FILE = Path(td) / "send.lock"
            assert not rb.already_posted("u-1")
            rb.mark_posted("u-1")
            assert rb.already_posted("u-1")
            rb.mark_posted("u-1")  # idempotent
            state = json.loads(rb.ROUTER_STATE_FILE.read_text())
            assert state["posted"].count("u-1") == 1
    finally:
        rb.ROUTER_STATE_FILE, rb.SEND_LOCK_FILE = orig_rs, orig_lk


def _setup_inflight(tmp: Path):
    rb.INFLIGHT_FILE = tmp / "resident-inflight.json"
    rb.SEND_LOCK_FILE = tmp / "send.lock"


def test_inflight_claim_then_busy():
    """One foreground turn at a time: first claim wins, second sees busy."""
    orig = (rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_inflight(Path(td))
            assert rb.claim_inflight("tA") is True
            assert rb.inflight_busy() is True
            assert rb.claim_inflight("tB") is False   # held by tA
            rb.release_inflight()
            assert rb.inflight_busy() is False
            assert rb.claim_inflight("tB") is True     # now free
    finally:
        rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE = orig


def test_inflight_stale_claim_taken_over():
    """A timed-out turn's claim ages out after INFLIGHT_TTL so the lane
    never wedges shut."""
    import json as _json, time as _t
    orig = (rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_inflight(Path(td))
            rb.INFLIGHT_FILE.write_text(_json.dumps(
                {"thread_ts": "old", "ts": _t.time() - rb.INFLIGHT_TTL - 10}))
            assert rb.inflight_busy() is False         # stale -> not busy
            assert rb.claim_inflight("tNew") is True    # taken over
    finally:
        rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE = orig


def test_inflight_release_idempotent_and_failopen():
    orig = (rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_inflight(Path(td))
            rb.release_inflight()                       # no file yet -> no crash
            assert rb.claim_inflight("t1") is True
            rb.release_inflight()
            rb.release_inflight()                       # double release ok
            assert rb.inflight_busy() is False
    finally:
        rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE = orig


def test_user_text_handles_both_shapes():
    s = rb.parse_entries(_user("plain string"))[0]
    assert rb._user_text(s) == "plain string"
    blocks = json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "block text"}]}})
    b = rb.parse_entries(blocks)[0]
    assert rb._user_text(b) == "block text"


def _setup_pending(tmp: Path):
    rb.PENDING_FILE = tmp / "resident-pending.json"
    rb.SEND_LOCK_FILE = tmp / "send.lock"
    rb.STATE_FILE = tmp / "resident-pane.json"
    rb.ROUTER_STATE_FILE = tmp / "router.json"


def test_pending_registry_roundtrip():
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_pending(Path(td))
            rb.register_pending("d1", thread_ts="t1", channel="C1",
                                start_offset=100, journal_offset=5)
            p = rb.pending_dispatches()
            assert p["d1"]["thread_ts"] == "t1"
            assert p["d1"]["journal_offset"] == 5
            rb.clear_pending("d1")
            assert rb.pending_dispatches() == {}
            rb.clear_pending("never-existed")  # must not raise
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig


def test_recover_pending_delivers_late_reply():
    """The poll-reply fix: a timed-out dispatch whose reply later
    materializes must be recovered with its routing metadata."""
    import time as _t
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    orig_fns = (rb._tsize, rb._tread)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _setup_pending(tmp)
            rb.STATE_FILE.write_text(json.dumps({"session_id": "s-1"}))
            rb.register_pending("late-1", thread_ts="t9", channel="C9",
                                start_offset=0, journal_offset=0)
            # age the entry past the await window
            p = rb.pending_dispatches()
            p["late-1"]["created"] = _t.time() - rb.DEFAULT_TIMEOUT_S - 60
            rb.PENDING_FILE.write_text(json.dumps(p))
            marker = rb.make_marker("t9", "late-1")
            raw = "\n".join([
                _user(f"{marker} fill the poll"),
                _assistant_u([{"type": "text",
                               "text": "Poll filled: 3 slots."}], "a9"),
                _user(rb.make_marker("tX", "next-id") + " next"),
            ])
            rb._tsize = lambda tp: len(raw)
            rb._tread = lambda tp, off: raw
            out = rb.recover_pending()
            assert len(out) == 1
            assert out[0]["reply"] == "Poll filled: 3 slots."
            assert out[0]["thread_ts"] == "t9"
            assert rb.pending_dispatches() == {}, "recovered entry cleared"
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig
        rb._tsize, rb._tread = orig_fns


def test_recover_pending_waits_for_live_awaits():
    """Entries younger than the await window are NOT touched — the live
    await still owns that segment."""
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _setup_pending(tmp)
            rb.STATE_FILE.write_text(json.dumps({"session_id": "s-1"}))
            rb.register_pending("fresh", thread_ts="t", channel="C",
                                start_offset=0, journal_offset=0)
            assert rb.recover_pending() == []
            assert "fresh" in rb.pending_dispatches()
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig


def test_recover_pending_expires_ancient_entries():
    import time as _t
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _setup_pending(tmp)
            rb.STATE_FILE.write_text(json.dumps({"session_id": "s-1"}))
            rb.register_pending("old", thread_ts="t", channel="C",
                                start_offset=0, journal_offset=0)
            p = rb.pending_dispatches()
            p["old"]["created"] = _t.time() - rb.PENDING_EXPIRE_S - 10
            rb.PENDING_FILE.write_text(json.dumps(p))
            out = rb.recover_pending()
            assert len(out) == 1 and out[0]["reply"] is None
            assert rb.pending_dispatches() == {}
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig


def test_maybe_rotate_guards():
    """No rotation while small, and NEVER while dispatches are pending."""
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    orig_tsize = rb._tsize
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _setup_pending(tmp)
            rb.STATE_FILE.write_text(json.dumps({"session_id": "s-1"}))
            rb._tsize = lambda tp: 100  # tiny transcript
            assert rb.maybe_rotate() is False
            rb._tsize = lambda tp: rb.ROTATE_MAX_BYTES + 1
            rb.register_pending("inflight", thread_ts="t", channel="C",
                                start_offset=0, journal_offset=0)
            assert rb.maybe_rotate() is False, \
                "must not rotate away a pending dispatch's transcript"
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig
        rb._tsize = orig_tsize


def test_rotation_due_is_the_nondestructive_half():
    """rotation_due() answers "would maybe_rotate() fire?" without firing.

    The bot asks it first so it can DEFER rotation on a thread follow-up
    and log that decision. Rotating mid-thread starts the turn on a cold
    session and overruns the dispatch timeout."""
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
            rb.ROUTER_STATE_FILE)
    orig_tsize = rb._tsize
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _setup_pending(tmp)
            rb._tsize = lambda tp: rb.ROTATE_MAX_BYTES + 1
            assert rb.rotation_due() is False, "no session -> nothing to rotate"
            rb.STATE_FILE.write_text(json.dumps({"session_id": "s-1"}))
            assert rb.rotation_due() is True             # over cap, idle
            rb._tsize = lambda tp: 100
            assert rb.rotation_due() is False            # under the cap
            rb._tsize = lambda tp: rb.ROTATE_MAX_BYTES + 1
            rb.register_pending("inflight", thread_ts="t", channel="C",
                                start_offset=0, journal_offset=0)
            assert rb.rotation_due() is False, \
                "a pending dispatch's transcript must not be rotated away"
            assert rb.maybe_rotate() is False            # and neither rotates
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE,
         rb.ROUTER_STATE_FILE) = orig
        rb._tsize = orig_tsize


def test_transcript_path_follows_state():
    orig = rb.STATE_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "resident-pane.json"
            sf.write_text(json.dumps({"session_id": "abc-123"}))
            rb.STATE_FILE = sf
            assert rb.transcript_path().name == "abc-123.jsonl"
            sf.unlink()
            assert rb.transcript_path() is None
    finally:
        rb.STATE_FILE = orig


def test_usage_limit_detection():
    # Real CLI pane phrasing + reset time.
    assert rb._usage_limit_hit(
        "You've hit your session limit · resets 7:10pm (America/Denver)"
    ) == "7:10pm (America/Denver)"
    # Structured transcript rate-limit error, reset elsewhere in the slice.
    assert rb._usage_limit_hit(
        '{"error":"rate_limit","isApiErrorMessage":true,"apiErrorStatus":429}'
        ' ... resets 12:49 AM (America/Denver)') == "12:49 AM (America/Denver)"
    # A limit with no parseable reset still detects (empty hint, not None).
    assert rb._usage_limit_hit('{"error":"rate_limit"}') == ""
    # Benign replies (even ones mentioning limits) must NOT trigger.
    assert rb._usage_limit_hit("Here's your schedule for next week.") is None
    assert rb._usage_limit_hit(
        "Your plan usage limit is 200 messages per session.") is None
    assert rb._usage_limit_hit("") is None


def test_rotation_deferral_is_bounded_by_hard_max():
    """The bot defers rotation on a thread follow-up to protect live context.
    That deferral must not be unbounded: ROTATE_HARD_MAX_BYTES is the ceiling
    past which the bot rotates even mid-thread, because beyond it every
    remaining turn of the day pays the slow-transcript cost."""
    import resident_bridge as rb
    assert rb.ROTATE_HARD_MAX_BYTES > rb.ROTATE_MAX_BYTES

    src = (HERE.parent / "ea-chat-bot.py").read_text()
    i = src.find("Resident rotation deferred")
    assert i != -1, "deferral branch missing from ea-chat-bot.py"
    window = src[max(0, i - 400):i]
    assert "ROTATE_HARD_MAX_BYTES" in window, (
        "the thread_seen deferral must be gated on the hard max, otherwise a "
        "long single-thread day never rotates")


def _launched(agent_id="a1"):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text",
         "text": f"Async agent launched successfully. agentId: {agent_id}"}]}]}})


def test_background_agent_holds_the_segment_until_its_result_is_read():
    """A background subagent launched inside the segment makes the text
    written before it look final. The dispatch stays open until the task
    notification arrives or the model reads the task's output; a later
    marker closes it regardless."""
    m1 = rb.make_marker("t", "id-1")
    before = [
        _user(f"{m1} write an intro email"),
        _assistant_u([{"type": "tool_use", "name": "Agent",
                       "input": {"subagent_type": "email-author", "run_in_background": True}}], "a1"),
        _launched("a1"),
        _assistant_u([{"type": "text", "text": "Writing the intro now."}], "a2"),
    ]
    entries = rb.parse_entries("\n".join(before))
    reply, closed, _ = rb.find_segment_reply(entries, "id-1")
    assert reply == "Writing the intro now." and closed is False
    assert rb.segment_background_pending(entries, "id-1") is True

    notified = before + [
        _user("<task-notification><task-id>a1</task-id><status>completed</status></task-notification>", "u9"),
        _assistant_u([{"type": "tool_use", "name": "Bash", "input": {}}], "a3"),
        _user_tool_result(),
        _assistant_u([{"type": "text", "text": ":email: Draft created to Alex and Oakley."}], "a4"),
    ]
    entries = rb.parse_entries("\n".join(notified))
    assert rb.segment_background_pending(entries, "id-1") is False
    assert rb.find_segment_reply(entries, "id-1")[0] == ":email: Draft created to Alex and Oakley."

    # the common shape: launch, then a blocking TaskOutput read, no notification ever
    read_back = before[:3] + [
        _assistant_u([{"type": "tool_use", "name": "TaskOutput",
                       "input": {"task_id": "a1", "block": True}}], "a5"),
        _user_tool_result(),
        _assistant_u([{"type": "text", "text": ":email: Draft created to Gus."}], "a6"),
    ]
    entries = rb.parse_entries("\n".join(read_back))
    assert rb.segment_background_pending(entries, "id-1") is False
    assert rb.find_segment_reply(entries, "id-1")[0] == ":email: Draft created to Gus."

    # the model gave up on the result and drafted itself: nothing to wait for
    drafted = before[:3] + [
        _assistant_u([{"type": "tool_use", "name": "Bash", "id": "b1", "input": {
            "command": "python3 /x/preprocessor/mail_actions.py create-reply --provider outlook"}}], "a7"),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "{}"}]}}),
        _assistant_u([{"type": "text", "text": ":email: Draft created to Gus."}], "a8"),
    ]
    assert rb.segment_background_pending(rb.parse_entries("\n".join(drafted)), "id-1") is False
    failed = drafted[:4] + [json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b1", "is_error": True, "content": "Exit code 2"}]}})]
    assert rb.segment_background_pending(rb.parse_entries("\n".join(failed)), "id-1") is True

    m2 = rb.make_marker("t2", "id-2")
    entries = rb.parse_entries("\n".join(before + [_user(f"{m2} next ask", "u5")]))
    assert rb.segment_background_pending(entries, "id-1") is True
    assert rb.find_segment_reply(entries, "id-1")[1] is True


def test_marker_folded_into_a_tool_result_still_starts_its_segment():
    """A paste that lands mid-turn is queued by the TUI and attached to the
    next tool result. The dispatch's segment starts there, so its reply
    and its drafts are attributed to it and not to the previous dispatch."""
    m1 = rb.make_marker("t1", "id-1")
    m2 = rb.make_marker("t2", "id-2")
    folded = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": f"<retrieval_status>success</retrieval_status>\n{m2} Kit says: do Y"}]}})
    raw = "\n".join([
        _user(f"{m1} do X"),
        _assistant_u([{"type": "tool_use", "name": "TaskOutput", "input": {"task_id": "a1"}}], "a1"),
        folded,
        _assistant_u([{"type": "text", "text": "Y is done."}], "a2"),
    ])
    entries = rb.parse_entries(raw)
    r1, closed1, _ = rb.find_segment_reply(entries, "id-1")
    r2, closed2, _ = rb.find_segment_reply(entries, "id-2")
    assert r1 is None and closed1 is True
    assert r2 == "Y is done." and closed2 is False


def test_release_inflight_is_scoped_to_the_holder():
    """A late reply frees only the slot its own dispatch left held; a newer
    holder keeps the pane."""
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    old_lock, old_file = rb.SEND_LOCK_FILE, rb.INFLIGHT_FILE
    rb.SEND_LOCK_FILE, rb.INFLIGHT_FILE = d / "send.lock", d / "inflight.json"
    try:
        assert rb.claim_inflight("t-new") is True
        holder_ts = json.loads(rb.INFLIGHT_FILE.read_text())["ts"]
        assert rb.release_inflight("t-old", holder_ts - 100) is False
        assert rb.inflight_busy() is True
        assert rb.release_inflight("t-new", holder_ts - 100) is False, "claimed after the late dispatch"
        assert rb.release_inflight("t-new", holder_ts + 1) is True
        assert rb.inflight_busy() is False
        assert rb.claim_inflight("t-x") is True and rb.release_inflight() is True
    finally:
        rb.SEND_LOCK_FILE, rb.INFLIGHT_FILE = old_lock, old_file


def test_input_holds_sees_a_stranded_paste_only_in_the_input_box():
    head = "[slack-dispatch thread=stream:4:topic:x id=146]"
    assert rb._input_holds("history\n❯ " + head + " more", head)
    assert rb._input_holds("history\n❯ [Pasted text #1 +312 lines]\n⏵⏵ auto", head)
    # the same text above the prompt is scrollback, not the input box
    assert not rb._input_holds("❯ " + head + "\n\nreply\n❯ \n⏵⏵ auto", head)
    assert not rb._input_holds("booting, no prompt yet", head)


def _await_with_transcript(sizes):
    """Run dispatch_and_await with _tsize answering from `sizes` (None =
    no file) and no real waits; returns (error, pending entries)."""
    orig = (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE, rb.ROUTER_STATE_FILE,
            rb.transcript_path, rb.send, rb._tsize, rb.POLL_S, rb.TRANSCRIPT_APPEAR_S,
            rb.time.monotonic)
    clock = iter(range(0, 10_000, 5))
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_pending(Path(td))
            answers = iter(sizes)
            rb.transcript_path = lambda: Path(td) / "t.jsonl"
            rb.send = lambda text: True
            rb._tsize = lambda path: next(answers, None)
            rb.POLL_S = 0
            rb.TRANSCRIPT_APPEAR_S = 20
            rb.time.monotonic = lambda: float(next(clock))
            try:
                rb.dispatch_and_await("text", "146", timeout_s=400,
                                      thread_ts="stream:4:topic:old", channel="stream:4")
                err = None
            except RuntimeError as e:
                err = str(e)
            return err, rb.pending_dispatches()
    finally:
        (rb.PENDING_FILE, rb.SEND_LOCK_FILE, rb.STATE_FILE, rb.ROUTER_STATE_FILE,
         rb.transcript_path, rb.send, rb._tsize, rb.POLL_S, rb.TRANSCRIPT_APPEAR_S,
         rb.time.monotonic) = orig


def test_a_transcript_that_never_appears_leaves_no_pending_dispatch():
    err, pending = _await_with_transcript([None] * 50)
    assert err == "transcript never appeared" and pending == {}


def test_a_transcript_that_vanishes_leaves_no_pending_dispatch():
    err, pending = _await_with_transcript([100, 100, 120, None])
    assert err == "transcript vanished mid-await" and pending == {}



def test_echoed_marker_in_tool_output_does_not_end_the_segment():
    """The model's own tools can print markers (the conversation index
    shows each thread's last dispatch). An echo of this dispatch or an
    older one must not close the segment, or its reply is never found;
    only a newer dispatch does."""
    old = rb.make_marker("stream:4:topic:Other", "2570")
    mine = rb.make_marker("stream:4:topic:Property%20Dashboard", "2583")
    newer = rb.make_marker("stream:4:topic:Next", "2590")
    echo = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": f"asked: {mine} Kit says: All this is closed\n"
                                           f"asked: {old} Kit says: other"}]}})
    raw = "\n".join([
        _user(f"{mine} Kit says:\nAll this is closed"),
        _assistant_u([{"type": "tool_use", "name": "Bash", "input": {}}], "a1"),
        echo,
        _assistant_u([{"type": "text", "text": "Closed both."}], "a2"),
    ])
    entries = rb.parse_entries(raw)
    assert rb.find_segment_reply(entries, "2583")[:2] == ("Closed both.", False)
    assert rb.segment_background_pending(entries, "2583") is False
    raw2 = raw + "\n" + "\n".join([_user(f"{newer} Kit says:\nnext"),
                                    _assistant_u([{"type": "text", "text": "Next."}], "a3")])
    entries2 = rb.parse_entries(raw2)
    assert rb.find_segment_reply(entries2, "2583")[:2] == ("Closed both.", True)
    assert rb.find_segment_reply(entries2, "2590")[:2] == ("Next.", False)


def test_inflight_thread_names_the_holder():
    orig = (rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            _setup_inflight(Path(td))
            assert rb.inflight_thread() == ""
            assert rb.claim_inflight("stream:4:topic:a") is True
            assert rb.inflight_thread() == "stream:4:topic:a"
            rb.release_inflight()
            assert rb.inflight_thread() == ""
    finally:
        rb.INFLIGHT_FILE, rb.SEND_LOCK_FILE = orig


def test_a_dispatch_is_typed_as_the_users_request_before_the_paste():
    """The TUI wraps a long paste as pasted content, and a message that is
    only a paste read as material from elsewhere. A typed lead line comes
    first, then the paste, then Enter."""
    from types import SimpleNamespace
    steps = []

    def run(cmd, **kwargs):
        if "send-keys" in cmd:
            steps.append(("keys", cmd[-1]))
        elif "paste-buffer" in cmd:
            steps.append(("paste", ""))
        return SimpleNamespace(returncode=0, stdout="")
    orig = (rb.SEND_LOCK_FILE, rb._run, rb._tui_ready, rb.time.sleep)
    with tempfile.TemporaryDirectory() as td:
        try:
            rb.SEND_LOCK_FILE = Path(td) / "send.lock"
            rb._run, rb._tui_ready, rb.time.sleep = run, (lambda: True), (lambda _: None)
            assert rb.send("[slack-dispatch id=x] hello")
        finally:
            rb.SEND_LOCK_FILE, rb._run, rb._tui_ready, rb.time.sleep = orig
    assert steps[:3] == [("keys", rb.DISPATCH_LEAD), ("paste", ""), ("keys", "Enter")]


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
