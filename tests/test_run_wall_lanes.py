"""The wall (tests/run_wall.py) runs the browser files a few at a time
beside the other files, gives each file's time, names a failing file's
failing tests and keeps its output, and names a file past the soft limit."""
import re
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import _tmp_retention  # noqa: E402
import run_wall as W  # noqa: E402


def test_a_failing_file_names_its_tests_from_pytest_and_from_a_script():
    out = ("..F\nFAILED tests/test_x.py::test_a[r1] - AssertionError: no\n"
           "ERROR tests/test_x.py::test_b - TimeoutError\n1 failed, 2 passed in 1.0s\n")
    assert W.failing_tests(out) == ["tests/test_x.py::test_a[r1]", "tests/test_x.py::test_b"]
    script = "  PASS test_c\n  FAIL test_d: boom\nFAIL  test_e: 'x'\nFAIL: 3 helpers missing\n"
    assert W.failing_tests(script) == ["test_d", "test_e"]


def test_the_browser_lane_takes_the_jobs_asked_for_or_half_the_free_cpus(monkeypatch):
    monkeypatch.setenv("WALL_BROWSER_JOBS", "3")
    assert W.browser_jobs() == 3
    monkeypatch.delenv("WALL_BROWSER_JOBS")
    for cpus, load, jobs in ((32, 0.5, 4), (4, 0.5, 2), (32, 30.0, 1), (8, 12.0, 1), (2, 0.0, 1)):
        monkeypatch.setattr(W.os, "cpu_count", lambda c=cpus: c)
        monkeypatch.setattr(W.os, "sched_getaffinity", lambda _pid, c=cpus: set(range(c)), raising=False)
        monkeypatch.setattr(W.os, "getloadavg", lambda x=load: (x, x, x))
        assert W.browser_jobs() == jobs, (cpus, load)


def test_the_wall_times_each_file_names_a_failure_and_a_slow_file(tmp_path, monkeypatch, capsys):
    import _tmp_retention
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_webui_a.py", "test_webui_b.py", "test_other.py"):
        (tests / name).write_text("")
    fail = "..F\nFAILED tests/test_webui_b.py::test_flaky - AssertionError\n1 failed, 2 passed in 1.0s\n"
    ran = []

    def check(path, env=None):
        ran.append(path.name)
        return (False, "1 failed", fail) if path.name == "test_webui_b.py" else (True, "3 passed", "3 passed")
    monkeypatch.setattr(W, "TESTS", tests)
    monkeypatch.setattr(W, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(W, "SOFT_LIMIT", -1)
    monkeypatch.setattr(W, "check_file", check)
    monkeypatch.setattr(_tmp_retention, "prune", lambda: None)
    monkeypatch.setenv("WALL_BROWSER_JOBS", "2")
    monkeypatch.setenv("WALL_JOBS", "3")
    assert W.main([]) == 1
    text = capsys.readouterr().out
    assert sorted(ran) == ["test_other.py", "test_webui_a.py", "test_webui_b.py"]
    assert "2 browser files, 2 at a time, beside 1 other files 3 at a time, then 0 alone" in text
    assert re.search(r"✅ test_other\.py +3 passed  \(\d+ s\)", text)
    assert "      ❌ tests/test_webui_b.py::test_flaky" in text  # the post-commit hook shows ❌ lines
    log = next((tmp_path / "logs").glob("test_webui_b.*.log"))
    assert log.read_text() == fail and f"output: {log}" in text
    assert re.search(r"⚠  test_webui_a\.py took \d+ s, past the soft limit", text)
    assert re.search(r"wall: 2/3 files clean in \d+ s", text)
    # --browser runs the browser files alone.
    ran.clear()
    W.main(["--browser"])
    assert sorted(ran) == ["test_webui_a.py", "test_webui_b.py"]


def test_a_passing_file_with_skips_says_so(monkeypatch):
    """The plain CI legs have no Chromium, and their browser tests skip: the
    file's line says how many."""
    import subprocess
    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, "26 passed, 120 skipped in 2.0s\n", ""))
    ok, label, _out = W.check_file(TESTS / "test_webui.py", env={})
    assert ok and label == "26 passed, 120 skipped"


def test_each_file_gets_its_own_temp_dir_kept_only_on_failure(tmp_path, monkeypatch):
    seen = {}

    def fake(path, env):
        seen[path.name] = env["TMPDIR"]
        Path(env["TMPDIR"], "sandbox").write_text("x")
        return (path.name != "test_bad.py", "", "")
    monkeypatch.setattr(W, "_check_file", fake)
    for name in ("test_good.py", "test_bad.py"):
        W.check_file(tmp_path / name, {"PATH": "/usr/bin"})
    assert seen["test_good.py"] != seen["test_bad.py"]
    assert Path(seen["test_bad.py"]).is_dir() and not Path(seen["test_good.py"]).exists()


def test_a_serial_file_runs_after_the_parallel_ones(tmp_path, monkeypatch, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_a.py", "test_b.py", "test_alone.py"):
        (tests / name).write_text("def test_x(): pass\n")
    ran = []
    monkeypatch.setattr(W, "TESTS", tests)
    monkeypatch.setattr(W, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(W, "SERIAL", {"test_alone.py": "a fixed port"})
    monkeypatch.setattr(W, "check_file", lambda path, env=None: ran.append(path.name) or (True, "1 passed", ""))
    monkeypatch.setattr(_tmp_retention, "prune", lambda: None)
    monkeypatch.setenv("WALL_JOBS", "2")
    assert W.main([]) == 0
    assert ran[-1] == "test_alone.py" and sorted(ran[:2]) == ["test_a.py", "test_b.py"]
    assert "then 1 alone" in capsys.readouterr().out
