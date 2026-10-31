"""Tripwire: an owner-side writer never touches a decoy behind its target name.

The resident owns every directory in its space, so it can put a symlink or a
hard link at any name there, pointing at a file only the owner should change.
Every writer in WRITERS runs as an owner process would, in a sandbox: once
with the name clear (it must write its target), and once per decoy:

  symlink     a symlink at the target name, to the decoy
  hardlink    a hard link of the decoy at the target name
  sidenames   symlinks to the decoy at the names a writer could derive from
              the target (.tmp, .lock, .new, .cursor, SQLite's -journal, ...)

in each way an owner process reaches resident space:

  child       through the resident_io child, the inline form
  child-file  through the resident_io child, its own file
              (preprocessor/resident_io_child.py)
  fallback    the sudo preflight failed: the direct O_NOFOLLOW and atomic
              writes

After each run the decoy has the same bytes, mode, inode and mtime, nothing
new sits beside it, and its marker is in no file of the case directory (a
read through the link would carry it there).

Adding a writer is one line in WRITERS: W(module, call, attr=, name=, ...).
`call(m, p)` gets the imported module and the target path. `attr` names a
module attribute (dotted to reach one on a module it imports, as M.QUEUE)
that is pointed into the case directory for the run, under its own
basename; `name` is the
target's path in the case directory (a string, or a function of the module),
by default the attribute's. `also` names more attributes to point into the
case directory, `owner_seam` a module's own owner decision to answer yes, and
`known` marks a writer that reads its target by path before replacing it,
with the class of the gap; its link cases are expected failures until it is
fixed. A module that gains an owner write must be listed here or in UNLISTED.

The child runs as the test user through resident_io's spawn seam; no test
runs sudo, and every path a writer is pointed at is in the sandbox.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

SANDBOX = Path(tempfile.mkdtemp(prefix="ea-test-tripwire-"))
EA_DIR = SANDBOX / "ea"
PRIV = SANDBOX / "private"
OWNER = SANDBOX / "owner"
os.environ["EA_DIR"] = str(EA_DIR)
for _sub in ("config", "state", "logs"):
    (EA_DIR / _sub).mkdir(parents=True, exist_ok=True)
PRIV.mkdir(mode=0o700)
OWNER.mkdir(mode=0o700)

import resident_io as rio  # noqa: E402

CHILD_FILE = ROOT / "preprocessor" / "resident_io_child.py"
BODY = " ".join(["word"] * 20)
ONE_TS = "1712345678.123456"
ONE_SESSION = "00000000-0000-4000-8000-000000000000"
READS_BY_PATH = "reads its resident-space target by path before replacing it"


@dataclass(frozen=True)
class W:
    """One owner-side writer (see the module docstring)."""
    module: str
    call: Callable
    attr: str | None = None
    name: str | Callable | None = None
    also: tuple = ()
    owner_seam: str | None = None
    known: str | None = None


def _staged(path: Path) -> Path:
    """A job file the owner finds in place, for the dead-letter move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def _veto(m, p: Path) -> None:
    store = m.VetoStore.load(p)
    store.record_close("e1", "signal")
    store.save()


def _plugin_ctx(m, plugins_dir: Path):
    """An ea-plugin context whose resident tree is the case directory."""
    return m.Context(private=PRIV, agent_dir=plugins_dir, ea_dir=plugins_dir.parent,
                     local_copy=True, out=lambda line: None)


def _plugin_release() -> Path:
    """A one-file plugin release in the owner's staging space."""
    src = Path(tempfile.mkdtemp(dir=OWNER))
    (src / "plugin.json").write_text('{"name": "x"}')
    return src


WRITERS = [
    # resident_io: appends
    W("item_feedback", lambda m, p: m._append({"k": 1}), attr="FEEDBACK_LOG"),
    W("open_claims_audit", lambda m, p: m._append({"k": 1}), attr="AUDIT_LOG"),
    W("log_missed_closure", lambda m, p: m._append({"k": 1}), attr="LOG_PATH"),
    W("closure_scrubber", lambda m, p: m._append_jsonl(p, [{"k": 1}]), name="logs/closure-scrubber.log"),
    W("list_digest", lambda m, p: m._append_history({"k": 1}), attr="HISTORY_FILE"),
    W("alert", lambda m, p: m._stage({"id": "x"}), attr="PENDING"),
    W("draft_events", lambda m, p: m.record("created", "d1", "create-draft"), attr="JOURNAL"),
    W("compile_letter", lambda m, p: m.record_build(Path("x.tex"), _staged(Path(tempfile.mkdtemp(dir=OWNER)) / "x.pdf")),
      attr="BUILDS_FILE"),
    # resident_io: read-modify-write under the file's lock
    W("calendar_reconciler", lambda m, p: m.log_release("series-1", "Focus"), attr="DECISIONS_FILE"),
    W("scheduling_engine", lambda m, p: m._append_decision({"k": 1}), attr="DECISIONS_FILE"),
    W("preferences", lambda m, p: m.update(lambda d: d["calibration"].append({"note": "x"}), p), name="config/preferences.json"),
    W("quiet_hours", lambda m, p: m.defer("ea-assistant", "x"), attr="QUEUE_FILE"),
    W("quiet_hours", lambda m, p: m.set_hour_override("morning-briefing", "2026-09-29", 7), attr="HOUR_OVERRIDE_FILE"),
    W("ea_channels", lambda m, p: m._mutate(lambda rows: rows.update(C1={"a": 1}), p), name="config/ea-channels.json"),
    # resident_io: replace
    W("draft_events", lambda m, p: m._write_cursor(7), attr="JOURNAL", name="draft-events.jsonl.cursor"),
    W("draft_capture", lambda m, p: m.capture("d1", BODY), attr="STORE", name=lambda m: f"draft-bodies/{m._key('d1')}.json"),
    W("quiet_hours", lambda m, p: m._write_travel_state({"active": False}), attr="TRAVEL_STATE_FILE"),
    W("slack_outbox", lambda m, p: m._write_sidecar(p, {"k": 1}), name="outbox/job-1.json"),
    W("style_analysis", lambda m, p: m.save({"window": {"to": "2026-09"}}), attr="LATEST"),
    W("alert", lambda m, p: m._rewrite_pending([{"id": "x"}]), attr="PENDING"),
    W("page_watch", lambda m, p: m._save(p, [{"id": "x"}]), name="state/page-watches.json"),
    W("mail_privacy", lambda m, p: m._write_reduced("{}"), attr="REDUCED_BRIEFING"),
    W("remote_digests", lambda m, p: m._write_report("list-digest", "x"), attr="REPORT_DIR",
      name="reports/list-digest-latest.md"),
    W("install_overlay", lambda m, p: m.render(str(p)), name="config/CLAUDE.install.md"),
    W("remote_briefing", lambda m, p: m.queue_escalations(
        [{"conversation_id": f"c-{uuid.uuid4().hex}", "queued_at": m._now().isoformat()}],
        agent="morning-briefing", now=m._now()), attr="ESCALATIONS"),
    W("mail_mediator", lambda m, p: m._respond("r1", "", "", 0), attr="M.QUEUE", name="mail-queue/resp-r1.json", also=("AUDIT",)),
    # resident_io: lock files, directories and renames
    W("draft_events", lambda m, p: os.close(m._open_lock(p)), attr="JOURNAL", name="draft-events.jsonl.lock"),
    W("graph_actions", lambda m, p: os.close(m._open_lock(p)), name="state/pending-confirmations.lock"),
    W("scheduled_sends", lambda m, p: os.close(m._open_sends_lock(p)), attr="SENDS_FILE", name="scheduled-sends.lock"),
    W("ledger", lambda m, p: m.guarded_connect(p).close(), name="state/obligations-ledger.db", owner_seam="_is_owner"),
    W("slack_outbox", lambda m, p: m._deadletter(p.parent, _staged(p.parent / "job-1.json"), None), name="outbox/failed"),
    # atomic_io
    W("quiet_hours", lambda m, p: m.due_local("tripwire", m.local_hour()), attr="LAST_RUN_FILE"),
    W("graph_actions", lambda m, p: m._write_state_json(p, {"k": 1}, mode=0o664), name="state/pending-confirmations.json"),
    W("graph_actions", lambda m, p: m._save_snoozes(p, [{"k": 1}]), name="state/snoozed-messages.json"),
    W("scheduled_sends", lambda m, p: m._save_sends([{"draft_id": "d1"}]), attr="SENDS_FILE"),
    W("preprocess", lambda m, p: m.save_seen_messages({"m1"}), attr="SEEN_MESSAGES_FILE"),
    W("preprocess", lambda m, p: m.save_seen_conversations({"c1": "2026-09-29"}), attr="SEEN_CONVERSATIONS_FILE"),
    W("preprocess", lambda m, p: m.save_surfaced_responses({"c1": ""}), attr="SURFACED_RESPONSES_FILE"),
    W("closure_scrubber", _veto, name="state/closure-scrubber-vetoes.json"),
    W("calendar_reconciler", lambda m, p: m._write_gap_warned(["k"]), attr="GAP_WARNED_FILE"),
    W("admissions_filter", lambda m, p: m.save_state(m._empty_state(), p), attr="STATE_FILE"),
    W("mail_tagger", lambda m, p: m.save_state(m._empty_state(), p), attr="STATE_FILE"),
    W("preferences", lambda m, p: m.write_digest_cursor({"a": 1}, p), name="state/preferences-digest-seen.json"),
    W("onboarding", lambda m, p: m._update_state(lambda s: s, ea_dir=p.parents[1]), name="state/onboarding.json"),
    W("outbound_review", lambda m, p: m.write_cursor("2026-09-29T00:00:00", p), name="state/outbound-review-seen.json"),
    W("resident_context", lambda m, p: m._write_state({"threads": {}}), attr="STATE_FILE"),
    W("resident_bridge", lambda m, p: m._save_router_state({"posted": []}), attr="ROUTER_STATE_FILE"),
    W("slack_sessions", lambda m, p: m._mutate(lambda rows: True), attr="REGISTRY"),
    W("retry_policy", lambda m, p: m._save_all({}), attr="STATE_FILE"),
    # atomic_io after a read of the target with the resident's access
    W("retry_policy", lambda m, p: m.CircuitBreaker("tripwire").record_failure("x"), attr="STATE_FILE"),
    W("slack_sessions", lambda m, p: m.adopt(ONE_TS, ONE_SESSION, require_file=False), attr="REGISTRY"),
    # plugins (docs/plugins.md): the click token, the verb alias hint, an
    # agent-zone release written into the resident's plugins dir
    W("plugin_mail", lambda m, p: m.deliver("x", "agent", [{"id": "m1", "received": "",
      "delivered_at": "2026-10-18T00:00:00Z"}], datetime(2026, 10, 18, tzinfo=timezone.utc),
      ea_dir=p.parents[2]), name="state/plugin-mail/x.json"),
    W("plugin_cards", lambda m, p: m.mint_token("p", "c", {"x": 1}, path=p),
      name="state/plugin-card-tokens.json"),
    W("plugin_install", lambda m, p: _plugin_ctx(m, p.parent.parent / "plugins")._write_alias_hint(
        {"plugins": {}}), name="state/plugin-verbs.json"),
    W("plugin_install", lambda m, p: m._place_agent(_plugin_ctx(m, p.parent.parent),
                                                    _plugin_release(), p.parent.name, []),
      name="plugins/x/plugin.json"),
]

# Modules with an owner write that no line above runs, and why.
UNLISTED = {
    "ea-chat-bot.py": "its writers run inside the bot's event handlers "
                       "(tests/test_owner_writes_resident_space.py pins them)",
    "botcore/botcore/chat/slack_frontend.py": "downloads, written through chat.files (resident_io in the EA)",
    "botcore/botcore/chat/zulip_frontend.py": "downloads, written through chat.files (resident_io in the EA)",
    "preprocessor/graph_client.py": "the --align --write path of its CLI, after a Graph sign-in",
    "preprocessor/resident_io.py": "the mechanism itself (tests/test_resident_io.py)",
    "scripts/boundary-audit.py": "its one write is its own state file, in the owner's home",
    "scripts/closure-metrics-checkin.py": "a one-off check-in script",
    "scripts/update.py": "its one write in resident space is the EA's copy of the update "
                         "status, through resident_io.replace_text (tests/test_update.py)",
}

MODES = ("child", "child-file", "fallback")
KINDS = ("symlink", "hardlink", "sidenames")
SIDE_SUFFIXES = (".tmp", ".lock", ".new", ".bak", ".old", ".part", ".cursor", "~",
                 "-journal", "-wal", "-shm")

_WRITE_CALL = re.compile(
    r"\b(?:_rio|resident_io|_resident_io)\.(?:append_text|replace_text|replace_bytes|"
    r"update_text|make_dir|ensure_file|move)\(|\b(?:_atomic|atomic_io)\.write_(?:text|json)\(")


# ── the harness ───────────────────────────────────────────────────────

def _label(i: int, w: W) -> str:
    where = w.name if isinstance(w.name, str) else w.attr
    return f"{i:02d}-{w.module}-{Path(str(where)).name}"


def _pipes(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))


@pytest.fixture(autouse=True, scope="module")
def _private_sandbox():
    """The owner-private dir is the sandbox's while this file runs."""
    before = os.environ.get("EA_PRIVATE_DIR")
    os.environ["EA_PRIVATE_DIR"] = str(PRIV)
    yield
    if before is None:
        os.environ.pop("EA_PRIVATE_DIR", None)
    else:
        os.environ["EA_PRIVATE_DIR"] = before


def _owner(monkeypatch, mode: str) -> list:
    """This process is the owner; the child runs as the test user in the
    child modes, and the preflight fails in the fallback mode. Returns the
    program part (argv[2:4]) of each resident_io child started."""
    programs: list = []

    def spawn(argv):
        if argv[2] == str(CHILD_FILE) or argv[2:4] == ["-c", rio._CHILD]:
            programs.append(argv[2:4] if argv[2] == "-c" else argv[2:3])
        return _pipes(argv)

    monkeypatch.setattr(rio, "_owner_check", lambda: True)
    monkeypatch.setattr(rio, "_availability", lambda: mode != "fallback")
    monkeypatch.setattr(rio, "_spawn", spawn)
    monkeypatch.setattr(rio, "_child_file",
                        (lambda: str(CHILD_FILE)) if mode == "child-file" else (lambda: None))
    monkeypatch.setattr(rio, "_WARNED", set())
    return programs


def _point(monkeypatch, m, dotted: str, case: Path) -> Path:
    """Point module attribute `dotted` into `case` under its own basename."""
    *path, leaf = dotted.split(".")
    obj = m
    for part in path:
        obj = getattr(obj, part)
    orig = getattr(obj, leaf)
    new = case / Path(orig).name
    monkeypatch.setattr(obj, leaf, new if isinstance(orig, Path) else str(new))
    return new


def _case(w: W, monkeypatch) -> tuple[object, Path]:
    """Import the writer's module, point its attributes into a fresh case
    directory in the EA tree, and return the module and the target path."""
    m = importlib.import_module(w.module)
    case = EA_DIR / f"case-{uuid.uuid4().hex[:12]}"
    case.mkdir()
    target = _point(monkeypatch, m, w.attr, case) if w.attr else None
    for dotted in w.also:
        _point(monkeypatch, m, dotted, case)
    if w.owner_seam:
        monkeypatch.setattr(m, w.owner_seam, lambda: True)
    name = w.name(m) if callable(w.name) else w.name
    if name:
        target = case / name
    assert target is not None, f"{w.module}: give attr= or name="
    target.parent.mkdir(parents=True, exist_ok=True)
    return m, target


def _side_names(p: Path) -> list[Path]:
    names = {p.with_name(p.name + s) for s in SIDE_SUFFIXES}
    names |= {p.with_name("." + p.name + s) for s in (".tmp", ".lock", ".swp")}
    if p.suffix:
        names |= {p.with_suffix(s) for s in (".tmp", ".lock", ".new")}
    names.discard(p)
    return sorted(names)


def _plant(kind: str, target: Path) -> tuple[Path, str]:
    """A decoy the owner alone should change, and the links to it."""
    home = OWNER / uuid.uuid4().hex[:12]
    home.mkdir(mode=0o700)
    mark = f"tripwire-decoy-{uuid.uuid4().hex}"
    decoy = home / "decoy.json"
    decoy.write_text(json.dumps({mark: mark, "owner": "only"}) + "\n")
    os.chmod(decoy, 0o600)
    if kind == "symlink":
        target.symlink_to(decoy)
    elif kind == "hardlink":
        os.link(decoy, target)
    else:
        for side in _side_names(target):
            side.symlink_to(decoy)
    return decoy, mark


def _snapshot(decoy: Path) -> tuple:
    st = os.lstat(decoy)
    return (decoy.read_bytes(), st.st_mode, st.st_ino, st.st_mtime_ns, st.st_uid,
            sorted(os.listdir(decoy.parent)))


def _files_holding(root: Path, mark: str, decoy: Path) -> list[str]:
    """Files under `root`, other than links to the decoy, that hold `mark`."""
    own = os.stat(decoy)
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino):
                    continue
                if mark.encode() in p.read_bytes():
                    hits.append(str(p.relative_to(root)))
            except OSError:
                continue
    return hits


def _cases(with_kind: bool):
    for i, w in enumerate(WRITERS):
        for mode in MODES:
            if not with_kind:
                yield pytest.param(w, mode, id=f"{_label(i, w)}-{mode}")
                continue
            for kind in KINDS:
                marks = ()
                if w.known and kind != "sidenames":
                    marks = pytest.mark.xfail(strict=True, reason=w.known)
                yield pytest.param(w, mode, kind, id=f"{_label(i, w)}-{mode}-{kind}", marks=marks)


# ── the tripwire ──────────────────────────────────────────────────────

@pytest.mark.parametrize("w, mode", list(_cases(False)))
def test_the_writer_writes_its_target(w, mode, monkeypatch):
    """The line runs the writer: with the name clear, the target appears,
    and every resident_io child ran the program the mode names."""
    programs = _owner(monkeypatch, mode)
    m, target = _case(w, monkeypatch)
    w.call(m, target)
    assert os.path.lexists(target), f"{w.module} did not write {target.name}"
    assert not target.is_symlink()
    want = {"child": [["-c", rio._CHILD]], "child-file": [[str(CHILD_FILE)]], "fallback": []}[mode]
    assert all(p in want for p in programs), f"{mode}: a child ran {programs[0][:1]}"


@pytest.mark.parametrize("w, mode, kind", list(_cases(True)))
def test_the_decoy_behind_the_name_is_never_touched(w, mode, kind, monkeypatch):
    _owner(monkeypatch, mode)
    m, target = _case(w, monkeypatch)
    decoy, mark = _plant(kind, target)
    before = _snapshot(decoy)
    try:
        w.call(m, target)
    except Exception:
        pass  # a refusal may raise; the decoy is what is checked
    assert _snapshot(decoy) == before, f"{w.module} changed the decoy behind {target.name}"
    leaked = _files_holding(EA_DIR / target.relative_to(EA_DIR).parts[0], mark, decoy)
    assert not leaked, f"{w.module} carried the decoy's content into {leaked}"


@pytest.mark.parametrize("mode", ["child", "child-file"])
def test_the_child_modes_reach_the_child(mode, monkeypatch):
    programs = _owner(monkeypatch, mode)
    m, target = _case(WRITERS[0], monkeypatch)
    WRITERS[0].call(m, target)
    assert programs == [["-c", rio._CHILD] if mode == "child" else [str(CHILD_FILE)]]


def test_every_writer_has_its_own_label():
    labels = [_label(i, w) for i, w in enumerate(WRITERS)]
    assert len(set(labels)) == len(labels)


def test_every_module_with_an_owner_write_is_listed():
    """A module that gains a resident_io or atomic_io write gets a line in
    WRITERS (or a reason in UNLISTED)."""
    listed = {f"preprocessor/{w.module.replace('.', '/')}.py" for w in WRITERS}
    sources = sorted((ROOT / "preprocessor").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    sources.append(ROOT / "ea-chat-bot.py")
    missing = []
    for src in sources:
        rel = str(src.relative_to(ROOT))
        if "/tests/" in f"/{rel}" or rel in listed or rel in UNLISTED:
            continue
        if _WRITE_CALL.search(src.read_text(errors="replace")):
            missing.append(rel)
    assert not missing, f"add a W(...) line to WRITERS (or a reason to UNLISTED) for: {missing}"
    stale = [rel for rel in UNLISTED if not (ROOT / rel).exists()]
    assert not stale, f"UNLISTED names files that are gone: {stale}"
