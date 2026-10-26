#!/usr/bin/env python3
"""deck_tools: the EA's deck workspace. Scaffold, lint, build, stage, render.

Decks are Slidev projects, authored the way the owner's own are. The owner's
presentations tree is mounted read-only at REF (theme, playbooks, helper
scripts, worked decks) and the EA's decks live in WORKSPACE under the
ea-bot user, next to a `shared` symlink into REF, so the owner's
build-deck.sh runs unchanged from a deck directory.

    deck_tools.py init [--template]        workspace, symlinks, the node_modules template
    deck_tools.py new --slug S --title T [--venue V] [--date D] [--subtitle X]
                                           [--description X] [--genre briefing|talk|lecture]
    deck_tools.py lint --slug S            slide-lint.py on slides.md (exit 1 = findings)
    deck_tools.py build --slug S [--no-pdf] [--figures]
                                           build_deck.sh: build, serve on the tailnet, PDF
    deck_tools.py render --slug S [--slides 1,3] [--clicks 99]
                                           one PNG per slide, to look at with Read
    deck_tools.py off --slug S             take the deck off the tailnet, stop its server
    deck_tools.py list                     decks, ports, URLs
    deck_tools.py figures <terms...>       figure-find.py over the owner's figure pool
    deck_tools.py doctor                   is the deck lane usable

`build` runs the deck's own build_deck.sh with three things arranged for
the ea-bot user: node from ~/.local/node, `tailscale` and `curl` shims
first on PATH (scripts/deck-bin) that turn the script's two route writes
into mediated deck_serve.py calls and its closing check into a fetch of the
deck's loopback port (the resident may not reach tailscale serve), and its
own PID directory and port range so it never
collides with the owner's staged decks. When the ea-bot user manager is
running (linger enabled), the build runs in a transient systemd scope so
the static server it leaves behind survives a bot restart.

Every route this tool creates is tailnet-only: deck_serve.py has no funnel
verb and the owner-side mediator executes nothing else. To share a deck outside
the tailnet, send the PDF.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
from deck_serve import PORT_RANGE, RESIDENT_USER, SLUG_RE, routes as _routes  # noqa: E402


def _home() -> Path:
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


HOME = _home()
EA_DIR = Path(os.environ.get("EA_DIR", str(HOME / ".ea")))
WORKSPACE = Path(os.environ.get("EA_DECKS_ROOT", str(HOME / "presentations")))
REF = Path(os.environ.get("EA_PRESENTATIONS_REF", "/srv/ea/presentations"))
LIBRARY_REF = Path(os.environ.get("EA_PRESENTATION_LIBRARY_REF",
                                  "/srv/ea/presentation-library"))
NODE_BIN = Path(os.environ.get("EA_NODE_BIN", str(HOME / ".local" / "node" / "bin")))
SHIM_DIR = REPO / "scripts" / "deck-bin"
PID_DIR = EA_DIR / "state" / "slidev-stage"
DECKS = WORKSPACE / "decks"
TEMPLATE = DECKS / "_template"
TEMPLATE_PKG_NAME = "ea-deck-template"
# The owner's briefing deck whose lockfile pins the toolchain versions.
EXEMPLAR = "ai-engineering-minor-2026-09"
BUILD_TIMEOUT_S = 1200
NPM_TIMEOUT_S = 900
RENDER_TIMEOUT_S = 600
TAILSCALE_BIN = "/usr/bin/tailscale"

# Symlinks in the workspace root: the owner's tree, read-only, at the
# relative places the presentation scripts expect.
LINKS = {
    "shared": REF / "shared",
    "playbooks": REF / "playbooks",
    "decks-ref": REF / "decks",
    "courses-ref": REF / "courses",
    "library-index": LIBRARY_REF / "library-index",
    "library": LIBRARY_REF / "library",
}

PACKAGE_JSON = {
    "name": "",
    "version": "1.0.0",
    "private": True,
    "description": "",
    "scripts": {
        "dev": "slidev --open",
        "build": "slidev build",
        "export": "",
        "stage:tailnet": "node ../../shared/scripts/stage-tailnet.mjs",
    },
    # `deck.theme` in ea-config.json adds the theme package: a local
    # `slidev-theme-<name>` from shared/theme-<name>. The default theme is
    # Slidev's own, bundled with the CLI, and needs no entry.
    "dependencies": {
        "@slidev/cli": "^0.50.0",
        "vue": "^3.5.0",
    },
    "devDependencies": {
        "playwright-chromium": "^1.60.0",
    },
}

VITE_CONFIG = """import { defineConfig } from 'vite'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const pkg = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf-8'))

export default defineConfig(({ command }) => ({
  // In `build` (the static deploy that staging serves), mount the deck under
  // its package name so the tailnet URL is https://<host>/<deck-name>/. In
  // dev keep base at '/' so Vite's internal /@fs/ paths resolve.
  base: command === 'build' ? `/${pkg.name}/` : '/',
  build: {
    outDir: `dist/${pkg.name}`,
    emptyOutDir: true,
  },
  server: {
    allowedHosts: ['.ts.net'],
  },
}))
"""

STYLE_CSS = """/* Briefing deck: dense status tables, few figures. The accent and
   muted colors come from the theme's variables when it defines them,
   with plain fallbacks for Slidev's default theme. */

.slidev-layout .acc {
  color: var(--cu-gold-deep, #8a6d00);
  font-weight: 600;
}

/* Status tables carry the slide, so they run tighter than the theme default. */
.slidev-layout table {
  font-size: 0.78rem;
}

.slidev-layout table th {
  padding: 0.35rem 0.6rem;
}

.slidev-layout table td {
  padding: 0.38rem 0.6rem;
  line-height: 1.45;
}

/* A status table that has to share the canvas with a figure. */
.slidev-layout .tight table th {
  padding: 0.28rem 0.6rem;
}

.slidev-layout .tight table td {
  padding: 0.22rem 0.6rem;
  line-height: 1.35;
}

.slidev-layout .tight table th:first-child,
.slidev-layout .tight table td:first-child {
  white-space: nowrap;
}

/* Source strip at the foot of a slide. */
.src {
  color: var(--cu-dark-gray, #555555);
  font-size: 0.68rem;
  line-height: 1.5;
}
"""

SLIDES_MD = """---
theme: {theme}
title: "{title}"
info: {info}
transition: slide-left
mdc: true
fonts:
  sans: 'Inter'
  serif: 'Source Serif 4'
  mono: 'JetBrains Mono'
  weights: '400,500,600,700'
download: "/{slug}/{slug}.pdf"
layout: cover
venue: "{venue}"
byline: "{byline}"
affiliation: "{affiliation}"
date: "{date}"
---

# {title}

{subtitle}

<!--
POINT: what this deck has to land, in one line.
ARC: from where to where.
SAY:
- the first thing to say in the room
-->

---

# Current State

<div class="text-sm leading-relaxed">

| Item | State | Next step | Owner | Date |
| --- | --- | --- | --- | --- |
| (replace this row) | | | | |

</div>

<!-- lint-ok: no-figure, no-reveal -->
<!--
POINT:
ARC:
SAY:
-
SOURCES:
- every number on this slide, with where it came from
-->
"""

SOURCE_MD = """# Source and decisions

Occasion: {venue}, {date}. Genre: {genre}.

Brief: {info}

## Primary sources

| Source | Used for |
| --- | --- |
| | |

## Decisions taken at build time

-
"""

README_MD = """# EA deck workspace

Decks the EA builds on request, one Slidev project per `decks/<slug>/`,
authored against the owner's theme and playbooks, which are mounted
read-only and symlinked here (`shared`, `playbooks`, `decks-ref`,
`library-index`). `decks/_template/` holds the installed toolchain that
every new deck copies its `node_modules` from; it is not a deck.

Each deck's `build_deck.sh` is the owner's script. Run through
`deck_tools.py build`, it serves the deck on the tailnet at
`https://<node>/<slug>/` and refreshes `public/<slug>.pdf`.

To adopt a deck into the owner's tree: `cp -r decks/<slug>
~/presentations/decks/` and re-link `build_deck.sh` there.
"""

RENDER_JS = r"""
const { chromium } = require("playwright-chromium");
const [port, slug, outDir, clicks, list, vw, vh] = process.argv.slice(1);
(async () => {
  const b = await chromium.launch();
  for (const s of list.split(",")) {
    const p = await b.newPage({ viewport: { width: Number(vw), height: Number(vh) },
                                deviceScaleFactor: 1 });
    await p.goto(`http://127.0.0.1:${port}/${slug}/${s}?clicks=${clicks}`,
                 { waitUntil: "networkidle", timeout: 60000 });
    await p.waitForTimeout(1200);
    await p.screenshot({ path: `${outDir}/${String(s).padStart(3, "0")}.png` });
    await p.close();
  }
  await b.close();
})().catch((e) => { console.error(String(e)); process.exit(1); });
"""


class DeckError(Exception):
    pass


# --- helpers -----------------------------------------------------------------

def _env() -> dict:
    """The environment build_deck.sh and node need under the ea-bot user."""
    env = dict(os.environ)
    env["HOME"] = str(HOME)
    env["EA_DIR"] = str(EA_DIR)
    env["PATH"] = os.pathsep.join([str(SHIM_DIR), str(NODE_BIN),
                                   "/usr/local/bin", "/usr/bin", "/bin"])
    env["SLIDEV_STAGE_PID_DIR"] = str(PID_DIR)
    env["DECK_FIRST_PORT"], env["DECK_LAST_PORT"] = (str(p) for p in PORT_RANGE)
    env["PRESENTATIONS_ROOT"] = str(WORKSPACE)
    env.pop("NVM_DIR", None)
    uid = os.geteuid()
    runtime = Path(f"/run/user/{uid}")
    if (runtime / "bus").exists():
        env["XDG_RUNTIME_DIR"] = str(runtime)
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / 'bus'}"
    return env


def _scope_prefix(slug: str, env: dict) -> list[str]:
    """`systemd-run --user --scope` when the ea-bot user manager answers, so
    the static server the build leaves behind lives in ea-bot's own slice
    and not in the bot service's cgroup (which a deploy restarts)."""
    if "DBUS_SESSION_BUS_ADDRESS" not in env or not shutil.which("systemd-run"):
        return []
    probe = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                           env=env, capture_output=True, text=True)
    if probe.returncode != 0:
        return []
    return ["systemd-run", "--user", "--scope", "--quiet",
            f"--unit=ea-deck-{slug}-{int(time.time())}"]


def _require_resident() -> None:
    """The workspace default is $HOME/presentations, which for the owner is
    the real tree. Anyone else sets EA_DECKS_ROOT explicitly."""
    if os.environ.get("EA_DECKS_ROOT"):
        return
    try:
        if os.geteuid() == pwd.getpwnam(RESIDENT_USER).pw_uid:
            return
    except KeyError:
        pass
    raise DeckError(f"deck_tools runs as {RESIDENT_USER}; set EA_DECKS_ROOT to use "
                    "it as another user")


def _check_slug(slug: str) -> str:
    if not SLUG_RE.match(slug or "") or slug.startswith("_"):
        raise DeckError(f"slug {slug!r} must be lowercase kebab-case, e.g. "
                        "ai-minor-update-2026-09")
    return slug


def _deck_dir(slug: str) -> Path:
    _check_slug(slug)
    deck = DECKS / slug
    if not (deck / "slides.md").exists():
        raise DeckError(f"no deck at {deck} (run `new --slug {slug}` first)")
    return deck


def _profile() -> dict:
    try:
        return json.loads((EA_DIR / "config" / "user-profile.json").read_text())
    except (OSError, ValueError):
        return {}


DEFAULT_THEME = "default"
THEME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def deck_theme() -> str:
    """`deck.theme` in ea-config.json: the Slidev theme every new deck
    uses. "default" (the default) is Slidev's built-in theme, bundled with
    the CLI; any other name is a local package at shared/theme-<name> in
    the owner's presentations tree. A name that is not a plain word is a
    DeckError, since it goes into package.json and the front matter."""
    try:
        cfg = json.loads((EA_DIR / "config" / "ea-config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    deck = cfg.get("deck") if isinstance(cfg, dict) else None
    theme = (deck or {}).get("theme") if isinstance(deck, dict) else None
    theme = str(theme or DEFAULT_THEME).strip()
    if not THEME_RE.match(theme):
        raise DeckError(f"deck.theme {theme!r} in ea-config.json must be a lowercase word")
    return theme


def _theme_dir(theme: str) -> Path:
    return REF / "shared" / f"theme-{theme}"


def _require_theme(theme: str) -> None:
    """The presentations tree must be mounted; a named theme must be a
    package in it. Nothing is written before this passes."""
    if not (REF / "shared" / "scripts" / "build-deck.sh").exists():
        raise DeckError(f"{REF} is not mounted; run deploy/enable-deck-builder.sh first")
    if theme != DEFAULT_THEME and not (_theme_dir(theme) / "package.json").exists():
        raise DeckError(f"theme {theme!r} (deck.theme in ea-config.json) has no package at "
                        f"{_theme_dir(theme)}; set deck.theme to \"default\" for Slidev's "
                        "built-in theme")


def _link(name: str, target: Path) -> str:
    """Create or repoint a symlink in the workspace; never replace a real
    directory (the owner's own tree has real ones at these names)."""
    link = WORKSPACE / name
    if link.is_symlink():
        if Path(os.readlink(link)) == target:
            return "ok"
        link.unlink()
        link.symlink_to(target)
        return "repointed"
    if link.exists():
        return "kept (real path)"
    link.symlink_to(target)
    return "created"


def _npm(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(NODE_BIN / "npm"), *args], cwd=cwd, env=_env(),
                          capture_output=True, text=True, timeout=NPM_TIMEOUT_S)


def _write_package_json(deck: Path, name: str, description: str,
                        theme: str = DEFAULT_THEME) -> None:
    pkg = json.loads(json.dumps(PACKAGE_JSON))
    pkg["name"] = name
    pkg["description"] = description
    if theme != DEFAULT_THEME:
        pkg["dependencies"][f"slidev-theme-{theme}"] = f"file:../../shared/theme-{theme}"
    pkg["scripts"]["export"] = (
        "slidev export --per-slide --wait-until networkidle "
        f"--executable-path /snap/bin/chromium --output dist/{name}.pdf")
    (deck / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")


def _lockfile_for(name: str) -> str | None:
    """The exemplar's package-lock.json with its root package renamed, so
    `npm ci` installs the same toolchain the owner's decks were built with."""
    src = REF / "decks" / EXEMPLAR / "package-lock.json"
    try:
        lock = json.loads(src.read_text())
    except (OSError, ValueError):
        return None
    lock["name"] = name
    root = lock.get("packages", {}).get("")
    if isinstance(root, dict):
        root["name"] = name
    return json.dumps(lock, indent=2) + "\n"


def _install_modules(deck: Path, name: str) -> str:
    """node_modules for a deck: copied from the template when it exists,
    otherwise installed from the pinned lockfile (network)."""
    if (TEMPLATE / "node_modules").is_dir():
        r = subprocess.run(["cp", "-a", str(TEMPLATE / "node_modules"), str(deck / "node_modules")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise DeckError(f"copying node_modules failed: {r.stderr.strip()[:200]}")
        lock = _lockfile_for(name)
        if lock:
            (deck / "package-lock.json").write_text(lock)
        return "copied from template"
    lock = _lockfile_for(name)
    if lock:
        (deck / "package-lock.json").write_text(lock)
        r = _npm(["ci", "--no-audit", "--no-fund"], deck)
        if r.returncode == 0:
            return "npm ci"
    r = _npm(["install", "--no-audit", "--no-fund"], deck)
    if r.returncode != 0:
        raise DeckError(f"npm install failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return "npm install"


def _serve_status() -> dict:
    """The node's serve config, read directly: tailscaled answers reads for
    any local user."""
    try:
        r = subprocess.run([TAILSCALE_BIN, "serve", "status", "--json"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def _dns_name() -> str:
    try:
        r = subprocess.run([TAILSCALE_BIN, "status", "--json"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return ""


def _served_port(slug: str) -> int | None:
    return _routes(_serve_status()).get(f"/{slug}")


def count_slides(text: str) -> int:
    """Count slides the way Slidev does: per-slide frontmatter blocks are
    delimited by --- too and belong to the chunk after them; a slide whose
    frontmatter says `hide: true` is not served."""
    parts = re.split(r"(?m)^---$", text)

    def is_fm(chunk: str) -> bool:
        lines = [ln for ln in chunk.split("\n") if ln.strip()]
        return bool(lines) and all(
            re.match(r"^\s*[\w-]+\s*:", ln) or re.match(r"^\s+\S", ln) for ln in lines)

    chunks = parts[2:]
    n = i = 0
    while i < len(chunks):
        if is_fm(chunks[i]) and i + 1 < len(chunks):
            hidden = re.search(r"(?m)^\s*hide\s*:\s*true\s*$", chunks[i]) is not None
            i += 1
            if hidden:
                i += 1
                continue
        n += 1
        i += 1
    return n


PREVIEW_WIDTH = 1280


def preview_viewport(text: str) -> tuple[int, int]:
    """The capture viewport for a deck's slides: 1280 px wide, as high as the
    headmatter's `aspectRatio` says (16/9 when absent). A 3:2 poster rendered
    in a 16:9 frame is letterboxed and its text a third smaller."""
    parts = re.split(r"(?m)^---$", text)
    head = parts[1] if len(parts) > 1 else ""
    m = re.search(r"(?m)^aspectRatio\s*:\s*['\"]?([^'\"\n#]+)", head)
    aspect = 16 / 9
    if m:
        raw = m.group(1).strip()
        try:
            if "/" in raw:
                num, den = raw.split("/", 1)
                aspect = float(num) / float(den)
            else:
                aspect = float(raw)
        except (ValueError, ZeroDivisionError):
            aspect = 16 / 9
    if aspect <= 0:
        aspect = 16 / 9
    return PREVIEW_WIDTH, round(PREVIEW_WIDTH / aspect)


def _tail(text: str, n: int = 40) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


# --- verbs -------------------------------------------------------------------

def cmd_init(a) -> int:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    DECKS.mkdir(exist_ok=True)
    for d in (WORKSPACE, DECKS):
        try:
            os.chmod(d, 0o2775)     # group ea (the owner is in it) may edit too
        except OSError:
            pass
    PID_DIR.mkdir(parents=True, exist_ok=True)
    links = {name: _link(name, target) for name, target in LINKS.items()}
    if not (WORKSPACE / "README.md").exists():
        (WORKSPACE / "README.md").write_text(README_MD)
    report = {"status": "ok", "workspace": str(WORKSPACE), "links": links,
              "ref_mounted": (REF / "shared" / "scripts" / "build-deck.sh").exists(),
              "library_mounted": (LIBRARY_REF / "library-index").exists(),
              "node": (NODE_BIN / "node").exists(),
              "template": (TEMPLATE / "node_modules").is_dir()}
    if a.template:
        if not report["ref_mounted"]:
            raise DeckError(f"{REF} is not mounted; run deploy/enable-deck-builder.sh first")
        if not report["node"]:
            raise DeckError(f"no node at {NODE_BIN}; run deploy/enable-deck-builder.sh first")
        theme = deck_theme()
        _require_theme(theme)
        TEMPLATE.mkdir(exist_ok=True)
        _write_package_json(TEMPLATE, TEMPLATE_PKG_NAME, "toolchain template, not a deck", theme)
        if (TEMPLATE / "node_modules").is_dir():
            shutil.rmtree(TEMPLATE / "node_modules")
        lock = _lockfile_for(TEMPLATE_PKG_NAME)
        how = "npm install"
        if lock:
            (TEMPLATE / "package-lock.json").write_text(lock)
            r = _npm(["ci", "--no-audit", "--no-fund"], TEMPLATE)
            how = "npm ci"
        if not lock or r.returncode != 0:
            r = _npm(["install", "--no-audit", "--no-fund"], TEMPLATE)
            how = "npm install"
        if r.returncode != 0:
            raise DeckError(f"{how} failed in the template: {(r.stderr or r.stdout).strip()[-400:]}")
        report["template"] = (TEMPLATE / "node_modules").is_dir()
        report["template_install"] = how
    print(json.dumps(report, indent=2))
    return 0


def cmd_new(a) -> int:
    slug = _check_slug(a.slug)
    deck = DECKS / slug
    if deck.exists():
        raise DeckError(f"{deck} exists; edit its slides.md, or pick another slug")
    theme = deck_theme()
    _require_theme(theme)
    profile = _profile()
    today = _dt.date.today()
    date = a.date or today.strftime("%B %-d, %Y")
    venue = a.venue or ""
    info = a.description or a.title
    for sub in ("public/figures", "public/img", "figures-src", "dist"):
        (deck / sub).mkdir(parents=True)
    _write_package_json(deck, slug, info, theme)
    (deck / "vite.config.ts").write_text(VITE_CONFIG)
    (deck / "style.css").write_text(STYLE_CSS)
    (deck / "slides.md").write_text(SLIDES_MD.format(
        theme=theme,
        title=a.title.replace('"', "'"), info=info.replace("\n", " "),
        slug=slug, venue=venue.replace('"', "'"),
        byline=profile.get("name", ""),
        affiliation=", ".join(x for x in (
            f"Department of {profile['department']}" if profile.get("department") else "",
            profile.get("university", "")) if x),
        date=date, subtitle=a.subtitle or ""))
    (deck / "SOURCE.md").write_text(SOURCE_MD.format(
        venue=venue or "(venue)", date=date, genre=a.genre, info=info))
    (deck / "build_deck.sh").symlink_to("../../shared/scripts/build-deck.sh")
    modules = _install_modules(deck, slug)
    print(json.dumps({
        "status": "ok", "slug": slug, "deck_dir": str(deck),
        "slides": str(deck / "slides.md"), "source": str(deck / "SOURCE.md"),
        "theme": theme, "node_modules": modules,
        "playbooks": [str(REF / "playbooks" / "common.md"),
                      str(REF / "playbooks" / f"{a.genre}.md")],
        "next": [f"write {deck / 'slides.md'} and SOURCE.md",
                 f"deck_tools.py lint --slug {slug}",
                 f"deck_tools.py build --slug {slug}",
                 f"deck_tools.py render --slug {slug}  (then Read every PNG)"],
    }, indent=2))
    return 0


def cmd_lint(a) -> int:
    deck = _deck_dir(a.slug)
    lint = REF / "shared" / "scripts" / "slide-lint.py"
    if not lint.exists():
        raise DeckError(f"{lint} missing; is {REF} mounted?")
    r = subprocess.run([sys.executable, str(lint), str(deck / "slides.md")],
                       capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode == 0:
        print(json.dumps({"status": "ok", "slug": a.slug, "lint": "clean"}))
    return r.returncode


def cmd_build(a) -> int:
    deck = _deck_dir(a.slug)
    script = deck / "build_deck.sh"
    if not script.exists():
        script.symlink_to("../../shared/scripts/build-deck.sh")
    if not (deck / "node_modules").is_dir():
        _install_modules(deck, a.slug)
    env = _env()
    args = [str(script)] + (["--no-pdf"] if a.no_pdf else []) + (["--figures"] if a.figures else [])
    cmd = _scope_prefix(a.slug, env) + args
    try:
        r = subprocess.run(cmd, cwd=deck, env=env, capture_output=True, text=True,
                           timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise DeckError(f"build_deck.sh ran past {BUILD_TIMEOUT_S}s")
    out = (r.stdout or "") + (r.stderr or "")
    print(_tail(out))
    m = re.search(r"^serving: (\S+)", out, re.M)
    pdf = deck / "public" / f"{a.slug}.pdf"
    print(json.dumps({
        "status": "ok" if r.returncode == 0 else "error", "rc": r.returncode,
        "slug": a.slug, "url": m.group(1) if m else None,
        "pdf": str(pdf) if pdf.exists() and not a.no_pdf else None,
        "port": _served_port(a.slug), "tailnet_only": True,
        "scoped": bool(cmd[0] == "systemd-run"),
    }))
    return r.returncode


def cmd_off(a) -> int:
    deck = _deck_dir(a.slug)
    script = deck / "build_deck.sh"
    if not script.exists():
        script.symlink_to("../../shared/scripts/build-deck.sh")
    r = subprocess.run([str(script), "--off"], cwd=deck, env=_env(),
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    print(_tail(out))
    print(json.dumps({"status": "ok" if r.returncode == 0 else "error", "rc": r.returncode,
                      "slug": a.slug, "port": _served_port(a.slug)}))
    return r.returncode


def cmd_render(a) -> int:
    deck = _deck_dir(a.slug)
    port = _served_port(a.slug)
    if port is None:
        raise DeckError(f"/{a.slug} is not served; run `build --slug {a.slug}` first")
    if not (deck / "node_modules" / "playwright-chromium").exists():
        raise DeckError("playwright-chromium is not in the deck's node_modules")
    text = (deck / "slides.md").read_text()
    n = count_slides(text)
    vw, vh = preview_viewport(text)
    if a.slides:
        wanted = []
        for tok in a.slides.split(","):
            tok = tok.strip()
            if "-" in tok:
                lo, hi = (int(x) for x in tok.split("-", 1))
                wanted.extend(range(lo, hi + 1))
            elif tok:
                wanted.append(int(tok))
        wanted = [s for s in wanted if 1 <= s <= n]
        if not wanted:
            raise DeckError(f"no slide in {a.slides!r} is within 1..{n}")
    else:
        wanted = list(range(1, n + 1))
    out_dir = deck / "render"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()
    r = subprocess.run([str(NODE_BIN / "node"), "-e", RENDER_JS, str(port), a.slug,
                        str(out_dir), str(a.clicks), ",".join(map(str, wanted)),
                        str(vw), str(vh)],
                       cwd=deck, env=_env(), capture_output=True, text=True,
                       timeout=RENDER_TIMEOUT_S)
    if r.returncode != 0:
        raise DeckError(f"render failed: {(r.stderr or r.stdout).strip()[-400:]}")
    pngs = sorted(str(p) for p in out_dir.glob("*.png"))
    print(json.dumps({"status": "ok", "slug": a.slug, "slides": n, "rendered": wanted,
                      "viewport": f"{vw}x{vh}",
                      "pngs": pngs, "note": "Read each PNG; check overflow past "
                      f"{vw}x{vh}, collisions, raw LaTeX, empty canvas."}, indent=2))
    return 0


def cmd_list(a) -> int:
    served = _routes(_serve_status())
    host = _dns_name()
    rows = []
    if DECKS.is_dir():
        for d in sorted(DECKS.iterdir()):
            if d.name.startswith("_") or not (d / "slides.md").exists():
                continue
            port = served.get(f"/{d.name}")
            rows.append({
                "slug": d.name, "port": port,
                "url": f"https://{host}/{d.name}/" if port and host else None,
                "pdf": (d / "public" / f"{d.name}.pdf").exists(),
                "modified": _dt.datetime.fromtimestamp(
                    (d / "slides.md").stat().st_mtime).isoformat(timespec="minutes"),
            })
    print(json.dumps({"status": "ok", "workspace": str(WORKSPACE), "decks": rows}, indent=2))
    return 0


def cmd_figures(a) -> int:
    find = REF / "shared" / "scripts" / "figure-find.py"
    if not find.exists():
        raise DeckError(f"{find} missing; is {REF} mounted?")
    if not (WORKSPACE / "library-index").exists():
        raise DeckError("the figure pool is not mounted (library-index); "
                        "run deploy/enable-deck-builder.sh")
    r = subprocess.run([sys.executable, str(find), *a.args], cwd=WORKSPACE, env=_env(),
                       capture_output=True, text=True, timeout=300)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def cmd_doctor(a) -> int:
    checks = {}
    checks["ref_mounted"] = (REF / "shared" / "scripts" / "build-deck.sh").exists()
    checks["library_mounted"] = (LIBRARY_REF / "library-index" / "figures-ledger.jsonl").exists()
    checks["workspace"] = DECKS.is_dir()
    checks["node"] = (NODE_BIN / "node").exists()
    checks["template"] = (TEMPLATE / "node_modules" / "@slidev").is_dir()
    checks["shim"] = all(os.access(SHIM_DIR / n, os.X_OK) for n in ("tailscale", "curl"))
    checks["pid_dir_writable"] = os.access(PID_DIR, os.W_OK) if PID_DIR.exists() else False
    env = _env()
    checks["user_manager"] = "DBUS_SESSION_BUS_ADDRESS" in env
    try:
        r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "deck_serve.py"), "status"],
                           capture_output=True, text=True, timeout=60, env=env)
        checks["mediator_deck_serve"] = r.returncode == 0
        if r.returncode != 0:
            checks["mediator_error"] = (r.stderr or r.stdout).strip()[-200:]
    except subprocess.TimeoutExpired:
        checks["mediator_deck_serve"] = False
        checks["mediator_error"] = "timeout"
    required = ("ref_mounted", "workspace", "node", "template", "shim", "mediator_deck_serve")
    ok = all(checks.get(k) for k in required)
    print(json.dumps({"status": "ok" if ok else "incomplete", "checks": checks,
                      "fix": None if ok else "sudo bash deploy/enable-deck-builder.sh"},
                     indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("init", allow_abbrev=False); p.add_argument("--template", action="store_true")
    p = sub.add_parser("new", allow_abbrev=False)
    p.add_argument("--slug", required=True); p.add_argument("--title", required=True)
    p.add_argument("--venue", default=""); p.add_argument("--date", default="")
    p.add_argument("--subtitle", default=""); p.add_argument("--description", default="")
    p.add_argument("--genre", default="briefing", choices=("briefing", "talk", "lecture"))
    p = sub.add_parser("lint", allow_abbrev=False); p.add_argument("--slug", required=True)
    p = sub.add_parser("build", allow_abbrev=False); p.add_argument("--slug", required=True)
    p.add_argument("--no-pdf", action="store_true"); p.add_argument("--figures", action="store_true")
    p = sub.add_parser("render", allow_abbrev=False); p.add_argument("--slug", required=True)
    p.add_argument("--slides", default=""); p.add_argument("--clicks", default="99")
    p = sub.add_parser("off", allow_abbrev=False); p.add_argument("--slug", required=True)
    sub.add_parser("list", allow_abbrev=False)
    p = sub.add_parser("figures", allow_abbrev=False); p.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("doctor", allow_abbrev=False)
    a = ap.parse_args(argv)
    try:
        _require_resident()
        return {"init": cmd_init, "new": cmd_new, "lint": cmd_lint, "build": cmd_build,
                "render": cmd_render, "off": cmd_off, "list": cmd_list,
                "figures": cmd_figures, "doctor": cmd_doctor}[a.verb](a)
    except DeckError as e:
        print(json.dumps({"status": "error", "message": str(e)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
