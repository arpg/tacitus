"""model_tiers: the tag, the commands, the topic name, the lane default,
the order they win in, the stakes rule, the Fable budget, the footer
label, the record and the report lines. Nothing reaches the network or
the real private directory."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import ea_config  # noqa: E402
import model_tiers as mt  # noqa: E402


@pytest.fixture(autouse=True)
def local_chat_ready(monkeypatch):
    """These tests cover the local tier on the chat lanes, so the chat floor
    is lifted here; tests/test_routing_presets_floor.py covers the floor."""
    monkeypatch.setattr(ea_config, "local_chat_ready", lambda *a, **k: True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    owner_trust.write_owner_file(priv / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "off"}).encode(), mode=0o600)
    return priv


@pytest.fixture(autouse=True)
def _endpoint(monkeypatch):
    """A remote endpoint is set up unless a test says otherwise, so a
    local pick stays local whatever the machine's own private config."""
    monkeypatch.setattr(rl, "config", lambda: {"endpoint": "https://llm.example.edu/v1"})


def test_a_local_pick_runs_on_claude_when_no_remote_model_is_set_up():
    off = {"endpoint": ""}
    cfg = {"lanes": {"sched-channel": "local"}}
    tagged = mt.resolve(lane="dm", tag="local", remote_cfg=off)
    assert tagged == {"tier": "opus", "source": "tag", "explicit": True, "note": "no-local", "floor": ""}
    lane = mt.resolve(lane="sched-channel", user_msg="find 30 minutes next week", cfg=cfg, remote_cfg=off)
    assert lane["tier"] == "opus" and lane["note"] == "no-local"
    drawn = mt.resolve(lane="dm", arm="remote", cfg={}, remote_cfg=off)
    assert drawn["tier"] == "opus" and drawn["explicit"] is True
    assert mt.label("opus", note="no-local") == "opus (no local model set up)"
    # with an endpoint the same tag stays local
    assert mt.resolve(lane="dm", tag="local", remote_cfg={"endpoint": "https://x.example/v1"})["tier"] == "local"


def test_a_tag_names_the_tier_for_one_message_and_comes_off():
    assert mt.split_tag("@fable can you look at this") == ("fable", "can you look at this")
    assert mt.split_tag("@Fable, is this wise?") == ("fable", "is this wise?")
    assert mt.split_tag("ask fable: should I decline?") == ("fable", "should I decline?")
    assert mt.split_tag("@local find a time with Wren") == ("local", "find a time with Wren")
    assert mt.split_tag("@qwen find a time") == ("local", "find a time")
    assert mt.split_tag("@claude please") == ("opus", "please")
    # a bare tag, or no tag, leaves the message alone
    assert mt.split_tag("@fable") == ("", "@fable")
    assert mt.split_tag("can you look at this") == ("", "can you look at this")
    assert mt.split_tag("email fable@example.org") == ("", "email fable@example.org")


def test_a_tag_after_the_room_or_dm_preamble_is_still_seen():
    # The bot puts a bracketed preamble before the user's words in a
    # project room and in the DM; the tag comes after it, and the
    # preamble stays on the message for the model.
    room = ("[From the private #ea-robo-5000 channel, scoped to project 'robo-5000'. "
            "Reply in this thread; the project pack follows below.]\n\n")
    assert mt.split_tag(room + "@qwen please change the 10/27 lecturer to Quillon") == (
        "local", room + "please change the 10/27 lecturer to Quillon")
    dm = "[From your direct message with the EA. No project scope: treat this as a quick ask.]\n\n"
    assert mt.split_tag(dm + "ask fable: should I decline?") == ("fable", dm + "should I decline?")
    assert mt.split_tag(room + "no tag here") == ("", room + "no tag here")
    assert mt.parse_command(room + "use fable") == {"cmd": "use", "tier": "fable"}
    assert mt.parse_command(dm + "which model?") == {"cmd": "which"}
    # a user's own bracketed opener is not a preamble
    assert mt.split_tag("[draft] @fable look at this") == ("", "[draft] @fable look at this")


def test_the_commands_pin_escalate_hand_back_and_ask():
    assert mt.parse_command("use fable") == {"cmd": "use", "tier": "fable"}
    assert mt.parse_command("switch to local for this topic") == {"cmd": "use", "tier": "local"}
    assert mt.parse_command("use qwen here") == {"cmd": "use", "tier": "local"}
    assert mt.parse_command("use claude") == {"cmd": "use", "tier": "opus"}
    assert mt.parse_command("Escalate.") == {"cmd": "use", "tier": "fable"}
    assert mt.parse_command("escalate this to fable") == {"cmd": "use", "tier": "fable"}
    assert mt.parse_command("hand it back") == {"cmd": "use", "tier": "opus"}
    assert mt.parse_command("hand back to opus") == {"cmd": "use", "tier": "opus"}
    assert mt.parse_command("which model?") == {"cmd": "which"}
    assert mt.parse_command("what tier is this") == {"cmd": "which"}
    assert mt.parse_command("use the solver") is None
    assert mt.parse_command("escalate the ticket with IT") is None


def test_the_topic_name_asks_for_a_tier():
    assert mt.topic_name("stream:5:topic:%5Bfable%5D%20budget%20plan") == "[fable] budget plan"
    assert mt.topic_tier("stream:5:topic:%5Bfable%5D%20budget%20plan") == "fable"
    assert mt.topic_tier("stream:5:topic:%5B%20Local%20%5D%20times%20for%20Wren") == "local"
    assert mt.topic_tier("stream:5:topic:Draft%20and%20times%20for%20Wren") == ""
    assert mt.topic_tier("1760000000.123456") == ""


def test_the_pick_is_tag_then_pin_then_topic_then_lane_then_the_knobs(monkeypatch):
    cfg = {"lanes": {"dm": "fable", "sched-channel": "local"}}
    kw = dict(lane="dm", thread_ts="stream:4:topic:%5Blocal%5D%20x", session_meta={"tier": "opus"}, cfg=cfg)
    assert mt.resolve(tag="fable", **kw)["source"] == "tag"
    assert mt.resolve(**kw) == {"tier": "opus", "source": "pinned", "explicit": True, "note": "", "floor": ""}
    kw["session_meta"] = {}
    assert mt.resolve(**kw)["source"] == "topic name" and mt.resolve(**kw)["tier"] == "local"
    kw["thread_ts"] = "stream:4:topic:plain"
    assert mt.resolve(**kw) == {"tier": "fable", "source": "dm default", "explicit": True, "note": "", "floor": ""}
    # with none of the four, the pick only describes where the knobs put the turn
    none = dict(lane="main", thread_ts="stream:4:topic:plain", session_meta={}, cfg=cfg)
    assert mt.resolve(**none) == {"tier": "opus", "source": "default", "explicit": False, "note": "", "floor": ""}
    assert mt.resolve(arm="remote", **none) == {"tier": "local", "source": "draw", "explicit": False, "note": "", "floor": ""}
    assert mt.resolve(session_meta={"model": "claude-fable-5-1"}, lane="main", cfg=cfg) == {
        "tier": "fable", "source": "session", "explicit": False, "note": "", "floor": ""}
    assert mt.resolve(session_meta={"model": "claude-opus-5-5[1m]"}, lane="room", cfg=cfg)["tier"] == "opus"


def test_a_high_stakes_message_never_runs_on_local_unless_tagged():
    cfg = {"lanes": {"sched-channel": "local"}}
    pick = mt.resolve(lane="sched-channel", user_msg="set up a meeting with the dean about the budget", cfg=cfg)
    assert pick == {"tier": "opus", "source": "sched-channel default", "explicit": True, "note": "stakes", "floor": ""}
    # the A/B's drawn arm is moved too, and recorded as such
    drawn = mt.resolve(lane="sched-channel", user_msg="decline the invitation politely", arm="remote", cfg=cfg)
    assert drawn["tier"] == "opus" and drawn["note"] == "stakes" and drawn["explicit"]
    # the user's own tag wins
    tagged = mt.resolve(lane="dm", user_msg="find a slot with the dean", tag="local", cfg=cfg)
    assert tagged == {"tier": "local", "source": "tag", "explicit": True, "note": "", "floor": ""}
    # an ordinary scheduling ask stays local
    assert mt.resolve(lane="sched-channel", user_msg="find 30 minutes with Wren next week", cfg=cfg)["tier"] == "local"
    assert mt.high_stakes("the contract renewal") and not mt.high_stakes("lunch on Tuesday")


def test_the_fable_budget_moves_a_pick_to_opus(store):
    cfg = {"fable_daily_turns": 2}
    assert mt.resolve(lane="dm", tag="fable", cfg=cfg, fable_turns_today=1)["tier"] == "fable"
    spent = mt.resolve(lane="dm", tag="fable", cfg=cfg, fable_turns_today=2)
    assert spent == {"tier": "opus", "source": "tag", "explicit": True, "note": "budget", "floor": ""}
    # the count comes from the record
    for _ in range(2):
        mt.note_turn(tier="fable", source="tag", lane="dm", model="claude-fable-5-1")
    assert mt.fable_turns_today() == 2
    assert mt.resolve(lane="dm", tag="fable", cfg=cfg)["note"] == "budget"
    assert mt.resolve(lane="dm", tag="fable", cfg={"fable_daily_turns": 0})["tier"] == "fable"


def test_models_labels_and_aliases(monkeypatch):
    monkeypatch.setattr(ea_config, "get_tiers", lambda: ea_config.tiers_from_block(
        {"models": {"fable": "claude-fable-5-1"}, "efforts": {"fable": "high"}}))
    assert mt.model_for("fable", default_model="claude-opus-5-5[1m]") == ("claude-fable-5-1", "high")
    assert mt.model_for("opus", default_model="claude-opus-5-5[1m]") == ("claude-opus-5-5[1m]", "")
    assert mt.model_alias("fable", default_model="x") == "claude-fable-5-1"
    assert mt.model_alias("claude-sonnet-5-5", default_model="x") == "claude-sonnet-5-5"
    assert mt.tier_of_model("claude-fable-5-1") == "fable"
    assert mt.tier_of_model("qwen3.8-mm+slim") == "local" and mt.tier_of_model("x", arm="remote") == "local"
    assert mt.tier_of_model("claude-opus-5-5[1m]") == "opus"
    assert mt.label("local", backstop={"verdict": "ok"}) == "local, checked by Claude"
    assert mt.label("local") == "local"
    assert mt.label("opus", note="stakes") == "opus (stakes rule)"
    assert mt.label("opus", note="budget") == "opus (Fable budget spent)"
    assert mt.label("fable") == "fable"
    assert mt.describe("fable", "pinned") == "This topic runs on Fable (pinned by you)."
    assert mt.describe("local", "draw") == "This topic runs on the on-prem model (drawn by the model A/B)."


def test_the_config_block_drops_what_it_does_not_know():
    cfg = ea_config.tiers_from_block({"default": "sonnet", "models": {"haiku": "x", "fable": "f"},
                                      "efforts": {"fable": "huge", "opus": "high"},
                                      "lanes": {"dm": "fable", "room": "sonnet", 3: "opus"},
                                      "fable_daily_turns": "12"})
    assert cfg == {"default": "opus", "models": {"opus": "", "fable": "f"},
                   "efforts": {"opus": "high", "fable": ""}, "lanes": {"dm": "fable"}, "fable_daily_turns": 12}
    assert ea_config.tiers_from_block(None)["lanes"] == {}
    assert ea_config.get_tiers(Path("/nonexistent/ea-config.json"))["default"] == "opus"


def test_the_report_counts_turns_per_tier_and_why(store):
    assert mt.summary_lines() == []
    mt.note_turn(tier="opus", source="default", lane="main", model="o")
    mt.note_turn(tier="fable", source="tag", lane="dm", model="f")
    mt.note_turn(tier="opus", source="draw", lane="sched-channel", model="o", note="stakes")
    mt.note_turn(tier="local", source="draw", lane="sched-channel", model="q+slim")
    lines = mt.summary_lines(cfg={"fable_daily_turns": 20})
    assert lines == ["Tiers (today): local 1, opus 2, fable 1; picked by draw 2, default 1, tag 1; "
                     "moved to opus by stakes 1; Fable budget 1/20"]
    assert mt.summary_lines(since=date(2099, 1, 1)) == []
    assert mt.main(["report"]) == 0
