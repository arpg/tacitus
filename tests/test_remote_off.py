"""With the remote model off, every lane takes its Claude path. Two off
states: no remote-llm.json at all (a fresh install), and a file that names
an endpoint with every switch at off. Nothing here reaches a model, the
network or the real private directory."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402


@pytest.fixture(params=["no-file", "all-off"])
def off(request, tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)

    def no_call(*a, **k):
        raise AssertionError("the remote model was called with every switch off")

    monkeypatch.setattr(rl, "chat", no_call)
    monkeypatch.setattr(rl, "chat_json", no_call)
    if request.param == "all-off":
        cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off",
               "scheduling": "off", "drafting": "off", "digests": "off", "briefings": "off"}
        owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def test_every_switch_reads_off(off):
    eff = {k: v["effective"] for k, v in rl.switches().items()}
    assert set(eff.values()) == {"off"}, eff
    for part in ("mail", "scheduling"):
        assert not rl.enforcing(part) and not rl.reading(part)


def test_briefings_hand_the_run_to_the_claude_lane(off, tmp_path):
    import remote_briefing
    rc = remote_briefing.main(["run", "--agent", "morning-briefing",
                               "--briefing", str(tmp_path / "none.json"), "--run-id", "t"])
    assert rc == remote_briefing.EXIT_OFF == 3
    # run-agent.sh runs the Claude lane on exit 3 and only then
    text = (REPO / "run-agent.sh").read_text()
    assert 'elif [[ "$REMOTE_RC" != "3" ]]' in text


def test_digests_exec_the_claude_lane(off):
    import remote_digests
    calls = []
    rc = remote_digests.main(["list-digest"], execv=lambda path, argv: calls.append(argv))
    assert rc == 0
    assert calls == [[str(remote_digests.RUN_AGENT), "list-digest"]]


def test_drafting_falls_back_to_the_claude_writer(off, tmp_path):
    import email_writer
    ans, code = email_writer.write("someone@example.org", "say thanks", str(tmp_path / "body.txt"))
    assert code == email_writer.EXIT_FALLBACK
    assert ans == {"status": "fallback", "reason": "writer off"}
    assert not (tmp_path / "body.txt").exists()


def test_tagger_asks_claude(off, monkeypatch):
    import subprocess
    import mail_tagger
    assert mail_tagger._remote_mode() is False
    seen = {}

    class R:
        returncode, stderr = 0, ""
        stdout = json.dumps({"result": "{}", "total_cost_usd": 0.0})

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    mail_tagger.ask_model("prompt")
    assert "-p" in seen["cmd"]


def test_mail_gate_stands_aside(off):
    import mail_privacy
    import mail_mediator
    assert mail_privacy.tick() == {"skipped": "mail switch is off"}
    assert mail_mediator._gate_error("graph_actions", "get-message", "cron-morning", RuntimeError("x")) == ""


def test_scheduling_stays_with_claude(off):
    import model_arms
    import sched_agent
    assert sched_agent.live_on() is False and sched_agent._shadow_on() is False
    assert model_arms.arm_for("C1", "t1", "sched-channel") == "claude"


def test_a_local_tier_runs_on_claude_without_an_endpoint(off, request):
    """`@local` is the user's own request, not a switch: it uses an endpoint
    when one is set up, and runs on Claude when none is."""
    import model_tiers
    pick = model_tiers.resolve(lane="dm", tag="local", local_chat_ready=True)
    if request.node.callspec.params["off"] == "no-file":
        assert pick["tier"] == "opus" and pick["note"] == "no-local"
    else:
        assert pick["tier"] == "local"
