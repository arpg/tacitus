"""The state of a conversation in the web chat (docs/webchat-state.md):
the rules in their order, the owner's override, the model step with a
fake model, the engine's change pass and tick over a scratch store, the
EA's ledger read and model route, and the routing table's new task.
Every name, address and id here is made up; nothing reaches a network."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat import thread_state as ts  # noqa: E402
from chat.types import Status  # noqa: E402
from chat.web_frontend import WebFrontend, WebStore  # noqa: E402
import ea_config  # noqa: E402
import ledger  # noqa: E402
import model_routing as mr  # noqa: E402
import remote_llm as rl  # noqa: E402
import web_thread_state as wts  # noqa: E402

NOW = 1_800_000_000.0
MIN = 60.0
DAY = 86400.0


def m(seq, author="bot", text="", *, kind="message", card=None, reactions=(), at=None,
      updated=None):
    """A stored message as the store's JSON gives it."""
    at = NOW - 3600 + seq if at is None else at
    return {"seq": seq, "channel": "c1", "id": f"w{seq}", "thread_key": "t1", "author": author,
            "kind": kind, "text": text, "card": card, "file": None, "reactions": list(reactions),
            "posted_at": at, "updated_at": at if updated is None else updated, "reply_count": 0}


def live_card(title=":lock: *Confirm send*", **value):
    return {"kind": "send", "title": title, "fields": [{"label": "To", "value": "x@example.test"}],
            "body": "A draft body the model never sees.",
            "actions": [{"id": "approve_send", "label": "Send", "value": value or {"token": "t"},
                         "style": "primary", "word": "send"},
                        {"id": "cancel_send", "label": "Cancel", "value": {"token": "t"},
                         "style": "", "word": "cancel"}],
            "select": None}


def decide(msgs, **kw):
    kw.setdefault("now", NOW)
    return ts.decide(msgs, **kw)


# ── the rules, one by one and in order ────────────────────────────────

NOTICE = ('A new device asks to reach Tacitus through the relay: "Pixel", pairing code 123 456. '
          "Approve it under Pair a phone in the web chat only when the phone shows the same code.")
PENDING = [{"id": "0123456789abcdef", "name": "Pixel", "pending": True, "code": "123456"}]
APPROVED = [{"id": "0123456789abcdef", "name": "Pixel", "pending": False}]

RULE_CASES = [
    # (name, messages, keywords, expected state or None)
    ("a live card needs you",
     [m(1, text="Draft ready."), m(2, kind="card", card=live_card())], {}, "needs_you"),
    ("a card past the registry's age has no live buttons",
     [m(1, kind="card", card=live_card(), at=NOW - 8 * DAY)], {}, "none"),
    ("a card with no buttons is not live",
     [m(1, kind="card", card={"title": "A note", "actions": [], "select": None})], {}, "none"),
    ("a new device waits for approval",
     [m(1, text=NOTICE)], {"devices": PENDING}, "needs_you"),
    ("a new device approved",
     [m(1, text=NOTICE)], {"devices": APPROVED}, "done"),
    ("a new device removed",
     [m(1, text=NOTICE)], {"devices": []}, "done"),
    ("a new-device notice with no relay running is an automated post",
     [m(1, text=NOTICE)], {"devices": None}, "none"),
    ("the owner wrote a moment ago",
     [m(1, "owner", "And the budget?", at=NOW - 5 * MIN)], {}, "working"),
    ("the bot's hourglass on the owner's message",
     [m(1, "owner", "Find a time with Avery.", reactions=["eyes"], at=NOW - 5 * MIN)], {},
     "working"),
    ("a progress line is no answer",
     [m(1, "owner", "Find a time.", reactions=["eyes"], at=NOW - 30 * MIN),
      m(2, text=":hourglass_flowing_sand: Still working... (3m)", at=NOW - 2 * MIN)], {},
     "working"),
    ("no answer past twenty minutes",
     [m(1, "owner", "And the budget?", at=NOW - 2 * 3600)], {}, "needs_you"),
    ("the hourglass past twenty minutes is a stuck turn",
     [m(1, "owner", "And the budget?", reactions=["eyes"], at=NOW - 25 * MIN)], {}, "needs_you"),
    ("a failed turn at once",
     [m(1, "owner", "Export this.", reactions=["x"], at=NOW - MIN)], {}, "needs_you"),
    ("a failed-turn notice from the bot",
     [m(1, "owner", "Plan the trip.", at=NOW - 2 * MIN),
      m(2, text=":warning: I hit my usage limit and could not process that.", at=NOW - MIN)], {},
     "needs_you"),
    ("a finished turn with no words is the model's",
     [m(1, "owner", "Archive it.", reactions=["white_check_mark"], at=NOW - 2 * 3600)], {}, None),
    ("a message copied from the past is the model's",
     [{**m(1, "owner", "Thanks.", at=NOW - 3 * DAY), "history": True}], {}, None),
    ("the bot acts on a press",
     [m(1, kind="status", text=":hourglass_flowing_sand: Sending...", card=live_card(),
        updated=NOW - MIN)], {}, "working"),
    ("a card acted on is done",
     [m(1, kind="status", text=":white_check_mark: Sent.", card=live_card(), updated=NOW - 600)],
     {}, "done"),
    ("a card acted on, then the bot asks something",
     [m(1, kind="status", text=":no_entry_sign: Send cancelled.", updated=NOW - 600),
      m(2, text="Should I write a shorter one?", at=NOW - 300)], {}, "none"),
    ("a card acted on, then the bot asks something, in a thread the owner wrote in",
     [m(1, "owner", "Send the draft to Avery.", at=NOW - 900),
      m(2, kind="status", text=":no_entry_sign: Send cancelled.", updated=NOW - 600),
      m(3, text="Should I write a shorter one?", at=NOW - 300)], {}, None),
    ("a card that failed is not done",
     [m(1, kind="status", text=":x: Send failed: the draft is gone", updated=NOW - 600)], {}, "none"),
    ("the bot's check on the first message is no rule",
     [m(1, "owner", "Move the call to Friday.", reactions=["white_check_mark"]),
      m(2, text="Moved to Friday at 10:00.")], {}, None),
    ("an open reply row needs you",
     [m(1, text="Mail from a colleague.")],
     {"ledger": [{"id": "response-a1", "type": "response_needed", "status": "open"}]}, "needs_you"),
    ("a follow_up draft row needs you",
     [m(1, text="x")],
     {"ledger": [{"id": "d-a1", "type": "draft_pending", "status": "follow_up"}]}, "needs_you"),
    ("a waiting status waits",
     [m(1, text="Asked for the slides.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "waiting"}]}, "waiting"),
    ("a snoozed row waits",
     [m(1, text="Later.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "snoozed"}]}, "waiting"),
    ("a closed row is done",
     [m(1, text="Filed.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "completed"}]}, "done"),
    ("an open task row is the model's",
     [m(1, "owner", "Fill the form?"), m(2, text="It is due Friday.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "open"}]}, None),
    ("an open row of the waiting type is the model's",
     [m(1, "owner", "Any news?"), m(2, text="Asked for the slides.")],
     {"ledger": [{"id": "waiting-a1", "type": "waiting", "status": "open"}]}, None),
    ("an open deadline row is the model's",
     [m(1, "owner", "When is it due?"), m(2, text="Report due.")],
     {"ledger": [{"id": "deadline-a1", "type": "deadline", "status": "open"}]}, None),
    ("an open task row in a thread the owner never wrote in is none",
     [m(1, text="Fill the form.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "open"}]}, "none"),
    ("a notifier post the owner never wrote in",
     [m(1, text="Seven items for today.")], {"origin": "ea-assistant"}, "none"),
    ("a briefing by its title",
     [m(1, text=":sunny: **Morning Briefing, Mon Oct 26**\n- one")], {}, "none"),
    ("a digest by its title",
     [m(1, text="**List digest** (12 messages)")], {}, "none"),
    ("an automated post the owner answered",
     [m(1, text="Seven items for today."), m(2, "owner", "Skip item 3.", at=NOW - 3 * 3600),
      m(3, text="Skipped item 3.", at=NOW - 3 * 3600 + 60)],
     {"origin": "ea-assistant"}, None),
    ("a conversation that ends on the bot's prose goes to the model",
     [m(1, "owner", "Who wrote about the visit?", reactions=["white_check_mark"]),
      m(2, text="Avery did, on Tuesday.")], {}, None),
]


@pytest.mark.parametrize("name,msgs,kw,want", RULE_CASES, ids=[c[0] for c in RULE_CASES])
def test_each_rule(name, msgs, kw, want):
    got = decide(msgs, **kw)
    if want is None:
        assert got is None, got
    else:
        assert got is not None and got[0] == want and got[1] == "rule", got
        assert got[2] and "\u2014" not in got[2]


def test_the_reasons_the_owner_reads():
    stuck = decide([m(1, "owner", "And the budget?", at=NOW - 2 * 3600)])
    assert stuck == ("needs_you", "rule", "Tacitus did not answer this. Ask again.")
    assert decide([m(1, text=NOTICE)], devices=PENDING)[2] == "A phone waits for your approval."
    assert decide([m(1, "owner", "Hi.", at=NOW - MIN)])[2] == \
        "You wrote last and Tacitus has not answered yet."


ORDER_CASES = [
    ("the owner's override beats a live card",
     [m(1, kind="card", card=live_card())],
     {"prev": {"state": "done", "by": "owner", "reason": "You marked this done.", "judged_seq": 1}},
     ("done", "owner")),
    ("a card posted after the owner's message beats the hourglass",
     [m(1, "owner", "Draft it.", reactions=["eyes"], at=NOW - 2 * MIN),
      m(2, kind="card", card=live_card(), at=NOW - MIN)], {},
     ("needs_you", "rule")),
    ("the owner's change beats a live card",
     [m(1, kind="card", card=live_card(), at=NOW - 10 * MIN),
      m(2, "owner", "Wait.", reactions=["eyes"], at=NOW - 2 * MIN)], {},
     ("working", "rule")),
    ("a waiting device beats the owner's unanswered message",
     [m(1, text=NOTICE), m(2, "owner", "Is this my phone?", at=NOW - 3 * 3600)],
     {"devices": PENDING}, ("needs_you", "rule")),
    ("the hourglass beats a card acted on",
     [m(1, kind="status", text=":white_check_mark: Sent.", updated=NOW - 600),
      m(2, "owner", "Now the next one.", reactions=["eyes"], at=NOW - 60)], {},
     ("working", "rule")),
    ("an unanswered message beats a closed ledger row",
     [m(1, text="Filed."), m(2, "owner", "And the other one?", at=NOW - 2 * 3600)],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "completed"}]},
     ("needs_you", "rule")),
    ("a card acted on beats an open ledger row",
     [m(1, kind="status", text=":white_check_mark: Sent.", updated=NOW - 600)],
     {"ledger": [{"id": "response-a1", "type": "response_needed", "status": "open"}]},
     ("done", "rule")),
    ("an open ledger row beats an automated post",
     [m(1, text="**List digest**")],
     {"origin": "personal",
      "ledger": [{"id": "response-a1", "type": "response_needed", "status": "open"}]},
     ("needs_you", "rule")),
    ("needs_you wins among linked rows",
     [m(1, text="x")],
     {"ledger": [{"id": "a-1", "type": "task", "status": "completed"},
                 {"id": "b-1", "type": "task", "status": "waiting"},
                 {"id": "c-1", "type": "draft_pending", "status": "follow_up"}]},
     ("needs_you", "rule")),
    ("an open task among closed rows leaves it to the model",
     [m(1, "owner", "x?"), m(2, text="y")],
     {"ledger": [{"id": "a-1", "type": "task", "status": "completed"},
                 {"id": "b-1", "type": "task", "status": "open"}]},
     None),
]


@pytest.mark.parametrize("name,msgs,kw,want", ORDER_CASES, ids=[c[0] for c in ORDER_CASES])
def test_the_first_rule_that_fires_wins(name, msgs, kw, want):
    got = decide(msgs, **kw)
    assert (got[:2] if got else None) == want, got


def test_the_owner_override_holds_until_a_new_message_lands():
    prev = {"state": "waiting", "by": "owner", "reason": "You marked this as waiting on someone "
            "else.", "judged_seq": 2}
    msgs = [m(1, kind="card", card=live_card()), m(2, text="Card above.")]
    assert decide(msgs, prev=prev) == ("waiting", "owner", prev["reason"])
    # a reaction or a card outcome adds no message: it still holds
    msgs[1]["reactions"] = ["white_check_mark"]
    assert decide(msgs, prev=prev)[1] == "owner"
    # a new message ends it, and the rules decide again
    msgs.append(m(3, "owner", "Send it.", reactions=["eyes"]))
    assert decide(msgs, prev=prev)[:2] == ("needs_you", "rule")
    # a rule's or the model's earlier state is not sticky
    assert decide(msgs[:2], prev={**prev, "by": "model"})[:2] == ("needs_you", "rule")


def test_a_card_reason_names_its_title_in_plain_words():
    state, _by, reason = decide([m(1, kind="card", card=live_card(":lock: *Confirm send*"))])
    assert reason == 'The card "Confirm send" still has its buttons.'
    assert decide([m(1, kind="status", text=":white_check_mark: *Sent.*", updated=NOW - 60)])[2] \
        == "The card was answered: Sent."


def test_asks_reads_a_question_outside_links_and_code():
    assert ts.asks("Which one works?")
    assert ts.asks("Tuesday or Wednesday?\nI can hold both.")
    assert not ts.asks("See https://example.test/a?b=1 for the form.")
    assert not ts.asks("Run `ledger.py get --id x?` later.")
    assert not ts.asks("Sent.")


def test_link_keys_take_ids_from_cards_and_text():
    card = live_card(conversation_id="AAQkconv1=", ledger_id="response-Ab12_x=")
    msgs = [m(1, kind="card", card=card),
            m(2, text="Row `task-Zz9_q=` is open; the follow-up is on Friday.")]
    ids, convs = ts.link_keys(msgs)
    assert convs == ["AAQkconv1="]
    assert ids[:2] == ["response-Ab12_x=", "task-Zz9_q="] and "follow-up" in ids
    many = [m(i, text=" ".join(f"word-{i}{j}" for j in range(10))) for i in range(1, 10)]
    assert len(ts.link_keys(many)[0]) == ts.LINK_CAP


# ── the model step ────────────────────────────────────────────────────

GOOD = '{"state": "waiting", "reason": "The colleague has not replied yet.", "confidence": 0.8}'


@pytest.mark.parametrize("text,want", [
    (GOOD, ("waiting", 0.8)),
    ("```json\n" + GOOD + "\n```", ("waiting", 0.8)),
    ("Here it is: " + GOOD, None),
    ('{"state": "waiting", "reason": "x"}', None),
    ('{"state": "waiting", "reason": "x", "confidence": 0.8, "extra": 1}', None),
    ('{"state": "later", "reason": "x", "confidence": 0.8}', None),
    ('{"state": "done", "reason": "x", "confidence": 1.5}', None),
    ('{"state": "done", "reason": "x", "confidence": true}', None),
    ('{"state": "done", "reason": "  ", "confidence": 0.9}', None),
    ("[1, 2]", None),
    (None, None),
])
def test_the_answer_parses_strictly(text, want):
    got = ts.parse_answer(text)
    if want is None:
        assert got is None
    else:
        assert (got["state"], got["confidence"]) == want


def test_the_model_s_reason_is_one_plain_sentence():
    got = ts.parse_answer(json.dumps({"state": "needs_you", "reason": "**Avery** asked a question. "
                                      "You have not replied :email:.", "confidence": 0.9}))
    assert got["reason"] == "Avery asked a question."


def test_low_confidence_or_no_answer_keeps_the_previous_state():
    prev = {"state": "working", "by": "rule", "reason": "x", "judged_seq": 3}
    assert ts.model_verdict(ts.parse_answer(GOOD), prev) == \
        ("waiting", "model", "The colleague has not replied yet.")
    low = ts.parse_answer('{"state": "done", "reason": "Maybe done.", "confidence": 0.59}')
    state, by, reason = ts.model_verdict(low, prev)
    assert (state, by) == ("working", "model") and "unsure" in reason
    state, by, reason = ts.model_verdict(None, prev)
    assert (state, by) == ("working", "model") and "no answer" in reason
    assert ts.model_verdict(low, None)[0] == "none"


def test_the_prompt_carries_titles_and_outcomes_never_card_bodies():
    msgs = [m(1, "owner", "Reply to Avery about the visit.", at=NOW - 7200),
            m(2, kind="card", card=live_card(), at=NOW - 7100),
            m(3, kind="status", text=":white_check_mark: Sent.", card=live_card(), at=NOW - 7000)]
    p = ts.judge_prompt(msgs, channel={"id": "c1", "name": "ea-dm", "kind": "dm"},
                        ledger=[{"id": "task-a1", "type": "task", "status": "open",
                                 "deadline": "2026-11-05", "last_action": "Sent the form."}],
                        now=NOW)
    assert '"ea-dm" (kind: dm)' in p
    assert "- type task, status open, deadline 2026-11-05, last action: Sent the form." in p
    assert 'card "Confirm send": waiting for a button' in p and 'card "Confirm send": Sent.' in p
    assert "A draft body the model never sees" not in p and "x@example.test" not in p
    assert "[owner, " in p and "Reply to Avery" in p and "JSON" in p
    long = [m(1, "owner", "first")] + [m(i, text="y" * 900) for i in range(2, 60)]
    p = ts.judge_prompt(long, now=NOW)
    assert "] first" in p and "earlier messages left out" in p and len(p) < 12000


# ── the engine over a scratch store ───────────────────────────────────

class FakeModel:
    def __init__(self, answer=GOOD):
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer(prompt) if callable(self.answer) else self.answer


@pytest.fixture
def store(tmp_path):
    s = WebStore(tmp_path / "webchat.sqlite")
    s.upsert_channel("main", "ea", "main")
    s.upsert_channel("dm", "dm", "dm")
    yield s
    s.close()


def chat_thread(store, text="Avery wrote back.", reply="Who is Avery?", at=None):
    """A thread no rule settles: a bot post, the owner's question and the
    bot's answer, hours ago."""
    at = time.time() - 3 * 3600 if at is None else at
    first = store.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                              text=text, posted_at=at)
    store.add_message("main", mid=None, thread_key=first["thread_key"], author="owner",
                      kind="message", text=reply, posted_at=at + 60)
    store.add_message("main", mid=None, thread_key=first["thread_key"], author="bot",
                      kind="message", text="A colleague from the visit.", posted_at=at + 120)
    return first["thread_key"]


def engine(store, model=None, **kw):
    return ts.ThreadStates(lambda: store, model=model, **kw)


def test_the_tick_asks_the_model_at_most_its_cap_and_records_judged_seq(store):
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * 3600 + i) for i in range(25)]
    model = FakeModel()
    e = engine(store, model)
    out = e.tick()
    assert out["model_calls"] == 20 and len(model.prompts) == 20 and out["open"] == 25
    judged = [k for k in keys if store.thread_state("main", k)]
    # newest first: the five oldest wait for the next tick
    assert judged == keys[5:]
    row = store.thread_state("main", keys[-1])
    assert (row["state"], row["by"]) == ("waiting", "model")
    assert row["judged_seq"] == store.newest_seq("main", keys[-1])
    assert e.tick()["model_calls"] == 5
    assert e.tick()["model_calls"] == 0
    # a new message past judged_seq brings that thread back, and only it
    store.add_message("main", mid=None, thread_key=keys[3], author="bot", kind="message",
                      text="Avery asked again.", posted_at=time.time() - 3 * 3600)
    model.prompts.clear()
    assert e.tick()["model_calls"] == 1 and "post 3" in model.prompts[0]


def test_the_tick_stops_asking_when_no_model_answers(store):
    for i in range(5):
        chat_thread(store, f"post {i}")
    model = FakeModel(answer=None)
    out = engine(store, model).tick()
    assert out["model_calls"] == 1 and out["stopped"]
    assert store._one("SELECT COUNT(*) AS n FROM thread_state")["n"] == 0
    # with no model at all the rules alone run and nothing is asked
    assert engine(store, None).tick()["model_calls"] == 0


def test_bad_json_and_low_confidence_keep_the_previous_state(store):
    key = chat_thread(store)
    seq = store.newest_seq("main", key)
    store.set_thread_state("main", key, "needs_you", "model", "Earlier.", judged_seq=seq - 1)
    engine(store, FakeModel("not json")).tick()
    row = store.thread_state("main", key)
    assert (row["state"], row["by"], row["judged_seq"]) == ("needs_you", "model", seq)
    assert "no answer" in row["reason"]
    store.add_message("main", mid=None, thread_key=key, author="bot", kind="message",
                      text="More.", posted_at=time.time() - 3 * 3600)
    engine(store, FakeModel('{"state": "done", "reason": "Done.", "confidence": 0.3}')).tick()
    row = store.thread_state("main", key)
    assert row["state"] == "needs_you" and "unsure" in row["reason"]


def test_a_change_runs_the_rules_at_once_and_appends_the_event(store):
    e = engine(store)
    e.changed()  # the watermark
    row = store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                            text="Draft a reply to Avery.")
    start = store.max_event_seq()
    assert e.changed() == 1
    st = store.thread_state("main", row["thread_key"])
    assert (st["state"], st["by"]) == ("working", "rule")
    events = [(t, json.loads(p)) for _s, t, p in store.events_since(start)]
    assert events == [("thread_state", {"channel": "main", "thread_key": row["thread_key"],
                                        "state": "working", "by": "rule",
                                        "reason": "You wrote last and Tacitus has not answered yet.",
                                        "at": st["at"], "stale": False})]
    # a card outcome is a change too
    card = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card",
                             text="Confirm send", card=live_card())
    e.changed()
    assert store.thread_state("dm", card["thread_key"])["state"] == "needs_you"
    store.change_message("dm", card["id"], "status", kind="status", text=":white_check_mark: Sent.")
    e.changed()
    assert store.thread_state("dm", card["thread_key"])["state"] == "done"
    # so is a reaction; an unchanged state appends no event
    start = store.max_event_seq()
    store.toggle_reaction("dm", card["id"], "eyes", True)
    e.changed()
    assert [t for _s, t, _p in store.events_since(start)] == ["reaction"]


def test_a_change_the_rules_leave_open_brings_the_tick_forward(store):
    e = engine(store, FakeModel())
    e.changed()
    chat_thread(store)
    e.changed()
    assert e._due


def test_a_rule_that_no_longer_holds_goes_to_the_model(store):
    clock = [time.time()]
    ask = store.add_message("dm", mid=None, thread_key=None, author="owner", kind="message",
                            text="Send the draft.", posted_at=clock[0] - 120)
    card = store.add_message("dm", mid=None, thread_key=ask["thread_key"], author="bot",
                             kind="status", text=":hourglass_flowing_sand: Sending...",
                             card=live_card(), posted_at=clock[0] - 60)
    model = FakeModel()
    e = engine(store, model, clock=lambda: clock[0])
    e.tick()
    assert store.thread_state("dm", card["thread_key"])["state"] == "working"
    assert model.prompts == []
    clock[0] += 30 * MIN  # still "Sending..." half an hour on: the rule no longer holds
    e.tick()
    assert len(model.prompts) == 1
    assert store.thread_state("dm", card["thread_key"])["by"] == "model"


def test_a_thread_the_owner_never_wrote_in_is_none_and_never_reaches_the_model(store):
    """An ops notice with an ask in its words, copied with no origin and
    matching no title pattern, gets no sticker; the model reads it only once
    the owner writes in it."""
    store.upsert_channel("stream:12", "ea-health", "room")
    at = time.time() - 3 * 3600
    post = store.add_message("stream:12", mid=None, thread_key=None, author="bot",
                             kind="message", text="3 messages were held back; review them",
                             posted_at=at)
    key = post["thread_key"]
    model, hosted = FakeModel('{"state": "needs_you", "reason": "Review them.", '
                              '"confidence": 0.9}'), FakeHosted()
    out = engine(store, model).tick()
    row = store.thread_state("stream:12", key)
    assert (row["state"], row["by"]) == ("none", "rule")
    assert row["reason"] == "A post from Tacitus: " + ts.QUIET_TAIL
    assert out["model_calls"] == 0 and model.prompts == []
    assert hosted_engine(store, hosted).tick()["hosted_calls"] == 0 and hosted.prompts == []
    # the owner writes in it and the bot answers: now the model reads it
    store.add_message("stream:12", mid=None, thread_key=key, author="owner", kind="message",
                      text="Which ones?", posted_at=at + 60)
    store.add_message("stream:12", mid=None, thread_key=key, author="bot", kind="message",
                      text="Two from a list and one from a vendor.", posted_at=at + 120)
    assert engine(store, model).tick()["model_calls"] == 1
    assert "held back" in model.prompts[0]
    row = store.thread_state("stream:12", key)
    assert (row["state"], row["by"]) == ("needs_you", "model")


def test_the_next_tick_turns_a_model_state_on_an_owner_silent_thread_to_none(store):
    """A store from before: the model's needs_you on threads the owner never
    wrote in, one of them older than the tick's 14 days. The next tick sets
    both by rule, with no new message and no model call."""
    keys = []
    for i, at in enumerate((time.time() - 3 * 3600, time.time() - 20 * DAY)):
        post = store.add_message("main", mid=None, thread_key=None, author="bot",
                                 kind="message", text=f"Mail held back ({i}); review it.",
                                 posted_at=at)
        keys.append(post["thread_key"])
        store.set_thread_state("main", post["thread_key"], "needs_you", "model",
                               "Review the held mail.", judged_seq=post["seq"])
    model = FakeModel()
    start = store.max_event_seq()
    engine(store, model).tick()
    assert model.prompts == []
    for key in keys:
        row = store.thread_state("main", key)
        assert (row["state"], row["by"]) == ("none", "rule"), key
    events = [json.loads(p) for _s, t, p in store.events_since(start) if t == "thread_state"]
    assert sorted(ev["thread_key"] for ev in events) == sorted(keys)
    assert store.needs() == []


def test_an_unanswered_message_turns_from_working_to_needs_you_on_the_tick(store):
    clock = [time.time()]
    row = store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                            text="And the budget?", posted_at=clock[0])
    e = engine(store, FakeModel(), clock=lambda: clock[0])
    e.tick()
    assert store.thread_state("main", row["thread_key"])["state"] == "working"
    clock[0] += 21 * MIN
    assert e.tick()["model_calls"] == 0
    st = store.thread_state("main", row["thread_key"])
    assert (st["state"], st["by"], st["reason"]) == \
        ("needs_you", "rule", "Tacitus did not answer this. Ask again.")


def test_a_new_device_notice_follows_the_relay(store):
    devices = [list(PENDING)]
    notice = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="message",
                               text=NOTICE)
    reads = []

    def rows():
        reads.append(1)
        return devices[0]
    e = ts.ThreadStates(lambda: store, devices=rows)
    chat_thread(store)
    e.tick()
    assert store.thread_state("dm", notice["thread_key"])["state"] == "needs_you"
    assert len(reads) == 1  # read once, for the batch that holds a notice
    devices[0] = APPROVED
    e.tick()
    st = store.thread_state("dm", notice["thread_key"])
    assert (st["state"], st["reason"]) == ("done", "The phone was approved or removed.")


def test_the_store_never_lets_a_stale_write_or_a_rule_undo_the_owner(store):
    key = chat_thread(store)
    seq = store.newest_seq("main", key)
    store.set_thread_state("main", key, "done", "owner", "You marked this done.", judged_seq=seq)
    # the model's answer on the same messages arrives after the override
    store.set_thread_state("main", key, "needs_you", "model", "Late.", judged_seq=seq)
    assert store.thread_state("main", key)["by"] == "owner"
    store.set_thread_state("main", key, "needs_you", "rule", "Newer.", judged_seq=seq + 1)
    assert store.thread_state("main", key)["state"] == "needs_you"
    # an answer judged on older messages never replaces a newer one
    store.set_thread_state("main", key, "done", "model", "Old.", judged_seq=seq)
    assert store.thread_state("main", key)["reason"] == "Newer."
    with pytest.raises(ValueError):
        store.set_thread_state("main", key, "later", "rule", "x", judged_seq=seq + 2)


def test_the_bot_runs_the_states_beside_the_server(tmp_path):
    f = WebFrontend.from_config({"port": 0, "tailscale_login": "owner@example.test"},
                                store_path=tmp_path / "priv" / "webchat.sqlite",
                                webui_dir=tmp_path / "webui", trusted_config=True)
    f.poll_s = f.pair_delay_s = 0.02
    states = f.enable_thread_state(poll_s=0.02, tick_s=3600)
    f.start()
    try:
        assert states._thread is not None and states._thread.is_alive()
        ref = f.post("main", "Here is the morning briefing.")
        f.react("main", ref.id, Status.DONE)
        msg = f.store.add_message("main", mid=None, thread_key=None, author="owner",
                                  kind="message", text="Draft a reply.")
        deadline = time.time() + 5
        while time.time() < deadline and not f.store.thread_state("main", msg["thread_key"]):
            time.sleep(0.02)
        assert f.store.thread_state("main", msg["thread_key"])["state"] == "working"
    finally:
        f.stop()
    assert states._thread is None


def test_the_tick_reads_the_ledger_once_a_batch_and_links_each_thread(store):
    calls = []

    def led(ids, convs):
        calls.append((list(ids), list(convs)))
        return [{"id": "response-Ab1=", "type": "response_needed", "status": "open",
                 "conversation_id": ""},
                {"id": "task-Q1=", "type": "task", "status": "completed", "conversation_id": ""},
                {"id": "task-W1=", "type": "task", "status": "waiting",
                 "conversation_id": "AAQkc1="}]
    old = time.time() - 3 * 3600
    by_id = chat_thread(store, "Mail from a colleague: `response-Ab1=`.", at=old)
    done = chat_thread(store, "Filed as `task-Q1=`.", at=old + 10)
    card = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="status",
                             text=":x: Send failed", card=live_card(conversation_id="AAQkc1="),
                             posted_at=old + 20)
    plain = chat_thread(store, "Nothing linked here.", at=old + 30)
    e = ts.ThreadStates(lambda: store, ledger=led)
    e.tick()
    assert len(calls) == 1 and "AAQkc1=" in calls[0][1] and "response-Ab1=" in calls[0][0]
    assert store.thread_state("main", by_id)["state"] == "needs_you"
    assert store.thread_state("main", done)["state"] == "done"
    assert store.thread_state("dm", card["thread_key"])["state"] == "waiting"
    assert store.thread_state("main", plain) is None
    # a ledger that cannot be read leaves the rules to run without it
    e = ts.ThreadStates(lambda: store, ledger=lambda ids, convs: 1 / 0)
    assert e.tick()["threads"] == 4


def test_the_engine_reads_which_messages_were_copied_from_the_past(store):
    store.add_message("main", mid="h1", thread_key="th", author="owner", kind="message",
                      text="Thanks.", posted_at=time.time() - 3 * 3600, history=True)
    assert store.thread("main", "th", history=True)[0]["history"] is True
    assert "history" not in store.thread("main", "th")[0]
    # with no reactions copied, an old owner message is not taken as unanswered
    verdict, _ctx = engine(store).rules("main", "th")
    assert verdict == "open"


def test_a_moved_thread_keeps_its_state(store):
    key = chat_thread(store)
    seq = store.newest_seq("main", key)
    store.set_thread_state("main", key, "waiting", "owner", "You marked this as waiting on "
                           "someone else.", judged_seq=seq)
    store.note_origin("main", key, "ea-assistant")
    store.upsert_channel("r1", "ea-room", "room")
    store.move_thread("main", key, "r1", "r1:moved")
    assert store.thread_state("r1", "r1:moved")["state"] == "waiting"
    assert store.thread_origin("r1", "r1:moved") == "ea-assistant"
    assert store.thread_state("main", key) is None


# ── shadow mode: cards copied as text from Zulip's past ───────────────

ZHEAD = ":locked: **Confirm send**\n**To:** `x@example.test`"
ZRULE = "\n\n----\n"
CHIPS = ("Tap a reaction below: :+1: Send · :-1: Cancel\n"
         "Or reply `<word> ABCD` in this topic: send, cancel.")
ZBODY = "**Body:**\n> A draft body the model never sees."


def zcard(middle=CHIPS, *, seq=1, at=None, head=ZHEAD, channel="stream:6", primary=None):
    """A card as the history copy stores it: Zulip's raw text, no card JSON."""
    row = {**m(seq, text=head + ZRULE + middle + ZRULE + ZBODY, at=at), "channel": channel,
           "history": True}
    if primary is not None:
        row["primary"] = primary
    return row


MIRROR_CASES = [
    # (name, messages, expected (state, reason) or None for the model)
    ("the chips legend still has its buttons", [zcard()],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("the typed legend still has its buttons",
     [zcard("Reply `<word> ABCD` in this topic: keep.")],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("the bare code still has its buttons", [zcard("Code **ABCD**, in this topic.")],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("the owner's mention before the lock",
     [zcard(head="@**Avery Owner|9** :locked: **Confirm send**")],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("the Slack spelling of the lock", [zcard(head=":lock: *Confirm send*")],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("a legend past the registry's age acts no more", [zcard(at=NOW - 8 * DAY)], ("none", "A card from Tacitus: " + ts.QUIET_TAIL)),
    ("sent with the button", [zcard(":check: **Sent**")],
     ("done", "The card was answered: Sent")),
    ("sent from Outlook", [zcard(":mail_received: Sent from Outlook.")],
     ("done", "The card was answered: Sent from Outlook.")),
    ("handled", [zcard(":check: Handled. Draft deleted, thread archived.")],
     ("done", "The card was answered: Handled. Draft deleted, thread archived.")),
    ("cancelled", [zcard(":prohibited: Send cancelled. Draft kept in Outlook.")],
     ("done", "The card was answered: Send cancelled. Draft kept in Outlook.")),
    ("revised", [zcard(":pencil: Revised. The new Send card for this draft is below.")],
     ("done", "The card was answered: Revised. The new Send card for this draft is below.")),
    ("deleted in Outlook", [zcard(":wastebasket: Deleted in Outlook; not sent.")],
     ("done", "The card was answered: Deleted in Outlook; not sent.")),
    ("a status of two lines reads its first",
     [zcard(":check: Sent.\n:check: Closed: `response-a1`")],
     ("done", "The card was answered: Sent.")),
    ("a failed press is not settled",
     [zcard(":cross_mark: Could not authorize this card: expired")], ("none", "A card from Tacitus: " + ts.QUIET_TAIL)),
    ("a warning is not settled", [zcard(":warning: This send confirmation is stale")], ("none", "A card from Tacitus: " + ts.QUIET_TAIL)),
    ("a refusal is not settled", [zcard(":no_entry: The draft is gone.")], ("none", "A card from Tacitus: " + ts.QUIET_TAIL)),
    ("a press under way that Zulip edited a moment ago",
     [zcard(":time_ticking: Sending...",
            primary={"text": ZHEAD + ZRULE + ":time_ticking: Sending...", "filed": False,
                     "edited_at": NOW - MIN})],
     ("working", "Tacitus is acting on the button you pressed.")),
    ("a press under way with no time but the copy's", [zcard(":time_ticking: Sending...")],
     ("none", "A card from Tacitus: " + ts.QUIET_TAIL)),
    ("Zulip's text wins over the stored copy",
     [zcard(primary={"text": ZHEAD + ZRULE + ":mail_received: Sent from Outlook.",
                     "filed": False, "edited_at": NOW - 600})],
     ("done", "The card was answered: Sent from Outlook.")),
    ("Zulip says the card is still live",
     [zcard(primary={"text": ZHEAD + ZRULE + CHIPS, "filed": False, "edited_at": None})],
     ("needs_you", 'The card "Confirm send" still has its buttons.')),
    ("filed in the done stream with its status",
     [zcard(primary={"text": ZHEAD + ZRULE + ":check: Handled.", "filed": True,
                     "edited_at": NOW - 600})],
     ("done", "The card was answered: Handled.")),
    ("filed in the done stream with no status that acted",
     [zcard(primary={"text": ZHEAD + ZRULE + CHIPS, "filed": True, "edited_at": None})],
     ("done", "The card was answered: Settled outside chat.")),
    ("settled, then the owner asks something", [
        zcard(":check: **Sent**"),
        {**m(2, "owner", "And the other one?", at=NOW - 30 * MIN), "history": True}], None),
    ("an owner message after the card counts the same when Zulip answered", [
        zcard(primary={"text": ZHEAD + ZRULE + ":check: **Sent**", "filed": False,
                       "edited_at": NOW - MIN}),
        {**m(2, "owner", "send ABCD", at=NOW - 30 * MIN), "history": True}], None),
    ("settled, then the bot's note", [
        zcard(":check: **Sent**"),
        {**m(2, text=":check: Calendar event created.", at=NOW - 30 * MIN), "history": True}],
     ("done", "The card was answered: Sent")),
]


@pytest.mark.parametrize("name,msgs,want", MIRROR_CASES, ids=[c[0] for c in MIRROR_CASES])
def test_a_mirrored_card_reads_its_outcome(name, msgs, want):
    got = decide(msgs)
    assert (None if got is None else (got[0], got[2])) == want
    if got is not None:
        assert got[1] == "rule"


def test_only_a_card_copied_from_zulip_is_read_as_one():
    pointer = {**m(1, text=":locked: The Send button for this draft is in your DM: "
                         "[open it](https://chat.example.org/#narrow)"),
               "channel": "stream:6", "history": True}
    for row in (pointer,
                {**zcard(), "history": False},             # a live row
                zcard(channel="C_SLACK"),                  # not a Zulip stream
                {**zcard(), "card": live_card()},          # it has its card JSON
                {**m(1, text="**Morning briefing**" + ZRULE + "one"), "channel": "stream:6",
                 "history": True}):
        assert not ts.mirrored_card(row)
        assert ts.card_view(row) is row
    assert ts.mirrored_card(zcard())
    assert ts.card_pending(zcard()["text"]) and ts.card_pending(
        zcard(":time_ticking: Sending...")["text"])
    assert not ts.card_pending(zcard(":check: **Sent**")["text"])
    assert not ts.card_pending(zcard(":cross_mark: Could not send")["text"])


def test_the_prompt_reads_a_mirrored_card_as_a_card():
    p = ts.judge_prompt([zcard(":cross_mark: Could not send"),
                         {**m(2, "owner", "Why?"), "history": True}], now=NOW)
    assert 'card "Confirm send": Could not send' in p
    assert "model never sees" not in p and "x@example.test" not in p
    p = ts.judge_prompt([zcard(at=NOW - 8 * DAY)], now=NOW)
    assert 'card "Confirm send": no live buttons' in p


ZKEY = "stream:6:topic:lock%20ABCD%20x"


class FakeCards:
    """The primary's card read: an answer per message id, every call kept."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, channel, thread_key, msg_id):
        self.calls.append((channel, thread_key, msg_id))
        got = self.answers.get(msg_id)
        if isinstance(got, Exception):
            raise got
        return got


def mirrored(store, n, middle=CHIPS, at=None):
    """`n` cards copied as text into the DM stream, one thread each,
    newest last; their thread keys."""
    store.upsert_channel("stream:6", "ea-dm", "dm")
    at = time.time() - 3 * 3600 if at is None else at
    keys = []
    for i in range(n):
        key = f"{ZKEY}{i}"
        store.add_message("stream:6", mid=str(700 + i), thread_key=key, author="bot",
                          kind="message", text=ZHEAD + ZRULE + middle + ZRULE + ZBODY,
                          posted_at=at + i, history=True)
        keys.append(key)
    return keys


def test_a_mirrored_card_is_read_from_the_primary_and_never_asked_of_the_model(store):
    live, settled = mirrored(store, 2)
    store.add_message("stream:6", mid="799", thread_key=f"{ZKEY}s", author="bot",
                      kind="message", text=ZHEAD + ZRULE + ":check: **Sent**",
                      posted_at=time.time() - 3 * 3600, history=True)
    cards = FakeCards({"700": {"text": ZHEAD + ZRULE + CHIPS, "filed": False, "edited_at": None},
                       "701": {"text": ZHEAD + ZRULE + ":mail_received: Sent from Outlook.",
                               "filed": True, "edited_at": time.time() - 600}})
    model = FakeModel()
    out = engine(store, model, cards=cards).tick()
    assert model.prompts == [] and out["model_calls"] == 0
    assert sorted(c[2] for c in cards.calls) == ["700", "701"]       # a settled copy is final
    assert ("stream:6", live, "700") in cards.calls
    assert store.thread_state("stream:6", live)["state"] == "needs_you"
    assert (store.thread_state("stream:6", settled)["state"],
            store.thread_state("stream:6", settled)["reason"]) == (
        "done", "The card was answered: Sent from Outlook.")
    assert store.thread_state("stream:6", f"{ZKEY}s")["state"] == "done"


def test_the_primary_s_card_reads_are_capped_and_kept_for_a_tick(store):
    keys = mirrored(store, 35)
    cards = FakeCards()                            # no answer: the stored text stands
    clock = [time.time()]
    e = engine(store, FakeModel(), cards=cards, clock=lambda: clock[0])
    assert e.tick()["card_reads"] == ts.CARD_CAP == 30
    assert len(cards.calls) == 30
    # past the cap a card keeps its stored legend: needs you, by rule
    assert {store.thread_state("stream:6", k)["by"] for k in keys} == {"rule"}
    assert e.tick()["card_reads"] == 5             # the rest; the 30 answers are kept
    assert len({c[2] for c in cards.calls}) == 35
    assert e.tick()["card_reads"] == 0
    clock[0] += ts.TICK_S                          # one tick interval later they are read again
    assert e.tick()["card_reads"] == 30
    # a change between ticks draws on the tick's allowance, spent here
    e.changed()
    store.add_message("stream:6", mid=None, thread_key=keys[0], author="bot", kind="message",
                      text="Still waiting.", posted_at=time.time())
    before = len(cards.calls)
    assert e.changed() == 1 and len(cards.calls) == before


def test_no_primary_means_no_read(store, tmp_path):
    keys = mirrored(store, 3)
    e = engine(store, FakeModel())
    assert e.cards is None
    assert e.tick()["card_reads"] == 0
    assert {store.thread_state("stream:6", k)["state"] for k in keys} == {"needs_you"}
    # a failing read is logged once and the stored text stands
    e = engine(store, FakeModel(), cards=FakeCards({"700": RuntimeError("down")}))
    e.tick()
    assert store.thread_state("stream:6", keys[0])["state"] == "needs_you"
    # the web chat alone has no read; one set later reaches the running states
    f = WebFrontend({"port": 0}, store_path=tmp_path / "w" / "webchat.sqlite",
                    webui_dir=tmp_path / "webui")
    states = f.enable_thread_state()
    assert f.card_lookup is None and states.cards is None
    read = FakeCards()
    f.card_lookup = read
    assert states.cards is read


# ── a conversation's cards in Direct messages (rule 2b) ───────────────

def dm_card(card_id="w9", *, kind="card", text="", at=None, updated=None, history=False,
            card=None, channel="dm"):
    """A card the conversation links to, in its own thread in the DM."""
    row = {**m(9, kind=kind, text=text, at=at, updated=updated),
           "channel": channel, "id": card_id, "thread_key": f"t-{card_id}",
           "card": live_card() if card is None else card}
    if history:
        row["history"] = True
    return row


ASK = [m(1, "owner", "Draft a reply to Avery.", reactions=["white_check_mark"], at=NOW - 3 * 3600),
       m(2, text=":lock: The Send button for this draft is in your DM: "
                 "[open it](https://h.example.test/chat/#/dm/t-w9/w9)", at=NOW - 3 * 3600 + 60),
       m(3, text="Draft created. It says both trips are covered.", at=NOW - 3 * 3600 + 61)]

LINK_CASES = [
    # (name, linked cards, more messages after ASK, expected (state, reason) or None)
    ("a live card in the DM waits for the owner", [dm_card()], [],
     ("needs_you", "Its card in Direct messages waits for you.")),
    ("a card sent from the DM settles the conversation",
     [dm_card(kind="status", text=":white_check_mark: Sent.", updated=NOW - 600)], [],
     ("done", "Its card was handled: Sent.")),
    ("several cards, all handled: the latest outcome is named",
     [dm_card("w8", kind="status", text=":pencil2: Revised. The new Send card for this draft is "
                                         "below.", updated=NOW - 900),
      dm_card(kind="status", text=":incoming_envelope: Sent from Outlook.", updated=NOW - 600)],
     [], ("done", "Its cards were handled: Sent from Outlook.")),
    ("one handled and one live is live",
     [dm_card("w8", kind="status", text=":white_check_mark: Sent.", updated=NOW - 900),
      dm_card()], [], ("needs_you", "Its card in Direct messages waits for you.")),
    ("an owner message after the last outcome is the later rules'",
     [dm_card(kind="status", text=":white_check_mark: Sent.", updated=NOW - 600)],
     [m(4, "owner", "Thanks. And the other one?", reactions=["white_check_mark"], at=NOW - 300),
      m(5, text="Which one do you mean?", at=NOW - 290)], None),
    ("a bot line after the outcome does not reopen it",
     [dm_card(kind="status", text=":white_check_mark: Sent.", updated=NOW - 600)],
     [m(4, text=":white_check_mark: Sent to Avery.", at=NOW - 300)],
     ("done", "Its card was handled: Sent.")),
    ("a press under way is working",
     [dm_card(kind="status", text=":hourglass_flowing_sand: Sending...", updated=NOW - 60)], [],
     ("working", "Tacitus is acting on the button you pressed.")),
    ("a press stuck for long decides nothing",
     [dm_card(kind="status", text=":hourglass_flowing_sand: Sending...", updated=NOW - 3600)], [],
     None),
    ("a failed press decides nothing",
     [dm_card(kind="status", text=":x: Could not send: the draft is gone.", updated=NOW - 600)],
     [], None),
    ("a card past the registry's age decides nothing", [dm_card(at=NOW - 8 * DAY)], [], None),
    ("a link to a card the store does not hold keeps it open",
     [dm_card(kind="status", text=":white_check_mark: Sent.", updated=NOW - 600),
      {"kind": "missing", "id": "5522", "card": None}], [], None),
    ("a linked message that is no card decides nothing",
     [{**m(9, text="Just a note."), "channel": "dm", "id": "w9"}], [], None),
    ("a card copied from Zulip, settled there",
     [{**zcard(":check: **Sent**", at=NOW - 2 * 3600 - 30), "id": "5522"}], [],
     ("done", "Its card was handled: Sent")),
    ("a card copied from Zulip, still with its legend",
     [{**zcard(at=NOW - 2 * 3600), "id": "5522"}], [],
     ("needs_you", "Its card in Direct messages waits for you.")),
]


@pytest.mark.parametrize("name,linked,more,want", LINK_CASES, ids=[c[0] for c in LINK_CASES])
def test_a_conversation_reads_its_cards_in_direct_messages(name, linked, more, want):
    got = decide(ASK + more, linked=linked)
    assert got == (None if want is None else (want[0], "rule", want[1]))


def test_the_link_rule_sits_after_the_live_card_rule_and_before_the_owner_s_last_word():
    sent = [dm_card(kind="status", text=":white_check_mark: Sent.", updated=NOW - 600)]
    # a live card in the conversation itself names that card
    here = ASK + [m(4, kind="card", card=live_card(":lock: *Confirm invite*"))]
    assert decide(here, linked=sent)[2] == 'The card "Confirm invite" still has its buttons.'
    # the owner's override still holds
    prev = {"state": "done", "by": "owner", "reason": "You marked this done.", "judged_seq": 3}
    assert decide(ASK, linked=[dm_card()], prev=prev)[:2] == ("done", "owner")
    # the owner's message after the card, unanswered past the window, is a
    # stuck turn with the card or without it (rule 1a, then rule 3)
    asked = ASK + [m(4, "owner", "Make it shorter.", at=NOW - 30 * MIN)]
    assert decide(asked, linked=[dm_card()]) == ("needs_you", "rule", ts.NOT_ANSWERED)
    assert decide(asked)[2] == ts.NOT_ANSWERED
    # with no link the conversation is the model's, as before
    assert decide(ASK) is None and decide(ASK, linked=[]) is None


# ── the owner's change to a live card (rule 1a) ───────────────────────

CARD_ASK = [m(1, "owner", "Draft a reply to Avery.", reactions=["white_check_mark"],
              at=NOW - 40 * MIN),
            m(2, kind="card", card=live_card(), at=NOW - 39 * MIN)]
CARD_HERE = 'The card "Confirm send" still has its buttons.'
REVISED = ":pencil2: Revised. The new Send card for this draft is below."


def change(seq, text="Make it shorter.", at=NOW - 5 * MIN, **kw):
    return m(seq, "owner", text, at=at, **kw)


def test_the_owner_s_change_to_a_card_in_the_thread_is_working_then_stuck():
    asked = CARD_ASK + [change(3, reactions=["eyes"])]
    assert decide(asked) == ("working", "rule", ts.CHANGE_WORKING)
    # a progress line is no answer: still working, the window counts from it
    busy = asked + [m(4, text=":hourglass_flowing_sand: Still working... (21m)", at=NOW - MIN)]
    assert decide(busy, now=NOW + 16 * MIN) == ("working", "rule", ts.CHANGE_WORKING)
    # twenty minutes with no answer is a stuck turn, the card still live
    assert decide(asked, now=NOW + 20 * MIN) == ("needs_you", "rule", ts.NOT_ANSWERED)
    # a failed turn needs the owner at once
    assert decide(CARD_ASK + [change(3, reactions=["x"])]) == \
        ("needs_you", "rule", ts.NOT_ANSWERED)
    failed = asked + [m(4, text=":warning: I hit my usage limit.", at=NOW - MIN)]
    assert decide(failed) == ("needs_you", "rule", ts.NOT_ANSWERED)


def test_once_the_bot_answers_the_change_the_card_rule_decides_again():
    asked = CARD_ASK + [change(3)]
    # a revised card: the old one retired, a new one below the change
    revised = [CARD_ASK[0], m(2, kind="status", text=REVISED, card=live_card(),
                              at=NOW - 39 * MIN, updated=NOW - 2 * MIN),
               change(3), m(4, kind="card", card=live_card(":lock: *Confirm send (short)*"),
                            at=NOW - 2 * MIN)]
    assert decide(revised) == ("needs_you", "rule",
                               'The card "Confirm send (short)" still has its buttons.')
    # the card revised in place
    in_place = [CARD_ASK[0], m(2, kind="card", card=live_card(), at=NOW - 39 * MIN,
                               updated=NOW - MIN), change(3)]
    assert decide(in_place) == ("needs_you", "rule", CARD_HERE)
    # a message from the bot
    assert decide(asked + [m(4, text="Shorter now: three lines.", at=NOW - MIN)]) == \
        ("needs_you", "rule", CARD_HERE)
    # a finished turn with no words, and a change copied from Zulip's past,
    # are not decisive: the card's buttons are
    assert decide(CARD_ASK + [change(3, reactions=["white_check_mark"])]) == \
        ("needs_you", "rule", CARD_HERE)
    assert decide(CARD_ASK + [{**change(3), "history": True}]) == ("needs_you", "rule", CARD_HERE)
    # the owner's override still wins
    prev = {"state": "done", "by": "owner", "reason": "You marked this done.", "judged_seq": 3}
    assert decide(asked, prev=prev)[:2] == ("done", "owner")


def test_the_owner_s_change_to_a_linked_card_is_working_then_stuck():
    asked = ASK + [change(4, "Add the budget line.")]
    assert decide(asked, linked=[dm_card()]) == ("working", "rule", ts.CHANGE_WORKING)
    assert decide(asked, linked=[dm_card()], now=NOW + 20 * MIN) == \
        ("needs_you", "rule", ts.NOT_ANSWERED)
    # a card posted in the DM after the owner's message answers it, with a
    # pointer or through the card's own link alone
    old = dm_card("w8", kind="status", text=REVISED, updated=NOW - 2 * MIN)
    new = dm_card(at=NOW - 2 * MIN)
    pointer = m(5, text=":lock: The new Send card for the revised draft is in your DM: "
                        "[open it](https://h.example.test/chat/#/dm/t-w9/w9)", at=NOW - 2 * MIN)
    assert decide(asked + [pointer], linked=[old, new]) == ("needs_you", "rule", ts.CARD_WAITS)
    assert decide(asked, linked=[old, new]) == ("needs_you", "rule", ts.CARD_WAITS)
    # the card revised in place, and a message from the bot
    assert decide(asked, linked=[dm_card(updated=NOW - MIN)]) == \
        ("needs_you", "rule", ts.CARD_WAITS)
    assert decide(asked + [m(5, text="Added the line.", at=NOW - MIN)], linked=[dm_card()]) == \
        ("needs_you", "rule", ts.CARD_WAITS)
    # a press under way in the DM keeps its own reason
    assert decide(asked, linked=[dm_card(kind="status", text=":hourglass_flowing_sand: "
                                                             "Sending...", updated=NOW - MIN)]) \
        == ("working", "rule", ts.CARD_BUSY)


def test_the_closure_label_names_the_rule_of_the_owner_s_change():
    import closure_labels as cl

    def rule(msgs, now=NOW, **kw):
        f, _head = cl.thread_features(msgs, channel={"kind": "dm"}, now=now, **kw)
        return f["rule_id"], f["rule_state"], f["stuck_turn"]
    asked = CARD_ASK + [change(3)]
    assert rule(asked) == ("1a-reply-over-card", "working", False)
    assert rule(asked, now=NOW + 20 * MIN) == ("3-stuck", "needs_you", True)
    assert rule(asked + [m(4, text="Shorter now.", at=NOW - MIN)]) == \
        ("2-live-card", "needs_you", False)
    linked = ASK + [change(4, "Add the budget line.")]
    assert rule(linked, linked=[dm_card()]) == ("1a-reply-over-card", "working", False)
    assert rule(linked, linked=[dm_card()], now=NOW + 20 * MIN) == ("3-stuck", "needs_you", True)
    assert rule(linked, linked=[dm_card(updated=NOW - MIN)]) == \
        ("2b-linked-card", "needs_you", False)
    assert cl.rule_id("working", ts.CHANGE_WORKING) == "1a-reply-over-card"


def test_the_engine_follows_the_owner_s_change_and_the_bot_s_answer(store):
    clock = [time.time()]
    e = engine(store, FakeModel(), clock=lambda: clock[0])
    e.changed()
    # a card in the thread itself
    ask = store.add_message("dm", mid=None, thread_key=None, author="owner", kind="message",
                            text="Draft a reply to Avery.", posted_at=clock[0] - 120)
    key = ask["thread_key"]
    card = store.add_message("dm", mid=None, thread_key=key, author="bot", kind="card",
                             text="Confirm send", card=live_card(), posted_at=clock[0] - 60)
    # a card in the DM for a conversation in main, with its pointer
    ask2 = store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                             text="Draft a reply to Blake.", posted_at=clock[0] - 120)
    key2 = ask2["thread_key"]
    card2 = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card",
                              text="Confirm send", card=_send_card("main", key2),
                              posted_at=clock[0] - 60)
    e.changed()
    assert store.thread_state("dm", key)["state"] == "needs_you"
    assert store.thread_state("main", key2)["reason"] == ts.CARD_WAITS
    for channel, k in (("dm", key), ("main", key2)):
        store.add_message(channel, mid=None, thread_key=k, author="owner", kind="message",
                          text="Make it shorter.", posted_at=clock[0])
    e.changed()
    for channel, k in (("dm", key), ("main", key2)):
        st = store.thread_state(channel, k)
        assert (st["state"], st["by"], st["reason"]) == ("working", "rule", ts.CHANGE_WORKING)
    # no answer for twenty minutes: the tick calls the turn stuck
    clock[0] += 21 * MIN
    assert e.tick()["model_calls"] == 0
    for channel, k in (("dm", key), ("main", key2)):
        st = store.thread_state(channel, k)
        assert (st["state"], st["reason"]) == ("needs_you", ts.NOT_ANSWERED)
    # the bot answers: a revised card in the thread, the DM card revised in place
    store.change_message("dm", card["id"], "status", kind="status", text=REVISED)
    store.add_message("dm", mid=None, thread_key=key, author="bot", kind="card",
                      text="Confirm send", card=live_card(":lock: *Confirm send (short)*"),
                      posted_at=clock[0])
    store.change_message("dm", card2["id"], "card", kind="card",
                         card=_send_card("main", key2, ":lock: *Confirm send (short)*"))
    e.changed()
    st = store.thread_state("dm", key)
    assert (st["state"], st["reason"]) == \
        ("needs_you", 'The card "Confirm send (short)" still has its buttons.')
    assert store.thread_state("main", key2)["reason"] == ts.CARD_WAITS


def test_pointers_name_their_card_on_each_platform():
    web = (":lock: The Create button for this event is in your DM: "
           "[open it](https://h.example.test/chat/#/dm/t5/w5)")
    zulip = (":locked: The Publish button for this change is in your DM: [open it]"
             "(https://chat.example.org/#narrow/channel/6-ea-dm/topic/lock.20W7KL/near/5664)")
    slack = (":lock: The Send button for this draft is in your DM: "
             "[open it](https://x.slack.com/archives/D0123ABC/p1700000000123456)")
    assert ts.pointer_targets(web) == [("dm", "w5")]
    assert ts.pointer_targets(zulip) == [("", "5664")]
    assert ts.pointer_targets(slack) == [("D0123ABC", "1700000000.123456")]
    assert ts.pointer_targets(web + "\n" + zulip) == [("dm", "w5"), ("", "5664")]
    for text in (":lock: The Send button for this draft is in your DM.",  # no link
                 "I made a reply draft. The Send button is in your DM.",
                 ":sunny: **Morning briefing**: 3 drafts are waiting as a card in your DM: one.",
                 "See [the card](https://h.example.test/chat/#/dm/t5/w5)."):
        assert ts.pointer_targets(text) == [], text
    assert ts.card_origin({"ea_channel": "main", "thread_ts": "t4"}) == ("main", "t4")
    assert ts.card_origin({"ea_channel": "main", "thread_ts": ""}) is None
    assert ts.card_origin(None) is None


def _send_card(origin_channel="main", origin_key="", title=":lock: *Confirm send*"):
    return {**live_card(title), "ea_channel": origin_channel, "thread_ts": origin_key}


def test_a_card_posted_for_a_conversation_moves_its_state(store):
    e = engine(store)
    e.changed()
    ask = store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                            text="Draft a reply to Avery.", posted_at=time.time() - 120)
    store.toggle_reaction("main", ask["id"], "white_check_mark", True)
    card = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card",
                             text="Confirm send", card=_send_card("main", ask["thread_key"]))
    store.add_message("main", mid=None, thread_key=ask["thread_key"], author="bot",
                      kind="message", text="Draft created.")
    e.changed()
    st = store.thread_state("main", ask["thread_key"])
    assert (st["state"], st["reason"]) == ("needs_you", ts.CARD_WAITS)
    assert store.thread_state("dm", card["thread_key"])["state"] == "needs_you"
    # the press in the DM moves the conversation it came from too
    start = store.max_event_seq()
    store.change_message("dm", card["id"], "status", kind="status",
                         text=":white_check_mark: Sent.")
    assert e.changed() == 2
    st = store.thread_state("main", ask["thread_key"])
    assert (st["state"], st["by"], st["reason"]) == ("done", "rule", "Its card was handled: Sent.")
    assert sorted(json.loads(p)["thread_key"] for _s, t, p in store.events_since(start)
                  if t == "thread_state") == sorted([ask["thread_key"], card["thread_key"]])
    # a card asked for in its own thread is no link
    store.add_message("dm", mid=None, thread_key=card["thread_key"], author="bot", kind="card",
                      text="Again", card=_send_card("dm", card["thread_key"]))
    assert store.linked_cards("dm", card["thread_key"]) == []


def test_links_made_before_the_table_are_found_once_at_start(store):
    """Rows stored before card_link: a web pointer, a Zulip pointer to a card
    copied as text, and a card whose JSON names its conversation."""
    store.upsert_channel("stream:6", "ea-dm", "dm")
    store.upsert_channel("stream:4", "ea", "main")
    old = time.time() - 30 * DAY
    zask = store.add_message("stream:4", mid="100", thread_key="stream:4:topic:Avery", author="owner",
                             kind="message", text="Draft a reply.", posted_at=old, history=True)
    zcard_row = store.add_message("stream:6", mid="101", thread_key=ZKEY, author="bot",
                                  kind="message", text=ZHEAD + ZRULE + ":check: **Sent**",
                                  posted_at=old + 60, history=True)
    store.add_message("stream:4", mid="102", thread_key=zask["thread_key"], author="bot",
                      kind="message", history=True, posted_at=old + 61,
                      text=":locked: The Send button for this draft is in your DM: [open it]"
                           "(https://chat.example.org/#narrow/channel/6-ea-dm/topic/x/near/101)")
    wask = store.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                             text="Make the invite.", posted_at=old)
    wcard = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="status",
                              text=":white_check_mark: Created.", card=live_card(),
                              posted_at=old + 60)
    store.add_message("main", mid=None, thread_key=wask["thread_key"], author="bot",
                      kind="message", posted_at=old + 61,
                      text=f":lock: The Create button for this event is in your DM: [open it]"
                           f"(https://h.example.test/chat/#/dm/{wcard['thread_key']}/{wcard['id']})")
    jask = chat_thread(store, at=old)
    store.add_message("dm", mid=None, thread_key=None, author="bot", kind="status",
                      text=":white_check_mark: Sent.", card=_send_card("main", jask),
                      posted_at=old + 300)
    for key in (zask["thread_key"], wask["thread_key"], jask):
        ch = "stream:4" if key.startswith("stream:") else "main"
        store.set_thread_state(ch, key, "needs_you", "model", "Old.",
                               judged_seq=store.newest_seq(ch, key))
    # as a store from before the table holds them: no links, and no mark
    store._conn.execute("DELETE FROM card_link")
    store._conn.execute("DELETE FROM meta WHERE key='card_link_backfill'")
    store._conn.commit()
    e = engine(store, FakeModel())
    assert e.link_cards() == 3
    assert [r["id"] for r in store.linked_cards("stream:4", zask["thread_key"])] == ["101"]
    assert [r["id"] for r in store.linked_cards("main", wask["thread_key"])] == [wcard["id"]]
    for ch, key, what in (("stream:4", zask["thread_key"], "Sent"),
                          ("main", wask["thread_key"], "Created."), ("main", jask, "Sent.")):
        st = store.thread_state(ch, key)
        assert (st["state"], st["reason"]) == ("done", f"Its card was handled: {what}"), key
    # once per store; and a card that moves channel stays linked
    assert store.backfill_card_links() == 0
    store.upsert_channel("stream:49", "ea-dm-done", "room")
    store.move_thread("stream:6", ZKEY, "stream:49", ZKEY)
    assert [r["channel"] for r in store.linked_cards("stream:4", zask["thread_key"])] == ["stream:49"]
    store.move_thread("stream:4", zask["thread_key"], "stream:4", "stream:4:topic:Avery2")
    assert [r["id"] for r in store.linked_cards("stream:4", "stream:4:topic:Avery2")] == ["101"]


# ── the hosted step: one call a tick over a batch of threads ─────────

HEAD_RE = re.compile(r"^=== conversation (c\d+) ===$", re.M)
READ = "The colleague has not replied yet."


def item(ref, state="waiting", reason=READ, confidence=0.8, **more):
    return {"ref": ref, "state": state, "reason": reason, "confidence": confidence, **more}


class FakeHosted:
    """The hosted call: keeps each prompt and answers every ref in it as
    waiting, or with what `answer(refs)` gives."""

    def __init__(self, answer=None):
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        refs = HEAD_RE.findall(prompt)
        if self.answer is not None:
            return self.answer(refs)
        return json.dumps([item(r) for r in refs])


def hosted_engine(store, hosted, route="hosted", model=None, **kw):
    return ts.ThreadStates(lambda: store, model=model, hosted=hosted, route=lambda: route, **kw)


def test_the_batch_answer_parses_strictly():
    refs = ["c1", "c2"]
    both = [item("c1"), item("c2", "done", "Sent. The rest is noise.", 1)]
    want = {"c1": {"state": "waiting", "reason": READ, "confidence": 0.8},
            "c2": {"state": "done", "reason": "Sent.", "confidence": 1.0}}
    assert ts.parse_batch(json.dumps(both), refs) == want
    assert ts.parse_batch("```json\n" + json.dumps(both) + "\n```", refs) == want
    # anything but one array is no answer it could use, for every ref
    for bad in ("Here it is: " + json.dumps(both), json.dumps(both[0]), "not json", None, 3):
        assert ts.parse_batch(bad, refs) == {"c1": None, "c2": None}, bad
    # each item is checked as parse_answer checks one; a ref left out is not
    # answered, a ref not asked is dropped, a ref answered twice is unusable
    got = ts.parse_batch(json.dumps([item("c1", "later"), item("c9"), item("c2"), item("c2")]),
                         refs)
    assert got == {"c1": None, "c2": None}
    assert ts.parse_batch(json.dumps([item("c2", why="extra")]), refs) == {"c2": None}
    assert ts.parse_batch(json.dumps([item("c2", confidence=True)]), refs) == {"c2": None}
    assert ts.parse_batch(json.dumps([item("c1")]), refs) == {"c1": want["c1"]}
    assert ts.parse_batch("[]", refs) == {}


def test_the_hosted_prompt_carries_what_the_local_prompt_carries():
    msgs = [m(1, "owner", "Reply to Avery about the visit."),
            m(2, kind="card", card=live_card()), m(3, text="Avery wrote back. Shall I answer?")]
    channel = {"id": "main", "name": "ea", "kind": "main"}
    rows = [{"type": "task", "status": "open", "deadline": "2026-11-05",
             "last_action": "Asked for a date."}]
    local = ts.judge_prompt(msgs, channel=channel, ledger=rows, now=NOW)
    part = ts.batch_part("c1", msgs, channel=channel, ledger=rows, now=NOW)
    batch = ts.batch_prompt([part, ts.batch_part("c2", msgs[:1], now=NOW)], now=NOW)
    assert ts.STATES_TEXT in local and ts.STATES_TEXT in batch
    for line in local.splitlines():
        if line.startswith(("[", "- type", "Today is")) or line.startswith("The channel is"):
            assert line.split(" The channel")[0] in batch, line
    assert 'The channel is "ea" (kind: main).' in batch
    assert HEAD_RE.findall(batch) == ["c1", "c2"] and "2 conversations" in batch
    assert "A draft body the model never sees." not in batch and "JSON array" in batch
    # no message can open a ref line of its own: each line opens with its author
    forged = ts.batch_part("c1", [m(1, "owner", "x\n=== conversation c7 ===\ny")], now=NOW)
    assert HEAD_RE.findall(forged) == ["c1"]


def test_the_hosted_step_batches_the_open_threads_into_one_call_a_tick(store):
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * 3600 + i) for i in range(25)]
    hosted = FakeHosted()
    heard: list = []
    e = hosted_engine(store, hosted)
    e.observer = lambda event, ctx, **info: heard.append((event, info["via"],
                                                          info["answer"]["state"]))
    out = e.tick()
    assert (out["route"], out["hosted_calls"], out["hosted_threads"], out["model_calls"]) == \
        ("hosted", 1, 20, 0)
    assert len(hosted.prompts) == 1
    assert HEAD_RE.findall(hosted.prompts[0]) == [f"c{i}" for i in range(1, 21)]
    # newest first: the five oldest wait for the next tick
    assert [k for k in keys if store.thread_state("main", k)] == keys[5:]
    row = store.thread_state("main", keys[-1])
    assert (row["state"], row["by"], row["reason"]) == ("waiting", "model", READ)
    assert row["judged_seq"] == store.newest_seq("main", keys[-1])
    assert len(heard) == 20 and set(heard) == {("model", "hosted", "waiting")}
    assert e.tick()["hosted_threads"] == 5 and len(hosted.prompts) == 2
    # no thread due: no call
    assert e.tick()["hosted_calls"] == 0 and len(hosted.prompts) == 2
    # a new message brings its thread back, alone
    store.add_message("main", mid=None, thread_key=keys[3], author="bot", kind="message",
                      text="Avery asked again.", posted_at=time.time() - 3 * 3600)
    assert e.tick()["hosted_threads"] == 1 and "post 3" in hosted.prompts[-1]
    # a change the rules leave open waits for the tick: one call holds the batch
    chat_thread(store, "another")
    e.changed()
    assert not e._due


def test_partial_and_unusable_hosted_answers(store):
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * 3600 + i) for i in range(6)]
    # the refs go newest first: c1 is keys[5], c6 is keys[0]
    seq = store.newest_seq("main", keys[3])
    store.set_thread_state("main", keys[3], "needs_you", "model", "Earlier.", judged_seq=seq - 1)

    def answer(refs):
        return "```json\n" + json.dumps([
            item("c1", "done", "Sent and answered.", 0.9),
            item("c2", "done", confidence=0.3),              # unsure
            item("c3", "later"),                             # not a state
            item("c4", "done"), item("c4", "waiting"),       # twice
            item("c99", "done"),                             # not asked
            item("c5", why="an extra key")]) + "\n```"      # c6 left out
    hosted = FakeHosted(answer)
    e = hosted_engine(store, hosted)
    assert e.tick()["hosted_threads"] == 6
    st = {i: store.thread_state("main", k) for i, k in zip(range(6, 0, -1), keys)}
    assert (st[1]["state"], st[1]["reason"]) == ("done", "Sent and answered.")
    assert (st[2]["state"], st[2]["by"]) == ("none", "model") and "unsure" in st[2]["reason"]
    assert st[3]["state"] == "needs_you" and "no answer" in st[3]["reason"]
    assert st[3]["judged_seq"] == seq
    assert st[4]["state"] == st[5]["state"] == "none" and "no answer" in st[5]["reason"]
    assert st[6] is None
    # the thread left out is asked again, alone
    e.tick()
    assert HEAD_RE.findall(hosted.prompts[-1]) == ["c1"] and "post 0" in hosted.prompts[-1]
    # an answer that is no array at all: each thread keeps its state, judged
    key = chat_thread(store, "one more")
    store.set_thread_state("main", key, "waiting", "model", "Earlier.",
                           judged_seq=store.newest_seq("main", key) - 1)
    e.hosted = FakeHosted(lambda refs: "I could not read these.")
    e.tick()
    row = store.thread_state("main", key)
    assert (row["state"], row["judged_seq"]) == ("waiting", store.newest_seq("main", key))
    assert "no answer" in row["reason"]


def test_a_failed_hosted_call_keeps_every_state_and_asks_again(store, caplog):
    keys = [chat_thread(store, f"post {i}") for i in range(3)]
    seq = store.newest_seq("main", keys[0])
    store.set_thread_state("main", keys[0], "needs_you", "model", "Earlier.", judged_seq=seq - 1)
    before = store.thread_state("main", keys[0])

    def boom(refs):
        raise RuntimeError("claude rc=1: usage limit")
    e = hosted_engine(store, None)
    with caplog.at_level("WARNING", logger="chat.web.state"):
        for answer in (lambda refs: None, boom, boom):
            e.hosted = hosted = FakeHosted(answer)
            out = e.tick()
            assert out["hosted_calls"] == 1 and len(hosted.prompts) == 1
            assert store.thread_state("main", keys[0]) == before
            assert [store.thread_state("main", k) for k in keys[1:]] == [None, None]
    # one log line an hour for the same failure
    assert sum("hosted model did not judge" in r.getMessage() for r in caplog.records) == 1
    # the next tick asks again, and a working call writes them all
    e.hosted = FakeHosted()
    assert e.tick()["hosted_threads"] == 3
    assert {store.thread_state("main", k)["state"] for k in keys} == {"waiting"}


def test_auto_hands_what_the_local_model_left_to_the_hosted_call(store):
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * 3600 + i) for i in range(4)]
    local, hosted = FakeModel(answer=None), FakeHosted()
    out = hosted_engine(store, hosted, route="auto", model=local).tick()
    assert (out["model_calls"], out["stopped"], out["hosted_calls"], out["hosted_threads"]) == \
        (1, True, 1, 4)
    assert all(store.thread_state("main", k)["state"] == "waiting" for k in keys)
    # with the local model answering, no hosted call
    chat_thread(store, "fresh")
    local.answer = GOOD
    out = hosted_engine(store, hosted, route="auto", model=local).tick()
    assert (out["model_calls"], out["hosted_calls"]) == (1, 0) and len(hosted.prompts) == 1
    # set to local, a local model that does not answer leaves the rest to the
    # next tick; off asks no model
    chat_thread(store, "later")
    for route in ("local", "off"):
        out = hosted_engine(store, hosted, route=route, model=FakeModel(answer=None)).tick()
        assert out["hosted_calls"] == 0 and len(hosted.prompts) == 1, route
    # a route that fails reads off
    e = ts.ThreadStates(lambda: store, hosted=hosted, route=lambda: 1 / 0)
    assert e.tick()["route"] == "off" and len(hosted.prompts) == 1
    # with no route given, a hosted call alone is the hosted route
    assert ts.ThreadStates(lambda: store, hosted=hosted).tick()["hosted_calls"] == 1


def test_the_hosted_prompt_stays_under_its_byte_cap(store, monkeypatch):
    monkeypatch.setattr(ts, "HOSTED_PROMPT_BYTES", 6000)
    for i in range(10):
        chat_thread(store, f"post {i} " + "x" * 600, reply="y" * 600)
    hosted = FakeHosted()
    out = hosted_engine(store, hosted).tick()
    assert 1 <= out["hosted_threads"] < 10
    assert len(hosted.prompts[0].encode()) <= 6000
    # a thread larger than the cap goes alone rather than never
    monkeypatch.setattr(ts, "HOSTED_PROMPT_BYTES", 100)
    assert hosted_engine(store, hosted).tick()["hosted_threads"] == 1


def test_once_running_the_hosted_call_runs_beside_the_rules(store):
    keys = [chat_thread(store, f"post {i}") for i in range(2)]
    started, go = threading.Event(), threading.Event()

    def answer(refs):
        started.set()
        go.wait(5)
        return json.dumps([item(r) for r in refs])
    e = hosted_engine(store, FakeHosted(answer))
    e._thread = threading.current_thread()          # as start() leaves it
    try:
        assert e.tick()["hosted_calls"] == 1 and started.wait(5)
        assert [store.thread_state("main", k) for k in keys] == [None, None]
        # the rules still run at once, and no second call starts meanwhile
        row = store.add_message("main", mid=None, thread_key=None, author="owner",
                                kind="message", text="And the budget?")
        e.changed()
        assert store.thread_state("main", row["thread_key"])["state"] == "working"
        assert e.tick()["hosted_calls"] == 0
        go.set()
        e._hosted_worker.join(5)
        assert [store.thread_state("main", k)["state"] for k in keys] == ["waiting", "waiting"]
    finally:
        go.set()
        e._thread = None


# ── the stale shelf ───────────────────────────────────────────────────

def test_the_stale_shelf_counts_from_the_conversation_not_the_state(store):
    old_at = time.time() - ts.STALE_S - DAY
    keys = [chat_thread(store, f"post {i}", at=old_at if i < 3 else None) for i in range(4)]
    clock = [time.time()]
    e = engine(store, clock=lambda: clock[0])
    start = store.max_event_seq()
    for key, state in zip(keys, ("needs_you", "waiting", "done", "needs_you")):
        store.set_thread_state("main", key, state, "model", "x", judged_seq=store.newest_seq("main", key))
    # a new state on a conversation quiet past a week goes straight to the
    # shelf, in its own event; done is never stale, and a fresh one is not
    events = [json.loads(p) for _s, t, p in store.events_since(start) if t == "thread_state"]
    assert [ev["stale"] for ev in events] == [True, True, False, False]
    assert [bool(store.thread_state("main", k)["stale"]) for k in keys] == [True, True, False, False]
    assert e.shelve() == 0
    # a store from before marked by the state's age: the tick sets each mark
    # from the conversation, one event each, and once
    store._conn.execute("UPDATE thread_state SET stale=0")
    store._conn.execute("UPDATE thread_state SET stale=1 WHERE thread_key=?", (keys[3],))
    store._conn.commit()
    start = store.max_event_seq()
    assert e.tick()["stale"] == 3
    events = {json.loads(p)["thread_key"]: json.loads(p)["stale"]
              for _s, t, p in store.events_since(start) if t == "thread_state"}
    assert events == {keys[0]: True, keys[1]: True, keys[3]: False}
    assert e.tick()["stale"] == 0
    rows = {r["thread_key"]: r for r in store.threads("main")}
    assert [rows[k]["stale"] for k in keys] == [True, True, False, False]
    assert {r["thread_key"]: r["stale"] for r in store.needs()} == \
        {keys[0]: True, keys[1]: True, keys[3]: False}
    # a message takes the conversation off the shelf, though its state stays
    # (a rule's write of the same state sets the mark at once, with the event)
    store.add_message("main", mid=None, thread_key=keys[1], author="bot", kind="message",
                      text="The colleague wrote again.", posted_at=clock[0] - 60)
    start = store.max_event_seq()
    store.set_thread_state("main", keys[1], "waiting", "model", "x",
                           judged_seq=store.newest_seq("main", keys[1]))
    ((ev,),) = [[json.loads(p) for _s, t, p in store.events_since(start) if t == "thread_state"]]
    assert (ev["thread_key"], ev["state"], ev["stale"]) == (keys[1], "waiting", False)
    # the bot's nudge is no activity: it keeps nothing off the shelf
    assert store.post_nudge("main", keys[0], ts.NUDGE_TEXT) is not None
    assert e.shelve() == 0 and store.thread_state("main", keys[0])["stale"] == 1
    # the owner's own tap is fresh for a week from the tap
    store.set_thread_state("main", keys[0], "needs_you", "owner", ts.OWNER_REASONS["needs_you"],
                           judged_seq=store.newest_seq("main", keys[0]))
    assert store.thread_state("main", keys[0])["stale"] == 0
    assert store.thread_row("main", keys[0])["stale"] is False
    assert e.shelve() == 0
    clock[0] += ts.STALE_S + 60
    start = store.max_event_seq()
    assert e.shelve() == 3                          # the tap, the new message and the fresh one
    assert all(json.loads(p)["stale"] for _s, t, p in store.events_since(start) if t == "thread_state")


def test_the_shelf_and_the_nudge_clocks():
    prev = {"state": "needs_you", "by": "model", "at": NOW}
    msgs = [m(1, text="A question?", at=NOW - 9 * DAY), m(2, "owner", "Later.", at=NOW - 8 * DAY),
            m(3, text="Sure.", at=NOW - 3 * DAY)]
    assert ts.active_at(msgs, prev) == NOW - 3 * DAY
    assert ts.nudge_since(msgs, prev) == NOW - 8 * DAY
    tapped = {**prev, "by": "owner", "at": NOW - DAY}
    assert ts.active_at(msgs, tapped) == NOW - DAY and ts.nudge_since(msgs, tapped) == NOW - DAY
    # a conversation the owner never wrote in nor tapped gets no nudge
    assert ts.nudge_since(msgs[:1], prev) is None
    assert ts.nudge_since(msgs[:1], tapped) == NOW - DAY
    before = NOW - ts.STALE_S
    assert ts.is_stale("needs_you", "model", NOW, NOW - 8 * DAY, before)
    assert ts.is_stale("waiting", "rule", NOW, NOW - 8 * DAY, before)
    assert not ts.is_stale("done", "model", NOW, NOW - 30 * DAY, before)
    assert not ts.is_stale("needs_you", "model", NOW - 30 * DAY, NOW - DAY, before)
    assert not ts.is_stale("needs_you", "owner", NOW - DAY, NOW - 30 * DAY, before)
    assert ts.is_stale("needs_you", "owner", NOW - 8 * DAY, NOW - 30 * DAY, before)
    # no message: the state's own time
    assert ts.is_stale("needs_you", "model", NOW - 8 * DAY, None, before)


# ── the nudge ─────────────────────────────────────────────────────────

class Nudges:
    """The front end's nudge over the scratch store, every call kept."""

    def __init__(self, store):
        self.store = store
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, channel, key, text):
        self.calls.append((channel, key, text))
        return self.store.post_nudge(channel, key, text)


def _needs(store, key, hours, channel="main"):
    store.set_thread_state(channel, key, "needs_you", "model", "A question waits.",
                           judged_seq=store.newest_seq(channel, key))
    store._conn.execute("UPDATE thread_state SET at=? WHERE channel=? AND thread_key=?",
                        (time.time() - hours * 3600, channel, key))
    store._conn.commit()


def test_a_day_old_question_gets_one_nudge_that_changes_nothing(store):
    # the owner last wrote 30 hours ago; the state itself is an hour old
    key = chat_thread(store, at=time.time() - 30 * 3600)
    _needs(store, key, 1)
    model = FakeModel()
    nudge = Nudges(store)
    e = engine(store, model, nudge=nudge)
    e.changed()
    before = store.thread_state("main", key)
    start = store.max_event_seq()
    out = e.tick()
    assert out["nudges"] == 1 and nudge.calls == [("main", key, ts.NUDGE_TEXT)]
    row = store.thread("main", key)[-1]
    assert (row["author"], row["kind"], row["text"]) == ("bot", "message", ts.NUDGE_TEXT)
    assert store.nudge_ids("main", key) == {row["id"]}
    assert [t for _s, t, _p in store.events_since(start)] == ["message"]
    # the rules and the model never read it: no state moves, no question to the model
    e.changed()
    assert e.tick()["model_calls"] == 0 and model.prompts == []
    after = store.thread_state("main", key)
    assert {k: after[k] for k in ("state", "by", "reason", "at", "judged_seq")} == \
        {k: before[k] for k in ("state", "by", "reason", "at", "judged_seq")}
    assert ts.NUDGE_TEXT not in ts.judge_prompt(engine(store).contexts([("main", key)])[0]["messages"])
    # once per conversation
    assert e.tick()["nudges"] == 0 and len(nudge.calls) == 1
    assert store.post_nudge("main", key, ts.NUDGE_TEXT) is None
    # an owner reply after it is the rules' and the model's as ever
    store.add_message("main", mid=None, thread_key=key, author="owner", kind="message",
                      text="Yes, still.")
    e.changed()
    assert store.thread_state("main", key)["state"] == "working"


def test_the_nudge_skips_what_it_must(store):
    t0 = time.time() - 40 * 3600
    cases = {}
    # the clock is the owner's last message: 20 hours, though the state is older
    cases["young"] = chat_thread(store, "young", at=time.time() - 20 * 3600)
    _needs(store, cases["young"], 30)
    # quiet past a week: it shelves instead, though the state is new
    cases["stale"] = chat_thread(store, "stale", at=t0 - 10 * DAY)
    _needs(store, cases["stale"], 1)
    # the owner tapped Needs me two hours ago: the clock starts at the tap
    cases["tapped"] = chat_thread(store, "tapped", at=t0)
    store.set_thread_state("main", cases["tapped"], "needs_you", "owner",
                           ts.OWNER_REASONS["needs_you"],
                           judged_seq=store.newest_seq("main", cases["tapped"]))
    store._conn.execute("UPDATE thread_state SET at=? WHERE thread_key=?",
                        (time.time() - 2 * 3600, cases["tapped"]))
    cases["owner wrote since"] = chat_thread(store, "owner wrote since", at=t0)
    _needs(store, cases["owner wrote since"], 30)
    store.add_message("main", mid=None, thread_key=cases["owner wrote since"], author="owner",
                      kind="message", text="Any news?", posted_at=time.time() - 2 * 3600)
    store.add_message("main", mid=None, thread_key=cases["owner wrote since"], author="bot",
                      kind="message", text="Not yet.", posted_at=time.time() - 2 * 3600 + 30)
    cases["briefing"] = chat_thread(store, "**Morning briefing**: two items.", at=t0)
    _needs(store, cases["briefing"], 30)
    cases["notifier"] = chat_thread(store, "Seat open in a course.", at=t0)
    store.note_origin("main", cases["notifier"], "seat-watcher")
    _needs(store, cases["notifier"], 30)
    cases["waiting"] = chat_thread(store, "waiting", at=t0)
    store.set_thread_state("main", cases["waiting"], "waiting", "model", "x",
                           judged_seq=store.newest_seq("main", cases["waiting"]))
    store._conn.execute("UPDATE thread_state SET at=? WHERE thread_key=?",
                        (time.time() - 30 * 3600, cases["waiting"]))
    card = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="status",
                             text=":x: Could not send.", card=live_card(), posted_at=t0)
    cases["a card's own"] = card["thread_key"]
    _needs(store, card["thread_key"], 30, "dm")
    cases["live linked card"] = chat_thread(store, "live linked card", at=t0)
    store.add_message("dm", mid=None, thread_key=None, author="bot", kind="card", text="c",
                      card=_send_card("main", cases["live linked card"]), posted_at=time.time() - 3600)
    _needs(store, cases["live linked card"], 30)
    cases["due"] = chat_thread(store, "due", at=t0)
    _needs(store, cases["due"], 30)
    store._conn.commit()
    nudge = Nudges(store)
    e = engine(store, nudge=nudge)
    assert e.nudges() == 1 and [c[1] for c in nudge.calls] == [cases["due"]]
    # quiet hours hold every nudge; a check that fails counts as quiet
    cases["due later"] = chat_thread(store, "due later", at=t0)
    _needs(store, cases["due later"], 30)
    for quiet in (lambda: True, lambda: 1 / 0):
        e = engine(store, nudge=nudge, quiet=quiet)
        assert e.nudges() == 0
    assert engine(store, nudge=nudge, quiet=lambda: False).nudges() == 1
    assert engine(store).nudges() == 0             # no nudge to post through


def test_the_nudges_are_capped_a_tick_the_longest_waiting_first(store):
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * DAY - i * 3600) for i in range(13)]
    for key in keys:
        _needs(store, key, 1)
    nudge = Nudges(store)
    e = engine(store, nudge=nudge)
    assert e.nudges() == ts.NUDGE_CAP == 10
    assert [c[1] for c in nudge.calls] == keys[::-1][:10]
    assert e.nudges() == 3 and e.nudges() == 0
    # a conversation in an archived room gets none
    key = chat_thread(store, "archived", at=time.time() - 3 * DAY)
    _needs(store, key, 30)
    store.set_channel("main", archived=True)
    assert e.nudges() == 0


def test_the_ea_reads_quiet_hours_for_the_nudge(monkeypatch):
    import quiet_hours
    monkeypatch.setattr(quiet_hours, "should_defer", lambda priority="normal", now=None: True)
    assert wts.quiet_now() is True
    monkeypatch.setattr(quiet_hours, "should_defer", lambda priority="normal", now=None: False)
    assert wts.quiet_now() is False
    monkeypatch.setattr(quiet_hours, "should_defer", lambda *a, **k: 1 / 0)
    assert wts.quiet_now() is True
    f = WebFrontend.from_config({}, store_path="/nonexistent/webchat.sqlite")
    assert wts.enable(f) and f.thread_states.quiet is wts.quiet_now
    assert f.thread_states.nudge == f._post_nudge


# ── the EA's side: the ledger read and the model route ────────────────

def _ledger_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for migration in ledger.MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass
    rows = [("response-Ab1=", "needs_response", "open", "AAQkconv1=", "2026-11-05",
             "Asked for a date."),
            ("waiting-Cd2=", "task", "waiting", None, None, None),
            ("task-Ef3=", "task", "closed", "AAQkconv2=", None, None)]
    for rid, typ, status, conv, deadline, last in rows:
        conn.execute("INSERT INTO ledger (id, type, status, conversation_id, deadline, "
                     "last_action, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,'2026-10-01','2026-10-01')",
                     (rid, typ, status, conv, deadline, last))
    conn.commit()
    conn.close()
    return path


def test_the_ledger_read_is_read_only_and_in_the_ledger_s_words(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_is_owner", lambda: False)
    db = _ledger_db(tmp_path / "obligations-ledger.db")
    before = db.read_bytes()
    rows = wts.ledger_rows(["waiting-Cd2=", "follow-up"], ["AAQkconv1=", "AAQkconv2="], db_path=db)
    by = {r["id"]: r for r in rows}
    assert by["response-Ab1="] == {"id": "response-Ab1=", "type": "response_needed",
                                   "status": "open", "conversation_id": "AAQkconv1=",
                                   "deadline": "2026-11-05", "last_action": "Asked for a date."}
    assert (by["waiting-Cd2="]["type"], by["waiting-Cd2="]["status"]) == ("task", "waiting")
    assert by["task-Ef3="]["status"] == "completed"
    assert wts.ledger_rows([], []) == []
    assert db.read_bytes() == before
    # a thread linked this way gets its state from the rows
    got = ts.decide([m(1, text="x")], ledger=rows, now=NOW)
    assert got[0] == "needs_you"


@pytest.fixture
def routing(monkeypatch):
    def set_(value, cfg):
        block = ea_config.routing_from_block({"tasks": {"thread_state": value}})
        monkeypatch.setattr(ea_config, "get_routing", lambda *a, **k: block)
        monkeypatch.setattr(ea_config, "get_routing_source", lambda *a, **k: (block, "routing.json"))
        monkeypatch.setattr(rl, "config", lambda: cfg)
        wts._said.clear()
    return set_


LOCAL = {**rl._off(), "endpoint": "https://llm.example.test/v1", "model": "local-27b"}


def test_the_route_follows_the_table_and_each_model_answers_on_its_own(routing, monkeypatch):
    calls, runs = [], []

    def fake_chat(messages, **kw):
        calls.append(kw)
        return {"content": GOOD}
    monkeypatch.setattr(rl, "chat", fake_chat)
    monkeypatch.setattr(wts, "CLAUDE", lambda prompt: runs.append(prompt) or "[]")
    for value, cfg, want in (("local", LOCAL, "local"), ("auto", LOCAL, "auto"),
                             ("hosted", LOCAL, "hosted"),
                             # local with no endpoint reads hosted, as the table reads it
                             ("local", rl._off(), "hosted"), ("auto", rl._off(), "hosted")):
        routing(value, cfg)
        assert wts.route() == want, (value, cfg.get("endpoint"))
        local, hosted = wts.model_answer("prompt"), wts.hosted_answer("batch")
        assert (local is not None) == (want in ("local", "auto")), value
        assert (hosted is not None) == (want in ("hosted", "auto")), value
    assert calls[0]["purpose"] == "chat-thread-state" and calls[0]["schema"] == ts.ANSWER_SCHEMA
    assert len(calls) == 2 and runs == ["batch"] * 4

    def boom(messages, **kw):
        raise rl.RemoteLLMError("circuit open after repeated failures")
    monkeypatch.setattr(rl, "chat", boom)
    routing("local", LOCAL)
    assert wts.model_answer("prompt") is None


def test_a_failed_hosted_run_answers_none_and_logs_once_an_hour(routing, monkeypatch, caplog):
    def fail(prompt):
        raise RuntimeError("claude rc=1: usage limit")
    monkeypatch.setattr(wts, "CLAUDE", fail)
    routing("hosted", LOCAL)
    with caplog.at_level("INFO", logger="chat.web.state"):
        assert wts.hosted_answer("batch") is None
        assert wts.hosted_answer("batch") is None
    lines = [r.getMessage() for r in caplog.records if "hosted model did not judge" in r.getMessage()]
    assert len(lines) == 1 and "usage limit" in lines[0]


def test_the_hosted_run_is_the_tool_less_claude_call_the_reconcile_makes(monkeypatch):
    import mail_privacy
    got = []

    def run(prompt, model, tools, *, timeout):
        got.append((prompt, model, tools, timeout))
        return {"text": "[]", "rc": 0, "stderr": ""}
    monkeypatch.setattr(mail_privacy, "_run_claude", run)
    monkeypatch.setattr(ea_config, "get_models", lambda *a, **k: {"room_specialist": "x"})
    assert wts._claude("batch") == "[]"
    assert got == [("batch", "opus", "", wts.HOSTED_TIMEOUT_S)]
    monkeypatch.setattr(ea_config, "get_models", lambda *a, **k: {"thread_state": "claude-x-1"})
    wts._claude("batch")
    assert got[-1][1] == "claude-x-1"
    monkeypatch.setattr(mail_privacy, "_run_claude",
                        lambda *a, **k: {"text": "", "rc": 1, "stderr": "not logged in"})
    with pytest.raises(RuntimeError, match="not logged in"):
        wts._claude("batch")


def test_the_routing_table_names_the_task_and_never_warns_for_its_default(monkeypatch):
    block = ea_config.routing_from_block(None)
    assert block["tasks"]["thread_state"] == "local"
    monkeypatch.setattr(ea_config, "get_routing", lambda *a, **k: block)
    monkeypatch.setattr(ea_config, "get_routing_source", lambda *a, **k: (block, "default"))
    off = rl._off()
    row = {r["task"]: r for r in mr.rows(cfg=off, tiers=ea_config.tiers_from_block({}))}["thread_state"]
    assert row["effective"] == "hosted" and row["why"].endswith("your own model is not set up, so Claude does it")
    assert row["allowed"] == ["local", "hosted", "auto"] and not row["locked"]
    monkeypatch.setattr(rl, "config", lambda: off)
    status, _detail, fix = mr.doctor_row()
    assert "thread_state" not in fix
    row = {r["task"]: r for r in mr.rows(cfg=LOCAL, tiers=ea_config.tiers_from_block({}))}["thread_state"]
    assert row["effective"] == "local"


def test_the_bot_turns_the_states_on_for_its_web_chat():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "_web_thread_state.enable(_web)" in bot
    f = WebFrontend.from_config({}, store_path="/nonexistent/webchat.sqlite")
    assert wts.enable(f) and f.thread_states.ledger is wts.ledger_rows \
        and f.thread_states.model is wts.model_answer
    assert f.thread_states.hosted is wts.hosted_answer and f.thread_states.route is wts.route
    assert not wts.enable(object())
    assert f.thread_states.observer is wts.observe


# ── the owner's tap: a closure label, and the ledger write-back ───────

class FakeLedgerCLI:
    """The CLIs the write-back runs, recorded; each answers as it would."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, script, *args):
        self.calls.append((script, *args))
        if script == "ledger.py":
            return {"ok": True, "_exit": 0,
                    "action": {"close": "closed", "reject": "kept_open"}.get(args[0], "updated")}
        return {"ok": True, "_exit": 0}


def _row(rid, status="open", conv="AAQkconv1=", **kw):
    return {"id": rid, "type": "task", "status": status, "conversation_id": conv, **kw}


def _tap_ctx(store, rows, *, text="Send the signed form to the department.", reply=None):
    """A thread whose text names each row's id, read as the engine reads it."""
    at = time.time() - 3 * 3600
    first = store.add_message("main", mid=None, thread_key=None,
                              author="bot" if reply else "owner", kind="message", text=text,
                              posted_at=at)
    ids = " ".join(f"`{r['id']}`" for r in rows)
    store.add_message("main", mid=None, thread_key=first["thread_key"], author="owner"
                      if reply else "bot", kind="message",
                      text=reply or f"Filed as {ids}.", posted_at=at + 60)
    if reply:
        store.add_message("main", mid=None, thread_key=first["thread_key"], author="bot",
                          kind="message", text=f"Noted {ids}.", posted_at=at + 120)
    states = ts.ThreadStates(lambda: store, ledger=lambda ids_, convs: [
        {k: r[k] for k in ("id", "type", "status", "conversation_id")} for r in rows])
    return states.contexts([("main", first["thread_key"])])[0]


@pytest.fixture
def writeback(store, monkeypatch, tmp_path):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "priv"))
    cli = FakeLedgerCLI()
    monkeypatch.setattr(wts, "RUN", cli)
    entries: list[dict] = []
    monkeypatch.setattr(wts, "_entries", lambda ids, db_path=None: [
        e for e in entries if e["id"] in set(ids)])

    def tap(rows, state, **kw):
        entries[:] = rows
        cli.calls.clear()
        ctx = _tap_ctx(store, rows, **kw)
        wts.observe("owner", ctx, now=time.time(), store=store,
                    row={"state": state, "at": time.time()},
                    before={"state": "needs_you", "by": "model"})
        return cli.calls
    return tap


def _labels():
    import closure_labels as cl
    return [r for r in cl.load() if r["source"] == "owner-tap"]


def test_mark_done_closes_the_open_row_on_the_owner_s_word(writeback):
    calls = writeback([_row("task-Ab1=")], "done")
    assert calls == [
        ("ledger.py", "close", "--id", "task-Ab1=", "--user-said", wts.WEB_DONE_PHRASE,
         "--note", wts.WEB_DONE_NOTE, "--skip-archive"),
        ("log_missed_closure.py", "--ledger-id", "task-Ab1=", "--user-message",
         wts.WEB_DONE_PHRASE, "--source", "web-chat", "--quiet")]
    # no mail moves: the close skips the archive, and nothing else is run
    assert not any("archive-message" in c for call in calls for c in call)
    (rec,) = _labels()
    assert (rec["label"], rec["features"]["writeback"]) == ("done", "close")
    assert rec["features"]["linked_ledger_status"] == "open"


def test_mark_done_writes_nothing_back_when_it_is_not_safe(writeback):
    # a briefing names rows in passing
    assert writeback([_row("task-Ab1=")], "done", text="**Morning briefing**, Mon",
                     reply="thanks") == []
    # two open rows on two mail threads
    assert writeback([_row("task-Ab1="), _row("task-Cd2=", conv="AAQkconv2=")], "done") == []
    # more rows than the cap, even on one thread
    assert writeback([_row(f"task-X{i}=") for i in range(4)], "done") == []
    # nothing open, a plugin's row, or a waiting tap
    assert writeback([_row("task-Ab1=", status="completed")], "done") == []
    assert writeback([_row("watch-Ef3=", source="plugin:docket-watcher")], "done") == []
    assert writeback([_row("task-Ab1=")], "waiting") == []
    assert [r["features"]["writeback"] for r in _labels()] == \
        ["automated", "ambiguous", "ambiguous", "none", "none", "none"]
    # two open rows on one thread close together
    calls = writeback([_row("task-Ab1="), _row("response-Gh4=")], "done")
    assert [c[:4] for c in calls if c[0] == "ledger.py"] == [
        ("ledger.py", "close", "--id", "task-Ab1="), ("ledger.py", "close", "--id", "response-Gh4=")]


def test_needs_me_rejects_a_proposal_and_reopens_a_recent_auto_close(writeback):
    def ago(days):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * DAY))
    rows = [
        _row("deadline-Ac1=", close_proposal={"signal": "llm:reconcile"}),
        _row("response-Aa1=", status="completed", closed_at=ago(2),
             closed_by={"signal": "user_archived"}),
        _row("task-Ab1=", status="completed", closed_at=ago(1),
             closed_by={"signal": "user_stated"}, last_action=wts.WEB_DONE_NOTE),
        # left alone: closed too long ago, by the owner's own word, or folded
        _row("response-Old=", status="completed", closed_at=ago(10),
             closed_by={"signal": "user_archived"}),
        _row("task-Mine=", status="completed", closed_at=ago(1),
             closed_by={"signal": "user_stated"}, last_action="done, per user"),
        _row("task-Dup=", status="completed", closed_at=ago(1),
             closed_by={"signal": "duplicate_of"}),
        _row("task-Open=")]
    calls = writeback(rows, "needs_you")
    note = next(c[-1] for c in calls if c[:2] == ("ledger.py", "update"))
    assert "reopened" in note and "web chat" in note
    assert [c for c in calls if c[0] == "ledger.py"] == [
        ("ledger.py", "reject", "--id", "deadline-Ac1=", "--actor", "web-chat"),
        ("ledger.py", "update", "--id", "response-Aa1=", "--status", "open", "--last-action",
         note),
        ("ledger.py", "update", "--id", "task-Ab1=", "--status", "open", "--last-action", note)]
    assert [c for c in calls if c[0] == "item_feedback.py"] == [
        ("item_feedback.py", "--id", rid, "--kind", "still_open", "--source", "web-chat")
        for rid in ("deadline-Ac1=", "response-Aa1=", "task-Ab1=")]
    assert _labels()[-1]["features"]["writeback"] == "reject_reopen"
    # one more target than the cap: nothing
    more = rows[:3] + [_row("response-Bb2=", status="completed", closed_at=ago(1),
                            closed_by={"signal": "linked_send"})]
    assert writeback(more, "needs_you") == []
    assert _labels()[-1]["features"]["writeback"] == "ambiguous"


def test_the_route_hands_the_tap_to_the_observer(tmp_path, monkeypatch):
    f = WebFrontend.from_config({}, store_path=tmp_path / "priv" / "webchat.sqlite")
    f.store.upsert_channel("main", "ea", "main")
    msg = f.store.add_message("main", mid=None, thread_key=None, author="owner",
                              kind="message", text="Send the form. `task-Ab1=`")
    states = f.enable_thread_state(ledger=lambda ids, convs: [_row("task-Ab1=")])
    heard: list = []
    states.observer = lambda event, ctx, **info: heard.append((event, ctx, info))
    started: list = []
    real = states.owner_tap
    monkeypatch.setattr(states, "owner_tap", lambda *a: started.append(real(*a)) or started[-1])
    f.web_set_state("main", msg["thread_key"], "done")
    started[0].join(5)
    ((event, ctx, info),) = heard
    assert event == "owner" and (ctx["channel"], ctx["key"]) == ("main", msg["thread_key"])
    assert [r["id"] for r in ctx["ledger"]] == ["task-Ab1="]
    assert info["row"]["state"] == "done" and info["row"]["by"] == "owner"
    assert info["before"] == {} and info["store"] is f.store and info["now"] > 0
    # with no observer the route starts nothing
    states.observer = None
    assert real("main", msg["thread_key"], info["row"], {}) is None


def test_a_model_answer_becomes_a_label(store, monkeypatch, tmp_path):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "priv"))
    key = chat_thread(store)
    e = engine(store, FakeModel('{"state": "done", "reason": "Done.", "confidence": 0.45}'))
    heard: list = []
    e.observer = lambda event, ctx, **info: (heard.append((event, info)),
                                             wts.observe(event, ctx, **info))
    e.tick()
    ((event, info),) = heard
    assert event == "model" and info["answer"]["confidence"] == 0.45
    assert info["row"]["state"] == "none"   # below the bar: the state stays as it was
    import closure_labels as cl
    (rec,) = [r for r in cl.load() if r["source"] == "model"]
    assert (rec["kind"], rec["label"], rec["features"]["confidence"],
            rec["features"]["applied_state"], rec["features"]["model_via"]) == \
        ("unsure", "done", 0.45, "none", "local")
    assert rec["thread_ref"] == cl.thread_ref("main", key)
    # a failing observer changes no state and stops nothing
    e.observer = lambda *a, **k: 1 / 0
    store.add_message("main", mid=None, thread_key=key, author="bot", kind="message",
                      text="More.", posted_at=time.time() - 3 * 3600)
    assert e.tick()["model_calls"] == 1


def test_the_write_back_through_the_real_ledger_cli(tmp_path, monkeypatch):
    """ledger.py and item_feedback.py run for real on a scratch ledger; the
    missed-closure log (which fetches mail evidence) is recorded instead."""
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    (ea / "logs").mkdir()
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(ledger, "_is_owner", lambda: False)
    db = _ledger_db(ea / "state" / "obligations-ledger.db")
    logged = []

    def run(script, *args):
        if script == "log_missed_closure.py":
            logged.append(args)
            return {"_exit": 0}
        return wts._cli(script, *args)
    monkeypatch.setattr(wts, "RUN", run)
    rows = wts._entries(["response-Ab1="], db_path=db)
    now = time.time()
    plan = wts.writeback_plan({"ledger": rows, "messages": [m(1, "owner", "x")],
                               "channel_row": {"kind": "main"}}, "done", now=now, entries=rows)
    assert plan == {"action": "close", "ops": [("close", "response-Ab1=")]}
    assert wts.run_writeback(plan) == [{"op": "close", "id": "response-Ab1=", "ok": True}]
    (row,) = wts._entries(["response-Ab1="], db_path=db)
    assert row["status"] == "completed" and row["closed_by"]["signal"] == "user_stated"
    assert row["last_action"] == wts.WEB_DONE_NOTE and not row.get("email_archived")
    assert logged == [("--ledger-id", "response-Ab1=", "--user-message", wts.WEB_DONE_PHRASE,
                       "--source", "web-chat", "--quiet")]
    # the owner changes their mind: the row the tap closed opens again
    plan = wts.writeback_plan({"ledger": [row], "messages": [m(1, "owner", "x")],
                               "channel_row": {"kind": "main"}}, "needs_you", now=time.time(),
                              entries=[row])
    assert plan["ops"] == [("reopen", "response-Ab1=")]
    assert all(r["ok"] for r in wts.run_writeback(plan))
    (row,) = wts._entries(["response-Ab1="], db_path=db)
    assert row["status"] == "open" and row.get("confirmed_open_at")
    feedback = (ea / "logs" / "item-feedback.jsonl").read_text()
    assert '"kind": "still_open"' in feedback and '"source": "web-chat"' in feedback
