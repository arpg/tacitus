#!/usr/bin/env python3
"""Tests for preprocessor/slack_outbox.py (bot-mediated delivery).

The resident (ea-bot) has no Slack token, so it STAGES jobs and the main bot
DRAINS them with its token. These pin: staging writes the copied file + a
sidecar; drain uploads with the caller-forced channel and cleans up on
success; bad thread_ts / orphan jobs are rejected; failures retry then
dead-letter.

Also pins text-only jobs: a sidecar with no paired file, posted into its
thread by post_fn. That is the only route a cron/plugin/external
invocation has into a THREAD, since incoming webhooks cannot thread.
File jobs behave the same either way.
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
# resident one, so the calls act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-outbox-")

import slack_outbox  # noqa: E402

SC = slack_outbox.SIDECAR_SUFFIX


def _mkfile(d: Path, name="pic.png", data=b"\x89PNG\r\n\x1a\nDATA") -> Path:
    p = d / name
    p.write_bytes(data)
    return p


class _Recorder:
    """Fake upload_fn / post_fn; records kwargs, fails the first N calls."""

    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    def __call__(self, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("simulated upload failure")


def test_stage_writes_file_and_sidecar():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base, "meme.png")
        staged = slack_outbox.stage(str(src), "1783005489.654719",
                                    comment="hi", title="My Meme", base=base)
        assert len(staged) == 1
        outbox = slack_outbox.outbox_dir(base)
        sidecars = list(outbox.glob("*" + SC))
        assert len(sidecars) == 1
        meta = json.loads(sidecars[0].read_text())
        assert meta["thread_ts"] == "1783005489.654719"
        assert meta["comment"] == "hi"
        assert meta["title"] == "My Meme"
        assert meta["original_name"] == "meme.png"
        paired = Path(str(sidecars[0])[:-len(SC)])
        assert paired.is_file()
        assert paired.read_bytes() == src.read_bytes()  # true copy


def test_stage_rejects_bad_thread_ts():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base)
        for bad in ("", "next monday", "1783005489]", "-abc", "a" * 401):
            try:
                slack_outbox.stage(str(src), bad, base=base)
                assert False, f"should have rejected thread_ts {bad!r}"
            except ValueError:
                pass


def test_stage_rejects_missing_file():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        try:
            slack_outbox.stage(str(base / "nope.png"), "1.2", base=base)
            assert False, "should have rejected a missing file"
        except ValueError:
            pass


def test_drain_uploads_forced_channel_and_cleans_up():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base, "meme.png")
        slack_outbox.stage(str(src), "1783005489.654719", comment="hi", base=base)
        rec = _Recorder()
        results = slack_outbox.drain(rec, channel="C_FORCED", base=base)
        assert len(results) == 1 and results[0]["status"] == "sent"
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["channel"] == "C_FORCED"              # forced by the caller
        assert call["thread_ts"] == "1783005489.654719"
        assert call["initial_comment"] == "hi"
        assert call["filename"] == "meme.png"
        assert Path(call["file"]).name.endswith("meme.png")
        outbox = slack_outbox.outbox_dir(base)
        assert list(outbox.glob("*" + SC)) == []          # cleaned up on success


def test_drain_empty_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        assert slack_outbox.drain(_Recorder(), channel="C", base=Path(td)) == []


def test_drain_retries_then_deadletters():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base, "meme.png")
        slack_outbox.stage(str(src), "1.2", base=base)
        rec = _Recorder(fail_times=99)  # always fails
        outbox = slack_outbox.outbox_dir(base)
        r1 = slack_outbox.drain(rec, channel="C", base=base, max_attempts=3)
        assert r1[0]["status"] == "retry"
        assert list(outbox.glob("*" + SC))                # still queued
        slack_outbox.drain(rec, channel="C", base=base, max_attempts=3)  # attempt 2
        r3 = slack_outbox.drain(rec, channel="C", base=base, max_attempts=3)
        assert r3[0]["status"] == "failed"
        assert list(outbox.glob("*" + SC)) == []          # removed from queue
        assert (outbox / "failed").is_dir()
        assert list((outbox / "failed").glob("*.png"))    # dead-lettered file kept


def test_drain_counts_attempts_on_a_read_only_sidecar():
    """The resident stages the sidecar and the bot drains it as another
    user: the count is rewritten by rename, which needs only the directory."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (s,) = slack_outbox.stage(str(_mkfile(base)), "1.2", base=base)
        sidecar = Path(s["sidecar"])
        sidecar.chmod(0o440)
        slack_outbox.drain(_Recorder(fail_times=99), channel="C", base=base)
        assert json.loads(sidecar.read_text())["attempts"] == 1


def test_drain_deadletters_when_the_sidecar_cannot_be_rewritten():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        slack_outbox.stage(str(_mkfile(base)), "1.2", base=base)
        outbox = slack_outbox.outbox_dir(base)
        real = slack_outbox._write_sidecar

        def refuse(sidecar, meta):
            raise PermissionError("not yours")

        slack_outbox._write_sidecar = refuse
        try:
            rec = _Recorder(fail_times=99)
            statuses = [slack_outbox.drain(rec, channel="C", base=base,
                                           max_attempts=3)[0]["status"]
                        for _ in range(3)]
        finally:
            slack_outbox._write_sidecar = real
        assert statuses == ["retry", "retry", "failed"]
        assert list(outbox.glob("*" + SC)) == []
        assert len(rec.calls) == 3


def test_drain_orphan_sidecar_dropped():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        outbox = slack_outbox.outbox_dir(base)
        outbox.mkdir(parents=True)
        (outbox / ("orphan.png" + SC)).write_text(
            json.dumps({"thread_ts": "1.2", "original_name": "orphan.png"}))
        rec = _Recorder()
        results = slack_outbox.drain(rec, channel="C", base=base)
        assert results and results[0]["status"] == "dropped"
        assert rec.calls == []                            # never attempted
        assert list(outbox.glob("*" + SC)) == []


def test_drain_file_result_still_reports_kind_file():
    """Backward compat: a sidecar with no `kind` is a FILE job, and the
    result keeps its `file` name (the bot logs it)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base, "meme.png")
        slack_outbox.stage(str(src), "1.2", base=base)
        sidecar = list(slack_outbox.outbox_dir(base).glob("*" + SC))[0]
        assert "kind" not in json.loads(sidecar.read_text())
        results = slack_outbox.drain(_Recorder(), channel="C", base=base)
        assert results[0]["kind"] == "file"
        assert results[0]["file"].endswith("meme.png")


# ---------------------------------------------------------------------------
# Text-only jobs: the thread route for out-of-dispatch callers
# ---------------------------------------------------------------------------

def test_stage_text_writes_a_lone_sidecar():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        job = slack_outbox.stage_text("  cron finished  ",
                                      "1783005489.654719", base=base)
        outbox = slack_outbox.outbox_dir(base)
        sidecars = list(outbox.glob("*" + SC))
        assert len(sidecars) == 1 and job["sidecar"] == str(sidecars[0])
        meta = json.loads(sidecars[0].read_text())
        assert meta["kind"] == "text"
        assert meta["comment"] == "cron finished"        # stripped
        assert meta["thread_ts"] == "1783005489.654719"
        paired = Path(str(sidecars[0])[:-len(SC)])
        assert not paired.exists()                       # no file to hang on


def test_stage_text_rejects_bad_thread_ts_and_empty_text():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for bad in ("", "next monday", "1783005489]", "-abc", "a" * 401):
            try:
                slack_outbox.stage_text("hi", bad, base=base)
                assert False, f"should have rejected thread_ts {bad!r}"
            except ValueError:
                pass
        for empty in ("", "   ", None):
            try:
                slack_outbox.stage_text(empty, "1.2", base=base)
                assert False, f"should have rejected text {empty!r}"
            except ValueError:
                pass
        assert list(slack_outbox.outbox_dir(base).glob("*" + SC)) == []


def test_stage_text_twice_does_not_collide():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        a = slack_outbox.stage_text("first", "1.2", base=base)
        b = slack_outbox.stage_text("second", "1.2", base=base)
        assert a["sidecar"] != b["sidecar"]
        assert len(list(slack_outbox.outbox_dir(base).glob("*" + SC))) == 2


def test_drain_posts_text_job_into_its_thread():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        slack_outbox.stage_text("poll filled", "1783005489.654719", base=base)
        up, post = _Recorder(), _Recorder()
        results = slack_outbox.drain(up, channel="C_FORCED", post_fn=post,
                                     base=base)
        assert len(results) == 1
        assert results[0]["status"] == "sent"
        assert results[0]["kind"] == "text" and results[0]["file"] is None
        assert up.calls == []                            # never an upload
        assert post.calls == [{"channel": "C_FORCED",    # channel forced
                               "thread_ts": "1783005489.654719",
                               "text": "poll filled"}]
        assert list(slack_outbox.outbox_dir(base).glob("*" + SC)) == []


def test_drain_text_job_without_post_fn_is_dropped():
    """A caller wired only for files must not spin on a text job forever."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        slack_outbox.stage_text("orphaned text", "1.2", base=base)
        rec = _Recorder()
        results = slack_outbox.drain(rec, channel="C", base=base)
        assert results[0]["status"] == "dropped"
        assert results[0]["kind"] == "text"
        assert rec.calls == []                           # never uploaded
        outbox = slack_outbox.outbox_dir(base)
        assert list(outbox.glob("*" + SC)) == []
        assert list((outbox / "failed").glob("*" + SC))   # kept for forensics


def test_drain_handles_file_and_text_jobs_together():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base, "receipt.png")
        slack_outbox.stage(str(src), "1.2", comment="here", base=base)
        slack_outbox.stage_text("and a note", "1.2", base=base)
        up, post = _Recorder(), _Recorder()
        results = slack_outbox.drain(up, channel="C", post_fn=post, base=base)
        assert len(results) == 2
        assert {r["status"] for r in results} == {"sent"}
        assert {r["kind"] for r in results} == {"file", "text"}
        assert len(up.calls) == 1 and len(post.calls) == 1
        assert up.calls[0]["filename"] == "receipt.png"
        assert post.calls[0]["text"] == "and a note"
        assert list(slack_outbox.outbox_dir(base).glob("*" + SC)) == []


def test_drain_text_retries_then_deadletters():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        slack_outbox.stage_text("flaky", "1.2", base=base)
        post = _Recorder(fail_times=99)
        outbox = slack_outbox.outbox_dir(base)
        r1 = slack_outbox.drain(_Recorder(), channel="C", post_fn=post,
                                base=base, max_attempts=2)
        assert r1[0]["status"] == "retry" and r1[0]["kind"] == "text"
        assert list(outbox.glob("*" + SC))               # still queued
        r2 = slack_outbox.drain(_Recorder(), channel="C", post_fn=post,
                                base=base, max_attempts=2)
        assert r2[0]["status"] == "failed"
        assert list(outbox.glob("*" + SC)) == []
        assert list((outbox / "failed").glob("*" + SC))  # text kept on disk


def test_drain_text_sidecar_with_empty_body_dropped():
    """Hand-written / truncated sidecar: never post an empty message."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        outbox = slack_outbox.outbox_dir(base)
        outbox.mkdir(parents=True)
        (outbox / ("1.message" + SC)).write_text(
            json.dumps({"kind": "text", "thread_ts": "1.2", "comment": "  "}))
        post = _Recorder()
        results = slack_outbox.drain(_Recorder(), channel="C", post_fn=post,
                                     base=base)
        assert results[0]["status"] == "dropped"
        assert post.calls == []
        assert list(outbox.glob("*" + SC)) == []


def test_stage_accepts_an_imported_thread_key():
    """A thread the web chat imported from Zulip keeps its old key, and the
    bot posts there under the old channel id, which the web chat redirects."""
    key = "stream:12:topic:Hello%20there"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = _mkfile(base)
        staged = slack_outbox.stage(str(src), key, base=base)
        assert len(staged) == 1
        job = slack_outbox.stage_text("hi", key, base=base)
        assert json.loads(Path(job["sidecar"]).read_text())["thread_ts"] == key
        rec, post = _Recorder(), _Recorder()
        results = slack_outbox.drain(rec, channel="stream:12", post_fn=post,
                                     base=base)
        assert sorted(r["status"] for r in results) == ["sent", "sent"], results
        assert rec.calls[0]["thread_ts"] == key
        assert post.calls[0]["thread_ts"] == key


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
