"""mail_tagger: the vocabulary gate, the merge with the user's own
categories, the model-output parser, and the two passes on injected
Graph data."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import mail_tagger as mt  # noqa: E402

NOW = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
VOCAB = [{"id": "robo-5000", "name": "ROBO 5000", "domain": "teaching", "status": "active"},
         {"id": "bs-ai", "name": "BS in AI", "domain": "service", "status": "active"}]
IDS = {v["id"] for v in VOCAB}


def _msg(i, frm="yael.quillon56@example.edu", subject="s", cats=None, cid="c1"):
    return {"id": f"m{i}", "subject": subject, "conversationId": cid,
            "from": {"emailAddress": {"address": frm}},
            "toRecipients": [{"emailAddress": {"address": "wren.fairbanks84@example.edu"}}],
            "ccRecipients": [], "bodyPreview": "hello", "receivedDateTime": "2026-10-05T15:00:00Z",
            "hasAttachments": False, "categories": cats or []}


def test_validate_keeps_only_vocabulary():
    assert mt.validate({"project": "robo-5000", "state": ["needs-reply", "deadline"],
                        "topic": "lor"}, IDS) == ["auto-tagged", "robo-5000", "needs-reply", "deadline", "lor"]
    # an invented project and topic vanish; a domain word is accepted
    assert mt.validate({"project": "made-up", "state": "fyi", "topic": "nonsense"}, IDS) == ["auto-tagged", "fyi"]
    assert mt.validate({"project": "service", "state": ["todo"]}, IDS) == ["auto-tagged", "service", "todo"]
    # no legal state tag: unusable
    assert mt.validate({"project": "bs-ai", "state": ["later"]}, IDS) is None
    assert mt.validate("garbage", IDS) is None


def test_merge_keeps_user_categories_and_replaces_state():
    assert mt.merge(["Red category", "needs-reply", "auto-tagged"],
                    ["auto-tagged", "bs-ai", "fyi"]) == ["Red category", "auto-tagged", "bs-ai", "fyi"]


def test_parse_verdicts_tolerates_fence_and_prose():
    text = 'Here you go:\n```json\n{"tags": {"0": {"project": "bs-ai", "state": ["fyi"], "topic": null}}}\n```'
    assert mt.parse_verdicts(text) == {"0": {"project": "bs-ai", "state": ["fyi"], "topic": None}}
    assert mt.parse_verdicts("no json here") == {}
    assert mt.parse_verdicts({"tags": {"1": {"state": "todo"}}}) == {"1": {"state": "todo"}}


class FakeGraph:
    def __init__(self, inbox_by_conv=None):
        self.patches = []
        self.inbox_by_conv = inbox_by_conv or {}

    def graph_patch(self, endpoint, body):
        self.patches.append((endpoint, body["categories"]))
        return {"id": endpoint.rsplit("/", 1)[-1], **body}

    def graph_get(self, url, params=None):
        for cid, rows in self.inbox_by_conv.items():
            if cid in url + str(params or ""):
                return {"value": rows}
        return {"value": []}


def test_tag_messages_tags_untagged_rows_and_retries_failures(tmp_path):
    g = FakeGraph()
    state = mt._empty_state()
    msgs = [_msg(0, subject="Quote request"), _msg(1, cats=["auto-tagged", "fyi"]), _msg(2)]
    seen = {}

    def asker(prompt):
        seen["prompt"] = prompt
        return ({"0": {"project": "bs-ai", "state": ["needs-reply"], "topic": "purchasing"},
                 "1": {"project": "x", "state": ["nope"]}}, {"cost_usd": 0.001})

    s = mt.tag_messages(msgs, graph=g, state=state, dry_run=False, now=NOW, vocab=VOCAB,
                        asker=asker, log_path=tmp_path / "log.jsonl")
    # m1 already carried the marker and was skipped; m0 tagged; m2 had no verdict -> failure counted
    assert s["tagged"] == 1 and s["skipped"] == 1 and s["failed"] == 1
    assert g.patches == [("/me/messages/m0", ["auto-tagged", "bs-ai", "needs-reply", "purchasing"])]
    assert state["failures"] == {"m2": 1}
    assert "m0" in state["recent"]
    assert "Quote request" in seen["prompt"] and "robo-5000 | ROBO 5000" in seen["prompt"]
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["event"] == "tagged"
    assert json.loads(lines[0])["conversation_id"] == "c1"


def test_project_votes_read_the_log_by_message_and_conversation(tmp_path):
    log = tmp_path / "log.jsonl"
    recs = [
        {"event": "tagged", "id": "m0", "conversation_id": "c1",
         "cats": ["auto-tagged", "robo-5000", "fyi"]},
        {"event": "tagged", "id": "m1", "conversation_id": "c2", "cats": ["auto-tagged", "needs-reply"]},
        {"event": "tagged", "id": "m2", "cats": ["auto-tagged"]},              # never answered
        {"event": "tagged", "id": "m3", "cats": ["auto-tagged", "old-closed", "todo"]},
        {"event": "flipped", "id": "m4", "cats": ["bs-ai"]},
    ]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\nnot json\n")
    v = mt.project_votes(IDS, path=log)
    assert v["messages"] == {"m0": "robo-5000", "m1": "", "m3": ""}
    assert v["conversations"] == {"c1": "robo-5000", "c2": ""}
    # the message's own call first, then its conversation; else no call
    assert mt.call_for(v, "m1", "c1") == ""
    assert mt.call_for(v, "m-unknown", "c1") == "robo-5000"
    assert mt.call_for(v, "m2", "c-unknown") is None
    assert mt.project_votes(IDS, path=tmp_path / "absent.jsonl") == {"messages": {},
                                                                      "conversations": {}}


def test_tag_messages_gives_up_with_marker_after_retry_limit(tmp_path):
    g = FakeGraph()
    state = mt._empty_state()
    state["failures"]["m0"] = mt.RETRY_LIMIT - 1
    s = mt.tag_messages([_msg(0)], graph=g, state=state, dry_run=False, now=NOW, vocab=VOCAB,
                        asker=lambda p: ({}, {}), log_path=tmp_path / "log.jsonl")
    assert s["tagged"] == 1
    assert g.patches == [("/me/messages/m0", ["auto-tagged"])]


def test_flip_answered_swaps_needs_reply_for_awaiting_only_before_the_send(tmp_path):
    g = FakeGraph({"c1": [
        {"id": "m0", "subject": "s", "receivedDateTime": "2026-10-05T10:00:00Z",
         "categories": ["auto-tagged", "bs-ai", "needs-reply"]},
        {"id": "m1", "subject": "t", "receivedDateTime": "2026-10-05T10:00:00Z",
         "categories": ["auto-tagged", "fyi"]},
        {"id": "m2", "subject": "u", "receivedDateTime": "2026-10-05T13:00:00Z",
         "categories": ["auto-tagged", "bs-ai", "needs-reply"]},   # arrived after the send
    ]})
    sent = [{"conversationId": "c1", "sentDateTime": "2026-10-05T11:00:00Z"},
            {"conversationId": "c1", "sentDateTime": "2026-10-05T12:00:00Z"},
            {"conversationId": "c9", "sentDateTime": "2026-10-05T12:00:00Z"}]
    s = mt.flip_answered(sent, graph=g, dry_run=False, now=NOW, log_path=tmp_path / "log.jsonl")
    assert s["flipped"] == 1 and s["conversations"] == 2
    assert g.patches == [("/me/messages/m0", ["auto-tagged", "bs-ai", "awaiting-reply"])]


def test_tick_dry_run_writes_nothing(tmp_path):
    g = FakeGraph()
    s = mt.tick(dry_run=True, graph=g, now=NOW, state_path=tmp_path / "st.json",
                log_path=tmp_path / "log.jsonl", inbox=[_msg(0)], sent=[],
                asker=lambda p: ({"0": {"project": "teaching", "state": ["fyi"]}}, {}), vocab=VOCAB)
    assert s["tag"]["tagged"] == 1 and g.patches == []
    assert not (tmp_path / "st.json").exists()


def test_sender_topic_hints_feed_the_prompt_and_labels_are_remembered(tmp_path):
    g = FakeGraph()
    state = mt._empty_state()
    state["senders"]["bri.foxworth13@example.org"] = {"cats": ["auto-tagged", "front-range-consortium", "fyi", "funding"], "at": "2026-10-01T00:00:00Z"}
    seen = {}

    def asker(prompt):
        seen["prompt"] = prompt
        return ({"0": {"project": "bs-ai", "state": ["fyi"]}}, {})

    mt.tag_messages([_msg(0, frm="bri.foxworth13@example.org")], graph=g, state=state, dry_run=False, now=NOW,
                    vocab=VOCAB, asker=asker, log_path=tmp_path / "log.jsonl")
    # the topic carries over; the project call does not follow the sender
    assert "bri.foxworth13@example.org: funding" in seen["prompt"]
    assert "front-range-consortium" not in seen["prompt"]
    assert state["senders"]["bri.foxworth13@example.org"]["cats"] == ["auto-tagged", "bs-ai", "fyi"]


def test_project_lines_carry_parent_aliases_people_and_summary():
    """The model tells projects apart by more than a name: the parent, the
    aliases, the members' addresses and the first sentence of the summary.
    Fields left empty stay out of the line; dormant projects stay out of
    the prompt."""
    vocab = [{"id": "ai-dir", "name": "AI Faculty Director", "domain": "service", "status": "active"},
             {"id": "bs-ai", "name": "BS in AI", "domain": "service", "status": "active",
              "parent": "ai-dir", "aliases": "BS-AIE", "people": ["tamsin.rook@example.edu"],
              "about": "The new undergraduate degree."},
             {"id": "old-grant", "name": "Old grant", "domain": "research", "status": "dormant"}]
    prompt = mt.build_prompt([mt.compact(_msg(0), 0, set())], vocab)
    assert "\nai-dir | AI Faculty Director | service\n" in prompt
    assert ("bs-ai | BS in AI | service | under: ai-dir | aka: BS-AIE | "
            "people: tamsin.rook@example.edu | The new undergraduate degree.") in prompt
    assert "old-grant" not in prompt
    assert mt._first_sentence("Two tracks. The second starts in fall.") == "Two tracks."
    assert mt._first_sentence("x" * 200, cap=20) == "x" * 17 + "..."
