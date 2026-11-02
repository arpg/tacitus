#!/usr/bin/env python3
"""Google Drive reader: the text of a Google Doc, Sheet or Slides file, a
search of the user's Drive, and a copy of a file stored there.

Why it exists. A Google Docs link in mail or chat is almost always private
to the user's account. fetch_url.py has no Google login, so Google answers
it with HTTP 401. The claude.ai Drive connector loads only under a browser
login of Claude Code; a setup-token login (scripts/install/ea-bot-token.sh,
and the macOS path) loads no connector at all. This client signs in with
the install's own Google OAuth client, the one google_calendar_client.py
and gmail_client.py use, and asks for one scope, `drive.readonly`: it can
list and read the files the account can open, and it cannot change, share
or delete any of them.

Setup, by the owner in a terminal (docs/SETUP.md "Google Drive"; the
Drive API must be enabled in the Google Cloud project):

    python3 preprocessor/drive_client.py setup --email you@example.edu
    python3 preprocessor/drive_client.py test

Each account's token is ~/.ea/auth/google_token_<account>_drive.json,
owner-only like the other tokens; the file records the account it holds.

The verbs the EA runs (as ea-bot they go through the mail mediator, which
holds the tokens; mail_mediation.SAFE):

    read     --url <link> | --id <id>  [--account a] [--start N] [--max-chars N]
    search   [--query "<words>"] [--folder <link or id>] [--account a] [--max-results N]
    download --url <link> | --id <id>  --output <path> [--format docx|xlsx|pptx|pdf|csv|txt|md]
    accounts

`read` tries each signed-in account until one can open the file. The owner
side never parses a file's bytes: `read` returns Google's own text export
of a Google file, or the bytes of a plain-text file; any other file is
saved with `download` and read with doc_text.py on the resident's side.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import owner_trust as _owner_trust
    import published_apps as _published
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import published_apps as _published

try:
    from googleapiclient.errors import HttpError
except ImportError:  # the resident's shim forwards before any Google call
    class HttpError(Exception):  # type: ignore[no-redef]
        pass


EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
AUTH_DIR = EA_DIR / "auth"
CLIENT_SECRET_FILE = AUTH_DIR / "google_client_secret.json"

# Read and list only. No Drive write verb exists in this client.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_PREFIX = "google_token_"
TOKEN_SUFFIX = "_drive.json"

# Links the client takes apart. A published-to-the-web link (/d/e/2PACX-...)
# names no file; fetch_url.py reads it, since it is public.
HOSTS = ("docs.google.com", "drive.google.com")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,200}")
_PATH_ID_RE = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{10,200})(?=[/?#]|$)")
_PUBLISHED_RE = re.compile(r"/d/e/[A-Za-z0-9_-]+")
_EMAIL_RE = re.compile(r"^[^@\s/\\]+@[^@\s/\\]+\.[^@\s/\\]+$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

GOOGLE_PREFIX = "application/vnd.google-apps."
GOOGLE_DOC = GOOGLE_PREFIX + "document"
GOOGLE_SHEET = GOOGLE_PREFIX + "spreadsheet"
GOOGLE_SLIDES = GOOGLE_PREFIX + "presentation"
GOOGLE_FOLDER = GOOGLE_PREFIX + "folder"
GOOGLE_SHORTCUT = GOOGLE_PREFIX + "shortcut"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF = "application/pdf"

# What `read` asks Google for, in order: Markdown keeps a doc's headings,
# lists and tables; plain text is the fallback where Markdown is not offered.
TEXT_EXPORTS = {
    GOOGLE_DOC: ("document", ("text/markdown", "text/plain")),
    GOOGLE_SHEET: ("spreadsheet", ("text/csv",)),
    GOOGLE_SLIDES: ("presentation", ("text/plain",)),
}
# What `download --format` may ask for, and the default.
FILE_EXPORTS = {
    GOOGLE_DOC: {"docx": DOCX, "pdf": PDF, "txt": "text/plain", "md": "text/markdown"},
    GOOGLE_SHEET: {"xlsx": XLSX, "pdf": PDF, "csv": "text/csv"},
    GOOGLE_SLIDES: {"pptx": PPTX, "pdf": PDF, "txt": "text/plain"},
}
DEFAULT_FORMAT = {GOOGLE_DOC: "docx", GOOGLE_SHEET: "xlsx", GOOGLE_SLIDES: "pptx"}
FORMATS = ("docx", "xlsx", "pptx", "pdf", "csv", "txt", "md")
PLAIN_TEXT_TYPES = ("application/json", "application/xml")

DEFAULT_MAX_CHARS = 30000
MAX_CHARS_CAP = 200000
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_QUERY = 200
META_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners(emailAddress),shortcutDetails"
LIST_FIELDS = "files(id,name,mimeType,modifiedTime,webViewLink,owners(emailAddress))"

SIGN_IN_FIX = ("the owner signs the account in once, in a terminal: python3 "
               "preprocessor/drive_client.py setup --email <the Google account> "
               "(docs/SETUP.md 'Google Drive')")
API_FIX = ("enable the Google Drive API in the install's Google Cloud project: "
           "APIs & Services > Library > Google Drive API > Enable (docs/SETUP.md 'Google Drive')")
SHARE_FIX = ("ask the user to share the file with a signed-in account, or to paste "
             "its text into the chat")


class DriveError(Exception):
    """A refusal or failure the EA can act on: what happened and the next step."""

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.message = message
        self.fix = fix

    def as_json(self) -> dict:
        out = {"status": "error", "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        return out


# -------------------------------------------------------------------
# Token files (the same custody rules as gmail_client.py)
# -------------------------------------------------------------------
def _checked_token_io() -> bool:
    """True when token files go through the owner checks: every process but
    the resident's own and one on a scratch EA tree (a test). An unknown
    answer counts as checked."""
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


def _token_path(email: str) -> Path:
    safe = email.replace("@", "_at_").replace(".", "_")
    return AUTH_DIR / f"{TOKEN_PREFIX}{safe}{TOKEN_SUFFIX}"


def _read_json(path: Path) -> dict:
    if _checked_token_io():
        text = _owner_trust.read_owner_file(path, allow_private_group=True)
    else:
        text = path.read_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _save_token(email: str, creds) -> None:
    """The token, with the account it holds, so the reader can list the
    signed-in accounts from the auth directory alone."""
    data = json.loads(creds.to_json())
    data["account"] = email
    text = json.dumps(data)
    path = _token_path(email)
    if _checked_token_io():
        mode, gid = _owner_trust.token_file_mode()
        _owner_trust.write_owner_file(path, text, mode=mode, gid=gid, allow_private_group=True)
        return
    path.write_text(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def signed_in_accounts() -> list[str]:
    """Every account with a Drive token, in name order. A token counts only
    when the account it records maps back to its own file name."""
    out: list[str] = []
    try:
        paths = sorted(AUTH_DIR.glob(f"{TOKEN_PREFIX}*{TOKEN_SUFFIX}"))
    except OSError:
        return out
    for path in paths:
        try:
            info = _read_json(path)
        except (OSError, ValueError, _owner_trust.OwnerTrustError):
            continue
        acct = str(info.get("account") or "").strip().lower()
        if _EMAIL_RE.match(acct) and _token_path(acct) == path and acct not in out:
            out.append(acct)
    return out


def _check_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise DriveError(f"{email!r} is not an email address")
    return email


def _client_flow():
    from google_auth_oauthlib.flow import InstalledAppFlow
    if _published.google_client_source(CLIENT_SECRET_FILE) == "published":
        return InstalledAppFlow.from_client_config(_published.google_client_config(), SCOPES)
    return InstalledAppFlow.from_client_config(_read_json(CLIENT_SECRET_FILE), SCOPES)


def authenticate_account(email: str, interactive: bool = False):
    """Valid Drive credentials for one account, or None. Interactive (the
    owner's terminal) opens a sign-in when there is no usable token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    path = _token_path(email)
    creds = None
    if path.exists():
        try:
            creds = Credentials.from_authorized_user_info(_read_json(path), SCOPES)
        except (OSError, ValueError, _owner_trust.OwnerTrustError) as e:
            print(f"Drive token refused for {email}: {e}", file=sys.stderr)
            if not interactive:
                return None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001 - google.auth RefreshError and transport errors
            print(f"Drive token refresh failed for {email}: {e}", file=sys.stderr)
            creds = None
        else:
            try:
                _save_token(email, creds)
            except (OSError, _owner_trust.OwnerTrustError) as e:
                print(f"Could not save the refreshed Drive token for {email} (non-fatal): {e}",
                      file=sys.stderr)
            return creds
    if not interactive:
        return None
    if not _published.google_client_source(CLIENT_SECRET_FILE):
        raise DriveError(f"no Google client: save the OAuth client JSON as {CLIENT_SECRET_FILE}",
                         "docs/SETUP.md 'Google OAuth app'")
    print(f"\nSigning in {email} for Google Drive (read only).", file=sys.stderr)
    print("Sign in with that account. If Google says the app is blocked, the account's "
          "administrator has to allow it; docs/SETUP.md 'Google Drive' says what to ask.",
          file=sys.stderr)
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # the loopback redirect is plain http
    flow = _client_flow()
    flow.redirect_uri = "http://localhost:8400"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline",
                                         login_hint=email)
    print(f"Open this URL in your browser:\n{auth_url}\n", file=sys.stderr)
    print("After signing in, paste the full address the browser lands on here:", file=sys.stderr)
    redirect = input("Paste URL: ").strip()
    flow.fetch_token(authorization_response=redirect)
    creds = flow.credentials
    _save_token(email, creds)
    print(f"Drive token saved for {email}", file=sys.stderr)
    return creds


def _service(email: str):
    creds = authenticate_account(email, interactive=False)
    if not creds:
        raise DriveError(f"the Drive sign-in of {email} is missing, expired or refused",
                         SIGN_IN_FIX.replace("<the Google account>", email))
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# -------------------------------------------------------------------
# Links, errors and text
# -------------------------------------------------------------------
def file_id(ref: str) -> str:
    """The Drive id in a docs.google.com or drive.google.com link, or a bare id."""
    ref = (ref or "").strip()
    if _ID_RE.fullmatch(ref):
        return ref
    try:
        parts = urlsplit(ref)
    except ValueError:
        parts = None
    host = (parts.hostname or "").lower() if parts else ""
    if not parts or parts.scheme not in ("http", "https") or host not in HOSTS:
        raise DriveError(f"{ref[:120]!r} is not a Google Docs or Drive link or a Drive id",
                         "a web page is read with fetch_url.py")
    if _PUBLISHED_RE.search(parts.path):
        raise DriveError("a published-to-the-web link names no Drive file",
                         "it is public: read it with fetch_url.py")
    m = _PATH_ID_RE.search(parts.path)
    if m:
        return m.group(1)
    qid = (parse_qs(parts.query).get("id") or [""])[0]
    if _ID_RE.fullmatch(qid):
        return qid
    raise DriveError(f"no file id in {ref[:120]!r}", "pass the file's link as it appears in the address bar")


def _http_status(e) -> int:
    resp = getattr(e, "resp", None)
    try:
        return int(getattr(resp, "status", 0) or getattr(e, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _http_text(e) -> str:
    content = getattr(e, "content", b"") or b""
    try:
        return content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    except Exception:  # noqa: BLE001
        return ""


def _api_disabled(e) -> bool:
    text = _http_text(e)
    return _http_status(e) == 403 and any(
        s in text for s in ("accessNotConfigured", "SERVICE_DISABLED",
                            "has not been used in project", "it is disabled"))


def _too_large(e) -> bool:
    return "exportSizeLimitExceeded" in _http_text(e)


def _decode(data) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data or "")
    text = text.lstrip("﻿").replace("\r\n", "\n")
    return _CONTROL_RE.sub("", text)


# A Markdown export carries each image inline as a base64 data address,
# often thousands of characters that hold no text.
_DATA_IMAGE_REF = re.compile(r"^\[[^\]\n]{1,64}\]:\s*<?data:image/[^\s>]*>?[ \t]*$", re.M)
_DATA_IMAGE_INLINE = re.compile(r"\(data:image/[^)\s]*\)")


def _strip_data_images(text: str) -> str:
    text = _DATA_IMAGE_REF.sub("", text)
    return _DATA_IMAGE_INLINE.sub("(image)", text)


def _kind(mime: str) -> str:
    if mime in TEXT_EXPORTS:
        return TEXT_EXPORTS[mime][0]
    if mime == GOOGLE_FOLDER:
        return "folder"
    if mime == PDF:
        return "pdf"
    if mime.startswith(GOOGLE_PREFIX):
        return mime[len(GOOGLE_PREFIX):]
    return "file"


def _is_plain_text(mime: str) -> bool:
    return mime.startswith("text/") or mime in PLAIN_TEXT_TYPES


def _owner(meta: dict) -> str | None:
    owners = meta.get("owners") or []
    return (owners[0] or {}).get("emailAddress") if owners else None


def _accounts(account: str | None) -> list[str]:
    signed = signed_in_accounts()
    if account:
        account = _check_email(account)
        if account not in signed:
            raise DriveError(f"{account} is not signed in for Drive",
                             SIGN_IN_FIX.replace("<the Google account>", account))
        return [account]
    if not signed:
        raise DriveError("no Google account is signed in for Drive", SIGN_IN_FIX)
    return signed


def _get_meta(svc, fid: str) -> dict:
    meta = svc.files().get(fileId=fid, fields=META_FIELDS, supportsAllDrives=True).execute()
    if meta.get("mimeType") == GOOGLE_SHORTCUT:
        target = (meta.get("shortcutDetails") or {}).get("targetId") or ""
        if not _ID_RE.fullmatch(target):
            raise DriveError("a shortcut whose target is gone")
        meta = svc.files().get(fileId=target, fields=META_FIELDS, supportsAllDrives=True).execute()
    return meta


def _locate(fid: str, account: str | None):
    """(account, service, metadata) of the first account that can open the file."""
    misses = []
    for acct in _accounts(account):
        try:
            svc = _service(acct)
            return acct, svc, _get_meta(svc, fid)
        except DriveError as e:
            misses.append(f"{acct}: {e.message}")
        except HttpError as e:
            if _api_disabled(e):
                raise DriveError("the Google Drive API is not enabled for this install", API_FIX)
            status = _http_status(e)
            misses.append(f"{acct}: {'cannot open it' if status in (403, 404) else f'HTTP {status}'}")
    raise DriveError("no signed-in Google account can open this file (" + "; ".join(misses) + ")",
                     SHARE_FIX)


def _media(svc, meta: dict, limit: int) -> bytes:
    size = int(meta.get("size") or 0)
    if size > limit:
        raise DriveError(f"{meta.get('name')!r} is {size // (1024 * 1024)} MB, over the "
                         f"{limit // (1024 * 1024)} MB limit")
    data = svc.files().get_media(fileId=meta["id"], supportsAllDrives=True).execute()
    if len(data) > limit:
        raise DriveError(f"{meta.get('name')!r} is over the {limit // (1024 * 1024)} MB limit")
    return data


def _export(svc, meta: dict, mime: str) -> bytes:
    try:
        return svc.files().export(fileId=meta["id"], mimeType=mime).execute()
    except HttpError as e:
        if _too_large(e):
            raise DriveError(f"{meta.get('name')!r} is too large for Google to export "
                             "(the limit is 10 MB)", "open it in the browser, or ask the "
                             "user for the part that matters") from None
        raise


def _base(acct: str, meta: dict) -> dict:
    mime = meta.get("mimeType") or ""
    return {"status": "ok", "account": acct, "id": meta.get("id"), "title": meta.get("name"),
            "kind": _kind(mime), "mime_type": mime, "modified": meta.get("modifiedTime"),
            "owner": _owner(meta), "url": meta.get("webViewLink")}


# -------------------------------------------------------------------
# The verbs
# -------------------------------------------------------------------
def read(ref: str, account: str | None = None, start: int = 0,
         max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """The file's text. A negative `start` counts from the end."""
    max_chars = max(1, min(int(max_chars), MAX_CHARS_CAP))
    acct, svc, meta = _locate(file_id(ref), account)
    mime = meta.get("mimeType") or ""
    note = None
    if mime in TEXT_EXPORTS:
        text = fmt = None
        for candidate in TEXT_EXPORTS[mime][1]:
            try:
                text, fmt = _decode(_export(svc, meta, candidate)), candidate
                break
            except HttpError as e:
                if _http_status(e) == 400:
                    continue  # Google does not offer this format for the file
                raise
        if text is None:
            raise DriveError(f"Google offers no text export of {meta.get('name')!r}",
                             "download it with --format pdf and read it with doc_text.py")
        if fmt == "text/markdown":
            text = _strip_data_images(text)
        if mime == GOOGLE_SHEET:
            note = ("A CSV export holds the first sheet only. For every sheet: download "
                    "--format xlsx --output <path>, then doc_text.py <path>.")
    elif mime == GOOGLE_FOLDER:
        raise DriveError(f"{meta.get('name')!r} is a folder", "list it with: search --folder <link>")
    elif mime.startswith(GOOGLE_PREFIX):
        raise DriveError(f"{meta.get('name')!r} is a Google {_kind(mime)} file, which has no text "
                         "export", "ask the user what it says")
    elif _is_plain_text(mime):
        text, fmt = _decode(_media(svc, meta, MAX_TEXT_BYTES)), mime
    else:
        raise DriveError(f"{meta.get('name')!r} is a stored {mime or 'binary'} file",
                         f"save it with download --output {EA_DIR / 'uploads'}/<name>, then "
                         "read it with doc_text.py")
    total = len(text)
    begin = max(0, total + int(start)) if int(start) < 0 else min(int(start), total)
    end = min(total, begin + max_chars)
    out = _base(acct, meta)
    out.update({"format": fmt, "chars": total, "start": begin, "end": end,
                "truncated": end < total, "next_start": end if end < total else None})
    if note:
        out["note"] = note
    out["text"] = text[begin:end]
    return out


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def search(query: str | None = None, folder: str | None = None, account: str | None = None,
           max_results: int = 20) -> dict:
    """Files by name and content words, the files in one folder, or (with
    neither) the most recently changed files, across the signed-in accounts."""
    max_results = max(1, min(int(max_results), 50))
    clauses = ["trashed = false"]
    full_text = False
    query = _CONTROL_RE.sub(" ", (query or "")).strip()
    if len(query) > MAX_QUERY:
        raise DriveError(f"the query is over {MAX_QUERY} characters")
    if query:
        q = _quote(query)
        clauses.append(f"(name contains '{q}' or fullText contains '{q}')")
        full_text = True
    if folder:
        clauses.append(f"'{file_id(folder)}' in parents")
    kwargs = {"q": " and ".join(clauses), "pageSize": max_results, "fields": LIST_FIELDS,
              "corpora": "allDrives", "includeItemsFromAllDrives": True,
              "supportsAllDrives": True}
    if not full_text:
        kwargs["orderBy"] = "modifiedTime desc"  # Drive refuses an order with fullText
    files, errors = [], []
    for acct in _accounts(account):
        try:
            resp = _service(acct).files().list(**kwargs).execute()
        except DriveError as e:
            errors.append(f"{acct}: {e.message}")
            continue
        except HttpError as e:
            if _api_disabled(e):
                raise DriveError("the Google Drive API is not enabled for this install", API_FIX)
            errors.append(f"{acct}: HTTP {_http_status(e)}")
            continue
        for f in resp.get("files") or []:
            mime = f.get("mimeType") or ""
            files.append({"account": acct, "id": f.get("id"), "title": f.get("name"),
                          "kind": _kind(mime), "mime_type": mime,
                          "modified": f.get("modifiedTime"), "owner": _owner(f),
                          "url": f.get("webViewLink")})
    if errors and not files:
        raise DriveError("the search failed: " + "; ".join(errors),
                         "try again in a minute; if it fails again, the owner runs "
                         "python3 preprocessor/drive_client.py test")
    out = {"status": "ok", "count": len(files), "files": files}
    if errors:
        out["errors"] = errors
    return out


def download(ref: str, output: str, fmt: str | None = None, account: str | None = None) -> dict:
    """The file's bytes at `output`: a Google file in the export format
    asked for, any other file as it is stored."""
    acct, svc, meta = _locate(file_id(ref), account)
    mime = meta.get("mimeType") or ""
    if mime in FILE_EXPORTS:
        fmt = fmt or DEFAULT_FORMAT[mime]
        target = FILE_EXPORTS[mime].get(fmt)
        if not target:
            raise DriveError(f"a Google {_kind(mime)} file exports as "
                             f"{', '.join(sorted(FILE_EXPORTS[mime]))}, not {fmt}")
        data = _export(svc, meta, target)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise DriveError(f"the export is over {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    elif mime.startswith(GOOGLE_PREFIX):
        raise DriveError(f"{meta.get('name')!r} is a Google {_kind(mime)} file, which cannot be "
                         "downloaded")
    else:
        if fmt:
            raise DriveError(f"--format applies to a Google Docs, Sheets or Slides file; "
                             f"{meta.get('name')!r} is a stored {mime} file and is saved as it is")
        data = _media(svc, meta, MAX_DOWNLOAD_BYTES)
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    out = _base(acct, meta)
    out.update({"saved": str(path), "bytes": len(data), "format": fmt,
                "note": "read it with doc_text.py"})
    return out


def check_accounts() -> dict:
    """Each signed-in account's own name, as Drive reports it (the owner's check)."""
    rows = []
    for acct in signed_in_accounts():
        try:
            about = _service(acct).about().get(fields="user(emailAddress)").execute()
            user = str((about.get("user") or {}).get("emailAddress") or "")
            row = {"account": acct, "ok": True, "drive_user": user}
            if user and user.lower() != acct:
                # An alias of the same account differs too, so this is a note, not a failure.
                row["note"] = (f"Drive names this sign-in {user}; if that is another account, "
                               f"delete {_token_path(acct).name} and sign in again")
            rows.append(row)
        except DriveError as e:
            rows.append({"account": acct, "ok": False, "message": e.message, "fix": e.fix})
        except HttpError as e:
            rows.append({"account": acct, "ok": False,
                         "message": ("the Google Drive API is not enabled" if _api_disabled(e)
                                     else f"HTTP {_http_status(e)}"),
                         "fix": API_FIX if _api_disabled(e) else SIGN_IN_FIX})
    return {"status": "ok" if rows and all(r["ok"] for r in rows) else "error",
            "accounts": rows,
            **({} if rows else {"message": "no Google account is signed in for Drive",
                                "fix": SIGN_IN_FIX})}


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The CLI's parser, also what the mediator's argv tripwire parses with
    (tests/test_mediator_argv_tripwire.py)."""
    parser = argparse.ArgumentParser(description="Google Drive reader (read only)",
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command")

    rd = sub.add_parser("read", help="The text of a Google Doc, Sheet, Slides or text file",
                        allow_abbrev=False)
    rd_ref = rd.add_mutually_exclusive_group(required=True)
    rd_ref.add_argument("--url", help="The file's docs.google.com or drive.google.com link")
    rd_ref.add_argument("--id", help="The file's Drive id")
    rd.add_argument("--account", help="Read with this signed-in account only")
    rd.add_argument("--start", type=int, default=0,
                    help="First character to return; negative counts from the end")
    rd.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help=f"Characters to return (default {DEFAULT_MAX_CHARS})")

    sr = sub.add_parser("search", help="Find files by words, list a folder, or list recent files",
                        allow_abbrev=False)
    sr.add_argument("--query", help="Words in the file's name or text")
    sr.add_argument("--folder", help="A folder's link or id: list the files in it")
    sr.add_argument("--account", help="Search this signed-in account only")
    sr.add_argument("--max-results", type=int, default=20, help="At most this many per account")

    dl = sub.add_parser("download", help="Save a file (a Google file in an export format)",
                        allow_abbrev=False)
    dl_ref = dl.add_mutually_exclusive_group(required=True)
    dl_ref.add_argument("--url", help="The file's docs.google.com or drive.google.com link")
    dl_ref.add_argument("--id", help="The file's Drive id")
    dl.add_argument("--output", required=True, help="Where to save it, under ~/.ea")
    dl.add_argument("--format", choices=FORMATS,
                    help="Export format of a Google file (default: docx, xlsx or pptx)")
    dl.add_argument("--account", help="Use this signed-in account only")

    sub.add_parser("accounts", help="The accounts signed in for Drive", allow_abbrev=False)

    st = sub.add_parser("setup", help="Sign one Google account in for Drive (the owner, a terminal)",
                        allow_abbrev=False)
    st.add_argument("--email", required=True, help="The Google account")

    sub.add_parser("test", help="Check every signed-in account against Drive (the owner)",
                   allow_abbrev=False)
    return parser


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm
    _mm.forward_or_none("drive_client", argv)  # ea-bot: the mediator runs it as the owner
    parser = build_parser()
    args = parser.parse_args(argv)
    _mm.check_child_parse(parser, args)
    _mm.refuse_unplain_ids("drive_client", args.command or "", argv)
    try:
        if args.command == "read":
            _print(read(args.url or args.id, args.account, args.start, args.max_chars))
        elif args.command == "search":
            _print(search(args.query, args.folder, args.account, args.max_results))
        elif args.command == "download":
            _print(download(args.url or args.id, args.output, args.format, args.account))
        elif args.command == "accounts":
            _print({"status": "ok", "accounts": signed_in_accounts()})
        elif args.command == "setup":
            email = _check_email(args.email)
            if not authenticate_account(email, interactive=True):
                raise DriveError(f"the sign-in of {email} did not finish", SIGN_IN_FIX)
            result = check_accounts()
            _print(result)
            return 0 if result["status"] == "ok" else 1
        elif args.command == "test":
            result = check_accounts()
            _print(result)
            return 0 if result["status"] == "ok" else 1
        else:
            parser.print_help()
            return 2
    except DriveError as e:
        _print(e.as_json())
        return 1
    except HttpError as e:
        _print({"status": "error", "message": f"Google Drive answered HTTP {_http_status(e)}",
                "fix": API_FIX if _api_disabled(e) else SHARE_FIX})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
