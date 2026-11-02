#!/usr/bin/env python3
"""Closure labels: every Done marker in Tacitus as one labelled example.

A closure detector learns from the places where someone said what state a
piece of work is in. This module gathers those places into one store,
`<private dir>/evals/closure-labels.jsonl` (mode 0600, in a 0700 dir),
one JSON line per labelled event, with the features a detector could have
used when the decision was made (docs/ea/closure-signals.md). It reads
every source and writes nothing but that file. Features and ids only: no
mail text, no message text, no names. A thread key (which can be a topic
name) and a ledger id go in as short hashes, `thread_ref` and
`ledger_ref`, so two sources about one thread or one row still join.

Sources:
  owner-tap   the owner's "Mark done" / "Needs me" in the web chat: each
              line of `thread-state-labels.jsonl` beside the web store.
              Gold. web_thread_state writes the record at the tap, with the
              thread as the rules read it then; `collect` rebuilds a tap
              the hook missed from the store (`reconstructed`).
  card        a card's outcome as its status line records it (Sent,
              Created, Closed, Cancelled, Sent from Outlook, Kept open...),
              from the web store: a `status` row, or a card copied from
              Zulip's past read as thread_state.card_view reads it.
  ledger      rows that reached completed, with their provenance
              (closure_metrics) and closing signal (closure.py); reopens,
              rejected and confirmed close proposals (ledger_events); and
              item_feedback dispositions (still_open, done, not_mine,
              snooze).
  owner-said  missed-closures.log: a closure the owner had to state, with
              the log's signal autopsy (present, fired, not evaluated).
              Gold for "the detector missed it".
  model       the thread_state model's judgments: written by the engine's
              observer at the answer (state and confidence), and taken
              from the store's `thread_state` events for the days before
              the observer ran (state only).

Record (validate() refuses any other shape, and any string that is not a
short token, so no text can get in):

  {"key", "at", "source", "kind", "label", "subject_type", "channel_kind",
   "origin", "thread_ref", "ledger_ref", "features": {...}}

`key` is a stable hash per event; append() writes a key once, so collect
runs any number of times. `at` is UTC ISO. `label` is the state the event
says: done, open, needs_you, waiting, working or none; closed(label)
reads done and none as closed. FEATURE_KEYS lists the features.

    closure_labels.py collect [--days N] [--store PATH] [--labels PATH] [--report]
    closure_labels.py stats [--labels PATH]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import closure as _closure
    import ledger as _ledger
    import owner_trust as _owner_trust
    import resident_io as _rio
    from chat import thread_state as _ts
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import closure as _closure
    from preprocessor import ledger as _ledger
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _rio
    from preprocessor.chat import thread_state as _ts

LABELS_FILE = "closure-labels.jsonl"
EVALS_DIR = "evals"
STORE_FILE = "webchat.sqlite"
TAPS_FILE = "thread-state-labels.jsonl"

SOURCES = ("owner-tap", "card", "ledger", "owner-said", "model")
LABELS = ("done", "open", "needs_you", "waiting", "working", "none")
CLOSED_LABELS = frozenset({"done", "none"})
RECORD_KEYS = ("key", "at", "source", "kind", "label", "subject_type", "channel_kind",
               "origin", "thread_ref", "ledger_ref", "features")
FEATURE_KEYS = frozenset({
    # a chat thread as it stood at the decision
    "message_count", "owner_messages", "bot_messages", "hours_since_last_message",
    "hours_since_last_owner_message", "hours_since_last_bot_message", "last_author",
    "ends_with_question", "has_live_card", "card_kind", "card_outcome", "has_card_pointer",
    "pointer_card_outcome", "stuck_turn", "mirrored", "interaction_type", "rule_state",
    "rule_id", "prior_state", "prior_by", "reconstructed", "writeback", "confidence",
    "applied_state",
    # the linked ledger row, or the row itself
    "linked_ledger_count", "linked_ledger_type", "linked_ledger_status", "deadline_delta_days",
    "closes_on", "provenance", "signal", "closed_how", "age_days", "has_conversation",
    "has_source_message", "detector_row", "plugin_row",
    # a missed closure's autopsy
    "signals_present", "signals_fired", "signals_unevaluated", "evidence_complete",
    "candidate_evidence_count", "suggestion", "said_via",
})
# Every string value: a short token with no space, so a sentence, a name
# with a space, a subject line or an address with a space cannot pass.
TOKEN_RE = re.compile(r"[A-Za-z0-9_.:+=/-]{0,40}")
LIST_CAP = 16

# A card's outcome, from its status line. `live` and `busy` are not
# settled; the rest are, and SETTLED reads them as the card closed.
SETTLED = frozenset({"acted", "declined", "outside", "superseded", "kept_open"})
_OUTCOME_RULES = (
    (re.compile(r"tap a reaction below|reply `|^code \*\*", re.I), "live"),
    (re.compile(r"\brevised\b|one by one", re.I), "superseded"),
    (re.compile(r"sent from|marked as sent|sent myself|already (?:handled|sent|scheduled|closed)"
                r"|deleted in|draft was deleted|settled outside|was already handled", re.I),
     "outside"),
    (re.compile(r"kept open|still open", re.I), "kept_open"),
    (re.compile(r"cancel+ed|not published|skipped|not sent|nothing was sent", re.I), "declined"),
    (re.compile(r"expired|no longer have|could not|failed", re.I), "failed"),
)
_CARD_TITLE_KINDS = (
    ("confirm send", "send"), ("drafts are ready", "bulk"), ("look done", "close"),
    ("looks done", "close"), ("confirm reschedule", "event_update"),
    ("confirm event deletion", "event_delete"), ("confirm calendar", "event"),
    ("push to", "publish"), ("publish", "publish"), ("invit", "invite"), ("schedule", "schedule"),
)
# The one-line pointer to a card in the DM (thread_state.POINTER_RE reads
# its link).
_POINTER_RE = re.compile(r"\bin your DM\b", re.I)

# The subject of an owner's ask, by the closure analysis's regexes over
# the first owner message (scheduling intent wins over drafting).
_SCHED_RX = re.compile(
    r"\b(schedul\w*|resched\w*|availability|available (times|slots)|(good|some|any|workable|more"
    r"|other|possible|acceptable) (times|slots|dates)|times? (that|for|to|next|this|in|on|when)\b"
    r"|times\?|calendar invite|(send|create|set up|make) (an? |the )?(calendar )?(invite|meeting"
    r"|event|hold)\b|zoom (link|invite)|calendly|cal\.com|when2meet|doodle|poll\b|find a time"
    r"|time to meet|set up a (call|meeting|time)|move (the|my) (meeting|call)|hold (the|a) "
    r"(date|time)|block (off|out)|p[012]\b)", re.I)
_SCHED_WEAK_RX = re.compile(r"\b((meet|meeting|call) (with|between)|1:1|slot\b|meet\b|meeting\b)",
                            re.I)
_DOC_RX = re.compile(
    r"\b(slides?|deck|presentation|two-pager|one-pager|white ?paper|outline|abstract|quiz"
    r"|assignment|lecture|letter of rec\w*|recommendation letter|letter for|proposal (section|text"
    r"|draft)|bio\b|cv\b|report\b|summary\b|doc\b|document|spreadsheet|workbook|overleaf"
    r"|markdown|syllabus|narrative|paragraph|section)", re.I)
_DOC_VERB_RX = re.compile(r"\b(draft|write|make|create|build|put together|prepare|revise|edit"
                          r"|update|fix|rework|finish|compile|turn)\w*", re.I)
_EMAIL_RX = re.compile(
    r"(^\W*(draft|reply|respond|write|email|send|follow up|ping|thank|apolog|bump|get back to"
    r"|tell|let .{1,40} know|nudge|loop in|cc)\b|\b(draft (a|an|the)? ?(email|reply|response"
    r"|note|message)|reply to|respond to|email (to|from|back)|follow ?up (with|on|to)|note (to"
    r"|back)|send (a|an|the|him|her|them) |write (a|an|back|to)|thank (him|her|them|you note)"
    r"|apolog\w*|decline|forward (this|it|the)|draft (it|this|that)|response to)\b)", re.I)
_INSTR_RX = re.compile(
    r"(\b(from now on|going forward|in the future|always|never|don't|do not|stop\b|remember"
    r"|prefer\w*|archive|close (it|this|the|out)|mark (it|this|as)|log (this|the|a|it)|file (this"
    r"|it|the|a)|add (this|it|to|a)|update (the|my)|track\w*|remind\w*|create (a )?(new )?(project"
    r"|channel|room)|open a channel|rename|snooze|ledger|ignore|suppress|install|turn (on|off)"
    r"|enable|disable|fill (out|in|this)|submit|upload|attach|stage|save|push|publish|delete"
    r"|cancel|priority|reimburs\w*|receipts?|form\b)\b)", re.I)
_LOOKUP_RX = re.compile(
    r"(\?|\b(what|when|where|who|why|how|which|did|does|do i|is there|are there|can you find"
    r"|check\b|look (at|up|into|over)|find\b|status|summar\w*|review|read\b|any (news|update)"
    r"|analy[sz]\w*|fact ?check|thoughts|wdyt)\b)", re.I)
_CHIT_RX = re.compile(r"^\s*(hi|hello|hey|test\w*|ping|thanks?|thank you|ok(ay)?|good (morning"
                      r"|night)|lol|haha|nice|great)\b", re.I)

# The rules' reasons (thread_state.decide) by the rule that gives them.
_RULE_IDS = (
    ("still has its buttons", "2-live-card"),
    ("A card here still has", "2-live-card"),
    ("phone waits", "2a-device-pending"),
    ("Its card in Direct messages waits", "2b-linked-card"),
    ("Its card was handled", "2b-linked-card"),
    ("Its cards were handled", "2b-linked-card"),
    ("phone was approved", "2a-device-settled"),
    (_ts.NOT_ANSWERED, "3-stuck"),
    ("working on your last message", "3-working"),
    ("You wrote last", "3-working"),
    ("acting on the button", "3c-busy-card"),
    ("The card was answered", "4-card-outcome"),
    ("linked ledger item", "6-ledger"),
    ("An automated post", "7-automated"),
)

# Closing signals by who settled the row: a rule (decisive evidence, a
# standing rule of the owner's), the model, the owner confirming a
# proposal, or the owner's own word or click.
_OWNER_SIGNALS = frozenset(_closure.USER_SIGNALS - {"user_confirmed", "user_rule"})
# A close written before closed_by existed, read off its note when no
# signal can be inferred from it.
_LEGACY_HOW = (
    (re.compile(r"\b(per user|user (said|confirmed|stated|marked)|marked done|per the owner"
                r"|owner (said|confirmed)|from (the )?dashboard)\b", re.I), "owner"),
    (re.compile(r"\b(duplicate|dup of|superseded|merged into)\b", re.I), "rule"),
    (re.compile(r"\b(auto-?closed|briefing|catch-?up|audit|reconcile)\b", re.I), "model"),
)


# ── paths ─────────────────────────────────────────────────────────────

def labels_path() -> Path:
    return _owner_trust.private_dir() / EVALS_DIR / LABELS_FILE


def store_path() -> Path:
    """The web chat's store: the chat block's `web.store_path`, else
    `<private dir>/webchat.sqlite`."""
    try:
        try:
            import ea_config
        except ModuleNotFoundError:
            from preprocessor import ea_config
        cfg, _trusted = ea_config.get_chat_source(None)
        web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
        if web.get("store_path"):
            return Path(str(web["store_path"]))
    except Exception:
        pass
    return _owner_trust.private_dir() / STORE_FILE


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))


# ── small helpers ─────────────────────────────────────────────────────

def ref(*parts: Any) -> str:
    """A short stable hash of `parts`."""
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:16]


def thread_ref(channel: str, thread_key: str) -> str:
    return ref("thread", channel, thread_key)


def ledger_ref(ledger_id: str) -> str:
    return ref("ledger", ledger_id)


def closed(label: str | None) -> bool:
    return label in CLOSED_LABELS


def token(value: Any, default: str | None = None) -> str | None:
    """`value` as a token a record may hold, else `default`."""
    if value is None:
        return default
    s = str(value).strip()
    return s if s and TOKEN_RE.fullmatch(s) else default


def iso(ts: Any) -> str:
    """UTC ISO seconds for an epoch number or an ISO string; "" when neither."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dt = parse_ts(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def epoch(value: Any) -> float | None:
    dt = parse_ts(value)
    return dt.timestamp() if dt else None


def _days(later: Any, earlier: Any) -> float | None:
    a, b = epoch(later), epoch(earlier)
    return round((a - b) / 86400, 2) if a is not None and b is not None else None


def _hours(now: float, at: float | None) -> float | None:
    return round((now - at) / 3600, 2) if at else None


def _deadline_delta(deadline: Any, now: Any) -> float | None:
    """Days from `now` to the deadline's date (negative once past)."""
    d = parse_ts(str(deadline or "")[:10]) if deadline else None
    n = parse_ts(now)
    if d is None or n is None:
        return None
    return float((d.date() - n.date()).days)


# ── the record ────────────────────────────────────────────────────────

def _check_value(name: str, v: Any) -> None:
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"{name}: not a finite number")
        return
    if isinstance(v, str):
        if not TOKEN_RE.fullmatch(v):
            raise ValueError(f"{name}: a string that is not a short token (no text goes in)")
        return
    if isinstance(v, list):
        if len(v) > LIST_CAP:
            raise ValueError(f"{name}: more than {LIST_CAP} items")
        for x in v:
            if not isinstance(x, str) or not TOKEN_RE.fullmatch(x):
                raise ValueError(f"{name}: a list item that is not a short token")
        return
    raise ValueError(f"{name}: a {type(v).__name__} is no feature value")


def validate(rec: dict) -> dict:
    """`rec` when it has the record's shape; ValueError otherwise, and for
    any string that is not a short token (no space, at most 40 of
    [A-Za-z0-9_.:+=/-]), so no text can reach the store."""
    if not isinstance(rec, dict) or tuple(sorted(rec)) != tuple(sorted(RECORD_KEYS)):
        raise ValueError(f"a record has exactly the keys {', '.join(RECORD_KEYS)}")
    if rec["source"] not in SOURCES:
        raise ValueError(f"unknown source {rec['source']!r}")
    if rec["label"] not in LABELS:
        raise ValueError(f"unknown label {rec['label']!r}")
    if not rec["key"] or not rec["at"]:
        raise ValueError("a record needs its key and its time")
    for k in RECORD_KEYS:
        if k != "features":
            _check_value(k, rec[k])
    feats = rec["features"]
    if not isinstance(feats, dict):
        raise ValueError("features is an object")
    extra = set(feats) - FEATURE_KEYS
    if extra:
        raise ValueError(f"unknown features: {', '.join(sorted(extra))}")
    for k, v in feats.items():
        _check_value(f"features.{k}", v)
    return rec


def record(source: str, kind: str, label: str, *, key: str, at: Any, features: dict,
           subject_type: str | None = None, channel_kind: str | None = None,
           origin: str | None = None, thread: str | None = None,
           ledger: str | None = None) -> dict:
    """One validated record."""
    feats = {k: v for k, v in features.items() if v is not None}
    return validate({"key": key, "at": iso(at), "source": source, "kind": token(kind, "other"),
                     "label": label, "subject_type": token(subject_type),
                     "channel_kind": token(channel_kind), "origin": token(origin),
                     "thread_ref": thread, "ledger_ref": ledger, "features": feats})


# ── the store file ────────────────────────────────────────────────────

_KEYS_LOCK = threading.Lock()
# path -> (inode, bytes read, the keys in those bytes)
_KEYS: dict[str, tuple[int, int, set]] = {}


def _known_keys(fh, path: str) -> set:
    """The keys already in the file, read incrementally: a file this
    process read before is read from where it stopped."""
    st = os.fstat(fh.fileno())
    ino, done, keys = _KEYS.get(path, (st.st_ino, 0, set()))
    if ino != st.st_ino or st.st_size < done:
        ino, done, keys = st.st_ino, 0, set()
    fh.seek(done)
    chunk = fh.read()
    end = chunk.rfind("\n") + 1
    for line in chunk[:end].splitlines():
        try:
            k = json.loads(line).get("key")
        except (ValueError, AttributeError):
            continue
        if k:
            keys.add(k)
    _KEYS[path] = (ino, done + len(chunk[:end].encode()), keys)
    return keys


def append(records: list[dict], path: str | Path | None = None) -> int:
    """Append each record whose key the file does not hold yet; how many
    were written. The file is made 0600 in a 0700 dir; a link at its name
    is refused. Each record is validated first."""
    recs = [validate(r) for r in records]
    if not recs:
        return 0
    path = Path(path or labels_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _KEYS_LOCK:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            if os.fstat(fh.fileno()).st_mode & 0o077:
                os.fchmod(fh.fileno(), 0o600)
            keys = _known_keys(fh, str(path))
            lines = []
            for r in recs:
                if r["key"] in keys:
                    continue
                keys.add(r["key"])
                lines.append(json.dumps(r, sort_keys=True, separators=(",", ":")))
            if lines:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                ino, done, _ = _KEYS[str(path)]
                _KEYS[str(path)] = (ino, done + len(("\n".join(lines) + "\n").encode()), keys)
    return len(lines)


def load(path: str | Path | None = None) -> list[dict]:
    """Every record in the store, oldest line first; a line that does not
    parse is skipped."""
    path = Path(path or labels_path())
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("source") in SOURCES:
            out.append(r)
    return out


# ── reading a chat thread ─────────────────────────────────────────────

def subject_type(messages: list[dict]) -> str:
    """What the owner's first message asks for: scheduling,
    drafting_email, drafting_doc, instruction, lookup, chit_chat or other;
    `none` when the owner never wrote. Read in memory only."""
    first = next((m for m in messages if m.get("author") == "owner"), None)
    if first is None:
        return "none"
    t = str(first.get("text") or "")[:700]
    if len(t.strip()) < 25 and _CHIT_RX.match(t):
        return "chit_chat"
    if _SCHED_RX.search(t):
        return "scheduling"
    if _EMAIL_RX.search(t):
        return "drafting_email"
    if _SCHED_WEAK_RX.search(t) and not _DOC_RX.search(t):
        return "scheduling"
    if _DOC_RX.search(t) and _DOC_VERB_RX.search(t):
        return "drafting_doc"
    if _INSTR_RX.search(t):
        return "instruction"
    if _LOOKUP_RX.search(t):
        return "lookup"
    return "chit_chat" if len(t.strip()) < 40 else "other"


def _is_card(m: dict) -> bool:
    return m.get("kind") in ("card", "status")


def interaction_type(messages: list[dict], channel_kind: str | None, origin: str = "") -> str:
    """The closure analysis's taxonomy: owner_ask, scheduling or room (the
    owner started it), mail_card (a card's own thread), automated (a post
    the owner never wrote in), briefing_reply (an automated post the owner
    answered) or bot_post."""
    if not messages:
        return "bot_post"
    first = _ts.card_view(messages[0])
    if first.get("author") == "owner":
        if channel_kind == "sched":
            return "scheduling"
        if channel_kind == "room":
            return "room"
        return "owner_ask"
    if _is_card(first):
        return "mail_card"
    owner_wrote = any(m.get("author") == "owner" for m in messages)
    if origin or _ts.AUTOMATED_TITLE_RE.search(_ts.plain(first.get("text"), 200)) \
            or _ts.device_code(first.get("text")):
        return "briefing_reply" if owner_wrote else "automated"
    if channel_kind == "sched":
        return "scheduling"
    if channel_kind == "room":
        return "room"
    return "bot_post"


def outcome_class(text: Any) -> str:
    """A card's status line as one word: busy, failed, live, superseded,
    outside (settled outside chat), kept_open, declined or acted."""
    first = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    base = _ts.outcome(first)
    if base == "busy":
        return "busy"
    plain = _ts.plain_text(first)
    for rx, name in _OUTCOME_RULES:
        if rx.search(plain):
            return name
    return "failed" if base == "failed" else "acted"


def card_kind(m: dict) -> str | None:
    """A card's kind: its stored `kind`, else read off its title."""
    card = m.get("card") if isinstance(m.get("card"), dict) else {}
    k = token(card.get("kind"))
    if k:
        return k
    title = _ts.plain_text(str(card.get("title") or m.get("text") or "")).lower()
    for needle, name in _CARD_TITLE_KINDS:
        if needle in title:
            return name
    return "other" if card or _ts.mirrored_card(m) else None


def card_state(m: dict, now: float) -> str | None:
    """`live`, an outcome_class word, `expired` for a card whose buttons
    no longer act, or None for a message that is no card."""
    v = _ts.card_view(m)
    if v.get("kind") == "card":
        card = v.get("card") if isinstance(v.get("card"), dict) else {}
        if not (card.get("actions") or card.get("select")):
            return None
        return "live" if _ts._live(v, now) else "expired"
    if v.get("kind") == "status":
        return outcome_class(v.get("text"))
    return None


def _pointer_targets(text: str) -> list[tuple[str | None, str]]:
    """(card channel or None, card id) for each pointer link in `text`."""
    return [(ch or None, mid) for ch, mid in _ts.pointer_targets(text)]


def _lookup(store: Any, channel: str | None, mid: str) -> dict | None:
    if store is None:
        return None
    try:
        if channel is None:
            channel = store.thread_channel(mid)
        return store.message(channel, mid) if channel else None
    except Exception:
        return None


def rule_id(state: str | None, reason: str | None) -> str | None:
    """The rule of thread_state.decide that gave (state, reason)."""
    if not state:
        return None
    r = str(reason or "")
    for needle, rid in _RULE_IDS:
        if needle in r:
            return f"{rid}-{state}" if rid == "6-ledger" else rid
    return "other"


def _primary_row(rows: list[dict]) -> dict | None:
    """The linked row a detector reads first: an open row of a type that
    needs the owner, else any open row, else the first."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        return None
    open_ = [r for r in rows if str(r.get("status") or "") not in ("completed", "closed")]
    needs = [r for r in open_ if str(r.get("type") or "") in _ts.LEDGER_NEEDS]
    return (needs or open_ or rows)[0]


def thread_features(messages: list[dict], *, channel: dict | None = None,
                    ledger: list[dict] | None = None, origin: str = "",
                    devices: list[dict] | None = None, now: float,
                    before: dict | None = None, store: Any = None,
                    linked: list[dict] | None = None) -> tuple[dict, dict]:
    """(features, head) for a thread as it stood at `now`: `messages` is
    the store's JSON, oldest first, cut at `now` by the caller; `before`
    is the state row the decision replaced; `linked` the cards in other
    threads the conversation links to (the store's card_link, rule 2b);
    `store` answers message(channel, id) and thread_channel(id) for a
    pointer's card the links do not hold. The head holds subject_type,
    channel_kind and origin. No text leaves."""
    ch = channel or {}
    kind = token(ch.get("kind"), "other")
    views = [_ts.card_view(m) for m in messages]
    owner_at = [float(m.get("posted_at") or 0) for m in views if m.get("author") == "owner"]
    bot_at = [float(m.get("posted_at") or 0) for m in views if m.get("author") != "owner"]
    all_at = owner_at + bot_at
    last = views[-1] if views else {}
    cards = [v for v in views if _is_card(v)]
    newest_card = cards[-1] if cards else None
    newest_card_raw = next((m for m in reversed(messages) if _is_card(_ts.card_view(m))), None)
    pointers = [t for v in views if v.get("author") != "owner"
                and _POINTER_RE.search(str(v.get("text") or ""))
                for t in _pointer_targets(str(v.get("text") or ""))]
    has_pointer = any(v.get("author") != "owner" and _POINTER_RE.search(str(v.get("text") or ""))
                      for v in views)
    pointer_outcome = None
    for target in reversed([c for c in linked or [] if c.get("kind") != "missing"]):
        pointer_outcome = card_state(target, now)
        if pointer_outcome:
            break
    for ch_id, mid in reversed(pointers if pointer_outcome is None else []):
        target = _lookup(store, ch_id, mid)
        if target is not None:
            pointer_outcome = card_state(target, now)
            if pointer_outcome:
                break
    got = _ts.decide(messages, channel=ch, ledger=ledger, prev=None, origin=origin,
                     devices=devices, now=now, linked=linked) if messages else None
    rows = [r for r in ledger or [] if isinstance(r, dict)]
    row = _primary_row(rows)
    prior = before or {}
    inter = interaction_type(messages, kind, origin)
    first = messages[0] if messages else {}
    feats = {
        "message_count": len(messages),
        "owner_messages": len(owner_at),
        "bot_messages": len(bot_at),
        "hours_since_last_message": _hours(now, max(all_at) if all_at else None),
        "hours_since_last_owner_message": _hours(now, max(owner_at) if owner_at else None),
        "hours_since_last_bot_message": _hours(now, max(bot_at) if bot_at else None),
        "last_author": ("owner" if last.get("author") == "owner" else "bot") if last else None,
        "ends_with_question": bool(last) and last.get("author") != "owner"
        and last.get("kind") == "message" and _ts.asks(last.get("text")),
        "has_live_card": any(_ts._live(v, now) for v in views),
        "card_kind": card_kind(newest_card) if newest_card else None,
        "card_outcome": card_state(newest_card_raw, now) if newest_card_raw else None,
        "has_card_pointer": has_pointer or bool(linked),
        "pointer_card_outcome": pointer_outcome,
        "stuck_turn": bool(got) and got[2] == _ts.NOT_ANSWERED,
        "mirrored": any(m.get("history") for m in messages),
        "interaction_type": inter,
        "rule_state": got[0] if got else None,
        "rule_id": rule_id(got[0], got[2]) if got else None,
        "prior_state": token(prior.get("state")),
        "prior_by": token(prior.get("by")),
        "linked_ledger_count": len(rows),
        "linked_ledger_type": token(row.get("type"), "other") if row else None,
        "linked_ledger_status": token(row.get("status"), "other") if row else None,
        "deadline_delta_days": _deadline_delta(row.get("deadline"), now) if row else None,
    }
    origin_word = token(origin, "notifier") if origin else (
        "owner" if first.get("author") == "owner" else "bot")
    head = {"subject_type": subject_type(messages), "channel_kind": kind, "origin": origin_word}
    return feats, head


# ── live records (the engine's observer, web_thread_state) ────────────

def tap_key(channel: str, thread_key: str, at: Any, state: str) -> str:
    return ref("owner-tap", channel, thread_key, f"{float(at or 0):.6f}", state)


def owner_tap_record(ctx: dict, *, row: dict, before: dict | None, now: float,
                     store: Any = None, writeback: str | None = None,
                     reconstructed: bool = False) -> dict:
    """The record of one owner tap: the state the owner set (`row`, the
    store row the tap wrote) and the thread as the rules read it (`ctx`,
    ThreadStates.contexts)."""
    feats, head = thread_features(ctx.get("messages") or [], channel=ctx.get("channel_row"),
                                  ledger=ctx.get("ledger"), origin=ctx.get("origin") or "",
                                  devices=ctx.get("devices"), now=now, before=before,
                                  store=store, linked=ctx.get("linked"))
    feats["writeback"] = token(writeback)
    feats["reconstructed"] = reconstructed or None
    lrow = _primary_row(ctx.get("ledger") or [])
    state = str(row.get("state") or "")
    return record("owner-tap", "tap", state if state in LABELS else "open",
                  key=tap_key(ctx["channel"], ctx["key"], row.get("at"), state),
                  at=row.get("at") or now, features=feats,
                  thread=thread_ref(ctx["channel"], ctx["key"]),
                  ledger=ledger_ref(lrow["id"]) if lrow and lrow.get("id") else None, **head)


def model_record(ctx: dict, *, answer: dict | None, row: dict | None, now: float,
                 store: Any = None) -> dict:
    """The record of one model answer: the model's own state and
    confidence (`answer`, thread_state.parse_answer; None when it did not
    parse), and the state the engine applied (`row`)."""
    feats, head = thread_features(ctx.get("messages") or [], channel=ctx.get("channel_row"),
                                  ledger=ctx.get("ledger"), origin=ctx.get("origin") or "",
                                  devices=ctx.get("devices"), now=now, before=ctx.get("prev"),
                                  store=store, linked=ctx.get("linked"))
    applied = str((row or {}).get("state") or "none")
    if answer is None:
        kind, label, conf = "unparsed", applied, None
    else:
        conf = float(answer.get("confidence") or 0.0)
        kind = "judgment" if conf >= _ts.MIN_CONFIDENCE else "unsure"
        label = str(answer.get("state") or applied)
    feats["confidence"] = round(conf, 3) if conf is not None else None
    feats["applied_state"] = token(applied)
    lrow = _primary_row(ctx.get("ledger") or [])
    return record("model", kind, label if label in LABELS else "none",
                  key=ref("model", ctx["channel"], ctx["key"], ctx.get("newest"), kind),
                  at=now, features=feats, thread=thread_ref(ctx["channel"], ctx["key"]),
                  ledger=ledger_ref(lrow["id"]) if lrow and lrow.get("id") else None, **head)


def record_owner_tap(ctx: dict, **kw: Any) -> int:
    """Append the record of one owner tap (1, or 0 when the store holds
    it already). A record that cannot be written raises; the caller logs
    it."""
    return append([owner_tap_record(ctx, **kw)])


def record_model(ctx: dict, **kw: Any) -> int:
    """Append the record of one model answer, as record_owner_tap."""
    return append([model_record(ctx, **kw)])


# ── the web store, read-only ──────────────────────────────────────────

class StoreReader:
    """The web chat's store opened read-only (ledger.guarded_connect), with
    the few reads collect makes. Messages come back as the store's JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = _ledger.guarded_connect(self.path, timeout=10, read_only=True)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _loads(s: Any) -> Any:
        try:
            return json.loads(s) if s else None
        except ValueError:
            return None

    def _msg(self, r: sqlite3.Row) -> dict:
        return {"seq": r["seq"], "channel": r["channel"], "id": r["id"],
                "thread_key": r["thread_key"], "author": r["author"], "kind": r["kind"],
                "text": r["text"] or "", "card": self._loads(r["card"]),
                "file": self._loads(r["file"]), "reactions": self._loads(r["reactions"]) or [],
                "posted_at": r["posted_at"], "updated_at": r["updated_at"], "reply_count": 0,
                "history": bool(r["history"])}

    def channel(self, cid: str) -> dict | None:
        r = self.conn.execute("SELECT id, name, kind FROM channels WHERE id=?", (cid,)).fetchone()
        return {"id": r["id"], "name": r["name"] or "", "kind": r["kind"] or "room"} if r else None

    def _table(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (name,)).fetchone() is not None

    def thread(self, channel: str, key: str, upto: float | None = None) -> list[dict]:
        """The thread up to `upto`, without the bot's nudges (the rules and
        the model never read them)."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE channel=? AND thread_key=? AND (? IS NULL OR "
            "posted_at <= ?) ORDER BY seq", (channel, key, upto, upto)).fetchall()
        nudged: set = set()
        if rows and self._table("nudges"):
            nudged = {r[0] for r in self.conn.execute(
                "SELECT msg_id FROM nudges WHERE channel=? AND thread_key=?", (channel, key))}
        return [self._msg(r) for r in rows if r["id"] not in nudged]

    def linked_cards(self, channel: str, key: str) -> list[dict]:
        """The cards in other threads the conversation links to (card_link),
        as the store's linked_cards reads them; a link to a card the store
        does not hold is a row of kind `missing`."""
        if not self._table("card_link"):
            return []
        rows = self.conn.execute(
            "SELECT m.*, l.card_id AS link_id FROM card_link l LEFT JOIN messages m ON "
            "m.id = l.card_id AND (l.card_channel = '' OR m.channel = l.card_channel) "
            "WHERE l.channel=? AND l.thread_key=? ORDER BY m.seq IS NULL, m.seq LIMIT ?",
            (channel, key, _ts.LINK_CAP)).fetchall()
        out, seen = [], set()
        for r in rows:
            if r["seq"] is None:
                out.append({"kind": "missing", "id": r["link_id"], "card": None})
            elif r["seq"] not in seen and (r["channel"], r["thread_key"]) != (channel, key):
                seen.add(r["seq"])
                out.append(self._msg(r))
        return out

    def link_origin(self, card_channel: str, card_id: str) -> tuple[str, str] | None:
        """The conversation that links to this card (card_link), or None."""
        if not self._table("card_link"):
            return None
        r = self.conn.execute("SELECT channel, thread_key FROM card_link WHERE card_id=? AND "
                              "(card_channel='' OR card_channel=?) LIMIT 1",
                              (card_id, card_channel)).fetchone()
        return (r[0], r[1]) if r else None

    def message(self, channel: str, mid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM messages WHERE channel=? AND id=?",
                              (channel, mid)).fetchone()
        return self._msg(r) if r else None

    def thread_channel(self, key: str) -> str | None:
        for col in ("thread_key", "id"):
            rows = self.conn.execute(f"SELECT DISTINCT channel FROM messages WHERE {col}=? "
                                     f"LIMIT 2", (key,)).fetchall()
            if rows:
                return rows[0][0] if len(rows) == 1 else None
        return None

    def origin(self, channel: str, key: str) -> str:
        try:
            r = self.conn.execute("SELECT origin FROM thread_origin WHERE channel=? AND "
                                  "thread_key=?", (channel, key)).fetchone()
        except sqlite3.OperationalError:
            return ""
        return str(r[0] or "") if r else ""

    def state_events(self, since: float | None) -> list[dict]:
        try:
            rows = self.conn.execute("SELECT at, payload FROM events WHERE type='thread_state' "
                                     "AND (? IS NULL OR at >= ?) ORDER BY seq",
                                     (since, since)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = self._loads(r["payload"])
            if isinstance(d, dict):
                out.append(d)
        return out

    def card_rows(self, since: float | None) -> list[dict]:
        """Status rows, and the text rows a card copied from Zulip's past
        may be."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE (kind='status' OR (history=1 AND kind='message' AND "
            "text LIKE '%:lock%')) AND (? IS NULL OR COALESCE(updated_at, posted_at) >= ?) "
            "ORDER BY seq", (since, since)).fetchall()
        return [self._msg(r) for r in rows]

    def pointer_messages(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM messages WHERE author!='owner' AND "
                                 "text LIKE '%in your DM%'").fetchall()
        return [self._msg(r) for r in rows]


def _ledger_fn(db: Path | None) -> Callable[[list, list], list]:
    """The linked rows of a thread, as the bot's engine reads them."""
    try:
        import web_thread_state as _wts
    except ModuleNotFoundError:
        from preprocessor import web_thread_state as _wts

    def read(ids: list, convs: list) -> list:
        try:
            return _wts.ledger_rows(ids, convs, db_path=db)
        except Exception:
            return []
    return read


def _context(reader: StoreReader, channel: str, key: str, upto: float,
             ledger_fn: Callable) -> dict | None:
    msgs = reader.thread(channel, key, upto=upto)
    if not msgs:
        return None
    ids, convs = _ts.link_keys(msgs)
    linked = [c for c in reader.linked_cards(channel, key)
              if c.get("kind") == "missing" or float(c.get("posted_at") or 0) <= upto]
    return {"channel": channel, "key": key, "messages": msgs,
            "channel_row": reader.channel(channel) or {"id": channel, "kind": "other"},
            "ledger": ledger_fn(ids, convs), "origin": reader.origin(channel, key),
            "devices": None, "prev": None, "linked": linked,
            "newest": max(int(m["seq"]) for m in msgs)}


# ── the sources, for collect ──────────────────────────────────────────

def _read_jsonl_text(text: str | None) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def owner_tap_records(reader: StoreReader, taps_path: Path, since: float | None,
                      ledger_fn: Callable) -> list[dict]:
    """Each line of the taps file, rebuilt from the store as the thread
    stood at the tap (`reconstructed`; the linked rows are read as they
    are now)."""
    try:
        text = taps_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in _read_jsonl_text(text):
        at = line.get("at")
        if not isinstance(at, (int, float)) or (since is not None and at < since):
            continue
        channel, key = str(line.get("channel") or ""), str(line.get("thread_key") or "")
        state = str(line.get("state") or "")
        if not channel or not key or state not in LABELS:
            continue
        before = {"state": line.get("previous_state"), "by": line.get("previous_by")}
        ctx = _context(reader, channel, key, float(at), ledger_fn) or {
            "channel": channel, "key": key, "messages": [], "ledger": [], "origin": "",
            "channel_row": reader.channel(channel) or {"kind": "other"}}
        rec = owner_tap_record(ctx, row={"state": state, "at": at}, before=before,
                               now=float(at), store=reader, reconstructed=True)
        if not ctx["messages"]:
            rec["features"]["message_count"] = int(line.get("message_count") or 0)
        out.append(rec)
    return out


def _pointer_origins(reader: StoreReader) -> dict[tuple[str | None, str], tuple[str, str]]:
    """(channel or None, card message id) -> the (channel, thread) whose
    pointer names that card."""
    out: dict[tuple[str | None, str], tuple[str, str]] = {}
    for m in reader.pointer_messages():
        for ch, mid in _pointer_targets(str(m.get("text") or "")):
            out.setdefault((ch, mid), (m["channel"], m["thread_key"]))
            out.setdefault((None, mid), (m["channel"], m["thread_key"]))
    return out


def card_records(reader: StoreReader, since: float | None, ledger_fn: Callable) -> list[dict]:
    """Each card whose status line says it settled (or failed), once."""
    origins = _pointer_origins(reader)
    out = []
    for m in reader.card_rows(since):
        at = float(m.get("updated_at") or m.get("posted_at") or 0)
        state = card_state(m, at)
        if state in (None, "live", "busy", "expired"):
            continue
        ctx = _context(reader, m["channel"], m["thread_key"], at, ledger_fn) or {
            "channel": m["channel"], "key": m["thread_key"], "messages": [m], "ledger": [],
            "origin": "", "channel_row": reader.channel(m["channel"]) or {"kind": "other"}}
        feats, head = thread_features(ctx["messages"], channel=ctx["channel_row"],
                                      ledger=ctx["ledger"], origin=ctx["origin"], now=at,
                                      store=reader, linked=ctx.get("linked"))
        feats["card_kind"] = card_kind(_ts.card_view(m))
        feats["card_outcome"] = state
        feats["reconstructed"] = True
        card = m.get("card") if isinstance(m.get("card"), dict) else {}
        src = _ts.card_origin(card) or reader.link_origin(m["channel"], m["id"]) \
            or origins.get((m["channel"], m["id"])) or origins.get((None, m["id"]))
        if src is not None:
            msgs = reader.thread(src[0], src[1], upto=at)
            ch = reader.channel(src[0]) or {}
            feats["interaction_type"] = interaction_type(msgs, ch.get("kind"),
                                                         reader.origin(*src)) if msgs \
                else "mail_card"
            head["subject_type"] = subject_type(msgs) if msgs else head["subject_type"]
        else:
            feats["interaction_type"] = "mail_card"
        head["origin"] = "card"
        out.append(record("card", state, "done" if state in SETTLED else "open",
                          key=ref("card", m["channel"], m["id"]), at=at, features=feats,
                          thread=thread_ref(*src) if src else thread_ref(m["channel"],
                                                                         m["thread_key"]),
                          ledger=None, **head))
    return out


_UNSURE = ("The model was unsure", "gave no answer it could use")


def model_event_records(reader: StoreReader, since: float | None, ledger_fn: Callable,
                        existing: list[dict]) -> list[dict]:
    """The model's answers in the store's event log (kept 14 days), state
    only, for the days before the observer wrote them itself; an answer
    the observer already wrote (same thread, within 2 seconds) is left out."""
    seen: dict[str, list[float]] = {}
    for r in existing:
        if r.get("source") == "model":
            t = epoch(r.get("at"))
            if t is not None:
                seen.setdefault(str(r.get("thread_ref")), []).append(t)
    out = []
    for ev in reader.state_events(since):
        if ev.get("by") != "model" or any(u in str(ev.get("reason") or "") for u in _UNSURE):
            continue
        at, channel, key = ev.get("at"), str(ev.get("channel") or ""), str(ev.get("thread_key") or "")
        state = str(ev.get("state") or "")
        if not isinstance(at, (int, float)) or not channel or not key or state not in LABELS:
            continue
        tref = thread_ref(channel, key)
        if any(abs(t - float(at)) <= 2 for t in seen.get(tref, [])):
            continue
        ctx = _context(reader, channel, key, float(at), ledger_fn)
        if ctx is None:
            continue
        feats, head = thread_features(ctx["messages"], channel=ctx["channel_row"],
                                      ledger=ctx["ledger"], origin=ctx["origin"],
                                      now=float(at), store=reader, linked=ctx.get("linked"))
        feats["applied_state"] = state
        feats["reconstructed"] = True
        lrow = _primary_row(ctx["ledger"])
        out.append(record("model", "judgment", state,
                          key=ref("model-event", channel, key, f"{float(at):.6f}"), at=at,
                          features=feats, thread=tref,
                          ledger=ledger_ref(lrow["id"]) if lrow and lrow.get("id") else None,
                          **head))
    return out


def _signal_of(entry: dict) -> str:
    cb = entry.get("closed_by") if isinstance(entry.get("closed_by"), dict) else {}
    return str(cb.get("signal") or "") or _closure.infer_signal(entry.get("last_action") or "")


def closed_how(signal: str, provenance: str = "", last_action: str = "") -> str:
    """Who settled a row: rule, model, confirmed (the owner confirmed a
    proposal), owner (their word or a click of their own) or other."""
    s = signal or ""
    if s == "user_confirmed":
        return "confirmed"
    if s.startswith("llm:") or provenance == "briefing_llm":
        return "model"
    if s in _OWNER_SIGNALS:
        return "owner"
    if s or provenance in ("scrubber", "inbox_archive"):
        return "rule"
    for rx, how in _LEGACY_HOW:
        if rx.search(last_action or ""):
            return how
    return "other"


def _provenance(last_action: str) -> str:
    try:
        import closure_metrics as _metrics
    except ModuleNotFoundError:
        from preprocessor import closure_metrics as _metrics
    return _metrics.classify_last_action(last_action or "")


def _ledger_features(entry: dict, at: Any) -> dict:
    eid = str(entry.get("id") or "")
    return {"linked_ledger_count": 1,
            "linked_ledger_type": token(_ledger.canonical_type(entry.get("type")), "other"),
            "linked_ledger_status": token(entry.get("status"), "other"),
            "closes_on": token(_ledger.closes_on(entry)),
            "deadline_delta_days": _deadline_delta(entry.get("deadline"), at),
            "age_days": _days(at, entry.get("date_identified") or entry.get("created_at")),
            "has_conversation": bool(entry.get("conversation_id")),
            "has_source_message": bool(entry.get("source_message_id")),
            "detector_row": _ledger.is_detector_row(eid),
            "plugin_row": _closure.is_plugin_row(entry),
            "interaction_type": "mail_item"}


def _ledger_record(kind: str, label: str, entry: dict, at: Any, key: str, **extra: Any) -> dict:
    feats = {**_ledger_features(entry, at), **extra}
    plugin = _closure.is_plugin_row(entry)
    origin = "plugin" if plugin else ("mail" if entry.get("source_message_id") else "manual")
    return record("ledger", kind, label, key=key, at=at, features=feats, subject_type=None,
                  channel_kind="mail", origin=origin, thread=None,
                  ledger=ledger_ref(entry.get("id") or ""))


def ledger_records(db: Path, since: float | None, ea_dir: Path) -> list[dict]:
    """Completed rows, reopens, rejected and confirmed proposals, and the
    owner's item dispositions."""
    cutoff = iso(since) if since is not None else ""
    out: list[dict] = []
    conn = _ledger.guarded_connect(db, read_only=True)
    try:
        conn.row_factory = sqlite3.Row
        entries: dict[str, dict] = {}
        for r in conn.execute("SELECT * FROM ledger"):
            entries[r["id"]] = _ledger._row_to_dict(r)
        for e in entries.values():
            if str(e.get("status") or "") not in ("completed", "closed"):
                continue
            at = e.get("closed_at") or e.get("updated_at")
            if not at or (cutoff and iso(at) < cutoff):
                continue
            sig = _signal_of(e)
            prov = _provenance(e.get("last_action") or "")
            out.append(_ledger_record(
                "completed", "done", e, at, ref("ledger", "completed", e["id"], at),
                provenance=prov, signal=token(sig), closed_how=closed_how(
                    sig, prov, e.get("last_action") or "")))
        try:
            evs = conn.execute("SELECT seq, ts, entry_id, kind, signal FROM ledger_events "
                               "WHERE kind IN ('reopen', 'reject', 'confirm') AND ts >= ? "
                               "ORDER BY seq", (cutoff,)).fetchall()
        except sqlite3.OperationalError:
            evs = []
        names = {"reopen": ("reopened", "open"), "reject": ("rejected", "open"),
                 "confirm": ("confirmed", "done")}
        for ev in evs:
            e = entries.get(ev["entry_id"]) or {"id": ev["entry_id"]}
            kind, label = names[ev["kind"]]
            sig = str(ev["signal"] or "")
            out.append(_ledger_record(kind, label, e, ev["ts"], ref("ledger", "event", ev["seq"]),
                                      signal=token(sig), closed_how=closed_how(sig)))
    finally:
        conn.close()
    feedback = _rio.read_text(ea_dir / "logs" / "item-feedback.jsonl")
    labels = {"still_open": "open", "done": "done", "not_mine": "done", "snooze": "waiting"}
    for line in _read_jsonl_text(feedback):
        kind, lid, ts = line.get("kind"), str(line.get("ledger_id") or ""), line.get("ts")
        if kind not in labels or not lid or not ts or (cutoff and iso(ts) < cutoff):
            continue
        e = entries.get(lid) or {"id": lid}
        out.append(_ledger_record(kind, labels[kind], e, ts,
                                  ref("ledger", "feedback", ts, lid, kind),
                                  said_via=token(line.get("source"), "other")))
    return out


def _suggestion(text: str) -> str:
    t = str(text or "")
    if not t:
        return "none"
    for needle, name in (("INCONCLUSIVE", "inconclusive"), ("No detectable signal", "no_signal"),
                         ("Outlook-reply", "broaden_reply"), ("calendar-match", "broaden_calendar"),
                         ("CONFIRMATION_SENDER", "confirmation_sender")):
        if needle in t:
            return name
    return "other"


def owner_said_records(ea_dir: Path, since: float | None) -> list[dict]:
    """missed-closures.log: each closure the owner had to state, with the
    log's autopsy as tokens and counts."""
    text = _rio.read_text(ea_dir / "logs" / "missed-closures.log")
    cutoff = iso(since) if since is not None else ""
    out = []
    for r in _read_jsonl_text(text):
        ts, lid = r.get("ts"), str(r.get("ledger_id") or "")
        if not ts or not lid or (cutoff and iso(ts) < cutoff):
            continue
        es = r.get("entry_summary") if isinstance(r.get("entry_summary"), dict) else {}
        sigs = [s for s in r.get("scrubber_signals_present") or [] if isinstance(s, dict)]
        name = lambda s: token(s.get("signal"), "other")  # noqa: E731
        feats = {
            "linked_ledger_count": 1 if es else 0,
            "linked_ledger_type": token(_ledger.canonical_type(es.get("type")), "other") if es
            else None,
            "linked_ledger_status": token(es.get("status")),
            "deadline_delta_days": _deadline_delta(es.get("deadline"), ts),
            "age_days": _days(ts, es.get("date_identified")),
            "has_conversation": bool(es.get("conversation_id")) if es else None,
            "has_source_message": bool(es.get("has_source_message_id")) if es else None,
            "detector_row": _ledger.is_detector_row(lid),
            "signals_present": sorted({name(s) for s in sigs})[:LIST_CAP],
            "signals_fired": sorted({name(s) for s in sigs if s.get("would_fire")})[:LIST_CAP],
            "signals_unevaluated": sorted({name(s) for s in sigs
                                           if s.get("evaluated") is False})[:LIST_CAP],
            "evidence_complete": r.get("evidence_complete"),
            "candidate_evidence_count": len(r.get("candidate_evidence") or []),
            "suggestion": _suggestion(r.get("suggested_signal")),
            "said_via": token(r.get("source"), "other"),
            "interaction_type": "mail_item",
        }
        out.append(record("owner-said", "missed", "done", key=ref("owner-said", ts, lid), at=ts,
                          features=feats, channel_kind="mail", origin="owner",
                          ledger=ledger_ref(lid)))
    return out


# ── collect ───────────────────────────────────────────────────────────

def collect(*, days: float | None = None, store: str | Path | None = None,
            labels: str | Path | None = None, ledger_db: str | Path | None = None,
            ea_dir: str | Path | None = None, now: float | None = None) -> dict:
    """Walk every source and append what the store does not hold yet.
    A source that fails is reported and the others still run."""
    now = time.time() if now is None else float(now)
    since = now - float(days) * 86400 if days else None
    labels = Path(labels or labels_path())
    ea = Path(ea_dir or _ea_dir())
    db = Path(ledger_db or (ea / "state" / "obligations-ledger.db"))
    store = Path(store or store_path())
    existing = load(labels)
    found: dict[str, list[dict]] = {s: [] for s in SOURCES}
    errors: dict[str, str] = {}
    ledger_fn = _ledger_fn(db) if db.exists() else (lambda _ids, _convs: [])
    if store.is_file() and not store.is_symlink():
        reader = None
        try:
            reader = StoreReader(store)
            for name, fn in (("owner-tap", lambda: owner_tap_records(
                                 reader, store.parent / TAPS_FILE, since, ledger_fn)),
                             ("card", lambda: card_records(reader, since, ledger_fn)),
                             ("model", lambda: model_event_records(reader, since, ledger_fn,
                                                                   existing))):
                try:
                    found[name] = fn()
                except Exception as e:
                    errors[name] = f"{type(e).__name__}: {e}"[:200]
        except Exception as e:
            errors["store"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            if reader is not None:
                reader.close()
    if db.exists():
        try:
            found["ledger"] = ledger_records(db, since, ea)
        except Exception as e:
            errors["ledger"] = f"{type(e).__name__}: {e}"[:200]
    try:
        found["owner-said"] = owner_said_records(ea, since)
    except Exception as e:
        errors["owner-said"] = f"{type(e).__name__}: {e}"[:200]
    added = {}
    for name in SOURCES:
        added[name] = append(found[name], labels) if found[name] else 0
    total = len(existing) + sum(added.values())
    return {"added": added, "seen": {k: len(v) for k, v in found.items()}, "total": total,
            "errors": errors, "path": str(labels)}


def stats(path: str | Path | None = None) -> dict:
    recs = load(path)
    return {"total": len(recs),
            "by_source": dict(Counter(r["source"] for r in recs)),
            "by_source_label": dict(Counter(f"{r['source']}:{r['label']}" for r in recs))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="walk every source; append the new records",
                       allow_abbrev=False)
    c.add_argument("--days", type=float, help="only events of the last N days (default all)")
    c.add_argument("--store", help="the web chat's store (default: the chat block's)")
    c.add_argument("--labels", help=f"the label store (default <private dir>/{EVALS_DIR}/"
                                    f"{LABELS_FILE})")
    c.add_argument("--report", action="store_true",
                   help="then write the evaluation (closure_eval.py report)")
    s = sub.add_parser("stats", help="counts by source and label", allow_abbrev=False)
    s.add_argument("--labels")
    a = ap.parse_args(argv)
    if a.cmd == "stats":
        print(json.dumps(stats(a.labels), sort_keys=True))
        return 0
    out = collect(days=a.days, store=a.store, labels=a.labels)
    if a.report:
        try:
            import closure_eval
        except ModuleNotFoundError:
            from preprocessor import closure_eval
        rep = closure_eval.write_report(labels=a.labels)
        out["summary"] = closure_eval.summary_line(rep)
    print(json.dumps(out, sort_keys=True))
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
