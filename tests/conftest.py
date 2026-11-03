"""Shared pytest fixtures.

Script-style test files (they run under `python3 tests/<file>.py`, which is
how the test wall runs any file with a __main__ block) hand every test a
fresh temporary directory as `d`. Under pytest the same tests get it here.

Many test files set os.environ["EA_DIR"] when they are imported. The wall
gives each file its own process; one pytest process imports every file
before it runs any test, so the last file's EA_DIR would be the one every
test sees. The value each file set is recorded as it is collected and put
back while that file's tests run.
"""
import os
import tempfile

import pytest

# Temp paths with no symlinked ancestor: the tree checks open each component
# without following a link, and macOS keeps /var behind one.
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

# A hook exports git's repository variables (GIT_DIR, GIT_INDEX_FILE,
# GIT_WORK_TREE, ...). Fixtures build their own repos with plain git, which
# would act on the real repository under them, so no test inherits any.
for _name in [n for n in os.environ if n.startswith("GIT_")]:
    del os.environ[_name]

# On macOS the temp dir is /var/folders/..., and /var is a symlink to
# /private/var. Code that resolves paths (or refuses a path with a link in
# it) then disagrees with a test holding the unresolved name. Every test,
# and every subprocess it starts, gets the resolved temp dir instead.
import tempfile as _tf
_tf.tempdir = os.path.realpath(_tf.gettempdir())
os.environ["TMPDIR"] = _tf.tempdir

# The owner-private dir defaults to ~/.ea-private in the real passwd home
# (owner_trust.private_dir), which no test may touch. Any test that does
# not set its own gets a throwaway one for the whole session.
if not os.environ.get("EA_PRIVATE_DIR"):
    import tempfile as _tempfile
    os.environ["EA_PRIVATE_DIR"] = _tempfile.mkdtemp(prefix="ea-private-test-")
# claude_cli probes the installed CLI's --help (as ea-bot, through sudo)
# for its bare-mode opt-out. No test command line should start a real CLI.
os.environ.setdefault("EA_CLAUDE_PROBE", "off")

# The calendar and scheduling tests state their clocks in one zone, the
# first install's. A host or runner in another zone runs the suite in it:
# set here for pytest, and by run_wall.py for the script-style files.
# Constructing MSAL performs remote discovery even when every API verb is mocked.
import msal
from unittest.mock import MagicMock
msal.PublicClientApplication = MagicMock()
msal.ConfidentialClientApplication = MagicMock()
os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-session-"))

SUITE_TZ = "America/Denver"
if not os.environ.get("TZ"):
    import time as _time
    os.environ["TZ"] = SUITE_TZ
    _time.tzset()


def resident_child_available() -> bool:
    """True on a host with the two-user install: the ea-bot user exists
    and this user may run commands as it through sudo without a password,
    which is what an owner process's resident child needs. A bare CI
    runner has neither; the tests of that boundary skip there and run in
    the Docker image and on an installed host."""
    import pwd
    import shutil
    import subprocess
    try:
        pwd.getpwnam("ea-bot")
    except KeyError:
        return False
    if not shutil.which("sudo"):
        return False
    try:
        r = subprocess.run(["sudo", "-n", "-u", "ea-bot", "true"], capture_output=True,
                           timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# For a file or a test that needs the resident child:
#   pytestmark = pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)
NEEDS_RESIDENT = "needs the ea-bot user and passwordless sudo to it (the two-user install)"

_EA_DIR_BY_FILE: dict[str, str | None] = {}


def pytest_sessionstart(session):
    """Old sandboxes of earlier runs leave /tmp (_tmp_retention.py)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _tmp_retention
    _tmp_retention.prune()


def pytest_collectreport(report):
    if report.nodeid.endswith(".py"):
        _EA_DIR_BY_FILE[report.nodeid] = os.environ.get("EA_DIR")


@pytest.fixture(autouse=True, scope="module")
def _file_ea_dir(request):
    want = _EA_DIR_BY_FILE.get(request.node.nodeid)
    before = os.environ.get("EA_DIR")
    if want is not None:
        os.environ["EA_DIR"] = want
    yield
    if before is None:
        os.environ.pop("EA_DIR", None)
    else:
        os.environ["EA_DIR"] = before


@pytest.fixture
def d(tmp_path):
    return tmp_path


# The draft-events journal path is fixed when draft_events is first
# imported, so a test file that sets EA_DIR after another file imported it
# still writes to the REAL journal. The bot reads that journal and posts a
# Send card per entry: on 2026-10-08 a fixture draft id "d1" reached Slack
# that way. Every test whose journal resolves to the real one gets a
# throwaway journal instead. Both import names are loaded here so a later
# first import cannot slip past this.
import sys as _sys
from pathlib import Path as _Path

_PREPROCESSOR = str(_Path(__file__).resolve().parent.parent / "preprocessor")
if _PREPROCESSOR not in _sys.path:
    _sys.path.append(_PREPROCESSOR)
# run_wall.py imports this file outside pytest, without the repo root on
# the path that `from preprocessor import ...` below needs.
_ROOT = str(_Path(__file__).resolve().parent.parent)
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)
import draft_events as _de_bare  # noqa: E402
from preprocessor import draft_events as _de_pkg  # noqa: E402

_REAL_EA = (_Path.home() / ".ea").resolve()


@pytest.fixture(autouse=True)
def _no_real_draft_journal(tmp_path, monkeypatch):
    for mod in {id(m): m for m in (_de_bare, _de_pkg,
                                   _sys.modules.get("draft_events"),
                                   _sys.modules.get("preprocessor.draft_events"))
                if m is not None}.values():
        try:
            real = _REAL_EA in _Path(mod.JOURNAL).resolve().parents
        except OSError:
            real = True
        if real:
            monkeypatch.setattr(mod, "JOURNAL", tmp_path / "draft-events.jsonl")
    yield


@pytest.fixture
def walks_stop_at_tmp_path(tmp_path, monkeypatch):
    """The eval guard's walks up a path (provider._work_tree_of and
    claude_cli.memory_on_path) report, for a path inside tmp_path, only what
    is inside tmp_path. tmp_path sits in the system temp dir, where any user
    can make a `.git` entry or a memory file (Codex's sandbox leaves empty
    `.git` folders in the folders a run may write), so what is above it is
    the host's, not the test's. For any other path (the checkout) the walks
    are the real ones, and every refusal the guard makes is unchanged."""
    _evals = str(_Path(__file__).resolve().parent / "evals")
    if _evals not in _sys.path:
        _sys.path.insert(0, _evals)
    import claude_cli
    import provider
    base = _Path(os.path.realpath(tmp_path))

    def inside(p) -> bool:
        p = _Path(os.path.abspath(p))
        return p == base or base in p.parents

    real_tree, real_memory = provider._work_tree_of, claude_cli.memory_on_path

    def work_tree_of(path):
        found = real_tree(path)
        return None if found is not None and inside(path) and not inside(found) else found

    def memory_on_path(cwd):
        found = real_memory(cwd)
        return [p for p in found if inside(p)] if inside(cwd) else found

    monkeypatch.setattr(provider, "_work_tree_of", work_tree_of)
    monkeypatch.setattr(claude_cli, "memory_on_path", memory_on_path)
    return base


@pytest.fixture(autouse=True)
def _no_resident_codex_dirs(monkeypatch):
    """codex_ghost.ensure_codex_tmp and ensure_lane_cwd make folders in the
    resident's real state through sudo, and no test may. Every test gets
    stand-ins that make nothing and name the same folders; a test of the
    real ones keeps references taken when its file is imported."""
    for name in ("codex_ghost", "preprocessor.codex_ghost"):
        mod = _sys.modules.get(name)
        if mod is None:
            continue
        for fn, const in (("ensure_codex_tmp", "CODEX_TMP"), ("ensure_lane_cwd", "LANE_CWD")):
            if hasattr(mod, fn):
                monkeypatch.setattr(mod, fn, lambda runner=None, _m=mod, _c=const: getattr(_m, _c))
    yield
