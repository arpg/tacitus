"""A ledger description shown in a fixed width is cut on a word with an
ellipsis, never mid-word (the afternoon update once read "send the
CSCI 5848 recomme"), and `add` says when a description is longer than
a title."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import ledger  # noqa: E402
import preprocess  # noqa: E402

LONG = ("Committee Oct 21: after the meeting, act on the takeaways table in the brief's Oct 21 tab: "
        "send the CSCI 5848 recommendation to the graduate committee, write the chair notes")


def test_brief_title_cuts_on_a_word_and_marks_the_cut():
    t = ledger.brief_title(LONG, 120)
    assert len(t) <= 120 and t.endswith("…") and t.endswith("CSCI 5848…")
    assert ledger.brief_title("short title", 120) == "short title"
    assert ledger.brief_title(None, 10) == "" and ledger.brief_title("  two   spaces ", 40) == "two spaces"
    assert ledger.brief_title("a" * 30, 10) == "a" * 9 + "…", "no space: a plain cut"
    assert not ledger.brief_title("word, " * 40, 20).endswith(",…"), "no dangling punctuation"


def test_every_brief_width_goes_through_the_helper():
    src = (ROOT / "preprocessor" / "ledger.py").read_text()
    assert 'brief_title(r["description"], 120)' in src and 'brief_title(r[1], 60)' in src
    assert '[:120]' not in src.split("def get_closed_entries_brief")[1].split("def _derive_conversation_id")[0]
    pre = (ROOT / "preprocessor" / "preprocess.py").read_text()
    assert '_title(ob.get("description"), 160)' in pre and '_title(ob.get("description"), 100)' in pre
    assert preprocess._title(LONG, 100).endswith("…") and len(preprocess._title(LONG, 100)) <= 100


def test_add_notes_a_description_longer_than_a_title(tmp_path):
    env = {"EA_DIR": str(tmp_path), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    (tmp_path / "state").mkdir()
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "ledger.py"), "add", "--id", "t-long",
                        "--type", "task", "--description", LONG], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "character description" in r.stderr and "--notes" in r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "ledger.py"), "add", "--id", "t-short",
                        "--type", "task", "--description", "Send the committee recommendation"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "character description" not in r.stderr
