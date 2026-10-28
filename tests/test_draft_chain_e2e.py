#!/usr/bin/env python3
"""E2E regression harness for the draft-confirmation chain.

The chain is Claude output -> draft detection -> confirmation button ->
token gate -> send. Isolated tests cover each stage; this suite drives
the stages against recorded output shapes so their interactions stay
pinned:

  Stage 1  retired. The stdout-regex detector it covered is gone; the
           journal is the only draft-detection path left. Stage
           numbering is unchanged so the labels below still line up.
  Stage 2  draft-events journal          (deleted-draft subtraction plus
                                         run attribution and the claim
                                         cursor)
  Stage 3  _claim_event_actions          (staged-action claim protocol:
                                         atomic pickup, age gate,
                                         routing)
  Stage 4  _reject_if_stale_confirmation  (48h click-window guard)
  Stage 5  token gate lifecycle         (mint -> consume-once -> expire,
                                         send-draft blocked w/o token)

Bot helpers are exec'd out of ea-chat-bot.py by source markers (the
scheduler-trace test pattern), because importing the module pulls
slack_bolt and the isolation gate.
"""

import io
import json
import re
import sys
import tempfile
import time
import traceback
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# The journal and the capture store go through resident_io, which runs a
# resident child in an owner process; a sandboxed EA_DIR makes this a
# resident process, so the writes land directly in the test's temp paths.
import os as _os_env  # noqa: E402
_os_env.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-draftchain-")
import atomic_io as _atomic  # noqa: E402

import chat as _chat  # noqa: E402

BOT_SRC = (HERE.parent / "ea-chat-bot.py").read_text()

# These tests rewire the shared graph_actions, draft_events and
# draft_capture modules in place (a fake graph client, temp paths). A
# script run is one process per file; under pytest the next file would
# inherit the fakes, so every rewired attribute is put back after each test.
_REWIRED = {
    "graph_actions": ("graph", "_user_addresses", "_draft_events", "_draft_capture",
                      "PENDING_CONFIRMS_FILE", "PENDING_CONFIRMS_LOCK"),
    "draft_events": ("JOURNAL",),
    "draft_capture": ("STORE",),
}
try:
    import pytest
except ImportError:
    pytest = None
if pytest is not None:
    @pytest.fixture(autouse=True)
    def _restore_rewired_modules():
        import importlib
        saved = []
        for name, attrs in _REWIRED.items():
            mod = importlib.import_module(name)
            saved += [(mod, a, getattr(mod, a)) for a in attrs if hasattr(mod, a)]
        yield
        for mod, a, value in saved:
            setattr(mod, a, value)


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Stage 2: draft-events journal. The bot reads structured records
# written by graph_actions itself, not stdout or step results.
# ---------------------------------------------------------------------------

import draft_events as de  # noqa: E402


def _journal(tmp: Path):
    de.JOURNAL = tmp / "draft-events.jsonl"
    return de


def test_s2_created_draft_journaled_and_read():
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        off = j.offset()
        j.record("created", "AAMkKEEP=", verb="create-reply")
        net = j.effective_drafts(j.read_since(off))
        assert net["created"] == {"AAMkKEEP="}
        assert not net["deleted"] and not net["updated"]


def test_s2_deleted_draft_nets_out():
    # The 170d755 rule, now structural: created-then-deleted (rewrite
    # flow) must not surface for confirmation; the replacement must.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        off = j.offset()
        j.record("created", "AAMkDELETED=", verb="create-reply")
        j.record("deleted", "AAMkDELETED=", verb="delete-draft")
        j.record("created", "AAMkFINAL=", verb="create-reply")
        net = j.effective_drafts(j.read_since(off))
        assert net["created"] == {"AAMkFINAL="}
        assert net["deleted"] == {"AAMkDELETED="}


def test_s2_update_is_reported_separately_from_created():
    # "update the draft" must NOT produce a SECOND confirmation while the
    # first block's buttons are still live: the draft id is unchanged, so
    # the standing Send button already points at the revised body. The
    # journal's job is only to keep `updated` distinct from `created`;
    # whether a block gets reposted is _reconfirm_updated_drafts' call,
    # pinned below.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        off = j.offset()
        j.record("updated", "AAMkEXISTING=", verb="update-draft")
        net = j.effective_drafts(j.read_since(off))
        assert net["created"] == set()
        assert net["updated"] == {"AAMkEXISTING="}


def _reconfirm_helpers(tmp: Path):
    """Exec the draft-confirmation registry + _reconfirm_updated_drafts out
    of the bot source, with Slack and the clock stubbed."""
    posted = []
    logged = []
    ns = {
        "json": json, "os": __import__("os"), "time": time, "_atomic": _atomic,
        "threading": __import__("threading"),
        "datetime": datetime, "timezone": timezone,
        "EA_DIR": tmp,
        "_private_state": lambda name: tmp / "state" / name,
        "MAX_CONFIRM_AGE_HOURS": 48,
        "log": types.SimpleNamespace(
            info=lambda m, *a, **k: logged.append(m),
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None),
        "_post_send_confirmation":
            lambda ch, tts, did, **kw: posted.append((ch, tts, did)),
        # Only when the revised draft cannot be read is the live block
        # re-rendered in place instead of replaced.
        "_refresh_send_confirmation":
            lambda did: posted.append(("refreshed", did)) is None,
        "_build_send_confirmation":
            lambda ch, tts, did, *a, **kw: (None, "unreadable") if "BAD" in did
            else (object(), None),
        # The repost routes by mail system from the journal records.
        "_draft_events": de,
        "_GMAIL_ACCOUNT": "kit.kowalczyk1@example.com",
    }
    _exec_block("# Draft-confirmation registry",
                "def _replace_buttons_with_status", ns)
    return ns, posted, logged


def _with_retire_stub(ns, posted):
    def retire(channel, ts, text):
        posted.append(("retired", channel, ts, text))
        ns["_retire_confirmation_message"](channel, ts)
    ns["_replace_buttons_with_status"] = retire

    def post(ch, tts, did, **kw):
        posted.append((ch, tts, did, kw.get("what")))
        ns["_note_draft_confirmation"](did, "DM1", "222.2", ch, tts)
    ns["_post_send_confirmation"] = post


def test_s2_a_revision_retires_the_live_block_and_posts_a_new_one():
    # One live :lock: block per draft: the revised draft's block is new, at
    # the bottom of the DM, and the old one says where it went.
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        _with_retire_stub(ns, posted)
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test")
        assert posted[0][:3] == ("retired", "DM1", "111.1") and "new Send card" in posted[0][3]
        assert posted[1] == ("C1", "T1", "AAMkD=", "The new Send card for the revised draft")
        assert len(posted) == 2
        entry = ns["_load_draft_confirms"]()["AAMkD="]
        assert entry["status"] == "live" and entry["message_ts"] == "222.2"


def test_s2_an_unreadable_revision_keeps_the_live_block():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        _with_retire_stub(ns, posted)
        ns["_note_draft_confirmation"]("AAMkBAD=", "DM1", "111.1", "C1", "T1")
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkBAD="}, "test")
        assert posted == [("refreshed", "AAMkBAD=")]
        assert ns["_load_draft_confirms"]()["AAMkBAD="]["message_ts"] == "111.1"


def test_s2_update_after_cancel_reposts_the_block():
    # Cancel replaces the buttons with a status line, so a revised draft
    # needs a fresh block or it has no send path at all.
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_retire_confirmation_message"]("DM1", "111.1")   # what Cancel does
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test")
        assert posted == [("C1", "T1", "AAMkD=")]


def test_s2_update_after_send_never_reposts():
    # The draft already went out; a resurrected Send button would send it
    # twice.
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_mark_draft_confirmation"]("AAMkD=", "consumed")
        ns["_retire_confirmation_message"]("DM1", "111.1")
        assert ns["_draft_confirmation_state"]("AAMkD=") == "consumed"
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test")
        assert posted == []


def _journal_ts(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc
                                  ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_s2_a_revision_that_predates_the_cancel_does_not_repost():
    # The em-dash rewrite's own update-draft record, left unclaimed by the
    # run that made it and swept up by a later dispatch AFTER the user had
    # cancelled, must not resurrect a Send button.
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        stale = [{"action": "updated", "draft_id": "AAMkD=",
                  "ts": _journal_ts(-120)}]
        ns["_retire_confirmation_message"]("DM1", "111.1")   # Cancel, now
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test",
                                        events=stale)
        assert posted == []


def test_s2_a_revision_after_the_cancel_still_reposts():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        ns["_retire_confirmation_message"]("DM1", "111.1")
        fresh = [{"action": "updated", "draft_id": "AAMkD=",
                  "ts": _journal_ts(120)}]
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test",
                                        events=fresh)
        assert posted == [("C1", "T1", "AAMkD=")]


def test_s2_update_of_an_unknown_draft_gets_a_block():
    # reused_existing_draft on a thread's FIRST request: nothing ever
    # posted a block for that id, so the user has no way to send it.
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _reconfirm_helpers(Path(td))
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkNEVERSEEN="}, "test")
        assert posted == [("C1", "T1", "AAMkNEVERSEEN=")]


def test_s2_stale_block_counts_as_gone():
    # A block past the 48h click window refuses the click, so it is not a
    # send path either.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, posted, _ = _reconfirm_helpers(tmp)
        ns["_note_draft_confirmation"]("AAMkD=", "DM1", "111.1", "C1", "T1")
        store = tmp / "state" / "draft-confirmations.json"
        data = json.loads(store.read_text())
        data["AAMkD="]["posted_at"] = time.time() - 72 * 3600
        store.write_text(json.dumps(data))
        assert ns["_draft_confirmation_state"]("AAMkD=") == "retired"
        ns["_reconfirm_updated_drafts"]("C1", "T1", {"AAMkD="}, "test")
        assert posted == [("C1", "T1", "AAMkD=")]


# --- run attribution + claim cursor ---------------------------------------

def test_s2_run_id_stamped_from_env():
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        _os.environ["EA_RUN_ID"] = "run-A"
        try:
            j.record("created", "AAMkA=", verb="create-reply")
        finally:
            _os.environ.pop("EA_RUN_ID", None)
        j.record("created", "AAMkB=", verb="create-reply")
        evs = {e["draft_id"]: e.get("run_id") for e in j.read_since(0)}
        assert evs == {"AAMkA=": "run-A", "AAMkB=": ""}


def test_s2_effective_drafts_drops_another_runs_labelled_draft():
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        events = [
            {"action": "created", "draft_id": "AAMkMINE=", "run_id": "A"},
            {"action": "created", "draft_id": "AAMkTHEIRS=", "run_id": "B"},
            {"action": "created", "draft_id": "AAMkUNLABELLED="},
        ]
        net = j.effective_drafts(events, run_id="A")
        assert net["created"] == {"AAMkMINE=", "AAMkUNLABELLED="}
        strict = j.effective_drafts(events, run_id="A", accept_unlabeled=False)
        assert strict["created"] == {"AAMkMINE="}
        # No run id given = legacy behaviour, everything passes.
        assert j.effective_drafts(events)["created"] == {
            "AAMkMINE=", "AAMkTHEIRS=", "AAMkUNLABELLED="}


def test_s2_claim_cursor_splits_unlabelled_records_between_runs():
    # The interleaving: run A snapshots O1, run B snapshots O2 > O1, B
    # creates Db and finishes first, then A creates Da and finishes.
    # Without the cursor, A's read_since(O1) returns BOTH and posts a live
    # Send button for B's draft in A's thread.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        o1 = j.offset()
        j.record("created", "AAMkNOISE=", verb="create-reply")
        o2 = j.offset()
        j.record("created", "AAMkDb=", verb="create-reply")
        b_net = j.effective_drafts(j.claim_since(o2))
        assert b_net["created"] == {"AAMkDb="}
        j.record("created", "AAMkDa=", verb="create-reply")
        a_net = j.effective_drafts(j.claim_since(o1))
        assert a_net["created"] == {"AAMkDa="}, (
            "run A must not re-claim the record run B already took")


def test_s2_claim_returns_own_labelled_records_even_past_the_cursor():
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        off = j.offset()
        _os.environ["EA_RUN_ID"] = "run-A"
        try:
            j.record("created", "AAMkMINE=", verb="create-reply")
        finally:
            _os.environ.pop("EA_RUN_ID", None)
        # Another consumer sweeps the cursor past it.
        j.claim_since(j.offset())
        mine = j.effective_drafts(j.claim_since(off, "run-A"))
        assert mine["created"] == {"AAMkMINE="}


def test_s2_a_labelled_run_never_takes_an_unlabelled_draft():
    """2026-10-02: the resident (unlabelled records) drafted the
    AutonomouStuff ping while a per-thread lane (labelled) ran on another
    topic; the lane finished first and posted that draft's Send block in
    its own topic. A lane with accept_unlabelled=False takes only its own
    records and leaves the cursor, so the resident still claims its draft."""
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        resident_off = j.offset()
        lane_off = j.offset()
        j.record("created", "AAMkRESIDENT=", verb="create-reply")       # unlabelled
        _os.environ["EA_RUN_ID"] = "lane-1"
        try:
            j.record("created", "AAMkLANE=", verb="create-reply")
        finally:
            _os.environ.pop("EA_RUN_ID", None)
        lane = j.effective_drafts(j.claim_since(lane_off, "lane-1", accept_unlabelled=False))
        assert lane["created"] == {"AAMkLANE="}
        resident = j.effective_drafts(j.claim_since(resident_off, "res-run"))
        assert resident["created"] == {"AAMkRESIDENT="}
        assert j.claim_since(lane_off, "", accept_unlabelled=False) == []


def test_s2_prune_parks_the_cursor_at_eof_not_at_zero():
    # A prune shifts every offset. Re-opening the surviving half for
    # claiming would post a live Send button for every stale draft in it.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        j.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        for i in range(40):
            j.record("created", f"AAMkOLD{i}=", verb="create-draft")
        j._prune()
        assert j._read_cursor() == j.offset()
        assert j.claim_since(0) == [], (
            "a pruned journal must not re-offer its history for claiming")
        j.record("created", "AAMkNEW=", verb="create-draft")
        assert [e["draft_id"] for e in j.claim_since(0)] == ["AAMkNEW="]


def test_s2_claim_never_hands_a_labelled_record_to_another_run():
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        off = j.offset()
        _os.environ["EA_RUN_ID"] = "run-A"
        try:
            j.record("created", "AAMkMINE=", verb="create-reply")
        finally:
            _os.environ.pop("EA_RUN_ID", None)
        assert j.effective_drafts(j.claim_since(off, "run-B"))["created"] == set()


def test_s2_offset_isolates_runs():
    # Events from a PRIOR run must not leak into this run's detection.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        j.record("created", "AAMkPRIOR=", verb="create-draft")
        off = j.offset()  # snapshot AFTER the prior run
        j.record("created", "AAMkTHISRUN=", verb="create-reply")
        net = j.effective_drafts(j.read_since(off))
        assert net["created"] == {"AAMkTHISRUN="}


def test_s2_garbage_lines_skipped():
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        j.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        j.JOURNAL.write_text("not json\n")
        off = 0
        j.record("created", "AAMkOK=", verb="create-draft")
        net = j.effective_drafts(j.read_since(off))
        assert net["created"] == {"AAMkOK="}


def test_s2_shrunk_file_rereads_from_start():
    # Journal pruned mid-run (offset beyond EOF) -> read from 0 rather
    # than silently missing a created draft.
    with tempfile.TemporaryDirectory() as td:
        j = _journal(Path(td))
        j.record("created", "AAMkA=", verb="create-draft")
        big_offset = j.offset() + 10_000
        j.record("created", "AAMkB=", verb="create-draft")
        events = j.read_since(big_offset)
        ids = {e["draft_id"] for e in events}
        assert {"AAMkA=", "AAMkB="} <= ids


# ---------------------------------------------------------------------------
# Stage 3: staged-action claim protocol (pending-*.json pickup)
# ---------------------------------------------------------------------------
# The pickup is an atomic rename (one owner per staged write), age-gated
# for the out-of-band sweeper, and it carries optional thread routing.

import os as _os                                            # noqa: E402
import uuid as _uuid                                        # noqa: E402


def _claim_ns(tmp: Path):
    # Claims wait in the owner-private dir (here a sandbox beside state/).
    ns = {"re": re, "json": json, "os": _os, "time": time, "uuid": _uuid, "_atomic": _atomic,
          "Path": Path, "EA_DIR": tmp,
          "_chat": types.SimpleNamespace(private_dir=lambda: tmp / "private"),
          "log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                       info=lambda *a, **k: None,
                                       error=lambda *a, **k: None)}
    _exec_block("# === staged-action claim protocol",
                "def _handle_context_export", ns)
    return ns


def _claimer(tmp: Path):
    return _claim_ns(tmp)["_claim_event_actions"]


def _stage(tmp: Path, name: str, payload):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    f = tmp / "state" / name
    f.write_text(json.dumps(payload))
    return f


def test_s3_pending_event_picked_up_and_removed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-event.json",
                   {"subject": "Hold: focus", "start": "X",
                    "end": "Y", "attendees": ""})
        actions = _claimer(tmp)()
        assert len(actions) == 1
        assert actions[0]["action"] == "event_create"
        assert actions[0]["subject"] == "Hold: focus"
        assert not f.exists(), "pending file must be consumed"


def test_s3_pending_channel_is_a_staged_action():
    """A room request for an existing project rides the claim protocol:
    the file is consumed, the action names the project and keeps the
    thread it was asked in."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-channel.json",
                   {"project": "lab-bench-rebuild", "thread_ts": "1.2"})
        actions = _claimer(tmp)()
        assert len(actions) == 1
        assert actions[0]["action"] == "open_channel"
        assert actions[0]["project"] == "lab-bench-rebuild"
        assert actions[0]["_route"] == {"thread_ts": "1.2"}
        assert not f.exists(), "pending file must be consumed"


def test_s3_update_and_delete_files():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-event-update.json",
               {"event_id": "EV1", "start": "S"})
        claim = _claimer(tmp)
        assert [a["action"] for a in claim()] == ["event_update"]
        _stage(tmp, "pending-event-delete.json", {"event_id": "EV2"})
        assert [a["action"] for a in claim()] == ["event_delete"]


def test_s3_all_staged_files_are_collected_in_one_pass():
    # A scan that returns on the FIRST file it finds leaves the second for
    # the racing poller, which posts it channel-level.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-event.json", {"subject": "New Thursday"})
        _stage(tmp, "pending-event-delete.json", {"event_id": "EV-TUE"})
        kinds = sorted(a["action"] for a in _claimer(tmp)())
        assert kinds == ["event_create", "event_delete"]


def test_s3_update_array_fans_out_instead_of_vanishing():
    # An array in the update file must fan out; read-and-unlink without a
    # fan-out drops every entry with no button and no log line.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-event-update.json",
               [{"event_id": "EV1"}, {"event_id": "EV2"}, {"event_id": "EV3"}])
        actions = _claimer(tmp)()
        assert [a["event_id"] for a in actions] == ["EV1", "EV2", "EV3"]
        assert all(a["action"] == "event_update" for a in actions)


def test_s3_a_plugin_cards_file_is_left_for_its_plugin():
    # A pending file no core action names belongs to a plugin card
    # (docs/plugins.md); with no plugin enabled the core leaves it alone.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-example-notes.json", [{"title": "Q3 plan"}])
        assert _claimer(tmp)() == []
        assert f.exists()


def test_s3_corrupt_pending_file_cleaned_up():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir(parents=True)
        f = tmp / "state" / "pending-event.json"
        f.write_text("{not json")
        assert _claimer(tmp)() == []
        assert not f.exists(), "corrupt pending file must still be removed"


def test_s3_multi_event_array():
    # A JSON ARRAY in pending-event.json yields event_create_multi with
    # all events: N buttons in one pass.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-event.json", [
            {"subject": "Summit Day 1", "start": "S1", "end": "E1"},
            {"subject": "Summit Day 2", "start": "S2", "end": "E2"},
            {"subject": "Summit Day 3", "start": "S3", "end": "E3"},
        ])
        claim = _claimer(tmp)
        actions = claim()
        assert len(actions) == 1
        assert actions[0]["action"] == "event_create_multi"
        assert [e["subject"] for e in actions[0]["events"]] == \
            ["Summit Day 1", "Summit Day 2", "Summit Day 3"]
        assert not f.exists()
        # empty array is not a confirmation request
        f.write_text("[]")
        assert claim() == []


def test_s3_no_files_returns_nothing():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir(parents=True)
        assert _claimer(tmp)() == []


def test_s3_claim_is_exclusive():
    # Two consumers reading the same file, one unlinking and the other's
    # unlink failing silently, post two live Create buttons. The rename
    # makes exactly one winner.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns = _claim_ns(tmp)
        _stage(tmp, "pending-event.json", {"subject": "Only once"})
        first = ns["_claim_staged_file"]("pending-event.json")
        second = ns["_claim_staged_file"]("pending-event.json")
        assert first is not None and second is None
        assert json.loads(first[0].read_text())["subject"] == "Only once"


def test_s3_claim_moves_the_file_out_of_the_staging_path():
    # Nothing may delete state/pending-event.json by NAME: the name can
    # already hold a newer, unread staging write.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns = _claim_ns(tmp)
        _stage(tmp, "pending-event.json", {"subject": "first"})
        claim_path, _ = ns["_claim_staged_file"]("pending-event.json")
        # A newer write lands at the canonical path while the first claim
        # is still being processed.
        _stage(tmp, "pending-event.json", {"subject": "second"})
        ns["_release_claim"](claim_path)
        survivor = tmp / "state" / "pending-event.json"
        assert survivor.exists(), "releasing a claim must not touch a new write"
        assert json.loads(survivor.read_text())["subject"] == "second"


def test_s3_sweeper_age_gate_leaves_a_fresh_file_alone():
    # The sweeper must not consume a file the live dispatch is still going
    # to claim, or the button posts at the channel root instead of in the
    # thread.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-event.json", {"subject": "mid-dispatch"})
        assert _claimer(tmp)(min_age_s=120) == []
        assert f.exists(), "a fresh staging file belongs to the live dispatch"
        _os.utime(f, (time.time() - 3600, time.time() - 3600))
        assert len(_claimer(tmp)(min_age_s=120)) == 1


def test_s3_dispatch_does_not_claim_a_file_staged_before_it_started():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-event.json", {"subject": "someone else's"})
        _os.utime(f, (time.time() - 600, time.time() - 600))
        assert _claimer(tmp)(newer_than=time.time() - 60) == []
        assert f.exists()
        assert len(_claimer(tmp)(newer_than=time.time() - 900)) == 1


def test_s3_abandoned_claim_is_recovered_not_lost():
    # A crash (or a bot restart) between the rename and the Slack post used
    # to lose the staged event outright; the claim file is now re-opened
    # once it is older than any dispatch window.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns = _claim_ns(tmp)
        _stage(tmp, "pending-event.json",
               {"subject": "Orphaned", "thread_ts": "T9"})
        claim_path, _ = ns["_claim_staged_file"]("pending-event.json")
        # Too fresh: an owner may still be mid-post.
        assert ns["_recover_abandoned_claims"](900) == []
        assert claim_path.exists()
        _os.utime(claim_path, (time.time() - 3600, time.time() - 3600))
        recovered = ns["_recover_abandoned_claims"](900)
        assert [a["subject"] for a in recovered] == ["Orphaned"]
        assert recovered[0]["_route"] == {"thread_ts": "T9"}, (
            "recovery must keep the payload's routing")
        assert not claim_path.exists()
        assert ns["_recover_abandoned_claims"](0) == [], "recover once only"


def test_s3_optional_thread_ts_routes_the_confirmation():
    # Backward-compatible schema extension: the payload MAY name the
    # conversation the buttons belong to.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-event.json",
               {"subject": "Committee", "thread_ts": "1712.5",
                "channel": "C-SCHED"})
        action = _claimer(tmp)()[0]
        assert action["_route"] == {"thread_ts": "1712.5",
                                    "channel": "C-SCHED"}


def test_s3_route_defaults_to_empty_for_the_documented_bare_payload():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-event.json", {"subject": "Bare"})
        assert _claimer(tmp)()[0]["_route"] == {}


def test_s3_routing_keys_do_not_leak_into_the_event():
    # thread_ts must not reach _post_event_confirmation as event content.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns = _claim_ns(tmp)
        seen = {}
        ns["_post_event_confirmation"] = lambda ch, tts, **kw: seen.update(
            {"channel": ch, "thread_ts": tts, **kw})
        ns["_infer_online"] = lambda ev: False
        ns["_repeat_fields"] = lambda ev: {}
        ns["_is_hold"] = lambda ev: False
        ns["_create_guestless_event"] = lambda ch, tts, ev: False
        _stage(tmp, "pending-event.json",
               {"subject": "Committee", "start": "S", "end": "E",
                "attendees": "a@b.c", "thread_ts": "1712.5"})
        action = ns["_claim_event_actions"]()[0]
        ns["_post_staged_action"]("C-DEFAULT", None, action)
        assert seen["thread_ts"] == "1712.5"
        assert seen["channel"] == "C-DEFAULT"
        assert seen["subject"] == "Committee"
        assert "thread_ts" not in seen.get("body", "")


# ---------------------------------------------------------------------------
# Stage 4: _reject_if_stale_confirmation (48h click window)
# ---------------------------------------------------------------------------

def _stale_guard(registry_dir: Path | None = None):
    """The guard as the bot runs it, against a card registry in a scratch
    dir and a frontend whose post time is the ref's own (a Slack ts)."""
    calls = []
    registry = _chat.CardRegistry(
        (registry_dir or Path(tempfile.mkdtemp())) / "chat-cards.json")
    ns = {"time": time, "_chat": _chat, "_CARDS": registry,
          "FRONTEND": types.SimpleNamespace(message_posted_at=lambda ref: ref.posted_at),
          "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                       warning=lambda *a, **k: None),
          "_replace_buttons_with_status":
              lambda ch, ts, txt: calls.append((ch, ts, txt))}
    # from the click-resolution helper the guard reads the card through
    _exec_block("def _clicked_card(", "@app.action", ns)
    return ns["_reject_if_stale_confirmation"], calls, registry


def _click(age_hours: float | None, action_id: str = "approve_send"):
    ts = f"{time.time() - age_hours * 3600:.6f}" if age_hours is not None else ""
    card = _chat.MessageRef("D1", ts, ts, float(ts) if ts else None)
    return _chat.ActionEvent(action_id, "U_OWNER", card, {})


def test_s4_fresh_confirmation_allowed():
    guard, calls, _ = _stale_guard()
    assert guard(_click(1.0), "send") is False
    assert calls == []


def test_s4_stale_confirmation_rejected():
    guard, calls, _ = _stale_guard()
    ev = _click(72.0)
    assert guard(ev, "send") is True
    assert len(calls) == 1
    assert calls[0][:2] == ("D1", ev.card.id)
    assert "stale" in calls[0][2]


def test_s4_registry_post_time_wins():
    """The card registry's post time is read first, so a frontend whose
    message ids are not timestamps is judged the same way."""
    guard, calls, registry = _stale_guard()
    card = _chat.MessageRef("D1", "m17", "m17", time.time() - 72 * 3600)
    registry.add(card, _chat.ConfirmationCard(kind="send", title="x"), code=None)
    ev = _chat.ActionEvent("approve_send", "U_OWNER",
                           _chat.MessageRef("D1", "m17", "m17"), {})
    assert guard(ev, "send") is True and len(calls) == 1


def test_s4_missing_ts_fails_open():
    # malformed payload must not lock the user out of a legit button
    guard, calls, _ = _stale_guard()
    assert guard(_click(None), "send") is False


def test_s4_boundary_just_under_limit():
    guard, calls, _ = _stale_guard()
    assert guard(_click(47.5), "send") is False


# ---------------------------------------------------------------------------
# Stage 5: confirmation-token gate lifecycle (graph_actions)
# ---------------------------------------------------------------------------

def _ga(tmp: Path):
    import graph_actions as ga
    ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
    ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
    return ga


def test_s5_token_single_use():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        token = ga._create_confirmation("send", "AAMkMSG=", "test draft")
        entry = ga._validate_and_consume_token(token, "send", "AAMkMSG=")
        assert entry and entry["message_id"] == "AAMkMSG="
        # one-time: second consume fails
        assert ga._validate_and_consume_token(token, "send", "AAMkMSG=") is None


def test_s5_token_action_and_id_must_match():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        token = ga._create_confirmation("send", "AAMkMSG=")
        assert ga._validate_and_consume_token(token, "delete", "AAMkMSG=") is None
        assert ga._validate_and_consume_token(token, "send", "AAMkOTHER=") is None
        # mismatches must NOT consume — the right call still works
        assert ga._validate_and_consume_token(token, "send", "AAMkMSG=")


def test_s5_expired_token_rejected_and_pruned():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        token = ga._create_confirmation("send", "AAMkMSG=")
        pending = json.loads(ga.PENDING_CONFIRMS_FILE.read_text())
        pending[token]["expires"] = (
            datetime.now() - timedelta(minutes=1)).isoformat()
        ga.PENDING_CONFIRMS_FILE.write_text(json.dumps(pending))
        assert ga._validate_and_consume_token(token, "send", "AAMkMSG=") is None
        assert token not in json.loads(ga.PENDING_CONFIRMS_FILE.read_text())


def test_s5_send_draft_blocked_without_token():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        touched = []
        orig = ga.graph
        ga.graph = types.SimpleNamespace(
            send_draft=lambda *a, **k: touched.append("send") or {},
            preflight_check=lambda *a, **k: {"ok": True, "errors": [],
                                             "warnings": []})
        try:
            args = types.SimpleNamespace(message_id="AAMkMSG=",
                                         confirm_token=None)
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_send_draft(args)
            out = json.loads(buf.getvalue())
            assert "BLOCKED" in out.get("error", "")
            assert touched == [], "send_draft must NOT be reached w/o token"
        finally:
            ga.graph = orig


def test_s5_send_draft_blocked_with_bogus_token():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        touched = []
        orig = ga.graph
        ga.graph = types.SimpleNamespace(
            send_draft=lambda *a, **k: touched.append("send") or {},
            preflight_check=lambda *a, **k: {"ok": True, "errors": [],
                                             "warnings": []})
        try:
            args = types.SimpleNamespace(message_id="AAMkMSG=",
                                         confirm_token="forged-token-123")
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    ga.cmd_send_draft(args)
                except SystemExit:
                    pass
            assert touched == [], "forged token must NOT reach send_draft"
        finally:
            ga.graph = orig


# --- a leading-dash token must not break argv parsing ----------------------
# base64url includes '-', so ~1 in 64 tokens looks like an option flag and
# argparse rejects it as "expected one argument". Two defences, both
# pinned here: tokens never start with '-', and every call site passes
# `--confirm-token=<tok>` so even a dashed token survives.

def test_s5_minted_tokens_never_start_with_dash():
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        for _ in range(400):
            assert not ga._new_token().startswith("-")
        # and the real mint path uses it
        assert not ga._create_confirmation("send", "AAMkMSG=").startswith("-")


def test_s5_dashed_token_needs_equals_form_in_argv():
    """Documents the failure mode the call sites must avoid."""
    import argparse
    p = argparse.ArgumentParser(prog="send-draft")
    p.add_argument("--message-id", required=True)
    p.add_argument("--confirm-token", default=None)
    try:
        p.parse_args(["--message-id", "M", "--confirm-token", "-dashy"])
        raise AssertionError("argparse should reject a bare dashed value")
    except SystemExit:
        pass
    assert p.parse_args(
        ["--message-id", "M", "--confirm-token=-dashy"]).confirm_token == "-dashy"


def test_s5_bot_passes_confirm_token_as_single_argv_element():
    """No `"--confirm-token", token` two-element form anywhere in the bot."""
    bad = re.findall(r'"--confirm-token",\s*(\w+)', BOT_SRC)
    assert not bad, (
        f"ea-chat-bot.py passes --confirm-token as a separate argv element "
        f"({bad}); use f\"--confirm-token={{token}}\" so a token beginning "
        f"with '-' is not parsed as an option flag"
    )
    assert BOT_SRC.count('f"--confirm-token={token}"') >= 5, (
        "all five gated call sites (send-draft, send-gmail-draft, create-event, "
        "update-event, delete-event) must use the = form"
    )


def test_s5_preprocessor_also_bans_two_element_token_form():
    """The same argv-splitting ban across preprocessor/*.py: the
    two-element form must not survive anywhere, prefix-safe value or
    not."""
    pre = HERE.parent / "preprocessor"
    bad = {}
    for f in pre.glob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            # argv-list usage only; argparse DEFINITIONS are fine
            if '"--confirm-token",' in line and "add_argument" not in line:
                bad.setdefault(f.name, []).append(n)
    assert not bad, (
        f"two-element '--confirm-token' argv form in {bad}; use the "
        f"'--confirm-token=<value>' single-element form"
    )


def _reply_args(**over):
    base = dict(message_id="AAMkMSG=", conversation_id="CONV1", cc=None,
                sender_only=False, content_type="Text", body="hello",
                force=False)
    base.update(over)
    return types.SimpleNamespace(**base)


def test_s6_create_reply_skips_when_user_already_replied():
    # Regression: a stale briefing surfaces a thread, but the user already
    # replied (has the latest message) -> create-reply must NOT draft.
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        made = []
        orig_graph, orig_ua, orig_de = ga.graph, ga._user_addresses, ga._draft_events
        ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
        ga._draft_events = types.SimpleNamespace(record=lambda *a, **k: None)
        ga.graph = types.SimpleNamespace(
            conversation_latest_sender=lambda cid: "wren.fairbanks84@example.edu",
            get_message_full=lambda mid: {"conversationId": "CONV1"},
            create_draft=lambda **k: made.append("draft") or {"id": "AAMkNEW="})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_create_reply(_reply_args())
            out = json.loads(buf.getvalue())
            assert out.get("status") == "skipped", out
            assert made == [], "must NOT draft when the user already replied"
        finally:
            ga.graph, ga._user_addresses, ga._draft_events = orig_graph, orig_ua, orig_de


def test_s6_create_reply_proceeds_when_other_is_latest():
    # The correspondent (not the user) sent the latest message -> a new turn,
    # so drafting a reply is correct.
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        made = []
        orig_graph, orig_ua, orig_de = ga.graph, ga._user_addresses, ga._draft_events
        ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
        ga._draft_events = types.SimpleNamespace(record=lambda *a, **k: None)
        ga.graph = types.SimpleNamespace(
            conversation_latest_sender=lambda cid: "site.host@example.com",
            get_message_full=lambda mid: {"conversationId": "CONV1"},
            create_draft=lambda **k: made.append("draft") or {"id": "AAMkNEW="})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_create_reply(_reply_args())
            out = json.loads(buf.getvalue())
            assert out.get("status") == "reply_draft_created", out
            assert made == ["draft"], "should draft when a new inbound is latest"
        finally:
            ga.graph, ga._user_addresses, ga._draft_events = orig_graph, orig_ua, orig_de


def _reply_with_latest(latest_recipients, target_sender="sawyer.lark@example.edu"):
    """create-reply where the user sent the latest message, to
    latest_recipients, and the message being answered came from
    target_sender. Returns (output, drafts made)."""
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        made = []
        orig_graph, orig_ua, orig_de = ga.graph, ga._user_addresses, ga._draft_events
        ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
        ga._draft_events = types.SimpleNamespace(record=lambda *a, **k: None)
        ga.graph = types.SimpleNamespace(
            conversation_latest_sender=lambda cid: "wren.fairbanks84@example.edu",
            conversation_latest_message=lambda cid: {
                "from": "wren.fairbanks84@example.edu", "recipients": latest_recipients},
            get_message_full=lambda mid: {"conversationId": "CONV1",
                                          "from": {"emailAddress": {"address": target_sender}}},
            create_draft=lambda **k: made.append("draft") or {"id": "AAMkNEW="})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_create_reply(_reply_args())
            return json.loads(buf.getvalue()), made
        finally:
            ga.graph, ga._user_addresses, ga._draft_events = orig_graph, orig_ua, orig_de


def test_s6_a_forward_to_a_colleague_is_not_an_answer():
    # The user forwarded the request to a colleague; the sender still has
    # no answer, so a reply to the sender's message drafts.
    out, made = _reply_with_latest(["colleague@example.edu"])
    assert out.get("status") == "reply_draft_created", out
    assert made == ["draft"]


def test_s6_a_reply_that_reached_the_sender_still_skips():
    out, made = _reply_with_latest(["sawyer.lark@example.edu", "teammate@example.edu"])
    assert out.get("status") == "skipped" and "--force" in out.get("override", ""), out
    assert made == []


def test_s6_create_reply_journals_the_rows_it_names():
    # --ledger-id reaches the journal record, which is what the Send button
    # reads to close exactly those rows.
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        recs = []
        orig_graph, orig_ua, orig_de = ga.graph, ga._user_addresses, ga._draft_events
        ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
        ga._draft_events = types.SimpleNamespace(record=lambda *a, **k: recs.append((a, k)))
        ga.graph = types.SimpleNamespace(
            conversation_latest_sender=lambda cid: "site.host@example.com",
            get_message_full=lambda mid: {"conversationId": "CONV1"},
            create_draft=lambda **k: {"id": "AAMkNEW="})
        try:
            with redirect_stdout(io.StringIO()):
                ga.cmd_create_reply(_reply_args(ledger_id=["site-visit"]))
            created = [(a, k) for a, k in recs if a[0] == "created"]
            assert created and created[0][1].get("ledger_ids") == ["site-visit"], recs
        finally:
            ga.graph, ga._user_addresses, ga._draft_events = orig_graph, orig_ua, orig_de


def test_s6_create_reply_force_overrides_guard():
    # --force bypasses the guard even if the user has the latest message.
    with tempfile.TemporaryDirectory() as td:
        ga = _ga(Path(td))
        made = []
        orig_graph, orig_ua, orig_de = ga.graph, ga._user_addresses, ga._draft_events
        ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
        ga._draft_events = types.SimpleNamespace(record=lambda *a, **k: None)
        ga.graph = types.SimpleNamespace(
            conversation_latest_sender=lambda cid: "wren.fairbanks84@example.edu",
            get_message_full=lambda mid: {"conversationId": "CONV1"},
            create_draft=lambda **k: made.append("draft") or {"id": "AAMkNEW="})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_create_reply(_reply_args(force=True))
            out = json.loads(buf.getvalue())
            assert out.get("status") == "reply_draft_created", out
            assert made == ["draft"]
        finally:
            ga.graph, ga._user_addresses, ga._draft_events = orig_graph, orig_ua, orig_de


# --- Stage 6b: the dedupe may only ever reuse an EA-AUTHORED draft -------
# Taking existing[0] from an unordered listing of EVERY draft in the
# conversation and replacing its body wholesale overwrites a reply the
# user started by hand in Outlook. The journal records that as `updated`,
# so no :lock: block is posted and nothing in Slack says it happened.
# Provenance decides instead: draft_capture for the conversation, or the
# draft-events journal for the id.
#
# These tests give the stub a real graph_get; without one the whole dedupe
# path falls into `except Exception` and is never exercised.

def _dedupe_ga(tmp: Path, drafts: list, *, ea_ids=(), captured=()):
    """graph_actions wired to a fake mailbox and a private provenance store."""
    ga = _ga(tmp)
    de_mod = _journal(tmp)                       # journal in the temp dir
    import draft_capture as dc
    dc.STORE = tmp / "draft-bodies"

    for did in ea_ids:
        de_mod.record("created", did, verb="create-reply")
    for did, conv in captured:
        dc.capture(did, "word " * 40, verb="create-reply", conversation_id=conv)

    seen = {"updated": [], "created": [], "gets": []}

    def graph_get(url, params=None, *a, **k):
        seen["gets"].append(url + " " + json.dumps(params or {}))
        return {"value": list(drafts)}

    ga.graph = types.SimpleNamespace(
        conversation_latest_sender=lambda cid: "oakley@example.com",
        get_message_full=lambda mid: {"conversationId": "CONV1"},
        graph_get=graph_get,
        update_draft=lambda eid, body, ct: seen["updated"].append((eid, body)),
        create_draft=lambda **k: (seen["created"].append(k)
                                  or {"id": "AAMkNEW=", "conversationId": "CONV1"}),
        set_draft_recipients=lambda *a, **k: None,
    )
    ga._user_addresses = lambda: {"wren.fairbanks84@example.edu"}
    ga._draft_events = de_mod
    ga._draft_capture = dc
    return ga, seen


def _run_reply(ga, **over):
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_create_reply(_reply_args(**over))
    return json.loads(buf.getvalue())


def _draft(did, modified):
    return {"id": did, "subject": "Re: numbers",
            "lastModifiedDateTime": modified}


def test_s6_dedupe_refuses_to_overwrite_a_hand_written_draft():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(tmp, [_draft("AAMkCHRIS=", "2026-08-22T09:00:00Z")])
        out = _run_reply(ga)
        assert out["status"] == "reply_draft_created", out
        assert seen["updated"] == [], (
            "a draft the EA never wrote must never be overwritten")
        assert seen["created"], "the reply still has to be drafted"


def test_s6_dedupe_reuses_a_journalled_ea_draft():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(tmp, [_draft("AAMkEA=", "2026-08-22T09:00:00Z")],
                              ea_ids=["AAMkEA="])
        out = _run_reply(ga)
        assert out["status"] == "reused_existing_draft", out
        assert out["id"] == "AAMkEA="
        assert [e[0] for e in seen["updated"]] == ["AAMkEA="]
        assert seen["created"] == []


def test_s6_dedupe_reuses_a_captured_ea_draft():
    """draft_capture is the other half of the provenance test."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(tmp, [_draft("AAMkCAP=", "2026-08-22T09:00:00Z")],
                              captured=[("AAMkCAP=", "CONV1")])
        out = _run_reply(ga)
        assert out["status"] == "reused_existing_draft", out
        assert out["id"] == "AAMkCAP="


def test_s6_dedupe_picks_the_newest_ea_draft_not_the_first_listed():
    """The newest EA draft wins; the first row of an unordered listing
    is arbitrary."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(
            tmp,
            [_draft("AAMkOLD=", "2026-08-01T09:00:00Z"),
             _draft("AAMkNEWEST=", "2026-08-22T18:00:00Z")],
            ea_ids=["AAMkOLD=", "AAMkNEWEST="])
        out = _run_reply(ga)
        assert out["id"] == "AAMkNEWEST=", out
        assert any("lastModifiedDateTime" in g for g in seen["gets"]), (
            "the listing must ask Graph to order by last edit")


def test_s6_dedupe_skips_the_hand_written_draft_and_takes_the_ea_one():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(
            tmp,
            [_draft("AAMkCHRIS=", "2026-08-22T18:00:00Z"),   # newest, his
             _draft("AAMkEA=", "2026-08-22T09:00:00Z")],
            ea_ids=["AAMkEA="])
        out = _run_reply(ga)
        assert out["id"] == "AAMkEA=", out
        assert [e[0] for e in seen["updated"]] == ["AAMkEA="]


def test_s6_dedupe_is_off_for_force_and_for_a_redirected_reply():
    for over in ({"force": True}, {"to": "nicole@example.com"}):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ga, seen = _dedupe_ga(
                tmp, [_draft("AAMkEA=", "2026-08-22T09:00:00Z")],
                ea_ids=["AAMkEA="])
            out = _run_reply(ga, **over)
            assert out["status"] == "reply_draft_created", (over, out)
            assert seen["updated"] == [], over


def test_s6_dedupe_survives_a_graph_that_rejects_orderby():
    """A rejected $orderby must fall back to the plain listing, not to
    'no drafts found' -- that would mint the duplicate the guard exists
    to prevent."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga, seen = _dedupe_ga(tmp, [_draft("AAMkEA=", "2026-08-22T09:00:00Z")],
                              ea_ids=["AAMkEA="])
        plain = {"value": [_draft("AAMkEA=", "2026-08-22T09:00:00Z")]}

        def picky_get(url, params=None, *a, **k):
            if "$orderby" in url + str(params or ""):
                raise RuntimeError("InefficientFilter")
            return plain

        ga.graph.graph_get = picky_get
        out = _run_reply(ga)
        assert out["status"] == "reused_existing_draft", out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
