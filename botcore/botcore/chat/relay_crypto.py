"""End-to-end encryption for the web chat relay (docs/webchat-relay.md).

The relay forwards frames between the owner's phone and the bot by install
id. This module makes sure it can read none of them. It is the protocol
with no I/O:

- `BotIdentity`: the bot's X25519 key and relay token, and what derives
  from them (the install id, the client key, the public key).
- `PairingPayload`: what "Pair a phone" shows as a QR code, as a plain
  string (a link whose fragment carries the payload).
- `client_hello`, `bot_accept`, `client_finish`: the handshake. It follows
  the Noise IK pattern (the phone knows the bot's static key from the QR)
  without being wire-compatible with Noise: four X25519 results (es, ss,
  ee, se), and the pairing secret when the phone is pairing, go through
  HKDF-SHA256 into one AES-256-GCM key per direction and a session id.
- `Session`: seals and opens frames with a counter per direction and a
  replay window. Each frame's session id, kind and id are authenticated
  data, so the relay cannot move a sealed body to another request.
- `pack`, `unpack`: a sealed plaintext, a JSON head and a raw body.

The crypto comes from the `cryptography` package (installed with msal).
Without it `AVAILABLE` is False and every key operation raises
`CryptoUnavailable`; the relay stays off and the rest of the web chat
works.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except ImportError:  # the relay stays off; see the module docstring
    AVAILABLE = False

PROTOCOL = b"tacitus-relay/1"
VERSION = 1
# Plaintext is padded to a multiple of this many bytes, so the relay sees a
# frame's size only to this granularity.
PAD = 256
# How far behind the newest counter a frame may arrive and still be taken
# (concurrent requests reach the other side in any order).
WINDOW = 4096
MAX_COUNTER = 1 << 48
# How far a hello's clock may be from the bot's, in seconds. The bot
# remembers a hello's ephemeral key for twice that: a hello stamped
# HELLO_SKEW ahead passes the clock check until 2 * HELLO_SKEW after it came.
HELLO_SKEW = 600
HELLO_CACHE_MAX = 50000
PAIRING_TTL = 600

# Frame kinds. Sealed kinds carry ciphertext under the session keys.
CLIENT_SEALED = frozenset({"req", "open", "ping"})
BOT_SEALED = frozenset({"welcome", "res", "sse", "end"})
# Plaintext control frames: `hello` (phone to bot, its own format), `err`
# (bot or relay to phone, a code from ERR_CODES only) and `close` (relay
# to bot: the phone's event stream went away).
ERR_CODES = frozenset({"session", "auth", "replay", "bad", "busy", "offline",
                       "timeout"})

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SID_RE = re.compile(r"^[0-9a-f]{32}$")
PAIRING_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# A bundle hash: sha256 hex of the bot's signed app manifest.
BUNDLE_RE = re.compile(r"^[0-9a-f]{64}$")
_B64_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_ZERO_NONCE = bytes(12)
_C2B = b"c2b\x00"
_B2C = b"b2c\x00"


class CryptoUnavailable(RuntimeError):
    """The `cryptography` package is not installed."""


class HandshakeError(ValueError):
    """A hello or welcome that does not verify."""


class FrameError(ValueError):
    """A frame that does not open: wrong session, kind, id or tag."""


class ReplayError(FrameError):
    """A frame whose counter was already taken or is too old."""


def _need() -> None:
    if not AVAILABLE:
        raise CryptoUnavailable("the relay needs the cryptography package")


# ── encodings ─────────────────────────────────────────────────────────

def b64e(data: bytes) -> str:
    """URL-safe base64 with no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: Any) -> bytes:
    """The bytes of `b64e` text; FrameError on anything else."""
    if not isinstance(text, str) or not _B64_RE.match(text):
        raise FrameError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise FrameError("not base64url") from exc


def new_id() -> str:
    """A frame id: 16 random bytes."""
    return secrets.token_hex(16)


def install_id_for(token: bytes) -> str:
    """The install id a relay token proves: 26 base32 characters (130 bits)
    of its hash. The relay checks a bot's token against the id in the path
    with this, so it needs no table of installs."""
    digest = hashlib.sha256(b"tacitus-relay install\x00" + token).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:26]


def client_key_for(token: bytes) -> str:
    """The key a paired phone shows the relay with each frame. It keeps
    strangers who learn an install id from spending its limits; it is no
    secret from the relay, which derives it from the bot's token."""
    return b64e(hmac.new(token, b"tacitus-relay client", hashlib.sha256).digest())


def device_id_for(public: bytes) -> str:
    """A paired device's id: 16 hex characters of its public key's hash."""
    return hashlib.sha256(public).hexdigest()[:16]


def _pad(data: bytes) -> bytes:
    body = len(data).to_bytes(4, "big") + data
    return body + b"\x00" * (-len(body) % PAD)


def _unpad(data: bytes) -> bytes:
    if len(data) < 4:
        raise FrameError("short plaintext")
    n = int.from_bytes(data[:4], "big")
    if n > len(data) - 4:
        raise FrameError("bad padding")
    return data[4:4 + n]


def pack(meta: dict, body: bytes = b"") -> bytes:
    """A sealed frame's plaintext: a 4-byte length, a JSON object, then the
    raw body (empty for every kind but `req` and `res`)."""
    head = json.dumps(meta, separators=(",", ":")).encode()
    return len(head).to_bytes(4, "big") + head + body


def unpack(data: bytes) -> tuple[dict, bytes]:
    """(the JSON object, the raw body) of a `pack` plaintext."""
    if len(data) < 4:
        raise FrameError("short plaintext")
    n = int.from_bytes(data[:4], "big")
    if n > len(data) - 4:
        raise FrameError("bad plaintext length")
    try:
        meta = json.loads(data[4:4 + n])
    except ValueError as exc:
        raise FrameError("plaintext head is not JSON") from exc
    if not isinstance(meta, dict):
        raise FrameError("plaintext head is not an object")
    return meta, data[4 + n:]


def _no_constant(name: str) -> Any:
    """json's hook for NaN and Infinity: a hello carries neither."""
    raise ValueError(f"{name} is not a number a hello carries")


def _h(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _raw_public(key: "X25519PrivateKey") -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)


def _raw_private(key: "X25519PrivateKey") -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _dh(private: bytes, public: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(private).exchange(
            X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:  # a low-order or malformed point
        raise HandshakeError("bad key") from exc


def generate_private() -> bytes:
    """A new raw X25519 private key."""
    _need()
    return _raw_private(X25519PrivateKey.generate())


def public_of(private: bytes) -> bytes:
    """The raw public key of a raw X25519 private key."""
    _need()
    return _raw_public(X25519PrivateKey.from_private_bytes(private))


# ── identities and pairing ────────────────────────────────────────────

@dataclass(frozen=True)
class BotIdentity:
    """The bot's long-term relay identity, as the key file holds it."""
    private: bytes = field(repr=False)
    token: bytes = field(repr=False)

    @classmethod
    def generate(cls) -> "BotIdentity":
        return cls(generate_private(), secrets.token_bytes(32))

    @property
    def public(self) -> bytes:
        return public_of(self.private)

    @property
    def install_id(self) -> str:
        return install_id_for(self.token)

    @property
    def client_key(self) -> str:
        return client_key_for(self.token)


@dataclass(frozen=True)
class PairingPayload:
    """What a phone needs to pair: where the relay is, which install, the
    bot's public key, the client key, and a one-time pairing secret.

    Version 2 (`#tr2=`) adds `bundle` (`b`: the hash of the bot's signed
    app bundle, 64 hex) and `app` (`a`: the app URL the link opens), each
    when set; a payload with neither is version 1 (`#tr1=`), which a phone
    still takes. `to_string` gives a link whose fragment carries the
    payload; a browser never sends a fragment to the server, so opening the
    link does not hand the pairing secret to the relay's logs."""
    relay_url: str
    install_id: str
    bot_public: bytes
    client_key: str
    pairing_id: str
    psk: bytes = field(repr=False)
    expires: int
    bundle: str = ""
    app: str = ""

    PREFIXES = {1: "tr1=", 2: "tr2="}

    @property
    def version(self) -> int:
        return 2 if self.bundle or self.app else 1

    def encode(self) -> str:
        obj = {"v": self.version, "r": self.relay_url, "i": self.install_id,
               "k": b64e(self.bot_public), "c": self.client_key,
               "p": self.pairing_id, "s": b64e(self.psk), "x": int(self.expires)}
        if self.bundle:
            obj["b"] = self.bundle
        if self.app:
            obj["a"] = self.app
        return b64e(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode())

    def to_string(self, app_url: Optional[str] = None) -> str:
        base = (app_url or self.app or self.relay_url).split("#", 1)[0]
        last = urlsplit(base).path.rsplit("/", 1)[-1]
        if not base.endswith("/") and "." not in last:  # a folder, not a file
            base += "/"
        return f"{base}#{self.PREFIXES[self.version]}{self.encode()}"

    @classmethod
    def from_string(cls, text: str) -> "PairingPayload":
        frag = text.split("#", 1)[1] if "#" in text else text
        named = next((v for v, pre in cls.PREFIXES.items() if frag.startswith(pre)), None)
        if named:
            frag = frag[len(cls.PREFIXES[named]):]
        try:
            obj = json.loads(b64d(frag.strip()))
        except (FrameError, ValueError) as exc:
            raise HandshakeError("not a pairing payload") from exc
        if not isinstance(obj, dict) or obj.get("v") not in cls.PREFIXES \
                or named not in (None, obj["v"]):
            raise HandshakeError("unknown pairing payload version")
        v2 = obj["v"] == 2
        try:
            out = cls(relay_url=str(obj["r"]), install_id=str(obj["i"]),
                      bot_public=b64d(obj["k"]), client_key=str(obj["c"]),
                      pairing_id=str(obj["p"]), psk=b64d(obj["s"]),
                      expires=int(obj["x"]), bundle=obj.get("b", "") if v2 else "",
                      app=obj.get("a", "") if v2 else "")
        except (KeyError, TypeError, ValueError) as exc:
            raise HandshakeError("incomplete pairing payload") from exc
        if (len(out.bot_public) != 32 or len(out.psk) != 32
                or not PAIRING_ID_RE.match(out.pairing_id)
                or not isinstance(out.bundle, str) or not isinstance(out.app, str)
                or (out.bundle and not BUNDLE_RE.match(out.bundle))
                or (out.app and not _web_url(out.app))
                or (v2 and not (out.bundle or out.app))):
            raise HandshakeError("malformed pairing payload")
        return out


def _web_url(url: str) -> bool:
    """True for an http(s) URL with a host."""
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def new_pairing(identity: BotIdentity, relay_url: str, *, ttl: int = PAIRING_TTL,
                now: Optional[float] = None) -> PairingPayload:
    """A fresh pairing for this identity (the caller stores id and psk)."""
    now = time.time() if now is None else now
    return PairingPayload(relay_url=relay_url, install_id=identity.install_id,
                          bot_public=identity.public, client_key=identity.client_key,
                          pairing_id=secrets.token_hex(16), psk=secrets.token_bytes(32),
                          expires=int(now + ttl))


# ── sessions ──────────────────────────────────────────────────────────

class ReplayWindow:
    """Counters already taken: the newest one and a bitmap of the WINDOW
    before it. A counter older than the window is refused."""

    def __init__(self, size: int = WINDOW) -> None:
        self.size = size
        self.top = -1
        self.bits = 0

    def seen(self, n: int) -> bool:
        if n < 0:
            return True
        if n > self.top:
            return False
        d = self.top - n
        return d >= self.size or bool((self.bits >> d) & 1)

    def mark(self, n: int) -> None:
        if n > self.top:
            shift = n - self.top
            # A jump past the window starts a new bitmap: shifting first
            # would build an integer of `shift` bits (gigabytes for a large n).
            self.bits = 1 if shift >= self.size else \
                ((self.bits << shift) | 1) & ((1 << self.size) - 1)
            self.top = n
        else:
            self.bits |= 1 << (self.top - n)


class Session:
    """One phone-to-bot session: a key and a counter per direction."""

    def __init__(self, sid: str, send_key: bytes, recv_key: bytes, role: str,
                 device: str = "") -> None:
        _need()
        if role not in ("client", "bot"):
            raise ValueError(role)
        self.sid = sid
        self.role = role
        self.device = device
        self.info: dict = {}
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        client = role == "client"
        self._send_dir, self._recv_dir = (_C2B, _B2C) if client else (_B2C, _C2B)
        self._send_kinds = CLIENT_SEALED if client else BOT_SEALED
        self._recv_kinds = BOT_SEALED if client else CLIENT_SEALED
        self._ctr = 0
        self._window = ReplayWindow()
        self._lock = threading.Lock()
        self.created = self.last_used = time.time()

    def _aad(self, kind: str, frame_id: str) -> bytes:
        return b"|".join((PROTOCOL, self.sid.encode(), kind.encode(), frame_id.encode()))

    def _seal_raw(self, kind: str, frame_id: str, data: bytes) -> tuple[int, bytes]:
        if not ID_RE.match(frame_id or ""):
            raise FrameError("bad frame id")
        with self._lock:
            n = self._ctr
            if n >= MAX_COUNTER:
                raise FrameError("session exhausted")
            self._ctr += 1
        nonce = self._send_dir + n.to_bytes(8, "big")
        return n, self._send.encrypt(nonce, _pad(data), self._aad(kind, frame_id))

    def seal(self, kind: str, frame_id: str, data: bytes) -> dict:
        """A sealed frame of `kind` (one this side may send) for `frame_id`."""
        if kind not in self._send_kinds or kind == "welcome":
            raise FrameError(f"{self.role} does not send {kind}")
        n, ct = self._seal_raw(kind, frame_id, data)
        self.last_used = time.time()
        return {"id": frame_id, "k": kind, "sid": self.sid, "n": n, "c": b64e(ct)}

    def _open_raw(self, kind: str, frame_id: str, n: Any, ct: bytes) -> bytes:
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < MAX_COUNTER:
            raise FrameError("bad counter")
        if not ID_RE.match(frame_id if isinstance(frame_id, str) else ""):
            raise FrameError("bad frame id")
        nonce = self._recv_dir + n.to_bytes(8, "big")
        with self._lock:
            if self._window.seen(n):
                raise ReplayError(f"counter {n} replayed or too old")
            try:
                pt = self._recv.decrypt(nonce, ct, self._aad(kind, frame_id))
            except InvalidTag as exc:
                raise FrameError("frame does not authenticate") from exc
            self._window.mark(n)
        self.last_used = time.time()
        return _unpad(pt)

    def open(self, frame: dict) -> bytes:
        """The plaintext of a sealed frame from the other side."""
        if not isinstance(frame, dict):
            raise FrameError("not a frame")
        kind = frame.get("k")
        if kind not in self._recv_kinds or kind == "welcome":
            raise FrameError(f"{self.role} does not take {kind}")
        if frame.get("sid") != self.sid:
            raise FrameError("wrong session")
        return self._open_raw(kind, frame.get("id"), frame.get("n"), b64d(frame.get("c")))


def error_frame(frame_id: str, code: str) -> dict:
    """A plaintext error for the phone: a code from ERR_CODES and nothing else."""
    return {"id": frame_id if ID_RE.match(frame_id or "") else "x",
            "k": "err", "e": code if code in ERR_CODES else "bad"}


# ── the handshake ─────────────────────────────────────────────────────

class HelloCache:
    """Ephemeral keys of the hellos taken in the last 2 * HELLO_SKEW
    seconds: a replayed hello is refused, and an older one fails the clock
    check. Full (`limit` keys), it refuses every new hello until keys age
    out, rather than forget one that could then be replayed."""

    def __init__(self, ttl: float = 2 * HELLO_SKEW, limit: int = HELLO_CACHE_MAX) -> None:
        self.ttl = ttl
        self.limit = limit
        self._seen: "OrderedDict[bytes, float]" = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, key: bytes, now: Optional[float] = None) -> bool:
        """True when `key` was taken before (or the cache is full); records
        it otherwise."""
        now = time.time() if now is None else now
        with self._lock:
            while self._seen and now - next(iter(self._seen.values())) > self.ttl:
                self._seen.popitem(last=False)
            if key in self._seen or len(self._seen) >= self.limit:
                return True
            self._seen[key] = now
            return False


@dataclass
class PendingHello:
    """The phone's half of a handshake, kept until the welcome arrives."""
    frame_id: str
    e_private: bytes = field(repr=False)
    s_private: bytes = field(repr=False)
    psk: bytes = field(repr=False)
    es: bytes = field(repr=False)
    ss: bytes = field(repr=False)
    h: bytes = field(repr=False)


@dataclass
class Accepted:
    """A hello that verified. The caller checks the device (registered, or
    pairing with `pairing_id`) before it sends `welcome`. `code` is the
    hello's pairing code (`pairing_code`), which the phone shows too."""
    session: Session
    welcome: dict
    device_public: bytes
    pairing_id: Optional[str]
    info: dict
    code: str = ""


def pairing_code(h: bytes) -> str:
    """Six digits from a hello's transcript hash (after c_p). The phone and
    the bot each compute them; the owner approves a new device on the
    tailnet only when the code there matches the one on the phone."""
    n = int.from_bytes(_h(PROTOCOL, b" code\x00", h)[:4], "big") % 1_000_000
    return f"{n:06d}"


def _header(pairing_id: Optional[str]) -> bytes:
    if pairing_id is None:
        return bytes([VERSION, 0])
    if not PAIRING_ID_RE.match(pairing_id):
        raise HandshakeError("bad pairing id")
    return bytes([VERSION, 1]) + bytes.fromhex(pairing_id)


def _h0(install_id: str, bot_public: bytes, header: bytes, e_public: bytes) -> bytes:
    return _h(PROTOCOL, b"\x00", install_id.encode(), b"\x00", bot_public, header, e_public)


def _session_keys(ikm: bytes, h: bytes) -> tuple[bytes, bytes, str]:
    okm = _hkdf(ikm, h, b"tacitus-relay/1 session", 80)
    return okm[:32], okm[32:64], okm[64:80].hex()


def client_hello(bot_public: bytes, install_id: str, device_private: bytes, *,
                 pairing: Optional[tuple[str, bytes]] = None,
                 info: Optional[dict] = None, frame_id: Optional[str] = None,
                 now: Optional[float] = None) -> tuple[dict, PendingHello]:
    """The phone's hello: its ephemeral key in the clear, its static key
    sealed under es, and a payload (the clock, a device name) sealed under
    es and ss. `pairing` is (pairing id, psk) from the QR on a first
    contact; the psk then goes into every key, so only a phone that saw the
    QR can pair."""
    _need()
    if len(bot_public) != 32:
        raise HandshakeError("bad bot key")
    pid, psk = pairing if pairing else (None, b"")
    if pairing and len(psk) != 32:
        raise HandshakeError("bad pairing secret")
    header = _header(pid)
    e = X25519PrivateKey.generate()
    e_priv, e_pub = _raw_private(e), _raw_public(e)
    s_pub = public_of(device_private)
    h = _h0(install_id, bot_public, header, e_pub)
    es = _dh(e_priv, bot_public)
    c_s = AESGCM(_hkdf(es + psk, h, b"tacitus-relay/1 static")).encrypt(_ZERO_NONCE, s_pub, h)
    h = _h(h, c_s)
    ss = _dh(device_private, bot_public)
    body = dict(info or {})
    body["t"] = int(time.time() if now is None else now)
    c_p = AESGCM(_hkdf(es + ss + psk, h, b"tacitus-relay/1 hello")).encrypt(
        _ZERO_NONCE, _pad(json.dumps(body).encode()), h)
    h = _h(h, c_p)
    fid = frame_id or new_id()
    frame = {"id": fid, "k": "hello", "c": b64e(header + e_pub + c_s + c_p)}
    return frame, PendingHello(fid, e_priv, device_private, psk, es, ss, h)


def bot_accept(identity: BotIdentity, frame: dict, *,
               psk_for: Callable[[str], Optional[bytes]],
               seen: Optional[Callable[[bytes], bool]] = None,
               now: Optional[float] = None) -> Accepted:
    """Verify a hello and make the bot's half of the session. `psk_for`
    gives the pairing secret of an open pairing id (None when there is
    none); `seen` returns True for an ephemeral key already used."""
    _need()
    if not isinstance(frame, dict) or frame.get("k") != "hello":
        raise HandshakeError("not a hello")
    fid = frame.get("id")
    if not isinstance(fid, str) or not ID_RE.match(fid):
        raise HandshakeError("bad frame id")
    try:
        blob = b64d(frame.get("c"))
    except FrameError as exc:
        raise HandshakeError("bad hello") from exc
    if len(blob) < 2 or blob[0] != VERSION or blob[1] not in (0, 1):
        raise HandshakeError("unknown hello version")
    pid = None
    psk = b""
    off = 2
    if blob[1] == 1:
        if len(blob) < 18:
            raise HandshakeError("short hello")
        pid = blob[2:18].hex()
        off = 18
        psk = psk_for(pid) or b""
        if len(psk) != 32:
            raise HandshakeError("no such pairing")
    header = blob[:off]
    e_pub = blob[off:off + 32]
    c_s = blob[off + 32:off + 80]
    c_p = blob[off + 80:]
    if len(e_pub) != 32 or len(c_s) != 48 or len(c_p) < 16 + PAD:
        raise HandshakeError("short hello")
    h = _h0(identity.install_id, identity.public, header, e_pub)
    es = _dh(identity.private, e_pub)
    try:
        s_pub = AESGCM(_hkdf(es + psk, h, b"tacitus-relay/1 static")).decrypt(_ZERO_NONCE, c_s, h)
        h = _h(h, c_s)
        ss = _dh(identity.private, s_pub)
        body = _unpad(AESGCM(_hkdf(es + ss + psk, h, b"tacitus-relay/1 hello")).decrypt(
            _ZERO_NONCE, c_p, h))
    except (InvalidTag, FrameError) as exc:
        raise HandshakeError("hello does not authenticate") from exc
    h = _h(h, c_p)
    try:
        info = json.loads(body, parse_constant=_no_constant)
        t = float(info["t"])
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise HandshakeError("bad hello payload") from exc
    now = time.time() if now is None else now
    if not math.isfinite(t) or abs(now - t) > HELLO_SKEW:
        raise HandshakeError("hello clock out of range")
    if seen is not None and seen(e_pub):
        raise HandshakeError("hello replayed")
    code = pairing_code(h)
    eb = X25519PrivateKey.generate()
    eb_priv, eb_pub = _raw_private(eb), _raw_public(eb)
    ee = _dh(eb_priv, e_pub)
    se = _dh(eb_priv, s_pub)
    h = _h(h, eb_pub)
    k_c2b, k_b2c, sid = _session_keys(es + ss + ee + se + psk, h)
    session = Session(sid, k_b2c, k_c2b, "bot", device=device_id_for(s_pub))
    n, ct = session._seal_raw("welcome", fid, pack({"device": session.device, "t": int(now)}))
    welcome = {"id": fid, "k": "welcome", "sid": sid, "n": n, "c": b64e(eb_pub + ct)}
    return Accepted(session, welcome, s_pub, pid, info if isinstance(info, dict) else {}, code)


def client_finish(pending: PendingHello, frame: dict) -> Session:
    """The phone's session from the bot's welcome. Only the holder of the
    bot's private key can make a welcome that opens here."""
    _need()
    if not isinstance(frame, dict) or frame.get("k") != "welcome" \
            or frame.get("id") != pending.frame_id:
        raise HandshakeError("not the welcome for this hello")
    try:
        blob = b64d(frame.get("c"))
    except FrameError as exc:
        raise HandshakeError("bad welcome") from exc
    eb_pub, ct = blob[:32], blob[32:]
    if len(eb_pub) != 32:
        raise HandshakeError("short welcome")
    ee = _dh(pending.e_private, eb_pub)
    se = _dh(pending.s_private, eb_pub)
    h = _h(pending.h, eb_pub)
    k_c2b, k_b2c, sid = _session_keys(pending.es + pending.ss + ee + se + pending.psk, h)
    if frame.get("sid") != sid:
        raise HandshakeError("welcome names another session")
    session = Session(sid, k_c2b, k_b2c, "client")
    try:
        session.info = unpack(session._open_raw("welcome", pending.frame_id,
                                                frame.get("n"), ct))[0]
    except FrameError as exc:
        raise HandshakeError("welcome does not authenticate") from exc
    session.device = str(session.info.get("device", ""))
    return session
