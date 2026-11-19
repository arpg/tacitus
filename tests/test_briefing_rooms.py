"""briefing_rooms: which project an item belongs to, which rooms may get
a post, and the post each room gets; and remote_briefing's room step.
All names and mail here are made up; nothing reaches the network."""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import briefing_rooms as br  # noqa: E402
import chat_notify  # noqa: E402
import remote_briefing as rb  # noqa: E402

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    """No test reads the owner's tagger log."""
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    return d


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE projects (id TEXT, name TEXT, status TEXT);
        CREATE TABLE project_ledger (project_id TEXT, ledger_id TEXT);
        CREATE TABLE project_messages (project_id TEXT, conversation_id TEXT);
    """)
    c.executemany("INSERT INTO projects VALUES (?,?,?)",
                  [("kestrel", "Kestrel radar", "active"), ("lumen", "Lumen survey", "active"),
                   ("old", "Old grant", "closed"), ("nohome", "No room", "active")])
    c.execute("INSERT INTO project_ledger VALUES ('kestrel', 'row-k')")
    c.execute("INSERT INTO project_ledger VALUES ('old', 'row-old')")
    c.execute("INSERT INTO project_messages VALUES ('lumen', 'conv-l')")
    return c


def _matcher(scores):
    return lambda text, conn: [{"id": k, "score": v} for k, v in scores.items() if k in text.lower()] \
        or []


TARGETS = {"kestrel": {"channel": "r7", "room": "ea-kestrel"},
           "lumen": {"channel": "r8", "room": "ea-lumen"}}


def test_a_link_beats_a_filed_thread_beats_the_matcher(conn):
    idx = br.index(conn)
    m = _matcher({"lumen": 3})
    assert br.projects_for({"ledger_id": "row-k", "conversation_id": "conv-l", "title": "lumen"},
                           idx, conn, matcher=m) == ["kestrel"]
    assert br.projects_for({"ledger_id": "row-x", "conversation_id": "conv-l", "title": ""},
                           idx, conn, matcher=m) == ["lumen"]
    assert br.projects_for({"title": "the lumen survey"}, idx, conn, matcher=m) == ["lumen"]
    # a closed project's link is no link; a weak match is no project
    assert br.projects_for({"ledger_id": "row-old", "title": "x"}, idx, conn, matcher=m) == []
    weak = lambda text, conn: [{"id": "lumen", "score": 2}]
    assert br.projects_for({"title": "lumen"}, idx, conn, matcher=weak) == []
    tied = lambda text, conn: [{"id": "kestrel", "score": 3}, {"id": "lumen", "score": 3},
                               {"id": "nohome", "score": 3}]
    assert br.projects_for({"title": "x"}, idx, conn, matcher=tied) == ["kestrel", "lumen"]


def test_the_taggers_call_places_an_item_when_its_words_echo_it(conn):
    conn.execute("CREATE TABLE ledger (id TEXT, source_message_id TEXT)")
    conn.executemany("INSERT INTO ledger VALUES (?,?)",
                     [("row-a", "msg-a"), ("row-b", "msg-b"), ("row-c", "msg-c"), ("row-d", "")])
    votes = {"messages": {"msg-a": "lumen", "msg-b": "lumen", "msg-c": ""},
             "conversations": {"conv-d": "kestrel"}}
    idx = br.index(conn, votes)
    trace = lambda text, conn: [{"id": "lumen", "score": 1}, {"id": "kestrel", "score": 3}]
    silent = lambda text, conn: [{"id": "kestrel", "score": 3}]
    # the call wins over a stronger word match when the words echo it
    assert br.projects_for({"ledger_id": "row-a", "title": "x"}, idx, conn, matcher=trace) == ["lumen"]
    # a call the words do not echo places nothing, and neither do the words
    assert br.projects_for({"ledger_id": "row-b", "title": "x"}, idx, conn, matcher=silent) == []
    # the tagger named no project: the words alone place nothing
    assert br.projects_for({"ledger_id": "row-c", "title": "x"}, idx, conn, matcher=silent) == []
    # a call found by conversation
    assert br.projects_for({"ledger_id": "row-d", "conversation_id": "conv-d", "title": "x"},
                           idx, conn, matcher=trace) == ["kestrel"]
    # no call (a Gmail row, a row from chat): the word match alone
    assert br.projects_for({"ledger_id": "row-x", "title": "x"}, idx, conn, matcher=silent) == ["kestrel"]


def test_only_rooms_the_owner_record_names_get_a_post():
    registry = {"r7": {"kind": "project", "project": "kestrel", "name": "ea-kestrel"},
                "r8": {"kind": "project", "project": "lumen", "name": "ea-lumen"},
                "r9": {"kind": "project", "project": "arch", "name": "ea-arch", "archived": True},
                "r10": {"kind": "session", "session_id": "x", "name": "tmp-1"},
                "r11": {"kind": "project", "project": "swap", "name": "ea-kestrel"}}
    owner = {"r7": {"name": "ea-kestrel"}, "r9": {"name": "ea-arch"},
             "r10": {"name": "tmp-1"}, "r11": {"name": "ea-other"}}
    assert br.rooms(registry=registry, owner=owner) == {
        "kestrel": {"channel": "r7", "room": "ea-kestrel"}}
    owner["r7"]["archived_by_bot"] = True
    assert br.rooms(registry=registry, owner=owner) == {}


class _FE:
    """A stand-in frontend: records posts, links rooms Slack style."""
    def __init__(self):
        self.posts = []

    def post(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))
        return type("Ref", (), {"thread_key": f"{channel}:t1"})()

    def mention(self, channel):
        return f"<#{channel}>"


@pytest.mark.parametrize("frontend", ["slack", "web"])
def test_a_room_key_posts_through_the_frontend_to_owner_rooms_only(monkeypatch, frontend):
    import owner_rooms
    monkeypatch.setattr(owner_rooms, "get", lambda cid: {"name": "ea-kestrel"} if cid == "C7" else None)
    fe = _FE()
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [fe])
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: frontend)
    monkeypatch.setattr(chat_notify, "_send_slack", lambda *a: pytest.fail("webhook path"))
    monkeypatch.setattr(chat_notify, "_send_web", lambda *a, **k: pytest.fail("notify path"))
    text = f"part one{chat_notify.SPLIT_MARKER}part two"
    assert chat_notify.post(chat_notify.room_key("C7"), text, _bypass_quiet=True)
    assert fe.posts == [("C7", "part one", None), ("C7", "part two", "C7:t1")]
    assert not chat_notify.post(chat_notify.room_key("C99"), "x", _bypass_quiet=True)
    assert len(fe.posts) == 2
    assert chat_notify.room_mention("C7") == "<#C7>"


def test_quiet_hours_hold_a_room_post(monkeypatch):
    import quiet_hours
    held = []
    monkeypatch.setattr(quiet_hours, "should_defer", lambda priority: True)
    monkeypatch.setattr(quiet_hours, "defer", lambda key, text, priority: held.append(key))
    monkeypatch.setattr(chat_notify, "_send_room", lambda *a: pytest.fail("posted in quiet hours"))
    assert chat_notify.post(chat_notify.room_key("C7"), "x")
    assert held == ["room:C7"]


def test_each_room_gets_one_post_with_its_items_and_its_other_open_rows(conn):
    entries = [
        {"ref": "L1", "section": "top", "line": "*Radar order* (Pat): first thing", "ledger_id": "row-k",
         "conversation_id": "", "title": "Radar order", "summary": ""},
        {"ref": "R1", "section": "needs", "line": "*Survey dates* (Lee): pick one", "ledger_id": "",
         "conversation_id": "conv-l", "title": "Survey dates", "summary": ""},
        {"ref": "R2", "section": "flex", "line": "*Lunch* (Sam): fine", "ledger_id": "",
         "conversation_id": "", "title": "Lunch", "summary": ""},
    ]
    open_rows = [{"id": "row-k", "description": "Radar order"},
                 {"id": "row-k2", "description": "Kestrel field test plan", "notes": ""}]
    posts = br.plan_posts(entries, heading="Morning briefing, Mon Oct 12", open_rows=open_rows,
                          conn=conn, targets=TARGETS, clean=rb._clean, matcher=_matcher({"kestrel": 3}))
    assert [(p["room"], p["refs"]) for p in posts] == [("ea-kestrel", ["L1"]), ("ea-lumen", ["R1"])]
    k = posts[0]["text"]
    assert k.startswith("*Radar order*\n")
    assert k.endswith("_From the morning briefing, Mon Oct 12. Reply in this thread to act on it here._")
    assert ":dart: Do today: *Radar order* (Pat): first thing" in k
    assert "Also open here (1): Kestrel field test plan" in k and "Lunch" not in k
    assert "—" not in k
    assert br.pointer_line(posts, mention=lambda cid: f"<#{cid}>") == [
        "", ":house: Also in project rooms: <#r7> (1), <#r8> (1)"]
    sent = br.send(posts, poster=lambda key, text: key == "room:r7")
    assert sent == [{"room": "ea-kestrel", "ok": True}, {"room": "ea-lumen", "ok": False}]


def test_a_kept_row_in_also_open_is_named_in_fixed_words(conn):
    """Room sessions read the slice: a row kept from hosted models is
    named in code's fixed words, by its stamp or its keep label."""
    entries = [{"ref": "L1", "section": "top", "line": "*Radar order* (Pat): first thing",
                "ledger_id": "row-k", "conversation_id": "", "title": "Radar order", "summary": ""}]
    open_rows = [{"id": "row-k", "description": "Radar order"},
                 {"id": "kept-k2", "description": "Kestrel export licence", "keep_level": "local"},
                 {"id": "kept-k3", "description": "Kestrel badge form", "kept_from_frontier": "listed domain"},
                 {"id": "row-k4", "description": "Kestrel parts list", "keep_level": "summary"}]
    posts = br.plan_posts(entries, heading="Morning briefing, Mon Oct 12", open_rows=open_rows,
                          conn=conn, targets=TARGETS, clean=rb._clean, matcher=_matcher({"kestrel": 3}))
    k = posts[0]["text"]
    assert "Also open here (3): A kept message; A kept message; Kestrel parts list" in k
    assert "export licence" not in k and "badge form" not in k


def test_the_morning_run_posts_slices_and_names_the_rooms(monkeypatch, conn, tmp_path):
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setattr(br, "enabled", lambda cfg=None: True)
    b = {"open_obligations": [{"id": "row-k", "description": "Radar order", "type": "task",
                               "from_email": "pat@example.edu"}],
         "today_top": [], "delta_summary": {}}
    cands = rb.candidates(b, today=NOW.date())
    ref = next(iter(cands))
    plan = {"get_done_today": [{"ref": ref, "when": "first thing", "why": "the quote expires"}],
            "flexes": [], "needs_you": []}
    got = []
    kw = {"conn": conn, "targets": TARGETS, "open_rows": [], "matcher": _matcher({})}
    posts = rb.room_slices("morning-briefing", cands, plan, [], [], {}, b, now=NOW, dry_run=False,
                           poster=lambda key, text: got.append((key, text)) or True, **kw)
    assert [k for k, _t in got] == ["room:r7"] and posts[0]["ok"] is True
    assert "the quote expires" in got[0][1]
    # a row the audit closed is in no slice
    assert rb.room_slices("morning-briefing", cands, plan, [], [], {"closed": ["row-k"]}, b, now=NOW,
                          dry_run=False, poster=lambda *a: pytest.fail("posted"), **kw) == []
    # a dry run plans and sends nothing
    dry = rb.room_slices("morning-briefing", cands, plan, [], [], {}, b, now=NOW, dry_run=True,
                         poster=lambda *a: pytest.fail("posted"), **kw)
    assert dry and "ok" not in dry[0]


# ── the post's name (the Recent row and the room's thread list) ────────

def _entry(section, title, line=""):
    return {"ref": title[:4], "section": section, "line": line or f"*{title}*", "ledger_id": "",
            "conversation_id": "", "title": title, "summary": ""}


def test_a_slice_is_named_by_its_items_in_plain_words():
    """A post opens with what it is about, never "Morning briefing, <date>:
    <project>", which made one row per room alike in the web chat's Recent
    with the project cut off."""
    one = [_entry("flex", "Re: FW: Kestrel calendar changes")]
    assert br.conversation_name(one, rb._clean) == "Kestrel calendar changes"
    # several: the first the post shows, by section, then how many more
    several = [_entry("flex", "Lunch order"), _entry("due", "Task: Survey consent form"),
               _entry("top", "Due 2026-10-12: Radar quote :memo:")]
    assert br.conversation_name(several, rb._clean) == "Radar quote and 2 more"
    text = br.render("Afternoon update, Mon Oct 12", several, [], rb._clean)
    lines = text.splitlines()
    assert lines[0] == "*Radar quote and 2 more*"
    assert lines[1].startswith(":dart: Do today:") and lines[-1] == (
        "_From the afternoon update, Mon Oct 12. Reply in this thread to act on it here._")
    assert "Morning briefing" not in text and "Afternoon update" not in text and "\u2014" not in text


def test_a_long_title_is_cut_at_a_word():
    long = "[EXT] Apply the staged Kestrel Monday rotation and the prerequisite calendar changes for the winter"
    name = br.conversation_name([_entry("flex", long)], rb._clean)
    # whole words only, and no small word ("and the") left at the cut
    assert name == "Apply the staged Kestrel Monday rotation…"
    more = br.conversation_name([_entry("flex", long)] + [_entry("due", "x")] * 11, rb._clean)
    assert more == "Apply the staged Kestrel Monday rotation… and 11 more"
    assert len(more) <= br.NAME_MAX
    one_word = br.conversation_name([_entry("top", "K" * 90)], rb._clean)
    assert len(one_word) == br.NAME_MAX and one_word.endswith("…")
    # no title: the item's line in plain words
    assert br.conversation_name([_entry("top", "", "*Radar quote* (Pat): first thing")],
                                rb._clean) == "Radar quote (Pat): first thing"


@pytest.mark.parametrize("level", ["local", "facts"])
def test_a_kept_item_names_its_slice_in_fixed_words(level):
    """A first item kept from hosted models gives the post code's fixed
    words, never its subject or its sender; a later kept item adds only
    to the count, and an item a hosted model may read is named as usual."""
    kept = {**_entry("top", "Re: Tenure case letter for Dr. Example"), "keep_level": level,
            "line": "A kept message from Pat Example on Mon Oct 12 needs you: Tenure case letter."}
    name = br.conversation_name([kept, _entry("flex", "Lunch order")], rb._clean)
    assert name == "A kept message and 1 more"
    assert br.render("Morning briefing, Mon Oct 12", [kept], [], rb._clean).splitlines()[0] == (
        "*A kept message*")
    # no title: still never the kept line's words
    assert br.conversation_name([{**kept, "title": ""}], rb._clean) == "A kept message"
    later = br.conversation_name([_entry("top", "Radar quote"), {**kept, "section": "flex"}], rb._clean)
    assert later == "Radar quote and 1 more" and "Tenure" not in later and "Pat" not in later
    for open_level in ("summary", "full", ""):
        assert br.conversation_name([{**kept, "keep_level": open_level}], rb._clean) == (
            "Tenure case letter for Dr. Example")


def test_the_run_hands_each_items_keep_level_to_its_room():
    cands = {"R1": {"title": "Re: Tenure case letter", "keep_level": "facts", "ledger_id": "row-k"}}
    got = rb.room_entries(cands, {"needs": [("R1", "A kept message needs you.")]})
    assert got[0]["keep_level"] == "facts"
    assert br.conversation_name(got, rb._clean) == "A kept message"


def test_an_unanswered_slice_stays_out_of_recent_and_needs_you(tmp_path, monkeypatch):
    """A slice the owner has not answered is in neither Recent, Needs you
    nor Home's "since you last checked in", in its new form or its old;
    once the owner writes in it, it is listed, titled by its name."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_web_frontend import make, owner
    import json
    import owner_rooms
    from chat import thread_state
    fe = make(tmp_path)
    fe.start()
    try:
        fe.create_private_room("ea-kestrel")
        fe.create_private_room("ea-lumen")
        ids = {c["name"]: c["id"] for c in fe.store.channels()}
        monkeypatch.setattr(owner_rooms, "get", lambda cid: {"name": "ea-x"} if cid in ids.values() else None)
        monkeypatch.setattr(chat_notify, "_ROOM_FE", [fe])
        monkeypatch.setattr(chat_notify, "frontend_name", lambda: "web")
        heading = "Morning briefing, Mon Oct 12"
        k_text = br.render(heading, [_entry("top", "Radar quote"), _entry("flex", "Lunch order")], [],
                           rb._clean)
        l_text = br.render(heading, [_entry("needs", "Re: Survey dates")], [], rb._clean)
        k = chat_notify.post(chat_notify.room_key(ids["ea-kestrel"]), k_text, _bypass_quiet=True)
        lum = chat_notify.post(chat_notify.room_key(ids["ea-lumen"]), l_text, _bypass_quiet=True)
        assert fe.store.thread_origin(ids["ea-kestrel"], k) == f"room:{ids['ea-kestrel']}"
        # a slice in the old form, which opens with the briefing's title
        chat_notify.post(chat_notify.room_key(ids["ea-kestrel"]),
                         "*Morning briefing, Sun Oct 11: Kestrel radar*\n:dart: Do today: *Radar quote*",
                         _bypass_quiet=True)

        def recent():
            status, _h, body = owner(fe, "GET", "/api/recent")
            assert status == 200, body
            return [(t["channel"], t["thread_key"]) for t in json.loads(body)["threads"]]

        assert recent() == []
        # Home's "since you last checked in" counts Recent's threads with an
        # unread answer: an unanswered slice is none of them
        import time
        from zoneinfo import ZoneInfo
        import home_screen

        def since():
            src = home_screen.Sources(now=time.time() + 120, zone=ZoneInfo("UTC"),
                                      checkin={"prev": time.time() - 3600},
                                      recent=fe.store.recent(20)["threads"])
            return home_screen.since(src, [], [])

        assert since() is None
        # the rules give an unanswered slice no state, so Needs you leaves it out
        first = fe.store.thread(ids["ea-kestrel"], k)
        state = thread_state.decide(first, origin=f"room:{ids['ea-kestrel']}")
        assert state[0] == "none"
        fe.store.add_message(ids["ea-lumen"], mid=None, thread_key=lum, author="owner", kind="message",
                             text="Pick the first date.")
        assert recent() == [(ids["ea-lumen"], lum)]
        status, _h, body = owner(fe, "GET", "/api/recent")
        row = json.loads(body)["threads"][0]
        assert row["text"].splitlines()[0] == "**Survey dates**"  # the app's title: the first line
        fe.store.add_message(ids["ea-lumen"], mid=None, thread_key=lum, author="bot", kind="message",
                             text="Done: the first date.")
        assert [x["text"] for x in since()["lines"]] == ["1 conversation has a new answer."]
    finally:
        fe.stop()
