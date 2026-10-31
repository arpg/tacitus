#!/usr/bin/env python3
"""The Tacitus web chat relay (docs/webchat-relay.md).

A self-hosted bot opens an outbound long-poll to this server. The owner's
phone loads the web app from here (or from the bot's own tailnet origin)
and sends every API call and its event stream through here. The relay
matches the two by install id and forwards frames it cannot read: they
are sealed end to end between the phone and the bot.

It keeps one in-memory route per online install (the connection, its
queues, its counters), writes nothing to disk and logs no frame content.
A restart drops every route; bots reconnect and phones say hello again.

Routes:
  POST /r/{install}/bot   the bot: posts its frames, long-polls for the phone's
  POST /r/{install}/c     one phone frame (hello, req, ping); waits for the answer
  POST /r/{install}/sse   the phone's `open` frame; answers a one-time ticket
  GET  /r/{install}/sse   ?t=<ticket>: the phone's event stream
  GET  /healthz           liveness and counts, for the operator's monitor
  GET  /...               the static web app from --static

A bot proves its install id with a relay token whose hash is the id, so
the relay needs no table of installs. A phone shows the client key the
bot's token derives, in the X-Relay-Key header; the relay checks it before
it reads a body, and answers a wrong key as it answers an install that is
offline. The event stream's URL carries only a ticket that works once
and for 30 seconds, so a proxy's access log holds no key and no frame.
With --allow-file, only the listed install ids are served (the hosted
tier's activation list). Without one, a source address may open a few
new installs a minute, and a full table makes room by dropping the
longest-idle install that holds no poll.

Standard library only. Run it behind a TLS terminator (Caddy, nginx) that
does not buffer the event streams.

Usage:
  relay_server.py [--bind 127.0.0.1] [--port 8471] [--static DIR]
                  [--allow-file FILE] [--log-file FILE] [--max-installs N]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import queue
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

ROUTE_RE = re.compile(r"^/r/([a-z2-7]{26})/(bot|c|sse)$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
B64_RE = re.compile(r"^[A-Za-z0-9_-]*$")
CLIENT_KINDS = frozenset({"hello", "req", "ping"})
BOT_KINDS = frozenset({"welcome", "res", "sse", "end", "err"})
ERR_CODES = frozenset({"session", "auth", "replay", "bad", "busy", "offline", "timeout"})
# Kinds whose frame is small: a cap of their own, so no large body passes as one.
SMALL_KINDS = frozenset({"hello", "ping", "open"})
TICKET_RE = re.compile(r"^[0-9a-f]{32}$")
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# One answer for an install that is offline and for a wrong client key, so
# a stranger who knows an install id cannot tell whether it is online.
UNAVAILABLE = (503, {"error": "unavailable"})
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Relay-Key",
        "Access-Control-Max-Age": "600"}
STATIC_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; connect-src 'self'; "
                                "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}
EXTRA_TYPES = {".webmanifest": "application/manifest+json", ".js": "text/javascript",
               ".mjs": "text/javascript", ".svg": "image/svg+xml"}


def b64d(text: str) -> bytes:
    if not isinstance(text, str) or not B64_RE.match(text):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:
        raise ValueError("not base64url") from exc


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# The same derivations as botcore.chat.relay_crypto (tests/test_relay.py
# checks they agree); this file imports nothing from the repository so it
# deploys alone.
def install_id_for(token: bytes) -> str:
    digest = hashlib.sha256(b"tacitus-relay install\x00" + token).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:26]


def client_key_for(token: bytes) -> str:
    return b64e(hmac.new(token, b"tacitus-relay client", hashlib.sha256).digest())


@dataclass
class Limits:
    """Per-install and global bounds; every one is a plain number."""
    max_frame: int = 16 << 20         # one frame's ciphertext (base64), bytes
    max_inbox_frames: int = 64        # phone frames waiting for the bot
    max_inbox_bytes: int = 32 << 20
    max_waiting: int = 32             # phone requests waiting for an answer
    max_streams: int = 4              # open event streams
    max_polls: int = 4                # concurrent bot long-polls
    rate: float = 20.0                # phone frames per second, refilled
    burst: int = 100
    client_timeout: float = 30.0      # how long /c waits for the bot
    max_poll_wait: float = 25.0
    offline_after: float = 60.0       # no poll for this long: the bot is gone
    keepalive: float = 20.0           # comment line on an idle event stream
    stream_queue: int = 256
    max_installs: int = 1000
    max_total_bytes: int = 512 << 20
    max_connections: int = 2048
    max_small_frame: int = 4096       # a hello, ping or open frame, bytes
    ticket_ttl: float = 30.0          # an event stream ticket's life
    max_tickets: int = 8              # unredeemed tickets per install
    new_route_rate: float = 0.1       # new installs per second per source address
    new_route_burst: int = 20
    max_sources: int = 10000          # source addresses tracked for that rate


def clean_frame(obj: Any, kinds: frozenset) -> Optional[dict]:
    """The frame with only the fields the protocol has, each checked; None
    when it is not one. The relay carries nothing else in the clear."""
    if not isinstance(obj, dict):
        return None
    fid, kind = obj.get("id"), obj.get("k")
    if not isinstance(fid, str) or not ID_RE.match(fid) or kind not in kinds:
        return None
    out: dict = {"id": fid, "k": kind}
    sid = obj.get("sid")
    if sid is not None:
        if not isinstance(sid, str) or not ID_RE.match(sid):
            return None
        out["sid"] = sid
    n = obj.get("n")
    if n is not None:
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < 1 << 63:
            return None
        out["n"] = n
    c = obj.get("c")
    if c is not None:
        if not isinstance(c, str) or not B64_RE.match(c):
            return None
        out["c"] = c
    if kind == "err":
        out["e"] = obj.get("e") if obj.get("e") in ERR_CODES else "bad"
    return out


def _size(frame: dict) -> int:
    return len(frame.get("c", "")) + 160


class Waiter:
    __slots__ = ("frame",)

    def __init__(self) -> None:
        self.frame: Optional[dict] = None


class Stream:
    def __init__(self, sid: str, maxsize: int) -> None:
        self.id = sid
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize)
        self.closed = False


class Route:
    """One online install: its queues and counters, nothing else."""

    def __init__(self, install: str, client_key: str, lock: threading.Lock,
                 limits: Limits, now: float) -> None:
        self.install = install
        self.client_key = client_key
        self.cond = threading.Condition(lock)
        self.last_poll = now
        self.polls = 0
        self.inbox: "deque[tuple[dict, int]]" = deque()
        self.inbox_bytes = 0
        self.waiting: dict[str, Waiter] = {}
        self.streams: "OrderedDict[str, Stream]" = OrderedDict()
        self.tokens = float(limits.burst)
        self.refill_at = now
        # Event stream tickets: ticket -> (the open frame, its expiry).
        self.tickets: "OrderedDict[str, tuple[dict, float]]" = OrderedDict()
        # Whether a phone frame ever came: an install never used goes first
        # when the table is full.
        self.used = False


class Relay:
    """The routing table and its rules, apart from HTTP so tests drive it."""

    def __init__(self, limits: Optional[Limits] = None, *, allow_file: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None,
                 tap: Optional[Callable[[str, dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits or Limits()
        self.allow_file = Path(allow_file) if allow_file else None
        self._allow: frozenset = frozenset()
        self._allow_stamp: Any = None
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.tap = tap
        self.clock = clock
        self.lock = threading.Lock()
        self.routes: dict[str, Route] = {}
        self.total_bytes = 0
        # Per source address: [tokens, refilled at], for opening new routes.
        self._sources: dict[str, list[float]] = {}

    # activation

    def allowed(self, install: str) -> bool:
        """True when no allow file is set or it lists `install`."""
        if self.allow_file is None:
            return True
        try:
            st = self.allow_file.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            if stamp != self._allow_stamp:
                ids = set()
                for line in self.allow_file.read_text().splitlines():
                    word = line.split("#", 1)[0].strip()
                    if word:
                        ids.add(word)
                self._allow, self._allow_stamp = frozenset(ids), stamp
        except OSError:
            return False
        return install in self._allow

    # helpers, called with the lock held

    def _tap(self, direction: str, frame: dict) -> None:
        if self.tap is not None:
            self.tap(direction, frame)

    def _online(self, install: str, now: float) -> Optional[Route]:
        route = self.routes.get(install)
        if route is None:
            return None
        if route.polls == 0 and now - route.last_poll > self.limits.offline_after:
            self._drop(install, route)
            return None
        return route

    def _rate_ok(self, route: Route, now: float) -> bool:
        lim = self.limits
        route.tokens = min(float(lim.burst), route.tokens + (now - route.refill_at) * lim.rate)
        route.refill_at = now
        if route.tokens < 1.0:
            return False
        route.tokens -= 1.0
        return True

    def _room(self, route: Route, size: int) -> bool:
        lim = self.limits
        return (len(route.inbox) < lim.max_inbox_frames
                and route.inbox_bytes + size <= lim.max_inbox_bytes
                and self.total_bytes + size <= lim.max_total_bytes)

    def _enqueue(self, route: Route, frame: dict) -> None:
        size = _size(frame)
        route.inbox.append((frame, size))
        route.inbox_bytes += size
        self.total_bytes += size
        route.cond.notify_all()

    def _unqueue(self, route: Route, fid: str) -> None:
        for item in list(route.inbox):
            if item[0]["id"] == fid:
                route.inbox.remove(item)
                route.inbox_bytes -= item[1]
                self.total_bytes -= item[1]

    def _take(self, route: Route) -> list[dict]:
        out, budget = [], 2 * self.limits.max_frame
        while route.inbox and len(out) < 32:
            frame, size = route.inbox[0]
            if out and size > budget:
                break
            route.inbox.popleft()
            route.inbox_bytes -= size
            self.total_bytes -= size
            budget -= size
            out.append(frame)
        return out

    def _end_stream(self, route: Route, st: Stream, code: str) -> None:
        st.closed = True
        try:
            st.q.put_nowait({"id": st.id, "k": "err", "e": code})
        except queue.Full:
            pass
        if route.streams.get(st.id) is st:
            del route.streams[st.id]

    def _drop(self, install: str, route: Route) -> None:
        for fid, w in route.waiting.items():
            if w.frame is None:
                w.frame = {"id": fid, "k": "err", "e": "offline"}
        for st in list(route.streams.values()):
            self._end_stream(route, st, "offline")
        self.total_bytes -= route.inbox_bytes
        route.inbox.clear()
        route.inbox_bytes = 0
        if self.routes.get(install) is route:
            del self.routes[install]
        route.cond.notify_all()

    def _deliver(self, route: Route, frame: dict) -> None:
        fid, kind = frame["id"], frame["k"]
        w = route.waiting.get(fid)
        if w is not None and kind in ("welcome", "res", "err"):
            w.frame = frame
            route.cond.notify_all()
            return
        st = route.streams.get(fid)
        if st is not None and kind in ("sse", "end", "err"):
            try:
                st.q.put_nowait(frame)
            except queue.Full:  # a phone that does not read: end its stream
                self._end_stream(route, st, "busy")
                self._enqueue(route, {"id": fid, "k": "close"})
                return
            if kind in ("end", "err"):
                st.closed = True
                del route.streams[fid]

    # the three endpoints

    def _source_ok(self, source: str, now: float) -> bool:
        """Whether `source` may open another route now (a token bucket per
        address); called with the lock held."""
        lim = self.limits
        if len(self._sources) > lim.max_sources:
            full = lim.new_route_burst / max(lim.new_route_rate, 1e-9)
            for key in [k for k, (_, t) in self._sources.items() if now - t > full]:
                del self._sources[key]
            if len(self._sources) > lim.max_sources:
                self._sources.clear()
        tokens, at = self._sources.get(source, (float(lim.new_route_burst), now))
        tokens = min(float(lim.new_route_burst), tokens + (now - at) * lim.new_route_rate)
        if tokens < 1.0:
            self._sources[source] = [tokens, now]
            return False
        self._sources[source] = [tokens - 1.0, now]
        return True

    def _make_room(self) -> bool:
        """With the table full, drop the idle route (no poll open) that saw
        no phone frame, else the one idle longest; False when every route
        holds a poll. Called with the lock held."""
        idle = [(r.used, r.last_poll, i) for i, r in self.routes.items() if r.polls == 0]
        if not idle:
            return False
        _, _, install = min(idle)
        self._drop(install, self.routes[install])
        return True

    def bot_exchange(self, install: str, auth: str, body: Any,
                     source: str = "") -> tuple[int, dict]:
        """The bot posts its frames and long-polls for the phone's. `source`
        is the bot's address, which only meters how fast it opens routes."""
        token_text = auth[7:].strip() if auth.startswith("Bearer ") else ""
        try:
            token = b64d(token_text) if token_text else b""
        except ValueError:
            token = b""
        if len(token) < 16:
            return 401, {"error": "bot token required"}
        if not hmac.compare_digest(install_id_for(token), install):
            return 403, {"error": "token does not match install"}
        if not self.allowed(install):
            return 403, {"error": "install not activated on this relay"}
        if not isinstance(body, dict) or not isinstance(body.get("frames", []), list) \
                or len(body.get("frames", [])) > 64:
            return 400, {"error": "bad poll"}
        try:
            wait = max(0.0, min(float(body.get("wait", 0)), self.limits.max_poll_wait))
        except (TypeError, ValueError):
            return 400, {"error": "bad wait"}
        lim = self.limits
        with self.lock:
            now = self.clock()
            route = self._online(install, now)
            if route is None:
                if not self._source_ok(source, now):
                    return 429, {"error": "too many new installs from this address"}
                if len(self.routes) >= lim.max_installs and not self._make_room():
                    return 503, {"error": "relay full"}
                route = Route(install, client_key_for(token), self.lock, lim, now)
                self.routes[install] = route
            route.last_poll = now
            for raw in body.get("frames", []):
                frame = clean_frame(raw, BOT_KINDS)
                if frame is not None and _size(frame) <= lim.max_frame + 160:
                    self._tap("b2c", frame)
                    self._deliver(route, frame)
            if route.polls >= lim.max_polls:
                wait = 0.0
            route.polls += 1
            try:
                deadline = now + wait
                while not route.inbox and self.routes.get(install) is route:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        break
                    route.cond.wait(remaining)
                out = self._take(route)
                route.last_poll = self.clock()
            finally:
                route.polls -= 1
        return 200, {"frames": out}

    def _keyed(self, install: str, key: Any, now: float) -> Optional[Route]:
        """The install's route when it is online and `key` is its client
        key; None for either failure alike. Called with the lock held."""
        route = self._online(install, now)
        if route is None or not isinstance(key, str) or not hmac.compare_digest(
                key.encode(), route.client_key.encode()):
            return None
        return route

    def check_key(self, install: str, key: Any) -> tuple[int, dict]:
        """The check the HTTP layer makes from the headers alone, before it
        reads a phone's body: (200, {}) or the unavailable answer."""
        with self.lock:
            return (200, {}) if self._keyed(install, key, self.clock()) else UNAVAILABLE

    def _admit(self, install: str, key: str, frame: Optional[dict],
               now: float) -> tuple[Optional[Route], int, dict]:
        """Checks shared by /c and /sse, with the lock held."""
        if frame is None:
            return None, 400, {"error": "bad frame"}
        if _size(frame) > self.limits.max_frame + 160 or (
                frame["k"] in SMALL_KINDS and _size(frame) > self.limits.max_small_frame):
            return None, 413, {"error": "frame too large"}
        route = self._keyed(install, key, now)
        if route is None:
            return (None, *UNAVAILABLE)
        route.used = True
        if not self._rate_ok(route, now):
            return None, 429, {"error": "rate limited"}
        if frame["id"] in route.waiting or frame["id"] in route.streams:
            return None, 409, {"error": "frame id in use"}
        if not self._room(route, _size(frame)):
            return None, 503, {"error": "queue full"}
        return route, 200, {}

    def client_request(self, install: str, key: str, obj: Any) -> tuple[int, dict]:
        """One phone frame; the bot's answer frame, or an error."""
        frame = clean_frame(obj, CLIENT_KINDS)
        with self.lock:
            now = self.clock()
            route, status, err = self._admit(install, key, frame, now)
            if route is None:
                return status, err
            assert frame is not None
            if len(route.waiting) >= self.limits.max_waiting:
                return 429, {"error": "too many requests waiting"}
            fid = frame["id"]
            w = Waiter()
            route.waiting[fid] = w
            self._tap("c2b", frame)
            self._enqueue(route, frame)
            deadline = now + self.limits.client_timeout
            try:
                while w.frame is None:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        break
                    route.cond.wait(remaining)
            finally:
                route.waiting.pop(fid, None)
                if w.frame is None:
                    self._unqueue(route, fid)
        if w.frame is None:
            return 504, {"id": fid, "k": "err", "e": "timeout"}
        return 200, w.frame

    def stream_ticket(self, install: str, key: str, obj: Any) -> tuple[int, dict]:
        """Take a phone's `open` frame and answer a ticket for the stream:
        it works once, within `ticket_ttl` seconds. The frame reaches the
        bot when the ticket is used."""
        frame = clean_frame(obj, frozenset({"open"}))
        with self.lock:
            now = self.clock()
            route, status, err = self._admit(install, key, frame, now)
            if route is None:
                return status, err
            assert frame is not None
            for t in [t for t, (_, exp) in route.tickets.items() if exp <= now]:
                del route.tickets[t]
            while len(route.tickets) >= self.limits.max_tickets:
                route.tickets.popitem(last=False)
            ticket = secrets.token_hex(16)
            route.tickets[ticket] = (frame, now + self.limits.ticket_ttl)
        return 200, {"ticket": ticket, "ttl": self.limits.ticket_ttl}

    def open_stream(self, install: str, ticket: Any) -> tuple[int, Any]:
        """Spend a ticket on a phone event stream; (200, Stream) or an
        error. An unknown, used or late ticket gets the unavailable answer."""
        with self.lock:
            now = self.clock()
            route = self._online(install, now)
            entry = None
            if route is not None and isinstance(ticket, str) and TICKET_RE.match(ticket):
                entry = route.tickets.pop(ticket, None)
            if route is None or entry is None or entry[1] <= now:
                return UNAVAILABLE
            frame = entry[0]
            if frame["id"] in route.waiting or frame["id"] in route.streams:
                return 409, {"error": "frame id in use"}
            if not self._room(route, _size(frame)):
                return 503, {"error": "queue full"}
            while len(route.streams) >= self.limits.max_streams:
                oldest = next(iter(route.streams.values()))
                self._end_stream(route, oldest, "busy")
                self._enqueue(route, {"id": oldest.id, "k": "close"})
            st = Stream(frame["id"], self.limits.stream_queue)
            route.streams[st.id] = st
            self._tap("c2b", frame)
            self._enqueue(route, frame)
        return 200, st

    def close_stream(self, install: str, st: Stream) -> None:
        """The phone went away: forget the stream and tell the bot."""
        with self.lock:
            route = self.routes.get(install)
            if route is not None and route.streams.get(st.id) is st:
                del route.streams[st.id]
                self._enqueue(route, {"id": st.id, "k": "close"})

    def reap(self) -> int:
        """Drop the routes whose bot stopped polling; how many went."""
        with self.lock:
            now = self.clock()
            gone = [i for i, r in self.routes.items()
                    if r.polls == 0 and now - r.last_poll > self.limits.offline_after]
            for install in gone:
                self._drop(install, self.routes[install])
        return len(gone)

    def snapshot(self) -> dict:
        """Everything the relay holds, as JSON-able data."""
        with self.lock:
            return {"total_bytes": self.total_bytes, "routes": {
                i: {"last_poll": r.last_poll, "polls": r.polls,
                    "inbox": [f for f, _ in r.inbox], "waiting": sorted(r.waiting),
                    "streams": list(r.streams)} for i, r in self.routes.items()}}

    def counts(self) -> dict:
        with self.lock:
            return {"installs": len(self.routes),
                    "streams": sum(len(r.streams) for r in self.routes.values()),
                    "queued_bytes": self.total_bytes}


# ── HTTP ──────────────────────────────────────────────────────────────

class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tacitus-relay/1"
    sys_version = ""
    timeout = 30  # socket reads and writes; a stalled phone is dropped

    server: "RelayHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        """The default access line carries the query string (a frame); the
        relay writes its own line instead (`_log`)."""

    def _log(self, kind: str, install: str, status: int, n_in: int, n_out: int,
             t0: float) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ms = int((time.monotonic() - t0) * 1000)
        self.server.relay.log(f"{stamp} {self.command} {kind} {install or '-'} {status} "
                              f"in={n_in} out={n_out} {ms}ms")

    def _send_json(self, status: int, obj: Any, cors: bool = False) -> int:
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (CORS.items() if cors else ()):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        return len(body)

    def _source(self) -> str:
        """The client's address, for the new-route meter only (never
        logged). Behind the TLS terminator on this host (a loopback peer)
        it is the last X-Forwarded-For entry, the one the terminator added."""
        peer = str(self.client_address[0])
        if peer in LOOPBACK:
            last = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if last:
                return last
        return peer

    def _read_json(self, cors: bool, limit: Optional[int] = None) -> tuple[Any, int]:
        """(parsed body, its size), or (None, -1) with the error sent."""
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._send_json(411, {"error": "content length required"}, cors)
            self.close_connection = True
            return None, -1
        n = int(length)
        if n > (limit if limit is not None else self.server.relay.limits.max_frame + 4096):
            self._send_json(413, {"error": "too large"}, cors)
            self.close_connection = True
            return None, -1
        raw = self.rfile.read(n)
        try:
            return json.loads(raw), n
        except ValueError:
            self._send_json(400, {"error": "not JSON"}, cors)
            return None, -1

    def do_OPTIONS(self) -> None:
        m = ROUTE_RE.match(urlsplit(self.path).path)
        if not m or m.group(2) == "bot":
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(204)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        t0 = time.monotonic()
        m = ROUTE_RE.match(urlsplit(self.path).path)
        if not m:
            self._send_json(404, {"error": "not found"})
            self.close_connection = True
            return
        install, kind = m.group(1), m.group(2)
        relay = self.server.relay
        cors = kind != "bot"
        key = self.headers.get("X-Relay-Key", "")
        if cors:
            # A phone's key is checked from the headers before its body is
            # read: a stranger never makes the relay read or parse one.
            status, err = relay.check_key(install, key)
            if status != 200:
                self.close_connection = True
                n_out = self._send_json(status, err, cors)
                self._log(kind, install, status, 0, n_out, t0)
                return
        limit = relay.limits.max_small_frame + 1024 if kind == "sse" else None
        body, n_in = self._read_json(cors, limit)
        if n_in < 0:
            self._log(kind, install, 400, 0, 0, t0)
            return
        if kind == "bot":
            status, out = relay.bot_exchange(install, self.headers.get("Authorization", ""),
                                             body, self._source())
        elif kind == "sse":
            status, out = relay.stream_ticket(install, key, body)
        else:
            status, out = relay.client_request(install, key, body)
        n_out = self._send_json(status, out, cors)
        self._log(kind, install, status, n_in, n_out, t0)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self._send_json(200, {"ok": True, **self.server.relay.counts()})
            return
        m = ROUTE_RE.match(url.path)
        if m:
            if m.group(2) == "sse":
                self._sse(m.group(1), url.query)
            else:
                self._send_json(405, {"error": "method not allowed"})
            return
        self._static(url.path)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))

    def _sse(self, install: str, query: str) -> None:
        t0 = time.monotonic()
        relay = self.server.relay
        status, st = relay.open_stream(install, parse_qs(query).get("t", [""])[0])
        if status != 200:
            self._send_json(status, st, cors=True)
            self._log("sse", install, status, 0, 0, t0)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True
        n_out = 0
        try:
            self._chunk(b": open\n\n")
            while True:
                try:
                    fr = st.q.get(timeout=relay.limits.keepalive)
                except queue.Empty:
                    if st.closed:
                        break
                    self._chunk(b": ping\n\n")
                    continue
                data = b"data: " + json.dumps(fr, separators=(",", ":")).encode() + b"\n\n"
                self._chunk(data)
                n_out += len(data)
                if fr.get("k") in ("end", "err"):
                    break
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            relay.close_stream(install, st)
            self._log("sse", install, 200, 0, n_out, t0)

    def _static(self, path: str) -> None:
        root = self.server.static_dir
        rel = unquote(path).lstrip("/")
        if not rel or rel.endswith("/"):
            rel += "index.html"
        parts = rel.split("/")
        full = None
        if root is not None and "\\" not in rel and "\x00" not in rel \
                and not any(p in ("", ".", "..") for p in parts):
            cand = (root / rel).resolve()
            if cand.is_relative_to(root) and cand.is_file():
                full = cand
        if full is None:
            self._send_json(404, {"error": "not found"})
            return
        data = full.read_bytes()
        ctype = EXTRA_TYPES.get(full.suffix) or mimetypes.guess_type(full.name)[0] \
            or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in STATIC_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple, relay: Relay, static_dir: Optional[str] = None) -> None:
        super().__init__(addr, RelayHandler)
        self.relay = relay
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self._slots = threading.BoundedSemaphore(relay.limits.max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def start_reaper(relay: Relay, every: float = 5.0) -> threading.Event:
    """A daemon thread that drops offline routes; set the event to stop it."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            relay.reap()
    threading.Thread(target=loop, name="relay-reaper", daemon=True).start()
    return stop


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="The Tacitus web chat relay.")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8471)
    ap.add_argument("--static", default="", help="the web app's folder (index.html, app.js, ...)")
    ap.add_argument("--allow-file", default="", help="serve only the install ids listed here")
    ap.add_argument("--log-file", default="", help="append the access lines here, not stderr")
    ap.add_argument("--max-installs", type=int, default=Limits.max_installs)
    ap.add_argument("--max-frame-bytes", type=int, default=Limits.max_frame)
    args = ap.parse_args(argv)
    logger = None
    if args.log_file:
        fh = open(args.log_file, "a", buffering=1)

        def logger(line: str) -> None:
            fh.write(line + "\n")
    limits = Limits(max_installs=args.max_installs, max_frame=args.max_frame_bytes)
    relay = Relay(limits, allow_file=args.allow_file or None, log=logger)
    server = RelayHTTPServer((args.bind, args.port), relay, args.static or None)
    start_reaper(relay)
    relay.log(f"relay: listening on {args.bind}:{server.server_address[1]}"
              f" static={args.static or '-'} allow={args.allow_file or '-'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
