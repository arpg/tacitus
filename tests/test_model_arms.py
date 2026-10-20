"""model_arms: the draw and its stickiness, the window and the lanes,
pins and escalations, the turn, draft and event records, the scorecard,
and the config writers. Nothing reaches the network or the real private
directory."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import model_arms as ma  # noqa: E402


def _cfg(**kw):
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1",
           "ab": {"share": 0.5, "lanes": ["sched-channel", "main"], "from": "2026-10-12", "until": "2026-10-26"}}
    cfg.update(kw)
    return cfg


@pytest.fixture
def store(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "config", lambda: _cfg())
    owner_trust.write_owner_file(priv / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off"}).encode(), mode=0o600)
    return priv


class Rng:
    def __init__(self, draws):
        self.draws = list(draws)

    def random(self):
        return self.draws.pop(0)


def test_the_draw_is_by_share_sticky_and_only_in_the_lanes_and_window(store):
    today = date(2026, 10, 13)
    assert ma.arm_for("C", "t1", "sched-channel", rng=Rng([0.2]), today=today) == "remote"
    assert ma.arm_for("C", "t2", "sched-channel", rng=Rng([0.7]), today=today) == "claude"
    # Sticky: no draw on the second turn, whatever the rng would say.
    assert ma.arm_for("C", "t1", "sched-channel", rng=Rng([0.9]), today=today) == "remote"
    assert ma.arm_for("C", "t2", "sched-channel", rng=Rng([0.1]), today=today) == "claude"
    # A lane the A/B does not cover is Claude and leaves no row.
    assert ma.arm_for("R", "t3", "room", rng=Rng([0.0]), today=today) == "claude"
    assert ma.arm_of("R", "t3") is None
    # Outside the window: Claude, no row; a drawn topic keeps its arm.
    assert ma.arm_for("C", "t4", "main", rng=Rng([0.0]), today=date(2026, 11, 1)) == "claude"
    assert ma.arm_of("C", "t4") is None
    assert ma.arm_for("C", "t1", "sched-channel", today=date(2026, 11, 1)) == "remote"
    row = ma.arm_of("C", "t1")
    assert row["arm"] == "remote" and row["lane"] == "sched-channel" and row["reason"] == "drawn"
    assert row["draw"] == 0.2 and not row["pinned"]
    # The store is owner-only.
    assert (store / "remote-llm" / ma.ARMS_FILE).stat().st_mode & 0o777 == 0o600
    assert ma.remote_model() == "m-1"
    assert not ma.enabled(_cfg(ab={"share": 0.0, "lanes": ["main"]}))
    assert not ma.enabled(_cfg(ab={"share": 0.5, "lanes": []}))
    assert not ma.enabled(_cfg(endpoint=""))
    assert ma.enabled(_cfg(ab={"share": 0.5, "lanes": ["main"], "from": "", "until": ""}))


def test_a_pin_fixes_the_arm_and_counts_as_an_escalation(store):
    today = date(2026, 10, 13)
    assert ma.arm_for("C", "t1", "sched-channel", rng=Rng([0.2]), today=today) == "remote"
    row = ma.pin("C", "t1", "claude", reason="user")
    assert row["arm"] == "claude" and row["pinned"] and row["from_arm"] == "remote"
    assert ma.arm_for("C", "t1", "sched-channel", rng=Rng([0.0]), today=today) == "claude"
    esc = [t for t in rl.read(ma.TURNS) if t.get("kind") == "escalation"]
    assert len(esc) == 1 and esc[0]["arm"] == "remote" and esc[0]["to"] == "claude"
    # A pin on a fresh topic is a row, not an escalation; aliases hold.
    row = ma.pin("C", "t9", "qwen", lane="main")
    assert row["arm"] == "remote" and row["lane"] == "main"
    assert len([t for t in rl.read(ma.TURNS) if t.get("kind") == "escalation"]) == 1
    with pytest.raises(ValueError):
        ma.pin("C", "t9", "gpt")


def test_turns_drafts_and_events_join_into_the_scorecard(store, monkeypatch):
    today = date(2026, 10, 13)
    ma.arm_for("C", "t1", "sched-channel", rng=Rng([0.2]), today=today)      # remote
    ma.arm_for("C", "t2", "sched-channel", rng=Rng([0.8]), today=today)      # claude
    ma.note_turn(run_id="r1", channel="C", thread_ts="t1", lane="sched-channel", arm="remote", model="m-1",
                 rc=0, elapsed_s=12.3, num_turns=4, cost_usd=None, tools=5,
                 drafts={"d1": "Hi Ana, Tuesday 2pm works for me."}, events_staged=1,
                 backstop={"verdict": "ok", "issues": []})
    ma.note_turn(run_id="r2", channel="C", thread_ts="t1", lane="sched-channel", arm="remote", model="m-1",
                 rc=0, elapsed_s=30.0, fallback="backstop",
                 backstop={"verdict": "withhold", "issues": [{"kind": "time", "detail": "2pm is busy"}]})
    ma.note_turn(run_id="r3", channel="C", thread_ts="t2", lane="sched-channel", arm="claude",
                 model="claude-opus-5-5[1m]", rc=0, elapsed_s=40.0, cost_usd=0.5, tools=3,
                 drafts={"d2": "Hi Bo, Wednesday works."}, bounced=True)
    ma.note_turn(run_id="r4", channel="C", thread_ts="t2", lane="sched-channel", arm="claude",
                 model="claude-opus-5-5[1m]", rc=1, elapsed_s=5.0, error="boom")
    # A draft the arm's turn made: sent as written, sent edited, cancelled.
    assert ma.note_draft("d1", "sent", sent_body="Hi Ana, Tuesday 2pm works for me.")["outcome"] == "sent"
    rec = ma.note_draft("d2", "sent", sent_body="Hi Bo, Thursday at noon would suit me better, could we?")
    assert rec["outcome"] == "sent_edited" and rec["edit_ratio"] >= ma.EDITED_AT and rec["arm"] == "claude"
    assert ma.note_draft("d1", "cancelled")["arm"] == "remote"
    assert ma.note_draft("unknown", "sent") is None
    # A button in a topic with an arm; one in a topic without is nothing.
    assert ma.note_event_outcome("create", "t1", {"subject": "Ana"}, True)["arm"] == "remote"
    assert ma.note_event_outcome("delete", "t-none", {}, True) is None
    r = ma.report()
    rem, cl = r["lanes"]["sched-channel"]["remote"], r["lanes"]["sched-channel"]["claude"]
    assert rem["turns"] == 2 and rem["fallbacks"] == {"backstop": 1} and rem["backstop"] == {"ok": 1, "withheld": 1}
    assert rem["drafts"] == 1 and rem["sent"] == 1 and rem["cancelled"] == 1 and rem["events"] == {"create:ok": 1}
    # Nearest-rank percentiles, as sched_agent reports them: two values give the upper one.
    assert rem["latency_s"] == {"p50": 30.0, "p95": 30.0} and rem["cost_usd"] == 0.0 and rem["tools"] == 5
    assert cl["turns"] == 2 and cl["errors"] == 1 and cl["bounced"] == 1 and cl["sent_edited"] == 1
    assert cl["cost_usd"] == 0.5 and cl["sent_unchanged_share"] == 0.0
    assert r["topics"] == {"claude": 1, "remote": 1, "pipeline": 0, "codex": 0} and r["arms"]["remote"]["withheld"] == 1
    text = ma._report_text(r)
    assert "sched-channel/remote: turns 2" in text and "sched-channel/claude: turns 2" in text
    assert ma.summary_lines()[0].startswith("Model A/B (on; share 0.5")
    # Nothing to say while the A/B is off and no topic has an arm.
    monkeypatch.setattr(rl, "config", lambda: _cfg(ab={"share": 0.0, "lanes": []}))
    assert ma.summary_lines() != []            # topics have arms
    assert ma.report(since=date(2027, 1, 1))["lanes"] == {}


def test_enable_and_disable_write_the_config_block(store, monkeypatch, capsys):
    block = ma.enable(0.3, ["sched-channel", "bogus"], days=7, today=date(2026, 10, 13))
    assert block == {"share": 0.3, "lanes": ["sched-channel"], "from": "2026-10-13", "until": "2026-10-20", "arm": "remote"}
    raw = json.loads(owner_trust.read_owner_file(store / rl.CONFIG_FILE))
    assert raw["ab"] == block and raw["mail"] == "off"
    monkeypatch.setattr(rl, "config", rl.config.__wrapped__ if hasattr(rl.config, "__wrapped__") else rl.config)
    with pytest.raises(ValueError):
        ma.enable(0.5, ["bogus"])
    block = ma.disable()
    assert block["until"] < date.today().isoformat()
    assert ma.main(["arms"]) == 0 and ma.main(["report"]) == 0
    assert "Model A/B" in capsys.readouterr().out


def test_the_config_parses_the_ab_block(store, monkeypatch):
    monkeypatch.setattr(rl, "config", rl.config.__wrapped__ if hasattr(rl.config, "__wrapped__") else rl.config)
    owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1",
         "ab": {"share": 1.7, "lanes": ["main", 3], "from": "2026-10-12"}, "sched_backstop": False}).encode(),
        mode=0o600)
    import importlib
    cfg = importlib.reload(rl).config()
    monkeypatch.setattr(rl, "_owner", lambda: True)
    cfg = rl.config()
    assert cfg["ab"] == {"share": 1.0, "lanes": ["main"], "from": "2026-10-12", "until": "", "arm": "remote"}
    assert cfg["sched_backstop"] is False
    assert rl._off()["ab"] == {"share": 0.0, "lanes": [], "from": "", "until": "", "arm": "remote"}
    # the arm survives the loader: a pipeline window draws the pipeline,
    # not the harness arm it replaced; anything but pipeline reads as remote
    for written, read in (("pipeline", "pipeline"), ("claude", "remote"), (None, "remote")):
        block = {"share": 1.0, "lanes": ["sched-channel"], "from": "2026-10-14", "until": "2026-10-28"}
        if written:
            block["arm"] = written
        owner_trust.write_owner_file(store / rl.CONFIG_FILE, json.dumps(
            {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "ab": block}).encode(), mode=0o600)
        cfg = rl.config()
        assert cfg["ab"]["arm"] == read and ma.settings(cfg)["arm"] == read
        assert ma.arm_for("C", f"t-{written}", "sched-channel", cfg=cfg, rng=Rng([0.1]), today=date(2026, 10, 15)) == read
