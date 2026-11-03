"""codex_turn.run_turn: one chat turn on Codex as the resident, live or as
the ghost. A fake codex run stands in; the thread registry lives in a
temporary owner-private dir; the scratch setup and the staged-file
cleanup are fakes too."""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
import codex_ghost as cg  # noqa: E402
import codex_turn as ct  # noqa: E402
import remote_llm  # noqa: E402

_REAL_ENSURE_LIVE_CWD = ct._ensure_live_cwd
_REAL_RESIDENT_TEXT = ct._resident_text
_REAL_CONTEXT_FILES = ct.context_files
_REAL_LIVE_WRITE_PATHS = ct.live_write_paths
_REAL_APPLY_STAGED = ct._apply_staged


@pytest.fixture
def private(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    cfg = priv / remote_llm.CONFIG_FILE
    cfg.write_text(json.dumps({"endpoint": "https://models.example.net/v1", "model": "m"}))
    os.chmod(cfg, 0o600)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    (tmp_path / "ea").mkdir()
    return priv


MEMORY_INDEX = "# Memory index\n\n- [A tool quirk](tool-quirk.md): what the verb really returns"
FAKE_WRITES = [ct.REAL_EA_DIR / "state", ct.REAL_EA_DIR / "docs"]


@pytest.fixture(autouse=True)
def _no_resident(monkeypatch):
    """The resident's files, the live working directory and the write list
    are fakes: no test reads the real ones or runs sudo. The deployed tree
    carries CLAUDE.md, CLAUDE.local.md and its memory index; any other
    directory (a lane directory) carries nothing."""
    files = {ct.BOT_TREE / "CLAUDE.md": "CORE RULES", ct.BOT_TREE / "CLAUDE.local.md": "INSTALL FACTS",
             ct.memory_dir(ct.BOT_TREE) / "MEMORY.md": MEMORY_INDEX}
    monkeypatch.setattr(ct, "_resident_text", lambda path: files.get(Path(path), ""))
    monkeypatch.setattr(ct, "context_files",
                        lambda cwd: [f for f in (Path(cwd) / "CLAUDE.md", Path(cwd) / "CLAUDE.local.md") if f in files])
    monkeypatch.setattr(ct, "live_write_paths", lambda: list(FAKE_WRITES))
    made = []
    monkeypatch.setattr(ct, "_ensure_live_cwd", lambda runner=None: made.append(ct.LIVE_CWD))
    monkeypatch.setattr(ct, "_apply_staged", lambda text, runner=None: {})
    return made


class _Ok:
    returncode = 0
    stderr = ""


def _scratch(cmd, inp):
    return _Ok()


def _codex(answers):
    """A fake codex_cli.run that pops one answer per call and records the call."""
    calls = []

    def run(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        a = answers.pop(0)
        return {"output": a.get("output", ""), "tool_calls": a.get("tool_calls", []), "usage": {"input_tokens": 5},
                "duration_s": 2.5, "exit_code": a.get("exit_code", 0), "harness_error": a.get("harness_error", ""),
                "files": [], "thread_id": a.get("thread_id", ""), "num_turns": 1}
    run.calls = calls
    return run


def test_a_live_turn_runs_on_the_real_ea_dir_with_writes_confined_and_keeps_its_thread(private):
    run = _codex([{"output": "Done.", "thread_id": "th-1",
                   "tool_calls": [{"input": {"command": "python3 x ledger.py list"}, "exit_code": 0, "output": "rows"}]}])
    rec = ct.run_turn("CONTINUATION", thread_key="t1", run_id="run-abc", live=True, fresh_prompt="FRESH", lane="main",
                      codex_run=run)
    call = run.calls[0]
    assert call["prompt"].startswith(f"=== INSTRUCTIONS ({ct.BOT_TREE / 'CLAUDE.md'}) ===\nCORE RULES"), \
        "a Codex turn starts with what a Claude run in the deployed tree loads by itself"
    assert call["prompt"].index("INSTALL FACTS") < call["prompt"].index("=== YOUR MEMORY ===") < call["prompt"].index("FRESH")
    assert "\n\nFRESH" in call["prompt"], "then the bot's fresh prompt, which carries the thread's history"
    assert "You are Codex, not Claude" in call["prompt"] and "waits for the user's button" in call["prompt"]
    assert call["cwd"] == ct.LIVE_CWD and call["sandbox"] == "workspace-write" and call["ephemeral"] is False
    assert call["resume"] == "" and call["add_dirs"] == FAKE_WRITES
    env = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in call["prefix"][5:]}
    assert env["EA_RUN_ID"] == "run-abc" and env["EA_DIR"] == str(ct.REAL_EA_DIR)
    assert rec["output"] == "Done." and rec["mode"] == "live" and rec["thread_id"] == "th-1" and rec["staged"] == {}
    assert rec["tool_calls"][0][0] == "Bash" and rec["tool_calls"][0][4] == {"command": "python3 x ledger.py list"}
    assert rec["returncode"] == 0 and rec["duration_ms"] == 2500
    assert ct.thread_row("t1", "live")["thread_id"] == "th-1" and ct.thread_row("t1", "ghost") == {}
    # the next turn is a new Codex thread too: a resumed one loses its writable roots
    run2 = _codex([{"output": "Again.", "thread_id": "th-2"}])
    ct.run_turn("CONTINUATION", thread_key="t1", run_id="run-def", live=True, fresh_prompt="FRESH", codex_run=run2)
    assert run2.calls[0]["resume"] == "" and run2.calls[0]["prompt"].startswith("=== INSTRUCTIONS")
    assert "\n\nFRESH" in run2.calls[0]["prompt"] and run2.calls[0]["add_dirs"] == FAKE_WRITES
    assert ct.thread_row("t1", "live")["turns"] == 2 and ct.thread_row("t1", "live")["thread_id"] == "th-2"


def test_a_registered_thread_is_never_resumed(private):
    """`codex exec resume` drops the writable roots: a resumed turn's
    sandbox policy has none, so it could not write the ledger, a draft or
    a mail request. Every turn is a new thread on the fresh prompt."""
    ct.remember_thread("t2", "live", "old", ct.BOT_HOME)
    run = _codex([{"output": "Fresh start.", "thread_id": "th-new"}])
    rec = ct.run_turn("CONT", thread_key="t2", run_id="r", live=True, fresh_prompt="FRESH", codex_run=run)
    assert len(run.calls) == 1 and run.calls[0]["resume"] == "" and "\n\nFRESH" in run.calls[0]["prompt"]
    assert rec["output"] == "Fresh start." and ct.thread_row("t2", "live")["thread_id"] == "th-new"
    run2 = _codex([{"output": "ok", "thread_id": "th-3"}])
    ct.run_turn("ONLY", thread_key="t2", run_id="r2", live=True, codex_run=run2)
    assert "\n\nONLY" in run2.calls[0]["prompt"], "with no fresh prompt the bot's prompt stands in"


def test_a_harness_error_keeps_the_shape_and_forgets_nothing(private):
    run = _codex([{"output": "", "harness_error": "codex timed out after 600s", "exit_code": -1}])
    rec = ct.run_turn("P", thread_key="t3", run_id="r", live=True, codex_run=run)
    assert rec["harness_error"].startswith("codex timed out") and rec["returncode"] == -1 and rec["output"] == ""
    assert ct.thread_row("t3", "live") == {}


def test_a_ghost_turn_runs_in_a_scratch_root_with_the_prompt_paths_rewritten(private, tmp_path, monkeypatch):
    cleared = []
    monkeypatch.setattr(ct, "clear_staged", lambda root, runner=None: cleared.append(root))
    root_holder = {}

    def fake_prepare(jid, runner=None, **kw):
        root = tmp_path / "scratch" / jid / "ea"
        (root / "state").mkdir(parents=True)
        (root / "state" / "pending-event.json").write_text(json.dumps({"subject": "Sync", "start": "2026-11-06T09:00"}))
        root_holder["root"] = root
        return root
    monkeypatch.setattr(cg, "prepare_scratch", fake_prepare)
    real = str(ct.REAL_EA_DIR)
    prompt = f"Write {real}/state/pending-event.json and run ledger.py; the ledger is at {real}/state."
    run = _codex([{"output": "Staged the invite.", "thread_id": "gh-1"}])
    rid = cg.run_id_for("chat-x")
    rec = ct.run_turn(prompt, thread_key="t4", run_id=rid, live=False, codex_run=run, scratch_runner=_scratch)
    call = run.calls[0]
    root = root_holder["root"]
    assert real not in call["prompt"].split("# You are Codex")[0], "the real EA_DIR never appears in a ghost's task"
    assert str(root) in call["prompt"] and call["cwd"] == root
    assert call["add_dirs"] == [ct.REAL_EA_DIR / "state" / "mail-queue"], "the mediator's queue is the one real writable path"
    env = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in call["prefix"][5:]}
    assert env["EA_DIR"] == str(root) and env["EA_RUN_ID"] == rid and cg.is_ghost_run(env["EA_RUN_ID"])
    assert rec["mode"] == "ghost" and rec["staged"]["pending-event.json"]["subject"] == "Sync"
    assert cleared == [root], "a ghost's staged files are cleared for the next turn"
    assert ct.thread_row("t4", "ghost") == {"thread_id": "gh-1", "root": str(root),
                                            "started": ct.thread_row("t4", "ghost")["started"],
                                            "turns": 1, "last": ct.thread_row("t4", "ghost")["last"]}
    # the second ghost turn reuses the scratch root, on a new Codex thread
    monkeypatch.setattr(cg, "prepare_scratch", lambda *a, **k: pytest.fail("the scratch is reused"))
    run2 = _codex([{"output": "Again.", "thread_id": "gh-1"}])
    ct.run_turn("CONT", thread_key="t4", run_id=cg.run_id_for("chat-y"), live=False, codex_run=run2)
    assert run2.calls[0]["resume"] == "" and run2.calls[0]["cwd"] == root


def test_a_ghost_turn_without_persist_is_a_new_thread_on_a_scratch_removed_after(private, tmp_path, monkeypatch):
    """A resumed ghost thread carried the stub answers of earlier turns
    into later ones (it learned an outage that never happened): the ghost
    arm runs each replay on a new thread and a new scratch."""
    removed = []
    monkeypatch.setattr(cg, "remove_scratch", lambda jid, runner=None: removed.append(jid))

    def fake_prepare(jid, runner=None, **kw):
        root = tmp_path / "scratch" / jid / "ea"
        (root / "state").mkdir(parents=True)
        (root / "state" / "pending-event.json").write_text(json.dumps({"subject": "Sync"}))
        return root
    monkeypatch.setattr(cg, "prepare_scratch", fake_prepare)
    ct.remember_thread("chat-j1", "ghost", "old-thread", Path("/old"))
    run = _codex([{"output": "Done.", "thread_id": "gh-new"}])
    rec = ct.run_turn("FRESH", thread_key="chat-j1", run_id=cg.run_id_for("chat-j1"), live=False,
                      fresh_prompt="FRESH", codex_run=run, scratch_runner=_scratch, persist=False)
    assert run.calls[0]["resume"] == "" and str(run.calls[0]["cwd"]).endswith(ct.ghost_scratch_id("chat-j1") + "/ea")
    assert rec["staged"] == {"pending-event.json": {"subject": "Sync"}}, "staged files are read before the scratch goes"
    assert removed == [ct.ghost_scratch_id("chat-j1")]
    assert ct.thread_row("chat-j1", "ghost")["thread_id"] == "old-thread", "the registry is neither read nor written"
    run2 = _codex([{"output": "Live.", "thread_id": "L1"}])
    ct.run_turn("P", thread_key="t-live", run_id="r", live=True, codex_run=run2, persist=False)
    assert ct.thread_row("t-live", "live")["thread_id"] == "L1", "production threads always persist"


def test_live_and_ghost_threads_never_share_a_row(private):
    ct.remember_thread("t5", "live", "L", ct.BOT_HOME)
    ct.remember_thread("t5", "ghost", "G", Path("/x"))
    assert ct.thread_row("t5", "live")["thread_id"] == "L" and ct.thread_row("t5", "ghost")["thread_id"] == "G"
    ct.forget_thread("t5", "ghost")
    assert ct.thread_row("t5", "ghost") == {} and ct.thread_row("t5", "live")["thread_id"] == "L"


def test_the_ghost_runner_takes_the_production_path_for_a_chat_job_with_a_prompt(private, monkeypatch):
    seen = {}

    def fake_run_turn(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return {"output": "ghost reply", "tool_calls": [("Bash", 0.0, "cmd", "", {"command": "cmd"})],
                "returncode": 0, "num_turns": 1, "duration_ms": 1500, "usage": {"input_tokens": 9},
                "harness_error": "", "warnings": [], "thread_id": "gh-9", "mode": "ghost",
                "staged": {"pending-event.json": {"subject": "S"}}, "files": []}
    monkeypatch.setattr(ct, "run_turn", fake_run_turn)
    job = {"kind": "chat", "id": "chat-9", "run_id": cg.run_id_for("chat-9"), "lane": "dm", "channel": "dm:1",
           "thread_ts": "stream:5:topic:x", "source_run_id": "abc", "user_msg": "hi", "task_block": "ctx",
           "prompt": "THE PROMPT", "fresh_prompt": "THE FRESH PROMPT",
           "claude": {"output": "claude reply", "tools": [], "elapsed_s": 3}}
    rec = cg.run_job(job, codex_run=lambda *a, **k: pytest.fail("codex_cli.run must not be called directly"))
    # each replay is its own thread: the fresh prompt (it carries the
    # thread's history), keyed by the job, kept nowhere
    assert seen["thread_key"] == "chat-9" and seen["live"] is False and seen["fresh_prompt"] == "THE FRESH PROMPT"
    assert seen["prompt"] == "THE FRESH PROMPT" and seen["run_id"] == job["run_id"] and seen["persist"] is False
    assert rec["status"] == "ok" and rec["codex"]["output"] == "ghost reply" and rec["thread_id"] == "gh-9"
    assert rec["staged"] == {"pending-event.json": {"subject": "S"}}
    assert rec["codex"]["tool_calls"][0]["command"] == "cmd"


def test_a_codex_run_meets_the_same_gates_as_a_claude_run():
    """Tool parity: the token gate and the mediator refuse the gated verbs
    for any resident run, whatever the harness, and a Codex prompt names
    the rules a Claude run gets from its deny list and its subagent."""
    import mail_mediation as mm
    for verb in ("send-draft", "send-gmail-draft", "create-event", "update-event", "delete-event", "schedule-send"):
        assert verb in mm.GATED["graph_actions"], verb
        assert verb not in mm.SAFE["graph_actions"], verb
    assert "create-event" in mm.GATED.get("google_calendar_client", set())
    text = cg.codex_suffix(ct.REAL_EA_DIR, "chat")
    assert "no Agent tool and no email-author subagent" in text and "email_writer.py write" in text
    assert "Never write a file outside EA_DIR" in text and "never a `~` path" in text
    live = ct.live_prompt("P")
    assert "waits for the user's button" in live and "recorded by the mediator" not in live
    ghost = ct.ghost_prompt(f"see {ct.REAL_EA_DIR}/state", Path("/scratch/ea"))
    assert "recorded by the mediator" in ghost and str(ct.REAL_EA_DIR) + "/state" not in ghost


def test_the_bot_wires_the_codex_arm_and_the_tier():
    bot = (REPO / "ea-chat-bot.py").read_text()
    assert "def _codex_turn_live(" in bot
    assert 'if live_cr is None and arm == "codex":' in bot
    assert "live_cr, codex_turn_rec = _codex_turn_live(" in bot
    assert 'elif tier_pick["explicit"] and tier_pick["tier"] == "codex":' in bot
    assert '"remote" if tier == "local" else "codex" if tier == "codex" else "claude"' in bot
    assert "prompt=prompt, fresh_prompt=full_prompt,\n" in bot, "the ghost gets the prompt the bot sent Claude"
    import model_arms
    import model_tiers
    assert "codex" in model_arms.ARMS and model_arms._ALIASES["codex"] == "codex"
    assert model_tiers.tier_of("codex") == "codex" and model_tiers.NAMES["codex"] == "Codex"
    assert model_tiers.tier_of_model("codex", "codex") == "codex" and model_tiers.tier_of_model("claude-x", "claude") == "opus"
    assert model_tiers.parse_command("use codex")["tier"] == "codex"
    assert model_tiers.split_tag("@codex find a time")[0] == "codex"
    assert model_tiers.model_for("codex", default_model="m") == ("codex", "")


# ── what Claude Code loads by itself, for Codex ───────────────────────

def test_the_context_follows_the_directory_the_claude_run_would_use(private, tmp_path, monkeypatch):
    """A Claude run in the deployed tree loads CLAUDE.md, CLAUDE.local.md and
    the tree's memory index by itself; one in a lane directory (the
    scheduling and slim reply prompts) loads only that lane's own memory
    index, when it has one. Codex gets exactly that set."""
    tree = ct.context_block(ct.BOT_TREE)
    assert "CORE RULES" in tree and "INSTALL FACTS" in tree and MEMORY_INDEX in tree
    assert tree == ct.context_block(None), "the default is the deployed tree"
    # the real finder and reader on a tree of this test's own
    import resident_io
    home = tmp_path / "home"
    lane = home / ".local" / "state" / "ea-lanes" / "scheduling"
    lane.mkdir(parents=True)
    monkeypatch.setattr(ct, "BOT_HOME", home)
    monkeypatch.setattr(ct, "context_files", _REAL_CONTEXT_FILES)
    monkeypatch.setattr(ct, "_resident_text", _REAL_RESIDENT_TEXT)
    monkeypatch.setattr(resident_io, "read_text", lambda p: Path(p).read_text() if Path(p).is_file() else "")
    assert ct.context_block(lane) == "", "a lane directory carries no instruction file"
    mem = ct.memory_dir(lane)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [lane note](n.md): a lane fact")
    block = ct.context_block(lane)
    assert "lane note" in block and "=== INSTRUCTIONS" not in block, "the lane's own memory, nothing else"
    run = _codex([{"output": "ok", "thread_id": "th"}])
    monkeypatch.setattr(ct, "context_files", lambda cwd: [])
    monkeypatch.setattr(ct, "_resident_text", lambda p: "")
    ct.run_turn("SCHED", thread_key="t-s", run_id="r", live=True, fresh_prompt="SCHED", codex_run=run, claude_cwd=lane)
    assert run.calls[0]["prompt"].startswith("SCHED"), "the scheduling prompt goes alone, as it does to Claude"



def test_the_memory_is_read_only_to_codex_as_it_is_to_a_claude_lane(tmp_path, monkeypatch):
    import resident_write_paths as rwp
    assert "Edit(~/.claude/projects/*/memory/**)" in rwp.STEERING_RULES
    block = ct.context_block(ct.BOT_TREE)
    assert str(ct.memory_dir(ct.BOT_TREE)) in block and "The folder is read-only" in block
    assert "preferences.py add" in block, "a standing rule of the user's goes to the store, not to memory"
    assert "\u2014" not in ct._MEMORY_HEAD
    # the real write list on a tree of this test's own: the memory folder,
    # the Claude settings and ~/.local are not in it
    home, ea = tmp_path / "home", tmp_path / "home" / ".ea"
    for d in ("state", "config", ".claude/projects/x/memory", ".local/codex-vendor", "presentations"):
        (home / d if d.startswith(".") or d == "presentations" else ea / d).mkdir(parents=True)
    monkeypatch.setattr(ct, "BOT_HOME", home)
    monkeypatch.setattr(ct, "REAL_EA_DIR", ea)
    got = _REAL_LIVE_WRITE_PATHS()
    assert ea / "state" in got and home / "presentations" in got
    for off in (home, home / ".claude", home / ".claude" / "projects" / "x" / "memory", home / ".local"):
        assert off not in got, off
    assert not any(str(d).startswith(str(home / ".claude")) for d in got)



def test_the_memory_index_is_cut_where_claude_code_cuts_it(monkeypatch):
    long_index = "\n".join(f"- [n{i}](n{i}.md): hook" for i in range(300))
    monkeypatch.setattr(ct, "_resident_text", lambda p: long_index if str(p).endswith("MEMORY.md") else "")
    block = ct.context_block(ct.BOT_TREE)
    assert "[n199]" in block and "[n200]" not in block


def test_with_nothing_to_read_a_turn_gets_the_bots_prompt_alone(private, monkeypatch):
    monkeypatch.setattr(ct, "_resident_text", lambda p: "")
    assert ct.context_block(ct.BOT_TREE) == ""
    run = _codex([{"output": "ok", "thread_id": "th"}])
    ct.run_turn("CONT", thread_key="t-empty", run_id="r", live=True, fresh_prompt="FRESH", codex_run=run)
    assert run.calls[0]["prompt"].startswith("FRESH")


def test_the_context_files_are_the_ones_claude_code_finds_and_are_read_as_the_resident(tmp_path, monkeypatch):
    import claude_cli
    import resident_io
    cwd = tmp_path / "tree"
    (cwd / ".claude").mkdir(parents=True)
    for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
        (cwd / name).write_text(name)
    (tmp_path / "CLAUDE.md").write_text("parent")
    monkeypatch.setattr(ct, "BOT_HOME", tmp_path / "home")
    files = _REAL_CONTEXT_FILES(cwd)
    assert set(files) == set(claude_cli.memory_on_path(cwd))
    assert files.index(tmp_path / "CLAUDE.md") < files.index(cwd / "CLAUDE.md"), "the most general first"
    seen = []
    monkeypatch.setattr(resident_io, "read_text", lambda p: seen.append(p) or "text")
    assert _REAL_RESIDENT_TEXT(cwd / "CLAUDE.md") == "text" and seen == [cwd / "CLAUDE.md"]

    def refuse(p):
        raise OSError("no")
    monkeypatch.setattr(resident_io, "read_text", refuse)
    assert _REAL_RESIDENT_TEXT(cwd / "CLAUDE.md") == "", "a failed read leaves the file out, never the turn"


def test_the_memory_folder_is_the_one_claude_code_keeps_for_the_directory():
    import slack_sessions
    assert ct.memory_dir(ct.BOT_TREE) == (ct.BOT_HOME / ".claude" / "projects"
                                          / slack_sessions.project_dir_name(os.path.realpath(str(ct.BOT_TREE)))
                                          / "memory")


def test_the_home_roots_are_made_before_the_write_list(tmp_path, monkeypatch):
    """A Claude turn makes ~/proposals or ~/presentations on first use
    (overleaf_tools clone, a deck build); a Codex turn writes only the
    folders it was given, so they are made with the live working
    directory, as the resident, and the write list then carries them."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ct, "BOT_HOME", home)
    monkeypatch.setattr(ct.codex_ghost, "_bot_env", lambda extra=None: ["env"])
    cmds = []
    _REAL_ENSURE_LIVE_CWD(runner=lambda c, inp: cmds.append(c))
    assert cmds and cmds[0][:5] == ["env", "install", "-d", "-m", "750"]
    made = cmds[0][5:]
    assert made[0] == str(ct.LIVE_CWD)
    assert set(made[1:]) == {str(d) for d in ct.home_roots()}
    assert {d.name for d in ct.home_roots()} == {"presentations", "doc-review", "proposals",
                                                "letters-of-recommendation"}
    assert all(d.parent == home for d in ct.home_roots())


def test_a_live_turn_writes_where_a_claude_turn_writes(tmp_path, monkeypatch):
    """The folders of resident_write_paths (Claude's Write and Edit rules)
    and the ones its allowlisted scripts write (config by an atomic
    replace, logs, uploads); never the plugins' code, the memory, the
    Claude settings or the home itself."""
    import resident_write_paths as rwp
    home, ea = tmp_path / "home", tmp_path / "home" / ".ea"
    for d in ("state", "docs", "letters", "research", "receipts", "exports", "config", "logs", "uploads",
              "plugins/p1/state", "plugins/p1/bin"):
        (ea / d).mkdir(parents=True)
    (ea / "config" / "autonomy-tiers.json").write_text("{}")
    for d in ("presentations", "doc-review", "proposals", ".claude/projects/x/memory"):
        (home / d).mkdir(parents=True)
    monkeypatch.setattr(ct, "BOT_HOME", home)
    monkeypatch.setattr(ct, "REAL_EA_DIR", ea)
    got = _REAL_LIVE_WRITE_PATHS()
    want = [ea / d for d in ("state", "docs", "letters", "research", "receipts", "exports", "plugins/p1/state")]
    want += [home / d for d in ("presentations", "doc-review", "proposals")] + [ea / "logs", ea / "uploads"]
    assert got == want, got
    assert ea / "config" not in got, ("the config folder would cover ea-config.json, github.json and "
                                      "CLAUDE.install.md; autonomy-tiers.json does not open it")
    for p in rwp.paths(str(home), str(ea)):
        base = Path(p[:-3] if p.endswith("/**") else p)
        if p.startswith("/tmp") or "*" in p or not base.exists() or base.parent == ea / "config":
            continue   # a file in config opens no folder root
        assert base in got or base.parent in got, p
    for never in (home, ea, ea / "plugins" / "p1", ea / "plugins" / "p1" / "bin", home / ".claude"):
        assert never not in got


def test_the_staged_preference_writes_are_applied_after_a_live_turn_as_the_resident(private, monkeypatch, tmp_path):
    """preferences.py add and onboarding.py answer cannot write the config
    folder from the sandbox; they stage the write, and after the turn the
    bot replays it as the resident with the user's message on stdin, so a
    rule is applied only when the words it quotes are the user's."""
    staged = tmp_path / "preferences-staged.jsonl"
    monkeypatch.setattr(ct, "STAGED_FILE", staged)
    monkeypatch.setattr(ct.codex_ghost, "_bot_env", lambda extra=None: ["sudo", "-u", "ea-bot", "env",
                                                                        *(f"{k}={v}" for k, v in (extra or {}).items())])
    runs = []

    class _Res:
        stdout = json.dumps({"applied": [{"tool": "preferences", "argv": ["add"]}],
                             "refused": [{"tool": "preferences", "argv": ["add"], "why": "the quoted words are not in the user's message this turn"}]})

    def runner(cmd, inp):
        runs.append((cmd, inp))
        return _Res()
    assert _REAL_APPLY_STAGED("hello", runner=runner) == {} and runs == [], "nothing staged: no run"
    staged.write_text('{"tool": "preferences", "argv": ["add"]}\n')
    out = _REAL_APPLY_STAGED("for LoR uploads, just do them", runner=runner)
    cmd, inp = runs[0]
    assert cmd[:3] == ["sudo", "-u", "ea-bot"] and f"EA_DIR={ct.REAL_EA_DIR}" in cmd
    assert cmd[-4:] == [str(ct.BOT_TREE / "preprocessor" / "preferences.py"), "apply-staged", "--user-text-file", "-"]
    assert inp == "for LoR uploads, just do them", "the user's message, on stdin"
    assert len(out["applied"]) == 1 and len(out["refused"]) == 1
    # the live turn hands the user's message over and carries the outcome
    seen = []
    monkeypatch.setattr(ct, "_apply_staged", lambda text, runner=None: seen.append(text) or {"applied": [1], "refused": []})
    run = _codex([{"output": "ok", "thread_id": "th"}])
    rec = ct.run_turn("P", thread_key="t-p", run_id="r", live=True, fresh_prompt="P", codex_run=run,
                      user_text="just do them")
    assert seen == ["just do them"] and rec["preferences"] == {"applied": [1], "refused": []}
    ghost = _codex([{"output": "ok", "thread_id": "th"}])
    ct.run_turn("P", thread_key="t-g", run_id="r", live=False, fresh_prompt="P", codex_run=ghost, persist=False)
    assert seen == ["just do them"], "a ghost turn applies nothing"


def test_the_live_directory_is_made_as_the_resident_inside_the_state():
    cmds = []
    _REAL_ENSURE_LIVE_CWD(runner=lambda c, inp: cmds.append(c))
    assert cmds[0][:4] == ["sudo", "-n", "-u", cg.RESIDENT]
    tail = cmds[0][cmds[0].index("install"):]
    assert tail[:5] == ["install", "-d", "-m", "750", str(ct.LIVE_CWD)]
    assert tail[5:] == [str(d) for d in ct.home_roots()], "the home roots, made with it"
    assert ct.LIVE_CWD.parent == ct.REAL_EA_DIR / "state"


def test_a_live_chat_turn_may_ask_in_its_reply_and_writes_where_the_rules_say():
    live = cg.codex_suffix(ct.REAL_EA_DIR, "chat", live=True)
    assert "a question for the user goes in your final message" in live and "unattended" not in live
    assert "Write files only where the rules above say to" in live and "memory folder is read-only" in live
    for text in (cg.codex_suffix(ct.REAL_EA_DIR, "chat"), cg.codex_suffix(ct.REAL_EA_DIR, "briefing", live=True)):
        assert "this run is unattended" in text and "Never write a file outside EA_DIR." in text


def test_the_bot_hands_codex_the_directory_the_claude_run_would_use():
    bot = (REPO / "ea-chat-bot.py").read_text()
    assert bot.count("claude_cwd=str(prompt_cwd or EA_BOT_SCRIPT_DIR)") == 2, "the live arm and the ghost job"
    assert "fresh_prompt=fresh_prompt, lane=lane, claude_cwd=claude_cwd," in bot
    ghost = (REPO / "preprocessor" / "codex_ghost.py").read_text()
    assert 'claude_cwd=job.get("claude_cwd") or None' in ghost
