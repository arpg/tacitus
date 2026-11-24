"""One list (docs/webchat.md, Home, "One list"): on the web chat Home is the
one place to act. Every card whose press needs no confirmation token (a
reply that waits, a close proposal, a close note, a work case's cards, a
"which one was it?" question) is a Home item with the card's answers, and
a press there runs the code the card's button ran; Action requests and its
count hold the rest (the gated cards); a reply Home starts is written in
the item's one conversation and the item says so, then becomes the way to
its Send card. The switch is the web block's `home.one_list` (Settings,
Home, Where you answer); off, everything is as before. Made-up data only.
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


# ── Home's list ───────────────────────────────────────────────────────

def test_a_close_proposal_is_a_looks_done_item_with_done_and_not_done():
    left = H.left(one_list())
    it = next(x for x in left if x["id"] == "proposal:task-s2")
    assert (it["kind"], it["label"], it["title"]) == ("proposal", "Looks done", "Send the slides to the committee")
    assert it["detail"] == "The slides went out on Friday"
    assert not any(x["id"] == "item:task-s2" for x in left)           # the row once
    card = I.item(one_list(), "proposal:task-s2")
    assert ids(card) == ["done", "not_done", "start"]
    assert card["choices"][0]["style"] == "primary"
    assert "Tacitus thinks it is done: The slides went out on Friday" in card["brief"]["standing"]
    assert card["brief"]["next"] == "Close it once you say it is done."
    for w in [card["label"], card["title"], *[c["label"] for c in card["choices"]], *card["brief"]["standing"]]:
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
    assert ids(card) == ["done", "not_done"] and card["brief"]["what"] == ""   # no Ask on a kept item


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
    assert ids(card) == ["start"] and card["brief"]["next"] == "Drafting the reply."
    src = one_list(turns={"response-r1": {"state": "failed", "why": "The thread asks for a file Tacitus cannot find."}})
    card = I.item(src, "reply:response-r1")
    assert card["brief"]["standing"][0] == "The reply was not drafted: The thread asks for a file Tacitus cannot find."
    assert ids(card)[:2] == ["draft", "say"]                                     # Draft a reply is Try again
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


def test_a_draft_from_home_stays_on_home_and_tells_the_apps():
    src = one_list()
    h, seen, _clock, fe = _home(src)
    got, _after = h.act("reply:response-r1", "draft")
    assert got["ok"] and "goto" not in got and seen["drafts"] == [("response-r1", "", "")]
    assert fe.changed == 1


def test_ask_opens_the_items_one_conversation_and_keeps_the_one_it_opens(threads):
    src = one_list()
    h, _seen, _clock, fe = _home(src)
    got, _after = h.act("reply:response-r1", "start")
    assert got["ok"] and got["goto"] == "#/main/tb1" and len(fe.posts) == 1
    assert json.loads(threads.read_text())["conv-r1"]["thread"] == "tb1"
    got, _after = h.act("reply:response-r1", "start")                 # again: the same one, no new post
    assert got["goto"] == "#/main/tb1" and len(fe.posts) == 1
    assert got["said"] == "Opened its conversation. Write what you need."
    item_threads.record("conv-r2", "main", "t-turn")                  # where its reply was written
    got, _after = h.act("reply:response-r2", "start")
    assert got["goto"] == "#/main/t-turn" and len(fe.posts) == 1
    h, _seen, _clock, fe = _home(one_list(one_list=False), fe=_FE(one=False))
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
              _item_conversation=lambda key, rid="", conv="", case_rows=(): kept.get(key, ("main", "")),
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
    assert ids(card) == ["start", "done"] and card["brief"]["standing"] == ["A question waits for you."]
    for w in [card["brief"]["what"], card["brief"]["next"], card["label"]]:
        plain(w)
    assert not any(x["kind"] == "conversation" for x in H.left(one_list(one_list=False, conversations=[_talk()])))


def test_ask_opens_the_conversation_and_done_is_its_mark_done_after_the_undo():
    src = one_list(conversations=[_talk()])
    fe = _FE()
    marked = []
    fe.web_set_state = lambda ch, tk, state: marked.append((ch, tk, state)) or {}
    h, _seen, clock, fe = _home(src, fe=fe)
    got, _after = h.act("thread:main:t9", "start")
    assert got["ok"] and got["goto"] == "#/main/t9" and fe.posts == []
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
