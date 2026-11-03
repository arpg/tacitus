#!/usr/bin/env python3
"""Tests for preprocessor/preferences.py, the personalization layer, and
its wiring into the three prompt lanes (run-agent.sh, run-plugin.sh,
ea-chat-bot.py).

Topical judgment belongs in a store that is the user's, not in a skill
file that is the code's. This file pins that home: scoped rules in the
user's words, injected by scope, writable from every lane under a lock.

Usage:
    python3 tests/test_preferences.py
"""
from __future__ import annotations

import json
from contextlib import redirect_stdout
import pytest
import io
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Sandbox before import: the module derives its store path from EA_DIR.
SANDBOX = Path(tempfile.mkdtemp(prefix="ea-test-prefs-"))
os.environ["EA_DIR"] = str(SANDBOX)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import preferences as prefs  # noqa: E402

RUN_AGENT = REPO / "run-agent.sh"
RUN_PLUGIN = REPO / "run-plugin.sh"
BOT = REPO / "ea-chat-bot.py"
SKILLS = REPO / "skills"
CLI = [sys.executable, str(REPO / "preprocessor" / "preferences.py")]


def _fresh_store(name: str) -> Path:
    p = SANDBOX / "config" / f"{name}.json"
    if p.exists():
        p.unlink()
    return p


def _sample() -> dict:
    d = prefs._empty()
    prefs.add_rule(d, kind="avoid", scopes=["funding", "newsletters"],
                   text="Standing or office-wide BAAs are not opportunities; skip them.",
                   quote="That office BAA is like the others, just noise - not actually an opportunity.",
                   date_str="2026-09-01", source="slack", rule_id="standing-baas")
    prefs.add_rule(d, kind="instruction", scopes=["newsletters:robotics-worldwide"],
                   text="Send the relevant snippets, then archive; never job postings.",
                   quote="anything else can go - job postings especially",
                   date_str="2026-09-01", source="slack", rule_id="rw-instruction")
    prefs.add_rule(d, kind="fact", scopes=["*"], text="Senior faculty member in the department.",
                   date_str="2026-08-01", rule_id="tenured")
    prefs.add_rule(d, kind="prefer", scopes=["newsletters"], text="Dataset releases in perception.",
                   date_str="2026-09-01", rule_id="datasets")
    prefs.add_rule(d, kind="avoid", scopes=["briefing"], text="Never surface Quarantine digests.",
                   date_str="2026-06-01", rule_id="quarantine")
    prefs.retire_rule(d, "quarantine", "example of a retired rule")
    prefs.add_rule(d, kind="avoid", scopes=["briefing"], text="Suppress SafetyPortal reminders unless FINAL/OVERDUE.",
                   date_str="2026-06-01", rule_id="scishield")
    prefs.add_calibration(d, scope="newsletters:robotics-worldwide", date_str="2026-09-01",
                          note="1 of 17 posted (Foggy Docks); the user confirmed.")
    prefs.add_calibration(d, scope="briefing", date_str="2026-08-30", note="briefing ran long.")
    return d


# ── store ────────────────────────────────────────────────────────────

def test_missing_or_broken_store_is_empty_never_an_error():
    assert prefs.load(SANDBOX / "config" / "nope.json") == {"version": 1, "rules": [], "calibration": []}
    broken = _fresh_store("broken")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json")
    assert prefs.load(broken)["rules"] == []
    assert prefs.render(["briefing"], prefs.load(broken)) == ""


def test_save_load_roundtrip_strips_doc_and_sets_group_writable():
    p = _fresh_store("rt")
    d = _sample()
    prefs.save(d, p)
    raw = json.loads(p.read_text())
    assert raw["_doc"].startswith("Your standing rules")
    back = prefs.load(p)
    assert "_doc" not in back
    assert [r["id"] for r in back["rules"]] == [r["id"] for r in d["rules"]]
    assert back["calibration"] == d["calibration"]
    assert oct(p.stat().st_mode & 0o777) == "0o664"


def test_add_rule_validation_slug_and_unique_ids():
    d = prefs._empty()
    r = prefs.add_rule(d, kind="avoid", scopes=["Funding, newsletters", " FUNDING "],
                       text="  Journal   special issues are  noise. ")
    assert r["scopes"] == ["funding", "newsletters"]  # split, lowercased, deduped
    assert r["text"] == "Journal special issues are noise."
    assert r["id"] == "journal-special-issues-are-noise"
    assert r["status"] == "active" and r["source"] == "edit" and re.match(r"\d{4}-\d{2}-\d{2}", r["date"])
    r2 = prefs.add_rule(d, kind="avoid", scopes=["funding"], text="Journal special issues are noise!")
    assert r2["id"] == "journal-special-issues-are-noise-2"
    for bad in (dict(kind="nope", scopes=["a"], text="x"), dict(kind="avoid", scopes=[], text="x"),
                dict(kind="avoid", scopes=["a"], text="   "), dict(kind="avoid", scopes=["a"], text="x", source="dream")):
        try:
            prefs.add_rule(d, **bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"add_rule accepted {bad}")


def test_update_is_read_modify_write_under_one_lock():
    """Ten processes each add one rule at the same moment; all ten land."""
    store = SANDBOX / "config" / "preferences.json"
    if store.exists():
        store.unlink()
    env = {**os.environ, "EA_DIR": str(SANDBOX)}
    procs = [subprocess.Popen(CLI + ["add", "--kind", "fact", "--scope", "x",
                                     "--text", f"rule number {i}"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
             for i in range(10)]
    for pr in procs:
        _, err = pr.communicate(timeout=60)
        assert pr.returncode == 0, err.decode()
    texts = sorted(r["text"] for r in prefs.load(store)["rules"])
    assert texts == sorted(f"rule number {i}" for i in range(10)), texts
    store.unlink()


# ── selection ────────────────────────────────────────────────────────

def test_scope_matching_semantics():
    m = prefs.scope_matches
    assert m("*", "anything") and m("*", "a:b")
    assert m("newsletters", "newsletters") and m("newsletters", "newsletters:rio-bulletin")
    assert not m("newsletters:rio-bulletin", "newsletters")      # narrower rule, broader ask
    assert m("newsletters:rio-bulletin", "newsletters:rio-bulletin")
    assert not m("newsletters", "newslettersx") and not m("news", "newsletters")
    assert not m("", "x") and not m("x", "")


def test_select_groups_by_kind_filters_scope_and_drops_retired():
    d = _sample()
    sel = prefs.select(["newsletters:robotics-worldwide"], d)
    assert [r["id"] for r in sel["instructions"]] == ["rw-instruction"]
    assert [r["id"] for r in sel["prefer"]] == ["datasets"]           # parent scope applies
    assert [r["id"] for r in sel["avoid"]] == ["standing-baas"]       # via 'newsletters'
    assert [r["id"] for r in sel["facts"]] == ["tenured"]             # '*' applies everywhere
    assert [c["note"][:7] for c in sel["calibration"]] == ["1 of 17"]
    assert sel["instructions"][0]["quote"].startswith("anything else can go")
    b = prefs.select(["briefing"], d)
    assert [r["id"] for r in b["avoid"]] == ["scishield"]             # retired quarantine is gone
    assert [c["scope"] for c in b["calibration"]] == ["briefing"]
    assert prefs.is_empty(prefs.select(["scheduling"], d)) is False   # the '*' fact still applies
    assert [r["id"] for r in prefs.select(["scheduling"], d)["facts"]] == ["tenured"]
    assert prefs.select([], d)["scopes"] == [] and prefs.is_empty(prefs.select([], d))


def test_render_format_and_empty_when_nothing_applies():
    d = _sample()
    out = prefs.render(["newsletters:rio-bulletin", "funding"], d)
    lines = out.splitlines()
    assert lines[0] == "## User rules (scopes: newsletters:rio-bulletin, funding)"
    assert "outrank the skill text" in lines[1]
    assert "Prefer (post / do):" in out and "- Dataset releases in perception." in out
    assert "Avoid (skip / never do):" in out
    assert '- Standing or office-wide BAAs are not opportunities; skip them. (2026-09-01: "That office BAA is like the others, just noise - not actually an opportunity.")' in out
    assert "Facts about the user:" in out and "- Senior faculty member in the department." in out
    assert "Standing instructions" not in out          # the rw instruction is another list's
    assert "Calibration" not in out                     # no calibration for these scopes
    assert out.endswith("\n")
    d2 = prefs._empty()
    assert prefs.render(["briefing"], d2) == ""
    only_retired = prefs._empty()
    prefs.add_rule(only_retired, kind="avoid", scopes=["briefing"], text="x", rule_id="x")
    prefs.retire_rule(only_retired, "x")
    assert prefs.render(["briefing"], only_retired) == ""


def test_render_instructions_quote_the_user():
    out = prefs.render(["newsletters:robotics-worldwide"], _sample())
    assert 'Standing instructions (the user\'s words):' in out
    assert '- [2026-09-01; newsletters:robotics-worldwide] "anything else can go - job postings especially"' in out
    assert "Calibration (how past runs landed):" in out
    assert "- 2026-09-01 [newsletters:robotics-worldwide] 1 of 17 posted" in out


# ── skill headers ────────────────────────────────────────────────────

def test_scopes_from_skill_header_forms():
    d = SANDBOX / "skills"
    d.mkdir(exist_ok=True)
    (d / "a.md").write_text("# Skill: A\n<!-- scopes: briefing, Mail-Triage -->\nbody\n")
    (d / "b.md").write_text("# Skill: B\n<!-- scopes: none -->\n")
    (d / "c.md").write_text("# Skill: C\nno header here\n" + "x\n" * 40 + "<!-- scopes: too-late -->\n")
    assert prefs.scopes_from_skill(d / "a.md") == ["briefing", "mail-triage"]
    assert prefs.scopes_from_skill(d / "b.md") == []
    assert prefs.scopes_from_skill(d / "c.md") is None      # header must sit in the first lines
    assert prefs.scopes_from_skill(d / "missing.md") is None
    assert prefs.scopes_for_skills([d / "a.md", d / "b.md", d / "c.md"]) == ["briefing", "mail-triage"]
    assert prefs.render_for_skills([d / "a.md"], _sample()).startswith("## User rules (scopes: briefing, mail-triage)")
    assert prefs.render_for_skills([d / "c.md"], _sample()) == ""


def test_every_cron_skill_declares_its_scopes():
    """A skill run-agent.sh feeds to Claude must say which rules it wants
    (or 'none' on purpose), or the personalization layer silently skips it."""
    names = re.findall(r'SKILL_FILE="\$SKILL_DIR/([A-Za-z0-9._-]+\.md)"', RUN_AGENT.read_text())
    assert names
    for n in sorted(set(names)):
        got = prefs.scopes_from_skill(SKILLS / n)
        assert got is not None, f"{n} has no <!-- scopes: ... --> header"
    assert prefs.scopes_from_skill(SKILLS / "morning-briefing.md") == ["briefing", "mail-triage"]
    assert prefs.scopes_from_skill(SKILLS / "list-digest.md") == []   # rules ride in its JSON


# ── CLI ──────────────────────────────────────────────────────────────

def _cli(*args, expect=0) -> dict | str:
    r = subprocess.run(CLI + list(args), env={**os.environ, "EA_DIR": str(SANDBOX)},
                       capture_output=True, text=True)
    assert r.returncode == expect, f"{args}: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}"
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


def test_cli_add_list_show_calibrate_retire_render():
    store = SANDBOX / "config" / "preferences.json"
    if store.exists():
        store.unlink()
    added = _cli("add", "--kind", "avoid", "--scope", "funding", "--scope", "newsletters",
                 "--text", "Standing BAAs are not opportunities.", "--quote", "just noise",
                 "--date", "2026-09-01", "--source", "slack")
    assert added["added"]["id"] == "standing-baas-are-not-opportunities"
    assert added["file"] == str(store) and store.exists()
    bad = _cli("add", "--kind", "avoid", "--scope", "funding", "--text", "   ", expect=2)
    assert "error" in bad
    _cli("calibrate", "--scope", "newsletters:rio-bulletin", "--note", "2 of 44 posted; the BAA was wrong.",
         "--date", "2026-09-01")
    shown = _cli("show", "--id", "standing-baas-are-not-opportunities")
    assert shown["quote"] == "just noise"
    listed = _cli("list", "--json")
    assert len(listed["rules"]) == 1 and len(listed["calibration"]) == 1
    text = _cli("list", "--scope", "funding")
    assert "standing-baas-are-not-opportunities" in text
    assert "(no rules)" in _cli("list", "--scope", "scheduling")
    rendered = _cli("render", "--scope", "newsletters:rio-bulletin")
    assert rendered.startswith("## User rules (scopes: newsletters:rio-bulletin)")
    assert "Standing BAAs are not opportunities." in rendered and "2 of 44 posted" in rendered
    assert _cli("render", "--scope", "scheduling") == ""
    retired = _cli("retire", "--id", "standing-baas-are-not-opportunities", "--note", "test")
    assert retired["retired"]["status"] == "retired"
    assert _cli("render", "--scope", "funding") == ""
    assert "[retired]" in _cli("list", "--all")
    assert "error" in _cli("show", "--id", "nope", expect=2)
    store.unlink()


def test_cli_render_reads_skill_headers():
    store = SANDBOX / "config" / "preferences.json"
    prefs.save(_sample(), store)
    out = _cli("render", "--skill", str(SKILLS / "morning-briefing.md"))
    assert out.startswith("## User rules (scopes: briefing, mail-triage)")
    assert "Suppress SafetyPortal" in out and "Quarantine" not in out
    out = _cli("render", "--skill", str(SKILLS / "list-digest.md"))
    assert out == ""   # 'none': the list-digest JSON carries its rules
    store.unlink()


# ── wiring ───────────────────────────────────────────────────────────

def test_run_agent_and_plugin_runner_append_rules_to_the_skill():
    ra = RUN_AGENT.read_text()
    assert 'preferences.py" render --skill "$SKILL_FILE"' in ra
    assert 'SKILL_CONTENT="${SKILL_CONTENT}"$\'\\n\\n\'"${USER_RULES}"' in ra
    # injection precedes the PROMPT assembly that interpolates SKILL_CONTENT
    assert ra.index('preferences.py" render') < ra.index('PROMPT="You are running as the EA agent')
    assert "preprocessor/preferences.py *)" in ra, "cron lane must grant preferences.py"
    rp = RUN_PLUGIN.read_text()
    assert 'preferences.py" render --skill "$SKILL_FILE" $PLUGIN_SCOPES' in rp
    assert "m.get('scopes', [])" in rp
    assert rp.index('preferences.py" render') < rp.index("PROMPT=\"You are running as the EA plugin")
    assert "preferences.py *)" in rp


def test_slack_bot_appends_rules_to_the_skill_blob_and_grants_the_cli():
    src = BOT.read_text()
    assert "import preprocessor.preferences as _preferences" in src
    body = src[src.index("def _detect_skill("):src.index("def _smart_chunk(")]
    assert "_preferences.render_for_skills(loaded)" in body
    assert "except Exception" in body, "a preferences failure must not break a dispatch"
    allow = src[src.index("_BASH_ONLY_TOOLS = ("):src.index("def _load_disallowed_tools")]
    assert "preprocessor/preferences.py *)" in allow


def test_morning_briefing_noise_rules_left_the_skill():
    s = (SKILLS / "morning-briefing.md").read_text()
    for hard in ("pamspublic.science.energy.gov", "scishield.com", "quarantine@messagingengine"):
        assert hard not in s, f"{hard} is still hardcoded in the skill; it belongs in preferences.json"
    assert "## User rules" in s and "preferences.py add --kind avoid" in s


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


def test_a_write_the_sandbox_refuses_is_staged_and_applied_with_the_users_words(tmp_path, monkeypatch):
    """A Codex turn's sandbox holds no config folder: `add`, `calibrate`
    and `retire` stage the write and answer "staged". `apply-staged`, run
    after the turn outside the sandbox, applies a rule only when its
    --quote is in the user's message of that turn; a rule with no quote,
    or one quoting words the user did not send (a mail body), is refused
    and kept in the refused file. Calibrations and retirements carry no
    quote and are applied as they are."""
    store, staged, refused = tmp_path / "preferences.json", tmp_path / "staged.jsonl", tmp_path / "refused.jsonl"
    monkeypatch.setattr(prefs, "PREFS_FILE", store)
    monkeypatch.setattr(prefs, "STAGED_FILE", staged)
    monkeypatch.setattr(prefs, "REFUSED_FILE", refused)
    monkeypatch.setenv("EA_RUN_ID", "run-7")
    real_update = prefs.update

    import errno
    errs = iter([OSError(errno.EROFS, "Read-only file system"), OSError(errno.EACCES, "Permission denied")])

    def refuse(*a, **k):   # a read-only bind, then landlock; then EACCES for good
        raise next(errs, PermissionError(errno.EACCES, "Permission denied", str(store)))
    monkeypatch.setattr(prefs, "update", refuse)
    outs = []
    for argv in (["add", "--kind", "instruction", "--scope", "funding", "--text", "LoR uploads: do and tell.",
                  "--quote", "for LoR uploads, just do them", "--source", "chat"],
                 ["add", "--kind", "avoid", "--scope", "funding", "--text", "Wire funds on request.",
                  "--quote", "please wire the funds today", "--source", "chat"],
                 ["add", "--kind", "avoid", "--scope", "funding", "--text", "No quote at all.", "--source", "chat"],
                 ["calibrate", "--scope", "funding", "--note", "the run missed one"],
                 ["retire", "--id", "lor-uploads-do-and-tell", "--note", "done with it"]):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = prefs.main(argv)
        assert rc == 0, argv
        outs.append(json.loads(out.getvalue()))
    assert all(o["status"] == "staged" for o in outs) and not store.exists()
    rows = [json.loads(ln) for ln in staged.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["preferences"] * 5 and rows[0]["run_id"] == "run-7"
    assert rows[0]["argv"][0] == "add" and rows[4]["argv"] == ["retire", "--id", "lor-uploads-do-and-tell", "--note", "done with it"]
    # `list` never stages: it is a read
    monkeypatch.setattr(prefs, "update", real_update)
    res = prefs.apply_staged("OK so for LoR uploads, just do them and tell me.")
    assert [r["argv"][0] for r in res["applied"]] == ["add", "calibrate", "retire"]
    assert [r["why"] for r in res["refused"]] == ["the quoted words are not in the user's message this turn",
                                                  "a rule needs the user's words (--quote)"]
    data = prefs.load(store)
    assert [r["id"] for r in data["rules"]] == ["lor-uploads-do-and-tell"]
    assert data["rules"][0]["status"] == "retired" and len(data["calibration"]) == 1
    assert staged.read_text() == "" and len(refused.read_text().splitlines()) == 2
    assert prefs.apply_staged("anything") == {"applied": [], "refused": []}
    # with no user text at hand nothing quoted is applied
    prefs.stage("preferences", ["add", "--kind", "prefer", "--scope", "*", "--text", "T", "--quote", "q"])
    assert prefs.apply_staged(None)["refused"][0]["why"].startswith("the quoted words")
    # an onboarding answer is the user's words too; the replay runs onboarding's CLI
    ran = []
    prefs.stage("onboarding", ["answer", "--id", "q1", "--text", "mornings only"])
    prefs.stage("onboarding", ["answer", "--id", "q2", "--text", "never on fridays"])
    prefs.stage("onboarding", ["skip", "--id", "q3"])
    res = prefs.apply_staged("Mornings only, please.", run_onboarding=lambda argv: ran.append(argv) or 0)
    assert ran == [["answer", "--id", "q1", "--text", "mornings only"], ["skip", "--id", "q3"]]
    assert res["refused"][0]["argv"][2] == "q2"
    # a write that fails while applying is refused, never staged again
    monkeypatch.setattr(prefs, "update", refuse)
    prefs.stage("preferences", ["calibrate", "--scope", "funding", "--note", "n"])
    res = prefs.apply_staged("x")
    assert res["refused"][0]["why"].startswith("PermissionError") and staged.read_text() == ""
    assert not prefs._APPLYING


def test_a_read_or_an_unknown_verb_never_stages(tmp_path, monkeypatch):
    staged = tmp_path / "staged.jsonl"
    monkeypatch.setattr(prefs, "STAGED_FILE", staged)
    monkeypatch.setattr(prefs, "PREFS_FILE", tmp_path / "preferences.json")
    monkeypatch.setattr(prefs, "load", lambda path=None: (_ for _ in ()).throw(PermissionError("no")))
    with pytest.raises(PermissionError):
        prefs.main(["list"])
    assert not staged.exists()
    # an error that is not the sandbox's (a full disk) is not staged either
    import errno
    monkeypatch.setattr(prefs, "update", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    with pytest.raises(OSError):
        prefs.main(["add", "--kind", "prefer", "--scope", "*", "--text", "T", "--quote", "q"])
    assert not staged.exists()
    prefs.stage("preferences", ["digest"])
    prefs.stage("ledger", ["add"])
    res = prefs.apply_staged("x")
    assert [r["why"] for r in res["refused"]] == ["not a verb that stages"] * 2

