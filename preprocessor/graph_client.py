"""
Microsoft Graph API client using MSAL delegated permissions.
Authenticates once interactively, then silently via refresh token.

Uses Microsoft first-party well-known client IDs that are pre-consented
in virtually all M365 tenants. See docs/AUTHENTICATION.md for why this is necessary.
"""

import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import msal
import requests

try:
    import owner_trust as _owner_trust
    import published_apps as _published
except ModuleNotFoundError:  # imported as preprocessor.graph_client
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import published_apps as _published

# -------------------------------------------------------------------
# Configuration – edit these or override via environment variables
# -------------------------------------------------------------------

# Microsoft Graph PowerShell SDK: a first-party app pre-authorized for Graph
# in virtually every M365 tenant, so it bypasses third-party consent policies.
# The fallback when no custom app is registered. Azure PowerShell (1950a258)
# is NOT a substitute; it covers Azure management, not Graph.
# docs/AUTHENTICATION.md section "1a. Graph API auth" has the setup steps.
GRAPH_POWERSHELL_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# A custom app registered in the user's own Azure portal, when present.
CUSTOM_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")

# Confidential-client (auth-code + secret) support. When a client secret is
# provided, graph_client uses the OAuth2 authorization-code flow with a
# confidential client instead of the public-client device-code/interactive
# flows. This suits tenants that permit app registrations but disable
# "Allow public client flows". See docs/AUTHENTICATION.md.
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
_DEFAULT_REDIRECT_URI = "http://localhost:8400/callback"
_REDIRECT_URI_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?(/[^\s]*)?$")


def _loopback_redirect_uri(value: str) -> str:
    """The auth-code redirect, which carries the authorization code: a
    loopback http URL on this machine, else the default."""
    if not value or _REDIRECT_URI_RE.match(value):
        return value or _DEFAULT_REDIRECT_URI
    print(f"graph_client: ignoring GRAPH_REDIRECT_URI {value!r}: not a loopback "
          f"URL; using {_DEFAULT_REDIRECT_URI}", file=sys.stderr)
    return _DEFAULT_REDIRECT_URI


GRAPH_REDIRECT_URI = _loopback_redirect_uri(os.environ.get("GRAPH_REDIRECT_URI", ""))

# Explicit auth-method selector, chosen at setup time and stored in ~/.ea/.env:
#   "device_code" | "interactive" | "confidential"
# When unset, the method is inferred: a client secret => confidential;
# otherwise public-client interactive with a device-code fallback. An owner
# plugin's auth source (docs/plugins.md) can supply a token whatever the
# method.
GRAPH_AUTH_METHOD = os.environ.get("GRAPH_AUTH_METHOD", "").strip().lower()


def _use_confidential() -> bool:
    """True when the confidential auth-code + secret flow should be used.
    Never for the project's published app, a public client with no secret."""
    if CLIENT_PATH == "published":
        return False
    if GRAPH_AUTH_METHOD == "confidential":
        return True
    if GRAPH_AUTH_METHOD in ("device_code", "interactive"):
        return False
    return bool(GRAPH_CLIENT_SECRET)


WELL_KNOWN_CLIENTS = (
    [("custom", CUSTOM_CLIENT_ID)] if CUSTOM_CLIENT_ID
    else [("graph_powershell", GRAPH_POWERSHELL_CLIENT_ID)]
)

# 'organizations' (not tenant-specific) avoids tenant-level app restriction
# policies while still requiring an organizational account to sign in.
_DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
_AUTHORITY_PREFIX = "https://login.microsoftonline.com/"


def _microsoft_authority(value: str) -> str:
    """The sign-in authority, which receives the refresh token on every
    renewal: Microsoft's login host only, else the default."""
    if not value or value.startswith(_AUTHORITY_PREFIX):
        return value or _DEFAULT_AUTHORITY
    print(f"graph_client: ignoring GRAPH_AUTHORITY {value!r}: not "
          f"{_AUTHORITY_PREFIX}; using {_DEFAULT_AUTHORITY}", file=sys.stderr)
    return _DEFAULT_AUTHORITY


AUTHORITY = _microsoft_authority(os.environ.get("GRAPH_AUTHORITY", ""))

SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/Contacts.Read",
    "https://graph.microsoft.com/MailboxSettings.Read",
    "https://graph.microsoft.com/Tasks.ReadWrite",
    "https://graph.microsoft.com/People.Read",
    "https://graph.microsoft.com/Files.Read",
]

# Optional runtime override of the requested scopes, aligned at setup time to
# what the app registration actually grants (see setup_confidential_and_align).
# The full SCOPES list above remains the default superset when unset.
# Space- or comma-separated; short names ("Mail.ReadWrite") or full URLs.
GRAPH_SCOPES = os.environ.get("GRAPH_SCOPES", "").replace(",", " ").split()

# A broader grant subsumes these narrower Read scopes: if the app grants the
# ReadWrite variant, the plain Read request is redundant and can be dropped.
_SUBSUMED_BY = {
    "Mail.Read": "Mail.ReadWrite",
    "Calendars.Read": "Calendars.ReadWrite",
    "Contacts.Read": "Contacts.ReadWrite",
    "Tasks.Read": "Tasks.ReadWrite",
    "Files.Read": "Files.ReadWrite",
}
# If we want a ReadWrite scope the app doesn't grant but it DOES grant the Read
# variant, downgrade to Read so we keep read access instead of losing the area.
_READ_FALLBACK = {
    "Mail.ReadWrite": "Mail.Read",
    "Calendars.ReadWrite": "Calendars.Read",
    "Contacts.ReadWrite": "Contacts.Read",
    "Tasks.ReadWrite": "Tasks.Read",
    "Files.ReadWrite": "Files.Read",
}


def _scope_basename(s: str) -> str:
    return s.rstrip("/").rsplit("/", 1)[-1]


def _full_scope(s: str) -> str:
    """Normalize a scope to its full Graph URL form (leaves reserved scopes)."""
    s = s.strip()
    if (not s or s.startswith("http")
            or s.endswith("/.default")
            or s in ("openid", "profile", "offline_access", "email")):
        return s
    return f"https://graph.microsoft.com/{s}"


def _effective_scopes() -> list:
    """Scopes to request: the setup-aligned GRAPH_SCOPES override if present;
    otherwise, for the project's published app, the permissions it registers
    for the assistant profile (a request outside them would ask the tenant
    for a consent it has not given); otherwise the full default SCOPES
    superset."""
    if GRAPH_SCOPES:
        return [_full_scope(s) for s in GRAPH_SCOPES]
    if CLIENT_PATH == "published":
        return [_full_scope(s) for s in _published.PUBLISHED_GRAPH_SCOPES]
    return SCOPES


def align_scopes(granted, desired=None) -> list:
    """Prune `desired` scopes to what the app actually grants.

    `granted` is a set/iterable of scope basenames (e.g. from a token's `scp`
    claim). For each desired scope:
      - keep it if granted directly;
      - drop it if it's a Read scope whose ReadWrite superset is granted
        (redundant — ReadWrite already covers reads);
      - downgrade a ReadWrite scope to Read if only Read is granted;
      - otherwise drop it (not granted at all).
    Returns full-URL scopes.
    """
    desired = desired if desired is not None else SCOPES
    granted = {_scope_basename(g) for g in granted}
    out = []
    for s in desired:
        base = _scope_basename(s)
        if base in granted:
            out.append(_full_scope(s))
        elif _SUBSUMED_BY.get(base) in granted:
            continue  # redundant read; superset already kept
        elif _READ_FALLBACK.get(base) in granted:
            out.append(_full_scope(_READ_FALLBACK[base]))  # downgrade to Read
    return out


GRAPH_URL = "https://graph.microsoft.com/v1.0"
OUTLOOK_REST_URL = "https://outlook.office.com/api/v2.0"


def _api_base() -> str:
    """Return the right API base URL for the current token.

    MSAL refresh tokens always produce Graph-audience tokens, so use
    GRAPH_URL when that path is available. Use the Outlook REST URL only
    when the token comes from an owner plugin's auth source and carries
    the Outlook audience.
    """
    # MSAL cache present = Graph API audience (not Outlook REST)
    if CACHE_FILE.exists() and _app.get_accounts():
        return GRAPH_URL

    token = _source_token()
    if token and _decode_jwt_payload(token).get("aud") == OUTLOOK_AUDIENCE:
        return OUTLOOK_REST_URL
    return GRAPH_URL

# Persistent token cache location
EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CACHE_FILE = EA_DIR / "auth" / "msal_token_cache.json"


# --- Token files in the auth directory --------------------------------------
# ~/.ea/auth sits under a directory ea-bot owns, so ea-bot can rename it away
# and put its own in its place. An owner process (owner_trust.is_owner_process)
# reads and writes a token file only after the owner checks on the directory
# and the file, made on the open descriptors; a failed check raises
# OwnerTrustError. Token files are 0660 in the owner's own group, which the
# checks accept only while that group holds no other user. Other processes
# (ea-bot, which cannot read the tokens under mail mediation) keep plain path
# reads and writes.
def _checked_token_io() -> bool:
    """True when token files go through the owner checks: every process but
    the resident's own and one on a scratch EA tree (a test). An owner
    process on the install tree is checked whatever
    owner_trust.is_owner_process() says, so a missing private directory
    cannot switch the checks off. An unknown answer counts as checked.
    The same rule as gmail_client and google_calendar_client."""
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


def _read_auth_text(path: Path) -> str:
    """The text of a token file in the auth directory (see above)."""
    if _checked_token_io():
        return _owner_trust.read_owner_file(path, allow_private_group=True).decode("utf-8")
    return path.read_text()


# --- Process-local approvals ------------------------------------------------
# The CLI (graph_actions.py) validates and CONSUMES a one-time token from
# pending-confirmations.json, then calls into this library. What it hands the
# library is a random value that lives only in this process's memory, so no
# string read out of a file on disk can satisfy the second gate.
_INPROC_PREFIX = "cli-approved:"
_INPROC_APPROVALS: dict[str, tuple[str, str, str]] = {}


def issue_inprocess_approval(action: str, identifier: str, fingerprint: str = "") -> str:
    """Mint a one-time, process-local approval for one gated library call.

    Call ONLY after a real one-time confirmation token has been validated and
    consumed (graph_actions.py does this). The returned value is bound to
    (action, identifier, fingerprint) and is invalidated by its first use.
    A send approval carries the fingerprint of the content the owner
    reviewed (draft_fingerprint.py).
    """
    token = _INPROC_PREFIX + secrets.token_urlsafe(24)
    _INPROC_APPROVALS[token] = (action, identifier, fingerprint or "")
    return token


def _graph_actions_module():
    """Import graph_actions (the one-time token store), or None. Imported
    at call time: graph_actions imports this module."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import graph_actions
        return graph_actions
    except Exception:
        return None


def _scheduled_sends_module():
    """Import the scheduled-send credential helpers, or None."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import scheduled_sends
        return scheduled_sends
    except Exception:
        return None


def _validate_library_token(token: Optional[str], action: str, identifier: str,
                            fingerprint: Optional[str] = None) -> bool:
    """Defense-in-depth: validate a confirmation token at the library level.

    This is the second gate (after graph_actions.py CLI). Even if someone
    calls graph_client.send_draft() directly, this blocks without a valid token.

    Accepts three credentials, all bound to (action, identifier), and for a
    send also to the fingerprint of the reviewed content:
    - a process-local approval minted by issue_inprocess_approval() after the
      CLI consumed a real token (one-time, never written to disk)
    - a scheduled-send credential: an HMAC over (draft_id, send_after,
      fingerprint) signed with a key only the owner can read
    - a live one-time token from pending-confirmations.json

    A bare "pre-approved:" marker is NOT a credential and is refused. A send
    with no fingerprint is refused whatever the token.
    """
    if not token:
        return False
    if action == "send" and not fingerprint:
        return False

    if token.startswith(_INPROC_PREFIX):
        return _INPROC_APPROVALS.pop(token, None) == (action, identifier, fingerprint or "")

    if token.startswith("pre-approved:"):
        # Scheduled sends. The queue file is writable by the prompt-injectable
        # user, so the HMAC is the authority; the legacy bare marker fails.
        if action != "send":
            return False
        sched = _scheduled_sends_module()
        return sched is not None and sched.verify_send_token(
            token, identifier, fingerprint=fingerprint)

    # A stored one-time token: graph_actions owns that store (its location in
    # the owner-private directory, its lock, and its checked reads and
    # writes), so the check and the consumption happen there.
    ga = _graph_actions_module()
    if ga is None:
        return False
    try:
        entry = ga._validate_and_consume_token(token, action, identifier)
    except Exception:
        return False
    if entry is None:
        return False
    return action != "send" or entry.get("fingerprint") == fingerprint


def _get_user_timezone() -> str:
    """The user's zone name (user_tz.py: the profile, the scheduling
    preferences, then the system's zone)."""
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return user_tz.name(EA_DIR)


def to_user_local(dt: datetime) -> datetime:
    """`dt` as a local time in the user's zone, with no offset. Graph reads
    an event's dateTime in the timeZone sent beside it (the user's zone), so
    a time staged with another offset is converted here, once, to the
    instant the confirmation card showed. A time with no offset is already
    the user's local time and comes back unchanged."""
    if dt.tzinfo is None:
        return dt
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return dt.astimezone(user_tz.zone(EA_DIR)).replace(tzinfo=None)


def _event_time(dt: datetime) -> dict:
    """An event start or end for Graph: the user's local time, no offset,
    and the user's zone."""
    return {"dateTime": to_user_local(dt).isoformat(), "timeZone": _get_user_timezone()}


# -------------------------------------------------------------------
# Token cache persistence
# -------------------------------------------------------------------
def _first_json_object(text: str) -> tuple[str, bool]:
    """(the first JSON object in `text`, whether anything followed it).

    A cache file left with trailing bytes by an interrupted or interleaved
    write still opens with one complete, valid object. Keep it rather than
    treating the whole file as unreadable."""
    dec = json.JSONDecoder()
    obj, end = dec.raw_decode(text.lstrip())
    lead = len(text) - len(text.lstrip())
    return text[lead:lead + end], bool(text[lead + end:].strip())


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if CACHE_FILE.exists():
        raw = _read_auth_text(CACHE_FILE)
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            good, _trailing = _first_json_object(raw)  # raises if no object at all
            print(f"graph_client: {CACHE_FILE.name} had trailing bytes after a "
                  f"complete object; using the object and rewriting the file",
                  file=sys.stderr)
            raw = good
            try:
                _write_cache_file(raw)
            except OSError as e:  # read-only caller (ea-bot): still usable
                print(f"graph_client: could not rewrite {CACHE_FILE.name}: {e}",
                      file=sys.stderr)
        cache.deserialize(raw)
    return cache


def _write_cache_file(serialized: str) -> None:
    """Atomic, serialized write: a temp file in the same directory, fsync,
    rename over the old one, under an exclusive lock, so two processes
    can never interleave bytes and a reader never sees a half-written
    file. An owner process writes through owner_trust instead
    (_write_cache_file_owner), which never creates the auth directory: it
    is the owner's, provisioned at install, and a missing one is refused."""
    import fcntl
    import tempfile
    if _checked_token_io():
        _write_cache_file_owner(serialized)
        return
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE_FILE.with_suffix(".lock")
    with open(lock_path, "a+") as lock:
        try:
            os.chmod(lock_path, 0o660)
        except OSError:
            pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(prefix=CACHE_FILE.name + ".", dir=CACHE_FILE.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o660)  # group-writable, as before
            os.replace(tmp, CACHE_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_cache_file_owner(serialized: str) -> None:
    """The owner's cache write: the lock and the new file are opened relative
    to the checked auth directory, and the file lands there by an atomic
    rename (owner_trust.write_owner_file), so a swapped directory can neither
    receive the token nor redirect the lock."""
    import fcntl
    lock_name = CACHE_FILE.with_suffix(".lock").name
    with _owner_trust.open_owner_dir(CACHE_FILE.parent, allow_private_group=True) as dfd:
        lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                       0o660, dir_fd=dfd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _owner_trust.write_owner_file(CACHE_FILE, serialized, mode=0o660,
                                          allow_private_group=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)


def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        try:
            _write_cache_file(cache.serialize())
        except (OSError, _owner_trust.OwnerTrustError) as e:
            # A non-owner caller cannot write the cache, and an owner process
            # refuses an auth directory that fails the owner checks. Never
            # crash the whole run over that; the token in hand is still usable.
            print(f"graph_client: token cache not saved: {e}", file=sys.stderr)


MSAL_TOKENS_FILE = EA_DIR / "auth" / "msal_tokens.json"


# -------------------------------------------------------------------
# Tokens from owner plugins' auth sources (docs/plugins.md)
# -------------------------------------------------------------------
# A source may return an Outlook REST token (Outlook on the web issues
# them); _api_base routes such a token to OUTLOOK_REST_URL.
OUTLOOK_AUDIENCE = "https://outlook.office.com"         # Outlook REST API resource


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


# The last token an auth source returned and its expiry (the JWT exp
# claim), kept for this process: _api_base and _headers both need it on
# every request. A token without a readable exp is not kept.
_SOURCE_TOKEN: dict = {"token": None, "exp": 0.0}


def _source_token() -> Optional[str]:
    """The first token the enabled owner-zone plugins' graph auth sources
    return (docs/plugins.md), or None. Loaded only in an owner process,
    from the owner's private dir; a token is reused until a minute before
    it expires."""
    if _SOURCE_TOKEN["token"] and _SOURCE_TOKEN["exp"] - time.time() > 60:
        return _SOURCE_TOKEN["token"]
    _SOURCE_TOKEN["token"], _SOURCE_TOKEN["exp"] = None, 0.0
    try:
        try:
            import plugin_registry as _plugins
        except ModuleNotFoundError:
            from preprocessor import plugin_registry as _plugins
        for token in _plugins.owner_auth_tokens("graph"):
            try:
                exp = float(_decode_jwt_payload(token).get("exp") or 0)
            except (TypeError, ValueError):
                exp = 0.0
            if exp:
                _SOURCE_TOKEN["token"], _SOURCE_TOKEN["exp"] = token, exp
            return token
    except Exception as e:
        print(f"plugin auth sources skipped: {e}", file=sys.stderr)
    return None


# -------------------------------------------------------------------
# WSL browser fix
# -------------------------------------------------------------------
def _is_wsl() -> bool:
    """True under Windows Subsystem for Linux."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _patch_wsl_browser():
    """
    In WSL, Python's webbrowser module can't find the Windows browser.

    Opens the URL through PowerShell's Start-Process, which takes it as a
    single string. `cmd.exe /c start` would read '&' as a command separator
    and truncate the URL at the first query parameter, which Entra reports
    as AADSTS900144 (scope missing).
    """
    if not _is_wsl():
        return  # Not WSL

    def _wsl_open(url, new=0, autoraise=True):
        # Print URL first so user can click/copy it regardless of browser behavior
        print(f"\nOpen this URL in your browser to sign in:\n{url}\n", file=sys.stderr)
        # Use PowerShell to open — handles & in URLs correctly
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    webbrowser.open = _wsl_open
    print("WSL detected: will open auth URL via PowerShell.", file=sys.stderr)


# -------------------------------------------------------------------
# Authentication
# -------------------------------------------------------------------
try:
    _cache = _load_cache()
except PermissionError as _pe:
    # Under mail mediation the MSAL token cache is owner-owned; ea-bot cannot
    # read it BY DESIGN. Fail with guidance rather than a bare traceback: the
    # mediated CLI entrypoints are the supported path for ea-bot.
    raise PermissionError(
        f"{_pe}\n\n"
        "graph_client cannot be imported directly under mail mediation: "
        "the Graph token cache is owner-owned and this process cannot read "
        "it. Use the mediated CLI instead, e.g.\n"
        "  python3 preprocessor/graph_actions.py <verb> ...\n"
        "which forwards SAFE verbs to the owner-side mediator. There is no "
        "supported direct-import path for ea-bot."
    ) from None


def _cached_client_ids() -> set:
    """The client ids that hold a refresh token in the MSAL cache."""
    try:
        lookup = getattr(_cache, "search", None) or _cache.find  # find: msal before search
        return {rt.get("client_id") for rt in
                lookup(msal.TokenCache.CredentialType.REFRESH_TOKEN)}
    except Exception:
        return set()


def _client_choice() -> tuple[str, str]:
    """(path, client id) for the MSAL app: "custom" for the user's own
    registration (GRAPH_CLIENT_ID); "published" for the project's app
    (published_apps.PUBLISHED_GRAPH_CLIENT_ID) when the user configured
    none; else "graph_powershell", the earlier default. A cache whose
    refresh token came from the earlier default, and none from the
    published app, keeps the earlier default, so its silent sign-in goes
    on working."""
    path, cid = _published.graph_client_id(CUSTOM_CLIENT_ID)
    if path == "published":
        held = _cached_client_ids()
        if GRAPH_POWERSHELL_CLIENT_ID in held and cid not in held:
            path = ""
    if not path:
        return "graph_powershell", GRAPH_POWERSHELL_CLIENT_ID
    return path, cid


CLIENT_PATH, _CLIENT_ID = _client_choice()
if CLIENT_PATH == "published":
    WELL_KNOWN_CLIENTS = [("published", _CLIENT_ID)]
    # The published app signs in work and school accounts of any tenant;
    # a tenant the user named in GRAPH_AUTHORITY, or an explicit common,
    # stays.
    if (AUTHORITY == _DEFAULT_AUTHORITY
            and os.environ.get("GRAPH_AUTHORITY", "").strip() != _DEFAULT_AUTHORITY):
        AUTHORITY = _published.PUBLISHED_GRAPH_AUTHORITY


def _build_app(client_id: str):
    """Construct the right MSAL app for the configured auth method.

    A confidential client (with secret) for the auth-code + secret flow;
    otherwise a public client for the device-code / interactive flows.
    Both share the same on-disk token cache, so silent refresh works
    identically once a refresh token has been seeded.
    """
    if _use_confidential():
        return msal.ConfidentialClientApplication(
            client_id,
            authority=AUTHORITY,
            client_credential=GRAPH_CLIENT_SECRET,
            token_cache=_cache,
        )
    return msal.PublicClientApplication(
        client_id, authority=AUTHORITY, token_cache=_cache
    )


# Default app — rebuilt in the public-client fallback loop if needed.
_app = _build_app(WELL_KNOWN_CLIENTS[0][1])


def _confidential_auth_code_flow(scopes=None) -> str:
    """Seed a token via the OAuth2 authorization-code flow (confidential client).

    Opens the browser once for the user to sign in, catches the redirect on a
    local loopback server, and exchanges the code (with the client secret) for
    tokens. The refresh token is cached, so subsequent runs refresh silently
    via acquire_token_silent — no browser — which is what the headless/cron
    path relies on.

    This is the flow to use when the tenant permits an app registration but
    has "Allow public client flows" disabled (so device-code/interactive
    fail with AADSTS7000218). Requires GRAPH_CLIENT_SECRET and a redirect URI
    registered under the app's "Web" platform matching GRAPH_REDIRECT_URI.
    """
    import urllib.parse as _up

    scopes = scopes if scopes is not None else _effective_scopes()
    parsed = _up.urlparse(GRAPH_REDIRECT_URI)
    host, port = parsed.hostname or "localhost", parsed.port or 80

    flow = _app.initiate_auth_code_flow(scopes, redirect_uri=GRAPH_REDIRECT_URI)
    if "auth_uri" not in flow:
        raise RuntimeError(f"Could not start auth-code flow: {flow}")

    print(
        "\nOpen this URL in your browser to sign in "
        f"(confidential auth-code flow):\n{flow['auth_uri']}\n",
        file=sys.stderr,
    )
    _patch_wsl_browser()
    webbrowser.open(flow["auth_uri"])

    print(f"Waiting for the sign-in redirect on {host}:{port} ...", file=sys.stderr)
    captured = _await_redirect(host, port)  # blocks until the redirect arrives

    if "error" in captured:
        raise RuntimeError(
            f"Auth-code flow error: {captured.get('error')}: "
            f"{captured.get('error_description', '')}"
        )

    result = _app.acquire_token_by_auth_code_flow(flow, captured)
    if "access_token" not in result:
        raise RuntimeError(
            f"Token exchange failed: {result.get('error')}: "
            f"{result.get('error_description', '')}"
        )
    _save_cache(_cache)
    user = result.get("id_token_claims", {}).get("preferred_username", "?")
    print(f"Confidential auth-code sign-in successful for {user}.", file=sys.stderr)
    return result["access_token"]


def _await_redirect(host: str, port: int, timeout: Optional[float] = None) -> dict:
    """The query of the first request to a loopback server on host:port,
    which is the sign-in redirect; {} when none arrives within `timeout`
    seconds (None waits for ever)."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import urllib.parse as _up

    captured: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.update(_up.parse_qsl(_up.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Sign-in complete.</h2>"
                b"You can close this tab and return to the terminal.</body></html>"
            )

        def log_message(self, *_):
            pass  # silence default request logging

    server = HTTPServer((host, port), _Handler)
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()
    return captured


def setup_confidential_and_align() -> dict:
    """Confidential setup helper: sign in and align scopes to the app's grants.

    Signs in via the auth-code + secret flow requesting `.default` (which yields
    a token carrying exactly the delegated permissions consented for the app),
    reads the granted scopes from the token, and prunes the desired SCOPES
    superset to match. The caller persists GRAPH_SCOPES = ' '.join(aligned_short)
    so future runs request only the granted scopes.

    Returns {user, granted, aligned_short, dropped_short}.
    """
    token = _confidential_auth_code_flow(
        scopes=["https://graph.microsoft.com/.default"])
    claims = _decode_jwt_payload(token)
    granted = set(claims.get("scp", "").split())
    aligned_short = [_scope_basename(s) for s in align_scopes(granted, SCOPES)]
    dropped_short = [
        _scope_basename(s) for s in SCOPES
        if _scope_basename(s) not in aligned_short
        and _READ_FALLBACK.get(_scope_basename(s)) not in aligned_short
    ]
    return {
        "user": claims.get("preferred_username", "?"),
        "granted": sorted(granted),
        "aligned_short": aligned_short,
        "dropped_short": dropped_short,
    }


def _banner_wanted() -> bool:
    """Whether to announce which MSAL identity we are using.

    Useful to a human at a terminal, harmful to a machine reader: a caller
    that merges stdout and stderr gets the JSON payload followed by this
    line, and json.loads raises "Extra data".

    Printed when stderr is a terminal, or when EA_DEBUG is set for a
    non-interactive run that wants it back.
    """
    if os.environ.get("EA_DEBUG"):
        return True
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


# Set by this file's command line, which is a sign-in the user asked for:
# its device-code and browser flows print a link and wait, and read nothing
# from the keyboard, so they run also with no terminal (a `!` command in
# Claude Code). A library caller keeps the terminal check in authenticate().
_SIGN_IN_REQUESTED = False


def authenticate() -> str:
    """
    Get a valid access token. Tries in order:
      1. MSAL silent (cached refresh token in msal_token_cache.json) —
         standard MSAL operation. Token rotation is handled internally
         by the cache. Works for both public and confidential clients.
      2. Auth sources of enabled owner-zone plugins (docs/plugins.md), such
         as a token harvested from Outlook on the web for a tenant without
         an app registration.
      3. First-run / re-auth interactive seeding, by configured method:
         - confidential: auth-code + client secret (public client flows OFF)
         - the project's published app (no GRAPH_CLIENT_ID): auth-code with
           PKCE on a loopback redirect, or a device code with no browser
         - otherwise: MSAL interactive (browser) with device-code fallback
    """
    # 1. MSAL silent path — use cached refresh token
    accounts = _app.get_accounts()
    if accounts:
        result = _app.acquire_token_silent(_effective_scopes(), account=accounts[0])
        if result and "access_token" in result:
            _save_cache(_cache)
            if not getattr(authenticate, "_printed", False) and _banner_wanted():
                user = result.get("id_token_claims", {}).get(
                    "preferred_username", accounts[0].get("username", "?"))
                print(f"Using MSAL token for {user}.", file=sys.stderr)
                authenticate._printed = True
            return result["access_token"]

    # 2. Auth sources from enabled owner-zone plugins (docs/plugins.md),
    #    loaded only in an owner process, from the owner's private dir.
    token = _source_token()
    if token:
        return token

    # Interactive path — skip entirely in headless/non-TTY environments
    # (e.g., subprocess launched by Slack bot). Hanging here burns the
    # entire subprocess timeout (480s) and fails silently.
    if not sys.stdin.isatty() and not _SIGN_IN_REQUESTED:
        raise RuntimeError(
            "Graph token expired and no refresh token available. "
            "Run `python graph_client.py` interactively to re-authenticate, "
            "or refresh the token an owner plugin's auth source supplies "
            "(docs/plugins.md)"
        )

    # Confidential client: single auth-code + secret sign-in (no well-known
    # client loop — we use the registered app + secret directly).
    if _use_confidential():
        return _confidential_auth_code_flow()

    # The project's published app, when the user registered none.
    if CLIENT_PATH == "published":
        return _published_seed()

    return _interactive_seed()


def _interactive_seed(seed_scopes=None, clients=None,
                      authority=None) -> str:
    """First-run / re-consent seeding via the public-client flows.

    Defaults request only the mail-family scopes: mixing an unconsented
    chat scope into a mail re-auth sends the user into the tenant's
    admin-consent justification wall and blocks MAIL recovery.
    GRAPH_AUTH_METHOD=device_code skips the browser attempt outright,
    because over SSH there is no display and the browser failure does not
    match the Entra error codes the automatic fallback keys on.
    """
    _patch_wsl_browser()

    if seed_scopes is None:
        seed_scopes = _effective_scopes()
    if clients is None:
        clients = (
            [("env", os.environ["GRAPH_CLIENT_ID"])]
            if os.environ.get("GRAPH_CLIENT_ID")
            else WELL_KNOWN_CLIENTS
        )

    force_device = GRAPH_AUTH_METHOD == "device_code"
    last_error = "no clients tried"
    for name, cid in clients:
        app = msal.PublicClientApplication(
            cid, authority=authority or AUTHORITY, token_cache=_cache)

        result = {}
        if force_device:
            print(f"Device-code auth via {name} ({cid[:8]}...)",
                  file=sys.stderr)
        else:
            # Try interactive (browser) first
            print(f"Trying {name} ({cid[:8]}...) — browser auth...", file=sys.stderr)
            try:
                result = app.acquire_token_interactive(seed_scopes)
            except Exception as e:
                last_error = str(e)
                print(f"  Interactive failed ({last_error[:80]}), trying device code...", file=sys.stderr)
                result = {}

        # Fall back to device code if interactive failed or returned an error
        if "access_token" not in result:
            err = result.get("error_description", last_error)
            # AADSTS900144 = scope missing in interactive URL (WSL/client issue)
            # AADSTS1001010 = service principal not in tenant
            if force_device or any(
                    code in err for code in ("900144", "1001010", "1002012")):
                if not force_device:
                    print(f"  Falling back to device code flow...", file=sys.stderr)
                flow = app.initiate_device_flow(seed_scopes)
                if "user_code" not in flow:
                    last_error = str(flow)
                    if "1001010" in last_error:
                        print(f"  {name} not in tenant, trying next client...", file=sys.stderr)
                        continue
                    break
                print(flow["message"], file=sys.stderr)
                result = app.acquire_token_by_device_flow(flow)

        if "access_token" in result:
            _save_cache(_cache)
            return result["access_token"]

        last_error = result.get("error_description", str(result))
        if "1001010" in last_error:
            print(f"  {name} not in tenant, trying next...", file=sys.stderr)
            continue
        break

    raise RuntimeError(f"Authentication failed: {last_error}")


# -------------------------------------------------------------------
# The project's published app (docs/published-oauth-apps.md)
# -------------------------------------------------------------------
# How long the browser sign-in waits for the redirect before it gives the
# user a device code instead.
_PUBLISHED_BROWSER_WAIT = 300

# Entra error codes a sign-in through the published app can end in, with
# the step that fixes each.
_PUBLISHED_ERRORS = (
    (("AADSTS65001", "AADSTS90094", "AADSTS90095"),
     "your institution has not consented to the Tacitus app. Send your IT group "
     "docs/it-request.md, 'No registration: consent to the Tacitus app'"),
    (("AADSTS50105",),
     "your institution requires assignment to the Tacitus app; ask your IT group "
     "to assign you to it"),
    (("AADSTS53003",),
     "a Conditional Access policy blocked this sign-in (often the device-code "
     "flow). Sign in with a browser on this machine, or through an SSH tunnel "
     "(docs/SETUP.md 'Option D: no registration (the published app)')"),
)


def _browser_available() -> bool:
    """Whether a browser can open on this machine and reach its loopback
    address: on a Mac or under WSL unless over SSH, on Linux only with a
    display, and elsewhere where Python finds a browser."""
    ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if sys.platform == "darwin" or _is_wsl():
        return not ssh
    if sys.platform.startswith("linux") and not display:
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def _published_flow() -> str:
    """The first sign-in flow for the published app: "browser" (the
    authorization-code flow with PKCE on a loopback redirect) when a
    browser can reach this machine, or when GRAPH_AUTH_METHOD=interactive
    says the user opens the link through a tunnel; "device_code" when
    GRAPH_AUTH_METHOD=device_code or no browser can."""
    if GRAPH_AUTH_METHOD == "device_code":
        return "device_code"
    if GRAPH_AUTH_METHOD == "interactive" or _browser_available():
        return "browser"
    return "device_code"


def _published_redirect_uri() -> str:
    """http://localhost:<port>, the redirect the published app registers
    (Entra ignores a localhost redirect's port): the port GRAPH_REDIRECT_URI
    names, else 8400. Never its path, which the registration lacks."""
    import urllib.parse as _up
    port = _up.urlparse(GRAPH_REDIRECT_URI).port
    return f"http://localhost:{port or _published.PUBLISHED_GRAPH_REDIRECT_PORT}"


def _loopback_sign_in(app, scopes: list, redirect_uri: str,
                      wait: Optional[float] = None) -> dict:
    """A public client's authorization-code sign-in with PKCE (MSAL makes
    the code verifier) on a loopback redirect: print the link, open a
    browser, catch the redirect, redeem the code. The MSAL result, or
    {"error": "timeout"} when no redirect came within `wait` seconds."""
    import urllib.parse as _up

    flow = app.initiate_auth_code_flow(scopes, redirect_uri=redirect_uri)
    if "auth_uri" not in flow:
        return {"error": "flow", "error_description": str(flow)}
    print(f"\nOpen this URL in your browser to sign in:\n{flow['auth_uri']}\n",
          file=sys.stderr)
    _patch_wsl_browser()
    try:
        webbrowser.open(flow["auth_uri"])
    except Exception:
        pass  # the link is printed above; a browser elsewhere works through a tunnel
    parsed = _up.urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80
    print(f"Waiting for the sign-in redirect on {host}:{port} ...", file=sys.stderr)
    captured = _await_redirect(host, port, timeout=wait)
    if not captured:
        return {"error": "timeout",
                "error_description": f"no redirect within {wait} seconds"}
    if "error" in captured:
        return captured
    try:
        return app.acquire_token_by_auth_code_flow(flow, captured)
    except ValueError as e:  # a state that does not match: not this sign-in's redirect
        return {"error": "state", "error_description": str(e)}


def _published_failure(result: dict) -> str:
    """The error a failed published-app sign-in raises, with the next step
    when the Entra code names one."""
    err = f"{result.get('error', '')}: {result.get('error_description', result)}"
    for codes, fix in _PUBLISHED_ERRORS:
        if any(code in err for code in codes):
            return f"Authentication failed: {err}\n  Next step: {fix}."
    return f"Authentication failed: {err}"


def _published_seed(scopes=None) -> str:
    """First sign-in through the project's published app, a public client
    with no secret: a browser sign-in on the loopback redirect, which falls
    back to a device code when the browser never redirects; or a device
    code at once when no browser can reach this machine. An error from
    Entra (a consent the tenant has not given, a blocked flow) is final:
    another flow meets the same policy."""
    scopes = scopes if scopes is not None else _effective_scopes()
    result: dict = {}
    if _published_flow() == "browser":
        try:
            result = _loopback_sign_in(_app, scopes, _published_redirect_uri(),
                                       wait=_PUBLISHED_BROWSER_WAIT)
        except OSError as e:  # the port is taken, or no loopback here
            result = {"error": "loopback", "error_description": str(e)}
        if result.get("error") in ("timeout", "loopback"):
            print(f"  The browser sign-in did not finish "
                  f"({str(result.get('error_description'))[:100]}); "
                  "using a device code instead.", file=sys.stderr)
            result = {}
        elif "access_token" not in result:
            raise RuntimeError(_published_failure(result))
    if not result:
        flow = _app.initiate_device_flow(scopes)
        if "user_code" not in flow:
            raise RuntimeError(_published_failure(flow))
        print(flow["message"], file=sys.stderr)
        result = _app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(_published_failure(result))
    _save_cache(_cache)
    user = result.get("id_token_claims", {}).get("preferred_username", "?")
    print(f"Signed in as {user} through the Tacitus app.", file=sys.stderr)
    return result["access_token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {authenticate()}",
        "Content-Type": "application/json",
    }


# -------------------------------------------------------------------
# Generic Graph helpers
# -------------------------------------------------------------------
def _normalize(obj):
    """Outlook REST keys are PascalCase, Graph keys are camelCase. Normalize
    to camelCase so the rest of the code works unchanged. Idempotent: an
    already-camelCase key has a lower first character already.
    """
    if isinstance(obj, dict):
        return {k[0].lower() + k[1:]: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(item) for item in obj]
    return obj


# Microsoft Graph is temporarily overloaded or rate-limiting us. Worth
# retrying, because the next attempt has a real chance of succeeding. Any
# other 4xx/5xx is terminal: retrying will not change the outcome.
_GRAPH_RETRY_STATUSES = {429, 502, 503, 504}

# Backoff in seconds, indexed by attempt number (1-based). The ~5 minute
# cumulative wait outlasts Graph's peak-load spikes.
_GRAPH_RETRY_BACKOFF = [5, 15, 45, 90, 180]


# Circuit breaker: per-call retry handles a 30-60s blip; the breaker handles
# "Graph has been down all morning" by failing fast instead of re-walking the
# ~5-min backoff ladder on every cron job. State is shared across processes
# via ~/.ea/state/circuit-breakers.json; sentinel.py alerts when open.
try:
    from retry_policy import CircuitBreaker
    _graph_breaker = CircuitBreaker("graph")
except Exception:  # retry_policy missing in stripped deploys — degrade
    _graph_breaker = None


_PATH_REFUSED = re.compile(r"[?#%\\\s\x00-\x1f\x7f]")
# The characters of a Graph id: the mediator's plain-id test
# (mail_mediation._ID_RE and _LONG_ID_RE), kept here so this module needs no
# import of the mediation layer.
_PLAIN_ID_RE = re.compile(r"[A-Za-z0-9_=+-]{1,512}")
_LONG_PLAIN_ID_RE = re.compile(r"[A-Za-z0-9_=+-]{1,4096}")


def _id(value, *, long: bool = False) -> str:
    """`value` when it is a plain Graph id, else ValueError. Every id this
    module puts into a request path passes here first: _check_endpoint sees
    only the whole path, where a slash inside an id reads as one more
    segment and could make a call on one object reach another endpoint."""
    if isinstance(value, str) and (_LONG_PLAIN_ID_RE if long else _PLAIN_ID_RE).fullmatch(value):
        return value
    raise ValueError(f"refused Graph id {str(value)[:80]!r}: not a plain id")


def _check_endpoint(endpoint: str) -> str:
    """The URL of a Graph request, refused unless the path is plain.

    Ids are put into the path as they are, and some arrive from files the
    resident can write, so a path may not carry a query, a fragment, a
    percent escape, a backslash, whitespace or a dot segment: any of those
    could make a call on one object reach another endpoint. An absolute URL
    (a nextLink from Graph) must be on the API host the token is for."""
    if endpoint.startswith("http"):
        base = next((b for b in (GRAPH_URL, OUTLOOK_REST_URL)
                     if endpoint.startswith(b + "/")), None)
        if base is None:
            raise ValueError("Graph URL is not on the API host")
        path = endpoint[len(base):].split("?", 1)[0]
        url = endpoint
    else:
        path = endpoint
        url = f"{_api_base()}{endpoint}"
    if (not path.startswith("/") or _PATH_REFUSED.search(path)
            or any(seg in (".", "..") for seg in path.split("/"))):
        raise ValueError(f"refused Graph path {path[:120]!r}")
    return url


def graph_get(endpoint: str, params: Optional[dict] = None,
               _retries: int = len(_GRAPH_RETRY_BACKOFF) + 1) -> dict:
    """GET request to Graph/Outlook API with retry on transient errors.

    Retries a network blip and the transient statuses in
    _GRAPH_RETRY_STATUSES, honouring Retry-After when Graph sends one.
    Other HTTP errors (4xx auth/not-found, 500 internal) are terminal and
    propagate immediately.

    Raises CircuitOpen immediately (no network call) while the Graph
    breaker is open, i.e. after 3 consecutive calls exhausted their full
    retry ladders within the cooldown window.
    """
    if _graph_breaker is not None:
        _graph_breaker.raise_if_open()
    url = _check_endpoint(endpoint)
    for attempt in range(1, _retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=60)
            resp.raise_for_status()
            result = _normalize(resp.json())
            if _graph_breaker is not None:
                _graph_breaker.record_success()
            return result
        except (requests.exceptions.ConnectionError,
                 requests.exceptions.Timeout) as e:
            if attempt == _retries:
                if _graph_breaker is not None:
                    _graph_breaker.record_failure(f"timeout: {e}")
                raise
            wait = _GRAPH_RETRY_BACKOFF[attempt - 1]
            print(f"  Graph API timeout (attempt {attempt}/{_retries}), "
                  f"retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status not in _GRAPH_RETRY_STATUSES or attempt == _retries:
                if status in _GRAPH_RETRY_STATUSES and _graph_breaker is not None:
                    # transient class, retries exhausted -> counts toward
                    # the breaker. Terminal 4xx/500 do NOT count: they are
                    # caller errors, not service-down evidence.
                    _graph_breaker.record_failure(f"HTTP {status} after "
                                                  f"{attempt} attempts")
                raise
            # Honor Retry-After if Graph supplied one (mostly for 429),
            # else fall back to our schedule. Cap at 5 min.
            ra = (e.response.headers.get("Retry-After")
                  if e.response is not None else None)
            if ra and ra.isdigit():
                wait = min(int(ra), 300)
            else:
                wait = _GRAPH_RETRY_BACKOFF[attempt - 1]
            print(f"  Graph API HTTP {status} (attempt {attempt}/{_retries}), "
                  f"retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)


def graph_get_all(endpoint: str, params: Optional[dict] = None) -> list:
    """GET with automatic pagination – returns all items."""
    items = []
    data = graph_get(endpoint, params)
    items.extend(data.get("value", []))
    while "@odata.nextLink" in data:
        data = graph_get(data["@odata.nextLink"])
        items.extend(data.get("value", []))
    return items


def _pascalize(obj):
    """Convert camelCase keys to PascalCase recursively. Outlook REST
    requires PascalCase in request bodies where Graph uses camelCase.
    Idempotent on PascalCase input.
    """
    if isinstance(obj, dict):
        return {k[0].upper() + k[1:]: _pascalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_pascalize(item) for item in obj]
    return obj


_MOVE_RE = re.compile(r"^/me/messages/(.+)/move$")


def graph_post(endpoint: str, body: dict) -> dict:
    url = _check_endpoint(endpoint)
    # Outlook REST API requires PascalCase in the request body
    send_body = _pascalize(body) if _api_base() == OUTLOOK_REST_URL else body
    resp = requests.post(url, headers=_headers(), json=send_body, timeout=30)
    resp.raise_for_status()
    result = _normalize(resp.json()) if resp.content else {}
    move = _MOVE_RE.match(endpoint or "")
    if move:
        # Every move the EA makes is recorded, so ledger_sync never reads
        # the EA's own archive as the user's answer to a row (ea_moves).
        try:
            try:
                import ea_moves as _ea_moves
            except ModuleNotFoundError:
                from preprocessor import ea_moves as _ea_moves
            _ea_moves.record(move.group(1), (result or {}).get("id", ""),
                             str((body or {}).get("destinationId")
                                 or (body or {}).get("DestinationId") or ""),
                             (result or {}).get("conversationId", ""))
        except Exception:
            pass
    return result


def graph_patch(endpoint: str, body: dict) -> dict:
    url = _check_endpoint(endpoint)
    send_body = _pascalize(body) if _api_base() == OUTLOOK_REST_URL else body
    resp = requests.patch(url, headers=_headers(), json=send_body, timeout=30)
    resp.raise_for_status()
    return _normalize(resp.json()) if resp.content else {}


def mark_message_read(message_id: str) -> dict:
    """Mark a message read in place. Does NOT move or archive it."""
    graph_patch(f"/me/messages/{_id(message_id)}", {"isRead": True})
    return {"marked_read": True, "message_id": message_id}


def graph_delete(endpoint: str) -> None:
    """DELETE request to Graph/Outlook API. Returns nothing on success."""
    url = _check_endpoint(endpoint)
    resp = requests.delete(url, headers=_headers(), timeout=30)
    resp.raise_for_status()


# -------------------------------------------------------------------
# Mail
# -------------------------------------------------------------------
MAIL_FIELDS = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,bodyPreview,"
    "hasAttachments,conversationId,flag,isRead,importance,categories,"
    "isDraft"
)

MAIL_FIELDS_FULL = MAIL_FIELDS + ",body"

# The names Graph takes in /me/mailFolders/{name} as they are. A folder the
# user made (admissions-inquiries, a project folder) has no such name: the
# path needs its id, which resolve_folder looks up.
WELL_KNOWN_FOLDERS = frozenset({
    "archive", "clutter", "conflicts", "conversationhistory", "deleteditems",
    "drafts", "inbox", "junkemail", "localfailures", "msgfolderroot", "outbox",
    "recoverableitemsdeletions", "scheduled", "searchfolders", "sentitems",
    "serverfailures", "syncissues",
})
_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")
_FOLDER_IDS: dict[str, str] = {}


_FOLDER_TREE_DEPTH = 8


def _folder_tree() -> list[tuple[str, str]]:
    """(path, id) of every mail folder, parents first, the path joined
    with "/" ("Research/PhD Candidates")."""
    # shortcut: 200 folders per parent, one page; page when a mailbox has more.
    out, level = [], [("/me/mailFolders", "")]
    for _ in range(_FOLDER_TREE_DEPTH):
        nxt = []
        for endpoint, prefix in level:
            data = graph_get(endpoint, {"$select": "id,displayName,childFolderCount", "$top": 200})
            for f in data.get("value", []):
                name = (f.get("displayName") or "").strip()
                if not (name and f.get("id")):
                    continue
                path = f"{prefix}/{name}" if prefix else name
                out.append((path, f["id"]))
                if f.get("childFolderCount"):
                    nxt.append((f"/me/mailFolders/{f['id']}/childFolders", path))
        if not nxt:
            break
        level = nxt
    return out


def resolve_folder(folder: str) -> str:
    """The /me/mailFolders path segment for `folder`. A well-known name or a
    folder id passes as it is. Any other name is a folder the user made, at
    any depth, matched case-insensitively by its path ("Research/PhD
    Candidates") or by its own name. Raises LookupError when no folder has
    the name, or when several do and only the path can tell them apart."""
    name = (folder or "").strip()
    if name.lower() in WELL_KNOWN_FOLDERS:
        return name.lower()
    if _FOLDER_ID_RE.match(name):
        return name
    key = name.strip("/").lower()
    if key not in _FOLDER_IDS:
        tree = _folder_tree()
        hits = ([(p, i) for p, i in tree if p.lower() == key]
                or [(p, i) for p, i in tree if p.rsplit("/", 1)[-1].lower() == key])
        if not hits:
            raise LookupError(f"no mail folder named {name!r}")
        if len(hits) > 1:
            raise LookupError(f"several mail folders are named {name!r}: "
                              + ", ".join(p for p, _ in hits) + "; pass the path")
        _FOLDER_IDS[key] = hits[0][1]
    return _FOLDER_IDS[key]


def get_messages(
    folder: str = "inbox",
    days_back: Optional[int] = None,
    unread_only: bool = False,
    full_body: bool = False,
) -> list:
    """Fetch messages from a mail folder.

    If days_back is None, fetches ALL messages in the folder (with pagination).
    That is correct for inbox: the inbox IS the task queue. Use days_back only
    for cross-reference folders like sent mail.
    """
    filters = []
    if days_back is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        filters.append(f"receivedDateTime ge {since}")
    if unread_only:
        filters.append("isRead eq false")
    params = {
        "$select": MAIL_FIELDS_FULL if full_body else MAIL_FIELDS,
        "$orderby": "receivedDateTime desc",
        "$top": 250,  # page size, not a cap — graph_get_all paginates
    }
    if filters:
        params["$filter"] = " and ".join(filters)
    return graph_get_all(f"/me/mailFolders/{resolve_folder(folder)}/messages", params)


def get_entire_inbox(**kwargs) -> list:
    """Read the ENTIRE inbox. No time filter, no cap.

    The inbox is the user's task queue, so everything in it is potentially
    actionable. Pagination handles any size.
    """
    return get_messages("inbox", days_back=None, **kwargs)


def get_sent(days_back: int = 14, **kwargs) -> list:
    """Sent mail with a time window, for cross-referencing (did we reply to
    this thread?). Not a task queue, so a window is appropriate."""
    return get_messages("sentitems", days_back=days_back, **kwargs)


def get_flagged() -> list:
    """Flagged messages across all folders. Graph-only filter — returns [] on Outlook REST API."""
    if _api_base() == OUTLOOK_REST_URL:
        return []
    # Note: Graph API rejects $filter on flag/flagStatus combined with
    # $orderby (returns "restriction or sort order is too complex").
    # Fetch without ordering and sort client-side.
    params = {
        "$filter": "flag/flagStatus eq 'flagged'",
        "$select": MAIL_FIELDS,
        "$top": 250,
    }
    results = graph_get_all("/me/messages", params)
    results.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return results


def get_message_full(message_id: str) -> dict:
    """Fetch a single message with full body."""
    return graph_get(f"/me/messages/{_id(message_id)}", {"$select": MAIL_FIELDS_FULL})


def get_attachments(message_id: str) -> list:
    return graph_get_all(f"/me/messages/{_id(message_id)}/attachments")


def list_folder_messages(folder: str = "inbox", top: int = 25,
                         order_by: str = "receivedDateTime",
                         since: Optional[str] = None) -> tuple[list, bool]:
    """Newest-first page of ONE folder, ordered server-side.

    The cheap answer to "what is newest in <folder>?": a single GET whose
    ordering and cap Graph applies before it sends anything back.
    get_messages() paginates the whole folder and search_messages() runs a
    mailbox-wide $search, and both take minutes on sentitems.

    order_by is the datetime property to sort (and optionally filter) on:
    receivedDateTime for delivered folders, sentDateTime for sent mail.
    `since` is a Graph datetime literal (e.g. 2026-07-01T00:00:00Z);
    filtering the SAME property we sort on is the combination Graph
    accepts (mixing properties trips "restriction or sort order is too
    complex" — see get_flagged).

    Returns (results, has_more) like search_messages.
    """
    params = {
        "$select": MAIL_FIELDS,
        "$orderby": f"{order_by} desc",
        "$top": top,
    }
    if since:
        params["$filter"] = f"{order_by} ge {since}"
    data = graph_get(f"/me/mailFolders/{resolve_folder(folder)}/messages", params)
    has_more = "@odata.nextLink" in data
    return data.get("value", []), has_more


def search_messages(query: str, top: int = 25, folder: str | None = None) -> tuple[list, bool]:
    """Full-text search across mailbox, or inside one folder (a well-known
    name or one the user made, see resolve_folder; LookupError if none has
    the name). A folder search does not descend into its subfolders.

    Returns (results, has_more), at most `top` results per call. Paginating a
    broad query returns 200+ results, takes 100+ seconds and produces 170KB+
    of JSON that bloats LLM context, so this does not paginate.
    """
    params = {"$search": f'"{query}"', "$top": top, "$select": MAIL_FIELDS}
    endpoint = (f"/me/mailFolders/{resolve_folder(folder)}/messages" if folder
                else "/me/messages")
    data = graph_get(endpoint, params)
    has_more = "@odata.nextLink" in data
    return data.get("value", []), has_more


def _normalize_plaintext_quoted(quoted_html: str) -> str:
    """If the quoted section is plain text wrapped in <body> (no block tags),
    convert its newlines to <br> so it renders correctly under HTML contentType.

    Graph's createReplyAll returns contentType=html wrapping the original
    plain-text body in <html><body>…</body></html> — but it does NOT insert
    <br> between the plain-text lines. When we later PATCH the draft as HTML,
    those raw newlines collapse into a single run-on paragraph.
    """
    body_match = re.search(r"(<body[^>]*>)(.*)(</body>)",
                           quoted_html, re.IGNORECASE | re.DOTALL)
    if not body_match:
        return quoted_html
    body_open, inner, body_close = body_match.group(1), body_match.group(2), body_match.group(3)
    has_block_tags = re.search(r"<(br|p|div|li|tr|h[1-6]|pre|blockquote)\b",
                               inner, re.IGNORECASE)
    if has_block_tags or not ("\n" in inner or "\r" in inner):
        return quoted_html
    normalized = inner.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return (quoted_html[:body_match.start()]
            + body_open + normalized + body_close
            + quoted_html[body_match.end():])


# A body declared as Text but written with HTML break tags. Escaping those
# faithfully, which is what a plain-text renderer should do, puts a literal
# "&lt;br&gt;" in front of the recipient. Fold the tags back into newlines
# before escaping so either convention renders.
_AUTHORED_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_AUTHORED_BLOCK_CLOSE_RE = re.compile(r"(?i)</\s*(?:p|div)\s*>")
_AUTHORED_BLOCK_OPEN_RE = re.compile(r"(?i)<\s*(?:p|div)(?:\s[^>]*)?>")


# A line at or past this length that runs straight into the next one is a
# hard wrap, not a break someone wanted. Real intentional short lines (a
# time option, an address, "Thanks," over a name) sit well under it.
_WRAP_JOIN_MIN = 66
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\(?\d+[.)]|[a-z][.)])\s+")


def _unwrap_hard_wrapped(paragraph: str) -> str:
    """Rejoin lines that were wrapped for source width, keep real breaks.

    The renderer turns every newline inside a paragraph into <br>, so a body
    wrapped at some column arrives broken mid-sentence. Only a line long
    enough to have BEEN wrapped is joined, which leaves a list of proposed
    times or a signature alone.
    """
    lines = paragraph.split("\n")
    if len(lines) < 2:
        return paragraph
    out = [lines[0]]
    for line in lines[1:]:
        prev = out[-1]
        joinable = (
            len(prev.rstrip()) >= _WRAP_JOIN_MIN
            and line.strip()
            and not _LIST_MARKER_RE.match(line)
            and not _LIST_MARKER_RE.match(prev)
            and not prev.rstrip().endswith(":")
        )
        if joinable:
            out[-1] = prev.rstrip() + " " + line.lstrip()
        else:
            out.append(line)
    return "\n".join(out)


def _normalize_authored_text(text: str) -> str:
    """Plain-text body, whichever line-break convention the author used.

    Only the block tags are touched. A stray "a < b" is left alone, so
    prose that merely contains an angle bracket still escapes normally.
    """
    if not text or "<" not in text:
        return text
    normalized = _AUTHORED_BR_RE.sub("\n", text)
    normalized = _AUTHORED_BLOCK_CLOSE_RE.sub("\n\n", normalized)
    normalized = _AUTHORED_BLOCK_OPEN_RE.sub("", normalized)
    # Collapse the runs those substitutions can create, so a <div>-wrapped
    # paragraph does not turn into three blank lines.
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip("\n") if normalized.strip() else text


def _render_reply_html(new_body: str, content_type: str = "Text") -> str:
    """Render the NEW reply portion (no quoted thread) as Outlook-native HTML.

    Converts plain text to one <div> per paragraph with empty <div><br></div>
    spacers. Graph's createReply ships a "<style>p{margin:0}</style>" that
    collapses vertical spacing between <p> tags — Outlook itself side-steps
    this by using <div class="elementToProof"> with explicit spacer divs, so
    we match that format. This also keeps in-Outlook editing clean: breaking
    one of our divs into two still produces divs (not nested <p> soup).
    """
    import html as html_mod

    if content_type != "Text":
        return new_body
    paragraphs = _normalize_authored_text(new_body).split("\n\n")
    parts = []
    for i, para in enumerate(paragraphs):
        para = _unwrap_hard_wrapped(para)
        escaped = html_mod.escape(para).replace("\n", "<br>")
        parts.append(f'<div>{escaped}</div>')
        if i < len(paragraphs) - 1:
            parts.append('<div><br></div>')
    return "".join(parts)


def _split_quoted_thread(current_body: str) -> int | None:
    """Index in `current_body` where the quoted thread begins, or None.

    Outlook's sanitizer strips standalone HTML comments from stored message
    bodies, so the EA_REPLY_START/END markers `_build_reply_html` emits do NOT
    survive a round trip through Graph — anything anchored on them silently
    falls through. Anchor on structure Outlook actually keeps: the
    `divRplyFwdMsg` block it wraps quoted threads in, and the <hr> rule it
    places immediately above that.
    """
    fwd = re.search(r'<div\s+id=["\']?divRplyFwdMsg', current_body, re.IGNORECASE)
    if fwd:
        # Outlook puts an <hr> just above the header block; cut above it so
        # repeated edits don't strand a separator in the reply portion.
        window = current_body[max(0, fwd.start() - 300):fwd.start()]
        hr = None
        for m in re.finditer(r"<hr[\s/>]", window, re.IGNORECASE):
            hr = m
        if hr:
            return max(0, fwd.start() - 300) + hr.start()
        return fwd.start()

    # No header block (some plain-text originals): fall back to the first
    # rule after <body>, which createReply uses as the thread separator.
    body_m = re.search(r"<body[^>]*>", current_body, re.IGNORECASE)
    start = body_m.end() if body_m else 0
    hr = re.search(r"<hr[\s/>]", current_body[start:], re.IGNORECASE)
    return start + hr.start() if hr else None


def _build_reply_html(new_body: str, quoted_html: str, content_type: str = "Text") -> str:
    """Combine new reply text with the quoted thread from createReply.

    The Graph API's createReply returns a draft whose body contains the
    quoted original thread (with headers like From/Sent/To/Subject).
    We prepend our reply text with a marker so update_draft can later
    replace just the reply portion without touching the quoted thread.
    """
    # When the original message was plain text, createReplyAll returns
    # HTML with a plain-text interior and no <br> tags. Normalize first so
    # the quoted section renders with line breaks intact.
    quoted_html = _normalize_plaintext_quoted(quoted_html)

    reply_html = _render_reply_html(new_body, content_type)

    marked_reply = (
        f"<!-- EA_REPLY_START -->\n{reply_html}\n<!-- EA_REPLY_END -->"
    )

    # The quoted_html is typically a full HTML document from Outlook.
    # Insert our reply at the TOP, before any quoted content.
    # Priority order matters: appendonsend can appear mid-thread (between
    # the most recent quoted message and earlier ones), so inserting there
    # puts the reply in the middle of the body. Instead, insert right after
    # <body> to guarantee the reply is at the top.
    body_match = re.search(r"(<body[^>]*>)", quoted_html, re.IGNORECASE)
    if body_match:
        pos = body_match.end()
        return quoted_html[:pos] + marked_reply + "<br>" + quoted_html[pos:]

    hr_match = re.search(r"<hr[\s>]", quoted_html, re.IGNORECASE)
    if hr_match:
        pos = hr_match.start()
        return quoted_html[:pos] + marked_reply + "<br>" + quoted_html[pos:]

    # Fallback: just prepend
    return marked_reply + "<br><br>" + quoted_html


def _is_quoted_thread_flattened(quoted_html: str) -> bool:
    """Detect when Graph's createReply returned a quoted thread that
    will render as a run-on wall of text when saved as HTML body.

    Two failure modes for replies to plain-text originals:

    1. Graph returns raw plain text (no <html>/<body>/<br>, just
       \\r\\n-separated lines). Stored as an HTML-content-type body, the
       newlines are ignored at render time and everything collapses onto
       one line.

    2. Graph returns a proper HTML skeleton but stuffs the plain-text
       original inside without converting \\r\\n to <br>. Same outcome.

    Both cases need `_build_quoted_thread_fallback` to reconstruct the
    quoted block from the original message with proper line breaks.
    """
    if not quoted_html.strip():
        return False  # empty is a different case, handled by empty-check
    body_match = re.search(r"<body[^>]*>(.*?)</body>", quoted_html,
                            re.IGNORECASE | re.DOTALL)
    if not body_match:
        # Case 1: no <body> tag at all. If the response has newlines
        # but no <br> / </div> / </p> tags, it will render as a
        # single run-on line.
        has_newlines = "\n" in quoted_html
        has_br = bool(re.search(r"<br\s*/?>", quoted_html, re.IGNORECASE))
        has_block = bool(re.search(r"</(div|p)\s*>", quoted_html,
                                    re.IGNORECASE))
        return has_newlines and not (has_br or has_block)
    # Case 2: has <body> but little-to-no break structure inside.
    body_content = body_match.group(1)
    text = re.sub(r"<[^>]+>", "", body_content).strip()
    if len(text) < 200:
        return False  # too short to distinguish from a terse reply
    breaks = len(re.findall(r"<br\s*/?>", body_content, re.IGNORECASE))
    blocks = len(re.findall(r"</(div|p|tr|li|h[1-6])\s*>", body_content,
                            re.IGNORECASE))
    return (breaks + blocks) <= 2


def _plain_text_to_html_paragraphs(text: str) -> str:
    """Convert plain-text body into Outlook-native divs, preserving both
    paragraph boundaries (blank lines) and soft line breaks.

    Matches the shape emitted by `_build_reply_html` for our own reply
    text so the quoted thread renders consistently with the new reply
    above it.
    """
    import html as html_mod
    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _normalize_authored_text(text)
    paragraphs = text.split("\n\n")
    parts = []
    for i, para in enumerate(paragraphs):
        if not para.strip():
            parts.append("<div><br></div>")
            continue
        para = _unwrap_hard_wrapped(para)
        escaped = html_mod.escape(para).replace("\n", "<br>")
        parts.append(f"<div>{escaped}</div>")
        if i < len(paragraphs) - 1:
            parts.append("<div><br></div>")
    return "".join(parts)


def _build_quoted_thread_fallback(original_id: str) -> str:
    """Build a quoted thread HTML from the original message when
    `createReply` either returned nothing or returned a flattened
    run-on string (see `_is_quoted_thread_flattened`).

    Constructs a standard Outlook-style From/Sent/To/Subject header
    plus the original body, with plain-text bodies rewritten to
    Outlook-native divs so `\\r\\n` line breaks actually render.
    """
    import html as html_mod
    try:
        orig = get_message_full(original_id)
        from_addr = orig.get("from", {}).get("emailAddress", {})
        from_name = from_addr.get("name", from_addr.get("address", ""))
        sent = orig.get("receivedDateTime", "")[:16].replace("T", " ")
        to_list = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in orig.get("toRecipients", [])
        )
        cc_list = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in orig.get("ccRecipients", [])
        )
        subject = orig.get("subject", "")
        orig_body = orig.get("body", {}).get("content", "")
        orig_type = orig.get("body", {}).get("contentType", "text")

        if orig_type.lower() == "text":
            # Graph's own reply-builder drops the line breaks of a text
            # body; this preserves them.
            orig_body = _plain_text_to_html_paragraphs(orig_body)

        header_lines = [
            f'<b>From:</b> {html_mod.escape(from_name)}<br>',
            f'<b>Sent:</b> {html_mod.escape(sent)}<br>',
            f'<b>To:</b> {html_mod.escape(to_list)}<br>',
        ]
        if cc_list:
            header_lines.append(f'<b>Cc:</b> {html_mod.escape(cc_list)}<br>')
        header_lines.append(f'<b>Subject:</b> {html_mod.escape(subject)}')

        quoted = (
            f'<html><body>'
            f'<hr style="display:inline-block; width:98%">'
            f'<div id="divRplyFwdMsg"><font face="Calibri, sans-serif" style="font-size:11pt">'
            f'{"".join(header_lines)}'
            f'</font></div>'
            f'<div><br></div>'
            f'{orig_body}'
            f'</body></html>'
        )
        return quoted
    except Exception as e:
        print(f"Warning: could not build quoted thread fallback: {e}", file=sys.stderr)
        return ""


class AmbiguousReplyTarget(ValueError):
    """A stale reply target whose conversation has several correspondents:
    its newest message may be an aside to someone else."""


def _resolve_latest_in_conversation(conversation_id: str, *,
                                    one_correspondent: bool = False) -> Optional[str]:
    """Return the id of the most recent non-draft message in a conversation.

    Recovers when a stored message-id has gone stale: archiving or moving an
    Outlook message changes its id, so a create-reply against the old id
    404s. Re-resolving by conversationId finds the message's current id so
    the reply can still thread instead of degrading to a fresh, quote-less
    draft. Returns None if nothing resolvable.

    one_correspondent (a reply target): the newest message stands in for the
    stale one only when the thread has at most two senders, the user and one
    correspondent. A forward the user sent to a colleague joins the same
    conversation, and a reply to it would go to the colleague and quote the
    aside. With more senders this raises AmbiguousReplyTarget."""
    try:
        data = graph_get("/me/messages", {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,isDraft,receivedDateTime,from",
            "$top": 25,
        })
    except Exception:
        return None
    msgs = [m for m in data.get("value", []) if not m.get("isDraft")]
    if not msgs:
        return None
    if one_correspondent:
        senders = sorted({((m.get("from") or {}).get("emailAddress") or {})
                          .get("address", "").lower() for m in msgs} - {""})
        if len(senders) > 2:
            raise AmbiguousReplyTarget(
                "the reply target moved (its id is stale), and its "
                f"conversation has several senders ({', '.join(senders)}), so "
                "the newest message may be an aside to someone else. Read "
                "get-conversation and pass the live --message-id of the "
                "message to answer, without --conversation-id.")
    # Graph won't reliably $orderby alongside a conversationId $filter, so
    # sort client-side; reply to the most recent message in the thread.
    msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return msgs[0].get("id")


def conversation_latest_message(conversation_id: str) -> Optional[dict]:
    """The most recent NON-DRAFT message in a conversation, as {"from",
    "recipients"} (lowercased addresses; recipients is To plus Cc), or None."""
    try:
        data = graph_get("/me/messages", {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,isDraft,receivedDateTime,from,toRecipients,ccRecipients",
            "$top": 25,
        })
    except Exception:
        return None
    msgs = [m for m in data.get("value", []) if not m.get("isDraft")]
    if not msgs:
        return None
    msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    m = msgs[0]

    def addr(r):
        return ((r or {}).get("emailAddress") or {}).get("address", "").lower()
    return {"from": addr(m.get("from")),
            "recipients": sorted({addr(r) for r in (m.get("toRecipients") or [])
                                  + (m.get("ccRecipients") or []) if addr(r)})}


def conversation_latest_sender(conversation_id: str) -> Optional[str]:
    """Lowercased from-address of the most recent NON-DRAFT message in a
    conversation (or None). Lets a caller skip drafting a reply when the user
    already sent the latest message in the thread: they have handled it, and a
    briefing snapshot that predates their reply must not produce a draft."""
    latest = conversation_latest_message(conversation_id)
    return (latest or {}).get("from") or None


def _odata_quote(value: str) -> str:
    """Escape a string for an OData literal (single quotes double up)."""
    return (value or "").replace("'", "''")


def get_conversation_messages(
    conversation_id: str,
    top: int = 50,
    include_drafts: bool = False,
) -> tuple[list, bool]:
    """Every message in one conversation, OLDEST-FIRST, with bodies.

    conversationId is the exact join key and the mailbox indexes it, so one
    GET replaces a search plus a get-message per hit. Reads /me/messages
    (the whole mailbox, not one folder), so a thread keeps reading correctly
    after its earlier messages were archived.

    Returns (messages, truncated).
    """
    params = {
        "$filter": f"conversationId eq '{_odata_quote(conversation_id)}'",
        "$select": MAIL_FIELDS_FULL,
        "$top": max(1, min(int(top), 100)),
    }
    data = graph_get("/me/messages", params)
    msgs = data.get("value", []) or []
    truncated = "@odata.nextLink" in data
    if not include_drafts:
        msgs = [m for m in msgs if not m.get("isDraft")]
    # Graph won't reliably $orderby alongside a conversationId $filter
    # (same constraint _resolve_latest_in_conversation works around), so
    # order client-side. A draft has no receivedDateTime; fall back to
    # sentDateTime so it does not sort to the front of the thread.
    msgs.sort(key=lambda m: (m.get("receivedDateTime")
                             or m.get("sentDateTime") or ""))
    return msgs, truncated


def create_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    reply_to_id: Optional[str] = None,
    content_type: str = "Text",
    reply_all: bool = True,
    conversation_id: Optional[str] = None,
) -> dict:
    """Create a draft in the Drafts folder.

    For replies, pass `conversation_id` (from the ledger) so a stale
    `reply_to_id` (the source email was archived/moved -> id changed -> 404)
    self-heals: the message is re-resolved by conversation and the reply is
    retried, preserving the quoted thread instead of falling back to a fresh,
    quote-less draft."""
    if reply_to_id:
        if not body or not body.strip():
            raise ValueError(
                "create_draft called with reply_to_id but empty body. "
                "This would send a reply with no content (just the quoted thread). "
                "Provide the reply text in --body."
            )
        # createReplyAll preserves all original To/CC recipients (default)
        # createReply sends only to the original sender
        endpoint = "createReplyAll" if reply_all else "createReply"
        try:
            reply = graph_post(f"/me/messages/{_id(reply_to_id)}/{endpoint}", {})
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404 and conversation_id:
                # Stale reply target (archived/moved -> id changed). Re-resolve
                # the live message in the conversation and retry so the reply
                # still threads. If we cannot resolve, re-raise rather than
                # silently producing a quote-less draft.
                resolved = _resolve_latest_in_conversation(
                    conversation_id, one_correspondent=True)
                if not resolved:
                    raise
                print(f"create-reply target {reply_to_id[:20]}... was stale "
                      f"(404); re-resolved via conversationId to "
                      f"{resolved[:20]}...", file=sys.stderr)
                reply_to_id = resolved
                reply = graph_post(f"/me/messages/{_id(reply_to_id)}/{endpoint}", {})
            else:
                raise
        # The reply draft body contains the quoted thread from the original.
        # Merge our reply text with the quoted thread so the email looks
        # like a standard reply (new text above, quoted thread below).
        quoted_html = reply.get("body", {}).get("content", "")
        if not quoted_html.strip():
            # createReply returned nothing — build the quoted thread
            # ourselves from the original message.
            print("Warning: createReply returned empty body, building quoted thread manually",
                  file=sys.stderr)
            quoted_html = _build_quoted_thread_fallback(reply_to_id)
        elif _is_quoted_thread_flattened(quoted_html):
            # createReply returned a quoted thread but flattened the
            # original plain-text body into a run-on string (no <br> /
            # </div> / </p> tags). Rebuild from scratch so line breaks
            # render correctly.
            print("Warning: createReply returned a flattened quoted thread, "
                  "rebuilding with preserved line breaks", file=sys.stderr)
            quoted_html = _build_quoted_thread_fallback(reply_to_id)
        if quoted_html.strip():
            combined = _build_reply_html(body, quoted_html, content_type)
            patch_body = {
                "body": {"contentType": "HTML", "content": combined},
            }
        else:
            patch_body = {
                "body": {"contentType": content_type, "content": body},
            }
        if cc:
            patch_body["ccRecipients"] = [
                {"emailAddress": {"address": a}} for a in cc
            ]
        return graph_patch(f"/me/messages/{_id(reply.get('id'))}", patch_body)
    else:
        msg = {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "isDraft": True,
        }
        if cc:
            msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        return graph_post("/me/messages", msg)


def set_draft_recipients(message_id: str, to: list | None = None,
                         cc: list | None = None) -> dict:
    """PATCH a draft's To/CC lines. createReply populates recipients from
    the thread; this is how a reply gets redirected elsewhere."""
    body = {}
    if to is not None:
        body["toRecipients"] = [
            {"emailAddress": {"address": a.strip()}} for a in to if a.strip()]
    if cc is not None:
        body["ccRecipients"] = [
            {"emailAddress": {"address": a.strip()}} for a in cc if a.strip()]
    if not body:
        return {}
    return graph_patch(f"/me/messages/{_id(message_id)}", body)


def update_draft(message_id: str, body: str, content_type: str = "Text") -> dict:
    """Update the body of an existing draft message (PATCH, no new draft).

    If the draft contains EA_REPLY markers (from a reply with quoted thread),
    only the reply portion is replaced — the quoted thread is preserved.
    """
    # Fetch the current draft to check for quoted thread markers
    current = get_message_full(message_id)
    current_body = current.get("body", {}).get("content", "")

    if "<!-- EA_REPLY_START -->" in current_body and "<!-- EA_REPLY_END -->" in current_body:
        # Strip old reply portion, then re-insert updated text
        stripped = re.sub(
            r"<!-- EA_REPLY_START -->.*?<!-- EA_REPLY_END --><br>",
            "",
            current_body,
            count=1,
            flags=re.DOTALL,
        )
        combined = _build_reply_html(body, stripped, content_type)
        return graph_patch(f"/me/messages/{_id(message_id)}", {
            "body": {"contentType": "HTML", "content": combined},
        })

    # Markers absent: either a plain draft, or (far more common) a reply whose
    # comment markers Outlook's sanitizer ate on the way into the mailbox.
    # Locate the quoted thread structurally before falling back to a wholesale
    # replace, which would silently drop the conversation history — CLAUDE.md
    # routes every revision through here, so that loss compounds per edit.
    boundary = _split_quoted_thread(current_body)
    if boundary is not None:
        head_m = re.search(r"<body[^>]*>", current_body, re.IGNORECASE)
        head_end = head_m.end() if head_m else 0
        if head_end <= boundary:
            combined = (current_body[:head_end]
                        + _render_reply_html(body, content_type)
                        + "<br>"
                        + current_body[boundary:])
            return graph_patch(f"/me/messages/{_id(message_id)}", {
                "body": {"contentType": "HTML", "content": combined},
            })

    return graph_patch(f"/me/messages/{_id(message_id)}", {
        "body": {"contentType": content_type, "content": body},
    })


def delete_draft(message_id: str) -> None:
    """Delete a draft message."""
    resp = requests.delete(
        _check_endpoint(f"/me/messages/{_id(message_id)}"),
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()


_WD3 = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MO3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_WD_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
# "Thursday Aug 21", "Fri, August 21", "Wed Sept 3." — a weekday word
# followed by a month and day. First three letters key both maps, and
# every weekday and month is unique in its first three, so the loose
# tail (day/-day/abbrev with optional period) all collapse cleanly.
_WEEKDAY_DATE_RE = None


def weekday_date_mismatches(text: str, today=None) -> list:
    """Weekday+date pairs whose stated weekday contradicts the calendar.

    A drafted "Thursday Aug 21" when the 21st is a Friday is exactly the
    kind of hand-computed weekday an LLM gets wrong, and it shipped to a
    real recipient. This resolves the date to the year that puts it
    nearest the send date (scheduling is near-future), so it needs no
    year in the text, and reports the mismatch for preflight to block.
    Pure: no I/O, unit-testable.
    """
    import datetime as _dt
    global _WEEKDAY_DATE_RE
    if _WEEKDAY_DATE_RE is None:
        _WEEKDAY_DATE_RE = re.compile(
            r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+"
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
            r"(\d{1,2})\b", re.IGNORECASE)
    today = today or _dt.date.today()
    out = []
    for wd, mon, day in _WEEKDAY_DATE_RE.findall(text):
        stated = _WD3.get(wd.lower()[:3])
        month = _MO3.get(mon.lower()[:3])
        day = int(day)
        if stated is None or month is None or not (1 <= day <= 31):
            continue
        # Resolve to the nearest occurrence across last/this/next year.
        best = None
        for yr in (today.year - 1, today.year, today.year + 1):
            try:
                cand = _dt.date(yr, month, day)
            except ValueError:
                continue
            if best is None or abs((cand - today).days) < abs((best - today).days):
                best = cand
        if best is None or abs((best - today).days) > 400:
            continue  # far-off or impossible date: not a real appointment
        if best.weekday() != stated:
            out.append({
                "stated": _WD_FULL[stated],
                "actual": _WD_FULL[best.weekday()],
                "date": best.strftime("%b %-d %Y"),
            })
    return out


def _video_platform() -> str:
    """The institution's video platform from the profile, '' when unknown."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        return institution.video()["platform"]
    except Exception:
        return ""


def preflight_check(message_id: str) -> dict:
    """Pre-send validation of a draft. Returns issues that should be
    reviewed before sending.

    Returns {"ok": bool, "errors": [...], "warnings": [...], "draft_summary": {...}}
    - errors: hard blocks — do NOT send until fixed
    - warnings: review recommended but not blocking
    """
    errors = []
    warnings = []

    draft = get_message_full(message_id)
    subject = draft.get("subject", "")
    body_content = draft.get("body", {}).get("content", "")
    body_type = draft.get("body", {}).get("contentType", "text")
    to_list = [r.get("emailAddress", {}).get("address", "")
               for r in draft.get("toRecipients", [])]

    # Extract text from HTML for content checks
    if body_type.lower() == "html":
        text_body = re.sub(r"<[^>]+>", " ", body_content)
        text_body = re.sub(r"\s+", " ", text_body).strip()
    else:
        text_body = body_content.strip()

    is_reply = subject.upper().startswith(("RE:", "FW:"))

    # --- Errors (hard blocks) ---

    # Empty body
    # For replies, strip the quoted thread to check if there's actual new content
    if is_reply:
        # Remove everything after the quoted thread marker
        new_content = text_body
        for marker in ["From:", "-----Original Message-----", "On "]:
            idx = new_content.find(marker)
            if idx > 0:
                new_content = new_content[:idx]
        new_content = new_content.strip()
        if not new_content:
            errors.append(
                "Reply body is empty — only the quoted thread is present. "
                "The actual reply text is missing."
            )
    elif not text_body:
        errors.append("Email body is completely empty.")

    # No recipients
    if not to_list or all(not t for t in to_list):
        errors.append("No recipients (To field is empty).")

    # A Teams link when the institution's platform is another one: only
    # check the NEW reply portion, not the quoted thread. A Teams link from
    # someone else's previous message in the thread is not ours to police.
    new_body_html = body_content
    if body_type.lower() == "html":
        for marker in (r"<!--\s*EA_REPLY_END\s*-->", r'<div\s+id="divRplyFwdMsg"',
                       r"<hr[\s/>]"):
            m = re.search(marker, new_body_html, re.IGNORECASE)
            if m:
                new_body_html = new_body_html[:m.start()]
                break
    else:
        for marker in ("-----Original Message-----", "\nFrom:"):
            idx = new_body_html.find(marker)
            if idx > 0:
                new_body_html = new_body_html[:idx]
                break
    # With no platform configured the check is skipped: nothing says
    # Teams is wrong for this install.
    platform = _video_platform()
    if "teams.microsoft.com" in new_body_html and platform and "teams" not in platform.lower():
        errors.append(
            "Body contains a Microsoft Teams link. The user's institution "
            f"uses {platform}, not Teams. Use "
            "--online with create-event to add the user's own link."
        )

    # Em-dash in the NEW reply text (quoted thread is someone else's text,
    # not ours to police). User preference: never use em-dashes in drafted
    # content. Covers U+2014 (—) and U+2013 (–, en-dash).
    if body_type.lower() == "html":
        new_text_for_emdash = re.sub(r"<[^>]+>", " ", new_body_html)
    else:
        new_text_for_emdash = new_body_html
    if "\u2014" in new_text_for_emdash or "\u2013" in new_text_for_emdash:
        errors.append(
            "Draft contains an em-dash (—) or en-dash (–). User preference: "
            "never use em-dashes in drafted content. Replace with a comma, "
            "period, or rephrase."
        )
    # The rule also covers the ASCII double-hyphen used as a dash. A range
    # like "9-5" or a flag "--online" is a single hyphen, so key on " -- "
    # with surrounding spaces to avoid false positives.
    if " -- " in new_text_for_emdash:
        errors.append(
            "Draft contains ' -- ' (ASCII double-hyphen used as a dash). "
            "User preference: never use dashes in drafted content. Rephrase "
            "with a comma, colon, semicolon, or parentheses."
        )
    # Stated weekday contradicts the calendar date (e.g. Thursday Aug 21
    # when the 21st is a Friday). Hand-computed weekdays are an LLM failure
    # mode and one shipped to a recipient, so this blocks the send.
    for _mm in weekday_date_mismatches(new_text_for_emdash):
        errors.append(
            f"Draft says '{_mm['stated']} {_mm['date']}' but {_mm['date']} "
            f"is a {_mm['actual']}. Fix the weekday or the date before sending."
        )

    # --- Warnings (review recommended) ---

    # Reply without quoted thread
    has_quoted = ("divRplyFwdMsg" in body_content
                  or "EA_REPLY_START" in body_content
                  or "<hr" in body_content.lower())
    if is_reply and not has_quoted:
        warnings.append(
            "Reply has no quoted original thread. Recipient won't see "
            "conversation context. Was create-reply used (not create-draft)?"
        )

    # No subject
    if not subject.strip():
        warnings.append("Subject line is empty.")

    # Build draft summary for the LLM to review
    draft_summary = {
        "subject": subject,
        "to": to_list,
        "cc": [r.get("emailAddress", {}).get("address", "")
               for r in draft.get("ccRecipients", [])],
        "body_preview": text_body[:500],
        "body_length": len(text_body),
        "is_reply": is_reply,
        "has_quoted_thread": has_quoted,
    }

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "draft_summary": draft_summary,
    }


def draft_fingerprint(message_id: str) -> str:
    """The content fingerprint of an Outlook draft, from a fresh read
    (draft_fingerprint.py)."""
    try:
        import draft_fingerprint as _dfp
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import draft_fingerprint as _dfp
    return _dfp.outlook_fingerprint(message_id, sys.modules[__name__])[0]


def send_draft(message_id: str, confirm_token: str = None,
               fingerprint: str = None) -> dict:
    """Send an existing draft message after running preflight checks.

    Requires a valid confirm_token (defense-in-depth). Even if the CLI
    layer is somehow bypassed, this gate prevents unauthorized sends.

    `fingerprint` is the content fingerprint of what the owner approved.
    The draft is read again right before the send; when its content no
    longer has that fingerprint, nothing is sent and the status is
    "changed". A send with no fingerprint is blocked.

    Returns a dict with status, errors, and warnings. If errors are found,
    the draft is NOT sent — fix the issues first.
    """
    if not fingerprint:
        return {
            "status": "blocked",
            "error": "Send requires the fingerprint of the content the owner approved.",
        }
    if not _validate_library_token(confirm_token, "send", message_id, fingerprint):
        return {
            "status": "blocked",
            "error": "Missing or invalid confirmation token. Send requires approval.",
        }

    try:
        import draft_lock as _lock
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import draft_lock as _lock
    # The draft's lock is held from the check to the send. The mail mediator
    # takes the same lock around every draft-changing verb, so no mediated
    # edit lands between the last read and the send (draft_lock.py).
    try:
        with _lock.hold([message_id]):
            return _checked_send(message_id, fingerprint)
    except _lock.Busy:
        return {
            "status": "error",
            "error": ("The draft is being changed right now. Nothing was sent; "
                      "try Send again in a minute."),
        }


def _checked_send(message_id: str, fingerprint: str) -> dict:
    """Preflight, the content check and the send, run under the draft's
    lock (send_draft)."""
    check = preflight_check(message_id)

    if not check["ok"]:
        return {
            "status": "blocked",
            "errors": check["errors"],
            "warnings": check["warnings"],
            "draft_summary": check["draft_summary"],
            "message": "Draft NOT sent. Fix the errors above, then retry.",
        }

    if check["warnings"]:
        print(f"Pre-send warnings: {check['warnings']}", file=sys.stderr)

    # The last read before the send. Graph sends the draft as it is when the
    # POST lands; the draft lock keeps every mediated edit out until then.
    current = draft_fingerprint(message_id)
    if current != fingerprint:
        return {
            "status": "changed",
            "error": ("The draft changed after it was approved. Nothing was sent; "
                      "the owner reviews the new content on a new card."),
            "fingerprint": current,
        }

    graph_post(f"/me/messages/{_id(message_id)}/send", {})
    return {
        "status": "sent",
        "warnings": check["warnings"],
    }


# -------------------------------------------------------------------
# Text helpers
# -------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Minimal HTML tag stripping for message previews."""
    text = re.sub(r'<attachment[^>]*>.*?</attachment>', '[attachment]', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    return text.strip()


# -------------------------------------------------------------------
# Calendar
# -------------------------------------------------------------------
def get_calendar_view(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    days_ahead: int = 14,
) -> list:
    """Get calendar events in a date range."""
    if start is None:
        start = datetime.now(timezone.utc)
    if end is None:
        end = start + timedelta(days=days_ahead)
    params = {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "$select": "subject,start,end,attendees,location,isAllDay,"
        "responseStatus,recurrence,onlineMeeting,organizer,"
        # Recurrence identity + free/busy state, so calendar-view can tell
        # a standing series occurrence from a one-off and a real block from
        # a Free-shown event. The calendar reconciler keys on these.
        # categories carries the "EA Hold" tag, so a view shows which
        # tentative blocks the EA may clear itself; is_ea_hold also needs
        # isOrganizer, and counts a read without it as not organiser.
        "seriesMasterId,type,showAs,categories,isOrganizer",
        "$orderby": "start/dateTime",
        "$top": 200,
    }
    return graph_get_all("/me/calendarView", params)


def get_schedule_detail(
    emails: list[str],
    start: datetime,
    end: datetime,
    interval_minutes: int = 15,
) -> dict:
    """Free/busy for internal (same-tenant) users, with each person's error.

    Calls POST /me/calendar/getSchedule, which returns an availability
    string per user: one character per `interval_minutes` of real time
    from `start` (0 free, 1 tentative, 2 busy, 3 OOF, 4 working
    elsewhere). Across a daylight-saving change the string is an hour
    longer or shorter than the wall-clock span; callers map each
    character by its real time.

    Returns {email: {"view": str, "error": str}} for every address asked.
    `error` is Graph's reason when the calendar could not be read (an
    address outside the tenant, an unknown address, no permission), or
    "no schedule returned"; the view is then empty and means nothing.
    Supports up to 20 schedules per call (Graph API limit).
    """
    tz_name = _get_user_timezone()
    body = {
        "schedules": emails,
        "startTime": {
            "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": tz_name,
        },
        "endTime": {
            "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": tz_name,
        },
        "availabilityViewInterval": interval_minutes,
    }
    data = graph_post("/me/calendar/getSchedule", body)
    result = {}
    for item in data.get("value", []):
        err = item.get("error") or {}
        reason = ""
        if err:
            reason = str(err.get("message") or err.get("responseCode") or "unreadable")
        view = item.get("availabilityView", "") or ""
        result[item.get("scheduleId", "")] = {
            "view": "" if reason else view,
            "error": reason or ("" if view else "no schedule returned"),
        }
    for email in emails:
        if not any(k.lower() == email.lower() for k in result):
            result[email] = {"view": "", "error": "no schedule returned"}
    return result


def get_schedule(
    emails: list[str],
    start: datetime,
    end: datetime,
    interval_minutes: int = 15,
) -> dict:
    """{email: availability_view string}; get_schedule_detail without the
    errors. An unreadable calendar has an empty view."""
    return {k: v["view"] for k, v in
            get_schedule_detail(emails, start, end, interval_minutes).items()}

# -------------------------------------------------------------------
# Recurring events
# -------------------------------------------------------------------
# Two-letter day codes, the form the CLI takes (--repeat-days TU,TH).
DAY_CODES = {
    "MO": "monday",
    "TU": "tuesday",
    "WE": "wednesday",
    "TH": "thursday",
    "FR": "friday",
    "SA": "saturday",
    "SU": "sunday",
}


def parse_repeat_days(days: str) -> list[str]:
    """Turn "TU,TH" into ["tuesday", "thursday"].

    Raises ValueError on an unknown code or an empty list — a typo must
    fail loudly instead of quietly producing a different schedule.
    """
    out: list[str] = []
    for raw in (days or "").split(","):
        code = raw.strip().upper()
        if not code:
            continue
        if code not in DAY_CODES:
            raise ValueError(
                f"Unknown day code '{raw.strip()}' in --repeat-days. "
                "Use two-letter codes MO,TU,WE,TH,FR,SA,SU "
                "(comma-separated), e.g. TU,TH"
            )
        name = DAY_CODES[code]
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError(
            "--repeat-days is empty. Give at least one day, e.g. TU,TH")
    return out


def build_recurrence(
    repeat: str,
    start: datetime,
    days: Optional[str] = None,
    interval: int = 1,
    until: Optional[str] = None,
    count: Optional[int] = None,
    timezone_name: Optional[str] = None,
) -> dict:
    """Build the Graph `recurrence` object for a repeating event.

    repeat:   "weekly" (requires days) or "daily" (must not have days)
    start:    the event's own start datetime; its DATE anchors the series
    days:     "TU,TH" — weekly only
    interval: every N weeks/days (default 1)
    until:    "YYYY-MM-DD" last date of the series -> range type endDate
    count:    number of occurrences -> range type numbered
              (exactly one of until/count)

    Raises ValueError on any invalid combination.
    """
    repeat = (repeat or "").strip().lower()
    if repeat not in ("weekly", "daily"):
        raise ValueError(
            f"--repeat must be 'weekly' or 'daily' (got '{repeat}')")

    if interval is None:
        interval = 1
    interval = int(interval)
    if interval < 1:
        raise ValueError(f"--repeat-interval must be 1 or more (got {interval})")

    if until and count:
        raise ValueError(
            "Use --repeat-until OR --repeat-count, not both.")
    if not until and not count:
        raise ValueError(
            "A recurring event needs an end: pass --repeat-until YYYY-MM-DD "
            "or --repeat-count N.")

    pattern: dict = {"type": repeat, "interval": interval}
    if repeat == "weekly":
        if not days:
            raise ValueError(
                "--repeat weekly requires --repeat-days, e.g. --repeat-days TU,TH")
        pattern["daysOfWeek"] = parse_repeat_days(days)
    elif days:
        raise ValueError(
            "--repeat-days only applies to --repeat weekly. "
            "For every-N-days use --repeat daily --repeat-interval N.")

    tz_name = timezone_name or _get_user_timezone()
    range_: dict = {
        "startDate": start.date().isoformat(),
        "recurrenceTimeZone": tz_name,
    }
    if until:
        try:
            until_date = datetime.strptime(until.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(
                f"--repeat-until must be YYYY-MM-DD (got '{until}')")
        if until_date < start.date():
            raise ValueError(
                f"--repeat-until ({until_date.isoformat()}) is before the "
                f"start date ({start.date().isoformat()}).")
        range_["type"] = "endDate"
        range_["endDate"] = until_date.isoformat()
    else:
        count = int(count)
        if count < 1:
            raise ValueError(f"--repeat-count must be 1 or more (got {count})")
        range_["type"] = "numbered"
        range_["numberOfOccurrences"] = count

    return {"pattern": pattern, "range": range_}


# The tag on an Outlook event the EA placed as a hold. Together with showAs
# tentative, no attendees and the user as organiser it is what clear_hold
# checks before deleting without a button; a colleague-facing meeting can
# never satisfy all four.
EA_HOLD_CATEGORY = "EA Hold"


def is_ea_hold(event: dict) -> tuple[bool, str]:
    """(True, "") when `event` is a hold the EA may clear on its own, else
    (False, reason). The reason names the first failed check."""
    cats = [str(c).strip().lower() for c in (event.get("categories") or [])]
    if EA_HOLD_CATEGORY.lower() not in cats:
        return False, f"not tagged '{EA_HOLD_CATEGORY}'"
    if str(event.get("showAs", "")).lower() != "tentative":
        return False, f"shown as {event.get('showAs') or 'unknown'}, not tentative"
    if event.get("attendees"):
        return False, f"has {len(event['attendees'])} attendee(s)"
    # Only an explicit True counts: a read without the field says nothing
    # about who organised the event.
    if event.get("isOrganizer") is not True:
        return False, "the user is not the organiser"
    return True, ""


def attendee_list(required: Optional[list[str]], optional: Optional[list[str]]) -> list[dict]:
    """Graph attendees: each address once, `optional` where the address is
    in `optional`, else `required`."""
    out, seen = [], set()
    opt = {a.strip().lower() for a in optional or [] if a and a.strip()}
    for addr in list(required or []) + list(optional or []):
        a = (addr or "").strip()
        if not a or a.lower() in seen:
            continue
        seen.add(a.lower())
        out.append({"emailAddress": {"address": a},
                    "type": "optional" if a.lower() in opt else "required"})
    return out


def create_event(
    subject: str,
    start: datetime,
    end: datetime,
    attendees: Optional[list[str]] = None,
    body: str = "",
    location: str = "",
    is_online: bool = False,
    confirm_token: str = None,
    recurrence: Optional[dict] = None,
    hold: bool = False,
    optional_attendees: Optional[list[str]] = None,
) -> dict:
    """Create a calendar event.

    All event creation requires a valid confirm_token (defense-in-depth),
    including personal holds with no attendees. The Slack-button approval
    is the single entry point for putting anything on the user's calendar.

    `hold=True` marks the event as an EA hold: shown as tentative and
    tagged EA_HOLD_CATEGORY, so clear_hold can release it later without a
    button. A hold never carries attendees.

    `optional_attendees` are invited as Outlook optional attendees; an
    address in both lists is optional.

    When is_online=True, adds the user's own meeting link (the
    institution's video platform, institution.video(); the profile's
    legacy zoom_link is the fallback) to the event body and location.
    isOnlineMeeting stays false, so Outlook attaches no meeting of its own.

    Pass `recurrence` (from build_recurrence) to create a SERIES instead of a
    single event: start/end then describe ONE occurrence and the recurrence
    object carries the pattern and range.
    """
    if not _validate_library_token(confirm_token, "event", subject):
        return {
            "status": "blocked",
            "error": "Missing or invalid confirmation token. Calendar event creation requires Slack approval.",
        }
    if is_online:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        video_link = institution.video_link()
        if video_link:
            join_block = institution.video_join_block()
            body = f"{body}\n\n{join_block}\n" if body else join_block
            # Fill OR replace a placeholder location with the real join link.
            # The resident often writes the literal platform word or "Online"
            # as the location; that must become the link, not stay as text.
            _placeholder = {"", "zoom", "online", "virtual", "video", "tbd",
                            "teams", "meet", "webex", "zoom meeting", "teams meeting",
                            "online meeting", "video call"}
            if location.strip().lower() in _placeholder:
                location = video_link
        else:
            # No link configured: the event goes out with no join link, and
            # the calendar still attaches no meeting of its own.
            print("WARNING: is_online=True but the profile has no video link "
                  "(institution.video.link or zoom_link). No join link added.",
                  file=sys.stderr)

    event = {
        "subject": subject,
        "start": _event_time(start),
        "end": _event_time(end),
        "body": {"contentType": "Text", "content": body},
        # Explicitly disable Teams auto-attach
        "isOnlineMeeting": False,
        # Colleagues check the user's Outlook free/busy before proposing a
        # time, so anything we create has to actually read as busy. Graph
        # defaults to this today, but a teaching series that silently landed
        # as Free would defeat the entire reason for putting it on Outlook.
        "showAs": "busy",
    }
    if hold:
        if attendees or optional_attendees:
            return {"status": "blocked",
                    "error": "a hold cannot carry attendees; create a real invite instead"}
        event["showAs"] = "tentative"
        event["categories"] = [EA_HOLD_CATEGORY]
    if location:
        event["location"] = {"displayName": location}
    if attendees or optional_attendees:
        event["attendees"] = attendee_list(attendees, optional_attendees)
    if recurrence:
        event["recurrence"] = recurrence
    return graph_post("/me/events", event)


def get_event(event_id: str) -> dict:
    """Retrieve a single calendar event by ID."""
    return graph_get(f"/me/events/{_id(event_id)}")


def update_event(
    event_id: str,
    subject: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    attendees: Optional[list[str]] = None,
    body: Optional[str] = None,
    location: Optional[str] = None,
    confirm_token: Optional[str] = None,
    optional_attendees: Optional[list[str]] = None,
) -> dict:
    """Update an existing calendar event via PATCH.

    Only fields that are not None are included in the update. A new
    attendee list keeps as optional everyone the event already had as
    optional, plus `optional_attendees`; everyone else is required.
    ALWAYS requires a valid confirm_token (defense-in-depth), like create_event
    and delete_event. A pure retime is the ordinary reschedule and Outlook
    mails it to every attendee on the event, so it is button-required too.
    """
    if not _validate_library_token(confirm_token, "update_event", event_id):
        return {
            "status": "blocked",
            "error": "Missing or invalid confirmation token. Updating an event requires approval.",
        }

    patch_body: dict = {}
    if subject is not None:
        patch_body["subject"] = subject
    if start is not None:
        patch_body["start"] = _event_time(start)
    if end is not None:
        patch_body["end"] = _event_time(end)
    if body is not None:
        patch_body["body"] = {"contentType": "Text", "content": body}
    if location is not None:
        patch_body["location"] = {"displayName": location}
    if attendees is not None or optional_attendees is not None:
        current = get_event(event_id).get("attendees") or []
        if attendees is None:
            attendees = [(a.get("emailAddress") or {}).get("address", "") for a in current
                         if a.get("type") != "resource"]
        was_optional = [(a.get("emailAddress") or {}).get("address", "") for a in current
                        if a.get("type") == "optional"]
        keep = {a.strip().lower() for a in attendees + list(optional_attendees or [])}
        optional = list(optional_attendees or []) + [a for a in was_optional if a.lower() in keep]
        # Rooms and equipment stay booked.
        patch_body["attendees"] = attendee_list(attendees, optional) + [
            a for a in current if a.get("type") == "resource"]

    if not patch_body:
        return {"status": "noop", "error": "No fields to update."}

    return graph_patch(f"/me/events/{_id(event_id)}", patch_body)


def delete_event(event_id: str, confirm_token: Optional[str] = None) -> dict:
    """Delete a calendar event.

    Always requires a valid confirm_token (defense-in-depth).
    Deleting events is a button-required action.
    """
    if not _validate_library_token(confirm_token, "delete_event", event_id):
        return {
            "status": "blocked",
            "error": "Missing or invalid confirmation token. Deleting events requires approval.",
        }

    graph_delete(f"/me/events/{_id(event_id)}")
    return {"status": "deleted", "event_id": event_id}


def clear_hold(event_id: str) -> dict:
    """Release an Outlook hold the EA placed, without a button.

    The event is read back and deleted only when is_ea_hold accepts it:
    tagged EA_HOLD_CATEGORY, shown as tentative, no attendees, the user as
    organiser. Those four together describe nothing but the EA's own
    holds, and deleting one notifies nobody, which is why this path needs
    no confirmation token where delete_event always does. Every other
    event is refused with the failed check named, and the caller stages
    pending-event-delete.json for the button as before.
    """
    event = get_event(event_id)
    ok, reason = is_ea_hold(event)
    if not ok:
        return {"status": "refused", "event_id": event_id,
                "subject": event.get("subject", ""), "reason": reason,
                "hint": "Only a tentative event tagged 'EA Hold' with no attendees "
                        "can be cleared here; stage pending-event-delete.json for "
                        "the Delete button instead."}
    graph_delete(f"/me/events/{_id(event_id)}")
    start = (event.get("start") or {}).get("dateTime", "")
    return {"status": "hold_cleared", "event_id": event_id,
            "subject": event.get("subject", ""), "start": start[:19]}


# -------------------------------------------------------------------
# Mailbox Settings (working hours, timezone, auto-replies)
# -------------------------------------------------------------------
def get_mailbox_settings() -> dict:
    return graph_get("/me/mailboxSettings")


def get_working_hours() -> dict:
    settings = get_mailbox_settings()
    return settings.get("workingHours", {})


# -------------------------------------------------------------------
# To Do (obligations ledger alternative)
# -------------------------------------------------------------------
def get_todo_lists() -> list:
    # /me/todo/lists is Graph-only — not available in Outlook REST API.
    # Returns an empty list with an Outlook REST token; works with Graph auth.
    if _api_base() == OUTLOOK_REST_URL:
        return []
    return graph_get_all("/me/todo/lists")


def create_todo_task(
    list_id: str,
    title: str,
    body: str = "",
    due_date: Optional[str] = None,
    importance: str = "normal",
) -> dict:
    task = {
        "title": title,
        "importance": importance,
        "body": {"content": body, "contentType": "text"},
    }
    if due_date:
        task["dueDateTime"] = {"dateTime": due_date, "timeZone": "UTC"}
    return graph_post(f"/me/todo/lists/{_id(list_id)}/tasks", task)


# -------------------------------------------------------------------
# Attachments
# -------------------------------------------------------------------
def download_attachment(message_id: str, attachment_id: str) -> bytes:
    url = _check_endpoint(
        f"{GRAPH_URL}/me/messages/{_id(message_id)}"
        f"/attachments/{_id(attachment_id, long=True)}/$value"
    )
    resp = requests.get(url, headers=_headers(), timeout=60)
    resp.raise_for_status()
    return resp.content


# -------------------------------------------------------------------
# CLI test
# -------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Graph API authentication and diagnostics.")
    parser.add_argument(
        "--align", action="store_true",
        help="Confidential mode: sign in, re-detect the app's granted delegated "
             "scopes, and print the aligned GRAPH_SCOPES line. Use after your "
             "admin adds a permission (e.g., Calendars.ReadWrite).")
    parser.add_argument(
        "--write", action="store_true",
        help="With --align: write the aligned GRAPH_SCOPES into ~/.ea/.env.")
    args = parser.parse_args()

    if args.align:
        if not _use_confidential():
            print("--align requires the confidential method. Set "
                  "GRAPH_CLIENT_SECRET (and GRAPH_AUTH_METHOD=confidential) first.")
            sys.exit(1)
        info = setup_confidential_and_align()
        print(f"\nSigned in as {info['user']}.")
        print(f"App grants  : {', '.join(info['granted']) or '(none)'}")
        print(f"Aligned     : {', '.join(info['aligned_short'])}")
        if info["dropped_short"]:
            print(f"Dropped     : {', '.join(info['dropped_short'])} "
                  "(not granted by your app)")
        new_line = "GRAPH_SCOPES=" + " ".join(info["aligned_short"])
        if args.write:
            # ~/.ea/.env is resident space (the owner cannot even create entries
            # in ~/.ea), and what is read from it is written back to it, so the
            # whole read-modify-write runs with the resident's access
            # (resident_io.update_text). Mode 0640: the owner's processes read it
            # through group ea.
            try:
                import resident_io as _rio
            except ModuleNotFoundError:
                from preprocessor import resident_io as _rio
            env_file = EA_DIR / ".env"

            def _with_scopes(old: str) -> str:
                existing, order = {}, []
                for _l in old.splitlines():
                    if "=" in _l and not _l.strip().startswith("#"):
                        _k, _v = _l.split("=", 1)
                        _k = _k.strip()
                        if _k not in existing:
                            order.append(_k)
                        existing[_k] = _v.strip()
                if "GRAPH_SCOPES" not in existing:
                    order.append("GRAPH_SCOPES")
                existing["GRAPH_SCOPES"] = " ".join(info["aligned_short"])
                return "\n".join(f"{k}={existing[k]}" for k in order) + "\n"

            if not _rio.update_text(env_file, _with_scopes, mode=0o640):
                print(f"\nCould not write {env_file}; add this line by hand:\n{new_line}")
                sys.exit(1)
            print(f"\nWrote {new_line}\n  -> {env_file}")
        else:
            print(f"\n{new_line}")
            print("\nAdd/replace that line in ~/.ea/.env, or re-run with --write.")
        sys.exit(0)

    print("Authenticating...")
    _SIGN_IN_REQUESTED = True
    token = authenticate()
    print(f"Authenticated. Token length: {len(token)}")

    print("\nFetching mailbox settings...")
    settings = get_mailbox_settings()
    tz = settings.get("timeZone", "unknown")
    wh = settings.get("workingHours", {})
    print(f"  Timezone: {tz}")
    print(f"  Working hours: {wh.get('startTime', '?')} - {wh.get('endTime', '?')}")
    print(f"  Work days: {wh.get('daysOfWeek', '?')}")

    print("\nFetching inbox (entire)...")
    msgs = get_entire_inbox()
    print(f"  Total messages in inbox: {len(msgs)}")
    for m in msgs[:5]:
        subj = m.get("subject", "(no subject)")
        frm = m.get("from", {}).get("emailAddress", {}).get("address", "?")
        print(f"  [{m.get('receivedDateTime','')}] {frm}: {subj}")

    print("\nFetching next 7 days of calendar...")
    events = get_calendar_view(days_ahead=7)[:5]
    for e in events:
        print(f"  {e['start']['dateTime'][:16]} - {e.get('subject', '?')}")

    print("\nFetching To Do lists...")
    lists = get_todo_lists()
    for l in lists:
        print(f"  {l['displayName']} (id: {l['id'][:12]}...)")

    print("\nDone. Graph API access confirmed.")
