"""keep_gate: the one check every path asks before a hosted model works an
item with no owner asking (docs/llm-lanes.md 2.3). The level an item says
itself, the level of the mail's records and the keep list, and the check
failing closed. All mail here is made up; the store is a temporary
private dir."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import keep_gate  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A private dir with a keep list and one record of a thread kept on the
    machine: a review system's notice from a listed domain."""
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "shadow_until": "2099-01-01",
           "keep_from_frontier": {"domains": ["reviews.example.org"], "addresses": []}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    store = d / "remote-llm"
    store.mkdir(mode=0o700)
    rec = {"key": "k-kept", "id": "m-kept", "conversation_id": "c-kept", "status": "ok",
           "from": "notices@reviews.example.org", "subject": "Review confirmation",
           "keep_from_frontier": "listed domain reviews.example.org",
           "result": {"summary": "A review is due.", "ask": "Submit the review.", "state": "todo"}}
    plain = {"key": "k-plain", "id": "m-plain", "conversation_id": "c-plain", "status": "ok",
             "from": "pat@example.edu", "subject": "Slides", "result": {"summary": "Slides."}}
    (store / "extractions.jsonl").write_text("\n".join(json.dumps(r) for r in (rec, plain)) + "\n")
    import mail_privacy
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    import privacy_rules
    privacy_rules.reset()
    yield d
    privacy_rules.reset()


def test_what_an_item_says_itself():
    assert keep_gate.label_level({"id": "row-1"}) == "full"
    # The live briefing's copy of a kept row: the label sits on the row.
    assert keep_gate.label_level({"id": "kept-x", "kept_from_claude": "listed domain x.example"}) == "facts"
    # The ledger's own row: the label sits in its metadata.
    assert keep_gate.label_level({"id": "kept-x", "metadata": json.dumps(
        {"kept_from_frontier": "keep list"})}) == "facts"
    # A thread brief that names its level, in code or in plain words.
    assert keep_gate.label_level({"thread_brief": {"keep_level": "local"}}) == "local"
    assert keep_gate.label_level({"brief": {"kept_level": "Summary",
                                            "kept_from_frontier": "admissions"}}) == "summary"
    # A reply-queue entry's flag.
    assert keep_gate.label_level({"id": "q", "kept": True}) == "facts"
    assert keep_gate.stricter("summary", "facts", "full") == "facts"
    assert keep_gate.may_hand_off("summary") and keep_gate.may_hand_off("full")
    assert not keep_gate.may_hand_off("facts") and not keep_gate.may_hand_off("local")
    assert not keep_gate.may_hand_off(None) and not keep_gate.may_hand_off("")


def test_the_records_and_the_keep_list_decide_what_a_label_misses(store):
    rows = [
        # The row as the remote briefing once saw it: no label at all.
        {"id": "kept-a", "conversation_id": "c-kept", "source_message_id": "m-kept"},
        # A row whose sender is on the keep list, no record yet.
        {"id": "row-b", "from_email": "editor@reviews.example.org"},
        # Ordinary mail.
        {"id": "row-c", "conversation_id": "c-plain", "source_message_id": "m-plain"},
        # A row from no mail.
        {"id": "row-d"},
    ]
    assert keep_gate.levels(rows) == {"kept-a": "local", "row-b": "local", "row-c": "full", "row-d": "full"}
    # A hand-off item, as the queue holds it.
    ok, kept = keep_gate.split_handoffs([
        {"conversation_id": "c-kept", "ledger_id": "kept-a", "keep_level": ""},
        {"conversation_id": "c-plain", "ledger_id": "row-c", "keep_level": "full"}])
    assert [i["ledger_id"] for i in ok] == ["row-c"]
    assert [(i["ledger_id"], i["keep_level"]) for i in kept] == [("kept-a", "local")]
    assert keep_gate.refusal([], conversation_id="c-kept") == keep_gate.KEPT_REFUSAL
    assert keep_gate.refusal([{"id": "row-c", "conversation_id": "c-plain"}], conversation_id="c-plain") == ""


def test_the_check_fails_closed(monkeypatch):
    def broken():
        raise OSError("store locked")
    monkeypatch.setattr(keep_gate, "_records", broken)
    rows = [{"id": "a", "conversation_id": "c"}, {"id": "b", "source_message_id": "m"}, {"id": "c"}]
    assert keep_gate.levels(rows) == {"a": "local", "b": "local", "c": "full"}
    with pytest.raises(OSError):
        keep_gate.levels(rows, strict=True)
    # One item's records that cannot be read keep that item local.
    seen = keep_gate.levels(rows, records=lambda i: 1 / 0)
    assert seen == {"a": "local", "b": "local", "c": "full"}
    assert keep_gate.refusal([], conversation_id="c-any") == keep_gate.KEPT_REFUSAL
    marked = keep_gate.mark_kept([{"id": "q", "conversation_id": "c", "kept": False}])
    assert marked[0]["kept"] is True and marked[0]["keep_level"] == "local"


def test_the_briefing_rows_carry_the_level_every_lane_reads(monkeypatch):
    """preprocess stamps each kept row in the briefing JSON (the lanes on
    Claude or Codex read it), and the ledger's own kept test reads the
    stamp, so the reply queue's `kept` agrees."""
    import ledger
    import preprocess
    monkeypatch.setattr(keep_gate, "_records", lambda: (
        lambda i: {"c-kept": "local", "c-summary": "summary"}.get(i.get("conversation_id"), "full")))
    rows = [{"id": "kept-a", "conversation_id": "c-kept"}, {"id": "row-b", "conversation_id": "c-summary"},
            {"id": "row-c", "conversation_id": "c-plain"}]
    preprocess._mark_keep(rows)
    assert rows[0]["keep_level"] == "local" and rows[0]["kept_level"] == "Kept on your machine"
    assert rows[1]["kept_level"] == "Summary" and "keep_level" not in rows[2]
    assert ledger.kept_thread(rows[0]) and not ledger.kept_thread(rows[1]) and not ledger.kept_thread(rows[2])
