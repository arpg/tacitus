#!/usr/bin/env python3
"""The closure hill climb: whole signals move between closing and proposing
by the owner's record with them.

closure.py decides what one piece of evidence may do to one row: decisive
evidence closes, the rest proposes, and the owner settles a proposal with
one click. This module learns from those clicks which signals may move:

  promoted  a signal that proposes, whose proposals the owner confirmed:
            at least PROMOTE_MIN decisions, precision at or above
            PROMOTE_PRECISION over the last WINDOW, and none kept open in
            the last PROMOTE_CLEAN. Its proposals close at once instead
            (closure tier `promoted`), do-and-tell: the bot archives the
            mail and posts one line in the DM with a Reopen button.
  demoted   a signal that closes at once whose closes the owner reopened:
            precision under DEMOTE_PRECISION over the last WINDOW, or a
            reopen in the last DEMOTE_REOPEN. A promoted signal goes back
            to proposing; a decisive one proposes until its proposals earn
            it back by the promotion test. A reopen moves the signal at
            once (demote_now); the nightly run keeps the numbers.

A decision (decisions()) is one verdict of the owner's on one signal,
read from `ledger_events`:

  confirm   a proposal the owner confirmed (the card, the web chat's Mark
            done, or the owner's word while the proposal stood); every
            signal on the proposal is credited.
  reject    a proposal the owner kept open (Still open, Needs me).
  close     a close at once: right once it stood SETTLE_DAYS, wrong when
            the owner reopened it. A new message on the row's thread after
            the close (a renewal, closure.note_renew) is a new ask, and the
            close stood.

Each decision belongs to the population of rows it was made on. Rows where
the signal proposes by closure.tier hold its `proposal` decisions and, once
it is promoted, its `promoted` closes; rows where it closes hold its
`decisive` closes and, once it is demoted, its `demoted` proposals. A move
reads one population: promotion its proposals and promoted closes, the way
back from a demotion its decisive closes and demoted proposals, a demotion
the closes made at once since the state began.

Never in the loop (NEVER): the owner's word or click, a standing rule, a
duplicate, a detector sibling, a send that names the row (--ledger-id) and
the Send button's close of a reply row. A model signal is in the loop only
when an owner process makes it (LLM_LOOP). closure.hill_move never
promotes a resident's request, a row the owner kept open or a row that
waits on someone else.

The policy file, <EA_DIR>/state/closure-policy.json, holds each signal's
state, the numbers that justified it and the last changes. The nightly
`closure_labels.py collect --report` runs update() after the closure eval,
and each change posts one plain line to the health channel. closure.settle
reads state(): a missing file, `closure.hill_climb` false in
ea-config.json, or an install without chat buttons (no Reopen) reads as no
promotion; `freeze` keeps every state as it is.

    closure_policy.py show [--json]
    closure_policy.py update [--dry-run] [--no-post]
    closure_policy.py freeze [--off]
    closure_policy.py reset
    closure_policy.py backtest [--days 30] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import closure as _closure
    import ledger as _ledger
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import closure as _closure
    from preprocessor import ledger as _ledger
    from preprocessor import resident_io as _rio

POLICY_FILE = "closure-policy.json"
VERSION = 1

PROMOTE_MIN = 15
PROMOTE_PRECISION = 0.92
PROMOTE_CLEAN = 10
DEMOTE_PRECISION = 0.85
DEMOTE_REOPEN = 5
WINDOW = 30
# An at-once close the owner left alone this long stood.
SETTLE_DAYS = 2
# How far back the nightly run reads the events.
LOOKBACK_DAYS = 120
# How long a process keeps the file it read.
CACHE_S = 300
CHANGES_KEEP = 50
BACKTEST_DAYS = 30

PROMOTED = _closure.PROMOTED
DEMOTED = _closure.DEMOTED
STATES = (PROMOTED, DEMOTED)
# The owner's own acts and the bookkeeping that loses no obligation: they
# close, always, and the loop never reads them.
NEVER = frozenset(_closure.USER_SIGNALS | _closure.LINKED_SIGNALS
                  | _closure.STRUCTURAL_SIGNALS | {"user_confirmed", "send_reply_row"})
# Model signals an owner process makes. A resident lane's model (llm:chat,
# llm:<cron lane>) proposes, always.
LLM_LOOP = frozenset({"llm:reconcile", "llm:reconcile-lapsed", "llm:reconcile-duplicate",
                      "llm:open-claims-audit", "llm:mail-tick"})
_SIGNAL_RE = re.compile(r"[a-z][a-z0-9_:+.-]{0,60}")
# The populations: rows where the signal proposes by closure.tier, and rows
# where it closes.
PROPOSING = ("proposal", PROMOTED)
CLOSING = ("decisive", DEMOTED)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def in_loop(signal: str) -> bool:
    """Whether the hill climb may move this signal."""
    s = str(signal or "")
    if not s or s in NEVER or not _SIGNAL_RE.fullmatch(s):
        return False
    return s in LLM_LOOP if s.startswith("llm:") else True


# ── the decisions ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """One verdict of the owner's on one signal. `at` orders the
    decisions (the verdict for a proposal, the close for a close); `known`
    is when the verdict could be read (a close stands SETTLE_DAYS after it
    was made); `acted` is when the signal acted on the row (the first
    proposal, or the close): the backtest's clock."""
    signal: str
    track: str
    right: bool
    at: str
    known: str
    acted: str
    entry: str


def _evidence(raw) -> dict:
    try:
        ev = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return ev if isinstance(ev, dict) else {}


def decisions(conn: sqlite3.Connection, *, since: str = "", now: datetime | None = None,
              plugin_ids: set | None = None) -> list[Decision]:
    """Every decision the events since `since` hold, oldest first. A close
    made less than SETTLE_DAYS before `now` with no reopen has no verdict
    yet and is left out. Rows of a plugin are left out."""
    now = now or _now_dt()
    conn.row_factory = sqlite3.Row
    if plugin_ids is None:
        plugin_ids = {r[0] for r in conn.execute(
            "SELECT id FROM ledger WHERE source LIKE 'plugin:%'")}
    try:
        evs = conn.execute("SELECT seq, ts, entry_id, kind, tier, signal, actor, evidence "
                           "FROM ledger_events WHERE ts >= ? ORDER BY seq",
                           (since or "",)).fetchall()
    except sqlite3.OperationalError:
        return []
    proposals: dict[str, dict[str, tuple[str, str]]] = {}
    closes: dict[str, tuple[str, str, str]] = {}
    out: list[Decision] = []
    for e in evs:
        eid, kind, sig, ts = e["entry_id"], e["kind"], str(e["signal"] or ""), str(e["ts"])
        if eid in plugin_ids:
            continue
        if kind == "propose":
            if in_loop(sig):
                track = DEMOTED if e["tier"] == DEMOTED else "proposal"
                proposals.setdefault(eid, {}).setdefault(sig, (track, ts))
        elif kind in ("confirm", "reject"):
            right = kind == "confirm"
            held = proposals.pop(eid, {})
            named = _evidence(e["evidence"]).get("signals") if not right else None
            for s in set(held) | {x for x in (named or [sig]) if isinstance(x, str)}:
                if in_loop(s):
                    track, first = held.get(s, ("proposal", ts))
                    out.append(Decision(s, track, right, ts, ts, first, eid))
        elif kind == "close":
            held = proposals.pop(eid, {})
            if sig in _closure.USER_SIGNALS:
                # The owner closed the row in words while it looked done:
                # the proposal was right.
                for s, (track, first) in held.items():
                    out.append(Decision(s, track, True, ts, ts, first, eid))
            elif in_loop(sig) and e["tier"] in (_closure.DECISIVE, PROMOTED):
                closes[eid] = (sig, PROMOTED if e["tier"] == PROMOTED else "decisive", ts)
        elif kind in ("reopen", "renew"):
            pending = closes.pop(eid, None)
            if pending is None:
                continue
            s, track, at = pending
            if _closure.is_renewal(kind, e["actor"]):
                out.append(Decision(s, track, True, at, ts, at, eid))
            else:
                out.append(Decision(s, track, False, ts, ts, at, eid))
    cutoff = now - timedelta(days=SETTLE_DAYS)
    for eid, (s, track, at) in closes.items():
        dt = _parse(at)
        if dt is not None and dt <= cutoff:
            out.append(Decision(s, track, True, at, _iso(dt + timedelta(days=SETTLE_DAYS)), at, eid))
    out.sort(key=lambda d: (d.at, d.entry))
    return out


# ── the moves ─────────────────────────────────────────────────────────

def _numbers(ds: list[Decision]) -> dict:
    last = ds[-WINDOW:]
    n = len(last)
    right = sum(1 for d in last if d.right)
    return {"n": n, "right": right, "precision": round(right / n, 3) if n else None,
            "wrong_last": sum(1 for d in last[-PROMOTE_CLEAN:] if not d.right)}


def _earns(ds: list[Decision]) -> bool:
    last = ds[-WINDOW:]
    if len(last) < PROMOTE_MIN:
        return False
    p = sum(1 for d in last if d.right) / len(last)
    return p >= PROMOTE_PRECISION and all(d.right for d in last[-PROMOTE_CLEAN:])


def _fails(ds: list[Decision]) -> bool:
    last = ds[-WINDOW:]
    if not last:
        return False
    p = sum(1 for d in last if d.right) / len(last)
    return p < DEMOTE_PRECISION or any(not d.right for d in last[-DEMOTE_REOPEN:])


def _say(n: dict, what: str) -> str:
    if not n.get("n"):
        return f"no {what} yet"
    return f"{n['right']} of {n['n']} {what} right"


def judge(ds: list[Decision], prev: dict | None, now: str) -> dict:
    """The signal's next state from its decisions (oldest first) and its
    previous record ({"state", "since"}). Returns {"state", "since",
    "why", "numbers"}; the state is "" when the signal does what
    closure.tier says."""
    prev = prev or {}
    state = prev.get("state") if prev.get("state") in STATES else ""
    since = str(prev.get("since") or "")
    pop = {k: [d for d in ds if d.track in k] for k in (PROPOSING, CLOSING)}
    after = [d for d in ds if d.at > since] if since else list(ds)
    numbers = {"proposing": _numbers(pop[PROPOSING]), "closing": _numbers(pop[CLOSING]),
               "at_once": _numbers([d for d in after if d.track in ("decisive", PROMOTED)])}
    keep = {"state": state, "since": since, "numbers": numbers,
            "why": str(prev.get("why") or "")}

    def move(to: str, why: str) -> dict:
        return {"state": to, "since": now, "numbers": numbers, "why": why}
    if state == DEMOTED:
        if _earns(pop[CLOSING]):
            return move("", _say(numbers["closing"], "decisions") + "; it closes at once again")
        return keep
    decisive_after = [d for d in after if d.track == "decisive"]
    if _fails(decisive_after):
        return move(DEMOTED, _say(_numbers(decisive_after), "closes")
                    + "; a close was reopened, so it proposes now")
    if state == PROMOTED:
        promoted_after = [d for d in after if d.track == PROMOTED]
        if _fails(promoted_after):
            return move("", _say(_numbers(promoted_after), "closes")
                        + "; a close was reopened, so it proposes again")
        return keep
    if any(d.track == "proposal" for d in pop[PROPOSING]) and _earns(pop[PROPOSING]):
        return move(PROMOTED, _say(numbers["proposing"], "proposals")
                    + f"; none kept open in the last {PROMOTE_CLEAN}")
    return keep


def step(policy: dict, ds: list[Decision], now: str) -> tuple[dict, list[dict]]:
    """The policy after one nightly run over the decisions known at `now`:
    (the new signals block, the changes). A frozen policy keeps every
    state and records the numbers only."""
    by: dict[str, list[Decision]] = defaultdict(list)
    for d in ds:
        by[d.signal].append(d)
    old = policy.get("signals") if isinstance(policy.get("signals"), dict) else {}
    frozen = bool(policy.get("frozen"))
    out: dict[str, dict] = {}
    changes: list[dict] = []
    for sig in sorted(set(by) | {s for s in old if in_loop(s)}):
        prev = old.get(sig) if isinstance(old.get(sig), dict) else {}
        rec = judge(by.get(sig, []), prev, now)
        if frozen and rec["state"] != (prev.get("state") or ""):
            rec = {**rec, "state": prev.get("state") or "", "since": prev.get("since") or "",
                   "why": prev.get("why") or ""}
        if rec["state"] != (prev.get("state") or ""):
            changes.append({"at": now, "signal": sig, "from": prev.get("state") or "",
                            "to": rec["state"], "why": rec["why"]})
        out[sig] = rec
    return out, changes


# ── the file ──────────────────────────────────────────────────────────

def _state_dir() -> Path:
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "state"


def policy_path() -> Path:
    return _state_dir() / POLICY_FILE


def ledger_path() -> Path:
    """The ledger this process's EA_DIR names (ledger.DB_PATH at call time)."""
    return _state_dir() / _ledger.DB_PATH.name


_CACHE: dict = {"at": -1e18, "path": "", "data": {}, "on": True, "buttons": True}


def _clean(data) -> dict:
    """The policy as the readers take it: a dict whose signals block holds
    only loop signals with a known state."""
    if not isinstance(data, dict):
        return {}
    sigs = data.get("signals") if isinstance(data.get("signals"), dict) else {}
    data = dict(data)
    data["signals"] = {s: r for s, r in sigs.items()
                       if in_loop(s) and isinstance(r, dict) and r.get("state", "") in ("",) + STATES}
    if not isinstance(data.get("changes"), list):
        data["changes"] = []
    return data


def load(path: Path | None = None) -> dict:
    """The policy file, or {} when it is missing or unreadable. The file is
    in the resident's space: an owner process reads it with the resident's
    access (resident_io)."""
    p = path or policy_path()
    try:
        text = _rio.read_text(p)
        return _clean(json.loads(text)) if text else {}
    except (ValueError, TypeError):
        return {}


def _cached() -> dict:
    """The file, the switch and the buttons, read once per CACHE_S."""
    p = str(policy_path())
    now = time.monotonic()
    if _CACHE["path"] != p or now - _CACHE["at"] > CACHE_S:
        _CACHE.update(at=now, path=p, on=enabled(), buttons=_buttons(),
                      data=load(Path(p)))
    return _CACHE


def forget() -> None:
    """Drop the cached file (tests; a write in this process)."""
    _CACHE.update(at=-1e18, path="", data={})


def enabled() -> bool:
    """The switch: `closure.hill_climb` in ea-config.json; only a literal
    false turns it off."""
    try:
        try:
            import ea_config
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import ea_config
        return ea_config.closure_hill_climb_enabled()
    except Exception:
        return True


def _buttons() -> bool:
    """Whether the install has chat buttons, so a promoted close's note
    carries its Reopen."""
    try:
        try:
            import ea_config
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import ea_config
        return bool(ea_config.profile_features().get("buttons", True))
    except Exception:
        return True


def state(signal: str) -> str:
    """PROMOTED, DEMOTED or "" for this signal now (closure.hill_move)."""
    if not in_loop(signal):
        return ""
    got = _cached()
    if not got["on"]:
        return ""
    rec = (got["data"].get("signals") or {}).get(signal) or {}
    st = rec.get("state") if isinstance(rec, dict) else ""
    if st == PROMOTED and not got["buttons"]:
        return ""
    return st if st in STATES else ""


def _render(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=True) + "\n"


def _write(fn, path: Path | None = None) -> bool:
    """Read-modify-write the file under its lock with the resident's
    access; fn(old policy) returns the new one."""
    p = path or policy_path()

    def apply(old: str) -> str:
        try:
            cur = _clean(json.loads(old)) if old.strip() else {}
        except ValueError:
            cur = {}
        return _render(fn(cur))
    ok = _rio.update_text(p, apply, mode=0o664)
    forget()
    return ok


def _base(data: dict) -> dict:
    return {"version": VERSION, "frozen": bool(data.get("frozen")),
            "signals": data.get("signals") or {}, "changes": data.get("changes") or [],
            "updated_at": data.get("updated_at") or "",
            "renewals_repaired": data.get("renewals_repaired") or "",
            "thresholds": thresholds()}


def thresholds() -> dict:
    return {"promote_min": PROMOTE_MIN, "promote_precision": PROMOTE_PRECISION,
            "promote_clean": PROMOTE_CLEAN, "demote_precision": DEMOTE_PRECISION,
            "demote_reopen": DEMOTE_REOPEN, "window": WINDOW, "settle_days": SETTLE_DAYS}


def demote_now(signal: str, how: str, *, entry_id: str = "") -> bool:
    """The owner reopened a close made at once: the signal proposes from
    now (a promoted one again, a decisive one until it earns its way
    back). The nightly run posts the change. False when nothing moved.
    With no policy file yet the hill climb has not started, and the nightly
    run reads the reopen from the events."""
    if not in_loop(signal) or not enabled() or how not in (_closure.DECISIVE, PROMOTED):
        return False
    if not load():
        return False
    now = _iso(_now_dt())
    moved: list[bool] = []

    def fn(cur: dict) -> dict:
        data = _base(cur)
        if data["frozen"]:
            return data
        rec = dict(data["signals"].get(signal) or {})
        old = rec.get("state") or ""
        new = "" if old == PROMOTED else (DEMOTED if old == "" and how == _closure.DECISIVE
                                          else old)
        if new == old:
            return data
        why = "the owner reopened a close it made, so it proposes " + (
            "again" if old == PROMOTED else "now")
        rec.update(state=new, since=now, why=why)
        data["signals"][signal] = rec
        data["changes"] = (data["changes"] + [{"at": now, "signal": signal, "from": old,
                                               "to": new, "why": why, "posted": False}]
                           )[-CHANGES_KEEP:]
        moved.append(True)
        return data
    _write(fn)
    return bool(moved)


def _health_line(ch: dict) -> str:
    sig, to = ch.get("signal"), ch.get("to") or ""
    if to == PROMOTED:
        head = f"Closure policy: {sig} now closes at once. You get a note with a Reopen button."
    elif to == DEMOTED:
        head = f"Closure policy: {sig} now proposes. You confirm each one on a card."
    elif ch.get("from") == PROMOTED:
        head = f"Closure policy: {sig} proposes again. You confirm each one on a card."
    else:
        head = f"Closure policy: {sig} closes at once again."
    why = str(ch.get("why") or "").rstrip(".")
    return f"{head} Why: {why}." if why else head


def _post(lines: list[str]) -> bool:
    try:
        try:
            import chat_notify
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import chat_notify
        return bool(chat_notify.post(chat_notify.health_channel_key(), "\n".join(lines)))
    except Exception as e:
        print(f"closure_policy: the health post failed: {e}", file=sys.stderr)
        return False


def update(*, db_path: Path | None = None, now: datetime | None = None, post: bool = True,
           dry_run: bool = False) -> dict:
    """The nightly run: read the decisions, move the signals, write the
    file, post each change not posted yet (a reopen's demote_now too) to
    the health channel. Rows that intake renewed before note_renew get
    their vetoes repaired once (closure.repair_renewals)."""
    if not enabled():
        return {"skipped": "closure.hill_climb is off"}
    now = now or _now_dt()
    stamp = _iso(now)
    path = Path(db_path or ledger_path())
    if not path.exists():
        return {"skipped": "no ledger"}
    conn = _ledger.guarded_connect(path, read_only=True)
    try:
        ds = decisions(conn, since=_iso(now - timedelta(days=LOOKBACK_DAYS)), now=now)
    finally:
        conn.close()
    known = [d for d in ds if d.known <= stamp]
    result: dict = {"decisions": len(known)}

    def fn(cur: dict) -> dict:
        data = _base(cur)
        signals, changes = step(data, known, stamp)
        data["signals"] = signals
        data["changes"] = (data["changes"] + [{**c, "posted": False} for c in changes]
                           )[-CHANGES_KEEP:]
        data["updated_at"] = stamp
        result["changes"] = changes
        result["signals"] = {s: r["state"] for s, r in signals.items() if r["state"]}
        return data
    if dry_run:
        fn(load())
        return result
    if not load().get("renewals_repaired"):
        conn = _ledger.guarded_connect(path)
        try:
            fixed = _closure.repair_renewals(conn)
            conn.commit()
        finally:
            conn.close()
        result["renewals_repaired"] = len(fixed)
    _write(lambda cur: {**fn(cur), "renewals_repaired": cur.get("renewals_repaired") or stamp})
    if result.get("changes") is None:
        result["changes"] = []
    pending = [c for c in load().get("changes") or [] if not c.get("posted")]
    if pending and post:
        if _post([_health_line(c) for c in pending]):
            keys = {(c.get("at"), c.get("signal"), c.get("to")) for c in pending}

            def mark(cur: dict) -> dict:
                data = _base(cur)
                for c in data["changes"]:
                    if (c.get("at"), c.get("signal"), c.get("to")) in keys:
                        c["posted"] = True
                return data
            _write(mark)
            result["posted"] = len(pending)
    return result


def freeze(on: bool = True) -> dict:
    """Keep every state as it is (on), or let the nightly run move them."""
    _write(lambda cur: {**_base(cur), "frozen": bool(on)})
    return {"frozen": bool(on)}


def reset() -> dict:
    """Every signal back to what closure.tier says; the history stays."""
    def fn(cur: dict) -> dict:
        data = _base(cur)
        now = _iso(_now_dt())
        for sig, rec in data["signals"].items():
            if rec.get("state"):
                data["changes"].append({"at": now, "signal": sig, "from": rec["state"], "to": "",
                                        "why": "the policy was reset", "posted": False})
        data["changes"] = data["changes"][-CHANGES_KEEP:]
        data["signals"] = {}
        return data
    _write(fn)
    return {"reset": True}


# ── the backtest ──────────────────────────────────────────────────────

def _nights(start: datetime, end: datetime) -> list[datetime]:
    first = start.replace(hour=8, minute=45, second=0, microsecond=0)
    if first < start:
        first += timedelta(days=1)
    out = []
    while first <= end:
        out.append(first)
        first += timedelta(days=1)
    return out


def replay(ds: list[Decision], *, start: datetime, end: datetime) -> dict:
    """The decisions since `start` replayed with the loop on: a nightly
    step (08:45 UTC, the 01:45 run) over the decisions known by then, a
    reopen's move at once, and each decision taken in the state its signal
    held when it acted on the row. Per signal: clicks saved (a promoted
    proposal the owner would have confirmed), wrong closes made (a
    promoted proposal the owner kept open), clicks added (a demoted close
    that stood) and wrong closes avoided (a demoted close the owner
    reopened)."""
    s0 = _iso(start)
    history = [d for d in ds if d.acted < s0]
    window = sorted((d for d in ds if s0 <= d.acted <= _iso(end)), key=lambda d: d.acted)
    seen: list[Decision] = list(history)
    states, changes = step({}, [d for d in seen if d.known < s0], s0)
    nights = _nights(start, end)
    moves: list[tuple[str, str, str]] = []  # (when, signal, state) from a reopen
    per: dict[str, dict] = defaultdict(lambda: {"decisions": 0, "clicks_saved": 0,
                                                "wrong_closes": 0, "clicks_added": 0,
                                                "wrong_closes_avoided": 0, "states": []})

    def set_state(sig: str, to: str, when: str, why: str) -> None:
        prev = states.get(sig) or {}
        if (prev.get("state") or "") == to:
            return
        states[sig] = {**prev, "state": to, "since": when, "why": why}
        changes.append({"at": when, "signal": sig, "from": prev.get("state") or "", "to": to,
                        "why": why})
    for d in window:
        while nights and _iso(nights[0]) <= d.acted:
            t = _iso(nights.pop(0))
            for when, sig, to in sorted(m for m in moves if m[0] <= t):
                set_state(sig, to, when, "a close was reopened")
            moves = [m for m in moves if m[0] > t]
            new, ch = step({"signals": states}, [x for x in seen if x.known <= t], t)
            states = new
            changes += ch
        for when, sig, to in sorted(m for m in moves if m[0] <= d.acted):
            set_state(sig, to, when, "a close was reopened")
        moves = [m for m in moves if m[0] > d.acted]
        st = (states.get(d.signal) or {}).get("state") or ""
        row = per[d.signal]
        row["decisions"] += 1
        nd = d
        if d.track == "proposal" and st == PROMOTED:
            nd = replace(d, track=PROMOTED)
            if d.right:
                row["clicks_saved"] += 1
            else:
                row["wrong_closes"] += 1
                moves.append((d.at, d.signal, ""))
        elif d.track == "decisive" and st == DEMOTED:
            nd = replace(d, track=DEMOTED)
            if d.right:
                row["clicks_added"] += 1
            else:
                row["wrong_closes_avoided"] += 1
        elif d.track == "decisive" and not d.right:
            moves.append((d.at, d.signal, DEMOTED))
        seen.append(nd)
    for c in changes:
        per[c["signal"]]["states"].append(f"{c['at'][:10]} {c['from'] or 'base'} -> "
                                          f"{c['to'] or 'base'}")
    totals = {k: sum(r[k] for r in per.values()) for k in
              ("decisions", "clicks_saved", "wrong_closes", "clicks_added",
               "wrong_closes_avoided")}
    return {"signals": {s: dict(r) for s, r in sorted(per.items())}, "totals": totals,
            "changes": changes,
            "final": {s: r.get("state") for s, r in states.items() if r.get("state")}}


def renewal_backtest(conn: sqlite3.Connection, *, since: str) -> dict:
    """What closing renewed rows at once would do: the proposals since
    `since` on a row a new message reopened (and the user never kept
    open), whose signal closes the same row at once without the renewal.
    A confirm would be a click saved, a "Still open" a wrong close. The
    numbers are why a renewed row proposes (closure.tier); the hill climb
    can still promote a signal the user confirms there."""
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: _ledger._row_to_dict(r) for r in conn.execute("SELECT * FROM ledger")}
    history: dict[str, list] = defaultdict(list)
    out = {"proposals": 0, "clicks_saved": 0, "wrong_closes": 0, "undecided": 0,
           "signals": defaultdict(int)}
    pending: dict[str, str] = {}
    for e in conn.execute("SELECT * FROM ledger_events ORDER BY seq"):
        eid, kind = e["entry_id"], e["kind"]
        if kind == "propose" and str(e["ts"]) >= since and eid not in pending:
            past = history[eid]
            intake = any(_closure.is_renewal(x["kind"], x["actor"]) for x in past)
            owner = any(x["kind"] == "reject" or (x["kind"] == "reopen"
                                                  and not _closure.is_renewal(x["kind"], x["actor"]))
                        for x in past)
            entry = dict(rows.get(eid) or {"id": eid})
            entry.pop("close_vetoes", None)
            if intake and not owner and not _closure.is_plugin_row(entry) \
                    and _closure.tier(str(e["signal"] or ""), entry) == _closure.DECISIVE:
                pending[eid] = str(e["signal"] or "")
                out["proposals"] += 1
                out["signals"][pending[eid]] += 1
        elif kind in ("confirm", "reject") and eid in pending:
            pending.pop(eid)
            out["clicks_saved" if kind == "confirm" else "wrong_closes"] += 1
        elif kind == "close" and eid in pending:
            pending.pop(eid)
            out["clicks_saved" if str(e["signal"] or "") in _closure.USER_SIGNALS
                else "undecided"] += 1
        history[eid].append(e)
    out["undecided"] += len(pending)
    out["signals"] = dict(out["signals"])
    return out


def backtest(*, days: int = BACKTEST_DAYS, db_path: Path | None = None,
             now: datetime | None = None) -> dict:
    now = now or _now_dt()
    start = now - timedelta(days=days)
    conn = _ledger.guarded_connect(db_path or ledger_path(), read_only=True)
    try:
        ds = decisions(conn, since=_iso(start - timedelta(days=LOOKBACK_DAYS)), now=now)
        renew = renewal_backtest(conn, since=_iso(start))
    finally:
        conn.close()
    out = replay(ds, start=start, end=now)
    out.update(days=days, thresholds=thresholds(), renewals=renew)
    return out


# ── the CLI ───────────────────────────────────────────────────────────

def _table(rows: list[tuple]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                               for i, (c, w) in enumerate(zip(r, widths))).rstrip()
                     for r in rows)


def render_backtest(rep: dict) -> str:
    head = ("signal", "decisions", "saved", "wrong", "added", "avoided", "moves")
    rows = [head] + [(s, r["decisions"], r["clicks_saved"], r["wrong_closes"], r["clicks_added"],
                      r["wrong_closes_avoided"], "; ".join(r["states"]) or "-")
                     for s, r in rep["signals"].items()]
    t = rep["totals"]
    rows.append(("total", t["decisions"], t["clicks_saved"], t["wrong_closes"],
                 t["clicks_added"], t["wrong_closes_avoided"], ""))
    rn = rep.get("renewals") or {}
    th = rep["thresholds"]
    return "\n".join([
        f"Closure hill climb, last {rep['days']} days (promote: {th['promote_min']}+ decisions, "
        f"precision >= {th['promote_precision']}, none kept open in the last "
        f"{th['promote_clean']}; demote: precision < {th['demote_precision']} or a reopen in "
        f"the last {th['demote_reopen']}).",
        "saved = clicks the owner would not have made; wrong = closes he would have reopened;",
        "added = clicks a demotion asks for; avoided = wrong closes a demotion holds back.", "",
        _table(rows), "",
        f"Renewed rows (a new message reopened a closed row): {rn.get('proposals', 0)} "
        f"proposals would close at once without the renewal; that would save "
        f"{rn.get('clicks_saved', 0)} clicks and make {rn.get('wrong_closes', 0)} wrong closes "
        f"({rn.get('undecided', 0)} undecided), so they stay proposals."])


def render_show(data: dict) -> str:
    if not data:
        return ("No closure policy yet: every signal does what closure.py says. "
                "The nightly run writes it.")
    lines = [f"Closure policy, updated {data.get('updated_at') or 'never'}"
             f"{' (frozen)' if data.get('frozen') else ''}"
             f"{'' if enabled() else ' (off: closure.hill_climb is false)'}."]
    rows = [("signal", "state", "since", "proposing", "closing", "why")]
    for sig, rec in sorted((data.get("signals") or {}).items()):
        n = rec.get("numbers") or {}

        def cell(k: str) -> str:
            x = n.get(k) or {}
            return f"{x.get('right', 0)}/{x.get('n', 0)}" if x.get("n") else "-"
        rows.append((sig, rec.get("state") or "base", (rec.get("since") or "-")[:10],
                     cell("proposing"), cell("closing"), rec.get("why") or ""))
    if len(rows) > 1:
        lines += ["", _table(rows)]
    ch = [c for c in data.get("changes") or []][-10:]
    if ch:
        lines += ["", "Last changes:"] + [f"  {c.get('at', '')[:16]} {c.get('signal')}: "
                                          f"{c.get('from') or 'base'} -> {c.get('to') or 'base'}"
                                          for c in ch]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="each signal's state and the numbers behind it",
                       allow_abbrev=False)
    s.add_argument("--json", action="store_true")
    u = sub.add_parser("update", help="the nightly run: move the signals, post the changes",
                       allow_abbrev=False)
    u.add_argument("--dry-run", action="store_true", help="compute and print; write nothing")
    u.add_argument("--no-post", action="store_true", help="post nothing to the health channel")
    f = sub.add_parser("freeze", help="keep every state as it is", allow_abbrev=False)
    f.add_argument("--off", action="store_true", help="let the nightly run move them again")
    sub.add_parser("reset", help="every signal back to what closure.py says",
                   allow_abbrev=False)
    b = sub.add_parser("backtest", help="replay the recorded decisions with the loop on",
                       allow_abbrev=False)
    b.add_argument("--days", type=int, default=BACKTEST_DAYS)
    b.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        data = load()
        print(json.dumps(data, indent=1, sort_keys=True) if a.json else render_show(data))
    elif a.cmd == "update":
        print(json.dumps(update(post=not a.no_post, dry_run=a.dry_run), sort_keys=True))
    elif a.cmd == "freeze":
        print(json.dumps(freeze(not a.off)))
    elif a.cmd == "reset":
        print(json.dumps(reset()))
    else:
        rep = backtest(days=a.days)
        print(json.dumps(rep, indent=1, sort_keys=True) if a.json else render_backtest(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
