"""One date per case, then quiet until it is time (preprocessor/case_dates.py,
cases.py, case_surface.py, letters_work.py and the bot's case cards).

The owner's fault this pins, in made-up data: a letter case's block asked
"Set the due date" for each place with no date; the web card had one field
of words, labelled for Draft, so a press of the date button sent that
empty field and got "I did not find a date in that" three times; with a
date typed, the answer asked which place it was for; every failed press
posted the block again; two places from portal mail had no name, so the
card listed ": no due date" and two buttons "Submitted: " with nothing
after; a Yes on a case with two places due the same day crashed
comparing two dicts; and every reminder, every press and every new
message brought the same block back.

What holds now:
  1. One date per case, given with the yes ("Yes, by Tue Dec 1", "Yes, by
     another date" as a date picker); with no date stated the yes asks
     for one. A date is never asked twice, never per place, and words with
     no day in them change nothing and loop on nothing.
  2. A place that comes later joins silently under the date; one with an
     earlier date of its own moves the case's date and says so once.
  3. A dated case is quiet (no card, not on Home, on no daily list, on the
     weekly review) until its lead time; then one block, "Time to write
     the letter"; never again in the same state.
  4. A ready letter is one delivery list with Submitted per place and All
     submitted; a place that asks after it is ready is one line.
  5. A part always has a name.
  6. The Yes crash is gone.
  7. A case from before one date per case gets one date card.
Every name, address and place here is made up.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))
sys.path.insert(2, str(ROOT / "botcore"))
sys.path.insert(3, str(ROOT / "tests"))

import case_dates as D  # noqa: E402
import cases  # noqa: E402
import closure  # noqa: E402
import ledger  # noqa: E402
import letters  # noqa: E402
import letters_work as LW  # noqa: E402
from letters_fake import FakeStore, a_case, dest  # noqa: E402

NOW = "2026-11-12T00:00:00Z"
NO_PLUGINS = ([], [])


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            c.execute(m)
        except sqlite3.OperationalError:
            pass
    letters.ensure_schema(c)
    closure.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _ea(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setattr(LW, "EA_DIR", tmp_path)
    monkeypatch.setattr(LW, "owner_rules", lambda: [])
    return tmp_path


def _add(c, rid, *, conv="", sender=""):
    c.execute(
        "INSERT INTO ledger (id, type, status, description, from_email, source_message_id, "
        "conversation_id, date_identified, deadline, last_action, notes, metadata, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, "task", "open", f"desc {rid}", sender, f"msg-{rid}", conv, NOW, None, "", "", "{}", NOW, NOW))


def _portal(mid, person, place="", *, sender="gradapply@northfield.example", due="", name=""):
    """A portal's request on the person's behalf. With no `place`, the mail
    names none (the owner's empty-name fault)."""
    where = f" to {place} via the secure link" if place else " through the link"
    return {"id": mid, "conversation_id": f"conv-{mid}", "received": NOW, "from_name": name,
            "subject": f"Recommendation Request for {person}" + (f" for {place}" if place else ""),
            "from": sender,
            "result": {"ask": f"Submit a letter of recommendation for {person}{where}.",
                       "summary": "", "confidential_kind": "letter", "deadline": due,
                       "links": [f"https://apply.example.org/refer?key={mid}"]}}


def _own(mid, sender, name, places):
    rec = {"id": mid, "conversation_id": f"conv-{mid}", "received": NOW,
           "subject": "Recommendation letter request", "from": sender, "from_name": name,
           "result": {"ask": f"Decide whether to write a recommendation letter for {name}.",
                      "summary": "", "confidential_kind": "letter", "deadline": ""}}
    rec["result"]["letter"] = {"role": "request", "person": name, "kind": "recommendation",
                               "destination": "", "due": "", "method": "unknown",
                               "destinations": [{"name": n, "due": d} for n, d in places]}
    return rec


def _ingest(c, rec, ledger_id=""):
    return letters.ingest_record(c, rec, ledger_id=ledger_id,
                                 found=letters.detect(rec, plugin_senders=NO_PLUGINS))


# ── the evidence, reproduced ──────────────────────────────────────────

def test_the_yes_on_two_places_due_the_same_day_does_not_crash():
    """The Yes crash: two destinations due Dec 15 were sorted as (date,
    dict) pairs, and equal dates compared the dicts."""
    c = a_case(destinations=[dest("d1", "Harbor Tech PhD", "2026-12-01", "portal"),
                             dest("d2", "Pinecrest College MS", "2026-12-15", "portal"),
                             dest("d3", "Ridgeline Institute", "2026-12-15", "portal")])
    assert LW._next_due(dict(c, destinations=c["destinations"][1:]))[0] == "2026-12-15"
    st = FakeStore([c])
    out = LW.answer("lc-1", "yes", store=st)
    assert out["ok"] and out["said"].startswith("You said yes, by Tue Dec 1.")
    assert LW.decide(FakeStore([c]), "lc-1", "yes", "yes")["answer"] == "yes"


def test_a_date_press_never_loops_and_never_asks_which_place(monkeypatch):
    """The three "I did not find a date" presses, then "which place?": the
    date is the case's, one tap sets it, and words with no day in them
    change nothing."""
    monkeypatch.setattr(LW, "_today", lambda: date(2026, 11, 22))
    c = a_case(decision="accepted",
               destinations=[dest("d1", "Harbor Tech PhD", "2026-12-01", "portal", state="accepted"),
                             dest("d2", "", "", "portal", state="accepted"),
                             dest("d3", "", "", "portal", state="accepted")])
    st = FakeStore([c])
    assert LW.answer("lc-1", "date", text="what to say in it", store=st) == \
        {"ok": False, "said": LW.PICK_A_DATE}
    assert "do_by" not in st.cases["lc-1"]
    out = LW.answer("lc-1", "date", text="2026-12-01", store=st)            # the picker's day
    assert out == {"ok": True, "said": "The date is Tue Dec 1. I bring it back on Tue Nov 24."}
    assert "turn" not in out and st.cases["lc-1"]["do_by"] == "2026-12-01"
    assert {d["due"] for d in st.cases["lc-1"]["destinations"]} == {"2026-12-01"}
    # the common written forms read as days where no picker exists
    assert LW.parse_due("December 1st") == "2026-12-01" and LW.parse_due("12/1") == "2026-12-01"


def test_the_block_has_one_field_of_words_and_the_date_is_a_picker():
    """The web card drew one field for the first action with `ask`
    (Draft's "what to say in it"), so the date button sent that field.
    Every case card now has at most one action that takes words; a date
    is an action with a date picker."""
    import case_surface as C
    today = date(2026, 11, 22)
    for case in (dict(a_case(decision="accepted"), case_kind="letter"),
                 dict(a_case(decision="accepted", do_by="2026-12-01"), case_kind="letter")):
        for d in case["destinations"]:
            d["state"] = "accepted"
        for spec in (C.case_spec(case, today), C.date_spec(case, today),
                     C.decision_spec(dict(case, decision=""), today)):
            words = [a for a in spec["actions"] if a[5] and not (len(a) > 6 and a[6])]
            assert len(words) <= 1, spec["title"]
            for a in spec["actions"]:
                if len(a) > 6 and a[6]:
                    assert a[5] == C.DATE_ASK and {"min", "max"} <= set(a[6])
            assert not any("due date for" in ln for ln in spec["lines"])


def test_the_web_chat_refuses_a_date_press_with_no_day():
    """A date action's press reaches the bot only with a day in range."""
    from botcore.chat import web_frontend as W
    spec = {"min": "2026-11-22", "max": "2027-11-23"}
    assert W._picked_day("2026-12-01", spec) == "2026-12-01"
    for bad in ("", "Dec 1", "2026-02-30", "2026-11-21", "2028-01-01"):
        with pytest.raises(W._HttpError) as e:
            W._picked_day(bad, spec)
        assert e.value.status == 400 and e.value.message == "Pick a day from Sun Nov 22 to Tue Nov 23, 2027."


def test_slack_draws_a_date_picker_and_its_pick_is_the_text():
    from botcore.chat import slack_frontend as sf
    from botcore.chat.cards import CardAction, ConfirmationCard
    card = ConfirmationCard(kind="letter_ask", title="x", actions=[
        CardAction("letter_date", "By Tue Dec 1", {"case": "lc-1", "by": "2026-12-01"}),
        CardAction("letter_date_pick", "Another date", {"case": "lc-1"}, ask="The date",
                   date={"proposed": "2026-12-01", "min": "2026-11-22", "max": "2027-11-23"})])
    _text, blocks = sf.render_card(card)
    els = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert [e["type"] for e in els] == ["button", "datepicker"]
    assert els[1]["initial_date"] == "2026-12-01" and els[1]["action_id"] == "letter_date_pick"
    fe = sf.SlackFrontend.__new__(sf.SlackFrontend)
    fe._resolve_card = lambda body: None
    ev = fe._action_event({"actions": [{"type": "datepicker", "action_id": "letter_date_pick",
                                        "selected_date": "2026-12-03", "block_id": "b"}],
                           "user": {"id": "U1"}})
    assert ev.text == "2026-12-03" and ev.action_id == "letter_date_pick"


def test_a_part_always_has_a_name(conn):
    """Two places came from portal mail that named none: their names were
    empty, so the card read "2. : no due date" and "Submitted: ". A part
    is named by its mail, else its sender's own name, else its service's
    domain, else "Place N"; two that read the same are numbered."""
    _add(conn, "task-a", conv="conv-m-a")
    _add(conn, "task-b", conv="conv-m-b")
    a = _ingest(conn, _portal("m-a", "Sam Ortiz", sender="graduate_admissions@northfield.example",
                              name="Northfield Graduate Admissions"), ledger_id="task-a")
    _ingest(conn, _portal("m-b", "Sam Ortiz", sender="gradadmit@lakeside.example"), ledger_id="task-b")
    cid = a["case"]
    names = [d["name"] for d in letters.case(conn, cid)["destinations"]]
    assert sorted(names) == ["Northfield Graduate Admissions", "lakeside.example"]
    two = letters.ensure_case(conn, "Jamie Rivera")
    letters.add_destination(conn, two, "")
    letters.add_destination(conn, two, "", sender="lor-noreply@vendor.example",
                            link="https://vendor.example/lor?token=2")
    letters.add_destination(conn, two, "", sender="lor-noreply@vendor.example",
                            link="https://vendor.example/lor?token=3")
    names = [d["name"] for d in letters.case(conn, two)["destinations"]]
    assert sorted(names) == ["Place 1", "vendor.example", "vendor.example 2"]
    assert all(n.strip() for n in names)


def test_a_name_keeps_the_capitals_the_mail_gives_it(conn):
    """The person's own display name had a lower-case second surname; the
    portals' mail spelled it with its capital. The same name in other
    letter case keeps the spelling with its capitals."""
    assert cases.better_spelling("Ana Ruiz-diaz", "Ana Ruiz-Diaz") == "Ana Ruiz-Diaz"
    assert cases.better_spelling("Ana Ruiz-Diaz", "ana ruiz-diaz") == "Ana Ruiz-Diaz"
    assert cases.better_spelling("Ana Ruiz", "Ana Ruiz-Diaz") == "Ana Ruiz"
    cid = letters.ensure_case(conn, "Ana Ruiz-diaz", "ana.ruiz-diaz@uni.example")
    _ingest(conn, _portal("m-c", "Ana Ruiz-Diaz", "Northfield University"))
    assert letters.case(conn, cid)["person_name"] == "Ana Ruiz-Diaz"


def test_the_common_ways_to_write_a_day():
    t = date(2026, 11, 23)
    for words, want in (("12/1", "2026-12-01"), ("Dec 1", "2026-12-01"), ("December 1st", "2026-12-01"),
                        ("next Friday", "2026-11-27"), ("tomorrow", "2026-11-24"),
                        ("2026-12-01", "2026-12-01"), ("1 December", "2026-12-01"),
                        ("in 2 weeks", "2026-12-07"), ("Thurs", "2026-11-26")):
        assert D.parse_words(words, t).isoformat() == want, words
    for words in ("soon", "", "what to say in it", "next month"):
        assert D.parse_words(words, t) is None, words


# ── the store: one date, inherited, pulled earlier ────────────────────

def test_parts_inherit_the_one_date_and_an_earlier_one_pulls_it(conn):
    cid = letters.ensure_case(conn, "Sam Ortiz", "sam@uni.example")
    a = letters.add_destination(conn, cid, "Northfield University", due="2026-12-15")
    letters.add_destination(conn, cid, "Lakeside Institute")
    letters.accept(conn, cid, actor="owner", do_by="2026-12-01")
    c = letters.case(conn, cid)
    assert c["do_by"] == "2026-12-01"
    by = {d["name"]: d for d in c["destinations"]}
    assert by["Lakeside Institute"]["due"] == "2026-12-01" and by["Lakeside Institute"]["own_due"] == ""
    # a later date of its own is kept, and the case's date is what counts
    assert by["Northfield University"]["due"] == "2026-12-01"
    assert by["Northfield University"]["own_due"] == "2026-12-15"
    # a place that comes with an earlier date moves the case's date, once, with why
    letters.add_destination(conn, cid, "Westmoor College", due="2026-11-28")
    assert letters.case(conn, cid)["do_by"] == "2026-11-28"
    ev = conn.execute("SELECT evidence FROM case_events WHERE case_id = ? AND kind = 'do_by' "
                      "ORDER BY seq DESC LIMIT 1", (cid,)).fetchone()
    assert json.loads(ev["evidence"]) == {"from": "2026-12-01", "to": "2026-11-28", "why": "earlier"}
    letters.set_due(conn, a, "2026-12-20")                         # a later one moves nothing
    assert letters.case(conn, cid)["do_by"] == "2026-11-28"
    assert {m["move"] for m in cases.moves(cases.case(conn, cid))} >= {"draft", "delivered"}
    assert not [m for m in cases.moves(cases.case(conn, cid)) if m["move"] == "set_due"]


def test_an_older_store_gets_the_new_columns_and_a_read_only_one_reads_without(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(ledger.SCHEMA)
    for stmt in cases.SCHEMA:                    # the store as its first release made it
        for col in ("do_by", "lead_days", "start_on", "label"):
            stmt = __import__("re").sub(rf",\s*{col}\s+[A-Z]+(?: NOT NULL DEFAULT '')?", "", stmt)
        c.execute(stmt)
    for name, sql in cases.LETTER_VIEWS.items():
        c.execute(f"CREATE VIEW {name} AS {sql}")
    assert "do_by" not in {r[1] for r in c.execute("PRAGMA table_info(cases)")}
    c.execute("INSERT INTO cases (id, kind, opened, updated_at) VALUES ('lc-old', 'letter', ?, ?)", (NOW, NOW))
    c.commit()
    c.close()
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    got = cases.case(ro, "lc-old")
    assert got["do_by"] == "" and got["lead_days"] is None
    ro.close()
    rw = sqlite3.connect(db)
    rw.row_factory = sqlite3.Row
    cases.ensure_schema(rw)
    assert {"do_by", "lead_days", "start_on"} <= {r[1] for r in rw.execute("PRAGMA table_info(cases)")}
    assert "label" in {r[1] for r in rw.execute("PRAGMA table_info(case_parts)")}
    rw.close()


def test_the_stages_and_the_lead_times(_ea):
    c = {"case_kind": "letter", "decision": "accepted", "status": "open",
         "destinations": [{"id": "d1", "due": "", "state": "accepted"}]}
    assert D.stage(c, date(2026, 11, 23)) == "undated"
    c["do_by"] = "2026-12-01"
    assert D.wake(c) == date(2026, 11, 24)
    assert D.stage(c, date(2026, 11, 23)) == "quiet" and D.stage(c, date(2026, 11, 24)) == "due"
    assert D.stage(dict(c, lead_days=3), date(2026, 11, 27)) == "quiet"         # "remind me 3 days before"
    assert D.stage(dict(c, start_on="2026-11-23"), date(2026, 11, 23)) == "due"  # "start this one now"
    c["destinations"][0]["state"] = "ready"
    assert D.stage(c, date(2026, 11, 20)) == "ready"
    (_ea / "config" / "ea-config.json").write_text(json.dumps({"case_lead_days": {"letter": 14}}))
    c["destinations"][0]["state"] = "accepted"
    assert D.wake(c) == date(2026, 11, 17)                                       # his setting
    assert {k: D.LEAD_DAYS[k] for k in ("letter", "form", "review", "report")} == \
        {"letter": 7, "form": 3, "review": 10, "report": 7}


# ── the lifecycle through the bot's tick and presses ──────────────────

class _Sync:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


class Clock:
    def __init__(self, d):
        self.day = d


def _bot(monkeypatch, conn, clock):
    """The bot's case section over a real store in memory: the letters
    layer reads it, letters_work answers on it, and the frontend records
    cards, status lines, plain lines and in-place redraws."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_LETTER_CARDS = _private_state(")
    end = src.index("# The Replies card: the replies that wait")
    from preprocessor import case_surface as PC
    from preprocessor import chat as _chat
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "_letters", lambda: letters)
    monkeypatch.setattr(PL, "load_cases", lambda c=None, open_only=True: letters.cases(conn, open_only=open_only))
    monkeypatch.setattr(PL, "today_local", lambda: clock.day)
    monkeypatch.setattr(PC, "today_local", lambda: clock.day)
    monkeypatch.setattr(PC, "load_cases", lambda c=None, open_only=True, kinds=None: [])
    monkeypatch.setattr(PC, "part_questions", lambda c=None: [])
    monkeypatch.setattr(PC, "write_stated_dates", lambda: cases.stated_dates(conn, actor="bot"))
    monkeypatch.setattr(LW, "_today", lambda: clock.day)
    monkeypatch.setattr(LW, "open_store", lambda: LW.Store(conn, letters))
    monkeypatch.setitem(sys.modules, "letters_work", LW)
    calls = {"cards": [], "status": [], "posts": [], "updates": [], "turns": []}
    store: dict = {}
    retired = set()

    class App:
        def action(self, name):
            return lambda fn: fn

    def post_card(ch, card, thread_ts=None):
        calls["cards"].append(card)
        card._mid = f"m{len(calls['cards'])}"
        return types.SimpleNamespace(channel=ch, id=card._mid, thread_key=card._mid)

    class Cards:
        def by_ref(self, ch, mid):
            return {"status": "retired" if mid in retired else "live"}

    def replace(ch, ts, text):
        calls["status"].append((ts, text))
        retired.add(ts)

    class Frontend:
        def post(self, channel="", thread_ts=None, text=""):
            calls["posts"].append((channel, thread_ts, text))
            return types.SimpleNamespace(id=f"p{len(calls['posts'])}")

        def update_card(self, ref, card):
            calls["updates"].append((ref.id, card))
            return True

        def supports(self, cap):
            return cap == "registers_cards"
    ns = {"json": json, "re": __import__("re"), "time": __import__("time"), "_chat": _chat, "app": App(),
          "_CARDS": Cards(), "threading": types.SimpleNamespace(Thread=_Sync),
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(json.loads(json.dumps(d)))),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
          "_verify_button_user": lambda ev: True, "_resolve_dm_channel": lambda: "dm",
          "_post_card": post_card, "_replace_buttons_with_status": replace, "FRONTEND": Frontend(),
          "_card_ref": lambda ch, mid: _chat.MessageRef(ch, mid, mid),
          "handle_message_async": lambda *a, session_key="": calls["turns"].append((a, session_key)),
          "OWNER_SLACK_USER_ID": "U1", "_one_list": lambda: False}
    exec(compile(src[start:end], "bot[quiet-cases]", "exec"), ns)
    ns["_load_letter_cards"] = lambda: json.loads(json.dumps(store))
    ns["_load_case_cards"] = lambda: {}
    ns["_save_case_cards"] = lambda d: None
    ns["_case_rows"] = lambda: {}
    ns["_letter_levels"] = lambda cs: {str(c["id"]): "full" for c in cs}
    return ns, calls, store


def _press(ns, card, action_id, text="", selected=""):
    """A press on a posted card, as the frontend hands it in."""
    a = next((x for x in card.actions if x.id == action_id), None)
    mid = card._mid
    ns["handle_letter"](types.SimpleNamespace(
        action_id=action_id, value=a.value if a else "", user_id="U1", text=text, selected=selected,
        card=types.SimpleNamespace(channel="dm", id=mid, thread_key=mid)))


def _tick(ns, calls):
    """One router tick: the cards it posted."""
    before = len(calls["cards"])
    ns["_post_letter_cards"](force=True)
    return calls["cards"][before:]


def _home(conn, day):
    import case_surface as C
    from zoneinfo import ZoneInfo
    from datetime import datetime
    zone = ZoneInfo("America/Denver")
    src = types.SimpleNamespace(zone=zone, now=datetime(day.year, day.month, day.day, 12, tzinfo=zone).timestamp(),
                                requests={}, rows={}, dm="dm")
    items, _ = C.home_items(src, [C._tag(c, "letter") for c in letters.cases(conn)])
    return items


def _briefing(conn, day):
    import case_surface as C
    view = C.briefing_view([], today=day, cases=[C._tag(c, "letter") for c in letters.cases(conn)], closed=[])
    return C.apply_briefing({"today_top": []}, view)


def test_one_date_then_quiet_until_it_is_time(conn, monkeypatch):
    clock = Clock(date(2026, 11, 12))
    ns, calls, store = _bot(monkeypatch, conn, clock)
    _add(conn, "task-own", conv="conv-m-own", sender="sam.ortiz@uni.example")
    cid = _ingest(conn, _own("m-own", "sam.ortiz@uni.example", "Sam Ortiz",
                             [("Northfield University", "2026-12-01")]), ledger_id="task-own")["case"]
    _ingest(conn, _portal("m-l", "Sam Ortiz", "Lakeside Institute", sender="admissions@lakeside.example"))

    # 1. the decision proposes the date in the same tap as Yes
    [ask] = _tick(ns, calls)
    assert [(a.id, a.label) for a in ask.actions] == [
        ("letter_yes", ":white_check_mark: Yes, by Tue Dec 1"),
        ("letter_yes_date", ":calendar: Yes, by another date"),
        ("letter_yes_ask", ":page_facing_up: Yes, ask for materials"),
        ("letter_no", ":no_entry_sign: No")]
    assert ask.actions[1].date == {"proposed": "2026-12-01", "min": "2026-11-12", "max": "2027-11-13"}
    assert _home(conn, clock.day)                                   # Home has its decision
    _press(ns, ask, "letter_yes")
    said = calls["status"][-1][1]
    assert "You said yes, by Tue Dec 1. I bring it back on Tue Nov 24." in said
    assert letters.case(conn, cid)["do_by"] == "2026-12-01"

    # 3. quiet: no card, not on Home, on no daily list, on the weekly one
    assert _tick(ns, calls) == [] and calls["posts"] == []
    assert _home(conn, clock.day) == []
    b = _briefing(conn, clock.day)
    assert b["letter_cases"] == [] and b["today_top"] == []
    [quiet] = b["quiet_cases"]
    assert quiet["line"] == ("Letter for Sam Ortiz: by Tue Dec 1, 2 places. Back on your list Tue Nov 24.")
    import case_surface as C
    assert C.briefing_entries(b) == [] and C.briefing_entries(b, quiet=True) == [quiet]

    # 2. a place that comes later joins silently under the date
    clock.day = date(2026, 11, 15)
    _ingest(conn, _portal("m-w", "Sam Ortiz", "Westmoor College", sender="apply@westmoor.example"))
    assert _tick(ns, calls) == [] and calls["posts"] == []
    assert "3 places" in _briefing(conn, clock.day)["quiet_cases"][0]["line"]
    # ... and one with an earlier date of its own moves the date, said once
    clock.day = date(2026, 11, 16)
    _ingest(conn, _portal("m-h", "Sam Ortiz", "Harbor Tech", sender="apply@harbor.example", due="2026-11-28"))
    assert _tick(ns, calls) == []
    [(_ch, _th, line)] = calls["posts"]
    assert line == ":calendar: Harbor Tech asks by Sat Nov 28. The letter for Sam Ortiz is now due Sat Nov 28."
    assert _tick(ns, calls) == [] and len(calls["posts"]) == 1

    # the lead time: one block, "Time to write the letter", and not again
    clock.day = date(2026, 11, 20)
    assert _tick(ns, calls) == []
    clock.day = date(2026, 11, 21)
    [block] = _tick(ns, calls)
    assert block.title == ":memo: *Time to write the letter for Sam Ortiz*: due Sat Nov 28, 4 places."
    assert [a.id for a in block.actions] == ["letter_draft", "letter_date_pick", "letter_decline"]
    assert [i["id"] for i in _home(conn, clock.day)] == [f"letter:{cid}"]
    assert _briefing(conn, clock.day)["letter_cases"][0]["case"] == cid
    clock.day = date(2026, 11, 22)
    assert _tick(ns, calls) == []                                    # the same state: nothing again
    # a date press with no day in it changes nothing: its own card is drawn
    # again in place with the words, open; no status line, no second block
    n, posts = len(calls["status"]), len(calls["posts"])
    _press(ns, block, "letter_date_pick", text="what to say in it")
    assert len(calls["status"]) == n and len(calls["posts"]) == posts
    (mid, again), = calls["updates"]
    assert mid == block._mid and again.failed == ns["CASE_PICK_A_DATE"]
    assert [a.id for a in again.actions] == [a.id for a in block.actions]
    assert _tick(ns, calls) == []

    # 4. the letter is ready: the block becomes the delivery list, in place
    letters.record_draft(conn, cid, "/letters/sam-ortiz/letter.md")
    for d in letters.case(conn, cid)["destinations"]:
        letters.set_state(conn, d["id"], "ready", "owner: approved", "owner")
    conn.commit()
    assert _tick(ns, calls) == []
    assert len(calls["updates"]) == 2
    ready = calls["updates"][-1][1]
    assert ready.failed == ""
    assert ready.title == (":outbox_tray: *The letter for Sam Ortiz is ready.* Submit it to 4 places "
                           "by Sat Nov 28.")
    assert [a.id for a in ready.actions] == ["letter_submitted"] * 3 + ["letter_all", "letter_draft"]
    assert all(a.label.split(": ", 1)[-1].strip() for a in ready.actions)
    # a place that asks after the letter is ready is one line, with its link
    clock.day = date(2026, 11, 23)
    _ingest(conn, _portal("m-p", "Sam Ortiz", "Pinecrest College", sender="apply@pinecrest.example"))
    assert _tick(ns, calls) == []
    line = calls["posts"][-1][2]
    assert line.startswith(":inbox_tray: Pinecrest College also asked. The letter is ready: <https://")
    assert "5 places" in calls["updates"][-1][1].title
    # All submitted, in one tap on the redrawn block, closes the case
    now_shown = calls["updates"][-1][1]
    now_shown._mid = block._mid
    _press(ns, now_shown, "letter_all")
    assert "Submitted: all. The letter for Sam Ortiz is off your list." in calls["status"][-1][1]
    assert letters.case(conn, cid)["status"] == "closed"
    assert _tick(ns, calls) == []


def _from_before(conn, who, email, places=()):
    """A case decided before one date per case: accepted, with no date."""
    cid = letters.ensure_case(conn, who, email)
    for name, due, sender in places:
        letters.add_destination(conn, cid, name, due=due, sender=sender)
    letters.accept(conn, cid, actor="migration", quote="recorded before letter cases")
    cases.set_date(conn, cid, "")
    conn.commit()
    return cid


def test_cases_from_before_take_their_stated_dates_with_one_line_and_no_card(conn, monkeypatch):
    """7. The deploy opens with no pile of cards: a case whose places state
    a date takes the earliest one with no question, and one line names
    them all; a case with no place stays silent (the weekly review); only a
    case with places and no date anywhere gets its one date card."""
    clock = Clock(date(2026, 11, 23))
    ns, calls, store = _bot(monkeypatch, conn, clock)
    a = _from_before(conn, "Rowan Vale", "rowan@uni.example",
                     [("Northfield University", "2026-12-01", ""), ("", "", "gradadmit@lakeside.example")])
    b = _from_before(conn, "Pat Lee", "pat@uni.example", [("Westmoor College", "2026-12-08", "")])
    c = _from_before(conn, "Sam Ortiz", "sam@uni.example")
    d = _from_before(conn, "Jamie Rivera", "jamie@uni.example", [("Harbor Tech", "", "")])
    [card] = _tick(ns, calls)
    assert card.title == ":calendar: *When will you write the letter for Jamie Rivera?*"
    assert [(x.id, x.label) for x in card.actions] == [("letter_date_pick", ":calendar: Pick the date")]
    [(_ch, _th, line)] = calls["posts"]
    assert line == (":calendar: I set dates for 2 letters from what they asked: Rowan Vale by Tue Dec 1, "
                    "Pat Lee by Tue Dec 8. Change one from its block when it comes back.")
    assert letters.case(conn, a)["do_by"] == "2026-12-01" and letters.case(conn, b)["do_by"] == "2026-12-08"
    assert letters.case(conn, a)["do_by_from"] == "stated" and letters.case(conn, c)["do_by"] == ""
    assert _tick(ns, calls) == [] and len(calls["posts"]) == 1          # said once
    assert [i["id"] for i in _home(conn, clock.day)] == [f"letter:{d}"]  # only the one with no date
    assert {e["case"] for e in _briefing(conn, clock.day)["quiet_cases"]} == {a, b, c}
    # each then follows its course: back at its lead time, as one block
    clock.day = date(2026, 11, 24)
    [block] = _tick(ns, calls)
    assert block.title == ":memo: *Time to write the letter for Rowan Vale*: due Tue Dec 1, 2 places."
    # a case that gains its first dated place takes its date the same way, said once
    letters.add_destination(conn, c, "Ridgeline Institute", due="2026-12-20")
    conn.commit()
    assert _tick(ns, calls) == []
    assert calls["posts"][-1][2] == (":calendar: I set the date for 1 letter from what they asked: Sam Ortiz "
                                     "by Sun Dec 20. Change it from its block when it comes back.")
    # his own date on a case is never "set from what they asked"
    letters.add_destination(conn, d, "Coastal Institute", due="2026-12-28")
    conn.commit()
    assert letters.case(conn, d)["do_by"] == "2026-12-28"
    n = len(calls["posts"])
    cases.set_date(conn, d, "2026-12-28")                                # he picks the same day himself
    conn.commit()
    assert letters.case(conn, d)["do_by_from"] == "owner"
    assert _tick(ns, calls) == [] and len(calls["posts"]) == n


def test_once_ready_a_later_own_date_counts_after_the_case_date():
    """2. Before the letter is ready every place is due by the case's date.
    Once it is ready and that date passed, a place still open is late only
    against its own date, and the delivery list shows each one's own date."""
    import case_surface as C
    c = {"id": "lc-1", "case_kind": "letter", "person_name": "Rowan Vale", "decision": "accepted",
         "status": "open", "do_by": "2026-12-01", "opened": "2026-11-10",
         "destinations": [{"id": "d1", "name": "Harbor Tech PhD", "due": "2026-12-01", "own_due": "",
                           "state": "accepted", "method": "portal", "link": ""},
                          {"id": "d2", "name": "Pinecrest College MS", "due": "2026-12-01",
                           "own_due": "2026-12-15", "state": "accepted", "method": "portal", "link": ""}]}
    after = date(2026, 12, 3)
    assert {d["id"] for d in C.facts(c, after)["overdue"]} == {"d1", "d2"}       # not ready: the case's date
    for d in c["destinations"]:
        d["state"] = "ready"
    f = C.facts(c, after)
    assert [d["id"] for d in f["overdue"]] == ["d1"] and f["next_due"] == date(2026, 12, 1)
    spec = C.case_spec(c, after)
    assert "2. Pinecrest College MS: due Tue Dec 15, ready to submit, portal link" in spec["lines"]
    assert "1. Harbor Tech PhD: was due Tue Dec 1, ready to submit, portal link" in spec["lines"]
    before = C.case_spec(dict(c, destinations=[dict(x, state="accepted") for x in c["destinations"]]),
                         date(2026, 11, 26))
    assert "2. Pinecrest College MS: to write, portal link" in before["lines"]


def test_a_live_block_from_before_gives_way_to_the_quiet_line(conn, monkeypatch):
    clock = Clock(date(2026, 11, 20))
    ns, calls, store = _bot(monkeypatch, conn, clock)
    cid = letters.ensure_case(conn, "Rowan Vale", "rowan@uni.example")
    letters.add_destination(conn, cid, "Northfield University", due="2026-12-15")
    letters.accept(conn, cid, actor="owner", do_by="2026-12-15")
    conn.commit()
    store.update({"cards": {cid: {"kind": "case", "live": True, "card": "old1", "channel": "dm",
                                  "posted": 1.0}}, "sent": {}})
    assert _tick(ns, calls) == []
    assert calls["status"] == [("old1", ":zzz: Set for Tue Dec 15. I bring it back on Tue Dec 8.")]


def test_a_yes_with_no_place_and_no_date_is_silent_until_one_comes(conn, monkeypatch):
    """A case with no place named yet and no date asks nothing: off Home
    and the daily lists, on the weekly review, until a place or a date
    comes."""
    clock = Clock(date(2026, 11, 23))
    ns, calls, store = _bot(monkeypatch, conn, clock)
    cid = _from_before(conn, "Jamie Rivera", "jamie@uni.example")
    assert _tick(ns, calls) == [] and calls["posts"] == []
    assert _home(conn, clock.day) == []
    b = _briefing(conn, clock.day)
    assert b["letter_cases"] == [] and [e["case"] for e in b["quiet_cases"]] == [cid]


def test_a_kind_declares_whether_its_yes_carries_the_date(monkeypatch):
    """The date answers are the kind's data (Decision.dated): a kind whose
    yes carries no date keeps a plain Yes."""
    import case_surface as C
    c = {"id": "rv-1", "kind": "review", "case_kind": "review", "decision": "", "status": "open",
         "counterpart_name": "Pat Lee", "parts": [{"id": "p1", "name": "Report", "due": "2026-12-14",
                                                   "state": "asked", "method": "portal"}],
         "questions": [], "materials": []}
    dated = C.decision_spec(c, date(2026, 11, 23))
    assert [a[0] for a in dated["actions"]] == ["case_yes", "case_yes_date", "case_no"]
    plain = types.SimpleNamespace(decision=types.SimpleNamespace(dated=(), choices=("Yes", "No"),
                                                                 no=("No",), then={}),
                                  delivered="returned", ladder=(), loud=())
    monkeypatch.setitem(C._DECL, "review", plain)
    spec = C.decision_spec(c, date(2026, 11, 23))
    assert [(a[0], a[1], a[5]) for a in spec["actions"]] == [("case_yes", ":white_check_mark: Yes", ""),
                                                             ("case_no", ":no_entry_sign: No", "")]


def test_a_name_made_up_for_the_owner_never_goes_into_a_letter():
    """A place the store named from its sender ("brown.example", "Place 2")
    is for the owner's eyes: a letter's {{program}} never takes it, and it
    is no other place's name to look for."""
    made_up = dict(dest("d2", "lakeside.example", "", "portal"), named=False)
    assert LW._fill("Dear committee of {{program}}.", a_case(), {}, made_up) == "Dear committee of ."
    assert LW._phrases(a_case(), {}, made_up) == []
    named = dest("d1", "Harbor Tech PhD", "2026-12-01", "portal")
    assert LW._fill("{{program}}", a_case(), {}, named) == "Harbor Tech PhD"


def test_the_weekly_review_lists_the_cases_set_for_later(conn):
    """The weekly review is where a quiet case shows: by the day it comes back."""
    import case_surface as C
    import remote_audit as RA
    from datetime import datetime, timezone
    cid = letters.ensure_case(conn, "Sam Ortiz", "sam@uni.example")
    letters.add_destination(conn, cid, "Northfield University", due="2026-12-15")
    letters.accept(conn, cid, actor="owner", do_by="2026-12-15")
    b = _briefing(conn, date(2026, 11, 23))
    assert C.briefing_entries(b) == []
    cands = {f"C{n}": C.candidate(e) for n, e in enumerate(C.briefing_entries(b, quiet=True), 1)}
    text = RA.render(cands, {"critical": [], "important": []}, closed=[], reimb=[], hygiene=[],
                     closure={}, counts={}, now=datetime(2026, 11, 23, 15, tzinfo=timezone.utc), handed=[])
    assert (":calendar: Set for later (each comes back on its day):\n"
            "• *Letter for Sam Ortiz*: by Tue Dec 15, 1 place. Back on your list Tue Dec 8.") in text


def test_the_day_a_case_comes_back_reads_as_a_person_says_it():
    import case_surface as C
    today = date(2026, 11, 23)
    assert D.on_day(date(2026, 11, 24), today) == "tomorrow"
    assert D.on_day(today, today) == "today"
    assert D.on_day(date(2026, 12, 1), today) == "on Tue Dec 1"
    case = {"case_kind": "letter", "do_by": "2026-12-01", "state": "accepted",
            "parts": [{"name": "Northfield University", "due": "2026-12-01", "state": "accepted"}]}
    assert C.quiet_words(case, today) == ":zzz: Set for Tue Dec 1. I bring it back tomorrow."
