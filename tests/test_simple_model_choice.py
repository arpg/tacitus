"""The Models page's one pick and the install's one model question.

model_routing.simple_view gives the web chat's Models page one choice,
which hosted model does the work (the switch), with a line when custom
settings are on and a line when only one hosted model is set up
(hosted_ready). A pick saves the switch alone, so a task on the owner's
own model or set apart stays where it is. The /setup skill asks Claude
or Codex only when Codex is on the machine, and keeps the owner's own
model to an advanced section it skips unless the user asks; the setup
page and the terminal questions ask nothing about models. Nothing here
reaches the network or the real private directory.
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts" / "install"))
import ea_config  # noqa: E402
import model_routing as mr  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
from plain_words import JARGON  # noqa: E402

BOTH = {"claude": True, "codex": True}
PLAIN_TIERS = ea_config.tiers_from_block({})
SKILL = (REPO / ".claude" / "skills" / "setup" / "SKILL.md").read_text()


def remote(**over) -> dict:
    """A remote config with a made-up endpoint, every switch off and an
    empty keep list, unless `over` says."""
    cfg = rl._off()
    cfg.update(endpoint="https://llm.example.test/v1", model="local-27b",
               keep_from_frontier={"domains": [], "addresses": [], "admissions": False})
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def floor(monkeypatch):
    """The chat floor as the template sets it, read from no file."""
    monkeypatch.setattr(ea_config, "local_chat_ready", lambda *a, **k: False)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner process with a scratch private dir and EA tree, and a local
    model whose key is there and whose breaker is shut."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", ea / "config" / "ea-config.json")
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    return priv


def view(routing: dict, cfg: dict | None = None, tiers: dict | None = None, ready: dict = BOTH) -> dict:
    return mr.payload(cfg=cfg if cfg is not None else rl._off(), routing=routing,
                      tiers=tiers or PLAIN_TIERS, ready=ready)["simple"]


# ── the simple view ───────────────────────────────────────────────────

def test_a_plain_install_gets_one_pick_and_no_custom_line():
    """The template's table with no local model: one pick between Claude
    and Codex, both set up, the switch's model picked, nothing custom."""
    plain = ea_config.DEFAULTS["routing"]
    s = view(plain)
    assert s == {"model": "claude", "choices": [{"model": "claude", "name": "Claude", "ready": True},
                                                {"model": "codex", "name": "Codex", "ready": True}],
                 "line": "", "custom": False, "custom_line": ""}
    assert view({**plain, "hosted_default": "codex"})["model"] == "codex"
    assert not view({**plain, "hosted_default": "codex"})["custom"]
    # the All Claude and All Codex presets are plain too: every task follows the one pick
    for name in ("claude", "codex"):
        p = mr.BUILTIN_PRESETS[name]
        s = view({"tasks": p["tasks"], "hosted_default": p["hosted_default"]})
        assert (s["model"], s["custom"]) == (name, False)


def test_custom_settings_are_named_in_one_line(owner):
    """A task on the owner's own model now, a task set apart, and (with no
    switch set) a task the tiers block puts on the other model each make
    the view custom, and the one line names which."""
    tail = " A pick here changes only the tasks that follow it."
    own = view(ea_config.DEFAULTS["routing"], cfg=remote(mail="enforce"))
    assert own["custom"] and own["custom_line"] == ("Custom settings are on. Some tasks run on your own model."
                                                    + tail)
    # an install shaped like a privacy setup: every task on auto, the local switches on, Codex picked
    shaped = {"tasks": {**ea_config.DEFAULTS["routing"]["tasks"]}, "hosted_default": "codex"}
    on = remote(mail="enforce", drafting="remote", digests="remote", briefings="remote", audits="remote")
    assert view(shaped, cfg=on)["custom_line"].startswith("Custom settings are on. Some tasks run on your own model.")
    apart = view({"tasks": {}, "hosted_default": "codex", "hosted": {"review": "claude"}})
    assert apart["custom_line"] == "Custom settings are on. Some tasks have their own pick." + tail
    # an override on the switch's own model still stays when the switch moves
    assert view({"tasks": {}, "hosted_default": "codex", "hosted": {"review": "codex"}})["custom"]
    lanes = ea_config.tiers_from_block({"lanes": {"main": "codex"}})
    split = view({"tasks": {}}, tiers=lanes)
    assert split["model"] == "claude"
    assert split["custom_line"] == "Custom settings are on. Some tasks run on Codex." + tail
    # once the switch is set it outranks that lane default, and nothing is custom
    assert not view({"tasks": {}, "hosted_default": "claude"}, tiers=lanes)["custom"]
    every = view({"tasks": {}, "hosted": {"review": "claude"}}, cfg=remote(mail="enforce"), tiers=lanes)
    assert every["custom_line"] == ("Custom settings are on. Some tasks run on your own model, have their own "
                                    "pick or run on Codex." + tail)
    for s in (own, apart, split, every):
        assert not JARGON.search(s["custom_line"]) and chr(0x2014) not in s["custom_line"]


def test_with_one_model_set_up_the_view_names_it():
    """Exactly one hosted model set up: a line names it in place of the
    choice, and says so when the switch names the one that is not."""
    plain = ea_config.DEFAULTS["routing"]
    only = {"claude": True, "codex": False}
    s = view(plain, ready=only)
    assert s["line"] == "Claude does the work. Codex is not set up on this computer."
    assert [c["ready"] for c in s["choices"]] == [True, False]
    s = view({**plain, "hosted_default": "codex"}, ready=only)
    assert s["line"] == "Codex is picked, but it is not set up on this computer. Use Claude until it is."
    s = view({**plain, "hosted_default": "codex"}, ready={"claude": False, "codex": True})
    assert s["line"] == "Codex does the work. Claude is not set up on this computer."
    # both set up, or neither found: no line, the choice stands
    assert view(plain)["line"] == "" and view(plain, ready={"claude": False, "codex": False})["line"] == ""
    for r in (only, {"claude": False, "codex": True}):
        for line in (view(plain, ready=r)["line"], view({**plain, "hosted_default": "codex"}, ready=r)["line"]):
            assert not JARGON.search(line) and chr(0x2014) not in line


def test_hosted_ready_reads_the_resident_home(tmp_path, monkeypatch):
    """A model is set up when its program is on the resident's PATH and,
    for Codex, its login file is in the resident's home. A folder the owner
    cannot open counts as set up; with no resident every model does."""
    monkeypatch.setattr(mr, "RESIDENT_PATH", (".local/bin",))
    home = tmp_path / "resident"
    (home / ".local" / "bin").mkdir(parents=True)
    assert mr.hosted_ready(home) == {"claude": False, "codex": False}
    (home / ".local" / "bin" / "claude").write_text("#!/bin/sh\n")
    assert mr.hosted_ready(home) == {"claude": True, "codex": False}
    (home / ".local" / "bin" / "codex").write_text("#!/bin/sh\n")
    assert mr.hosted_ready(home) == {"claude": True, "codex": False}   # not signed in
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}")
    assert mr.hosted_ready(home) == BOTH
    monkeypatch.setenv("EA_BOT_HOME", str(home))
    assert mr._resident_home() == home and mr.hosted_ready() == BOTH
    monkeypatch.setattr(mr, "_there", lambda p: None)                  # the owner may not look
    (home / ".codex" / "auth.json").unlink()
    assert mr.hosted_ready(home) == BOTH
    monkeypatch.setattr(mr, "_resident_home", lambda: None)
    assert mr.hosted_ready() == BOTH


def test_the_page_data_carries_the_simple_view():
    data = mr.payload(cfg=rl._off(), routing=ea_config.DEFAULTS["routing"], tiers=PLAIN_TIERS, ready=BOTH)
    assert data["simple"]["model"] == data["hosted_default"] == "claude"
    assert set(data) >= {"simple", "rows", "presets", "local", "hosted_default"}


# ── a pick saves the switch alone ─────────────────────────────────────

def test_a_pick_saves_the_switch_alone_and_keeps_every_custom_setting(owner):
    """The pick at the top posts {"hosted_default": m}: routing.json gets
    the new switch, and every task value (the owner's own model on mail
    and drafting) and every override stay as they were. The change log
    names the switch alone."""
    priv = owner
    mr.save({"mail": "local", "drafting": "local", "chat": "auto"}, hosted={"review": "claude"},
            hosted_default="codex", cfg=remote())
    before = json.loads((priv / owner_trust.ROUTING_FILE).read_text())
    page = mr.WebSettings()
    out = page.save({"hosted_default": "claude"})
    after = json.loads((priv / owner_trust.ROUTING_FILE).read_text())
    assert after["hosted_default"] == "claude"
    assert after["tasks"] == before["tasks"] and after["tasks"]["mail"] == "local"
    assert after["hosted"] == before["hosted"] == {"review": "claude"}
    log = [json.loads(ln) for ln in (priv / rl.STORE_DIRNAME / mr.CHANGES).read_text().splitlines()]
    assert log[-1]["changed"] == {"hosted_default": ["codex", "claude"]} and log[-1]["via"] == "web"
    assert out["saved"] and out["simple"]["model"] == "claude" and out["simple"]["custom"]
    page.save({"hosted_default": "codex"})
    again = json.loads((priv / owner_trust.ROUTING_FILE).read_text())
    assert again["tasks"] == before["tasks"] and again["hosted"] == before["hosted"]
    with pytest.raises(mr.RoutingRefused, match="The hosted model is Claude or Codex."):
        page.save({"hosted_default": "your own model"})


# ── the install's model question ──────────────────────────────────────

def _section(title: str) -> str:
    """The skill's section under `title`, as written."""
    start = SKILL.index(f"\n## {title}")
    return SKILL[start:SKILL.index("\n## ", start + 1)]


def test_setup_asks_only_claude_or_codex_and_only_when_codex_is_there():
    """Section 5b offers exactly Claude and Codex, recommends Claude, asks
    only when `codex` is on the PATH (and not on macOS), asks nothing else
    about models, and records the answer through the switch."""
    s = _section("5b. Claude or Codex")
    offered = re.findall(r"^- `([A-Za-z]+)`: (.+)$", s, re.M)
    assert [name for name, _ in offered] == ["Claude", "Codex"]
    words = " ".join(s.split())
    assert "recommend Claude" in words and "command -v codex" in words and "not on macOS" in words
    assert "otherwise ask nothing" in words and "Ask nothing else about models" in words
    assert "set-hosted claude" in words and "set-hosted codex" in words and "deploy/enable-codex-ghost.sh" in words
    for _name, line in offered:
        assert not JARGON.search(line) and len(line.split()) <= 25 and chr(0x2014) not in line
    assert SKILL.index("## 5. Service account") < SKILL.index("## 5b. Claude or Codex") < SKILL.index("## 6. Chat")
    # a second run skips the question once it is answered or moot
    assert "section 5b is done" in _section("0. Resume")
    assert (REPO / "deploy" / "enable-codex-ghost.sh").is_file()


def test_setup_skips_the_owners_own_model_unless_asked():
    """The own model is an advanced section the skill skips: it is never
    offered, and no other section asks about it."""
    s = _section("7b. Advanced: a model of their own (skipped by default)")
    words = " ".join(s.split())
    assert "Skip this section: do not offer it" in words
    assert "only when the user brings it up themselves" in words
    rest = SKILL.replace(s, "")
    for ask in ("do they run their own model", "OpenAI-compatible endpoint, that should",
                "remote_llm.py init"):
        assert ask not in rest, ask


def test_the_setup_page_and_the_terminal_ask_no_model_question():
    """The questions the setup page and the terminal ask (setup_questions)
    name no model: the one model question is the skill's, after the
    service account."""
    import setup_questions as Q
    words = re.compile(r"\b(codex|claude or codex|own model|local model|remote model|llm|endpoint|hosted)\b", re.I)
    for q in Q._all_questions():
        text = " ".join(str(q.get(k) or "") for k in ("key", "label", "help", "why"))
        assert not words.search(text), q.get("key")


def test_the_reference_docs_name_the_choice_and_keep_the_own_model_advanced():
    setup = (REPO / "docs" / "SETUP.md").read_text()
    assert "\n## Claude or Codex\n" in setup and "\n## Advanced: your own model\n" in setup
    adv = setup[setup.index("\n## Advanced: your own model\n"):]
    assert adv.index("### Remote writer") < adv.index("\n## ", 1)
    assert "Setup skips this section unless you ask for it" in adv
    install = (REPO / "docs" / "INSTALL.md").read_text()
    assert "| A model you host | advanced, optional |" in install
    assert "one question: Claude or Codex" in install
