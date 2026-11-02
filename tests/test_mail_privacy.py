"""mail_privacy: the record validator, the keep-from-Anthropic label, one pass over
injected Graph data with a fake model, the mediator's release note, the
calendar shim and the reduced briefing. Nothing here reaches the network
or the real private dir."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402

NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
GOOD = {"sensitivity": "normal", "confidential_kind": "none", "needs_reply": True,
        "ask": "Pick a time.", "deadline": "2026-10-30", "deadline_text": "by Oct 30",
        "scheduling": "request", "offered_times": ["2026-10-20T14:00:00"],
        "people": [{"name": "Pat", "role": "student"}], "tone": [], "state": "scheduling",
        "domain": "teaching", "summary": "Pat asks for a meeting."}


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {"owner@example.edu"})
    monkeypatch.setattr(mp, "_profile_line", lambda: "The user is a professor.")
    mp._INDEX_CACHE.update(mtime=None, index=None)
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "shadow",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": True}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def _msg(i, frm="pat@example.edu", cc=(), subject="Meeting", cid=None, received=None):
    return {"id": f"AAMk{'x' * 40}{i}", "conversationId": cid or f"conv{i}",
            "receivedDateTime": received or f"2026-10-09T1{i % 10}:00:00Z",
            "subject": subject, "hasAttachments": False, "isDraft": False,
            "from": {"emailAddress": {"name": "P", "address": frm}},
            "toRecipients": [{"emailAddress": {"address": "owner@example.edu"}}],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}


class FakeGraph:
    MAIL_FIELDS = "id"

    def __init__(self, msgs, body="Could we meet next week? Tue at 2 works."):
        self.msgs, self.body, self.fetched = msgs, body, []

    def graph_get_all(self, endpoint, params=None):
        return list(self.msgs)

    def get_message_full(self, mid):
        self.fetched.append(mid)
        return {"body": {"contentType": "text", "content": self.body}}


def _chat(result=GOOD, calls=None):
    def fn(messages, **kw):
        if calls is not None:
            calls.append(messages)
        return dict(result), {"latency_ms": 1200}
    return fn


def test_validate_turns_a_dated_reminder_into_a_todo():
    """'Your recommendation is due in two days' filed as fyi carries a
    deadline the lanes never tracked; the repair makes it a todo. A dated
    fyi with no due words (a seminar announcement) stays fyi."""
    rec = mp.validate({"state": "fyi", "deadline": "2026-10-07", "deadline_text": "due in two days",
                       "summary": "A nudge."})
    assert rec["state"] == "todo" and rec["deadline"] == "2026-10-07"
    rec = mp.validate({"state": "fyi", "deadline": "2026-10-07", "deadline_text": "", "ask": "",
                       "summary": "Seminar on October 7."})
    assert rec["state"] == "fyi"
    rec = mp.validate({"state": "fyi", "deadline": "", "deadline_text": "overdue"})
    assert rec["state"] == "fyi"


def _kept_rec(i, *, conv, received, state="todo", needs_reply=True, deadline="", ask="Submit it.",
              own=False, kept="never-send domain journal.example.org", frm="notices@journal.example.org",
              subject="Communication regarding Manuscript ID JRNL-26-7385"):
    rec = {"key": f"k{i}", "id": f"m{i}", "conversation_id": conv, "received": received, "from": frm,
           "subject": subject, "status": "ok", "keep_from_anthropic": kept,
           "result": {"state": state, "needs_reply": needs_reply, "ask": ask, "deadline": deadline,
                      "deadline_text": "a few days overdue" if deadline else "", "scheduling": "none",
                      "summary": "Pat Lee asks for the recommendation."}}
    if own:
        rec["own"] = True
        rec["from"] = "owner@example.edu"
    return rec


def _index(recs):
    by_conv = {}
    for r in recs:
        by_conv.setdefault(r["conversation_id"], []).append(r)
    return {"by_key": {r["key"]: r for r in recs}, "by_id": {r["id"]: r for r in recs}, "by_conv": by_conv}


def test_kept_row_plan_creates_folds_nudges_and_closes_from_the_records():
    now = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    user = {"owner@example.edu"}
    # A kept todo with a deadline in a thread no row covers: a deadline row.
    nudge = _kept_rec(1, conv="c-oct13", received="2026-10-13T12:04:31Z", deadline="2026-10-15")
    plan = mp.kept_row_plan(_index([nudge]), [], user=user, now=now)
    assert plan["update"] == [] and plan["close"] == []
    [row] = plan["create"]
    assert row["id"] == "kept-c-oct13" and row["type"] == "deadline" and row["deadline"] == "2026-10-15"
    assert row["description"].startswith("Communication regarding Manuscript")
    assert "Pat Lee asks" in row["notes"] and "Ask: Submit it." in row["notes"]
    assert row["metadata"]["kept_from_claude"].startswith("never-send") and row["conversation_id"] == "c-oct13"
    # Typed by state: an undated todo is a task, a plain ask a response row,
    # a meeting a scheduling row; FYI and the user's own mail make nothing.
    kinds = {}
    for state, needs, dl, sched in (("todo", False, "", "none"), ("needs-reply", True, "", "none"),
                                    ("scheduling", True, "", "propose"), ("fyi", False, "", "none")):
        r = _kept_rec(9, conv=f"c-{state}", received="2026-10-13T13:00:00Z", state=state, needs_reply=needs,
                      deadline=dl)
        r["result"]["scheduling"] = sched
        p = mp.kept_row_plan(_index([r]), [], user=user, now=now)
        kinds[state] = p["create"][0]["type"] if p["create"] else None
    assert kinds == {"todo": "task", "needs-reply": "response_needed", "scheduling": "scheduling", "fyi": None}
    own = _kept_rec(3, conv="c-own", received="2026-10-13T13:00:00Z", own=True)
    assert mp.kept_row_plan(_index([own]), [], user=user, now=now)["create"] == []
    # an applicant's inquiry, kept by the admissions rule, is the admissions filter's
    applicant = _kept_rec(8, conv="c-app", received="2026-10-13T13:00:00Z", kept="admissions",
                          frm="student@example.edu", subject="Prospective PhD student for Fall 2027")
    assert mp.kept_row_plan(_index([applicant]), [], user=user, now=now)["create"] == []
    # A thread a row already tracks (the user's own hand-made row) is left alone.
    rows = [{"id": "ijrr-ae-rec", "status": "open", "conversation_id": "c-oct13", "source_message_id": "",
             "from_email": "", "description": "Journal recommendation", "deadline": "2026-10-07", "metadata": {}}]
    assert mp.kept_row_plan(_index([nudge]), rows, user=user, now=now) == {"create": [], "update": [], "close": []}
    # The same sender nudging again under the same subject, in a new
    # conversation, folds onto the open kept row instead of a second row.
    again = _kept_rec(4, conv="c-oct20", received="2026-10-20T09:00:00Z", deadline="2026-10-22")
    open_row = {"id": "kept-c-oct13", "status": "open", "conversation_id": "c-oct13", "source_message_id": "m1",
                "from_email": nudge["from"], "description": nudge["subject"], "deadline": "2026-10-15",
                "metadata": {"kept_received": "2026-10-13T12:04:31Z"}}
    plan = mp.kept_row_plan(_index([nudge, again]), [open_row], user=user,
                            now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert plan["create"] == [] and len(plan["update"]) == 1
    rid, fields = plan["update"][0]
    assert rid == "kept-c-oct13" and fields["deadline"] == "2026-10-22" and "nudged again" in fields["last_action"]
    # Closure from the records: the user's own reply in the thread, or a
    # later inbound message that asks nothing.
    reply = _kept_rec(5, conv="c-oct13", received="2026-10-14T08:00:00Z", own=True)
    plan = mp.kept_row_plan(_index([nudge, reply]), [open_row], user=user, now=now)
    assert plan["close"] == [("kept-c-oct13", "the user replied in the thread on 2026-10-14",
                              "user_wrote_in_thread", "2026-10-14T08:00:00Z")]
    moved = _kept_rec(6, conv="c-oct13", received="2026-10-14T09:00:00Z", state="fyi", needs_reply=False, ask="")
    plan = mp.kept_row_plan(_index([nudge, moved]), [open_row], user=user, now=now)
    assert len(plan["close"]) == 1 and "asks nothing" in plan["close"][0][1]
    # Outside the window, nothing is created; a meeting notice whose date
    # has passed is over, not owed (a dated task past due still is).
    old = _kept_rec(7, conv="c-old", received="2026-09-01T12:00:00Z", deadline="2026-09-05")
    assert mp.kept_row_plan(_index([old]), [], user=user, now=now)["create"] == []
    past_meeting = _kept_rec(10, conv="c-mtg", received="2026-10-05T12:00:00Z", state="scheduling",
                             deadline="2026-10-06", kept="listed domain mil", frm="someone@army.mil",
                             subject="Invitation: CRADA call")
    past_meeting["result"]["scheduling"] = "propose"
    assert mp.kept_row_plan(_index([past_meeting]), [], user=user, now=now)["create"] == []
    late_task = _kept_rec(11, conv="c-late", received="2026-10-05T12:00:00Z", deadline="2026-10-07")
    assert mp.kept_row_plan(_index([late_task]), [], user=user, now=now)["create"][0]["type"] == "deadline"


def test_sync_kept_rows_applies_the_plan_through_the_seam():
    now = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    nudge = _kept_rec(1, conv="c-oct13", received="2026-10-13T12:04:31Z", deadline="2026-10-15")
    calls = []
    out = mp.sync_kept_rows(now=now, index=_index([nudge]), rows=[], user={"owner@example.edu"},
                            write=lambda verb, rid, fields: calls.append((verb, rid, fields["type"])))
    assert calls == [("create", "kept-c-oct13", "deadline")] and out["created"] == ["kept-c-oct13"]


def test_validate_coerces_every_field():
    rec = mp.validate({"sensitivity": "secret", "confidential_kind": "health",
                       "deadline": "next week", "tone": ["distress", "angry"],
                       "state": "later", "people": [{"name": " A "}, "junk"], "summary": 5})
    assert rec["sensitivity"] == "normal" and rec["confidential_kind"] == "none"
    assert rec["deadline"] == "" and rec["tone"] == ["distress"]
    assert rec["state"] == "fyi" and rec["people"] == [{"name": "A", "role": ""}]
    assert rec["summary"] == ""
    assert mp.validate({"sensitivity": "confidential"})["confidential_kind"] == "other"


def test_keep_reason_checks_every_participant_and_admissions(priv):
    cfg = rl.config()
    text = "Could we meet?"
    assert mp.keep_reason(_msg(1, cc=["admin@army.mil"]), text, cfg) == "listed domain mil"
    applicant = _msg(2, frm="someone@gmail.com", subject="Prospective PhD student")
    text = ("Dear Professor, I am applying to the PhD program and would like to join "
            "your lab. My GPA is 3.9 and my CV is attached. Are you accepting students?")
    assert mp.keep_reason(applicant, text, cfg) == "admissions"
    assert mp.keep_reason(_msg(3), text, cfg) == ""  # an internal sender is never an applicant


def test_tick_extracts_everything_labels_kept_and_skips_own_mail(priv):
    msgs = [_msg(1), _msg(2, cc=["someone@navy.mil"]), _msg(3, frm="owner@example.edu")]
    graph, calls = FakeGraph(msgs), []
    res = mp.tick(graph=graph, chat=_chat(calls=calls), now=NOW)
    assert res["ok"] == 2 and res["kept"] == 1 and res["skipped"] == 1
    assert len(calls) == 2 and len(graph.fetched) == 2  # the listed one goes to the model too
    recs = {r["conversation_id"]: r for r in rl.read(mp.EXTRACTIONS)}
    assert recs["conv2"]["keep_from_anthropic"] == "listed domain mil"
    assert recs["conv1"]["keep_from_anthropic"] == ""
    again = mp.tick(graph=graph, chat=_chat(), now=NOW)
    assert again["ok"] == 0 and again["skipped"] == 3


def test_redo_withheld_extracts_what_an_older_version_held_back(priv):
    msg = _msg(4, cc=["user@army.mil"])
    old = dict(mp._base_record(msg), status="withheld", reason="never-send domain mil")
    rl.append(mp.EXTRACTIONS, old)
    st = mp.load_state()
    st["seen"][old["key"]] = old["ts"]
    st["last_tick"] = "2026-10-09T15:00:00Z"
    mp.save_state(st)
    res = mp.redo_withheld(graph=FakeGraph([msg]), chat=_chat())
    assert res["redone"] == 1 and res["ok"] == 1
    mp._INDEX_CACHE.update(mtime=None, index=None)
    rec = mp.load_index()["by_key"][old["key"]]
    assert rec["status"] == "ok" and rec["keep_from_anthropic"] == "listed domain mil"


def test_tick_is_a_noop_with_the_switch_off(priv):
    owner_trust.write_owner_file(priv / rl.CONFIG_FILE, b'{"mail": "off"}', mode=0o600)
    assert mp.tick(graph=FakeGraph([_msg(1)]), chat=_chat()) == {"skipped": "mail switch is off"}


def test_errors_retry_then_stop(priv):
    def boom(messages, **kw):
        raise rl.RemoteLLMError("down")
    graph = FakeGraph([_msg(1)])
    for _ in range(mp.RETRY_LIMIT):
        assert mp.tick(graph=graph, chat=boom, now=NOW)["error"] == 1
    assert mp.tick(graph=graph, chat=boom, now=NOW)["error"] == 0


def test_note_release_classes_threads_and_never_raises(priv):
    conf = dict(GOOD, sensitivity="confidential", confidential_kind="student-record")
    mp.tick(graph=FakeGraph([_msg(1)]), chat=_chat(conf), now=NOW)
    out = json.dumps({"conversation_id": "conv1", "messages": [{"id": "AAMk" + "y" * 50}]})
    mp.note_release("graph_actions", "get-conversation", ["--conversation-id", "conv1"],
                    "run-1", out)
    mp.note_release("graph_actions", "get-message", [], "shadow-run-2", "not json")
    mp.note_release("graph_actions", "send-draft", [], "run-3", out)  # not a read: ignored
    mp.tick(graph=FakeGraph([_msg(5, cc=["user@army.mil"])]), chat=_chat(), now=NOW)
    mp.note_release("graph_actions", "get-conversation", ["--conversation-id", "conv5"],
                    "run-4", "{}")
    rel = rl.read(mp.RELEASES)
    assert len(rel) == 3
    assert rel[0]["classes"] == {"confidential": 1} and rel[0]["flagged_threads"] == ["conv1"]
    assert rel[1]["shadow"] is True
    assert rel[2]["classes"] == {"kept": 1} and rel[2]["flagged_threads"] == ["conv5"]


def test_shim_masks_personal_events():
    ev = {"id": "e1", "subject": "Dentist", "categories": ["Personal"],
          "start": {"dateTime": "2026-10-09T09:00:00.0000000"},
          "end": {"dateTime": "2026-10-09T10:00:00.0000000"},
          "attendees": [{}, {}], "showAs": "busy"}
    s = mp.shim_event(ev)
    assert s["title"] == "personal" and s["attendees"] == 2 and s["start"] == "2026-10-09T09:00:00"
    assert mp.shim_event(dict(ev, categories=["EA Hold"]))["ea_hold"] is True


def test_reduce_briefing_replaces_previews_and_stubs_confidential(priv):
    conf = dict(GOOD, sensitivity="confidential", confidential_kind="health")
    mp.tick(graph=FakeGraph([_msg(1)]), chat=_chat(), now=NOW)
    mp.tick(graph=FakeGraph([_msg(2)]), chat=_chat(conf), now=NOW)
    mp.tick(graph=FakeGraph([_msg(3, cc=["someone@army.mil"])]), chat=_chat(), now=NOW)
    briefing = {
        "new_needs_response": [
            {"message_id": _msg(1)["id"], "body_preview": "raw text one"},
            {"conversation_id": "conv2", "body_preview": "raw text two"},
            {"conversation_id": "conv3", "subject": "Range test", "body_preview": "raw text three"},
            {"conversation_id": "conv9", "body_preview": "raw text nine"}],
        "todays_events": [{"subject": "Lab meeting", "start": {"dateTime": "2026-10-09T10:00:00"},
                           "end": {"dateTime": "2026-10-09T11:00:00"}, "attendees": []}],
    }
    reduced, stats = mp.reduce_briefing(briefing, mp.load_index())
    items = reduced["new_needs_response"]
    assert items[0]["extracted"]["ask"] == "Pick a time." and "body_preview" not in items[0]
    assert items[1]["extracted"] == {"confidential": "health", "needs_reply": True,
                                     "deadline": "2026-10-30"}
    assert items[2] == {"conversation_id": "conv3", "subject": "(kept from Claude)",
                        "extracted": {"kept_from_anthropic": True, "needs_reply": True,
                                      "deadline": "2026-10-30"}}
    assert items[3]["body_preview"] == "raw text nine"   # never extracted
    assert stats == {"previews": 4, "replaced": 2, "confidential": 1, "kept": 1,
                     "unknown": 1, "events": 1}
    assert reduced["todays_events"][0]["title"] == "Lab meeting"


def test_measure_without_mail_reads(priv):
    mp.tick(graph=FakeGraph([_msg(1), _msg(2, cc=["user@army.mil"])]), chat=_chat(), now=NOW)
    m = mp.measure(with_mail=False, now=NOW)
    recs = {k: {kk: v.get(kk) for kk in ("status", "keep_from_anthropic", "keep_reason", "error")}
            for k, v in mp.load_index()["by_key"].items()}
    assert m["ok"] == 2, (m, recs)
    assert m["kept"] == 1 and m["kept_by_reason"] == {"list": 1}, (m, recs)
    text = mp.summary_text(m, {}, 1, cfg={"mail": "enforce", "scheduling": "off", "drafting": "remote",
                                          "ghost": {"mail": {"from": "2026-10-11", "until": "2026-10-17"}}})
    assert "Mail: 2 messages" in text
    assert text.startswith("*Remote LLM shadow run, day 1* (mail enforce, scheduling off; numbers only)")
    # The briefing reduction and the drafting A/B lines appear only while they run.
    assert "Briefing:" not in text and "Drafting A/B" not in text


# ── sealed: the remote model's record in place of the text ───────────

def _sealed(priv, mail="sealed"):
    cfg = json.loads((priv / rl.CONFIG_FILE).read_text())
    cfg["mail"] = mail
    owner_trust.write_owner_file(priv / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    mp._INDEX_CACHE.update(mtime=None, index=None)


def _store(msg, result=GOOD, **extra):
    rec = dict(mp._base_record(msg), status="ok", latency_ms=5, result=dict(result), **extra)
    rl.append(mp.EXTRACTIONS, rec)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    return rec


def test_sealed_get_message_returns_the_record_not_the_text(priv):
    _sealed(priv)
    msg = _msg(1)
    _store(msg)
    raw = json.dumps({"id": msg["id"], "subject": "Meeting", "from": {"address": "pat@example.edu"},
                      "to": [], "cc": [], "received": msg["receivedDateTime"],
                      "body": "SECRET BODY TEXT", "body_type": "text",
                      "conversation_id": "conv1"})
    out, refusal = mp.release_filter("graph_actions", "get-message",
                                     ["--message-id", msg["id"]], "room-run-1", raw)
    assert refusal == ""
    got = json.loads(out)
    assert "SECRET" not in out
    assert got["sealed"] is True and got["body"].startswith("Pat asks for a meeting.")
    assert got["remote_record"]["offered_times"] == ["2026-10-20T14:00:00"]
    assert got["id"] == msg["id"] and got["conversation_id"] == "conv1"


def test_every_unified_read_is_checked_as_its_graph_actions_twin():
    import mail_mediation as MM
    reads = {v for v, why in __import__("test_send_content_binding").NOT_DRAFT_CHANGING[
        "mail_actions"].items() if why == "read"} - {"preflight-check"}
    for verb in reads:
        tool, twin = mp.as_graph_read("mail_actions", verb, [verb, "--provider", "outlook"])
        assert tool == "graph_actions" and twin in mp.RELEASE_VERBS, verb
        assert twin in MM.SAFE["graph_actions"], verb
    # Gmail reads are not filtered on either CLI (gmail_client's are not)
    assert mp.as_graph_read("mail_actions", "search", ["search", "--provider", "gmail"]) == \
        ("mail_actions", "search")


def test_sealed_unified_outlook_reads_return_the_record(priv):
    import mail_client
    _sealed(priv)
    msg = dict(_msg(1), body={"contentType": "text", "content": "SECRET BODY TEXT"},
               bodyPreview="SECRET PREVIEW")
    _store(msg)
    one = mail_client._normalize_outlook_get_message(msg)
    out, refusal = mp.release_filter("mail_actions", "get-message",
                                     ["get-message", "--provider", "outlook", "--message-id",
                                      msg["id"]], "room-run-1", json.dumps(one))
    assert refusal == "" and "SECRET" not in out
    got = json.loads(out)
    assert got["sealed"] is True and got["body"].startswith("Pat asks for a meeting.")
    rows = [mail_client._normalize_outlook_search_result(msg)]
    out, refusal = mp.release_filter("mail_actions", "search",
                                     ["search", "--provider", "outlook", "--query", "x"],
                                     "room-run-2", json.dumps(rows))
    assert refusal == "" and "SECRET" not in out
    assert json.loads(out)[0]["preview"].startswith("Pat asks")
    # the same output as a Gmail read passes as gmail_client's does
    out, _ = mp.release_filter("mail_actions", "search", ["search", "--provider", "gmail"],
                               "room-run-3", json.dumps(rows))
    assert "SECRET" in out


def test_sealed_get_conversation_extracts_unread_and_own_mail_inline(priv):
    _sealed(priv)
    inbound, own = _msg(1, cid="convA"), _msg(2, frm="owner@example.edu", cid="convA")
    raw = json.dumps({"conversation_id": "convA", "subject": "Meeting", "message_count": 2,
                      "participants": ["pat@example.edu", "owner@example.edu"],
                      "messages": [
                          {"id": inbound["id"], "subject": "Meeting",
                           "from": {"address": "pat@example.edu"}, "to": ["owner@example.edu"],
                           "cc": [], "sent": inbound["receivedDateTime"], "is_draft": False,
                           "has_attachments": False, "text": "RAW INBOUND"},
                          {"id": own["id"], "subject": "RE: Meeting",
                           "from": {"address": "owner@example.edu"}, "to": ["pat@example.edu"],
                           "cc": [], "sent": own["receivedDateTime"], "is_draft": False,
                           "has_attachments": False, "text": "RAW OWN REPLY"}]})
    calls = []
    data = mp.seal_output("get-conversation", json.loads(raw), graph=FakeGraph([]),
                          chat=_chat(calls=calls), budget_s=10)
    text = json.dumps(data)
    assert "RAW" not in text and data["sealed"] is True
    assert len(calls) == 2                      # both were unread: extracted here
    m_in, m_own = data["messages"]
    assert m_in["text"].startswith("Pat asks") and "remote_record" in m_in
    assert m_own["own"] is True and m_own["text"].startswith("Pat asks")
    recs = mp.load_index()["by_id"]
    assert recs[own["id"]]["own"] is True and recs[inbound["id"]].get("own") is None


def test_sealed_list_rows_lose_previews_and_mark_unread(priv):
    _sealed(priv)
    known, unknown = _msg(1), _msg(2)
    _store(known)
    raw = json.dumps([
        {"id": known["id"], "subject": "Meeting", "from": "pat@example.edu",
         "received": known["receivedDateTime"], "conversation_id": "conv1",
         "preview": "RAW PREVIEW ONE"},
        {"id": unknown["id"], "subject": "Other", "from": "lee@example.edu",
         "received": unknown["receivedDateTime"], "conversation_id": "conv2",
         "preview": "RAW PREVIEW TWO"}])
    out, refusal = mp.release_filter("graph_actions", "search-mail", ["--query", "x"],
                                     "room-run-2", raw)
    assert refusal == "" and "RAW" not in out
    rows = json.loads(out)
    assert rows[0]["preview"].startswith("Pat asks") and "remote_unread" not in rows[0]
    assert rows[1]["remote_unread"] is True and rows[1]["preview"] == ""
    assert rows[-1]["_note"] == mp.SEALED_NOTE


def test_enforce_leaves_a_room_read_alone_and_sealed_keeps_the_cron_rules(priv):
    _sealed(priv, mail="enforce")
    msg = _msg(1)
    raw = json.dumps({"id": msg["id"], "body": "RAW", "conversation_id": "conv1"})
    assert mp.release_filter("graph_actions", "get-message", [], "room-run", raw) == (raw, "")
    _sealed(priv)
    kept = _msg(3, cc=["someone@navy.mil"])
    _store(kept, keep_from_anthropic="listed domain mil")
    raw = json.dumps({"id": kept["id"], "body": "RAW", "conversation_id": "conv3"})
    out, refusal = mp.release_filter("graph_actions", "get-message", [], "cron-morning", raw)
    assert out == "" and refusal == mp.KEPT_REFUSAL
    out, refusal = mp.release_filter("graph_actions", "get-message", [], "room-run", raw)
    assert refusal == "" and "RAW" not in out
    assert json.loads(out)["kept_from_claude"] == "listed domain mil"


def test_sealed_failure_refuses_the_read(priv, monkeypatch):
    _sealed(priv)
    monkeypatch.setattr(mp, "seal_output", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out, refusal = mp.release_filter("graph_actions", "get-message", [],
                                     "room-run", json.dumps({"id": "m", "body": "RAW"}))
    assert out == "" and refusal.startswith("The mail gate could not check this read")


# ── the thread brief ─────────────────────────────────────────────────

def test_thread_brief_composes_the_thread_and_its_latest_ask(priv):
    first = _msg(1, cid="convB", received="2026-10-09T10:00:00Z")
    own = _msg(2, frm="owner@example.edu", cid="convB", received="2026-10-09T11:00:00Z")
    second = _msg(3, cid="convB", received="2026-10-09T12:00:00Z")
    _store(first, dict(GOOD, ask="Pick a time.", summary="Pat asks to meet."))
    _store(own, dict(GOOD, ask="", needs_reply=False, summary="You offered Tuesday."), own=True)
    _store(second, dict(GOOD, ask="Confirm Tuesday 2pm.", deadline="2026-10-13",
                        summary="Pat takes Tuesday."))
    b = mp.thread_brief("convB", now=NOW)
    assert [m["own"] for m in b["messages"]] == [False, True, False]
    assert b["messages"][1]["gist"] == "You offered Tuesday." and "ask" not in b["messages"][1]
    assert b["ask"] == "Confirm Tuesday 2pm." and b["deadline"] == "2026-10-13"
    assert b["last_from_user"] == "2026-10-09T11:00:00Z" and b["user_replied_after_latest"] is False
    assert b["people"] == [{"name": "Pat", "role": "student"}]
    assert b["written_by"] == "remote model" and b["message_count"] == 3
    assert mp.thread_brief("nope") is None


def test_sync_briefs_writes_the_row_and_extracts_the_users_own_replies(priv):
    _sealed(priv, mail="enforce")
    inbound = _msg(1, cid="convC", received="2026-10-09T10:00:00Z")
    own = _msg(2, frm="owner@example.edu", cid="convC", received="2026-10-09T11:00:00Z")
    _store(inbound)

    class Graph(FakeGraph):
        def get_conversation_messages(self, cid, top=50, include_drafts=False):
            assert cid == "convC"
            return [dict(inbound, body={"contentType": "text", "content": "in"}),
                    dict(own, body={"contentType": "text", "content": "mine"})], False

    writes = []
    rows = [{"id": "row-1", "conversation_id": "convC"}, {"id": "row-2", "conversation_id": ""}]
    calls = []
    res = mp.sync_briefs(graph=Graph([]), chat=_chat(calls=calls), rows=rows,
                         write=lambda i, **f: writes.append((i, f)), now=NOW)
    assert res["rows"] == 1 and res["written"] == 1 and res["own_extracted"] == 1
    assert len(calls) == 1                       # only the user's own reply was unread
    rid, fields = writes[0]
    brief = fields["thread_brief"]
    assert rid == "row-1" and brief["own_checked"] is True
    assert [m["own"] for m in brief["messages"]] == [False, True]
    # Unchanged thread, brief on the row already: nothing is fetched or written.
    rows[0]["thread_brief"] = brief
    again = mp.sync_briefs(graph=Graph([]), chat=_chat(), rows=rows,
                           write=lambda i, **f: writes.append((i, f)), now=NOW)
    assert again["fetched"] == 0 and again["written"] == 0 and len(writes) == 1


def test_the_users_own_record_keeps_what_he_asked_them(priv):
    """Slides for the sensor-fusion working group: the user's reply asked how long
    his slot is. His record clears the ask of him and keeps the question he
    put to them, and the model is told whose message it is."""
    r = mp._own_result({"ask": "Say how long the slot is.", "state": "awaiting-reply",
                        "needs_reply": True, "deadline": "2026-10-26", "scheduling": "none"})
    assert r["asked_of_them"] == "Say how long the slot is." and r["awaits_reply"] is True
    assert r["ask"] == "" and r["needs_reply"] is False and r["deadline"] == "" and r["state"] == "fyi"
    plain = mp._own_result({"ask": "", "state": "fyi", "scheduling": "none"})
    assert plain["awaits_reply"] is False and plain["asked_of_them"] == ""
    # The model calls a plain "thanks" awaiting-reply too; only an ask waits.
    thanks = mp._own_result({"ask": "", "state": "awaiting-reply", "scheduling": "none"})
    assert thanks["awaits_reply"] is False
    calls = []
    mp.process(_msg(4, frm="owner@example.edu"), graph=FakeGraph([]), cfg=rl.config(),
               chat=_chat(calls=calls), own=True)
    assert mp.OWN_NOTE in calls[0][1]["content"]


def test_the_users_own_message_goes_to_the_frozen_setup_without_the_thread(priv, monkeypatch):
    """Given the thread, the model took the ask his reply answered for the
    one he made; his own message is read alone."""
    import mail_tuning
    seen = []
    monkeypatch.setattr(mp, "_frozen_setup", lambda: {"context": True, "candidate": "c-test"})
    monkeypatch.setattr(mail_tuning, "conversation", lambda graph, cid: seen.append(cid) or [{"x": 1}])
    monkeypatch.setattr(mail_tuning, "live_row", lambda msg, body, conv, user: {"conv": conv})
    rows = []
    monkeypatch.setattr(mail_tuning, "run_frozen", lambda fz, row, chat=None: rows.append(row) or (
        {"state": "fyi", "summary": "s", "ask": ""}, {"latency_ms": 1}))
    monkeypatch.setattr(mail_tuning, "to_record", lambda ans: dict(GOOD, **ans))
    mp.process(_msg(6, frm="owner@example.edu"), graph=FakeGraph([]), cfg=rl.config(), own=True)
    mp.process(_msg(7), graph=FakeGraph([]), cfg=rl.config())
    assert seen == ["conv7"] and rows[0]["conv"] == [] and rows[1]["conv"] == [{"x": 1}]


def test_a_thread_that_ends_on_the_users_question_waits_on_them(priv):
    ask = _msg(1, cid="convW", received="2026-10-16T01:51:21Z")
    mine = _msg(2, frm="owner@example.edu", cid="convW", received="2026-10-16T02:53:58Z")
    _store(ask, dict(GOOD, ask="Send your slides.", state="todo", summary="Flo asks for slides."))
    _store(mine, mp._own_result(dict(GOOD, ask="Say how long the slot is.", state="awaiting-reply",
                                     summary="You asked for the slot length.")), own=True)
    b = mp.thread_brief("convW", now=NOW)
    assert b["waiting_on_reply"] == {"since": "2026-10-16T02:53:58Z", "asked": "Say how long the slot is."}
    assert b["messages"][1]["asked"] == "Say how long the slot is."
    # Their answer ends the wait; the row is the user's again.
    _store(_msg(3, cid="convW", received="2026-10-16T18:00:00Z"),
           dict(GOOD, ask="Ten minutes; slides by Thursday.", state="todo"))
    assert "waiting_on_reply" not in mp.thread_brief("convW", now=NOW)
    # A reply that asks nothing does not wait.
    _store(_msg(5, frm="owner@example.edu", cid="convX", received="2026-10-16T03:00:00Z"),
           mp._own_result(dict(GOOD, ask="", state="fyi")), own=True)
    assert "waiting_on_reply" not in mp.thread_brief("convX", now=NOW)


def test_sync_briefs_refetches_a_thread_the_user_wrote_in(priv):
    """The brief was written before the user replied. His reply is the
    newest message and nothing came in after it, so only his send can say
    the thread changed."""
    _sealed(priv, mail="enforce")
    inbound = _msg(1, cid="convS", received="2026-10-09T10:00:00Z")
    own = _msg(2, frm="owner@example.edu", cid="convS", received="2026-10-09T17:00:00Z")
    _store(inbound)
    thread = [dict(inbound, body={"contentType": "text", "content": "in"})]

    class Graph(FakeGraph):
        def get_conversation_messages(self, cid, top=50, include_drafts=False):
            return list(thread), False

    writes = []
    rows = [{"id": "row-s", "conversation_id": "convS"}]
    mp.sync_briefs(graph=Graph([]), chat=_chat(), rows=rows, own_sent={},
                   write=lambda i, **f: writes.append((i, f)), now=NOW)
    rows[0]["thread_brief"] = writes[-1][1]["thread_brief"]
    assert rows[0]["thread_brief"]["updated"] == "2026-10-09T16:00:00Z"
    # A send older than the brief changes nothing.
    quiet = mp.sync_briefs(graph=Graph([]), chat=_chat(), rows=rows,
                           own_sent={"convS": "2026-10-09T09:00:00Z"},
                           write=lambda i, **f: writes.append((i, f)), now=NOW)
    assert quiet["fetched"] == 0 and len(writes) == 1
    # His reply at 17:00, after the brief: fetched, extracted, rewritten.
    thread.append(dict(own, body={"contentType": "text", "content": "How long is my slot?"}))
    asked = dict(GOOD, ask="Say how long the slot is.", state="awaiting-reply")
    res = mp.sync_briefs(graph=Graph([]), chat=_chat(asked), rows=rows,
                         own_sent={"convS": "2026-10-09T17:00:00Z"},
                         write=lambda i, **f: writes.append((i, f)),
                         now=NOW + timedelta(hours=2))
    assert res["fetched"] == 1 and res["own_extracted"] == 1 and res["written"] == 1
    brief = writes[-1][1]["thread_brief"]
    assert brief["message_count"] == 2 and brief["user_replied_after_latest"] is True
    assert brief["waiting_on_reply"]["since"] == "2026-10-09T17:00:00Z"


def test_the_tick_keeps_the_users_latest_send_per_thread(priv):
    mine = dict(_msg(3, frm="owner@example.edu", cid="convT"), sentDateTime="2026-10-09T13:00:00Z")
    mp.tick(graph=FakeGraph([_msg(1), mine]), chat=_chat(), now=NOW)
    assert mp.load_state()["own_sent"]["convT"] == "2026-10-09T13:00:00Z"


def test_a_kept_row_is_not_proposed_done_by_the_users_question(priv):
    now = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    nudge = _kept_rec(1, conv="c-sl", received="2026-10-16T01:51:21Z", kept="listed domain mil",
                      frm="someone@us.af.mil", subject="Slides for Sensor-Fusion Working Group")
    question = _kept_rec(2, conv="c-sl", received="2026-10-16T02:53:58Z", own=True)
    question["result"] = mp._own_result(dict(question["result"], ask="Say how long the slot is.",
                                             state="awaiting-reply"))
    row = {"id": "kept-c-sl", "status": "open", "conversation_id": "c-sl", "source_message_id": "m1",
           "from_email": "someone@us.af.mil", "description": "Slides for Sensor-Fusion Working Group",
           "deadline": "", "metadata": {"kept_received": "2026-10-16T01:51:21Z"}}
    plan = mp.kept_row_plan(_index([nudge, question]), [row], user={"owner@example.edu"}, now=now)
    assert plan["close"] == []


def test_sync_briefs_is_a_noop_unless_mail_is_enforced(priv):
    assert "skipped" in mp.sync_briefs(rows=[{"id": "r", "conversation_id": "c"}])


def test_ledger_set_metadata_merges_without_touching_updated_at(tmp_path):
    import sqlite3
    import ledger
    db = tmp_path / "l.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ledger (id TEXT PRIMARY KEY, metadata TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO ledger VALUES ('r1', '{\"a\": 1}', 't0')")
    conn.commit()
    conn.close()
    assert ledger.set_metadata("r1", db_path=db, thread_brief={"ask": "x"}) is True
    assert ledger.set_metadata("nope", db_path=db, thread_brief={}) is False
    conn = ledger.guarded_connect(db)
    meta, upd = conn.execute("SELECT metadata, updated_at FROM ledger WHERE id='r1'").fetchone()
    conn.close()
    assert json.loads(meta) == {"a": 1, "thread_brief": {"ask": "x"}} and upd == "t0"


# ── what a paraphrase loses: links, quotes, files; the user's own mail ──

def test_process_adds_links_quotes_and_files_and_guards_offered_times(priv):
    body = ("Could we meet Tue Oct 20 at 2pm? The fee is $3,000; the form is at "
            "https://x.example/form. I could do 8:30 MT most days next week.")
    msg = dict(_msg(1, received="2026-10-15T14:00:00Z"), hasAttachments=True)

    class G(FakeGraph):
        def get_attachments(self, mid):
            return [{"id": "a1", "name": "award.pdf", "contentType": "application/pdf", "size": 1000,
                     "contentBytes": ""}, {"id": "a2", "name": "photo.png", "contentType": "image/png", "size": 5}]

        def download_attachment(self, mid, aid):
            raise RuntimeError("no pdf here")
    res = dict(GOOD, offered_times=["2026-10-20T14:00:00", "2026-10-21T08:30:00", "2026-10-22T08:30:00"],
               summary="Pat asks to meet and names a fee.")
    rec = mp.process(msg, graph=G([msg], body=body), cfg=rl.config(), chat=_chat(res))
    r = rec["result"]
    assert r["links"] == ["https://x.example/form"]
    assert r["offered_times"] == ["2026-10-20T14:00:00"]          # the 8:30 "most days" slots were inferred
    assert any("$3,000" in q for q in r["quotes"]) and len(r["quotes"]) <= 3
    assert [a["name"] for a in r["attachments"]] == ["award.pdf", "photo.png"]
    assert r["attachments"][0].get("error")                        # unreadable PDF is noted, not fatal
    rl.append(mp.EXTRACTIONS, rec)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    assert "award.pdf" in mp.thread_brief("conv1")["messages"][0]["attachments"]
    assert mp.thread_brief("conv1")["links"] == ["https://x.example/form"]
    import mail_gate
    assert "Links: https://x.example/form" in mail_gate.remote_text(rec) and "Files: award.pdf" in mail_gate.remote_text(rec)
    assert mp._sealed_fields(rec)["remote_record"]["links"] == ["https://x.example/form"]


def test_own_message_record_carries_no_ask_deadline_or_needs_reply(priv):
    msg = _msg(2, frm="owner@example.edu")
    res = dict(GOOD, needs_reply=True, ask="Confirm the time.", deadline="2026-10-09", state="needs-reply",
               scheduling="proposes-times", offered_times=["2026-10-20T14:00:00"])
    calls = []
    rec = mp.process(msg, graph=FakeGraph([msg], body="I could do Tue Oct 20 at 2pm."), cfg=rl.config(),
                     chat=_chat(res, calls=calls), full=None, own=True)
    r = rec["result"]
    assert rec["own"] is True and r["needs_reply"] is False and r["ask"] == "" and r["deadline"] == ""
    assert r["state"] == "scheduling" and r["offered_times"] == ["2026-10-20T14:00:00"]
    assert f"\n\n{mp.OWN_NOTE}\n\n" in calls[0][1]["content"]
    # What he asked them is kept: the next move is theirs.
    assert r["asked_of_them"] == "Confirm the time." and r["awaits_reply"] is True


def test_validate_keeps_code_side_fields_when_present():
    rec = mp.validate(dict(GOOD, links=["https://a.example", 5], quotes=["q1"], attachments=[{"name": "f.pdf"}, "x"]))
    assert rec["links"] == ["https://a.example"] and rec["quotes"] == ["q1"]
    assert rec["attachments"] == [{"name": "f.pdf"}]
    assert "links" not in mp.validate(dict(GOOD))
