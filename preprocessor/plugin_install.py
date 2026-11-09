"""Install, verify, update and remove plugins (scripts/ea-plugin.py is the CLI;
docs/plugins.md is the guide).

Every git step runs as the owner in the owner-private staging directory,
never in the resident's space: a repository the resident can write would
let its git config or hooks run as the owner. An agent-zone plugin is then
copied, without its .git, into the resident's plugins directory as the
resident. An owner-zone plugin moves, with its .git, into the owner-private
plugins directory.

The lock records, per plugin: the source URL, the ref asked for and the
commit it resolved to, the SHA-256 of every file, the normalized manifest,
the digest of the capabilities the owner accepted, and whether it is
enabled. Nothing here pushes anywhere.
"""
from __future__ import annotations

import fnmatch
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import owner_trust as _owner_trust
    import plugin_manifest as PM
    import plugin_registry as R
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.plugin_install
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import plugin_manifest as PM
    from preprocessor import plugin_registry as R
    from preprocessor import resident_io as _rio

RESIDENT_USER = "ea-bot"
STAGING_DIRNAME = "plugins-staging"

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_CONSENT, EXIT_BLOCKED = 0, 1, 2, 3, 4


class PluginError(RuntimeError):
    """An install step that cannot go on; `code` is the CLI exit status."""

    def __init__(self, msg: str, code: int = EXIT_USAGE):
        super().__init__(msg)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Context:
    """Where things live. The defaults are the live install; tests pass
    their own directories and set local_copy."""

    def __init__(self, private=None, agent_dir=None, ea_dir=None, local_copy=None, out=None):
        self.private = Path(private) if private else _owner_trust.private_dir()
        self.agent_dir = Path(agent_dir) if agent_dir else R.agent_plugins_dir()
        self.ea_dir = Path(ea_dir) if ea_dir else R.EA_DIR
        if local_copy is None:
            local_copy = (os.environ.get("EA_PLUGIN_LOCAL_COPY") == "1"
                          or not _resident_exists())
        self.local_copy = bool(local_copy)
        self.out = out or (lambda line: print(line))

    @property
    def lock_path(self) -> Path:
        return R.lock_path(self.private)

    def plugin_dir(self, name: str, zone: str) -> Path:
        if zone == "owner":
            return R.owner_plugins_dir(self.private) / name
        return self.agent_dir / name

    def read_lock(self) -> dict:
        R._LOCK_CACHE["key"] = None
        if not self.lock_path.exists():
            return R._empty_lock()
        return R.read_lock(self.lock_path)

    def write_lock(self, lock: dict) -> None:
        R.write_lock(lock, self.lock_path)
        self._write_alias_hint(lock)

    def _write_alias_hint(self, lock: dict) -> None:
        hint = json.dumps(R.alias_hint(lock=lock), indent=1, sort_keys=True) + "\n"
        path = self.ea_dir / "state" / R.ALIAS_HINT_FILE
        if not (_rio.make_dir(path.parent) and _rio.replace_text(path, hint, 0o664)):
            self.out("warning: the verb alias hint was not written; "
                     "aliases run only once it is")


def _resident_exists() -> bool:
    try:
        pwd.getpwnam(RESIDENT_USER)
        return True
    except KeyError:
        return False


# ── sources and policy ────────────────────────────────────────────────

def parse_source(spec: str) -> tuple[str, str]:
    """`git:<url>[@<ref>]` or `git:<url>#<ref>` -> (url, ref). A ref after
    `@` is only split off when it comes after the last `/`, so an scp-style
    `git@host:path` URL keeps its user."""
    if not spec.startswith("git:"):
        raise PluginError(f"unsupported source {spec!r}: use git:<url>[@<ref>]")
    rest = spec[len("git:"):]
    ref = ""
    if "#" in rest:
        rest, ref = rest.rsplit("#", 1)
    else:
        tail = rest.rsplit("/", 1)[-1]
        if "@" in tail:
            rest, ref = rest.rsplit("@", 1)
    if not rest:
        raise PluginError(f"no URL in {spec!r}")
    if ref.startswith("-") or rest.startswith("-"):
        raise PluginError("a URL or ref may not start with '-'")
    return rest, ref


def _matches(url: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(url, p) for p in patterns)


def consent(url: str, summary: dict, accept: bool, policy: dict | None = None) -> str:
    """How the install is allowed: "trusted-source" or "accepted". Raises
    PluginError (exit 4) for a blocked source and (exit 3) when consent is
    needed and not given."""
    policy = policy if policy is not None else R.install_policy()
    if _matches(url, policy.get("blocked_sources") or []):
        raise PluginError(f"{url} matches blocked_sources in the install policy", EXIT_BLOCKED)
    trusted = _matches(url, policy.get("trusted_sources") or [])
    if trusted and not PM.needs_explicit_consent(summary):
        return "trusted-source"
    if accept:
        return "accepted"
    why = ("it runs code as the owner, which a trusted source does not cover" if trusted
           else "the source is not on trusted_sources")
    raise PluginError(f"consent needed ({why}): review the capabilities above, then run "
                      f"again with --accept-capabilities", EXIT_CONSENT)


# ── git, in the owner's staging directory ─────────────────────────────

_GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "protocol.ext.allow=never"]


def _git(args: list, cwd=None) -> str:
    env = {**os.environ, **_GIT_ENV}
    p = subprocess.run(["git"] + _GIT_SAFE + args, cwd=cwd, capture_output=True,
                       text=True, env=env, timeout=600)
    if p.returncode != 0:
        raise PluginError(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip()


def _staging(ctx: Context) -> Path:
    root = ctx.private / STAGING_DIRNAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _owner_trust.check_owner_dir(root)
    return Path(tempfile.mkdtemp(prefix="stage-", dir=root))


def fetch(ctx: Context, url: str, ref: str) -> tuple[Path, Path, str]:
    """(staging dir, checkout, commit). The caller removes the staging dir."""
    stage = _staging(ctx)
    src = stage / "src"
    _git(["clone", "--quiet", "--no-tags", "--", url, str(src)])
    if ref:
        # The clone carries no tags, so the ref is fetched from the source by
        # name, which resolves a tag, a branch or a commit alike, and the
        # commit it names is checked out. A checkout of the bare name found
        # no such tag in the clone and read it as a path (2026-11-09).
        _git(["fetch", "--quiet", "--no-tags", "origin", "--", ref], cwd=src)
        _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=src)
    commit = _git(["rev-parse", "HEAD"], cwd=src)
    return stage, src, commit


# ── placing files ─────────────────────────────────────────────────────

def _place_agent(ctx: Context, src: Path, name: str, state_dirs) -> None:
    """Write the release in `src` (owner staging, without .git) to
    <agent dir>/<name> through resident_io, so every write lands as the
    resident's and never follows a link the resident placed. State dirs
    are made writable for the resident."""
    dest = ctx.agent_dir / name
    if not _rio.make_dir(dest, 0o2775):
        raise PluginError(f"could not make {dest}")
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel = Path(dirpath).relative_to(src)
        for d in dirnames:
            if not _rio.make_dir(dest / rel / d, 0o2755):
                raise PluginError(f"could not make {dest / rel / d}")
        for f in filenames:
            path = Path(dirpath) / f
            if path.is_symlink():
                continue
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            if not _rio.replace_bytes(dest / rel / f, path.read_bytes(), mode):
                raise PluginError(f"could not write {dest / rel / f}")
    for s in state_dirs:
        if not _rio.make_dir(dest / s, 0o2775):
            raise PluginError(f"could not make {dest / s}")


def _remove_agent(ctx: Context, name: str) -> None:
    """Remove <agent dir>/<name>: as the resident through sudo on a host
    with one, in place on a host without (tests, a dev box)."""
    dest = ctx.agent_dir / name
    if not os.path.lexists(dest):
        return
    if ctx.local_copy:
        shutil.rmtree(dest)
        return
    p = subprocess.run(["sudo", "-n", "-u", RESIDENT_USER, "rm", "-rf", "--", str(dest)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise PluginError(f"removing {dest} failed: {p.stderr[-300:]}")


def _swap_agent(ctx: Context, src: Path, name: str, state_dirs) -> None:
    """Replace an installed agent plugin with the release in `src`: write
    it beside the old one, move the state dirs across, then swap the two
    names (resident_io moves within the plugins dir) and remove the old."""
    tag = os.urandom(4).hex()
    new, old = f".{name}.new-{tag}", f".{name}.old-{tag}"
    _place_agent(ctx, src, new, [])
    for s in state_dirs:
        if os.path.isdir(ctx.agent_dir / name / s):
            if not _rio.move(ctx.agent_dir / name / s, ctx.agent_dir / new / s):
                raise PluginError(f"could not keep the state dir {s}")
        elif not _rio.make_dir(ctx.agent_dir / new / s, 0o2775):
            raise PluginError(f"could not make {s}")
    if not _rio.move(ctx.agent_dir / name, ctx.agent_dir / old):
        raise PluginError(f"could not move the old {name} aside")
    if not _rio.move(ctx.agent_dir / new, ctx.agent_dir / name):
        _rio.move(ctx.agent_dir / old, ctx.agent_dir / name)
        raise PluginError(f"could not put the new {name} in place")
    _remove_agent(ctx, old)


def _owner_dest(ctx: Context, name: str) -> Path:
    return R.owner_plugins_dir(ctx.private) / name


def _place_owner(ctx: Context, src: Path, name: str, state_dirs) -> None:
    """Move the checkout in `src` (with .git) into the owner-private
    plugins dir, owner only."""
    dest = _owner_dest(ctx, name)
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _owner_trust.check_owner_dir(dest.parent)
    os.rename(src, dest)
    for dirpath, dirnames, filenames in os.walk(dest):
        os.chmod(dirpath, 0o700)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode & 0o700 or 0o600)
    for s in state_dirs:
        (dest / s).mkdir(mode=0o700, parents=True, exist_ok=True)


def _remove_owner(ctx: Context, name: str) -> None:
    dest = _owner_dest(ctx, name)
    if dest.exists():
        _owner_trust.check_owner_dir(dest)
        shutil.rmtree(dest)


# ── commands ──────────────────────────────────────────────────────────

def _load_checked(src: Path) -> tuple[dict, dict]:
    try:
        norm, errors = PM.load(src)
    except PM.ManifestError as e:
        raise PluginError(str(e))
    if norm["manifest_version"] != 2:
        raise PluginError("ea-plugin.py installs v2 plugins only; adopt a v1 plugin in place "
                          "with `ea-plugin.py adopt <name>`")
    if errors:
        raise PluginError("the manifest does not pass the checks:\n  " + "\n  ".join(errors))
    return norm, PM.capability_summary(norm)


def _show(ctx: Context, name: str, url: str, commit: str, summary: dict) -> None:
    ctx.out(f"plugin {name} from {url} at {commit[:12]} declares:")
    for line in PM.describe(summary):
        ctx.out(f"  - {line}")


def install(ctx: Context, spec: str, accept: bool = False, enable: bool = True,
            policy: dict | None = None) -> dict:
    url, ref = parse_source(spec)
    stage, src, commit = fetch(ctx, url, ref)
    try:
        norm, summary = _load_checked(src)
        name = norm["name"]
        lock = ctx.read_lock()
        if name in lock["plugins"]:
            raise PluginError(f"{name} is already installed; use `ea-plugin.py update {name}`")
        dest = ctx.plugin_dir(name, norm["zone"])
        if dest.exists() or os.path.islink(dest):
            raise PluginError(f"{dest} exists; remove it or adopt it")
        _show(ctx, name, url, commit, summary)
        how = consent(url, summary, accept, policy)
        files, problems = R.hash_tree(src, norm["state_dirs"])
        if problems:
            raise PluginError("the release has problems: " + "; ".join(problems))
        if norm["zone"] == "owner":
            _place_owner(ctx, src, name, norm["state_dirs"])
        else:
            _place_agent(ctx, src, name, norm["state_dirs"])
        digest = PM.capability_digest(summary)
        entry = {"name": name, "zone": norm["zone"], "enabled": bool(enable),
                 "source": {"url": url, "ref": ref, "commit": commit},
                 "installed_at": _now(), "updated_at": _now(), "manifest": norm,
                 "capability_digest": digest,
                 "accepted": {"digest": digest, "at": _now(), "how": how},
                 "files": files}
        lock["plugins"][name] = entry
        ctx.write_lock(lock)
        _post_install_notes(ctx, entry)
        return entry
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _post_install_notes(ctx: Context, entry: dict) -> None:
    caps = entry["manifest"]["capabilities"]
    ctx.out(f"installed {entry['name']} ({entry['zone']} zone) at "
            f"{ctx.plugin_dir(entry['name'], entry['zone'])}; "
            f"{'enabled' if entry['enabled'] else 'disabled'}")
    if caps["schedules"]:
        ctx.out("  schedules run from cron: run-plugin.sh --check-schedules lists the lines to add")
    if caps["owner_hooks"]["cron"]:
        ctx.out("  owner cron jobs: `ea-plugin.py cron-lines` prints the lines for the owner's crontab")
    if caps["cards"] or caps["chat"] or caps["owner_hooks"]["verbs"] or caps["denied_tools"]:
        ctx.out("  restart the chat bot and the mail mediator to load its cards, chat trigger, "
                "verbs and denials")
    if caps["network"]["local"]:
        ctx.out("  re-render the resident's network policy (deploy/enable-resident-netpolicy.sh)")


def read_git_meta(dest: Path) -> tuple[str, str]:
    """(origin URL, HEAD commit) of a checkout, read from its .git files as
    text. No git command runs there: in the resident's space its config
    and hooks are the resident's."""
    git = dest / ".git"
    if not git.is_dir() or os.path.islink(git):
        return "", ""
    url = commit = ""
    try:
        section = ""
        for line in (git / "config").read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                section = s
            elif section == '[remote "origin"]' and s.startswith("url"):
                url = s.split("=", 1)[1].strip()
    except OSError:
        pass
    try:
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if ref_file.is_file() and not os.path.islink(ref_file):
                commit = ref_file.read_text().strip()
            else:
                for line in (git / "packed-refs").read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        commit = parts[0]
        else:
            commit = head
    except OSError:
        pass
    if not (len(commit) in (40, 64) and all(c in "0123456789abcdef" for c in commit)):
        commit = ""
    return url, commit


def adopt(ctx: Context, name: str, zone: str = "agent", accept: bool = False,
          policy: dict | None = None) -> dict:
    """Record a plugin that is already in place (a v1 plugin cloned by
    hand, or a v2 plugin placed by setup) without fetching it again. A v1
    plugin's lock entry is for verify and list only; its own plugin.json
    keeps deciding what it does."""
    if not PM.NAME_RE.match(name):
        raise PluginError(f"bad plugin name {name!r}")
    dest = ctx.plugin_dir(name, zone)
    if not dest.is_dir() or os.path.islink(dest):
        raise PluginError(f"{dest} is not a plugin directory")
    try:
        norm, errors = PM.load(dest)
    except PM.ManifestError as e:
        raise PluginError(str(e))
    if norm["manifest_version"] == 2 and errors:
        raise PluginError("the manifest does not pass the checks:\n  " + "\n  ".join(errors))
    if norm["manifest_version"] == 2 and norm["zone"] != zone:
        raise PluginError(f"{name} declares zone {norm['zone']}, not {zone}")
    url, commit = read_git_meta(dest)
    summary = PM.capability_summary(norm)
    if norm["manifest_version"] == 2:
        _show(ctx, name, url or str(dest), commit or "(in place)", summary)
        if zone == "agent" and not accept:
            # The origin URL comes from a .git/config in the resident's
            # space, so it is no evidence of a trusted source.
            raise PluginError("consent needed (an adopted agent-zone plugin's origin is "
                              "the resident's own .git): review the capabilities above, "
                              "then run again with --accept-capabilities", EXIT_CONSENT)
        how = consent(url or str(dest), summary, accept, policy)
    else:
        how = "v1 manifest"
    files, problems = R.hash_tree(dest, norm["state_dirs"])
    if problems:
        raise PluginError("the plugin has problems: " + "; ".join(problems))
    digest = PM.capability_digest(summary)
    lock = ctx.read_lock()
    entry = {"name": name, "zone": zone,
             "enabled": bool(norm["enabled"]) if norm["manifest_version"] == 1 else True,
             "source": {"url": url, "ref": "", "commit": commit, "adopted": True},
             "installed_at": _now(), "updated_at": _now(), "manifest": norm,
             "capability_digest": digest, "accepted": {"digest": digest, "at": _now(), "how": how},
             "files": files}
    lock["plugins"][name] = entry
    ctx.write_lock(lock)
    ctx.out(f"adopted {name} ({zone} zone, manifest v{norm['manifest_version']}), "
            f"{len(files)} files recorded")
    return entry


def _locked(ctx: Context, name: str):
    lock = ctx.read_lock()
    entry = lock["plugins"].get(name)
    if not isinstance(entry, dict):
        raise PluginError(f"{name} is not installed")
    return lock, entry


def verify(ctx: Context, names=()) -> dict:
    """{name: [problems]} for the named plugins, or all of them. A secrets
    file other users can read is a problem too: the plugin's credentials
    are for the resident, not for every account on the host."""
    lock = ctx.read_lock()
    out = {}
    for name in (names or sorted(lock["plugins"])):
        entry = lock["plugins"].get(name)
        if not isinstance(entry, dict):
            out[name] = ["not installed"]
            continue
        zone = entry.get("zone", "agent")
        lp = R.LockedPlugin(name=name, zone=zone, dir=ctx.plugin_dir(name, zone),
                            manifest=entry["manifest"], entry=entry)
        problems = R.verify(lp, use_cache=False)
        secrets = lp.dir / ".env.secrets"
        try:
            if secrets.exists() and os.stat(secrets).st_mode & 0o007:
                problems.append(f"{secrets} is readable by other users: chmod o-rwx it")
        except OSError:
            pass
        out[name] = problems
    return out


def _resident_python() -> str:
    """An interpreter the resident can run: its own venv, else the system one."""
    try:
        venv = Path(pwd.getpwnam(RESIDENT_USER).pw_dir) / ".local" / "venv" / "bin" / "python3"
        if os.access(venv, os.X_OK):
            return str(venv)
    except (KeyError, OSError):
        pass
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"


def _runs_as_resident(zone: str) -> bool:
    """An agent-zone plugin's tests run as the resident when the caller is
    the owner: its directory is the resident's to write, so the code that
    runs is whatever the resident left there, checked or not."""
    return zone != "owner" and _resident_exists() and _owner_trust.is_owner_process()


def suite_command(core: Path, pdir: Path, zone: str) -> tuple[list[str], dict]:
    """(argv, env) for one plugin's pytest run, with the core on the path
    the way run-plugin.sh and the owner cron jobs provide it."""
    pyargs = ["-m", "pytest", "-q", "-p", "no:cacheprovider", str(pdir / "tests")]
    base = {"EA_CORE_DIR": str(core), "PYTHONPATH": f"{core}:{core / 'preprocessor'}",
            "PYTHONDONTWRITEBYTECODE": "1"}
    if _runs_as_resident(zone):
        home = pwd.getpwnam(RESIDENT_USER).pw_dir
        env_args = [f"{k}={v}" for k, v in {**base, "HOME": home,
                                             "PATH": "/usr/local/bin:/usr/bin:/bin"}.items()]
        return (["sudo", "-n", "-u", RESIDENT_USER, "env", "-i", *env_args,
                 _resident_python(), *pyargs], {"PATH": "/usr/bin:/bin"})
    return [sys.executable, *pyargs], {**os.environ, **base}


_run = subprocess.run


def run_tests(ctx: Context, core: Path, names=()) -> dict:
    """{name: (exit status or None, output tail)}: each named installed
    plugin's own pytest suite (its tests/ directory). None when the plugin
    ships no tests.

    A plugin runs only when its files match the owner's lock (R.verify,
    uncached); a mismatch is refused with EXIT_BLOCKED and the problems.
    An agent-zone plugin's suite runs as the resident when the caller is
    the owner (suite_command): its directory is the resident's to write, so a
    file could change between the check and the import."""
    lock = ctx.read_lock()
    out = {}
    for name in (names or sorted(lock["plugins"])):
        entry = lock["plugins"].get(name)
        if not isinstance(entry, dict):
            out[name] = (2, "not installed")
            continue
        zone = entry.get("zone", "agent")
        pdir = ctx.plugin_dir(name, zone)
        lp = R.LockedPlugin(name=name, zone=zone, dir=pdir, manifest=entry["manifest"],
                            entry=entry)
        problems = R.verify(lp, use_cache=False)
        if problems:
            out[name] = (EXIT_BLOCKED, "refused: the plugin does not match the lock: "
                         + "; ".join(problems[:5]))
            continue
        if not (pdir / "tests").is_dir():
            out[name] = (None, "")
            continue
        argv, env = suite_command(core, pdir, zone)
        cwd = "/" if argv[0] == "sudo" else str(pdir)
        p = _run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
        out[name] = (p.returncode, (p.stdout + p.stderr)[-1500:])
    return out


def update(ctx: Context, name: str, ref: str | None = None, accept: bool = False,
           policy: dict | None = None) -> dict:
    lock, entry = _locked(ctx, name)
    src_info = entry.get("source") or {}
    url = (src_info.get("url") or "").strip()
    if not url:
        raise PluginError(f"{name} has no source URL; reinstall it from git")
    use_ref = src_info.get("ref", "") if ref is None else ref
    stage, src, commit = fetch(ctx, url, use_ref)
    try:
        norm, summary = _load_checked(src)
        if norm["name"] != name:
            raise PluginError(f"the release is plugin {norm['name']}, not {name}")
        if norm["zone"] != entry.get("zone"):
            raise PluginError(f"the release moves {name} to the {norm['zone']} zone; "
                              f"uninstall and install it again")
        digest = PM.capability_digest(summary)
        accepted = (entry.get("accepted") or {}).get("digest")
        how = (entry.get("accepted") or {}).get("how", "")
        if digest != accepted:
            ctx.out(f"{name}: the capabilities changed since they were accepted.")
            _show(ctx, name, url, commit, summary)
            how = consent(url, summary, accept, policy)
        files, problems = R.hash_tree(src, norm["state_dirs"])
        if problems:
            raise PluginError("the release has problems: " + "; ".join(problems))
        if norm["zone"] == "owner":
            keep = _save_owner_state(ctx, name, norm["state_dirs"])
            try:
                _remove_owner(ctx, name)
                _place_owner(ctx, src, name, norm["state_dirs"])
            finally:
                _restore_owner_state(keep, _owner_dest(ctx, name), norm["state_dirs"])
        else:
            _swap_agent(ctx, src, name, norm["state_dirs"])
        entry.update({"source": {"url": url, "ref": use_ref, "commit": commit},
                      "updated_at": _now(), "manifest": norm, "capability_digest": digest,
                      "files": files})
        if digest != accepted:
            entry["accepted"] = {"digest": digest, "at": _now(), "how": how}
        lock["plugins"][name] = entry
        ctx.write_lock(lock)
        ctx.out(f"updated {name} to {commit[:12]}")
        return entry
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _save_owner_state(ctx: Context, name: str, state_dirs) -> Path | None:
    """Move an owner plugin's state dirs aside (within the private dir)
    before an update replaces the plugin."""
    dest = _owner_dest(ctx, name)
    if not dest.is_dir():
        return None
    keep = Path(tempfile.mkdtemp(prefix="state-", dir=ctx.private / STAGING_DIRNAME))
    for s in state_dirs:
        if (dest / s).is_dir():
            (keep / s).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest / s), str(keep / s))
    return keep


def _restore_owner_state(keep: Path | None, dest: Path, state_dirs) -> None:
    if keep is None:
        return
    try:
        for s in state_dirs:
            if not (keep / s).is_dir():
                continue
            target = dest / s
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(keep / s), str(target))
    finally:
        shutil.rmtree(keep, ignore_errors=True)


def set_enabled(ctx: Context, name: str, enabled: bool, accept: bool = False,
                policy: dict | None = None) -> dict:
    lock, entry = _locked(ctx, name)
    if enabled:
        summary = PM.capability_summary(entry["manifest"])
        digest = PM.capability_digest(summary)
        if (entry.get("accepted") or {}).get("digest") != digest:
            _show(ctx, name, (entry.get("source") or {}).get("url", ""),
                  (entry.get("source") or {}).get("commit", ""), summary)
            how = consent((entry.get("source") or {}).get("url", ""), summary, accept, policy)
            entry["accepted"] = {"digest": digest, "at": _now(), "how": how}
    entry["enabled"] = bool(enabled)
    entry["updated_at"] = _now()
    lock["plugins"][name] = entry
    ctx.write_lock(lock)
    ctx.out(f"{name}: {'enabled' if enabled else 'disabled'}")
    return entry


def uninstall(ctx: Context, name: str, yes: bool = False) -> None:
    lock, entry = _locked(ctx, name)
    zone = entry.get("zone", "agent")
    dest = ctx.plugin_dir(name, zone)
    if not yes:
        raise PluginError(f"this removes {dest} and its state; run again with --yes")
    if (entry.get("source") or {}).get("adopted") and entry["manifest"].get("manifest_version") == 1:
        ctx.out(f"{name} was adopted in place; its directory is kept, only the lock entry goes")
    elif zone == "owner":
        _remove_owner(ctx, name)
    else:
        _remove_agent(ctx, name)
    del lock["plugins"][name]
    ctx.write_lock(lock)
    ctx.out(f"uninstalled {name}")


def listing(ctx: Context) -> list[dict]:
    """One row per plugin: the locked ones and any unmanaged directory."""
    lock = ctx.read_lock()
    rows = []
    for name in sorted(lock["plugins"]):
        e = lock["plugins"][name]
        src = e.get("source") or {}
        rows.append({"name": name, "zone": e.get("zone"), "enabled": bool(e.get("enabled")),
                     "manifest": e.get("manifest", {}).get("manifest_version"),
                     "commit": (src.get("commit") or "")[:12], "url": src.get("url", ""),
                     "managed": True})
    try:
        dirs = sorted(p for p in ctx.agent_dir.iterdir() if (p / PM.MANIFEST_FILE).is_file())
    except OSError:
        dirs = []
    for d in dirs:
        if d.name in lock["plugins"]:
            continue
        try:
            raw = PM.read(d)
        except PM.ManifestError:
            continue
        rows.append({"name": d.name, "zone": "agent", "enabled": bool(raw.get("enabled")),
                     "manifest": PM.version(raw), "commit": "", "url": "", "managed": False})
    return rows


def cron_lines(ctx: Context, script: Path, log_dir: Path) -> list[str]:
    lines = []
    for plugin, job in R.owner_cron_jobs(lock=ctx.read_lock()):
        log = log_dir / f"plugin-{plugin}-{job['label']}.log"
        lines.append(f"{job['cron']}  {sys.executable} {script} run-owner {plugin} "
                     f"{job['label']} >> {log} 2>&1")
    return lines


def run_owner(ctx: Context, name: str, label: str) -> int:
    lock = ctx.read_lock()
    lp = R.get_plugin(name, lock=lock, private=ctx.private)
    if lp is None:
        raise PluginError(f"{name} is not an enabled plugin")
    job = next((j for j in lp.manifest["capabilities"]["owner_hooks"]["cron"]
                if j["label"] == label), None)
    if job is None:
        raise PluginError(f"{name} has no owner cron job {label!r}")
    try:
        R._require_owner_code(lp)
    except PermissionError as e:
        raise PluginError(str(e), EXIT_PROBLEMS)
    argv = R.command_argv(lp, job["command"])
    env = R.owner_command_env(name, lp.dir)
    p = subprocess.run(argv, cwd=str(lp.dir), env=env, timeout=job["timeout_seconds"])
    return p.returncode
