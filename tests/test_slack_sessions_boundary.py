"""The session registry follows the owner trust rules.

The bot, an owner process, reads the registry in the resident's space and
writes it back. The read is made with the resident's access, so a symlink
or a hard link the resident puts at the registry's name hands the bot none
of the file behind it, and the write replaces the name, so that file is
never changed. A registry that is there but cannot be read is never written
over, and only rows in the shape the module writes are used.

Each case runs as the resident would (a direct call), and as the owner
through the resident_io child (run as the test user through its spawn
seam) and through the fallback a failed sudo preflight selects.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner.
SANDBOX = Path(tempfile.mkdtemp(prefix="ea-test-slack-sessions-boundary-"))
EA_DIR = SANDBOX / "ea"
OWNER = SANDBOX / "owner"
(EA_DIR / "state").mkdir(parents=True)
OWNER.mkdir(mode=0o700)
os.environ["EA_DIR"] = str(EA_DIR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import resident_io as rio  # noqa: E402
import slack_sessions as ss  # noqa: E402

DECOY_TS = "1788000000.999999"
DECOY_SID = "dddddddd-0000-4000-8000-000000000001"
NEW_SID = "aaaaaaaa-0000-4000-8000-000000000002"
MODES = ("resident", "child", "fallback")


def _popen(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))


def _as(monkeypatch, mode: str, spawn=_popen) -> None:
    monkeypatch.setattr(rio, "_owner_check", lambda: mode != "resident")
    monkeypatch.setattr(rio, "_availability", lambda: mode == "child")
    monkeypatch.setattr(rio, "_spawn", spawn)
    monkeypatch.setattr(rio, "_child_file", lambda: None)
    monkeypatch.setattr(rio, "_WARNED", set())


def _registry(monkeypatch) -> Path:
    case = EA_DIR / "state" / f"case-{uuid.uuid4().hex[:12]}"
    case.mkdir()
    path = case / "slack-sessions.json"
    monkeypatch.setattr(ss, "REGISTRY", path)
    return path


def _row(sid: str, **meta) -> dict:
    # Stamped now: the registry treats a row older than MAX_AGE_DAYS as
    # not resumable, so a fixed date would age the fixture out.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {"session_id": sid, "created": now, "last_used": now, "meta": meta}


def _decoy(mark: str) -> Path:
    """A registry-shaped file only the owner should read or change."""
    home = OWNER / uuid.uuid4().hex[:12]
    home.mkdir(mode=0o700)
    decoy = home / "decoy.json"
    decoy.write_text(json.dumps({DECOY_TS: _row(DECOY_SID, title=mark)}))
    os.chmod(decoy, 0o600)
    return decoy


def _snapshot(p: Path) -> tuple:
    st = os.lstat(p)
    return p.read_bytes(), st.st_mode, st.st_ino, st.st_mtime_ns


def _holding(root: Path, mark: str, decoy: Path) -> list[str]:
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


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("mode", MODES)
def test_a_link_at_the_registry_name_hands_over_nothing(mode, kind, monkeypatch):
    _as(monkeypatch, mode)
    reg = _registry(monkeypatch)
    mark = f"decoy-{uuid.uuid4().hex}"
    decoy = _decoy(mark)
    if kind == "symlink":
        reg.symlink_to(decoy)
    else:
        os.link(decoy, reg)
    before = _snapshot(decoy)

    assert ss.describe(DECOY_TS) is None
    assert ss.find_thread(DECOY_SID) == ""
    assert ss.has(DECOY_TS) is False
    assert [r for r in ss.recent(days=3650) if r["thread_ts"] == DECOY_TS] == []
    sid, resumable, meta = ss.lookup(DECOY_TS)
    assert sid != DECOY_SID and not resumable and meta == {}
    ss.adopt("1788000000.000001", NEW_SID, require_file=False)
    ss.remember(DECOY_TS, channel="C0MAIN")
    ss.set_title(DECOY_TS, "renamed")
    ss.mark_used(DECOY_TS)
    ss.invalidate(DECOY_TS)
    ss.prune(max_age_days=0)

    assert _snapshot(decoy) == before
    assert _holding(reg.parent, mark, decoy) == []


@pytest.mark.parametrize("mode", MODES)
def test_the_registry_works_with_its_name_clear(mode, monkeypatch):
    _as(monkeypatch, mode)
    reg = _registry(monkeypatch)
    sid, resumable, _ = ss.lookup("t1")
    assert not resumable
    assert ss.lookup("t1")[:2] == (sid, True)
    ss.adopt("t2", NEW_SID, require_file=False, model="m")
    assert ss.describe("t2")["meta"]["model"] == "m"
    assert set(json.loads(reg.read_text())) == {"t1", "t2"}
    assert not reg.is_symlink()


def test_a_registry_that_cannot_be_read_is_not_written_over(monkeypatch):
    """The owner's read fails (here the child exits with an error): the
    rows on disk stay as they are and every mutation leaves them alone."""
    reg = _registry(monkeypatch)
    rows = {"t1": _row("11111111-0000-4000-8000-000000000001", title="kept"),
            "t2": _row("22222222-0000-4000-8000-000000000002")}
    reg.write_text(json.dumps(rows))
    before = _snapshot(reg)
    _as(monkeypatch, "child",
        spawn=lambda argv: _popen(["/usr/bin/python3", "-c", "import sys; sys.exit(3)"]))

    assert ss.describe("t1") is None
    sid, resumable, _ = ss.lookup("t3")
    assert not resumable and ss.SESSION_ID_RE.match(sid)
    ss.adopt("t4", NEW_SID, require_file=False)
    ss.remember("t1", channel="C0MAIN")
    ss.set_title("t1", "renamed")
    ss.mark_used("t1")
    ss.invalidate("t2")
    ss.prune(max_age_days=0)

    assert _snapshot(reg) == before


def test_rows_in_another_shape_are_left_out(monkeypatch):
    _as(monkeypatch, "resident")
    reg = _registry(monkeypatch)
    good = "33333333-0000-4000-8000-000000000003"
    reg.write_text(json.dumps({
        "good": _row(good, title="good"),
        "not-a-row": ["x"],
        "no-id": {"created": "2026-09-29T12:00:00+00:00"},
        "id-not-a-string": _row("x") | {"session_id": 7},
        "id-a-flag": _row("--print"),
        "meta-a-list": _row("44444444-0000-4000-8000-000000000004") | {"meta": ["x"]},
    }))
    for ts in ("not-a-row", "no-id", "id-not-a-string", "id-a-flag", "meta-a-list"):
        assert ss.describe(ts) is None
        assert ss.has(ts) is False
    assert [r["thread_ts"] for r in ss.recent(days=3650)] == ["good"]
    assert ss.find_thread("--print") == ""
    sid, resumable, meta = ss.lookup("meta-a-list")
    assert not resumable and meta == {} and ss.SESSION_ID_RE.match(sid)
    ss.remember("meta-a-list", channel="C0MAIN")
    assert ss.describe("meta-a-list")["meta"] == {"channel": "C0MAIN"}
    assert ss.lookup("good")[:2] == (good, True)


def test_a_registry_that_is_not_json_is_replaced(monkeypatch):
    _as(monkeypatch, "resident")
    reg = _registry(monkeypatch)
    reg.write_bytes(b"\xff{not json")
    sid, resumable, _ = ss.lookup("t1")
    assert not resumable
    assert json.loads(reg.read_text())["t1"]["session_id"] == sid
