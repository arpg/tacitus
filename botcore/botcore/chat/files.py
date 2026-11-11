"""Where a frontend saves a file it downloads (a Slack or web chat upload).

The default writes plainly: the directory is created as needed and the
file is replaced atomically (temp file in the same directory, then
rename), so a reader never sees a half-written file and a name that is a
symlink is replaced, not written through.

An application that keeps its files somewhere it must reach in a special
way (the EA writes into another OS user's space through its own helper)
installs its writers once at startup with `use(make_dir, replace_bytes)`.
Both return True on success, False on failure; neither raises.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def _plain_make_dir(path, mode: int = 0o2775) -> bool:
    try:
        p = Path(path)
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, mode)
        return True
    except OSError:
        return False


def _plain_replace_bytes(path, data: bytes, mode: int = 0o664) -> bool:
    p = Path(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


make_dir: Callable[..., bool] = _plain_make_dir
replace_bytes: Callable[..., bool] = _plain_replace_bytes


def use(make_dir_fn: Callable[..., bool], replace_bytes_fn: Callable[..., bool]) -> None:
    """Install the application's writers for every frontend."""
    global make_dir, replace_bytes
    make_dir, replace_bytes = make_dir_fn, replace_bytes_fn
