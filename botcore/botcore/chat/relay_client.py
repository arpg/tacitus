"""The bot side of the web chat relay (docs/webchat-relay.md).

`start_relay(web_cfg, local_base_url)` is what the web front end calls
when its `web` block carries a `relay` block. It starts a `RelayClient`:
a thread that long-polls the relay for the phone's frames, opens each
under its session, replays the request against the web front end's own
HTTP server on loopback, and seals the answer back. A phone's event
stream is forwarded the same way, one sealed frame per event.

The relay sees install ids, frame sizes and timing. The keys stay here
and on the phone. The phone never chooses where a request goes: only
`/api/` paths on the loopback server, never the pairing routes, with the
owner's credential added here and every header but a short list dropped.

`KeyStore` is the key file (owner-private, mode 0600): the bot's X25519
key, the relay token, the paired devices and the open pairings. The CLI
at the bottom (`scripts/webchat-relay.py` runs it) creates the file,
shows the install id, opens a pairing and revokes a device.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from . import relay_crypto as rc

try:
    import fcntl
except ImportError:  # not POSIX: the in-process lock alone
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("chat.relay")

DEFAULT_WAIT = 25.0
LOCAL_TIMEOUT = 25.0
STREAM_READ_TIMEOUT = 75.0      # the local event stream pings every 20 s
MAX_BODY = 10 * 1024 * 1024     # a request or response body (the chat's file cap)
MAX_EVENT = 1024 * 1024
MAX_POLL_ANSWER = 48 * 1024 * 1024  # the relay hands back at most 32 MiB of frames
MAX_STREAMS = 4
MAX_SESSIONS = 32
SESSION_IDLE = 12 * 3600
MAX_PAIRINGS = 8
NAME_MAX = 40

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Headers a phone's request may carry to the local server; every other one
# (a cookie, an identity header, Host) is dropped here.
FORWARD_HEADERS = ("content-type", "accept", "last-event-id")
RETURN_HEADERS = ("content-type", "content-disposition")
# The local routes the relay never reaches: pairing the web chat and
# managing relay devices happen on the tailnet only.
DENIED_PATHS = ("/api/pair",)
DENIED_PREFIXES = ("/api/relay/",)
# What a device paired through the relay's own origin may do. The relay
# serves that copy of the web app, so a compromised relay could change its
# code; such a device never reaches the card actions (the Send and Create
# buttons) unless the owner sets "all". A device paired through the bot's
# own origin (the tailnet) runs code the relay never touches, and may do
# everything.
ACCESS_LEVELS = ("read", "chat", "all")
DEFAULT_RELAY_ORIGIN_ACCESS = "chat"
_CARD_ACTION_PREFIX = "/api/cards/"
_PATH_RE = re.compile(r"^/api/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*(\?[A-Za-z0-9._~%!$&'()*+,;=:@/?-]*)?$")


class RelayError(Exception):
    """A key file or config problem; the relay does not start."""


# ── the key file ──────────────────────────────────────────────────────

class KeyStore:
    """The relay key file. JSON, mode 0600, rewritten whole through a
    random temp name and a rename, under an flock on `<file>.lock` so the
    bot and the CLI do not lose each other's writes."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._cache: Optional[dict] = None
        self._stamp: Optional[tuple] = None
        self._mu = threading.RLock()

    def exists(self) -> bool:
        return self.path.exists()

    @classmethod
    def create(cls, path: str | os.PathLike, relay_url: str = "") -> "KeyStore":
        """A new key file with a fresh identity; refuses to replace one."""
        store = cls(path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with store._locked():
            if store.path.exists():
                raise RelayError(f"{store.path} exists; it holds this install's identity")
            ident = rc.BotIdentity.generate()
            store._write({"v": 1, "bot_private": rc.b64e(ident.private),
                          "relay_token": rc.b64e(ident.token), "relay_url": relay_url,
                          "devices": [], "pairings": []})
        return store

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._mu:
            if fcntl is None:
                yield
                return
            lock = self.path.with_name(self.path.name + ".lock")
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self) -> dict:
        st = os.stat(self.path)
        if st.st_mode & 0o077:
            raise RelayError(f"{self.path} is readable by others; chmod 600 it")
        try:
            data = json.loads(self.path.read_text())
        except ValueError as exc:
            raise RelayError(f"{self.path} is not JSON") from exc
        if not isinstance(data, dict) or data.get("v") != 1:
            raise RelayError(f"{self.path} is not a relay key file")
        data.setdefault("devices", [])
        data.setdefault("pairings", [])
        return data

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".relay-key-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._cache = None

    def load(self) -> dict:
        """The file's contents, read again only when the file changed."""
        with self._mu:
            st = os.stat(self.path)
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            if self._cache is None or stamp != self._stamp:
                self._cache, self._stamp = self._read(), stamp
            return self._cache

    def _update(self, fn: Callable[[dict], Any]) -> Any:
        with self._locked():
            data = self._read()
            out = fn(data)
            self._write(data)
            return out

    def identity(self) -> rc.BotIdentity:
        data = self.load()
        try:
            return rc.BotIdentity(rc.b64d(data["bot_private"]), rc.b64d(data["relay_token"]))
        except (KeyError, rc.FrameError) as exc:
            raise RelayError(f"{self.path} has no usable identity") from exc

    def relay_url(self) -> str:
        return str(self.load().get("relay_url") or "")

    def devices(self) -> list[dict]:
        return [dict(d) for d in self.load()["devices"]]

    def device(self, public: bytes) -> Optional[dict]:
        did = rc.device_id_for(public)
        return next((dict(d) for d in self.load()["devices"]
                     if d.get("id") == did and d.get("public") == rc.b64e(public)), None)

    def device_by_id(self, device_id: str) -> Optional[dict]:
        return next((dict(d) for d in self.load()["devices"] if d.get("id") == device_id), None)

    def add_pairing(self, ttl: int = rc.PAIRING_TTL, now: Optional[float] = None,
                    relay_url: str = "", origin: str = "relay") -> rc.PairingPayload:
        """A new one-time pairing, stored here (the expired ones dropped).
        `origin` is where its link opens the app: "own" (the bot's own
        origin) or "relay"; the device that spends it keeps that mark."""
        now = time.time() if now is None else now
        payload = rc.new_pairing(self.identity(), relay_url or self.relay_url(), ttl=ttl, now=now)

        def add(data: dict) -> None:
            live = [p for p in data["pairings"] if p.get("expires", 0) > now]
            live.append({"id": payload.pairing_id, "psk": rc.b64e(payload.psk),
                         "expires": payload.expires,
                         "origin": "own" if origin == "own" else "relay"})
            data["pairings"] = live[-MAX_PAIRINGS:]
        self._update(add)
        return payload

    def pairing_psk(self, pairing_id: str, now: Optional[float] = None) -> Optional[bytes]:
        now = time.time() if now is None else now
        for p in self.load()["pairings"]:
            if p.get("id") == pairing_id and p.get("expires", 0) > now:
                return rc.b64d(p["psk"])
        return None

    def register(self, pairing_id: str, public: bytes, name: str = "",
                 now: Optional[float] = None) -> dict:
        """Spend an open pairing on a device key. RelayError when the
        pairing is gone (used, expired or never made)."""
        now = time.time() if now is None else now
        clean = re.sub(r"[^\w .,'()+-]", "", str(name))[:NAME_MAX].strip() or "device"
        record = {"id": rc.device_id_for(public), "public": rc.b64e(public),
                  "name": clean, "paired_at": int(now), "origin": "relay"}

        def reg(data: dict) -> None:
            live = [p for p in data["pairings"]
                    if p.get("id") == pairing_id and p.get("expires", 0) > now]
            if not live:
                raise RelayError("pairing is used, expired or unknown")
            record["origin"] = "own" if live[0].get("origin") == "own" else "relay"
            data["pairings"] = [p for p in data["pairings"] if p.get("id") != pairing_id]
            data["devices"] = [d for d in data["devices"] if d.get("id") != record["id"]]
            data["devices"].append(record)
        self._update(reg)
        return dict(record)

    def revoke(self, device_id: str) -> bool:
        def rev(data: dict) -> bool:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d.get("id") != device_id]
            return len(data["devices"]) != before
        return self._update(rev)


# ── helpers ───────────────────────────────────────────────────────────

def _check_relay_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK):
        return url.rstrip("/")
    raise RelayError(f"relay url must be https (http only on loopback): {url}")


def _check_local_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or (parts.hostname or "").lower() not in LOOPBACK:
        raise RelayError(f"the local base url must be http on loopback: {url}")
    return url.rstrip("/")


def owner_headers_for(web_cfg: Mapping[str, Any]) -> dict[str, str]:
    """The credential the local server takes as the owner: the pair-token
    cookie when its file is readable, else the tailscale login header."""
    tok_file = web_cfg.get("pair_token_file")
    if tok_file:
        try:
            tok = Path(os.path.expanduser(str(tok_file))).read_text().strip()
        except OSError:
            tok = ""
        if tok:
            return {"Cookie": f"ea_web={tok}"}
    login = web_cfg.get("tailscale_login")
    return {"Tailscale-User-Login": str(login)} if login else {}


def _origin(url: Optional[str]) -> Optional[tuple[str, str, int]]:
    """(scheme, host, port) of an http(s) URL, normalized: the host
    lowercased and without a trailing dot, the default port written out.
    None for anything else."""
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def origin_of(app_url: Optional[str], public_url: Optional[str],
              relay_url: Optional[str] = None) -> str:
    """"own" when a pairing link opens the app on the bot's own origin: the
    web block's `public_url`, compared normalized (scheme, host without a
    trailing dot, explicit port). Anything else is "relay": no public_url,
    another origin, or the relay's own origin under any spelling. Its code
    may come from a server that is not the bot."""
    app, own = _origin(app_url), _origin(public_url)
    if app is None or app != own:
        return "relay"
    if relay_url and _origin(relay_url) == app:
        return "relay"
    return "own"


def access_allows(level: str, method: str, path: str) -> bool:
    """Whether a relay-origin device at `level` may make this request. The
    path is compared decoded and lowercased, so no spelling of a card
    action reaches a router that normalizes it."""
    if level == "all":
        return True
    if method == "GET":
        return True
    if level != "chat":
        return False
    route = urllib.parse.unquote(path.split("?", 1)[0]).lower()
    return not re.sub(r"/{2,}", "/", route).startswith(_CARD_ACTION_PREFIX.rstrip("/"))


def path_allowed(path: Any) -> bool:
    """True for an `/api/` path (and query) the relay may reach."""
    if not isinstance(path, str) or len(path) > 2048 or not _PATH_RE.match(path):
        return False
    route = path.split("?", 1)[0]
    segments = route.split("/")
    if "" in segments[1:-1]:  # an empty segment: "//"
        return False
    if any(s in (".", "..") or urllib.parse.unquote(s) in (".", "..") for s in segments):
        return False
    # The local server decodes each segment before it routes, so the denied
    # routes are compared decoded and lowercased: no spelling of them
    # (`/api/%72elay/pair`, `/api/Pair`) gets through.
    plain = re.sub(r"/{2,}", "/", urllib.parse.unquote(route).lower())
    if plain.rstrip("/") in DENIED_PATHS:
        return False
    return not any(plain.startswith(p) or plain + "/" == p for p in DENIED_PREFIXES)


def parse_sse(lines: list[str]) -> Optional[dict]:
    """One SSE event block as {"id"?, "event"?, "retry"?, "data"}; None for
    a block of comments only. The local stream's first block (`retry:`) is
    kept: it reaches the phone as the sign that the bot's stream is open,
    so no event falls between the phone's open and the bot's."""
    ev: dict[str, str] = {}
    data: list[str] = []
    for line in lines:
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            data.append(value)
        elif name in ("id", "event", "retry"):
            ev[name] = value
    if not data and "event" not in ev and "retry" not in ev:
        return None
    ev["data"] = "\n".join(data)
    return ev


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def _response(status: int, error: str) -> tuple[dict, bytes]:
    return ({"s": status, "h": {"content-type": "application/json"}},
            _json_bytes({"error": error}))


# ── the client ────────────────────────────────────────────────────────

class RelayClient:
    """Long-polls the relay, opens frames, replays them on loopback."""

    def __init__(self, relay_url: str, store: KeyStore, local_base_url: str, *,
                 owner_headers: Callable[[], Mapping[str, str]] | Mapping[str, str] | None = None,
                 wait: float = DEFAULT_WAIT, workers: int = 8,
                 app_url: Optional[str] = None, public_url: Optional[str] = None,
                 relay_origin_access: str = DEFAULT_RELAY_ORIGIN_ACCESS,
                 logger: Optional[logging.Logger] = None) -> None:
        rc._need()
        self.relay_url = _check_relay_url(relay_url)
        self.local = _check_local_url(local_base_url)
        self.store = store
        self.identity = store.identity()
        self.install_id = self.identity.install_id
        self.app_url = app_url
        # The bot's own origin (the web block's public_url): a pairing link
        # that opens the app there makes an "own" device.
        self.public_url = public_url
        if relay_origin_access not in ACCESS_LEVELS:
            raise RelayError(f"relay_origin_access is one of {ACCESS_LEVELS}")
        self.relay_origin_access = relay_origin_access
        self.wait = max(0.0, min(float(wait), 55.0))
        self.log = logger or log
        if callable(owner_headers):
            self._owner_headers = owner_headers
        else:
            fixed = dict(owner_headers or {})
            self._owner_headers = lambda: fixed
        self._bot_url = f"{self.relay_url}/r/{self.install_id}/bot"
        self._auth = "Bearer " + rc.b64e(self.identity.token)
        host = (urllib.parse.urlsplit(self.relay_url).hostname or "").lower()
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # The loopback server never goes through a proxy; the relay does
        # when the environment names one, unless it is on loopback too.
        self._local_opener = no_proxy
        self._relay_opener = no_proxy if host in LOOPBACK else urllib.request.build_opener()
        self._sessions: dict[str, rc.Session] = {}
        self._streams: dict[str, tuple[threading.Event, list]] = {}
        self._hellos = rc.HelloCache()
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="relay")
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_ok = 0.0
        self.last_error = ""

    # lifecycle

    def start(self) -> "RelayClient":
        self._thread = threading.Thread(target=self._poll_loop, name="relay-poll", daemon=True)
        self._thread.start()
        self.log.info("relay: polling %s as install %s", self.relay_url, self.install_id)
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._mu:
            streams = list(self._streams.values())
            self._streams.clear()
            self._sessions.clear()
        for ev, holder in streams:
            ev.set()
            self._shut(holder)
        self._pool.shutdown(wait=False)

    def status(self) -> dict:
        with self._mu:
            sessions, streams = len(self._sessions), len(self._streams)
        return {"install_id": self.install_id, "relay_url": self.relay_url,
                "connected": self.connected, "last_ok": self.last_ok,
                "last_error": self.last_error, "sessions": sessions,
                "streams": streams, "devices": len(self.store.devices())}

    # pairing, for the tailnet web chat

    def new_pairing(self, app_url: Optional[str] = None, ttl: int = rc.PAIRING_TTL) -> str:
        """A one-time pairing link (the QR code's text) for a new phone."""
        url = app_url or self.app_url
        payload = self.store.add_pairing(ttl=ttl, relay_url=self.relay_url,
                                         origin=origin_of(url, self.public_url, self.relay_url))
        return payload.to_string(url)

    def devices(self) -> list[dict]:
        return [{k: d.get(k) for k in ("id", "name", "paired_at", "origin")}
                for d in self.store.devices()]

    def revoke(self, device_id: str) -> bool:
        """Remove a device; its sessions end at once."""
        gone = self.store.revoke(device_id)
        with self._mu:
            for sid in [s for s, sess in self._sessions.items() if sess.device == device_id]:
                del self._sessions[sid]
        return gone

    # the relay connection

    def _exchange(self, frames: list, wait: float) -> list[dict]:
        body = _json_bytes({"frames": frames, "wait": wait})
        req = urllib.request.Request(self._bot_url, data=body, method="POST", headers={
            "Authorization": self._auth, "Content-Type": "application/json"})
        with self._relay_opener.open(req, timeout=wait + 15) as resp:
            raw = resp.read(MAX_POLL_ANSWER + 1)
        if len(raw) > MAX_POLL_ANSWER:
            raise RelayError("the relay's answer is too large")
        data = json.loads(raw)
        got = data.get("frames") if isinstance(data, dict) else None
        return [f for f in got if isinstance(f, dict)] if isinstance(got, list) else []

    def _note_error(self, exc: BaseException) -> float:
        """Log a new kind of failure once; the back-off floor for it."""
        floor = 1.0
        text = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, urllib.error.HTTPError):
            with contextlib.suppress(Exception):
                text = f"HTTP {exc.code}: {json.loads(exc.read()).get('error', '')}"
            if exc.code in (401, 403):
                floor = 60.0
        if text != self.last_error:
            self.log.warning("relay: %s", text)
        self.last_error = text
        self.connected = False
        return floor

    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                frames = self._exchange([], self.wait)
            except Exception as exc:  # the network, the relay, a proxy
                floor = self._note_error(exc)
                backoff = max(backoff, floor)
                self._stop.wait(backoff * (0.5 + random.random() / 2))
                backoff = min(backoff * 2, 60.0)
                continue
            if not self.connected:
                self.log.info("relay: connected")
            backoff, self.connected, self.last_ok, self.last_error = 1.0, True, time.time(), ""
            self._dispatch(frames)

    def _dispatch(self, frames: list[dict]) -> None:
        for frame in frames:
            try:
                self._pool.submit(self._handle, frame)
            except RuntimeError:  # stopped
                return

    def _send(self, frame: dict) -> None:
        """Post one frame now; the relay hands back any frames waiting."""
        try:
            got = self._exchange([frame], 0)
        except Exception as exc:
            self._note_error(exc)
            return
        self._dispatch(got)

    # frames

    def _handle(self, frame: dict) -> None:
        kind = frame.get("k")
        try:
            if kind == "hello":
                self._on_hello(frame)
            elif kind in rc.CLIENT_SEALED:
                self._on_sealed(frame)
            elif kind == "close":
                self._on_close(str(frame.get("id", "")))
        except Exception:
            self.log.exception("relay: %s frame failed", kind)

    def _on_hello(self, frame: dict) -> None:
        fid = str(frame.get("id", ""))
        try:
            acc = rc.bot_accept(self.identity, frame, psk_for=self.store.pairing_psk,
                                seen=self._hellos.seen)
            if acc.pairing_id:
                name = acc.info.get("name", "") if isinstance(acc.info, dict) else ""
                dev = self.store.register(acc.pairing_id, acc.device_public, name)
                self.log.info("relay: paired device %s (%s)", dev["id"], dev["name"])
            elif self.store.device(acc.device_public) is None:
                raise rc.HandshakeError("device not paired")
        except (rc.HandshakeError, RelayError) as exc:
            self.log.info("relay: hello refused: %s", exc)
            self._send(rc.error_frame(fid, "auth"))
            return
        self._add_session(acc.session)
        self._send(acc.welcome)

    def _add_session(self, session: rc.Session) -> None:
        now = time.time()
        with self._mu:
            for sid in [s for s, x in self._sessions.items() if now - x.last_used > SESSION_IDLE]:
                del self._sessions[sid]
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda x: x.last_used)
                del self._sessions[oldest.sid]
            self._sessions[session.sid] = session

    def _session(self, frame: dict) -> tuple[Optional[rc.Session], dict, str]:
        """The frame's session and its device record, or an error code."""
        with self._mu:
            session = self._sessions.get(str(frame.get("sid", "")))
        if session is None:
            return None, {}, "session"
        device = self.store.device_by_id(session.device)
        if device is None:
            with self._mu:
                self._sessions.pop(session.sid, None)
            return None, {}, "auth"
        return session, device, ""

    def _on_sealed(self, frame: dict) -> None:
        fid = str(frame.get("id", ""))
        session, device, code = self._session(frame)
        if session is None:
            self._send(rc.error_frame(fid, code))
            return
        try:
            plain = session.open(frame)
        except rc.ReplayError:
            self._send(rc.error_frame(fid, "replay"))
            return
        except rc.FrameError:
            self._send(rc.error_frame(fid, "bad"))
            return
        kind = frame["k"]
        if kind == "ping":
            self._send(session.seal("res", fid, rc.pack({"pong": int(time.time())})))
            return
        try:
            req, body = rc.unpack(plain)
        except rc.FrameError:
            self._send(session.seal("res", fid, rc.pack(*_response(400, "bad request"))))
            return
        if kind == "req":
            level = "all" if device.get("origin") == "own" else self.relay_origin_access
            if not access_allows(level, str(req.get("m")), str(req.get("p"))):
                out = _response(403, "not allowed from a device paired on the relay's origin")
            else:
                out = self._local_call(req, body, session.device, level)
            self._send(session.seal("res", fid, rc.pack(*out)))
        elif kind == "open":
            self._open_stream(session, fid, req)

    def _headers(self, device: str, extra: Mapping[str, Any] | None = None,
                 level: str = "read") -> dict[str, str]:
        """The phone's allowed headers, the owner's credential, and the
        relay marks: the device id, and whether this request may confirm a
        card (`X-Tacitus-Relay-Cards`). A local server that resolves typed
        card codes skips that on "deny"."""
        headers: dict[str, str] = {}
        for name, value in (extra or {}).items():
            key = str(name).lower()
            if key in FORWARD_HEADERS and isinstance(value, str) \
                    and len(value) <= 256 and not re.search(r"[\r\n]", value):
                headers[key] = value
        headers.update(self._owner_headers())
        headers["X-Tacitus-Relay"] = device
        headers["X-Tacitus-Relay-Cards"] = "allow" if level == "all" else "deny"
        return headers

    def _local_call(self, req: dict, raw: bytes, device: str,
                    level: str = "read") -> tuple[dict, bytes]:
        """Replay one request on the loopback server: (status and headers,
        body) of its answer."""
        method, path = req.get("m"), req.get("p")
        if method not in ("GET", "POST"):
            return _response(405, "method not allowed")
        if not path_allowed(path):
            return _response(403, "path not allowed through the relay")
        body = raw if method == "POST" else None
        if body is not None and len(body) > MAX_BODY:
            return _response(413, "body too large")
        hdrs = req.get("h") if isinstance(req.get("h"), dict) else {}
        request = urllib.request.Request(self.local + path, data=body, method=method,
                                         headers=self._headers(device, hdrs, level))
        try:
            resp = self._local_opener.open(request, timeout=LOCAL_TIMEOUT)
        except urllib.error.HTTPError as exc:
            resp = exc
        except (urllib.error.URLError, OSError) as exc:
            self.log.warning("relay: local server: %s", exc)
            return _response(502, "the web chat did not answer")
        with resp:
            data = resp.read(MAX_BODY + 1)
            status = resp.status if hasattr(resp, "status") else resp.code
            headers = {k.lower(): v for k, v in resp.headers.items()
                       if k.lower() in RETURN_HEADERS}
        if len(data) > MAX_BODY:
            return _response(502, "response too large for the relay")
        return {"s": int(status), "h": headers}, data

    # event streams

    def _open_stream(self, session: rc.Session, fid: str, req: dict) -> None:
        since = req.get("since")
        with self._mu:
            while len(self._streams) >= MAX_STREAMS:
                old_id = next(iter(self._streams))
                ev, holder = self._streams.pop(old_id)
                ev.set()
                self._shut(holder)
            ev, holder = threading.Event(), []
            self._streams[fid] = (ev, holder)
        args = (session, fid, since if isinstance(since, int) and since >= 0 else None, ev, holder)
        threading.Thread(target=self._stream, args=args, name=f"relay-sse-{fid[:8]}",
                         daemon=True).start()

    @staticmethod
    def _shut(holder: list) -> None:
        """Unblock a stream's read by shutting its socket."""
        for resp in holder:
            with contextlib.suppress(Exception):
                resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)

    def _stream(self, session: rc.Session, fid: str, since: Optional[int],
                ev: threading.Event, holder: list) -> None:
        path = "/api/events" + (f"?since={since}" if since is not None else "")
        headers = self._headers(session.device, {"accept": "text/event-stream"})
        status = 200
        try:
            resp = self._local_opener.open(urllib.request.Request(self.local + path, headers=headers),
                                           timeout=STREAM_READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            status, resp = exc.code, None
        except (urllib.error.URLError, OSError):
            status, resp = 502, None
        try:
            if resp is not None:
                holder.append(resp)
                block: list[str] = []
                size = 0
                while not ev.is_set() and not self._stop.is_set():
                    raw = resp.readline(MAX_EVENT)
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line:
                        block.append(line)
                        size += len(line)
                        if size > MAX_EVENT:
                            break
                        continue
                    event = parse_sse(block)
                    block, size = [], 0
                    if event is not None and not ev.is_set():
                        self._send(session.seal("sse", fid, rc.pack(event)))
        except (OSError, ValueError):
            pass
        finally:
            for r in holder:
                with contextlib.suppress(Exception):
                    r.close()
            with self._mu:
                if self._streams.get(fid, (None,))[0] is ev:
                    del self._streams[fid]
            if not ev.is_set() and not self._stop.is_set():
                self._send(session.seal("end", fid, rc.pack({"s": status})))

    def _on_close(self, fid: str) -> None:
        with self._mu:
            entry = self._streams.pop(fid, None)
        if entry:
            entry[0].set()
            self._shut(entry[1])


# ── the web front end's hook ──────────────────────────────────────────

def start_relay(frontend_cfg: Mapping[str, Any], local_base_url: str, *,
                logger: Optional[logging.Logger] = None) -> Optional[RelayClient]:
    """Start the relay client when the `web` block names a relay.

    `frontend_cfg` is the chat block's `web` dict; its `relay` block holds
    `url`, `key_file` and optionally `install_id`, `app_url` (default: the
    web block's `public_url`), `relay_origin_access`, `wait` and
    `enabled`. `local_base_url` is the running web front end's loopback
    base, scheme to base path (`http://127.0.0.1:8470/chat`). Returns the
    started client (call `stop()` at shutdown), or None when no relay is
    configured or it cannot start; the reason is logged, and the web chat
    keeps working on the tailnet either way. A missing key file is created
    on the first start unless the block pins an `install_id`."""
    lg = logger or log
    relay = (frontend_cfg or {}).get("relay") or {}
    if not isinstance(relay, Mapping) or not relay or relay.get("enabled", True) is False:
        return None
    if not rc.AVAILABLE:
        lg.warning("relay: off, the cryptography package is not installed")
        return None
    url, key_file, want = relay.get("url"), relay.get("key_file"), relay.get("install_id")
    if not url or not key_file:
        lg.error("relay: off, the relay block needs url and key_file")
        return None
    try:
        store = KeyStore(Path(os.path.expanduser(str(key_file))))
        if not store.exists():
            if want:
                raise RelayError(f"{store.path} is missing and install_id {want} is pinned")
            store = KeyStore.create(store.path, relay_url=str(url))
            lg.warning("relay: created %s, install id %s", store.path, store.identity().install_id)
        have = store.identity().install_id
        if want and want != have:
            raise RelayError(f"{store.path} is install {have}, the config pins {want}")
        client = RelayClient(str(url), store, local_base_url,
                             owner_headers=lambda: owner_headers_for(frontend_cfg),
                             wait=float(relay.get("wait", DEFAULT_WAIT)),
                             app_url=relay.get("app_url") or frontend_cfg.get("public_url"),
                             public_url=frontend_cfg.get("public_url") or None,
                             relay_origin_access=str(relay.get("relay_origin_access",
                                                               DEFAULT_RELAY_ORIGIN_ACCESS)),
                             logger=lg)
    except (RelayError, ValueError, OSError) as exc:
        lg.error("relay: off, %s", exc)
        return None
    return client.start()


# ── the owner's CLI ───────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="webchat-relay",
                                 description="The web chat relay's key file: create, show, pair, revoke.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("init", "create the key file and print the install id"),
                       ("show", "print the install id and the paired devices"),
                       ("pair", "open a one-time pairing and print its link"),
                       ("revoke", "remove a paired device")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--key-file", required=True)
        if name in ("init", "pair"):
            p.add_argument("--relay-url", default="")
        if name == "pair":
            p.add_argument("--public-url", default="",
                           help="the web block's public_url: the link opens the app there")
            p.add_argument("--app-url", default="",
                           help="open another app url (own origin only if it is public_url's)")
            p.add_argument("--ttl", type=int, default=rc.PAIRING_TTL)
        if name == "revoke":
            p.add_argument("--device", required=True)
    args = ap.parse_args(argv)
    if not rc.AVAILABLE:
        print("the relay needs the cryptography package", file=sys.stderr)
        return 2
    path = Path(os.path.expanduser(args.key_file)).absolute()
    try:
        if args.cmd == "init":
            if args.relay_url:
                _check_relay_url(args.relay_url)
            store = KeyStore.create(path, relay_url=args.relay_url)
            ident = store.identity()
            print(json.dumps({"install_id": ident.install_id, "key_file": str(path),
                              "relay": {"url": args.relay_url or "<relay url>",
                                        "install_id": ident.install_id,
                                        "key_file": str(path)}}, indent=1))
            return 0
        store = KeyStore(path)
        if args.cmd == "show":
            print(json.dumps({"install_id": store.identity().install_id,
                              "relay_url": store.relay_url(),
                              "devices": [{k: d.get(k) for k in ("id", "name", "paired_at",
                                                                 "origin")}
                                          for d in store.devices()],
                              "open_pairings": len([p for p in store.load()["pairings"]
                                                    if p.get("expires", 0) > time.time()])},
                             indent=1))
            return 0
        if args.cmd == "pair":
            url = _check_relay_url(args.relay_url or store.relay_url())
            app = args.app_url or (args.public_url.rstrip("/") + "/" if args.public_url else "")
            origin = origin_of(app, args.public_url, url)
            if app and origin == "relay":
                print(f"note: {app} is not the bot's own origin (--public-url); the device "
                      "is paired as a relay-origin device", file=sys.stderr)
            payload = store.add_pairing(ttl=args.ttl, relay_url=url, origin=origin)
            print(payload.to_string(app or None))
            return 0
        if args.cmd == "revoke":
            ok = store.revoke(args.device)
            print("revoked" if ok else "no such device")
            return 0 if ok else 1
    except (RelayError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
