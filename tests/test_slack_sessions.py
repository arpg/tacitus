#!/usr/bin/env python3
"""Tests for preprocessor/slack_sessions.py (session-per-thread registry).

Each Slack thread maps to one persistent Claude session. Covers
lookup() semantics, age expiry, meta persistence (use_mcp),
invalidation after failed resume, and pruning.
"""

import json
import os
import sys
import tempfile
import traceback
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# The registry is read through resident_io, which runs a resident child in
# an owner process; a sandboxed EA_DIR makes this process a resident one,
# so the calls act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-slack-sessions-")

import slack_sessions as ss  # noqa: E402


def _setup(tmp: Path):
    ss.REGISTRY = tmp / "slack-sessions.json"


def test_new_thread_mints_uuid_session():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sid, resumable, meta = ss.lookup("1781000000.000100")
        assert not resumable
        assert meta == {}
        uuid_mod.UUID(sid)  # must be a valid UUID (CLI requirement)
        # registered for next time
        reg = json.loads(ss.REGISTRY.read_text())
        assert reg["1781000000.000100"]["session_id"] == sid


def test_second_lookup_resumes_same_session():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sid1, r1, _ = ss.lookup("t1")
        sid2, r2, _ = ss.lookup("t1")
        assert not r1 and r2
        assert sid1 == sid2


def test_distinct_threads_distinct_sessions():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sid_a, _, _ = ss.lookup("ta")
        sid_b, _, _ = ss.lookup("tb")
        assert sid_a != sid_b


def test_old_session_not_resumed():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sid1, _, _ = ss.lookup("t1")
        reg = json.loads(ss.REGISTRY.read_text())
        old = (datetime.now(timezone.utc)
               - timedelta(days=ss.MAX_AGE_DAYS + 1)).isoformat()
        reg["t1"]["last_used"] = old
        ss.REGISTRY.write_text(json.dumps(reg))
        sid2, resumable, _ = ss.lookup("t1")
        assert not resumable, "stale session must not resume"
        assert sid2 != sid1, "a fresh session id must be minted"


def test_remember_meta_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.lookup("t1")
        ss.remember("t1", use_mcp=True)
        _, resumable, meta = ss.lookup("t1")
        assert resumable
        assert meta.get("use_mcp") is True


def test_invalidate_forces_fresh():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        sid1, _, _ = ss.lookup("t1")
        ss.invalidate("t1")
        sid2, resumable, _ = ss.lookup("t1")
        assert not resumable
        assert sid2 != sid1


def test_mark_used_refreshes_age():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.lookup("t1")
        reg = json.loads(ss.REGISTRY.read_text())
        old = (datetime.now(timezone.utc)
               - timedelta(days=ss.MAX_AGE_DAYS - 1)).isoformat()
        reg["t1"]["last_used"] = old
        ss.REGISTRY.write_text(json.dumps(reg))
        ss.mark_used("t1")
        _, resumable, _ = ss.lookup("t1")
        assert resumable


def test_prune_removes_idle_entries():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.lookup("fresh")
        ss.lookup("stale")
        reg = json.loads(ss.REGISTRY.read_text())
        reg["stale"]["last_used"] = (
            datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        ss.REGISTRY.write_text(json.dumps(reg))
        assert ss.prune(max_age_days=14) == 1
        reg = json.loads(ss.REGISTRY.read_text())
        assert "fresh" in reg and "stale" not in reg


def test_corrupt_registry_recovers():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        ss.REGISTRY.write_text("{corrupt")
        sid, resumable, meta = ss.lookup("t1")
        assert not resumable
        uuid_mod.UUID(sid)


def test_adopt_binds_a_thread_to_an_existing_session():
    """The shell to Slack handoff: a conversation held in the ea-bot
    shell is resumed in a thread by registering its id."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.SESSIONS_DIR = Path(td) / "sessions"
        ss.SESSIONS_DIR.mkdir()
        sid = "d488ed1f-f075-4ddd-bbf2-eb382ad3eb87"
        (ss.SESSIONS_DIR / f"{sid}.jsonl").write_text("{}\n")
        entry = ss.adopt("1788000000.000100", sid.upper(), source="slack",
                         model="claude-fable-5-1")
        assert entry["session_id"] == sid
        assert entry["meta"] == {"adopted_from": "slack", "model": "claude-fable-5-1"}
        got, resumable, meta = ss.lookup("1788000000.000100")
        assert got == sid and resumable and meta["model"] == "claude-fable-5-1"
        assert ss.find_thread(sid) == "1788000000.000100"
        assert ss.describe("1788000000.000100")["session_id"] == sid
        # re-pointing a thread that already had a session records the old one
        old, _, _ = ss.lookup("1788000000.000200")
        entry = ss.adopt("1788000000.000200", sid)
        assert entry["meta"]["replaced"] == old
        assert ss.lookup("1788000000.000200")[0] == sid


def test_adopt_refuses_bad_ids_and_missing_transcripts():
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.SESSIONS_DIR = Path(td) / "sessions"
        ss.SESSIONS_DIR.mkdir()
        for bad in ("", "d488ed1f", "not-a-uuid-at-all-0000-000000000000"):
            try:
                ss.adopt("t", bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted {bad!r}")
        sid = "11111111-2222-3333-4444-555555555555"
        try:
            ss.adopt("t", sid)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("adopted a session with no transcript")
        assert ss.describe("t") is None  # nothing written on refusal
        # --no-check path
        assert ss.adopt("t", sid, require_file=False)["session_id"] == sid


def test_conversation_index_titles_and_recent():
    """Threads are conversations, so they carry names and appear in a
    list."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        t1, _, _ = ss.lookup("1788000001.000100")
        ss.remember("1788000001.000100", channel="C0MAIN",
                    title=ss.title_from_message(
                        "[From the private #ea-x channel, scoped to project 'x'. "
                        "Reply in this thread.]\n\nDraft a reply to Sol with an "
                        "abstract similar to the one I gave to Jules for the workshop"))
        t2, _, _ = ss.lookup("1788000002.000100")
        assert ss.set_title("1788000002.000100", "  Field   strategy  ")
        assert not ss.set_title("nope", "x") and not ss.set_title("1788000002.000100", "")
        rows = ss.recent(limit=5)
        assert [r["thread_ts"] for r in rows] == ["1788000002.000100", "1788000001.000100"]
        assert rows[0]["title"] == "Field strategy"
        assert rows[1]["title"] == "Draft a reply to Sol with an abstract similar to the one I gave to…"
        assert rows[1]["channel"] == "C0MAIN" and rows[0]["session_id"] == t2
        # an idle row falls out of the index after `days`
        reg = json.loads(ss.REGISTRY.read_text())
        reg["1788000001.000100"]["last_used"] = (
            datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        ss.REGISTRY.write_text(json.dumps(reg))
        assert [r["thread_ts"] for r in ss.recent(days=14)] == ["1788000002.000100"]
    assert ss.title_from_message("") == ""
    assert ss.title_from_message("short one") == "short one"


def test_aged_out_thread_keeps_its_old_session_for_resume():
    """A thread past MAX_AGE_DAYS gets a fresh session on its next
    message, and the old id, title and model ride along on the new row so
    `resume this conversation` can re-adopt it."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        ss.SESSIONS_DIR = Path(td) / "sessions"
        ss.SESSIONS_DIR.mkdir()
        old, _, _ = ss.lookup("t-old")
        ss.remember("t-old", title="Field strategy", channel="C0MAIN", model="claude-fable-5-1")
        reg = json.loads(ss.REGISTRY.read_text())
        reg["t-old"]["last_used"] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        ss.REGISTRY.write_text(json.dumps(reg))
        fresh, resumable, meta = ss.lookup("t-old")
        assert not resumable and fresh != old
        assert meta["expired_session_id"] == old
        assert meta["title"] == "Field strategy" and meta["model"] == "claude-fable-5-1"
        assert ss.recent(days=30)[0]["expired_session_id"] == old
        # the resume: adopt the old id back onto the thread
        (ss.SESSIONS_DIR / f"{old}.jsonl").write_text("{}\n")
        ss.adopt("t-old", old, source="resume-thread", model=meta["model"])
        sid, resumable, meta = ss.lookup("t-old")
        assert sid == old and resumable and meta["model"] == "claude-fable-5-1"


def test_registry_cli_round_trip():
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "state").mkdir()
        (d / "sessions").mkdir()
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (d / "sessions" / f"{sid}.jsonl").write_text("{}\n")
        env = dict(EA_DIR=str(d), EA_CLAUDE_SESSIONS_DIR=str(d / "sessions"),
                   PATH="/usr/bin:/bin")
        cli = str(HERE.parent / "preprocessor" / "slack_sessions.py")
        r = subprocess.run([sys.executable, cli, "adopt", "--thread", "1.2",
                            "--session", sid], capture_output=True, text=True, env=env)
        assert r.returncode == 0 and json.loads(r.stdout)["ok"], r.stdout + r.stderr
        r = subprocess.run([sys.executable, cli, "show", "--session", sid],
                           capture_output=True, text=True, env=env)
        assert json.loads(r.stdout)["thread"] == "1.2"
        r = subprocess.run([sys.executable, cli, "show", "--thread", "1.2"],
                           capture_output=True, text=True, env=env)
        assert json.loads(r.stdout)["entry"]["session_id"] == sid
        r = subprocess.run([sys.executable, cli, "adopt", "--thread", "1.3",
                            "--session", "nope"], capture_output=True, text=True, env=env)
        assert r.returncode == 1 and not json.loads(r.stdout)["ok"]



def test_project_dir_name_is_the_cli_folder_name():
    """Claude Code names a transcript folder after the cwd, so ~ea-bot's
    folder is -Users-ea-bot-ea-system on macOS, not the Linux name."""
    assert ss.project_dir_name("/home/ea-bot/ea-system") == "-home-ea-bot-ea-system"
    assert ss.project_dir_name("/Users/ea-bot/ea-system") == "-Users-ea-bot-ea-system"
    assert ss.project_dir_name("/srv/my_bot.v2/ea-system") == "-srv-my-bot-v2-ea-system"


def test_set_session_effort_pins_every_bound_thread():
    """A room's threads carry the room's effort from binding; pinning the
    session's effort updates each of them and no other thread."""
    with tempfile.TemporaryDirectory() as td:
        _setup(Path(td))
        room = str(uuid_mod.uuid4())
        ss.adopt("t-room-1", room, source="project-room", require_file=False, effort="xhigh")
        ss.adopt("t-room-2", room, source="project-room", require_file=False, effort="xhigh")
        other, _r, _m = ss.lookup("t-other")
        assert ss.set_session_effort(room, "high") == 2
        reg = json.loads(ss.REGISTRY.read_text())
        assert reg["t-room-1"]["meta"]["effort"] == "high"
        assert reg["t-room-2"]["meta"]["effort"] == "high"
        assert "effort" not in (reg["t-other"].get("meta") or {})
        assert ss.set_session_effort(room, "high") == 0
        assert ss.set_session_effort("not-a-uuid", "high") == 0


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
