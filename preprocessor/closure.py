"""Closing obligation rows: one policy, one write path, one history.

Two goals pull against each other: no row may close by mistake, and every
row that is done should close. The policy splits the evidence in two.

  decisive   evidence tied to this row that means its work happened: the
             user said so or clicked; a draft, event or scheduled send that
             names the row went out; the user's own reply to the person a
             reply row owes; the user's archive of a detector row's mail; a
             duplicate folded into a row that stays open. It closes.
  proposal   everything else that suggests the row is done: a calendar
             match, an acknowledgement, a reply in a thread that carries
             other work, a model's reading of the thread. It never changes
             the row's status. The row carries the proposal, the briefing
             and a chat card list it, and one click closes it, or keeps it
             open.

"Still open" on a card, or a reopen, is the user's judgment of the whole
row at that moment. No evidence from before it may close or propose the
row again, under any signal name: one send reaches the bot as a reply in
its thread, the scrubber as the user's Outlook reply and the mail tick as
a message in the thread. After it only the user's word, a duplicate or a
send that names the row closes the row; other evidence proposes.

Every transition (close, reopen, propose, confirm, reject) appends a row
to `ledger_events` with its signal, actor and evidence, so `report()`
counts, per signal, the closes later reopened (false closes), the
proposals confirmed and rejected, and the closes the user had to state.

The policy lives here and nowhere else: a closer names its signal and
`settle()` decides what that signal may do to this row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    import ledger as _ledger
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ledger as _ledger

DECISIVE = "decisive"
PROPOSAL = "proposal"

# The user acted on this row: a statement in chat, a click on a card or the
# dashboard, a standing rule they wrote that says such mail is no work.
USER_SIGNALS = frozenset({
    "user_stated", "user_confirmed", "user_dashboard", "user_handled",
    "user_sent_myself", "user_feedback", "user_rule",
})
# The EA's own action named this row and the user approved it: a sent draft
# (`--ledger-id`), a created event, a scheduled send that went out.
LINKED_SIGNALS = frozenset({"linked_send", "linked_event", "linked_scheduled_send"})
# The user's own message to the person a row owes a reply. Decisive only
# on a reply row (ledger.closes_on_reply); on any other row a reply in its
# thread can be about something else, so it proposes.
REPLY_SIGNALS = frozenset({
    "user_replied_outlook", "user_replied_gmail", "send_reply_row",
})
# The user moved the row's own mail out of the inbox. Decisive only on a
# detector row, the one row whose whole content is that mail.
ARCHIVE_SIGNALS = frozenset({"user_archived"})
# Bookkeeping that loses no obligation: a duplicate folded into a row that
# stays open, a detector sibling answered by the same evidence.
STRUCTURAL_SIGNALS = frozenset({"duplicate_of", "detector_sibling"})

# How long a rejected model proposal stays quiet for that row. A model has
# no evidence clock, so a rejection cannot be compared with the evidence.
LLM_VETO_DAYS = 14

# The close_vetoes key for "the user kept this whole row open at this
# time", set by reject() and note_reopen() beside the per-signal keys.
KEPT_OPEN = "*"

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    entry_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    tier      TEXT,
    signal    TEXT,
    actor     TEXT,
    evidence  TEXT,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_events_entry ON ledger_events(entry_id);
CREATE INDEX IF NOT EXISTS idx_ledger_events_ts ON ledger_events(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# Evidence that finishes a row declared to close on it (ledger.CLOSES_ON):
# an acknowledgement of the submission in the row's own thread, the user's
# own answer to the invite the row asks about.
SUBMISSION_SIGNALS = frozenset({"inbound_ack:submission-ack"})
EVENT_SIGNALS = frozenset({"meeting_response_sent"})
# The row's date passed and ended it (ledger.lapses, ledger.close_lapsed):
# decisive on a row the EA declared to lapse, or on a row intake made from
# mail that asked nothing, when the user never wrote in its thread; a
# thread he wrote in may hold work the records missed, so it proposes.
LAPSE_SIGNALS = frozenset({"lapsed"})


def is_plugin_row(entry: dict) -> bool:
    return str(entry.get("source") or "").startswith("plugin:")


def tier(signal: str, entry: dict) -> str:
    """What `signal` may do to `entry`: DECISIVE closes, PROPOSAL proposes."""
    if is_plugin_row(entry):
        # A plugin owns its rows' lifecycle (docs/plugin-architecture.md);
        # its close is recorded, not second-guessed.
        return DECISIVE
    if signal in USER_SIGNALS or signal in STRUCTURAL_SIGNALS:
        return DECISIVE
    if _ledger.is_waiting(entry):
        # Someone else owes this row's deliverable: nothing the user sends
        # finishes it, linked or not.
        return PROPOSAL
    if signal in LINKED_SIGNALS:
        return DECISIVE
    if kept_open_at(entry):
        # The user judged this row open after evidence of this kind looked
        # like an answer; their word or a send that names the row decides.
        return PROPOSAL
    if signal in REPLY_SIGNALS:
        return DECISIVE if _ledger.closes_on_reply(entry) else PROPOSAL
    if signal in ARCHIVE_SIGNALS:
        return DECISIVE if _ledger.is_detector_row(entry.get("id") or "") else PROPOSAL
    how = _ledger.closes_on(entry)
    if signal in LAPSE_SIGNALS:
        if not _ledger.lapses(entry):
            return PROPOSAL
        brief = entry.get("thread_brief")
        wrote = isinstance(brief, dict) and bool(brief.get("last_from_user"))
        intake = str(entry.get("id") or "").startswith("deadline-")
        return PROPOSAL if intake and wrote else DECISIVE
    if how == "submission" and signal in SUBMISSION_SIGNALS:
        return DECISIVE
    if how == "event" and signal in EVENT_SIGNALS:
        return DECISIVE
    return PROPOSAL


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(EVENTS_SCHEMA)


def record_event(conn: sqlite3.Connection, entry_id: str, kind: str, *,
                 tier: str = "", signal: str = "", actor: str = "",
                 evidence=None, note: str = "", ts: str = "") -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO ledger_events (ts, entry_id, kind, tier, signal, actor, evidence, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or _now(), entry_id, kind, tier, signal, actor,
         json.dumps(evidence, sort_keys=True, default=str) if evidence not in (None, "") else "",
         (note or "")[:2000]))


def _meta(row) -> dict:
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _write_meta(conn, entry_id: str, meta: dict) -> None:
    conn.execute("UPDATE ledger SET metadata = ? WHERE id = ?",
                 (json.dumps(meta, sort_keys=True, default=str), entry_id))


def kept_open_at(entry: dict) -> str:
    """When the user last kept this row open ("Still open", a reopen), or ""."""
    vetoes = entry.get("close_vetoes")
    return str(vetoes.get(KEPT_OPEN) or "") if isinstance(vetoes, dict) else ""


def vetoed(entry: dict, signal: str, evidence_ts: str = "", now: datetime | None = None) -> bool:
    """True when the user has refused this evidence for this row: "Still
    open" on a proposal or a reopen after a close, with the evidence no
    newer than that refusal. The refusal covers the signal it named and,
    through KEPT_OPEN, every other signal whose evidence is as old. Newer
    evidence (a later reply, a later archive) is new information and is
    heard. A model signal has no evidence clock; a refusal silences it for
    LLM_VETO_DAYS."""
    vetoes = entry.get("close_vetoes") or {}
    if not isinstance(vetoes, dict):
        return False
    family = signal.split(":", 1)[0]
    now = now or datetime.now(timezone.utc)
    ev = _parse(evidence_ts)
    at = vetoes.get(signal) or (vetoes.get(family) if family == "llm" else None)
    if at and _refuses(at, family, ev, now, same_signal=True):
        return True
    kept = vetoes.get(KEPT_OPEN)
    return bool(kept) and _refuses(kept, family, ev, now, same_signal=False)


def _refuses(at, family: str, ev: datetime | None, now: datetime, *, same_signal: bool) -> bool:
    at_dt = _parse(at)
    if at_dt is None:
        return True
    if family == "llm":
        return now - at_dt < timedelta(days=LLM_VETO_DAYS)
    if ev is None:
        # No evidence clock to compare: the refused signal stays refused,
        # and another signal is heard (it proposes, see tier()).
        return same_signal
    return ev <= at_dt


def _veto(meta: dict, signal: str, at: str) -> None:
    vetoes = meta.get("close_vetoes") if isinstance(meta.get("close_vetoes"), dict) else {}
    vetoes[signal] = at
    if signal.startswith("llm:"):
        vetoes["llm"] = at
    meta["close_vetoes"] = vetoes


# Signals whose evidence answers the row's whole thread up to that moment,
# so the detector rows of earlier mail on it close too: the user's word, a
# reply in the thread itself. A duplicate, an archive of one mail, a linked
# send from another thread or a scheduled send answer only their row.
SWEEP_SIGNALS = USER_SIGNALS | REPLY_SIGNALS | {"user_confirmed"}


def _close_row(conn, row, *, signal: str, actor: str, note: str, evidence,
               evidence_ts: str, tier_: str, sweep: bool | None = None) -> list[str]:
    """Write the close and its event; sweep the detector siblings the same
    evidence answers. Returns the ids closed (the row first)."""
    now = _now()
    meta = _meta(row)
    meta["closed_by"] = {"signal": signal, "actor": actor, "tier": tier_, "at": now}
    meta.pop("close_proposal", None)
    conn.execute(
        "UPDATE ledger SET status = 'completed', closed_at = ?, last_action = ?, "
        "updated_at = ?, metadata = ? WHERE id = ?",
        (now, note, now, json.dumps(meta, sort_keys=True, default=str), row["id"]))
    record_event(conn, row["id"], "close", tier=tier_, signal=signal, actor=actor,
                 evidence=evidence, note=note, ts=now)
    closed = [row["id"]]
    if sweep if sweep is not None else signal in SWEEP_SIGNALS:
        closed += _sweep_siblings(conn, row, note=note, actor=actor,
                                  evidence_ts=evidence_ts or now, now=now)
    return closed


def _sweep_siblings(conn, row, *, note: str, actor: str, evidence_ts: str,
                    now: str) -> list[str]:
    """Close the open detector rows on the same conversation, within the
    closer's own source, whose mail came no later than the evidence: a reply
    or a statement answers the asks that came before it, and a mail that
    arrived after it is a new ask."""
    conv = (row["conversation_id"] or "").strip()
    if not conv:
        return []
    keys = row.keys()
    source = row["source"] if "source" in keys else None
    pred, args = ("AND source = ?", (source,)) if source else ("AND source IS NULL", ())
    sibs = conn.execute(
        f"SELECT * FROM ledger WHERE conversation_id = ? AND id != ? "
        f"AND status IN ('open', 'snoozed', 'follow_up') {pred}",
        (conv, row["id"], *args)).fetchall()
    cutoff = _parse(evidence_ts)
    out = []
    for sib in sibs:
        if not source and not _ledger.is_detector_row(sib["id"]):
            continue
        born = _parse(sib["date_identified"] or sib["created_at"])
        if cutoff is not None and born is not None and born > cutoff + timedelta(minutes=2):
            continue
        meta = _meta(sib)
        meta["closed_by"] = {"signal": "detector_sibling", "actor": actor,
                             "tier": DECISIVE, "at": now, "with": row["id"]}
        meta.pop("close_proposal", None)
        sib_note = f"co-closed with {row['id']}: {note}"
        conn.execute(
            "UPDATE ledger SET status = 'completed', closed_at = ?, last_action = ?, "
            "email_archived = 1, updated_at = ?, metadata = ? WHERE id = ?",
            (now, sib_note, now, json.dumps(meta, sort_keys=True, default=str), sib["id"]))
        record_event(conn, sib["id"], "close", tier=DECISIVE, signal="detector_sibling",
                     actor=actor, evidence={"with": row["id"]}, note=sib_note, ts=now)
        out.append(sib["id"])
    return out


def settle(conn: sqlite3.Connection, entry_id: str, *, signal: str, actor: str,
           note: str, evidence=None, evidence_ts: str = "",
           sweep: bool | None = None, why: str = "") -> dict:
    """Apply the policy to one piece of evidence that `entry_id` is done.

    `note` is the closing note; `why` is the proposal's line for the user
    (default: the note). Returns {"action": "closed", "ids": [...]} on
    decisive evidence, {"action": "proposed"} otherwise, or {"action":
    "vetoed" | "skipped", "why": ...}. The caller commits."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return {"action": "skipped", "why": "no such row"}
    if row["status"] not in _ledger.OPEN_STATUSES:
        return {"action": "skipped", "why": f"already {row['status']}"}
    entry = _ledger._row_to_dict(row)
    if (signal not in USER_SIGNALS and not is_plugin_row(entry)
            and vetoed(entry, signal, evidence_ts)):
        return {"action": "vetoed", "why": f"the user kept this row open against {signal}"}
    t = tier(signal, entry)
    if t == DECISIVE:
        return {"action": "closed", "tier": t, "signal": signal,
                "ids": _close_row(conn, row, signal=signal, actor=actor, note=note,
                                  evidence=evidence, evidence_ts=evidence_ts, tier_=t,
                                  sweep=sweep)}
    return propose(conn, entry_id, signal=signal, actor=actor, note=why or note,
                   evidence=evidence, evidence_ts=evidence_ts, _row=row)


def propose(conn: sqlite3.Connection, entry_id: str, *, signal: str, actor: str,
            note: str, evidence=None, evidence_ts: str = "", _row=None) -> dict:
    """Mark an open row as looking done. Its status does not change.
    Evidence the user already refused for this row (vetoed()) proposes
    nothing."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = _row or conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    if row is None or row["status"] not in _ledger.OPEN_STATUSES:
        return {"action": "skipped", "why": "not an open row"}
    entry = _ledger._row_to_dict(row)
    if not is_plugin_row(entry) and vetoed(entry, signal, evidence_ts):
        return {"action": "vetoed", "why": f"the user kept this row open against {signal}"}
    meta = _meta(row)
    current = meta.get("close_proposal") if isinstance(meta.get("close_proposal"), dict) else {}
    signals = list(current.get("signals") or [])
    if signal not in signals:
        signals.append(signal)
    meta["close_proposal"] = {
        "signals": signals, "signal": signal, "actor": actor,
        "why": (note or "")[:400], "evidence": evidence, "evidence_ts": evidence_ts or "",
        "first_at": current.get("first_at") or _now(), "at": _now(),
        "carded": current.get("carded", ""),
    }
    _write_meta(conn, row["id"], meta)
    if signal not in (current.get("signals") or []):
        record_event(conn, row["id"], "propose", tier=PROPOSAL, signal=signal,
                     actor=actor, evidence=evidence, note=note)
    return {"action": "proposed", "tier": PROPOSAL, "signal": signal, "ids": [row["id"]]}


def confirm(conn: sqlite3.Connection, entry_id: str, *, actor: str = "user") -> dict:
    """The user agreed the row is done: close it on their word."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    if row is None or row["status"] not in _ledger.OPEN_STATUSES:
        return {"action": "skipped", "why": "not an open row"}
    prop = _meta(row).get("close_proposal") or {}
    why = prop.get("why") or "looked done"
    record_event(conn, row["id"], "confirm", tier=DECISIVE, signal=prop.get("signal") or "",
                 actor=actor, evidence=prop.get("evidence"), note=why)
    return {"action": "closed", "tier": DECISIVE, "signal": "user_confirmed",
            "ids": _close_row(conn, row, signal="user_confirmed", actor=actor,
                              note=f"Confirmed done in chat: {why}"[:600],
                              evidence={"proposal": prop}, evidence_ts="", tier_=DECISIVE)}


def reject(conn: sqlite3.Connection, entry_id: str, *, actor: str = "user") -> dict:
    """The user says the row is still open: drop the proposal, and refuse
    its signals, and any other evidence no newer than now, for this row."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return {"action": "skipped", "why": "no such row"}
    meta = _meta(row)
    prop = meta.pop("close_proposal", None) or {}
    now = _now()
    for sig in prop.get("signals") or ([prop["signal"]] if prop.get("signal") else []):
        _veto(meta, sig, now)
    _veto(meta, KEPT_OPEN, now)
    _write_meta(conn, row["id"], meta)
    record_event(conn, row["id"], "reject", tier=PROPOSAL, signal=prop.get("signal") or "",
                 actor=actor, evidence={"signals": prop.get("signals")}, note=prop.get("why") or "")
    return {"action": "kept_open", "ids": [row["id"]], "vetoed": prop.get("signals") or []}


_LEGACY_NOTES = (
    ("email archived from inbox", "user_archived"),
    ("user archived conversation email", "user_archived"),
    ("Email sent via EA Send button", "send_reply_row"),
    ("User sent from Outlook directly", "user_sent_myself"),
    ("auto-closed by briefing", "llm:morning-briefing"),
)


def infer_signal(note: str) -> str:
    """The signal a close written before closed_by existed came from, read
    off its note, or ""."""
    note = note or ""
    marker = "closure_scrubber: "
    if marker in note:
        return note.split(marker, 1)[1].split(" ", 1)[0].split("(", 1)[0].strip(":, ")
    for text, signal in _LEGACY_NOTES:
        if text in note:
            return signal
    return ""


def note_reopen(conn: sqlite3.Connection, row, *, actor: str = "", meta: dict | None = None) -> dict:
    """A closed row is open again: the evidence that closed it was wrong.
    Records the event and vetoes the closing signal, and any other evidence
    no newer than now, for this row. Returns the metadata to write (the
    caller owns the row update)."""
    meta = dict(meta if meta is not None else _meta(row))
    closed_by = meta.get("closed_by") if isinstance(meta.get("closed_by"), dict) else {}
    signal = closed_by.get("signal") or infer_signal(row["last_action"] or "")
    now = _now()
    if signal and signal not in USER_SIGNALS:
        _veto(meta, signal, now)
    _veto(meta, KEPT_OPEN, now)
    meta["reopened_at"] = now
    record_event(conn, row["id"], "reopen", signal=signal, actor=actor,
                 evidence={"closed_by": closed_by}, note=(row["last_action"] or "")[:400], ts=now)
    return meta


def pending_proposals(conn: sqlite3.Connection, *, include_plugins: bool = False) -> list[dict]:
    """Open rows that carry a close proposal, oldest proposal first."""
    conn.row_factory = sqlite3.Row
    src = "" if include_plugins else " AND source IS NULL"
    rows = conn.execute(
        "SELECT * FROM ledger WHERE status IN ('open', 'snoozed', 'follow_up') "
        "AND metadata LIKE '%close_proposal%'" + src).fetchall()
    out = []
    for r in rows:
        d = _ledger._row_to_dict(r)
        if isinstance(d.get("close_proposal"), dict):
            out.append(d)
    out.sort(key=lambda d: d["close_proposal"].get("first_at") or "")
    return out


def mark_carded(conn: sqlite3.Connection, entry_ids, at: str = "") -> None:
    """Note that these proposals are on a card, so the next card skips them."""
    conn.row_factory = sqlite3.Row
    for eid in entry_ids:
        row = conn.execute("SELECT * FROM ledger WHERE id = ?", (eid,)).fetchone()
        if row is None:
            continue
        meta = _meta(row)
        if isinstance(meta.get("close_proposal"), dict):
            meta["close_proposal"]["carded"] = at or _now()
            _write_meta(conn, eid, meta)


def report(conn: sqlite3.Connection, since: str = "") -> dict:
    """Per signal since `since`: decisive closes, those later reopened
    (false closes), proposals, confirmed, rejected and still pending; and
    the closes the user had to state (missed by every signal)."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    evs = conn.execute(
        "SELECT * FROM ledger_events WHERE ts >= ? ORDER BY seq", (since or "",)).fetchall()
    by: dict[str, dict] = {}
    last_close: dict[str, str] = {}
    last_prop: dict[str, str] = {}

    def slot(sig: str) -> dict:
        return by.setdefault(sig or "?", {"closed": 0, "reopened": 0, "proposed": 0,
                                          "confirmed": 0, "rejected": 0})
    for e in evs:
        k, sig, eid = e["kind"], e["signal"] or "", e["entry_id"]
        if k == "close" and sig != "user_confirmed":
            slot(sig)["closed"] += 1
            last_close[eid] = sig
        elif k == "reopen" and eid in last_close:
            slot(last_close.pop(eid))["reopened"] += 1
        elif k == "propose":
            slot(sig)["proposed"] += 1
            last_prop[eid] = sig
        elif k == "confirm":
            slot(sig)["confirmed"] += 1
        elif k == "reject":
            slot(sig)["rejected"] += 1
    pending = len(pending_proposals(conn))
    stated = by.get("user_stated", {}).get("closed", 0)
    decisive_closes = sum(v["closed"] for s, v in by.items() if s not in USER_SIGNALS)
    false_closes = sum(v["reopened"] for v in by.values())
    return {"since": since, "signals": by, "pending_proposals": pending,
            "user_stated_closes": stated, "evidence_closes": decisive_closes,
            "false_closes": false_closes}
