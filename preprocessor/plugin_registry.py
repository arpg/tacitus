"""The plugin registry: which plugins are installed, enabled and trusted, and
what each one may do (docs/plugins.md).

Two kinds of plugin manifest exist (plugin_manifest.py):

- **v1** plugins (no `manifest_version`) behave as they always did. Their
  own `plugin.json` under ``~/.ea/plugins/<name>/`` decides `enabled`, the
  cron schedule and the ``slack`` (or ``chat``) block that the live bot
  loads into its prompt when a message matches::

      "slack": {
          "skill": "skill.md",
          "keywords": ["<trigger word>", "<vendor name>"],
          "patterns": ["\\\\b(re)?schedule\\\\b.{0,40}\\\\b<thing>\\\\b"]
      }

- **v2** plugins (``"manifest_version": 2``) are installed with
  ``scripts/ea-plugin.py``. The owner's lock file (``plugins.lock.json`` in
  the owner-private directory) records each one: its source URL, ref and
  commit, the SHA-256 of every file, the manifest the owner accepted, and
  whether it is enabled. An owner process acts on the manifest copy in the
  lock, never on the live ``plugin.json``: the resident can edit files in
  its own space, and that edit changes nothing the owner does until the
  owner runs ``ea-plugin.py update`` and accepts again.

Two trust zones:

- **agent** plugins live in ``<EA_DIR>/plugins/<name>``, the resident's
  space. Their code runs as the resident. The integrity check reports
  drift there; it is not a boundary against the resident.
- **owner** plugins live in ``<private_dir>/plugins/<name>``, owner only.
  Owner processes load their code (mediated verbs, auth sources, owner
  cron jobs, owner card executors) only from there, after the owner_trust
  checks and a clean integrity check.

SECURITY NOTE: an interactive plugin's skill is injected into the main bot
prompt, which runs with the bot's tool allowlist. Interactive plugin skills
are therefore as privileged as core skills, so installation is trust.
"""

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

try:
    import owner_trust as _owner_trust
    import plugin_manifest as PM
except ModuleNotFoundError:  # imported as preprocessor.plugin_registry
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import plugin_manifest as PM

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
PLUGINS_DIR = EA_DIR / "plugins"
# The core checkout this module belongs to. Owner plugin commands get it as
# EA_CORE_DIR, with its preprocessor directory on PYTHONPATH.
CORE_DIR = Path(__file__).resolve().parent.parent

LOCK_FILE = "plugins.lock.json"
LOCK_VERSION = 1
OWNER_PLUGINS_DIRNAME = "plugins"
# The resident's copy of the verb aliases (`graph_actions get-teams-dms` ->
# plugin teams-dms). A hint only: the mediator re-checks every request
# against the owner's lock.
ALIAS_HINT_FILE = "plugin-verbs.json"
POLICY_FILE = "plugins-policy.json"

# What the integrity check leaves out: version control, caches, logs and
# the plugin's own state and secrets.
INTEGRITY_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".pytest_cache"})
INTEGRITY_SKIP_SUFFIXES = (".pyc", ".log")
INTEGRITY_SKIP_NAMES = frozenset({".env.secrets", ".DS_Store"})

# Duck-typed by prompt_logic.detect_skills: it only reads .name / .keywords
# / .patterns, so any object with those attributes works (tests can pass a
# plain namedtuple without importing this module's filesystem code).
InteractivePlugin = namedtuple(
    "InteractivePlugin", ["name", "skill_path", "keywords", "patterns"]
)

# A plugin the owner's lock records.
LockedPlugin = namedtuple("LockedPlugin", ["name", "zone", "dir", "manifest", "entry"])

# Process-lifetime cache keyed on the manifest set's mtimes, so the bot pays
# the directory scan once but still picks up a plugin.json edit on the next
# call without a restart.
_CACHE: dict = {"sig": None, "plugins": None}
_LOCK_CACHE: dict = {"key": None, "lock": None}
_VERIFY_CACHE: dict = {}


def _warn(msg: str) -> None:
    print(f"[plugin_registry] {msg}", file=sys.stderr)


# ── v1 discovery (unchanged behaviour) ────────────────────────────────

def _dir_signature(plugins_dir: Path) -> tuple:
    """Cheap fingerprint of every plugin.json (path + mtime) under the dir."""
    out = []
    try:
        manifests = sorted(plugins_dir.glob("*/plugin.json"))
    except OSError:
        return ()
    for mp in manifests:
        try:
            out.append((str(mp), mp.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(out)


def _interactive_from(name: str, pdir: Path, chat: dict):
    """One InteractivePlugin for a chat block, or None (with a warning)."""
    skill_path = pdir / chat.get("skill", "skill.md")
    keywords = tuple(
        k.lower() for k in chat.get("keywords", []) if isinstance(k, str)
    )
    patterns = []
    for pat in chat.get("patterns", []):
        if not isinstance(pat, str):
            continue
        try:
            patterns.append(re.compile(pat))
        except re.error as e:
            _warn(f"{name}: bad pattern {pat!r}: {e}")
    if not skill_path.exists():
        _warn(f"{name}: slack.skill {skill_path} not found; skipping")
        return None
    if not keywords and not patterns:
        _warn(f"{name}: slack block has no keywords or patterns; skipping")
        return None
    return InteractivePlugin(name=name, skill_path=skill_path,
                             keywords=keywords, patterns=tuple(patterns))


def discover_interactive_plugins(plugins_dir=None, lock=None) -> list:
    """Return ``[InteractivePlugin]`` for enabled plugins with a chat block.

    v1 plugins come from their own manifests under the plugins dir. v2
    plugins come from the owner's lock (enabled, accepted), and only on the
    default scan or when a lock is passed in: a test that hands in its own
    plugins dir never sees the live lock.

    Cached by manifest mtimes; rescans only when a plugin.json changes.
    Never raises: a malformed manifest, bad regex, or missing skill file is
    skipped with a stderr warning so one broken plugin can't take the bot down.
    """
    pdir = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    use_lock = plugins_dir is None or lock is not None
    # A v2 plugin acts only while its files match the lock, so the cache key
    # carries the plugins that pass the check now.
    verified = verified_plugins(lock=lock) if use_lock else []
    sig = (_dir_signature(pdir), _lock_signature(lock) if use_lock else None,
           tuple(lp.name for lp in verified))
    if _CACHE["sig"] == sig and _CACHE["plugins"] is not None:
        return _CACHE["plugins"]

    plugins: list = []
    try:
        manifests = sorted(pdir.glob("*/plugin.json"))
    except OSError:
        manifests = []

    # A directory the owner's lock names is read only through the lock,
    # whatever its live plugin.json says: a resident that rewrites a locked
    # plugin's manifest as v1 gets no skill, grant or schedule back.
    locked = locked_names(lock) if use_lock else set()
    for mp in manifests:
        try:
            manifest = json.loads(mp.read_text())
        except Exception as e:  # bad JSON, unreadable, etc.
            _warn(f"skipping bad manifest {mp}: {e}")
            continue
        if not isinstance(manifest, dict) or PM.version(manifest) == 2:
            continue  # a v2 plugin acts only through the owner's lock
        if mp.parent.name in locked or str(manifest.get("name") or "") in locked:
            continue
        if not manifest.get("enabled"):
            continue
        # "chat" is the frontend-neutral name for the same block; "slack"
        # wins when a manifest has both.
        slack = manifest.get("slack") or manifest.get("chat")
        if not isinstance(slack, dict):
            continue  # scheduled-only plugin; not interactive
        name = manifest.get("name") or mp.parent.name
        p = _interactive_from(name, mp.parent, slack)
        if p is not None:
            plugins.append(p)

    if use_lock:
        for lp in verified:
            chat = lp.manifest["capabilities"].get("chat")
            if lp.manifest["manifest_version"] == 2 and chat:
                p = _interactive_from(lp.name, lp.dir, chat)
                if p is not None:
                    plugins.append(p)

    _CACHE["sig"] = sig
    _CACHE["plugins"] = plugins
    return plugins


# ── tool grants: one rule per entry point ─────────────────────────────
#
# A permission rule's `*` matches across `/`, so a template such as
# `Bash(python3 {plugin_dir}/*.py *)` taken literally also matches a script
# under the plugin's state dir, which the chat and cron lanes may write
# (resident_write_paths.py). Every grant is therefore expanded into one rule
# per file: the files the owner's lock hashed for a v2 plugin, the regular
# files on disk for a v1 plugin, never a file under a state dir, a skipped
# dir or a symlink, and never a name that holds a glob character.

V1_CHAT_TEMPLATE = "Bash(python3 {plugin_dir}/*.py *)"
V1_RUN_TEMPLATE = "Bash(python3 {plugin_dir}/** *)"
_GRANT_TEMPLATE_RE = re.compile(r"^Bash\(python3 \{plugin_dir\}/(?P<path>[^ ()]+)(?P<args> \*)?\)$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _glob_match(parts: list, pats: list) -> bool:
    """Segment-wise glob: `*` and `?` stay inside one path segment, a `**`
    segment matches zero or more segments."""
    import fnmatch
    if not pats:
        return not parts
    if pats[0] == "**":
        return any(_glob_match(parts[i:], pats[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) \
        and _glob_match(parts[1:], pats[1:])


def _in_state(rel: str, state_dirs) -> bool:
    for s in state_dirs or PM.DEFAULT_STATE_DIRS:
        s = str(s).strip("/")
        if s and (rel == s or rel.startswith(s + "/")):
            return True
    return False


def plugin_files_on_disk(root, state_dirs=PM.DEFAULT_STATE_DIRS) -> list[str]:
    """Relative paths of the regular files under `root` outside its state
    dirs and the integrity skip list, for a plugin no lock describes."""
    files, _problems = hash_tree(root, state_dirs)
    return sorted(files)


def expand_tool_grants(templates, root, files, state_dirs=PM.DEFAULT_STATE_DIRS) -> list[str]:
    """One `Bash(python3 <root>/<file>[ *])` rule per file in `files` that a
    template's path glob names. A file under a state dir, or with a glob
    character in its path, gets no rule; a malformed template gets none."""
    root = str(root).rstrip("/")
    out: list = []
    for t in templates:
        m = _GRANT_TEMPLATE_RE.match(str(t))
        if not m or ".." in m.group("path").split("/"):
            continue
        pats = m.group("path").split("/")
        args = m.group("args") or ""
        for rel in sorted(files):
            parts = rel.split("/")
            if not all(_SAFE_SEGMENT_RE.match(p) for p in parts):
                continue
            if _in_state(rel, state_dirs) or not _glob_match(parts, pats):
                continue
            rule = f"Bash(python3 {root}/{rel}{args})"
            if rule not in out:
                out.append(rule)
    return out


def locked_tool_grants(lp, root=None) -> list[str]:
    """A locked plugin's accepted tool templates over the files its lock
    entry hashed, rooted at `root` (default: where the plugin lives)."""
    return expand_tool_grants(lp.manifest["capabilities"]["tools"],
                              root if root is not None else lp.dir,
                              (lp.entry.get("files") or {}).keys(),
                              lp.manifest.get("state_dirs") or PM.DEFAULT_STATE_DIRS)


def plugin_tool_grants(plugins_dir=None, lock=None) -> list:
    """Allowlist patterns for interactive plugins' own scripts.

    For an enabled v1 interactive plugin, one rule per top-level `.py` file
    of its directory, so a plugin whose skill the bot loads can run its own
    code. A v2 plugin's grants come from its accepted manifest over the
    files its lock entry hashed, and only while its files match the lock
    (verified_plugins). No rule names a file under a state dir. Gated verbs
    stay gated inside the scripts themselves (confirm tokens), as with the
    core preprocessor tools.
    """
    grants = []
    managed = locked_names(lock) if (plugins_dir is None or lock is not None) else set()
    for p in discover_interactive_plugins(plugins_dir, lock=lock):
        if p.name in managed:
            continue
        pdir = Path(p.skill_path).parent
        grants += expand_tool_grants([V1_CHAT_TEMPLATE], pdir, plugin_files_on_disk(pdir))
    if plugins_dir is None or lock is not None:
        # Only a plugin whose files match the lock: its scripts are what the
        # grant lets the resident run.
        for lp in verified_plugins(lock=lock):
            if lp.manifest["manifest_version"] != 2:
                continue
            grants += locked_tool_grants(lp)
    return grants


def run_tool_grants(name: str, root, lock=None) -> list[str]:
    """The Bash grants run-plugin.sh gives one plugin's scheduled run, with
    `root` as the plugin directory the run sees. A locked plugin gets its
    accepted templates over its locked files, and nothing when its files no
    longer match; any other plugin gets python3 on each file of its
    directory outside the state dirs."""
    if name in locked_names(lock):
        lp = verified_plugin(name, lock=lock)
        if lp is None:
            return []
        # An owner-zone plugin's verified files are in the owner's space,
        # never in the resident's copy of the directory.
        return locked_tool_grants(lp, root if lp.zone == "agent" else None)
    return expand_tool_grants([V1_RUN_TEMPLATE], root, plugin_files_on_disk(root))


def skill_path_map(plugins=None) -> dict:
    """Map plugin name -> skill_path for the bot's skill loader."""
    if plugins is None:
        plugins = discover_interactive_plugins()
    return {p.name: p.skill_path for p in plugins}


# ── the owner's lock ──────────────────────────────────────────────────

def _empty_lock() -> dict:
    return {"lock_version": LOCK_VERSION, "plugins": {}}


def _is_owner() -> bool:
    try:
        return _owner_trust.is_owner_process()
    except Exception:
        return False


def lock_path(private=None) -> Path:
    return Path(private or _owner_trust.private_dir()) / LOCK_FILE


def owner_plugins_dir(private=None) -> Path:
    return Path(private or _owner_trust.private_dir()) / OWNER_PLUGINS_DIRNAME


def agent_plugins_dir() -> Path:
    return Path(PLUGINS_DIR)


def _lock_signature(lock) -> tuple:
    if isinstance(lock, dict):
        return ("dict", id(lock))
    path = Path(lock) if lock is not None else (lock_path() if _is_owner() else None)
    if path is None:
        return ("none",)
    try:
        st = os.stat(path)
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None)


def read_lock(path=None) -> dict:
    """The owner's lock: {"lock_version": 1, "plugins": {name: entry}}.

    Read with the owner checks. Empty outside an owner process (the
    resident cannot read it), when it is absent, or when it fails a check
    (with a warning): no v2 plugin acts then."""
    if path is None:
        if not _is_owner():
            return _empty_lock()
        path = lock_path()
    path = Path(path)
    key = _lock_signature(path)
    if _LOCK_CACHE["key"] == key and _LOCK_CACHE["lock"] is not None:
        return _LOCK_CACHE["lock"]
    try:
        data = json.loads(_owner_trust.read_owner_file(path))
    except FileNotFoundError:
        data = _empty_lock()
    except Exception as e:
        _warn(f"lock {path} refused, no v2 plugin acts: {e}")
        data = _empty_lock()
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        data = _empty_lock()
    _LOCK_CACHE["key"], _LOCK_CACHE["lock"] = key, data
    return data


def write_lock(data: dict, path=None) -> None:
    """Replace the owner's lock (owner checks on the private dir)."""
    path = Path(path) if path is not None else lock_path()
    data = {"lock_version": LOCK_VERSION, "plugins": dict(data.get("plugins") or {})}
    _owner_trust.write_owner_file(path, json.dumps(data, indent=1, sort_keys=True) + "\n",
                                  mode=0o600)
    _LOCK_CACHE["key"] = None


def _as_lock(lock) -> dict:
    if isinstance(lock, dict):
        return lock
    return read_lock(lock)


def plugin_dir(name: str, zone: str, private=None) -> Path:
    """Where a plugin of `zone` lives."""
    if zone == "owner":
        return owner_plugins_dir(private) / name
    return agent_plugins_dir() / name


def managed_plugins(lock=None, enabled_only: bool = True, private=None) -> list:
    """[LockedPlugin] for the lock's v2 entries whose accepted capabilities
    match the manifest the lock holds. An entry whose digest no longer
    matches what was accepted is skipped with a warning."""
    out = []
    plugins = _as_lock(lock).get("plugins") or {}
    for name in sorted(plugins):
        entry = plugins[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("manifest"), dict):
            continue
        if enabled_only and not entry.get("enabled"):
            continue
        manifest = entry["manifest"]
        if manifest.get("manifest_version") != 2:
            continue
        if not PM.NAME_RE.match(name) or manifest.get("name") != name:
            _warn(f"lock entry {name!r} names a different plugin; skipped")
            continue
        try:
            digest = PM.capability_digest(PM.capability_summary(manifest))
        except (KeyError, TypeError) as e:
            _warn(f"lock entry {name}: bad manifest ({e}); skipped")
            continue
        if (entry.get("accepted") or {}).get("digest") != digest:
            _warn(f"{name}: capabilities changed since they were accepted; skipped "
                  f"(run ea-plugin.py enable {name} --accept-capabilities)")
            continue
        zone = manifest.get("zone", "agent")
        out.append(LockedPlugin(name=name, zone=zone, dir=plugin_dir(name, zone, private),
                                manifest=manifest, entry=entry))
    return out


def get_plugin(name: str, lock=None, private=None):
    """The enabled, accepted LockedPlugin called `name`, or None."""
    for lp in managed_plugins(lock=lock, private=private):
        if lp.name == name:
            return lp
    return None


# ── integrity ─────────────────────────────────────────────────────────

def _skip(rel_parts: tuple, state_dirs) -> bool:
    if any(p in INTEGRITY_SKIP_DIRS for p in rel_parts[:-1]):
        return True
    name = rel_parts[-1]
    if name in INTEGRITY_SKIP_NAMES or name.endswith(INTEGRITY_SKIP_SUFFIXES):
        return True
    top = rel_parts[0] if len(rel_parts) > 1 else ""
    return top in state_dirs


def hash_tree(root, state_dirs=PM.DEFAULT_STATE_DIRS) -> tuple[dict, list[str]]:
    """({relative path: sha256}, problems) for every file under `root` the
    integrity check covers. A symlink is a problem: a plugin ships files."""
    root = Path(root)
    files: dict = {}
    problems: list = []
    state = {str(s).strip("/").split("/")[0] for s in state_dirs}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        keep = []
        for d in dirnames:
            full = Path(dirpath) / d
            rel = rel_dir / d
            if os.path.islink(full):
                problems.append(f"{rel}: a symlink")
                continue
            if d in INTEGRITY_SKIP_DIRS or (rel_dir == Path(".") and d in state):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            full = Path(dirpath) / f
            rel = (rel_dir / f)
            parts = rel.parts
            if _skip(parts, state):
                continue
            if os.path.islink(full):
                problems.append(f"{rel}: a symlink")
                continue
            h = hashlib.sha256()
            try:
                # No link is followed and no FIFO blocks the read: the agent
                # zone is the resident's to change between the check and the
                # open.
                fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | getattr(os, "O_CLOEXEC", 0))
            except OSError as e:
                problems.append(f"{rel}: unreadable ({e})")
                continue
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    problems.append(f"{rel}: not a regular file")
                    continue
                with os.fdopen(fd, "rb", closefd=False) as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
            except OSError as e:
                problems.append(f"{rel}: unreadable ({e})")
                continue
            finally:
                os.close(fd)
            files[rel.as_posix()] = h.hexdigest()
    return files, problems


def _tree_signature(root: Path) -> tuple:
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in INTEGRITY_SKIP_DIRS]
        for f in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            # ctime and the inode change on every write and replace, and no
            # user can set them back, so an edit that restores the size and
            # mtime still misses the cache.
            out.append((dirpath, f, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                        st.st_ino, st.st_mode))
    return tuple(sorted(out))


def verify(lp, use_cache: bool = True) -> list[str]:
    """Problems with an installed plugin against its lock entry: a file
    missing, changed or added, a symlink, or (owner zone) a directory that
    fails the owner checks. [] when it matches."""
    root = Path(lp.dir)
    problems = []
    if lp.zone == "owner":
        try:
            _owner_trust.check_owner_dir(root)
        except Exception as e:
            return [f"{root}: {e}"]
    if not root.is_dir():
        return [f"{root}: not installed"]
    sig = _tree_signature(root)
    key = (str(root), lp.entry.get("commit") or (lp.entry.get("source") or {}).get("commit"))
    cached = _VERIFY_CACHE.get(key)
    if use_cache and cached and cached[0] == sig:
        return list(cached[1])
    want = lp.entry.get("files") or {}
    have, problems = hash_tree(root, lp.manifest.get("state_dirs") or PM.DEFAULT_STATE_DIRS)
    for rel in sorted(set(want) | set(have)):
        if rel not in have:
            problems.append(f"{rel}: missing")
        elif rel not in want:
            problems.append(f"{rel}: not in the installed release")
        elif want[rel] != have[rel]:
            problems.append(f"{rel}: changed since install")
    _VERIFY_CACHE[key] = (sig, tuple(problems))
    return problems


_INTEGRITY_FAILED: dict = {}
_INTEGRITY_CHECKED: set = set()
INTEGRITY_ALERTS_FILE = "plugin-integrity-alerts.json"


def verified_plugins(lock=None, private=None) -> list:
    """managed_plugins() whose installed files match the lock. A plugin
    that fails the check is left out (it gets no skill, tool grant or
    scheduled run), with one warning per distinct problem set, and is
    listed by integrity_failures() until it passes again."""
    out = []
    for lp in managed_plugins(lock=lock, private=private):
        problems = verify(lp)
        _INTEGRITY_CHECKED.add(lp.name)
        if problems:
            key = tuple(problems)
            if _INTEGRITY_FAILED.get(lp.name) != key:
                _warn(f"{lp.name}: integrity check failed, the plugin does not act: "
                      f"{'; '.join(problems[:3])}")
            _INTEGRITY_FAILED[lp.name] = key
            continue
        _INTEGRITY_FAILED.pop(lp.name, None)
        out.append(lp)
    return out


def verified_plugin(name: str, lock=None, private=None):
    """The enabled, accepted LockedPlugin called `name` whose files match
    the lock, or None."""
    for lp in verified_plugins(lock=lock, private=private):
        if lp.name == name:
            return lp
    return None


def integrity_failures() -> dict:
    """{plugin: [problem]} for the plugins this process found changed."""
    return {k: list(v) for k, v in _INTEGRITY_FAILED.items()}


def _post_health(text: str) -> bool:
    try:
        import chat_notify
    except ModuleNotFoundError:
        from preprocessor import chat_notify
    return bool(chat_notify.post(chat_notify.health_channel_key(), text,
                                 priority="urgent", _bypass_quiet=True))


def alert_integrity_failures(failures=None, private=None, post=None, checked=None) -> int:
    """Post each plugin's integrity failure to the health channel once per
    distinct problem set, across processes (the owner-private
    INTEGRITY_ALERTS_FILE remembers what was posted). A plugin in `checked`
    (default: every plugin this process checked) that passes is forgotten,
    so a later failure posts again; the record of any other plugin is kept.
    Owner processes only. Returns the number of posts made."""
    if not _is_owner() and private is None:
        return 0
    failures = integrity_failures() if failures is None else failures
    checked = set(_INTEGRITY_CHECKED if checked is None else checked) | set(failures)
    path = Path(private or _owner_trust.private_dir()) / INTEGRITY_ALERTS_FILE
    try:
        seen = json.loads(_owner_trust.read_owner_file(path))
        seen = seen if isinstance(seen, dict) else {}
    except FileNotFoundError:
        seen = {}
    except Exception as e:
        _warn(f"integrity alert record refused ({e}); alerting again")
        seen = {}
    post = post or _post_health
    posted = 0
    keep = {k: v for k, v in seen.items() if k not in checked}
    for name, problems in sorted(failures.items()):
        digest = hashlib.sha256("\n".join(problems).encode()).hexdigest()[:16]
        keep[name] = digest
        if seen.get(name) == digest:
            continue
        shown = "\n".join(f"• {p}" for p in problems[:8])
        more = f"\n• … {len(problems) - 8} more" if len(problems) > 8 else ""
        text = (f":rotating_light: *Plugin integrity*: `{name}` no longer matches the "
                f"files the owner installed, so it gets no skill, tool grant or "
                f"scheduled run until it does.\n{shown}{more}\n"
                f"_Check it with `ea-plugin.py verify {name}`; restore it, or accept the "
                f"change with `ea-plugin.py update {name}`._")
        try:
            ok = post(text)
        except Exception as e:
            _warn(f"integrity alert for {name} not posted: {e}")
            ok = False
        if ok:
            posted += 1
        else:
            keep[name] = seen.get(name, "")
    if keep != seen:
        try:
            _owner_trust.write_owner_file(path, json.dumps(keep, sort_keys=True), mode=0o600)
        except Exception as e:
            _warn(f"integrity alert record not saved: {e}")
    return posted


def _require_owner_code(lp) -> None:
    """Raise unless the owner may run code from this plugin now."""
    if lp.zone != "owner":
        raise PermissionError(f"{lp.name}: an agent-zone plugin's code never runs as the owner")
    if not _is_owner():
        raise PermissionError(f"{lp.name}: owner code runs only in an owner process")
    problems = verify(lp)
    if problems:
        raise PermissionError(f"{lp.name}: integrity check failed: {'; '.join(problems[:5])}")


def owner_command_env(name: str, plugin_dir, base=None) -> dict:
    """The environment an owner plugin command runs with: `base` (default
    os.environ) plus EA_PLUGIN_NAME, EA_PLUGIN_DIR, EA_CORE_DIR and the
    core's preprocessor directory and root first on PYTHONPATH, so the
    plugin's code imports owner_trust and the other core modules it builds
    on, as an agent plugin's collect.py does under run-plugin.sh."""
    env = dict(os.environ if base is None else base)
    paths = [str(CORE_DIR / "preprocessor"), str(CORE_DIR)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env.update({"EA_PLUGIN_NAME": str(name), "EA_PLUGIN_DIR": str(plugin_dir),
                "EA_CORE_DIR": str(CORE_DIR), "PYTHONPATH": os.pathsep.join(paths)})
    return env


def command_argv(lp, command: list, token: str = "") -> list[str]:
    """A manifest command with its placeholders filled in."""
    fields = {"plugin_dir": str(lp.dir), "python": sys.executable, "token": token}
    out = []
    for part in command:
        for k, v in fields.items():
            part = part.replace("{" + k + "}", v)
        out.append(part)
    if out and out[0] == "python3":
        out[0] = sys.executable
    return out


# ── owner-side hooks ──────────────────────────────────────────────────

def _load_module(lp, rel: str):
    _require_owner_code(lp)
    path = Path(lp.dir) / rel
    _owner_trust.check_owner_file(path)
    _owner_trust.check_owner_dir(path.parent)
    stem = re.sub(r"[^A-Za-z0-9_]", "_", f"ea_plugin_{lp.name}_{Path(rel).with_suffix('')}")
    spec = importlib.util.spec_from_file_location(stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path}: cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owner_auth_tokens(kind: str = "graph", lock=None):
    """Yield the tokens the enabled owner plugins' auth sources of `kind`
    return, in plugin then manifest order. A source that fails is skipped
    with a warning; None or '' means it has no token now."""
    if lock is None and not _is_owner():
        return
    for lp in managed_plugins(lock=lock):
        for src in lp.manifest["capabilities"]["owner_hooks"]["auth_sources"]:
            if src.get("kind") != kind:
                continue
            try:
                fn = getattr(_load_module(lp, src["module"]), src["function"])
                token = fn()
            except Exception as e:
                _warn(f"{lp.name}: auth source {src.get('name')} failed: {e}")
                continue
            if token:
                yield str(token)


def owner_verb(plugin: str, verb: str, lock=None):
    """The verb spec an enabled owner plugin declares, or None."""
    lp = get_plugin(plugin, lock=lock)
    if lp is None or lp.zone != "owner":
        return None
    for v in lp.manifest["capabilities"]["owner_hooks"]["verbs"]:
        if v["name"] == verb:
            return v
    return None


def owner_verb_command(plugin: str, verb: str, lock=None) -> tuple[list, Path, int]:
    """(argv prefix, cwd, timeout) for an owner plugin verb, after the
    owner checks and the integrity check. The caller appends the verb and
    its arguments. Raises PermissionError or LookupError."""
    lp = get_plugin(plugin, lock=lock)
    spec = owner_verb(plugin, verb, lock=lock)
    if lp is None or spec is None:
        raise LookupError(f"no enabled owner verb {plugin} {verb}")
    _require_owner_code(lp)
    return command_argv(lp, spec["command"]), Path(lp.dir), int(spec["timeout_seconds"])


def owner_verb_process(plugin: str, argv: list, lock=None) -> subprocess.CompletedProcess:
    """Run `plugin <verb> args...` as the owner, here, and return the
    finished process with its output captured. Raises as
    owner_verb_command does, or subprocess.TimeoutExpired."""
    verb = argv[0] if argv else ""
    cmd, cwd, timeout = owner_verb_command(plugin, verb, lock=lock)
    return subprocess.run(cmd + list(argv), cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, env=owner_command_env(plugin, cwd))


def run_owner_verb(plugin: str, argv: list, lock=None) -> int:
    """Run `plugin <verb> args...` as the owner, here, and pass its output
    through. For an owner process that does not go through the mediator."""
    p = owner_verb_process(plugin, argv, lock=lock)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


def verb_aliases(tool: str, lock=None, hint_path=None) -> dict:
    """{alias verb: (plugin, verb)} for a core tool. An owner process reads
    the lock; a resident reads the hint file ea-plugin.py writes into its
    state dir (the mediator re-checks every request, so a forged hint
    reaches nothing)."""
    out = {}
    if lock is not None or (_is_owner() and hint_path is None):
        for lp in managed_plugins(lock=lock):
            if lp.zone != "owner":
                continue
            for v in lp.manifest["capabilities"]["owner_hooks"]["verbs"]:
                a = v.get("alias")
                if a and a.get("tool") == tool:
                    out[a["verb"]] = (lp.name, v["name"])
        return out
    path = Path(hint_path) if hint_path else EA_DIR / "state" / ALIAS_HINT_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    for alias, target in ((data or {}).get(tool) or {}).items():
        if (isinstance(target, dict) and PM.VERB_RE.match(str(alias))
                and PM.NAME_RE.match(str(target.get("plugin") or ""))
                and PM.VERB_RE.match(str(target.get("verb") or ""))):
            out[alias] = (target["plugin"], target["verb"])
    return out


def alias_hint(lock=None) -> dict:
    """The resident's hint file contents for the current lock."""
    out: dict = {}
    for lp in managed_plugins(lock=lock):
        if lp.zone != "owner":
            continue
        for v in lp.manifest["capabilities"]["owner_hooks"]["verbs"]:
            a = v.get("alias")
            if a:
                out.setdefault(a["tool"], {})[a["verb"]] = {"plugin": lp.name, "verb": v["name"]}
    return out


def owner_cron_jobs(lock=None) -> list[tuple]:
    """[(plugin, job)] for every enabled owner plugin's cron job."""
    out = []
    for lp in managed_plugins(lock=lock):
        if lp.zone == "owner":
            for job in lp.manifest["capabilities"]["owner_hooks"]["cron"]:
                out.append((lp.name, job))
    return out


# ── capabilities the core reads ───────────────────────────────────────

def cards(lock=None) -> list[tuple]:
    """[(LockedPlugin, card spec)] for every enabled plugin's cards."""
    return [(lp, c) for lp in managed_plugins(lock=lock)
            for c in lp.manifest["capabilities"]["cards"]]


def card_action_type(plugin: str, card_id: str) -> str:
    return f"plugin_card:{plugin}:{card_id}"


def card_pending_files(lock=None) -> list[tuple]:
    """[(action type, pending file name)] for the bot's claim protocol."""
    return [(card_action_type(lp.name, c["id"]), c["pending_file"]) for lp, c in cards(lock)]


def card_spec(action_type: str, lock=None):
    """(LockedPlugin, card spec) for a `plugin_card:<plugin>:<card>` action
    type, or None."""
    parts = action_type.split(":")
    if len(parts) != 3 or parts[0] != "plugin_card":
        return None
    for lp, c in cards(lock):
        if lp.name == parts[1] and c["id"] == parts[2]:
            return lp, c
    return None


def mail_rules(lock=None) -> dict:
    """The enabled plugins' mail rules, merged and sorted."""
    out = {"closure_confirm_senders": set(), "closure_exclude_senders": set(),
           "keep_from_anthropic_domains": set()}
    for lp in managed_plugins(lock=lock):
        rules = lp.manifest["capabilities"]["mail_rules"]
        for key in out:
            out[key].update(rules.get(key) or [])
    return {k: sorted(v) for k, v in out.items()}


def mail_triggers(lock=None) -> list[tuple]:
    """[(LockedPlugin, trigger)] for the mail triggers of the enabled
    plugins whose files match the lock: mail goes only to a plugin the
    owner accepted, as it was accepted."""
    out = []
    for lp in managed_plugins(lock=lock):
        triggers = lp.manifest["capabilities"].get("mail_triggers") or []
        # Only a plugin that asks for mail pays for the file hashes.
        if triggers and not verify(lp):
            out += [(lp, t) for t in triggers]
    return out


def netpolicy_allows(lock=None) -> list[dict]:
    """Allowlist entries for resident_netpolicy, one per local endpoint an
    enabled plugin declares, named after the plugin."""
    out = []
    for lp in managed_plugins(lock=lock):
        for e in lp.manifest["capabilities"]["network"]["local"]:
            out.append({"name": f"plugin:{lp.name}", "daddr": e["daddr"],
                        "proto": e["proto"], "ports": e["ports"]})
    return out


def denied_tools(lock=None) -> list[str]:
    """Tool denials the enabled plugins add to every lane's deny list."""
    out = []
    for lp in managed_plugins(lock=lock):
        out.extend(lp.manifest["capabilities"]["denied_tools"])
    return out


def claude_plugin_dirs(lock=None) -> list[Path]:
    """The Claude Code plugin roots (each holds .claude-plugin/plugin.json)
    of enabled agent plugins, for `claude --plugin-dir` in agent sessions."""
    out = []
    for lp in managed_plugins(lock=lock):
        rel = lp.manifest["capabilities"].get("claude_plugin")
        if rel and lp.zone == "agent":
            d = Path(lp.dir) / rel
            if (d / ".claude-plugin" / "plugin.json").is_file():
                out.append(d.resolve() if rel == "." else d)
    return out


def _pick_schedule(norm: dict, args) -> dict:
    scheds = norm["capabilities"]["schedules"]
    args = [str(a) for a in args]
    pick = next((s for s in scheds if s["args"] == args), scheds[0] if scheds else None)
    if pick is None:
        return {"label": "default", "llm": "claude", "model": norm["model"],
                "timeout_seconds": norm["timeout_seconds"], "skill": "skill.md"}
    return {k: pick[k] for k in ("label", "llm", "model", "timeout_seconds", "skill")}


def schedule_config(manifest: dict, args=(), name: str = "", lock=None) -> dict:
    """What run-plugin.sh runs for a plugin and its extra arguments: the
    schedule whose args match, else the first, with `enabled`.

    A v1 manifest decides for itself (its own `enabled`; llm claude, skill
    skill.md). A plugin the owner's lock names runs from the manifest in
    the lock and only when the lock enables it, whatever version the live
    plugin.json claims; the live file decides nothing."""
    name = name or str((manifest or {}).get("name") or "")
    if PM.version(manifest or {}) == 2 or name in locked_names(lock):
        lp = get_plugin(name, lock=lock)
        if lp is None:
            return {"enabled": False, "label": "", "llm": "claude", "model": "sonnet",
                    "timeout_seconds": 1800, "skill": "skill.md"}
        return {"enabled": True, **_pick_schedule(lp.manifest, args)}
    norm, _errors = PM.normalize(manifest or {})
    return {"enabled": bool(norm.get("enabled")), **_pick_schedule(norm, args)}


def locked_names(lock=None) -> set:
    """The names of every plugin in the owner's lock, enabled or not. A
    directory with one of these names is never read as a v1 plugin."""
    try:
        data = _as_lock(lock)
    except Exception:
        return set()
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return {str(n) for n in (plugins or {}) if isinstance(n, str)}


def _read_policy() -> dict:
    """The install policy: the owner-private plugins-policy.json, read by
    an owner process. Keys: trusted_sources and blocked_sources (fnmatch
    patterns over the source URL). There is no other source: ea-config.json
    is the resident's file, and a trusted source named there would let
    the resident install without consent."""
    try:
        if _is_owner():
            raw = json.loads(_owner_trust.read_owner_file(_owner_trust.private_dir() / POLICY_FILE))
            if isinstance(raw, dict):
                return raw
    except FileNotFoundError:
        pass
    except Exception as e:
        _warn(f"{POLICY_FILE} refused: {e}")
    return {}


def install_policy() -> dict:
    pol = _read_policy()
    return {"trusted_sources": [s for s in pol.get("trusted_sources") or [] if isinstance(s, str)],
            "blocked_sources": [s for s in pol.get("blocked_sources") or [] if isinstance(s, str)]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Plugin registry queries")
    ap.add_argument("--plugins-dir", default=None)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="interactive plugins (the default)")
    sub.add_parser("denied-tools", help="plugin tool denials, comma separated")
    sub.add_parser("tool-grants", help="the interactive plugins' Bash grants, one rule per "
                                      "entry point, comma separated")
    p_rg = sub.add_parser("run-grants", help="the Bash grants of one plugin's scheduled run, "
                                             "comma separated")
    p_rg.add_argument("--plugin", required=True)
    p_rg.add_argument("--dir", required=True, help="the plugin directory the run sees")
    p_sc = sub.add_parser("schedule-config",
                          help="what run-plugin.sh runs for the manifest on stdin and ARGS, as JSON")
    p_sc.add_argument("--plugin", default="")
    p_sc.add_argument("args", nargs="*")
    p_lk = sub.add_parser("locked", help="exit 0 when the owner's lock names the plugin")
    p_lk.add_argument("--plugin", required=True)
    p_mf = sub.add_parser("manifest",
                          help="the accepted manifest of an enabled locked plugin whose files "
                               "match the lock, as JSON (exit 3 when they do not)")
    p_mf.add_argument("--plugin", required=True)
    p_vf = sub.add_parser("verify", help="exit 0 when an enabled locked plugin's files match "
                                         "the lock, 3 when they do not, 1 when it is not enabled")
    p_vf.add_argument("--plugin", required=True)
    p_vf.add_argument("--alert", action="store_true",
                      help="post a failure to the health channel (once per problem set)")
    args = ap.parse_args()
    if args.cmd == "denied-tools":
        print(",".join(denied_tools()))
    elif args.cmd == "tool-grants":
        print(",".join(plugin_tool_grants()))
    elif args.cmd == "run-grants":
        print(",".join(run_tool_grants(args.plugin, args.dir)))
    elif args.cmd == "locked":
        sys.exit(0 if args.plugin in locked_names() else 1)
    elif args.cmd in ("manifest", "verify"):
        lp = get_plugin(args.plugin)
        if lp is None:
            sys.exit(1)
        problems = verify(lp, use_cache=False)
        if problems:
            for line in problems:
                print(f"{args.plugin}: {line}", file=sys.stderr)
            if getattr(args, "alert", False):
                alert_integrity_failures({args.plugin: problems}, checked={args.plugin})
            sys.exit(3)
        if getattr(args, "alert", False):
            alert_integrity_failures({}, checked={args.plugin})
        if args.cmd == "manifest":
            print(json.dumps(lp.manifest))
    elif args.cmd == "schedule-config":
        try:
            raw = json.load(sys.stdin)
        except ValueError:
            raw = {}
        print(json.dumps(schedule_config(raw if isinstance(raw, dict) else {}, args.args,
                                         name=args.plugin)))
    else:
        for p in discover_interactive_plugins(args.plugins_dir):
            print(f"{p.name}\t{p.skill_path}\tkeywords={len(p.keywords)} "
                  f"patterns={len(p.patterns)}")
