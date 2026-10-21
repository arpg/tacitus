#!/usr/bin/env python3
"""Write the container's crontab for supercronic.

The scheduler service runs this at start, as the owner, and hands the
output to supercronic. It holds the same jobs scripts/install/setup.sh and docs/SETUP.md "Cron schedule"
install on a host, for the install profile in ea-config.json:

  - every profile: the briefings, the weekly audit, intake, ledger sync,
    the closure scrubber, the calendar reconciler, the style sync, the
    persistence digest;
  - assistant: the chat checks, deferred sends, the quiet-hours flush and
    the monthly style analysis, which all need the chat platform;
  - the admissions filter and digest, when ea-config.json enables them;
  - one line per schedule of each plugin in ~/.ea/plugins: a v1 plugin
    from its own plugin.json when that says enabled; a v2 plugin from the
    owner's lock (plugins.lock.json in the private directory), the way
    scripts/install/setup.sh and plugin_registry.get_plugin read it, so a v2 manifest
    without an accepted, enabled lock entry schedules nothing;
  - the owner plugins' cron jobs, the lines `ea-plugin.py cron-lines`
    prints;
  - the owner's own extra lines (~/.ea-private/crontab.extra), verbatim.

Left out on purpose: the nightly evals (they read git history, which the
image does not hold) and the browser token harvests (host installs only).

  python3 crontab.py [--profile P] [--code-dir D] [--ea-dir E] [--private-dir P]
                     [--log-dir L] [--extra F]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

# The checkout this file is in: the lock readers come from its preprocessor
# directory, the same tree the scheduler runs this script from.
TREE = Path(__file__).resolve().parent.parent.parent

PROFILES = ("email", "review-only", "assistant")

# (schedule, command relative to the code dir, log file name)
COMMON = (
    ("0 * * * 1-5", "run-agent.sh morning-briefing", "morning.log"),
    ("0 15 * * 1-5", "run-agent.sh afternoon-catchup", "afternoon.log"),
    ("0 20 * * 0", "run-agent.sh weekly-audit --model opus", "weekly.log"),
    ("*/10 7-19 * * 1-5", "run-agent.sh intake-check", "intake-check.log"),
    ("*/15 * * * *", "python3 {code}/preprocessor/ledger_sync.py check", "ledger-sync.log"),
    ("20 7-14,16-21 * * *", "python3 {code}/preprocessor/closure_scrubber.py check --json",
     "closure-scrubber-cron.log"),
    ("7,37 * * * *", "python3 {code}/preprocessor/calendar_reconciler.py --apply",
     "calendar-reconciler.log"),
    ("20 4 * * 1", "python3 {code}/preprocessor/style_profiles.py sync --days 21",
     "style-profiles.log"),
    ("40 6 * * *", "python3 {code}/scripts/persistence-digest.py", "persistence-digest.log"),
    ("30 6 * * *", "python3 {code}/scripts/preferences-digest.py", "preferences-digest.log"),
    ("45 6 * * *", "python3 {code}/scripts/outbound-review.py", "outbound-review.log"),
)
ASSISTANT = (
    ("0 8-20 * * 1-5", "run-agent.sh scheduling-check", "scheduling-check.log"),
    ("*/30 8-18 * * 1-5", "run-agent.sh priority-check", "priority-check.log"),
    ("*/5 * * * *", "python3 {code}/preprocessor/scheduled_sends.py check", "scheduled-sends.log"),
    ("*/15 * * * *", "python3 {code}/preprocessor/quiet_hours.py flush", "quiet-hours.log"),
    ("40 4 1 * *", "python3 {code}/preprocessor/style_analysis.py run --dispatch",
     "style-analysis.log"),
)
ADMISSIONS = (
    ("*/5 * * * *", "python3 {code}/preprocessor/admissions_filter.py sweep",
     "admissions-filter.log"),
    ("0 8 * * 1", "run-agent.sh admissions-digest", "admissions-digest.log"),
)

# Plugin manifests are ea-bot space and their fields become cron lines that
# run as the owner, so a field is used only when it has its shape. These are
# the checks scripts/install/setup.sh applies to the same manifests.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SCHEDULE = re.compile(r"@(?:reboot|yearly|annually|monthly|weekly|daily|hourly)"
                      r"|[0-9A-Za-z*/,-]+(?:[ \t]+[0-9A-Za-z*/,-]+){4}")
ARGS = re.compile(r"(?:[A-Za-z0-9_=.,:+/-]+(?:[ \t]+[A-Za-z0-9_=.,:+/-]+)*)?")


def shaped(pattern: re.Pattern, value) -> str | None:
    """`value` with single spaces, or None when it is not a string that
    `pattern` matches in full."""
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        return None
    return " ".join(value.split())


def comment(value) -> str:
    """`value` as one line of printable text, for a comment line."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))[:200]


def read_config(ea_dir: Path) -> dict:
    try:
        data = json.loads((ea_dir / "config" / "ea-config.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def job(schedule: str, command: str, log: str, code: Path, log_dir: Path) -> str:
    cmd = command.format(code=code)
    if cmd.startswith("run-agent.sh"):
        cmd = f"{code}/{cmd}"
    return f"{schedule}  {cmd} >> {log_dir}/{log} 2>&1"


def _registry():
    """plugin_registry and plugin_install from this checkout, or None when
    the tree lacks them (a trimmed image); the caller says so in a comment."""
    if str(TREE / "preprocessor") not in sys.path:
        sys.path.insert(0, str(TREE / "preprocessor"))
    try:
        import plugin_install
        import plugin_registry
    except Exception:
        return None
    return plugin_registry, plugin_install


def _plugin_log(log_dir: Path, name: str) -> str:
    plugin_log = log_dir / "plugins" / name
    plugin_log.mkdir(parents=True, exist_ok=True)
    return f"{plugin_log}/plugin.log"


def v2_lines(name: str, lock: dict, private: Path, code: Path, log_dir: Path,
             registry) -> list[str]:
    """The lines of one v2 plugin, from the owner's lock. The live manifest
    decides nothing: a plugin without an enabled, accepted entry in the
    lock gets no line."""
    try:
        lp = registry.get_plugin(name, lock=lock, private=private)
    except Exception as e:
        return [f"# WARN: plugin '{name}' (manifest v2): the owner's lock is unreadable: "
                f"{comment(e)}"]
    if lp is None:
        return [f"# plugin '{name}' (manifest v2): no enabled, accepted entry in the "
                "owner's lock; skipping"]
    lines = []
    log = _plugin_log(log_dir, name)
    for sched in lp.manifest["capabilities"]["schedules"]:
        label = comment(sched.get("label", "default"))
        cron = shaped(SCHEDULE, sched.get("cron"))
        args = shaped(ARGS, " ".join(str(a) for a in sched.get("args") or []))
        if not cron or args is None:
            lines.append(f"# WARN: plugin '{name}' ({label}) has a schedule or arguments "
                         "that are not plain; skipping")
            continue
        tail = f" -- {args}" if args else ""
        lines.append(f"# plugin {name} ({label})")
        lines.append(f"{cron}  {code}/run-plugin.sh {name}{tail} >> {log} 2>&1")
    return lines


def plugin_lines(ea_dir: Path, code: Path, log_dir: Path, private: Path) -> list[str]:
    plugins = ea_dir / "plugins"
    if not plugins.is_dir():
        return []
    lines = ["", "# Plugins (~/.ea/plugins/*/plugin.json; v2 from the owner's lock)"]
    registry = lock = None
    for manifest in sorted(plugins.glob("*/plugin.json")):
        try:
            m = json.loads(manifest.read_text())
            if not isinstance(m, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            lines.append(f"# WARN: bad manifest {comment(manifest)}: {comment(e)}")
            continue
        name = shaped(NAME, m.get("name") or manifest.parent.name)
        if not name:
            lines.append(f"# WARN: plugin in {comment(manifest.parent.name)} has a name "
                         "that is not a plain word; skipping")
            continue
        if m.get("manifest_version") == 2:
            if registry is None:
                mods = _registry()
                if mods is None:
                    lines.append(f"# WARN: plugin '{name}' (manifest v2): plugin_registry is "
                                 "not in this tree; skipping")
                    continue
                registry = mods[0]
                lock = registry.read_lock(registry.lock_path(private))
            lines += v2_lines(name, lock, private, code, log_dir, registry)
            continue
        if not m.get("enabled"):
            continue
        schedule = shaped(SCHEDULE, m.get("schedule"))
        if not schedule:
            lines.append(f"# WARN: plugin '{name}' has no schedule of cron fields; skipping")
            continue
        log = _plugin_log(log_dir, name)
        lines.append(f"{schedule}  {code}/run-plugin.sh {name} >> {log} 2>&1")
        extras = m.get("extra_schedules")
        for ex in extras if isinstance(extras, list) else []:
            if not isinstance(ex, dict) or not ex.get("cron"):
                continue
            cron = shaped(SCHEDULE, ex.get("cron"))
            args = shaped(ARGS, ex.get("args") or "")
            if not cron or args is None:
                lines.append(f"# WARN: plugin '{name}' ({comment(ex.get('label', 'extra'))}) "
                             "has a schedule or arguments that are not plain; skipping")
                continue
            tail = f" -- {args}" if args else ""
            lines.append(f"{cron}  {code}/run-plugin.sh {name}{tail} >> {log} 2>&1")
    return lines


def owner_lines(ea_dir: Path, code: Path, log_dir: Path, private: Path) -> list[str]:
    """The owner plugins' cron jobs, the lines `scripts/ea-plugin.py
    cron-lines` prints, from the same function."""
    mods = _registry()
    if mods is None:
        return []
    _registry_mod, install = mods
    try:
        ctx = install.Context(private=private, agent_dir=ea_dir / "plugins", ea_dir=ea_dir,
                              local_copy=True, out=lambda _line: None)
        lines = install.cron_lines(ctx, code / "scripts" / "ea-plugin.py", log_dir)
    except Exception as e:
        return ["", f"# WARN: owner plugin cron jobs skipped: {comment(e)}"]
    if not lines:
        return []
    return ["", "# Owner plugins (ea-plugin.py cron-lines)", *lines]


def extra_lines(path: Path) -> list[str]:
    """The owner's own lines, used only from a file the owner alone can
    change (owned by this user, not writable by group or others)."""
    try:
        st = path.lstat()
    except OSError:
        return []
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        return ["", f"# WARN: {comment(path)} skipped: it must be a regular file "
                    "owned by this user and writable by no one else"]
    return ["", f"# Owner extras ({comment(path)})", path.read_text().rstrip("\n")]


def render(profile: str, code: Path, ea_dir: Path, log_dir: Path, extra: Path,
           private: Path) -> str:
    cfg = read_config(ea_dir)
    lines = [f"# EA cron jobs for the {profile} profile. Written by deploy/docker/crontab.py",
             "# at scheduler start; edit ~/.ea-private/crontab.extra to add lines."]
    lines += [job(*j, code, log_dir) for j in COMMON]
    if profile == "assistant":
        lines += [job(*j, code, log_dir) for j in ASSISTANT]
    admissions = cfg.get("admissions_filter")
    if isinstance(admissions, dict) and admissions.get("enabled"):
        lines += [job(*j, code, log_dir) for j in ADMISSIONS]
    lines += plugin_lines(ea_dir, code, log_dir, private)
    lines += owner_lines(ea_dir, code, log_dir, private)
    lines += extra_lines(extra)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    private_default = Path(os.environ.get("EA_PRIVATE_DIR") or home / ".ea-private")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", choices=PROFILES)
    ap.add_argument("--code-dir", type=Path, default=Path("/opt/ea-system"))
    ap.add_argument("--ea-dir", type=Path, default=Path(os.environ.get("EA_DIR") or home / ".ea"))
    ap.add_argument("--private-dir", type=Path, default=private_default,
                    help="the owner-private directory: the plugin lock, the logs, the extras")
    ap.add_argument("--log-dir", type=Path, default=None)
    ap.add_argument("--extra", type=Path, default=None)
    a = ap.parse_args(argv)
    profile = a.profile or read_config(a.ea_dir).get("profile") or "assistant"
    if profile not in PROFILES:
        print(f"crontab.py: unknown profile {profile!r} in ea-config.json", file=sys.stderr)
        return 2
    log_dir = a.log_dir or a.private_dir / "logs"
    extra = a.extra or a.private_dir / "crontab.extra"
    sys.stdout.write(render(profile, a.code_dir, a.ea_dir, log_dir, extra, a.private_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
