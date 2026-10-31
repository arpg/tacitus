"""Web Push for the Tacitus web chat (docs/webchat.md, Notifications).

A browser that turns notifications on subscribes at the push service its
vendor runs (Google, Mozilla, Apple, Microsoft) and gives the bot the
subscription: an endpoint URL on that service and two keys. To notify,
the bot encrypts a small JSON payload to those keys and POSTs it to the
endpoint; the service wakes the device and the app's service worker
shows the notification. The push service carries only ciphertext.

- VAPID (RFC 8292): each POST carries a JWT signed with the bot's P-256
  key (ES256) and the public key itself, so the push service takes pushes
  for the subscription only from this bot. The key pair is made on first
  use and kept in `webchat-vapid.json` beside the store (mode 0600).
- Payload encryption (RFC 8291 over RFC 8188, `aes128gcm`): an ephemeral
  P-256 key agrees a secret with the browser's key, HMAC-SHA-256 steps
  with the browser's auth secret and a random salt derive the key and the
  nonce, and AES-128-GCM seals the payload in one record.

The bot POSTs only to https URLs on the known push services (PUSH_HOSTS,
exact names) and follows no redirect: a subscription is input from a
browser, and it must not steer the bot into calling another host. A 404
or 410 answer drops the subscription; a 429 holds that endpoint back for
its Retry-After; any other failure is counted on the row and logged by
host. Before each send the subscriptions are pruned (`Pusher.prune`):
all of them when the pairing token changed since they were made (the
owner replaced the token to cut devices off), and each one the app has
not posted again for 30 days.

Each payload is padded to a multiple of 256 bytes before it is sealed,
so its length tells the push service little about the text.

Sending runs on one worker thread (`Pusher.start`). A push for a later
time (an alert) waits in the store's `push_queue` table, keyed by its
tag, until the worker that runs in the bot process sends it; a process
outside the bot (alert.py) writes it there.

The crypto comes from the `cryptography` package (installed with msal).
Without it `AVAILABLE` is False and push is off; the chat works.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    AVAILABLE = True
except ImportError:  # push stays off; see the module docstring
    AVAILABLE = False

log = logging.getLogger("chat.web.push")

KEY_FILE = "webchat-vapid.json"
# The `sub` claim of the VAPID JWT: a contact for the push service. The
# web block's `push_subject` (a mailto: or https: URL) replaces it.
DEFAULT_SUBJECT = "https://github.com/arpg/tacitus"
# Seconds a push service holds a push for a device that is offline.
TTL_S = 3600
# At most one push per thread (per urgency) in this many seconds.
MIN_GAP_S = 30.0
JWT_LIFE_S = 12 * 3600
TIMEOUT_S = 10.0
# Seconds between the worker's looks at the push queue.
DUE_POLL_S = 15.0
RECORD_SIZE = 4096
# Payload bytes: the record (4096) holds the payload, its delimiter and
# the tag, and the push services take a body of 4096 bytes.
MAX_PAYLOAD = 3000
MAX_SUBSCRIPTIONS = 20
URGENCIES = ("very-low", "low", "normal", "high")
# The push services' hosts, by exact name. Chrome's endpoints are on
# fcm.googleapis.com and, lately, on hosts such as jmt17.google.com;
# Windows' are one name under notify.windows.com.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com")
PUSH_HOST_PATTERNS = (re.compile(r"^jmt\d+\.google\.com$"),
                      re.compile(r"^[a-z0-9-]+\.notify\.windows\.com$"))
# A sealed record's plaintext (payload, delimiter, padding) is a multiple
# of this many bytes.
PAD_BLOCK = 256
# A subscription the app has not posted again for this long is dropped:
# the app posts its own at every start.
SUBSCRIPTION_MAX_AGE_S = 30 * 86400

_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
_SLACK_LINK_RE = re.compile(r"<(?:https?|mailto):[^|>\s]+\|([^>]*)>")
_BARE_LINK_RE = re.compile(r"<((?:https?|mailto):[^|>\s]+)>")
_MARKS_RE = re.compile(r"\*\*|__|~~|[*`]|(?<!\w)_|_(?!\w)")
_HIDDEN_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩"
                        "﻿]")


class PushUnavailable(RuntimeError):
    """The cryptography package is missing; push is off."""


def _need() -> None:
    if not AVAILABLE:
        raise PushUnavailable("web push needs the cryptography package")


def b64u(data: bytes) -> str:
    """base64url without padding (RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: Any) -> bytes:
    """Bytes from base64url, with or without padding; ValueError for
    anything else."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", text):
        raise ValueError("not base64url")
    s = text.rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def plain_line(text: Any, limit: int = 200) -> str:
    """The first non-empty line of `text` as a notification shows it: no
    emoji shortcodes, Markdown or mrkdwn marks, inert-span sentinels or
    control characters, at most `limit` characters."""
    for raw in str(text or "").splitlines():
        line = _HIDDEN_RE.sub("", raw)
        line = _MD_LINK_RE.sub(r"\1", line)
        line = _SLACK_LINK_RE.sub(r"\1", line)
        line = _BARE_LINK_RE.sub(r"\1", line)
        line = _SHORTCODE_RE.sub("", line)
        line = _MARKS_RE.sub("", line).lstrip("#> \t")
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."
    return ""


def endpoint_allowed(url: Any) -> bool:
    """True for an https URL on a known push service, with no user info
    and the default port."""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or port not in (None, 443):
        return False
    return host in PUSH_HOSTS or any(r.match(host) for r in PUSH_HOST_PATTERNS)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key: Any) -> bytes:
    """The uncompressed P-256 point (65 bytes) of a private key."""
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def private_key(raw: bytes) -> Any:
    """A P-256 private key from its 32-byte scalar."""
    _need()
    if len(raw) != 32:
        raise ValueError("a P-256 private key is 32 bytes")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def check_public(ua_public: bytes) -> None:
    """ValueError unless `ua_public` is an uncompressed point on P-256."""
    _need()
    if len(ua_public) != 65 or ua_public[0] != 4:
        raise ValueError("the key is not an uncompressed P-256 point")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)


def derive(ecdh_secret: bytes, auth_secret: bytes, ua_public: bytes, as_public: bytes,
           salt: bytes) -> tuple[bytes, bytes]:
    """(CEK, NONCE) for one message: RFC 8291 section 3.4 combines the
    ECDH secret with the auth secret into IKM, and RFC 8188 section 2.2
    derives the content key and the nonce from IKM and the salt."""
    key_info = b"WebPush: info\x00" + ua_public + as_public
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, key_info + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    return cek, nonce


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            salt: bytes | None = None, as_private: bytes | None = None,
            rs: int = RECORD_SIZE, pad_to: int = 0) -> bytes:
    """The aes128gcm body of one push: the header (salt, record size, the
    sender's public key) and one sealed record. `salt` and `as_private`
    (the sender's 32-byte scalar) are fresh unless given; a test passes
    them to reproduce RFC 8291's example. With `pad_to`, zero bytes after
    the delimiter (RFC 8188 section 2) make the record's plaintext a
    multiple of that many bytes, as far as the record holds."""
    _need()
    check_public(ua_public)
    if len(auth_secret) != 16:
        raise ValueError("the auth secret is 16 bytes")
    if len(plaintext) + 1 + 16 > rs:
        raise ValueError("the payload does not fit in one record")
    pad = 0
    if pad_to > 0:
        size = len(plaintext) + 1
        pad = min(-size % pad_to, rs - 16 - size)
    key = private_key(as_private) if as_private is not None else ec.generate_private_key(
        ec.SECP256R1())
    as_public = _public_bytes(key)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    secret = key.exchange(ec.ECDH(), peer)
    salt = secrets.token_bytes(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("the salt is 16 bytes")
    cek, nonce = derive(secret, auth_secret, ua_public, as_public, salt)
    # One record, the last: the plaintext, the delimiter 0x02, the padding.
    sealed = AESGCM(cek).encrypt(nonce, plaintext + b"\x02" + b"\x00" * pad, None)
    return salt + struct.pack(">IB", rs, len(as_public)) + as_public + sealed


class Vapid:
    """The bot's VAPID key pair (RFC 8292), made on first use, and the
    Authorization header of a push."""

    def __init__(self, key_file: str | Path, subject: str = DEFAULT_SUBJECT) -> None:
        self.key_file = Path(key_file)
        self.subject = subject or DEFAULT_SUBJECT
        self._key: Any = None
        self._public = b""
        self._tokens: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def _read(self) -> bytes | None:
        try:
            data = json.loads(self.key_file.read_text())
        except FileNotFoundError:
            return None
        return unb64u(data["private"])

    def _create(self) -> bytes:
        """Write a new key file; when another process wrote one first, its
        key stands, since every subscription is bound to one key."""
        key = ec.generate_private_key(ec.SECP256R1())
        raw = key.private_numbers().private_value.to_bytes(32, "big")
        body = json.dumps({"private": b64u(raw), "public": b64u(_public_bytes(key)),
                           "created_at": int(time.time())})
        self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.key_file.with_name(f".{self.key_file.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, self.key_file)
                log.info("web push: a VAPID key was made at %s", self.key_file)
            except FileExistsError:
                pass
            except OSError:  # a file system without hard links
                if not self.key_file.exists():
                    os.replace(tmp, self.key_file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        return self._read() or raw

    def key(self) -> Any:
        _need()
        with self._lock:
            if self._key is None:
                self._key = private_key(self._read() or self._create())
                self._public = _public_bytes(self._key)
            return self._key

    @property
    def public_key(self) -> str:
        """The public key as the browser's applicationServerKey takes it:
        the uncompressed point, base64url."""
        self.key()
        return b64u(self._public)

    def token(self, audience: str, now: float | None = None) -> str:
        """The signed JWT for one push service origin, reused until an hour
        before it expires."""
        key = self.key()
        now = int(time.time() if now is None else now)
        with self._lock:
            hit = self._tokens.get(audience)
            if hit and hit[1] - now > 3600:
                return hit[0]
        exp = now + JWT_LIFE_S
        head = b64u(b'{"typ":"JWT","alg":"ES256"}')
        claims = b64u(json.dumps({"aud": audience, "exp": exp, "sub": self.subject},
                                 separators=(",", ":")).encode("utf-8"))
        signing = f"{head}.{claims}".encode("ascii")
        r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
        jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        with self._lock:
            self._tokens[audience] = (jwt, exp)
        return jwt

    def authorization(self, endpoint: str, now: float | None = None) -> str:
        """`vapid t=<JWT>, k=<public key>` for a push to `endpoint`; the
        JWT's audience is the endpoint's origin."""
        parts = urllib.parse.urlsplit(endpoint)
        audience = f"{parts.scheme}://{parts.netloc}"
        return f"vapid t={self.token(audience, now)}, k={self.public_key}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service answers; it never sends the bot elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Pusher:
    """Pushes to every subscription a WebStore holds."""

    def __init__(self, store: Any, key_file: str | Path, *,
                 subject: str = DEFAULT_SUBJECT,
                 pair_hash: Any = None) -> None:
        self.store = store
        self.vapid = Vapid(key_file, subject)
        # The hash of the pairing token now (a callable; "" when there is
        # none): a change drops every subscription (prune).
        self.pair_hash = pair_hash
        self.max_age_s = SUBSCRIPTION_MAX_AGE_S
        # The check every endpoint passes, at subscription and at send.
        self.allow = endpoint_allowed
        self.ttl = TTL_S
        self.timeout = TIMEOUT_S
        self.min_gap_s = MIN_GAP_S
        self.due_poll_s = DUE_POLL_S
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._jobs: queue.Queue = queue.Queue()
        self._last: dict[tuple[str, bool], float] = {}
        self._retry_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # the worker

    def start(self) -> None:
        """Run the worker: queued pushes, and due ones from the store."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="webchat-push", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(None)

    def _run(self) -> None:
        next_due = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= next_due:
                for payload, urgency in self._due(now):
                    self._send_logged(payload, urgency)
                next_due = now + self.due_poll_s
            try:
                job = self._jobs.get(timeout=max(0.05, next_due - time.time()))
            except queue.Empty:
                continue
            if job is None:
                break
            self._send_logged(*job)

    def _due(self, now: float) -> list[tuple[bytes, str]]:
        """Claim the queued pushes now due; one more than a TTL late is
        dropped, since its moment has passed."""
        try:
            rows = self.store.push_claim_due(now)
        except Exception:
            log.exception("web push: the push queue could not be read")
            return []
        out = []
        for row in rows:
            if row["due_at"] < now - self.ttl:
                log.info("web push: a push for %s is past its moment; dropped", row["tag"])
                continue
            out.append((row["payload"].encode("utf-8"), row["urgency"]))
        return out

    def _send_logged(self, payload: bytes, urgency: str) -> None:
        try:
            self.send_all(payload, urgency)
        except Exception:
            log.exception("web push: a push failed")

    # notifying

    def payload(self, title: str, body: str = "", url: str = "", tag: str = "") -> bytes:
        """The JSON the service worker shows: title, body, url, tag."""
        data = {"title": plain_line(title, 120) or "Tacitus", "body": plain_line(body, 240),
                "url": str(url or "")[:1000], "tag": str(tag or "")[:200]}
        out = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(out) > MAX_PAYLOAD:
            data["url"] = ""
            out = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return out

    def _slot(self, key: str, high: bool, now: float) -> bool:
        """True when this thread had no push of this urgency class in the
        last min_gap_s seconds, and takes the slot."""
        with self._lock:
            if len(self._last) > 1000:
                self._last = {k: t for k, t in self._last.items() if now - t < self.min_gap_s}
            last = self._last.get((key, high))
            if last is not None and now - last < self.min_gap_s:
                return False
            self._last[(key, high)] = now
            return True

    def notify(self, title: str, body: str = "", url: str = "", tag: str = "", *,
               urgency: str = "normal", at: float | None = None, wait: bool = False) -> dict:
        """Push a notification to every subscription. `tag` names the thread:
        the device shows one notification per tag, and a thread gets at most
        one push per min_gap_s, counted apart for high urgency, so a card is
        never held back by a message push. `at` (a Unix time ahead) queues
        it in the store under its tag, replacing one queued before. `wait`
        sends on this thread and returns the counts, for a process that
        exits next; otherwise the worker sends it."""
        _need()
        urgency = urgency if urgency in URGENCIES else "normal"
        data = self.payload(title, body, url, tag)
        now = time.time()
        if at is not None and at > now + 1:
            self.store.push_defer(tag or f"push:{secrets.token_hex(6)}", at, data.decode("utf-8"),
                                  urgency)
            return {"queued_for": at}
        if not self.store.push_count():
            return {"sent": 0, "subscriptions": 0}
        if not self._slot(tag or url or title, urgency == "high", now):
            return {"sent": 0, "skipped": "rate limit"}
        if wait:
            return self.send_all(data, urgency)
        self.start()
        self._jobs.put((data, urgency))
        return {"queued": True}

    def cancel(self, tag: str) -> bool:
        """Drop the push queued under `tag`; True when there was one."""
        return bool(tag) and self.store.push_cancel(tag)

    # the subscriptions

    def prune(self, now: float | None = None) -> int:
        """Drop the subscriptions that may no longer be the owner's: all of
        them when the pairing token changed since they were made, and each
        one the app has not posted again for max_age_s. The number dropped;
        never raises."""
        now = time.time() if now is None else now
        dropped = 0
        try:
            if callable(self.pair_hash):
                dropped += self.store.push_check_token(self.pair_hash())
            dropped += self.store.push_drop_stale(now - self.max_age_s)
        except Exception:
            log.exception("web push: the subscriptions could not be pruned")
        return dropped

    # sending

    def send_all(self, payload: bytes, urgency: str = "normal") -> dict:
        counts = {"sent": 0, "failed": 0, "dropped": 0, "held": 0}
        now = time.time()
        self.prune(now)
        for sub in self.store.push_subscriptions():
            endpoint = sub["endpoint"]
            if self._retry_at.get(endpoint, 0.0) > now:
                counts["held"] += 1
                continue
            counts[self.send_one(sub, payload, urgency)] += 1
        return counts

    def send_one(self, sub: dict, payload: bytes, urgency: str = "normal") -> str:
        """POST one push; "sent", "failed" or "dropped"."""
        endpoint = sub["endpoint"]
        host = urllib.parse.urlsplit(endpoint).hostname or "?"
        if not self.allow(endpoint):
            log.warning("web push: %s is not a push service; the subscription is dropped", host)
            self.store.push_unsubscribe(endpoint)
            return "dropped"
        try:
            keys = sub["keys"] if isinstance(sub["keys"], dict) else json.loads(sub["keys"])
            body = encrypt(payload, unb64u(keys["p256dh"]), unb64u(keys["auth"]),
                           pad_to=PAD_BLOCK)
            req = urllib.request.Request(endpoint, data=body, method="POST", headers={
                "Authorization": self.vapid.authorization(endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(int(self.ttl)),
                "Urgency": urgency,
            })
        except (KeyError, TypeError, ValueError) as e:
            log.warning("web push: a subscription at %s is unusable (%s); dropped", host, e)
            self.store.push_unsubscribe(endpoint)
            return "dropped"
        retry_after = ""
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            retry_after = e.headers.get("Retry-After") or ""
            e.close()
        except (urllib.error.URLError, OSError) as e:
            log.warning("web push: %s unreachable: %s", host, getattr(e, "reason", e))
            self.store.push_result(endpoint, False)
            return "failed"
        if 200 <= status < 300:
            self.store.push_result(endpoint, True)
            self._retry_at.pop(endpoint, None)
            return "sent"
        if status in (404, 410):
            log.info("web push: %s says the subscription is gone (%d); dropped", host, status)
            self.store.push_unsubscribe(endpoint)
            return "dropped"
        if status == 429:
            wait = int(retry_after) if retry_after.isdigit() else 60
            self._retry_at[endpoint] = time.time() + min(max(wait, 1), 3600)
            log.warning("web push: %s asks to slow down; held for %d s", host, wait)
        else:
            log.warning("web push: %s answered HTTP %d", host, status)
        self.store.push_result(endpoint, False)
        return "failed"
