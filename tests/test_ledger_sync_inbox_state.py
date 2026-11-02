#!/usr/bin/env python3
"""Tests for ledger_sync's inbox arbitration.

Outlook rewrites a message's id on every folder move and nothing writes
the new id back to the ledger, so a stored `source_message_id` goes stale
the first time the message is archived, snoozed or woken. A Graph 404
therefore cannot mean "the user archived it": that would close rows the
system had just woken back into the inbox on purpose.

Arbitration has three outcomes and only one of them closes:

    ARCHIVED  demonstrably out of the inbox      -> close
    IN_INBOX  demonstrably in the inbox          -> defer (+ heal the id)
    UNKNOWN   dead id, thread not re-resolvable  -> defer and report

The tests drive `_message_inbox_state` and `cmd_check` against a fake
Graph, so every branch is exercised without a token.
"""

import json
import re
import sys
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import ledger as ledger_mod  # noqa: E402
import ledger_sync  # noqa: E402
import retry_policy  # noqa: E402

INBOX_ID = "FOLDER-INBOX"
ARCHIVE_ID = "FOLDER-ARCHIVE"


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test runs against a temp EA_DIR and a temp ledger DB.

    Nothing in this file may reach the production state directory.
    """
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(ledger_mod, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(ledger_sync, "AUTONOMY_FILE",
                        tmp_path / "ea" / "config" / "autonomy-tiers.json")
    monkeypatch.setattr(ledger_sync, "EA_DIR", tmp_path / "ea")
    monkeypatch.setattr(ledger_sync, "_load_env", lambda: None)
    monkeypatch.setattr(retry_policy, "TICK_BACKOFF_FILE", tmp_path / "tick-backoff.json")


class FakeGraph:
    """Minimal stand-in for graph_client.

    `messages` maps message id -> parentFolderId. An id that is absent
    404s, which is exactly what Outlook does to an id after a move.
    `conversations` maps conversationId -> list of message dicts.
    """

    def __init__(self, messages=None, conversations=None,
                 conversation_raises=False):
        self.messages = dict(messages or {})
        self.conversations = dict(conversations or {})
        self.conversation_raises = conversation_raises
        self.calls: list = []

    def graph_get(self, endpoint, params=None):
        self.calls.append(endpoint)
        if endpoint == "/me/mailFolders/inbox":
            return {"id": INBOX_ID}
        if endpoint == "/me/messages":
            if self.conversation_raises:
                raise RuntimeError("graph exploded")
            filt = (params or {}).get("$filter", "")
            m = re.search(r"conversationId eq '([^']*)'", filt)
            cid = m.group(1) if m else ""
            return {"value": list(self.conversations.get(cid, []))}
        if endpoint.startswith("/me/messages/"):
            mid = endpoint.split("/me/messages/", 1)[1]
            if mid in self.messages:
                return {"parentFolderId": self.messages[mid]}
            raise _http_error(404)
        raise AssertionError(f"unexpected endpoint {endpoint}")


def _http_error(code: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(response=resp)


def _use(monkeypatch, graph) -> None:
    monkeypatch.setattr(ledger_sync, "_GRAPH", graph)


def _msg(mid, folder, received="2026-08-20T10:00:00Z", is_draft=False):
    return {"id": mid, "parentFolderId": folder,
            "receivedDateTime": received, "isDraft": is_draft}


def _add_row(entry_id, *, msg_id, conv_id="", status="open",
             snooze_until=None, follow_up_after=None, source=None):
    db = ledger_mod._get_db()
    now = ledger_mod._now()
    db.execute(
        """INSERT INTO ledger (id, type, status, description,
               source_message_id, conversation_id, snooze_until,
               follow_up_after, date_identified, last_action, notes,
               metadata, created_at, updated_at, source)
           VALUES (?, 'response_needed', ?, ?, ?, ?, ?, ?, ?, '', '', '{}',
                   ?, ?, ?)""",
        (entry_id, status, f"desc {entry_id}", msg_id, conv_id, snooze_until,
         follow_up_after, now, now, now, source))
    db.commit()
    db.close()


def _row(entry_id):
    db = ledger_mod._get_db()
    r = db.execute(
        "SELECT status, source_message_id, last_action FROM ledger WHERE id = ?",
        (entry_id,)).fetchone()
    db.close()
    return r


# ── _message_inbox_state ───────────────────────────────────────────────

def test_live_id_in_inbox_defers(monkeypatch):
    _use(monkeypatch, FakeGraph(messages={"M1": INBOX_ID}))
    state, live, note = ledger_sync._message_inbox_state("M1", INBOX_ID)
    assert (state, live, note) == (ledger_sync.IN_INBOX, "M1", "")


def test_live_id_outside_inbox_closes(monkeypatch):
    _use(monkeypatch, FakeGraph(messages={"M1": ARCHIVE_ID}))
    state, _, _ = ledger_sync._message_inbox_state("M1", INBOX_ID)
    assert state == ledger_sync.ARCHIVED


def test_stale_id_with_no_conversation_is_unknown(monkeypatch):
    """A bare 404 must NOT read as 'archived'."""
    _use(monkeypatch, FakeGraph(messages={}))
    state, live, note = ledger_sync._message_inbox_state("DEAD", INBOX_ID)
    assert state == ledger_sync.UNKNOWN, (state, note)
    assert live is None
    assert "no conversation_id" in note


def test_stale_id_reresolves_into_inbox_and_offers_heal(monkeypatch):
    """The snooze/wake shape. wake-snoozed put the mail back in the inbox
    under a THIRD id; the ledger still holds the pre-snooze one."""
    _use(monkeypatch, FakeGraph(
        messages={},
        conversations={"CONV": [_msg("NEW-ID", INBOX_ID)]}))
    state, live, _ = ledger_sync._message_inbox_state("OLD-ID", INBOX_ID, "CONV")
    assert state == ledger_sync.IN_INBOX
    assert live == "NEW-ID"


def test_stale_id_reresolves_outside_inbox_closes(monkeypatch):
    """A genuine user archive still closes: the id 404s, but the thread
    re-resolves into the Archive folder."""
    _use(monkeypatch, FakeGraph(
        messages={},
        conversations={"CONV": [_msg("NEW-ID", ARCHIVE_ID)]}))
    state, live, _ = ledger_sync._message_inbox_state("OLD-ID", INBOX_ID, "CONV")
    assert state == ledger_sync.ARCHIVED
    assert live == "NEW-ID"


def test_any_inbox_message_in_thread_defers(monkeypatch):
    """One archived message plus a newer inbound still in the inbox means
    the thread is live, so the row is not closed."""
    _use(monkeypatch, FakeGraph(
        messages={},
        conversations={"CONV": [
            _msg("OLDER", ARCHIVE_ID, "2026-08-19T10:00:00Z"),
            _msg("NEWER", INBOX_ID, "2026-08-21T10:00:00Z"),
        ]}))
    state, live, _ = ledger_sync._message_inbox_state("OLD-ID", INBOX_ID, "CONV")
    assert (state, live) == (ledger_sync.IN_INBOX, "NEWER")


def test_drafts_do_not_count_as_the_thread(monkeypatch):
    """A staged draft sitting in the thread must not resolve the question."""
    _use(monkeypatch, FakeGraph(
        messages={},
        conversations={"CONV": [_msg("D1", INBOX_ID, is_draft=True)]}))
    state, _, note = ledger_sync._message_inbox_state("OLD", INBOX_ID, "CONV")
    assert state == ledger_sync.UNKNOWN, note


def test_conversation_query_failure_is_unknown(monkeypatch):
    _use(monkeypatch, FakeGraph(messages={}, conversation_raises=True))
    state, _, note = ledger_sync._message_inbox_state("OLD", INBOX_ID, "CONV")
    assert state == ledger_sync.UNKNOWN
    assert "could not be re-resolved" in note


def test_non_404_http_error_is_unknown(monkeypatch):
    class Boom(FakeGraph):
        def graph_get(self, endpoint, params=None):
            if endpoint.startswith("/me/messages/"):
                raise _http_error(503)
            return super().graph_get(endpoint, params)

    _use(monkeypatch, Boom())
    state, _, note = ledger_sync._message_inbox_state("M", INBOX_ID, "CONV")
    assert state == ledger_sync.UNKNOWN
    assert "503" in note


def test_quote_in_conversation_id_is_refused(monkeypatch):
    """An id that cannot be safely quoted into an OData filter yields
    UNKNOWN rather than a malformed query."""
    _use(monkeypatch, FakeGraph(messages={}))
    state, _, _ = ledger_sync._message_inbox_state("OLD", INBOX_ID, "CO'NV")
    assert state == ledger_sync.UNKNOWN


def test_boolean_wrapper_maps_unknown_to_none(monkeypatch):
    _use(monkeypatch, FakeGraph(messages={}))
    assert ledger_sync._check_message_in_inbox("DEAD", INBOX_ID) is None


# ── cmd_check end to end ───────────────────────────────────────────────

def _run_check(capsys) -> dict:
    ledger_sync.cmd_check(None)
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_check_closes_a_genuine_archive(monkeypatch, capsys):
    _add_row("response-archived", msg_id="M-ARCH", conv_id="CONV-A")
    _use(monkeypatch, FakeGraph(messages={"M-ARCH": ARCHIVE_ID}))
    out = _run_check(capsys)
    assert out["closed"] == 1, out
    assert _row("response-archived")[0] == "completed"


class ThrottledGraph(FakeGraph):
    """Graph answering 429 to the inbox folder read ("raise"), or answering
    every read while counting a 429 one of them met ("swallow")."""

    def __init__(self, mode="raise", **kw):
        super().__init__(**kw)
        self.mode = mode
        self.count = 0

    def graph_get(self, endpoint, params=None):
        if self.mode == "raise" and endpoint == "/me/mailFolders/inbox":
            self.calls.append(endpoint)
            raise _http_error(429)
        if self.mode == "swallow" and endpoint.startswith("/me/messages/"):
            self.count += 1
        return super().graph_get(endpoint, params)

    def throttled_calls(self):
        return self.count


def test_a_throttled_check_skips_the_next_three(monkeypatch, capsys):
    _add_row("response-a", msg_id="M-A", conv_id="CONV-A")
    g = ThrottledGraph(messages={"M-A": INBOX_ID})
    _use(monkeypatch, g)
    out = _run_check(capsys)
    assert "Could not get inbox folder" in out["error"] and "next 3 checks" in out["backoff"]
    for left in (2, 1, 0):
        out = _run_check(capsys)
        assert out["skipped"] is True and f"({left} more to skip)" in out["reason"], out
    assert g.calls == ["/me/mailFolders/inbox"]     # the skipped checks asked Graph nothing
    g.mode = "ok"
    out = _run_check(capsys)
    assert out["checked"] == 1 and "backoff" not in out, out


def test_a_429_a_row_read_swallowed_starts_the_backoff(monkeypatch, capsys):
    _add_row("response-a", msg_id="M-A", conv_id="CONV-A")
    _use(monkeypatch, ThrottledGraph("swallow", messages={"M-A": INBOX_ID}))
    out = _run_check(capsys)
    assert out["checked"] == 1 and "HTTP 429" in out["backoff"], out
    assert retry_policy.TickBackoff(ledger_sync.BACKOFF_JOB).pending() == 3


def test_check_does_not_close_a_woken_snooze(monkeypatch, capsys):
    """End-to-end version of the `response-syn-qv01` scenario: the row
    is past its snooze, the mail is back in the inbox, and the ledger holds
    the pre-snooze id."""
    _add_row("e-woken", msg_id="PRE-SNOOZE-ID", conv_id="CONV-W",
             status="snoozed", snooze_until="2020-01-01T00:00:00Z")
    _use(monkeypatch, FakeGraph(
        messages={},
        conversations={"CONV-W": [_msg("POST-WAKE-ID", INBOX_ID)]}))
    out = _run_check(capsys)
    assert out["closed"] == 0, out
    status, msg_id, _ = _row("e-woken")
    assert status == "snoozed"
    assert msg_id == "POST-WAKE-ID", "stale id should be healed in place"
    assert out.get("healed_message_ids") == 1, out


def test_check_reports_unresolvable_rows_instead_of_closing(monkeypatch, capsys):
    _add_row("e-orphan", msg_id="DEAD-ID")          # no conversation_id
    _use(monkeypatch, FakeGraph(messages={}))
    out = _run_check(capsys)
    assert out["closed"] == 0, out
    assert out["unresolved"] == 1, out
    assert out["unresolved_items"][0]["id"] == "e-orphan"
    assert _row("e-orphan")[0] == "open"


def test_check_skips_rows_parked_for_the_future(monkeypatch, capsys):
    """Belt and braces with test_ledger_sync_snooze: a parked row must not
    even be handed to Graph, let alone closed."""
    _add_row("e-parked", msg_id="M-GONE", conv_id="CONV-P",
             status="follow_up", follow_up_after="2099-01-01")
    graph = FakeGraph(messages={})
    _use(monkeypatch, graph)
    out = _run_check(capsys)
    assert out == {"checked": 0, "closed": 0}, out
    assert graph.calls == [], graph.calls
    assert _row("e-parked")[0] == "follow_up"


def test_check_co_closes_siblings_on_a_real_archive(monkeypatch, capsys):
    """The co-close sweep survives the rework."""
    _add_row("response-primary", msg_id="M-ARCH", conv_id="CONV-S")
    _add_row("thread-sibling", msg_id="M-OTHER", conv_id="CONV-S")
    _use(monkeypatch, FakeGraph(messages={"M-ARCH": ARCHIVE_ID,
                                          "M-OTHER": ARCHIVE_ID}))
    out = _run_check(capsys)
    assert _row("response-primary")[0] == "completed"
    assert _row("thread-sibling")[0] == "completed"
    assert out["closed"] >= 2, out


# ── Plugin isolation on the sibling sweep ──────────────────────────────
# A sibling-sweep query on conversation_id alone, with no source
# predicate, lets a main-EA close complete plugin rows and a plugin close
# sweep main-EA rows. docs/plugin-architecture.md and ledger_sync's own
# candidate SELECT both state the contract: each plugin owns its own
# lifecycle.

def _status(entry_id):
    return _row(entry_id)[0]


def test_check_does_not_co_close_a_plugin_sibling(monkeypatch, capsys):
    _add_row("response-primary", msg_id="M-ARCH", conv_id="CONV-X")
    _add_row("example-notes-note-0780", msg_id="M-PLUGIN",
             conv_id="CONV-X", source="plugin:example-notes")
    _use(monkeypatch, FakeGraph(messages={"M-ARCH": ARCHIVE_ID,
                                          "M-PLUGIN": ARCHIVE_ID}))
    out = _run_check(capsys)
    assert _status("response-primary") == "completed"
    assert _status("example-notes-note-0780") == "open", (
        "a user archive on the main-EA row must not close the plugin's")
    assert out["closed"] == 1, out


def test_ledger_close_does_not_co_close_across_the_source_boundary():
    """The other sweep, in ledger.py cmd_close."""
    import types as _types
    _add_row("response-syn-oa501", msg_id="", conv_id="CONV-Y")
    _add_row("example-notes-note-0785", msg_id="",
             conv_id="CONV-Y", source="plugin:example-notes")
    ledger_mod.cmd_close(_types.SimpleNamespace(user_said="done", 
        id="response-syn-oa501", note="Notes sent today",
        skip_archive=True))
    assert _status("response-syn-oa501") == "completed"
    assert _status("example-notes-note-0785") == "open"


def test_a_plugin_close_sweeps_only_its_own_siblings():
    import types as _types
    _add_row("plugin-a", msg_id="", conv_id="CONV-Z",
             source="plugin:example-notes")
    _add_row("plugin-b", msg_id="", conv_id="CONV-Z",
             source="plugin:example-notes")
    _add_row("main-ea-row", msg_id="", conv_id="CONV-Z")
    ledger_mod.cmd_close(_types.SimpleNamespace(user_said="done", 
        id="plugin-a", note="done", skip_archive=True))
    assert _status("plugin-b") == "completed", "its own sibling still closes"
    assert _status("main-ea-row") == "open"


def test_closed_conversation_times_is_source_scoped():
    """closure_scrubber's closed-sibling signal reads this map; an
    unscoped map let a plugin closure sweep main-EA rows."""
    import types as _types
    _add_row("plugin-only", msg_id="", conv_id="CONV-P1",
             source="plugin:example-notes")
    ledger_mod.cmd_close(_types.SimpleNamespace(user_said="done", 
        id="plugin-only", note="done", skip_archive=True))
    main = ledger_mod.get_closed_conversation_times(db_path=ledger_mod.DB_PATH)
    assert "CONV-P1" not in main, main
    everything = ledger_mod.get_closed_conversation_times(
        db_path=ledger_mod.DB_PATH, include_plugins=True)
    assert "CONV-P1" in everything
    just_that_plugin = ledger_mod.get_closed_conversation_times(
        db_path=ledger_mod.DB_PATH, source="plugin:example-notes")
    assert "CONV-P1" in just_that_plugin


def test_an_archive_closes_one_row_and_the_policy_sweep_keeps_the_source():
    """Each row is judged by its own mail: the sync has no sibling sweep and
    settles with sweep=False. The policy's own sweep (a user's word, a reply)
    stays inside the closer's source."""
    src = Path(ledger_sync.__file__).read_text()
    assert "co-closed with" not in src and "UPDATE ledger SET status" not in src
    assert 'signal="user_archived"' in src and "sweep=False" in src
    import closure
    sweep = Path(closure.__file__).read_text()
    sweep = sweep[sweep.index("def _sweep_siblings"):]
    assert 'pred, args = ("AND source = ?", (source,)) if source else ("AND source IS NULL", ())' in sweep


def test_candidate_sql_is_the_one_cmd_check_uses():
    """The test calls the real SELECT rather than a copy of it."""
    src = (Path(ledger_sync.__file__)).read_text()
    assert src.count("SELECT id, description, source_message_id") == 1
    assert "candidate_rows(db)" in src
    assert "follow_up_after" in ledger_sync.CANDIDATE_WHERE


def test_no_test_writes_reach_the_deployed_state_dir(monkeypatch):
    """No test write may reach the deployed state directory."""
    import os
    ea = Path(os.environ["EA_DIR"])
    assert "ea-bot" not in str(ea), ea
    assert str(ledger_mod.DB_PATH) != str(
        Path.home() / ".ea" / "state" / "obligations-ledger.db")


# ── an archive that predates the row ───────────────────────────────────

class TimedGraph(FakeGraph):
    """FakeGraph whose messages carry lastModifiedDateTime, which Outlook
    changes when a message moves out of the inbox."""

    def __init__(self, messages, modified, conversations=None):
        super().__init__(messages=messages, conversations=conversations)
        self.modified = dict(modified)

    def graph_get(self, endpoint, params=None):
        res = super().graph_get(endpoint, params)
        if endpoint.startswith("/me/messages/"):
            mid = endpoint.split("/me/messages/", 1)[1]
            res = dict(res, lastModifiedDateTime=self.modified.get(mid))
        return res


def _metadata_of(entry_id):
    db = ledger_mod._get_db()
    r = db.execute("SELECT metadata FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    db.close()
    return json.loads(r[0] or "{}")


def test_mail_archived_before_the_row_existed_does_not_close_it(monkeypatch, capsys):
    """A detector row whose thread was archived before the row existed:
    that archive is not the user's answer to it."""
    _add_row("response-old-thread", msg_id="M-OLD", conv_id="CONV-OLD")
    _use(monkeypatch, TimedGraph(
        messages={"M-OLD": ARCHIVE_ID}, modified={"M-OLD": "2026-01-02T13:36:22Z"},
        conversations={"CONV-OLD": [dict(_msg("M-OLD", ARCHIVE_ID),
                                         lastModifiedDateTime="2026-01-02T13:36:22Z")]}))
    out = _run_check(capsys)
    assert out["closed"] == 0 and out["archived_before_row"] == ["response-old-thread"], out
    assert _row("response-old-thread")[0] == "open"
    assert _metadata_of("response-old-thread")["archived_before_row"] == "2026-01-02T13:36:22Z"
    # a later mailbox-wide touch of the thread does not turn it into a close
    _use(monkeypatch, TimedGraph(messages={"M-OLD": ARCHIVE_ID},
                                 modified={"M-OLD": "2099-01-01T00:00:00Z"}))
    assert _run_check(capsys)["closed"] == 0
    # the thread back in the inbox (a new reply) makes a later archive count
    _use(monkeypatch, TimedGraph(messages={"M-OLD": INBOX_ID}, modified={}))
    _run_check(capsys)
    _use(monkeypatch, TimedGraph(messages={"M-OLD": ARCHIVE_ID},
                                 modified={"M-OLD": "2099-01-01T00:00:00Z"}))
    assert _run_check(capsys)["closed"] == 1


def test_mail_archived_after_the_row_was_made_closes_it(monkeypatch, capsys):
    _add_row("response-fresh", msg_id="M-NEW")
    _use(monkeypatch, TimedGraph(messages={"M-NEW": ARCHIVE_ID},
                                 modified={"M-NEW": "2099-01-01T00:00:00Z"}))
    assert _run_check(capsys)["closed"] == 1
    assert _row("response-fresh")[0] == "completed"


def test_mail_seen_in_the_inbox_then_archived_closes_whatever_its_timestamp(monkeypatch, capsys):
    """Once the sync has seen the mail in the inbox for this row, a later
    archive is the user's answer, even if Outlook kept an old timestamp."""
    _add_row("response-seen", msg_id="M-SEEN")
    _use(monkeypatch, TimedGraph(messages={"M-SEEN": INBOX_ID},
                                 modified={"M-SEEN": "2026-01-02T00:00:00Z"}))
    assert _run_check(capsys)["closed"] == 0
    assert _metadata_of("response-seen").get("inbox_seen") is True
    _use(monkeypatch, TimedGraph(messages={"M-SEEN": ARCHIVE_ID},
                                 modified={"M-SEEN": "2026-01-02T00:00:00Z"}))
    assert _run_check(capsys)["closed"] == 1


def test_a_row_reopened_after_an_archive_close_is_not_closed_by_an_archive_again(monkeypatch, capsys):
    _add_row("priority-reopened", msg_id="M-R")
    db = ledger_mod._get_db()
    db.execute("UPDATE ledger SET metadata = ? WHERE id = 'priority-reopened'",
               (json.dumps({"archive_not_evidence": "2026-09-28: reopened"}),))
    db.commit()
    db.close()
    _use(monkeypatch, TimedGraph(messages={"M-R": ARCHIVE_ID},
                                 modified={"M-R": "2099-01-01T00:00:00Z"}))
    out = _run_check(capsys)
    assert out["closed"] == 0 and out["archived_before_row"] == ["priority-reopened"]


# ── rows the EA tracks ─────────────────────────────────────────────────

def test_an_archive_never_closes_a_row_the_ea_tracks(monkeypatch, capsys):
    """A task the EA made (from a call, a conversation) is linked to its
    thread as evidence; archiving that mail after handing it over is not
    doing the work."""
    _add_row("field-trial-budget-quadchart", msg_id="M-T", conv_id="CONV-T")
    _use(monkeypatch, TimedGraph(messages={"M-T": ARCHIVE_ID},
                                 modified={"M-T": "2099-01-01T00:00:00Z"}))
    out = _run_check(capsys)
    assert out["closed"] == 0 and out["held_ea_rows"] == 1, out
    assert out["held_ea_ids"] == ["field-trial-budget-quadchart"]
    assert _row("field-trial-budget-quadchart")[0] == "open"


def test_a_detector_archive_does_not_co_close_a_tracked_task_on_its_thread(monkeypatch, capsys):
    _add_row("response-ask", msg_id="M-ASK", conv_id="CONV-B")
    _add_row("thread-sib", msg_id="M-SIB", conv_id="CONV-B")
    _add_row("send-avery-outline", msg_id="", conv_id="CONV-B")
    _use(monkeypatch, FakeGraph(messages={"M-ASK": ARCHIVE_ID, "M-SIB": ARCHIVE_ID}))
    _run_check(capsys)
    assert _status("response-ask") == "completed" and _status("thread-sib") == "completed"
    assert _status("send-avery-outline") == "open"


def test_archive_closes_only_detector_prefixes():
    assert all(ledger_sync.archive_closes(i) for i in
               ("response-AAq=", "priority-AAq=", "sched-AAq=", "thread-AAq="))
    assert not any(ledger_sync.archive_closes(i) for i in
                   ("survey-paper-outline-0928", "needs-response-x", "reopen-colleague-coffee"))
