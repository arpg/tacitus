#!/usr/bin/env python3
"""A project created during a Slack turn gets its #ea- room.

What must hold: the store answers "which projects were created since T",
the bot asks it after detection in both lanes and opens a room only for an
active project with no registered channel, through the same path the
`open a channel` command uses, and announces it in the thread.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-rooms-")

import project_store as ps  # noqa: E402

BOT_SRC = (REPO / "ea-chat-bot.py").read_text()


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    return BOT_SRC[start:end]


def _add(pid: str, status: str = "active", horizon: str = "project") -> None:
    rc = ps.cmd_add_project(argparse.Namespace(
        id=pid, name=pid.replace("-", " ").title(), domain="research", kind=None,
        status=status, parent=None, summary="", next_actions="", aliases="",
        horizon=horizon))
    assert rc == 0


def test_projects_created_since_reads_the_window():
    before = time.time() - 5
    _add("radar-ice-2026")
    _add("lab-fleet", horizon="area")
    _add("parked-idea", status="dormant")
    rows = ps.projects_created_since(before)
    ids = [r["id"] for r in rows]
    assert {"radar-ice-2026", "lab-fleet", "parked-idea"} <= set(ids)
    assert {r["id"]: r["horizon"] for r in rows}["lab-fleet"] == "area"
    assert {r["id"]: r["status"] for r in rows}["parked-idea"] == "dormant"
    assert ps.projects_created_since(time.time() + 60) == []


def test_bot_opens_rooms_after_detection_in_both_lanes():
    fn = _fn_src("_open_rooms_for_new_projects")
    assert "_ps.projects_created_since(run_started - 2)" in fn
    # idempotent, active only, the command's own path, no rename
    assert '_ea_channels.for_project(pid)' in fn and 'row.get("status") != "active"' in fn
    assert '_open_project_channel(channel, thread_ts or "", "", pid)' in fn
    assert "if not run_started or not _rooms_on():\n        return 0" in fn
    # the thread hears about it, the room links back (shared announcer)
    assert "_announce_room(channel, thread_ts, res," in fn
    ann = _fn_src("_announce_room")
    assert "FRONTEND.post(channel=channel, thread_ts=thread_ts or None, text=text)" in ann
    assert "_room_back_link(channel, thread_ts, res)" in ann
    assert "FRONTEND.permalink(channel, thread_ts)" in _fn_src("_room_back_link")
    # both lanes call it after the staged-action claim, before the promise check
    mono_claim = BOT_SRC.index('_claim_event_actions(newer_than=run_started, token=stage_tok),\n                "mono")')
    mono_rooms = BOT_SRC.index('_open_rooms_for_new_projects(channel, thread_ts, run_started, "mono")')
    mono_warn = BOT_SRC.index('_warn_if_button_promised(channel, thread_ts, response, "mono")')
    assert mono_claim < mono_rooms < mono_warn
    resident = _fn_src("_resident_post_run_detection")
    assert '_open_rooms_for_new_projects(channel, thread_ts, started_at, "resident")' in resident
    assert resident.index("_claim_event_actions(newer_than=started_at)") < \
        resident.index("_open_rooms_for_new_projects(")


def test_the_model_is_told_the_room_comes_by_itself():
    core = (REPO / "CLAUDE.md").read_text()
    assert "gets its room after your turn" in " ".join(core.split())
    guide = (REPO / "docs" / "ea" / "project-store.md").read_text()
    assert "do not tell the user to say `open a\n  channel`" in guide or "do not tell the user to say" in guide


def _open_channel_re():
    i = BOT_SRC.index("_ROOM_VERB = ")
    j = BOT_SRC.index("re.I | re.S)", i) + len("re.I | re.S)")
    ns = {"re": __import__("re")}
    exec(BOT_SRC[i:j], ns)
    return ns["_OPEN_CHANNEL_RE"]


def test_room_command_accepts_create_make_and_room_wording():
    rx = _open_channel_re()
    for text, project in (("open a channel for lab-bench-rebuild", "lab-bench-rebuild"),
                          ("Create a channel for this conversation about foundation repair connected to this",
                           "this conversation about foundation repair connected to this"),
                          ("make a room for the field-trial engagement", "the field-trial engagement"),
                          ("spin up a private project channel for spring-symposium", "spring-symposium"),
                          ("start a channel named ea-fixes for lab-bench-rebuild", "lab-bench-rebuild")):
        m = rx.match(text)
        assert m, text
        assert (m.group("project") or "").strip() == project, (text, m.groupdict())
    assert _open_channel_re().match("open a channel named ea-fixes for x").group("name") == "ea-fixes"
    assert _open_channel_re().match("open a channel for this conversation").group("this")
    for text in ("create a project for foundation repair", "channel the energy", "open the door"):
        assert not rx.match(text), text
    # the fast gate that skips Claude agrees with it
    k = BOT_SRC.index("_BOT_COMMAND_RE = re.compile(")
    gate = BOT_SRC[k:BOT_SRC.index("re.I | re.S)", k) + len("re.I | re.S)")]
    ns = {"re": __import__("re")}; exec(gate, ns)
    assert ns["_BOT_COMMAND_RE"].match("Create a channel for this conversation about foundation repair connected to this")
    assert ns["_BOT_COMMAND_RE"].match("make a room for field-trial")


def test_room_command_reads_this_project_and_trailing_punctuation():
    rx = _open_channel_re()
    m = rx.match("Create a new channel for this project.")
    assert m and m.group("thisproj") and not m.group("project"), m and m.groupdict()
    for text in ("make a room for it", "open a channel for the project", "open a channel"):
        m = rx.match(text)
        assert m and not m.group("project"), (text, m and m.groupdict())
    assert rx.match("open a channel for fire-shield.").group("project") == "fire-shield"
    assert rx.match("open a channel for this conversation").group("this")
    # the command routes the thread-scoped form to the thread reader
    cmd = _fn_src("_colleague_command")
    assert 'm.group("thisproj")' in cmd and "_open_project_channel_from_thread(channel, thread_ts)" in cmd


def test_no_match_offers_buttons_instead_of_bouncing():
    fn = _fn_src("_open_project_channel")
    assert "_room_picker(channel, thread_ts, query, candidates)" in fn
    assert "I could not match a project" not in fn
    for aid in ("room_open_project", "room_pick_project", "room_create_project"):
        assert f'@app.action("{aid}")' in BOT_SRC, aid
        assert f'("{aid}"' in _fn_src("_room_picker_card"), aid
    picker = _fn_src("_room_picker")
    assert "_room_picker_card(" in picker and "_ps.list_active()" in picker
    assert '_chat.CardSelect("room_pick_project"' in _fn_src("_room_picker_card")
    assert '"picker": True' in picker
    # Create hands the thread to Claude; the row it writes brings the room
    create = _fn_src("_dispatch_room_create")
    assert "add-project" in create and "target=handle_message_async" in create
    assert "Do NOT write pending-channel.json" in create
    # 'this project' resolves from the room, the thread text and the ledger ids named in it
    this = _fn_src("_open_project_channel_from_thread")
    assert "FRONTEND.thread_replies(channel, thread_ts, 60)" in this
    assert "project_ledger" in this and "_room_picker(channel, thread_ts, \"\", candidates)" in this
    # a weak or ambiguous match is a picker, not a wrong room
    res = _fn_src("_resolve_project_candidates")
    assert 'hits[0]["score"] >= hits[1].get("score", 0) + 2' in res
    assert "_ps.suggest_projects(q, conn, limit=3)" in res
    # command replies carry a card; the pick and the staged path share the announcer
    hb = _fn_src("_handle_bot_command")
    assert '_cmd.get("card")' in hb and '"question" if _cmd.get("picker")' in hb
    ann = _fn_src("_announce_room")
    assert 'if res.get("card"):' in ann
    assert '_post_card(channel, dataclasses.replace(res["card"], fallback_text=text)' in ann
    for path in ("CLAUDE.md", "docs/ea/project-store.md"):
        assert "picker" in (REPO / path).read_text(), path


def test_every_room_path_asks_the_rooms_switch_first():
    """Rooms are the collaborator mode's. Each path that makes a room asks
    the switch before anything else and answers ROOMS_OFF_TEXT; the
    switch reads the mode on each call, so a mode change needs no restart."""
    on = _fn_src("_rooms_on")
    assert 'profile_features()["rooms"]' in on
    for fn in ("_open_project_channel", "_open_session_channel", "_room_picker"):
        body = _fn_src(fn)
        doc_end = body.index('"""', body.index('"""') + 3) + 3
        assert body[doc_end:].lstrip().startswith("if not _rooms_on():"), fn
        assert '"rooms_off": True, "text": ROOMS_OFF_TEXT' in body, fn
    assert "if not _rooms_on():" in _fn_src("_dispatch_room_create")
    assert "if not _rooms_on():" in _fn_src("_open_channel_action")
    cmd = _fn_src("_colleague_command")
    assert "if m and not _rooms_on():\n        return {\"ok\": True, \"text\": ROOMS_OFF_TEXT}" in cmd
    assert cmd.index("if m and not _rooms_on():") < cmd.index('if m.group("session") or m.group("this"):')


def test_add_project_names_the_room_step_for_the_mode():
    import contextlib
    import io
    import json
    import ea_config
    cfg = Path(tempfile.mkdtemp(prefix="ea-test-rooms-cfg-")) / "ea-config.json"
    saved = ea_config.CONFIG_FILE
    ea_config.CONFIG_FILE = cfg

    def add(pid: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _add(pid)
        return json.loads(out.getvalue())
    try:
        cfg.write_text(json.dumps({"profile": "assistant", "profile_version": 2}))
        got = add("solo-chat-project")
        assert got["ok"] and got["room"].startswith("none: project rooms come with the collaborator mode")
        cfg.write_text(json.dumps({"profile": "collaborator", "profile_version": 2}))
        assert add("room-project")["room"] == "the bot opens its room after this turn"
    finally:
        ea_config.CONFIG_FILE = saved


def test_suggest_projects_finds_loosely_named_projects():
    _add("flood-shield-intl-delta")
    _add("lab-bench-rebuild")
    top = [s["id"] for s in ps.suggest_projects("delta-nsf-intl-flood")]
    assert top and top[0] == "flood-shield-intl-delta", top
    assert [s["id"] for s in ps.suggest_projects("flood shield")][0] == "flood-shield-intl-delta"
    assert [s["id"] for s in ps.suggest_projects("lab bench")][0] == "lab-bench-rebuild"
    assert ps.suggest_projects("zzzz qqqq wwww") == []
    assert {p["id"] for p in ps.list_active()} >= {"flood-shield-intl-delta", "lab-bench-rebuild"}
    assert "parked-idea" not in {p["id"] for p in ps.list_active()}


def test_pending_channel_is_wired_end_to_end():
    assert '("open_channel", "pending-channel.json")' in BOT_SRC
    assert 'elif kind == "open_channel":\n        _open_channel_action(channel, thread_ts, act)' in BOT_SRC
    fn = _fn_src("_open_channel_action")
    assert "_open_project_channel(channel, thread_ts or \"\", " in fn and "_announce_room(" in fn
    assert "CHANNEL_ACTION_FILES" in _fn_src("_pending_events_poller"), "out-of-band writes must be swept"
    for path in ("CLAUDE.md", "preprocessor/resident_context.py", "docs/ea/project-store.md"):
        assert "pending-channel.json" in (REPO / path).read_text(), path
    core = " ".join((REPO / "CLAUDE.md").read_text().lower().split())
    assert "never say a room is opening" in core



def test_room_context_posts_at_the_channel_top():
    # the opening pack, the reopening pack, a session room's notice and the
    # back-link sit outside every conversation, so the owner's first post
    # in the room starts its own thread
    intro = _fn_src("_post_room_intro")
    assert "thread_ts=FRONTEND.channel_top(room)" in intro
    for fn in ("_open_project_channel",):
        body = _fn_src(fn)
        assert body.count("_post_room_intro(cid, _project_opening_post(pid))") == 2
        assert "FRONTEND.post(channel=cid, text=_project_opening_post" not in body
    assert "_post_room_intro(\n            cid," in _fn_src("_open_session_channel")
    assert "_post_room_intro(room, back, unfurl=False)" in _fn_src("_room_back_link")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
