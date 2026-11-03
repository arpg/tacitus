"""
Custom promptfoo provider for EA system integration tests.

Wraps the full EA pipeline: briefing JSON → Claude subprocess → actions + output.
Can also run in "fast mode" (no Claude) to test just the preprocessing + prompt
construction, with an LLM-as-judge evaluating the assembled prompt.

Usage in promptfooconfig.yaml:
  providers:
    - id: python:provider.py
      config:
        mode: full          # full = invoke Claude; prompt-only = just build prompt
        model: sonnet       # Claude model to use
        skill: morning-briefing
        timeout: 300
"""

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent  # ea-system root
PREPROCESS_DIR = SCRIPT_DIR / "preprocessor"
EVAL_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(PREPROCESS_DIR))

# A full-mode run is a tool-using model running as the owner, so nothing
# it reads may come from the resident's space (docs/SECURITY.md, "Owner
# trust rules"). Its config comes from the eval's own made-up config, then
# the repository's templates, never from the live ~/.ea/config, which the
# resident writes.
EVAL_CONFIG_DIR = EVAL_DIR / "config"
TEMPLATE_CONFIG_DIR = SCRIPT_DIR / "config"
CONFIG_FILES = ("user-profile.json", "scheduling-preferences.json",
                "autonomy-tiers.json", "active-projects.md")
DENY_LIST = SCRIPT_DIR / "scripts" / "disallowed-tools.txt"
RESIDENT_USER = "ea-bot"
# Denied on top of the shared list. The file tools are already confined to
# the run's directory (--restricted); these name the resident's tree and
# the owner's secrets outright in case that confinement ever changes.
EXTRA_DENY = ("WebFetch", "WebSearch", "Read(//home/ea-bot/**)", "Read(~/.ea/**)",
              "Read(~/.ea-private/**)", "Edit(~/.ea/**)", "Write(~/.ea/**)")
EMPTY_MCP = '{"mcpServers":{}}'


# A fixture is a recorded scenario with fixed dates, but the model knows the
# real date: the prompt names the run time and the CLI's own environment
# block names today. Against April data in September it reports a stale
# snapshot instead of working the scenario (the Zoom fixture then never
# schedules anything). In full mode every date in the fixture moves by whole
# weeks, so its generated_at lands within three days of the run date and
# every weekday named in the text stays true, and the prompt's run time is
# that moved generated_at.
_ISO_DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)")
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(_MONTH_NAMES)} | {"Sept": 9}
_WORDED_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES) + r"|Sept|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(\.?)(\s+)(\d{1,2})\b(,\s*20\d\d\b)?")


def _week_shift(anchor: "datetime.date", today: "datetime.date") -> int:
    """Days to add: the whole number of weeks that brings anchor nearest
    today."""
    return 7 * round((today - anchor).days / 7)


def rebase_fixture(fixture: dict, today: "datetime.date | None" = None) -> tuple[dict, str]:
    """(the fixture with every ISO and worded date moved by _week_shift,
    the moved briefing.generated_at, or "" when the fixture has none)."""
    import datetime as _dt
    today = today or _dt.date.today()
    gen = str((fixture.get("briefing") or {}).get("generated_at") or "")
    m = _ISO_DATE_RE.match(gen)
    if not m:
        return fixture, ""
    anchor = _dt.date(*(int(x) for x in m.groups()))
    delta = _dt.timedelta(days=_week_shift(anchor, today))

    def iso(mm):
        try:
            return (_dt.date(*(int(x) for x in mm.groups())) + delta).isoformat()
        except ValueError:
            return mm.group(0)

    def worded(mm):
        name, dot, space, day, year = mm.groups()
        month = (_MONTH_NAMES.index(name) + 1 if name in _MONTH_NAMES
                 else _MONTH_ABBR[name])
        y = int(year.strip(" ,")) if year else anchor.year
        try:
            d = _dt.date(y, month, int(day)) + delta
        except ValueError:
            return mm.group(0)
        out = _MONTH_NAMES[d.month - 1] if name in _MONTH_NAMES else _MONTH_NAMES[d.month - 1][:3]
        return f"{out}{dot}{space}{d.day}" + (f", {d.year}" if year else "")

    text = _WORDED_DATE_RE.sub(worded, _ISO_DATE_RE.sub(iso, json.dumps(fixture)))
    moved = json.loads(text)
    return moved, str(moved["briefing"].get("generated_at") or "")


def config_source(name: str) -> Path | None:
    """The repository file a run's config `name` is copied from: the
    eval's own config (tests/evals/config), else the template in config/,
    else None."""
    for base in (EVAL_CONFIG_DIR, TEMPLATE_CONFIG_DIR):
        p = base / name
        if p.is_file():
            return p
    return None


def _work_tree_of(path: Path) -> Path | None:
    """The directory at or above `path` that holds a `.git` entry, else
    None."""
    for d in (path, *path.parents):
        if (d / ".git").exists():
            return d
    return None


def resident_home() -> Path | None:
    """The resident user's home, resolved; None on a host without one."""
    try:
        import pwd
        return Path(os.path.realpath(pwd.getpwnam(RESIDENT_USER).pw_dir))
    except (KeyError, ImportError, OSError):
        return None


# Where the runs' tmpdirs are made: an owner-only directory (0700) outside
# the system temp dir. Claude Code loads CLAUDE.md files from every
# directory above its working directory, and the resident can write the
# system temp dir, so a run started under it could load the resident's
# instructions. EA_EVAL_SCRATCH (the owner's environment) names another.
SCRATCH_NAME = ".ea-eval-scratch"


def scratch_root() -> Path:
    """The owner-only directory a run's tmpdir is made in: EA_EVAL_SCRATCH,
    else `.ea-eval-scratch` in the euid's passwd home. Not created here."""
    env = os.environ.get("EA_EVAL_SCRATCH", "").strip()
    if env:
        return Path(env)
    import owner_trust
    return owner_trust.owner_home() / SCRATCH_NAME


def _ancestors_owner_only(d: Path) -> str:
    """'' when no user but this one and root can write `d` or any directory
    above it (so nobody else can plant a memory file on the run's path),
    else the first directory that fails."""
    me = os.geteuid()
    for a in (d, *d.parents):
        try:
            st = os.lstat(a)
        except OSError as e:
            return f"{a}: {e.strerror}"
        if st.st_uid not in (me, 0) or st.st_mode & 0o022:
            return f"{a} is writable by another user"
    return ""


def scratch_parent() -> Path:
    """Where a run's tmpdir is made (scratch_root), created 0700, resolved.
    RuntimeError when it is a link, not this user's, below a directory
    another user can write, holds a memory file on its path, or sits in a
    git work tree, in this checkout or in the resident's home: a `claude -p`
    run's working directory decides which memory files and project settings
    it picks up."""
    import claude_cli
    root = scratch_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(root)
    except OSError as e:
        raise RuntimeError(f"cannot make the scratch dir {root}: {e}") from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"the scratch dir {root} is not a plain directory")
    if st.st_uid != os.geteuid():
        raise RuntimeError(f"the scratch dir {root} is not this user's")
    if st.st_mode & 0o077:
        os.chmod(root, 0o700)
    d = Path(os.path.realpath(root))
    tree = _work_tree_of(d)
    if tree is not None:
        raise RuntimeError(f"the scratch dir {d} is in the work tree {tree}")
    if d == SCRIPT_DIR or SCRIPT_DIR in d.parents:
        raise RuntimeError(f"the scratch dir {d} is in the checkout {SCRIPT_DIR}")
    home = resident_home()
    if home is not None and (d == home or home in d.parents):
        raise RuntimeError(f"the scratch dir {d} is in the resident's home {home}")
    why = _ancestors_owner_only(d)
    if why:
        raise RuntimeError(f"the scratch dir {d} is not owner-only: {why}")
    found = claude_cli.memory_on_path(d)
    if found:
        raise RuntimeError(f"a memory file is on the scratch dir's path: {found[0]}")
    return d


def run_memory_error(tmpdir: Path) -> str:
    """'' when the only memory file a run in `tmpdir` can load is the
    repository CLAUDE.md _setup_test_env put there, else why not. Checked
    right before the model starts."""
    import claude_cli
    tmpdir = Path(tmpdir)
    above = claude_cli.memory_on_path(tmpdir.parent)
    if above:
        return f"a memory file is above the run's directory: {above[0]}"
    own = [p for p in claude_cli.memory_on_path(tmpdir) if p != tmpdir / "CLAUDE.md"]
    if own:
        return f"an unexpected memory file is in the run's directory: {own[0]}"
    return ""


def resident_space_readable() -> bool:
    """True when this process can list the resident's home: a model shell
    started here could then read what the resident wrote."""
    home = resident_home()
    return bool(home) and os.access(home, os.R_OK | os.X_OK)


def _setup_test_env(fixture: dict, tmpdir: Path) -> dict:
    """Create an isolated test environment from a fixture.

    Returns a dict with paths to all created files.
    """
    state_dir = tmpdir / "state"
    config_dir = tmpdir / "config"
    state_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    # Write briefing JSON
    briefing_path = state_dir / "briefing-latest.json"
    briefing_path.write_text(json.dumps(fixture.get("briefing", {}), indent=2))

    # Create test ledger DB
    ledger_db = state_dir / "obligations-ledger.db"
    _create_test_ledger(ledger_db, fixture.get("ledger_entries", []))

    # Config from the repository (config_source), never from ~/.ea.
    for cfg in CONFIG_FILES:
        src = config_source(cfg)
        if src is not None:
            shutil.copyfile(src, config_dir / cfg)

    # The run starts in tmpdir (_invoke_claude): the product core sits
    # there for Claude Code to load, as it does in a lane's tree, and
    # `preprocessor` names this checkout's code, so the skill's relative
    # `python3 preprocessor/ledger.py` runs.
    shutil.copyfile(SCRIPT_DIR / "CLAUDE.md", tmpdir / "CLAUDE.md")
    os.symlink(PREPROCESS_DIR, tmpdir / "preprocessor")

    return {
        "briefing": str(briefing_path),
        "ledger_db": str(ledger_db),
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
        "tmpdir": str(tmpdir),
    }


def _create_test_ledger(db_path: Path, entries: list):
    """Create a test SQLite ledger with the given entries."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            description     TEXT,
            from_email      TEXT,
            from_name       TEXT,
            source_message_id TEXT,
            date_identified TEXT,
            deadline        TEXT,
            follow_up_after TEXT,
            snooze_until    TEXT,
            last_action     TEXT,
            notes           TEXT,
            priority        TEXT,
            metadata        TEXT DEFAULT '{}',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            closed_at       TEXT,
            conversation_id TEXT,
            email_archived  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON ledger(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deadline ON ledger(deadline)")

    for entry in entries:
        cols = list(entry.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        conn.execute(
            f"INSERT OR REPLACE INTO ledger ({col_names}) VALUES ({placeholders})",
            [entry[c] for c in cols],
        )
    conn.commit()
    conn.close()


# Substrings that mean the `claude` CLI never reached the model: quota
# exhaustion, auth failure, transport failure. These arrive on stdout as
# PLAIN TEXT, not as the `--output-format json` document, so a bare
# `except json.JSONDecodeError` would hand them to the judges as if the
# model had said them. See _harness_error_for().
_CLI_FAILURE_MARKERS = (
    "hit your weekly limit",
    "hit your usage limit",
    "hit your limit",
    "rate limit",
    "usage limit reached",
    "credit balance is too low",
    "invalid api key",
    "authentication_error",
    "please run /login",
    "oauth token has expired",
    "overloaded_error",
    "internal server error",
)


def _harness_error_for(stdout: str, stderr: str, returncode: int,
                       parsed: dict | None) -> str:
    """Classify a `claude -p` invocation as a harness failure, or return "".

    A harness failure means the suite could not be MEASURED (the CLI refused,
    timed out, or produced something that is not its JSON envelope). It is
    categorically different from "the model answered and the answer was wrong",
    and the two must not be reported the same way.
    """
    head = (stdout or "").strip()[:400].lower()
    err = (stderr or "").strip()[:400].lower()

    if parsed is None:
        # `--output-format json` guarantees a JSON document on success. Anything
        # else on stdout is the CLI talking, not the model.
        detail = head or err or f"exit {returncode}, no output"
        return f"claude CLI produced no JSON envelope: {detail[:200]}"

    if parsed.get("is_error") is True or parsed.get("subtype") not in (
            None, "success"):
        detail = (parsed.get("result") or parsed.get("error")
                  or parsed.get("subtype") or f"exit {returncode}")
        return f"claude CLI reported an error: {str(detail)[:200]}"

    for marker in _CLI_FAILURE_MARKERS:
        if marker in head or marker in err:
            return f"claude CLI failure message: {(head or err)[:200]}"

    if returncode != 0:
        return f"claude exited {returncode}: {(err or head)[:200]}"

    return ""


def _run_time(run_at: str) -> str:
    """The prompt's run time: the fixture's moved generated_at, else now."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        return t.strftime("%Y-%m-%d %H:%M:%S ") + time.strftime("%Z")
    except ValueError:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z")


# The production state files an eval must never write: the live bot turns
# each into a real Create / Reschedule / Delete / Schedule card.
PRODUCTION_STATE = Path(os.path.expanduser("~/.ea/state"))
STAGED_FILES = ("pending-event.json", "pending-event-update.json",
                "pending-event-delete.json", "pending-schedule-send.json")


def _staging_note(env_paths: dict) -> str:
    """The prompt lines for a fixture that stages calendar invites."""
    return f"""
This test run may stage calendar invites. Write a staged event to
{env_paths['state_dir']}/pending-event.json (this run's own state directory),
never to ~/.ea/state. validate-draft and the scheduling engine are not
available here: confirm a named time is free against availability_merged in
the briefing JSON.
"""


def _production_staged() -> dict:
    """{name: mtime} of the staged files in production state."""
    out = {}
    for name in STAGED_FILES:
        try:
            out[name] = (PRODUCTION_STATE / name).stat().st_mtime
        except OSError:
            pass
    return out


def _read_staged_event(env_paths: dict):
    """The event the run staged in its own state dir: the parsed JSON (an
    object or a list), {"_raw": text} when it does not parse, else None."""
    path = Path(env_paths["state_dir"]) / "pending-event.json"
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text[:2000]}


def _production_leaks(before: dict, marker: str) -> list[str]:
    """Staged files in production state that this run wrote: new or
    changed since `before` AND naming the fixture's own marker (its made-up
    attendee), so a real staging by a live EA run at the same moment is
    never touched. Each leak is removed before the live bot turns it into a
    card, and named in the result."""
    leaks = []
    if not marker:
        return leaks
    for name, mtime in _production_staged().items():
        if before.get(name) == mtime:
            continue
        path = PRODUCTION_STATE / name
        try:
            if marker.lower() in path.read_text().lower():
                path.unlink()
                leaks.append(name)
        except OSError:
            pass
    return leaks


def deny_list() -> list[str]:
    """The shared deny list (scripts/disallowed-tools.txt), then EXTRA_DENY."""
    rows = [line.strip() for line in DENY_LIST.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    return rows + [r for r in EXTRA_DENY if r not in rows]


# The ledger verbs a full-mode run may call: the reads, and the writes the
# briefing skill makes into the run's own ledger (add, update, propose).
# Not close, confirm or reject (a close archives mail through Graph, with the
# owner's token here), and not repair-ids, normalize, import-json or any verb
# that takes a database path.
EVAL_LEDGER_VERBS = ("list", "get", "search", "stats", "types", "proposals",
                     "add", "update", "propose")


def eval_tools(env_paths: dict, stages_events: bool = False) -> tuple[str, str]:
    """(--tools, --allowedTools) for a full-mode run: Read, and Bash for
    the ledger CLI's EVAL_LEDGER_VERBS only; Write and Edit on this run's
    own pending-event.json when the fixture stages an event.

    Both spellings of the ledger call are allowed: the briefing skill
    writes it relative ("python3 preprocessor/ledger.py list --status
    open"), which runs through the tmpdir's `preprocessor` link."""
    tools = ["Bash", "Read"]
    allowed = []
    for verb in EVAL_LEDGER_VERBS:
        for exe in (f"{PREPROCESS_DIR}/ledger.py", "preprocessor/ledger.py"):
            allowed += [f"Bash(python3 {exe} {verb})", f"Bash(python3 {exe} {verb} *)"]
    allowed.append("Read")
    if stages_events:
        tools += ["Write", "Edit"]
        # One file, in this run's state dir ("//" marks an absolute path).
        staged = f"/{env_paths['state_dir']}/pending-event.json"
        allowed += [f"Write({staged})", f"Edit({staged})"]
    return ",".join(tools), ",".join(allowed)


def claude_command(prompt: str, model: str, tools: str, allowed_tools: str,
                   claude_bin: str = "claude") -> list[str]:
    """The `claude -p` command line of a full-mode run, which runs as the
    owner. It loads no settings file (the owner's user and project
    settings pre-approve broad Bash), no MCP server, and offers only
    `tools`. --restricted confines the file tools to the working directory
    (the run's tmpdir); claude_cli.apply adds --permission-mode dontAsk,
    which refuses every call the allow list does not name, and the shared
    deny list applies on top.
    The caller starts it in the run's tmpdir (scratch_parent)."""
    import claude_cli
    args = ["-p", prompt, "--model", model, "--output-format", "json",
            "--restricted", "--setting-sources", "",
            "--tools", tools, "--allowedTools", allowed_tools,
            "--disallowedTools", ",".join(deny_list()),
            "--permission-prompts", "none",
            "--mcp-config", EMPTY_MCP, "--strict-mcp-config",
            "--no-session-persistence"]
    return [claude_bin] + claude_cli.apply(args, claude_bin=claude_bin)


def _invoke_claude(env_paths: dict, skill: str, model: str,
                   timeout: int, run_at: str = "", stages_events: bool = False) -> dict:
    """Invoke Claude subprocess with the test environment.

    Returns dict with: output (text), tool_calls (parsed), exit_code, duration,
    and harness_error ("" when the invocation itself was healthy).
    """
    skill_file = SCRIPT_DIR / "skills" / f"{skill}.md"
    if not skill_file.exists():
        return {"output": "", "harness_error": f"Skill file not found: {skill_file}",
                "tool_calls": [], "exit_code": 1, "duration_s": 0}

    # The framing is shared with the Codex run (_claude_prompt); it is
    # copied from run-agent.sh's PROMPT so the eval measures the same
    # contract production states. Keep _claude_prompt in sync with it.
    prompt = _claude_prompt(env_paths, skill, skill_file.read_text(), run_at, stages_events)

    tools, allowed_tools = eval_tools(env_paths, stages_events)
    cmd = claude_command(prompt, model, tools, allowed_tools)

    # EA_DIR points ledger.py (its DB_PATH) at the throwaway tmpdir, so every
    # ledger call in the run reads and writes the fixture's sandbox copy and
    # never the real obligations ledger. ledger.py takes no database flag on
    # its everyday verbs, so this variable is the whole routing; a prompt
    # that asked for one made every ledger read a usage error.
    env = dict(os.environ, EA_DIR=env_paths["tmpdir"])

    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=env_paths["tmpdir"], env=env, stdin=subprocess.DEVNULL,
        )
        duration = time.time() - start

        # Parse JSON output for tool calls
        tool_calls = []
        output_text = ""
        parsed = None
        try:
            data = json.loads(result.stdout)
            if isinstance(data, list):
                parsed = {"subtype": "success"}
                for item in data:
                    if item.get("type") == "tool_use":
                        tool_calls.append({
                            "tool": item.get("name", ""),
                            "input": item.get("input", {}),
                        })
                    elif item.get("type") == "text":
                        output_text += item.get("text", "")
            elif isinstance(data, dict):
                parsed = data
                output_text = data.get("result", data.get("text", "")) or ""
        except json.JSONDecodeError:
            parsed = None

        harness_error = _harness_error_for(
            result.stdout, result.stderr, result.returncode, parsed)

        return {
            "output": "" if harness_error else output_text,
            "tool_calls": tool_calls,
            "exit_code": result.returncode,
            "duration_s": duration,
            "harness_error": harness_error,
            "permission_denials": (parsed or {}).get("permission_denials", []),
            "num_turns": (parsed or {}).get("num_turns"),
            "stderr": result.stderr[-500:] if result.stderr else "",
        }

    except subprocess.TimeoutExpired:
        # A timeout is a harness condition, not a fixture verdict: the
        # model never got to finish, so every assertion below would be
        # scoring an empty string, and the data-only ones would "pass".
        return {
            "output": "",
            "harness_error": f"claude timed out after {timeout}s",
            "tool_calls": [],
            "exit_code": -1,
            "duration_s": timeout,
        }


def _invoke_codex(env_paths: dict, skill: str, model: str,
                  timeout: int, run_at: str = "", stages_events: bool = False) -> dict:
    """The same run on Codex (`codex exec`, preprocessor/codex_cli.py).

    Codex loads no CLAUDE.md, so the prompt carries the core operating
    instructions (prompt_logic.build_claude_md with nothing discovered)
    before the same framing _invoke_claude sends. The sandbox is
    workspace-write on the run's throwaway EA_DIR, so the ledger and a
    staged event land there and nowhere else; the code tree is readable
    and not writable; there is no tool allow list, the sandbox is the
    boundary. `model` is a Codex model name ("" keeps the local default).

    Codex's sandbox confines writes, not reads: a model shell it starts
    reads anything this user can. So the run is refused, as a harness
    error, where this process can list the resident's home: there the
    model, running as the owner, could read what the resident wrote."""
    if resident_space_readable():
        return {"output": "", "tool_calls": [], "exit_code": 1, "duration_s": 0,
                "harness_error": (f"codex harness refused: this user can read "
                                  f"{resident_home()}, and codex's sandbox does not "
                                  f"confine reads (run it where the resident's space "
                                  f"is unreadable)")}
    sys.path.insert(0, str(PREPROCESS_DIR))
    import codex_cli
    import prompt_logic
    skill_file = SCRIPT_DIR / "skills" / f"{skill}.md"
    if not skill_file.exists():
        return {"output": "", "harness_error": f"Skill file not found: {skill_file}",
                "tool_calls": [], "exit_code": 1, "duration_s": 0}
    try:
        core = prompt_logic.build_claude_md(set(), (SCRIPT_DIR / "CLAUDE.md").read_text(),
                                            overlay="", discovered=())
    except Exception as e:  # a missing core is still a measurable run
        core = f"(operating instructions unavailable: {e})"
    framing = _claude_prompt(env_paths, skill, skill_file.read_text(), run_at, stages_events)
    prompt = ("# Operating instructions (CLAUDE.md)\n\n" + core.strip()
              + "\n\n# This run\n\n" + framing
              + "\n\nYou are Codex, not Claude: the rules above about Claude Code tools "
                "(Read, Bash allow lists, MCP) mean the same for your shell: run only the "
                "ledger commands named, read only the files named, write nothing but the "
                "test ledger. Your final message is the briefing.")
    env = dict(os.environ, EA_DIR=env_paths["tmpdir"])
    # The run's temp dir is its own, never /tmp (codex_cli.temp_flags).
    tmp_dir = Path(env_paths["tmpdir"]) / "tmp"
    tmp_dir.mkdir(mode=0o700, exist_ok=True)
    res = codex_cli.run(prompt, cwd=env_paths["tmpdir"], model=model, sandbox="workspace-write",
                        timeout=timeout, env=env, tmp_dir=tmp_dir)
    res.setdefault("permission_denials", [])
    return res


def _claude_prompt(env_paths: dict, skill: str, skill_content: str, run_at: str,
                   stages_events: bool) -> str:
    """The run's framing, shared by both harnesses (see _invoke_claude)."""
    return f"""You are running as the EA agent '{skill}' at {_run_time(run_at)}.

Execute the following skill instructions:

{skill_content}

The preprocessed briefing is at: {env_paths['briefing']}
The CLAUDE.md with full operating instructions is at: {env_paths['tmpdir']}/CLAUDE.md
This run's config files (user-profile.json, scheduling-preferences.json,
autonomy-tiers.json, active-projects.md) are in {env_paths['config_dir']}.
Where the instructions name ~/.ea or /home/ea-bot/.ea, this run uses
{env_paths['tmpdir']}; nothing outside it is readable here.

CRITICAL: This is a TEST RUN. Do NOT call any external APIs (Graph, Gmail, Calendar).
Do NOT create real drafts or send real emails. Do NOT modify the production ledger.
Only use Read to read the briefing JSON and config files.
Only use Bash to run: python3 {PREPROCESS_DIR}/ledger.py (list, get, search, add, update, propose).
Every ledger command in this run already reads and writes the test ledger at
{env_paths['ledger_db']}; do not pass a database path, the commands take none.
A ledger row's draft id is in its notes, last_action and metadata: run
`ledger.py get --id <id>` before you say a draft does not exist.
{_staging_note(env_paths) if stages_events else ""}
CRITICAL OUTPUT RULE: claude -p only captures text from the FINAL assistant turn.
Any text you write BEFORE a tool call is SILENTLY DROPPED and the user sees nothing.
Therefore: do ALL tool calls (reading files, updating state, writing ledger) FIRST,
then write the briefing/update text as your FINAL output with NO tool calls after it.

MANDATORY FORMAT: For morning briefings, your FIRST line of text MUST be:
  :sunny: *Morning Briefing — [date]*
For afternoon catchups, your FIRST line of text MUST be:
  :coffee: *Afternoon Update — [date]*

FORBIDDEN PATTERNS — these are meta-commentary and MUST NEVER appear in your output:
- 'State updated' / 'state files updated'
- 'briefing posted' / 'posted to Slack'
- 'here is a summary' / 'here is what was covered'
- 'Morning briefing complete'
- Any text describing what you did rather than being the briefing itself

WORKFLOW ORDER:
1. Read all data (briefing JSON, config, ledger) — tool calls
2. Write the briefing text — your FINAL output, NO tool calls after this

Do NOT use Slack MCP tools; your text output IS the briefing.
Use Slack emoji shortcodes (:sunny:, :email:) not unicode emoji.
Use *single asterisks* for bold (Slack mrkdwn), NOT **double**.

Begin now."""


def _build_prompt_only(env_paths: dict, skill: str) -> dict:
    """Build the full prompt without invoking Claude.

    Returns the assembled prompt for LLM-as-judge evaluation.
    """
    skill_file = SCRIPT_DIR / "skills" / f"{skill}.md"
    skill_content = skill_file.read_text() if skill_file.exists() else ""

    briefing = json.loads(Path(env_paths["briefing"]).read_text())

    # Query the test ledger directly (ledger.py CLI uses production DB)
    conn = sqlite3.connect(env_paths["ledger_db"])
    conn.row_factory = sqlite3.Row
    open_rows = conn.execute(
        "SELECT * FROM ledger WHERE status NOT IN ('completed', 'closed') "
        "ORDER BY date_identified DESC"
    ).fetchall()
    conn.close()
    open_entries = [dict(r) for r in open_rows]

    return {
        "output": json.dumps({
            "skill_instructions": skill_content,
            "briefing_data": briefing,
            "open_obligations": {"count": len(open_entries),
                                 "entries": open_entries},
        }, indent=2),
        "tool_calls": [],
        "exit_code": 0,
        "duration_s": 0,
    }


def _read_ledger_state(db_path: str) -> list:
    """Read all ledger entries after a test run."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ledger ORDER BY date_identified").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """Promptfoo provider entrypoint.

    Args:
        prompt: The rendered prompt (from vars substitution).
        options: Provider config from promptfooconfig.yaml.
        context: Test context including vars.

    Returns:
        dict with 'output' and optionally 'metadata'.
    """
    config = options.get("config", {})
    mode = config.get("mode", "prompt-only")
    model = config.get("model", "sonnet")
    skill = config.get("skill", "morning-briefing")
    timeout = config.get("timeout", 300)
    harness = config.get("harness", "claude")
    if harness not in ("claude", "codex"):
        return {"output": "", "metadata": {"harness_error": f"unknown harness {harness!r}"}}

    # Load fixture from vars
    fixture_path = context.get("vars", {}).get("fixture_path", "")
    if fixture_path and not os.path.isabs(fixture_path):
        fixture_path = os.path.join(os.path.dirname(__file__), fixture_path)

    if not fixture_path or not os.path.exists(fixture_path):
        return {"output": "",
                "metadata": {"harness_error":
                             f"fixture not found: {fixture_path}"}}

    with open(fixture_path) as f:
        fixture = json.load(f)
    run_at = ""
    if mode == "full":
        fixture, run_at = rebase_fixture(fixture)
    meta = fixture.get("_meta") or {}
    stages_events = mode == "full" and bool(meta.get("stages_events"))

    # Set up isolated test environment, outside any checkout
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="ea-eval-", dir=scratch_parent()))
    except (RuntimeError, OSError) as e:
        return {"output": "", "metadata": {"harness_error": f"no scratch dir: {e}"}}
    try:
        env_paths = _setup_test_env(fixture, tmpdir)
        memory_error = run_memory_error(tmpdir) if mode == "full" else ""
        if memory_error:
            return {"output": "", "metadata": {"harness_error": memory_error}}

        staged_before = _production_staged() if stages_events else {}
        if mode == "full" and harness == "codex":
            result = _invoke_codex(env_paths, skill, model, timeout, run_at,
                                   stages_events=stages_events)
        elif mode == "full":
            result = _invoke_claude(env_paths, skill, model, timeout, run_at,
                                    stages_events=stages_events)
        else:
            result = _build_prompt_only(env_paths, skill)
        leaks = (_production_leaks(staged_before, meta.get("expected_attendee", ""))
                 if stages_events else [])

        # Read final ledger state
        ledger_after = _read_ledger_state(env_paths["ledger_db"])

        # Build metadata for assertions
        metadata = {
            "tool_calls": result.get("tool_calls", []),
            "ledger_after": ledger_after,
            "ledger_changes": _diff_ledger(
                fixture.get("ledger_entries", []), ledger_after),
            "duration_s": result.get("duration_s", 0),
            "exit_code": result.get("exit_code", 0),
            "harness_error": result.get("harness_error", ""),
            "permission_denials": result.get("permission_denials", []),
            "num_turns": result.get("num_turns"),
            "fixture_name": os.path.basename(fixture_path),
            "harness": harness,
            "stages_events": stages_events,
            "staged_event": _read_staged_event(env_paths) if stages_events else None,
            "production_state_writes": leaks,
        }

        return {
            "output": result.get("output", ""),
            "metadata": metadata,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _diff_ledger(before: list, after: list) -> dict:
    """Compute ledger changes between fixture input and post-run state."""
    before_map = {e.get("id", ""): e for e in before}
    after_map = {e.get("id", ""): e for e in after}

    added = [e for eid, e in after_map.items() if eid not in before_map]
    removed = [e for eid, e in before_map.items() if eid not in after_map]
    changed = []
    for eid, after_entry in after_map.items():
        if eid in before_map:
            before_entry = before_map[eid]
            diffs = {k: {"before": before_entry.get(k), "after": v}
                     for k, v in after_entry.items()
                     if before_entry.get(k) != v}
            if diffs:
                changed.append({"id": eid, "changes": diffs})

    return {"added": added, "removed": removed, "changed": changed}
