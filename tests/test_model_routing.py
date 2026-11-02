"""model_routing: the routing table's defaults and validation, the two
floors, the owner-private file over ea-config.json, the doctor's row, the
chat command, and every lane's decision point honouring the table with
the local model stubbed. Nothing reaches the network or the real private
directory."""
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import ea_config  # noqa: E402
import model_routing as mr  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402

TASKS = ("mail", "tagging", "drafting", "digests", "briefing", "audit", "scheduling", "chat",
         "thread_state", "review")


EMPTY_KEEP = {"domains": [], "addresses": [], "admissions": False}


def remote(**over) -> dict:
    """A remote config as remote_llm.config() returns it, with a made-up
    endpoint, every switch off and an empty keep list (no domain, no
    address, the admissions rule off) unless `over` says."""
    cfg = rl._off()
    cfg.update(endpoint="https://llm.example.test/v1", model="local-27b",
               keep_from_anthropic=dict(EMPTY_KEEP))
    cfg.update(over)
    return cfg


KEEP = {"domains": ["contractor.example.com"], "addresses": [], "admissions": True}


@pytest.fixture
def table(monkeypatch):
    """set(**tasks) makes that the routing table every lane reads."""
    def set_(**tasks):
        block = ea_config.routing_from_block({"tasks": tasks})
        monkeypatch.setattr(ea_config, "get_routing", lambda *a, **k: block)
        monkeypatch.setattr(ea_config, "get_routing_source", lambda *a, **k: (block, "routing.json"))
        return block
    set_()
    return set_


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner process with a scratch private dir and EA tree."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", ea / "config" / "ea-config.json")
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    return priv, ea


# ── defaults and validation ───────────────────────────────────────────

def test_every_task_defaults_to_auto_but_review_and_the_template_agrees():
    tasks = ea_config.DEFAULTS["routing"]["tasks"]
    assert tuple(tasks) == TASKS == ea_config.ROUTING_TASKS == tuple(mr.TASKS)
    assert {t: v for t, v in tasks.items() if v != "auto"} == {"review": "hosted",
                                                                "thread_state": "local"}
    template = json.loads((REPO / "config" / "ea-config.json").read_text())
    assert template["routing"] == ea_config.DEFAULTS["routing"]
    assert ea_config.routing_problems(template["routing"]) == []
    # a missing block, a junk block and an empty file all read as the defaults
    for val in (None, "local", [], {"tasks": "x"}, {}):
        assert ea_config.routing_from_block(val) == ea_config.DEFAULTS["routing"]


def test_a_wrong_task_or_value_is_named_and_dropped():
    bad = {"tasks": {"mail": "qwen", "drafts": "local", "chat": "local"}}
    problems = ea_config.routing_problems(bad)
    assert any("mail takes local, hosted or auto" in p for p in problems)
    assert any("no task 'drafts'" in p for p in problems)
    assert len(problems) == 2
    tasks = ea_config.routing_from_block(bad)["tasks"]
    assert tasks["mail"] == "auto" and tasks["chat"] == "local" and "drafts" not in tasks
    assert ea_config.routing_problems("x") == ["the routing block is not a JSON object"]
    # a flat block is named, not silently ignored
    assert ea_config.routing_problems({"chat": "local", "_doc": "x"}) == \
        ["routing takes its values under tasks, not 'chat'"]


def test_the_stakes_floor_holds_review_at_hosted_whatever_a_file_says(table):
    assert ea_config.routing_problems({"tasks": {"review": "local"}}) == \
        ["review is hosted only (the stakes floor)"]
    assert ea_config.routing_from_block({"tasks": {"review": "local"}})["tasks"]["review"] == "hosted"
    table(review="local")
    assert mr.value("review") == "hosted"
    assert mr.choice("review", cfg=remote(briefing_audit="remote")) == "hosted"
    assert not mr.local("review", switch_local=True, cfg=remote())
    row = {r["task"]: r for r in mr.rows(cfg=remote(), tiers=ea_config.tiers_from_block({}))}["review"]
    assert row["locked"] and row["allowed"] == ["hosted"] and row["floor"] == mr.STAKES_FLOOR[0]
    assert row["effective"] == "hosted" and row["why"] == "only Claude makes these judgment calls"
    tech = {r["task"]: r for r in mr.rows(cfg=remote(), tiers=ea_config.tiers_from_block({}),
                                           verbose=True)}["review"]
    assert tech["floor"].startswith("Stakes floor") and tech["why"] == "the stakes floor"
    with pytest.raises(mr.RoutingRefused, match="always stays on Hosted") as e:
        mr.save({"review": "local"}, cfg=remote())
    assert e.value.tech == "review is hosted only (the stakes floor)"


def test_the_keep_floor_holds_mail_and_tagging_off_hosted_while_the_list_has_entries(table):
    kept = remote(mail="enforce", keep_from_anthropic=KEEP)
    table(mail="hosted", tagging="hosted")
    # hosted waits: the switches decide, so kept mail stays off Claude
    assert mr.choice("mail", cfg=kept) == "auto" and mr.choice("tagging", cfg=kept) == "auto"
    assert rl.mode("mail", kept) == "enforce"
    assert mr.local("tagging", switch_local=True, cfg=kept)
    rows = {r["task"]: r for r in mr.rows(cfg=kept, tiers=ea_config.tiers_from_block({}))}
    tech = {r["task"]: r for r in mr.rows(cfg=kept, tiers=ea_config.tiers_from_block({}), verbose=True)}
    for task in ("mail", "tagging"):
        assert rows[task]["allowed"] == ["local", "auto"] and rows[task]["floor"] == mr.KEEP_FLOOR[0]
        assert rows[task]["effective"] == "local"
        assert rows[task]["why"].startswith("Hosted waits while you keep some mail away from Claude; ")
        assert tech[task]["floor"].startswith("Keep floor") and "keep floor" in tech[task]["why"]
    with pytest.raises(mr.RoutingRefused, match="cannot be Hosted while you keep some mail") as e:
        mr.save({"mail": "hosted"}, cfg=kept)
    assert "keep floor" in e.value.tech
    # with an empty list, hosted is hosted
    empty = remote(mail="enforce")
    assert mr.choice("mail", cfg=empty) == "hosted" and rl.mode("mail", empty) == "off"
    assert {r["task"]: r for r in mr.rows(cfg=empty)}["mail"]["floor"] == ""


def test_the_admissions_rule_alone_holds_the_floor_and_digests_are_floored(table):
    """The admissions rule keeps mail too (it is on unless set false, as
    remote_llm.config reads it), and the digest lane reads list and
    admissions mail: hosted waits on every reader of new mail."""
    assert rl._off()["keep_from_anthropic"]["admissions"] is True, "on by default"
    admissions = remote(mail="enforce", digests="remote",
                        keep_from_anthropic={"domains": [], "addresses": [], "admissions": True})
    assert mr.keep_listed(admissions) and not mr.keep_listed(remote())
    table(mail="hosted", tagging="hosted", digests="hosted")
    for task in ("mail", "tagging", "digests"):
        assert mr.choice(task, cfg=admissions) == "auto", task
    assert mr.local("digests", switch_local=True, cfg=admissions)
    rows = {r["task"]: r for r in mr.rows(cfg=admissions, tiers=ea_config.tiers_from_block({}))}
    assert rows["digests"]["allowed"] == ["local", "auto"] and "admissions" in rows["digests"]["floor"]
    assert rows["digests"]["effective"] == "local" and "Hosted waits" in rows["digests"]["why"]
    for task in ("mail", "tagging", "digests"):
        with pytest.raises(mr.RoutingRefused, match=f'"{mr.TASKS[task][0]}" cannot be Hosted') as e:
            mr.save({task: "hosted"}, cfg=admissions)
        assert e.value.tech.startswith(f"{task} cannot be hosted")
    # with the rule off and nothing listed, digests may be hosted
    assert mr.choice("digests", cfg=remote(digests="remote")) == "hosted"


def test_local_with_no_local_model_reads_hosted(table):
    table(drafting="local", chat="local")
    off = rl._off()
    assert mr.choice("drafting", cfg=off) == "hosted"
    assert not mr.local("drafting", switch_local=True, cfg=off)
    row = {r["task"]: r for r in mr.rows(cfg=off)}["drafting"]
    assert row["effective"] == "hosted"
    assert row["why"] == "set to Local, but your own model is not set up, so Claude does it"
    assert "no local model" in {r["task"]: r for r in mr.rows(cfg=off, verbose=True)}["drafting"]["why"]
    assert mr.local_status(off)["configured"] is False


def test_every_task_at_local_with_no_local_model_keeps_every_lane_on_claude(table, monkeypatch):
    import email_writer
    import mail_tagger
    import model_tiers as mt
    import remote_audit
    import remote_briefing
    import sched_agent
    table(**{t: "local" for t in TASKS})
    off = rl._off()
    monkeypatch.setattr(rl, "config", lambda: off)
    assert {k: v["effective"] for k, v in rl.switches(off).items()} == {k: "off" for k in rl.SWITCHES}
    assert not rl.reading("mail", off) and not sched_agent.live_on()
    assert not email_writer.writer_on() and not mail_tagger._remote_mode()
    assert not remote_briefing.active(off) and not remote_audit.active(off)
    assert remote_briefing.audit_judge(off) == "claude"
    for lane in ("main", "dm", "room", "sched-channel", "proactive"):
        pick = mt.resolve(lane=lane, cfg={}, remote_cfg=off)
        assert (pick["tier"], pick["note"]) == ("opus", "no-local"), lane


# ── the owner-private file outranks ea-config.json ────────────────────

def test_an_owner_process_reads_routing_json_and_never_ea_config(owner, monkeypatch):
    priv, ea = owner
    (ea / "config" / "ea-config.json").write_text(json.dumps({"routing": {"tasks": {"chat": "local"}}}))
    # no routing.json: the defaults, not ea-bot's copy
    block, source = ea_config.get_routing_source()
    assert source == "default" and block["tasks"]["chat"] == "auto"
    owner_trust.write_owner_file(priv / "routing.json", json.dumps(
        {"tasks": {"chat": "hosted", "drafting": "local"}}).encode(), mode=0o600)
    block, source = ea_config.get_routing_source()
    assert source == "routing.json"
    assert block["tasks"]["chat"] == "hosted" and block["tasks"]["drafting"] == "local"
    assert ea_config.get_routing() == block
    # any other process reads ea-config.json, where the block only shapes wording
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    block, source = ea_config.get_routing_source()
    assert source == "ea-config.json" and block["tasks"]["chat"] == "local"
    # an explicit path is that file, as for every other block
    assert ea_config.get_routing(ea / "config" / "ea-config.json")["tasks"]["chat"] == "local"


def test_a_routing_json_that_fails_the_owner_checks_reads_as_the_defaults(owner):
    priv, _ea = owner
    path = priv / "routing.json"
    path.write_text(json.dumps({"tasks": {"drafting": "local"}}))
    os.chmod(path, 0o666)
    block, source = ea_config.get_routing_source()
    assert source.startswith("refused") and block == ea_config.DEFAULTS["routing"]
    os.chmod(path, 0o600)
    path.write_text("[1, 2]")
    block, source = ea_config.get_routing_source()
    assert source.startswith("refused") and "JSON object" in source


def test_save_merges_writes_0600_and_records_the_change(owner):
    priv, _ea = owner
    cfg = remote()
    block = mr.save({"drafting": "local"}, cfg=cfg)
    assert block["tasks"]["drafting"] == "local" and block["tasks"]["review"] == "hosted"
    path = priv / "routing.json"
    assert path.stat().st_mode & 0o777 == 0o600
    mr.save({"chat": "hosted"}, cfg=cfg)
    on_disk = json.loads(path.read_text())["tasks"]
    assert on_disk["drafting"] == "local" and on_disk["chat"] == "hosted"
    changes = rl.read(mr.CHANGES)
    assert [c["changed"] for c in changes] == [{"drafting": ["auto", "local"]}, {"chat": ["auto", "hosted"]}]
    for bad in ({}, None, {"chat": "fable"}):
        with pytest.raises(mr.RoutingRefused):
            mr.save(bad, cfg=cfg)
    assert json.loads(path.read_text())["tasks"] == on_disk


def test_only_an_owner_process_saves(table, monkeypatch):
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    with pytest.raises(mr.RoutingRefused, match="runs as you") as e:
        mr.save({"chat": "local"}, cfg=remote())
    assert "owner" in e.value.tech


# ── the decision points ───────────────────────────────────────────────

def test_the_mail_part_follows_the_table(table):
    assert rl.mode("mail", remote(mail="shadow", shadow_until="2099-01-01")) == "shadow"
    table(mail="local")
    for raw, out in (("off", "enforce"), ("shadow", "enforce"), ("enforce", "enforce"), ("sealed", "sealed")):
        assert rl.mode("mail", remote(mail=raw, shadow_until="2099-01-01")) == out, raw
    assert rl.enforcing("mail", remote(mail="off"))
    # no local model: the gate stays as its switch says
    assert rl.mode("mail", {**rl._off(), "mail": "off"}) == "off"
    table(mail="hosted")
    assert rl.mode("mail", remote(mail="sealed")) == "off" and not rl.reading("mail", remote(mail="enforce"))
    assert rl.switch_mode("mail", remote(mail="sealed")) == "sealed"


def test_the_scheduling_agent_stops_on_hosted_and_keeps_its_gate_on_local(table, monkeypatch):
    import sched_agent
    cfg = remote(scheduling="enforce")
    monkeypatch.setattr(rl, "config", lambda: cfg)
    assert sched_agent.live_on()
    table(scheduling="hosted")
    assert not sched_agent.live_on() and rl.mode("scheduling", cfg) == "off"
    table(scheduling="local")
    # local never sets enforce: that value stays behind the ghost week's gate
    assert rl.mode("scheduling", remote(scheduling="off")) == "off"
    assert rl.mode("scheduling", cfg) == "enforce"


def test_the_writer_and_the_bot_s_draft_mode_follow_the_table(table, monkeypatch):
    import draft_ab
    import email_writer
    cfg = remote(drafting="remote")
    monkeypatch.setattr(rl, "config", lambda: cfg)
    assert email_writer.writer_on() and draft_ab.mode(cfg) == "remote"
    table(drafting="hosted")
    assert not email_writer.writer_on() and draft_ab.mode(cfg) == "off"
    out, rc = email_writer.write("someone@example.test", "Say yes.", "/nonexistent/out.txt")
    assert rc == email_writer.EXIT_FALLBACK and out == email_writer.WRITER_OFF
    cfg = remote(drafting="off")
    table(drafting="local")
    assert email_writer.writer_on() and draft_ab.mode(cfg) == "remote"
    assert draft_ab.mode(remote(drafting="ab", shadow_until="2099-01-01")) == "remote"


def test_the_briefing_audit_and_digest_lanes_follow_the_table(table, monkeypatch):
    import remote_audit
    import remote_briefing
    import remote_digests
    on = remote(briefings="remote", audits="remote", digests="remote")
    off = remote()
    assert remote_briefing.active(on) and remote_audit.active(on)
    assert not remote_briefing.active(off) and not remote_audit.active(off)
    table(briefing="local", audit="local", digests="local")
    assert remote_briefing.active(off) and remote_audit.active(off)
    table(briefing="hosted", audit="hosted", digests="hosted")
    assert not remote_briefing.active(on) and not remote_audit.active(on)
    # the digest lane: hosted hands the run to the Claude lane, local runs it remote
    monkeypatch.setattr(rl, "config", lambda: on)
    ran, claude = [], []
    monkeypatch.setattr(remote_digests, "run_list_digest", lambda **kw: ran.append(kw) or 0)
    assert remote_digests.main(["list-digest"], execv=lambda *a: claude.append(a)) == 0
    assert claude and not ran
    table(digests="local")
    monkeypatch.setattr(rl, "config", lambda: off)
    monkeypatch.setattr(remote_digests._owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(ea_config, "is_ea_enabled", lambda *a: True)
    claude.clear()
    assert remote_digests.main(["list-digest"], execv=lambda *a: claude.append(a)) == 0
    assert ran and not claude


def test_the_still_open_check_stays_on_claude_under_the_stakes_floor(table):
    import remote_briefing
    table(review="local")
    assert remote_briefing.audit_judge(remote(briefing_audit="remote")) == "claude"
    assert remote_briefing.audit_judge(remote()) == "claude"


def test_the_tagger_follows_the_table(table, monkeypatch):
    import mail_tagger
    cfg = remote(mail="enforce")
    monkeypatch.setattr(rl, "config", lambda: cfg)
    assert mail_tagger._remote_mode()
    table(tagging="hosted")
    assert not mail_tagger._remote_mode()
    cfg = remote()
    table(tagging="local")
    assert mail_tagger._remote_mode()


def test_switches_show_the_value_in_force(table):
    cfg = remote(drafting="remote", briefings="remote", mail="enforce")
    table(drafting="hosted", digests="local", mail="hosted")
    sw = rl.switches(cfg)
    assert sw["drafting"] == {"set": "remote", "effective": "off"}
    assert sw["digests"] == {"set": "off", "effective": "remote"}
    assert sw["briefings"] == {"set": "remote", "effective": "remote"}
    assert sw["mail"] == {"set": "enforce", "effective": "off"}


def test_a_lane_keeps_its_switch_when_the_table_cannot_be_read(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(ea_config, "get_routing", broken)
    cfg = remote(mail="enforce", drafting="remote")
    assert rl.mode("mail", cfg) == "enforce" and rl.local_for("drafting", True, cfg)
    import model_tiers as mt
    assert mt.resolve(lane="dm", cfg={"lanes": {"dm": "local"}}, remote_cfg=cfg)["tier"] == "local"


# ── the chat lanes (model_tiers.resolve) ──────────────────────────────

def test_the_chat_and_scheduling_lanes_take_the_table_over_the_lane_default():
    import model_tiers as mt
    cfg, rc = {"lanes": {"dm": "local"}}, remote()
    local = {"tasks": {"chat": "local", "scheduling": "local"}}
    hosted = {"tasks": {"chat": "hosted", "scheduling": "hosted"}}
    pick = mt.resolve(lane="main", cfg={}, remote_cfg=rc, routing=local)
    assert pick == {"tier": "local", "source": "routing", "explicit": True, "note": ""}
    assert mt.resolve(lane="sched-channel", cfg={}, remote_cfg=rc, routing=local)["tier"] == "local"
    assert mt.describe("local", "routing") == "This topic runs on the on-prem model (the routing table in Settings)."
    # the stakes rule sits over a local table value
    pick = mt.resolve(lane="dm", user_msg="Should I decline the dean's offer?", cfg={}, remote_cfg=rc,
                      routing=local)
    assert pick["tier"] == "opus" and pick["note"] == "stakes"
    # no local model: the footer says so
    assert mt.resolve(lane="dm", cfg={}, remote_cfg={"endpoint": ""}, routing=local)["note"] == "no-local"
    # hosted moves a pick that would run local: a lane default (to the bot's own
    # model, so a room keeps its specialist), a drawn arm, a local session
    pick = mt.resolve(lane="dm", cfg=cfg, remote_cfg=rc, routing=hosted)
    assert (pick["tier"], pick["source"], pick["explicit"]) == ("opus", "routing", False)
    for kw in ({"cfg": {}, "lane": "room", "arm": "remote"},
               {"cfg": {}, "lane": "main", "session_meta": {"model": "local-27b+slim"}}):
        pick = mt.resolve(remote_cfg=rc, routing=hosted, **kw)
        assert (pick["tier"], pick["source"], pick["explicit"]) == ("opus", "routing", True), kw
    # and leaves a hosted pick where it was (a room's own specialist model)
    pick = mt.resolve(lane="room", cfg={}, session_meta={"model": "claude-opus-5-5[1m]"},
                      remote_cfg=rc, routing=hosted)
    assert (pick["tier"], pick["source"], pick["explicit"]) == ("opus", "session", False)
    assert mt.resolve(lane="dm", cfg={"lanes": {"dm": "fable"}}, remote_cfg=rc,
                      routing=hosted)["tier"] == "fable"
    # the owner's own picks in a topic outrank the table
    assert mt.resolve(lane="dm", tag="local", cfg={}, remote_cfg=rc, routing=hosted)["tier"] == "local"
    assert mt.resolve(lane="dm", session_meta={"tier": "local"}, cfg={}, remote_cfg=rc,
                      routing=hosted)["tier"] == "local"
    assert mt.resolve(lane="dm", tag="opus", cfg={}, remote_cfg=rc, routing=local)["tier"] == "opus"
    # auto is the pick from before the table
    auto = {"tasks": {}}
    assert mt.resolve(lane="dm", cfg=cfg, remote_cfg=rc, routing=auto)["source"] == "dm default"
    assert mt.resolve(lane="main", cfg={}, remote_cfg=rc, routing=auto)["source"] == "default"
    assert mr.lane_route("other-lane", local) == "auto"


# ── the local model's state, the doctor, the screen's data ───────────

def _calls(priv: Path, *recs) -> None:
    store = priv / rl.STORE_DIRNAME
    store.mkdir(mode=0o700, exist_ok=True)
    (store / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_the_local_model_state_comes_from_its_last_call_and_its_key(owner, monkeypatch):
    priv, _ea = owner
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cfg = remote()
    monkeypatch.delenv("REMOTE_LLM_API_KEY", raising=False)
    monkeypatch.setattr(rl, "api_key", lambda: "")
    st = mr.local_status(cfg)
    assert st["configured"] and st["model"] == "local-27b" and st["reachable"] is False
    assert st["detail"] == "Its access key is missing."
    assert "REMOTE_LLM_API_KEY" in mr.local_status(cfg, verbose=True)["detail"]
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    assert mr.local_status(cfg)["reachable"] is None
    _calls(priv, {"ts": now, "purpose": "mail", "ok": True},
           {"ts": now, "purpose": "mail", "ok": False, "error": "request failed: timed out"},
           {"ts": now, "purpose": "chat-title", "ok": False, "error": "no free request slot within 2s"})
    st = mr.local_status(cfg)
    assert st["reachable"] is False and st["detail"] == "It did not answer when last asked, 1 min ago."
    assert "timed out" in mr.local_status(cfg, verbose=True)["detail"]
    _calls(priv, {"ts": now, "purpose": "mail", "ok": False, "error": "HTTP 502: bad gateway"},
           {"ts": now, "purpose": "mail-tune-x", "ok": False, "error": "request failed"},
           {"ts": now, "purpose": "draft", "ok": True})
    st = mr.local_status(cfg)
    assert st["reachable"] is True and st["detail"] == "Answered 1 min ago."


def _doctor():
    spec = importlib.util.spec_from_file_location("ea_doctor_routing", REPO / "scripts" / "ea-doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_doctor_row_names_each_task_s_model_and_the_local_model(owner, monkeypatch):
    priv, _ea = owner
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    cfg = remote(mail="enforce", drafting="remote")
    monkeypatch.setattr(rl, "config", lambda: cfg)
    status, detail, _fix = mr.doctor_row()
    assert status == "PASS"
    assert detail.startswith("local: mail, tagging, drafting, thread_state; "
                             "hosted: digests, briefing, audit, ")
    assert "local model local-27b: No call yet." in detail and detail.endswith("(default)")
    mr.save({"chat": "local"}, cfg=cfg)
    _calls(priv, {"ts": datetime.now().astimezone().isoformat(), "purpose": "mail", "ok": False,
                  "error": "request failed: connection refused"})
    status, detail, fix = mr.doctor_row()
    assert status == "WARN" and "chat" in detail.split(";")[0] and "ping" in fix
    assert detail.endswith("(routing.json)")
    # a doctor run records the row under its name
    doc = _doctor()
    doc.RESULTS.clear()
    doc.check_model_routing()
    assert [(r["name"], r["status"]) for r in doc.RESULTS] == [("model routing", "WARN")]
    # set to local with no local model to run on
    monkeypatch.setattr(rl, "config", lambda: rl._off())
    status, _detail, fix = mr.doctor_row()
    assert status == "WARN" and fix.startswith("chat set to local with no local model")
    # a refused file fails the row
    os.chmod(priv / "routing.json", 0o666)
    status, detail, _fix = mr.doctor_row()
    assert status == "FAIL" and "refused" in detail


def test_the_screen_data_and_the_chat_lines(table):
    table(drafting="local")
    data = mr.payload(cfg=remote(mail="enforce", keep_from_anthropic=KEEP),
                      tiers=ea_config.tiers_from_block({"lanes": {"dm": "local"}}))
    assert data["values"] == ["local", "hosted", "auto"] and data["source"] == "routing.json"
    rows = {r["task"]: r for r in data["rows"]}
    assert list(rows) == list(TASKS)
    assert all(r["label"] and r["what"] for r in rows.values())
    assert rows["drafting"]["value"] == "local" and rows["drafting"]["effective"] == "local"
    assert rows["mail"]["why"] == "Auto picked your own model" and rows["tagging"]["effective"] == "local"
    assert rows["tagging"]["why"] == 'Auto does what "Reading your mail" does'
    assert rows["chat"]["effective"] == "mixed"
    assert rows["chat"]["why"] == "Auto picked your own model for Direct messages, and Claude for the rest"
    assert rows["briefing"] == {**rows["briefing"], "value": "auto", "effective": "hosted",
                                "why": "Auto picked Claude", "locked": False}
    # the same rows in the code's terms, for `show --verbose` and the doctor
    tech = {r["task"]: r for r in mr.payload(cfg=remote(mail="enforce", keep_from_anthropic=KEEP),
                                             tiers=ea_config.tiers_from_block({"lanes": {"dm": "local"}}),
                                             verbose=True)["rows"]}
    assert tech["mail"]["why"] == "the mail switch is enforce" and "dm" in tech["chat"]["why"]
    assert tech["briefing"]["why"] == "the briefings switch is off"
    assert [r["label"] for r in tech.values()] == [r["label"] for r in rows.values()]
    assert tech["mail"]["what"] == mr.TECH["mail"] != rows["mail"]["what"]
    lines = mr.lines(data)
    assert lines[0].startswith(":control_knobs: *Model routing.* Local model local-27b")
    assert any(ln.startswith("- *Checking its own work* (`review`): hosted, now hosted") for ln in lines)
    assert "Settings" in lines[-1]
    # no em-dash anywhere the owner reads
    assert "—" not in json.dumps(data) + "\n".join(lines)


def test_the_web_page_saves_through_save_and_answers_the_screen(owner, monkeypatch):
    monkeypatch.setattr(rl, "config", lambda: remote())
    page = mr.WebSettings()
    assert [r["task"] for r in page.get()["rows"]] == list(TASKS)
    out = page.save({"tasks": {"briefing": "local"}})
    assert out["saved"] and {r["task"]: r for r in out["rows"]}["briefing"]["value"] == "local"
    with pytest.raises(ValueError, match='"Checking its own work" always stays on Hosted'):
        page.save({"tasks": {"review": "local"}})
    with pytest.raises(ValueError):
        page.save({})


def test_the_bot_answers_routing_without_a_model_call():
    bot = (REPO / "ea-chat-bot.py").read_text()
    m = re.search(r"_ROUTING_RE = re\.compile\((r\".*?\")", bot)
    rx = re.compile(eval(m.group(1)), re.I)
    for said in ("routing", "/routing", "model routing", " Routing? "):
        assert rx.match(said), said
    for said in ("routing of the mail", "what is the routing for chat"):
        assert not rx.match(said), said
    start = bot.index("def _colleague_command(")
    body = bot[start:bot.index("\ndef ", start + 10)]
    assert "_ROUTING_RE.match(text)" in body and "_mr.lines()" in body
    # the bot serves the Settings screen on a web front end, alone or as a shadow
    assert '_web.settings_pages["routing"] = _model_routing.WebSettings()' in bot
    assert 'getattr(FRONTEND, "shadow", None)' in bot


def test_the_cli_sets_one_task_and_refuses_a_floor(owner, monkeypatch, capsys):
    monkeypatch.setattr(rl, "config", lambda: remote())
    assert mr.main(["set", "audit", "local"]) == 0
    assert json.loads(capsys.readouterr().out) == {"task": "audit", "value": "local"}
    assert mr.main(["set", "review", "local"]) == 2
    assert "stakes floor" in capsys.readouterr().err
    assert mr.main(["show"]) == 0
    assert "*The weekly check* (`audit`): local, now local (set to Local)" in capsys.readouterr().out
    assert mr.main(["show", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "(`audit`): local, now local (set to local)" in out and "the stakes floor" in out


# ── plain words on the Settings screen ────────────────────────────────

sys.path.insert(0, str(REPO / "tests"))
from plain_words import JARGON  # noqa: E402

# The values the screen reads as code, never shows as a sentence: the task
# keys, the choices, where the table came from and the local model's name.
CODE_KEYS = {"task", "value", "values", "allowed", "effective", "source", "model"}


def _said(obj, key=None) -> list[str]:
    """Every string in `obj` the screen can show (all but CODE_KEYS)."""
    if key in CODE_KEYS:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in _said(v, k)]
    if isinstance(obj, list):
        return [s for v in obj for s in _said(v, key)]
    return []


def test_every_string_the_settings_screen_can_show_is_plain(owner, monkeypatch):
    """What /api/settings/routing can serve: the labels and descriptions,
    the why line and the floor under every value, switch, chat tier and
    keep list, the local model's state in each case, and every refusal a
    save can answer. None names a word tests/plain_words.py lists, which
    the app's own text is held to too; `verbose` keeps the code's terms."""
    priv, _ea = owner
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    on = {"mail": "enforce", "drafting": "remote", "digests": "remote", "briefings": "remote",
          "audits": "remote", "scheduling": "enforce"}
    mixed = {"mail": "shadow", "shadow_until": "2099-01-01", "drafting": "ab"}
    cfgs = [rl._off()] + [remote(keep_from_anthropic=keep, **sw)
                          for keep in (EMPTY_KEEP, KEEP) for sw in ({}, on, mixed)]
    lanes = [{}, {"dm": "local"}, {"main": "local", "room": "local"},
             {k: "local" for k in ("main", "dm", "room", "sched-channel", "proactive")}]
    said, tech, whys = [], [], set()
    for cfg in cfgs:
        for v in mr.VALUES:
            for ln in lanes:
                kw = {"cfg": cfg, "routing": {"tasks": {t: v for t in TASKS}},
                      "tiers": ea_config.tiers_from_block({"lanes": ln})}
                data = mr.payload(**kw)
                said += _said(data)
                whys |= {r["why"] for r in data["rows"]}
                tech += _said(mr.payload(**kw, verbose=True))
    # each branch of the why lines is reached
    for part in (*mr.AUTO_SAYS.values(), mr.JUDGMENT, mr.NOT_SET_UP, "set to Local", "set to Hosted",
                 "Hosted waits", "@local", "for Direct messages", "for Ask Tacitus and project rooms",
                 'Auto does what "Reading your mail" does', "Claude takes what it leaves"):
        assert any(part in w for w in whys), part
    # the local model's state: no key, no call yet, a failure whose error
    # names the code's words, an answer, and the breaker open
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    states = []
    monkeypatch.setattr(rl, "api_key", lambda: "")
    states.append(remote())
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    states.append(remote())
    for rec in ({"ts": now, "purpose": "mail", "ok": False,
                 "error": "HTTP 502: the endpoint dropped the session token"},
                {"ts": now, "purpose": "mail", "ok": True}):
        _calls(priv, rec)
        said += _said(mr.local_status(remote()))
        tech += _said(mr.local_status(remote(), verbose=True))

    class Open:
        def is_open(self):
            return True
    monkeypatch.setattr(rl, "_breaker", lambda *a: Open())
    states.append(remote())
    for cfg in states:
        said += _said(mr.local_status(cfg))
        tech += _said(mr.local_status(cfg, verbose=True))
    # every refusal a save answers, through the page the web chat calls
    page = mr.WebSettings()
    monkeypatch.setattr(rl, "config", lambda: remote(keep_from_anthropic=KEEP))
    bodies = [{}, {"tasks": {}}, {"tasks": []}, {"tasks": {"drafts": "local"}},
              {"tasks": {t: "qwen" for t in TASKS}}, {"tasks": {"review": "local"}},
              {"tasks": {t: "hosted" for t in TASKS}}]
    for body in bodies:
        with pytest.raises(mr.RoutingRefused) as e:
            page.save(body)
        said.append(str(e.value))
        tech.append(e.value.tech)

    def unwritable(*a, **k):
        raise OSError("the session token store is read-only")
    monkeypatch.setattr(owner_trust, "write_owner_file", unwritable)
    with pytest.raises(mr.RoutingRefused) as e:
        page.save({"tasks": {"chat": "local"}})
    said.append(str(e.value))
    tech.append(e.value.tech)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    with pytest.raises(mr.RoutingRefused) as e:
        page.save({"tasks": {"chat": "local"}})
    said.append(str(e.value))
    tech.append(e.value.tech)
    assert said and [s for s in said if JARGON.search(s)] == []
    assert "—" not in "".join(said)
    # the code's terms, the local model's own error included, stay for `show --verbose`
    assert any("switch" in s for s in tech) and any("session token" in s for s in tech)
    assert any(JARGON.search(s) for s in tech)
