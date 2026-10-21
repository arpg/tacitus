#!/usr/bin/env python3
"""The user's time zone, for every module that needs a local clock.

Where it comes from, first answer wins:
  1. `timezone` in ~/.ea/config/user-profile.json (the identity answers
     setup writes);
  2. `timezone` in ~/.ea/config/scheduling-preferences.json (where
     scripts/install/user_init.py writes it);
  3. the system's zone: $TZ, then the /etc/localtime link, then
     /etc/timezone;
  4. UTC.

A name counts only when zoneinfo knows it, so a typo in a config file
falls through to the next source instead of raising. EA_DIR is read at
call time, so tests and lanes that set it see their own config.

  python3 user_tz.py          print the zone this install resolves to
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

FALLBACK = "UTC"
CONFIG_FILES = ("user-profile.json", "scheduling-preferences.json")


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def _valid(tz) -> str:
    """`tz` when it is a zone name zoneinfo knows, else ''."""
    if not isinstance(tz, str) or not tz.strip():
        return ""
    tz = tz.strip()
    try:
        ZoneInfo(tz)
    except Exception:
        return ""
    return tz


def _configured(ea_dir: Path) -> str:
    for name in CONFIG_FILES:
        try:
            data = json.loads((ea_dir / "config" / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            tz = _valid(data.get("timezone"))
            if tz:
                return tz
    return ""


def system_name() -> str:
    """The system's zone name, '' when it cannot be told."""
    tz = _valid(os.environ.get("TZ", "").lstrip(":"))
    if tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        tz = _valid(target.split("zoneinfo/", 1)[1])
        if tz:
            return tz
    try:
        return _valid(Path("/etc/timezone").read_text().split()[0])
    except (OSError, IndexError):
        return ""


def name(ea_dir: Path | None = None) -> str:
    """The user's zone name: the config, else the system, else UTC."""
    return (_configured(Path(ea_dir) if ea_dir else _ea_dir())
            or system_name() or FALLBACK)


def zone(ea_dir: Path | None = None) -> ZoneInfo:
    """The user's zone as a ZoneInfo."""
    return ZoneInfo(name(ea_dir))


if __name__ == "__main__":
    print(name())
    sys.exit(0)
