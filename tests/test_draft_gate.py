"""The owner's decisions are not a model's: draft_gate and the paths that
carry the owner's word on a thread.

The fault this pins: the owner closed a row in his own words ("Not
joining. No response."). A follow-up on the same conversation four days
later made a new row, and the morning briefing drafted a reply-all to
two dozen people that said yes. The new row carried nothing of the
decision, the yes-or-no ask was answered by the model, and no rule looked
at the invitation or at the size of the reply-all.

All names, addresses and mail here are made up; nothing reaches Graph or
the live ledger.
"""
import io
import json
import os
import sqlite3
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import closure  # noqa: E402
import draft_gate  # noqa: E402
import ledger  # noqa: E402
import owner_trust  # noqa: E402
import remote_briefing as rb  # noqa: E402
import remote_llm as rl  # noqa: E402
import reply_scan  # noqa: E402

NOW = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)          # Thu 04:00 MT
CONV = "conv-pilot-program"
FIRST, FOLLOW_UP = "msg-invite-1", "msg-invite-2"
SENDER = "pat.rivera@example.edu"
OWNER = "owner@example.edu"
CROWD = [f"member{i}@example.edu" for i in range(23)]          # the list it went to
ASK_1 = "Decide whether to join the pilot program team and reply by Monday, November 16."
ASK_2 = "Reply to Pat Rivera with a 'Yes' or 'No' regarding participation in the pilot program."
SUBJECT = "Advisory board: invitation to the pilot program"


# ── a ledger with the owner's close in it ─────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    closure.ensure_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.setattr(ledger, "_derive_conversation_id", lambda mid: "")
    return path


def _insert(path, rid, conv=CONV, mid=FIRST, rtype="task", notes="Pat invites the user to the pilot."):
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO ledger (id, type, status, description, source_message_id, conversation_id, "
                 "from_email, date_identified, last_action, notes, metadata, created_at, updated_at) "
                 "VALUES (?, ?, 'open', ?, ?, ?, ?, ?, '', ?, '{}', ?, ?)",
                 (rid, rtype, f"Task: {SUBJECT}", mid, conv, SENDER, "2026-11-11T19:10:09Z", notes,
                  "2026-11-11T19:10:09Z", "2026-11-11T19:10:09Z"))
    conn.commit()
    conn.close()


def _close_in_his_words(path, rid, quote="Not joining. No response.",
                        note="The user will not join the pilot and wants no reply sent."):
    """`ledger.py close --user-said` as the chat turn ran it."""
    conn = sqlite3.connect(str(path))
    res = closure.settle(conn, rid, signal="user_stated", actor="cli:chat", note=note,
                         evidence={"quote": quote})
    conn.commit()
    conn.close()
    assert res["action"] == "closed"


def test_the_owners_close_reaches_a_new_row_on_the_same_thread(db):
    """The follow-up's row (a second detector, keyed by its own message)
    carries his word from the start: in its notes, which every lane reads,
    and as `owner_said`."""
    _insert(db, "task-pilot")
    _close_in_his_words(db, "task-pilot")
    found = ledger.owner_decisions([CONV, "conv-other"])
    assert list(found) == [CONV]
    (d,) = found[CONV]
    assert d["row"] == "task-pilot" and d["signal"] == "user_stated" and d["no_reply"] is True
    assert d["said"] == "Not joining. No response."

    assert ledger.add_entry("priority-followup", "response_needed", f"Priority email: RE: {SUBJECT}",
                            from_email=SENDER, source_message_id=FOLLOW_UP, conversation_id=CONV,
                            notes="Pat asks again for a yes or no by Friday.")
    row = ledger.get_entry("priority-followup")
    assert row["owner_said"][0]["no_reply"] is True
    assert row["notes"].startswith('On ') and \
        'the user said: "Not joining. No response." No reply goes out.' in row["notes"]
    assert row["notes"].endswith("Pat asks again for a yes or no by Friday.")


def test_a_close_he_reopened_by_hand_is_no_decision_but_a_renewal_keeps_it(db):
    _insert(db, "task-pilot")
    _close_in_his_words(db, "task-pilot")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE id = 'task-pilot'").fetchone()
    closure.note_renew(conn, row, received="2026-11-18T22:01:37Z")       # a new message
    conn.commit()
    assert ledger.owner_decisions([CONV])[CONV][0]["no_reply"] is True
    closure.note_reopen(conn, row, actor="user")                           # his own reopen
    conn.commit()
    conn.close()
    assert ledger.owner_decisions([CONV]) == {}


def test_his_other_words_travel_but_only_no_reply_words_say_no_reply(db):
    _insert(db, "task-pilot")
    _close_in_his_words(db, "task-pilot", quote="Done, I sent it from my phone.", note="Sent.")
    (d,) = ledger.owner_decisions([CONV])[CONV]
    assert d["no_reply"] is False and d["said"] == "Done, I sent it from my phone."
    for words in ("No reply needed: the owner's click on the Replies card", "don't reply to this",
                  "I'm not attending.", "Decline it.", "Just file it away."):
        assert ledger.says_no_reply(words), words
    for words in ("Sent.", "Handled by phone.", "Reply with the slides."):
        assert not ledger.says_no_reply(words), words


def test_the_reply_queue_entry_carries_his_word(db):
    _insert(db, "task-pilot")
    _close_in_his_words(db, "task-pilot")
    brief = {"needs_reply": True, "state": "needs-reply", "ask": ASK_2, "scheduling": "none",
             "conversation_id": CONV, "user_replied_after_latest": False,
             "messages": [{"id": FIRST, "from": SENDER, "own": False, "received": "2026-11-11T19:01:16Z",
                           "state": "decide", "gist": "Pat invites the user to the pilot."},
                          {"id": FOLLOW_UP, "from": SENDER, "own": False, "received": "2026-11-18T22:01:37Z",
                           "state": "needs-reply", "gist": "Pat asks again for a yes or no."}]}
    rows = ledger.with_owner_decisions([{"id": "priority-followup", "type": "response_needed",
                                         "status": "open", "conversation_id": CONV,
                                         "from_email": SENDER, "thread_brief": brief}])
    (e,) = ledger.reply_backlog(rows, NOW.date(), draft_convs=())
    assert e["owner_said"][0]["no_reply"] is True
    assert [m.get("state") for m in e["thread"]] == ["decide", "needs-reply"]


# ── the morning run on the follow-up ──────────────────────────────────

@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": OWNER}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rb, "EA_DIR", ea)
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(rb, "_rules", lambda: "")
    monkeypatch.setattr(rb, "_profile", lambda: "The user: a professor.")
    monkeypatch.setattr(rb._institution, "safety_rail_domain", lambda: "example.edu")
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "drafting": "remote", "briefings": "remote", "shadow_until": "2099-01-01"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


SAID = [{"row": "task-pilot", "at": "2026-11-14", "signal": "user_stated",
         "said": "Not joining. No response.", "no_reply": True}]


def _brief():
    return {"needs_reply": True, "state": "needs-reply", "ask": ASK_2, "scheduling": "none",
            "conversation_id": CONV, "deadline": "2026-11-20", "last_inbound": "2026-11-18T22:01:37Z",
            "user_replied_after_latest": False, "subject": SUBJECT,
            "people": [{"name": "Pat Rivera", "role": "Director, Research Engagement"}],
            "messages": [{"id": FIRST, "from": SENDER, "own": False, "received": "2026-11-11T19:01:16Z",
                          "state": "decide", "ask": ASK_1, "gist": "Pat invites the user to the pilot."},
                         {"id": FOLLOW_UP, "from": SENDER, "own": False, "received": "2026-11-18T22:01:37Z",
                          "state": "needs-reply", "ask": ASK_2, "gist": "Pat asks again for a yes or no."}]}


def _nov19(*, decided=True, asked=False):
    """The morning's input as the preprocessor wrote it: the follow-up
    as an RSVP item (which named no conversation), its new row and its
    reply-queue entry. `decided`: the owner's close of Nov 14 is in the
    ledger. `asked`: an earlier run already asked him about this message."""
    row = {"id": "priority-followup", "type": "response_needed", "status": "open",
           "description": f"Priority email: RE: {SUBJECT}", "from_email": SENDER,
           "from_name": "Pat Rivera", "source_message_id": FOLLOW_UP, "conversation_id": CONV,
           "notes": "Flags: urgent. Pat asks again for a Yes or No by Friday.",
           "thread_brief": _brief()}
    if decided:
        row["owner_said"] = SAID
    if asked:
        row["reply_queue"] = {"inbound": "2026-11-18T22:01:37Z", "outcome": "decision",
                              "note": "They asked again."}
    entry = dict(ledger.reply_backlog([row], NOW.date(), draft_convs=())[0])
    return {
        "today_top": [], "new_needs_response": [], "new_dropped_threads": [],
        "pending_rsvps": [{"message_id": FOLLOW_UP, "subject": f"RE: {SUBJECT}", "from": SENDER,
                           "from_name": "Pat Rivera", "received": "2026-11-18T22:01:37Z",
                           "is_calendar_invite": False, "to": [OWNER] + CROWD[:12], "cc": CROWD[12:],
                           "body_preview": "Pat asks again for a Yes or No by Friday.",
                           "remote_record": {"state": "needs-reply", "ask": ASK_2,
                                             "deadline": "2026-11-20", "scheduling": "none"}}],
        "open_obligations": [row], "reply_backlog": [entry],
        "owner_decisions": {CONV: SAID} if decided else {},
        "todays_events": [], "availability_merged": [], "recently_closed": [],
        "delta_summary": {"open_obligations": 1, "total_inbox": 10}}


# What the model planned on Nov 19: a yes for the owner.
YES_PLAN = {"get_done_today": [{"ref": "V1", "when": "first thing", "why": "Pat needs a yes or no"}],
            "flexes": [], "needs_you": [], "escalate": [],
            "drafts": [{"ref": "V1", "reply_must_say":
                        "Yes, I would like to participate in the pilot program."}]}


def _model(plan, replies=None):
    def chat(messages, **kw):
        chat.calls.append(kw.get("purpose"))
        if kw.get("purpose") == "briefing-backlog":
            chat.asked = [i["ref"] for i in json.loads(messages[1]["content"])["items"]]
            return {"replies": [{"ref": r, "action": a[0], "text": a[1], "choices": list(a[2:])}
                                for r, a in (replies or {}).items()]}, {"latency_ms": 5}
        chat.items = json.loads(messages[1]["content"])["items"]
        return json.loads(json.dumps(plan)), {"latency_ms": 5}
    chat.calls, chat.asked, chat.items = [], [], []
    return chat


def _marks():
    def mark(ledger_id, inbound, outcome, *, now, draft_id="", note="", choices=()):
        mark.seen.append((ledger_id, outcome, note, list(choices)))
        return True
    mark.seen = []
    return mark


def _no_draft(*a, **k):
    pytest.fail("a reply was drafted against the owner's decision")


def test_nov19_a_follow_up_on_a_thread_he_declined_gets_one_quiet_question(priv):
    """The replay: the model drafts a yes; code makes no draft, keeps it off
    the do-list, and asks once whether no reply still holds."""
    b, chat, mark = _nov19(), _model(YES_PLAN), _marks()
    text, rec = rb.run("morning-briefing", b, run_id="cron-morning-briefing-1", now=NOW, chat=chat,
                       drafter=_no_draft, audit=lambda *a, **k: {}, mark=mark)
    (item,) = [i for i in chat.items if i["ref"] == "V1"]
    assert item["owner_said"] == 'On Nov 14 the user said: "Not joining. No response." No reply goes out.'
    assert rec["drafts"] == 0 and "Drafts ready" not in text
    do_list = text.split("*Get done today*")[1].split("\n\n")[0]
    assert "pilot program" not in do_list                # no to-do on his no-reply thread
    line = "They asked again. On Nov 14 you said no reply. Still no reply?"
    assert f"- *RE: {SUBJECT}* (Pat Rivera): {line}" in text and "participate" not in text
    assert mark.seen == [("priority-followup", "decision", line, [])]
    assert chat.calls == ["briefing-morning"]          # the queue entry is the same thread


def test_nov19_the_question_is_asked_once(priv):
    b = _nov19(asked=True)
    plan = dict(YES_PLAN, needs_you=[])
    text, rec = rb.run("morning-briefing", b, run_id="r", now=NOW, chat=_model(plan),
                       drafter=_no_draft, audit=lambda *a, **k: {}, mark=_marks())
    assert "They asked again" not in text and rec["drafts"] == 0


def test_a_decision_the_model_raised_on_his_no_reply_thread_is_the_quiet_question(priv):
    plan = dict(YES_PLAN, drafts=[], needs_you=[{"ref": "V1", "decision": "Say yes or no to Pat."}])
    text, _rec = rb.run("morning-briefing", _nov19(), run_id="r", now=NOW, chat=_model(plan),
                        drafter=_no_draft, audit=lambda *a, **k: {}, mark=_marks())
    assert "On Nov 14 you said no reply. Still no reply?" in text and "Say yes or no" not in text


def test_without_his_word_a_yes_or_no_ask_is_still_his_decision(priv):
    """Item 2: no earlier decision, but the ask is yes or no, the reply
    accepts an invitation and goes to two dozen people. No draft; a
    decision with Yes and No buttons whose line carries no model answer."""
    b, mark = _nov19(decided=False), _marks()
    text, rec = rb.run("morning-briefing", b, run_id="r", now=NOW, chat=_model(YES_PLAN),
                       drafter=_no_draft, audit=lambda *a, **k: {}, mark=mark)
    assert rec["drafts"] == 0 and "Drafts ready" not in text
    ((rid, outcome, note, choices),) = mark.seen
    assert (rid, outcome, choices) == ("priority-followup", "decision", ["Yes", "No"])
    assert note == f"Reply needs you (the answer is your decision): They ask: {ASK_2}"
    assert "I would like to participate" not in text


def test_the_reply_queue_never_hands_his_no_reply_thread_to_the_model(priv):
    b = _nov19()
    cands = rb.backlog_candidates(b)
    chat, mark = _model({}, {"Q1": ("draft", "Yes, I would like to participate.")}), _marks()
    out, stats = rb.clear_backlog(cands, rb.backlog_refs(cands, set()), rules="", now=NOW,
                                  run_id="r", chat=chat, drafter=_no_draft, mark=mark)
    assert chat.calls == [] and stats["decisions"] == 1 and stats["drafted"] == 0
    assert out[0]["decision"] == "They asked again. On Nov 14 you said no reply. Still no reply? (1d)"
    assert mark.seen[0][1] == "decision" and "Still no reply?" in mark.seen[0][2]


def test_the_reply_queue_turns_a_yes_or_no_into_a_decision(priv):
    b = _nov19(decided=False)
    cands = rb.backlog_candidates(b)
    chat, mark = _model({}, {"Q1": ("draft", "Yes, I would like to participate.")}), _marks()
    out, stats = rb.clear_backlog(cands, ["Q1"], rules="", now=NOW, run_id="r", chat=chat,
                                  drafter=_no_draft, mark=mark)
    assert stats["drafted"] == 0 and mark.seen[0][3] == ["Yes", "No"]
    assert mark.seen[0][2] == f"Reply needs you (the answer is your decision): They ask: {ASK_2}"


# ── the gate's rules one by one ───────────────────────────────────────

def _item(**over):
    base = {"kind": "reply", "title": "Slides for the review", "ask": "Send the slides.",
            "state": "needs-reply", "scheduling": "none", "from": "lee@example.edu",
            "to": [OWNER], "cc": [], "owner_said": []}
    base.update(over)
    return base


def test_an_easy_reply_still_drafts():
    assert draft_gate.check(_item(), "Yes, the slides go out tonight.", own={OWNER}) is None
    assert draft_gate.check(_item(ask="Confirm the date."), "Yes, the date works.", own={OWNER}) is None
    assert draft_gate.check(_item(ask="Let me know whether Tuesday works.", scheduling="request"),
                            "Happy to join the call on Tuesday at 2.", own={OWNER}) is None


def test_the_rules_each_route_to_the_owner():
    said = draft_gate.check(_item(owner_said=SAID), "Sure.", own={OWNER})
    assert said["rule"] == "no_reply" and said["choices"] == []
    choice = draft_gate.check(_item(ask=ASK_2), "Yes.", own={OWNER})
    assert choice["rule"] == "owner_choice" and choice["choices"] == ["Yes", "No"]
    assert draft_gate.check(_item(state="decide"), "Yes.", own={OWNER})["rule"] == "owner_choice"
    assert draft_gate.check(_item(ask="Pat invites the user to join the advisory board."), "Thanks.",
                            own={OWNER})["rule"] == "owner_choice"
    accept = draft_gate.check(_item(), "I'd be happy to serve on the panel.", own={OWNER})
    assert accept["rule"] == "invitation" and accept["why"] == "it accepts an invitation"
    decline = draft_gate.check(_item(), "Unfortunately I can't join the board this year.", own={OWNER})
    assert decline["why"] == "it declines an invitation"
    many = draft_gate.check(_item(to=[OWNER] + CROWD[:9]), "Yes, the slides go out tonight.", own={OWNER})
    assert many["rule"] == "many" and many["why"] == "a reply to all 10 people on it"
    assert draft_gate.check(_item(to=[OWNER] + CROWD[:8]), "Yes, the slides go out tonight.",
                            own={OWNER}) is None
    assert draft_gate.check(_item(ask="Confirm the date."), "I accept the invitation to join the "
                            "advisory board.", own={OWNER})["rule"] == "invitation"


# ── meeting scheduling is the scheduling lane's, untouched ────────────

MEETING_YES = "Yes, happy to meet. Would Tuesday at 2 or Thursday at 10 work for you?"


def test_a_one_to_one_meeting_yes_with_times_still_drafts():
    """A meeting request from one person: the gate leaves it to the
    scheduling lane and the morning sweep, whatever the record's words or
    state, and whether the item came as a reply or an RSVP."""
    for kind in ("reply", "rsvp", "backlog"):
        for ask, state in (("Find a time to meet next week.", "scheduling"),
                           ("Decide on a time for the project meeting.", "needs-reply"),
                           ("Let me know whether Tuesday or Thursday works.", "decide"),
                           ("Reply yes or no: can you meet Tuesday at 2?", "needs-reply")):
            item = _item(kind=kind, ask=ask, state=state, scheduling="request",
                         to=[OWNER], cc=["assistant@example.edu"])
            for says in (MEETING_YES, "Happy to join the call on Tuesday at 2.",
                         "Yes, I can attend on Tuesday at 2.", "Thanks, I accept. Tuesday at 2 works.",
                         "Unfortunately I cannot make Tuesday; Thursday at 10 works."):
                assert draft_gate.check(item, says, own={OWNER}) is None, (kind, ask, says)
    # An RSVP that is no meeting and no participation choice drafts too.
    assert draft_gate.check(_item(kind="rsvp", ask="Confirm you received the agenda."), "Received, thanks.",
                            own={OWNER}) is None


def test_a_meeting_reply_in_a_scheduled_lane_is_not_refused():
    """The morning sweep's create-reply on a scheduling row or a meeting
    thread: no rule but the owner's no-reply word and the big reply-all."""
    sched = {"id": "sched-mtg123", "type": "scheduling",
             "thread_brief": {"state": "decide", "scheduling": "request", "ask": "Decide on a time.",
                              "messages": [{"own": False, "state": "decide"}]}}
    meeting = {"id": "response-mtg456", "type": "response_needed",
               "thread_brief": {"state": "needs-reply", "scheduling": "proposes-times",
                                "ask": "Reply yes or no: does Tuesday at 2 work?"}}
    for row in (sched, meeting):
        for body in (MEETING_YES, "Yes, I can attend on Tuesday at 2."):
            assert draft_gate.cron_refusal(run_id="cron-morning-briefing-1", rows=[row], decisions=[],
                                           body=body, recipient_count=2) == "", (row["id"], body)
    assert draft_gate.cron_refusal(run_id="cron-morning-briefing-1", rows=[sched], decisions=SAID,
                                   body=MEETING_YES, recipient_count=2).startswith("the owner said no reply")
    assert draft_gate.cron_refusal(run_id="cron-morning-briefing-1", rows=[sched], decisions=[],
                                   body=MEETING_YES, recipient_count=12) == \
        "a reply to all 12 people on the thread"


def test_the_scheduling_lanes_never_run_under_a_scheduled_lanes_id():
    """The scheduling channel, the pipeline and the reschedule path write
    under the bot's dispatch ids (hex, or shadow-), never `cron-`."""
    for run_id in ("1a1f37ea1f2-b258d4", "shadow-1a1f37ea1f2-b258d4", "pipe-1763550000", "manual", ""):
        assert not draft_gate.autonomous_run(run_id)
        assert draft_gate.cron_refusal(run_id=run_id, rows=[], decisions=SAID, body="Yes, I will "
                                       "participate in the program.", recipient_count=40) == ""


# ── the last step: create-reply in a scheduled lane ───────────────────

def _reply_args(**over):
    base = dict(message_id=FOLLOW_UP, conversation_id=CONV, cc=None, sender_only=False, to=None,
                content_type="Text", body="Yes, the slides go out tonight.", force=False, ledger_id=[])
    base.update(over)
    return types.SimpleNamespace(**base)


def _create_reply(monkeypatch, run_id, *, people, args=None, decisions=()):
    import graph_actions as ga
    made = []
    monkeypatch.setenv("EA_RUN_ID", run_id)
    monkeypatch.setattr(ga, "_user_addresses", lambda: {OWNER})
    monkeypatch.setattr(ga, "_draft_events", types.SimpleNamespace(record=lambda *a, **k: None))
    monkeypatch.setattr(ga, "_draft_capture", types.SimpleNamespace(capture=lambda *a, **k: None))
    monkeypatch.setattr(ga, "_ea_authored_draft_ids", lambda conv: [])
    monkeypatch.setattr(ledger, "owner_decisions",
                        lambda convs, db_path=None: {CONV: list(decisions)} if decisions else {})
    monkeypatch.setattr(ga, "graph", types.SimpleNamespace(
        conversation_latest_sender=lambda cid: SENDER,
        get_message_full=lambda mid: {
            "conversationId": CONV, "from": {"emailAddress": {"address": SENDER}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in [OWNER] + people],
            "ccRecipients": []},
        create_draft=lambda **k: made.append(k) or {"id": "draft-new"},
        AmbiguousReplyTarget=Exception))
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_create_reply(args or _reply_args())
    return json.loads(buf.getvalue()), made


def test_create_reply_refuses_a_scheduled_lane_a_reply_all_to_many(monkeypatch):
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-20261119-040002", people=CROWD)
    assert out["status"] == "needs_owner" and made == []
    assert out["reason"] == "a reply to all 24 people on the thread"
    # The same reply asked for in a chat turn is the owner's own.
    out, made = _create_reply(monkeypatch, "chat-1700000000.0001", people=CROWD)
    assert out["status"] == "reply_draft_created" and len(made) == 1


def test_create_reply_refuses_a_scheduled_lane_his_no_reply_thread_and_an_acceptance(monkeypatch):
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[], decisions=SAID)
    assert out["status"] == "needs_owner" and made == [] and "said no reply" in out["reason"]
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[], args=_reply_args(
        body="Yes, I would like to participate in the pilot program."))
    assert out["status"] == "needs_owner" and out["reason"] == "the reply accepts an invitation"
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[])
    assert out["status"] == "reply_draft_created" and len(made) == 1


def test_create_draft_holds_the_same_rule_for_a_new_message(monkeypatch):
    import graph_actions as ga
    made = []
    monkeypatch.setenv("EA_RUN_ID", "cron-morning-briefing-1")
    monkeypatch.setattr(ga, "_user_addresses", lambda: {OWNER})
    monkeypatch.setattr(ga, "_draft_events", types.SimpleNamespace(record=lambda *a, **k: None))
    monkeypatch.setattr(ga, "_draft_capture", types.SimpleNamespace(capture=lambda *a, **k: None))
    monkeypatch.setattr(ga, "graph", types.SimpleNamespace(
        create_draft=lambda **k: made.append(k) or {"id": "draft-new"}))
    args = types.SimpleNamespace(subject="The pilot program", to=",".join(CROWD[:12]), cc=None,
                                 body="Thanks, noted.", content_type="Text", ledger_id=[],
                                 force_new_thread=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_create_draft(args)
    assert json.loads(buf.getvalue())["status"] == "needs_owner" and made == []


def test_cron_refusal_reads_the_rows_the_draft_names():
    row = {"id": "priority-followup", "thread_brief": _brief()}
    assert draft_gate.cron_refusal(run_id="cron-x", rows=[row], decisions=[]) == \
        "the answer is the owner's decision"
    assert draft_gate.cron_refusal(run_id="chat-x", rows=[row], decisions=[]) == ""


def test_a_scheduled_lane_never_drafts_on_a_kept_thread(monkeypatch):
    """Rule 5: no scheduled run drafts on a thread kept on the machine, and
    on a Facts only thread only the local writer's body goes. The shared
    check (keep_gate) reads the row's brief, its label and the mail's
    records; a chat turn is not gated here."""
    import keep_gate
    kept = {"id": "kept-review", "thread_brief": {"keep_level": "local"}}
    labelled = {"id": "kept-notice", "kept_from_claude": "listed domain reviews.example.org"}
    for row in (kept, labelled):
        assert draft_gate.cron_refusal(run_id="cron-morning-briefing-1", rows=[row], decisions=[],
                                       keep_records=False) == keep_gate.KEPT_REFUSAL
        assert draft_gate.cron_refusal(run_id="chat-1", rows=[row], decisions=[], keep_records=False) == ""
    records = lambda i: {"c-local": "local", "c-facts": "facts",  # noqa: E731
                         "c-summary": "summary"}.get(i.get("conversation_id"), "full")

    def refused(conv, writer):
        return draft_gate.cron_refusal(run_id="cron-x", conversation_id=conv, rows=[], decisions=[],
                                       keep_records=records, local_writer=writer)
    # On the machine: no scheduled run drafts, whoever writes the body.
    assert refused("c-local", True) == refused("c-local", False) == keep_gate.KEPT_REFUSAL
    # Facts only: the local writer's body goes, a hosted model's does not.
    assert refused("c-facts", True) == "" and refused("c-facts", False) == keep_gate.KEPT_REFUSAL
    for conv in ("c-summary", "c-plain"):
        assert refused(conv, False) == ""
    # create-reply holds it for the morning sweep on Claude or Codex; the
    # remote briefing's own create-reply says its body is the local writer's.
    level = {"now": "facts"}
    monkeypatch.setattr(keep_gate, "_records", lambda: (lambda i: level["now"] if i.get("conversation_id")
                                                       == CONV else "full"))
    monkeypatch.delenv(keep_gate.LOCAL_WRITER_ENV, raising=False)
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[])
    assert out["status"] == "needs_owner" and made == [] and out["reason"] == keep_gate.KEPT_REFUSAL
    monkeypatch.setenv(keep_gate.LOCAL_WRITER_ENV, "local")
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[])
    assert out["status"] == "reply_draft_created" and len(made) == 1
    level["now"] = "local"
    out, made = _create_reply(monkeypatch, "cron-morning-briefing-1", people=[])
    assert out["status"] == "needs_owner" and made == []
    monkeypatch.delenv(keep_gate.LOCAL_WRITER_ENV)
    out, made = _create_reply(monkeypatch, "chat-1700000000.0001", people=[])
    assert out["status"] == "reply_draft_created" and len(made) == 1


def test_the_remote_briefing_turns_a_refused_reply_into_a_decision(priv):
    b = _nov19(decided=False)
    b["pending_rsvps"] = []
    cands = rb.backlog_candidates(b)
    c = dict(cands["Q1"], ask="Send the slides.", thread=[], state="needs-reply")
    refused = lambda cand, says, run_id: {"ok": False, "owner": "a reply to all 24 people on the thread"}
    mark = _marks()
    chat = _model({}, {"Q1": ("draft", "Yes, the slides go out tonight.")})
    out, stats = rb.clear_backlog({"Q1": c}, ["Q1"], rules="", now=NOW, run_id="r", chat=chat,
                                  drafter=refused, mark=mark)
    assert stats["failed"] == 0 and stats["decisions"] == 1
    assert mark.seen[0][2] == ("Reply needs you (a reply to all 24 people on the thread): "
                               "They ask: Send the slides.")


# ── the scan block in chat ────────────────────────────────────────────

def test_the_scan_block_names_his_word_and_says_no_draft():
    row = {"id": "priority-followup", "description": f"Priority email: RE: {SUBJECT}",
           "from_name": "Pat Rivera", "age_days": 1, "ask": ASK_2, "queued": {},
           "scheduling": "none", "owner_said": SAID}
    text, shown, _hidden = reply_scan.block([row])
    assert 'The user decided this thread on 2026-11-14: "Not joining. No response."' in text
    assert "write no draft" in text
