"""First-week onboarding (preprocessor/onboarding.py): the question bank
validates against the preferences vocabulary, the verbs record answers
as rules in a scratch store, one question is asked a day, and the
morning briefing's question block passes the eval judge's format check."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests" / "evals"))

# The judge reads the profile at import; preprocess makes its state dir at
# import. Both must see a sandbox, never the live tree.
_EA = Path(tempfile.mkdtemp(prefix="ea-test-onboarding-"))
(_EA / "config").mkdir()
(_EA / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
os.environ["EA_DIR"] = str(_EA)

import onboarding  # noqa: E402
import preferences as prefs  # noqa: E402

CLI = [sys.executable, str(REPO / "preprocessor" / "onboarding.py")]
BANK = onboarding.load_bank()
# The questions a test install can use: one that waits on a local model
# (the keep rule for applicant mail) is not asked without one.
OPEN = [q for q in BANK if not q.get("requires")]
# Spelled by code point: the character itself stays out of the tree.
EM_DASH = chr(0x2014)
MORNING = (REPO / "skills" / "morning-briefing.md").read_text()
AFTERNOON = (REPO / "skills" / "afternoon-catchup.md").read_text()


def _run(ea_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "EA_DIR": str(ea_dir)}
    return subprocess.run(CLI + list(args), env=env, capture_output=True, text=True, timeout=60)


# ── the bank ─────────────────────────────────────────────────────────

def test_bank_validates_against_the_preferences_vocabulary():
    assert onboarding.validate_bank(BANK) == []
    assert 10 <= len(BANK) <= 14
    ids = [q["id"] for q in BANK]
    assert len(ids) == len(set(ids))
    for q in BANK:
        assert q["kind"] in prefs.KINDS
        assert q["scope"].split(":")[0] in prefs.SCOPES
        assert q["question"].endswith("?") and q["question"].count(". ") == 0
        for f in ("question", "why", "example", "topic"):
            assert EM_DASH not in q[f], f"{q['id']}: em-dash in {f}"


def test_bank_covers_the_first_week_topics():
    ids = {q["id"] for q in BANK}
    for needed in ("vip-senders", "skip-lists", "urgent-means", "protected-days",
                   "quiet-times", "sign-off", "never-draft-to", "standing-facts",
                   "briefing-length", "never-alone"):
        assert needed in ids, needed


def test_scheduling_questions_promise_only_what_the_code_does():
    """scheduling_engine.py takes its rules from the EA-Managed calendar,
    never from preferences.json, so a scheduling answer is a standing
    rule for drafts and the briefing, and the bank says how to make it a
    hard block. Working hours are asked by /setup, not here."""
    ids = [q["id"] for q in BANK]
    assert "meeting-edges" not in ids, "repeats the working-hours question"
    engine = (REPO / "preprocessor" / "scheduling_engine.py").read_text()
    assert '"preferences.json"' not in engine and "import preferences" not in engine
    for q in BANK:
        if q["scope"] == "scheduling":
            assert "never offered" not in q["why"] and "solver drops" not in q["why"].lower()
            assert "EA-Managed calendar" in q["why"], q["id"]


def test_validate_bank_names_each_problem():
    bad = [{"id": "a", "scope": "nowhere", "kind": "wish", "topic": "t",
            "question": "q?", "why": "w", "example": "e"},
           {"id": "a", "scope": "briefing", "kind": "fact", "topic": "",
            "question": "q?", "why": "w", "example": "e"}]
    problems = onboarding.validate_bank(bad)
    assert any("scope 'nowhere'" in p for p in problems)
    assert any("kind 'wish'" in p for p in problems)
    assert any("duplicate id" in p for p in problems)
    assert any("missing topic" in p for p in problems)


# ── the verbs ────────────────────────────────────────────────────────

def test_next_answer_skip_status_and_the_rule_in_the_store(tmp_path):
    first = onboarding.next_questions(ea_dir=tmp_path)
    assert [q["id"] for q in first] == [BANK[0]["id"]]
    assert first[0]["remaining"] == len(OPEN) and first[0]["total"] == len(BANK)
    assert first[0]["question"] and first[0]["why"] and first[0]["example"]

    res = onboarding.record_answer(BANK[0]["id"], "  My chair  and my students. ", ea_dir=tmp_path)
    rule = res["rule"]
    assert rule["id"] == "onboarding-" + BANK[0]["id"]
    assert rule["kind"] == BANK[0]["kind"] and rule["scopes"] == [BANK[0]["scope"]]
    assert rule["quote"] == "My chair and my students."
    assert rule["text"] == f"{BANK[0]['topic']}: My chair and my students."
    assert rule["source"] == "onboarding" and rule["status"] == "active"
    store = prefs.load(tmp_path / "config" / "preferences.json")
    assert [r["id"] for r in store["rules"]] == [rule["id"]]
    # The rule reaches a prompt for its scope.
    assert "My chair and my students." in prefs.render([BANK[0]["scope"]], store)

    onboarding.skip_question(BANK[1]["id"], ea_dir=tmp_path)
    st = onboarding.status(ea_dir=tmp_path)
    assert st["answered"] == [BANK[0]["id"]] and st["skipped"] == [BANK[1]["id"]]
    assert st["remaining"] == [q["id"] for q in OPEN[2:]]
    assert st["enabled"] and not st["done"] and not st["complete"]

    state = json.loads((tmp_path / "state" / "onboarding.json").read_text())
    assert state["answered"][BANK[0]["id"]]["rule_id"] == rule["id"]
    assert BANK[1]["id"] in state["skipped"]


def test_a_second_answer_retires_the_first_rule(tmp_path):
    qid = BANK[2]["id"]
    onboarding.record_answer(qid, "first words", ea_dir=tmp_path)
    second = onboarding.record_answer(qid, "second words", ea_dir=tmp_path)["rule"]
    store = prefs.load(tmp_path / "config" / "preferences.json")
    by_id = {r["id"]: r for r in store["rules"]}
    assert by_id["onboarding-" + qid]["status"] == "retired"
    assert second["id"] == "onboarding-" + qid + "-2" and second["status"] == "active"
    assert onboarding.status(ea_dir=tmp_path)["answered"] == [qid]


def test_answer_rejects_an_unknown_id_and_an_empty_answer(tmp_path):
    with pytest.raises(KeyError):
        onboarding.record_answer("no-such-question", "x", ea_dir=tmp_path)
    with pytest.raises(ValueError):
        onboarding.record_answer(BANK[0]["id"], "   ", ea_dir=tmp_path)
    assert not (tmp_path / "config" / "preferences.json").exists()


def test_stop_marks_the_bank_done(tmp_path):
    onboarding.stop(ea_dir=tmp_path)
    assert onboarding.next_questions(ea_dir=tmp_path, force=True) == []
    st = onboarding.status(ea_dir=tmp_path)
    assert st["done"] and st["remaining"]


def test_config_switch_turns_the_questions_off(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(
        json.dumps({"onboarding": {"enabled": False}}))
    assert onboarding.next_questions(ea_dir=tmp_path) == []
    assert not onboarding.status(ea_dir=tmp_path)["enabled"]
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"profile": "email"}))
    assert onboarding.enabled(ea_dir=tmp_path)


def test_scope_filter_and_count(tmp_path):
    sched = onboarding.next_questions(count=5, scope="scheduling", ea_dir=tmp_path, record=False)
    assert sched and all(q["scope"] == "scheduling" for q in sched)
    assert len(sched) == sum(1 for q in BANK if q["scope"] == "scheduling")
    three = onboarding.next_questions(count=3, ea_dir=tmp_path, record=False)
    assert [q["id"] for q in three] == [q["id"] for q in BANK[:3]]
    assert [q["remaining"] for q in three] == [len(OPEN), len(OPEN) - 1, len(OPEN) - 2]


# ── one a day ────────────────────────────────────────────────────────

def test_one_question_a_day(tmp_path):
    day1 = datetime(2026, 3, 2, 6, 0)
    first = onboarding.next_questions(ea_dir=tmp_path, now=day1)
    assert len(first) == 1
    # Asked today and still open: nothing more today, unless forced.
    assert onboarding.next_questions(ea_dir=tmp_path, now=day1.replace(hour=15)) == []
    forced = onboarding.next_questions(ea_dir=tmp_path, now=day1.replace(hour=15), force=True)
    assert [q["id"] for q in forced] == [first[0]["id"]]
    # Answered: the next one may follow.
    onboarding.record_answer(first[0]["id"], "answered", ea_dir=tmp_path, now=day1)
    second = onboarding.next_questions(ea_dir=tmp_path, now=day1.replace(hour=16))
    assert [q["id"] for q in second] == [BANK[1]["id"]]
    # Left open overnight: the same question comes back the next day.
    day2 = datetime(2026, 3, 3, 6, 0)
    again = onboarding.next_questions(ea_dir=tmp_path, now=day2)
    assert [q["id"] for q in again] == [BANK[1]["id"]]
    assert onboarding.status(ea_dir=tmp_path, now=day2)["asked_today"] == BANK[1]["id"]
    # A peek records nothing.
    fresh = tmp_path / "peek"
    onboarding.next_questions(ea_dir=fresh, now=day1, record=False)
    assert not (fresh / "state" / "onboarding.json").exists()
    assert len(onboarding.next_questions(ea_dir=fresh, now=day1)) == 1


# ── the CLI ──────────────────────────────────────────────────────────

def test_cli_next_ask_answer_skip_stop_status(tmp_path):
    r = _run(tmp_path, "next")
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [q["id"] for q in rows] == [BANK[0]["id"]]
    assert json.loads(_run(tmp_path, "next").stdout) == []

    r = _run(tmp_path, "ask", "--count", "3", "--force")
    assert r.returncode == 0, r.stderr
    assert BANK[0]["question"] in r.stdout and BANK[2]["question"] in r.stdout
    assert f'onboarding.py answer --id {BANK[0]["id"]} --text' in r.stdout
    assert f"onboarding.py skip --id {BANK[1]['id']}" in r.stdout
    assert EM_DASH not in r.stdout

    r = _run(tmp_path, "answer", "--id", BANK[0]["id"], "--text", "Never bury my chair.")
    assert r.returncode == 0, r.stderr
    first_line = json.loads(r.stdout.splitlines()[0])
    assert first_line["recorded"]["quote"] == "Never bury my chair."
    assert "Recorded rule onboarding-" + BANK[0]["id"] in r.stdout
    assert _run(tmp_path, "answer", "--id", "nope", "--text", "x").returncode == 2

    assert json.loads(_run(tmp_path, "skip", "--id", BANK[1]["id"]).stdout)["skipped"] == BANK[1]["id"]
    st = json.loads(_run(tmp_path, "status", "--json").stdout)
    assert st["answered"] == [BANK[0]["id"]] and st["skipped"] == [BANK[1]["id"]]
    text = _run(tmp_path, "status").stdout
    assert f"{len(OPEN) - 2} of {len(BANK)} questions remain" in text

    assert json.loads(_run(tmp_path, "stop").stdout) == {"done": True}
    assert json.loads(_run(tmp_path, "next", "--force").stdout) == []
    assert _run(tmp_path, "ask").stdout.strip() == "No question today."
    assert "done (the user said stop)" in _run(tmp_path, "status").stdout

    store = prefs.load(tmp_path / "config" / "preferences.json")
    assert store["rules"][0]["source"] == "onboarding"


# ── the briefing lanes ───────────────────────────────────────────────

def test_the_preprocessor_carries_the_question_of_the_day():
    from preprocessor import preprocess
    row = preprocess._onboarding_question()
    assert row and row["id"] == BANK[0]["id"]
    assert preprocess._onboarding_question() is None  # asked today, still open
    assert '"onboarding_question": _onboarding_question()' in (
        REPO / "preprocessor" / "preprocess.py").read_text()


def test_the_morning_skill_asks_and_the_catchup_only_records():
    assert "### 9a. First week" in MORNING
    assert ":seedling: *One question*" in MORNING
    assert "`onboarding_question`" in MORNING
    assert 'onboarding.py answer\n--id <id> --text "<their words>"' in MORNING
    assert "onboarding.py skip --id <id>" in MORNING and "onboarding.py stop" in MORNING
    assert MORNING.index("### 9a. First week") < MORNING.index("### 10. WRITE THE BRIEFING")
    assert "never asks a first-week question" in AFTERNOON
    assert "onboarding.py answer --id <id>" in AFTERNOON.replace("\n  ", " ")
    assert "onboarding.py" in (REPO / "CLAUDE.md").read_text()
    assert "onboarding_question" in (REPO / "docs" / "ea" / "briefing-data.md").read_text()


def test_the_judge_accepts_a_briefing_that_ends_with_the_question_block():
    import judge
    q = BANK[0]
    briefing = (
        ":sunny: *Morning Briefing " + EM_DASH + " Monday, March 2*\n\n"
        ":dart: *Get done today*\n1. Reply to the review request before your 9:30.\n\n"
        ":leaves: *Flexes*\n- Read the draft agenda.\n\n"
        ":clipboard: 9:30 lab meeting · 2 advising\n\n"
        ":white_check_mark: *Quietly handled*: 2 drafts staged.\n\n"
        "Open obligations: 4 | Inbox: 12 messages\n\n"
        f":seedling: *One question* ({len(BANK)} left)\n"
        f"{q['question']} {q['why']} Example: {q['example']} Reply in this thread "
        "in your own words; say *skip* to pass or *stop asking* to end the questions.\n")
    verdict = judge.briefing_format_valid(briefing, {})
    assert verdict["pass"], verdict["reason"]


def test_an_answer_the_sandbox_refuses_is_staged_for_the_bot(tmp_path, monkeypatch):
    """From a Codex turn the preferences store is not writable: the answer
    is staged (preferences.STAGED_FILE) and applied after the turn when
    the text is the user's words; the question is not marked answered
    until then."""
    import io
    from contextlib import redirect_stdout
    staged = tmp_path / "staged.jsonl"
    monkeypatch.setattr(prefs, "STAGED_FILE", staged)
    monkeypatch.setattr(prefs, "update", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")))
    qid = BANK[0]["id"]
    out = io.StringIO()
    with redirect_stdout(out):
        rc = onboarding.main(["answer", "--id", qid, "--text", "mornings only"])
    assert rc == 0 and json.loads(out.getvalue())["status"] == "staged"
    row = json.loads(staged.read_text())
    assert row["tool"] == "onboarding" and row["argv"] == ["answer", "--id", qid, "--text", "mornings only"]
    assert onboarding.load_state()["answered"].get(qid) is None

