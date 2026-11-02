"""Timezone-aware quiet hours + travel mode for proactive Slack delivery.

The EA's cron jobs capture work on the server's Mountain Time schedule
(correct: the user's business is MT). But unsolicited Slack posts should
respect the user's CURRENT local waking hours. This module gates delivery
at slack_notifier.post_to_slack: anything that would land during local quiet
hours is held in a queue and flushed as ONE digest the moment quiet hours
end (the user's local morning), so nothing buzzes the phone at 3am but
nothing is lost either.

Config (ea-config.json):
  "quiet_hours": {"enabled": true, "start": "22:00", "end": "07:30", "allow_urgent": false}
  "travel_mode": {"enabled": true, "timezone": "Europe/Lisbon", "until": "2026-05-20"}

Active timezone = travel_mode.timezone when travel_mode is enabled and the
date is on/before `until`; otherwise the home timezone from
scheduling-preferences.json, else user_tz.py (the profile, then the
system's zone). travel_mode auto-reverts after
`until` with no rewrite.

Delivery flow:
  post_to_slack(...) -> should_defer(priority)? -> defer() to a queue file.
  cron: `quiet_hours.py flush` runs every 15 min (all hours); when the
  window has ended it posts the held queue as one digest and clears it.

No circular import: slack_notifier imports this lazily inside post_to_slack;
this module imports slack_notifier at top (slack_notifier is fully loaded by
the time post_to_slack runs).

Files: the deferred queue, the travel state and the hour overrides are in
the resident's space and both users write them (the owner's crons and
notifier, the EA through this CLI), so every read and write of them goes
through resident_io. The per-agent last-run record is written only by
owner processes (run-agent.sh) and read by the sentinel in the state dir,
so it is written by atomic replace and read with the resident's access.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io
import ea_config
import resident_io
import slack_notifier
import user_tz

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
PREFS_FILE = EA_DIR / "config" / "scheduling-preferences.json"
QUEUE_FILE = EA_DIR / "state" / "deferred-posts.json"
# Travel mode is user-toggled state, so it lives in the writable state/ dir
# rather than the read-only config/ dir.
TRAVEL_STATE_FILE = EA_DIR / "state" / "travel-mode.json"
# Per-agent "last ran on this local date" marker, so an hourly cron can run
# an agent exactly once at its target LOCAL hour (e.g. the 4am-local
# morning briefing) regardless of the server timezone.
LAST_RUN_FILE = EA_DIR / "state" / "agent-last-run.json"
# One-DAY target-hour overrides, keyed by agent: {"morning-briefing":
# {"date": "2026-08-27", "hour": 6}}. Each entry names the LOCAL date it
# applies to and is ignored on every other date, so a "just for tomorrow"
# change expires on its own. Nothing to revert, and no edit to the cron
# schedule or to run-agent.sh's default hour.
HOUR_OVERRIDE_FILE = EA_DIR / "state" / "agent-hour-overrides.json"
# The hour each agent normally targets. run-agent.sh passes the real value on
# the command line and stays the authority; this mirror lets `status` answer
# "what time does the briefing run today?" in one call.
DEFAULT_AGENT_HOURS = {"morning-briefing": 4}
DEFAULT_CHANNEL = "ea-assistant"


def _read_json(path: Path):
    """The parsed JSON of a file in the resident's space, read with the
    resident's access; None when it is missing or unreadable."""
    text = resident_io.read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _home_tz() -> str:
    prefs = _read_json(PREFS_FILE)
    tz = prefs.get("timezone") if isinstance(prefs, dict) else None
    return tz if isinstance(tz, str) and tz else user_tz.name(EA_DIR)


def _get_travel() -> dict:
    """Travel-mode settings: the writable state file wins, then any static
    travel_mode block in ea-config.json, then defaults (disabled)."""
    state = _read_json(TRAVEL_STATE_FILE)
    if isinstance(state, dict):
        tm = dict(ea_config.DEFAULTS["travel_mode"])
        tm.update(state)
        return tm
    return ea_config.get_travel_mode()


def _travel_active(today: date | None = None) -> bool:
    tm = _get_travel()
    if not tm.get("enabled") or not tm.get("timezone"):
        return False
    until = tm.get("until")
    if until:
        try:
            if (today or date.today()) > date.fromisoformat(str(until)):
                return False  # trip is over; auto-revert
        except ValueError:
            pass  # malformed `until` -> treat as no expiry
    return True


def active_timezone() -> str:
    """The timezone quiet-hours math runs in right now."""
    if _travel_active():
        return _get_travel()["timezone"]
    return _home_tz()


def _parse_hhmm(s: str, fallback: str) -> time:
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except Exception:
        h, m = fallback.split(":")
        return time(int(h), int(m))


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True if the current (or given) instant falls in the local quiet window."""
    qh = ea_config.get_quiet_hours()
    if not qh.get("enabled"):
        return False
    # Quiet hours apply only while traveling by default; at home the EA posts
    # immediately.
    if qh.get("only_when_traveling") and not _travel_active():
        return False
    tz = ZoneInfo(active_timezone())
    now_local = now.astimezone(tz) if now is not None else datetime.now(tz)
    start = _parse_hhmm(qh.get("start", "22:00"), "22:00")
    end = _parse_hhmm(qh.get("end", "07:30"), "07:30")
    t = now_local.time()
    if start <= end:
        return start <= t < end
    # overnight wrap (e.g. 22:00 -> 07:30 next day)
    return t >= start or t < end


def local_hour(now: datetime | None = None) -> int:
    """Current hour (0-23) in the active timezone."""
    tz = ZoneInfo(active_timezone())
    return (now.astimezone(tz) if now is not None else datetime.now(tz)).hour


def _local_date(now: datetime | None = None) -> str:
    tz = ZoneInfo(active_timezone())
    return (now.astimezone(tz) if now is not None else datetime.now(tz)).date().isoformat()


def _load_hour_overrides() -> dict:
    data = _read_json(HOUR_OVERRIDE_FILE)
    return data if isinstance(data, dict) else {}


def _update_json(path: Path, fn, default):
    """Read-modify-write a JSON file in the resident's space under its lock,
    with the resident's access. `fn(data)` changes `data` in place; the
    value the file held (or `default` when it held none) comes back after
    the change. Raises OSError when the file could not be written."""
    box = {}

    def _rewrite(text: str) -> str:
        try:
            data = json.loads(text) if text.strip() else default
        except ValueError:
            data = default
        if not isinstance(data, type(default)):
            data = default
        fn(data)
        box["data"] = data
        return json.dumps(data, indent=2)

    if not resident_io.update_text(path, _rewrite, 0o664):
        raise OSError(f"could not update {path}")
    return box.get("data", default)


def hour_override(agent: str, now: datetime | None = None) -> int | None:
    """The one-day target hour for `agent` today, or None.

    An entry applies only on the LOCAL date it names, so it expires without
    anyone remembering to remove it. A malformed entry is ignored: a bad
    override must not stop the briefing, only fail to move it."""
    entry = _load_hour_overrides().get(agent)
    if not isinstance(entry, dict) or entry.get("date") != _local_date(now):
        return None
    try:
        h = int(entry["hour"])
    except (KeyError, TypeError, ValueError):
        return None
    return h if 0 <= h <= 23 else None


def _effective_hour(agent: str, default: int,
                    now: datetime | None = None) -> int:
    """Today's target hour for `agent`: the override if one applies today,
    else `default`. An override of 0 (midnight) is a real value, so this
    tests for None rather than falsiness."""
    override = hour_override(agent, now)
    return default if override is None else override


def set_hour_override(agent: str, local_date: str, hour: int) -> dict:
    """Record a one-day target hour. Returns the full override map."""
    date.fromisoformat(local_date)          # raises on a malformed date
    if not 0 <= int(hour) <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    entry = {"date": local_date, "hour": int(hour)}
    return _update_json(HOUR_OVERRIDE_FILE,
                        lambda d: d.__setitem__(agent, entry), {})


def clear_hour_override(agent: str) -> dict:
    return _update_json(HOUR_OVERRIDE_FILE, lambda d: d.pop(agent, None), {})


def _load_last_runs() -> dict:
    """{agent: local date}, read with the resident's access and taken only
    as {str: "YYYY-MM-DD"}."""
    old = _read_json(LAST_RUN_FILE)
    if not isinstance(old, dict):
        return {}
    out = {}
    for k, v in old.items():
        if isinstance(k, str) and isinstance(v, str):
            try:
                date.fromisoformat(v)
            except ValueError:
                continue
            out[k] = v
    return out


def due_local(agent: str, hour: int, now: datetime | None = None) -> bool:
    """True (and records a run) when the active-timezone hour matches `hour`
    and `agent` has not already run on today's LOCAL date. Lets an hourly
    cron fire an agent once per day at a target local hour, anywhere in the
    world. Records the run only when returning True, so it won't double-fire
    within the matching hour.

    A one-day override (see HOUR_OVERRIDE_FILE) replaces `hour` for its date
    only. It moves the run rather than adding one: on an override day the
    default hour no longer matches, so the agent still fires exactly once."""
    hour = _effective_hour(agent, hour, now)
    if local_hour(now) != hour:
        return False
    today = _local_date(now)
    runs = _load_last_runs()
    if runs.get(agent) == today:
        return False
    runs[agent] = today
    # An atomic replace: a link at the name is replaced, never followed.
    atomic_io.write_json(LAST_RUN_FILE, runs, mode=0o664)
    return True


def should_defer(priority: str = "normal", now: datetime | None = None) -> bool:
    """Whether a post of this priority should be held right now."""
    qh = ea_config.get_quiet_hours()
    if not qh.get("enabled") or not in_quiet_hours(now):
        return False
    if priority == "urgent" and qh.get("allow_urgent"):
        return False  # let true urgent through when configured
    return True


def quiet_end(now: datetime | None = None) -> datetime | None:
    """The moment the quiet window that holds a normal post now ends (the
    next `end` in the active timezone), or None when should_defer("normal")
    does not hold. The web chat queues a reply's or a briefing's push until
    then (docs/webchat.md, Notifications)."""
    if not should_defer("normal", now):
        return None
    tz = ZoneInfo(active_timezone())
    now_local = now.astimezone(tz) if now is not None else datetime.now(tz)
    end = _parse_hhmm(ea_config.get_quiet_hours().get("end", "07:30"), "07:30")
    day = now_local.date()
    if now_local.time() >= end:
        day = date.fromordinal(day.toordinal() + 1)
    return datetime.combine(day, end, tzinfo=tz)


def _load_queue() -> list:
    data = _read_json(QUEUE_FILE)
    return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []


def defer(channel_key: str, text: str, priority: str = "normal") -> None:
    """Hold a post for delivery after quiet hours end."""
    item = {
        "ts_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "channel_key": channel_key,
        "text": text,
        "priority": priority,
    }
    _update_json(QUEUE_FILE, lambda q: q.append(item), [])


def _drop_from_queue(items: list) -> None:
    """Remove `items` from the queue, keeping anything deferred while the
    digest was posting."""
    gone = [json.dumps(i, sort_keys=True) for i in items]

    def _drop(q: list) -> None:
        keep = []
        for i in q:
            key = json.dumps(i, sort_keys=True)
            if key in gone:
                gone.remove(key)
            else:
                keep.append(i)
        q[:] = keep

    _update_json(QUEUE_FILE, _drop, [])


def flush_if_clear(force: bool = False) -> int:
    """If quiet hours have ended (or force), post the held queue as one
    digest per channel and clear it. Returns the number of items flushed."""
    queue = _load_queue()
    if not queue:
        return 0
    if not force and in_quiet_hours():
        return 0

    tz = ZoneInfo(active_timezone())
    qh = ea_config.get_quiet_hours()
    flushed = 0
    sent: list = []

    # Group by channel so each channel gets its own digest.
    channels: dict[str, list] = {}
    for item in queue:
        channels.setdefault(item.get("channel_key", DEFAULT_CHANNEL), []).append(item)

    for channel_key, items in channels.items():
        # Urgent first, then chronological.
        items.sort(key=lambda x: (0 if x.get("priority") == "urgent" else 1,
                                  x.get("ts_utc", "")))
        n = len(items)
        header = (
            f":sunny: *Held while you were offline ({n} item{'s' if n != 1 else ''})*\n"
            f"_Queued during quiet hours "
            f"({qh.get('start')}-{qh.get('end')} {active_timezone()})._"
        )
        parts = [header]
        for item in items:
            try:
                hhmm = datetime.fromisoformat(item["ts_utc"]).astimezone(tz).strftime("%a %H:%M")
            except Exception:
                hhmm = ""
            tag = ":red_circle: " if item.get("priority") == "urgent" else ""
            parts.append(f"{tag}_(held {hhmm})_\n{item['text']}")
        digest = ("\n" + slack_notifier.SPLIT_MARKER + "\n").join(parts)
        ok = slack_notifier.post_to_slack(channel_key, digest, _bypass_quiet=True)
        if ok:
            flushed += n
            sent.extend(items)
        # a failed channel stays queued for the next flush

    if sent:
        _drop_from_queue(sent)
    return flushed


def _write_travel_state(tm: dict) -> None:
    """Persist travel mode to the writable state file."""
    if not resident_io.replace_text(TRAVEL_STATE_FILE, json.dumps(tm, indent=2) + "\n", 0o664):
        raise OSError(f"could not write {TRAVEL_STATE_FILE}")


def main():
    ap = argparse.ArgumentParser(description="Quiet hours / travel mode")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("flush", help="Deliver held queue if quiet hours have ended")
    sub.add_parser("flush-force", help="Deliver held queue now regardless")
    sub.add_parser("status", help="Show active tz, quiet state, queue size")
    ld = sub.add_parser("local-due",
                        help="Exit 0 iff it is --hour o'clock in the active tz "
                             "and --agent has not run yet today (records the run)")
    ld.add_argument("--hour", type=int, required=True)
    ld.add_argument("--agent", required=True)
    st = sub.add_parser("set-travel", help="Enable travel mode")
    st.add_argument("--timezone", required=True, help="IANA tz, e.g. Europe/Lisbon")
    st.add_argument("--until", required=True, help="YYYY-MM-DD auto-revert date")
    sub.add_parser("clear-travel", help="Disable travel mode")
    sh = sub.add_parser("set-hour",
                        help="Move an agent's target local hour for ONE date "
                             "(expires by itself; nothing to revert)")
    sh.add_argument("--agent", required=True)
    sh.add_argument("--date", required=True, help="LOCAL date, YYYY-MM-DD")
    sh.add_argument("--hour", type=int, required=True, help="0-23, local")
    ch = sub.add_parser("clear-hour", help="Drop an agent's hour override")
    ch.add_argument("--agent", required=True)

    args = ap.parse_args()

    if args.command in ("flush", "flush-force"):
        n = flush_if_clear(force=(args.command == "flush-force"))
        print(json.dumps({"flushed": n, "queue_remaining": len(_load_queue())}))
    elif args.command == "local-due":
        sys.exit(0 if due_local(args.agent, args.hour) else 1)
    elif args.command == "status":
        print(json.dumps({
            "active_timezone": active_timezone(),
            "travel_active": _travel_active(),
            "local_hour": local_hour(),
            "hour_overrides": _load_hour_overrides(),
            "morning_briefing_hour_today": _effective_hour(
                "morning-briefing", DEFAULT_AGENT_HOURS["morning-briefing"]),
            "in_quiet_hours": in_quiet_hours(),
            "local_now": datetime.now(ZoneInfo(active_timezone())).strftime("%Y-%m-%d %H:%M %Z"),
            "quiet_hours": ea_config.get_quiet_hours(),
            "travel_mode": _get_travel(),
            "queue_size": len(_load_queue()),
        }, indent=2))
    elif args.command == "set-travel":
        tm = _get_travel()
        tm.update({"enabled": True, "timezone": args.timezone, "until": args.until})
        _write_travel_state(tm)
        print(json.dumps({"travel_mode": tm}))
    elif args.command == "set-hour":
        print(json.dumps({"overrides": set_hour_override(
            args.agent, args.date, args.hour)}, indent=2))
    elif args.command == "clear-hour":
        print(json.dumps({"overrides": clear_hour_override(args.agent)},
                         indent=2))
    elif args.command == "clear-travel":
        tm = _get_travel()
        tm["enabled"] = False
        _write_travel_state(tm)
        print(json.dumps({"travel_mode": tm}))


if __name__ == "__main__":
    main()
