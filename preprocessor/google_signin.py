#!/usr/bin/env python3
"""Google sign-in from the chat: a link the user taps, an address they paste.

Why it exists. The person an install serves may have no terminal. Google's
desktop clients send a finished sign-in to a loopback address
(http://localhost:8400), which a phone cannot reach: the browser shows a
page that does not load, with the sign-in code in its address. The user
pastes that address into the chat, and the bot, an owner process, trades
the code for a token. The code is worth nothing without the PKCE verifier,
which stays in the owner-private directory, and Google takes it once.

The flow, owner side only (the bot; never the resident, never mediated):

    start(account, service)  the sign-in link; its state and verifier wait in
                             <private dir>/google-signin/<state>.json
    find_redirect(text)      the (state, code, error) a pasted address holds
    finish(text)             trades the code, saves the token, checks it

How a link is asked for: the user says `connect Google Drive <account>`
in the chat, or the EA stages ~/.ea/state/pending-google-signin.json with
{"account": "<account>", "service": "drive"} and the bot posts the link
(ea-chat-bot.py, the staged-action protocol). The terminal sign-in
(`drive_client.py setup`) stays for an install with no chat.

SERVICES names what a sign-in is for. Drive is the one there now; a
calendar or Gmail sign-in is one more entry with its scopes and token file.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import drive_client as _drive
    import owner_trust as _owner_trust
    import published_apps as _published
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import drive_client as _drive
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import published_apps as _published

REDIRECT_URI = "http://localhost:8400"
PENDING_TTL_S = 2 * 24 * 3600
_STATE_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
_EMAIL_RE = re.compile(r"^[^@\s/\\<>]+@[^@\s/\\<>]+\.[^@\s/\\<>]+$")
# A pasted loopback address: any port, either loopback spelling, the query
# up to the first space or bracket (Slack wraps a link in <...|...>).
_REDIRECT_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?/?\?[^\s<>|\"']+", re.I)
_ACTIVATION_RE = re.compile(r"https://console\.(?:developers|cloud)\.google\.com/apis/api/"
                            r"drive\.googleapis\.com/overview\?project=[A-Za-z0-9_-]+")
# The bot's own command: "connect Google Drive you@gmail.com".
COMMAND_RE = re.compile(
    r"^\s*connect\s+(?:my\s+)?(?:google\s+)?drive\b(?:\s+(?:for|as|with)\b)?\s*(\S+@\S+?)?\s*[.!]?\s*$",
    re.I)


def _drive_check(creds) -> tuple[str, str]:
    """(the account Drive names, the activation link when the API is off).
    Raises the Drive call's error for anything else."""
    from googleapiclient.discovery import build
    try:
        about = build("drive", "v3", credentials=creds, cache_discovery=False) \
            .about().get(fields="user(emailAddress)").execute()
    except _drive.HttpError as e:
        if _drive._api_disabled(e):
            m = _ACTIVATION_RE.search(_drive._http_text(e))
            return "", (m.group(0) if m else "https://console.cloud.google.com/apis/library/"
                                               "drive.googleapis.com")
        raise
    return str((about.get("user") or {}).get("emailAddress") or "").strip().lower(), ""


SERVICES = {
    "drive": {
        "label": "Google Drive",
        "scopes": _drive.SCOPES,
        "save": _drive._save_token,
        "check": _drive_check,
        "why": ("the EA can then open the Google Docs your mail links to. The access is "
                "read only: it cannot change, share or delete a file."),
        "done": "The EA can now open the Google Docs your mail links to.",
    },
}


class SigninError(Exception):
    """A sign-in step that cannot go on; `text` is the chat reply."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def _pending_dir() -> Path:
    d = _owner_trust.private_dir() / "google-signin"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _client_source() -> str:
    return _published.google_client_source(_drive.CLIENT_SECRET_FILE)


def _client_config() -> dict:
    source = _client_source()
    if source == "published":
        return _published.google_client_config()
    if source == "file":
        return _drive._read_json(_drive.CLIENT_SECRET_FILE)
    raise SigninError(":x: This install has no Google sign-in client yet. The person who set it "
                      "up adds one first (docs/SETUP.md 'Google OAuth app').")


def _flow(scopes, **kwargs):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(_client_config(), scopes, redirect_uri=REDIRECT_URI, **kwargs)


def check_account(account: str) -> str:
    account = (account or "").strip().strip("<>").lower()
    if not _EMAIL_RE.match(account):
        raise SigninError(f":x: {account or 'That'} is not an email address. Say: "
                          f"`connect Google Drive you@gmail.com`.")
    return account


def parse_command(text: str):
    """None when `text` is not the connect command, else the account it
    names ("" when it names none)."""
    m = COMMAND_RE.match(text or "")
    if not m:
        return None
    return (m.group(1) or "").strip().strip("<>")


def start(account: str, service: str = "drive", channel: str = "", thread_ts: str = "") -> dict:
    """The sign-in link for one account and service; its state and PKCE
    verifier wait owner-private for PENDING_TTL_S."""
    account = check_account(account)
    spec = SERVICES.get(service)
    if not spec:
        raise SigninError(f":x: There is no sign-in for {service!r}.")
    state = secrets.token_urlsafe(24)
    flow = _flow(spec["scopes"], state=state)
    url, _ = flow.authorization_url(prompt="consent", access_type="offline", login_hint=account)
    record = {"state": state, "verifier": flow.code_verifier, "account": account,
              "service": service, "created": time.time(),
              "channel": str(channel or ""), "thread_ts": str(thread_ts or "")}
    _owner_trust.write_owner_file(_pending_dir() / f"{state}.json", json.dumps(record))
    _prune()
    return {"url": url, "state": state, "account": account, "service": service,
            "own_client": _client_source() == "file"}


def link_text(started: dict) -> str:
    """The chat message that carries the link: why, then four steps."""
    spec = SERVICES[started["service"]]
    acct = started["account"]
    lines = [f":key: *Connect {spec['label']} for {acct}*",
             f"Why: {spec['why']}",
             f"1. Open this link and sign in as {acct}: {started['url']}"]
    n = 2
    if started.get("own_client"):
        lines.append(f"{n}. Google may say that it has not verified the app. The app is this "
                     f"install's own: tap *Advanced*, then the link that continues.")
        n += 1
    lines.append(f"{n}. Allow the {spec['label']} access, and leave its box ticked.")
    lines.append(f"{n + 1}. Your browser then opens a page that does not load. That is expected. "
                 f"Copy the whole address from the address bar and paste it here.")
    lines.append("The link works for two days. After you approve, paste the address within "
                 "ten minutes.")
    return "\n".join(lines)


def find_redirect(text: str):
    """(state, code, error) from the first loopback address in `text` that
    carries a state, else None. Slack sends `&` as `&amp;`, so the text is
    unescaped first."""
    for m in _REDIRECT_RE.finditer(html.unescape(text or "")):
        q = parse_qs(urlsplit(m.group(0)).query)
        state = (q.get("state") or [""])[0]
        if state:
            return state, (q.get("code") or [""])[0], (q.get("error") or [""])[0]
    return None


def is_redirect(text: str) -> bool:
    return find_redirect(text) is not None


def _load(state: str) -> dict | None:
    if not _STATE_RE.fullmatch(state or ""):
        return None
    path = _pending_dir() / f"{state}.json"
    try:
        record = json.loads(_owner_trust.read_owner_file(path))
    except (FileNotFoundError, ValueError, OSError, _owner_trust.OwnerTrustError):
        return None
    if not isinstance(record, dict) or record.get("state") != state:
        return None
    if time.time() - float(record.get("created") or 0) > PENDING_TTL_S:
        _drop(state)
        return None
    return record


def _drop(state: str) -> None:
    try:
        (_pending_dir() / f"{state}.json").unlink()
    except OSError:
        pass


def _prune() -> None:
    """Forget links older than PENDING_TTL_S."""
    cutoff = time.time() - PENDING_TTL_S
    try:
        for path in _pending_dir().glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _granted(token: dict) -> set[str]:
    scope = (token or {}).get("scope") or []
    return set(scope.split() if isinstance(scope, str) else scope)


def finish(text: str) -> dict:
    """Trade the code in a pasted address for a token, save it and check it.
    Returns {"status": "ok"|"api_off"|"error", "text": <the chat reply>, ...}."""
    found = find_redirect(text)
    if not found:
        return {"status": "error", "text": ":x: That message holds no sign-in address."}
    state, code, error = found
    record = _load(state)
    if not record:
        return {"status": "error",
                "text": ":x: That sign-in address does not match a link I sent, or the link is "
                        "more than two days old. Say `connect Google Drive <account>` for a new one."}
    service, requested = record["service"], record["account"]
    spec = SERVICES.get(service) or SERVICES["drive"]
    again = f"Say `connect {spec['label']} {requested}` for a new link."
    if error or not code:
        _drop(state)
        why = "you did not allow the access" if error == "access_denied" else f"Google answered {error or 'no code'}"
        return {"status": "error", "text": f":x: The sign-in did not finish: {why}. {again}"}
    # Google returns only the scopes the user left ticked; the check below
    # reads them instead of oauthlib's scope-change exception.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    try:
        flow = _flow(spec["scopes"], state=state, code_verifier=record["verifier"])
        token = flow.fetch_token(code=code)
    except SigninError as e:
        return {"status": "error", "text": e.text}
    except Exception as e:  # noqa: BLE001 - oauthlib raises its own error classes
        _drop(state)
        detail = "it works only once, and only for about ten minutes" \
            if "invalid_grant" in str(e) else str(e)[:160]
        return {"status": "error",
                "text": f":x: Google refused that sign-in address: {detail}. {again}"}
    _drop(state)
    missing = set(spec["scopes"]) - _granted(token)
    if missing:
        return {"status": "error",
                "text": f":x: The sign-in did not include the {spec['label']} access. "
                        f"{again} Leave the {spec['label']} box ticked."}
    creds = flow.credentials
    account, activation = requested, ""
    try:
        named, activation = spec["check"](creds)
        account = named or requested
    except Exception:  # noqa: BLE001 - saved anyway; the reader reports a later failure
        pass
    spec["save"](account, creds)
    out = {"account": account, "requested": requested, "service": service}
    other = (f" You signed in as {account}, not {requested}; the EA uses {account}."
             if account != requested else "")
    if activation:
        return {**out, "status": "api_off", "activation_url": activation,
                "text": f":warning: The sign-in for {account} worked, but the {spec['label']} "
                        f"API is turned off in this app's Google Cloud project. Open {activation}, "
                        f"tap *Enable*, and wait a minute. You do not need to sign in again."
                        + other}
    return {**out, "status": "ok",
            "text": f":white_check_mark: {spec['label']} is connected for {account}, read only. "
                    f"{spec['done']}" + other}
