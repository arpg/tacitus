"""arm_backtest: the collector keeps scheduling-lane first prompts only,
the arm runner parses a stream-json transcript and classifies the
outcome, the judge's answer is bounded, and the report counts. Fake
collector, runner and judge: nothing runs as ea-bot or reaches the
gateway."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import remote_llm as rl  # noqa: E402
import arm_backtest as ab  # noqa: E402

SCHED_PROMPT = ("# Scheduling Assistant – Operating Instructions\n\nYou are the scheduling assistant...\n"
                "The user posted this NEW message in #ea-scheduling:\n\n> Find 30 minutes with Ana next week\n")


def _stream(reply="Tue 2pm?\n=== DRAFT ===\nTo: wren.foxworth31@example.edu | Cc: none | sender-only\nHi Ana, Tuesday 2pm?\n=== EVENT ===\nnone",
            tool="python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting --duration 30",
            result='{"status": "ok", "options": [{"start": "2026-10-13T14:00:00-06:00"}]}', error=False):
    return "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "input": {"command": tool}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": result,
                                                  "is_error": error}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}},
        {"type": "result", "result": reply},
    ])


def test_collect_keeps_the_scheduling_lanes_first_prompts(monkeypatch):
    rows = [{"session": "a", "ts": "2026-10-01T10:00:00Z", "user": SCHED_PROMPT, "reads": [
                {"verb": "get-conversation", "cmd": "graph_actions.py get-conversation --conversation-id c", "output": "x" * 50}],
             "reply": "Done.", "drafts": ["create-reply"], "writes": [], "tools": 3},
            {"session": "b", "ts": "2026-10-01T11:00:00Z", "user": "The user posted this NEW message:\n> hi",
             "reads": [], "reply": "Hi.", "drafts": [], "writes": [], "tools": 0}]
    seen = {}
    ex = ab.collect(45, runner=lambda argv: seen.update(argv=argv) or "".join(json.dumps(r) + "\n" for r in rows))
    assert [e["session"] for e in ex] == ["a"]
    assert ex[0]["reads"] == [{"verb": "get-conversation", "cmd": "graph_actions.py get-conversation --conversation-id c"}]
    assert seen["argv"][3] == str(ab.USER_CAP)
    assert ab._request_tail(SCHED_PROMPT).startswith("The user posted this NEW message")


def test_run_arm_parses_the_transcript_and_classifies_the_outcome(monkeypatch):
    monkeypatch.setattr(ab, "_lane_env", lambda i, variant="": ["HOME=/home/ea-bot", "EA_RUN_ID=shadow-armbt-1"])
    cmds = []

    def runner(cmd, prompt):
        cmds.append((cmd, prompt))
        return 0, _stream(), ""
    arm = ab.run_arm(SCHED_PROMPT + ab.SANDBOX_NOTE, 1, runner=runner)
    assert arm["status"] == "ok" and arm["steps"] == 1 and arm["reply"].startswith("Tue 2pm?")
    assert arm["results"][0]["tool"].endswith("propose-meeting --duration 30") and not arm["results"][0]["error"]
    cmd, prompt = cmds[0]
    assert cmd[:5] == ["sudo", "-n", "-u", "ea-bot", "env"] and "EA_RUN_ID=shadow-armbt-1" in cmd
    assert "--no-session-persistence" in cmd and "--tools" in cmd and cmd[cmd.index("--tools") + 1] == "Bash,Read,Write" and "--bare" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mail_actions.py search *" in allowed and "scheduling_engine.py *" in allowed
    assert "graph_actions.py get-conversation *" in allowed
    assert "graph_actions.py search-mail *" not in allowed   # denied: the unified verb replaces it
    assert "create-reply" not in allowed
    dis = cmd[cmd.index("--disallowedTools") + 1]
    assert "Write" in dis.split(",") and "Bash(python3 *graph_actions.py create-reply *)" in dis
    assert "send-draft" in dis
    assert "=== SANDBOX REPLAY" in prompt
    assert ab.run_arm("p", 2, runner=lambda c, p: (1, "", "boom"))["status"] == "error"
    assert ab.run_arm("p", 3, runner=lambda c, p: (0, _stream(reply=""), ""))["status"] == "empty"
    assert ab.run_arm("p", 4, runner=lambda c, p: (0, _stream(reply="I stopped: the same call to Bash came back"), ""))["status"] == "loop"
    assert ab.run_arm("p", 5, runner=lambda c, p: (-1, "", "timeout"))["status"] == "timeout"


def test_judge_and_report(monkeypatch, tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    seen = {}

    def judge(prompt, schema):
        seen["prompt"] = prompt
        return {"grade": "edit", "better": "claude", "mode_agree": True, "withhold": False,
                "invented_times": [], "problems": [{"kind": "wrong-recipients", "note": "reply-all on a poll"}],
                "note": "right slot, wrong recipients", "enhancement": "Say sender-only for polls."}, {"cost_usd": 0.04}
    ex = {"session": "a", "ts": "2026-10-01T10:00:00Z", "user": SCHED_PROMPT,
          "reads": [{"verb": "get-conversation", "cmd": "graph_actions.py get-conversation --conversation-id c"}],
          "reply": "How does Tue 2pm sound?", "drafts": ["create-reply"],
          "writes": [{"path": "/home/ea-bot/.ea/state/reply-body-1.txt", "content": "Hi Ana, Tue 2pm?"}], "tools": 3}
    monkeypatch.setattr(ab, "_lane_env", lambda i, variant="": [])
    arm = ab.run_arm("p", 1, runner=lambda c, p: (0, _stream(), ""))
    v, meta = ab.judge_exchange(ex, arm, judge=judge)
    assert v["grade"] == "edit" and v["problems"][0]["kind"] == "wrong-recipients" and meta["cost_usd"] == 0.04
    p = seen["prompt"]
    assert "Find 30 minutes with Ana" in p and "Hi Ana, Tue 2pm?" in p and "propose-meeting" in p
    assert "=== DRAFT ===" in p and '"options"' in p
    rep = ab.run(45, 5, stamp="t", collector=lambda argv: json.dumps(ex) + "\n",
                 arm_runner=lambda c, pr: (0, _stream(), ""), judge=judge)
    assert rep["replayed"] == 1 and rep["grades"] == {"edit": 1} and rep["withhold"] == 0
    assert rep["arm_status"] == {"ok": 1} and rep["problems"] == {"wrong-recipients": 1}
    md = Path(rep["path"]).read_text()
    assert "Arm backtest t" in md and "wrong-recipients: reply-all on a poll" in md and "Say sender-only for polls." in md
    # A rerun resumes: the judged exchange is not replayed again.
    rep2 = ab.run(45, 5, stamp="t", collector=lambda argv: json.dumps(ex) + "\n",
                  arm_runner=lambda c, pr: (_ for _ in ()).throw(AssertionError("replayed twice")), judge=judge)
    assert rep2["replayed"] == 1


def test_select_parallel_and_compare(monkeypatch, tmp_path, capsys):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(ab, "_lane_env", lambda i, variant="": [])
    exs = [{"session": s, "ts": ts, "user": SCHED_PROMPT, "reads": [], "reply": "r", "drafts": [], "writes": [], "tools": 1}
           for s, ts in (("a", "2026-10-01T10:00:00Z"), ("b", "2026-10-02T10:00:00Z"), ("c", "2026-10-03T10:00:00Z"))]
    judge = lambda prompt, schema: ({"grade": "edit", "better": "same", "mode_agree": True, "withhold": False,
                                     "invented_times": [], "problems": [], "note": "", "enhancement": ""}, {"cost_usd": 0.01})
    rep = ab.run(45, 10, stamp="s1", collector=lambda argv: "".join(json.dumps(e) + "\n" for e in exs),
                 arm_runner=lambda c, p: (0, _stream(), ""), judge=judge, select=["2026-10-01", "2026-10-03"], parallel=3)
    assert rep["replayed"] == 2 and rep["judge_cost_usd"] == 0.02
    rep = ab.run(45, 10, stamp="s2", collector=lambda argv: "".join(json.dumps(e) + "\n" for e in exs),
                 arm_runner=lambda c, p: (0, _stream(reply=""), ""), judge=judge, select=["2026-10-01"])
    assert rep["arm_status"] == {"empty": 1}
    text = ab.compare(["s1", "s2"])
    assert "2026-10-01T10:00" in text and "2026-10-03T10:00" in text and "empty" in text
    assert "ok 2/2 send 0 edit 2 wrong 0" in text and "ok 0/1" in text
    assert len(ab.SMOKE) == 10 and all(len(t) == 16 for t, _w in ab.SMOKE)
    assert ab.main(["compare", "--stamps", "s1,s2"]) == 0 and "tally" in capsys.readouterr().out


def test_the_short_prompt_mode_rebuilds_the_compact_prompt(monkeypatch, tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(ab, "_lane_env", lambda i, variant="": [])
    seen = {}

    def runner(cmd, prompt):
        seen["prompt"] = prompt
        return 0, _stream(), ""
    lane = ("# Scheduling Assistant – Operating Instructions\n\n=== USER PROFILE ===\n{\"name\": \"C\"}\n=== END USER PROFILE ===\n"
            + "rule " * 3000 + "\nThe user posted this NEW message in #ea-scheduling:\n\n> Find 30 minutes with Ana next week\n")
    ex = {"session": "a", "ts": "2026-10-01T10:00:00Z", "user": lane, "reads": [], "reply": "r", "drafts": [], "writes": [], "tools": 1}
    judge = lambda prompt, schema: ({"grade": "send", "better": "same", "mode_agree": True, "withhold": False,
                                     "invented_times": [], "problems": [], "note": "", "enhancement": ""}, {"cost_usd": 0.01})
    rep = ab.run(45, 5, stamp="sp", collector=lambda argv: json.dumps(ex) + "\n", arm_runner=runner, judge=judge, prompt="short")
    assert rep["replayed"] == 1
    assert seen["prompt"].startswith("# Scheduling Assistant (on-prem arm)") and "rule rule" not in seen["prompt"]
    assert "Find 30 minutes with Ana" in seen["prompt"] and "=== SANDBOX REPLAY" in seen["prompt"]
    row = json.loads((ab.out_dir("sp") / "exchanges.jsonl").read_text().splitlines()[0])
    assert row["prompt"] == "short" and row["prompt_chars"] < len(lane) / 2
