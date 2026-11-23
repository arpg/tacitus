"""Letter cases where the owner sees them (preprocessor/letter_surface.py):
one line per case in the briefings, never one per portal mail and never a
reply owed; the reminders by date; the two cards in Action requests and a
press on them; Home's one item per case; the pre-send review's record and
its rule; a project room's name. The fault this pins: a letter request
was one row for the person's own mail and one row per portal mail, each
listed on its own, a reply closed or proposed to close them, and the
Replies card offered them as replies. Every name, address and destination
here is made up."""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))
sys.path.insert(2, str(ROOT / "botcore"))

import letter_surface as L  # noqa: E402

TODAY = date(2026, 11, 23)                                   # a Monday
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)     # 04:00 in Denver
ZONE = ZoneInfo("America/Denver")


@pytest.fixture(autouse=True)
def _no_store(monkeypatch, tmp_path):
    """No test reads a real letters store, a real letters_work module or
    the machine's ledger: the case briefing reads the ledger's rows, and a
    live ledger is readable only while a process holds it open."""
    monkeypatch.setattr(L, "_letters", lambda: None)
    monkeypatch.setitem(sys.modules, "letters_work", None)
    import ledger
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "no-ledger.db")


@pytest.fixture
def mountain(monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _dest(did, name, due="", state="accepted", method="portal", link="", lid=""):
    return {"id": did, "name": name, "due": due, "method": method, "link": link, "state": state,
            "ledger_id": lid or f"task-{did}", "submitted_at": "", "evidence": ""}


def avery():
    """Nine destinations: three submitted, two due tomorrow, two on Dec 1,
    two whose portal mail named no date."""
    ds = [_dest(f"av{i}", f"Program {c}", state="submitted") for i, c in enumerate("ABC", 1)]
    for d in ds:
        d["submitted_at"] = "2026-11-20T15:00:00Z"
    ds += [_dest("av4", "Northfield Graduate School", "2026-11-24",
                 link="https://apply.northfield.example.org/rec/xyz"),
           _dest("av5", "Lakeside Institute", "2026-11-24"),
           _dest("av6", "Harbor Fellowship", "2026-12-01", state="drafted"),
           _dest("av7", "Willow College", "2026-12-01"),
           _dest("av8", "Prairie University"), _dest("av9", "Coastal Academy")]
    return {"id": "case-av", "person_name": "Avery Quinn", "person_email": "avery.q@example.org",
            "kind": "recommendation", "opened": "2026-11-18", "materials": [{"kind": "cv", "ref": "cv.pdf"}],
            "base_letter": "", "notes": "", "destinations": ds}


def jordan():
    """A new request yesterday: no answer yet, no date."""
    return {"id": "case-jp", "person_name": "Tamsin Okafor", "person_email": "jp@example.org",
            "kind": "reference", "opened": "2026-11-22", "materials": [],
            "request_attachments": [{"name": "statement.pdf"}, {"name": "resume.pdf"}],
            "destinations": [_dest("jp1", "Riverbend Housing", state="asked", method="email")]}


def riley():
    return {"id": "case-rc", "person_name": "Rowan Ellery", "person_email": "rc@example.org",
            "kind": "nomination", "opened": "2026-11-02", "materials": [{"kind": "cv", "ref": "cv.pdf"}],
            "destinations": [_dest("rc1", "Mountain Teaching Award", "2026-12-15", method="email")]}


def _row(lid, conv, desc, **over):
    r = {"id": lid, "type": "task", "status": "open", "description": desc, "conversation_id": conv,
         "source_message_id": f"m-{lid}", "closes_on": "submission"}
    r.update(over)
    return r


def letter_rows():
    """The rows the Nov fault had: the person's own mail and one per portal."""
    rows = []
    for c in (avery(), jordan(), riley()):
        for d in c["destinations"]:
            rows.append(_row(d["ledger_id"], f"conv-{d['id']}", f"Task: recommendation portal {d['name']}",
                             letter={"case": c["id"], "destination": d["id"]}))
    return rows


def other_row():
    return _row("deadline-report", "conv-report", "Due 2026-11-24: annual report", type="deadline",
                deadline="2026-11-24", closes_on="")


def cases():
    return [avery(), jordan(), riley()]


# ── one case in facts and words ───────────────────────────────────────

def test_a_case_is_one_line_with_its_counts_and_next_date():
    f = L.facts(avery(), TODAY)
    assert (f["total"], f["submitted"], f["next_due"]) == (9, 3, date(2026, 11, 24))
    assert [d["id"] for d in f["due_next"]] == ["av4", "av5"] and len(f["undated"]) == 2
    assert L.line(avery(), TODAY) == "Letter for Avery Quinn: 3 of 9 submitted. Next due tomorrow: 2 destinations."
    assert L.line(riley(), TODAY) == "Letter for Rowan Ellery: 0 of 1 submitted. Next due Tue Dec 15: 1 destination."
    new = L.line(jordan(), TODAY)
    assert new.startswith("Letter for Tamsin Okafor: asked Nov 22, 1 destination. No due date yet.")
    assert new.endswith("Will you write it? Answer in Action requests.")
    assert "—" not in new


def test_a_case_with_no_date_asks_for_it_once():
    c = riley()
    c["destinations"][0]["due"] = ""
    c["opened"] = "2026-11-22"
    assert L.line(c, TODAY).endswith("No due date yet. Tell me the due date.")
    c["opened"] = "2026-11-10"
    assert L.line(c, TODAY).endswith("No due date yet.")


def test_an_overdue_destination_is_named_in_the_line():
    c = avery()
    c["destinations"][3]["due"] = "2026-11-20"
    assert "1 was due Fri Nov 20. Next due tomorrow: 1 destination." in L.line(c, TODAY)
    assert L.entry(c, TODAY)["next_due"] == "2026-11-20" and L.entry(c, TODAY)["overdue"] == 1


def test_keep_levels_hold_names_back():
    rows = {d["ledger_id"]: {"id": d["ledger_id"], "keep_level": "local"} for d in avery()["destinations"]}
    lv = L.level(avery(), rows, records=False)
    assert lv == "local"
    line = L.line(avery(), TODAY, lv=lv)
    assert line.startswith("A kept letter case: 3 of 9 submitted.") and "Avery" not in line
    assert L.name(avery(), "local") == "A kept letter case"
    facts_line = L.line(avery(), TODAY, lv="facts")
    assert facts_line.startswith("Letter for Avery Quinn")
    e = L.entry(avery(), TODAY, rows)
    assert e["person"] == "" and all(d["name"] == "" for d in e["destinations"])
    assert e["keep_level"] == "local"


def test_summary_line_comes_from_the_store_when_it_has_one(monkeypatch):
    mod = types.SimpleNamespace(summary_line=lambda case: "Letter for Avery Quinn: 3 of 9 submitted, next due Nov 24 (2 destinations)")
    monkeypatch.setattr(L, "_letters", lambda: mod)
    assert L.line(avery(), TODAY) == "Letter for Avery Quinn: 3 of 9 submitted, next due Nov 24 (2 destinations)."
    # a kept case never takes the store's words: they name the person
    assert L.line(avery(), TODAY, lv="local").startswith("A kept letter case")


# ── the briefing JSON ─────────────────────────────────────────────────

def _briefing(rows):
    portal = [r for r in rows if r["id"].startswith("task-av")][:2]
    return {
        "today_top": [{"source": "obligation", "title": r["description"], "ref": r["id"], "score": 40,
                       "reason": "open obligation"} for r in portal]
        + [{"source": "deadline", "title": "annual report", "ref": "deadline-report", "score": 85,
            "reason": "due Nov 24 (1d)"}],
        "everything_else": [],
        "new_needs_response": [{"message_id": "m-task-av8", "conversation_id": "conv-av8",
                                "subject": "Recommendation request: Prairie University", "from": "noreply@portal.example.org"},
                               {"message_id": "m-intro", "conversation_id": "conv-intro", "subject": "Intro",
                                "from": "pat@example.edu"}],
        "new_dropped_threads": [], "deadline_items": [{"message_id": "m-task-av4", "conversation_id": "conv-av4",
                                                        "subject": "Reminder: letter due"}],
        "open_obligations": rows, "stale_obligations": [], "resurrected_follow_ups": [],
        "reply_backlog": [{"id": "task-jp1", "conversation_id": "conv-jp1", "age_days": 2},
                          {"id": "response-intro", "conversation_id": "conv-intro", "age_days": 3}],
        "handled_by_reply": [],
    }


def test_the_briefing_json_has_one_entry_per_case_and_no_destination_rows():
    rows = letter_rows() + [other_row()]
    view = L.briefing_view(rows, today=TODAY, cases=cases(), closed=[])
    b = L.apply_briefing(_briefing(rows), view)
    assert [e["case"] for e in b["letter_cases"]] == ["case-av", "case-rc", "case-jp"]
    assert [o["id"] for o in b["open_obligations"]] == ["deadline-report"]
    assert [i["message_id"] for i in b["new_needs_response"]] == ["m-intro"]
    assert b["deadline_items"] == []
    assert [e["id"] for e in b["reply_backlog"]] == ["response-intro"]
    refs = [t["ref"] for t in b["today_top"]]
    assert refs == ["deadline-report", "letter:case-av"]       # due tomorrow ranks; Dec 15 does not
    assert b["today_top"][1]["reason"] == "letter due tomorrow"


def test_without_takes_the_case_items_out_of_the_ranking_inputs():
    rows = letter_rows() + [other_row()]
    view = L.briefing_view(rows, today=TODAY, cases=cases(), closed=[])
    mail = [{"message_id": "m-task-av8", "conversation_id": "conv-av8"}, {"message_id": "m-x", "conversation_id": "c-x"}]
    got_mail, got_rows = L.without(view, mail, rows)
    assert [m["message_id"] for m in got_mail] == ["m-x"]
    assert [r["id"] for r in got_rows] == ["deadline-report"]
    # no case: the lists come back as they are
    assert L.without({}, mail, rows) == (mail, rows)


def test_the_ranking_never_scores_a_destination_row():
    import preprocess
    rows = letter_rows() + [other_row()]
    view = L.briefing_view(rows, today=TODAY, cases=cases(), closed=[])
    top, rest = preprocess.build_today_top(*L.without(view, [], [], [], [], rows), [])
    assert all(not str(t["ref"]).startswith("task-") for t in top + rest)


def test_a_case_that_finished_says_so_quietly():
    done = riley()
    done["destinations"][0].update(state="submitted", submitted_at="2026-11-22T18:00:00Z")
    view = L.briefing_view([], today=TODAY, cases=[avery()], closed=[done])
    assert view["finished"] == [{"case": "case-rc", "name": "Nomination letter for Rowan Ellery", "total": 1}]
    assert L.finished_words(view["finished"]) == \
        "1 letter finished: all its destinations are submitted (Nomination letter for Rowan Ellery)"


def test_no_store_means_no_change():
    b = {"today_top": [{"ref": "x", "score": 50}], "open_obligations": [{"id": "x"}]}
    assert L.briefing_view([{"id": "x"}], today=TODAY, cases=[], closed=[]) == {}
    assert L.apply_briefing(dict(b), {}) == b


# ── the remote briefing post ──────────────────────────────────────────

@pytest.fixture
def priv(tmp_path, monkeypatch):
    import owner_trust
    import remote_briefing as rb
    import remote_llm as rl
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    monkeypatch.setattr(rb, "outlook_links", lambda convs, **k: {})
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "drafting": "remote", "briefings": "remote", "shadow_until": "2099-01-01"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def _model(plan):
    def chat(messages, **kw):
        chat.calls.append((kw.get("purpose"), messages))
        return json.loads(json.dumps(plan)), {"latency_ms": 5}
    chat.calls = []
    return chat


def _post_briefing():
    rows = letter_rows() + [other_row()]
    view = L.briefing_view(rows, today=TODAY, cases=cases(), closed=[])
    return L.apply_briefing(_briefing(rows), view)


def test_the_candidates_carry_one_ref_per_case():
    import remote_briefing as rb
    b = _post_briefing()
    cands = rb.candidates(b, today=TODAY)
    letters = {r: c for r, c in cands.items() if c["kind"] == "letter"}
    assert sorted(letters) == ["C1", "C2", "C3"]
    assert not any(c.get("ledger_id", "").startswith("task-") for c in cands.values())
    assert cands["C1"]["due"] == "2026-11-24" and cands["C1"]["ledger_id"] == ""
    assert [t["ref"] for t in rb.top_refs(b, cands)] == ["L1", "C1"]


def test_the_candidates_drop_a_letter_row_the_preprocessor_missed():
    import remote_briefing as rb
    rows = letter_rows() + [other_row()]
    b = {"open_obligations": rows, "new_needs_response": [{"message_id": "m-task-av8", "conversation_id": "conv-av8",
                                                           "subject": "Portal request"}]}
    cands = rb.candidates(b, today=TODAY)
    assert [c["ledger_id"] for c in cands.values()] == ["deadline-report"]


PLAN = {"get_done_today": [{"ref": "C1", "when": "in the 1-2:30 gap", "why": "two portals close tomorrow"},
                           {"ref": "L1", "when": "first thing", "why": "the report"}],
        "flexes": [], "drafts": [{"ref": "C2", "reply_must_say": "Yes, I will write it."}],
        "needs_you": [{"ref": "C3", "decision": "Will you write it?"}],
        "escalate": [{"ref": "C2", "why": "a letter to write", "task": "write the letter"}]}


def test_a_morning_post_names_each_case_once_never_as_a_reply(mountain, priv):
    import remote_briefing as rb
    text, rec = rb.run("morning-briefing", _post_briefing(), run_id="r", now=NOW, chat=_model(PLAN),
                       dry_run=True, queue=lambda *a, **k: pytest.fail("queued"),
                       audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    # the do-list: code's line and the window, never the model's words
    assert "1. *Letter for Avery Quinn*: 3 of 9 submitted. Next due tomorrow: 2 destinations. " \
           "Work on it in the 1-2:30 gap." in text
    assert "two portals close tomorrow" not in text
    # the Letters list: the others, nearest date first, the new one last
    head = ":memo: *Letters you are writing* (2)"
    assert head in text
    block = text.split(head)[1].split("\n\n")[0]
    assert block.index("Rowan Ellery") < block.index("Tamsin Okafor")
    assert "Will you write it? Answer in Action requests." in block
    # never a draft, a decision or a hand-off for a letter
    assert "Drafts ready" not in text and "Needs you" not in text and "Sent to Claude" not in text
    assert rec["drafts"] == 0 and rec["escalated"] == 0
    # one line per case: no portal mail, no destination row anywhere
    for word in ("portal Prairie", "Recommendation request", "Lakeside", "Northfield"):
        assert word not in text
    assert text.count("Avery Quinn") == 1 and text.count("Tamsin Okafor") == 1
    # the queue had a destination row too: it counts the one real reply alone
    assert "*Reply queue*: 1 reply owed" in text
    assert "—" not in text


def test_a_case_due_soon_is_in_the_date_banner(mountain, priv):
    import remote_briefing as rb
    plan = dict(PLAN, get_done_today=[{"ref": "L1", "when": "first thing", "why": "the report"}])
    text, _ = rb.run("morning-briefing", _post_briefing(), run_id="r", now=NOW, chat=_model(plan),
                     dry_run=True, audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert ":alarm_clock: DUE SOON: *Letter for Avery Quinn*: 3 of 9 submitted. Next due tomorrow: " \
           "2 destinations." in text
    assert text.count("Avery Quinn") == 1


def test_the_prompt_tells_the_model_a_letter_is_no_reply(mountain, priv):
    import remote_briefing as rb
    chat = _model(PLAN)
    rb.run("morning-briefing", _post_briefing(), run_id="r", now=NOW, chat=chat, dry_run=True,
           audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    system, user = chat.calls[0][1][0]["content"], json.loads(chat.calls[0][1][1]["content"])
    assert "kind `letter`" in system and "never a reply" in system
    item = next(i for i in user["items"] if i["ref"] == "C1")
    assert item["kind"] == "letter" and item["summary"].startswith("Letter for Avery Quinn")
    assert item["due_in"] == "tomorrow"


def test_home_view_leaves_cases_to_home(mountain, priv):
    import remote_briefing as rb
    b = _post_briefing()
    cands = rb.candidates(b, today=TODAY)
    view = rb.home_view("morning-briefing", cands, rb.check_plan(PLAN, cands, morning=True), [], [], {}, NOW)
    assert all("Avery" not in i["line"] for i in view["items"])


def test_the_afternoon_names_cases_by_date(mountain, priv):
    import remote_briefing as rb
    b = _post_briefing()
    b["letter_cases"][1]["next_due"] = "2026-11-23"             # Rowan: due today
    b["letter_cases"][1]["line"] = "Letter for Rowan Ellery: 0 of 1 submitted. Next due today: 1 destination."
    plan = {"since_morning": [{"ref": "C3", "line": "new mail"}], "pending": []}
    text, _ = rb.run("afternoon-catchup", b, run_id="r", now=NOW + timedelta(hours=10), chat=_model(plan),
                     audit=lambda *a, **k: {}, rooms=lambda *a, **k: [])
    assert "- Due tomorrow: *Letter for Avery Quinn*: 3 of 9 submitted." in text
    assert ":memo: *Letters due* (1):\n- *Letter for Rowan Ellery*: 0 of 1 submitted. Next due today" in text
    assert "new mail" not in text                         # a case is no mail since the morning
    assert "Tamsin Okafor" not in text


# ── the weekly audit ──────────────────────────────────────────────────

def test_the_weekly_audit_lists_a_case_once(monkeypatch):
    import remote_audit as ra
    rows = letter_rows() + [other_row()]
    view = L.briefing_view(rows, today=TODAY, cases=cases(), closed=[])
    b = L.apply_briefing({"open_obligations": rows}, view)
    cands = ra.candidates(rows, b, today=TODAY)
    assert [c["ledger_id"] for c in cands.values() if c["kind"] == "ledger"] == ["deadline-report"]
    assert sum(1 for c in cands.values() if c["kind"] == "letter") == 3
    assert cands["C1"]["days_left"] == 1
    line = ra._line(cands["C1"], "the model's words")
    assert line == "• *Letter for Avery Quinn*: 3 of 9 submitted. Next due tomorrow: 2 destinations."
    monkeypatch.setattr(L, "load_cases", lambda conn=None, open_only=True: cases())
    closed = [dict(r, status="completed") for r in letter_rows() if r["id"] in ("task-av1", "task-av2", "task-av3")]
    done = L.collapse_rows(closed, open_only=False, today=TODAY)
    assert len(done) == 1 and done[0]["description"].startswith("Recommendation letter for Avery Quinn: 3 of 9")


# ── the pre-send review ───────────────────────────────────────────────

def test_the_review_sees_a_case_with_its_destinations_and_cannot_drop_it():
    import briefing_review as br
    import remote_briefing as rb
    cands = rb.candidates(_post_briefing(), today=TODAY)
    lists = {"top": [("C1", "*Letter for Avery Quinn*: 3 of 9 submitted.")], "letters": [("C2", "x")]}
    items = br.collect_items(lists, cands)
    assert items[0]["letter_case"]["total"] == 9
    assert [d.get("due") for d in items[0]["letter_case"]["destinations"]][:2] == ["2026-11-24", "2026-11-24"]
    ledger = {"rows": {}, "events": {}, "by_conv": {}, "naming": {}}
    packet, _meta = br.build_packet(agent="morning-briefing", post="p", items=items, ledger=ledger,
                                    levels={}, projects={}, drafts={}, evidence={}, calendar=[], rooms={},
                                    rules="")
    rec = packet["items"][0]
    assert rec["letter_case"]["rule"].endswith("A reply in its thread is not a submission.")
    obj = {"items": [{"ref": "C1", "action": "drop", "kind": "done", "to": "", "line": "",
                      "why": "the owner replied that he will write it"},
                     {"ref": "C2", "action": "rewrite-line", "kind": "", "to": "", "line": "Done.", "why": ""}]}
    verdicts, rejected, _notes = br.check_verdicts(obj, packet, agent="morning-briefing", items=items)
    assert verdicts == {}
    assert [r["ref"] for r in rejected] == ["C1", "C2"]
    assert "every destination is submitted" in rejected[0]["why"]
    moved, _r, _n = br.check_verdicts({"items": [{"ref": "C2", "action": "move", "kind": "", "to": "flex",
                                                   "line": "", "why": ""}]}, packet, agent="morning-briefing",
                                      items=items)
    assert moved["C2"]["action"] == "move"
    assert "letter_case" in br.PROMPT


# ── project rooms ─────────────────────────────────────────────────────

def test_a_room_names_a_case_by_its_person_and_kind():
    import briefing_rooms as rooms
    import remote_briefing as rb
    cands = rb.candidates(_post_briefing(), today=TODAY)
    entries = rb.room_entries(cands, {"letters": [("C2", rb.letter_surface.post_line(cands["C2"]))]})
    assert entries[0]["title"] == "Nomination letter for Rowan Ellery"
    assert rooms.conversation_name(entries, rb._clean) == "Nomination letter for Rowan Ellery"
    idx = {"rows": {"task-rc1": ["teaching"]}, "threads": {}, "loose": set(), "votes": {}, "sources": {}}
    assert rooms.placement(entries[0], idx, None) == {"projects": ["teaching"], "by": "link", "hits": []}
    idx["rows"] = {}
    assert rooms.placement(entries[0], idx, None)["projects"] == []     # never on words
    kept = dict(entries[0], keep_level="local")
    assert rooms.conversation_name([kept], rb._clean) == "A kept message"
    text = rooms.render("Morning briefing, Mon Nov 23", entries, [], rb._clean)
    assert ":memo: Letter: *Letter for Rowan Ellery*" in text


def test_a_rooms_also_open_list_shows_a_case_once(monkeypatch):
    monkeypatch.setattr(L, "load_cases", lambda conn=None, open_only=True: cases())
    rows = [r for r in letter_rows() if r["id"].startswith("task-av")] + [other_row()]
    out = L.collapse_rows(rows, today=TODAY)
    assert len(out) == 2 and out[0]["description"].startswith("Recommendation letter for Avery Quinn: 3 of 9")


# ── reminders by date ─────────────────────────────────────────────────

def test_the_ladder_rings_seven_three_one_the_day_and_the_day_after():
    c = riley()
    rungs = {}
    for n in range(-3, 12):
        day = date(2026, 12, 15) - timedelta(days=n)
        got = L.reminders([c], day)
        rungs[n] = [(r["rung"], r["loud"]) for r in got]
    assert {n: v for n, v in rungs.items() if v} == {7: [(7, False)], 3: [(3, False)], 1: [(1, True)],
                                                     0: [(0, True)], -1: [(-1, True)]}


def test_a_rung_fires_once_and_stops_when_submitted():
    c = riley()
    day = date(2026, 12, 14)
    first = L.reminders([c], day)
    assert len(first) == 1
    sent = {first[0]["key"]: 1.0}
    assert L.reminders([c], day, sent) == []
    c["destinations"][0]["state"] = "submitted"
    assert L.reminders([c], date(2026, 12, 15)) == []
    moved = riley()
    moved["destinations"][0]["due"] = "2026-12-15"
    assert L.reminders([moved], day, sent) == []           # same date, same rung: already sent
    moved["destinations"][0]["due"] = "2026-12-15"
    moved["destinations"][0]["due"] = "2026-12-21"
    assert L.reminders([moved], day, sent)[0]["rung"] == 7   # a new date starts its own ladder


def test_the_card_plan_asks_once_then_keeps_one_block():
    c_new, c_open = jordan(), riley()
    # Dec 15 is more than two weeks out: no block yet, the briefing lists it
    plan = L.card_plan([c_new, c_open], {}, TODAY)
    assert sorted((p["case"], p["what"]) for p in plan) == [("case-jp", "ask")]
    plan = L.card_plan([c_new, c_open], {}, date(2026, 12, 1))
    assert sorted((p["case"], p["what"]) for p in plan) == [("case-jp", "ask"), ("case-rc", "case")]
    cards = {"case-jp": {"kind": "ask", "live": False}, "case-rc": {"kind": "case", "live": True}}
    assert L.card_plan([c_new, c_open], cards, TODAY) == []     # asked once; the block is up
    # the day before its date, the block is posted again
    plan = L.card_plan([c_open], cards, date(2026, 12, 14))
    assert plan[0]["what"] == "remind" and plan[0]["items"][0]["rung"] == 1
    # three days before: a quiet rung, recorded, nothing posted
    plan = L.card_plan([c_open], cards, date(2026, 12, 12))
    assert plan == [{"case": "case-rc", "what": "mark", "keys": ["rc1:3:2026-12-15"]}]
    # a case with nothing left to do: its card is retired
    done = riley()
    done["destinations"][0]["state"] = "submitted"
    assert L.card_plan([done], cards, TODAY) == [{"case": "case-rc", "what": "retire"}]


def test_the_loud_reminder_names_the_destinations_due():
    items = L.reminders([avery()], TODAY)
    assert {i["dest"] for i in items} == {"av4", "av5"} and all(i["loud"] for i in items)
    text = L.reminder_text(avery(), items, TODAY, "full")
    assert text == "Letter for Avery Quinn: 2 destinations due tomorrow (Northfield Graduate School, Lakeside Institute)."
    assert "Northfield" not in L.reminder_text(avery(), items, TODAY, "facts")


# ── the cards in Action requests ──────────────────────────────────────

def test_the_new_case_card_asks_will_you_write_it():
    spec = L.decision_spec(jordan(), TODAY)
    assert spec["kind"] == "letter_ask" and "Will you write it?" in spec["title"]
    assert [(a[0], a[1]) for a in spec["actions"]] == [
        ("letter_yes", ":white_check_mark: Yes"), ("letter_yes_ask", ":page_facing_up: Yes, ask for materials"),
        ("letter_no", ":no_entry_sign: No")]
    assert spec["no_default"] is True and all(a[3] != "danger" for a in spec["actions"])
    assert "The request included: statement.pdf, resume.pdf." in spec["lines"]
    assert "1. Riverbend Housing: no due date, not answered yet, by email" in spec["lines"]
    v = spec["actions"][0][2]
    assert v == {"case": "case-jp", "rows": ["task-jp1"]} and "ledger_id" not in v
    kept = L.decision_spec(jordan(), TODAY, "local")
    assert "The request included 2 files." in kept["lines"] and "Tamsin" not in json.dumps(kept)


def test_the_case_block_offers_what_fits_its_state():
    spec = L.case_spec(avery(), TODAY)
    assert spec["kind"] == "letter" and spec["title"].startswith(":memo: *Recommendation letter for Avery Quinn*: 3 of 9")
    ids = [a[0] for a in spec["actions"]]
    assert ids == ["letter_draft", "letter_submitted", "letter_submitted", "letter_submitted", "letter_date",
                   "letter_decline"]
    assert spec["actions"][0][1] == ":pencil2: Change the letter"        # one destination is drafted
    assert spec["actions"][1][2]["dest"] == "av4"
    assert [o[1] for o in spec["select"][2]] == ["av7", "av8", "av9"]
    assert "Missing: the due date for 2 destinations." in spec["lines"]
    assert "<https://apply.northfield.example.org/rec/xyz|Open the portal for Northfield Graduate School>" in spec["lines"]
    assert all("ledger_id" not in a[2] for a in spec["actions"])
    one = L.case_spec(riley(), TODAY)
    assert [a[0] for a in one["actions"]] == ["letter_draft", "letter_submitted", "letter_decline"]
    assert one["actions"][0][1] == ":pencil2: Draft the letter" and one["select"] is None
    kept = L.case_spec(avery(), TODAY, "local")
    assert not any(ln.startswith("<https://") for ln in kept["lines"]) and "Avery" not in json.dumps(kept)
    loud = L.case_spec(riley(), TODAY, reminder="Letter for Rowan Ellery: 1 destination due tomorrow.")
    assert loud["title"].startswith(":alarm_clock: *Letter for Rowan Ellery: 1 destination due tomorrow.*")


def test_an_answer_goes_to_letters_work_when_it_is_there(monkeypatch):
    seen = []
    mod = types.SimpleNamespace(answer=lambda cid, choice, **kw: seen.append((cid, choice, kw))
                                or {"ok": True, "said": "Recorded.", "turn": "Draft it."})
    monkeypatch.setitem(sys.modules, "letters_work", mod)
    got = L.answer(avery(), "draft", text="mention the field work")
    assert got == {"ok": True, "said": "Recorded.", "turn": "Draft it."}
    assert seen == [("case-av", "draft", {"dest": "", "text": "mention the field work", "actor": "owner"})]


def test_the_fallback_answers_without_letters_work():
    calls = []

    def runner(argv):
        calls.append(argv)
        return {"ok": True}
    assert L.answer(jordan(), "yes", runner=runner)["ok"] and calls[-1] == ["accept", "--case", "case-jp"]
    got = L.answer(avery(), "submitted", dest="av4", runner=runner)
    assert got["ok"] and calls[-1][:3] == ["record-submitted", "--dest", "av4"]
    assert not L.answer(avery(), "submitted", runner=runner)["ok"]       # no destination named
    no = L.answer(jordan(), "no", runner=runner)
    assert no == {"ok": True, "said": "You will not write it. Nothing is sent."}      # no turn, no draft
    assert calls[-1] == ["decline", "--case", "case-jp"]
    asked = L.answer(jordan(), "yes", question="lq-1", runner=runner)
    assert asked["ok"] and calls[-1] == ["answer", "--question", "lq-1", "--choice", "Yes"]
    assert L.answer(avery(), "decline", runner=runner)["said"] == "Declined. Nothing is sent."
    assert calls[-1] == ["decline", "--case", "case-av"]
    same = L.answer(jordan(), "join_same", question="lq-2", runner=runner)
    assert same["ok"] and calls[-1] == ["answer", "--question", "lq-2", "--choice", "Same person"]
    assert not L.answer(jordan(), "join_other", runner=runner)["ok"]                 # no question named
    draft = L.answer(avery(), "draft", text="short", runner=runner)
    assert "letters.py get --case case-av" in draft["turn"] and 'The owner added: "short".' in draft["turn"]
    ask = L.answer(jordan(), "yes_ask", runner=runner)
    assert "Ask only for what the request did not already include" in ask["turn"]
    assert not L.answer(avery(), "close")["ok"]


def test_the_slack_front_end_draws_both_cards():
    from botcore.chat import slack_frontend as sf
    from preprocessor import chat as _chat
    for spec in (L.decision_spec(jordan(), TODAY), L.case_spec(avery(), TODAY)):
        actions = [_chat.CardAction(a, lbl, v, st, w, ask=ask) for a, lbl, v, st, w, ask in spec["actions"]]
        sel = spec.get("select")
        card = _chat.ConfirmationCard(kind=spec["kind"], title=spec["title"], lines=spec["lines"],
                                      actions=actions, block_id=spec["block_id"],
                                      select=_chat.CardSelect(*sel) if sel else None)
        text, blocks = sf.render_card(card)
        kinds = [b["type"] for b in blocks]
        assert "actions" in kinds and text
        if sel:
            els = next(b for b in blocks if b["type"] == "actions")["elements"]
            assert els[-1]["type"] == "static_select"


def test_the_folded_block_names_the_kind():
    from botcore.chat import action_requests as ar
    card = {"kind": "letter", "title": ":memo: x", "fields": [{"label": "From", "value": "Avery Quinn"},
                                                             {"label": "Subject", "value": "Recommendation letter"},
                                                             {"label": "When", "value": "Next due tomorrow"}]}
    assert ar.block({"card": card}) == {"action": "Letter to write", "who": "From Avery Quinn",
                                        "what": "Recommendation letter", "when": "Next due tomorrow"}
    assert ar.block({"card": dict(card, kind="letter_ask")})["action"] == "Letter request"


def test_the_block_edge_reads_the_case_due_date(monkeypatch):
    import web_requests as wr
    monkeypatch.setattr(wr._wts, "ledger_rows", lambda ids, convs, db: [])
    monkeypatch.setattr(wr, "_projects", lambda ids, convs, db: ({}, {}))
    monkeypatch.setattr(L, "load_cases", lambda conn=None, open_only=True: cases())
    spec = L.case_spec(avery(), TODAY)
    card = {"kind": "letter", "actions": [{"id": a[0], "value": a[2]} for a in spec["actions"]]}
    assert wr.context([{"id": "m1", "card": card}]) == {"m1": {"due": "2026-11-24"}}


# ── the bot's tick and a press (ea-chat-bot.py) ───────────────────────

class _Sync:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


def _bot(monkeypatch, case_list, store=None):
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("_LETTER_CARDS = _private_state(")
    end = src.index("# The Replies card: the replies that wait")
    from preprocessor import chat as _chat
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "_letters", lambda: None)
    monkeypatch.setattr(PL, "load_cases", lambda conn=None, open_only=True: case_list)
    monkeypatch.setattr(PL, "today_local", lambda: TODAY)
    calls = {"cards": [], "status": [], "posts": [], "turns": [], "answers": [], "actions": []}
    store = store if store is not None else {}
    retired = set()

    class App:
        def action(self, name):
            calls["actions"].append(name)
            return lambda fn: fn

    def post_card(ch, card, thread_ts=None):
        calls["cards"].append(card)
        return types.SimpleNamespace(channel=ch, id=f"m{len(calls['cards'])}", thread_key=f"m{len(calls['cards'])}")

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
    ns = {"json": json, "re": __import__("re"), "time": __import__("time"), "_chat": _chat, "app": App(),
          "_CARDS": Cards(), "threading": types.SimpleNamespace(Thread=_Sync),
          "_private_state": lambda name: Path("/nonexistent") / name,
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, mode=0: store.update(json.loads(json.dumps(d)))),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
          "_verify_button_user": lambda ev: True, "_resolve_dm_channel": lambda: "dm",
          "_post_card": post_card, "_replace_buttons_with_status": replace, "FRONTEND": Frontend(),
          "handle_message_async": lambda *a, session_key="": calls["turns"].append((a, session_key))}
    exec(compile(src[start:end], "bot[letters]", "exec"), ns)
    ns["_load_letter_cards"] = lambda: json.loads(json.dumps(store))
    ns["_letter_levels"] = lambda cs: {str(c["id"]): "full" for c in cs}
    return ns, calls, store


def test_the_tick_posts_the_decision_once_and_the_block_after_yes(monkeypatch):
    soon = riley()
    soon["destinations"][0]["due"] = "2026-12-01"
    ns, calls, store = _bot(monkeypatch, [jordan(), soon])
    assert ns["_post_letter_cards"](force=True) == 2
    kinds = [c.kind for c in calls["cards"]]
    assert kinds == ["letter_ask", "letter"]
    assert store["cards"]["case-jp"]["kind"] == "ask" and store["cards"]["case-rc"]["live"]
    assert ns["_post_letter_cards"](force=True) == 0          # nothing new
    # A field's value is plain text: the frontends render it inert, and a
    # wrapped one would show its delimiters in the folded block.
    values = [f.value for c in calls["cards"] for f in c.fields]
    assert values and all("\ue000" not in v and "\ue001" not in v for v in values)
    assert any(a.pattern.startswith("^letter_") for a in calls["actions"] if hasattr(a, "pattern"))


def test_a_press_runs_the_answer_and_its_turn_in_the_cards_thread(monkeypatch):
    ns, calls, store = _bot(monkeypatch, [avery()])
    ns["_post_letter_cards"](force=True)
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "answer", lambda case, choice, dest="", text="", question="": calls["answers"].append(
        (case["id"], choice, dest, text)) or {"ok": True, "said": "I am writing the letter's draft.",
                                               "turn": "Draft the letter for case-av."})
    ev = types.SimpleNamespace(action_id="letter_draft", value={"case": "case-av", "rows": []}, user_id="U1",
                               text="keep it short", selected="",
                               card=types.SimpleNamespace(channel="dm", id="m1", thread_key="m1"))
    ns["handle_letter"](ev)
    assert calls["answers"] == [("case-av", "draft", "", "keep it short")]
    assert calls["posts"][0][:2] == ("dm", "m1") and "writing the letter" in calls["posts"][0][2]
    (args, key), = calls["turns"]
    assert args[3] == "Draft the letter for case-av." and key.endswith("letter-case-av") or "letter-case-av" in key
    from botcore.chat import card_text
    assert card_text.strip_inert(calls["status"][-1][1]).startswith(":white_check_mark: I am writing")
    assert [c.kind for c in calls["cards"]] == ["letter", "letter"]   # the block posted again


def test_a_press_on_a_gone_destination_does_nothing(monkeypatch):
    ns, calls, _store = _bot(monkeypatch, [avery()])
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "answer", lambda *a, **k: pytest.fail("answered"))
    ev = types.SimpleNamespace(action_id="letter_submitted", value={"case": "case-av", "dest": "av1"},
                               user_id="U1", text="", selected="",
                               card=types.SimpleNamespace(channel="dm", id="m9", thread_key="m9"))
    ns["handle_letter"](ev)                                  # av1 is submitted already
    assert calls["status"] == [("m9", ns["LETTER_CARD_GONE"])]


def test_the_select_names_the_destination_alone(monkeypatch):
    ns, calls, _store = _bot(monkeypatch, [avery()])
    from preprocessor import letter_surface as PL
    monkeypatch.setattr(PL, "answer", lambda case, choice, dest="", text="", question="": calls["answers"].append(
        (case["id"], choice, dest)) or {"ok": True, "said": "Marked as submitted."})
    ev = types.SimpleNamespace(action_id="letter_submitted", value="", user_id="U1", text="", selected="av8",
                               card=types.SimpleNamespace(channel="dm", id="m3", thread_key="m3"))
    ns["handle_letter"](ev)
    assert calls["answers"] == [("case-av", "submitted", "av8")]


# ── Home ──────────────────────────────────────────────────────────────

def _src(**over):
    import home_screen as H
    now = datetime(2026, 11, 23, 13, 10, tzinfo=ZONE).timestamp()
    src = H.Sources(now=now, zone=ZONE)
    for k, v in over.items():
        setattr(src, k, v)
    return src


def test_home_lists_one_item_per_case_in_place_of_its_rows_and_cards():
    import home_screen as H
    rows = {r["id"]: r for r in letter_rows()}
    card = {"kind": "letter_ask", "actions": [{"id": "letter_yes", "value": {"case": "case-jp", "rows": ["task-jp1"]}}]}
    src = _src(rows=rows, letters=cases(),
               requests={"waiting": [{"id": "r9", "channel": "dm", "thread_key": "t9", "kind": "card",
                                      "waiting": True, "action": "Letter request", "what": "x", "urgency": "calm",
                                      "card": card}]},
               replies=[{"id": "task-jp1", "conversation_id": "conv-jp1", "age_days": 2, "subject": "Reference"},
                        {"id": "response-x", "conversation_id": "c-x", "age_days": 2, "subject": "Intro"}])
    left = H.left(src)
    letters = [x for x in left if x["kind"] == "letter"]
    # Rowan's date is three weeks out: on the briefing's Letters list, not Home
    assert sorted(x["id"] for x in letters) == ["letter:case-av", "letter:case-jp"]
    assert not any(x["id"] in ("request:r9", "reply:task-jp1") for x in left)
    assert any(x["id"] == "reply:response-x" for x in left)
    av = next(x for x in letters if x["id"] == "letter:case-av")
    assert av["title"] == "Recommendation letter for Avery Quinn" and av["urgency"] == "soon"
    assert av["detail"].startswith("3 of 9 submitted.")
    jp = next(x for x in letters if x["id"] == "letter:case-jp")
    assert jp["title"] == "Will you write it? Reference letter for Tamsin Okafor." and jp["href"] == "#/dm/t9/r9"
    payload = H.build(src)
    assert "task-" not in json.dumps([x for x in payload["left"] if x["kind"] == "letter"])


def test_home_leaves_a_far_case_to_the_briefing_and_hides_kept_names():
    far = riley()
    far["destinations"][0]["due"] = "2027-02-01"
    items, _taken = L.home_items(_src(rows={}), [far])
    assert items == []
    rows = {d["ledger_id"]: {"id": d["ledger_id"], "keep_level": "local"} for d in avery()["destinations"]}
    items, _ = L.home_items(_src(rows=rows), [avery()])
    assert items[0]["kept"] == "local" and items[0]["title"] == "A kept letter case" and "ask" not in items[0]


def test_home_quick_briefing_and_choices():
    b = L.home_brief(avery(), TODAY)
    assert b == {"what": "Recommendation letter for Avery Quinn.", "asked": "Write and submit it to 9 destinations.",
                 "due": "Next due tomorrow.",
                 "standing": ["3 of 9 destinations submitted.", "2 destinations with no due date."],
                 "next": "Tacitus can help you submit it to the next destination."}
    ids = [c["id"] for c in L.home_choices(avery(), TODAY)]
    assert ids == ["letter:draft", "letter:submitted:av4", "letter:submitted:av5", "letter:submitted:av6",
                   "letter:date", "open"]
    assert [c["id"] for c in L.home_choices(jordan(), TODAY)] == ["letter:yes", "letter:yes_ask", "letter:no"]
    assert L.home_choice("letter:submitted:av4") == ("submitted", "av4")
    assert L.home_choice("letter:draft") == ("draft", "")
    assert L.home_choice("done") == ("", "") and L.home_choice("letter:close") == ("", "")


def test_home_without_the_store_is_as_it_was():
    import home_screen as H
    src = _src(replies=[{"id": "response-x", "conversation_id": "c-x", "age_days": 2, "subject": "Intro"}])
    assert [x["id"] for x in H.left(src)] == ["reply:response-x"]


def test_home_takes_the_ranked_case_once():
    import home_screen as H
    src = _src(rows={}, letters=[avery()], open_ids=None,
               briefing={"today_top": [{"source": "letter", "ref": "letter:case-av", "score": 65,
                                        "title": "Letter for Avery Quinn: 3 of 9 submitted.",
                                        "reason": "letter due tomorrow"}]})
    ids = [x["id"] for x in H.left(src)]
    assert ids == ["letter:case-av"]


def test_the_still_open_audit_never_asks_about_a_destination():
    import open_claims_audit as oca
    rows = [r for r in letter_rows() if r["id"] == "task-av4"] + [other_row()]
    post = ("Get done today: recommendation portal Northfield Graduate School, and the annual report "
            "is due tomorrow.")
    rec = oca.prepare(agent="morning-briefing", post=post, briefing={}, open_entries=rows, inbox=[], sent=[],
                      events=[], closed_rows=[], archive=[], no_fetch=True, rendered=["task-av4"])
    assert "task-av4" not in {c["id"] for c in rec["claims"]}



# ── the store's own shapes (letters.py) ───────────────────────────────

def _stored_new_case():
    """A person's own request with no destination yet, as letters.cases
    gives it: no decision, its own row, a `write` question and a `join`."""
    return {"id": "case-new", "person_name": "Sage Marlow", "person_email": "sm@example.org",
            "kind": "evaluation", "status": "open", "decision": "", "opened": "2026-11-23",
            "materials": [{"kind": "cv", "ref": "m-own"}, {"kind": "statement", "ref": "m-own"}],
            "base_letter": "", "keep_level": "full", "case_ledger_id": "task-own", "destinations": [],
            "questions": [{"id": "lq-w", "kind": "write", "text": "Will you write it?",
                           "choices": ["Yes", "Yes, ask for materials", "No"], "answer": ""},
                          {"id": "lq-j", "kind": "join", "text": "Is Sage Marlow the same person as S. Marlow?",
                           "choices": ["Same person", "Someone else"], "answer": ""}]}


def test_a_new_case_with_no_destination_yet_is_shown_and_asked():
    c = _stored_new_case()
    assert L.is_open(c) and L.is_new(c)
    rows = [_row("task-own", "conv-own", "Task: evaluation letter", letter={"case": "case-new", "role": "case"})]
    view = L.briefing_view(rows, today=TODAY, cases=[c], closed=[])
    assert view["hide"]["ids"] == ["task-own"]
    assert view["cases"][0]["line"] == ("Letter for Sage Marlow: asked Nov 23, no places named yet. No due date yet. "
                                        "Will you write it? Answer in Action requests.")
    plan = L.card_plan([c], {}, TODAY)
    assert [p["what"] for p in plan] == ["join"]               # the decision waits for who it is
    c_no_join = dict(c, questions=c["questions"][:1])
    assert [p["what"] for p in L.card_plan([c_no_join], {}, TODAY)] == ["ask"]
    spec = L.decision_spec(c, TODAY)
    assert spec["actions"][0][2]["question"] == "lq-w" and spec["actions"][0][2]["rows"] == ["task-own"]
    assert "It names no places to send it yet." in spec["lines"]
    assert "The request included: a CV, a statement." in spec["lines"]
    join = L.join_spec(c, L.questions(c, "join")[0])
    assert [a[0] for a in join["actions"]] == ["letter_join_same", "letter_join_other"]
    assert "Sage Marlow" not in json.dumps(L.join_spec(c, L.questions(c, "join")[0], "local"))
    # once answered, the question's card gives way
    c["questions"][1]["answer"] = "Someone else"
    cards = {"q:lq-j": {"kind": "join", "live": True}}
    assert L.card_plan([c], cards, TODAY) == [{"case": "case-new", "what": "ask", "keys": []},
                                              {"case": "q:lq-j", "what": "retire"}]


def test_the_stores_line_is_used_at_the_cases_level(monkeypatch):
    seen = []

    def summary_line(c, *, level=None, today=None):
        seen.append((level, today))
        who = c["person_name"] if level in ("full", "summary") else ""
        return f"Letter for {who or '(kept)'}: will you write it? no places to send it yet."
    monkeypatch.setattr(L, "_letters", lambda: types.SimpleNamespace(summary_line=summary_line))
    c = _stored_new_case()
    assert L.line(c, TODAY) == ("Letter for Sage Marlow: will you write it? no places to send it yet. "
                                "Answer in Action requests.")
    assert seen[-1] == ("full", TODAY)
    assert "Sage" not in L.line(c, TODAY, lv="local")


def test_the_tick_asks_a_join_question_once_and_routes_its_answer(monkeypatch):
    ns, calls, store = _bot(monkeypatch, [_stored_new_case()])
    assert ns["_post_letter_cards"](force=True) == 1          # who it is comes first
    assert [c.title for c in calls["cards"]] == [":memo: *Is this the same person?*"]
    assert store["cards"]["q:lq-j"]["kind"] == "join"
    assert ns["_post_letter_cards"](force=True) == 0
    from preprocessor import letter_surface as PL
    seen = []
    monkeypatch.setattr(PL, "answer", lambda case, choice, dest="", text="", question="": seen.append(
        (choice, question)) or {"ok": True, "said": "Recorded: one letter case now."})
    ev = types.SimpleNamespace(action_id="letter_join_same", value={"case": "case-new", "question": "lq-j"},
                               user_id="U1", text="", selected="",
                               card=types.SimpleNamespace(channel="dm", id="m2", thread_key="m2"))
    ns["handle_letter"](ev)
    assert seen == [("join_same", "lq-j")] and calls["turns"] == []


# ── Home's item view (home_items) ─────────────────────────────────────

def _live_case(days: int = 1):
    """A case whose two open destinations are due `days` from the real
    today (Home's fixtures run on the real clock)."""
    sys.path.insert(0, str(ROOT / "tests"))
    import home_fixtures as F
    due = (datetime.now(F.ZONE).date() + timedelta(days=days)).isoformat()
    c = avery()
    for d in c["destinations"][3:5]:
        d["due"] = due
    for d in c["destinations"][5:]:
        d["due"] = (datetime.now(F.ZONE).date() + timedelta(days=days + 7)).isoformat()
    return c


def test_home_opens_a_letter_item_with_its_brief_and_choices(monkeypatch):
    sys.path.insert(0, str(ROOT / "tests"))
    import home_fixtures as F
    import home_items as I
    import home_screen as H
    case = _live_case()
    monkeypatch.setattr(L, "load_cases", lambda conn=None, open_only=True: [case])
    src = F.sources()
    src.letters = [case]
    it = I.item(src, "letter:case-av")
    assert it["kind"] == "letter" and it["label"] == "Letter"
    assert it["brief"]["what"] == "Recommendation letter for Avery Quinn."
    assert it["brief"]["due"] == "Next due tomorrow."
    assert [c["id"] for c in it["choices"]] == ["letter:draft", "letter:submitted:av4", "letter:submitted:av5",
                                                "letter:submitted:av6", "open"]
    # its briefing is case_briefing's; with no ledger to read its rows, the
    # case shows from itself alone and fails closed (no name, no place)
    assert it["deeper"]["state"] in ("ready", "none") and "What was asked" in it["deeper"]["text"]
    assert "Avery" not in it["deeper"]["text"]
    called = []

    class FE:
        owner_id, main_channel, cfg = "owner", "main", {}
        store = types.SimpleNamespace(home_get=lambda n: {}, home_set=lambda n, v: None,
                                      message=lambda ch, mid: {"channel": "dm", "id": mid, "thread_key": mid},
                                      channel=lambda cid: {"id": cid})

        def home_changed(self):
            called.append("changed")

        def dm_channel(self, user):
            return "dm"
    h = H.Home(FE(), clock=lambda: F.at(13, 10), sources=lambda tz: src, background=False)
    h.hooks.letter = lambda cid, answer, dest, words: called.append((cid, answer, dest, words)) or \
        {"ok": True, "said": "Marked as submitted."}
    got, _after = h.act("letter:case-av", "letter:submitted:av4")
    assert got == {"ok": True, "said": "Marked as submitted."}
    assert ("case-av", "submitted", "av4", "") in called
    got, _after = h.act("letter:case-av", "done")
    assert got["ok"] is False                                  # no Done for a letter
    h.hooks.letter = lambda cid, answer, dest, words: {"ok": True, "said": "I am writing the letter's draft.",
                                                       "channel": "dm", "id": "p9"}
    got, _after = h.act("letter:case-av", "letter:draft", "keep it to one page")
    assert got["goto"] == "#/dm/p9/p9"


def test_the_bots_home_hook_runs_the_answer_and_its_turn(monkeypatch):
    ns, calls, _store = _bot(monkeypatch, [jordan()])
    ns["OWNER_SLACK_USER_ID"] = "U1"
    from preprocessor import letter_surface as PL
    seen = []
    monkeypatch.setattr(PL, "answer", lambda case, choice, dest="", text="", question="": seen.append(
        (choice, question)) or {"ok": True, "said": "I am writing the request for the materials.",
                                "turn": "Ask for the materials."})
    got = ns["_home_letter"]("case-jp", "yes_ask")
    assert got["ok"] and got["channel"] == "dm" and got["id"] == "p1"
    assert seen == [("yes_ask", "")]                          # no write question in this made-up case
    assert calls["turns"][0][0][3] == "Ask for the materials."
    assert ns["_home_letter"]("case-gone", "yes")["ok"] is False
