"""
Gmail API client for draft management and sending.

Reuses the same Google OAuth credentials as google_calendar_client.py
(~/.ea/auth/google_client_secret.json) but with Gmail-specific scopes.
Token files are separate: google_token_<email>_gmail.json.

This client makes every Gmail draft the EA writes, in every account. The
claude.ai Gmail connector reads the primary account; its create_draft and
update_draft are denied (scripts/disallowed-tools.txt) because create_draft
rewrote every link in a body as a google.com/url?q= redirect. A draft here
is plain text, so a link stays the address it was given. The token-gated
pair (read a draft back for the confirmation block, send it once the user
has approved) is here too.

Setup:
    python3 preprocessor/gmail_client.py --setup --email you@gmail.com

Usage:
    from gmail_client import get_draft, send_draft

    draft = get_draft("you@gmail.com", "r1234567890", with_fingerprint=True)
    # Returns: {"id", "to", "cc", "subject", "body", "threadId", "fingerprint"}

    result = send_draft("you@gmail.com", "r1234567890", fingerprint=draft["fingerprint"])
    # Returns: {"status": "sent", "messageId": "..."}, or "changed" when the
    # draft no longer has that content
"""

import argparse
import base64
import json
import os
import re
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    import draft_fingerprint as _dfp
    import draft_lock as _draft_lock
    import mail_links as _mail_links
    import owner_trust as _owner_trust
    import published_apps as _published
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import draft_fingerprint as _dfp
    from preprocessor import draft_lock as _draft_lock
    from preprocessor import mail_links as _mail_links
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

# send: send as the user. readonly: read a draft back for the
# confirmation block. compose: create and modify drafts.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _token_path(email: str) -> Path:
    """Per-account Gmail token file (separate from Calendar tokens)."""
    safe_name = email.replace("@", "_at_").replace(".", "_")
    return AUTH_DIR / f"google_token_{safe_name}_gmail.json"


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
        # Same rule as google_calendar_client._save_token: the token file
        # mode (0600 mediated, 0640 group ea otherwise), never 0660.
        mode, gid = _owner_trust.token_file_mode()
        _owner_trust.write_owner_file(token_file, creds.to_json(), mode=mode, gid=gid,
                                      allow_private_group=True)
        return
    token_file.write_text(creds.to_json())
    try:
        os.chmod(token_file, 0o660)
    except OSError:
        pass


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
# Authentication (mirrors google_calendar_client.py pattern)
# -------------------------------------------------------------------
def authenticate_account(email: str, interactive: bool = True) -> Optional[Credentials]:
    """Get valid Gmail credentials. Opens browser on first run."""
    token_file = _token_path(email)
    creds = None

    if token_file.exists():
        try:
            creds = _load_token(token_file)
        except _owner_trust.OwnerTrustError as e:
            print(f"Gmail token refused for {email}: {e}", file=sys.stderr)
            return None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_file, creds)
            return creds
        except Exception as e:
            print(f"Gmail token refresh failed for {email}: {e}", file=sys.stderr)
            if not interactive:
                return None

    if not interactive:
        print(f"No valid Gmail token for {email} and not in interactive mode.",
              file=sys.stderr)
        return None

    source = _client_source()
    if not source:
        print(
            f"ERROR: Google client secret not found at {CLIENT_SECRET_FILE}\n"
            f"Download from Google Cloud Console > APIs & Services > Credentials",
            file=sys.stderr,
        )
        return None

    print(f"\nAuthenticating Gmail for: {email}", file=sys.stderr)
    if source == "file":
        print("Make sure the Gmail API is enabled in your Google Cloud project:", file=sys.stderr)
        print("  APIs & Services > Library > search 'Gmail API' > Enable\n", file=sys.stderr)

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _client_flow()
    flow.redirect_uri = "http://localhost:8400"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    print(f"Open this URL in your browser:\n{auth_url}\n", file=sys.stderr)
    print("After signing in, paste the full redirect URL here:", file=sys.stderr)

    redirect_response = input("Paste URL: ").strip()
    flow.fetch_token(authorization_response=redirect_response)
    creds = flow.credentials

    _save_token(token_file, creds)
    print(f"Gmail token saved for {email}", file=sys.stderr)
    return creds


def _get_service(email: str, interactive: bool = False):
    """Build Gmail API service for an account."""
    creds = authenticate_account(email, interactive=interactive)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# -------------------------------------------------------------------
# Inbox search
# -------------------------------------------------------------------
def search_messages(email: str, query: str, max_results: int = 20) -> list:
    """Search a Gmail account using standard Gmail query syntax.

    Returns a list of message summaries (no body), oldest-first within
    the matched set. Used by skills/plugins that need to scan a
    non-MCP-attached account (the Gmail MCP is bound to the primary only).

    Each item:
      {"id", "thread_id", "subject", "from", "to", "date",
       "snippet", "label_ids"}
    """
    service = _get_service(email)
    if not service:
        return [{"error": f"No Gmail credentials for {email}"}]
    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
    except HttpError as e:
        return [{"error": f"Gmail API error: {e}"}]

    refs = resp.get("messages", []) or []
    out = []
    for ref in refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "To", "Date"],
            ).execute()
        except HttpError:
            continue
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", "")[:300],
            "label_ids": msg.get("labelIds", []),
        })
    return out


# -------------------------------------------------------------------
# Draft operations
# -------------------------------------------------------------------
def _addresses(header_value: str) -> list[str]:
    """Bare addresses from a To/From/Cc header ("Name <a@b>, c@d")."""
    from email.utils import getaddresses
    return [addr for _name, addr in getaddresses([header_value or ""]) if addr]


def _reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if not subject:
        return ""
    return subject if re.match(r"^\s*re\s*:", subject, re.I) else f"Re: {subject}"


def _threading_for_reply(service, message_id: str) -> dict:
    """What a reply to `message_id` must carry to land in its thread: the
    Gmail threadId, In-Reply-To (the original's Message-ID), References
    (the original's chain plus its Message-ID), the Re: subject, and the
    address to answer (Reply-To, else From). Gmail threads a draft only
    when threadId is set AND the subject matches, and every other client
    reads In-Reply-To/References; a draft without them opens a new
    conversation, which is what happened to the Puppet replies."""
    try:
        orig = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["Message-ID", "Message-Id", "References", "Subject",
                             "From", "Reply-To", "To", "Cc"]).execute()
    except HttpError as e:
        return {"error": f"Gmail API error reading {message_id}: {e}"}
    headers = {(h.get("name") or "").lower(): h.get("value", "")
               for h in (orig.get("payload") or {}).get("headers", []) or []}
    msgid = headers.get("message-id", "")
    refs = " ".join(x for x in (headers.get("references", "").strip(), msgid) if x)
    return {"thread_id": orig.get("threadId", ""),
            "in_reply_to": msgid,
            "references": refs,
            "subject": _reply_subject(headers.get("subject", "")),
            "reply_to": headers.get("reply-to") or headers.get("from", ""),
            "to": headers.get("to", ""), "cc": headers.get("cc", "")}


def _newest_message_in_thread(service, thread_id: str) -> str:
    """The id of the last non-draft message in a thread, to reply to when
    the caller knows the thread but not the message."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="minimal").execute()
    except HttpError as e:
        return ""
    msgs = [m for m in thread.get("messages", []) or []
            if "DRAFT" not in (m.get("labelIds") or [])]
    return (msgs or thread.get("messages", []) or [{}])[-1].get("id", "")


def _build_raw(email: str, to: list[str], subject: str, body: str,
               cc: list[str] | None, bcc: list[str] | None,
               in_reply_to: str = "", references: str = "") -> str:
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["From"] = email
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _resolve_threading(service, reply_to_message_id: str, thread_id: str) -> dict:
    """Threading fields for a reply request: by message id, or by thread id
    (answering the thread's newest message). {} when the draft is fresh."""
    if not reply_to_message_id and thread_id:
        reply_to_message_id = _newest_message_in_thread(service, thread_id)
    if not reply_to_message_id:
        return {"thread_id": thread_id} if thread_id else {}
    info = _threading_for_reply(service, reply_to_message_id)
    if "error" in info:
        return info
    if thread_id:
        info["thread_id"] = thread_id
    info["message_id"] = reply_to_message_id
    return info


def _quote_original(service, message_id: str) -> str:
    """The message `message_id` quoted under a plain-text reply, the way
    Gmail quotes it: "On <date> <sender> wrote:", then each line of its
    text with ">" in front. The date is in this machine's time zone. ""
    when the message cannot be read or has no text."""
    from email.utils import parsedate_to_datetime
    try:
        orig = service.users().messages().get(
            userId="me", id=message_id, format="full").execute()
    except HttpError:
        return ""
    payload = orig.get("payload") or {}
    headers = {(h.get("name") or "").lower(): h.get("value", "")
               for h in payload.get("headers", []) or []}
    text = _extract_body(payload).replace("\r\n", "\n").strip()
    if not text:
        return ""
    when = ""
    try:
        dt = parsedate_to_datetime(headers.get("date", "")).astimezone()
        when = (f"{dt:%a, %b} {dt.day}, {dt.year} at "
                f"{dt.hour % 12 or 12}:{dt:%M %p} ")
    except (TypeError, ValueError):
        pass
    sender = headers.get("from", "").strip() or "the sender"
    quoted = "\n".join(f"> {line}".rstrip() if line.strip() else ">"
                       for line in text.splitlines())
    return f"On {when}{sender} wrote:\n{quoted}\n"


def _with_quote(body: str, quote: str) -> str:
    """`body` with `quote` below it, unless `body` already ends in quoted
    history or there is nothing to quote."""
    if not quote or _mail_links.quote_start(body) < len(body):
        return body
    return body.rstrip() + "\n\n" + quote


def create_draft(email: str, to: list[str], subject: str, body: str,
                 cc: list[str] | None = None,
                 bcc: list[str] | None = None, *,
                 reply_to_message_id: str = "", thread_id: str = "",
                 quote: bool = True) -> dict:
    """Create a Gmail draft in the named account.

    A reply names the message it answers (`reply_to_message_id`, a Gmail
    message id) or its thread (`thread_id`); the draft then carries that
    threadId, In-Reply-To and References, defaults its subject to the
    original's "Re:" form and its recipient to the original's Reply-To or
    From when `to` is empty, and quotes the original below the body
    (`quote=False` leaves it out; a body that already ends in quoted
    history gets no second quote). Without either it is a new
    conversation.

    Returns {"draft_id", "message_id", "thread_id", "in_reply_to",
    "quoted"} on success or {"error": ...} on failure.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}

    threading = _resolve_threading(service, reply_to_message_id, thread_id)
    if "error" in threading:
        return threading
    if not to and threading.get("reply_to"):
        to = _addresses(threading["reply_to"])
    if not subject and threading.get("subject"):
        subject = threading["subject"]
    if not to:
        return {"error": "no recipient: pass to=, or reply to a message that has a sender"}
    if quote and threading.get("message_id"):
        body = _with_quote(body, _quote_original(service, threading["message_id"]))
    raw = _build_raw(email, to, subject, body, cc, bcc,
                     in_reply_to=threading.get("in_reply_to", ""),
                     references=threading.get("references", ""))
    message: dict = {"raw": raw}
    if threading.get("thread_id"):
        message["threadId"] = threading["thread_id"]

    try:
        draft = service.users().drafts().create(
            userId="me", body={"message": message}
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}

    created = draft.get("message", {})
    return {
        "draft_id": draft.get("id", ""),
        "message_id": created.get("id", ""),
        "thread_id": created.get("threadId", ""),
        "in_reply_to": threading.get("in_reply_to", ""),
        "quoted": _mail_links.quote_start(body) < len(body),
    }


def list_drafts(email: str, max_results: int = 10, since_epoch: float = 0.0) -> dict:
    """The account's newest drafts: id, message id, thread id and the
    message's internal time. `since_epoch` keeps only drafts whose message
    is at least that new, which is how the bot finds a draft the Gmail
    MCP created during one run. Reads only; nothing here can send.

    Returns {"drafts": [...]} or {"error": ...}.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        listing = service.users().drafts().list(
            userId="me", maxResults=max(1, min(int(max_results), 50))).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    out = []
    for item in listing.get("drafts", []) or []:
        did = item.get("id", "")
        if not did:
            continue
        try:
            d = service.users().drafts().get(
                userId="me", id=did, format="metadata").execute()
        except HttpError:
            continue
        message = d.get("message", {}) or {}
        try:
            internal = int(message.get("internalDate", "0")) / 1000.0
        except (TypeError, ValueError):
            internal = 0.0
        if since_epoch and internal and internal < since_epoch:
            continue
        subject = ""
        for h in (message.get("payload", {}) or {}).get("headers", []) or []:
            if (h.get("name") or "").lower() == "subject":
                subject = h.get("value", "")
                break
        out.append({"draft_id": did, "message_id": message.get("id", ""),
                    "thread_id": message.get("threadId", ""),
                    "internal_date": internal, "subject": subject})
    return {"drafts": out}


def _journal(action: str, result: dict, verb: str, email: str,
             ledger_ids=()) -> None:
    """Record a successful draft mutation in the draft-events journal, the
    same journal graph_actions.py writes for Outlook, so the bot posts the
    :lock: block for a Gmail draft the way it does for an Outlook one.
    `ledger_ids` are the rows the draft delivers (`--ledger-id`).
    Best-effort: a journal failure never fails the CLI call."""
    try:
        draft_id = (result or {}).get("draft_id") or ""
        if not draft_id or "error" in (result or {}):
            return
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import draft_events
        draft_events.record(action, draft_id, verb=verb, system="gmail", email=email,
                            ledger_ids=ledger_ids)
    except Exception:
        pass


def delete_draft(email: str, draft_id: str) -> dict:
    """Delete a Gmail draft. Used for cleanup when a draft was created
    in the wrong account (e.g. before the operator specified which
    mailbox to send from)."""
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        service.users().drafts().delete(userId="me", id=draft_id).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    return {"status": "deleted", "draft_id": draft_id}


def mark_read(email: str, message_id: str) -> dict:
    """Mark a Gmail message read (removes the UNREAD label) in place."""
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    return {"status": "marked_read", "message_id": message_id}


def _message_fingerprint(service, message_id: str) -> str:
    """The content fingerprint (draft_fingerprint.py) of one Gmail message,
    from its RFC 822 form. A Gmail message never changes: an edit to a draft
    makes a new message id, so this hash covers exactly the version that has
    `message_id`."""
    raw = service.users().messages().get(userId="me", id=message_id,
                                         format="raw").execute().get("raw") or ""
    return _dfp.gmail_fingerprint_raw(raw)


def get_draft(email: str, draft_id: str, with_fingerprint: bool = False) -> dict:
    """Fetch a Gmail draft by ID. Returns parsed fields for confirmation block.

    With `with_fingerprint`, the result also carries "fingerprint", the
    content fingerprint of the same message version the fields come from
    (the bot's card read; the Send click compares against it), and
    "quote_cut": the index in the first text part (`text_parts[0]`, else
    `body`) where the part a card may collapse begins. That part must end
    an earlier message of the thread (mail_links.verified_quote_start);
    otherwise quote_cut is the text's length and nothing is collapsed.

    Returns:
        {"to": "a@example.com, b@example.com", "cc": "", "subject": "...",
         "body": "...", "redirects": [links in the new text that go
         through a redirect wrapper], "conversation_id": "threadId"}
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}

    try:
        draft = service.users().drafts().get(
            userId="me", id=draft_id, format="full"
        ).execute()

        msg = draft.get("message", {})
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}

        body_text = _extract_body(msg.get("payload", {}))
        html = _extract_html(msg.get("payload", {}))
        # The resources the whole HTML loads when it is opened, quoted part
        # included: the text the card shows drops tag attributes.
        resources = _mail_links.remote_resources(html)
        html = re.split(r'<div[^>]+class="gmail_quote', html, maxsplit=1)[0]

        out = {
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "bcc": headers.get("bcc", ""),
            "subject": headers.get("subject", "(no subject)"),
            "body": body_text,
            "redirects": _mail_links.redirect_links(
                _mail_links.new_text(body_text) + "\n" + html),
            "conversation_id": msg.get("threadId", ""),
            "message_id": msg.get("id", ""),
            "attachments": [p.get("filename") for p in _attachment_parts(msg.get("payload", {}))
                            if p.get("filename")],
            "reply_to": headers.get("reply-to", ""),
            "remote_resources": resources,
            # "body" is one text part. A draft with more than one plain or
            # more than one HTML text part carries text that part leaves
            # out, so every such part is listed here and the card shows
            # them all.
            "text_parts": (_text_parts(msg.get("payload", {}))
                           if _more_than_one_text_part(msg.get("payload", {})) else []),
        }
        if with_fingerprint:
            out["fingerprint"] = _message_fingerprint(service, msg.get("id", ""))
            first = (out["text_parts"] or [body_text])[0]
            out["quote_cut"] = _mail_links.verified_quote_start(
                first, _mail_links.quote_start(first),
                _thread_texts(service, msg.get("threadId", ""), msg.get("id", "")))
        return out
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    except Exception as e:
        return {"error": f"Failed to fetch draft: {e}"}


def _thread_texts(service, thread_id: str, draft_message_id: str) -> list:
    """The text of every sent or received message in a thread: what a
    draft's quoted part must match before a card collapses it. Drafts are
    left out. [] when the thread cannot be read."""
    if not thread_id:
        return []
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full").execute()
    except Exception:
        return []
    out = []
    for m in thread.get("messages", []) or []:
        if m.get("id") == draft_message_id or "DRAFT" in (m.get("labelIds") or []):
            continue
        out.append(_extract_body(m.get("payload", {})))
    return out


def get_message(email: str, message_id: str) -> dict:
    """Fetch a full message by ID from a monitored mailbox.

    `search_messages` returns only a 300-char snippet (no body). When a
    skill/plugin needs the actual content of a message in a
    non-MCP-attached mailbox (e.g. reading a booking confirmation in a
    co-owner account to extract dates / counts / codes), use this.

    Returns:
        {"id", "thread_id", "subject", "from", "to", "cc", "date", "body"}
        or {"error": ...} on failure.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}

    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"]
               for h in payload.get("headers", [])}
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "date": headers.get("date", ""),
        "body": _extract_body(payload),
        "html": _extract_html(payload),
        "attachments": [_attachment_meta(p)
                        for p in _attachment_parts(payload)],
    }


def get_thread(email: str, thread_id: str) -> dict:
    """Fetch a full thread by ID, oldest-first.

    Used when a skill/plugin needs the whole conversation (e.g. reading an
    existing vendor thread before drafting a follow-up). Each message
    carries the same fields as `get_message`.

    Returns {"thread_id", "messages": [ {...}, ... ]} or {"error": ...}.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}

    messages = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"]
                   for h in payload.get("headers", [])}
        messages.append({
            "id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "date": headers.get("date", ""),
            "body": _extract_body(payload),
        })
    return {"thread_id": thread_id, "messages": messages}


def _attachment_parts(payload: dict) -> list[dict]:
    """Every part of a message payload that carries a filename, nested
    multiparts included. Returns the raw part dicts: a real file has
    body.attachmentId, a tiny one can inline body.data instead."""
    parts = []
    if payload.get("filename"):
        parts.append(payload)
    for part in payload.get("parts", []) or []:
        parts.extend(_attachment_parts(part))
    return parts


def _attachment_meta(part: dict) -> dict:
    body = part.get("body", {})
    return {
        "attachment_id": body.get("attachmentId", ""),
        "filename": part.get("filename", ""),
        "mime_type": part.get("mimeType", ""),
        "size": body.get("size", 0),
    }


def list_attachments(email: str, message_id: str) -> dict:
    """List the file attachments on a message.

    Returns {"message_id", "attachments": [{attachment_id, filename,
    mime_type, size}, ...]} or {"error": ...}. Inline images with a
    filename (signature logos) are listed too; mime_type and size are
    there so the caller can tell them from real documents.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    return {
        "message_id": msg.get("id", message_id),
        "attachments": [_attachment_meta(p)
                        for p in _attachment_parts(msg.get("payload", {}))],
    }


def download_attachment(email: str, message_id: str, output: str,
                        attachment_id: str | None = None,
                        name_pattern: str | None = None) -> dict:
    """Download one attachment to disk.

    Pick it by attachment_id (exact) or name_pattern (case-insensitive
    substring on the filename). Zero or multiple matches are errors, so
    skills can rely on deterministic behavior — the same contract as
    graph_actions.py download-attachment on the Outlook side.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}

    parts = _attachment_parts(msg.get("payload", {}))
    if attachment_id:
        candidates = [p for p in parts
                      if p.get("body", {}).get("attachmentId") == attachment_id]
    elif name_pattern:
        pat = name_pattern.lower()
        candidates = [p for p in parts
                      if pat in (p.get("filename") or "").lower()]
    else:
        return {"error": "Provide --attachment-id or --name-pattern"}

    if not candidates:
        return {"error": "No attachment matched",
                "tried": {"attachment_id": attachment_id,
                          "name_pattern": name_pattern},
                "available": [_attachment_meta(p) for p in parts]}
    if len(candidates) > 1:
        return {"error": "Multiple attachments matched; narrow with "
                         "--attachment-id",
                "matches": [_attachment_meta(p) for p in candidates]}

    part = candidates[0]
    body = part.get("body", {})
    data = body.get("data", "")
    if not data:
        aid = body.get("attachmentId", "")
        if not aid:
            return {"error": "Attachment part has neither inline data nor "
                             "an attachmentId",
                    "attachment": _attachment_meta(part)}
        try:
            att = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=aid
            ).execute()
        except HttpError as e:
            return {"error": f"Gmail API error: {e}"}
        data = att.get("data", "")
    try:
        content = base64.urlsafe_b64decode(data)
    except Exception as e:
        return {"error": f"Could not decode attachment data: {e}"}

    out_path = Path(output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(content)
    return {"saved": str(out_path),
            "name": part.get("filename", ""),
            "mime_type": part.get("mimeType", ""),
            "size": len(content)}


def _existing_draft_state(service, draft_id: str) -> dict:
    """The threading, addressing and quoted history an existing draft
    already carries, so an in-place edit keeps the draft in its
    conversation: threadId, In-Reply-To, References, To, Cc, Subject, and
    the quoted history at the end of its text ("" when none). {} when
    unreadable."""
    try:
        d = service.users().drafts().get(
            userId="me", id=draft_id, format="full").execute()
    except HttpError:
        return {}
    msg = d.get("message", {}) or {}
    payload = msg.get("payload") or {}
    headers = {(h.get("name") or "").lower(): h.get("value", "")
               for h in payload.get("headers", []) or []}
    text = _extract_body(payload).replace("\r\n", "\n")
    return {"thread_id": msg.get("threadId", ""),
            "in_reply_to": headers.get("in-reply-to", ""),
            "references": headers.get("references", ""),
            "to": headers.get("to", ""), "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "quote": text[_mail_links.quote_start(text):].strip()}


def _existing_attachments(service, draft_id: str) -> list:
    """The attachment parts of an existing draft, so replacing its text
    does not drop the files on it (attach-file put them there). [] when
    it has none or cannot be read."""
    import email as _email
    try:
        d = service.users().drafts().get(
            userId="me", id=draft_id, format="raw").execute()
    except HttpError:
        return []
    raw = (d.get("message") or {}).get("raw") or ""
    if not raw:
        return []
    msg = _email.message_from_bytes(base64.urlsafe_b64decode(raw.encode("ascii") + b"=="))
    return [part for part in msg.walk()
            if part.get_content_disposition() == "attachment"]


def _with_attachments(raw_b64: str, parts: list) -> str:
    """The plain-text message `raw_b64` (base64url) as multipart/mixed with
    `parts` after its text; `raw_b64` unchanged when there are none."""
    import email as _email
    from email.mime.multipart import MIMEMultipart
    if not parts:
        return raw_b64
    old = _email.message_from_bytes(base64.urlsafe_b64decode(raw_b64))
    new = MIMEMultipart("mixed")
    for h in ("From", "To", "Cc", "Bcc", "Subject", "In-Reply-To", "References"):
        if old.get(h):
            new[h] = old[h]
    text = old.get_payload(decode=True).decode(old.get_content_charset() or "utf-8")
    new.attach(MIMEText(text, "plain", "utf-8"))
    for part in parts:
        new.attach(part)
    return base64.urlsafe_b64encode(new.as_bytes()).decode("ascii")


def update_draft(email: str, draft_id: str, to: list[str], subject: str,
                 body: str, cc: list[str] | None = None,
                 bcc: list[str] | None = None, *,
                 reply_to_message_id: str = "", thread_id: str = "",
                 quote: bool = True) -> dict:
    """Replace the contents of an existing Gmail draft in place.

    Used so an edit updates an existing draft rather than creating a
    duplicate. The draft keeps the thread it is in (threadId,
    In-Reply-To, References are read back and re-applied), and an empty
    `to` or `subject` keeps the existing value. `body` is the new text
    only: the quoted history the draft ends with is kept below it, and
    its attachments stay on it.
    `reply_to_message_id` or `thread_id` moves the draft into that
    conversation instead, which is how a reply drafted as a new
    conversation is repaired, and quotes that message. `quote=False`
    leaves the quoted history out.

    Returns {"draft_id", "message_id", "thread_id", "in_reply_to",
    "quoted", "attachments_kept"} or {"error": ...}.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}

    current = _existing_draft_state(service, draft_id)
    attachments = _existing_attachments(service, draft_id)
    threading = _resolve_threading(service, reply_to_message_id, thread_id)
    if "error" in threading:
        return threading
    if not threading:
        threading = {k: current.get(k, "") for k in ("thread_id", "in_reply_to", "references")}
    if not to:
        to = _addresses(current.get("to", "")) or _addresses(threading.get("reply_to", ""))
    if not subject:
        subject = current.get("subject", "") or threading.get("subject", "")
    if cc is None and current.get("cc"):
        cc = _addresses(current["cc"])
    if not to:
        return {"error": "no recipient: pass to=, or update a draft that has one"}
    if quote:
        if threading.get("message_id"):
            body = _with_quote(body, _quote_original(service, threading["message_id"]))
        elif current.get("quote"):
            body = _with_quote(body, current["quote"] + "\n")
    raw = _build_raw(email, to, subject, body, cc, bcc,
                     in_reply_to=threading.get("in_reply_to", ""),
                     references=threading.get("references", ""))
    message: dict = {"raw": _with_attachments(raw, attachments)}
    if threading.get("thread_id"):
        message["threadId"] = threading["thread_id"]

    try:
        draft = service.users().drafts().update(
            userId="me", id=draft_id, body={"message": message}
        ).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}

    updated = draft.get("message", {})
    return {
        "draft_id": draft.get("id", ""),
        "message_id": updated.get("id", ""),
        "thread_id": updated.get("threadId", ""),
        "in_reply_to": threading.get("in_reply_to", ""),
        "quoted": _mail_links.quote_start(body) < len(body),
        "attachments_kept": len(attachments),
    }


GMAIL_ATTACHMENT_LIMIT_BYTES = 25 * 1024 * 1024


def _guess_mime(path: Path) -> tuple[str, str]:
    import mimetypes
    ctype, _enc = mimetypes.guess_type(str(path))
    ctype = ctype or "application/octet-stream"
    main, _, sub = ctype.partition("/")
    return main, sub or "octet-stream"


def _shrink_image(data: bytes, max_px: int) -> tuple[bytes, str]:
    """A photo re-encoded as JPEG with its long side at most `max_px`.
    Phone originals run 2 to 4 MB each and a dozen of them pass Gmail's
    25 MB cap; an inspector or a colleague needs a legible picture, not
    the sensor dump. Returns (bytes, mime subtype); the input is returned
    unchanged when Pillow is missing or the image is already small."""
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
    except Exception:
        return data, "jpeg"
    try:
        im = Image.open(BytesIO(data))
        im = ImageOps.exif_transpose(im)
        if max(im.size) <= max_px:
            return data, (im.format or "jpeg").lower()
        im.thumbnail((max_px, max_px))
        out = BytesIO()
        im.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), "jpeg"
    except Exception:
        return data, "jpeg"


def _rebuild_with_attachments(raw_bytes: bytes, files: list, names: list | None,
                              sender: str, max_image_px: int = 0) -> tuple[bytes, list, int]:
    """The draft's MIME with `files` added as attachments.

    Headers that carry addressing and threading (From, To, Cc, Bcc,
    Subject, In-Reply-To, References) are copied over; the existing body
    parts are kept as they are; each file becomes a base64 attachment
    part with its own filename. Returns (new raw bytes, attached names,
    total attachment bytes)."""
    import email as _email
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart

    old = _email.message_from_bytes(raw_bytes)
    new = MIMEMultipart("mixed")
    for h in ("From", "To", "Cc", "Bcc", "Subject", "In-Reply-To", "References", "Reply-To"):
        if old.get(h):
            new[h] = old[h]
    if not new.get("From") and sender:
        new["From"] = sender
    # existing content: a multipart/mixed keeps its parts; anything else
    # (a plain body, or a multipart/alternative) rides as one part
    if old.is_multipart() and old.get_content_type() == "multipart/mixed":
        for part in old.get_payload():
            new.attach(part)
    else:
        new.attach(old)
    names = list(names or [])
    attached, total = [], 0
    for i, fpath in enumerate(files):
        p = Path(fpath).expanduser()
        data = p.read_bytes()
        main, sub = _guess_mime(p)
        if max_image_px and main == "image":
            data, sub = _shrink_image(data, max_image_px)
        total += len(data)
        part = MIMEBase(main, sub)
        part.set_payload(data)
        encoders.encode_base64(part)
        fname = names[i] if i < len(names) and names[i] else p.name
        if max_image_px and main == "image" and sub == "jpeg" and not fname.lower().endswith((".jpg", ".jpeg")):
            fname = Path(fname).stem + ".jpg"
        part.add_header("Content-Disposition", "attachment", filename=fname)
        new.attach(part)
        attached.append(fname)
    return new.as_bytes(), attached, total


def remove_attachment(email: str, draft_id: str, name: str) -> dict:
    """Remove the attachments named `name` (any case) from a Gmail draft,
    in place. As for attach_files, the draft is read raw, rebuilt without
    those parts and written with drafts().update; the draft id, thread and
    addressing survive. Only the top-level parts of a multipart/mixed
    draft (where attach-file puts files) are considered.

    Returns {"draft_id", "message_id", "thread_id", "removed",
    "remaining"} or {"error": ...}."""
    import email as _email
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    try:
        d = service.users().drafts().get(userId="me", id=draft_id, format="raw").execute()
    except HttpError as e:
        return {"error": f"Gmail API error reading draft: {e}"}
    msg = d.get("message", {}) or {}
    raw = base64.urlsafe_b64decode((msg.get("raw") or "").encode("ascii") + b"==")
    mime = _email.message_from_bytes(raw)
    if not (mime.is_multipart() and mime.get_content_type() == "multipart/mixed"):
        return {"error": "this draft has no attachments"}
    keep, removed, names = [], [], []
    for part in mime.get_payload():
        fname = part.get_filename() or ""
        if part.get_content_disposition() == "attachment":
            if fname.lower() == name.lower():
                removed.append(fname)
                continue
            names.append(fname)
        keep.append(part)
    if not removed:
        return {"error": "no attachment by that name on this draft", "attachments": names}
    mime.set_payload(keep)
    body: dict = {"message": {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}}
    if msg.get("threadId"):
        body["message"]["threadId"] = msg["threadId"]
    try:
        upd = service.users().drafts().update(userId="me", id=draft_id, body=body).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    updated = upd.get("message", {}) or {}
    return {"draft_id": upd.get("id", draft_id), "message_id": updated.get("id", ""),
            "thread_id": updated.get("threadId", msg.get("threadId", "")),
            "removed": removed, "remaining": names}


def attach_files(email: str, draft_id: str, files: list, names: list | None = None,
                 max_image_px: int = 0) -> dict:
    """Add file attachments to an existing Gmail draft, in place.

    The Gmail API has no attachment endpoint: a draft is one MIME message,
    so the draft is read back raw, rebuilt as multipart/mixed with the new
    parts, and written with drafts().update. The draft id, thread id and
    every addressing and threading header survive; existing attachments
    are kept. Total attachment size is capped at Gmail's 25 MB.

    Returns {"draft_id", "message_id", "thread_id", "attached",
    "attachment_bytes"} or {"error": ...}.
    """
    service = _get_service(email)
    if not service:
        return {"error": f"No Gmail credentials for {email}"}
    paths = [Path(f).expanduser() for f in files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        return {"error": f"file not found: {', '.join(missing)}"}
    try:
        d = service.users().drafts().get(userId="me", id=draft_id, format="raw").execute()
    except HttpError as e:
        return {"error": f"Gmail API error reading draft: {e}"}
    msg = d.get("message", {}) or {}
    raw = base64.urlsafe_b64decode((msg.get("raw") or "").encode("ascii") + b"==")
    new_bytes, attached, total = _rebuild_with_attachments(raw, paths, names, email,
                                                           max_image_px=max_image_px)
    if total > GMAIL_ATTACHMENT_LIMIT_BYTES:
        return {"error": f"attachments total {total} bytes, over Gmail's 25 MB limit"}
    body: dict = {"message": {"raw": base64.urlsafe_b64encode(new_bytes).decode("ascii")}}
    if msg.get("threadId"):
        body["message"]["threadId"] = msg["threadId"]
    try:
        upd = service.users().drafts().update(userId="me", id=draft_id, body=body).execute()
    except HttpError as e:
        return {"error": f"Gmail API error: {e}"}
    updated = upd.get("message", {}) or {}
    return {"draft_id": upd.get("id", draft_id), "message_id": updated.get("id", ""),
            "thread_id": updated.get("threadId", msg.get("threadId", "")),
            "attached": attached, "attachment_bytes": total}


def _extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart: look for text/plain first, then text/html
    parts = payload.get("parts", [])
    plain_body = ""
    html_body = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if not data:
            nested = _extract_body(part)  # nested multipart
            if nested:
                return nested
            continue
        decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if part_mime == "text/plain":
            plain_body = decoded
        elif part_mime == "text/html":
            html_body = decoded

    if plain_body:
        return plain_body
    if html_body:
        text = re.sub(r'<br\s*/?>', '\n', html_body, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        import html
        return html.unescape(text).strip()

    return ""


def _text_leaves(payload: dict) -> dict:
    """{"text/plain": [decoded text, ...], "text/html": [...]} of the text
    parts that are not files, in tree order."""
    found = {"text/plain": [], "text/html": []}

    def _walk(p: dict) -> None:
        kids = p.get("parts") or []
        if kids:
            for k in kids:
                _walk(k)
            return
        mime = (p.get("mimeType") or "").lower()
        if mime in found and not p.get("filename"):
            data = (p.get("body") or {}).get("data", "")
            found[mime].append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                               if data else "")
    _walk(payload or {})
    return found


def _more_than_one_text_part(payload: dict) -> bool:
    """True when the payload has more than one text/plain or more than one
    text/html part that is not a file: the body then has text that the one
    part _extract_body returns does not carry."""
    found = _text_leaves(payload)
    return len(found["text/plain"]) > 1 or len(found["text/html"]) > 1


def _text_parts(payload: dict) -> list[str]:
    """The text of every body part a reader can see, in tree order: every
    plain part, then the text of each HTML part beyond the ones the plain
    parts stand in for (an HTML part is taken as the alternative of the
    plain part in the same place)."""
    import html as _html
    found = _text_leaves(payload)
    plain, rich = found["text/plain"], found["text/html"]

    def _html_text(h: str) -> str:
        t = re.sub(r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))[^>]*>(.*?)</a>""",
                   lambda m: f"{m.group(4)} [{m.group(1) or m.group(2) or m.group(3) or ''}]",
                   h, flags=re.IGNORECASE | re.DOTALL)   # keep the link addresses
        t = re.sub(r'<br\s*/?>', '\n', t, flags=re.IGNORECASE)
        t = re.sub(r'<[^>]+>', '', t)
        return _html.unescape(t).strip()
    return list(plain) + [_html_text(h) for h in rich[len(plain):]]


def _extract_html(payload: dict) -> str:
    """Return the RAW decoded text/html part of a payload, recursing into
    nested multiparts. Unlike _extract_body (which strips tags to plain text
    and thus loses links/structure), this preserves the HTML so callers can
    parse hrefs and layout (e.g. a plugin reading emailed docket digests).
    Returns '' when the message has no text/html part."""
    if payload.get("mimeType", "") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        found = _extract_html(part)
        if found:
            return found
    return ""


def send_draft(email: str, draft_id: str, fingerprint: str = "") -> dict:
    """Send a Gmail draft. Returns {"status": "sent", "messageId": "..."}.

    This is the token-gated operation: only called after user clicks
    the Send button in Slack and a confirmation token is validated.

    `fingerprint` is the content fingerprint of what the owner approved.
    The draft is read again right before the send, and when its content no
    longer has that fingerprint nothing is sent and the status is
    "changed". With no fingerprint nothing is sent.

    The draft's lock (draft_lock.py) is held from that read to the send.
    The mail mediator takes the same lock around every draft-changing verb,
    so no mediated edit lands in between. drafts.send names the draft by
    id: the discovery document's Draft schema says only that `message` is
    "the message content of the draft", not that a send with it sends that
    content, so the send does not rely on it.
    """
    if not fingerprint:
        return {"status": "blocked",
                "error": "Send requires the fingerprint of the content the owner approved."}
    service = _get_service(email)
    if not service:
        return {"status": "error", "error": f"No Gmail credentials for {email}"}
    try:
        with _draft_lock.hold([draft_id]):
            return _checked_send(service, draft_id, fingerprint)
    except _draft_lock.Busy:
        return {"status": "error",
                "error": ("The draft is being changed right now. Nothing was sent; "
                          "try Send again in a minute.")}


def _checked_send(service, draft_id: str, fingerprint: str) -> dict:
    """The content check and the send, run under the draft's lock
    (send_draft)."""
    try:
        d = service.users().drafts().get(userId="me", id=draft_id, format="raw").execute()
        current = _dfp.gmail_fingerprint_raw((d.get("message") or {}).get("raw") or "")
    except HttpError as e:
        return {"status": "error", "error": f"Gmail API error reading draft: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"Could not read the draft: {e}"}
    if current != fingerprint:
        return {"status": "changed",
                "error": ("The draft changed after it was approved. Nothing was sent; "
                          "the owner reviews the new content on a new card."),
                "fingerprint": current}

    try:
        result = service.users().drafts().send(
            userId="me", body={"id": draft_id}
        ).execute()

        return {
            "status": "sent",
            "messageId": result.get("id", ""),
            "threadId": result.get("threadId", ""),
        }
    except HttpError as e:
        return {"status": "error", "error": f"Gmail API error: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"Send failed: {e}"}


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------
def _send_token_ok(token, draft_id: str) -> dict | None:
    """Consume the one-time Send token for this draft and return its entry
    (which carries the reviewed content's fingerprint), else None.
    graph_actions holds the token store; it is imported on this path only."""
    if not token:
        return False
    try:
        import graph_actions
    except ModuleNotFoundError:
        from preprocessor import graph_actions
    return graph_actions._validate_and_consume_token(token, "send", draft_id) or None


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    parser = argparse.ArgumentParser(description="Gmail API client", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="Authenticate a Gmail account", allow_abbrev=False)
    setup.add_argument("--email", required=True, help="Gmail address")

    get = sub.add_parser("get-draft", help="Fetch a draft", allow_abbrev=False)
    get.add_argument("--email", required=True)
    get.add_argument("--draft-id", required=True)
    get.add_argument("--with-fingerprint", action="store_true",
                     help="Add the content fingerprint of this draft version")

    send = sub.add_parser("send-draft", help="Send a draft", allow_abbrev=False)
    send.add_argument("--email", required=True)
    send.add_argument("--draft-id", required=True)
    send.add_argument("--confirm-token", default=None,
                      help="One-time approval token the bot mints on a Send click")
    send.add_argument("--fingerprint", default=None,
                      help="Content fingerprint of the reviewed draft (get-draft "
                           "--with-fingerprint); must match the token's")

    srch = sub.add_parser("search", help="Search messages by Gmail query", allow_abbrev=False)
    srch.add_argument("--email", required=True,
                      help="Gmail account whose inbox to search")
    srch.add_argument("--query", required=True,
                      help="Gmail search syntax (e.g. 'from:foo newer_than:30d')")
    srch.add_argument("--max-results", type=int, default=20,
                      help="Max number of messages to return (default 20)")

    cdraft = sub.add_parser("create-draft", help="Create a draft in the named account", allow_abbrev=False)
    cdraft.add_argument("--email", required=True,
                        help="Gmail account whose Drafts folder receives the new draft")
    cdraft.add_argument("--to", default="",
                        help="Comma-separated recipient email addresses (a reply "
                             "defaults to the original sender)")
    cdraft.add_argument("--subject", default="",
                        help="Subject line (a reply defaults to Re: the original)")
    cdraft.add_argument("--body-file", required=True,
                        help="Path to a file whose contents become the plain-text body")
    cdraft.add_argument("--cc", default="", help="Comma-separated Cc recipients")
    cdraft.add_argument("--bcc", default="", help="Comma-separated Bcc recipients")
    cdraft.add_argument("--reply-to-message-id", default="",
                        help="Gmail message id this draft answers: the draft joins its "
                             "thread with In-Reply-To and References set. Use it for "
                             "EVERY reply; a Re: draft without it opens a new conversation")
    cdraft.add_argument("--thread-id", default="",
                        help="Gmail thread id to answer (the thread's newest message) "
                             "when you have the thread but not the message")
    cdraft.add_argument("--no-quote", action="store_true",
                        help="A reply leaves the original out; by default it is "
                             "quoted below the body")
    cdraft.add_argument("--ledger-id", action="append", default=[],
                        help="Ledger row this draft delivers (repeatable, or "
                             "comma-separated). Sending it closes exactly these "
                             "rows; with none named, a send closes only a reply "
                             "row on its thread.")

    ldrafts = sub.add_parser("list-drafts",
                             help="Newest drafts (ids, times); a read",
                             allow_abbrev=False)
    ldrafts.add_argument("--email", required=True, help="Gmail address")
    ldrafts.add_argument("--max-results", type=int, default=10)
    ldrafts.add_argument("--since", type=float, default=0.0,
                         help="Only drafts whose message is at least this new (epoch seconds)")

    mread = sub.add_parser("mark-read", help="Mark a message read in place (removes UNREAD)",
                           allow_abbrev=False)
    mread.add_argument("--email", required=True, help="Gmail address")
    mread.add_argument("--message-id", required=True)

    ddraft = sub.add_parser("delete-draft", help="Delete a draft by ID", allow_abbrev=False)
    ddraft.add_argument("--email", required=True,
                        help="Gmail account whose draft to delete")
    ddraft.add_argument("--draft-id", required=True)

    gmsg = sub.add_parser("get-message",
                          help="Fetch a full message body by ID",
                          allow_abbrev=False)
    gmsg.add_argument("--email", required=True,
                      help="Gmail account the message lives in")
    gmsg.add_argument("--message-id", required=True)

    gthr = sub.add_parser("get-thread",
                          help="Fetch a full thread (oldest-first) by ID",
                          allow_abbrev=False)
    gthr.add_argument("--email", required=True,
                      help="Gmail account the thread lives in")
    gthr.add_argument("--thread-id", required=True)

    latt = sub.add_parser("list-attachments",
                          help="List file attachments on a message",
                          allow_abbrev=False)
    latt.add_argument("--email", required=True,
                      help="Gmail account the message lives in")
    latt.add_argument("--message-id", required=True)

    datt = sub.add_parser("download-attachment",
                          help="Download one attachment to disk",
                          allow_abbrev=False)
    datt.add_argument("--email", required=True,
                      help="Gmail account the message lives in")
    datt.add_argument("--message-id", required=True)
    datt.add_argument("--attachment-id", default=None,
                      help="Exact id from list-attachments")
    datt.add_argument("--name-pattern", default=None,
                      help="Case-insensitive substring of the filename")
    datt.add_argument("--output", required=True,
                      help="Destination file path. Under mail mediation it "
                           "must sit inside an ea-bot dir: ~/.ea/uploads, "
                           "receipts, docs, state, or /tmp")

    afile = sub.add_parser("attach-file",
                           help="Add one or more file attachments to an existing draft",
                           allow_abbrev=False)
    afile.add_argument("--email", required=True, help="Gmail account that owns the draft")
    afile.add_argument("--draft-id", required=True)
    afile.add_argument("--file", action="append", required=True,
                       help="Path to a file (repeatable)")
    afile.add_argument("--name", action="append", default=None,
                       help="Filename override, one per --file, in order")
    afile.add_argument("--max-image-px", type=int, default=0,
                       help="Re-encode photos so the long side is at most this many "
                            "pixels (JPEG q85); 0 keeps originals. 1600 fits a dozen "
                            "phone photos under Gmail's 25 MB cap")
    afile.set_defaults(_verb="attach-file")

    rfile = sub.add_parser("remove-attachment",
                           help="Remove an attachment from an existing draft, by file name",
                           allow_abbrev=False)
    rfile.add_argument("--email", required=True, help="Gmail account that owns the draft")
    rfile.add_argument("--draft-id", required=True)
    rfile.add_argument("--name", required=True, help="The attachment's file name (any case)")
    rfile.set_defaults(_verb="remove-attachment")

    udraft = sub.add_parser("update-draft",
                            help="Replace an existing draft's contents in place",
                            allow_abbrev=False)
    udraft.add_argument("--email", required=True,
                        help="Gmail account whose draft to update")
    udraft.add_argument("--draft-id", required=True)
    udraft.add_argument("--to", default="",
                        help="Comma-separated recipient email addresses (empty keeps "
                             "the draft's)")
    udraft.add_argument("--subject", default="",
                        help="Subject line (empty keeps the draft's)")
    udraft.add_argument("--body-file", required=True,
                        help="Path to a file whose contents become the plain-text body")
    udraft.add_argument("--cc", default="", help="Comma-separated Cc recipients")
    udraft.add_argument("--bcc", default="", help="Comma-separated Bcc recipients")
    udraft.add_argument("--reply-to-message-id", default="",
                        help="Move the draft into the thread of this Gmail message id "
                             "(In-Reply-To and References set); without it the draft "
                             "keeps the thread it is in")
    udraft.add_argument("--thread-id", default="",
                        help="Move the draft into this Gmail thread (answers its newest "
                             "message)")
    udraft.add_argument("--ledger-id", action="append", default=[],
                        help="Ledger row this draft delivers (repeatable, or "
                             "comma-separated). Sending it closes exactly these "
                             "rows; with none named, a send closes only a reply "
                             "row on its thread.")
    udraft.add_argument("--no-quote", action="store_true",
                        help="Drop the quoted history; by default the draft's quote "
                             "stays below the new body")

    # Legacy: --setup flag
    parser.add_argument("--setup", action="store_true", help="Run setup")
    parser.add_argument("--email", help="Gmail address for setup")
    return parser


def main():
    from mail_mediation import forward_or_none
    forward_or_none("gmail_client", sys.argv[1:])  # ea-bot: mediate SAFE, refuse gated
    parser = build_parser()
    args = parser.parse_args()
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mediated = _mm_child.check_child_parse(parser, args)  # the verb the mediator checked
    # Every id option is a plain mail id, whoever runs the tool.
    _mm_child.refuse_unplain_ids("gmail_client", getattr(args, "command", "") or "", sys.argv[1:])
    # An owner-side draft edit the mediator did not start holds the draft's
    # lock, as the mediator does for the resident (draft_lock.py).
    _mm_child.hold_owner_draft_lock("gmail_client", args.command, sys.argv[1:],
                                    mediated=_mediated)

    if args.setup or args.command == "setup":
        email = getattr(args, "email", None)
        if not email:
            email = input("Gmail address: ").strip()
        creds = authenticate_account(email, interactive=True)
        if creds:
            print(f"Authenticated {email} for Gmail API")
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = service.users().getProfile(userId="me").execute()
            print(f"  Email: {profile.get('emailAddress')}")
            print(f"  Messages: {profile.get('messagesTotal', 'N/A')}")
        else:
            print("Authentication failed", file=sys.stderr)
            sys.exit(1)

    elif args.command == "get-draft":
        result = get_draft(args.email, args.draft_id,
                           with_fingerprint=args.with_fingerprint)
        print(json.dumps(result, indent=2))

    elif args.command == "send-draft":
        # The same one-time token graph_actions send-gmail-draft takes, so a
        # send never rests on the mediator's verb check alone.
        entry = _send_token_ok(args.confirm_token, args.draft_id)
        if not entry:
            print(json.dumps({
                "error": "BLOCKED: send-draft requires a valid --confirm-token. "
                         "The bot posts the Send button once the draft exists.",
                "draft_id": args.draft_id}))
            sys.exit(2)
        reviewed = entry.get("fingerprint") or ""
        if not reviewed or (args.fingerprint and args.fingerprint != reviewed):
            print(json.dumps({
                "error": "BLOCKED: the token names no reviewed content, or other "
                         "content than --fingerprint. The owner approves it on a new card.",
                "draft_id": args.draft_id}))
            sys.exit(2)
        result = send_draft(args.email, args.draft_id, fingerprint=reviewed)
        print(json.dumps(result, indent=2))

    elif args.command == "search":
        results = search_messages(args.email, args.query, args.max_results)
        print(json.dumps(results, indent=2))

    elif args.command == "create-draft":
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
        to = [a.strip() for a in args.to.split(",") if a.strip()]
        cc = [a.strip() for a in args.cc.split(",") if a.strip()] if args.cc else None
        bcc = [a.strip() for a in args.bcc.split(",") if a.strip()] if args.bcc else None
        if not to and not (args.reply_to_message_id or args.thread_id):
            print(json.dumps({"error": "--to is required unless --reply-to-message-id "
                                       "or --thread-id names what this answers"}))
            sys.exit(2)
        if not args.subject and not (args.reply_to_message_id or args.thread_id):
            print(json.dumps({"error": "--subject is required for a new conversation"}))
            sys.exit(2)
        result = create_draft(args.email, to, args.subject, body, cc=cc, bcc=bcc,
                              reply_to_message_id=args.reply_to_message_id,
                              thread_id=args.thread_id, quote=not args.no_quote)
        _journal("created", result, "create-draft", args.email, args.ledger_id)
        print(json.dumps(result, indent=2))

    elif args.command == "list-drafts":
        result = list_drafts(args.email, args.max_results, args.since)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.command == "mark-read":
        result = mark_read(args.email, args.message_id)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.command == "delete-draft":
        result = delete_draft(args.email, args.draft_id)
        if result.get("status") == "deleted":
            _journal("deleted", {"draft_id": args.draft_id}, "delete-draft", args.email)
        print(json.dumps(result, indent=2))

    elif args.command == "get-message":
        result = get_message(args.email, args.message_id)
        print(json.dumps(result, indent=2))

    elif args.command == "get-thread":
        result = get_thread(args.email, args.thread_id)
        print(json.dumps(result, indent=2))

    elif args.command == "list-attachments":
        result = list_attachments(args.email, args.message_id)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.command == "download-attachment":
        if not args.attachment_id and not args.name_pattern:
            print(json.dumps(
                {"error": "Provide --attachment-id or --name-pattern"}))
            sys.exit(2)
        result = download_attachment(args.email, args.message_id, args.output,
                                     attachment_id=args.attachment_id,
                                     name_pattern=args.name_pattern)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.command == "attach-file":
        result = attach_files(args.email, args.draft_id, args.file, names=args.name,
                              max_image_px=args.max_image_px)
        _journal("updated", result, "attach-file", args.email)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    elif args.command == "remove-attachment":
        result = remove_attachment(args.email, args.draft_id, args.name)
        _journal("updated", result, "remove-attachment", args.email)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    elif args.command == "update-draft":
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
        to = [a.strip() for a in args.to.split(",") if a.strip()]
        cc = [a.strip() for a in args.cc.split(",") if a.strip()] if args.cc else None
        bcc = [a.strip() for a in args.bcc.split(",") if a.strip()] if args.bcc else None
        result = update_draft(args.email, args.draft_id, to, args.subject, body,
                              cc=cc, bcc=bcc,
                              reply_to_message_id=args.reply_to_message_id,
                              thread_id=args.thread_id, quote=not args.no_quote)
        _journal("updated", result, "update-draft", args.email, args.ledger_id)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
