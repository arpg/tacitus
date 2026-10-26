#!/usr/bin/env python3
"""A staged Outlook event with no attendees mails no one, so the bot creates
it with no Create card and posts one line for the batch. An event with a
guest, one on a Google calendar, one past the hourly cap and one whose
create failed still get the card; the same event staged twice is made once.
"""

import json
import tempfile
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    return BOT_SRC[start:BOT_SRC.index("\ndef ", start + 1)]


def _block(start_marker: str, end_marker: str) -> str:
    start = BOT_SRC.index(start_marker)
    return BOT_SRC[start:BOT_SRC.index(end_marker, start)]


class _Atomic:
    @staticmethod
    def write_json(path, data, mode=0o600):
        Path(path).write_text(json.dumps(data))


def _ns(tmp: Path, *, create_ok=True):
    created, posts, cards = [], [], []
    ns = {
        "json": json, "time": time, "threading": threading, "_atomic": _Atomic,
        "_private_state": lambda name: tmp / name,
        "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
        "_event_time_local": lambda v: v,
        "_event_time_text": lambda v: f"T({v})",
        "_infer_online": lambda ev: bool(ev.get("online")),
        "_resolve_online_location": lambda loc, online: loc,
        "_repeat_fields": lambda ev: {},
        "_is_hold": lambda ev: bool(ev.get("hold")),
        "_execute_event_create":
            lambda ch, tts, ts, data, **kw: created.append((data, kw)) or create_ok,
        "FRONTEND": types.SimpleNamespace(post=lambda **kw: posts.append(kw)),
        # the poster's other collaborators, as the claimed action reaches them
        "_staged_route": lambda route, ch, tts: (ch, tts),
        "STAGED_ROUTING_KEYS": ("thread_ts", "channel"),
        "_remote_llm_note": lambda *a, **k: None,
        "_post_event_confirmation": lambda ch, tts, **kw: cards.append(kw),
    }
    exec(compile(_block("# An Outlook event with no attendees mails no one",
                        "def _load_button_payloads("), "ea-chat-bot.py[block]", "exec"), ns)
    exec(compile(_fn_src("_post_staged_action"), "ea-chat-bot.py[fn]", "exec"), ns)
    return ns, created, posts, cards


def _ev(subject="Panel", **kw):
    return {"subject": subject, "start": "2027-01-05T10:00:00",
            "end": "2027-01-05T11:00:00", "attendees": "", **kw}


def test_a_guestless_batch_is_created_with_one_post_and_no_card():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td))
        ns["_post_staged_action"]("C1", "T1", {
            "action": "event_create_multi",
            "events": [_ev("Opening"), _ev("Lunch", start="2027-01-05T12:00:00"),
                       _ev("Run", hold=True, start="2027-01-05T07:00:00")]})
        assert [d["subject"] for d, _ in created] == ["Opening", "Lunch", "Run"]
        assert all(kw.get("announce") is False for _, kw in created)
        assert created[2][0]["hold"] is True
        assert cards == []
        assert len(posts) == 1 and "3 events" in posts[0]["text"]
        assert posts[0]["thread_ts"] == "T1"


def test_a_guest_or_a_google_calendar_still_gets_the_card():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td))
        ns["_post_staged_action"]("C1", "T1", {
            "action": "event_create_multi",
            "events": [_ev("Mine"),
                       _ev("Invite", attendees="ash.iverson87@example.com"),
                       _ev("Optional", optional_attendees=["ash.iverson87@example.com"]),
                       _ev("Family", calendar="family@example.com")]})
        assert [d["subject"] for d, _ in created] == ["Mine"]
        assert [c["subject"] for c in cards] == ["Invite", "Optional", "Family"]
        assert len(posts) == 1


def test_a_single_staged_event_takes_the_same_path():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td))
        ns["_post_staged_action"]("C1", "T1", {"action": "event_create", **_ev()})
        assert len(created) == 1 and cards == [] and "1 event" in posts[0]["text"]
        ns["_post_staged_action"]("C1", "T1", {
            "action": "event_create", **_ev(attendees="ash.iverson87@example.com")})
        assert len(created) == 1 and len(cards) == 1


def test_the_same_event_staged_twice_is_created_once():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td))
        for _ in range(2):
            ns["_post_staged_action"]("C1", "T1", {"action": "event_create", **_ev()})
        assert len(created) == 1 and cards == [] and len(posts) == 1


def test_past_the_hourly_cap_the_card_comes_back():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td))
        cap = ns["_GUESTLESS_PER_HOUR"]
        ns["_post_staged_action"]("C1", "T1", {
            "action": "event_create_multi",
            "events": [_ev(f"E{i}") for i in range(cap + 2)]})
        assert len(created) == cap
        assert [c["subject"] for c in cards] == [f"E{cap}", f"E{cap + 1}"]


def test_a_failed_create_falls_back_to_the_card_and_can_be_retried():
    with tempfile.TemporaryDirectory() as td:
        ns, created, posts, cards = _ns(Path(td), create_ok=False)
        ns["_post_staged_action"]("C1", "T1", {"action": "event_create", **_ev()})
        assert len(created) == 1 and len(cards) == 1 and posts == []
        assert json.loads((Path(td) / "guestless-events.json").read_text()) == {}
