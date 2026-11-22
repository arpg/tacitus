"""Action requests as blocks (docs/webchat.md, Action requests): the folded
words and the urgency (botcore action_requests), and the EA's view of each
request (preprocessor/web_requests.py: the project and deadline from the
ledger and the project store, the event's start from its staged payload).
Made-up data only."""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "botcore"))
sys.path.insert(0, str(ROOT))

from botcore.chat import action_requests as ar  # noqa: E402
from botcore.chat import card_text  # noqa: E402
from preprocessor import web_requests as wr  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400


def msg(kind="card", card=None, at=NOW - 60, **kw):
    return {"kind": kind, "card": card, "posted_at": at, "text": kw.pop("text", ""), "author": "bot", **kw}


def card(kind="send", title=":lock: *Send this reply?*", fields=(), actions=None):
    return {"kind": kind, "title": title, "fields": [{"label": a, "value": b} for a, b in fields],
            "actions": actions if actions is not None else [{"id": "approve_send", "value": {"d": 1}}]}


def test_a_block_reads_the_card_s_labelled_fields_and_falls_back_to_its_title():
    got = ar.block(msg(card=card(fields=[("To", "a@example.test, b@example.test, c@example.test, d@example.test"),
                                         ("CC", "e@example.test"), ("Subject", "Re: Figures")])))
    assert got == {"action": "Send email", "who": "To a@example.test, b@example.test and 2 more",
                   "what": "Re: Figures", "when": ""}
    got = ar.block(msg(card=card("event_update", ":lock: Move this meeting?",
                                 [("Event", "Lab meeting"), ("When", "Mon 9:00"), ("New time", "Tue 9:00"),
                                  ("Organizer", "kim@example.test")])))
    assert got == {"action": "Move meeting", "who": "Organizer kim@example.test", "what": "Lab meeting",
                   "when": "Tue 9:00"}
    # an old card, or one of a plugin's, with no fields: its title
    assert ar.block(msg(card=card("plugin", ":lock: *Upload the letter?*"))) == \
        {"action": "Upload the letter?", "who": "", "what": "", "when": ""}
    assert ar.block(msg(card=card("close", ":lock: Looks done?", actions=[{}, {}, {}])))["action"] == "Close items"
    # a thread that opens with a plain message
    assert ar.block(msg("message", text="Your flight leaves at nine."))["action"] == "Notice"
    assert ar.block(msg("message", text="A quick ask", author="owner"))["action"] == "Your message"
    assert ar.block(msg("file", file={"name": "letter.pdf"})) == \
        {"action": "A file for you", "who": "", "what": "letter.pdf", "when": ""}
    assert ar.block(msg("message", text="A new device asks to reach Tacitus: \"a phone\", pairing code 123 456."))[
        "action"] == "Approve a device"


def test_a_folded_letter_card_removes_internal_markers_but_keeps_real_control_warnings():
    subject = "Recommendation letter for Avery Quinn"
    got = ar.block(msg(card=card("letter", ":memo: *" + card_text.inert(subject) + "*",
                                 [("From", card_text.inert("Avery Quinn")),
                                  ("Subject", card_text.inert(subject)),
                                  ("When", card_text.inert("Next due Dec 1"))])))
    assert got == {"action": "Letter to write", "who": "From Avery Quinn", "what": subject,
                   "when": "Next due Dec 1"}
    # The same fallback for a card without fields: its renderer's
    # delimiters never become visible warnings.
    got = ar.block(msg(card=card("plugin", "Review " + card_text.inert(subject))))
    assert got["action"] == "Review " + subject
    assert all("\ue000" not in v and "\ue001" not in v and "[U+E00" not in v for v in got.values())
    # Untrusted controls were already made visible inside the inert span;
    # removing the builder's delimiters must preserve that warning.
    got = ar.block(msg(card=card(fields=[("Subject", card_text.inert("Budget\u202e <b>x</b>"))])))
    assert got["what"] == "Budget[U+202E] <b>x</b>"


def test_urgency_has_three_levels_from_the_deadline_the_event_and_the_wait():
    fresh = msg(at=NOW - 600)
    yesterday = datetime.fromtimestamp(NOW - DAY).date().isoformat()
    tomorrow = datetime.fromtimestamp(NOW + DAY).date().isoformat()
    cases = [
        (fresh, {}, ("calm", "")),
        (fresh, {"due": yesterday}, ("overdue", "due")),
        (fresh, {"due": tomorrow}, ("soon", "due")),
        (fresh, {"due": "2099-01-01"}, ("calm", "")),
        (fresh, {"starts": datetime.fromtimestamp(NOW - 3600).isoformat()}, ("overdue", "starts")),
        (fresh, {"starts": datetime.fromtimestamp(NOW + 5 * 3600).isoformat()}, ("soon", "starts")),
        (msg(at=NOW - 6.5 * DAY), {}, ("overdue", "expires")),  # its buttons stop within a day
        (msg(at=NOW - 3.5 * DAY), {}, ("soon", "waited")),
        (fresh, {"due": "not a date"}, ("calm", "")),
    ]
    for m, ctx, want in cases:
        got = ar.urgency(m, True, ctx, NOW)
        assert (got["urgency"], got["why"]) == want, (ctx, got)
    assert ar.urgency(msg(at=NOW - 6.5 * DAY), False, {"due": yesterday}, NOW)["urgency"] == "calm"  # answered
    items = [{"urgency": "calm", "posted_at": 5}, {"urgency": "overdue", "posted_at": 1},
             {"urgency": "soon", "posted_at": 2}, {"urgency": "overdue", "posted_at": 3}]
    assert [(j["urgency"], j["posted_at"]) for j in ar.order(items)] == \
        [("overdue", 3), ("overdue", 1), ("soon", 2), ("calm", 5)]


def _db(tmp_path):
    path = tmp_path / "obligations-ledger.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ledger (id TEXT PRIMARY KEY, type TEXT, status TEXT, conversation_id TEXT,
                             deadline TEXT, last_action TEXT);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE project_ledger (project_id TEXT, ledger_id TEXT);
        CREATE TABLE project_messages (project_id TEXT, message_id TEXT, conversation_id TEXT);
    """)
    soon = (date.today() + timedelta(days=1)).isoformat()
    conn.executemany("INSERT INTO ledger VALUES (?,?,?,?,?,?)", [
        ("response-a1", "response_needed", "open", "conv-1", soon, ""),
        ("response-a2", "response_needed", "completed", "conv-1", "2020-01-01", ""),
        ("task-b1", "task", "open", "", "", ""),
    ])
    conn.executemany("INSERT INTO projects VALUES (?,?)", [("grant", "Grant report"), ("panel", "Review panel")])
    conn.execute("INSERT INTO project_ledger VALUES ('grant', 'task-b1')")
    conn.execute("INSERT INTO project_messages VALUES ('panel', 'm1', 'conv-1')")
    conn.commit()
    conn.close()
    return path, soon


def test_the_ea_names_each_request_s_project_deadline_and_start(tmp_path):
    path, soon = _db(tmp_path)
    start = "2026-12-01T10:00:00-07:00"
    items = [
        {"id": "w1", "card": card(actions=[{"id": "approve_send", "value": {"draft_id": "d1", "conversation_id": "conv-1"}}])},
        {"id": "w2", "card": card("close", actions=[{"id": "close_all_proposed", "value": {"ledger_ids": ["task-b1"]}}])},
        {"id": "w3", "card": card("event", actions=[{"id": "approve_event", "value": {"payload_ref": "p1"}}])},
        {"id": "w4", "card": card("replies", actions=[{"id": "reply_draft", "value": '{"row": "missing-x"}'}])},
    ]
    seen = []

    def payload_of(value):
        seen.append(value)
        return {"start": start} if value.get("payload_ref") == "p1" else None
    got = wr.context(items, payload_of=payload_of, db_path=path)
    # the open row of the mail conversation names the deadline; the filed conversation its project
    assert got["w1"] == {"project": "Review panel", "due": soon}
    assert got["w2"] == {"project": "Grant report"}  # an open task with no deadline
    assert got["w3"] == {"starts": start}
    assert "w4" not in got  # a row the ledger does not hold: nothing known
    assert seen == [{"payload_ref": "p1"}]  # only an event's staged payload is read


def test_a_ledger_that_cannot_be_read_gives_nothing(tmp_path):
    got = wr.context([{"id": "w1", "card": card(actions=[{"value": {"ledger_id": "x"}}])}],
                     db_path=tmp_path / "missing" / "nothing.db")
    assert got == {}


def test_the_bot_hands_the_web_chat_its_requests_context():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "_web.request_context = lambda items: _web_requests.context(" in bot
    assert "_staged_button_payload(v, consume=False)" in bot  # the staged event stays for its button


def test_soon_counts_two_working_days_for_a_deadline_or_a_start():
    """Soon is by the end of the second working day after today (Monday to
    Friday). On a Friday a Monday deadline is soon, a Sunday start too (two
    calendar days), a Tuesday deadline too; next Wednesday's is not. On a
    Wednesday, Friday's deadline is soon and Saturday's is not."""
    friday = datetime(2026, 11, 20, 10, 0).timestamp()
    wednesday = datetime(2026, 11, 18, 10, 0).timestamp()
    assert datetime.fromtimestamp(friday).weekday() == 4
    assert datetime.fromtimestamp(ar.soon_until(friday)) == datetime(2026, 11, 24, 23, 59, 59)
    fresh = lambda now: msg(at=now - 600)  # noqa: E731
    cases = [
        (friday, {"due": "2026-11-23"}, "soon"),                     # Monday
        (friday, {"starts": "2026-11-22T15:00:00"}, "soon"),         # Sunday
        (friday, {"due": "2026-11-24"}, "soon"),                     # Tuesday, the second working day
        (friday, {"due": "2026-11-25"}, "calm"),                     # next Wednesday
        (friday, {"starts": "2026-11-25T09:00:00"}, "calm"),
        (wednesday, {"due": "2026-11-20"}, "soon"),                  # Friday
        (wednesday, {"due": "2026-11-21"}, "calm"),                  # Saturday
        (wednesday, {"due": "2026-11-17"}, "overdue"),
    ]
    for now, ctx, want in cases:
        assert ar.urgency(fresh(now), True, ctx, now)["urgency"] == want, (ctx, now)
    # the wait counts calendar days, as the Replies card's rungs do
    assert ar.urgency(msg(at=friday - 3 * DAY), True, {}, friday)["why"] == "waited"


def test_a_question_folds_to_answer_needed_and_a_status_links_on():
    q = card("replies", ":inbox_tray: *Review request*")
    q["question"] = {"text": "Will you review the paper by December 1?", "from": "Pat", "since": "", "about": "x"}
    assert ar.block(msg(card=q)) == {"action": "Answer needed", "who": "From Pat",
                                     "what": "Will you review the paper by December 1?", "when": ""}
    writing = msg("status", text=":hourglass_flowing_sand: Writing the reply: Yes.", updated_at=NOW - 60)
    assert ar.working(writing, NOW) and not ar.working({**writing, "updated_at": NOW - 3600}, NOW)
    assert not ar.working(msg("status", text=":white_check_mark: Sent."), NOW)
    done = msg("status", text=":white_check_mark: The reply is drafted. Its Send request: "
                              "[open it](https://h.example.test/chat/#/dm/t9/w9)")
    assert ar.next_request(done, "dm") == "w9" and ar.next_request(done, "main") == ""
    core = msg("status", text=":speech_balloon: One more answer is needed: <https://h.example.test/chat/#/dm/t8/w8|open it>")
    assert ar.next_request(core, "dm") == "w8"
    assert ar.next_request(msg("card", text="[open it](https://h.example.test/chat/#/dm/t9/w9)"), "dm") == ""
    items = [{"urgency": "overdue", "posted_at": 1}, {"urgency": "calm", "posted_at": 0, "working": True}]
    assert [j.get("working", False) for j in ar.order(items)] == [True, False]


def test_a_request_shows_only_its_own_lines():
    """Two rows' cards share one thread: each takes the later lines with its
    own tag. A card alone in its own thread takes the later untagged lines.
    An untagged line in a shared thread belongs to no request."""
    thread = [{"seq": 1, "id": "w1", "kind": "card", "author": "bot", "text": ""},
              {"seq": 2, "id": "w2", "kind": "card", "author": "bot", "text": ""},
              {"seq": 3, "id": "w3", "kind": "card", "author": "bot", "text": ""},
              {"seq": 4, "id": "w4", "kind": "message", "author": "bot", "text": "Writing A"},
              {"seq": 5, "id": "w5", "kind": "message", "author": "bot", "text": "Writing B"},
              {"seq": 6, "id": "w6", "kind": "message", "author": "bot", "text": "Still working"},
              {"seq": 7, "id": "w7", "kind": "message", "author": "owner", "text": "mine"}]
    tags = {"w2": "reply:a", "w3": "reply:b", "w4": "reply:a", "w5": "reply:b"}
    assert [x["text"] for x in ar.lines_of({"id": "w2", "seq": 2}, thread, tags)] == ["Writing A"]
    assert [x["text"] for x in ar.lines_of({"id": "w3", "seq": 3}, thread, tags)] == ["Writing B"]
    assert ar.lines_of({"id": "w1", "seq": 1}, thread, tags) == []   # the shared thread's parent
    alone = [{"seq": 1, "id": "w1", "kind": "card", "author": "bot", "text": ""},
             {"seq": 2, "id": "w2", "kind": "message", "author": "bot", "text": "Tell me the change"}]
    assert [x["text"] for x in ar.lines_of({"id": "w1", "seq": 1}, alone, {})] == ["Tell me the change"]


def test_a_reply_that_was_not_written_folds_to_its_reason():
    c = card("replies", ":inbox_tray: *Review request*")
    c["problem"] = {"text": "The thread has no address to answer.", "from": "Pat", "since": "", "about": "x"}
    c["question"] = {}
    assert ar.block(msg(card=c)) == {"action": "Not written", "who": "From Pat",
                                     "what": "The thread has no address to answer.", "when": ""}
