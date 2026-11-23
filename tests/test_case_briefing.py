"""The briefing on a case (preprocessor/case_briefing.py, docs/webchat.md,
Home, "The briefing on a case") for every kind from its declaration: the
packet at each keep level, the moves (the store's, never a decline) and
code's check of the steps, the store and the owner's last look (on Home
and in Action requests), the dates a migration wrote kept out, the
refresh on each kind of change, the new-message line, the hosted
rewrite, the Home item of a letter and of a form or a review with their
steps through the case card's own answers, a press while the store waits
for its migration, and a fault that is logged and shown. Made-up data
only; every test runs with HOME, EA_DIR, EA_PRIVATE_DIR and the ledger in
a temp folder."""
from __future__ import annotations

import json
import logging
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


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """No live state in any test here: HOME, EA_DIR and the private dir are
    temp folders, and the ledger the code opens is a file there."""
    import ledger
    home = tmp_path / "home"
    for d in (home / ".ea" / "state", home / ".ea-private"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EA_DIR", str(home / ".ea"))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(home / ".ea-private"))
    monkeypatch.setattr(ledger, "DB_PATH", home / ".ea" / "state" / "obligations-ledger.db")
    monkeypatch.setattr(D, "_store_path", lambda: home / ".ea-private" / "home-briefings.sqlite")
    CB._LOADED.update(at=0.0, key=None, cases=[])
    yield home


@pytest.fixture
def env(hermetic, monkeypatch):
    """The case store in the temp ledger, the keep levels from the rows' own
    labels, the owner's own model ready (a stand-in answers), no rules and
    no calendar."""
    import cases
    import keep_gate
    import ledger
    import letters
    conn = cases.connect(ledger.DB_PATH)
    monkeypatch.setattr(keep_gate, "levels", lambda items, **kw: {
        str(i.get("id")): keep_gate.label_level(i) for i in items})
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
    yield types.SimpleNamespace(conn=conn, cases=cases, letters=letters, calls=calls, tmp=hermetic,
                                store=CB.Store(hermetic / ".ea-private" / "home-briefings.sqlite"))
    conn.close()


ANSWER = {"history": ["Rowan Vale asked for the letter and named Harbor Tech PhD."],
          "stakes": ["Harbor Tech PhD needs the letter by its date."],
          "unknown": ["The records do not show a statement."],
          "steps": [{"kind": "draft", "part": "", "date": "", "why": "no draft yet"},
                    {"kind": "open_link", "part": "p1", "date": "", "why": "its portal"},
                    {"kind": "set_due", "part": "p2", "date": "", "why": "no date"}]}


def make_case(env, *, level="full", decide=True, drafted=False, person_wrote=True) -> str:
    """A recommendation letter for a made-up person, two destinations: one
    portal with a link and a date, one by email with no date; the person's
    own mail (with a CV attached, a quote and a gist) on the case's row."""
    L, conn = env.letters, env.conn
    cid = L.ensure_case(conn, "Rowan Vale", "rowan.vale@example.org", "recommendation", keep_level=level)
    L.add_destination(conn, cid, "Harbor Tech PhD", due=day(9), method="portal", link=PORTAL,
                      conversation_id="conv-portal", message_id="msg-portal")
    L.add_destination(conn, cid, "Pinecrest College MS", method="email", conversation_id="conv-pine",
                      message_id="msg-pine")
    if person_wrote:
        add_row(env, "task-own", "conv-own")
        L.link_row(conn, "task-own", cid, role="case")
        conn.execute("UPDATE cases SET case_ledger_id = 'task-own' WHERE id = ?", (cid,))
        set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>",
                                        "Rowan Vale asked for a recommendation letter for two programs.",
                                        files=["Rowan_Vale_CV.pdf"])])
    if decide:
        L.accept(conn, cid, actor="owner", quote="yes (Action requests)")
    if drafted:
        L.record_draft(conn, cid, "/letters/rowan-vale/letter.md")
    conn.commit()
    return cid


def make_work(env, kind, who, email, title, part, *, due=None, link="", subkind="", decide=True, level="full"):
    """A case of another kind (a form, a review) through the store's own
    verbs, one part on a row with its own mail."""
    c, conn = env.cases, env.conn
    cid = c.ensure_case(conn, kind, who, email, subkind, title=title, keep_level=level, actor="intake")
    pid = c.add_part(conn, cid, part, due or "", "portal" if link else "unknown", link,
                     conversation_id=f"conv-{cid}", message_id=f"msg-{cid}", actor="intake")
    if decide:
        c.accept(conn, cid, actor="owner", quote="yes (Action requests)")
    conn.commit()
    return cid, pid


def add_row(env, lid, conv):
    env.conn.execute("INSERT INTO ledger (id, type, status, description, conversation_id, date_identified, "
                     "metadata, created_at, updated_at) VALUES (?, 'task', 'open', 'A request', ?, ?, '{}', ?, ?)",
                     (lid, conv, iso(80), iso(80), iso(80)))


def msg(mid, hours_ago, sender, gist, *, own=False, files=None):
    return {"id": mid, "received": iso(hours_ago), "from": sender, "own": own, "gist": gist,
            "quotes": [QUOTE], **({"attachments": files} if files else {})}


def set_brief(env, lid, messages):
    row = env.conn.execute("SELECT metadata FROM ledger WHERE id = ?", (lid,)).fetchone()
    meta = json.loads(row["metadata"] or "{}")
    meta["thread_brief"] = {"messages": messages, "latest": max(m["received"] for m in messages),
                            "message_count": len(messages),
                            "last_inbound": max((m["received"] for m in messages if not m["own"]), default="")}
    env.conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?", (json.dumps(meta), lid))
    env.conn.commit()


def one(env, cid) -> dict:
    return CB.load_one(cid)


def plain(text: str) -> None:
    assert not JARGON.search(text), text
    assert "\u2014" not in text and "\u2013" not in text, text


def test_every_path_the_code_opens_is_in_the_temp_folder(hermetic):
    import ledger
    import owner_trust
    assert str(ledger.DB_PATH).startswith(str(hermetic))
    assert str(owner_trust.private_dir()).startswith(str(hermetic))
    assert str(D._store_path()).startswith(str(hermetic))


# ── the packet at each keep level ─────────────────────────────────────

@pytest.mark.parametrize("level", ["full", "summary", "facts", "local"])
def test_the_packet_holds_only_what_its_level_shows(env, level):
    cid = make_case(env, level=level)
    x = one(env, cid)
    assert x["level"] == level
    prep = CB.prepare(x, now=NOW)
    text = json.dumps(prep["packet"], ensure_ascii=False)
    for never in (PORTAL, "apply.example.com", "rowan.vale@example.org", QUOTE, LETTER_TEXT, "/letters/"):
        assert never not in text
    assert ("Rowan Vale" in text) == (level != "local")
    assert ("Harbor Tech PhD" in text) == (level in ("full", "summary"))
    assert ("Rowan_Vale_CV.pdf" in text) == (level in ("full", "summary"))
    assert ("asked for a recommendation letter for two programs" in text) == (level in ("full", "summary"))
    assert '"kind": "cv"' in text                      # a CV came in, by kind, at every level
    assert prep["packet"]["destinations"][0]["part"] == "p1"


def test_the_packet_never_carries_the_letter_or_its_file_contents(env):
    cid = make_case(env, drafted=True)
    (env.tmp / "letter.md").write_text(LETTER_TEXT)
    prep = CB.prepare(one(env, cid), now=NOW)
    text = json.dumps(prep["packet"])
    assert LETTER_TEXT not in text and "letter.md" not in text
    assert prep["packet"]["case"]["work"] == "the letter: drafted"


def test_an_address_or_a_link_in_the_mail_record_never_reaches_a_packet(env):
    cid = make_case(env)
    set_brief(env, "task-own", [msg("m1", 5, "Rowan Vale <rowan.vale@example.org>",
                                    "Rowan Vale wrote from rowan.v@example.net with the form at "
                                    "https://forms.example.com/r/SECRETKEY.")])
    env.conn.execute("UPDATE cases SET counterpart_name = '' WHERE id = ?", (cid,))
    env.conn.commit()
    text = json.dumps(CB.prepare(one(env, cid), now=NOW)["packet"])
    assert "rowan.v@example.net" not in text and "SECRETKEY" not in text and "example.org" not in text
    assert "wrote from an address with the form at a link" in text


# ── the moves: the store's, and never a decline ───────────────────────

def moves(env, cid):
    return [CB._move_key(m) for m in CB.allowed_moves(CB.view(one(env, cid), today=TODAY))]


def test_decline_is_never_a_step_though_the_store_allows_it(env):
    cid = make_case(env)
    v = CB.view(one(env, cid), today=TODAY)
    assert any(m["move"] == "decline" for m in v["moves"])          # the card's own button stays
    assert "decline" not in moves(env, cid) and "decline" not in CB.STEP_KINDS
    prep = CB.prepare(one(env, cid), now=NOW)
    got, why = CB.check_step({"kind": "decline", "part": "", "date": "", "why": ""}, allowed=prep["allowed"],
                             known=D.Known(prep["packet"]), today=TODAY)
    assert got is None and why == "unknown kind"
    assert "decline" not in CB.SCHEMA["properties"]["steps"]["items"]["properties"]["kind"]["enum"]
    assert "decline" not in CB.prompt_text(prep["view"], prep["packet"]).split("The records:")[0]


def test_a_new_case_offers_no_draft(env):
    cid = make_case(env, decide=False)
    got = moves(env, cid)
    assert "draft" not in got and "ask_materials" in got


def test_an_accepted_letter_offers_draft_dates_and_its_portal(env):
    cid = make_case(env)
    got = moves(env, cid)
    assert got[0] == "draft" and "set_due p2" in got and "open_link p1" in got
    assert "set_due p1" not in got                         # it has a date: the card's own button
    assert "open_link p2" not in got and not [m for m in got if m.startswith("mark_delivered")]


def test_only_a_part_whose_work_exists_may_be_marked_delivered(env):
    cid = make_case(env, drafted=True)
    got = moves(env, cid)
    assert "mark_delivered p1" in got and "mark_delivered p2" in got


def test_a_kept_case_gets_no_draft_no_ask_and_no_link(env):
    cid = make_case(env, level="facts")
    got = moves(env, cid)
    assert "draft" not in got and "ask_materials" not in got and not [m for m in got if m.startswith("open")]
    assert "set_due p2" in got


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
    assert check(kind="open_link", part="p9")[0] is None               # no such part
    assert check(kind="set_due", part="p2")[0] == {"kind": "set_due", "params": {"part": "p2"}}
    assert check(kind="set_due", part="p2", date=day(20))[1] == "a date the records do not state"
    assert check(kind="set_due", part="p1")[0] is None                 # it has a date
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
    assert "Tacitus could not check this." in got["text"] and "Juniper" not in got["text"]
    assert [s["kind"] for s in got["steps"]] == ["draft", "open_link", "set_due"]
    assert got["rejected"][0]["why"].startswith("mark_delivered p1")


def test_the_letters_steps_in_its_kinds_words(env):
    cid = make_case(env, drafted=True)
    v = CB.view(one(env, cid), today=TODAY)
    steps = [{"id": "s1", "kind": "open_link", "params": {"part": "p1"}},
             {"id": "s2", "kind": "mark_delivered", "params": {"part": "p1"}},
             {"id": "s3", "kind": "ask_materials", "params": {}},
             {"id": "s4", "kind": "draft", "params": {}}]
    pub = {s["pub"]["id"]: s for s in CB.offered(steps, v, today=TODAY)}
    assert pub["s1"]["pub"] == {"id": "s1", "label": "Open the portal", "detail": "Harbor Tech PhD", "href": PORTAL}
    assert pub["s2"]["pub"]["label"] == "Mark it submitted"
    assert pub["s2"]["pub"]["confirm"] == "Mark Harbor Tech PhD as submitted? Only do this once it went in."
    assert CB.choice_of(pub["s2"], v)[0] == f"letter:submitted:{pub['s2']['part']['id']}"
    assert "It waits for your Send." in pub["s3"]["pub"]["confirm"] and "statement" in pub["s3"]["pub"]["detail"]
    assert CB.choice_of(pub["s3"], v) == ("letter:yes_ask", "")
    assert pub["s4"]["pub"]["label"] == "Change the letter"
    for s in pub.values():
        assert len(s["pub"]["label"]) <= CB.LABEL_MAX
        plain(s["pub"]["label"] + s["pub"].get("detail", "") + s["pub"].get("confirm", ""))


def test_a_part_with_no_name_is_numbered_and_the_ask_names_what_is_missing(env):
    cid = make_case(env)
    env.letters.add_destination(env.conn, cid, "", method="portal", link="https://apply.example.com/ref/zz",
                                conversation_id="conv-z", message_id="msg-z")
    set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale asked.",
                                    files=["Rowan_Vale_CV.pdf"]),
                                msg("m2", 70, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale sent it again.",
                                    files=["Rowan_Vale_CV.pdf"])])
    x = one(env, cid)
    v = CB.view(x, today=TODAY)
    assert v["parts"][-1]["name"] == "Destination 3"
    text = CB.card(x, None, now=NOW, since=None)["text"]
    assert "- Destination 3: no due date, to write, portal link" in text
    assert "- A CV (Rowan_Vale_CV.pdf)." in text                 # one file, named once
    [s] = CB.offered([{"id": "s1", "kind": "ask_materials", "params": {}}], v, today=TODAY)
    assert s["pub"]["detail"] == "a statement and the due date for 2 destinations"


# ── a form and a review: the same briefing from their declarations ────

def test_a_form_case_reads_in_its_own_words_with_case_choice_ids(env):
    cid, pid = make_work(env, "form", "Compliance Office", "compliance@example.edu", "Annual disclosure",
                         "Annual disclosure", due=day(5), link="https://forms.example.edu/annual/zz1",
                         subkind="disclosure")
    lid = env.cases.case(env.conn, cid)["parts"][0]["ledger_id"]
    set_brief(env, lid, [msg("f1", 30, "Compliance Office <compliance@example.edu>",
                             "The compliance office asks for the annual disclosure form.")])
    x = one(env, cid)
    v = CB.view(x, today=TODAY)
    text = CB.card(x, None, now=NOW, since=None)["text"]
    assert "- Disclosure form for Compliance Office, asked " in text
    assert "The forms\n- Annual disclosure: due " in text and ", to fill in, portal link" in text
    assert "Still missing\n- The answers." in text
    assert [CB._move_key(m) for m in CB.allowed_moves(v)] == ["draft", "open_link p1"]   # a form wants no materials
    pub = {s["kind"]: s for s in CB.offered([{"id": "s1", "kind": "draft", "params": {}},
                                             {"id": "s2", "kind": "open_link", "params": {"part": "p1"}}],
                                            v, today=TODAY)}
    assert pub["draft"]["pub"]["label"] == "Fill it in" and CB.choice_of(pub["draft"], v) == ("case:draft", "")
    assert pub["open_link"]["pub"]["label"] == "Open the link"
    prompt = CB.prepare(x, now=NOW)["prompt"]
    assert "a disclosure form he was asked for" in prompt and "the answers" in prompt and "letter" not in prompt
    plain(text)


def test_a_review_case_marks_its_report_returned(env):
    cid, pid = make_work(env, "review", "Pat Lee", "pat.lee@example.org", "Paper 12", "Referee report",
                         due=day(10), subkind="paper")
    env.cases.set_state(env.conn, pid, "drafted", "/docs/reviews/r.md", actor="owner")
    env.conn.commit()
    v = CB.view(one(env, cid), today=TODAY)
    assert "mark_delivered p1" in [CB._move_key(m) for m in CB.allowed_moves(v)]
    [s] = CB.offered([{"id": "s1", "kind": "mark_delivered", "params": {"part": "p1"}}], v, today=TODAY)
    assert s["pub"]["label"] == "Mark it returned"
    assert CB.choice_of(s, v) == (f"case:delivered:{pid}", "")
    text = CB.card(one(env, cid), None, now=NOW, since=None)["text"]
    assert "Paper review for Pat Lee" in text and "The reports\n- Referee report: due " in text


# ── the store and the owner's last look ──────────────────────────────

def test_the_store_is_owner_private_and_keeps_one_briefing_per_case(env):
    st = env.store
    st.put("lc-1", {"version": "v1", "by": "local", "at": 1.0, "level": "full", "text": "T", "steps": [{"id": "s1"}]})
    assert st.get("lc-1")["steps"] == [{"id": "s1"}]
    assert st.versions() == {"lc-1": ("v1", "full", "local")}
    assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o600
    st.put("lc-2", {"version": "v2", "text": "U"})
    assert st.drop({"lc-2"}) == 1 and st.get("lc-1") is None and st.get("lc-2")["text"] == "U"
    with sqlite3.connect(st.path) as c:
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master")} >= {"briefings", "case_briefings"}


def test_a_look_starts_a_visit_and_since_counts_from_the_visit_before(env):
    st = env.store
    assert st.look("lc-1", 1000.0) is None
    assert st.look("lc-1", 1000.0 + 60) is None
    assert st.look("lc-1", 1060.0 + CB.VISIT_S + 1) == 1060.0
    assert st.since("lc-1") == 1060.0
    assert st.look("lc-1", 1060.0 + CB.VISIT_S + 30) == 1060.0


def test_opening_the_block_in_action_requests_is_a_look(env):
    import web_requests as wr
    cid = make_case(env)
    card = {"kind": "letter", "actions": [{"id": "letter_draft", "value": json.dumps({"case": cid, "rows": []})}]}
    assert wr.looked([{"kind": "send", "actions": []}]) == 0       # a card with no case: nothing
    wr.context([{"id": "m1", "card": card}])                          # reading the list is no look
    assert not env.store.path.exists() or env.store.since(cid) is None
    assert wr.looked([card]) == 1
    with sqlite3.connect(env.store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM case_seen WHERE case_id = ?", (cid,)).fetchone()[0] == 1


def test_the_web_chats_read_of_a_thread_tells_the_host():
    sys.path.insert(0, str(REPO / "botcore"))
    import inspect
    from botcore.chat import web_frontend as wf
    src = inspect.getsource(wf)
    route = src[src.index('segs[4] == "read"'):]
    route = route[:route.index("return None")]
    assert "fe.thread_opened(segs[1], key)" in route and "except Exception" in route
    assert "self.thread_opened" in inspect.getsource(wf.WebFrontend.__init__)


# ── what changed since he last looked; the dates a migration wrote ────

def test_a_new_message_from_them_shows_with_its_gist(env):
    cid = make_case(env)
    set_brief(env, "task-own", [msg("m1", 72, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale asked for a letter."),
                                msg("m2", 2, "Rowan Vale <rowan.vale@example.org>",
                                    "Rowan Vale sent a new deadline for Pinecrest College MS.")])
    v = CB.view(one(env, cid), today=TODAY)
    lines = CB.since_lines(v, NOW - 24 * 3600, now=NOW)
    assert lines[0].startswith("A new message from Rowan Vale on ")
    assert lines[0].endswith(": Rowan Vale sent a new deadline for Pinecrest College MS.")
    assert QUOTE not in " ".join(lines)
    assert CB.since_lines(v, None, now=NOW) == []


def test_a_message_the_store_recorded_shows_when_no_row_holds_it(env):
    cid = make_case(env)
    env.cases._event(env.conn, cid, "message", actor="intake",
                     evidence={"at": iso(1), "from": "rowan.vale@example.org", "name": "Rowan Vale",
                               "conversation_id": "conv-new", "message_id": "m-new", "part": ""})
    env.conn.commit()
    lines = CB.since_lines(CB.view(one(env, cid), today=TODAY), NOW - 3 * 3600, now=NOW)
    assert lines[0].startswith("A new message from Rowan Vale on ") and lines[0].endswith(".")


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
    p1, p2 = [d["id"] for d in one(env, cid)["case"]["parts"]]
    since = time.time() - 1
    L.set_due(env.conn, p1, day(12))
    L.add_material(env.conn, cid, "statement", "msg-own#statement.pdf")
    L.set_state(env.conn, p2, "submitted", "the owner said so")
    env.conn.commit()
    lines = CB.since_lines(CB.view(one(env, cid), today=TODAY), since, now=time.time() + 1)
    assert any(ln.startswith("Harbor Tech PhD: due date moved from ") for ln in lines)
    assert "A statement came in." in lines and "Pinecrest College MS: marked submitted." in lines


def carried_over(env, cid, *, at: str):
    """The case as the first letter release's migration wrote it: its open,
    its destinations and its decision, all at the migration's time."""
    env.conn.execute("UPDATE cases SET opened = ? WHERE id = ?", (at, cid))
    env.conn.execute("UPDATE case_events SET ts = ?, actor = 'cli:owner' WHERE case_id = ?", (at, cid))
    env.conn.execute("UPDATE case_events SET evidence = ? WHERE case_id = ? AND kind = 'accept'",
                     (json.dumps({"quote": CB.CARRIED_QUOTE}), cid))
    env.conn.commit()


def test_the_migrations_day_never_reaches_the_card_or_the_model(env):
    cid = make_case(env, person_wrote=False)
    then = TODAY - timedelta(days=1)
    migrated = then.isoformat() + "T22:34:43Z"
    carried_over(env, cid, at=migrated)
    x = one(env, cid)
    v = CB.view(x, today=TODAY)
    text = CB.card(x, None, now=NOW, since=None)["text"]
    assert "You said yes." in text and f"{then:%b} {then.day}" not in text and ", asked " not in text
    packet = json.dumps(CB.prepare(x, now=NOW)["packet"])
    assert "answered_on" not in packet and "first_mail_on" not in packet
    assert migrated[:10] not in packet and f"{then:%B} {then.day}" not in packet
    assert CB.since_lines(v, NOW - 10 * 86400, now=NOW) == []      # nor as news


def test_the_owners_own_decision_gives_its_day_and_the_first_mail_the_ask(env):
    cid = make_case(env)
    x = one(env, cid)
    text = CB.card(x, None, now=NOW, since=None)["text"]
    asked = datetime.fromtimestamp(NOW - 72 * 3600, ZONE).date()      # his day, not the store's UTC one
    decided = datetime.now(ZONE).date()
    assert f"Recommendation letter for Rowan Vale, asked {asked:%a}, {asked:%b} {asked.day}." in text
    assert f"You said yes on {decided:%a}, {decided:%b} {decided.day}." in text
    assert CB.prepare(x, now=NOW)["packet"]["case"]["answered_on"].startswith(decided.isoformat())


# ── the card's code-built lines ───────────────────────────────────────

def test_the_card_lists_the_parts_what_came_in_and_what_is_missing(env):
    cid = make_case(env)
    got = CB.card(one(env, cid), None, now=NOW, since=None)
    text = got["text"]
    assert "Now\n- Next due " in text and "in 9 days: Harbor Tech PhD." in text
    assert "- Rowan Vale asked you directly." in text
    assert "The destinations\n- Harbor Tech PhD: due " in text and ", to write, portal link" in text
    assert "- Pinecrest College MS: no due date, to write, by email" in text
    assert "What came in\n- A CV (Rowan_Vale_CV.pdf)." in text
    assert "Still missing\n- The letter.\n- A statement.\n- The due date for 1 destination." in text
    assert got["steps"] == [] and PORTAL not in text
    plain(text)
    requests = CB.card(one(env, cid), None, now=NOW, since=None, places=False)["text"]
    assert "The destinations" not in requests and "Still missing" not in requests and "What was asked" in requests


def test_a_local_case_names_no_one_and_no_part(env):
    cid = make_case(env, level="local")
    text = CB.card(one(env, cid), None, now=NOW, since=None)["text"]
    assert "Rowan" not in text and "Harbor" not in text and "Destination 1: due " in text
    assert "A CV" in text and "pdf" not in text


# ── refresh on each kind of change ────────────────────────────────────

def test_refresh_writes_once_and_again_on_each_kind_of_change(env):
    L = env.letters
    cid = make_case(env)
    _fid, pid2 = make_work(env, "form", "Compliance Office", "c@example.edu", "Annual disclosure",
                           "Annual disclosure", due=day(5))
    got = CB.refresh(store=env.store, daily=None)
    assert got["written"] == 2 and len(env.calls) == 2           # a letter and a form alike
    assert CB.refresh(store=env.store, daily=None)["stale"] == 0
    p1, p2 = [d["id"] for d in one(env, cid)["case"]["parts"]]

    def changed(why):
        n = len(env.calls)
        got = CB.refresh(store=env.store, daily=None)
        assert got["written"] == 1 and len(env.calls) == n + 1, why
    set_brief(env, "task-own", [msg("m9", 1, "Rowan Vale <rowan.vale@example.org>", "Rowan Vale wrote again.")])
    changed("a new message")
    env.cases._event(env.conn, cid, "mail", part_id=p1, evidence={"message": "msg-r", "role": "reminder"})
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
    env.cases.set_due(env.conn, pid2, day(6))
    env.conn.commit()
    changed("a form's date")


def test_refresh_keeps_a_kept_case_on_the_owners_own_model(env, monkeypatch):
    make_case(env, level="local")
    hosted = []
    monkeypatch.setattr(CB, "LOCAL", lambda p: {"ok": False, "text": "", "usage": {}, "reason": "timeout"})
    monkeypatch.setattr(CB, "HOSTED", lambda p: hosted.append(p) or {"ok": True, "text": json.dumps(ANSWER)})
    got = CB.refresh(store=env.store, daily=None)
    assert got["failed"] == 1 and hosted == []


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


def test_a_case_call_leaves_the_row_briefings_calls_alone(env):
    before = (D.LOCAL, D.HOSTED)
    got = CB.run_model("the records", ["local"])
    assert got["ok"] and (D.LOCAL, D.HOSTED) == before and env.calls == ["the records"]


# ── a store that waits for its owner's migration ──────────────────────

def test_a_store_waiting_for_its_migration_reads_and_skips(env, hermetic, monkeypatch):
    """The first release's letter tables still hold rows: the briefing reads
    them through the store's temporary views, writes only its own store,
    and nothing crashes."""
    import ledger
    from test_cases import _legacy_db
    db = hermetic / "legacy.db"
    _legacy_db(db).close()
    monkeypatch.setattr(ledger, "DB_PATH", db)
    assert [x["case"]["id"] for x in CB.load_all()] == ["lc-old1"]
    got = CB.refresh(store=env.store, daily=None)
    assert got["written"] == 1 and not got["skipped"]
    assert CB.hosted_pass(store=env.store, call=lambda p: {"ok": False, "reason": "error"}, now=NOW)["stored"] == 0
    with sqlite3.connect(str(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM letter_events").fetchone()[0] == 3     # nothing written there
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"letter_cases"}


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
    for d in one(env, cid)["case"]["parts"]:
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


def home(env):
    import home_fixtures as F
    import home_screen as H
    src = F.sources()
    src.cases = None                       # Home reads the store itself
    fe = FE()
    h = H.Home(fe, clock=lambda: F.at(13, 10), sources=lambda tz: src, background=False)
    pressed = []
    h.hooks.letter = lambda case_id, answer, dest, words: pressed.append(("letter", case_id, answer, dest, words)) or \
        {"ok": True, "said": "Done on the card."}
    h.hooks.case = lambda case_id, answer, part, words: pressed.append(("case", case_id, answer, part, words)) or \
        {"ok": True, "said": "Done on the card."}
    return h, fe, pressed


def test_the_home_item_shows_the_stored_briefing_and_its_steps(env):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    h, fe, _pressed = home(env)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "ready" and not d.get("stale") and d["by"] == "local"
    assert d["text"].startswith("Now\n") and "What happened\n- Rowan Vale asked" in d["text"]
    assert [s["label"] for s in d["steps"]] == ["Draft the letter", "Open the portal", "Set the due date",
                                                "Continue in a conversation"]
    assert d["steps"][1]["href"] == PORTAL and d["steps"][2]["input"] == "date"
    plain(d["text"])


def test_a_step_runs_the_case_cards_own_answer(env):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    h, fe, pressed = home(env)
    p2 = one(env, cid)["case"]["parts"][1]["id"]
    ref = f"letter:{cid}"
    assert h.act(ref, "step:s1")[0]["ok"] and pressed[-1] == ("letter", cid, "draft", "", "")
    assert h.act(ref, "step:s3")[0] == {"ok": False, "said": "Pick the date first."} and len(pressed) == 1
    assert h.act(ref, "step:s3", day(15))[0]["ok"] and pressed[-1] == ("letter", cid, "date", p2, day(15))
    assert h.act(ref, "step:s2")[0] == {"ok": True, "said": "Opening its link.", "goto": PORTAL}
    assert len(pressed) == 2


def test_a_form_items_briefing_and_step_on_home(env):
    """A `case` item gets the briefing a letter gets, and its step presses
    Hooks.case with the case card's answer."""
    cid, _pid = make_work(env, "form", "Compliance Office", "compliance@example.edu", "Annual disclosure",
                          "Annual disclosure", due=day(4), link="https://forms.example.edu/annual/zz1")
    env.store.put(cid, {"version": one(env, cid)["version"], "level": "full", "by": "local", "at": NOW,
                        "text": "What happened\n- The compliance office asked for it.",
                        "steps": [{"id": "s1", "kind": "draft", "params": {}},
                                  {"id": "s2", "kind": "open_link", "params": {"part": "p1"}}]})
    h, fe, pressed = home(env)
    it = h.item(f"case:{cid}")
    assert it["kind"] == "case" and it["deeper"]["state"] == "ready"
    assert "The forms\n- Annual disclosure: due " in it["deeper"]["text"]
    assert [s["label"] for s in it["deeper"]["steps"]] == ["Fill it in", "Open the link", "Continue in a conversation"]
    assert h.act(f"case:{cid}", "step:s1")[0]["ok"] and pressed[-1] == ("case", cid, "draft", "", "")


def test_a_step_home_does_not_list_runs_as_its_card_button(env):
    """Ask for what is missing is the card's Yes, ask for materials (not one
    of Home's choices for an accepted case)."""
    cid = make_case(env)
    env.store.put(cid, {"version": one(env, cid)["version"], "level": "full", "by": "local", "at": NOW,
                        "text": "What happened\n- Rowan Vale asked for it.",
                        "steps": [{"id": "s1", "kind": "ask_materials", "params": {}}]})
    h, fe, pressed = home(env)
    assert "letter:yes_ask" not in [c["id"] for c in h.item(f"letter:{cid}")["choices"]]
    assert h.act(f"letter:{cid}", "step:s1")[0]["ok"] and pressed[-1][2] == "yes_ask"
    assert h.act(f"letter:{cid}", "letter:yes_ask")[0]["ok"] is False    # posted without a step: refused


def test_a_press_while_the_store_waits_for_its_migration_says_so_plainly(env):
    import home_items as I
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    h, fe, pressed = home(env)

    def waiting(*a):
        raise env.cases.MigrationPending(env.cases.MIGRATE_LINE)
    h.hooks.letter = waiting
    assert h.act(f"letter:{cid}", "letter:draft", "keep it short")[0] == {"ok": False, "said": I.MIGRATION_SAID}
    assert h.act(f"letter:{cid}", "step:s1")[0] == {"ok": False, "said": I.MIGRATION_SAID}


def test_any_other_fault_in_the_hook_is_not_taken_for_a_waiting_store(env):
    cid = make_case(env)
    h, fe, pressed = home(env)

    def broken(*a):
        raise KeyError("a bug")
    h.hooks.letter = broken
    with pytest.raises(KeyError):
        h.act(f"letter:{cid}", "letter:draft", "keep it short")


def test_a_fault_in_the_briefing_is_logged_with_its_trace_and_shown(env, monkeypatch, caplog):
    """A wrong name inside the hook (a merge's seam) is a logged error and a
    failed block, never an `unavailable` that hides it."""
    import home_items as I
    cid = make_case(env)
    h, fe, pressed = home(env)

    def wrong(*a, **k):
        return undefined_name  # noqa: F821
    monkeypatch.setattr(CB, "state", wrong)
    with caplog.at_level(logging.ERROR, logger="chat.web.home.items"):
        d = h.item(f"letter:{cid}")["deeper"]
    assert d == {"state": "failed", "note": I.CASE_FAILED}
    rec = next(r for r in caplog.records if "case briefing's state failed" in r.getMessage())
    assert rec.levelno == logging.ERROR and rec.exc_info and rec.exc_info[0] is NameError


def test_a_briefing_older_than_the_case_is_shown_stale_and_its_steps_refused(env):
    cid = make_case(env)
    CB.refresh(store=env.store, daily=None)
    env.letters.set_due(env.conn, one(env, cid)["case"]["parts"][1]["id"], day(30))
    env.conn.commit()
    h, fe, pressed = home(env)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "ready" and d["stale"] is True
    assert h.act(f"letter:{cid}", "step:s1")[0]["ok"] is False and pressed == []
    assert h.deeper(f"letter:{cid}")["state"] == "ready"
    assert not h.item(f"letter:{cid}")["deeper"].get("stale")


def test_with_nothing_stored_the_card_shows_the_facts_and_asks_for_the_rest(env):
    cid = make_case(env)
    h, fe, _ = home(env)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "none" and "The destinations" in d["text"] and d["steps"] == []
    assert h.deeper(f"letter:{cid}") == {"ok": True, "state": "ready"} and len(env.calls) == 1
    assert fe.emitted == [f"letter:{cid}", f"letter:{cid}"]


def test_a_kept_case_with_no_local_model_shows_its_facts_and_says_so(env, monkeypatch):
    cid = make_case(env, level="local")
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: False)
    h, fe, _ = home(env)
    d = h.item(f"letter:{cid}")["deeper"]
    assert d["state"] == "ready" and d["note"] == D.NOTE_KEPT_NO_LOCAL and "Rowan" not in d["text"]
    assert h.deeper(f"letter:{cid}")["state"] == "unavailable" and env.calls == []


def test_a_kept_case_offers_no_conversation(env):
    cid = make_case(env, level="facts")
    CB.refresh(store=env.store, daily=None)
    h, fe, _ = home(env)
    assert D.CONTINUE not in [s["id"] for s in h.item(f"letter:{cid}")["deeper"]["steps"]]


def test_opening_the_item_is_the_owners_look(env):
    cid = make_case(env)
    h, fe, _ = home(env)
    h.item(f"letter:{cid}")
    assert env.store.since(cid) is None
    with sqlite3.connect(env.store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM case_seen WHERE case_id = ?", (cid,)).fetchone()[0] == 1


# ── Action requests ───────────────────────────────────────────────────

def test_a_case_cards_block_carries_its_briefing(env):
    import web_requests as wr
    cid = make_case(env)
    fid, _pid = make_work(env, "form", "Compliance Office", "c@example.edu", "Annual disclosure", "Annual disclosure",
                          due=day(5))
    CB.refresh(store=env.store, daily=None)
    cards = [{"id": "m1", "card": {"kind": "letter", "actions": [
        {"id": "letter_draft", "value": json.dumps({"case": cid, "rows": []})}]}},
             {"id": "m2", "card": {"kind": "case", "actions": [
                 {"id": "case_draft", "value": json.dumps({"case": fid, "rows": []})}]}}]
    got = wr.context(cards)
    assert "What happened" in got["m1"]["briefing"]["text"] and "The destinations" not in got["m1"]["briefing"]["text"]
    assert "What happened" in got["m2"]["briefing"]["text"] and "The forms" not in got["m2"]["briefing"]["text"]


def test_the_web_chat_puts_the_briefing_on_the_block():
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
