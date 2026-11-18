#!/usr/bin/env python3
"""Daily digest of changes to the ea-bot stores that steer later runs.

The resident (ea-bot) can write files that change what every later session
does: its Claude settings (hooks, permissions, MCP servers), the CLAUDE.md
files and auto-memory a session loads, the EA config (autonomy tiers and
per-contact overrides, the VIP list, the institution domains, the profile),
the page watches, the agent-zone plugin files the crons run and the bot
loads as skills, its systemd user units, its shell, login and git
settings and the Python start-up hooks in its site-packages. An
email that steers the resident could change one of them, and the change
would outlive the session. preferences-digest.py covers the preference
rules; this covers the rest.

Once a day an owner cron reads every watched file with the resident's
access (resident_io), hashes it, compares it with the owner-private
snapshot, and posts the changes to the health channel with a short diff:
a file added, removed or changed, and the named keys that changed. Values
under names that look like secrets are masked in the post. It posts only
on a change; the first run seeds the snapshot and posts nothing. A failed
post keeps the old snapshot, so the change is reported on the next run.

The snapshot (WATCH paths, their hashes and their text up to MAX_TEXT) is
`persistence-digest.json` in the owner-private directory, mode 0600,
written through owner_trust, so the resident can neither read it nor move
it past a change it made.

    python3 preprocessor/persistence_digest.py            # check, post on change
    python3 preprocessor/persistence_digest.py --dry-run  # print the report only
    python3 preprocessor/persistence_digest.py --list     # the files it watches

Cron (owner, daily):  40 6 * * *  python3 /opt/ea-system/scripts/persistence-digest.py
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

RESIDENT_USER = "ea-bot"
SNAPSHOT_FILE = "persistence-digest.json"
MAX_TEXT = 256 * 1024          # text kept for a diff; a larger file is hashed only
MAX_FILES = 2000               # per run, so a planted tree cannot stall the cron
DIFF_LINES = 14                # diff lines shown per file
POST_CHARS = 3800              # the post is cut here, with a pointer to --dry-run

# Each entry: (pattern, kind). {home} is ea-bot's home, {ea} the EA tree. A
# `*` segment matches any directory entry. "file" watches one file, "tree"
# every file below the directory (TREE_SKIP applies), "mcp" the MCP server
# blocks of a Claude state file (the rest of that file changes on every run).
WATCH: tuple[tuple[str, str], ...] = (
    ("{ea}/config/*", "file"),
    ("{ea}/state/page-watches.json", "file"),
    ("{ea}/.env", "file"),
    ("{ea}/CLAUDE.md", "file"),
    ("{ea}/plugins/*", "tree"),
    ("{home}/CLAUDE.md", "file"),
    ("{home}/CLAUDE.local.md", "file"),
    ("{home}/.claude/CLAUDE.md", "file"),
    ("{home}/.claude/settings.json", "file"),
    ("{home}/.claude/settings.local.json", "file"),
    ("{home}/.claude/plugins/installed_plugins.json", "file"),
    ("{home}/.claude/plugins/known_marketplaces.json", "file"),
    ("{home}/.claude/skills", "tree"),
    ("{home}/.claude/agents", "tree"),
    ("{home}/.claude/commands", "tree"),
    ("{home}/.claude/hooks", "tree"),
    ("{home}/.claude/projects/*/memory", "tree"),
    ("{home}/.claude.json", "mcp"),
    ("{home}/.local/state/ea-lanes/*/CLAUDE.md", "file"),
    ("{home}/.local/state/ea-lanes/*/.claude", "tree"),
    ("{home}/.config/systemd/user", "tree"),
    ("{home}/.config/environment.d", "tree"),
    ("{home}/.gitconfig", "file"),
    ("{home}/.config/git", "tree"),
    ("{home}/.bashrc", "file"),
    ("{home}/.profile", "file"),
    ("{home}/.bash_profile", "file"),
    ("{home}/.bash_login", "file"),
    ("{home}/.bash_logout", "file"),
    ("{home}/.bash_aliases", "file"),
    ("{home}/.zshenv", "file"),
    ("{home}/.zprofile", "file"),
    ("{home}/.zshrc", "file"),
    ("{home}/.zlogin", "file"),
    ("{home}/.zlogout", "file"),
    ("{home}/.pam_environment", "file"),
    # Python start-up hooks in the site-packages ea-bot can write (its
    # plugin venv, its user site, the deployed tree's venv): a .pth line
    # that starts with `import` runs in every interpreter on that path, and
    # so do sitecustomize and usercustomize. The rest of a venv is not read.
    *((f"{site}/{name}", "file")
      for site in ("{home}/.local/venv/lib/python*/site-packages",
                   "{home}/.local/lib/python*/site-packages",
                   "{home}/ea-system/.venv/lib/python*/site-packages")
      for name in ("*.pth", "sitecustomize.py", "usercustomize.py")),
)

# Left out on purpose: preferences.json has its own digest
# (preferences-digest.py); ea-channels.json is the bot's room registry and
# changes with every new conversation.
SKIP_FILES = frozenset({"{ea}/config/preferences.json", "{ea}/config/ea-channels.json"})

# Below a tree: VCS and cache directories, a plugin's state, logs and
# secrets (never read: the snapshot holds text), the Claude skill trash.
TREE_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".pytest_cache",
                            ".venv", "venv", "state", ".trash"})
TREE_SKIP_NAMES = ("*.pyc", "*.log", ".env.secrets", "*.secrets", ".DS_Store",
                   ".credentials.json", ".git-credentials",
                   # the Claude skill sync's own markers, rewritten every round
                   ".last-complete-round", ".bucket-*")

# Named keys whose change gets its own line, over the JSON at that path.
KEYS = {
    "{ea}/config/autonomy-tiers.json": ("per_contact_overrides", "tiers"),
    "{ea}/config/user-profile.json": ("institution.domains", "email"),
    "{ea}/config/ea-config.json": ("profile", "mail_gate", "chat"),
    "{home}/.claude/settings.json": ("hooks", "permissions", "env", "enabledPlugins"),
    "{home}/.claude/settings.local.json": ("hooks", "permissions", "env"),
}

_SECRET_LINE = re.compile(
    r'(?i)^(\s*[-+ ]?\s*"?[A-Za-z0-9_.\-]*(?:token|secret|passw|api[_-]?key|credential|'
    r'private[_-]?key|_key)[A-Za-z0-9_.\-]*"?\s*[:=]\s*)(.+)$')


# ── reading ea-bot space ─────────────────────────────────────────────

def _resident_home() -> str | None:
    try:
        return pwd.getpwnam(RESIDENT_USER).pw_dir
    except KeyError:
        return None


def _default_io():
    import resident_io
    return resident_io


class _Listed:
    """The io with each directory listed once per run."""

    def __init__(self, io):
        self._io, self._seen = io, {}

    def list_dir(self, path):
        if path not in self._seen:
            self._seen[path] = self._io.list_dir(path)
        return self._seen[path]

    def read_bytes(self, path):
        return self._io.read_bytes(path)

    def kind_of(self, path: str) -> str | None:
        head, name = path.rsplit("/", 1)
        return next((k for k, n in self.list_dir(head or "/") or [] if n == name), None)


def _expand(pattern: str, io) -> list[tuple[str, str]]:
    """[(path, kind letter)] for the entries that match `pattern` (absolute,
    `*` per segment). Intermediate segments match directories only."""
    parts = pattern.strip("/").split("/")
    current = ["/"]
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        nxt: list = []
        for base in current:
            if "*" not in seg:
                nxt.append(base.rstrip("/") + "/" + seg)
                continue
            for kind, name in io.list_dir(base) or []:
                if fnmatch.fnmatchcase(name, seg) and (last or kind == "d"):
                    nxt.append(base.rstrip("/") + "/" + name)
        current = nxt
    return [(p, "") for p in current]


def _skipped(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in TREE_SKIP_NAMES)


def _walk(root: str, io, budget: list) -> list[tuple[str, str]]:
    """[(path, kind)] for every entry below `root` the digest covers."""
    out: list = []
    stack = [root]
    while stack and budget[0] > 0:
        d = stack.pop()
        for kind, name in io.list_dir(d) or []:
            p = d.rstrip("/") + "/" + name
            if kind == "d":
                if name not in TREE_SKIP_DIRS:
                    stack.append(p)
                continue
            if _skipped(name):
                continue
            out.append((p, kind))
            budget[0] -= 1
            if budget[0] <= 0:
                break
    return out


def _entry(data: bytes | None, kind: str = "f") -> dict | None:
    if kind == "l":
        return {"sha256": "symlink", "size": 0, "kind": "symlink"}
    if kind == "o":
        return {"sha256": "special", "size": 0, "kind": "special"}
    if data is None:
        return None
    e = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "kind": "file"}
    if len(data) <= MAX_TEXT:
        try:
            e["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return e


def _mcp_view(data: bytes | None) -> bytes | None:
    """The MCP server blocks of a Claude state file, as stable JSON."""
    if data is None:
        return None
    try:
        doc = json.loads(data)
    except ValueError:
        return b'"unreadable JSON"'
    if not isinstance(doc, dict):
        return b"{}"
    view = {"mcpServers": doc.get("mcpServers") or {}}
    projects = doc.get("projects") if isinstance(doc.get("projects"), dict) else {}
    per = {k: v.get("mcpServers") for k, v in sorted(projects.items())
           if isinstance(v, dict) and v.get("mcpServers")}
    if per:
        view["projects"] = per
    return json.dumps(view, indent=1, sort_keys=True).encode()


def collect(home: str, ea: str, io=None) -> dict:
    """{display path: entry} for every watched file that exists."""
    io = _Listed(io or _default_io())
    subst = {"{home}": home.rstrip("/"), "{ea}": ea.rstrip("/")}

    def fill(s: str) -> str:
        for k, v in subst.items():
            s = s.replace(k, v)
        return s

    skip = {fill(s) for s in SKIP_FILES}
    budget = [MAX_FILES]
    snap: dict = {}
    for pattern, how in WATCH:
        for path, _ in _expand(fill(pattern), io):
            if path in skip:
                continue
            if how == "tree":
                for f, kind in _walk(path, io, budget):
                    if f in skip:
                        continue
                    e = _entry(io.read_bytes(f) if kind == "f" else None, kind)
                    if e:
                        snap[f] = e
                continue
            if budget[0] <= 0:
                break
            if how == "mcp":
                if io.kind_of(path) != "f":
                    continue
                e = _entry(_mcp_view(io.read_bytes(path)))
                if e:
                    snap[path + "#mcpServers"] = e
                budget[0] -= 1
                continue
            kind = io.kind_of(path)
            if kind is None or kind == "d":
                continue
            e = _entry(io.read_bytes(path) if kind == "f" else None, kind)
            if e:
                snap[path] = e
            budget[0] -= 1
    return snap


# ── comparing ────────────────────────────────────────────────────────

def compare(old: dict, new: dict) -> dict:
    """{"added": [...], "removed": [...], "changed": [...]} by path."""
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(p for p in set(old) & set(new)
                          if old[p].get("sha256") != new[p].get("sha256")),
    }


def _dig(doc, dotted: str):
    for k in dotted.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


def _json(text):
    try:
        return json.loads(text) if text is not None else None
    except ValueError:
        return None


def _lines(entry: dict | None) -> list[str] | None:
    """The entry's text as lines for a diff (JSON normalized), or None."""
    if not entry or "text" not in entry:
        return None
    text = entry["text"]
    doc = _json(text) if text.lstrip()[:1] in ("{", "[") else None
    if doc is not None:
        text = json.dumps(doc, indent=1, sort_keys=True)
    return text.splitlines()


def mask(line: str) -> str:
    """A diff line with the value under a secret-looking name masked."""
    m = _SECRET_LINE.match(line)
    return m.group(1) + "…" if m else line


def _env_mask(line: str, path: str) -> str:
    if path.endswith("/.env"):
        m = re.match(r"^([-+ ]\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=).*$", line)
        if m:
            return m.group(1) + "…"
    return mask(line)


def changed_keys(path: str, old: dict | None, new: dict | None, home: str, ea: str) -> list[str]:
    for pat, keys in KEYS.items():
        if pat.replace("{home}", home.rstrip("/")).replace("{ea}", ea.rstrip("/")) != path:
            continue
        a = _json((old or {}).get("text"))
        b = _json((new or {}).get("text"))
        return [k for k in keys if _dig(a, k) != _dig(b, k)]
    return []


def _shown(path: str, home: str, ea: str) -> str:
    ea, home = ea.rstrip("/"), home.rstrip("/")
    if path.startswith(ea + "/"):
        return "~/.ea/" + path[len(ea) + 1:]
    if path.startswith(home + "/"):
        return f"~{RESIDENT_USER}/" + path[len(home) + 1:]
    return path


def _diff(path: str, old: dict | None, new: dict | None) -> list[str]:
    a, b = _lines(old), _lines(new)
    if a is None and old is not None or b is None and new is not None:
        return []
    out = [ln for ln in difflib.unified_diff(a or [], b or [], lineterm="", n=1)
           if not ln.startswith(("---", "+++"))]
    out = [_env_mask(ln, path) for ln in out]
    if len(out) > DIFF_LINES:
        out = out[:DIFF_LINES] + [f"… {len(out) - DIFF_LINES} more line(s)"]
    return out


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def render(report: dict, old: dict, new: dict, home: str, ea: str) -> str:
    """The chat post for a non-empty report, or "" when nothing changed."""
    n = sum(len(v) for v in report.values())
    if not n:
        return ""
    lines = [f":mag: *EA persistence watch*: {n} change(s) in ea-bot's stores since the "
             "last check. A change you did not make came from an EA session."]
    for kind, verb in (("changed", "changed"), ("added", "added"), ("removed", "removed")):
        for path in report[kind]:
            o, w = old.get(path), new.get(path)
            note = ""
            keys = changed_keys(path, o, w, home, ea)
            if keys:
                note = f" (keys: {', '.join(keys)})"
            elif kind == "added":
                note = f" ({(w or {}).get('kind', 'file')}, {_size((w or {}).get('size', 0))})"
            lines.append(f"• {verb} `{_shown(path, home, ea)}`{note}")
            d = _diff(path, o if kind != "added" else None, w if kind != "removed" else None)
            if d and kind == "changed":
                lines.append("```diff\n" + "\n".join(d) + "\n```")
    text = "\n".join(lines)
    if len(text) > POST_CHARS:
        text = (text[:POST_CHARS].rsplit("\n", 1)[0]
                + "\n```\n_Cut here: the owner's `persistence_digest.py --dry-run` prints the rest._")
    return text


# ── the snapshot ─────────────────────────────────────────────────────

def snapshot_path() -> Path:
    import owner_trust
    return owner_trust.private_dir() / SNAPSHOT_FILE


def read_snapshot(path: Path) -> dict | None:
    import owner_trust
    try:
        doc = json.loads(owner_trust.read_owner_file(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, owner_trust.OwnerTrustError) as e:
        print(f"persistence-digest: snapshot refused ({e}); starting over", file=sys.stderr)
        return None
    return doc.get("files") if isinstance(doc, dict) and isinstance(doc.get("files"), dict) else None


def write_snapshot(path: Path, files: dict) -> None:
    import owner_trust
    doc = {"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files}
    owner_trust.write_owner_file(path, json.dumps(doc, sort_keys=True), mode=0o600)


def _post(text: str) -> bool:
    """An alert: a change in ea-bot's stores the owner did not make came
    from an EA session, so it posts whether or not debug mode is on
    (chat_notify, Debug mode)."""
    from preprocessor import chat_notify
    return chat_notify.post(chat_notify.health_channel_key(), text)


def run(home: str, ea: str, *, snap_path: Path, io=None, post=_post,
        dry_run: bool = False) -> int:
    new = collect(home, ea, io)
    old = read_snapshot(snap_path)
    if old is None:
        if dry_run:
            print(f"persistence-digest: no snapshot yet; {len(new)} file(s) would be seeded")
            return 0
        write_snapshot(snap_path, new)
        print(f"persistence-digest: seeded {len(new)} file(s); nothing posted")
        return 0
    report = compare(old, new)
    text = render(report, old, new, home, ea)
    n = sum(len(v) for v in report.values())
    if dry_run:
        print(text or f"persistence-digest: no change in {len(new)} file(s)")
        return 0
    if text and not post(text):
        print("persistence-digest: post failed; snapshot kept", file=sys.stderr)
        return 1
    write_snapshot(snap_path, new)
    print(f"persistence-digest: {n} change(s) in {len(new)} file(s)"
          f"{' posted' if text else ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="digest of changes to ea-bot's behaviour stores")
    ap.add_argument("--dry-run", action="store_true", help="print the report; no post, no write")
    ap.add_argument("--list", action="store_true", help="print the watched files")
    a = ap.parse_args(argv)
    home = _resident_home()
    if home is None:
        print(f"persistence-digest: no {RESIDENT_USER} user", file=sys.stderr)
        return 1
    import owner_trust
    ea = os.path.realpath(owner_trust.install_ea_dir())
    if a.list:
        for path, e in sorted(collect(home, ea).items()):
            print(f"{e['sha256'][:12]}  {e['size']:>8}  {_shown(path, home, ea)}")
        return 0
    return run(home, ea, snap_path=snapshot_path(), dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
