"""Project and session rooms on the web chat alone (`"frontend": "web"`).

The bot's room path runs against a WebFrontend on a temporary store, with
the real room registry (ea_channels) and the real owner room record
(owner_rooms) in temporary directories: `open a channel for <project>`
creates the room, registers it and posts its opening pack; the app gets
the room's `channel` event; the owner's message in the room is routed to
the room's project scope; the room's specialist session is minted; the
briefing posts its room slice there; the hygiene pass archives the room of
a dormant project. A session room (#tmp-<id8>), a room opened for a
project created in a turn, the staged pending-channel.json and the
picker's Open button take the same path, and a room made before its
registration failed is taken back. Shadow mode (a Zulip primary, the web
chat as its mirror) keeps the primary's room ids.

The bot is hyphenated and heavy, so its functions are exec'd from its
source into a namespace of stubs, as tests/test_owner_routing.py does."""
from __future__ import annotations

import dataclasses
import json
import queue
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocessor"))
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

SID = "5f0c2a9e-7b1d-4c3e-9a8f-0d1e2f3a4b5c"
LOGIN = "owner@example.test"

# Imported when a test runs, not at collection: the preprocessor modules fix
# their paths from EA_DIR on import, and other test files set EA_DIR while
# they are collected.
_chat = _ea_channels = _owner_rooms = None


@pytest.fixture(autouse=True)
def _modules():
    global _chat, _ea_channels, _owner_rooms
    from preprocessor import chat, ea_channels, owner_rooms
    _chat, _ea_channels, _owner_rooms = chat, ea_channels, owner_rooms


def _fn_body(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    body = BOT_SRC[start:end]
    # a decorator line of the next function belongs to it, not to this one
    return re.sub(r"\n@app\.[^\n]*\s*$", "\n", body)


def _stmt(name: str) -> str:
    """A top-level assignment of the bot, its continuation lines included."""
    start = BOT_SRC.index(f"\n{name} = ") + 1
    lines = BOT_SRC[start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))


class _Store(types.ModuleType):
    """preprocessor.project_store: one status per project, and the
    projects a turn created."""

    def __init__(self, status):
        super().__init__("project_store")
        self.status = dict(status)
        self.created: list[dict] = []
        self.journal: list[tuple] = []

    def _get_db(self):
        store = self

        def execute(sql, args=()):
            if sql.lstrip().upper().startswith("INSERT"):
                store.journal.append(tuple(args))
                return []
            return [{"id": k, "status": v} for k, v in store.status.items()]
        return types.SimpleNamespace(execute=execute, commit=lambda: None)

    def _project(self, conn, pid):
        if pid not in self.status:
            return None
        return {"id": pid, "name": pid.title(), "status": self.status[pid]}

    def _now(self):
        return "2026-01-01T00:00:00"

    def projects_created_since(self, t):
        return list(self.created)


class _Thread:
    """threading.Thread for on_message: records the dispatch it would start."""
    started: list = []

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append(self.args)


BOT_FNS = (
    "_core_room_names", "_core_channel", "_owned_room", "_room_members_ok", "_channel_scope",
    "_owner_dm_channels", "_owner_destination", "_record_owner_room", "_untrusted_room_text",
    "_room_not_permitted_text", "_unregistered_own_room", "_post_room_intro",
    "_open_project_channel",
    "_open_session_channel", "_announce_room", "_room_back_link", "_open_rooms_for_new_projects",
    "_open_channel_action", "_finish_room_pick", "_room_hygiene_pass", "_bind_room_session",
    "_colleague_command", "on_message",
)
REGEXES = ("_CONVERSATIONS_RE", "_VERSION_RE", "_ROUTING_RE", "_NAME_THREAD_RE", "_ROOM_VERB",
           "_OPEN_CHANNEL_RE", "_RESUME_THREAD_RE")


def _web(tmp_path):
    """The web chat as the owner's bot builds it: a trusted chat block and a
    private store; served on a loopback port."""
    from preprocessor.chat.web_frontend import WebFrontend
    fe = WebFrontend.from_config({"port": 0, "tailscale_login": LOGIN,
                                  "public_url": "https://host.example.test/chat"},
                                 store_path=tmp_path / "webchat" / "webchat.sqlite",
                                 webui_dir=tmp_path / "webui", trusted_config=True)
    fe.poll_s = fe.pair_delay_s = 0.02
    return fe


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """(namespace, frontend, project store, adopted sessions, log) with the
    bot's room functions exec'd against `frontend`, a started web chat."""
    import preprocessor
    from preprocessor import ea_config, slack_sessions
    import briefing_rooms  # noqa: F401  (its `import ea_channels` is the bare module)
    import ea_channels as bare_channels
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(private))
    (tmp_path / "config").mkdir()
    registry = tmp_path / "config" / "ea-channels.json"
    for mod in (_ea_channels, bare_channels):
        monkeypatch.setattr(mod, "REGISTRY", registry)
    store = _Store({"atlas": "active", "lumen": "active"})
    monkeypatch.setitem(sys.modules, "preprocessor.project_store", store)
    monkeypatch.setattr(preprocessor, "project_store", store, raising=False)
    transcript = tmp_path / f"{SID}.jsonl"
    transcript.write_text("{}\n")
    adopted: list = []
    monkeypatch.setattr(slack_sessions, "session_file", lambda sid: transcript)
    monkeypatch.setattr(slack_sessions, "describe", lambda key: None)
    monkeypatch.setattr(slack_sessions, "find_thread", lambda sid: "")
    monkeypatch.setattr(slack_sessions, "has", lambda key: False)
    monkeypatch.setattr(slack_sessions, "adopt",
                        lambda key, sid, **kw: adopted.append((key, sid, kw)))
    monkeypatch.setattr(ea_config, "get_models", lambda: {"room_specialist": "spec-model"})
    monkeypatch.setattr(ea_config, "get_efforts", lambda: {"room_specialist": "high"})
    fe = _web(tmp_path)
    fe.start()
    log = _Log()
    _Thread.started = []
    ns = {"json": json, "re": re, "time": time, "uuid": __import__("uuid"),
          "dataclasses": dataclasses, "log": log, "FRONTEND": fe, "_chat": _chat,
          "CHANNEL_ID": fe.main_channel, "SCHED_CHANNEL_ID": fe.sched_channel,
          "OWNER_SLACK_USER_ID": fe.owner_id, "BOT_USER_ID": fe.bot_user_id,
          "EA_DIR": tmp_path, "_CHANNEL_SCOPE_CACHE": {}, "ROOM_MEMBERS_TTL_S": 300,
          "_ROOM_MEMBERS_CACHE": {}, "_owner_rooms": _owner_rooms,
          "_resolve_dm_channel": lambda: fe.dm_channel(fe.owner_id),
          "_resolve_project_candidates":
              lambda q: (q, q.title(), []) if q in store.status else ("", "", []),
          "_project_opening_post": lambda pid: f"PACK {pid}",
          "_journal_room_note": lambda pid, text: store.journal.append((pid, text)),
          "_publish_home": lambda: None,
          "_room_picker": lambda *a: {"ok": True, "picker": True, "text": "picker"},
          "_post_card": lambda ch, card, thread_ts=None: fe.post_card(ch, card,
                                                                     thread_ts=thread_ts),
          "_replace_buttons_with_status": lambda ch, ts, text: None,
          "threading": types.SimpleNamespace(Thread=_Thread, Lock=threading.Lock),
          "_last_message_lock": threading.Lock(), "_last_message_time": 0.0,
          "_active_lock": threading.Lock(), "_active_threads": set(),
          "_is_bot_command": lambda t: False, "handle_message_async": lambda *a: None}
    for name in REGEXES:
        exec(compile(_stmt(name), f"ea-chat-bot.py[{name}]", "exec"), ns)
    for name in BOT_FNS:
        exec(compile(_fn_body(name), f"ea-chat-bot.py[{name}]", "exec"), ns)
    fe.registry.event("message")(ns["on_message"])
    yield ns, fe, store, adopted, log
    fe.stop()


def _say(fe, channel: str, text: str, thread_key: str | None = None) -> dict:
    """The owner types `text` in the web app (POST .../messages): the store
    takes it and the bot's handler runs, as the HTTP route does."""
    row, after = fe.web_message(channel, text, thread_key)
    if after is not None:
        after()
    return row


def _command(ns, fe, text: str, channel: str = "main") -> tuple[dict, dict]:
    """The owner's message in a thread of `channel`, answered by the bot's
    own command path (no model call): (the stored message, the result)."""
    row, _ = fe.web_message(channel, text, None)
    return row, ns["_colleague_command"](channel, row["thread_key"], row["id"], text)


def _bot_texts(fe, channel: str) -> list[str]:
    out = []
    for t in fe.store.threads(channel, 50):
        out += [m["text"] for m in fe.store.thread(channel, t["thread_key"])
                if m["author"] != "owner"]
    return out


def _events(sub) -> list[tuple[str, dict]]:
    out = []
    while True:
        try:
            _, typ, payload = sub.q.get_nowait()
        except queue.Empty:
            return out
        out.append((typ, json.loads(payload)))


def test_open_a_channel_makes_a_web_room_the_bot_serves(bot):
    ns, fe, store, adopted, log = bot
    sub = fe.store.subscribe()
    try:
        asked, res = _command(ns, fe, "open a channel for atlas")
        assert res["ok"] and res["room"] == "r1", res
        assert "could not register" not in res["text"]
        # registered under the web room id, with its link; in the owner record
        row = _ea_channels.scope_of("r1")
        assert row["kind"] == "project" and row["project"] == "atlas"
        assert row["name"] == "ea-atlas" and row["url"].endswith("/#/r1")
        assert _ea_channels.for_project("atlas") == ["r1"]
        assert _owner_rooms.get("r1")["name"] == "ea-atlas"
        # a private room in the store, announced to the open app as a channel event
        ch = fe.store.channel("r1")
        assert ch["kind"] == "room" and ch["name"] == "ea-atlas" and not ch["archived"]
        assert ("channel", "r1") in [(t, e.get("id")) for t, e in _events(sub)]
    finally:
        fe.store.unsubscribe(sub)
    assert ns["_channel_scope"]("r1")["project"] == "atlas"
    assert ns["_owner_destination"]("r1")
    # the opening pack sits at the room's top level, a thread of its own
    assert _bot_texts(fe, "r1") == ["PACK atlas"]
    assert any("Room #ea-atlas opened" in str(j) for j in store.journal)
    # the thread hears where the room is, and the room links back to the thread
    ns["_announce_room"]("main", asked["thread_key"], res)
    said = [m["text"] for m in fe.store.thread("main", asked["thread_key"])]
    assert any("Opened [#ea-atlas](https://host.example.test/chat/#/r1)" in t for t in said), said
    back = [t for t in _bot_texts(fe, "r1") if "Opened from" in t]
    assert back and f"/#/main/{asked['thread_key']}/" in back[0], back
    # asking again names the room it has
    _, again = _command(ns, fe, "open a channel for atlas")
    assert again["ok"] and again["room"] == "r1" and "already has a channel" in again["text"]
    assert [c["id"] for c in fe.store.channels() if c["kind"] == "room"] == ["r1"]
    assert not [m for lvl, m in log.lines if lvl in ("error", "warning")], log.lines


def test_the_owners_message_in_a_web_room_is_answered_with_its_project(bot):
    ns, fe, _, adopted, _ = bot
    _command(ns, fe, "open a channel for atlas")
    _Thread.started.clear()
    msg = _say(fe, "r1", "what is still open here?")
    assert len(_Thread.started) == 1, "the owner's message in the room was dropped"
    channel, thread_ts, ts, user_msg, user_id, is_reply = _Thread.started[0]
    assert (channel, thread_ts, ts, user_id, is_reply) == (
        "r1", msg["thread_key"], msg["id"], fe.owner_id, False)
    assert user_msg.startswith("[From the private #ea-atlas channel, scoped to project 'atlas'.")
    assert user_msg.endswith("what is still open here?")
    # a reply in the same thread stays in it
    _say(fe, "r1", "and the budget?", msg["thread_key"])
    assert _Thread.started[1][:2] == ("r1", msg["thread_key"]) and _Thread.started[1][5]
    # the room's specialist: minted on its first thread, pinned on the row
    ns["_bind_room_session"]("r1", msg["thread_key"])
    row = _ea_channels.scope_of("r1")
    assert re.fullmatch(r"[0-9a-f-]{36}", row["session_id"])
    assert (row["model"], row["effort"]) == ("spec-model", "high")
    assert adopted == [(msg["thread_key"], row["session_id"],
                        {"source": "project-room", "model": "", "effort": "",
                         "require_file": False})]


def test_the_briefing_posts_its_slice_in_the_web_room(bot, monkeypatch):
    ns, fe, *_ = bot
    _command(ns, fe, "open a channel for atlas")
    import briefing_rooms
    import chat_notify
    assert briefing_rooms.rooms() == {"atlas": {"channel": "r1", "room": "ea-atlas"}}
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [fe])
    assert chat_notify._send_room(chat_notify.room_key("r1"), ["Do today: the atlas review"])
    assert "Do today: the atlas review" in _bot_texts(fe, "r1")
    assert chat_notify.room_mention("r1") == "[#ea-atlas](https://host.example.test/chat/#/r1)"


def test_the_hygiene_pass_archives_and_reopens_a_web_room(bot):
    ns, fe, store, *_ = bot
    _command(ns, fe, "open a channel for atlas")
    store.status["atlas"] = "dormant"
    assert ns["_room_hygiene_pass"]() == ["archived #ea-atlas (atlas is dormant)"]
    assert fe.store.channel("r1")["archived"] and _ea_channels.scope_of("r1")["archived"]
    assert _owner_rooms.get("r1")["archived_by_bot"]
    from preprocessor.chat import web_frontend as wf
    with pytest.raises(wf._HttpError, match="archived"):
        fe.web_message("r1", "still there?", None)
    store.status["atlas"] = "active"
    assert ns["_room_hygiene_pass"]() == ["unarchived #ea-atlas (atlas is active again)"]
    assert not fe.store.channel("r1")["archived"]
    # a room archived in the store by anyone but the bot (the owner) stays
    # archived and is noted; `open a channel` reopens it
    fe.store.set_channel("r1", archived=True)
    assert ns["_room_hygiene_pass"]()[0].startswith("noted #ea-atlas archived by the owner")
    assert _ea_channels.scope_of("r1")["archived_by"] == "user"
    _, res = _command(ns, fe, "open a channel for atlas")
    assert res["ok"] and res["text"].startswith(":file_folder: Reopened")
    assert not fe.store.channel("r1")["archived"]


def test_a_session_room_on_the_web_chat(bot):
    ns, fe, _, adopted, _ = bot
    _, res = _command(ns, fe, f"open a channel for session {SID}")
    assert res["ok"] and res["room"] == "r1", res
    row = _ea_channels.scope_of("r1")
    assert (row["kind"], row["session_id"], row["name"]) == ("session", SID, "tmp-5f0c2a9e")
    assert _owner_rooms.get("r1")["kind"] == "session"
    assert fe.store.channel("r1")["name"] == "tmp-5f0c2a9e"
    assert any(f"mirrors session `{SID}`" in t for t in _bot_texts(fe, "r1"))
    _Thread.started.clear()
    msg = _say(fe, "r1", "pick it up from here")
    assert len(_Thread.started) == 1
    assert _Thread.started[0][3].startswith(
        "[From #tmp-5f0c2a9e, the chat room that mirrors your session 5f0c2a9e.")
    ns["_bind_room_session"]("r1", msg["thread_key"])
    assert adopted[-1][:2] == (msg["thread_key"], SID)


def test_rooms_from_a_turn_a_staged_file_and_the_picker(bot):
    ns, fe, store, *_ = bot
    asked, _ = _command(ns, fe, "register the lumen survey as a project")
    # add-project in a turn: the room opens by itself and the thread hears of it
    store.created = [{"id": "lumen", "name": "Lumen", "status": "active"}]
    assert ns["_open_rooms_for_new_projects"]("main", asked["thread_key"], time.time(), "t") == 1
    assert _ea_channels.for_project("lumen") == ["r1"]
    said = [m["text"] for m in fe.store.thread("main", asked["thread_key"])]
    assert any(t.startswith(":file_folder: **Lumen** is registered.") and "#/r1" in t
               for t in said), said
    assert ns["_open_rooms_for_new_projects"]("main", asked["thread_key"], time.time(), "t") == 0
    # pending-channel.json, which the model stages for a project that exists
    ns["_open_channel_action"]("main", asked["thread_key"], {"project": "atlas"})
    assert _ea_channels.for_project("atlas") == ["r2"]
    assert ns["_channel_scope"]("r2")["project"] == "atlas"
    # the picker's Open button on a project that has its room
    card = fe.post("main", "picker", thread_ts=asked["thread_key"])
    ns["_finish_room_pick"](card, "lumen", {"c": "main", "t": asked["thread_key"]})
    assert sorted(c["id"] for c in fe.store.channels() if c["kind"] == "room") == ["r1", "r2"]


def test_a_room_made_before_its_registration_failed_is_taken_back(bot):
    """What the bot left when the registry refused web room ids: the room in
    the store and in the owner record, no registry row. Asking again
    registers that room; a room of the name the bot did not make stays
    refused."""
    ns, fe, *_ = bot
    fe.create_private_room("ea-atlas", "EA project channel: Atlas (atlas)")
    _owner_rooms.record("r1", name="ea-atlas", kind="project")
    fe.create_private_room("tmp-5f0c2a9e")
    _owner_rooms.record("r2", name="tmp-5f0c2a9e", kind="session")
    assert _ea_channels.load() == {}
    _, res = _command(ns, fe, "open a channel for atlas")
    assert res["ok"] and res["room"] == "r1", res
    assert _ea_channels.scope_of("r1")["project"] == "atlas"
    assert _bot_texts(fe, "r1") == ["PACK atlas"]
    _, res = _command(ns, fe, f"open a channel for session {SID}")
    assert res["ok"] and res["room"] == "r2", res
    assert _ea_channels.scope_of("r2")["session_id"] == SID
    fe.create_private_room("ea-lumen")
    _, res = _command(ns, fe, "open a channel for lumen")
    assert not res["ok"] and "already exists" in res["text"]
    assert _ea_channels.for_project("lumen") == [] and not _bot_texts(fe, "r3")
    assert sorted(c["id"] for c in fe.store.channels() if c["kind"] == "room") == ["r1", "r2", "r3"]


def test_a_refused_room_names_the_platform_that_refused(bot):
    ns, fe, *_ = bot

    def refuse(name, description=""):
        raise _chat.NotPermitted("not allowed")
    fe.create_private_room = refuse
    _, res = _command(ns, fe, "open a channel for atlas")
    assert not res["ok"] and "Slack" not in res["text"] and "not allowed" in res["text"]
    ns["FRONTEND"] = types.SimpleNamespace(name="slack")
    assert "groups:write" in ns["_room_not_permitted_text"](_chat.NotPermitted("x"))
    ns["FRONTEND"] = types.SimpleNamespace(name="slack+web")
    assert "groups:write" in ns["_room_not_permitted_text"](_chat.NotPermitted("x"))


def test_shadow_mode_keeps_the_primarys_room_ids(bot, tmp_path):
    """Zulip primary, web mirror: the room is the primary's stream, and the
    web store holds it under that id."""
    from preprocessor.chat.memory_frontend import MemoryFrontend
    from preprocessor.chat.shadow_frontend import ShadowFrontend
    from preprocessor.chat.types import RoomRef
    ns, _, *_ = bot

    class ZulipLike(MemoryFrontend):
        name = "zulip"

        def create_private_room(self, name, description=""):
            room = super().create_private_room(name, description)
            cid = f"stream:{40 + len(self.created_rooms)}"
            self.rooms[cid] = self.rooms.pop(room.id)
            self.rooms[cid].id = cid
            return RoomRef(cid, name, f"https://zulip.example.test/#narrow/channel/{cid[7:]}")

    web = _web(tmp_path / "shadow")
    composite = ShadowFrontend(ZulipLike(), web)
    ns.update({"FRONTEND": composite, "CHANNEL_ID": composite.main_channel,
               "OWNER_SLACK_USER_ID": composite.owner_id, "BOT_USER_ID": composite.bot_user_id,
               "_resolve_dm_channel": lambda: "D_OWNER"})
    res = ns["_open_project_channel"]("C_MAIN", "", "", "atlas")
    assert res["ok"] and res["room"] == "stream:41", res
    assert _ea_channels.scope_of("stream:41")["project"] == "atlas"
    assert web.store.channel("stream:41")["name"] == "ea-atlas"
    assert web.store.channel("stream:41")["kind"] == "room"
    assert not [c for c in web.store.channels() if re.fullmatch(r"r\d+", c["id"])]
    web.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
