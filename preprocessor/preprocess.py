"""
EA Preprocessor – analyzes mail, calendars, and tasks to produce
a slim briefing JSON for Claude. Runs entirely without Claude tokens.

Usage:
    python preprocess.py --mode full-daily
    python preprocess.py --mode scheduling-check
    python preprocess.py --mode priority-check
    python preprocess.py --mode full-weekly
"""

import argparse
import json
import sqlite3
import os
import re
try:
    import atomic_io as _atomic
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import resident_io as _rio
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure the preprocessor package is importable
sys.path.insert(0, str(Path(__file__).parent))
import graph_client as graph
import institution as _institution
import google_calendar_client as gcal
# The one availability model (token preferences + merged busy time). The
# briefing's `availability_merged` comes from here so it cannot disagree
# with what the solver enforces.
import scheduling_engine as sched_engine
import pdf_utils

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_DIR = EA_DIR / "config"
STATE_DIR = EA_DIR / "state"
# The state directory is resident space. An owner process (the cron run) never
# creates it: the install provides it, and a missing one fails the first write.
if not _rio.is_owner():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------------
# User-configurable settings loaded from config files with fallbacks
# -------------------------------------------------------------------

def _load_config_json(filename: str, fallback: dict) -> dict:
    """Load a JSON config file from EA_DIR/config, falling back to default."""
    path = CONFIG_DIR / filename
    if path.exists():
        try:
            data = json.loads(path.read_text())
            # Strip _doc and _example keys (template scaffolding)
            return {k: v for k, v in data.items()
                    if not k.startswith("_")}
        except (json.JSONDecodeError, OSError):
            pass
    return fallback


def _load_preprocessor_config() -> dict:
    """Load preprocessor-config.json with sensible defaults."""
    defaults = {
        "stale_thread_days": 3,
        "stale_thread_days_weekly_audit": 2,
        "writing_style_window_days": 14,
        "inbox_philosophy": {"mode": "inbox_zero", "read_entire_inbox": True},
    }
    return _load_config_json("preprocessor-config.json", defaults)


PREPROCESSOR_CONFIG = _load_preprocessor_config()


def _load_user_email() -> str:
    """Load user's email from profile for To/CC detection."""
    path = CONFIG_DIR / "user-profile.json"
    if path.exists():
        try:
            return json.loads(path.read_text()).get("email", "").lower()
        except (json.JSONDecodeError, OSError):
            pass
    return ""


USER_EMAIL = _load_user_email()

# -------------------------------------------------------------------
# Delta tracking – only surface NEW items to Claude
# -------------------------------------------------------------------
SEEN_MESSAGES_FILE = STATE_DIR / "seen-messages.json"
SEEN_CONVERSATIONS_FILE = STATE_DIR / "seen-conversations.json"
SURFACED_RESPONSES_FILE = STATE_DIR / "surfaced-responses.json"


def _read_state_json(path: Path):
    """A state file in the resident's space, read with the resident's access
    (so a link at its name reaches nothing the resident could not read) and
    parsed; None when it is absent. Raises OSError when it exists but could
    not be read, so a caller never saves an empty result over real data."""
    text = _rio.read_text(path)
    if text is None:
        if os.path.lexists(path):
            raise OSError(f"could not read {path}")
        return None
    return json.loads(text)


def _str_map(data, path: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def load_seen_messages() -> set:
    data = _read_state_json(SEEN_MESSAGES_FILE)
    if data is None:
        return set()
    if not isinstance(data, list):
        raise ValueError(f"{SEEN_MESSAGES_FILE}: expected a JSON list")
    return {x for x in data if isinstance(x, str)}


def save_seen_messages(seen: set):
    _atomic.write_text(SEEN_MESSAGES_FILE, json.dumps(list(seen)), mode=0o664)


def load_seen_conversations() -> dict:
    """Map of conversation_id -> last seen datetime ISO string."""
    data = _read_state_json(SEEN_CONVERSATIONS_FILE)
    return {} if data is None else _str_map(data, SEEN_CONVERSATIONS_FILE)


def save_seen_conversations(seen: dict):
    _atomic.write_text(SEEN_CONVERSATIONS_FILE, json.dumps(seen), mode=0o664)


def load_surfaced_responses() -> dict:
    """Map of conversation_id → last surfaced message timestamp (ISO string).

    Migrates from the old format (list of IDs) automatically.
    """
    data = _read_state_json(SURFACED_RESPONSES_FILE)
    if data is None:
        return {}
    if isinstance(data, list):
        # Migrate: old format was a list of conversation IDs.
        # Use empty string so any new message will be newer.
        return {cid: "" for cid in data if isinstance(cid, str)}
    return _str_map(data, SURFACED_RESPONSES_FILE)


def save_surfaced_responses(surfaced: dict):
    _atomic.write_text(SURFACED_RESPONSES_FILE, json.dumps(surfaced, indent=2), mode=0o664)


def filter_to_delta(messages: list, seen_ids: set) -> tuple[list, set]:
    """
    Split messages into new (unseen) and return updated seen set.
    Returns (new_messages, updated_seen_set).
    """
    new_msgs = [m for m in messages if m.get("id") not in seen_ids]
    updated_seen = seen_ids | {m["id"] for m in messages if m.get("id")}
    return new_msgs, updated_seen


def filter_conversations_delta(
    dropped_threads: list, seen_convs: dict
) -> tuple[list, dict]:
    """
    Only return dropped threads with new activity since last check.
    """
    new_dropped = []
    updated_convs = dict(seen_convs)

    for thread in dropped_threads:
        conv_id = thread.get("conversation_id", "")
        received = thread.get("received", "")
        last_seen = seen_convs.get(conv_id, "")

        if received > last_seen:
            new_dropped.append(thread)
            updated_convs[conv_id] = received

    return new_dropped, updated_convs


def suppress_closed_conversations(items: list, label: str) -> list:
    """Drop derived items whose conversation the user already CLOSED in the
    ledger, unless the message arrived after the closure (genuinely new
    inbound). This is the anti-re-derivation gate: without it, unarchived
    inbox artifacts of a closed conversation become "new" obligations the
    next morning."""
    try:
        closed = _ledger_mod.get_closed_conversation_times()
    except Exception as e:
        print(f"  suppress_closed_conversations skipped ({e})", file=sys.stderr)
        return items
    if not closed:
        return items
    kept, dropped_n = [], 0
    for it in items:
        closed_at = closed.get(it.get("conversation_id", ""))
        if closed_at and (it.get("received", "") or "") <= closed_at:
            dropped_n += 1
            continue
        kept.append(it)
    if dropped_n:
        print(f"  {label}: suppressed {dropped_n} item(s) from conversations "
              f"already closed in the ledger", file=sys.stderr)
    return kept


# -------------------------------------------------------------------
# Sent-reply cross-referencing
# -------------------------------------------------------------------
# The ledger filter above only knows about items the user (or bot)
# explicitly closed. A reply the user dashed off from his phone closes
# nothing. These helpers match surfaced items against actual outbound
# mail: same conversation, or, for a reply that starts a fresh thread,
# same counterparty plus the same prefix-stripped subject.


def build_sent_reply_index(sent: list) -> dict:
    """Index outbound mail three ways: conversation_id -> latest sent time,
    (recipient, normalized subject) -> latest sent time, and recipient ->
    the latest mail to them (time and subject), whatever the thread."""
    conv_times: dict = {}
    pair_times: dict = {}
    recip_latest: dict = {}
    for m in sent:
        ts = m.get("sentDateTime") or m.get("receivedDateTime", "")
        if not ts:
            continue
        cid = m.get("conversationId")
        if cid and ts > conv_times.get(cid, ""):
            conv_times[cid] = ts
        subj = _norm_subject(m.get("subject", ""))
        for recip in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
            addr = (recip.get("emailAddress") or {}).get("address", "").lower()
            if not addr:
                continue
            if ts > (recip_latest.get(addr) or {}).get("at", ""):
                recip_latest[addr] = {"at": ts, "subject": (m.get("subject") or "")[:120]}
            if subj:
                key = (addr, subj)
                if ts > pair_times.get(key, ""):
                    pair_times[key] = ts
    return {"conv": conv_times, "pair": pair_times, "recip": recip_latest}


def split_replied_items(items: list, reply_index: dict, source: str) -> tuple:
    """Partition items into (still_open, handled_by_reply).

    An item is handled when the user's latest outbound to that thread or
    counterparty is NEWER than the inbound that surfaced the item. Newer
    inbound than the reply keeps the item open (they answered back).

    Mail to the same sender in ANOTHER thread after their message is not
    a reply, but the briefing must not present the ask as untouched news
    either: the item stays open and carries `wrote_to_since` (when, and
    the subject), for the writer to say "you wrote to X since"."""
    still_open, handled = [], []
    for it in items:
        received = it.get("received", "") or ""
        replied_at, match = "", ""
        counterparty = (it.get("from") or "").lower()
        t = reply_index["conv"].get(it.get("conversation_id", ""), "")
        if t and t > received:
            replied_at, match = t, "same-conversation"
        else:
            subj = _norm_subject(it.get("subject", ""))
            if counterparty and subj:
                t = reply_index["pair"].get((counterparty, subj), "")
                if t and t > received:
                    replied_at, match = t, "recipient+subject"
        if not replied_at and counterparty:
            latest = (reply_index.get("recip") or {}).get(counterparty) or {}
            if latest.get("at", "") > received:
                it["wrote_to_since"] = {"at": latest["at"], "subject": latest.get("subject", "")}
        if replied_at:
            handled.append({
                "subject": it.get("subject", ""),
                "from": it.get("from", ""),
                "from_name": it.get("from_name", ""),
                "received": received,
                "replied_at": replied_at,
                "match": match,
                "source": source,
            })
        else:
            still_open.append(it)
    return still_open, handled


# -------------------------------------------------------------------
# Slack mention reconciliation
# -------------------------------------------------------------------
def annotate_slack_mentions(items: list, slack_messages: list) -> int:
    """Tag briefing items already being discussed in the EA Slack channel.

    Matching is deliberately conservative: the counterparty's name (a
    token of 4+ chars) or two significant subject words must appear in a
    recent message. Tagged items carry the matching snippet so the
    briefing can say "already in play in Slack" instead of re-raising."""
    tagged = 0
    prepared = []
    for msg in slack_messages or []:
        text = (msg.get("text") or "").lower()
        if text:
            prepared.append((text, msg))
    if not prepared:
        return 0
    for it in items:
        name_tokens = [t for t in re.split(r"\W+", (it.get("from_name") or "").lower())
                       if len(t) >= 4]
        subj_tokens = [t for t in re.split(r"\W+", _norm_subject(it.get("subject", "")))
                       if len(t) >= 5]
        for text, msg in prepared:
            name_hit = any(t in text for t in name_tokens)
            subj_hits = sum(1 for t in subj_tokens if t in text)
            if name_hit or subj_hits >= 2:
                it["mentioned_in_slack"] = {
                    "sender": msg.get("sender", ""),
                    "ts": msg.get("ts", "") or msg.get("timestamp", ""),
                    "snippet": (msg.get("text") or "")[:160],
                }
                tagged += 1
                break
    return tagged


# -------------------------------------------------------------------
# Deterministic priority queue
# -------------------------------------------------------------------
# Ranking is computed here, in code, so "what needs doing today" is a
# property of the data, not of prompt wording.
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _parse_deadline_date(text: str) -> "datetime | None":
    """Best-effort parse of extracted deadline strings ('August 15',
    'Aug 15, 2026', '8/15', '8/15/26'). Returns tz-aware UTC or None."""
    if not text:
        return None
    now = datetime.now(timezone.utc)
    t = text.strip().lower().rstrip(".,")
    m = re.match(r"([a-z]+)\s+(\d{1,2})(?:,?\s*(\d{4}))?$", t)
    if m and (m.group(1)[:3] in {k[:3] for k in _MONTHS} or m.group(1) in _MONTHS):
        month = next((v for k, v in _MONTHS.items()
                      if k == m.group(1) or k[:3] == m.group(1)[:3]), None)
        if month:
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                d = datetime(year, month, int(m.group(2)), tzinfo=timezone.utc)
                # Bare "January 5" seen in November means next year.
                if not m.group(3) and (now - d).days > 90:
                    d = d.replace(year=year + 1)
                return d
            except ValueError:
                return None
    m = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", t)
    if m:
        year = now.year
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000
        try:
            d = datetime(year, int(m.group(1)), int(m.group(2)),
                         tzinfo=timezone.utc)
            if not m.group(3) and (now - d).days > 90:
                d = d.replace(year=year + 1)
            return d
        except ValueError:
            return None
    return None


def _parse_ledger_deadline(value) -> "datetime | None":
    """Parse a ledger row's `deadline` field to a tz-aware UTC date.

    Rows written by the EA use 'YYYY-MM-DD', but the column is free
    text: it can be absent, empty, an ISO timestamp, or prose ('TBD',
    'end of the month'). The briefing is an unattended cron job, so a
    bad value returns None and never raises.
    """
    if not isinstance(value, str):
        return None
    t = value.strip()
    if not t:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]|$)", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    # Hand-written entries sometimes carry 'Aug 31' or '8/31'; reuse the
    # same free-text parser the email deadline path uses.
    return _parse_deadline_date(t)


def _title(text, limit: int) -> str:
    """A ledger description in a fixed width: ledger.brief_title (a
    word-boundary cut with an ellipsis), imported here to keep the
    module's import order."""
    import ledger
    return ledger.brief_title(text or "", limit)


def _user_zone():
    """The user's zone (user_tz.py: the profile, the scheduling
    preferences, then the system's zone). The briefing's day is the
    user's, never the machine's."""
    try:
        import user_tz
        return user_tz.zone()
    except Exception:
        return None   # astimezone(None): the system's zone


def _user_today(days: int = 0) -> str:
    """The user's date, `days` from now, 'YYYY-MM-DD'."""
    return (datetime.now(_user_zone()) + timedelta(days=days)).strftime("%Y-%m-%d")


def _event_local_day(e: dict) -> str:
    """The event's start date on the user's clock, 'YYYY-MM-DD'. Graph's
    calendar view prints UTC wall-clock with no offset (timeZone "UTC");
    an all-day event's date is its own date."""
    st = e.get("start") or {}
    raw = str(st.get("dateTime") or "")
    if e.get("isAllDay") or len(raw) < 19:
        return raw[:10]
    try:
        from zoneinfo import ZoneInfo
        zone = str(st.get("timeZone") or "UTC")
        src = timezone.utc if zone.upper() == "UTC" else ZoneInfo(zone)
    except Exception:
        src = timezone.utc
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", raw)
    try:
        if m and m.group(2):
            off = "+00:00" if m.group(2) == "Z" else m.group(2)
            dt = datetime.fromisoformat(m.group(1) + off)
        else:
            dt = datetime.fromisoformat(raw[:19]).replace(tzinfo=src)
        return dt.astimezone(_user_zone()).strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


def _close_proposals(obligations: list) -> list[dict]:
    """The open rows that carry a close proposal, for the briefing."""
    out = []
    for ob in obligations or []:
        prop = ob.get("close_proposal")
        if isinstance(prop, dict):
            out.append({"id": ob.get("id"), "description": _title(ob.get("description"), 160),
                        "why": (prop.get("why") or "")[:200], "since": prop.get("first_at") or ""})
    return out[:20]


def _onboarding_question() -> dict | None:
    """The first-week question of the day (preprocessor/onboarding.py),
    recorded as asked, or None: the bank is done, the questions are off,
    today's is still open, or the lookup failed."""
    try:
        import onboarding
        rows = onboarding.next_questions(count=1)
    except Exception as e:
        print(f"  Onboarding question skipped: {e}", file=sys.stderr)
        return None
    return rows[0] if rows else None


def _mark_parked(obligations: list) -> None:
    """Stamp `parked_until` on every row a future follow_up_after or
    snooze_until parks, so the briefing writer sees in the row itself
    that it must not be raised yet."""
    for ob in obligations or []:
        until = _ledger_mod.parked_until(ob)
        if until:
            ob["parked_until"] = until


_CLOCK_TIME_RE = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))(?:\s*(?:MT|MDT|MST|ET|PT|CT))?", re.IGNORECASE)


def _unparks_today(ob: dict, today) -> bool:
    """True when follow_up_after or snooze_until names today's date."""
    for field in ("follow_up_after", "snooze_until"):
        raw = str(ob.get(field) or "").strip()[:10]
        if len(raw) == 10 and raw == today.isoformat():
            return True
    return False


def _clock_time_in(text: str) -> str:
    """The first clock time written in `text` ('10:00am', '7:30 p.m.'), or ''."""
    m = _CLOCK_TIME_RE.search(text or "")
    return re.sub(r"\s+", "", m.group(1)).lower() if m else ""


def build_today_top(needs_resp: list, dropped: list, deadlines: list,
                    rsvps: list, obligations: list, vip_senders: list,
                    cap: int = 5, floor: int = 30) -> tuple:
    """Score every actionable item and return (today_top, everything_else).

    today_top: at most `cap` items scoring >= `floor`, each with a
    human-readable reason string. everything_else: one-liners for the
    rest, so the briefing can digest instead of enumerate."""
    now = datetime.now(timezone.utc)
    vips = {v.lower() for v in (vip_senders or [])}
    scored = []

    def add(source, title, ref, score, reasons, counterparty=""):
        scored.append({
            "source": source,
            "title": title,
            "ref": ref,
            "counterparty": counterparty,
            "score": score,
            "reason": "; ".join(reasons),
        })

    for it in needs_resp or []:
        score, reasons = 0, []
        if it.get("from", "").lower() in vips:
            score += 25; reasons.append("VIP sender")
        if it.get("user_is_direct_recipient"):
            score += 15
        if it.get("user_is_cc_only"):
            score -= 20; reasons.append("CC only")
        days = it.get("days_in_inbox", 0) or 0
        score += min(days * 2, 20)
        if days >= 5:
            reasons.append(f"waiting {days}d")
        score += min((it.get("response_signals", 0) or 0) * 3, 15)
        if it.get("importance") == "high":
            score += 10; reasons.append("marked high importance")
        if it.get("mentioned_in_slack"):
            score += 10; reasons.append("already discussed in Slack")
        if not reasons:
            reasons.append("needs a reply")
        add("needs_response", it.get("subject", ""), it.get("message_id", ""),
            score, reasons, it.get("from_name") or it.get("from", ""))

    for it in dropped or []:
        score, reasons = 10, ["dropped thread"]
        if it.get("from", "").lower() in vips:
            score += 25; reasons.append("VIP sender")
        days = it.get("days_stale", 0) or 0
        score += min(days * 2, 20)
        if days:
            reasons.append(f"stale {days}d")
        if it.get("user_is_cc_only"):
            score -= 20
        add("dropped_thread", it.get("subject", ""), it.get("message_id", ""),
            score, reasons, it.get("from_name") or it.get("from", ""))

    for it in deadlines or []:
        score, reasons = 15, []
        due = _parse_deadline_date(it.get("extracted_date", ""))
        if it.get("owed_to_user"):
            # A date the user set for other people. Their replies are not
            # the user's overdue work; keep it in the digest, out of the
            # queue.
            when = f" by {due:%b %-d}" if due else ""
            add("deadline", it.get("subject", ""), it.get("message_id", ""),
                5, [f"your own ask{when}; answers owed to you"],
                it.get("from_name") or it.get("from", ""))
            continue
        if it.get("answered_at"):
            # The user wrote in this thread after the reminder: its date
            # is history, and a row that carries the thread has its own.
            add("deadline", it.get("subject", ""), it.get("message_id", ""),
                5, [f"you replied {str(it['answered_at'])[:10]}"],
                it.get("from_name") or it.get("from", ""))
            continue
        if due:
            days_left = (due - now).days
            if days_left < 0:
                score += 100; reasons.append(f"OVERDUE since {due:%b %-d}")
            elif days_left <= 2:
                score += 60; reasons.append(f"due {due:%b %-d} ({days_left}d)")
            elif days_left <= 7:
                score += 25; reasons.append(f"due {due:%b %-d}")
            else:
                reasons.append(f"due {due:%b %-d} (not urgent yet)")
        else:
            reasons.append(f"deadline: {it.get('deadline_text', '')[:40]}")
        add("deadline", it.get("subject", ""), it.get("message_id", ""),
            score, reasons, it.get("from_name") or it.get("from", ""))

    for it in rsvps or []:
        add("rsvp", it.get("subject", ""), it.get("message_id", ""),
            20, ["RSVP pending"], it.get("from_name") or it.get("from", ""))

    # Obligation scoring reads the DATE, not the entry type. Proximity
    # credit mirrors the email deadline path above, so the two paths rank
    # a given due date the same way.
    today = now.date()
    local_today = now.astimezone(_user_zone()).date()
    for ob in obligations or []:
        if ob.get("status") in ("snoozed", "follow_up", "completed"):
            continue
        # A future follow_up_after or snooze_until parks the row: it is
        # not actionable yet, so it never enters the queue or the digest,
        # whatever its deadline says.
        if _ledger_mod.parked_until(ob, local_today):
            continue
        # A row that looks done waits on the user's one-click confirm
        # (closure.py); it goes on the "Looks done?" line, not the queue.
        if isinstance(ob.get("close_proposal"), dict):
            continue
        # The thread ends on the user's own question: the next move is the
        # other side's, for ledger.WAITING_DAYS; the post lists it as waiting.
        if _ledger_mod.waiting_on_reply(ob, local_today):
            continue
        score, reasons = 5, []
        unanswered = _ledger_mod.unanswered_since(ob, local_today)
        if unanswered:
            reasons.append(f"no answer to your question since {unanswered}")
        # A row parked until today was parked FOR today: the day its
        # window opens is its day, deadline field or not. A clock time in
        # the description is the moment itself and rides along in the
        # reason so the writer can put it first with the time.
        unparks = _unparks_today(ob, local_today)
        if unparks:
            score += 60
            at = _clock_time_in(ob.get("description") or "")
            reasons.append(f"window opens today{' at ' + at if at else ''}")
        due = _parse_ledger_deadline(ob.get("deadline"))
        spent = _ledger_mod.date_spent(ob, local_today) if due else ""
        if spent:
            # The date passed, but it is not overdue work: a meeting that
            # has happened, or a reminder the user wrote back on after
            # its date. The row keeps its place; the date earns nothing.
            reasons.append(f"its {due:%b %-d} date passed; {spent}")
        elif due:
            # Ledger deadlines are dates, not instants, so compare whole
            # days. Otherwise anything due today reads as overdue by
            # -1 from mid-morning onward.
            days_left = (due.date() - today).days
            if days_left < 0:
                # The email path gives a flat +100 here, which works for
                # mail because a message stops arriving once it is dead.
                # A ledger row three weeks past due is usually blocked,
                # not urgent, so overdue enters the same band as "due
                # now" and earns a small premium per day late; priority
                # and staleness order the band.
                late = -days_left
                score += 60 + min(late * 3, 15)
                reasons.append(f"OVERDUE since {due:%b %-d} ({late}d late)")
            elif days_left == 0:
                score += 60; reasons.append(f"due TODAY ({due:%b %-d})")
            elif days_left <= 2:
                score += 60; reasons.append(f"due {due:%b %-d} ({days_left}d)")
            elif days_left <= 7:
                score += 25; reasons.append(f"due {due:%b %-d}")
            else:
                reasons.append(f"due {due:%b %-d} (not urgent yet)")
        elif ob.get("type") == "deadline":
            # No usable date, but the entry declares itself a deadline.
            # Keep the legacy flat credit for that case only: it is the
            # one place where the type still carries information the
            # date field does not.
            score += 40; reasons.append("ledger deadline (no date on entry)")
        # Free-text column like `deadline`: 'high' and 'low' are what the
        # EA writes, everything else (absent, 'normal', junk) is neutral.
        priority = str(ob.get("priority") or "").strip().lower()
        if priority == "high":
            score += 15; reasons.append("high priority")
        elif priority == "low":
            score -= 10
        stale = ob.get("days_stale", 0) or 0
        score += min(stale * 2, 20)
        if stale >= 5:
            reasons.append(f"open {stale}d without action")
        if not reasons:
            reasons.append("open obligation")
        add("obligation", _title(ob.get("description"), 100), ob.get("id", ""),
            score, reasons)

    scored.sort(key=lambda x: x["score"], reverse=True)
    today_top = [s for s in scored if s["score"] >= floor][:cap]
    top_refs = {s["ref"] for s in today_top}
    everything_else = [
        {"source": s["source"], "title": s["title"][:80], "score": s["score"]}
        for s in scored if s["ref"] not in top_refs
    ]
    return today_top, everything_else


# -------------------------------------------------------------------
# Configuration – customize these for your workflow
# -------------------------------------------------------------------
# VIP senders: emails from these addresses trigger priority alerts
VIP_SENDERS_FILE = EA_DIR / "config" / "vip_senders.txt"

# Scheduling keywords (case-insensitive)
# A request to MOVE a meeting that already exists. Detected as a scheduling
# request like the rest, and tagged kind="reschedule" so the proactive lane
# runs the reschedule brief (find the meeting, weigh the sender's history)
# instead of proposing from scratch.
RESCHEDULE_KEYWORDS = [
    r"reschedul", r"move (?:our|the|this|that) (?:meeting|call|1:1|one[- ]on[- ]one|chat|sync)",
    r"push (?:it|this|that|our meeting|our call)?\s*(?:back|out|to)",
    r"postpone", r"bump (?:our|the|this) (?:meeting|call)",
    r"no longer works?", r"doesn'?t work (?:for me )?(?:any ?more|now|after all)",
    r"(?:can'?t|cannot|won'?t be able to|unable to) make (?:it|our|the|that|this)",
    r"(?:something|a conflict|an? (?:appointment|meeting)) (?:came|has come) up",
    r"(?:a )?conflict (?:with|on|at) (?:our|the|that|this)",
    r"(?:different|another|new|later|earlier) (?:time|day|slot)",
    r"shift (?:our|the|this) (?:meeting|call)",
]
RESCHEDULE_PATTERN = re.compile("|".join(RESCHEDULE_KEYWORDS), re.IGNORECASE)

SCHEDULING_KEYWORDS = [
    "when are you free", "when are you available", "available times",
    "schedule a meeting", "find a time", "book a time",
    "can we meet", "let's meet", "doodle", "when2meet", "calendly",
    "would .* work for you", "what times work", "check your availability",
    "schedule a call", "set up a meeting", "open on your calendar",
    "on your calendar", "check your calendar", "send a calendar invite",
    "calendar invite", "still work for you", "still free",
    "are you still (?:free|available)",
    r"(?:%2F|\b)cal\.com(?:/|%2F)[\w.-]+(?:/|%2F)[\w.-]+",
] + RESCHEDULE_KEYWORDS
SCHEDULING_PATTERN = re.compile(
    "|".join(SCHEDULING_KEYWORDS), re.IGNORECASE
)


def classify_scheduling_kind(text: str) -> str:
    """"reschedule" when the mail asks to move a meeting that exists,
    else "new". The proactive lane branches on it."""
    return "reschedule" if RESCHEDULE_PATTERN.search(text or "") else "new"

# Past-tense / completed scheduling — NOT a request, skip these.
# Catches: "I scheduled a meeting", "thank you for filling out the when2meet",
# "meeting is scheduled for Tuesday", "I went ahead and scheduled", etc.
SCHEDULING_DONE_PATTERNS = re.compile(
    r"|".join([
        r"(?:i|we|i've|we've|i\s+have|we\s+have)\s+(?:already\s+|went\s+ahead\s+and\s+)?scheduled",
        r"(?:has|have)\s+been\s+scheduled",
        r"(?:is|are)\s+scheduled\s+for",
        r"meeting\s+(?:is|has\s+been)\s+set",
        r"(?:thank|thanks)\s+.{0,30}(?:filled|filling)\s+out",
        r"(?:filled|already\s+filled)\s+out\s+the\s+(?:when2meet|doodle|whenisgood|calendly|timeful|poll)",
        r"already\s+scheduled",
        r"went\s+ahead\s+and\s+(?:scheduled|booked|set\s+up)",
    ]),
    re.IGNORECASE,
)

# Priority keywords
PRIORITY_KEYWORDS = [
    "urgent", "asap", "immediately", "time-sensitive", "deadline",
    "action required", "please respond", "by end of day", "by eod",
    "by tomorrow", "overdue", "past due", "final notice", "reminder",
]
PRIORITY_PATTERN = re.compile(
    "|".join(PRIORITY_KEYWORDS), re.IGNORECASE
)

# Receipt / reimbursement indicators
RECEIPT_KEYWORDS = [
    "receipt", "invoice", "confirmation", "itinerary", "booking",
    "reservation", "hotel", "flight", "airfare", "uber", "lyft",
    "expense", "reimbursement", "travel authorization",
]
RECEIPT_PATTERN = re.compile("|".join(RECEIPT_KEYWORDS), re.IGNORECASE)

# Grant / project report keywords
GRANT_KEYWORDS = [
    "no.cost extension", "nce", "progress report", "annual report",
    "final report", "project report", "continuation", "renewal",
    "closeout", "award", "subaward", "sponsored research",
    "program officer", "nsf", "darpa", "aro", "afosr", "onr", "doe",
    "nih", "nasa",
]
GRANT_PATTERN = re.compile("|".join(GRANT_KEYWORDS), re.IGNORECASE)


def load_vip_senders() -> set:
    if VIP_SENDERS_FILE.exists():
        return {
            line.strip().lower()
            for line in VIP_SENDERS_FILE.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
    return set()


# -------------------------------------------------------------------
# Thread drop detection
# -------------------------------------------------------------------
def _is_list_mail(m: dict) -> bool:
    """Mail from a list named in list-digests.json belongs to the
    list-digest agent, which posts a summary and archives it. It is never
    a reply owed, a dropped thread, a deadline or a priority alert."""
    try:
        import list_digest
        return list_digest.is_list_mail(m)
    except Exception:
        return False


_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|tr)\s*:\s*)+", re.IGNORECASE)


def _norm_subject(subject: str) -> str:
    """Subject with every reply/forward prefix stripped, lowercased and
    whitespace-collapsed: the key that joins a thread's messages when the
    conversation id is missing."""
    s = _REPLY_PREFIX_RE.sub("", subject or "")
    return " ".join(s.lower().split())


def _user_started_threads(sent: list) -> tuple[dict, dict]:
    """(by conversation id, by normalized subject) -> the earliest time
    the user sent a message with a BARE subject, i.e. opened the thread."""
    by_conv: dict[str, str] = {}
    by_subject: dict[str, str] = {}
    for s in sent or []:
        subject = s.get("subject") or ""
        if _REPLY_PREFIX_RE.match(subject):
            continue
        ts = s.get("sentDateTime") or s.get("receivedDateTime") or ""
        if not ts:
            continue
        cid = s.get("conversationId") or ""
        if cid and (cid not in by_conv or ts < by_conv[cid]):
            by_conv[cid] = ts
        key = _norm_subject(subject)
        if key and (key not in by_subject or ts < by_subject[key]):
            by_subject[key] = ts
    return by_conv, by_subject


def _user_last_sent_by_conv(sent: list) -> dict[str, str]:
    """conversation id -> the latest time the user sent in that thread."""
    out: dict[str, str] = {}
    for s in sent or []:
        cid = s.get("conversationId") or ""
        ts = s.get("sentDateTime") or s.get("receivedDateTime") or ""
        if cid and ts and ts > out.get(cid, ""):
            out[cid] = ts
    return out


def detect_dropped_threads(
    inbox: list, sent: list, stale_days: int = 3
) -> list:
    """
    Find threads where:
    - Last message was inbound (in inbox)
    - No sent reply in the same conversation since
    - Older than stale_days
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    # Index: conversationId -> latest sent datetime
    sent_by_conv = {}
    for m in sent:
        conv = m.get("conversationId")
        if not conv:
            continue
        dt = datetime.fromisoformat(
            m["receivedDateTime"].replace("Z", "+00:00")
        )
        if conv not in sent_by_conv or dt > sent_by_conv[conv]:
            sent_by_conv[conv] = dt

    # Check inbox threads
    dropped = []
    seen_convs = set()
    for m in inbox:
        conv = m.get("conversationId")
        if not conv or conv in seen_convs:
            continue
        seen_convs.add(conv)
        if _is_list_mail(m):
            continue

        received = datetime.fromisoformat(
            m["receivedDateTime"].replace("Z", "+00:00")
        )
        if received > cutoff:
            continue  # Too recent to be "dropped"

        # Was there a sent reply after this message?
        last_sent = sent_by_conv.get(conv)
        if last_sent and last_sent >= received:
            continue  # We replied

        # Skip drafts, read-receipts, noreply
        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )
        if any(skip in from_addr for skip in [
            "noreply", "no-reply", "mailer-daemon", "postmaster",
            "notifications", "donotreply"
        ]):
            continue

        # Detect To/CC — is the user directly addressed or just CC'd?
        to_addrs = [r.get("emailAddress", {}).get("address", "").lower()
                    for r in m.get("toRecipients", [])]
        cc_addrs = [r.get("emailAddress", {}).get("address", "").lower()
                    for r in m.get("ccRecipients", [])]
        user_is_to = any(USER_EMAIL == a for a in to_addrs) if USER_EMAIL else True
        user_is_cc_only = (not user_is_to and
                           any(USER_EMAIL == a for a in cc_addrs)) if USER_EMAIL else False

        days_stale = (datetime.now(timezone.utc) - received).days

        # Categorize
        preview = m.get("bodyPreview", "") + " " + m.get("subject", "")
        category = "response_needed"
        if RECEIPT_PATTERN.search(preview):
            category = "reimbursement"
        elif GRANT_PATTERN.search(preview):
            category = "grant_action"
        elif SCHEDULING_PATTERN.search(preview):
            category = "scheduling"

        dropped.append({
            "message_id": m["id"],
            "conversation_id": conv,
            "subject": m.get("subject", "(no subject)"),
            "from": from_addr,
            "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "to": to_addrs,
            "cc": cc_addrs,
            "user_is_direct_recipient": user_is_to,
            "user_is_cc_only": user_is_cc_only,
            "received": m["receivedDateTime"],
            "days_stale": days_stale,
            "has_attachments": m.get("hasAttachments", False),
            "category": category,
            "importance": m.get("importance", "normal"),
            "is_flagged": m.get("flag", {}).get("flagStatus") == "flagged",
            "body_preview": m.get("bodyPreview", "")[:500],
        })

    # Sort by staleness descending
    dropped.sort(key=lambda x: x["days_stale"], reverse=True)
    return dropped


# -------------------------------------------------------------------
# Writing style analysis
# -------------------------------------------------------------------
def analyze_writing_style(sent: list) -> dict:
    """
    Extract the user's email writing patterns from sent mail.
    Returns greeting/closing preferences, avg length, and per-contact style.
    Zero extra API calls -- uses the sent mail already fetched.
    """
    # Common greetings and closings to detect
    GREETING_PATTERNS = [
        (r"^Hey (\w+)", "Hey {name},"),
        (r"^Hi (\w+)", "Hi {name},"),
        (r"^Hello (\w+)", "Hello {name},"),
        (r"^Dear (\w+)", "Dear {name},"),
        (r"^Good (?:morning|afternoon|evening)", "Good {time},"),
    ]
    CLOSING_PATTERNS = [
        "best", "cheers", "thanks", "thank you", "regards",
        "sincerely", "all the best", "take care", "warmly",
    ]

    # Per-contact accumulator
    contact_data = defaultdict(lambda: {
        "greetings": [], "closings": [], "lengths": [],
        "has_contractions": 0, "has_exclamation": 0, "count": 0,
    })
    all_greetings = []
    all_closings = []
    all_lengths = []
    contraction_count = 0
    total_count = 0

    for m in sent:
        preview = m.get("bodyPreview", "").strip()
        if not preview or len(preview) < 20:
            continue

        # Identify primary recipient
        to_list = m.get("toRecipients", [])
        if not to_list:
            continue
        recip_addr = to_list[0].get("emailAddress", {}).get("address", "").lower()

        total_count += 1
        word_count = len(preview.split())
        all_lengths.append(word_count)

        cd = contact_data[recip_addr]
        cd["count"] += 1
        cd["lengths"].append(word_count)

        # Detect greeting (first line)
        first_line = preview.split("\n")[0].strip().rstrip(",")
        for pat, fmt in GREETING_PATTERNS:
            if re.match(pat, first_line, re.IGNORECASE):
                all_greetings.append(fmt)
                cd["greetings"].append(fmt)
                break

        # Detect closing (last few lines)
        lines = [l.strip() for l in preview.split("\n") if l.strip()]
        for line in lines[-3:]:
            line_lower = line.rstrip(",.!").lower()
            for closing in CLOSING_PATTERNS:
                if line_lower == closing or line_lower.startswith(closing + ","):
                    all_closings.append(closing.title())
                    cd["closings"].append(closing.title())
                    break

        # Formality markers
        has_contraction = bool(re.search(
            r"\b(don't|can't|won't|wouldn't|shouldn't|couldn't|isn't|aren't|I'm|I'll|I've|it's|that's|there's|we're|they're)\b",
            preview, re.IGNORECASE
        ))
        if has_contraction:
            contraction_count += 1
            cd["has_contractions"] += 1
        if "!" in preview:
            cd["has_exclamation"] += 1

    if total_count == 0:
        return {}

    # Compute defaults
    def most_common(lst):
        if not lst:
            return None
        counts = defaultdict(int)
        for item in lst:
            counts[item] += 1
        return max(counts, key=counts.get)

    avg_len = sum(all_lengths) // len(all_lengths) if all_lengths else 80

    # Build per-contact profiles (only for contacts with 3+ emails)
    per_contact = {}
    for addr, cd in contact_data.items():
        if cd["count"] < 3:
            continue
        avg_contact_len = sum(cd["lengths"]) // len(cd["lengths"])
        greeting = most_common(cd["greetings"])
        closing = most_common(cd["closings"])

        # Infer tone from formality markers
        contraction_rate = cd["has_contractions"] / cd["count"]
        if contraction_rate > 0.5:
            tone = "casual"
        elif contraction_rate > 0.2:
            tone = "casual-professional"
        else:
            tone = "formal"

        per_contact[addr] = {
            "greeting": greeting,
            "closing": closing,
            "avg_length": avg_contact_len,
            "sample_count": cd["count"],
            "tone": tone,
        }

    return {
        "default_greeting": most_common(all_greetings) or "Hi {name},",
        "default_closing": most_common(all_closings) or "Best,",
        "avg_length_words": avg_len,
        "uses_contractions": contraction_count > total_count * 0.3,
        "per_contact": per_contact,
    }


# The remote record's scheduling kinds that make a request of the lane.
# "confirmation" is the other side accepting a time the user offered: the
# keyword pass never sees it (the user has written to that thread), and it
# is the moment an invite and a confirming reply are due.
RECORD_SCHED_KINDS = ("request", "proposes-times", "reschedule", "confirmation")


def _sched_description(kind: str, subject: str) -> str:
    lead = {"reschedule": "Reschedule request: ", "confirmation": "Time accepted: ",
            "proposes-times": "Times proposed: "}.get(kind or "", "Scheduling request: ")
    return lead + (subject or "")[:80]


def _sent_with_bodies(days: int = 14) -> list:
    """The user's Sent mail with bodies, for the thread-state pass: an
    offer of times is longer than a preview. Empty when Graph is not
    reachable; the pass then finds nothing and the record pass stands."""
    try:
        return graph.get_sent(days_back=days, full_body=True)
    except Exception as e:
        print(f"  sent bodies not read ({e}); the thread-state pass is skipped", file=sys.stderr)
        return []


def detect_scheduling_requests(inbox: list, sent: list, gate=None, sent_full: list | None = None) -> list:
    """Find emails that contain scheduling requests we haven't responded to.

    Two passes: the keyword list over the preview (a thread the user has
    already written to is skipped there), then, with a mail gate, the
    remote records: a message whose record says it is scheduling (an
    acceptance of a time the user offered, times proposed back, a request
    the list misses) is a request too, also in a thread the user has
    written to, as long as it arrived after the user's last message there.
    A record item carries the record's `offered_times` and `ask`.

    A third pass needs no model: where the user's own last message in a
    thread names clock times (sched_thread_state), an inbound after it
    that names one of them is an acceptance (kind "confirmation", the one
    time) and one that names other times is a counter ("proposes-times").
    `sent_full` is the Sent mail with bodies; with a gate and none given
    it is fetched once."""
    sent_convs = {m.get("conversationId") for m in sent if m.get("conversationId")}
    requests_found = []
    import mail_privacy as _mp

    for m in inbox:
        preview = (m.get("bodyPreview", "") + " " + m.get("subject", "")).lower()
        if not SCHEDULING_PATTERN.search(preview):
            continue
        # Skip if the email reports scheduling is DONE, not requesting it
        if SCHEDULING_DONE_PATTERNS.search(preview):
            continue
        # Skip if we already replied
        if m.get("conversationId") in sent_convs:
            continue
        from_addr = m.get("from", {}).get("emailAddress", {}).get("address", "")
        if any(skip in from_addr.lower() for skip in ["noreply", "no-reply", "notifications"]):
            continue

        requests_found.append({
            "message_id": m["id"],
            "conversation_id": m.get("conversationId", ""),
            "subject": m.get("subject", ""),
            "from": from_addr,
            "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "received": m["receivedDateTime"],
            "body_preview": m.get("bodyPreview", "")[:500],
            "kind": classify_scheduling_kind(preview),
        })

    if gate is not None and getattr(gate, "on", False):
        sent_times: dict = {}
        for m in sent:
            cid = m.get("conversationId")
            ts = m.get("sentDateTime") or m.get("receivedDateTime", "")
            if cid and ts > sent_times.get(cid, ""):
                sent_times[cid] = ts
        have = {r["message_id"] for r in requests_found}
        for m in inbox:
            mid = m.get("id", "")
            if not mid or mid in have:
                continue
            if _mp.CALENDAR_RESPONSE_SUBJECT.match(m.get("subject") or ""):
                continue  # Outlook's own meeting response, not an ask
            rec = gate.record_fields({"message_id": mid, "conversation_id": m.get("conversationId", "")})
            if not rec or rec.get("scheduling") not in RECORD_SCHED_KINDS:
                continue
            conv = m.get("conversationId", "")
            if conv and (m.get("receivedDateTime", "") or "") <= sent_times.get(conv, ""):
                continue  # the user wrote after it: answered
            from_addr = m.get("from", {}).get("emailAddress", {}).get("address", "")
            if any(skip in from_addr.lower() for skip in ["noreply", "no-reply", "notifications"]):
                continue
            kind = rec.get("scheduling")
            requests_found.append({
                "message_id": mid,
                "conversation_id": conv,
                "subject": m.get("subject", ""),
                "from": from_addr,
                "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                "received": m.get("receivedDateTime", ""),
                "body_preview": m.get("bodyPreview", "")[:500],
                "kind": kind if kind in ("reschedule", "confirmation", "proposes-times") else "new",
                "offered_times": [str(t) for t in (rec.get("offered_times") or [])],
                "ask": str(rec.get("ask") or "")[:300],
                "from_record": True,
            })

    if gate is not None and sent_full is None:
        sent_full = _sent_with_bodies()
    if sent_full:
        import sched_thread_state as _sts
        offers = _sts.offers_from_sent(sent_full)
        have = {r["message_id"]: r for r in requests_found}
        for m in inbox:
            mid, conv = m.get("id", ""), m.get("conversationId", "")
            offer = offers.get(conv)
            if not mid or not offer or (m.get("receivedDateTime", "") or "") <= offer["at"]:
                continue
            if _mp.CALENDAR_RESPONSE_SUBJECT.match(m.get("subject") or ""):
                continue
            from_addr = m.get("from", {}).get("emailAddress", {}).get("address", "")
            if any(skip in from_addr.lower() for skip in ["noreply", "no-reply", "notifications"]):
                continue
            state, picks, others = _sts.classify_reply(m.get("bodyPreview", "") or "", offer["times"])
            if state == "accepted":
                kind, times, note = "confirmation", [picks[0]["start"]], f"Accepted {picks[0]['start']}, one of the times the user offered."
            elif state in ("counter", "several"):
                kind, times, note = "proposes-times", [t["start"] for t in (picks + others)], "Named other times than the user offered."
            else:
                continue
            prior = have.get(mid)
            if prior is not None:
                # The record pass found it too: the thread's own times win,
                # and the two agree or the item says they do not.
                prior["offered_times"] = times
                prior["kind"] = kind
                prior["offered_match"] = (state == "accepted")
                continue
            requests_found.append({
                "message_id": mid, "conversation_id": conv, "subject": m.get("subject", ""),
                "from": from_addr, "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                "received": m.get("receivedDateTime", ""), "body_preview": m.get("bodyPreview", "")[:500],
                "kind": kind, "offered_times": times, "ask": note, "from_state": True,
                "offered_match": state == "accepted",
            })

    return requests_found


PROACTIVE_QUEUE_FILE = STATE_DIR / "scheduling-proactive-queue.json"
PROACTIVE_QUEUE_MAX_AGE_H = 72


def register_new_scheduling_requests(scheduling: list) -> list:
    """The requests the ledger has not seen: add each as a sched- row (so
    it never re-notifies) and return them. Shared by the hourly
    scheduling check and the ten-minute intake, so a request is opened
    once whichever sees it first."""
    known_ids = _ledger_mod.get_all_source_message_ids()
    new = [s for s in scheduling if s["message_id"] not in known_ids]
    for s in new:
        notes = s.get("body_preview", "")[:300]
        if s.get("offered_times"):
            notes = f"Times named: {', '.join(s['offered_times'][:6])}. " + (s.get("ask") or notes)
        _ledger_mod.add_entry(
            entry_id=f"sched-{s['message_id'][-12:]}",
            entry_type="scheduling",
            description=_sched_description(s.get("kind"), s["subject"]),
            from_email=s.get("from", ""),
            from_name=s.get("from_name", ""),
            source_message_id=s["message_id"],
            notes=notes[:500],
        )
    return new


def sched_channel_configured(chat: dict | None = None) -> bool:
    """Is a dedicated scheduling channel configured for the chat frontend?
    On Zulip it is chat.zulip.sched_stream; on Slack, EA_SCHED_CHANNEL_ID."""
    if chat is None:
        try:
            import ea_config
            chat = ea_config.get_chat()
        except Exception:
            chat = {}
    if (chat.get("frontend") or "slack") == "zulip":
        return bool(str((chat.get("zulip") or {}).get("sched_stream") or "").strip())
    return bool(os.environ.get("EA_SCHED_CHANNEL_ID", "").strip())


def write_proactive_queue(reqs: list, path: Path | None = None,
                          now: datetime | None = None) -> int:
    """Merge new scheduling requests into the bot's proactive queue.

    The bot opens each queued request once (it keeps a seen set keyed by
    conversation id), so the queue is a union by conversation id, never an
    overwrite: two writers minutes apart (intake, then the hourly check)
    must not drop an entry the bot has not read yet. Entries older than
    PROACTIVE_QUEUE_MAX_AGE_H fall off. Only written when a scheduling
    channel is configured. Returns the queue length."""
    if not sched_channel_configured():
        return 0
    path = path or PROACTIVE_QUEUE_FILE
    now = now or datetime.now(timezone.utc)
    # Read with the resident's access (resident_io refuses a link or a hard
    # link at the name): the merge is written back into the resident's state
    # directory, so the read must not hand this process a file only the
    # owner may read.
    try:
        text = _rio.read_text(path)
        existing = json.loads(text) if text else []
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    merged: dict = {}
    for r in existing + list(reqs or []):
        if not isinstance(r, dict):
            continue
        key = r.get("conversation_id") or r.get("message_id") or ""
        if not key:
            continue
        try:
            age_h = (now - datetime.fromisoformat(
                str(r.get("received", "")).replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:
            age_h = 0.0
        if age_h > PROACTIVE_QUEUE_MAX_AGE_H:
            continue
        merged[key] = r
    out = list(merged.values())
    _atomic.write_json(path, out, indent=None)
    return len(out)


# -------------------------------------------------------------------
# Priority email detection
# -------------------------------------------------------------------
def detect_priority_emails(
    inbox: list, vip_senders: set, sent: list = None, hours_back: int = 4
) -> list:
    """Find high-priority emails from recent hours.

    If sent is provided, skip emails in conversations the user already replied
    to, unless new inbound arrived after the latest sent timestamp. Outlook's
    auto-generated "Accepted:" calendar-acceptance message lands in the sent
    folder too, so accepting an invite also counts as closing the thread.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    sent_conv_times: dict = {}
    if sent:
        for s in sent:
            cid = s.get("conversationId")
            if cid:
                ts = s.get("sentDateTime") or s.get("receivedDateTime", "")
                if ts > sent_conv_times.get(cid, ""):
                    sent_conv_times[cid] = ts

    priority = []

    for m in inbox:
        received = datetime.fromisoformat(
            m["receivedDateTime"].replace("Z", "+00:00")
        )
        if received < cutoff:
            continue
        if _is_list_mail(m):
            continue

        conv = m.get("conversationId")
        if conv and conv in sent_conv_times:
            if m.get("receivedDateTime", "") <= sent_conv_times[conv]:
                continue

        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )
        preview = m.get("bodyPreview", "") + " " + m.get("subject", "")
        is_vip = from_addr in vip_senders
        is_urgent = (
            m.get("importance") == "high"
            or PRIORITY_PATTERN.search(preview)
        )
        is_flagged = m.get("flag", {}).get("flagStatus") == "flagged"

        if is_vip or is_urgent or is_flagged:
            priority.append({
                "message_id": m["id"],
                "subject": m.get("subject", ""),
                "from": from_addr,
                "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                "received": m["receivedDateTime"],
                "body_preview": m.get("bodyPreview", "")[:500],
                "is_vip": is_vip,
                "is_urgent": is_urgent,
                "is_flagged": is_flagged,
                "importance": m.get("importance", "normal"),
            })

    return priority


# -------------------------------------------------------------------
# Receipt / travel expense detection
# -------------------------------------------------------------------
def detect_unreimbursed_travel(
    inbox: list, sent: list, calendar_events: list
) -> list:
    """
    Cross-reference travel-related emails/attachments against calendar
    to find trips that may not have been reimbursed.
    """
    # Find calendar events that look like travel
    travel_events = []
    for e in calendar_events:
        subj = (e.get("subject", "") or "").lower()
        loc = (e.get("location", {}).get("displayName", "") or "").lower()
        if any(kw in subj + " " + loc for kw in [
            "travel", "flight", "conference", "workshop", "visit",
            "ttx", "exercise", "summit", "symposium", "meeting"
        ]):
            travel_events.append(e)

    # Find receipt-like emails
    receipt_emails = []
    for m in inbox:
        preview = (
            m.get("bodyPreview", "") + " " + m.get("subject", "")
        )
        if RECEIPT_PATTERN.search(preview) and m.get("hasAttachments"):
            receipt_emails.append(m)

    # Check if reimbursement was submitted (look for sent emails about it)
    reimbursement_sent = any(
        RECEIPT_PATTERN.search(m.get("subject", "") + " " + m.get("bodyPreview", ""))
        and any(kw in (m.get("bodyPreview", "") + " " + m.get("subject", "")).lower()
                for kw in ["reimbursement", "expense report", "receipts attached"])
        for m in sent
    )

    findings = []
    if receipt_emails and not reimbursement_sent:
        findings.append({
            "type": "unreimbursed_receipts",
            "receipt_count": len(receipt_emails),
            "related_travel_events": [
                {
                    "subject": e.get("subject", ""),
                    "start": e["start"]["dateTime"][:10],
                    "location": e.get("location", {}).get("displayName", ""),
                }
                for e in travel_events[:5]
            ],
            "receipt_subjects": [
                m.get("subject", "") for m in receipt_emails[:10]
            ],
            "receipt_message_ids": [m["id"] for m in receipt_emails[:10]],
        })

    return findings


# -------------------------------------------------------------------
# Needs-response detection (simpler than dropped threads)
# -------------------------------------------------------------------
def _on_domain(addr: str, domain: str) -> bool:
    """True when the address's host is `domain` or a subdomain of it;
    `notexample.edu` is not on `example.edu`."""
    domain = str(domain or "").strip().lower().lstrip("@")
    addr = str(addr or "").strip().lower()
    if not domain or "@" not in addr:
        return False
    host = addr.rsplit("@", 1)[-1].rstrip(">").strip()
    return host == domain or host.endswith("." + domain)


def detect_needs_response(inbox: list, sent: list) -> list:
    """
    Emails in the inbox that likely need a response.
    Include-by-default: every unreplied human email is a candidate.
    Automated/newsletter/notification emails are excluded.
    Positive signals determine priority ranking, NOT inclusion.
    """
    user_email = USER_EMAIL

    # Map conversation ID → latest sent datetime (to detect new inbound after reply)
    sent_conv_times = {}
    for m in sent:
        cid = m.get("conversationId")
        if cid:
            ts = m.get("sentDateTime") or m.get("receivedDateTime", "")
            if ts > sent_conv_times.get(cid, ""):
                sent_conv_times[cid] = ts
    sent_convs = set(sent_conv_times.keys())

    # Build set of addresses the user has emailed (known contacts)
    sent_to_addrs = set()
    for s in sent:
        for recip in s.get("toRecipients", []):
            addr = recip.get("emailAddress", {}).get("address", "")
            if addr:
                sent_to_addrs.add(addr.lower())

    vip_senders = load_vip_senders()
    _own_domain = _institution.own_domain()

    # Automated sender patterns (exclude these entirely)
    AUTO_SENDER_PATTERNS = [
        "noreply", "no-reply", "mailer-daemon", "notifications",
        "donotreply", "do-not-reply", "calendar", "teams",
        "bounce", "postmaster", "daemon", "automated",
        "alerts@", "digest@", "updates@",
        "marketing@", "promo@", "news@",
        "support@", "billing@", "service@",
    ]
    # Automated sender domains (full domain match, exclude)
    AUTO_SENDER_DOMAINS = [
        "mdpi.com", "mail.wandb.ai", "papercept.net",
        "aimgroup.eu", "wiley.com", "elsevier.com",
        "springer.com", "ieee.org", "acm.org",
        "mail.google.com", "facebookmail.com",
        "linkedin.com", "indeed.com",
    ]
    # Automated subject patterns (exclude)
    AUTO_SUBJECT_PATTERNS = [
        "unsubscribe", "newsletter", "weekly digest", "daily digest",
        "out of office", "automatic reply", "auto-reply", "autoreply",
        "invitation to review", "manuscript id:",
        "your trial", "your subscription",
        # Receipt-shaped confirmations: the system acknowledging YOUR
        # action owes no reply.
        "we received your", "has been received", "request received",
        "approved! your", "has been approved",
        "thank you for submitting", "confirmation of your",
    ]
    # Calendar notification prefixes (exclude)
    CALENDAR_PREFIXES = [
        "accepted:", "declined:", "tentative:", "canceled:",
        "updated invitation:", "invitation:",
    ]

    needs_response = []
    seen_convs = set()

    for m in inbox:
        conv = m.get("conversationId")
        if conv in seen_convs:
            continue
        seen_convs.add(conv)
        if _is_list_mail(m):
            continue

        # Detect To/CC — is the user directly addressed or just CC'd?
        to_addrs = [r.get("emailAddress", {}).get("address", "").lower()
                    for r in m.get("toRecipients", [])]
        cc_addrs = [r.get("emailAddress", {}).get("address", "").lower()
                    for r in m.get("ccRecipients", [])]
        user_is_to = any(user_email == a for a in to_addrs) if user_email else True
        user_is_cc_only = (not user_is_to and
                           any(user_email == a for a in cc_addrs)) if user_email else False

        # Skip conversations we replied to UNLESS there's a newer inbound
        user_already_replied = conv in sent_convs
        if user_already_replied:
            last_sent = sent_conv_times[conv]
            msg_received = m.get("receivedDateTime", "")
            if msg_received <= last_sent:
                continue
            # New inbound after our last reply — this is a new turn

        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )
        subject = m.get("subject", "")
        subject_lower = subject.lower()

        # --- Exclusions: automated/non-human emails ---
        if any(pat in from_addr for pat in AUTO_SENDER_PATTERNS):
            continue
        from_domain = from_addr.split("@")[-1] if "@" in from_addr else ""
        if from_domain in AUTO_SENDER_DOMAINS:
            continue
        if any(pat in subject_lower for pat in AUTO_SUBJECT_PATTERNS):
            continue
        if any(subject_lower.startswith(pfx) for pfx in CALENDAR_PREFIXES):
            continue
        # Delivery status / read receipts
        if m.get("internetMessageHeaders"):
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in m["internetMessageHeaders"]}
            if "x-auto-response-suppress" in headers:
                continue
            if headers.get("auto-submitted", "") not in ("", "no"):
                continue

        # --- Priority scoring (higher = more urgent, but all items included) ---
        preview = m.get("bodyPreview", "")
        preview_lower = preview.lower()
        signals = 1  # baseline: someone emailed you and you haven't replied

        # Content signals
        if "?" in preview:
            signals += 1
        if any(w in preview_lower for w in [
            "please", "could you", "can you", "would you",
            "let me know", "your thoughts", "rsvp", "respond",
            "action required", "needed", "deadline",
        ]):
            signals += 1

        # Metadata signals
        if m.get("importance") == "high":
            signals += 2
        if m.get("flag", {}).get("flagStatus") == "flagged":
            signals += 2

        # Relationship signals
        if from_addr in vip_senders:
            signals += 3
        if _on_domain(from_addr, _own_domain):
            signals += 1
        if from_addr in sent_to_addrs:
            signals += 1  # known contact

        # Direct message (not CC'd to many people)
        to_count = len(m.get("toRecipients", []))
        cc_count = len(m.get("ccRecipients", []))
        if to_count <= 2 and cc_count == 0:
            signals += 1  # personal/direct email

        # Staleness
        received = datetime.fromisoformat(
            m["receivedDateTime"].replace("Z", "+00:00")
        )
        days_old = (datetime.now(timezone.utc) - received).days
        if days_old > 7:
            signals += 2  # old and still in inbox
        if days_old > 14:
            signals += 2  # very old

        needs_response.append({
            "message_id": m["id"],
            "conversation_id": conv,
            "subject": subject,
            "from": from_addr,
            "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "to": to_addrs,
            "cc": cc_addrs,
            "user_is_direct_recipient": user_is_to,
            "user_is_cc_only": user_is_cc_only,
            "user_already_replied_in_thread": user_already_replied,
            "received": m["receivedDateTime"],
            "days_in_inbox": days_old,
            "body_preview": preview[:500],
            "importance": m.get("importance", "normal"),
            "response_signals": signals,
        })

    needs_response.sort(key=lambda x: x["response_signals"], reverse=True)
    return needs_response


# -------------------------------------------------------------------
# Group thread detection (for thread-digest skill)
# -------------------------------------------------------------------
# Group lists live in config/group-senders.json; the code ships none.
_GROUP_SENDERS_FALLBACK: dict = {}
GROUP_SENDERS = _load_config_json("group-senders.json", _GROUP_SENDERS_FALLBACK)

# Flatten for fast lookup
_ALL_GROUP_ADDRS = set()
_ADDR_TO_GROUP = {}
for _grp, _addrs in GROUP_SENDERS.items():
    for _a in _addrs:
        _ALL_GROUP_ADDRS.add(_a.lower())
        _ADDR_TO_GROUP[_a.lower()] = _grp


def detect_noisy_group_threads(inbox: list) -> list:
    """Identify group email threads with many replies (reply-all storms).

    Returns threads grouped by group name with message count and last
    activity, so the thread-digest skill can summarize them efficiently.
    """
    # Group by conversationId
    conv_messages: dict[str, list] = defaultdict(list)
    conv_group: dict[str, str] = {}

    for m in inbox:
        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )
        # Check To/CC for group addresses too
        all_addrs = [from_addr]
        for recip in m.get("toRecipients", []):
            all_addrs.append(
                recip.get("emailAddress", {}).get("address", "").lower()
            )
        for recip in m.get("ccRecipients", []):
            all_addrs.append(
                recip.get("emailAddress", {}).get("address", "").lower()
            )

        matched_group = None
        for addr in all_addrs:
            if addr in _ADDR_TO_GROUP:
                matched_group = _ADDR_TO_GROUP[addr]
                break

        if matched_group:
            conv_id = m.get("conversationId", m["id"])
            conv_messages[conv_id].append(m)
            conv_group[conv_id] = matched_group

    # Only surface threads with >= 3 messages (actual noise)
    noisy = []
    for conv_id, messages in conv_messages.items():
        if len(messages) < 3:
            continue
        latest = max(messages, key=lambda x: x.get("receivedDateTime", ""))
        noisy.append({
            "conversation_id": conv_id,
            "group": conv_group[conv_id],
            "subject": latest.get("subject", ""),
            "message_count": len(messages),
            "latest_received": latest.get("receivedDateTime", ""),
            "latest_from": latest.get("from", {}).get("emailAddress", {}).get("name", ""),
            "latest_preview": latest.get("bodyPreview", "")[:300],
        })

    noisy.sort(key=lambda x: x["message_count"], reverse=True)
    return noisy


# -------------------------------------------------------------------
# Deadline extraction (for deadline-watchdog skill)
# -------------------------------------------------------------------
DEADLINE_PATTERN = re.compile(
    r"(?:due|deadline|by|before|submit.+?by|must be completed by|"
    r"respond by|action required|overdue|expir)"
    r"[:\s]*"
    r"(\w+\s+\d{1,2}(?:,?\s*\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+night|\s+morning|\s+evening)?)",
    re.IGNORECASE,
)

# Sender domain -> deadline category. Personal vendors (insurers, banks,
# clubs) belong in config/deadline-senders.json; the code keeps only the
# institutional default: the institution's own domains are "university".
_DEADLINE_SENDERS_FALLBACK = {d: "university" for d in _institution.domains()}
DEADLINE_SENDERS = _load_config_json("deadline-senders.json", _DEADLINE_SENDERS_FALLBACK)


def detect_deadlines(inbox: list, sent: list | None = None) -> list:
    """Extract emails with deadline signals, categorized by domain.

    One item per conversation (its newest message), and a date the user
    set for OTHER people is marked `owed_to_user`. Otherwise a charge the
    user wrote himself mints one OVERDUE item per reply. `sent` is what
    says who opened the thread; without it nothing is owed_to_user. An
    item the user has answered in its own thread since it arrived carries
    `answered_at`: the charge was met or taken up, so it is not overdue
    work (build_today_top)."""
    root_by_conv, root_by_subject = _user_started_threads(sent or [])
    last_sent = _user_last_sent_by_conv(sent or [])
    best: dict[str, dict] = {}
    order: list[str] = []
    for m in inbox:
        if _is_list_mail(m):
            continue
        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )
        subject = m.get("subject", "")
        preview = m.get("bodyPreview", "")
        combined = f"{subject} {preview}".lower()

        # Check for deadline keywords
        match = DEADLINE_PATTERN.search(combined)
        has_reminder = "reminder" in subject.lower()
        has_action = "action" in subject.lower() or "take action" in combined

        if not (match or has_reminder or has_action):
            continue

        # Categorize by sender domain
        domain = "general"
        for sender_pattern, cat in DEADLINE_SENDERS.items():
            if sender_pattern in from_addr:
                domain = cat
                break

        received = m.get("receivedDateTime", "")
        conv = m.get("conversationId") or m["id"]
        opened = root_by_conv.get(conv) or root_by_subject.get(_norm_subject(subject), "")
        item = {
            "message_id": m["id"],
            "conversation_id": m.get("conversationId", ""),
            "subject": subject,
            "from": from_addr,
            "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "received": received,
            "domain": domain,
            "deadline_text": match.group(0) if match else "",
            "extracted_date": match.group(1) if match else "",
            "body_preview": preview[:400],
            "is_reminder": has_reminder,
            # The user opened this thread before the message arrived: the
            # date in it is the user's ask of others, not the user's work.
            "owed_to_user": bool(opened and received and opened < received),
        }
        prev = best.get(conv)
        if prev is None:
            best[conv] = item
            order.append(conv)
        elif received > (prev.get("received") or ""):
            best[conv] = item

    for conv in order:
        item = best[conv]
        answered = last_sent.get(item["conversation_id"]) if item["conversation_id"] else ""
        if answered and answered > (item.get("received") or ""):
            item["answered_at"] = answered
    return [best[c] for c in order]


# -------------------------------------------------------------------
# RSVP / invitation detection (for rsvp-handler skill)
# -------------------------------------------------------------------
RSVP_PATTERN = re.compile(
    r"(?:rsvp|invitation|you(?:'re| are) invited|"
    r"please (?:register|confirm|respond)|"
    r"will you (?:be |attend)|"
    r"can you (?:make it|attend|come))",
    re.IGNORECASE,
)


def detect_pending_rsvps(inbox: list) -> list:
    """Find invitation/RSVP emails that haven't been responded to."""
    rsvps = []
    for m in inbox:
        if _is_list_mail(m):
            continue
        subject = m.get("subject", "")
        preview = m.get("bodyPreview", "")
        combined = f"{subject} {preview}"

        # Calendar invitation emails
        is_calendar_invite = (
            "invitation:" in subject.lower()
            or "invited" in subject.lower()
            or "rsvp" in combined.lower()
        )

        if not (is_calendar_invite or RSVP_PATTERN.search(combined)):
            continue

        from_addr = (
            m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
        )

        rsvps.append({
            "message_id": m["id"],
            "subject": subject,
            "from": from_addr,
            "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
            "received": m.get("receivedDateTime", ""),
            "body_preview": preview[:400],
            "is_calendar_invite": "invitation:" in subject.lower(),
        })

    return rsvps


# -------------------------------------------------------------------
# Teams DMs (an owner plugin's verb)
# -------------------------------------------------------------------
def get_teams_dms(days_back: int = 1) -> list:
    """Recent Teams 1:1 DMs from the owner plugin that declares
    `graph_actions get-teams-dms` (docs/plugins.md), in the shape that verb
    prints under "chats". [] when no enabled plugin declares it, or when
    the plugin fails or has no token."""
    import plugin_verbs
    res = plugin_verbs.call_alias("graph_actions", "get-teams-dms",
                                  ["--days", str(int(days_back))])
    if res is None:
        return []
    rc, out, err = res
    if rc != 0:
        print(f"  Teams DMs: the plugin verb failed (exit {rc}): {err.strip()[-200:]}",
              file=sys.stderr)
        return []
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return []
    chats = data.get("chats") if isinstance(data, dict) else None
    return chats if isinstance(chats, list) else []


# -------------------------------------------------------------------
# Availability computation
# -------------------------------------------------------------------
# Calendar read failures from the most recent
# compute_merged_availability() call.
LAST_CALENDAR_FETCH_ERRORS: list = []


def compute_merged_availability(
    outlook_events: list,
    days_ahead: int = 14,
) -> list:
    """
    Free time slots across ALL calendars:
    - Outlook events (passed in)
    - All configured Google Calendar accounts (fetched automatically)

    Delegates to `scheduling_engine.compute_free_slots`, the single
    availability model: working hours, no-meeting blocks and preferences
    come from the tokened events on the EA-Managed calendar, exactly as
    the solver reads them. A second implementation here would publish
    slots the solver rejects.

    A slot is free ONLY if free in ALL calendars.
    """
    # Fetch Google Calendar events for all configured accounts
    google_events = gcal.get_all_events(days_ahead=days_ahead)

    result = sched_engine.compute_free_slots(
        outlook_events=outlook_events,
        google_events=google_events,
        days_ahead=days_ahead,
    )

    # Surface per-account fetch failures instead of silently treating a
    # dead account as "completely free" (which makes the solver propose
    # times over real meetings). Briefing builders copy this into
    # `calendar_fetch_errors`; skills must warn and treat availability
    # as non-authoritative when non-empty.
    global LAST_CALENDAR_FETCH_ERRORS
    LAST_CALENDAR_FETCH_ERRORS = list(result.get("calendar_fetch_errors", []))
    for err in LAST_CALENDAR_FETCH_ERRORS:
        print(f"  WARNING calendar fetch failed: {err['account']}: "
              f"{err['error']}", file=sys.stderr)

    return result.get("slots", [])


# -------------------------------------------------------------------
# Obligations ledger management (SQLite via ledger.py)
# -------------------------------------------------------------------
import ledger as _ledger_mod
import ea_config as _ea_config
import scheduled_sends as _scheduled_sends_mod


MAIL_ROW_PREFIXES = ("response-", "task-", "deadline-")


def mail_row_kind(item: dict) -> dict:
    """The row a needs-response item becomes: {prefix, type, description,
    deadline, notes}. Without a remote record (the mail switch off) it is
    the response row it always was. With one (enforce), the record's
    state and deadline type it: a dated todo or decision is a `deadline`
    row, an undated one a `task` row, the rest a `response_needed` row.
    A nudge about work the user owes thus tracks the work, not the
    nudge, and archiving the nudge does not close it (only response rows
    are detector rows)."""
    subject = item.get("subject") or "(no subject)"
    rr = item.get("remote_record") if isinstance(item.get("remote_record"), dict) else None
    out = {"prefix": "response-", "type": "response_needed",
           "description": f"Needs response: {subject}", "deadline": "",
           "notes": (item.get("body_preview") or "")[:500]}
    if not rr:
        return out
    state, deadline = rr.get("state"), str(rr.get("deadline") or "")[:10]
    owes = state in ("todo", "decide")
    # A meeting's date (a reminder, an invitation, a time proposal) is
    # when it happens, not a date the user owes something by: it never
    # mints a deadline row, which would read OVERDUE once the meeting
    # has been held.
    if str(rr.get("scheduling") or "").strip().lower() in _ledger_mod.MEETING_KINDS:
        deadline = ""
    if deadline and (owes or not rr.get("needs_reply")):
        out.update(prefix="deadline-", type="deadline", deadline=deadline,
                   description=f"Due {deadline}: {subject}")
    elif owes:
        out.update(prefix="task-", type="task", description=f"Task: {subject}")
    if rr.get("ask"):
        out["notes"] = (f"{out['notes']} Ask: {str(rr['ask']).strip()}").strip()[:500]
    return out


def _reopen_mail_row_if_newer(entry_id: str, item: dict, kind: dict) -> bool:
    """A thread's row is keyed by the conversation, so a second ask in a
    thread whose row was closed found the id taken and was dropped. When
    the new message arrived after the close, the row opens again on it:
    the new source id, the new ask, and closure's reopen record (the
    evidence that closed it does not close it again)."""
    row = _ledger_mod.get_entry(entry_id)
    if not row or row.get("status") not in ("completed", "closed"):
        return False
    closed_at = row.get("closed_at") or row.get("updated_at") or ""
    received = item.get("received", "") or ""
    if not received or received <= closed_at:
        return False
    try:
        import closure
        db = _ledger_mod._get_db()
        db.row_factory = sqlite3.Row
        sql_row = db.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        try:
            prior = json.loads(sql_row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            prior = {}
        meta = closure.note_reopen(db, sql_row, actor="intake", meta=prior)
        now = _ledger_mod._now()
        db.execute("""UPDATE ledger SET status = 'open', source_message_id = ?, description = ?,
                      notes = ?, last_action = ?, metadata = ?, closed_at = NULL, email_archived = 0,
                      updated_at = ? WHERE id = ?""",
                   (item.get("message_id", ""), kind["description"], kind["notes"][:500],
                    f"{now[:10]}: reopened, a new message after the close", json.dumps(meta), now, entry_id))
        db.commit()
        db.close()
        print(f"  reopened {entry_id}: a new message arrived after its close", file=sys.stderr)
        return True
    except Exception as e:
        print(f"  reopen of {entry_id} failed: {e}", file=sys.stderr)
        return False


def update_ledger_from_analysis(
    ledger: list,
    dropped_threads: list,
    travel_findings: list,
    needs_response: list | None = None,
    scheduling_requests: list | None = None,
) -> list:
    """Add new items to the SQLite ledger, skip duplicates."""
    # Load all tracked message IDs once (any status, any entry type)
    # so we don't re-add emails already tracked by priority/scheduling checks
    all_tracked_msgs = _ledger_mod.get_all_source_message_ids()

    # --- Needs response items ---
    for item in (needs_response or []):
        msg_id = item.get("message_id", "")
        conv_id = item.get("conversation_id", "")
        if not msg_id:
            continue
        # Skip if this message is already tracked under any entry ID
        if msg_id in all_tracked_msgs:
            continue
        # Use last 12 chars of conversation_id (unique suffix; first 12 is shared mailbox GUID)
        key = conv_id if conv_id else msg_id
        kind = mail_row_kind(item)
        entry_id = f"{kind['prefix']}{key[-12:]}"
        if _ledger_mod.entry_exists(entry_id):
            _reopen_mail_row_if_newer(entry_id, item, kind)
            continue
        # Skip if another mail row, a thread- or a sched- entry already
        # covers this conversation
        if any(_ledger_mod.entry_exists(f"{p}{key[-12:]}")
               for p in MAIL_ROW_PREFIXES + ("thread-", "sched-") if p != kind["prefix"]):
            continue
        db = _ledger_mod._get_db()
        now = _ledger_mod._now()
        db.execute("""
            INSERT OR IGNORE INTO ledger
            (id, type, status, description, from_email, source_message_id,
             conversation_id, date_identified, deadline, last_action, notes, metadata,
             created_at, updated_at)
            VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, '', ?, '{}', ?, ?)
        """, (
            entry_id,
            kind["type"],
            kind["description"],
            item.get("from", ""),
            item.get("message_id", ""),
            conv_id,
            now,
            kind["deadline"] or None,
            kind["notes"][:500],
            now, now,
        ))
        db.commit()
        db.close()

    # --- Scheduling requests ---
    for item in (scheduling_requests or []):
        msg_id = item.get("message_id", "")
        conv_id = item.get("conversation_id", "")
        if not msg_id:
            continue
        if msg_id in all_tracked_msgs:
            continue
        key = conv_id if conv_id else msg_id
        entry_id = f"sched-{key[-12:]}"
        if _ledger_mod.entry_exists(entry_id):
            continue
        db = _ledger_mod._get_db()
        now = _ledger_mod._now()
        db.execute("""
            INSERT OR IGNORE INTO ledger
            (id, type, status, description, from_email, source_message_id,
             conversation_id, date_identified, last_action, notes, metadata,
             created_at, updated_at)
            VALUES (?, 'scheduling', 'open', ?, ?, ?, ?, ?, '', ?, '{}', ?, ?)
        """, (
            entry_id,
            f"Scheduling request: {item.get('subject', '(no subject)')}",
            item.get("from", ""),
            item.get("message_id", ""),
            conv_id,
            now,
            item.get("body_preview", "")[:200],
            now, now,
        ))
        db.commit()
        db.close()

    # --- Dropped threads ---
    for thread in dropped_threads:
        msg_id = thread.get("message_id", "")
        if msg_id and msg_id in all_tracked_msgs:
            continue
        key = thread.get("conversation_id", msg_id)
        entry_id = f"thread-{key[-12:]}"
        if _ledger_mod.entry_exists(entry_id):
            continue
        # Skip if a mail row or a sched- entry already covers this conversation
        conv_id = thread.get("conversation_id", "")
        if conv_id and any(_ledger_mod.entry_exists(f"{p}{key[-12:]}")
                           for p in MAIL_ROW_PREFIXES + ("sched-",)):
            continue
        db = _ledger_mod._get_db()
        now = _ledger_mod._now()
        db.execute("""
            INSERT OR IGNORE INTO ledger
            (id, type, status, description, from_email, source_message_id,
             conversation_id, date_identified, last_action, notes, metadata,
             created_at, updated_at)
            VALUES (?, ?, 'open', ?, ?, ?, ?, ?, '', ?, '{}', ?, ?)
        """, (
            entry_id,
            _ledger_mod.canonical_type(thread["category"]),
            f"Dropped thread: {thread['subject']}",
            thread["from"],
            thread["message_id"],
            thread.get("conversation_id", ""),
            now,
            thread["body_preview"][:200],
            now, now,
        ))
        db.commit()
        db.close()

    for finding in travel_findings:
        key = f"travel-{finding['receipt_count']}"
        # Check if any open travel entry exists
        db = _ledger_mod._get_db()
        has_open = db.execute(
            "SELECT 1 FROM ledger WHERE id LIKE 'travel-%' AND status = 'open'"
        ).fetchone()
        if not has_open:
            now = _ledger_mod._now()
            metadata = {"receipt_message_ids": finding["receipt_message_ids"]}
            db.execute("""
                INSERT OR IGNORE INTO ledger
                (id, type, status, description, date_identified, notes,
                 metadata, created_at, updated_at)
                VALUES (?, 'reimbursement', 'open', ?, ?, ?, ?, ?, ?)
            """, (
                key,
                f"Unreimbursed travel: {finding['receipt_count']} receipts found",
                now,
                json.dumps(finding["related_travel_events"]),
                json.dumps(metadata),
                now, now,
            ))
        db.commit()
        db.close()

    # Return current open entries for the briefing
    return _ledger_mod.get_open_entries()


# -------------------------------------------------------------------
# PDF attachment scanning – extract actionable info from PDFs
# -------------------------------------------------------------------
def enrich_with_attachment_hints(email_items: list, label: str = "") -> list:
    """
    For email items that have attachments, scan PDFs for actionable info
    (submission emails, deadlines, required docs, portal URLs).
    Adds an 'attachment_hints' field to items where something is found.

    Only scans PDFs — skips images, Office docs, etc.
    Designed to be cheap: max 3 PDFs per message, max 2MB each.
    """
    scanned = 0
    enriched = 0
    for item in email_items:
        if not item.get("has_attachments", False):
            continue
        msg_id = item.get("message_id")
        if not msg_id:
            continue

        scanned += 1
        hints = pdf_utils.scan_message_pdf_attachments(msg_id, graph)
        if hints:
            item["attachment_hints"] = hints
            enriched += 1

    if scanned > 0:
        print(f"  PDF scan ({label}): {scanned} messages scanned, "
              f"{enriched} had actionable info", file=sys.stderr)
    return email_items


# -------------------------------------------------------------------
# Main briefing assembly
# -------------------------------------------------------------------
# Deliberately narrow: only claims about OUTLOOK drafts pending a send.
# "staged to pending-chat.json" (design notes) must not match.
_STAGED_CLAIM_RE = re.compile(
    r"(?i)await(?:ing)?\s+(?:your\s+)?send\b|sitting\s+unsent|"
    r"unsent\s+in\s+(?:outlook\s+)?drafts|drafts?\s+staged\s+awaiting|"
    r"staged\s+in\s+(?:outlook\s+)?drafts|in\s+outlook\s+drafts\s+awaiting|"
    r"click\s+\*?send\b")
_DRAFT_ID_TOKEN_RE = re.compile(r"([A-Za-z0-9+/_-]{10,}={0,2})(?=[\s).,;\]]|$)")


def reconcile_draft_claims(open_obligations: list, staged_drafts: list) -> int:
    """Annotate open items whose notes claim a staged draft that no
    longer exists in the Drafts folder. Matching: the item's
    conversation_id has no draft, AND no draft id ends with any id-like
    token from the notes (notes usually carry truncated ids). Appends a
    dated auto-note ONCE per item; the note self-corrects for every
    consumer (briefing, packs, App Home)."""
    marker = "[auto-reconciled-drafts"
    draft_convs = {d.get("conversation_id") for d in staged_drafts if d.get("conversation_id")}
    draft_ids = [d.get("id", "") for d in staged_drafts]
    n = 0
    for ob in open_obligations or []:
        text = f"{ob.get('last_action') or ''} {ob.get('notes') or ''}"
        if marker in text or not _STAGED_CLAIM_RE.search(text):
            continue
        if ob.get("conversation_id") and ob["conversation_id"] in draft_convs:
            continue  # a draft really is staged in this thread
        tokens = [t for t in _DRAFT_ID_TOKEN_RE.findall(text) if len(t) >= 10]
        if any(did.endswith(t) for t in tokens for did in draft_ids if did):
            continue  # a referenced draft still exists
        try:
            db = _ledger_mod._get_db()
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            db.execute(
                "UPDATE ledger SET last_action = COALESCE(last_action,'') || ? , "
                "updated_at=? WHERE id=?",
                (f" {marker} {stamp}] the staged draft(s) referenced above "
                 f"are no longer in Drafts - sent or deleted; treat those "
                 f"notes as history, not pending work.",
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 ob["id"]))
            db.commit()
            db.close()
            n += 1
        except Exception:
            continue
    return n


def _mail_gate(msgs: list, label: str, budget_s: float = 60.0):
    """The gate over the mail this run fetched (mail_gate.py). It passes
    everything through unless the mail switch is `enforce`; then the
    detectors' output reaches the ledger, the scheduling queue and the
    lanes' JSON as the remote model's records, FYI and kept mail left out."""
    import mail_gate
    return mail_gate.Gate(msgs, label=label, graph=graph, budget_s=budget_s)


def _obligations_by_list(entries: list) -> dict:
    """The non-closed rows split by list, by STATUS except for waiting:
    waiting is a row someone else owes (ledger.is_waiting, the type),
    whatever its status; follow_up and snoozed are rows parked until a
    date (their status); open is every other row, the work in front of the
    user now, so an open row of type follow_up counts as open. The four
    add up to open_obligations."""
    out = {"open": 0, "follow_up": 0, "snoozed": 0, "waiting": 0}
    for e in entries:
        if _ledger_mod.is_waiting(e):
            out["waiting"] += 1
        elif e.get("status") in ("follow_up", "snoozed"):
            out[e["status"]] += 1
        else:
            out["open"] += 1
    return out

def run_full_daily(force_full: bool = False) -> dict:
    """
    Daily preprocessing with delta tracking.
    Reads the ENTIRE inbox (it IS the task queue), then only surfaces
    NEW items since last run to minimize Claude's token intake.
    Set force_full=True for the first run or after state reset.
    """
    print("Running daily preprocessing (delta mode)...", file=sys.stderr)

    # Run ledger sync first: close any items whose email was archived
    # since last run. This prevents stale items from appearing in the briefing.
    try:
        import ledger_sync
        import argparse as _ap
        _sync_args = _ap.Namespace()
        ledger_sync.cmd_check(_sync_args)
        print("  Ledger sync: reconciled with inbox", file=sys.stderr)
    except Exception as e:
        print(f"  Ledger sync: skipped ({e})", file=sys.stderr)

    # Load delta tracking state
    seen_ids = load_seen_messages()
    seen_convs = load_seen_conversations()
    is_first_run = len(seen_ids) == 0 or force_full

    # Read the ENTIRE inbox — no time filter, no cap.
    # If you have 20 messages, this reads 20. If 150, reads 150.
    inbox = graph.get_entire_inbox()
    print(f"  Inbox: {len(inbox)} messages", file=sys.stderr)

    # Sent mail: go back far enough to cover the oldest inbox message.
    # This ensures we can detect whether we replied to anything in the inbox.
    if inbox:
        oldest = min(
            datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
            for m in inbox
        )
        sent_days_back = (datetime.now(timezone.utc) - oldest).days + 1
        sent_days_back = max(sent_days_back, 14)  # minimum 14 days
    else:
        sent_days_back = 14
    sent = graph.get_sent(days_back=sent_days_back)
    print(f"  Sent: {len(sent)} messages ({sent_days_back}d window)", file=sys.stderr)

    # Archive, for the closure scrubber only. Archiving a conversation in
    # Outlook moves the user's OWN sent copy out of Sent Items, so the
    # `get_sent` above cannot see a reply in an archived thread, and an
    # archived thread is the likeliest to be finished work. Failure is
    # non-fatal: the scrubber treats the archive as an additive source.
    try:
        archive = graph.get_messages("archive", days_back=sent_days_back)
        print(f"  Archive: {len(archive)} messages "
              f"({sent_days_back}d window)", file=sys.stderr)
    except Exception as e:
        archive = []
        print(f"  Archive: unavailable ({e})", file=sys.stderr)

    flagged = graph.get_flagged()
    calendar = graph.get_calendar_view(days_ahead=14)
    working_hours = graph.get_working_hours()

    # Pre-briefing closure scrubber: walks open ledger items and closes
    # any where the user has already replied (Outlook or Gmail) or the
    # latest inbound is an acknowledgment. Runs BEFORE the detect_*
    # functions so the closed conversation_ids reach the loop-closure
    # filter below on this same run.
    try:
        import closure_scrubber
        _gmail_accts = closure_scrubber._load_user_gmail_accounts()
        _scrub = closure_scrubber.scrub_closures(
            outlook_sent=sent,
            outlook_inbox=inbox,
            # Additive, and read by the two conversation-id-free signals
            # only: the user's archived sent copies, and inbound in
            # archived threads.
            outlook_archive=archive,
            # The calendar_event_match signal needs the forward-looking
            # calendar; without it the signal receives [] and never fires.
            # `calendar` is the same 14-day view the briefing uses.
            calendar_events=calendar,
            gmail_accounts=_gmail_accts,
            dry_run=False,
        )
        if _scrub["closed"]:
            print(f"  Closure scrubber: closed {_scrub['closed']} ledger items "
                  f"({_scrub['checked']} checked)", file=sys.stderr)
            for c in _scrub["closures"]:
                print(f"    - {c['id']}: {c['reason']}", file=sys.stderr)
        else:
            print(f"  Closure scrubber: {_scrub['checked']} checked, "
                  f"0 closed", file=sys.stderr)
    except Exception as e:
        print(f"  Closure scrubber: skipped ({e})", file=sys.stderr)

    # The detectors read the raw inbox; what they return goes through the
    # gate. Mail the remote model has not read is not marked seen.
    gate = _mail_gate(inbox, "full-daily", budget_s=120.0)

    # Separate new messages from already-seen
    new_inbox, seen_ids = filter_to_delta(gate.ready(inbox), seen_ids)
    new_sent, seen_ids = filter_to_delta(sent, seen_ids)
    print(f"  New since last run: {len(new_inbox)} inbox, {len(new_sent)} sent", file=sys.stderr)

    # Run analysis on ALL inbox messages (for accuracy) but only surface
    # threads/items involving NEW messages to Claude
    all_dropped = gate.items(suppress_closed_conversations(
        detect_dropped_threads(
            inbox, sent,
            stale_days=PREPROCESSOR_CONFIG.get("stale_thread_days", 3)),
        "dropped_threads"))
    all_needs_resp = gate.items(suppress_closed_conversations(
        detect_needs_response(inbox, sent), "needs_response"))
    all_scheduling = gate.items(detect_scheduling_requests(inbox, sent, gate=gate))
    travel = detect_unreimbursed_travel(inbox, sent, calendar)

    # Filter to delta: only surface items never shown to the user before.
    # Once surfaced, it's in the obligations ledger — no need to repeat.
    # Use --rerun to clear surfaced state and re-surface everything.
    surfaced_responses = load_surfaced_responses()

    if is_first_run:
        # First run or --rerun: surface EVERYTHING.
        dropped_delta = all_dropped
        needs_resp_delta = all_needs_resp
        scheduling_delta = all_scheduling
        print(f"  {'Rerun' if force_full else 'First run'}: surfacing all "
              f"{len(all_dropped)} dropped, "
              f"{len(all_needs_resp)} needs-response, "
              f"{len(all_scheduling)} scheduling", file=sys.stderr)
    else:
        dropped_delta, seen_convs = filter_conversations_delta(
            all_dropped, seen_convs
        )
        # Surface needs-response items that are new OR have a newer message
        # than when we last surfaced them. This catches re-opened threads
        # (e.g., someone replies after we already responded).
        needs_resp_delta = [
            r for r in all_needs_resp
            if r.get("conversation_id") not in surfaced_responses
            or r.get("received", "") > surfaced_responses.get(r.get("conversation_id"), "")
        ]
        # For scheduling: new messages only (scheduling is time-sensitive)
        new_msg_ids = {m["id"] for m in new_inbox}
        scheduling_delta = [
            s for s in all_scheduling if s.get("message_id") in new_msg_ids
        ]
        print(f"  Delta: {len(dropped_delta)} dropped, "
              f"{len(needs_resp_delta)} needs-response, "
              f"{len(scheduling_delta)} scheduling", file=sys.stderr)

    # ── Loop closure: filter out items already handled ────────────────
    # Cross-check delta items against ledger + sent mail to prevent
    # surfacing items the user or bot already acted on.
    closed_convs = _ledger_mod.get_closed_conversation_ids()
    closed_msgs = _ledger_mod.get_closed_source_message_ids()

    pre_filter = len(needs_resp_delta)
    needs_resp_delta = [
        r for r in needs_resp_delta
        if r.get("conversation_id") not in closed_convs
        and r.get("message_id") not in closed_msgs
    ]
    dropped_delta = [
        d for d in dropped_delta
        if d.get("conversation_id") not in closed_convs
        and d.get("message_id") not in closed_msgs
    ]
    scheduling_delta = [
        s for s in scheduling_delta
        if s.get("conversation_id") not in closed_convs
        and s.get("message_id") not in closed_msgs
    ]
    filtered_out = pre_filter - len(needs_resp_delta)
    if filtered_out:
        print(f"  Loop closure: filtered {filtered_out} already-handled items",
              file=sys.stderr)

    # Sent-reply cross-referencing: anything the user answered — in-thread
    # or via a fresh thread to the same person+subject — moves out of the
    # actionable lists and into handled_by_reply (briefed as one line).
    reply_index = build_sent_reply_index(sent)
    needs_resp_delta, handled_nr = split_replied_items(
        needs_resp_delta, reply_index, "needs_response")
    dropped_delta, handled_dr = split_replied_items(
        dropped_delta, reply_index, "dropped_thread")
    handled_by_reply = handled_nr + handled_dr
    if handled_by_reply:
        print(f"  Sent-reply match: moved {len(handled_by_reply)} item(s) "
              f"to handled_by_reply", file=sys.stderr)

    # Mark these conversations as surfaced with their message timestamp
    for r in needs_resp_delta:
        cid = r.get("conversation_id", "")
        if cid:
            surfaced_responses[cid] = r.get("received", "")

    # --- Writing style (from sent mail, zero extra API calls) ---
    writing_style = analyze_writing_style(sent)

    # Fold this batch into the PERSISTENT per-contact style DB: the
    # briefing's writing_style only covers the recent window, while the
    # DB accumulates every contact ever emailed plus few-shot snippets.
    # Drafting lanes query it via `style_profiles.py get --contact <addr>`.
    try:
        import style_profiles
        style_profiles.update_from_messages(sent)
    except Exception as e:
        print(f"  style-profiles update skipped: {e}", file=sys.stderr)

    # --- New detectors (all run on full inbox, zero extra API calls) ---
    group_threads = gate.strip(detect_noisy_group_threads(inbox))
    deadlines = gate.items(detect_deadlines(inbox, sent))
    pending_rsvps = gate.items(detect_pending_rsvps(inbox))
    print(f"  Group threads: {len(group_threads)}, "
          f"Deadlines: {len(deadlines)}, "
          f"RSVPs: {len(pending_rsvps)}", file=sys.stderr)

    # Scan PDF attachments on new items for actionable info
    # (submission addresses, deadlines, required docs, portals)
    enrich_with_attachment_hints(dropped_delta, "dropped")
    enrich_with_attachment_hints(needs_resp_delta, "needs-response")
    enrich_with_attachment_hints(scheduling_delta, "scheduling")

    # Save updated tracking state
    save_seen_messages(seen_ids)
    save_seen_conversations(seen_convs)
    save_surfaced_responses(surfaced_responses)

    # Update ledger with new findings (atomic writes via SQLite)
    update_ledger_from_analysis(
        [], dropped_delta, travel,
        needs_response=needs_resp_delta,
        scheduling_requests=scheduling_delta,
    )

    # Resurrect follow-up items whose date has arrived
    resurrected = _ledger_mod.resurrect_due_follow_ups()

    # Pre-filter stale obligations (>7 days open)
    stale = _ledger_mod.get_stale_entries(7)

    # Wake snoozed emails (move back to inbox if due)
    try:
        import graph_actions as _graph_actions_mod
        woken_snoozed = _graph_actions_mod.wake_snoozed_emails()
    except Exception:
        woken_snoozed = {"woken": 0, "descriptions": [], "remaining": 0}

    # Scheduled sends due today
    try:
        sends_due_today = _scheduled_sends_mod.get_due_today()
    except Exception:
        sends_due_today = []

    # Fetch recent Slack messages from EA channel (last 24h)
    try:
        import chat_read
        slack_messages = chat_read.fetch_recent_messages(hours_back=24)
        slack_summary = chat_read.summarize_for_briefing(slack_messages)
        user_msg_count = slack_summary["user_message_count"]
        print(f"  Slack channel: {slack_summary['message_count']} messages "
              f"({user_msg_count} from user)", file=sys.stderr)
    except Exception as e:
        print(f"  Slack reader: failed ({e}), continuing without", file=sys.stderr)
        slack_summary = {"message_count": 0, "user_message_count": 0,
                         "messages": [], "user_instructions": []}

    # Tag items already being discussed in the EA channel (48h window)
    try:
        slack_48h = chat_read.fetch_recent_messages(hours_back=48) \
            if slack_summary.get("message_count") else slack_summary.get("messages", [])
    except Exception:
        slack_48h = slack_summary.get("messages", [])
    n_tagged = annotate_slack_mentions(
        needs_resp_delta + dropped_delta, slack_48h)
    if n_tagged:
        print(f"  Slack reconciliation: tagged {n_tagged} item(s) already "
              f"in play in Slack", file=sys.stderr)

    # Fetch recent Teams DMs (the owner plugin that reads the Teams chat
    # service; none when it is not installed)
    teams_summary = {"message_count": 0, "chats": []}
    try:
        teams_dms = get_teams_dms(days_back=1)
        if teams_dms:
            total_msgs = sum(len(c.get("messages", [])) for c in teams_dms)
            teams_summary = {
                "message_count": total_msgs,
                "chat_count": len(teams_dms),
                "chats": teams_dms[:15],
            }
            print(f"  Teams DMs: {len(teams_dms)} chats, {total_msgs} messages",
                  file=sys.stderr)
        else:
            print("  Teams DMs: none (no plugin, no token or no recent messages)",
                  file=sys.stderr)
    except Exception as e:
        print(f"  Teams DMs: failed ({e}), continuing without", file=sys.stderr)

    # Compute availability for scheduling requests
    availability = compute_merged_availability(calendar)

    open_obligations = _ledger_mod.get_open_entries()

    # De-duplicate open_obligations: if the same conversation_id has both
    # a "thread-" and a "response-" entry, keep only one (prefer response-).
    # This prevents duplicate items from cluttering the briefing.
    _seen_convs_ob = {}
    _deduped_obligations = []
    for ob in open_obligations:
        cid = ob.get("conversation_id", "")
        if not cid:
            _deduped_obligations.append(ob)
            continue
        existing = _seen_convs_ob.get(cid)
        if existing is None:
            _seen_convs_ob[cid] = len(_deduped_obligations)
            _deduped_obligations.append(ob)
        else:
            # Keep the more specific entry (response > thread > sched)
            existing_id = _deduped_obligations[existing]["id"]
            if existing_id.startswith("thread-") and not ob["id"].startswith("thread-"):
                _deduped_obligations[existing] = ob
            # Otherwise keep the one already there
    if len(open_obligations) != len(_deduped_obligations):
        print(f"  Dedup: {len(open_obligations)} → {len(_deduped_obligations)} "
              f"open obligations (removed {len(open_obligations) - len(_deduped_obligations)} dupes)",
              file=sys.stderr)
    open_obligations = _deduped_obligations
    _mark_parked(open_obligations)

    # Count totals for the summary
    delta_item_count = (
        len(dropped_delta) + len(needs_resp_delta) + len(scheduling_delta)
    )

    # ── Draft-claim reconciliation ───────────────────────────────────
    # Ledger narrative rots: "two drafts staged awaiting Send" stays in
    # last_action after the user sends them, and the briefing then
    # instructs clicking Send on ghosts. Ground truth is the Drafts
    # folder: fetch it, publish it as staged_drafts (the ONLY permissible
    # source for send instructions), and auto-annotate open items whose
    # staged claims no longer match a living draft.
    staged_drafts = []
    try:
        _draft_msgs = graph.get_messages("drafts", days_back=90)
        staged_drafts = [{
            "subject": m.get("subject", ""),
            "created": m.get("receivedDateTime", ""),
            "conversation_id": m.get("conversationId", ""),
            "id": m.get("id", ""),
        } for m in _draft_msgs]
        reconciled = reconcile_draft_claims(open_obligations, staged_drafts)
        if reconciled:
            print(f"  Draft-claim reconcile: annotated {reconciled} item(s) "
                  f"whose staged drafts are gone (sent/deleted)",
                  file=sys.stderr)
            open_obligations = _ledger_mod.get_open_entries()
            _mark_parked(open_obligations)
    except Exception as e:
        print(f"  Draft-claim reconcile skipped: {e}", file=sys.stderr)

    # Entity-level auto-merge: fold sibling items about one underlying
    # matter (shared ticket/manuscript/award token) into a view-level
    # aggregation. Never silent: new merges are announced in the
    # briefing and badged in App Home, and `project_store.py unmerge`
    # reverses one.
    try:
        import project_store as _ps_mod
        auto_merged = _ps_mod.auto_merge_scan()
        if auto_merged:
            print(f"  Auto-merge: {len(auto_merged)} new group(s): "
                  + ", ".join(m["entity"] for m in auto_merged),
                  file=sys.stderr)
    except Exception as e:
        print(f"  Auto-merge scan skipped: {e}", file=sys.stderr)
        auto_merged = []

    # Deterministic priority queue: ranking happens HERE, in code, so the
    # briefing leads with what needs doing instead of enumerating.
    today_top, everything_else = build_today_top(
        needs_resp_delta, dropped_delta, deadlines, pending_rsvps,
        open_obligations, load_vip_senders())
    if today_top:
        print(f"  Priority queue: {len(today_top)} today-top item(s), "
              f"{len(everything_else)} in the digest", file=sys.stderr)

    briefing = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "full-daily-delta",
        "is_first_run": is_first_run,
        "mail_gate": gate.summary(),
        "delta_summary": {
            "new_inbox_messages": len(new_inbox),
            "new_sent_messages": len(new_sent),
            "total_delta_items": delta_item_count,
            "total_inbox": len(inbox),
            "flagged_count": len(flagged),
            "events_next_14d": len(calendar),
            "open_obligations": len(open_obligations),
            # ea-config.json "obligation_split": {"enabled": true}; off, the
            # footer keeps its one total.
            **({"obligations_by_list": _obligations_by_list(open_obligations)}
               if _ea_config.obligation_split_enabled() else {}),
        },
        # Ranked queue, computed in code. The briefing LEADS with today_top
        # (each entry carries its reason); everything_else is digest-only.
        "today_top": today_top,
        "everything_else": everything_else[:40],
        # Items the user already answered by email — briefed as one line
        # ("also handled by your replies: ..."), never as open asks.
        "handled_by_reply": handled_by_reply[:15],
        # The Drafts folder, verbatim. THE only source of truth for any
        # "click Send" instruction — ledger notes about drafts are
        # narrative that rots the moment the user sends.
        "staged_drafts": staged_drafts[:20],
        # NEW entity-level aggregations this run (a shared ticket or
        # manuscript id folded sibling items together). Announce each in
        # one line so a false merge is caught by eyeballs, not silence.
        "auto_merged": auto_merged[:10],
        # Items surfaced to user for the first time (or re-surfaced on --rerun)
        "new_dropped_threads": dropped_delta[:15],
        "new_needs_response": needs_resp_delta[:50],
        "new_scheduling_requests": scheduling_delta,
        # True when a dedicated #ai-scheduling-assistant channel is configured:
        # the proactive loop opens scheduling requests as conversations there, so
        # briefings should condense them to a one-line handoff, not full detail.
        "scheduling_channel_active": sched_channel_configured(),
        "travel_reimbursement": travel,
        # Availability is always current (cheap, just time slots)
        "availability_merged": availability[:15],
        # Non-empty when a calendar source failed to load: availability
        # above is then INCOMPLETE — skills must warn, not schedule.
        "calendar_fetch_errors": LAST_CALENDAR_FETCH_ERRORS,
        # Ledger is the persistent brain – Claude reads this
        # instead of re-reading old emails
        "open_obligations": open_obligations[:30],
        # Closed in the last 7 days. DO-NOT-SURFACE list: anything here is
        # settled — never brief it as open/pending, regardless of what
        # prior briefings or emails suggest.
        "recently_closed": _ledger_mod.get_recently_closed(days=7)[:20],
        # Open rows that look done, waiting on the user's one-click confirm
        # (the bot posts the card). Never brief them as open asks, and never
        # as closed: one "Looks done?" line, each with its why.
        "close_proposals": _close_proposals(open_obligations),
        # The first-week question of the day, or None. The morning briefing
        # ends with it; the afternoon catchup ignores it.
        "onboarding_question": _onboarding_question(),
        "working_hours": working_hours,
        # Today's schedule is always included (small, essential). The day
        # is the user's: an evening event is UTC's next day.
        "todays_events": [
            e for e in calendar
            if _event_local_day(e) == _user_today()
        ],
        "tomorrows_events": [
            e for e in calendar
            if _event_local_day(e) == _user_today(1)
        ],
        # --- New skill data (zero extra API calls) ---
        "noisy_group_threads": group_threads[:10],
        "deadline_items": deadlines[:15],
        "pending_rsvps": pending_rsvps[:10],
        # Writing style profile (from sent mail analysis)
        "writing_style": writing_style,
        # --- Automated bookkeeping (no LLM tool calls needed) ---
        "resurrected_follow_ups": resurrected[:10],
        "stale_obligations": stale[:15],
        "woken_snoozed": woken_snoozed.get("descriptions", []),
        "scheduled_sends_due_today": sends_due_today,
        # Recent Slack messages from user's EA channel (last 24h)
        "recent_slack_messages": slack_summary,
        # Recent Teams DMs (last 24h, from the teams-dms owner plugin)
        "recent_teams_dms": teams_summary,
    }

    return briefing


def run_scheduling_check() -> dict:
    """Lightweight check for new scheduling requests.

    Only fetches the last 2 days of inbox (not the entire inbox).
    Scheduling requests older than that are stale and already covered
    by the morning briefing's full-daily run.

    Dedup: skips emails already tracked in the ledger (any status).
    New items are auto-added to the ledger on first detection.
    """
    print("Running scheduling check...", file=sys.stderr)

    inbox = graph.get_messages("inbox", days_back=2)
    sent = graph.get_sent(days_back=7)
    calendar = graph.get_calendar_view(days_ahead=14)
    working_hours = graph.get_working_hours()

    gate = _mail_gate(inbox, "scheduling-check")
    scheduling = gate.items(detect_scheduling_requests(inbox, sent, gate=gate))
    new_scheduling = register_new_scheduling_requests(scheduling)
    write_proactive_queue(new_scheduling)

    availability = compute_merged_availability(calendar)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "scheduling-check",
        "mail_gate": gate.summary(),
        "scheduling_requests": new_scheduling,
        "availability_merged": availability[:20],
        "calendar_fetch_errors": LAST_CALENDAR_FETCH_ERRORS,
        "working_hours": working_hours,
    }


def run_intake_check() -> dict:
    """Intraday capture of ORDINARY needs-response emails into the ledger.

    Without it the 4 AM full-daily is the only path that turns regular
    mail into response-* items, so a Tuesday-2pm email does not exist on
    the desk, App Home, or the merge scan until the next morning. This
    mode runs the same detection, suppression and sent-reply
    cross-referencing on a 2-day window and feeds
    update_ledger_from_analysis, whose dedupe (tracked message ids,
    existing response-/thread-/sched- entries) makes it idempotent and
    safe alongside the 4 AM run.

    SILENT by design: no Slack post, no per-email notification. Items
    appear on the desk, App Home and tray. Priority alerts keep their
    loud path; merge announcements stay with the bot's 15-minute
    heartbeat, which scans AFTER this creates items.
    """
    print("Running intake check...", file=sys.stderr)

    inbox = graph.get_messages("inbox", days_back=2)
    sent = graph.get_sent(days_back=3)

    gate = _mail_gate(inbox, "intake-check")
    needs = gate.items(suppress_closed_conversations(
        detect_needs_response(inbox, sent), "intake"))
    reply_index = build_sent_reply_index(sent)
    needs, handled = split_replied_items(needs, reply_index, "needs_response")

    before = set(_ledger_mod.get_all_source_message_ids())
    update_ledger_from_analysis([], [], [], needs_response=needs,
                                scheduling_requests=None)
    after = set(_ledger_mod.get_all_source_message_ids())
    created = len(after - before)
    if created:
        print(f"  Intake: {created} new response item(s) captured",
              file=sys.stderr)

    # Scheduling and reschedule requests ride the same ten-minute cadence
    # into the bot's proactive queue, so a "can we move Tuesday?" reaches
    # the scheduling channel within minutes instead of at the next hourly
    # check. Same detector, same ledger dedupe, same queue merge.
    new_scheduling = register_new_scheduling_requests(
        gate.items(detect_scheduling_requests(inbox, sent, gate=gate)))
    queued = write_proactive_queue(new_scheduling)
    if new_scheduling:
        print(f"  Intake: {len(new_scheduling)} scheduling request(s) queued "
              f"({sum(1 for s in new_scheduling if s.get('kind') == 'reschedule')} "
              f"reschedule)", file=sys.stderr)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "intake-check",
        "mail_gate": gate.summary(),
        "created": created,
        "already_handled_by_reply": len(handled),
        "scheduling_requests": new_scheduling,
        "proactive_queue_len": queued,
    }


def run_priority_check() -> dict:
    """
    Lightweight, high-frequency check for urgent emails.
    Fetches the last day of inbox messages (read and unread) and filters
    to the last 4 hours via detect_priority_emails. This catches emails
    the user opened on mobile but hasn't acted on yet.

    Dedup: skips emails already tracked in the ledger (any status).
    New items are auto-added to the ledger on first detection.
    """
    print("Running priority check...", file=sys.stderr)

    vip_senders = load_vip_senders()
    # Last day of messages (read + unread); the 4-hour filter is in detect_priority_emails
    recent = graph.get_messages("inbox", days_back=1)
    sent = graph.get_sent(days_back=2)

    gate = _mail_gate(recent, "priority-check")
    priority = gate.items(detect_priority_emails(recent, vip_senders, sent=sent, hours_back=4))

    # Filter out emails already tracked in the ledger
    known_ids = _ledger_mod.get_all_source_message_ids()
    new_priority = [p for p in priority if p["message_id"] not in known_ids]

    # Auto-add new items to ledger so they won't re-notify
    for p in new_priority:
        flags = []
        if p.get("is_vip"): flags.append("VIP")
        if p.get("is_urgent"): flags.append("urgent")
        if p.get("is_flagged"): flags.append("flagged")
        _ledger_mod.add_entry(
            entry_id=f"priority-{p['message_id'][-12:]}",
            entry_type="response_needed",
            description=f"Priority email: {p['subject'][:80]}",
            from_email=p.get("from", ""),
            from_name=p.get("from_name", ""),
            source_message_id=p["message_id"],
            notes=f"Flags: {', '.join(flags)}. {p.get('body_preview', '')[:250]}",
            priority="high" if p.get("is_urgent") else None,
        )

    # Also check for recent Teams DMs (lightweight, optional)
    teams_dms = []
    try:
        raw_dms = get_teams_dms(days_back=1)
        # Filter to DMs with messages in last 4 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
        for chat in raw_dms:
            recent_msgs = [
                m for m in chat.get("messages", [])
                if m.get("created") and
                datetime.fromisoformat(m["created"].replace("Z", "+00:00")) >= cutoff
            ]
            if recent_msgs:
                teams_dms.append({
                    "display_name": chat.get("display_name", "?"),
                    "email": chat.get("email", ""),
                    "recent_messages": recent_msgs[:3],
                })
    except Exception:
        pass  # Teams is optional

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "priority-check",
        "mail_gate": gate.summary(),
        "priority_emails": new_priority,
        "teams_dms": teams_dms,
    }


def _project_hygiene(days: int = 30) -> dict:
    try:
        import project_store
        return {"days": days, "candidates": project_store.dormancy_candidates(days=days)}
    except Exception as e:
        print(f"  project hygiene skipped: {e}", file=sys.stderr)
        return {"days": days, "candidates": [], "error": str(e)[:120]}


def run_full_weekly() -> dict:
    """
    Deep weekly audit – 14-day lookback, all analyses.
    This is the ONE run that does a full scan. It also resets delta tracking
    so the following week starts clean.
    """
    print("Running full weekly preprocessing...", file=sys.stderr)

    # Read ENTIRE inbox — same as daily, the inbox is the task queue
    inbox = graph.get_entire_inbox()
    print(f"  Inbox: {len(inbox)} messages", file=sys.stderr)

    # Sent window covers the oldest inbox message
    if inbox:
        oldest = min(
            datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
            for m in inbox
        )
        sent_days_back = (datetime.now(timezone.utc) - oldest).days + 1
        sent_days_back = max(sent_days_back, 14)
    else:
        sent_days_back = 14
    sent = graph.get_sent(days_back=sent_days_back)
    print(f"  Sent: {len(sent)} messages ({sent_days_back}d window)", file=sys.stderr)

    flagged = graph.get_flagged()
    calendar = graph.get_calendar_view(days_ahead=21)
    past_calendar = graph.get_calendar_view(
        start=datetime.now(timezone.utc) - timedelta(days=60),
        end=datetime.now(timezone.utc),
    )
    working_hours = graph.get_working_hours()
    todo_lists = graph.get_todo_lists()

    gate = _mail_gate(inbox, "full-weekly", budget_s=120.0)
    dropped = gate.items(suppress_closed_conversations(
        detect_dropped_threads(
            inbox, sent,
            stale_days=PREPROCESSOR_CONFIG.get(
                "stale_thread_days_weekly_audit", 2)),
        "dropped_threads"))
    scheduling = gate.items(detect_scheduling_requests(inbox, sent, gate=gate))
    needs_resp = gate.items(suppress_closed_conversations(
        detect_needs_response(inbox, sent), "needs_response"))
    travel = detect_unreimbursed_travel(inbox, sent, past_calendar)
    group_threads = gate.strip(detect_noisy_group_threads(inbox))
    deadlines = gate.items(detect_deadlines(inbox, sent))
    pending_rsvps = gate.items(detect_pending_rsvps(inbox))

    # Update ledger with new findings (atomic writes via SQLite)
    update_ledger_from_analysis([], dropped, travel)

    open_obligations = _ledger_mod.get_open_entries()
    availability = compute_merged_availability(calendar)

    # RESET delta tracking for the new week
    # This means Monday morning's run will be a "first run" with full context
    save_seen_messages(set())
    save_seen_conversations({})

    # For the weekly audit, we send the FULL dropped threads and obligations
    # but we keep it targeted: only the top items by severity, with
    # enough context for Claude (Opus) to do pattern analysis
    briefing = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "full-weekly",
        "mail_gate": gate.summary(),
        "summary": {
            "inbox_total": len(inbox),
            "sent_window_days": sent_days_back,
            "sent_count": len(sent),
            "flagged_count": len(flagged),
            "events_next_21d": len(calendar),
            "open_obligations": len(open_obligations),
            "archived_this_week": sum(1 for o in open_obligations if o.get("email_archived")),
            "todo_lists": len(todo_lists),
        },
        # Weekly gets full list, but capped to keep tokens reasonable
        "dropped_threads": dropped[:25],
        "needs_response": needs_resp[:15],
        "scheduling_requests": scheduling,
        "travel_reimbursement": travel,
        "availability_merged": availability[:20],
        "calendar_fetch_errors": LAST_CALENDAR_FETCH_ERRORS,
        "open_obligations": open_obligations,
        "recently_closed": _ledger_mod.get_recently_closed(days=7)[:20],
        "close_proposals": _close_proposals(open_obligations),
        # The PARA pass: projects (horizon project) with no open items and
        # no activity for 30 days, for the audit to propose as dormant.
        "project_hygiene": _project_hygiene(),
        "working_hours": working_hours,
        "todo_lists": [
            {"id": l["id"], "name": l["displayName"]} for l in todo_lists
        ],
        # --- New skill data ---
        "noisy_group_threads": group_threads[:15],
        "deadline_items": deadlines[:20],
        "pending_rsvps": pending_rsvps[:15],
    }

    return briefing


def run_list_digest() -> dict:
    """Mailing-list digest triage input for skills/list-digest.md: the
    digests found in the inbox, split into items, job posts already
    dropped. The logic lives in list_digest.py; it is registered here so
    run-agent.sh keeps a single preprocess entry point per agent."""
    import list_digest
    return list_digest.collect()


def run_admissions_digest() -> dict:
    """The weekly admissions-inquiry digest input for
    skills/admissions-digest.md: the last week of the admissions-inquiries
    folder, grouped by sender. The logic lives in admissions_filter.py."""
    import admissions_filter
    if not admissions_filter.enabled():
        return {"mode": "admissions-digest", "digests": [], "disabled": True}
    return admissions_filter.collect()


# Modes that read the inbox for triage. Each first runs the admissions
# filter, so an applicant's cold email is in its folder before any
# detector can turn it into a needs-response row.
ADMISSIONS_SWEEP_MODES = ("full-daily", "scheduling-check", "priority-check",
                          "intake-check", "full-weekly")

MODES = {
    "full-daily": run_full_daily,
    "scheduling-check": run_scheduling_check,
    "priority-check": run_priority_check,
    "intake-check": run_intake_check,
    "full-weekly": run_full_weekly,
    "list-digest": run_list_digest,
    "admissions-digest": run_admissions_digest,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EA Preprocessor")
    parser.add_argument(
        "--mode",
        choices=MODES.keys(),
        required=True,
        help="Processing mode",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file (default: stdout)",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Clear surfaced state and re-surface all items (use after detection improvements)",
    )
    args = parser.parse_args()

    if args.rerun:
        # Clear surfaced-responses so all items are treated as new
        save_surfaced_responses({})
        print("Rerun: cleared surfaced-responses state", file=sys.stderr)

    import requests as _req

    if args.mode in ADMISSIONS_SWEEP_MODES:
        import admissions_filter
        admissions_filter.preprocess_hook()

    for _attempt in range(1, 3):
        try:
            if args.mode in ("full-daily",):
                result = MODES[args.mode](force_full=args.rerun)
            else:
                result = MODES[args.mode]()
            break
        except (_req.exceptions.ConnectionError, _req.exceptions.Timeout) as e:
            if _attempt == 2:
                raise
            print(f"Preprocessor failed (attempt {_attempt}/2): {e}",
                  file=sys.stderr)
            print("Retrying in 10s...", file=sys.stderr)
            time.sleep(10)

    output = json.dumps(result, indent=2)
    if args.output:
        # The output sits in the resident's state dir: a random temp name
        # and a replace, so a link at the name is replaced, never followed.
        _atomic.write_text(args.output, output, mode=0o664)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)
