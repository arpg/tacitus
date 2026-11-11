#!/usr/bin/env python3
"""Tests for preprocessor/resident_context.py (smart dispatch selection).

The primer, skills and scheduling rules are not re-injected on every
resident dispatch. Primer once per session;
skills+scheduling gated per (session, thread); briefing extract always;
new-vs-continuation decided by the session-keyed tracker (NOT Slack's
is_thread_reply); rotation (session_id change) auto-resets.
"""

import json
import os
import stat
import sys
import tempfile
import traceback
from pathlib import Path

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, and resident_io then reads through the resident.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-resident-context-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import resident_context as rc  # noqa: E402

BRIEF = "[Current obligations data matched ...]\n- thing about Ash"
SKILL = "## Skill: reply\nDraft a reply ..."
SCHED = "SCHEDULING RULES (authoritative): use propose-meeting ..."


def _setup(tmp: Path):
    rc.STATE_FILE = tmp / "resident-context.json"


def _new_thread(session_id="s-1", thread_ts="t1", **kw):
    return rc.build_dispatch(
        user_msg="do the thing", thread_ts=thread_ts, msg_ts="m1",
        session_id=session_id, briefing_blurb=BRIEF, skill_blurb=SKILL,
        detected_skills=["reply"], scheduling_ctx=SCHED,
        needed_modules={"scheduling"}, **kw)


def test_fresh_session_new_thread_full_context():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        text, debug = _new_thread()
        assert rc.PRIMER[:40] in text
        assert "em-dash" in text.lower() or "STYLE" in text or "voice" in text.lower()
        assert "Ash" in text                       # briefing always
        assert "SCHEDULING RULES" in text             # scheduling injected
        assert "Skill: reply" in text                 # skill injected
        assert debug["primer_injected"] is True
        assert "primer" in debug["injected"]
        assert "scheduling" in debug["injected"]
        assert any(k.startswith("skills:") for k in debug["injected"])


def test_continuation_is_lean():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        text1, debug1 = _new_thread()
        rc.note_dispatched("s-1", "t1", debug1["injected"])
        # second dispatch, same session + thread
        text2, debug2 = rc.build_dispatch(
            user_msg="and also this", thread_ts="t1", msg_ts="m2",
            session_id="s-1", briefing_blurb=BRIEF, skill_blurb=SKILL,
            detected_skills=["reply"], scheduling_ctx=SCHED,
            needed_modules={"scheduling"})
        assert rc.PRIMER[:40] not in text2            # primer NOT re-sent
        assert "Skill: reply" not in text2            # skill NOT re-sent
        assert "SCHEDULING RULES" not in text2        # scheduling NOT re-sent
        assert "Ash" in text2                      # briefing STILL sent
        assert rc.GUARDRAIL in text2                  # guardrail always
        assert debug2["is_continuation"] is True
        assert debug2["injected"] == []
        assert debug2["bytes"] < debug1["bytes"] // 2


def test_reply_to_unseen_thread_gets_full_context():
    # Slack 'reply' but the resident session has never seen this thread
    # (e.g., post-rotation). Tracker, not is_thread_reply, decides.
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        _, d1 = _new_thread(thread_ts="t1")
        rc.note_dispatched("s-1", "t1", d1["injected"])
        text, debug = rc.build_dispatch(
            user_msg="reply on a different thread", thread_ts="t2",
            msg_ts="m9", session_id="s-1", briefing_blurb=BRIEF,
            skill_blurb=SKILL, detected_skills=["reply"], scheduling_ctx=SCHED,
            needed_modules={"scheduling"})
        # primer already done for the session, but skills+scheduling are
        # per-thread and t2 is new -> they ARE injected; primer is NOT.
        assert rc.PRIMER[:40] not in text
        assert "Skill: reply" in text
        assert "SCHEDULING RULES" in text
        assert "scheduling" in debug["injected"]


def test_rotation_resets_everything():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        _, d1 = _new_thread(session_id="s-1", thread_ts="t1")
        rc.note_dispatched("s-1", "t1", d1["injected"])
        # session_id changes (rotation)
        text, debug = _new_thread(session_id="s-2", thread_ts="t1")
        assert rc.PRIMER[:40] in text                 # primer re-injected
        assert debug["primer_injected"] is True
        assert "primer" in debug["injected"]
        assert "scheduling" in debug["injected"]      # thread "unseen" again


def test_session_id_none_injects_primer_no_write():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        text, debug = _new_thread(session_id=None)
        assert rc.PRIMER[:40] in text
        # primer included but NOT recorded (can't track without a session)
        assert "primer" not in debug["injected"]
        rc.note_dispatched(None, "t1", debug["injected"])  # must not write
        assert not rc.STATE_FILE.exists()


def test_empty_briefing_omitted_cleanly():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        text, _ = rc.build_dispatch(
            user_msg="hi", thread_ts="t1", msg_ts="m1", session_id="s-1",
            briefing_blurb="", skill_blurb="", detected_skills=[],
            scheduling_ctx="", needed_modules=set())
        assert "Current obligations" not in text
        assert "\n\n\n" not in text                   # no empty blocks


def test_skill_set_expansion_reinjects():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        _, d1 = rc.build_dispatch(
            user_msg="reply", thread_ts="t1", msg_ts="m1", session_id="s-1",
            briefing_blurb="", skill_blurb="## reply", detected_skills=["reply"],
            scheduling_ctx="", needed_modules=set())
        rc.note_dispatched("s-1", "t1", d1["injected"])
        # same thread, now reply+compose-letter detected -> new skill key
        text, debug = rc.build_dispatch(
            user_msg="reply and write a letter", thread_ts="t1", msg_ts="m2",
            session_id="s-1", briefing_blurb="",
            skill_blurb="## reply\n## compose-letter",
            detected_skills=["reply", "compose-letter"], scheduling_ctx="",
            needed_modules=set())
        assert "compose-letter" in text
        assert any(k.startswith("skills:") for k in debug["injected"])


def test_thread_seen_tracks_ownership():
    """thread_seen drives the concurrency router: owned thread -> queue,
    unseen -> spill. Must key on session_id (resets on rotation)."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        assert rc.thread_seen("s-1", "t1") is False        # never dispatched
        _, d = _new_thread(session_id="s-1", thread_ts="t1")
        rc.note_dispatched("s-1", "t1", d["injected"])
        assert rc.thread_seen("s-1", "t1") is True          # now owned
        assert rc.thread_seen("s-1", "t2") is False         # other thread
        assert rc.thread_seen("s-2", "t1") is False         # post-rotation
        assert rc.thread_seen(None, "t1") is False          # unknown session


def test_note_dispatched_reset_across_sessions():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rc.note_dispatched("A", "t1", ["primer", "scheduling"])
        a = rc._load_state("A")
        assert a["primer_injected"] is True
        assert "scheduling" in a["threads"]["t1"]
        # asking for session B yields fresh state
        assert rc._load_state("B")["threads"] == {}
        rc.note_dispatched("B", "t1", ["primer"])
        b = json.loads(rc.STATE_FILE.read_text())
        assert b["session_id"] == "B"
        assert "t1" in b["threads"]  # B's own thread record
        assert b["threads"]["t1"] == []  # only primer (not a thread key)



# ── the tracker read follows the owner trust rules ────────────────────

def _decoy(home: Path, mark: str) -> Path:
    """A file only the owner should change, shaped like a tracker whose
    records carry `mark`."""
    decoy = home / "decoy.json"
    decoy.write_text(json.dumps({"session_id": "s-1", "primer_injected": True,
                                 "threads": {"t1": [mark]}}))
    os.chmod(decoy, 0o600)
    return decoy


def _files_holding(root: Path, mark: str, decoy: Path) -> list:
    """Regular files under `root`, other than the decoy's own inode, that
    hold `mark`."""
    own = os.stat(decoy)
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino):
                continue
            if mark.encode() in p.read_bytes():
                hits.append(p.name)
    return hits


def _link_case(kind: str):
    """A link of `kind` at the tracker name, to a decoy outside the state
    dir: nothing is read through it, nothing is written over it, and the
    decoy's records reach no file in the state dir."""
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as owner:
        _setup(Path(td))
        mark = f"decoy-{kind}-record"
        decoy = _decoy(Path(owner), mark)
        before = (decoy.read_bytes(), os.stat(decoy).st_mtime_ns)
        if kind == "symlink":
            rc.STATE_FILE.symlink_to(decoy)
        else:
            os.link(decoy, rc.STATE_FILE)
        assert rc.thread_seen("s-1", "t1") is False
        text, debug = _new_thread(session_id="s-1", thread_ts="t1")
        assert rc.PRIMER[:40] in text and "primer" in debug["injected"]
        rc.note_dispatched("s-1", "t2", debug["injected"])
        assert (decoy.read_bytes(), os.stat(decoy).st_mtime_ns) == before
        assert _files_holding(Path(td), mark, decoy) == []
        assert os.path.lexists(rc.STATE_FILE)   # an unreadable tracker is kept


def test_a_symlink_at_the_tracker_name_is_not_followed():
    _link_case("symlink")


def test_a_hard_link_at_the_tracker_name_is_refused():
    _link_case("hardlink")


def test_every_tracker_read_goes_through_resident_io():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rc.note_dispatched("s-1", "t1", ["primer", "scheduling"])
        real = rc._rio.read_bytes
        seen = []

        def spy(path):
            seen.append(Path(path))
            return real(path)

        rc._rio.read_bytes = spy
        try:
            assert rc.thread_seen("s-1", "t1") is True
            _new_thread(session_id="s-1", thread_ts="t1")
            rc.note_dispatched("s-1", "t1", [])
        finally:
            rc._rio.read_bytes = real
        assert seen == [rc.STATE_FILE] * 3


def test_only_the_tracker_shape_is_kept():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rc.STATE_FILE.write_text(json.dumps({
            "session_id": "s-1", "primer_injected": "yes",
            "threads": {"t1": ["scheduling", 5, {"k": "v"}], "t2": "junk", "t3": []},
            "renamed": {"t3": "t0", "t4": 7},
            "extra": "carried-field"}))
        state = rc._load_state("s-1")
        assert state == {"session_id": "s-1", "primer_injected": False,
                         "threads": {"t1": ["scheduling"], "t3": []}}
        rc.note_dispatched("s-1", "t1", ["primer"])
        stored = json.loads(rc.STATE_FILE.read_text())
        assert "extra" not in stored and "carried-field" not in rc.STATE_FILE.read_text()
        assert stored["primer_injected"] is True
        assert stored["threads"] == {"t1": ["scheduling"], "t3": []}


def test_a_tracker_that_could_not_be_read_is_kept():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        rc.note_dispatched("s-1", "t1", ["primer", "scheduling"])
        before = rc.STATE_FILE.read_bytes()
        real = rc._rio.read_bytes
        rc._rio.read_bytes = lambda path: None
        try:
            assert rc.thread_seen("s-1", "t1") is False    # reads as fresh
            rc.note_dispatched("s-1", "t2", ["scheduling"])
        finally:
            rc._rio.read_bytes = real
        assert rc.STATE_FILE.read_bytes() == before        # records kept
        assert rc.thread_seen("s-1", "t1") is True


def test_content_that_is_not_a_tracker_is_replaced():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        for junk in ("[1, 2]", "{not json", "\xff\xfe"):
            rc.STATE_FILE.write_bytes(junk.encode("latin-1"))
            assert rc.thread_seen("s-1", "t1") is False
            rc.note_dispatched("s-1", "t1", ["scheduling"])
            assert rc.thread_seen("s-1", "t1") is True


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
