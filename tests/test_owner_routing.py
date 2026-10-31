"""Where the bot posts and whose messages it takes, against requests from
ea-bot space.

A staged file, a thread key and the room registry are all ea-bot space, so
each only asks: the bot posts in a channel only when it is one of the
owner's own (the EA channel, the scheduling channel, the owner's DM, or a
registered room that is private and holds only the owner and the bot), a
confirmation card goes only to the owner's DM, and a message is an
instruction only when its user id is the owner's.

The bot is hyphenated and heavy, so the functions are exec'd from its source
into a namespace of stubs, against a MemoryFrontend."""
import json
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

# Imported when a test runs, not at collection: the preprocessor modules
# fix their paths from EA_DIR on import, and other test modules set EA_DIR
# while they are collected.
_chat = _ea_channels = MemoryFrontend = None


@pytest.fixture(autouse=True)
def _modules():
    global _chat, _ea_channels, MemoryFrontend
    from preprocessor import chat, ea_channels
    from preprocessor.chat.memory_frontend import MemoryFrontend as _MF
    _chat, _ea_channels, MemoryFrontend = chat, ea_channels, _MF

ROOM = "C_ROOM"
STRANGER_ROOM = "C_ELSEWHERE"
SCHED = "C_SCHED"


def _fn_body(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    body = BOT_SRC[start:end]
    # a decorator line of the next function belongs to it, not to this one
    return re.sub(r"\n@app\.[^\n]*\s*$", "\n", body)


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))


class _Rooms:
    """The owner room record (preprocessor.owner_rooms), in memory."""

    def __init__(self, ids=()):
        self.rooms = None if ids is None else {c: {"name": c, "how": "created"} for c in ids}

    def path(self):
        return Path("/owner-private/owner-rooms.json")

    def load(self):
        return None if self.rooms is None else dict(self.rooms)

    def get(self, cid):
        return (self.rooms or {}).get(cid)

    def has(self, cid):
        return cid in (self.rooms or {})

    def record(self, cid, *, name="", kind="project", how="created"):
        self.rooms = self.rooms or {}
        self.rooms.setdefault(cid, {"name": name, "kind": kind, "how": how})

    def update(self, cid, **fields):
        if cid not in (self.rooms or {}):
            return False
        self.rooms[cid].update(fields)
        return True

    def start(self, adopt):
        if self.rooms is not None:
            return None
        self.rooms = {c: dict(r, how="adopted") for c, r in adopt.items()}
        return sorted(self.rooms)


ROOM_HELPERS = ("_core_room_names", "_core_channel", "_owned_room", "_room_members_ok",
                "_channel_scope")


def _ns(monkeypatch, *, registry=None, owner="U_OWNER", dm="D_OWNER", owned=None, **extra):
    fe = MemoryFrontend()
    fe.sched_channel = SCHED
    fe.rooms[ROOM] = _chat.ChannelInfo(ROOM, "ea-atlas", is_private=True, is_member=True)
    fe.rooms[STRANGER_ROOM] = _chat.ChannelInfo(STRANGER_ROOM, "other", is_private=True,
                                                is_member=True)
    reg = {ROOM: {"project": "atlas", "name": "ea-atlas"}} if registry is None else registry
    monkeypatch.setattr(_ea_channels, "scope_of", lambda c: reg.get(c))
    log = _Log()
    ns = {"json": json, "re": re, "time": time, "threading": threading, "log": log,
          "FRONTEND": fe, "_chat": _chat, "CHANNEL_ID": fe.main_channel,
          "SCHED_CHANNEL_ID": SCHED, "OWNER_SLACK_USER_ID": owner, "BOT_USER_ID": "U_BOT",
          "_CHANNEL_SCOPE_CACHE": {}, "ROOM_MEMBERS_TTL_S": 300, "_ROOM_MEMBERS_CACHE": {},
          "_resolve_dm_channel": lambda: dm,
          "_owner_rooms": _Rooms(list(reg) if owned is None else owned), **extra}
    for name in (*ROOM_HELPERS, "_thread_channel", "_web_thread_channel", "_staged_route",
                 "_owner_dm_channels", "_owner_destination", "_delivery_channel",
                 "_is_owner_message", "_owner_card_channel", "_deliver_confirmation"):
        exec(compile(_fn_body(name), f"ea-chat-bot.py[{name}]", "exec"), ns)
    return ns, fe, log


# ── finding: staged files chose where the bot posts ───────────────────

def test_a_staged_route_to_someone_elses_room_falls_back(monkeypatch):
    ns, _, log = _ns(monkeypatch)
    route = ns["_staged_route"]
    for ch in (STRANGER_ROOM, "D_STRANGER", "dm:999", "stream:77"):
        assert route({"channel": ch, "thread_ts": "1.2"}, "C_MAIN", "9.9") == ("C_MAIN", "9.9"), ch
    assert any("not one of the owner's" in m for _, m in log.lines)


def test_a_staged_route_to_the_owners_own_destinations_is_kept(monkeypatch):
    ns, _, _ = _ns(monkeypatch)
    route = ns["_staged_route"]
    assert route({"channel": SCHED, "thread_ts": "5.5"}, "C_MAIN", "9.9") == (SCHED, "5.5")
    assert route({"channel": "D_OWNER"}, "C_MAIN", "9.9") == ("D_OWNER", None)
    assert route({"channel": ROOM, "thread_ts": "7.7"}, "C_MAIN", None) == (ROOM, "7.7")
    assert route({"thread_ts": "3.3"}, "C_MAIN", "9.9") == ("C_MAIN", "3.3")
    assert route({}, "C_MAIN", "9.9") == ("C_MAIN", "9.9")


def test_a_zulip_thread_key_cannot_carry_the_post_to_another_stream(monkeypatch):
    ns, fe, _ = _ns(monkeypatch, registry={"stream:36": {"project": "atlas"}})
    fe.rooms["stream:36"] = _chat.ChannelInfo("stream:36", "ea-atlas", is_private=True,
                                              is_member=True)
    fe.name = "zulip"
    ns["CHANNEL_ID"] = "stream:4"
    route = ns["_staged_route"]
    # a key in a stream that is not the owner's is dropped
    assert route({"thread_ts": "stream:66:topic:x"}, "stream:4", "stream:4:topic:a") == \
        ("stream:4", "stream:4:topic:a")
    # a key in the owner's room moves the post to that room
    assert route({"thread_ts": "stream:36:topic:y"}, "stream:4", None) == \
        ("stream:36", "stream:36:topic:y")
    # a channel and a key that disagree: the key is dropped
    assert route({"channel": "stream:36", "thread_ts": "stream:66:topic:x"},
                 "stream:4", None) == ("stream:36", None)
    # a DM with anyone but the owner is refused; the owner's DM is fine
    assert route({"channel": "dm:999"}, "stream:4", None) == ("stream:4", None)
    assert route({"channel": "dm:U_OWNER"}, "stream:4", None) == ("dm:U_OWNER", None)


def test_the_staged_poster_routes_through_the_check():
    body = _fn_body("_post_staged_action")
    assert "_staged_route(act.pop(\"_route\", None) or {}, channel, thread_ts)" in body
    assert 'route.get("channel") or channel' not in body


def test_background_delivery_uses_the_same_check(monkeypatch):
    ns, fe, _ = _ns(monkeypatch, registry={"stream:36": {"project": "atlas"}})
    fe.rooms["stream:36"] = _chat.ChannelInfo("stream:36", "ea-atlas", is_private=True,
                                              is_member=True)
    ns["CHANNEL_ID"] = "C_MAIN"
    deliver = ns["_delivery_channel"]
    assert deliver("stream:36:topic:a") == "stream:36"
    assert deliver("stream:66:topic:a") == "C_MAIN"
    fe.members["stream:36"] = {"U_OWNER", "U_BOT", "U_GUEST"}
    ns["_ROOM_MEMBERS_CACHE"].clear()
    assert deliver("stream:36:topic:a") == "C_MAIN"


def test_a_web_thread_key_finds_its_channel_through_the_store(monkeypatch):
    """A web chat key (t<seq>) names no channel in its text: the web
    front end's store says which channel holds it, and the owner rule
    still decides whether the bot posts there."""
    ns, fe, _ = _ns(monkeypatch, dm="D_OWNER")
    held = {"t5": "D_OWNER", "t6": ROOM, "t7": STRANGER_ROOM}
    fe.thread_channel = lambda key: held.get(key)
    ns["CHANNEL_ID"] = "C_MAIN"
    deliver, route = ns["_delivery_channel"], ns["_staged_route"]
    assert (deliver("t5"), deliver("t6"), deliver("t7"), deliver("t8")) == \
        ("D_OWNER", ROOM, "C_MAIN", "C_MAIN")
    assert route({"thread_ts": "t6"}, "C_MAIN", None) == (ROOM, "t6")
    assert route({"thread_ts": "t7"}, "C_MAIN", "9.9") == ("C_MAIN", "9.9")
    # a front end without the lookup (Slack, Zulip, a composite) is as before
    del fe.thread_channel
    assert deliver("t5") == "C_MAIN" and ns["_thread_channel"]("1.2") is None


# ── finding: the room registry is ea-bot space ────────────────────────

def test_a_registered_room_with_another_member_is_not_served(monkeypatch):
    ns, fe, log = _ns(monkeypatch)
    assert ns["_channel_scope"](ROOM)["project"] == "atlas"
    fe.members[ROOM] = {"U_OWNER", "U_BOT", "U_STRANGER"}
    assert ns["_channel_scope"](ROOM) is not None          # the answer stands for the TTL
    ns["_ROOM_MEMBERS_CACHE"].clear()
    assert ns["_channel_scope"](ROOM) is None
    assert not ns["_owner_destination"](ROOM)
    assert any("besides the owner" in m for _, m in log.lines)


def test_a_room_whose_members_cannot_be_read_is_not_served(monkeypatch):
    ns, fe, _ = _ns(monkeypatch)
    fe.channel_members = lambda channel: None
    assert ns["_channel_scope"](ROOM) is None
    ns["_ROOM_MEMBERS_CACHE"].clear()
    fe.channel_members = lambda channel: (_ for _ in ()).throw(RuntimeError("down"))
    assert ns["_channel_scope"](ROOM) is None


def test_a_registry_row_alone_does_not_make_a_room_the_owners(monkeypatch):
    # ea-bot registers a public stream, or a room it does not share with the owner
    ns, fe, _ = _ns(monkeypatch, registry={STRANGER_ROOM: {"project": "x"}})
    fe.rooms[STRANGER_ROOM].is_private = False
    assert ns["_channel_scope"](STRANGER_ROOM) is None
    ns2, fe2, _ = _ns(monkeypatch, registry={STRANGER_ROOM: {"project": "x"}})
    fe2.members[STRANGER_ROOM] = {"U_BOT", "U_PARTNER"}
    assert ns2["_channel_scope"](STRANGER_ROOM) is None


def test_the_zulip_frontend_reads_stream_members(tmp_path):
    from preprocessor.chat.zulip_frontend import ZulipFrontend

    class Client:
        def __init__(self, res):
            self.res = res

        def get_stream_id(self, name):
            return {"result": "error"}

        def call_endpoint(self, url, method, request):
            assert url == "streams/36/members"
            return self.res

    fe = ZulipFrontend({"site": "https://z.example"}, api_key="k", private_dir=tmp_path,
                       client=Client({"result": "success", "subscribers": [8, 9]}))
    assert fe.channel_members("stream:36") == {"8", "9"}
    fe.client = Client({"result": "error", "msg": "no"})
    assert fe.channel_members("stream:36") is None


# ── finding: room paths acted on any registry row ─────────────────────

def test_a_registry_row_for_a_room_the_bot_did_not_create_is_not_served(monkeypatch):
    # a private room holding only the owner and the bot, but not in the
    # owner room record: the registry alone never makes it the owner's
    ns, _, log = _ns(monkeypatch, owned=[])
    assert ns["_channel_scope"](ROOM) is None
    assert not ns["_owner_destination"](ROOM)
    assert any("not a room the bot created" in m for _, m in log.lines)


def test_the_bots_own_streams_are_never_rooms(monkeypatch):
    # ea-bot registers the EA channel, the DM, or the health stream as a room
    health = "stream:90"
    reg = {"C_MAIN": {"project": "x"}, "D_OWNER": {"project": "x"}, SCHED: {"project": "x"},
           health: {"project": "x"}}
    ns, fe, _ = _ns(monkeypatch, registry=reg)
    fe.cfg = {"stream": "ea", "dm_stream": "ea-dm",
              "notify": {"health": {"stream": "ea-health"}}}
    fe.rooms[health] = _chat.ChannelInfo(health, "ea-health", is_private=True, is_member=True)
    for cid in reg:
        assert ns["_channel_scope"](cid) is None, cid
        assert not ns["_owned_room"](cid) or cid == health, cid
    assert "ea-health" in ns["_core_room_names"]()


ROOM_FNS = ("_untrusted_room_text", "_post_room_intro", "_record_owner_room",
            "_open_project_channel", "_open_session_channel", "_room_back_link",
            "_room_hygiene_pass", "_adopt_registered_rooms", "on_thread_moved")


class _Store(types.ModuleType):
    """preprocessor.project_store with one project per status."""

    def __init__(self, status):
        super().__init__("project_store")
        self.status = status

    def _get_db(self):
        rows = [{"id": k, "status": v} for k, v in self.status.items()]
        return types.SimpleNamespace(execute=lambda *a: rows, commit=lambda: None)

    def _project(self, conn, pid):
        return {"id": pid, "name": pid.title(), "status": self.status.get(pid, "")} \
            if pid in self.status else None

    def _now(self):
        return "2026-01-01T00:00:00"


def _room_ns(monkeypatch, *, registry, owned=None, status=None):
    """The room paths against a MemoryFrontend, the registry as given and
    the owner room record holding `owned`."""
    import preprocessor
    reg = {k: dict(v) for k, v in registry.items()}
    store = _Store(status or {"atlas": "active"})
    monkeypatch.setitem(sys.modules, "preprocessor.project_store", store)
    monkeypatch.setattr(preprocessor, "project_store", store, raising=False)
    monkeypatch.setattr(_ea_channels, "load", lambda path=None: reg)
    monkeypatch.setattr(_ea_channels, "for_project",
                        lambda p, path=None: [c for c, e in reg.items() if e.get("project") == p])
    monkeypatch.setattr(_ea_channels, "for_session",
                        lambda s, path=None: [c for c, e in reg.items() if e.get("session_id") == s])

    def set_archived(cid, archived, path=None, by=""):
        reg[cid]["archived"], reg[cid]["archived_by"] = archived, by
    monkeypatch.setattr(_ea_channels, "set_archived", set_archived)

    def register(cid, project="", name="", **kw):
        reg[cid] = {"project": project, "name": name, **kw}
    monkeypatch.setattr(_ea_channels, "register", register)
    notes = []
    ns, fe, log = _ns(monkeypatch, registry=reg, owned=owned,
                      _resolve_project_candidates=lambda q: (q, q.title(), []),
                      _journal_room_note=lambda pid, text: notes.append(text),
                      _project_opening_post=lambda pid: f"PACK {pid}",
                      _publish_home=lambda: None, dataclasses=__import__("dataclasses"),
                      _room_picker=lambda *a: {"ok": True, "picker": True, "text": "picker"},
                      _follow_thread_move=lambda *a: notes.append(("moved", a)))
    monkeypatch.setattr(_ea_channels, "scope_of", lambda c, path=None: reg.get(c))
    for name in ROOM_FNS:
        exec(compile(_fn_body(name), f"ea-chat-bot.py[{name}]", "exec"), ns)
    return ns, fe, log, reg, notes


def _room_posts(fe, cid):
    return [p for p in fe.posts if p["channel"] == cid]


def test_a_staged_reopen_of_a_stream_the_bot_did_not_create_is_refused(monkeypatch):
    # the resident registers someone else's archived stream for an active
    # project and stages pending-channel.json: no unarchive, no pack posted
    ns, fe, log, reg, _ = _room_ns(
        monkeypatch, owned=[],
        registry={STRANGER_ROOM: {"project": "atlas", "name": "ea-atlas", "archived": True}})
    fe.rooms[STRANGER_ROOM].is_archived = True
    res = ns["_open_project_channel"]("C_MAIN", "1.1", "", "atlas")
    assert not res["ok"] and "not a room I created" in res["text"]
    assert fe.rooms[STRANGER_ROOM].is_archived and not _room_posts(fe, STRANGER_ROOM)
    assert not [op for op in fe.room_ops if op["op"] == "unarchive"]
    ns["_room_back_link"]("C_MAIN", "1.1", {"ok": True, "room": STRANGER_ROOM})
    assert not _room_posts(fe, STRANGER_ROOM)


def test_the_bots_own_archived_room_reopens_with_the_pack(monkeypatch):
    ns, fe, _, reg, notes = _room_ns(
        monkeypatch, registry={ROOM: {"project": "atlas", "name": "ea-atlas", "archived": True}})
    fe.rooms[ROOM].is_archived = True
    res = ns["_open_project_channel"]("C_MAIN", "1.1", "", "atlas")
    assert res["ok"] and not fe.rooms[ROOM].is_archived and reg[ROOM]["archived"] is False
    assert [p["text"] for p in _room_posts(fe, ROOM)] == ["PACK atlas"]
    ns["_room_back_link"]("C_MAIN", "1.1", res)
    assert len(_room_posts(fe, ROOM)) == 2


def test_a_reopened_room_with_a_stranger_goes_back_to_the_archive(monkeypatch):
    ns, fe, _, reg, _ = _room_ns(
        monkeypatch, registry={ROOM: {"project": "atlas", "name": "ea-atlas", "archived": True}})
    fe.rooms[ROOM].is_archived = True
    fe.members[ROOM] = {"U_OWNER", "U_BOT", "U_STRANGER"}
    res = ns["_open_project_channel"]("C_MAIN", "1.1", "", "atlas")
    assert not res["ok"] and fe.rooms[ROOM].is_archived and not _room_posts(fe, ROOM)


def test_an_existing_room_answer_names_only_a_room_the_bot_serves(monkeypatch):
    ns, fe, _, _, _ = _room_ns(
        monkeypatch, owned=[], registry={STRANGER_ROOM: {"project": "atlas", "name": "x"}})
    res = ns["_open_project_channel"]("C_MAIN", "1.1", "", "atlas")
    assert not res["ok"] and not res.get("room")
    ns2, fe2, _, _, _ = _room_ns(monkeypatch, registry={ROOM: {"project": "atlas"}})
    assert ns2["_open_project_channel"]("C_MAIN", "1.1", "", "atlas")["room"] == ROOM


def test_an_existing_session_room_the_bot_did_not_create_is_refused(monkeypatch, tmp_path):
    sid = "0123abcd-0000-4000-8000-000000000000"
    from preprocessor import slack_sessions
    monkeypatch.setattr(slack_sessions, "session_file", lambda s: tmp_path)
    ns, fe, _, _, _ = _room_ns(monkeypatch, owned=[],
                               registry={STRANGER_ROOM: {"session_id": sid, "kind": "session"}})
    res = ns["_open_session_channel"](sid)
    assert not res["ok"] and not res.get("room")


def test_a_new_room_is_recorded_owner_side_and_served(monkeypatch):
    ns, fe, _, reg, _ = _room_ns(monkeypatch, registry={}, owned=[],
                                 status={"borealis": "active"})
    res = ns["_open_project_channel"]("C_MAIN", "1.1", "", "borealis")
    cid = res["room"]
    assert res["ok"] and ns["_owner_rooms"].has(cid) and reg[cid]["project"] == "borealis"
    assert [p["text"] for p in _room_posts(fe, cid)] == ["PACK borealis"]


def test_room_hygiene_leaves_a_stream_the_bot_did_not_create(monkeypatch):
    # a dormant project whose registry row names the EA channel, or a room
    # not in the owner record: neither is archived
    reg = {"C_MAIN": {"project": "atlas", "name": "ea"},
           STRANGER_ROOM: {"project": "atlas", "name": "other"},
           ROOM: {"project": "atlas", "name": "ea-atlas"}}
    ns, fe, _, _, _ = _room_ns(monkeypatch, registry=reg, owned=["C_MAIN", ROOM],
                               status={"atlas": "dormant"})
    changed = ns["_room_hygiene_pass"]()
    archived = [op["channel"] for op in fe.room_ops if op["op"] == "archive"]
    assert archived == [ROOM] and len(changed) == 1
    assert ns["_owner_rooms"].get(ROOM)["archived_by_bot"] is True


def test_room_hygiene_unarchives_only_what_it_archived(monkeypatch):
    # ea-bot marks the owner's own archived room "archived_by: bot"
    reg = {ROOM: {"project": "atlas", "archived": True, "archived_by": "bot"}}
    ns, fe, _, _, _ = _room_ns(monkeypatch, registry=reg)
    fe.rooms[ROOM].is_archived = True
    ns["_room_hygiene_pass"]()
    assert fe.rooms[ROOM].is_archived
    ns["_owner_rooms"].update(ROOM, archived_by_bot=True)
    ns["_room_hygiene_pass"]()
    assert not fe.rooms[ROOM].is_archived


def test_a_room_intro_is_posted_only_in_a_room_the_bot_serves(monkeypatch):
    ns, fe, _, _, _ = _room_ns(monkeypatch, owned=[], registry={ROOM: {"project": "atlas"}})
    with pytest.raises(PermissionError):
        ns["_post_room_intro"](ROOM, "x")
    assert not fe.posts


def test_the_record_begins_with_the_rooms_that_hold_only_the_owner_and_the_bot(monkeypatch):
    extra, public, alone = "C_EXTRA", "C_PUBLIC", "C_ALONE"
    reg = {ROOM: {"project": "atlas", "name": "ea-atlas"}, extra: {"project": "b"},
           public: {"project": "c"}, alone: {"project": "d"}, "C_MAIN": {"project": "e"},
           "C_GONE": {"project": "f"}}
    ns, fe, log, _, _ = _room_ns(monkeypatch, registry=reg, owned=None)
    ns["_owner_rooms"].rooms = None                    # no record yet
    for cid in (extra, public, alone):
        fe.rooms[cid] = _chat.ChannelInfo(cid, cid.lower(), is_private=True, is_member=True)
    fe.rooms[public].is_private = False
    fe.members[extra] = {"U_OWNER", "U_BOT", "U_STRANGER"}
    fe.members[alone] = {"U_BOT"}
    assert ns["_adopt_registered_rooms"]() == [ROOM]
    assert ns["_owner_rooms"].rooms[ROOM]["how"] == "adopted"
    for cid in (extra, public, alone, "C_MAIN", "C_GONE"):
        assert any(cid in m and "not taken over" in m for _, m in log.lines), cid
    # once the record exists nothing more is taken over
    fe.members[extra] = {"U_OWNER", "U_BOT"}
    assert ns["_adopt_registered_rooms"]() == []
    assert not ns["_owner_rooms"].has(extra)


def test_only_the_owners_or_the_bots_move_rekeys_a_thread(monkeypatch):
    ns, fe, log, _, notes = _room_ns(monkeypatch, registry={})
    ns["BOT_USER_ID"] = "U_BOT"
    mv = {"old": "stream:4:topic:a", "new": "stream:9:topic:a", "channel": "stream:9",
          "old_channel": "stream:4"}
    for who in ("U_STRANGER", "", None):
        ns["on_thread_moved"]({**mv, "user_id": who} if who is not None else dict(mv))
    assert not notes and any("ignored" in m for _, m in log.lines)
    ns["on_thread_moved"]({**mv, "user_id": "U_OWNER"})
    ns["on_thread_moved"]({**mv, "user_id": "U_BOT"})
    assert len(notes) == 2


def test_the_owner_room_record_is_owner_private(tmp_path, monkeypatch):
    from preprocessor import owner_rooms
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    assert owner_rooms.load() is None and not owner_rooms.has("stream:5")
    assert owner_rooms.start({"stream:5": {"name": "ea-atlas"}}) == ["stream:5"]
    assert owner_rooms.start({"stream:6": {}}) is None
    owner_rooms.record("stream:7", name="tmp-1", kind="session")
    assert owner_rooms.has("stream:7") and owner_rooms.get("stream:5")["how"] == "adopted"
    assert owner_rooms.update("stream:5", archived_by_bot=True)
    assert not owner_rooms.update("stream:6", archived_by_bot=True)
    assert (priv / owner_rooms.FILE).stat().st_mode & 0o077 == 0
    # a record anyone else can write counts as no record
    (priv / owner_rooms.FILE).chmod(0o666)
    assert not owner_rooms.has("stream:5")


def test_every_room_path_goes_through_the_check():
    assert "_channel_scope(room) is None" in _fn_body("_post_room_intro")
    assert "_channel_scope(room) is None" in _fn_body("_room_back_link")
    assert "if not _owned_room(cid):" in _fn_body("_open_project_channel")
    assert "if not _owned_room(cid):" in _fn_body("_room_hygiene_pass")
    assert "_adopt_registered_rooms()" in BOT_SRC[BOT_SRC.index('if __name__ == "__main__":'):]
    assert "except _chat.ChannelNotPrivate as e:" in BOT_SRC


# ── finding: a card outside the owner's DM ────────────────────────────

def test_a_card_goes_only_to_the_owners_dm(monkeypatch):
    crumbs = []
    ns, fe, _ = _ns(monkeypatch, _note_run_post=lambda kind: None,
                    _post_dm_breadcrumb=lambda *a: crumbs.append(a),
                    _post_card=lambda ch, card, thread_ts=None: fe.post_card(ch, card,
                                                                             thread_ts=thread_ts))
    card = _chat.ConfirmationCard(kind="event", title="t", lines=["l"], actions=[])
    assert ns["_deliver_confirmation"]("C_MAIN", "1.1", card, "The Create button", "event")[0] == "D_OWNER"
    assert [c["channel"] for c in fe.cards] == ["D_OWNER"]
    ns["_resolve_dm_channel"] = lambda: None
    assert ns["_deliver_confirmation"]("C_MAIN", "1.1", card, "The Create button", "event") == (None, None)
    assert [c["channel"] for c in fe.cards] == ["D_OWNER"]
    assert "no DM with the owner" in fe.posts[-1]["text"]


# ── finding: instructions from anyone but the owner ───────────────────

def _on_message_ns(monkeypatch):
    dispatched = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args

        def start(self):
            dispatched.append(self.args)

    ns, fe, _ = _ns(monkeypatch)
    ns.update({"threading": types.SimpleNamespace(Thread=_Thread, Lock=threading.Lock),
               "_last_message_lock": threading.Lock(), "_last_message_time": 0.0,
               "_active_lock": threading.Lock(), "_active_threads": set(),
               "_is_bot_command": lambda t: False, "EA_DIR": Path("/nonexistent"),
               "handle_message_async": lambda *a: None})
    exec(compile(_fn_body("on_message"), "ea-chat-bot.py[on_message]", "exec"), ns)
    return ns, fe, dispatched


def _msg(channel, user, text="do the thing", **kw):
    kw.setdefault("is_dm", channel.startswith("D"))
    return _chat.InboundMessage(channel=channel, msg_id=f"m{time.time_ns()}", text=text,
                                user_id=user, posted_at=time.time(), **kw)


def test_only_the_owner_instructs_in_the_main_and_scheduling_channels(monkeypatch):
    ns, _, dispatched = _on_message_ns(monkeypatch)
    for channel in ("C_MAIN", SCHED, ROOM, "D_OWNER"):
        ns["on_message"](_msg(channel, "U_COLLEAGUE"))
        assert not dispatched, channel
        ns["on_message"](_msg(channel, "U_OWNER"))
        assert len(dispatched) == 1 and dispatched[0][0] == channel, channel
        dispatched.clear()


def test_no_owner_id_means_no_instructions(monkeypatch):
    ns, _, dispatched = _on_message_ns(monkeypatch)
    ns["OWNER_SLACK_USER_ID"] = None
    ns["on_message"](_msg("C_MAIN", "U_OWNER"))
    assert not dispatched


def test_the_catchup_dispatches_only_the_owners_messages(monkeypatch):
    spawned = []
    ns, fe, _ = _ns(monkeypatch, _catchup_spawn=lambda *a: spawned.append(a))
    exec(compile(_fn_body("_catchup_walk"), "ea-chat-bot.py[_catchup_walk]", "exec"), ns)
    hist = [_msg("C_MAIN", "U_COLLEAGUE", reply_count=0),
            _msg("C_MAIN", "U_BOT", reply_count=0, is_self=True),
            _msg("C_MAIN", "U_OWNER", reply_count=0)]
    ns["_catchup_walk"](hist)
    assert [a[3] for a in spawned] == ["U_OWNER"]
    # a stranger's follow-up in a thread is not the owner's last word
    spawned.clear()
    parent = _msg("C_MAIN", "U_OWNER", reply_count=1)
    reply = _msg("C_MAIN", "U_COLLEAGUE", is_reply=True)
    reply.posted_at = time.time() - 300
    fe.thread_replies = lambda ch, ts, n: [parent, reply]
    ns["_catchup_walk"]([parent])
    assert spawned == []


def test_a_zulip_owner_with_a_bot_like_address_is_still_the_owner():
    from preprocessor.chat.zulip_frontend import inbound_from_message
    m = {"id": 5, "sender_id": 8, "sender_email": "ana-bot@z.example", "type": "stream",
         "stream_id": 4, "subject": "t", "content": "hi"}
    assert not inbound_from_message(m, 1, 8, None, lambda k, i: False).is_bot
    other = dict(m, sender_id=9)
    assert inbound_from_message(other, 1, 8, None, lambda k, i: False).is_bot


def test_the_on_message_gate_checks_the_owner_by_id():
    gate = _fn_body("on_message")
    assert 'str(msg.user_id) != str(OWNER_SLACK_USER_ID)' in gate
    assert "_is_owner_message(m)" in _fn_body("_catchup_walk")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
