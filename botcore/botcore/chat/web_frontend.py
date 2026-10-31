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

Identity, standalone: the owner is "owner", the bot is "bot"; messages
are `w<seq>`, a thread is `t<seq of its first message>`, rooms are
`r<n>`, files `f<n>`; the EA channel is `main` and the DM is `dm`. In
shadow mode (docs/webchat.md, Shadow mode) the composite writes through
the `mirror_*` methods with the primary's ids and the store mints none;
the owner's web input goes to `self.inbound` instead of the registry.

Outbound text is the LLM's Slack mrkdwn, converted the way the Zulip
frontend converts it (chat.text.mrkdwn_to_zulip), so the web app renders
one Markdown subset. A card is stored as `dataclasses.asdict` JSON and
the web app draws it; a click carries only the action id, and the value
the core receives is the one stored with the card.

The HTTP layer is `http.server.ThreadingHTTPServer` on a daemon thread.
Static files come from `webui/` beside this module without auth; every
`/api/` route needs the owner (the Tailscale login header from a loopback
peer, or the pairing cookie). A POST from another origin is refused.
Standard library only, except Web Push (web_push.py), which needs the
`cryptography` package and is off without it: a new card, a bot message
in the DM and a loud alert (`notify`) reach the browsers that turned
notifications on, even with the app closed.
"""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import http.server
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
from typing import Any, Optional

from . import files as _files
from . import web_push
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
COOKIE = "ea_web"
WEBUI_DIR = Path(__file__).resolve().parent / "webui"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8470
DEFAULT_BASE = "/chat"

# Seconds between the comment lines that keep an idle event stream open.
PING_S = 20.0
# Events kept for replay; a browser away longer reloads its view.
EVENT_KEEP_S = 14 * 86400
# Events a stream may fall behind by before it is closed; the browser
# reconnects with Last-Event-ID and replays from the store.
STREAM_QUEUE = 2000
MAX_JSON_BYTES = 1 << 20
MAX_UPLOAD_BYTES = 50 << 20
MAX_TEXT_CHARS = 20000
COOKIE_MAX_AGE = 365 * 86400

KINDS = ("main", "sched", "dm", "room")
EVENT_TYPES = ("message", "update", "reaction", "card", "status", "file", "channel")
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# Emoji shortcodes, held aside while text converts (WebFrontend._out).
_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")
_HELD_RE = re.compile("\x02(\\d+)\x02")

# The web app's pages load only their own files: nothing inline, nothing
# from another origin.
HTML_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
# A stored file opened directly runs nothing.
FILE_CSP = "sandbox; default-src 'none'"

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
    posted_at REAL, updated_at REAL, UNIQUE(channel, id));
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
    failures INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS push_queue(
    tag TEXT PRIMARY KEY, due_at REAL, payload TEXT, urgency TEXT, created_at REAL);
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
    queue sees events in seq order."""

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
            self._conn.commit()
        self._subs: list[_Sub] = []
        self._pending: list[tuple[int, str, str]] = []
        self._emitted = 0

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
        unread = self._one(
            "SELECT COUNT(*) AS n FROM messages WHERE channel=? AND author=? AND seq > MAX("
            "COALESCE((SELECT seq FROM reads WHERE channel=?), 0),"
            "COALESCE((SELECT MAX(seq) FROM messages WHERE channel=? AND author=?), 0))",
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

    def events_since(self, seq: int, limit: int = 500) -> list[tuple[int, str, str]]:
        rows = self._all("SELECT seq, type, payload FROM events WHERE seq > ? ORDER BY seq "
                         "LIMIT ?", (int(seq), int(limit)))
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
            for item in self._pending:
                self._fan_out(item)
            self._emitted += len(self._pending)
            self._pending = []
            if self._emitted >= 1000:
                self._emitted = 0
                self.prune_events()
            return result

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
                    event: str | None = None) -> dict:
        """Insert a message and its event. With no mid the store mints
        `w<seq>`, and with no thread_key the message opens `t<seq>`. Its
        updated_at starts at posted_at; every change raises it (the app
        reads a row whose two times differ as an edit)."""
        now = time.time()

        def run() -> dict:
            cur = self._conn.execute(
                "INSERT INTO messages(channel, id, thread_key, author, kind, text, card, file, "
                "reactions, posted_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (channel, mid, thread_key, author, kind, text or "",
                 json.dumps(card) if card is not None else None,
                 json.dumps(file) if file is not None else None, "[]",
                 posted_at if posted_at is not None else now,
                 posted_at if posted_at is not None else now))
            seq = int(cur.lastrowid)
            if mid is None or thread_key is None:
                self._conn.execute("UPDATE messages SET id=?, thread_key=? WHERE seq=?",
                                   (mid if mid is not None else f"w{seq}",
                                    thread_key if thread_key is not None else f"t{seq}", seq))
            row = self._conn.execute("SELECT * FROM messages WHERE seq=?", (seq,)).fetchone()
            out = self._msg_json(row)
            self._emit(event or ("message" if kind == "message" else kind), channel, row["id"], out)
            return out
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
        row. With no fid the store mints the next `f<n>` under its lock, so
        two uploads never share an id."""
        self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.files_dir / f"incoming-{secrets.token_hex(12)}"
        _write_private(tmp, data)
        try:
            with self._lock:
                if fid is None:
                    fid = f"f{self.next_number('files', 'f')}"
                disk = self.files_dir / hashlib.sha256(fid.encode("utf-8")).hexdigest()[:32]
                os.replace(tmp, disk)
                with self._conn:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO files(id, channel, name, path, mimetype, size, "
                        "at) VALUES (?,?,?,?,?,?,?)",
                        (fid, channel, name, str(disk), mimetype, len(data), time.time()))
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
                       keep: int = web_push.MAX_SUBSCRIPTIONS) -> None:
        """Keep a subscription (a renewed one starts with no failures); past
        `keep`, the ones that have gone longest without a delivery go."""
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO push_subscriptions(endpoint, keys, created_at, last_ok, failures) "
                    "VALUES (?,?,?,NULL,0) ON CONFLICT(endpoint) DO UPDATE SET "
                    "keys=excluded.keys, failures=0", (endpoint, json.dumps(keys), now))
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
                 webui_dir: str | Path | None = None) -> None:
        super().__init__()
        cfg = dict(cfg or {})
        self.cfg = cfg
        self.bind = str(cfg.get("bind") or DEFAULT_BIND)
        port = cfg.get("port")
        self.port = int(port) if port not in (None, "") else DEFAULT_PORT
        self.base_path = _norm_base(cfg.get("base_path"))
        self.public_url = str(cfg.get("public_url") or "").rstrip("/")
        self.tailscale_login = str(cfg.get("tailscale_login") or "").strip()
        self.store_path = Path(store_path)
        self.pair_token_file = Path(cfg.get("pair_token_file")
                                    or self.store_path.parent / PAIR_FILE)
        self.webui_dir = Path(webui_dir) if webui_dir else WEBUI_DIR
        self.push_subject = str(cfg.get("push_subject") or "").strip() or web_push.DEFAULT_SUBJECT
        self.owner_id = OWNER
        self.bot_user_id = BOT
        self.main_channel = MAIN_ID
        self.main_channel_name = MAIN_NAME
        self.sched_channel = ""
        # Shadow mode: the composite takes the owner's web input here
        # (docs/webchat.md); None dispatches it through self.registry.
        self.inbound: Inbound | None = None
        self.ping_s = PING_S
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
        self._stop = threading.Event()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_config(cls, cfg: dict | None, *, store_path: str | Path | None = None,
                    webui_dir: str | Path | None = None) -> WebFrontend:
        """Build one from the chat block's `web` section. The store is
        `store_path`, else the block's `store_path`; ChatError with
        neither."""
        cfg = dict(cfg or {})
        path = store_path or cfg.get("store_path")
        if not path:
            raise ChatError("web chat: no store path")
        return cls(cfg, store_path=path, webui_dir=webui_dir)

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
                                             subject=self.push_subject)
            return self._push

    # lifecycle

    def start(self) -> None:
        """Open the store, make the standalone channels (unless a composite
        drives this frontend), write the pairing token if there is none, and
        serve HTTP on a daemon thread. Idempotent."""
        with self._life_lock:
            if self._server is not None:
                return
            store = self.store
            store.prune_events()
            if self.inbound is None:
                self._ensure_own_channels()
            self._ensure_pair_token()
            cls = _Server6 if ":" in self.bind else _Server
            srv = cls((self.bind, self.port), _Handler)
            srv.frontend = self
            self.port = int(srv.server_address[1])
            self._stop.clear()
            self._server = srv
            self._thread = threading.Thread(target=srv.serve_forever,
                                            kwargs={"poll_interval": self.poll_s},
                                            name="webchat-http", daemon=True)
            self._thread.start()
            if web_push.AVAILABLE:
                self.push.start()
            log.info("web chat serving on %s:%d%s/", self.bind, self.port, self.base_path)

    def run_forever(self) -> None:
        """Block until stop(); the server runs on its own thread."""
        while not self._stop.wait(1.0):
            pass

    def stop(self) -> None:
        """Stop serving and close every event stream."""
        self._stop.set()
        with self._life_lock:
            srv, self._server = self._server, None
        if self._store is not None:
            self._store.close_subscribers()
        if self._push is not None:
            self._push.stop()
        if srv is not None:
            srv.shutdown()
            srv.server_close()

    def ping(self) -> bool:
        if not self.store.ok():
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
        when there is none."""
        try:
            return self.pair_token_file.read_text().strip()
        except OSError:
            return ""

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
        """A new card is pushed at high urgency: its title, then its first
        line (a line of the card, a field, else the body)."""
        if not row:
            return
        first = [*(card.lines or []), *(f"{f.label}: {f.value}" for f in card.fields or []),
                 card.body or ""]
        body = next((ln for ln in first if web_push.plain_line(ln)), "")
        self.notify(card.title or "Confirm", body,
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

    def mirror_channel(self, id: str, name: str, kind: str, archived: bool = False) -> dict:
        """Write a channel row as the primary describes it."""
        return self.store.upsert_channel(id, name, kind, archived)

    def _mirror_upsert(self, ref: MessageRef, event: str, *, author: str, kind: str,
                       text: str, card: dict | None = None, file: dict | None = None,
                       set_kind: bool = True) -> dict:
        """Insert the message under the primary's ref, or bring the stored
        row in line and append `event` when anything changed. `kind` applies
        to a stored row only with set_kind."""
        self._ensure_channel(ref.channel)
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

    def mirror_post(self, ref: MessageRef, text: str, author: str = BOT) -> dict:
        """Store a message the primary posted, under its ref. The bot's text
        is converted as post() converts it; the owner's is kept as typed."""
        author = OWNER if author == OWNER else BOT
        out = self._out(text) if author == BOT else (text or "")
        new = self.store.message(ref.channel, ref.id) is None
        row = self._mirror_upsert(ref, "update", author=author, kind="message", text=out,
                                  set_kind=False)
        if new:
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
                    path: str | Path | None = None, text: str = "") -> dict:
        """Store a file message the primary posted. With `path` the bytes are
        kept too, under the primary's file id, and the web app downloads
        them from this server; without it the file has no id here and its
        link is the primary's absolute `file.url`."""
        author = OWNER if author == OWNER else BOT
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
        out = self._out(text) if author == BOT else (text or "")
        return self._mirror_upsert(ref, "file", author=author, kind="file", text=out, file=fj)

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


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    frontend: WebFrontend


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Tacitus"
    sys_version = ""
    server: _Server

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
            if rest == "/api" or rest.startswith("/api/"):
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
        if method == "POST":
            self._check_origin()
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
                                 "base_path": fe.base_path, "push": web_push.AVAILABLE})
                return None
            if segs == ["push", "vapid"]:
                self._json(200, {"key": self._pusher().vapid.public_key})
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
            if n == 4 and segs[0] == "cards" and segs[3] == "actions":
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

    # checks and inputs

    def _is_owner(self) -> bool:
        fe = self.fe
        if fe.tailscale_login and self.client_address[0] in LOOPBACK:
            got = self.headers.get("Tailscale-User-Login") or ""
            if got and hmac.compare_digest(got.encode("utf-8"),
                                           fe.tailscale_login.encode("utf-8")):
                return True
        token = fe.pair_token()
        if token:
            cookie = self._cookie(COOKIE)
            if cookie and hmac.compare_digest(cookie.encode("utf-8"), token.encode("utf-8")):
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

    def _check_origin(self) -> None:
        """A POST from a page on another origin is refused: the cookie is
        SameSite=Strict, and this is the second lock."""
        origin = self.headers.get("Origin")
        if origin is None:
            return
        allowed = {self.headers.get("Host") or ""}
        if self.client_address[0] in LOOPBACK:
            allowed.add(self.headers.get("X-Forwarded-Host") or "")
        if self.fe.public_url:
            allowed.add(urllib.parse.urlsplit(self.fe.public_url).netloc)
        if urllib.parse.urlsplit(origin).netloc not in allowed - {""}:
            raise _HttpError(401, "not the owner")

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
        """Read an unread request body before an error answer, so closing
        the socket does not reset the connection under the client's read."""
        if self._body_read:
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= MAX_UPLOAD_BYTES:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _read_chunked(self, limit: int) -> bytes:
        """A chunked request body (a proxy may forward one)."""
        out = bytearray()
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip() or b"x", 16)
            except ValueError:
                raise _HttpError(400, "bad chunked body") from None
            if size == 0:
                while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(out)
            if len(out) + size > limit:
                raise _HttpError(400, "the body is too large")
            out += self.rfile.read(size)
            self.rfile.readline(1024)

    def _read_body(self, limit: int) -> bytes:
        self._body_read = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._read_chunked(limit)
        length = self.headers.get("Content-Length")
        if length is None:
            raise _HttpError(400, "Content-Length is required")
        try:
            n = int(length)
        except ValueError:
            raise _HttpError(400, "bad Content-Length") from None
        if n < 0 or n > limit:
            raise _HttpError(400, "the body is too large")
        return self.rfile.read(n)

    def _read_json(self) -> dict:
        try:
            body = json.loads(self._read_body(MAX_JSON_BYTES) or b"{}")
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
        pusher.store.push_subscribe(endpoint, {"p256dh": keys["p256dh"], "auth": keys["auth"]})

    def _pair(self) -> None:
        body = self._read_json()
        token = self.fe.pair_token()
        got = body.get("token")
        if not token or not isinstance(got, str) or not hmac.compare_digest(
                got.strip().encode("utf-8"), token.encode("utf-8")):
            time.sleep(self.fe.pair_delay_s)
            raise _HttpError(401, "not the owner")
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
        live events, with a comment line every ping_s seconds."""
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
            while not fe._stop.is_set() and not sub.dropped:
                try:
                    item = sub.q.get(timeout=fe.ping_s)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
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
            target = root / "index.html"
        if not target.is_file():
            if target.name == "index.html":
                self._send(200, PLACEHOLDER_PAGE, STATIC_TYPES[".html"],
                           (("Cache-Control", "no-cache"), ("Content-Security-Policy", HTML_CSP)))
                return
            raise _HttpError(404, "not found")
        ctype = (STATIC_TYPES.get(target.suffix.lower())
                 or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        headers: list[tuple[str, str]] = [("Cache-Control", "no-cache")]
        if ctype.startswith("text/html"):
            headers.append(("Content-Security-Policy", HTML_CSP))
        self._send(200, target.read_bytes(), ctype, tuple(headers))
