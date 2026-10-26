#!/usr/bin/env python3
"""Static check for the owner trust rules (docs/SECURITY.md, "Owner trust rules").

Owner processes (the bot, the mediator, the crons, the deploy hook) hold the
tokens and take nothing from the resident's space (`~/.ea`, `/home/ea-bot`)
on trust. This check reads the repository's code, runs none of it, and
reports each place that bypasses one of those rules, with the approved
helper to use instead:

  rule               reports                                      use instead
  py-write           a write, create, rename, chmod or temp file  resident_io.*, atomic_io.*
                     made by path in resident space
  py-rmw-read        a plain read of a resident file that the     resident_io.update_text
                     same code writes back                        (or read_text)
  py-trust-anchor    a read or write by path in the auth          owner_trust.read_owner_file,
                     directory (tokens, the scheduled-send key)   write_owner_file
  py-check-then-use  an ownership check made by path, then a      owner_trust.read_owner_file
                     use of that same path
  py-env-file        ~/.ea/.env or a secrets file read as text    owner_trust.read_env_values,
                                                                  owner_secret
  py-env-exec        an env file loaded or run (dotenv, exec, a   owner_trust.load_owner_env,
                     shell `source` in a command string)          scripts/owner-io.sh env
  py-sqlite          sqlite3.connect                              ledger.guarded_connect
  py-git             git run in a resident-space tree             the deploy hook's checked tree
  py-resident-module owner code that imports a resident-only      sudo -n -u ea-bot
                     tool, or runs one as the owner
  sh-source          `source`, `.` or `eval` of an env file or a  scripts/owner-io.sh env
                     file in resident space
  sh-io              a shell read or write of a resident path     scripts/owner-io.sh
  sh-git             git run in a resident-space tree             deploy_tree_ok + deploy_git
  cron-log           a crontab line whose output goes into        ~/.ea-private/logs
                     resident space
  unit-envfile       a systemd EnvironmentFile outside            ~/.ea-private/<file>
                     ~/.ea-private

How it decides what is resident space. Python: a path expression is
resident when it is built from `EA_DIR`, `ea_dir()`, a `.ea` path part,
`/home/ea-bot` or `EA_BOT_HOME`, directly or through module constants,
imports and functions that return such a path; it is owner-private when it
is built from `private_dir()`, `owner_log_dir()` or `.ea-private`, and
scratch when it comes from tempfile. A function's parameter that reaches a
file operation is followed to the calls that pass it, through any
function that hands it on. Shell: a word is resident when it names
`$EA_DIR`, `~/.ea`, `$HOME/.ea`, `/home/ea-bot`, `$EA_BOT_HOME`,
`$EA_BOT_SCRIPT_DIR` or a variable the script sets from one of them.

What it leaves out. The approved helpers themselves (owner_trust,
resident_io, atomic_io, ledger.guarded_connect, scripts/owner-io.sh) and
tests/. Code on the resident side of an owner check (the body of
`if not is_owner(): ...`, the `else` of `if _checked_token_io(): ...`, the
rest of a block after `if is_owner_process(): ... return`), since that code
never runs in an owner process; OWNER_PREDICATES and the lists beside it
name the checks it knows. The modules in RESIDENT_MODULES, which run only
as the resident, and the steps in TERMINAL_TOOLS, which are checked only
for TERMINAL_RULES. A shell command that runs as the resident
(`sudo -u ea-bot`, `runuser -u ea-bot`) or through owner-io.sh. It does
not follow a path held in an object attribute set in another module, and it
reads a shell heredoc or multi-line string only for unit and crontab lines
(inline Python there is not analysed).

Accepted exceptions live in tests/boundary_baseline.json: one entry per
file and rule, with a one-line reason and the source line of each accepted
hit. tests/test_boundary_lint.py fails on a hit that is not listed and on a
listed line that no hit matches, so the list only shrinks. The pre-commit
hook runs `--staged`.

Usage:
    boundary_lint.py              scan the repository; exit 1 on a hit not in the baseline
    boundary_lint.py --staged     scan the staged content of the staged files only
    boundary_lint.py --all        list every hit, the baselined ones too
    boundary_lint.py --json       print the hits as JSON
    boundary_lint.py FILE...      scan only these files (repository-relative)
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "boundary_baseline.json"

RULES = {
    "py-write": "a write into resident space by path",
    "py-rmw-read": "a plain read of a resident file that is written back",
    "py-trust-anchor": "a trust anchor (auth directory) used by path",
    "py-check-then-use": "an ownership check by path, then a use of that path",
    "py-env-file": "an env or secrets file read as text",
    "py-env-exec": "an env file loaded or run as code",
    "py-sqlite": "a database opened without the owner-side guard",
    "py-git": "git run in a resident-space tree",
    "py-resident-module": "a resident-only module reached from owner code",
    "sh-source": "an env file or resident-space file sourced as shell",
    "sh-io": "a shell read or write of a resident path",
    "sh-git": "git run in a resident-space tree",
    "cron-log": "a crontab line that sends its output into resident space",
    "unit-envfile": "a unit environment file outside ~/.ea-private",
}

# The approved helpers: their code is analysed for what their functions
# return, and never reported.
HELPER_FILES = frozenset({
    "preprocessor/owner_trust.py",
    "preprocessor/resident_io.py",
    "preprocessor/atomic_io.py",
    "scripts/owner-io.sh",
    "scripts/boundary_lint.py",
})
HELPER_FUNCTIONS = frozenset({("preprocessor/ledger.py", "guarded_connect")})

# Modules that run only as the resident (LLM tools, and what the bot runs
# through `sudo -u ea-bot`). The rules bind owner processes, so their file
# access is not reported; py-resident-module reports owner code that
# imports one, or runs one without switching to the resident.
RESIDENT_MODULES = frozenset({
    "preprocessor/doc_review.py",
    "preprocessor/poll_filler.py",
    "preprocessor/booking_link.py",
    "scripts/deck-bin/tailscale",
    "scripts/deck-bin/curl",
})

# Terminal tools: install, migration and diagnostic steps the owner or root
# runs by hand, most of them to create or repair resident space. The rules
# bind the unattended owner processes, so these are checked only for what
# they hand to those processes (unit environment files, crontab lines) and
# for what is never safe as the owner: running an env file as code, and git
# in the lanes' tree.
TERMINAL_TOOLS = frozenset({
    "scripts/install/setup.sh", "scripts/install/setup-ea-user.sh", "scripts/install/setup-ea-user-macos.sh", "scripts/install/user_init.py",
    "deploy/enable-mail-mediation.sh", "deploy/enable-deck-builder.sh",
    "deploy/harden-permissions.sh", "deploy/stage-proc-hidepid.sh",
    "scripts/zulip_switch.sh", "scripts/ea-doctor.py", "scripts/build_closure_corpus.py",
})
TERMINAL_RULES = frozenset({"unit-envfile", "cron-log", "sh-source", "sh-git", "py-env-exec",
                            "py-git"})

SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".claude",
                       ".pytest_cache", ".mypy_cache"})


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    rule: str
    code: str
    message: str
    helper: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.rule, self.code)

    def render(self) -> str:
        return f"{self.file}:{self.line}: {self.rule}: {self.message}; use {self.helper}"


def _norm(text: str) -> str:
    return " ".join(text.split())


# ══ Python ═════════════════════════════════════════════════════════════
#
# Tags on a value: "R" resident space, "P" owner-private, "T" a scratch
# directory of this process (tempfile), and the kind of file it names: "A"
# the auth directory, "S" a secrets file, "E" the resident's .env. A path
# takes its location (R, P or T) from its root, the first part that has
# one, and the kind tags from every part.

LOC = frozenset({"R", "P", "T"})
NONE: frozenset = frozenset()

# The owner checks the code branches on. An owner predicate is true in every
# process the rules bind (its else branch is the resident's or a scratch
# tree's); a resident predicate is true only outside them; a guard call
# raises in an owner process, so the rest of its block is the resident's.
OWNER_PREDICATES = frozenset({
    "is_owner_process", "is_owner", "_is_owner", "_checked_token_io", "_owner",
    "_via_child", "_default_owner_check", "_in_resident_space", "_resident_space",
})
RESIDENT_PREDICATES = frozenset({
    "should_mediate", "_is_resident", "mediation_active_for_resident", "_plain_key_read",
    "_plain_token_io",
})
GUARD_CALLS = frozenset({"_refuse_owner"})
TEMP_FUNCS = frozenset({"mkdtemp", "mkstemp", "gettempdir", "TemporaryDirectory",
                        "NamedTemporaryFile"})
RESIDENT_NAMES = frozenset({"EA_DIR", "EA_BOT_HOME"})
PATH_METHODS = frozenset({
    "resolve", "expanduser", "absolute", "with_suffix", "with_name", "with_stem",
    "joinpath", "glob", "rglob", "iterdir", "relative_to",
})
PATH_FUNCS = frozenset({
    ("os.path", "join"), ("os.path", "dirname"), ("os.path", "realpath"),
    ("os.path", "abspath"), ("os.path", "expanduser"), ("os.path", "normpath"),
    ("os", "fspath"), ("os", "listdir"), ("os", "scandir"), ("glob", "glob"),
    ("glob", "iglob"), ("", "Path"), ("pathlib", "Path"), ("", "PurePath"),
    ("", "str"), ("", "sorted"), ("", "list"), ("", "reversed"), ("", "tuple"),
})
# Library modules whose calls are read as file operations or path builders.
KNOWN_MODULES = frozenset({"os", "os.path", "shutil", "tempfile", "sqlite3", "io", "codecs",
                           "gzip", "bz2", "lzma", "subprocess", "runpy", "glob", "pathlib",
                           "importlib.util", "dotenv", "json"})

_RESIDENT_STR = re.compile(r"(?:^|/)\.ea(?:/|$)|^~/\.ea(?:/|$)|/home/ea-bot(?:/|$)")
_SHELL_SOURCE = re.compile(
    r"(?:^|[\s;&|(])(?:source|\.)\s+[\"']?"
    r"(?:\$\{?EA_DIR\b|~/\.ea/|\$\{?HOME\}?/\.ea/|/home/ea-bot/|\S*\.env\b)")


def _str_tags(s: str) -> frozenset:
    tags = set()
    if ".ea-private" in s or s == "EA_PRIVATE_DIR":
        tags.add("P")
    elif _RESIDENT_STR.search(s) or s == "EA_DIR":
        tags.add("R")
    if s == "auth" or s.startswith("auth/") or re.search(r"\.ea/auth(?:/|$)", s):
        tags.add("A")
    if s in (".env.secrets", "secrets.env") or s.endswith(("/.env.secrets", "/secrets.env")):
        tags.add("S")
    if s == ".env" or s.endswith("/.ea/.env"):
        tags.add("E")
    return frozenset(tags)


def _root(*parts: frozenset) -> frozenset:
    """Location from the first part that has one; kinds from every part."""
    loc: frozenset = NONE
    kinds: set = set()
    for p in parts:
        if not loc and p & LOC:
            loc = p & LOC
        kinds |= p - LOC
    return loc | frozenset(kinds)


def _either(*parts: frozenset) -> frozenset:
    """Every tag of every alternative (a value that may be any of them)."""
    out: set = set()
    for p in parts:
        out |= p
    return frozenset(out)


def is_resident(tags: frozenset) -> bool:
    return "R" in tags


def _callee(node: ast.AST) -> str:
    """The last name of a call target: `a.b.c()` -> "c"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _predicate(n, aliases=None) -> str | None:
    """"owner", "resident" or None for one expression: a predicate call,
    `bool(...)` of one, or a local name that holds one (`aliases`)."""
    if isinstance(n, ast.Call) and _callee(n.func) == "bool" and len(n.args) == 1:
        n = n.args[0]
    if isinstance(n, ast.Call):
        name = _callee(n.func)
        if name in OWNER_PREDICATES:
            return "owner"
        if name in RESIDENT_PREDICATES:
            return "resident"
    if isinstance(n, ast.Name) and aliases:
        return aliases.get(n.id)
    return None


def predicate_aliases(assignments: list) -> dict[str, str]:
    """Local names that only ever hold an owner check's answer (`owner =
    _rio.is_owner()`, a True/False fallback allowed), with its polarity."""
    seen: dict[str, set] = {}
    for kind, target, value in assignments:
        if kind != "name" or not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, bool):
            pol = "const"
        else:
            pol = _predicate(value) or "other"
        seen.setdefault(target.id, set()).add(pol)
    out = {}
    for name, pols in seen.items():
        pols.discard("const")
        if len(pols) == 1 and next(iter(pols)) in ("owner", "resident"):
            out[name] = next(iter(pols))
    return out


def owner_polarity(test: ast.AST, aliases=None):
    """"owner" when `test` holds only in an owner process (`pred()` or
    `pred() and ...`), "resident" when only outside one (`not pred()`),
    None otherwise; the second value is True when the else branch is the
    exact opposite (a bare predicate or its negation)."""
    def pred(n):
        return _predicate(n, aliases)
    flip = {"owner": "resident", "resident": "owner"}
    p = pred(test)
    if p:
        return p, True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and pred(test.operand):
        return flip[pred(test.operand)], True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        if any(pred(v) == "owner" for v in test.values):
            return "owner", False
        if any(pred(v) == "resident" for v in test.values):
            return "resident", False
    return None, False


def _terminates(stmts: list) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break))


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class PyModule:
    """One Python file: its imports, and the tags of its module-level names,
    functions (what they return in an owner process) and attributes."""

    def __init__(self, rel: str, source: str):
        self.rel = rel
        self.source = source
        self.lines = source.splitlines()
        self.tree = ast.parse(source, filename=rel)
        self.keys = _module_keys(rel)
        self.package = self.keys[0].rsplit(".", 1)[0] if "." in self.keys[0] else ""
        if rel.endswith("__init__.py"):
            self.package = self.keys[0]
        self.aliases: dict[str, str] = {}             # local name -> module key
        self.stdlib: dict[str, str] = {}              # local name -> a KNOWN_MODULES module
        self.symbols: dict[str, tuple[str, str]] = {}  # local name -> (module key, name)
        self.imports: list[tuple[ast.AST, str]] = []   # (import node, module key)
        self.imported: dict[str, str] = {}             # local name -> dotted name
        self.imported_from: dict[str, str] = {}        # local symbol -> its module's name
        self.modnames: set[str] = set()                # local names bound to a module
        self.env: dict[str, frozenset] = {}
        self.funcs: dict[str, frozenset] = {}
        self.attrs: dict[str, frozenset] = {}

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""


def _module_keys(rel: str) -> list[str]:
    p = rel[:-3] if rel.endswith(".py") else rel
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    if p.startswith("preprocessor/"):
        inner = p[len("preprocessor/"):].replace("/", ".")
        return [inner, "preprocessor." + inner]
    return [p.replace("/", ".")]


class PyProject:
    """Every Python file in the scan, with the tags resolved across them."""

    def __init__(self, sources: dict[str, str]):
        self.modules: dict[str, PyModule] = {}
        self.errors: list[str] = []
        self.assign_cache: dict[int, list] = {}
        self.return_cache: dict[int, list] = {}
        for rel, src in sorted(sources.items()):
            try:
                self.modules[rel] = PyModule(rel, src)
            except SyntaxError as e:
                self.errors.append(f"{rel}: not parsed ({e.msg}, line {e.lineno})")
        self.by_key: dict[str, PyModule] = {}
        for m in self.modules.values():
            for k in m.keys:
                self.by_key.setdefault(k, m)
        for m in self.modules.values():
            self._imports(m)
        self.global_funcs: dict[str, frozenset] = {}
        self._resolve()

    # ── imports ──────────────────────────────────────────────────────
    def _find(self, name: str, pkg: str = "") -> str | None:
        for cand in ((pkg + "." + name) if pkg else None, name):
            if cand and cand in self.by_key:
                return cand
        return None

    def _imports(self, m: PyModule) -> None:
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    target = a.name if a.asname else a.name.split(".")[0]
                    m.imported[local] = target
                    m.modnames.add(local)
                    if a.name in KNOWN_MODULES:
                        m.stdlib[local] = a.name if a.asname else target
                    key = self._find(target)
                    if key:
                        m.aliases[local] = key
                    full = self._find(a.name)
                    if full:
                        m.imports.append((node, full))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = m.package
                    for _ in range(node.level - 1):
                        pkg = pkg.rsplit(".", 1)[0] if "." in pkg else ""
                    base = f"{pkg}.{base}".strip(".") if base else pkg
                for a in node.names:
                    local = a.asname or a.name
                    m.imported[local] = f"{base}.{a.name}" if base else a.name
                    m.imported_from[local] = base
                    if base in ("", "preprocessor", m.package) and a.name in HELPER_MODULE_STEMS:
                        m.modnames.add(local)    # a helper module, in the scan or not
                    if base in KNOWN_MODULES:
                        m.symbols[local] = ("<lib>" + base, a.name)
                        continue
                    sub = self._find(f"{base}.{a.name}" if base else a.name)
                    if sub and (not base or f"{base}.{a.name}" in self.by_key
                                or base == "preprocessor"):
                        m.aliases[local] = sub
                        m.modnames.add(local)
                        m.imports.append((node, sub))
                        continue
                    key = self._find(base) if base else None
                    if key:
                        m.symbols[local] = (key, a.name)
                        m.imports.append((node, key))

    # ── the fixpoint over every module ───────────────────────────────
    def _resolve(self) -> None:
        for _ in range(8):
            changed = False
            for m in self.modules.values():
                changed |= self._module_pass(m)
            gf: dict[str, frozenset] = {}
            for m in self.modules.values():
                for name, tags in m.funcs.items():
                    gf[name] = _either(gf.get(name, NONE), tags)
            if gf != self.global_funcs:
                self.global_funcs = gf
                changed = True
            if not changed:
                break

    def _module_pass(self, m: PyModule) -> bool:
        before = (dict(m.env), dict(m.funcs), dict(m.attrs))
        scope = Scope(self, m, None)
        for _ in range(2):
            _collect_assignments(m.tree.body, scope)
        for fn in _functions(m.tree.body):
            m.funcs[fn.name] = _either(m.funcs.get(fn.name, NONE), self.returns(m, fn, scope))
        return (m.env, m.funcs, m.attrs) != before

    def returns(self, m: PyModule, fn, outer: "Scope") -> frozenset:
        """What `fn` returns in an owner process: its return values outside
        the resident side of an owner check."""
        values = _owner_returns(fn, self)
        if not values:
            return NONE
        scope = function_scope(self, m, fn, outer)
        return _either(*(scope.tags(v) for v in values))


class Scope:
    """Name tags visible at one point: a function's locals over its
    enclosing scopes, over the module's names."""

    def __init__(self, project: PyProject, module: PyModule, parent: "Scope | None"):
        self.project = project
        self.module = module
        self.parent = parent
        self.local: dict[str, frozenset] = {} if parent is not None else module.env

    def lookup(self, name: str) -> frozenset:
        found = self._lookup(name)
        if name in RESIDENT_NAMES and not found & LOC:
            return found | {"R"}        # EA_DIR names resident space however it is set
        return found

    def _lookup(self, name: str) -> frozenset:
        s = self
        while s is not None:
            if name in s.local:
                return s.local[name]
            s = s.parent
        m = self.module
        if name in m.symbols:
            key, sym = m.symbols[name]
            other = self.project.by_key.get(key)
            if other is not None:
                return other.env.get(sym, NONE)
        return NONE

    def func_tags(self, name: str) -> frozenset | None:
        m = self.module
        if name in m.funcs:
            return m.funcs[name]
        if name in m.symbols:
            key, sym = m.symbols[name]
            other = self.project.by_key.get(key)
            if other is not None and sym in other.funcs:
                return other.funcs[sym]
        return None

    def assign(self, name: str, tags: frozenset) -> None:
        self.local[name] = _either(self.local.get(name, NONE), tags)

    # ── the tags of an expression ────────────────────────────────────
    def tags(self, node) -> frozenset:
        if node is None:
            return NONE
        if isinstance(node, ast.Constant):
            return _str_tags(node.value) if isinstance(node.value, str) else NONE
        if isinstance(node, ast.Name):
            return self.lookup(node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in self.module.aliases:
                other = self.project.by_key.get(self.module.aliases[node.value.id])
                if other is not None:
                    return other.env.get(node.attr, NONE)
            if node.attr in ("parent", "parents", "name", "stem", "suffix"):
                base = self.tags(node.value)
                if node.attr in ("parent", "parents") or (node.attr == "name" and "T" in base):
                    return base          # a temp file object's .name is its full path
                return base - LOC
            if node.attr in RESIDENT_NAMES:
                return frozenset({"R"})
            return self.module.attrs.get(node.attr, NONE)
        if isinstance(node, ast.BinOp):
            return _root(self.tags(node.left), self.tags(node.right))
        if isinstance(node, ast.JoinedStr):
            return _root(*(self.tags(v.value) if isinstance(v, ast.FormattedValue)
                           else self.tags(v) for v in node.values))
        if isinstance(node, ast.IfExp):
            pol, exact = owner_polarity(node.test)
            if pol == "owner" and exact:
                return self.tags(node.body)
            if pol == "resident" and exact:
                return self.tags(node.orelse)
            return _either(self.tags(node.body), self.tags(node.orelse))
        if isinstance(node, ast.BoolOp):
            return _either(*(self.tags(v) for v in node.values))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return _either(*(self.tags(e) for e in node.elts))
        if isinstance(node, ast.Dict):
            return _either(*(self.tags(v) for v in node.values if v is not None))
        if isinstance(node, ast.Subscript):
            # os.environ["EA_DIR"] names resident space by its key
            key = self.tags(node.slice) if _const_str(node.slice) is not None else NONE
            return _root(self.tags(node.value), key)
        if isinstance(node, ast.Starred):
            return self.tags(node.value)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            return _either(self.tags(node.elt), *(self.tags(g.iter) for g in node.generators))
        if isinstance(node, ast.Call):
            return self._call_tags(node)
        return NONE

    def _call_tags(self, node: ast.Call) -> frozenset:
        f = node.func
        name = _callee(f)
        args = [self.tags(a) for a in node.args]
        if isinstance(f, ast.Attribute):
            mod = self._stdlib_of(f.value)
            if mod is not None:
                if (mod, f.attr) in PATH_FUNCS or (mod, f.attr) in (("os", "getenv"),
                                                                    ("os.environ", "get")):
                    return _root(*args)
                if mod == "tempfile" and f.attr in TEMP_FUNCS:
                    return frozenset({"T"})
                return NONE
            if isinstance(f.value, ast.Name) and f.value.id in self.module.aliases:
                other = self.project.by_key.get(self.module.aliases[f.value.id])
                if other is not None and f.attr in other.funcs:
                    return other.funcs[f.attr]
            if f.attr in PATH_METHODS:
                return _root(self.tags(f.value), *args)
            if f.attr == "get":
                return self.tags(f.value)
            if f.attr == "home" and isinstance(f.value, ast.Name) and f.value.id == "Path":
                return NONE
            return self.project.global_funcs.get(f.attr, NONE)
        if isinstance(f, ast.Name):
            if ("", name) in PATH_FUNCS:
                return _root(*args)
            if name in self.module.symbols and self.module.symbols[name][0].startswith("<lib>"):
                mod, sym = self.module.symbols[name]
                mod = mod[len("<lib>"):]
                if mod == "tempfile" and sym in TEMP_FUNCS:
                    return frozenset({"T"})
                return _root(*args) if (mod, sym) in PATH_FUNCS or sym == "Path" else NONE
            ft = self.func_tags(name)
            if ft is not None:
                return ft
        return NONE

    def _stdlib_of(self, node) -> str | None:
        """The KNOWN_MODULES module an expression names (`os`, `os.path`,
        an alias of one)."""
        if isinstance(node, ast.Name):
            if node.id in self.module.stdlib:
                return self.module.stdlib[node.id]
            return node.id if node.id in KNOWN_MODULES else None
        if isinstance(node, ast.Attribute):
            base = self._stdlib_of(node.value)
            if base == "os" and node.attr in ("path", "environ"):
                return "os." + node.attr
        return None


def _functions(body: list):
    """Module-level functions, including those inside top-level if/try."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            for sub in _sub_blocks(node):
                yield from _functions(sub)


def _sub_blocks(node) -> list:
    """The statement lists directly inside a compound statement."""
    out = []
    for name in ("body", "orelse", "finalbody"):
        v = getattr(node, name, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            out.append(v)
    for h in getattr(node, "handlers", []) or []:
        out.append(h.body)
    for c in getattr(node, "cases", []) or []:
        out.append(c.body)
    return out


def visit_blocks(stmts: list, resident: bool, fn, aliases=None) -> None:
    """Call fn(stmt, resident) for every statement, nested ones included
    (not into nested function or class bodies), where `resident` is True on
    the resident side of an owner check. `aliases` names the locals that
    hold an owner check's answer."""
    rest_resident = resident
    for st in stmts:
        cur = rest_resident
        fn(st, cur)
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and _callee(st.value.func) in GUARD_CALLS):
            rest_resident = True
            continue
        if isinstance(st, ast.If):
            pol, exact = owner_polarity(st.test, aliases)
            if pol == "owner":
                visit_blocks(st.body, cur, fn, aliases)
                visit_blocks(st.orelse, cur or exact, fn, aliases)
                if exact and _terminates(st.body) and not st.orelse:
                    rest_resident = True
            elif pol == "resident":
                visit_blocks(st.body, True, fn, aliases)
                visit_blocks(st.orelse, cur, fn, aliases)
            else:
                visit_blocks(st.body, cur, fn, aliases)
                visit_blocks(st.orelse, cur, fn, aliases)
            continue
        for sub in _sub_blocks(st):
            visit_blocks(sub, cur, fn, aliases)


def _targets(t) -> list:
    if isinstance(t, (ast.Tuple, ast.List)):
        out = []
        for e in t.elts:
            out.extend(_targets(e))
        return out
    return [t]


_SCRATCH = frozenset({"T"})


def _assignments(body: list, cache: dict) -> list:
    """The assignments in `body`, found once per project and kept in
    `cache` (keyed by the body, which the project keeps alive): (kind,
    target, value) with kind "name" (a Name, Attribute or Starred target
    given the value's tags), "attr" (a class-level name, an attribute) or
    "const" (a target given fixed tags). Nested function bodies are not
    entered."""
    key = id(body)
    if key in cache:
        return cache[key]
    out: list = []

    def visit(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(st, ast.ClassDef):
                for sub in st.body:
                    if isinstance(sub, (ast.Assign, ast.AnnAssign)) and getattr(sub, "value", None):
                        for t in (sub.targets if isinstance(sub, ast.Assign) else [sub.target]):
                            for n in _targets(t):
                                if isinstance(n, ast.Name):
                                    out.append(("attr", n.id, sub.value))
                continue
            if isinstance(st, ast.Assign):
                if (len(st.targets) == 1 and isinstance(st.targets[0], (ast.Tuple, ast.List))
                        and isinstance(st.value, (ast.Tuple, ast.List))
                        and len(st.targets[0].elts) == len(st.value.elts)):
                    for t, v in zip(st.targets[0].elts, st.value.elts):
                        out.append(("name", t, v))
                else:
                    for t in st.targets:
                        for n in _targets(t):
                            out.append(("name", n, st.value))
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                out.append(("name", st.target, st.value))
            elif isinstance(st, ast.AugAssign):
                out.append(("name", st.target, st.value))
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                for n in _targets(st.target):
                    out.append(("name", n, st.iter))
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    if item.optional_vars is None:
                        continue
                    # a temp directory or file is this process's scratch; any
                    # other context value is a file object or a descriptor
                    ctx = item.context_expr
                    tags = (_SCRATCH if isinstance(ctx, ast.Call)
                            and _callee(ctx.func) in TEMP_FUNCS else NONE)
                    for n in _targets(item.optional_vars):
                        out.append(("const", n, tags))
            for node in _own_nodes(st):
                if isinstance(node, ast.NamedExpr):
                    out.append(("name", node.target, node.value))
            for sub in _sub_blocks(st):
                visit(sub)

    visit(body)
    cache[key] = out
    return out


def _collect_assignments(body: list, scope: Scope) -> None:
    """Record the tags of every name (and attribute) assigned in `body`."""
    m = scope.module

    def one(target, tags):
        if isinstance(target, ast.Name):
            scope.assign(target.id, tags)
        elif isinstance(target, ast.Attribute):
            m.attrs[target.attr] = _either(m.attrs.get(target.attr, NONE), tags)
        elif isinstance(target, ast.Starred):
            one(target.value, tags)

    for kind, target, value in _assignments(body, scope.project.assign_cache):
        if kind == "attr":
            m.attrs[target] = _either(m.attrs.get(target, NONE), scope.tags(value))
        elif kind == "const":
            one(target, value)
        else:
            one(target, scope.tags(value))


def _owner_returns(fn, project: "PyProject") -> list:
    """The return values of `fn` outside the resident side of an owner
    check, found once per project and kept."""
    key = id(fn)
    cache = project.return_cache
    if key not in cache:
        found: list = []

        def on(st, resident):
            if isinstance(st, ast.Return) and st.value is not None and not resident:
                found.append(st.value)
        aliases = predicate_aliases(_assignments(fn.body, project.assign_cache))
        visit_blocks(fn.body, False, on, aliases)
        cache[key] = found
    return cache[key]


def function_scope(project: PyProject, m: PyModule, fn, outer: Scope) -> Scope:
    scope = Scope(project, m, outer)
    a = fn.args
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(pos, defaults):
        scope.local[arg.arg] = scope.tags(d) if d is not None else NONE
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        scope.local[arg.arg] = scope.tags(d) if d is not None else NONE
    for arg in (a.vararg, a.kwarg):
        if arg is not None:
            scope.local[arg.arg] = NONE
    for _ in range(3):
        _collect_assignments(fn.body, scope)
    return scope


# ── Python sinks ─────────────────────────────────────────────────────

WRITE_HELP = {
    "replace": "resident_io.replace_text / replace_bytes, or atomic_io.write_text / write_json",
    "append": "resident_io.append_text (or an owner-private log, log_paths.owner_log)",
    "rmw": "resident_io.update_text",
    "create": "resident_io.ensure_file",
    "mkdir": "resident_io.make_dir",
    "move": "resident_io.move",
    "copy": "resident_io.read_bytes, then resident_io.replace_bytes",
    "chmod": "the mode argument of resident_io.replace_text / make_dir",
    "link": "no link: an owner process never makes a link in resident space",
    "temp": "atomic_io.write_text, or resident_io.replace_text",
    "write": "resident_io.replace_text, or atomic_io.write_text",
}
ANCHOR_HELP = {
    "read": "owner_trust.read_owner_file",
    "chmod": "owner_trust.set_owner_file_mode",
    "mkdir": "owner_trust.check_owner_dir / open_owner_dir",
}
_O_WRITE = re.compile(r"\bO_(?:WRONLY|RDWR|CREAT|APPEND|TRUNC)\b")


@dataclass
class Sink:
    node: ast.AST
    kind: str          # "read", "write", "sqlite", "git", "env-exec"
    op: str            # the write operation, a WRITE_HELP key
    paths: list = field(default_factory=list)   # path expressions


def _mode_of(call: ast.Call, pos: int) -> str | None:
    mode = call.args[pos] if len(call.args) > pos else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return "r"
    return _const_str(mode)


def _open_op(mode: str | None) -> str | None:
    """None for a read, else the write op of an open() mode."""
    if mode is None:
        return "write"
    if "+" in mode:
        return "rmw"
    if "a" in mode:
        return "append"
    if "w" in mode:
        return "replace"
    if "x" in mode:
        return "create"
    return None


def _kw(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def sinks_of(call: ast.Call, scope: Scope) -> list[Sink]:
    """The file operations one call makes, with their path expressions."""
    f = call.func
    name = _callee(f)
    args = call.args
    out: list[Sink] = []
    stdmod = scope._stdlib_of(f.value) if isinstance(f, ast.Attribute) else None
    if isinstance(f, ast.Name) and f.id in scope.module.symbols:
        mod, sym = scope.module.symbols[f.id]
        if mod.startswith("<lib>"):
            stdmod, name = mod[len("<lib>"):], sym

    if _kw(call, "dir_fd") is not None or _kw(call, "src_dir_fd") is not None:
        return out     # relative to a checked descriptor: the helper pattern

    # builtin open, and the open() of io, codecs and the compressors
    if (isinstance(f, ast.Name) and f.id == "open") or (
            stdmod in ("io", "codecs", "gzip", "bz2", "lzma") and name == "open"):
        path = args[0] if args else _kw(call, "file")
        op = _open_op(_mode_of(call, 1))
        out.append(Sink(call, "write" if op else "read", op or "", [path]))
        return out
    if stdmod == "os":
        if name == "open" and args:
            flags = ast.unparse(args[1]) if len(args) > 1 else ""
            if _O_WRITE.search(flags):
                op = "append" if "O_APPEND" in flags else "rmw" if "O_RDWR" in flags else "replace"
                if "O_CREAT" in flags and "O_EXCL" in flags:
                    op = "create"
                out.append(Sink(call, "write", op, [args[0]]))
            else:
                out.append(Sink(call, "read", "", [args[0]]))
        elif name in ("makedirs", "mkdir") and args:
            out.append(Sink(call, "write", "mkdir", [args[0]]))
        elif name in ("rename", "replace", "renames") and args:
            out.append(Sink(call, "write", "move", list(args[:2])))
        elif name in ("symlink", "link") and len(args) > 1:
            out.append(Sink(call, "write", "link", [args[1]]))
        elif name in ("chmod", "chown", "lchown", "truncate") and args:
            out.append(Sink(call, "write", "chmod" if name != "truncate" else "replace", [args[0]]))
        return out
    if stdmod == "shutil" and args:
        if name == "move":
            out.append(Sink(call, "write", "move", list(args[:2])))
        elif name in ("copy", "copy2", "copyfile", "copytree", "copymode", "copystat"):
            if len(args) > 1:
                out.append(Sink(call, "write", "copy", [args[1]]))
            out.append(Sink(call, "read", "", [args[0]]))
        return out
    if stdmod == "tempfile" and name in ("mkstemp", "NamedTemporaryFile", "mkdtemp",
                                         "TemporaryDirectory"):
        d = _kw(call, "dir")
        if d is not None:
            out.append(Sink(call, "write", "temp", [d]))
        return out
    if stdmod == "sqlite3" and name == "connect":
        out.append(Sink(call, "sqlite", "", [args[0] if args else _kw(call, "database")]))
        return out
    if stdmod == "runpy" and name == "run_path" and args:
        out.append(Sink(call, "env-exec", "code", [args[0]]))
        return out
    if name == "spec_from_file_location" and len(args) > 1:
        out.append(Sink(call, "env-exec", "code", [args[1]]))
        return out
    if name in ("load_dotenv", "dotenv_values"):
        out.append(Sink(call, "env-exec", "dotenv", list(args[:1])))
        return out
    if isinstance(f, ast.Name) and f.id in ("exec", "eval") and args:
        text = ast.unparse(args[0])
        if re.search(r"\bread(?:_text)?\(|\bopen\(", text):
            out.append(Sink(call, "env-exec", "exec", []))
        return out
    if stdmod == "subprocess" or name in ("run", "Popen", "check_output", "check_call", "call"):
        cmd = args[0] if args else _kw(call, "args")
        if isinstance(cmd, (ast.List, ast.Tuple)) and cmd.elts and _const_str(cmd.elts[0]) == "git":
            paths = []
            for i, e in enumerate(cmd.elts[:-1]):
                if _const_str(e) == "-C":
                    paths.append(cmd.elts[i + 1])
            cwd = _kw(call, "cwd")
            if cwd is not None:
                paths.append(cwd)
            if paths:
                out.append(Sink(call, "git", "", paths))
        if stdmod == "subprocess" or name in ("run", "Popen"):
            return out
    # Path methods: the receiver is the path (not a module: atomic_io.write_text
    # and the like are the helpers)
    if (isinstance(f, ast.Attribute) and stdmod is None
            and not (isinstance(f.value, ast.Name) and f.value.id in scope.module.modnames)):
        recv = f.value
        if name in ("write_text", "write_bytes"):
            out.append(Sink(call, "write", "replace", [recv]))
        elif name == "touch":
            out.append(Sink(call, "write", "create", [recv]))
        elif name == "mkdir":
            out.append(Sink(call, "write", "mkdir", [recv]))
        elif name in ("rename", "replace") and len(args) == 1 and not call.keywords:
            out.append(Sink(call, "write", "move", [recv, args[0]]))
        elif name in ("symlink_to", "hardlink_to"):
            out.append(Sink(call, "write", "link", [recv]))
        elif name in ("chmod", "lchmod") and args:
            out.append(Sink(call, "write", "chmod", [recv]))
        elif name in ("read_text", "read_bytes"):
            out.append(Sink(call, "read", "", [recv]))
        elif name == "open":
            op = _open_op(_mode_of(call, 0))              # Path.open(mode)
            out.append(Sink(call, "write" if op else "read", op or "", [recv]))
    return out


HELPER_WRITE_CALLS = frozenset({"write_text", "write_json", "replace_text", "replace_bytes",
                                "write_owner_file"})
HELPER_MODULE_STEMS = ("atomic_io", "resident_io", "owner_trust")
CHECK_CALLS = frozenset({"check_owner_file", "check_owner_dir"})


def _path_key(node) -> str:
    """A path expression's text without str()/Path()/os.fspath() around it."""
    while (isinstance(node, ast.Call) and _callee(node.func) in ("str", "Path", "fspath")
           and len(node.args) == 1):
        node = node.args[0]
    return ast.unparse(node) if node is not None else ""


def _helper_receiver(call: ast.Call, scope: Scope) -> bool:
    """True when `call` is a function of an approved helper module (named
    by its import, whether or not the module is in the scan)."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        name = scope.module.imported.get(f.value.id, "")
    elif isinstance(f, ast.Name):
        name = scope.module.imported_from.get(f.id, "")
    else:
        return False
    return name.rsplit(".", 1)[-1] in HELPER_MODULE_STEMS


class PyChecker:
    """Walks every function (and the module body) of one module for sinks."""

    def __init__(self, project: PyProject, m: PyModule):
        self.project = project
        self.m = m
        self.hits: list[Hit] = []
        self.module_names = set(m.env) | set(m.funcs) | set(m.symbols) | set(m.aliases)
        self.reads: list[tuple[str, bool, ast.AST, str]] = []   # (key, module-level key, node, where)
        self.writes: set[tuple[str, str]] = set()               # (where, key)
        # parameter flow: which parameters of which function reach a file
        # operation, and the calls that may pass them a path
        self.params_of: dict[str, list[str]] = {}
        self.param_sinks: dict[str, dict[str, tuple[str, str]]] = {}
        self.calls: list[tuple[ast.Call, Scope, object]] = []
        self._fn = None

    # ── parameter flow ───────────────────────────────────────────────
    def _callee_of(self, call: ast.Call, checkers: dict) -> tuple["PyChecker", str, bool] | None:
        """(the checker of the module that defines the called function, its
        name, whether the call is a method call on self or cls)."""
        f = call.func
        m = self.m
        if isinstance(f, ast.Name):
            if f.id in self.params_of:
                return self, f.id, False
            if f.id in m.symbols:
                key, sym = m.symbols[f.id]
                other = self.project.by_key.get(key)
                c = checkers.get(other.rel) if other is not None else None
                if c is not None and sym in c.params_of:
                    return c, sym, False
            return None
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id in ("self", "cls") and f.attr in self.params_of:
                return self, f.attr, True
            if f.value.id in m.aliases:
                other = self.project.by_key.get(m.aliases[f.value.id])
                c = checkers.get(other.rel) if other is not None else None
                if c is not None and f.attr in c.params_of:
                    return c, f.attr, False
        return None

    def _bound_args(self, call: ast.Call, params: list[str], method: bool):
        names = params[1:] if method else params
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Starred):
                break
            if i < len(names):
                yield names[i], a
        for kw in call.keywords:
            if kw.arg:
                yield kw.arg, kw.value

    def propagate(self, checkers: dict) -> bool:
        """A call that hands one of this function's own parameters to a
        parameter that reaches a file operation makes that parameter reach
        it too. True when something was added."""
        changed = False
        for call, _scope, fn in self.calls:
            if fn is None:
                continue
            target = self._callee_of(call, checkers)
            if target is None:
                continue
            other, name, method = target
            sinks = other.param_sinks.get(name)
            if not sinks:
                continue
            own = self.params_of.get(fn.name, ())
            for pname, arg in self._bound_args(call, other.params_of[name], method):
                if pname in sinks and isinstance(arg, ast.Name) and arg.id in own:
                    mine = self.param_sinks.setdefault(fn.name, {})
                    if arg.id not in mine:
                        mine[arg.id] = sinks[pname]
                        changed = True
        return changed

    def finish(self, checkers: dict) -> list[Hit]:
        """Hits at the calls that pass a resident path, an auth-directory
        file or a secrets file to a parameter that reaches a file operation."""
        for call, scope, _fn in self.calls:
            target = self._callee_of(call, checkers)
            if target is None:
                continue
            other, name, method = target
            sinks = other.param_sinks.get(name)
            if not sinks:
                continue
            for pname, arg in self._bound_args(call, other.params_of[name], method):
                if pname not in sinks:
                    continue
                kind, op = sinks[pname]
                t = scope.tags(arg)
                if "A" in t and kind in ("read", "write"):
                    self.hit(call, "py-trust-anchor",
                             f"passes a file in the auth directory to {name}(), which uses it by path",
                             "owner_trust.read_owner_file / write_owner_file")
                elif kind == "read" and t & {"S", "E"}:
                    self.hit(call, "py-env-file",
                             f"passes an env or secrets file to {name}(), which reads it as text",
                             "owner_trust.owner_secret / read_env_values")
                elif kind == "write" and is_resident(t):
                    self.hit(call, "py-write",
                             f"passes a resident path to {name}(), which writes it by name",
                             WRITE_HELP.get(op, WRITE_HELP["write"]))
                elif kind == "sqlite" and is_resident(t):
                    self.hit(call, "py-sqlite",
                             f"passes a resident database to {name}(), which opens it with "
                             f"plain sqlite3.connect", "ledger.guarded_connect")
                else:
                    continue
                break
        return self.hits

    def hit(self, node, rule, message, helper):
        line = getattr(node, "lineno", 0)
        self.hits.append(Hit(self.m.rel, line, rule, _norm(self.m.line(line)), message, helper))

    def run(self) -> list[Hit]:
        top = Scope(self.project, self.m, None)
        self._fn = None
        self._scan_body(self.m.tree.body, top, "<module>")
        for fn, scope, where in self._all_functions():
            if (self.m.rel, fn.name) in HELPER_FUNCTIONS:
                continue
            self._fn = fn
            a = fn.args
            self.params_of[fn.name] = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
            self._scan_body(fn.body, scope, where)
        self._fn = None
        # a plain read of a resident file that is written back
        for key, mod_level, node, where in self.reads:
            if (where, key) in self.writes or (mod_level and any(k == key for _, k in self.writes)):
                self.hit(node, "py-rmw-read",
                         "reads a resident file by path and the same code writes it back",
                         "resident_io.update_text (or resident_io.read_text and a shape check)")
        return self.hits

    def _all_functions(self):
        top = Scope(self.project, self.m, None)

        def walk(body, scope, prefix):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    s = function_scope(self.project, self.m, node, scope)
                    where = prefix + node.name
                    yield node, s, where
                    yield from walk(node.body, s, where + ".")
                elif isinstance(node, ast.ClassDef):
                    yield from walk(node.body, scope, prefix + node.name + ".")
                else:
                    for sub in _sub_blocks(node):
                        yield from walk(sub, scope, prefix)
        yield from walk(self.m.tree.body, top, "")

    def _mod_level_key(self, key: str) -> bool:
        try:
            names = {n.id for n in ast.walk(ast.parse(key, mode="eval")) if isinstance(n, ast.Name)}
        except SyntaxError:
            return False
        return bool(names) and names <= self.module_names | {"str", "Path", "os"}

    def _scan_body(self, body, scope: Scope, where: str):
        checked: dict[str, ast.AST] = {}
        uses: list[tuple[str, ast.AST]] = []

        def on_stmt(st, resident):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            for node in _own_nodes(st):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _SHELL_SOURCE.search(node.value) and not resident:
                        self.hit(node, "py-env-exec",
                                 "a command string sources an env file as shell",
                                 "scripts/owner-io.sh env (parse KEY=VALUE as data)")
                    continue
                if not isinstance(node, ast.Call):
                    continue
                name = _callee(node.func)
                if name in CHECK_CALLS and node.args:
                    checked.setdefault(_path_key(node.args[0]), node)
                if name in CHECK_CALLS and isinstance(node.func, ast.Attribute) and not node.args:
                    checked.setdefault(_path_key(node.func.value), node)
                if name in HELPER_WRITE_CALLS and _helper_receiver(node, scope) and node.args:
                    self.writes.add((where, _path_key(node.args[0])))
                if resident:
                    continue
                found = sinks_of(node, scope)
                for s in found:
                    self._sink(s, scope, where, uses)
                if not found and (node.args or node.keywords):
                    self.calls.append((node, scope, self._fn))

        aliases = predicate_aliases(_assignments(body, self.project.assign_cache))
        visit_blocks(body, False, on_stmt, aliases)
        for key, node in uses:
            if key in checked and key:
                self.hit(node, "py-check-then-use",
                         "checks a path's owner by path, then opens the path by name",
                         "owner_trust.read_owner_file (checks on the open descriptor)")

    def _sink(self, s: Sink, scope: Scope, where: str, uses: list):
        tags = [scope.tags(p) for p in s.paths]
        joined = _either(*tags) if tags else NONE
        if self._fn is not None:
            # a path that is one of the function's own parameters: the caller
            # names it, so the call site is checked (finish)
            params = self.params_of.get(self._fn.name, ())
            for p, t in zip(s.paths, tags):
                if isinstance(p, ast.Name) and p.id in params and not t & LOC:
                    self.param_sinks.setdefault(self._fn.name, {}).setdefault(
                        p.id, (s.kind, s.op))
        if s.kind == "read" or (s.kind == "write" and s.op != "mkdir"):
            for p, t in zip(s.paths, tags):
                if not (t & {"P", "T"}):      # a checked path of the owner's own is fine
                    uses.append((_path_key(p), s.node))
        if s.kind == "sqlite":
            if joined & {"P", "T"} and "R" not in joined:
                return               # owner-private, or this process's scratch
            self.hit(s.node, "py-sqlite", "opens a SQLite database with plain sqlite3.connect",
                     "ledger.guarded_connect")
            return
        if s.kind == "git":
            if is_resident(joined):
                self.hit(s.node, "py-git", "runs git in a resident-space tree",
                         "the deploy hook's checked tree (deploy_tree_ok, git with GIT_* cleared)")
            return
        if s.kind == "env-exec":
            if s.op == "code":
                if is_resident(joined):
                    self.hit(s.node, "py-env-exec", "loads code from resident space",
                             "none: run it as the resident (sudo -u ea-bot)")
            else:
                self.hit(s.node, "py-env-exec", f"loads an env file ({s.op})",
                         "owner_trust.load_owner_env / read_env_values")
            return
        if "A" in joined:
            if s.kind == "read":
                self.hit(s.node, "py-trust-anchor", "reads a file in the auth directory by path",
                         "owner_trust.read_owner_file")
            else:
                helper = ANCHOR_HELP.get(s.op, "owner_trust.write_owner_file")
                self.hit(s.node, "py-trust-anchor",
                         f"{s.op}s a file in the auth directory by path", helper)
            return
        if s.kind == "read" and joined & {"S", "E"}:
            if "S" in joined:
                self.hit(s.node, "py-env-file", "reads a secrets file as text",
                         "owner_trust.owner_secret / owner_secrets")
            else:
                self.hit(s.node, "py-env-file", "reads ~/.ea/.env as text",
                         "owner_trust.read_env_values (allowlisted keys, as data)")
            return
        if s.kind == "read":
            for p, t in zip(s.paths, tags):
                if is_resident(t):
                    key = _path_key(p)
                    self.reads.append((key, self._mod_level_key(key), s.node, where))
            return
        # a write
        for p, t in zip(s.paths, tags):
            if is_resident(t):
                self.writes.add((where, _path_key(p)))
                what = {"replace": "writes", "append": "appends to", "rmw": "rewrites",
                        "create": "creates", "mkdir": "makes the directory", "move": "renames",
                        "copy": "copies onto", "chmod": "changes the mode of",
                        "link": "makes a link at", "temp": "makes a temp file in",
                        "write": "writes"}[s.op]
                self.hit(s.node, "py-write", f"{what} a resident path by name", WRITE_HELP[s.op])
                return
        for p in s.paths:
            self.writes.add((where, _path_key(p)))


def _own_nodes(st):
    """The nodes of one statement, not descending into nested statements
    (compound statements report their header expressions only)."""
    if isinstance(st, (ast.If, ast.While)):
        roots = [st.test]
    elif isinstance(st, (ast.For, ast.AsyncFor)):
        roots = [st.iter]
    elif isinstance(st, (ast.With, ast.AsyncWith)):
        roots = [i.context_expr for i in st.items]
    elif isinstance(st, ast.Try) or hasattr(ast, "TryStar") and isinstance(st, ast.TryStar):
        roots = []
    elif isinstance(st, ast.Match) if hasattr(ast, "Match") else False:
        roots = [st.subject]
    elif isinstance(st, ast.Expr) and _const_str(st.value) is not None:
        roots = []          # a docstring or a bare string is not code
    else:
        roots = [st]
    for r in roots:
        yield from _walk_expr(r)


def _walk_expr(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def lint_python(sources: dict[str, str], report: set[str] | None = None) -> tuple[list[Hit], list[str]]:
    """Hits in the Python files of `sources` (all of them analysed; only
    those in `report`, when given, reported)."""
    project = PyProject(sources)
    resident_keys = {k for rel in RESIDENT_MODULES if rel in project.modules
                     for k in project.modules[rel].keys}
    resident_scripts = {Path(rel).name if rel.endswith(".py") else None for rel in RESIDENT_MODULES}
    resident_scripts.discard(None)
    checkers = {rel: PyChecker(project, m) for rel, m in project.modules.items()
                if rel not in HELPER_FILES and rel not in RESIDENT_MODULES}
    for c in checkers.values():
        c.run()
    for _ in range(6):
        if not any([c.propagate(checkers) for c in checkers.values()]):
            break
    hits: list[Hit] = []
    for rel, c in checkers.items():
        if report is not None and rel not in report:
            continue
        found = c.finish(checkers) + _resident_module_hits(c.m, resident_keys, resident_scripts)
        if rel in TERMINAL_TOOLS:
            found = [h for h in found if h.rule in TERMINAL_RULES]
        hits.extend(found)
    return hits, project.errors


def _resident_module_hits(m: PyModule, keys: set[str], scripts: set[str]) -> list[Hit]:
    """An import of a resident-only module, or a command list that runs one
    without switching to the resident (one hit per line)."""
    out = []
    lines: set[int] = set()

    def hit(node, message):
        line = getattr(node, "lineno", 0)
        if line in lines:
            return
        lines.add(line)
        out.append(Hit(m.rel, line, "py-resident-module", _norm(m.line(line)), message,
                       "run it as the resident (sudo -n -u ea-bot), or take it out of "
                       "RESIDENT_MODULES in scripts/boundary_lint.py"))
    for node, key in m.imports:
        if key in keys:
            hit(node, f"imports {key}, a resident-only module, into owner code")
    for node in ast.walk(m.tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        texts = [c.value for c in ast.walk(node)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        names = [n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(node)
                 if isinstance(n, (ast.Name, ast.Attribute))]
        if any(t.endswith(tuple(scripts)) for t in texts) and not (
                "ea-bot" in texts or any("RESIDENT_USER" in n or "EA_BOT_USER" in n
                                         for n in names)):
            hit(node, "runs a resident-only tool without switching to the resident")
    return out


# ══ Shell ══════════════════════════════════════════════════════════════

_SH_RESIDENT = [
    r"\$\{?EA_DIR\b", r"~/\.ea(?![-\w])", r"\$\{?HOME\}?/\.ea(?![-\w])", r"/home/ea-bot\b",
    r"\$\{?EA_BOT_HOME\b", r"\$\{?EA_BOT_SCRIPT_DIR\b",
]
_SH_PRIVATE = re.compile(r"\.ea-private|EA_PRIVATE_DIR|OWNER_LOG|owner_log")
_SH_ASSIGN = re.compile(r"^\s*(?:(?:local|export|readonly|declare(?:\s+-\w+)*)\s+)?"
                        r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SH_AS_RESIDENT = re.compile(r"\b(?:sudo|runuser)\b[^|;&]*?-u\s+\"?(?:ea-bot|\$\{?EA_BOT_USER\}?|"
                             r"\$\{?RESIDENT_USER\}?)\"?")
_SH_HELPER = re.compile(r"owner-io\.sh|\$\{?OWNER_IO\}?|\bdeploy_git\b|\bdeploy_tree_ok\b|\bdst_git\b")
_SH_WRITE_CMDS = {"mkdir": "mkdir", "touch": "touch", "cp": "write", "mv": "mv", "ln": "write",
                  "rm": "rm", "chmod": "write", "chown": "write", "chgrp": "write",
                  "install": "write", "tee": "write", "truncate": "write", "rsync": "write",
                  "mktemp": "write"}
_SH_READ_CMDS = {"cat", "head", "tail", "jq"}
_HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_SH_SPLIT = re.compile(r"\|\||&&|[;|&`]|\$\(|\(|\)|\{\s|\bthen\b|\bdo\b|\belse\b")
# eval of a file's content, or export of the words a file holds
_SH_EVAL_ENV = re.compile(r"\beval\b[^;|&]*\$\(\s*(?:cat\b|<)|\beval\b[^;|&]*\.env\b"
                          r"|\bexport\s+\$\(\s*(?:cat|grep|xargs|<)[^)]*\.env")


def _scan_quotes(line: str, q: str = "") -> tuple[str, str]:
    """(the line without its trailing comment, the quote still open at its
    end), starting inside quote `q` ("" for none)."""
    out, prev, i = [], " ", 0
    while i < len(line):
        ch = line[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(line):
                out.append(line[i:i + 2])
                i += 2
                continue
            if ch == q:
                q = ""
        elif ch == "\\" and i + 1 < len(line):
            out.append(line[i:i + 2])
            i += 2
            prev = "x"
            continue
        elif ch in "'\"":
            q = ch
        elif ch == "#" and prev in " \t;|&(":
            break
        out.append(ch)
        prev = ch
        i += 1
    return "".join(out), q


def shell_lines(text: str):
    """(line number, text, kind) for each logical line. kind is "code" (the
    comment stripped, continuation lines joined), "doc" (a heredoc body) or
    "str" (a line inside a quoted string that spans lines)."""
    raw = text.splitlines()
    i, heredoc, strip_tabs, q = 0, None, False, ""
    while i < len(raw):
        n, line = i + 1, raw[i]
        i += 1
        if heredoc is not None:
            if (line.lstrip("\t") if strip_tabs else line) == heredoc:
                heredoc = None
                continue
            yield n, line, "doc"
            continue
        if q:
            _, q = _scan_quotes(line, q)
            yield n, line, "str"
            continue
        code, q = _scan_quotes(line)
        while not q and code.endswith("\\") and i < len(raw):
            more, q = _scan_quotes(raw[i])
            code = code[:-1] + " " + more
            i += 1
        m = _HEREDOC.search(code)
        if m:
            # a heredoc only when its terminator line follows
            word, tabs = m.group(3), bool(m.group(1))
            if any((r.lstrip("\t") if tabs else r) == word for r in raw[i:]):
                heredoc, strip_tabs = word, tabs
        yield n, code, "code"


def _redirects(code: str):
    """(operator, target word) for each redirection outside quotes; a
    descriptor duplication (2>&1, >&2) is not one."""
    out, q, i, n = [], "", 0, len(code)
    while i < n:
        ch = code[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = ""
            i += 1
            continue
        if ch in "'\"":
            q = ch
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in "<>":
            j = i
            if ch == "<":
                if code.startswith("<<", i) or code.startswith("<(", i) or (i and code[i - 1] == "<"):
                    i += 1
                    continue
                op, j = "<", i + 1
            else:
                op, j = (">>", i + 2) if code.startswith(">>", i) else (">", i + 1)
                if j < n and code[j] in "|":
                    j += 1
                if j < n and code[j] == "&":      # a duplication, not a file
                    i = j + 1
                    continue
                if i and code[i - 1] == "&":
                    op = "&" + op
            while j < n and code[j] in " \t":
                j += 1
            k = j
            if k < n and code[k] in "'\"":
                end = code.find(code[k], k + 1)
                k = n if end < 0 else end + 1
            else:
                while k < n and code[k] not in " \t;|&()<>":
                    k += 1
            word = code[j:k].strip("'\"")
            if word:
                out.append((op, word))
            i = max(k, i + 1)
            continue
        i += 1
    return out


def _unquote_single(s: str) -> str:
    return re.sub(r"'[^']*'", "''", s)


_CRON = re.compile(r"^\s*(?:@(?:reboot|yearly|annually|monthly|weekly|daily|hourly)"
                   r"|(?:[\d*/,\-]+\s+){5})\S")
_CRON_SINK = re.compile(r"(?:\d?>>?|&>>?)\s*(\S+)")


class ShellVars:
    """The variables a script sets, and whether each names resident space."""

    def __init__(self, lines):
        self.values: dict[str, list[str]] = {}
        for _, code, kind in lines:
            if kind != "code":
                continue
            m = _SH_ASSIGN.match(code)
            if m:
                self.values.setdefault(m.group(1), []).append(m.group(2))
        self.resident: set[str] = set()
        self.private: set[str] = set()
        for _ in range(4):
            for name, vals in self.values.items():
                for v in vals:
                    if _SH_PRIVATE.search(v) or any(re.search(rf"\$\{{?{p}\b", v) for p in self.private):
                        self.private.add(name)
                    elif self.word_resident(v):
                        self.resident.add(name)
        self.resident -= self.private

    def word_resident(self, word: str) -> bool:
        if _SH_PRIVATE.search(word):
            return False
        if any(re.search(p, word) for p in _SH_RESIDENT):
            return True
        return any(re.search(rf"\$\{{?{v}\b", word) for v in getattr(self, "resident", ()))

    def word_private(self, word: str) -> bool:
        if _SH_PRIVATE.search(word):
            return True
        return any(re.search(rf"\$\{{?{v}\b", word) for v in self.private)

    def expand(self, word: str, depth: int = 0) -> str:
        if depth > 5:
            return word

        def sub(m):
            vals = self.values.get(m.group(1))
            return self.expand(vals[-1], depth + 1) if vals else m.group(0)
        return re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", sub, word)


def _after_sudo(words: list[str]) -> list[str]:
    """The command a `sudo` or `runuser` line runs: its options (and their
    arguments) and a `--` dropped."""
    out = words[1:]
    while out:
        w = out[0]
        if w == "--":
            return out[1:]
        if w in ("-u", "-g", "-C", "-D", "-h", "-p", "-U", "-R", "-T"):
            out = out[2:]
        elif w.startswith("-"):
            out = out[1:]
        else:
            break
    return out


def lint_shell(rel: str, text: str) -> list[Hit]:
    lines = list(shell_lines(text))
    sv = ShellVars(lines)
    hits: list[Hit] = []
    src = text.splitlines()

    def hit(n, rule, message, helper):
        hits.append(Hit(rel, n, rule, _norm(src[n - 1]) if n <= len(src) else "", message, helper))

    for n, code, kind in lines:
        # unit and crontab text is checked wherever it sits: a heredoc and a
        # quoted string are how the install steps write them
        unit = re.search(r"^\s*EnvironmentFile=-?(\S*)", code)
        if unit:
            val = unit.group(1).strip("\"'")
            if val and not sv.word_private(val) and not _SH_PRIVATE.search(sv.expand(val)):
                hit(n, "unit-envfile", "a unit loads an environment file outside ~/.ea-private",
                    "an owner-private file in ~/.ea-private")
            continue
        if _CRON.match(code):
            for m in _CRON_SINK.finditer(code):
                if sv.word_resident(m.group(1).strip("\"'")):
                    hit(n, "cron-log", "a crontab line sends an owner job's output into "
                        "resident space", "an owner-private log (~/.ea-private/logs, "
                        "log_paths.owner_log_dir)")
                    break
            continue
        if kind != "code":
            continue
        seen = False
        for op, target in _redirects(code):
            if target.startswith(("/dev/", "&")) or not sv.word_resident(target):
                continue
            if op == "<":
                hit(n, "sh-io", "the owner's shell reads a resident path by name",
                    "scripts/owner-io.sh read")
            else:
                helper = ("an owner-private log (~/.ea-private/logs), or scripts/owner-io.sh write"
                          if ">>" in op else "scripts/owner-io.sh write")
                hit(n, "sh-io", "the owner's shell writes a resident path by name", helper)
            seen = True
            break
        if seen or _SH_HELPER.search(code):
            continue
        if _SH_EVAL_ENV.search(code):
            hit(n, "sh-source", "evaluates an env file's content as shell",
                "scripts/owner-io.sh env (KEY=VALUE as data)")
            continue
        for part in _SH_SPLIT.split(_unquote_single(code)):
            words = part.split()
            while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])
                             or words[0] in ("exec", "command", "builtin", "env", "nice", "!",
                                             "if", "while", "until", "time")):
                words = words[1:]
            if not words:
                continue
            if words[0] in ("sudo", "runuser"):
                if _SH_AS_RESIDENT.search(" ".join(words)):
                    continue            # runs as the resident, with its access
                words = _after_sudo(words)
                if not words:
                    continue
            cmd, rest = words[0], words[1:]
            joined = " ".join(rest)
            if cmd == "git":
                if any(w == "-C" and sv.word_resident(rest[i + 1])
                       for i, w in enumerate(rest[:-1])):
                    hit(n, "sh-git", "runs git in a resident-space tree",
                        "the deploy hook's deploy_tree_ok + deploy_git")
                continue
            if cmd in ("source", "."):
                if rest and (sv.word_resident(rest[0]) or re.search(r"\.env\b|secrets", rest[0])):
                    hit(n, "sh-source", "sources an env file or a resident-space file as shell",
                        "scripts/owner-io.sh env (KEY=VALUE as data)")
                continue
            if cmd == "sed" and any(w.startswith("-i") for w in rest):
                cmd = "tee"
            args = [w for w in rest if not w.startswith("-")]
            if cmd in _SH_WRITE_CMDS and any(sv.word_resident(a) for a in args):
                hit(n, "sh-io", f"the owner's shell runs {cmd} on a resident path",
                    f"scripts/owner-io.sh {_SH_WRITE_CMDS[cmd]}")
                break
            if cmd in _SH_READ_CMDS and any(sv.word_resident(a) for a in args):
                hit(n, "sh-io", f"the owner's shell runs {cmd} on a resident path",
                    "scripts/owner-io.sh read")
                break
    return hits


# ══ files, baseline, CLI ═══════════════════════════════════════════════

def _kind(rel: str, head: str) -> str | None:
    if rel.startswith("tests/") and not rel.endswith(".sh"):
        return None
    if rel.endswith(".py"):
        return "py"
    if rel.endswith((".sh", ".service")):
        return "sh"
    first = head.split("\n", 1)[0]
    if first.startswith("#!"):
        if "python" in first:
            return "py"
        if re.search(r"\b(?:ba)?sh\b", first):
            return "sh"
    return None


def repo_files(root: Path = REPO) -> list[str]:
    """Every file the scan covers: the tracked files when `root` is a git
    checkout (an untracked scratch file never fails the check; the hook sees
    a new file once it is staged), else a walk of the tree."""
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                           capture_output=True, timeout=30,
                           env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        if r.returncode == 0:
            return sorted({p for p in r.stdout.decode().split("\0") if p and (root / p).is_file()})
    except (OSError, subprocess.TimeoutExpired):
        pass
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)


def _read(root: Path, rel: str) -> str | None:
    try:
        with open(root / rel, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def lint(root: Path = REPO, only: set[str] | None = None,
         overrides: dict[str, str] | None = None) -> tuple[list[Hit], list[str]]:
    """Every hit in the repository at `root` (only in `only` when given),
    with `overrides` standing in for some files' content."""
    overrides = overrides or {}
    py: dict[str, str] = {}
    hits: list[Hit] = []
    rels = set(repo_files(root)) | set(overrides)
    for rel in sorted(rels):
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        text = overrides.get(rel)
        if text is None:
            text = _read(root, rel)
        if text is None:
            continue
        kind = _kind(rel, text[:200])
        if kind == "py":
            py[rel] = text
        elif kind == "sh" and rel not in HELPER_FILES:
            if only is None or rel in only:
                found = lint_shell(rel, text)
                if rel in TERMINAL_TOOLS:
                    found = [h for h in found if h.rule in TERMINAL_RULES]
                hits.extend(found)
    py_hits, errors = lint_python(py, only)
    hits.extend(py_hits)
    hits.sort(key=lambda h: (h.file, h.line, h.rule))
    return hits, errors


def load_baseline(path: Path = BASELINE) -> list[dict]:
    """The accepted exceptions: each {file, rule, reason, lines}, where
    `lines` holds the source line of every accepted hit (whitespace
    collapsed; a line listed twice accepts two hits)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return list(doc.get("exceptions", []))


def compare(hits: list[Hit], baseline: list[dict], files: set[str] | None = None):
    """(new hits, stale lines): the hits the baseline does not accept, and
    the (file, rule, line) of each accepted line (in `files`, when given)
    that no hit matches any more."""
    allowed: dict[tuple, int] = {}
    for e in baseline:
        for line in e.get("lines", []):
            k = (e.get("file", ""), e.get("rule", ""), _norm(line))
            allowed[k] = allowed.get(k, 0) + 1
    seen: dict[tuple, int] = {}
    new = []
    for h in hits:
        seen[h.key] = seen.get(h.key, 0) + 1
        if seen[h.key] > allowed.get(h.key, 0):
            new.append(h)
    stale = [k for k, n in allowed.items()
             if (files is None or k[0] in files) and seen.get(k, 0) < n]
    return new, stale


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True,
                          timeout=30).stdout


def staged() -> dict[str, str]:
    """The staged content of each staged (added, copied, modified, renamed)
    file. git keeps the caller's environment: in the pre-commit hook,
    GIT_INDEX_FILE names the index being committed (a partial commit uses a
    temporary one)."""
    names = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").decode()
    out = {}
    for rel in filter(None, names.split("\0")):
        try:
            out[rel] = _git("show", f":{rel}").decode("utf-8")
        except (subprocess.CalledProcessError, UnicodeDecodeError):
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="static check for the owner trust rules "
                                             "(docs/SECURITY.md)")
    ap.add_argument("files", nargs="*", help="repository-relative files to scan (default: all)")
    ap.add_argument("--staged", action="store_true", help="scan the staged content of staged files")
    ap.add_argument("--all", action="store_true", help="list baselined hits too")
    ap.add_argument("--json", action="store_true", help="print hits as JSON")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    a = ap.parse_args(argv)

    overrides = None
    only = set(a.files) if a.files else None
    if a.staged:
        try:
            overrides = staged()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"boundary_lint: cannot read the staged files ({e})", file=sys.stderr)
            return 2
        only = set(overrides)
        if not only:
            return 0
    hits, errors = lint(REPO, only, overrides)
    errors = [e for e in errors if only is None or e.split(":", 1)[0] in only]
    new, stale = compare(hits, load_baseline(a.baseline), only)
    shown = hits if a.all else new
    if a.json:
        print(json.dumps([h.__dict__ for h in shown], indent=2))
    else:
        for h in shown:
            print(h.render())
        for e in errors:
            print(f"boundary_lint: {e}", file=sys.stderr)
        if not a.staged:
            for file, rule, line in stale:
                print(f"{file}: {rule}: a baseline line no hit matches any more; remove it "
                      f"from {a.baseline.name}: {line}")
        if new:
            sys.stdout.flush()
            print(f"\nboundary_lint: {len(new)} hit(s) bypass the owner trust rules "
                  f"(docs/SECURITY.md, \"Owner trust rules\"). Use the named helper; an "
                  f"accepted exception goes in tests/boundary_baseline.json with a reason.",
                  file=sys.stderr)
    return 1 if new or (stale and not a.staged) or errors else 0


if __name__ == "__main__":
    sys.exit(main())
