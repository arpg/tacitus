"""The web chat's Home at the bot (preprocessor/home_screen.py, docs/webchat.md,
Home): what is left and in what order, what got done and by whom, the
calendar's next gap, travel and vacation, what changed since the last
visit, the greeting (the templates, the owner's model and its checks, the
quiet line), the vote on it, the check-ins, the `home` event, the routes,
the Settings page, and the briefing handed to Home instead of a post.
Made-up data only (tests/home_fixtures.py)."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))

import chat_notify  # noqa: E402
import ea_config  # noqa: E402
import home_fixtures as F  # noqa: E402
import home_screen as H  # noqa: E402
import owner_trust  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from plain_words import JARGON  # noqa: E402

TODAY = datetime.now(F.ZONE).date()


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "\u2014" not in text and "\u2013" not in text, text


# ── what is left ─────────────────────────────────────────────────────

def test_left_puts_the_urgent_first_and_each_item_opens_its_place():
    got = H.left(F.sources())
    assert [(x["kind"], x["urgency"]) for x in got] == [
        ("request", "soon"), ("reply", "soon"), ("item", "soon"),
        ("request", "calm"), ("reply", "calm"), ("item", "calm")]
    req = got[0]
    assert req["href"] == "#/dm/tw41/w41" and "ask" not in req and req["project"] == "Lab"
    reply = got[1]
    assert reply["detail"] == "From Alex Kim · Waiting 4 days" and reply["ask"] == 'About the reply to "Data access form": '
    item = got[2]
    assert item["title"] == "Send the workshop review" and item["ask"] == 'About "Send the workshop review": '


def test_a_row_a_card_names_is_listed_once_and_a_closed_item_not_at_all():
    src = F.sources()
    card = src.requests["waiting"][0]["card"]
    card["actions"][0]["value"] = json.dumps({"rows": ["response-r2", "deadline-w1"]})
    src.open_ids.discard("task-s2")
    titles = [x["title"] for x in H.left(src)]
    assert "Data access form" not in titles and "Send the workshop review" not in titles
    assert "Send the slides to the committee" not in titles
    assert titles.count("Re: Lab budget") == 1


def test_a_reply_counts_once_it_has_waited_a_day():
    import ledger
    assert H.REPLY_MIN_DAYS == ledger.REPLY_DRAFT_DAYS == 1  # the Replies ladder's first rung
    src = F.sources()
    src.replies.append({"id": "response-r3", "subject": "Came in this morning", "from_name": "Lee",
                        "age_days": 0, "conversation_id": "conv-r3"})
    titles = [x["title"] for x in H.left(src)]
    assert "Came in this morning" not in titles and "Seminar date" in titles
    assert "min_days=REPLY_MIN_DAYS" in (Path(H.__file__)).read_text()


def test_a_request_at_work_is_not_left_for_the_owner():
    src = F.sources()
    src.requests["waiting"][1]["working"] = True
    assert "Planning call" not in [x["title"] for x in H.left(src)]


# ── what got done ────────────────────────────────────────────────────

def test_done_says_whose_doing_each_was_and_counts_a_conversation_once():
    src = F.sources()
    src.closed.append(F.closed("row-c5", "A sibling row of the visit", F.at(10, 41), "detector_sibling", "conv-w40"))
    got = H.done(src)
    by = {d["title"]: (d["kind"], d["by"]) for d in got}
    assert by["Parking permit renewal"] == ("closed", "tacitus")
    assert by["Reply to the visit request"] == ("closed", "you")
    assert by["Room booking for Friday"] == ("closed", "you")
    assert "A sibling row of the visit" not in by
    assert by["Re: Lab budget"] == ("drafted", "tacitus")  # a draft that waits for Send
    assert ("answered", "you") in by.values()
    assert [d["at"] for d in got] == sorted((d["at"] for d in got), reverse=True)


def test_a_sent_draft_is_the_owners_answer_not_another_draft():
    src = F.sources()
    assert not [d for d in H.done(src) if d["kind"] == "drafted" and d["title"] == "A reply"]


def test_yesterdays_closures_are_not_today():
    src = F.sources()
    src.closed = [F.closed("row-old", "Yesterday's work", F.at(16, 0, -1), "user_confirmed")]
    src.requests = {"waiting": [], "done": [F.answered("w30", "Sent to Lee Example.", F.at(17, 0, -1))]}
    assert H.done(src) == []


# ── the calendar ─────────────────────────────────────────────────────

def test_the_next_gap_and_what_fits_in_it():
    p = F.payload()
    assert p["next"]["line"] == ("You have 50 minutes before your 2:00. "
                                 "That is enough for the first 5 things on your list.")
    assert p["next"]["event"]["title"] == "Seminar" and p["next"]["fits"] == 5


def test_in_a_meeting_the_gap_after_it_and_with_nothing_left_the_first_event_tomorrow():
    src = F.sources()
    src.now = F.at(14, 20)
    gap = H.next_gap(src, H.events(src), H.left(src))
    assert gap["line"] == "You are busy until 3:00. After that your day is open."
    src.now = F.at(15, 30)
    gap = H.next_gap(src, H.events(src), H.left(src))
    assert gap["line"] == "Nothing else is on your calendar today. Tomorrow starts at 9:30."
    src.briefing["tomorrows_events"] = []
    assert H.next_gap(src, H.events(src), [])["line"] == "Nothing else is on your calendar today."


def test_back_to_back_meetings_are_one_busy_stretch_and_the_gap_is_the_one_after_it():
    src = F.sources()
    src.now = F.at(11, 40)
    src.briefing["todays_events"] = [F.event("Review", F.at(11, 30), F.at(12, 0)),
                                     F.event("Lunch talk", F.at(12, 0), F.at(13, 0)),
                                     F.event("Office hours", F.at(13, 5), F.at(13, 30)),
                                     F.event("Seminar", F.at(14, 20), F.at(15, 0))]
    gap = H.next_gap(src, H.events(src), H.left(src))
    assert gap["line"].startswith("You are busy until 1:30. Then you have 50 minutes before your 2:20.")
    assert gap["minutes"] == 50 and "have 0 minutes" not in gap["line"]


def test_a_long_gap_is_nothing_until_and_after_midnight_it_is_late():
    src = F.sources()
    src.now = F.at(8, 0)
    src.briefing["todays_events"] = src.briefing["todays_events"][1:]  # the 2:00 alone
    gap = H.next_gap(src, H.events(src), H.left(src))
    assert gap["line"] == "Nothing is on your calendar until your 2:00. That is enough for everything on your list."
    src.now = F.at(0, 14)
    assert H.part_of_day(src) == "night"
    assert H.next_gap(src, H.events(src), H.left(src))["line"] == "It is late. Your calendar starts at 2:00."
    assert [H.part_of_day(H.Sources(now=F.at(h), zone=F.ZONE)) for h in (3, 4, 11, 12, 16, 17, 21, 22)] == [
        "night", "morning", "morning", "afternoon", "afternoon", "evening", "evening", "night"]


def test_a_declined_or_free_event_leaves_the_gap_open():
    src = F.sources()
    src.briefing["todays_events"][1]["responseStatus"] = {"response": "declined"}
    gap = H.next_gap(src, H.events(src), H.left(src))
    assert "2:00" not in gap["line"]


def test_an_evening_event_is_on_its_own_day_whatever_the_briefing_said():
    src = F.sources()
    late = F.event("Dinner", F.at(19, 30), F.at(21, 0))
    src.briefing["tomorrows_events"] = [late]  # yesterday's briefing filed it under tomorrow
    assert "Dinner" in [e["title"] for e in H.build(src)["calendar"]]


# ── travel and vacation ──────────────────────────────────────────────

def test_a_vacation_block_shows_only_what_is_on_fire():
    p = F.payload("vacation")
    assert p["mode"]["kind"] == "vacation" and p["mode"]["until"] == (TODAY + timedelta(days=3)).isoformat()
    assert p["left"] == [] and len(p["held"]) == 6 and p["tally"]["left"] == 0
    assert p["mode"]["line"].startswith("You are off until ") and p["next"] is None
    assert p["greeting"]["text"] in ("Nothing is on fire. Go enjoy your time off.",
                                     "Nothing is on fire. Close the app and go.")
    src = F.sources("vacation")
    src.briefing["today_top"][0]["title"] = f"Due {(TODAY - timedelta(days=1)).isoformat()}: Send the workshop review"
    p = H.build(src)
    assert [x["title"] for x in p["left"]] == ["Send the workshop review"]
    assert p["mode"]["line"].startswith("You are off until ")
    assert p["greeting"]["plain"]  # something overdue: the plain line


def test_out_of_office_with_no_word_is_a_vacation_and_a_conference_is_travel():
    src = F.sources()
    src.briefing["todays_events"] = [F.all_day("Busy", TODAY, 1, show="oof")]
    assert H.mode(src, H.events(src))["kind"] == "vacation"
    src.briefing["todays_events"] = [F.all_day("Robotics conference", TODAY, 2, show="free")]
    assert H.mode(src, H.events(src)) == {"kind": "travel", "until": (TODAY + timedelta(days=1)).isoformat(),
                                          "why": "calendar"}
    src.briefing["todays_events"] = [F.all_day("Office hours", TODAY, 1, show="free")]
    assert H.mode(src, H.events(src))["kind"] == "normal"


def test_travel_shows_what_cannot_wait_until_the_owner_is_back():
    p = F.payload("travel")
    assert (p["mode"]["kind"], p["mode"]["why"]) == ("travel", "travel mode")
    assert [x["title"] for x in p["left"]] == ["Re: Lab budget", "Send the workshop review"]
    assert len(p["held"]) == 4
    assert "Only what cannot wait shows here." in p["mode"]["line"] and "4 other things" in p["mode"]["line"]


def test_a_phone_in_another_time_zone_is_travel_and_an_old_trip_is_over():
    src = F.sources()
    src.device_zone = "Asia/Tokyo"
    assert H.mode(src, H.events(src)) == {"kind": "travel", "until": "", "why": "time zone"}
    src.device_zone = F.ZONE_NAME
    assert H.mode(src, H.events(src))["kind"] == "normal"
    src.travel = {"enabled": True, "timezone": "Europe/Lisbon", "until": (TODAY - timedelta(days=1)).isoformat()}
    assert H.mode(src, H.events(src))["kind"] == "normal"


# ── since the last visit ─────────────────────────────────────────────

def test_since_counts_from_the_visit_before_and_links_where_it_can():
    p = F.payload()
    lines = [x["text"] for x in p["since"]["lines"]]
    assert lines == ["2 new requests in Action requests.", "Tacitus drafted 1 reply.", "Tacitus closed 1 item.",
                     "You finished 3 things.", "1 conversation has a new answer."]
    assert p["since"]["lines"][0]["href"] == "#/dm/tw41/w41"
    src = F.sources()
    src.checkin = {}
    assert H.build(src)["since"] is None
    src.checkin = {"prev": src.now - 3600}
    src.reports = [{"kind": "afternoon-catchup", "text": "x", "at": src.now - 600}]
    assert {"text": f"Your afternoon update came in at {H.clock(src.now - 600, F.ZONE)}.",
            "href": "#/home/afternoon-catchup"} in H.build(src)["since"]["lines"]


# ── the greeting ─────────────────────────────────────────────────────

FACTS = {"part_of_day": "afternoon", "weekday": "Thursday", "done_today": 7, "done_by_tacitus": 3, "left": 6,
         "overdue": 0, "replies": 2, "taps": 2, "todos": 2, "mode": "normal", "held": 0, "light": "ok",
         "minutes_free": 50, "next_at": "2:00"}


@pytest.mark.parametrize("tone", H.TONES)
@pytest.mark.parametrize("change", [{}, {"left": 0}, {"left": 1}, {"done_today": 0}, {"overdue": 2},
                                    {"light": "problem"}, {"mode": "travel", "held": 3},
                                    {"mode": "vacation", "left": 0, "held": 4}, {"part_of_day": "evening"},
                                    {"part_of_day": "morning"}, {"part_of_day": "night"}])
def test_every_template_line_is_plain_and_holds_only_the_facts_numbers(tone, change):
    f = {**FACTS, **change}
    for day in ("2026-01-01:a", "2026-01-02:b", "2026-01-03:c"):
        line = H.template(f, tone, day)
        plain(line)
        assert H.checked_line(line, f) == line, line


def test_quiet_means_no_jokes_and_facts_only_never_asks_the_model():
    assert H.template({**FACTS, "overdue": 2}, "breezy", "k") == "2 things are overdue. Start there."
    assert H.template({**FACTS, "light": "problem"}, "dry", "k") == "Tacitus needs a look. 6 things wait for you."
    assert H.template(FACTS, "facts", "k") == "7 done today. 6 wait for you."
    for state in ("red", "overdue"):
        assert F.payload(state)["greeting"]["plain"] is True


def test_the_models_line_is_kept_only_when_it_is_one_short_line_of_the_facts():
    ok = "Afternoon. 6 left and 50 minutes before your 2:00."
    assert H.checked_line(f' "{ok}" ', FACTS) == ok
    for bad in ("You have 9 things left.", "Line one.\nLine two.", "x" * 200, "Six left \u2014 go.",
                "Mail Pat at pat@example.org.", "**Bold** move.", "Read https://example.org now."):
        assert H.checked_line(bad, FACTS) == "", bad


@pytest.mark.parametrize("line", [
    "Afternoon. Two replies and a tap stand between you and a quiet evening.",  # 3 of 6
    "Two replies wait for you.", "Just a tap or two left.", "A few replies and you are done.",
    "Some quick taps left.", "Three things left.", "Tacitus handled 4 things. 6 left.",
    "6 things left, among them 3 replies.", "Seven left. Go.", "One reply, then coffee."])
def test_a_line_that_misstates_the_list_is_dropped(line):
    """The owner's model may speak of what is left only as the whole, or with
    parts that are each true and add up to it."""
    assert H.checked_line(line, FACTS) == "", line


@pytest.mark.parametrize("line", [
    "Afternoon. 6 things left and 50 minutes before your 2:00.",
    "Six things to go. None of them bite.", "Two replies, two taps and two to-dos. That is the lot.",
    "Tacitus handled 7 things. 6 are yours.", "Tacitus took care of 3 items, and six things wait.",
    "6 things left, 2 of them replies.", "Afternoon. Plenty of room before your 2:00.", "Halfway there."])
def test_a_line_true_to_the_list_is_kept(line):
    assert H.checked_line(line, FACTS) == line, line


def test_the_overdue_and_held_counts_are_checked_where_a_line_says_them():
    f = {**FACTS, "overdue": 1, "held": 4}
    assert H.checked_line("1 thing is overdue. 6 things in all.", f)
    assert H.checked_line("2 things are overdue.", f) == ""
    assert H.checked_line("Tacitus holds 4 things for when you are back.", f)
    assert H.checked_line("Tacitus holds 3 things for later.", f) == ""


def test_the_prompt_holds_the_numbers_and_the_votes_never_a_name():
    p = F.payload()
    prompt = H.greeting_prompt(p["facts"], "breezy", ["Liked line."], ["Disliked line."])
    assert json.dumps(p["facts"], sort_keys=True) in prompt
    for name in ("Pat", "Alex", "Sam", "Lab budget", "Seminar", "workshop"):
        assert name not in prompt
    assert "Liked line." in prompt and "Disliked line." in prompt
    assert "What is left is 6 things in all. Speak of it as that whole, or stay general." in prompt


class FakeStore:
    def __init__(self):
        self.meta: dict = {}
        self.events: list = []

    def home_get(self, name):
        return dict(self.meta.get(name) or {})

    def home_set(self, name, value):
        self.meta[name] = dict(value)


class FakeFE:
    def __init__(self):
        self.store = FakeStore()
        self.cfg = {}
        self.changed = 0

    def home_changed(self):
        self.changed += 1


def _home(state="normal", model=None, calibrate=None, clock=None, sources=None):
    fe = FakeFE()
    src = sources or (lambda tz: F.sources(state))
    return fe, H.Home(fe, clock=clock or (lambda: F.at(13, 10)), sources=src, model=model,
                      calibrate=calibrate, background=False)


def test_the_models_line_replaces_the_template_once_it_answers_and_a_bad_one_never_does():
    asked = []
    fe, home = _home(model=lambda prompt: asked.append(prompt) or "Afternoon. 6 left, then a quiet evening.")
    first = home.payload()
    assert first["greeting"]["by"] == "code"  # Home never waits: the model's line lands after it
    assert len(asked) == 1 and fe.changed == 1
    again = home.payload("", checkin=True)
    assert again["greeting"] == {"text": "Afternoon. 6 left, then a quiet evening.", "by": "model",
                                 "tone": "breezy", "plain": False}
    fe, home = _home(model=lambda prompt: "You have 12 meetings today!")
    home.payload()
    assert home.payload("", checkin=True)["greeting"]["by"] == "code" and fe.changed == 0


def test_no_model_is_asked_for_the_plain_line_or_more_than_once_in_a_while():
    asked = []
    _fe, home = _home("red", model=lambda p: asked.append(p) or "x")
    home.payload()
    assert asked == []
    _fe, home = _home(model=lambda p: asked.append(p) or None)
    home.payload()
    home.payload("Asia/Tokyo")
    assert len(asked) == 1


def test_a_vote_is_a_calibration_line_under_home_and_a_down_vote_drops_the_models_line():
    got = []
    fe, home = _home(calibrate=lambda scope, note: got.append((scope, note)))
    fe.store.meta["greeting"] = {"key": "k", "text": "A line."}
    assert home.vote(True, "Afternoon. 6 left.") == {"ok": True, "vote": "up"}
    assert home.vote(False, "Six   left.") == {"ok": True, "vote": "down"}
    assert got == [("home", 'Liked the Home greeting (breezy): "Afternoon. 6 left."'),
                   ("home", 'Did not like the Home greeting (breezy): "Six left."')]
    assert fe.store.meta["greeting"] == {}


def test_the_votes_feed_the_next_prompt(monkeypatch):
    import preferences
    monkeypatch.setattr(preferences, "select", lambda scopes: {"calibration": [
        {"scope": "home", "note": 'Liked the Home greeting (breezy): "Up line."'},
        {"scope": "home", "note": 'Did not like the Home greeting (dry): "Down line."'}]})
    assert H._votes() == (["Up line."], ["Down line."])
    assert "home" in preferences.SCOPES


# ── check-ins and the event ──────────────────────────────────────────

def test_a_visit_moves_the_check_in_and_a_quick_return_does_not():
    now = [F.at(8, 0)]
    fe, home = _home(clock=lambda: now[0])
    assert home.checkin() == {"prev": None, "last": now[0]}
    now[0] += 600
    assert home.checkin()["prev"] is None  # ten minutes later: the same visit
    now[0] += H.VISIT_GAP_S + 1
    got = home.checkin()
    assert got["prev"] == F.at(8, 0) + 600 and got["last"] == now[0]


def test_the_home_event_goes_out_only_when_home_changed():
    state = ["normal"]
    now = [F.at(13, 10)]
    fe, home = _home(sources=lambda tz: F.sources(state[0]), clock=lambda: now[0])
    home.tick()
    assert fe.changed == 1
    now[0] += 60  # a minute on: the gap says 49 minutes, but nothing else moved
    home.tick()
    assert fe.changed == 1
    state[0] = "done"
    home.tick()
    assert fe.changed == 2


def test_each_payload_word_is_plain():
    for state in F.STATES:
        p = F.payload(state)
        words = [p["greeting"]["text"], p["mode"].get("line", ""), (p["next"] or {}).get("line", ""), p["day"]]
        words += [x["label"] for x in p["left"] + p["held"]] + [x.get("detail", "") for x in p["left"]]
        words += [x["text"] for x in (p["since"] or {}).get("lines", [])]
        for w in words:
            plain(w)


# ── the routes ───────────────────────────────────────────────────────

from test_web_frontend import call, make, owner  # noqa: E402


class StaticHome:
    def __init__(self):
        self.asked = []
        self.votes = []

    def payload(self, tz="", checkin=False):
        self.asked.append((tz, checkin))
        return {"greeting": {"text": "Hello."}, "tally": {"done": 1, "left": 2}}

    def vote(self, up, text):
        self.votes.append((up, text))
        return {"ok": True, "vote": "up" if up else "down"}

    def tick(self):
        return {}


@pytest.fixture
def served(tmp_path):
    f = make(tmp_path)
    f.home = StaticHome()
    f.start()
    yield f
    f.stop()


def test_the_routes_serve_home_to_any_device_that_reads_and_take_a_vote(served):
    status, _h, raw = owner(served, "GET", "/api/me")
    assert json.loads(raw)["home"] is True
    status, _h, raw = owner(served, "GET", "/api/home?tz=Europe%2FLisbon&checkin=1")
    assert status == 200 and json.loads(raw)["tally"] == {"done": 1, "left": 2}
    relay = {"X-Tacitus-Relay": "0123456789abcdef"}
    assert owner(served, "GET", "/api/home", headers=relay)[0] == 200
    assert served.home.asked == [("Europe/Lisbon", True), ("", False)]
    assert owner(served, "POST", "/api/home/vote", {"vote": "up", "text": "Hello."}, relay)[0] == 200
    assert served.home.votes == [(True, "Hello.")]
    for bad in ({"vote": "maybe", "text": "x"}, {"vote": "up"}, {"vote": "down", "text": " "}):
        assert owner(served, "POST", "/api/home/vote", bad)[0] == 400
    assert call(served, "GET", "/api/home")[0] == 401  # not the owner
    assert owner(served, "GET", "/api/home/other")[0] == 404


def test_no_home_answers_404_and_the_event_reaches_the_stream(tmp_path):
    f = make(tmp_path)
    f.start()
    try:
        assert owner(f, "GET", "/api/home")[0] == 404
        assert json.loads(owner(f, "GET", "/api/me")[2])["home"] is False
        before = f.store.max_event_seq()
        f.home_changed()
        got = f.store.events_since(before)
        assert [e[1] for e in got] == ["home"]
        assert "home" in wf.EVENT_TYPES
    finally:
        f.stop()


def test_the_store_keeps_homes_records_and_its_tick(tmp_path):
    s = wf.WebStore(tmp_path / "web" / "webchat.sqlite")
    try:
        assert s.home_get("checkin") == {} and s.home_alive(300) is False
        s.home_set("checkin", {"prev": 1.0, "last": 2.0})
        assert s.home_get("checkin") == {"prev": 1.0, "last": 2.0}
        s.home_mark()
        assert s.home_alive(300) is True
        s.home_report("morning-briefing", "The text.", at=5.0)
        assert s.home_get("report:morning-briefing") == {"text": "The text.", "at": 5.0}
    finally:
        s.close()


# ── Settings: Home ───────────────────────────────────────────────────

def test_the_settings_page_checks_and_saves_the_web_blocks_home(monkeypatch):
    import web_notify_settings as wns
    block = {"web": {"port": 1, "home": {"tone": "dry"}}}
    monkeypatch.setattr(wns, "_rewrite", lambda change, what: change(block))
    fe = FakeFE()
    fe.cfg = dict(block["web"])
    fe.home = H.Home(fe, sources=lambda tz: F.sources(), background=False)
    page = H.HomeSettings(fe)
    assert page.get() == {"tone": "dry", "briefing": "home"}
    assert page.save({"briefing": "post"}) == {"tone": "dry", "briefing": "post", "saved": True}
    assert block["web"] == {"port": 1, "home": {"tone": "dry", "briefing": "post"}}
    assert fe.home.settings == {"tone": "dry", "briefing": "post"} and fe.changed == 1
    for bad in ({}, {"tone": "loud"}, {"briefing": "slack"}, {"colour": "red"}, "tone"):
        with pytest.raises(ValueError) as e:
            page.save(bad)
        plain(str(e.value))
    assert H.home_settings({"home": {"tone": "nope", "briefing": "nope"}}) == {"tone": "breezy", "briefing": "home"}


# ── the briefing on Home ─────────────────────────────────────────────

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """An owner process on the web chat alone; each push recorded."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(private))
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", tmp_path / "ea-config.json")
    import chat
    monkeypatch.setattr(chat.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [])
    pushes = []
    monkeypatch.setattr(wf.WebFrontend, "notify", lambda self, *a, **kw: pushes.append((a, kw)) or {"sent": 1})
    store = wf.WebStore(private / "webchat.sqlite")

    def block(frontend="web", home=None, briefings=True):
        chat_notify._ROOM_FE.clear()
        web_block = {"port": 0, "notify_push": {"briefings": briefings, "quiet": "send"}}
        if home is not None:
            web_block["home"] = home
        _write(private / "chat.json", {"frontend": frontend, "web": web_block})
    block()
    yield types.SimpleNamespace(store=store, block=block, pushes=pushes)
    store.close()


def _main(store):
    return [m["text"] for m in store.threads("main")]


def test_the_briefing_goes_to_home_with_one_push_and_no_post(web, capsys):
    web.store.home_mark()
    text = ":sunny: *Morning Briefing, today*\n1. A thing." + chat_notify.SPLIT_MARKER + "Part two."
    assert chat_notify.post("ea-assistant", text, home="morning-briefing") is True
    assert _main(web.store) == []
    kept = web.store.home_get("report:morning-briefing")
    assert kept["text"] == ":sunny: **Morning Briefing, today**\n1. A thing.\n\nPart two."  # as a post shows it
    (args, kw), = web.pushes
    assert args[0] == "Your morning is ready" and args[1] == "Tap to see your day."
    assert args[2].endswith("#/home") and args[3] == "home" and kw.get("at") is None
    assert "[home]" in capsys.readouterr().out
    assert web.store.events_since(0)[-1]


def test_the_briefing_posts_as_before_when_home_is_off_not_running_or_on_slack(web, monkeypatch):
    assert chat_notify.post("ea-assistant", "Morning Briefing one", home="morning-briefing", _bypass_quiet=True)
    assert _main(web.store) == ["Morning Briefing one"]  # Home has not ticked
    web.store.home_mark()
    web.block(home={"briefing": "post"})
    assert chat_notify.home_carries("morning-briefing") is False
    assert chat_notify.post("ea-assistant", "Morning Briefing two", home="morning-briefing", _bypass_quiet=True)
    assert "Morning Briefing two" in _main(web.store)
    assert chat_notify.post("ea-assistant", "Weekly audit", home="weekly-audit", _bypass_quiet=True)
    assert "Weekly audit" in _main(web.store)
    web.block(frontend="slack")
    sent = []
    monkeypatch.setattr(chat_notify, "_send_slack", lambda key, parts, blocks: sent.append(parts) or True)
    assert chat_notify.home_carries("morning-briefing") is False
    assert chat_notify.post("ea-assistant", "Morning Briefing three", home="morning-briefing", _bypass_quiet=True)
    assert sent == [["Morning Briefing three"]]
    assert [a for a, _kw in web.pushes if a[3] == "home"] == []  # the posts' own pushes, never Home's


def test_the_briefings_switch_off_keeps_home_quiet(web):
    web.block(briefings=False)
    web.store.home_mark()
    assert chat_notify.post("ea-assistant", "Afternoon Update", home="afternoon-catchup") is True
    assert web.store.home_get("report:afternoon-catchup")["text"] == "Afternoon Update"
    assert web.pushes == []


def test_run_agent_names_the_two_reports_home_carries():
    src = (REPO / "run-agent.sh").read_text()
    assert 'case "$AGENT" in morning-briefing|afternoon-catchup) HOME_KIND="$AGENT" ;; esac' in src
    assert "slack.post_to_slack('ea-assistant', msg, home=sys.argv[1], home_run=sys.argv[2])" in src
    assert '" "$HOME_KIND" "cron-${AGENT}-${TIMESTAMP}" <<<' in src  # the remote run's own id
    assert set(chat_notify.HOME_KINDS) == set(H.REPORTS) == {"morning-briefing", "afternoon-catchup"}


def test_home_shows_todays_reports_the_newest_first_and_none_from_yesterday():
    src = F.sources()
    src.reports.append({"kind": "afternoon-catchup", "text": "The update.", "at": F.at(15, 2)})
    assert [r["title"] for r in H.build(src)["reports"]] == ["This afternoon's update", "This morning's briefing"]
    src.reports = [{**src.reports[0], "at": F.at(16, 0, -1)}]
    assert H.build(src)["reports"] == []


# ── Home lists what the post lists (the pre-send review, the rules upstream) ──

@pytest.fixture
def remote(tmp_path, monkeypatch):
    """The remote briefing on made-up data, as tests/test_briefing_review.py
    runs it: an owner-private store, no network, no model, no Graph."""
    import remote_briefing as rb
    import remote_llm as rl
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "kit@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    monkeypatch.setattr(rb, "rail", lambda c, says, **k: "")
    monkeypatch.setattr(rb, "write_reply", lambda c, says, write=None: {"ok": True, "body": says})
    monkeypatch.setattr(rb, "stage_reply", lambda c, body, run_id, **k: {"ok": True, "draft_id": "d-" + c["message_id"]})
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "drafting": "remote", "briefings": "remote", "shadow_until": "2099-01-01"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return rb


MAIL = [{"message_id": f"m-{k}", "conversation_id": f"c-{k}", "subject": subj, "from": f"{who.lower()}@example.edu",
         "from_name": who, "body_preview": ask, "remote_record": {"ask": ask}}
        for k, subj, who, ask in (("a", "Talk on Friday", "Pat", "Can you give the talk?"),
                                  ("b", "Slides", "Lee", "Send the slides."),
                                  ("c", "Paper section", "Ana", "Write the section."))]


def _morning(rb, review, monkeypatch):
    """One morning: a do-today item (R3) and two replies written (R1, R2);
    the review stand-in decides; the post and the run record come back."""
    b = {"new_needs_response": [dict(m) for m in MAIL], "open_obligations": [], "delta_summary": {},
         "recently_closed": []}
    plan = {"get_done_today": [{"ref": "R3", "why": "the section is due"}], "flexes": [], "needs_you": [],
            "drafts": [{"ref": "R1", "reply_must_say": "Yes."}, {"ref": "R2", "reply_must_say": "Here they are."}],
            "escalate": []}
    chat = lambda messages, **kw: (json.loads(json.dumps(plan)), {})  # noqa: E731
    return rb.run("morning-briefing", b, run_id="cron-morning-briefing-t1", now=datetime.now(F.ZONE), chat=chat,
                  review=review, queue=lambda items, agent, now: len(items), rooms=lambda *a, **k: [],
                  mark=lambda *a, **k: True)


def _home_after(text, view):
    """Home on the morning the post came from: the staged reply's Send card
    waits in Action requests, and the reply queue still names all three
    threads, as it does until the owner acts."""
    src = F.sources()
    src.reports = [{"kind": "morning-briefing", "text": text, "at": F.at(5, 0), "view": view}]
    send = F.request("w51", "send", "Send email", "Re: Slides", "To Lee")
    send["card"]["actions"][0]["value"] = json.dumps({"conversation_id": "c-b"})
    src.requests = {"waiting": [send], "done": []}
    src.replies = [{"id": "", "subject": m["subject"], "from_name": m["from_name"], "age_days": 2,
                    "conversation_id": m["conversation_id"]} for m in MAIL]
    return H.build(src)


def test_the_review_drops_one_item_and_holds_one_draft_and_home_shows_neither_as_work_or_a_draft(remote, monkeypatch):
    def review(agent, text, b, **kw):
        return {"closed": [], "closed_rows": [], "unsure_rows": [], "parked": [],
                "review": {"status": "ok", "model": "codex", "hold": {"R1": "the choice is his"},
                           "drop": {"R3": {"kind": "not-owners", "why": "Ana writes it"}},
                           "footer": ["", ":mag: *Checked before sending* by Codex."]}}
    text, rec = _morning(remote, review, monkeypatch)
    view = rec["home"]
    assert {g["conversation_id"]: g["kind"] for g in view["gone"]} == {"c-c": "not-owners", "c-a": "held"}
    assert "Paper section" not in text and "I held the reply I wrote: the choice is his." in text
    p = _home_after(text, view)
    every = p["left"] + p["held"]
    # the dropped item is nowhere: not as work, not as its reply
    assert not [x for x in every if "Paper section" in x["title"] + x.get("detail", "")]
    # the held draft is its question, never a draft ready, never a Send card
    talk = [x for x in every if "Talk on Friday" in x["title"] + x.get("detail", "")]
    assert [(x["kind"], x["label"]) for x in talk] == [("question", "Answer needed")]
    assert talk[0]["title"].startswith("I held the reply I wrote: the choice is his.")
    assert not [d for d in p["done"] if d["kind"] == "drafted" and "Talk" in d["title"]]
    # the staged reply is its Send card, once
    slides = [x for x in every if "Slides" in x["title"]]
    assert [x["kind"] for x in slides] == ["request"]
    # the briefing's own ranking is not Home's list once the post's is there
    assert "Send the workshop review" not in [x["title"] for x in every]
    assert p["unchecked"] == ""
    # the run kept the list for Home; the handoff takes it only for its own run
    remote.write_home("morning-briefing", "cron-morning-briefing-t1", view)
    assert remote.home_handoff("morning-briefing", "cron-morning-briefing-t1") == view
    assert remote.home_handoff("morning-briefing", "cron-morning-briefing-t0") is None
    assert remote.home_handoff("afternoon-catchup", "cron-morning-briefing-t1") is None


def test_a_review_that_did_not_run_gives_home_the_posts_own_words(remote, monkeypatch):
    import briefing_review as br

    def review(agent, text, b, **kw):
        return {"closed": [], "closed_rows": [], "unsure_rows": [], "parked": [],
                "review": {"status": "failed", "reason": "timeout", "model": "codex",
                           "footer": br.footer({"status": "failed", "reason": "timeout", "model": "codex"}, [])}}
    text, rec = _morning(remote, review, monkeypatch)
    note = rec["home"]["review"]["note"]
    assert note == "Not checked before sending: Codex did not answer in time. Tacitus's own checks ran."
    assert f":warning: *Not checked before sending*: {note.split(': ', 1)[1]}" in text
    p = _home_after(text, rec["home"])
    assert p["unchecked"] == note
    plain(note)
    # unchecked, the post's items are Home's: the do-today item shows as the post shows it
    assert [x["label"] for x in p["left"] if "Paper section" in x["title"]] == ["To do"]


def test_without_a_view_home_keeps_the_upstream_rules():
    """No run kept a list (the Claude lane, the review off): the briefing's
    ranking, less a row others owe before their date, a row merged into
    another and a thread the owner said gets no reply."""
    src = F.sources()
    src.reports = []
    src.briefing["open_obligations"] = [{"id": "task-s2", "merged_into": "deadline-w1"}]
    src.rows = {"deadline-w1": {"id": "deadline-w1", "type": "deadline", "status": "open",
                                "description": "Send the workshop review",
                                "owner_said": [{"said": "Not doing it. No response.", "no_reply": True}]}}
    titles = [x["title"] for x in H.left(src)]
    assert "Send the workshop review" not in titles and "Send the slides to the committee" not in titles
    src.rows = {"deadline-w1": {"id": "deadline-w1", "type": "waiting", "status": "open",
                                "description": "The panel sends its review", "deadline": "2099-01-01"}}
    src.briefing["open_obligations"] = []
    titles = [x["title"] for x in H.left(src)]
    assert "Send the workshop review" not in titles and "Send the slides to the committee" in titles


def test_a_reply_an_earlier_run_turned_into_a_question_is_that_question_and_a_kept_one_says_only_the_fixed_words():
    src = F.sources()
    src.replies = [
        {"id": "r-q", "subject": "Committee seat", "from_name": "Sam", "age_days": 2, "conversation_id": "c-q",
         "queued": {"outcome": "decision", "note": "Join the committee? Yes or no."}},
        {"id": "r-l", "subject": "Thanks note", "from_name": "Lee", "age_days": 2, "conversation_id": "c-l",
         "queued": {"outcome": "left", "note": "No reply needed."}},
        {"id": "r-n", "subject": "Survey", "from_name": "Jo", "age_days": 2, "conversation_id": "c-n",
         "owner_said": [{"said": "No reply.", "no_reply": True}]},
        {"id": "r-k", "subject": "Applicant question", "from_name": "Kim", "age_days": 4, "conversation_id": "c-k",
         "kept": True, "ask": "words from the kept mail"}]
    got = {x["id"]: x for x in H.left(src)}
    assert (got["reply:r-q"]["kind"], got["reply:r-q"]["label"], got["reply:r-q"]["title"]) == \
        ("question", "Answer needed", "Join the committee? Yes or no.")
    assert "reply:r-l" not in got and "reply:r-n" not in got
    kept = got["reply:r-k"]
    assert (kept["title"], kept["detail"]) == ("Applicant question", "Kept mail · Waiting 4 days")
    assert "Kim" not in str(kept) and "kept mail" not in str({k: v for k, v in kept.items() if k != "detail"})
