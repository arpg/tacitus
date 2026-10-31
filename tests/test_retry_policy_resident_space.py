#!/usr/bin/env python3
"""The circuit-breaker state is read with the resident's access.

circuit-breakers.json is in the resident's space, and every write carries
the entries it read back into that file. These pin the read side of that
read-modify-write (docs/SECURITY.md, "Owner trust rules"):

  * a symlink or a hard link at the state name is refused on the read, so
    the owner-only file behind it is neither changed nor copied into the
    state, and the write replaces the name with a plain file;
  * in an owner process the read runs in the resident child;
  * only entries in the shape the module writes are carried over, and a
    file of another shape reads as no state, never as an exception;
  * a state another process renames over the name between the read's
    open and its check is read from the name once more, so the count and
    the other entries survive; the file found then gets the same check,
    and a second rename is a failed read.

Each case runs as the resident (direct), as an owner process whose sudo
preflight fails (direct, O_NOFOLLOW) and as an owner process with the
child. No test runs sudo: resident_io's child program runs as the test
user through its `_spawn` seam.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import resident_io as rio  # noqa: E402
import retry_policy as rp  # noqa: E402

MODES = ("resident", "fallback", "child")


def _ops(calls: list) -> list:
    """The operation of each resident child call, in the inline form
    (python3 -I -c CODE op ...) or the file form (python3 -I FILE op ...)."""
    return [a[4] if a[2] == "-c" else a[3] for a in calls]


@pytest.fixture(params=MODES)
def mode(request, monkeypatch):
    """resident_io as the named process; returns (mode, child argvs)."""
    calls: list = []

    def spawn(argv):
        calls.append(list(argv))
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))

    monkeypatch.setattr(rio, "_owner_check", lambda: request.param != "resident")
    monkeypatch.setattr(rio, "_availability", lambda: request.param == "child")
    monkeypatch.setattr(rio, "_spawn", spawn)
    monkeypatch.setattr(rio, "_child_file", lambda: None)
    monkeypatch.setattr(rio, "_WARNED", set())
    return request.param, calls


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "ea" / "state" / "circuit-breakers.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(rp, "STATE_FILE", path)
    return path


def _breaker(service: str = "graph") -> rp.CircuitBreaker:
    return rp.CircuitBreaker(service, threshold=3, cooldown=300.0)


def _decoy(tmp_path: Path) -> tuple[Path, str]:
    """An owner-only file in the breaker's own shape, with a marker entry
    that a read through a link would carry into the state."""
    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    mark = f"owner-only-{uuid.uuid4().hex}"
    decoy = home / "decoy.json"
    decoy.write_text(json.dumps({mark: {"failures": 1, "last_error": mark}}))
    os.chmod(decoy, 0o600)
    return decoy, mark


def _holding(root: Path, mark: str, decoy: Path) -> list[str]:
    """Files under `root`, other than the decoy itself, that hold `mark`."""
    own = os.stat(decoy)
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = os.lstat(p)
            if p.is_symlink() or (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino):
                continue
            if mark.encode() in p.read_bytes():
                hits.append(p.name)
    return hits


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_a_link_at_the_state_name_is_not_read_through(link, mode, state, tmp_path):
    decoy, mark = _decoy(tmp_path)
    if link == "symlink":
        state.symlink_to(decoy)
    else:
        os.link(decoy, state)
    before = decoy.read_bytes()

    _breaker().record_failure("timeout")

    assert decoy.read_bytes() == before
    assert not state.is_symlink() and state.stat().st_ino != decoy.stat().st_ino
    assert _holding(state.parent, mark, decoy) == []
    written = json.loads(state.read_text())
    assert set(written) == {"graph"}
    assert written["graph"]["failures"] == 1


def test_an_owner_process_reads_the_state_in_the_resident_child(mode, state):
    kind, calls = mode
    b = _breaker()
    for i in range(3):
        b.record_failure(f"e{i}")
    assert _breaker().is_open()
    assert [s["service"] for s in rp.open_breakers()] == ["graph"]
    if kind == "child":
        assert "read" in _ops(calls)
        assert all(a[-1] == str(state) for a in calls)
    else:
        assert calls == []


@pytest.mark.parametrize("text", [
    "[1, 2, 3]",
    '"graph"',
    "not json",
    json.dumps({"graph": [3, 0]}),
    json.dumps({"graph": {"failures": "3", "opened_at": "now", "last_error": 7}}),
    json.dumps({"graph": {"failures": True, "opened_at": None}}),
])
def test_a_state_of_another_shape_reads_as_no_state(text, mode, state):
    state.write_text(text)
    b = _breaker()
    assert not b.is_open()
    b.raise_if_open()
    assert rp.open_breakers() == []
    b.record_failure("x")
    written = json.loads(state.read_text())
    assert written["graph"]["failures"] == 1
    assert isinstance(written["graph"]["last_error"], str)


def test_only_the_fields_the_module_writes_are_carried_over(mode, state):
    state.write_text(json.dumps({
        "graph": {"failures": 3, "opened_at": 1.5, "last_failure_ts": 2.5,
                  "last_error": "e" * 400, "extra": {"k": "v"}},
        "gmail": {"failures": 1, "last_error": "ok", "note": "dropped"},
        "bad": "not an entry",
    }))
    loaded = rp._load_all()
    assert loaded == {
        "graph": {"failures": 3, "opened_at": 1.5, "last_failure_ts": 2.5,
                  "last_error": "e" * 300},
        "gmail": {"failures": 1, "last_error": "ok"},
    }
    _breaker("gmail").record_failure("again")
    written = json.loads(state.read_text())
    assert set(written) == {"graph", "gmail"}
    assert "extra" not in written["graph"] and written["gmail"]["failures"] == 2


# ── another writer's rename between the read's open and its check ─────

def _renames_at_check(state: Path, *replacements: Path) -> str:
    """Python source that patches os.fstat: a check of a descriptor open on
    the file now at the state name first renames the next of
    `replacements` still there over that name, as another process's
    atomic write can between a read's open and its check. Each one is
    renamed in once, in whichever process (or child) checks first."""
    return f"""
import os
_real_fstat = os.fstat
_queue = {[str(r) for r in replacements]!r}
def _fstat(fd):
    st = _real_fstat(fd)
    while _queue and not os.path.lexists(_queue[0]):
        _queue.pop(0)
    try:
        here = os.stat({str(state)!r}, follow_symlinks=False)
    except OSError:
        return st
    if _queue and (here.st_dev, here.st_ino) == (st.st_dev, st.st_ino):
        os.replace(_queue.pop(0), {str(state)!r})
        st = _real_fstat(fd)
    return st
os.fstat = _fstat
"""


def _race(kind: str, monkeypatch, source: str) -> None:
    """Run `source` where the read runs: in each resident child that reads
    (ahead of the child program), or in this process."""
    if kind == "child":
        spawn = rio._spawn

        def racing(argv):
            argv = list(argv)
            assert argv[2] == "-c"
            if argv[4] == "read":
                argv[3] = source + argv[3]
            return spawn(argv)
        monkeypatch.setattr(rio, "_spawn", racing)
    else:
        monkeypatch.setattr(os, "fstat", os.fstat)   # put back after the test
        exec(source, {})


def test_a_state_renamed_in_during_the_read_is_read_again(mode, state, monkeypatch, capsys):
    kind, calls = mode
    state.write_text(json.dumps({"graph": {"failures": 2, "opened_at": 0.0}}))
    newer = state.with_name("newer.json")
    newer.write_text(json.dumps({"graph": {"failures": 2, "opened_at": 0.0},
                                 "gmail": {"failures": 1, "last_error": "x"}}))
    _race(kind, monkeypatch, _renames_at_check(state, newer))

    _breaker().record_failure("timeout")

    assert not newer.exists(), "the rename did not happen"
    written = json.loads(state.read_text())
    assert written["graph"]["failures"] == 3
    assert written["gmail"] == {"failures": 1, "last_error": "x"}
    assert _breaker().is_open()
    assert "could not read" not in capsys.readouterr().err
    assert (kind == "child") == ("read" in _ops(calls))


def test_a_hard_link_renamed_in_during_the_read_is_refused(mode, state, monkeypatch,
                                                           tmp_path, capsys):
    kind, _calls = mode
    decoy, _mark = _decoy(tmp_path)
    state.write_text(json.dumps({"graph": {"failures": 1}}))
    linked = state.with_name("linked.json")
    os.link(decoy, linked)
    _race(kind, monkeypatch, _renames_at_check(state, linked))

    assert rio.read_text(state) is None

    assert not linked.exists(), "the rename did not happen"
    assert os.stat(state).st_ino == os.stat(decoy).st_ino
    assert "could not read" in capsys.readouterr().err


def test_a_second_rename_during_the_read_is_a_failed_read(mode, state, monkeypatch, capsys):
    kind, _calls = mode
    state.write_text("{}")
    first, second = state.with_name("first.json"), state.with_name("second.json")
    first.write_text('{"graph": {"failures": 1}}')
    second.write_text('{"graph": {"failures": 2}}')
    _race(kind, monkeypatch, _renames_at_check(state, first, second))

    assert rio.read_text(state) is None

    assert not first.exists() and not second.exists(), "the renames did not happen"
    assert "could not read" in capsys.readouterr().err
