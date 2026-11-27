"""Not done means "still open, not now" (docs/webchat.md, Open, not now).

Not done on a "Looks done" item, or on a row Tacitus closed, holds the row:
it stays open in the ledger and leaves Home's list and the briefing for a
folded "Open, not now" list at the bottom of Home, until new mail on its
thread, its date two days away, or the owner's own work on it brings it
back. One predicate (ledger.is_held) answers for Home and the briefing.

Here: the predicate on a pinned date; the ledger's hold and unhold verbs
on a scratch database; Home's list, its folded list and an item unfolded;
both Not done paths on Home; the owner's work ending a hold; the
briefing's lanes leaving a held row out; and the bot's and the web
thread's Not done holding the row. Made-up data only."""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta, timezone
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

# A pinned day for the predicate and the briefing's lists.
TODAY = date(2026, 11, 20)
HELD_AT = "2026-11-18T10:00:00Z"
HELD_DAY = "2026-11-18"      # the owner's local day of HELD_AT


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "—" not in text and "–" not in text, text


def _row(rid="task-h1", **kw) -> dict:
    """An open row as the lanes read it (metadata merged), held at HELD_AT
    when its mail last came the day before."""
    row = {"id": rid, "type": "task", "status": "open", "description": "Order the poster tubes",
           "conversation_id": f"conv-{rid}",
           "thread_brief": {"last_inbound": "2026-11-17T09:00:00Z", "subject": "Poster tubes"},
           "hold": {"at": HELD_AT, "day": HELD_DAY, "inbound": "2026-11-17T09:00:00Z", "by": "user"}}
    row.update(kw)
    return row


# ── the predicate ─────────────────────────────────────────────────────

def test_a_held_row_stays_held_until_new_mail_comes_on_its_thread():
    row = _row()
    assert ledger.is_held(row, TODAY) and ledger.hold_released(row, TODAY) == ""
    row["thread_brief"]["last_inbound"] = "2026-11-19T08:00:00Z"        # they wrote after the hold
    assert not ledger.is_held(row, TODAY) and ledger.hold_released(row, TODAY) == "mail"
    # his own message is not new mail from them
    own = _row(thread_brief={"messages": [{"received": "2026-11-17T09:00:00Z"},
                                          {"received": "2026-11-19T08:00:00Z", "own": True}]})
    assert ledger.is_held(own, TODAY)
    theirs = _row(thread_brief={"messages": [{"received": "2026-11-19T08:00:00+00:00"}]})
    assert ledger.hold_released(theirs, TODAY) == "mail"


def test_mail_from_before_the_hold_never_ends_it():
    """A row held with no thread record yet: a record that comes later with
    mail from before the hold leaves it held; mail after it ends it."""
    row = _row(thread_brief={}, hold={"at": HELD_AT, "day": HELD_DAY, "inbound": "", "by": "user"})
    assert ledger.is_held(row, TODAY)
    row["thread_brief"] = {"last_inbound": "2026-11-18T09:59:00Z"}
    assert ledger.is_held(row, TODAY)
    row["thread_brief"] = {"last_inbound": "2026-11-18T10:01:00Z"}
    assert ledger.hold_released(row, TODAY) == "mail"
    # a row with no mail behind it ends only on its date or the owner's work
    chat = _row(conversation_id="", thread_brief={}, hold={"at": HELD_AT, "day": HELD_DAY, "inbound": "", "by": "user"})
    assert ledger.latest_inbound(chat) == "" and ledger.is_held(chat, TODAY)


@pytest.mark.parametrize("due, held", [
    ("2026-11-23", True),     # three days away: still held
    ("2026-11-22", False),    # two days away, and four days after the hold: back
    ("2026-11-21", False),    # tomorrow, three days after the hold: back
    ("2026-11-20", True),     # today, but already two days away on the day of the hold
    ("2026-11-02", True),     # past already when he held it
])
def test_a_held_row_comes_back_two_days_before_a_date_that_was_further_off(due, held):
    """Held on Nov 18, read on Nov 20: a date counts only when it was more
    than two days after the day of the hold."""
    assert ledger.is_held(_row(deadline=due), TODAY) is held
    # the date its thread names counts when the row has none
    brief = {"last_inbound": "2026-11-17T09:00:00Z", "deadline": due}
    assert ledger.is_held(_row(thread_brief=brief), TODAY) is held
    assert ledger.hold_date_counts(_row(deadline=due)) is (due > "2026-11-20")
    if not held:
        assert ledger.hold_released(_row(deadline=due), TODAY) == "due"


def _held_on(day: date, **kw) -> dict:
    """A row the owner held on `day` (his local date), at noon UTC."""
    return _row(hold={"at": f"{day.isoformat()}T12:00:00Z", "day": day.isoformat(),
                      "inbound": "2026-11-17T09:00:00Z", "by": "user"}, **kw)


def test_a_row_held_with_its_date_past_stays_held():
    """The owner holds a row whose date passed: he knew it, so the date
    never brings it back; new mail still does."""
    row = _held_on(TODAY, deadline=(TODAY - timedelta(days=3)).isoformat())
    for later in range(0, 30, 3):
        assert ledger.is_held(row, TODAY + timedelta(days=later)), later
    # no date of its own, its thread's two days past (a reminder that came before)
    brief = {"last_inbound": "2026-11-17T09:00:00Z", "deadline": (TODAY - timedelta(days=2)).isoformat()}
    row = _held_on(TODAY, thread_brief=brief)
    assert ledger.is_held(row, TODAY) and not ledger.hold_date_counts(row)
    row["thread_brief"]["last_inbound"] = f"{TODAY.isoformat()}T15:00:00Z"
    assert ledger.hold_released(row, TODAY) == "mail"


def test_a_row_held_with_its_date_tomorrow_stays_held():
    row = _held_on(TODAY, deadline=(TODAY + timedelta(days=1)).isoformat())
    assert not ledger.hold_date_counts(row)
    for later in (0, 1, 2, 5):
        assert ledger.is_held(row, TODAY + timedelta(days=later)), later


def test_a_row_held_ten_days_before_its_date_comes_back_two_days_before_it():
    row = _held_on(TODAY, deadline=(TODAY + timedelta(days=10)).isoformat())
    assert ledger.hold_date_counts(row)
    for later in range(0, 8):
        assert ledger.is_held(row, TODAY + timedelta(days=later)), later
    assert ledger.hold_released(row, TODAY + timedelta(days=8)) == "due"
    assert not ledger.is_held(row, TODAY + timedelta(days=12))             # and stays back after it


def test_a_date_moved_after_the_hold_counts_once_it_was_further_off_than_two_days():
    """Held with its date tomorrow (no return on that date); the date moves
    to six days after the hold: it comes back two days before the new one.
    A move to another date that was already near on the day of the hold
    does not."""
    row = _held_on(TODAY, deadline=(TODAY + timedelta(days=1)).isoformat())
    assert ledger.is_held(row, TODAY + timedelta(days=3))
    row["deadline"] = (TODAY + timedelta(days=6)).isoformat()
    assert ledger.is_held(row, TODAY + timedelta(days=3))
    assert ledger.hold_released(row, TODAY + timedelta(days=4)) == "due"
    row["deadline"] = (TODAY + timedelta(days=2)).isoformat()
    assert ledger.is_held(row, TODAY + timedelta(days=4))


def test_the_day_of_a_hold_is_the_owners_local_day(monkeypatch):
    """A hold records the owner's local day; one without it reads the local
    date of its time (user_tz)."""
    assert ledger.hold_day({"at": HELD_AT, "day": "2026-11-17"}) == date(2026, 11, 17)
    monkeypatch.setattr(ledger, "_local_day", lambda at: (at - timedelta(hours=12)).date())
    assert ledger.hold_day({"at": "2026-11-18T05:00:00Z"}) == date(2026, 11, 17)
    assert ledger.hold_day({}) is None


def test_the_owners_work_a_close_or_a_new_proposal_ends_it():
    assert ledger.hold_released(_row(hold={"at": HELD_AT, "inbound": "", "ended": "2026-11-19T12:00:00Z"}),
                                TODAY) == "worked"
    assert ledger.hold_released(_row(status="completed"), TODAY) == "closed"
    assert ledger.hold_released(_row(close_proposal={"why": "the form went in"}), TODAY) == "looks_done"
    assert not ledger.is_held(_row(hold=None), TODAY) and not ledger.is_held(_row(hold={}), TODAY)
    assert ledger.without_held([_row(), _row("task-h2", hold=None)], TODAY) == [_row("task-h2", hold=None)]


def test_a_kept_row_is_held_on_times_alone():
    """Mail kept from hosted models is held the same way; the hold holds
    times, never a word of the mail."""
    row = _row(keep_level="local", thread_brief={}, kept_received="2026-11-17T09:00:00Z")
    assert ledger.is_held(row, TODAY)
    row["kept_received"] = "2026-11-19T09:00:00Z"
    assert ledger.hold_released(row, TODAY) == "mail"
    shown = ledger.shown_hold(_row(), TODAY)
    assert shown["held"] is True and shown["held_since"] == HELD_AT and "hold" not in shown
    released = ledger.shown_hold(_row(deadline="2026-11-21"), TODAY)
    assert "held" not in released and "hold" not in released


# ── the ledger's verbs ────────────────────────────────────────────────

@pytest.fixture
def owner_ledger(tmp_path, monkeypatch):
    """A scratch ledger the owner's process opens (guarded_connect's owner
    branch, as the bot runs it), with an open reply row and a closed row."""
    import resident_io as rio
    state = tmp_path / "state"
    state.mkdir()
    path = state / "obligations-ledger.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    brief = {"messages": [{"id": "m-1", "received": since, "from": "pat@example.org"}],
             "last_inbound": since, "needs_reply": True, "subject": "Room for the seminar"}
    conn.execute("INSERT INTO ledger (id, type, status, description, conversation_id, source_message_id, "
                 "date_identified, metadata, created_at, updated_at) VALUES (?, 'response_needed', 'open', ?, "
                 "?, 'm-1', ?, ?, ?, ?)", ("response-h1", "Needs response: Room for the seminar", "conv-h1",
                                          since, json.dumps({"thread_brief": brief}), since, since))
    conn.execute("INSERT INTO ledger (id, type, status, description, closed_at, created_at, updated_at) "
                 "VALUES ('task-h9', 'task', 'completed', 'Done long ago', ?, ?, ?)", (since, since, since))
    conn.commit()
    conn.close()
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.setattr(ledger, "_install_tree", lambda: tmp_path / "no-tree")
    monkeypatch.setattr(rio, "_owner_check", lambda: False)
    monkeypatch.setattr(ledger, "_row_written", lambda rid, created: None)
    owner = {"is": True}
    monkeypatch.setattr(ledger, "_is_owner", lambda: owner["is"])
    return owner


def _cli(monkeypatch, capsys, *argv) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["ledger.py", *argv])
    rc = ledger.main()
    out = capsys.readouterr().out.strip().splitlines()
    return rc, json.loads(out[-1]) if out else {}


def _meta(rid: str) -> dict:
    conn = sqlite3.connect(str(ledger.DB_PATH))
    try:
        return json.loads(conn.execute("SELECT metadata FROM ledger WHERE id = ?", (rid,)).fetchone()[0] or "{}")
    finally:
        conn.close()


def test_hold_is_the_owners_tap_and_the_reply_queue_and_the_list_honour_it(owner_ledger, monkeypatch, capsys):
    owner_ledger["is"] = False
    rc, out = _cli(monkeypatch, capsys, "hold", "--id", "response-h1")
    assert rc == 1 and "owner" in out["error"] and "hold" not in _meta("response-h1")   # a model lane never holds
    owner_ledger["is"] = True
    rc, out = _cli(monkeypatch, capsys, "reply-queue")
    assert [e["id"] for e in out["entries"]] == ["response-h1"]
    rc, out = _cli(monkeypatch, capsys, "hold", "--id", "response-h1", "--actor", "web-chat")
    rec = _meta("response-h1")["hold"]
    assert rc == 0 and out["ok"] and rec["by"] == "web-chat" and rec["day"] == ledger.local_today().isoformat()
    assert rec["inbound"] == ledger.latest_inbound(_meta("response-h1"))       # the mail it waits past
    _rc, out = _cli(monkeypatch, capsys, "reply-queue")
    assert out["entries"] == []                                               # out of the reply queue
    _rc, out = _cli(monkeypatch, capsys, "list", "--status", "open")
    row = next(e for e in out["entries"] if e["id"] == "response-h1")
    assert row["held"] is True and row["held_since"] == rec["at"] and "hold" not in row   # still open
    _rc, got = _cli(monkeypatch, capsys, "get", "--id", "response-h1")
    assert got["held"] is True
    rc, out = _cli(monkeypatch, capsys, "hold", "--id", "task-h9")
    assert rc == 1 and "open" in out["error"]                                 # a closed row is not held
    conn = sqlite3.connect(str(ledger.DB_PATH))
    kinds = [r[0] for r in conn.execute("SELECT kind FROM ledger_events WHERE entry_id = 'response-h1'")]
    conn.close()
    assert kinds == ["hold"]


def test_the_owners_work_or_a_status_set_on_the_row_ends_the_hold(owner_ledger, monkeypatch, capsys):
    _cli(monkeypatch, capsys, "hold", "--id", "response-h1")
    owner_ledger["is"] = False                                                # unhold: any process
    rc, out = _cli(monkeypatch, capsys, "unhold", "--id", "response-h1", "--why", "a note on Home")
    assert rc == 0 and out["ended"] and _meta("response-h1")["hold"]["ended"]
    _rc, got = _cli(monkeypatch, capsys, "get", "--id", "response-h1")
    assert "held" not in got
    _rc, out = _cli(monkeypatch, capsys, "reply-queue")
    assert [e["id"] for e in out["entries"]] == ["response-h1"]               # back in the reply queue
    _rc, out = _cli(monkeypatch, capsys, "unhold", "--id", "response-h1")
    assert out["ok"] and not out["ended"]                                     # once
    owner_ledger["is"] = True
    _cli(monkeypatch, capsys, "hold", "--id", "response-h1")
    _cli(monkeypatch, capsys, "update", "--id", "response-h1", "--last-action", "2026-11-20: a note")
    assert "hold" in _meta("response-h1")                                     # a note on the row is no decision
    _cli(monkeypatch, capsys, "update", "--id", "response-h1", "--status", "snoozed", "--snooze-until", "2026-12-01")
    assert "hold" not in _meta("response-h1")                                 # Later: the hold ends for good


def test_the_reply_queue_leaves_a_held_row_out():
    row = _row("response-h2", type="response_needed", source_message_id="m-2",
               thread_brief={"messages": [{"id": "m-2", "received": "2026-11-17T09:00:00Z"}],
                             "last_inbound": "2026-11-17T09:00:00Z", "needs_reply": True})
    assert ledger.reply_backlog([row], TODAY) == []
    assert [e["id"] for e in ledger.reply_backlog([dict(row, hold=None)], TODAY)] == ["response-h2"]


# ── Home ──────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sources(state: str = "normal") -> H.Sources:
    """The fixture's afternoon, with no work cases and no "which one was
    it?" questions (none is read from a store)."""
    src = F.sources(state)
    src.cases, src.questions = [], []
    return src


def hold(src: H.Sources, lid: str, days_ago: float = 1.0) -> None:
    """The row as `ledger.py hold` leaves it: when, and its thread's newest
    mail from the other side then."""
    row = src.rows[lid]
    row.pop("close_proposal", None)
    at = src.now - days_ago * 86400
    row["hold"] = {"at": _iso(at), "day": datetime.fromtimestamp(at, src.zone).date().isoformat(),
                   "inbound": ledger.latest_inbound(row), "by": "user"}


def held_sources(**over) -> H.Sources:
    """The normal afternoon with one list on, where the owner holds the
    slides (a to-do) and the seminar date (a reply owed)."""
    src = sources()
    src.one_list = True
    src.rows["response-r1"]["source_message_id"] = "m-r1"   # the message it owes a reply to
    hold(src, "task-s2", 1)
    hold(src, "response-r1", 3)
    for k, v in over.items():
        setattr(src, k, v)
    return src


def refs(items: list[dict]) -> list[str]:
    return [x["ref"] for x in items]


def test_a_held_row_leaves_left_for_you_for_one_folded_line_of_its_own():
    before = H.build(sources())
    assert {"item:task-s2", "reply:response-r1"} <= set(refs(before["left"])) and before["held"] == []
    p = H.build(held_sources())
    assert "item:task-s2" not in refs(p["left"]) and "reply:response-r1" not in refs(p["left"])
    assert refs(p["held"]) == ["item:task-s2", "reply:response-r1"]          # the one set aside last first
    slides, seminar = p["held"]
    assert (slides["kind"], slides["label"], slides["title"]) == ("item", "To do", "Send the slides to the committee")
    assert (seminar["kind"], seminar["label"], seminar["title"]) == ("reply", "Reply owed", "Seminar date")
    assert p["tally"]["left"] == len(p["left"]) == before["tally"]["left"] - 2
    assert p["mode"]["kind"] == "normal" and p["facts"]["held"] == 0           # no count of it anywhere else
    assert [c["id"] for c in slides["acts"]] == ["done", "later", "waiting"]   # its usual row, once unfolded
    for x in p["held"]:
        assert not [k for k in x if k.startswith("_")] and "mins" not in x
        plain(x["title"] + x["label"] + x["detail"])


def test_a_held_item_unfolds_in_place_with_its_choices_and_what_brings_it_back():
    src = held_sources()
    it = I.item(src, "item:task-s2")
    assert it["kind"] == "item" and it["note"]["open"]
    assert [c["id"] for c in it["acts"]] == ["done", "later", "waiting"]
    # no mail thread and no date: only the owner brings it back
    assert it["brief"]["standing"][:2] == ["You set it aside yesterday.", "It stays here until you work on it."]
    reply = I.item(src, "reply:response-r1")
    assert reply["kind"] == "reply" and [c["id"] for c in reply["acts"]] == ["later", "not_needed"]
    day = (datetime.fromtimestamp(src.now, F.ZONE).date() - timedelta(days=3)).strftime("%A")
    assert reply["brief"]["standing"][:2] == [f"You set it aside on {day}.", "It comes back when new mail arrives."]
    for line in it["brief"]["standing"] + reply["brief"]["standing"]:
        plain(line)


def test_a_row_held_when_it_was_due_tomorrow_stays_in_open_not_now_and_says_only_mail_brings_it_back():
    """The workshop review is due tomorrow when the owner taps Not done: it
    stays held, and its briefing never promises a return on that date."""
    src = held_sources()
    assert "item:deadline-w1" in refs(H.build(src)["left"])
    hold(src, "deadline-w1", 0)
    p = H.build(src)
    assert "item:deadline-w1" in refs(p["held"]) and "item:deadline-w1" not in refs(p["left"])
    assert I.item(src, "item:deadline-w1")["brief"]["standing"][:2] == [
        "You set it aside today.", "It comes back when new mail arrives."]


def test_a_conversation_about_a_held_row_is_not_answer_needed():
    talk = {"id": "c1", "channel": "main", "thread_key": "t9", "state": "needs_you", "title": "About the slides",
            "_ids": ["task-s2"], "_convs": []}
    other = dict(talk, id="c2", thread_key="t8", title="About the poster", _ids=["task-zz"])
    got = [x["id"] for x in H.left(held_sources(conversations=[talk, other]))]
    assert "thread:main:t8" in got and "thread:main:t9" not in got     # its row is held, so it is too


def test_new_mail_or_its_date_brings_a_held_row_back_at_once():
    """Back in Left for you as soon as Home reads it, whether or not
    today's briefing listed it (a row no list names, here)."""
    src = held_sources()
    today = datetime.fromtimestamp(src.now, F.ZONE).date()
    src.rows["task-h9"] = {"id": "task-h9", "type": "task", "status": "open", "description": "Renew the lab permit",
                           "conversation_id": "conv-h9", "deadline": (today + timedelta(days=9)).isoformat()}
    src.open_ids.add("task-h9")
    hold(src, "task-h9", 2)
    p = H.build(src)
    assert "item:task-h9" in refs(p["held"]) and "item:task-h9" not in refs(p["left"])
    assert I.item(src, "item:task-h9")["brief"]["standing"][1] == \
        "It comes back when new mail arrives or it is due in two days."
    src.rows["task-h9"]["deadline"] = (today + timedelta(days=2)).isoformat()           # two days away
    p = H.build(src)
    back = next(x for x in p["left"] if x["ref"] == "item:task-h9")
    assert back["title"] == "Renew the lab permit" and "item:task-h9" not in refs(p["held"])
    # the other side wrote on the seminar thread after the hold
    src.rows["response-r1"]["thread_brief"]["last_inbound"] = _iso(src.now - 3600)
    p = H.build(src)
    assert "reply:response-r1" in refs(p["left"]) and refs(p["held"]) == ["item:task-s2"]


def test_a_new_close_proposal_shows_a_held_row_as_looks_done_again():
    src = held_sources()
    src.rows["task-s2"]["close_proposal"] = {"why": "The slides went out on Friday", "first_at": _iso(src.now)}
    p = H.build(src)
    assert "proposal:task-s2" in refs(p["left"]) and "item:task-s2" not in refs(p["held"])


def test_travel_keeps_its_own_count_and_what_the_owner_holds_folds_in_after():
    src = sources("travel")
    away = H.build(src)
    hold(src, "task-s2")
    p = H.build(src)
    assert p["mode"]["held"] == away["mode"]["held"] - 1 == p["facts"]["held"]
    assert refs(p["held"])[-1] == "item:task-s2"
    assert "Tacitus holds" in p["mode"]["line"] and "item:task-s2" not in refs(p["left"])


class _FE:
    one_list = True
    owner_id = "owner"

    def __init__(self):
        self.changed = 0
        self.store = types.SimpleNamespace(home_get=lambda name: {}, home_set=lambda name, value: None)

    def home_changed(self):
        self.changed += 1

    def dm_channel(self, user):
        return "dm"


def _home(src, runs):
    """Home over `src`, with the bot's hooks as the ledger would answer
    them: Not done on a proposal rejects and holds, Not done on a closed
    row opens it again and holds it."""
    h = H.Home(_FE(), clock=lambda: src.now, sources=lambda tz: src, background=False)
    pressed = []

    def close(lid, keep):
        pressed.append(("close", lid, keep))
        if keep:
            hold(src, lid, 0)
        return {"ok": True}

    def reopen(lid):
        pressed.append(("reopen", lid))
        row = next(r for r in src.closed if r["id"] == lid)
        src.closed.remove(row)
        src.rows[lid] = {"id": lid, "type": "task", "status": "open", "description": row["description"]}
        src.open_ids.add(lid)
        hold(src, lid, 0)
        return {"ok": True}
    h.hooks.close, h.hooks.reopen = close, reopen
    h.hooks.note = lambda info, words: pressed.append(("note", info.get("lid"), words)) or {"ok": True}
    h.hooks.draft = lambda rid, words, answer: pressed.append(("draft", rid)) or {"ok": True, "home": True}
    return h, pressed


def test_not_done_on_looks_done_holds_the_row_out_of_left_for_you(monkeypatch):
    runs = []
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or {"ok": True})
    src = sources()
    src.one_list = True
    src.rows["task-s2"]["close_proposal"] = {"why": "The slides went out on Friday", "first_at": _iso(src.now)}
    h, pressed = _home(src, runs)
    assert "proposal:task-s2" in refs(h.payload()["left"])
    out, _after = h.act("proposal:task-s2", "not_done")
    assert out == {"ok": True, "said": I.HOLD_SAID, "held": True} and pressed == [("close", "task-s2", True)]
    plain(out["said"])
    p = H.build(src)
    assert "proposal:task-s2" not in refs(p["left"]) and "item:task-s2" not in refs(p["left"])
    assert refs(p["held"]) == ["item:task-s2"]


def test_not_done_on_a_row_tacitus_closed_opens_it_and_holds_it(monkeypatch):
    runs = []
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or {"ok": True})
    src = sources()
    src.one_list = True
    h, pressed = _home(src, runs)
    out, _after = h.act("closed:row-c1", "reopen")
    assert out == {"ok": True, "said": I.HOLD_SAID, "held": True} and pressed == [("reopen", "row-c1")]
    p = H.build(src)
    assert "closed:row-c1" not in [d["ref"] for d in p["done"]]
    assert refs(p["held"]) == ["item:row-c1"] and "item:row-c1" not in refs(p["left"])


def test_working_on_a_held_item_ends_the_hold_and_brings_it_back(monkeypatch):
    runs = []

    def run(*a):
        runs.append(a)
        if a[:2] == ("ledger.py", "unhold"):          # as the verb leaves the row
            src.rows[a[3]]["hold"]["ended"] = _iso(src.now)
        return {"ok": True}
    monkeypatch.setattr(wts, "RUN", run)
    src = held_sources()
    h, pressed = _home(src, runs)
    out, _after = h.act("item:task-s2", "note", "Send them to the chair first")
    assert out["ok"] and pressed == [("note", "task-s2", "Send them to the chair first")]
    assert runs == [("ledger.py", "unhold", "--id", "task-s2", "--why", "the owner's note on Home")]
    p = H.build(src)
    assert "item:task-s2" in refs(p["left"]) and refs(p["held"]) == ["reply:response-r1"]
    # unfolding alone ends nothing, and a row that is not held runs no verb
    runs.clear()
    assert I.item(src, "reply:response-r1") and runs == []
    h.act("item:task-s2", "note", "And the dean")
    assert runs == []


def test_later_on_a_held_item_ends_the_hold_with_the_snooze_it_writes(monkeypatch):
    runs = []
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or {"ok": True, "entry": {"status": "snoozed"}})
    src = held_sources()
    clock = [src.now]
    h = H.Home(_FE(), clock=lambda: clock[0], sources=lambda tz: src, background=False)
    store: dict = {}
    h._meta, h._meta_set = (lambda name: json.loads(json.dumps(store.get(name) or {})),
                            lambda name, value: store.__setitem__(name, value))
    out, _after = h.act("item:task-s2", "later", "tomorrow")
    assert out["ok"] and "item:task-s2" not in refs(h.payload()["held"])      # off Home inside the Undo window
    clock[0] += I.UNDO_S + 1
    h.acts.commit_due()
    assert [a[:6] for a in runs] == [("ledger.py", "update", "--id", "task-s2", "--status", "snoozed")]


# ── the briefing ──────────────────────────────────────────────────────

def test_the_briefing_leaves_a_held_row_and_its_thread_out_of_every_list():
    import preprocess
    held, sibling, free = _row("task-h1"), _row("thread-h1", hold=None, conversation_id="conv-task-h1"), \
        _row("task-h3", hold=None)
    deadlines = [{"conversation_id": "conv-task-h1", "subject": "Poster tubes"},
                 {"conversation_id": "conv-task-h3", "subject": "Other"}]
    rows, dl, stale = preprocess.without_held([held, sibling, free], deadlines, [held, free], TODAY)
    assert [o["id"] for o in rows] == ["task-h3"] and [o["id"] for o in stale] == ["task-h3"]
    ended = _row("task-h4", hold={"at": HELD_AT, "day": HELD_DAY, "inbound": "", "ended": "2026-11-19T12:00:00Z"})
    rows, _dl, _stale = preprocess.without_held([ended], [], [], TODAY)
    assert rows == [ended] and "hold" not in rows[0]                           # an ended hold is no fact for a lane
    assert [d["subject"] for d in dl] == ["Other"]
    same = preprocess.without_held([free], deadlines, [], TODAY)
    assert same[0] == [free] and same[1] == deadlines
    # the ranking, the slice, the reply backlog and the "Looks done" lines all read the filtered rows
    src = (REPO / "preprocessor" / "preprocess.py").read_text()
    run = src[src.index("def run_full_daily"):src.index("def run_scheduling_check")]
    cut = run.index("without_held(open_obligations")
    for use in ("build_today_top(", '"open_obligations": briefing_rows(open_obligations', '"reply_backlog":',
                '"close_proposals": _close_proposals(open_obligations)'):
        assert run.index(use) > cut, use
    assert '"open_obligations": len(all_open)' in run                         # the total still counts it


def test_the_remote_lane_never_sees_a_held_row():
    import remote_briefing as rb
    from test_remote_briefing import NOW, _briefing
    today = NOW.date()
    b = _briefing()
    later, began = (today + timedelta(days=9)).isoformat(), today - timedelta(days=5)
    b["open_obligations"] += [_held_on(began, id="row-held", description="Held slides", deadline=later)]
    b["stale_obligations"] = [_held_on(began, id="row-held-stale", description="Held form", deadline=later)]
    c = rb.candidates(b, today=today)
    assert not {x.get("ledger_id") for x in c.values()} & {"row-held", "row-held-stale"}
    b["open_obligations"][-1]["deadline"] = (today + timedelta(days=1)).isoformat()      # its date: back
    assert "row-held" in {x.get("ledger_id") for x in rb.candidates(b, today=today).values()}


def test_the_hosted_lane_is_told_never_to_brief_a_held_row():
    for name in ("skills/morning-briefing.md", "skills/afternoon-catchup.md", "docs/ea/briefing-data.md"):
        text = (REPO / name).read_text()
        assert "`held: true`" in text and "never brief it" in text, name


# ── the other entry points ────────────────────────────────────────────

def test_the_web_threads_not_done_holds_only_a_row_it_kept_open(monkeypatch):
    runs = []
    answers = {"reject": {"ok": False, "action": "skipped"}, "update": {"ok": True, "entry": {"status": "completed"},
                                                                        "warning": "closed"}}
    monkeypatch.setattr(wts, "RUN", lambda *a: runs.append(a) or dict(answers.get(a[1], {"ok": True}), _exit=0))
    wts.run_writeback({"ops": [("reject", "task-a"), ("reopen", "task-b")]})
    assert not [r for r in runs if r[:2] == ("ledger.py", "hold")]
    answers.clear()
    runs.clear()
    wts.run_writeback({"ops": [("reject", "task-a"), ("reopen", "task-b")]})
    assert [r[:4] for r in runs if r[0] == "ledger.py"] == [
        ("ledger.py", "reject", "--id", "task-a"), ("ledger.py", "hold", "--id", "task-a"),
        ("ledger.py", "update", "--id", "task-b"), ("ledger.py", "hold", "--id", "task-b")]
    assert wts.CARD_DONE["reject"] == ":pushpin: Not done. Tacitus holds it until you want it."


def test_the_close_cards_not_done_holds_the_row_only_when_it_stayed_open():
    import test_closure_policy as T
    ns = T._bot_ns({})
    cli = []
    ns["_ledger_cli"] = lambda *a: cli.append(a) or {}                         # the ledger did not answer
    assert not ns["_keep_open_proposed"]("ae28") and cli == [("reject", "--id", "ae28")]
    assert not ns["_reopen_closed"]("t1") and [a[0] for a in cli] == ["reject", "update"]
    cli.clear()
    ns["_ledger_cli"] = lambda *a: cli.append(a) or {"ok": True, "entry": {"status": "open"}}
    assert ns["_keep_open_proposed"]("ae28") and ns["_reopen_closed"]("t1")
    assert [a[:3] for a in cli] == [("reject", "--id", "ae28"), ("hold", "--id", "ae28"),
                                    ("update", "--id", "t1"), ("hold", "--id", "t1")]
    plain(ns["HOLD_SAID"])
    assert ns["HOLD_SAID"] == I.HOLD_SAID
    import closure_labels as cl
    assert cl.outcome_class(f":pushpin: {ns['HOLD_SAID']}") == "kept_open"
