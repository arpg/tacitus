#!/usr/bin/env python3
"""
Sync the obligations ledger with Outlook inbox state.

When the user manually archives an email from their inbox (via Outlook,
mobile, etc.), this script detects the change and marks the corresponding
ledger item as completed. The inbox is treated as an alternate completion
signal: if you moved it out, you handled it.

This behavior is controlled by the `inbox_as_completion` preference in
`~/.ea/config/autonomy-tiers.json`. When enabled (default), archiving
an email closes the ledger item. When disabled, ledger items stay open
regardless of inbox state.

Arbitration, three outcomes per row (only the first one closes):

  ARCHIVED  the mail is demonstrably out of the inbox -> close.
  IN_INBOX  the mail is demonstrably in the inbox -> defer, and heal the
            stored source_message_id if Outlook has rewritten it.
  UNKNOWN   the stored id is dead and the conversation cannot be
            re-resolved, or Graph errored -> defer and report. A row the
            sync cannot adjudicate is never closed.

An archive closes only the rows the inbox detectors write (ids starting
response-, priority-, sched-, thread-: one row per mail that asks for
something), where archiving the mail is the user's answer. Every other row
is work the EA tracks (a task from a conversation, a follow-up), linked to
its thread as evidence; the user archives that mail once he has handed it
over, so an archive never closes such a row. Evidence (closure_scrubber)
or the user does. Such rows are still checked, to heal their message ids,
and reported as held.

Among detector rows, an archive closes a row only when it can be the
user's answer to that row. A row the sync has never seen with its mail in the inbox, whose
thread was last changed before the row was created, was attached to mail
already archived (a task from a conversation, linked to an old thread for
context): that archive predates the obligation, so it defers. When the
timing cannot be read, the archive closes as before. A row reopened after an
archive closed it (metadata `archive_not_evidence`, set by `ledger.py
update --status open`) is never closed by an archive again.

The EA's own moves never count: graph_client records every move it makes
(ea_moves), and a row whose mail the EA moved during its life defers. The
close itself goes through the closure policy (closure.settle, signal
user_archived), one row per mail: a sibling whose own mail is still in the
inbox is a separate ask and stays open.

Rows the candidate SELECT declines outright (future snooze_until, future
follow_up_after, plugin-sourced) are not adjudicated at all: the system
itself moved that mail out of the inbox, so its absence carries no
information about the user.

A check that meets HTTP 429 from Graph makes the next three checks skip
(retry_policy.TickBackoff, state in ~/.ea/state/tick-backoff.json).

Usage:
  python ledger_sync.py check   # cron calls this every 15 min
  python ledger_sync.py status  # show what would be closed
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ledger as ledger_mod
import closure  # noqa: E402
import ea_moves  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
AUTONOMY_FILE = EA_DIR / "config" / "autonomy-tiers.json"
LOG_FILE = EA_DIR / "logs" / "ledger-sync.log"

# Inbox states a candidate row can be in. ARCHIVED is the ONLY one that
# closes; the other two defer. "unknown" exists because a Graph 404 does
# not mean "the user archived it": Outlook changes a message id on every
# folder move, so a 404 is equally consistent with a snooze or wake that
# just put the mail back in front of the user.
IN_INBOX = "in_inbox"
ARCHIVED = "archived"
UNKNOWN = "unknown"


def _plain(value) -> bool:
    """A ledger row is resident state: an id read from one goes into a
    Graph path only when it is a plain mail id (mail_mediation.is_plain_id)."""
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import mail_mediation as _mm
    return _mm.is_plain_id(value)

# Candidate predicate, shared by `check` and `status` and importable by
# tests, so a change here cannot pass a test that inlined its own copy.
#
# Guards, in order:
#   * status: active rows only.
#   * snoozed rows with a FUTURE snooze_until are opted out: archiving a
#     snoozed email is part of the snooze flow, so closing on archive
#     would defeat the snooze.
#   * follow_up_after in the FUTURE is opted out for the same reason: the
#     follow-up flow archives the mail and then parks the row for a later
#     date, so the archive is the system's own doing, not a completion
#     signal. The predicate is deliberately identical to
#     ledger.resurrect_due_follow_ups so the two never disagree about
#     when a follow-up is due.
#   * source IS NULL: main-EA rows only. source_message_id semantics here
#     are Outlook-specific (Graph checks the owner's Outlook inbox) and
#     plugin rows may use other id namespaces. Each plugin handles its
#     own lifecycle, which is why the sibling co-close in cmd_check
#     carries the same `source IS NULL` predicate.
CANDIDATE_WHERE = """
        (
            status IN ('open', 'follow_up')
            OR (status = 'snoozed'
                AND (snooze_until IS NULL OR snooze_until <= datetime('now')))
        )
        AND (follow_up_after IS NULL OR follow_up_after <= date('now'))
        AND source_message_id IS NOT NULL
        AND source_message_id != ''
        AND source IS NULL
"""

CANDIDATE_SQL = f"""
    SELECT id, description, source_message_id, last_action, conversation_id,
           created_at, metadata
    FROM ledger
    WHERE {CANDIDATE_WHERE}
"""


def candidate_rows(db) -> list:
    """Rows ledger_sync is allowed to reconcile against the inbox."""
    return db.execute(CANDIDATE_SQL).fetchall()


_GRAPH = None


def _graph():
    """Import graph_client lazily.

    At module scope the import makes ledger_sync unimportable for tests
    and for any ea-bot-side caller under mail mediation (the MSAL cache is
    owner-owned, so the import itself raises PermissionError). Deferring it
    keeps the SQL half of this module loadable everywhere.
    """
    global _GRAPH
    if _GRAPH is None:
        import graph_client as _g
        _GRAPH = _g
    return _GRAPH


def _load_env():
    """Fill in the Graph settings and secrets not already in the environment:
    secrets by the owner_trust rule, settings from ~/.ea/.env as data (only
    owner_trust.DOTENV_SETTING_KEYS; the file is the resident's)."""
    try:
        import owner_trust
    except ImportError:
        from preprocessor import owner_trust
    owner_trust.load_owner_env()


def _inbox_as_completion_enabled() -> bool:
    """Check if inbox-as-completion is enabled in autonomy config."""
    if not AUTONOMY_FILE.exists():
        return True  # Default: enabled
    config = json.loads(AUTONOMY_FILE.read_text())
    return config.get("inbox_as_completion", {}).get("enabled", True)


def _get_inbox_folder_id() -> str:
    """Get the actual folder ID for the inbox."""
    data = _graph().graph_get("/me/mailFolders/inbox", {"$select": "id"})
    return data["id"]


def _conversation_inbox_state(conversation_id: str,
                              inbox_id: str) -> tuple[str, str | None]:
    """Re-resolve a stale message id through its conversation.

    Outlook rewrites a message's id on every folder move and nothing writes
    the new id back to the ledger, so a stored source_message_id goes stale
    the first time the message is archived, snoozed or woken. Asking the
    conversation instead of the dead id answers the question that actually
    matters: is ANY live message of this thread still in the inbox?

    Same recovery pattern as graph_client._resolve_latest_in_conversation
    (used by create-reply for the identical staleness class), extended to
    carry parentFolderId so the answer is a folder, not just an id.

    Returns (state, live_id). UNKNOWN means "could not tell", never
    "archived".
    """
    if not conversation_id or "'" in conversation_id:
        # No thread to ask, or an id that cannot be safely quoted into an
        # OData filter. Either way: unknown, not archived.
        return (UNKNOWN, None)
    try:
        data = _graph().graph_get("/me/messages", {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,parentFolderId,isDraft,receivedDateTime",
            "$top": 25,
        })
    except Exception:
        return (UNKNOWN, None)
    msgs = [m for m in (data.get("value") or []) if not m.get("isDraft")]
    if not msgs:
        # The thread is not resolvable at all. It may have been hard
        # deleted, or the query may have failed silently. Not evidence of
        # a user archive.
        return (UNKNOWN, None)
    # Graph won't reliably $orderby alongside a conversationId $filter, so
    # sort client-side.
    msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    for m in msgs:
        if m.get("parentFolderId") == inbox_id:
            return (IN_INBOX, m.get("id"))
    return (ARCHIVED, msgs[0].get("id"))


def _message_inbox_state(message_id: str, inbox_id: str,
                         conversation_id: str = "") -> tuple[str, str | None, str]:
    """Where does this ledger row's mail live right now?

    Returns (state, live_id, note). `live_id` is the message's current
    Outlook id when we managed to re-resolve it, so the caller can heal the
    stored source_message_id. `note` is a short diagnostic for the UNKNOWN
    case.
    """
    import requests as req
    if not _plain(message_id):
        return (UNKNOWN, None, "the stored id is not a plain mail id")
    try:
        data = _graph().graph_get(
            f"/me/messages/{message_id}",
            {"$select": "parentFolderId"},
        )
        if data.get("parentFolderId") == inbox_id:
            return (IN_INBOX, message_id, "")
        return (ARCHIVED, message_id, "")
    except req.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", None) if e.response is not None else None
        if code == 404:
            # The stored id is dead. It does NOT follow that the user
            # archived the mail: snooze and wake-snoozed both move the
            # message and both mint a fresh id. Ask the conversation.
            state, live_id = _conversation_inbox_state(conversation_id, inbox_id)
            if state == UNKNOWN:
                note = ("stale source_message_id (404) and conversation "
                        "could not be re-resolved"
                        if conversation_id else
                        "stale source_message_id (404) and row has no "
                        "conversation_id to re-resolve through")
                return (UNKNOWN, None, note)
            return (state, live_id, "")
        return (UNKNOWN, None, f"graph HTTP {code}")
    except Exception as e:
        return (UNKNOWN, None, f"{type(e).__name__}")


def _check_message_in_inbox(message_id: str, inbox_id: str,
                            conversation_id: str = "") -> bool | None:
    """Tri-state wrapper kept for callers that only want the boolean.

    True = still in inbox, False = archived/moved out, None = cannot tell.
    None NEVER closes.
    """
    state, _, _ = _message_inbox_state(message_id, inbox_id, conversation_id)
    if state == IN_INBOX:
        return True
    if state == ARCHIVED:
        return False
    return None


def _heal_source_message_id(entry_id: str, live_id: str) -> None:
    """Write a re-resolved Outlook id back onto the ledger row.

    Without this the row 404s again on every subsequent pass. Touches only
    source_message_id: last_action carries closure provenance and
    updated_at feeds staleness ranking, so neither is disturbed by what is
    really a pointer repair.
    """
    try:
        db = ledger_mod._get_db()
        db.execute("UPDATE ledger SET source_message_id = ? WHERE id = ?",
                   (live_id, entry_id))
        db.commit()
        db.close()
    except Exception:
        pass  # Best effort: a failed heal costs one extra 404 next pass.


DETECTOR_PREFIXES = ledger_mod.DETECTOR_PREFIXES


def _moved_by_ea(msg_id: str, live_id: str, created_at, raw_metadata=None) -> bool:
    """True when the EA's own move took this mail out of the inbox during
    the row's life (ea_moves, recorded by graph_client on every move) and
    the mail has not been seen back in the inbox since."""
    try:
        cleared = str(_metadata(raw_metadata).get("ea_move_cleared") or "")
        return ea_moves.moved_out_by_ea([msg_id, live_id], since=str(created_at or ""),
                                        after=cleared) is not None
    except Exception:
        return False


def _clear_ea_move(entry_id: str, msg_id: str, live_id: str, created_at, raw_metadata) -> None:
    """The mail is in the inbox again after an EA move took it out: a later
    absence is the user's own doing, so that move stops counting."""
    try:
        hit = ea_moves.moved_out_by_ea([msg_id, live_id], since=str(created_at or ""),
                                       after=str(_metadata(raw_metadata).get("ea_move_cleared") or ""))
    except Exception:
        hit = None
    if hit:
        _set_metadata(entry_id, raw_metadata, ea_move_cleared=hit.get("ts", ""))


def archive_closes(entry_id: str) -> bool:
    """Whether an archive of its mail can close this row: detector rows only."""
    return ledger_mod.is_detector_row(entry_id)


# An archive this long before the row's creation still counts as after
# it: a detector can see the mail in the inbox and write the row a moment
# later, and the two clocks are not the same clock.
PREDATE_MARGIN_S = 120


def _metadata(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set_metadata(entry_id: str, raw_metadata, **fields) -> None:
    """Merge fields into the row's metadata. Best effort."""
    meta = _metadata(raw_metadata)
    if all(meta.get(k) == v for k, v in fields.items()):
        return
    meta.update(fields)
    try:
        db = ledger_mod._get_db()
        db.execute("UPDATE ledger SET metadata = ? WHERE id = ?",
                   (json.dumps(meta, sort_keys=True), entry_id))
        db.commit()
        db.close()
    except Exception:
        pass


def _mark_inbox_seen(entry_id: str, raw_metadata) -> None:
    """Record that the row's mail has been in the inbox during its life, so
    a later archive is the user's answer."""
    _set_metadata(entry_id, raw_metadata, inbox_seen=True)


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _thread_last_modified(message_id: str, conversation_id: str) -> datetime | None:
    """The latest lastModifiedDateTime of the row's message and its thread,
    or None when Graph gives none. A move out of the inbox changes it."""
    stamps = []
    if not _plain(message_id):
        return None
    try:
        d = _graph().graph_get(f"/me/messages/{message_id}",
                               {"$select": "lastModifiedDateTime"})
        stamps.append(_parse_ts(d.get("lastModifiedDateTime")))
    except Exception:
        pass
    if conversation_id and "'" not in conversation_id:
        try:
            data = _graph().graph_get("/me/messages", {
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": "lastModifiedDateTime", "$top": 25})
            stamps += [_parse_ts(m.get("lastModifiedDateTime"))
                       for m in data.get("value") or []]
        except Exception:
            pass
    stamps = [t for t in stamps if t is not None]
    return max(stamps) if stamps else None


def _archive_predates_row(entry_id: str, message_id: str, conversation_id: str,
                          created_at, raw_metadata) -> bool:
    """True only on evidence that the mail left the inbox before the row
    existed: the sync never saw it in the inbox for this row, and nothing in
    the thread changed after the row was created. The finding is kept, since
    a later unrelated touch of the thread (a mailbox-wide change) would make
    the old archive look new; the thread's return to the inbox ends it."""
    meta = _metadata(raw_metadata)
    if meta.get("archive_not_evidence"):
        return True  # reopened after an archive closed it: the archive was not the answer
    if meta.get("inbox_seen"):
        return False
    if meta.get("archived_before_row"):
        return True
    created = _parse_ts(created_at)
    if created is None:
        return False
    last = _thread_last_modified(message_id, conversation_id)
    if last is None or (created - last).total_seconds() <= PREDATE_MARGIN_S:
        return False
    _set_metadata(entry_id, raw_metadata,
                  archived_before_row=last.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return True


# The check's throttle backoff (retry_policy.TickBackoff): a check that met
# HTTP 429 makes the next TICK_SKIPS checks skip, so a mailbox Microsoft
# throttles is not asked once per ledger row every 15 minutes.
BACKOFF_JOB = "ledger_sync"


def _backoff():
    try:
        import retry_policy
    except ImportError:
        from preprocessor import retry_policy
    return retry_policy.TickBackoff(BACKOFF_JOB)


def _throttled_calls() -> int:
    """How many of this process's Graph reads ended on 429 so far."""
    try:
        return int(_graph().throttled_calls())
    except Exception:
        return 0


def _note_throttle(backoff, before: int, exc: BaseException | None = None) -> bool:
    """Start the backoff when this check met HTTP 429 (a read that spent its
    retries, raised or swallowed): True when it did."""
    try:
        import retry_policy
    except ImportError:
        from preprocessor import retry_policy
    if not (retry_policy.is_throttled(exc) or _throttled_calls() > before):
        return False
    backoff.throttled("HTTP 429")
    return True


def cmd_check(args):
    """Check inbox state for all active ledger items with message IDs."""
    _load_env()

    if not _inbox_as_completion_enabled():
        print(json.dumps({"skipped": True, "reason": "inbox_as_completion disabled"}))
        return

    backoff = _backoff()
    left = backoff.take()
    if left:
        print(json.dumps({"skipped": True,
                          "reason": f"Graph throttled a recent check ({left - 1} more to skip)"}))
        return
    before = _throttled_calls()

    # Candidate set: see CANDIDATE_WHERE for every guard and why it exists.
    db = ledger_mod._get_db()
    rows = candidate_rows(db)
    db.close()

    if not rows:
        print(json.dumps({"checked": 0, "closed": 0}))
        return

    # Get inbox folder ID once
    try:
        inbox_id = _get_inbox_folder_id()
    except Exception as e:
        out = {"error": f"Could not get inbox folder: {e}"}
        if _note_throttle(backoff, before, e):
            out["backoff"] = f"skipping the next {backoff.skips} checks"
        print(json.dumps(out))
        return

    closed = []
    checked = 0
    unresolved: list[dict] = []
    predated: list[str] = []
    held: list[str] = []
    ea_moved: list[str] = []
    proposed: list[str] = []
    healed = 0
    for row in rows:
        msg_id = row[2]  # source_message_id
        conv_id_stored = (row[4] or "").strip() if len(row) > 4 else ""
        state, live_id, note = _message_inbox_state(
            msg_id, inbox_id, conv_id_stored)
        checked += 1

        if state == UNKNOWN:
            # "I cannot tell" is not "the user archived it". Defer, and
            # surface the row so a stuck id is visible instead of silently
            # closing an obligation.
            unresolved.append({"id": row[0], "reason": note})
            continue

        if state == IN_INBOX:
            if live_id and live_id != msg_id:
                _heal_source_message_id(row[0], live_id)
                healed += 1
            _mark_inbox_seen(row[0], row[6] if len(row) > 6 else None)
            _clear_ea_move(row[0], msg_id, live_id, row[5], row[6] if len(row) > 6 else None)
            continue

        if state == ARCHIVED and not archive_closes(row[0]):
            # Work the EA tracks: the archive is not the answer to it.
            held.append(row[0])
            continue

        if state == ARCHIVED and len(row) > 6 and _archive_predates_row(
                row[0], live_id or msg_id, conv_id_stored, row[5], row[6]):
            # The mail was archived before this row existed, so archiving
            # it says nothing about the row. Defer, and report it.
            predated.append(row[0])
            continue

        if state == ARCHIVED and _moved_by_ea(msg_id, live_id, row[5],
                                              row[6] if len(row) > 6 else None):
            # The EA moved this mail itself (a snooze, a close's thread
            # archive, the admissions filter, the list digest): its absence
            # from the inbox says nothing about the user. Defer.
            ea_moved.append(row[0])
            continue

        if state == ARCHIVED:
            # The user moved this row's own mail out of the inbox. The policy
            # closes a detector row on it; each row is judged by its own mail,
            # so a sibling whose mail is still in the inbox stays open.
            entry_id = row[0]
            description = row[1]
            prev_action = row[3] or ""
            close_note = (
                f"{datetime.now().strftime('%Y-%m-%d')}: "
                f"Auto-closed: email archived from inbox by user. "
                f"Previous: {prev_action}"
            )
            db = ledger_mod._get_db()
            res = closure.settle(db, entry_id, signal="user_archived", actor="ledger_sync",
                                 note=close_note, evidence={"message_id": live_id or msg_id},
                                 sweep=False)
            if res["action"] == "closed":
                db.execute("UPDATE ledger SET email_archived = 1 WHERE id = ?", (entry_id,))
                closed.append(description)
            elif res["action"] == "proposed":
                proposed.append(entry_id)
            db.commit()
            db.close()

    out = {"checked": checked, "closed": len(closed), "descriptions": closed}
    if healed:
        out["healed_message_ids"] = healed
    if predated:
        out["archived_before_row"] = predated
    if held:
        out["held_ea_rows"] = len(held)
        out["held_ea_ids"] = held[:20]
    if ea_moved:
        out["moved_by_ea"] = ea_moved[:20]
    if proposed:
        out["proposed"] = proposed[:20]
    if unresolved:
        # Deliberately reported, not swallowed: these are rows the sync
        # could not adjudicate, and a growing count means stale ids are
        # accumulating rather than that nothing happened.
        out["unresolved"] = len(unresolved)
        out["unresolved_items"] = unresolved[:20]
    if _note_throttle(backoff, before):
        out["backoff"] = f"Graph answered HTTP 429; skipping the next {backoff.skips} checks"
    print(json.dumps(out))


def cmd_status(args):
    """Dry run: show which items would be closed."""
    _load_env()

    enabled = _inbox_as_completion_enabled()
    print(f"inbox_as_completion: {'enabled' if enabled else 'disabled'}")

    if not enabled:
        print("No checks performed (disabled).")
        return

    db = ledger_mod._get_db()
    rows = candidate_rows(db)
    db.close()

    print(f"Active items with message IDs: {len(rows)}")

    if not rows:
        return

    try:
        inbox_id = _get_inbox_folder_id()
    except Exception as e:
        print(f"Error getting inbox folder: {e}")
        return

    labels = {IN_INBOX: "IN INBOX", ARCHIVED: "ARCHIVED", UNKNOWN: "UNRESOLVED"}
    for row in rows:
        state, live_id, note = _message_inbox_state(
            row[2], inbox_id, (row[4] or "").strip())
        would_close = " -> WOULD CLOSE" if state == ARCHIVED else ""
        detail = f"  ({note})" if note else ""
        if state == IN_INBOX and live_id and live_id != row[2]:
            detail = "  (stale id; would heal from conversation)"
        print(f"  [{labels[state]}] {row[1]}{would_close}{detail}")


if __name__ == "__main__":
    # A tick: its Graph calls run in the background lane unless its caller
    # set one (retry_policy.graph_lane).
    import retry_policy
    retry_policy.set_graph_lane(retry_policy.BACKGROUND, only_if_unset=True)
    parser = argparse.ArgumentParser(description="Sync ledger with inbox state")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)
