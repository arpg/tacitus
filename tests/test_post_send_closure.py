#!/usr/bin/env python3
"""The Send button must close the obligation it just answered.

`ledger.py search --query` runs a LIKE over description, notes, from_name,
from_email and last_action, and not over the conversation_id COLUMN, so
looking a row up by conversation id that way finds nothing and the button
closes nothing. CLAUDE.md classes a user-stated closure as a defect in
the closure pipeline.

Pinned here, bottom up:
  1. ledger.py search --conversation-id finds the row that --query cannot.
  2. the shared bot helper closes it, announces it (do-and-tell), and skips
     the archive on the send path.
  3. both call sites go through that helper, so the copy cannot rot apart
     again.
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, so a run of this file alone must not see it.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-post-send-")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
# The helper imports preprocessor.ledger, as the bot does from the repo root.
sys.path.insert(1, str(ROOT))

from chat.memory_frontend import MemoryFrontend  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

CONV = "AAQkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQAQAQQQQQQ="


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# 1. the ledger lookup itself
# ---------------------------------------------------------------------------

def _ledger(tmp: Path):
    """A ledger module bound to a private DB."""
    import ledger as ledger_mod
    ledger_mod.DB_PATH = tmp / "obligations-ledger.db"
    return ledger_mod


def _add(ledger_mod, entry_id: str, conv_id: str, *, status="open",
         source=None, description="") -> None:
    db = ledger_mod._get_db()
    now = ledger_mod._now()
    db.execute(
        """INSERT INTO ledger (id, type, status, description, notes,
               source_message_id, conversation_id, date_identified,
               last_action, metadata, created_at, updated_at, source)
           VALUES (?, 'response_needed', ?, ?, '', '', ?, ?, '', '{}',
                   ?, ?, ?)""",
        (entry_id, status, description or f"desc {entry_id}", conv_id, now,
         now, now, source))
    db.commit()
    db.close()


def _search(ledger_mod, **kw):
    import io
    from contextlib import redirect_stdout
    args = types.SimpleNamespace(query=None, conversation_id=None, status=None,
                                 limit=20, source=None, include_plugins=False)
    for k, v in kw.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ledger_mod.cmd_search(args)
    return json.loads(buf.getvalue())


def test_query_search_cannot_find_a_row_by_its_conversation_id():
    """The --query LIKE cannot reach the conversation_id column."""
    with tempfile.TemporaryDirectory() as td:
        lm = _ledger(Path(td))
        _add(lm, "oakley-committee", CONV)
        assert _search(lm, query=CONV)["count"] == 0, (
            "the LIKE covers text columns only; if this ever passes, the "
            "conversation id leaked into prose, not into the column")


def test_conversation_id_search_finds_it():
    with tempfile.TemporaryDirectory() as td:
        lm = _ledger(Path(td))
        _add(lm, "oakley-committee", CONV)
        out = _search(lm, conversation_id=CONV)
        assert [e["id"] for e in out["entries"]] == ["oakley-committee"]
        assert out["conversation_id"] == CONV


def test_conversation_id_search_honours_status_and_source_filters():
    with tempfile.TemporaryDirectory() as td:
        lm = _ledger(Path(td))
        _add(lm, "open-row", CONV)
        _add(lm, "done-row", CONV, status="completed")
        _add(lm, "plugin-row", CONV, source="plugin:example-notes")
        ids = {e["id"] for e in
               _search(lm, conversation_id=CONV, status="open")["entries"]}
        assert ids == {"open-row"}, ids
        both = {e["id"] for e in
                _search(lm, conversation_id=CONV,
                        status="open", include_plugins=True)["entries"]}
        assert both == {"open-row", "plugin-row"}, both


def test_search_still_requires_one_of_the_two():
    with tempfile.TemporaryDirectory() as td:
        lm = _ledger(Path(td))
        out = _search(lm)
        assert "required" in out.get("error", "")


def test_the_cli_exposes_conversation_id_end_to_end():
    """Through argparse and a real subprocess: this is how the bot calls it."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir(parents=True)
        env = dict(os.environ, EA_DIR=str(tmp))
        add = subprocess.run(
            [sys.executable, str(ROOT / "preprocessor" / "ledger.py"), "add",
             "--id", "oakley-committee", "--type", "response_needed",
             "--description", "Oakley committee request",
             "--conversation-id", CONV],
            capture_output=True, text=True, env=env, timeout=60)
        assert add.returncode == 0, add.stderr
        found = subprocess.run(
            [sys.executable, str(ROOT / "preprocessor" / "ledger.py"),
             "search", "--conversation-id", CONV, "--status", "open"],
            capture_output=True, text=True, env=env, timeout=60)
        out = json.loads(found.stdout)
        assert [e["id"] for e in out["entries"]] == ["oakley-committee"], out


# ---------------------------------------------------------------------------
# 2. the bot helper
# ---------------------------------------------------------------------------

def _closure_ns():
    frontend = MemoryFrontend()
    ns = {
        "json": json,
        "datetime": datetime,
        "timezone": timezone,
        "SCRIPT_DIR": ROOT,
        "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                     debug=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
        "FRONTEND": frontend,
    }
    _exec_block("_LEDGER_OPEN_STATUSES = (", "def _execute_send", ns)
    return ns, frontend.posts


def _fake_ledger_cli(calls, entries):
    """Stand in for `ledger.py`: answers search, records close."""
    def run(cmd, **kwargs):
        calls.append(list(cmd))
        result = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        if "search" in cmd:
            result.stdout = json.dumps({"count": len(entries),
                                        "entries": entries})
        else:
            result.stdout = json.dumps({"ok": True, "action": "proposed" if "propose" in cmd else "closed"})
        return result
    return run


def test_helper_looks_up_by_conversation_id_and_closes():
    ns, posted = _closure_ns()
    calls = []
    entry = {"id": "oakley-committee", "type": "response_needed", "conversation_id": CONV,
             "status": "open", "description": "Oakley committee request"}
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, [entry])):
        closed = ns["_close_ledger_for_conversation"](
            "C1", "T1", CONV, "Email sent via EA Send button",
            skip_archive=True)
    assert closed is True
    search_cmd = calls[0]
    assert "search" in search_cmd
    assert "--conversation-id" in search_cmd, search_cmd
    assert CONV in search_cmd
    assert "--query" not in search_cmd, (
        "the full-text search cannot match a conversation id")
    close_cmd = calls[1]
    assert close_cmd[2:5] == ["close", "--id", "oakley-committee"], close_cmd
    assert "--skip-archive" in close_cmd
    # do-and-tell: the closure is announced in the thread
    assert posted and "Closed obligation" in posted[0]["text"]
    assert posted[0]["thread_ts"] == "T1"


def test_helper_archives_when_not_told_to_skip():
    """The 'Sent myself' path lets ledger.py close do its own archive."""
    ns, _ = _closure_ns()
    calls = []
    entry = {"id": "x", "type": "response_needed", "conversation_id": CONV, "status": "open"}
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, [entry])):
        ns["_close_ledger_for_conversation"](
            "C1", "T1", CONV, "User sent from Outlook directly",
            skip_archive=False)
    assert "--skip-archive" not in calls[1]


def test_helper_closes_the_reply_row_before_a_task_on_the_thread():
    """'Sent myself' on the partner action-items reply closed the white paper
    task, the first row the search returned. The send answers the detector
    row; a task the EA logged from the thread waits for its own evidence."""
    ns, _ = _closure_ns()
    calls = []
    rows = [{"id": "field-trial-whitepaper", "type": "task",
             "conversation_id": CONV, "status": "open"},
            {"id": "response-partner", "type": "response_needed",
             "conversation_id": CONV, "status": "open"}]
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, rows)):
        assert ns["_close_ledger_for_conversation"](
            "C1", "T1", CONV, "User sent from Outlook directly", skip_archive=False)
    assert calls[1][2:5] == ["close", "--id", "response-partner"], calls
    assert len(calls) == 2


def test_helper_never_closes_a_wait_on_someone_else():
    """The partner owes the test plan; Kit's own send does not deliver it."""
    ns, posted = _closure_ns()
    calls = []
    rows = [{"id": "field-trial-pilot-plan", "type": "waiting",
             "conversation_id": CONV, "status": "open"}]
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, rows)):
        closed = ns["_close_ledger_for_conversation"](
            "C1", "T1", CONV, "User sent from Outlook directly", skip_archive=False)
    assert closed is False and len(calls) == 1 and posted == []


def test_helper_ignores_a_row_from_another_conversation():
    ns, posted = _closure_ns()
    calls = []
    entry = {"id": "other", "conversation_id": "SOMETHING-ELSE",
             "status": "open"}
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, [entry])):
        closed = ns["_close_ledger_for_conversation"](
            "C1", "T1", CONV, "note", skip_archive=True)
    assert closed is False
    assert len(calls) == 1, "nothing may be closed"
    assert posted == []


def test_helper_covers_the_statuses_the_briefing_treats_as_open():
    ns, _ = _closure_ns()
    import ledger as ledger_mod
    assert tuple(ns["_LEDGER_OPEN_STATUSES"]) == ledger_mod.OPEN_STATUSES


def test_after_send_falls_back_to_the_draft_id_in_notes():
    """Strategy 2 survives: outbound-email entries carry the draft id."""
    ns, posted = _closure_ns()
    calls = []
    draft_id = "AAMkAGRAFT-1234567890"
    entry = {"id": "outbound-x", "status": "open", "notes": f"draft {draft_id}",
             "description": "outbound"}

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        result = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        if "search" in cmd and "--conversation-id" in cmd:
            result.stdout = json.dumps({"count": 0, "entries": []})
        elif "search" in cmd:
            result.stdout = json.dumps({"count": 1, "entries": [entry]})
        else:
            result.stdout = json.dumps({"ok": True, "action": "proposed" if "propose" in cmd else "closed"})
        return result

    with patch("subprocess.run", side_effect=run):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, draft_id)
    assert any(c[2] == "close" and c[4] == "outbound-x" for c in calls), calls
    assert posted and "Closed obligation" in posted[0]["text"]


def test_a_send_in_a_thread_leaves_a_deadline_row_open():
    """A reply to Nico about a meeting closed the field-trial submission
    row, a deadline logged from the same thread, twice in one day. A send
    finishes a task, deadline or meeting row only when the draft names it."""
    ns, posted = _closure_ns()
    calls = []
    rows = [{"id": "field-trial-submission", "type": "deadline",
             "conversation_id": CONV, "status": "open"}]
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, rows)):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, "AAMkAGRAFT-meeting-reply")
    assert not [c for c in calls if "close" in c], calls
    assert posted == []
    # It goes on the confirm card instead: the send may have finished it.
    props = [c for c in calls if "propose" in c]
    assert len(props) == 1 and "field-trial-submission" in props[0], calls
    assert props[0][props[0].index("--signal") + 1] == "reply_in_thread"
    # The send's time is the evidence clock: once the user says "Still
    # open", the scrubber's view of the same send proposes nothing again.
    assert props[0][props[0].index("--evidence-ts") + 1].endswith("Z")


def test_an_outlook_send_proposes_with_its_own_send_time():
    ns, _ = _closure_ns()
    calls = []
    rows = [{"id": "kept-slides", "type": "deadline", "conversation_id": CONV, "status": "open"}]
    with patch("subprocess.run", side_effect=_fake_ledger_cli(calls, rows)):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, "AAMkAGRAFT-ack",
                                      how="outlook_send", sent_at="2026-10-16T02:53:58Z")
    props = [c for c in calls if "propose" in c]
    assert props and props[0][props[0].index("--evidence-ts") + 1] == "2026-10-16T02:53:58Z"


def _linked_cli(calls, rows_by_id, conv_rows):
    """`ledger.py` for a draft that names rows: get answers from
    rows_by_id, the conversation search from conv_rows."""
    def run(cmd, **kwargs):
        calls.append(list(cmd))
        r = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        if "get" in cmd:
            r.stdout = json.dumps(rows_by_id.get(cmd[cmd.index("--id") + 1],
                                                 {"error": "not found"}))
        elif "search" in cmd and "--conversation-id" in cmd:
            r.stdout = json.dumps({"count": len(conv_rows), "entries": conv_rows})
        elif "search" in cmd:
            r.stdout = json.dumps({"count": 0, "entries": []})
        else:
            r.stdout = json.dumps({"ok": True, "action": "proposed" if "propose" in cmd else "closed"})
        return r
    return run


def _closed_ids(calls):
    return [c[c.index("--id") + 1] for c in calls if "close" in c]


def test_a_draft_that_names_its_row_closes_it_and_the_reply_row():
    """The draft names the task it delivers (--ledger-id): that row closes
    whatever its type, the thread's reply row closes as before, and a
    deadline on the same thread that the draft does not name stays open."""
    from preprocessor import draft_events
    ns, posted = _closure_ns()
    calls = []
    task = {"id": "send-zion-doc", "type": "task", "status": "open",
            "description": "Send Zion the honors doc"}
    conv_rows = [{"id": "response-zion", "type": "response_needed",
                  "conversation_id": CONV, "status": "open"},
                 {"id": "rio-seed-deadline", "type": "deadline",
                  "conversation_id": CONV, "status": "open"}]
    with patch.object(draft_events, "ledger_ids_of",
                      return_value=["send-zion-doc", "no-such-row"]), \
            patch("subprocess.run", side_effect=_linked_cli(calls, {task["id"]: task}, conv_rows)):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, "AAMkAGRAFT-1")
    assert _closed_ids(calls) == ["send-zion-doc", "response-zion"], calls
    assert [p["text"] for p in posted][0].endswith("Send Zion the honors doc")
    named, reply = [c for c in calls if "close" in c]
    # The named row is on another thread: it closes alone, as a linked send.
    assert named[named.index("--signal") + 1] == "linked_send" and "--no-sweep" in named
    # The reply row is answered by the send itself, earlier asks with it.
    assert reply[reply.index("--signal") + 1] == "send_reply_row" and "--no-sweep" not in reply
    # The deadline on the thread is proposed, not closed.
    assert any("propose" in c and "rio-seed-deadline" in c for c in calls), calls


def test_each_way_a_draft_leaves_names_its_own_signal():
    ns, _ = _closure_ns()
    outcomes = ns["_DRAFT_OUTCOMES"]
    assert outcomes["send"][2:4] == ("linked_send", "send_reply_row")
    assert outcomes["sent_myself"][1] is False and outcomes["sent_myself"][2] == "user_sent_myself"
    assert outcomes["handled"][2] == "user_handled"
    assert outcomes["outlook_send"][2] == "linked_send"
    assert set(outcomes) == set(ns["_LEFT_OPEN_WHY"])
    # Every way a draft leaves archives the mail its close settles: the
    # owner never archives by hand what a send closed.
    assert not any(skip for _note, skip, *_rest in outcomes.values())


def test_a_send_closes_its_row_with_the_archive_and_waits_for_it():
    ns, _ = _closure_ns()
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("timeout")))
        return types.SimpleNamespace(stdout='{"action": "closed"}', returncode=0)
    with patch("subprocess.run", side_effect=run):
        ns["_close_ledger_row"]("C1", "T1", {"id": "response-a"}, "sent", skip_archive=False,
                                signal="send_reply_row", announced=[])
    ((cmd, timeout),) = calls
    assert "--skip-archive" not in cmd and timeout == 60


def test_a_named_row_closes_once_even_when_it_is_the_reply_row():
    from preprocessor import draft_events
    ns, _ = _closure_ns()
    calls = []
    row = {"id": "response-pat", "type": "response_needed", "conversation_id": CONV,
           "status": "open"}
    with patch.object(draft_events, "ledger_ids_of", return_value=["response-pat"]), \
            patch("subprocess.run", side_effect=_linked_cli(calls, {row["id"]: row}, [row])):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, "AAMkAGRAFT-2")
    assert _closed_ids(calls) == ["response-pat"], calls


def test_a_named_row_that_waits_on_someone_else_stays_open():
    """A nudge that names the contractor drawings row does not deliver the
    drawings."""
    from preprocessor import draft_events
    ns, posted = _closure_ns()
    calls = []
    wait = {"id": "lab-contractor-drawings", "type": "waiting", "status": "open"}
    with patch.object(draft_events, "ledger_ids_of", return_value=[wait["id"]]), \
            patch("subprocess.run", side_effect=_linked_cli(calls, {wait["id"]: wait}, [])):
        ns["_close_ledger_for_draft"]("C1", "T1", CONV, "AAMkAGRAFT-3")
    assert _closed_ids(calls) == [] and posted == []


def test_a_closed_named_row_is_not_closed_again():
    from preprocessor import draft_events
    ns, _ = _closure_ns()
    calls = []
    done = {"id": "send-x", "type": "task", "status": "completed"}
    with patch.object(draft_events, "ledger_ids_of", return_value=["send-x"]), \
            patch("subprocess.run", side_effect=_linked_cli(calls, {"send-x": done}, [])):
        ns["_close_ledger_for_draft"]("C1", "T1", "", "AAMkAGRAFT-4")
    assert _closed_ids(calls) == []


def test_closes_on_reply_by_row_kind():
    import ledger as lm
    for t in ("response_needed", "follow_up", "draft_pending"):
        assert lm.closes_on_reply({"id": "row", "type": t}), t
    for t in ("task", "deadline", "scheduling", "reimbursement", "dispute", "waiting"):
        assert not lm.closes_on_reply({"id": "row", "type": t}), t
    # A detector row is the "needs a reply" row, whatever its type.
    assert lm.closes_on_reply({"id": "sched-abc", "type": "scheduling"})
    assert not lm.closes_on_reply({"id": "response-x", "type": "waiting"})


# ---------------------------------------------------------------------------
# 3. both call sites use the helper
# ---------------------------------------------------------------------------

def _fn_body(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    return BOT_SRC[start:end]


def test_sent_myself_uses_the_same_helper():
    body = _fn_body("handle_sent_myself")
    assert "_close_ledger_for_draft(" in body, (
        "the 'Sent myself' button had its own copy of the broken search")
    assert '"search", "--query", conversation_id' not in body


def test_every_close_path_goes_through_the_draft_helper():
    """Send, Sent myself, Handled and a send from Outlook all close by the
    draft, so a named row closes and an unnamed task stays open on each."""
    for fn in ("_execute_send", "handle_sent_myself", "handle_draft_handled",
               "_reconcile_vanished_drafts"):
        assert "_close_ledger_for_draft(" in _fn_body(fn), fn
    assert "_close_ledger_after_send" not in BOT_SRC
    assert BOT_SRC.count("_close_ledger_for_conversation(") == 2, \
        "only its definition and _close_ledger_for_draft"


def test_no_full_text_search_by_conversation_id_survives_anywhere():
    assert '"search", "--query", conversation_id' not in BOT_SRC, (
        "a conversation id passed to --query matches nothing")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
