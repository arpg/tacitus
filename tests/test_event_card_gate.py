"""Event cards show everything their click sends, and the click acts only
on what the card showed.

A Create card carries the invite text as the calendar gets it (the join
block of an online invite included) and every attendee. A Reschedule or
Delete card comes from the bot's own read of the event, not from the
staged labels, and its payload keeps that read's fingerprint: the click
reads the event again and, on any difference, changes nothing and posts a
new card. A card that cannot show all of it in one message has no
approval button. No network: the reads and the gated verbs are stubbed.
"""
import dataclasses
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "preprocessor"))
import chat  # noqa: E402
from chat import slack_frontend as slack  # noqa: E402
from chat import zulip_frontend as zulip  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
VALUE = json.dumps({"thread_ts": "T", "ea_channel": "C", "payload_ref": "k"})
FP = "a" * 64


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    ends = [BOT_SRC.index("\ndef ", start + 1)]
    for deco in ("\n@app.", "\n# ── "):
        nxt = BOT_SRC.find(deco, start + 1)
        if nxt > 0:
            ends.append(nxt)
    return BOT_SRC[start:min(ends)]


def _zulip_fits(card) -> bool:
    """ZulipFrontend.card_fits at the default limit: the whole card text."""
    return len(zulip.render_card(card, "X" * 6, None, 0)) + 100 <= 9800


def cards(fits=None) -> dict:
    """The bot's `# ── Confirmation cards` block. `fits` is the
    frontend's card_fits; None takes any card."""
    start = BOT_SRC.index("# ── Confirmation cards")
    end = BOT_SRC.index("# ── End confirmation cards", start)
    fe = types.SimpleNamespace(card_fits=fits) if fits else types.SimpleNamespace()
    ns = {"json": json, "_chat": chat, "dataclasses": dataclasses, "FRONTEND": fe}
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[cards]", "exec"), ns)
    return ns


def rendered(card) -> str:
    """Everything the owner sees, on both frontends."""
    text, blocks = slack.render_card(card)
    return json.dumps(blocks) + "\n" + zulip.render_card(card, "ABCDEF", None, 0)


def action_ids(card) -> list:
    return [a.id for a in card.actions]


def read(**kw) -> dict:
    """An owner-side read as _read_calendar_event returns it."""
    ev = {"subject": "Budget review", "when": "Tue Oct 6 2026, 2:00 PM MDT to Tue Oct 6 "
          "2026, 3:00 PM MDT", "organizer": "you", "is_organizer": True,
          "attendees": [{"name": "Ira Lindqvist", "address": "ira.lindqvist53@example.edu",
                         "type": "required"},
                        {"name": "", "address": "sam.lee@example.edu", "type": "optional"}],
          "location": "Room 101", "series": "", "cancelled": False, "fingerprint": FP}
    ev.update(kw)
    return ev


# --- the Create card -------------------------------------------------------

def test_the_create_card_shows_the_whole_invite_text_and_every_attendee():
    ns = cards(_zulip_fits)
    people = [f"person{i}@example.edu" for i in range(9)]
    body = "Agenda:\n1. Budget\n2. Hiring\n\nJoin Zoom meeting\nhttps://example.zoom.us/j/1"
    card = ns["_event_card"]("C", "T", "Planning", "s", "e", ",".join(people), "", "", False,
                             VALUE, optional_attendees="opt1@example.edu", body=body)
    assert "approve_event" in action_ids(card)
    shown = rendered(card)
    for addr in people + ["opt1@example.edu"]:
        assert addr in shown, addr
    assert "more" not in shown
    for line in body.splitlines():
        assert line in shown, line
    assert any("every invitee gets it" in ln for ln in card.lines)


def test_an_online_invite_shows_the_join_block_the_calendar_appends(monkeypatch):
    from preprocessor import institution
    monkeypatch.setattr(institution, "video_link", lambda: "https://example.zoom.us/j/1")
    monkeypatch.setattr(institution, "video_join_block",
                        lambda: "Join Zoom meeting\nhttps://example.zoom.us/j/1")
    ns = {"log": types.SimpleNamespace(warning=lambda *a: None)}
    exec(_fn_src("_invite_body_as_sent"), ns)
    sent = ns["_invite_body_as_sent"]
    # graph_client.create_event: f"{body}\n\n{join_block}\n", or the block alone.
    assert sent("Agenda", True) == "Agenda\n\nJoin Zoom meeting\nhttps://example.zoom.us/j/1\n"
    assert sent("", True) == "Join Zoom meeting\nhttps://example.zoom.us/j/1"
    assert sent("Agenda", False) == "Agenda"
    monkeypatch.setattr(institution, "video_link", lambda: "")
    assert sent("Agenda", True) == "Agenda"


def test_a_long_invite_text_gets_no_create_button():
    for fits in (_zulip_fits, slack.draft_card_fits, None):
        ns = cards(fits)
        card = ns["_event_card"]("C", "T", "Planning", "s", "e", "a@example.edu", "", "",
                                 False, VALUE, body="Line of the agenda.\n" * 6000)
        assert action_ids(card) == ["cancel_event"], fits
        assert card.no_default, "a thumbs up meant as Create must not pick Cancel"
        assert any("Not shown in full" in ln for ln in card.lines)
        # The short card itself fits, so it posts.
        assert _zulip_fits(card) and len(slack.render_card(card)[1]) <= 50


def test_a_reply_with_a_long_quoted_thread_offers_no_thumbs_up():
    """A short reply over a long quoted thread is more than one card shows,
    so the card withholds Send. It once gave Edit the thumbs up, the chip
    that means Send on every other draft card; it now has no default."""
    ns = cards(_zulip_fits)
    draft = {"to": "emma.lindgren41@example.edu", "subject": "RE: Seats", "conversation_id": "c1",
             "body": "Hi Emma,\nWould that work on your end?\nKit",
             "quoted": "> Earlier message in the thread.\n" * 600}
    card = ns["_send_card"]("C", "T", "d1", draft)
    assert "approve_send" not in action_ids(card) and card.no_default
    assert chat.cards.action_reactions(card)[0] == ("pencil2", "edit_draft")
    assert "+1" not in dict(chat.cards.action_reactions(card))
    short = ns["_send_card"]("C", "T", "d1", dict(draft, quoted=""))
    assert "approve_send" in action_ids(short) and not short.no_default
    assert dict(chat.cards.action_reactions(short))["+1"] == "approve_send"


def test_an_attendee_list_too_long_for_one_card_gets_no_create_button():
    ns = cards(_zulip_fits)
    people = ",".join(f"invitee{i:03d}@example.edu" for i in range(200))
    card = ns["_event_card"]("C", "T", "All hands", "s", "e", people, "", "", False, VALUE)
    assert action_ids(card) == ["cancel_event"]
    assert ("Attendees", "200 required, 0 optional") in [(f.label, f.value) for f in card.fields]


def test_a_card_whose_button_lost_the_invite_text_has_no_create_button():
    """The payload store refused, and the inline value shed the body."""
    ns = cards()
    inline = json.dumps({"thread_ts": "T", "event": {"subject": "Planning"}})
    card = ns["_event_card"]("C", "T", "Planning", "s", "e", "a@example.edu", "", "", False,
                             inline, body="Agenda")
    assert action_ids(card) == ["cancel_event"]


def test_link_markup_shows_its_target():
    ns = cards()
    card = ns["_event_card"]("C", "T", "<https://evil.example/a|Planning>", "s", "e",
                             "a@example.edu", "<https://evil.example/x|Room 101>", "", False,
                             VALUE, body="See [the doc](https://evil.example/d) and "
                                         "<https://evil.example/e>")
    fields = dict((f.label, f.value) for f in card.fields)
    assert fields["Location"] == "Room 101 (https://evil.example/x)"
    assert fields["Subject"] == "Planning (https://evil.example/a)"
    text = "\n".join(card.lines)
    assert "the doc (https://evil.example/d)" in text and "https://evil.example/e" in text
    assert "|" not in text and "](" not in text


# --- the Reschedule and Delete cards ---------------------------------------

def test_the_reschedule_card_comes_from_the_read_not_the_staged_labels():
    ns = cards(_zulip_fits)
    change = {"subject": "Coffee chat", "when": "Thu Oct 8 2026, 10:00 AM MDT to Thu Oct 8 "
              "2026, 10:30 AM MDT", "attendees": "ira.lindqvist53@example.edu,new@example.edu",
              "optional_attendees": "", "location": ""}
    card = ns["_event_update_card"]("C", "T", read(), change, VALUE)
    fields = dict((f.label, f.value) for f in card.fields)
    assert fields["Event"] == "Budget review"
    assert fields["When"] == read()["when"]
    assert fields["Organizer"] == "you"
    assert fields["New time"] == change["when"]
    # A staged subject that differs from the event's title is a rename.
    assert fields["Rename"] == "Budget review → Coffee chat"
    text = chat.card_text.strip_inert("\n".join(card.lines))
    assert "*Added (1):* new@example.edu" in text
    assert "*Removed (1):* sam.lee@example.edu (optional)" in text
    assert "Ira Lindqvist (ira.lindqvist53@example.edu)" in text
    assert "Outlook mails the change to all 2 attendees." in text
    assert "approve_event_update" in action_ids(card)


def test_a_reschedule_with_the_events_own_title_is_no_rename():
    ns = cards()
    card = ns["_event_update_card"]("C", "T", read(), {"subject": "Budget review",
                                                       "when": "x to y"}, VALUE)
    assert "Rename" not in [f.label for f in card.fields]
    assert "*Attendees (2):* Ira Lindqvist (ira.lindqvist53@example.edu), " \
           "sam.lee@example.edu (optional)" in [chat.card_text.strip_inert(ln)
                                                for ln in card.lines]


def test_the_delete_card_names_organizer_attendees_and_the_cancellation():
    ns = cards(_zulip_fits)
    card = ns["_event_delete_card"]("C", "T", read(), VALUE, staged_subject="Lunch")
    fields = dict((f.label, f.value) for f in card.fields)
    assert fields["Event"] == "Budget review" and fields["When"] == read()["when"]
    text = chat.card_text.strip_inert("\n".join(card.lines))
    assert "The request named this event “Lunch”; the calendar has it as “Budget review”" in text
    assert "sends a cancellation to all 2 attendees" in text
    assert "sam.lee@example.edu (optional)" in text
    theirs = ns["_event_delete_card"]("C", "T", read(
        is_organizer=False, organizer="Ira Lindqvist (ira.lindqvist53@example.edu)"),
        VALUE, staged_subject="Budget review")
    fields = dict((f.label, f.value) for f in theirs.fields)
    assert fields["Organizer"] == "Ira Lindqvist (ira.lindqvist53@example.edu)"
    assert not any("cancellation" in ln or "request named" in ln for ln in theirs.lines)


def test_a_move_or_delete_of_a_huge_meeting_has_no_approval():
    ns = cards(_zulip_fits)
    big = read(attendees=[{"name": f"Person {i}", "address": f"p{i:03d}@example.edu",
                           "type": "required"} for i in range(400)])
    upd = ns["_event_update_card"]("C", "T", big, {"subject": "", "when": "x to y"}, VALUE)
    dele = ns["_event_delete_card"]("C", "T", big, VALUE)
    assert action_ids(upd) == ["cancel_event_update"]
    assert action_ids(dele) == ["cancel_event_delete"]
    for card in (upd, dele):
        assert _zulip_fits(card) and len(slack.render_card(card)[1]) <= 50


# --- the owner-side read ----------------------------------------------------

GRAPH_EVENT = {
    "id": "AAMkEV1", "subject": "Budget review",
    "start": {"dateTime": "2026-10-06T20:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-10-06T21:00:00.0000000", "timeZone": "UTC"},
    "isOrganizer": False, "type": "occurrence", "seriesMasterId": "AAMkSER",
    "organizer": {"emailAddress": {"name": "Ira Lindqvist",
                                   "address": "ira.lindqvist53@example.edu"}},
    "attendees": [{"type": "required", "status": {"response": "none"},
                   "emailAddress": {"name": "Sam Lee", "address": "sam.lee@example.edu"}}],
    "location": {"displayName": "Room 101"}, "body": {"content": "Agenda"},
}


def reader(monkeypatch) -> dict:
    from preprocessor import user_tz
    monkeypatch.setattr(user_tz, "zone", lambda *a: ZoneInfo("America/Denver"))
    import re
    from datetime import datetime
    ns = {"json": json, "re": re, "datetime": datetime, "SCRIPT_DIR": ROOT}
    for fn in ("_plain_mail_id", "_event_time_text", "_event_span_text", "_normalize_outlook_event",
               "_normalize_google_event", "_event_read_fingerprint", "_read_calendar_event"):
        exec(compile(_fn_src(fn), f"ea-chat-bot.py[{fn}]", "exec"), ns)
    return ns


def _run_returning(raw, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return types.SimpleNamespace(stdout=json.dumps(raw), stderr="", returncode=0)
    return run


def test_the_read_normalizes_an_outlook_event_and_shows_it_in_the_users_zone(monkeypatch):
    ns = reader(monkeypatch)
    calls = []
    with patch("subprocess.run", _run_returning(GRAPH_EVENT, calls)):
        ev = ns["_read_calendar_event"]("AAMkEV1")
    assert calls[0][1].endswith("graph_actions.py") and calls[0][2:] == [
        "get-event", "--event-id", "AAMkEV1"]
    assert ev["when"] == "Tue Oct 6 2026, 2:00 PM MDT to Tue Oct 6 2026, 3:00 PM MDT"
    assert ev["organizer"] == "Ira Lindqvist (ira.lindqvist53@example.edu)"
    assert ev["is_organizer"] is False and ev["series"] == "occurrence"
    assert ev["attendees"] == [{"name": "Sam Lee", "address": "sam.lee@example.edu",
                                "type": "required"}]
    assert len(ev["fingerprint"]) == 64


def test_the_fingerprint_ignores_responses_and_sees_every_shown_change(monkeypatch):
    ns = reader(monkeypatch)

    def fp(raw):
        with patch("subprocess.run", _run_returning(raw)):
            return ns["_read_calendar_event"]("AAMkEV1")["fingerprint"]
    base = fp(GRAPH_EVENT)
    accepted = json.loads(json.dumps(GRAPH_EVENT))
    accepted["attendees"][0]["status"] = {"response": "accepted"}
    assert fp(accepted) == base
    for path, value in ((("start", "dateTime"), "2026-10-07T20:00:00.0000000"),
                        (("subject",), "Budget review (moved)"),
                        (("location", "displayName"), "Room 202"),
                        (("body", "content"), "New agenda"),
                        (("isOrganizer",), True)):
        changed = json.loads(json.dumps(GRAPH_EVENT))
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert fp(changed) != base, path
    added = json.loads(json.dumps(GRAPH_EVENT))
    added["attendees"].append({"type": "required", "emailAddress": {"address": "x@example.edu"}})
    assert fp(added) != base


def test_a_google_read_uses_the_owner_side_verb(monkeypatch):
    ns = reader(monkeypatch)
    raw = {"id": "g1", "summary": "Night class",
           "start": {"dateTime": "2027-02-11T11:00:00-07:00"},
           "end": {"dateTime": "2027-02-11T12:15:00-07:00"},
           "organizer": {"email": "me@example.com", "self": True},
           "attendees": [{"email": "kid@example.com", "optional": True}],
           "recurringEventId": "g0"}
    calls = []
    with patch("subprocess.run", _run_returning(raw, calls)):
        ev = ns["_read_calendar_event"]("g1", "me@example.com")
    assert calls[0][1].endswith("google_calendar_client.py")
    assert calls[0][2:] == ["get-event", "--email", "me@example.com", "--event-id", "g1"]
    assert ev["is_organizer"] is True and ev["series"] == "occurrence"
    assert ev["attendees"] == [{"name": "", "address": "kid@example.com", "type": "optional"}]
    assert ev["when"].startswith("Thu Feb 11 2027, 11:00 AM MST")


def test_an_unreadable_event_is_an_error(monkeypatch):
    ns = reader(monkeypatch)
    with patch("subprocess.run", _run_returning({"error": "404 Not Found"})):
        assert ns["_read_calendar_event"]("AAMkGONE")["error"] == "404 Not Found"
    assert ns["_read_calendar_event"]("")["error"]


def test_get_event_is_owner_side_only(tmp_path, monkeypatch):
    """ea-bot cannot call the Google single-event read: the mediator knows
    no such verb, so forward_or_none refuses it."""
    # google_calendar_client creates its auth dir at import: keep it in a
    # sandbox, and import here so the other files' sandboxes are not
    # pre-empted at collection.
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    import google_calendar_client as gcc
    import mail_mediation as M
    sub = next(a for a in gcc.build_parser()._actions if a.dest == "command")
    assert "get-event" in sub.choices
    assert not M.is_safe("google_calendar_client", "get-event")
    assert not M.is_gated("google_calendar_client", "get-event")


# --- the posters ------------------------------------------------------------

def _poster_ns(current: dict):
    posted, delivered, payloads = [], [], []
    ns = cards()
    ns.update({
        "log": types.SimpleNamespace(warning=lambda *a: None),
        "FRONTEND": types.SimpleNamespace(post=lambda **kw: posted.append(kw)),
        "_staged_google_target": lambda *a, **k: ("", True),
        "_read_calendar_event": lambda event_id, google="": dict(current),
        "_event_span_text": lambda s, e, *a: f"{s} to {e}",
        "_event_time_local": lambda v: v,
        "_staged_button_value": lambda base, payload: payloads.append(payload) or VALUE,
        "_deliver_confirmation": lambda ch, t, card, what, kind: delivered.append(card),
    })
    for fn in ("_refuse_event_card", "_post_event_update_confirmation",
               "_post_event_delete_confirmation"):
        exec(compile(_fn_src(fn), f"ea-chat-bot.py[{fn}]", "exec"), ns)
    return ns, posted, delivered, payloads


def test_the_posters_render_from_the_read_and_store_its_fingerprint():
    ns, posted, delivered, payloads = _poster_ns(read())
    ns["_post_event_update_confirmation"]("C", "T", event_id="AAMkEV1", subject="Lunch",
                                          new_start="2026-10-08T10:00", new_end="2026-10-08T10:30")
    ns["_post_event_delete_confirmation"]("C", "T", event_id="AAMkEV1", subject="Lunch",
                                          start="2026-01-01T00:00", end="2026-01-01T01:00")
    assert not posted and len(delivered) == 2
    for card in delivered:
        text = rendered(card)
        assert "Budget review" in text and "ira.lindqvist53@example.edu" in text
        assert "2026-01-01" not in text
    assert [p["event_fingerprint"] for p in payloads] == [FP, FP]


def test_an_unreadable_event_gets_no_button():
    ns, posted, delivered, payloads = _poster_ns({"error": "404 Not Found"})
    ns["_post_event_update_confirmation"]("C", "T", event_id="AAMkGONE", subject="Lunch")
    ns["_post_event_delete_confirmation"]("C", "T", event_id="AAMkGONE", subject="Lunch")
    assert not delivered and not payloads
    assert "No Reschedule button" in posted[0]["text"] and "404" in posted[0]["text"]
    assert "No Delete button" in posted[1]["text"]


# --- the click --------------------------------------------------------------

def _click_ns(current: dict):
    statuses, recards, runs = [], [], []
    ns = {"json": json, "SCRIPT_DIR": ROOT,
          "log": types.SimpleNamespace(exception=lambda *a: None, warning=lambda *a: None),
          "_csv": lambda v: v if isinstance(v, str) else ",".join(v),
          "_remote_llm_note": lambda *a: None,
          "_replace_buttons_with_status": lambda ch, ts, text: statuses.append(text),
          "FRONTEND": types.SimpleNamespace(post=lambda **kw: statuses.append(kw["text"])),
          "_read_calendar_event": lambda event_id, google="": dict(current),
          "_post_event_update_confirmation": lambda *a, **kw: recards.append(("update", kw)),
          "_post_event_delete_confirmation": lambda *a, **kw: recards.append(("delete", kw))}
    for fn in ("_event_unchanged_since_card", "_recard_event_change",
               "_execute_event_update", "_execute_event_delete"):
        exec(compile(_fn_src(fn), f"ea-chat-bot.py[{fn}]", "exec"), ns)

    def run(cmd, **kw):
        runs.append(cmd)
        out = ({"token": "TOK"} if "request-confirm" in cmd
               else {"status": "event_updated" if "update-event" in cmd else "deleted"})
        return types.SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)
    return ns, statuses, recards, runs, run


STAGED = {"event_id": "AAMkEV1", "subject": "Budget review", "start": "2026-10-08T10:00",
          "end": "2026-10-08T10:30", "attendees": "", "location": "", "event_fingerprint": FP}


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_a_changed_event_at_click_time_recards_and_changes_nothing(verb):
    ns, statuses, recards, runs, run = _click_ns(read(fingerprint="b" * 64))
    with patch("subprocess.run", run):
        ok = ns[f"_execute_event_{verb}"]("C", "T", "111.1", dict(STAGED), dm_channel="DM")
    assert ok is False and runs == [], "no token is minted and no verb runs"
    assert "Nothing was changed" in statuses[-1] and "not the same" in statuses[-1]
    assert recards and recards[0][0] == verb
    assert recards[0][1]["event_id"] == "AAMkEV1"


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_a_card_without_a_fingerprint_changes_nothing(verb):
    ns, statuses, recards, runs, run = _click_ns(read())
    staged = {k: v for k, v in STAGED.items() if k != "event_fingerprint"}
    with patch("subprocess.run", run):
        assert ns[f"_execute_event_{verb}"]("C", "T", "111.1", staged) is False
    assert runs == [] and "Nothing was changed" in statuses[-1] and recards


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_an_event_that_cannot_be_read_again_changes_nothing(verb):
    ns, statuses, recards, runs, run = _click_ns({"error": "404 Not Found"})
    with patch("subprocess.run", run):
        assert ns[f"_execute_event_{verb}"]("C", "T", "111.1", dict(STAGED)) is False
    assert runs == [] and "could not read the event again" in statuses[-1]


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_an_unchanged_event_goes_through(verb):
    ns, statuses, recards, runs, run = _click_ns(read())
    with patch("subprocess.run", run):
        assert ns[f"_execute_event_{verb}"]("C", "T", "111.1", dict(STAGED)) is True
    assert len(runs) == 2 and "request-confirm" in runs[0] and not recards


# --- cut fields, forged lines -----------------------------------------------

def _no_approval(card) -> bool:
    return not any(a.id.startswith("approve_") for a in card.actions)


def test_a_field_the_frontends_cut_withholds_the_create_button():
    """The frontends cut Subject, Location and Repeats at FIELD_CHARS; a
    card that shows a cut marker there offers no Create."""
    limit = chat.card_text.FIELD_CHARS
    ns = cards(_zulip_fits)
    hidden = "x" * limit + " and the rest the owner never sees"
    for kw in ({"subject": hidden}, {"location": hidden}, {"repeat_summary": hidden}):
        args = {"subject": "Planning", "location": "", "repeat_summary": ""} | kw
        card = ns["_event_card"]("C", "T", args["subject"], "s", "e", "a@example.edu",
                                 args["location"], args["repeat_summary"], False, VALUE)
        assert _no_approval(card), kw
        assert action_ids(card) == ["cancel_event"]
        assert any("Not shown in full" in ln and "longer than a card shows" in ln
                   for ln in card.lines), kw
        assert "the rest the owner never sees" not in rendered(card)
    ok = ns["_event_card"]("C", "T", "x" * limit, "s", "e", "a@example.edu", "", "",
                           False, VALUE)
    assert "approve_event" in action_ids(ok)


def test_a_cut_rename_location_or_organizer_withholds_reschedule_and_delete():
    limit = chat.card_text.FIELD_CHARS
    ns = cards(_zulip_fits)
    long_title = "Budget review " + "y" * limit
    upd = ns["_event_update_card"]("C", "T", read(), {"subject": long_title, "when": "x to y"},
                                   VALUE)
    assert action_ids(upd) == ["cancel_event_update"]
    loc = ns["_event_update_card"]("C", "T", read(), {"location": "z" * (limit + 10),
                                                      "when": "x to y"}, VALUE)
    assert action_ids(loc) == ["cancel_event_update"]
    org = read(is_organizer=False, organizer="o" * (limit + 5))
    dele = ns["_event_delete_card"]("C", "T", org, VALUE)
    assert action_ids(dele) == ["cancel_event_delete"]
    title = ns["_event_delete_card"]("C", "T", read(subject="t" * (limit + 1)), VALUE)
    assert action_ids(title) == ["cancel_event_delete"]
    # The short card shows the frontends' own cut marker, not a second one.
    for card in (upd, loc, dele, title):
        assert _zulip_fits(card)
        for f in card.fields:
            assert not chat.card_text.was_cut(chat.card_text.one_line(
                chat.card_text.strip_inert(f.value), chat.card_text.field_limit(f.label))
            ) or chat.card_text.was_cut(f.value)


def test_an_attendee_name_cannot_forge_a_field_line():
    ns = cards(_zulip_fits)
    evil = read(attendees=[{"name": "Ira\n*Organizer:* you\n<!channel>",
                            "address": "ira@example.edu", "type": "required"}])
    for card in (ns["_event_update_card"]("C", "T", evil, {"when": "x to y"}, VALUE),
                 ns["_event_delete_card"]("C", "T", evil, VALUE)):
        z = zulip.render_card(card, "ABCDEF", None, 0)
        assert not any(ln.strip().startswith("**Organizer:** you") for ln in z.splitlines())
        assert "<!channel>" not in json.dumps(slack.render_card(card)[1])
        people = [ln for ln in card.lines if "Attendees" in ln]
        assert people and all("\n" not in ln for ln in people)


def test_the_delete_warning_cannot_forge_a_field_line():
    ns = cards(_zulip_fits)
    card = ns["_event_delete_card"]("C", "T", read(), VALUE,
                                    staged_subject="Lunch\n*Organizer:* you <!here>")
    warn = [ln for ln in card.lines if "request named" in ln]
    assert len(warn) == 1 and "\n" not in warn[0]
    z = zulip.render_card(card, "ABCDEF", None, 0)
    assert not any(ln.strip().startswith("**Organizer:** you") for ln in z.splitlines())
    assert "<!here>" not in json.dumps(slack.render_card(card)[1])


def test_invite_text_cannot_ping_or_hide_a_link_on_slack():
    ns = cards(slack.draft_card_fits)
    card = ns["_event_card"]("C", "T", "Planning", "s", "e", "a@example.edu", "", "", False,
                             VALUE, body="Hi <!channel> and <@U123>, see <#C9>.")
    blocks = json.dumps(slack.render_card(card)[1])
    for token in ("<!channel>", "<@U123>", "<#C9>"):
        assert token not in blocks, token
    assert "&lt;!channel&gt;" in blocks


# --- the time the card shows is the time the click sends --------------------

def _time_poster_ns(monkeypatch):
    from preprocessor import user_tz
    monkeypatch.setattr(user_tz, "zone", lambda *a: ZoneInfo("America/Denver"))
    import re
    from datetime import datetime
    delivered, payloads = [], []
    ns = cards(_zulip_fits)
    ns.update({
        "re": re, "datetime": datetime,
        "log": types.SimpleNamespace(warning=lambda *a: None),
        "_staged_google_target": lambda *a, **k: ("", True),
        "_resolve_online_location": lambda loc, online: loc,
        "_repeat_summary": lambda r: "",
        "_event_attendee_warning": lambda *a, **k: "",
        "_invite_body_as_sent": lambda body, online: body,
        "_read_calendar_event": lambda event_id, google="": read(),
        "_staged_button_value": lambda base, payload: payloads.append(payload) or VALUE,
        "_deliver_confirmation": lambda ch, t, card, what, kind: delivered.append(card),
    })
    for fn in ("_event_time_local", "_event_time_text", "_event_span_text",
               "_post_event_confirmation", "_post_event_update_confirmation"):
        exec(compile(_fn_src(fn), f"ea-chat-bot.py[{fn}]", "exec"), ns)
    return ns, delivered, payloads


def test_a_staged_offset_is_converted_once_for_the_card_and_the_payload(monkeypatch):
    """A time staged with an offset other than the user's zone: the card
    and the stored payload hold the same local time, which the gated verb
    sends with the user's zone (graph_client.to_user_local)."""
    ns, delivered, payloads = _time_poster_ns(monkeypatch)
    local = ns["_event_time_local"]
    assert local("2026-10-20T09:00:00-04:00") == "2026-10-20T07:00:00"
    assert local("2026-10-20T15:00:00Z") == "2026-10-20T09:00:00"
    for same in ("2026-10-20T09:00:00", "2026-10-20", "next Tuesday", ""):
        assert local(same) == same
    ns["_post_event_confirmation"]("C", "T", "Sync", "2026-10-20T09:00:00-04:00",
                                   "2026-10-20T10:00:00-04:00", "a@example.edu")
    assert (payloads[-1]["start"], payloads[-1]["end"]) == ("2026-10-20T07:00:00",
                                                            "2026-10-20T08:00:00")
    when = dict((f.label, f.value) for f in delivered[-1].fields)["When"]
    assert when == "Tue Oct 20 2026, 7:00 AM MDT to Tue Oct 20 2026, 8:00 AM MDT"
    ns["_post_event_update_confirmation"]("C", "T", event_id="AAMkEV1", subject="Budget review",
                                          new_start="2026-10-20T15:00:00Z",
                                          new_end="2026-10-20T16:00:00+00:00")
    assert (payloads[-1]["start"], payloads[-1]["end"]) == ("2026-10-20T09:00:00",
                                                            "2026-10-20T10:00:00")
    new = dict((f.label, f.value) for f in delivered[-1].fields)["New time"]
    assert new == "Tue Oct 20 2026, 9:00 AM MDT to Tue Oct 20 2026, 10:00 AM MDT"
