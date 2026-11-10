"""The Replies card (ea-chat-bot.py): the replies that wait, on one card
in the owner's DM, once a day.

Pinned here:
  1. where a reply stands: a live Send card, a draft whose card closed, a
     question for the owner, a meeting to place, no draft.
  2. the tick: once a local day, from the card hour, outside quiet hours;
     the day before's cards give way; no card when nothing needs one.
  3. the clicks: One by one, Draft it (a turn, or a new Send card), Skip
     (a snooze), Not needed (the draft goes, the row closes).
  4. the card names its rows as `row` and `rows`, so the web chat's Mark
     done on its thread closes nothing.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))

import ledger as ledger_mod  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
DAY = "2026-10-12"


def _q(rid: str, age: int, **over) -> dict:
    """A queue entry as `ledger.py reply-queue` prints it."""
    row = {"id": f"response-{rid}", "description": f"Reply to {rid.title()}", "age_days": age,
           "from": f"{rid}@example.edu", "from_name": rid.title(), "ask": "Confirm the date.",
           "since": "2026-10-05T16:00:00Z", "scheduling": "none", "type": "response_needed",
           "queued": {}, "rung": "ask" if age >= 5 else "name"}
    row.update(over)
    return row


class _SyncThread:
    """threading.Thread that runs its target at start()."""
    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _bot(queue: list | None, *, confirms: dict | None = None, journal: list | None = None,
         clock=(DAY, 9, False)):
    """The Replies section of the bot on stand-ins. Returns (ns, calls)."""
    start = BOT_SRC.index("_REPLY_CARDS = _private_state(")
    end = BOT_SRC.index("def _toggle_quoted(")
    from preprocessor import chat as _chat
    calls: dict = {"cards": [], "threads": [], "status": [], "cli": [], "turns": [], "posts": [],
                   "send_cards": [], "deleted": [], "marked": []}
    confirms = confirms if confirms is not None else {}
    store: dict = {}
    retired: set = set()

    class App:
        def action(self, _name):
            return lambda fn: fn

    def post_card(ch, card, thread_ts=None):
        calls["cards"].append(card)
        calls["threads"].append(thread_ts)
        mid = f"m{len(calls['cards'])}"
        return types.SimpleNamespace(channel=ch, id=mid, thread_key=thread_ts or mid)

    class Cards:
        def by_ref(self, ch, mid):
            known = {f"m{i + 1}" for i in range(len(calls["cards"]))}
            return {"status": "retired" if mid in retired else "live"} if mid in known else None

    def replace(ch, ts, text):
        calls["status"].append((ts, text))
        retired.add(ts)

    def ledger_cli(*argv):
        calls["cli"].append(argv)
        if argv[0] == "reply-queue":
            return {"entries": queue} if queue is not None else {}
        if argv[0] == "update":
            return {"entry": {"id": argv[2], "status": "snoozed"}}
        if argv[0] == "close":
            return {"ok": True, "action": "closed"}
        return {}

    def state_of(draft_id):
        return (confirms.get(draft_id) or {}).get("status", "")

    def post_send(channel, thread_ts, draft_id, email_system="outlook", gmail_email="", what=""):
        calls["send_cards"].append((channel, thread_ts, draft_id, email_system))
        confirms[draft_id] = dict(confirms.get(draft_id) or {}, status="live")

    class Frontend:
        def post(self, channel="", thread_ts=None, text=""):
            calls["posts"].append((channel, thread_ts, text))
            return types.SimpleNamespace(id=f"p{len(calls['posts'])}")

    import threading as _threading
    ns = {"json": json, "time": __import__("time"), "datetime": datetime, "timedelta": timedelta,
          "uuid": __import__("uuid"), "_chat": _chat, "app": App(), "_CARDS": Cards(),
          "threading": types.SimpleNamespace(Thread=_SyncThread),
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(d=d)),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                       debug=lambda *a: None),
          "CLOSE_PROPOSAL_CHECK_S": 300,
          "_verify_button_user": lambda ev: True,
          "_resolve_dm_channel": lambda: "dm",
          "_post_card": post_card, "_replace_buttons_with_status": replace,
          "_ledger_cli": ledger_cli,
          "_draft_events": types.SimpleNamespace(
              read_since=lambda _n: list(journal or []),
              parse_ledger_ids=lambda ids: [str(x) for x in ids or ()],
              system_of=lambda events, did: ("outlook", "")),
          "_draft_confirmation_state": state_of,
          "_load_draft_confirms": lambda: confirms,
          "_mark_draft_confirmation": lambda did, st: calls["marked"].append((did, st)),
          "_handled_delete_draft": lambda did: (calls["deleted"].append(did), (0, ""))[1],
          "_build_send_confirmation": lambda ch, th, did, system="outlook", acct="":
              (object(), None) if not did.startswith("gone") else (None, "not found"),
          "_post_send_confirmation": post_send,
          "_GMAIL_ACCOUNT": "", "FRONTEND": Frontend(),
          "handle_message_async": lambda *a: calls["turns"].append(a),
          "_active_threads": set(), "_active_lock": _threading.Lock(),
          "_item_context": lambda rid: f"Ledger item {rid}: made up"}
    exec(compile(BOT_SRC[start:end], "bot[replies]", "exec"), ns)
    ns["_load_reply_cards"] = lambda: store.get("d", {})
    ns["_reply_clock"] = lambda: clock
    return ns, calls


def _ev(value, card_id="m1", thread=None):
    return types.SimpleNamespace(value=value, user_id="U1",
                                 card=types.SimpleNamespace(channel="dm", id=card_id,
                                                            thread_key=thread or card_id))


def _text(card) -> str:
    """The card's words as the owner reads them."""
    from botcore.chat import card_text
    return card_text.strip_inert("\n".join([card.title] + list(card.lines)))


def _tick(ns, **kw):
    ns["_LAST_REPLY_CHECK"][0] = 0.0
    return ns["_post_reply_queue"](**kw)


# ── where a reply stands ──────────────────────────────────────────────

def test_where_a_reply_stands():
    confirms = {"d-live": {"status": "live"}, "d-old": {"status": "retired"},
                "d-sent": {"status": "consumed"}}
    ns, _ = _bot([], confirms=confirms)
    state = ns["_reply_state"]
    drafted = lambda did: {"outcome": "drafted", "draft_id": did}      # noqa: E731
    assert state(_q("a", 4, queued=drafted("d-live")), {}) == ("card", "d-live")
    assert state(_q("b", 4, queued=drafted("d-old")), {}) == ("draft", "d-old")
    assert state(_q("c", 4, queued=drafted("d-unknown")), {}) == ("draft", "d-unknown")
    assert state(_q("d", 4, queued=drafted("d-sent")), {}) == ("sent", "d-sent")
    assert state(_q("e", 4, queued={"outcome": "decision", "note": "One year or three?"}), {}) \
        == ("decision", "")
    assert state(_q("f", 4, scheduling="request"), {}) == ("meeting", "")
    assert state(_q("g", 4, state="scheduling"), {}) == ("meeting", "")
    assert state(_q("h", 4), {}) == ("none", "")


def test_a_draft_the_journal_links_counts_when_it_is_no_older_than_the_message():
    journal = [
        {"ts": "2026-10-06T15:00:00Z", "action": "created", "draft_id": "d-new",
         "ledger_ids": ["response-pat"]},
        {"ts": "2026-10-01T15:00:00Z", "action": "created", "draft_id": "d-stale",
         "ledger_ids": ["response-lee"]},
        {"ts": "2026-10-06T15:00:00Z", "action": "created", "draft_id": "d-cut",
         "ledger_ids": ["response-sam"]},
        {"ts": "2026-10-07T15:00:00Z", "action": "deleted", "draft_id": "d-cut"}]
    ns, _ = _bot([], journal=journal)
    linked = ns["_reply_linked_drafts"]()
    assert linked == {"response-pat": ("d-new", "2026-10-06T15:00:00Z"),
                      "response-lee": ("d-stale", "2026-10-01T15:00:00Z")}
    assert ns["_reply_state"](_q("pat", 4), linked) == ("draft", "d-new")
    assert ns["_reply_state"](_q("lee", 4), linked) == ("none", "")       # an older message's draft
    assert ns["_reply_state"](_q("sam", 4), linked) == ("none", "")       # deleted


# ── the tick ──────────────────────────────────────────────────────────

def test_the_card_lists_the_threads_that_wait_oldest_first():
    queue = [_q("pat", 12, queued={"outcome": "drafted", "draft_id": "d-old"}),
             _q("lee", 8, queued={"outcome": "decision", "note": "One year or three?"}),
             _q("sam", 5, scheduling="request"), _q("kim", 3),
             _q("ready", 4, queued={"outcome": "drafted", "draft_id": "d-live"})]
    ns, calls = _bot(queue, confirms={"d-live": {"status": "live"}})
    assert _tick(ns) == 4
    assert calls["cli"][0] == ("reply-queue", "--min-age", "3")
    (card,) = calls["cards"]
    assert card.kind == "replies" and calls["threads"] == [None]
    assert "*4 replies wait for you.* The oldest has waited 12 days." in card.title
    assert "1 more has a draft ready: its Send card is in this DM." in card.title
    assert [line.split(" (")[1] for line in card.lines] == [
        "12 days, its Send card closed)", "8 days, needs your answer)",
        "5 days, a meeting to place)", "3 days, no draft yet)"]
    (action,) = card.actions
    assert (action.id, action.word, action.style) == ("review_replies", "one by one", "primary")
    assert action.value["rows"] == ["response-pat", "response-lee", "response-sam", "response-kim"]


def test_the_card_names_at_most_ten_and_counts_the_rest():
    ns, calls = _bot([_q(f"p{n}", 30 - n) for n in range(13)])
    assert _tick(ns) == 13
    (card,) = calls["cards"]
    assert len(card.lines) == 11 and card.lines[-1] == "And 3 more after these."
    assert len(card.actions[0].value["rows"]) == 10


def test_one_thread_is_its_own_card():
    ns, calls = _bot([_q("pat", 4)])
    assert _tick(ns) == 1
    (card,) = calls["cards"]
    assert [a.id for a in card.actions] == ["reply_draft", "reply_skip", "reply_not_needed"]
    assert _text(card) == (":inbox_tray: *Reply to Pat*\nFrom Pat, 4 days ago.\n"
                           "Confirm the date.\nNo draft yet.")


def test_the_card_comes_once_a_day_from_its_hour_and_not_in_quiet_hours():
    queue = [_q("pat", 4), _q("lee", 3)]
    ns, calls = _bot(queue, clock=(DAY, 7, False))
    assert _tick(ns) == 0 and not calls["cli"]                    # before the hour
    ns["_reply_clock"] = lambda: (DAY, 9, True)
    assert _tick(ns) == 0 and not calls["cli"]                    # quiet hours
    ns["_reply_clock"] = lambda: (DAY, 9, False)
    assert _tick(ns) == 2 and len(calls["cards"]) == 1
    assert _tick(ns) == 0 and len(calls["cards"]) == 1            # the same day: no second card
    assert ns["_post_reply_queue"]() == 0                         # inside the check interval


def test_the_next_day_retires_the_cards_of_the_day_before():
    queue = [_q("pat", 4), _q("lee", 3)]
    ns, calls = _bot(queue)
    _tick(ns)
    ns["handle_review_replies"](_ev(calls["cards"][0].actions[0].value))
    assert len(calls["cards"]) == 3 and calls["threads"][1:] == ["m1", "m1"]
    ns["handle_reply_skip"](_ev(calls["cards"][1].actions[1].value, "m2", "m1"))
    calls["status"].clear()
    ns["_reply_clock"] = lambda: ("2026-10-13", 9, False)
    queue[:] = [_q("lee", 4), _q("kim", 3)]
    assert _tick(ns) == 2
    # The answered cards (m1, m2) stand as they are; the unanswered one gives way.
    assert [ts for ts, _ in calls["status"]] == ["m3"]
    assert calls["status"][0][1].startswith(":zzz:")
    assert len(calls["cards"]) == 4


def test_no_card_when_nothing_needs_one_and_a_silent_ledger_is_asked_again():
    live = {"d-live": {"status": "live"}}
    ns, calls = _bot([_q("ready", 4, queued={"outcome": "drafted", "draft_id": "d-live"})],
                     confirms=live)
    assert _tick(ns) == 0 and not calls["cards"]
    assert _tick(ns) == 0 and len(calls["cli"]) == 1              # the day is done
    ns, calls = _bot(None)
    assert _tick(ns) == 0 and _tick(ns) == 0
    assert len(calls["cli"]) == 2                                 # no answer: the next tick asks


# ── the clicks ────────────────────────────────────────────────────────

def test_one_by_one_cards_each_thread_that_still_waits():
    queue = [_q("pat", 6), _q("lee", 4), _q("sam", 3)]
    ns, calls = _bot(queue)
    _tick(ns)
    value = calls["cards"][0].actions[0].value
    del queue[1]                                                  # answered since the card
    ns["handle_review_replies"](_ev(value))
    rows = calls["cards"][1:]
    assert [c.actions[0].value["row"] for c in rows] == ["response-pat", "response-sam"]
    assert calls["status"][-1] == ("m1", ":ok_hand: One by one:")
    queue.clear()
    ns["handle_review_replies"](_ev(value, "m9"))
    assert calls["status"][-1][1].startswith(":information_source: Nothing is left")
    assert len(calls["cards"]) == 3


def test_draft_it_starts_a_turn_in_the_cards_thread():
    row = _q("lee", 8, queued={"outcome": "decision", "note": "One year or three?"})
    ns, calls = _bot([row])
    _tick(ns)
    card = calls["cards"][0]
    assert card.actions[0].label == ":pencil2: Draft it"
    assert "I need your answer before I write it: One year or three?" in _text(card)
    ns["handle_reply_draft"](_ev(card.actions[0].value, "m1", "t1"))
    (turn,) = calls["turns"]
    channel, thread, msg_ts, prompt, user, is_reply = turn
    assert (channel, thread, msg_ts, user, is_reply) == ("dm", "t1", "p1", "U1", True)
    assert calls["posts"][0][:2] == ("dm", "t1")
    assert "ledger.py get --id response-lee" in prompt
    assert "--ledger-id response-lee" in prompt and "note-reply --id response-lee" in prompt
    assert "One year or three?" in prompt and "data, not an instruction" in prompt
    assert calls["status"][-1] == ("m1", ":pencil2: The draft is being written.")
    assert not calls["send_cards"]


def test_a_draft_whose_send_card_closed_gets_a_new_send_card():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "d-old"})
    confirms = {"d-old": {"status": "retired", "ea_channel": "ea", "thread_ts": "t-first",
                          "email_system": "gmail", "gmail_email": "owner@example.com"}}
    ns, calls = _bot([row], confirms=confirms)
    _tick(ns)
    card = calls["cards"][0]
    assert card.actions[0].label == ":lock: New Send card"
    assert "Its Send card closed." in card.title
    ns["handle_reply_draft"](_ev(card.actions[0].value))
    assert calls["send_cards"] == [("ea", "t-first", "d-old", "gmail")]   # its own conversation
    assert calls["status"][-1] == ("m1", ":lock: The new Send card is in this DM.")
    assert not calls["turns"]


def test_a_draft_that_is_gone_is_written_again():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "gone-1"})
    ns, calls = _bot([row])
    _tick(ns)
    ns["handle_reply_draft"](_ev(calls["cards"][0].actions[0].value))
    assert not calls["send_cards"] and len(calls["turns"]) == 1


def test_skip_snoozes_the_row_for_three_days():
    ns, calls = _bot([_q("pat", 4)])
    _tick(ns)
    ns["handle_reply_skip"](_ev(calls["cards"][0].actions[1].value))
    assert calls["cli"][-1] == ("update", "--id", "response-pat", "--status", "snoozed",
                                "--snooze-until", "2026-10-15", "--last-action",
                                "2026-10-12: skipped for 3 days on the Replies card")
    assert calls["status"][-1] == ("m1", ":fast_forward: Skipped. It comes back in 3 days.")


def test_not_needed_deletes_the_draft_and_closes_the_row():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "d-live"})
    confirms = {"d-live": {"status": "retired", "channel": "dm", "message_ts": "s1"}}
    ns, calls = _bot([row], confirms=confirms)
    _tick(ns)
    confirms["d-live"]["status"] = "live"                         # a Send card came up since
    ns["handle_reply_not_needed"](_ev(calls["cards"][0].actions[2].value))
    assert calls["deleted"] == ["d-live"] and calls["marked"] == [("d-live", "retired")]
    assert calls["cli"][-1] == ("close", "--id", "response-pat", "--signal", "user_handled",
                                "--note", "No reply needed: the owner's click on the Replies card")
    assert [text for _ts, text in calls["status"][-3:]] == [
        ":hourglass_flowing_sand: Closing it...",
        ":white_check_mark: Not needed. Draft deleted.",
        ":white_check_mark: Closed. No reply goes out. Draft deleted."]


def test_not_needed_changes_nothing_while_the_draft_is_being_sent():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "d-old"})
    ns, calls = _bot([row])
    ns["_handled_delete_draft"] = lambda did: (3, "sending")
    _tick(ns)
    ns["handle_reply_not_needed"](_ev(calls["cards"][0].actions[2].value))
    assert not [a for a in calls["cli"] if a[0] == "close"]
    assert calls["status"][-1][1].startswith(":warning: Its draft is being sent now")


def test_a_click_on_a_thread_that_no_longer_waits_does_nothing():
    """A row that was answered, closed or skipped is out of the queue: a
    Skip would otherwise reopen a closed row as snoozed."""
    queue = [_q("pat", 4)]
    ns, calls = _bot(queue)
    _tick(ns)
    actions = calls["cards"][0].actions
    queue.clear()
    for handler, action in (("handle_reply_draft", actions[0]), ("handle_reply_skip", actions[1]),
                            ("handle_reply_not_needed", actions[2])):
        ns[handler](_ev(action.value))
        assert calls["status"][-1][1].startswith(":information_source: This thread changed")
    assert {a[0] for a in calls["cli"]} == {"reply-queue"}
    assert not calls["turns"] and not calls["deleted"]


# ── what the web chat reads from the card ─────────────────────────────

def test_mark_done_on_the_replies_thread_closes_nothing():
    """The web chat closes the rows a thread's cards name as `ledger_id`,
    `ledger_ids` or `conversation_id`, and the id-shaped words of its
    text. The Replies cards carry neither."""
    from botcore.chat import thread_state
    queue = [_q("pat", 6, queued={"outcome": "drafted", "draft_id": "d-old"}), _q("lee", 4)]
    ns, calls = _bot(queue)
    _tick(ns)
    ns["handle_review_replies"](_ev(calls["cards"][0].actions[0].value))
    assert len(calls["cards"]) == 3
    messages = [{"text": _text(c),
                 "card": {"actions": [{"id": a.id, "value": a.value} for a in c.actions]}}
                for c in calls["cards"]]
    assert thread_state.link_keys(messages) == ([], [])
    for card in calls["cards"]:
        for a in card.actions:
            assert not {"ledger_id", "ledger_ids", "conversation_id"} & set(a.value)


def test_the_cards_age_is_the_ladders_third_day_and_the_tick_is_wired():
    ns, _ = _bot([])
    assert ns["REPLY_CARD_MIN_AGE"] == ledger_mod.REPLY_NAME_DAYS
    start = BOT_SRC.index("def resident_router_loop")
    body = BOT_SRC[start:BOT_SRC.index("\ndef ", start + 1)]
    gate = body.index("if not (_resident.enabled() and _resident.pane_alive())")
    assert 0 < body.index("_post_reply_queue()") < gate       # an install with no resident gets it
    from botcore.chat import slack_frontend
    assert "replies" in slack_frontend.RENDERERS
