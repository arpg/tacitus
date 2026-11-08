"""The one hosted-model switch: `hosted_default` in the routing table moves
every task without an override and the chat default together, overrides
survive a flip, a table from before the switch reads as the switch, the
tasks with no choice of their own stay on Claude, and no policy code
branches on a hosted model's name (a new branch fails here and must be
justified in ALLOWLIST). Nothing reaches the network or the real private
directory."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import ea_config  # noqa: E402
import hosted_text  # noqa: E402
import model_arms  # noqa: E402
import model_routing as mr  # noqa: E402
import model_tiers  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402

# The tasks a hosted model is a choice for, and the two with none.
CHOOSABLE = [t for t in mr.TASKS if len(mr.allowed(t)) > 1]
FIXED = [t for t in mr.TASKS if len(mr.allowed(t)) == 1]
REMOTE = {"endpoint": "https://llm.example.test/v1", "model": "local-27b",
          "keep_from_anthropic": {"domains": [], "addresses": [], "admissions": False}}


def remote() -> dict:
    cfg = rl._off()
    cfg.update(REMOTE)
    return cfg


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
    monkeypatch.setattr(rl, "config", remote)
    return priv


def _file(priv: Path) -> dict:
    return json.loads((priv / owner_trust.ROUTING_FILE).read_text())


def _chat_tiers(routing: dict, lanes: dict | None = None) -> dict:
    """{lane: (tier, explicit)} for every chat and scheduling lane, with no
    tag, no pin and no topic name."""
    out = {}
    for lane in mr.LANE_TASK:
        pick = model_tiers.resolve(lane=lane, cfg={"lanes": lanes or {}}, remote_cfg=remote(),
                                   routing=routing)
        out[lane] = (pick["tier"], pick["explicit"])
    return out


def test_the_registry_names_the_two_hosted_models_and_the_restricted_tasks():
    assert mr.HOSTED == ("claude", "codex") and mr.BASE == "claude"
    assert set(FIXED) == {"mail", "drafting"} and set(CHOOSABLE) == set(mr.CODEX_TASKS)
    # every name the code records finds its model, and a local name finds none
    for name, key in (("claude", "claude"), ("opus", "claude"), ("fable", "claude"),
                      ("claude-opus-5-5[1m]", "claude"), ("sonnet", "claude"), ("codex", "codex"),
                      ("gpt-6.1-sol", "codex")):
        assert mr.family(name) == key and mr.is_hosted(name), name
    for name in ("local", "remote", "pipeline", "local-27b+slim", "pipeline/local-27b", "local:qwen"):
        assert not mr.is_hosted(name) and mr.family(name) == "" and mr.is_local(name), name
    # a name no entry claims is hosted for privacy (fail closed) but has no entry to route by
    for name in ("", None, "code reply", "some-new-model"):
        assert mr.is_hosted(name) and mr.for_model(name) is None and not mr.is_base(name), name
    assert mr.is_base("opus") and not mr.is_base("codex") and not mr.is_base("local")
    assert (mr.harness_of("fable"), mr.harness_of("codex"), mr.harness_of("remote")) == ("claude", "codex", "")
    # the tiers and the arms come from the registry
    assert model_tiers.TIERS == ("local", "opus", "fable", "codex")
    assert model_arms.ARMS == ("claude", "remote", "pipeline", "codex") and model_arms.CONTROL == "claude"


# ── (a) the switch moves every task and the chat default together ─────

def test_flipping_the_switch_moves_every_unpinned_task_and_the_chat_default(monkeypatch):
    for model in mr.HOSTED:
        routing = {"tasks": {}, "hosted_default": model}
        hm = mr.hosted_map(routing)
        assert {hm[t] for t in CHOOSABLE} == {model}, model
        assert {mr.hosted_source(t, routing) for t in CHOOSABLE} == {"default"}
        # the cron lanes' harness follows: the briefings, the audit and the digests
        assert {mr.harness(a, routing) for a in mr.AGENT_TASK} == {mr.REGISTRY[model]["harness"]}
        # the tool-less calls follow: the tagger, the thread states and the review
        monkeypatch.setattr(ea_config, "get_routing", lambda *a, r=routing, **k: ea_config.routing_from_block(r))
        assert {hosted_text.model_for(t) for t in ("tagging", "thread_state", "review")} == {model}
    # the chat and scheduling lanes: Codex is an explicit pick in every lane
    codex = {"tasks": {}, "hosted_default": "codex"}
    assert set(_chat_tiers(codex).values()) == {("codex", True)}
    # Claude is the bot's own model: the lanes keep today's knobs (a room's
    # specialist, the scheduler model), and a `codex` lane default from the
    # tiers block gives way to the switch
    claude = {"tasks": {}, "hosted_default": "claude"}
    assert set(_chat_tiers(claude).values()) == {("opus", False)}
    lanes = {lane: "codex" for lane in mr.LANE_TASK}
    assert set(_chat_tiers(claude, lanes).values()) == {("opus", False)}
    # with no switch set the tiers block's `codex` lane default stands, as before the switch
    assert set(_chat_tiers({"tasks": {}}, lanes).values()) == {("codex", True)}
    assert mr.hosted_model("chat", routing={"tasks": {}}, tiers={"lanes": lanes}) == "codex"
    assert mr.hosted_model("briefing", routing={"tasks": {}}, tiers={"lanes": lanes}) == "claude"
    # the owner's own picks in a topic outrank the switch
    for tag, tier in (("opus", "opus"), ("codex", "codex"), ("fable", "fable"), ("local", "local")):
        for routing in (codex, claude):
            pick = model_tiers.resolve(lane="dm", tag=tag, cfg={}, remote_cfg=remote(), routing=routing)
            assert pick["tier"] == tier, (tag, routing)
    assert model_tiers.split_tag("@claude look at this")[0] == "opus"
    assert model_tiers.resolve(lane="main", session_meta={"tier": "opus"}, cfg={}, remote_cfg=remote(),
                               routing=codex)["tier"] == "opus"
    assert model_tiers.resolve(lane="main", thread_ts="stream:1:topic:%5Bcodex%5D%20plan", cfg={},
                               remote_cfg=remote(), routing=claude)["tier"] == "codex"


def test_the_switch_reaches_the_screen_the_chat_lines_and_the_doctor(owner):
    data = mr.payload(cfg=remote(), routing={"tasks": {}, "hosted_default": "codex"}, tiers={"lanes": {}})
    assert data["hosted_default"] == "codex" and data["hosted_default_set"] is True
    rows = {r["task"]: r for r in data["rows"]}
    assert {rows[t]["hosted"] for t in CHOOSABLE} == {"codex"}
    assert rows["chat"]["hosted_note"] == mr.CODEX_NOTE[0]
    lines = mr.lines(data)
    assert lines[1] == "*Hosted model:* Codex for every task not set apart."
    assert any(ln.startswith("- *Your briefing* (`briefing`): auto, hosted on Codex (default)") for ln in lines)
    unset = mr.payload(cfg=remote(), routing={"tasks": {}}, tiers={"lanes": {}})
    assert (unset["hosted_default"], unset["hosted_default_set"]) == ("claude", False)
    assert mr.lines(unset)[1].endswith("(not chosen yet).")
    (owner / owner_trust.ROUTING_FILE).write_text(json.dumps({"tasks": {}, "hosted_default": "codex"}))
    (owner / owner_trust.ROUTING_FILE).chmod(0o600)
    _status, detail, _fix = mr.doctor_row(cfg=remote())
    assert "hosted default: codex" in detail and "hosted on codex: tagging" in detail


# ── (b) overrides survive a flip ──────────────────────────────────────

def test_overrides_survive_a_flip_and_clear_one_at_a_time(owner, capsys):
    assert mr.main(["set-hosted", "codex"]) == 0
    assert _file(owner) == {**ea_config.DEFAULTS["routing"], "hosted_default": "codex"}
    assert mr.main(["set-hosted", "claude", "--task", "briefing"]) == 0
    assert mr.main(["set", "review", "claude"]) == 0
    assert _file(owner)["hosted"] == {"briefing": "claude", "review": "claude"}
    capsys.readouterr()
    for model in ("claude", "codex", "claude"):
        assert mr.main(["set-hosted", model]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["hosted_default"] == model and out["overrides"] == {"briefing": "claude", "review": "claude"}
        hm = mr.hosted_map()
        assert hm["briefing"] == hm["review"] == "claude"
        assert {hm[t] for t in CHOOSABLE if t not in ("briefing", "review")} == {model}
    assert mr.main(["show"]) == 0
    shown = capsys.readouterr().out
    assert "(`briefing`): auto, hosted on Claude (override)" in shown
    assert "(`chat`): auto, hosted on Claude (default)" in shown
    # --clear-task drops an override; the task follows the switch again
    assert mr.main(["set-hosted", "codex", "--clear-task", "briefing"]) == 0
    assert _file(owner)["hosted"] == {"review": "claude"} and mr.hosted_map()["briefing"] == "codex"
    assert mr.main(["set-hosted", "--clear-task", "review"]) == 0
    assert _file(owner)["hosted"] == {} and _file(owner)["hosted_default"] == "codex"
    # the screen's "use default" is the same drop
    page = mr.WebSettings()
    page.save({"hosted": {"chat": "claude"}})
    assert {r["task"]: r["hosted_source"] for r in page.get()["rows"]}["chat"] == "override"
    out = page.save({"hosted": {"chat": mr.FOLLOW}, "hosted_default": "claude"})
    rows = {r["task"]: r for r in out["rows"]}
    assert rows["chat"]["hosted"] == "claude" and rows["chat"]["hosted_source"] == "default"
    assert out["hosted_default"] == "claude" and _file(owner)["hosted"] == {}
    # each change is one line in the change log, the switch by its own key
    log = [json.loads(ln) for ln in (owner / rl.STORE_DIRNAME / mr.CHANGES).read_text().splitlines()]
    assert log[0]["changed"] == {"hosted_default": ["", "codex"]}
    assert log[-1]["changed"] == {"hosted.chat": ["claude", ""], "hosted_default": ["codex", "claude"]}
    # a bad switch or a task the CLI does not know is refused
    with pytest.raises(mr.RoutingRefused, match="The hosted model is Claude or Codex"):
        page.save({"hosted_default": "gpt"})
    for argv in (["set-hosted"], ["set-hosted", "--task", "chat"], ["set-hosted", "gpt"]):
        with pytest.raises(SystemExit):
            mr.main(argv)


# ── a table from before the switch ────────────────────────────────────

def test_a_table_with_one_model_for_every_task_reads_as_the_switch(owner):
    every = {"tasks": {}, "hosted": {t: "codex" for t in CHOOSABLE}}
    block = ea_config.routing_from_block(every)
    assert block["hosted_default"] == "codex" and block["hosted"] == {}
    assert ea_config.routing_problems(every) == []
    # one entry, or a few, are overrides: a single chat choice moves nothing else
    some = ea_config.routing_from_block({"hosted": {"chat": "codex", "briefing": "codex"}})
    assert "hosted_default" not in some and some["hosted"] == {"chat": "codex", "briefing": "codex"}
    assert mr.hosted_map(some)["review"] == "claude"
    # every task, two models: the chat entry's model is the switch, and each
    # task whose entry differs from it stays an override
    for chat in mr.HOSTED:
        other = next(m for m in mr.HOSTED if m != chat)
        mixed = {t: (other if i % 2 else chat) for i, t in enumerate(CHOOSABLE)}
        mixed["chat"] = chat
        split = ea_config.routing_from_block({"hosted": mixed})
        assert split["hosted_default"] == chat
        assert split["hosted"] == {t: m for t, m in mixed.items() if m != chat} and split["hosted"]
        hm = mr.hosted_map(split)
        assert {t: hm[t] for t in CHOOSABLE} == mixed
        # a flip moves the tasks that followed the chat entry, the rest stay where they were
        flipped = mr.hosted_map({**split, "hosted_default": other})
        assert {hm[t] for t in CHOOSABLE if flipped[t] != hm[t]} == {chat}
    # a task with no choice of its own adds no override
    extra = ea_config.routing_from_block({"hosted": {**mixed, "mail": "claude", "drafting": "claude"}})
    assert "mail" not in extra["hosted"] and "drafting" not in extra["hosted"]
    # a switch the file sets is kept whatever its entries say
    kept = ea_config.routing_from_block({**every, "hosted_default": "claude"})
    assert kept["hosted_default"] == "claude" and kept["hosted"] == every["hosted"]
    # the shape the earlier screen saved, split four and four as an install
    # can have it: the chat entry's model is the switch, the rest overrides
    four = dict(zip(CHOOSABLE, ["claude", "claude", "codex", "claude", "codex", "codex", "claude", "codex"]))
    assert four["chat"] == "codex"
    block = ea_config.routing_from_block({"hosted": four})
    assert block["hosted_default"] == "codex"
    assert block["hosted"] == {t: "claude" for t, m in four.items() if m == "claude"}
    # the first save writes it in the new shape: the switch, and only the entries that differ
    (owner / owner_trust.ROUTING_FILE).write_text(json.dumps({"tasks": {}, "hosted": four}))
    (owner / owner_trust.ROUTING_FILE).chmod(0o600)
    mr.WebSettings().save({"tasks": {"briefing": "auto"}})
    assert _file(owner)["hosted_default"] == "codex" and _file(owner)["hosted"] == block["hosted"]
    (owner / owner_trust.ROUTING_FILE).write_text(json.dumps(every))
    (owner / owner_trust.ROUTING_FILE).chmod(0o600)
    assert mr.hosted_default() == "codex" and mr.overrides() == {}
    assert mr.main(["set-hosted", "claude", "--task", "audit"]) == 0
    assert _file(owner)["hosted_default"] == "codex" and _file(owner)["hosted"] == {"audit": "claude"}
    (owner / owner_trust.ROUTING_FILE).write_text(json.dumps(every))
    mr.WebSettings().save({"tasks": {"drafting": "hosted"}})
    assert _file(owner)["hosted_default"] == "codex" and _file(owner)["hosted"] == {}


# ── (d) the tasks with no choice of their own stay on Claude ──────────

def test_the_restricted_tasks_stay_on_claude_whatever_the_switch(owner):
    for model in mr.HOSTED:
        routing = {"tasks": {}, "hosted_default": model}
        hm = mr.hosted_map(routing)
        assert {hm[t] for t in FIXED} == {"claude"}, model
        rows = {r["task"]: r for r in mr.rows(cfg=remote(), routing=routing, tiers={"lanes": {}})}
        for task in FIXED:
            assert rows[task]["hosted"] == "claude" and rows[task]["hosted_source"] == "fixed"
            assert rows[task]["hosted_why"] == "it has no hosted model of its own to pick"
            assert rows[task]["hosted_allowed"] == ["claude"]
        line = next(ln for ln in mr.lines(mr.payload(cfg=remote(), routing=routing, tiers={"lanes": {}}))
                    if "(`mail`)" in ln)
        assert "hosted on Claude (fixed: it has no hosted model of its own to pick)" in line
        verbose = {r["task"]: r for r in mr.rows(cfg=remote(), routing=routing, tiers={"lanes": {}},
                                                 verbose=True)}
        assert "mail gate off" in verbose["mail"]["hosted_why"]
    for task in FIXED:
        with pytest.raises(mr.RoutingRefused, match="has no hosted model of its own to pick"):
            mr.save({}, hosted={task: "codex"})
        assert mr.main(["set-hosted", "codex", "--task", task]) == 2
    # a file that asks for one is named and dropped
    assert any("mail has no Codex route" in p
               for p in ea_config.routing_problems({"hosted": {"mail": "codex"}}))


# ── (c) no policy code branches on a hosted model's name ──────────────

# The modules this change converted: each decides policy (what a hosted
# model may see, routing and floors, counters, what a turn is told) and
# asks model_routing by class, so none may name a model in a branch.
POLICY = ["preprocessor/model_routing.py", "preprocessor/ea_config.py", "preprocessor/model_tiers.py",
          "preprocessor/model_arms.py", "preprocessor/hosted_text.py", "preprocessor/mail_tagger.py",
          "preprocessor/sched_agent.py", "preprocessor/remote_briefing.py",
          "preprocessor/ledger_reconcile.py", "preprocessor/web_thread_state.py", "ea-chat-bot.py"]
# Files that may still branch on a model's name, each with why. A harness
# file builds and parses one harness's own run; a record file labels the
# hosted side of a local-against-hosted measure and routes nothing.
ALLOWLIST = {
    "run-agent.sh": "harness: starts a cron lane through the Codex lane runner or `claude -p` "
                    "on the answer of `model_routing.py harness` and the EA_HARNESS override",
    "preprocessor/codex_ghost.py": "harness: the Codex ghost arm's own config key and its judge's "
                                   "verdict label",
    "preprocessor/draft_ab.py": "record: the writer A/B names the draft it showed, claude or qwen",
    "preprocessor/draft_review.py": "record: the writer review page labels the claude and remote sides",
    "preprocessor/draft_backtest.py": "record: the writer backtest names which side wrote first",
    "preprocessor/ghost_week.py": "record: the ghost week's judge is claude or remote",
    "preprocessor/arm_backtest.py": "record: the arm backtest's verdict names claude, arm or same",
}
_NAMES = "|".join(map(re.escape, mr.REGISTRY))
BRANCH = re.compile(
    rf"""(?:==|!=)\s*['"](?:{_NAMES})['"]"""
    rf"""|['"](?:{_NAMES})['"]\s*(?:==|!=)"""
    rf"""|['"](?:{_NAMES})['"]\s+(?:not\s+)?in\b"""
    rf"""|\bin\s+[(\[{{](?:[^)\]}}]*,\s*)?['"](?:{_NAMES})['"]\s*[,)\]}}]"""
    rf"""|startswith\(\s*\(?['"](?:{_NAMES})""")


def _production_files() -> list[Path]:
    files = [REPO / "ea-chat-bot.py", REPO / "run-agent.sh"]
    files += sorted((REPO / "preprocessor").glob("*.py"))
    files += sorted((REPO / "botcore" / "botcore").rglob("*.py"))
    files += sorted((REPO / "scripts").glob("*.py")) + sorted((REPO / "scripts").glob("*.sh"))
    return [f for f in files if f.is_file()]


def _branches(path: Path) -> list[str]:
    out = []
    for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        code = line.split("#", 1)[0] if path.suffix == ".sh" else line
        if line.lstrip().startswith("#"):
            continue
        if BRANCH.search(code):
            out.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    return out


def test_no_policy_module_branches_on_a_hosted_model_s_name():
    for rel in POLICY:
        assert (REPO / rel).is_file(), rel
        assert _branches(REPO / rel) == [], rel
    found = [hit for f in _production_files() for hit in _branches(f)
             if str(f.relative_to(REPO)) not in ALLOWLIST]
    assert found == [], ("a branch on a hosted model's name: ask model_routing (is_hosted, is_local, "
                         "is_base, for_model) or justify the file in ALLOWLIST\n" + "\n".join(found))
    # the allowlist names only files that still need it
    assert all(_branches(REPO / rel) for rel in ALLOWLIST), \
        [rel for rel in ALLOWLIST if not _branches(REPO / rel)]
    # the pattern catches each shape it stands for
    for bad in ('if m == "codex":', "if 'claude' != arm:", 'if "codex" in raw:', 'if x in ("claude", "y"):',
                'm.startswith("codex")', '[[ "$H" == "codex" ]]'):
        assert BRANCH.search(bad), bad


# ── a new hosted model is one registry entry ──────────────────────────

NEW_MODEL = '''
    "nova": {"name": "Nova", "harness": "codex", "tier": "nova", "tiers": ("nova",), "arm": "nova",
             "names": ("nova",), "tasks": ("chat", "scheduling", "review")},
'''


def test_a_new_hosted_model_needs_one_registry_entry(tmp_path):
    """A made-up model added to HOSTED_MODELS alone, on the Codex harness:
    the tiers, the aliases, the arms, the screen's choices, the switch and
    the tool-less call all take it with no other edit."""
    src = (REPO / "preprocessor" / "ea_config.py").read_text()
    anchor = '"thread_state", "review")},\n'
    assert src.count(anchor) == 1
    (tmp_path / "ea_config.py").write_text(src.replace(anchor, anchor + NEW_MODEL))
    probe = f"""
import json, sys
sys.path[:0] = [{str(tmp_path)!r}, {str(REPO / "preprocessor")!r}]
import ea_config  # the copy with the new entry, before any module puts the tree first on the path
assert "nova" in ea_config.HOSTED_MODELS
import model_routing as mr, model_tiers as mt, model_arms as ma, hosted_text as ht
r = {{"tasks": {{}}, "hosted_default": "nova"}}
seen = {{}}
ht.codex = lambda prompt, **kw: seen.setdefault("called", {{"text": "OK", "rc": 0}})
print(json.dumps({{
  "hosted": list(mr.HOSTED), "tiers": list(mt.TIERS), "arms": list(ma.ARMS),
  "tag": mt.split_tag("@nova find a time")[0], "use": mt.parse_command("use nova")["tier"],
  "name": mt.NAMES["nova"], "arm_of": mt.arm_of("nova"), "model_for": mt.model_for("nova", default_model="m")[0],
  "footer": mt.tier_of_model("nova", "nova"), "allowed": mr.allowed("review"),
  "map": {{t: mr.hosted_map(r)[t] for t in ("chat", "review", "briefing", "mail")}},
  "source": mr.hosted_source("briefing", r), "lane": mt.resolve(lane="dm", cfg={{}}, remote_cfg={{"endpoint": ""}}, routing=r)["tier"],
  "problems": ea_config.routing_problems({{"hosted": {{"briefing": "nova"}}}}),
  "call": ht.call("nova", "p")["text"], "harness": mr.harness_of("nova-2"),
  "is_hosted": mr.is_hosted("nova-2"), "is_base": mr.is_base("nova"),
}}))
"""
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["hosted"] == ["claude", "codex", "nova"] and got["tiers"][-1] == "nova"
    assert got["arms"][-1] == "nova" and got["tag"] == got["use"] == "nova" and got["name"] == "Nova"
    assert got["arm_of"] == "nova" and got["model_for"] == "nova" and got["footer"] == "nova"
    assert got["allowed"] == ["claude", "codex", "nova"]
    assert got["map"] == {"chat": "nova", "review": "nova", "briefing": "claude", "mail": "claude"}
    assert got["source"] == "fixed" and got["lane"] == "nova"
    assert got["problems"] == ["briefing has no Nova route; its hosted part follows the model that runs it"]
    assert got["call"] == "OK" and got["harness"] == "codex" and got["is_hosted"] and not got["is_base"]
