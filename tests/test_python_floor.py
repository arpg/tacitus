"""Every Python file parses on the floor docs/SETUP.md states, Python 3.10.

The wall usually runs on a newer interpreter, which accepts syntax the
floor rejects (an f-string that nests its own quote type, for one), so a
file can pass here and fail the ubuntu-22.04 leg. When a python3.10 is on
the machine, each file is compiled with it; otherwise the check says so
and leaves the verdict to that leg.
"""
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _files() -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"], capture_output=True, text=True)
    files = [f for f in out.stdout.splitlines() if f]
    return files or [str(p.relative_to(REPO)) for p in REPO.rglob("*.py") if ".git" not in p.parts]


def test_every_file_compiles_on_python_3_10():
    py = shutil.which("python3.10")
    if not py:
        print("  (no python3.10 on this machine; the ubuntu-22.04 CI leg is the check)")
        return
    bad = []
    for f in _files():
        r = subprocess.run([py, "-m", "py_compile", f], cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{f}: {r.stderr.strip().splitlines()[-1][:120]}")
    assert not bad, "\n".join(bad)
