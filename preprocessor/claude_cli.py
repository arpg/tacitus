#!/usr/bin/env python3
"""The one builder for every `claude -p` command line.

Every lane that starts the Claude CLI headless takes its extra flags from
here, so one change reaches all of them: the chat bot's `_sudo_claude_cmd`,
run-agent.sh, run-plugin.sh and the owner tools (mail_privacy, mail_tuning,
mail_tagger).

The flags keep a run on the user's own subscription login. Claude Code's
docs say `--bare` becomes the default for `-p` in a future release, and a
bare run reads no subscription login. When the installed CLI offers an
opt-out of bare mode (`--no-bare` in its `--help`), every run passes it.
`login_flags` in the owner's private `claude.json` adds any other flag a
future CLI needs, with no code change:

    <private_dir>/claude.json   {"login_flags": ["--some-flag"]}

The file sits in the owner-only private dir because these flags go on
command lines the owner builds; ea-config.json is ea-bot's to write. A
process running as the resident user reads `claude.login_flags` from
ea-config.json instead, and takes from it only the flags in
RESIDENT_ALLOWED_FLAGS; any other process takes nothing from it.

This module never adds `--bare`, an API key or an apiKeyHelper: the EA runs
on the user's own Claude login. A command that already carries `--bare`
(the remote arm, whose harness talks to the tool gateway) gets no login
flags.

Every command `apply` builds also runs in the permission mode
LANE_PERMISSION_MODE (`dontAsk`), bare or not. ea-bot's Claude settings
run auto mode, where a tool outside `--allowedTools` goes to the auto-mode
classifier and can be approved; under `dontAsk` the CLI refuses it, so the
allow list is the boundary. `apply` refuses a command that asks for
another mode or for bypass: the interactive resident pane and the ea-bot
shell, which run in bypass on purpose, do not build through it. The shell
lanes (run-agent.sh, run-plugin.sh) write the mode into their own command
line, so a failed `flags` call cannot drop it.

A run with `--strict-mcp-config` loads no claude.ai connector. A lane that
needs no connector passes it, and `no_connector_env(args)` gives the
environment switch that turns the connectors off as well
(ENABLE_CLAUDEAI_MCP_SERVERS). A lane that keeps the connectors reaches
only the connector tools its allow list names, since `dontAsk` refuses
the rest.

    from claude_cli import apply
    cmd = [claude_bin] + apply(["-p", prompt, "--model", model],
                               claude_bin=claude_bin, as_ea_bot=True)

`plugin_dir_flags()` gives the chat lanes `--plugin-dir` for each Claude
Code plugin an enabled plugin declares (docs/plugins.md, claude_plugin).

A non-bare run also loads CLAUDE.md and CLAUDE.local.md from its working
directory. `discovered_memory(args, cwd)` says which of the two a run
will load, so a prompt builder embeds only the rest.

    python3 claude_cli.py flags [--claude-bin PATH] [--as-ea-bot]
        prints the flags NUL-separated, nothing when there are none (for
        the shell lanes)

EA_CLAUDE_PROBE=off skips the `--help` probe (the test suite sets it).
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_trust as _owner_trust  # noqa: E402

OPT_OUT_FLAG = "--no-bare"
PRIVATE_FILE = "claude.json"
CACHE_FILE = "claude-cli-cache.json"
HELP_TIMEOUT_S = 20
# A probe result is trusted this long even when the binary's mtime cannot
# be read (another user's home), so a self-updated CLI is seen within a day.
CACHE_TTL_S = 24 * 3600
ISOLATION_USER = "ea-bot"
# The memory files Claude Code loads from a non-bare run's working
# directory: the product core and the install overlay beside it
# (preprocessor/install_overlay.py).
MEMORY_FILES = ("CLAUDE.md", "CLAUDE.local.md")
# The only flags ea-config.json can add, since the resident can write that
# file: the opt-out that keeps a run on the subscription login.
RESIDENT_ALLOWED_FLAGS = frozenset({OPT_OUT_FLAG})
# The permission mode of every headless lane: a tool the allow list does
# not name is refused, never sent to the auto-mode classifier.
LANE_PERMISSION_MODE = "dontAsk"
# The flags that choose a mode. A command that carries one is a code bug,
# and no configured flag may add one.
_MODE_FLAGS = ("--permission-mode", "--dangerously-skip-permissions",
               "--allow-dangerously-skip-permissions")
# The environment switch that keeps the shared account's claude.ai
# connectors out of a run.
NO_CONNECTORS_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

_memo: dict[str, bool] = {}


def _probe_enabled() -> bool:
    return os.environ.get("EA_CLAUDE_PROBE", "").strip().lower() not in ("off", "0", "no")


def _ea_bot_home() -> str:
    try:
        return pwd.getpwnam(ISOLATION_USER).pw_dir
    except KeyError:
        return ""


def _binary_key(claude_bin: str, as_ea_bot: bool) -> str:
    """The cache key: who runs it, the resolved binary and its mtime. An
    unreadable mtime (a binary in another user's home) reads as `?`, and
    such an entry then expires after CACHE_TTL_S."""
    path = claude_bin
    try:
        path = os.path.realpath(claude_bin)
        mtime = str(os.stat(path).st_mtime_ns)
    except OSError:
        mtime = "?"
    who = ISOLATION_USER if as_ea_bot else "self"
    return f"{who}|{path}|{mtime}"


def _help_text(claude_bin: str, as_ea_bot: bool) -> str:
    """`claude --help`, run as the user the lane runs as. "" on failure."""
    if as_ea_bot:
        home = _ea_bot_home()
        cmd = ["sudo", "-n", "-u", ISOLATION_USER, "env"]
        if home:
            cmd.append(f"HOME={home}")
        cmd += [claude_bin, "--help"]
    else:
        cmd = [claude_bin, "--help"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=HELP_TIMEOUT_S, cwd="/", stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "") + (r.stderr or "")


def _cache_path() -> Path | None:
    """The probe cache, in the owner's private dir; None in any other
    process, which then keeps the answer in memory only."""
    if not _owner_trust.is_owner_process():
        return None
    return _owner_trust.private_dir() / CACHE_FILE


def _read_cache() -> dict:
    path = _cache_path()
    if path is None:
        return {}
    try:
        data = json.loads(_owner_trust.read_owner_file(path))
    except (OSError, ValueError, _owner_trust.OwnerTrustError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict) -> None:
    path = _cache_path()
    if path is None:
        return
    try:
        _owner_trust.write_owner_file(path, json.dumps(data, indent=1, sort_keys=True))
    except (OSError, _owner_trust.OwnerTrustError):
        pass


def has_opt_out(claude_bin: str, as_ea_bot: bool = False) -> bool:
    """True when the installed CLI lists the bare-mode opt-out flag."""
    if not _probe_enabled():
        return False
    key = _binary_key(claude_bin, as_ea_bot)
    if key in _memo:
        return _memo[key]
    cache = _read_cache()
    entry = cache.get(key)
    now = time.time()
    if isinstance(entry, dict) and isinstance(entry.get("opt_out"), bool):
        fresh = not key.endswith("|?") or now - float(entry.get("at") or 0) < CACHE_TTL_S
        if fresh:
            _memo[key] = entry["opt_out"]
            return entry["opt_out"]
    text = _help_text(claude_bin, as_ea_bot)
    found = OPT_OUT_FLAG in text.split()
    _memo[key] = found
    if text:
        # Only an answer from a CLI that ran is worth keeping: a failed
        # probe (sudo refused, a timeout) is asked again next time.
        cache = {k: v for k, v in cache.items()
                 if isinstance(v, dict) and now - float(v.get("at") or 0) < 30 * 24 * 3600}
        cache[key] = {"opt_out": found, "at": now}
        _write_cache(cache)
    return found


def _clean_flags(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _is_resident_process() -> bool:
    """True when this process's euid is the resident user."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name == ISOLATION_USER
    except KeyError:
        return False


def configured_flags() -> list[str]:
    """`login_flags` from the owner's private claude.json (owner process),
    or the RESIDENT_ALLOWED_FLAGS among `claude.login_flags` in
    ea-config.json (a process running as the resident user). Any other
    process gets none."""
    if _owner_trust.is_owner_process():
        try:
            data = json.loads(_owner_trust.read_owner_file(
                _owner_trust.private_dir() / PRIVATE_FILE))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, _owner_trust.OwnerTrustError) as e:
            print(f"claude_cli: {PRIVATE_FILE} unreadable, no extra flags: {e}",
                  file=sys.stderr)
            return []
        return _clean_flags(data.get("login_flags") if isinstance(data, dict) else None)
    if not _is_resident_process():
        return []
    path = _owner_trust.ea_dir() / "config" / "ea-config.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    block = data.get("claude") if isinstance(data, dict) else None
    flags = _clean_flags(block.get("login_flags") if isinstance(block, dict) else None)
    dropped = [f for f in flags if f not in RESIDENT_ALLOWED_FLAGS]
    if dropped:
        print(f"claude_cli: ea-config.json login_flags not allowed, ignored: {dropped}",
              file=sys.stderr)
    return [f for f in flags if f in RESIDENT_ALLOWED_FLAGS]


def login_flags(claude_bin: str = "claude", as_ea_bot: bool = False) -> list[str]:
    """The flags that keep a `-p` run on the subscription login: the
    CLI's bare-mode opt-out when it has one, then the configured flags.
    Never --bare, never an API key."""
    flags: list[str] = []
    if has_opt_out(claude_bin, as_ea_bot):
        flags.append(OPT_OUT_FLAG)
    for f in configured_flags():
        if f == "--bare" or f in flags:
            continue
        if _is_mode_flag(f):
            print(f"claude_cli: a login flag may not set the permission mode, ignored: {f}",
                  file=sys.stderr)
            continue
        flags.append(f)
    return flags


def _is_mode_flag(arg: str) -> bool:
    return any(arg == f or arg.startswith(f + "=") for f in _MODE_FLAGS)


def permission_flags() -> list[str]:
    """The flags that put a headless run in LANE_PERMISSION_MODE."""
    return ["--permission-mode", LANE_PERMISSION_MODE]


def no_connector_env(args) -> dict[str, str]:
    """NO_CONNECTORS_ENV for a command that carries --strict-mcp-config
    (a run that loads no connector), else {}. The sudo env builders add
    it to the run's environment."""
    return dict(NO_CONNECTORS_ENV) if "--strict-mcp-config" in list(args or ()) else {}


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def resident_venv_missing(venv, requirements) -> list[str] | None:
    """The packages of `requirements` that ea-bot's venv does not hold;
    None when there is no venv or the requirements cannot be read.

    Read from the names of the venv's *.dist-info directories. The venv
    is in ea-bot's home, so the owner reads the listing and never runs
    that interpreter; a false listing changes only ea-bot's own PATH."""
    try:
        wanted = [m.group(0) for ln in Path(requirements).read_text().splitlines()
                  if (m := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", ln.strip()))]
        venv = Path(venv)
        sites = list(venv.glob("lib/python*/site-packages"))
        if not sites or not (venv / "bin" / "python3").exists():
            return None
        have = {_dist_name(d.name.split("-", 1)[0]) for s in sites for d in s.glob("*.dist-info")}
    except OSError:
        return None
    return [n for n in wanted if _dist_name(n) not in have]


def resident_path_dirs(venv, local_bin, requirements) -> list[str]:
    """The directories that lead the PATH of ea-bot's tool calls: its
    venv's bin and its ~/.local/bin, when that venv holds every package
    of `requirements`. Else none, and the service's PATH stands: a venv
    made before a requirement was added lacks a package that the python3
    further along the PATH can carry, and leading with it would take that
    package from the lane."""
    if resident_venv_missing(venv, requirements) != []:
        return []
    return [str(Path(venv) / "bin"), str(local_bin)]


def plugin_dir_flags() -> list[str]:
    """`--plugin-dir <root>` for each Claude Code plugin that an enabled
    agent-zone plugin declares in its `claude_plugin` capability and the
    owner accepted in the plugin lock (docs/plugins.md). The chat lanes add
    them. [] outside an owner process, which cannot read the lock, and when
    no plugin declares one."""
    try:
        import plugin_registry
        dirs = plugin_registry.claude_plugin_dirs()
    except Exception as e:
        print(f"claude_cli: plugin dirs unavailable: {e}", file=sys.stderr)
        return []
    out: list[str] = []
    for d in dirs:
        out += ["--plugin-dir", str(d)]
    return out


def apply(args: list[str], *, claude_bin: str = "claude", as_ea_bot: bool = False) -> list[str]:
    """`args` with the login flags in front and the lane permission mode
    behind. A command that already carries --bare owns its harness and
    gets no login flags, but the mode all the same. A command that sets a
    permission mode itself raises ValueError."""
    args = list(args)
    # Every element counts, the one after -p too: -p takes no value, so the
    # CLI would read a prompt spelled like a mode flag as that flag.
    chosen = [a for a in args if _is_mode_flag(a)]
    if chosen:
        raise ValueError("claude_cli.apply sets the permission mode; "
                         f"the command asked for {chosen}")
    if "--bare" in args:
        return args + permission_flags()
    return login_flags(claude_bin, as_ea_bot) + args + permission_flags()


def discovered_memory(args, cwd) -> frozenset[str]:
    """The memory files a `claude -p` run with these arguments, started in
    `cwd`, loads by itself: CLAUDE.md (the product core) and
    CLAUDE.local.md (the install overlay) when they are in that directory,
    none under --bare. A prompt builder embeds only what is missing from
    this set, so no run reads the core twice. `apply` never adds --bare,
    so the caller's own arguments decide."""
    if "--bare" in list(args or ()):
        return frozenset()
    try:
        base = Path(cwd)
        return frozenset(n for n in MEMORY_FILES if (base / n).is_file())
    except (OSError, TypeError, ValueError):
        return frozenset()


def memory_on_path(cwd) -> list[Path]:
    """Every memory file a non-bare run started in `cwd` could pick up on
    its way to the root: CLAUDE.md, CLAUDE.local.md and .claude/CLAUDE.md
    in `cwd` and in each directory above it. Claude Code reads the ones
    above the working directory too, so a lane that must load none needs
    this list empty, not only discovered_memory."""
    found: list[Path] = []
    try:
        here = Path(os.path.abspath(cwd))
    except (TypeError, ValueError):
        return found
    for d in (here, *here.parents):
        for rel in (*MEMORY_FILES, os.path.join(".claude", "CLAUDE.md")):
            p = d / rel
            try:
                if p.is_file():
                    found.append(p)
            except OSError:
                continue
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("flags", help="print the login flags, NUL-separated")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--as-ea-bot", action="store_true",
                   help="probe the CLI as the ea-bot user, as the lanes run it")
    args = ap.parse_args(argv)
    if args.cmd == "flags":
        out = login_flags(args.claude_bin, args.as_ea_bot)
        if out:
            sys.stdout.write("\0".join(out) + "\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
