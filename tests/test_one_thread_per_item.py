"""One tracked item, one thread: the regression for a request that fanned
out over two days into eight posts on four screens.

The shape of that trace, with made-up names and mail: a briefing hands a
workshop request to Claude in a new EA-channel thread, and the turn drafts
the work as a document; the weekly audit lists the row under Critical and
again under its hand-offs; a project room's slice lists one thread twice;
the next morning's run records that the message needs no reply until the
owner reviews the document, yet the Replies list reads "no draft yet"; a
colleague's forwarded copy in the same mail conversation trips the priority
alert, which opens a second EA-channel thread. Each step is pinned here at
the seam that now keeps the item to one place.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(HERE))
import briefing_rooms as br  # noqa: E402
import chat_notify  # noqa: E402
import item_threads  # noqa: E402
import ledger  # noqa: E402
import remote_audit as ra  # noqa: E402
import slack_notifier  # noqa: E402
from test_chat_notify import env, web_env  # noqa: E402,F401  (fixtures)

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
CONV = "AAQkConvWorkshop0001="
ROW = "deadline-WorkshopRow1="
NOW = datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc)


def _fn(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    return BOT_SRC[start:BOT_SRC.index("\ndef ", start + 1)]


def _alert(message_id: str, conv: str = CONV, subject: str = "Workshop homework") -> dict:
    return {"message_id": message_id, "conversation_id": conv, "subject": subject,
            "from": "pat@example.edu", "from_name": "Pat Example", "received": "2026-03-18T16:03:00Z",
            "body_preview": "Please fill in the capability table.", "is_urgent": True}


# ── the EA channel: one thread per mail conversation ──────────────────

def test_the_alert_on_a_handed_off_conversation_joins_its_thread(web_env):  # noqa: F811
    """The hand-off opened the conversation's thread; the forwarded copy's
    alert goes into it, the same alert again posts nothing, and another
    conversation's alert opens its own thread."""
    store = web_env.wf.WebStore(web_env.store)
    handoff = chat_notify.post("ea-assistant", ":arrow_up: *Workshop homework*\nSent to Claude.",
                               _bypass_quiet=True)
    # what the bot's hand-off tick records (_item_thread_record)
    ns = {"FRONTEND": types.SimpleNamespace(name="web"), "CHANNEL_ID": "main",
          "log": types.SimpleNamespace(warning=lambda *a: None)}
    exec(_fn("_item_thread_record"), ns)
    ns["_item_thread_record"]([{"conversation_id": CONV, "message_id": "m-original"}], handoff)
    assert item_threads.find(CONV)["thread"] == handoff

    slack_notifier.notify_priority_emails([_alert("m-forward")])
    (thread,) = [t for t in store.threads("main")]
    msgs = [m for m in store.thread("main", handoff)]
    assert thread["thread_key"] == handoff and len(msgs) == 2
    assert "New priority email" in msgs[1]["text"]
    # the same message again: nothing new, nothing posted
    slack_notifier.notify_priority_emails([_alert("m-forward")])
    assert len(store.thread("main", handoff)) == 2
    # another conversation: its own thread, recorded as its own
    slack_notifier.notify_priority_emails([_alert("m-other", conv="AAQkConvOther00002=", subject="Visit")])
    assert len(store.threads("main")) == 2
    assert item_threads.find("AAQkConvOther00002=")["thread"] != handoff


def test_on_slack_an_alert_posts_as_before(env, monkeypatch):  # noqa: F811
    """Slack has no thread to join by key: one post per alert, as before."""
    sent = []
    monkeypatch.setattr(chat_notify, "_send_slack", lambda key, parts, blocks: sent.append(parts) or True)
    slack_notifier.notify_priority_emails([_alert("m-1"), _alert("m-2")])
    assert len(sent) == 2 and item_threads.find(CONV) is None


def _tick_ns(tmp_path, queue, joined):
    posts, turns = [], []
    seen_file = tmp_path / "seen.json"
    ns = {"re": __import__("re"), "json": json, "datetime": datetime, "timezone": timezone,
          "_untrusted_field": lambda t, n=140: str(t or "")[:n],
          "REMOTE_ESCALATIONS": tmp_path / "q.json", "REMOTE_ESCALATIONS_SEEN": seen_file,
          "REMOTE_ESCALATION_BATCH_MAX": 2, "_CONV_ID_RE": __import__("re").compile(r"^[A-Za-z0-9=]{6,}$"),
          "CHANNEL_ID": "main", "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
          "_resident_text": lambda p: json.dumps(queue),
          "_remote_escalations_seen": lambda: json.loads(seen_file.read_text()) if seen_file.exists() else {},
          "FRONTEND": types.SimpleNamespace(post=lambda **k: posts.append(k) or types.SimpleNamespace(
              id=f"m{len(posts)}", thread_key=k.get("thread_ts") or f"t-new{len(posts)}")),
          "_atomic": types.SimpleNamespace(write_json=lambda p, d, **k: p.write_text(json.dumps(d))),
          "handle_message_async": None, "_keep_split": lambda items: (list(items), []),
          "_item_thread": lambda e: joined.get(e.get("conversation_id")),
          "_item_thread_record": lambda batch, thread: None, "_HANDOFF_TURNS": {},
          "threading": types.SimpleNamespace(Thread=lambda target, args, daemon, kwargs=None:
                                             types.SimpleNamespace(start=lambda: turns.append(args)))}
    for name in ("_remote_escalation_item", "_remote_escalation_prompt", "_remote_escalation_batch_prompt",
                 "_remote_escalation_tick"):
        exec(_fn(name), ns)
    return ns, posts, turns


def test_a_hand_off_joins_the_thread_its_conversation_has(tmp_path):
    """The alert came first: the hand-off posts into the alert's thread,
    alone, and its turn reads that thread as history; the other item gets
    a new thread on the next tick."""
    queue = [{"conversation_id": CONV, "subject": "Workshop homework", "from_name": "Pat",
              "ledger_id": ROW, "message_id": "m-original"},
             {"conversation_id": "AAQkConvOther00002=", "subject": "Visit", "from_name": "Lee"}]
    ns, posts, turns = _tick_ns(tmp_path, queue, {CONV: {"channel": "main", "thread": "t-alert"}})
    ns["_remote_escalation_tick"]()
    assert posts[0]["thread_ts"] == "t-alert" and "handed this one item" in turns[0][3]
    channel, thread, msg, _prompt, _who, is_reply = turns[0]
    assert (channel, thread, is_reply) == ("main", "t-alert", True)
    assert ns["_HANDOFF_TURNS"][msg] == [ROW]
    ns["_remote_escalation_tick"]()
    assert "thread_ts" not in posts[1] and turns[1][1] == "t-new2" and turns[1][5] is False


# ── the Replies list: work done elsewhere is not "no draft yet" ───────

def test_the_hand_off_names_its_row_so_its_draft_links_to_it():
    """A row id ends in its message id's tail, "=" included: the hand-off
    names the row, and the draft verb's --ledger-id with it."""
    ns = {"re": __import__("re"), "_untrusted_field": lambda t, n=140: str(t or "")[:n]}
    exec(_fn("_remote_escalation_item"), ns)
    lines = "\n".join(ns["_remote_escalation_item"]({"conversation_id": CONV, "ledger_id": ROW,
                                                      "subject": "Workshop homework"}))
    assert f"ledger.py get --id {ROW}" in lines and f"--ledger-id {ROW}" in lines
    assert f"ledger.py update --id {ROW}" in lines
    bad = "\n".join(ns["_remote_escalation_item"]({"conversation_id": CONV, "ledger_id": "x; rm -rf /"}))
    assert "--ledger-id" not in bad


def test_a_one_item_hand_off_records_its_loose_draft_on_the_row():
    calls = []
    ns = {"_HANDOFF_TURNS": {"m1": [ROW], "m2": [ROW, "row-2"], "m3": [ROW]},
          "_drafts_naming_rows": lambda: {"d-named"},
          "_ledger_cli": lambda *a: calls.append(a) or {"ok": True},
          "log": types.SimpleNamespace(warning=lambda *a: None)}
    exec(_fn("_handoff_turn_ended"), ns)
    ns["_handoff_turn_ended"]("m1", ["d-named", "d-loose"])
    assert calls == [("note-reply", "--id", ROW, "--outcome", "drafted", "--draft-id", "d-loose")]
    ns["_handoff_turn_ended"]("m2", ["d-loose"])          # two items: whose draft is it? none
    ns["_handoff_turn_ended"]("m3", ["d-named"])          # the draft names its row already
    ns["_handoff_turn_ended"]("m9", ["d-loose"])          # not a hand-off turn
    assert len(calls) == 1 and ns["_HANDOFF_TURNS"] == {}


def test_a_row_a_run_left_is_on_no_reply_list():
    """The morning run recorded that the message needs no reply now (the
    work is in a review request): the Replies blocks leave it out, as Home
    does (tests/test_reply_card.py has the tick)."""
    ns = {}
    exec(_fn("_reply_left"), ns)
    row = {"id": ROW, "queued": {"outcome": "left", "note": "The table is drafted for review."}}
    assert ns["_reply_left"](row) and not ns["_reply_left"]({"id": ROW, "queued": {}})
    assert not ns["_reply_left"]({"id": ROW, "queued": {"outcome": "drafted", "draft_id": "d-1"}})


# ── the Scheduling channel holds its threads ──────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    from preprocessor import ledger as pledger
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for m in ledger.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    rows = [("response-kai1=", "response_needed"), ("sched-kai1=", "scheduling"), ("response-lee1=", "response_needed")]
    brief = {"needs_reply": True, "user_replied_after_latest": False, "scheduling": "proposes-times",
             "messages": [{"id": "m-k", "own": False, "received": "2026-03-14T16:00:00Z", "gist": "Times?"}]}
    for rid, typ in rows:
        conv = "conv-lee" if "lee" in rid else "conv-kai"
        conn.execute("INSERT INTO ledger (id, type, status, description, source_message_id, conversation_id, "
                     "date_identified, metadata, created_at, updated_at) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
                     (rid, typ, f"Reply on {conv}", f"m-{rid}", conv, "2026-03-14T16:00:00Z",
                      json.dumps({"thread_brief": brief}), "2026-03-14T16:00:00Z", "2026-03-14T16:00:00Z"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(ledger, "DB_PATH", path)
    monkeypatch.setattr(pledger, "DB_PATH", path)
    return path


def test_the_scheduling_channel_holds_its_thread_off_the_reply_list(db):
    """The bot marks the open scheduling row of each conversation that has a
    Scheduling thread; the reply list then leaves that conversation out,
    until its thread there is done."""
    state = {"t40": "needs_you"}
    ns = {"datetime": datetime, "timezone": timezone, "SCHED_CHANNEL_ID": "sched",
          "_sched_seen": lambda: {"conv-kai": {"thread": "t40", "messages": ["m-k"]},
                                  "conv-none": {"thread": "", "messages": []}},
          "FRONTEND": types.SimpleNamespace(store=types.SimpleNamespace(
              thread_state=lambda ch, key: {"state": state[key]} if key in state else None))}
    exec(_fn("_sched_thread_ended"), ns)
    exec(_fn("_sched_mark_held"), ns)

    def owed():
        rows = ledger.get_open_entries()
        return [e["id"] for e in ledger.reply_backlog(rows, datetime(2026, 3, 18).date(), draft_convs=None)]
    assert owed() == ["response-kai1=", "response-lee1="]
    assert ns["_sched_mark_held"]() == 1
    assert ledger.get_entry("sched-kai1=")[ledger.SCHED_THREAD]["thread"] == "t40"
    assert owed() == ["response-lee1="]
    assert ns["_sched_mark_held"]() == 0                   # nothing changed, nothing written
    state["t40"] = "done"
    assert ns["_sched_mark_held"]() == 1 and owed() == ["response-kai1=", "response-lee1="]


# ── the weekly audit and a room's slice: each item once ───────────────

def test_the_weekly_audit_lists_a_handed_off_item_once(tmp_path, monkeypatch):
    rows = [{"id": ROW, "description": "Workshop homework", "status": "open", "deadline": "2026-03-20",
             "type": "deadline", "conversation_id": CONV, "source_message_id": "m-original"},
            {"id": "priority-workshop-forward=", "description": "Priority email: FW: Workshop homework",
             "status": "open", "deadline": "2026-03-20", "type": "response_needed",
             "conversation_id": CONV, "source_message_id": "m-forward"}]
    cands = ra.candidates(rows, {}, today=NOW.date())
    assert len(cands) == 1                                   # two rows on one thread: one item
    (ref,) = cands
    plan = {"critical": [{"ref": ref, "why": "The table is due Friday."}], "important": [],
            "handoffs": [{"ref": ref, "why": "a document", "task": "draft the table"}], "kept": [],
            "patterns": []}
    text = ra.render(cands, plan, closed=[], reimb=[], hygiene=[], closure={}, counts={}, now=NOW,
                     handed=plan["handoffs"])
    assert text.count("Workshop homework") == 1
    assert "The table is due Friday. (handed to Claude)" in text and "Handed to Claude (one" not in text


def test_the_web_chat_posts_no_slice_and_a_slice_elsewhere_names_a_thread_once():
    assert br.enabled({}, frontend="web") is False
    entries = [{"ref": "F1", "section": "flex", "line": "*Workshop homework*: fill in the table",
                "ledger_id": ROW, "conversation_id": CONV, "title": "Workshop homework"},
               {"ref": "D1", "section": "draft", "line": "*Workshop homework*: thanks, attached",
                "ledger_id": ROW, "conversation_id": CONV, "title": "Workshop homework"}]
    assert [e["ref"] for e in br.one_per_thread(entries)] == ["D1"]
