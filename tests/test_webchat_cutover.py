"""scripts/webchat-cutover.py: the move from shadow mode to the web chat alone.

A store seeded the way the shadow fills it (Zulip's stream ids, thread keys
and message ids; live rows, a week of history rows, a live card, a file
kept only on Zulip), registries that name the streams, and a fake Zulip
primary that holds the whole history. Pins:
  * `plan` changes nothing and says where each channel goes;
  * `apply` refuses while the bot is up (and says why), unless
    --while-running;
  * `apply` takes a labelled snapshot first; brings every message Zulip
    holds over (older ones before the live rows of their thread), with the
    owner's web posts as the owner's; downloads Zulip's files and points
    the rows and their links at the copies; folds the EA, DM, done,
    scheduling and digest streams into main, dm, sched and digests and the
    rest into rooms r<n>, thread keys and message ids kept; carries the
    read marks, a queued push and a judged state to the new numbers;
    keeps the redirect map, which the web front end honours; moves the
    room registry, the room record, the session rows and the card
    registry; rewrites chat.json with the notifier keys;
  * a failed export leaves the store, the registries and chat.json as
    they were;
  * ZulipFrontend.channel_history reads a stream whole, forward, page by
    page; ShadowFrontend.export_history falls back to the thread reads on
    a primary without it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "botcore"))

import chat_notify  # noqa: E402
import ea_channels  # noqa: E402
import owner_rooms  # noqa: E402
import resident_io  # noqa: E402
import slack_sessions  # noqa: E402
from botcore.chat import web_frontend as wf  # noqa: E402
from botcore.chat.memory_frontend import MemoryFrontend  # noqa: E402
from botcore.chat.shadow_frontend import ShadowFrontend  # noqa: E402
from botcore.chat.types import FileRef, InboundMessage  # noqa: E402

SPEC = importlib.util.spec_from_file_location("webchat_cutover",
                                              ROOT / "scripts" / "webchat-cutover.py")
cut = importlib.util.module_from_spec(SPEC)
sys.modules["webchat_cutover"] = cut
SPEC.loader.exec_module(cut)

SITE = "https://zulip.example.test"
PUBLIC = "https://host.example.test:8444/chat"
OWNER, BOT = "9", "50"
NOW = time.time()
DAY = 86400
PLAN = "stream:4:topic:Plan"
OLD = "stream:4:topic:Old%20thread"
CARD_KEY = "stream:6:topic:Send%20the%20draft"
ROOM_KEY = "stream:10:topic:Report"
REPORT = "/user_uploads/2/ab/x/report.pdf"
PHOTO = "/user_uploads/2/cd/y/photo.png"
SESSION = "0f0e0d0c-0b0a-4908-8706-050403020100"


def msg(channel, mid, key, user, text, at, files=()):
    return InboundMessage(channel=channel, msg_id=str(mid), text=text, user_id=user,
                          posted_at=at, thread_key=key, is_self=user == BOT,
                          is_bot=user == BOT, files=list(files),
                          subtype="file_share" if files else "")


class FakeZulip(MemoryFrontend):
    """The Zulip side: the whole history of each stream, and its files."""

    main_channel = "stream:4"
    sched_channel = "stream:5"
    owner_id = OWNER
    bot_user_id = BOT
    history_markdown = True

    def __init__(self, streams: dict) -> None:
        super().__init__()
        self.streams = streams
        self.downloads: list[str] = []
        self.fail_on: str = ""

    def channel_history(self, channel):
        if channel == self.fail_on:
            raise RuntimeError("zulip get_messages failed: the server is gone")
        yield from sorted(self.streams.get(channel, []), key=lambda m: int(m.msg_id))

    def download(self, ref, dest_dir):
        self.downloads.append(ref.id)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / ref.name
        out.write_bytes(b"bytes of " + ref.name.encode())
        return str(out)


def zulip_history() -> dict:
    return {
        "stream:4": [
            msg("stream:4", 40, OLD, OWNER, "an old question", NOW - 20 * DAY),
            msg("stream:4", 41, OLD, BOT, "an old answer", NOW - 20 * DAY + 60),
            msg("stream:4", 45, OLD, OWNER, f"[photo.png]({PHOTO})", NOW - 20 * DAY + 120,
                [FileRef(name="photo.png", url=SITE + PHOTO, id=PHOTO)]),
            msg("stream:4", 50, PLAN, OWNER, "the plan's first line", NOW - 10 * DAY),
            msg("stream:4", 100, PLAN, OWNER, "history row, already there", NOW - 3 * DAY),
            msg("stream:4", 101, PLAN, BOT, "history answer, already there",
                NOW - 3 * DAY + 60),
            msg("stream:4", 300, PLAN, OWNER, "live question", NOW - 3600),
            msg("stream:4", 301, PLAN, BOT, "live answer", NOW - 3500),
            msg("stream:4", 302, PLAN, BOT, "**You (web):** typed on the web", NOW - 3400),
        ],
        "stream:6": [msg("stream:6", 310, CARD_KEY, BOT, "card text in zulip", NOW - 3000)],
        "stream:49": [msg("stream:49", 310, CARD_KEY, BOT, "the same card, filed", NOW - 3000),
                      msg("stream:49", 330, "stream:49:topic:Filed", BOT,
                          ":lock: a filed card\n\n----\n:white_check_mark: Sent", NOW - 5 * DAY)],
        "stream:10": [msg("stream:10", 320, ROOM_KEY, OWNER, f"[report.pdf]({REPORT})",
                          NOW - 2 * DAY, [FileRef(name="report.pdf", url=SITE + REPORT,
                                                  id=REPORT)])],
        "stream:12": [msg("stream:12", 340, "stream:12:topic:Health", BOT, "all well",
                          NOW - DAY)],
    }


def seed_store(priv: Path) -> dict:
    """The store as the shadow left it; the seqs of the rows the test follows."""
    store = wf.WebStore(priv / "webchat.sqlite")
    for cid, name, kind, archived in (
            ("stream:4", "ea", "main", False), ("stream:5", "ea-scheduling", "sched", False),
            ("stream:6", "ea-dm", "dm", False), ("stream:10", "ea-atlas", "room", False),
            ("stream:11", "ea-old-project", "room", True),
            ("stream:12", "ea-health", "room", False),
            ("stream:48", "mail-digest", "room", False),
            ("stream:49", "ea-dm-done", "room", False)):
        store.upsert_channel(cid, name, kind, archived)
    seqs = {}
    for mid, author, text, at in (("100", "owner", "history row, already there", NOW - 3 * DAY),
                                  ("101", "bot", "history answer, already there",
                                   NOW - 3 * DAY + 60)):
        seqs[mid] = store.add_message("stream:4", mid=mid, thread_key=PLAN, author=author,
                                      kind="message", text=text, posted_at=at,
                                      history=True)["seq"]
    for mid, author, text, at in (("300", "owner", "live question", NOW - 3600),
                                  ("301", "bot", "live answer", NOW - 3500)):
        seqs[mid] = store.add_message("stream:4", mid=mid, thread_key=PLAN, author=author,
                                      kind="message", text=text, posted_at=at)["seq"]
    card = {"title": ":lock: Send this draft?", "kind": "send", "block_id": "blk",
            "actions": [{"id": "approve_send", "label": "Send",
                         "value": {"token": "t-1", "ea_channel": "stream:4",
                                   "thread_ts": PLAN}}]}
    seqs["310"] = store.add_message("stream:6", mid="310", thread_key=CARD_KEY, author="bot",
                                    kind="card", text=":lock: Send this draft?", card=card,
                                    posted_at=NOW - 3000)["seq"]
    file = {"id": "", "name": "report.pdf", "url": SITE + REPORT,
            "mimetype": "application/pdf", "size": 0}
    seqs["320"] = store.add_message("stream:10", mid="320", thread_key=ROOM_KEY,
                                    author="owner", kind="file",
                                    text=f"[report.pdf]({REPORT})", file=file,
                                    posted_at=NOW - 2 * DAY)["seq"]
    store.mark_read("stream:4")
    with store._conn:
        store._conn.execute("INSERT INTO thread_state(channel, thread_key, state, by, reason, "
                            "at, judged_seq) VALUES ('stream:4', ?, 'done', 'model', 'answered',"
                            " ?, ?)", (PLAN, NOW, seqs["301"]))
        store._conn.execute("INSERT INTO push_queue(tag, due_at, payload, urgency, created_at,"
                            " channel, seq) VALUES ('stream:6/x', ?, '{}', 'high', ?, "
                            "'stream:6', ?)", (NOW + 60, NOW, seqs["310"]))
    store.close()
    return seqs


@pytest.fixture
def install(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "state").mkdir()
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    monkeypatch.setattr(ea_channels, "REGISTRY", ea / "config" / "ea-channels.json")
    monkeypatch.setattr(slack_sessions, "REGISTRY", ea / "state" / "slack-sessions.json")
    monkeypatch.setattr(cut, "bot_up", lambda install: "")
    chat = {"frontend": "zulip", "shadow": "web",
            "zulip": {"site": SITE, "bot_email": "bot@example.test", "owner_user_id": OWNER,
                      "stream": "ea", "sched_stream": "ea-scheduling", "dm_stream": "ea-dm",
                      "notify": {"personal": {"stream": "ea"},
                                 "calendar": {"stream": "ea-scheduling"},
                                 "health": {"stream": "ea-health"},
                                 "mail-digest": {"stream": "mail-digest"}}},
            "web": {"public_url": PUBLIC, "tailscale_login": "owner@example.test"}}
    (priv / "chat.json").write_text(json.dumps(chat))
    (priv / "chat.json").chmod(0o600)
    seqs = seed_store(priv)
    (priv / "webchat-owner-ids.json").write_text(json.dumps({"v": 1, "ids": [["stream:4", "302"]]}))
    (priv / "webchat-owner-ids.json").chmod(0o600)
    (priv / "webchat-pair.token").write_text("t" * 43 + "\n")
    (priv / "webchat-pair.token").chmod(0o600)
    ea_channels.register("stream:10", "atlas", "ea-atlas",
                         url=SITE + "/#narrow/channel/10-ea-atlas")
    ea_channels.register("stream:11", "old-project", "ea-old-project")
    ea_channels.register("stream:77", "never-mirrored", "ea-never-mirrored")
    owner_rooms.start({"stream:10": {"name": "ea-atlas"}, "stream:11": {"name": "ea-old"}})
    slack_sessions._mutate(lambda rows: rows.update({
        PLAN: {"session_id": SESSION, "meta": {"channel": "stream:4"}},
        ROOM_KEY: {"session_id": SESSION, "meta": {"channel": "stream:10"}}}) or True)
    cards = {"stream:6|310": {"kind": "send", "channel": "stream:6", "msg_id": "310",
                              "card_thread": CARD_KEY, "status": "live", "code": "K7Q2",
                              "thread_ts": PLAN, "ea_channel": "stream:4",
                              "actions": {"approve_send": {"value": {"ea_channel": "stream:4",
                                                                     "thread_ts": PLAN}}}}}
    (priv / "chat-cards.json").write_text(json.dumps(cards))
    (priv / "chat-cards.json").chmod(0o600)
    return {"priv": priv, "ea": ea, "seqs": seqs, "zulip": FakeZulip(zulip_history())}


def _tree(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p)] = (stat.S_IMODE(p.stat().st_mode),
                               hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _main(install, *args):
    return cut.main(list(args), primary_factory=lambda inst: install["zulip"],
                    priv=install["priv"])


def _rows(priv: Path) -> list[dict]:
    with sqlite3.connect(priv / "webchat.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY seq")]


def test_digest_names_agree_with_chat_notify():
    assert cut.DIGEST_ID == chat_notify.WEB_DIGEST_ID
    assert cut.DIGEST_NAME == chat_notify.WEB_DIGEST_NAME
    assert cut.DIGEST_KIND == chat_notify.WEB_DIGEST_KIND
    assert cut.DIGEST_STREAM == chat_notify.DIGEST_STREAM
    assert set(cut.DIGEST_KEYS) == set(chat_notify.DIGEST_KEYS)


def test_plan_writes_nothing_and_says_where_each_channel_goes(install, capsys):
    priv, ea = install["priv"], install["ea"]
    before = _tree(priv, ea)
    assert _main(install, "plan") == 0
    assert _tree(priv, ea) == before
    out = capsys.readouterr().out
    assert "nothing is written" in out
    for old, new in (("stream:4", "main"), ("stream:5", "sched"), ("stream:6", "dm"),
                     ("stream:10", "r1"), ("stream:11", "r2"), ("stream:12", "r3"),
                     ("stream:48", "digests"), ("stream:49", "dm")):
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(old + " "))
        assert f"-> {new} " in line, line
    assert "2 room(s) move to their r<n> id; 1 stay under a Zulip id (not mirrored): " \
        "stream:77" in out
    assert "health -> r3" in out and "calendar -> sched" in out and "personal" not in out
    assert "1 message(s) in the store link a file Zulip holds" in out
    assert "1 live" in out
    # with --zulip it counts what Zulip holds, still writing nothing
    assert _main(install, "plan", "--zulip") == 0
    assert _tree(priv, ea) == before
    out = capsys.readouterr().out
    assert "9 in Zulip, 1 file(s)" in out


def test_apply_refuses_while_the_bot_is_up(install, monkeypatch, capsys):
    monkeypatch.setattr(cut, "bot_up", lambda inst: "ea-chat-bot.service is active")
    before = _tree(install["priv"], install["ea"])
    assert _main(install, "apply") == 2
    err = capsys.readouterr().err
    assert "the bot is up (ea-chat-bot.service is active)" in err
    assert "writes into it" in err and "card registry" in err and "--while-running" in err
    assert _tree(install["priv"], install["ea"]) == before


def test_apply_moves_the_history_the_channels_and_the_registries(install, capsys):
    priv, seqs = install["priv"], install["seqs"]
    assert _main(install, "apply") == 0
    out = capsys.readouterr().out
    assert "Moved to the web chat alone." in out and "sudo tailscale serve --https=8443 off" in out

    # the snapshot came first, with the registries beside it
    snap = priv / "backups" / "webchat" / f"{time.strftime('%Y-%m-%d')}-cutover"
    assert (snap / "MANIFEST.tsv").is_file() and (snap / "chat.json").is_file()
    assert json.loads((snap / "chat.json").read_text())["frontend"] == "zulip"
    assert "stream:10" in json.loads((snap / "registries" / "ea-channels.json").read_text())
    assert (snap / "registries" / "slack-sessions.json").is_file()
    with sqlite3.connect(snap / "webchat.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM channels WHERE id LIKE 'stream:%'").fetchone()[0] == 8

    # the channels
    with sqlite3.connect(priv / "webchat.sqlite") as conn:
        chans = {r[0]: r[1:] for r in conn.execute("SELECT id, name, kind, archived FROM channels")}
    assert chans == {"main": ("ea", "main", 0), "dm": ("dm", "dm", 0),
                     "sched": ("Scheduling", "sched", 0), "r1": ("ea-atlas", "room", 0),
                     "r2": ("ea-old-project", "room", 1), "r3": ("ea-health", "room", 0),
                     "digests": ("Mail digests", "digest", 0)}

    rows = _rows(priv)
    by_id = {(r["channel"], r["id"]): r for r in rows}
    assert not [r for r in rows if r["channel"].startswith("stream:")]
    # every message Zulip holds came over once, in time order, keys kept
    plan = [r["id"] for r in rows if r["thread_key"] == PLAN]
    assert plan == ["50", "100", "101", "300", "301", "302"]
    assert [r["id"] for r in rows if r["thread_key"] == OLD] == ["40", "41", "45"]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert [r["posted_at"] for r in rows] == sorted(r["posted_at"] for r in rows)
    assert by_id[("main", "50")]["history"] == 1 and by_id[("main", "300")]["history"] == 0
    # the owner's web post, posted into Zulip as the bot, is the owner's
    assert by_id[("main", "302")]["author"] == "owner"
    assert by_id[("main", "302")]["text"] == "typed on the web"
    assert by_id[("main", "40")]["author"] == "owner" and by_id[("main", "41")]["author"] == "bot"
    # the done stream folded into the DM; the card it holds twice stays the live row
    assert [r for r in rows if r["id"] == "310"] == [by_id[("dm", "310")]]
    assert by_id[("dm", "310")]["kind"] == "card"
    assert by_id[("dm", "330")]["text"].endswith(":white_check_mark: Sent")
    assert ("r3", "340") in by_id

    # the files: downloaded, kept here, the rows and the links point at them
    assert sorted(install["zulip"].downloads) == [REPORT, PHOTO]
    report = json.loads(by_id[("r1", "320")]["file"])
    url = f"/chat/api/files/{urllib.parse.quote(REPORT, safe='')}"
    assert report["id"] == REPORT and report["url"] == url and report["size"] > 0
    assert by_id[("r1", "320")]["text"] == f"[report.pdf]({url})"
    photo = json.loads(by_id[("main", "45")]["file"])
    assert photo["id"] == PHOTO and by_id[("main", "45")]["text"].startswith("[photo.png](/chat/")
    store = wf.WebStore(priv / "webchat.sqlite")
    try:
        kept = store.file(REPORT)
        assert Path(kept["path"]).read_bytes() == b"bytes of report.pdf"
        assert Path(kept["path"]).parent == priv / wf.FILES_DIR
        # the numbers that name a message moved with it
        new = {r["id"]: r["seq"] for r in rows}
        assert store._one("SELECT seq FROM reads WHERE channel='main'")["seq"] == new["301"]
        assert store._one("SELECT judged_seq FROM thread_state")["judged_seq"] == new["301"]
        assert store._one("SELECT channel, thread_key FROM thread_state")[0] == "main"
        q = store._one("SELECT tag, channel, seq FROM push_queue")
        assert (q["tag"], q["channel"], q["seq"]) == ("dm/x", "dm", new["310"])
        # the main channel shows the bot's live answer read, nothing new unread
        assert next(c for c in store.channels() if c["id"] == "main")["unread"] == 0
        # the card points at the new channel
        card = json.loads(by_id[("dm", "310")]["card"])
        assert card["actions"][0]["value"] == {"token": "t-1", "ea_channel": "main",
                                               "thread_ts": PLAN}
        # the event log holds one channel event per new channel
        events = store._all("SELECT type, channel FROM events")
        assert {e["type"] for e in events} == {"channel"}
        assert sorted(e["channel"] for e in events) == sorted(
            ["main", "sched", "dm", "r1", "r2", "r3", "digests"])
        assert store.redirects == {"stream:4": "main", "stream:5": "sched", "stream:6": "dm",
                                   "stream:10": "r1", "stream:11": "r2", "stream:12": "r3",
                                   "stream:48": "digests", "stream:49": "dm"}
    finally:
        store.close()

    # the registries
    reg = ea_channels.load()
    assert set(reg) == {"r1", "r2", "stream:77"}
    assert reg["r1"]["project"] == "atlas" and reg["r1"]["url"] == f"{PUBLIC}/#/r1"
    assert set(owner_rooms.load()) == {"r1", "r2"}
    sessions = slack_sessions._load()
    assert sessions[PLAN]["meta"]["channel"] == "main"
    assert sessions[ROOM_KEY]["meta"]["channel"] == "r1"
    cards = json.loads((priv / "chat-cards.json").read_text())
    assert set(cards) == {"dm|310"}
    assert cards["dm|310"]["ea_channel"] == "main" and cards["dm|310"]["card_thread"] == CARD_KEY
    assert cards["dm|310"]["actions"]["approve_send"]["value"]["ea_channel"] == "main"
    assert stat.S_IMODE((priv / "chat-cards.json").stat().st_mode) == 0o600

    # chat.json
    chat = json.loads((priv / "chat.json").read_text())
    assert chat["frontend"] == "web" and "shadow" not in chat
    assert chat["zulip"]["site"] == SITE
    assert chat["web"]["notify"] == {"calendar": {"channel": "sched"},
                                     "health": {"channel": "r3"}}
    assert stat.S_IMODE((priv / "chat.json").stat().st_mode) == 0o600

    # a second apply finds nothing to move
    assert _main(install, "apply") == 1
    assert "already runs the web chat alone" in capsys.readouterr().err


def test_the_web_front_end_honours_the_redirect_map(install):
    assert _main(install, "apply") == 0
    fe = wf.WebFrontend({"public_url": PUBLIC, "port": 0},
                        store_path=install["priv"] / "webchat.sqlite", writer_only=True)
    fe.start()
    try:
        ref = fe.post("stream:4", "a reply in an old thread", thread_ts=PLAN)
        assert (ref.channel, ref.thread_key) == ("main", PLAN)
        assert [m.msg_id for m in fe.thread_replies("stream:4", PLAN)][-1] == ref.id
        assert fe.room_url("stream:10") == f"{PUBLIC}/#/r1"
        assert fe.is_dm("stream:6") and fe.channel_info("stream:10").name == "ea-atlas"
        assert fe.message_posted_at(type(ref)("stream:6", "310", CARD_KEY)) is not None
        assert fe.replace_card_with_status(type(ref)("stream:6", "310", CARD_KEY), "Sent.")
        assert fe.store.message("dm", "310")["kind"] == "status"
        fe.archive("stream:12")
        assert fe.store.channel("r3")["archived"]
        assert fe.store.canonical("main") == "main" and fe.store.canonical("") == ""
    finally:
        fe.store.close()


def test_old_links_reach_the_new_channel_over_http(install, tmp_path):
    assert _main(install, "apply") == 0
    fe = wf.WebFrontend({"public_url": PUBLIC, "port": 0},
                        store_path=install["priv"] / "webchat.sqlite",
                        webui_dir=tmp_path / "webui", trusted_config=True)
    fe.poll_s = fe.pair_delay_s = 0.02
    fe.start()
    try:
        import urllib.request
        path = (f"/api/channels/{urllib.parse.quote('stream:4', safe='')}/threads/"
                f"{urllib.parse.quote(PLAN, safe='')}")
        req = urllib.request.Request(fe.url + path, headers={"Cookie": "ea_web=" + "t" * 43})
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        assert [m["channel"] for m in rows] == ["main"] * 6
        assert [m["id"] for m in rows][:2] == ["50", "100"]
    finally:
        fe.stop()


def test_a_failed_export_leaves_everything_as_it_was(install, capsys):
    install["zulip"].fail_on = "stream:10"
    priv, ea = install["priv"], install["ea"]
    watched = [priv / "webchat.sqlite", priv / "chat.json", priv / "chat-cards.json",
               priv / "owner-rooms.json", ea / "config" / "ea-channels.json",
               ea / "state" / "slack-sessions.json"]
    before = {p: p.read_bytes() for p in watched}
    assert _main(install, "apply") == 1
    err = capsys.readouterr().err
    assert "the move stopped (RuntimeError" in err and "the server is gone" in err
    assert "are as they were" in err
    assert {p: p.read_bytes() for p in watched} == before
    assert not (priv / "webchat.sqlite.cutover").exists()
    # the snapshot it took first stays
    assert (priv / "backups" / "webchat" / f"{time.strftime('%Y-%m-%d')}-cutover").is_dir()


def test_teardown_names_every_piece_of_zulip(capsys):
    assert cut.main(["teardown"]) == 0
    out = capsys.readouterr().out
    for step in ("deploy/zulip/backup.sh", "tailscale serve --https=8443 off",
                 "tag:ea-host:8443", "deploy/zulip/compose.sh down -v", "docker image rm",
                 "crontab -e", "zulip.env", "ZULIP_API_KEY", "secrets.env"):
        assert step in out, step


def test_zulip_channel_history_reads_forward_page_by_page(tmp_path):
    from botcore.chat.zulip_frontend import ChatError, ZulipFrontend

    def zmsg(mid, topic, sender):
        return {"id": mid, "type": "stream", "stream_id": 4, "subject": topic,
                "sender_id": sender, "sender_email": "x@example.test",
                "content": f"message {mid}", "timestamp": 1000 + mid}

    class Client:
        def __init__(self):
            self.msgs = [zmsg(i, "A" if i % 2 else "B", 9) for i in range(1, 11)]
            self.asked = []
            self.refuse = False

        def get_stream_id(self, name):
            return {"result": "success", "stream_id": {"ea": 4, "ea-dm": 6}.get(name, 99)}

        def get_messages(self, req):
            self.asked.append(dict(req))
            if self.refuse:
                return {"result": "error", "msg": "no"}
            start = 0 if req["anchor"] == "oldest" else req["anchor"]
            page = [m for m in self.msgs if m["id"] >= start][:req["num_after"] + 1]
            return {"result": "success", "messages": page,
                    "found_newest": page[-1]["id"] == self.msgs[-1]["id"] if page else True}

    client = Client()
    fe = ZulipFrontend({"site": SITE, "bot_email": "bot@example.test", "owner_user_id": OWNER},
                       api_key="k", private_dir=tmp_path / "p", client=client)
    fe._sleep = lambda s: None
    got = list(fe.channel_history("stream:4", page=3))
    # each page holds the anchor and `page` after it; the anchor is not read twice
    assert [m.msg_id for m in got] == [str(i) for i in range(1, 11)]
    assert [m.is_reply for m in got] == [False, False] + [True] * 8
    assert [m.thread_key for m in got[:2]] == ["stream:4:topic:A", "stream:4:topic:B"]
    assert [a["anchor"] for a in client.asked] == ["oldest", 4, 7]
    assert all(a["num_before"] == 0 and a["narrow"] == [{"operator": "channel", "operand": 4}]
               for a in client.asked)
    client.refuse = True
    with pytest.raises(ChatError, match="get_messages failed"):
        list(fe.channel_history("stream:4"))


def test_export_history_falls_back_to_the_thread_reads():
    class Plain(MemoryFrontend):
        owner_id = OWNER
        bot_user_id = BOT

    primary = Plain()
    primary.history = [
        msg("C1", "1", "t1", OWNER, "root", 10.0),
        msg("C1", "2", "t1", BOT, "reply", 11.0),
        msg("C1", "3", "t3", BOT, "another root", 12.0),
    ]
    primary.history[1].is_reply = True
    shadow = MemoryFrontend()
    shadow.inbound = None
    comp = ShadowFrontend(primary, shadow)
    comp._sleep = lambda s: None
    out = list(comp.export_history(["C1"]))
    assert [(r.id, r.thread_key, a) for r, a, _t, _f in out] == [
        ("1", "t1", "owner"), ("2", "t1", "bot"), ("3", "t3", "bot")]
