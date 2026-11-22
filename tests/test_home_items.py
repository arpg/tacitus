"""A Home item opened in place (preprocessor/home_items.py, docs/webchat.md,
Home items): the quick briefing and the choices of each kind, kept mail,
the refs Home lists, what each choice runs (the Replies card's draft turn
and Not needed, the web chat's Mark done and Needs me, the ledger's
snooze), the undo window, a row that changed between the read and the
choice or the write, the routes and the relay's rule for buttons, and the
deeper briefing's hook. Made-up data only (tests/home_fixtures.py)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))

import home_fixtures as F  # noqa: E402
import home_items as I  # noqa: E402
import home_screen as H  # noqa: E402
import ledger  # noqa: E402
import web_thread_state as wts  # noqa: E402
from plain_words import JARGON  # noqa: E402

TODAY = datetime.now(F.ZONE).date()
SECRET_WHO = "Quinn Hollow"
SECRET_SUBJECT = "Confidential grant numbers"


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "—" not in text and "–" not in text, text


def words_of(it: dict) -> list[str]:
    b = it["brief"]
    return [b["what"], b["asked"], b["due"], b["next"], *b["standing"], it["title"]] + \
        [c["label"] for c in it["choices"]]


def ids(it: dict) -> list[str]:
    return [c["id"] for c in it["choices"]]


@pytest.fixture(autouse=True)
def no_deeper_module(monkeypatch):
    """The deeper briefing's module (preprocessor/home_deeper.py) ships on
    its own: each test reads as if it were not there, unless it puts a
    stand-in in its place."""
    monkeypatch.setattr(I, "_deeper_mod", lambda: None)


# ── a stand-in front end, ledger and bot ────────────────────────────

class FakeStore:
    def __init__(self):
        self.meta: dict = {}
        self.messages: dict = {}
        self.channels = {"main": {"id": "main"}, "dm": {"id": "dm"}, "room-lab": {"id": "room-lab"}}

    def home_get(self, name):
        return json.loads(json.dumps(self.meta.get(name) or {}))

    def home_set(self, name, value):
        self.meta[name] = json.loads(json.dumps(value))

    def message(self, channel, mid):
        return self.messages.get((channel, mid))

    def channel(self, cid):
        return self.channels.get(cid)


class FakeFE:
    owner_id = "owner"
    main_channel = "main"

    def __init__(self, store=None):
        self.store = store or FakeStore()
        self.cfg = {}
        self.changed = 0
        self.sent: list = []
        self.dispatched: list = []

    def home_changed(self):
        self.changed += 1

    def dm_channel(self, user):
        return "dm"

    def rooms_on(self):
        return True

    def web_message(self, channel, text, thread_key, nonce=None):
        self.sent.append((channel, text, thread_key))
        row = {"channel": channel, "id": f"w{len(self.sent)}", "thread_key": f"t{len(self.sent)}"}
        return row, lambda: self.dispatched.append(row["id"])


class Ledger:
    """ledger.py and the other CLIs web_thread_state runs, on a record."""

    def __init__(self, monkeypatch):
        self.calls: list = []
        monkeypatch.setattr(wts, "RUN", self)

    def __call__(self, script, *args):
        self.calls.append((script, *args))
        if script == "ledger.py" and args[0] == "close":
            return {"ok": True, "action": "closed", "_exit": 0}
        if script == "ledger.py" and args[0] == "confirm":
            return {"ok": True, "action": "closed", "_exit": 0}
        if script == "ledger.py" and args[0] == "update":
            got = dict(zip(args[1::2], args[2::2]))
            status = got.get("--status", "open")
            entry = {"id": got.get("--id"), "status": "open" if status == "waiting" else status,
                     "type": "waiting" if status == "waiting" else "task"}
            return {"ok": True, "action": "updated", "entry": entry, "_exit": 0}
        return {"ok": True, "_exit": 0}

    def verbs(self):
        return [(c[0], c[1]) for c in self.calls]


class Bot:
    """The bot's hooks (ea-chat-bot.py's _home_draft, _home_not_needed,
    _home_outlook), recorded."""

    def __init__(self, fe):
        self.fe = fe
        self.drafts: list = []
        self.closed: list = []

    def draft(self, rid, words, answer):
        self.drafts.append((rid, words, answer))
        self.fe.store.messages[("dm", "p1")] = {"channel": "dm", "id": "p1", "thread_key": "p1"}
        return {"ok": True, "channel": "dm", "id": "p1",
                "said": "Tacitus is writing the reply. Its Send card comes to Action requests."}

    def not_needed(self, rid, note):
        self.closed.append((rid, note))
        return {"ok": True, "said": "Closed. No reply goes out."}

    def outlook(self, conv):
        return f"https://outlook.example.com/mail/{conv}"


def home(state="normal", *, src=None, clock=None, fe=None, hooks=True):
    """A Home on made-up sources; `src(tz)` makes them, `clock` is a list
    holding now."""
    fe = fe or FakeFE()
    clock = clock or [F.at(13, 10)]
    h = H.Home(fe, clock=lambda: clock[0], sources=src or (lambda tz: F.sources(state)),
               background=False)
    bot = Bot(fe)
    if hooks:
        h.hooks.draft, h.hooks.not_needed, h.hooks.outlook = bot.draft, bot.not_needed, bot.outlook
    return fe, h, bot, clock


def decision_sources(choices=("Yes, I can join", "No, not this term")):
    src = F.sources()
    src.replies[1]["queued"] = {"outcome": "decision", "note": "Do you sign it as the lab's lead?",
                                "choices": list(choices)}
    return src


# ── the refs Home lists ─────────────────────────────────────────────

def test_every_item_home_lists_has_a_ref_and_nothing_of_the_bots_own():
    for state in F.STATES:
        p = F.payload(state)
        for x in p["left"] + p["held"] + p["done"]:
            assert x["ref"] == x["id"]
            assert not [k for k in x if k.startswith("_")] and "mins" not in x
            assert I.item(F.sources(state), x["ref"]) is not None, x["ref"]


def test_a_ref_home_does_not_list_reads_none():
    assert I.item(F.sources(), "reply:response-nope") is None
    assert I.item(F.sources(), "") is None


# ── the quick briefing and the choices, by kind ─────────────────────

def test_a_reply_owed_says_who_asked_what_and_how_long_it_waited():
    it = I.item(F.sources(), "reply:response-r2")
    assert it["kind"] == "reply" and it["kept"] == ""
    assert it["brief"]["what"] == "A reply you owe Alex Kim."  # the title names the subject
    assert it["brief"]["asked"] == "Sign the data access form and send it back."
    assert it["brief"]["standing"] == ["It has waited 4 days for your reply.", "You last wrote on it 5 days ago."]
    # signing a form is work, not only a reply (mail_completion, the form the ask names)
    assert it["brief"]["next"] == "This needs a form filled in, not just a reply. Ask Tacitus to start it."
    assert ids(it) == ["draft", "say", "not_needed", "later"]
    assert next(c for c in it["choices"] if c["id"] == "say")["input"] == "text"
    assert it["ask"] == 'About the reply to "Data access form": '
    assert it["deeper"] == {"state": "unavailable"}
    for w in words_of(it):
        plain(w)


def test_what_tacitus_would_do_next_is_chosen_from_the_record_never_made_up():
    src = F.sources()
    it = I.item(src, "reply:response-r1")  # they ask to meet, and offered no times
    assert it["brief"]["next"] == "Draft a reply with two or three times from your calendar."
    src.rows["response-r1"]["thread_brief"].update(scheduling="proposes-times",
                                                   offered_times=["Mon 10:00", "Tue 14:00"])
    it = I.item(src, "reply:response-r1")
    assert it["brief"]["next"] == ("Draft a reply that takes one of the times they offered, "
                                   "if your calendar is free.")
    assert "They offered 2 times to meet." in it["brief"]["standing"]
    src.replies[0]["queued"] = {"outcome": "drafted", "draft_id": "d1"}
    assert I.item(src, "reply:response-r1")["brief"]["next"] == \
        "Bring its draft back to Action requests for your Send."
    for state in F.STATES:  # no line says yes for the owner
        for x in F.payload(state)["left"]:
            assert "yes" not in I.item(F.sources(state), x["ref"])["brief"]["next"].lower()


def test_a_to_do_says_its_date_and_last_step_and_offers_start_done_waiting_later():
    it = I.item(F.sources(), "item:deadline-w1")
    assert it["brief"]["what"] == "A deadline, from Jo Rivera."
    assert it["brief"]["asked"] == "Send your review of the four workshop papers."
    assert it["brief"]["due"] == "Due tomorrow."
    assert ids(it) == ["start", "done", "waiting", "later"]
    task = I.item(F.sources(), "item:task-s2")
    assert task["brief"]["standing"] == ["Last step today: asked the committee for its slide template."]
    assert task["brief"]["asked"] == "You said you would send them this week."
    src = F.sources()
    # already waits on someone, and its date passed (else it is off the list): no Waiting on someone
    src.rows["task-s2"].update(type="waiting", deadline=(TODAY - timedelta(days=2)).isoformat())
    assert ids(I.item(src, "item:task-s2")) == ["start", "done", "later"]
    src.rows["task-s2"]["close_proposal"] = {"why": "The slides went out on Friday"}
    it = I.item(src, "item:task-s2")
    assert "Tacitus thinks it is done: The slides went out on Friday" in it["brief"]["standing"]
    assert it["brief"]["next"] == "Close it once you say it is done."


def test_an_overdue_date_says_so():
    src = F.sources("overdue")
    src.rows["deadline-w1"]["deadline"] = (TODAY - timedelta(days=1)).isoformat()
    src.rows["deadline-w1"]["thread_brief"]["deadline"] = ""
    assert I.item(src, "item:deadline-w1")["brief"]["due"] == "Overdue since yesterday."


def test_a_decision_shows_the_answers_the_replies_card_offers():
    src = decision_sources()
    it = I.item(src, "reply:response-r2")
    assert it["kind"] == "question" and it["title"] == "Do you sign it as the lab's lead?"
    assert ids(it) == ["answer:1", "answer:2", "answer", "not_needed", "later"]
    assert [c["label"] for c in it["choices"][:3]] == ["Yes, I can join", "No, not this term", "Other answer"]
    assert it["brief"]["what"] == 'Tacitus needs your answer before it writes to Alex Kim about "Data access form".'
    assert it["brief"]["next"] == "Once you answer, Tacitus writes the reply. You check it before it goes."
    src = decision_sources(choices=())  # a question with no answers to pick: Answer
    assert ids(I.item(src, "reply:response-r2")) == ["answer", "not_needed", "later"]


def test_a_question_with_no_reply_behind_it_is_answered_in_a_conversation():
    it = I.item(F.sources("unchecked"), "item:task-q1")
    assert it["kind"] == "question" and ids(it) == ["answer", "later"]
    assert it["brief"]["what"] == "Committee seat (Sam Rivera)."
    assert it["brief"]["asked"] == ""  # the question is the title


def test_a_request_opens_in_action_requests_and_carries_no_token():
    it = I.item(F.sources(), "request:w41")
    assert ids(it) == ["open"] and it["goto"] == "#/dm/tw41/w41"
    assert it["brief"]["what"] == "Send email: Re: Lab budget, to Pat Example."
    assert it["brief"]["due"] == "Due tomorrow."
    assert it["brief"]["next"] == "Nothing goes out until you tap Send in Action requests."
    raw = json.dumps(it)
    assert "token" not in raw and "confirm" not in raw
    ev = I.item(F.sources(), "request:w42")
    assert ev["brief"]["next"] == "Your calendar does not change until you tap its button in Action requests."


def test_so_far_today_opens_too():
    closed = I.item(F.sources(), "closed:row-c1")
    assert closed["brief"]["what"] == "Tacitus closed it at 7:15."
    assert closed["brief"]["standing"] == ["Why: The permit date passed, so it lapsed."]
    assert ids(closed) == ["reopen"]
    assert ids(I.item(F.sources(), "drafted:w41")) == ["open"]
    assert I.item(F.sources(), "answered:w40")["brief"]["what"] == "You answered it at 10:40."


def test_each_briefing_word_is_plain_and_bounded():
    for state in F.STATES:
        p = F.payload(state)
        for x in p["left"] + p["held"] + p["done"]:
            it = I.item(F.sources(state), x["ref"])
            for w in words_of(it):
                plain(w)
                assert len(w) <= I.FIELD_MAX + 40, w
            assert len(it["brief"]["standing"]) <= I.STANDING_MAX


# ── kept mail ───────────────────────────────────────────────────────

def kept_sources(level: str):
    src = F.sources()
    src.replies[1].update(keep_level=level, from_name=SECRET_WHO, subject=SECRET_SUBJECT,
                          ask="Wire the funds today", since="2026-11-17T15:00:00Z")
    src.rows["response-r2"].update(keep_level=level, from_name=SECRET_WHO, notes="Funds and numbers",
                                   last_action="2026-11-18: read the numbers")
    src.rows["response-r2"]["thread_brief"].update(ask="Wire the funds today", subject=SECRET_SUBJECT)
    return src


@pytest.mark.parametrize("level", ["local", "facts"])
def test_a_kept_item_shows_keep_gates_line_alone_and_opens_in_outlook(level):
    it = I.item(kept_sources(level), "reply:response-r2")
    assert it["kept"] == level
    assert it["brief"] == {"what": "", "asked": "", "due": "", "standing": [], "next": ""}
    assert ids(it) == ["outlook", "done", "later"] and "ask" not in it and it["detail"] == ""
    raw = json.dumps({k: v for k, v in it.items() if k != "title"})
    for secret in ("Wire the funds", "Funds and numbers", "read the numbers", SECRET_WHO, SECRET_SUBJECT):
        assert secret not in raw
    if level == "local":
        assert SECRET_WHO not in it["title"] and SECRET_SUBJECT not in it["title"]
    else:  # facts: the sender and subject as the headers give them, and no more
        assert SECRET_WHO in it["title"] and "Wire the funds" not in it["title"]


def test_a_kept_item_takes_done_and_later_and_nothing_that_reads_it(monkeypatch):
    led = Ledger(monkeypatch)
    _fe, h, bot, clock = home(src=lambda tz: kept_sources("local"))
    for choice in ("draft", "say", "not_needed", "start", "waiting"):
        out, _after = h.act("reply:response-r2", choice, "tomorrow")
        assert out == {"ok": False, "said": I.NO_CHOICE, "item": out["item"]}
    assert bot.drafts == [] and led.calls == []
    out, _after = h.act("reply:response-r2", "outlook")
    assert out["ok"] and out["goto"] == "https://outlook.example.com/mail/conv-r2"
    out, _after = h.act("reply:response-r2", "done", "", h.item("reply:response-r2")["version"])
    assert out["said"] == "Done. It is off your list." and out["undo"]
    raw = json.dumps(out) + json.dumps(h.item("reply:response-r2"))
    assert SECRET_WHO not in raw and SECRET_SUBJECT not in raw and "Wire the funds" not in raw
    assert h.undo(out["undo"])["ok"] and led.calls == []  # undone in time: nothing written
    out, _after = h.act("reply:response-r2", "later", "tomorrow")
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    assert led.calls[0][:2] == ("ledger.py", "update") and "--snooze-until" in led.calls[0]
    _fe, h, bot, _c = home(src=lambda tz: kept_sources("local"))
    out, _after = h.act("reply:response-r2", "done", "", "0" * 16)  # read before it changed
    assert out["ok"] is False and out["said"] == I.CHANGED


def test_a_kept_post_item_opens_its_own_link():
    _fe, h, _bot, _c = home("kept")
    out, _after = h.act("item:row-k1", "outlook")
    assert out == {"ok": True, "said": "Opening it in Outlook.", "goto": "https://outlook.example.com/mail/k1"}


def test_open_in_outlook_without_a_link_says_so_plainly():
    _fe, h, _bot, _c = home(src=lambda tz: kept_sources("local"), hooks=False)
    out, _after = h.act("reply:response-r2", "outlook")
    assert out == {"ok": False, "said": "Tacitus could not find its link. Look for it in Outlook."}


# ── the immediate choices ───────────────────────────────────────────

def test_draft_a_reply_starts_the_replies_draft_turn_and_goes_where_send_comes():
    fe, h, bot, _c = home()
    out, after = h.act("reply:response-r2", "draft")
    assert bot.drafts == [("response-r2", "", "")] and after is None
    assert out == {"ok": True, "said": "Tacitus is writing the reply. Its Send card comes to Action requests.",
                   "goto": "#/dm/p1/p1"}


def test_tell_tacitus_what_to_say_passes_the_owners_line_as_his_instruction():
    _fe, h, bot, _c = home()
    out, _after = h.act("reply:response-r2", "say", "  Yes, I will sign it\nby Friday; rm -rf /  ")
    assert out["ok"] and bot.drafts == [("response-r2", "Yes, I will sign it by Friday; rm -rf /", "")]
    out, _after = h.act("reply:response-r2", "say", "   ")
    assert out == {"ok": False, "said": "Write what the reply must say first."}
    long = "word " * 200
    h.act("reply:response-r2", "say", long)
    assert len(bot.drafts[-1][1]) <= I.TEXT_MAX


def test_an_answer_tap_drafts_the_reply_that_says_it():
    _fe, h, bot, _c = home(src=lambda tz: decision_sources())
    out, _after = h.act("reply:response-r2", "answer:2")
    assert out["ok"] and bot.drafts == [("response-r2", "", "No, not this term")]
    out, _after = h.act("reply:response-r2", "answer", "Only if the dean signs too")
    assert bot.drafts[-1] == ("response-r2", "Only if the dean signs too", "")
    out, _after = h.act("reply:response-r2", "answer", "")
    assert out == {"ok": False, "said": "Write your answer first."}


def test_with_no_bot_a_draft_is_refused_plainly():
    _fe, h, _bot, _c = home(hooks=False)
    out, _after = h.act("reply:response-r2", "draft")
    assert out == {"ok": False, "said": "Tacitus cannot start a draft from here."}


def test_start_it_opens_a_conversation_with_the_item_sent(monkeypatch):
    monkeypatch.setattr(I, "_project_of", lambda lid, conv: "")
    fe, h, _bot, _c = home()
    out, after = h.act("item:deadline-w1", "start")
    assert out == {"ok": True, "said": "Opened a conversation with Tacitus about it.", "goto": "#/main/t1"}
    channel, text, thread = fe.sent[0]
    assert channel == "main" and thread is None
    assert text.splitlines() == ["Let's start this: Send the workshop review", "A deadline, from Jo Rivera.",
                                 "Send your review of the four workshop papers.", "Due tomorrow.",
                                 "(item `deadline-w1`)"]
    after()
    assert fe.dispatched == ["w1"]


def test_start_it_goes_to_the_items_project_room(monkeypatch):
    import ea_channels
    monkeypatch.setattr(I, "_project_of", lambda lid, conv: "lab" if lid == "deadline-w1" else "")
    monkeypatch.setattr(ea_channels, "for_project", lambda pid: ["room-gone", "room-lab"] if pid == "lab" else [])
    fe, h, _bot, _c = home()
    out, _after = h.act("item:deadline-w1", "start")
    assert fe.sent[0][0] == "room-lab" and out["goto"] == "#/room-lab/t1"
    fe.rooms_on = lambda: False  # no rooms in this mode: Ask Tacitus
    h.act("item:task-s2", "start")
    assert fe.sent[1][0] == "main"


def test_an_answer_to_a_question_with_no_reply_is_the_owners_message(monkeypatch):
    monkeypatch.setattr(I, "_project_of", lambda lid, conv: "")
    fe, h, _bot, _c = home("unchecked")
    out, after = h.act("item:task-q1", "answer", "Yes, for one term")
    assert out["ok"] and after is not None
    assert fe.sent[0][1].splitlines() == ['My answer to "Join the committee this term? Yes or no.": '
                                          'Yes, for one term', "(item `task-q1`)"]


def test_not_done_yet_reopens_a_row_closed_today(monkeypatch):
    led = Ledger(monkeypatch)
    monkeypatch.setattr(I._wts(), "RUN", led)
    _fe, h, _bot, _c = home()
    out, _after = h.act("closed:row-c1", "reopen")
    assert out == {"ok": True, "said": "Opened again. It is back on your list."}
    assert ("ledger.py", "update") in led.verbs() and ("item_feedback.py", "--id") in led.verbs()


# ── the choices that wait for an undo ───────────────────────────────

def test_done_acts_at_once_waits_for_undo_then_closes_as_the_web_chats_mark_done(monkeypatch):
    led = Ledger(monkeypatch)
    fe, h, _bot, clock = home()
    out, after = h.act("item:deadline-w1", "done")
    assert after is None and out["ok"] and out["said"] == "Done. It is off your list."
    assert out["undo_until"] == clock[0] + I.UNDO_S and out["undo"]
    assert "item:deadline-w1" not in [x["ref"] for x in h.payload()["left"]]  # off Home at once
    assert led.calls == [] and fe.changed >= 1  # nothing written inside the window
    pending = h.item("item:deadline-w1")
    assert pending["choices"] == [] and pending["pending"]["undo"] == out["undo"]
    assert h.acts.commit_due() == []
    clock[0] += I.UNDO_S
    got = h.tick() and h.acts.commit_due()
    assert got == []  # the tick wrote it
    assert led.calls[0][:3] == ("ledger.py", "close", "--id") and led.calls[0][3] == "deadline-w1"
    assert wts.WEB_DONE_PHRASE in led.calls[0]
    # a closure the owner had to state is a closure the pipeline missed
    assert led.calls[1][0] == "log_missed_closure.py" and "deadline-w1" in led.calls[1]


def test_done_on_a_row_tacitus_proposed_confirms_it_and_logs_no_miss(monkeypatch):
    led = Ledger(monkeypatch)
    src = F.sources()
    src.rows["deadline-w1"]["close_proposal"] = {"why": "The reviews went in"}
    _fe, h, _bot, clock = home(src=lambda tz: src)
    h.act("item:deadline-w1", "done")
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    assert led.verbs() == [("ledger.py", "confirm")]


def test_undo_in_time_takes_it_back_exactly_and_late_undo_is_refused(monkeypatch):
    led = Ledger(monkeypatch)
    _fe, h, _bot, clock = home()
    before = I.item(F.sources(), "item:task-s2")
    out, _a = h.act("item:task-s2", "later", "monday")
    clock[0] += I.UNDO_S - 1
    assert h.undo(out["undo"]) == {"ok": True, "said": "Undone. It is back on your list."}
    assert led.calls == [] and h.item("item:task-s2") == before
    assert "item:task-s2" in [x["ref"] for x in h.payload()["left"]]
    clock[0] += I.UNDO_S * 2
    assert h.acts.commit_due() == []  # nothing left to write
    out, _a = h.act("item:task-s2", "waiting")
    clock[0] += I.UNDO_S
    assert h.undo(out["undo"]) == {"ok": False, "said": I.TOO_LATE}
    h.acts.commit_due()
    assert h.undo(out["undo"]) == {"ok": False, "said": I.TOO_LATE}
    assert h.undo("not-a-token") == {"ok": False, "said": I.TOO_LATE}


def test_not_needed_runs_the_replies_cards_not_needed_and_logs_no_miss(monkeypatch):
    led = Ledger(monkeypatch)
    _fe, h, bot, clock = home()
    out, _a = h.act("reply:response-r1", "not_needed")
    assert out["said"] == "Not needed. No reply goes out."
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    assert bot.closed == [("response-r1", I.NOT_NEEDED_NOTE)]
    assert ledger.says_no_reply(I.NOT_NEEDED_NOTE)  # a later row on the thread carries it
    assert not [c for c in led.calls if c[0] == "log_missed_closure.py"]


@pytest.mark.parametrize("arg,day", [("tomorrow", 1), ("monday", None), ("date", None)])
def test_later_snoozes_the_row_to_the_day_chosen(monkeypatch, arg, day):
    led = Ledger(monkeypatch)
    _fe, h, _bot, clock = home()
    want = TODAY + timedelta(days=day) if day else (
        TODAY + timedelta(days=(7 - TODAY.weekday()) or 7) if arg == "monday" else TODAY + timedelta(days=9))
    out, _a = h.act("reply:response-r2", "later", want.isoformat() if arg == "date" else arg)
    assert out["ok"] and out["said"].startswith("Later. It comes back ")
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    (call,) = led.calls
    got = dict(zip(call[2::2], call[3::2]))
    assert call[:2] == ("ledger.py", "update") and got["--status"] == "snoozed"
    assert got["--snooze-until"] == want.isoformat() and got["--id"] == "response-r2"


@pytest.mark.parametrize("arg", ["", "next week", (TODAY - timedelta(days=1)).isoformat(), TODAY.isoformat(),
                                 (TODAY + timedelta(days=400)).isoformat()])
def test_later_refuses_a_day_it_cannot_take(arg):
    _fe, h, _bot, _c = home()
    out, _a = h.act("reply:response-r2", "later", arg)
    assert out == {"ok": False, "said": "Pick a day: tomorrow, Monday or a date within a year."}


def test_later_offers_tomorrow_monday_and_a_date():
    sunday = TODAY + timedelta(days=(6 - TODAY.weekday()))
    wednesday = sunday + timedelta(days=3)
    assert [o["arg"] for o in I.later_choice(sunday)["options"]] == ["tomorrow", "date"]  # Monday is tomorrow
    opts = I.later_choice(wednesday)["options"]
    assert [o["arg"] for o in opts] == ["tomorrow", "monday", "date"]
    assert opts[1]["day"] == (wednesday + timedelta(days=5)).isoformat()


def test_waiting_marks_the_row_as_owed_by_others(monkeypatch):
    led = Ledger(monkeypatch)
    _fe, h, _bot, clock = home()
    out, _a = h.act("item:task-s2", "waiting")
    assert out["said"] == "Waiting on someone else. It is off your list."
    clock[0] += I.UNDO_S
    assert h.acts.commit_due() == [{"op": "waiting", "id": "task-s2", "ok": True}]
    got = dict(zip(led.calls[0][2::2], led.calls[0][3::2]))
    assert got["--status"] == "waiting"
    assert got["--follow-up-after"] == (TODAY + timedelta(days=ledger.WAITING_DAYS)).isoformat()


def test_a_choice_that_waits_survives_a_restart(monkeypatch):
    led = Ledger(monkeypatch)
    store = FakeStore()
    _fe, h, _bot, clock = home(fe=FakeFE(store))
    h.act("item:task-s2", "done")
    _fe2, h2, _bot2, _c2 = home(fe=FakeFE(store), clock=[clock[0] + I.UNDO_S + 60])
    assert "item:task-s2" not in [x["ref"] for x in h2.payload()["left"]]
    h2.tick()
    assert led.calls[0][:4] == ("ledger.py", "close", "--id", "task-s2")


# ── stale reads ─────────────────────────────────────────────────────

def test_an_item_that_changed_since_it_was_read_is_refused_with_the_item_as_it_is(monkeypatch):
    led = Ledger(monkeypatch)
    state = {"src": F.sources()}
    _fe, h, bot, _c = home(src=lambda tz: state["src"])
    seen = h.item("reply:response-r2")
    # a new message came: the thread's record moved
    state["src"] = F.sources()
    state["src"].rows["response-r2"]["thread_brief"].update(latest="2026-11-22T09:00:00Z", message_count=4)
    out, _a = h.act("reply:response-r2", "draft", "", seen["version"])
    assert out["ok"] is False and out["said"] == I.CHANGED
    assert out["item"]["version"] != seen["version"] and bot.drafts == []
    out, _a = h.act("reply:response-r2", "draft", "", out["item"]["version"])
    assert out["ok"] and bot.drafts == [("response-r2", "", "")]
    assert led.calls == []


def test_the_clock_alone_never_moves_a_version():
    src = F.sources()
    it = I.item(src, "reply:response-r2")
    src.replies[1]["age_days"] = 5  # a day later, nothing else
    later = I.item(src, "reply:response-r2")
    assert later["brief"]["standing"][0] == "It has waited 5 days for your reply."
    assert (later["version"], later["row_version"]) == (it["version"], it["row_version"])


def test_the_row_version_is_the_rows_own_and_moves_with_its_thread():
    src = F.sources()
    row = src.rows["response-r2"]
    it = I.item(src, "reply:response-r2")
    assert it["row_version"] == I.row_version(row) and len(it["row_version"]) == 16
    same = json.loads(json.dumps(row))
    same["thread_brief"]["updated"] = "2026-11-22T16:00:01Z"  # a refresh with the same facts
    same["owner_said"] = [{"said": "Later", "at": "2026-11-20"}]  # read from other rows, not this one
    assert I.row_version(same) == it["row_version"]
    for change in ({"latest": "2026-11-22T09:00:00Z", "message_count": 4}, {"ask": "Sign it by noon"}):
        moved = json.loads(json.dumps(row))
        moved["thread_brief"].update(change)
        assert I.row_version(moved) != it["row_version"]
    moved = json.loads(json.dumps(row))
    moved["reply_queue"] = {"outcome": "drafted", "draft_id": "d1"}
    assert I.row_version(moved) != it["row_version"]
    assert I.item(F.sources(), "request:w42")["row_version"] == ""  # no row behind it


def test_a_refreshed_record_with_the_same_facts_keeps_the_version():
    src = F.sources()
    v = I.item(src, "reply:response-r2")["version"]
    src.rows["response-r2"]["thread_brief"]["updated"] = "2026-11-22T16:00:01Z"
    src.rows["response-r2"]["thread_brief"]["own_checked"] = True
    assert I.item(src, "reply:response-r2")["version"] == v
    src.rows["response-r2"]["deadline"] = (TODAY + timedelta(days=3)).isoformat()
    assert I.item(src, "reply:response-r2")["version"] != v


def test_a_choice_the_item_no_longer_offers_and_a_gone_item_are_refused():
    src = F.sources()
    _fe, h, bot, _c = home(src=lambda tz: src)
    out, _a = h.act("reply:response-r2", "done")  # a reply has no Done
    assert out["ok"] is False and out["said"] == I.NO_CHOICE and out["item"]["ref"] == "reply:response-r2"
    src.replies.pop()  # the reply went out elsewhere
    out, _a = h.act("reply:response-r2", "draft")
    assert out == {"ok": False, "said": I.GONE} and bot.drafts == []


def test_a_row_that_changed_inside_the_undo_window_is_not_written(monkeypatch):
    led = Ledger(monkeypatch)
    state = {"src": F.sources()}
    _fe, h, _bot, clock = home(src=lambda tz: state["src"])
    h.act("reply:response-r2", "not_needed")
    state["src"] = F.sources()
    state["src"].rows["response-r2"]["thread_brief"].update(latest="2026-11-22T13:20:00Z", message_count=4)
    clock[0] += I.UNDO_S
    assert h.acts.commit_due() == [{"op": "not_needed", "id": "response-r2", "ok": False, "why": "changed"}]
    assert led.calls == [] and "reply:response-r2" in [x["ref"] for x in h.payload()["left"]]


def test_a_row_closed_elsewhere_inside_the_window_is_not_written(monkeypatch):
    led = Ledger(monkeypatch)
    state = {"src": F.sources()}
    _fe, h, _bot, clock = home(src=lambda tz: state["src"])
    h.act("item:task-s2", "done")
    state["src"] = F.sources()
    state["src"].briefing["today_top"] = state["src"].briefing["today_top"][:1]
    clock[0] += I.UNDO_S
    assert h.acts.commit_due()[0]["why"] == "gone" and led.calls == []


def test_a_second_choice_on_an_item_that_waits_is_refused():
    _fe, h, _bot, _c = home()
    h.act("item:task-s2", "done")
    out, _a = h.act("item:task-s2", "later", "tomorrow")
    assert out == {"ok": False, "said": "You already chose for this one. Undo it first to choose again."}


# ── the deeper briefing's hook ──────────────────────────────────────

class Deeper:
    """A stand-in for preprocessor/home_deeper.py, the deeper briefing's module."""

    def __init__(self, step_answer):
        self.calls: list = []
        self.step_answer = step_answer

    def state(self, fe, ref, item, row=None):
        self.calls.append(("state", ref, item["version"], (row or {}).get("id")))
        assert "deeper" not in item and item["brief"]["what"]
        return {"state": "ready", "text": "More.", "steps": [{"id": "s1", "label": "Ask Jo for a week"},
                                                            {"id": "continue", "label": "Continue in a conversation"}]}

    def start(self, fe, ref, item, row=None):
        self.calls.append(("start", ref))
        return {"ok": True, "state": "working"}

    def step(self, fe, ref, step_id, item, row=None, arg=""):
        self.calls.append(("step", ref, step_id, arg))
        return dict(self.step_answer)


def test_with_no_deeper_module_the_block_reads_unavailable_and_a_step_is_refused():
    it = I.item(F.sources(), "item:deadline-w1")
    assert it["deeper"] == {"state": "unavailable"}
    _fe, h, _bot, _c = home()
    assert h.deeper("item:deadline-w1") == {"ok": False, "state": "unavailable"}
    assert h.deeper("item:nope") is None
    out, _a = h.act("item:deadline-w1", "step:s1")
    assert out["ok"] is False and out["said"] == I.NO_CHOICE


def test_the_deeper_steps_stay_after_the_code_built_choices_and_a_step_may_run_one(monkeypatch):
    led = Ledger(monkeypatch)
    deeper = Deeper({"choice": "later", "arg": (TODAY + timedelta(days=4)).isoformat()})
    monkeypatch.setattr(I, "_deeper_mod", lambda: deeper)
    _fe, h, _bot, clock = home()
    it = h.item("item:deadline-w1")
    assert ids(it) == ["start", "done", "waiting", "later"]  # the code-built ones, in their order
    assert [s["id"] for s in it["deeper"]["steps"]] == ["s1", "continue"]
    assert deeper.calls[-1] == ("state", "item:deadline-w1", it["version"], "deadline-w1")
    assert h.deeper("item:deadline-w1") == {"ok": True, "state": "working"}
    out, _a = h.act("item:deadline-w1", "step:s1")  # it runs as the owner's Later, with its undo
    assert out["ok"] and out["undo"] and deeper.calls[-1] == ("step", "item:deadline-w1", "s1", "")
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    assert dict(zip(led.calls[0][2::2], led.calls[0][3::2]))["--snooze-until"] == \
        (TODAY + timedelta(days=4)).isoformat()
    out, _a = h.act("item:task-s2", "step:gone")
    assert out["ok"] is False and out["said"] == I.NO_CHOICE


def test_a_deeper_step_that_drafts_with_words_is_tell_tacitus_what_to_say(monkeypatch):
    deeper = Deeper({"choice": "draft", "arg": "Thank them and say Thursday works"})
    monkeypatch.setattr(I, "_deeper_mod", lambda: deeper)
    _fe, h, bot, _c = home()
    out, _a = h.act("reply:response-r2", "step:s1")
    assert out["ok"] and bot.drafts == [("response-r2", "Thank them and say Thursday works", "")]
    deeper.step_answer = {"choice": "done"}  # a reply has no Done: refused
    out, _a = h.act("reply:response-r2", "step:s1")
    assert out["ok"] is False and out["said"] == I.NO_CHOICE
    deeper.step_answer = {"ok": True, "said": "Opened a conversation.", "goto": "#/main/t9", "extra": 1}
    out, _a = h.act("reply:response-r2", "step:continue")
    assert out == {"ok": True, "said": "Opened a conversation.", "goto": "#/main/t9"}


def test_a_failing_deeper_module_reads_unavailable(monkeypatch):
    class Broken:
        def state(self, *a, **k):
            raise RuntimeError("no model")
        start = step = state
    monkeypatch.setattr(I, "_deeper_mod", lambda: Broken())
    assert I.item(F.sources(), "item:deadline-w1")["deeper"] == {"state": "unavailable"}
    _fe, h, _bot, _c = home()
    assert h.deeper("item:deadline-w1") == {"ok": False, "said": I.DEEPER_FAILED}


# ── the routes ──────────────────────────────────────────────────────

from test_web_frontend import call, make, owner  # noqa: E402


class RouteHome:
    def __init__(self):
        self.calls = []

    def payload(self, tz="", checkin=False):
        return {}

    def tick(self):
        return {}

    def item(self, ref, tz=""):
        self.calls.append(("item", ref))
        return None if ref == "gone" else {"ref": ref, "choices": []}

    def act(self, ref, choice, arg="", version=""):
        self.calls.append(("act", ref, choice, arg, version))
        return {"ok": True, "said": "Done. It is off your list.", "undo": "tok"}, \
            (lambda: self.calls.append(("after",)))

    def undo(self, token):
        self.calls.append(("undo", token))
        return {"ok": True, "said": "Undone. It is back on your list."}

    def deeper(self, ref):
        self.calls.append(("deeper", ref))
        return None if ref == "gone" else {"ok": False, "state": "unavailable"}


@pytest.fixture
def served(tmp_path):
    f = make(tmp_path)
    f.home = RouteHome()
    f.start()
    yield f
    f.stop()


def test_the_item_routes(served):
    status, _h, raw = owner(served, "GET", "/api/home/item?ref=reply%3Aresponse-r2&tz=Europe%2FLisbon")
    assert status == 200 and json.loads(raw)["ref"] == "reply:response-r2"
    status, _h, raw = owner(served, "GET", "/api/home/item?ref=gone")
    assert status == 404 and json.loads(raw) == {"ok": False, "said": "This is not on your list any more."}
    assert owner(served, "GET", "/api/home/item")[0] == 400
    body = {"ref": "item:x", "choice": "done", "arg": "", "version": "v1"}
    status, _h, raw = owner(served, "POST", "/api/home/act", body)
    assert status == 200 and json.loads(raw)["undo"] == "tok"
    assert ("act", "item:x", "done", "", "v1") in served.home.calls
    status, _h, raw = owner(served, "POST", "/api/home/undo", {"token": "tok"})
    assert status == 200 and json.loads(raw)["ok"] is True
    status, _h, raw = owner(served, "POST", "/api/home/deeper", {"ref": "item:x"})
    assert status == 200 and json.loads(raw)["state"] == "unavailable"
    status, _h, raw = owner(served, "POST", "/api/home/deeper", {"ref": "gone"})
    assert status == 404 and json.loads(raw)["ok"] is False
    for bad in ({"ref": "item:x"}, {"choice": "done"}, {"ref": "item:x", "choice": "done", "arg": 3},
                {"ref": "x" * 500, "choice": "done"}):
        assert owner(served, "POST", "/api/home/act", bad)[0] == 400
    assert owner(served, "POST", "/api/home/undo", {})[0] == 400
    assert call(served, "GET", "/api/home/item?ref=item%3Ax")[0] == 401  # not the owner
    assert call(served, "POST", "/api/home/act", body)[0] in (401, 403)


def test_a_choice_follows_the_relays_rule_for_buttons(served):
    deny = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "deny"}
    allow = {"X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "allow"}
    body = {"ref": "item:x", "choice": "done"}
    assert owner(served, "GET", "/api/home/item?ref=item%3Ax", headers=deny)[0] == 200  # a read
    assert owner(served, "POST", "/api/home/act", body, deny)[0] == 403
    assert owner(served, "POST", "/api/home/undo", {"token": "tok"}, deny)[0] == 403
    assert not [c for c in served.home.calls if c[0] in ("act", "undo")]
    assert owner(served, "POST", "/api/home/act", body, allow)[0] == 200


def test_a_choice_that_breaks_says_so_in_plain_words(served):
    def broken(*a, **k):
        raise RuntimeError("the ledger is locked")
    served.home.act = served.home.undo = broken
    for path, body in (("/api/home/act", {"ref": "item:x", "choice": "done"}), ("/api/home/undo", {"token": "t"})):
        status, _h, raw = owner(served, "POST", path, body)
        assert status == 200 and json.loads(raw) == {"ok": False,
                                                     "said": "Tacitus could not do that. Try again in a minute."}


def test_a_last_step_is_shown_only_when_it_reads_as_one():
    src = F.sources()
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    shown = {f"{yesterday}: Sent the signed form to the office. More notes after.":
             "Last step yesterday: Sent the signed form to the office.",
             "Asked the chair for the room.": "Last step: Asked the chair for the room."}
    for raw, want in shown.items():
        assert I._last_step(raw, src) == want
    for raw in (f"{yesterday}: Pat asks for the table by Friday. (reconcile)",
                "Auto-resurrected: follow_up_after date reached",
                f"{yesterday}: draft id Zx9fakeDraft0000example7 created",
                f"{yesterday}: shipped (71381df, 0630881).", "INCIDENT: a false close.",
                f"{yesterday}: nudged again ({yesterday}); the note is newer.",
                f"{yesterday}: skipped for 3 days on the Replies card", "Wrote to the dean ('Looks good."):
        assert I._last_step(raw, src) == "", raw


def test_a_write_a_crash_cut_short_is_written_again_and_finds_the_row_closed(monkeypatch):
    led = Ledger(monkeypatch)
    store = FakeStore()
    _fe, h, _bot, clock = home(fe=FakeFE(store))
    h.act("item:task-s2", "done")
    (token, rec), = store.meta["acts"].items()
    rec["writing"] = clock[0] + I.UNDO_S  # the process stopped while it wrote
    store.meta["acts"] = {token: rec}
    clock[0] += I.UNDO_S + 1
    assert h.acts.commit_due() == []  # still being written, as far as anyone knows
    assert h.undo(token) == {"ok": False, "said": I.TOO_LATE}
    clock[0] += I.WRITE_STALE_S + 1
    assert h.acts.commit_due()[0]["ok"] and store.meta["acts"] == {}
    src = F.sources()
    src.briefing["today_top"] = src.briefing["today_top"][:1]  # closed by the cut-short write
    _fe2, h2, _bot2, _c2 = home(fe=FakeFE(store), src=lambda tz: src, clock=[clock[0]])
    store.meta["acts"] = {token: dict(rec, writing=0)}
    assert h2.acts.commit_due()[0]["why"] == "gone" and store.meta["acts"] == {}
    assert len([c for c in led.calls if c[1] == "close"]) == 1


# ── the ledger tells the briefing's writer of a row it wrote ────────

@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    monkeypatch.setattr(ledger, "DB_PATH", path)
    return path


def _verb(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["ledger.py", *argv])
    return ledger.main()


def test_add_and_update_tell_the_briefings_writer_of_an_open_row(ledger_db, monkeypatch, capsys):
    import types
    told = []
    monkeypatch.setitem(sys.modules, "home_briefing_queue",
                        types.SimpleNamespace(row_written=lambda rid, created: told.append((rid, created))))
    assert ledger.ROW_WRITTEN_HOOKS == ("home_briefing_queue",)
    assert _verb(monkeypatch, "add", "--id", "task-z1", "--type", "task", "--description",
                 "Order the poster tubes", "--deadline", "2026-12-04") == 0
    assert _verb(monkeypatch, "update", "--id", "task-z1", "--last-action", "asked for a quote") == 0
    assert told == [("task-z1", True), ("task-z1", False)]
    assert _verb(monkeypatch, "close", "--id", "task-z1", "--user-said", "done", "--skip-archive") == 0
    assert _verb(monkeypatch, "update", "--id", "task-z1", "--notes", "a note on a closed row") == 0
    assert _verb(monkeypatch, "add", "--id", "plug-1", "--type", "review", "--description", "x",
                 "--set", "source=plugin:reviews") in (0, 1)
    assert told == [("task-z1", True), ("task-z1", False)]  # no closed row, no plugin row


def test_a_hook_that_fails_never_fails_the_write(ledger_db, monkeypatch, capsys):
    import types

    def broken(rid, created):
        raise RuntimeError("the queue is full")
    monkeypatch.setitem(sys.modules, "home_briefing_queue", types.SimpleNamespace(row_written=broken))
    assert _verb(monkeypatch, "add", "--id", "task-z2", "--type", "task", "--description",
                 "Book the room", "--deadline", "2026-12-04") == 0
    out = capsys.readouterr()
    assert json.loads(out.out.strip().splitlines()[-1])["ok"] is True
    assert "was not told of task-z2" in out.err
    monkeypatch.setitem(sys.modules, "home_briefing_queue", types.SimpleNamespace())  # no hook in it
    assert _verb(monkeypatch, "update", "--id", "task-z2", "--last-action", "booked") == 0


# ── the owner in the third person ────────────────────────────────────

NAMES = ("Pat Moreno", "Pat")


@pytest.mark.parametrize("note,card", [
    ("Pat confirmed this is waiting on collaborators.", "You confirmed this is waiting on collaborators."),
    ("Pat asks for the table.", "You ask for the table."),
    ("Pat is out until Monday.", "You are out until Monday."),
    ("Pat also wants the slides.", "You also want the slides."),
    ("Pat Moreno agreed to write the letter.", "You agreed to write the letter."),
    ("Sam asks Pat to complete the table by Friday.", "Sam asks you to complete the table by Friday."),
    ("Sam asked Pat to review his paper.", "Sam asked you to review his paper."),
    ("Pat's draft went out.", "Your draft went out."),
    ("Lee answered Pat's questions.", "Lee answered your questions."),
    ("The review is the reviewer's, not Pat's.", "The review is the reviewer's, not yours."),
    ("Pat made it dormant; Pat has the final say.", "You made it dormant; you have the final say."),
    ("Patterns from the lab went out.", "Patterns from the lab went out."),
])
def test_a_note_about_the_owner_speaks_to_him(note, card):
    assert I.second_person(note, NAMES) == card


@pytest.mark.parametrize("note", ["Pat (chat): the pass takes four hours.", "Pat said he would send it.",
                                  "Pat purple the thing.", "Pat"])
def test_a_note_that_cannot_be_rewritten_cleanly_is_not_shown(note):
    assert I.second_person(note, NAMES) == ""


def test_the_card_rewrites_the_note_and_never_the_row(monkeypatch):
    monkeypatch.setattr(I, "owner_names", lambda: NAMES)
    src = F.sources()
    src.rows["task-s2"]["last_action"] = f"{TODAY.isoformat()}: Pat asked the committee for its template."
    assert I.item(src, "item:task-s2")["brief"]["standing"] == [
        "Last step today: You asked the committee for its template."]
    assert src.rows["task-s2"]["last_action"].endswith("Pat asked the committee for its template.")
    src.rows["task-s2"]["last_action"] = f"{TODAY.isoformat()}: Pat said he would ask."
    assert I.item(src, "item:task-s2")["brief"]["standing"] == []
    src.closed[0]["last_action"] = "Pat closed it from the phone."
    assert I.item(src, "closed:row-c1")["brief"]["standing"] == ["Why: You closed it from the phone."]


def test_the_owners_name_comes_from_the_profile(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "user-profile.json").write_text(json.dumps({"name": "Pat Moreno"}))
    monkeypatch.setattr(H, "_ea_dir", lambda: tmp_path)
    monkeypatch.setattr(I, "_PROFILE", {})
    assert I.owner_names() == ("Pat Moreno", "Pat")
    monkeypatch.setattr(I, "_PROFILE", {})
    monkeypatch.setattr(H, "_ea_dir", lambda: tmp_path / "none")
    assert I.owner_names() == ()


# ── a request that needs work, not only a reply ─────────────────────

def test_a_reply_that_asks_for_work_says_what_the_work_is():
    src = F.sources()
    src.rows["response-r1"]["thread_brief"].update(scheduling="none", ask="Ask about the seminar room")
    assert I.item(src, "reply:response-r1")["brief"]["next"] == \
        "Draft a short reply for you to check. Nothing goes out without your Send."
    src.rows["response-r1"]["thread_brief"]["ask"] = "Submit the recommendation letter through the portal"
    assert I.item(src, "reply:response-r1")["brief"]["next"] == \
        "This needs a submission, not just a reply. Ask Tacitus to start it."
    src.rows["response-r1"]["thread_brief"]["ask"] = "Ask about the seminar room"
    src.rows["response-r1"]["type"] = "deadline"  # work the owner delivers, on the reply list
    assert I.item(src, "reply:response-r1")["brief"]["next"] == \
        "This needs work, not just a reply. Ask Tacitus to start it."
    src.rows["deadline-w1"]["thread_brief"]["ask"] = "Fill out the reviewer survey"
    assert I.item(src, "item:deadline-w1")["brief"]["next"] == "Fill in the form with you once you start it."


# ── the briefing's review counts a pending choice as written ────────

def test_the_review_reads_homes_pending_choices_as_written(monkeypatch):
    import sqlite3
    import briefing_review as br
    import owner_trust
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)  # the review runs as the owner
    store = owner_trust.private_dir() / "webchat.sqlite"
    store.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(store))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    acts = {"t1": {"ref": "item:task-s2", "op": "done", "lid": "task-s2", "due": 1},
            "t2": {"ref": "reply:response-r2", "op": "later", "lid": "response-r2", "due": 1},
            "t3": {"ref": "x", "op": "draft", "lid": "nope"}}
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('home:acts', ?)", (json.dumps(acts),))
    conn.commit()
    conn.close()
    try:
        assert I.pending_rows() == {"task-s2": "done", "response-r2": "later"}
        assert br.home_pending() == {"task-s2": "done", "response-r2": "later"}
    finally:
        store.unlink()
    assert I.pending_rows() == {}
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)  # another user reads nothing
    assert I.pending_rows() == {}
    items = [{"ref": "A", "ledger_id": "task-s2", "section": "top", "line": "Send the slides"},
             {"ref": "B", "ledger_id": "response-r2", "section": "needs", "line": "Data access form"},
             {"ref": "C", "ledger_id": "task-s2", "section": "draft", "sections": ["draft"], "line": "x"},
             {"ref": "D", "ledger_id": "deadline-w1", "section": "top", "line": "Review"}]
    led = {"rows": {}, "events": {}, "by_conv": {}, "naming": {}}
    got = br.hard_checks(items, led, today=TODAY, pending={"task-s2": "done", "response-r2": "later"})
    assert got["A"] == {"action": "drop", "kind": "closed", "by": "code", "why": "you marked it done on Home"}
    assert got["B"]["kind"] == "snoozed" and got["B"]["why"] == "you set it aside on Home"
    assert got["C"]["action"] == "hold-draft" and "D" not in got
    assert br.hard_checks(items, led, today=TODAY) == {}


# ── the deeper briefing's own module, merged ────────────────────────

def test_with_the_deeper_module_the_item_reads_its_state_and_its_steps_run_as_choices(monkeypatch):
    import importlib
    deeper = importlib.import_module("home_deeper")
    monkeypatch.setattr(I, "_deeper_mod", lambda: deeper)
    _fe, h, _bot, _c = home()
    it = h.item("item:deadline-w1")
    assert it["deeper"]["state"] in ("none", "unavailable") and ids(it)[:2] == ["start", "done"]
    assert set(deeper.CHOICE_OF) <= set(deeper.STEP_KINDS)
    mine = {"draft", "say", "done", "not_needed", "later", "waiting", "start", "answer"}
    assert {c for v in deeper.CHOICE_OF.values() for c in v} <= mine  # every step names a choice here


def test_a_ledger_write_loads_no_hook_module_that_has_no_hook(monkeypatch):
    for name in ("home_deeper", "preprocessor.home_deeper"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(ledger, "ROW_WRITTEN_HOOKS", ("home_deeper",))
    src = (REPO / "preprocessor" / "home_deeper.py").read_text()
    ledger._row_written("task-z9", created=True)
    assert ("home_deeper" in sys.modules) == ("def row_written(" in src)
