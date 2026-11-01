"""The state of a conversation in the web chat (docs/webchat-state.md):
the rules in their order, the owner's override, the model step with a
fake model, the engine's change pass and tick over a scratch store, the
EA's ledger read and model route, and the routing table's new task.
Every name, address and id here is made up; nothing reaches a network."""
from __future__ import annotations

import json
import sqlite3
import sys
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
     [m(1, kind="card", card=live_card(), at=NOW - 8 * DAY)], {}, None),
    ("a card with no buttons is not live",
     [m(1, kind="card", card={"title": "A note", "actions": [], "select": None})], {}, None),
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
      m(2, text="Should I write a shorter one?", at=NOW - 300)], {}, None),
    ("a card that failed is not done",
     [m(1, kind="status", text=":x: Send failed: the draft is gone", updated=NOW - 600)], {}, None),
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
     [m(1, text="Fill the form.")],
     {"ledger": [{"id": "task-a1", "type": "task", "status": "open"}]}, None),
    ("an open row of the waiting type is the model's",
     [m(1, text="Asked for the slides.")],
     {"ledger": [{"id": "waiting-a1", "type": "waiting", "status": "open"}]}, None),
    ("an open deadline row is the model's",
     [m(1, text="Report due.")],
     {"ledger": [{"id": "deadline-a1", "type": "deadline", "status": "open"}]}, None),
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
    ("a live card beats the hourglass",
     [m(1, kind="card", card=live_card()), m(2, "owner", "Wait.", reactions=["eyes"])], {},
     ("needs_you", "rule")),
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
     [m(1, text="x")],
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
    card = store.add_message("dm", mid=None, thread_key=None, author="bot", kind="status",
                             text=":hourglass_flowing_sand: Sending...", card=live_card(),
                             posted_at=clock[0] - 60)
    model = FakeModel()
    e = engine(store, model, clock=lambda: clock[0])
    e.tick()
    assert store.thread_state("dm", card["thread_key"])["state"] == "working"
    assert model.prompts == []
    clock[0] += 30 * MIN  # still "Sending..." half an hour on: the rule no longer holds
    e.tick()
    assert len(model.prompts) == 1
    assert store.thread_state("dm", card["thread_key"])["by"] == "model"


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
    ("a legend past the registry's age acts no more", [zcard(at=NOW - 8 * DAY)], None),
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
     [zcard(":cross_mark: Could not authorize this card: expired")], None),
    ("a warning is not settled", [zcard(":warning: This send confirmation is stale")], None),
    ("a refusal is not settled", [zcard(":no_entry: The draft is gone.")], None),
    ("a press under way that Zulip edited a moment ago",
     [zcard(":time_ticking: Sending...",
            primary={"text": ZHEAD + ZRULE + ":time_ticking: Sending...", "filed": False,
                     "edited_at": NOW - MIN})],
     ("working", "Tacitus is acting on the button you pressed.")),
    ("a press under way with no time but the copy's", [zcard(":time_ticking: Sending...")],
     None),
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
    # a live card beats the owner's unanswered message (its button is the next step)
    asked = ASK + [m(4, "owner", "Make it shorter.", at=NOW - 30 * MIN)]
    assert decide(asked, linked=[dm_card()])[0] == "needs_you"
    assert decide(asked)[2] == ts.NOT_ANSWERED
    # with no link the conversation is the model's, as before
    assert decide(ASK) is None and decide(ASK, linked=[]) is None


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


# ── the stale shelf ───────────────────────────────────────────────────

def test_a_state_a_week_old_goes_on_the_stale_shelf_once(store):
    keys = [chat_thread(store, f"post {i}") for i in range(4)]
    for key, state in zip(keys, ("needs_you", "waiting", "done", "needs_you")):
        store.set_thread_state("main", key, state, "model", "x", judged_seq=store.newest_seq("main", key))
    clock = [time.time()]
    e = engine(store, clock=lambda: clock[0])
    assert e.shelve() == 0
    store._conn.execute("UPDATE thread_state SET at=? WHERE thread_key IN (?,?,?)",
                        (clock[0] - ts.STALE_S - 60, *keys[:3]))
    store._conn.commit()
    start = store.max_event_seq()
    assert e.tick()["stale"] == 2                   # needs_you and waiting; done is never stale
    events = [json.loads(p) for _s, t, p in store.events_since(start) if t == "thread_state"]
    assert sorted(ev["thread_key"] for ev in events) == sorted(keys[:2])
    assert all(ev["stale"] is True for ev in events)
    assert e.tick()["stale"] == 0                   # once
    assert [k for k in keys if store.thread_state("main", k)["stale"]] == keys[:2]
    rows = {r["thread_key"]: r for r in store.threads("main")}
    assert [rows[k]["stale"] for k in keys] == [True, True, False, False]
    assert {r["thread_key"]: r["stale"] for r in store.needs()} == \
        {keys[0]: True, keys[1]: True, keys[3]: False}
    # a new state starts fresh
    store.set_thread_state("main", keys[0], "waiting", "model", "y",
                           judged_seq=store.newest_seq("main", keys[0]))
    assert store.thread_state("main", keys[0])["stale"] == 0
    assert store.thread_row("main", keys[0])["stale"] is False


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
    key = chat_thread(store, at=time.time() - 30 * 3600)
    _needs(store, key, 25)
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
    cases["young"] = chat_thread(store, "young", at=t0)
    _needs(store, cases["young"], 20)
    cases["stale"] = chat_thread(store, "stale", at=t0 - 10 * DAY)
    _needs(store, cases["stale"], 8 * 24)
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
    keys = [chat_thread(store, f"post {i}", at=time.time() - 3 * DAY) for i in range(13)]
    for i, key in enumerate(keys):
        _needs(store, key, 30 + i)
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


def test_the_model_routes_local_and_never_calls_a_hosted_model(routing, monkeypatch):
    calls = []

    def fake_chat(messages, **kw):
        calls.append(kw)
        return {"content": GOOD}
    monkeypatch.setattr(rl, "chat", fake_chat)
    routing("local", LOCAL)
    assert wts.model_answer("prompt") == GOOD
    assert calls[0]["purpose"] == "chat-thread-state" and calls[0]["schema"] == ts.ANSWER_SCHEMA
    routing("auto", LOCAL)
    assert wts.route() == "local"
    for value, cfg in (("local", rl._off()), ("hosted", LOCAL), ("auto", rl._off())):
        routing(value, cfg)
        assert wts.model_answer("prompt") is None, value
    assert len(calls) == 1

    def boom(messages, **kw):
        raise rl.RemoteLLMError("circuit open after repeated failures")
    monkeypatch.setattr(rl, "chat", boom)
    routing("local", LOCAL)
    assert wts.model_answer("prompt") is None


def test_the_routing_table_names_the_task_and_never_warns_for_its_default(monkeypatch):
    block = ea_config.routing_from_block(None)
    assert block["tasks"]["thread_state"] == "local"
    monkeypatch.setattr(ea_config, "get_routing", lambda *a, **k: block)
    monkeypatch.setattr(ea_config, "get_routing_source", lambda *a, **k: (block, "default"))
    off = rl._off()
    row = {r["task"]: r for r in mr.rows(cfg=off, tiers=ea_config.tiers_from_block({}))}["thread_state"]
    assert row["effective"] == "hosted" and "rules alone" in row["why"]
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
    assert not wts.enable(object())
