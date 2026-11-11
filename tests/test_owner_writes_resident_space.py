#!/usr/bin/env python3
"""Owner processes write into the resident's space only with its access.

The resident (ea-bot) owns every directory under ~/.ea, so it can put a
symlink or a hard link at any name there. An owner process (the bot, the
mediator's child, the crons) that appends to, rewrites, creates or renames an
entry there by path could be steered onto a file only the owner may change.
These tests pin the converted writers and readers to resident_io, with its
test seams standing in for the sudo child (the real child program runs as the
test user), and each one fails on the earlier direct write:

  * the draft journal, its cursor lock and its prune
  * the draft-capture store and its one-child listing
  * the channel registry
  * the quiet-hours queue, hour overrides and last-run record
  * the outbox dead-letter and sidecar read, and the owner staging refusal
  * the resident bridge's private state, locks and validated legacy copy
  * the context-export attachment names and workspace check
  * the chat frontends' downloads
  * the bot's claims, private state, in-flight record and prompt reads
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

# A sandboxed EA tree and private dir: nothing here touches live state, and
# resident_io's move() takes the sandbox as the resident's space.
EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-ownerwrites-"))
PRIV = Path(tempfile.mkdtemp(prefix="ea-test-ownerwrites-priv-"))
os.environ["EA_DIR"] = str(EA_DIR)
# Only while the modules below import (resident_bridge binds its private
# paths then); put back after, so no other test file sees this sandbox.
_PRIV_BEFORE = os.environ.get("EA_PRIVATE_DIR")
os.environ["EA_PRIVATE_DIR"] = str(PRIV)
for _sub in ("config", "state", "logs", "uploads", "exports"):
    (EA_DIR / _sub).mkdir(parents=True, exist_ok=True)

import resident_io as rio  # noqa: E402
import draft_events as de  # noqa: E402
import draft_capture as dc  # noqa: E402
import ea_channels  # noqa: E402
import quiet_hours as qh  # noqa: E402
import slack_outbox  # noqa: E402
import resident_bridge as rb  # noqa: E402
import context_export as cx  # noqa: E402
from chat import slack_frontend as sf  # noqa: E402
from chat.types import FileRef  # noqa: E402

import pytest
from conftest import NEEDS_RESIDENT, resident_child_available

# The resident child needs the two-user install; a bare runner skips.
pytestmark = pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)

if _PRIV_BEFORE is None:
    os.environ.pop("EA_PRIVATE_DIR", None)
else:
    os.environ["EA_PRIVATE_DIR"] = _PRIV_BEFORE

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
VICTIM_TEXT = '{"owner": "only"}\n'


# ── seams ─────────────────────────────────────────────────────────────

def _pipes(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))


@contextmanager
def _as_owner(calls: list | None = None):
    """resident_io decides this is an owner process with the child allowed;
    the child program runs as the test user and each argv is recorded."""
    calls = [] if calls is None else calls
    saved = (rio._owner_check, rio._availability, rio._spawn)
    rio._owner_check = lambda: True
    rio._availability = lambda: True

    def spawn(argv):
        calls.append(list(argv))
        return _pipes(argv)

    rio._spawn = spawn
    try:
        yield calls
    finally:
        rio._owner_check, rio._availability, rio._spawn = saved


def _ops(calls: list) -> list:
    """(op, path) for each resident_io child call (argv: python -I -c
    <program> <op> <mode> <path> ...)."""
    return [(c[4], c[6]) for c in calls if len(c) > 6 and c[3] == rio._CHILD]


def _victim(tmp: Path, name: str = "victim.json", mode: int = 0o600) -> Path:
    v = tmp / name
    v.write_text(VICTIM_TEXT)
    os.chmod(v, mode)
    return v


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=EA_DIR / "state"))


# ── draft_events ──────────────────────────────────────────────────────

def test_journal_append_never_follows_a_link():
    tmp = _tmpdir()
    victim = _victim(tmp)
    saved = de.JOURNAL
    try:
        de.JOURNAL = tmp / "draft-events.jsonl"
        de.JOURNAL.symlink_to(victim)
        with _as_owner() as calls:
            de.record("created", "AAMkLINK=", verb="create-draft")
        assert victim.read_text() == VICTIM_TEXT, "the append went through the link"
        assert ("append", str(de.JOURNAL)) in _ops(calls)
        de.JOURNAL.unlink()
        with _as_owner():
            de.record("created", "AAMkPLAIN=", verb="create-draft")
        assert [e["draft_id"] for e in de.read_since(0)] == ["AAMkPLAIN="]
    finally:
        de.JOURNAL = saved


def test_journal_lock_is_opened_read_only_and_never_through_a_link():
    tmp = _tmpdir()
    saved = de.JOURNAL
    try:
        de.JOURNAL = tmp / "draft-events.jsonl"
        target = tmp / "made-through-the-link"
        de._lock_path().symlink_to(target)
        with _as_owner():
            de.record("created", "AAMk1=")
            assert [e["draft_id"] for e in de.claim_since(0)] == ["AAMk1="]
        assert not target.exists(), "the lock open followed the link"
        de._lock_path().unlink()
        with _as_owner() as calls:
            de.claim_since(0)
        assert de._lock_path().is_file() and not de._lock_path().is_symlink()
        assert ("touch", str(de._lock_path())) in _ops(calls)
    finally:
        de.JOURNAL = saved


def test_journal_prune_and_cursor_go_through_the_resident():
    tmp = _tmpdir()
    saved = (de.JOURNAL, de.MAX_BYTES)
    try:
        de.JOURNAL = tmp / "draft-events.jsonl"
        de.MAX_BYTES = 400
        with _as_owner() as calls:
            for i in range(8):
                de.record("created", f"AAMk{i}=")
        ops = [op for op, _ in _ops(calls)]
        assert "update" in ops and "replace" in ops, ops
        left = [e["draft_id"] for e in de.read_since(0)]
        assert left and left[-1] == "AAMk7=" and "AAMk0=" not in left
        assert de._read_cursor() == de.offset()
    finally:
        de.JOURNAL, de.MAX_BYTES = saved


# ── draft_capture ─────────────────────────────────────────────────────

BODY = "Hi Sam,\n\n" + " ".join(["The schedule holds for next week."] * 6) + "\n\nKit"


def test_capture_replaces_a_link_at_its_name_instead_of_writing_through_it():
    tmp = _tmpdir()
    victim = _victim(tmp)
    saved = dc.STORE
    try:
        dc.STORE = tmp / "draft-bodies"
        dc.STORE.mkdir()
        path = dc.STORE / f"{dc._key('D1')}.json"
        path.symlink_to(victim)
        with _as_owner() as calls:
            dc.capture("D1", BODY, conversation_id="C1")
        assert victim.read_text() == VICTIM_TEXT
        assert path.is_file() and not path.is_symlink()
        assert json.loads(path.read_text())["body"] == BODY
        assert ("replace", str(path)) in _ops(calls)
    finally:
        dc.STORE = saved


def test_capture_listing_is_one_resident_child_and_skips_links():
    tmp = _tmpdir()
    victim = tmp / "victim.json"
    victim.write_text(json.dumps({"draft_id": "OWNER", "body": "secret",
                                  "conversation_id": "C1", "captured_at": "z"}))
    saved = dc.STORE
    try:
        dc.STORE = tmp / "draft-bodies"
        with _as_owner():
            for n in ("D1", "D2", "D3"):
                dc.capture(n, BODY, conversation_id="C1")
        (dc.STORE / "planted.json").symlink_to(victim)
        with _as_owner() as calls:
            recs = dc.load_all()
            ids = dc.ids_for_conversation("C1")
        assert sorted(r["draft_id"] for r in recs) == ["D1", "D2", "D3"]
        assert ids == {"D1", "D2", "D3"}
        dumps = [c for c in calls if len(c) > 3 and c[3] == dc._DUMP_CHILD]
        assert len(dumps) == 2 and len(calls) == 2, "one child per listing"
    finally:
        dc.STORE = saved


# ── ea_channels ───────────────────────────────────────────────────────

def test_channel_registry_refuses_a_link_and_leaves_its_target_alone():
    tmp = _tmpdir()
    victim = _victim(tmp, mode=0o600)
    reg = tmp / "ea-channels.json"
    reg.symlink_to(victim)
    with _as_owner():
        try:
            ea_channels.register("C0123456789", "proj-a", "ea-proj-a", path=reg)
        except OSError:
            pass
        else:
            raise AssertionError("a registration through a link must fail")
    assert victim.read_text() == VICTIM_TEXT
    assert (victim.stat().st_mode & 0o777) == 0o600, "the mode was changed through the link"


def test_channel_registry_updates_in_place_through_the_resident():
    tmp = _tmpdir()
    reg = tmp / "ea-channels.json"
    reg.write_text(json.dumps({"_doc": "kept"}))
    with _as_owner() as calls:
        ea_channels.register("C0123456789", "proj-a", "ea-proj-a", path=reg)
        assert ea_channels.rename("C0123456789", "ea-renamed", path=reg)
        assert ea_channels.scope_of("C0123456789", path=reg)["name"] == "ea-renamed"
    ops = [op for op, _ in _ops(calls)]
    assert ops.count("update") == 2 and "read" in ops, ops
    data = json.loads(reg.read_text())
    assert data["_doc"] == "kept" and data["C0123456789"]["project"] == "proj-a"


# ── quiet_hours ───────────────────────────────────────────────────────

@contextmanager
def _quiet_paths(tmp: Path):
    saved = (qh.QUEUE_FILE, qh.HOUR_OVERRIDE_FILE, qh.TRAVEL_STATE_FILE,
             qh.LAST_RUN_FILE, qh.PREFS_FILE)
    qh.QUEUE_FILE = tmp / "deferred-posts.json"
    qh.HOUR_OVERRIDE_FILE = tmp / "agent-hour-overrides.json"
    qh.TRAVEL_STATE_FILE = tmp / "travel-mode.json"
    qh.LAST_RUN_FILE = tmp / "agent-last-run.json"
    qh.PREFS_FILE = tmp / "scheduling-preferences.json"
    qh.PREFS_FILE.write_text(json.dumps({"timezone": "America/Denver"}))
    try:
        yield
    finally:
        (qh.QUEUE_FILE, qh.HOUR_OVERRIDE_FILE, qh.TRAVEL_STATE_FILE,
         qh.LAST_RUN_FILE, qh.PREFS_FILE) = saved


def test_quiet_hours_writes_never_follow_a_link():
    from datetime import datetime, timezone
    tmp = _tmpdir()
    with _quiet_paths(tmp), _as_owner():
        for name in ("agent-hour-overrides.json", "deferred-posts.json"):
            (tmp / name).symlink_to(_victim(tmp, name + ".victim"))
        try:
            qh.set_hour_override("morning-briefing", "2026-10-01", 6)
        except OSError:
            pass
        try:
            qh.defer("ea-assistant", "held", "normal")
        except OSError:
            pass
        qh.LAST_RUN_FILE.symlink_to(_victim(tmp, "last-run.victim"))
        four_am = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)   # 04:00 MDT
        assert qh.due_local("morning-briefing", 4, now=four_am) is True
    for name in ("agent-hour-overrides.json.victim", "deferred-posts.json.victim",
                 "last-run.victim"):
        assert (tmp / name).read_text() == VICTIM_TEXT, name
    assert not qh_last_run_is_link(tmp)


def qh_last_run_is_link(tmp: Path) -> bool:
    return (tmp / "agent-last-run.json").is_symlink()


def test_a_post_deferred_while_the_digest_posts_is_kept():
    tmp = _tmpdir()
    with _quiet_paths(tmp), _as_owner():
        qh.defer("ea-assistant", "first", "normal")
        orig = qh.slack_notifier.post_to_slack

        def post(channel, text, **kw):
            qh.defer("ea-assistant", "arrived mid-flush", "normal")
            return True

        qh.slack_notifier.post_to_slack = post
        try:
            assert qh.flush_if_clear(force=True) == 1
        finally:
            qh.slack_notifier.post_to_slack = orig
        assert [i["text"] for i in qh._load_queue()] == ["arrived mid-flush"]


# ── slack_outbox ──────────────────────────────────────────────────────

def test_an_owner_process_does_not_stage():
    tmp = _tmpdir()
    src = tmp / "a.png"
    src.write_bytes(b"PNG")
    with _as_owner():
        for call in (lambda: slack_outbox.stage(str(src), "1.2", base=tmp),
                     lambda: slack_outbox.stage_text("hi", "1.2", base=tmp)):
            try:
                call()
            except ValueError:
                continue
            raise AssertionError("an owner process staged into the outbox")


def test_the_bot_dead_letters_with_the_resident_access():
    tmp = _tmpdir()
    src = tmp / "a.png"
    src.write_bytes(b"PNG")
    slack_outbox.stage(str(src), "1.2", base=tmp)

    def failing(**kw):
        raise RuntimeError("upload failed")

    with _as_owner() as calls:
        res = slack_outbox.drain(failing, channel="C", base=tmp, max_attempts=1)
    assert res[0]["status"] == "failed"
    ops = [op for op, _ in _ops(calls)]
    assert "mkdir" in ops and ops.count("move") == 2, ops
    failed = slack_outbox.outbox_dir(tmp) / "failed"
    assert len(list(failed.iterdir())) == 2


def test_a_linked_sidecar_is_never_posted():
    tmp = _tmpdir()
    victim = tmp / "victim.json"
    victim.write_text(json.dumps({"kind": "text", "thread_ts": "1.2",
                                  "comment": "owner-only text"}))
    out = slack_outbox.outbox_dir(tmp)
    out.mkdir(parents=True)
    name = f"1-1-0.message{slack_outbox.SIDECAR_SUFFIX}"
    (out / name).symlink_to(victim)
    posted = []
    with _as_owner():
        res = slack_outbox.drain(lambda **kw: None, channel="C", base=tmp,
                                 post_fn=lambda **kw: posted.append(kw))
    assert posted == [], "the bot posted a file read through a link"
    assert [r["status"] for r in res] == ["dropped"]
    assert (out / "failed" / name).is_symlink(), "the refused sidecar is set aside"
    assert json.loads(victim.read_text())["comment"] == "owner-only text"


# ── resident_bridge ───────────────────────────────────────────────────

def test_bridge_state_and_locks_are_owner_private():
    # The paths the module bound at import from the owner-private dir.
    for p in (rb.SEND_LOCK_FILE, rb.ROUTER_STATE_FILE, rb.PENDING_FILE, rb.INFLIGHT_FILE):
        assert Path(p).parent == rb._PRIVATE and "/.ea/" not in str(p), p
    assert "/.ea/state/resident-pane.lock" not in (ROOT / "preprocessor" / "resident_bridge.py").read_text()


def test_a_link_at_a_bridge_lock_is_never_truncated():
    tmp = Path(tempfile.mkdtemp())
    victim = _victim(tmp)
    saved = (rb.SEND_LOCK_FILE, rb.INFLIGHT_FILE)
    try:
        rb.SEND_LOCK_FILE = tmp / "resident-pane.lock"
        rb.INFLIGHT_FILE = tmp / "resident-inflight.json"
        rb.SEND_LOCK_FILE.with_suffix(".inflight-lock").symlink_to(victim)
        rb.claim_inflight("t1")        # fails open, never through the link
        assert victim.read_text() == VICTIM_TEXT
    finally:
        rb.SEND_LOCK_FILE, rb.INFLIGHT_FILE = saved


def test_the_legacy_router_and_pending_copies_are_read_once_and_validated():
    tmp = Path(tempfile.mkdtemp())
    saved = (rb.ROUTER_STATE_FILE, rb.PENDING_FILE, rb.SEND_LOCK_FILE)
    state = EA_DIR / "state"
    try:
        rb.ROUTER_STATE_FILE = tmp / "resident-router.json"
        rb.PENDING_FILE = tmp / "resident-pending.json"
        rb.SEND_LOCK_FILE = tmp / "resident-pane.lock"
        (state / "resident-router.json").write_text(json.dumps(
            {"offset": 5, "posted": ["u1", 3], "extra": "dropped"}))
        (state / "resident-pending.json").write_text(json.dumps(
            {"d1": {"thread_ts": "t1", "channel": "C1", "start_offset": 1,
                    "journal_offset": 2, "created": 3.0},
             "d2": {"thread_ts": 7, "channel": "C1"}, "d3": "junk"}))
        with _as_owner() as calls:
            assert rb._router_state() == {"offset": 5, "posted": ["u1"]}
            assert list(rb.pending_dispatches()) == ["d1"]
        assert [op for op, _ in _ops(calls)] == ["read", "read"]
        rb.mark_posted("u2")
        assert json.loads(rb.ROUTER_STATE_FILE.read_text())["posted"] == ["u1", "u2"]
        (state / "resident-router.json").write_text(json.dumps({"posted": ["later"]}))
        assert rb._router_state()["posted"] == ["u1", "u2"], "the private copy wins"
    finally:
        rb.ROUTER_STATE_FILE, rb.PENDING_FILE, rb.SEND_LOCK_FILE = saved
        for n in ("resident-router.json", "resident-pending.json"):
            (state / n).unlink(missing_ok=True)


# ── context_export ────────────────────────────────────────────────────

def test_attachment_names_are_reduced_to_a_safe_basename():
    f = cx.safe_attachment_name
    assert f("report.pdf") == "report.pdf"
    assert f("/home/owner/.bashrc") == "bashrc"
    assert f("../../x.txt") == "x.txt"
    assert f("..\\..\\evil.doc") == "evil.doc"
    assert f("..") == "attachment" and f("") == "attachment" and f(None) == "attachment"


def test_attachments_land_inside_the_workspace():
    work = Path(tempfile.mkdtemp())
    outside = work.parent / f"escaped-{uuid.uuid4().hex[:6]}.txt"
    fake = types.SimpleNamespace(
        get_attachments=lambda mid: [
            {"name": f"../{outside.name}", "size": 3,
             "contentBytes": "YWJj"},
            {"name": str(outside), "size": 3, "contentBytes": "YWJj"}])
    sys.modules["graph_client"], saved = fake, sys.modules.get("graph_client")
    try:
        got = cx.download_attachments("AAMk=", work)
    finally:
        if saved is None:
            sys.modules.pop("graph_client", None)
        else:
            sys.modules["graph_client"] = saved
    assert not outside.exists(), "an attachment name chose a path outside the workspace"
    for a in got:
        assert Path(a["path"]).parent == work / "attachments", a


def test_an_owner_workspace_inside_the_resident_tree_is_refused():
    with _as_owner():
        try:
            cx.check_workspace(EA_DIR / "exports" / "x")
        except ValueError:
            pass
        else:
            raise AssertionError("an owner workspace in the resident's tree was accepted")
        cx.check_workspace(PRIV / "exports" / "x")


# ── chat downloads ────────────────────────────────────────────────────

class _Resp:
    status_code = 200
    content = b"uploaded bytes"

    def raise_for_status(self):
        return None


def test_slack_download_replaces_a_link_instead_of_writing_through_it():
    dest = EA_DIR / "uploads"
    victim = _victim(Path(tempfile.mkdtemp()))
    fixed = types.SimpleNamespace(now=lambda: types.SimpleNamespace(
        strftime=lambda fmt: "20261001-120000"))
    target = dest / "20261001-120000_a.pdf"
    target.symlink_to(victim)
    saved = (sys.modules.get("requests"), sf.datetime)
    sys.modules["requests"] = types.SimpleNamespace(get=lambda url, **kw: _Resp())
    sf.datetime = fixed
    try:
        with _as_owner() as calls:
            got = sf.SlackFrontend.download(
                types.SimpleNamespace(_bot_token="xoxb"), FileRef("a.pdf", "https://f/1"), dest)
    finally:
        sys.modules["requests"], sf.datetime = saved
    assert got == str(target)
    assert victim.read_text() == VICTIM_TEXT
    assert target.read_bytes() == b"uploaded bytes" and not target.is_symlink()
    assert ("replace", str(target)) in _ops(calls)


# ── the bot ───────────────────────────────────────────────────────────

def _bot_block(start: str, end: str, ns: dict) -> dict:
    i = BOT_SRC.index(start)
    exec(compile(BOT_SRC[i:BOT_SRC.index(end, i)], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


def _claim_ns(tmp: Path) -> dict:
    return _bot_block("# === staged-action claim protocol", "def _handle_context_export", {
        "re": __import__("re"), "json": json, "os": os, "time": time, "uuid": uuid,
        "Path": Path, "EA_DIR": tmp,
        "_chat": types.SimpleNamespace(private_dir=lambda: tmp / "private"),
        "log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                     info=lambda *a, **k: None,
                                     error=lambda *a, **k: None)})


def test_a_claim_is_moved_into_the_private_dir():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "state").mkdir()
    (tmp / "state" / "pending-event.json").write_text(json.dumps({"subject": "x"}))
    ns = _claim_ns(tmp)
    claim, _ = ns["_claim_staged_file"]("pending-event.json")
    assert claim.parent == tmp / "private" / "event-claims"
    assert (claim.parent.stat().st_mode & 0o777) == 0o700
    assert not (tmp / "state" / "event-claims").exists()


def test_a_claim_that_is_a_link_or_a_second_name_is_not_read():
    tmp = Path(tempfile.mkdtemp())
    ns = _claim_ns(tmp)
    victim = _victim(tmp)
    link = tmp / "claim-link"
    link.symlink_to(victim)
    hard = tmp / "claim-hard"
    os.link(victim, hard)
    for p in (link, hard):
        try:
            ns["_read_claim"](p)
        except OSError:
            continue
        raise AssertionError(f"{p.name} was read")
    plain = tmp / "claim-plain"
    plain.write_text('{"a": 1}')
    assert ns["_read_claim"](plain) == '{"a": 1}'


def test_private_state_copies_only_a_json_object_read_as_the_resident():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ea" / "state").mkdir(parents=True)
    reads = []

    def read(p):
        reads.append(str(p))
        return Path(p).read_text() if Path(p).exists() else None

    ns = {"os": os, "logging": __import__("logging"), "Path": Path, "json": json,
          "EA_DIR": tmp / "ea",
          "_chat": types.SimpleNamespace(private_dir=lambda: tmp / "priv"),
          "_resident_io": types.SimpleNamespace(read_text=read)}
    i = BOT_SRC.index("\ndef _private_state(")
    exec(compile(BOT_SRC[i:BOT_SRC.index("\n\n\n", i)], "bot[_private_state]", "exec"), ns)
    (tmp / "ea" / "state" / "usage-ledger.json").write_text("[1, 2]")
    path = ns["_private_state"]("usage-ledger.json")
    assert not path.exists(), "a non-object old copy was taken"
    assert reads == [str(tmp / "ea" / "state" / "usage-ledger.json")]


def test_the_bot_keeps_owner_state_private_and_reads_prompt_files_as_the_resident():
    assert 'def _inflight_file():' in BOT_SRC
    fn = BOT_SRC[BOT_SRC.index("def _inflight_file():"):]
    fn = fn[:fn.index("\n\n\n")]
    assert "_chat.private_dir() / \"bot-inflight.json\"" in fn
    assert 'EA_DIR / "state" / "bot-inflight.json"' not in BOT_SRC
    assert '_inflight_file().write_text(' not in BOT_SRC
    assert 'USAGE_LEDGER = _private_state("usage-ledger.json")' in BOT_SRC
    assert 'SCHED_PROACTIVE_SEEN = _private_state("scheduling-proactive-seen.json")' in BOT_SRC
    assert '(EA_DIR / "logs").mkdir' not in BOT_SRC
    assert 'EA_DIR / "state" / "event-claims"' not in BOT_SRC
    assert 'reply-body-edit-{int(time.time())}' not in BOT_SRC
    assert 'dir=str(EA_DIR / "state")' not in BOT_SRC
    assert "briefing_path.read_text()" not in BOT_SRC
    assert "SCHED_PROACTIVE_QUEUE.read_text()" not in BOT_SRC
    export = BOT_SRC[BOT_SRC.index("def _handle_context_export("):
                     BOT_SRC.index("def _write_fallback_briefing(")]
    assert '_chat.private_dir() / "exports" / timestamp' in export
    assert "_resident_io.replace_text(manifest_path" in export
    assert "_resident_io.read_text(briefing_path)" in export
    hook = (ROOT / ".githooks" / "post-commit").read_text()
    assert 'INFLIGHT_FILE="${EA_PRIVATE_DIR:-$HOME/.ea-private}/bot-inflight.json"' in hook


if __name__ == "__main__":
    if not resident_child_available():
        print(f"skipped: {NEEDS_RESIDENT}")
        raise SystemExit(0)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
