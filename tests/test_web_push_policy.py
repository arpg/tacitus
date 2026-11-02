"""The web chat's push policy (botcore/botcore/chat/web_push_policy.py;
docs/webchat.md, Notifications): which of the bot's posts notify the
owner's devices, the collapse of a thread's pushes, the check that lets
one text push once (a direct message and its alert), the queue during
quiet hours, the owner's `notify_push` switches, and the wiring through
the chat package and the notifier. Digests, nudges and progress lines
never push. The owner's reply, in the web app or mirrored from the
primary, drops the thread's queued push, sends one clear for a thread
that notified, and runs the thread-state rules at once."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))

import chat as chat_pkg  # noqa: E402
import chat.direct as direct  # noqa: E402
import chat_notify  # noqa: E402
import ea_config  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from chat.cards import CardAction, ConfirmationCard  # noqa: E402
from chat.types import MessageRef  # noqa: E402
from chat.web_frontend import WebFrontend  # noqa: E402
from test_web_frontend import make  # noqa: E402

pol = wf.web_push_policy
wp = wf.web_push
needs_push = pytest.mark.skipif(not wp.AVAILABLE, reason="web push needs the cryptography package")

BRIEFING = ":sunny: *Morning Briefing — Sun Nov 1*\n1. Answer the review request"
UPDATE = ":coffee: *Afternoon Update — Sun Nov 1*\nNothing new since the morning."
PROGRESS = (":hourglass_flowing_sand: Still working... (3m)",
            ":hourglass_flowing_sand: 4 min in. So far: read the mail.",
            ":time_ticking: Still working...")
POINTER = (":lock: The draft to the committee is in your DM: "
           "[open it](https://host.example.test:8444/chat/#/dm/t9/w9)")
FCM = "https://fcm.googleapis.com/fcm/send/made-up-subscription"


def channels(f: WebFrontend) -> WebFrontend:
    s = f.store
    s.upsert_channel("main", "ea", "main")
    s.upsert_channel("dm", "dm", "dm")
    s.upsert_channel("sched", "ea-scheduling", "sched")
    s.upsert_channel("r1", "ea-atlas", "room")
    s.upsert_channel("digests", "Mail digests", "digest")
    return f


@pytest.fixture
def fe(tmp_path):
    f = channels(make(tmp_path))
    yield f
    f.stop()


def row(f, channel, author, text, thread=None, kind="message", origin=""):
    r = f.store.add_message(channel, mid=None, thread_key=thread, author=author, kind=kind,
                            text=text)
    if origin:
        f.store.note_origin(channel, r["thread_key"], origin)
    return r


def decide(f, r, **settings):
    return pol.message_push(f.store, r, pol.Settings(**settings))


# the policy, clause by clause


def test_a_bot_message_in_the_dm_pushes_and_a_direct_message_goes_high(fe):
    r = row(fe, "dm", "bot", "Your flight leaves at 9.")
    p = decide(fe, r)
    assert (p.clause, p.title, p.body, p.urgency, p.may_wait) == \
        ("dm", "Tacitus", "Your flight leaves at 9.", "normal", False)
    d = row(fe, "dm", "bot", "Tickets on sale now.", origin="personal")
    assert decide(fe, d).urgency == "high"
    # the switches leave the DM alone
    assert decide(fe, r, briefings=False, replies=False, rooms=False).clause == "dm"


def test_a_briefing_and_an_update_push_their_title_alone(fe):
    for text, title in ((BRIEFING, "Your morning briefing is ready"),
                        (UPDATE, "Afternoon update"),
                        (":sunny: *Morning Briefing, Sun Nov 1*", "Your morning briefing is ready"),
                        ("_(held 04:00)_\n" + BRIEFING, "Your morning briefing is ready")):
        r = row(fe, "main", "bot", text)
        assert decide(fe, r) is None                     # no notifier opened it
        fe.store.note_origin("main", r["thread_key"], "ea-assistant")
        p = decide(fe, r)
        assert (p.clause, p.title, p.body, p.urgency, p.may_wait) == \
            ("briefing", title, "", "normal", True)
        assert decide(fe, r, briefings=False) is None
        part = row(fe, "main", "bot", "2. Book the room", thread=r["thread_key"])
        assert decide(fe, part) is None                  # the report's later parts
    # other notices from a notifier, and the title anywhere else
    for text in (":clipboard: *Weekly audit*\nAll green.",
                 "morning-briefing: nothing new since the last run.",
                 "Notes on the morning briefing format"):
        assert decide(fe, row(fe, "main", "bot", text, origin="ea-assistant")) is None
    for channel, origin in (("r1", "room:r1"), ("sched", "scheduling"),
                            ("digests", "briefing-preview"), ("dm", "")):
        p = decide(fe, row(fe, channel, "bot", BRIEFING, origin=origin))
        assert p is None or p.clause == "dm"


def test_an_answer_to_the_owner_pushes_once_per_turn(fe):
    first = row(fe, "main", "owner", "What is on my calendar tomorrow?")
    key = first["thread_key"]
    p = decide(fe, row(fe, "main", "bot", "Two meetings: 10:00 and 14:00.", thread=key))
    assert (p.clause, p.title, p.body, p.urgency, p.may_wait) == \
        ("reply", "Tacitus replied", "Two meetings: 10:00 and 14:00.", "normal", True)
    assert decide(fe, row(fe, "main", "bot", "The 14:00 moved.", thread=key)) is None
    row(fe, "main", "owner", "Move the 10:00 to Friday.", thread=key)
    assert decide(fe, row(fe, "main", "bot", "Moved.", thread=key)).clause == "reply"
    # a thread the bot opened (a briefing) that the owner answered
    b = row(fe, "main", "bot", BRIEFING, origin="ea-assistant")
    row(fe, "main", "owner", "Draft the reply in item 1.", thread=b["thread_key"])
    assert decide(fe, row(fe, "main", "bot", "Draft created.",
                          thread=b["thread_key"])).title == "Tacitus replied"
    # the scheduling channel answers count as replies too
    s = row(fe, "sched", "owner", "Find an hour with the lab next week.")
    assert decide(fe, row(fe, "sched", "bot", "Tuesday 10:00 works.",
                          thread=s["thread_key"])).clause == "reply"


def test_an_answer_in_a_room_names_the_room_and_rooms_has_its_own_switch(fe):
    o = row(fe, "r1", "owner", "Where is the review draft?")
    a = row(fe, "r1", "bot", "In the project folder.", thread=o["thread_key"])
    assert decide(fe, a).title == "Tacitus replied in ea-atlas"
    assert decide(fe, a, rooms=False) is None
    assert decide(fe, a, replies=False).clause == "reply"
    m = row(fe, "main", "owner", "Any news?")
    ma = row(fe, "main", "bot", "None today.", thread=m["thread_key"])
    assert decide(fe, ma, replies=False) is None
    assert decide(fe, ma, rooms=False).clause == "reply"


def test_progress_lines_pointers_and_nudges_never_push_nor_answer(fe):
    o = row(fe, "main", "owner", "Summarise the thread with the reviewers.")
    key = o["thread_key"]
    for text in PROGRESS + (POINTER,):
        assert decide(fe, row(fe, "main", "bot", text, thread=key)) is None
        assert decide(fe, row(fe, "dm", "bot", text)) is None
    nudge = fe.store.post_nudge("main", key, "Still need this?")
    assert nudge is not None and decide(fe, nudge) is None
    # none of them was the answer, so the answer still pushes
    assert decide(fe, row(fe, "main", "bot", "Here is the summary.", thread=key)).clause == "reply"


def test_a_card_in_the_conversation_counts_as_the_answer(fe):
    o = row(fe, "main", "owner", "Draft a reply to the committee.")
    row(fe, "main", "bot", "Send this draft?", thread=o["thread_key"], kind="card")
    assert decide(fe, row(fe, "main", "bot", "Draft created.", thread=o["thread_key"])) is None


def test_digests_and_notices_the_owner_never_wrote_in_never_push(fe):
    d = row(fe, "digests", "bot", "*Kept mail*: two messages", origin="mail-digest")
    assert decide(fe, d) is None
    row(fe, "digests", "owner", "thanks", thread=d["thread_key"])
    assert decide(fe, row(fe, "digests", "bot", "noted", thread=d["thread_key"])) is None
    for channel in ("main", "sched", "r1"):
        assert decide(fe, row(fe, channel, "bot", "Calendar check: all clear.",
                              origin="ea-assistant")) is None
    assert decide(fe, row(fe, "main", "owner", "hello")) is None
    assert decide(fe, row(fe, "nowhere", "bot", "a channel the store lacks")) is None
    assert decide(fe, None) is None


def test_the_settings_read_the_notify_push_block(tmp_path):
    S = pol.Settings
    assert S.from_config(None) == S(True, True, True, "queue")
    assert S.from_config({"briefings": False, "replies": "off", "rooms": "no",
                          "quiet": "SEND"}) == S(False, False, False, "send")
    assert S.from_config({"replies": "yes", "quiet": "later"}) == S()
    assert S.from_config(["not", "a", "block"]) == S()
    f = make(tmp_path, notify_push={"rooms": False, "quiet": "send"})
    assert f.push_settings == S(rooms=False, quiet="send")
    assert make(tmp_path / "b").push_settings == S()


def test_the_fingerprint_reads_the_line_a_notification_shows():
    fp = pol.fingerprint
    raw = ":rotating_light: *Tickets* on sale <https://tix.example.test|now>\nLedger `tix`."
    md = ":rotating_light: **Tickets** on sale [now](https://tix.example.test)"
    assert fp("Tacitus", raw) == fp("Tacitus", md) != ""
    assert fp("Tacitus", "Tickets on sale later") != fp("Tacitus", raw)
    assert fp("Your morning briefing is ready") != fp("Afternoon update")
    assert fp("", "") == ""


# the front end: what goes out, when, and once


def pushing_fe(tmp_path, **cfg) -> WebFrontend:
    """A front end with one made-up subscription whose sends are recorded
    in `f.sent` ((payload, urgency)), never made."""
    f = channels(make(tmp_path, **cfg))
    f.store.push_subscribe(FCM, {"p256dh": "made-up", "auth": "made-up"})
    f.sent = []
    f.push.send_all = lambda payload, urgency="normal": f.sent.append(
        (json.loads(payload), urgency)) or {"sent": 1}
    return f


@pytest.fixture
def pushing(tmp_path):
    f = pushing_fe(tmp_path)
    yield f
    f.stop()


def until(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def shown(f) -> list[dict]:
    """The pushes that show a notification: every one but the clears."""
    return [p for p, _u in f.sent if p.get("type") != "clear"]


def owner_says(f, channel, text, thread=None) -> str:
    """The owner's message, as web_message stores it; its thread key."""
    return row(f, channel, "owner", text, thread=thread)["thread_key"]


def card() -> ConfirmationCard:
    return ConfirmationCard(kind="send", title=":lock: Send this draft?", body="Hello.",
                            actions=[CardAction("approve_send", "Send", {"token": "t"},
                                                "primary", "send")])


@needs_push
def test_an_answer_pushes_and_its_thread_collapses_for_two_minutes(pushing):
    f = pushing
    assert wp.MIN_GAP_S == 120.0
    key = owner_says(f, "main", "What is on tomorrow?")
    ref = f.post("main", "Two *meetings*.", thread_ts=key)
    assert until(lambda: len(f.sent) == 1)
    payload, urgency = f.sent[0]
    assert (payload["title"], payload["body"], payload["tag"], urgency) == \
        ("Tacitus replied", "Two meetings.", f"main/{key}", "normal")
    assert payload["url"].endswith(f"#/main/{key}/{ref.id}")
    owner_says(f, "main", "Move the first one.", key)
    f.post("main", "Moved.", thread_ts=key)              # within two minutes: collapsed
    time.sleep(0.2)
    assert len(shown(f)) == 1
    f.push._last = {k: t - 121 for k, t in f.push._last.items()}
    owner_says(f, "main", "And the second?", key)
    f.post("main", "Moved as well.", thread_ts=key)      # two minutes on: pushed
    assert until(lambda: len(shown(f)) == 2)
    assert shown(f)[1]["body"] == "Moved as well."


@needs_push
def test_a_briefing_pushes_its_title_and_a_notice_nothing(pushing):
    f = pushing
    first = f.post("main", BRIEFING, origin="ea-assistant")
    assert until(lambda: len(f.sent) == 1)
    assert (f.sent[0][0]["title"], f.sent[0][0]["body"]) == ("Your morning briefing is ready", "")
    assert f.sent[0][0]["tag"] == f"main/{first.thread_key}"
    assert f.store.thread_origin("main", first.thread_key) == "ea-assistant"
    f.post("main", "2. Book the room", thread_ts=first.thread_key)
    f.post("main", ":clipboard: *Weekly audit*\nAll green.", origin="ea-assistant")
    f.post("digests", "*Morning briefing, Sun Nov 1*", origin="briefing-preview")
    f.post("main", UPDATE, origin="ea-assistant")
    assert until(lambda: len(f.sent) == 2)
    time.sleep(0.2)
    assert [s[0]["title"] for s in f.sent] == ["Your morning briefing is ready",
                                               "Afternoon update"]


@needs_push
def test_digests_nudges_and_progress_lines_never_push(pushing):
    f = pushing
    f.post("digests", "*Kept mail*: two messages", origin="mail-digest")
    key = owner_says(f, "main", "Summarise the reviews.")
    for text in PROGRESS + (POINTER,):
        f.post("main", text, thread_ts=key)
        f.post("dm", text)
    assert f._post_nudge("main", key, "Still need this?") is not None
    time.sleep(0.2)
    assert f.sent == []
    f.post("main", "The summary.", thread_ts=key)
    assert until(lambda: len(f.sent) == 1)


@needs_push
def test_a_direct_message_and_its_alert_push_once(pushing):
    f = pushing
    text = ":rotating_light: In 15 minutes: *Tickets*\nLedger `tix`."
    f.post("dm", text, origin="personal")               # what send_direct posts
    assert until(lambda: len(f.sent) == 1)
    assert f.sent[0][1] == "high"
    out = f.notify("Tacitus", text, f.app_url(), "alert:tix", urgency="high", wait=True,
                   dedupe=True)
    assert out == {"sent": 0, "skipped": "duplicate"}
    # the other order: the alert's push first, then the DM post
    other = ":rotating_light: Registration opens now"
    assert f.notify("Tacitus", other, f.app_url(), "alert:reg", urgency="high", wait=True,
                    dedupe=True)["sent"] == 1
    f.post("dm", other, origin="personal")
    time.sleep(0.2)
    assert len(f.sent) == 2
    # ten seconds on, the same text pushes again
    f.store.push_note("text:" + pol.fingerprint("Tacitus", other), time.time() - 11)
    assert f.notify("Tacitus", other, f.app_url(), "alert:reg2", urgency="high", wait=True,
                    dedupe=True)["sent"] == 1
    # a plain notify and a card never take part in the check
    assert f.notify("Tacitus", other, tag="x", wait=True)["sent"] == 1
    f.post_card("dm", card())
    f.post_card("dm", card())
    assert until(lambda: len(f.sent) == 6)


@needs_push
def test_an_alert_after_its_direct_message_pushes_once_across_front_ends(tmp_path, monkeypatch):
    """alert.py's path: send_direct's DM post through one front end, then
    chat.direct.web_notify through another on the same store."""
    from test_web_push import FakePushService, browser
    svc = FakePushService()
    f = channels(make(tmp_path))
    try:
        store = tmp_path / "priv" / "webchat.sqlite"
        ea = tmp_path / "ea"
        (ea / "config").mkdir(parents=True)
        (ea / "config" / "ea-config.json").write_text(json.dumps({"chat": {
            "frontend": "zulip", "shadow": "web",
            "web": {"store_path": str(store), "public_url": "https://host.example.test/chat"}}}))
        f.store.push_subscribe(f"{svc.origin}/push/a", browser()[2])
        monkeypatch.setattr(wp, "endpoint_allowed", lambda url: True)
        f.push.allow = lambda url: True
        text = ":rotating_light: In 15 minutes, at 10:00 am MT: Tickets\nLedger `tix`."
        f.post("dm", text, origin="personal")
        svc.wait(1)
        assert direct.web_notify(text, tag="alert:tix", ea_dir=ea) == \
            {"sent": 0, "skipped": "duplicate"}
        time.sleep(0.2)
        assert len(svc.requests) == 1 and svc.requests[0]["headers"]["urgency"] == "high"
    finally:
        f.stop()
        svc.close()


@needs_push
def test_quiet_hours_hold_a_briefing_and_a_reply_until_they_end(pushing):
    f = pushing
    end = time.time() + 3600
    f.quiet_until = lambda: end
    b = f.post("main", BRIEFING, origin="ea-assistant")
    key = owner_says(f, "main", "Anything for tomorrow?")
    answer = f.post("main", "Nothing booked.", thread_ts=key)
    rkey = owner_says(f, "r1", "Status of the draft?")
    f.post("r1", "With the coauthors.", thread_ts=rkey)
    f.post("dm", "A direct message.", origin="personal")    # the DM goes at once
    f.post_card("dm", card())                               # and so does a card
    assert until(lambda: len(f.sent) == 2)
    time.sleep(0.2)
    assert sorted(s[0]["title"] for s in f.sent) == ["Tacitus", "Tacitus"]
    queued = {q["tag"]: q for q in f.store.push_queued()}
    assert set(queued) == {f"main/{b.thread_key}", f"main/{key}", f"r1/{rkey}"}
    assert {q["due_at"] for q in queued.values()} == {end}
    assert json.loads(queued[f"main/{b.thread_key}"]["payload"])["title"] == \
        "Your morning briefing is ready"
    assert queued[f"main/{key}"]["seq"] == f.store.message("main", answer.id)["seq"]
    # the owner reads the EA channel meanwhile: only the room's push is left
    f.store.mark_read("main")
    assert [q["tag"] for q in f.store.push_claim_due(end + 1)] == [f"r1/{rkey}"]
    assert f.store.push_queued() == []


@needs_push
def test_a_later_owner_message_drops_a_queued_reply(pushing):
    f = pushing
    f.quiet_until = lambda: time.time() + 3600
    key = owner_says(f, "r1", "Status of the draft?")
    f.post("r1", "With the coauthors.", thread_ts=key)
    owner_says(f, "r1", "Thanks.", key)
    assert f.store.push_claim_due(time.time() + 7200) == []


@needs_push
def test_quiet_send_and_a_failing_quiet_hook_push_at_once(tmp_path):
    f = pushing_fe(tmp_path / "a", notify_push={"quiet": "send"})
    g = pushing_fe(tmp_path / "b")
    try:
        f.quiet_until = lambda: time.time() + 3600

        def boom():
            raise RuntimeError("no quiet hours here")
        g.quiet_until = boom
        for x in (f, g):
            key = owner_says(x, "main", "Anything new?")
            x.post("main", "Nothing new.", thread_ts=key)
            assert until(lambda: len(x.sent) == 1)
            assert x.store.push_queued() == []
    finally:
        f.stop()
        g.stop()


@needs_push
def test_the_switches_turn_the_pushes_off(tmp_path):
    f = pushing_fe(tmp_path, notify_push={"briefings": False, "replies": False, "rooms": False})
    try:
        f.post("main", BRIEFING, origin="ea-assistant")
        f.post("main", "Answer.", thread_ts=owner_says(f, "main", "Question?"))
        f.post("r1", "Answer.", thread_ts=owner_says(f, "r1", "Question?"))
        f.post("dm", "Still pushed: the DM.")
        assert until(lambda: len(f.sent) == 1)
        time.sleep(0.2)
        assert f.sent[0][0]["body"] == "Still pushed: the DM."
    finally:
        f.stop()


@needs_push
def test_a_writer_only_copy_sends_before_returning(tmp_path):
    base = make(tmp_path)
    f = WebFrontend.from_config({"port": 0}, store_path=base.store_path, writer_only=True)
    f.start()
    try:
        channels(f)
        f.store.push_subscribe(FCM, {"p256dh": "made-up", "auth": "made-up"})
        sent: list = []
        f.push.send_all = lambda payload, urgency="normal": sent.append(urgency) or {"sent": 1}
        f.post("dm", "A note from a command-line sender.")
        assert sent == ["normal"]                          # no worker: sent in the call
        assert f.push._thread is None
    finally:
        f.stop()
        base.stop()


# the owner's reply clears the conversation's notifications


def clears(f) -> list[dict]:
    return [p for p, _u in f.sent if p.get("type") == "clear"]


@needs_push
def test_an_owner_reply_sends_one_clear_for_a_thread_that_notified(pushing):
    f = pushing
    key = owner_says(f, "main", "What is on tomorrow?")
    f.post("main", "Two meetings.", thread_ts=key)
    assert until(lambda: len(f.sent) == 1)
    owner_says(f, "main", "Move the first one.", key)
    assert until(lambda: len(f.sent) == 2)
    assert f.sent[1][0] == {"type": "clear", "tag": f"main/{key}"}     # no title: shows nothing
    owner_says(f, "main", "And the second.", key)                    # nothing shows: no clear
    # the two-minute window holds: the answer right after the reply does not buzz
    f.post("main", "Both moved.", thread_ts=key)
    other = owner_says(f, "main", "A thread that never notified.")
    owner_says(f, "main", "Still nothing to clear.", other)
    time.sleep(0.2)
    assert len(clears(f)) == 1 and len(f.sent) == 2


@needs_push
def test_an_owner_reply_drops_the_threads_queued_push(pushing):
    f = pushing
    f.quiet_until = lambda: time.time() + 3600
    key = owner_says(f, "r1", "Status of the draft?")
    f.post("r1", "With the coauthors.", thread_ts=key)
    other = owner_says(f, "main", "Anything for tomorrow?")
    f.post("main", "Nothing booked.", thread_ts=other)
    assert {q["tag"] for q in f.store.push_queued()} == {f"r1/{key}", f"main/{other}"}
    owner_says(f, "r1", "Thanks.", key)
    assert [q["tag"] for q in f.store.push_queued()] == [f"main/{other}"]
    time.sleep(0.2)
    assert f.sent == []                                  # nothing showed, so no clear


@needs_push
def test_a_queued_push_that_goes_is_cleared_by_the_next_reply(pushing):
    f = pushing
    f.quiet_until = lambda: time.time() + 3600
    key = owner_says(f, "main", "Anything for tomorrow?")
    f.post("main", "Nothing booked.", thread_ts=key)
    (due,) = f.store.push_claim_due(time.time() + 7200)  # the worker sends it at the end
    assert due["tag"] == f"main/{key}"
    owner_says(f, "main", "Book the lab for 10.", key)
    assert until(lambda: clears(f) == [{"type": "clear", "tag": f"main/{key}"}])


@needs_push
def test_a_mirrored_owner_reply_clears_like_one_from_the_app(tmp_path):
    """Shadow mode: the owner answers on the primary, the composite mirrors
    the message, and the phone's notification for the thread goes."""
    f = pushing_fe(tmp_path)
    f.inbound = lambda ev: None
    try:
        f.mirror_channel("stream:7", "ea-dm", "dm")
        key = "stream:7:topic:lock"
        f.mirror_card(MessageRef("stream:7", "1001", key, time.time()), card())
        assert until(lambda: len(f.sent) == 1)
        f.mirror_post(MessageRef("stream:7", "900", key, time.time() - 60), "older", "owner",
                      quiet=True)                        # history: no clear
        f.mirror_post(MessageRef("stream:7", "1002", key, time.time()), "Sent it myself.",
                      "owner")
        assert until(lambda: clears(f) == [{"type": "clear", "tag": f"stream:7/{key}"}])
    finally:
        f.stop()


def test_a_mirrored_owner_message_runs_the_state_rules_at_once(tmp_path):
    """The rules read the store's event log, so a message the composite
    mirrors from the primary moves the thread's state as one typed in the
    web app does: a failed turn needs the owner, the next message is work."""
    f = make(tmp_path)
    f.inbound = lambda ev: None
    try:
        ts = f.enable_thread_state()
        f.mirror_channel("stream:9", "ea", "main")
        key = "stream:9:topic:room booking"
        ts.changed()                                      # the first call takes the log's end
        f.mirror_post(MessageRef("stream:9", "101", key, time.time()), "Book the room.", "owner")
        f.mirror_reaction("stream:9", "101", "x", True)   # the turn failed
        assert ts.changed() == 1
        assert f.store.thread_state("stream:9", key)["state"] == "needs_you"
        f.mirror_post(MessageRef("stream:9", "102", key, time.time()), "Try again.", "owner")
        assert ts.changed() == 1
        row = f.store.thread_state("stream:9", key)
        assert (row["state"], row["by"]) == ("working", "rule")
    finally:
        f.stop()


# the wiring: the chat package's quiet hours and the notifier's posts


def test_the_chat_package_gives_the_web_chat_the_owners_quiet_hours(tmp_path, monkeypatch):
    end = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    monkeypatch.setitem(sys.modules, "quiet_hours", SimpleNamespace(quiet_end=lambda: end))
    assert chat_pkg.quiet_until() == end.timestamp()
    monkeypatch.setitem(sys.modules, "quiet_hours", SimpleNamespace(quiet_end=lambda: None))
    assert chat_pkg.quiet_until() is None
    monkeypatch.setitem(sys.modules, "quiet_hours", SimpleNamespace())
    assert chat_pkg.quiet_until() is None                  # unread: the push goes now
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": {
        "frontend": "zulip", "shadow": "web",
        "web": {"store_path": str(tmp_path / "priv" / "webchat.sqlite")}}}))
    fe = chat_pkg.load_web_frontend(ea_dir=tmp_path)
    assert fe.quiet_until is chat_pkg.quiet_until


@pytest.fixture
def web_alone(tmp_path, monkeypatch):
    """The web chat alone in the owner-private chat.json, an owner process,
    quiet hours off, and every push the front ends ask for recorded."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    cfg = tmp_path / "ea-config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(chat_pkg.owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(chat_notify, "_ROOM_FE", [])
    monkeypatch.setitem(sys.modules, "quiet_hours", SimpleNamespace(
        should_defer=lambda priority: False, defer=lambda *a: None, quiet_end=lambda: None))
    (priv / "chat.json").write_text(json.dumps({"frontend": "web", "web": {"port": 0}}))
    (priv / "chat.json").chmod(0o600)
    pushed: list = []
    monkeypatch.setattr(wp, "AVAILABLE", True)
    monkeypatch.setattr(WebFrontend, "notify", lambda self, title, body="", url="", tag="", **kw:
                        pushed.append((title, body, tag, kw)) or {"queued": True})
    return pushed


def test_the_notifiers_briefing_and_direct_message_push_and_a_digest_does_not(web_alone):
    pushed = web_alone
    assert chat_notify.post("ea-assistant", BRIEFING)
    ((title, body, tag, kw),) = pushed
    assert (title, body, tag.split("/")[0]) == ("Your morning briefing is ready", "", "main")
    assert kw == {"urgency": "normal", "wait": True, "dedupe": True}
    assert chat_notify.post("ea-assistant", ":clipboard: *Weekly audit*\nAll green.")
    assert chat_notify.post("mail-digest", "*Kept* mail")
    assert len(pushed) == 1
    assert direct.send_direct("Tickets go on sale at 10:00.", direct.frontend()) == \
        {"posted": True}
    assert pushed[1][0:2] == ("Tacitus", "Tickets go on sale at 10:00.")
    assert pushed[1][2].startswith("dm/") and pushed[1][3]["urgency"] == "high"
