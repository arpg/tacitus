"""The deeper briefing on a Home item (preprocessor/home_deeper.py,
docs/webchat.md, Home, "The deeper briefing"): the packet at each keep
level, where the call runs, code's check of the words and the steps
against the gates, the job's states, the bounds, the fallbacks, the
`home-item` event and the conversation a briefing opens.
Made-up data only."""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))

import home_deeper as D  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from plain_words import JARGON  # noqa: E402

ZONE = ZoneInfo("America/Denver")
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=ZONE).timestamp()
TODAY = date(2026, 11, 22)
QUOTE = "PRIVATE WORDS FROM THE MAIL"


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "\u2014" not in text and "\u2013" not in text, text


def make_row(**over) -> dict:
    brief = {"subject": "Rover loan for the field week",
             "ask": "Confirm the rover can go out for the December field week.",
             "state": "needs-reply", "scheduling": "none", "conversation_id": "conv-1",
             "last_inbound": "2026-11-18",
             "messages": [{"received": "2026-11-18T09:00", "own": False, "from": "Robin Vale",
                           "gist": "Robin Vale asked to borrow the rover for the field week from December 7.",
                           "quotes": [QUOTE]}]}
    brief.update(over.pop("brief", {}))
    row = {"id": "response-t1", "type": "response_needed", "status": "open",
           "description": "Needs response: Rover loan for the field week", "from_name": "Robin Vale",
           "from_email": "robin@example.org", "conversation_id": "conv-1", "date_identified": "2026-11-18",
           "deadline": "2026-12-04", "updated_at": "2026-11-20T10:00:00", "last_action": "",
           "metadata": json.dumps({"thread_brief": brief})}
    row.update(over)
    return row


ITEM = {"ref": "reply:response-t1", "kind": "reply", "kept": "", "title": "Rover loan for the field week",
        "brief": {"what": "Robin Vale asks about the rover.", "standing": ["Waiting 4 days"]},
        "choices": [{"id": "draft"}, {"id": "not_needed"}, {"id": "later"}, {"id": "say"}]}


def answer(**over) -> str:
    obj = {"history": ["Robin Vale asked on November 18 to borrow the rover for the field week."],
           "stakes": ["The reply is due by December 4."],
           "unknown": ["The records do not show if the rover is free."],
           "steps": [{"kind": "draft_reply", "say": "Yes, the rover can go out for the field week.",
                      "question": "", "date": "", "minutes": 0, "ask": "", "why": "he asked"},
                     {"kind": "hold_slot", "say": "", "question": "", "date": "2026-11-30", "minutes": 60,
                      "ask": "", "why": "time to plan"}]}
    obj.update(over)
    return json.dumps(obj)


class FakeStore:
    def __init__(self):
        self.meta = {}
        self.events = []

    def home_get(self, name):
        return json.loads(json.dumps(self.meta.get(name) or {}))

    def home_set(self, name, value):
        self.meta[name] = json.loads(json.dumps(value))

    def home_item_emit(self, ref):
        self.events.append(ref)
        return len(self.events)


class Ref:
    def __init__(self, channel, id, thread_key):
        self.channel, self.id, self.thread_key = channel, id, thread_key


class FakeFE:
    def __init__(self, store=None):
        self.store = store or FakeStore()
        self.cfg = {}
        self.main_channel = "main"
        self.posts = []
        self.sent = []
        self.dispatched = threading.Event()

    def post(self, channel, text, **kw):
        self.posts.append((channel, text))
        return Ref(channel, f"m{len(self.posts)}", f"t{len(self.posts)}")

    def web_message(self, channel, text, thread_key, nonce=None):
        self.sent.append((channel, thread_key, text))
        return {"id": "o1"}, self.dispatched.set


@pytest.fixture
def env(monkeypatch):
    """No live source: the ledger, the briefing, the rules, the log and the
    keep check are stand-ins; the owner's own model is ready and the
    briefing runs on it."""
    calls = {"local": [], "hosted": []}
    replies = {"local": answer(), "hosted": answer()}

    def local(prompt):
        calls["local"].append(prompt)
        r = replies["local"]
        if isinstance(r, dict):
            return r
        return {"ok": True, "text": r, "usage": {"in": 900, "out": 200}, "reason": ""}

    def hosted(prompt):
        calls["hosted"].append(prompt)
        r = replies["hosted"]
        if isinstance(r, dict):
            return r
        return {"ok": True, "text": r, "usage": {"in": 900, "out": 200}, "reason": "", "model": "codex"}

    monkeypatch.setattr(D, "LOCAL", local)
    monkeypatch.setattr(D, "HOSTED", hosted)
    monkeypatch.setattr(D, "_log", lambda rec: calls.setdefault("log", []).append(rec))
    monkeypatch.setattr(D, "_zone", lambda: ZONE)
    monkeypatch.setattr(D, "_sources", lambda: ({}, ""))
    monkeypatch.setattr(D, "_project", lambda lid, conn=None: None)
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: True)
    monkeypatch.setattr(D, "_remote_cfg", lambda: {"endpoint": "x", "model": "m", "briefings": "remote"})
    monkeypatch.setattr(D, "hosted_name", lambda: "Codex")
    monkeypatch.setattr(D, "_model_name", lambda key: "Your own model" if key == "local" else "Codex")
    levels = {"level": "full"}
    monkeypatch.setattr(D, "_level", lambda item, row: levels["level"])
    import briefing_review
    import ledger
    monkeypatch.setattr(briefing_review, "read_ledger", lambda ids, convs, **kw: {
        "rows": {}, "by_conv": {}, "naming": {},
        "events": {"response-t1": [{"ts": "2026-11-19T08:00:00", "kind": "nudge", "signal": "", "actor": "ea",
                                    "note": "Asked the owner about the rover.", "evidence": "{}"}]}})
    monkeypatch.setattr(ledger, "with_owner_decisions", lambda rows, db_path=None: rows)

    import remote_llm
    monkeypatch.setattr(remote_llm, "local_for", lambda task, switch, cfg=None: bool(switch))
    fe = FakeFE()
    fe._home_deeper = D.Jobs(fe, clock=lambda: NOW, background=False)
    return {"fe": fe, "calls": calls, "replies": replies, "levels": levels, "mp": monkeypatch}


# ── the packet at each keep level ────────────────────────────────────

def test_the_packet_carries_the_records_never_the_mail_and_names_its_days():
    row = make_row()
    led = {"events": {"response-t1": [{"ts": "2026-11-19T08:00", "kind": "nudge", "note": "Asked."}]},
           "by_conv": {"conv-1": [row, {"id": "task-t2", "type": "task", "status": "completed",
                                         "description": "Lend the rover last spring", "closed_at": "2026-04-02",
                                         "metadata": json.dumps({"closed_by": {"signal": "user_stated",
                                                                              "quote": "Done, lent it."}})}]}}
    p = D.build_packet("reply:response-t1", ITEM, row, level="full", now=NOW, zone=ZONE, ledger=led,
                       briefing={}, project={"id": "p1", "name": "Field robots", "summary": "The rover fleet.",
                                             "next": ""}, rules="Keep replies short.", allowed=["draft_reply"])
    text = json.dumps(p)
    assert QUOTE not in text  # a quote from mail never goes
    assert "Robin Vale asked to borrow the rover" in text  # the record's gist does
    assert p["row"]["deadline"] == "2026-12-04 (Friday, December 4)"
    assert p["same_thread"][0]["closed_by_owner"]["said"] == "Done, lent it."
    assert p["project"] == {"name": "Field robots", "summary": "The rover fleet."}
    assert p["steps_you_may_choose"] == ["draft_reply"] and p["owner_rules"] == "Keep replies short."
    assert p["days_since_they_wrote"] >= 4


def test_a_packet_that_does_not_fit_sheds_detail_first(monkeypatch):
    row = make_row(notes="n " * 4000)
    big = {"availability_merged": [{"start": f"2026-11-{d:02d}T16:00:00+00:00",
                                    "end": f"2026-11-{d:02d}T17:00:00+00:00"} for d in range(23, 30)]}

    def build(shed):
        return D.build_packet("reply:response-t1", ITEM, row, level="full", now=NOW, zone=ZONE,
                              briefing=big, rules="r" * 5000, shed=shed)
    full = build(0)
    assert len(full["owner_rules"]) == 2000 and len(full["calendar"]["free_slots"]) == 7
    monkeypatch.setattr(D, "PROMPT_MAX_CHARS", len(json.dumps(full)) - 100)
    p = D.fit_packet(build)
    assert len(json.dumps(p)) <= D.PROMPT_MAX_CHARS
    assert len(p["owner_rules"]) == 1200 and len(p["calendar"]["free_slots"]) <= 5
    monkeypatch.setattr(D, "PROMPT_MAX_CHARS", 900)
    p = D.fit_packet(build)
    assert "calendar" not in p and "owner_rules" not in p and p["row"]["id"] == "response-t1"


def test_a_kept_item_runs_on_the_owners_own_model_alone(env):
    env["levels"]["level"] = "local"
    fe, calls = env["fe"], env["calls"]
    item = dict(ITEM, kept="local")
    assert D.route("local") == ["local"] and D.route("facts") == ["local"]
    assert D.allowed_kinds(item, make_row(), "local") == ["wait_until", "mark_done"]
    env["replies"]["local"] = answer(steps=[{"kind": "mark_done", "say": "", "question": "", "date": "",
                                             "minutes": 0, "ask": "", "why": "done"}])
    got = D.start(fe, item["ref"], item, row=make_row())
    assert got == {"ok": True, "state": "ready"}
    assert len(calls["local"]) == 1 and calls["hosted"] == []
    st = D.state(fe, item["ref"], dict(item, choices=[]), row=make_row())
    assert [s["id"] for s in st["steps"]] == ["s1"]  # no Continue: hosted turns read conversations


def test_a_kept_item_with_no_local_model_shows_the_quick_briefing_alone(env):
    env["mp"].setattr(D, "local_ready", lambda cfg=None: False)
    fe, calls = env["fe"], env["calls"]
    item = dict(ITEM, kept="facts")
    assert D.route("facts", cfg={}) == []
    got = D.start(fe, item["ref"], item, row=make_row())
    assert got["state"] == "unavailable" and calls == {"local": [], "hosted": []}
    st = D.state(fe, item["ref"], item, row=make_row())
    assert st["state"] == "unavailable" and "own model" in st["note"]
    plain(st["note"])
    # a bare `kept: true` is kept too
    assert D.start(fe, item["ref"], dict(ITEM, kept=True), row=make_row())["state"] == "unavailable"


def test_the_route_follows_the_briefings_choice_and_never_hosts_an_unknown_level(env):
    assert D.route("full", cfg={"endpoint": "x", "model": "m", "briefings": "remote"}) == ["local", "hosted"]
    assert D.route("summary", cfg={"endpoint": "x", "model": "m", "briefings": "off"}) == ["hosted", "local"]
    env["mp"].setattr(D, "local_ready", lambda cfg=None: False)
    assert D.route("full", cfg={}) == ["hosted"]
    assert D.route("", cfg={}) == []  # no level said: fail closed


# ── code's check of the words ────────────────────────────────────────

def test_a_sentence_with_a_number_a_day_or_a_name_the_records_lack_cannot_show():
    k = D.Known({"a": "2026-12-04 (Friday, December 4)", "b": "Robin Vale asked about the NSF rover."})
    assert D.checked("Robin Vale asked about the rover.", k)
    assert D.checked("The reply is due Friday, December 4.", k)
    assert D.checked("It went to the NSF office.", k)
    assert not D.checked("The rover costs 4000 dollars.", k)
    assert not D.checked("The reply is due Monday.", k)
    assert not D.checked("It is due in March.", k)
    assert not D.checked("Robin asked Jordan Pike too.", k)
    assert not D.checked("The DOE office asked.", k)
    assert not D.checked("They sent seven files.", k)
    assert not D.checked("See https://example.org for it.", k)
    assert D.checked("He may need it, so they sat down and ate on the sun deck.", k)  # plain words
    assert D.checked("It is due Fri, Dec 4.", k) and not D.checked("It is due Wed, Dec 4.", k)
    assert not D.checked("Write to robin@example.org.", k)


def test_the_answer_keeps_what_code_can_check_and_says_so_for_the_rest(env):
    prep = D.prepare("reply:response-t1", ITEM, make_row(), now=NOW, zone=ZONE)
    obj = json.loads(answer(stakes=["The rover costs 4000 dollars.", "Jordan Pike wants it too.",
                                    "The reply is due by December 4."],
                            steps=[{"kind": "explode", "say": "", "question": "", "date": "", "minutes": 0,
                                    "ask": "", "why": ""}]))
    got = D.check_answer(obj, prep["packet"], allowed=prep["allowed"], today=TODAY, due=prep["due"],
                         cand=prep["cand"])
    assert got["ok"] and got["unchecked"] == 2
    assert "What is at stake\n- Tacitus could not check this.\n- The reply is due by December 4." in got["text"]
    assert "4000" not in got["text"] and "Jordan" not in got["text"]
    assert got["steps"] == [] and got["rejected"] == [{"kind": "explode", "why": "unknown kind"}]
    plain(got["text"])


@pytest.mark.parametrize("text", ["no json here", "{\"history\": 3}", json.dumps(
    {"history": ["Jordan Pike paid 900 dollars."], "stakes": [], "unknown": [], "steps": []})])
def test_an_answer_out_of_shape_or_wholly_unchecked_fails_plainly(env, text):
    env["replies"]["local"] = text
    fe = env["fe"]
    got = D.start(fe, ITEM["ref"], ITEM, row=make_row())
    assert got["state"] == "failed"
    st = D.state(fe, ITEM["ref"], ITEM, row=make_row())
    assert st == {"state": "failed", "note": D.NOTE_UNCHECKED}
    plain(st["note"])


# ── the steps against the gates ──────────────────────────────────────

def test_a_decision_gets_no_draft_and_a_draft_never_answers_for_the_owner(env):
    decide = make_row(brief={"ask": "Decide whether to join the review panel.", "state": "decide"})
    assert "draft_reply" not in D.allowed_kinds(ITEM, decide, "full")
    assert "draft_reply" not in D.allowed_kinds(dict(ITEM, kind="question"), make_row(), "full")
    prep = D.prepare("reply:response-t1", ITEM, make_row(), now=NOW, zone=ZONE)
    k = D.Known(prep["packet"])

    def one(**raw):
        base = {"kind": "", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""}
        return D.check_step(dict(base, **raw), allowed=D.STEP_KINDS, known=k, today=TODAY, due=prep["due"],
                            cand=prep["cand"])
    assert one(kind="draft_reply", say="I accept your invitation to join the board.")[0] is None
    assert one(kind="draft_reply", say="Happy to serve on the committee for the field week.")[0] is None
    assert one(kind="ask_sender", question="Should I decline the field week?")[0] is None
    assert one(kind="work_on_it", ask="Tell them yes and sign up.")[0] is None
    assert one(kind="draft_reply", say="I consent to sharing my details for the field week.")[0] is None
    assert one(kind="draft_reply", say="Pat Moss can lend it.")[1] == "say holds words the records do not"
    ok, _ = one(kind="draft_reply", say="The rover can go out for the field week.")
    assert ok == {"kind": "draft_reply", "params": {"say": "The rover can go out for the field week."}}


def test_dates_and_minutes_must_make_sense(env):
    prep = D.prepare("reply:response-t1", ITEM, make_row(), now=NOW, zone=ZONE)
    k = D.Known(prep["packet"])

    def one(**raw):
        base = {"kind": "", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""}
        return D.check_step(dict(base, **raw), allowed=D.STEP_KINDS, known=k, today=TODAY,
                            due=date(2026, 12, 4), cand=prep["cand"])[0]
    assert one(kind="wait_until", date="2026-11-22") is None  # not after today
    assert one(kind="wait_until", date="2027-06-01") is None  # too far
    assert one(kind="wait_until", date="2026-11-27") == {"kind": "wait_until", "params": {"date": "2026-11-27"}}
    assert one(kind="hold_slot", date="2026-12-10", minutes=60) is None  # after the due date
    assert one(kind="hold_slot", date="2026-11-30", minutes=5) is None
    assert one(kind="hold_slot", date="2026-11-30", minutes=62)["params"] == {"date": "2026-11-30", "minutes": 60}


def test_what_each_item_allows(env):
    row = make_row()
    no_reply = make_row(owner_said=[{"row": "x", "at": "2026-11-01", "said": "No reply.", "no_reply": True}])
    meeting = make_row(brief={"scheduling": "request", "ask": "Find a time to meet."})
    assert D.allowed_kinds(dict(ITEM, kind="request"), None, "full") == ["work_on_it"]
    assert D.allowed_kinds(ITEM, make_row(status="completed"), "full") == ["work_on_it"]
    assert D.allowed_kinds(ITEM, no_reply, "full") == ["wait_until", "mark_done", "work_on_it", "hold_slot"]
    assert D.allowed_kinds(ITEM, meeting, "full")[:3] == ["draft_reply", "propose_times", "ask_sender"]
    assert "propose_times" not in D.allowed_kinds(ITEM, row, "full")
    assert D.allowed_kinds(dict(ITEM, kind="item"), make_row(conversation_id="", metadata="{}"),
                           "full") == ["wait_until", "mark_done", "work_on_it", "hold_slot"]


def test_a_step_runs_as_the_items_own_choice_or_not_at_all(env):
    fe = env["fe"]
    env["replies"]["local"] = answer(steps=[
        {"kind": "draft_reply", "say": "The rover can go out for the field week.", "question": "", "date": "",
         "minutes": 0, "ask": "", "why": ""},
        {"kind": "mark_done", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""},
        {"kind": "wait_until", "say": "", "question": "", "date": "2026-11-27", "minutes": 0, "ask": "",
         "why": ""}])
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    st = D.state(fe, ITEM["ref"], ITEM, row=make_row())
    assert st["state"] == "ready" and st["model"] == "Your own model"
    assert st["steps"] == [{"id": "s1", "label": "Draft a reply: The rover can go out for the field week."},
                           {"id": "s2", "label": "Not needed"},
                           {"id": "s3", "label": "Wait until Fri, Nov 27"},
                           {"id": "continue", "label": "Continue in a conversation"}]
    for s in st["steps"]:
        plain(s["label"])
    assert D.step(fe, ITEM["ref"], "s1", ITEM, row=make_row()) == {
        "choice": "say", "arg": "The rover can go out for the field week."}
    assert D.step(fe, ITEM["ref"], "s2", ITEM, row=make_row()) == {"choice": "not_needed", "arg": None}
    assert D.step(fe, ITEM["ref"], "s3", ITEM, row=make_row()) == {"choice": "later", "arg": "2026-11-27"}
    # an item that offers no reply words (a decision's own buttons) has no draft step
    bare = dict(ITEM, choices=[{"id": "answer:1"}, {"id": "answer"}])
    assert [s["id"] for s in D.state(fe, ITEM["ref"], bare, row=make_row())["steps"]] == ["continue"]
    assert D.step(fe, ITEM["ref"], "s1", bare, row=make_row())["ok"] is False
    # the row moved on: the steps are stale
    moved = make_row(updated_at="2026-11-22T09:00:00", last_action="Replied by phone")
    got = D.step(fe, ITEM["ref"], "s1", ITEM, row=moved)
    assert got == {"ok": False, "said": D.NOTE_STALE_STEP}
    plain(got["said"])


# ── the job ──────────────────────────────────────────────────────────

def test_a_result_stays_until_the_row_or_thread_changes_and_a_repeat_tap_costs_nothing(env):
    fe, calls = env["fe"], env["calls"]
    assert D.state(fe, ITEM["ref"], ITEM, row=make_row()) == {"state": "none"}
    assert D.start(fe, ITEM["ref"], ITEM, row=make_row()) == {"ok": True, "state": "ready"}
    assert D.start(fe, ITEM["ref"], ITEM, row=make_row()) == {"ok": True, "state": "ready"}
    assert len(calls["local"]) == 1
    # a new message on the thread: the result is stale and the next tap runs it again
    newer = make_row(brief={"messages": [{"received": "2026-11-21T10:00", "own": False, "from": "Robin Vale",
                                          "gist": "Robin Vale asked again."}]})
    assert D.state(fe, ITEM["ref"], ITEM, row=newer) == {"state": "none"}
    D.start(fe, ITEM["ref"], ITEM, row=newer)
    assert len(calls["local"]) == 2
    # the quick briefing's day count is not a change
    later = dict(ITEM, brief={"standing": ["Waiting 5 days"]})
    assert D.state(fe, ITEM["ref"], later, row=newer)["state"] == "ready"


def test_a_failed_job_runs_again_on_the_next_tap_and_a_lost_one_reads_failed(env):
    fe, calls = env["fe"], env["calls"]
    env["replies"]["local"] = {"ok": False, "text": "", "usage": {}, "reason": "timeout"}
    env["replies"]["hosted"] = {"ok": False, "text": "", "usage": {}, "reason": "error"}
    assert D.start(fe, ITEM["ref"], ITEM, row=make_row())["state"] == "failed"
    st = D.state(fe, ITEM["ref"], ITEM, row=make_row())
    assert st == {"state": "failed", "note": D.NOTE_FAILED}
    env["replies"]["local"] = answer()
    assert D.start(fe, ITEM["ref"], ITEM, row=make_row())["state"] == "ready"
    # a job the process lost (a restart) reads failed, and the next tap runs it
    j = fe._home_deeper
    rec = fe.store.home_get("deeper")
    rec["jobs"]["item:lost"] = {"ref": "item:lost", "sig": D.signature("item:lost", {}, None),
                                "state": "working", "started": NOW - 3600}
    fe.store.home_set("deeper", rec)
    assert D.state(fe, "item:lost", {}, row=None) == {"state": "failed", "note": D.NOTE_STOPPED}
    assert j.running == set()


def test_one_job_per_item_and_a_small_cap_on_jobs_at_once(env):
    env["mp"].setattr(D, "MAX_RUNNING", 1)
    env["mp"].setattr(D, "MAX_WAITING", 1)
    fe = env["fe"]
    fe._home_deeper = D.Jobs(fe, clock=lambda: NOW, background=True)
    gate = threading.Event()
    asked = []

    def slow(prompt):
        asked.append(prompt)
        gate.wait(10)
        return {"ok": True, "text": answer(), "usage": {}, "reason": ""}
    env["mp"].setattr(D, "LOCAL", slow)
    a = dict(ITEM, ref="reply:a")
    assert D.start(fe, "reply:a", a, row=make_row(id="a"))["state"] == "working"
    assert D.start(fe, "reply:a", a, row=make_row(id="a"))["state"] == "working"
    assert D.start(fe, "reply:b", dict(ITEM, ref="reply:b"), row=make_row(id="b"))["state"] == "working"
    busy = D.start(fe, "reply:c", dict(ITEM, ref="reply:c"), row=make_row(id="c"))
    assert busy == {"ok": True, "state": "failed", "note": D.NOTE_BUSY}
    plain(busy["note"])
    gate.set()
    for _ in range(200):
        if not fe._home_deeper.running:
            break
        time.sleep(0.02)
    assert len(asked) == 2  # one call for a, one for b
    assert D.state(fe, "reply:a", a, row=make_row(id="a"))["state"] == "ready"


def test_the_daily_cap_stops_new_calls_and_says_so_in_plain_words(env):
    fe, calls = env["fe"], env["calls"]
    env["mp"].setattr(D, "DAILY_CAP", 2)
    for n in range(2):
        D.start(fe, f"reply:r{n}", dict(ITEM, ref=f"reply:r{n}"), row=make_row(id=f"r{n}"))
    got = D.start(fe, "reply:r9", dict(ITEM, ref="reply:r9"), row=make_row(id="r9"))
    assert got["state"] == "unavailable" and got["note"] == (
        "You reached today's limit of 2 deeper briefings. They start again tomorrow.")
    plain(got["note"])
    assert len(calls["local"]) == 2
    assert D.state(fe, "reply:r9", ITEM, row=make_row(id="r9"))["note"] == got["note"]
    # a result made before the cap still shows
    assert D.state(fe, "reply:r0", dict(ITEM, ref="reply:r0"), row=make_row(id="r0"))["state"] == "ready"
    # the next day the cap starts over
    fe._home_deeper.clock = lambda: NOW + 86400
    assert D.start(fe, "reply:r9", dict(ITEM, ref="reply:r9"), row=make_row(id="r9"))["state"] == "ready"


def test_a_prewarm_runs_only_on_the_owners_own_model_and_within_its_bound(env):
    fe, calls = env["fe"], env["calls"]
    left = [{"id": "reply:p1", "kind": "reply", "title": "One"}, {"id": "request:q", "kind": "request"},
            {"id": "reply:p3", "kind": "reply", "title": "Three"}]
    env["mp"].setattr(D, "read_row", lambda ref, conn=None: make_row(id=ref.split(":")[1]))
    assert D.prewarm(fe, left) == 0  # inline jobs report ready, not working
    assert len(calls["local"]) == 1  # the card is never prewarmed, the third is past the top two
    D.prewarm(fe, left)
    assert len(calls["local"]) == 1  # one try per version
    # hosted first: no prewarm spends a hosted plan
    env["mp"].setattr(D, "_remote_cfg", lambda: {"endpoint": "x", "model": "m", "briefings": "off"})
    D.prewarm(fe, [{"id": "reply:p4", "kind": "reply", "title": "Four"}])
    assert len(calls["local"]) == 1 and calls["hosted"] == []
    assert D.state(fe, "reply:p4", {"kind": "reply", "title": "Four"},
                   row=make_row(id="p4"))["state"] == "none"
    # its own daily bound
    env["mp"].setattr(D, "_remote_cfg", lambda: {"endpoint": "x", "model": "m", "briefings": "remote"})
    env["mp"].setattr(D, "PREWARM_DAILY", 1)
    D.prewarm(fe, [{"id": "reply:p5", "kind": "reply", "title": "Five"}])
    assert len(calls["local"]) == 1


# ── the fallbacks ────────────────────────────────────────────────────

def test_the_other_model_answers_when_the_first_does_not_and_the_card_says_so(env):
    fe, calls = env["fe"], env["calls"]
    env["replies"]["local"] = {"ok": False, "text": "", "usage": {}, "reason": "RemoteLLMError: circuit open"}
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    st = D.state(fe, ITEM["ref"], ITEM, row=make_row())
    assert st["state"] == "ready" and st["model"] == "Codex"
    assert st["note"] == "Your own model did not answer, so Codex wrote this."
    assert len(calls["local"]) == 1 and len(calls["hosted"]) == 1


def test_a_hosted_plan_at_its_limit_hands_the_call_to_the_owners_model_or_fails_plainly(env):
    fe, calls = env["fe"], env["calls"]
    env["mp"].setattr(D, "_remote_cfg", lambda: {"endpoint": "x", "model": "m", "briefings": "off"})
    env["replies"]["hosted"] = {"ok": False, "text": "", "usage": {}, "reason": "limit"}
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    st = D.state(fe, ITEM["ref"], ITEM, row=make_row())
    assert st["state"] == "ready" and st["note"] == "Codex is at its plan limit, so your own model wrote this."
    assert fe._home_deeper.day()["calls"] == 1  # the plan check made no call
    env["mp"].setattr(D, "local_ready", lambda cfg=None: False)
    ref = "reply:other"
    D.start(fe, ref, dict(ITEM, ref=ref), row=make_row(id="other"))
    st = D.state(fe, ref, dict(ITEM, ref=ref), row=make_row(id="other"))
    assert st == {"state": "failed", "note": "Codex is at its plan limit. Open this again after it resets."}
    plain(st["note"])


def test_a_kept_item_never_falls_back_to_a_hosted_model(env):
    env["levels"]["level"] = "facts"
    fe, calls = env["fe"], env["calls"]
    env["replies"]["local"] = {"ok": False, "text": "", "usage": {}, "reason": "timeout"}
    item = dict(ITEM, kept="facts")
    D.start(fe, item["ref"], item, row=make_row())
    assert calls["hosted"] == []
    assert D.state(fe, item["ref"], item, row=make_row()) == {"state": "failed", "note": D.NOTE_LOCAL_FAILED}


def test_each_job_is_one_log_line_with_no_content(env):
    fe, calls = env["fe"], env["calls"]
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    rec = calls["log"][-1]
    assert rec["ok"] and rec["via"] == "local" and rec["usage"] == {"in": 900, "out": 200}
    assert "rover" not in json.dumps(rec).lower() and ITEM["ref"] not in json.dumps(rec)


# ── the event ────────────────────────────────────────────────────────

def test_the_job_tells_the_open_apps_by_the_home_item_event(env, tmp_path):
    store = wf.WebStore(tmp_path / "web" / "webchat.sqlite")
    try:
        fe = FakeFE(store=store)
        fe._home_deeper = D.Jobs(fe, clock=lambda: NOW, background=False)
        before = store.max_event_seq()
        D.start(fe, ITEM["ref"], ITEM, row=make_row())
        got = store.events_since(before)
        assert [e[1] for e in got] == ["home-item", "home-item"]  # working, then ready
        assert json.loads(got[-1][2]) == {"ref": ITEM["ref"]}
        assert "home-item" in wf.EVENT_TYPES
        assert D.state(fe, ITEM["ref"], ITEM, row=make_row())["state"] == "ready"
    finally:
        store.close()


# ── a conversation from the briefing ─────────────────────────────────

def test_continue_opens_a_conversation_named_by_the_item_with_the_briefing_first(env):
    fe = env["fe"]
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    got = D.step(fe, ITEM["ref"], "continue", ITEM, row=make_row())
    assert got == {"ok": True, "said": "Opened a conversation with the briefing.", "goto": "#/main/t1"}
    channel, text = fe.posts[0]
    first, rest = text.split("\n", 1)
    assert channel == "main" and first == "*Rover loan for the field week*"
    assert "What happened\n- Robin Vale asked on November 18" in rest
    assert "Next steps Tacitus saw: Draft a reply" in rest and "(item response-t1)" in rest
    assert fe.sent == []  # the owner types; nothing is sent for him
    plain(got["said"])


def test_a_long_title_names_the_conversation_in_sixty_characters_cut_at_a_word(env):
    fe = env["fe"]
    long = dict(ITEM, title="Re: Rover loan for the field week and the spare batteries for the second team in December")
    D.start(fe, ITEM["ref"], long, row=make_row())
    D.step(fe, ITEM["ref"], "continue", long, row=make_row())
    first = fe.posts[0][1].split("\n", 1)[0]
    assert first.startswith("*Rover loan") and len(first.strip("*")) <= 60 and "Re:" not in first


def test_continue_goes_to_the_items_project_room_when_it_has_one(env):
    fe = env["fe"]
    env["mp"].setattr(D, "_project", lambda lid, conn=None: {"id": "rovers", "name": "Rovers"})
    import briefing_rooms
    env["mp"].setattr(briefing_rooms, "rooms", lambda **kw: {"rovers": {"channel": "r7", "room": "ea-rovers"}})
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    assert D.step(fe, ITEM["ref"], "continue", ITEM, row=make_row())["goto"] == "#/r7/t1"


def test_a_work_step_opens_the_conversation_and_sends_its_words_for_the_owner(env):
    fe = env["fe"]
    env["replies"]["local"] = answer(steps=[
        {"kind": "work_on_it", "say": "", "question": "", "date": "", "minutes": 0,
         "ask": "Check whether the rover is free for the field week.", "why": ""},
        {"kind": "hold_slot", "say": "", "question": "", "date": "2026-11-30", "minutes": 45, "ask": "",
         "why": ""}])
    D.start(fe, ITEM["ref"], ITEM, row=make_row())
    got = D.step(fe, ITEM["ref"], "s1", ITEM, row=make_row())
    assert got["ok"] and got["goto"] == "#/main/t1"
    assert fe.sent == [("main", "t1", "Check whether the rover is free for the field week.")]
    assert fe.dispatched.wait(2)
    D.step(fe, ITEM["ref"], "s2", ITEM, row=make_row())
    assert fe.sent[-1][2] == ("Hold 45 minutes on Monday, November 30 on my calendar to work on this. "
                              "Put no one else on the hold.")


def test_a_kept_item_never_opens_a_conversation(env):
    env["levels"]["level"] = "local"
    fe = env["fe"]
    item = dict(ITEM, kept="local", choices=[])
    env["replies"]["local"] = answer(steps=[{"kind": "mark_done", "say": "", "question": "", "date": "",
                                             "minutes": 0, "ask": "", "why": ""}])
    D.start(fe, item["ref"], item, row=make_row())
    assert D.step(fe, item["ref"], "continue", item, row=make_row()) == {"ok": False, "said": D.NOTE_STEP_GONE}
    assert fe.posts == []


def test_every_line_the_owner_reads_is_plain():
    for name in dir(D):
        if name.startswith("NOTE_") or name in ("UNCHECKED", "CONTINUE_LABEL", "LOCAL_NAME"):
            plain(getattr(D, name).format(n=100, hosted="Claude"))
    for _key, head, _n in D.SECTIONS:
        plain(head)


# ── the hook home_items calls ────────────────────────────────────────

def test_a_step_names_the_items_own_choice_for_home_items_to_run(env):
    """home_items runs {"choice", "arg"} as that tap (its Undo, its ledger
    verb); a step with words prefers Tell Tacitus what to say, a date
    prefers Waiting on someone, which takes the day."""
    fe = env["fe"]
    todo = dict(ITEM, ref="item:task-t3", kind="item",
                choices=[{"id": "start"}, {"id": "done"}, {"id": "waiting"}, {"id": "later"}])
    row = make_row(id="task-t3", type="task")
    env["replies"]["local"] = answer(steps=[
        {"kind": "wait_until", "say": "", "question": "", "date": "2026-11-27", "minutes": 0, "ask": "", "why": ""},
        {"kind": "mark_done", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""}])
    assert D.start(fe, todo["ref"], todo, row=row)["state"] == "ready"
    st = D.state(fe, todo["ref"], todo, row=row)
    assert [s["label"] for s in st["steps"]] == ["Wait until Fri, Nov 27", "Mark it done",
                                                 "Continue in a conversation"]
    assert D.step(fe, todo["ref"], "s1", todo, row=row, arg="") == {"choice": "waiting", "arg": "2026-11-27"}
    assert D.step(fe, todo["ref"], "s2", todo, row=row, arg="") == {"choice": "done", "arg": None}
    # an item that offers Draft a reply alone still takes the words
    draft_only = dict(ITEM, choices=[{"id": "draft"}])
    env["replies"]["local"] = answer()
    D.start(fe, draft_only["ref"], draft_only, row=make_row())
    assert D.step(fe, draft_only["ref"], "s1", draft_only, row=make_row()) == {
        "choice": "draft", "arg": "Yes, the rover can go out for the field week."}


def test_the_calendar_part_holds_what_is_ahead_up_to_the_due_date():
    def ev(subject, start_h, day=22):
        return {"id": subject, "subject": subject, "isAllDay": False, "showAs": "busy",
                "start": {"dateTime": f"2026-11-{day:02d}T{start_h:02d}:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": f"2026-11-{day:02d}T{start_h + 1:02d}:00:00.0000000", "timeZone": "UTC"}}
    briefing = {"todays_events": [ev("Lab meeting", 15), ev("Seminar", 22)],
                "tomorrows_events": [ev("Site visit", 17, 23)],
                "availability_merged": [{"start": "2026-11-24T16:00:00+00:00", "end": "2026-11-24T17:30:00+00:00"},
                                        {"start": "2026-12-09T16:00:00+00:00", "end": "2026-12-09T17:00:00+00:00"}]}
    cal = D._calendar(briefing, now=NOW, zone=ZONE, until=date(2026, 12, 4))
    assert [e.split(": ")[-1] for e in cal["events"]] == ["Seminar", "Site visit"]  # the 8 AM one is past
    assert cal["free_slots"] == ["Tuesday, November 24 9:00 to 10:30 (90 minutes)"]
