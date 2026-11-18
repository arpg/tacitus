#!/usr/bin/env python3
"""Claude plan usage (session window + weekly) for the reply footer and
the `usage` chat command.

Where the numbers come from
---------------------------
Every Claude run the EA starts prints a `rate_limit_event` with the plan's
windows, and the owner processes that read the run save the latest one
(rate_limits.py). The footer and the report are built from that saved
reading first, so a reply makes no API call, and the long-lived
CLAUDE_CODE_OAUTH_TOKEN, which the usage endpoint refuses, still gets the
percentages. The usage endpoint is the last fallback:

  footer_usage()   the saved reading when it is under STALE_AFTER_S old;
                   else the endpoint, asked at most once per
                   ENDPOINT_RETRY_S (its answer is saved and kept); else
                   the older reading, with its age; else nothing.
  report_usage()   the saved reading when it is under STALE_AFTER_S old;
                   else a one-word Claude run (the `refresh` the bot
                   passes, at most once per REFRESH_RETRY_S), whose stream
                   saves a new reading; else the endpoint; else the older
                   reading, with its age; else an error.

Whose usage
-----------
The chat bot runs as the owner; the EA's Claude runs as the resident user
(ea-bot), on its own Claude account. The quota the EA consumes is the
resident's, so `get_usage()` reports THAT account by default: when the
caller is not the resident, it runs this same script as the resident
(`sudo -n -u ea-bot … --self --json`, the bot's existing privilege) and
returns what it prints. Reading the owner's own credentials from the bot
process reported the owner's other account, whose percentages and reset
times had nothing to do with the EA's work. The saved readings are the
resident's too: every run the EA starts runs as the resident.

  from claude_usage import footer_usage, report_usage, format_usage_footer
  usage = footer_usage()               # saved reading first, no API call per reply
  usage = get_usage()                  # the endpoint, as the resident
  usage = get_usage(account="self")    # the endpoint, this process's own credentials

  python3 claude_usage.py              # report_usage (no refresh run): JSON + footer + report
  python3 claude_usage.py --self --json
  python3 claude_usage.py --self --account --json   # the login's name and plan only
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import rate_limits
except ModuleNotFoundError:  # imported as preprocessor.claude_usage
    from preprocessor import rate_limits

RESIDENT_USER = os.environ.get("EA_USAGE_USER", "ea-bot")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DELEGATE_TIMEOUT_S = 25

# A saved reading younger than this is used as it is.
STALE_AFTER_S = 30 * 60
# The footer names a reading's age once it is older than this.
SHOW_AGE_AFTER_S = 10 * 60
# The footer asks the usage endpoint at most this often (per process).
ENDPOINT_RETRY_S = 15 * 60
# The `usage` command starts a refresh run at most this often.
REFRESH_RETRY_S = 5 * 60


def _home() -> Path:
    """The invoking user's home from passwd, not $HOME: under sudo -u the
    environment can still carry the caller's HOME."""
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


CREDENTIALS_FILE = _home() / ".claude" / ".credentials.json"
ACCOUNT_FILE = _home() / ".claude.json"


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def _resident_home() -> Path | None:
    try:
        return Path(pwd.getpwnam(RESIDENT_USER).pw_dir)
    except KeyError:
        return None


def _load_token() -> str | None:
    """Read the OAuth access token from Claude Code credentials. Without a
    credentials file (macOS keeps logins in the keychain, which the resident
    has none of; docs/macos-claude-binary.md), the long-lived
    CLAUDE_CODE_OAUTH_TOKEN the resident's Claude runs on: from the
    environment, else from ~/.claude/settings.json's env block."""
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        return data["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, PermissionError, KeyError, json.JSONDecodeError) as e:
        err = e
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not tok:
        try:
            env = json.loads((_home() / ".claude" / "settings.json").read_text()).get("env") or {}
            tok = env.get("CLAUDE_CODE_OAUTH_TOKEN", "") if isinstance(env, dict) else ""
        except (OSError, ValueError, AttributeError):
            tok = ""
    if tok:
        return tok
    print(f"claude_usage: could not load token: {err}", file=sys.stderr)
    return None


def account_info() -> dict:
    """{"account": email, "plan": subscription type} for this user's Claude
    login, each "" when unknown. Best-effort; never raises."""
    email = plan = ""
    try:
        acct = json.loads(ACCOUNT_FILE.read_text()).get("oauthAccount") or {}
        email = acct.get("emailAddress") or ""
    except Exception:
        pass
    try:
        plan = json.loads(CREDENTIALS_FILE.read_text())["claudeAiOauth"].get("subscriptionType") or ""
    except Exception:
        pass
    return {"account": email, "plan": plan}


def should_delegate(account: str, current_user: str, resident_home) -> bool:
    """Run as the resident when the resident's usage is wanted and this is
    not the resident's process (and the resident exists on this host)."""
    return (account == "resident" and current_user != RESIDENT_USER
            and resident_home is not None)


def _resident_cmd(*args: str) -> list[str]:
    """This script, run as the resident with `args`: the resident's own
    interpreter (its venv has requests; the bare python3 on a macOS host
    does not)."""
    home = _resident_home()
    script = (home / "ea-system" / "preprocessor" / "claude_usage.py") if home else None
    if script is None or not script.exists():
        script = Path(__file__).resolve()
    py = (home / "ea-system" / ".venv" / "bin" / "python3") if home else None
    py = str(py) if py is not None and py.exists() else "python3"
    return ["sudo", "-n", "-u", RESIDENT_USER, py, str(script), *args]


def _usage_as_resident() -> dict:
    """This script, run as the resident, reading the resident's own token."""
    # From /, which the resident can always enter.
    cmd = _resident_cmd("--self", "--json")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DELEGATE_TIMEOUT_S, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"could not read the {RESIDENT_USER} account's usage ({e})"}
    if r.returncode != 0 or not r.stdout.strip():
        hint = (r.stderr or "").strip()[-200:] or f"exit {r.returncode}"
        return {"error": f"could not read the {RESIDENT_USER} account's usage ({hint})"}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": f"{RESIDENT_USER} usage returned no JSON"}
    return data if isinstance(data, dict) else {"error": "unexpected usage payload"}


def resident_account() -> dict:
    """{"account", "plan"} of the Claude login the EA runs on, read as the
    resident when this process is not the resident. No API call. Empty
    values when unknown; never raises."""
    if not should_delegate("resident", _current_user(), _resident_home()):
        return account_info()
    try:
        r = subprocess.run(_resident_cmd("--self", "--account", "--json"), capture_output=True,
                           text=True, timeout=DELEGATE_TIMEOUT_S, cwd="/")
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"account": str(data.get("account") or "")[:200], "plan": str(data.get("plan") or "")[:60]}


def get_usage(token: str | None = None, account: str = "resident") -> dict:
    """Current claude.ai plan usage.

    account: "resident" (default) is the account the EA's Claude runs as;
    "self" is this process's own credentials. An explicit token always
    means "self".

    Returns dict with keys:
      session_percent_used  (float, 0-100)
      session_reset         (ISO timestamp str)
      daily_percent_used    (float, 0-100)      # the 7-day window
      daily_reset           (ISO timestamp str)
      account, plan         (str; who this usage belongs to)
      error                 (str or None)
    """
    if token is None and should_delegate(account, _current_user(), _resident_home()):
        return _usage_as_resident()
    if not token:
        token = _load_token()
    if not token:
        return {"error": "No OAuth token available", **account_info()}

    try:
        resp = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e), **account_info()}

    five_hour = data.get("five_hour") or {}
    seven_day = data.get("seven_day") or {}

    return {
        "session_percent_used": five_hour.get("utilization"),
        "session_reset": five_hour.get("resets_at"),
        "daily_percent_used": seven_day.get("utilization"),
        "daily_reset": seven_day.get("resets_at"),
        "error": None,
        **account_info(),
    }


# ── the saved reading first, the endpoint last ───────────────────────

# The two windows the footer shows, and the get_usage keys they fill.
_MAIN_WINDOWS = {"five_hour": ("session_percent_used", "session_reset"),
                 "seven_day": ("daily_percent_used", "daily_reset")}
_ENDPOINT_MEMO: dict = {}
_REFRESH_MEMO: dict = {}
_MEMO_LOCK = threading.Lock()


def _parse_time(iso_str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(iso_str))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def usage_from_snapshot(entry: dict, now: datetime | None = None) -> dict:
    """get_usage()'s shape from a saved reading (rate_limits.load), plus:
      source       "run" (a Claude run's stream) or "endpoint"
      as_of        when the reading was taken (ISO)
      age_s        its age in seconds
      ended        names of the main windows that reset since the reading
                   (their percentage is unknown now, so it is None)
      other_windows [{"name", "percent_used", "resets_at", "status"}]
      limited      True when the reading said the plan's limit is reached
    """
    now = now or datetime.now(timezone.utc)
    out = {"session_percent_used": None, "session_reset": "",
           "daily_percent_used": None, "daily_reset": "",
           "error": None, "account": entry.get("login") or "", "plan": entry.get("plan") or "",
           "source": "endpoint" if entry.get("source") == "endpoint" else "run",
           "as_of": entry["ts"].isoformat(), "age_s": int(rate_limits.age_seconds(entry, now) or 0),
           "ended": [], "other_windows": [], "limited": False}
    for name, w in entry.get("windows", {}).items():
        reset = w.get("resets_at") or ""
        when = _parse_time(reset) if reset else None
        ended = when is not None and when <= now
        if w.get("status") == "rejected" and not ended:
            out["limited"] = True
        if name in _MAIN_WINDOWS:
            pct_key, reset_key = _MAIN_WINDOWS[name]
            out[reset_key] = reset
            if ended:
                out["ended"].append(name)
            else:
                out[pct_key] = w["percent_used"]
        elif not ended:
            out["other_windows"].append({"name": name, "percent_used": w["percent_used"],
                                         "resets_at": reset, "status": w.get("status") or ""})
    if not any(w.get("status") for w in entry.get("windows", {}).values()) \
            and entry.get("status") == "rejected" and not out["ended"]:
        out["limited"] = True
    return out


def _may_start(memo: dict, gap_s: float) -> bool:
    """True, and the time noted, when nothing was started from `memo` in
    the last `gap_s` seconds."""
    with _MEMO_LOCK:
        t = time.monotonic()
        last = memo.get("at")
        if last is not None and t - last < gap_s:
            return False
        memo["at"] = t
        return True


def _endpoint_reading(fetch, path, now) -> tuple[dict | None, str]:
    """(saved-reading shape, "") from the usage endpoint, or (None, the
    error). The reading is saved for the next footer."""
    try:
        usage = fetch()
    except Exception as e:  # noqa: BLE001
        return None, str(e) or "usage check failed"
    if not isinstance(usage, dict):
        return None, "unexpected usage payload"
    if usage.get("error"):
        return None, str(usage["error"])
    parsed = rate_limits.from_usage(usage)
    if parsed is None:
        return None, "the usage check returned no percentages"
    rate_limits.save(parsed, source="endpoint", path=path, now=now)
    return {"ts": now, "source": "endpoint", "status": "", **parsed}, ""


def footer_usage(*, fetch=None, path=None, now: datetime | None = None,
                 memo: dict | None = None) -> dict:
    """The usage a reply footer shows, with no API call per reply: the
    saved reading when it is fresh; else the endpoint, at most once per
    ENDPOINT_RETRY_S (its answer is kept for the gap); else the older
    reading, with its age; else {"error"}. Never raises."""
    try:
        memo = _ENDPOINT_MEMO if memo is None else memo
        when = now or datetime.now(timezone.utc)
        snap = rate_limits.load(path=path)
        age = rate_limits.age_seconds(snap, when)
        if snap is not None and age <= STALE_AFTER_S:
            return usage_from_snapshot(snap, when)
        if _may_start(memo, ENDPOINT_RETRY_S):
            reading, err = _endpoint_reading(fetch or get_usage, path, when)
            memo["reading"], memo["error"] = reading, err
        reading = memo.get("reading")
        if reading is not None and (snap is None or reading["ts"] >= snap["ts"]):
            return usage_from_snapshot(reading, when)
        if snap is not None:
            return usage_from_snapshot(snap, when)
        return {"error": memo.get("error") or "no usage reading yet"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e) or "usage failed"}


def report_usage(*, refresh=None, fetch=None, account_lookup=None, path=None,
                 now: datetime | None = None, memo: dict | None = None) -> dict:
    """The usage the `usage` command reports. In order: the saved reading
    when it is fresh; a refresh run (`refresh`, a callable that starts a
    one-word Claude run whose stream saves a new reading; at most once per
    REFRESH_RETRY_S, and only here, since it spends quota); the usage
    endpoint; the older reading, with its age; else {"error"}. A saved
    reading gets the login's name from `account_lookup`
    (resident_account by default). Never raises."""
    try:
        memo = _REFRESH_MEMO if memo is None else memo
        lookup = account_lookup or resident_account

        def _clock() -> datetime:
            return now or datetime.now(timezone.utc)

        def _named(entry: dict) -> dict:
            out = usage_from_snapshot(entry, _clock())
            if not out.get("account"):
                try:
                    out.update({k: v for k, v in (lookup() or {}).items()
                                if k in ("account", "plan") and v})
                except Exception:  # noqa: BLE001
                    pass
            return out

        snap = rate_limits.load(path=path)
        if snap is not None and rate_limits.age_seconds(snap, _clock()) <= STALE_AFTER_S:
            return _named(snap)
        if refresh is not None and _may_start(memo, REFRESH_RETRY_S):
            try:
                refresh()
            except Exception:  # noqa: BLE001
                pass
            fresh = rate_limits.load(path=path)
            if fresh is not None and rate_limits.age_seconds(fresh, _clock()) <= STALE_AFTER_S:
                return _named(fresh)
            snap = fresh or snap
        reading, err = _endpoint_reading(fetch or get_usage, path, _clock())
        if reading is not None:
            return _named(reading)
        if snap is not None:
            return _named(snap)
        return {"error": err or "no usage reading yet"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e) or "usage failed"}


def format_age(seconds) -> str:
    """'just now', '12m ago', '3h ago', '3h 20m ago' or '2d ago'."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "a while ago"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m ago" if m and h < 6 else f"{h}h ago"
    return f"{s // 86400}d ago"


def _relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to relative string like '3h 12m' or '2d 4h'."""
    try:
        reset = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = reset - now
        total_sec = int(delta.total_seconds())

        if total_sec <= 0:
            return "now"

        hours, remainder = divmod(total_sec, 3600)
        minutes = remainder // 60

        days, rem_hours = divmod(hours, 24)
        if days > 0:
            if rem_hours > 0:
                return f"{days}d {rem_hours}h"
            return f"{days}d"

        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "unknown"


def _absolute_time(iso_str: str) -> str:
    """Convert ISO timestamp to local time like '6:30 PM' (with the weekday
    when it is not today)."""
    try:
        reset = datetime.fromisoformat(iso_str).astimezone()
        today = datetime.now(reset.tzinfo).date()
        clock = reset.strftime("%-I:%M %p")
        if reset.date() == today:
            return clock
        return f"{reset.strftime('%a')} {clock}"
    except Exception:
        return ""


def _window_label(name: str) -> str:
    """A plain label for a window name the report does not know by heart."""
    known = {"five_hour": "Session (5h rolling)", "seven_day": "Weekly (7d rolling)",
             "seven_day_opus": "Weekly, Opus models", "seven_day_sonnet": "Weekly, Sonnet models"}
    return known.get(name) or name.replace("_", " ").capitalize()


def format_usage_footer(usage: dict, elapsed_sec: float, label: str = "EA") -> str:
    """Format usage into a chat footer line; `label` names the lane
    ("EA resident" for the resident pane).

    Returns e.g.:
      '\n\n---\n_EA: 45s · 10% session (resets in 3h 12m) · 12% weekly (resets in 2d 4h)_'
    A saved reading older than SHOW_AGE_AFTER_S adds its age
    ('· checked 2h ago'). On error, or with no window to show, just the
    elapsed time.
    """
    base = f"{label}: {elapsed_sec:.0f}s"

    if usage.get("error"):
        return f"\n\n---\n_{base}_"

    parts = [base]

    sess = usage.get("session_percent_used")
    if sess is not None:
        reset = usage.get("session_reset") or ""
        parts.append(f"{sess:.0f}% session" + (f" (resets in {_relative_time(reset)})" if reset else ""))

    weekly = usage.get("daily_percent_used")
    if weekly is not None:
        reset = usage.get("daily_reset") or ""
        parts.append(f"{weekly:.0f}% weekly" + (f" (resets in {_relative_time(reset)})" if reset else ""))

    age = usage.get("age_s")
    if len(parts) > 1 and isinstance(age, (int, float)) and age > SHOW_AGE_AFTER_S:
        parts.append(f"checked {format_age(age)}")

    return f"\n\n---\n_{' · '.join(parts)}_"


def _report_window(name: str, pct, reset: str) -> str:
    line = f"*{_window_label(name)}:* {pct:.0f}% used"
    if reset:
        line += f" · resets {_absolute_time(reset)} (in {_relative_time(reset)})"
    return line


def format_usage_report(usage: dict) -> str:
    """Format a detailed usage report for the 'usage' chat command. Names
    the account, so a number that looks wrong can be checked against the
    right Claude login, and says how old a saved reading is."""
    if usage.get("error"):
        return (":warning: I could not read the Claude usage. No recent Claude run "
                f"reported it, and the usage check failed: {usage['error']}")

    lines = [":bar_chart: *Claude Usage*", ""]

    who = usage.get("account") or ""
    plan = usage.get("plan") or ""
    if who or plan:
        label = who + (f" ({plan})" if plan else "")
        lines.append(f"*Account:* {label} · the login the EA runs on")
    elif "age_s" in usage:
        lines.append("*Account:* the Claude login the EA runs on")

    ended = usage.get("ended") or []
    for name, (pct_key, reset_key) in _MAIN_WINDOWS.items():
        pct = usage.get(pct_key)
        reset = usage.get(reset_key) or ""
        if pct is not None:
            lines.append(_report_window(name, pct, reset))
        elif name in ended:
            lines.append(f"*{_window_label(name)}:* it reset at {_absolute_time(reset)}. "
                         "No newer reading yet.")

    for w in usage.get("other_windows") or []:
        lines.append(_report_window(w["name"], w["percent_used"], w.get("resets_at") or ""))

    if usage.get("limited"):
        lines.append(":warning: The plan's limit is reached. Claude cannot answer until it resets.")

    lines.append("")
    age = usage.get("age_s")
    if not isinstance(age, (int, float)):
        lines.append("_Checked just now_")
    elif usage.get("source") == "endpoint":
        lines.append(f"_Checked {format_age(age)}_")
    else:
        lines.append(f"_From the EA's last Claude run, {format_age(age)}_")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude plan usage")
    ap.add_argument("--self", dest="self_only", action="store_true",
                    help="this process's own credentials (no delegation)")
    ap.add_argument("--account", action="store_true",
                    help="only the login's name and plan (no API call)")
    ap.add_argument("--json", action="store_true", help="JSON only")
    a = ap.parse_args()
    if a.account:
        info = account_info() if a.self_only else resident_account()
        print(json.dumps(info))
        return 0
    # Without --self: the report's own order, with no refresh run (a
    # refresh spends quota; only the chat command starts one).
    usage = get_usage(account="self") if a.self_only else report_usage()
    if a.json:
        print(json.dumps(usage))
        return 0 if not usage.get("error") else 1
    print(json.dumps(usage, indent=2))
    print()
    print("Footer:", format_usage_footer(usage, 12.3))
    print()
    print("Report:")
    print(format_usage_report(usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
