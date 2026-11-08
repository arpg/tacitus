"""Presets and the chat floor. Presets: the built-ins as code defines them,
apply and save as a round trip through the owner-private routing.json, the
keep floor's note, and a name a built-in has. The chat floor: while
`local_chat_ready` is false, `chat` and `scheduling` never run on the
owner's own model, whether a save, a preset, the table, a lane default,
an A/B draw, an `@local` tag or a `use local` pin asks for it; the screen
and `show` say "not yet", and every word of it passes the plain-words
scan. Nothing reaches the network or the real private directory."""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))
import ea_config  # noqa: E402
import model_routing as mr  # noqa: E402
import model_tiers as mt  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
from plain_words import JARGON  # noqa: E402

NOTE = ("Your own model does not hold up in conversation yet. Talking with you and "
        "finding meeting times stay on Claude or Codex for now.")
EMPTY_KEEP = {"domains": [], "addresses": [], "admissions": False}
KEEP = {"domains": ["contractor.example.com"], "addresses": [], "admissions": True}
CHAT_LANES = ("main", "dm", "room", "sched-channel", "proactive")


def remote(keep=None, **over) -> dict:
    cfg = rl._off()
    cfg.update(endpoint="https://llm.example.test/v1", model="local-27b",
               keep_from_frontier=dict(keep or EMPTY_KEEP))
    cfg.update(over)
    return cfg


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner process with a scratch private dir and EA tree, no keep list."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", ea / "config" / "ea-config.json")
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rl, "config", lambda: remote())
    return priv


def _file(priv: Path) -> dict:
    return json.loads((priv / owner_trust.ROUTING_FILE).read_text())


def _write(priv: Path, block: dict) -> None:
    path = priv / owner_trust.ROUTING_FILE
    path.write_text(json.dumps(block))
    path.chmod(0o600)


def _ready(monkeypatch, ready: bool) -> None:
    monkeypatch.setattr(ea_config, "local_chat_ready", lambda *a, **k: ready)


# ── presets ───────────────────────────────────────────────────────────

def test_the_built_in_presets_are_code_and_never_written(owner):
    b = mr.BUILTIN_PRESETS
    assert list(b) == ["private", "claude", "codex"]
    assert b["private"]["tasks"] == {"mail": "auto", "tagging": "auto", "drafting": "auto", "digests": "auto",
                                     "briefing": "auto", "audit": "auto", "scheduling": "hosted",
                                     "chat": "hosted", "thread_state": "local", "review": "hosted"}
    assert "hosted_default" not in b["private"]
    for model in ("claude", "codex"):
        assert b[model]["hosted_default"] == model
        assert b[model]["tasks"] == {**{t: "hosted" for t in mr.TASKS}, "thread_state": "local"}
    listed = mr.preset_list(routing={"tasks": {}})
    assert [(p["name"], p["label"], p["builtin"]) for p in listed] == [
        ("private", "Private", True), ("claude", "All Claude", True), ("codex", "All Codex", True)]
    assert all(p["note"] for p in listed)
    # applying one never writes the built-ins into the file
    mr.apply_preset("codex")
    assert "presets" not in _file(owner)
    # the template and the defaults agree that a fresh install keeps the floor
    template = json.loads((REPO / "config" / "ea-config.json").read_text())
    assert template["local_chat_ready"] is False and ea_config.DEFAULTS["local_chat_ready"] is False


def test_apply_and_save_are_a_round_trip(owner, capsys):
    assert mr.main(["set-hosted", "claude"]) == 0
    assert mr.main(["set-hosted", "codex", "--task", "review"]) == 0
    assert mr.main(["set", "briefing", "local"]) == 0
    # apply writes the tasks and the switch, and keeps no override the preset does not name
    out = mr.apply_preset("codex")
    f = _file(owner)
    assert f["tasks"] == mr.BUILTIN_PRESETS["codex"]["tasks"] and f["hosted_default"] == "codex"
    assert f["hosted"] == {} and out["notes"] == []
    assert mr.preset_now() == "codex"
    # private leaves the switch as it is
    mr.apply_preset("private")
    f = _file(owner)
    assert f["hosted_default"] == "codex" and f["tasks"]["mail"] == "auto" and f["tasks"]["chat"] == "hosted"
    assert mr.preset_now() == "private"
    # save keeps the table in force, overrides and switch included, under a name
    mr.save({"briefing": "local"}, hosted={"review": "claude"})
    capsys.readouterr()
    assert mr.main(["preset", "save", "Travel week", "--note", "on the road"]) == 0
    saved = _file(owner)["presets"]["Travel week"]
    assert saved["tasks"]["briefing"] == "local" and saved["hosted"] == {"review": "claude"}
    assert saved["hosted_default"] == "codex" and saved["note"] == "on the road"
    assert mr.preset_now() == "Travel week"
    # another preset, then the saved one back: the same table
    mr.apply_preset("claude")
    assert _file(owner)["hosted"] == {} and _file(owner)["hosted_default"] == "claude"
    assert "Travel week" in _file(owner)["presets"]   # a later save keeps the saved presets
    capsys.readouterr()
    assert mr.main(["preset", "apply", "Travel week"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tasks"]["briefing"] == "local" and out["overrides"] == {"review": "claude"}
    assert out["hosted_default"] == "codex"
    assert mr.main(["preset", "list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert listed[-1].startswith("Travel week (now): Travel week. on the road")
    # the change log names the preset applied and the one saved
    log = [json.loads(ln) for ln in (owner / rl.STORE_DIRNAME / mr.CHANGES).read_text().splitlines()]
    assert any(ln.get("preset") == "codex" for ln in log)
    assert any("presets.Travel week" in ln["changed"] for ln in log)
    # a name a built-in has, a bad name and an unknown preset are refused
    with pytest.raises(mr.RoutingRefused, match="belongs to a preset Tacitus has"):
        mr.save_preset("Codex")
    with pytest.raises(mr.RoutingRefused, match="up to 40 letters"):
        mr.save_preset("a/b")
    assert mr.main(["preset", "apply", "nothing-here"]) == 2
    with pytest.raises(SystemExit):
        mr.main(["preset", "save"])


def test_a_preset_under_the_keep_floor_keeps_mail_off_hosted_and_says_so(owner, monkeypatch):
    monkeypatch.setattr(rl, "config", lambda: remote(keep=KEEP))
    out = mr.apply_preset("claude")
    f = _file(owner)
    assert {t: f["tasks"][t] for t in mr.KEEP_FLOORED} == {"mail": "auto", "tagging": "auto", "digests": "auto"}
    assert f["tasks"]["briefing"] == "hosted" and f["hosted_default"] == "claude"
    assert out["notes"] == ["Reading your mail, Sorting mail and Mail digests stay on Auto while you keep "
                            "some mail away from hosted models."]


def test_the_screen_applies_and_saves_a_preset_through_one_field(owner):
    page = mr.WebSettings()
    data = page.get()
    assert [p["name"] for p in data["presets"]] == ["private", "claude", "codex"] and data["preset_now"] == ""
    out = page.save({"preset": "codex"})
    assert out["saved"] and out["preset_now"] == "codex" and out["notes"] == []
    assert {r["task"]: r["hosted"] for r in out["rows"]}["chat"] == "codex"
    out = page.save({"preset": {"save": "Mine", "note": "x"}})
    assert [p["name"] for p in out["presets"]][-1] == "Mine" and out["preset_now"] == "Mine"
    for body in ({"preset": "nothing-here"}, {"preset": 3}, {"preset": {"save": "private"}}):
        with pytest.raises(mr.RoutingRefused):
            page.save(body)


# ── the chat floor ────────────────────────────────────────────────────

def test_set_and_a_preset_cannot_put_chat_or_scheduling_on_your_own_model(owner, monkeypatch, capsys):
    assert mr.chat_floor_holds("chat") and mr.chat_floor_holds("scheduling")
    assert not mr.chat_floor_holds("briefing")
    for task in ("chat", "scheduling"):
        assert mr.main(["set", task, "local"]) == 2
        assert "Chat floor" in capsys.readouterr().err
    with pytest.raises(mr.RoutingRefused) as e:
        mr.WebSettings().save({"tasks": {"chat": "local", "scheduling": "local"}})
    assert str(e.value) == NOTE                      # one line, once
    assert mr.main(["set", "briefing", "local"]) == 0    # every other task still takes it
    # a saved preset that names them local is refused whole
    _write(owner, {"tasks": {}, "presets": {"Old": {"tasks": {"chat": "local", "audit": "local"}, "hosted": {}}}})
    with pytest.raises(mr.RoutingRefused, match="does not hold up in conversation yet"):
        mr.apply_preset("Old")
    assert _file(owner)["tasks"] == {}                  # nothing was written
    # with the floor lifted the same preset applies
    _ready(monkeypatch, True)
    mr.apply_preset("Old")
    assert _file(owner)["tasks"]["chat"] == "local"


def test_no_local_pick_on_a_chat_or_scheduling_lane_runs_local(monkeypatch):
    rc = remote()
    local = {"tasks": {"chat": "local", "scheduling": "local"}}
    asks = [dict(tag="local"), dict(session_meta={"tier": "local"}),
            dict(thread_ts="stream:1:topic:%5Blocal%5D%20plan"), dict(arm="remote"), dict(arm="pipeline"),
            dict(routing=local), dict(cfg={"lanes": {lane: "local" for lane in CHAT_LANES}})]
    for lane in CHAT_LANES:
        for ask in asks:
            kw = {"lane": lane, "remote_cfg": rc, "cfg": {}, "routing": {"tasks": {}}, **ask}
            pick = mt.resolve(**kw, local_chat_ready=False)
            assert (pick["tier"], pick["floor"], pick["note"], pick["explicit"]) == \
                ("opus", "local_chat", "local_chat", True), (lane, ask)
            codex = mt.resolve(**{**kw, "routing": {**kw["routing"], "hosted_default": "codex"}},
                               local_chat_ready=False)
            # under Codex the switch is the lane's pick before an A/B draw is read
            assert codex["tier"] == "codex" and (codex["floor"] == "local_chat" or "arm" in ask), (lane, ask)
            assert mt.resolve(**kw, local_chat_ready=True)["tier"] == "local", (lane, ask)
    # the floor reads ea-config when the caller says nothing, and an unknown lane is not floored
    _ready(monkeypatch, False)
    assert mt.resolve(lane="dm", tag="local", remote_cfg=rc, cfg={})["floor"] == "local_chat"
    assert mt.resolve(lane="other", tag="local", remote_cfg=rc, cfg={})["tier"] == "local"
    assert mt.chat_floor_note("dm") == mt.chat_floor_note("sched-channel") == NOTE
    assert mt.chat_floor_note("other") == ""
    assert mt.label("opus", note="local_chat") == "opus (your own model: not yet)"
    _ready(monkeypatch, True)
    assert mt.resolve(lane="dm", tag="local", remote_cfg=rc, cfg={})["tier"] == "local"
    assert mt.chat_floor_note("dm") == ""


def test_the_scheduling_agent_answers_no_turn_under_the_floor(monkeypatch):
    routing = {"tasks": {"scheduling": "auto"}}
    monkeypatch.setattr(ea_config, "get_routing", lambda *a, **k: ea_config.routing_from_block(routing))
    assert mr.scheduling_mode("enforce", cfg=remote()) == "shadow"
    assert mr.scheduling_mode("shadow", cfg=remote()) == "shadow"
    _ready(monkeypatch, True)
    assert mr.scheduling_mode("enforce", cfg=remote()) == "enforce"


def test_the_bot_answers_a_floored_tag_and_pin_with_the_note():
    bot = (REPO / "ea-chat-bot.py").read_text()
    use = bot[bot.index('floor_note = _tiers.chat_floor_note(lane) if _cmd.get("tier") == "local" else ""'):]
    assert use.index('text = f":label: {floor_note}"') < use.index("elif _cmd[\"cmd\"] == \"use\":")
    assert use.index("elif _cmd[\"cmd\"] == \"use\":") < use.index("_arms.pin(")
    assert ('if tier_pick.get("floor") and turn_tag == "local":' in bot
            and 'text=":label: " + _tiers.chat_floor_note(_turn_lane(channel, user_id)))' in bot)
    assert "', floor ' + tier_pick['floor']" in bot


def test_the_screen_and_show_say_not_yet_on_the_floored_rows(owner, capsys):
    data = mr.payload(cfg=remote(), routing={"tasks": {"chat": "local"}}, tiers={"lanes": {"dm": "local"}})
    rows = {r["task"]: r for r in data["rows"]}
    for task in ("chat", "scheduling"):
        assert rows[task]["not_yet"] is True and rows[task]["floor"] == NOTE
        assert "local" not in rows[task]["allowed"] and rows[task]["effective"] == "hosted"
    assert rows["chat"]["why"] == "Claude does it: your own model is not ready for conversation yet"
    assert not rows["briefing"]["not_yet"] and "local" in rows["briefing"]["allowed"]
    tech = {r["task"]: r for r in mr.rows(cfg=remote(), routing={"tasks": {"chat": "local"}}, verbose=True)}
    assert tech["chat"]["floor"] == mr.LOCAL_CHAT_NOTE[1] and "chat floor" in tech["chat"]["why"]
    lines = mr.lines(data)
    assert any("(`chat`)" in ln and ln.endswith("; your own model: not yet") for ln in lines)
    assert NOTE in lines and any(ln.startswith("Presets: Private (`private`)") for ln in lines)
    assert mr.main(["show"]) == 0
    assert "your own model: not yet" in capsys.readouterr().out


def test_every_word_of_presets_and_the_floor_is_plain(owner, monkeypatch):
    """The note, the presets' labels and lines, the rows under the floor and
    every refusal a preset or a floored save answers: none names a word
    tests/plain_words.py lists, and none has an em-dash."""
    said = [NOTE, mr.LOCAL_CHAT_NOTE[0]]
    for p in mr.preset_list(routing={"tasks": {}}):
        said += [p["label"], p["note"]]
    for routing in ({"tasks": {"chat": "local", "scheduling": "local"}}, {"tasks": {}}):
        for r in mr.rows(cfg=remote(keep=KEEP), routing=routing, tiers={"lanes": {}}):
            said += [r["why"], r["floor"], r["what"], r["hosted_note"], r["hosted_why"], r["label"]]
    for body in ({"tasks": {"chat": "local"}}, {"preset": "nothing-here"}, {"preset": {"save": "codex"}},
                 {"preset": {"save": "a/b"}}, {"preset": 3}):
        with pytest.raises(mr.RoutingRefused) as e:
            mr.WebSettings().save(body)
        said.append(str(e.value))
    monkeypatch.setattr(rl, "config", lambda: remote(keep=KEEP))
    said += mr.apply_preset("codex")["notes"]
    said = [s for s in said if s]
    assert said and [s for s in said if JARGON.search(s)] == []
    assert "\u2014" not in "".join(said)
    # the screen's own words for the choices and the presets
    js = (REPO / "botcore" / "botcore" / "chat" / "webui" / "app.js").read_text()
    for words in ("'Your own model'", "'Claude or Codex'", "'Presets'", "'Save current as...'",
                  "'A name for these settings:'"):
        assert words in js, words
        assert not JARGON.search(words), words
    assert not re.search(r"\bLocal\b|'Hosted'", js[js.index("const ROUTE_WORDS"):js.index("let presetPick")])
