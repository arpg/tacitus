"""One list (docs/webchat.md, Home, "One list"): on the web chat Home is the
one place to act. Every card whose press needs no confirmation token (a
reply that waits, a close proposal, a close note, a work case's cards, a
"which one was it?" question) is a Home item with the card's answers, and
a press there runs the code the card's button ran; Action requests and its
count hold the rest (the gated cards); a reply Home starts is written in
the item's one conversation and the item says so, then becomes the way to
its Send card. An item unfolds in place with its mail (read when it opens,
as the Send card reads its quoted thread), what Tacitus has, and one note
box whose words start one turn in the item's conversation; its row keeps
Done, Later and No reply needed, and nothing on the row starts a turn. The
switch is the web block's `home.one_list` (Settings, Home, Where you
answer); off, everything is as before. Made-up data only.
"""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))

import home_fixtures as F  # noqa: E402
import home_items as I  # noqa: E402
import home_screen as H  # noqa: E402
import item_threads  # noqa: E402
import web_thread_state as wts  # noqa: E402
from plain_words import JARGON  # noqa: E402

TODAY = datetime.now(F.ZONE).date()


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "—" not in text and "–" not in text, text


@pytest.fixture(autouse=True)
def no_deeper_module(monkeypatch):
    monkeypatch.setattr(I, "_deeper_mod", lambda: None)


@pytest.fixture
def threads(tmp_path, monkeypatch):
    """The item conversations' record (item_threads) in a private place."""
    monkeypatch.setattr(item_threads, "path", lambda: tmp_path / "item-threads.json")
    monkeypatch.setattr(item_threads.owner_trust, "read_owner_file", lambda p: Path(p).read_text())
    monkeypatch.setattr(item_threads.owner_trust, "write_owner_file",
                        lambda p, text, mode=0o600: Path(p).write_text(text))
    return tmp_path / "item-threads.json"


def one_list(**over) -> H.Sources:
    """The normal afternoon with one list on: task-s2 looks done to
    Tacitus, and response-r2 waits on the owner's answer."""
    src = F.sources()
    src.one_list = True
    src.rows["task-s2"]["close_proposal"] = {"why": "The slides went out on Friday",
                                             "first_at": "2026-11-20T10:00:00Z", "signals": ["linked_send"]}
    for k, v in over.items():
        setattr(src, k, v)
    return src


def ids(it: dict) -> list[str]:
    return [c["id"] for c in it["choices"]]


def acts(it: dict) -> list[str]:
    """The ids of the buttons on an item's folded row (one list)."""
    return [c["id"] for c in it.get("acts") or []]


# ── Home's list ───────────────────────────────────────────────────────

def test_a_close_proposal_is_a_looks_done_item_with_done_and_not_done():
    left = H.left(one_list())
    it = next(x for x in left if x["id"] == "proposal:task-s2")
    assert (it["kind"], it["label"], it["title"]) == ("proposal", "Looks done", "Send the slides to the committee")
    assert it["detail"] == "The slides went out on Friday"
    assert not any(x["id"] == "item:task-s2" for x in left)           # the row once
    card = I.item(one_list(), "proposal:task-s2")
    assert acts(card) == ["done", "not_done"] and ids(card) == [] and card["note"]["open"]   # Ask is the note's
    assert card["acts"][0]["style"] == "primary"
    assert "Tacitus thinks it is done: The slides went out on Friday" in card["brief"]["standing"]
    assert card["brief"]["next"] == "Close it once you say it is done."
    for w in [card["label"], card["title"], *[c["label"] for c in card["acts"]], *card["brief"]["standing"]]:
        plain(w)


def test_with_one_list_off_a_proposal_stays_the_close_cards_and_home_is_as_before():
    src = one_list(one_list=False)
    assert not any(x["kind"] == "proposal" for x in H.left(src))
    assert [x["id"] for x in H.left(src)] == [x["id"] for x in H.left(F.sources())]


def test_a_kept_proposal_reads_the_fixed_words_alone():
    src = one_list()
    src.rows["task-s2"].update(keep_level="local", conversation_id="conv-k9", date_identified="2026-11-20")
    it = next(x for x in H.left(src) if x["id"] == "proposal:task-s2")
    assert it["kept"] == "local" and "Send the slides" not in it["title"] and it["detail"] == ""
    card = I.item(src, "proposal:task-s2")
    assert acts(card) == ["done", "not_done"] and card["brief"]["what"] == ""   # no Ask on a kept item
    assert card["note"]["open"] and card["note"]["chips"] == []                 # its note: code's words go with it


def test_a_send_card_for_a_reply_takes_its_items_place_and_its_ref():
    """One list: the reply's item turns into the way to its Send card, under
    the same ref, so an open card follows; off, the card is its own item."""
    send = F.request("w51", "send", "Send email", "Re: Data access form", "To Alex Kim")
    send["card"]["actions"][0]["value"] = '{"conversation_id": "conv-r2", "draft_id": "d1"}'
    src = one_list()
    src.requests = {"waiting": [send], "done": []}
    left = H.left(src)
    it = next(x for x in left if x["id"] == "reply:response-r2")
    assert it["kind"] == "request" and it["label"] == "Send email" and it["href"].endswith("/w51")
    assert sum(1 for x in left if x.get("_conv") == "conv-r2") == 1
    card = I.item(src, "reply:response-r2")
    assert ids(card) == ["open"] and card["goto"].endswith("/w51")
    src.one_list = False
    left = H.left(src)
    assert any(x["id"] == "request:w51" for x in left) and not any(x["id"] == "reply:response-r2" for x in left)


def test_a_reply_being_written_says_so_and_offers_only_its_conversation():
    src = one_list(turns={"response-r1": {"state": "writing"}})
    it = next(x for x in H.left(src) if x["id"] == "reply:response-r1")
    assert it["detail"] == "Drafting the reply."
    card = I.item(src, "reply:response-r1")
    assert ids(card) == [] and acts(card) == [] and "note" not in card          # its Send card comes next
    assert card["brief"]["next"] == "Drafting the reply."
    src = one_list(turns={"response-r1": {"state": "failed", "why": "The thread asks for a file Tacitus cannot find."}})
    card = I.item(src, "reply:response-r1")
    assert card["brief"]["standing"][0] == "The reply was not drafted: The thread asks for a file Tacitus cannot find."
    assert acts(card) == ["later", "not_needed"] and card["note"]["open"]       # a note is Try again
    off = one_list(one_list=False, turns={"response-r1": {"state": "writing"}})
    assert next(x for x in H.left(off) if x["id"] == "reply:response-r1")["detail"] != "Drafting the reply."


# ── a press on Home runs the card's own code ─────────────────────────

class _FE:
    owner_id, main_channel = "owner", "main"

    def __init__(self, one=True):
        self.one_list, self.cfg, self.changed = one, {}, 0
        self.posts: list = []
        self.store = self

    def home_get(self, name):
        return json.loads(json.dumps(getattr(self, "_meta", {}).get(name) or {}))

    def home_set(self, name, value):
        self.__dict__.setdefault("_meta", {})[name] = json.loads(json.dumps(value))

    def message(self, channel, mid):
        return None

    def channel(self, cid):
        return {"id": cid}

    def home_changed(self):
        self.changed += 1

    def dm_channel(self, user):
        return "dm"

    def rooms_on(self):
        return False

    def post(self, channel, text, thread_ts=None, **kw):
        self.posts.append((channel, text, thread_ts))
        return types.SimpleNamespace(channel=channel, id=f"b{len(self.posts)}", thread_key=f"tb{len(self.posts)}")


def _home(src, *, fe=None, clock=None):
    fe = fe or _FE()
    clock = clock or [F.at(13, 10)]
    h = H.Home(fe, clock=lambda: clock[0], sources=lambda tz: src, background=False)
    seen: dict = {"close": [], "reopen": [], "which": [], "drafts": []}
    h.hooks.close = lambda lid, keep: seen["close"].append((lid, keep)) or {"ok": True}
    h.hooks.reopen = lambda lid: seen["reopen"].append(lid) or {"ok": True}
    h.hooks.which = lambda qid, pick: seen["which"].append((qid, pick)) or {"ok": True, "said": "Recorded."}
    h.hooks.draft = lambda rid, words, answer: seen["drafts"].append((rid, words, answer)) or \
        {"ok": True, "home": True, "said": "Drafting the reply. Its Send card comes to Action requests."}
    seen["notes"], seen["mail"] = [], []
    h.hooks.note = lambda info, words: seen["notes"].append((info, words)) or \
        {"ok": True, "said": "Tacitus is working on it."}
    h.hooks.mail = lambda conv: seen["mail"].append(conv) or [dict(m) for m in MAIL.get(conv, [])]
    return h, seen, clock, fe


def test_done_on_a_proposal_waits_for_its_undo_then_runs_the_close_cards_done(monkeypatch):
    runs = []
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or {"ok": True})
    src = one_list()
    h, seen, clock, _fe = _home(src)
    got, _after = h.act("proposal:task-s2", "done")
    assert got["ok"] and got["said"] == "Done. It is off your list." and got["undo"]
    assert seen["close"] == []                                        # nothing yet: the Undo window
    clock[0] += I.UNDO_S + 1
    h.acts.commit_due()
    assert seen["close"] == [("task-s2", False)] and runs == []       # the close card's Done, not the thread's


def test_not_done_on_a_proposal_is_the_close_cards_not_done_at_once():
    h, seen, _clock, fe = _home(one_list())
    got, _after = h.act("proposal:task-s2", "not_done")
    assert got["ok"] and seen["close"] == [("task-s2", True)]
    assert got["said"] == "Not done. It stays on your list."
    plain(got["said"])
    assert fe.changed


def test_not_done_on_a_row_tacitus_closed_is_the_auto_close_notes_not_done(monkeypatch):
    runs = []
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or {"ok": True, "_exit": 0})
    h, seen, _clock, _fe = _home(one_list())
    got, _after = h.act("closed:row-c1", "reopen")                    # lapsed: Tacitus closed it
    assert got["ok"] and seen["reopen"] == ["row-c1"] and runs == []
    got, _after = h.act("closed:row-c4", "reopen")                    # the owner closed it: as before
    assert got["ok"] and seen["reopen"] == ["row-c1"] and runs[0][:3] == ("ledger.py", "update", "--id")
    h, seen, _clock, _fe = _home(one_list(one_list=False), fe=_FE(one=False))
    runs.clear()
    h.act("closed:row-c1", "reopen")
    assert seen["reopen"] == [] and runs                              # off: the thread's Not done (reopen), as before


def test_a_which_one_question_is_an_item_whose_pick_runs_the_cards_answer():
    q = {"id": "q7", "kind": "part", "text": "A portal says a letter went in. Which one was it?",
         "choices": ["The first fellowship", "The second fellowship", "None of these"]}
    src = one_list(questions=[q])
    it = next(x for x in H.left(src) if x["id"] == "which:q7")
    assert it["label"] == "Answer needed" and it["title"] == q["text"]
    card = I.item(src, "which:q7")
    assert [c["label"] for c in card["choices"]] == q["choices"]
    assert card["brief"]["standing"] == ["Nothing is marked done until you answer."]
    h, seen, _clock, _fe = _home(src)
    got, _after = h.act("which:q7", "which:1")
    assert got == {"ok": True, "said": "Recorded."} and seen["which"] == [("q7", "1")]
    assert not any(x["kind"] == "which" for x in H.left(one_list(questions=[q], one_list=False)))


def test_draft_and_ask_leave_the_row_and_off_they_are_as_before():
    src = one_list()
    h, seen, _clock, fe = _home(src)
    for choice in ("draft", "say", "start", "answer:1"):
        got, _after = h.act("reply:response-r1", choice, "Yes")
        assert not got["ok"] and got["said"] == I.NO_CHOICE, choice
    assert seen["drafts"] == [] and fe.posts == []                    # no turn from one tap
    h, seen, _clock, fe = _home(one_list(one_list=False), fe=_FE(one=False))
    got, _after = h.act("reply:response-r1", "draft")
    assert got["ok"] and seen["drafts"] == [("response-r1", "", "")]
    h.act("reply:response-r1", "start")
    h.act("reply:response-r1", "start")
    assert len(fe.posts) == 2                                          # off: a new one each time, as before


# ── a work case on Home ───────────────────────────────────────────────

def test_a_case_asks_who_it_is_first_and_has_no_open_it_without_a_card():
    import case_surface as C
    import test_case_surface as T
    case = T.new_review()
    case["questions"] = list(case.get("questions") or []) + [
        {"id": "jq1", "kind": "join", "text": "Is the second request the same review?", "answer": ""}]
    got = C.home_choices(case, TODAY, "full")
    assert [c["id"] for c in got] == ["case:join_same", "case:join_other"]
    assert C.home_brief(case, TODAY)["next"] == "Tacitus waits for your answer: is this the same review?"
    src = one_list(cases=[case])
    it = next(x for x in H.left(src) if x.get("_case") == str(case["id"]))
    assert it["title"] == "Is the second request the same review?" and "href" not in it
    assert "open" not in ids(I.item(src, it["id"]))


# ── Action requests holds what needs the lock ───────────────────────

def _fe(tmp_path, **home):
    from chat.web_frontend import WebFrontend
    f = WebFrontend.from_config({"port": 0, "tailscale_login": "owner@example.test",
                                 **({"home": home} if home else {})},
                                store_path=tmp_path / "priv" / "webchat.sqlite",
                                webui_dir=tmp_path / "webui", trusted_config=True)
    return f


def test_the_switch_is_on_with_home_and_off_in_shadow_or_when_the_block_says_so(tmp_path):
    f = _fe(tmp_path)
    assert f.one_list is False                                       # no Home: the cards stay
    f.home = object()
    assert f.one_list is True                                        # the web chat's default
    f.inbound = lambda *a, **k: None
    assert f.one_list is False                                       # a shadow: the primary's DM
    g = _fe(tmp_path / "b", one_list=False)
    g.home = object()
    assert g.one_list is False
    from chat.shadow_frontend import ShadowFrontend
    assert ShadowFrontend.one_list.fget(None) is False
    from chat.base import ChatFrontend
    assert ChatFrontend.one_list is False                            # Slack: its DM cards


def _card(kind, title):
    from chat.cards import CardAction, ConfirmationCard
    return ConfirmationCard(kind=kind, title=title, actions=[
        CardAction("a1", "Yes", {"row": "r1"}, "primary", "yes")], block_id=f"b-{kind}")


def test_action_requests_and_its_count_leave_out_what_home_holds(tmp_path):
    f = _fe(tmp_path)
    f.home = object()
    f.start()
    try:
        for kind in ("send", "replies", "close", "letter_ask", "case", "event"):
            f.post_card("dm", _card(kind, f"A {kind} card"))
        with f.tagging("reply:r1"):
            f.post("dm", ":hourglass_flowing_sand: Writing the reply: a made-up thread.")
        f.post("dm", "A notice with nothing to answer.")
        waiting = f.requests()["waiting"]
        assert sorted(j["card"]["kind"] for j in waiting) == ["event", "send"]
        done = f.requests()["done"]
        assert [j["text"] for j in done] == ["A notice with nothing to answer."]
        assert f.store.channel("dm")["waiting"] == 2
        every = f.store.requests("dm", every=True)["waiting"]
        assert len(every) == 6                                       # the bot's own read sees them all
        f.cfg["home"] = {"one_list": False}                          # Settings, In Action requests
        assert len(f.requests()["waiting"]) == 6 and f.store.channel("dm")["waiting"] == 6
    finally:
        f.stop()


# ── the bot: no ungated card in Action requests, one push to Home ───

class _Pushes:
    one_list = True

    def __init__(self):
        self.sent: list = []

    def notify(self, title, body="", url="", tag="", **kw):
        self.sent.append((title, body, url, tag, kw.get("urgency")))
        return {"sent": 1}

    def app_url(self):
        return "https://chat.example/"


def _close_ns(fe):
    import test_closure_policy as T
    calls: dict = {}
    ns = T._bot_ns(calls)
    pushed: dict = {}
    ns.update(FRONTEND=fe, _HOME_PUSHED=types.SimpleNamespace(read_text=lambda: json.dumps(pushed)))
    real = ns["_atomic"].write_json
    ns["_atomic"] = types.SimpleNamespace(
        write_json=lambda p, d, mode=0: pushed.update(d) if p is ns["_HOME_PUSHED"] else real(p, d, mode))
    return ns, calls


def test_a_close_proposal_posts_no_card_and_pushes_home_once():
    fe = _Pushes()
    ns, calls = _close_ns(fe)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cli = []
    pending = [{"id": "ae28", "description": "Submit the form", "proposal": {"why": "w", "at": old}},
               {"id": "t2", "description": "Send the doc", "proposal": {"why": "v", "at": old}}]
    ns["_ledger_cli"] = lambda *a: cli.append(a) or ({"entries": pending} if a[0] == "proposals" else {})
    assert ns["_post_close_proposals"](force=True) == 0
    assert "cards" not in calls and not [a for a in cli if "--mark-carded" in a]  # uncarded: off brings the card
    assert fe.sent == [("Home", "2 items look done.", "https://chat.example/#/home", "home", "high")]
    ns["_post_close_proposals"](force=True)
    assert len(fe.sent) == 1                                          # once per row
    fe.one_list = False
    assert ns["_post_close_proposals"](force=True) == 2 and calls["cards"][0].kind == "close"


def test_a_close_made_at_once_gets_no_note_and_its_mail_is_still_archived():
    fe = _Pushes()
    ns, calls = _close_ns(fe)
    cli = []
    rows = [{"id": "t1", "description": "Send the form", "why": "the portal confirmed it"}]
    ns["_ledger_cli"] = lambda *a: cli.append(a) or ({"entries": rows} if a == ("promoted",) else {})
    assert ns["_note_promoted_closes"](force=True) == 1
    assert "cards" not in calls and cli[-1] == ("promoted", "--noted", "t1") and fe.sent == []


def test_homes_close_hooks_run_the_close_cards_own_ledger_verbs():
    fe = _Pushes()
    ns, _calls = _close_ns(fe)
    cli = []

    def ledger_cli(*a):
        cli.append(a)
        return {"confirm": {"action": "closed"}, "reject": {"ok": True, "action": "kept_open"},
                "update": {"entry": {"status": "open"}}}.get(a[0], {})
    ns["_ledger_cli"] = ledger_cli
    assert ns["_home_close"]("ae28", False) == {"ok": True, "said": "Done. It is off your list."}
    assert ns["_home_close"]("ae28", True)["ok"]
    assert ns["_home_reopen"]("t1")["ok"]
    assert [a[:3] for a in cli] == [("confirm", "--id", "ae28"), ("reject", "--id", "ae28"),
                                    ("update", "--id", "t1")]
    ev = types.SimpleNamespace(value={"ledger_id": "x1", "batch": ""}, card=types.SimpleNamespace(channel="dm", id="m1"))
    ns["handle_keep_open_proposed"](ev)
    ns["handle_reopen_closed"](ev)
    assert [a[:3] for a in cli[3:]] == [("reject", "--id", "x1"), ("update", "--id", "x1")]


def _reply_ns(queue, fe, **kw):
    import test_reply_card as T
    ns, calls = T._bot(queue, **kw)
    turns: dict = {}
    kept: dict = {}
    ns.update(FRONTEND=fe, _one_list=lambda: ns["FRONTEND"].one_list, OWNER_SLACK_USER_ID="U1", _DRAINING=False,
              _dispatch_lock=__import__("threading").Lock(), _active_dispatches={},
              _load_reply_turns=lambda: json.loads(json.dumps(turns)),
              _save_reply_turns=lambda d: (turns.clear(), turns.update(d)),
              _home_push=lambda keys, line: calls.setdefault("home_pushes", []).append((list(keys), line)) or True,
              _item_conversation=lambda key, rid="", conv="", case_rows=(), room=True:
                  calls.setdefault("rooms", []).append(room) or kept.get(key, ("main", "")),
              _keep_item_conversation=lambda key, ch, th: kept.__setitem__(key, (ch, th)))
    return ns, calls, turns, kept


class _Web(_Pushes):
    dm_takes_text = False

    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def tagging(self, tag):
        import contextlib
        return contextlib.nullcontext()

    def post(self, channel="", thread_ts=None, text=""):
        self.calls["posts"].append((channel, thread_ts, text))
        return types.SimpleNamespace(id=f"p{len(self.calls['posts'])}")

    def lines_tagged(self, channel, thread, tag):
        return [{"id": "p1", "seq": 1, "text": "x"},
                {"id": "p9", "seq": 9, "text": "I could not find the form they ask for."}]


def test_the_days_replies_post_no_block_and_push_home_once_and_off_posts_them_the_same_day():
    import test_reply_card as T
    queue = [T._q("pat", 5), T._q("lee", 4)]
    holder: dict = {}
    ns, calls, _turns, _kept = _reply_ns(queue, _Pushes())
    fe = _Web(calls)
    ns["FRONTEND"] = fe
    holder["fe"] = fe
    assert T._tick(ns) == 0 and calls["cards"] == []
    assert calls["home_pushes"] == [(["reply:response-pat", "reply:response-lee"], "2 replies wait for you.")]
    assert T._tick(ns) == 0 and len(calls["home_pushes"]) == 1       # once a day
    fe.one_list = False
    assert T._tick(ns) == 2 and [c.kind for c in calls["cards"]] == ["replies", "replies"]


def test_a_draft_from_home_runs_in_the_items_conversation_and_its_end_posts_no_card():
    import test_reply_card as T
    holder: dict = {}
    queue = [T._q("ana", 2, conversation_id="conv-ana")]
    ns, calls, turns, kept = _reply_ns(queue, _Pushes())
    fe = _Web(calls)
    ns["FRONTEND"] = fe
    holder["q"] = queue
    got = ns["_home_draft"]("response-ana")
    assert got["ok"] and got["home"] and "channel" not in got
    assert calls["posts"][0][:2] == ("main", None)                    # a new thread in Ask Tacitus
    assert kept["conv-ana"] == ("main", "p1")                         # kept as the item's conversation
    (turn,) = calls["turns"]
    assert turn[:3] == ("main", "p1", "p1")
    (t,) = turns.values()
    assert t["card"] == "" and t["thread"] == "p1" and not t.get("ended")
    assert ns["_home_turns"]() == {"response-ana": {"state": "writing"}}
    # its end: no draft, no record of why: the turn's own last words, no card
    progress = next(iter(turns))
    ns["_reply_turn_ended"](progress, "done")
    assert calls["cards"] == [] and calls["status"] == []
    assert turns[progress]["outcome"] == "problem"
    assert ns["_home_turns"]() == {"response-ana": {"state": "failed",
                                                     "why": "I could not find the form they ask for."}}
    # a second draft runs in the same conversation
    ns["_active_threads"].clear()                                     # the first turn's run is over
    assert ns["_home_draft"]("response-ana")["ok"]
    assert calls["posts"][-1][:2] == ("main", "p1")


def _case_ns(monkeypatch, cases, fe):
    import test_case_surface as T
    ns, calls, store = T._bot(monkeypatch, cases)
    kept: dict = {}
    ns.update(FRONTEND=fe, _one_list=lambda: fe.one_list,
              _home_push=lambda keys, line: calls.setdefault("home_pushes", []).append((list(keys), line)) or True,
              _strip_slack_markup=lambda t: __import__("re").sub(r"[*_`~]|:[a-z_]+:", "", t).strip(),
              _item_conversation=lambda key, rid="", conv="", case_rows=(), room=True:
                  kept.get(key, ("main" if not room else "r-room", "") if case_rows else ("main", "")),
              _keep_item_conversation=lambda key, ch, th: kept.__setitem__(key, (ch, th)))
    return ns, calls, store, kept


def test_a_case_posts_no_card_pushes_home_and_off_brings_its_card(monkeypatch):
    import test_case_surface as T

    class Fe(_Pushes):
        def post(self, channel="", thread_ts=None, text=""):
            return types.SimpleNamespace(id="p1")
    fe = Fe()
    ns, calls, store, _kept = _case_ns(monkeypatch, [T.new_review()], fe)
    assert ns["_post_letter_cards"](force=True) == 0 and calls["cards"] == []
    (push,) = calls["home_pushes"]
    assert push[0][0].startswith("case:rv-2:ask:") and "review" in push[1].lower()
    rec = store["cards"]["rv-2"]
    assert rec["home"] and rec["live"] and rec["card"] == ""
    assert ns["_post_letter_cards"](force=True) == 0 and len(calls["home_pushes"]) == 1   # asked once
    fe.one_list = False
    assert ns["_post_letter_cards"](force=True) == 1 and calls["cards"][0].kind == "case_ask"


def test_a_case_turn_from_home_runs_in_the_cases_conversation(monkeypatch):
    import test_case_surface as T
    fe = _Pushes()
    case = T._live_review()
    ns, calls, _store, kept = _case_ns(monkeypatch, [case], fe)
    fe.post = lambda channel="", thread_ts=None, text="": types.SimpleNamespace(id="p1")
    ns["_case_rows"] = lambda: {}
    got = ns["_home_case"]("rv-1", "draft", "", "Keep it short.")
    assert got["ok"] and "channel" not in got                         # Home keeps the owner where he pressed
    ((args, key),) = calls["turns"]
    assert args[1] == "p1" and key.endswith("letter-rv-1")
    assert kept["case:rv-1"][1] == "p1"
    # a case kept from hosted models below Summary never opens one in a room
    from preprocessor import case_surface as C
    monkeypatch.setattr(C, "level", lambda case, rows=None, records=None: "local")
    kept.clear()
    calls["turns"].clear()
    ns["_home_case"]("rv-1", "draft", "", "")
    assert kept["case:rv-1"][0] == "main"


# ── Needs you is part of Home ─────────────────────────────────────────

def _talk(ch="main", tk="t9", text="Which project is the new grant for?", reason="A question waits for you.",
          **over):
    return {"channel": ch, "thread_key": tk, "text": text, "state": "needs_you", "state_reason": reason,
            "stale": False, "_ids": [], "_convs": [], **over}


def test_a_conversation_that_needs_you_is_an_answer_needed_item_once():
    src = one_list(conversations=[
        _talk(),
        _talk(tk="t10", text="About the seminar date", _convs=["conv-r1"]),       # its reply is on Home
        _talk(tk="t11", text="Slides question", _ids=["task-s2"]),               # names a listed row
        _talk(tk="t12", text="Old thread", stale=True),
        _talk(tk="t13", text="Waits on someone", state="waiting"),
        _talk(ch="r1", tk="t14", text="Budget table check", _room="lab-budget")])
    left = H.left(src)
    talk = [x for x in left if x["kind"] == "conversation"]
    assert [x["id"] for x in talk] == ["thread:main:t9", "thread:r1:t14", "thread:main:t12"]  # stale last
    first = talk[0]
    assert (first["label"], first["title"], first["detail"]) == \
        ("Answer needed", "Which project is the new grant for?", "A question waits for you.")
    assert first["href"] == "#/main/t9" and talk[1]["project"] == "lab-budget"
    assert left[-1]["id"] == "thread:main:t12"
    card = I.item(src, "thread:main:t9")
    assert acts(card) == ["done"] and ids(card) == [] and card["note"]["open"]
    assert card["brief"]["standing"] == ["A question waits for you."]
    for w in [card["brief"]["what"], card["brief"]["next"], card["label"]]:
        plain(w)
    assert not any(x["kind"] == "conversation" for x in H.left(one_list(one_list=False, conversations=[_talk()])))


def test_a_note_goes_to_the_conversation_and_done_is_its_mark_done_after_the_undo():
    src = one_list(conversations=[_talk()])
    fe = _FE()
    marked = []
    fe.web_set_state = lambda ch, tk, state: marked.append((ch, tk, state)) or {}
    h, seen, clock, fe = _home(src, fe=fe)
    got, _after = h.act("thread:main:t9", "note", "The lab budget one.")
    assert got["ok"] and fe.posts == []
    ((info, words),) = seen["notes"]
    assert words == "The lab budget one." and info["key"] == "thread:main:t9" and info["thread"] == ["main", "t9"]
    got, _after = h.act("thread:main:t9", "done")
    assert got["ok"] and got["undo"] and marked == []
    assert "thread:main:t9" not in [x["ref"] for x in h.payload()["left"]]   # off the list while Undo runs
    clock[0] += I.UNDO_S + 1
    h.acts.commit_due()
    assert marked == [("main", "t9", "done")]


def test_the_conversations_name_what_they_are_about(threads):
    """_needs: a thread's cards and id-shaped words, an item's own
    conversation, its room."""
    item_threads.record("conv-r2", "main", "t3")
    item_threads.record("case:lc-1", "r1", "t4")

    class Store:
        def needs(self):
            return [{"channel": "main", "thread_key": "t3", "state": "needs_you", "text": "x"},
                    {"channel": "r1", "thread_key": "t4", "state": "needs_you", "text": "y"},
                    {"channel": "main", "thread_key": "t5", "state": "needs_you", "text": "z"},
                    {"channel": "main", "thread_key": "t6", "state": "waiting", "text": "w"}]

        def thread(self, ch, tk, history=False):
            return [{"text": "From Home (item task-s2). Write here what to do next."}] if tk == "t5" else [{"text": ""}]

        def channel(self, cid):
            return {"kind": "room", "name": "ea-lab-budget"} if cid == "r1" else {"kind": "main"}
    fe = types.SimpleNamespace(store=Store())
    rows = {r["thread_key"]: r for r in H._needs(fe)}
    assert set(rows) == {"t3", "t4", "t5"}
    assert rows["t3"]["_convs"] == ["conv-r2"] and rows["t4"]["_ids"] == ["lc-1"]
    assert rows["t4"]["_room"] == "lab-budget" and "task-s2" in rows["t5"]["_ids"]


# ── Edit stays in one conversation ───────────────────────────────────

def _edit_ns(one=True):
    """handle_edit_draft on the web chat (no windows), on stand-ins."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("def handle_edit_draft(")
    end = src.index("@app.view(\"edit_draft_modal\")")
    posts, kept = [], {}

    class Fe:
        dm_takes_text = False
        one_list = one

        def supports(self, cap):
            return False

        def is_dm(self, ch):
            return ch == "dm"

        def post(self, channel="", thread_ts=None, text="", unfurl=True):
            posts.append((channel, thread_ts, text))
            return types.SimpleNamespace(id=f"q{len(posts)}")

        def permalink(self, ch, mid):
            return f"https://chat.example/#/{ch}/{mid}"
    ns = {"FRONTEND": Fe(), "log": types.SimpleNamespace(debug=lambda *a: None, warning=lambda *a: None),
          "_verify_button_user": lambda ev: True, "_one_list": lambda: one, "CHANNEL_ID": "main",
          "_fetch_draft_body": lambda did, *a: {"subject": "Re: Seminar date"},
          "_draft_account": lambda v: ("outlook", ""),
          "_delivery_channel": lambda key: "dm" if key.startswith("dm") else "main",
          "_item_conversation": lambda key, rid="", conv="", room=True: kept.get(key, ("main" if not room else "r-room", "")),
          "_keep_item_conversation": lambda key, ch, th: kept.__setitem__(key, (ch, th)),
          "app": types.SimpleNamespace(action=lambda name: (lambda fn: fn))}
    ns["_card_value"] = lambda ev: ev.value
    exec(compile(src[start:end], "bot[edit]", "exec"), ns)
    return ns, posts, kept


def _edit_ev(value):
    return types.SimpleNamespace(value=value, card=types.SimpleNamespace(channel="dm", id="w7"), trigger_id="web:x")


def test_edit_asks_in_the_drafts_own_conversation_and_says_the_card_changes_in_place():
    ns, posts, _kept = _edit_ns()
    ns["handle_edit_draft"](_edit_ev({"draft_id": "d1", "thread_ts": "t-item", "conversation_id": "conv-r1"}))
    (asked, line) = posts
    assert asked[:2] == ("main", "t-item")
    assert asked[2].endswith("Tell me here and I will revise it on its Send card. You can also edit it in your mail client.")
    assert line[:2] == ("dm", "w7") and line[2].endswith("The revised draft shows on this card.")
    plain(line[2])


def test_edit_on_a_draft_from_action_requests_asks_in_its_items_conversation_and_keeps_it():
    ns, posts, kept = _edit_ns()
    ns["handle_edit_draft"](_edit_ev({"draft_id": "d1", "thread_ts": "dm-7", "conversation_id": "conv-r1"}))
    assert posts[0][:2] == ("main", None) and kept["conv-r1"] == ("main", "q1")   # its one conversation now
    ns["handle_edit_draft"](_edit_ev({"draft_id": "d1", "thread_ts": "dm-7", "conversation_id": "conv-r1"}))
    assert posts[2][:2] == ("main", "q1")                                           # the same one again
    ns, posts, kept = _edit_ns(one=False)
    ns["handle_edit_draft"](_edit_ev({"draft_id": "d1", "thread_ts": "dm-7", "conversation_id": "conv-r1"}))
    assert posts[0][:2] == ("main", None) and kept == {}                            # off: a new one, as before
    assert "post a new Send card" in posts[0][2] and "until the revised draft gets its new card" in posts[1][2]


def test_a_revised_draft_replaces_its_send_card_in_place():
    import test_draft_chain_e2e as T
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = T._reconfirm_helpers(Path(td))
        T._with_retire_stub(ns, posted, web=True)
        crumbs = []
        ns.update(_one_list=lambda: True,
                  _post_dm_breadcrumb=lambda *a: crumbs.append(a))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test")
        assert posted == [("refreshed", "AAMkD=")] and crumbs == []          # no new card, no old one retired
        ns["_reconfirm_updated_drafts"]("main", "t-item", {"AAMkD="}, "test")
        assert crumbs == [("main", "t-item", "DM1", "111.1", "The Send button for the revised draft")]


# ── an item unfolds in place: its mail, what Tacitus has, one note box ──

# The mail threads the bot's Hooks.mail would read (oldest first): made up.
MAIL = {"conv-r1": [
    {"id": "m-1", "from": {"name": "Sam Rivera", "address": "sam.rivera@example.org"},
     "sent": "2026-11-20T15:00:00Z", "subject": "Seminar date",
     "text": "Could you give the seminar in March?\nAny week works for us."},
    {"id": "m-2", "from": {"name": "Pat Owner", "address": "owner@example.edu"},
     "sent": "2026-11-21T15:00:00Z", "subject": "Re: Seminar date", "text": "Let me look at my calendar."}],
    "conv-r2": [{"id": "m-3", "from": {"name": "Alex Kim", "address": "alex.kim@example.org"},
                 "sent": "2026-11-19T10:00:00Z", "subject": "Data access form", "text": "form " * 1000}]}


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(I, "owner_address", lambda: "owner@example.edu")


def test_the_folded_row_has_done_later_and_no_reply_needed_and_nothing_that_starts_a_turn():
    src = one_list(cases=[])
    src.replies[1]["queued"] = {"outcome": "decision", "note": "Sign it?", "choices": ["Yes, I sign it", "No"]}
    left = {x["ref"]: x for x in H.build(src)["left"]}
    assert acts(left["reply:response-r1"]) == ["later", "not_needed"]
    assert acts(left["reply:response-r2"]) == ["later", "not_needed"]          # a question: its picks are chips
    assert acts(left["item:deadline-w1"]) == ["done", "later", "waiting"]
    assert acts(left["proposal:task-s2"]) == ["done", "not_done"]
    assert acts(left["request:w41"]) == []                                       # Send stays in Action requests
    for x in left.values():
        assert set(acts(x)) <= set(I.ACT_IDS) and acts(x) == sorted(acts(x), key=I.ACT_IDS.index)
        assert "mail" not in x and "note" not in x                               # read when it opens, not here
        for c in x.get("acts") or []:
            plain(c["label"])
    off = H.build(one_list(one_list=False, cases=[]))
    assert not any("acts" in x for x in off["left"])


def test_an_item_opened_reads_its_mail_once_the_way_the_send_card_shows_quoted_mail(owner):
    src = one_list(cases=[])
    h, seen, _clock, _fe = _home(src)
    h.payload()
    assert seen["mail"] == []                                                    # Home's list reads no mail
    it = h.item("reply:response-r1")
    assert it["mail"] == {"label": "The email from Sam Rivera, Nov 20", "cut": False,
                          "text": "Could you give the seminar in March?\nAny week works for us."}
    plain(it["mail"]["label"])
    h.item("reply:response-r1")
    assert seen["mail"] == ["conv-r1"]                                           # read once, then kept a while
    long = h.item("reply:response-r2")["mail"]
    assert long["cut"] and len(long["text"]) <= I.MAIL_MAX < len(MAIL["conv-r2"][0]["text"])   # bounded
    assert "mail" not in h.item("proposal:task-s2")                              # a row from chat: no mail part
    h, seen, _clock, _fe = _home(one_list(one_list=False, cases=[]), fe=_FE(one=False))
    assert "mail" not in h.item("reply:response-r1") and seen["mail"] == []      # off: as before


def test_the_message_that_started_it_is_the_rows_source_else_the_newest_from_them():
    msgs = MAIL["conv-r1"] + [{"id": "m-9", "from": {"name": "Sam Rivera", "address": "sam.rivera@example.org"},
                               "sent": "2026-11-22T09:00:00Z", "subject": "Re: Seminar date", "text": "Any news?"}]
    assert I.pick_mail(msgs, "m-1", "owner@example.edu")["id"] == "m-1"
    assert I.pick_mail(msgs, "", "owner@example.edu")["id"] == "m-9"
    assert I.pick_mail(msgs[:2], "gone", "owner@example.edu")["id"] == "m-1"     # never the owner's own
    assert I.pick_mail([], "", "") is None


def test_kept_mail_shows_as_the_send_card_shows_it_and_reaches_no_model(owner, monkeypatch):
    """The Send card shows its quoted thread as the bot's own live read gives
    it, at every keep level, and puts it in nothing a model reads: the item
    does the same."""
    seen_items: list = []

    class Deeper:
        def state(self, fe, ref, item, row=None):
            seen_items.append(json.dumps(item))
            return {"state": "unavailable"}
    monkeypatch.setattr(I, "_deeper_mod", lambda: Deeper())
    plain_item = _home(one_list(cases=[]))[0].item("reply:response-r1")
    src = one_list(cases=[])
    src.replies[0]["keep_level"] = "local"
    h, seen, _clock, _fe = _home(src)
    it = h.item("reply:response-r1")
    assert it["kept"] == "local" and "Sam Rivera" not in it["title"] and it["brief"]["what"] == ""
    assert it["mail"] == plain_item["mail"]                                      # one rule: the Send card's
    assert it["note"]["open"] and it["note"]["chips"] == ["Yes", "No"] and acts(it) == ["done", "later"]
    assert not any("Could you give the seminar" in x for x in seen_items)        # never in the deeper packet
    f = I.find(one_list(cases=[]), "reply:response-r1")
    info = I.note_info(f, plain_item)
    assert "seminar in March" not in json.dumps(info)                            # nor in a note's turn


def test_quick_answers_fill_the_note_box_and_never_act(monkeypatch):
    src = one_list(cases=[])
    src.replies[1]["queued"] = {"outcome": "decision", "note": "Sign it?", "choices": ["Yes, I sign it", "No"]}
    h, seen, _clock, _fe = _home(src)
    assert h.item("reply:response-r1")["note"]["chips"] == ["Yes", "No"]
    assert h.item("reply:response-r2")["note"]["chips"] == ["Yes, I sign it", "No"]
    got, _after = h.act("reply:response-r2", "answer:1")
    assert not got["ok"] and seen["drafts"] == [] and seen["notes"] == []

    class Deeper:
        def state(self, fe, ref, item, row=None):
            return {"state": "ready", "text": "What happened\n- A made-up line.", "steps": [
                {"id": "s1", "label": "Draft: Tuesday at 2:00 works.", "fill": "Tuesday at 2:00 works."},
                {"id": "s2", "label": "Done"}, {"id": "continue", "label": "Ask"}]}

        def step(self, *a, **k):
            raise AssertionError("a chip never acts")
    monkeypatch.setattr(I, "_deeper_mod", lambda: Deeper())
    it = h.item("reply:response-r1")
    assert it["note"]["chips"] == ["Yes", "No", "Tuesday at 2:00 works."]
    assert [st["id"] for st in it["deeper"]["steps"]] == ["s2"]                  # no Ask, no draft step
    got, _after = h.act("reply:response-r1", "step:s1")
    assert not got["ok"]


def test_a_note_goes_to_the_items_own_conversation_and_starts_one_turn(owner):
    src = one_list(cases=[])
    h, seen, clock, _fe = _home(src)
    it = h.item("reply:response-r1")
    got, _after = h.act("reply:response-r1", "note", "  Yes, but after Dec 21.  ", it["version"])
    assert got == {"ok": True, "said": "Tacitus is working on it."}
    ((info, words),) = seen["notes"]
    assert words == "Yes, but after Dec 21."
    assert (info["key"], info["lid"], info["conv"], info["reply"]) == ("conv-r1", "response-r1", "conv-r1", True)
    assert info["opening"].endswith("From Home (item response-r1).") and not info["kept"]
    assert "Could you give the seminar" not in json.dumps(info)                 # no mail text goes with it
    assert seen["drafts"] == []
    got, _after = h.act("reply:response-r1", "note", "   ")
    assert got == {"ok": False, "said": I.NOTE_EMPTY} and len(seen["notes"]) == 1
    got, _after = h.act("reply:response-r1", "note", "No reply needed.")       # the row's own button, not a turn
    assert got["ok"] and got["undo"] and got["said"] == "No reply needed. It is off your list."
    assert len(seen["notes"]) == 1
    plain(got["said"])


def test_nothing_on_unfold_starts_a_turn():
    src = one_list(cases=[], conversations=[_talk()])
    h, seen, _clock, _fe = _home(src)
    for x in h.payload()["left"]:
        h.item(x["ref"])
    assert seen["notes"] == [] and seen["drafts"] == []


def _noted(state, said="", card=None, ref="reply:response-r1", **over):
    rec = {"ref": ref, "key": "conv-r1", "lid": "response-r1", "conv": "conv-r1", "case": "", "state": state,
           "asked": "When is the seminar?", "said": said, "at": F.at(13, 0), "route": "#/main/t-note"}
    return [dict(rec, **({"card": card} if card else {}), **over)]


def test_the_items_line_and_card_show_its_note_working_then_its_answer():
    src = one_list(cases=[], notes=_noted("working"))
    v = I.item(one_list(cases=[]), "reply:response-r1")["version"]
    h, seen, _clock, _fe = _home(src)
    line = next(x for x in h.payload()["left"] if x["ref"] == "reply:response-r1")
    assert line["detail"] == H.NOTE_WORKING
    it = h.item("reply:response-r1")
    assert it["version"] == v                                                    # the line's change is not a change
    assert it["note"]["last"] == {"state": "working", "asked": "When is the seminar?", "said": "",
                                  "goto": "#/main/t-note"}
    got, _after = h.act("reply:response-r1", "note", "And the room?")
    assert got == {"ok": False, "said": I.NOTE_BUSY} and seen["notes"] == []     # one turn at a time
    src = one_list(cases=[], notes=_noted("answered", "It is on March 3, in the main hall."))
    h, _seen, _clock, _fe = _home(src)
    assert next(x for x in h.payload()["left"] if x["ref"] == "reply:response-r1")["detail"] == H.NOTE_ANSWERED
    last = h.item("reply:response-r1")["note"]["last"]
    assert last["state"] == "answered" and last["said"] == "It is on March 3, in the main hall."
    for w in (H.NOTE_WORKING, H.NOTE_ANSWERED, I.NOTE_BUSY, I.NOTE_EMPTY, I.NOTE_STARTED):
        plain(w)


def test_a_draft_the_note_made_is_its_send_card_in_the_items_place():
    send = F.request("w52", "send", "Send email", "Re: Seminar date", "To Sam Rivera")
    send["card"]["actions"][0]["value"] = '{"conversation_id": "conv-r1", "draft_id": "d2"}'
    src = one_list(cases=[], notes=_noted("drafted", "The draft says yes, after Dec 21.", ["dm", "w52"]))
    src.requests = {"waiting": [send], "done": []}
    it = I.item(src, "reply:response-r1", fe=_FE())
    assert it["kind"] == "request" and ids(it) == ["open"]                       # Send stays in Action requests
    assert it["note"]["open"] is False and it["note"]["last"]["state"] == "drafted"
    assert it["note"]["last"]["said"] == "The draft says yes, after Dec 21." and it["note"]["last"]["card"]


# ── the bot: the note's one turn ─────────────────────────────────────

class _NoteWeb(_Web):
    def __init__(self, calls):
        super().__init__(calls)
        self.dispatched = 0
        calls["told"] = []
        self.home = types.SimpleNamespace(changed=lambda: calls["told"].append("home"))
        self.store = types.SimpleNamespace(home_item_emit=lambda ref: calls["told"].append(ref))

    def web_message(self, channel, text, thread_key, nonce=None):
        self.calls["owner"].append((channel, text, thread_key))
        row = {"id": f"n{len(self.calls['owner'])}", "channel": channel, "thread_key": thread_key}
        return row, lambda: setattr(self, "dispatched", self.dispatched + 1)


def _note_ns():
    import test_reply_card as T
    ns, calls, _turns, kept = _reply_ns([T._q("ana", 2, conversation_id="conv-ana")], _Pushes())
    calls["owner"] = []
    fe = _NoteWeb(calls)
    notes: dict = {}
    ns.update(FRONTEND=fe, _load_home_notes=lambda: json.loads(json.dumps(notes)),
              _save_home_notes=lambda d: (notes.clear(), notes.update(d)))
    return ns, calls, notes, kept, fe


ITEM = {"ref": "reply:response-ana", "kind": "reply", "lid": "response-ana", "conv": "conv-ana", "case": "",
        "key": "conv-ana", "thread": [], "title": "Seminar date", "kept": "", "reply": True,
        "brief": {"what": "A reply you owe Ana.", "asked": "Confirm the date.", "due": ""}, "owner_said": [],
        "opening": "*Seminar date*\nA reply you owe Ana.\n\nFrom Home (item response-ana)."}


def test_the_bot_posts_the_note_as_the_owners_message_and_runs_one_turn():
    ns, calls, notes, kept, fe = _note_ns()
    got = ns["_home_note"](dict(ITEM), "Yes, but after Dec 21.")
    assert got == {"ok": True, "said": "Tacitus is working on it."}
    assert calls["posts"] == [("main", None, ITEM["opening"])]                   # a new conversation opens
    assert kept["conv-ana"] == ("main", "p1")                                    # it is the item's one now
    assert calls["owner"] == [("main", "Yes, but after Dec 21.", "p1")]          # his words, as his message
    assert fe.dispatched == 0                                                    # its own dispatch never runs
    ((channel, thread, msg, prompt, user, reply),) = calls["turns"]             # one turn, on his message
    assert (channel, thread, msg, user, reply) == ("main", "p1", "n1", "U1", True)
    assert '"Yes, but after Dec 21."' in prompt and "--ledger-id response-ana" in prompt
    assert "Ledger item response-ana" in prompt and "write no draft for a question alone" in prompt
    assert ns["_home_notes"]()[0]["state"] == "working"
    assert calls["told"] == ["home", "reply:response-ana"]                     # the open item reads it again
    again = ns["_home_note"](dict(ITEM), "And the room?")
    assert not again["ok"] and len(calls["turns"]) == 1                          # one at a time
    ns["_note_answer"]("n1", "It is on March 3, in the main hall. *Draft* none.")
    ns["_note_turn_ended"]("n1", "done", [])
    (rec,) = ns["_home_notes"]()
    assert calls["told"][-2:] == ["home", "reply:response-ana"]
    assert rec["state"] == "answered" and rec["said"] == "It is on March 3, in the main hall. Draft none."
    assert rec["route"] == "#/main/p1" and rec["asked"] == "Yes, but after Dec 21."
    # a second note goes to the same conversation; a draft makes it drafted
    ns["_active_threads"].clear()
    assert ns["_home_note"](dict(ITEM), "Say yes for Dec 22.")["ok"]
    assert calls["owner"][-1] == ("main", "Say yes for Dec 22.", "p1") and len(calls["posts"]) == 1
    ns["_load_draft_confirms"]()["d-9"] = {"status": "live", "channel": "dm", "message_ts": "s9"}
    ns["_note_turn_ended"]("n2", "done", ["d-9"])
    rec = ns["_home_notes"]()[0]
    assert rec["state"] == "drafted" and rec["card"] == ["dm", "s9"]


def test_the_bot_takes_no_note_with_one_list_off():
    ns, calls, _notes, _kept, fe = _note_ns()
    fe.one_list = False
    assert not ns["_home_note"](dict(ITEM), "Yes.")["ok"]
    assert calls["turns"] == [] and calls["owner"] == []


def test_a_note_turn_that_ends_with_nothing_says_why():
    ns, _calls, _notes, _kept, _fe = _note_ns()
    ns["_home_note"](dict(ITEM), "What did they ask?")
    ns["_note_turn_ended"]("n1", "timeout", [])
    rec = ns["_home_notes"]()[0]
    assert rec["state"] == "failed" and rec["said"] == ns["REPLY_ENDED_WORDS"]["timeout"]


def test_the_items_mail_is_the_bots_own_read_as_the_send_cards_is():
    """Both reads run graph_actions as the owner's process (never the mail
    mediator, so neither the sealed view nor a model is in it)."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    for fn in ("def _graph_json(", "def _fetch_draft_body("):
        body = src[src.index(fn):src.index("\ndef ", src.index(fn) + 10)]
        assert '"preprocessor" / "graph_actions.py"' in body and "mail_actions" not in body, fn
    ns, _calls, _notes, _kept, _fe = _note_ns()
    reads = []
    ns["_graph_json"] = lambda *argv: reads.append(argv) or {"messages": MAIL["conv-r1"]}
    ns["_plain_mail_id"] = lambda v: v == "conv-r1"
    got = ns["_home_mail"]("conv-r1")
    assert reads == [("get-conversation", "--conversation-id", "conv-r1", "--max-chars", "3001")]
    assert [m["id"] for m in got] == ["m-1", "m-2"] and got[0]["text"].startswith("Could you give")
    assert ns["_home_mail"]("not a mail id") == [] and len(reads) == 1


# ── a kept item's note: code's fixed words alone go with it ─────────────

ASK_WORDS = "Pick a date in March for the seminar"      # the thread record's ask (words from the mail)


def _kept(level: str) -> H.Sources:
    """One list's afternoon with response-r1's thread kept at `level`; the
    owner's earlier word on it and a question's picks made up."""
    src = one_list(cases=[])
    src.replies[0].update(keep_level=level, since="2026-11-23T15:00:00Z",
                          owner_said=[{"said": "Tell them March is fine", "at": "2026-11-21T10:00:00Z"}])
    return src


@pytest.mark.parametrize("level", ["local", "facts"])
def test_a_kept_items_note_carries_no_mail_and_no_more_than_its_level(owner, level):
    src = _kept(level)
    h, seen, _clock, _fe = _home(src)
    it = h.item("reply:response-r1")
    assert it["kept"] == level and it["note"]["open"] and it["note"]["chips"] == ["Yes", "No"]
    got, _after = h.act("reply:response-r1", "note", "Can I say yes for March 5?")
    assert got["ok"]
    ((info, words),) = seen["notes"]
    assert words == "Can I say yes for March 5?" and info["kept"] == level
    sent = json.dumps(info)
    for never in ("Could you give the seminar", "Any week works", ASK_WORDS, "March is fine"):
        assert never not in sent, never                                         # no mail, no record, no earlier word
    assert info["brief"] == {"what": "", "asked": "", "due": ""} and info["owner_said"] == []
    assert info["opening"].startswith(f"*{info['title']}*") and "(item response-r1)" in info["opening"]
    if level == "local":                                                         # the date alone
        assert "Sam Rivera" not in sent and "Seminar date" not in sent
        assert info["title"] == "A kept message from Nov 23 needs you. Open it in Outlook."
    else:                                                                        # also the headers' sender and subject
        assert "Sam Rivera" in info["title"] and "Seminar date" in info["title"]


def test_a_kept_questions_picks_are_not_its_quick_answers():
    src = _kept("facts")
    src.replies[0]["queued"] = {"outcome": "decision", "note": "Thursday or Friday?",
                                "choices": ["Thursday the 5th", "Friday the 6th"]}
    it = I.item(src, "reply:response-r1")
    assert it["note"]["chips"] == ["Yes", "No"] and "Thursday" not in json.dumps(it)


def test_the_bot_runs_a_kept_items_note_on_code_words_alone_and_never_in_a_room():
    ns, calls, _notes, _kept, _fe = _note_ns()
    line = "A kept message from Nov 23 needs you. Open it in Outlook."
    item = dict(ITEM, kept="local", title=line, brief={"what": "", "asked": "", "due": ""},
                opening=f"*{line}*\n\nFrom Home (item response-ana).")
    got = ns["_home_note"](item, "Can I say yes?")
    assert got["ok"] and calls["rooms"] == [False]                               # Ask Tacitus, never a room
    assert calls["posts"][0][2] == item["opening"] and calls["owner"][0][1] == "Can I say yes?"
    ((_ch, _th, _msg, prompt, _u, _r),) = calls["turns"]
    assert "Ledger item response-ana: made up" not in prompt                     # never its row's words
    assert line in prompt and "keep level local" in prompt and "mail_actions.py" in prompt
    assert "ledger item response-ana" in prompt and "conversation id conv-ana" in prompt
    assert '"Can I say yes?"' in prompt
    plain_item = ns["_home_note_prompt"](dict(ITEM), "Yes.")
    assert "Ledger item response-ana: made up" in plain_item and "keep level" not in plain_item
