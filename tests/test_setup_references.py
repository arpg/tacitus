#!/usr/bin/env python3
"""Every pointer into docs/SETUP.md names an appendix that exists.

docs/SETUP.md is reference material with named appendices, and the skill, the
doctor's fix texts, the docs and the code point into it by heading:
`docs/SETUP.md "Heading"`, `docs/SETUP.md 'Heading'` (inside a Python string) or
`docs/SETUP.md#slug`. A heading renamed without its pointers leaves a reader
with nothing to open, so this test slugs the headings the way GitHub does
and checks every pointer in the tree against them. Step numbers were the
old scheme; none may remain where this tree points into docs/SETUP.md.
"""
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "docs/SETUP.md"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "worktrees"}
TEXT_SUFFIXES = {".md", ".py", ".sh", ".txt", ".json", ".yml", ".yaml", ".example", ".toml", ".cfg", ""}

# A pointer is docs/SETUP.md, a separator, then the quoted name; a quote with
# no separator closes a string literal around the file name. A pointer
# may wrap onto the next line of a paragraph or a comment, and a
# double-quoted name may hold an apostrophe, so the text is joined on
# line breaks (dropping a comment marker) before the match.
QUOTED = re.compile(r"SETUP\.md[ ,:;(]{1,3}(?:\"([^\"]{1,80})\"|'([^']{1,80})')")
JOIN = re.compile(r"\n[ \t]*(?:#[ \t]*)?")
ANCHOR = re.compile(r"SETUP\.md#([A-Za-z0-9_-]+)")
INTERNAL_ANCHOR = re.compile(r"\]\(#([A-Za-z0-9_-]+)\)")
STEP_NUMBER = re.compile(r"SETUP\.md\)?,?\s+(?:[Ss]teps?\s*\d|\d[A-Za-z]?(?:\.\d)?\b)")
# Where the old step numbers lived, and the top-level files that point
# into docs/SETUP.md. scripts/install/setup.sh is held by its own work and is not listed. The
# fresh-install test keeps old step strings as the list of what the
# doctor must no longer say.
OWNED = ("docs/SETUP.md", ".claude", "scripts", "docs", "preprocessor", "deploy", "skills", "tests",
         "README.md", ".env.example", "scripts/install/user_init.py", "scripts/install/setup-ea-user-macos.sh",
         "scripts/install/setup-ea-user.sh")
STEP_EXEMPT = {"tests/test_fresh_install_path.py"}


def quoted_pointers(text: str) -> list[str]:
    joined = JOIN.sub(" ", text)
    return [" ".join((a or b).split()) for a, b in QUOTED.findall(joined)]


def github_slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def headings() -> tuple[set[str], set[str]]:
    names, slugs = set(), set()
    in_fence = False
    for line in SETUP.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        name = line.lstrip("#").strip()
        names.add(name)
        slugs.add(github_slug(name))
    return names, slugs


def tree_files() -> list[Path]:
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True)
    if r.returncode == 0 and r.stdout:
        return [REPO / p for p in r.stdout.decode().split("\0") if p]
    out = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        out.extend(Path(root) / f for f in files)
    return out


def _text(p: Path) -> str:
    # The changelog quotes headings as they were when each entry was written.
    if p.suffix not in TEXT_SUFFIXES or p.name in (Path(__file__).name, "CHANGELOG.md"):
        return ""
    try:
        return p.read_text(errors="ignore")
    except (OSError, UnicodeDecodeError):
        return ""


def test_setup_md_has_the_named_appendices():
    names, _ = headings()
    for must in ("Prerequisites", "Choose a profile", "Microsoft Graph app registration",
                 "Google OAuth app", "The EA-Managed calendar", "Zulip server", "Slack",
                 "The service account", "Services", "Cron schedule", "Hardening", "Config files",
                 "Docker", "macOS notes", "Troubleshooting and the doctor's checks",
                 "The install overlay", "The deferred-send signing key", "Phone push"):
        assert must in names, must
    text = SETUP.read_text()
    assert "## Step " not in text and "### Step " not in text, "docs/SETUP.md is reference, not steps"
    assert "READ FIRST" not in text, "the agent invariants live in the setup skill"
    assert "./install.sh" in text.split("\n## ", 1)[0], "the opening names the installer"


def test_every_pointer_into_setup_md_names_a_heading():
    names, slugs = headings()
    missing = []
    for p in tree_files():
        text = _text(p)
        if not text or "docs/SETUP.md" not in text:
            continue
        rel = p.relative_to(REPO)
        for name in quoted_pointers(text):
            if name not in names:
                missing.append(f"{rel}: docs/SETUP.md \"{name}\"")
        for slug in ANCHOR.findall(text):
            if slug not in slugs:
                missing.append(f"{rel}: docs/SETUP.md#{slug}")
    assert not missing, "\n".join(missing)


def test_setup_md_internal_anchors_resolve():
    _, slugs = headings()
    bad = [s for s in INTERNAL_ANCHOR.findall(SETUP.read_text()) if s not in slugs]
    assert not bad, bad


def test_no_step_numbers_remain_where_this_tree_points_into_setup_md():
    stale = []
    for p in tree_files():
        rel = str(p.relative_to(REPO))
        if not rel.startswith(OWNED) or rel in STEP_EXEMPT:
            continue
        text = _text(p)
        for m in STEP_NUMBER.finditer(text):
            stale.append(f"{rel}: {m.group(0)!r}")
    assert not stale, "\n".join(stale)


def test_the_skill_points_into_setup_md_by_heading_only():
    names, _ = headings()
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    pointers = quoted_pointers(skill)
    assert len(pointers) >= 10, "the skill links each step's background"
    assert all(name in names for name in pointers), [n for n in pointers if n not in names]
    assert "Read first: the invariants" in skill and "EA_ISOLATION_MODE" in skill
    assert "## 9. First week" in skill
    for must in ("onboarding.py ask --count 3", 'onboarding.py answer --id <id> --text "<their words>"',
                 "AskUserQuestion", "one more question each day for a week"):
        assert must in skill, must
    # 450: section 6 asks for the web chat first, with its host, login and serve line.
    assert len(skill.splitlines()) <= 450, "the skill stays about 400 lines"
    for text, name in ((skill, "SKILL.md"), (SETUP.read_text(), "docs/SETUP.md")):
        assert chr(0x2014) not in text, f"em-dash in {name}"


def test_the_front_docs_agree_with_the_code():
    """Platforms, defaults, the sudoers rule and the contributor steps say
    what the installers and the tests do."""
    readme = (REPO / "README.md").read_text()
    assert "(or WSL2)" not in readme and readme.count("WSL2 is untested") >= 1
    setup = SETUP.read_text()
    assert "an absent `chat` block means Slack" in setup and "means `assistant`" in setup
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert "allows exactly" not in changelog and "NOPASSWD: ALL" in changelog
    assert "ALL=(ea-bot) NOPASSWD: ALL" in (REPO / "scripts/install/setup-ea-user.sh").read_text().replace("$REAL_USER ", "")
    contributing = (REPO / "CONTRIBUTING.md").read_text()
    assert "Fork the repository" in contributing and "pytest -q tests`" not in contributing
    assert "tests/run_wall.py" in contributing and "core.hooksPath .githooks" in contributing


def test_common_failures_cover_the_first_install_snags():
    text = SETUP.read_text()
    failures = text[text.index("### Common failures"):text.index("### Python packages in three places")]
    for must in ("The Claude login fails or has expired", "setup-token", "AADSTS65001", "AADSTS90094",
                 "docs/it-request.md", "externally-managed-environment", "newer than 3.14",
                 "No sudo rights", "No Graph sign-in yet. Sign in yourself"):
        assert must in failures, must
    assert "No Graph sign-in yet. Sign in yourself" in (REPO / "scripts/install/setup.sh").read_text()


def test_connectors_are_the_claude_ai_account_ones_the_allowlists_grant():
    """The lanes grant mcp__claude_ai_* tools: claude.ai account connectors,
    enabled in claude.ai, never servers `claude mcp add` makes (whose flag
    is --transport, not --type)."""
    lanes = (REPO / "run-agent.sh").read_text() + (REPO / "ea-chat-bot.py").read_text()
    assert "mcp__claude_ai_Gmail__" in lanes and "mcp__claude_ai_Google_Calendar__" in lanes
    stale = []
    for p in tree_files():
        text = _text(p)
        if "mcp add" in text and "--type http" in text:
            stale.append(str(p.relative_to(REPO)))
        if "gmail.mcp.claude.com" in text or "gcal.mcp.claude.com" in text:
            stale.append(str(p.relative_to(REPO)))
    assert not stale, stale
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    assert "Settings > Connectors" in skill and "claude mcp list" in skill


def test_the_skill_runs_nothing_that_reads_the_terminal():
    """Claude Code's Bash tool has no terminal. The skill gives scripts/install/user_init.py
    its answers in a file, lets scripts/install/setup.sh run non-interactive, and hands the
    steps that need a password, a browser or a login to the user with the
    `!` prefix, each followed by a read-only check."""
    skill = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()
    assert "## How the commands run" in skill and "AskUserQuestion is multiple choice" in skill
    assert '"Other"' in skill
    for m in re.finditer(r"user_init\.py[^`]*", skill):
        assert "--answers" in m.group(0) or m.group(0).strip() == "user_init.py", m.group(0)
    # A `!` command has no terminal: only the Graph sign-in, which prints a
    # link and waits, is typed there; what reads the keyboard (sudo, the
    # pasted Google address, ea-bot's full-screen login) goes to the second
    # terminal.
    assert "! python3 preprocessor/graph_client.py" in skill
    assert "sudo: A terminal is required to authenticate" in skill
    assert "! sudo" not in skill and "! cd /" not in skill
    for typed in ("`sudo ./scripts/install/setup-ea-user.sh`", "`sudo ./scripts/install/setup-ea-user-macos.sh`",
                  "`python3 preprocessor/google_calendar_client.py --setup`",
                  "`sudo -u ea-bot env HOME=/home/ea-bot claude`",
                  "`sudo bash deploy/harden-permissions.sh`"):
        assert typed in skill, typed
        line_start = skill.rfind("\n\n", 0, skill.index(typed))
        assert "second" in skill[line_start:skill.index(typed)], f"{typed} is not a second-terminal step"
    assert "--install-cron" in skill
    for check in ("calendar-view --days 1", "google_calendar_client.py --test",
                  "id ea-bot && test -L ~/.ea", ".claude/.credentials.json"):
        assert check in skill, check
    # the skill lets scripts/install/setup.sh copy the templates before anything writes config
    sec1 = skill[skill.index("## 1. Profile"):skill.index("## 2. Identity")]
    assert "./scripts/install/setup.sh --profile <name>" in sec1
    assert "mkdir -p ~/.ea" not in skill


if __name__ == "__main__":
    import sys
    failed = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            try:
                v()
                print(f"PASS {k}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {k}: {e}")
    sys.exit(1 if failed else 0)
