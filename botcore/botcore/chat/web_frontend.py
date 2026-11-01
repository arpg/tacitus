"""The Tacitus web chat: a ChatFrontend the bot process serves itself.

docs/webchat.md is the contract. The bot serves a small web app and a
JSON API over HTTP on loopback; the owner reaches it through a proxy on
their tailnet (`tailscale serve`, which adds the identity header) or by
pairing a browser with a token. Nothing here talks to another server.

The store is one SQLite file (`webchat.sqlite`): channels, messages, the
event log the browser streams, uploaded files and a read mark per
channel. Uploads sit beside it under `webchat-files/`. Every write to a
message or a channel appends an event in the same transaction and hands
it to each open event stream, so a browser that reconnects with
`Last-Event-ID` replays exactly what it missed.

Several processes write one store (docs/webchat.md, CLI senders). The
bot serves it. A command-line sender in an owner process (chat_notify,
chat_upload, a cron lane's post) opens it writer-only: `start()` opens
the store and serves nothing, and its mirrors write their rows and
events into the same tables under the primary's ids. SQLite takes one
writer at a time, so the event seqs commit in order. The serving store
hands its own events to the streams at once; a tail thread reads the
log every `tail_s` seconds (one `SELECT MAX(seq)` when nothing is new)
and hands on the events another process appended, in seq order and as
the same JSON. The store keeps the last seq it handed on, so no event
reaches a stream twice. The same thread posts the scheduled posts a
sender queued (`scheduled_posts`, chat.direct.schedule_direct on the web
chat alone) once they are due. A process that is not the owner's never
opens the owner-private store: its posts reach the primary only
(chat.load_frontend).

Identity, standalone: the owner is "owner", the bot is "bot"; messages
are `w<seq>`, a thread is `t<seq of its first message>`, rooms are
`r<n>`, files `f<n>`; the EA channel is `main` and the DM is `dm`. In
shadow mode (docs/webchat.md, Shadow mode) the composite writes through
the `mirror_*` methods with the primary's ids and the store mints none;
the owner's web input goes to `self.inbound` instead of the registry.
The history the composite copies in when it starts goes in quietly
(`quiet=True`): a history row has no event of its own, never counts as
unread, and never goes before a newer row of its thread.

Outbound text is the LLM's Slack mrkdwn, converted the way the Zulip
frontend converts it (chat.text.mrkdwn_to_zulip), so the web app renders
one Markdown subset. A card is stored as `dataclasses.asdict` JSON and
the web app draws it; a click carries only the action id, and the value
the core receives is the one stored with the card.

The HTTP layer is `http.server.ThreadingHTTPServer` on a daemon thread,
with a cap on open connections and a 30-second socket timeout. Static
files come from `webui/` beside this module without auth; every `/api/`
route needs the owner (the Tailscale login header on a connection from
tailscaled itself, or the pairing cookie). Before that, an `/api/`
request must name this server in `Host` (public_url's host and port, or
the loopback port), must not come from another site (`Sec-Fetch-Site`),
and a POST must carry the exact `Origin` of public_url (a loopback
client without the identity header may use the loopback origin). The
app is one per origin: a page from another port of the same host (a
deck) is another origin and is refused. Standard library only, except
Web Push (web_push.py), which needs the `cryptography` package and is
off without it: a new card, a bot message in the DM and a loud alert
(`notify`) reach the browsers that turned notifications on, even with
the app closed.

The bot serves only a chat block it can trust (`trusted_config`, set by
chat.load_frontend when the block came from the owner-private chat.json)
and only from a store directory and pairing token file that this user
owns, that no other user can read, and that lie outside the resident's
tree (`untrusted_roots`). Otherwise start() refuses: standalone it
raises; under a ShadowFrontend it logs why and the bot runs on its
primary alone.

A `relay` block in the web block starts the relay client
(relay_client.start_relay, docs/webchat-relay.md) once the server runs:
a phone off the tailnet reaches this server through a public relay that
sees only ciphertext. The owner manages it from their own web chat (the
tailnet, or this machine) under `/api/relay/` (pair, approve, revoke): a
phone pairs on the bot's own origin, or on the relay's copy of the app
with the signed bundle's hash in the link; a relayed request
(`X-Tacitus-Relay`) never reaches those routes or `/api/pair`, and
`/api/me` tells the app whether its requests may confirm a card
(`X-Tacitus-Relay-Cards`). A new device is announced in the DM channel;
removing one drops its push subscriptions.

Settings screens: the host registers a page in `settings_pages` (a name
and an object with `get()` and `save(body)`; the EA's routing table is
`routing`, model_routing.WebSettings). `/api/me` lists the names, and
`/api/settings/<name>` answers the owner: GET is the page's data, POST
hands the JSON body to `save`, whose ValueError is a 400 with its text. A
`save` that takes `who` also gets the credential that signed the request
in (`cookie` or `header`) and the device a relayed request names, for its
change log.
A POST must pass the same Host, Sec-Fetch-Site and Origin checks as
every other, and a relayed one (`X-Tacitus-Relay`) is refused: settings
change only in the owner's own web chat.

At start the bot signs the app's files (web_bundle, docs/webchat.md, Code
integrity): every file under `webui/` and its sha256, signed with an
Ed25519 key that lives in the relay key file (a key for this process
alone when no relay runs). It serves the signed manifest at
`<base>/manifest.sig.json` without auth and at `/api/bundle` (the route the
relay reaches); the app's service worker serves a file only when it
matches. A phone whose copy of the app checks posts the bundle hash to
`/api/relay/attest` through the relay, which records the check on the
device and grants nothing: an approved relay-origin device reads and
chats either way, and only the owner's approval with `cards` lets it
press card buttons. The bot logs a warning when a
file on disk no longer matches what it signed; `refresh_bundle()` signs
again.
"""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import http.server
import inspect
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import select
import socket
import sqlite3
import stat
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
from typing import Any, Optional

from . import files as _files
from . import relay_crypto, web_bundle, web_push
from .base import (
    AlreadyArchived,
    ChatError,
    ChatFrontend,
    Limits,
    NotArchived,
    RoomNameTaken,
)
from .cards import ConfirmationCard
from .text import mrkdwn_to_zulip
from .types import (
    THREAD_KEY_RE,
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
)

log = logging.getLogger("chat.web")

OWNER = "owner"
BOT = "bot"
MAIN_ID, MAIN_NAME = "main", "ea"
DM_ID, DM_NAME = "dm", "dm"

STORE_FILE = "webchat.sqlite"
FILES_DIR = "webchat-files"
PAIR_FILE = "webchat-pair.token"
# The primary ids of the owner's web messages (shadow mode), which the
# primary holds as the bot's: the history copy and chat_read take them as
# the owner's from this list, never from the text.
OWNER_IDS_FILE = "webchat-owner-ids.json"
OWNER_IDS_MAX = 5000
RELAY_KEY_FILE = "webchat-relay.json"
COOKIE = "ea_web"
WEBUI_DIR = Path(__file__).resolve().parent / "webui"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8470
DEFAULT_BASE = "/chat"

# Seconds between the comment lines that keep an idle event stream open.
PING_S = 20.0
# How often an idle event stream checks that its client is still there: a
# client that left frees its connection slot within this, not at the ping.
GONE_CHECK_S = 1.0
# Seconds between the serving process's reads of the event log for the
# events another process (a command-line sender) appended.
TAIL_S = 1.0
# Events kept for replay; a browser away longer reloads its view.
EVENT_KEEP_S = 14 * 86400
# Events a stream may fall behind by before it is closed; the browser
# reconnects with Last-Event-ID and replays from the store.
STREAM_QUEUE = 2000
MAX_JSON_BYTES = 1 << 20
MAX_UPLOAD_BYTES = 50 << 20
MAX_TEXT_CHARS = 20000
COOKIE_MAX_AGE = 365 * 86400
# The pairing route reads at most this many bytes.
MAX_PAIR_BYTES = 1024
# An error answer reads at most this much of an unread body first.
MAX_DRAIN_BYTES = 64 << 10
# A pairing token file holding a shorter token is refused.
MIN_PAIR_TOKEN = 16
# Seconds a request waits for the pairing lock before a 429.
PAIR_WAIT_S = 10.0
# Open connections the server takes at once; more are closed at once.
MAX_CONNECTIONS = 64
# Open connections one peer address other than loopback may hold at once,
# under MAX_CONNECTIONS; more from it are closed at once, so one client
# cannot take every slot. The web block's `peer_connections` changes it;
# 0 lifts it, for a proxy (every request then comes from the proxy's
# address) that limits each client itself.
MAX_PEER_CONNECTIONS = 8
# Socket reads and writes; a stalled client is dropped.
HANDLER_TIMEOUT_S = 30
# Seconds between the serving process's checks of the app's files against
# the manifest it signed (a changed file is logged once).
BUNDLE_CHECK_S = 60.0

KINDS = ("main", "sched", "dm", "room")
EVENT_TYPES = ("message", "update", "reaction", "card", "status", "file", "channel")
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# Host names a loopback client (the relay client, a test) puts in Host and
# Origin, with this server's port.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
_CHUNK_SIZE_RE = re.compile(rb"[0-9A-Fa-f]{1,8}")
_LENGTH_RE = re.compile(r"[0-9]{1,12}")
# Emoji shortcodes, held aside while text converts (WebFrontend._out).
_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")
_HELD_RE = re.compile("\x02(\\d+)\x02")

# The web app's pages load only their own files: nothing inline, nothing
# from another origin.
HTML_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'")
# A stored file opened directly runs nothing.
FILE_CSP = "sandbox; default-src 'none'"
# A relay's origin as the page's connect-src names it: a scheme and a host,
# nothing a policy could read as another directive.
_ORIGIN_RE = re.compile(r"^https?://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:\d{1,5})?$")
# A relay URL's path as connect-src may carry it: plain segments only.
_RELAY_PATH_RE = re.compile(r"^(/(?!\.+(?:/|$))[A-Za-z0-9._~-]+)*$")
_DEVICE_RE = re.compile(r"^[0-9a-f]{16}$")
_BUNDLE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

PLACEHOLDER_PAGE = (b"<!doctype html><html><head><meta charset=\"utf-8\">"
                    b"<meta name=\"viewport\" content=\"width=device-width\">"
                    b"<title>Tacitus</title></head><body><p>The Tacitus web app is not "
                    b"installed in this tree (botcore/botcore/chat/webui/). The API answers "
                    b"under api/.</p></body></html>")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
    id TEXT PRIMARY KEY, name TEXT, kind TEXT, archived INT DEFAULT 0,
    description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS messages(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, id TEXT, thread_key TEXT,
    author TEXT, kind TEXT, text TEXT, card TEXT, file TEXT, reactions TEXT,
    posted_at REAL, updated_at REAL, history INT DEFAULT 0, UNIQUE(channel, id));
CREATE INDEX IF NOT EXISTS messages_thread ON messages(channel, thread_key, seq);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, type TEXT, channel TEXT, id TEXT,
    payload TEXT);
CREATE TABLE IF NOT EXISTS files(
    id TEXT PRIMARY KEY, channel TEXT, name TEXT, path TEXT, mimetype TEXT, size INT,
    at REAL);
CREATE TABLE IF NOT EXISTS reads(channel TEXT PRIMARY KEY, seq INT);
CREATE TABLE IF NOT EXISTS push_subscriptions(
    endpoint TEXT PRIMARY KEY, keys TEXT, created_at REAL, last_ok REAL,
    failures INT DEFAULT 0, last_seen REAL, device TEXT);
CREATE TABLE IF NOT EXISTS push_queue(
    tag TEXT PRIMARY KEY, due_at REAL, payload TEXT, urgency TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS scheduled_posts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, text TEXT, due_at REAL,
    created_at REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _status_name(status: Status | str) -> str:
    return status.value if isinstance(status, Status) else str(status)


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _norm_base(base: Any) -> str:
    """'/chat' for 'chat', '/chat/' or '/chat'; '' for the root."""
    b = "/" + str(base if base is not None else DEFAULT_BASE).strip().strip("/")
    return "" if b == "/" else b


def safe_name(name: str, default: str = "file") -> str:
    """A display name for a file: its last path part, control characters
    dropped, at most 200 characters."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return base[:200] or default


def _disk_name(name: str) -> str:
    """A file name safe on any file system, for a download's target."""
    n = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").lstrip(".")
    return n[:120] or "file"


def _write_private(path: Path, data: bytes) -> None:
    """Write data at path, mode 0600, through a temp file and a rename."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# who is on the other end of a loopback connection (Linux)

PROC_NET = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
TAILSCALED_SOCKETS = ("/run/tailscale/tailscaled.sock", "/var/run/tailscale/tailscaled.sock",
                      "/var/snap/tailscale/common/socket/tailscaled.sock")
# Seconds between two lookups of tailscaled's uid after a mismatch.
TAILSCALED_RECHECK_S = 5.0
_TCP_TIME_WAIT, _TCP_LISTEN = "06", "0A"
_ts_lock = threading.Lock()
_ts_cache: dict[str, Any] = {"uid": None, "at": 0.0}


def _proc_hex(ip: str) -> list[tuple[int, str]]:
    """(index into PROC_NET, the address as /proc/net/tcp* prints it) for
    an IP: each 32-bit word in host byte order, as uppercase hex. An
    IPv4-mapped IPv6 address is looked up in both tables."""
    try:
        if ":" not in ip:
            packed = socket.inet_aton(ip)
            return [(0, "%08X" % struct.unpack("=I", packed)[0])]
        packed = socket.inet_pton(socket.AF_INET6, ip.split("%", 1)[0])
    except (OSError, ValueError):
        return []
    words = "".join("%08X" % struct.unpack("=I", packed[i:i + 4])[0] for i in range(0, 16, 4))
    out = [(1, words)]
    if packed[:12] == b"\x00" * 10 + b"\xff\xff":
        out.append((0, "%08X" % struct.unpack("=I", packed[12:])[0]))
    return out


def peer_uid(peer: tuple, local: tuple) -> int | None:
    """The uid owning the client socket of a TCP connection, from
    /proc/net/tcp and /proc/net/tcp6: the entry whose local end is `peer`
    and whose remote end is `local` (this server's end). None off Linux,
    when no live entry matches, or when the matches disagree."""
    try:
        pip, pport = str(peer[0]), int(peer[1])
        lip, lport = str(local[0]), int(local[1])
    except (IndexError, TypeError, ValueError):
        return None
    uids: set[int] = set()
    lhex = dict(_proc_hex(lip))
    for table, phex in _proc_hex(pip):
        want_local = f"{phex}:{pport:04X}"
        rhex = lhex.get(table)
        if rhex is None:
            continue
        want_remote = f"{rhex}:{lport:04X}"
        try:
            text = PROC_NET[table].read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[1].upper() != want_local or f[2].upper() != want_remote:
                continue
            if f[3] in (_TCP_TIME_WAIT, _TCP_LISTEN) or f[9] == "0":
                continue
            try:
                uids.add(int(f[7]))
            except ValueError:
                continue
    return uids.pop() if len(uids) == 1 else None


def _lookup_tailscaled_uid() -> int | None:
    """tailscaled's uid: the effective uid of the `tailscaled` processes in
    /proc, else the owner of its control socket. When the processes run as
    several users, the socket's owner decides among them."""
    procs: set[int] = set()
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        entries = []
    for pid in entries:
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() != "tailscaled":
                continue
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("Uid:"):
                    procs.add(int(line.split()[2]))
                    break
        except (OSError, ValueError, IndexError):
            continue
    sock = None
    for path in TAILSCALED_SOCKETS:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode):
            sock = st.st_uid
            break
    if len(procs) == 1:
        return procs.pop()
    if sock is not None and (not procs or sock in procs):
        return sock
    return None


def tailscaled_uid(refresh: bool = False) -> int | None:
    """tailscaled's uid, looked up once and kept; `refresh` looks again
    (at most once in TAILSCALED_RECHECK_S), for a mismatch that a restarted
    daemon may explain. None off Linux or when it cannot be found."""
    now = time.monotonic()
    with _ts_lock:
        have = _ts_cache["at"] > 0
        if have and not refresh:
            return _ts_cache["uid"]
        if have and now - _ts_cache["at"] < TAILSCALED_RECHECK_S:
            return _ts_cache["uid"]
        _ts_cache["uid"] = _lookup_tailscaled_uid()
        _ts_cache["at"] = now
        return _ts_cache["uid"]


# the owner checks on the store and the pairing token

def private_problem(path: Path, kind: str) -> str | None:
    """None when `path` is a `kind` ("dir" or "file") that this euid owns,
    not a symlink, with no permission for group or others (a file with one
    link); else what is wrong with it."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    want = "0700" if kind == "dir" else "0600"
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symlink"
    if not (stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode)):
        return f"{path} is not a {'directory' if kind == 'dir' else 'regular file'}"
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if euid is not None and st.st_uid != euid:
        return f"{path} is owned by uid {st.st_uid}, not by this process's uid {euid}"
    if st.st_mode & 0o077:
        return f"{path} has mode {oct(st.st_mode & 0o777)}; it must be {want}"
    if kind == "file" and st.st_nlink != 1:
        return f"{path} has {st.st_nlink} links"
    return None


def _guarded_dir_problem(path: Path) -> str | None:
    """None when directory `path` is this euid's and no other user can
    write it (so no one else can swap a file in it)."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return f"{path} is not a directory"
    if euid is not None and st.st_uid != euid:
        return f"{path} is owned by uid {st.st_uid}, not by this process's uid {euid}"
    if st.st_mode & 0o022:
        return f"{path} is writable by group or others (mode {oct(st.st_mode & 0o777)})"
    return None


def _inside(path: Path, roots: tuple[Path, ...]) -> Path | None:
    """The first of `roots` that `path` resolves into, else None."""
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    for root in roots:
        try:
            if real == Path(os.path.realpath(root)) or real.is_relative_to(
                    Path(os.path.realpath(root))):
                return Path(root)
        except (OSError, ValueError):
            continue
    return None


def _hostport(value: str) -> tuple[str, int | None] | None:
    """(host lowercased, port or None) of a Host header's value; None when
    it is not a bare host[:port]."""
    value = (value or "").strip()
    if not value or any(c in value for c in "/?#@\\ \t"):
        return None
    try:
        parts = urllib.parse.urlsplit("//" + value)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    return (host, port) if host else None


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an origin or URL, the port defaulted by the
    scheme; None for anything that is not http or https."""
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or parts.username is not None:
        return None
    return scheme, host, port if port is not None else (443 if scheme == "https" else 80)


def parse_multipart(content_type: str, body: bytes) -> dict[str, dict]:
    """The parts of a multipart/form-data body by field name:
    {name: {"filename": str | None, "type": str, "data": bytes}}.
    Raises ValueError for a body that is not multipart."""
    ct = Message()
    ct["content-type"] = content_type or ""
    if ct.get_content_maintype() != "multipart":
        raise ValueError("not a multipart body")
    boundary = ct.get_param("boundary")
    if not boundary or not isinstance(boundary, str):
        raise ValueError("no multipart boundary")
    delim = b"\r\n--" + boundary.encode("latin-1")
    out: dict[str, dict] = {}
    for chunk in (b"\r\n" + body).split(delim)[1:]:
        if chunk.startswith(b"--"):
            break
        eol = chunk.find(b"\r\n")
        if eol < 0:
            continue
        head, sep, data = chunk[eol + 2:].partition(b"\r\n\r\n")
        if not sep:
            continue
        hdr = Message()
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, val = line.partition(":")
            if key.strip():
                hdr[key.strip()] = val.strip()
        name = hdr.get_param("name", header="content-disposition")
        if not name:
            continue
        name = collapse_rfc2231_value(name)
        out[str(name)] = {"filename": hdr.get_filename(),
                          "type": hdr.get_content_type() if hdr.get("content-type") else "",
                          "data": data}
    return out


class _Sub:
    """One event stream's queue; `dropped` once it fell too far behind."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=STREAM_QUEUE)
        self.dropped = False


class WebStore:
    """The SQLite store and the event fan-out.

    One connection, shared by every thread under one lock. A write to a
    message or a channel and its event go in one transaction; the event
    then goes to every subscriber's queue while the lock is held, so each
    queue sees events in seq order. Other processes may write the same
    file: `deliver_new` hands their events on, and `_delivered` (the last
    seq handed on) keeps any event from going out twice."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.files_dir = self.path.parent / FILES_DIR
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        if fresh:
            os.chmod(self.path, 0o600)
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
            if "history" not in cols:
                # A store from before the history copy: its rows are live ones.
                self._conn.execute("ALTER TABLE messages ADD COLUMN history INT DEFAULT 0")
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(push_subscriptions)")}
            if "last_seen" not in cols:
                # A store from before: a subscription counts as posted when made.
                self._conn.execute("ALTER TABLE push_subscriptions ADD COLUMN last_seen REAL")
            if "device" not in cols:
                # A store from before: no subscription names a relay device.
                self._conn.execute("ALTER TABLE push_subscriptions ADD COLUMN device TEXT")
            self._conn.commit()
        self._subs: list[_Sub] = []
        self._pending: list[tuple[int, str, str]] = []
        self._emitted = 0
        # The last event seq handed to the streams, or passed over while
        # none was open: a stream that opens later replays from the log.
        self._delivered = self.max_event_seq()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # reads

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def ok(self) -> bool:
        try:
            return self._one("SELECT 1") is not None
        except sqlite3.Error:
            return False

    def channel(self, cid: str) -> dict | None:
        row = self._one("SELECT * FROM channels WHERE id=?", (cid,))
        return self._channel_json(row) if row else None

    def _channel_json(self, row: sqlite3.Row) -> dict:
        cid = row["id"]
        last = self._one("SELECT MAX(posted_at) AS t FROM messages WHERE channel=?", (cid,))
        # A history row (copied from the primary's past) counts neither as
        # unread nor as the owner's last message.
        unread = self._one(
            "SELECT COUNT(*) AS n FROM messages WHERE channel=? AND author=? AND history=0 "
            "AND seq > MAX(COALESCE((SELECT seq FROM reads WHERE channel=?), 0),"
            "COALESCE((SELECT MAX(seq) FROM messages WHERE channel=? AND author=? "
            "AND history=0), 0))",
            (cid, BOT, cid, cid, OWNER))
        return {"id": cid, "name": row["name"] or "", "kind": row["kind"] or "room",
                "archived": bool(row["archived"]), "description": row["description"] or "",
                "last_at": last["t"] if last else None, "unread": int(unread["n"]) if unread else 0}

    def channels(self) -> list[dict]:
        rows = self._all("SELECT * FROM channels ORDER BY CASE kind WHEN 'dm' THEN 0 "
                         "WHEN 'main' THEN 1 WHEN 'sched' THEN 2 ELSE 3 END, name, id")
        return [self._channel_json(r) for r in rows]

    def _row(self, channel: str, mid: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM messages WHERE channel=? AND id=?", (channel, mid))

    def message(self, channel: str, mid: str) -> dict | None:
        row = self._row(channel, mid)
        return self._msg_json(row) if row else None

    def _msg_json(self, row: sqlite3.Row, reply_count: int | None = None) -> dict:
        if reply_count is None:
            t = self._one("SELECT MIN(seq) AS first, COUNT(*) AS n FROM messages "
                          "WHERE channel=? AND thread_key=?", (row["channel"], row["thread_key"]))
            reply_count = int(t["n"]) - 1 if t and t["first"] == row["seq"] else 0
        return {"seq": row["seq"], "channel": row["channel"], "id": row["id"],
                "thread_key": row["thread_key"], "author": row["author"], "kind": row["kind"],
                "text": row["text"] or "", "card": _loads(row["card"]),
                "file": _loads(row["file"]), "reactions": _loads(row["reactions"]) or [],
                "posted_at": row["posted_at"], "updated_at": row["updated_at"],
                "reply_count": max(0, reply_count)}

    def thread_of(self, channel: str, key: str | None) -> str | None:
        """The thread key `key` names in the channel: the key itself when a
        message carries it, the thread of the message whose id it is, else
        None."""
        if not key:
            return None
        if self._one("SELECT 1 FROM messages WHERE channel=? AND thread_key=? LIMIT 1",
                     (channel, key)):
            return key
        row = self._row(channel, key)
        return row["thread_key"] if row else None

    def thread_channel(self, key: str | None) -> str | None:
        """The one channel that holds thread `key` (or the message whose id
        it is); None when no channel or more than one does. A standalone
        key (`t<seq>`, `w<seq>`) is unique across the store."""
        if not key:
            return None
        for col in ("thread_key", "id"):
            rows = self._all(f"SELECT DISTINCT channel FROM messages WHERE {col}=? LIMIT 2",
                             (key,))
            if rows:
                return rows[0]["channel"] if len(rows) == 1 else None
        return None

    def thread(self, channel: str, key: str, limit: int = 1000) -> list[dict]:
        """The thread's messages, oldest first: its first message, then the
        replies."""
        rows = self._all("SELECT * FROM messages WHERE channel=? AND thread_key=? "
                         "ORDER BY seq LIMIT ?", (channel, key, int(limit)))
        return [self._msg_json(r) if i == 0 else self._msg_json(r, 0)
                for i, r in enumerate(rows)]

    def threads(self, channel: str, limit: int = 50, before: int | None = None) -> list[dict]:
        """Each thread's first message, newest first, with reply_count and
        last_at (its newest post); `before` is a first message's seq, and a
        page holds the threads that opened before it."""
        rows = self._all(
            "SELECT m.*, t.n AS n, t.last_at AS last_at FROM messages m "
            "JOIN (SELECT MIN(seq) AS first, COUNT(*) AS n, MAX(posted_at) AS last_at "
            "FROM messages WHERE channel=? GROUP BY thread_key) t "
            "ON m.seq = t.first WHERE (? IS NULL OR m.seq < ?) "
            "ORDER BY m.seq DESC LIMIT ?", (channel, before, before, int(limit)))
        out = []
        for r in rows:
            j = self._msg_json(r, int(r["n"]) - 1)
            j["last_at"] = r["last_at"]
            out.append(j)
        return out

    def parents(self, channel: str, since_s: float, after_seq: int, limit: int) -> list[dict]:
        """Thread-opening messages posted after since_s with seq > after_seq,
        oldest first."""
        rows = self._all(
            "SELECT m.*, t.n AS n FROM messages m JOIN (SELECT MIN(seq) AS first, COUNT(*) AS n "
            "FROM messages WHERE channel=? GROUP BY thread_key) t ON m.seq = t.first "
            "WHERE m.posted_at > ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
            (channel, float(since_s), int(after_seq), int(limit)))
        return [self._msg_json(r, int(r["n"]) - 1) for r in rows]

    def file(self, fid: str) -> dict | None:
        row = self._one("SELECT * FROM files WHERE id=?", (fid,))
        return dict(row) if row else None

    def next_number(self, table: str, prefix: str) -> int:
        """1 + the largest n among `<prefix><n>` ids in the channels or the
        files table."""
        if table not in ("channels", "files"):
            raise ValueError(table)
        rows = self._all(f"SELECT id FROM {table} WHERE id LIKE ?", (prefix + "%",))
        best = 0
        for r in rows:
            tail = str(r["id"])[len(prefix):]
            if tail.isdigit():
                best = max(best, int(tail))
        return best + 1

    def max_event_seq(self) -> int:
        row = self._one("SELECT MAX(seq) AS s FROM events")
        return int(row["s"] or 0) if row else 0

    def events_since(self, seq: int, limit: int = 500,
                     upto: int | None = None) -> list[tuple[int, str, str]]:
        """(seq, type, payload) of the logged events after `seq`, through
        `upto` when given, oldest first."""
        rows = self._all("SELECT seq, type, payload FROM events WHERE seq > ? AND "
                         "(? IS NULL OR seq <= ?) ORDER BY seq LIMIT ?",
                         (int(seq), upto, upto, int(limit)))
        return [(int(r["seq"]), r["type"], r["payload"]) for r in rows]

    # writes

    def _emit(self, typ: str, channel: str, mid: str, payload: dict) -> int:
        """Append an event inside the caller's transaction and queue it for
        every stream once the caller commits (the lock is held throughout)."""
        text = json.dumps(payload, separators=(",", ":"))
        cur = self._conn.execute("INSERT INTO events(at, type, channel, id, payload) "
                                 "VALUES (?,?,?,?,?)", (time.time(), typ, channel, mid, text))
        seq = int(cur.lastrowid)
        self._pending.append((seq, typ, text))
        return seq

    def _transaction(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            self._pending = []
            try:
                with self._conn:
                    result = fn()
            except BaseException:
                self._pending = []
                raise
            items, self._pending = self._pending, []
            self._deliver(items)
            self._emitted += len(items)
            if self._emitted >= 1000:
                self._emitted = 0
                self.prune_events()
            return result

    def _deliver(self, items: list[tuple[int, str, str]]) -> None:
        """Hand this process's committed events to the streams at once.
        When another process logged events after the last one handed on,
        those go first, read back from the log, so each stream sees the
        seqs in order. Caller holds the lock."""
        if not items:
            return
        if self._subs and items[0][0] > self._delivered + 1:
            self._catch_up(items[-1][0])
        else:
            for item in items:
                self._fan_out(item)
        self._delivered = max(self._delivered, items[-1][0])

    def _catch_up(self, upto: int | None = None) -> int:
        """Hand on the logged events after the last one handed on, through
        `upto` (all of them when None), oldest first; how many. Caller
        holds the lock."""
        n = 0
        while True:
            batch = self.events_since(self._delivered, 500, upto)
            for item in batch:
                self._fan_out(item)
                self._delivered = item[0]
            n += len(batch)
            if len(batch) < 500:
                return n

    def deliver_new(self) -> int:
        """Hand the streams the events another process appended since the
        last one handed on; how many. One `SELECT MAX(seq)` when there is
        nothing new; with no stream open the new events are passed over,
        since a stream that opens later replays from the log."""
        with self._lock:
            top = self.max_event_seq()
            if top <= self._delivered:
                return 0
            if not self._subs:
                self._delivered = top
                return 0
            return self._catch_up(top)

    def _fan_out(self, item: tuple[int, str, str]) -> None:
        for sub in list(self._subs):
            try:
                sub.q.put_nowait(item)
            except queue.Full:
                sub.dropped = True
                self._subs.remove(sub)

    def subscribe(self) -> _Sub:
        sub = _Sub()
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: _Sub) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def close_subscribers(self) -> None:
        with self._lock:
            for sub in self._subs:
                sub.dropped = True
                try:
                    sub.q.put_nowait(None)
                except queue.Full:
                    pass
            self._subs = []

    def prune_events(self, keep_s: float = EVENT_KEEP_S) -> int:
        with self._lock:
            with self._conn:
                cur = self._conn.execute("DELETE FROM events WHERE at < ?",
                                         (time.time() - keep_s,))
            return cur.rowcount

    def upsert_channel(self, cid: str, name: str, kind: str, archived: bool = False,
                       description: str | None = None) -> dict:
        """Write a channel row; a `channel` event when it changed."""
        kind = kind if kind in KINDS else "room"

        def run() -> dict:
            row = self._conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            desc = description if description is not None else (row["description"] if row else "")
            new = (name or (row["name"] if row else "") or cid, kind, int(bool(archived)), desc or "")
            if row is None:
                self._conn.execute("INSERT INTO channels(id, name, kind, archived, description) "
                                   "VALUES (?,?,?,?,?)", (cid, *new))
            elif (row["name"], row["kind"], row["archived"], row["description"]) != new:
                self._conn.execute("UPDATE channels SET name=?, kind=?, archived=?, description=? "
                                   "WHERE id=?", (*new, cid))
            else:
                return self._channel_json(row)
            out = self._channel_json(self._conn.execute(
                "SELECT * FROM channels WHERE id=?", (cid,)).fetchone())
            self._emit("channel", cid, cid, out)
            return out
        return self._transaction(run)

    def set_channel(self, cid: str, **fields: Any) -> dict | None:
        """Change some of a channel's fields; None when there is no such
        channel."""
        row = self.channel(cid)
        if row is None:
            return None
        return self.upsert_channel(cid, fields.get("name", row["name"]),
                                   fields.get("kind", row["kind"]),
                                   fields.get("archived", row["archived"]),
                                   fields.get("description", row["description"]))

    def add_message(self, channel: str, *, mid: str | None, thread_key: str | None,
                    author: str, kind: str, text: str = "", card: dict | None = None,
                    file: dict | None = None, posted_at: float | None = None,
                    event: str | None = None, history: bool = False) -> dict:
        """Insert a message and its event. With no mid the store mints
        `w<seq>`, and with no thread_key the message opens `t<seq>`. Its
        updated_at starts at posted_at; every change raises it (the app
        reads a row whose two times differ as an edit). A `history` row (a
        message from the primary's past) gets no event and never counts as
        unread."""
        now = time.time()

        def run() -> dict:
            cur = self._conn.execute(
                "INSERT INTO messages(channel, id, thread_key, author, kind, text, card, file, "
                "reactions, posted_at, updated_at, history) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (channel, mid, thread_key, author, kind, text or "",
                 json.dumps(card) if card is not None else None,
                 json.dumps(file) if file is not None else None, "[]",
                 posted_at if posted_at is not None else now,
                 posted_at if posted_at is not None else now, int(bool(history))))
            seq = int(cur.lastrowid)
            if mid is None or thread_key is None:
                self._conn.execute("UPDATE messages SET id=?, thread_key=? WHERE seq=?",
                                   (mid if mid is not None else f"w{seq}",
                                    thread_key if thread_key is not None else f"t{seq}", seq))
            row = self._conn.execute("SELECT * FROM messages WHERE seq=?", (seq,)).fetchone()
            out = self._msg_json(row)
            if not history:
                self._emit(event or ("message" if kind == "message" else kind), channel,
                           row["id"], out)
            return out
        return self._transaction(run)

    def newest_in_thread(self, channel: str, thread_key: str) -> float | None:
        """The posted_at of the thread's newest message; None for a thread
        with none."""
        row = self._one("SELECT MAX(posted_at) AS t FROM messages WHERE channel=? AND "
                        "thread_key=?", (channel, thread_key))
        return float(row["t"]) if row and row["t"] is not None else None

    def touch_channel(self, cid: str) -> dict | None:
        """Append a `channel` event carrying the channel's row as it is now
        (its last_at and unread), so an open app reads it again; None when
        there is no such channel."""
        def run() -> dict | None:
            row = self._conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            if row is None:
                return None
            out = self._channel_json(row)
            self._emit("channel", cid, cid, out)
            return out
        return self._transaction(run)

    def move_thread(self, old_channel: str, old_key: str, new_channel: str,
                    new_key: str) -> int:
        """Re-key every message of a thread to its new channel and key, with
        an `update` event each; a message the new channel holds already is
        dropped from the old one. The number of messages moved."""
        def run() -> int:
            rows = self._conn.execute("SELECT * FROM messages WHERE channel=? AND thread_key=? "
                                      "ORDER BY seq", (old_channel, old_key)).fetchall()
            moved = 0
            for row in rows:
                if new_channel != old_channel and self._conn.execute(
                        "SELECT 1 FROM messages WHERE channel=? AND id=?",
                        (new_channel, row["id"])).fetchone():
                    self._conn.execute("DELETE FROM messages WHERE seq=?", (row["seq"],))
                    continue
                self._conn.execute("UPDATE messages SET channel=?, thread_key=? WHERE seq=?",
                                   (new_channel, new_key, row["seq"]))
                out = self._msg_json(self._conn.execute("SELECT * FROM messages WHERE seq=?",
                                                        (row["seq"],)).fetchone())
                self._emit("update", new_channel, row["id"], out)
                moved += 1
            return moved
        return self._transaction(run)

    def change_message(self, channel: str, mid: str, event: str,
                       **fields: Any) -> dict | None:
        """Change a message's fields (text, kind, card, file, reactions) and
        append `event`; None when there is no such message."""
        bad = set(fields) - {"text", "kind", "card", "file", "reactions"}
        if bad:
            raise ValueError(f"not a message field: {sorted(bad)}")

        def run() -> dict | None:
            row = self._conn.execute("SELECT * FROM messages WHERE channel=? AND id=?",
                                     (channel, mid)).fetchone()
            if row is None:
                return None
            sets, args = ["updated_at=?"], [time.time()]
            for key, val in fields.items():
                if key in ("card", "file", "reactions"):
                    val = json.dumps(val) if val is not None else None
                sets.append(f"{key}=?")
                args.append(val)
            self._conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE seq=?",
                               (*args, row["seq"]))
            out = self._msg_json(self._conn.execute("SELECT * FROM messages WHERE seq=?",
                                                    (row["seq"],)).fetchone())
            self._emit(event, channel, mid, out)
            return out
        return self._transaction(run)

    def toggle_reaction(self, channel: str, mid: str, name: str, on: bool) -> dict | None:
        """Add or remove a reaction; the message JSON when it changed, None
        when the message is missing or already in that state."""
        def run() -> dict | None:
            row = self._conn.execute("SELECT * FROM messages WHERE channel=? AND id=?",
                                     (channel, mid)).fetchone()
            if row is None:
                return None
            have = _loads(row["reactions"]) or []
            if on == (name in have):
                return None
            have = [*have, name] if on else [r for r in have if r != name]
            self._conn.execute("UPDATE messages SET reactions=? WHERE seq=?",
                               (json.dumps(have), row["seq"]))
            out = self._msg_json(self._conn.execute("SELECT * FROM messages WHERE seq=?",
                                                    (row["seq"],)).fetchone())
            self._emit("reaction", channel, mid, out)
            return out
        return self._transaction(run)

    def add_file(self, fid: str | None, channel: str, name: str, data: bytes,
                 mimetype: str) -> dict:
        """Keep a file's bytes under the files dir and record them; the
        row. With no fid the store mints the next `f<n>`; the row goes in
        before the bytes take its name, and a minted id another process
        took first is minted again, so two uploads never share an id."""
        self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.files_dir / f"incoming-{secrets.token_hex(12)}"
        _write_private(tmp, data)
        mint = fid is None
        verb = "INSERT" if mint else "INSERT OR REPLACE"
        try:
            with self._lock:
                while True:
                    if mint:
                        fid = f"f{self.next_number('files', 'f')}"
                    disk = self.files_dir / hashlib.sha256(fid.encode("utf-8")).hexdigest()[:32]
                    try:
                        with self._conn:
                            self._conn.execute(
                                f"{verb} INTO files(id, channel, name, path, mimetype, size, "
                                "at) VALUES (?,?,?,?,?,?,?)",
                                (fid, channel, name, str(disk), mimetype, len(data), time.time()))
                    except sqlite3.IntegrityError:
                        if mint:
                            continue
                        raise
                    break
                os.replace(tmp, disk)
                return self.file(fid) or {}
        finally:
            if tmp.exists():
                tmp.unlink()

    def mark_read(self, channel: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO reads(channel, seq) VALUES (?, (SELECT COALESCE(MAX(seq), 0) "
                    "FROM messages WHERE channel=?)) ON CONFLICT(channel) DO UPDATE SET "
                    "seq=excluded.seq", (channel, channel))

    # web push: the browsers' subscriptions and the pushes queued for later

    def push_subscribe(self, endpoint: str, keys: dict,
                       keep: int = web_push.MAX_SUBSCRIPTIONS, device: str | None = None) -> None:
        """Keep a subscription (a renewed one starts with no failures, and
        `last_seen` is now); past `keep`, the ones that have gone longest
        without a delivery go. `device` is the relay device it belongs to
        (removing that device drops it); a renewal without one keeps the
        one recorded."""
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO push_subscriptions(endpoint, keys, created_at, last_ok, failures, "
                    "last_seen, device) VALUES (?,?,?,NULL,0,?,?) ON CONFLICT(endpoint) DO UPDATE "
                    "SET keys=excluded.keys, failures=0, last_seen=excluded.last_seen, "
                    "device=COALESCE(excluded.device, push_subscriptions.device)",
                    (endpoint, json.dumps(keys), now, now, device))
                self._conn.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint NOT IN (SELECT endpoint FROM "
                    "push_subscriptions ORDER BY MAX(COALESCE(last_ok, 0), created_at) DESC "
                    "LIMIT ?)", (int(keep),))

    def push_unsubscribe(self, endpoint: str) -> bool:
        with self._lock:
            with self._conn:
                cur = self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?",
                                         (endpoint,))
            return cur.rowcount > 0

    def push_subscriptions(self) -> list[dict]:
        rows = self._all("SELECT * FROM push_subscriptions ORDER BY created_at")
        return [{**dict(r), "keys": _loads(r["keys"]) or {}} for r in rows]

    @staticmethod
    def push_id(endpoint: str) -> str:
        """A subscription's id in the app's list: a hash of its endpoint, so
        the list never shows the endpoint's secret path."""
        return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]

    def push_list(self) -> list[dict]:
        """The subscriptions as the app lists them: id, the push service's
        host, when made, last posted by the app and last delivered."""
        out = []
        for r in self._all("SELECT endpoint, created_at, last_ok, failures, last_seen "
                           "FROM push_subscriptions ORDER BY created_at"):
            out.append({"id": self.push_id(r["endpoint"]),
                        "host": urllib.parse.urlsplit(r["endpoint"]).hostname or "",
                        "created_at": r["created_at"],
                        "last_seen": r["last_seen"] or r["created_at"],
                        "last_ok": r["last_ok"], "failures": int(r["failures"] or 0)})
        return out

    def push_revoke(self, sub_id: str) -> bool:
        """Drop the subscription whose id (push_id) is `sub_id`."""
        with self._lock:
            for r in self._conn.execute("SELECT endpoint FROM push_subscriptions").fetchall():
                if hmac.compare_digest(self.push_id(r["endpoint"]), str(sub_id)):
                    return self.push_unsubscribe(r["endpoint"])
        return False

    def push_check_token(self, pair_hash: str) -> int:
        """Drop every subscription when the pairing token's hash differs
        from the one recorded; record it. The number dropped. An empty hash
        (no token readable here) changes nothing, and the first hash a
        store sees is recorded with its subscriptions kept."""
        if not pair_hash:
            return 0
        with self._lock:
            with self._conn:
                row = self._conn.execute("SELECT value FROM meta WHERE key='pair_hash'").fetchone()
                if row is not None and hmac.compare_digest(str(row["value"]), pair_hash):
                    return 0
                dropped = 0
                if row is not None:
                    dropped = self._conn.execute("DELETE FROM push_subscriptions").rowcount
                self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                                   "('pair_hash', ?)", (pair_hash,))
        if dropped:
            log.warning("web push: the pairing token changed; %d subscription(s) dropped",
                        dropped)
        return dropped

    def push_drop_devices(self, keep: set[str]) -> int:
        """Drop each subscription that names a relay device not in `keep`
        (a device removed); the number dropped."""
        with self._lock:
            with self._conn:
                rows = self._conn.execute("SELECT endpoint, device FROM push_subscriptions "
                                          "WHERE device IS NOT NULL").fetchall()
                gone = [r["endpoint"] for r in rows if r["device"] not in keep]
                for endpoint in gone:
                    self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?",
                                       (endpoint,))
        if gone:
            log.info("web push: %d subscription(s) of a removed relay device dropped", len(gone))
        return len(gone)

    def push_drop_stale(self, before: float) -> int:
        """Drop each subscription the app has not posted since `before`."""
        with self._lock:
            with self._conn:
                n = self._conn.execute(
                    "DELETE FROM push_subscriptions WHERE COALESCE(last_seen, created_at) < ?",
                    (float(before),)).rowcount
        if n:
            log.info("web push: %d subscription(s) not posted by the app for 30 days dropped", n)
        return n

    def push_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM push_subscriptions")
        return int(row["n"]) if row else 0

    def push_result(self, endpoint: str, ok: bool) -> None:
        """Record a delivery (last_ok, failures back to 0) or a failure."""
        with self._lock:
            with self._conn:
                if ok:
                    self._conn.execute("UPDATE push_subscriptions SET last_ok=?, failures=0 "
                                       "WHERE endpoint=?", (time.time(), endpoint))
                else:
                    self._conn.execute("UPDATE push_subscriptions SET failures=failures+1 "
                                       "WHERE endpoint=?", (endpoint,))

    def push_defer(self, tag: str, due_at: float, payload: str, urgency: str) -> None:
        """Queue a push for `due_at`, replacing one queued under the same tag."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO push_queue(tag, due_at, payload, urgency, created_at) "
                    "VALUES (?,?,?,?,?)", (tag, float(due_at), payload, urgency, time.time()))

    def push_cancel(self, tag: str) -> bool:
        with self._lock:
            with self._conn:
                cur = self._conn.execute("DELETE FROM push_queue WHERE tag=?", (tag,))
            return cur.rowcount > 0

    def push_queued(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM push_queue ORDER BY due_at")]

    def push_claim_due(self, now: float) -> list[dict]:
        """Take the queued pushes due by `now` off the queue, in one
        transaction, so two processes never both send one."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = [dict(r) for r in self._conn.execute(
                    "SELECT * FROM push_queue WHERE due_at <= ? ORDER BY due_at", (now,))]
                self._conn.execute("DELETE FROM push_queue WHERE due_at <= ?", (now,))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return rows

    # scheduled posts (chat.direct.schedule_direct on the web chat alone)

    def post_defer(self, channel: str, text: str, due_at: float) -> int:
        """Queue the bot's post of `text` in `channel` for `due_at`; its id."""
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO scheduled_posts(channel, text, due_at, created_at) "
                    "VALUES (?,?,?,?)", (channel, text, float(due_at), time.time()))
            return int(cur.lastrowid)

    def post_cancel(self, post_id: int) -> bool:
        """Drop a scheduled post; False when it is gone (posted or dropped)."""
        with self._lock:
            with self._conn:
                cur = self._conn.execute("DELETE FROM scheduled_posts WHERE id=?",
                                         (int(post_id),))
            return cur.rowcount > 0

    def posts_queued(self) -> list[dict]:
        return [dict(r) for r in
                self._all("SELECT * FROM scheduled_posts ORDER BY due_at, id")]

    def post_claim_due(self, now: float) -> list[dict]:
        """Take the scheduled posts due by `now` off the queue in one
        transaction, so two processes never both post one. A plain read
        comes first: the bot asks every tail_s seconds."""
        if self._one("SELECT 1 FROM scheduled_posts WHERE due_at <= ? LIMIT 1",
                     (now,)) is None:
            return []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = [dict(r) for r in self._conn.execute(
                    "SELECT * FROM scheduled_posts WHERE due_at <= ? ORDER BY due_at, id",
                    (now,))]
                self._conn.execute("DELETE FROM scheduled_posts WHERE due_at <= ?", (now,))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return rows


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


Inbound = Callable[[Any], Optional[MessageRef]]


class WebFrontend(ChatFrontend):
    """The bot's own web chat: the store, the HTTP API and the event stream."""

    name = "web"
    capabilities = frozenset({"buttons", "select", "files", "private_rooms", "reactions"})
    limits = Limits(20000, 20000, 200, 0)

    # The LLM writes the Markdown the web app renders; the frontend also
    # converts the core's Slack-style marks (*bold*, <url|text>) on the way
    # out, so both reach the owner as formatted text. "platform" reads as an
    # adjective in the prompts ("a real-time {platform} message").
    PROMPT_STRINGS = {
        "platform": "Tacitus web chat",
        "format_rule_main": ("Format in Markdown: **bold**, _italic_, [text](url) links, "
                             "`code`, - lists, :emoji_shortcodes: (not unicode emoji)."),
        "format_rule_reply": ("Your reply is posted to a thread in the Tacitus web chat. "
                              "Write Markdown: **bold**, _italic_, [text](url) links."),
        "confirm_location": "the Send button is on the card in your DM thread",
        "confirm_sentence": ("The bot posts a :lock: confirmation card with Send and Cancel "
                             "buttons in your DM, with a one-line pointer in this thread; say "
                             "\"the Send button is on the card in your DM thread\", never "
                             "\"above\"."),
        "history_note": "",
        "room_word": "room",
        "continuation_header": "Continuing the web chat thread:",
        "output_only": "Your text output is posted to the web chat automatically.",
        "output_only_resume": ("Output ONLY the reply text (Markdown: **bold**, _italic_, "
                               ":emoji_shortcodes:)."),
        "format_rule_closing": (
            "Formatting: Markdown, **bold** (double asterisks), _italic_ (underscore), "
            "[text](url) links.\n"
            "Use emoji shortcodes (:white_check_mark:, :email:) not unicode emoji."),
        "confirm_sentence_wrapped": (
            "buttons themselves are posted on a card in your DM, so say \"the\n"
            "Send button is on the card in your DM thread\", never \"above\"."),
    }

    def __init__(self, cfg: dict | None = None, *, store_path: str | Path,
                 webui_dir: str | Path | None = None, writer_only: bool = False,
                 trusted_config: bool = False,
                 untrusted_roots: tuple[str | Path, ...] = ()) -> None:
        super().__init__()
        # A command-line sender's copy: start() opens the store and serves
        # nothing; the bot's tail thread hands its events to the streams.
        self.writer_only = bool(writer_only)
        # True only when `cfg` came from the owner-private chat block: the
        # server serves nothing otherwise (start()).
        self.trusted_config = bool(trusted_config)
        # Trees the store and the pairing token must lie outside (the
        # resident's), checked by start().
        self.untrusted_roots = tuple(Path(r) for r in untrusted_roots if r)
        # Why start() refused to serve; None while it serves or before.
        self.refused: str | None = None
        cfg = dict(cfg or {})
        self.cfg = cfg
        self.bind = str(cfg.get("bind") or DEFAULT_BIND)
        port = cfg.get("port")
        self.port = int(port) if port not in (None, "") else DEFAULT_PORT
        self.base_path = _norm_base(cfg.get("base_path"))
        self.public_url = str(cfg.get("public_url") or "").rstrip("/")
        self.tailscale_login = str(cfg.get("tailscale_login") or "").strip()
        trust = cfg.get("trust_tailscale_header", True)
        # The owner may turn the identity header off; the cookie then is
        # the only sign-in.
        self.trust_tailscale_header = trust is not False and str(trust).strip().lower() not in (
            "false", "0", "no", "off", "")
        self.store_path = Path(store_path)
        self.pair_token_file = Path(cfg.get("pair_token_file")
                                    or self.store_path.parent / PAIR_FILE)
        self.webui_dir = Path(webui_dir) if webui_dir else WEBUI_DIR
        self.push_subject = str(cfg.get("push_subject") or "").strip() or web_push.DEFAULT_SUBJECT
        self.peer_connections = _peer_cap(cfg.get("peer_connections"))
        self.owner_id = OWNER
        self.bot_user_id = BOT
        self.main_channel = MAIN_ID
        self.main_channel_name = MAIN_NAME
        self.sched_channel = ""
        # Shadow mode: the composite takes the owner's web input here
        # (docs/webchat.md); None dispatches it through self.registry.
        self.inbound: Inbound | None = None
        self.notice: Callable[[str], None] | None = None
        self.ping_s = PING_S
        self.tail_s = TAIL_S
        # Seconds the server loop waits between shutdown checks, and the
        # pause after a wrong pairing token; tests shorten both.
        self.poll_s = 0.5
        self.pair_delay_s = 0.5
        self._store: WebStore | None = None
        self._store_lock = threading.Lock()
        self._push: web_push.Pusher | None = None
        self._life_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._rooms_lock = threading.Lock()
        # One pairing attempt at a time, so the pause after a wrong token
        # bounds the rate of guesses.
        self._pair_lock = threading.Lock()
        self._short_token_logged = ""
        self._owner_ids_lock = threading.Lock()
        self._owner_ids: tuple[tuple, frozenset] | None = None
        self._stop = threading.Event()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._tail: threading.Thread | None = None
        # The relay client (relay_client.RelayClient) while it runs.
        self.relay: Any = None
        # Settings screens the host serves at /api/settings/<name>: each
        # an object with get() -> dict and save(body) -> dict, whose
        # ValueError refuses the body.
        self.settings_pages: dict[str, Any] = {}
        self.html_csp = self._html_csp()
        # The app's signed bundle (web_bundle.Bundle) once start() signed it.
        self._bundle: web_bundle.Bundle | None = None
        self._bundle_lock = threading.Lock()
        self._bundle_warned: set = set()
        self._own_sign_key: bytes | None = None
        self.bundle_check_s = BUNDLE_CHECK_S

    @classmethod
    def from_config(cls, cfg: dict | None, *, store_path: str | Path | None = None,
                    webui_dir: str | Path | None = None,
                    writer_only: bool = False, trusted_config: bool = False,
                    untrusted_roots: tuple[str | Path, ...] = ()) -> WebFrontend:
        """Build one from the chat block's `web` section. The store is
        `store_path`, else the block's `store_path`; ChatError with
        neither. `writer_only` is a command-line sender's copy;
        `trusted_config` and `untrusted_roots` are start()'s checks."""
        cfg = dict(cfg or {})
        path = store_path or cfg.get("store_path")
        if not path:
            raise ChatError("web chat: no store path")
        return cls(cfg, store_path=path, webui_dir=webui_dir, writer_only=writer_only,
                   trusted_config=trusted_config, untrusted_roots=untrusted_roots)

    @property
    def store(self) -> WebStore:
        with self._store_lock:
            if self._store is None:
                self._store = WebStore(self.store_path)
            return self._store

    @property
    def push(self) -> web_push.Pusher:
        """The Web Push sender; its VAPID key sits beside the store."""
        store = self.store
        with self._store_lock:
            if self._push is None:
                self._push = web_push.Pusher(store, self.store_path.parent / web_push.KEY_FILE,
                                             subject=self.push_subject, pair_hash=self.pair_hash,
                                             devices=self._relay_device_ids)
            return self._push

    # lifecycle

    def start(self) -> None:
        """Open the store, make the standalone channels (unless a composite
        drives this frontend), write the pairing token if there is none,
        serve HTTP on a daemon thread and tail the event log on another.
        Writer-only, open the store and nothing else. Idempotent.

        It serves only a trusted chat block, a private store directory and
        a private pairing token outside the resident's tree (the module
        docstring). Otherwise it sets `refused` and logs why; standalone
        (no `inbound`) it also raises ChatError, since there is no primary
        to run on."""
        with self._life_lock:
            if self.writer_only:
                _ = self.store
                return
            if self._server is not None:
                return
            # The paths are checked before anything is written there.
            why = self._config_refusal() or self._place_refusal()
            store = None if why else self.store
            if store is not None:
                why = self._store_refusal()
            if store is not None and not why:
                self._ensure_pair_token()
                why = self._token_refusal()
            if why:
                self.refused = why
                log.error("web chat: not serving: %s", why)
                if self.inbound is None:
                    raise ChatError(f"web chat: not serving: {why}")
                return
            self.refused = None
            store.prune_events()
            if self.inbound is None:
                self._ensure_own_channels()
            cls = _Server6 if ":" in self.bind else _Server
            srv = cls((self.bind, self.port), _Handler, max_per_peer=self.peer_connections)
            srv.frontend = self
            self.port = int(srv.server_address[1])
            self._stop.clear()
            self._server = srv
            self._thread = threading.Thread(target=srv.serve_forever,
                                            kwargs={"poll_interval": self.poll_s},
                                            name="webchat-http", daemon=True)
            self._thread.start()
            self._tail = threading.Thread(target=self._tail_events, name="webchat-tail",
                                          daemon=True)
            self._tail.start()
            if web_push.AVAILABLE:
                self.push.prune()
                self.push.start()
            log.info("web chat serving on %s:%d%s/", self.bind, self.port, self.base_path)
            self._start_relay()
            self.refresh_bundle()

    # the relay (docs/webchat-relay.md)

    def relay_block(self) -> dict:
        """The web block's `relay` block; {} when there is none or it is
        off (`enabled: false`)."""
        relay = self.cfg.get("relay")
        if not isinstance(relay, dict) or not relay or relay.get("enabled", True) is False:
            return {}
        return relay

    def relay_origin(self) -> str:
        """The configured relay's origin (scheme, host, port), or ""."""
        parts = urllib.parse.urlsplit(str(self.relay_block().get("url") or ""))
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        return origin if _ORIGIN_RE.match(origin) else ""

    def relay_source(self) -> str:
        """The relay as connect-src names it: its origin, and its path as a
        prefix (`https://host/relay/`) when it has one, so the page reaches
        that path of a shared host and nothing else there. "" when the URL
        is not a plain origin and path."""
        origin = self.relay_origin()
        path = urllib.parse.urlsplit(str(self.relay_block().get("url") or "")).path.rstrip("/")
        if not origin or not _RELAY_PATH_RE.match(path):
            return ""
        return f"{origin}{path}/" if path else origin

    def _html_csp(self) -> str:
        """The pages' policy: with a relay configured, connect-src names it,
        so the app served here can reach the relay when this server cannot
        be reached (off the tailnet)."""
        source = self.relay_source()
        if not source:
            return HTML_CSP
        return HTML_CSP.replace("connect-src 'self'", f"connect-src 'self' {source}")

    def _relay_device_ids(self) -> set[str] | None:
        """The relay devices still paired; None when no relay runs. A push
        subscription that names another device is dropped."""
        relay = self.relay
        if relay is None:
            return None
        try:
            return {d["id"] for d in relay.devices()}
        except Exception:
            log.exception("web chat: the relay's devices could not be read")
            return None

    def _relay_notice(self, text: str) -> None:
        """A notice from the relay client (a new device) in the owner's DM.
        In shadow mode the composite sets `notice`, which posts in the
        primary's DM and mirrors it; standalone, the web DM is `dm`."""
        if self.notice is not None:
            self.notice(text)
        else:
            self.post(DM_ID, text)

    def loopback_url(self) -> str:
        """This server on loopback, base path included: where the relay
        client replays a phone's requests."""
        host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(self.bind, self.bind)
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}{self.base_path}"

    def _start_relay(self) -> None:
        """Start the relay client when the web block names a relay. The
        block is a trust setting: ea_config.get_chat keeps it only from the
        owner-private chat.json. The key file defaults to the store's
        directory (the owner-private one). A relay that does not start is
        logged, and the chat goes on serving the tailnet."""
        relay = self.relay_block()
        if not relay:
            return
        try:
            from .relay_client import start_relay
            cfg = dict(self.cfg)
            cfg["pair_token_file"] = str(self.pair_token_file)
            if not relay.get("key_file"):
                cfg["relay"] = {**relay, "key_file": str(self.store_path.parent / RELAY_KEY_FILE)}
            why = self._key_file_refusal(Path(os.path.expanduser(str(cfg["relay"]["key_file"]))))
            if why:
                log.error("web chat: the relay is off: %s", why)
                return
            self.relay = start_relay(cfg, self.loopback_url(), logger=log,
                                     notify=self._relay_notice)
        except Exception:
            log.exception("web chat: the relay did not start")
            self.relay = None

    def _key_file_refusal(self, path: Path) -> str | None:
        """Why the relay key file may not live at `path`: inside the
        resident's tree, in a directory another user may write (where the
        file could be deleted for a new identity, or an old copy put back
        with a removed device in it), or a file not this user's alone."""
        root = _inside(path, self.untrusted_roots)
        if root is not None:
            return f"{path} is inside {root}, which another user controls"
        if os.path.lexists(path.parent):
            why = _guarded_dir_problem(path.parent)
            if why:
                return why
        return private_problem(path, "file") if os.path.lexists(path) else None

    def _config_refusal(self) -> str | None:
        if not self.trusted_config:
            return ("the chat block does not come from the owner-private chat.json, so its "
                    "bind, public_url, login and token file may not be the owner's; the web "
                    "chat serves only that block (docs/SECURITY.md, Web chat)")
        return None

    def _place_refusal(self) -> str | None:
        """Why the store or the pairing token may not live where the block
        puts them, checked before either is opened or written: inside the
        resident's tree, or in a directory that is there and not this
        user's alone (the store's) or that others may write (the token's)."""
        d, p = self.store_path.parent, self.pair_token_file
        for path in (d, p):
            root = _inside(path, self.untrusted_roots)
            if root is not None:
                return f"{path} is inside {root}, which another user controls"
        if os.path.lexists(d):
            why = private_problem(d, "dir")
            if why:
                return why
        if os.path.lexists(p.parent):
            return _guarded_dir_problem(p.parent)
        return None

    def _store_refusal(self) -> str | None:
        """Why the opened store may not be served: its directory and file
        must be this user's alone."""
        return (private_problem(self.store_path.parent, "dir")
                or private_problem(self.store_path, "file"))

    def _token_refusal(self) -> str | None:
        """Why the pairing token file may not be trusted. A missing file is
        no refusal (the cookie is then off); one that is there must be this
        user's alone, in a directory no other user can write."""
        p = self.pair_token_file
        if not os.path.lexists(p):
            return None
        return _guarded_dir_problem(p.parent) or private_problem(p, "file")

    def _tail_events(self) -> None:
        """Every tail_s seconds, hand the open streams the events other
        processes wrote to the store (WebStore.deliver_new), then post the
        scheduled posts now due (post_due); every bundle_check_s, check the
        app's files against the signed bundle."""
        next_check = time.monotonic() + self.bundle_check_s
        while not self._stop.wait(self.tail_s):
            try:
                self.store.deliver_new()
            except Exception as e:
                # The next read tries again: a busy or failed read loses nothing.
                log.warning("web chat: reading the event log failed: %s", e)
            self.post_due()
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + self.bundle_check_s
                try:
                    self.check_bundle()
                except Exception as e:
                    log.warning("web chat: checking the app's files failed: %s", e)

    # the signed app bundle (docs/webchat.md, Code integrity)

    @property
    def bundle(self) -> web_bundle.Bundle | None:
        """The app's signed bundle; None before start() or without the
        cryptography package."""
        return self._bundle

    def bundle_hash(self) -> str:
        """The bundle hash the bot signs now; "" when it signs none. A front
        end that is not serving (the owner's CLI) signs on first use when
        its block names a relay, with the relay key file's key, so its hash
        is the serving bot's for the same files."""
        if self._bundle is None and self.relay_block() and relay_crypto.AVAILABLE \
                and self._server is None:
            self.refresh_bundle()
        bundle = self._bundle
        return bundle.hash if bundle is not None else ""

    def _key_file(self) -> Path | None:
        """The relay key file the relay block names (or its default beside
        the store), when it is there and may be trusted."""
        relay = self.relay_block()
        if not relay:
            return None
        path = Path(os.path.expanduser(str(relay.get("key_file")
                                           or self.store_path.parent / RELAY_KEY_FILE)))
        return path if os.path.lexists(path) and not self._key_file_refusal(path) else None

    def _signing_key(self) -> bytes:
        """The Ed25519 key that signs the app: the relay key file's when a
        relay is configured (the key a relay-origin phone pins), else one
        made for this process."""
        try:
            if self.relay is not None:
                return self.relay.store.signing_keypair()[0]
            path = self._key_file()
            if path is not None:
                from .relay_client import KeyStore
                return KeyStore(path).signing_keypair()[0]
        except Exception as e:
            log.error("web chat: the relay key file holds no signing key (%s); the app is "
                      "signed with a key of this process alone", e)
        if self._own_sign_key is None:
            self._own_sign_key = relay_crypto.signing_keypair()[0]
        return self._own_sign_key

    def refresh_bundle(self) -> str:
        """Hash and sign the files under webui/ again, and serve the new
        manifest; the bundle hash, or "" when the bot cannot sign (no
        cryptography package). A phone's check then compares with this one."""
        if not relay_crypto.AVAILABLE:
            log.warning("web chat: the app is not signed: the cryptography package is missing")
            return ""
        try:
            new = web_bundle.Bundle(self.webui_dir, self._signing_key())
        except Exception:
            log.exception("web chat: signing the app failed")
            return ""
        with self._bundle_lock:
            old, self._bundle = self._bundle, new
            self._bundle_warned = set()
        if new.skipped:
            log.warning("web chat: the manifest leaves out %s", ", ".join(new.skipped))
        if old is None:
            log.info("web chat: signed the app, %d files, bundle %s", len(new.files),
                     new.hash[:16])
        elif old.hash != new.hash:
            log.warning("web chat: signed the app again, bundle %s; changed: %s", new.hash[:16],
                        ", ".join(web_bundle.diff(old.files, new.files)) or "built_at")
        return new.hash

    def _warn_unsigned(self, paths: list[str], why: str) -> None:
        key = (tuple(paths), why)
        with self._bundle_lock:
            if key in self._bundle_warned:
                return
            self._bundle_warned.add(key)
        log.warning("web chat: %s no longer match%s the app the bot signed at start (%s); a "
                    "phone refuses them until the bot signs again (refresh_bundle or a restart)",
                    ", ".join(paths), "es" if len(paths) == 1 else "", why)

    def check_bundle(self) -> list[str]:
        """The files under webui/ that no longer match the signed bundle
        (changed, added or gone); each change is logged once."""
        bundle = self._bundle
        if bundle is None:
            return []
        changed = bundle.changes()
        if changed:
            self._warn_unsigned(changed, "on disk")
        return changed

    def note_served(self, rel: str, data: bytes) -> bool:
        """Whether `data`, served as `rel`, is the file the bot signed; a
        file that is not is logged once."""
        bundle = self._bundle
        if bundle is None or bundle.matches(rel, data):
            return True
        self._warn_unsigned([rel], "served " + web_bundle.sha256_hex(data)[:16])
        return False

    def run_forever(self) -> None:
        """Block until stop(); the server runs on its own thread."""
        while not self._stop.wait(1.0):
            pass

    def stop(self) -> None:
        """Stop the relay client, stop serving, stop tailing the event log
        and close every event stream."""
        self._stop.set()
        with self._life_lock:
            srv, self._server = self._server, None
            tail, self._tail = self._tail, None
            relay, self.relay = self.relay, None
        if relay is not None:
            relay.stop()
        if self._store is not None:
            self._store.close_subscribers()
        if self._push is not None:
            self._push.stop()
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        if tail is not None and tail is not threading.current_thread():
            tail.join(timeout=5)

    def ping(self) -> bool:
        if self.refused or not self.store.ok():
            return False
        return self._thread is None or self._thread.is_alive()

    @property
    def url(self) -> str:
        """The server's own loopback URL, base path included."""
        host = f"[{self.bind}]" if ":" in self.bind else self.bind
        return f"http://{host}:{self.port}{self.base_path}"

    def _ensure_own_channels(self) -> None:
        for cid, name, kind in ((MAIN_ID, MAIN_NAME, "main"), (DM_ID, DM_NAME, "dm")):
            if self.store.channel(cid) is None:
                self.store.upsert_channel(cid, name, kind)

    def _ensure_pair_token(self) -> None:
        p = self.pair_token_file
        if p.exists():
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
            log.info("web chat: pairing token written to %s", p)
        except FileExistsError:
            pass
        except OSError as e:
            log.warning("web chat: could not write the pairing token at %s: %s", p, e)

    def pair_token(self) -> str:
        """The pairing token, read fresh so a replaced file takes effect; ""
        when there is none, or when the file holds fewer than
        MIN_PAIR_TOKEN characters (refused, with a log line)."""
        try:
            token = self.pair_token_file.read_text().strip()
        except OSError:
            return ""
        if len(token) < MIN_PAIR_TOKEN:
            if token:
                mark = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if mark != self._short_token_logged:
                    self._short_token_logged = mark
                    log.warning("web chat: the pairing token in %s is shorter than %d "
                                "characters; pairing by token is off until it is replaced",
                                self.pair_token_file, MIN_PAIR_TOKEN)
            return ""
        return token

    def pair_hash(self) -> str:
        """A hash of the pairing token now; "" when there is none. Push
        subscriptions made under another token are dropped (web_push)."""
        token = self.pair_token()
        return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""

    # who is the owner

    def peer_is_tailscaled(self, peer: tuple, local: tuple) -> bool:
        """True when the client end of this connection belongs to
        tailscaled's uid (Linux): only then is the identity header the
        proxy's. A mismatch looks tailscaled up again (rate-limited)."""
        uid = peer_uid(peer, local)
        if uid is None:
            return False
        if uid == tailscaled_uid():
            return True
        return uid == tailscaled_uid(refresh=True)

    def host_allowed(self, host: str) -> bool:
        """True when a request's Host names this server: public_url's host
        and port, or a loopback name with this server's port."""
        got = _hostport(host)
        if got is None:
            return False
        name, port = got
        pub = self.public_origin()
        if pub is not None and name == pub[1]:
            return (port if port is not None else (443 if pub[0] == "https" else 80)) == pub[2]
        return name in LOOPBACK_HOSTS and (port if port is not None else 80) == self.port

    def public_origin(self) -> tuple[str, str, int] | None:
        """(scheme, host, port) of public_url; None when there is none."""
        return _origin_key(self.public_url) if self.public_url else None

    def loopback_origin(self, key: tuple[str, str, int] | None) -> bool:
        """True for an http origin on a loopback name and this server's port."""
        return key is not None and key[0] == "http" and key[1] in LOOPBACK_HOSTS \
            and key[2] == self.port

    # the owner's web messages, by primary id (shadow mode)

    @property
    def owner_ids_file(self) -> Path:
        return self.store_path.parent / OWNER_IDS_FILE

    def _read_owner_ids(self) -> frozenset:
        """The (channel, id) pairs in the owner-ids file, read again when
        the file changed. Empty when it is missing or is not this user's
        alone (a file another user could write proves nothing)."""
        p = self.owner_ids_file
        try:
            st = os.lstat(p)
        except OSError:
            return frozenset()
        # A change of content, mode, owner or links reads it (and checks it) again.
        stamp = (st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid, st.st_ino, st.st_nlink)
        with self._owner_ids_lock:
            if self._owner_ids is not None and self._owner_ids[0] == stamp:
                return self._owner_ids[1]
        if private_problem(p, "file"):
            log.warning("web chat: %s is not this user's alone; it is ignored", p)
            return frozenset()
        try:
            data = json.loads(p.read_text())
            ids = frozenset((str(c), str(m)) for c, m in data.get("ids") or [])
        except (OSError, ValueError, TypeError, AttributeError):
            return frozenset()
        with self._owner_ids_lock:
            self._owner_ids = (stamp, ids)
        return ids

    def owner_posted(self, channel: str, msg_id: str) -> bool:
        """True when the primary's message `msg_id` in `channel` carries the
        owner's web input (posted as the bot): the history copy and
        chat_read take it as the owner's."""
        return bool(msg_id) and (str(channel), str(msg_id)) in self._read_owner_ids()

    def note_owner_post(self, channel: str, msg_id: str) -> None:
        """Record that the primary's message carries the owner's web input;
        the file keeps the newest OWNER_IDS_MAX. A failure is logged."""
        if not msg_id:
            return
        p = self.owner_ids_file
        with self._owner_ids_lock:
            try:
                data = json.loads(p.read_text()) if p.exists() else {}
                ids = [list(x) for x in data.get("ids") or []
                       if isinstance(x, list) and len(x) == 2]
            except (OSError, ValueError, AttributeError):
                ids = []
            item = [str(channel), str(msg_id)]
            if item not in ids:
                ids.append(item)
            ids = ids[-OWNER_IDS_MAX:]
            try:
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_private(p, json.dumps({"v": 1, "ids": ids}).encode("utf-8"))
            except OSError as e:
                log.warning("web chat: the owner's message ids could not be kept: %s", e)
            self._owner_ids = None

    # helpers

    def _site(self) -> str:
        return self.public_url or self.base_path

    def _out(self, text: str) -> str:
        """The bot's text as the web app renders it: Slack marks become
        Markdown as on Zulip, but emoji shortcodes keep their Slack names,
        the names reactions use, instead of taking Zulip's."""
        held: list[str] = []

        def hold(m: re.Match) -> str:
            held.append(m.group(0))
            return f"\x02{len(held) - 1}\x02"
        text = _SHORTCODE_RE.sub(hold, (text or "").replace("\x02", ""))
        out = mrkdwn_to_zulip(text, self.mention)
        return _HELD_RE.sub(lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held)
                            else "", out)

    def _thread_for(self, channel: str, thread_ts: str | None) -> str | None:
        if not thread_ts:
            return None
        known = self.store.thread_of(channel, thread_ts)
        if known:
            return known
        return thread_ts if THREAD_KEY_RE.match(thread_ts) else None

    def thread_channel(self, thread_ts: str | None) -> str | None:
        """The channel that holds a thread, for a caller that has only the
        thread key (the bot delivering a staged job): a web key names no
        channel in its text. None when the store does not know it."""
        return self.store.thread_channel(thread_ts)

    def _channel_or_raise(self, channel: str) -> dict:
        row = self.store.channel(channel)
        if row is None:
            raise ChatError(f"channel_not_found: {channel}")
        return row

    def _ref(self, row: dict) -> MessageRef:
        return MessageRef(row["channel"], row["id"], row["thread_key"], row["posted_at"],
                          self._permalink(row["channel"], row["thread_key"], row["id"]))

    def _ensure_channel(self, channel: str) -> None:
        """A mirrored write to a channel the composite has not described yet
        makes a placeholder row, so nothing it mirrors is lost."""
        if self.store.channel(channel) is None:
            self.store.upsert_channel(channel, channel, "room")

    def _file_json(self, fid: str, name: str, mimetype: str, size: int,
                   url: str = "") -> dict:
        """A message's file. A file kept here has its id and this server's
        url; one kept only on the primary has no id and the primary's url."""
        if not fid:
            return {"id": "", "name": name, "url": url, "mimetype": mimetype, "size": int(size)}
        return {"id": fid, "name": name,
                "url": f"{self.base_path}/api/files/{urllib.parse.quote(fid, safe='')}",
                "mimetype": mimetype, "size": int(size)}

    # notifications (Web Push)

    def notify(self, title: str, body: str = "", url: str = "", tag: str = "", *,
               urgency: str = "normal", at: float | None = None, wait: bool = False) -> dict:
        """Push a notification to every browser that turned them on: the
        title, the first line of `body`, and `url` to open on a tap. `tag`
        names the thread: one notification per tag on the device, and at
        most one push per thread in 30 s (web_push.Pusher.notify). `at`
        (a Unix time ahead) queues it for the worker; `wait` sends before
        returning. Never raises: push is an extra, and a failure is logged."""
        if not web_push.AVAILABLE:
            return {"sent": 0, "push": "off"}
        try:
            return self.push.notify(title, body, url, tag, urgency=urgency, at=at, wait=wait)
        except Exception:
            log.exception("web chat: a push failed")
            return {"sent": 0, "error": "push failed"}

    def cancel_notify(self, tag: str) -> bool:
        """Drop the push queued under `tag`; never raises."""
        try:
            return web_push.AVAILABLE and self.push.cancel(tag)
        except Exception:
            log.exception("web chat: a queued push could not be cancelled")
            return False

    # scheduled posts (chat.direct.schedule_direct on the web chat alone)

    def schedule_post(self, channel: str, text: str, at: float) -> int:
        """Queue `text` as the bot's post in `channel` at `at` (a Unix time);
        the serving bot posts it when it is due (post_due). Its id, which
        cancel_scheduled_post takes. A writer-only copy queues it as well."""
        return self.store.post_defer(channel, text or "", float(at))

    def cancel_scheduled_post(self, post_id: int) -> bool:
        """Drop a queued post; False when it went out already or never was."""
        return self.store.post_cancel(int(post_id))

    def scheduled_posts(self) -> list[dict]:
        """The queued posts: id, channel, text, due_at, created_at."""
        return self.store.posts_queued()

    def post_due(self, now: float | None = None) -> int:
        """Post each scheduled post now due as the bot, each one a new
        thread; the number posted. One the bot was down for goes out when it
        is back. It carries no push of its own: the sender queues the push
        for the same moment (chat.direct.web_notify). A post whose channel
        is gone is dropped, with a log line. Never raises."""
        try:
            rows = self.store.post_claim_due(time.time() if now is None else now)
        except Exception as e:
            log.warning("web chat: reading the scheduled posts failed: %s", e)
            return 0
        posted = 0
        for row in rows:
            try:
                if self.store.channel(row["channel"]) is None:
                    log.warning("web chat: scheduled post %s dropped: channel %s is gone",
                                row["id"], row["channel"])
                    continue
                self.store.add_message(row["channel"], mid=None, thread_key=None, author=BOT,
                                       kind="message", text=self._out(row["text"]))
                posted += 1
            except Exception:
                log.exception("web chat: scheduled post %s failed", row["id"])
        return posted

    def app_url(self) -> str:
        """The web app's own address: public_url, else the base path."""
        return self._site() + "/"

    @staticmethod
    def _push_tag(row: dict) -> str:
        """A thread's notification tag: one notification per thread."""
        return f"{row['channel']}/{row['thread_key']}"

    def _push_message(self, row: dict | None) -> None:
        """A new bot message in the DM channel is pushed."""
        if not row or row.get("author") != BOT or row.get("kind") != "message":
            return
        ch = self.store.channel(row["channel"])
        if not ch or ch["kind"] != "dm":
            return
        self.notify("Tacitus", row.get("text") or "",
                    self._permalink(row["channel"], row["thread_key"], row["id"]),
                    self._push_tag(row))

    def _push_card(self, row: dict | None, card: ConfirmationCard) -> None:
        """A new card is pushed at high urgency: "Tacitus", then the card's
        title. Never a field, a line or the body: they hold addresses and
        mail text, and a notification shows on a locked screen."""
        if not row:
            return
        self.notify("Tacitus", card.title or "A card to confirm",
                    self._permalink(row["channel"], row["thread_key"], row["id"]),
                    self._push_tag(row), urgency="high")

    # messages

    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        self._channel_or_raise(channel)
        row = self.store.add_message(channel, mid=None,
                                     thread_key=self._thread_for(channel, thread_ts),
                                     author=BOT, kind="message", text=self._out(text))
        self._push_message(row)
        return self._ref(row)

    def update(self, ref: MessageRef, text: str) -> bool:
        return self.store.change_message(ref.channel, ref.id, "update",
                                         text=self._out(text)) is not None

    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        self.store.toggle_reaction(channel, msg_id, _status_name(status), True)
        return self.store.message(channel, msg_id) is not None

    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        self.store.toggle_reaction(channel, msg_id, _status_name(status), False)
        return self.store.message(channel, msg_id) is not None

    # cards

    @staticmethod
    def _card_json(card: ConfirmationCard) -> dict:
        """The card as the store keeps it: asdict, through JSON (tuples
        become lists), so a stored card compares equal to a fresh one."""
        return json.loads(json.dumps(asdict(card)))

    def _card_text(self, card: ConfirmationCard) -> str:
        return self._out(card.fallback_text or card.title or "")

    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        self._channel_or_raise(channel)
        row = self.store.add_message(channel, mid=None,
                                     thread_key=self._thread_for(channel, thread_ts),
                                     author=BOT, kind="card", text=self._card_text(card),
                                     card=self._card_json(card))
        self._push_card(row, card)
        return self._ref(row)

    def card_fits(self, card: ConfirmationCard) -> bool:
        """The web app shows a card's whole body and quoted thread."""
        return True

    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        return self.store.change_message(ref.channel, ref.id, "card", kind="card",
                                         text=self._card_text(card),
                                         card=self._card_json(card)) is not None

    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        """The row becomes a `status` row; its text is the core's status line
        as written, which the app renders as card text."""
        return self.store.change_message(ref.channel, ref.id, "status", kind="status",
                                         text=text or "") is not None

    def message_posted_at(self, ref: MessageRef) -> float | None:
        row = self.store.message(ref.channel, ref.id)
        return float(row["posted_at"]) if row and row["posted_at"] is not None else None

    # conversations and rooms

    def dm_channel(self, user_id: str) -> str:
        return DM_ID

    def is_dm(self, channel: str) -> bool:
        row = self.store.channel(channel)
        return bool(row and row["kind"] == "dm")

    def channel_info(self, channel: str) -> ChannelInfo:
        row = self.store.channel(channel)
        if row is None:
            return ChannelInfo(channel, "", is_private=False, is_member=False, exists=False)
        return ChannelInfo(channel, row["name"], is_private=True, is_member=True,
                           is_archived=row["archived"], is_dm=row["kind"] == "dm")

    def channel_members(self, channel: str) -> set[str] | None:
        return {self.owner_id, self.bot_user_id} if self.store.channel(channel) else None

    def mention(self, channel: str) -> str:
        row = self.store.channel(channel)
        name = (row or {}).get("name") or channel
        return f"[#{name}]({self.room_url(channel)})"

    def room_url(self, channel: str) -> str:
        return f"{self._site()}/#/{urllib.parse.quote(channel, safe='')}"

    def _permalink(self, channel: str, thread_key: str, msg_id: str) -> str:
        q = urllib.parse.quote
        return f"{self._site()}/#/{q(channel, safe='')}/{q(thread_key, safe='')}/{q(msg_id, safe='')}"

    def permalink(self, channel: str, msg_id: str) -> str:
        row = self.store.message(channel, msg_id)
        return self._permalink(channel, row["thread_key"] if row else msg_id, msg_id)

    def _inbound_of(self, row: dict, is_dm: bool, opens: bool) -> InboundMessage:
        """A stored message as the core reads history; `opens` is True for
        the first message of its thread."""
        f = row.get("file")
        files = [FileRef(name=f.get("name", ""), url=f.get("url", ""), id=f.get("id", ""),
                         mimetype=f.get("mimetype", ""))] if isinstance(f, dict) else []
        bot = row["author"] != OWNER
        return InboundMessage(
            channel=row["channel"], msg_id=row["id"], text=row["text"] or "",
            user_id=self.bot_user_id if bot else self.owner_id,
            posted_at=float(row["posted_at"] or 0.0), thread_key=row["thread_key"],
            is_reply=not opens, is_dm=is_dm, is_bot=bot, is_self=bot,
            subtype="file_share" if files else "", files=files,
            reply_count=int(row.get("reply_count") or 0), raw=row)

    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        key = self.store.thread_of(channel, thread_ts)
        if not key:
            return []
        dm = self.is_dm(channel)
        rows = self.store.thread(channel, key, limit)
        return [self._inbound_of(r, dm, i == 0) for i, r in enumerate(rows)]

    def _seq_of(self, channel: str, msg_id: str) -> int:
        row = self.store.message(channel, msg_id)
        if row:
            return int(row["seq"])
        m = re.fullmatch(r"w(\d+)", msg_id or "")
        return int(m.group(1)) if m else 0

    def recent_messages(self, channel: str, since_s: float,
                        limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        after = self._seq_of(channel, since_id) if since_id else 0
        dm = self.is_dm(channel)
        return [self._inbound_of(r, dm, True)
                for r in self.store.parents(channel, since_s, after, limit)]

    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        with self._rooms_lock:
            if any((c["name"] or "").lower() == (name or "").lower()
                   for c in self.store.channels()):
                raise RoomNameTaken(name)
            cid = f"r{self.store.next_number('channels', 'r')}"
            self.store.upsert_channel(cid, name, "room", False, description or "")
        return RoomRef(cid, name, self.room_url(cid))

    def invite(self, channel: str, user_id: str) -> None:
        """One owner, one bot: both are in every room already."""

    def set_description(self, channel: str, text: str) -> None:
        self.store.set_channel(channel, description=text or "")

    def archive(self, channel: str) -> None:
        row = self._channel_or_raise(channel)
        if row["archived"]:
            raise AlreadyArchived(channel)
        self.store.set_channel(channel, archived=True)

    def unarchive(self, channel: str) -> None:
        row = self._channel_or_raise(channel)
        if not row["archived"]:
            raise NotArchived(channel)
        self.store.set_channel(channel, archived=False)

    def archived_map(self) -> dict[str, bool]:
        try:
            return {c["id"]: bool(c["archived"]) for c in self.store.channels()
                    if c["kind"] != "dm"}
        except sqlite3.Error:
            return {}

    # files

    @staticmethod
    def _mimetype(name: str, given: str = "") -> str:
        given = (given or "").split(";", 1)[0].strip().lower()
        if given and given != "application/octet-stream":
            return given
        return mimetypes.guess_type(name)[0] or "application/octet-stream"

    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        self._channel_or_raise(channel)
        p = Path(path)
        data = p.read_bytes()
        name = safe_name(filename or p.name)
        mimetype = self._mimetype(name)
        fid = self.store.add_file(None, channel, name, data, mimetype)["id"]
        fj = self._file_json(fid, name, mimetype, len(data))
        self.store.add_message(channel, mid=None, thread_key=self._thread_for(channel, thread_ts),
                               author=BOT, kind="file", text=self._out(comment), file=fj)
        return FileRef(name=name, url=fj["url"], id=fid, mimetype=mimetype, title=title)

    def _file_id(self, ref: FileRef) -> str:
        if ref.id and self.store.file(ref.id):
            return ref.id
        m = re.search(r"/api/files/([^/?#]+)$", ref.url or "")
        return urllib.parse.unquote(m.group(1)) if m else (ref.id or "")

    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        """Copy a stored file into dest_dir as <YYYYmmdd-HHMMSS>_<name>,
        through the application's writers (chat.files)."""
        row = self.store.file(self._file_id(ref))
        if not row or not row.get("path"):
            return None
        try:
            data = Path(row["path"]).read_bytes()
        except OSError as e:
            log.warning("web chat: stored file %s unreadable: %s", row["id"], e)
            return None
        dest = Path(dest_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = dest / f"{stamp}_{_disk_name(ref.name or row['name'])}"
        if not (_files.make_dir(dest, 0o2775) and _files.replace_bytes(target, data, 0o664)):
            log.warning("web chat: download could not be saved to %s", target)
            return None
        return str(target)

    # LLM-facing

    def prompt_strings(self) -> dict[str, str]:
        return dict(self.PROMPT_STRINGS)

    def llm_tool_grants(self) -> str:
        return ""

    # shadow mode: the composite's writes, with the primary's ids

    def mirror_channel(self, id: str, name: str, kind: str, archived: bool = False, *,
                       description: str | None = None) -> dict:
        """Write a channel row as the primary describes it; a description
        not given keeps the stored one."""
        return self.store.upsert_channel(id, name, kind, archived, description)

    def mirror_backfilled(self, channel: str) -> dict | None:
        """The composite copied history into the channel quietly: one
        `channel` event, so an open app reads the channel's threads again."""
        return self.store.touch_channel(channel)

    def mirror_move(self, old_channel: str, old_key: str, new_channel: str,
                    new_key: str) -> int:
        """A thread the primary moved (a renamed or moved Zulip topic): its
        messages take the new channel and key. The number moved."""
        self._ensure_channel(new_channel)
        return self.store.move_thread(old_channel, old_key, new_channel, new_key)

    def _history_row(self, ref: MessageRef, *, author: str, kind: str, text: str,
                     file: dict | None = None) -> dict | None:
        """Store a message from the primary's past as a history row (no
        event, never unread). None when the row is stored already, or when
        the thread holds a newer message: the rows of a thread stay in time
        order, so a thread mirrored live before keeps its older history out."""
        if self.store.message(ref.channel, ref.id) is not None:
            return None
        key = ref.thread_key or ref.id
        newest = self.store.newest_in_thread(ref.channel, key)
        if newest is not None and ref.posted_at is not None and newest > float(ref.posted_at):
            return None
        try:
            return self.store.add_message(ref.channel, mid=ref.id, thread_key=key,
                                          author=author, kind=kind, text=text, file=file,
                                          posted_at=ref.posted_at, history=True)
        except sqlite3.IntegrityError:
            return None

    def _mirror_upsert(self, ref: MessageRef, event: str, *, author: str, kind: str,
                       text: str, card: dict | None = None, file: dict | None = None,
                       set_kind: bool = True, quiet: bool = False) -> dict | None:
        """Insert the message under the primary's ref, or bring the stored
        row in line and append `event` when anything changed. `kind` applies
        to a stored row only with set_kind. `quiet` stores a history row
        (_history_row) and leaves a stored row as it is."""
        self._ensure_channel(ref.channel)
        if quiet:
            return self._history_row(ref, author=author, kind=kind, text=text, file=file)
        row = self.store.message(ref.channel, ref.id)
        if row is None:
            try:
                return self.store.add_message(
                    ref.channel, mid=ref.id, thread_key=ref.thread_key or ref.id, author=author,
                    kind=kind, text=text, card=card, file=file, posted_at=ref.posted_at,
                    event="message" if event == "update" else event)
            except sqlite3.IntegrityError:
                row = self.store.message(ref.channel, ref.id)
                if row is None:
                    raise
        fields: dict[str, Any] = {}
        if row["text"] != text:
            fields["text"] = text
        if set_kind and row["kind"] != kind:
            fields["kind"] = kind
        if card is not None and row["card"] != card:
            fields["card"] = card
        if file is not None and row["file"] != file:
            fields["file"] = file
        if not fields:
            return row
        return self.store.change_message(ref.channel, ref.id, event, **fields) or row

    def mirror_post(self, ref: MessageRef, text: str, author: str = BOT, *,
                    quiet: bool = False, markdown: bool = False) -> dict | None:
        """Store a message the primary posted, under its ref. The bot's text
        is converted as post() converts it, unless `markdown` says it is
        Markdown already (Zulip's history); the owner's is kept as typed.
        `quiet` copies history: no event, never unread, a stored row left as
        it is and None returned for it."""
        author = OWNER if author == OWNER else BOT
        out = self._out(text) if author == BOT and not markdown else (text or "")
        new = self.store.message(ref.channel, ref.id) is None
        row = self._mirror_upsert(ref, "update", author=author, kind="message", text=out,
                                  set_kind=False, quiet=quiet)
        if new and row is not None and not quiet:
            self._push_message(row)
        return row

    def mirror_update(self, ref: MessageRef, text: str) -> dict:
        """Replace a stored message's text (a missing one is stored)."""
        row = self.store.message(ref.channel, ref.id)
        author = row["author"] if row else BOT
        out = self._out(text) if author == BOT else (text or "")
        return self._mirror_upsert(ref, "update", author=author, kind="message", text=out,
                                   set_kind=False)

    def mirror_card(self, ref: MessageRef, card: ConfirmationCard) -> dict:
        """Store a card the primary posted or changed; a new one is pushed."""
        new = self.store.message(ref.channel, ref.id) is None
        row = self._mirror_upsert(ref, "card", author=BOT, kind="card",
                                  text=self._card_text(card), card=self._card_json(card))
        if new:
            self._push_card(row, card)
        return row

    def mirror_card_status(self, ref: MessageRef, text: str) -> dict:
        return self._mirror_upsert(ref, "status", author=BOT, kind="status", text=text or "")

    def mirror_reaction(self, channel: str, msg_id: str, status: Status | str, on: bool) -> bool:
        """True when the message is in the store (whether or not it changed)."""
        self.store.toggle_reaction(channel, msg_id, _status_name(status), bool(on))
        return self.store.message(channel, msg_id) is not None

    def mirror_file(self, ref: MessageRef, file: FileRef, author: str = BOT, *,
                    path: str | Path | None = None, text: str = "", quiet: bool = False,
                    markdown: bool = False) -> dict | None:
        """Store a file message the primary posted. With `path` the bytes are
        kept too, under the primary's file id, and the web app downloads
        them from this server; without it the file has no id here and its
        link is the primary's absolute `file.url`. `quiet` and `markdown`
        are mirror_post's."""
        author = OWNER if author == OWNER else BOT
        if quiet and self.store.message(ref.channel, ref.id) is not None:
            return None
        fid = file.id or file.url or f"{ref.channel}:{ref.id}"
        name = safe_name(file.name)
        mimetype = self._mimetype(name, file.mimetype)
        kept = self.store.file(fid)
        if path is not None:
            data = Path(path).read_bytes()
            self.store.add_file(fid, ref.channel, name, data, mimetype)
            fj = self._file_json(fid, name, mimetype, len(data))
        elif kept:
            fj = self._file_json(fid, name, mimetype, int(kept["size"] or 0))
        else:
            fj = self._file_json("", name, mimetype, 0, file.url)
        out = self._out(text) if author == BOT and not markdown else (text or "")
        return self._mirror_upsert(ref, "file", author=author, kind="file", text=out, file=fj,
                                   quiet=quiet)

    # the owner's input from the web app

    def _dispatch(self, ev: Any) -> None:
        with self._dispatch_lock:
            try:
                if isinstance(ev, ActionEvent):
                    if not self.registry.dispatch_action(ev):
                        log.info("web chat: no handler for action %s", ev.action_id)
                else:
                    self.registry.dispatch_message(ev)
            except Exception:
                log.exception("web chat: a handler failed")

    def _hand_in(self, ev: Any) -> MessageRef | None:
        """Give the owner's input to the composite (shadow mode)."""
        with self._dispatch_lock:
            ref = self.inbound(ev) if self.inbound is not None else None
        return ref if isinstance(ref, MessageRef) else None

    def _target(self, channel: str, thread_key: str | None) -> tuple[dict, str | None]:
        ch = self.store.channel(channel)
        if ch is None:
            raise _HttpError(404, "no such channel")
        if ch["archived"]:
            raise _HttpError(400, "the channel is archived")
        key = None
        if thread_key:
            key = self.store.thread_of(channel, thread_key)
            if not key:
                raise _HttpError(404, "no such thread")
        return ch, key

    def web_message(self, channel: str, text: str,
                    thread_key: str | None) -> tuple[dict, Callable[[], None] | None]:
        """Store the owner's message and build its dispatch: (the stored
        message, a call to run once the reply is sent)."""
        ch, key = self._target(channel, thread_key)
        msg = InboundMessage(channel=channel, msg_id="", text=text, user_id=self.owner_id,
                             posted_at=time.time(), thread_key=key, is_reply=bool(key),
                             is_dm=ch["kind"] == "dm", raw={"source": "web"})
        if self.inbound is not None:
            ref = self._hand_in(msg)
            if ref is None:
                return {"ok": True}, None
            self.note_owner_post(ref.channel, ref.id)
            row = self.store.message(ref.channel, ref.id) or self.mirror_post(ref, text, OWNER)
            return row, None
        row = self.store.add_message(channel, mid=None, thread_key=key, author=OWNER,
                                     kind="message", text=text)
        msg.msg_id, msg.thread_key, msg.posted_at = row["id"], row["thread_key"], row["posted_at"]
        return row, lambda: self._dispatch(msg)

    def web_upload(self, channel: str, name: str, data: bytes, mimetype: str, text: str,
                   thread_key: str | None) -> tuple[dict, Callable[[], None] | None]:
        """Store the owner's upload and build its dispatch, as web_message."""
        ch, key = self._target(channel, thread_key)
        name = safe_name(name, "upload")
        mimetype = self._mimetype(name, mimetype)
        msg = InboundMessage(channel=channel, msg_id="", text=text, user_id=self.owner_id,
                             posted_at=time.time(), thread_key=key, is_reply=bool(key),
                             is_dm=ch["kind"] == "dm", subtype="file_share",
                             raw={"source": "web"})
        if self.inbound is not None:
            return self._upload_shadow(msg, name, data, mimetype, text), None
        fid = self.store.add_file(None, channel, name, data, mimetype)["id"]
        fj = self._file_json(fid, name, mimetype, len(data))
        row = self.store.add_message(channel, mid=None, thread_key=key, author=OWNER,
                                     kind="file", text=text, file=fj)
        msg.msg_id, msg.thread_key, msg.posted_at = row["id"], row["thread_key"], row["posted_at"]
        msg.files = [FileRef(name=name, url=fj["url"], id=fid, mimetype=mimetype)]
        return row, lambda: self._dispatch(msg)

    def _upload_shadow(self, msg: InboundMessage, name: str, data: bytes, mimetype: str,
                       text: str) -> dict:
        """Shadow mode: the bytes wait in a temp file whose path is
        msg.raw["path"] while the composite uploads them to the primary;
        the stored row keeps them under the primary's file id."""
        incoming = self.store.files_dir / "incoming"
        incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = incoming / secrets.token_hex(12)
        _write_private(tmp, data)
        msg.raw["path"] = str(tmp)
        msg.files = [FileRef(name=name, mimetype=mimetype)]
        try:
            ref = self._hand_in(msg)
            if ref is None:
                return {"ok": True}
            self.note_owner_post(ref.channel, ref.id)
            row = self.store.message(ref.channel, ref.id)
            if row is None:
                return self.mirror_file(ref, FileRef(name=name, mimetype=mimetype,
                                                     id=f"{ref.channel}:{ref.id}"),
                                        OWNER, path=tmp, text=text)
            f = row.get("file")
            if isinstance(f, dict) and not (f.get("id") and self.store.file(f["id"])):
                fid = f.get("id") or f"{ref.channel}:{ref.id}"
                self.store.add_file(fid, ref.channel, name, data, mimetype)
                row = self.store.change_message(
                    ref.channel, ref.id, "file",
                    file=self._file_json(fid, name, mimetype, len(data))) or row
            return row
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def web_action(self, channel: str, msg_id: str, action_id: str,
                   selected: str = "") -> Callable[[], None] | None:
        """Check a click against the stored card and build its ActionEvent;
        the value is the one stored with the card, never the browser's."""
        row = self.store.message(channel, msg_id)
        if row is None or row["kind"] not in ("card", "status") or not row["card"]:
            raise _HttpError(404, "no such card")
        if row["kind"] == "status":
            raise _HttpError(400, "the card is already answered")
        card = row["card"]
        value: Any = ""
        source = "button"
        action = next((a for a in card.get("actions") or [] if a.get("id") == action_id), None)
        select = card.get("select") or {}
        if action is not None:
            value = action.get("value", "")
            selected = ""
        elif select and select.get("id") == action_id:
            if selected not in {str(o[1]) for o in select.get("options") or [] if len(o) > 1}:
                raise _HttpError(400, "no such option")
            source = "select"
        else:
            raise _HttpError(400, "no such action")
        ev = ActionEvent(action_id=action_id, user_id=self.owner_id,
                         card=MessageRef(channel, msg_id, row["thread_key"], row["posted_at"]),
                         value=value, source=source, selected=selected,
                         block_id=str(card.get("block_id") or ""), raw={"source": "web"})
        if self.inbound is not None:
            self._hand_in(ev)
            return None
        return lambda: self._dispatch(ev)


def _peer_cap(value: Any) -> int:
    """The web block's `peer_connections`: a whole number from 0 (no cap
    per peer) to MAX_CONNECTIONS; MAX_PEER_CONNECTIONS when unset or not
    one."""
    if isinstance(value, int) and not isinstance(value, bool):
        n = value
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value)
    else:
        return MAX_PEER_CONNECTIONS
    return n if 0 <= n <= MAX_CONNECTIONS else MAX_PEER_CONNECTIONS


class _Server(http.server.ThreadingHTTPServer):
    """One thread per connection, at most MAX_CONNECTIONS at once, and at
    most `max_per_peer` from one address other than loopback: one more is
    closed at once, so a flood of connections cannot use up the process's
    threads, and one client cannot hold every slot."""
    daemon_threads = True
    allow_reuse_address = True
    frontend: WebFrontend

    def __init__(self, addr: tuple, handler: type,
                 max_connections: int = MAX_CONNECTIONS,
                 max_per_peer: int = MAX_PEER_CONNECTIONS) -> None:
        super().__init__(addr, handler)
        self._slots = threading.BoundedSemaphore(max_connections)
        self.max_per_peer = max_per_peer
        self._peers: dict[str, int] = {}
        self._peers_lock = threading.Lock()
        self._peer_warned = 0.0

    def _peer(self, client_address: Any) -> str | None:
        """The address a per-peer count is kept for; None for loopback (the
        relay client, tailscale serve, a proxy on this machine) or with no
        cap."""
        host = str(client_address[0]) if client_address else ""
        if not self.max_per_peer or host in LOOPBACK:
            return None
        return host

    def _take_peer(self, peer: str | None) -> bool:
        if peer is None:
            return True
        with self._peers_lock:
            n = self._peers.get(peer, 0)
            if n >= self.max_per_peer:
                now = time.monotonic()
                if now - self._peer_warned >= 60:
                    self._peer_warned = now
                    log.warning("web chat: %s holds %d connections, the most one address may; "
                                "closing more from it", peer, n)
                return False
            self._peers[peer] = n + 1
        return True

    def _drop_peer(self, peer: str | None) -> None:
        if peer is None:
            return
        with self._peers_lock:
            n = self._peers.get(peer, 0) - 1
            if n > 0:
                self._peers[peer] = n
            else:
                self._peers.pop(peer, None)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        peer = self._peer(client_address)
        if not self._take_peer(peer):
            self._slots.release()
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._drop_peer(peer)
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._drop_peer(self._peer(client_address))
            self._slots.release()


class _Server6(_Server):
    address_family = socket.AF_INET6



# The relay's copy of the app, probed once per RELAY_APP_TTL seconds: a relay
# installed without `--with-app` answers "not found" at its root, and a
# phone sent there would see that error instead of the app.
RELAY_APP_TTL = 600.0
_relay_app_seen: dict[str, tuple[float, bool]] = {}


def relay_serves_app(relay_url: str, *, now: float | None = None) -> bool:
    if not relay_url:
        return False
    now = time.time() if now is None else now
    hit = _relay_app_seen.get(relay_url)
    if hit and now - hit[0] < RELAY_APP_TTL:
        return hit[1]
    from .relay_client import relay_app_url
    ok = False
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(relay_app_url(relay_url) + "index.html", method="GET",
                                     headers={"User-Agent": "tacitus-web-chat"})
        with opener.open(req, timeout=3) as r:
            ok = r.status == 200 and "text/html" in (r.headers.get("Content-Type") or "")
    except Exception:
        ok = False
    _relay_app_seen[relay_url] = (now, ok)
    return ok


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Tacitus"
    sys_version = ""
    server: _Server
    # Socket reads and writes; a stalled client is dropped. An event
    # stream writes a comment every ping_s seconds, well inside it.
    timeout = HANDLER_TIMEOUT_S

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("web chat %s: " + fmt, self.client_address[0], *args)

    @property
    def fe(self) -> WebFrontend:
        return self.server.frontend

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    # responses

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        # No page of another origin (a deck on another port of the host)
        # may load an answer of this one.
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status: int, body: bytes, ctype: str,
              headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _json(self, status: int, obj: Any, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8",
                   (("Cache-Control", "no-store"), *headers))

    # routing

    def _route(self, method: str) -> None:
        after = None
        self._body_read = False
        self._drain_limit = MAX_DRAIN_BYTES
        try:
            parts = urllib.parse.urlsplit(self.path)
            base = self.fe.base_path
            path = parts.path
            if base and path == base:
                loc = base + "/" + (f"?{parts.query}" if parts.query else "")
                self._send(301, b"", "text/plain", (("Location", loc),))
                return
            if not path.startswith(base + "/"):
                raise _HttpError(404, "not found")
            rest = path[len(base):]
            if rest == "/" + web_bundle.MANIFEST_NAME and method == "GET":
                self._manifest()
            elif rest == "/api" or rest.startswith("/api/"):
                segs = [urllib.parse.unquote(s) for s in rest.split("/")[2:]]
                query = urllib.parse.parse_qs(parts.query)
                after = self._api(method, segs, query)
            elif method == "GET":
                self._static(rest)
            else:
                raise _HttpError(404, "not found")
        except _HttpError as e:
            self._drain()
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            log.exception("web chat: %s %s failed", method, self.path)
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass
        self.close_connection = True
        if after is not None:
            after()

    def _api(self, method: str, segs: list[str], query: dict) -> Callable[[], None] | None:
        if segs == ["pair"]:
            self._drain_limit = MAX_PAIR_BYTES
        self._check_host()
        self._check_fetch_site()
        if method == "POST":
            self._check_origin()
        if segs and segs[0] in ("pair", "relay") and self.headers.get("X-Tacitus-Relay") is not None \
                and segs != ["relay", "attest"]:
            # The relay client never forwards these; this is the second lock.
            # A phone's attestation of its app is the one relay route it reaches.
            raise _HttpError(403, "pairing and relay devices are managed in the owner's own web chat")
        if method == "POST" and segs == ["pair"]:
            self._pair()
            return None
        if not self._is_owner():
            raise _HttpError(401, "not the owner")
        fe, store = self.fe, self.fe.store
        n = len(segs)
        if method == "GET":
            if segs == ["me"]:
                self._json(200, {"owner": fe.owner_id, "bot": fe.bot_user_id,
                                 "public_url": fe.public_url or None,
                                 "base_path": fe.base_path, "push": web_push.AVAILABLE,
                                 "relay": fe.relay is not None,
                                 "card_actions": self._relay_cards_allowed(),
                                 "bundle": fe.bundle_hash(),
                                 "settings": sorted(fe.settings_pages)})
                return None
            if segs == ["push", "vapid"]:
                self._json(200, {"key": self._pusher().vapid.public_key})
                return None
            if segs == ["bundle"]:
                # The signed manifest, for an app that reaches the bot through the relay.
                self._manifest()
                return None
            if segs == ["push", "subscriptions"]:
                pusher = self._pusher()
                pusher.prune()
                self._json(200, pusher.store.push_list())
                return None
            if segs == ["channels"]:
                self._json(200, store.channels())
                return None
            if n == 3 and segs[0] == "channels" and segs[2] == "threads":
                self._need_channel(segs[1])
                limit = self._int(query, "limit", 50, 1, 200)
                before = self._int(query, "before", None, 0, None)
                rows = store.threads(segs[1], limit, before)
                store.mark_read(segs[1])
                self._json(200, rows)
                return None
            if n == 4 and segs[0] == "channels" and segs[2] == "threads":
                self._need_channel(segs[1])
                key = store.thread_of(segs[1], segs[3])
                if not key:
                    raise _HttpError(404, "no such thread")
                rows = store.thread(segs[1], key)
                store.mark_read(segs[1])
                self._json(200, rows)
                return None
            if n == 2 and segs[0] == "files":
                self._file(segs[1])
                return None
            if segs == ["events"]:
                self._events(query)
                return None
        if segs and segs[0] == "relay":
            self._relay_api(method, segs[1:])
            return None
        if n == 2 and segs[0] == "settings":
            self._settings(method, segs[1])
            return None
        if method == "POST":
            if n == 3 and segs[0] == "channels" and segs[2] == "messages":
                body = self._read_json()
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise _HttpError(400, "text is required")
                if len(text) > MAX_TEXT_CHARS:
                    raise _HttpError(400, "text is too long")
                row, after = fe.web_message(segs[1], text, self._key(body.get("thread_key")))
                self._json(200, row)
                return after
            if n == 3 and segs[0] == "channels" and segs[2] == "files":
                return self._upload(segs[1])
            if segs == ["push", "subscribe"]:
                self._push_subscribe(self._read_json())
                self._json(200, {"ok": True})
                return None
            if segs == ["push", "unsubscribe"]:
                endpoint = self._read_json().get("endpoint")
                if not isinstance(endpoint, str) or not endpoint:
                    raise _HttpError(400, "endpoint is required")
                self._json(200, {"ok": True,
                                 "removed": self._pusher().store.push_unsubscribe(endpoint)})
                return None
            if segs == ["push", "revoke"]:
                self._refuse_relay_deny("revoke a subscription")
                sub_id = self._read_json().get("id")
                if not isinstance(sub_id, str) or not sub_id:
                    raise _HttpError(400, "id is required")
                self._json(200, {"ok": True,
                                 "removed": self._pusher().store.push_revoke(sub_id)})
                return None
            if n == 4 and segs[0] == "cards" and segs[3] == "actions":
                # The Send and Create buttons: never from a device paired on
                # the relay's origin below "all", whatever its relay allows.
                self._refuse_relay_deny("act on a card")
                body = self._read_json()
                action_id = body.get("action_id")
                if not isinstance(action_id, str) or not action_id:
                    raise _HttpError(400, "action_id is required")
                selected = body.get("selected")
                after = fe.web_action(segs[1], segs[2], action_id,
                                      selected if isinstance(selected, str) else "")
                self._json(200, {"ok": True})
                return after
        raise _HttpError(404, "not found")

    def _settings(self, method: str, name: str) -> None:
        """`/api/settings/<name>`: a settings page the host registered.
        GET answers its data; POST saves the JSON body, which only the
        owner's own web chat may do (never a relayed request)."""
        page = self.fe.settings_pages.get(name)
        if page is None:
            raise _HttpError(404, "no such settings page")
        if method == "GET":
            self._json(200, page.get())
            return
        if self.headers.get("X-Tacitus-Relay") is not None:
            raise _HttpError(403, "settings change only in the owner's own web chat")
        body = self._read_json()
        # Who made the change, for a page that logs it: the credential that
        # signed this request in, and the device a relayed request names.
        who = {"credential": getattr(self, "_credential", "") or "unknown"}
        device = (self.headers.get("X-Tacitus-Relay") or "").strip()
        if device:
            who["device"] = device
        try:
            if "who" in inspect.signature(page.save).parameters:
                out = page.save(body, who=who)
            else:
                out = page.save(body)
        except ValueError as e:
            raise _HttpError(400, str(e)[:500]) from None
        self._json(200, out)

    # the relay's owner routes (docs/webchat-relay.md, For the web front end)

    def _relay_cards_allowed(self) -> bool:
        """False for a request the relay client marks as one that may not
        confirm a card: `X-Tacitus-Relay-Cards: deny`, or a relayed request
        (`X-Tacitus-Relay`) without `allow`. The app reads it in /api/me."""
        cards = (self.headers.get("X-Tacitus-Relay-Cards") or "").strip().lower()
        if self.headers.get("X-Tacitus-Relay") is None and not cards:
            return True
        return cards == "allow"

    def _relay_api(self, method: str, rest: list[str]) -> None:
        """`/api/relay/...`: the owner pairs a phone, lists, approves (and
        chooses whether a relay-origin device may press card buttons) and
        revokes the paired devices and reads the client's state. `_api` has
        refused a relayed request already, except a phone's attestation, so
        the others answer only in the owner's own web chat (the tailnet, or
        this machine). A pairing opens the app on the bot's own origin, or
        with `{"via": "relay"}` on the relay's copy, which checks its code
        against the hash of the signed app bundle the link carries."""
        relay = self.fe.relay
        if method == "GET" and rest == ["status"]:
            self._json(200, {"enabled": relay is not None, **(relay.status() if relay else {}),
                             **(self._pair_ways(relay) if relay else {})})
            return
        if relay is None:
            raise _HttpError(404, "the relay is off")
        if method == "POST" and rest == ["approve"]:
            body = self._read_json()
            device, cards = body.get("device"), body.get("cards", False)
            if not isinstance(device, str) or not _DEVICE_RE.match(device):
                raise _HttpError(400, "device is a 16-character device id")
            if not isinstance(cards, bool):
                raise _HttpError(400, "cards is true or false")
            if not relay.approve(device, cards=cards):
                raise _HttpError(404, "no such device")
            self._json(200, {"ok": True})
            return
        if method == "POST" and rest == ["attest"]:
            self._attest(relay)
            return
        if method == "GET" and rest == ["devices"]:
            self._json(200, relay.devices())
            return
        if method == "POST" and rest == ["pair"]:
            self._relay_pair(relay)
            return
        if method == "POST" and rest == ["revoke"]:
            device = self._read_json().get("device")
            if not isinstance(device, str) or not _DEVICE_RE.match(device):
                raise _HttpError(400, "device is a 16-character device id")
            if not relay.revoke(device):
                raise _HttpError(404, "no such device")
            if web_push.AVAILABLE:
                self.fe.push.prune()  # the removed device's push subscriptions go now
            self._json(200, {"ok": True})
            return
        raise _HttpError(404, "not found")

    def _pair_ways(self, relay: Any) -> dict:
        """How "Pair a phone" can pair: `pair_own`, a link to the bot's own
        origin (public_url); `pair_relay`, a link to the relay's copy of
        the app with the signed bundle's hash (`bundle`); `relay_app`, that
        the relay serves a copy at all (a relay installed without the app
        answers "not found" there, so the dialog hides that way)."""
        from .relay_client import bundle_hash_of
        bundle = bundle_hash_of(self.fe)
        return {"pair_own": relay.can_pair_own(), "pair_relay": bool(bundle),
                "relay_app": relay_serves_app(getattr(relay, "relay_url", "")),
                "bundle": bundle or None}

    def _relay_pair(self, relay: Any) -> None:
        """`POST /api/relay/pair`, `{}` or `{"via": "own" | "relay"}`: a
        one-time pairing link. Through the relay the link opens the
        relay's copy of the app and carries the bundle hash; once approved,
        that device reads and chats (`access`), and presses card buttons
        only when the owner allows them for it."""
        from .relay_client import BUNDLE_ACCESS, RelayError, bundle_hash_of, relay_app_url
        from .relay_crypto import PairingPayload
        sized = self.headers.get("Content-Length") not in (None, "0") \
            or self.headers.get("Transfer-Encoding")
        via = (self._read_json(MAX_PAIR_BYTES) if sized else {}).get("via", "own")
        if via not in ("own", "relay"):
            raise _HttpError(400, 'via is "own" or "relay"')
        bundle = bundle_hash_of(self.fe)
        if via == "relay" and not bundle:
            raise _HttpError(409, "pairing through the relay needs the signed app bundle, "
                                  "which this build does not have")
        if via == "relay" and not relay_serves_app(getattr(relay, "relay_url", "")):
            raise _HttpError(409, "the relay serves no copy of the app, so a phone cannot "
                                  "start there; set the phone up on the tailnet instead")
        try:
            if via == "relay":
                link = relay.new_pairing(app_url=relay_app_url(relay.relay_url),
                                         relay_origin=True, bundle=bundle)
            else:
                link = relay.new_pairing(bundle=bundle)
            expires = PairingPayload.from_string(link).expires
        except RelayError as e:
            raise _HttpError(409, str(e)) from None
        except Exception:
            log.exception("web chat: the relay could not open a pairing")
            raise _HttpError(503, "the relay could not open a pairing") from None
        # The link is the QR code's text; the app draws the code itself.
        self._json(200, {"link": link, "qr": link, "expires": expires, "origin": via,
                         "bundle": bundle or None,
                         "access": BUNDLE_ACCESS if via == "relay" else "all"})

    def _attest(self, relay: Any) -> None:
        """A phone's check of its app (docs/webchat.md, Code integrity): it
        posts, through the relay, the bundle hash it verified. The device's
        record says `bundle_verified`, with the hash and the bot's release,
        when that is the hash the bot signs now; any other hash clears it.
        The record grants nothing: the code that checked came from the
        relay, so only the owner lets a device press card buttons."""
        device = (self.headers.get("X-Tacitus-Relay") or "").strip()
        if not _DEVICE_RE.match(device):
            raise _HttpError(400, "a phone attests its app through the relay")
        bundle = self._read_json().get("bundle")
        if not isinstance(bundle, str) or not _BUNDLE_HASH_RE.match(bundle):
            raise _HttpError(400, "bundle is a sha256 in hex")
        current = self.fe.bundle_hash()
        ok = bool(current) and hmac.compare_digest(bundle, current)
        release = web_bundle.release()
        if not relay.attest(device, bundle if ok else "", release):
            raise _HttpError(404, "no such device")
        if not ok:
            self._json(409, {"error": "not the bundle the bot signs now",
                             "bundle_verified": False})
            return
        self._json(200, {"ok": True, "bundle_verified": True, "release": release})

    def _manifest(self) -> None:
        """The signed manifest of the app's files: public data, never
        cached (docs/webchat.md, Code integrity)."""
        bundle = self.fe.bundle
        if bundle is None:
            raise _HttpError(404, "the app is not signed")
        self._send(200, bundle.body, "application/json; charset=utf-8",
                   (("Cache-Control", "no-store"),))

    # checks and inputs

    def _is_owner(self) -> bool:
        """The owner: the Tailscale login header on a connection that
        tailscaled itself opened (it sets the header and strips one a
        client sent; any other local process could forge it), or the
        pairing cookie."""
        fe = self.fe
        self._credential = ""
        if fe.tailscale_login and fe.trust_tailscale_header \
                and self.client_address[0] in LOOPBACK:
            got = self.headers.get("Tailscale-User-Login") or ""
            if got and hmac.compare_digest(got.encode("utf-8"),
                                           fe.tailscale_login.encode("utf-8")):
                try:
                    local = self.connection.getsockname()
                except OSError:
                    local = None
                if local is not None and fe.peer_is_tailscaled(self.client_address, local):
                    self._credential = "header"
                    return True
        token = fe.pair_token()
        if token:
            cookie = self._cookie(COOKIE)
            if cookie and hmac.compare_digest(cookie.encode("utf-8"), token.encode("utf-8")):
                self._credential = "cookie"
                return True
        return False

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel is not None else ""

    def _check_host(self) -> None:
        """An API request names this server: public_url's host and port, or
        a loopback name with this server's port. Anything else (a page on a
        name rebound to this host) is misdirected."""
        if not self.fe.host_allowed(self.headers.get("Host") or ""):
            raise _HttpError(421, "this server does not answer for that host")

    def _check_fetch_site(self) -> None:
        """A request a page of another origin made (a deck on another port
        of this host is same-site) is refused, a GET too."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            raise _HttpError(403, "not from this app's own pages")

    def _check_origin(self) -> None:
        """A POST carries the exact Origin of public_url (scheme, host and
        port). A loopback client that sends no identity header (the relay
        client, which sets it) may send this server's loopback origin.
        Anything else, a missing Origin too, is refused: the cookie is
        SameSite=Strict, and this is the second lock."""
        got = _origin_key(self.headers.get("Origin") or "")
        if got is None:
            raise _HttpError(403, "a POST must carry this app's Origin")
        if got == self.fe.public_origin():
            return
        if self.client_address[0] in LOOPBACK and "Tailscale-User-Login" not in self.headers \
                and self.fe.loopback_origin(got):
            return
        raise _HttpError(403, "not from this app's origin")

    def _refuse_relay_deny(self, what: str) -> None:
        """403 for a request the relay marks `X-Tacitus-Relay-Cards: deny`
        (a device paired through the relay's origin below "all")."""
        if (self.headers.get("X-Tacitus-Relay-Cards") or "").strip().lower() == "deny":
            raise _HttpError(403, f"a device paired on the relay's origin may not {what}")

    def _need_channel(self, cid: str) -> dict:
        row = self.fe.store.channel(cid)
        if row is None:
            raise _HttpError(404, "no such channel")
        return row

    @staticmethod
    def _int(query: dict, key: str, default: int | None, lo: int,
             hi: int | None) -> int | None:
        vals = query.get(key)
        if not vals:
            return default
        try:
            v = int(vals[0])
        except ValueError:
            raise _HttpError(400, f"{key} must be a number") from None
        if v < lo or (hi is not None and v > hi):
            raise _HttpError(400, f"{key} is out of range")
        return v

    @staticmethod
    def _key(val: Any) -> str | None:
        if val in (None, ""):
            return None
        if not isinstance(val, str):
            raise _HttpError(400, "thread_key must be a string")
        return val

    def _drain(self) -> None:
        """Read a small unread request body before an error answer, so
        closing the socket does not reset the connection under the client's
        read. A body over the drain limit (the pairing route's own cap
        there) is left unread."""
        if self._body_read:
            return
        self._body_read = True
        length = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") or not _LENGTH_RE.fullmatch(length):
            return
        n = int(length)
        if 0 < n <= self._drain_limit:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _read_chunked(self, limit: int) -> bytes:
        """A chunked request body (a proxy may forward one). A chunk size is
        1 to 8 hex digits and nothing else (no sign, no prefix); a chunk
        that would take the body past `limit` is refused before it is read."""
        out = bytearray()
        while True:
            line = self.rfile.readline(1024)
            if not line.endswith(b"\n"):
                raise _HttpError(400, "bad chunked body")
            size_text = line.split(b";", 1)[0].strip()
            if not _CHUNK_SIZE_RE.fullmatch(size_text):
                raise _HttpError(400, "bad chunked body")
            size = int(size_text, 16)
            if size == 0:
                for _ in range(64):
                    if self.rfile.readline(1024) in (b"\r\n", b"\n", b""):
                        return bytes(out)
                raise _HttpError(400, "bad chunked body")
            if len(out) + size > limit:
                raise _HttpError(400, "the body is too large")
            data = self.rfile.read(size)
            if len(data) != size or self.rfile.readline(1024) not in (b"\r\n", b"\n"):
                raise _HttpError(400, "bad chunked body")
            out += data

    def _read_body(self, limit: int) -> bytes:
        self._body_read = True
        te = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if te:
            if te != "chunked":
                raise _HttpError(400, "only a chunked or a plain body is taken")
            return self._read_chunked(limit)
        length = self.headers.get("Content-Length")
        if length is None:
            raise _HttpError(400, "Content-Length is required")
        if not _LENGTH_RE.fullmatch(length.strip()):
            raise _HttpError(400, "bad Content-Length")
        n = int(length.strip())
        if n > limit:
            raise _HttpError(400, "the body is too large")
        return self.rfile.read(n)

    def _read_json(self, limit: int = MAX_JSON_BYTES) -> dict:
        try:
            body = json.loads(self._read_body(limit) or b"{}")
        except ValueError:
            raise _HttpError(400, "the body is not JSON") from None
        if not isinstance(body, dict):
            raise _HttpError(400, "the body must be a JSON object")
        return body

    # routes with bodies of their own

    def _pusher(self) -> web_push.Pusher:
        if not web_push.AVAILABLE:
            raise _HttpError(404, "web push is off: the cryptography package is missing")
        return self.fe.push

    def _push_subscribe(self, body: dict) -> None:
        """Keep a browser's PushSubscription (its toJSON()). The endpoint must
        be on a known push service and the keys well formed. A device paired
        through the relay's origin below "all" may not subscribe: the relay
        serves that page's code, and a subscription outlives the pairing."""
        if (self.headers.get("X-Tacitus-Relay-Cards") or "").lower() == "deny":
            raise _HttpError(403, "not from a device paired on the relay's origin")
        pusher = self._pusher()
        # A token change drops the old subscriptions before this one is kept.
        pusher.prune()
        endpoint = body.get("endpoint")
        keys = body.get("keys")
        if not isinstance(endpoint, str) or not pusher.allow(endpoint):
            raise _HttpError(400, "the endpoint is not a known push service")
        if not isinstance(keys, dict):
            raise _HttpError(400, "keys are required")
        try:
            web_push.check_public(web_push.unb64u(keys.get("p256dh")))
            if len(web_push.unb64u(keys.get("auth"))) != 16:
                raise ValueError("auth")
        except ValueError:
            raise _HttpError(400, "the keys are not a P-256 key and a 16-byte secret") from None
        # The relay device the subscription belongs to: the relay client's
        # mark on a relayed request, else the one the app names (a phone
        # paired for the relay, on the tailnet now). Removing the device
        # drops the subscription.
        device = self.headers.get("X-Tacitus-Relay")
        if device is None and isinstance(body.get("device"), str):
            device = body["device"]
        pusher.store.push_subscribe(endpoint, {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                                    device=device if device and _DEVICE_RE.match(device) else None)

    def _pair(self) -> None:
        """Set the cookie for the right token. The body is read only up to
        MAX_PAIR_BYTES, and one attempt runs at a time, with a pause after a
        wrong token, so guesses come at most one per pause."""
        body = self._read_json(MAX_PAIR_BYTES)
        fe = self.fe
        if not fe._pair_lock.acquire(timeout=PAIR_WAIT_S):
            raise _HttpError(429, "too many pairing attempts; try again later")
        try:
            token = fe.pair_token()
            got = body.get("token")
            if not token or not isinstance(got, str) or not hmac.compare_digest(
                    got.strip().encode("utf-8"), token.encode("utf-8")):
                time.sleep(fe.pair_delay_s)
                raise _HttpError(401, "not the owner")
        finally:
            fe._pair_lock.release()
        attrs = [f"{COOKIE}={token}", f"Path={self.fe.base_path or '/'}", "HttpOnly",
                 "SameSite=Strict", f"Max-Age={COOKIE_MAX_AGE}"]
        if self.fe.public_url.startswith("https:"):
            attrs.append("Secure")
        self._json(200, {"ok": True}, (("Set-Cookie", "; ".join(attrs)),))

    def _upload(self, channel: str) -> Callable[[], None] | None:
        ctype = self.headers.get("Content-Type") or ""
        try:
            fields = parse_multipart(ctype, self._read_body(MAX_UPLOAD_BYTES))
        except ValueError as e:
            raise _HttpError(400, str(e)) from None
        part = fields.get("file")
        if not part or part.get("filename") is None:
            raise _HttpError(400, "a file part is required")

        def text_of(key: str) -> str:
            f = fields.get(key)
            return f["data"].decode("utf-8", "replace") if f else ""
        text = text_of("text")
        if len(text) > MAX_TEXT_CHARS:
            raise _HttpError(400, "text is too long")
        row, after = self.fe.web_upload(channel, part["filename"] or "", part["data"],
                                        part.get("type") or "", text,
                                        self._key(text_of("thread_key").strip() or None))
        self._json(200, row)
        return after

    def _file(self, fid: str) -> None:
        row = self.fe.store.file(fid)
        if not row or not row.get("path"):
            raise _HttpError(404, "no such file")
        try:
            data = Path(row["path"]).read_bytes()
        except OSError:
            raise _HttpError(404, "no such file") from None
        name = row["name"] or "file"
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name) or "file"
        disp = (f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}")
        self._send(200, data, row["mimetype"] or "application/octet-stream",
                   (("Content-Disposition", disp), ("Cache-Control", "private, max-age=3600"),
                    ("Content-Security-Policy", FILE_CSP)))

    def _events(self, query: dict) -> None:
        """The event stream: replay after Last-Event-ID (or ?since=), then
        live events, with a comment line every ping_s seconds. A client that
        closed its end is noticed within GONE_CHECK_S, so streams opened and
        dropped fast (by a relayed device, say) do not hold the connection
        slots until the next ping."""
        raw = self.headers.get("Last-Event-ID") or (query.get("since") or [None])[0]
        since = None
        if raw not in (None, ""):
            try:
                since = max(0, int(raw))
            except ValueError:
                raise _HttpError(400, "since must be a number") from None
        fe, store = self.fe, self.fe.store
        sub = store.subscribe()
        if since is not None:
            # A seq beyond the log is from another store (a reset one): the
            # stream goes on from the log's end, so live events still arrive.
            since = min(since, store.max_event_seq())
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self._common_headers()
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            last = since if since is not None else store.max_event_seq()
            if since is not None:
                while True:
                    batch = store.events_since(last)
                    for item in batch:
                        self._event(item)
                        last = item[0]
                    if len(batch) < 500:
                        break
            self.wfile.flush()
            next_ping = time.monotonic() + fe.ping_s
            while not fe._stop.is_set() and not sub.dropped:
                try:
                    item = sub.q.get(timeout=max(0.01, min(GONE_CHECK_S,
                                                           next_ping - time.monotonic())))
                except queue.Empty:
                    if self._client_gone():
                        break
                    if time.monotonic() >= next_ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        next_ping = time.monotonic() + fe.ping_s
                    continue
                if item is None:
                    break
                if item[0] <= last:
                    continue
                self._event(item)
                self.wfile.flush()
                last = item[0]
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            pass
        except OSError:
            pass
        finally:
            store.unsubscribe(sub)

    def _client_gone(self) -> bool:
        """True when the client closed its end. An event stream's client
        sends nothing after its request, so a readable socket is one at EOF
        (or one that broke)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True

    def _event(self, item: tuple[int, str, str]) -> None:
        seq, typ, payload = item
        self.wfile.write(f"id: {seq}\nevent: {typ}\ndata: {payload}\n\n".encode("utf-8"))

    # static files

    def _static(self, rest: str) -> None:
        root = self.fe.webui_dir
        rel = urllib.parse.unquote(rest.lstrip("/")) or "index.html"
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") or "\\" in p or "\x00" in p
               for p in parts):
            raise _HttpError(404, "not found")
        target = root / rel
        try:
            inside = target.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not (inside and target.is_file()):
            # A path with no file extension is a route inside the app.
            if "." in parts[-1] and rel != "index.html":
                raise _HttpError(404, "not found")
            target, rel = root / "index.html", "index.html"
        if not target.is_file():
            if target.name == "index.html":
                self._send(200, PLACEHOLDER_PAGE, STATIC_TYPES[".html"],
                           (("Cache-Control", "no-cache"),
                            ("Content-Security-Policy", self.fe.html_csp)))
                return
            raise _HttpError(404, "not found")
        ctype = (STATIC_TYPES.get(target.suffix.lower())
                 or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        headers: list[tuple[str, str]] = [("Cache-Control", "no-cache")]
        if ctype.startswith("text/html"):
            headers.append(("Content-Security-Policy", self.fe.html_csp))
        data = target.read_bytes()
        self.fe.note_served(rel, data)  # a file changed since the bot signed it is logged
        self._send(200, data, ctype, tuple(headers))
