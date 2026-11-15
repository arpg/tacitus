"""ea_written: the matcher the style profiles use to leave out mail the EA
wrote, over draft_capture's store and the writer's logs. Nothing here
reaches the network or the real private dir."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import draft_capture  # noqa: E402
import ea_written as ew  # noqa: E402

EA_BODY = ("Hi Pat,\n\nThe lab can host the visit on the ninth. We need the sensor "
           "mount finished before then, and two people from your team for the "
           "outdoor runs. Could you confirm who will come?\n\nKit")
EDITED = ("Hi Pat,\n\nThe lab can host the visit on the ninth (finally!). We need the "
          "sensor mount finished before then, and two people from your team for the "
          "outdoor runs. Could you confirm who will come?\n\nKit")
REWRITTEN = ("Pat, quick one: can two of your folks make the ninth? Mount has to be "
             "done first. Kit")
UNRELATED = ("Hi Pat,\n\nI am out next week, so the review meeting moves to the "
             "following Tuesday. Could you tell the others?\n\nKit")


@pytest.fixture
def private(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(draft_capture, "load_all", lambda: [])
    return d


def _matcher(*recs):
    return ew.Matcher(list(recs))


def test_a_sent_copy_of_the_ea_body_matches_even_after_edits():
    m = _matcher({"ts": "2026-03-01T10:00:00Z", "conversation_id": "c1", "text": EA_BODY})
    assert m.is_ea_written(EA_BODY, "c1", "2026-03-01T11:00:00Z")
    assert m.is_ea_written(EDITED, "c1", "2026-03-01T11:00:00Z")
    assert not m.is_ea_written(REWRITTEN, "c1", "2026-03-01T11:00:00Z")
    assert not m.is_ea_written(UNRELATED, "c1", "2026-03-01T11:00:00Z")
    # Another conversation's body is not this message's.
    assert not m.is_ea_written(EA_BODY, "c2", "2026-03-01T11:00:00Z")


def test_shared_common_words_alone_do_not_match():
    m = _matcher({"ts": "2026-03-01T10:00:00Z", "conversation_id": "c1", "text": EA_BODY})
    assert m.share(UNRELATED, "c1", "2026-03-01T11:00:00Z") < 0.2


def test_a_body_written_after_the_send_is_a_later_draft():
    m = _matcher({"ts": "2026-03-02T10:00:00Z", "conversation_id": "c1", "text": EA_BODY})
    assert not m.is_ea_written(EA_BODY, "c1", "2026-03-01T11:00:00Z")


def test_a_body_with_no_conversation_matches_inside_the_window():
    m = _matcher({"ts": "2026-03-01T10:00:00Z", "text": EA_BODY})
    assert m.is_ea_written(EA_BODY, "c7", "2026-03-05T10:00:00Z")
    assert not m.is_ea_written(EA_BODY, "c7", "2026-04-01T10:00:00Z")


def test_load_reads_the_captures_and_the_writer_logs(private, monkeypatch):
    monkeypatch.setattr(draft_capture, "load_all", lambda: [
        {"draft_id": "d1", "conversation_id": "c1", "captured_at": "2026-03-01T10:00:00Z",
         "body": "<div>" + EA_BODY + "</div>"}])
    hist = private / "remote-llm"
    hist.mkdir()
    (hist / "draft-writer.jsonl").write_text(json.dumps(
        {"ts": "2026-03-01T10:00:00Z", "conversation_id": "c2", "body": UNRELATED}) + "\n")
    (hist / "draft-ab.jsonl").write_text(json.dumps(
        {"ts": "2026-03-01T10:00:00Z", "conversation_id": "c3", "qwen_body": REWRITTEN,
         "claude_body": ""}) + "\nnot json\n")
    m = ew.load()
    assert m.available and len(m) == 3
    assert m.is_ea_written(EA_BODY, "c1", "2026-03-01T12:00:00Z")
    assert m.is_ea_written(UNRELATED, "c2", "2026-03-01T12:00:00Z")


def test_a_private_dir_with_no_writer_logs_is_a_complete_answer(private):
    m = ew.load()
    assert m.available and len(m) == 0


def test_an_unreadable_private_dir_still_uses_the_captures(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(draft_capture, "load_all", lambda: [
        {"conversation_id": "c1", "captured_at": "2026-03-01T10:00:00Z", "body": EA_BODY}])
    m = ew.load()
    assert not m.available and len(m) == 1
    assert m.is_ea_written(EA_BODY, "c1", "2026-03-01T11:00:00Z")


def test_a_failed_capture_read_adds_nothing(private, monkeypatch):
    def boom():
        raise OSError("store unreadable")
    monkeypatch.setattr(draft_capture, "load_all", boom)
    assert len(ew.load()) == 0


def test_a_locked_private_dir_is_not_available(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_capture, "load_all", lambda: [])
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        if not os.access(locked, os.R_OK):  # root reads anything
            assert not ew.load(locked).available
    finally:
        locked.chmod(0o700)
