"""The heartbeat says what the turn is doing: tool calls become phrases,
the summary lists the last steps and the current one, Codex's command
events feed the same tracker, and both runners are wired to it."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import turn_progress as tp  # noqa: E402

P = "python3 /home/ea-bot/ea-system/preprocessor/"


@pytest.mark.parametrize("tool,args,phrase", [
    ("Bash", {"command": P + "graph_actions.py get-conversation --conversation-id X"}, "reading the thread"),
    ("Bash", {"command": P + "graph_actions.py create-reply --message-id a --body x"}, "creating the draft"),
    ("Bash", {"command": P + "scheduling_engine.py propose-meeting --duration 30"}, "finding times"),
    ("Bash", {"command": P + "email_writer.py write --to a@b.edu --brief-file f"}, "writing the body"),
    ("Bash", {"command": "EA_DIR=/x " + P + "ledger.py list --status open | jq ."}, "reading the ledger"),
    ("Bash", {"command": P + "ledger.py update --id r --last-action x"}, "updating the ledger"),
    ("Bash", {"command": P + "some_new_tool.py go"}, "running some_new_tool"),
    ("Bash", {"command": "git -C /x status"}, "checking the repository"),
    ("Bash", {"command": "grep -n foo bar.py"}, "reading files"),
    ("Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF"}, "running a script"),
    ("Read", {"file_path": "/x"}, "reading files"),
    ("Edit", {}, "writing files"),
    ("WebFetch", {"url": "https://x"}, "reading the web"),
    ("Agent", {"subagent_type": "email-author"}, "writing the body"),
    ("mcp__claude_ai_Google_Drive__read_file_content", {}, "reading Drive"),
    ("ToolSearch", {}, None),
])
def test_a_tool_call_becomes_a_phrase(tool, args, phrase):
    assert tp.describe(tool, args) == phrase


def test_the_summary_lists_the_steps_and_the_current_one():
    now = [1000.0]
    pr = tp.Progress(clock=lambda: now[0])
    assert pr.summary() is None, "nothing to say before the first step"
    pr.begin("Bash", {"command": P + "graph_actions.py get-conversation --conversation-id X"})
    now[0] += 20
    pr.end("Bash", {"command": P + "graph_actions.py get-conversation --conversation-id X"})
    pr.end("Bash", {"command": P + "graph_actions.py get-conversation --conversation-id Y"})
    pr.end("Bash", {"command": P + "scheduling_engine.py propose-meeting"})
    pr.end("Bash", {"command": P + "email_writer.py write"})
    now[0] += 340
    pr.begin("Bash", {"command": P + "graph_actions.py create-reply --message-id a"})
    now[0] += 40
    s = pr.summary()
    assert s == (":hourglass_flowing_sand: 7 min in. So far: read the thread, found times, wrote the body. "
                 "Now: creating the draft (40 s).")
    for i in range(6):
        pr.end("Bash", {"command": P + f"ledger.py update --id r{i}"})
        pr.end("Read", {})
    s = pr.summary()
    assert "So far: …, " in s and s.count(",") >= 3, "the last four steps, with a mark for the rest"
    assert "Now:" not in s, "a step that ended is no longer current"


def test_codex_command_events_feed_the_same_tracker():
    pr = tp.Progress(clock=lambda: 0.0)
    cmd = P + "scheduling_engine.py propose-meeting --duration 30"
    pr.codex_event({"type": "item.started", "item": {"type": "command_execution", "command": cmd}})
    assert "Now: finding times" in pr.summary()
    pr.codex_event({"type": "item.completed", "item": {"type": "command_execution", "command": cmd}})
    pr.codex_event({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    assert pr.summary() == ":hourglass_flowing_sand: 0 min in. So far: found times."


def test_both_runners_feed_the_heartbeat():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "progress.begin(tool_name" in bot and "progress.end(tname" in bot, "the Claude runner feeds it"
    assert bot.count("progress=progress") >= 4, "every mono-lane Claude call and the Codex turn pass it"
    assert "progress.summary() or" in bot, "the heartbeat prefers the summary"
    assert "FRONTEND.update(ref, text)" in bot, "later beats edit the first post"
    i = bot.index("heartbeat_stop = threading.Event()\n        def _heartbeat")
    assert i < bot.index("if live_cr is None and arm == \"codex\":"), "the heartbeat covers a live Codex turn"
    cli = (ROOT / "preprocessor" / "codex_cli.py").read_text()
    assert "on_event" in cli and "subprocess.Popen(" in cli, "Codex streams its events when asked"
    turn = (ROOT / "preprocessor" / "codex_turn.py").read_text()
    assert "on_event" in turn
