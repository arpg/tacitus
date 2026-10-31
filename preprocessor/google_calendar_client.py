"""
Google Calendar API client for the EA preprocessor.
Supports multiple personal Google accounts with separate OAuth2 tokens.

Setup (one-time):
1. Go to https://console.cloud.google.com/
2. Create a project (e.g., "EA Assistant")
3. Enable the Google Calendar API:
   APIs & Services → Library → search "Google Calendar API" → Enable
4. Create OAuth credentials:
   APIs & Services → Credentials → Create Credentials → OAuth client ID
   Application type: Desktop app
   Name: "EA Assistant"
5. Download the client secret JSON → save as ~/.ea/auth/google_client_secret.json
6. Run: python google_calendar_client.py --setup
   This opens a browser for each Google account. Sign in and authorize.
   Tokens are saved per-account and refreshed automatically.

Usage:
    from google_calendar_client import get_all_events

    # Get events from all configured accounts
    events = get_all_events(days_ahead=14)

Free/busy is NOT computed here. Availability has exactly one model, in
scheduling_engine.compute_free_slots, which reads the tokened preference
events off the EA-Managed calendar.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    import owner_trust as _owner_trust
    import published_apps as _published
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import published_apps as _published


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
AUTH_DIR = EA_DIR / "auth"


def _checked_token_io() -> bool:
    """True when token files go through the owner checks: every process but
    the resident's own and one on a scratch EA tree (a test). An owner
    process on the install tree is checked whatever
    owner_trust.is_owner_process() says, so a missing private directory
    cannot switch the checks off. An unknown answer counts as checked."""
    try:
        import pwd
        if os.geteuid() == pwd.getpwnam("ea-bot").pw_uid:
            return False
    except KeyError:
        pass
    except Exception:
        return True
    try:
        return (os.path.realpath(_owner_trust.ea_dir())
                == os.path.realpath(_owner_trust.install_ea_dir()))
    except Exception:
        return True


# The auth directory is the owner's, provisioned at install; only the plain
# branch (the resident, a scratch tree) creates it.
if not _checked_token_io():
    AUTH_DIR.mkdir(parents=True, exist_ok=True)

CLIENT_SECRET_FILE = AUTH_DIR / "google_client_secret.json"

# Full calendar access (read + write) — needed for creating events.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Accounts to authenticate — configured in scheduling-preferences.json
# Each gets its own token file: ~/.ea/auth/google_token_<email>.json
CONFIG_FILE = EA_DIR / "config" / "scheduling-preferences.json"


def _get_google_accounts() -> list[dict]:
    """Read Google Calendar accounts from scheduling preferences."""
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text())
        calendars = config.get("calendars", {})
        return [
            {
                "email": cal["email"],
                "key": key,
                "blocks_availability": cal.get("blocks_availability", True),
            }
            for key, cal in calendars.items()
            if cal.get("type") == "google_calendar_mcp"
        ]
    return []


def _token_path(email: str) -> Path:
    """Per-account token file path."""
    safe_name = email.replace("@", "_at_").replace(".", "_")
    return AUTH_DIR / f"google_token_{safe_name}.json"


# -------------------------------------------------------------------
# Token files in the auth directory
# -------------------------------------------------------------------
# ~/.ea/auth sits under a directory ea-bot owns, so ea-bot can rename it away
# and put its own in its place. Unless _checked_token_io() says otherwise, a
# token file is read and written only after the owner checks on the directory
# and the file, made on the open descriptors; a failed check raises
# OwnerTrustError. Token files stay 0660 in the owner's own group, which the
# checks accept only while that group holds no other user. The resident and a
# scratch tree keep plain path reads and writes.
def _load_json_file(path: Path) -> dict:
    """A JSON file from the auth directory, read through owner_trust."""
    return json.loads(_owner_trust.read_owner_file(path, allow_private_group=True))


def _load_token(token_file: Path) -> Credentials:
    if _checked_token_io():
        return Credentials.from_authorized_user_info(_load_json_file(token_file), SCOPES)
    return Credentials.from_authorized_user_file(str(token_file), SCOPES)


def _save_token(token_file: Path, creds: Credentials) -> None:
    if _checked_token_io():
        # The token file rule (owner_trust.token_file_mode): 0600 under mail
        # mediation, 0640 group ea without it. Not 0660: the owner checks
        # refuse group write when the group holds another user, as ea does,
        # and as every group does on macOS, where a new file takes its
        # directory's group. A 0660 token there failed on its next read.
        mode, gid = _owner_trust.token_file_mode()
        _owner_trust.write_owner_file(token_file, creds.to_json(), mode=mode, gid=gid,
                                      allow_private_group=True)
        return
    token_file.write_text(creds.to_json())


def _client_source() -> str:
    """The client a new sign-in uses: "file" (the user's own client secret,
    which always wins), "published" (the project's desktop client,
    docs/published-oauth-apps.md) or "" (neither)."""
    return _published.google_client_source(CLIENT_SECRET_FILE)


def _client_flow() -> InstalledAppFlow:
    if _client_source() == "published":
        return InstalledAppFlow.from_client_config(_published.google_client_config(), SCOPES)
    if _checked_token_io():
        return InstalledAppFlow.from_client_config(_load_json_file(CLIENT_SECRET_FILE), SCOPES)
    return InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)


# -------------------------------------------------------------------
# Authentication
# -------------------------------------------------------------------
def authenticate_account(email: str, interactive: bool = True) -> Optional[Credentials]:
    """
    Get valid credentials for a Google account.
    On first run (or expired refresh token), opens browser if interactive=True.
    """
    token_file = _token_path(email)
    creds = None

    if token_file.exists():
        try:
            creds = _load_token(token_file)
        except _owner_trust.OwnerTrustError as e:
            print(f"Google token refused for {email}: {e}", file=sys.stderr)
            return None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"Token refresh failed for {email}: {e}", file=sys.stderr)
            if not interactive:
                return None
            creds = None
        else:
            try:
                _save_token(token_file, creds)
            except (OSError, _owner_trust.OwnerTrustError) as e:
                print(f"Could not save refreshed token for {email} (non-fatal): {e}", file=sys.stderr)
            if not _checked_token_io():
                try:
                    os.chmod(token_file, 0o660)
                except OSError:
                    pass  # the owner cannot chmod ea-bot's files
            return creds

    if not interactive:
        print(f"No valid token for {email} and not in interactive mode.", file=sys.stderr)
        return None

    if not _client_source():
        print(
            f"ERROR: Google client secret not found at {CLIENT_SECRET_FILE}\n"
            f"Download it from Google Cloud Console → APIs & Services → Credentials\n"
            f"See the setup instructions in this file's docstring.",
            file=sys.stderr,
        )
        return None

    print(f"\nAuthenticating Google account: {email}", file=sys.stderr)

    flow = _client_flow()

    # Copy-paste flow: works on remote machines where localhost redirect fails.
    # User completes sign-in in browser; the redirect to localhost will fail to
    # load (that's OK). They copy the full URL from the address bar and paste it.
    # oauthlib refuses HTTP redirect URIs unless this is set
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    flow.redirect_uri = "http://localhost:8400"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print(f"\nOpen this URL in your browser:\n{auth_url}\n", file=sys.stderr)
    print("After signing in, your browser will redirect to http://localhost:8400/?code=...", file=sys.stderr)
    print("That page will fail to load — that's OK.", file=sys.stderr)
    print("Copy the FULL URL from your browser's address bar and paste it below.", file=sys.stderr)
    redirect_url = input("Paste the full redirect URL: ").strip()
    flow.fetch_token(authorization_response=redirect_url)
    creds = flow.credentials

    _save_token(token_file, creds)
    if not _checked_token_io():
        try:
            os.chmod(token_file, 0o660)
        except OSError:
            pass  # File may be owned by ea-bot; group rw is fine
    print(f"Token saved to {token_file}", file=sys.stderr)
    return creds


def _build_service(email: str, interactive: bool = False):
    """Build a Google Calendar API service for an account."""
    creds = authenticate_account(email, interactive=interactive)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# -------------------------------------------------------------------
# Calendar queries
# -------------------------------------------------------------------

# Per-fetch error journal. Reset by get_all_events(); populated whenever an
# account's fetch fails. Callers copy it into the briefing as
# `calendar_fetch_errors`, so a dead account reads as "calendar source FAILED"
# instead of silently as "completely free".
LAST_FETCH_ERRORS: list = []


def _mirror_source(item: dict) -> str:
    """The source key an [auto] mirror names in its description
    ("source: outlook/<id>"), or "" for any other event."""
    if "[auto]" not in (item.get("summary") or ""):
        return ""
    for line in (item.get("description") or "").splitlines():
        if line.startswith("source: "):
            return line[len("source: "):].strip()
    return ""


def get_events(
    email: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    days_ahead: int = 14,
    calendar_id: str = "primary",
) -> list:
    """
    Get calendar events for a single Google account.
    Returns list of event dicts with normalized keys matching the
    preprocessor's expected format. `calendar_id` selects a specific
    calendar within the account (default the account primary); pass the
    EA-Managed calendar id to read the EA's own holds.
    """
    service = _build_service(email)
    if not service:
        print(f"WARNING: Could not connect to Google Calendar for {email}", file=sys.stderr)
        LAST_FETCH_ERRORS.append({"account": email,
                                  "error": "could not build service (auth/token failure)"})
        return []

    if start is None:
        start = datetime.now(timezone.utc)
    if end is None:
        end = start + timedelta(days=days_ahead)

    try:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=500,
        ).execute()

        events = []
        for item in result.get("items", []):
            # Handle all-day events vs timed events
            start_raw = item.get("start", {})
            end_raw = item.get("end", {})

            if "dateTime" in start_raw:
                event_start = start_raw["dateTime"]
                event_end = end_raw.get("dateTime", event_start)
                is_all_day = False
            elif "date" in start_raw:
                event_start = start_raw["date"] + "T00:00:00"
                event_end = end_raw.get("date", start_raw["date"]) + "T23:59:59"
                is_all_day = True
            else:
                continue

            events.append({
                "subject": item.get("summary", "(no title)"),
                "start": {"dateTime": event_start},
                "end": {"dateTime": event_end},
                "location": {"displayName": item.get("location", "")},
                "attendees": [
                    a.get("email", "") for a in item.get("attendees", [])
                ],
                # The ids calendar_reconciler keys a mirror on
                # ("<calendar>/<series or event id>"), and on a mirror the
                # key of the event it reflects: how the solver finds the
                # level an event's mirror declares.
                "id": item.get("id", ""),
                "recurring_event_id": item.get("recurringEventId", ""),
                "calendar_id": email if calendar_id == "primary" else calendar_id,
                "mirror_of": _mirror_source(item),
                "source_calendar": email,
                "source_type": "google",
                "is_all_day": is_all_day,
                "status": item.get("status", "confirmed"),
                "response_status": next(
                    (
                        a.get("responseStatus", "")
                        for a in item.get("attendees", [])
                        if a.get("self")
                    ),
                    "accepted",
                ),
            })

        return events

    except HttpError as e:
        print(f"Google Calendar API error for {email}: {e}", file=sys.stderr)
        # Status+reason only, never str(e): the full HttpError can carry
        # response headers and body, and this string flows into the briefing
        # JSON, the LLM context, and ultimately Slack.
        status = getattr(getattr(e, "resp", None), "status", "?")
        reason = getattr(getattr(e, "resp", None), "reason", "") or "API error"
        LAST_FETCH_ERRORS.append({"account": email,
                                  "error": f"HTTP {status} {reason}"[:120]})
        return []


def get_all_events(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    days_ahead: int = 14,
) -> list:
    """
    Get events from ALL configured Google Calendar accounts.
    Returns a merged list sorted by start time.
    """
    accounts = _get_google_accounts()
    all_events = []
    LAST_FETCH_ERRORS.clear()  # fresh journal per merged fetch

    for account in accounts:
        events = get_events(account["email"], start, end, days_ahead)
        all_events.extend(events)
        print(
            f"  Google Calendar {account['email']}: {len(events)} events",
            file=sys.stderr,
        )

    # EA-Managed calendar: a secondary calendar under the primary account where
    # the EA auto-creates its own attendee-less holds. Merge it as an ALWAYS-
    # blocking source (tagged 'ea-managed', which is never in the non-blocking
    # set) so the scheduler never proposes a time over the EA's own hold.
    ea = ea_managed_target()
    if ea:
        try:
            holds = get_events(ea["email"], start, end, days_ahead,
                               calendar_id=ea["calendar_id"])
            for h in holds:
                h["source_calendar"] = "ea-managed"
                h["ea_managed_hold"] = True
            all_events.extend(holds)
            print(f"  EA-Managed calendar: {len(holds)} holds", file=sys.stderr)
        except Exception as e:
            LAST_FETCH_ERRORS.append({"account": "ea-managed", "error": str(e)})

    all_events.sort(key=lambda e: e["start"]["dateTime"])
    return all_events


def _ea_managed_source() -> Optional[dict]:
    """The EA-Managed calendar (email + calendar_id) from scheduling
    preferences, or None if not configured. Kept OUT of the regular accounts
    list so it is not double-fetched as a primary calendar."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except Exception:
        return None
    ea = cfg.get("ea_managed_calendar")
    if isinstance(ea, dict) and ea.get("email") and ea.get("calendar_id"):
        return {"email": ea["email"], "calendar_id": ea["calendar_id"]}
    return None


def _mail_mediation():
    try:
        import mail_mediation
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import mail_mediation
    return mail_mediation


def ea_managed_target() -> Optional[dict]:
    """The EA-Managed calendar this process may write (email + calendar_id),
    or None.

    Owner side (mail_mediation.owner_side): the calendar id from the
    owner-private calendars.json and nothing else, so None without it and
    no write happens. The account is calendars.json's `ea_managed_account`;
    a file seeded before that key existed falls back to the resident's
    config for the account only. The account picks which of the owner's
    tokens is used; the calendar written is the owner's id either way.
    Resident side: scheduling-preferences.json (_ea_managed_source), a hint
    for wording and pre-checks; the mediator decides."""
    mm = _mail_mediation()
    if not mm.owner_side():
        return _ea_managed_source()
    owner = mm.owner_calendars()
    cal = str(owner.get("ea_managed_calendar_id") or "").strip()
    if not cal:
        return None
    email = str(owner.get("ea_managed_account") or "").strip()
    if not email:
        email = (_ea_managed_source() or {}).get("email", "")
    if not email:
        return None
    return {"email": email, "calendar_id": cal}


# -------------------------------------------------------------------
# Write operations
# -------------------------------------------------------------------
# Tokened EA-Managed events are colored by polarity so the calendar reads at a
# glance: GREEN = available frame (wh), RED = hard (nm/p0), AMBER = soft (p1/p2),
# GRAPHITE = informational (wr). Google colorIds: 10 Basil, 11 Tomato, 6
# Tangerine, 8 Graphite.
_TOKEN_COLOR = {"wh": "10", "nm": "11", "p0": "11",
                "p1": "6", "p2": "6", "wr": "8"}


def _token_color(summary: str) -> Optional[str]:
    """Google colorId for a tokened event title, or None for a plain event."""
    s = (summary or "").strip()
    head = s.split(None, 1)[0].lower() if s else ""
    return _TOKEN_COLOR.get(head)


def _zone_name(tz: str = "") -> str:
    """`tz` when given, else the user's zone (user_tz.py)."""
    if tz:
        return tz
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return user_tz.name()


# --- The library gate -------------------------------------------------------
# create_event, _patch_event and _remove_event write a calendar. Each one
# checks for itself, like graph_client's library gate, so a direct call
# cannot write a calendar the CLI would refuse: the target is the EA-Managed
# calendar (this process's id, the owner-private one on the owner side), or
# the call carries a process-local approval the CLI minted after it consumed
# the button's one-time token. The approval lives only in this process's
# memory, is bound to (action, account, calendar, event id or title), and
# is spent on first use.
_INPROC_PREFIX = "gcal-approved:"
_INPROC_APPROVALS: dict[str, tuple[str, str, str, str]] = {}


def issue_inprocess_approval(action: str, email: str, calendar_id: str, ident: str) -> str:
    """One process-local approval for one library write ("create", "update"
    or "delete"). Call ONLY after a button token was validated and consumed
    (cmd_create_event, cmd_update_event, cmd_delete_event)."""
    import secrets
    token = _INPROC_PREFIX + secrets.token_urlsafe(24)
    _INPROC_APPROVALS[token] = (action, email, calendar_id, ident)
    return token


def _approved(approval: str, action: str, email: str, calendar_id: str, ident: str) -> bool:
    if not approval or not approval.startswith(_INPROC_PREFIX):
        return False
    return _INPROC_APPROVALS.pop(approval, None) == (action, email, calendar_id, ident)


def _is_ea_managed(calendar_id: str) -> bool:
    ea = _ea_managed_calendar_id()
    return bool(ea) and (calendar_id or "").strip().lower() == ea.strip().lower()


def _library_gate(action: str, email: str, calendar_id: str, ident: str,
                  approval: str) -> tuple[str, Optional[dict]]:
    """("approved", None), ("ea-managed", None), or ("", the refusal)."""
    if _approved(approval, action, email, calendar_id, ident):
        return "approved", None
    if _is_ea_managed(calendar_id):
        return "ea-managed", None
    return "", {"error": f"BLOCKED: a Google calendar {action} outside EA-Managed needs "
                         "the user's button approval.",
                "target": calendar_id}


def ea_managed_event_refusal(event: dict, calendar_id: str) -> str:
    """"" when `event` is one the EA may move or delete on the EA-Managed
    calendar without a button, else why not: it has no attendees, and its
    organizer is that calendar (Google's organizer.self, or the organizer's
    address equal to the calendar id). An event someone invited the
    calendar to is not the EA's to change."""
    if not isinstance(event, dict) or not event:
        return "the event could not be read"
    if event.get("attendees"):
        return f"has {len(event['attendees'])} attendee(s)"
    org = event.get("organizer") or {}
    if not isinstance(org, dict):
        return "no organizer"
    if org.get("self") is True:
        return ""
    if str(org.get("email") or "").strip().lower() == (calendar_id or "").strip().lower():
        return ""
    return "not organized by the EA-Managed calendar"


def create_event(
    email: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    timezone: str = "",
    calendar_id: str = "",
    recurrence: Optional[list] = None,
    color_id: str = "",
    approval: str = "",
) -> dict:
    """Create a calendar event on the specified Google account.

    Args:
        email: Google account email (must be in scheduling-preferences.json)
        summary: Event title
        start: ISO datetime (e.g. "2026-04-08T10:00:00"), OR a date-only
            "YYYY-MM-DD" for an all-day event.
        end: ISO datetime or date-only (all-day end is exclusive, per Google).
        description: Optional event description/body
        location: Optional location
        timezone: Timezone for the event (default: the user's zone, user_tz)
        calendar_id: Target calendar ID within the account. Empty => the
            account's primary calendar (unchanged legacy behavior). Set to the
            EA-Managed calendar for un-gated auto-holds (see mail_mediation).
        recurrence: Optional list of RRULE strings (e.g.
            ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]) for a repeating event.
        approval: The process-local approval cmd_create_event mints after
            the Create button's token; needed for every calendar but
            EA-Managed (_library_gate).

    Returns:
        Dict with event id, htmlLink, and status.
    """
    target = calendar_id or email
    _, refused = _library_gate("create", email, target, summary, approval)
    if refused:
        return {**refused, "summary": summary}
    timezone = _zone_name(timezone)
    service = _build_service(email, interactive=False)
    if not service:
        return {"error": f"Could not authenticate Google Calendar for {email}"}

    def _endpoint(val: str) -> dict:
        # A bare "YYYY-MM-DD" (no time) means an all-day event; Google wants
        # {"date": ...} for those and {"dateTime", "timeZone"} for timed ones.
        if len(val) == 10 and val[4] == "-" and val[7] == "-":
            return {"date": val}
        return {"dateTime": val, "timeZone": timezone}

    event_body = {
        "summary": summary,
        "start": _endpoint(start),
        "end": _endpoint(end),
    }
    if recurrence:
        event_body["recurrence"] = (
            list(recurrence) if isinstance(recurrence, (list, tuple)) else [recurrence])
    # Color by token polarity unless an explicit color is given.
    resolved_color = color_id or _token_color(summary)
    if resolved_color:
        event_body["colorId"] = str(resolved_color)
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    result = service.events().insert(
        calendarId=target, body=event_body).execute()
    return {
        "status": "created",
        "event_id": result.get("id", ""),
        "link": result.get("htmlLink", ""),
        "summary": summary,
        "start": start,
        "end": end,
    }


# Two-letter weekday codes, as RFC 5545 BYDAY and the staged repeat_days
# both spell them.
_DAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _is_date_only(val: str) -> bool:
    return len(val) == 10 and val[4] == "-" and val[7] == "-"


def rrule_from_repeat(repeat: str, start: str, end: str, days: str = "",
                      interval=None, until: str = "", count=None,
                      tz_name: str = "") -> list:
    """The RRULE list for a series described by the staged repeat keys
    (repeat, repeat_days, repeat_interval, repeat_until, repeat_count), the
    same keys graph_client.build_recurrence takes for Outlook, refused on
    the same combinations so a series reads alike on both calendars.

    `start` and `end` describe ONE occurrence. UNTIL is the end of the last
    day in `tz_name`, written in UTC, as RFC 5545 requires when DTSTART
    carries a zone. Raises ValueError on any invalid combination.
    """
    repeat = (repeat or "").strip().lower()
    if repeat not in ("weekly", "daily"):
        raise ValueError(f"repeat must be 'weekly' or 'daily' (got '{repeat}')")
    interval = int(interval or 1)
    if interval < 1:
        raise ValueError(f"repeat interval must be 1 or more (got {interval})")
    if until and count:
        raise ValueError("Use repeat-until OR repeat-count, not both.")
    if not until and not count:
        raise ValueError("A repeating event needs an end: repeat-until "
                         "YYYY-MM-DD or repeat-count N.")
    first = datetime.fromisoformat(start)
    if not (_is_date_only(start) and _is_date_only(end)):
        if first.date() != datetime.fromisoformat(end).date():
            raise ValueError(
                "start and end must be on the same day for a repeating event "
                "(they describe one occurrence); repeat-until is the last "
                "date of the series.")

    parts = [f"FREQ={repeat.upper()}"]
    if interval != 1:
        parts.append(f"INTERVAL={interval}")
    if repeat == "weekly":
        codes = []
        for raw in (days or "").split(","):
            code = raw.strip().upper()
            if not code:
                continue
            if code not in _DAY_CODES:
                raise ValueError(f"Unknown day code '{raw.strip()}' in repeat-days. "
                                 "Use MO,TU,WE,TH,FR,SA,SU, e.g. TU,TH")
            if code not in codes:
                codes.append(code)
        if not codes:
            raise ValueError("A weekly repeat needs repeat-days, e.g. TU,TH")
        parts.append("BYDAY=" + ",".join(codes))
    elif days:
        raise ValueError("repeat-days only applies to a weekly repeat. For "
                         "every N days use daily with repeat-interval N.")

    if until:
        try:
            last = datetime.strptime(str(until).strip(), "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"repeat-until must be YYYY-MM-DD (got '{until}')")
        if last.date() < first.date():
            raise ValueError(f"repeat-until ({last.date().isoformat()}) is before "
                             f"the start date ({first.date().isoformat()}).")
        from zoneinfo import ZoneInfo
        end_of_day = last.replace(hour=23, minute=59, second=59,
                                  tzinfo=ZoneInfo(_zone_name(tz_name)))
        parts.append("UNTIL=" + end_of_day.astimezone(timezone.utc)
                     .strftime("%Y%m%dT%H%M%SZ"))
    else:
        count = int(count)
        if count < 1:
            raise ValueError(f"repeat-count must be 1 or more (got {count})")
        parts.append(f"COUNT={count}")
    return ["RRULE:" + ";".join(parts)]


def confirm_key(email: str, ident: str) -> str:
    """What a calendar button's token is bound to: the account, and the
    title (create) or the event id (update, delete). The bot mints the
    token with this as its message id."""
    return f"{email}:{ident}"


def cmd_create_event(args) -> dict:
    """create-event, behind the Create button for every calendar but
    EA-Managed.

    Those calendars hold the user's real events, so a write there needs the
    one-time token the bot mints when the user clicks Create (graph_actions
    holds the token store), bound to confirm_key(account, title). The
    EA-Managed calendar is the un-gated carve-out
    (mail_mediation.is_ea_managed_autohold) and needs none. The recurrence
    is checked before the token is consumed, so a typo does not burn an
    approval.
    """
    ea = _ea_managed_calendar_id()
    gated = not ea or (args.calendar_id or "") != ea
    if gated and not args.confirm_token:
        return {"error": "BLOCKED: create-event on a Google calendar other than "
                         "EA-Managed requires --confirm-token. Stage "
                         "~/.ea/state/pending-event.json with \"calendar\": "
                         "\"<account>\"; the user clicks Create to approve.",
                "summary": args.summary}

    recurrence = list(args.recurrence or [])
    repeat_flags = (args.repeat, args.repeat_days, args.repeat_interval,
                    args.repeat_until, args.repeat_count)
    if any(v not in (None, "") for v in repeat_flags):
        if recurrence:
            return {"error": "BLOCKED: give --recurrence or the --repeat flags, "
                             "not both.", "summary": args.summary}
        try:
            recurrence = rrule_from_repeat(
                args.repeat, args.start, args.end, days=args.repeat_days,
                interval=args.repeat_interval, until=args.repeat_until,
                count=args.repeat_count, tz_name=args.timezone)
        except ValueError as e:
            return {"error": f"BLOCKED: bad recurrence flags. {e}",
                    "summary": args.summary}

    if gated and not _button_token_ok(args.confirm_token, "create_google_event",
                                      args.email, args.summary):
        return {"error": "BLOCKED: invalid, expired, or already-used "
                         "confirmation token.", "summary": args.summary}
    approval = (issue_inprocess_approval("create", args.email,
                                         args.calendar_id or args.email, args.summary)
                if gated else "")

    try:
        return create_event(
            email=args.email, summary=args.summary,
            start=args.start, end=args.end,
            description=args.description, location=args.location,
            timezone=args.timezone, calendar_id=args.calendar_id,
            recurrence=recurrence or None, color_id=args.color_id,
            approval=approval,
        )
    except Exception as e:
        return {"error": f"Google Calendar create failed: {e}",
                "summary": args.summary}


def _ea_managed_calendar_id() -> str:
    """The calendar the un-gated verbs may touch. Owner side
    (mail_mediation.owner_side, which an owner process the owner checks
    misclassify still lands on): the owner-private id only, never the
    config's, so "" without calendars.json. Resident side: the config's id
    (a hint; the mediator decides), else EA_MANAGED_CALENDAR_ID from the
    environment (tests)."""
    mm = _mail_mediation()
    if mm.owner_side():
        return mm.owner_ea_managed_calendar_id()
    return ((_ea_managed_source() or {}).get("calendar_id", "")
            or os.environ.get("EA_MANAGED_CALENDAR_ID", "").strip())


def _require_ea_managed(calendar_id: str) -> Optional[dict]:
    """Gate for the mutating verbs below.

    The EA-Managed calendar is the assistant's own rule store: no
    attendees, no invitations, and a cron reconciler already rewrites it
    every half hour. Editing it needs no human in the loop.

    Every other calendar holds real meetings with real people, where a
    move or a delete mails a notice to attendees. Those stay on the
    button-gated path, so adding these verbs cannot become a way around
    it. Returns an error dict when the target is out of bounds.
    """
    ea = _ea_managed_calendar_id()
    if not ea:
        return {"error": "EA-Managed calendar is not configured: the owner-private "
                         "calendars.json is missing (docs/SETUP.md 'The EA-Managed calendar'; "
                         "python3 preprocessor/owner_trust.py seed --calendar)"}
    if (calendar_id or "").strip().lower() != ea.strip().lower():
        return {
            "error": "refused: this verb only edits the EA-Managed calendar",
            "target": calendar_id,
            "ea_managed": ea,
            "hint": "For a real meeting, stage the change and let the user "
                    "approve it: ~/.ea/state/pending-event-update.json to "
                    "reschedule, pending-event-delete.json to cancel.",
        }
    return None


def update_event(
    email: str,
    event_id: str,
    calendar_id: str,
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
    recurrence: Optional[list] = None,
    color_id: str = "",
) -> dict:
    """Patch an event on the EA-Managed calendar.

    Only the fields supplied are sent, so a retime leaves the token word
    and description alone. Recolors to match the token when the summary
    changes and no explicit color is given, keeping the polarity
    convention intact without a separate recolor pass.
    """
    blocked = _require_ea_managed(calendar_id)
    if blocked:
        return blocked
    body = _event_patch(summary, start, end, description, location, timezone,
                        recurrence, color_id or (_token_color(summary) if summary else ""))
    if not body:
        return {"error": "nothing to update: supply at least one field"}
    # A stale event id is the ordinary case after the reconciler rewrites the
    # calendar, so it answers with a structured error, not a stacktrace.
    return _patch_event(email, event_id, calendar_id, body,
                        hint="Re-read the id with list-ea-managed; the reconciler "
                             "rewrites this calendar every 30 minutes.")


def _event_patch(summary: str = "", start: str = "", end: str = "",
                 description: str = "", location: str = "",
                 timezone: str = "", recurrence: Optional[list] = None,
                 color_id: str = "") -> dict:
    """The body of a patch: only the fields supplied, so a retime leaves the
    title, description and location as they are. An empty `timezone` is
    the user's zone."""
    timezone = _zone_name(timezone)
    body: dict = {}
    if summary:
        body["summary"] = summary
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    for field, value in (("start", start), ("end", end)):
        if not value:
            continue
        body[field] = ({"date": value} if len(value) == 10
                       else {"dateTime": value, "timeZone": timezone})
    if recurrence is not None:
        body["recurrence"] = recurrence
    if color_id:
        body["colorId"] = color_id
    return body


def _ea_managed_check(service, event_id: str, calendar_id: str) -> tuple[dict, Optional[dict]]:
    """Read the event an un-gated move or delete names on EA-Managed: (the
    event, None) when ea_managed_event_refusal accepts it, else (the event
    or {}, the refusal). Nothing is written before this passes."""
    try:
        existing = service.events().get(
            calendarId=calendar_id, eventId=event_id).execute() or {}
    except Exception as e:
        return {}, {"error": f"refused: could not read the event to check it: "
                             f"{type(e).__name__}: {e}", "event_id": event_id,
                    "hint": "Re-read the id with list-ea-managed."}
    why = ea_managed_event_refusal(existing, calendar_id)
    if why:
        return existing, {
            "error": f"refused: this event is not the EA's own hold ({why})",
            "event_id": event_id,
            "hint": "An event with attendees, or one another calendar organizes, "
                    "moves or goes only with the user's button: stage "
                    "~/.ea/state/pending-event-update.json or pending-event-delete.json."}
    return existing, None


def _patch_event(email: str, event_id: str, calendar_id: str, body: dict,
                 hint: str, *, approval: str = "") -> dict:
    """Send one patch. A series master's id moves every occurrence; an
    instance id (`<master>_<timestamp>`) moves that occurrence only. A
    failure answers with a structured error carrying `hint`. Without an
    `approval` (cmd_update_event's, after the Reschedule button) only an
    attendee-less event the EA-Managed calendar organizes is patched."""
    mode, refused = _library_gate("update", email, calendar_id, event_id, approval)
    if refused:
        return {**refused, "event_id": event_id}
    if mode == "ea-managed" and "attendees" in body:
        return {"error": "refused: an EA-Managed event carries no attendees",
                "event_id": event_id}
    service = _build_service(email, interactive=False)
    if not service:
        return {"error": f"Could not authenticate Google Calendar for {email}"}
    if mode == "ea-managed":
        _, refused = _ea_managed_check(service, event_id, calendar_id)
        if refused:
            return refused
    try:
        result = service.events().patch(
            calendarId=calendar_id, eventId=event_id, body=body).execute()
    except Exception as e:
        return {"error": f"could not update event: {type(e).__name__}: {e}",
                "event_id": event_id, "hint": hint}
    return {
        "status": "updated",
        "event_id": result.get("id", ""),
        "summary": result.get("summary", ""),
        "start": (result.get("start") or {}).get(
            "dateTime", (result.get("start") or {}).get("date", "")),
        "end": (result.get("end") or {}).get(
            "dateTime", (result.get("end") or {}).get("date", "")),
        "updated_fields": sorted(body),
    }


def delete_event(email: str, event_id: str, calendar_id: str) -> dict:
    """Delete an event from the EA-Managed calendar.

    Deleting a series master removes every occurrence. To drop a single
    occurrence, pass that instance's id (the one carrying the
    `<master>_<timestamp>` suffix).
    """
    blocked = _require_ea_managed(calendar_id)
    if blocked:
        return blocked
    return _remove_event(email, event_id, calendar_id)


def _remove_event(email: str, event_id: str, calendar_id: str, *,
                  approval: str = "") -> dict:
    """Delete one event (a series master takes every occurrence with it).
    Without an `approval` (cmd_delete_event's, after the Delete button) only
    an attendee-less event the EA-Managed calendar organizes is deleted."""
    mode, refused = _library_gate("delete", email, calendar_id, event_id, approval)
    if refused:
        return {**refused, "event_id": event_id}
    service = _build_service(email, interactive=False)
    if not service:
        return {"error": f"Could not authenticate Google Calendar for {email}"}
    if mode == "ea-managed":
        existing, refused = _ea_managed_check(service, event_id, calendar_id)
        if refused:
            return refused
    else:
        try:
            existing = service.events().get(
                calendarId=calendar_id, eventId=event_id).execute()
        except Exception:
            existing = {}
    try:
        service.events().delete(
            calendarId=calendar_id, eventId=event_id).execute()
    except Exception as e:
        return {"error": f"could not delete event: {type(e).__name__}: {e}",
                "event_id": event_id}
    return {
        "status": "deleted",
        "event_id": event_id,
        "summary": (existing or {}).get("summary", ""),
    }


def get_event(email: str, event_id: str, calendar_id: str = "") -> dict:
    """One event resource as the API returns it, or {"error": ...}. The
    bot's read for a Reschedule or Delete card and for the check at click
    time; the calendar defaults to the account's own, the one the button's
    write targets."""
    service = _build_service(email, interactive=False)
    if not service:
        return {"error": f"Could not authenticate Google Calendar for {email}"}
    try:
        return service.events().get(calendarId=calendar_id or email,
                                    eventId=event_id).execute()
    except Exception as e:
        status = getattr(getattr(e, "resp", None), "status", "")
        return {"error": f"could not read event: {type(e).__name__} {status}".strip(),
                "event_id": event_id}


def _button_token_ok(token: str, action: str, email: str, ident: str) -> bool:
    """Consume the one-time token the bot minted when the user clicked a
    calendar button, bound to confirm_key(account, ident). graph_actions
    holds the token store; it is imported on the gated path only."""
    import graph_actions
    return bool(graph_actions._validate_and_consume_token(
        token, action, confirm_key(email, ident)))


def cmd_update_event(args) -> dict:
    """update-event. Without --confirm-token it edits the EA-Managed calendar
    only (update_event). With one it is the Reschedule button's path to the
    account's own calendar (--email; --calendar-id names another calendar
    of that account): the token is bound to (account, event id), and an
    empty patch is refused before the token is consumed."""
    if not args.confirm_token:
        src = ea_managed_target() or {}
        return update_event(
            email=args.email or src.get("email", ""),
            event_id=args.event_id,
            calendar_id=args.calendar_id or _ea_managed_calendar_id(),
            summary=args.summary, start=args.start, end=args.end,
            description=args.description, location=args.location,
            timezone=args.timezone, recurrence=args.recurrence,
            color_id=args.color_id)
    if not args.email:
        return {"error": "BLOCKED: a button update needs --email <account>.",
                "event_id": args.event_id}
    body = _event_patch(args.summary, args.start, args.end, args.description,
                        args.location, args.timezone, args.recurrence, args.color_id)
    if not body:
        return {"error": "nothing to update: supply at least one field",
                "event_id": args.event_id}
    if not _button_token_ok(args.confirm_token, "update_google_event",
                            args.email, args.event_id):
        return {"error": "BLOCKED: invalid, expired, or already-used "
                         "confirmation token.", "event_id": args.event_id}
    target = args.calendar_id or args.email
    return _patch_event(args.email, args.event_id, target, body,
                        hint="Re-read the id with get-events.",
                        approval=issue_inprocess_approval(
                            "update", args.email, target, args.event_id))


def cmd_delete_event(args) -> dict:
    """delete-event. Without --confirm-token it deletes from the EA-Managed
    calendar only (delete_event). With one it is the Delete button's path to
    the account's own calendar, the token bound to (account, event id)."""
    if not args.confirm_token:
        src = ea_managed_target() or {}
        return delete_event(
            email=args.email or src.get("email", ""),
            event_id=args.event_id,
            calendar_id=args.calendar_id or _ea_managed_calendar_id())
    if not args.email:
        return {"error": "BLOCKED: a button delete needs --email <account>.",
                "event_id": args.event_id}
    if not _button_token_ok(args.confirm_token, "delete_google_event",
                            args.email, args.event_id):
        return {"error": "BLOCKED: invalid, expired, or already-used "
                         "confirmation token.", "event_id": args.event_id}
    target = args.calendar_id or args.email
    return _remove_event(args.email, args.event_id, target,
                         approval=issue_inprocess_approval(
                             "delete", args.email, target, args.event_id))


def list_ea_managed(email: str = "", expand_instances: bool = False,
                    days: int = 60) -> dict:
    """Everything on the EA-Managed calendar, with ids, so a move or a delete
    has something to name. Series masters are returned by default; pass
    `expand_instances` to address a single occurrence.
    """
    src = ea_managed_target() or {}
    calendar_id = src.get("calendar_id", "")
    if not calendar_id:
        return {"error": "EA-Managed calendar is not configured"}
    service = _build_service(email or src.get("email", ""), interactive=False)
    if not service:
        return {"error": "Could not authenticate Google Calendar"}

    params = {"calendarId": calendar_id, "maxResults": 250,
              "showDeleted": False, "singleEvents": expand_instances}
    if expand_instances:
        now = datetime.now(timezone.utc)
        params["timeMin"] = now.isoformat()
        params["timeMax"] = (now + timedelta(days=days)).isoformat()
        params["orderBy"] = "startTime"
    res = service.events().list(**params).execute()

    events = []
    for e in res.get("items", []):
        if e.get("status") == "cancelled":
            continue
        start = e.get("start") or {}
        end = e.get("end") or {}
        events.append({
            "event_id": e.get("id", ""),
            "summary": e.get("summary", ""),
            "start": start.get("dateTime", start.get("date", "")),
            "end": end.get("dateTime", end.get("date", "")),
            "recurrence": e.get("recurrence") or [],
            "recurring_event_id": e.get("recurringEventId", ""),
            # Reconciler-owned mirrors get rewritten on the next cron
            # pass, so a hand edit to one will not survive.
            "auto_managed": "[auto]" in (e.get("summary") or "").lower(),
        })
    return {"calendar_id": calendar_id, "count": len(events),
            "expanded": expand_instances, "events": events}


def recolor_ea_managed(email: str, calendar_id: str) -> dict:
    """Patch colorId on every tokened event on a calendar to the polarity
    convention (green wh / red nm,p0 / amber p1,p2 / graphite wr). Idempotent
    maintenance pass to run after manual UI edits (which don't auto-color).
    Operates on masters, so recurring series recolor in one patch. It has
    no button path, so it writes the EA-Managed calendar only."""
    blocked = _require_ea_managed(calendar_id)
    if blocked:
        return blocked
    service = _build_service(email, interactive=False)
    if not service:
        return {"error": f"Could not authenticate Google Calendar for {email}"}
    res = service.events().list(calendarId=calendar_id, singleEvents=False,
                                maxResults=250, showDeleted=False).execute()
    recolored, already, skipped = [], 0, 0
    for e in res.get("items", []):
        if e.get("status") == "cancelled":
            continue
        want = _token_color(e.get("summary", ""))
        # An event with attendees, or one another calendar organizes, is
        # not the EA's to change (ea_managed_event_refusal).
        if not want or ea_managed_event_refusal(e, calendar_id):
            skipped += 1
            continue
        if e.get("colorId") == want:
            already += 1
            continue
        service.events().patch(calendarId=calendar_id, eventId=e["id"],
                               body={"colorId": want}).execute()
        recolored.append(e.get("summary", ""))
    return {"status": "ok", "recolored": recolored,
            "already_correct": already, "non_token_skipped": skipped}


# -------------------------------------------------------------------
# CLI: setup and test
# -------------------------------------------------------------------
def setup_all_accounts():
    """Interactive setup — authenticate all configured Google accounts."""
    accounts = _get_google_accounts()
    if not accounts:
        print("No Google Calendar accounts configured in scheduling-preferences.json")
        print("Add entries with type 'google_calendar_mcp' to the calendars section.")
        return

    if not _client_source():
        print(f"\nERROR: Client secret not found at {CLIENT_SECRET_FILE}")
        print("\nSetup steps:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project (or select existing)")
        print("3. Enable the Google Calendar API:")
        print("   APIs & Services → Library → 'Google Calendar API' → Enable")
        print("4. Create OAuth credentials:")
        print("   APIs & Services → Credentials → Create Credentials → OAuth client ID")
        print("   Application type: Desktop app")
        print("   Name: 'EA Assistant'")
        print(f"5. Download JSON → save as {CLIENT_SECRET_FILE}")
        print("6. Re-run this setup.")
        return

    print(f"Authenticating {len(accounts)} Google Calendar account(s)...\n")
    for account in accounts:
        email = account["email"]
        print(f"--- {email} ---")
        creds = authenticate_account(email, interactive=True)
        if creds:
            print(f"✓ Authenticated {email}")
        else:
            print(f"✗ Failed to authenticate {email}")
        print()


def test_all_accounts():
    """Test connectivity and show upcoming events."""
    accounts = _get_google_accounts()
    if not accounts:
        print("No Google Calendar accounts configured.")
        return

    for account in accounts:
        email = account["email"]
        print(f"\n--- {email} ---")
        events = get_events(email, days_ahead=7)
        print(f"  {len(events)} events in next 7 days:")
        for e in events[:5]:
            dt = e["start"]["dateTime"][:16]
            print(f"  {dt} — {e['subject']}")
        if len(events) > 5:
            print(f"  ... and {len(events) - 5} more")


def build_parser():
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    import argparse
    parser = argparse.ArgumentParser(description="Google Calendar EA Client", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command")

    # Legacy flags (still supported)
    parser.add_argument("--setup", action="store_true", help="Interactive setup")
    parser.add_argument("--test", action="store_true", help="Test connectivity")
    parser.add_argument("--events", action="store_true", help="Print events as JSON")
    parser.add_argument("--days", type=int, default=14, help="Days ahead to query")

    # Subcommands
    p_get = sub.add_parser("get-events", help="Get events for a Google account", allow_abbrev=False)
    p_get.add_argument("--email", required=True, help="Google account email")
    p_get.add_argument("--start", required=True, help="Start time (ISO format)")
    p_get.add_argument("--end", required=True, help="End time (ISO format)")

    # One event by id, for the bot's Reschedule and Delete cards. It is no
    # mediated verb: ea-bot reads Google events with get-events.
    p_one = sub.add_parser("get-event", help="Read one event by id (owner-side)",
                           allow_abbrev=False)
    p_one.add_argument("--email", required=True, help="Google account email")
    p_one.add_argument("--event-id", required=True, help="Event ID")
    p_one.add_argument("--calendar-id", default="",
                       help="Calendar (default: the account's own calendar)")

    p_create = sub.add_parser("create-event", help="Create a calendar event", allow_abbrev=False)
    p_create.add_argument("--email", required=True, help="Google account email")
    p_create.add_argument("--summary", required=True, help="Event title")
    p_create.add_argument("--start", required=True, help="Start time (ISO format)")
    p_create.add_argument("--end", required=True, help="End time (ISO format)")
    p_create.add_argument("--description", default="", help="Event description")
    p_create.add_argument("--location", default="", help="Location")
    p_create.add_argument("--timezone", default="", help="Timezone (default: the user's zone)")
    p_create.add_argument("--calendar-id", default="",
                          help="Target calendar ID (default: account primary). "
                               "The EA-Managed calendar is the only one the "
                               "resident may write un-gated (attendee-less holds).")
    p_create.add_argument("--recurrence", action="append", default=None,
                          help="RRULE string for a repeating event, e.g. "
                               "'RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR'. "
                               "Repeat the flag for multiple rules.")
    p_create.add_argument("--color-id", default="",
                          help="Google colorId override. Default: auto by token "
                               "(wh green, nm/p0 red, p1/p2 amber, wr graphite).")
    # The staged-event repeat keys, as graph_actions.py create-event takes
    # them, so the Create button passes one flag set to either calendar.
    p_create.add_argument("--repeat", default=None, help="weekly | daily")
    p_create.add_argument("--repeat-days", default=None, help="Weekly days, e.g. TU,TH")
    p_create.add_argument("--repeat-interval", default=None, help="Every N weeks/days")
    p_create.add_argument("--repeat-until", default=None, help="Last date, YYYY-MM-DD")
    p_create.add_argument("--repeat-count", default=None, help="Number of occurrences")
    p_create.add_argument("--confirm-token", default=None,
                          help="One-time approval token from the Create button. "
                               "Required for every calendar but EA-Managed.")

    # Mutating verbs. Without a button token both refuse any calendar but
    # EA-Managed, and the calendar id defaults to EA-Managed because that
    # is the only target they have then. With --confirm-token (minted by
    # the bot on a Reschedule or Delete click) they act on --email's own
    # calendar.
    p_update = sub.add_parser(
        "update-event",
        help="Move or edit an event on the EA-Managed calendar (un-gated), "
             "or, with --confirm-token, on the account's own calendar.",
        allow_abbrev=False)
    p_update.add_argument("--email", default="", help="Google account email")
    p_update.add_argument("--event-id", required=True, help="Event ID to patch")
    p_update.add_argument("--calendar-id", default="",
                          help="Target calendar (default: EA-Managed)")
    p_update.add_argument("--summary", default="", help="New title")
    p_update.add_argument("--start", default="", help="New start (ISO or date)")
    p_update.add_argument("--end", default="", help="New end (ISO or date)")
    p_update.add_argument("--description", default="", help="New description")
    p_update.add_argument("--location", default="", help="New location")
    p_update.add_argument("--timezone", default="", help="Timezone (default: the user's zone)")
    p_update.add_argument("--recurrence", action="append", default=None,
                          help="Replacement RRULE(s); repeat for multiple")
    p_update.add_argument("--color-id", default="", help="colorId override")
    p_update.add_argument("--confirm-token", default=None,
                          help="One-time token from the Reschedule button")

    p_delete = sub.add_parser(
        "delete-event",
        help="Delete an event from the EA-Managed calendar (un-gated), "
             "or, with --confirm-token, from the account's own calendar.",
        allow_abbrev=False)
    p_delete.add_argument("--email", default="", help="Google account email")
    p_delete.add_argument("--event-id", required=True, help="Event ID to delete")
    p_delete.add_argument("--calendar-id", default="",
                          help="Target calendar (default: EA-Managed)")
    p_delete.add_argument("--confirm-token", default=None,
                          help="One-time token from the Delete button")

    p_list = sub.add_parser(
        "list-ea-managed",
        help="List events on the EA-Managed calendar with their IDs, so a "
             "move or delete has something to name.",
        allow_abbrev=False)
    p_list.add_argument("--email", default="", help="Google account email")
    p_list.add_argument("--show-instances", action="store_true",
                        help="Expand recurring series into occurrences")
    p_list.add_argument("--days", type=int, default=60)

    p_recolor = sub.add_parser(
        "recolor-tokens",
        help="Recolor tokened events on a calendar to the polarity convention "
             "(maintenance pass after manual edits).",
        allow_abbrev=False)
    p_recolor.add_argument("--email", required=True, help="Google account email")
    p_recolor.add_argument("--calendar-id", required=True, help="Target calendar ID")
    return parser


if __name__ == "__main__":
    import id_handles as _id_handles
    sys.argv[1:] = _id_handles.resolve_argv(sys.argv[1:])
    from mail_mediation import forward_or_none
    forward_or_none("google_calendar_client", sys.argv[1:],  # ea-bot: mediate SAFE, refuse gated
                    transform=_id_handles.annotate_output)

    parser = build_parser()
    args = parser.parse_args()
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mm_child.check_child_parse(parser, args)  # the verb the mediator checked
    # Every id option is a plain mail id, whoever runs the tool.
    _mm_child.refuse_unplain_ids("google_calendar_client", getattr(args, "command", "") or "", sys.argv[1:])

    if args.command == "get-events":
        _start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        _end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        events = get_events(
            email=args.email, start=_start, end=_end,
        )
        print(json.dumps(events, indent=2))
    elif args.command == "get-event":
        print(json.dumps(get_event(args.email, args.event_id, args.calendar_id), indent=2))
    elif args.command == "create-event":
        print(json.dumps(cmd_create_event(args), indent=2))
    elif args.command == "update-event":
        print(json.dumps(cmd_update_event(args), indent=2))
    elif args.command == "delete-event":
        print(json.dumps(cmd_delete_event(args), indent=2))
    elif args.command == "list-ea-managed":
        print(json.dumps(list_ea_managed(
            email=args.email, expand_instances=args.show_instances,
            days=args.days), indent=2))
    elif args.command == "recolor-tokens":
        print(json.dumps(recolor_ea_managed(args.email, args.calendar_id), indent=2))
    elif args.setup:
        setup_all_accounts()
    elif args.test:
        test_all_accounts()
    elif args.events:
        events = get_all_events(days_ahead=args.days)
        print(json.dumps(events, indent=2))
    else:
        parser.print_help()
