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
key, the relay token, the Ed25519 key that signs the web app, the paired
devices and the open pairings. A new device waits for the owner's
approval on the tailnet. The CLI at the
bottom (`scripts/webchat-relay.py` runs it) creates the file, shows the
install id, opens a pairing, and approves and revokes a device.

The relay is not trusted: whatever it hands the bot is bounded here (the
frames per answer, the work queued, the bytes read, the log lines), and
the bot follows no redirect from it or from the loopback server.
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
import stat
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
MIN_WAIT = 1.0                  # also the pause after an empty answer that came early
LOCAL_TIMEOUT = 25.0
LOCAL_DEADLINE = 60.0           # one replayed request, its answer read whole
STREAM_READ_TIMEOUT = 75.0      # the local event stream pings every 20 s
MAX_BODY = 10 * 1024 * 1024     # a request or response body (the chat's file cap)
MAX_EVENT = 1024 * 1024
MAX_POLL_ANSWER = 48 * 1024 * 1024  # the relay hands back at most 32 MiB of frames
MAX_ANSWER_FRAMES = 64          # frames taken from one answer; the rest are dropped
MAX_STREAMS = 4
MAX_SESSIONS = 32
MAX_DEVICE_SESSIONS = 4
SESSION_IDLE = 12 * 3600
MAX_PAIRINGS = 8
NAME_MAX = 40
REFUSED_LOG_EVERY = 60.0        # at most one "hello refused" line a minute

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Headers a phone's request may carry to the local server; every other one
# (a cookie, an identity header, Host) is dropped here.
FORWARD_HEADERS = ("content-type", "accept", "last-event-id")
RETURN_HEADERS = ("content-type", "content-disposition")
# The local routes the relay never reaches: pairing the web chat and
# managing relay devices happen on the tailnet only. The event stream is
# reached by an `open` frame, never by a plain request.
DENIED_PREFIXES = ("/api/pair/", "/api/relay/")
EVENTS_PATH = "/api/events"
# The one relay route a phone reaches through the relay: after it checks
# the app's code against the bot's signed manifest it posts the bundle hash
# here, and the web front end records it on the device (docs/webchat.md,
# Code integrity). A device at any level but "off" may post it.
ATTEST_PATH = "/api/relay/attest"
# What a device paired through the relay's own origin may do. The relay
# serves that copy of the web app, so a compromised relay could change its
# code, read what the phone shows and type as the owner. Such a device is
# refused unless the owner-private chat.json opts in ("read", "chat" or
# "all"), and never reaches the card actions (the Send and Create
# buttons) below "all" unless the owner allows them for that device
# (`cards_allowed`, set by `approve(..., cards=True)` only). Its check of
# its code (`bundle_verified`) is recorded but grants nothing: the code
# that runs the check came from the relay. A device paired through the
# bot's own origin (the tailnet) runs code the relay never touches, and
# may do everything.
ACCESS_LEVELS = ("off", "read", "chat", "all")
DEFAULT_RELAY_ORIGIN_ACCESS = "off"
RELAY_ORIGIN_WARNING = ("a relay-origin device runs the copy of the app the relay serves: whoever "
                        "runs the relay can read this phone's chat and type as you")
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
        st = os.lstat(self.path)
        if not stat.S_ISREG(st.st_mode):
            raise RelayError(f"{self.path} is not a regular file")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise RelayError(f"{self.path} is owned by uid {st.st_uid}, not by this user")
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
        """Change the file under the lock; an expired pairing's secret is
        dropped on every write."""
        with self._locked():
            data = self._read()
            out = fn(data)
            now = time.time()
            data["pairings"] = [p for p in data["pairings"] if p.get("expires", 0) > now]
            self._write(data)
            return out

    def prune(self) -> None:
        """Drop the expired pairings now (each write does too)."""
        now = time.time()
        if any(p.get("expires", 0) <= now for p in self.load()["pairings"]):
            self._update(lambda data: None)

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

    def _pairing(self, pairing_id: str, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        return next((p for p in self.load()["pairings"]
                     if p.get("id") == pairing_id and p.get("expires", 0) > now), None)

    def pairing_psk(self, pairing_id: str, now: Optional[float] = None) -> Optional[bytes]:
        p = self._pairing(pairing_id, now)
        return rc.b64d(p["psk"]) if p else None

    def pairing_origin(self, pairing_id: str) -> str:
        """"own" or "relay" for an open pairing; "" when it is gone."""
        p = self._pairing(pairing_id)
        return ("own" if p.get("origin") == "own" else "relay") if p else ""

    def register(self, pairing_id: str, public: bytes, name: str = "",
                 now: Optional[float] = None, code: str = "") -> dict:
        """Spend an open pairing on a device key. The device waits for the
        owner's approval (`pending`, with the hello's pairing `code`).
        RelayError when the pairing is gone (used, expired or never made)."""
        now = time.time() if now is None else now
        clean = re.sub(r"[^\w .,'()+-]", "", str(name))[:NAME_MAX].strip() or "device"
        record = {"id": rc.device_id_for(public), "public": rc.b64e(public),
                  "name": clean, "paired_at": int(now), "origin": "relay",
                  "pending": True, "code": code}

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

    def approve(self, device_id: str, cards: bool = False) -> bool:
        """Let a waiting device in, and set whether it may press card
        buttons (`cards_allowed`; approving again changes it). False when
        there is no such device."""
        def app(data: dict) -> bool:
            for d in data["devices"]:
                if d.get("id") == device_id:
                    d.pop("pending", None)
                    d.pop("code", None)
                    d["cards_allowed"] = cards is True
                    return True
            return False
        return self._update(app)

    def revoke(self, device_id: str) -> bool:
        def rev(data: dict) -> bool:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d.get("id") != device_id]
            return len(data["devices"]) != before
        return self._update(rev)

    # the web app's signing key (docs/webchat.md, Code integrity)

    def signing_keypair(self) -> tuple[bytes, bytes]:
        """The Ed25519 pair the bot signs its web app with (raw private, raw
        public): `sign_private` and `sign_public`, made and written into
        this file on first use."""
        have = self._signing(self.load())
        return have if have is not None else self._update(self._add_signing)

    def _signing(self, data: dict) -> Optional[tuple[bytes, bytes]]:
        if not data.get("sign_private"):
            return None
        try:
            priv, pub = rc.signing_keypair(rc.b64d(data["sign_private"]))
        except (rc.FrameError, ValueError) as exc:
            raise RelayError(f"{self.path} holds a signing key that does not load") from exc
        if data.get("sign_public") != rc.b64e(pub):
            raise RelayError(f"{self.path}: sign_public is not the key of sign_private")
        return priv, pub

    def _add_signing(self, data: dict) -> tuple[bytes, bytes]:
        have = self._signing(data)  # another process may have made it first
        if have is None:
            have = rc.signing_keypair()
            data["sign_private"], data["sign_public"] = rc.b64e(have[0]), rc.b64e(have[1])
        return have

    def attest(self, device_id: str, bundle: str, release: str = "",
               now: Optional[float] = None) -> bool:
        """Record a device's last check of its app: `bundle_verified`, the
        `attested_bundle` hash it matched and the bot's `attested_release`
        then; "" records a failed check. It grants nothing (`cards_allowed`
        does). False when there is no such device."""
        now = time.time() if now is None else now

        def att(data: dict) -> bool:
            for d in data["devices"]:
                if d.get("id") == device_id:
                    d["bundle_verified"] = bool(bundle)
                    d["attested_at"] = int(now)
                    for key, value in (("attested_bundle", bundle), ("attested_release", release)):
                        if bundle and value:
                            d[key] = value
                        else:
                            d.pop(key, None)
                    return True
            return False
        return self._update(att)


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
    cookie from `pair_token_file`, else none. Never the Tailscale login
    header: the web front end takes that only on a connection from
    tailscaled, and refuses it from any other loopback client."""
    tok_file = web_cfg.get("pair_token_file")
    if tok_file:
        try:
            tok = Path(os.path.expanduser(str(tok_file))).read_text().strip()
        except OSError:
            tok = ""
        if tok:
            return {"Cookie": f"ea_web={tok}"}
    return {}


def _origin(url: Optional[str]) -> Optional[tuple[str, str, int]]:
    """(scheme, host, port) of an http(s) URL, normalized: the host
    lowercased and without a trailing dot, the default port written out.
    None for anything else, and for a URL with a backslash or user info,
    which a browser and urllib may read as different hosts."""
    if not url or "\\" in str(url):
        return None
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port
    except ValueError:
        return None
    if "@" in parts.netloc:
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
    if level not in ACCESS_LEVELS or level == "off":
        return False
    if method == "GET" or (method == "POST" and _plain_route(path) == ATTEST_PATH):
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
    if plain == ATTEST_PATH:
        return True
    return not any(plain.startswith(p) or plain + "/" == p for p in DENIED_PREFIXES)


def device_row(d: Mapping[str, Any]) -> dict:
    """A device as the owner's list shows it: id, name, paired_at, origin,
    `pending` (and the pairing `code` while pending); for a relay-origin
    device also `cards_allowed`, `bundle_verified` (its last check of its
    app) and the `release` it checked against."""
    row = {k: d.get(k) for k in ("id", "name", "paired_at", "origin")}
    row["pending"] = bool(d.get("pending"))
    if row["pending"]:
        row["code"] = str(d.get("code") or "")
    if d.get("origin") != "own":
        row["cards_allowed"] = d.get("cards_allowed") is True
        row["bundle_verified"] = d.get("bundle_verified") is True
        if row["bundle_verified"]:
            row["release"] = str(d.get("attested_release") or "")
    return row


def _plain_route(path: str) -> str:
    """The path without its query, decoded, lowercased, with no trailing slash."""
    return urllib.parse.unquote(path.split("?", 1)[0]).lower().rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Neither the relay nor the loopback server has a reason to redirect;
    following one would carry the relay token or the owner's cookie to
    another address. A 3xx is answered as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _printable(text: str, limit: int = 160) -> str:
    """`text` for a log line or the status route: printable ASCII only, cut."""
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


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


def _response(status: int, error: str, **extra: Any) -> tuple[dict, bytes]:
    return ({"s": status, "h": {"content-type": "application/json"}},
            _json_bytes({"error": error, **extra}))


# ── the client ────────────────────────────────────────────────────────

class _Stream:
    """One phone event stream the bot forwards: its stop flag, the local
    answer (whose socket a stop shuts), its device, and why it ended
    ("auth" when the device was removed: the phone then hears `err auth`)."""
    __slots__ = ("ev", "resp", "device", "why")

    def __init__(self, device: str) -> None:
        self.ev = threading.Event()
        self.resp: Any = None
        self.device = device
        self.why = ""

    def stop(self, why: str = "") -> None:
        if why:
            self.why = why
        self.ev.set()
        with contextlib.suppress(Exception):
            self.resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)


def _read_capped(resp: Any, limit: int, deadline: float) -> Optional[bytes]:
    """Up to `limit` bytes of `resp`; None when the monotonic `deadline`
    passes first."""
    out = bytearray()
    while len(out) < limit:
        if time.monotonic() > deadline:
            return None
        chunk = resp.read(min(1 << 16, limit - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


PENDING = "this phone waits for the owner's approval on the tailnet"


class RelayClient:
    """Long-polls the relay, opens frames, replays them on loopback."""

    def __init__(self, relay_url: str, store: KeyStore, local_base_url: str, *,
                 owner_headers: Callable[[], Mapping[str, str]] | Mapping[str, str] | None = None,
                 wait: float = DEFAULT_WAIT, workers: int = 8,
                 app_url: Optional[str] = None, public_url: Optional[str] = None,
                 relay_origin_access: str = DEFAULT_RELAY_ORIGIN_ACCESS,
                 notify: Optional[Callable[[str], Any]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        rc._need()
        self.relay_url = _check_relay_url(relay_url)
        self.local = _check_local_url(local_base_url)
        # A replayed POST carries the loopback origin the local server takes
        # from a client without the identity header; Host is the URL's.
        parts = urllib.parse.urlsplit(self.local)
        self.local_origin = f"{parts.scheme}://{parts.netloc}"
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
        self.wait = max(MIN_WAIT, min(float(wait), 55.0))
        self.log = logger or log
        # Posts the owner a notice in the DM channel (a new device).
        self.notify = notify
        if callable(owner_headers):
            self._owner_headers = owner_headers
        else:
            fixed = dict(owner_headers or {})
            self._owner_headers = lambda: fixed
        self._bot_url = f"{self.relay_url}/r/{self.install_id}/bot"
        self._auth = "Bearer " + rc.b64e(self.identity.token)
        host = (urllib.parse.urlsplit(self.relay_url).hostname or "").lower()
        # The loopback server never goes through a proxy; the relay does
        # when the environment names one, unless it is on loopback too.
        # Neither opener follows a redirect.
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        self._local_opener = no_proxy
        self._relay_opener = no_proxy if host in LOOPBACK else urllib.request.build_opener(_NoRedirect)
        self._sessions: dict[str, rc.Session] = {}
        self._streams: dict[str, _Stream] = {}
        self._hellos = rc.HelloCache()
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="relay")
        # The frames waiting for a worker; past this many a frame is dropped.
        self._slots = threading.BoundedSemaphore(workers * 8)
        self._thread: Optional[threading.Thread] = None
        self._refused_at = float("-inf")
        self._refused = 0
        self.dropped = 0
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
        for st in streams:
            st.stop()
        self._pool.shutdown(wait=False)

    def status(self) -> dict:
        """The client's state for the tailnet UI. `bot_public` and
        `client_key` let the app check a pairing link against the bot;
        `sign_public` is the key the bot signs its web app with."""
        with self._mu:
            sessions, streams = len(self._sessions), len(self._streams)
        devices = self.store.devices()
        try:
            sign_public = rc.b64e(self.store.signing_keypair()[1])
        except (RelayError, OSError):
            sign_public = ""
        return {"install_id": self.install_id, "relay_url": self.relay_url,
                "bot_public": rc.b64e(self.identity.public),
                "client_key": self.identity.client_key, "sign_public": sign_public,
                "connected": self.connected, "last_ok": self.last_ok,
                "last_error": self.last_error, "sessions": sessions,
                "streams": streams, "devices": len(devices),
                "pending": sum(1 for d in devices if d.get("pending"))}

    # pairing, for the tailnet web chat

    def level(self, device: Mapping[str, Any]) -> str:
        """What a device may do: "all" for one paired on the bot's own
        origin. For one paired on the relay's origin: "chat" when it was
        paired against the bot's signed bundle (`bundle`), else
        `relay_origin_access` ("off" keeps it out); "all" when the owner
        allowed it card buttons (`cards_allowed`)."""
        if device.get("origin") == "own":
            return "all"
        base = "chat" if device.get("bundle") else self.relay_origin_access
        if base == "off":
            return "off"
        return "all" if device.get("cards_allowed") is True else base

    def attest(self, device_id: str, bundle: str, release: str = "") -> bool:
        """Record a device's check of its app: the bundle hash it matched
        and the bot's release then, or "" for a failed one. It grants
        nothing. False for an unknown device."""
        return self.store.attest(device_id, bundle, release)

    def new_pairing(self, app_url: Optional[str] = None, ttl: int = rc.PAIRING_TTL,
                    relay_origin: bool = False) -> str:
        """A one-time pairing link (the QR code's text) for a new phone. A
        link that opens the app anywhere but the bot's own origin makes a
        relay-origin device; it takes `relay_origin` and a chat.json whose
        `relay_origin_access` is not "off". RelayError otherwise."""
        url = app_url or self.app_url
        origin = origin_of(url, self.public_url, self.relay_url)
        if origin != "own" and not relay_origin:
            raise RelayError("the link would not open the app on the bot's own origin: set "
                             "public_url in the web block")
        if origin != "own" and self.relay_origin_access == "off":
            raise RelayError("relay_origin_access is off in chat.json: the bot refuses a "
                             "relay-origin device")
        payload = self.store.add_pairing(ttl=ttl, relay_url=self.relay_url, origin=origin)
        return payload.to_string(url)

    def devices(self) -> list[dict]:
        """`device_row` of each paired device."""
        return [device_row(d) for d in self.store.devices()]

    def approve(self, device_id: str, cards: bool = False) -> bool:
        """Let a waiting device in (its next request works), and set
        whether it may press card buttons."""
        return self.store.approve(device_id, cards=cards)

    def revoke(self, device_id: str) -> bool:
        """Remove a device: its sessions end at once, and each event stream
        it holds ends with `err auth`."""
        gone = self.store.revoke(device_id)
        with self._mu:
            for sid in [s for s, x in self._sessions.items() if x.device == device_id]:
                del self._sessions[sid]
            streams = [st for st in self._streams.values() if st.device == device_id]
        for st in streams:
            st.stop("auth")
        return gone

    # the relay connection

    def _exchange(self, frames: list, wait: float) -> list[dict]:
        body = _json_bytes({"frames": frames, "wait": wait})
        req = urllib.request.Request(self._bot_url, data=body, method="POST", headers={
            "Authorization": self._auth, "Content-Type": "application/json"})
        with self._relay_opener.open(req, timeout=wait + 10) as resp:
            raw = resp.read(MAX_POLL_ANSWER + 1)
        if len(raw) > MAX_POLL_ANSWER:
            raise RelayError("the relay's answer is too large")
        data = json.loads(raw)
        got = data.get("frames") if isinstance(data, dict) else None
        if not isinstance(got, list):
            return []
        if len(got) > MAX_ANSWER_FRAMES:
            self.dropped += len(got) - MAX_ANSWER_FRAMES
        return [f for f in got[:MAX_ANSWER_FRAMES] if isinstance(f, dict)]

    def _note_error(self, exc: BaseException) -> float:
        """Log a new kind of failure once; the back-off floor for it. The
        relay's words never reach the log or the status route: an HTTP
        error is its status code, unread, and anything else is cut to
        printable ASCII."""
        floor = 1.0
        if isinstance(exc, urllib.error.HTTPError):
            text = f"HTTP {exc.code}"
            with contextlib.suppress(Exception):
                exc.close()
            if exc.code in (401, 403):
                floor = 60.0
                text += " (the relay does not take this install's token, or has not activated it)"
        else:
            text = _printable(f"{type(exc).__name__}: {exc}")
        if text != self.last_error:
            self.log.warning("relay: %s", text)
        self.last_error = text
        self.connected = False
        return floor

    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            t0 = time.monotonic()
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
            # An empty answer well before the wait is up (a full or broken
            # relay) gets a pause, so no relay spins this loop.
            if not frames and time.monotonic() - t0 < self.wait / 2:
                self._stop.wait(MIN_WAIT)

    def _dispatch(self, frames: list[dict]) -> None:
        """Hand each frame to a worker. The frames waiting are bounded: past
        the bound one is dropped, and the phone retries or says hello again."""
        for frame in frames:
            if not self._slots.acquire(blocking=False):
                self.dropped += 1
                continue
            try:
                self._pool.submit(self._handle, frame)
            except RuntimeError:  # stopped
                self._slots.release()
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
        finally:
            self._slots.release()

    def _refused_line(self, exc: BaseException) -> None:
        """A refused hello, logged at most once a minute with a count."""
        self._refused += 1
        now = time.monotonic()
        if now - self._refused_at >= REFUSED_LOG_EVERY:
            self.log.info("relay: hello refused: %s (%d since the last such line)",
                          exc, self._refused)
            self._refused_at, self._refused = now, 0

    def _tell_owner(self, dev: Mapping[str, Any]) -> None:
        """The owner's notice of a new device, in the DM channel."""
        if self.notify is None:
            return
        code = str(dev.get("code") or "")
        try:
            self.notify(f"A new device asks to reach Tacitus through the relay: "
                        f"\"{dev.get('name')}\", pairing code {code[:3]} {code[3:]}. "
                        "Approve it under Pair a phone on the tailnet only when the phone "
                        "shows the same code. If you did not pair a phone now, remove it there.")
        except Exception:
            self.log.exception("relay: the notice of a new device failed")

    def _on_hello(self, frame: dict) -> None:
        fid = str(frame.get("id", ""))
        try:
            acc = rc.bot_accept(self.identity, frame, psk_for=self.store.pairing_psk,
                                seen=self._hellos.seen)
            if acc.pairing_id:
                if self.store.pairing_origin(acc.pairing_id) == "relay" \
                        and self.relay_origin_access == "off":
                    raise RelayError("relay-origin devices are off (relay_origin_access)")
                name = acc.info.get("name", "") if isinstance(acc.info, dict) else ""
                dev = self.store.register(acc.pairing_id, acc.device_public, name, code=acc.code)
                self.log.info("relay: paired device %s (%s); it waits for approval",
                              dev["id"], dev["name"])
                self._tell_owner(dev)
            else:
                dev = self.store.device(acc.device_public)
                if dev is None:
                    raise rc.HandshakeError("device not paired")
                if self.level(dev) == "off":
                    raise RelayError("relay-origin devices are off (relay_origin_access)")
        except (rc.HandshakeError, RelayError) as exc:
            self._refused_line(exc)
            self._send(rc.error_frame(fid, "auth"))
            return
        self._add_session(acc.session)
        self._send(acc.welcome)

    def _add_session(self, session: rc.Session) -> None:
        now = time.time()
        with self._mu:
            for sid in [s for s, x in self._sessions.items() if now - x.last_used > SESSION_IDLE]:
                del self._sessions[sid]
            mine = sorted((x for x in self._sessions.values() if x.device == session.device),
                          key=lambda x: x.last_used)
            for old in mine[:max(0, len(mine) + 1 - MAX_DEVICE_SESSIONS)]:
                del self._sessions[old.sid]
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda x: x.last_used)
                del self._sessions[oldest.sid]
            self._sessions[session.sid] = session

    def _standing(self, session: rc.Session) -> str:
        """"" while the session stands; else the error code its frames get:
        "session" when it is gone from here, "auth" when its device was
        removed, waits for approval, or may no longer come in."""
        with self._mu:
            if self._sessions.get(session.sid) is not session:
                return "session"
        device = self.store.device_by_id(session.device)
        if device is None or device.get("pending") or self.level(device) == "off":
            return "auth"
        return ""

    def _session(self, frame: dict) -> tuple[Optional[rc.Session], dict, str]:
        """The frame's session and its device record, or an error code."""
        with self._mu:
            session = self._sessions.get(str(frame.get("sid", "")))
        if session is None:
            return None, {}, "session"
        device = self.store.device_by_id(session.device)
        if device is None or self.level(device) == "off":
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
        if device.get("pending"):
            # Paired, not yet approved on the tailnet: nothing reaches the
            # web chat, and the phone shows its pairing code.
            if kind == "req":
                self._send(session.seal("res", fid, rc.pack(*_response(403, PENDING, pending=True))))
            else:
                self._send(session.seal("end", fid, rc.pack({"s": 403})))
            return
        if kind == "req":
            level = self.level(device)
            if not access_allows(level, str(req.get("m")), str(req.get("p"))):
                out = _response(403, "not allowed from a device paired on the relay's origin")
            else:
                out = self._local_call(req, body, session.device, level)
            self._send(session.seal("res", fid, rc.pack(*out)))
        elif kind == "open":
            self._open_stream(session, fid, req)

    def _headers(self, device: str, extra: Mapping[str, Any] | None = None,
                 level: str = "read", method: str = "GET") -> dict[str, str]:
        """The phone's allowed headers (printable ASCII only), the owner's
        credential, the local origin on a POST, and the relay marks: the
        device id, and whether this request may confirm a card
        (`X-Tacitus-Relay-Cards`). A local server that resolves typed card
        codes skips that on "deny"."""
        headers: dict[str, str] = {}
        for name, value in (extra or {}).items():
            key = str(name).lower()
            if key in FORWARD_HEADERS and isinstance(value, str) \
                    and len(value) <= 256 and re.fullmatch(r"[\x20-\x7e]*", value):
                headers[key] = value
        headers.update(self._owner_headers())
        if method == "POST":
            headers["Origin"] = self.local_origin
        headers["X-Tacitus-Relay"] = device
        headers["X-Tacitus-Relay-Cards"] = "allow" if level == "all" else "deny"
        return headers

    def _local_call(self, req: dict, raw: bytes, device: str,
                    level: str = "read") -> tuple[dict, bytes]:
        """Replay one request on the loopback server: (status and headers,
        body) of its answer. It never opens the event stream (an `open`
        frame does), and gives up at LOCAL_DEADLINE."""
        method, path = req.get("m"), req.get("p")
        if method not in ("GET", "POST"):
            return _response(405, "method not allowed")
        if not path_allowed(path):
            return _response(403, "path not allowed through the relay")
        if _plain_route(path) == EVENTS_PATH:
            return _response(403, "the event stream opens with an open frame")
        body = raw if method == "POST" else None
        if body is not None and len(body) > MAX_BODY:
            return _response(413, "body too large")
        hdrs = req.get("h") if isinstance(req.get("h"), dict) else {}
        request = urllib.request.Request(self.local + path, data=body, method=method,
                                         headers=self._headers(device, hdrs, level, method))
        deadline = time.monotonic() + LOCAL_DEADLINE
        try:
            resp = self._local_opener.open(request, timeout=LOCAL_TIMEOUT)
        except urllib.error.HTTPError as exc:
            resp = exc
        except (urllib.error.URLError, OSError) as exc:
            self.log.warning("relay: local server: %s", exc)
            return _response(502, "the web chat did not answer")
        with resp:
            if (resp.headers.get("Content-Type") or "").lower().startswith("text/event-stream"):
                return _response(502, "an event stream answers only an open frame")
            try:
                data = _read_capped(resp, MAX_BODY + 1, deadline)
            except OSError:
                data = None
            status = resp.status if hasattr(resp, "status") else resp.code
            headers = {k.lower(): v for k, v in resp.headers.items()
                       if k.lower() in RETURN_HEADERS}
        if data is None:
            return _response(504, "the web chat did not answer in time")
        if len(data) > MAX_BODY:
            return _response(502, "response too large for the relay")
        return {"s": int(status), "h": headers}, data

    # event streams

    def _open_stream(self, session: rc.Session, fid: str, req: dict) -> None:
        since = req.get("since")
        st = _Stream(session.device)
        with self._mu:
            # One stream per frame id: a second `open` with the id ends the first.
            old = [self._streams.pop(fid)] if fid in self._streams else []
            while len(self._streams) >= MAX_STREAMS:
                old.append(self._streams.pop(next(iter(self._streams))))
            self._streams[fid] = st
        for o in old:
            o.stop()
        args = (session, fid, since if isinstance(since, int) and since >= 0 else None, st)
        threading.Thread(target=self._stream, args=args, name=f"relay-sse-{fid[:8]}",
                         daemon=True).start()

    def _stream(self, session: rc.Session, fid: str, since: Optional[int], st: _Stream) -> None:
        path = "/api/events" + (f"?since={since}" if since is not None else "")
        headers = self._headers(session.device, {"accept": "text/event-stream"})
        status, resp = 200, None
        if st.ev.is_set():  # replaced before it started: open nothing
            return
        try:
            resp = self._local_opener.open(urllib.request.Request(self.local + path, headers=headers),
                                           timeout=STREAM_READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        except (urllib.error.URLError, OSError):
            status = 502
        try:
            if resp is not None:
                st.resp = resp
                block: list[str] = []
                size = 0
                while not st.ev.is_set() and not self._stop.is_set():
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
                    if event is None or st.ev.is_set():
                        continue
                    # Checked before each event: a removed device's stream
                    # ends here, whatever the phone sends.
                    why = self._standing(session)
                    if why:
                        st.why = why
                        break
                    self._send(session.seal("sse", fid, rc.pack(event)))
        except (OSError, ValueError):
            pass
        finally:
            if resp is not None:
                with contextlib.suppress(Exception):
                    resp.close()
            with self._mu:
                if self._streams.get(fid) is st:
                    del self._streams[fid]
            if not self._stop.is_set():
                if st.why:
                    self._send(rc.error_frame(fid, st.why))
                elif not st.ev.is_set():
                    self._send(session.seal("end", fid, rc.pack({"s": status})))

    def _on_close(self, fid: str) -> None:
        with self._mu:
            st = self._streams.pop(fid, None)
        if st:
            st.stop()


# ── the web front end's hook ──────────────────────────────────────────

def start_relay(frontend_cfg: Mapping[str, Any], local_base_url: str, *,
                logger: Optional[logging.Logger] = None,
                notify: Optional[Callable[[str], Any]] = None) -> Optional[RelayClient]:
    """Start the relay client when the `web` block names a relay.

    `frontend_cfg` is the chat block's `web` dict; its `relay` block holds
    `url`, `key_file` and optionally `install_id`, `app_url` (default: the
    web block's `public_url`), `relay_origin_access` (default "off"),
    `wait` and `enabled`. `local_base_url` is the running web front end's
    loopback base, scheme to base path (`http://127.0.0.1:8470/chat`).
    `notify(text)` posts the owner a notice (a new device). Returns the
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
        store.prune()
        client = RelayClient(str(url), store, local_base_url,
                             owner_headers=lambda: owner_headers_for(frontend_cfg),
                             wait=float(relay.get("wait", DEFAULT_WAIT)),
                             app_url=relay.get("app_url") or frontend_cfg.get("public_url"),
                             public_url=frontend_cfg.get("public_url") or None,
                             relay_origin_access=str(relay.get("relay_origin_access",
                                                               DEFAULT_RELAY_ORIGIN_ACCESS)),
                             notify=notify, logger=lg)
    except (RelayError, ValueError, OSError) as exc:
        lg.error("relay: off, %s", exc)
        return None
    return client.start()


# ── the owner's CLI ───────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="webchat-relay",
                                 description="The web chat relay's key file: create, show, "
                                             "pair, approve, revoke.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("init", "create the key file and print the install id"),
                       ("show", "print the install id and the paired devices"),
                       ("pair", "open a one-time pairing and print it as a QR code"),
                       ("approve", "let a paired device in, once its code matches the phone's"),
                       ("revoke", "remove a paired device")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--key-file", required=True)
        if name in ("init", "pair"):
            p.add_argument("--relay-url", default="")
        if name == "pair":
            p.add_argument("--public-url", default="",
                           help="the web block's public_url: the link opens the app there")
            p.add_argument("--relay-origin", action="store_true",
                           help=f"pair a relay-origin device on purpose ({RELAY_ORIGIN_WARNING}); "
                                "chat.json must set relay_origin_access too")
            p.add_argument("--app-url", default="",
                           help="open another app url (own origin only if it is public_url's)")
            p.add_argument("--print-link", action="store_true",
                           help="print the link as text, not as a QR code")
            p.add_argument("--ttl", type=int, default=rc.PAIRING_TTL)
        if name in ("approve", "revoke"):
            p.add_argument("--device", required=True)
        if name == "approve":
            p.add_argument("--cards", action="store_true",
                           help="also let a relay-origin device press card buttons (Send, "
                                "Create); its code comes from the relay")
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
            devices = [device_row(d) for d in store.devices()]
            print(json.dumps({"install_id": store.identity().install_id,
                              "relay_url": store.relay_url(), "devices": devices,
                              "open_pairings": len([p for p in store.load()["pairings"]
                                                    if p.get("expires", 0) > time.time()])},
                             indent=1))
            return 0
        if args.cmd == "pair":
            url = _check_relay_url(args.relay_url or store.relay_url())
            app = args.app_url or (args.public_url.rstrip("/") + "/" if args.public_url else "")
            origin = origin_of(app, args.public_url, url)
            if origin != "own" and not args.relay_origin:
                print("error: the link would not open the app on the bot's own origin. Give "
                      "--public-url (the web block's public_url). --relay-origin pairs a "
                      "relay-origin device on purpose.", file=sys.stderr)
                return 1
            if origin != "own":
                print(f"warning: {RELAY_ORIGIN_WARNING}. The bot lets it in only when "
                      "chat.json sets relay_origin_access.", file=sys.stderr)
            payload = store.add_pairing(ttl=args.ttl, relay_url=url, origin=origin)
            link = payload.to_string(app or None)
            if args.print_link:
                print(link)
            else:
                from . import relay_qr
                print(relay_qr.terminal(link))
            print("Scan it with the phone; it works once, for ten minutes. Then approve the "
                  "phone here (show, then approve --device <id>) or under Pair a phone on the "
                  "tailnet, when its code matches. Never send this link through chat or mail.",
                  file=sys.stderr)
            return 0
        if args.cmd in ("approve", "revoke"):
            ok = store.approve(args.device, cards=args.cards) if args.cmd == "approve" \
                else store.revoke(args.device)
            print(("approved" if args.cmd == "approve" else "revoked") if ok else "no such device")
            return 0 if ok else 1
    except (RelayError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
