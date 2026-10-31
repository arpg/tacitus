"""Web Push for the web chat (botcore/botcore/chat/web_push.py): the
aes128gcm encryption against RFC 8291's worked example and a decryptor
written here from the RFCs, the VAPID JWT, the subscription routes, the
pushes the front end sends (to a stand-in push service on loopback), the
queue for later pushes, and the alert path's helpers in chat.direct."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))

from chat import web_frontend as wf  # noqa: E402
from chat.cards import CardAction, CardField, ConfirmationCard  # noqa: E402
from chat.types import MessageRef  # noqa: E402
from chat.web_frontend import WebFrontend, WebStore  # noqa: E402
import chat.direct as direct  # noqa: E402
from test_web_frontend import call, make, owner  # noqa: E402

wp = wf.web_push
if not wp.AVAILABLE:
    pytest.skip("web push needs the cryptography package", allow_module_level=True)

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

# RFC 8291, section 5 and appendix A, joined across the RFC's line breaks.
RFC = {
    "plaintext": "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24",
    "as_public": "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "ua_private": "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94",
    "auth_secret": "BTBZMqHH6r4Tts7J_aSIgg",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "ecdh_secret": "kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs",
    "cek": "oIhVW04MRdy2XN9CiKLxTg",
    "nonce": "4h_95klXJ5E_qnoN",
    "header": ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
               "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"),
    "ciphertext": ("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs"
                   "bI_0LpXMuGvnzQ"),
    "body": ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
             "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
             "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"),
}
B = {k: wp.unb64u(v) for k, v in RFC.items()}

FCM = "https://fcm.googleapis.com/fcm/send/made-up-subscription"


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _point(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def decrypt(body: bytes, ua_key, auth: bytes) -> bytes:
    """An aes128gcm push body opened as a browser opens it, written from
    RFC 8188 section 2 and RFC 8291 section 3.4, apart from web_push.py."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, record = body[21:21 + idlen], body[21 + idlen:]
    assert len(record) <= rs
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    secret = ua_key.exchange(ec.ECDH(), peer)
    ikm = _hmac(_hmac(auth, secret), b"WebPush: info\x00" + _point(ua_key) + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    padded = AESGCM(cek).decrypt(nonce, record, None).rstrip(b"\x00")
    assert padded.endswith(b"\x02"), "one record, the last"
    return padded[:-1]


def browser() -> tuple:
    """(key, subscription keys) of a made-up browser."""
    key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    return key, auth, {"p256dh": wp.b64u(_point(key)), "auth": wp.b64u(auth)}


class FakePushService:
    """A push service on loopback: records each POST, answers `status`."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 201
        self.headers: dict[str, str] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "body": self.rfile.read(n),
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                self.send_response(outer.status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, n: int, timeout: float = 5.0) -> list[dict]:
        end = time.time() + timeout
        while len(self.requests) < n and time.time() < end:
            time.sleep(0.02)
        assert len(self.requests) >= n, f"{len(self.requests)} pushes, wanted {n}"
        return self.requests

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def svc():
    s = FakePushService()
    yield s
    s.close()


@pytest.fixture
def fe(tmp_path):
    f = make(tmp_path)
    f.start()
    f.push.allow = lambda url: url.startswith("http://127.0.0.1:")  # the stand-in service
    yield f
    f.stop()


def subscribe_fake(f: WebFrontend, svc: FakePushService, name: str = "sub-1"):
    key, auth, keys = browser()
    f.store.push_subscribe(f"{svc.origin}/push/{name}", keys)
    return key, auth


def card(**kw) -> ConfirmationCard:
    args = dict(kind="send", title=":lock: *Confirm send*",
                fields=[CardField("To", "someone@example.test"), CardField("Subject", "Plans")],
                body="Hello there.\nMore.",
                actions=[CardAction("approve_send", "Send", {"token": "t-1"}, "primary", "send")])
    args.update(kw)
    return ConfirmationCard(**args)


def spy(f: WebFrontend) -> list[tuple]:
    """Record each push the front end asks for, and what came of it."""
    got: list[tuple] = []
    real = f.push.notify

    def notify(title, body="", url="", tag="", **kw):
        out = real(title, body, url, tag, **kw)
        got.append((title, body, url, tag, kw.get("urgency", "normal"), out))
        return out
    f.push.notify = notify
    return got


# the encryption


def test_rfc8291_example_encrypts_to_the_rfcs_body():
    """RFC 8291 section 5 with its keys and salt, step by step and whole."""
    ua_key = wp.private_key(B["ua_private"])
    as_key = wp.private_key(B["as_private"])
    assert _point(ua_key) == B["ua_public"] and _point(as_key) == B["as_public"]
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), B["as_public"])
    assert ua_key.exchange(ec.ECDH(), peer) == B["ecdh_secret"]
    cek, nonce = wp.derive(B["ecdh_secret"], B["auth_secret"], B["ua_public"], B["as_public"],
                           B["salt"])
    assert (cek, nonce) == (B["cek"], B["nonce"])
    body = wp.encrypt(B["plaintext"], B["ua_public"], B["auth_secret"], salt=B["salt"],
                      as_private=B["as_private"])
    assert body == B["header"] + B["ciphertext"] == B["body"]
    assert wp.b64u(body) == RFC["body"]
    assert decrypt(body, ua_key, B["auth_secret"]) == b"When I grow up, I want to be a watermelon"


def test_encryption_round_trips_with_fresh_keys():
    key, auth, keys = browser()
    ua_public = wp.unb64u(keys["p256dh"])
    for text in (b"", b"x", json.dumps({"title": "T", "body": "b" * 2900}).encode()):
        a = wp.encrypt(text, ua_public, auth)
        b = wp.encrypt(text, ua_public, auth)
        assert a[:16] != b[:16] and a[21:86] != b[21:86]   # a fresh salt and sender key each time
        assert int.from_bytes(a[16:20], "big") == 4096 and a[20] == 65
        assert decrypt(a, key, auth) == text == decrypt(b, key, auth)
    with pytest.raises(ValueError):
        wp.encrypt(b"x" * 4080, ua_public, auth)                 # more than one record holds
    with pytest.raises(ValueError):
        wp.encrypt(b"x", ua_public[:-1] + b"\x00", auth)        # not a point on the curve
    with pytest.raises(ValueError):
        wp.encrypt(b"x", ua_public, auth[:8])


def test_base64url_and_the_notification_line():
    assert wp.unb64u(wp.b64u(b"\xff\xfe\x00")) == b"\xff\xfe\x00"
    assert wp.unb64u("AQ==") == wp.unb64u("AQ") == b"\x01"
    for bad in ("a+b/", "a b", None, 7):
        with pytest.raises(ValueError):
            wp.unb64u(bad)
    assert wp.plain_line(":lock: *Confirm send*") == "Confirm send"
    assert wp.plain_line("\n\n**Done:** see [the page](https://e.example/x) <https://e.example|here>") \
        == "Done: see the page here"
    assert wp.plain_line("> quoted `code` and snake_case") == "quoted code and snake_case"
    assert wp.plain_line("*literal* ‮flip") == "literal flip"
    assert wp.plain_line("x" * 500, 50) == "x" * 47 + "..."
    assert wp.plain_line(":eyes:") == "" and wp.plain_line(None) == ""


# VAPID


def test_vapid_jwt_is_es256_for_the_endpoint_origin(tmp_path):
    v = wp.Vapid(tmp_path / "priv" / wp.KEY_FILE, "mailto:owner@example.test")
    now = 1_800_000_000
    auth = v.authorization(FCM, now=now)
    m = re.fullmatch(r"vapid t=([\w-]+\.[\w-]+\.[\w-]+), k=([\w-]+)", auth)
    assert m, auth
    jwt, k = m.groups()
    assert k == v.public_key and len(wp.unb64u(k)) == 65 and wp.unb64u(k)[0] == 4
    head, claims, sig = jwt.split(".")
    assert json.loads(wp.unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(wp.unb64u(claims)) == {"aud": "https://fcm.googleapis.com",
                                              "exp": now + 12 * 3600,
                                              "sub": "mailto:owner@example.test"}
    raw = wp.unb64u(sig)
    assert len(raw) == 64   # r || s, not DER (RFC 7518 section 3.4)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.unb64u(k))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    forged = wp.b64u(json.dumps({"aud": "https://fcm.googleapis.com", "exp": now + 99}).encode())
    with pytest.raises(InvalidSignature):
        public.verify(der, f"{head}.{forged}".encode(), ec.ECDSA(hashes.SHA256()))
    # one token per origin, reused while it has more than an hour to run
    assert v.token("https://fcm.googleapis.com", now + 60) == jwt
    assert v.token("https://web.push.apple.com", now) != jwt


def test_the_vapid_key_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "priv" / wp.KEY_FILE
    a = wp.Vapid(path)
    key = a.public_key
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["public"] == key
    assert wp.Vapid(path).public_key == key          # another process reads the same key
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert json.loads(wp.unb64u(a.token("https://e.example").split(".")[1]))["sub"] == \
        wp.DEFAULT_SUBJECT


def test_the_vapid_key_is_written_without_hard_links(tmp_path, monkeypatch):
    def no_links(src, dst):
        raise PermissionError("no hard links here")
    monkeypatch.setattr(wp.os, "link", no_links)
    path = tmp_path / wp.KEY_FILE
    key = wp.Vapid(path).public_key
    assert path.stat().st_mode & 0o777 == 0o600 and wp.Vapid(path).public_key == key
    assert [p.name for p in tmp_path.iterdir()] == [wp.KEY_FILE]


def test_only_push_service_endpoints_pass():
    for url in (FCM, "https://updates.push.services.mozilla.com/wpush/v2/x",
                "https://web.push.apple.com/abc", "https://wns2-by3p.notify.windows.com/w/?token=x",
                "https://FCM.googleapis.com:443/fcm/send/x", "https://jmt17.google.com/fcm/send/x"):
        assert wp.endpoint_allowed(url), url
    for url in ("http://fcm.googleapis.com/fcm/send/x", "https://127.0.0.1/x",
                "https://fcm.googleapis.com.evil.example/x", "https://evilpush.apple.com/x",
                "https://google.com.evil.example/x", "https://notgoogle.com/x",
                "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x",
                "https://localhost/x", "javascript:alert(1)", "", None,
                "https://fcm.googleapis.com/" + "x" * 3000):
        assert not wp.endpoint_allowed(url), url


# the routes


def test_the_subscription_routes(fe):
    fe.push.allow = wp.endpoint_allowed
    assert call(fe, "GET", "/api/push/vapid")[0] == 401
    status, _h, body = owner(fe, "GET", "/api/push/vapid")
    assert status == 200 and json.loads(body)["key"] == fe.push.vapid.public_key
    assert (fe.store_path.parent / wp.KEY_FILE).stat().st_mode & 0o777 == 0o600
    assert json.loads(owner(fe, "GET", "/api/me")[2])["push"] is True
    _key, _auth, keys = browser()
    sub = {"endpoint": FCM, "expirationTime": None, "keys": keys}
    assert owner(fe, "POST", "/api/push/subscribe", sub)[0] == 200
    assert owner(fe, "POST", "/api/push/subscribe", sub)[0] == 200
    rows = fe.store.push_subscriptions()
    assert [(r["endpoint"], r["keys"], r["failures"]) for r in rows] == [(FCM, keys, 0)]
    bad = [
        {"endpoint": "http://127.0.0.1:9/push", "keys": keys},   # not a push service
        {"endpoint": "https://push.example.test/x", "keys": keys},
        {"endpoint": FCM},
        {"endpoint": FCM, "keys": {"p256dh": wp.b64u(b"\x04" + b"\x01" * 64), "auth": keys["auth"]}},
        {"endpoint": FCM, "keys": {"p256dh": keys["p256dh"], "auth": wp.b64u(b"short")}},
        {"endpoint": FCM, "keys": {"p256dh": "not base64!", "auth": keys["auth"]}},
    ]
    for body in bad:
        assert owner(fe, "POST", "/api/push/subscribe", body)[0] == 400, body
    assert call(fe, "POST", "/api/push/subscribe", sub)[0] == 401
    assert owner(fe, "POST", "/api/push/subscribe", sub,
                 {"Origin": "https://evil.example.test"})[0] == 401
    # a device paired through the relay's origin below "all" may not subscribe
    assert owner(fe, "POST", "/api/push/subscribe", sub,
                 {"X-Tacitus-Relay": "d1", "X-Tacitus-Relay-Cards": "deny"})[0] == 403
    assert owner(fe, "POST", "/api/push/subscribe", sub,
                 {"X-Tacitus-Relay": "d1", "X-Tacitus-Relay-Cards": "allow"})[0] == 200
    status, _h, body = owner(fe, "POST", "/api/push/unsubscribe", {"endpoint": FCM})
    assert status == 200 and json.loads(body) == {"ok": True, "removed": True}
    assert json.loads(owner(fe, "POST", "/api/push/unsubscribe", {"endpoint": FCM})[2])["removed"] \
        is False
    assert owner(fe, "POST", "/api/push/unsubscribe", {})[0] == 400
    assert fe.store.push_count() == 0


def test_the_store_keeps_at_most_twenty_subscriptions(tmp_path):
    store = WebStore(tmp_path / "s.sqlite")
    for n in range(25):
        store.push_subscribe(f"{FCM}-{n}", {"p256dh": "k", "auth": "a"})
        time.sleep(0.001)
    kept = [r["endpoint"] for r in store.push_subscriptions()]
    assert len(kept) == 20 and kept[0] == f"{FCM}-5" and kept[-1] == f"{FCM}-24"


# sending


def test_notify_posts_an_encrypted_push_with_vapid_headers(fe, svc):
    key, auth = subscribe_fake(fe, svc)
    out = fe.notify(":lock: *Confirm send*", "To: someone@example.test\nmore", "/chat/#/dm/t1/w1",
                    "dm/t1", urgency="high", wait=True)
    assert out == {"sent": 1, "failed": 0, "dropped": 0, "held": 0}
    req = svc.requests[0]
    h = req["headers"]
    assert req["path"] == "/push/sub-1"
    m = re.fullmatch(r"vapid t=([\w.-]+), k=([\w-]+)", h["authorization"])
    assert m and m.group(2) == fe.push.vapid.public_key
    claims = json.loads(wp.unb64u(m.group(1).split(".")[1]))
    assert claims["aud"] == svc.origin and claims["exp"] <= time.time() + 24 * 3600
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "3600" and h["urgency"] == "high"
    assert h["content-type"] == "application/octet-stream"
    assert json.loads(decrypt(req["body"], key, auth)) == {
        "title": "Confirm send", "body": "To: someone@example.test",
        "url": "/chat/#/dm/t1/w1", "tag": "dm/t1"}
    row = fe.store.push_subscriptions()[0]
    assert row["last_ok"] and row["failures"] == 0
    # the service says the subscription is gone: it is dropped
    svc.status = 410
    assert fe.notify("again", "x", tag="dm/t2", wait=True)["dropped"] == 1
    assert fe.store.push_count() == 0


def test_a_429_holds_the_endpoint_back_and_an_error_is_counted(fe, svc):
    subscribe_fake(fe, svc)
    svc.status, svc.headers = 429, {"Retry-After": "120"}
    assert fe.notify("one", tag="a", wait=True)["failed"] == 1
    assert fe.notify("two", tag="b", wait=True) == {"sent": 0, "failed": 0, "dropped": 0, "held": 1}
    assert len(svc.requests) == 1
    assert fe.store.push_subscriptions()[0]["failures"] == 1
    fe.push._retry_at.clear()
    svc.status, svc.headers = 500, {}
    assert fe.notify("three", tag="c", wait=True)["failed"] == 1
    assert fe.store.push_subscriptions()[0]["failures"] == 2    # kept: not a 404 or 410


def test_a_subscription_off_the_push_services_is_dropped_at_send(fe, svc):
    subscribe_fake(fe, svc)
    fe.push.allow = wp.endpoint_allowed
    assert fe.notify("x", tag="t", wait=True)["dropped"] == 1
    assert not svc.requests and fe.store.push_count() == 0


def test_no_push_without_a_subscription_and_none_when_push_is_off(fe, monkeypatch):
    assert fe.notify("x", tag="t", wait=True) == {"sent": 0, "subscriptions": 0}
    monkeypatch.setattr(wp, "AVAILABLE", False)
    assert fe.notify("x") == {"sent": 0, "push": "off"}
    assert owner(fe, "GET", "/api/push/vapid")[0] == 404
    assert json.loads(owner(fe, "GET", "/api/me")[2])["push"] is False


def test_the_payload_stays_small():
    p = wp.Pusher(None, "/nonexistent/key.json")
    data = json.loads(p.payload(":x: " + "t" * 500, "b" * 5000, "/chat/#/" + "u" * 900, "g" * 300))
    assert len(data["title"]) == 120 and len(data["body"]) == 240
    assert len(data["tag"]) == 200 and data["url"].startswith("/chat/#/")
    big = p.payload("t", "b", "/" + "\U0001f512" * 1500)   # 4 bytes each
    assert len(big) <= wp.MAX_PAYLOAD and json.loads(big)["url"] == ""


# what the front end pushes


def test_a_card_and_a_dm_post_are_pushed_once_per_thread(fe, svc):
    key, auth = subscribe_fake(fe, svc)
    pushes = spy(fe)
    c = fe.post_card("dm", card())
    req = svc.wait(1)[0]
    assert req["headers"]["urgency"] == "high"
    got = json.loads(decrypt(req["body"], key, auth))
    assert got["title"] == "Confirm send" and got["body"] == "To: someone@example.test"
    assert got["url"].endswith(f"#/dm/{c.thread_key}/{c.id}") and got["tag"] == f"dm/{c.thread_key}"
    # a bot reply in the thread: a message push, which a card push does not hold back
    fe.post("dm", "Reply *one*", thread_ts=c.thread_key)
    req = svc.wait(2)[1]
    assert req["headers"]["urgency"] == "normal"
    assert json.loads(decrypt(req["body"], key, auth))["body"] == "Reply one"
    fe.post("dm", "Reply two", thread_ts=c.thread_key)     # within 30 s: held back
    fe.post("main", "Not a DM")                            # not the DM: never pushed
    fe.update_card(c, card(title="Changed"))               # not new: never pushed
    fe.replace_card_with_status(c, "Sent")
    fe.post("dm", "Another thread")                        # another thread: pushed
    svc.wait(3)
    assert [p[5] for p in pushes] == [{"queued": True}, {"queued": True},
                                      {"sent": 0, "skipped": "rate limit"}, {"queued": True}]
    assert [p[4] for p in pushes] == ["high", "normal", "normal", "normal"]
    assert pushes[0][1] == "To: someone@example.test"
    time.sleep(0.2)
    assert len(svc.requests) == 3


def test_the_first_line_of_a_card_is_a_line_then_a_field_then_the_body(fe, svc):
    subscribe_fake(fe, svc)
    pushes = spy(fe)
    fe.post_card("dm", card(lines=["", ":warning: *Check the date*"]))
    fe.post_card("main", card(fields=[], lines=[]))
    assert [wp.plain_line(p[1]) for p in pushes] == ["Check the date", "Hello there."]


def test_shadow_mirrors_push_new_cards_and_the_bots_dm_posts(tmp_path):
    f = make(tmp_path)
    f.inbound = lambda ev: None
    f.start()
    try:
        f.store.push_subscribe(FCM, browser()[2])
        sent: list = []
        f.push.send_all = lambda payload, urgency="normal": sent.append(
            (json.loads(payload), urgency)) or {"sent": 1}
        f.mirror_channel("stream:7", "ea-dm", "dm")
        f.mirror_channel("stream:9", "ea", "main")
        ref = MessageRef("stream:7", "1001", "stream:7:topic:hello", 1000.0)
        f.mirror_post(ref, "*hi*")
        f.mirror_post(ref, "*hi*")                                         # the same row again
        f.mirror_update(ref, "*edited*")
        f.mirror_post(MessageRef("stream:7", "1002", "stream:7:topic:other"), "typed", "owner")
        f.mirror_post(MessageRef("stream:9", "1003", "stream:9:topic:x"), "in the EA channel")
        cref = MessageRef("stream:7", "1004", "stream:7:topic:lock", 1001.0)
        f.mirror_card(cref, card())
        f.mirror_card(cref, card(title="Changed"))                         # a card update
        f.mirror_card_status(cref, "Sent")
        end = time.time() + 5
        while len(sent) < 2 and time.time() < end:
            time.sleep(0.02)
        time.sleep(0.1)
        assert [(s["title"], s["body"], u) for s, u in sent] == [
            ("Tacitus", "hi", "normal"), ("Confirm send", "To: someone@example.test", "high")]
        assert sent[0][0]["url"] == "/chat/#/stream%3A7/stream%3A7%3Atopic%3Ahello/1001"
    finally:
        f.stop()


# pushes for later


def test_a_push_for_later_waits_in_the_queue(fe):
    later = time.time() + 3600
    assert fe.notify("Alert", "In 15 minutes: tickets", "/chat/", "alert:x", urgency="high",
                     at=later) == {"queued_for": later}
    fe.notify("Alert", "In 10 minutes: tickets", "/chat/", "alert:x", urgency="high", at=later + 60)
    queued = fe.store.push_queued()
    assert [(q["tag"], q["due_at"], q["urgency"]) for q in queued] == [("alert:x", later + 60, "high")]
    assert json.loads(queued[0]["payload"])["body"] == "In 10 minutes: tickets"
    assert fe.cancel_notify("alert:x") and not fe.cancel_notify("alert:x")
    assert fe.store.push_queued() == []


def test_the_worker_sends_a_queued_push_when_it_is_due(tmp_path, svc):
    """The bot's worker sends a queued push that is due, and drops one more
    than a TTL past its moment."""
    f = make(tmp_path)
    f.push.allow = lambda url: url.startswith("http://127.0.0.1:")
    f.push.due_poll_s = 0.05
    key, auth = subscribe_fake(f, svc)
    payload = f.push.payload("Alert", "due now", "/chat/", "alert:y").decode()
    f.store.push_defer("alert:y", time.time() - 5, payload, "high")
    f.store.push_defer("alert:z", time.time() - 2 * wp.TTL_S, payload, "high")
    f.store.push_defer("alert:later", time.time() + 600, payload, "high")
    f.start()
    try:
        req = svc.wait(1)[0]
        assert req["headers"]["urgency"] == "high"
        assert json.loads(decrypt(req["body"], key, auth))["body"] == "due now"
        time.sleep(0.2)
        assert len(svc.requests) == 1
        assert [q["tag"] for q in f.store.push_queued()] == ["alert:later"]
    finally:
        f.stop()


def test_two_processes_never_both_claim_a_queued_push(tmp_path):
    a = WebStore(tmp_path / "s.sqlite")
    b = WebStore(tmp_path / "s.sqlite")
    a.push_defer("alert:x", time.time() - 1, "{}", "high")
    a.push_defer("alert:later", time.time() + 600, "{}", "high")
    got = a.push_claim_due(time.time()) + b.push_claim_due(time.time())
    assert [g["tag"] for g in got] == ["alert:x"]
    assert [q["tag"] for q in b.push_queued()] == ["alert:later"]


def test_stop_ends_the_push_worker(tmp_path):
    f = make(tmp_path)
    f.start()
    thread = f.push._thread
    assert thread is not None and thread.is_alive()
    f.stop()
    thread.join(5)
    assert not thread.is_alive()


# the alert path (chat.direct)


def _ea_dir(tmp_path, chat: dict) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": chat}))
    return tmp_path


def test_direct_web_notify_does_nothing_without_a_web_front_end(tmp_path):
    ea = _ea_dir(tmp_path / "a", {"frontend": "zulip"})
    assert direct.web_notify("hi", tag="alert:x", ea_dir=ea) == {"web": "off"}
    assert direct.web_cancel("alert:x", ea_dir=ea) is False
    store = tmp_path / "b" / "webchat.sqlite"
    ea = _ea_dir(tmp_path / "b", {"frontend": "zulip", "shadow": "web",
                                  "web": {"store_path": str(store)}})
    assert direct.web_notify("hi", ea_dir=ea)["web"] == "off"     # no store: never made here
    assert not store.exists()


def test_direct_web_notify_queues_an_alert_for_its_moment(tmp_path, svc, monkeypatch):
    store = tmp_path / "priv" / "webchat.sqlite"
    ea = _ea_dir(tmp_path, {"frontend": "zulip", "shadow": "web",
                            "web": {"store_path": str(store),
                                    "public_url": "https://host.example.test/chat"}})
    WebStore(store).close()
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(hours=2)
    text = ":rotating_light: In 15 minutes, at 10:00 am MT: Tickets\nLedger `tix`."
    assert direct.web_notify(text, tag="alert:tix", when=when, ea_dir=ea) == \
        {"queued_for": when.timestamp()}
    queued = WebStore(store).push_queued()
    assert [(q["tag"], q["urgency"]) for q in queued] == [("alert:tix", "high")]
    assert json.loads(queued[0]["payload"]) == {
        "title": "Tacitus", "body": "In 15 minutes, at 10:00 am MT: Tickets",
        "url": "https://host.example.test/chat/", "tag": "alert:tix"}
    assert direct.web_cancel("alert:tix", ea_dir=ea) is True
    assert WebStore(store).push_queued() == []
    # now, to a subscription: sent before the call returns, no worker thread
    key, auth, keys = browser()
    WebStore(store).push_subscribe(f"{svc.origin}/push/a", keys)
    monkeypatch.setattr(wp, "endpoint_allowed", lambda url: True)
    out = direct.web_notify(text, tag="alert:tix", ea_dir=ea)
    assert out["sent"] == 1 and len(svc.requests) == 1
    assert json.loads(decrypt(svc.requests[0]["body"], key, auth))["body"].startswith("In 15 minutes")


def test_direct_web_notify_never_raises(tmp_path, monkeypatch):
    import chat as chat_pkg

    def boom(**kw):
        raise RuntimeError("no config")
    monkeypatch.setattr(direct, "load_web_frontend", boom)
    assert direct.web_notify("hi")["web"] == "error"
    assert direct.web_cancel("x") is False
    assert chat_pkg.load_web_frontend   # the loader is public
