"""Smart context selection for resident-pane dispatches.

The resident pane is ONE persistent interactive Claude conversation that
the Slack bot pastes each message into. This module decides, per
dispatch, what context is actually new:

  Axis 1, durable session primer (ONCE per resident session): the
    operating protocol, STYLE_RULES and paths. The model carries it for
    the rest of the conversation. A 2-line GUARDRAIL rides every
    dispatch as insurance against auto-compaction summarizing the primer
    away (the dangerous verbs are also code-gated, so a forgotten primer
    is degraded-quality, not unsafe).

  Axis 2, per-dispatch targeted context:
    * briefing extract: ALWAYS injected when non-empty (entity-matched
      to THIS message, so it cannot go stale against the thread).
    * skill files and scheduling rules: gated per (session, thread),
      static and large, so once a thread has them this session we rely
      on in-conversation memory.

"New vs continuation" is decided by a session-keyed tracker, NOT Slack's
is_thread_reply: after a rotation a Slack reply is brand-new to the
resident. State lives in ~/.ea/state/resident-context.json keyed by the
current resident session_id, so rotation (which changes session_id)
auto-resets it. The file is a CACHE, not a source of truth: losing it
costs at most one extra primer injection. The bot reads it with the
resident's access (resident_io) and keeps only the fields and types this
module writes, so a link at its name cannot hand the bot another file to
write back into the resident's space; the write is atomic_io's
replacement.

build_dispatch() is pure (no I/O; caller passes rendered blurbs) so the
selection logic is unit-testable. note_dispatched() is the only writer.
"""
from __future__ import annotations

import json
import os
try:
    import atomic_io as _atomic
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import resident_io as _rio
from pathlib import Path

try:
    from prompt_logic import STYLE_RULES
except Exception:  # pragma: no cover - import-path fallback
    from preprocessor.prompt_logic import STYLE_RULES  # type: ignore

try:
    import resident_bridge as _bridge
except Exception:  # pragma: no cover
    from preprocessor import resident_bridge as _bridge  # type: ignore

STATE_FILE = Path("/home/ea-bot/.ea/state/resident-context.json")

# Slack's wording, for when the configured frontend cannot be read.
_SLACK_PS = {"platform": "Slack", "room_word": "channel",
             "confirm_location": "the Send button is in your DM"}


def _prompt_strings() -> dict:
    """The configured chat platform's prompt wording (no client built);
    Slack's when the config cannot be read."""
    try:
        try:
            from chat import prompt_strings_for_config
        except ImportError:
            from preprocessor.chat import prompt_strings_for_config  # type: ignore
        ps = prompt_strings_for_config()
    except Exception:
        return dict(_SLACK_PS)
    return {k: ps.get(k) or v for k, v in _SLACK_PS.items()}


_PS = _prompt_strings()

# Durable operating instructions, injected ONCE per session. Paths are
# absolute because the resident runs from /home/ea-bot/ea-system as ea-bot.
PRIMER = f"""[EA orchestrator — operating protocol for this session. You will get many {_PS['platform']} dispatches in this one conversation, each prefixed with a [slack-dispatch thread=... id=...] marker. Your reply text between one marker and the next is posted to that marker's {_PS['platform']} thread.]
- Slack mrkdwn only: *single-asterisk bold*, _italic_, :emoji_shortcodes:. Never **double asterisks**.
- SHORT tasks (< ~2 min of tool work): do them now and reply.
- LONG tasks (deep research, many emails, big scans; never anything that creates a draft or stages an event): launch a BACKGROUND subagent (Agent tool, run_in_background) with a fully self-contained prompt, reply immediately that it is running, and stay responsive to new dispatches. When the background task finishes, write its report as a message that STARTS with the literal line [deliver:<thread_ts>] (the thread_ts from that task's marker) so the bot routes it to the right thread. Use [deliver:] ONLY for those async reports, never on a direct reply.
- Email bodies come from the remote writer: python3 /home/ea-bot/ea-system/preprocessor/email_writer.py write --to <addr> --brief-file <brief in ~/.ea/state> --out /home/ea-bot/.ea/state/reply-body.txt [--conversation-id <id>] [--draft-id <id> to revise]; do not rewrite its body. On exit 3 or 4 the email-author subagent writes it, and it ALWAYS runs in the FOREGROUND (run_in_background false). Your turn must end only after the draft exists; a turn that ends while it is writing is posted as your reply, and the Send button for the draft is never shown.
- Mail: python3 /home/ea-bot/ea-system/preprocessor/mail_actions.py <verb> --provider outlook|gmail for search, get-message, get-draft, mark-read/unread and every draft verb (create-draft, create-reply, create-forward, update-draft, delete-draft, preflight-check); the provider CLIs keep only their own verbs (get-conversation, get-thread, attachments, calendar). The Gmail MCP create_draft/update_draft are denied, they rewrote every link as a google.com/url?q= redirect. A REPLY is `create-reply --message-id <id>`, reply-all unless --sender-only, on both providers; on Gmail a plain create-draft opens a new conversation. The original is quoted below your text; revise with `mail_actions.py update-draft --draft-id <id>`, which keeps the quote and attachments. NEVER call send-draft — the bot posts the :lock: Send/Cancel block automatically once the draft exists. Every :lock: block goes to the user's DM with the bot, and the bot drops a one-line pointer in this thread. When you refer to it, say "{_PS['confirm_location']}"; never say the block is above or in this thread. Post ONE short confirmation line, do not paste the draft body.
- Calendar: write the pending-event JSON files (the bot posts :lock: Create/Reschedule/Delete buttons, in the DM, with a pointer here). Outlook event ids come from `graph_actions.py calendar-view --start <ISO> --end <ISO>`. A slot you park for an unconfirmed meeting is a HOLD: stage it with "hold": true and no attendees (created tentative, tagged EA Hold) and release it yourself later with `graph_actions.py clear-hold --event-id <id>`, no button; an EA-Managed Google hold is released with `google_calendar_client.py delete-event --event-id <id>`. For MULTIPLE events write pending-event.json ONCE as a JSON ARRAY of event objects — all the Create buttons post together, never one round-trip per event. In ANY pending-event*.json add "thread_ts" (and optionally "channel") from the dispatch marker: both are optional, change nothing about the event, and only pin the pointer and the result to THIS conversation instead of the channel root.
- Rooms: a project you create this turn gets its #ea- channel automatically after the turn. For an EXISTING project, write /home/ea-bot/.ea/state/pending-channel.json with {{"project": "<id>", "thread_ts": "<the marker's thread>"}}; the bot opens the room, invites the user and posts the link in the thread. You cannot create {_PS['platform']} {_PS['room_word']}s yourself: never say a room is opening unless you staged that file or created the project this turn.
- Deferred send: create the draft as usual, then write /home/ea-bot/.ea/state/pending-schedule-send.json with {{"draft_id", "send_after" (ISO), "description", optional "ledger_id", optional "thread_ts"}} — a JSON ARRAY stages several. The bot posts a :lock: Schedule button; the click is what queues it. NEVER call graph_actions.py schedule-send or scheduled_sends.py add yourself. NEVER pick the send time yourself: if the user did not give one, ask.
- Before drafting ANY email yourself, run: python3 /home/ea-bot/ea-system/preprocessor/style_profiles.py get --contact <recipient-addr> — it returns the accumulated style profile AND the user's recent real emails to that person. Mimic those examples (formality, length, sign-off). found:false -> use writing_style.default_* from the briefing.
- All ea-system scripts live at /home/ea-bot/ea-system/preprocessor/ — always use absolute paths. Track obligations with ledger.py (add/update/close/list/search); close only on the user's word (close --user-said), otherwise ledger.py propose (CLAUDE.md rule 8).
- Attached files: when a dispatch contains a line like "[The user attached a file ... saved at /home/ea-bot/.ea/uploads/...]", READ that file (the Read tool handles PDFs and images) BEFORE acting — the user attached it because its contents matter to the request.

{STYLE_RULES}"""

# Always-on, so the few hard rules survive auto-compaction of the primer.
GUARDRAIL = ("[Reminders: NEVER send-draft (bot handles Send buttons); "
             "email-author always in the foreground; "
             "[deliver:<thread_ts>] only for async background reports; "
             "Slack mrkdwn *single asterisks*, no em-dashes.]")


def _tracker_shape(data) -> dict:
    """The tracker fields and types this module writes, and nothing else:
    session_id, primer_injected and threads (thread key to a list of
    injected keys). ValueError when `data` is not a JSON object."""
    if not isinstance(data, dict):
        raise ValueError(f"{STATE_FILE}: expected a JSON object")
    sid = data.get("session_id")
    threads = data.get("threads")
    state = {
        "session_id": sid if isinstance(sid, str) else None,
        "primer_injected": data.get("primer_injected") is True,
        "threads": {k: [x for x in v if isinstance(x, str)]
                    for k, v in (threads.items() if isinstance(threads, dict) else ())
                    if isinstance(k, str) and isinstance(v, list)},
    }
    return state


def _read_tracker() -> dict | None:
    """The stored tracker in its shape, or None when there is no file.

    The read runs with the resident's access, so a link at the name
    reaches nothing the resident could not read. OSError when the file
    exists but could not be read, so a writer keeps the records in it;
    ValueError when its content is not a tracker."""
    raw = _rio.read_bytes(STATE_FILE)
    if raw is None:
        if os.path.lexists(STATE_FILE):
            raise OSError(f"could not read {STATE_FILE}")
        return None
    return _tracker_shape(json.loads(raw))


def _load_state(session_id: str | None, *, strict: bool = False) -> dict:
    """Return tracker state IFF it matches session_id, else fresh empty.

    Mismatch (rotation changed session_id), a missing file or content that
    is not a tracker yield a fresh state: the tracker is a cache, so the
    cost of a reset is at most one extra primer injection. A file that
    exists but could not be read also yields a fresh state, except under
    `strict` (a writer), where the OSError propagates so the write does
    not replace the records in that file."""
    fresh = {"session_id": session_id, "primer_injected": False,
             "threads": {}}
    if not session_id:
        return fresh
    try:
        data = _read_tracker()
    except OSError:
        if strict:
            raise
        return fresh
    except Exception:
        return fresh
    if data is None or data["session_id"] != session_id:
        return fresh
    return data


def thread_seen(session_id: str | None, thread_ts: str) -> bool:
    """Has the resident already replied in this thread during the current
    session? Used by the bot's concurrency router: a follow-up to a
    resident-owned thread QUEUES for the pane (continuity); a new/unseen
    thread SPILLS to the per-thread lane (parallel). Authoritative over
    Slack's is_thread_reply, which is blind to rotation."""
    if not session_id:
        return False
    return thread_ts in (_load_state(session_id).get("threads") or {})


def _skill_key(detected_skills) -> str | None:
    """Stable key for a set of detected skills (whole-blob dedup)."""
    if not detected_skills:
        return None
    return "skills:" + ",".join(sorted(detected_skills))


def build_dispatch(*, user_msg: str, thread_ts: str, msg_ts: str,
                   session_id: str | None, briefing_blurb: str = "",
                   skill_blurb: str = "", detected_skills=None,
                   scheduling_ctx: str = "",
                   needed_modules=None) -> tuple[str, dict]:
    """Assemble the dispatch text, including only what this turn needs.

    Pure: no disk I/O beyond reading the tracker via _load_state. The
    caller renders briefing_blurb / skill_blurb / scheduling_ctx and
    passes the detected skill names + needed modules.

    Returns (dispatch_text, debug) where debug =
    {primer_injected, is_continuation, injected: [...], bytes}.
    `injected` is the list of gated keys actually included this dispatch;
    pass it to note_dispatched() so the next turn knows to skip them.
    """
    detected_skills = list(detected_skills or [])
    needed_modules = set(needed_modules or set())
    state = _load_state(session_id)
    seen = set(state.get("threads", {}).get(thread_ts, []))
    primer_done = bool(state.get("primer_injected"))

    injected: list[str] = []
    parts: list[str] = []

    # Durable primer, once per session. An unknown session_id cannot be
    # tracked, so inject: over-priming beats a primer-less session.
    include_primer = (not primer_done) or (session_id is None)
    if include_primer:
        parts.append(PRIMER)
        if session_id is not None:
            injected.append("primer")

    marker = _bridge.make_marker(thread_ts, msg_ts)
    parts.append(f"{marker} The user says:\n{user_msg}")

    # Briefing extract, always when present (entity-matched, fresh).
    if briefing_blurb and briefing_blurb.strip():
        parts.append(briefing_blurb.strip())

    # Scheduling rules, gated per (session, thread).
    if scheduling_ctx and scheduling_ctx.strip() and "scheduling" not in seen:
        parts.append(scheduling_ctx.strip())
        injected.append("scheduling")

    # Skill .md blob, gated per (session, thread) by the detected-skill
    # set. A new or expanded skill set re-injects the whole blob.
    skey = _skill_key(detected_skills)
    if skill_blurb and skill_blurb.strip() and skey and skey not in seen:
        parts.append(skill_blurb.strip())
        injected.append(skey)

    # Always-on guardrail (cheap; survives compaction).
    parts.append(GUARDRAIL)

    dispatch_text = "\n\n".join(parts)
    debug = {
        "primer_injected": include_primer,
        "is_continuation": primer_done and bool(seen),
        "injected": injected,
        "bytes": len(dispatch_text),
    }
    return dispatch_text, debug


def note_dispatched(session_id: str | None, thread_ts: str,
                    injected: list[str]) -> None:
    """Record what was injected so later turns skip it. No-op when the
    session is unknown, and when the tracker exists but could not be read
    (its records are kept). Atomic tmp+replace; a lost update at worst
    re-injects a skill blob."""
    if not session_id:
        return
    try:
        state = _load_state(session_id, strict=True)
        state["session_id"] = session_id
        if "primer" in injected or state.get("primer_injected"):
            state["primer_injected"] = True
        threads = state.setdefault("threads", {})
        existing = set(threads.get(thread_ts, []))
        existing.update(k for k in injected if k != "primer")
        threads[thread_ts] = sorted(existing)
        _write_state(state)
    except Exception:
        # Tracker is best-effort; a write failure only costs re-injection.
        pass


def _write_state(state: dict) -> None:
    _atomic.write_json(STATE_FILE, state)
