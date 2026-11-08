"""Pre-briefing closure scrubber.

Runs inside the preprocessor before delta filtering. For each open
ledger entry it tries ten signals in order and hands the first that
fires to the closure policy, which closes or proposes (see below):

  1. user_replied_outlook: the user has a Sent message in the entry's
     `conversationId` after its `date_identified`. Uses the `sent` list
     the preprocessor already has from Graph. A sent message whose
     recipients are known and leave the entry's correspondent off is a
     forward, not a reply, and does not count.
  2. user_replied_gmail: the same against Gmail's `threadId`, for
     entries that carry `gmail_thread_id`.
  3. inbound_ack: the latest inbound message in the entry's
     conversation matches an acknowledgment pattern (see
     `acknowledgment_patterns.classify`). This is the portal case: the
     user submits somewhere else and the recipient replies "thanks for
     submitting", with no user-side email reply to find.
  4. confirmation_email: a cross-thread confirmation from a submission
     or registration sender whose text overlaps the entry.
  5. calendar_event_match: a forward-looking calendar event satisfies a
     scheduling-shaped entry.
  6. closed_sibling: another entry of the same conversation was closed
     after this entry was created.
  7. sender_reply_match: the user's own Sent mail to the entry's
     correspondent, by address or by a name the row spells out.
  8. project_member_completion: a member of the entry's project writes
     to say the work is finished.
  9. meeting_response_sent: the user's own Accept/Decline/Tentative on
     the invite an RSVP-shaped row asks about.
  10. identifier_siblings_closed: every manuscript or proposal id a row
     names was closed elsewhere by the user's own archive action.

Signals 1-6 join on `conversation_id` or `gmail_thread_id`. Most open
rows carry neither, so signals 7-10 exist for those and are scoped to
them.

Signals 5 and 7-10 carry a standing human veto: an entry a human
reopened after one of them closed it is never closed by that signal
again (see `VetoStore`). Matches the tightened rules suppress are
appended to `closure-scrubber-suppressed.log` for the next tuning pass.

A fired signal goes to the closure policy (closure.settle): it closes
the row on decisive evidence (a reply to the person a reply row owes)
and otherwise records a proposal the user confirms in one click. Does
NOT archive the source email: it may still be meaningful in the inbox,
and the inline scrubber should not produce side effects that surprise
the user mid-briefing-build.

Every closure is appended to `~/.ea/logs/closure-scrubber.log` so the
user can spot-check what the scrubber did.

Designed to be cheap: one regex pass per inbound plus a hash-map lookup
per ledger entry against `sent`. The Gmail signal does one
`search_messages` call per account, regardless of ledger size.
"""

from __future__ import annotations

import argparse
import json
import os
import re
try:
    import atomic_io as _atomic
    import resident_io as _resident_io
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import resident_io as _resident_io
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a module from inside `preprocessor/` or as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import acknowledgment_patterns  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import closure  # noqa: E402


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))


LOG_PATH = _ea_dir() / "logs" / "closure-scrubber.log"

# Near-misses (a match a looser rule would have closed, suppressed by the
# filters or by a human veto) go to their OWN log, not the audit log:
# closure_metrics reads closure-scrubber.log as the record of what really
# closed, and mixing suppressions in would inflate every count there.
# Derived from LOG_PATH at call time so that redirecting the audit log
# redirects this too.
SUPPRESSED_LOG_NAME = "closure-scrubber-suppressed.log"


def _suppressed_log_path() -> Path:
    return LOG_PATH.with_name(SUPPRESSED_LOG_NAME)


# Standing human vetoes, keyed (entry_id, signal). See `VetoStore`.
VETO_STATE_NAME = "closure-scrubber-vetoes.json"


def _veto_state_path() -> Path:
    return _ea_dir() / "state" / VETO_STATE_NAME


# Senders whose inbound is, by convention, a closure signal: submission
# portals, conference registration systems, NSF reviewer panels, etc.
# Match is substring-against-sender-address (lowercased). Keep the list
# conservative — false positives here auto-close real obligations.
#
# Review and submission systems that CREATE work (an assignment, a review
# invitation) are left out on purpose; papercept.net is one. A plugin adds
# its own systems to the exclusions (mail_rules.closure_exclude_senders,
# docs/plugins.md).
CONFIRMATION_SENDER_SUBSTRINGS: list[str] = [
    "@easychair.org",
    "noreply@hotcrp.com",
    "@hotcrp.com",
    "noreply@eventbrite.com",
    "@cvent.com",
    "no-reply@zoom.us",
    "noreply@zoom.us",
    "noreply@acm.org",
    "no-reply@acm.org",
    "@regonline.com",
    "noreply.events@ieee.org",
    # NSF: address-level, NOT domain-level. Program-officer mail and
    # meeting reminders from @nsf.gov are ACTION, not completion, so the
    # bare domain must never match.
    "noreply@nsf.gov",
    "no-reply@nsf.gov",
    "donotreply@nsf.gov",
    "do-not-reply@nsf.gov",
    "noreply.events@nsf.gov",
    "registration@nsf.gov",
    "nsf-registration@nsf.gov",
]

# How many distinct non-generic tokens a cross-thread confirmation must
# share with a ledger entry before it may close it. One token is noise;
# two independent content words are the cheapest thing that
# distinguishes "this email is about that obligation" from "these two
# texts are both English".
MIN_CONFIRMATION_OVERLAP_TOKENS = 2

# Same floor for the calendar signal, where one shared word closed items
# on overlaps like ['research'], ['lecture'] or ['2026']. Two tokens is
# the floor; at least one of them must also be DISTINCTIVE (see
# `_distinctive_overlap`).
MIN_CALENDAR_OVERLAP_TOKENS = 2

# A token counts as distinctive when it is rare in the ledger corpus:
# document frequency <= max(_RARE_DF_MAX_ABS, fraction * corpus_size).
# A frequent collaborator's name is NOT identifying, so an event naming
# one satisfies every entry that person appears in; a word the ledger
# says once really is about that one obligation. Frequency comes from
# the open ledger itself, so the rule adapts instead of growing a
# stoplist. Below _RARE_MIN_CORPUS entries the frequency estimate is
# meaningless and rarity is not required (the noise/numeric/domain
# filters and the 2-token floor still apply).
_RARE_DF_MAX_ABS = 4
_RARE_DF_FRACTION = 0.05
_RARE_MIN_CORPUS = 8

# Stopwords for token extraction. Kept small — the goal is to drop
# function words, not stem aggressively (an academic-domain Porter
# stemmer would be overkill here and could introduce surprising
# matches).
_STOPWORDS: set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "has", "have", "in", "is", "it", "its", "of", "on", "or",
    "re", "fwd", "the", "this", "that", "to", "was", "were", "will", "with",
    "you", "your", "needs", "response", "needed", "action", "due", "your",
    "please", "thanks", "thank", "hi", "hello", "regards", "best",
}

# Every type test reads `_etype`, the canonical word (ledger.TYPES), so a
# row written under an older spelling meets the same rule.
def _etype(entry: dict) -> str:
    return ledger_mod.canonical_type(entry.get("type"))


# Ledger entry types that are scheduling-shaped. The calendar-match
# signal only fires on entries with these types (or matching the
# scheduling-keyword fallback below).
_SCHEDULING_TYPES = ledger_mod.CALENDAR_MATCH_TYPES
# `calls? (?!for)`: "Call for ... Projects" is a solicitation, not a
# phone call, and a bare `call\b` reads it as scheduling.
_SCHEDULING_KEYWORDS = re.compile(
    r"\b(schedul(e|ing)|meet(ing)?|defense|dissertation|sync|"
    r"discuss(ion)?|1:1|one[-\s]on[-\s]one|appointment|"
    r"calls?\b(?!\s+for\b))",
    re.IGNORECASE,
)

# Entry types that name a DELIVERABLE: something the user must produce,
# file or send. A meeting about a thing is not the thing, and neither is
# a reply in its thread. These types carry a live deadline the user
# feels, so the calendar signal refuses them; only real completion
# evidence (a confirmation, an ack in the thread, a completion report)
# closes a deliverable.
# ae_review_request is a plugin's type, named here until plugins can
# declare traits; an undeclared plugin type keeps exactly this behaviour.
_DELIVERABLE_TYPES = ledger_mod.DELIVERABLE_TYPES | {"ae_review_request"}

# Entry types that name a CORRESPONDENCE obligation: the work is a reply.
# These are the only types `sender_reply_match` may close, because
# "the user wrote back" discharges them by definition. An allowlist, not
# a denylist: plugin docket rows are neither, and must never be closed
# from mail. `ledger.get_open_entries` already filters plugin rows out of
# the pre-briefing pass, so this is a second lock on a door the caller
# normally keeps shut; `scrub_closures` takes `open_entries` from whoever
# calls it, and one caller passing include_plugins would open it.
_REPLY_SHAPED_TYPES = ledger_mod.REPLY_SHAPED_TYPES


def _user_identity_tokens() -> set[str]:
    """Tokens that identify the user (name, email local-part, mail domain).

    Read from ~/.ea/config/user-profile.json. These are excluded from
    every token-overlap match because the user is the subject of every
    event/email and would otherwise match indiscriminately (e.g.,
    'smith' in every calendar invite they attend).

    Tokenised with the SAME pattern `_extract_content_tokens` uses, so a
    local-part carrying digits ('jsmi1234') compares equal on both sides
    instead of yielding 'jsmi' here and 'jsmi1234' there. Domain labels
    are excluded for the same reason: every attendee on the user's own
    domain contributes that domain's label, which is institutional
    boilerplate rather than topic.
    """
    cfg = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "config" / "user-profile.json"
    out: set[str] = set()
    try:
        data = json.loads(cfg.read_text())
    except Exception:
        return out

    def _absorb(text: str) -> None:
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]+", text or ""):
            t = tok.lower().strip("'-")
            if len(t) >= 3:
                out.add(t)

    _absorb(data.get("name") or "")
    email = (data.get("email") or "").lower()
    if "@" in email:
        local, _, domain = email.partition("@")
        _absorb(local)
        for label in domain.split("."):
            _absorb(label)
    elif email:
        _absorb(email)
    return out


# Cached so we read user-profile.json once per process.
_USER_TOKENS: set[str] | None = None


def _user_tokens() -> set[str]:
    global _USER_TOKENS
    if _USER_TOKENS is None:
        _USER_TOKENS = _user_identity_tokens()
    return _USER_TOKENS


# External-sender warning banners ("CAUTION: This email originated from
# outside of the organization. Do not click links or open attachments
# unless...") appear in BOTH ledger-entry notes (stored body previews)
# AND inbound previews, so their vocabulary produces phantom topic
# overlap between unrelated items. The whole banner is stripped BEFORE
# tokenizing: dropping individual words is whack-a-mole, since after
# 'caution'/'email'/'external' the residue 'open'/'organization' still
# matches.
_BANNER_RE = re.compile(
    r"caution[:\-\s][^.!\n]{0,200}[.!]?\s*"
    r"(do\s+not\s+click[^.!\n]{0,200}[.!]?)?",
    re.IGNORECASE,
)


def _strip_email_banners(text: str) -> str:
    # Cap input length: token matching only needs the head of the text,
    # and the cap bounds regex backtracking on adversarial email bodies.
    return _BANNER_RE.sub(" ", (text or "")[:5000])


def _extract_content_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens length >= 4 minus stopwords minus
    user-identity tokens (external-sender banners stripped first).

    Used to find subject/description overlap between a ledger entry
    and an inbound email or calendar event. Conservative threshold
    (>=4 chars) skips noise words while keeping people's names and
    acronyms (e.g., 'isrr', 'pams', 'nataliya'). User identity tokens
    are dropped so 'smith' doesn't match every event the user attends."""
    out: set[str] = set()
    user_toks = _user_tokens()
    text = _strip_email_banners(text)
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]+", text or ""):
        t = tok.lower().strip("'-")
        if len(t) < 4:
            continue
        if t in _STOPWORDS:
            continue
        if t in user_toks:
            continue
        out.add(t)
    return out


def _is_scheduling_entry(entry: dict) -> bool:
    """Is this entry the kind a calendar event could satisfy?

    The keyword fallback reads the DESCRIPTION only. Notes hold stored
    body previews plus the EA's own audit chatter, so one quoted
    "meeting" would make any entry scheduling-shaped. The description is
    what the entry IS; notes are what was said around it.
    """
    if _etype(entry) in _SCHEDULING_TYPES:
        return True
    return bool(_SCHEDULING_KEYWORDS.search(entry.get("description") or ""))


def _was_scheduling_entry_legacy(entry: dict) -> bool:
    """The looser gate (description OR notes), kept only so the
    suppression log can say the old rule would have considered this."""
    if _etype(entry) in _SCHEDULING_TYPES:
        return True
    desc = (entry.get("description") or "") + " " + (entry.get("notes") or "")
    return bool(_SCHEDULING_KEYWORDS.search(desc))


# ── Helpers ────────────────────────────────────────────────────────────

def build_sent_conv_index(sent: list) -> dict[str, str]:
    """Map Outlook conversationId → latest sentDateTime (ISO string).

    Shared with detect_needs_response() in preprocess.py. Kept here so
    the scrubber can be called standalone (CLI / tests) without
    importing preprocess.
    """
    out: dict[str, str] = {}
    for m in sent or []:
        cid = m.get("conversationId")
        if not cid:
            continue
        ts = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        if ts > out.get(cid, ""):
            out[cid] = ts
    return out


def _index_sent_by_conv(sent: list) -> dict[str, list]:
    """Map Outlook conversationId → the user's sent messages in it.

    `build_sent_conv_index` keeps only the latest sentDateTime per
    conversation; the recipient test in `_user_replied_outlook` needs the
    messages themselves.
    """
    out: dict[str, list] = {}
    for m in sent or []:
        cid = m.get("conversationId")
        if cid:
            out.setdefault(cid, []).append(m)
    return out


def _index_inbox_by_conv(inbox: list) -> dict[str, list]:
    """Map Outlook conversationId → list of inbound messages, newest first."""
    out: dict[str, list] = {}
    for m in inbox or []:
        cid = m.get("conversationId")
        if not cid:
            continue
        out.setdefault(cid, []).append(m)
    for cid, msgs in out.items():
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return out


def _index_gmail_sent(msgs: list) -> dict[str, str]:
    """{thread_id: latest_sent_iso} over threads where the user's sent
    mail is a REPLY, from a list of sent messages as
    gmail_client.search_messages returns them.

    A Gmail thread id equals the id of the thread's FIRST message, so a
    sent message with id == thread_id is the user STARTING the thread:
    outreach, not an answer. One such message poisons its whole thread
    here, because a later nudge on a thread the user opened is still not
    a reply, so user-started threads never enter the reply index at all.
    Sending the first message of a thread is the obligation STARTING,
    not finishing.
    """
    started_by_user: set[str] = set()
    for m in msgs or []:
        if not isinstance(m, dict) or "error" in m:
            continue
        if m.get("id") and m.get("id") == m.get("thread_id"):
            started_by_user.add(m["id"])
    out: dict[str, str] = {}
    for m in msgs or []:
        if not isinstance(m, dict) or "error" in m:
            continue
        tid = m.get("thread_id") or ""
        if not tid or tid in started_by_user:
            continue
        date_str = m.get("date") or ""
        # Gmail header `date` is RFC2822; convert to a comparable ISO
        # string. If parsing fails, fall back to lexical compare on the
        # raw string — still better than nothing, since dates within
        # the same window mostly sort correctly.
        iso = _parse_rfc2822_to_iso(date_str) or date_str
        if iso > out.get(tid, ""):
            out[tid] = iso
    return out


def _build_gmail_sent_index(account: str, days_back: int = 60) -> dict[str, str]:
    """For one Gmail account, return the reply index (_index_gmail_sent).

    One API call per account (Gmail query `in:sent newer_than:Nd`).
    Returns empty dict if the account isn't configured or the API errors.
    """
    try:
        import gmail_client  # local import: only when Gmail path is exercised
    except ImportError:
        return {}
    try:
        msgs = gmail_client.search_messages(
            account, f"in:sent newer_than:{days_back}d", max_results=100
        )
    except Exception:
        return {}
    if not isinstance(msgs, list):
        return {}
    return _index_gmail_sent(msgs)


def _parse_rfc2822_to_iso(date_str: str) -> str:
    """Best-effort RFC2822 → ISO8601 string. Returns '' on failure."""
    if not date_str:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _entry_iso(entry: dict, field: str = "date_identified") -> str:
    """Pull a normalized ISO timestamp from a ledger entry."""
    raw = entry.get(field) or ""
    if not raw:
        return ""
    # Ensure trailing 'Z' for lexical compare against Outlook timestamps
    if "T" in raw and not raw.endswith("Z") and "+" not in raw:
        return raw + "Z"
    return raw


def _entry_start(entry: dict) -> str:
    """When the entry's obligation began, for every "evidence after the
    entry" test: date_identified, or the latest human reopen when that is
    later. A reopen says the evidence up to then did not finish the work,
    so evidence older than it never closes the entry again."""
    start = _entry_iso(entry)
    reopened = _entry_iso(entry, "reopened_at")
    if start and reopened:
        return max(start, reopened)
    return start or reopened


# An out-of-office auto-reply says nothing about whether the work is done.
_NOT_EVIDENCE = frozenset({"inbound_ack:ooo"})

_REASON_KV_RE = re.compile(r"(\w+)=('[^']*'|[^,)\s]+)")


def _local_day(ts: str) -> str:
    """A UTC evidence time as the user's own calendar day ("Oct 15"): an
    evening reply in the Americas is already the next day in UTC."""
    dt = closure._parse(ts)
    if dt is None:
        return (ts or "")[:10]
    try:
        from zoneinfo import ZoneInfo
        import quiet_hours
        dt = dt.astimezone(ZoneInfo(quiet_hours.active_timezone()))
    except Exception:
        pass
    return f"{dt:%b} {dt.day}"


def proposal_why(signal: str, reason: str) -> str:
    """The proposal's line for the user, from a fired signal's reason."""
    kv = {k: v.strip("'") for k, v in _REASON_KV_RE.findall(reason or "")}
    subject, who = kv.get("subject", ""), kv.get("from") or kv.get("to") or ""
    sent = _local_day(kv.get("sent") or "")
    family, _, label = signal.partition(":")
    lines = {
        "user_replied_outlook": f"you replied in its thread on {sent}",
        "user_replied_gmail": f"you replied in its Gmail thread on {sent}",
        "inbound_ack": f"the latest message in its thread, from {who}, reads as "
                       f"{(label or 'an acknowledgement').replace('-', ' ')}",
        "confirmation_email": f"a confirmation came from {who}: {subject}",
        "calendar_event_match": f"your calendar has '{subject}'",
        "closed_sibling": "another item on its thread was closed",
        "sender_reply_match": f"you wrote to {who} on {sent}: {subject}",
        "project_member_completion": f"{who} wrote: {subject}",
        "meeting_response_sent": "you answered the invite it asks about",
        "identifier_siblings_closed": "every manuscript or proposal id it names was closed elsewhere",
    }
    return lines.get(family) or reason


_EVIDENCE_TS_RE = re.compile(r"\b(?:sent|ts|at)=(\d{4}-\d\d-\d\dT[\d:]+Z?)")


def _settle_entry(entry_id: str, signal: str, reason: str,
                  db_path: Path | None = None) -> str:
    """Hand one fired signal to the closure policy: it closes on decisive
    evidence and records a proposal otherwise. Returns the action
    ("closed", "proposed", "vetoed" or "skipped").

    Does NOT archive the source email: it may still be meaningful in the
    inbox, and the inline scrubber should not produce side effects that
    surprise the user mid-briefing-build.

    The ledger sits in the resident's state directory and this runs in
    the owner's briefing preprocess, so it opens through
    ledger.guarded_connect: a link or hard link at the database or a
    sidecar raises ledger.UnsafeDatabase and nothing is written.
    """
    path = db_path or ledger_mod.DB_PATH
    conn = ledger_mod.guarded_connect(path, timeout=10)
    try:
        prev = conn.execute("SELECT last_action FROM ledger WHERE id = ?",
                            (entry_id,)).fetchone()
        if prev is None:
            return "skipped"
        note = f"Auto-closed by closure_scrubber: {reason}"
        if prev[0]:
            note = f"{note}. Previous: {prev[0]}"
        m = _EVIDENCE_TS_RE.search(reason or "")
        res = closure.settle(conn, entry_id, signal=signal, actor="closure_scrubber",
                             note=note, why=proposal_why(signal, reason),
                             evidence={"reason": reason[:400]},
                             evidence_ts=(m.group(1) if m else ""))
        conn.commit()
        return res["action"]
    finally:
        conn.close()


def _append_jsonl(path: Path, records: list[dict]) -> None:
    """Append one JSON line per record. The logs are in the shared
    ~/.ea/logs, so an owner process (the briefing's preprocess) appends
    with the resident's access (resident_io). Best-effort: a failure costs
    one stderr line and never stops the scrub."""
    if not records:
        return
    try:
        text = "".join(json.dumps(rec, default=str) + "\n" for rec in records)
    except Exception:
        return
    _resident_io.append_text(path, text)


# ── Standing human vetoes ──────────────────────────────────────────────
# A human who reopens an entry the scrubber auto-closed has overruled
# that signal on that entry, permanently. Without a veto the scrubber
# closes it again on the next run, on the same evidence, forever. The
# veto is stored two ways so neither alone has to be right:
#   - structurally, in this file's own state: we recorded that we closed
#     X with signal S, and X is open again, so a human put it back.
#   - from the ledger text, so vetoes predating the state file are
#     honored on the first run after deploy.

# Signals whose closes are recorded so a human reopen becomes a standing
# veto. The token-matching signals are here because a human overruling a
# fuzzy match is a statement about that entry forever. The user_replied_*
# joins are here because an exact join to the wrong-shaped thread (the
# user's own outreach, say) re-fires on the same evidence every run, and
# a human reopen has to outrank it. closed_sibling joined them after its
# first false close on record: a journal manuscript row, a work item that
# shares its kickoff email's conversation, re-closed the day after a human
# reopened it. inbound_ack and confirmation_email stay unrecorded: no
# reopened false close of theirs is on record, and each addition belongs
# with a backtest.
_VETOABLE_SIGNALS: set[str] = {
    "calendar_event_match", "sender_reply_match", "closed_sibling",
    "project_member_completion",
    "user_replied_outlook", "user_replied_gmail",
    "meeting_response_sent", "identifier_siblings_closed",
}

_REOPEN_RE = re.compile(r"\breopen(?:ed|s|ing)?\b", re.IGNORECASE)
_AUTOCLOSE_HINTS = ("calendar_event_match", "closure_scrubber",
                    "auto-close", "auto-closed", "false-close",
                    "false closure", "false-closure")


def _history_has_reopen_veto(entry: dict, signal: str = "calendar_event_match") -> bool:
    """Does the entry's own text record a reopen of a scrubber auto-close?"""
    text = f"{entry.get('last_action') or ''} {entry.get('notes') or ''}"
    if not _REOPEN_RE.search(text):
        return False
    low = text.lower()
    return signal in low or any(h in low for h in _AUTOCLOSE_HINTS)


class VetoStore:
    """(entry_id, signal) vetoes plus the auto-closes that can become one."""

    def __init__(self, path: Path | None = None, data: dict | None = None):
        self.path = path or _veto_state_path()
        self.data = data if data is not None else {"version": 1, "closes": {},
                                                   "vetoes": {}}
        self.data.setdefault("closes", {})
        self.data.setdefault("vetoes", {})
        self.dirty = False

    @classmethod
    def load(cls, path: Path | None = None) -> "VetoStore":
        """The store at `path`. The state file is in resident space and
        `save` writes what was loaded back there, so the read goes through
        resident_io: an owner process reads it with the resident's access
        and never loads a file only the owner may read."""
        p = path or _veto_state_path()
        try:
            data = json.loads(_resident_io.read_text(p) or "")
            if isinstance(data, dict):
                return cls(p, data)
        except Exception:
            pass
        return cls(p)

    def is_vetoed(self, entry_id: str, signal: str) -> bool:
        rec = self.data["vetoes"].get(entry_id) or {}
        return signal in (rec.get("signals") or [])

    def add_veto(self, entry_id: str, signal: str, source: str) -> bool:
        if self.is_vetoed(entry_id, signal):
            return False
        rec = self.data["vetoes"].setdefault(
            entry_id, {"signals": [], "since": _utc_now(), "source": source})
        rec.setdefault("signals", []).append(signal)
        rec["source"] = source
        self.dirty = True
        return True

    def record_close(self, entry_id: str, signal: str) -> None:
        self.data["closes"][entry_id] = {"signal": signal, "ts": _utc_now()}
        self.dirty = True

    def note_reopen(self, entry_id: str, renewed_at: str = "") -> str:
        """The entry is open but we closed it: a human reopened it, unless a
        new message in its thread reopened it after the close (`renewed_at`,
        closure.note_renew): a new ask, and the close stood.

        Returns the vetoed signal name, or '' if we never closed it."""
        rec = self.data["closes"].pop(entry_id, None)
        if not rec:
            return ""
        self.dirty = True
        if renewed_at and renewed_at >= str(rec.get("ts") or ""):
            return ""
        signal = rec.get("signal") or ""
        if signal:
            self.add_veto(entry_id, signal, "reopened after auto-close")
        return signal

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            _atomic.write_json(self.path, self.data, indent=1, sort_keys=True)
            self.dirty = False
        except Exception:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Signal helpers ─────────────────────────────────────────────────────

def _forward_only_reason(entry: dict, msgs: list, entry_date: str) -> str:
    """Why the user's sent messages in this conversation are not a reply
    to the entry's correspondent, or '' when at least one of them is.

    A message is a reply when its recipients include the address the
    entry came from. A message with no recipient data at all (the time
    index built from older logs, a corpus world) is taken as a reply, as
    it always was. Only when every post-entry message names recipients
    and none of them names the correspondent is the set a forward: the
    person who asked has heard nothing.
    """
    addr = (entry.get("from_email") or "").strip().lower()
    if not addr:
        return ""
    seen: set[str] = set()
    considered = 0
    for m in msgs or []:
        ts = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        if entry_date and ts and ts <= entry_date:
            continue
        rcpts = _msg_recipients(m)
        if not rcpts or addr in rcpts:
            return ""
        considered += 1
        seen |= rcpts
    if not considered:
        return ""
    return (f"forward only: sent to {sorted(seen)[:4]}, "
            f"correspondent {addr} not among the recipients")


def _user_replied_outlook(entry: dict, sent_conv_times: dict[str, str],
                          sent_by_conv: dict[str, list] | None = None,
                          near_misses: list[dict] | None = None,
                          ) -> tuple[bool, str]:
    """Did the user send a reply in the same Outlook conversation after
    the ledger entry was created (or last reopened)? A row waiting on
    someone else's deliverable (ledger.is_waiting) is never closed by
    the user's own send.

    With `sent_by_conv` the sent messages are checked for the entry's
    correspondent among their recipients (`_forward_only_reason`); a
    forward of the request to third parties holds the entry open and is
    recorded in `near_misses`. Without it (the missed-closure autopsy,
    older callers) the time index alone decides, as before.
    """
    cid = entry.get("conversation_id") or ""
    if not cid or cid not in sent_conv_times:
        return (False, "")
    if ledger_mod.is_waiting(entry):
        return (False, "")
    last_sent = sent_conv_times[cid]
    entry_date = _entry_start(entry)
    if entry_date and last_sent <= entry_date:
        return (False, "")
    if sent_by_conv is not None:
        why = _forward_only_reason(entry, sent_by_conv.get(cid) or [], entry_date)
        if why:
            if near_misses is not None:
                near_misses.append({
                    "id": entry.get("id") or "",
                    "signal": "user_replied_outlook",
                    "event_subject": "",
                    "raw_overlap": [], "filtered_overlap": [],
                    "distinctive": [],
                    "suppressed_because": why,
                    "ts": _utc_now(),
                })
            return (False, "")
    return (True, f"user_replied_outlook (sent={last_sent})")


def _thread_has_inbound_before(msgs: list, account: str,
                               before_iso: str) -> bool:
    """Does the thread contain a message from someone OTHER than
    `account` that predates `before_iso`? That inbound message is what
    makes the user's later sent mail a reply. Alias sends from the same
    account are not detected; the account address is the identity."""
    acct = (account or "").lower()
    for m in msgs or []:
        sender = (m.get("from") or "").lower()
        if acct and acct in sender:
            continue
        iso = _parse_rfc2822_to_iso(m.get("date") or "") or (m.get("date") or "")
        if iso and iso < before_iso:
            return True
    return False


def _user_replied_gmail(entry: dict,
                        gmail_sent_indices: dict[str, dict[str, str]],
                        thread_fetch=None) -> tuple[bool, str]:
    """Did the user REPLY via Gmail in the same thread after the entry was
    created (or last reopened)? A waiting row never closes on the user's
    own send. A reply needs something to reply to: when `thread_fetch` is
    given, the thread must contain an inbound message (from someone other
    than the account) that predates the user's latest sent mail, or the
    signal holds.

    The sent-index id==thread_id prefilter alone cannot catch the common
    case: Gmail pins the thread id to the auto-saved DRAFT's message id,
    so a web- or draft-composed outreach is sent under a different id
    than the thread's. Only the thread itself says whether anything was
    ever answered.

    `gmail_sent_indices` is {account_email: {thread_id: latest_sent_iso}}.
    `thread_fetch(account, tid)` returns the thread's messages (each with
    `from` and RFC2822 `date`), or None when the thread cannot be read —
    an unverifiable thread does NOT close. Callers without thread data
    (the missed-closure autopsy) omit it and get the optimistic answer,
    which for "could this signal have seen it" is the right one.
    """
    tid = entry.get("gmail_thread_id") or ""
    if not tid or ledger_mod.is_waiting(entry):
        return (False, "")
    entry_date = _entry_start(entry)
    for account, idx in gmail_sent_indices.items():
        if tid not in idx:
            continue
        last_sent = idx[tid]
        if entry_date and last_sent <= entry_date:
            continue
        if thread_fetch is not None:
            msgs = thread_fetch(account, tid)
            if msgs is None:
                continue   # cannot verify -> hold
            if not _thread_has_inbound_before(msgs, account, last_sent):
                continue   # user-carried thread: outreach, not a reply
        return (True, f"user_replied_gmail (account={account}, sent={last_sent})")
    return (False, "")


def _closed_sibling_satisfies(entry: dict,
                              closed_conv_times: dict[str, str]
                              ) -> tuple[bool, str]:
    """Was ANOTHER ledger entry of this same conversation closed after this
    entry existed (or was last reopened)?

    One conversation often spawns several entries (priority-/response-/
    thread- rows). When the user closes or archives one, the survivors
    would otherwise keep getting briefed. Direction guard: only fire when
    the sibling closure happened AT/AFTER this entry's creation. An entry
    born after the closure may be a genuinely new ask and is left alone
    (the preprocessor's suppress_closed_conversations gate handles new
    derivations)."""
    # Only sweep the detector rows (ledger.DETECTOR_PREFIXES), the
    # duplicates this exists for. Every other row (a task from the
    # conversation, a staged draft, a wait on someone else) is its own
    # obligation that shares the thread, and a sibling's close says nothing
    # about it. A source_message_id does not tell them apart: the EA links
    # its own rows to their thread too.
    if not ledger_mod.is_detector_row(entry.get("id") or ""):
        return (False, "")
    if _etype(entry) == "draft_pending" or not entry.get("source_message_id"):
        return (False, "")
    cid = entry.get("conversation_id") or ""
    if not cid:
        return (False, "")
    closed_at = closed_conv_times.get(cid, "")
    if not closed_at:
        return (False, "")
    entry_date = _entry_start(entry)
    if entry_date and closed_at >= entry_date:
        return (True, f"closed_sibling (conversation closed {closed_at})")
    return (False, "")


def _entry_match_tokens(entry: dict) -> set[str]:
    """Token bag for fuzzy-matching this ledger entry against external
    artifacts (emails, calendar events).

    Includes description + from_name + from_email local-part + notes
    (truncated). Reason: many entries have generic descriptions like
    'Dissertation Defense Scheduling' where the actual identity-bearing
    token (the student name) lives in from_name. Without this, the
    calendar/confirmation signals match on noise and fire across unrelated
    items (e.g., student A's defense satisfying student B's scheduling
    entry)."""
    parts: list[str] = [
        entry.get("description") or "",
        entry.get("from_name") or "",
        # from_email local-part often carries the person's name
        ((entry.get("from_email") or "").split("@")[0] if entry.get("from_email") else ""),
        (entry.get("notes") or "")[:400],
    ]
    return _extract_content_tokens(" ".join(parts))


def _person_tokens(text: str) -> set[str]:
    """Tokens from a person-bearing field (a name, or an address local
    part). Address domains are dropped: a domain label is not a person."""
    if not text:
        return set()
    parts: list[str] = []
    for chunk in re.split(r"[\s,;<>]+", text):
        if "@" in chunk:
            parts.append(chunk.split("@", 1)[0])
        else:
            parts.append(chunk)
    return _extract_content_tokens(" ".join(parts))


_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _domain_label_tokens(text: str) -> set[str]:
    """Labels of every mail domain appearing in `text` ('gmail', 'sandia',
    'example', 'edu'). Structural: an address's domain says which mail
    system a person is on, never which obligation the text is about."""
    out: set[str] = set()
    for domain in _ADDRESS_RE.findall(text or ""):
        for label in domain.lower().split("."):
            if len(label) >= 3:
                out.add(label)
    return out


# Mail/institution labels that recur as bare words even when no address
# is nearby ("my campus colleagues", a visit named for the town). The
# address-derived set above catches them when an address is present;
# this covers the rest. The institution's own words come from the
# profile (preprocessor/institution.py boilerplate_tokens).
def _institution_words() -> set[str]:
    try:
        import institution
    except ImportError:
        from preprocessor import institution
    try:
        return set(institution.boilerplate_tokens())
    except Exception:
        return set()


_INSTITUTIONAL_LABELS: set[str] = {
    "gmail", "googlemail", "outlook", "hotmail", "yahoo", "icloud", "campus",
} | _institution_words()

# Calendar furniture, dropped only in the calendar signal. Two groups:
#   (1) the vocabulary observed producing false closures.
#   (2) the recurring-event words around them, which describe WHEN
#       something happens rather than WHAT it is about.
# 'campaign' is deliberately NOT here: it names a specific program
# ("the Field Robotics Outreach Campaign"), and the rarity test scores it
# correctly.
_CALENDAR_NOISE_TOKENS: set[str] = {
    # (1) observed false-close vocabulary
    "research", "undergrad", "undergraduate", "faculty", "lecture",
    "weekly", "group", "working", "planning", "draft", "drafts", "hold",
    "holds", "intro", "introduction", "calendar", "answer", "answers",
    # (2) recurring-event / calendar furniture
    "annual", "biweekly", "monthly", "recurring", "series", "session",
    "sessions", "standup", "kickoff", "checkin", "catchup", "office",
    "hours", "zoom", "teams", "room", "block", "busy", "tentative",
    "cancelled", "canceled", "invite", "invitation", "agenda", "team",
    "visit", "seminar", "colloquium", "workshop", "talk", "class",
    "course", "meet", "chat", "coffee", "breakfast", "lunch", "dinner",
    "fall", "spring", "summer", "winter", "semester", "week", "time",
    "date", "dates", "slot", "slots", "invitee", "attendees",
}

# Events that never close anything. `[note]` marks an event that records
# WHEN something happens without claiming the user's time, and the
# wh/nm/p0/p1/p2/wr tokens are the solver's own preference events on the
# EA-Managed calendar. scheduling_engine already treats both as
# non-blocking; neither is evidence that an obligation was met.
_NOTE_MARKER = "[note]"
_EVENT_SOLVER_TOKENS: set[str] = {"wh", "nm", "p0", "p1", "p2", "wr"}


def _event_is_non_binding(subject: str) -> bool:
    """Mirror of scheduling_engine._is_note_event / _parse_token."""
    s = (subject or "").strip()
    if _NOTE_MARKER in s.lower():
        return True
    head = s.split(None, 1)[0].lower() if s else ""
    return head in _EVENT_SOLVER_TOKENS


def _is_wordless_token(token: str) -> bool:
    """'2026', '2026-08-27', '1030' — a number carries no topic."""
    return not any(c.isalpha() for c in token)


def build_token_df(entries: list[dict]) -> dict[str, int]:
    """Document frequency of each match-token across the open ledger.

    One pass, reused for every (entry, event) pair in the run."""
    df: dict[str, int] = {}
    for e in entries or []:
        for t in _entry_match_tokens(e):
            df[t] = df.get(t, 0) + 1
    return df


def _rare_df_ceiling(corpus_size: int) -> int:
    return max(_RARE_DF_MAX_ABS, int(_RARE_DF_FRACTION * corpus_size))


def _filter_overlap(overlap: set[str], domain_labels: set[str], *,
                    extra_noise: set[str] = frozenset()) -> set[str]:
    """Drop tokens that cannot identify an obligation: generic noise,
    bare numbers, institution labels, and mail-domain labels.

    `extra_noise` is the per-signal vocabulary; the calendar signal
    passes its furniture list, the mail signals pass nothing extra."""
    return {
        t for t in overlap
        if t not in _GENERIC_NOISE_TOKENS
        and t not in extra_noise
        and t not in _INSTITUTIONAL_LABELS
        and t not in domain_labels
        and not _is_wordless_token(t)
    }


def _filter_calendar_overlap(overlap: set[str], domain_labels: set[str]) -> set[str]:
    return _filter_overlap(overlap, domain_labels,
                           extra_noise=_CALENDAR_NOISE_TOKENS)


def _distinctive_overlap(filtered: set[str],
                         *,
                         token_df: dict[str, int] | None,
                         corpus_size: int,
                         entry_person: set[str],
                         event_person: set[str]) -> set[str]:
    """The subset of `filtered` that identifies THIS obligation.

    Two ways to qualify:
      - person-bound: the token names a person who is both the entry's
        correspondent (from_name / from_email local part) and on the
        event (organizer or attendee). The event is with the person who
        owns the obligation.
      - rare: document frequency in the open ledger is at or below the
        ceiling. A word the user's ledger says once is about one thing.
    """
    bound = filtered & entry_person & event_person
    if token_df is None or corpus_size < _RARE_MIN_CORPUS:
        # No usable corpus (tests, tiny ledgers): rarity is unmeasurable,
        # so every surviving token counts. The 2-token floor still holds.
        return set(filtered)
    ceiling = _rare_df_ceiling(corpus_size)
    rare = {t for t in filtered if token_df.get(t, 0) <= ceiling}
    return bound | rare


def _prepare_events(events: list) -> list[dict]:
    """Tokenize each event once per run instead of once per ledger entry."""
    prepared: list[dict] = []
    for ev in events or []:
        subject = ev.get("subject") or ""
        organizer = ((ev.get("organizer") or {}).get("emailAddress") or {})
        organizer_text = ((organizer.get("name") or "") + " "
                          + (organizer.get("address") or ""))
        attendee_text = " ".join(
            ((a.get("emailAddress") or {}).get("name") or "") + " " +
            ((a.get("emailAddress") or {}).get("address") or "")
            for a in (ev.get("attendees") or [])
        )
        people_text = f"{organizer_text} {attendee_text}"
        ev_text = f"{subject} {people_text}"
        prepared.append({
            "subject": subject,
            "start": ((ev.get("start") or {}).get("dateTime") or ""),
            "tokens": _extract_content_tokens(ev_text),
            # The subject alone, so we can tell "the event is ABOUT this
            # person" (subject 'Gus / Kit') from "this person is one
            # of twenty attendees" (subject 'PLEASE HOLD for Review
            # Panel Meeting'). See _calendar_event_satisfies.
            "subject_tokens": _extract_content_tokens(subject),
            "person_tokens": _person_tokens(people_text),
            "domain_labels": _domain_label_tokens(ev_text),
            "non_binding": _event_is_non_binding(subject),
        })
    return prepared


def _deliverable_entry(entry: dict) -> bool:
    """Is this entry something the user must PRODUCE rather than answer?

    Two tests, either sufficient: a deliverable type, or a hard deadline
    on the row. The deadline test matters on its own because the type
    vocabulary drifts (a 'scheduling' row can still carry an RSVP-by
    date), and it is the field whose loss the user actually feels.
    """
    if _etype(entry) in _DELIVERABLE_TYPES:
        return True
    return bool((entry.get("deadline") or "").strip())


def _person_only_evidence(distinctive: set[str],
                          entry_person: set[str],
                          other_person: set[str]) -> bool:
    """Is every distinctive token just somebody's name?

    A person's name in both a ledger entry and an unrelated artifact
    says the two share a human, never that the work is done. One person
    spelled twice ('sriram' plus 'sankaranarayanan') clears both the
    two-token floor and the rarity test, so neither of those catches it.
    """
    return not (distinctive - entry_person - other_person)


def _calendar_event_satisfies(entry: dict,
                              events: list,
                              *,
                              prepared: list[dict] | None = None,
                              token_df: dict[str, int] | None = None,
                              corpus_size: int = 0,
                              vetoed: bool | None = None,
                              near_misses: list[dict] | None = None,
                              ) -> tuple[bool, str]:
    """Does any calendar event satisfy a scheduling-shaped ledger entry?

    Signal fires when ALL of:
      - the entry is scheduling-shaped by TYPE or by a keyword in its
        DESCRIPTION (notes do not count, see `_is_scheduling_entry`)
      - no standing human veto exists for (entry, calendar_event_match)
      - the event is not a `[note]` record or a solver token event
      - the event starts after the entry's date_identified
      - subject/organizer/attendee tokens overlap the entry's
        match-tokens in >= MIN_CALENDAR_OVERLAP_TOKENS tokens AFTER
        dropping generic noise, calendar furniture, bare numbers and
        mail-domain labels
      - at least one surviving token is DISTINCTIVE: person-bound to the
        entry's correspondent, or rare in the ledger corpus
      - the entry is not a deliverable (see `_deliverable_entry`)
      - the distinctive evidence is not a person's name alone (see
        `_person_only_evidence`)

    Every match the looser legacy gate would have closed and this one
    does not is appended to `near_misses` (written to
    closure-scrubber-suppressed.log) so the next tuning pass has data.
    """
    if not events:
        return (False, "")

    if vetoed is None:
        # Callers that don't track vetoes (the missed-closure autopsy)
        # still must not be told this signal would fire on a vetoed entry.
        vetoed = (_history_has_reopen_veto(entry)
                  or VetoStore.load().is_vetoed(entry.get("id") or "",
                                                "calendar_event_match"))

    audit_only = False
    if not _is_scheduling_entry(entry):
        if near_misses is None or not _was_scheduling_entry_legacy(entry):
            return (False, "")
        # The old notes-reading gate would have let this entry through.
        # Walk it anyway, but only to record what was suppressed.
        audit_only = True

    desc_tokens = _entry_match_tokens(entry)
    if not desc_tokens:
        return (False, "")

    entry_person = (_person_tokens(entry.get("from_name") or "")
                    | _person_tokens(entry.get("from_email") or ""))
    entry_domains = _domain_label_tokens(
        (entry.get("from_email") or "") + " " + (entry.get("notes") or "")[:400])

    entry_date = _entry_start(entry)
    prepared = prepared if prepared is not None else _prepare_events(events)
    logged = 0
    for ev in prepared:
        raw_overlap = (desc_tokens & ev["tokens"]) - _GENERIC_NOISE_TOKENS
        if not raw_overlap:
            continue
        # Compare time: prefer event start, fall back to created-after if present.
        if entry_date and ev["start"] and ev["start"] < entry_date:
            # event predates the entry — was already on the calendar
            # before the entry was created, so it can't "satisfy" it.
            continue

        filtered = _filter_calendar_overlap(
            raw_overlap, entry_domains | ev["domain_labels"])
        distinctive = _distinctive_overlap(
            filtered, token_df=token_df, corpus_size=corpus_size,
            entry_person=entry_person, event_person=ev["person_tokens"])

        why = ""
        if audit_only:
            why = "entry not scheduling-shaped by description"
        elif vetoed:
            why = "standing human veto (reopened after a prior auto-close)"
        elif ev["non_binding"]:
            why = "event is a [note] record or a solver token event"
        elif len(filtered) < MIN_CALENDAR_OVERLAP_TOKENS:
            why = (f"{len(filtered)} usable overlap token(s) "
                   f"(< {MIN_CALENDAR_OVERLAP_TOKENS})")
        elif not distinctive:
            why = "no distinctive token (all common in the ledger corpus)"
        elif _deliverable_entry(entry):
            why = ("entry is a deliverable (type "
                   f"{(entry.get('type') or '?')}"
                   f"{', deadline ' + entry['deadline'] if entry.get('deadline') else ''}"
                   "); a meeting about it is not it")
        elif _person_only_evidence(distinctive, entry_person,
                                   ev["person_tokens"]):
            # One concession: a scheduling-TYPED entry closed by an
            # event whose own SUBJECT names the person ('Bo
            # dissertation defense', 'Gus / Kit'). That is the case
            # the signal exists for, the meeting the entry asked for
            # landing on the calendar. Being one attendee among twenty
            # on somebody else's committee meeting is not.
            #
            # The TYPE test, not `_is_scheduling_entry`, because the
            # keyword path is where the false friends live: "Fall
            # Student Appointment Request Form" is scheduling-shaped by
            # the word 'appointment' and a 'Zev / Kit' 1:1 is not
            # its completion.
            named_in_subject = (distinctive & ev["person_tokens"]
                                & ev["subject_tokens"])
            if not (named_in_subject
                    and _etype(entry) in _SCHEDULING_TYPES):
                why = ("person-name overlap only "
                       f"({sorted(distinctive)[:4]})")

        if not why:
            sample = sorted(distinctive | filtered)[:5]
            return (True,
                    f"calendar_event_match (subject={ev['subject'][:60]!r}, "
                    f"overlap={sample})")

        if near_misses is not None and logged < 5:
            logged += 1
            near_misses.append({
                "id": entry.get("id") or "",
                "signal": "calendar_event_match",
                "event_subject": ev["subject"][:80],
                "raw_overlap": sorted(raw_overlap)[:8],
                "filtered_overlap": sorted(filtered)[:8],
                "distinctive": sorted(distinctive)[:8],
                "suppressed_because": why,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    return (False, "")


def _plugin_mail_rules() -> dict:
    """Mail rules from enabled plugins (docs/plugins.md): extra
    confirmation senders, and senders that never count as one. Empty
    outside an owner process or when no plugin declares any."""
    try:
        try:
            import plugin_registry as _plugins
        except ModuleNotFoundError:
            from preprocessor import plugin_registry as _plugins
        return _plugins.mail_rules()
    except Exception:
        return {}


def is_confirmation_sender(addr: str, rules: dict | None = None) -> bool:
    """True when mail from `addr` (lowercased) comes from a confirmation
    system: a core substring or a plugin's, and no plugin excludes it."""
    rules = _plugin_mail_rules() if rules is None else rules
    if any(s in addr for s in rules.get("closure_exclude_senders") or []):
        return False
    return (any(s in addr for s in CONFIRMATION_SENDER_SUBSTRINGS)
            or any(s in addr for s in rules.get("closure_confirm_senders") or []))


def _confirmation_email_satisfies(entry: dict,
                                  inbox: list) -> tuple[bool, str]:
    """Did a recent inbound from a confirmation-system sender close this?

    Signal fires when ALL of:
      - sender address contains one of CONFIRMATION_SENDER_SUBSTRINGS
      - the message was received after the entry's date_identified
      - its subject or body_preview ALSO classifies as an acknowledgment
        via `acknowledgment_patterns.classify` (or carries a confirmation
        marker — see CONFIRMATION_MARKERS regex). This guard prevents
        false-closes from program-officer addresses at the same domain
        ('@nsf.gov' covers both noreply.events and real PO emails).
      - subject/preview tokens overlap the entry's description by at least
        MIN_CONFIRMATION_OVERLAP_TOKENS distinct *content* tokens (generic
        scheduling/registration noise words like "meeting", "form",
        "june", "registration" are dropped before the count).

    Distinct from `_user_replied_outlook` (same conversation only) and
    `_latest_inbound_is_ack` (regex on latest reply in entry's own
    conversation). This one catches CROSS-conversation completion:
    an Easychair confirmation arrives in a new thread but its subject
    references the manuscript title that's in our ledger entry.
    """
    if not inbox:
        return (False, "")
    desc_tokens = _entry_match_tokens(entry)
    if not desc_tokens:
        return (False, "")
    entry_date = _entry_start(entry)

    rules = _plugin_mail_rules()
    for msg in inbox:
        addr = ((msg.get("from") or {}).get("emailAddress", {})
                .get("address", "") or "").lower()
        if not is_confirmation_sender(addr, rules):
            continue
        received = msg.get("receivedDateTime") or ""
        if entry_date and received and received < entry_date:
            continue
        # Require BOTH topic-token overlap AND ack-shape, so we don't
        # false-close real action-asking emails from a program officer.
        subject = msg.get("subject") or ""
        preview = msg.get("bodyPreview") or ""
        is_ack_shaped = (
            acknowledgment_patterns.classify(subject)[0]
            or acknowledgment_patterns.classify(preview)[0]
            or _CONFIRMATION_MARKERS.search(subject)
            or _CONFIRMATION_MARKERS.search(preview[:200])
        )
        if not is_ack_shaped:
            continue
        msg_text = subject + " " + preview
        msg_tokens = _extract_content_tokens(msg_text)
        overlap = (desc_tokens & msg_tokens) - _GENERIC_NOISE_TOKENS
        if len(overlap) < MIN_CONFIRMATION_OVERLAP_TOKENS:
            continue
        sample = sorted(overlap)[:5]
        return (True,
                f"confirmation_email (from={addr}, "
                f"subject={subject[:60]!r}, "
                f"overlap={sample})")
    return (False, "")


# Subjects/previews that signal confirmation even when ack-regex doesn't
# fire (e.g. "Confirmation: …", "Order #1234", "Receipt for your payment").
_CONFIRMATION_MARKERS = re.compile(
    r"\b(confirm(ed|ation)|receipt|registered|registration|"
    r"successfully|complete[d]?\b|order\s*#?\d+|"
    r"thank\s+you\s+for\s+(your\s+)?(submission|registration|payment))\b",
    re.IGNORECASE,
)

# Generic high-frequency words to drop from token-overlap matching so
# that one shared "meeting" or "june" doesn't auto-close obligations.
# Kept narrow — these are words that recur across many entries AND
# across many emails, so an overlap on them carries no signal.
_GENERIC_NOISE_TOKENS: set[str] = {
    # Calendar/date vocabulary
    "meeting", "form", "june", "july", "august", "september", "october",
    "november", "december", "january", "february", "march", "april", "may",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "morning", "afternoon", "evening", "tomorrow", "today",
    "schedule", "scheduling", "appointment", "call", "sync",
    "discuss", "discussion", "review", "feedback",
    "panel", "committee", "request", "needed", "deadline",
    "update", "reminder", "information",
    # Academic-scheduling vocabulary — recurs across many student events
    # and would otherwise wrong-close one student's scheduling against
    # another student's calendar event.
    "defense", "dissertation", "thesis", "exam", "qualifying",
    "preliminary", "proposal", "candidacy", "comprehensive",
    "phd", "graduate", "student",
    # Email-banner vocabulary. Both a stored body preview in an entry's
    # notes and an inbound preview carry the "CAUTION: This email
    # originated from outside..." banner, so two unrelated texts overlap
    # on ['caution', 'email', 'external'].
    "caution", "external", "email", "sender", "originated", "outside",
    "link", "links", "click", "clicking", "attachments", "unless",
    "recognize", "content", "safe",
    # Residue of the same banner, and the registration vocabulary around
    # it. closure_metrics counts these as generic too, so leaving them
    # out here makes the actor looser than the detector.
    "note", "registration", "register",
}


def _latest_inbound_is_ack(entry: dict, inbox_by_conv: dict[str, list]) -> tuple[bool, str]:
    """Does the latest inbound message in the ledger entry's conversation
    look like an acknowledgment of a prior action?
    """
    cid = entry.get("conversation_id") or ""
    msgs = inbox_by_conv.get(cid) or []
    if not msgs:
        return (False, "")
    latest = msgs[0]
    body = latest.get("bodyPreview") or latest.get("body_preview") or ""
    subject = latest.get("subject") or ""
    # The classifier checks the preview body. Subject lines like
    # "Re: thanks for your submission" are rarely on their own enough
    # to close; we want body-level confirmation.
    is_ack, label = acknowledgment_patterns.classify(body)
    if not is_ack:
        # Fall back: check the subject line in isolation.
        is_ack, label = acknowledgment_patterns.classify(subject)
    if not is_ack:
        return (False, "")
    sender = (latest.get("from") or {}).get("emailAddress", {}).get("address", "") or ""
    return (True, f"inbound_ack:{label} from={sender}")


# ── Signals for entries with no conversation id ────────────────────────
# Every signal above joins on `conversation_id` or `gmail_thread_id`.
# Hand-created entries (a task the user dictated in Slack, a deadline the
# EA wrote down) carry neither, which is most of the open ledger. Two
# joins reach them, and they cover different parts of that population:
#
#   sender_reply_match        : the entry carries a from_email, so the
#     recipient address of the user's own sent mail is the join.
#   project_member_completion : the entry is the sole open row of a
#     project that names its people, so curated project membership is
#     the join.
#
# Description-token matching against any human's completion mail was
# built and REJECTED: the message reporting a completion often shares
# zero content tokens with the entry, so no threshold both finds it and
# excludes the junk. Rows with neither a from_email nor a project link
# still have no signal. That is a known gap, not a solved one.
#
# Both signals here are the same kind of fuzzy attack surface as the
# calendar signal, so both are scoped to entries no other signal can
# reach, refuse plugin-owned rows, and honour standing vetoes.


def _user_addresses() -> set[str]:
    """The user's own mail addresses, lowercased. Used to keep the user's
    own filed copies out of the inbound-evidence signals."""
    cfg = _ea_dir() / "config" / "user-profile.json"
    out: set[str] = set()
    try:
        data = json.loads(cfg.read_text())
    except Exception:
        return out
    for key in ("email", "work_email", "personal_email"):
        val = (data.get(key) or "").strip().lower()
        if val:
            out.add(val)
    for val in data.get("gmail_accounts") or []:
        if isinstance(val, str) and val.strip():
            out.add(val.strip().lower())
    return out


def _msg_recipients(msg: dict) -> set[str]:
    """Every To/CC/BCC address on a message, lowercased."""
    out: set[str] = set()
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for rec in msg.get(field) or []:
            addr = ((rec.get("emailAddress") or {}).get("address") or "")
            addr = addr.strip().lower()
            if addr:
                out.add(addr)
    return out


def _msg_people_text(msg: dict) -> str:
    """Name + address text for everyone on a message, for person tokens."""
    parts: list[str] = []
    frm = ((msg.get("from") or {}).get("emailAddress") or {})
    parts.append((frm.get("name") or "") + " " + (frm.get("address") or ""))
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for rec in msg.get(field) or []:
            ea = (rec.get("emailAddress") or {})
            parts.append((ea.get("name") or "") + " " + (ea.get("address") or ""))
    return " ".join(parts)


def _blind_entry(entry: dict) -> bool:
    """Can no conversation-joined signal ever reach this entry?

    Both signals below are scoped to these. An entry WITH a conversation
    id already has three signals watching it; giving it two more fuzzy
    ones would add false-close risk and no recall.
    """
    return not (entry.get("conversation_id") or "").strip()


# The ff-watcher plugin owns its docket rows and closes them from the
# court docket. Nothing in the mailbox is evidence about them, and they
# are numerous enough to dominate any token-frequency estimate a caller
# that included them would build. Excluded by prefix.
_PLUGIN_OWNED_TYPE_PREFIXES = ("ff_",)


def _plugin_owned(entry: dict) -> bool:
    t = (entry.get("type") or "").lower()
    return any(t.startswith(p) for p in _PLUGIN_OWNED_TYPE_PREFIXES)


# Outlook writes these subjects itself when the user clicks a meeting
# button or an auto-reply fires. They are not the user answering
# anybody, so accepting one meeting must not close a row asking for an
# RSVP to a different one.
_AUTO_SUBJECT_PREFIXES = (
    "accepted:", "declined:", "tentative:", "tentatively accepted:",
    "canceled:", "cancelled:", "automatic reply:", "out of office",
    "undeliverable:", "read:", "not read:",
)


def _is_auto_generated_subject(subject: str) -> bool:
    s = (subject or "").strip().lower()
    return s.startswith(_AUTO_SUBJECT_PREFIXES)


# A "First Last" pair as written in a ledger description ("confirm
# Vic Okonkwo's participation"). Only a pair that equals a
# recipient's display name on the user's own sent mail counts, so the
# regex can stay loose: "Broad Area" never matches anybody's To line.
_NAME_PAIR_RE = re.compile(
    r"\b([A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?)\s+([A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?)(?=['’]s\b|\b)")


def _named_people(text: str) -> set[str]:
    """Lowercased 'first last' pairs named in `text`, minus the user."""
    user_toks = _user_tokens()
    out: set[str] = set()
    for first, last in _NAME_PAIR_RE.findall(_strip_email_banners(text or "")):
        if first.lower() in user_toks or last.lower() in user_toks:
            continue
        out.add(f"{first.lower()} {last.lower()}")
    return out


def _recipient_named(msg: dict, names: set[str]) -> tuple[str, str]:
    """(address, display name) of the first To/CC/BCC recipient whose
    display name is one of `names` ('Last, First' accepted), or ('', '')."""
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for rec in msg.get(field) or []:
            ea = rec.get("emailAddress") or {}
            name = " ".join((ea.get("name") or "").split())
            if "," in name:
                last, _, first = name.partition(",")
                name = f"{first.strip()} {last.strip()}"
            low = name.lower()
            if low and low in names:
                addr = (ea.get("address") or "").strip().lower()
                if addr:
                    return addr, name
    return "", ""


def _user_replied_to_correspondent(entry: dict,
                                   sent: list,
                                   *,
                                   token_df: dict[str, int] | None = None,
                                   corpus_size: int = 0,
                                   vetoed: bool = False,
                                   near_misses: list[dict] | None = None,
                                   ) -> tuple[bool, str]:
    """Did the user write to this entry's correspondent about this topic?

    `_user_replied_outlook` answers the same question by conversation id
    and returns False on its first line when the entry has none. This
    joins on the recipient address instead, and pays for the looser key
    with a topic test:

      - the entry has no conversation id, has a from_email, and is
        CORRESPONDENCE-shaped by type (`_REPLY_SHAPED_TYPES`)
      - no standing veto for (entry, sender_reply_match)
      - some sent message lists that address in To/CC/BCC and was sent
        after the entry's date_identified
      - that message is something the user WROTE, not an Outlook
        meeting-response or auto-reply (`_AUTO_SUBJECT_PREFIXES`)
      - its SUBJECT shares at least one DISTINCTIVE token with the entry
        that is not the correspondent's own name

    One topic token is the floor here, where the calendar signal needs
    two: the address is already a hard join, so the token only has to
    say which of that person's threads this is. Names do not count,
    because the address said that first, and saying it twice is not two
    facts.

    `_deliverable_entry` is deliberately NOT applied. A reply-shaped row
    can carry a reply-BY date ('RSVP by Tuesday'), and answering it is
    what discharges the row. The type allowlist is what keeps real
    deliverables out.
    """
    addr = (entry.get("from_email") or "").strip().lower()
    if not sent or not _blind_entry(entry):
        return (False, "")
    if _plugin_owned(entry):
        return (False, "")
    if _etype(entry) not in _REPLY_SHAPED_TYPES:
        return (False, "")
    # No address on the row: a person NAMED in the description can stand
    # in for it, provided the user's sent mail lists that exact display
    # name. A row asking to "confirm Vic Okonkwo's participation"
    # is answered by mail addressed To Vic Okonkwo.
    named: set[str] = set()
    if not addr:
        named = _named_people(entry.get("description") or "")
        if not named:
            return (False, "")

    desc_tokens = _entry_match_tokens(entry)
    if not desc_tokens:
        return (False, "")
    entry_person = (_person_tokens(entry.get("from_name") or "")
                    | _person_tokens(addr))
    entry_domains = _domain_label_tokens(addr)
    entry_date = _entry_start(entry)

    # NOT guarded on "did they write back afterwards". The types this
    # signal accepts are per-MESSAGE rows: the preprocessor mints a fresh
    # response_needed / priority_alert row for each new inbound, so a
    # later message from the same person is a NEW obligation with its own
    # row, not evidence that this one is unfinished. A guard on later
    # inbound suppresses the true positives instead, because a courtesy
    # reply is not an ack the classifier recognises, and widening that
    # classifier would loosen signal 3 for everyone.

    logged = 0
    for msg in sent:
        hit_addr, hit_name = addr, ""
        if addr:
            if addr not in _msg_recipients(msg):
                continue
        else:
            hit_addr, hit_name = _recipient_named(msg, named)
            if not hit_addr:
                continue
        ts = msg.get("sentDateTime") or msg.get("receivedDateTime") or ""
        if entry_date and ts and ts < entry_date:
            continue
        subject = msg.get("subject") or ""
        if _is_auto_generated_subject(subject):
            continue
        people_text = _msg_people_text(msg)
        raw = (desc_tokens & _extract_content_tokens(subject)) - _GENERIC_NOISE_TOKENS
        filtered = _filter_overlap(
            raw, entry_domains | _domain_label_tokens(people_text))
        msg_person = _person_tokens(people_text)
        this_person = entry_person | _person_tokens(hit_name) | _person_tokens(hit_addr)
        distinctive = _distinctive_overlap(
            filtered, token_df=token_df, corpus_size=corpus_size,
            entry_person=this_person, event_person=msg_person)
        topical = distinctive - this_person - msg_person

        why = ""
        if vetoed:
            why = "standing human veto (reopened after a prior auto-close)"
        elif not topical:
            why = (f"no distinctive non-person subject token "
                   f"({sorted(filtered)[:4]})")

        if not why:
            via = f", named={hit_name!r}" if hit_name else ""
            return (True,
                    f"sender_reply_match (to={hit_addr}{via}, sent={ts}, "
                    f"subject={subject[:60]!r}, overlap={sorted(topical)[:4]})")

        if near_misses is not None and logged < 5 and raw:
            logged += 1
            near_misses.append({
                "id": entry.get("id") or "",
                "signal": "sender_reply_match",
                "event_subject": subject[:80],
                "raw_overlap": sorted(raw)[:8],
                "filtered_overlap": sorted(filtered)[:8],
                "distinctive": sorted(topical)[:8],
                "suppressed_because": why,
                "ts": _utc_now(),
            })
    return (False, "")


# An explicit statement that the work is finished, read from the SUBJECT.
# Deliberately narrower than both `acknowledgment_patterns.classify`
# (which accepts polite receipt: "got it", "we'll be in touch") and
# `_CONFIRMATION_MARKERS` (which accepts the bare word "registration").
#
# Subject-only is the load-bearing part, and it inverts what
# `_latest_inbound_is_ack` does on purpose. That signal distrusts a
# subject and prefers the body, because it is already inside the right
# conversation and only has to read the newest turn. This one has no
# conversation to stand in, so it needs the message to be ABOUT the
# completion, and the subject is the field a sender writes to say what a
# message is about. A body-only match is a passing reference to some
# OTHER thing finishing ("the prequal has been approved" on a thread
# about the full application), and each such match closes a live
# obligation.
_COMPLETION_ASSERTION = re.compile(
    r"\bsubmitted!"
    r"|\b(?:we|it|that|this|everything|application|proposal|form|package)\s+"
    r"(?:is|are|was|were|'re|'s)\s+(?:now\s+)?"
    r"(?:submitted|filed|approved|complete|completed|done|final)\b"
    r"|\b(?:has|have|had)\s+been\s+"
    r"(?:submitted|filed|approved|uploaded|processed|completed|accepted)\b"
    r"|\bsuccessfully\s+(?:submitted|filed|registered|completed|processed)\b"
    r"|\bi\s+(?:have\s+)?(?:just\s+)?(?:submitted|filed|uploaded)\b"
    r"|\bmarked\s+my\s+approval\b",
    re.IGNORECASE,
)


def build_project_index(open_entries: list[dict],
                        db_path: Path | None = None) -> dict[str, tuple]:
    """{entry_id: (project_id, member_addresses)} for entries that are the
    SOLE open obligation of a project that names its people.

    The project store (`project_store.py`) is hand-curated relational
    memory over this same database: which people are on which project and
    which ledger rows belong to it. That makes it the one join key an
    entry with no conversation id has that is not a guess about words.

    Sole-open-row is the disambiguation rule and it does real work: a
    project carrying eight open rows and two members would see one "the
    workshop is set" from either member close all eight. When a project
    has exactly one open obligation, "it is done" can only mean that one.

    Returns {} on any error: an older database with no project tables
    must degrade to "this signal does not apply", never to an exception
    inside the pre-briefing pass. The read opens read-only through
    ledger.guarded_connect, so a database the guard refuses
    (ledger.UnsafeDatabase) degrades the same way, and a missing one is
    never created.
    """
    open_ids = {e.get("id") for e in open_entries or [] if e.get("id")}
    if not open_ids:
        return {}
    path = db_path or ledger_mod.DB_PATH
    try:
        conn = ledger_mod.guarded_connect(path, timeout=10, read_only=True)
        try:
            links = list(conn.execute(
                "SELECT project_id, ledger_id FROM project_ledger"))
            people = list(conn.execute(
                "SELECT project_id, email FROM project_people"))
        finally:
            conn.close()
    except Exception:
        return {}

    members: dict[str, set[str]] = {}
    for pid, email in people:
        addr = (email or "").strip().lower()
        if addr:
            members.setdefault(pid, set()).add(addr)

    open_by_project: dict[str, list[str]] = {}
    projects_of: dict[str, set[str]] = {}
    for pid, lid in links:
        if lid in open_ids:
            open_by_project.setdefault(pid, []).append(lid)
            projects_of.setdefault(lid, set()).add(pid)

    out: dict[str, tuple] = {}
    for pid, ids in open_by_project.items():
        if len(ids) != 1 or not members.get(pid):
            continue
        entry_id = ids[0]
        # An entry filed under two projects has two answers to "who is on
        # this". Take neither.
        if len(projects_of.get(entry_id) or ()) != 1:
            continue
        out[entry_id] = (pid, frozenset(members[pid]))
    return out


# ── Signal 9: the user answered the meeting invite itself ─────────────
# An RSVP-shaped row is discharged the moment Outlook sends "Accepted:",
# "Declined:" or "Tentative:" for that invite. Signal 7 refuses those
# subjects on purpose (accepting an unrelated meeting is not a reply), so
# without this signal a hand-made row ABOUT the invite has none at all.
_RSVP_SHAPE_RE = re.compile(
    r"\brsvp\b|not\s*responded|my_response|response\s*=\s*not|"
    r"\b(?:accept|decline|respond\s+to|reply\s+to)\b[^.]{0,60}\binvit(?:e|ation)\b|"
    r"\binvit(?:e|ation)\b[^.]{0,60}\b(?:accept|decline|respond|reply|rsvp)",
    re.IGNORECASE)
_MEETING_RESPONSE_PREFIXES = ("accepted:", "declined:", "tentative:",
                              "tentatively accepted:")


def _loose_text(s: str) -> str:
    """Lowercase, punctuation to spaces, one space between words: the
    form in which an invite title is looked for inside a row's text."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def _meeting_response_satisfies(entry: dict,
                                sent: list,
                                *,
                                vetoed: bool = False,
                                near_misses: list[dict] | None = None,
                                ) -> tuple[bool, str]:
    """Did the user click Accept/Decline/Tentative on the invite this
    row asks about?

      - the row has no conversation id (an id-ful invite row is closed
        by signal 1: the response lands in the invite's conversation)
      - the row is RSVP-shaped (`_RSVP_SHAPE_RE` on description + notes)
      - a sent message after the row's creation carries a meeting-response
        prefix, and its title appears VERBATIM in the row's text (case,
        whitespace and punctuation aside)

    Verbatim, not token overlap: an "Accepted: Nico-Kit Tag Up - Lab
    Sensing Program" shares three tokens with a row asking for an RSVP to
    a sponsor's lab visit for the same program, and accepting a tag-up is
    not answering that invite. A row about an invite quotes the invite.
    """
    if not sent or not _blind_entry(entry) or _plugin_owned(entry):
        return (False, "")
    if _etype(entry) == "draft_pending":
        return (False, "")
    text = f"{entry.get('description') or ''} {(entry.get('notes') or '')[:400]}"
    if not _RSVP_SHAPE_RE.search(text):
        return (False, "")
    desc_tokens = _entry_match_tokens(entry)
    if not desc_tokens:
        return (False, "")
    entry_date = _entry_start(entry)
    text_norm = _loose_text(text)

    logged = 0
    for msg in sent:
        subject = (msg.get("subject") or "").strip()
        low = subject.lower()
        prefix = next((p for p in _MEETING_RESPONSE_PREFIXES if low.startswith(p)), "")
        if not prefix:
            continue
        ts = msg.get("sentDateTime") or msg.get("receivedDateTime") or ""
        if entry_date and ts and ts < entry_date:
            continue
        title = subject[len(prefix):].strip()
        title_norm = _loose_text(title)
        overlap = (desc_tokens & _extract_content_tokens(title)) - _GENERIC_NOISE_TOKENS
        verbatim = len(title_norm) >= 8 and f" {title_norm} " in f" {text_norm} "

        why = ""
        if vetoed:
            why = "standing human veto (reopened after a prior auto-close)"
        elif not verbatim:
            why = f"invite title is not quoted by this row ({sorted(overlap)[:4]})"
        if not why:
            return (True,
                    f"meeting_response_sent ({subject[:60]!r}, sent={ts}, "
                    f"overlap={sorted(overlap)[:4]})")
        if near_misses is not None and logged < 5 and (overlap or verbatim):
            logged += 1
            near_misses.append({
                "id": entry.get("id") or "",
                "signal": "meeting_response_sent",
                "event_subject": subject[:80],
                "raw_overlap": sorted(overlap)[:8],
                "why": why,
            })
    return (False, "")


# ── Signal 10: every row carrying the same identifier was closed ──────
# A hand-made row that names manuscript or proposal ids ("4 JRNL reviews
# now due at once: 0996, 1209, 1336, 1412") outlives the per-email rows
# the preprocessor mints for the same ids, because those close when the
# user archives the reminders (ledger_sync, inbox-as-completion) and the
# umbrella has no conversation to join. The user's own archive action is
# the evidence here, so closes the scrubber itself made do not count: a
# fuzzy match must not cascade.
_FULL_ID_RE = re.compile(r"\b([A-Za-z]{2,8})[-_ ](\d{2,4})[-_](\d{3,6})\b")
_LONG_NUM_RE = re.compile(r"(?<![\d/.:$-])(\d{7,12})(?![\d/.:%-])")
_SHORT_NUM_RE = re.compile(r"(?<![\d/.:$-])(\d{3,5})(?![\d/.:%-])")
_SCRUBBER_CLOSE_RE = re.compile(r"auto-closed by closure_scrubber", re.IGNORECASE)


def _identifier_keys(text: str) -> tuple[set[str], set[str]]:
    """('jrnl:1209', 'num:0000904016') keys and the namespaces seen."""
    keys: set[str] = set()
    namespaces: set[str] = set()
    for pfx, _mid, tail in _FULL_ID_RE.findall(text or ""):
        keys.add(f"{pfx.lower()}:{tail}")
        namespaces.add(pfx.lower())
    for n in _LONG_NUM_RE.findall(text or ""):
        keys.add(f"num:{n}")
    return keys, namespaces


def _short_numbers(text: str) -> set[str]:
    """3-5 digit numbers that are not years: the bare tails a human
    writes once the prefix is understood ('JRNL ... 0996, 1209')."""
    out: set[str] = set()
    for n in _SHORT_NUM_RE.findall(text or ""):
        if len(n) == 4 and 1900 <= int(n) <= 2099:
            continue
        out.add(n)
    return out


# A row that describes a standing process ("monitor invites + top up per
# coverage policy") is never finished by any one id closing under it.
_STANDING_RE = re.compile(
    r"\b(monitor(?:ing)?|standing|ongoing|recurring|weekly|monthly|"
    r"pipeline|per\s+(?:\w+\s+)?policy)\b", re.IGNORECASE)


def build_closed_identifier_index(closed_rows: list[dict]) -> dict[str, list]:
    """key -> [(closed_at, closed_by_human, row id)] over closed ledger
    rows. One pass per run. Descriptions only: notes accumulate every
    id that ever passed through a thread."""
    idx: dict[str, list] = {}
    for row in closed_rows or []:
        keys, _ = _identifier_keys(row.get("description") or "")
        if not keys:
            continue
        history = f"{row.get('last_action') or ''} {(row.get('notes') or '')[:400]}"
        human = not _SCRUBBER_CLOSE_RE.search(history)
        for k in keys:
            idx.setdefault(k, []).append(
                (row.get("closed_at") or "", human, row.get("id") or ""))
    return idx


def sibling_keys(text: str, closed_index: dict[str, list]) -> tuple[set[str], set[str], set[str]]:
    """(keys, uncovered numbers, namespaces) for a row's description
    against the closed-row index: prefixed ids and long numbers as they
    are, bare 3-5 digit numbers only under a namespace word the closed
    rows use ('JRNL ... 0996, 1209' -> jrnl:0996, jrnl:1209). `uncovered`
    is the bare numbers no namespaced sibling carries. Shared by signal
    10 and the pre-send audit's evidence."""
    keys, namespaces = _identifier_keys(text)
    if keys:
        return keys, set(), namespaces
    shorts = _short_numbers(text)
    if not shorts:
        return set(), set(), set()
    ns_in_index = {k.split(":", 1)[0] for k in closed_index}
    namespaces = {w.lower() for w in re.findall(r"[A-Za-z]{2,8}", text)} & ns_in_index
    keys = {f"{ns}:{n}" for ns in namespaces for n in shorts
            if f"{ns}:{n}" in closed_index}
    uncovered = shorts - {k.split(":", 1)[1] for k in keys}
    return keys, uncovered, namespaces


def _identifier_siblings_satisfy(entry: dict,
                                 closed_index: dict[str, list],
                                 *,
                                 vetoed: bool = False,
                                 near_misses: list[dict] | None = None,
                                 ) -> tuple[bool, str]:
    """Was every identifier this row names closed elsewhere by the user?

      - the row has no conversation id (id-ful rows have signal 6)
      - its DESCRIPTION names at least one identifier: a prefixed id
        (JRNL-2026-1209, XR-26-0111), a long number (0000904016), or
        bare 3-5 digit numbers next to a namespace word the closed rows
        also use ("JRNL ... 0996, 1209"). Notes do not count: they
        accumulate every id a thread ever mentioned
        (an umbrella row would close on a bounce notice for one
        manuscript)
      - it is not a standing process ("monitor", "per policy")
      - for EVERY key, some closed row carrying it was closed at/after
        this row's creation, and not by a scrubber signal
    """
    if not closed_index or not _blind_entry(entry) or _plugin_owned(entry):
        return (False, "")
    if _etype(entry) == "draft_pending":
        return (False, "")
    text = entry.get("description") or ""
    if _STANDING_RE.search(text):
        return (False, "")
    keys, uncovered, namespaces = sibling_keys(text, closed_index)
    if uncovered:
        # some bare number has no namespaced sibling at all
        if near_misses is not None and keys:
            near_misses.append({
                "id": entry.get("id") or "",
                "signal": "identifier_siblings_closed",
                "event_subject": ", ".join(sorted(uncovered)),
                "raw_overlap": sorted(keys)[:8],
                "why": (f"no closed sibling carries {sorted(uncovered)} "
                        f"under {sorted(namespaces)}"),
            })
        return (False, "")
    if not keys:
        return (False, "")
    entry_date = _entry_start(entry)
    proof: list[str] = []
    for k in sorted(keys):
        hits = [(ts, sid) for ts, human, sid in closed_index.get(k, [])
                if human and ts and (not entry_date or ts >= entry_date)]
        if not hits:
            if near_misses is not None:
                near_misses.append({
                    "id": entry.get("id") or "",
                    "signal": "identifier_siblings_closed",
                    "event_subject": k,
                    "raw_overlap": sorted(keys)[:8],
                    "why": f"no user-closed sibling for {k} after {entry_date}",
                })
            return (False, "")
        ts, sid = max(hits)
        proof.append(f"{k} closed {ts} ({sid})")
    if vetoed:
        return (False, "")
    return (True, "identifier_siblings_closed (" + "; ".join(proof)[:200] + ")")


def _project_member_completion(entry: dict,
                               inbound: list,
                               *,
                               project_index: dict[str, tuple] | None = None,
                               vetoed: bool = False,
                               near_misses: list[dict] | None = None,
                               ) -> tuple[bool, str]:
    """Did someone on this entry's project write to say the work is done?

    `_confirmation_email_satisfies` covers the same cross-thread shape but
    only from a MACHINE: the sender must match
    CONFIRMATION_SENDER_SUBSTRINGS, so a human colleague announcing
    completion never matched. Grant analysts, department admins and co-PIs
    are who actually writes "we are submitted", and for a hand-created row
    that sentence is often the only completion evidence that will ever
    exist.

    Fires when ALL of:
      - the entry has no conversation id and is not plugin-owned
      - no standing veto for (entry, project_member_completion)
      - the entry is the sole open obligation of one project that names
        its people (see `build_project_index`)
      - the sender is one of those people
      - the message SUBJECT asserts completion (`_COMPLETION_ASSERTION`)
      - it arrived after the entry's date_identified

    There is deliberately NO token-overlap test. A message reporting a
    completion routinely shares ZERO content tokens with the entry (the
    entry names the program, the message says "proposal copy submitted"),
    so every threshold low enough to link them also links unrelated
    pairs. Curated project membership is the join instead.
    """
    if not inbound or not _blind_entry(entry) or _plugin_owned(entry):
        return (False, "")
    hit = (project_index or {}).get(entry.get("id") or "")
    if not hit:
        return (False, "")
    project_id, members = hit
    entry_date = _entry_start(entry)
    mine = _user_addresses()

    for msg in inbound:
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        addr = (frm.get("address") or "").strip().lower()
        if not addr or addr in mine or addr not in members:
            continue
        received = msg.get("receivedDateTime") or ""
        if entry_date and received and received < entry_date:
            continue
        subject = msg.get("subject") or ""
        if not _COMPLETION_ASSERTION.search(subject):
            continue
        if vetoed:
            if near_misses is not None:
                near_misses.append({
                    "id": entry.get("id") or "",
                    "signal": "project_member_completion",
                    "event_subject": subject[:80],
                    "raw_overlap": [], "filtered_overlap": [],
                    "distinctive": [],
                    "suppressed_because": ("standing human veto (reopened "
                                           "after a prior auto-close)"),
                    "ts": _utc_now(),
                })
            return (False, "")
        return (True,
                f"project_member_completion (project={project_id}, "
                f"from={addr}, subject={subject[:60]!r})")
    return (False, "")


# ── Entry point ────────────────────────────────────────────────────────

def _split_archive(archive: list) -> tuple[list, list]:
    """Archive folder → (messages the user wrote, messages they received).

    Archiving a conversation in Outlook moves the user's OWN sent copy out
    of Sent Items and into Archive, so a folder-scoped `get_sent` cannot
    see a reply in an archived thread. An archived thread is the MOST
    likely to be finished work, so that folder scope hides precisely the
    evidence these signals need. Archive is therefore a second sent
    source, and a second inbound source for `project_member_completion`.
    """
    mine = _user_addresses()
    authored, received = [], []
    for msg in archive or []:
        addr = (((msg.get("from") or {}).get("emailAddress") or {})
                .get("address") or "").strip().lower()
        (authored if addr and addr in mine else received).append(msg)
    return authored, received


def scrub_closures(*, open_entries: list[dict] | None = None,
                   outlook_sent: list | None = None,
                   outlook_inbox: list | None = None,
                   outlook_archive: list | None = None,
                   calendar_events: list | None = None,
                   gmail_accounts: list[str] | None = None,
                   gmail_sent: dict[str, list] | None = None,
                   gmail_threads: dict[str, dict[str, list]] | None = None,
                   dry_run: bool = False,
                   db_path: Path | None = None,
                   veto_store_path: Path | None = None) -> dict:
    """Run the closure pass. Returns a summary dict.

    All inputs are optional; missing inputs cause the corresponding
    signal to be skipped (the function never raises on missing data,
    only on programmer errors).

    `outlook_archive` is optional and additive: it feeds the two
    conversation-id-free signals only (see `_split_archive`). The
    signals that read `outlook_inbox` keep reading the inbox alone, so
    passing it cannot change what they do.
    """
    if open_entries is None:
        open_entries = ledger_mod.get_open_entries(db_path=db_path)

    archived_sent, archived_inbound = _split_archive(outlook_archive or [])
    sent_all = (outlook_sent or []) + archived_sent
    inbound_all = (outlook_inbox or []) + archived_inbound

    # Standing vetoes, and the reopens that create new ones. An entry we
    # closed that is open again was put back by a human, unless a new
    # message in its thread renewed it.
    vetoes = VetoStore.load(veto_store_path)
    new_vetoes: list[dict] = []
    for entry in open_entries:
        eid = entry.get("id") or ""
        if not eid:
            continue
        signal = vetoes.note_reopen(eid, renewed_at=str(entry.get("renewed_at") or ""))
        if signal:
            new_vetoes.append({"id": eid, "signal": signal,
                               "source": "reopened after auto-close"})
        elif _history_has_reopen_veto(entry):
            if vetoes.add_veto(eid, "calendar_event_match", "ledger history"):
                new_vetoes.append({"id": eid, "signal": "calendar_event_match",
                                   "source": "ledger history"})

    token_df = build_token_df(open_entries)
    corpus_size = len(open_entries)
    prepared_events = _prepare_events(calendar_events or [])
    project_index = build_project_index(open_entries, db_path=db_path)
    near_misses: list[dict] = []

    sent_conv_times = build_sent_conv_index(outlook_sent or [])
    sent_by_conv = _index_sent_by_conv(outlook_sent or [])
    inbox_by_conv = _index_inbox_by_conv(outlook_inbox or [])
    try:
        closed_conv_times = ledger_mod.get_closed_conversation_times(
            db_path=db_path)
    except Exception:
        closed_conv_times = {}
    try:
        closed_identifier_index = build_closed_identifier_index(
            ledger_mod.get_closed_entries_brief(db_path=db_path))
    except Exception:
        closed_identifier_index = {}

    gmail_sent_indices: dict[str, dict[str, str]] = {}
    if gmail_sent is not None:
        # Injected raw sent messages (harness / backtests): index them
        # with the same pure code the live path uses, no API.
        gmail_sent_indices = {acct: _index_gmail_sent(msgs)
                              for acct, msgs in gmail_sent.items()}
    elif gmail_accounts:
        # Only build the Gmail index if any open entry has a gmail_thread_id;
        # otherwise the API calls are wasted.
        has_gmail_entries = any(e.get("gmail_thread_id") for e in open_entries)
        if has_gmail_entries:
            for acct in gmail_accounts:
                gmail_sent_indices[acct] = _build_gmail_sent_index(acct)

    # The gmail signal verifies candidate threads before closing (a reply
    # needs an inbound message to answer). Injected thread data when the
    # harness provides it; a cached get_thread per candidate live — only
    # threads already past the sent-index and timing checks are fetched,
    # which is at most a handful per run.
    if gmail_threads is not None:
        def gmail_thread_fetch(account: str, tid: str):
            return (gmail_threads.get(account) or {}).get(tid)
    elif gmail_sent_indices:
        _thread_cache: dict = {}

        def gmail_thread_fetch(account: str, tid: str):
            key = (account, tid)
            if key not in _thread_cache:
                msgs = None
                try:
                    import gmail_client
                    t = gmail_client.get_thread(account, tid)
                    if isinstance(t, dict) and "error" not in t:
                        msgs = t.get("messages")
                except Exception:
                    msgs = None
                _thread_cache[key] = msgs
            return _thread_cache[key]
    else:
        def gmail_thread_fetch(account: str, tid: str):
            return None

    closures: list[dict] = []
    checked = 0
    for entry in open_entries:
        checked += 1
        entry_id = entry.get("id") or ""
        if not entry_id:
            continue

        def _candidates(entry=entry, entry_id=entry_id):
            """The signals that fire for this row, in order of strength."""
            # Signal 1: Outlook reply (same conversation). Veto-gated: a
            # human who reopened a user_replied close has said the reply did
            # not finish the work, and the same sent mail must not re-close
            # it every run after that.
            if not vetoes.is_vetoed(entry_id, "user_replied_outlook"):
                yield _user_replied_outlook(
                    entry, sent_conv_times, sent_by_conv=sent_by_conv,
                    near_misses=near_misses)
            if not vetoes.is_vetoed(entry_id, "user_replied_gmail"):
                # Signal 2: Gmail reply (same thread)
                yield _user_replied_gmail(entry, gmail_sent_indices,
                                          thread_fetch=gmail_thread_fetch)
            # Signal 3: latest inbound in same conv is an acknowledgment
            yield _latest_inbound_is_ack(entry, inbox_by_conv)
            # Signal 4: cross-thread confirmation from a known
            # submission/registration sender (Easychair, NSF, Eventbrite,
            # Zoom, etc.) whose subject overlaps the entry description.
            yield _confirmation_email_satisfies(entry, outlook_inbox or [])
            # Signal 5: a forward-looking calendar event satisfies a
            # scheduling-shaped entry (subject/attendees overlap the
            # entry description, and the event starts after the entry
            # was created).
            yield _calendar_event_satisfies(
                entry, calendar_events or [],
                prepared=prepared_events,
                token_df=token_df,
                corpus_size=corpus_size,
                vetoed=vetoes.is_vetoed(entry_id, "calendar_event_match"),
                near_misses=near_misses,
            )
            if not vetoes.is_vetoed(entry_id, "closed_sibling"):
                # Signal 6: a sibling entry of the same conversation was
                # closed after this entry existed — the conversation is
                # handled; sweep the survivor. A human reopen after one of
                # these closes is a standing veto for the entry.
                yield _closed_sibling_satisfies(entry, closed_conv_times)
            # Signal 7: the entry has no conversation id, but the user
            # wrote to its correspondent about its topic.
            yield _user_replied_to_correspondent(
                entry, sent_all,
                token_df=token_df,
                corpus_size=corpus_size,
                vetoed=vetoes.is_vetoed(entry_id, "sender_reply_match"),
                near_misses=near_misses,
            )
            # Signal 8: someone on this entry's project wrote to say the
            # work is finished, in a thread the entry never knew about.
            yield _project_member_completion(
                entry, inbound_all,
                project_index=project_index,
                vetoed=vetoes.is_vetoed(entry_id, "project_member_completion"),
                near_misses=near_misses,
            )
            # Signal 9: the user clicked Accept/Decline/Tentative on the
            # very invite an RSVP-shaped, id-less row asks about.
            yield _meeting_response_satisfies(
                entry, sent_all,
                vetoed=vetoes.is_vetoed(entry_id, "meeting_response_sent"),
                near_misses=near_misses,
            )
            # Signal 10: every manuscript/proposal id the row names was
            # closed elsewhere by the user (archived reminders).
            yield _identifier_siblings_satisfy(
                entry, closed_identifier_index,
                vetoed=vetoes.is_vetoed(entry_id, "identifier_siblings_closed"),
                near_misses=near_misses,
            )

        # The first signal that fires and that the user has not refused for
        # this row (closure.vetoed: "Still open" or a reopen, at or after
        # its evidence) goes to the policy. A refused one does not hide a
        # weaker signal with newer evidence behind it.
        signal = reason = ""
        for fired, why in _candidates():
            if not fired:
                continue
            sig = why.split(" ", 1)[0]
            m_ts = _EVIDENCE_TS_RE.search(why or "")
            if sig in _NOT_EVIDENCE or closure.vetoed(entry, sig, m_ts.group(1) if m_ts else ""):
                continue
            signal, reason = sig, why
            break
        if not signal:
            continue

        # The policy decides what the signal may do to this row: close on
        # decisive evidence, otherwise propose (closure.py).
        action = "closed" if closure.tier(signal, entry) == closure.DECISIVE else "proposed"
        if not dry_run:
            action = _settle_entry(entry_id, signal, reason, db_path=db_path)
            if action == "closed" and signal in _VETOABLE_SIGNALS:
                # Remember it, so a reopen becomes a veto next run.
                vetoes.record_close(entry_id, signal)
        if action not in ("closed", "proposed"):
            continue
        closures.append({
            "id": entry_id,
            "reason": reason,
            "action": action,
            "description": entry.get("description") or "",
            "conversation_id": entry.get("conversation_id") or "",
            "gmail_thread_id": entry.get("gmail_thread_id") or "",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dry_run": dry_run,
        })

    if not dry_run:
        if closures:
            _append_jsonl(LOG_PATH, closures)
        _append_jsonl(_suppressed_log_path(), near_misses)
        vetoes.save()

    return {
        "checked": checked,
        "closed": sum(1 for c in closures if c["action"] == "closed"),
        "proposed": sum(1 for c in closures if c["action"] == "proposed"),
        "closures": closures,
        "suppressed": near_misses,
        "new_vetoes": new_vetoes,
        "dry_run": dry_run,
    }


# ── Graph reads of a live check ────────────────────────────────────────
# A live check reads four things from Graph, each for one kind of evidence:
#
#   sent      the user's replies (signals 1, 7 and 9)
#   inbox     acknowledgments, confirmations, completion notes (3, 4, 8)
#   archive   sent copies and completion notes of archived threads
#             (`_split_archive`: 7, 8 and 9)
#   calendar  the meeting a scheduling row asked for (5)
#
# Each read selects only the fields its signals use, pages PAGE_SIZE items
# at a time and stops at its cap; mail comes newest first, so a cap drops
# the oldest. The reads share one deadline and one retry ladder: once a
# call has waited to retry, every later call makes one attempt, and a
# transient failure of such a call ends the reads. The pass then runs on
# what was read. Every signal is positive evidence, so less mail can only
# close less, and the next hourly run reads again.
READ_DEADLINE_S = 240
MAIL_DAYS_BACK = 14
CALENDAR_DAYS_BACK = 7
CALENDAR_DAYS_AHEAD = 14
PAGE_SIZE = 100
MAIL_CAP = 500
CALENDAR_CAP = 300
SENT_FIELDS = ("subject,from,toRecipients,ccRecipients,sentDateTime,"
               "receivedDateTime,conversationId")
INBOX_FIELDS = "subject,from,receivedDateTime,bodyPreview,conversationId"
ARCHIVE_FIELDS = ("subject,from,toRecipients,ccRecipients,sentDateTime,"
                  "receivedDateTime")
CALENDAR_FIELDS = "subject,start,organizer,attendees"
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})


def calendar_window(open_entries: list[dict],
                    now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """(start, end) of the calendar read, or None when no open row can use
    one. An event that starts before a row does never satisfies it, so the
    span opens at the earliest start among the rows the calendar signal
    walks (`_was_scheduling_entry_legacy` holds them all, the audit-only
    ones too), CALENDAR_DAYS_BACK ago at the most, and ends
    CALENDAR_DAYS_AHEAD from now. A row with no readable start opens it
    at the floor."""
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=CALENDAR_DAYS_BACK)
    earliest, rows = now, 0
    for entry in open_entries or []:
        if not _was_scheduling_entry_legacy(entry):
            continue
        rows += 1
        start = closure._parse(_entry_start(entry))
        if start is None:
            earliest = floor
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        earliest = min(earliest, start)
    if not rows:
        return None
    return (max(earliest, floor), now + timedelta(days=CALENDAR_DAYS_AHEAD))


def _error_kind(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}"
    return type(exc).__name__


def _circuit_open(exc: BaseException) -> bool:
    try:
        import retry_policy
        return retry_policy.find_circuit_open(exc) is not None
    except Exception:
        return False


def _transient(exc: BaseException) -> bool:
    """A failure the service may not repeat: a status Graph retries, a
    timeout or a dropped connection."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status in _TRANSIENT_STATUSES
    return any(c.__name__ in ("ConnectionError", "Timeout", "TimeoutError")
               for c in type(exc).__mro__)


class GraphReads:
    """The Graph calls of one live check, under one deadline and one retry
    ladder (see above). `graph` is graph_client, or a stand-in with
    graph_get and retry_waits; `clock` is time.monotonic, or a test's."""

    def __init__(self, graph, *, deadline_s: float = READ_DEADLINE_S,
                 clock=time.monotonic):
        self.graph = graph
        self.clock = clock
        self.deadline_s = deadline_s
        self.deadline = clock() + deadline_s
        self.ladder_spent = False
        self.calls = 0
        self.stopped = ""

    def _waits(self) -> int:
        try:
            return int(self.graph.retry_waits())
        except Exception:
            return 0

    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def read(self, name: str, endpoint: str, params: dict, cap: int) -> list:
        """Up to `cap` items of one read, page by page. A failure keeps the
        pages read before it; [] once the reads have ended."""
        items: list = []
        link, query = endpoint, params
        while link and len(items) < cap and not self.stopped:
            if self.out_of_time():
                self.stopped = (f"the {self.deadline_s:g}-second limit passed "
                                f"{'during' if items else 'before'} the {name} read")
                break
            single = self.ladder_spent
            kw = {"deadline": self.deadline}
            if single:
                kw["_retries"] = 1
            before = self._waits()
            self.calls += 1
            try:
                data = self.graph.graph_get(link, query, **kw)
            except Exception as e:
                print(f"  WARNING: Outlook {name} read failed: {e}", file=sys.stderr)
                if _circuit_open(e):
                    self.stopped = f"the {name} read met an open circuit breaker"
                elif _transient(e) and single:
                    self.stopped = (f"the {name} read failed ({_error_kind(e)}) "
                                    "after the run had used its retries")
                elif _transient(e):
                    self.ladder_spent = True
                break
            finally:
                if self._waits() > before:
                    self.ladder_spent = True
            items.extend(data.get("value") or [])
            link, query = data.get("@odata.nextLink"), None
        return items[:cap]


def read_graph(graph, open_entries: list[dict], *,
               now: datetime | None = None, clock=time.monotonic,
               deadline_s: float = READ_DEADLINE_S) -> dict:
    """The Graph evidence of one live check: {"sent", "inbox", "archive",
    "calendar"} lists, "calls" (graph_get calls, one a page, each with any
    retries it made) and "stopped" (why the reads ended early, or "").
    Reads that ended early log one line."""
    now = now or datetime.now(timezone.utc)
    reads = GraphReads(graph, deadline_s=deadline_s, clock=clock)
    since = (now - timedelta(days=MAIL_DAYS_BACK)).isoformat()

    def mail(name: str, folder: str, fields: str) -> tuple:
        return (name, f"/me/mailFolders/{folder}/messages",
                {"$select": fields, "$filter": f"receivedDateTime ge {since}",
                 "$orderby": "receivedDateTime desc", "$top": PAGE_SIZE},
                MAIL_CAP)

    plan = [mail("sent", "sentitems", SENT_FIELDS),
            mail("inbox", "inbox", INBOX_FIELDS),
            # Sent copies of archived threads live here, not in Sent
            # Items. See `_split_archive`.
            mail("archive", "archive", ARCHIVE_FIELDS)]
    window = calendar_window(open_entries, now)
    if window is not None:
        plan.append(("calendar", "/me/calendarView",
                     {"startDateTime": window[0].isoformat(),
                      "endDateTime": window[1].isoformat(),
                      "$select": CALENDAR_FIELDS,
                      "$orderby": "start/dateTime", "$top": PAGE_SIZE},
                     CALENDAR_CAP))

    out: dict = {"sent": [], "inbox": [], "archive": [], "calendar": []}
    unread: list[str] = []
    for name, endpoint, params, cap in plan:
        if reads.stopped:
            unread.append(name)
            continue
        out[name] = reads.read(name, endpoint, params, cap)
        if reads.stopped and not out[name]:
            unread.append(name)
    if reads.stopped:
        print(f"  closure_scrubber: Graph reads ended early, {reads.stopped}; "
              f"not read: {', '.join(unread) or 'nothing'}; the next run reads "
              "again", file=sys.stderr)
    out["calls"] = reads.calls
    out["stopped"] = reads.stopped
    return out


# ── CLI ────────────────────────────────────────────────────────────────

def _load_user_gmail_accounts() -> list[str]:
    """Read gmail_accounts from ~/.ea/config/user-profile.json. Returns []
    on any error.
    """
    cfg = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "config" / "user-profile.json"
    try:
        data = json.loads(cfg.read_text())
        accts = data.get("gmail_accounts") or []
        return [a for a in accts if isinstance(a, str)]
    except Exception:
        return []


def cmd_check(args) -> int:
    """CLI entry point. Loads live state via Graph API + Gmail client and
    runs the scrubber.
    """
    # Lazy import: graph_client requires MSAL tokens to be present.
    try:
        import graph_client as graph
    except ImportError:
        graph = None  # type: ignore

    open_entries = ledger_mod.get_open_entries()
    reads: dict = read_graph(graph, open_entries) if graph is not None else {}

    gmail_accounts = _load_user_gmail_accounts() if not args.no_gmail else []

    result = scrub_closures(
        open_entries=open_entries,
        outlook_sent=reads.get("sent") or [],
        outlook_inbox=reads.get("inbox") or [],
        outlook_archive=reads.get("archive") or [],
        calendar_events=reads.get("calendar") or [],
        gmail_accounts=gmail_accounts,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(f"[{mode}] Checked {result['checked']} open entries, "
              f"closed {result['closed']}.")
        for c in result["closures"]:
            print(f"  - {c['id']}: {c['reason']}")
            print(f"      {c['description'][:120]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-briefing closure scrubber")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="Run a closure pass (default: live close)")
    p_check.add_argument("--dry-run", action="store_true",
                         help="Log what would close but don't update the ledger")
    p_check.add_argument("--no-gmail", action="store_true",
                         help="Skip the Gmail reply signal (Outlook + ack only)")
    p_check.add_argument("--json", action="store_true",
                         help="Emit JSON instead of human-readable")
    args = parser.parse_args()
    if args.command == "check":
        return cmd_check(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    # A tick: its Graph calls run in the background lane unless its caller
    # set one (retry_policy.graph_lane).
    import retry_policy
    retry_policy.set_graph_lane(retry_policy.BACKGROUND, only_if_unset=True)
    sys.exit(main())
