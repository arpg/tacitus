#!/usr/bin/env python3
"""The macOS native-accounts backend, Python side.

On a Mac whose owner chose `mail.backend` or `calendar.backend` "macos",
the owner side reads and writes mail and calendars through the accounts
in System Settings > Internet Accounts, the way Mail and Calendar do. No
app registration and no OAuth token exist on this path: the owner's login
session is the credential. The design, the verb table and its gaps are in
docs/macos-native-accounts.md.

The macOS half is a small helper (deploy/macos/tacitus-native/): Swift for
EventKit and Contacts, and a JavaScript for Automation script for Mail.app,
run by the helper under /usr/bin/osascript. This module is the half that
runs anywhere:

  * the JSON protocol the helper speaks (make_request, parse_response,
    NativeError) and two transports to it: SubprocessTransport (one helper
    process per call, for a shell on the Mac) and QueueTransport (request
    files in an owner-private directory that the helper's launch agent
    serves in the owner's login session, the path every owner service
    takes);
  * NativeHelper, which refuses a gated helper verb unless a client's gate
    method minted a one-call approval for it;
  * FakeNativeHelper, an in-memory helper the tests drive on any OS;
  * NativeMailClient and NativeCalendarClient, the subset of graph_client
    the EA uses most, returning Graph-shaped dicts so that graph_actions,
    the preprocessor, the closure scrubber and the solver read them as
    they read Graph;
  * the owner-private scope (the accounts, mailboxes and calendars the EA
    sees) and the backend choice;
  * the doctor's rows.

The trust model is the mail mediator's (docs/mail-mediation-spec.md). The
resident never reaches the helper: the queue directory is owner-only, and
the helper and Mail.app run in the owner's login session, which another
user cannot send Apple events into. A gated operation (a send, a calendar
write outside the EA-Managed carve-out) passes the same one-time token
check as on Graph, in the method that owns it.

Owner CLI (on the Mac; nothing here is a mediated tool):
  python3 native_accounts.py status          the helper's view of the permissions
  python3 native_accounts.py request-access  ask for Calendars, Contacts and Mail once
  python3 native_accounts.py calendars       every calendar EventKit sees, for the scope file
  python3 native_accounts.py accounts        every Mail account and its mailboxes
  python3 native_accounts.py doctor          the doctor's rows for this backend
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROTOCOL_VERSION = 1
SCOPE_FILE = "native-accounts.json"
DRAFTS_FILE = "native-drafts.json"
QUEUE_DIR = "native-queue"
HELPER_DIR = Path("/usr/local/libexec/tacitus-native")
HELPER_BIN = HELPER_DIR / "tacitus-native"
RESIDENT_USER = "ea-bot"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_S = 60.0

# mail.backend, calendar.backend and availability.provider (availability.py:
# whose free/busy the solver reads, a choice apart from the two backends).
BACKENDS = {"mail": ("graph", "gmail", "macos"),
            "calendar": ("graph", "google", "macos"),
            "availability": ("graph", "owa", "none")}
DEFAULT_BACKENDS = {"mail": "graph", "calendar": "graph", "availability": "graph"}

# What each helper verb does. "gated" verbs leave the machine or write a
# calendar other people read; NativeHelper sends one only with an approval
# that the client's gate method minted for that one call.
HELPER_VERBS = {
    "status": "read", "request-access": "setup",
    "list-calendars": "read", "list-events": "read", "get-event": "read",
    "create-event": "gated", "update-event": "gated", "delete-event": "gated",
    "find-contacts": "read",
    "mail-status": "read", "list-mailboxes": "read", "list-messages": "read",
    "search-messages": "read", "get-message": "read", "find-thread": "read",
    "save-draft": "draft", "delete-draft": "draft",
    "move-message": "inbox", "set-flags": "inbox",
    "send-message": "gated",
}
MAIL_HELPER_VERBS = frozenset({
    "mail-status", "list-mailboxes", "list-messages", "search-messages",
    "get-message", "find-thread", "save-draft", "delete-draft",
    "move-message", "set-flags", "send-message"})
ERROR_CODES = frozenset({
    "bad-request", "unknown-verb", "permission-denied", "not-determined",
    "not-found", "unsupported", "mail-not-running", "out-of-scope",
    "timeout", "internal", "protocol"})

# The graph_actions verbs this backend implements, and the client method
# behind each. The class of a verb is never this module's to choose:
# verb_class() reads it from mail_mediation, so the native backend runs
# under exactly the SAFE / GATED split the mediator enforces.
NATIVE_VERBS = {
    "search-mail": ("mail", "search_messages"),
    "list-mail": ("mail", "list_folder_messages"),
    "get-message": ("mail", "get_message_full"),
    "get-conversation": ("mail", "get_conversation_messages"),
    "create-draft": ("mail", "create_draft"),
    "create-reply": ("mail", "create_reply"),
    "update-draft": ("mail", "update_draft"),
    "delete-draft": ("mail", "delete_draft"),
    "archive-message": ("mail", "archive_message"),
    "send-draft": ("mail", "send_draft"),
    "calendar-view": ("calendar", "get_calendar_view"),
    "get-event": ("calendar", "get_event"),
    "create-event": ("calendar", "create_event"),
    "clear-hold": ("calendar", "clear_hold"),
}

# Mailbox names per account kind, the defaults a scope file may override.
# The doctor checks each against the helper's list-mailboxes.
ROLE_DEFAULTS = {
    "exchange": {"inbox": "Inbox", "sentitems": "Sent Items", "archive": "Archive",
                 "drafts": "Drafts", "deleteditems": "Deleted Items"},
    "gmail": {"inbox": "INBOX", "sentitems": "[Gmail]/Sent Mail",
              "archive": "[Gmail]/All Mail", "drafts": "[Gmail]/Drafts",
              "deleteditems": "[Gmail]/Trash"},
    "icloud": {"inbox": "INBOX", "sentitems": "Sent Messages", "archive": "Archive",
               "drafts": "Drafts", "deleteditems": "Deleted Messages"},
    "imap": {"inbox": "INBOX", "sentitems": "Sent", "archive": "Archive",
             "drafts": "Drafts", "deleteditems": "Trash"},
}
ROLES = ("inbox", "sentitems", "archive", "drafts", "deleteditems")
# The order a message id is looked up in: the copy in an inbox is the one
# a reply or an archive acts on.
LOOKUP_ORDER = ("inbox", "archive", "sentitems", "drafts")

# EventKit availability -> Graph showAs.
SHOW_AS = {"busy": "busy", "free": "free", "tentative": "tentative",
           "unavailable": "oof", "not-supported": "busy"}
# getSchedule's availability view characters, by showAs.
VIEW_CHAR = {"free": "0", "tentative": "1", "busy": "2", "oof": "3"}
RESPONSE = {"accepted": "accepted", "declined": "declined", "tentative": "tentativelyAccepted",
            "pending": "notResponded", "unknown": "none", "delegated": "none",
            "completed": "accepted", "in-process": "notResponded"}
# EventKit has no categories, so an EA hold carries this line in its notes;
# get_calendar_view reports it as the category graph_client.is_ea_hold reads.
HOLD_MARKER = "[EA Hold]"
EA_HOLD_CATEGORY = "EA Hold"


class NativeError(RuntimeError):
    """A refusal or failure from the helper or from this module. `code` is
    one of ERROR_CODES."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code if code in ERROR_CODES else "internal"
        self.detail = detail or {}
        super().__init__(f"{self.code}: {message}")


# ── the protocol ─────────────────────────────────────────────────────

def make_request(verb: str, args: dict | None = None, *, request_id: str | None = None) -> dict:
    """One request: {"v", "id", "verb", "args"}. The id is 32 hex
    characters, the name the queue files carry."""
    if verb not in HELPER_VERBS:
        raise NativeError("unknown-verb", f"no helper verb {verb!r}")
    rid = request_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", rid):
        raise NativeError("bad-request", "a request id is 32 hex characters")
    return {"v": PROTOCOL_VERSION, "id": rid, "verb": verb, "args": dict(args or {})}


def encode_request(req: dict) -> bytes:
    return json.dumps(req, sort_keys=True, ensure_ascii=False).encode("utf-8")


def parse_response(req: dict, raw) -> dict:
    """The result of `req` from the helper's answer `raw` (bytes, text or a
    dict), or NativeError: a protocol fault when the answer is not one
    response to this request, else the helper's own error."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise NativeError("protocol", f"the answer is over {MAX_RESPONSE_BYTES} bytes")
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise NativeError("protocol", f"the answer is not JSON ({e.msg})") from None
    if not isinstance(raw, dict):
        raise NativeError("protocol", "the answer is not a JSON object")
    if raw.get("v") != PROTOCOL_VERSION:
        raise NativeError("protocol", f"protocol version {raw.get('v')!r}, expected {PROTOCOL_VERSION}")
    if raw.get("id") != req.get("id"):
        raise NativeError("protocol", "the answer is for another request")
    ok = raw.get("ok")
    if ok is True:
        result = raw.get("result")
        if not isinstance(result, dict):
            raise NativeError("protocol", "an ok answer carries no result object")
        return result
    if ok is False:
        err = raw.get("error") if isinstance(raw.get("error"), dict) else {}
        raise NativeError(str(err.get("code") or "internal"),
                          str(err.get("message") or "the helper gave no reason"),
                          {k: v for k, v in err.items() if k not in ("code", "message")})
    raise NativeError("protocol", "the answer has no ok field")


# ── transports ───────────────────────────────────────────────────────

class SubprocessTransport:
    """One helper process per call: `tacitus-native call`, the request on
    stdin and the answer on stdout. For an owner shell on the Mac; the
    services use QueueTransport, since the helper must run in the owner's
    login session to reach EventKit and Mail."""

    def __init__(self, helper_path=HELPER_BIN, timeout: float = DEFAULT_TIMEOUT_S,
                 runner=subprocess.run):
        self.helper_path = str(helper_path)
        self.timeout = timeout
        self._run = runner

    def __call__(self, req: dict) -> bytes:
        try:
            p = self._run([self.helper_path, "call"], input=encode_request(req),
                          capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise NativeError("timeout", f"the helper did not answer in {self.timeout:.0f} s") from None
        except OSError as e:
            raise NativeError("internal", f"the helper did not start ({e.strerror})") from None
        if not p.stdout:
            tail = (p.stderr or b"")[-300:].decode("utf-8", "replace").strip()
            raise NativeError("internal", f"the helper exited {p.returncode} with no answer"
                              + (f": {tail}" if tail else ""))
        return p.stdout


def _check_queue_dir(path: Path) -> None:
    """The queue must be a real directory of this euid that nobody else can
    read or enter: request files carry mail and drafts."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NativeError("internal", f"the helper queue {path} does not exist") from None
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode):
        raise NativeError("protocol", f"the helper queue {path} is not a directory")
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise NativeError("protocol", f"the helper queue {path} must be this user's, mode 0700")


class QueueTransport:
    """Request files in an owner-private directory, served by the helper's
    launch agent: `req-<id>.json` in, `resp-<id>.json` out. Both sides write
    a file under a temporary name and rename it, so a reader never sees half
    a file."""

    def __init__(self, queue_dir, timeout: float = DEFAULT_TIMEOUT_S, poll_s: float = 0.1):
        self.queue_dir = Path(queue_dir)
        self.timeout = timeout
        self.poll_s = poll_s

    def __call__(self, req: dict) -> bytes:
        from owner_trust import read_owner_file, write_owner_file
        _check_queue_dir(self.queue_dir)
        req_path = self.queue_dir / f"req-{req['id']}.json"
        resp_path = self.queue_dir / f"resp-{req['id']}.json"
        write_owner_file(req_path, encode_request(req), mode=0o600)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if resp_path.exists():
                data = read_owner_file(resp_path)
                resp_path.unlink(missing_ok=True)
                return data
            time.sleep(self.poll_s)
        # A request nobody took is withdrawn, so a late helper does not act on it.
        req_path.unlink(missing_ok=True)
        raise NativeError("timeout", f"the helper agent did not answer in {self.timeout:.0f} s "
                          "(is it loaded? launchctl print gui/$(id -u)/org.ea-system.tacitus-native)")


class _Approval:
    """A pass for one gated helper call, minted by a client's gate method
    after its check and spent by the first use."""

    __slots__ = ("verb", "used")

    def __init__(self, verb: str):
        self.verb = verb
        self.used = False


class NativeHelper:
    """The helper behind a transport. A gated verb needs an _Approval for
    that verb that has not been used."""

    def __init__(self, transport):
        self._transport = transport

    def call(self, verb: str, args: dict | None = None, *, approval: _Approval | None = None) -> dict:
        req = make_request(verb, args)
        if HELPER_VERBS[verb] == "gated":
            if not isinstance(approval, _Approval) or approval.verb != verb or approval.used:
                raise NativeError("permission-denied",
                                  f"{verb} is gated: it runs only after the owner's approval")
            approval.used = True
        return parse_response(req, self._transport(req))


# ── identities ───────────────────────────────────────────────────────
# Mail.app's own message id is a per-mailbox integer that changes when a
# message moves. The RFC 5322 Message-ID does not, so a message's handle is
# built from it. Every handle is a plain mail id (mail_mediation.is_plain_id):
# a prefix, then base64url without padding.

_MAX_HANDLE = 512
_MSGID_RE = re.compile(r"<([^<>\s]+)>")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text: str) -> str:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("not a native handle") from None


def bare_message_id(value: str) -> str:
    """A Message-ID without its angle brackets or surrounding space."""
    return str(value or "").strip().strip("<>").strip()


def _handle(prefix: str, value: str) -> str:
    h = prefix + _b64(value)
    if len(h) > _MAX_HANDLE:
        raise ValueError(f"a {prefix} handle would be longer than {_MAX_HANDLE} characters")
    return h


def _unhandle(prefix: str, handle: str) -> str:
    if not isinstance(handle, str) or not handle.startswith(prefix):
        raise ValueError(f"not a {prefix} handle")
    return _unb64(handle[len(prefix):])


def message_handle(message_id: str) -> str:
    return _handle("nm_", bare_message_id(message_id))


def message_id_of(handle: str) -> str:
    return _unhandle("nm_", handle)


def conversation_handle(root: str) -> str:
    return _handle("nt_", bare_message_id(root))


def root_of(conversation_id: str) -> str:
    return _unhandle("nt_", conversation_id)


def event_handle(event_id: str, occurrence: str = "") -> str:
    return _handle("ne_", f"{event_id}\x1f{occurrence or ''}")


def event_of(handle: str) -> tuple[str, str]:
    eid, _, occ = _unhandle("ne_", handle).partition("\x1f")
    return eid, occ


def new_draft_handle() -> str:
    return "nd_" + uuid.uuid4().hex


def thread_root(rec: dict) -> str:
    """The Message-ID a message's thread is named by: the first id in
    References (RFC 5322 keeps the first), else In-Reply-To, else its own.
    A reply that carries neither header starts a thread of its own."""
    headers = rec.get("headers") or {}
    for name in ("references", "in-reply-to"):
        ids = _MSGID_RE.findall(str(headers.get(name) or ""))
        if ids:
            return ids[0]
    return bare_message_id(rec.get("message_id"))


# ── scope and backend ────────────────────────────────────────────────

@dataclass
class MailAccount:
    name: str
    kind: str
    addresses: list[str]
    mailboxes: dict[str, str]
    search: list[str]


@dataclass
class NativeScope:
    """What the EA sees: the owner's choice, from the owner-private
    native-accounts.json. The resident cannot write that file, so it cannot
    widen its own view."""
    accounts: list[MailAccount] = field(default_factory=list)
    calendars: dict[str, dict] = field(default_factory=dict)
    default_calendar: str = ""
    ea_managed: str = ""
    shared: dict[str, str] = field(default_factory=dict)
    contacts: bool = False
    search_days: int = 90
    search_body: bool = True
    backend: dict[str, str] = field(default_factory=dict)

    def addresses(self) -> set[str]:
        return {a.lower() for acct in self.accounts for a in acct.addresses}

    def account(self, name: str) -> MailAccount:
        for a in self.accounts:
            if a.name == name:
                return a
        raise NativeError("out-of-scope", f"the Mail account {name!r} is not in the EA's scope")

    def boxes(self, folder: str | None) -> list[tuple[MailAccount, str, str]]:
        """(account, mailbox name, role or "") for a folder argument: a role
        name (inbox, sentitems, ...) in every account, a mailbox name in
        the accounts whose scope lists it, or None for every searchable
        mailbox. A folder in no account's scope is refused."""
        out = []
        if folder is None:
            for a in self.accounts:
                for name in a.search:
                    out.append((a, name, _role_of(a, name)))
            return out
        key = str(folder).strip()
        if key.lower() in ROLES:
            return [(a, a.mailboxes[key.lower()], key.lower()) for a in self.accounts
                    if a.mailboxes.get(key.lower())]
        for a in self.accounts:
            for name in a.search:
                if name.lower() == key.lower():
                    out.append((a, name, _role_of(a, name)))
        if not out:
            raise NativeError("out-of-scope", f"no mailbox named {folder!r} is in the EA's scope")
        return out


def _role_of(acct: MailAccount, mailbox: str) -> str:
    for role, name in acct.mailboxes.items():
        if name == mailbox:
            return role
    return ""


def scope_from_dict(doc: dict) -> NativeScope:
    """A NativeScope from the scope file's JSON; ValueError on a shape
    that would leave the EA reading something the owner did not name."""
    if not isinstance(doc, dict):
        raise ValueError("the scope file holds no JSON object")
    mail = doc.get("mail") or {}
    cal = doc.get("calendar") or {}
    accounts = []
    seen = set()
    for raw in mail.get("accounts") or []:
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("kind") or "imap").strip().lower()
        if not name or name in seen:
            raise ValueError("every Mail account needs a name of its own")
        if kind not in ROLE_DEFAULTS:
            raise ValueError(f"account {name!r}: kind is one of {', '.join(ROLE_DEFAULTS)}")
        boxes = dict(ROLE_DEFAULTS[kind])
        for role, mbox in (raw.get("mailboxes") or {}).items():
            if role not in ROLES:
                raise ValueError(f"account {name!r}: no mailbox role {role!r}")
            boxes[role] = str(mbox)
        addresses = [str(a).strip() for a in raw.get("addresses") or [] if str(a).strip()]
        if not addresses:
            raise ValueError(f"account {name!r}: list the addresses it sends and receives as")
        search = [str(s) for s in raw.get("search")
                  or [boxes["inbox"], boxes["sentitems"], boxes["archive"]]]
        accounts.append(MailAccount(name, kind, addresses, boxes, search))
        seen.add(name)
    calendars = {}
    for raw in cal.get("calendars") or []:
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("every calendar in the scope needs its EventKit id")
        calendars[cid] = {"title": str(raw.get("title") or cid),
                          "blocking": bool(raw.get("blocking", True)),
                          "write": bool(raw.get("write", False))}
    default = str(cal.get("default") or "")
    ea_managed = str(cal.get("ea_managed") or "")
    for label, cid in (("default", default), ("ea_managed", ea_managed)):
        if cid and cid not in calendars:
            raise ValueError(f"calendar.{label} names a calendar that is not in calendar.calendars")
    shared = {str(k).lower(): str(v) for k, v in (cal.get("shared") or {}).items()}
    for addr, cid in shared.items():
        if cid not in calendars:
            raise ValueError(f"calendar.shared[{addr!r}] names a calendar that is not in the scope")
    backend = {}
    for kind, value in (doc.get("backend") or {}).items():
        if kind in BACKENDS and value in BACKENDS[kind]:
            backend[kind] = value
    return NativeScope(accounts=accounts, calendars=calendars, default_calendar=default,
                       ea_managed=ea_managed, shared=shared,
                       contacts=bool((doc.get("contacts") or {}).get("enabled", False)),
                       search_days=int(mail.get("search_days") or 90),
                       search_body=bool(mail.get("search_body", True)), backend=backend)


def scope_path() -> Path:
    from owner_trust import private_dir
    return private_dir() / SCOPE_FILE


def load_scope(path=None) -> NativeScope:
    """The owner-private scope, read through the owner checks."""
    from owner_trust import read_owner_file
    p = Path(path) if path else scope_path()
    return scope_from_dict(json.loads(read_owner_file(p).decode("utf-8")))


def backends(config_path=None, scope: NativeScope | None = None) -> dict:
    """{"mail", "calendar", "availability"}. ea-config.json gives the value the
    resident and the doctor read; in an owner process the owner-private
    scope file's `backend` block outranks it, since the resident can write
    ea-config.json. Neither value widens what the resident may do: the
    verb gate is the same on every backend."""
    import ea_config
    out = dict(ea_config.get_backends(config_path))
    if scope is None:
        try:
            from owner_trust import is_owner_process
            if is_owner_process() and scope_path().exists():
                scope = load_scope()
        except Exception:
            scope = None
    if scope is not None:
        out.update(scope.backend)
    return out


def verb_class(verb: str) -> str:
    """SAFE or GATED for a graph_actions verb, from mail_mediation."""
    import mail_mediation
    if verb in mail_mediation.GATED["graph_actions"]:
        return "GATED"
    if verb in mail_mediation.SAFE["graph_actions"]:
        return "SAFE"
    return "UNKNOWN"


# ── shapes ───────────────────────────────────────────────────────────

def _graph_time(value: str) -> str:
    """An ISO time as Graph writes one: UTC, seconds, a Z."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _addr(name: str, address: str) -> dict:
    return {"emailAddress": {"name": name or "", "address": address or ""}}


def _recipients(rows) -> list:
    return [_addr(r.get("name", ""), r.get("address", "")) for r in rows or []
            if (r or {}).get("address")]


def graph_message(rec: dict, *, role: str = "", full: bool = False) -> dict:
    """A helper message record as the Graph message fields graph_client
    selects (MAIL_FIELDS, and `body` when full). Mail has no categories;
    `parentFolderId` is "<account>/<mailbox>"."""
    name, address = parseaddr(rec.get("sender") or "")
    mid = bare_message_id(rec.get("message_id"))
    content = str(rec.get("content") or "")
    preview = str(rec.get("preview") or content)[:255]
    out = {
        "id": message_handle(mid),
        "internetMessageId": f"<{mid}>" if mid else "",
        "conversationId": conversation_handle(thread_root(rec)) if mid else "",
        "subject": rec.get("subject") or "",
        "from": _addr(name, address),
        "toRecipients": _recipients(rec.get("to")),
        "ccRecipients": _recipients(rec.get("cc")),
        "receivedDateTime": _graph_time(rec.get("date_received")),
        "sentDateTime": _graph_time(rec.get("date_sent")),
        "bodyPreview": " ".join(preview.split())[:255],
        "hasAttachments": bool(rec.get("attachments")),
        "conversationIndex": "",
        "flag": {"flagStatus": "flagged" if rec.get("flagged") else "notFlagged"},
        "isRead": bool(rec.get("read")),
        "importance": "normal",
        "categories": [],
        "isDraft": role == "drafts",
        "parentFolderId": f"{rec.get('account', '')}/{rec.get('mailbox', '')}",
        "mailbox": {"account": rec.get("account", ""), "name": rec.get("mailbox", ""),
                    "role": role},
    }
    if full:
        out["body"] = {"contentType": "Text", "content": content}
        out["attachments"] = [{"name": a.get("name", ""), "contentType": a.get("mime", ""),
                               "size": a.get("size", 0)} for a in rec.get("attachments") or []]
    return out


def _sort_key(m: dict) -> str:
    return m.get("receivedDateTime") or m.get("sentDateTime") or ""


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").split())


# ── drafts ───────────────────────────────────────────────────────────

class DraftRegistry:
    """The EA's drafts, owner-private: what each draft says, and which Mail
    draft holds it now. Mail has no way to edit a saved draft in place, so
    an edit saves a new Mail draft and deletes the old one; the EA's handle
    (nd_<hex>) stays, and the approval fingerprint is computed from here."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self) -> dict:
        from owner_trust import read_owner_file
        try:
            return json.loads(read_owner_file(self.path).decode("utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, doc: dict) -> None:
        from owner_trust import write_owner_file
        write_owner_file(self.path, json.dumps(doc, sort_keys=True, indent=1), mode=0o600)

    def get(self, handle: str) -> dict | None:
        return self._load().get(handle)

    def put(self, handle: str, entry: dict) -> None:
        doc = self._load()
        doc[handle] = entry
        self._save(doc)

    def delete(self, handle: str) -> None:
        doc = self._load()
        if doc.pop(handle, None) is not None:
            self._save(doc)


def draft_parts(entry: dict) -> dict:
    """The fingerprint parts (draft_fingerprint.canonical) of a registry
    entry. A reply's quote is Mail's to add and is not part of it; the
    message it answers is."""
    att = []
    for path in entry.get("attachments") or []:
        try:
            data = Path(path).read_bytes()
            att.append({"name": Path(path).name, "content_type": "", "size": len(data),
                        "content": "sha256:" + hashlib.sha256(data).hexdigest()})
        except OSError:
            att.append({"name": Path(path).name, "content_type": "", "size": 0,
                        "content": "missing"})
    reply_to = (entry.get("reply_to") or {}).get("message_id", "")
    return {
        "to": [(r.get("name", ""), r.get("address", "")) for r in entry.get("to") or []],
        "cc": [(r.get("name", ""), r.get("address", "")) for r in entry.get("cc") or []],
        "from": [("", entry.get("sender", ""))],
        "raw_headers": {"reply_to": [reply_to] if reply_to else []},
        "subject": entry.get("subject", ""),
        "body": [["text", entry.get("body", "")]],
        "attachments": att,
    }


def _resident_euid() -> bool:
    """True in a process of the resident user, which never sends."""
    try:
        import pwd
        return os.geteuid() == pwd.getpwnam(RESIDENT_USER).pw_uid
    except KeyError:
        return False


def _default_approve(token, action, identifier, fingerprint=None) -> bool:
    """The Graph path's own library gate (graph_client._validate_library_token):
    the one-time button tokens, the in-process approvals and the
    scheduled-send credential, all bound to the action and the id."""
    try:
        import graph_client
    except Exception:
        return False
    return graph_client._validate_library_token(token, action, identifier, fingerprint)


def _draft_lock(message_id: str):
    try:
        import draft_lock
    except Exception:
        return nullcontext()
    return draft_lock.hold([message_id])


# ── mail ─────────────────────────────────────────────────────────────

class NativeMailClient:
    """Mail.app through the helper, in graph_client's shapes.

    `approve(token, action, identifier, fingerprint)` is the token check of
    a gated call (default: graph_client's); `is_resident()` says whether
    this process is the resident's (default: by euid); `lock(draft_id)` is
    the per-draft lock a send holds (default: draft_lock)."""

    def __init__(self, helper: NativeHelper, scope: NativeScope, drafts: DraftRegistry, *,
                 approve=None, is_resident=None, lock=None, now=None):
        self.helper = helper
        self.scope = scope
        self.drafts = drafts
        self._approve = approve or _default_approve
        self._is_resident = is_resident or _resident_euid
        self._lock = lock or _draft_lock
        self._now = now or (lambda: datetime.now(timezone.utc))

    # reads

    def _since(self, days) -> str | None:
        return None if days is None else _iso_utc(self._now() - timedelta(days=days))

    def list_folder_messages(self, folder: str = "inbox", top: int = 25,
                             order_by: str = "receivedDateTime", since: str | None = None):
        """Newest-first page of one folder across the accounts in scope.
        Returns (messages, has_more), as graph_client does."""
        out, more = [], False
        for acct, mbox, role in self.scope.boxes(folder):
            res = self.helper.call("list-messages", {
                "account": acct.name, "mailbox": mbox, "since": since, "top": top,
                "detail": "full", "preview": True})
            more = more or bool(res.get("has_more"))
            out += [graph_message(r, role=role) for r in res.get("messages") or []]
        out.sort(key=_sort_key, reverse=True)
        return out[:top], more or len(out) > top

    def get_messages(self, folder: str = "inbox", days_back: int | None = None,
                     unread_only: bool = False, full_body: bool = False) -> list:
        out = []
        for acct, mbox, role in self.scope.boxes(folder):
            res = self.helper.call("list-messages", {
                "account": acct.name, "mailbox": mbox, "since": self._since(days_back),
                "top": None, "detail": "full", "preview": full_body})
            out += [graph_message(r, role=role, full=full_body) for r in res.get("messages") or []]
        if unread_only:
            out = [m for m in out if not m["isRead"]]
        out.sort(key=_sort_key, reverse=True)
        return out

    def get_sent(self, days_back: int = 14, **kwargs) -> list:
        return self.get_messages("sentitems", days_back=days_back, **kwargs)

    def search_messages(self, query: str, top: int = 25, folder: str | None = None):
        """Subject and sender (and the body, when the scope allows) within
        the scope's search window. Returns (messages, has_more)."""
        boxes = self.scope.boxes(folder)
        res = self.helper.call("search-messages", {
            "mailboxes": [{"account": a.name, "mailbox": m} for a, m, _ in boxes],
            "query": str(query), "since": self._since(self.scope.search_days),
            "top": top, "body": self.scope.search_body, "preview": True})
        roles = {(a.name, m): r for a, m, r in boxes}
        msgs = [graph_message(r, role=roles.get((r.get("account"), r.get("mailbox")), ""))
                for r in res.get("messages") or []]
        msgs = _dedupe(msgs)
        msgs.sort(key=_sort_key, reverse=True)
        return msgs[:top], bool(res.get("has_more")) or len(msgs) > top

    def _lookup_boxes(self, roles=LOOKUP_ORDER) -> list[dict]:
        out = []
        for role in roles:
            for a in self.scope.accounts:
                if a.mailboxes.get(role):
                    out.append({"account": a.name, "mailbox": a.mailboxes[role], "role": role})
        return out

    def _find(self, message_handle_: str, *, full: bool = True) -> tuple[dict, str]:
        """(record, role) of a message by its handle, from the first mailbox
        in LOOKUP_ORDER that holds it."""
        try:
            mid = message_id_of(message_handle_)
        except ValueError:
            raise NativeError("bad-request", "not a native message id") from None
        boxes = self._lookup_boxes()
        res = self.helper.call("get-message", {
            "mailboxes": [{"account": b["account"], "mailbox": b["mailbox"]} for b in boxes],
            "message_id": mid, "full": full})
        rec = res.get("message")
        if not rec:
            raise NativeError("not-found", "no mailbox in the scope holds that message")
        role = next((b["role"] for b in boxes if b["account"] == rec.get("account")
                     and b["mailbox"] == rec.get("mailbox")), "")
        return rec, role

    def get_message_full(self, message_id: str) -> dict:
        if str(message_id).startswith("nd_"):
            return self._draft_message(message_id)
        rec, role = self._find(message_id)
        return graph_message(rec, role=role, full=True)

    def get_conversation_messages(self, conversation_id: str, top: int = 50,
                                  include_drafts: bool = False):
        """Every message of one thread, oldest first, from the inbox, sent,
        archive (and drafts) mailboxes in scope. Mail has no conversation
        index: the helper finds the messages whose headers name the thread's
        root Message-ID, and this keeps those whose own root is that id.
        Returns (messages, truncated)."""
        try:
            root = root_of(conversation_id)
        except ValueError:
            raise NativeError("bad-request", "not a native conversation id") from None
        roles = LOOKUP_ORDER if include_drafts else LOOKUP_ORDER[:3]
        boxes = self._lookup_boxes(roles)
        res = self.helper.call("find-thread", {
            "mailboxes": [{"account": b["account"], "mailbox": b["mailbox"]} for b in boxes],
            "root": root, "since": self._since(self.scope.search_days)})
        role_of = {(b["account"], b["mailbox"]): b["role"] for b in boxes}
        msgs = []
        for rec in res.get("messages") or []:
            if thread_root(rec) != root:
                continue
            msgs.append(graph_message(rec, role=role_of.get((rec.get("account"),
                                                             rec.get("mailbox")), ""), full=True))
        msgs = _dedupe(msgs)
        if not include_drafts:
            msgs = [m for m in msgs if not m["isDraft"]]
        msgs.sort(key=_sort_key)
        return msgs[:max(1, int(top))], len(msgs) > max(1, int(top))

    def conversation_latest_message(self, conversation_id: str) -> dict | None:
        msgs, _ = self.get_conversation_messages(conversation_id, top=100)
        if not msgs:
            return None
        m = msgs[-1]
        addr = lambda r: ((r or {}).get("emailAddress") or {}).get("address", "").lower()  # noqa: E731
        return {"from": addr(m.get("from")),
                "recipients": sorted({addr(r) for r in m["toRecipients"] + m["ccRecipients"]
                                      if addr(r)})}

    # drafts

    def _account_for(self, rec: dict | None) -> MailAccount:
        if rec:
            for a in self.scope.accounts:
                if a.name == rec.get("account"):
                    return a
        if not self.scope.accounts:
            raise NativeError("out-of-scope", "the scope names no Mail account")
        return self.scope.accounts[0]

    def _save_to_mail(self, entry: dict) -> str:
        acct = self.scope.account(entry["account"])
        res = self.helper.call("save-draft", {
            "account": acct.name, "drafts_mailbox": acct.mailboxes["drafts"],
            "sender": entry["sender"], "to": entry["to"], "cc": entry["cc"],
            "subject": entry["subject"], "body": entry["body"],
            "reply_to": entry.get("reply_to"), "reply_all": entry.get("reply_all", True),
            "attachments": entry.get("attachments") or []})
        mid = bare_message_id(res.get("message_id"))
        if not mid:
            raise NativeError("internal", "Mail saved the draft but the helper found no Message-ID")
        return mid

    def _remove_from_mail(self, entry: dict) -> None:
        acct = self.scope.account(entry["account"])
        if entry.get("mail_message_id"):
            self.helper.call("delete-draft", {
                "account": acct.name, "drafts_mailbox": acct.mailboxes["drafts"],
                "message_id": entry["mail_message_id"]})

    def _draft_message(self, handle: str) -> dict:
        entry = self.drafts.get(handle)
        if entry is None:
            raise NativeError("not-found", "no EA draft has that id")
        return {
            "id": handle, "isDraft": True, "subject": entry["subject"],
            "from": _addr("", entry["sender"]),
            "toRecipients": _recipients(entry["to"]), "ccRecipients": _recipients(entry["cc"]),
            "body": {"contentType": "Text", "content": entry["body"]},
            "conversationId": entry.get("conversation_id", ""),
            "internetMessageId": f"<{entry.get('mail_message_id', '')}>",
            "hasAttachments": bool(entry.get("attachments")),
            "parentFolderId": f"{entry['account']}/{self.scope.account(entry['account']).mailboxes['drafts']}",
        }

    def create_draft(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                     reply_to_id: str | None = None, content_type: str = "Text",
                     reply_all: bool = True, conversation_id: str | None = None) -> dict:
        """A new draft in Mail's Drafts mailbox of the account it belongs to
        (a reply: the account that holds the original). Returns the draft as
        graph_client does, with the EA's own handle as its id."""
        if content_type != "Text":
            raise NativeError("unsupported", "Mail scripting writes plain-text bodies only")
        if not str(body or "").strip():
            raise NativeError("bad-request", "a draft needs a body")
        orig, reply_to = None, None
        if reply_to_id:
            try:
                orig, role = self._find(reply_to_id, full=False)
            except NativeError as e:
                if e.code != "not-found" or not conversation_id:
                    raise
                latest, _ = self.get_conversation_messages(conversation_id)
                if not latest:
                    raise
                orig, role = self._find(latest[-1]["id"], full=False)
            reply_to = {"account": orig["account"], "mailbox": orig["mailbox"],
                        "message_id": bare_message_id(orig["message_id"])}
        acct = self._account_for(orig)
        own = {a.lower() for a in acct.addresses} | self.scope.addresses()
        if orig is not None and not to:
            _, sender = parseaddr(orig.get("sender") or "")
            people = [sender] + ([r.get("address", "") for r in (orig.get("to") or []) + (orig.get("cc") or [])]
                                 if reply_all else [])
            to = _unique([p for p in people if p and p.lower() not in own][:1])
            cc = cc if cc is not None else _unique(
                [p for p in people[1:] if p and p.lower() not in own and p not in to])
            subj = orig.get("subject") or ""
            subject = subj if re.match(r"(?i)^re:", subj) else f"Re: {subj}"
        if not to:
            raise NativeError("bad-request", "a draft needs a recipient")
        entry = {
            "account": acct.name, "sender": acct.addresses[0],
            "to": [{"name": "", "address": a} for a in _unique(to)],
            "cc": [{"name": "", "address": a} for a in _unique(cc or [])],
            "subject": subject or "", "body": body, "reply_to": reply_to,
            "reply_all": bool(reply_all), "attachments": [],
            "conversation_id": conversation_handle(thread_root(orig)) if orig else "",
            "created": _iso_utc(self._now()),
        }
        entry["mail_message_id"] = self._save_to_mail(entry)
        if not entry["conversation_id"]:
            entry["conversation_id"] = conversation_handle(entry["mail_message_id"])
        handle = new_draft_handle()
        self.drafts.put(handle, entry)
        return self._draft_message(handle)

    def create_reply(self, message_id: str, body: str, *, reply_all: bool = True,
                     conversation_id: str | None = None, to=None, cc=None) -> dict:
        return self.create_draft(to or [], "", body, cc=cc, reply_to_id=message_id,
                                 reply_all=reply_all, conversation_id=conversation_id)

    def _replace(self, handle: str, **changes) -> dict:
        entry = self.drafts.get(handle)
        if entry is None:
            raise NativeError("not-found", "no EA draft has that id")
        old = dict(entry)
        entry.update(changes)
        entry["mail_message_id"] = self._save_to_mail(entry)
        self._remove_from_mail(old)
        entry["updated"] = _iso_utc(self._now())
        self.drafts.put(handle, entry)
        return self._draft_message(handle)

    def update_draft(self, message_id: str, body: str, content_type: str = "Text") -> dict:
        """The draft's body replaced. Mail keeps no editable draft, so this
        saves a new Mail draft and deletes the old; the handle stays."""
        if content_type != "Text":
            raise NativeError("unsupported", "Mail scripting writes plain-text bodies only")
        if not str(body or "").strip():
            raise NativeError("bad-request", "a draft needs a body")
        return self._replace(message_id, body=body)

    def set_draft_recipients(self, message_id: str, to=None, cc=None) -> dict:
        changes = {}
        if to is not None:
            changes["to"] = [{"name": "", "address": a.strip()} for a in to if a.strip()]
        if cc is not None:
            changes["cc"] = [{"name": "", "address": a.strip()} for a in cc if a.strip()]
        return self._replace(message_id, **changes) if changes else {}

    def delete_draft(self, message_id: str) -> None:
        entry = self.drafts.get(message_id)
        if entry is None:
            raise NativeError("not-found", "no EA draft has that id")
        self._remove_from_mail(entry)
        self.drafts.delete(message_id)

    def draft_fingerprint(self, message_id: str) -> str:
        import draft_fingerprint
        entry = self.drafts.get(message_id)
        if entry is None:
            raise NativeError("not-found", "no EA draft has that id")
        return draft_fingerprint.fingerprint(draft_parts(entry))

    def _mail_copy(self, entry: dict) -> dict | None:
        acct = self.scope.account(entry["account"])
        try:
            res = self.helper.call("get-message", {
                "mailboxes": [{"account": acct.name, "mailbox": acct.mailboxes["drafts"]}],
                "message_id": entry["mail_message_id"], "full": True})
        except NativeError as e:
            if e.code == "not-found":
                return None
            raise
        return res.get("message")

    @staticmethod
    def _matches(entry: dict, rec: dict) -> bool:
        """Does Mail's copy of a draft still say what the registry says? The
        owner may edit the draft in Mail after the card went up. A reply's
        copy carries Mail's quote after the EA's text, so the text is a
        prefix there."""
        addrs = lambda rows: sorted(r.get("address", "").lower() for r in rows or [])  # noqa: E731
        return (addrs(rec.get("to")) == addrs(entry["to"])
                and addrs(rec.get("cc")) == addrs(entry["cc"])
                and (rec.get("subject") or "") == entry["subject"]
                and _norm_text(rec.get("content")).startswith(_norm_text(entry["body"])))

    def send_draft(self, message_id: str, confirm_token: str | None = None,
                   fingerprint: str | None = None) -> dict:
        """Send an EA draft: GATED. The checks are graph_client.send_draft's:
        a fingerprint, a valid one-time token bound to this draft and that
        fingerprint, the content unchanged since the approval, all under
        the draft's lock. Mail's copy is read again too; an edit made in
        Mail is a change, and the owner reviews it on a new card."""
        if not fingerprint:
            return {"status": "blocked",
                    "error": "Send requires the fingerprint of the content the owner approved."}
        if self._is_resident():
            return {"status": "blocked", "error": "The resident cannot send mail."}
        if not self._approve(confirm_token, "send", message_id, fingerprint):
            return {"status": "blocked",
                    "error": "Missing or invalid confirmation token. Send requires approval."}
        with self._lock(message_id):
            entry = self.drafts.get(message_id)
            if entry is None:
                return {"status": "error", "error": "No EA draft has that id."}
            current = self.draft_fingerprint(message_id)
            if current != fingerprint:
                return {"status": "changed", "fingerprint": current,
                        "error": "The draft changed after it was approved. Nothing was sent."}
            rec = self._mail_copy(entry)
            if rec is None:
                return {"status": "error",
                        "error": "The draft is no longer in Mail's Drafts. Nothing was sent."}
            if not self._matches(entry, rec):
                return {"status": "changed", "fingerprint": current,
                        "error": "The draft was edited in Mail after it was approved. Nothing was "
                                 "sent; the owner reviews the new content on a new card."}
            acct = self.scope.account(entry["account"])
            res = self.helper.call("send-message", {
                "account": acct.name, "sender": entry["sender"], "to": entry["to"],
                "cc": entry["cc"], "subject": entry["subject"], "body": entry["body"],
                "reply_to": entry.get("reply_to"), "reply_all": entry.get("reply_all", True),
                "attachments": entry.get("attachments") or []},
                approval=_Approval("send-message"))
            if not res.get("sent"):
                return {"status": "error", "error": "Mail did not accept the message."}
            try:
                self._remove_from_mail(entry)
            except NativeError:
                pass
            self.drafts.delete(message_id)
            return {"status": "sent", "warnings": []}

    # inbox state

    def archive_message(self, message_id: str) -> dict:
        """Move a message from an inbox to its account's archive mailbox."""
        rec, role = self._find(message_id, full=False)
        if role != "inbox":
            raise NativeError("bad-request", "only a message in an inbox is archived")
        acct = self.scope.account(rec["account"])
        self.helper.call("move-message", {
            "account": acct.name, "mailbox": rec["mailbox"],
            "message_id": bare_message_id(rec["message_id"]),
            "to_mailbox": acct.mailboxes["archive"]})
        return {"id": message_id, "parentFolderId": f"{acct.name}/{acct.mailboxes['archive']}"}


def _unique(items) -> list:
    seen, out = set(), []
    for i in items or []:
        k = str(i).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(i).strip())
    return out


def _dedupe(msgs: list) -> list:
    """One copy per message id; the copy whose role comes first in
    LOOKUP_ORDER wins (a Gmail message shows in INBOX and in All Mail)."""
    rank = {r: i for i, r in enumerate(LOOKUP_ORDER)}
    best: dict[str, dict] = {}
    for m in msgs:
        key = m["id"]
        cur = best.get(key)
        if cur is None or rank.get(m["mailbox"]["role"], 9) < rank.get(cur["mailbox"]["role"], 9):
            best[key] = m
    return list(best.values())


# ── calendar ─────────────────────────────────────────────────────────

def _local_date(value: str) -> str:
    return str(value or "")[:10]


def graph_event(ev: dict, scope: NativeScope) -> dict:
    """A helper event as the calendarView fields graph_client selects. A
    timed event's times are UTC with Graph's seven fractional digits; an
    all-day event's are midnights of its local dates, the end exclusive,
    as Graph gives them."""
    if ev.get("all_day"):
        start_day = _local_date(ev.get("start"))
        end_day = _local_date(ev.get("end"))
        try:
            end_excl = (datetime.fromisoformat(end_day) + timedelta(days=1)).date().isoformat()
        except ValueError:
            end_excl = end_day
        start = {"dateTime": f"{start_day}T00:00:00.0000000", "timeZone": "UTC"}
        end = {"dateTime": f"{end_excl}T00:00:00.0000000", "timeZone": "UTC"}
    else:
        start = {"dateTime": _graph_time(ev.get("start")).rstrip("Z") + ".0000000", "timeZone": "UTC"}
        end = {"dateTime": _graph_time(ev.get("end")).rstrip("Z") + ".0000000", "timeZone": "UTC"}
    org = ev.get("organizer") or {}
    attendees = []
    me = None
    for a in ev.get("attendees") or []:
        if a.get("is_current_user"):
            me = a
        if a.get("type") in ("room", "resource"):
            kind = "resource"
        else:
            kind = "optional" if a.get("role") == "optional" else "required"
        attendees.append({**_addr(a.get("name", ""), a.get("email", "")), "type": kind,
                          "status": {"response": RESPONSE.get(a.get("status", "unknown"), "none")}})
    is_organizer = bool(org.get("is_current_user")) or not org
    hold = HOLD_MARKER in str(ev.get("notes") or "")
    recurring = bool(ev.get("recurring"))
    cal = scope.calendars.get(ev.get("calendar_id"), {})
    return {
        "id": event_handle(ev.get("id", ""), ev.get("occurrence", "") if recurring else ""),
        "subject": ev.get("title") or "",
        "start": start, "end": end,
        "isAllDay": bool(ev.get("all_day")),
        "showAs": SHOW_AS.get(ev.get("availability", "busy"), "busy"),
        "attendees": attendees,
        "organizer": _addr(org.get("name", ""), org.get("email", "")) if org else _addr("", ""),
        "isOrganizer": is_organizer,
        "responseStatus": {"response": "organizer" if is_organizer and not me
                           else RESPONSE.get((me or {}).get("status", "unknown"), "none")},
        "location": {"displayName": ev.get("location") or ""},
        "categories": [EA_HOLD_CATEGORY] if hold else [],
        "type": "occurrence" if recurring else "singleInstance",
        "seriesMasterId": event_handle(ev.get("id", "")) if recurring else None,
        "isCancelled": ev.get("status") == "canceled",
        "bodyPreview": " ".join(str(ev.get("notes") or "").split())[:255],
        "source_calendar": cal.get("title") or ev.get("calendar_title", ""),
        "source_type": "macos",
        "calendar_id": ev.get("calendar_id", ""),
        "blocks_availability": cal.get("blocking", True),
    }


class NativeCalendarClient:
    """EventKit through the helper, in graph_client's shapes. EventKit
    reads attendees but cannot add them, and reads no one else's free/busy;
    those calls answer "unsupported" rather than doing less than asked."""

    def __init__(self, helper: NativeHelper, scope: NativeScope, *, approve=None, now=None):
        self.helper = helper
        self.scope = scope
        self._approve = approve or _default_approve
        self._now = now or (lambda: datetime.now(timezone.utc))

    def can_read(self, email: str) -> bool:
        """True for an address whose calendar is on this Mac: the owner's
        own, or a colleague's shared calendar the scope maps to it."""
        key = str(email or "").lower()
        return key in self.scope.addresses() or key in self.scope.shared

    def _events(self, start: datetime, end: datetime, calendars) -> list:
        if not calendars:
            return []
        res = self.helper.call("list-events", {"calendars": list(calendars),
                                               "start": _iso_utc(start), "end": _iso_utc(end)})
        return [e for e in res.get("events") or [] if e.get("calendar_id") in calendars]

    def get_calendar_view(self, start: datetime | None = None, end: datetime | None = None,
                          days_ahead: int = 14) -> list:
        start = start or self._now()
        end = end or start + timedelta(days=days_ahead)
        # A colleague's shared calendar answers free/busy for that colleague
        # only; it is never the owner's time.
        own = [c for c in self.scope.calendars if c not in self.scope.shared.values()]
        evs = [graph_event(e, self.scope) for e in self._events(start, end, own)]
        evs.sort(key=lambda e: e["start"]["dateTime"])
        return evs

    def get_event(self, event_id: str) -> dict:
        try:
            eid, occ = event_of(event_id)
        except ValueError:
            raise NativeError("bad-request", "not a native event id") from None
        ev = self.helper.call("get-event", {"id": eid, "occurrence": occ}).get("event") or {}
        if ev.get("calendar_id") not in self.scope.calendars:
            raise NativeError("out-of-scope", "that event is on a calendar outside the EA's scope")
        return graph_event(ev, self.scope)

    def _view(self, events: list, start: datetime, end: datetime, interval: int) -> str:
        slots = max(0, int((end - start).total_seconds() // (interval * 60)))
        view = ["0"] * slots
        for ev in events:
            g = graph_event(ev, self.scope)
            if g["isAllDay"] or g["isCancelled"]:
                continue
            ch = VIEW_CHAR.get(g["showAs"], "2")
            s = datetime.fromisoformat(g["start"]["dateTime"][:19]).replace(tzinfo=timezone.utc)
            t = datetime.fromisoformat(g["end"]["dateTime"][:19]).replace(tzinfo=timezone.utc)
            for i in range(slots):
                a = start + timedelta(minutes=interval * i)
                b = a + timedelta(minutes=interval)
                if a < t and b > s and ch > view[i]:
                    view[i] = ch
        return "".join(view)

    def get_schedule_detail(self, emails: list[str], start: datetime, end: datetime,
                            interval_minutes: int = 15) -> dict:
        """getSchedule's answer, {email: {"view", "error"}}. The owner's own
        addresses read the blocking calendars in scope; an address the scope
        maps to a calendar shared with the owner reads that calendar; any
        other address has no view, and the error says why."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        own = self.scope.addresses()
        blocking = [c for c, v in self.scope.calendars.items() if v.get("blocking", True)
                    and c not in self.scope.shared.values()]
        out = {}
        for email in emails:
            key = email.lower()
            if key in own:
                cals = blocking
            elif key in self.scope.shared:
                cals = [self.scope.shared[key]]
            else:
                out[email] = {"view": "", "error": (
                    "free/busy of other people is not available on the macos backend: "
                    "EventKit reads only the calendars on this Mac")}
                continue
            out[email] = {"view": self._view(self._events(start, end, cals), start, end,
                                             interval_minutes), "error": ""}
        return out

    def get_schedule(self, emails, start, end, interval_minutes: int = 15) -> dict:
        return {k: v["view"] for k, v in
                self.get_schedule_detail(emails, start, end, interval_minutes).items()}

    def _writable(self, calendar_id: str) -> str:
        cid = calendar_id or self.scope.default_calendar
        cal = self.scope.calendars.get(cid)
        if not cal:
            raise NativeError("out-of-scope", "that calendar is not in the EA's scope")
        if not cal.get("write"):
            raise NativeError("permission-denied", "the scope does not let the EA write that calendar")
        return cid

    def create_event(self, subject: str, start: datetime, end: datetime,
                     attendees: list[str] | None = None, body: str = "", location: str = "",
                     is_online: bool = False, confirm_token: str | None = None,
                     recurrence: dict | None = None, hold: bool = False,
                     optional_attendees: list[str] | None = None,
                     calendar_id: str | None = None) -> dict:
        """A calendar event: GATED, as on Graph, except an attendee-less
        event on the EA-Managed calendar (the mediator's carve-out). What
        EventKit cannot do is refused before any token is spent."""
        if attendees or optional_attendees:
            return {"status": "unsupported", "error": (
                "EventKit cannot add attendees, so the macos backend sends no invitations. "
                "Create the invitation in Calendar or Outlook.")}
        if recurrence:
            return {"status": "unsupported",
                    "error": "a recurring series is not built on the macos backend yet"}
        try:
            cid = self._writable(calendar_id or "")
        except NativeError as e:
            return {"status": "blocked", "error": str(e)}
        carve_out = bool(self.scope.ea_managed) and cid == self.scope.ea_managed
        if not carve_out and not self._approve(confirm_token, "event", subject):
            return {"status": "blocked", "error": (
                "Missing or invalid confirmation token. Calendar event creation requires approval.")}
        notes = body or ""
        if is_online:
            notes = _with_video_link(notes)
        if hold:
            notes = f"{HOLD_MARKER}\n{notes}".strip()
        ev = self.helper.call("create-event", {
            "calendar_id": cid, "title": subject, "start": _iso_utc(start), "end": _iso_utc(end),
            "all_day": False, "location": location, "notes": notes,
            "availability": "tentative" if hold else "busy"},
            approval=_Approval("create-event")).get("event") or {}
        return graph_event(ev, self.scope)

    def clear_hold(self, event_id: str) -> dict:
        """Delete one EA hold: tagged, shown as tentative, no attendees, the
        owner's own. Anything else is refused, as graph_client.clear_hold
        refuses it."""
        event = self.get_event(event_id)
        reason = ""
        if EA_HOLD_CATEGORY not in event["categories"]:
            reason = f"not tagged '{EA_HOLD_CATEGORY}'"
        elif event["attendees"]:
            reason = f"has {len(event['attendees'])} attendee(s)"
        elif not event["isOrganizer"]:
            reason = "the user is not the organiser"
        elif event["showAs"] not in ("tentative", "busy"):
            reason = f"shown as {event['showAs']}"
        if reason:
            return {"status": "refused", "error": f"not an EA hold: {reason}"}
        eid, occ = event_of(event_id)
        self.helper.call("delete-event", {"id": eid, "occurrence": occ, "span": "this"},
                         approval=_Approval("delete-event"))
        return {"status": "deleted", "id": event_id}


def _with_video_link(notes: str) -> str:
    try:
        import institution
        block = institution.video_join_block() if institution.video_link() else ""
    except Exception:
        block = ""
    return f"{notes}\n\n{block}\n".strip() if block else notes


# ── contacts ─────────────────────────────────────────────────────────

def resolve_contact(helper: NativeHelper, scope: NativeScope, name: str, top: int = 3) -> list:
    """{"name", "email"} rows for a name, from the Contacts framework (the
    native stand-in for graph_actions resolve-contact's People API)."""
    if not scope.contacts:
        raise NativeError("out-of-scope", "Contacts is not in the EA's scope")
    res = helper.call("find-contacts", {"name": name, "top": top})
    rows = []
    for c in res.get("contacts") or []:
        for email in c.get("emails") or []:
            rows.append({"name": c.get("name", ""), "email": email})
            break
    return rows[:top]


# ── the fake helper ──────────────────────────────────────────────────

class FakeNativeHelper:
    """An in-memory helper that speaks the protocol: a transport for the
    tests. It keeps Mail accounts with mailboxes of message records,
    calendars with events, contacts and the three permissions, and answers
    each request the way deploy/macos/tacitus-native does, refusals
    included. `calls` lists every verb it served."""

    def __init__(self):
        self.permissions = {"calendar": "authorized", "contacts": "authorized", "mail": "authorized"}
        self.mail_running = True
        self.accounts: dict[str, dict] = {}
        self.calendars: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.contacts: list[dict] = []
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self._n = 0

    # fixtures

    def add_account(self, name, addresses, mailboxes, kind="exchange"):
        self.accounts[name] = {"addresses": list(addresses), "kind": kind,
                               "mailboxes": {m: [] for m in mailboxes}}

    def add_message(self, account, mailbox, message_id, subject, sender, to=(), cc=(),
                    date="2026-10-01T09:00:00Z", content="", in_reply_to="", references="",
                    read=False, flagged=False):
        rec = {"account": account, "mailbox": mailbox, "local_id": self._next(),
               "message_id": bare_message_id(message_id), "subject": subject, "sender": sender,
               "to": [{"name": "", "address": a} for a in to],
               "cc": [{"name": "", "address": a} for a in cc],
               "date_received": date, "date_sent": date, "read": read, "flagged": flagged,
               "headers": {"in-reply-to": in_reply_to, "references": references},
               "content": content, "attachments": []}
        self.accounts[account]["mailboxes"][mailbox].append(rec)
        return rec

    def add_calendar(self, cid, title, writable=True, source_type="exchange"):
        self.calendars[cid] = {"id": cid, "title": title, "source": title,
                               "source_type": source_type, "writable": writable}

    def add_event(self, cid, title, start, end, **kw):
        eid = kw.pop("id", None) or f"EV-{self._next()}"
        ev = {"id": eid, "external_id": eid, "calendar_id": cid,
              "calendar_title": self.calendars[cid]["title"],
              "source_type": self.calendars[cid]["source_type"], "title": title,
              "start": start, "end": end, "all_day": False, "availability": "busy",
              "status": "confirmed", "location": "", "notes": "", "recurring": False,
              "detached": False, "attendees": [], "organizer": None}
        ev.update(kw)
        self.events[eid + "\x1f" + (ev.get("occurrence") or "")] = ev
        return ev

    def _next(self) -> int:
        self._n += 1
        return self._n

    # the protocol

    def __call__(self, req: dict) -> bytes:
        req = json.loads(encode_request(req))
        self.calls.append(req["verb"])
        resp = {"v": PROTOCOL_VERSION, "id": req["id"]}
        try:
            handler = getattr(self, "_v_" + req["verb"].replace("-", "_"), None)
            if handler is None:
                raise NativeError("unknown-verb", f"unknown verb {req['verb']}")
            resp.update(ok=True, result=handler(req.get("args") or {}))
        except NativeError as e:
            resp.update(ok=False, error={"code": e.code, "message": str(e).split(": ", 1)[-1]})
        return json.dumps(resp).encode("utf-8")

    def _need(self, what: str) -> None:
        state = self.permissions.get(what)
        if what == "mail" and not self.mail_running:
            raise NativeError("mail-not-running", "Mail is not running")
        if state == "not-determined":
            raise NativeError("not-determined", f"{what} access was never asked for")
        if state != "authorized":
            raise NativeError("permission-denied", f"{what} access is {state}")

    def _v_status(self, args):
        return {"version": "fake", "protocol": PROTOCOL_VERSION,
                "permissions": dict(self.permissions, mail=(
                    self.permissions["mail"] if self.mail_running else "mail-not-running")),
                "mail_running": self.mail_running}

    def _v_request_access(self, args):
        for what in ("calendar", "contacts", "mail"):
            if args.get(what) and self.permissions[what] == "not-determined":
                self.permissions[what] = "authorized"
        return {"permissions": dict(self.permissions)}

    def _v_list_calendars(self, args):
        self._need("calendar")
        return {"calendars": list(self.calendars.values())}

    def _v_list_events(self, args):
        self._need("calendar")
        cals = set(args.get("calendars") or [])
        if not cals:
            raise NativeError("bad-request", "list-events names its calendars")
        s, t = args["start"], args["end"]

        def utc(v):
            return _graph_time(v)
        return {"events": sorted((e for e in self.events.values() if e["calendar_id"] in cals
                                  and utc(e["start"]) < utc(t) and utc(e["end"]) > utc(s)),
                                 key=lambda e: utc(e["start"]))}

    def _v_get_event(self, args):
        self._need("calendar")
        ev = self.events.get(args.get("id", "") + "\x1f" + (args.get("occurrence") or ""))
        if ev is None:
            raise NativeError("not-found", "no such event")
        return {"event": ev}

    def _v_create_event(self, args):
        self._need("calendar")
        if args.get("attendees"):
            raise NativeError("unsupported", "EventKit cannot add attendees")
        cal = self.calendars.get(args.get("calendar_id"))
        if cal is None:
            raise NativeError("not-found", "no such calendar")
        if not cal["writable"]:
            raise NativeError("permission-denied", "the calendar does not allow changes")
        kw = {k: args[k] for k in ("location", "notes", "availability", "all_day") if k in args}
        return {"event": self.add_event(cal["id"], args.get("title", ""), args["start"],
                                        args["end"], **kw)}

    def _v_delete_event(self, args):
        self._need("calendar")
        key = args.get("id", "") + "\x1f" + (args.get("occurrence") or "")
        if self.events.pop(key, None) is None:
            raise NativeError("not-found", "no such event")
        return {"deleted": True}

    def _v_update_event(self, args):
        self._need("calendar")
        key = args.get("id", "") + "\x1f" + (args.get("occurrence") or "")
        ev = self.events.get(key)
        if ev is None:
            raise NativeError("not-found", "no such event")
        ev.update({k: args[k] for k in ("title", "start", "end", "location", "notes") if k in args})
        return {"event": ev}

    def _v_find_contacts(self, args):
        self._need("contacts")
        name = str(args.get("name") or "").lower()
        email = str(args.get("email") or "").lower()
        hits = [c for c in self.contacts if (name and name in c["name"].lower())
                or (email and email in [e.lower() for e in c["emails"]])]
        return {"contacts": hits[:int(args.get("top") or 10)]}

    # mail

    def _box(self, account, mailbox) -> list:
        self._need("mail")
        acct = self.accounts.get(account)
        if acct is None:
            raise NativeError("not-found", f"no Mail account named {account}")
        if mailbox not in acct["mailboxes"]:
            raise NativeError("not-found", f"no mailbox {mailbox} in {account}")
        return acct["mailboxes"][mailbox]

    @staticmethod
    def _newest(recs, top):
        recs = sorted(recs, key=lambda r: r.get("date_received") or "", reverse=True)
        if top:
            return recs[:top], len(recs) > top
        return recs, False

    def _v_mail_status(self, args):
        return {"running": self.mail_running,
                "accounts": [{"name": n, "enabled": True, "addresses": a["addresses"],
                              "kind": a["kind"]} for n, a in self.accounts.items()]}

    def _v_list_mailboxes(self, args):
        self._need("mail")
        acct = self.accounts.get(args.get("account"))
        if acct is None:
            raise NativeError("not-found", "no such account")
        return {"mailboxes": sorted(acct["mailboxes"])}

    def _v_list_messages(self, args):
        box = self._box(args.get("account"), args.get("mailbox"))
        since = _graph_time(args.get("since")) if args.get("since") else ""
        recs = [r for r in box if not since or _graph_time(r["date_received"]) >= since]
        msgs, more = self._newest(recs, args.get("top"))
        return {"messages": copy.deepcopy(msgs), "has_more": more}

    def _v_search_messages(self, args):
        q = str(args.get("query") or "").lower()
        since = _graph_time(args.get("since")) if args.get("since") else ""
        hits = []
        for b in args.get("mailboxes") or []:
            for r in self._box(b["account"], b["mailbox"]):
                text = [r["subject"], r["sender"]] + ([r["content"]] if args.get("body") else [])
                if any(q in str(t).lower() for t in text) and (
                        not since or _graph_time(r["date_received"]) >= since):
                    hits.append(r)
        msgs, more = self._newest(hits, args.get("top"))
        return {"messages": copy.deepcopy(msgs), "has_more": more}

    def _v_get_message(self, args):
        mid = bare_message_id(args.get("message_id"))
        for b in args.get("mailboxes") or []:
            for r in self._box(b["account"], b["mailbox"]):
                if r["message_id"] == mid:
                    return {"message": copy.deepcopy(r)}
        raise NativeError("not-found", "no mailbox named holds that message")

    def _v_find_thread(self, args):
        root = bare_message_id(args.get("root"))
        hits = []
        for b in args.get("mailboxes") or []:
            for r in self._box(b["account"], b["mailbox"]):
                headers = " ".join(str(v) for v in r["headers"].values())
                if r["message_id"] == root or root in headers:
                    hits.append(copy.deepcopy(r))
        return {"messages": hits}

    def _compose(self, args) -> dict:
        subject, content = args.get("subject", ""), args.get("body", "")
        refs, irt = "", ""
        if args.get("reply_to"):
            ref = args["reply_to"]
            orig = self._v_get_message({"mailboxes": [ref], "message_id": ref["message_id"]})["message"]
            irt = f"<{orig['message_id']}>"
            refs = " ".join(x for x in (orig["headers"].get("references", ""), irt) if x)
            content = f"{content}\n\nOn {orig['date_received']}, {orig['sender']} wrote:\n> " + \
                "\n> ".join(str(orig["content"]).splitlines())
        return {"subject": subject, "content": content, "in_reply_to": irt, "references": refs}

    def _v_save_draft(self, args):
        self._box(args["account"], args["drafts_mailbox"])
        c = self._compose(args)
        mid = f"draft-{self._next()}@fake.invalid"
        self.add_message(args["account"], args["drafts_mailbox"], mid, c["subject"],
                         args.get("sender", ""), [r["address"] for r in args.get("to") or []],
                         [r["address"] for r in args.get("cc") or []], content=c["content"],
                         in_reply_to=c["in_reply_to"], references=c["references"])
        return {"message_id": mid}

    def _v_delete_draft(self, args):
        box = self._box(args["account"], args["drafts_mailbox"])
        mid = bare_message_id(args.get("message_id"))
        keep = [r for r in box if r["message_id"] != mid]
        n = len(box) - len(keep)
        box[:] = keep
        return {"deleted": n}

    def _v_send_message(self, args):
        self._need("mail")
        c = self._compose(args)
        mid = f"sent-{self._next()}@fake.invalid"
        acct = self.accounts[args["account"]]
        sent_box = next((m for m in acct["mailboxes"] if "sent" in m.lower()), None)
        rec = {"account": args["account"], "message_id": mid, **c,
               "to": args.get("to"), "cc": args.get("cc")}
        self.sent.append(rec)
        if sent_box:
            self.add_message(args["account"], sent_box, mid, c["subject"], args.get("sender", ""),
                             [r["address"] for r in args.get("to") or []],
                             [r["address"] for r in args.get("cc") or []], content=c["content"],
                             in_reply_to=c["in_reply_to"], references=c["references"])
        return {"sent": True}

    def _v_move_message(self, args):
        src = self._box(args["account"], args["mailbox"])
        dst = self._box(args["account"], args["to_mailbox"])
        mid = bare_message_id(args.get("message_id"))
        for r in list(src):
            if r["message_id"] == mid:
                src.remove(r)
                r["mailbox"] = args["to_mailbox"]
                dst.append(r)
                return {"moved": True}
        raise NativeError("not-found", "no such message")

    def _v_set_flags(self, args):
        for r in self._box(args["account"], args["mailbox"]):
            if r["message_id"] == bare_message_id(args.get("message_id")):
                for k in ("read", "flagged"):
                    if k in args:
                        r[k] = bool(args[k])
                return {"ok": True}
        raise NativeError("not-found", "no such message")


# ── the doctor ───────────────────────────────────────────────────────

_PERMISSION_FIX = {
    "calendar": "System Settings > Privacy & Security > Calendars: turn on tacitus-native",
    "contacts": "System Settings > Privacy & Security > Contacts: turn on tacitus-native",
    "mail": "System Settings > Privacy & Security > Automation > tacitus-native: turn on Mail",
}


def doctor_rows(*, config_path=None, scope: NativeScope | None = None, scope_file=None,
                helper: NativeHelper | None = None, platform: str | None = None,
                owa_token=None) -> list[dict]:
    """The doctor's rows for this backend: {"status": PASS|WARN|FAIL,
    "name", "detail", "fix"}. With no backend at "macos" it is one PASS
    row. Every probe goes through the helper agent, so the answer is the
    agent's own permissions, not this shell's."""
    rows = []

    def row(status, name, detail="", fix=""):
        rows.append({"status": status, "name": name, "detail": detail, "fix": fix})

    platform = platform or sys.platform
    if scope is None and scope_file is not None:
        try:
            scope = load_scope(scope_file)
        except FileNotFoundError:
            scope = None
        except (ValueError, OSError) as e:
            row("FAIL", "native accounts: scope", f"unreadable: {e}",
                "docs/macos-native-accounts.md 'The scope file'")
            return rows
    chosen = backends(config_path, scope=scope or NativeScope())
    native = [k for k in ("mail", "calendar") if chosen.get(k) == "macos"]
    if not native:
        row("PASS", "native accounts (macOS)", "not used "
            f"(mail {chosen['mail']}, calendar {chosen['calendar']})")
        return rows
    provider = chosen.get("availability", "graph")
    row("PASS" if provider != "none" else "WARN", "native accounts: availability provider",
        {"graph": "graph (Graph getSchedule on a registration's token)",
         "owa": "owa (Graph getSchedule on the outlook-web-auth token)",
         "none": "none: no colleague's calendar is read; invitees pick"}[provider],
        "" if provider != "none" else
        "set availability.provider to owa (docs/macos-native-accounts.md "
        "'Other people's free/busy')")
    if provider == "owa":
        if owa_token is None:
            import availability
            owa_token = availability._owa_token
        has = bool(owa_token())
        row("PASS" if has else "WARN", "native accounts: outlook-web-auth token",
            "a token now" if has else "no token now: colleagues' free/busy is not read",
            "" if has else "SETUP.md 'Option C: no registration (outlook-web-auth)'")
    if platform != "darwin":
        row("FAIL", "native accounts (macOS)", f"{', '.join(native)} backend set on {platform}",
            "the macos backend runs only on a Mac; set mail.backend / calendar.backend back")
        return rows
    if scope is None:
        row("FAIL", "native accounts: scope", f"{SCOPE_FILE} missing in the private directory",
            "docs/macos-native-accounts.md 'The scope file'")
        return rows
    helper = helper or NativeHelper(QueueTransport(Path(scope_file or scope_path()).parent / QUEUE_DIR,
                                                   timeout=10))
    try:
        status = helper.call("status")
    except NativeError as e:
        row("FAIL", "native accounts: helper", str(e),
            "sudo bash deploy/macos/tacitus-native/build.sh --install, then log in as the owner")
        return rows
    row("PASS", "native accounts: helper", f"version {status.get('version', '?')}")
    perms = status.get("permissions") or {}
    wanted = (["calendar"] if "calendar" in native else []) + (["mail"] if "mail" in native else []) \
        + (["contacts"] if scope.contacts else [])
    for what in wanted:
        state = perms.get(what, "unknown")
        name = f"native accounts: {what} permission"
        if state == "authorized":
            row("PASS", name, state)
        elif state in ("not-determined", "mail-not-running"):
            row("WARN", name, state, "open Mail, then python3 preprocessor/native_accounts.py "
                "request-access and answer the prompt" if what == "mail" else
                "python3 preprocessor/native_accounts.py request-access, then answer the prompt")
        else:
            row("FAIL", name, state, _PERMISSION_FIX[what])
    if "mail" in native and perms.get("mail") == "authorized":
        try:
            have = {a["name"]: a for a in helper.call("mail-status").get("accounts") or []}
            for acct in scope.accounts:
                if acct.name not in have:
                    row("FAIL", f"native accounts: Mail account {acct.name}", "not in Mail",
                        "System Settings > Internet Accounts: turn on Mail for the account")
                    continue
                boxes = set(helper.call("list-mailboxes", {"account": acct.name}).get("mailboxes") or [])
                missing = sorted({m for m in acct.mailboxes.values() if m not in boxes}
                                 - {acct.mailboxes.get("deleteditems")})
                row("FAIL" if missing else "PASS", f"native accounts: Mail account {acct.name}",
                    f"missing mailboxes: {', '.join(missing)}" if missing else "mailboxes found",
                    "name the account's mailboxes in the scope file" if missing else "")
        except NativeError as e:
            row("WARN", "native accounts: Mail accounts", str(e), "open Mail and let it sync")
    if "calendar" in native and perms.get("calendar") == "authorized":
        try:
            have = {c["id"] for c in helper.call("list-calendars").get("calendars") or []}
            gone = sorted(set(scope.calendars) - have)
            row("FAIL" if gone else "PASS", "native accounts: calendars",
                f"{len(gone)} calendar(s) in the scope not on this Mac" if gone
                else f"{len(scope.calendars)} in scope",
                "python3 preprocessor/native_accounts.py calendars, then fix the scope file"
                if gone else "")
        except NativeError as e:
            row("WARN", "native accounts: calendars", str(e))
    return rows


# ── the clients an owner process uses ───────────────────────────────

def helper_for_services(timeout: float = DEFAULT_TIMEOUT_S) -> NativeHelper:
    """The helper agent behind the owner-private queue."""
    return NativeHelper(QueueTransport(scope_path().parent / QUEUE_DIR, timeout=timeout))


def mail_client() -> NativeMailClient:
    """The Mail client graph_actions uses on a macos mail backend."""
    from owner_trust import private_dir
    return NativeMailClient(helper_for_services(), load_scope(),
                            DraftRegistry(private_dir() / DRAFTS_FILE))


def calendar_client() -> NativeCalendarClient:
    """The calendar client graph_actions and the solver use on a macos
    calendar backend."""
    return NativeCalendarClient(helper_for_services(), load_scope())


# ── owner CLI ────────────────────────────────────────────────────────

def _cli_helper(timeout: float = 60.0) -> NativeHelper:
    return helper_for_services(timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="macOS native accounts (owner, on the Mac)",
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", allow_abbrev=False)
    sub.add_parser("request-access", allow_abbrev=False)
    sub.add_parser("calendars", allow_abbrev=False)
    sub.add_parser("accounts", allow_abbrev=False)
    sub.add_parser("doctor", allow_abbrev=False)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "doctor":
            out = doctor_rows(scope_file=scope_path())
        elif a.cmd == "request-access":
            # The prompts come from the agent, so they name tacitus-native,
            # the binary that keeps the grants.
            out = _cli_helper(timeout=300).call("request-access",
                                                {"calendar": True, "contacts": True, "mail": True})
        elif a.cmd == "accounts":
            h = _cli_helper()
            out = h.call("mail-status")
            for acct in out.get("accounts") or []:
                acct["mailboxes"] = h.call("list-mailboxes", {"account": acct["name"]}).get("mailboxes")
        else:
            out = _cli_helper().call({"status": "status", "calendars": "list-calendars"}[a.cmd])
    except NativeError as e:
        print(json.dumps({"error": e.code, "message": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
