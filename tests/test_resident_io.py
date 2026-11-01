#!/usr/bin/env python3
"""Owner writes into resident space are made with the resident's access.

The logs in the shared ~/.ea/logs and the preferences store in ~/.ea/config
are in resident space: the resident can put a link at any name there. These
pin resident_io and the owner-side writers that use it:

  * a non-owner process writes directly and never asks for the child;
  * an owner process writes through the child (sudo -u ea-bot, python3 -I
    -c, in / with a minimal environment), and only a failed once-per-process
    list-mode preflight selects the direct write, with one warning;
  * after a passing preflight every child outcome other than a clean framed
    reply is a failure for that write, never a direct write;
  * a symlink at the name is refused by the child and by the direct write;
  * the scrubber logs, the audit log, the feedback log, the missed-closure
    log and the preferences store go through resident_io in an owner
    process.

No test runs sudo: the child program runs as the test user through the
`_spawn` seam, and the preflight runs against a fake subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-resident-io-"))
os.environ["EA_DIR"] = str(EA_DIR)
for _sub in ("config", "state", "logs"):
    (EA_DIR / _sub).mkdir(parents=True, exist_ok=True)

import resident_io as rio  # noqa: E402
import atomic_io  # noqa: E402

import pytest
from conftest import NEEDS_RESIDENT, resident_child_available

# The resident child needs the two-user install; a bare runner skips.
pytestmark = pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)

VICTIM_TEXT = "owner-only content\n"


# ── helpers ───────────────────────────────────────────────────────────

@contextmanager
def _seams(owner: bool, available: bool = True, spawn=None, timeout: float | None = None):
    """Set the owner decision, the availability and the spawn; restore after."""
    saved = (rio._owner_check, rio._availability, rio._spawn, rio.CHILD_TIMEOUT_S,
             set(rio._WARNED), rio._RESIDENT_OK)
    rio._owner_check = lambda: owner
    rio._availability = lambda: available
    rio._spawn = spawn if spawn is not None else _no_spawn
    if timeout is not None:
        rio.CHILD_TIMEOUT_S = timeout
    rio._WARNED.clear()
    try:
        yield
    finally:
        (rio._owner_check, rio._availability, rio._spawn, rio.CHILD_TIMEOUT_S,
         warned, rio._RESIDENT_OK) = saved
        rio._WARNED.clear()
        rio._WARNED.update(warned)


def _no_spawn(argv):
    raise AssertionError(f"the child was started: {argv[:5]}")


def _pipes(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))


def _child_spawn(calls: list):
    """Run the real child program as the test user (no sudo) and record argv."""
    def spawn(argv):
        calls.append(list(argv))
        return _pipes(argv)
    return spawn


def _fake_child(code: str):
    """A spawn that runs `code` instead of the child program."""
    def spawn(argv):
        return _pipes([sys.executable, "-c", code])
    return spawn


_DIRECT = ("_direct_append", "_direct_update", "_direct_replace_bytes",
           "_direct_make_dir", "_direct_ensure_file", "_direct_move", "_direct_read",
           "_direct_list")


@contextmanager
def _no_direct_write():
    """Fail the test if any direct write is taken."""
    used = []
    saved = {name: getattr(rio, name) for name in _DIRECT}
    saved_atomic = rio._atomic

    def spy(*a, **k):
        used.append(a)
        raise AssertionError("the direct write was taken")

    for name in _DIRECT:
        setattr(rio, name, spy)
    rio._atomic = types.SimpleNamespace(write_text=spy)
    try:
        yield used
    finally:
        for name, fn in saved.items():
            setattr(rio, name, fn)
        rio._atomic = saved_atomic


def _victim_link(tmp: Path, name: str) -> tuple[Path, Path]:
    """A file only the owner should write, and a link to it at `name`."""
    victim = tmp / "private" / "victim.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text(VICTIM_TEXT)
    link = tmp / "logs" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim)
    return victim, link


def _ops(calls):
    """(op, path, ...) per recorded child argv."""
    return [(a[4], *a[6:]) if a[2] == "-c" else (a[3], *a[5:]) for a in calls]


@contextmanager
def _in_state():
    """A scratch directory under EA_DIR/state (inside resident space for
    move's scope check), removed afterwards."""
    with tempfile.TemporaryDirectory(dir=EA_DIR / "state") as td:
        yield Path(td)


def _mode(path: Path) -> int:
    return os.lstat(path).st_mode & 0o7777


# ── the child argv and the default spawn ──────────────────────────────

def test_child_argv_is_isolated_and_inline():
    argv = rio._child_argv("append", "/x/y.log", 0o664)
    assert argv[1:4] == ["-I", "-c", rio._CHILD]
    assert argv[4:] == ["append", "664", "/x/y.log"]
    # the child imports nothing from the repo
    for mod in ("atomic_io", "owner_trust", "resident_io", "preprocessor"):
        assert mod not in rio._CHILD, mod


def test_default_spawn_runs_the_child_as_the_resident_in_root_with_a_minimal_env():
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

    real = rio.subprocess
    rio.subprocess = types.SimpleNamespace(Popen=FakePopen, PIPE=subprocess.PIPE,
                                           TimeoutExpired=subprocess.TimeoutExpired)
    try:
        rio._sudo_spawn(rio._child_argv("append", "/a/b", 0o664))
    finally:
        rio.subprocess = real
    cmd, kw = seen["cmd"], seen["kw"]
    assert cmd[1:5] == ["-n", "-u", rio.RESIDENT_USER, rio._resident_python()]
    assert cmd[0].endswith("sudo")
    assert cmd[5:7] == ["-I", "-c"]
    assert kw["cwd"] == "/" and kw["env"] == rio.CHILD_ENV
    assert set(rio.CHILD_ENV) == {"PATH"}


# ── the preflight ─────────────────────────────────────────────────────

def _fake_run(outcome, seen):
    def run(cmd, **kw):
        seen.append((cmd, kw))
        if isinstance(outcome, BaseException):
            raise outcome
        return types.SimpleNamespace(returncode=outcome, stdout=b"", stderr=b"")
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


def test_preflight_is_a_list_mode_check_of_the_exact_child():
    real_sub, real_pwd = rio.subprocess, rio.pwd
    rio.pwd = types.SimpleNamespace(getpwnam=lambda n: object())
    seen = []
    try:
        rio.subprocess = _fake_run(0, seen)
        assert rio._probe_resident() is True
        rio.subprocess = _fake_run(1, seen)
        assert rio._probe_resident() is False
        rio.subprocess = _fake_run(FileNotFoundError("sudo"), seen)
        assert rio._probe_resident() is False
        rio.subprocess = _fake_run(subprocess.TimeoutExpired("sudo", 1), seen)
        assert rio._probe_resident() is False
    finally:
        rio.subprocess, rio.pwd = real_sub, real_pwd
    cmd, kw = seen[0]
    # list mode (-l): the policy is asked, nothing runs as the resident
    assert cmd[1:6] == ["-n", "-l", "-u", rio.RESIDENT_USER, rio._resident_python()]
    assert cmd[6:8] == ["-I", "-c"] and cmd[8] == rio._CHILD
    assert kw["env"] == rio.CHILD_ENV and kw["cwd"] == "/"


def test_preflight_without_a_resident_user_is_unavailable():
    real_sub, real_pwd = rio.subprocess, rio.pwd

    def missing(name):
        raise KeyError(name)

    seen = []
    rio.pwd = types.SimpleNamespace(getpwnam=missing)
    rio.subprocess = _fake_run(0, seen)
    try:
        assert rio._probe_resident() is False
        assert seen == [], "no sudo call without a resident user"
    finally:
        rio.subprocess, rio.pwd = real_sub, real_pwd


def test_preflight_runs_once_per_process():
    calls = []
    real_probe, real_ok = rio._probe_resident, rio._RESIDENT_OK
    rio._probe_resident = lambda: calls.append(1) or True
    rio._RESIDENT_OK = None
    try:
        assert rio._resident_available() and rio._resident_available()
    finally:
        rio._probe_resident, rio._RESIDENT_OK = real_probe, real_ok
    assert calls == [1]


def test_an_error_in_the_owner_decision_keeps_the_rule_on():
    saved = rio._owner_check

    def boom():
        raise RuntimeError("no answer")

    rio._owner_check = boom
    try:
        assert rio.is_owner() is True
    finally:
        rio._owner_check = saved


# ── non-owner processes ───────────────────────────────────────────────

def test_a_non_owner_process_writes_directly_and_never_asks():
    with tempfile.TemporaryDirectory() as td, _seams(owner=False, available=False):
        asked = []
        rio._availability = lambda: asked.append(1) or False
        log = Path(td) / "logs" / "a.log"
        assert rio.append_text(log, "one\n") and rio.append_text(log, "two\n")
        assert log.read_text() == "one\ntwo\n"
        state = Path(td) / "state" / "s.json"
        assert rio.replace_text(state, "{}", mode=0o640)
        assert state.read_text() == "{}" and (state.stat().st_mode & 0o777) == 0o640
        assert rio.update_text(state, lambda old: old + "!")
        assert state.read_text() == "{}!"
        assert asked == [], "availability is only asked in an owner process"


def test_a_non_owner_direct_append_refuses_a_symlink_at_the_name():
    with tempfile.TemporaryDirectory() as td, _seams(owner=False):
        victim, link = _victim_link(Path(td), "x.log")
        err = StringIO()
        with redirect_stderr(err):
            assert rio.append_text(link, "line\n") is False
        assert victim.read_text() == VICTIM_TEXT
        assert len(err.getvalue().strip().splitlines()) == 1


# ── owner processes, child allowed ────────────────────────────────────

def test_an_owner_append_goes_through_the_child():
    with tempfile.TemporaryDirectory() as td:
        calls = []
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            log = Path(td) / "logs" / "a.log"
            assert rio.append_text(log, "one\n")
            assert rio.append_text(log, "two\n")
        assert log.read_text() == "one\ntwo\n"
        assert _ops(calls) == [("append", str(log)), ("append", str(log))]
        # a new file gets umask 002, like the rest of ~/.ea
        assert (log.stat().st_mode & 0o777) == 0o664


def test_an_owner_replace_goes_through_the_child():
    with tempfile.TemporaryDirectory() as td:
        calls = []
        dest = Path(td) / "state" / "s.json"
        dest.parent.mkdir()
        dest.write_text("old")
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            assert rio.replace_text(dest, '{"a": 1}', mode=0o640)
        assert dest.read_text() == '{"a": 1}'
        assert (dest.stat().st_mode & 0o777) == 0o640
        assert _ops(calls) == [("replace", str(dest))]
        assert sorted(p.name for p in dest.parent.iterdir()) == ["s.json"]


def test_an_owner_update_goes_through_the_child_under_its_lock():
    with tempfile.TemporaryDirectory() as td:
        calls, seen = [], []
        dest = Path(td) / "config" / "p.json"
        dest.parent.mkdir()
        dest.write_text("abc")

        def fn(old):
            seen.append(old)
            return old.upper()

        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            assert rio.update_text(dest, fn)
            assert rio.update_text(dest, lambda old: None)
        assert seen == ["abc"] and dest.read_text() == "ABC"
        assert _ops(calls) == [("update", str(dest)), ("update", str(dest))]


def test_an_exception_from_fn_propagates_and_leaves_the_file():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "p.json"
        dest.write_text("keep")

        def fn(old):
            raise ValueError("bad rule")

        for owner in (True, False):
            with _seams(owner=owner, spawn=_child_spawn([])):
                try:
                    rio.update_text(dest, fn)
                except ValueError as e:
                    assert str(e) == "bad rule"
                else:
                    raise AssertionError("fn's exception was swallowed")
            assert dest.read_text() == "keep"


def test_the_child_refuses_a_symlink_at_the_name():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        victim, link = _victim_link(tmp, "x.log")
        with _seams(owner=True, spawn=_child_spawn([])), _no_direct_write():
            err = StringIO()
            with redirect_stderr(err):
                assert rio.append_text(link, "line\n") is False
                assert rio.update_text(link, lambda old: "overwritten") is False
            assert victim.read_text() == VICTIM_TEXT
            lines = err.getvalue().strip().splitlines()
            assert len(lines) == 2 and all(l.startswith("resident_io: ") for l in lines)


def test_the_child_gets_the_directory_resolved_and_the_name_as_given():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        real = tmp / "real-logs"
        real.mkdir()
        alias = tmp / "alias"
        alias.symlink_to(real)
        calls = []
        with _seams(owner=True, spawn=_child_spawn(calls)):
            assert rio.append_text(alias / "a.log", "x\n")
        assert _ops(calls) == [("append", str(real.resolve() / "a.log"))]
        assert (real / "a.log").read_text() == "x\n"


def test_a_passing_preflight_never_falls_back_on_a_bad_child():
    outcomes = {
        "non-zero exit": "import sys; sys.stderr.write('RESIDENT-IO-REFUSED: nope'); sys.exit(4)",
        "no output": "import sys; sys.stdin.buffer.read()",
        "malformed": "import sys; sys.stdin.buffer.read(); sys.stdout.write('OK')",
        "wrong count": ("import sys; sys.stdin.buffer.read(); "
                        "sys.stdout.buffer.write(b'RESIDENT-IO-OK 999\\n')"),
        "extra output": ("import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write("
                         "b'RESIDENT-IO-OK ' + str(len(d)).encode() + b'\\nmore')"),
    }
    with _in_state() as tmp:
        log = tmp / "a.log"
        log.write_text("before\n")
        for label, code in outcomes.items():
            # a fake that waits for stdin never sends update's first frame;
            # the watchdog ends it, so keep it short here
            with _seams(owner=True, spawn=_fake_child(code), timeout=1), \
                    _no_direct_write() as used:
                err = StringIO()
                with redirect_stderr(err):
                    assert rio.append_text(log, "x\n") is False, label
                    assert rio.replace_text(log, "x") is False, label
                    assert rio.replace_bytes(log, b"x") is False, label
                    assert rio.update_text(log, lambda old: "x") is False, label
                    assert rio.make_dir(tmp / "d") is False, label
                    assert rio.ensure_file(tmp / "lock") is False, label
                    assert rio.move(log, tmp / "b.log") is False, label
                    assert rio.read_bytes(log) is None, label
                    assert rio.read_text(log) is None, label
                assert used == [], label
                assert len(err.getvalue().strip().splitlines()) == 9, (label, err.getvalue())
            assert log.read_text() == "before\n", label
            assert sorted(p.name for p in tmp.iterdir()) == ["a.log"], label


def test_an_update_the_child_does_not_acknowledge_is_a_failure():
    code = ("import sys; sys.stdout.buffer.write(b'RESIDENT-IO-OK 0\\n'); "
            "sys.stdout.flush(); sys.stdin.buffer.readline(); sys.exit(4)")
    with tempfile.TemporaryDirectory() as td:
        with _seams(owner=True, spawn=_fake_child(code)), _no_direct_write():
            with redirect_stderr(StringIO()):
                assert rio.update_text(Path(td) / "p.json", lambda old: "new") is False


def test_a_child_that_hangs_is_killed_and_the_write_fails():
    code = "import time; time.sleep(30)"
    with tempfile.TemporaryDirectory() as td:
        with _seams(owner=True, spawn=_fake_child(code), timeout=0.5), _no_direct_write():
            with redirect_stderr(StringIO()):
                assert rio.append_text(Path(td) / "a.log", "x\n") is False
                assert rio.update_text(Path(td) / "p.json", lambda old: "x") is False


def test_a_child_that_cannot_start_is_a_failure():
    def spawn(argv):
        raise FileNotFoundError("sudo")

    with tempfile.TemporaryDirectory() as td:
        with _seams(owner=True, spawn=spawn), _no_direct_write():
            with redirect_stderr(StringIO()):
                assert rio.append_text(Path(td) / "a.log", "x\n") is False
                assert rio.update_text(Path(td) / "p.json", lambda old: "x") is False


# ── owner processes, child not allowed: the fallback ──────────────────

def test_the_fallback_writes_directly_and_warns_once():
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "a.log"
        err = StringIO()
        with _seams(owner=True, available=False), redirect_stderr(err):
            assert rio.append_text(log, "one\n") and rio.append_text(log, "two\n")
        assert log.read_text() == "one\ntwo\n"
        warnings = [l for l in err.getvalue().splitlines() if "sudo policy" in l]
        assert len(warnings) == 1, err.getvalue()


def test_the_fallback_refuses_a_symlink_at_the_name():
    with tempfile.TemporaryDirectory() as td:
        victim, link = _victim_link(Path(td), "x.log")
        with _seams(owner=True, available=False), redirect_stderr(StringIO()):
            assert rio.append_text(link, "line\n") is False
            assert rio.update_text(link, lambda old: "overwritten") is False
        assert victim.read_text() == VICTIM_TEXT


def test_the_fallback_replace_goes_through_atomic_io():
    with tempfile.TemporaryDirectory() as td:
        victim, link = _victim_link(Path(td), "s.json")
        used = []
        real = rio._atomic

        def spy(path, data, mode=0o644):
            used.append((path, mode))
            return atomic_io.write_text(path, data, mode=mode)

        rio._atomic = types.SimpleNamespace(write_text=spy)
        try:
            with _seams(owner=True, available=False), redirect_stderr(StringIO()):
                assert rio.replace_text(link, "{}", mode=0o660)
        finally:
            rio._atomic = real
        assert used == [(str(link), 0o660)]
        # the link was replaced, not followed
        assert victim.read_text() == VICTIM_TEXT
        assert not link.is_symlink() and link.read_text() == "{}"


# ── the owner-side writers ────────────────────────────────────────────

def test_the_scrubber_logs_go_through_the_child():
    import closure_scrubber as cs
    with tempfile.TemporaryDirectory() as td:
        calls = []
        log = Path(td) / "logs" / "closure-scrubber.log"
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            cs._append_jsonl(log, [{"id": "a"}, {"id": "b"}])
            cs._append_jsonl(log.with_name(cs.SUPPRESSED_LOG_NAME), [{"id": "c"}])
            cs._append_jsonl(log, [])
        assert [json.loads(l)["id"] for l in log.read_text().splitlines()] == ["a", "b"]
        assert _ops(calls) == [("append", str(log)),
                               ("append", str(log.with_name(cs.SUPPRESSED_LOG_NAME)))]


def test_the_scrubber_log_does_not_follow_a_link_in_an_owner_process():
    import closure_scrubber as cs
    with tempfile.TemporaryDirectory() as td:
        victim, link = _victim_link(Path(td), "closure-scrubber.log")
        with _seams(owner=True, spawn=_child_spawn([])), redirect_stderr(StringIO()):
            cs._append_jsonl(link, [{"id": "a"}])
        assert victim.read_text() == VICTIM_TEXT


def test_the_scrubber_veto_state_is_read_through_the_child():
    import closure_scrubber as cs
    with tempfile.TemporaryDirectory() as td:
        calls = []
        state = Path(td) / "state" / cs.VETO_STATE_NAME
        state.parent.mkdir()
        state.write_text(json.dumps({"version": 1, "closes": {},
                                     "vetoes": {"r1": {"signals": ["sender_reply_match"]}}}))
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            store = cs.VetoStore.load(state)
        assert store.is_vetoed("r1", "sender_reply_match")
        assert _ops(calls) == [("read", str(state))]
        # a link at the state name hands over nothing, so save() cannot
        # write a linked file's content back into resident space
        secret = Path(td) / "private" / "owner.json"
        secret.parent.mkdir()
        secret.write_text(json.dumps({"RefreshToken": {"x": "owner-only"}}))
        link = Path(td) / "state" / "linked-vetoes.json"
        link.symlink_to(secret)
        with _seams(owner=True, spawn=_child_spawn([])), redirect_stderr(StringIO()):
            store = cs.VetoStore.load(link)
        assert "RefreshToken" not in store.data
        assert store.data == {"version": 1, "closes": {}, "vetoes": {}}


def test_the_open_claims_audit_log_goes_through_the_child():
    import open_claims_audit as oca
    with tempfile.TemporaryDirectory() as td:
        calls = []
        log = Path(td) / "logs" / "open-claims-audit.jsonl"
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            assert oca._append({"agent": "morning-briefing", "asked": 0}, log)
        assert json.loads(log.read_text())["agent"] == "morning-briefing"
        assert _ops(calls) == [("append", str(log))]
        victim, link = _victim_link(Path(td), "audit-link.jsonl")
        with _seams(owner=True, spawn=_child_spawn([])), redirect_stderr(StringIO()):
            assert oca._append({"agent": "x"}, link) is False
        assert victim.read_text() == VICTIM_TEXT


def test_the_item_feedback_log_goes_through_the_child():
    import item_feedback as fb
    with tempfile.TemporaryDirectory() as td:
        calls = []
        log = Path(td) / "logs" / "item-feedback.jsonl"
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            assert fb._append({"ledger_id": "r1", "kind": "still_open"}, log)
        assert json.loads(log.read_text())["ledger_id"] == "r1"
        assert _ops(calls) == [("append", str(log))]
        victim, link = _victim_link(Path(td), "feedback-link.jsonl")
        with _seams(owner=True, spawn=_child_spawn([])), redirect_stderr(StringIO()):
            assert fb._append({"ledger_id": "r2"}, link) is False
        assert victim.read_text() == VICTIM_TEXT


def test_the_missed_closure_log_goes_through_the_child():
    import log_missed_closure as lmc
    with tempfile.TemporaryDirectory() as td:
        calls = []
        saved = lmc.LOG_PATH
        lmc.LOG_PATH = Path(td) / "logs" / "missed-closures.log"
        try:
            with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
                rec = lmc.log_missed_closure(ledger_id="no-such-row",
                                             user_message="done", no_fetch=True)
            assert rec["_logged"] is True
            assert json.loads(lmc.LOG_PATH.read_text())["ledger_id"] == "no-such-row"
            assert _ops(calls) == [("append", str(lmc.LOG_PATH))]
            victim, link = _victim_link(Path(td), "mc-link.log")
            lmc.LOG_PATH = link
            err = StringIO()
            with _seams(owner=True, spawn=_child_spawn([])), redirect_stderr(err):
                assert lmc._append({"ledger_id": "x"}) is False
            assert victim.read_text() == VICTIM_TEXT
            assert "NOT logged" in err.getvalue()
        finally:
            lmc.LOG_PATH = saved


# ── preferences ───────────────────────────────────────────────────────

def _prefs():
    import preferences
    return preferences


def test_an_owner_preferences_update_goes_through_the_child():
    P = _prefs()
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "config" / "preferences.json"
        store.parent.mkdir()
        store.write_text(json.dumps({"version": 1, "rules": [], "calibration": []}))
        calls = []
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            rule = P.update(lambda d: P.add_rule(d, kind="avoid", scopes=["funding"],
                                                 text="Skip standing BAAs."), store)
            row = P.update(lambda d: P.add_calibration(d, scope="briefing",
                                                       note="too many items"), store)
            P.save({"version": 1, "rules": [], "calibration": [row]}, store)
        assert rule["id"] and row["note"] == "too many items"
        assert _ops(calls) == [("update", str(store))] * 3
        doc = json.loads(store.read_text())
        assert doc["rules"] == [] and doc["calibration"] == [row] and "_doc" in doc


def test_an_owner_preferences_update_keeps_the_mutator_errors():
    P = _prefs()
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "preferences.json"
        store.write_text(json.dumps({"version": 1, "rules": [], "calibration": []}))
        before = store.read_text()
        with _seams(owner=True, spawn=_child_spawn([])):
            try:
                P.update(lambda d: P.add_rule(d, kind="bogus", scopes=["x"], text="t"), store)
            except ValueError:
                pass
            else:
                raise AssertionError("the ValueError did not reach the caller")
            try:
                P.update(lambda d: P.retire_rule(d, "missing"), store)
            except KeyError:
                pass
            else:
                raise AssertionError("the KeyError did not reach the caller")
        assert store.read_text() == before


def test_an_owner_preferences_update_does_not_rewrite_a_linked_file():
    P = _prefs()
    with tempfile.TemporaryDirectory() as td:
        victim, link = _victim_link(Path(td), "preferences.json")
        for available in (True, False):
            with _seams(owner=True, available=available, spawn=_child_spawn([])), \
                    redirect_stderr(StringIO()):
                try:
                    P.update(lambda d: P.add_calibration(d, scope="briefing", note="n"), link)
                except OSError:
                    pass
                else:
                    raise AssertionError("the update through a link did not fail")
            assert victim.read_text() == VICTIM_TEXT, available


def test_a_resident_preferences_update_keeps_the_locked_in_place_write():
    P = _prefs()
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "preferences.json"
        with _seams(owner=False), _no_direct_write():
            P.update(lambda d: P.add_calibration(d, scope="briefing", note="n"), store)
        assert json.loads(store.read_text())["calibration"][0]["note"] == "n"
        assert (store.stat().st_mode & 0o777) == 0o664


# ── directories, lock files, moves, bytes ─────────────────────────────

def _exercise_the_other_verbs(tmp: Path) -> None:
    """make_dir, ensure_file, replace_bytes and move as a caller sees them."""
    d = tmp / "outbox" / "failed"
    assert rio.make_dir(d, 0o2775)
    assert d.is_dir() and _mode(d) == 0o2775
    os.chmod(d, 0o2770)
    assert rio.make_dir(d, 0o2775), "an existing directory is fine"
    assert _mode(d) == 0o2770, "an existing directory keeps its mode"

    lock = tmp / "x.lock"
    assert rio.ensure_file(lock)
    assert lock.read_bytes() == b"" and _mode(lock) == 0o664
    lock.write_text("held")
    assert rio.ensure_file(lock) and lock.read_text() == "held"

    blob = tmp / "outbox" / "pic.png"
    assert rio.replace_bytes(blob, b"\x89PNG\x00\xff", mode=0o640)
    assert blob.read_bytes() == b"\x89PNG\x00\xff" and _mode(blob) == 0o640

    assert rio.move(blob, d / "pic.png")
    assert not blob.exists() and (d / "pic.png").read_bytes() == b"\x89PNG\x00\xff"


def test_the_other_verbs_in_a_non_owner_process():
    with _in_state() as tmp, _seams(owner=False):
        _exercise_the_other_verbs(tmp)


def test_the_other_verbs_go_through_the_child_in_an_owner_process():
    calls = []
    with _in_state() as tmp:
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            _exercise_the_other_verbs(tmp)
        real = os.path.realpath(tmp)
        d = os.path.join(real, "outbox", "failed")
        ops = _ops(calls)
        assert ops[0] == ("mkdir", d) and ops[1] == ("mkdir", d)
        assert ("touch", os.path.join(real, "x.lock")) in ops
        assert ("replace", os.path.join(real, "outbox", "pic.png")) in ops
        assert ops[-1] == ("move", os.path.join(real, "outbox", "pic.png"),
                           os.path.join(d, "pic.png"))


def test_the_other_verbs_in_the_fallback():
    with _in_state() as tmp, _seams(owner=True, available=False), redirect_stderr(StringIO()):
        _exercise_the_other_verbs(tmp)


def test_make_dir_and_ensure_file_refuse_a_symlink_at_the_name():
    for owner, available in ((False, False), (True, True), (True, False)):
        with _in_state() as tmp:
            target = tmp / "elsewhere"
            target.mkdir()
            os.chmod(target, 0o700)
            (tmp / "dirlink").symlink_to(target)
            victim = tmp / "victim.txt"
            victim.write_text(VICTIM_TEXT)
            (tmp / "filelink").symlink_to(victim)
            (tmp / "afile").write_text("x")
            with _seams(owner=owner, available=available, spawn=_child_spawn([])), \
                    redirect_stderr(StringIO()):
                assert rio.make_dir(tmp / "dirlink") is False, (owner, available)
                assert rio.make_dir(tmp / "afile") is False, (owner, available)
                assert rio.ensure_file(tmp / "filelink") is False, (owner, available)
            assert _mode(target) == 0o700, "the link target's mode is untouched"
            assert victim.read_text() == VICTIM_TEXT


def test_move_stays_in_the_top_level_directory():
    for owner in (False, True):
        calls = []
        with _in_state() as tmp:
            src = tmp / "a.json"
            src.write_text("{}")
            other_top = EA_DIR / "config" / f"moved-{os.getpid()}.json"
            outside = Path(tempfile.gettempdir()) / f"resident-io-outside-{os.getpid()}"
            with _seams(owner=owner, spawn=_child_spawn(calls)), redirect_stderr(StringIO()):
                assert rio.move(src, other_top) is False
                assert rio.move(src, outside) is False
                assert rio.move(src, EA_DIR / f"top-{os.getpid()}.json") is False
            assert src.read_text() == "{}"
            assert not other_top.exists() and not outside.exists()
            assert calls == [], "the scope check runs before the child starts"


def test_move_refuses_a_source_outside_resident_space():
    with tempfile.TemporaryDirectory() as td, _seams(owner=False), redirect_stderr(StringIO()):
        src = Path(td) / "a"
        src.write_text("x")
        assert rio.move(src, Path(td) / "b") is False
        assert src.exists()


def test_move_scope_follows_the_resolved_directory():
    """A link that makes a destination look like it is in the same top-level
    directory while it resolves into another one is refused."""
    with _in_state() as tmp:
        src = tmp / "a.json"
        src.write_text("{}")
        (tmp / "looks-local").symlink_to(EA_DIR / "config")
        with _seams(owner=False), redirect_stderr(StringIO()):
            assert rio.move(src, tmp / "looks-local" / "a.json") is False
        assert src.exists()


# ── reads ─────────────────────────────────────────────────────────────

def test_reads_in_every_mode():
    for owner, available in ((False, False), (True, True), (True, False)):
        calls = []
        with _in_state() as tmp:
            f = tmp / "s.json"
            f.write_bytes(b'{"a": "\xc3\xa9"}')
            with _seams(owner=owner, available=available, spawn=_child_spawn(calls)):
                err = StringIO()
                with redirect_stderr(err):
                    assert rio.read_bytes(f) == b'{"a": "\xc3\xa9"}'
                    assert rio.read_text(f) == '{"a": "é"}'
                    assert rio.read_text(tmp / "missing.json") is None
                    assert rio.read_bytes(tmp / "no-dir" / "missing.json") is None
                assert "could not" not in err.getvalue(), "a missing file is quiet"
            via_child = owner and available
            assert bool(calls) == via_child, (owner, available)
            if via_child:
                assert _ops(calls)[0] == ("read", os.path.join(os.path.realpath(tmp), "s.json"))


def test_a_read_through_a_link_is_refused():
    for owner, available in ((False, False), (True, True), (True, False)):
        with tempfile.TemporaryDirectory() as td:
            victim, link = _victim_link(Path(td), "state.json")
            err = StringIO()
            with _seams(owner=owner, available=available, spawn=_child_spawn([])), \
                    redirect_stderr(err):
                assert rio.read_bytes(link) is None, (owner, available)
                assert rio.read_text(link) is None, (owner, available)
            assert "could not read" in err.getvalue()


def test_an_owner_read_never_falls_back_on_a_bad_child():
    fakes = {
        "missing marker with junk": ("import sys; sys.stdout.buffer.write("
                                     "b'RESIDENT-IO-MISSING\\nextra')"),
        "short payload": "import sys; sys.stdout.buffer.write(b'RESIDENT-IO-OK 10\\nabc')",
        "non-zero exit": "import sys; sys.stdout.buffer.write(b'RESIDENT-IO-OK 0\\n'); sys.exit(1)",
    }
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "s.json"
        f.write_text("real")
        for label, code in fakes.items():
            err = StringIO()
            with _seams(owner=True, spawn=_fake_child(code)), _no_direct_write() as used, \
                    redirect_stderr(err):
                assert rio.read_bytes(f) is None, label
            assert used == [] and "could not read" in err.getvalue(), label


def test_undecodable_text_is_a_failure():
    with tempfile.TemporaryDirectory() as td, _seams(owner=False):
        f = Path(td) / "bad.txt"
        f.write_bytes(b"\xff\xfe")
        err = StringIO()
        with redirect_stderr(err):
            assert rio.read_text(f) is None
        assert len(err.getvalue().strip().splitlines()) == 1


def test_the_child_and_the_owner_agree_on_the_read_cap():
    assert f"MAX_READ = {rio.MAX_READ_BYTES}\n" in rio._CHILD
    assert rio.MISSING.decode() in rio._CHILD.replace("\\n", "\n")


# ── list_dir ──────────────────────────────────────────────────────────

def _tree(tmp: Path) -> Path:
    d = tmp / "listed"
    (d / "sub").mkdir(parents=True)
    (d / "b.txt").write_text("b")
    (d / "a.json").write_text("{}")
    (d / "link").symlink_to(tmp / "elsewhere")
    return d


_LISTED = [("f", "a.json"), ("f", "b.txt"), ("l", "link"), ("d", "sub")]


def test_an_owner_list_goes_through_the_child():
    with tempfile.TemporaryDirectory() as td:
        d = _tree(Path(td))
        calls = []
        with _seams(owner=True, spawn=_child_spawn(calls)), _no_direct_write():
            assert rio.list_dir(d) == _LISTED
            assert rio.list_dir(d / "missing") is None
        assert _ops(calls) == [("list", str(d)), ("list", str(d / "missing"))]


def test_a_non_owner_list_is_direct_and_the_same():
    with tempfile.TemporaryDirectory() as td:
        d = _tree(Path(td))
        with _seams(owner=False):
            assert rio.list_dir(d) == _LISTED
            assert rio.list_dir(d / "missing") is None


def test_list_refuses_a_symlink_at_the_name():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _tree(tmp)
        alias = tmp / "alias"
        alias.symlink_to(d)
        for owner in (True, False):
            spawn = _child_spawn([]) if owner else None
            with _seams(owner=owner, spawn=spawn):
                err = StringIO()
                with redirect_stderr(err):
                    assert rio.list_dir(alias) is None
                assert err.getvalue().startswith("resident_io: ")


def test_a_malformed_list_reply_is_a_failure():
    with tempfile.TemporaryDirectory() as td:
        with _seams(owner=True, spawn=_fake_child(
                "import sys; d=b'xbad'; sys.stdout.buffer.write(b'RESIDENT-IO-OK %d\\n' % len(d) + d)")):
            err = StringIO()
            with redirect_stderr(err):
                assert rio.list_dir(Path(td)) is None
            assert "malformed" in err.getvalue()


def test_the_read_retry_after_a_rename_checks_the_new_file_too():
    """_direct_read opens the name once more when the first file has no
    link left (a writer's rename in between). Whatever a racing resident
    puts at the name for that second open (a hard link to a file it should
    not read, a symlink, a fifo, a directory) is refused like the first.
    The child program refuses a hard link and a symlink at the name too."""
    real_fstat = os.fstat
    with tempfile.TemporaryDirectory() as td:
        secret = Path(td) / "secret"
        secret.write_text("SECRET")
        target = Path(td) / "state.json"

        def racing(kind):
            calls = {"n": 0}

            def fake(fd):
                st = real_fstat(fd)
                calls["n"] += 1
                if calls["n"] == 1:
                    target.unlink()
                    {"hardlink": lambda: os.link(secret, target),
                     "symlink": lambda: os.symlink(secret, target),
                     "fifo": lambda: os.mkfifo(target),
                     "dir": lambda: os.mkdir(target)}[kind]()
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, 0) + tuple(st)[4:])
                return st
            return fake

        for kind in ("hardlink", "symlink", "fifo", "dir"):
            if target.is_dir() and not target.is_symlink():
                target.rmdir()
            elif target.exists() or target.is_symlink():
                target.unlink()
            target.write_text("ok")
            os.fstat = racing(kind)
            try:
                with pytest.raises(OSError):
                    rio._direct_read(str(target))
            finally:
                os.fstat = real_fstat
        target.rmdir()
        for kind in ("hardlink", "symlink"):
            if target.exists() or target.is_symlink():
                target.unlink()
            (os.link if kind == "hardlink" else os.symlink)(secret, target)
            r = subprocess.run([sys.executable, "-I", "-c", rio._CHILD, "read", "0o644", str(target)],
                               capture_output=True)
            assert r.returncode != 0 and b"SECRET" not in r.stdout, kind

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
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
