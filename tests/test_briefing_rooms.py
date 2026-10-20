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


TARGETS = {"kestrel": {"channel": "stream:7", "room": "ea-kestrel"},
           "lumen": {"channel": "stream:8", "room": "ea-lumen"}}


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


def test_only_rooms_the_owner_record_names_get_a_post():
    registry = {"stream:7": {"kind": "project", "project": "kestrel", "name": "ea-kestrel"},
                "stream:8": {"kind": "project", "project": "lumen", "name": "ea-lumen"},
                "stream:9": {"kind": "project", "project": "arch", "name": "ea-arch", "archived": True},
                "stream:10": {"kind": "session", "session_id": "x", "name": "tmp-1"},
                "stream:11": {"kind": "project", "project": "swap", "name": "ea-kestrel"}}
    owner = {"stream:7": {"name": "ea-kestrel"}, "stream:9": {"name": "ea-arch"},
             "stream:10": {"name": "tmp-1"}, "stream:11": {"name": "ea-other"}}
    assert br.rooms(registry=registry, owner=owner) == {
        "kestrel": {"channel": "stream:7", "room": "ea-kestrel"}}
    owner["stream:7"]["archived_by_bot"] = True
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


@pytest.mark.parametrize("frontend", ["slack", "zulip"])
def test_a_room_key_posts_through_the_frontend_to_owner_rooms_only(monkeypatch, frontend):
    import owner_rooms
    monkeypatch.setattr(owner_rooms, "get", lambda cid: {"name": "ea-kestrel"} if cid == "C7" else None)
    fe = _FE()
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [fe])
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: frontend)
    monkeypatch.setattr(chat_notify, "_send_slack", lambda *a: pytest.fail("webhook path"))
    monkeypatch.setattr(chat_notify, "_send_zulip", lambda *a: pytest.fail("notify path"))
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
    assert k.startswith("*Morning briefing, Mon Oct 12: Kestrel radar*")
    assert ":dart: Do today: *Radar order* (Pat): first thing" in k
    assert "Also open here (1): Kestrel field test plan" in k and "Lunch" not in k
    assert "—" not in k
    assert br.pointer_line(posts, mention=lambda cid: f"<#{cid}>") == [
        "", ":house: Also in project rooms: <#stream:7> (1), <#stream:8> (1)"]
    sent = br.send(posts, poster=lambda key, text: key == "room:stream:7")
    assert sent == [{"room": "ea-kestrel", "ok": True}, {"room": "ea-lumen", "ok": False}]


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
    assert [k for k, _t in got] == ["room:stream:7"] and posts[0]["ok"] is True
    assert "the quote expires" in got[0][1]
    # a row the audit closed is in no slice
    assert rb.room_slices("morning-briefing", cands, plan, [], [], {"closed": ["row-k"]}, b, now=NOW,
                          dry_run=False, poster=lambda *a: pytest.fail("posted"), **kw) == []
    # a dry run plans and sends nothing
    dry = rb.room_slices("morning-briefing", cands, plan, [], [], {}, b, now=NOW, dry_run=True,
                         poster=lambda *a: pytest.fail("posted"), **kw)
    assert dry and "ok" not in dry[0]
