"""The Replies card (ea-chat-bot.py): the replies that wait, on one card
in the owner's DM, once a day.

Pinned here:
  1. where a reply stands: a live Send card, a draft whose card closed, a
     question for the owner, a meeting to place, no draft.
  2. the tick: once a local day, from the card hour, outside quiet hours;
     the day before's cards give way; no card when nothing needs one.
  3. the clicks: One by one, Draft it (a turn, or a new Send card), Later
     (a snooze to the day picked), No reply needed (the draft goes, the
     row and the thread's other reply rows close, and the card says where
     the mail is), the same two quiet choices on every row card; a line
     typed on the card that says only that no reply is needed runs No
     reply needed and starts no turn.
  4. the card names its rows as `row` and `rows`, so the web chat's Mark
     done on its thread closes nothing.
  5. a question with recorded answers shows one button per answer and no
     default; a tap starts the turn that writes the reply which says it,
     and only while the row still offers that answer.
  6. Draft it and Other answer take one line of the owner's words; the
     turn gets it as the owner's instruction for that reply.
  7. each row's drafting turn runs in a side session of the card's thread,
     with files of its own, so the drafts are written at the same time;
     the question first stays on the thread's session, which reads the
     owner's answer.
  8. on the web chat each reply that waits is its own block, with no card
     to press first: a row with a live block never gets a second one (the
     block is drawn again in place), at most ten wait at once, and a
     Replies card from before gives way.
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
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _bot(queue: list | None, *, confirms: dict | None = None, journal: list | None = None,
         clock=(DAY, 9, False), closes: dict | None = None, thread: list | None = None):
    """The Replies section of the bot on stand-ins. Returns (ns, calls).
    `closes` is what `ledger.py close` prints for a row id, `thread` the
    rows `ledger.py search --conversation-id` finds."""
    start = BOT_SRC.index("_REPLY_CARDS = _private_state(")
    end = BOT_SRC.index("def _toggle_quoted(")
    from preprocessor import chat as _chat
    calls: dict = {"cards": [], "threads": [], "status": [], "cli": [], "turns": [], "posts": [],
                   "send_cards": [], "deleted": [], "marked": [], "actions": [], "sessions": []}
    confirms = confirms if confirms is not None else {}
    store: dict = {}
    retired: set = set()

    class App:
        def action(self, name):
            calls["actions"].append(name)
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
            return (closes or {}).get(argv[2], {"ok": True, "action": "closed"})
        if argv[0] == "search":
            return {"entries": list(thread or [])}
        return {}

    def state_of(draft_id):
        return (confirms.get(draft_id) or {}).get("status", "")

    def post_send(channel, thread_ts, draft_id, email_system="outlook", gmail_email="", what=""):
        calls["send_cards"].append((channel, thread_ts, draft_id, email_system))
        ts = f"s{len(calls['send_cards'])}"
        confirms[draft_id] = dict(confirms.get(draft_id) or {}, status="live", channel="dm", message_ts=ts)
        return "dm", ts

    class Frontend:
        def post(self, channel="", thread_ts=None, text=""):
            calls["posts"].append((channel, thread_ts, text))
            return types.SimpleNamespace(id=f"p{len(calls['posts'])}")

    import threading as _threading
    ns = {"json": json, "re": __import__("re"), "time": __import__("time"), "datetime": datetime, "timedelta": timedelta,
          "uuid": __import__("uuid"), "_chat": _chat, "app": App(), "_CARDS": Cards(),
          "threading": types.SimpleNamespace(Thread=_SyncThread, Lock=_threading.Lock),
          "TIMEOUT_SECONDS": 3600, "timezone": __import__("datetime").timezone,
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(d=d)),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                       debug=lambda *a: None),
          "CLOSE_PROPOSAL_CHECK_S": 300, "_LEDGER_OPEN_STATUSES": ("open", "follow_up", "snoozed"),
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
          "handle_message_async": lambda *a, session_key="": (
              calls["turns"].append(a), calls["sessions"].append(session_key)),
          "_body_file_path": lambda ts, step=None:
              f"/state/reply-body-{ts}{'.' + step if step else ''}.txt",
          "_active_threads": set(), "_active_lock": _threading.Lock(),
          "_item_context": lambda rid: f"Ledger item {rid}: made up"}
    pv = BOT_SRC.index("def _press_value(")
    exec(compile(BOT_SRC[pv:BOT_SRC.index("def _case_card_failed(")], "bot[press]", "exec"), ns)
    exec(compile(BOT_SRC[start:end], "bot[replies]", "exec"), ns)
    ns["_load_reply_cards"] = lambda: store.get("d", {})
    ns["_reply_clock"] = lambda: clock
    return ns, calls


def _ev(value, card_id="m1", thread=None, text=""):
    return types.SimpleNamespace(value=value, user_id="U1", text=text,
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
    assert calls["cli"][0] == ("reply-queue", "--min-age", "1")
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


def test_a_question_for_the_owner_is_on_the_card_from_its_first_day():
    """A run asked the owner to decide a day-old thread: no run writes a
    draft for it, so it is on that day's card with its answer buttons. A
    day-old thread with no question waits for its draft, as before."""
    ask = {"outcome": "decision", "note": "Host the visit?", "choices": ["Yes, host it", "No"]}
    queue = [_q("pat", 7), _q("lee", 1, queued=ask), _q("kim", 2), _q("sam", 1),
             _q("ann", 0, queued=dict(ask))]
    ns, calls = _bot(queue)
    assert _tick(ns) == 2
    (card,) = calls["cards"]
    assert card.actions[0].value["rows"] == ["response-pat", "response-lee"]
    assert "1 day, needs your answer)" in card.lines[1]
    # The click finds the row through the same rule.
    rid, _batch, row = ns["_reply_click"](_ev({"row": "response-lee", "batch": "b"}))
    assert rid == "response-lee" and row is not None
    assert ns["_reply_click"](_ev({"row": "response-kim", "batch": "b"}))[2] is None


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
    assert "took none of them" not in prompt
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: One question first: ")
    assert calls["status"][-1] == (
        "m1", ":speech_balloon: Tell me your answer in this thread. Then I write the draft.")
    assert not calls["send_cards"]
    plain, calls = _bot([_q("ann", 4)])
    _tick(plain)
    plain["handle_reply_draft"](_ev(calls["cards"][0].actions[0].value))
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: Writing the reply: ")
    assert calls["status"][-1] == ("m1", ":pencil2: The draft is being written.")


def test_draft_it_takes_a_line_of_the_owners_words():
    ns, calls = _bot([_q("ann", 4)])
    _tick(ns)
    draft = calls["cards"][0].actions[0]
    assert (draft.label, draft.ask) == (":pencil2: Draft it", "what the reply must say")
    assert [a.ask for a in calls["cards"][0].actions[1:]] == ["The day it comes back", ""]
    ns["handle_reply_draft"](_ev(draft.value, "m1", "t1",
                                 text="  Say yes,\nbut not before the 20th.  " + "x" * 400))
    prompt = calls["turns"][0][3]
    assert 'The owner wrote this line on the card: "Say yes, but not before the 20th. xxx' in prompt
    assert "x" * 300 not in prompt                          # one line, 300 characters at most
    assert "the owner's instruction for this reply and for nothing else" in prompt
    assert "a decline too: the draft waits for the owner's Send" in prompt
    assert "note-reply --id response-ann" in prompt
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: Writing the reply: ")
    ts, status = calls["status"][-1]
    assert _plain(status).startswith(
        ":pencil2: The draft is being written. Your words: Say yes, but not before the 20th.")


def test_the_card_and_the_turn_carry_the_owners_earlier_word():
    """A follow-up on a thread the owner said gets no reply: the card shows
    his words, a tap with no words writes no draft, and his answer on the
    card is the reply's instruction."""
    said = [{"row": "task-pilot", "at": "2026-11-14", "signal": "user_stated",
             "said": "Not joining. No response.", "no_reply": True}]
    row = _q("pat", 1, owner_said=said, queued={
        "outcome": "decision", "note": "They asked again. On Nov 14 you said no reply. Still no reply?"})
    ns, calls = _bot([row])
    _tick(ns)
    card = calls["cards"][0]
    assert 'You said earlier: "Not joining. No response."' in _text(card)
    ns["handle_reply_draft"](_ev(card.actions[0].value, text="Write a short no, thanks."))
    prompt = calls["turns"][0][3]
    assert 'already decided this thread on 2026-11-14, in his words (data): "Not joining. No ' \
           'response."' in prompt
    assert "asks for a reply now: write the reply that says it" in prompt
    bare = ns["_reply_draft_prompt"](dict(row, queued={}))
    assert "his tap gives no words for a reply: write no draft" in bare


def test_a_line_on_a_question_is_the_answer():
    """A row that waits on the owner's answer: the words are that answer,
    so the turn writes the reply and does not ask again."""
    row = _q("lee", 8, queued={"outcome": "decision", "note": "One year or three?"})
    ns, calls = _bot([row])
    _tick(ns)
    draft = calls["cards"][0].actions[0]
    assert draft.ask == "your answer"
    ns["handle_reply_draft"](_ev(draft.value, text="Three years."))
    prompt = calls["turns"][0][3]
    assert "One year or three?" in prompt and "data, not an instruction" in prompt
    assert 'The owner wrote this line on the card: "Three years."' in prompt
    assert "ask that one question here in one line" not in prompt
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: Writing the reply: ")
    assert _plain(calls["status"][-1][1]) == (
        ":pencil2: The draft is being written. Your words: Three years.")


def test_each_rows_draft_runs_beside_the_others():
    """Draft it on two rows, one after the other: each turn has a side
    session of the thread, keyed by its row, and files named by its own
    status line, so neither waits for the other or writes over its body."""
    from preprocessor import slack_sessions as ss
    ns, calls = _bot([_q("ann", 4), _q("bo", 5)])
    _tick(ns)
    ns["handle_review_replies"](_ev(calls["cards"][0].actions[0].value, "m1", "t1"))
    ann, bo = calls["cards"][1:3]
    ns["handle_reply_draft"](_ev(ann.actions[0].value, "m2", "t1", text="Yes to the 20th."))
    ns["handle_reply_draft"](_ev(bo.actions[0].value, "m3", "t1"))
    assert [t[1] for t in calls["turns"]] == ["t1", "t1"]          # both post in the thread
    assert calls["sessions"] == [ss.side_key("t1", "reply-response-ann"),
                                 ss.side_key("t1", "reply-response-bo")]
    (_, _, ts_ann, prompt_ann, _, _), (_, _, ts_bo, prompt_bo, _, _) = calls["turns"]
    assert ts_ann != ts_bo
    assert (f"the brief goes to `/state/reply-body-{ts_ann}.brief.txt` and the body to "
            f"`/state/reply-body-{ts_ann}.txt`") in prompt_ann
    assert f"/state/reply-body-{ts_bo}.txt" in prompt_bo and ts_ann not in prompt_bo


def test_the_question_first_is_the_threads_own_turn():
    """A row that waits on the owner's answer, Draft it with no line: the
    turn asks, and the owner answers with a message in the thread, so it
    runs on the thread's session. An answer, picked or written, drafts in
    the row's side session."""
    from preprocessor import slack_sessions as ss
    ns, calls = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    card = calls["cards"][0]
    ns["handle_reply_draft"](_ev(card.actions[2].value, "m1", "t1"))
    ns, calls2 = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    ns["handle_reply_choice"](_ev(calls2["cards"][0].actions[0].value, "m1", "t1"))
    assert calls["sessions"] == [""]
    assert calls2["sessions"] == [ss.side_key("t1", "reply-response-lee")]

def test_a_new_send_card_takes_no_line():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "d-old"})
    ns, calls = _bot([row], confirms={"d-old": {"status": "expired"}})
    _tick(ns)
    assert [(a.label, a.ask) for a in calls["cards"][0].actions[:1]] == [
        (":lock: New Send card", "")]


def _plain(text: str) -> str:
    from botcore.chat import card_text
    return card_text.strip_inert(text)


ASKED = {"outcome": "decision", "note": "Renew for one year or for three?",
         "choices": ["Renew for one year", "Renew for three years"]}


def _labels(card) -> list[str]:
    from botcore.chat import card_text
    return [card_text.strip_inert(a.label) for a in card.actions]


def test_a_question_with_answers_shows_a_button_for_each():
    ns, calls = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    card = calls["cards"][0]
    assert "I need your answer before I write it: Renew for one year or for three?" in _text(card)
    assert _labels(card) == [":one: Renew for one year", ":two: Renew for three years",
                             ":pencil2: Other answer", "Later", "No reply needed"]
    assert [a.id for a in card.actions] == ["reply_choice_one", "reply_choice_two", "reply_draft",
                                            "reply_skip", "reply_not_needed"]
    assert [a.word for a in card.actions] == ["one", "two", "other", "later", "no reply needed"]
    assert card.actions[0].value == {"row": "response-lee", "batch": card.actions[0].value["batch"],
                                     "choice": "Renew for one year"}
    # No default: a thumbs up, or the bare code, picks no answer for the owner.
    from botcore.chat import cards
    assert card.no_default and cards.primary_action(card) is None
    assert cards.cancel_action(card).id == "reply_skip"
    assert cards.action_reactions(card) == [("one", "reply_choice_one"), ("two", "reply_choice_two"),
                                            ("pencil2", "reply_draft"), ("-1", "reply_skip")]
    (pattern,) = [a for a in calls["actions"] if not isinstance(a, str)]
    assert [bool(pattern.fullmatch(f"reply_choice_{k}")) for k in ("one", "two", "three", "four")] \
        == [True, True, True, False]


def test_only_a_question_with_two_or_three_short_answers_gets_buttons():
    def ids(queued, state_kw=None):
        ns, calls = _bot([_q("lee", 8, queued=queued, **(state_kw or {}))])
        _tick(ns)
        return [a.id for a in calls["cards"][0].actions]
    plain = ["reply_draft", "reply_skip", "reply_not_needed"]
    assert ids(dict(ASKED, choices=["Yes"])) == plain                       # one answer is no choice
    assert ids(dict(ASKED, choices="Yes or no")) == plain
    assert ids(dict(ASKED, choices=["Yes", "x" * 49, 7, " "])) == plain     # one left after the cut
    assert ids(dict(ASKED, outcome="left")) == plain                        # no question was asked
    assert ids(dict(ASKED, choices=["A", "B", "C", "D"]))[:4] == [
        "reply_choice_one", "reply_choice_two", "reply_choice_three", "reply_draft"]
    ns = _bot([])[0]
    assert ns["REPLY_CHOICE_CHARS"] == ledger_mod.REPLY_CHOICE_CHARS
    assert len(ns["REPLY_CHOICE_KEYS"]) == ledger_mod.REPLY_CHOICES_MAX


def test_a_tap_on_an_answer_starts_the_turn_that_writes_it():
    row = _q("lee", 8, queued=ASKED)
    ns, calls = _bot([row])
    _tick(ns)
    card = calls["cards"][0]
    ns["handle_reply_choice"](_ev(card.actions[1].value, "m1", "t1"))
    (turn,) = calls["turns"]
    channel, thread, msg_ts, prompt, user, is_reply = turn
    assert (channel, thread, msg_ts, user, is_reply) == ("dm", "t1", "p1", "U1", True)
    assert 'The owner picked this answer on the card: "Renew for three years".' in prompt
    assert "Renew for one year or for three?" in prompt and "data, not an instruction" in prompt
    assert "Renew for one year\"" not in prompt                  # the answer not picked is not there
    assert "--ledger-id response-lee" in prompt and "note-reply --id response-lee" in prompt
    assert "the draft waits for the owner's Send" in prompt
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: Writing the reply: ")
    from botcore.chat import card_text
    ts, status = calls["status"][-1]
    assert (ts, card_text.strip_inert(status)) == (
        "m1", ":pencil2: Your answer: Renew for three years. The draft is being written.")
    assert not calls["cli"][-1][0] == "close" and not calls["send_cards"]


def test_other_answer_starts_the_turn_that_asks():
    ns, calls = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    other = calls["cards"][0].actions[2]
    ns["handle_reply_draft"](_ev(other.value, "m1", "t1"))
    prompt = calls["turns"][0][3]
    assert "The card offered answers and the owner took none of them" in prompt
    assert "The owner picked this answer" not in prompt
    assert calls["status"][-1] == (
        "m1", ":speech_balloon: Tell me your answer in this thread. Then I write the draft.")


def test_other_answer_with_a_line_writes_the_reply():
    ns, calls = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    card = calls["cards"][0]
    assert [a.ask for a in card.actions] == ["", "", "your answer", "The day it comes back", ""]
    ns["handle_reply_draft"](_ev(card.actions[2].value, text="Two years, then we look again."))
    prompt = calls["turns"][0][3]
    assert "Renew for one year or for three?" in prompt
    assert 'The owner wrote this line on the card: "Two years, then we look again."' in prompt
    assert "took none of them" not in prompt and "The owner picked this answer" not in prompt
    assert _plain(calls["status"][-1][1]) == (
        ":pencil2: The draft is being written. Your words: Two years, then we look again.")


def test_a_tap_on_an_answer_the_row_no_longer_offers_does_nothing():
    """The card's answer is checked against the row as it is now: a newer
    message, another run's question, a draft or a hand-made value starts
    no turn."""
    row = _q("lee", 8, queued=ASKED)
    value = {"row": "response-lee", "batch": "b", "choice": "Renew for three years"}
    for now, clicked, kw in (
            ([dict(row, queued={})], value, {}),                               # a newer message
            ([dict(row, queued=dict(ASKED, choices=["Yes", "No"]))], value, {}),
            ([row], dict(value, choice="Wire the money today"), {}),
            ([row], dict(value, choice=""), {}),
            ([row], {"row": "response-lee", "batch": "b"}, {}),
            ([], value, {}),                                                   # answered or closed
            ([row], value, {"journal": [{"action": "created", "draft_id": "d-1",
                                         "ledger_ids": ["response-lee"],
                                         "ts": "2026-10-11T10:00:00Z"}]})):    # a draft exists
        ns, calls = _bot(now, **kw)
        ns["handle_reply_choice"](_ev(clicked))
        assert calls["status"] == [("m1", ns["REPLY_ROW_GONE"])], (now, clicked)
        assert not calls["turns"] and not calls["posts"]


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


def test_a_new_send_card_s_old_card_points_to_it_never_below():
    """On the web chat the newest card is at the top of Action requests:
    the old card links to the new one. Slack's DM lists cards in time, so
    there the new one is below."""
    for web, line in ((True, ":lock: Its new Send card: <https://chat.example/#/dm/s1/s1|open it>."),
                      (False, ":lock: The new Send card for this draft is below.")):
        confirms = {"d-old": {"status": "live", "channel": "dm", "message_ts": "c-old"}}
        if web:
            ns, calls, fe, _turns = _web([], confirms=confirms)
        else:
            ns, calls = _bot([], confirms=confirms)
        assert ns["_reply_new_send_card"]("d-old", "dm", None)
        assert calls["status"] == [("c-old", line)], web


def test_a_draft_that_is_gone_is_written_again():
    row = _q("pat", 6, queued={"outcome": "drafted", "draft_id": "gone-1"})
    ns, calls = _bot([row])
    _tick(ns)
    ns["handle_reply_draft"](_ev(calls["cards"][0].actions[0].value))
    assert not calls["send_cards"] and len(calls["turns"]) == 1


def test_later_snoozes_the_row_to_the_day_picked_or_three_days_on():
    """Later is Home's Later: a day from tomorrow to a year on. A press with
    no day (a thumbs down) is three days on; a day not after today changes
    nothing."""
    ns, calls = _bot([_q("pat", 4)])
    _tick(ns)
    later = calls["cards"][0].actions[1]
    assert (later.id, later.label, later.style, later.ask) == (
        "reply_skip", "Later", "", "The day it comes back")
    assert later.date == {"min": "2026-10-13", "max": "2027-10-13"}  # Slack: its name shows until a pick
    ns["handle_reply_skip"](_ev(later.value))
    assert calls["cli"][-1] == ("update", "--id", "response-pat", "--status", "snoozed",
                                "--snooze-until", "2026-10-15", "--last-action",
                                "2026-10-12: snoozed until 2026-10-15 on the Replies card")
    assert calls["status"][-1] == ("m1", ":fast_forward: Later. It comes back on Thursday.")
    ns["handle_reply_skip"](_ev(later.value, "m2", text="2026-10-13"))
    assert calls["cli"][-1][6] == "2026-10-13"
    assert calls["status"][-1] == ("m2", ":fast_forward: Later. It comes back tomorrow.")
    ns["handle_reply_skip"](_ev(later.value, "m3", text="2026-11-30"))
    assert calls["status"][-1] == ("m3", ":fast_forward: Later. It comes back on November 30.")
    n = len(calls["cli"])
    ns["handle_reply_skip"](_ev(later.value, "m4", text="2026-10-12"))
    assert len(calls["cli"]) == n + 1 and calls["cli"][-1][0] == "reply-queue"   # no update
    assert calls["status"][-1] == ("m4", ":warning: Pick a day after today. It stays on the list.")


def test_later_on_slack_is_a_date_picker_whose_row_comes_from_the_card_registry():
    """Slack's picker sends its day and no value: the row is the card's, as
    the registry keeps its actions."""
    ns, calls = _bot([_q("pat", 4)])
    _tick(ns)
    later = calls["cards"][0].actions[1]
    from botcore.chat import slack_frontend
    picker = slack_frontend._element(later)
    assert picker["type"] == "datepicker" and picker["placeholder"]["text"] == "Later"
    assert "initial_date" not in picker                    # its name shows until a day is picked
    ns["_CARDS"] = types.SimpleNamespace(by_ref=lambda ch, mid: {
        "status": "live", "actions": {"reply_skip": {"value": later.value}}})
    picked = _ev("", text="2026-10-20")
    picked.action_id = "reply_skip"
    ns["handle_reply_skip"](picked)
    assert calls["cli"][-1][:7] == ("update", "--id", "response-pat", "--status", "snoozed",
                                    "--snooze-until", "2026-10-20")


def test_the_web_chats_later_starts_three_days_on_and_thumbs_down_is_later():
    ns, calls, fe, turns, *_ = _setup(_q("pat", 4))
    card = ns["_reply_row_card"]("b1", _q("pat", 4), "none")
    later = next(a for a in card.actions if a.id == "reply_skip")
    assert later.date == {"min": "2026-10-13", "max": "2027-10-13", "proposed": "2026-10-15"}
    from botcore.chat import cards
    assert cards.cancel_action(card).id == "reply_skip"
    assert ns["_reply_quiet"]({"row": "x"})[0].label == ns["REPLY_LATER"] == "Later"
    from preprocessor import home_items
    assert ns["REPLY_NO_REPLY"] == home_items.NOT_NEEDED["label"] == "No reply needed"


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
        ":white_check_mark: No reply needed. Draft deleted.",
        ":white_check_mark: Closed. No reply goes out. Draft deleted."]


CLOSE_NOTE = "No reply needed: the owner's click on the Replies card"


def _not_needed(row, **kw) -> dict:
    ns, calls = _bot([row], **kw)
    _tick(ns)
    ns["handle_reply_not_needed"](_ev(calls["cards"][0].actions[2].value))
    return calls


def test_not_needed_says_where_the_mail_is():
    row = _q("pat", 6, conversation_id="conv-1")
    done = {"ok": True, "action": "closed"}
    for out, tail in [
            (dict(done, email_archive={"archived": True, "new_id": "m2"},
                  conversation_archive={"archived": True, "archived_count": 2, "errors": []}),
             " Its mail is out of the inbox."),
            (dict(done, conversation_archive={"archived": True, "archived_count": 0}),
             " Its mail is out of the inbox."),
            (dict(done, email_archive={"archived": False, "gone": True, "note": "stale"}),
             " I did not find its mail: it was moved or deleted before."),
            (dict(done, email_archive={"archived": False, "error": "timeout"}),
             " I could not move its mail out of the inbox."),
            (dict(done, email_archive={"archived": True},
                  conversation_archive={"archived": True, "errors": ["one failed"]}),
             " I could not move its mail out of the inbox."),
            (done, "")]:                                     # a row from no email
        calls = _not_needed(row, closes={"response-pat": out})
        assert calls["status"][-1][1] == ":white_check_mark: Closed. No reply goes out." + tail
        assert not [a for a in calls["cli"] if a[0] == "search"]


def test_not_needed_closes_the_threads_other_reply_rows():
    """A second row on the thread that a reply would finish closes with
    the card's row, so the thread leaves the inbox and the next card."""
    row = _q("pat", 6, conversation_id="conv-1")
    other = {"id": "kept-conv-1", "type": "response_needed", "status": "snoozed",
             "conversation_id": "conv-1", "description": "Answer Pat about the room"}
    calls = _not_needed(row, thread=[dict(row, status="open"), other], closes={
        "response-pat": {"ok": True, "action": "closed", "email_archive": {"archived": True},
                         "conversation_kept": ["kept-conv-1"]},
        "kept-conv-1": {"ok": True, "action": "closed",
                        "conversation_archive": {"archived": True, "archived_count": 3}}})
    assert calls["cli"][-3:] == [
        ("close", "--id", "response-pat", "--signal", "user_handled", "--note", CLOSE_NOTE),
        ("search", "--conversation-id", "conv-1", "--status", "open,follow_up,snoozed"),
        ("close", "--id", "kept-conv-1", "--signal", "user_handled", "--note", CLOSE_NOTE)]
    assert calls["status"][-1][1] == (
        ":white_check_mark: Closed. No reply goes out. Its mail is out of the inbox.")


def test_not_needed_leaves_a_row_that_no_reply_finishes():
    """A task or a row that waits on the other side stays open, and the
    card names it: its thread stays in the inbox."""
    row = _q("pat", 6, conversation_id="conv-1")
    task = {"id": "task-budget", "type": "task", "status": "open", "conversation_id": "conv-1",
            "description": "Send the budget sheet"}
    waits = {"id": "response-wait", "type": "waiting", "status": "open",
             "conversation_id": "conv-1", "description": "Pat sends the form"}
    stray = {"id": "response-else", "type": "response_needed", "status": "open",
             "conversation_id": "conv-2", "description": "Another thread"}
    calls = _not_needed(row, thread=[task, waits, stray], closes={
        "response-pat": {"ok": True, "action": "closed", "email_archive": {"archived": True},
                         "conversation_kept": ["task-budget", "response-wait"]}})
    assert [a[2] for a in calls["cli"] if a[0] == "close"] == ["response-pat"]
    assert _plain(calls["status"][-1][1]) == (
        ":white_check_mark: Closed. No reply goes out. Its thread stays in the inbox: "
        "Send the budget sheet and 1 more is still open on it.")


def test_not_needed_that_does_not_close_says_so():
    calls = _not_needed(_q("pat", 6, conversation_id="conv-1"),
                        closes={"response-pat": {"ok": False, "action": "refused"}})
    assert calls["status"][-1][1] == ":warning: I could not close it. It stays on the list."
    assert not [a for a in calls["cli"] if a[0] == "search"]


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


NO_REPLY_LINES = ("No email necessary", "no reply needed", "No reply needed.", "  NO REPLY NEEDED!! ",
                  "No reply is needed", "no response required", "No need to reply", "no need to respond.",
                  "No need for a reply", "Don\u2019t reply", "don't respond", "Do not reply", "No reply",
                  "No response", "No reply needed, thanks", "No reply needed. Thank you!",
                  '"No reply needed"', "no reply please")
NEAR_MISSES = ("No reply needed until Friday, then say yes", "no reply needed until friday",
               "No reply needed?", "Is no reply needed", "no", "No", "not needed", "Not needed.",
               "Tell them no reply is needed", "No, reply that we need more time", "don't reply yet",
               "Don't reply to Pat, reply to Lee", "No email", "no reply needed from them",
               "Yes, no reply needed on the form", "reply needed", "No thanks", "Decline it", "", "   ",
               "no reply needed\nbut say thanks")


def test_a_line_that_only_says_no_reply_is_needed_is_read_as_that_and_nothing_else():
    """ledger.no_reply_line: the whole line, in a fixed form, with case,
    curly quotes, spaces and end marks set aside; never part of a longer
    line, a bare no, "not needed" or a question."""
    for line in NO_REPLY_LINES:
        assert ledger_mod.no_reply_line(line), line
    for line in NEAR_MISSES:
        assert not ledger_mod.no_reply_line(line), line
    assert not ledger_mod.no_reply_line(None)


def test_a_line_that_says_no_reply_is_needed_is_no_reply_needed_and_starts_no_turn():
    """The owner types "No email necessary" where the reply's words go: the
    row closes as No reply needed does, and no turn writes a draft. A
    longer line that only starts so is the reply's words."""
    for text in ("No email necessary", "no reply needed.", "Don't reply"):
        row = _q("pat", 6, queued={"outcome": "decision", "note": "Answer them?"},
                 conversation_id="conv-1")
        ns, calls = _bot([row])
        _tick(ns)
        ns["handle_reply_draft"](_ev(calls["cards"][0].actions[0].value, text=text))
        assert not calls["turns"] and not calls["posts"], text
        assert ("close", "--id", "response-pat", "--signal", "user_handled",
                "--note", CLOSE_NOTE) in calls["cli"]
        assert [t for _ts, t in calls["status"]][-2:] == [
            ":hourglass_flowing_sand: Closing it...", ":white_check_mark: Closed. No reply goes out."]
    ns, calls = _bot([_q("pat", 6)])
    _tick(ns)
    ns["handle_reply_draft"](_ev(calls["cards"][0].actions[0].value,
                                 text="No reply needed until Friday, then say yes."))
    assert len(calls["turns"]) == 1 and not [a for a in calls["cli"] if a[0] == "close"]
    assert 'The owner wrote this line on the card: "No reply needed until Friday' in calls["turns"][0][3]


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


# ── the web chat: a decision answered in Action requests, in place ─────

def _plain(text):
    from botcore.chat import card_text
    return card_text.strip_inert(text)


class _WebFrontend:
    """The web chat as the Replies code sees it: Action requests takes
    button presses only. `said` is what the turn posts in the thread after
    its progress line; `tags` records the tag each post carried."""
    dm_takes_text = False

    def __init__(self, calls, said=""):
        self.calls, self.said, self.updates, self.tags, self._tag = calls, said, [], [], ""

    def tagging(self, tag):
        import contextlib
        fe = self

        @contextlib.contextmanager
        def ctx():
            before, fe._tag = fe._tag, tag
            try:
                yield
            finally:
                fe._tag = before
        return ctx()

    def post(self, channel="", thread_ts=None, text=""):
        self.calls["posts"].append((channel, thread_ts, text))
        self.tags.append(("post", text[:20], self._tag))
        return types.SimpleNamespace(id=f"p{len(self.calls['posts'])}")

    def update_card(self, ref, card):
        self.updates.append((ref.id, card))

    def permalink(self, channel, msg_id):
        return f"https://chat.example/#/{channel}/{msg_id}/{msg_id}"

    def thread_replies(self, channel, thread):
        msgs = [types.SimpleNamespace(msg_id=p, text="", posted_at=float(i), is_bot=True, is_self=True)
                for i, p in enumerate(f"p{n + 1}" for n in range(len(self.calls["posts"])))]
        if self.said:
            msgs.append(types.SimpleNamespace(msg_id="t-said", text=self.said, posted_at=999.0,
                                              is_bot=True, is_self=True))
        return msgs


VALUE = {"row": "response-lee", "batch": "b1"}


def _web(queue, said="", **kw):
    """The Replies section on the web chat, with its turns kept in memory,
    and a turn stand-in that ends as `turn` says (see _turn)."""
    ns, calls = _bot(queue, **kw)
    fe = _WebFrontend(calls, said)
    turns: dict = {}
    ns.update(FRONTEND=fe, _DRAINING=False, CHANNEL_ID="main", _dispatch_lock=_threading_lock(),
              _active_dispatches={}, _load_reply_turns=lambda: json.loads(json.dumps(turns)),
              _save_reply_turns=lambda d: (turns.clear(), turns.update(d)),
              _card_ref=lambda ch, ts: types.SimpleNamespace(channel=ch, id=ts, thread_key=ts))
    real_post_card = ns["_post_card"]

    def post_card(ch, card, thread_ts=None):  # the card carries the tag it was posted under
        fe.tags.append(("card", card.kind, fe._tag))
        return real_post_card(ch, card, thread_ts=thread_ts)
    ns["_post_card"] = post_card
    return ns, calls, fe, turns


def _threading_lock():
    import threading
    return threading.Lock()


def _turn(ns, calls, *, record=None, draft=None, journal=None, confirms=None, how="done"):
    """A turn stand-in as handle_message_async runs one: it may make a
    draft (its Send card posts, as _note_draft_confirmation calls
    _reply_draft_carded) or write the row's record (note-reply), then its
    own end calls _reply_turn_ended."""
    def run(channel, thread, progress, prompt, user_id, is_reply, session_key=""):
        calls["turns"].append((channel, thread, progress, prompt))
        calls["sessions"].append(session_key)
        calls["posts"].append((channel, thread, "turn words"))
        ns["FRONTEND"].tags.append(("turn", "", ns["FRONTEND"]._tag))
        drafts = []
        if draft:
            journal.append({"action": "created", "draft_id": draft, "ledger_ids": ["response-lee"],
                            "ts": "2026-10-09T10:00:00"})
            confirms[draft] = {"status": "live", "channel": "dm", "message_ts": "w77"}
            ns["_reply_draft_carded"](draft, "dm", "w77")
            drafts = [draft]
        if record is not None:
            from datetime import timezone as _tz
            ns["_test_queue"][0]["queued"] = dict(record, at=datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ns["_reply_turn_ended"](progress, how, drafts)
    ns["handle_message_async"] = run


def _queue(row):
    q = [row]
    return q


def test_a_decision_card_carries_its_question_for_the_web_chat():
    """The decision row's card names its question, who wrote and when, for
    the web chat's answer control; Slack's card is as it was."""
    ns, calls = _bot([_q("lee", 8, queued=ASKED)])
    _tick(ns)
    card = calls["cards"][0]
    assert card.question == {"text": "Renew for one year or for three?", "from": "Lee",
                             "since": "2026-10-05T16:00:00Z", "about": "Reply to Lee"}
    assert card.problem == {} and "I need your answer before I write it" in _text(card)
    ns, calls = _bot([_q("ana", 8)])  # no decision: no question, the card as before
    _tick(ns)
    assert calls["cards"][0].question == {}


def _setup(row=None, **kw):
    row = row or _q("lee", 8, queued=dict(ASKED))
    q = [row]
    journal, confirms = [], {}
    ns, calls, fe, turns = _web(q, journal=journal, confirms=confirms, **kw)
    ns["_test_queue"] = q
    ns["_draft_events"].ledger_ids_of = lambda did: [
        lid for ev in journal if ev.get("draft_id") == did for lid in ev.get("ledger_ids") or ()]
    return ns, calls, fe, turns, journal, confirms


def test_web_a_draft_the_turn_made_links_its_send_request_and_nothing_else_shows():
    """The card says "Writing the reply" before the turn runs, and once the
    turn's draft has its Send card, links to it: no timer, no Try again."""
    ns, calls, fe, turns, journal, confirms = _setup()
    _turn(ns, calls, draft="d-7", journal=journal, confirms=confirms)
    ns["handle_reply_draft"](_ev(VALUE, text="Three years, at the same rate."))
    statuses = [(_ts, _plain(t)) for _ts, t in calls["status"]]
    assert statuses == [("m1", ":hourglass_flowing_sand: Writing the reply: Three years, at the same rate."),
                        ("m1", ":white_check_mark: The reply is drafted. Its Send request: "
                               "<https://chat.example/#/dm/w77/w77|open it>.")]
    assert len(calls["turns"]) == 1 and calls["cards"] == []
    (t,) = turns.values()
    assert t["outcome"] == "drafted" and t["ended"]
    # the turn's posts and the progress line carry the row's tag
    assert [x for x in fe.tags if x[0] in ("post", "turn")] == [
        ("post", ":hourglass_flowing_s", "reply:response-lee"), ("turn", "", "reply:response-lee")]


def test_web_a_late_send_card_attaches_and_corrects_the_card_shown_before():
    """A turn that left no draft shows its "could not" card; the draft's
    Send card that posts later (the same row's `--ledger-id`) takes both the
    pressed card and that card over: nothing wrong is left behind."""
    ns, calls, fe, turns, journal, confirms = _setup()
    _turn(ns, calls)
    ns["handle_reply_draft"](_ev(VALUE, card_id="m7", text="Three years."))
    problem = calls["cards"][-1]                                   # posted as m1
    assert problem.problem["text"] == "Tacitus did not say what happened." and problem.question == {}
    assert calls["status"][-1] == ("m7", ":warning: The reply was not written: "
                                         "<https://chat.example/#/dm/m1/m1|see why>.")
    journal.append({"action": "created", "draft_id": "d-9", "ledger_ids": ["response-lee"], "ts": "x"})
    ns["_reply_draft_carded"]("d-9", "dm", "w99")
    drafted = ":white_check_mark: The reply is drafted. Its Send request: <https://chat.example/#/dm/w99/w99|open it>."
    assert ("m7", drafted) in calls["status"] and ("m1", drafted) in calls["status"]
    (t,) = turns.values()
    assert t["outcome"] == "drafted"
    assert not any("not written" in text for ts, text in calls["status"][-2:])


def test_web_a_second_press_never_starts_a_second_turn():
    """While the row's turn runs (or another press holds the row), a press
    starts nothing; a press on another card of the row says the reply is
    being written already. Once the row's draft exists, the press offers its
    Send card, as before."""
    ns, calls, fe, turns, journal, confirms = _setup()
    ns["handle_message_async"] = lambda *a, session_key="": calls["turns"].append(a)  # still running
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    ns["_reply_after_run"] = lambda progress: None
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))            # the same card again
    ns["handle_reply_draft"](_ev(dict(VALUE, choice="x"), card_id="m9", text="One year."))  # another card
    assert len(calls["turns"]) == 1
    assert _plain(calls["status"][-1][1]).startswith(":hourglass_flowing_sand: This reply is being written already")
    assert calls["status"][-1][0] == "m9"
    assert ns["_reply_claim"]("response-lee") is False
    # a press that holds the row: the second, at the same moment, waits on it
    ns2, calls2, *_ = _setup()
    ns2["_REPLY_CLAIMED"].add("response-lee")
    ns2["handle_reply_draft"](_ev(VALUE, text="Three years."))
    assert calls2["turns"] == []


def test_web_only_a_question_is_a_question_and_unmarked_text_is_could_not():
    """The turn's record says what it left: decision is a question card
    with the answer control; blocked says why with Try again and Open in Ask
    Tacitus; left closes the card with what happened. Text with no record is
    the could-not card, never a question; a record older than the turn
    counts for nothing."""
    ns, calls, fe, turns, *_ = _setup()
    _turn(ns, calls, record={"outcome": "decision", "note": "Which rate should it state?",
                             "inbound": "x", "choices": ["The old rate", "The new rate"]})
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    q = calls["cards"][-1]
    assert q.question["text"] == "Which rate should it state?" and q.problem == {}
    assert [a.id for a in q.actions][:2] == ["reply_choice_one", "reply_choice_two"]
    assert _plain(calls["status"][-1][1]).startswith(":speech_balloon: One more answer is needed: <")

    ns, calls, fe, turns, *_ = _setup()
    _turn(ns, calls, record={"outcome": "blocked", "note": "The thread has no address to answer.", "inbound": "x"})
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    b = calls["cards"][-1]
    assert b.question == {} and b.problem["text"] == "The thread has no address to answer."
    assert [(a.id, a.ask) for a in b.actions] == [("reply_draft", ""), ("reply_discuss", ""),
                                                  ("reply_skip", "The day it comes back"),
                                                  ("reply_not_needed", "")]
    assert b.actions[0].value["again"] == "Three years."            # Try again repeats the answer
    assert b.actions[1].value["why"] == "The thread has no address to answer."
    assert "I could not write it: " in _text(b)

    ns, calls, fe, turns, *_ = _setup()
    _turn(ns, calls, record={"outcome": "left", "note": "They answered the question themselves.", "inbound": "x"})
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    assert calls["cards"] == []
    assert _plain(calls["status"][-1][1]) == ":white_check_mark: No reply is needed: They answered the question themselves."

    old = _q("lee", 8, queued=dict(ASKED, at="2020-01-01T00:00:00Z"))   # a briefing run's, not this turn's
    ns, calls, fe, turns, *_ = _setup(row=old, said="I looked, and the thread asks something unclear?")
    _turn(ns, calls)
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    u = calls["cards"][-1]
    # text that reads like a question, with no record: its own words as the reason, never a question
    assert u.question == {} and u.problem["text"] == "I looked, and the thread asks something unclear?"
    assert not any(a.ask and not a.date for a in u.actions)        # no line of words, only Later's day


def test_web_a_turn_cut_short_says_so_in_plain_words():
    for how, words in (("stopped", "The reply was stopped before it was written."),
                       ("timeout", "Writing the reply took too long, so it was stopped."),
                       ("error", "Something went wrong while writing the reply.")):
        ns, calls, fe, turns, *_ = _setup()
        _turn(ns, calls, how=how)
        ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
        assert calls["cards"][-1].problem["text"] == words, how


def test_web_a_restart_mid_turn_ends_truthfully():
    """A restart that parks the turn shows nothing yet; the next instance's
    run of the same turn ends it; a turn no instance finished by its limit
    ends as lost; one still running is left alone. "Still writing" shows
    after REPLY_STILL_S and is written again, so the list keeps it at work."""
    ns, calls, fe, turns, journal, confirms = _setup()
    ns["handle_message_async"] = lambda *a, session_key="": calls["turns"].append(a)
    ns["_DRAINING"] = True
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    (progress,) = turns
    assert not turns[progress].get("ended")                     # parked: no outcome yet
    ns["_DRAINING"] = False
    _turn(ns, calls, draft="d-5", journal=journal, confirms=confirms)
    ns["handle_message_async"]("dm", "m1", progress, "", "U1", True)   # the next instance's run
    assert turns[progress]["outcome"] == "drafted"

    ns, calls, fe, turns, *_ = _setup()
    ns["handle_message_async"] = lambda *a, session_key="": None
    ns["_reply_after_run"] = lambda progress: None
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    (progress,) = turns
    start = turns[progress]["started"]
    ns["_active_threads"].add(progress)
    ns["_reply_watch_tick"](start + ns["REPLY_STILL_S"] + 1)
    assert _plain(calls["status"][-1][1]) == ":hourglass_flowing_sand: Still writing the reply: Three years."
    n = len(calls["status"])
    ns["_reply_watch_tick"](start + ns["REPLY_STILL_S"] + 30)
    assert len(calls["status"]) == n                             # not again before REPLY_REFRESH_S
    ns["_reply_watch_tick"](start + ns["REPLY_STILL_S"] + ns["REPLY_REFRESH_S"] + 1)
    assert len(calls["status"]) == n + 1
    ns["_reply_watch_tick"](start + ns["REPLY_TURN_LIMIT_S"] + 60)
    assert not turns[progress].get("ended")                      # still running here: left alone
    ns["_active_threads"].discard(progress)
    ns["_reply_watch_tick"](start + ns["REPLY_TURN_LIMIT_S"] + 120)
    assert turns[progress]["outcome"] == "problem"
    assert calls["cards"][-1].problem["text"] == "Tacitus restarted while writing the reply."


def test_web_a_turn_the_old_process_left_ends_soon_after_a_restart():
    """A turn begun before this process started, that no run here took up
    within the catch-up's grace, ended with the old process: it says so
    then, not an hour later; one the catch-up re-ran is left to its run."""
    ns, calls, fe, turns, *_ = _setup()
    ns["handle_message_async"] = lambda *a, session_key="": None
    ns["_reply_after_run"] = lambda progress: None
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    (progress,) = turns
    turns[progress]["started"] = ns["_REPLY_PROCESS_START"] - 60
    start = ns["_REPLY_PROCESS_START"]
    ns["_active_threads"].add(progress)                       # the catch-up re-ran it
    ns["_reply_watch_tick"](start + ns["REPLY_RESTART_GRACE_S"] + 1)
    assert not turns[progress].get("ended")
    ns["_active_threads"].discard(progress)
    ns["_REPLY_TICKS"]["watch"] = 0
    ns["_reply_watch_tick"](start + ns["REPLY_RESTART_GRACE_S"] + 20)
    assert turns[progress]["outcome"] == "problem"
    assert calls["cards"][-1].problem["text"] == "Tacitus restarted while writing the reply."


def test_web_open_in_ask_tacitus_starts_a_conversation_about_the_row():
    ns, calls, fe, turns, *_ = _setup()
    ns["handle_reply_discuss"](_ev(dict(VALUE, why="The thread has no address."), card_id="m4"))
    channel, thread, text = calls["posts"][-1]
    assert (channel, thread) == ("main", None)
    assert "Reply to Lee" in text and "The thread has no address." in text and "`response-lee`" in text
    assert calls["status"][-1] == ("m4", ":speech_balloon: Opened in Ask Tacitus: "
                                         f"<https://chat.example/#/main/p{len(calls['posts'])}/p{len(calls['posts'])}|open it>.")


def test_web_a_card_whose_thread_no_longer_waits_says_so():
    ns, calls, fe, turns, *_ = _setup()
    ns["_test_queue"].clear()
    store = {"cards": {"b1-x": {"rows": ["response-lee"], "channel": "dm", "card": "m1"}}}
    calls["cards"].append(object())                    # m1 is a live card
    ns["_load_reply_cards"] = lambda: store
    ns["_reply_watch_tick"](10 ** 10)
    assert calls["status"][-1] == ("m1", ":information_source: This thread no longer waits for a reply.")


def test_web_a_card_whose_row_moved_to_later_says_when_it_comes_back():
    """Later on Home snoozes the row; its card in Action requests then says
    Later and the day, never that the thread no longer waits."""
    ns, calls, fe, turns, *_ = _setup()
    ns["_test_queue"].clear()
    store = {"cards": {"b1-x": {"rows": ["response-lee"], "channel": "dm", "card": "m1"}}}
    calls["cards"].append(object())
    ns["_load_reply_cards"] = lambda: store
    cli = ns["_ledger_cli"]
    ns["_ledger_cli"] = lambda *a: {"id": "response-lee", "status": "snoozed",
                                    "snooze_until": "2026-10-16"} if a[0] == "get" else cli(*a)
    ns["_reply_watch_tick"](10 ** 10)
    assert calls["status"][-1] == ("m1", ":fast_forward: Later. It comes back on Friday.")
    assert not any("no longer waits" in t for _m, t in calls["status"])
    # a turn that ends after the owner moved the row to later says the same
    ns["_reply_clock"] = lambda: ("2026-10-15", 9, False)
    assert ns["_reply_gone_line"]("response-lee") == ":fast_forward: Later. It comes back tomorrow."


def test_on_the_web_a_draft_that_did_not_start_offers_try_again_with_the_answer_kept():
    """No turn started: the same card again, in place, with Try again and
    the owner's answer. Never "Ask me here"."""
    ns, calls, fe, turns, *_ = _setup()
    ns["_dispatch_reply_draft"] = lambda *a, **k: False
    ns["handle_reply_draft"](_ev(VALUE, text="Three years."))
    ((card_id, card),) = fe.updates
    assert card_id == "m1" and card.question["failed"] and card.question["answer"] == "Three years."
    assert "The draft did not start. Try again." in _text(card) and calls["status"] == []
    assert not any("Ask me here" in t for _ts, t in calls["status"])
    assert ns["_reply_claim"]("response-lee")             # the row is free again


def test_web_a_line_that_says_no_reply_is_needed_closes_the_row_with_no_turn():
    ns, calls, fe, turns, *_ = _setup()
    ns["handle_reply_draft"](_ev(VALUE, text="No email necessary."))
    assert turns == {} and not calls["turns"] and not calls["posts"]
    assert ("close", "--id", "response-lee", "--signal", "user_handled", "--note", CLOSE_NOTE) \
        in calls["cli"]
    assert calls["status"][-1] == ("m1", ":white_check_mark: Closed. No reply goes out.")


# ── the web chat: each reply that waits is its own block ─────────────────

def _next_day(ns, queue, day="2026-10-13"):
    ns["_reply_clock"] = lambda: (day, 9, False)
    for r in queue:
        r["age_days"] += 1


def _rows(cards) -> list[str]:
    return [c.actions[0].value["row"] for c in cards]


def _card_of(calls, rid) -> tuple[str, object]:
    """(its id, the card) of the last card posted for row `rid`."""
    i = max(i for i, c in enumerate(calls["cards"]) if c.actions[0].value.get("row") == rid)
    return f"m{i + 1}", calls["cards"][i]


def test_web_each_reply_that_waits_is_its_own_block_with_no_card_to_press_first():
    """The day's tick posts each thread that waits as its own block at the
    top of Action requests, tagged with its row: no summary card, no One by
    one. The youngest posts first, so the oldest is the newest post, which
    the list shows on top. A thread whose Send card is up gets none."""
    queue = [_q("pat", 12, queued={"outcome": "drafted", "draft_id": "d-old"}),
             _q("lee", 8, queued=dict(ASKED)), _q("sam", 5, scheduling="request"), _q("kim", 3),
             _q("ready", 4, queued={"outcome": "drafted", "draft_id": "d-live"})]
    ns, calls, fe, _turns = _web(queue, confirms={"d-live": {"status": "live"}})
    assert _tick(ns) == 4
    assert calls["threads"] == [None] * 4                 # each at the top, its own thread
    assert _rows(calls["cards"]) == ["response-kim", "response-sam", "response-lee", "response-pat"]
    from botcore.chat import action_requests
    shown = action_requests.order([{"id": f"m{i + 1}", "posted_at": float(i)} for i in range(4)])
    assert shown[0]["id"] == "m4" and _card_of(calls, "response-pat")[0] == "m4"   # the oldest on top
    assert [t[2] for t in fe.tags if t[0] == "card"] == [
        "reply:response-kim", "reply:response-sam", "reply:response-lee", "reply:response-pat"]
    assert not [a for c in calls["cards"] for a in c.actions if a.id == "review_replies"]
    for c in calls["cards"]:
        assert [(a.id, a.label) for a in c.actions[-2:]] == [
            ("reply_skip", "Later"), ("reply_not_needed", "No reply needed")]
    assert _card_of(calls, "response-lee")[1].question["text"] == "Renew for one year or for three?"
    assert calls["status"] == [] and _tick(ns) == 0       # once a day
    # A press on a block works as on a One by one card.
    mid, kim = _card_of(calls, "response-kim")
    ns["handle_reply_skip"](_ev(kim.actions[1].value, mid, text="2026-10-20"))
    assert calls["cli"][-1][:7] == ("update", "--id", "response-kim", "--status", "snoozed",
                                    "--snooze-until", "2026-10-20")


def test_web_the_day_s_blocks_push_once_with_how_many_replies_wait():
    """Each reply is its own block, but the morning's blocks notify once:
    one push that says how many replies wait (ChatFrontend.one_push), the
    ones whose block was up already counted too."""
    import contextlib
    queue = [_q("pat", 12), _q("lee", 8), _q("kim", 3)]
    ns, calls, fe, _turns = _web(queue)
    lines, inside = [], []

    @contextlib.contextmanager
    def one_push(text_for):
        inside.append(len(calls["cards"]))
        yield
        lines.append(text_for(len(calls["cards"]) - inside[-1]))
    fe.one_push = one_push
    assert _tick(ns) == 3
    assert inside == [0] and lines == ["3 replies wait for you."]
    queue.append(_q("ann", 2))
    _next_day(ns, queue)
    assert _tick(ns) == 1                                 # one new block, three still up
    assert lines[-1] == "4 replies wait for you."


def test_web_a_row_with_a_live_block_never_gets_a_second_one():
    """The next day's tick posts no block for a row whose block is still
    live, and retires none: the block is drawn again in place, with no
    push, for its new age or a new question. A row answered since gets a
    block again only when it waits again; a new row gets its own."""
    queue = [_q("pat", 6), _q("lee", 4)]
    ns, calls, fe, _turns = _web(queue)
    assert _tick(ns) == 2
    pat_id, lee_id = _card_of(calls, "response-pat")[0], _card_of(calls, "response-lee")[0]
    _next_day(ns, queue)
    assert _tick(ns) == 0 and len(calls["cards"]) == 2 and calls["status"] == []
    assert [(ref, c.actions[0].value["row"]) for ref, c in fe.updates] == [
        (pat_id, "response-pat"), (lee_id, "response-lee")]
    assert "From Pat, 7 days ago." in _text(fe.updates[0][1])
    # The same day and age again: nothing is drawn.
    ns["_reply_clock"] = lambda: ("2026-10-14", 9, False)
    assert _tick(ns) == 0 and len(fe.updates) == 2
    # A briefing run asks Lee's question: Lee's block shows it, in place.
    queue[1]["queued"] = dict(ASKED)
    ns["_reply_clock"] = lambda: ("2026-10-15", 9, False)
    assert _tick(ns) == 0 and len(calls["cards"]) == 2
    ref, card = fe.updates[-1]
    assert ref == lee_id and [a.id for a in card.actions][:2] == ["reply_choice_one", "reply_choice_two"]
    # Pat is answered (No reply needed closes the block); Pat waits again,
    # and Kim is new: each gets one block.
    ns["handle_reply_not_needed"](_ev(_card_of(calls, "response-pat")[1].actions[2].value, pat_id))
    queue.append(_q("kim", 3))
    _next_day(ns, queue, "2026-10-16")
    assert _tick(ns) == 2
    assert _rows(calls["cards"][2:]) == ["response-kim", "response-pat"]
    assert sorted(c["card"] for c in ns["_load_reply_cards"]()["cards"].values()) == sorted(
        [lee_id, "m3", "m4"])


def test_web_at_most_ten_blocks_wait_at_once_oldest_first():
    queue = [_q(f"p{n}", 30 - n) for n in range(13)]
    ns, calls, fe, _turns = _web(queue)
    assert _tick(ns) == 10
    assert sorted(_rows(calls["cards"])) == sorted(f"response-p{n}" for n in range(10))
    _next_day(ns, queue)
    assert _tick(ns) == 0                                 # ten still wait for a tap
    for n in range(3):                                    # three are answered
        mid, card = _card_of(calls, f"response-p{n}")
        ns["handle_reply_not_needed"](_ev(card.actions[2].value, mid))
    del queue[:3]
    _next_day(ns, queue, "2026-10-14")
    assert _tick(ns) == 3
    assert _rows(calls["cards"][10:]) == ["response-p12", "response-p11", "response-p10"]


def test_web_a_could_not_card_keeps_its_reason_until_its_row_changes():
    """The card a turn left (here: why it wrote no reply) is the row's live
    block: no second block comes; the next day draws it again with its
    reason; once the row changes, it is the row's card as it is now."""
    ns, calls, fe, turns, *_ = _setup(_q("lee", 8, queued=dict(ASKED)))
    queue = ns["_test_queue"]
    _turn(ns, calls, record={"outcome": "blocked", "note": "The thread has no address.", "inbound": "x"})
    ns["handle_reply_draft"](_ev(VALUE, card_id="m7", text="Three years."))
    assert len(calls["cards"]) == 1 and calls["cards"][0].problem
    _next_day(ns, queue)
    assert _tick(ns) == 0 and len(calls["cards"]) == 1
    ref, card = fe.updates[-1]
    assert ref == "m1" and card.problem["text"] == "The thread has no address."
    assert card.actions[0].value["again"] == "Three years."
    queue[0]["queued"] = dict(ASKED, note="Renew at the new rate?")
    _next_day(ns, queue, "2026-10-14")
    assert _tick(ns) == 0 and len(calls["cards"]) == 1
    ref, card = fe.updates[-1]
    assert ref == "m1" and card.problem == {} and card.question["text"] == "Renew at the new rate?"


def test_web_a_row_whose_reply_is_being_written_gets_no_block():
    ns, calls, fe, turns, *_ = _setup(_q("lee", 8, queued=dict(ASKED)))
    turns["p9"] = {"row": {"id": "response-lee"}, "card": "m0", "channel": "dm", "started": 1.0}
    assert _tick(ns) == 0 and calls["cards"] == []


def test_web_a_replies_card_from_before_gives_way_and_its_press_posts_the_blocks():
    """A Replies card posted before each reply was its own block: the day's
    tick settles it, and a press on one posts the blocks of its rows that
    still wait, at the top, and settles it. A row with a live block (a One
    by one card from that day) gets no second one."""
    queue = [_q("pat", 6), _q("lee", 4), _q("sam", 3)]
    ns, calls, fe, _turns = _web(queue)
    old = ns["_post_card"]("dm", ns["_replies_card"]("b0", [(r, "none") for r in queue], 0))
    one = ns["_post_card"]("dm", ns["_reply_row_card"]("b0-pat", queue[0], "none"), thread_ts=old.id)
    ns["_save_reply_cards"]({"day": "2026-10-11", "cards": {
        "b0": {"rows": [r["id"] for r in queue], "channel": "dm", "card": old.id},
        "b0-pat": {"rows": ["response-pat"], "channel": "dm", "card": one.id}}})
    press = ns["_replies_card"]("b0", [(r, "none") for r in queue], 0).actions[0]
    ns["handle_review_replies"](_ev(press.value, old.id))
    assert calls["status"] == [(old.id, ":information_source: Each reply that waits is its own request now.")]
    assert _rows(calls["cards"][2:]) == ["response-sam", "response-lee"]
    assert calls["threads"][2:] == [None, None]
    assert [ref for ref, _c in fe.updates] == ["m2"]      # Pat's card from that day, drawn again
    ns2, calls2, fe2, _t = _web([_q("pat", 6), _q("lee", 4)])
    old2 = ns2["_post_card"]("dm", ns2["_replies_card"]("b0", [(_q("pat", 6), "none"), (_q("lee", 4), "none")], 0))
    ns2["_save_reply_cards"]({"day": "2026-10-11", "cards": {
        "b0": {"rows": ["response-pat", "response-lee"], "channel": "dm", "card": old2.id}}})
    assert _tick(ns2) == 2
    assert calls2["status"][0] == (old2.id, ":information_source: Each reply that waits is its own request now.")
    assert sorted(c["card"] for c in ns2["_load_reply_cards"]()["cards"].values()) == ["m2", "m3"]


# ── end to end on the real web front end, with a stand-in turn ──────────

def test_on_the_real_web_chat_two_rows_in_one_thread_each_show_their_own_outcome(tmp_path):
    """The Replies section on a real web front end (a temporary store),
    with turns stood in: two rows' cards share the One by one thread. Row
    A's turn makes a draft; row B's leaves no record. GET /api/requests
    then shows A's card linking to its Send request (`next`), B's could-not
    card ("Not written", its reason), each block with only its own row's
    lines, and an untagged line of the thread in no block."""
    from preprocessor.chat.web_frontend import WebFrontend
    from preprocessor.chat.types import MessageRef
    fe = WebFrontend.from_config({"port": 0, "tailscale_login": "owner@example.test",
                                  "public_url": "https://host.example.test/chat"},
                                 store_path=tmp_path / "webchat" / "webchat.sqlite",
                                 webui_dir=tmp_path / "webui", trusted_config=True)
    fe.start()
    try:
        rows = [_q("ana", 8, queued=dict(ASKED)), _q("ben", 8, queued=dict(ASKED))]
        journal, confirms = [], {}
        ns, calls, _fe, turns = _web(rows, journal=journal, confirms=confirms)
        ns["_test_queue"] = rows
        ns["_draft_events"].ledger_ids_of = lambda did: [
            lid for ev in journal if ev.get("draft_id") == did for lid in ev.get("ledger_ids") or ()]
        registry: dict = {}

        def post_card(ch, card, thread_ts=None):
            ref = fe.post_card(ch, card, thread_ts=thread_ts)
            registry[ref.id] = "live"
            return ref

        def replace(ch, ts, text):
            registry[ts] = "retired"
            fe.replace_card_with_status(MessageRef(ch, ts, ts), text)
        ns.update(FRONTEND=fe, _post_card=post_card, _replace_buttons_with_status=replace,
                  _card_ref=lambda ch, ts: MessageRef(ch, ts, ts),
                  _CARDS=types.SimpleNamespace(by_ref=lambda ch, mid: {"status": registry.get(mid, "")}
                                                if mid in registry else None))
        parent = fe.post_card("dm", ns["_replies_card"]("b1", [(r, "decision") for r in rows], 0))
        cards = {}
        for r in rows:
            with ns["_reply_tagged"](r["id"]):
                cards[r["id"]] = post_card("dm", ns["_reply_row_card"]("b1", r, "decision"),
                                           thread_ts=parent.thread_key)
        fe.post("dm", ":hourglass_flowing_sand: Still working... (3m)", thread_ts=parent.thread_key)

        def turn(channel, thread, progress, prompt, user_id, is_reply, session_key=""):
            rid = "response-ana" if "response-ana" in prompt else "response-ben"
            fe.post(channel, f"Turn words for {rid[-3:]}.", thread_ts=thread)
            drafts = []
            if rid == "response-ana":
                send = fe.post_card("dm", ns["_chat"].ConfirmationCard(
                    kind="send", title=":lock: Send this reply?", actions=[
                        ns["_chat"].CardAction("approve_send", "Send", {"d": 1}, "primary", "send")]))
                journal.append({"action": "created", "draft_id": "d-a", "ledger_ids": [rid], "ts": "x"})
                confirms["d-a"] = {"status": "live", "channel": "dm", "message_ts": send.id}
                ns["_reply_draft_carded"]("d-a", "dm", send.id)
                drafts = ["d-a"]
            ns["_reply_turn_ended"](progress, "done", drafts)
        ns["handle_message_async"] = turn
        thread = parent.thread_key
        real_turn = turn

        def turn_late_words(channel, thread_, progress, prompt, user_id, is_reply, session_key=""):
            # Row ana's turn, still running, writes a line after ben's last words: by time it
            # would be ben's reason; by tag it is ana's alone.
            if "response-ben" not in prompt:
                return real_turn(channel, thread_, progress, prompt, user_id, is_reply, session_key)
            fe.post(channel, "Turn words for ben.", thread_ts=thread_)
            with ns["_reply_tagged"]("response-ana"):
                fe.post(channel, "A late line of row ana.", thread_ts=thread_)
            ns["_reply_turn_ended"](progress, "done", [])
        ns["handle_message_async"] = turn_late_words
        for rid in ("response-ana", "response-ben"):
            ns["handle_reply_draft"](types.SimpleNamespace(
                value={"row": rid, "batch": "b1"}, user_id="U1", text="Yes, three years.",
                card=types.SimpleNamespace(channel="dm", id=cards[rid].id, thread_key=thread)))
        out = fe.requests()
        items = {j["id"]: j for j in out["waiting"] + out["done"]}
        a, b = items[cards["response-ana"].id], items[cards["response-ben"].id]
        send_id = confirms["d-a"]["message_ts"]
        assert a["next"] == send_id and items[send_id]["waiting"]          # A: its Send request
        assert all("ana" in x["text"].lower() for x in a["lines"]) and a["lines"]
        assert "A late line of row ana." in [x["text"] for x in a["lines"]]
        assert all("ben" in x["text"].lower() for x in b["lines"]) and b["lines"]
        assert not any("Still working" in x["text"] for j in items.values() for x in j.get("lines") or [])
        could_not = items[b["next"]]
        assert (could_not["action"], could_not["waiting"]) == ("Not written", True)
        assert could_not["what"] == "Turn words for ben."
        assert could_not["card"]["problem"]["text"] == "Turn words for ben."
        assert not could_not["card"]["question"]
    finally:
        fe.stop()


def test_the_turn_s_own_end_the_send_card_and_the_tick_are_wired():
    """handle_message_async names how each turn ended and the drafts it
    made, and hands them to _reply_turn_ended in its own end; a Send card's
    registry write tells the Replies rows of its draft; the router tick
    checks the turns. No timer decides an outcome."""
    start = BOT_SRC.index("def handle_message_async(")
    hm = BOT_SRC[start:BOT_SRC.index('@app.event("message")', start)]
    for how in ('turn_end = "done"', 'turn_end = "stopped"', 'turn_end = "timeout"', 'turn_end = "error"'):
        assert hm.count(how) == 1, how
    fin = hm[hm.rindex("    finally:"):]
    assert "_reply_turn_ended(msg_ts, turn_end, run_drafts)" in fin
    assert hm.count("run_drafts = _surface_run_drafts(") == 3
    note = BOT_SRC[BOT_SRC.index("def _note_draft_confirmation("):BOT_SRC.index("def _set_draft_fingerprint(")]
    assert "_reply_draft_carded(draft_id, confirm_channel, confirm_ts)" in note
    assert "_reply_watch_tick()" in BOT_SRC[BOT_SRC.index("def resident_router_loop("):]
    assert "REPLY_DONE_WAIT_S" not in BOT_SRC and "time.sleep(3)" not in BOT_SRC[
        BOT_SRC.index("# The web chat (a DM that takes button presses only)"):BOT_SRC.index('@app.action("reply_draft")')]


# ── Home's choices on a reply (preprocessor/home_items.py) ───────────

def _home_bot(queue, **kw):
    import threading as _threading
    ns, calls = _bot(queue, **kw)
    ns.update(OWNER_SLACK_USER_ID="U1", _DRAINING=False, _dispatch_lock=_threading.Lock(),
              _active_dispatches={})
    return ns, calls


def test_home_draft_starts_the_replies_draft_turn_on_a_new_line_in_action_requests():
    ns, calls = _home_bot([_q("ana", 1)])
    got = ns["_home_draft"]("response-ana")
    assert got == {"ok": True, "channel": "dm", "id": "p1",
                   "said": "Tacitus is writing the reply. Its Send card comes to this DM."}
    assert calls["posts"][0][:2] == ("dm", None)                 # a new line, its own thread
    assert calls["posts"][0][2].startswith(":hourglass_flowing_sand: Writing the reply: ")
    (turn,) = calls["turns"]
    assert turn[:3] == ("dm", "p1", "p1") and turn[4] == "U1"
    assert "--ledger-id response-ana" in turn[3] and calls["sessions"][0].endswith("reply-response-ana")
    assert calls["cli"][0] == ("reply-queue", "--min-age", "0")  # the queue read again at the press


def test_home_draft_passes_the_owners_line_and_answer_as_the_card_does():
    ns, calls = _home_bot([_q("lee", 2, queued=ASKED)])
    assert ns["_home_draft"]("response-lee")["said"] == "Choose an answer first."
    assert ns["_home_draft"]("response-lee", "", "Renew for ten years")["said"] == \
        "That answer is not offered any more. Nothing was done."
    assert calls["turns"] == []
    assert ns["_home_draft"]("response-lee", "", "Renew for three years")["ok"]
    assert 'The owner picked this answer on the card: "Renew for three years".' in calls["turns"][0][3]
    ns, calls = _home_bot([_q("pat", 2)])
    ns["_home_draft"]("response-pat", "Yes, Thursday works.")
    assert 'The owner wrote this line on the card: "Yes, Thursday works."' in calls["turns"][0][3]


def test_home_draft_starts_nothing_where_the_reply_is_handled_or_already_being_written():
    ns, calls = _home_bot([])
    assert ns["_home_draft"]("response-ana") == {
        "ok": False, "said": "This no longer waits for a reply. Nothing was done."}
    confirms = {"d-live": {"status": "live"}}
    ns, calls = _home_bot([_q("ana", 3, queued={"outcome": "drafted", "draft_id": "d-live"})], confirms=confirms)
    assert ns["_home_draft"]("response-ana") == {"ok": False, "said": "Its Send card is already in this DM."}
    ns, calls = _home_bot([_q("ben", 3)])
    assert ns["_home_draft"]("response-ben")["ok"]
    assert ns["_home_draft"]("response-ben") == {"ok": False, "said": "This reply is being written already."}
    assert len(calls["turns"]) == 1
    ns, calls = _home_bot(None)
    assert ns["_home_draft"]("response-ben")["said"].startswith("Tacitus could not read the list")


def test_home_draft_of_a_reply_whose_send_card_closed_brings_a_new_send_card():
    confirms = {"d-old": {"status": "retired"}}
    ns, calls = _home_bot([_q("kim", 3, queued={"outcome": "drafted", "draft_id": "d-old"})], confirms=confirms)
    got = ns["_home_draft"]("response-kim")
    assert got == {"ok": True, "said": "Its draft was ready. The new Send card is in this DM."}
    assert calls["send_cards"] == [("dm", None, "d-old", "outlook")] and calls["turns"] == []


def test_home_not_needed_is_the_replies_cards_not_needed_with_homes_note():
    row = _q("pat", 2, queued={"outcome": "drafted", "draft_id": "d-old"}, conversation_id="conv-pat")
    ns, calls = _home_bot([row], confirms={"d-old": {"status": "retired"}})
    got = ns["_home_not_needed"]("response-pat", "No reply needed: the owner's tap on Home")
    assert got["ok"] and _plain(got["said"]).startswith(":white_check_mark: Closed. No reply goes out. Draft deleted.")
    assert calls["deleted"] == ["d-old"]
    assert ("close", "--id", "response-pat", "--signal", "user_handled",
            "--note", "No reply needed: the owner's tap on Home") in calls["cli"]
    assert not calls["status"]                                   # no card to change
    ns, calls = _home_bot([])
    assert ns["_home_not_needed"]("response-pat", "x") == {
        "ok": False, "said": "This no longer waits for a reply. Nothing was done."}


def test_homes_hooks_are_set_on_each_web_home():
    hook = BOT_SRC[BOT_SRC.index("_HOME_TURNS: dict = {}"):BOT_SRC.index("def _toggle_quoted(")]
    for name in ("draft", "not_needed", "outlook"):
        assert f"_home_of.hooks.{name} = _home_{name}" in hook
    assert "home_screen.Home(_web)" in BOT_SRC[:BOT_SRC.index("_HOME_TURNS: dict = {}")]
