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
    assert call["prompt"].startswith("FRESH"), "a thread with no Codex thread yet gets the fresh prompt"
    assert "You are Codex, not Claude" in call["prompt"] and "waits for the user's button" in call["prompt"]
    assert call["cwd"] == ct.BOT_HOME and call["sandbox"] == "workspace-write" and call["ephemeral"] is False
    assert call["resume"] == "" and set(call["add_dirs"]) == {ct.REAL_EA_DIR / d for d in ct.LIVE_WRITABLE}
    env = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in call["prefix"][5:]}
    assert env["EA_RUN_ID"] == "run-abc" and env["EA_DIR"] == str(ct.REAL_EA_DIR)
    assert rec["output"] == "Done." and rec["mode"] == "live" and rec["thread_id"] == "th-1" and rec["staged"] == {}
    assert rec["tool_calls"][0][0] == "Bash" and rec["tool_calls"][0][4] == {"command": "python3 x ledger.py list"}
    assert rec["returncode"] == 0 and rec["duration_ms"] == 2500
    assert ct.thread_row("t1", "live")["thread_id"] == "th-1" and ct.thread_row("t1", "ghost") == {}
    # the next turn resumes that thread with the continuation
    run2 = _codex([{"output": "Again.", "thread_id": "th-1"}])
    ct.run_turn("CONTINUATION", thread_key="t1", run_id="run-def", live=True, fresh_prompt="FRESH", codex_run=run2)
    assert run2.calls[0]["resume"] == "th-1" and run2.calls[0]["prompt"].startswith("CONTINUATION")
    assert ct.thread_row("t1", "live")["turns"] == 2


def test_a_failed_resume_starts_over_with_the_fresh_prompt(private):
    ct.remember_thread("t2", "live", "gone", ct.BOT_HOME)
    run = _codex([{"output": "", "harness_error": "codex exit 1: no such thread"},
                  {"output": "Fresh start.", "thread_id": "th-new"}])
    rec = ct.run_turn("CONT", thread_key="t2", run_id="r", live=True, fresh_prompt="FRESH", codex_run=run)
    assert [c["resume"] for c in run.calls] == ["gone", ""]
    assert run.calls[1]["prompt"].startswith("FRESH") and rec["output"] == "Fresh start."
    assert ct.thread_row("t2", "live")["thread_id"] == "th-new"


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
    # the second ghost turn reuses the scratch root and resumes the ghost thread
    monkeypatch.setattr(cg, "prepare_scratch", lambda *a, **k: pytest.fail("the scratch is reused"))
    run2 = _codex([{"output": "Again.", "thread_id": "gh-1"}])
    ct.run_turn("CONT", thread_key="t4", run_id=cg.run_id_for("chat-y"), live=False, codex_run=run2)
    assert run2.calls[0]["resume"] == "gh-1" and run2.calls[0]["cwd"] == root


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
    assert seen["thread_key"] == "stream:5:topic:x" and seen["live"] is False and seen["fresh_prompt"] == "THE FRESH PROMPT"
    assert seen["prompt"] == "THE PROMPT" and seen["run_id"] == job["run_id"]
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
    assert "prompt=prompt, fresh_prompt=full_prompt)" in bot, "the ghost gets the prompt the bot sent Claude"
    import model_arms
    import model_tiers
    assert "codex" in model_arms.ARMS and model_arms._ALIASES["codex"] == "codex"
    assert model_tiers.tier_of("codex") == "codex" and model_tiers.NAMES["codex"] == "Codex"
    assert model_tiers.tier_of_model("codex", "codex") == "codex" and model_tiers.tier_of_model("claude-x", "claude") == "opus"
    assert model_tiers.parse_command("use codex")["tier"] == "codex"
    assert model_tiers.split_tag("@codex find a time")[0] == "codex"
    assert model_tiers.model_for("codex", default_model="m") == ("codex", "")
