#!/usr/bin/env python3
"""Prepare ea-bot's ~/.ea and ~/.claude in the container. Runs as ea-bot.

The entrypoint's init role runs this through `runuser -u ea-bot` on every
start. Everything here is ea-bot space, so it runs as ea-bot: a symlink
planted in that space then reaches only what ea-bot can reach already.

  - the directories scripts/install/setup-ea-user.sh makes, where an older volume lacks
    them (the image puts them in every new volume);
  - the config templates a new volume does not have yet;
  - config files another user put there (a `docker compose cp` lands as
    root): rewritten as ea-bot's own, mode 0640;
  - the install profile, in an ea-config.json copied on this start;
  - mail mediation on, unless it is switched off or the owner's rollback
    file is there;
  - ea-bot's Claude Code settings with the self-updater off, once.

  python3 prepare_ea_dir.py --code-dir D [--profile P] [--mediation 0|1]
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
from pathlib import Path

RESIDENT_USER = "ea-bot"
PROFILES = ("email", "review-only", "assistant")
SHARED_DIRS = ("state", "logs", "receipts", "uploads", "letters", "templates", "exports",
               "plugins")
CLAUDE_SETTINGS = {"env": {"DISABLE_AUTOUPDATER": "1"}}


def _refuse_owner() -> None:
    """This script writes ea-bot space by name, which only ea-bot does."""
    if pwd.getpwuid(os.geteuid()).pw_name != RESIDENT_USER:
        raise SystemExit(f"prepare_ea_dir.py runs as {RESIDENT_USER}, not as "
                         f"{pwd.getpwuid(os.geteuid()).pw_name}")


def log(msg: str) -> None:
    print(f"[prepare_ea_dir] {msg}", file=sys.stderr)


def ensure_dir(path: Path, mode: int) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        path.mkdir()
        os.chmod(path, mode)
        return
    if not stat.S_ISDIR(st.st_mode):
        log(f"{path} is not a directory; left alone")
    elif st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) != mode:
        os.chmod(path, mode)


def create_file(path: Path, data: bytes, mode: int) -> bool:
    """Write `data` to a new file at `path`; False when the name exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)
    return True


def adopt_file(path: Path, mode: int) -> None:
    """Make a regular file another user left in an ea-bot directory into an
    ea-bot file with the same content."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        data = f.read()
    tmp = path.with_name(f".{path.name}.adopt")
    tmp.unlink(missing_ok=True)
    create_file(tmp, data, mode)
    os.replace(tmp, path)


def update_config(path: Path, change) -> None:
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    change(cfg)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    create_file(tmp, (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode(), 0o640)
    os.replace(tmp, path)


def prepare(ea_dir: Path, claude_dir: Path, code_dir: Path, profile: str | None,
            mediation: bool) -> None:
    for name in SHARED_DIRS:
        ensure_dir(ea_dir / name, 0o2775)
    ensure_dir(ea_dir / "state" / "mail-queue", 0o2770)
    ensure_dir(ea_dir / "config", 0o750)
    if not (ea_dir / "auth").is_dir():
        log(f"{ea_dir / 'auth'} is missing; the image creates it in a new volume "
            "(docs/docker.md, Volumes)")

    config = ea_dir / "config"
    fresh = not (config / "ea-config.json").exists()
    copied = 0
    for src in sorted((code_dir / "config").iterdir()):
        if src.is_file() and create_file(config / src.name, src.read_bytes(), 0o640):
            copied += 1
    if copied:
        log(f"copied {copied} config template(s)")
    for entry in [*sorted(config.iterdir()), ea_dir / ".env"]:
        try:
            st = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid != os.geteuid():
            adopt_file(entry, 0o640)
            log(f"took over {entry.name} (it belonged to uid {st.st_uid})")

    try:
        profile_now = json.loads((config / "ea-config.json").read_text()).get("profile")
    except (OSError, ValueError, AttributeError):
        profile_now = None

    if fresh and profile:
        update_config(config / "ea-config.json", lambda c: c.__setitem__("profile", profile))
        log(f"profile {profile} written to the new ea-config.json")
    else:
        log(f"profile: {profile_now or '?'} (ea-config.json)")

    if mediation and not (ea_dir / "auth" / "mediation-disabled").exists():
        def turn_on(cfg: dict) -> None:
            mm = cfg.get("mail_mediation")
            mm = mm if isinstance(mm, dict) else {}
            mm["enabled"] = True
            cfg["mail_mediation"] = mm
        try:
            current = json.loads((config / "ea-config.json").read_text()).get("mail_mediation")
        except (OSError, ValueError, AttributeError):
            current = None
        if not (isinstance(current, dict) and current.get("enabled")):
            update_config(config / "ea-config.json", turn_on)
            log("mail mediation turned on")

    if create_file(claude_dir / "settings.json",
                   (json.dumps(CLAUDE_SETTINGS, indent=2) + "\n").encode(), 0o600):
        log("Claude Code settings written (self-updater off)")


def main(argv: list[str] | None = None) -> int:
    _refuse_owner()
    home = Path(pwd.getpwnam(RESIDENT_USER).pw_dir)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code-dir", type=Path, required=True)
    ap.add_argument("--profile", default="")
    ap.add_argument("--mediation", choices=("0", "1"), default="1")
    a = ap.parse_args(argv)
    profile = a.profile or None
    if profile and profile not in PROFILES:
        log(f"profile {profile!r} is not one of {', '.join(PROFILES)}; keeping the template's")
        profile = None
    os.umask(0o002)
    prepare(home / ".ea", home / ".claude", a.code_dir, profile, a.mediation == "1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
