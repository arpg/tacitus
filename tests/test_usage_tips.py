"""Weekly usage tips (preprocessor/usage_tips.py): the catalogue speaks
plain words, every detector reads "not yet" on empty stores and "in use"
on a store that shows the habit, the cadence holds (the first tip after
the first week, one a week, one open at a time, not now for 14 days,
never for good), the owner's words act on the state and the preferences
store, and the bot and the briefing cron are wired to it."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

_EA = Path(tempfile.mkdtemp(prefix="ea-test-usage-tips-"))
for _sub in ("config", "state"):
    (_EA / _sub).mkdir()
os.environ["EA_DIR"] = str(_EA)

import ledger  # noqa: E402
import preferences as prefs  # noqa: E402
import usage_tips as ut  # noqa: E402
from chat.web_frontend import WebStore  # noqa: E402

CLI = [sys.executable, str(REPO / "preprocessor" / "usage_tips.py")]
TZ = ZoneInfo("America/Denver")
BANNED = re.compile(r"\b(channel|thread|session|context|ledger|lane)s?\b", re.I)
EM_DASH = chr(0x2014)
WEB_ON = {"frontend": "zulip", "shadow": "web", "web": {"relay": {"url": "https://relay.example"}}}
# Facts for a weekday morning after the briefing, nothing in the way.
CLEAR = {"enabled": True, "chat": True, "briefing_today": True, "onboarding_today": False,
         "quiet": False}


def at(day: str, hm: str = "08:30") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=TZ)


@pytest.fixture
def ea(tmp_path) -> Path:
    for sub in ("config", "state", "uploads"):
        (tmp_path / "ea" / sub).mkdir(parents=True)
    (tmp_path / "private").mkdir(mode=0o700)
    return tmp_path / "ea"


def usage(ea: Path, chat=None, local=True) -> ut.Usage:
    return ut.Usage(ea, private_dir=ea.parent / "private",
                    chat_cfg=WEB_ON if chat is None else chat, local_model=local)


def web_store(ea: Path) -> Path:
    path = ea.parent / "private" / "webchat.sqlite"
    WebStore(path)  # the real schema, empty
    return path


def sql(path: Path, *statements):
    conn = sqlite3.connect(path)
    for stmt, args in statements:
        conn.execute(stmt, args)
    conn.commit()
    conn.close()


def owner_says(path: Path, text: str, thread: str = "t1", n: int = 1):
    sql(path, ("INSERT INTO messages(channel, id, thread_key, author, kind, text, posted_at) "
               "VALUES ('main', ?, ?, 'owner', 'message', ?, 0)", (f"w{thread}-{n}", thread, text)))


def first_week_done(ea: Path, day: str = "2026-11-02"):
    ut._update_state(lambda s: s.__setitem__("first_briefing", day), ea)


def record_post(calls: list):
    def send(text: str) -> bool:
        calls.append(text)
        return True
    return send


def no_push(_text):
    return {"web": "off"}


# ── the catalogue ─────────────────────────────────────────────────────

def test_the_catalogue_is_in_the_order_a_new_person_meets_things():
    ids = [t.id for t in ut.CATALOGUE]
    assert ids == ["reply-draft", "tomorrow", "snooze", "standing-rule", "phone-alerts",
                   "away-from-home", "meeting-time", "poll", "attach-file", "needs-you",
                   "project-room", "model-settings"]
    assert len(set(ids)) == len(ids)


def test_every_string_a_person_reads_is_plain():
    strings = list(ut.REPLIES.values()) + [ut.REPLY_HINT]
    for tip in ut.CATALOGUE:
        strings += [tip.pitch, tip.example, tip.why, tip.needs, ut.post_text(tip)]
        for f in (tip.pitch, tip.example):
            assert "\n" not in f and len(f) <= 80, tip.id
    for s in strings:
        assert not BANNED.search(s), s
        assert EM_DASH not in s and " -- " not in s, s


def test_the_post_is_the_pitch_the_line_to_try_and_the_three_words():
    tip = ut.BY_ID["tomorrow"]
    lines = ut.post_text(tip).splitlines()
    assert lines[0] == ":bulb: Ask me what your day looks like."
    assert lines[1] == "Try: *What is on tomorrow?*"
    assert lines[2] == tip.why
    assert all(w in lines[3] for w in ("*more*", "*not now*", "*never*"))
    assert len(lines) == 4


# ── the detectors ─────────────────────────────────────────────────────

def test_every_detector_says_not_yet_with_no_stores_at_all(ea):
    u = usage(ea)
    for tip in ut.CATALOGUE:
        assert tip.uses(u) is False, tip.id


def test_every_detector_says_not_yet_on_empty_stores(ea):
    web_store(ea)
    db = ea / "state" / "obligations-ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript(ledger.SCHEMA)
    conn.close()
    (ea / "config" / "preferences.json").write_text(json.dumps({"version": 1, "rules": []}))
    (ea / "state" / "snoozed-messages.json").write_text("[]")
    (ea / "state" / "draft-events.jsonl").write_text("")
    (ea / "state" / "slack-sessions.json").write_text("{}")
    (ea / "config" / "ea-channels.json").write_text("{}")
    u = usage(ea)
    for tip in ut.CATALOGUE:
        assert tip.uses(u) is False, tip.id
        assert ut.tip_status(tip, ut.load_state(ea), u, at("2026-11-09")) in (
            "queued", f"waits for {tip.needs}"), tip.id


@pytest.mark.parametrize("tip_id,words", [
    ("reply-draft", "Can you draft a reply to Sam?"),
    ("tomorrow", "what is on tomorrow"),
    ("snooze", "snooze that until Monday"),
    ("meeting-time", "find a time with Alex"),
    ("poll", "fill the doodle please"),
])
def test_the_owners_own_words_show_a_habit(ea, tip_id, words):
    path = web_store(ea)
    owner_says(path, words)
    assert ut.BY_ID[tip_id].uses(usage(ea)) is True


def test_the_web_store_shows_the_phone_rooms_files_and_mark_done(ea):
    path = web_store(ea)
    sql(path,
        ("INSERT INTO push_subscriptions(endpoint, keys, created_at) VALUES ('https://x', '{}', 0)", ()),
        ("INSERT INTO channels(id, name, kind) VALUES ('r1', 'ea-fundraiser', 'room')", ()),
        ("INSERT INTO messages(channel, id, thread_key, author, kind, text, file, posted_at) "
         "VALUES ('main', 'w9', 't9', 'owner', 'file', '', 'f1', 0)", ()),
        ("INSERT INTO thread_state(channel, thread_key, state, by, reason, at) "
         "VALUES ('main', 't9', 'done', 'owner', 'You marked it done.', 0)", ()))
    u = usage(ea)
    for tip_id in ("phone-alerts", "project-room", "attach-file", "needs-you"):
        assert ut.BY_ID[tip_id].uses(u) is True, tip_id


def test_an_event_card_acted_on_shows_the_meeting_habit(ea):
    path = web_store(ea)
    sql(path, ("INSERT INTO messages(channel, id, thread_key, author, kind, text, card, posted_at) "
               "VALUES ('dm', 'w5', 't5', 'bot', 'status', 'Created.', ?, 0)",
               (json.dumps({"kind": "event", "title": "Create this event?"}),)))
    assert ut.BY_ID["meeting-time"].uses(usage(ea)) is True


def test_the_resident_files_show_drafts_snoozes_rules_and_uploads(ea):
    (ea / "state" / "draft-events.jsonl").write_text(
        json.dumps({"action": "created", "draft_id": "d1", "run_id": "cron-morning-briefing-1"})
        + "\n")
    assert ut.BY_ID["reply-draft"].uses(usage(ea)) is False   # a cron lane's draft
    with (ea / "state" / "draft-events.jsonl").open("a") as fh:
        fh.write(json.dumps({"action": "created", "draft_id": "d2", "run_id": "19a2b3c4d5e-0a1b2c"})
                 + "\n")
    assert ut.BY_ID["reply-draft"].uses(usage(ea)) is True

    (ea / "state" / "snoozed-messages.json").write_text(json.dumps([{"message_id": "m1"}]))
    assert ut.BY_ID["snooze"].uses(usage(ea)) is True

    store = prefs.load(ea / "config" / "preferences.json")
    prefs.add_rule(store, kind="avoid", scopes=["briefing"], text="Onboarding answer",
                   source="chat")["source"] = "onboarding"
    prefs.save(store, ea / "config" / "preferences.json")
    assert ut.BY_ID["standing-rule"].uses(usage(ea)) is False  # an onboarding answer
    prefs.update(lambda d: prefs.add_rule(d, kind="avoid", scopes=["briefing"],
                                          text="Never show me parking mail", source="chat"),
                 ea / "config" / "preferences.json")
    assert ut.BY_ID["standing-rule"].uses(usage(ea)) is True

    (ea / "uploads" / "form.pdf").write_bytes(b"%PDF")
    assert ut.BY_ID["attach-file"].uses(usage(ea)) is True


def test_a_snoozed_ledger_row_shows_the_snooze_habit(ea):
    conn = sqlite3.connect(ea / "state" / "obligations-ledger.db")
    conn.executescript(ledger.SCHEMA)
    conn.execute("INSERT INTO ledger(id, type, status, created_at, updated_at) "
                 "VALUES ('r1', 'task', 'snoozed', 'x', 'x')")
    conn.commit()
    conn.close()
    assert ut.BY_ID["snooze"].uses(usage(ea)) is True


def test_a_paired_phone_and_a_saved_routing_table_show_use(ea):
    priv = ea.parent / "private"
    (priv / "webchat-relay.json").write_text(json.dumps(
        {"v": 1, "devices": [{"id": "d1", "pending": True}]}))
    assert ut.BY_ID["away-from-home"].uses(usage(ea)) is False   # still waiting for approval
    (priv / "webchat-relay.json").write_text(json.dumps({"v": 1, "devices": [{"id": "d1"}]}))
    assert ut.BY_ID["away-from-home"].uses(usage(ea)) is True
    assert ut.BY_ID["model-settings"].uses(usage(ea)) is False
    (priv / "routing.json").write_text("{}")
    assert ut.BY_ID["model-settings"].uses(usage(ea)) is True


def test_tips_wait_until_they_fit(ea):
    path = web_store(ea)
    zulip_only = {"frontend": "zulip", "shadow": "", "web": {}}
    for tip_id in ("phone-alerts", "away-from-home", "needs-you", "model-settings"):
        assert ut.BY_ID[tip_id].ready(usage(ea, chat=zulip_only)) is False, tip_id
    assert ut.BY_ID["model-settings"].ready(usage(ea, local=False)) is False
    assert ut.BY_ID["model-settings"].ready(usage(ea)) is True
    room = ut.BY_ID["project-room"]
    for i in range(19):
        owner_says(path, f"question {i}", thread=f"t{i}")
    assert room.ready(usage(ea)) is False
    owner_says(path, "question 19", thread="t19")
    assert room.ready(usage(ea)) is True


# ── the cadence ───────────────────────────────────────────────────────

def blocked(state, now, first="2026-11-02", stopped=False, **facts):
    return ut.blocked(state, now, facts={**CLEAR, **facts}, is_stopped=stopped, first=first)


def test_the_first_tip_waits_seven_days_after_the_first_briefing():
    state = ut._empty_state()
    assert "no briefing" in blocked(state, at("2026-11-09"), first="")
    assert "first tip comes on 2026-11-09" in blocked(state, at("2026-11-06"))
    assert blocked(state, at("2026-11-09")) == ""


def test_only_a_weekday_morning_after_the_briefing_without_an_onboarding_question():
    state = ut._empty_state()
    assert "weekdays" in blocked(state, at("2026-11-14"))               # a Saturday
    assert "between 07:00 and 12:00" in blocked(state, at("2026-11-09", "06:00"))
    assert "between 07:00 and 12:00" in blocked(state, at("2026-11-09", "12:00"))
    assert "briefing has not posted" in blocked(state, at("2026-11-09"), briefing_today=False)
    assert "onboarding question" in blocked(state, at("2026-11-09"), onboarding_today=True)
    assert "quiet hours" in blocked(state, at("2026-11-09"), quiet=True)
    assert "enabled is false" in blocked(state, at("2026-11-09"), enabled=False)
    assert "no chat" in blocked(state, at("2026-11-09"), chat=False)
    assert "said never" in blocked(state, at("2026-11-09"), stopped=True)


def test_one_tip_a_week():
    state = ut._empty_state()
    state["shown"]["reply-draft"] = at("2026-11-09").isoformat()
    assert "one tip a week" in blocked(state, at("2026-11-13"))
    assert "one tip a week" in blocked(state, at("2026-11-16", "08:29"))
    assert blocked(state, at("2026-11-16")) == ""


def test_force_skips_the_cadence_but_not_never():
    state = ut._empty_state()
    facts = {**CLEAR, "briefing_today": False}
    assert ut.blocked(state, at("2026-11-14", "23:00"), facts=facts, is_stopped=False,
                      first="", force=True) == ""
    assert ut.blocked(state, at("2026-11-14"), facts=facts, is_stopped=True,
                      first="", force=True) != ""


def test_post_posts_one_tip_and_opens_it(ea):
    first_week_done(ea)
    calls, pushes = [], []
    res = ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
                  notify=record_post(calls), push=lambda t: pushes.append(t) or {"sent": 1})
    assert res["posted"] and res["tip"] == "reply-draft"
    assert calls == [ut.post_text(ut.BY_ID["reply-draft"])]
    assert pushes == [ut.BY_ID["reply-draft"].pitch]
    state = ut.load_state(ea)
    assert state["open"] == "reply-draft" and "reply-draft" in state["shown"]
    again = ut.post(ea_dir=ea, now=at("2026-11-10"), usage=usage(ea), facts=CLEAR,
                    notify=record_post(calls), push=no_push)
    assert not again["posted"] and "one tip a week" in again["why"] and len(calls) == 1


def test_a_new_tip_closes_the_one_left_unanswered(ea):
    first_week_done(ea)
    calls = []
    ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
            notify=record_post(calls), push=no_push)
    res = ut.post(ea_dir=ea, now=at("2026-11-16"), usage=usage(ea), facts=CLEAR,
                  notify=record_post(calls), push=no_push)
    assert res["tip"] == "tomorrow"
    state = ut.load_state(ea)
    assert state["open"] == "tomorrow"
    assert state["dismissed"]["reply-draft"]["why"] == "no answer"
    opened = [t for t in ut.CATALOGUE if ut.tip_status(t, state, usage(ea), at("2026-11-16")) == "open"]
    assert [t.id for t in opened] == ["tomorrow"]


def test_a_tip_for_something_in_use_is_skipped(ea):
    first_week_done(ea)
    owner_says(web_store(ea), "please draft a reply to the gym owner")
    res = ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
                  notify=record_post([]), push=no_push)
    assert res["tip"] == "tomorrow"


def test_a_failed_chat_post_records_nothing(ea):
    first_week_done(ea)
    res = ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
                  notify=lambda t: False, push=no_push)
    assert not res["posted"] and ut.load_state(ea)["shown"] == {}


def test_post_records_the_first_briefing_day(ea):
    (ea / "state" / "agent-post-success.json").write_text(json.dumps(
        {"morning-briefing": {"date": "2026-11-04", "validation": "ok"}}))
    res = ut.post(ea_dir=ea, now=at("2026-11-04"), usage=usage(ea), facts=CLEAR,
                  notify=record_post([]), push=no_push)
    assert not res["posted"] and "2026-11-11" in res["why"]
    assert ut.load_state(ea)["first_briefing"] == "2026-11-04"
    (ea / "state" / "onboarding.json").write_text(json.dumps(
        {"asked": {"vip-senders": "2026-10-28T04:00:00"}}))
    assert ut.first_briefing(ut.load_state(ea), ea) == "2026-11-04"   # recorded, kept


def test_the_briefing_marker_and_the_onboarding_question_are_read(ea):
    (ea / "state" / "agent-post-success.json").write_text(json.dumps(
        {"morning-briefing": {"date": "2026-11-09"}}))
    assert ut.briefing_posted_on("2026-11-09", ea) is True
    assert ut.briefing_posted_on("2026-11-10", ea) is False
    (ea / "state" / "onboarding.json").write_text(json.dumps(
        {"asked": {"vip-senders": "2026-11-09T04:00:00"}}))
    assert ut.onboarding_asked_on("2026-11-09", ea) is True
    assert ut.onboarding_asked_on("2026-11-10", ea) is False
    (ea / "state" / "onboarding.json").write_text(json.dumps(
        {"asked": {"vip-senders": "2026-11-09T04:00:00"},
         "answered": {"vip-senders": {"at": "2026-11-09T07:40:00"}}}))
    assert ut.onboarding_asked_on("2026-11-09", ea) is True    # answered: still that morning's
    assert ut.first_briefing(ut._empty_state(), ea) == "2026-11-09"


# ── the owner's words ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,word", [
    ("more", "more"), ("More!", "more"), ("another one", "more"),
    ("not now", "not now"), ("Not now.", "not now"), ("later", "not now"),
    ("never", "never"), ("Stop", "never"), ("no more tips", "never"),
    ("more about snoozing please", None), ("what is on tomorrow?", None), ("", None),
])
def test_the_tip_words(text, word):
    assert ut.reply_word(text) == word


def test_a_thread_is_a_tips_only_when_the_bot_opened_it_with_one():
    tip = ut.post_text(ut.BY_ID["snooze"])
    zulip_text = tip.replace("*", "**")
    assert ut.tip_in_thread([(tip, True), ("never", False)]) == "snooze"
    assert ut.tip_in_thread([(zulip_text, True), ("more", False)]) == "snooze"
    assert ut.tip_in_thread([(tip, False), ("never", False)]) is None       # not the bot's
    assert ut.tip_in_thread([("Draft ready.", True), (tip, True)]) is None  # not the first
    nxt = ut.post_text(ut.BY_ID["poll"])
    assert ut.tip_in_thread([(tip, True), ("more", False), (nxt, True), ("never", False)]) == "poll"
    assert ut.tip_in_thread([]) is None


def test_not_now_pauses_fourteen_days_and_twice_puts_the_tip_away(ea):
    first_week_done(ea)
    ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
            notify=record_post([]), push=no_push)
    res = ut.handle_reply("reply-draft", "not now", ea_dir=ea, now=at("2026-11-09", "09:00"),
                          usage=usage(ea))
    assert res["text"] == ut.REPLIES["not now"]
    state = ut.load_state(ea)
    assert state["open"] == "" and state["not_now"]["reply-draft"]["count"] == 1
    assert "not now" in blocked(state, at("2026-11-16"))
    assert "not now" in blocked(state, at("2026-11-23", "08:59"))
    assert blocked(state, at("2026-11-23", "09:00")) == ""
    again = ut.post(ea_dir=ea, now=at("2026-11-23", "09:00"), usage=usage(ea), facts=CLEAR,
                    notify=record_post([]), push=no_push)
    assert again["tip"] == "reply-draft"                       # asked again after 14 days
    ut.handle_reply("reply-draft", "later", ea_dir=ea, now=at("2026-11-23", "09:30"),
                    usage=usage(ea))
    state = ut.load_state(ea)
    assert state["dismissed"]["reply-draft"]["why"] == "not now twice"
    third = ut.post(ea_dir=ea, now=at("2026-12-07", "09:30"), usage=usage(ea), facts=CLEAR,
                    notify=record_post([]), push=no_push)
    assert third["tip"] == "tomorrow"


def test_never_records_a_rule_that_stops_the_tips_until_it_is_retired(ea):
    first_week_done(ea)
    ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
            notify=record_post([]), push=no_push)
    res = ut.handle_reply("reply-draft", "never", quote="Never.", ea_dir=ea,
                          now=at("2026-11-09", "09:00"), usage=usage(ea))
    assert res["text"] == ut.REPLIES["never"] and res["rule"] == ut.STOP_RULE_ID
    rules = prefs.load(ea / "config" / "preferences.json")["rules"]
    [rule] = [r for r in rules if r["id"] == ut.STOP_RULE_ID]
    assert rule["kind"] == "instruction" and rule["scopes"] == ["briefing"]
    assert rule["quote"] == "Never." and rule["source"] == ut.SOURCE
    assert ut.stopped(ut.load_state(ea), ea) is True
    later = ut.post(ea_dir=ea, now=at("2026-12-07"), usage=usage(ea), facts=CLEAR,
                    notify=record_post([]), push=no_push)
    assert not later["posted"] and "said never" in later["why"]
    ut.handle_reply("reply-draft", "stop", ea_dir=ea, now=at("2026-12-07"), usage=usage(ea))
    rules = prefs.load(ea / "config" / "preferences.json")["rules"]
    assert [r["id"] for r in rules].count(ut.STOP_RULE_ID) == 1   # one rule, kept
    assert not any(r["id"].startswith(ut.STOP_RULE_ID + "-") for r in rules)
    prefs.update(lambda d: prefs.retire_rule(d, ut.STOP_RULE_ID, "owner wants tips"),
                 ea / "config" / "preferences.json")
    assert ut.stopped(ut.load_state(ea), ea) is False


def test_never_holds_when_the_rule_could_not_be_written(ea, monkeypatch):
    monkeypatch.setattr(ut, "_record_stop", lambda quote, now, ea_dir=None: "")
    res = ut.handle_reply("snooze", "never", ea_dir=ea, now=at("2026-11-09"), usage=usage(ea))
    assert res["text"] == ut.REPLIES["never unsaved"]
    assert ut.stopped(ut.load_state(ea), ea) is True


def test_more_posts_the_next_tip_now(ea):
    first_week_done(ea)
    ut.post(ea_dir=ea, now=at("2026-11-09"), usage=usage(ea), facts=CLEAR,
            notify=record_post([]), push=no_push)
    res = ut.handle_reply("reply-draft", "more", ea_dir=ea, now=at("2026-11-09", "09:00"),
                          usage=usage(ea))
    assert res["next"] == "tomorrow" and res["text"] == ut.post_text(ut.BY_ID["tomorrow"])
    state = ut.load_state(ea)
    assert state["open"] == "tomorrow" and state["dismissed"]["reply-draft"]["why"] == "more"
    assert "one tip a week" in blocked(state, at("2026-11-13"))   # the week counts from now


def test_more_says_so_when_no_tip_is_left(ea):
    for tip in ut.CATALOGUE:
        ut._update_state(lambda s, t=tip.id: s["dismissed"].__setitem__(t, {"why": "test"}), ea)
    res = ut.handle_reply("model-settings", "more", ea_dir=ea, now=at("2026-11-09"),
                          usage=usage(ea))
    assert res["next"] is None and res["text"] == ut.REPLIES["none left"]


def test_reply_refuses_an_unknown_tip_or_word(ea):
    with pytest.raises(KeyError):
        ut.handle_reply("no-such-tip", "more", ea_dir=ea, usage=usage(ea))
    with pytest.raises(ValueError):
        ut.handle_reply("snooze", "maybe", ea_dir=ea, usage=usage(ea))


# ── the CLI ───────────────────────────────────────────────────────────

def _cli(ea: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "EA_DIR": str(ea), "EA_PRIVATE_DIR": str(ea.parent / "private")}
    return subprocess.run(CLI + list(args), env=env, capture_output=True, text=True, timeout=60)


def test_the_cli_lists_reports_and_resets(ea):
    r = _cli(ea, "list", "--json")
    assert r.returncode == 0, r.stderr
    assert [row["id"] for row in json.loads(r.stdout)] == [t.id for t in ut.CATALOGUE]
    r = _cli(ea, "next", "--json")
    assert r.returncode == 0, r.stderr
    nxt = json.loads(r.stdout)
    assert nxt["may_post"] is False and nxt["why_not"]
    r = _cli(ea, "status", "--json")
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)
    assert len(st["tips"]) == len(ut.CATALOGUE) and st["stopped"] is False
    assert _cli(ea, "status").returncode == 0
    ut._update_state(lambda s: s["shown"].__setitem__("snooze", "2026-11-09T08:30:00-07:00"), ea)
    r = _cli(ea, "reset")
    assert r.returncode == 0 and ut.load_state(ea)["shown"] == {}
    r = _cli(ea, "reply", "--word", "more")
    assert r.returncode == 2 and "no tip is open" in r.stdout


def test_post_by_hand_on_a_profile_without_chat_posts_nothing(ea):
    (ea / "config" / "ea-config.json").write_text(json.dumps({"profile": "email"}))
    r = _cli(ea, "post")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["posted"] is False and "no chat" in out["why"]


# ── the wiring ────────────────────────────────────────────────────────

def test_the_bot_answers_the_tip_words_before_anything_else():
    src = (REPO / "ea-chat-bot.py").read_text()
    start = src.index("\ndef _handle_bot_command(")
    body = src[start:src.index("\ndef ", start + 1)]
    first = body.index("_handle_usage_tip_reply(")
    assert first < body.index("_tiers.parse_command(user_msg)")
    assert "\ndef _handle_usage_tip_reply(" in src
    fn = src[src.index("\ndef _handle_usage_tip_reply("):start]
    for needed in ("reply_word(user_msg)", "thread_replies(channel, thread_ts",
                   "tip_in_thread(", "handle_reply(tip_id, word", "_active_threads.discard"):
        assert needed in fn, needed


def test_the_briefing_tick_posts_the_tip_before_the_4am_gate():
    src = (REPO / "run-agent.sh").read_text()
    call = src.index("preprocessor/usage_tips.py\" post")
    assert call < src.index("local-due --hour 4 --agent morning-briefing")
    block = src[src.rindex("if [[", 0, call):call]
    assert '"$AGENT" == "morning-briefing"' in block and '"$HAS_SLACK" == 1' in block
    assert "|| true" in src[call:call + 200]


def test_the_web_push_goes_at_normal_urgency(monkeypatch):
    seen = {}
    import chat.direct as direct

    def fake(text, **kw):
        seen.update(kw, text=text)
        return {"sent": 1}
    monkeypatch.setattr(direct, "web_notify", fake)
    assert ut._web_push("I can write a reply to an email for you.") == {"sent": 1}
    assert seen == {"text": "I can write a reply to an email for you.", "tag": ut.PUSH_TAG,
                    "urgency": "normal"}
