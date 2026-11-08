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
A bot that starts says so on its first poll (`fresh`): the streams the
bot before it was feeding end, and their phones open them again.

Routes:
  POST /r/{install}/bot   the bot: posts its frames, long-polls for the phone's
  POST /r/{install}/c     one phone frame (hello, req, ping); waits for the answer
  POST /r/{install}/sse   the phone's `open` frame; answers a one-time ticket
  GET  /r/{install}/sse   ?t=<ticket>: the phone's event stream
  POST /r/{install}/handoff  the bot leaves a sealed handoff blob in a mailbox
  GET  /h/{mailbox}       a computer takes that blob, once
  GET  /healthz           liveness; the counts only to a monitor on this host
  GET  /...               the static web app from --static (its own hostname only)

A bot proves its install id with a relay token whose hash is the id, so
the relay needs no table of installs; the relay checks the token from the
headers before it reads a bot's body. A phone shows the client key the
bot's token derives, in the X-Relay-Key header; the relay checks it before
it reads a body, and answers a wrong key as it answers an install that is
offline. The event stream's URL carries only a ticket that works once
and for 30 seconds, so a proxy's access log holds no key and no frame.
A handoff blob is the pairing a computer takes to join, sealed under a
key the bot and the computer derive from a short code with a slow KDF;
its mailbox name comes from the same KDF, so the relay holds neither the
code nor a cheap way to test one. A mailbox is read once, lives at most
ten minutes, and no log line names it.
With --allow-file, only the listed install ids are served (the hosted
tier's activation list). Without one, a source address may open a few
new installs a minute, and a full table makes room by dropping the
longest-idle install that holds no poll.

Standard library only. Run it behind a TLS terminator (Caddy, nginx) that
does not buffer the event streams. Serve the app (--static) only on a
hostname of its own: any other page on that origin can use a paired
phone's key.

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
import http
import ipaddress
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

ROUTE_RE = re.compile(r"^/r/([a-z2-7]{26})/(bot|c|sse|handoff)$")
HANDOFF_RE = re.compile(r"^/h/([0-9a-f]{32})$")
MAILBOX_RE = re.compile(r"^[0-9a-f]{32}$")
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
# A computer's lookup of its handoff mailbox: any origin (the hosted copy of
# the app), no credentials, no custom header.
HANDOFF_CORS = {"Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Max-Age": "600"}
STATIC_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; connect-src 'self'; "
                                "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-cache",
}
# The service worker's own policy (sw.js): its loader runs, as text, the copy
# of the worker's code (bundle.js) that it kept and checked against the bot's
# signed manifest, so a release that comes through the relay runs in the
# worker too (docs/webchat.md, Code integrity). That needs 'unsafe-eval' in the
# worker alone; the pages keep the policy above.
WORKER_CSP = STATIC_HEADERS["Content-Security-Policy"].replace(
    "script-src 'self'", "script-src 'self' 'unsafe-eval'")
EXTRA_TYPES = {".webmanifest": "application/manifest+json",
               ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
               ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".svg": "image/svg+xml"}


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
    max_streams: int = 8              # open event streams
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
    max_handoffs: int = 8             # handoff blobs held per install
    max_handoff_bytes: int = 4096     # one blob (base64url), bytes
    max_handoff_ttl: int = 600        # seconds a blob is held at most
    max_handoffs_total: int = 4096    # blobs held in all (16 MiB at most)
    max_handoffs_per_source: int = 32  # blobs held per source address (an IPv6 /48)
    lookup_rate: float = 20 / 60      # mailbox lookups per second per source address
    lookup_burst: int = 20


def source_group(source: str) -> str:
    """The unit a source address is metered by: an IPv4 address, or the /48
    of an IPv6 one (a tunnel broker hands out a whole /48 for free, so a
    /64 is no unit of cost)."""
    try:
        ip = ipaddress.ip_address(source)
    except ValueError:
        return source
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/48", strict=False)) if ip.version == 6 else str(ip)


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
    """One phone event stream: its frame id, the session its `open` frame
    names, and whether a bot took that frame (`taken`)."""

    def __init__(self, fid: str, maxsize: int, session: str = "") -> None:
        self.id = fid
        self.session = session
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize)
        self.closed = False
        self.taken = False


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
        # Handoff blobs by mailbox: (install, blob, expiry, source group).
        # Apart from the routes: a blob outlives a bot that stops polling,
        # up to its ttl.
        self.handoffs: "OrderedDict[str, tuple[str, str, float, str]]" = OrderedDict()
        # Per source address: [tokens, refilled at], for mailbox lookups.
        self._lookups: dict[str, list[float]] = {}

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
            if frame["k"] == "open" and frame["id"] in route.streams:
                route.streams[frame["id"]].taken = True
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

    def _bucket(self, table: dict, source: str, now: float, rate: float, burst: int) -> bool:
        """Spend one token of `source`'s bucket in `table`; False when it is
        empty. Past max_sources addresses the full buckets go first, then
        all. Called with the lock held."""
        if len(table) > self.limits.max_sources:
            full = burst / max(rate, 1e-9)
            for key in [k for k, (_, t) in table.items() if now - t > full]:
                del table[key]
            if len(table) > self.limits.max_sources:
                table.clear()
        tokens, at = table.get(source, (float(burst), now))
        tokens = min(float(burst), tokens + (now - at) * rate)
        if tokens < 1.0:
            table[source] = [tokens, now]
            return False
        table[source] = [tokens - 1.0, now]
        return True

    def _source_ok(self, source: str, now: float) -> bool:
        """Whether `source` may open another route now (a token bucket per
        address, an IPv6 /48 counting as one); called with the lock held."""
        lim = self.limits
        return self._bucket(self._sources, source_group(source), now, lim.new_route_rate,
                            lim.new_route_burst)

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

    def check_bot(self, install: str, auth: Any) -> tuple[int, dict, bytes]:
        """The check the HTTP layer makes from the headers alone, before it
        reads a bot's body: (200, {}, token), or the refusal and b""."""
        text = auth[7:].strip() if isinstance(auth, str) and auth.startswith("Bearer ") else ""
        try:
            token = b64d(text) if 0 < len(text) <= 128 else b""
        except ValueError:
            token = b""
        if len(token) < 16:
            return 401, {"error": "bot token required"}, b""
        if not hmac.compare_digest(install_id_for(token), install):
            return 403, {"error": "token does not match install"}, b""
        if not self.allowed(install):
            return 403, {"error": "install not activated on this relay"}, b""
        return 200, {}, token

    def bot_exchange(self, install: str, auth: str, body: Any,
                     source: str = "") -> tuple[int, dict]:
        """The bot posts its frames and long-polls for the phone's. `source`
        is the bot's address, which only meters how fast it opens routes.
        `"fresh": true` marks a bot's first poll after it starts."""
        status, err, token = self.check_bot(install, auth)
        if status != 200:
            return status, err
        if not isinstance(body, dict) or not isinstance(body.get("frames", []), list) \
                or len(body.get("frames", [])) > 64:
            return 400, {"error": "bad poll"}
        try:
            wait = max(0.0, min(float(body.get("wait", 0)), self.limits.max_poll_wait))
        except (TypeError, ValueError):
            return 400, {"error": "bad wait"}
        lim = self.limits
        fresh = body.get("fresh") is True
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
            if fresh:
                # A bot that starts holds no stream: each one a bot took
                # before ends, and its phone says hello again and reopens it.
                # A stream whose `open` waits in the inbox is this bot's.
                for st in [s for s in route.streams.values() if s.taken]:
                    self._end_stream(route, st, "session")
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
            # A page keeps one stream per session: one that opens another has
            # let its old one go, which a keepalive write would notice only later.
            same = [s for s in route.streams.values() if frame.get("sid") and s.session == frame["sid"]]
            while same or len(route.streams) >= self.limits.max_streams:
                oldest = same.pop() if same else next(iter(route.streams.values()))
                self._end_stream(route, oldest, "busy")
                self._enqueue(route, {"id": oldest.id, "k": "close"})
            st = Stream(frame["id"], self.limits.stream_queue, frame.get("sid", ""))
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

    # handoffs: a computer's pairing, sealed, held for one read

    def _purge_handoffs(self, now: float) -> None:
        """Drop the blobs past their time; called with the lock held."""
        for m in [m for m, (_, _, exp, _) in self.handoffs.items() if exp <= now]:
            del self.handoffs[m]

    def _polling(self, install: str, now: float) -> bool:
        """Whether `install` has a route open (its bot polls), changing
        nothing; called with the lock held."""
        route = self.routes.get(install)
        return route is not None and (route.polls > 0
                                      or now - route.last_poll <= self.limits.offline_after)

    def _room_for_handoff(self, now: float) -> bool:
        """A full table, for a deposit from a polling bot: drop the oldest
        blob of an install with no route open (no bot polls for it, so no
        bot is waiting on it); called with the lock held. False when every
        blob belongs to a polling bot (the table stays full)."""
        for m, (install, _, _, _) in self.handoffs.items():
            if not self._polling(install, now):
                del self.handoffs[m]
                return True
        return False

    def deposit_handoff(self, install: str, auth: Any, body: Any,
                        source: str = "") -> tuple[int, dict]:
        """The bot leaves a sealed blob in a mailbox: `{"m": <32 hex>, "c":
        <base64url, at most max_handoff_bytes>, "ttl": <1..max_handoff_ttl>}`.
        At most max_handoffs per install; a deposit to a mailbox the install
        already holds replaces it, and one another install holds is
        refused. An install with no route open (no bot polling) spends the
        new-route meter of its address, so tokens minted for the purpose do
        not fill the table cheaply. Each source address (an IPv6 /48) holds
        at most max_handoffs_per_source. A full table makes room for a bot
        that polls by dropping the oldest blob of an install that does not,
        and never for an install that does not poll (503): blobs left by
        tokens minted for the purpose give way to a real bot's, and never
        push one out."""
        status, err, _ = self.check_bot(install, auth)
        if status != 200:
            return status, err
        lim = self.limits
        if not isinstance(body, dict) or set(body) != {"m", "c", "ttl"}:
            return 400, {"error": "bad handoff"}
        m, c, ttl = body["m"], body["c"], body["ttl"]
        if not isinstance(m, str) or not MAILBOX_RE.match(m) or not isinstance(c, str) \
                or not c or not B64_RE.match(c) or not isinstance(ttl, int) \
                or isinstance(ttl, bool) or not 1 <= ttl <= lim.max_handoff_ttl:
            return 400, {"error": "bad handoff"}
        if len(c) > lim.max_handoff_bytes:
            return 413, {"error": "handoff too large"}
        with self.lock:
            now = self.clock()
            self._purge_handoffs(now)
            held = self.handoffs.get(m)
            group = source_group(source)
            if held is not None and held[0] != install:
                return 409, {"error": "mailbox in use"}
            if held is None:
                if sum(1 for i, _, _, _ in self.handoffs.values() if i == install) >= lim.max_handoffs:
                    return 429, {"error": "too many handoffs"}
                if sum(1 for _, _, _, g in self.handoffs.values()
                       if g == group) >= lim.max_handoffs_per_source:
                    return 429, {"error": "too many handoffs from this address"}
                if self._online(install, now) is None and not self._source_ok(source, now):
                    return 429, {"error": "too many new installs from this address"}
                if len(self.handoffs) >= lim.max_handoffs_total and not (
                        self._polling(install, now) and self._room_for_handoff(now)):
                    return 503, {"error": "relay full"}
            self.handoffs[m] = (install, c, now + ttl, group)
        return 200, {"ok": True}

    def take_handoff(self, mailbox: Any, source: str = "") -> tuple[int, dict]:
        """A computer's one read of a mailbox: (200, {"c": blob}) and the
        blob is gone, or 404 for one unknown or past its time. Each address
        has `lookup_burst` lookups, refilled at `lookup_rate`, found or not;
        past that 429."""
        lim = self.limits
        with self.lock:
            now = self.clock()
            if not self._bucket(self._lookups, source, now, lim.lookup_rate, lim.lookup_burst):
                return 429, {"error": "too many lookups"}
            self._purge_handoffs(now)
            held = self.handoffs.pop(mailbox, None) \
                if isinstance(mailbox, str) and MAILBOX_RE.match(mailbox) else None
        if held is None:
            return 404, {"error": "not found"}
        return 200, {"c": held[1]}

    def reap(self) -> int:
        """Drop the routes whose bot stopped polling, and the handoff blobs
        past their time; how many routes went."""
        with self.lock:
            now = self.clock()
            self._purge_handoffs(now)
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
                    "streams": list(r.streams)} for i, r in self.routes.items()},
                "handoffs": {m: {"install": i, "c": c, "expires": exp}
                             for m, (i, c, exp, _) in self.handoffs.items()}}

    def counts(self) -> dict:
        with self.lock:
            return {"installs": len(self.routes),
                    "streams": sum(len(r.streams) for r in self.routes.values()),
                    "queued_bytes": self.total_bytes, "handoffs": len(self.handoffs)}


# ── HTTP ──────────────────────────────────────────────────────────────

class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tacitus-relay/1"
    sys_version = ""
    timeout = 30  # socket reads and writes; a stalled phone is dropped

    server: "RelayHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        """The default access line carries the query string (a stream's
        ticket) and the client's address; the relay writes its own line
        instead (`_log`)."""

    def _log(self, kind: str, install: str, status: int, n_in: int, n_out: int,
             t0: float) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ms = int((time.monotonic() - t0) * 1000)
        self.server.relay.log(f"{stamp} {self.command} {kind} {install or '-'} {status} "
                              f"in={n_in} out={n_out} {ms}ms")

    def _send_json(self, status: int, obj: Any, cors: Any = False) -> int:
        """A JSON answer; `cors` True adds the phone routes' CORS headers,
        a dict adds those headers instead."""
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        headers = cors if isinstance(cors, dict) else CORS if cors else {}
        for k, v in headers.items():
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
        except (ValueError, RecursionError):  # not JSON, or nested past the parser's depth
            self._send_json(400, {"error": "not JSON"}, cors)
            return None, -1

    def send_error(self, code: int, message: Optional[str] = None,
                   explain: Optional[str] = None) -> None:
        """http.server's own errors (a bad request line, a method with no
        handler) as JSON with the status's phrase: no HTML, and nothing of
        the request."""
        self.close_connection = True
        try:
            phrase = http.HTTPStatus(code).phrase.lower()
        except ValueError:
            phrase = "error"
        try:
            self._send_json(code, {"error": phrase})
        except OSError:
            pass

    def do_HEAD(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def do_OPTIONS(self) -> None:
        path = urlsplit(self.path).path
        m = ROUTE_RE.match(path)
        handoff = HANDOFF_RE.match(path) is not None
        if not handoff and (not m or m.group(2) in ("bot", "handoff")):
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(204)
        for k, v in (HANDOFF_CORS if handoff else CORS).items():
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
        cors = kind not in ("bot", "handoff")
        key = self.headers.get("X-Relay-Key", "")
        # A phone's key and a bot's token are checked from the headers
        # before the body is read: a stranger never makes the relay read or
        # parse one.
        if cors:
            status, err = relay.check_key(install, key)
        else:
            status, err, _ = relay.check_bot(install, self.headers.get("Authorization", ""))
        if status != 200:
            self.close_connection = True
            n_out = self._send_json(status, err, cors)
            self._log(kind, install, status, 0, n_out, t0)
            return
        limit = relay.limits.max_small_frame + 1024 if kind == "sse" else \
            relay.limits.max_handoff_bytes + 1024 if kind == "handoff" else None
        body, n_in = self._read_json(cors, limit)
        if n_in < 0:
            self._log(kind, install, 400, 0, 0, t0)
            return
        if kind == "bot":
            status, out = relay.bot_exchange(install, self.headers.get("Authorization", ""),
                                             body, self._source())
        elif kind == "handoff":
            status, out = relay.deposit_handoff(install, self.headers.get("Authorization", ""),
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
            # The counts tell who is online: only a monitor on this host gets
            # them. A request through the proxy (it carries X-Forwarded-For)
            # gets liveness alone.
            local = str(self.client_address[0]) in LOOPBACK and \
                self.headers.get("X-Forwarded-For") is None
            self._send_json(200, {"ok": True, **(self.server.relay.counts() if local else {})})
            return
        h = HANDOFF_RE.match(url.path)
        if h:
            # The log line names no mailbox: the route and the status only.
            t0 = time.monotonic()
            status, out = self.server.relay.take_handoff(h.group(1), self._source())
            n_out = self._send_json(status, out, cors=HANDOFF_CORS)
            self._log("h", "", status, 0, n_out, t0)
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
            self.send_header(k, WORKER_CSP if k == "Content-Security-Policy" and full.name == "sw.js" else v)
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
    ap.add_argument("--static", default="",
                    help="the web app's folder (index.html, app.js, ...); only on a hostname "
                         "of its own")
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
    if args.static:
        relay.log("relay: serving the web app; give this relay a hostname of its own "
                  "(docs/webchat-relay.md, Running the relay)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
