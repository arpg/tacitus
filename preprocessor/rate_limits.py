"""The Claude plan's usage windows, read from the runs the EA starts.

Every `claude -p --output-format stream-json --verbose` run prints a
`rate_limit_event` line before its reply. Claude Code 2.1 writes:

  {"type": "rate_limit_event",
   "rate_limit_info": {"status": "allowed", "resetsAt": 1795029000,
                       "rateLimitType": "five_hour",
                       "unifiedWindows": {
                         "five_hour": {"utilization": 0.12, "resetsAt": 1795029000,
                                       "status": "allowed"},
                         "seven_day": {"utilization": 0.37, "resetsAt": 1795338000,
                                       "status": "allowed"}}}}

`utilization` is a fraction (0.12 is 12%) and `resetsAt` is in epoch
seconds. Other window names can appear beside these two; each one is kept.
An event with no `unifiedWindows` but a top-level `rateLimitType` and
`utilization` gives that one window. Any other shape gives no reading.

The owner processes that read a run's stream (the chat bot for the chat
lanes, log_claude_cost.py for the cron and plugin pipes) save the latest
reading here with `note_stream_event`. claude_usage.py builds the reply
footer and the `usage` report from it, so a reply makes no call to the
usage endpoint, and the long-lived CLAUDE_CODE_OAUTH_TOKEN, which that
endpoint refuses, still gets the percentages.

The file is `claude-rate-limits.json` in the owner-private dir, which the
resident cannot reach, written only by an owner process:

  {"version": 1,
   "accounts": {"resident": {"ts": "<when the reading was taken, UTC ISO>",
                             "source": "chat:mono" | "cron:<agent>" | "endpoint" | ...,
                             "status": "allowed",
                             "windows": {"five_hour": {"percent_used": 12.0,
                                                       "resets_at": "<UTC ISO>",
                                                       "status": "allowed"}, ...},
                             "login": "<email>", "plan": "<plan>"}}}

`resident` is the Claude login the EA runs on (ea-bot). Every run the EA
starts uses it; `owner` is kept for a reading of the owner's own login.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    import owner_trust
except ModuleNotFoundError:  # imported as preprocessor.rate_limits
    from preprocessor import owner_trust

FILE_NAME = "claude-rate-limits.json"
EVENT_TYPE = "rate_limit_event"
ACCOUNTS = ("resident", "owner")
# Names and statuses are written into chat text, so only plain tokens pass.
_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
_STATUS = re.compile(r"^[a-z_]{1,32}$")
MAX_WINDOWS = 8
# A percentage above this is not a reading.
MAX_PERCENT = 1000.0


def snapshot_path() -> Path:
    return owner_trust.private_dir() / FILE_NAME


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _iso(v) -> str:
    """A reset time as a UTC ISO string: from epoch seconds, epoch
    milliseconds or an ISO string. "" when it is none of these."""
    n = _num(v)
    if n is not None:
        if n > 1e11:
            n /= 1000.0
        if n <= 0:
            return ""
        try:
            return datetime.fromtimestamp(n, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    return ""


def _status(v) -> str:
    return v if isinstance(v, str) and _STATUS.match(v) else ""


def _window(raw) -> dict | None:
    """One window of an event, or None when it has no usable utilization."""
    if not isinstance(raw, dict):
        return None
    u = _num(raw.get("utilization"))
    if u is None or u < 0:
        return None
    pct = u * 100.0
    if pct > MAX_PERCENT:
        return None
    reset = raw.get("resetsAt", raw.get("resets_at"))
    return {"percent_used": round(pct, 2), "resets_at": _iso(reset),
            "status": _status(raw.get("status"))}


def parse_event(event) -> dict | None:
    """{"windows": {name: window}, "status": str} from a rate_limit_event,
    or None when the event carries no usable window. Never raises."""
    try:
        if not isinstance(event, dict) or event.get("type") != EVENT_TYPE:
            return None
        info = event.get("rate_limit_info", event.get("rateLimitInfo"))
        if not isinstance(info, dict):
            return None
        windows: dict = {}
        raw = info.get("unifiedWindows", info.get("unified_windows"))
        if isinstance(raw, dict):
            for name, w in raw.items():
                if len(windows) >= MAX_WINDOWS:
                    break
                if isinstance(name, str) and _NAME.match(name):
                    win = _window(w)
                    if win:
                        windows[name] = win
        if not windows:
            name = info.get("rateLimitType")
            if isinstance(name, str) and _NAME.match(name):
                win = _window(info)
                if win:
                    windows[name] = win
        if not windows:
            return None
        return {"windows": windows, "status": _status(info.get("status"))}
    except Exception:
        return None


def from_usage(usage) -> dict | None:
    """The same reading from claude_usage.get_usage()'s answer (the usage
    endpoint, which states percentages), or None when it has none."""
    if not isinstance(usage, dict) or usage.get("error"):
        return None
    windows = {}
    for name, pct_key, reset_key in (("five_hour", "session_percent_used", "session_reset"),
                                     ("seven_day", "daily_percent_used", "daily_reset")):
        pct = _num(usage.get(pct_key))
        if pct is None or pct < 0 or pct > MAX_PERCENT:
            continue
        windows[name] = {"percent_used": round(pct, 2),
                         "resets_at": _iso(usage.get(reset_key)), "status": ""}
    if not windows:
        return None
    return {"windows": windows, "status": "",
            "login": str(usage.get("account") or "")[:200],
            "plan": str(usage.get("plan") or "")[:60]}


def _read(path: Path) -> dict:
    try:
        data = json.loads(owner_trust.read_owner_file(path))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _entry(parsed: dict, source: str, now: datetime) -> dict:
    entry = {"ts": now.astimezone(timezone.utc).isoformat(), "source": str(source)[:64],
             "status": parsed.get("status") or "", "windows": parsed["windows"]}
    for key in ("login", "plan"):
        if parsed.get(key):
            entry[key] = str(parsed[key])[:200]
    return entry


def save(parsed, *, source: str = "", account: str = "resident", path=None,
         now: datetime | None = None) -> bool:
    """Save a reading as the account's latest. Without `path`, only an
    owner process saves, to the owner-private file. True when saved;
    never raises: a reading must not break a run."""
    try:
        if account not in ACCOUNTS or not isinstance(parsed, dict) \
                or not isinstance(parsed.get("windows"), dict) or not parsed["windows"]:
            return False
        if path is None:
            if not owner_trust.is_owner_process():
                return False
            path = snapshot_path()
        path = Path(path)
        now = now or datetime.now(timezone.utc)
        old = _read(path).get("accounts")
        accounts = {k: v for k, v in (old if isinstance(old, dict) else {}).items()
                    if k in ACCOUNTS and k != account}
        accounts[account] = _entry(parsed, source, now)
        body = json.dumps({"version": 1, "accounts": accounts}, indent=1) + "\n"
        owner_trust.write_owner_file(path, body, mode=0o600)
        return True
    except Exception:
        return False


def note_stream_event(event, *, source: str = "", account: str = "resident",
                      path=None) -> bool:
    """Save the reading in one stream-json event when it is a
    rate_limit_event that carries one. Any other event is ignored. True
    when a reading was saved; never raises."""
    if not isinstance(event, dict) or event.get("type") != EVENT_TYPE:
        return False
    parsed = parse_event(event)
    return save(parsed, source=source, account=account, path=path) if parsed else False


def _clean_window(w) -> dict | None:
    if not isinstance(w, dict):
        return None
    pct = _num(w.get("percent_used"))
    if pct is None or pct < 0 or pct > MAX_PERCENT:
        return None
    return {"percent_used": pct, "resets_at": _iso(w.get("resets_at")),
            "status": _status(w.get("status"))}


def load(account: str = "resident", path=None) -> dict | None:
    """The account's latest saved reading, checked again on the way in:
    {"ts": aware datetime, "source", "status", "windows", "login", "plan"}.
    None when there is none or it does not have that shape. Never raises."""
    try:
        data = _read(Path(path) if path is not None else snapshot_path())
        accounts = data.get("accounts")
        raw = accounts.get(account) if isinstance(accounts, dict) else None
        if not isinstance(raw, dict):
            return None
        ts = datetime.fromisoformat(str(raw.get("ts") or ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        windows = {}
        raw_windows = raw.get("windows")
        for name, w in (raw_windows.items() if isinstance(raw_windows, dict) else ()):
            if isinstance(name, str) and _NAME.match(name) and len(windows) < MAX_WINDOWS:
                clean = _clean_window(w)
                if clean:
                    windows[name] = clean
        if not windows:
            return None
        return {"ts": ts, "source": str(raw.get("source") or "")[:64],
                "status": _status(raw.get("status")), "windows": windows,
                "login": str(raw.get("login") or "")[:200], "plan": str(raw.get("plan") or "")[:60]}
    except Exception:
        return None


def age_seconds(entry, now: datetime | None = None) -> float | None:
    """Seconds since the reading was taken; None without a reading. A
    reading stamped in the future counts as taken now."""
    if not isinstance(entry, dict) or not isinstance(entry.get("ts"), datetime):
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - entry["ts"]).total_seconds())
