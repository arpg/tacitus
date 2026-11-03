"""autonomy_tiers.py: a tier promotion is a verb (CLAUDE.md rule 12), so a
Codex turn, whose sandbox holds no config folder, stages it like a
preference and the bot applies it after the turn when the quote is the
user's words."""
import errno
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

# Sandbox before import: the modules derive their paths from EA_DIR, and
# resident_io writes a sandbox directly (an owner process writes resident
# space through the resident child, which no test runs).
SANDBOX = Path(tempfile.mkdtemp(prefix="ea-test-tiers-"))
os.environ["EA_DIR"] = str(SANDBOX)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import autonomy_tiers as at  # noqa: E402
import preferences as prefs  # noqa: E402

CONFIG = {
    "task_tiers": {
        "send_email": {"tier": "button-required", "note": "the Send button"},
        "upload_letter_of_recommendation": {"tier": "ask-first", "note": "LoR"},
        "send_scheduling_availability": {"tier": "ask-first", "promotable_per_contact": True, "note": "times"},
    },
    "per_contact_overrides": {"_doc": "per contact"},
}


def _config(tmp: Path) -> Path:
    path = tmp / "config" / "autonomy-tiers.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(CONFIG, indent=2))
    return path


def _run(argv, **patch):
    saved = {k: getattr(at, k) for k in patch}
    for k, v in patch.items():
        setattr(at, k, v)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            rc = at.main(argv)
    finally:
        for k, v in saved.items():
            setattr(at, k, v)
    return rc, json.loads(out.getvalue())


def test_a_promotion_changes_the_task_or_the_contact_and_keeps_the_users_words():
    tmp = Path(tempfile.mkdtemp(prefix="ea-tiers-"))
    path = _config(tmp)
    rc, out = _run(["promote", "--task", "upload_letter_of_recommendation", "--tier", "do-and-tell",
                    "--quote", "for LoR uploads, just do them and tell me"], TIERS_FILE=path)
    assert rc == 0 and out["promoted"] == {"task": "upload_letter_of_recommendation", "tier": "do-and-tell",
                                           "was": "ask-first"}
    data = json.loads(path.read_text())
    row = data["task_tiers"]["upload_letter_of_recommendation"]
    assert row["tier"] == "do-and-tell" and row["promoted"]["quote"] == "for LoR uploads, just do them and tell me"
    assert row["promoted"]["from"] == "ask-first" and row["promoted"]["source"] == "chat" and row["note"] == "LoR"
    rc, out = _run(["promote", "--task", "send_scheduling_availability", "--tier", "do-and-tell",
                    "--contact", "Dr.Smith@Example.edu", "--quote", "for scheduling with Dr. Smith, just send it"],
                   TIERS_FILE=path)
    assert rc == 0 and out["promoted"]["contact"] == "dr.smith@example.edu"
    data = json.loads(path.read_text())
    over = data["per_contact_overrides"]["dr.smith@example.edu"]
    assert over["send_scheduling_availability"] == "do-and-tell"
    assert over["_promoted"]["send_scheduling_availability"]["quote"].startswith("for scheduling")
    assert data["task_tiers"]["send_scheduling_availability"]["tier"] == "ask-first", "the task itself is unchanged"
    assert data["per_contact_overrides"]["_doc"] == "per contact"
    rc, out = _run(["show", "--task", "upload_letter_of_recommendation"], TIERS_FILE=path)
    assert rc == 0 and list(out["task_tiers"]) == ["upload_letter_of_recommendation"]
    assert "dr.smith@example.edu" in out["per_contact_overrides"] and "_doc" not in out["per_contact_overrides"]


def test_a_button_an_unknown_task_or_a_task_with_no_contact_tier_is_refused():
    tmp = Path(tempfile.mkdtemp(prefix="ea-tiers-"))
    path = _config(tmp)
    before = path.read_text()
    for argv, why in ((["promote", "--task", "send_email", "--tier", "do-and-tell", "--quote", "just send"],
                       "button-required"),
                      (["promote", "--task", "upload_letter_of_recommendation", "--tier", "button-required",
                        "--quote", "q"], "button-required"),
                      (["promote", "--task", "nope", "--tier", "do-and-tell", "--quote", "q"], "no task"),
                      (["promote", "--task", "upload_letter_of_recommendation", "--tier", "do-and-tell",
                        "--contact", "a@example.edu", "--quote", "q"], "no per-contact tier"),
                      (["promote", "--task", "upload_letter_of_recommendation", "--tier", "do-and-tell",
                        "--quote", "   "], "--quote is required")):
        rc, out = _run(argv, TIERS_FILE=path)
        assert rc == 2 and why in out["error"], argv
    assert path.read_text() == before


def test_from_a_sandbox_the_promotion_is_staged_and_applied_with_the_users_words():
    tmp = Path(tempfile.mkdtemp(prefix="ea-tiers-"))
    path = _config(tmp)
    staged, refused = tmp / "staged.jsonl", tmp / "refused.jsonl"
    saved = (prefs.STAGED_FILE, prefs.REFUSED_FILE, at._resident_io.write_refused)
    prefs.STAGED_FILE, prefs.REFUSED_FILE = staged, refused
    try:
        argv = ["promote", "--task", "upload_letter_of_recommendation", "--tier", "do-and-tell",
                "--quote", "for LoR uploads, just do them"]
        for code in (errno.EACCES, errno.EROFS):
            at._resident_io.write_refused = lambda p, code=code: code
            rc, out = _run(argv, TIERS_FILE=path)
            assert rc == 0 and out["status"] == "staged"
        rows = [json.loads(ln) for ln in staged.read_text().splitlines()]
        assert [r["tool"] for r in rows] == ["autonomy", "autonomy"] and rows[0]["argv"] == argv
        assert json.loads(path.read_text()) == CONFIG, "nothing written from the sandbox"
        # any other answer from the probe is not the sandbox: the write is tried
        at._resident_io.write_refused = lambda p: errno.ENOSPC
        rc, out = _run(["show"], TIERS_FILE=path)
        assert rc == 0 and "status" not in out
        at._resident_io.write_refused = saved[2]
        assert at._resident_io.write_refused(path) is None and at._resident_io.write_refused(tmp / "none") == errno.ENOENT
        # the apply: the quote must be the user's words of the turn
        live_tiers, at.TIERS_FILE = at.TIERS_FILE, path
        try:
            res = prefs.apply_staged("ok, for LoR uploads, just do them")
            assert len(res["applied"]) == 2 and not res["refused"]
            prefs.stage("autonomy", argv)
            res = prefs.apply_staged("file it under the minor project")
        finally:
            at.TIERS_FILE = live_tiers
        assert json.loads(path.read_text())["task_tiers"]["upload_letter_of_recommendation"]["tier"] == "do-and-tell"
        assert res["refused"][0]["why"].startswith("the quoted words")
        assert "autonomy" in prefs.STAGED_VERBS and prefs.claimed_words("autonomy", argv) == argv[-1]
    finally:
        prefs.STAGED_FILE, prefs.REFUSED_FILE, at._resident_io.write_refused = saved


def test_the_verb_is_granted_where_preferences_is_and_the_core_names_it():
    for path, needle in ((ROOT / "run-agent.sh", "preprocessor/autonomy_tiers.py *)"),
                         (ROOT / "ea-chat-bot.py", "preprocessor/autonomy_tiers.py *),"),
                         (ROOT / "CLAUDE.md", "autonomy_tiers.py promote")):
        assert needle in path.read_text(), path.name
    assert "autonomy_tiers.py promote" in (ROOT / "preprocessor" / "codex_ghost.py").read_text()
    assert "preprocessor/autonomy_tiers.py" in (ROOT / "docs" / "ARCHITECTURE.md").read_text()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n  {len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
