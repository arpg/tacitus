"""The scheduling and slim reply lanes run outside the deployed tree, in a
directory of their own where Claude Code finds no memory file, so their
prompt is all the instructions they get. A session keeps the directory it
began in, and the paths the lanes' prompts use are absolute, so the move
changes nothing they call."""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import claude_cli  # noqa: E402
import slack_sessions as ss  # noqa: E402

ARGS = ["-p", "x", "--output-format", "json"]
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


class _DirectIO:
    """resident_io's calls, made directly on a scratch tree."""

    @staticmethod
    def make_dir(path, mode=0o2775):
        Path(path).mkdir(parents=True, exist_ok=True)
        return True

    @staticmethod
    def read_text(path):
        try:
            return Path(path).read_text()
        except OSError:
            return None

    @staticmethod
    def replace_text(path, text, mode=0o664):
        Path(path).write_text(text)
        return True

    @staticmethod
    def list_dir(path):
        out = []
        for e in sorted(Path(path).iterdir()):
            kind = "l" if e.is_symlink() else "d" if e.is_dir() else "f" if e.is_file() else "o"
            out.append((kind, e.name))
        return out


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An ea-bot home laid out as setup leaves it: the deployed tree with
    the core, the overlay and the project subagents, and the EA tree with
    the copy of CLAUDE.md scripts/install/setup.sh puts there."""
    home = tmp_path / "ea-bot"
    tree = home / "ea-system"
    (tree / ".claude" / "agents").mkdir(parents=True)
    (tree / "CLAUDE.md").write_text("# core\n")
    (tree / "CLAUDE.local.md").write_text("# overlay\n")
    (tree / ".claude" / "agents" / "email-author.md").write_text("---\nname: email-author\n---\n")
    (home / ".ea" / "state").mkdir(parents=True)
    (home / ".ea" / "CLAUDE.md").write_text("# copy\n")
    monkeypatch.setattr(ss, "LANE_ROOT", home / ".local" / "state" / "ea-lanes")
    monkeypatch.setattr(ss, "SESSIONS_DIR",
                        home / ".claude" / "projects" / ss.project_dir_name(str(tree)))
    monkeypatch.setattr(ss, "_rio", _DirectIO)
    return home


@pytest.mark.parametrize("lane", ss.LANES)
def test_a_lane_loads_no_memory_file(home, lane):
    d = ss.prepare_lane_dir(lane, home / "ea-system" / ".claude" / "agents")
    assert d == ss.lane_dir(lane)
    assert claude_cli.discovered_memory(ARGS, d) == frozenset()
    assert claude_cli.memory_on_path(d) == []
    # the project subagents come along, the memory files do not
    assert (d / ".claude" / "agents" / "email-author.md").is_file()
    # the main chat lane stays in the tree and loads both
    assert claude_cli.discovered_memory(ARGS, home / "ea-system") == {"CLAUDE.md", "CLAUDE.local.md"}


def test_the_ea_tree_is_no_place_for_a_lane(home):
    """Claude Code reads memory files above the working directory too:
    a directory in ~/.ea/state would pick up the EA tree's CLAUDE.md."""
    under_ea = home / ".ea" / "state" / "lane"
    under_ea.mkdir()
    assert claude_cli.discovered_memory(ARGS, under_ea) == frozenset()
    assert claude_cli.memory_on_path(under_ea) == [home / ".ea" / "CLAUDE.md"]


def test_a_memory_file_in_the_lane_sends_it_back_to_the_tree(home):
    agents = home / "ea-system" / ".claude" / "agents"
    d = ss.prepare_lane_dir("reply", agents)
    (d / "CLAUDE.md").write_text("planted\n")
    assert ss.prepare_lane_dir("reply", agents) is None
    (d / "CLAUDE.md").unlink()
    (d.parent / "CLAUDE.local.md").write_text("planted above\n")
    assert ss.prepare_lane_dir("reply", agents) is None


def test_the_subagents_are_read_with_the_residents_access(home, monkeypatch):
    """The source is in ea-bot space, which ea-bot can swap: the bot reads
    it through resident_io, never by path as the owner, and copies no link."""
    agents = home / "ea-system" / ".claude" / "agents"
    secret = home.parent / "owner-only.txt"
    secret.write_text("token\n")
    (agents / "planted.md").symlink_to(secret)
    reads = []
    monkeypatch.setattr(_DirectIO, "read_text",
                        staticmethod(lambda p: reads.append(Path(p)) or _read(p)))
    monkeypatch.setattr(Path, "read_text", _no_owner_read)
    d = ss.prepare_lane_dir("reply", agents)
    assert (d / ".claude" / "agents" / "email-author.md").is_file()
    assert not (d / ".claude" / "agents" / "planted.md").exists()
    assert agents / "email-author.md" in reads and agents / "planted.md" not in reads


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return None


def _no_owner_read(self, *a, **k):
    raise AssertionError(f"read by path as the owner: {self}")


def test_a_session_keeps_the_directory_it_began_in(home):
    assert ss.session_cwd(SID) is None
    lane = ss.lane_dir("scheduling")
    folder = ss.SESSIONS_DIR.parent / ss.project_dir_name(str(lane))
    folder.mkdir(parents=True)
    (folder / f"{SID}.jsonl").write_text("{}\n")
    assert ss.session_cwd(SID) == lane
    assert ss.session_file(SID) == folder / f"{SID}.jsonl"
    # a session in the tree's folder is the tree's, wherever else it shows
    ss.SESSIONS_DIR.mkdir(parents=True)
    (ss.SESSIONS_DIR / f"{SID}.jsonl").write_text("{}\n")
    assert ss.session_cwd(SID) is None
    assert ss.session_file(SID) == ss.SESSIONS_DIR / f"{SID}.jsonl"


def test_the_bot_runs_the_lanes_in_their_directory():
    src = (REPO / "ea-chat-bot.py").read_text()
    assert 'prompt_lane = "scheduling" if scheduling_only else "reply" if reply_only else ""' in src
    assert "prompt_cwd = _lane_cwd(prompt_lane) if prompt_lane else None" in src
    assert "text=True, cwd=str(cwd or EA_BOT_SCRIPT_DIR), env=_env," in src
    # every resume runs where its session began; every fresh run where its prompt belongs
    assert src.count('["--resume", session_id]') == src.count("cwd=_sessions.session_cwd(session_id)")
    assert src.count('["--session-id", session_id]') == src.count("cwd=prompt_cwd")


LANE_PROMPTS = ("prompts/CLAUDE-scheduling.md", "prompts/CLAUDE-scheduling-remote.md", "docs/ea/scheduling.md",
                "skills/scheduling-rules.md", "skills/reply.md", "skills/fill-poll.md")
RELATIVE = re.compile(r"(?<![/\w.~{}-])(?:preprocessor|skills|docs|scripts|config)/[\w./-]+")


@pytest.mark.parametrize("name", LANE_PROMPTS)
def test_the_lane_prompts_use_absolute_paths(name):
    bad = [line for line in (REPO / name).read_text().splitlines()
           if RELATIVE.search(line) and "(relative)" not in line]
    assert bad == []
