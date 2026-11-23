"""The briefing on a case (preprocessor/case_briefing.py, docs/webchat.md,
Home, "The briefing on a case"): the packet at each keep level, the moves
a case allows and code's check of the steps, the store and the owner's
last look, the refresh on each kind of change, the new-message line, the
hosted rewrite, the Home item and its steps through the case card's own
answers, and the block in Action requests. Made-up data only."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))

import case_briefing as CB  # noqa: E402
import home_deeper as D  # noqa: E402
from plain_words import JARGON  # noqa: E402

ZONE = ZoneInfo("America/Denver")
NOW = time.time()
TODAY = datetime.fromtimestamp(NOW, ZONE).date()
PORTAL = "https://apply.example.com/ref/k3y-SECRET-9"
QUOTE = "PRIVATE WORDS FROM THE MAIL"
LETTER_TEXT = "Rowan is the strongest student I have taught."


def day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def iso(hours_ago: float) -> str:
    return datetime.fromtimestamp(NOW - hours_ago * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A ledger with the letter tables in a temp file, the briefings' store
    beside it, the keep levels from the rows' own labels, the owner's own
    model ready (a stand-in answers), no rules and no calendar."""
    import keep_gate
    import ledger
    import letters
    db = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger, "DB_PATH", db)
    conn = letters.connect(db)
    monkeypatch.setattr(keep_gate, "levels", lambda items, **kw: {
        str(i.get("id")): keep_gate.label_level(i) for i in items})
    monkeypatch.setattr(D, "_store_path", lambda: tmp_path / "home-briefings.sqlite")
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: True)
    monkeypatch.setattr(D, "_zone", lambda: ZONE)
    monkeypatch.setattr(D, "_log", lambda rec: None)
    monkeypatch.setattr(CB, "_log", lambda rec: None)
    monkeypatch.setattr(CB, "_rules", lambda: "")
    monkeypatch.setattr(CB, "_briefing_now", lambda: {})
    calls = []
    monkeypatch.setattr(CB, "LOCAL", lambda prompt: calls.append(prompt) or {
        "ok": True, "text": json.dumps(ANSWER), "usage": {"in": 10, "out": 5}, "reason": ""})
    monkeypatch.setattr(CB, "HOSTED", lambda prompt: {"ok": False, "text": "", "usage": {}, "reason": "error"})
    CB._LOADED.update(at=0.0, key=None, cases=[])
    yield types.SimpleNamespace(conn=conn, letters=letters, db=db, calls=calls, tmp=tmp_path,
                                store=CB.Store(tmp_path / "home-briefings.sqlite"))
    conn.close()


ANSWER = {"history": ["Rowan Vale asked for the letter and named Harbor Tech PhD."],
          "stakes": ["Harbor Tech PhD needs the letter by its date."],
          "unknown": ["The records do not show a statement."],
          "steps": [{"kind": "draft", "part": "", "date": "", "why": "no draft yet"},
                    {"kind": "open_link", "part": "p1", "date": "", "why": "its portal"},
                    {"kind": "set_due", "part": "p2", "date": "", "why": "no date"}]}


def make_case(env, *, level="full", decide=True, drafted=False, person_wrote=True) -> str:
    """A recommendation letter for a made-up person, two places: one portal
    with a link and a date, one by email with no date; the person's own mail
    (with a CV attached, a quote and a gist) on the case's row."""
    L, conn = env.letters, env.conn
    cid = L.ensure_case(conn, "Rowan Vale", "rowan.vale@example.org", "recommendation", keep_level=level)
    L.add_destination(conn, cid, "Harbor Tech PhD", due=day(9), method="portal", link=PORTAL,
                      conversation_id="conv-portal", message_id="msg-portal")
    L.add_destination(conn, cid, "Pinecrest College MS", method="email", conversation_id="conv-pine",
                      message_id="msg-pine")
    if person_wrote:
        conn.execute("INSERT INTO ledger (id, type, status, description, from_email, conversation_id, "
                     "date_identified, metadata, created_at, updated_at) VALUES (?, 'task', 'open', ?, ?, ?, ?, ?, ?, ?)",
                     ("task-own", "Letter for Rowan Vale", "rowan.vale@example.org", "conv-own", iso(80), "{}",
                      iso(80), iso(80)))
        L.link_row(conn, "task-own", cid, role="case")
        conn.execute("UPDATE cases SET case_ledger_id = 'task-own' WHERE id = ?", (cid,))
        set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>",
                                        "Rowan Vale asked for a recommendation letter for two programs.",
                                        files=["Rowan_Vale_CV.pdf"])])
    if decide:
        L.accept(conn, cid, actor="owner")
    if drafted:
        L.record_draft(conn, cid, "/letters/rowan-vale/letter.md")
    conn.commit()
    return cid


def msg(mid, hours_ago, sender, gist, *, own=False, files=None):
    return {"id": mid, "received": iso(hours_ago), "from": sender, "own": own, "gist": gist,
            "quotes": [QUOTE], **({"attachments": files} if files else {})}


def set_brief(env, lid, messages):
    row = env.conn.execute("SELECT metadata FROM ledger WHERE id = ?", (lid,)).fetchone()
    meta = json.loads(row["metadata"] or "{}")
    latest = max(m["received"] for m in messages)
    meta["thread_brief"] = {"messages": messages, "latest": latest, "message_count": len(messages),
                            "last_inbound": max((m["received"] for m in messages if not m["own"]), default="")}
    env.conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?", (json.dumps(meta), lid))
    env.conn.commit()


def one(env, cid) -> dict:
    return CB.load_one(cid)


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "\u2014" not in text and "\u2013" not in text, text


# ── the packet at each keep level ─────────────────────────────────────

@pytest.mark.parametrize("level", ["full", "summary", "facts", "local"])
def test_the_packet_holds_only_what_its_level_shows(env, level):
    cid = make_case(env, level=level)
    x = one(env, cid)
    assert x["level"] == level
    prep = CB.prepare(x, now=NOW)
    text = json.dumps(prep["packet"], ensure_ascii=False)
    # never a link, an address, a quote from mail, a file's text or a path
    for never in (PORTAL, "apply.example.com", "rowan.vale@example.org", QUOTE, LETTER_TEXT, "/letters/"):
        assert never not in text
    assert ("Rowan Vale" in text) == (level != "local")
    assert ("Harbor Tech PhD" in text) == (level in ("full", "summary"))
    assert ("Rowan_Vale_CV.pdf" in text) == (level in ("full", "summary"))
    assert ("asked for a recommendation letter for two programs" in text) == (level in ("full", "summary"))
    assert '"kind": "cv"' in text                      # a CV came in, by kind, at every level
    assert prep["packet"]["places"][0]["part"] == "p1"
    assert "Thursday" in text or "day" in prep["packet"]["places"][0]["due"]


def test_an_address_or_a_link_in_the_mail_record_never_reaches_a_packet(env):
    cid = make_case(env)
    set_brief(env, "task-own", [msg("m1", 5, "Rowan Vale <rowan.vale@example.org>",
                                    "Rowan Vale wrote from rowan.v@example.net with the form at "
                                    "https://forms.example.com/r/SECRETKEY.")])
    env.conn.execute("UPDATE cases SET counterpart_name = '' WHERE id = ?", (cid,))
    env.conn.commit()
    prep = CB.prepare(one(env, cid), now=NOW)
    text = json.dumps(prep["packet"])
    assert "rowan.v@example.net" not in text and "SECRETKEY" not in text and "example.org" not in text
    assert "wrote from an address with the form at a link" in text


def test_the_packet_never_carries_the_letter_or_its_file_contents(env):
    cid = make_case(env, drafted=True)
    (env.tmp / "letter.md").write_text(LETTER_TEXT)
    prep = CB.prepare(one(env, cid), now=NOW)
    text = json.dumps(prep["packet"])
    assert LETTER_TEXT not in text and "letter.md" not in text
    assert prep["packet"]["case"]["work"] == "the letter: drafted"


# ── the moves a case allows, and code's check of the steps ───────────

def moves(env, cid):
    return [CB._move_key(m) for m in CB.allowed_moves(CB.view(one(env, cid), today=TODAY))]


def test_a_new_case_offers_no_draft_and_says_no_through_its_decision(env):
    cid = make_case(env, decide=False)
    got = moves(env, cid)
    assert "draft" not in got and "decline" in got and "ask_materials" in got
    v = CB.view(one(env, cid), today=TODAY)
    step = {"id": "s1", "kind": "decline", "params": {}}
    [s] = CB.offered([step], v, today=TODAY)
    assert CB.choice_of(s, v) == ("letter:no", "")         # the decision card's No, not Decline


def test_an_accepted_case_offers_draft_dates_its_portal_and_decline(env):
    cid = make_case(env)
    got = moves(env, cid)
    assert got[0] == "draft" and "set_due p2" in got and "open_link p1" in got
    assert "set_due p1" not in got                         # it has a date
    assert "open_link p2" not in got                       # no link on the email place
    assert not [m for m in got if m.startswith("mark_delivered")]   # no letter yet
    assert "decline" in got


def test_only_a_place_whose_letter_exists_may_be_marked_submitted(env):
    cid = make_case(env, drafted=True)
    got = moves(env, cid)
    assert "mark_delivered p1" in got and "mark_delivered p2" in got


def test_a_kept_case_gets_no_draft_no_ask_and_no_portal(env):
    cid = make_case(env, level="facts")
    got = moves(env, cid)
    assert "draft" not in got and "ask_materials" not in got
    assert not [m for m in got if m.startswith("open_link")]
    assert "set_due p2" in got and "decline" in got


def test_check_step_refuses_what_the_case_does_not_allow(env):
    cid = make_case(env)
    prep = CB.prepare(one(env, cid), now=NOW)
    known = D.Known(prep["packet"])

    def check(**raw):
        return CB.check_step({"part": "", "date": "", "why": "", **raw}, allowed=prep["allowed"], known=known,
                             today=TODAY)
    assert check(kind="draft")[0] == {"kind": "draft", "params": {}}
    assert check(kind="mark_delivered", part="p1")[0] is None          # no letter yet
    assert check(kind="open_link", part="p2")[0] is None               # no link
    assert check(kind="open_link", part="p9")[0] is None               # no such place
    assert check(kind="send_it")[0] is None
    assert check(kind="set_due", part="p2")[0] == {"kind": "set_due", "params": {"part": "p2"}}
    # a date only when the records state it
    assert check(kind="set_due", part="p2", date=day(20))[1] == "a date the records do not state"
    assert check(kind="set_due", part="p1")[0] is None                 # it has a date: the card's own button
    assert check(kind="set_due", part="p2", date=day(-2))[1] == "date out of range"


def test_a_new_date_the_records_state_may_be_set(env):
    cid = make_case(env)
    moved = TODAY + timedelta(days=14)
    set_brief(env, "task-own", [msg("m3", 3, "Rowan Vale <rowan.vale@example.org>",
                                    f"Rowan Vale says Pinecrest College MS wants it by {moved:%B} {moved.day}.")])
    prep = CB.prepare(one(env, cid), now=NOW)
    got, why = CB.check_step({"kind": "set_due", "part": "p2", "date": moved.isoformat(), "why": ""},
                             allowed=prep["allowed"], known=D.Known(prep["packet"]), today=TODAY)
    assert got == {"kind": "set_due", "params": {"part": "p2", "date": moved.isoformat()}}, why
    v = CB.view(one(env, cid), today=TODAY)
    [s] = CB.offered([dict(got, id="s1")], v, today=TODAY)
    assert s["pub"]["label"] == f"Set the due date: {moved:%a}, {moved:%b} {moved.day}" and "input" not in s["pub"]
    assert CB.choice_of(s, v) == ("letter:date:" + s["part"]["id"], moved.isoformat())


def test_the_answer_is_checked_against_the_packet(env):
    cid = make_case(env)
    prep = CB.prepare(one(env, cid), now=NOW)
    got = CB.check_answer(dict(ANSWER, stakes=["Juniper Labs wants it by Friday the 13th."],
                               steps=ANSWER["steps"] + [{"kind": "mark_delivered", "part": "p1", "date": "", "why": ""}]),
                          prep["packet"], allowed=prep["allowed"], today=TODAY)
    assert "Tacitus could not check this." in got["text"]
    assert "Juniper" not in got["text"]
    assert [s["kind"] for s in got["steps"]] == ["draft", "open_link", "set_due"]
    assert got["rejected"][0]["why"].startswith("mark_delivered p1")


def test_the_steps_as_the_card_shows_them(env):
    cid = make_case(env, drafted=True)
    v = CB.view(one(env, cid), today=TODAY)
    steps = [{"id": "s1", "kind": "open_link", "params": {"part": "p1"}},
             {"id": "s2", "kind": "set_due", "params": {"part": "p2"}},
             {"id": "s3", "kind": "mark_delivered", "params": {"part": "p1"}},
             {"id": "s4", "kind": "ask_materials", "params": {}},
             {"id": "s5", "kind": "draft", "params": {}}]
    pub = {s["pub"]["id"]: s["pub"] for s in CB.offered(steps, v, today=TODAY)}
    assert pub["s1"] == {"id": "s1", "label": "Open the portal", "detail": "Harbor Tech PhD", "href": PORTAL}
    assert pub["s2"]["input"] == "date" and pub["s2"]["detail"] == "Pinecrest College MS"
    assert pub["s3"]["confirm"] == "Is the letter for Harbor Tech PhD submitted?"
    assert "It waits for your Send." in pub["s4"]["confirm"] and "statement" in pub["s4"]["detail"]
    assert pub["s5"]["label"] == "Change the letter"
    for p in pub.values():
        assert len(p["label"]) <= CB.LABEL_MAX
        plain(p["label"] + p.get("detail", "") + p.get("confirm", ""))


def test_a_place_with_no_name_is_numbered_and_the_ask_names_what_is_missing(env):
    cid = make_case(env)
    env.letters.add_destination(env.conn, cid, "", method="portal", link="https://apply.example.com/ref/zz",
                                conversation_id="conv-z", message_id="msg-z")
    set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale asked.",
                                    files=["Rowan_Vale_CV.pdf"]),
                                msg("m2", 70, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale sent it again.",
                                    files=["Rowan_Vale_CV.pdf"])])
    x = one(env, cid)
    v = CB.view(x, today=TODAY)
    assert v["parts"][-1]["name"] == "Place 3"
    text = CB.card(x, None, now=NOW, since=None)["text"]
    assert "- Place 3: no due date, to write, portal link" in text
    assert "- A CV (Rowan_Vale_CV.pdf)." in text                 # one file, named once
    [s] = CB.offered([{"id": "s1", "kind": "ask_materials", "params": {}}], v, today=TODAY)
    assert s["pub"]["detail"] == "a statement and the due date for 2 places"
    assert s["pub"]["confirm"] == "Tacitus drafts a short ask for a statement and the due date for 2 places. " \
                                  "It waits for your Send."


# ── the store and the owner's last look ──────────────────────────────

def test_the_store_is_owner_private_and_keeps_one_briefing_per_case(env):
    st = env.store
    st.put("lc-1", {"version": "v1", "by": "local", "at": 1.0, "level": "full", "text": "T", "steps": [{"id": "s1"}]})
    assert st.get("lc-1")["steps"] == [{"id": "s1"}]
    assert st.versions() == {"lc-1": ("v1", "full", "local")}
    assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o600
    st.put("lc-2", {"version": "v2", "text": "U"})
    assert st.drop({"lc-2"}) == 1 and st.get("lc-1") is None and st.get("lc-2")["text"] == "U"
    with sqlite3.connect(st.path) as c:                # the row briefings' table is left alone
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master")} >= {"briefings", "case_briefings"}


def test_a_look_starts_a_visit_and_since_counts_from_the_visit_before(env):
    st = env.store
    assert st.look("lc-1", 1000.0) is None             # the first look: nothing to compare with
    assert st.look("lc-1", 1000.0 + 60) is None        # the same visit
    assert st.look("lc-1", 1060.0 + CB.VISIT_S + 1) == 1060.0   # a new visit: since the last look
    assert st.since("lc-1") == 1060.0
    assert st.look("lc-1", 1060.0 + CB.VISIT_S + 30) == 1060.0  # within it, the same baseline


# ── what changed since he last looked: the new-message line ──────────

def test_a_new_message_from_them_shows_with_its_gist(env):
    cid = make_case(env)
    set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale asked for a letter."),
                                msg("m2", 2, "Rowan Vale <rowan.vale@example.org>",
                                    "Rowan Vale sent a new deadline for Pinecrest College MS.")])
    x = one(env, cid)
    v = CB.view(x, today=TODAY)
    lines = CB.since_lines(v, NOW - 24 * 3600, now=NOW)
    assert lines[0].startswith("A new message from Rowan Vale on ")
    assert lines[0].endswith(": Rowan Vale sent a new deadline for Pinecrest College MS.")
    assert QUOTE not in " ".join(lines)
    assert v["last_inbound"]["who"] == "them" and v["last_inbound"]["gist"].startswith("Rowan Vale sent")
    assert CB.since_lines(v, None, now=NOW) == []      # no earlier look: nothing to compare


@pytest.mark.parametrize("level,head", [("facts", "A new message from Rowan Vale on "),
                                        ("local", "A new message from them on ")])
def test_a_kept_case_says_a_message_came_without_its_gist(env, level, head):
    cid = make_case(env, level=level)
    set_brief(env, "task-own", [msg("m2", 2, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale wrote again.")])
    lines = CB.since_lines(CB.view(one(env, cid), today=TODAY), NOW - 24 * 3600, now=NOW)
    assert lines[0].startswith(head) and lines[0].endswith(".") and "wrote again" not in lines[0]


def test_since_names_a_moved_date_a_new_file_and_a_submission(env):
    L = env.letters
    cid = make_case(env, drafted=True)
    x = one(env, cid)
    p1, p2 = [d["id"] for d in x["case"]["destinations"]]
    since = time.time() - 1
    L.set_due(env.conn, p1, day(12))
    L.add_material(env.conn, cid, "statement", "msg-own#statement.pdf")
    L.set_state(env.conn, p2, "submitted", "the owner said so")
    env.conn.commit()
    lines = CB.since_lines(CB.view(one(env, cid), today=TODAY), since, now=time.time() + 1)
    assert any(ln.startswith("Harbor Tech PhD: due date moved from ") for ln in lines)
    assert "A statement came in." in lines and "Pinecrest College MS: marked submitted." in lines


# ── the card's code-built lines ───────────────────────────────────────

def test_the_card_lists_the_places_what_came_in_and_what_is_missing(env):
    cid = make_case(env)
    got = CB.card(one(env, cid), None, now=NOW, since=None)
    text = got["text"]
    assert "Now\n- Next due " in text and "in 9 days: Harbor Tech PhD." in text
    assert "- A recommendation letter for Rowan Vale, asked " in text
    assert "- Rowan Vale asked you directly." in text and "- You said yes." in text
    assert "The places\n- Harbor Tech PhD: due " in text and ", to write, portal link" in text
    assert "- Pinecrest College MS: no due date, to write, by email" in text
    assert "What came in\n- A CV (Rowan_Vale_CV.pdf)." in text
    assert "Still missing\n- The letter.\n- A statement.\n- The due date for 1 place." in text
    assert got["steps"] == [] and PORTAL not in text
    plain(text)
    requests = CB.card(one(env, cid), None, now=NOW, since=None, places=False)["text"]
    assert "The places" not in requests and "Still missing" not in requests and "What was asked" in requests


def test_a_local_case_names_no_one_and_no_place(env):
    cid = make_case(env, level="local")
    text = CB.card(one(env, cid), None, now=NOW, since=None)["text"]
    assert "Rowan" not in text and "Harbor" not in text and "Place 1: due " in text
    assert "A CV" in text and "pdf" not in text


# ── refresh on each kind of change ────────────────────────────────────

def test_refresh_writes_once_and_again_on_each_kind_of_change(env):
    L = env.letters
    cid = make_case(env)
    got = CB.refresh(store=env.store, daily=None)
    assert got["written"] == 1 and len(env.calls) == 1
    assert CB.refresh(store=env.store, daily=None)["stale"] == 0          # nothing changed: no call
    p1, p2 = [d["id"] for d in one(env, cid)["case"]["destinations"]]

    def changed(why):
        n = len(env.calls)
        got = CB.refresh(store=env.store, daily=None)
        assert got["written"] == 1 and len(env.calls) == n + 1, why
    # a new mail joined: its message on the case's row, and the store's event
    set_brief(env, "task-own", [msg("m9", 1, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale wrote again.")])
    changed("a new message")
    import cases as store  # the letters' events are the case store's
    store._event(env.conn, cid, "mail", part_id=p1, evidence={"message": "msg-r", "role": "reminder"})
    env.conn.commit()
    changed("a mail joined")
    L.set_state(env.conn, p1, "drafted", "/letters/x.md")
    env.conn.commit()
    changed("a state")
    L.set_due(env.conn, p2, day(20))
    env.conn.commit()
    changed("a date")
    L.add_material(env.conn, cid, "statement", "msg-own#statement.pdf")
    env.conn.commit()
    changed("a material")


def test_a_case_call_leaves_the_row_briefings_calls_alone(env):
    """Row jobs run in the bot's threads beside case jobs: a case's call
    never swaps home_deeper's own."""
    before = (D.LOCAL, D.HOSTED)
    got = CB.run_model("the records", ["local"])
    assert got["ok"] and (D.LOCAL, D.HOSTED) == before and env.calls == ["the records"]


def test_refresh_keeps_a_kept_case_on_the_owners_own_model(env, monkeypatch):
    make_case(env, level="local")
    hosted = []
    monkeypatch.setattr(CB, "LOCAL", lambda p: {"ok": False, "text": "", "usage": {}, "reason": "timeout"})
    monkeypatch.setattr(CB, "HOSTED", lambda p: hosted.append(p) or {"ok": True, "text": json.dumps(ANSWER)})
    got = CB.refresh(store=env.store, daily=None)
    assert got["failed"] == 1 and hosted == []           # fails closed: never the hosted model


def test_refresh_needs_the_owners_own_model_and_keeps_its_daily_bound(env, monkeypatch):
    make_case(env)
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: False)
    assert CB.refresh(store=env.store)["skipped"] == "no local model"
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: True)
    env.store.count(D._today_key(), "case-ingest", CB.DAILY)
    assert CB.refresh(store=env.store)["skipped"] == "the day's writes are spent" and env.calls == []


def test_a_closed_case_loses_its_briefing(env):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    env.letters.decline(env.conn, cid, actor="owner")
    env.conn.commit()
    CB.refresh(store=env.store, daily=None)
    assert env.store.get(cid) is None


# ── the hosted rewrite of the day's list ──────────────────────────────

def test_the_hosted_pass_rewrites_the_cases_on_homes_list_and_leaves_kept_ones(env):
    cid = make_case(env)
    kept_id = env.letters.ensure_case(env.conn, "Sam Okafor", "sam@example.net", "reference", keep_level="local")
    env.letters.add_destination(env.conn, kept_id, "Northfield Fellowship", due=day(5), method="portal")
    env.conn.commit()
    seen = []

    def call(prompt):
        seen.append(prompt)
        return {"ok": True, "model": "", "usage": {"in": 100, "out": 50},
                "text": json.dumps({"items": [dict(ANSWER, ref=f"case:{cid}"), dict(ANSWER, ref=f"case:{kept_id}")]})}
    got = CB.hosted_pass(store=env.store, call=call, now=NOW)
    assert got["cases"] == 1 and got["kept"] == 1 and got["stored"] == 1 and got["calls"] == 1
    assert env.store.get(cid)["by"] == "hosted" and env.store.get(kept_id) is None
    assert "Northfield" not in seen[0] and "Sam Okafor" not in seen[0] and PORTAL not in seen[0]


def test_a_failed_hosted_pass_leaves_the_local_briefing(env):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    got = CB.hosted_pass(store=env.store, call=lambda p: {"ok": False, "reason": "limit"}, now=NOW)
    assert got["stored"] == 0 and env.store.get(cid)["by"] == "local"


def test_a_case_far_out_is_not_on_the_days_list(env):
    cid = make_case(env)
    for d in one(env, cid)["case"]["destinations"]:
        env.letters.set_due(env.conn, d["id"], day(40))
    env.conn.commit()
    assert CB.hosted_pass(store=env.store, call=lambda p: pytest.fail("no call"), now=NOW)["cases"] == 0


# ── the Home item and its steps ───────────────────────────────────────

class FE:
    owner_id, main_channel, cfg = "owner", "main", {}

    def __init__(self):
        rec: dict = {}
        self.emitted = []
        self.store = types.SimpleNamespace(home_get=lambda n: rec.get(n, {}), home_set=rec.__setitem__,
                                           home_item_emit=self.emitted.append,
                                           message=lambda ch, mid: {"channel": "dm", "id": mid, "thread_key": mid},
                                           channel=lambda cid: {"id": cid})
        self._home_deeper = D.Jobs(self, clock=time.time, background=False)

    def home_changed(self):
        pass

    def dm_channel(self, user):
        return "dm"


def home(env, monkeypatch, cid):
    import home_fixtures as F
    import home_screen as H
    import letter_surface as LS
    cases = lambda conn=None, open_only=True: [c for c in env.letters.cases(env.conn, open_only=open_only)]  # noqa: E731
    monkeypatch.setattr(LS, "load_cases", cases)
    src = F.sources()
    src.letters = cases()
    fe = FE()
    h = H.Home(fe, clock=lambda: F.at(13, 10), sources=lambda tz: src, background=False)
    pressed = []
    h.hooks.letter = lambda case_id, answer, dest, words: pressed.append((case_id, answer, dest, words)) or \
        {"ok": True, "said": "Done on the card."}
    return h, fe, pressed


def test_the_home_item_shows_the_stored_briefing_and_its_steps(env, monkeypatch):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    h, fe, _pressed = home(env, monkeypatch, cid)
    it = h.item(f"letter:{cid}")
    d = it["deeper"]
    assert d["state"] == "ready" and not d.get("stale") and d["by"] == "local"
    assert d["text"].startswith("Now\n") and "What happened\n- Rowan Vale asked" in d["text"]
    assert [s["label"] for s in d["steps"]] == ["Draft the letter", "Open the portal", "Set the due date",
                                                "Continue in a conversation"]
    assert d["steps"][1]["href"] == PORTAL and d["steps"][2]["input"] == "date"
    plain(d["text"])


def test_a_step_runs_the_case_cards_own_answer(env, monkeypatch):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    h, fe, pressed = home(env, monkeypatch, cid)
    p2 = one(env, cid)["case"]["destinations"][1]["id"]
    ref = f"letter:{cid}"
    got, _ = h.act(ref, "step:s1")
    assert got["ok"] and pressed[-1] == (cid, "draft", "", "")
    got, _ = h.act(ref, "step:s3")                       # its date first
    assert got == {"ok": False, "said": "Pick the date first."} and len(pressed) == 1
    got, _ = h.act(ref, "step:s3", day(15))
    assert got["ok"] and pressed[-1] == (cid, "date", p2, day(15))
    got, _ = h.act(ref, "step:s2")                       # the portal: a link, nothing pressed
    assert got == {"ok": True, "said": "Opening the portal.", "goto": PORTAL} and len(pressed) == 2


def test_a_step_home_does_not_list_runs_as_its_card_button(env, monkeypatch):
    """Ask for what is missing is the card's Yes, ask for materials (not one
    of Home's choices for an accepted case); Decline is the card's Decline."""
    cid = make_case(env)
    env.store.put(cid, {"version": one(env, cid)["version"], "level": "full", "by": "local", "at": NOW,
                        "text": "What happened\n- Rowan Vale asked for it.",
                        "steps": [{"id": "s1", "kind": "ask_materials", "params": {}},
                                  {"id": "s2", "kind": "decline", "params": {}}]})
    h, fe, pressed = home(env, monkeypatch, cid)
    ids = [c["id"] for c in h.item(f"letter:{cid}")["choices"]]
    assert "letter:yes_ask" not in ids and "letter:decline" not in ids
    assert h.act(f"letter:{cid}", "step:s1")[0]["ok"] and pressed[-1][1] == "yes_ask"
    assert h.act(f"letter:{cid}", "step:s2")[0]["ok"] and pressed[-1][1] == "decline"
    # a choice id posted without a step is still refused
    assert h.act(f"letter:{cid}", "letter:decline")[0]["ok"] is False


def test_a_briefing_older_than_the_case_is_shown_stale_and_its_steps_refused(env, monkeypatch):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    env.letters.set_due(env.conn, one(env, cid)["case"]["destinations"][1]["id"], day(30))
    env.conn.commit()
    h, fe, pressed = home(env, monkeypatch, cid)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "ready" and d["stale"] is True
    got, _ = h.act(f"letter:{cid}", "step:s1")
    assert got["ok"] is False and pressed == []
    assert h.deeper(f"letter:{cid}")["state"] == "ready"     # a tap writes it again (no background here)
    assert not h.item(f"letter:{cid}")["deeper"].get("stale")


def test_with_nothing_stored_the_card_shows_the_facts_and_asks_for_the_rest(env, monkeypatch):
    cid = make_case(env)
    h, fe, _ = home(env, monkeypatch, cid)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "none" and "The places" in d["text"] and d["steps"] == []
    assert h.deeper(f"letter:{cid}") == {"ok": True, "state": "ready"} and len(env.calls) == 1
    assert fe.emitted == [f"letter:{cid}", f"letter:{cid}"]


def test_a_kept_case_with_no_local_model_shows_its_facts_and_says_so(env, monkeypatch):
    cid = make_case(env, level="local")
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: False)
    h, fe, _ = home(env, monkeypatch, cid)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "ready" and d["note"] == D.NOTE_KEPT_NO_LOCAL and "Rowan" not in d["text"]
    assert h.deeper(f"letter:{cid}")["state"] == "unavailable" and env.calls == []


def test_a_kept_case_offers_no_conversation(env, monkeypatch):
    cid = make_case(env, level="facts")
    CB.refresh(store=env.store, daily=None)
    h, fe, _ = home(env, monkeypatch, cid)
    d = h.item(f"letter:{cid}")["deeper"]
    assert D.CONTINUE not in [s["id"] for s in d["steps"]]


def test_opening_the_item_is_the_owners_look(env, monkeypatch):
    cid = make_case(env)
    h, fe, _ = home(env, monkeypatch, cid)
    h.item(f"letter:{cid}")
    assert env.store.since(cid) is None
    with sqlite3.connect(env.store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM case_seen WHERE case_id = ?", (cid,)).fetchone()[0] == 1


# ── Action requests ───────────────────────────────────────────────────

def test_a_case_cards_block_carries_its_briefing(env, monkeypatch):
    import web_requests as wr
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    card = {"kind": "letter", "actions": [{"id": "letter_draft", "value": json.dumps({"case": cid, "rows": []})}]}
    got = wr.context([{"id": "m1", "card": card}])
    b = got["m1"]["briefing"]
    assert b["by"] == "local" and "What happened" in b["text"] and "The places" not in b["text"]
    assert env.store.since(cid) is None                   # Action requests reads; it is no look


def test_the_web_chat_puts_the_briefing_on_the_block(monkeypatch, tmp_path):
    sys.path.insert(0, str(REPO / "botcore"))
    from botcore.chat import web_frontend as wf
    fe = types.SimpleNamespace(store=types.SimpleNamespace(
        channels=lambda: [{"id": "dm", "kind": "dm"}], max_event_seq=lambda: 1,
        requests=lambda dm, limit: {"waiting": [{"id": "m1", "card": {"kind": "letter"}, "waiting": True,
                                                  "kind": "card", "text": "", "posted_at": 1.0}], "done": []}))
    fe.request_context = lambda items: {"m1": {"briefing": {"text": "Now\n- Next due Tue, Dec 1.", "by": "local",
                                                             "at": 5.0, "extra": "dropped"}}}
    out = wf.WebFrontend.requests(fe)
    assert out["waiting"][0]["briefing"] == {"text": "Now\n- Next due Tue, Dec 1.", "by": "local", "at": 5.0}
