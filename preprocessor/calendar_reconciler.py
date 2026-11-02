#!/usr/bin/env python3
"""Mirror every calendar commitment onto the EA-Managed calendar.

EA-Managed is the one view of the user's time with a priority on every
entry. The scheduling solver treats EA-Managed p0 tokens as hard blocks in
both the normal and relaxed passes, so a p0 mirror protects a slot no
matter what the movability heuristic thinks of the underlying event. This
pass keeps those mirrors in sync with reality:

  - Every timed event shown as busy or tentative on Exchange or a blocking
    Google calendar gets an "[auto]" mirror whose token word is its
    priority (_level_and_rule): a series gets one recurring mirror, a
    one-off meeting (a 1:1, a defense, a visit) gets a single one, and a
    series whose repeat pattern has no RRULE form is mirrored occurrence
    by occurrence. Exchange is scanned first: it is where meetings get
    created, so it anchors the mirror when the same commitment exists on
    several calendars.
  - A one-off mirror stays after its meeting ends; only a meeting that is
    cancelled or moved while still ahead changes its mirror, and those
    routine changes post nothing. A one-off already covered by a
    hand-placed token of the same or higher priority gets no mirror.
  - A level written on the source event (source_label: "p0 ..." or
    "[note]") is the mirror's level and relevels it when it changes.
    The solver reads the mirror's level in place of the raw event
    (scheduling_engine._mirror_level).
  - The mirror is anchored to its source series id. Source moved, mirror
    moves. Source gone, mirror goes. A single cancelled source instance
    (a lecture skipped for a holiday) cancels the matching mirror
    instance, so the p0 releases that occurrence instead of freezing it.
  - The user deleting a mirror is a decision, detected via showDeleted
    tombstones and recorded in scheduling-decisions.json: the event comes
    back as a [note] mirror, which blocks nothing, and deleting that note
    too takes it off the calendar for good.
    The reconciler stamps its OWN removals into the description first, so
    its housekeeping is never re-read as the user's decision.
  - Events without the [auto] marker are never touched. Hand-placed
    tokens belong to the user.
  - A source fetch failure freezes destructive actions for that source's
    mirrors, per account AND per series. An outage must never read as
    "everything was cancelled."

The EA-Managed calendar IS the rule store: every protection is a visible,
draggable event whose description names the rule that created it. There
is no hidden list. Runs on cron as ea-bot; Outlook is read through
graph_actions.py subprocesses (graph_client is unreadable to ea-bot under
mail mediation, by design) and Google through the calendar client.

Default is dry-run. Nothing is written without --apply.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import google_calendar_client as gcal  # noqa: E402
import atomic_io as _atomic  # noqa: E402
import owner_trust as _owner_trust  # noqa: E402
import resident_io as _rio  # noqa: E402
import user_tz as _user_tz  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
# Shared with scheduling_engine (log-decision) and read by the resident, so it
# stays in resident space and is rewritten with the resident's access.
DECISIONS_FILE = EA_DIR / "state" / "scheduling-decisions.json"


def _owner_state_dir() -> Path:
    """The owner-private directory in an owner process (this cron), the EA
    tree's state directory in any other (a test on a scratch tree)."""
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


# Owner-only dedupe state: no resident process reads it.
GAP_WARNED_FILE = _owner_state_dir() / "reconciler-gap-warned.json"
SCRIPT_DIR = Path(__file__).resolve().parent
USER_TZ_NAME = _user_tz.name(EA_DIR)
USER_TZ = ZoneInfo(USER_TZ_NAME)

AUTO_MARKER = "[auto]"
RULE_RECURRING_HOLD = "recurring-zero-attendee"
RULE_RECURRING_1ON1 = "recurring-1on1"
RULE_RECURRING_GROUP = "recurring-group"
HORIZON_DAYS = 60
OUTLOOK_ACCOUNT = "outlook"


RULE_LABELLED = "labelled-at-source"
RULE_RELEASED = "released-to-note"
RULE_ONE_OFF_HOLD = "one-off-zero-attendee"
RULE_ONE_OFF_1ON1 = "one-off-1on1"
RULE_ONE_OFF_GROUP = "one-off-group"
RULE_UNANSWERED = "unanswered-invite"
RULE_EXTERNAL_1ON1 = "external-1on1"


def _level_and_rule(attendee_count: int, one_off: bool = False,
                    answered: bool = True, external: bool = False) -> tuple:
    """The mirror's token level IS the movability declaration.

    An invite the user has not accepted (unanswered, or tentative) is p2:
    on the calendar, not yet a commitment. Zero other people is a personal
    commitment (a lecture, a visit): p0, never offered away. Exactly one is
    a 1:1: p1, offerable in relaxation but ranked down, unless that person
    is outside the user's domain, where a moved meeting takes weeks to land
    (the solver never offers those away either): p0. Two or more is a group
    meeting: p0. Attendee counts from the APIs are noisy, so these are
    DEFAULTS: the user edits the token word on the mirror and the
    reconciler preserves the edit, managing times only.
    """
    if not answered:
        return "p2", RULE_UNANSWERED
    if attendee_count == 1:
        if external:
            return "p0", RULE_EXTERNAL_1ON1
        return "p1", (RULE_ONE_OFF_1ON1 if one_off else RULE_RECURRING_1ON1)
    if attendee_count == 0:
        return "p0", (RULE_ONE_OFF_HOLD if one_off else RULE_RECURRING_HOLD)
    return "p0", (RULE_ONE_OFF_GROUP if one_off else RULE_RECURRING_GROUP)


# A level the user wrote on the source event itself. nm reads as p0; wh and
# wr are scheduling rules, not commitments, and are never mirrored.
_LABELS = {"p0": "p0", "p1": "p1", "p2": "p2", "nm": "p0"}
_NOTE_RE = re.compile(r"\s*\[note\]\s*", re.I)


def source_label(title: str) -> tuple:
    """(level, title without the label) for a source event the user
    labelled: a leading p0/p1/p2/nm, or [note] anywhere. ("", title) when
    unlabelled. The label outranks every default and every source setting,
    and a label added later relabels the mirror."""
    t = (title or "").strip()
    if "[note]" in t.lower():
        return "note", _NOTE_RE.sub(" ", t).strip() or t
    head, _, rest = t.partition(" ")
    level = _LABELS.get(head.lower())
    if level and rest.strip():
        return level, rest.strip()
    return "", t


def _apply_labels(protected: dict) -> dict:
    for info in protected.values():
        level, title = source_label(info.get("summary", ""))
        if level:
            info.update(level=level, rule=RULE_LABELLED, summary=title,
                        labeled=True)
    return protected


def _is_rule_title(summary: str) -> bool:
    """A wh or wr scheduling rule: never a commitment to mirror."""
    head = (summary or "").strip().split(None, 1)
    return bool(head) and head[0].lower() in ("wh", "wr")


# A title that names a 1:1: "1:1", "1-on-1", "one on one", "A <> B", or
# two names around a slash ("Sam / Pat", "Pat Smith / Albert
# Kettner: CLaSH"). People with two addresses and room mailboxes make the
# attendee count read high, so the title also counts.
_ONE_ON_ONE_RE = re.compile(
    r"\b1\s*(?::|-?on-?)\s*1\b|\bone[- ]on[- ]one\b|<>", re.I)


def _title_says_1on1(title: str) -> bool:
    t = (title or "").split(":", 1)[0]
    if _ONE_ON_ONE_RE.search(title or ""):
        return True
    parts = [x.strip() for x in t.split("/")]
    return len(parts) == 2 and all(parts)


def _outlook_level(item: dict, one_off: bool) -> tuple:
    """Level and rule for a calendar-view item. `others` (people besides
    the user) comes from calendar-view; an older mediator's output has
    only the raw attendee count, which is used as it is."""
    others = int(item.get("others", item.get("attendees") or 0) or 0)
    if others > 1 and _title_says_1on1(item.get("subject", "")):
        others = 1
    answered = not ((item.get("show_as") or "").lower() == "tentative"
                    or (item.get("my_response") or "") in
                    ("notResponded", "tentativelyAccepted"))
    return _level_and_rule(others, one_off=one_off, answered=answered,
                           external=int(item.get("others_external") or 0) > 0)


def _google_level(item: dict, one_off: bool, domain: str = "") -> tuple:
    """Level and rule for a raw Google event: the attendees other than the
    user (rooms left out), and the user's own response."""
    others = [a for a in item.get("attendees") or []
              if not a.get("self") and not a.get("resource")]
    count = len(others)
    if count > 1 and _title_says_1on1(item.get("summary", "")):
        count = 1
    me = next((a for a in item.get("attendees") or [] if a.get("self")), {})
    answered = me.get("responseStatus") not in ("needsAction", "tentative")
    external = bool(domain) and any(
        not (a.get("email") or "").lower().endswith(domain) for a in others)
    return _level_and_rule(count, one_off=one_off, answered=answered,
                           external=external)


class MasterFetchError(Exception):
    """A series master could not be READ this run.

    Distinct from a get_master callback returning None, which means the
    master is genuinely gone. A key that raises this is FROZEN: no
    destructive action for it this pass, because an outage must not read as
    "this commitment was cancelled".
    """


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------

def _to_local(dt_str: str, naive_is_utc: bool = False) -> str:
    """ISO datetime -> naive local ISO in the user's zone (USER_TZ).

    Graph returns naive UTC strings (and 7-digit fractional seconds, which
    fromisoformat rejects); Google returns offset-aware strings. Callers
    say which convention a naive string follows.
    """
    s = dt_str
    if "." in s:
        head, frac = s.split(".", 1)
        tail = ""
        for i, ch in enumerate(frac):
            if not ch.isdigit():
                tail = frac[i:]
                break
        s = head + tail
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        if not naive_is_utc:
            return dt.isoformat(timespec="seconds")
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ).replace(tzinfo=None).isoformat(
        timespec="seconds")


def _graph_endpoint_to_local(endpoint: dict) -> str:
    """Graph {"dateTime", "timeZone"} -> naive local ISO."""
    tz_name = (endpoint.get("timeZone") or "UTC").strip()
    raw = endpoint.get("dateTime", "")
    if tz_name.upper() == "UTC":
        return _to_local(raw, naive_is_utc=True)
    try:
        tz = ZoneInfo(tz_name)
        dt = datetime.fromisoformat(raw.split(".")[0]).replace(tzinfo=tz)
        return dt.astimezone(USER_TZ).replace(tzinfo=None).isoformat(
            timespec="seconds")
    except Exception:
        return _to_local(raw, naive_is_utc=True)


# ---------------------------------------------------------------------------
# Outlook recurrence -> RRULE
# ---------------------------------------------------------------------------

_GRAPH_DAY_TO_BYDAY = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
    "friday": "FR", "saturday": "SA", "sunday": "SU",
}


def outlook_recurrence_to_rrule(recurrence: dict) -> str:
    """Translate a Graph recurrence object into one RRULE string.

    Weekly and daily patterns cover the standing-hold shapes this pass
    protects. Anything fancier returns "" and the series is reported as
    skipped instead of half-protected.
    """
    pattern = (recurrence or {}).get("pattern") or {}
    rng = (recurrence or {}).get("range") or {}
    ptype = (pattern.get("type") or "").lower()
    if ptype not in ("weekly", "daily"):
        return ""
    parts = [f"FREQ={ptype.upper()}"]
    interval = int(pattern.get("interval") or 1)
    if interval > 1:
        parts.append(f"INTERVAL={interval}")
    if ptype == "weekly":
        days = [_GRAPH_DAY_TO_BYDAY.get((d or "").lower())
                for d in pattern.get("daysOfWeek") or []]
        days = sorted(d for d in days if d)
        if not days:
            return ""
        parts.append("BYDAY=" + ",".join(days))
    rtype = (rng.get("type") or "").lower()
    if rtype == "enddate" and rng.get("endDate"):
        # Inclusive local end date -> UTC UNTIL, end of day in the range's
        # timezone (matching how Google writes its own UNTILs).
        tz_name = rng.get("recurrenceTimeZone") or USER_TZ_NAME
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = USER_TZ
        end_local = datetime.fromisoformat(rng["endDate"] + "T23:59:59")
        until = end_local.replace(tzinfo=tz).astimezone(timezone.utc)
        parts.append("UNTIL=" + until.strftime("%Y%m%dT%H%M%SZ"))
    elif rtype == "numbered" and rng.get("numberOfOccurrences"):
        parts.append(f"COUNT={int(rng['numberOfOccurrences'])}")
    return "RRULE:" + ";".join(parts)


def _rrule_signature(recurrence: list) -> frozenset:
    """Order-insensitive semantic identity for an RRULE list.

    Google rewrites stored rules (param order changes, WKST may appear),
    so raw string comparison against what we wrote plans a retime on
    every run, forever. Compare meaning: parameter set with BYDAY codes
    sorted and WKST ignored.
    """
    parts = set()
    for r in recurrence or []:
        body = r.split(":", 1)[1] if ":" in r else r
        for p in body.split(";"):
            if not p or p.upper().startswith("WKST="):
                continue
            k, _, v = p.partition("=")
            k = k.upper()
            if k == "BYDAY":
                v = ",".join(sorted(x.strip().upper()
                                    for x in v.split(",") if x.strip()))
            parts.add((k, v.upper()))
    return frozenset(parts)


def _occurrences(info: dict) -> list:
    return info.get("instances") or [(info["start_local"],
                                      info["end_local"])]


# ---------------------------------------------------------------------------
# Protected-set computation (rule R1)
# ---------------------------------------------------------------------------

def source_key(account: str, series_id: str) -> str:
    return f"{account}/{series_id}"


def qualifies_as_protected(item: dict) -> bool:
    """Standing-commitment test on a raw Google API event instance.

    Timed, shown as busy, and not a wh/wr rule. Every
    recurring commitment is reflected; the attendee count decides the
    mirror's default token level, never whether a mirror exists.
    Non-recurring events pass this test too; whether they are mirrored
    is the source's one_offs setting (subscribed feeds publish meetings
    as individual events).
    """
    if item.get("status") == "cancelled":
        return False
    if "dateTime" not in item.get("start", {}):
        return False  # all-day events have their own judgment path
    if item.get("transparency") == "transparent":
        return False  # shown as Free; blocks nothing, needs no reflection
    # A p0/p1/p2 or [note] title is the user's label, read by source_label.
    return not _is_rule_title(item.get("summary", ""))


def qualifies_outlook(view_item: dict) -> bool:
    """Commitment test on a calendar-view dict: timed, not shown as free,
    not declined, and not a wh/wr rule. Series and one-offs alike;
    scan_outlook decides the mirror's shape and source_label its level."""
    if view_item.get("all_day"):
        return False
    if (view_item.get("show_as") or "").lower() == "free":
        return False
    if (view_item.get("my_response") or "") == "declined":
        return False
    return not _is_rule_title(view_item.get("subject", ""))


def scan_outlook(view_items: list, get_master, failed: set | None = None) -> dict:
    """Protected series from Exchange. view_items are calendar-view dicts
    (naive UTC times); get_master(series_id) -> raw Graph series master.

    get_master raises MasterFetchError when the read FAILED; that key is
    added to `failed` (the caller's freeze set) instead of silently
    dropping out of the protected set.
    """
    protected = {}
    unsupported = set()   # series whose pattern has no RRULE form
    for item in view_items:
        if not qualifies_outlook(item):
            continue
        series = item.get("series_master_id")
        if not series or series in unsupported:
            # A one-off, or an occurrence of a series mirrored occurrence
            # by occurrence. Graph keeps an occurrence's id stable.
            if item.get("id"):
                key = source_key(OUTLOOK_ACCOUNT, item["id"])
                protected[key] = _outlook_one_off(key, item)
            continue
        key = source_key(OUTLOOK_ACCOUNT, series)
        if key in protected:
            protected[key]["instances"].append(
                (_to_local(item["start"], naive_is_utc=True),
                 _to_local(item["end"], naive_is_utc=True)))
            continue
        try:
            master = get_master(item["series_master_id"])
        except MasterFetchError:
            if failed is not None:
                failed.add(key)
            continue
        if not master:
            continue
        rrule = outlook_recurrence_to_rrule(master.get("recurrence") or {})
        if not rrule:
            # Monthly, yearly and other patterns: each occurrence in the
            # window is mirrored on its own instead of not at all.
            unsupported.add(series)
            if item.get("id"):
                one = source_key(OUTLOOK_ACCOUNT, item["id"])
                protected[one] = _outlook_one_off(one, item)
            continue
        level, rule = _outlook_level(item, one_off=False)
        protected[key] = {
            "source_key": key,
            "summary": master.get("subject", "(no title)"),
            "level": level,
            "rule": rule,
            "start_local": _graph_endpoint_to_local(master.get("start", {})),
            "end_local": _graph_endpoint_to_local(master.get("end", {})),
            "recurrence": [rrule],
            "instances": [(_to_local(item["start"], naive_is_utc=True),
                           _to_local(item["end"], naive_is_utc=True))],
        }
    for info in protected.values():
        info["instances"].sort()
    return _apply_labels(protected)


def _outlook_one_off(key: str, item: dict) -> dict:
    """Protected entry for one Exchange event mirrored on its own."""
    inst = (_to_local(item["start"], naive_is_utc=True),
            _to_local(item["end"], naive_is_utc=True))
    level, rule = _outlook_level(item, one_off=True)
    return {"source_key": key, "summary": item.get("subject") or "(no title)",
            "level": level, "rule": rule,
            "start_local": inst[0], "end_local": inst[1],
            "recurrence": [], "instances": [inst]}


RULE_SUBSCRIBED = "subscribed-source"


def scan_google(list_instances, get_master, accounts,
                failed: set | None = None, domain: str = "") -> dict:
    """Protected series from Google-readable calendars (raw API items).

    Each account entry may carry:
      calendar_id   target calendar within the account (default: primary)
      force_level   token level for everything from this source, e.g. a
                    subscribed department feed or the family calendar
                    reflected as p1 soft discouragement
      one_offs      also mirror non-recurring timed events; the blocking
                    calendars set it, and so do feeds whose meetings are
                    published as individual events

    get_master raises MasterFetchError when the read FAILED; that key
    goes into `failed` (the caller's freeze set) rather than dropping out
    of the protected set and taking its mirror with it.
    """
    protected = {}
    for acct in accounts:
        email = acct["email"]
        cal = acct.get("calendar_id") or email
        force = acct.get("force_level")
        one_offs = acct.get("one_offs", False)
        for item in list_instances(email, cal):
            if not qualifies_as_protected(item):
                continue
            series_id = item.get("recurringEventId")
            inst = (_to_local(item["start"]["dateTime"]),
                    _to_local(item["end"]["dateTime"]))
            if not series_id:
                if not one_offs:
                    continue
                key = source_key(cal, item["id"])
                if key in protected:
                    continue
                level, rule = ((force, RULE_SUBSCRIBED) if force
                               else _google_level(item, True, domain))
                protected[key] = {
                    "source_key": key,
                    "summary": item.get("summary", "(no title)"),
                    "level": level,
                    "rule": rule,
                    "start_local": inst[0], "end_local": inst[1],
                    "recurrence": [],
                    "instances": [inst],
                }
                continue
            key = source_key(cal, series_id)
            if key in protected:
                protected[key]["instances"].append(inst)
                continue
            try:
                master = get_master(email, cal, series_id)
            except MasterFetchError:
                if failed is not None:
                    failed.add(key)
                continue
            if not master or not master.get("recurrence"):
                continue
            level, rule = _google_level(item, False, domain)
            if force:
                level, rule = force, RULE_SUBSCRIBED
            protected[key] = {
                "source_key": key,
                "summary": master.get("summary", "(no title)"),
                "level": level,
                "rule": rule,
                "start_local": _to_local(master["start"]["dateTime"]),
                "end_local": _to_local(master["end"]["dateTime"]),
                "recurrence": list(master["recurrence"]),
                "instances": [inst],
            }
    for info in protected.values():
        info["instances"].sort()
    return _apply_labels(protected)


def dedupe_series(scans: list) -> dict:
    """Merge scans in priority order; Exchange is passed first, so it
    anchors the mirror when a commitment lives on several calendars.

    Identity is occurrence overlap: two sources are the same commitment
    when their observed instance sets share any occurrence. Disjoint sets
    are distinct commitments; intersecting sets are copies, and the
    first-scanned source keeps them. Neither a clock-time signature (it
    swallows Outlook's split series) nor exact set equality (it splits
    cross-calendar copies as soon as one side carries a cancelled
    instance) survives the real data. Two genuinely different meetings
    double-booked into an identical start and end merge here; one mirror
    still declares that slot, so the cost is a label.
    """
    merged, owned = {}, set()
    for scan in scans:
        for key, info in scan.items():
            occ = set(_occurrences(info))
            if occ & owned:
                continue
            owned |= occ
            merged[key] = info
    return merged


# ---------------------------------------------------------------------------
# Mirror inventory on EA-Managed
# ---------------------------------------------------------------------------

def build_description(source_key_str: str, rule: str) -> str:
    return (f"source: {source_key_str}\n"
            f"rule: {rule}\n"
            f"created: {date.today().isoformat()}\n"
            "Managed by calendar_reconciler.py. The first word is this "
            "event's priority (p0, p1, p2 or [note]); edit it to change how "
            "the scheduler treats the event. Deleting this mirror turns it "
            "into a [note]; deleting the note removes it for good.")


def parse_source_key(description: str) -> str:
    for line in (description or "").splitlines():
        if line.startswith("source: "):
            return line[len("source: "):].strip()
    return ""


def parse_rule(description: str) -> str:
    """The rule line the reconciler stamped on a mirror: which default
    set its level word."""
    for line in (description or "").splitlines():
        if line.startswith("rule: "):
            return line[len("rule: "):].strip()
    return ""


def replace_rule(description: str, rule: str) -> str:
    lines = [l for l in (description or "").splitlines() if not l.startswith("rule: ")]
    return "\n".join([lines[0], f"rule: {rule}"] + lines[1:]) if lines else f"rule: {rule}"


# Provenance stamp written into a mirror's description immediately before
# the reconciler deletes it.
REMOVED_BY_LINE = "removed-by: calendar_reconciler"


def mark_description_removed(description: str) -> str:
    """Stamp a mirror description as a RECONCILER removal.

    Google keeps a deleted master retrievable through showDeleted=True WITH
    its description, so the stamp survives on the tombstone. Without it the
    reconciler's own removal and the user's are identical from here, and a
    series that disappears for one run and comes back reads as the user
    releasing it: logged permanently and never re-protected.
    """
    body = (description or "").rstrip()
    if REMOVED_BY_LINE in body:
        return body
    stamp = (f"{REMOVED_BY_LINE} "
             f"{datetime.now(USER_TZ).isoformat(timespec='seconds')}")
    return (body + "\n" + stamp).lstrip("\n")


def is_reconciler_removal(description: str) -> bool:
    """True for a tombstone this pass (or an earlier one) created itself."""
    return REMOVED_BY_LINE in (description or "")


def is_auto_mirror(item: dict) -> bool:
    """[auto] mirrors carry a token level the USER may edit (p1 -> p0 to
    make a 1:1 immovable), so any priority token counts. The level word
    belongs to the user after creation; the reconciler manages times."""
    summary = item.get("summary", "")
    return (summary.startswith(("p0 ", "p1 ", "p2 ", "[note] "))
            and AUTO_MARKER in summary)


def mirror_level(summary: str) -> str:
    """The level word of an [auto] mirror title: p0/p1/p2 or note."""
    s = (summary or "").strip()
    if s.lower().startswith("[note]"):
        return "note"
    return s.split(None, 1)[0].lower() if s else ""


def mirror_title(level: str, summary: str) -> str:
    prefix = "[note]" if level == "note" else level
    return f"{prefix} {AUTO_MARKER} {summary}"


def _mirror_color(level: str):
    """Token colour by level; a note is graphite, like wr."""
    return gcal._token_color("wr" if level == "note" else level) or None


def note_tombstoned_keys(masters: list) -> set:
    """Keys whose [note] mirror the user deleted: the slot was already
    released once, and deleting the note too ends its mirroring."""
    out = set()
    for item in masters:
        if (item.get("recurringEventId") or item.get("status") != "cancelled"
                or not is_auto_mirror(item)
                or mirror_level(item.get("summary", "")) != "note"
                or is_reconciler_removal(item.get("description", ""))):
            continue
        key = parse_source_key(item.get("description", ""))
        if key:
            out.add(key)
    return out


def load_mirrors(masters: list) -> tuple:
    """Split EA-Managed series masters into live mirrors and tombstones.

    Returns (active: {source_key: item}, tombstoned_keys: set). A cancelled
    [auto] mirror whose source id still parses is the user's deletion,
    visible on the calendar itself via showDeleted -- UNLESS it carries
    the reconciler's own removal stamp, in which case it is our own
    housekeeping and means nothing about what the user wants
    (`self_removed_keys` reports those separately).
    """
    active, tombstoned = {}, set()
    for item in masters:
        if item.get("recurringEventId"):
            # Exception instances of a mirror inherit its [auto] summary
            # and keyed description, and events().list(singleEvents=False)
            # returns them as separate items. They are occurrence-level
            # state, owned by the instances() diff, never the series. A live
            # one here would clobber the master's entry and send series
            # retimes to an instance id; a cancelled one would tombstone,
            # and release, the whole series off one deleted occurrence.
            continue
        if not is_auto_mirror(item):
            continue
        key = parse_source_key(item.get("description", ""))
        if not key:
            continue
        if item.get("status") == "cancelled":
            if is_reconciler_removal(item.get("description", "")):
                continue  # our own removal; not a decision by the user
            tombstoned.add(key)
        else:
            active[key] = item
    return active, tombstoned


def self_removed_keys(masters: list) -> set:
    """Source keys whose mirror the RECONCILER removed (stamped tombstones).

    Reported, never acted on: the point is that these are invisible to
    the release path.
    """
    out = set()
    for item in masters:
        if item.get("recurringEventId") or not is_auto_mirror(item):
            continue
        if item.get("status") != "cancelled":
            continue
        if not is_reconciler_removal(item.get("description", "")):
            continue
        key = parse_source_key(item.get("description", ""))
        if key:
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# Release memory (the user's deletions are decisions, not glitches)
# ---------------------------------------------------------------------------

def _decision_list(text: str | None) -> list:
    """The dict entries of a scheduling-decisions.json text; [] for an
    empty, corrupt or wrongly shaped file."""
    try:
        data = json.loads(text) if text and text.strip() else []
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def released_keys_from_log() -> set:
    out = set()
    for d in _decision_list(_rio.read_text(DECISIONS_FILE)):
        if d.get("action") == "released" and isinstance(d.get("source_key"), str) \
                and d["source_key"]:
            out.add(d["source_key"])
    return out


def log_release(source_key_str: str, summary: str) -> None:
    """Append a release decision in the scheduling-decisions.json schema.

    Written directly (same read-append-write as cmd_log_decision) because
    shelling into scheduling_engine.py would import graph_client, which
    ea-bot cannot do under mail mediation. Tombstones age out of Google's
    trash; this file is the durable record, and review-patterns mines it.

    The file is resident space and this is an owner cron, so the append is
    one resident_io.update_text: the resident's child locks, reads and
    rewrites the file, and only dict entries are carried over. The same
    lock covers scheduling_engine's writes, so neither loses the other's.
    """
    entry = {
        "date": date.today().isoformat(),
        "event": summary,
        "action": "released",
        "source_key": source_key_str,
        "comment": ("User deleted the p0 [auto] mirror; slot is negotiable "
                    "again. Reconciler will not re-protect this series."),
    }

    def _append(old: str) -> str:
        return json.dumps(_decision_list(old) + [entry], indent=2)

    if not _rio.update_text(DECISIONS_FILE, _append, mode=0o664):
        raise OSError(f"could not record the release in {DECISIONS_FILE}")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_actions(protected: dict, active: dict, tombstoned: set,
                 released: set, unreliable_prefixes: set = frozenset(),
                 frozen_keys: set = frozenset(), now_local: str = "",
                 covered: set = frozenset(),
                 final_keys: set = frozenset()) -> list:
    """Diff desired state against EA-Managed. Pure; returns action dicts.

    now_local (naive local ISO): a one-off mirror that ended before it is
    history, not a source gone; it stays. covered: one-off sources a
    hand-placed token already declares (covered_one_offs); no mirror is
    created for them.

    A mirror the user deleted releases its event: the deletion is logged
    once and the event comes back as a [note] mirror, visible and never
    blocking. final_keys (note_tombstoned_keys) are events whose note the
    user deleted as well; they stay off the calendar. A label on the
    source event relevels its mirror; without one, the mirror's level word
    is the user's and is never touched.

    unreliable_prefixes: source accounts whose fetch failed this run.
    Their mirrors get no destructive action, because "the fetch broke" and
    "every commitment vanished" look identical from here.

    frozen_keys: individual series whose MASTER could not be read this run
    (MasterFetchError). Same rule at series granularity: an unreadable
    master is not a cancelled commitment, so its mirror stands.

    While ANY source is unreliable, nothing is created either. Exchange
    anchors the cross-calendar dedupe, so with the Outlook scan down the
    Google copies of Outlook-anchored series (the department meeting on
    the subscribed feed, the lecture on the family calendar) surface as
    new series, get their own mirrors, and lose them again as "source
    gone" once Outlook returns. A fetch that broke is not new commitments
    appearing any more than it is old ones vanishing; the create waits for
    the next run.
    """
    def _unreliable(key):
        return any(key.startswith(p + "/") for p in unreliable_prefixes)

    actions = []
    for key, info in protected.items():
        if key in final_keys and key not in active:
            continue
        if (key in released or key in tombstoned) and key not in active:
            if key not in released:
                # Fresh deletion by the user: record it once.
                actions.append({"op": "release", "key": key,
                                "summary": info["summary"]})
            if unreliable_prefixes:
                continue
            note = info if info.get("labeled") else {
                **info, "level": "note", "rule": RULE_RELEASED}
            actions.append({"op": "create", "key": key, "info": note})
            continue
        if key not in active:
            if unreliable_prefixes or key in covered:
                continue  # deferred until every source is readable
            actions.append({"op": "create", "key": key, "info": info})
            continue
        if _unreliable(key):
            continue
        mirror = active[key]
        m_start = _to_local(mirror["start"]["dateTime"])
        m_end = _to_local(mirror["end"]["dateTime"])
        if (m_start != info["start_local"] or m_end != info["end_local"]
                or _rrule_signature(mirror.get("recurrence") or [])
                != _rrule_signature(info["recurrence"])):
            actions.append({"op": "retime", "key": key, "info": info,
                            "mirror_id": mirror["id"],
                            "one_off": not info["recurrence"]})
        if (info.get("labeled")
                and mirror_level(mirror.get("summary", "")) != info["level"]):
            actions.append({"op": "relevel", "key": key, "info": info,
                            "mirror_id": mirror["id"]})
        elif (mirror_level(mirror.get("summary", "")) == "p2"
                and parse_rule(mirror.get("description", "")) == RULE_UNANSWERED
                and info.get("rule") != RULE_UNANSWERED and info["level"] != "p2"):
            # The p2 was the reconciler's own default for an invite the
            # user had not answered; the invite is accepted now, so the
            # mirror takes the level the answered event gets. The rule
            # line moves with it, so a p2 the user writes later stands.
            actions.append({"op": "relevel", "key": key, "info": info,
                            "mirror_id": mirror["id"],
                            "description": replace_rule(mirror.get("description", ""), info["rule"])})
    for key, mirror in active.items():
        if key in protected or _unreliable(key) or key in frozen_keys:
            continue
        one_off = not mirror.get("recurrence")
        if one_off and now_local and _to_local(
                (mirror.get("end") or {}).get("dateTime", now_local)) <= now_local:
            continue  # the meeting happened; its mirror is the record
        actions.append({"op": "remove", "key": key, "one_off": one_off,
                        "summary": mirror.get("summary", ""),
                        "mirror_id": mirror["id"],
                        # Carried so the removal can stamp its provenance
                        # onto the tombstone without losing the source
                        # anchor line (see mark_description_removed).
                        "description": mirror.get("description", "")})
    return actions


_LEVEL_RANK = {"p0": 0, "nm": 0, "p1": 1, "p2": 2, "note": 3}


def hand_token_spans(masters: list) -> list:
    """(start_local, end_local, level) of the single tokens placed by hand,
    by the user or the EA: timed, live, not [auto] and not recurring."""
    spans = []
    for item in masters:
        if (item.get("status") == "cancelled" or item.get("recurrence")
                or item.get("recurringEventId")):
            continue
        summary = item.get("summary", "")
        level = summary.split(None, 1)[0].lower() if summary.strip() else ""
        if AUTO_MARKER in summary or level not in ("p0", "p1", "p2", "nm"):
            continue
        start = (item.get("start") or {}).get("dateTime")
        end = (item.get("end") or {}).get("dateTime")
        if start and end:
            spans.append((_to_local(start), _to_local(end), level))
    return spans


def covered_one_offs(protected: dict, spans: list) -> set:
    """One-off sources a hand-placed token already declares: a token that
    spans the whole event at the same or a higher priority. A mirror would
    only repeat it."""
    out = set()
    for key, info in protected.items():
        if info.get("recurrence"):
            continue
        rank = _LEVEL_RANK.get(info.get("level", "p0"), 0)
        if any(s <= info["start_local"] and e >= info["end_local"]
               and _LEVEL_RANK[lvl] <= rank for s, e, lvl in spans):
            out.add(key)
    return out


# A rescheduled occurrence moves by days or a few weeks. A vacated mirror
# slot further away than this is a different occurrence, and pairing the two
# turns one bad read into two writes.
MAX_MOVE_DAYS = 28


def horizon_window(now_local: datetime, horizon_days: int) -> tuple:
    """ONE window for both sources: (outlook_start, outlook_end, span_utc).

    Both bounds derive from one aware local datetime. Deriving the Outlook
    bound from naive local time and the Google bound from UTC puts them an
    hour apart across a DST change, and every occurrence in that hour is
    then a source with no mirror. The window ends at the END of the horizon
    day, so a day's occurrences enter it together instead of one at a time
    as the edge slides.
    """
    end_local = (now_local + timedelta(days=horizon_days)).replace(
        hour=23, minute=59, second=0, microsecond=0)
    span_utc = (now_local.astimezone(timezone.utc),
                end_local.astimezone(timezone.utc))
    return (now_local.strftime("%Y-%m-%dT%H:%M"),
            end_local.strftime("%Y-%m-%dT%H:%M"), span_utc)


def plan_instance_actions(protected: dict, active: dict,
                          mirror_instances: dict,
                          series_acted_keys: set,
                          unreliable_prefixes: set = frozenset(),
                          cancelled_instances: dict | None = None) -> list:
    """Per-occurrence reconciliation for series that are otherwise in sync.

    A cancelled source instance (the Thanksgiving lecture) cancels the
    matching mirror instance, releasing that occurrence. A single moved
    instance retimes its mirror instance.

    A RESCHEDULED occurrence shows up as two halves: the old day vacated
    (mirror instance with no source) plus a new off-pattern day (source
    with no mirror). Treating those independently releases the old slot
    and leaves the new one unprotectable, so the halves are PAIRED into a
    single move_instance: the mirror instance is retimed to the new day,
    reviving it (status back to confirmed) when the old half was already
    cancelled on a previous run. Pairing never touches a cancelled mirror
    instance on a day the source still occupies: that is the user
    releasing one occurrence by hand, and their deletion stands.

    A source occurrence with no pairable partner (a genuinely restored
    instance) is still reported, not written. Keys with a series-level
    action this run are skipped; the instance view is stale until the
    series write lands, and the next run is 30 minutes away.
    """
    def _unreliable(key):
        return any(key.startswith(p + "/") for p in unreliable_prefixes)

    cancelled_instances = cancelled_instances or {}
    actions = []
    for key, info in protected.items():
        if key not in active or key in series_acted_keys or _unreliable(key):
            continue
        if not info.get("recurrence"):
            continue  # one-off mirrors have no occurrence set to diff
        source = {}  # date -> (start, end)
        for s, e in info.get("instances", []):
            source[s[:10]] = (s, e)
        source_days_all = set(source)
        cancels, retimes = [], []
        for m_start, m_end, inst_id in mirror_instances.get(key, []):
            day = m_start[:10]
            if day not in source:
                cancels.append({"op": "cancel_instance", "key": key,
                                "summary": info["summary"],
                                "instance_id": inst_id, "when": m_start})
                continue
            s, e = source[day]
            if (s, e) != (m_start, m_end):
                retimes.append({"op": "retime_instance", "key": key,
                                "summary": info["summary"],
                                "instance_id": inst_id,
                                "start_local": s, "end_local": e})
            del source[day]

        # Pair each remaining gap with a vacated mirror slot: a live one
        # about to be cancelled this run, or one already cancelled on a
        # prior run (revive). Same-day cancelled instances are excluded
        # above the loop: their day is still in source, so they are a
        # user release, not a move.
        partners = [{"day": c["when"][:10], "cancel": c,
                     "instance_id": c["instance_id"], "revive": False}
                    for c in cancels]
        partners += [{"day": day, "cancel": None, "instance_id": inst_id,
                      "revive": True}
                     for day, inst_id in cancelled_instances.get(key, [])
                     if day not in source_days_all]
        moves, gaps = [], []
        for s, e in sorted(source.values()):
            day = s[:10]
            best = None
            for p in partners:
                if p.get("used") or p["day"] == day:
                    continue
                dist = abs((date.fromisoformat(day)
                            - date.fromisoformat(p["day"])).days)
                if dist > MAX_MOVE_DAYS:
                    continue  # not the same occurrence rescheduled
                if best is None or dist < best[0] or (
                        dist == best[0] and not p["revive"] and best[1]["revive"]):
                    best = (dist, p)
            if best is None:
                gaps.append({"op": "instance_gap", "key": key,
                             "summary": info["summary"], "when": s})
                continue
            best[1]["used"] = True
            moves.append({"op": "move_instance", "key": key,
                          "summary": info["summary"],
                          "instance_id": best[1]["instance_id"],
                          "start_local": s, "end_local": e, "when": s,
                          "revive": best[1]["revive"]})
        paired_cancels = {id(p["cancel"]) for p in partners
                          if p.get("used") and p["cancel"] is not None}
        actions += [c for c in cancels if id(c) not in paired_cancels]
        actions += retimes + moves + gaps
    return actions


# ---------------------------------------------------------------------------
# Apply + notify
# ---------------------------------------------------------------------------

def apply_actions(actions: list, service, ea: dict) -> list:
    """Execute the plan against the EA-Managed calendar. Returns results.

    Guard rails: creates carry the [auto] marker by construction; every
    other id came out of load_mirrors or the mirror's own instance list,
    which admit nothing without the marker.
    """
    results = []
    for a in actions:
        try:
            if a["op"] == "create":
                info = a["info"]
                level = info.get("level", "p0")
                body = {
                    "summary": mirror_title(level, info["summary"]),
                    "description": build_description(
                        a["key"], info.get("rule", RULE_RECURRING_HOLD)),
                    "start": {"dateTime": info["start_local"],
                              "timeZone": USER_TZ_NAME},
                    "end": {"dateTime": info["end_local"],
                            "timeZone": USER_TZ_NAME},
                    "recurrence": info["recurrence"],
                    "colorId": _mirror_color(level),
                }
                body = {k: v for k, v in body.items() if v}
                created = service.events().insert(
                    calendarId=ea["calendar_id"], body=body).execute()
                results.append({**a, "status": "ok", "id": created.get("id")})
            elif a["op"] == "retime":
                info = a["info"]
                service.events().patch(
                    calendarId=ea["calendar_id"], eventId=a["mirror_id"],
                    body={
                        "start": {"dateTime": info["start_local"],
                                  "timeZone": USER_TZ_NAME},
                        "end": {"dateTime": info["end_local"],
                                "timeZone": USER_TZ_NAME},
                        "recurrence": info["recurrence"],
                    }).execute()
                results.append({**a, "status": "ok"})
            elif a["op"] == "remove":
                # Stamp provenance FIRST. The tombstone Google keeps is
                # the only record of who deleted this mirror, and an
                # unstamped one reads as the user's decision on the run the
                # source reappears. If the stamp fails the delete does not
                # happen: an unremoved mirror costs a stale hold, an
                # unstamped tombstone costs the protection permanently.
                original = a.get("description", "")
                service.events().patch(
                    calendarId=ea["calendar_id"], eventId=a["mirror_id"],
                    body={"description": mark_description_removed(
                        original)}).execute()
                try:
                    service.events().delete(
                        calendarId=ea["calendar_id"],
                        eventId=a["mirror_id"]).execute()
                except Exception:
                    # The stamp landed but the delete did not. A LIVE
                    # mirror carrying a removal stamp would make the
                    # user's own later deletion read as ours, and their
                    # release would be silently undone. Put it back.
                    try:
                        service.events().patch(
                            calendarId=ea["calendar_id"],
                            eventId=a["mirror_id"],
                            body={"description": original}).execute()
                    except Exception:
                        pass
                    raise
                results.append({**a, "status": "ok"})
            elif a["op"] == "relevel":
                level = a["info"]["level"]
                body = {"summary": mirror_title(level, a["info"]["summary"]),
                        "colorId": _mirror_color(level)}
                if a.get("description"):
                    body["description"] = a["description"]
                service.events().patch(
                    calendarId=ea["calendar_id"], eventId=a["mirror_id"],
                    body=body).execute()
                results.append({**a, "status": "ok"})
            elif a["op"] == "cancel_instance":
                service.events().delete(
                    calendarId=ea["calendar_id"],
                    eventId=a["instance_id"]).execute()
                results.append({**a, "status": "ok"})
            elif a["op"] == "retime_instance":
                service.events().patch(
                    calendarId=ea["calendar_id"], eventId=a["instance_id"],
                    body={
                        "start": {"dateTime": a["start_local"],
                                  "timeZone": USER_TZ_NAME},
                        "end": {"dateTime": a["end_local"],
                                "timeZone": USER_TZ_NAME},
                    }).execute()
                results.append({**a, "status": "ok"})
            elif a["op"] == "move_instance":
                body = {
                    "start": {"dateTime": a["start_local"],
                              "timeZone": USER_TZ_NAME},
                    "end": {"dateTime": a["end_local"],
                            "timeZone": USER_TZ_NAME},
                }
                if a.get("revive"):
                    body["status"] = "confirmed"
                service.events().patch(
                    calendarId=ea["calendar_id"], eventId=a["instance_id"],
                    body=body).execute()
                results.append({**a, "status": "ok"})
            elif a["op"] == "release":
                log_release(a["key"], a["summary"])
                results.append({**a, "status": "ok"})
            elif a["op"] == "instance_gap":
                results.append({**a, "status": "reported"})
        except Exception as e:  # keep going; report per-action
            results.append({**a, "status": "error", "error": str(e)})
    return results


_LOUD_OPS = {"retime", "remove", "release", "cancel_instance",
             "retime_instance", "instance_gap"}


def _gap_file_private() -> bool:
    root = os.path.realpath(_owner_trust.private_dir())
    parent = os.path.realpath(GAP_WARNED_FILE.parent)
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


LEGACY_GAP_WARNED_FILE = EA_DIR / "state" / "reconciler-gap-warned.json"


def _read_gap_warned() -> set:
    """The gap keys already warned about; empty when unknown. With no file
    in the owner-private directory yet, the legacy one in resident space
    (read with the resident's access) seeds it, so the move does not repeat
    every open warning once."""
    try:
        if _gap_file_private():
            try:
                raw = _owner_trust.read_owner_file(GAP_WARNED_FILE).decode("utf-8")
            except FileNotFoundError:
                raw = _rio.read_text(LEGACY_GAP_WARNED_FILE) or "[]"
        else:
            raw = GAP_WARNED_FILE.read_text()
        data = json.loads(raw)
    except Exception:
        return set()
    return {k for k in data if isinstance(k, str)} if isinstance(data, list) else set()


def _write_gap_warned(keys: list) -> None:
    """Replace the dedupe state: through owner_trust (0600) in the
    owner-private directory, by atomic replace elsewhere."""
    if _gap_file_private():
        _owner_trust.write_owner_file(GAP_WARNED_FILE, json.dumps(keys), mode=0o600)
    else:
        _atomic.write_text(GAP_WARNED_FILE, json.dumps(keys))


def suppress_known_gaps(results: list) -> list:
    """instance_gap warnings post to Slack ONCE per (series, day), not every
    30 minutes until a human intervenes. Only the Slack copy is deduped: the
    full gap list stays in the run report. A gap that heals drops out of the
    state file, so the same occurrence warns afresh if it ever re-opens."""
    def _gap_key(r):
        return f"{r['key']}|{r['when'][:10]}"

    gaps_now = {_gap_key(r) for r in results if r["op"] == "instance_gap"}
    warned = _read_gap_warned()
    fresh = gaps_now - warned
    try:
        _write_gap_warned(sorted(gaps_now))
    except (OSError, _owner_trust.OwnerTrustError):
        pass  # dedupe is best-effort; never block the reconcile
    return [r for r in results
            if r["op"] != "instance_gap" or _gap_key(r) in fresh]


def notify(results: list) -> None:
    """One chat post per run, only for what may need a look: a
    protection-reducing action (_LOUD_OPS) or a failed one. A routine
    create is the system working and posts nothing; the run report keeps
    it. So is a one-off meeting moved or cancelled (one_off): meetings
    move every week. The post goes to the calendar target (the scheduling channel when
    configured), never the main EA channel when a better one exists.
    Quiet hours apply (no bypass): a 3am change can wait for morning."""
    results = [r for r in results or []
               if (r["op"] in _LOUD_OPS and not r.get("one_off"))
               or r["status"] not in ("ok", "reported")]
    if not results:
        return
    try:
        import slack_notifier  # noqa: PLC0415  (SCRIPT_DIR is on sys.path)
    except ImportError:
        from preprocessor import slack_notifier  # noqa: PLC0415
    lines = [":warning: *Calendar protection changed*"]
    for r in results:
        name = (r.get("info", {}).get("summary")
                or r.get("summary") or r["key"])
        when = f" ({r['when'][:10]})" if r.get("when") else ""
        verb = {
            "create": "protected",
            "retime": "moved with its source",
            "relevel": "relabelled from its source",
            "remove": "unprotected (source gone)",
            "release": "released by you (kept as a [note])",
            "cancel_instance": "occurrence released (source cancelled)",
            "retime_instance": "occurrence moved with its source",
            "move_instance": "occurrence re-protected at its rescheduled "
                             "time",
            "instance_gap": "occurrence unprotected; needs a look "
                            "(source instance restored?)",
        }[r["op"]]
        mark = ("" if r["status"] in ("ok", "reported")
                else f" [FAILED: {r.get('error')}]")
        lines.append(f"- *{name}*{when}: {verb}{mark}")
    slack_notifier.post_to_slack(slack_notifier.calendar_channel_key(), "\n".join(lines))


# ---------------------------------------------------------------------------
# Live fetch wiring
# ---------------------------------------------------------------------------

def _blocking_accounts() -> list:
    return [a for a in gcal._get_google_accounts()
            if a.get("blocks_availability", True)]


def list_masters(service, calendar_id: str) -> list:
    """Every event master on EA-Managed, deleted ones included, across all
    pages. A mirror missing from this list reads as absent and is created
    again, so a truncated read would duplicate the calendar."""
    masters, page = [], None
    while True:
        kwargs = {"calendarId": calendar_id, "singleEvents": False,
                  "showDeleted": True, "maxResults": 2500}
        if page:
            kwargs["pageToken"] = page
        resp = service.events().list(**kwargs).execute()
        masters += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return masters


def _user_domain() -> str:
    """"@<domain>" of the user's own address: a 1:1 with someone outside it
    is mirrored as p0."""
    import institution
    return institution.own_suffix()


def _graph_actions(*argv) -> dict:
    """Run a graph_actions verb as a subprocess (the mediated Outlook path
    that works for ea-bot). Raises on failure so the caller can mark the
    Outlook source unreliable instead of treating it as empty."""
    cmd = ["python3", str(SCRIPT_DIR / "graph_actions.py"), *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or "graph_actions failed")
    out = proc.stdout
    return json.loads(out[out.index("{"):])


def _live_run(apply: bool, horizon_days: int) -> dict:
    # The target this run writes with the owner's token. In the owner's
    # cron it comes from the owner-private calendars.json only, never from
    # the resident's scheduling-preferences.json, and without that file
    # nothing is written (google_calendar_client.ea_managed_target).
    ea = gcal.ea_managed_target()
    if not ea:
        return {"error": "EA-Managed calendar not configured: the owner-private "
                         "calendars.json is missing or refused "
                         "(python3 preprocessor/owner_trust.py seed --calendar)"}

    now_local = datetime.now(USER_TZ)
    view_start, view_end, span_utc = horizon_window(now_local, horizon_days)
    unreliable, scans = set(), []
    frozen_keys = set()   # per-series master fetch failures

    # Exchange first: it anchors mirrors for cross-calendar duplicates.
    try:
        view = _graph_actions(
            "calendar-view", "--start", view_start, "--end", view_end)
        events = view.get("events", [])
        if events and all("series_master_id" not in e for e in events):
            # A mediator or deployed copy running pre-reconciler code
            # returns events without the field at all. Reading that as
            # "no recurring holds exist" would remove every Outlook
            # mirror; reading it as an unreliable source freezes them.
            raise RuntimeError(
                "calendar-view output lacks series_master_id; "
                "stale mediator or deployed code")

        def get_outlook_master(series_id):
            # Every failure here is a READ failure, never "the series is
            # gone": we only ask for masters whose occurrences are sitting
            # in the calendar view we just fetched. A deleted series has no
            # occurrences and never reaches this call.
            try:
                master = _graph_actions("get-event", "--event-id", series_id)
            except Exception as exc:
                raise MasterFetchError(str(exc)[:200]) from exc
            if master.get("error"):
                raise MasterFetchError(str(master["error"])[:200])
            return master

        scans.append(scan_outlook(view.get("events", []), get_outlook_master,
                                  failed=frozen_keys))
    except Exception as e:
        unreliable.add(OUTLOOK_ACCOUNT)
        print(f"WARNING: Outlook scan failed, freezing its mirrors: {e}",
              file=sys.stderr)

    good_accounts, seen = [], set()
    for acct in _blocking_accounts():
        cal_id = acct.get("calendar_id") or acct["email"]
        if cal_id == ea["calendar_id"] or (acct["email"], cal_id) in seen:
            continue  # EA-Managed is the target, never a source; one scan per calendar
        seen.add((acct["email"], cal_id))
        if gcal._build_service(acct["email"]):
            good_accounts.append({**acct, "one_offs": True})
        else:
            unreliable.add(acct["email"])
            print(f"WARNING: Google auth failed for {acct['email']}, "
                  "freezing its mirrors", file=sys.stderr)

    def list_instances(email, cal):
        return gcal._build_service(email).events().list(
            calendarId=cal, timeMin=span_utc[0].isoformat(),
            timeMax=span_utc[1].isoformat(),
            singleEvents=True, maxResults=500).execute().get("items", [])

    def get_master(email, cal, series_id):
        # Same argument as the Outlook side: the series id came off an
        # instance in the window we just listed, so a failure here is the
        # API refusing, not the series ceasing to exist.
        try:
            return gcal._build_service(email).events().get(
                calendarId=cal, eventId=series_id).execute()
        except Exception as exc:
            raise MasterFetchError(str(exc)[:200]) from exc

    # Extra sources: subscribed feeds and soft-reflected calendars,
    # declared in scheduling-preferences.json with a forced level.
    extras = []
    try:
        prefs = json.loads(gcal.CONFIG_FILE.read_text())
        for src in prefs.get("reconciler_extra_sources", []):
            if not src.get("email") or not src.get("calendar_id"):
                continue
            if gcal._build_service(src["email"]):
                extras.append({"email": src["email"],
                               "calendar_id": src["calendar_id"],
                               "force_level": src.get("level", "p1"),
                               "one_offs": src.get("one_offs", False)})
            else:
                unreliable.add(src["calendar_id"])
    except Exception as e:
        print(f"WARNING: extra sources unreadable: {e}", file=sys.stderr)

    scans.append(scan_google(list_instances, get_master,
                             good_accounts + extras, failed=frozen_keys,
                             domain=_user_domain()))
    protected = dedupe_series(scans)
    if frozen_keys:
        print(f"WARNING: {len(frozen_keys)} series master(s) unreadable, "
              f"freezing their mirrors: {sorted(frozen_keys)}",
              file=sys.stderr)

    ea_service = gcal._build_service(ea["email"])
    masters = list_masters(ea_service, ea["calendar_id"])
    active, tombstoned = load_mirrors(masters)

    now_str = now_local.replace(tzinfo=None).isoformat(timespec="seconds")
    actions = plan_actions(protected, active, tombstoned,
                           released_keys_from_log(), unreliable, frozen_keys,
                           now_local=now_str,
                           covered=covered_one_offs(
                               protected, hand_token_spans(masters)),
                           final_keys=note_tombstoned_keys(masters))
    acted_keys = {a["key"] for a in actions}

    mirror_instances = {}
    cancelled_instances = {}
    for key, mirror in active.items():
        if key in acted_keys or key not in protected:
            continue
        if not mirror.get("recurrence"):
            continue  # single-event mirrors are handled by the retime diff
        items = ea_service.events().instances(
            calendarId=ea["calendar_id"], eventId=mirror["id"],
            timeMin=span_utc[0].isoformat(), timeMax=span_utc[1].isoformat(),
            showDeleted=True,
            maxResults=250).execute().get("items", [])
        live, gone = [], []
        for i in items:
            if i.get("status") == "cancelled":
                # A cancelled instance keeps only its originalStartTime;
                # the DAY is all the move-pairing needs.
                orig = (i.get("originalStartTime") or i.get("start") or {})
                stamp = orig.get("dateTime") or orig.get("date") or ""
                if stamp:
                    day = (_to_local(stamp)[:10] if "T" in stamp
                           else stamp[:10])
                    gone.append((day, i["id"]))
            elif "dateTime" in i.get("start", {}):
                live.append((_to_local(i["start"]["dateTime"]),
                             _to_local(i["end"]["dateTime"]), i["id"]))
        mirror_instances[key] = live
        if gone:
            cancelled_instances[key] = gone

    actions += plan_instance_actions(protected, active, mirror_instances,
                                     acted_keys, unreliable,
                                     cancelled_instances)

    report = {
        "protected_series": len(protected),
        "protected_keys": sorted(protected),
        "active_mirrors": len(active),
        # User deletions only. Mirrors the reconciler removed itself carry
        # a provenance stamp and are listed separately, never released.
        "tombstones": sorted(tombstoned),
        "self_removed_tombstones": sorted(self_removed_keys(masters)),
        "unreliable_sources": sorted(unreliable),
        "frozen_series": sorted(frozen_keys),
        "plan": actions,
        "applied": False,
    }
    if apply and actions:
        results = apply_actions(actions, ea_service, ea)
        try:
            notify(suppress_known_gaps(results))
        except Exception as e:
            # A Slack hiccup must never make a completed apply look like
            # a failed run; the report below is the record either way.
            print(f"WARNING: notification failed: {e}", file=sys.stderr)
        report["applied"] = True
        report["results"] = results
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write changes. Without this flag the run is a "
                         "dry-run that prints the plan and touches nothing.")
    ap.add_argument("--horizon-days", type=int, default=HORIZON_DAYS)
    args = ap.parse_args()
    report = _live_run(apply=args.apply, horizon_days=args.horizon_days)
    print(json.dumps(report, indent=2, default=str))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    # A tick: its Graph calls run in the background lane unless its caller
    # set one (retry_policy.graph_lane).
    import retry_policy
    retry_policy.set_graph_lane(retry_policy.BACKGROUND, only_if_unset=True)
    sys.exit(main())
