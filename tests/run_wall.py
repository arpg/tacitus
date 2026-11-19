#!/usr/bin/env python3
"""Discovery-based test wall: run EVERY tests/test_*.py, however styled.

Discovery finds every test file, and the structural dead-zone check
finds tests defined below a file's __main__ guard, which never run.

Per file:
  - has `if __name__` -> run `python3 <file>`; else -> pytest -q.
  - FAIL on: nonzero exit, "N failed" with N>0, pytest failures, or any
    top-level `def test_` BELOW the __main__ guard (dead zone).
Exit 0 only when every file passes. The post-commit hook calls this
once instead of maintaining a per-file block list.

Two lanes run at once: the browser files (test_webui*.py, each with its
own Chromium; they wait on a page far more than they compute) a few at a
time, and every other file one at a time, as before. Each file's line
carries its time; a failing file's line is followed by its failing tests,
and its whole output is kept in LOG_DIR. A file slower than SOFT_LIMIT is
named at the end: split it by area before it nears TIMEOUT.

  python3 tests/run_wall.py            every file
  python3 tests/run_wall.py --browser  the browser files alone
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIMEOUT = 240
# A file past this many seconds is named at the end of the run, long before
# it nears TIMEOUT (CONTRIBUTING.md, Tests).
SOFT_LIMIT = 90
# The browser files: the app's tests (tests/webui_browser.py has their fixtures).
BROWSER = "test_webui*.py"
# WALL_VERBOSE=1 prints the tail of a failing file's output, so a run on a
# machine no one is sitting at (a CI runner) still shows the traceback.
VERBOSE = bool(os.environ.get("WALL_VERBOSE"))
TAIL_LINES = 60
# A failing file's whole output, named by the file and this run's process
# (walls in several checkouts can run at once); a day later the next run
# removes it.
LOG_DIR = Path(os.environ.get("WALL_LOG_DIR") or Path(tempfile.gettempdir()) / f"tacitus-wall-{os.getuid()}")
LOG_KEEP_S = 86400
_PRINT = threading.Lock()


def _test_env() -> dict:
    """The environment for a test file: this one without git's repository
    variables (a hook exports GIT_DIR, GIT_INDEX_FILE and similar, and a
    fixture's git would otherwise act on the real repository), in the zone
    the suite states its clocks in (conftest.SUITE_TZ) unless one is set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if not env.get("TZ"):
        sys.path.insert(0, str(TESTS))
        import conftest
        env["TZ"] = conftest.SUITE_TZ
    return env


def browser_jobs() -> int:
    """How many browser files run at once: WALL_BROWSER_JOBS, else half the
    CPUs the load average leaves free, at least one and at most four. On a
    busy machine the browser files run one at a time, beside the rest."""
    env = os.environ.get("WALL_BROWSER_JOBS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    # The CPUs this process may run on (a container's or a taskset's), where the system says.
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    return max(1, min(4, round((cpus - load) / 2)))


def failing_tests(out: str) -> list:
    """The tests a file's output names as failed: pytest's FAILED and ERROR
    lines, and a script-style file's FAIL lines."""
    ids = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
    ids += re.findall(r"^\s*FAIL:?\s+([A-Za-z_][\w.\[\]-]*)", out, re.M)
    return list(dict.fromkeys(ids))


def check_file(path: Path, env: dict | None = None) -> tuple:
    """(ok, label, output) for one test file."""
    src = path.read_text()
    lines = src.splitlines()
    main_i = next((i for i, l in enumerate(lines)
                   if l.startswith("if __name__")), None)
    if main_i is not None:
        dead = [l for i, l in enumerate(lines)
                if i > main_i and re.match(r"^def test_", l)]
        if dead:
            return False, f"{len(dead)} test(s) DEFINED BELOW the runner", ""
        cmd = [sys.executable, str(path)]
    else:
        cmd = [sys.executable, "-m", "pytest", str(path), "-q"]
    try:
        # No terminal: code that would prompt (a Graph sign-in started from a
        # terminal waits for a browser) fails fast instead of hanging the wall.
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=TIMEOUT, cwd=str(TESTS.parent), env=env or _test_env())
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""  # bytes, whatever `text` says
        return False, f"TIMEOUT after {TIMEOUT}s", out.decode(errors="replace") if isinstance(out, bytes) else out
    out = r.stdout + r.stderr
    m = re.search(r"(\d+) passed(?:, (\d+) failed)?", out)
    skipped = re.search(r"(\d+) skipped", out)
    if r.returncode != 0:
        tail = out.strip().splitlines()[-1][:80] if out.strip() else "no output"
        return False, f"exit {r.returncode}: {tail}", out
    if m and m.group(2) and int(m.group(2)) > 0:
        return False, f"{m.group(2)} failed", out
    if re.search(r"OVERALL:.*\[FAIL\]", out):
        return False, "OVERALL [FAIL]", out
    label = (f"{m.group(1)} passed" if m else
             "ok" if r.returncode == 0 else "?")
    om = re.search(r"OVERALL: (\d+)/(\d+)", out)
    if om:
        label = f"{om.group(1)}/{om.group(2)}"
    if skipped and m:
        label += f", {skipped.group(1)} skipped"
    return True, label, out


def report(path: Path, ok: bool, label: str, out: str, took: float) -> None:
    """One file's line; for a failure, its failing tests, where its output
    is kept and, with WALL_VERBOSE, the output's tail."""
    lines = [f"  {'✅' if ok else '❌'} {path.name:38s} {label}  ({took:.0f} s)"]
    if not ok:
        lines += [f"      ❌ {t}" for t in failing_tests(out)[:20]]
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log = LOG_DIR / f"{path.stem}.{os.getpid()}.log"
            log.write_text(out)
            lines.append(f"      output: {log}")
        except OSError:
            pass
        if VERBOSE:
            lines += [f"      | {line}" for line in out.splitlines()[-TAIL_LINES:]]
    with _PRINT:
        print("\n".join(lines), flush=True)


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, str(TESTS))
    import _tmp_retention
    _tmp_retention.prune()
    for old in LOG_DIR.glob("*.log") if LOG_DIR.is_dir() else ():
        try:
            if time.time() - old.stat().st_mtime > LOG_KEEP_S:
                old.unlink()
        except OSError:
            pass
    env = _test_env()
    files = sorted(p for p in TESTS.glob("test_*.py"))
    browser = [p for p in files if p.match(BROWSER)]
    rest = [] if "--browser" in argv else [p for p in files if p not in browser]
    jobs = browser_jobs()
    print(f"  {len(browser)} browser files, {jobs} at a time, beside {len(rest)} other files one at a time",
          flush=True)
    took: dict = {}
    failures = []

    def run(path: Path) -> None:
        t = time.monotonic()
        ok, label, out = check_file(path, env)
        took[path] = time.monotonic() - t
        report(path, ok, label, out, took[path])
        if not ok:
            failures.append(path)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        lane = [pool.submit(run, p) for p in browser]
        for path in rest:
            run(path)
        for f in lane:
            f.result()
    n = len(browser) + len(rest)
    for path in sorted(took, key=took.get, reverse=True):
        if took[path] > SOFT_LIMIT:
            print(f"  ⚠  {path.name} took {took[path]:.0f} s, past the soft limit of {SOFT_LIMIT} s: "
                  "split it by area (CONTRIBUTING.md, Tests)")
    print(f"\n  wall: {n - len(failures)}/{n} files clean in {time.monotonic() - t0:.0f} s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
