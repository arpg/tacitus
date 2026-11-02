"""The closure label store (closure_labels.py, docs/ea/closure-signals.md):
each source read from a scratch tree, the dedupe that makes collect safe
to run again, the same key from the live hook and from collect, and the
rule that no text ever reaches the store. Every name, address, id and
message here is made up; nothing reaches a network."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import closure  # noqa: E402
import closure_labels as cl  # noqa: E402
import ledger  # noqa: E402
from chat import thread_state as ts  # noqa: E402
from chat.web_frontend import WebStore  # noqa: E402

T0 = 1_793_000_000.0
H = 3600.0
DAY = 86400.0
# Words that stand for mail and message text: none may reach the store.
SECRET = "Avery Quill"
SUBJECT = "the travel budget for the spring visit"

ZHEAD = ":locked: **Confirm send**\n**To:** `avery@example.test`"
ZRULE = "\n\n----\n"
ZBODY = f"**Body:**\n> Dear {SECRET}, about {SUBJECT}."


def _iso(t: float) -> str:
    return cl.iso(t)


def _ledger_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(ledger.SCHEMA)
    for migration in ledger.MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass
    rows = [
        # id, type, status, conv, closed_at, last_action, metadata
        ("response-Aa1=", "response_needed", "completed", "AAQkconv1=", _iso(T0 + 2 * H),
         f"Auto-closed: email archived from inbox by user ({SECRET})",
         {"closed_by": {"signal": "user_archived", "actor": "ledger_sync", "tier": "decisive"}}),
        ("task-Ab1=", "task", "completed", "AAQkconv2=", _iso(T0 + 3 * H),
         f"done, per user: {SUBJECT}",
         {"closed_by": {"signal": "user_stated", "actor": "cli:owner", "tier": "decisive"}}),
        ("deadline-Ac1=", "deadline", "open", "AAQkconv3=", None, f"Asked {SECRET}.",
         {"close_proposal": {"signal": "llm:reconcile", "why": SUBJECT}}),
    ]
    for rid, typ, status, conv, closed_at, last, meta in rows:
        conn.execute("INSERT INTO ledger (id, type, status, description, conversation_id, "
                     "source_message_id, date_identified, deadline, closed_at, last_action, "
                     "metadata, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, typ, status, f"Reply to {SECRET} about {SUBJECT}", conv, "AAMkmsg=",
                      _iso(T0 - 2 * DAY), "2026-11-05", closed_at, last, json.dumps(meta),
                      _iso(T0 - 2 * DAY), _iso(T0)))
    closure.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    closure.record_event(conn, "response-Aa1=", "reopen", signal="user_archived", actor="cli:chat",
                         note=SUBJECT, ts=_iso(T0 + 4 * H))
    closure.record_event(conn, "deadline-Ac1=", "reject", signal="llm:reconcile", actor="user",
                         note=SUBJECT, ts=_iso(T0 + 5 * H))
    closure.record_event(conn, "task-Ab1=", "confirm", signal="calendar_event_match",
                         actor="user", note=SUBJECT, ts=_iso(T0 + 6 * H))
    conn.commit()
    conn.close()
    return path


def _web_store(path: Path) -> SimpleNamespace:
    s = WebStore(path)
    s.upsert_channel("main", "ea", "main")
    s.upsert_channel("dm", "dm", "dm")
    s.upsert_channel("stream:4", "ea", "main")
    s.upsert_channel("stream:6", "ea-dm", "dm")
    # Thread A: the owner asks for a draft; the bot points at the Send card
    # in the DM, which the owner pressed.
    a = s.add_message("main", mid=None, thread_key=None, author="owner", kind="message",
                      text=f"Draft a reply to {SECRET} about {SUBJECT}.", posted_at=T0)
    card = s.add_message("dm", mid=None, thread_key=None, author="bot", kind="card",
                         text=":lock: *Confirm send*", posted_at=T0 + 60,
                         card={"kind": "send", "title": ":lock: *Confirm send*",
                               "fields": [{"label": "To", "value": "avery@example.test"}],
                               "body": f"Dear {SECRET}", "actions": [{"id": "approve_send"}]})
    s.add_message("main", mid=None, thread_key=a["thread_key"], author="bot", kind="message",
                  text=(f"Draft ready for {SECRET}. :lock: The Send button is in your DM: "
                        f"[open it](https://chat.example.test/chat/#/dm/{card['thread_key']}/"
                        f"{card['id']})"), posted_at=T0 + 61)
    s.change_message("dm", card["id"], "status", kind="status", text=":white_check_mark: Sent")
    # Thread B: a briefing that names a row, and the owner's thanks.
    b = s.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                      text=f"**Morning briefing**, Tue\n- `task-Ab1=` {SUBJECT}", posted_at=T0)
    s.add_message("main", mid=None, thread_key=b["thread_key"], author="owner", kind="message",
                  text=f"thanks, tell {SECRET} yes", posted_at=T0 + 300)
    # The model judged thread A once.
    seq = s.newest_seq("main", a["thread_key"])
    s.set_thread_state("main", a["thread_key"], "needs_you", "model",
                       "The draft waits for you.", judged_seq=seq)
    # Zulip's past: the owner's ask, the pointer and the card it names.
    zkey = "stream:4:topic:Budget%20reply%20to%20Avery"
    s.add_message("stream:4", mid="8000", thread_key=zkey, author="owner", kind="message",
                  text=f"Reply to {SECRET} and say yes.", posted_at=T0, history=True)
    s.add_message("stream:4", mid="8001", thread_key=zkey, author="bot", kind="message",
                  text=(":locked: The Send button for this draft is in your DM: [open it]"
                        "(https://zulip.example.test/#narrow/channel/6-ea-dm/topic/x/near/9001)"),
                  posted_at=T0 + 30, history=True)
    s.add_message("stream:6", mid="9001", thread_key="stream:6:topic:lock ABCD", author="bot",
                  kind="message", posted_at=T0 + 20, history=True,
                  text=ZHEAD + ZRULE + ":mail_received: Sent from Outlook." + ZRULE + ZBODY)
    s.close()
    return SimpleNamespace(a=a["thread_key"], b=b["thread_key"], zkey=zkey, card=card["id"])


@pytest.fixture
def tree(tmp_path, monkeypatch):
    ea, priv = tmp_path / "ea", tmp_path / "priv"
    (ea / "logs").mkdir(parents=True)
    (ea / "state").mkdir()
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ledger, "_is_owner", lambda: False)
    db = _ledger_db(ea / "state" / "obligations-ledger.db")
    store = priv / "webchat.sqlite"
    keys = _web_store(store)
    (priv / cl.TAPS_FILE).write_text("\n".join(json.dumps(x) for x in [
        {"at": T0 + 2 * H, "channel": "main", "thread_key": keys.a, "state": "done",
         "previous_state": "needs_you", "previous_by": "model", "message_count": 2},
        {"at": T0 + H, "channel": "main", "thread_key": keys.b, "state": "needs_you",
         "previous_state": "none", "previous_by": "rule", "message_count": 2},
    ]) + "\n")
    (ea / "logs" / "missed-closures.log").write_text(json.dumps({
        "ts": _iso(T0 + 3 * H), "ledger_id": "task-Ab1=",
        "user_message": f"{SECRET} has the form now", "source": "chat",
        "entry_summary": {"type": "task", "description": f"Send {SECRET} the form",
                          "status": "open", "date_identified": _iso(T0 - 2 * DAY),
                          "deadline": "2026-11-05", "conversation_id": "AAQkconv2=",
                          "has_source_message_id": True},
        "scrubber_signals_present": [
            {"signal": "user_replied_outlook", "would_fire": False, "evaluated": True,
             "reason": f"no reply to {SECRET}"},
            {"signal": "calendar_event_match", "would_fire": None, "evaluated": False,
             "reason": "not evaluated: calendar unavailable"}],
        "candidate_evidence": [f"sent 2026-10-01: subject='{SUBJECT}' to=avery@example.test"],
        "evidence_complete": False,
        "suggested_signal": "No detectable signal in data — this may be a genuine pass",
    }) + "\nnot json\n")
    (ea / "logs" / "item-feedback.jsonl").write_text(json.dumps({
        "ts": _iso(T0 + 7 * H), "ledger_id": "deadline-Ac1=", "kind": "still_open",
        "note": f"{SECRET} has not answered", "until": "", "source": "dashboard",
        "description": SUBJECT}) + "\n")
    return SimpleNamespace(ea=ea, priv=priv, db=db, store=store, keys=keys,
                           labels=priv / "evals" / cl.LABELS_FILE)


def _by(recs, source):
    return [r for r in recs if r["source"] == source]


# ── each source ───────────────────────────────────────────────────────

def test_collect_reads_every_source(tree):
    out = cl.collect()
    assert out["errors"] == {}
    assert out["added"] == {"owner-tap": 2, "card": 2, "model": 1, "ledger": 6, "owner-said": 1}
    recs = cl.load(tree.labels)
    assert len(recs) == 12 and Path(out["path"]) == tree.labels


def test_an_owner_tap_carries_the_thread_as_it_stood(tree):
    cl.collect()
    taps = {r["thread_ref"]: r for r in _by(cl.load(tree.labels), "owner-tap")}
    a = taps[cl.thread_ref("main", tree.keys.a)]
    assert (a["label"], a["kind"], a["subject_type"], a["channel_kind"], a["origin"]) == \
        ("done", "tap", "drafting_email", "main", "owner")
    f = a["features"]
    assert f["interaction_type"] == "owner_ask" and f["last_author"] == "bot"
    assert f["has_card_pointer"] and f["pointer_card_outcome"] == "acted"
    assert (f["prior_state"], f["prior_by"], f["reconstructed"]) == ("needs_you", "model", True)
    assert f["message_count"] == 2 and f["hours_since_last_owner_message"] == 2.0
    b = taps[cl.thread_ref("main", tree.keys.b)]
    assert b["label"] == "needs_you" and b["features"]["interaction_type"] == "briefing_reply"
    # the row the briefing names, linked by its id, as the engine links it
    assert b["ledger_ref"] == cl.ledger_ref("task-Ab1=")
    assert b["features"]["linked_ledger_status"] == "completed"
    # the owner wrote last and no answer came: the rules call it a stuck turn
    assert (b["features"]["rule_id"], b["features"]["rule_state"], b["features"]["stuck_turn"]) \
        == ("3-stuck", "needs_you", True)


def test_a_card_outcome_joins_the_conversation_that_asked_for_it(tree):
    cl.collect()
    cards = {r["kind"]: r for r in _by(cl.load(tree.labels), "card")}
    assert set(cards) == {"acted", "outside"}
    web = cards["acted"]
    assert web["label"] == "done" and web["features"]["card_kind"] == "send"
    # the pointer in thread A names it: the card belongs to that conversation
    assert web["thread_ref"] == cl.thread_ref("main", tree.keys.a)
    assert web["features"]["interaction_type"] == "owner_ask" and web["origin"] == "card"
    zulip = cards["outside"]
    assert zulip["features"]["card_outcome"] == "outside"
    assert zulip["thread_ref"] == cl.thread_ref("stream:4", tree.keys.zkey)
    assert zulip["subject_type"] == "drafting_email"
    assert zulip["features"]["interaction_type"] == "owner_ask"


def test_the_ledger_rows_events_and_dispositions(tree):
    cl.collect()
    led = _by(cl.load(tree.labels), "ledger")
    got = sorted((r["kind"], r["label"], r["features"].get("closed_how"),
                  r["features"].get("signal")) for r in led)
    assert got == [("completed", "done", "owner", "user_stated"),
                   ("completed", "done", "rule", "user_archived"),
                   ("confirmed", "done", "rule", "calendar_event_match"),
                   ("rejected", "open", "model", "llm:reconcile"),
                   ("reopened", "open", "rule", "user_archived"),
                   ("still_open", "open", None, None)]
    done = next(r for r in led if r["features"].get("signal") == "user_archived"
                and r["kind"] == "completed")
    f = done["features"]
    assert (f["linked_ledger_type"], f["provenance"], f["detector_row"], f["has_conversation"]) \
        == ("response_needed", "inbox_archive", True, True)
    assert done["origin"] == "mail" and done["ledger_ref"] == cl.ledger_ref("response-Aa1=")
    fb = next(r for r in led if r["kind"] == "still_open")
    assert fb["features"]["said_via"] == "dashboard"


def test_a_missed_closure_keeps_its_autopsy_as_tokens(tree):
    cl.collect()
    (said,) = _by(cl.load(tree.labels), "owner-said")
    f = said["features"]
    assert (said["label"], said["kind"], said["ledger_ref"]) == \
        ("done", "missed", cl.ledger_ref("task-Ab1="))
    assert f["signals_present"] == ["calendar_event_match", "user_replied_outlook"]
    assert f["signals_fired"] == [] and f["signals_unevaluated"] == ["calendar_event_match"]
    assert (f["suggestion"], f["candidate_evidence_count"], f["said_via"]) == \
        ("no_signal", 1, "chat")
    assert f["evidence_complete"] is False and f["linked_ledger_type"] == "task"


def test_a_model_judgment_from_the_event_log(tree):
    cl.collect()
    (m,) = _by(cl.load(tree.labels), "model")
    assert (m["label"], m["kind"], m["thread_ref"]) == \
        ("needs_you", "judgment", cl.thread_ref("main", tree.keys.a))
    assert m["features"]["applied_state"] == "needs_you" and "confidence" not in m["features"]


# ── dedupe and the live hook ──────────────────────────────────────────

def test_the_nightly_report_appends_one_history_line_a_run(tree, capsys):
    import closure_eval as ce
    hist = tree.labels.parent / ce.HISTORY_FILE
    for n in (1, 2):
        assert cl.main(["collect", "--report"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["summary"].startswith("Closure labels: ") and "Model local F1" in out["summary"]
        assert len(hist.read_text().splitlines()) == n
    assert stat.S_IMODE(hist.stat().st_mode) == 0o600
    assert SECRET not in hist.read_text() and "Avery" not in hist.read_text()
    (entry,) = ce.load_history(hist)[-1:]
    assert entry["owner_taps"] == 2 and set(entry["by_via"]) == set(ce.VIAS)


def test_collect_again_adds_nothing(tree):
    first = cl.collect()
    size = tree.labels.stat().st_size
    again = cl.collect()
    assert sum(again["added"].values()) == 0 and again["total"] == first["total"]
    assert tree.labels.stat().st_size == size
    # a fresh process (no cached keys) dedupes against the file too
    cl._KEYS.clear()
    assert sum(cl.collect()["added"].values()) == 0


def test_the_hook_and_collect_write_one_record_per_tap(tree):
    store = WebStore(tree.store)
    try:
        ctx = ts.ThreadStates(lambda: store).contexts([("main", tree.keys.a)])[0]
        hooked = cl.owner_tap_record(ctx, row={"state": "done", "at": T0 + 2 * H},
                                     before={"state": "needs_you", "by": "model"},
                                     now=T0 + 2 * H, store=store, writeback="close")
    finally:
        store.close()
    assert cl.append([hooked], tree.labels) == 1
    cl.collect()
    taps = _by(cl.load(tree.labels), "owner-tap")
    assert len(taps) == 2
    mine = next(r for r in taps if r["key"] == hooked["key"])
    assert mine["features"]["writeback"] == "close" and "reconstructed" not in mine["features"]


def test_an_answer_the_observer_wrote_is_not_taken_again_from_the_log(tree):
    reader = cl.StoreReader(tree.store)
    try:
        (ev,) = [e for e in reader.state_events(None) if e.get("by") == "model"]
        tref = cl.thread_ref(ev["channel"], ev["thread_key"])
        seen = [{"source": "model", "thread_ref": tref, "at": cl.iso(ev["at"] + 1)}]
        assert cl.model_event_records(reader, None, lambda i, c: [], seen) == []
        assert len(cl.model_event_records(reader, None, lambda i, c: [], [])) == 1
    finally:
        reader.close()


# ── no text, ever ─────────────────────────────────────────────────────

def test_no_text_reaches_the_store(tree):
    cl.collect()
    raw = tree.labels.read_text()
    for words in (SECRET, "Avery", SUBJECT, "budget", "travel", "avery@example.test",
                  "Confirm send", "Budget%20reply", "Dear", "form"):
        assert words not in raw, words
    for line in raw.splitlines():
        rec = json.loads(line)
        cl.validate(rec)

        def walk(v):
            if isinstance(v, str):
                assert cl.TOKEN_RE.fullmatch(v), v
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(rec)


def _ok_record(**over):
    rec = cl.record("owner-tap", "tap", "done", key="k1", at=T0,
                    features={"message_count": 3, "card_kind": "send"},
                    channel_kind="main", origin="owner", thread="t1")
    rec.update(over)
    return rec


@pytest.mark.parametrize("bad", [
    {"origin": "the owner"},                                   # a space: text
    {"kind": "x" * 41},                                        # too long for a token
    {"features": {"card_kind": "Confirm send"}},               # a title
    {"features": {"subject": "budget"}},                       # no such feature
    {"features": {"signals_present": [f"{SECRET}"]}},          # text in a list
    {"features": {"message_count": {"n": 3}}},                 # an object
    {"features": {"confidence": float("nan")}},
    {"label": "closed"},
    {"source": "slack"},
    {"text": "hello"},                                         # a field of its own
])
def test_a_text_looking_field_fails(bad):
    with pytest.raises(ValueError):
        cl.validate(_ok_record(**bad))


def test_append_refuses_the_whole_batch_on_one_bad_record(tmp_path):
    path = tmp_path / "evals" / cl.LABELS_FILE
    with pytest.raises(ValueError):
        cl.append([_ok_record(), _ok_record(key="k2", origin=f"{SECRET}")], path)
    assert not path.exists()
    assert cl.append([_ok_record()], path) == 1


def test_the_store_is_owner_only(tree):
    cl.collect()
    assert stat.S_IMODE(tree.labels.stat().st_mode) == 0o600
    assert stat.S_IMODE(tree.labels.parent.stat().st_mode) & 0o077 == 0
    link = tree.labels.parent / "elsewhere.jsonl"
    os.symlink(tree.labels, link)
    with pytest.raises(OSError):
        cl.append([_ok_record(key="k9")], link)


# ── the readings ──────────────────────────────────────────────────────

@pytest.mark.parametrize("line,want", [
    (":white_check_mark: Sent", "acted"),
    (":check: Calendar event created.", "acted"),
    (":check: Closed 2 items", "acted"),
    (":mail_received: Marked as sent by user", "outside"),
    (":incoming_envelope: Sent from Outlook", "outside"),
    (":information_source: This draft was already handled", "outside"),
    (":pencil: Revised. The new Send card for this draft is below.", "superseded"),
    (":push_pin: Kept open", "kept_open"),
    (":prohibited: Send cancelled. Draft kept in Outlook.", "declined"),
    (":no_entry_sign: Not published", "declined"),
    (":x: Send failed", "failed"),
    (":cross_mark: Could not authorize this card: expired", "failed"),
    (":hourglass_flowing_sand: Sending...", "busy"),
    (":time_ticking: Sending...", "busy"),
])
def test_a_status_line_is_one_outcome_word(line, want):
    assert cl.outcome_class(line) == want


def test_subject_and_interaction_types():
    def owner(text, ch="main"):
        return [{"author": "owner", "kind": "message", "text": text, "channel": ch}]
    assert cl.subject_type(owner("Can you find a time to meet with the lab next week?")) \
        == "scheduling"
    assert cl.subject_type(owner("Reply to the committee and decline politely.")) \
        == "drafting_email"
    assert cl.subject_type(owner("Please revise the slides for the seminar talk")) \
        == "drafting_doc"
    assert cl.subject_type(owner("From now on archive the facilities newsletters")) \
        == "instruction"
    assert cl.subject_type(owner("What did the dean's office say about the visit?")) == "lookup"
    assert cl.subject_type([{"author": "bot", "kind": "message", "text": "x"}]) == "none"
    assert cl.interaction_type(owner("x"), "sched") == "scheduling"
    assert cl.interaction_type(owner("x"), "room") == "room"
    assert cl.interaction_type(owner("x"), "dm") == "owner_ask"
    brief = [{"author": "bot", "kind": "message", "text": "**Morning briefing**, Mon"}]
    assert cl.interaction_type(brief, "main") == "automated"
    assert cl.interaction_type(brief + owner("thanks"), "main") == "briefing_reply"
    assert cl.interaction_type([{"author": "bot", "kind": "message", "text": "hi"}], "main",
                               origin="ea-assistant") == "automated"
    assert cl.interaction_type([{"author": "bot", "kind": "card", "text": "", "card": {
        "title": "Confirm send", "actions": [{"id": "a"}]}}], "dm") == "mail_card"


def test_who_settled_a_row():
    assert cl.closed_how("user_archived") == "rule"
    assert cl.closed_how("linked_send") == "rule"
    assert cl.closed_how("user_rule") == "rule"
    assert cl.closed_how("llm:reconcile") == "model"
    assert cl.closed_how("", "briefing_llm") == "model"
    assert cl.closed_how("user_confirmed") == "confirmed"
    assert cl.closed_how("user_stated") == "owner"
    assert cl.closed_how("user_sent_myself") == "owner"
    assert cl.closed_how("", "manual", "Closed from dashboard") == "owner"
    assert cl.closed_how("", "manual", "Duplicate of another row") == "rule"
    assert cl.closed_how("", "manual", "a note") == "other"


def test_the_rule_that_gave_a_state():
    assert cl.rule_id("needs_you", 'The card "Confirm send" still has its buttons.') \
        == "2-live-card"
    assert cl.rule_id("needs_you", ts.NOT_ANSWERED) == "3-stuck"
    assert cl.rule_id("done", "The card was answered: Sent") == "4-card-outcome"
    assert cl.rule_id("waiting", "The linked ledger item waits on someone else.") \
        == "6-ledger-waiting"
    assert cl.rule_id("none", "An automated post: it needs nothing from you.") == "7-automated"
    assert cl.rule_id("none", "A post from Tacitus: " + ts.QUIET_TAIL) == "7-automated"
    assert cl.rule_id("needs_you", ts.CARD_WAITS) == "2b-linked-card"
    assert cl.rule_id("done", "Its card was handled: Sent") == "2b-linked-card"
    assert cl.rule_id(None, "") is None
