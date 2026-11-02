"""The web chat relay: the crypto, the relay's routing rules, and a loopback
run of the whole path (docs/webchat-relay.md).

The loopback run starts a fake web chat API (a few routes and an event
stream) on an ephemeral port, the relay server on another, and the bot's
relay client between them; a phone made of relay_crypto calls pairs,
sends requests through the relay and reads an event. Every frame the
relay handles is tapped, and no plaintext marker appears in them, in its
log lines or in what it holds.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "botcore"))
sys.path.insert(0, str(ROOT / "relay"))
sys.path.insert(0, str(ROOT / "tests"))

pytest.importorskip("cryptography")

import relay_server as rs  # noqa: E402
from plain_words import JARGON  # noqa: E402
from botcore.chat import relay_client as rcl  # noqa: E402
from botcore.chat import relay_crypto as rc  # noqa: E402

PAIR_TOKEN = "pair-token-for-tests"
OWN_URL = "https://host.example.test/chat"
LOGIN = "owner@example.test"
REPLY_MARKER = "relay-reply-marker-7f3a2b"
EVENT_MARKER = "relay-event-marker-91c2d4"
REQUEST_MARKER = "relay-request-marker-55e1aa"
FILE_MARKER = b"relay-file-marker-0c9e33"
MARKERS = [REPLY_MARKER.encode(), EVENT_MARKER.encode(), REQUEST_MARKER.encode(), FILE_MARKER]
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# A made-up hash of a signed app bundle, as WebFrontend.bundle_hash() gives one.
BUNDLE = hashlib.sha256(b"made-up app bundle").hexdigest()


def _needles(marker: bytes) -> list[bytes]:
    """The marker and the part of its base64 that shows at any alignment,
    in both alphabets: a body sent unsealed but base64-encoded is caught."""
    out = [marker]
    for k in range(3):
        core = base64.b64encode(b"\x00" * k + marker)[4:-4]
        out += [core, core.replace(b"+", b"-").replace(b"/", b"_")]
    return out


def _leaks(blob: bytes) -> list[bytes]:
    return [n for m in MARKERS for n in _needles(m) if n in blob]


# ── crypto ────────────────────────────────────────────────────────────

def _pair(ident=None, psk_ok=True):
    """A bot identity, a pairing, and a hello/welcome round: (bot session,
    phone session, accepted)."""
    ident = ident or rc.BotIdentity.generate()
    payload = rc.new_pairing(ident, "https://relay.example.net")
    device = rc.generate_private()
    hello, pending = rc.client_hello(payload.bot_public, payload.install_id, device,
                                     pairing=(payload.pairing_id, payload.psk),
                                     info={"name": "test phone"})
    acc = rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk if psk_ok else None,
                        seen=rc.HelloCache().seen)
    return acc.session, rc.client_finish(pending, acc.welcome), acc


def test_handshake_round_trip_both_directions():
    bot, phone, acc = _pair()
    assert bot.sid == phone.sid and len(bot.sid) == 32
    assert acc.info["name"] == "test phone" and acc.pairing_id
    assert phone.device == bot.device == rc.device_id_for(acc.device_public)
    req = phone.seal("req", "r1", b'{"m":"GET","p":"/api/me"}')
    assert bot.open(req) == b'{"m":"GET","p":"/api/me"}'
    res = bot.seal("res", "r1", b"answer")
    assert phone.open(res) == b"answer"
    # Each side sends only its own kinds.
    with pytest.raises(rc.FrameError):
        phone.seal("res", "r2", b"x")
    with pytest.raises(rc.FrameError):
        bot.open(bot.seal("res", "r3", b"x"))


def test_replayed_frame_is_refused_and_reordering_is_not():
    bot, phone, _ = _pair()
    f0, f1, f2 = (phone.seal("req", f"r{i}", b"x") for i in range(3))
    assert bot.open(f2) == b"x" and bot.open(f0) == b"x"  # out of order is fine
    with pytest.raises(rc.ReplayError):
        bot.open(f2)
    with pytest.raises(rc.ReplayError):
        bot.open(f0)
    assert bot.open(f1) == b"x"
    # A counter older than the window is refused.
    w = rc.ReplayWindow(size=8)
    w.mark(20)
    assert w.seen(12) and not w.seen(13) and w.seen(20) and not w.seen(21)


def test_frame_bound_to_its_id_kind_and_session():
    bot, phone, _ = _pair()
    f = phone.seal("req", "r1", b"secret body")
    for change in ({"id": "r2"}, {"k": "ping"}, {"n": f["n"] + 1}):
        with pytest.raises(rc.FrameError):
            bot.open({**f, **change})
    raw = bytearray(rc.b64d(f["c"]))
    raw[5] ^= 1
    with pytest.raises(rc.FrameError):
        bot.open({**f, "c": rc.b64e(bytes(raw))})
    other_bot, other_phone, _ = _pair()
    with pytest.raises(rc.FrameError):
        other_bot.open({**f, "sid": other_bot.sid})
    assert bot.open(f) == b"secret body"  # the failures did not burn its counter


def test_padding_hides_exact_sizes():
    bot, phone, _ = _pair()
    sizes = {len(rc.b64d(phone.seal("req", f"r{i}", b"x" * i)["c"])) for i in range(0, 200, 7)}
    assert sizes == {rc.PAD + 16}


def test_hello_replay_and_wrong_secrets_are_refused():
    ident = rc.BotIdentity.generate()
    payload = rc.new_pairing(ident, "https://relay.example.net")
    device = rc.generate_private()
    hello, _ = rc.client_hello(payload.bot_public, payload.install_id, device,
                               pairing=(payload.pairing_id, payload.psk))
    cache = rc.HelloCache()
    rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk, seen=cache.seen)
    with pytest.raises(rc.HandshakeError, match="replayed"):
        rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk, seen=cache.seen)
    # The wrong pairing secret, no open pairing, a stale clock, another bot.
    with pytest.raises(rc.HandshakeError):
        rc.bot_accept(ident, hello, psk_for=lambda pid: os.urandom(32))
    with pytest.raises(rc.HandshakeError, match="no such pairing"):
        rc.bot_accept(ident, hello, psk_for=lambda pid: None)
    old, _ = rc.client_hello(payload.bot_public, payload.install_id, device,
                             pairing=(payload.pairing_id, payload.psk), now=time.time() - 3600)
    with pytest.raises(rc.HandshakeError, match="clock"):
        rc.bot_accept(ident, old, psk_for=lambda pid: payload.psk)
    with pytest.raises(rc.HandshakeError):
        rc.bot_accept(rc.BotIdentity.generate(), hello, psk_for=lambda pid: payload.psk)


def test_a_welcome_from_anyone_but_the_bot_is_refused():
    ident = rc.BotIdentity.generate()
    device = rc.generate_private()
    hello, pending = rc.client_hello(ident.public, ident.install_id, device)
    # An impostor that knows the bot's public key, not its private key,
    # answers with its own identity.
    impostor = rc.BotIdentity(rc.generate_private(), ident.token)
    with pytest.raises(rc.HandshakeError):
        acc = rc.bot_accept(impostor, hello, psk_for=lambda pid: None)
        rc.client_finish(pending, acc.welcome)
    acc = rc.bot_accept(ident, hello, psk_for=lambda pid: None)
    forged = dict(acc.welcome)
    blob = bytearray(rc.b64d(forged["c"]))
    blob[-1] ^= 1
    forged["c"] = rc.b64e(bytes(blob))
    with pytest.raises(rc.HandshakeError):
        rc.client_finish(pending, forged)
    assert rc.client_finish(pending, acc.welcome).sid == acc.session.sid


def test_pairing_payload_round_trip_keeps_the_secret_in_the_fragment():
    ident = rc.BotIdentity.generate()
    p = rc.new_pairing(ident, "https://relay.example.net")
    link = p.to_string()
    assert link.startswith("https://relay.example.net/#tr1=")
    assert rc.b64e(p.psk) not in link.split("#")[0]
    assert rc.PairingPayload.from_string(link) == p
    assert rc.PairingPayload.from_string(link.split("#", 1)[1]) == p
    tailnet = p.to_string("https://host.example.ts.net/chat/")
    assert tailnet.startswith("https://host.example.ts.net/chat/#tr1=")
    with pytest.raises(rc.HandshakeError):
        rc.PairingPayload.from_string("https://relay.example.net/#tr1=bm90LWpzb24")


def test_pairing_payload_v2_carries_the_bundle_hash_and_the_app_url():
    """`#tr2=`: the fields of version 1 plus `b` (the bundle hash) and `a`
    (the app URL the link opens). A `#tr1=` link still parses."""
    ident = rc.BotIdentity.generate()
    app = "https://relay.example.net/"
    p = dataclasses.replace(rc.new_pairing(ident, "https://relay.example.net"),
                            bundle=BUNDLE, app=app)
    link = p.to_string()
    assert link.startswith(app + "#tr2=") and p.version == 2
    obj = json.loads(rc.b64d(link.split("#tr2=", 1)[1]))
    assert set(obj) == {"v", "r", "i", "k", "c", "p", "s", "x", "b", "a"}
    assert (obj["v"], obj["b"], obj["a"]) == (2, BUNDLE, app)
    assert rc.PairingPayload.from_string(link) == p
    # The hash alone, on a link that opens the bot's own origin.
    own = dataclasses.replace(p, app="").to_string("https://host.example.ts.net/chat/")
    assert own.startswith("https://host.example.ts.net/chat/#tr2=")
    assert rc.PairingPayload.from_string(own).bundle == BUNDLE
    # A link from before: version 1 under #tr1=, no hash, no app.
    old = dataclasses.replace(p, bundle="", app="")
    assert old.to_string().startswith(app + "#tr1=")
    back = rc.PairingPayload.from_string(old.to_string())
    assert back == old and back.version == 1 and back.bundle == back.app == ""

    def bent(drop=(), **over):
        return rc.b64e(json.dumps({**{k: v for k, v in obj.items() if k not in drop},
                                   **over}).encode())
    for frag in ("tr1=" + bent(), "tr2=" + bent(v=1), "tr2=" + bent(v=3),
                 "tr2=" + bent(b="AB" * 32), "tr2=" + bent(b="ab"), "tr2=" + bent(b=None),
                 "tr2=" + bent(a="javascript:alert(1)"), "tr2=" + bent(a=7),
                 "tr2=" + bent(a="https://[::1/"), "tr2=" + bent(drop=("a", "b"))):
        with pytest.raises(rc.HandshakeError):
            rc.PairingPayload.from_string(app + "#" + frag)


def test_relay_and_bot_derive_the_same_ids():
    for _ in range(5):
        token = os.urandom(32)
        assert rs.install_id_for(token) == rc.install_id_for(token)
        assert rs.client_key_for(token) == rc.client_key_for(token)
        assert rs.ROUTE_RE.match(f"/r/{rc.install_id_for(token)}/bot")


# ── the relay's rules, without HTTP ───────────────────────────────────

def _bot_auth(token: bytes) -> str:
    return "Bearer " + rc.b64e(token)


def test_bot_must_prove_its_install_id():
    relay = rs.Relay(log=lambda line: None)
    token = os.urandom(32)
    install = rc.install_id_for(token)
    assert relay.bot_exchange(install, "", {"frames": []})[0] == 401
    assert relay.bot_exchange(install, _bot_auth(os.urandom(32)), {"frames": []})[0] == 403
    assert relay.bot_exchange(install, _bot_auth(token), {"frames": [], "wait": 0}) == (
        200, {"frames": []})


def test_allow_file_activates_installs(tmp_path):
    allow = tmp_path / "installs.txt"
    on, off = os.urandom(32), os.urandom(32)
    allow.write_text(f"# hosted installs\n{rc.install_id_for(on)}  # one\n")
    relay = rs.Relay(allow_file=str(allow), log=lambda line: None)
    assert relay.bot_exchange(rc.install_id_for(on), _bot_auth(on), {"wait": 0})[0] == 200
    status, out = relay.bot_exchange(rc.install_id_for(off), _bot_auth(off), {"wait": 0})
    assert status == 403 and "activated" in out["error"]


def test_client_limits_by_install():
    lim = rs.Limits(burst=2, rate=0.0, client_timeout=0.05)
    relay = rs.Relay(lim, log=lambda line: None)
    token = os.urandom(32)
    install, key = rc.install_id_for(token), rc.client_key_for(token)
    frame = lambda i: {"id": f"q{i}", "k": "ping", "sid": "s" * 32, "n": i, "c": "AAAA"}  # noqa: E731
    offline = relay.client_request(install, key, frame(0))  # bot not connected
    relay.bot_exchange(install, _bot_auth(token), {"wait": 0})
    # A wrong key gets the answer an offline install gets: a stranger who
    # knows the id cannot tell whether the bot is online.
    assert relay.client_request(install, "wrong", frame(0)) == offline == rs.UNAVAILABLE
    assert relay.client_request(install, key, {"id": "x", "k": "res"})[0] == 400
    statuses = [relay.client_request(install, key, frame(i))[0] for i in range(1, 4)]
    assert statuses == [504, 504, 429]  # two time out unanswered, the third is over the burst
    # The timed-out frames left the bot's queue.
    assert relay.snapshot()["routes"][install]["inbox"] == []


def test_offline_bot_is_reaped():
    now = [1000.0]
    relay = rs.Relay(rs.Limits(offline_after=60), log=lambda line: None, clock=lambda: now[0])
    token = os.urandom(32)
    relay.bot_exchange(rc.install_id_for(token), _bot_auth(token), {"wait": 0})
    assert relay.reap() == 0
    now[0] += 61
    assert relay.reap() == 1 and relay.snapshot()["routes"] == {}


def test_relay_strips_fields_it_does_not_route():
    frame = rs.clean_frame({"id": "a1", "k": "res", "sid": "b" * 32, "n": 3, "c": "QUJD",
                            "text": "plaintext", "headers": {"x": "y"}}, rs.BOT_KINDS)
    assert frame == {"id": "a1", "k": "res", "sid": "b" * 32, "n": 3, "c": "QUJD"}
    assert rs.clean_frame({"id": "a1", "k": "err", "e": "free text"}, rs.BOT_KINDS)["e"] == "bad"
    assert rs.clean_frame({"id": "../x", "k": "res"}, rs.BOT_KINDS) is None
    assert rs.clean_frame({"id": "a1", "k": "res", "c": "not base64!"}, rs.BOT_KINDS) is None


def test_path_policy():
    ok = ["/api/me", "/api/channels", "/api/channels/C%3A1/threads?limit=50&before=9",
          "/api/files/f1", "/api/cards/C1/w12/actions", "/api/bundle", "/api/relay/attest"]
    # The attestation passes in exactly one spelling; nothing else under /api/relay/ does.
    bad = ["/api/pair", "/api/pair/", "/api/pair/x", "/api/PAIR/x", "/api/relay/pair",
           "/api/relay/attest/", "/api/relay/attestx", "/api/relay/attest/../pair",
           "/api/%72elay/attest", "/api/relay/%61ttest", "/api/Relay/attest", "/api/relay//attest",
           "/api/relay/attest?x=1", "/api//relay/attest",
           "/api/relay", "/api/relay/approve", "/index.html",
           "/api/relay/devices", "/api/relay/revoke", "/api/relay/status", "/api/pair?x=1",
           "/api/%72elay/pair", "/api/p%61ir", "/api/Relay/devices", "/api/PAIR",
           "/api/relay%2Fpair", "/api/%2572elay/pair".replace("%25", "%"),
           "/api/../api/pair", "/api/%2e%2e/x", "/api/x y", "http://evil.example/api/me",
           "/api/me#frag", "/api/me\r\nX: 1", "/api//cards/C1/w1/actions", None, 7]
    assert all(rcl.path_allowed(p) for p in ok)
    assert not any(rcl.path_allowed(p) for p in bad)


def test_access_policy_for_relay_origin_devices():
    card = "/api/cards/C1/w1/actions"
    assert rcl.access_allows("read", "GET", "/api/me")
    assert not rcl.access_allows("read", "POST", "/api/channels/C1/messages")
    assert rcl.access_allows("chat", "POST", "/api/channels/C1/messages")
    assert not rcl.access_allows("chat", "POST", card)
    assert rcl.access_allows("all", "POST", card)
    # A phone attests its app at any level but "off" ("read" posts nothing else).
    assert rcl.access_allows("read", "POST", "/api/relay/attest")
    assert not rcl.access_allows("off", "POST", "/api/relay/attest")
    assert not rcl.access_allows("off", "GET", "/api/bundle")
    for spelling in ("/api//cards/C1/w1/actions", "/api/%63ards/C1/w1/actions",
                     "/api/CARDS/C1/w1/actions", "/api/cards", "/api/cards?x=1"):
        assert not rcl.access_allows("chat", "POST", spelling), spelling
    for level in ("read", "chat", "all"):
        assert rcl.access_allows(level, "POST", rcl.ATTEST_PATH), level
    for spelling in ("/api/relay/%61ttest", "/api/relay/attest/more", "/api/relay/attest?x=1"):
        assert not rcl.access_allows("read", "POST", spelling), spelling


def test_a_device_paired_against_a_bundle_hash_chats_and_presses_cards_only_when_allowed(tmp_path):
    """Own origin: everything. Relay origin with the bundle hash: "chat"
    whether or not it attested, whatever chat.json says, and "all" only
    when the owner approved it with card buttons. Relay origin without
    one: chat.json's level ("off" keeps it out, cards or not)."""
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.net")
    client = rcl.RelayClient("https://relay.example.net", store, "http://127.0.0.1:9/chat",
                             public_url=OWN_URL)
    paired = {"origin": "relay", "bundle": BUNDLE}
    assert client.level({"origin": "own"}) == "all"
    assert client.level({"origin": "relay"}) == "off"
    assert client.level(paired) == rcl.BUNDLE_ACCESS == "chat"
    assert client.level({**paired, "bundle_verified": True}) == "chat"  # the check grants nothing
    assert client.level({**paired, "cards_allowed": True}) == "all"
    assert client.level({**paired, "cards_allowed": "yes"}) == "chat"
    assert client.level({"origin": "relay", "cards_allowed": True}) == "off"
    client.relay_origin_access = "read"
    assert client.level({"origin": "relay"}) == "read"
    assert client.level({"origin": "relay", "cards_allowed": True}) == "all"
    assert client.level(paired) == "chat"
    # A link with the hash opens the relay's own copy of the app, nowhere else.
    client.relay_origin_access = "off"
    for app in ("https://other.example.net/", "https://relay.example.net:8443/"):
        with pytest.raises(rcl.RelayError, match="relay's own copy"):
            client.new_pairing(app_url=app, relay_origin=True, bundle=BUNDLE)
    with pytest.raises(rcl.RelayError, match="64 hex"):
        client.new_pairing(app_url="https://relay.example.net/", relay_origin=True, bundle="ab")
    link = client.new_pairing(app_url=rcl.relay_app_url(client.relay_url), relay_origin=True,
                              bundle=BUNDLE)
    p = rc.PairingPayload.from_string(link)
    assert (p.bundle, p.app) == (BUNDLE, "https://relay.example.net/")
    # The hash travels from the pairing to the device that spends it.
    assert store.pairing_device(p.pairing_id) == {"origin": "relay", "bundle": BUNDLE}
    dev = store.register(p.pairing_id, rc.public_of(rc.generate_private()), "phone", code="1")
    assert dev["bundle"] == BUNDLE and dev["origin"] == "relay"
    assert client.devices()[0]["bundle_verified"] is False
    assert store.pairing_device(p.pairing_id) == {}


def test_the_bundle_hash_comes_from_the_web_front_end(tmp_path):
    """A pairing carries WebFrontend.bundle_hash(); a malformed or failing
    one counts as none. The CLI's hash is the one the bot signs."""
    from botcore.chat.web_frontend import WebFrontend
    fe = WebFrontend({}, store_path=tmp_path / "webchat.sqlite")
    with mock.patch.object(WebFrontend, "bundle_hash", lambda self: BUNDLE):
        assert rcl.bundle_hash_of(fe) == BUNDLE
        assert rcl._cli_bundle_hash(tmp_path / "relay.json", "https://relay.example.net") == BUNDLE
    for bad in (lambda self: "ab", lambda self: None, lambda self: 1 / 0):
        with mock.patch.object(WebFrontend, "bundle_hash", bad):
            assert rcl.bundle_hash_of(fe) == ""
    key = tmp_path / "priv" / "relay.json"
    key.parent.mkdir(mode=0o700)
    rcl.KeyStore.create(key, relay_url="https://relay.example.net")
    bot = WebFrontend({"relay": {"url": "https://relay.example.net", "key_file": str(key)}},
                      store_path=key.parent / "webchat.sqlite")
    assert rcl._cli_bundle_hash(key, "https://relay.example.net") == bot.bundle_hash() != ""



def test_own_origin_is_public_urls_origin_only():
    """A pairing link makes an "own" device only when it opens the app on
    the web block's public_url origin, compared normalized; any spelling of
    the relay's origin, another origin or no public_url is "relay"."""
    relay, own = "https://relay.example.net", "https://host.example.ts.net/chat"
    assert rcl.origin_of("https://host.example.ts.net/chat/", own, relay) == "own"
    assert rcl.origin_of("https://HOST.example.ts.net.:443/other", own, relay) == "own"
    for app in (None, "", "https://relay.example.net/app/", "https://relay.example.net:443/",
                "https://relay.example.net./", "https://RELAY.example.net/",
                "http://host.example.ts.net/chat/", "https://host.example.ts.net:8443/chat/",
                "https://other.example.ts.net/chat/", "javascript:alert(1)", "https://:0/"):
        assert rcl.origin_of(app, own, relay) == "relay", app
    assert rcl.origin_of("https://host.example.ts.net/chat/", None, relay) == "relay"
    # A backslash or user info: a browser and urllib may read different hosts.
    for app in ("https://evil.example\\@host.example.ts.net/chat/",
                "https://evil.example@host.example.ts.net/chat/",
                "https://user:pw@host.example.ts.net/chat/"):
        assert rcl.origin_of(app, own, relay) == "relay", app
    assert rcl.origin_of("https://host.example.ts.net/chat/", "", relay) == "relay"
    # A public_url that is the relay's own origin never makes an own device.
    assert rcl.origin_of("https://relay.example.net/", "https://relay.example.net:443", relay) \
        == "relay"


def test_sse_block_parsing():
    assert rcl.parse_sse(["id: 7", "event: message", "data: {\"a\":1}"]) == {
        "id": "7", "event": "message", "data": "{\"a\":1}"}
    assert rcl.parse_sse([": ping"]) is None
    assert rcl.parse_sse(["retry: 3000"]) == {"retry": "3000", "data": ""}
    assert rcl.parse_sse(["data: a", "data: b"]) == {"data": "a\nb"}


def test_key_store_pairing_is_one_time(tmp_path):
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.net")
    assert (os.stat(store.path).st_mode & 0o777) == 0o600
    with pytest.raises(rcl.RelayError):
        rcl.KeyStore.create(store.path)
    p = store.add_pairing()
    pub = rc.public_of(rc.generate_private())
    assert store.pairing_psk(p.pairing_id) == p.psk
    dev = store.register(p.pairing_id, pub, "phone <script>", code="123456")
    assert dev["name"] == "phone script" and store.device(pub)["id"] == dev["id"]
    # A new device waits for the owner, with its pairing code.
    assert dev["pending"] is True and store.device(pub)["code"] == "123456"
    assert store.approve(dev["id"]) and "pending" not in store.device(pub)
    assert not store.approve("0" * 16)
    with pytest.raises(rcl.RelayError):
        store.register(p.pairing_id, pub)
    late = store.add_pairing(ttl=1, now=time.time() - 10)
    assert store.pairing_psk(late.pairing_id) is None
    # An expired pairing's secret leaves the file at the next write.
    assert late.pairing_id not in json.dumps(store.load()["pairings"])
    assert store.revoke(dev["id"]) and store.device(pub) is None
    os.chmod(store.path, 0o644)
    with pytest.raises(rcl.RelayError, match="readable"):
        rcl.KeyStore(store.path).load()
    os.chmod(store.path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(store.path)
    with pytest.raises(rcl.RelayError, match="regular file"):
        rcl.KeyStore(link).load()


def _link_of(key: str, pairing: dict, app: str | None) -> str:
    """The link a stored pairing makes, from the key file alone."""
    store = rcl.KeyStore(key)
    ident = store.identity()
    return rc.PairingPayload(relay_url=store.relay_url(), install_id=ident.install_id,
                             bot_public=ident.public, client_key=ident.client_key,
                             pairing_id=pairing["id"], psk=rc.b64d(pairing["psk"]),
                             expires=pairing["expires"], bundle=pairing.get("bundle", ""),
                             app=app or "").to_string(app)


def test_owner_cli(tmp_path, capsys, monkeypatch):
    from botcore.chat import relay_qr
    key = str(tmp_path / "relay.json")
    own = "https://host.example.ts.net/chat"
    assert rcl.main(["init", "--key-file", key, "--relay-url", "https://relay.example.net"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert made["relay"] == {"url": "https://relay.example.net",
                             "install_id": made["install_id"], "key_file": key}
    assert rcl.main(["init", "--key-file", key]) == 1  # never replaces an identity
    capsys.readouterr()
    # A QR code on the terminal, not the link's text; the advice on stderr.
    assert rcl.main(["pair", "--key-file", key, "--public-url", own]) == 0
    out = capsys.readouterr()
    assert "#tr2=" not in out.out and "Never send this link" in out.err
    first = rcl.KeyStore(key).load()["pairings"][0]
    assert out.out.rstrip("\n") == relay_qr.terminal(_link_of(key, first, own + "/"))
    assert rcl.main(["pair", "--key-file", key, "--public-url", own, "--print-link"]) == 0
    link = capsys.readouterr().out.strip()
    p = rc.PairingPayload.from_string(link)
    # The link carries the hash of the app the bot signs, as the bot's own links do.
    bundle = rcl._cli_bundle_hash(Path(key), "https://relay.example.net")
    assert link.startswith(own + "/#tr2=") and p.app == own + "/" and p.bundle == bundle != ""
    assert p.install_id == made["install_id"] and p.relay_url == "https://relay.example.net"
    # Any other origin is refused unless --relay-origin, which warns.
    for args in (["--app-url", own + "/"], []):
        assert rcl.main(["pair", "--key-file", key, "--print-link", *args]) == 1
        assert "own origin" in capsys.readouterr().err
    assert rcl.main(["pair", "--key-file", key, "--relay-origin", "--print-link"]) == 0
    out = capsys.readouterr()
    assert "checks its code against this bot's" in out.err and "approve --cards" in out.err
    assert out.out.startswith("https://relay.example.net/#tr2=")
    relayed = rc.PairingPayload.from_string(out.out.strip())
    assert (relayed.app, relayed.bundle) == ("https://relay.example.net/", bundle)
    # A bot that signs no app: the old warning, and the chat.json opt-in.
    monkeypatch.setattr(rcl, "_cli_bundle_hash", lambda path, url: "")
    assert rcl.main(["pair", "--key-file", key, "--relay-origin", "--print-link"]) == 0
    out = capsys.readouterr()
    assert "read this phone's chat and type as you" in out.err and "relay_origin_access" in out.err
    assert not rc.PairingPayload.from_string(out.out.strip()).bundle
    pairings = rcl.KeyStore(key).load()["pairings"]
    assert [x["origin"] for x in pairings] == ["own", "own", "relay", "relay"]
    assert rcl.main(["show", "--key-file", key]) == 0
    assert json.loads(capsys.readouterr().out)["open_pairings"] == 4
    # A device that spent a pairing waits; show lists its code, approve lets it in.
    store = rcl.KeyStore(key)
    dev = store.register(p.pairing_id, rc.public_of(rc.generate_private()), "phone", code="042917")
    assert rcl.main(["show", "--key-file", key]) == 0
    assert json.loads(capsys.readouterr().out)["devices"] == [
        {"id": dev["id"], "name": "phone", "paired_at": dev["paired_at"], "origin": "own",
         "pending": True, "code": "042917"}]
    assert rcl.main(["approve", "--key-file", key, "--device", dev["id"]]) == 0
    assert capsys.readouterr().out.strip() == "approved"
    assert rcl.main(["show", "--key-file", key]) == 0
    assert json.loads(capsys.readouterr().out)["devices"][0]["pending"] is False
    assert rcl.main(["approve", "--key-file", key, "--device", "0" * 16]) == 1
    assert rcl.main(["revoke", "--key-file", key, "--device", "0" * 16]) == 1
    assert rcl.main(["revoke", "--key-file", key, "--device", dev["id"]]) == 0


def test_owner_cli_pairs_through_the_relay_with_the_bundle_hash(tmp_path, capsys, monkeypatch):
    """With a signed app bundle, `pair --relay-origin` prints a v2 link to
    the relay's copy with the bundle hash, needing no chat.json opt-in;
    `show` lists each device's origin and whether its code is verified."""
    monkeypatch.setattr(rcl, "_cli_bundle_hash", lambda path, url: BUNDLE)
    key = str(tmp_path / "relay.json")
    assert rcl.main(["init", "--key-file", key, "--relay-url", "https://relay.example.net"]) == 0
    capsys.readouterr()
    assert rcl.main(["pair", "--key-file", key, "--relay-origin", "--print-link"]) == 0
    out = capsys.readouterr()
    p = rc.PairingPayload.from_string(out.out.strip())
    assert out.out.startswith("https://relay.example.net/#tr2=")
    assert (p.bundle, p.app) == (BUNDLE, "https://relay.example.net/")
    assert "checks its code against this bot's" in out.err
    assert "relay_origin_access" not in out.err
    # The hash only goes with a link to the relay's own copy.
    assert rcl.main(["pair", "--key-file", key, "--relay-origin", "--print-link",
                     "--app-url", "https://other.example.net/"]) == 1
    assert "relay's own copy" in capsys.readouterr().err
    store = rcl.KeyStore(key)
    relay_dev = store.register(p.pairing_id, rc.public_of(rc.generate_private()), "phone",
                               code="123456")
    assert rcl.main(["pair", "--key-file", key, "--public-url", OWN_URL, "--print-link"]) == 0
    own = rc.PairingPayload.from_string(capsys.readouterr().out.strip())
    assert own.bundle == BUNDLE and own.app == OWN_URL + "/"
    own_dev = store.register(own.pairing_id, rc.public_of(rc.generate_private()), "laptop")
    assert store.approve(own_dev["id"])
    assert rcl.main(["show", "--key-file", key]) == 0
    devices = json.loads(capsys.readouterr().out)["devices"]
    assert devices == [
        {"id": relay_dev["id"], "name": "phone", "paired_at": relay_dev["paired_at"],
         "origin": "relay", "pending": True, "code": "123456", "cards_allowed": False,
         "bundle_verified": False},
        {"id": own_dev["id"], "name": "laptop", "paired_at": own_dev["paired_at"],
         "origin": "own", "pending": False}]


def test_the_terminal_qr_is_the_apps_code():
    """relay_qr encodes module for module as the app's qr.js (the codes
    tests/test_webui.py pins), and the terminal drawing holds that grid."""
    import hashlib
    from botcore.chat import relay_qr
    from test_webui import QR_BASE, QR_PINNED
    for n, (sha, _decodable) in QR_PINNED.items():
        rows = ["".join("1" if c else "0" for c in r) for r in relay_qr.encode(QR_BASE[:n])]
        assert hashlib.sha256("\n".join(rows).encode()).hexdigest() == sha, n
    link = QR_BASE[:460]
    grid = relay_qr.encode(link)
    top = {" ": (0, 0), "\u2580": (1, 0), "\u2584": (0, 1), "\u2588": (1, 1)}
    drawn = []
    for line in relay_qr.terminal(link).splitlines():
        cells = line.removeprefix("\x1b[30;107m").removesuffix("\x1b[0m")
        drawn += [[top[c][0] for c in cells], [top[c][1] for c in cells]]
    n = len(grid)
    assert [r[4:4 + n] for r in drawn[4:4 + n]] == [[int(c) for c in r] for r in grid]
    assert not any(any(r) for r in drawn[:4] + drawn[4 + n:])  # the quiet zone


def test_start_relay_needs_a_relay_block(tmp_path):
    assert rcl.start_relay({}, "http://127.0.0.1:1/chat") is None
    assert rcl.start_relay({"relay": {"url": "https://r.example.net"}}, "http://127.0.0.1:1/chat") is None
    cfg = {"relay": {"url": "https://r.example.net", "key_file": str(tmp_path / "k.json"),
                     "install_id": "a" * 26}}
    assert rcl.start_relay(cfg, "http://127.0.0.1:1/chat") is None  # pinned id, no file
    cfg = {"relay": {"url": "http://relay.example.net", "key_file": str(tmp_path / "k.json")}}
    assert rcl.start_relay(cfg, "http://127.0.0.1:1/chat") is None  # plain http off loopback
    cfg = {"relay": {"url": "https://r.example.net", "key_file": str(tmp_path / "k2.json")}}
    assert rcl.start_relay(cfg, "http://192.0.2.1:8470/chat") is None  # not loopback
    assert rcl.start_relay({"relay": {"enabled": False, "url": "x", "key_file": "y"}}, "") is None


# ── the loopback run ──────────────────────────────────────────────────

class FakeAPI(BaseHTTPRequestHandler):
    """The web front end's API, as much of it as the relay needs."""
    seen: list = []
    stop = threading.Event()

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _owner(self):
        FakeAPI.seen.append((self.command, self.path, dict(self.headers)))
        if self.headers.get("Cookie") != f"ea_web={PAIR_TOKEN}":
            self._json(401, {"error": "not the owner"})
            return False
        return True

    def do_GET(self):
        if not self._owner():
            return
        if self.path == "/chat/api/me":
            self._json(200, {"owner": "owner", "bot": "bot", "note": REPLY_MARKER})
        elif self.path == "/chat/api/files/f1":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="a.bin"')
            self.send_header("Content-Length", str(len(FILE_MARKER)))
            self.send_header("Set-Cookie", "leak=1")
            self.end_headers()
            self.wfile.write(FILE_MARKER)
        elif self.path == "/chat/api/redirect":
            # A loopback server that redirects: the bot does not follow.
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/chat/api/sneaky":
            # An event stream on a plain path: a request never holds it open.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if FakeAPI.stop.wait(0.1):
                        break
            except OSError:
                pass
        elif self.path.startswith("/chat/api/events"):
            since = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("since", ["-"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b": ping\n\n")
                data = json.dumps({"text": EVENT_MARKER, "since": since})
                self.wfile.write(f"id: 8\nevent: message\ndata: {data}\n\n".encode())
                self.wfile.flush()
                for _ in range(100):
                    if FakeAPI.stop.wait(0.1):
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
        else:
            self._json(404, {"error": "no such route"})

    def do_POST(self):
        if not self._owner():
            return
        self._raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(self._raw or b"{}")
        except ValueError:
            body = {}
        if self.path == "/chat/api/channels/C1/messages":
            self._json(200, {"id": "w2", "text": body.get("text"), "author": "owner"})
        elif self.path == "/chat/api/channels/C1/files":
            self._json(200, {"size": len(self._raw), "head": self._raw[:16].decode("latin-1")})
        elif self.path == "/chat/api/cards/C1/w1/actions":
            self._json(200, {"ok": True, "action_id": body.get("action_id")})
        else:
            self._json(404, {"error": "no such route"})

    def do_PATCH(self):
        if not self._owner():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/chat/api/channels/r1":
            self._json(200, {"id": "r1", "name": body.get("name"), "kind": "room"})
        else:
            self._json(404, {"error": "no such route"})

    def do_DELETE(self):
        if not self._owner():
            return
        if self.path == "/chat/api/messages/C1/w2":
            self._json(200, {"id": "w2", "text": "", "deleted": True})
        else:
            self._json(404, {"error": "no such route"})


def _serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


class Phone:
    """The phone: relay_crypto and urllib, nothing of the bot's."""

    def __init__(self, base: str, payload: rc.PairingPayload):
        self.base = base
        self.payload = payload
        self.url = f"{base}/r/{payload.install_id}"
        self.key = rc.generate_private()
        self.session: rc.Session | None = None
        self.code = ""  # the pairing code this phone shows

    def ticket(self, opener: dict) -> str:
        """A one-time ticket for the event stream the `open` frame asks for."""
        status, out = self.post(opener, route="sse")
        assert status == 200, out
        return out["ticket"]

    def post(self, frame: dict, key: str | None = None, route: str = "c") -> tuple[int, dict]:
        req = urllib.request.Request(f"{self.url}/{route}", data=json.dumps(frame).encode(),
                                     method="POST", headers={
                                         "Content-Type": "application/json",
                                         "X-Relay-Key": key or self.payload.client_key})
        try:
            with NO_PROXY.open(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def hello(self, pairing: bool = True) -> tuple[int, dict]:
        p = self.payload
        frame, pending = rc.client_hello(p.bot_public, p.install_id, self.key,
                                         pairing=(p.pairing_id, p.psk) if pairing else None,
                                         info={"name": "loopback phone"})
        if pairing:
            self.code = rc.pairing_code(pending.h)
        status, answer = self.post(frame)
        if status == 200 and answer.get("k") == "welcome":
            self.session = rc.client_finish(pending, answer)
        return status, answer

    def call(self, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[dict, dict]:
        """(the sealed frame sent, the answer: decrypted, or the err frame)."""
        req = {"m": method, "p": path, "h": headers or {"content-type": "application/json"}}
        frame = self.session.seal("req", rc.new_id(), rc.pack(req, body or b""))
        return frame, self.send(frame)

    def send(self, frame: dict) -> dict:
        status, answer = self.post(frame)
        if answer.get("k") == "res":
            meta, body = rc.unpack(self.session.open(answer))
            return {**meta, "body": body}
        return answer


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    d = tmp_path_factory.mktemp("relay-loop")
    (d / "webui").mkdir()
    (d / "webui" / "index.html").write_text("<!doctype html><title>chat</title>")
    (d / "webui" / "sw.js").write_text("'use strict';")
    (d / "secret.txt").write_text("outside the static folder")
    (d / "pair.token").write_text(PAIR_TOKEN + "\n")
    FakeAPI.seen = []
    FakeAPI.stop.clear()
    api = _serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI))
    api.daemon_threads = True
    taps, logs = [], []
    relay = rs.Relay(rs.Limits(keepalive=0.3, client_timeout=10),
                     log=logs.append, tap=lambda d_, f: taps.append((d_, json.dumps(f))))
    server = _serve(rs.RelayHTTPServer(("127.0.0.1", 0), relay, str(d / "webui")))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    web_cfg = {"pair_token_file": str(d / "pair.token"), "public_url": OWN_URL,
               "relay": {"url": base, "key_file": str(d / "relay-key.json"), "wait": 1,
                         "relay_origin_access": "chat"}}
    client = rcl.start_relay(web_cfg, f"http://127.0.0.1:{api.server_address[1]}/chat")
    assert client is not None
    deadline = time.time() + 10
    while not client.connected and time.time() < deadline:
        time.sleep(0.05)
    assert client.connected, client.last_error
    phone = Phone(base, rc.PairingPayload.from_string(
        client.new_pairing(app_url=base + "/", relay_origin=True)))
    status, welcome = phone.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
    assert client.approve(phone.session.device)
    yield {"relay": relay, "server": server, "base": base, "client": client, "phone": phone,
           "taps": taps, "logs": logs, "dir": d, "cfg": web_cfg}
    FakeAPI.stop.set()
    client.stop()
    server.shutdown()
    api.shutdown()


def test_pairing_registers_the_phone(loop):
    devices = loop["client"].devices()
    assert [d["name"] for d in devices] == ["loopback phone"]
    assert devices[0]["id"] == loop["phone"].session.device
    key_file = loop["dir"] / "relay-key.json"
    assert (os.stat(key_file).st_mode & 0o777) == 0o600
    # The pairing was spent: a second phone with the same QR is refused.
    again = Phone(loop["base"], loop["phone"].payload)
    status, answer = again.hello()
    assert status == 200 and answer["k"] == "err" and answer["e"] == "auth"
    assert again.session is None


def test_request_through_the_relay(loop):
    phone = loop["phone"]
    _, me = phone.call("GET", "/api/me")
    assert me["s"] == 200 and json.loads(me["body"])["note"] == REPLY_MARKER
    body = json.dumps({"text": REQUEST_MARKER}).encode()
    _, posted = phone.call("POST", "/api/channels/C1/messages", body)
    assert posted["s"] == 200 and json.loads(posted["body"])["text"] == REQUEST_MARKER
    _, f = phone.call("GET", "/api/files/f1")
    assert f["body"] == FILE_MARKER
    assert f["h"] == {"content-type": "application/octet-stream",
                      "content-disposition": 'attachment; filename="a.bin"'}
    # The owner credential is added on the bot; the phone's own is dropped.
    _, me2 = phone.call("GET", "/api/me", headers={"cookie": "ea_web=forged",
                                                    "tailscale-user-login": "someone"})
    assert me2["s"] == 200
    hdrs = FakeAPI.seen[-1][2]
    assert hdrs["Cookie"] == f"ea_web={PAIR_TOKEN}"
    assert "Tailscale-User-Login" not in hdrs
    assert hdrs["X-Tacitus-Relay"] == phone.session.device
    assert hdrs["X-Tacitus-Relay-Cards"] == "deny"  # paired on the relay's origin
    assert "Origin" not in hdrs  # a GET
    local = urllib.parse.urlsplit(loop["client"].local)
    assert hdrs["Host"] == local.netloc
    posted = next(h for m, p, h in reversed(FakeAPI.seen) if m == "POST")
    assert posted["Origin"] == f"http://{local.netloc}" and posted["Host"] == local.netloc
    assert "Tailscale-User-Login" not in posted
    # The bot signs in with the pairing cookie only, never the identity header.
    assert rcl.owner_headers_for({"tailscale_login": LOGIN}) == {}
    assert rcl.owner_headers_for({"tailscale_login": LOGIN,
                                  "pair_token_file": str(loop["dir"] / "pair.token")}) == {
        "Cookie": f"ea_web={PAIR_TOKEN}"}


def test_a_room_rename_crosses_as_a_patch_and_other_methods_do_not(loop):
    """The app renames a room with PATCH, and removes the owner's message
    with DELETE: the bot replays each with its body and the loopback
    Origin, as a POST. A device that may chat may send them; one that only
    reads may not. Another method never reaches the server."""
    phone = loop["phone"]
    _, out = phone.call("PATCH", "/api/channels/r1", json.dumps({"name": "atlas 2027"}).encode())
    assert out["s"] == 200 and json.loads(out["body"])["name"] == "atlas 2027"
    method, path, hdrs = FakeAPI.seen[-1]
    local = urllib.parse.urlsplit(loop["client"].local)
    assert (method, path) == ("PATCH", "/chat/api/channels/r1")
    assert hdrs["Origin"] == f"http://{local.netloc}"
    assert hdrs["X-Tacitus-Relay"] == phone.session.device
    assert rcl.access_allows("chat", "PATCH", "/api/channels/r1")
    assert not rcl.access_allows("read", "PATCH", "/api/channels/r1")
    _, out = phone.call("DELETE", "/api/messages/C1/w2", b"")
    assert out["s"] == 200 and json.loads(out["body"])["deleted"] is True
    method, path, hdrs = FakeAPI.seen[-1]
    assert (method, path, hdrs["Origin"]) == ("DELETE", "/chat/api/messages/C1/w2", f"http://{local.netloc}")
    assert rcl.access_allows("chat", "DELETE", path) and not rcl.access_allows("read", "DELETE", path)
    n = len(FakeAPI.seen)
    for method in ("PUT", "OPTIONS", "HEAD"):
        _, out = phone.call(method, "/api/channels/r1", b"{}")
        assert out["s"] == 405, method
    assert len(FakeAPI.seen) == n


def test_large_bodies_cross_once_encoded(loop):
    """A body near the chat's 10 MiB file cap fits one frame; one over it
    is refused by the bot, not dropped by the relay."""
    phone = loop["phone"]
    big = b"\x89PNG" + os.urandom(rcl.MAX_BODY - 4)
    _, out = phone.call("POST", "/api/channels/C1/files", big,
                        headers={"content-type": "application/octet-stream"})
    assert out["s"] == 200 and json.loads(out["body"])["size"] == rcl.MAX_BODY
    _, out = phone.call("POST", "/api/channels/C1/files", big + b"x")
    assert out["s"] == 413


def test_ping_and_denied_paths(loop):
    phone = loop["phone"]
    frame = phone.session.seal("ping", rc.new_id(), rc.pack({}))
    status, answer = phone.post(frame)
    assert "pong" in rc.unpack(phone.session.open(answer))[0]
    n = len(FakeAPI.seen)
    for path in ("/api/pair", "/api/relay/pair", "/../chat/api/me"):
        _, out = phone.call("POST" if "pair" in path else "GET", path, b"{}")
        assert out["s"] == 403, path
    assert len(FakeAPI.seen) == n  # none reached the local server


def test_card_actions_need_a_device_paired_on_the_own_origin(loop):
    """The relay serves the app it pairs; a phone paired there cannot press
    a card's buttons. One paired through the bot's own origin can."""
    phone, client = loop["phone"], loop["client"]
    click = json.dumps({"action_id": "approve_send", "value": "v1"}).encode()
    n = len(FakeAPI.seen)
    _, out = phone.call("POST", "/api/cards/C1/w1/actions", click)
    assert out["s"] == 403 and "origin" in json.loads(out["body"])["error"]
    assert len(FakeAPI.seen) == n
    own = Phone(loop["base"], rc.PairingPayload.from_string(client.new_pairing(app_url=OWN_URL + "/")))
    assert own.hello()[0] == 200 and client.approve(own.session.device)
    assert {d["id"]: d["origin"] for d in client.devices()}[own.session.device] == "own"
    _, out = own.call("POST", "/api/cards/C1/w1/actions", click)
    assert out["s"] == 200 and json.loads(out["body"]) == {"ok": True, "action_id": "approve_send"}
    assert FakeAPI.seen[-1][2]["X-Tacitus-Relay-Cards"] == "allow"
    client.revoke(own.session.device)


def test_moved_replayed_and_unknown_session_frames(loop):
    phone = loop["phone"]
    rid = rc.new_id()
    f = phone.session.seal("req", rid, rc.pack({"m": "GET", "p": "/api/me"}))
    # The sealed body under another id does not open: the id is bound.
    assert phone.post({**f, "id": rc.new_id()})[1]["e"] == "bad"
    # The failure did not spend the counter; the frame itself opens once.
    assert phone.post(f)[1]["k"] == "res"
    assert phone.post(f)[1] == {"id": rid, "k": "err", "e": "replay"}
    status, unknown = phone.post({**f, "id": rc.new_id(), "sid": "0" * 32})
    assert unknown["k"] == "err" and unknown["e"] == "session"


def test_event_stream_through_the_relay(loop):
    phone, base = loop["phone"], loop["base"]
    sid = rc.new_id()
    opener = phone.session.seal("open", sid, rc.pack({"since": 5}))
    ticket = phone.ticket(opener)
    resp = NO_PROXY.open(f"{phone.url}/sse?t={ticket}", timeout=10)
    assert resp.headers["Content-Type"] == "text/event-stream"
    event = None
    for _ in range(50):
        line = resp.readline().decode().strip()
        if line.startswith("data: "):
            frame = json.loads(line[6:])
            assert frame["k"] == "sse" and frame["id"] == sid
            event = rc.unpack(phone.session.open(frame))[0]
            break
    assert event is not None
    assert event["id"] == "8" and event["event"] == "message"
    assert json.loads(event["data"]) == {"text": EVENT_MARKER, "since": "5"}
    resp.close()
    # The relay notices the phone left (its keepalive write fails), sends
    # the bot a close, and the bot closes the local stream.
    client = loop["client"]
    deadline = time.time() + 10
    while client.status()["streams"] and time.time() < deadline:
        time.sleep(0.05)
    assert client.status()["streams"] == 0


def test_wrong_keys_at_the_relay(loop):
    phone = loop["phone"]
    frame = phone.session.seal("ping", rc.new_id(), rc.pack({}))
    assert phone.post(frame, key="not-the-key") == rs.UNAVAILABLE
    install = phone.payload.install_id
    req = urllib.request.Request(f"{loop['base']}/r/{install}/bot", data=b'{"frames":[]}',
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": "Bearer " + rc.b64e(os.urandom(32))})
    with pytest.raises(urllib.error.HTTPError) as e:
        NO_PROXY.open(req, timeout=5)
    assert e.value.code == 403
    e.value.close()


def test_static_ui_and_traversal(loop):
    base = loop["base"]
    with NO_PROXY.open(base + "/", timeout=5) as r:
        assert b"<title>chat</title>" in r.read()
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert "unsafe-eval" not in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    # The worker's loader runs its checked copy of the worker's code as text: there alone.
    with NO_PROXY.open(base + "/sw.js", timeout=5) as r:
        assert r.headers["Content-Security-Policy"] == rs.WORKER_CSP
        assert "script-src 'self' 'unsafe-eval';" in rs.WORKER_CSP and "default-src 'self'" in rs.WORKER_CSP
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/missing.js"):
        with pytest.raises(urllib.error.HTTPError) as e:
            NO_PROXY.open(base + path, timeout=5)
        assert e.value.code == 404, path
        e.value.close()
    with NO_PROXY.open(base + "/healthz", timeout=5) as r:
        assert json.loads(r.read())["installs"] == 1


def test_revoked_device_loses_its_session(loop):
    client, phone = loop["client"], loop["phone"]
    other = Phone(loop["base"], rc.PairingPayload.from_string(client.new_pairing()))
    assert other.hello()[0] == 200 and other.session is not None
    assert client.approve(other.session.device)
    assert other.call("GET", "/api/me")[1]["s"] == 200
    assert client.revoke(other.session.device)
    _, out = other.call("GET", "/api/me")
    assert out["k"] == "err" and out["e"] in ("session", "auth")
    status, answer = other.hello(pairing=False)
    assert answer["k"] == "err" and answer["e"] == "auth"
    assert phone.call("GET", "/api/me")[1]["s"] == 200  # the first phone is untouched


def _raw_post(base: str, path: str, headers: dict, body: bytes = b"") -> tuple[float, bytes]:
    """(seconds to the answer, the answer's first bytes) for a POST whose
    Content-Length promises far more than `body`."""
    import socket
    host, port = base.split("//", 1)[1].split(":")
    t0 = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\n{head}\r\n".encode() + body)
        answer = b""
        while chunk := sock.recv(4096):  # the relay closes the connection
            answer += chunk
    return time.monotonic() - t0, answer


def test_a_phone_key_is_checked_before_the_body_is_read(loop):
    """A stranger never makes the relay read or parse a body: a wrong key
    or an install that is not online is answered from the headers, at once,
    although the body never comes."""
    phone, base = loop["phone"], loop["base"]
    unknown = rc.install_id_for(os.urandom(32))
    for install, key in ((phone.payload.install_id, "not-the-key"), (unknown, "any")):
        for route in ("c", "sse"):
            took, answer = _raw_post(base, f"/r/{install}/{route}", {
                "Content-Type": "application/json", "Content-Length": "9000000",
                "X-Relay-Key": key})
            assert took < 3 and answer.startswith(b"HTTP/1.1 503"), (route, answer[:40])
            assert answer.endswith(json.dumps(rs.UNAVAILABLE[1], separators=(",", ":")).encode())


def test_event_stream_tickets_work_once(loop):
    phone = loop["phone"]
    opener = phone.session.seal("open", rc.new_id(), rc.pack({}))
    ticket = phone.ticket(opener)
    resp = NO_PROXY.open(f"{phone.url}/sse?t={ticket}", timeout=10)
    assert resp.status == 200
    resp.close()
    for bad in (ticket, "0" * 32, "not-a-ticket", ""):
        with pytest.raises(urllib.error.HTTPError) as e:
            NO_PROXY.open(f"{phone.url}/sse?t={bad}", timeout=5)
        assert e.value.code == 503 and json.loads(e.value.read()) == rs.UNAVAILABLE[1]
        e.value.close()
    # The old query carrying the key and the frame opens nothing.
    q = urllib.parse.urlencode({"f": rc.b64e(json.dumps(opener).encode()),
                                "key": phone.payload.client_key})
    with pytest.raises(urllib.error.HTTPError) as e:
        NO_PROXY.open(f"{phone.url}/sse?{q}", timeout=5)
    assert e.value.code == 503
    e.value.close()


def test_the_relay_sees_only_ciphertext(loop):
    """Runs last: everything the relay handled in this module."""
    phone = loop["phone"]
    # The positive control: the needles find the markers in plaintext.
    _, me = phone.call("GET", "/api/me")
    assert _leaks(me["body"]) and _leaks(base64.urlsafe_b64encode(me["body"]))
    taps, logs, relay = loop["taps"], loop["logs"], loop["relay"]
    assert len(taps) > 20 and logs
    blobs = [t for _, t in taps] + logs + [json.dumps(relay.snapshot())]
    for _, text in taps:
        c = json.loads(text).get("c")
        if c:
            blobs.append(rc.b64d(c))
    leaks = [(i, _leaks(b if isinstance(b, bytes) else b.encode())) for i, b in enumerate(blobs)]
    assert [x for x in leaks if x[1]] == []
    # The log lines name the route, the install, the status and sizes only.
    assert all(len(line.split()) == 8 for line in logs), logs[:3]
    # Every tapped frame has the protocol's fields and nothing else.
    for _, text in taps:
        assert set(json.loads(text)) <= {"id", "k", "sid", "n", "c", "e"}


# ── the relay's hardening, without HTTP ───────────────────────────────

def _online_install(relay):
    token = os.urandom(32)
    install = rc.install_id_for(token)
    assert relay.bot_exchange(install, _bot_auth(token), {"wait": 0})[0] == 200
    return install, rc.client_key_for(token)


def test_small_kinds_have_a_small_cap():
    relay = rs.Relay(rs.Limits(client_timeout=0.05), log=lambda line: None)
    install, key = _online_install(relay)
    big = "A" * 8000
    for kind in ("hello", "ping"):
        frame = {"id": f"{kind}1", "k": kind, "sid": "s" * 32, "n": 0, "c": big}
        assert relay.client_request(install, key, frame)[0] == 413, kind
    assert relay.stream_ticket(install, key, {"id": "o1", "k": "open", "sid": "s" * 32,
                                              "n": 0, "c": big})[0] == 413
    # A request of that size is ordinary; unanswered, it times out.
    req = {"id": "r1", "k": "req", "sid": "s" * 32, "n": 0, "c": big}
    assert relay.client_request(install, key, req)[0] == 504


def test_offline_and_wrong_key_answer_alike_on_every_phone_route():
    relay = rs.Relay(log=lambda line: None)
    install, key = _online_install(relay)
    offline = rc.install_id_for(os.urandom(32))
    opener = {"id": "o1", "k": "open", "sid": "s" * 32, "n": 0, "c": "AAAA"}
    answers = [relay.check_key(offline, key), relay.check_key(install, "wrong"),
               relay.check_key(install, None)]
    assert answers == [rs.UNAVAILABLE] * 3
    assert relay.stream_ticket(offline, key, opener) == rs.UNAVAILABLE
    assert relay.stream_ticket(install, "wrong", opener) == rs.UNAVAILABLE
    assert relay.open_stream(offline, "0" * 32) == rs.UNAVAILABLE
    assert relay.open_stream(install, "0" * 32) == rs.UNAVAILABLE
    assert relay.check_key(install, key) == (200, {})


def test_stream_tickets_expire_and_are_capped():
    now = [1000.0]
    lim = rs.Limits(ticket_ttl=30, max_tickets=2)
    relay = rs.Relay(lim, log=lambda line: None, clock=lambda: now[0])
    install, key = _online_install(relay)

    def opener(i):
        return {"id": f"o{i}", "k": "open", "sid": "s" * 32, "n": i, "c": "AAAA"}
    t1 = relay.stream_ticket(install, key, opener(1))[1]["ticket"]
    status, st = relay.open_stream(install, t1)
    assert status == 200 and st.id == "o1"
    assert relay.open_stream(install, t1) == rs.UNAVAILABLE  # once only
    late = relay.stream_ticket(install, key, opener(2))[1]["ticket"]
    now[0] += 31  # the install stays online (60 s); the ticket does not
    assert relay.open_stream(install, late) == rs.UNAVAILABLE
    # At most max_tickets wait; the oldest goes first.
    tickets = [relay.stream_ticket(install, key, opener(i))[1]["ticket"] for i in (3, 4, 5)]
    assert relay.open_stream(install, tickets[0]) == rs.UNAVAILABLE
    assert relay.open_stream(install, tickets[2])[0] == 200
    # The open frame reached the bot only when its ticket was spent.
    inbox = relay.snapshot()["routes"][install]["inbox"]
    assert [f["id"] for f in inbox] == ["o1", "o5"]


def test_new_routes_are_metered_per_address_and_idle_ones_make_room():
    lim = rs.Limits(max_installs=3, new_route_burst=2, new_route_rate=0.0)
    relay = rs.Relay(lim, log=lambda line: None)
    tokens = [os.urandom(32) for _ in range(6)]
    ids = [rc.install_id_for(t) for t in tokens]

    def poll(i, source):
        return relay.bot_exchange(ids[i], _bot_auth(tokens[i]), {"wait": 0}, source)[0]
    assert [poll(0, "198.51.100.1"), poll(1, "198.51.100.1")] == [200, 200]
    assert poll(2, "198.51.100.1") == 429          # this address opened its share
    assert poll(0, "198.51.100.1") == 200          # a route that exists polls on
    assert poll(2, "198.51.100.2") == 200          # the table is full now
    relay.routes[ids[0]].used = True               # a phone has used install 0
    assert poll(3, "198.51.100.2") == 200          # install 1, idle and unused, makes room
    assert set(relay.routes) == {ids[0], ids[2], ids[3]}
    for route in relay.routes.values():            # every route holds a poll
        route.polls = 1
    assert poll(4, "198.51.100.3") == 503
    for route in relay.routes.values():
        route.polls = 0


def test_hello_with_a_non_finite_clock_is_refused(monkeypatch):
    ident = rc.BotIdentity.generate()
    device = rc.generate_private()
    for constant in ("NaN", "Infinity", "-Infinity"):
        class Shim:
            @staticmethod
            def dumps(obj, **kw):
                return '{"name": "x", "t": %s}' % constant
        with monkeypatch.context() as m:
            m.setattr(rc, "json", Shim)
            hello, _ = rc.client_hello(ident.public, ident.install_id, device)
        with pytest.raises(rc.HandshakeError, match="payload|clock"):
            rc.bot_accept(ident, hello, psk_for=lambda pid: None)
    ok, _ = rc.client_hello(ident.public, ident.install_id, device)
    rc.bot_accept(ident, ok, psk_for=lambda pid: None)


def test_the_new_route_meter_reads_the_terminators_address():
    class H:
        def __init__(self, peer, fwd):
            self.client_address = (peer, 1)
            self.headers = {"X-Forwarded-For": fwd} if fwd else {}
    src = rs.RelayHandler._source
    assert src(H("127.0.0.1", "203.0.113.9, 198.51.100.4")) == "198.51.100.4"
    assert src(H("127.0.0.1", "")) == "127.0.0.1"
    assert src(H("192.0.2.7", "198.51.100.4")) == "192.0.2.7"  # only a loopback proxy counts


# ── shared test vectors (the web app's crypto reproduces them) ────────

VECTORS = ROOT / "tests" / "fixtures" / "relay_vectors.json"


def _fixed(label: str) -> bytes:
    import hashlib
    return hashlib.sha256(f"tacitus relay test vector: {label}".encode()).digest()


def relay_vectors() -> dict:
    """The protocol run with fixed keys and clock, through relay_crypto
    itself: the ephemeral keys it would draw are fixed by a stand-in for
    X25519PrivateKey.generate. tests/test_webui.py runs the app's
    WebCrypto code on the same inputs in a browser."""
    from unittest import mock
    real = rc.X25519PrivateKey
    drawn = [_fixed("phone ephemeral"), _fixed("bot ephemeral"), _fixed("phone ephemeral, plain")]

    class Fixed:
        from_private_bytes = staticmethod(real.from_private_bytes)

        @staticmethod
        def generate():
            return real.from_private_bytes(drawn.pop(0))

    hexs = bytes.hex
    now = 1793421333
    ident = rc.BotIdentity(_fixed("bot static"), _fixed("relay token"))
    device = _fixed("device static")
    payload = rc.PairingPayload(relay_url="https://relay.example.net", install_id=ident.install_id,
                                bot_public=ident.public, client_key=ident.client_key,
                                pairing_id=_fixed("pairing id")[:16].hex(),
                                psk=_fixed("pairing secret"), expires=now + rc.PAIRING_TTL)
    info = {"name": "vector phone"}
    with mock.patch.object(rc, "X25519PrivateKey", Fixed):
        hello, pending = rc.client_hello(ident.public, ident.install_id, device,
                                         pairing=(payload.pairing_id, payload.psk), info=info,
                                         frame_id="h" * 32, now=now)
        acc = rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk, now=now)
        plain, plain_pending = rc.client_hello(ident.public, ident.install_id, device,
                                               info=info, frame_id="p" * 32, now=now)
    phone = rc.client_finish(pending, acc.welcome)
    eb = rc.b64d(acc.welcome["c"])[:32]
    okm_h = rc._h(pending.h, eb)
    k_c2b, k_b2c, sid = rc._session_keys(pending.es + pending.ss + rc._dh(pending.e_private, eb)
                                         + rc._dh(pending.s_private, eb) + pending.psk, okm_h)
    req_meta = {"m": "GET", "p": "/api/me", "h": {"accept": "application/json"}}
    req_body = b""
    res_meta = {"s": 200, "h": {"content-type": "application/json"}}
    res_body = b'{"owner":"owner","bot":"bot"}'
    req = phone.seal("req", "r" * 32, rc.pack(req_meta, req_body))
    res = acc.session.seal("res", "r" * 32, rc.pack(res_meta, res_body))
    sse_event = {"id": "8", "event": "message", "data": '{"text":"hello"}'}
    sse = acc.session.seal("sse", "s" * 32, rc.pack(sse_event))
    assert acc.session.open(req) == rc.pack(req_meta, req_body)
    assert rc.unpack(phone.open(res)) == (res_meta, res_body)
    # The handoff: a code, its mailbox and key, and the sealed payload. The
    # vector's KDF runs 1000 rounds to stay fast; `production` gives the
    # same code at the 600000 the bot and the app use.
    code = "7K3QX9ZRMA"
    relay, other_relay = "https://relay.example.org", "https://relay.example.net/relay/"
    secret = rc.handoff_secret(code, iterations=1000)
    mailbox, key = rc.handoff_keys(code, relay, iterations=1000)
    prod_mailbox, prod_key = rc.handoff_keys(code, relay)
    prod_secret = rc.handoff_secret(code)
    handoff_payload = rc.PairingPayload(
        relay_url="https://relay.example.org", install_id=ident.install_id,
        bot_public=ident.public, client_key=ident.client_key,
        pairing_id=_fixed("handoff pairing id")[:16].hex(), psk=_fixed("handoff pairing secret"),
        expires=now + rc.PAIRING_TTL, bundle=hashlib.sha256(b"vector bundle").hexdigest(),
        app="https://app.example.org/")
    nonce = _fixed("handoff nonce")[:12]
    blob = rc.seal_handoff(mailbox, key, handoff_payload.to_json(), nonce)
    assert rc.open_handoff(mailbox, key, blob) == handoff_payload
    normalize = [["7k3qx-9zrma", code], [" 7K3QX 9ZRMA ", code], ["7K3QX--9ZRMA", code],
                 ["OIL01-23456", "0110123456"], ["oil01 23456", "0110123456"],
                 ["7K3QX-9ZRM", None], ["7K3QX-9ZRMA0", None], ["7K3QX-9ZRMU", None],
                 ["7K3QX_9ZRMA", None], ["7K3QX\t9ZRMA", None], ["7K3QX–9ZRMA", None],
                 ["", None]]
    assert all(rc.normalize_handoff_code(i) == o for i, o in normalize)
    return {
        "about": ("tacitus-relay/1 with fixed keys, made by tests/test_relay.py "
                  "(relay_vectors); bytes are base64url without padding, keys and hashes hex"),
        "now": now,
        "bot": {"private": hexs(ident.private), "public": rc.b64e(ident.public),
                "token": rc.b64e(ident.token), "install_id": ident.install_id,
                "client_key": ident.client_key},
        "device": {"private": hexs(device), "public": rc.b64e(rc.public_of(device)),
                   "id": rc.device_id_for(rc.public_of(device))},
        "phone_ephemeral": {"private": hexs(pending.e_private),
                            "public": rc.b64e(rc.public_of(pending.e_private))},
        "bot_ephemeral": {"public": rc.b64e(eb)},
        "pairing": {"id": payload.pairing_id, "psk": rc.b64e(payload.psk),
                    "expires": payload.expires,
                    "link": payload.to_string("https://host.example.test/chat/")},
        "hello": {"payload": rc.b64e(json.dumps({**info, "t": now}).encode()),
                  "frame": hello, "h": hexs(pending.h), "code": acc.code},
        "plain_hello": {"phone_ephemeral": {"private": hexs(plain_pending.e_private),
                                            "public": rc.b64e(rc.public_of(plain_pending.e_private))},
                        "frame": plain},
        "session": {"k_c2b": hexs(k_c2b), "k_b2c": hexs(k_b2c), "sid": sid},
        "welcome": {"frame": acc.welcome, "info": {"device": acc.session.device, "t": now}},
        "req": {"meta": req_meta, "body": rc.b64e(req_body),
                "plaintext": rc.b64e(rc.pack(req_meta, req_body)), "frame": req},
        "res": {"meta": res_meta, "body": rc.b64e(res_body),
                "plaintext": rc.b64e(rc.pack(res_meta, res_body)), "frame": res},
        "sse": {"event": sse_event, "frame": sse},
        "handoff": {
            "about": ("k = PBKDF2-HMAC-SHA256(code, salt, iterations, 32 bytes); mailbox = hex "
                      "of the first 16 bytes of HMAC-SHA256(k, salt || ' mailbox' || 0x00 || "
                      "the relay URL without a trailing slash); key = HMAC-SHA256(k, salt || "
                      "' key'); the blob is base64url of nonce || AES-256-GCM(key, nonce, "
                      "payload, aad = salt || mailbox). This vector runs 1000 iterations; the "
                      "bot and the app run 600000 (production)."),
            "code": rc.format_handoff_code(code), "normalized": code,
            "salt": rc.HANDOFF_PROTOCOL.decode(), "iterations": 1000, "relay": relay,
            "secret": hexs(secret), "mailbox": mailbox, "key": hexs(key),
            "other_relay": {"relay": other_relay, "mailbox": rc.handoff_mailbox(secret, other_relay)},
            "nonce": hexs(nonce),
            "aad": rc.b64e(rc.HANDOFF_PROTOCOL + mailbox.encode()),
            "payload": handoff_payload.to_json().decode(), "blob": blob,
            "production": {"iterations": rc.HANDOFF_ITERATIONS, "secret": hexs(prod_secret),
                           "mailbox": prod_mailbox, "key": hexs(prod_key)},
            "normalize": normalize},
    }


def test_shared_vectors_match_relay_crypto():
    """The fixture is what relay_crypto makes; RELAY_VECTORS_WRITE=1 writes
    it again after a deliberate protocol change."""
    made = relay_vectors()
    if os.environ.get("RELAY_VECTORS_WRITE") == "1":
        VECTORS.write_text(json.dumps(made, indent=1) + "\n")  # key order is part of a frame
    assert json.loads(VECTORS.read_text()) == made
    v = made
    # The bot's side of the fixture: the hello verifies and the frames open.
    ident = rc.BotIdentity(bytes.fromhex(v["bot"]["private"]), rc.b64d(v["bot"]["token"]))
    assert ident.install_id == v["bot"]["install_id"]
    psk = rc.b64d(v["pairing"]["psk"])
    acc = rc.bot_accept(ident, v["hello"]["frame"], psk_for=lambda pid: psk, now=v["now"])
    assert acc.pairing_id == v["pairing"]["id"]
    assert rc.b64e(acc.device_public) == v["device"]["public"]
    assert acc.info == {"name": "vector phone", "t": v["now"]}
    assert acc.code == v["hello"]["code"] == rc.pairing_code(bytes.fromhex(v["hello"]["h"]))
    assert rc.PairingPayload.from_string(v["pairing"]["link"]).psk == psk
    # The handoff, from the contract's own formula rather than relay_crypto's.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    h = v["handoff"]
    import hmac as hmac_mod

    def contract(k: bytes, label: bytes) -> bytes:
        return hmac_mod.new(k, b"tacitus-handoff/1" + label, hashlib.sha256).digest()
    for it, want in ((h["iterations"], h), (600000, h["production"])):
        k = hashlib.pbkdf2_hmac("sha256", h["normalized"].encode(), b"tacitus-handoff/1", it, 32)
        assert k.hex() == want["secret"]
        assert contract(k, b" mailbox\x00" + h["relay"].encode())[:16].hex() == want["mailbox"]
        assert contract(k, b" key").hex() == want["key"]
    k = bytes.fromhex(h["secret"])
    other = h["other_relay"]
    assert contract(k, b" mailbox\x00" + other["relay"].rstrip("/").encode())[:16].hex() == other["mailbox"]
    assert other["mailbox"] != h["mailbox"]  # each relay sees a mailbox of its own
    raw = rc.b64d(h["blob"])
    assert raw[:12].hex() == h["nonce"]
    plain = AESGCM(bytes.fromhex(h["key"])).decrypt(
        raw[:12], raw[12:], b"tacitus-handoff/1" + h["mailbox"].encode())
    assert plain.decode() == h["payload"] and json.loads(plain)["v"] == 2
    assert rc.HANDOFF_ITERATIONS == 600000


# ── the web front end's side: start, routes, policy ───────────────────

def _web(tmp_path, relay_cfg=None, **cfg):
    """A web front end as the owner's bot builds it: a trusted block and a
    private store. The owner signs in with the pairing cookie (`_owner`)."""
    from botcore.chat.web_frontend import WebFrontend
    base = {"port": 0, "public_url": OWN_URL}
    if relay_cfg is not None:
        base["relay"] = relay_cfg
    base.update(cfg)
    f = WebFrontend(base, store_path=tmp_path / "priv" / "webchat.sqlite",
                    webui_dir=tmp_path / "webui", trusted_config=True)
    f.poll_s = f.pair_delay_s = 0.02
    return f


def _owner(f) -> dict:
    return {"Cookie": f"ea_web={f.pair_token()}"}


def _call(f, method, path, body=None, headers=None):
    """A request from a loopback client: a POST carries the loopback origin."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    if method == "POST":
        h["Origin"] = f"http://127.0.0.1:{f.port}"
    h.update(headers or {})
    req = urllib.request.Request(f.url + path, data=data, method=method, headers=h)
    try:
        with NO_PROXY.open(req, timeout=5) as r:
            return r.status, r.headers, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, json.loads(e.read() or b"null")


@pytest.fixture
def web(tmp_path):
    """A relay server and a WebFrontend whose relay block names it."""
    relay = rs.Relay(rs.Limits(keepalive=0.3, client_timeout=10), log=lambda line: None)
    server = _serve(rs.RelayHTTPServer(("127.0.0.1", 0), relay))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    # This relay serves no app; the tests below stand for one that does.
    from botcore.chat import web_frontend as wf
    wf._relay_app_seen[base] = (time.time(), True)
    # The hosted copy's list of relays, as read: it lists none (no network).
    rcl._hosted_relays_seen[rcl.DEFAULT_HOSTED_APP] = (time.time(), frozenset())
    f = _web(tmp_path, {"url": base, "wait": 1})
    f.start()
    deadline = time.time() + 10
    while f.relay is not None and not f.relay.connected and time.time() < deadline:
        time.sleep(0.05)
    assert f.relay is not None and f.relay.connected, f.relay and f.relay.last_error
    yield {"fe": f, "base": base, "dir": tmp_path, "relay": relay}
    wf._relay_app_seen.pop(base, None)
    rcl._hosted_relays_seen.pop(rcl.DEFAULT_HOSTED_APP, None)
    f.stop()
    server.shutdown()


def _phone(web, link, approve=True):
    """A phone that pairs with `link`; the owner approves it unless told not to."""
    phone = Phone(web["base"], rc.PairingPayload.from_string(link))
    status, welcome = phone.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
    if approve:
        assert web["fe"].relay.approve(phone.session.device)
    return phone


def test_the_web_front_end_runs_the_relay_client(web):
    f = web["fe"]
    client = f.relay
    assert isinstance(client, rcl.RelayClient) and client.local == f.loopback_url()
    # The key file defaults to the store's (owner-private) directory.
    assert (web["dir"] / "priv" / "webchat-relay.json").stat().st_mode & 0o077 == 0
    status, _h, me = _call(f, "GET", "/api/me", headers=_owner(f))
    assert status == 200 and me["relay"] is True and me["card_actions"] is True
    f.stop()
    assert f.relay is None and client._stop.is_set()


def test_the_relay_routes_pair_list_approve_and_revoke(web):
    f = web["fe"]
    status, _h, out = _call(f, "POST", "/api/relay/pair", {}, _owner(f))
    assert status == 200 and out["qr"] == out["link"] and out["origin"] == "own"
    assert out["link"].startswith(OWN_URL + "/#tr2=") and out["access"] == "all"
    assert abs(out["expires"] - (time.time() + rc.PAIRING_TTL)) < 5
    assert _call(f, "GET", "/api/relay/devices", headers=_owner(f))[2] == []
    phone = _phone(web, out["link"], approve=False)
    devices = _call(f, "GET", "/api/relay/devices", headers=_owner(f))[2]
    # The new phone waits, under the code the phone itself shows.
    assert len(phone.code) == 6 and phone.code.isdigit()
    assert devices == [{"id": phone.session.device, "name": "loopback phone",
                        "paired_at": devices[0]["paired_at"], "origin": "own",
                        "pending": True, "code": phone.code}]
    # The owner hears of it in the DM channel, code included.
    dm = [m["text"] for m in f.store.threads("dm", 50, None)]
    assert any("loopback phone" in t and f"{phone.code[:3]} {phone.code[3:]}" in t for t in dm), dm
    _, out = phone.call("GET", "/api/me")
    assert out["s"] == 403 and json.loads(out["body"])["pending"] is True
    status, _h, st = _call(f, "GET", "/api/relay/status", headers=_owner(f))
    assert st["enabled"] and st["connected"] and st["devices"] == 1 and st["pending"] == 1
    assert st["install_id"] == phone.payload.install_id
    # The app checks a link against these.
    assert st["bot_public"] == rc.b64e(phone.payload.bot_public)
    assert st["client_key"] == phone.payload.client_key and st["relay_url"] == web["base"]
    assert _call(f, "POST", "/api/relay/approve", {"device": "0" * 16}, _owner(f))[0] == 404
    assert _call(f, "POST", "/api/relay/approve", {"device": phone.session.device},
                 _owner(f))[:3:2] == (200, {"ok": True})
    assert phone.call("GET", "/api/me")[1]["s"] == 200
    assert _call(f, "GET", "/api/relay/devices", headers=_owner(f))[2][0]["pending"] is False
    assert _call(f, "POST", "/api/relay/revoke", {"device": "nope"}, _owner(f))[0] == 400
    assert _call(f, "POST", "/api/relay/revoke", {"device": "0" * 16}, _owner(f))[0] == 404
    assert _call(f, "POST", "/api/relay/revoke", {"device": phone.session.device},
                 _owner(f))[:3:2] == (200, {"ok": True})
    assert _call(f, "GET", "/api/relay/devices", headers=_owner(f))[2] == []
    _, out = phone.call("GET", "/api/me")
    assert out["k"] == "err" and out["e"] in ("session", "auth")
    # The routes are the owner's.
    assert _call(f, "POST", "/api/relay/pair", {})[0] == 401
    assert _call(f, "GET", "/api/relay/devices")[0] == 401
    assert _call(f, "POST", "/api/relay/approve", {"device": "0" * 16})[0] == 401


def test_the_owner_pairs_a_phone_through_the_relay_without_the_opt_in(web):
    """"Phone not on your network" in the owner's dialog: a v2 link to the
    relay's copy of the app with the bundle hash the bot signs, allowed
    while chat.json keeps relay_origin_access "off". The device waits for
    approval, then reads and chats, attested or not; its attestation
    records the check and grants nothing, and card buttons wait for the
    owner's `cards`."""
    from botcore.chat.web_frontend import WebFrontend
    f = web["fe"]
    f.registry.event("message")(lambda m: None)
    assert f.relay.relay_origin_access == "off"
    bundle = f.bundle_hash()
    st = _call(f, "GET", "/api/relay/status", headers=_owner(f))[2]
    assert (st["pair_own"], st["pair_relay"], st["bundle"]) == (True, True, bundle)
    assert _call(f, "POST", "/api/relay/pair", {"via": "elsewhere"}, _owner(f))[0] == 400
    # A bot that signs no app bundle: the relay way is closed, and says why.
    with mock.patch.object(WebFrontend, "bundle_hash", lambda self: ""):
        st = _call(f, "GET", "/api/relay/status", headers=_owner(f))[2]
        assert (st["pair_own"], st["pair_relay"], st["bundle"]) == (True, False, None)
        status, _h, out = _call(f, "POST", "/api/relay/pair", {"via": "relay"}, _owner(f))
        assert status == 409 and "bundle" in out["error"]
    status, _h, out = _call(f, "POST", "/api/relay/pair", {"via": "relay"}, _owner(f))
    own = _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]
    assert status == 200 and (out["origin"], out["access"], out["bundle"]) == ("relay", "chat", bundle)
    assert out["qr"] == out["link"] and out["link"].startswith(web["base"] + "/#tr2=")
    p = rc.PairingPayload.from_string(out["link"])
    assert (p.bundle, p.app, p.relay_url) == (bundle, web["base"] + "/", web["base"])
    assert own["origin"] == "own" and own["access"] == "all"
    assert rc.PairingPayload.from_string(own["link"]).bundle == bundle
    phone = _phone(web, out["link"], approve=False)
    device = phone.session.device
    dev = {d["id"]: d for d in f.relay.devices()}[device]
    assert (dev["origin"], dev["pending"], dev["bundle_verified"]) == ("relay", True, False)
    assert phone.call("GET", "/api/me")[1]["s"] == 403  # approval first, as for any device
    assert _attest(phone, bundle)["s"] == 403  # nor may it attest while it waits
    assert f.relay.approve(device)
    _, me = phone.call("GET", "/api/me")
    assert me["s"] == 200 and json.loads(me["body"])["card_actions"] is False
    text = json.dumps({"text": "from a phone that has not attested"}).encode()
    assert phone.call("POST", "/api/channels/dm/messages", text)[1]["s"] == 200
    click = json.dumps({"action_id": "approve_send"}).encode()
    assert phone.call("POST", "/api/cards/dm/w1/actions", click)[1]["s"] == 403
    # Its attestation is recorded and grants nothing.
    assert _attest(phone, bundle)["s"] == 200
    _, me = phone.call("GET", "/api/me")
    assert json.loads(me["body"])["card_actions"] is False
    assert phone.call("POST", "/api/cards/dm/w1/actions", click)[1]["s"] == 403
    rows = _call(f, "GET", "/api/relay/devices", headers=_owner(f))[2]
    assert [(d["id"], d["bundle_verified"], d["cards_allowed"]) for d in rows] == [(device, True, False)]
    # The owner's `cards` lets it confirm a card.
    assert _call(f, "POST", "/api/relay/approve", {"device": device, "cards": True}, _owner(f))[0] == 200
    _, me = phone.call("GET", "/api/me")
    assert json.loads(me["body"])["card_actions"] is True


def test_relayed_requests_never_reach_the_pairing_routes(web):
    """A relayed request reaches no pairing route, and of the device
    routes only the four relayed owner routes, in their exact spelling,
    from an approved own-origin phone (tests below)."""
    f = web["fe"]
    marks = {**_owner(f), "X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "allow"}
    for method, path in (("POST", "/api/relay/pair"), ("GET", "/api/relay/devices"),
                         ("GET", "/api/relay/status"), ("POST", "/api/relay/revoke"),
                         ("POST", "/api/relay/approve"), ("POST", "/api/relay/handoff"),
                         ("POST", "/api/pair"), ("POST", "/api/%72elay/pair")):
        assert _call(f, method, path, {} if method == "POST" else None, marks)[0] == 403, path
    own = _phone(web, _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"])
    before = len(f.relay.store.load()["pairings"])
    for method, path in (("POST", "/api/relay/pair"), ("POST", "/api/%72elay/pair"),
                         ("GET", "/api/relay/status"), ("GET", "/api/Relay/status"),
                         ("POST", "/api/p%61ir"), ("POST", "/api/pair/x"),
                         ("GET", "/api/relay/Devices"), ("GET", "/api/%72elay/devices"),
                         ("GET", "/api/relay/devices?x=1"), ("GET", "/api/relay/devices/"),
                         ("POST", "/api/relay/handoff/"), ("POST", "/api/relay/Handoff"),
                         ("GET", "/api/relay/handoff"), ("POST", "/api/relay/devices"),
                         ("PATCH", "/api/relay/approve"), ("DELETE", "/api/relay/revoke")):
        _, out = own.call(method, path, b"{}")
        assert out["s"] == 403, path
    assert len(f.relay.store.load()["pairings"]) == before


def test_a_relay_origin_phone_never_confirms_a_card(web):
    """The invariant: a request marked X-Tacitus-Relay-Cards: deny presses
    no card button, subscribes no push, and its text is never taken as a
    typed card code."""
    from botcore.chat.cards import CardAction, CardRegistry, ConfirmationCard, resolve_code
    from botcore.chat.types import ActionEvent, InboundMessage
    f = web["fe"]
    f.card_registry = CardRegistry(web["dir"] / "priv" / "cards.json")
    got, seen = [], threading.Event()
    f.registry.event("message")(lambda m: (got.append(m), seen.set()))
    f.registry.action("approve_send")(lambda a: (got.append(a), seen.set()))
    card = ConfirmationCard(kind="send", title="Send this draft?", body="Hello.",
                            actions=[CardAction("approve_send", "Send", {"t": 1}, "primary", "send")])
    ref = f.post_card("dm", card)
    f.card_registry.add(ref, card, code="K7Q2")
    f.relay.relay_origin_access = "chat"  # as chat.json opting in would set it
    phone = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/", relay_origin=True))
    assert {d["id"]: d["origin"] for d in f.relay.devices()}[phone.session.device] == "relay"
    _, me = phone.call("GET", "/api/me")
    assert json.loads(me["body"])["card_actions"] is False
    click = json.dumps({"action_id": "approve_send"}).encode()
    _, out = phone.call("POST", f"/api/cards/dm/{ref.id}/actions", click)
    assert out["s"] == 403
    sub = json.dumps({"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                      "keys": {"p256dh": "x", "auth": "y"}}).encode()
    _, out = phone.call("POST", "/api/push/subscribe", sub)
    assert out["s"] in (403, 404)  # 404 only when push is off on this host
    # A live code, typed in its card's thread, reaches the core as text.
    assert resolve_code("send K7Q2", f.card_registry, "owner", "owner",
                        thread_key=ref.thread_key) is not None
    body = json.dumps({"text": "send K7Q2", "thread_key": ref.thread_key}).encode()
    _, out = phone.call("POST", "/api/channels/dm/messages", body)
    assert out["s"] == 200 and seen.wait(5)
    time.sleep(0.2)
    assert [type(x) for x in got] == [InboundMessage] and got[0].text == "send K7Q2"
    assert not any(isinstance(x, ActionEvent) for x in got)


def _attest(phone, bundle: str) -> dict:
    return phone.call("POST", "/api/relay/attest", json.dumps({"bundle": bundle}).encode())[1]


def test_a_relay_origin_phone_presses_cards_only_when_the_owner_allows_it(web):
    """A relay-origin device gets `X-Tacitus-Relay-Cards: deny` whatever it
    attests: the code that checks itself came from the relay. Its check is
    recorded (`bundle_verified`, the release). Only the owner's approval
    with `cards` gives it `allow`, and approving again without takes it
    back. "off" in chat.json still keeps it out, and a "read" device may
    post its attestation and nothing else."""
    from botcore.chat.cards import CardAction, ConfirmationCard
    f = web["fe"]
    got = []
    f.registry.action("approve_send")(got.append)
    card = ConfirmationCard(kind="send", title="Send this draft?", body="Hello.",
                            actions=[CardAction("approve_send", "Send", {"t": 1}, "primary", "send")])
    ref = f.post_card("dm", card)
    click = json.dumps({"action_id": "approve_send"}).encode()
    f.relay.relay_origin_access = "chat"
    phone = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/", relay_origin=True))
    device = phone.session.device

    def me():
        return json.loads(phone.call("GET", "/api/me")[1]["body"])

    def row():
        return {d["id"]: d for d in f.relay.devices()}[device]
    assert me()["card_actions"] is False and row()["cards_allowed"] is False
    # The phone checks the bot's signed manifest, fetched through the relay.
    out = phone.call("GET", "/api/bundle")[1]
    assert out["s"] == 200
    m = rc.verify_manifest(json.loads(out["body"]))
    assert rc.bundle_hash(m) == f.bundle_hash() == me()["bundle"]
    out = _attest(phone, "0" * 64)
    assert out["s"] == 409 and row()["bundle_verified"] is False
    out = _attest(phone, rc.bundle_hash(m))
    assert out["s"] == 200 and row()["bundle_verified"] is True
    # Verified, and still no card buttons.
    assert me()["card_actions"] is False
    assert phone.call("POST", f"/api/cards/dm/{ref.id}/actions", click)[1]["s"] == 403
    # The owner allows them for this device.
    assert _call(f, "POST", "/api/relay/approve", {"device": device, "cards": True},
                 _owner(f))[0] == 200
    assert me()["card_actions"] is True and row()["cards_allowed"] is True
    assert phone.call("POST", f"/api/cards/dm/{ref.id}/actions", click)[1]["s"] == 200
    deadline = time.time() + 5
    while not got and time.time() < deadline:
        time.sleep(0.05)
    assert [a.action_id for a in got] == ["approve_send"]
    # Approved again without `cards`: denied again.
    assert f.relay.approve(device) and me()["card_actions"] is False
    # "off" keeps every relay-origin device out, whatever the owner allowed.
    f.relay.approve(device, cards=True)
    f.relay.relay_origin_access = "off"
    out = phone.call("GET", "/api/me")[1]
    assert out["k"] == "err" and out["e"] == "auth"
    # At "read" a device posts its attestation and nothing else.
    f.relay.relay_origin_access = "read"
    reader = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/", relay_origin=True))
    body = json.dumps({"text": "hello"}).encode()
    assert reader.call("POST", "/api/channels/dm/messages", body)[1]["s"] == 403
    assert _attest(reader, f.bundle_hash())["s"] == 200
    assert reader.call("POST", "/api/channels/dm/messages", body)[1]["s"] == 403


def test_the_cli_approves_with_card_buttons(tmp_path, capsys):
    """`approve --cards` lets a relay-origin device press card buttons;
    `approve` alone does not, and `show` lists both and the check."""
    key = tmp_path / "relay-key.json"
    store = rcl.KeyStore.create(key, relay_url="https://relay.example.net")
    pairing = store.add_pairing(relay_url="https://relay.example.net", origin="relay")
    device = store.register(pairing.pairing_id, bytes(range(32)), "phone")["id"]
    store.attest(device, "a" * 64, "v1.2.3")
    assert rcl.main(["approve", "--key-file", str(key), "--device", device, "--cards"]) == 0
    assert store.device_by_id(device)["cards_allowed"] is True
    assert rcl.main(["approve", "--key-file", str(key), "--device", device]) == 0
    assert store.device_by_id(device)["cards_allowed"] is False
    capsys.readouterr()
    assert rcl.main(["show", "--key-file", str(key)]) == 0
    (row,) = json.loads(capsys.readouterr().out)["devices"]
    assert row == {"id": device, "name": "phone", "paired_at": row["paired_at"], "origin": "relay",
                   "pending": False, "cards_allowed": False, "bundle_verified": True,
                   "release": "v1.2.3"}


def test_the_page_policy_names_the_relay_origin(tmp_path):
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<!doctype html><title>t</title>")
    cases = [
        (None, "connect-src 'self';"),
        ({"url": "https://Relay.Example.net:8443"},
         "connect-src 'self' https://relay.example.net:8443;"),
        # A relay under a path of a shared host: that path only.
        ({"url": "https://Relay.Example.net:8443/relay/"},
         "connect-src 'self' https://relay.example.net:8443/relay/;"),
        ({"url": "https://relay.example.net/a/../b"}, "connect-src 'self';"),
        ({"url": "https://relay.example.net", "enabled": False}, "connect-src 'self';"),
        ({"url": "https://relay.example.net; script-src *"}, "connect-src 'self';"),
        ({"url": "https://user@relay.example.net"}, "connect-src 'self';"),
    ]
    for i, (relay, want) in enumerate(cases):
        f = _web(tmp_path / str(i), relay)
        assert want in f.html_csp, (relay, f.html_csp)
    f = _web(tmp_path / "served", {"url": "https://relay.example.net", "enabled": False})
    f.webui_dir = tmp_path / "webui"
    f.start()
    try:
        assert f.relay is None
        req = urllib.request.Request(f.url + "/")
        with NO_PROXY.open(req, timeout=5) as r:
            assert "connect-src 'self';" in r.headers["Content-Security-Policy"]
        assert _call(f, "GET", "/api/relay/status", headers=_owner(f))[2] == \
            {"enabled": False, "handoff": False, "hosted_app": ""}
        assert _call(f, "POST", "/api/relay/pair", {}, _owner(f))[0] == 404
        status, _h, out = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
        assert status == 409 and not JARGON.search(out["error"]), out
        me = _call(f, "GET", "/api/me", headers=_owner(f))[2]
        assert me["relay"] is False and (me["owner_routes"], me["handoff"]) == ("all", False)
    finally:
        f.stop()


def test_a_relay_that_cannot_start_leaves_the_chat_running(tmp_path):
    f = _web(tmp_path, {"url": "http://relay.example.net", "key_file": str(tmp_path / "k.json")})
    f.start()
    try:
        assert f.relay is None  # plain http off loopback: refused, logged
        assert _call(f, "GET", "/api/me", headers=_owner(f))[0] == 200
    finally:
        f.stop()


def test_get_chat_keeps_the_relay_block_only_from_chat_json(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "preprocessor"))
    import ea_config
    block = {"frontend": "web", "web": {"port": 9000, "relay": {"url": "https://relay.example.net"}}}
    p = tmp_path / "ea-config.json"
    p.write_text(json.dumps({"chat": block}))
    assert "relay" not in ea_config.get_chat(p)["web"]
    monkeypatch.setattr(ea_config, "CONFIG_FILE", p)
    owner = ea_config._owner_trust()
    monkeypatch.setattr(owner, "owner_chat_block", lambda: None)
    assert "relay" not in ea_config.get_chat()["web"]          # not an owner process
    monkeypatch.setattr(owner, "owner_chat_block", lambda: json.loads(json.dumps(block)))
    assert ea_config.get_chat()["web"]["relay"] == {"url": "https://relay.example.net"}


# ── attacks on the relay, the bot and its key file, as assertions ─────
#
# Each test is one way a stranger, a compromised relay or another user on
# the bot host could get in, and checks that it no longer does.

def _raw(base: str, request: bytes) -> bytes:
    """Send raw bytes to the relay; everything it answers until it closes."""
    import socket
    host, port = base.split("//", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request)
        answer = b""
        while chunk := sock.recv(65536):
            answer += chunk
    return answer


def test_a_bot_token_is_checked_before_the_body_is_read(loop):
    """A stranger cannot make the relay read and parse 16 MiB on the bot
    route: no token, a wrong one, or one for another install is refused
    from the headers, at once, although the body never comes."""
    base, install = loop["base"], loop["phone"].payload.install_id
    for auth, want in (("", b"401"), ("Bearer " + rc.b64e(os.urandom(32)), b"403"),
                       ("Bearer " + "A" * 5000, b"401"), ("Basic x", b"401")):
        headers = {"Content-Type": "application/json", "Content-Length": "16000000"}
        if auth:
            headers["Authorization"] = auth
        took, answer = _raw_post(base, f"/r/{install}/bot", headers)
        assert took < 3 and answer.startswith(b"HTTP/1.1 " + want), (auth[:12], answer[:40])
    # With the right token, a body nested past the parser's depth is a 400.
    token = rcl.KeyStore(loop["dir"] / "relay-key.json").identity().token
    body = b"[" * 100000
    answer = _raw(base, (f"POST /r/{install}/bot HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}"
                         f"\r\nAuthorization: Bearer {rc.b64e(token)}\r\nConnection: close\r\n\r\n"
                         ).encode() + body)
    assert answer.startswith(b"HTTP/1.1 400") and answer.endswith(b'{"error":"not JSON"}')


def test_healthz_gives_counts_only_to_a_monitor_on_the_host(loop):
    """Through the proxy (X-Forwarded-For), /healthz says only that the
    relay is up: the counts would tell a stranger when an install is online."""
    base = loop["base"]
    with NO_PROXY.open(base + "/healthz", timeout=5) as r:
        assert set(json.loads(r.read())) == {"ok", "installs", "streams", "queued_bytes",
                                             "handoffs"}
    req = urllib.request.Request(base + "/healthz", headers={"X-Forwarded-For": "198.51.100.7"})
    with NO_PROXY.open(req, timeout=5) as r:
        assert json.loads(r.read()) == {"ok": True}


def test_the_relay_answers_in_json_and_sends_the_safety_headers(loop):
    base = loop["base"]
    head = _raw(base, b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 405") and head.endswith(b"\r\n\r\n")  # no body
    put = _raw(base, b"PUT /r/x/c HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
    assert put.startswith(b"HTTP/1.1 501") and put.endswith(b'{"error":"not implemented"}')
    bad = _raw(base, b"GET /\x01<script> HTTP/9.9\r\n\r\n")
    assert b"<script>" not in bad and b"html" not in bad.lower()
    with NO_PROXY.open(base + "/", timeout=5) as r:
        assert r.headers["Content-Type"] == "text/html; charset=utf-8"
        assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    with NO_PROXY.open(base + "/healthz", timeout=5) as r:
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_a_counter_jump_takes_no_memory():
    """A counter far ahead starts a new bitmap; shifting the old one by the
    jump would build an integer of that many bits (gigabytes)."""
    w = rc.ReplayWindow()
    w.mark(0)
    t0 = time.monotonic()
    w.mark(1 << 40)
    assert time.monotonic() - t0 < 0.1 and w.bits == 1 and w.top == 1 << 40
    assert w.seen(1 << 40) and not w.seen((1 << 40) - 1) and w.seen(0)
    w.mark((1 << 40) - 3)
    assert w.seen((1 << 40) - 3) and w.bits == 0b1001


def test_a_hello_cannot_be_replayed_while_its_clock_still_passes():
    """A hello stamped up to HELLO_SKEW ahead passes the clock check for
    2 * HELLO_SKEW: the cache keeps its key that long."""
    bot = rc.BotIdentity.generate()
    t = 2_000_000_000
    frame, _ = rc.client_hello(bot.public, bot.install_id, rc.generate_private(), now=t + 590)
    cache, clock = rc.HelloCache(), {"now": t}
    seen = lambda k: cache.seen(k, now=clock["now"])  # noqa: E731
    rc.bot_accept(bot, frame, psk_for=lambda p: None, seen=seen, now=t)
    for dt in (300, 601, 1100, 1189, 1191):
        clock["now"] = t + dt
        with pytest.raises(rc.HandshakeError, match="replayed|clock"):
            rc.bot_accept(bot, frame, psk_for=lambda p: None, seen=seen, now=t + dt)


def test_a_full_hello_cache_refuses_rather_than_forgets():
    cache = rc.HelloCache(ttl=10, limit=2)
    assert [cache.seen(k, now=0) for k in (b"a", b"b", b"c")] == [False, False, True]
    assert cache.seen(b"c", now=11) is False  # the old keys aged out


def test_a_hello_with_a_huge_clock_is_refused(monkeypatch):
    ident = rc.BotIdentity.generate()

    class Shim:
        @staticmethod
        def dumps(obj, **kw):
            return '{"name": "x", "t": 1%s}' % ("0" * 400)
    monkeypatch.setattr(rc, "json", Shim)
    hello, _ = rc.client_hello(ident.public, ident.install_id, rc.generate_private())
    monkeypatch.undo()
    with pytest.raises(rc.HandshakeError, match="payload"):
        rc.bot_accept(ident, hello, psk_for=lambda pid: None)


def test_the_pairing_code_is_the_same_on_both_sides():
    ident = rc.BotIdentity.generate()
    payload = rc.new_pairing(ident, "https://relay.example.net")
    hello, pending = rc.client_hello(payload.bot_public, payload.install_id, rc.generate_private(),
                                     pairing=(payload.pairing_id, payload.psk))
    acc = rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk)
    assert acc.code == rc.pairing_code(pending.h) and len(acc.code) == 6 and acc.code.isdigit()
    other, pending2 = rc.client_hello(payload.bot_public, payload.install_id, rc.generate_private(),
                                      pairing=(payload.pairing_id, payload.psk))
    assert rc.pairing_code(pending2.h) != acc.code or pending2.h != pending.h


class _Evil(BaseHTTPRequestHandler):
    """A relay under an attacker's control; `answer(handler, body)` answers."""
    answer = None
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        type(self).calls.append(body)
        type(self).answer(self, body)

    do_GET = do_POST


def _evil(answer) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Evil", (_Evil,), {"answer": staticmethod(answer), "calls": []})
    server = _serve(ThreadingHTTPServer(("127.0.0.1", 0), handler))
    server.daemon_threads = True
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _json_answer(h, status, obj, length=None):
    data = json.dumps(obj).encode()
    h.send_response(status)
    h.send_header("Content-Length", str(len(data) if length is None else length))
    h.end_headers()
    try:
        h.wfile.write(data)
    except OSError:
        pass


def _client(tmp_path, relay_url, local="http://127.0.0.1:9/chat", **kw):
    store = rcl.KeyStore.create(tmp_path / f"k{len(list(tmp_path.iterdir()))}.json")
    return rcl.RelayClient(relay_url, store, local, **kw)


def test_neither_opener_follows_a_redirect(tmp_path, loop):
    """A relay that answers 302 to a loopback service gets an error, not a
    request carrying the relay token; nor does the loopback server move a
    replayed request (and the owner's cookie) elsewhere."""
    hits = []
    target, target_url = _evil(lambda h, b: (hits.append(h.path), _json_answer(h, 200, {"frames": []})))

    def redirect(h, body):
        h.send_response(302)
        h.send_header("Location", target_url + "/internal?x=1")
        h.send_header("Content-Length", "0")
        h.end_headers()
    relay, relay_url = _evil(redirect)
    try:
        client = _client(tmp_path, relay_url, wait=1)
        with pytest.raises(urllib.error.HTTPError) as e:
            client._exchange([], 0)
        assert e.value.code == 302 and hits == []
        e.value.close()
    finally:
        relay.shutdown()
        target.shutdown()
    _, out = loop["phone"].call("GET", "/api/redirect")
    assert out["s"] == 302 and "location" not in out["h"]


def test_an_early_empty_answer_pauses_the_poll(tmp_path):
    """A relay that answers every long-poll at once with nothing cannot
    spin the bot's poll loop."""
    relay, relay_url = _evil(lambda h, b: _json_answer(h, 200, {"frames": []}))
    client = _client(tmp_path, relay_url, wait=25)
    try:
        client.start()
        time.sleep(2.2)
        assert 1 <= len(relay.RequestHandlerClass.calls) <= 4
    finally:
        client.stop()
        relay.shutdown()


def test_a_flood_from_the_relay_is_bounded(tmp_path, caplog):
    """5,000 bogus hellos per answer: the bot takes 64 of an answer, queues
    a bounded number for its workers, drops the rest, and logs one line."""
    def flood(h, body):
        if body.get("frames"):
            return _json_answer(h, 200, {"frames": []})
        time.sleep(0.3)
        n = len(h.calls)
        _json_answer(h, 200, {"frames": [{"id": f"f{n}x{i}", "k": "hello", "c": "AAAA"}
                                         for i in range(5000)]})
    relay, relay_url = _evil(flood)
    lg = logging.getLogger("test.relay.flood")
    client = _client(tmp_path, relay_url, wait=25, logger=lg)
    try:
        with caplog.at_level(logging.INFO, logger="test.relay.flood"):
            client.start()
            time.sleep(2.5)
            backlog = client._pool._work_queue.qsize()
        assert backlog <= 64 and client.dropped > 5000
        refused = [r for r in caplog.records if "hello refused" in r.getMessage()]
        assert len(refused) == 1, [r.getMessage() for r in caplog.records]
    finally:
        client.stop()
        relay.shutdown()


def test_the_relays_words_never_reach_the_log(tmp_path, caplog):
    """An error body is not read: a forged log line in it goes nowhere, and
    a body of hundreds of MiB costs nothing."""
    def forged(h, body):
        _json_answer(h, 500, {"error": "x\nWARNING chat.relay: relay: connected (forged)"})
    relay, relay_url = _evil(forged)
    lg = logging.getLogger("test.relay.words")
    client = _client(tmp_path, relay_url, wait=1, logger=lg)
    try:
        with caplog.at_level(logging.INFO, logger="test.relay.words"):
            with pytest.raises(urllib.error.HTTPError) as e:
                client._exchange([], 0)
            client._note_error(e.value)
        assert [r.getMessage() for r in caplog.records] == ["relay: HTTP 500"]
        assert client.status()["last_error"] == "HTTP 500"
    finally:
        relay.shutdown()

    def huge(h, body):
        h.send_response(500)
        h.send_header("Content-Length", str(400 << 20))
        h.end_headers()
        try:
            for _ in range(400):
                h.wfile.write(b"[" * (1 << 20))
        except OSError:
            pass
    relay, relay_url = _evil(huge)
    try:
        client = _client(tmp_path, relay_url, wait=1, logger=lg)
        t0 = time.monotonic()
        with pytest.raises(urllib.error.HTTPError) as e:
            client._exchange([], 0)
        client._note_error(e.value)
        assert time.monotonic() - t0 < 3 and client.last_error == "HTTP 500"
    finally:
        relay.shutdown()


def test_header_values_are_printable_ascii(tmp_path):
    client = _client(tmp_path, "https://relay.example.net")
    got = client._headers("d" * 16, {"accept": "text/plain ", "content-type": "café",
                                     "last-event-id": "7"})
    assert "accept" not in got and "content-type" not in got and got["last-event-id"] == "7"


def test_a_plain_request_never_holds_an_event_stream(loop):
    """A `req` for the event stream is refused (an `open` frame opens it),
    and an answer that turns out to be a stream is cut at once: eight such
    requests no longer tie up the bot's workers."""
    phone = loop["phone"]
    for path in ("/api/events", "/api/events?since=1", "/api/%65vents", "/api/Events/"):
        assert phone.call("GET", path)[1]["s"] == 403, path
    t0 = time.monotonic()
    _, out = phone.call("GET", "/api/sneaky")
    assert out["s"] == 502 and time.monotonic() - t0 < 5
    frame = phone.session.seal("ping", rc.new_id(), rc.pack({}))
    assert "pong" in rc.unpack(phone.session.open(phone.post(frame)[1]))[0]


def test_sessions_are_capped_per_device(tmp_path):
    client = _client(tmp_path, "https://relay.example.net")
    for i in range(rcl.MAX_DEVICE_SESSIONS + 3):
        s = rc.Session(f"{i:032x}", os.urandom(32), os.urandom(32), "bot", device="a" * 16)
        s.last_used = time.time() - 100 + i
        client._add_session(s)
    client._add_session(rc.Session("f" * 32, os.urandom(32), os.urandom(32), "bot", device="b" * 16))
    mine = [s for s in client._sessions.values() if s.device == "a" * 16]
    assert len(mine) == rcl.MAX_DEVICE_SESSIONS and len(client._sessions) == rcl.MAX_DEVICE_SESSIONS + 1


def _stream_reader(phone, fid, since=None):
    """Open the phone's event stream through the relay; (frames seen, the
    response). A thread appends each frame as it comes."""
    ticket = phone.ticket(phone.session.seal("open", fid, rc.pack({"since": since} if since else {})))
    resp = NO_PROXY.open(f"{phone.url}/sse?t={ticket}", timeout=15)
    frames: list = []

    def read():
        try:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    frames.append((time.monotonic(), json.loads(line[6:])))
        except (OSError, ValueError):
            pass
    threading.Thread(target=read, daemon=True).start()
    return frames, resp


def test_a_removed_device_loses_its_open_event_stream(web):
    """Removing a phone ends its open stream with `err auth` at once; no
    event posted after reaches it."""
    f = web["fe"]
    phone = _phone(web, f.relay.new_pairing())
    fid = rc.new_id()
    frames, resp = _stream_reader(phone, fid)
    deadline = time.time() + 10
    while not frames and time.time() < deadline:
        time.sleep(0.05)
    assert frames and frames[0][1]["k"] == "sse"  # the stream is open at the bot
    t0 = time.monotonic()
    assert _call(f, "POST", "/api/relay/revoke", {"device": phone.session.device},
                 _owner(f))[0] == 200
    f.post("main", "posted after the phone was removed")
    deadline = time.time() + 10
    while not any(fr["k"] == "err" for _, fr in frames) and time.time() < deadline:
        time.sleep(0.05)
    after = [fr for t, fr in frames if t > t0]
    assert {"id": fid, "k": "err", "e": "auth"} in after
    assert not [fr for fr in after if fr["k"] == "sse"]
    assert f.relay.status()["streams"] == 0
    resp.close()


def test_a_waiting_device_opens_no_stream(web):
    f = web["fe"]
    phone = _phone(web, f.relay.new_pairing(), approve=False)
    fid = rc.new_id()
    frames, resp = _stream_reader(phone, fid)
    deadline = time.time() + 10
    while not frames and time.time() < deadline:
        time.sleep(0.05)
    assert frames and frames[0][1]["k"] == "end"
    assert rc.unpack(phone.session.open(frames[0][1]))[0] == {"s": 403}
    resp.close()


def test_one_frame_id_holds_one_stream(web):
    """Seventy `open` frames with one id leave one stream at the bot, and
    the owner's own requests still get a connection."""
    from botcore.chat.web_frontend import GONE_CHECK_S
    f = web["fe"]
    client = f.relay
    phone = _phone(web, client.new_pairing())
    sent: list = []
    client._send = sent.append  # the relay's side swallows what the bot posts
    fid = "a" * 32
    opened, handled = [], []
    handle = client._handle

    def counted(frame):  # each frame a worker has finished with
        try:
            handle(frame)
        finally:
            handled.append(frame)
    client._handle = counted

    def open_stream():
        opened.append(fid)
        client._dispatch([phone.session.seal("open", fid, rc.pack({}))])

    def threads():
        return [t for t in threading.enumerate() if t.name == f"relay-sse-{fid[:8]}"]

    def settled():  # every frame handled or dropped, none in a worker
        return len(handled) + client.dropped == len(opened)

    for _ in range(70):
        open_stream()
    # Polled, not asserted once: on a slow runner the workers open the
    # streams late, and the local server holds a dropped stream's
    # connection for up to GONE_CHECK_S. A last `open` the server closed at
    # once (all its connections held) ends with an `end` frame, and the
    # phone then opens again; so does this test.
    deadline, reopen = time.time() + 20, time.time() + 2
    while not (settled() and client.status()["streams"] == 1 and len(threads()) == 1):
        assert time.time() < deadline, (client.status()["streams"], len(threads()), client.dropped)
        if time.time() > reopen and settled() and not threads():
            open_stream()
            reopen = time.time() + 2
        time.sleep(0.1)
    # The streams it replaced are gone; the owner's request gets a
    # connection once the server has seen them go.
    deadline = time.time() + GONE_CHECK_S + 4
    while True:
        try:
            assert _call(f, "GET", "/api/me", headers=_owner(f))[0] == 200
            break
        except OSError:  # a connection the server closed at once: its slots are still held
            assert time.time() < deadline
            time.sleep(0.2)
    assert client.status()["streams"] == 1 and len(threads()) == 1
    client._on_close(fid)


def test_relay_origin_devices_are_off_unless_chat_json_opts_in(web, tmp_path):
    """By default the bot makes no relay-origin pairing without the app
    bundle's hash and lets no such device in. A bot with no public_url
    (no tailnet) pairs only through the relay, with the hash."""
    f = web["fe"]
    client = f.relay
    assert client.relay_origin_access == "off"
    with pytest.raises(rcl.RelayError, match="own origin"):
        client.new_pairing(app_url=web["base"] + "/")
    with pytest.raises(rcl.RelayError, match="off"):
        client.new_pairing(app_url=web["base"] + "/", relay_origin=True)
    # A relay-origin pairing written to the key file (the CLI with
    # --relay-origin) is refused at the hello, and stays unspent.
    payload = client.store.add_pairing(relay_url=client.relay_url, origin="relay")
    phone = Phone(web["base"], payload)
    status, answer = phone.hello()
    assert answer == {"id": answer["id"], "k": "err", "e": "auth"}
    assert client.store.pairing_psk(payload.pairing_id) == payload.psk
    # One paired while the config let it in is refused once it does not.
    client.relay_origin_access = "chat"
    assert phone.hello()[1]["k"] == "welcome" and client.approve(phone.session.device)
    assert phone.call("GET", "/api/me")[1]["s"] == 200
    client.relay_origin_access = "off"
    _, out = phone.call("GET", "/api/me")
    assert out["k"] == "err" and out["e"] == "auth"
    assert phone.hello(pairing=False)[1]["e"] == "auth"
    # A web block without public_url: the UI pairs nothing.
    g = _web(tmp_path / "nopublic", {"url": web["base"], "wait": 1}, public_url="")
    g.start()
    try:
        deadline = time.time() + 10
        while g.relay is not None and not g.relay.connected and time.time() < deadline:
            time.sleep(0.05)
        status, _h, out = _call(g, "POST", "/api/relay/pair", {}, _owner(g))
        assert status == 409 and "public_url" in out["error"]
        st = _call(g, "GET", "/api/relay/status", headers=_owner(g))[2]
        assert (st["pair_own"], st["pair_relay"], st["bundle"]) == (False, True, g.bundle_hash())
        status, _h, out = _call(g, "POST", "/api/relay/pair", {"via": "relay"}, _owner(g))
        assert status == 200 and out["origin"] == "relay" and out["bundle"] == g.bundle_hash()
        assert out["link"].startswith(web["base"] + "/#tr2=")
    finally:
        g.stop()


def test_push_subscriptions_go_with_their_device(web):
    """A subscription made through the relay, or naming the phone's device
    on the tailnet, goes when the device is removed: from the UI at once,
    from the CLI at the next push."""
    from botcore.chat import web_push as wp
    if not wp.AVAILABLE:
        pytest.skip("web push needs the cryptography package")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    def sub(n):
        key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": f"https://fcm.googleapis.com/fcm/send/made-up-{n}",
                "keys": {"p256dh": wp.b64u(key), "auth": wp.b64u(os.urandom(16))}}
    f = web["fe"]
    one = _phone(web, f.relay.new_pairing())
    two = _phone(web, f.relay.new_pairing())
    assert one.call("POST", "/api/push/subscribe", json.dumps(sub(1)).encode())[1]["s"] == 200
    assert _call(f, "POST", "/api/push/subscribe", {**sub(2), "device": one.session.device},
                 _owner(f))[0] == 200
    assert _call(f, "POST", "/api/push/subscribe", sub(3), _owner(f))[0] == 200
    assert two.call("POST", "/api/push/subscribe", json.dumps(sub(4)).encode())[1]["s"] == 200
    devices = {r["endpoint"][-1]: r["device"] for r in f.store.push_subscriptions()}
    assert devices == {"1": one.session.device, "2": one.session.device, "3": None,
                       "4": two.session.device}
    assert _call(f, "POST", "/api/relay/revoke", {"device": one.session.device}, _owner(f))[0] == 200
    assert sorted(r["endpoint"][-1] for r in f.store.push_subscriptions()) == ["3", "4"]
    f.relay.store.revoke(two.session.device)  # as the CLI does, in the key file
    f.push.prune()
    assert [r["endpoint"][-1] for r in f.store.push_subscriptions()] == ["3"]


def test_the_key_file_must_be_the_owners_alone(tmp_path):
    """A key file in a directory another user may write (or inside the
    resident's tree) leaves the relay off: there it could be deleted for a
    new identity, or an old copy put back with a removed device in it."""
    from botcore.chat.web_frontend import WebFrontend
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    resident = tmp_path / "resident"
    resident.mkdir(mode=0o700)
    for i, (key_file, roots) in enumerate(((shared / "k.json", ()),
                                           (resident / "k.json", (resident,)))):
        f = WebFrontend({"port": 0, "public_url": OWN_URL,
                         "relay": {"url": "https://relay.example.net", "key_file": str(key_file)}},
                        store_path=tmp_path / f"priv{i}" / "webchat.sqlite",
                        webui_dir=tmp_path / "webui", trusted_config=True, untrusted_roots=roots)
        f.start()
        try:
            assert f.relay is None and not key_file.exists()
        finally:
            f.stop()


# ── rotation (scripts/webchat-relay.py rotate) ────────────────────────

def _relay_cli():
    """scripts/webchat-relay.py, loaded as a module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("webchat_relay_cli",
                                                  ROOT / "scripts" / "webchat-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _add_device(store, key: bytes, pending: bool = False) -> str:
    """A device record for `key`, written into the key file as a pairing
    would leave it; its id."""
    pub = rc.public_of(key)
    rec = {"id": rc.device_id_for(pub), "public": rc.b64e(pub), "name": "phone",
           "paired_at": 1793421333, "origin": "own"}
    if pending:
        rec.update({"pending": True, "code": "123456"})
    store._update(lambda data: data["devices"].append(rec))
    return rec["id"]


def _add_open_pairing(store) -> None:
    store._update(lambda data: data["pairings"].append(
        {"id": os.urandom(16).hex(), "psk": rc.b64e(os.urandom(32)),
         "expires": int(time.time()) + 600, "origin": "own"}))


def _keyed_store(tmp_path):
    """A key file with an approved device, a waiting one and an open pairing."""
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.net")
    _add_device(store, rc.generate_private())
    _add_device(store, rc.generate_private(), pending=True)
    _add_open_pairing(store)
    return store


def test_rotate_keeps_the_devices_and_the_bot_key(tmp_path):
    store = _keyed_store(tmp_path)
    before, devices = store.identity(), store.devices()
    out = rcl.rotate(store.path, "keep")
    after = rcl.KeyStore(store.path).identity()
    assert after.public == before.public and after.token != before.token
    assert after.install_id != before.install_id and after.client_key != before.client_key
    assert rcl.KeyStore(store.path).devices() == devices      # the waiting one still waits
    assert rcl.KeyStore(store.path).load()["pairings"] == []  # their links name the old install
    assert rcl.KeyStore(store.path).relay_url() == "https://relay.example.net"
    assert {k: out[k] for k in ("install_id", "old_install_id", "bot_key", "devices",
                                "device_count", "pairings_dropped", "install_id_changed",
                                "client_key_changed")} == {
        "install_id": after.install_id, "old_install_id": before.install_id,
        "bot_key": "kept", "devices": "kept", "device_count": 2, "pairings_dropped": 1,
        "install_id_changed": True, "client_key_changed": True}


def test_rotate_drops_the_devices_and_the_bot_key(tmp_path):
    store = _keyed_store(tmp_path)
    before = store.identity()
    out = rcl.rotate(store.path, "drop")
    fresh = rcl.KeyStore(store.path)
    after = fresh.identity()
    assert after.public != before.public and after.token != before.token
    assert fresh.devices() == [] and fresh.load()["pairings"] == []
    assert (out["bot_key"], out["devices"], out["device_count"]) == ("new", "dropped", 2)
    with pytest.raises(rcl.RelayError):
        rcl.rotate(store.path, "some")
    assert rcl.KeyStore(store.path).identity() == after  # a bad choice changes nothing


def test_rotate_keeps_a_private_backup_of_the_old_file(tmp_path):
    store = _keyed_store(tmp_path)
    old = store.path.read_bytes()
    now = time.time()
    first = Path(rcl.rotate(store.path, "keep", now=now)["backup"])
    assert first.parent == store.path.parent and first.name.startswith("relay.json.")
    assert first.read_bytes() == old and (first.stat().st_mode & 0o777) == 0o600
    assert (store.path.stat().st_mode & 0o777) == 0o600
    # A second rotation in the same second never writes over the first backup.
    second = Path(rcl.rotate(store.path, "drop", now=now)["backup"])
    assert second != first and first.read_bytes() == old
    assert (second.stat().st_mode & 0o777) == 0o600
    # A key file the bot would refuse is left as it is, with no backup.
    os.chmod(store.path, 0o644)
    kept = store.path.read_bytes()
    with pytest.raises(rcl.RelayError, match="readable"):
        rcl.rotate(store.path, "keep")
    assert store.path.read_bytes() == kept and len(list(tmp_path.glob("*.bak"))) == 2


def test_rotate_changes_the_install_id_the_relay_knows(tmp_path, capsys):
    """The install id and the client key both derive from the relay token,
    so a rotation changes the id: the relay's allow file must name the new
    one, the old client key reaches nothing, and a config that pins the old
    id leaves the relay off at the next start. The CLI says each step."""
    store = _keyed_store(tmp_path)
    before = store.identity()
    allow = tmp_path / "installs.txt"
    allow.write_text(before.install_id + "\n")
    relay = rs.Relay(allow_file=str(allow), log=lambda line: None)
    assert relay.bot_exchange(before.install_id, _bot_auth(before.token), {"wait": 0})[0] == 200
    cli = _relay_cli()
    assert cli.run(["rotate", "--key-file", str(store.path), "--devices", "keep"]) == 0
    out = capsys.readouterr()
    shown = json.loads(out.out)
    after = rcl.KeyStore(store.path).identity()
    assert shown["install_id"] == after.install_id == rs.install_id_for(after.token)
    assert shown["old_install_id"] == before.install_id
    for words in (f"--install-id {after.install_id}", "allow file", "pins install_id",
                  "next poll", shown["backup"]):
        assert words in out.err, words
    # Not activated until the allow file names the new id.
    status, err = relay.bot_exchange(after.install_id, _bot_auth(after.token), {"wait": 0})
    assert status == 403 and "activated" in err["error"]
    allow.write_text(after.install_id + "\n")
    assert relay.bot_exchange(after.install_id, _bot_auth(after.token), {"wait": 0})[0] == 200
    # The client key in the old links does not reach the new install.
    assert relay.check_key(after.install_id, before.client_key) == rs.UNAVAILABLE
    assert relay.check_key(after.install_id, after.client_key)[0] == 200
    # A config that pins the old id leaves the relay off; the new id starts it.
    cfg = {"relay": {"url": "https://relay.example.net", "key_file": str(store.path),
                     "install_id": before.install_id}}
    assert rcl.start_relay(cfg, "http://127.0.0.1:9/chat") is None
    cfg["relay"]["install_id"] = after.install_id
    client = rcl.start_relay(cfg, "http://127.0.0.1:9/chat")
    assert client is not None and client.install_id == after.install_id
    client.stop()
    # The choice is the owner's: no default, and nothing else is taken.
    for bad in ([], ["--devices", "some"]):
        with pytest.raises(SystemExit):
            cli.run(["rotate", "--key-file", str(store.path), *bad])
    capsys.readouterr()
    assert cli.run(["show", "--key-file", str(store.path)]) == 0  # the other commands pass on
    assert json.loads(capsys.readouterr().out)["install_id"] == after.install_id


def _phone_holding(base: str, store, key: bytes) -> Phone:
    """A phone with the device key `key` and what the key file says now
    (the install id, the bot's key, the client key), as an app that read
    `/api/relay/status` on the tailnet would hold it."""
    ident = store.identity()
    phone = Phone(base, rc.PairingPayload(relay_url=base, install_id=ident.install_id,
                                          bot_public=ident.public, client_key=ident.client_key,
                                          pairing_id="0" * 32, psk=bytes(32), expires=0))
    phone.key = key
    return phone


def _until(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_a_running_client_takes_the_rotated_key(tmp_path):
    """The bot polls on under the new install id after a rotation, with no
    restart: the old id goes offline at the relay, the old sessions end,
    a kept device that holds the new id and client key says a plain hello,
    and after `drop` the same device is refused."""
    relay = rs.Relay(rs.Limits(keepalive=0.3, client_timeout=1, offline_after=0.5),
                     log=lambda line: None)
    server = _serve(rs.RelayHTTPServer(("127.0.0.1", 0), relay))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url=base)
    key = rc.generate_private()
    _add_device(store, key)
    client = rcl.RelayClient(base, store, "http://127.0.0.1:9/chat", wait=1).start()
    try:
        assert _until(lambda: client.connected), client.last_error
        old = _phone_holding(base, store, key)
        assert old.hello(pairing=False)[1]["k"] == "welcome"
        assert len(client._sessions) == 1
        out = rcl.rotate(store.path, "keep")
        assert _until(lambda: client.install_id == out["install_id"] and client.connected
                      and out["install_id"] in relay.routes)
        assert client._sessions == {}
        assert client.status()["client_key"] == rcl.KeyStore(store.path).identity().client_key
        # The old install id answers nothing once its route is gone.
        assert _until(lambda: old.hello(pairing=False)[0] == 503)
        kept = _phone_holding(base, store, key)
        assert kept.hello(pairing=False)[1]["k"] == "welcome"
        ping = kept.session.seal("ping", rc.new_id(), rc.pack({}))
        assert "pong" in rc.unpack(kept.session.open(kept.post(ping)[1]))[0]
        # drop: a new bot key and no device. The phone that learns the new
        # install id is still refused.
        out = rcl.rotate(store.path, "drop")
        assert _until(lambda: client.install_id == out["install_id"] and client.connected
                      and out["install_id"] in relay.routes)
        assert _phone_holding(base, store, key).hello(pairing=False)[1]["e"] == "auth"
    finally:
        client.stop()
        server.shutdown()


def test_a_key_file_that_does_not_read_keeps_the_identity(tmp_path, caplog):
    store = rcl.KeyStore.create(tmp_path / "relay.json")
    client = rcl.RelayClient("https://relay.example.net", store, "http://127.0.0.1:9/chat")
    install = client.install_id
    good = store.path.read_bytes()
    store.path.write_text("not a key file")
    with caplog.at_level(logging.WARNING, logger="chat.relay"):
        assert client.reload() is False and client.reload() is False
    assert client.install_id == install
    assert sum("did not read" in r.getMessage() for r in caplog.records) == 1
    store.path.write_bytes(good)
    assert client.reload() is False   # the same identity: nothing to do
    rcl.rotate(store.path, "keep")
    assert client.reload() is True and client.install_id != install
    assert client._bot_url.endswith(f"/r/{client.install_id}/bot")


def test_a_relay_without_the_app_cannot_start_a_phone(web):
    """A relay installed without the app answers "not found" at its root, so
    the status says `relay_app: false` and a pairing through it is refused
    with the way out; the probe's answer is cached for RELAY_APP_TTL."""
    from botcore.chat import web_frontend as wf
    fe, base = web["fe"], web["base"]
    wf._relay_app_seen.pop(base, None)
    st, _, body = _call(fe, "GET", "/api/relay/status", headers=_owner(fe))
    assert st == 200 and body["relay_app"] is False and body["pair_relay"] is True
    st, _, body = _call(fe, "POST", "/api/relay/pair", {"via": "relay"}, headers=_owner(fe))
    assert st == 409 and "serves no copy of the app" in body["error"]
    wf._relay_app_seen[base] = (time.time(), True)
    st, _, _ = _call(fe, "POST", "/api/relay/pair", {"via": "relay"}, headers=_owner(fe))
    assert st == 200


# ── the handoff: a computer set up with a code ────────────────────────

HOSTED = rcl.DEFAULT_HOSTED_APP
R = "https://relay.example.org"


def test_a_handoff_code_is_ten_symbols_and_reads_as_typed():
    seen = {rc.new_handoff_code() for _ in range(200)}
    assert len(seen) == 200
    for code in seen:
        assert len(code) == 10 and set(code) <= set(rc.HANDOFF_ALPHABET)
        assert rc.normalize_handoff_code(code) == code
        shown = rc.format_handoff_code(code)
        assert shown == f"{code[:5]}-{code[5:]}" and rc.normalize_handoff_code(shown.lower()) == code
    assert rc.normalize_handoff_code(None) is None and rc.normalize_handoff_code(12) is None
    with pytest.raises(rc.HandoffError):
        rc.format_handoff_code("not a code")
    with pytest.raises(rc.HandoffError):
        rc.handoff_keys("short", R)
    # Both come from one slow KDF block, and production runs 600000 rounds.
    import inspect
    assert inspect.signature(rc.handoff_keys).parameters["iterations"].default == 600000
    assert inspect.signature(rc.handoff_secret).parameters["iterations"].default == 600000
    assert len(rc.handoff_secret("7K3QX9ZRMA", iterations=1000)) == 32  # one block, not two
    m1, k1 = rc.handoff_keys("7K3QX9ZRMA", R, iterations=1000)
    m2, k2 = rc.handoff_keys("7k3qx-9zrma", R + "/", iterations=1000)
    assert (m1, k1) == (m2, k2) and len(k1) == 32 and rc.MAILBOX_RE.match(m1)
    assert rc.handoff_keys("7K3QX9ZRMB", R, iterations=1000)[0] != m1
    assert rc.handoff_keys("7K3QX9ZRMA", R, iterations=1001)[0] != m1


def test_each_relay_sees_a_mailbox_of_its_own():
    """The review's finding: every relay in relays.json saw the same
    mailbox. Now the mailbox names the relay; the key is the same."""
    m1, k1 = rc.handoff_keys("7K3QX9ZRMA", R, iterations=1000)
    m2, k2 = rc.handoff_keys("7K3QX9ZRMA", "https://relay.example.net", iterations=1000)
    assert m1 != m2 and k1 == k2
    secret = rc.handoff_secret("7K3QX9ZRMA", iterations=1000)
    assert rc.handoff_mailbox(secret, R) == m1 and m1 not in (secret.hex(), k1.hex())


def _handoff_payload(**over) -> rc.PairingPayload:
    ident = rc.BotIdentity.generate()
    payload = dataclasses.replace(rc.new_pairing(ident, "https://relay.example.org"),
                                  bundle=BUNDLE, app=HOSTED)
    return dataclasses.replace(payload, **over)


def test_a_handoff_blob_opens_only_under_its_code_and_mailbox():
    payload = _handoff_payload()
    mailbox, key = rc.handoff_keys("7K3QX9ZRMA", R, iterations=1000)
    blob = rc.seal_handoff(mailbox, key, payload.to_json())
    assert len(blob) <= rc.HANDOFF_MAX and rc.open_handoff(mailbox, key, blob) == payload
    assert rc.seal_handoff(mailbox, key, payload.to_json()) != blob  # a fresh nonce each time
    other_m, other_k = rc.handoff_keys("7K3QX9ZRMB", R, iterations=1000)
    raw = bytearray(rc.b64d(blob))
    raw[-1] ^= 1
    for m, k, b in ((other_m, other_k, blob),    # another code
                    (other_m, key, blob),        # moved to another mailbox
                    (mailbox, other_k, blob),
                    (mailbox, key, rc.b64e(bytes(raw))),
                    (mailbox, key, "not base64!"), (mailbox, key, ""),
                    (mailbox, key, "A" * (rc.HANDOFF_MAX + 4))):
        with pytest.raises(rc.HandoffError):
            rc.open_handoff(m, k, b)
    # A payload without the app address and the bundle hash is no handoff.
    bare = dataclasses.replace(payload, app="", bundle="")
    with pytest.raises(rc.HandoffError):
        rc.open_handoff(mailbox, key, rc.seal_handoff(mailbox, key, bare.to_json()))
    with pytest.raises(rc.HandoffError):
        rc.seal_handoff(mailbox, key, b"x" * rc.HANDOFF_MAX)
    with pytest.raises(rc.HandoffError):
        rc.seal_handoff("ABC", key, b"{}")


# the relay's side

def _deposit(relay, token, body, source="198.51.100.7"):
    return relay.deposit_handoff(rc.install_id_for(token), _bot_auth(token), body, source)


def test_the_relay_holds_a_handoff_for_one_read():
    now = [1000.0]
    relay = rs.Relay(log=lambda line: None, clock=lambda: now[0])
    token = os.urandom(32)
    m = "a" * 32
    assert _deposit(relay, token, {"m": m, "c": "YmxvYg", "ttl": 600}) == (200, {"ok": True})
    assert relay.take_handoff(m, "203.0.113.9") == (200, {"c": "YmxvYg"})
    assert relay.take_handoff(m, "203.0.113.9") == (404, {"error": "not found"})
    # Gone at its time.
    assert _deposit(relay, token, {"m": m, "c": "Ymxvwg", "ttl": 5})[0] == 200
    now[0] += 5
    assert relay.take_handoff(m, "203.0.113.9")[0] == 404
    # A second deposit to a mailbox the install holds replaces it.
    assert _deposit(relay, token, {"m": m, "c": "b25l", "ttl": 60})[0] == 200
    assert _deposit(relay, token, {"m": m, "c": "dHdv", "ttl": 60})[0] == 200
    assert len(relay.handoffs) == 1 and relay.take_handoff(m)[1] == {"c": "dHdv"}
    # The reaper drops what is past its time.
    assert _deposit(relay, token, {"m": m, "c": "dHdv", "ttl": 1})[0] == 200
    now[0] += 2
    relay.reap()
    assert relay.handoffs == {}


def test_the_relay_refuses_a_bad_handoff_deposit(tmp_path):
    relay = rs.Relay(log=lambda line: None)
    token = os.urandom(32)
    install = rc.install_id_for(token)
    good = {"m": "b" * 32, "c": "Ymxvwg", "ttl": 600}
    assert relay.deposit_handoff(install, "", good)[0] == 401
    assert relay.deposit_handoff(install, _bot_auth(os.urandom(32)), good)[0] == 403
    for bad in ({}, [], {**good, "x": 1}, {"m": "b" * 32, "c": "Ymxvwg"},
                {**good, "m": "B" * 32}, {**good, "m": "b" * 31}, {**good, "m": 7},
                {**good, "c": ""}, {**good, "c": "not base64!"}, {**good, "c": 5},
                {**good, "ttl": 0}, {**good, "ttl": 601}, {**good, "ttl": True},
                {**good, "ttl": 1.5}, {**good, "ttl": "60"}):
        assert _deposit(relay, token, bad)[0] == 400, bad
    assert _deposit(relay, token, {**good, "c": "A" * 4097}) == (413, {"error": "handoff too large"})
    assert _deposit(relay, token, {**good, "c": "A" * 4096})[0] == 200
    # At most eight per install; a mailbox another install holds is refused.
    for i in range(1, 8):
        assert _deposit(relay, token, {**good, "m": f"{i:032x}"})[0] == 200
    assert _deposit(relay, token, {**good, "m": "c" * 32})[0] == 429
    assert _deposit(relay, os.urandom(32), good) == (409, {"error": "mailbox in use"})
    # A full relay says so.
    small = rs.Relay(rs.Limits(max_handoffs_total=1), log=lambda line: None)
    assert _deposit(small, token, good)[0] == 200
    assert _deposit(small, os.urandom(32), {**good, "m": "d" * 32})[0] == 503
    # The allow file applies as for a poll.
    allow = tmp_path / "installs.txt"
    allow.write_text("")
    gated = rs.Relay(log=lambda line: None, allow_file=str(allow))
    assert _deposit(gated, token, good)[0] == 403
    allow.write_text(install + "\n")
    assert _deposit(gated, token, good)[0] == 200


def _polling_bot(relay, source="198.51.100.7"):
    """A bot whose route is open: it polled once (wait 0). Its token."""
    token = os.urandom(32)
    assert relay.bot_exchange(rc.install_id_for(token), _bot_auth(token),
                              {"frames": [], "wait": 0}, source)[0] == 200
    return token


def _attack(relay, sources, per_source=1):
    """Deposits from installs no bot polls for, one fresh token each; how many the relay took."""
    took = 0
    for src in sources:
        for _ in range(per_source):
            body = {"m": secrets.token_hex(16), "c": "eA", "ttl": 600}
            took += _deposit(relay, os.urandom(32), body, source=src)[0] == 200
    return took


def test_one_ipv6_48_cannot_fill_the_handoff_table():
    """The review's first case: one-blob deposits from 4096 /64s of one /48
    (a free tunnel-broker block) filled the table and an honest bot got
    503. A /48 is one source now, for the held-blob cap and the new-route
    meter alike."""
    relay = rs.Relay(log=lambda line: None)
    took = _attack(relay, (f"2001:db8:42:{i:x}::1" for i in range(1, relay.limits.max_handoffs_total + 1)))
    assert took <= relay.limits.new_route_burst < relay.limits.max_handoffs_total
    assert rs.source_group("2001:db8:42:1::1") == rs.source_group("2001:db8:42:ffff:1::9")
    assert rs.source_group("2001:db8:43::1") != rs.source_group("2001:db8:42::1")
    assert rs.source_group("::ffff:192.0.2.1") == "192.0.2.1"
    # The held-blob cap counts the /48 too, even for installs whose bots poll.
    lim = rs.Limits(max_handoffs_per_source=4)
    capped = rs.Relay(lim, log=lambda line: None)
    tokens = [_polling_bot(capped, f"2001:db8:42:{i}::1") for i in range(1, 6)]
    got = [_deposit(capped, t, {"m": f"{i:032x}", "c": "eA", "ttl": 60}, f"2001:db8:42:{i + 1}::1")[0]
           for i, t in enumerate(tokens)]
    assert got == [200, 200, 200, 200, 429]
    token = _polling_bot(relay)
    for m in ("aa" * 16, "bb" * 16):
        assert _deposit(relay, token, {"m": m, "c": "eA", "ttl": 600}) == (200, {"ok": True})


def test_a_full_handoff_table_never_pushes_out_a_polling_bots_codes():
    """The review's second case: the eviction from "the largest source"
    pushed out an honest bot's oldest code at each attacker deposit. Now a
    full table makes room only for a bot that polls, and only by dropping
    a blob of an install no bot polls for."""
    lim = rs.Limits(max_handoffs_total=64, new_route_burst=10 ** 6)
    relay = rs.Relay(lim, log=lambda line: None)
    honest = _polling_bot(relay)
    for m in ("aa" * 16, "bb" * 16):
        assert _deposit(relay, honest, {"m": m, "c": "eA", "ttl": 600})[0] == 200
    assert _attack(relay, (f"192.0.2.{i}" for i in range(1, 63))) == 62
    assert len(relay.handoffs) == 64
    # The table is full: a deposit from an install no bot polls for is refused, and evicts nothing.
    assert _attack(relay, (f"203.0.113.{i}" for i in range(1, 50))) == 0
    assert {"aa" * 16, "bb" * 16} <= set(relay.handoffs) and len(relay.handoffs) == 64
    # The honest bot's next code takes the place of an attacker's oldest blob.
    oldest = [m for m, e in relay.handoffs.items() if e[0] != rc.install_id_for(honest)][0]
    assert _deposit(relay, honest, {"m": "cc" * 16, "c": "eA", "ttl": 600}) == (200, {"ok": True})
    assert {"aa" * 16, "bb" * 16, "cc" * 16} <= set(relay.handoffs) and oldest not in relay.handoffs
    # Another polling bot, from an address of its own, gets room too.
    other = _polling_bot(relay, "198.51.100.8")
    assert _deposit(relay, other, {"m": "dd" * 16, "c": "eA", "ttl": 600})[0] == 200
    # A table of polling bots' blobs alone stays full.
    small = rs.Relay(rs.Limits(max_handoffs_total=2), log=lambda line: None)
    for i, src in enumerate(("192.0.2.1", "192.0.2.2", "192.0.2.3")):
        t = _polling_bot(small, src)
        want = 200 if i < 2 else 503
        assert _deposit(small, t, {"m": f"{i:032x}", "c": "eA", "ttl": 60}, src)[0] == want


def test_deposits_from_installs_no_bot_holds_spend_the_address_meter():
    relay = rs.Relay(rs.Limits(new_route_burst=2, new_route_rate=0.0001), log=lambda line: None)
    for i in range(2):
        assert _deposit(relay, os.urandom(32), {"m": f"{i:032x}", "c": "eA", "ttl": 60})[0] == 200
    assert _deposit(relay, os.urandom(32), {"m": "e" * 32, "c": "eA", "ttl": 60})[0] == 429
    assert _deposit(relay, os.urandom(32), {"m": "e" * 32, "c": "eA", "ttl": 60},
                    source="192.0.2.1")[0] == 200


def test_mailbox_lookups_are_metered_per_address():
    now = [0.0]
    relay = rs.Relay(log=lambda line: None, clock=lambda: now[0])
    token = os.urandom(32)
    assert _deposit(relay, token, {"m": "f" * 32, "c": "eA", "ttl": 600})[0] == 200
    for _ in range(20):
        assert relay.take_handoff("0" * 32, "203.0.113.9")[0] == 404
    # Past twenty a minute, even the right mailbox waits.
    assert relay.take_handoff("f" * 32, "203.0.113.9")[0] == 429
    assert relay.take_handoff("f" * 32, "203.0.113.10")[0] == 200
    now[0] += 3.0  # one more lookup each three seconds
    assert relay.take_handoff("0" * 32, "203.0.113.9")[0] == 404
    assert relay.take_handoff("0" * 32, "203.0.113.9")[0] == 429


def test_the_handoff_routes_over_http_log_no_mailbox(loop):
    base, relay, logs = loop["base"], loop["relay"], loop["logs"]
    token = rc.b64d(loop["client"]._auth[7:])
    install = rc.install_id_for(token)
    m = "0123456789abcdef0123456789abcdef"
    url = f"{base}/r/{install}/handoff"

    def post(body, headers):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with NO_PROXY.open(req, timeout=5) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), json.loads(e.read())
    good = {"m": m, "c": "c2VhbGVk", "ttl": 60}
    assert post(good, {})[0] == 401
    status, headers, out = post(good, {"Authorization": _bot_auth(token)})
    assert (status, out) == (200, {"ok": True})
    assert "Access-Control-Allow-Origin" not in headers  # the bot's route has no CORS
    assert post({**good, "c": "A" * 9000}, {"Authorization": _bot_auth(token)})[0] == 413
    # The computer's one read: any origin, no credentials, GET and OPTIONS.
    with NO_PROXY.open(f"{base}/h/{m}", timeout=5) as r:
        assert r.status == 200 and json.loads(r.read()) == {"c": "c2VhbGVk"}
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert r.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
        assert "Access-Control-Allow-Credentials" not in r.headers
        assert r.headers["Cache-Control"] == "no-store"
    with pytest.raises(urllib.error.HTTPError) as err:
        NO_PROXY.open(f"{base}/h/{m}", timeout=5)
    assert err.value.code == 404 and err.value.headers["Access-Control-Allow-Origin"] == "*"
    err.value.close()
    req = urllib.request.Request(f"{base}/h/{m}", method="OPTIONS")
    with NO_PROXY.open(req, timeout=5) as r:
        assert r.status == 204 and r.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    req = urllib.request.Request(url, method="OPTIONS")
    with pytest.raises(urllib.error.HTTPError) as err:
        NO_PROXY.open(req, timeout=5)
    assert err.value.code == 404
    err.value.close()
    with pytest.raises(urllib.error.HTTPError) as err:
        NO_PROXY.open(f"{base}/r/{install}/handoff", timeout=5)  # a GET of the bot's route
    assert err.value.code == 405
    err.value.close()
    assert not [line for line in logs if m in line], "a log line names the mailbox"
    assert any(" GET h - 200 " in line for line in logs)
    assert any(" GET h - 404 " in line for line in logs)
    assert any(f" POST handoff {install} 200 " in line for line in logs)
    assert m not in json.dumps(relay.counts())


def test_the_nginx_snippet_has_the_handoff_routes():
    conf = (ROOT / "relay" / "snippets" / "nginx.conf").read_text()
    http = (ROOT / "relay" / "snippets" / "nginx-http.conf").read_text()
    look = conf.split('location ~ "^/relay/h/[0-9a-f]{32}$" {', 1)[1].split("    }\n", 1)[0]
    assert "limit_except GET { deny all; }" in look
    assert "limit_req zone=tacitus_relay_handoff" in look and "client_max_body_size" not in look
    dep = conf.split('location ~ "^/relay/r/[a-z2-7]{26}/handoff$" {', 1)[1].split("    }\n", 1)[0]
    assert 'if ($http_authorization = "") { return 401; }' in dep
    assert "limit_except POST { deny all; }" in dep and "client_max_body_size 16m" not in dep
    assert "limit_req_zone $binary_remote_addr zone=tacitus_relay_handoff:" in http
    assert "rate=20r/m" in http


# the bot's side

def test_the_hosted_app_setting_is_an_https_origin():
    assert rcl.check_hosted_app("") == "" and rcl.check_hosted_app(None) == ""
    assert rcl.check_hosted_app("https://Tacitus-AI.github.io/") == "https://tacitus-ai.github.io/"
    assert rcl.check_hosted_app("https://app.example.org:443/") == "https://app.example.org/"
    assert rcl.check_hosted_app("https://app.example.org:8443/") == "https://app.example.org:8443/"
    for bad in ("http://app.example.org/", "https://app.example.org", "https://app.example.org/x/",
                "https://app.example.org/?relay=x", "https://app.example.org/#x",
                "https://user@app.example.org/", "https://app.example.org\\@evil.example/",
                "https:///", "javascript:alert(1)", "https://app.example.org/ "):
        with pytest.raises(rcl.RelayError):
            rcl.check_hosted_app(bad)


def test_a_hosted_device_is_trusted_like_a_relay_copy(tmp_path):
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.org")
    client = rcl.RelayClient("https://relay.example.org", store, "http://127.0.0.1:9/chat",
                             app_url=OWN_URL + "/", public_url=OWN_URL, hosted_app=HOSTED)
    assert client.origin_for(HOSTED) == "hosted"
    assert client.origin_for(HOSTED + "index.html") == "hosted"
    assert client.origin_for(OWN_URL + "/") == "own"
    assert client.origin_for("https://relay.example.org/") == "relay"
    assert client.origin_for("https://elsewhere.example.org/") == "relay"
    # A hosted pairing needs the bundle hash and opens hosted_app itself.
    with pytest.raises(rcl.RelayError):
        client.new_pairing(app_url=HOSTED)
    with pytest.raises(rcl.RelayError):
        client.new_pairing(app_url=HOSTED + "other/", bundle=BUNDLE)
    with pytest.raises(rcl.RelayError):  # any other foreign origin stays refused
        client.new_pairing(app_url="https://elsewhere.example.org/", bundle=BUNDLE)
    p = rc.PairingPayload.from_string(client.new_pairing(app_url=HOSTED, bundle=BUNDLE))
    assert (p.app, p.bundle) == (HOSTED, BUNDLE)
    dev = store.register(p.pairing_id, rc.public_of(rc.generate_private()), "laptop", code="123456")
    assert dev["origin"] == "hosted" and dev["bundle"] == BUNDLE and "handoff" not in dev
    assert client.level(dev) == "chat"
    assert client.level({**dev, "cards_allowed": True}) == "all"
    assert client.level({"origin": "hosted", "cards_allowed": True}) == "off"  # no hash, no entry
    row = rcl.device_row(dev)
    assert (row["origin"], row["cards_allowed"], row["bundle_verified"]) == ("hosted", False, False)
    # A hosted_app on the bot's or the relay's origin, or not https, turns the handoff off.
    for clash in ("https://host.example.test/", "https://relay.example.org/", "http://x.example/"):
        c = rcl.RelayClient("https://relay.example.org", store, "http://127.0.0.1:9/chat",
                            public_url=OWN_URL, hosted_app=clash)
        assert c.hosted_app == ""


def test_the_relay_client_marks_owner_routes_only_from_the_session(tmp_path):
    store = rcl.KeyStore.create(tmp_path / "relay.json")
    client = rcl.RelayClient("https://relay.example.org", store, "http://127.0.0.1:9/chat")
    phone_says = {"X-Tacitus-Relay-Device": "0" * 16, "x-tacitus-relay": "1" * 16,
                  "X-Tacitus-Relay-Cards": "allow", "Cookie": "ea_web=x", "accept": "*/*"}
    h = client._headers("a" * 16, phone_says, "chat", "POST")
    assert rcl.OWNER_DEVICE_HEADER not in h and h["X-Tacitus-Relay"] == "a" * 16
    assert h["X-Tacitus-Relay-Cards"] == "deny" and "Cookie" not in h and h["accept"] == "*/*"
    h = client._headers("a" * 16, phone_says, "all", "GET", owner_device="a" * 16)
    assert h[rcl.OWNER_DEVICE_HEADER] == "a" * 16
    from botcore.chat import web_frontend as wf
    assert wf.OWNER_DEVICE_HEADER == rcl.OWNER_DEVICE_HEADER
    assert {(m, "/api/" + "/".join(s)) for m, s in wf.RELAYED_OWNER_ROUTES} == rcl.RELAYED_OWNER_ROUTES
    for method, path in rcl.RELAYED_OWNER_ROUTES:
        assert rcl.relayed_owner_route(method, path)
        assert not rcl.relayed_owner_route(method, path + "?x=1")
        assert not rcl.relayed_owner_route(method, path.upper())
        assert not rcl.path_allowed(path)  # never through the ordinary rule
    assert not rcl.relayed_owner_route("POST", "/api/relay/pair")
    assert not rcl.relayed_owner_route(["GET"], {"p": 1})  # a phone's odd JSON, no error


def test_a_phones_own_device_mark_never_reaches_the_web_chat(loop):
    """In the loopback run: a relay-origin phone's copy of the mark is
    dropped, and its owner routes stay refused before the web chat sees
    them; an own-origin phone's owner route carries the session's id."""
    client, phone = loop["client"], loop["phone"]
    FakeAPI.seen.clear()
    _, out = phone.call("GET", "/api/me", headers={"X-Tacitus-Relay-Device": phone.session.device,
                                                   "accept": "application/json"})
    assert out["s"] == 200
    sent = FakeAPI.seen[-1][2]
    assert "x-tacitus-relay-device" not in {k.lower() for k in sent}
    for method, path in sorted(rcl.RELAYED_OWNER_ROUTES):
        _, out = phone.call(method, path, b"{}", headers={
            "X-Tacitus-Relay-Device": phone.session.device, "content-type": "application/json"})
        assert out["s"] == 403, path
    assert not [p for _m, p, _h in FakeAPI.seen if "/api/relay/" in p]
    own = Phone(loop["base"], rc.PairingPayload.from_string(client.new_pairing()))
    assert own.hello()[1]["k"] == "welcome"
    # Waiting for approval: nothing, the owner routes too.
    assert own.call("GET", "/api/relay/devices")[1]["s"] == 403
    assert not [p for _m, p, _h in FakeAPI.seen if "/api/relay/" in p]
    assert client.approve(own.session.device)
    FakeAPI.seen.clear()
    own.call("GET", "/api/relay/devices", headers={"X-Tacitus-Relay-Device": "f" * 16})
    (method, path, sent), = [s for s in FakeAPI.seen if "/api/relay/" in s[1]]
    marks = {k.lower(): v for k, v in sent.items()}
    assert (method, path) == ("GET", "/chat/api/relay/devices")
    assert marks["x-tacitus-relay-device"] == own.session.device == marks["x-tacitus-relay"]
    FakeAPI.seen.clear()
    own.call("GET", "/api/me")
    assert "x-tacitus-relay-device" not in {k.lower() for k in FakeAPI.seen[-1][2]}
    assert client.revoke(own.session.device)


def _computer(web, code: str) -> "Phone":
    """The computer: derives the mailbox and key from the code (the real
    600000 rounds), takes the blob from the relay once, opens it and says
    the pairing hello. Returns the computer, waiting for approval."""
    mailbox, key = rc.handoff_keys(code, web["base"])
    with NO_PROXY.open(f"{web['base']}/h/{mailbox}", timeout=5) as r:
        blob = json.loads(r.read())["c"]
    payload = rc.open_handoff(mailbox, key, blob)
    assert payload.app == HOSTED and payload.bundle == web["fe"].bundle_hash()
    assert payload.relay_url == web["base"]
    computer = Phone(web["base"], payload)
    status, welcome = computer.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
    return computer


def _relay_post(phone, path, body):
    out = phone.call("POST", path, json.dumps(body).encode())[1]
    return out["s"], json.loads(out["body"] or b"null")


def test_a_phone_hands_off_to_a_computer_through_the_relay(web):
    """The whole path: an approved own-origin phone asks for a code through
    the relay; the computer takes the blob once, pairs and waits; the
    phone lists it and approves it through the relay; the computer chats,
    with no card buttons. The owner hears of both steps, never the code."""
    f, relay = web["fe"], web["relay"]
    f.registry.event("message")(lambda m: None)
    st = _call(f, "GET", "/api/relay/status", headers=_owner(f))[2]
    assert (st["handoff"], st["hosted_app"]) == (True, HOSTED)
    phone = _phone(web, _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"])
    me = json.loads(phone.call("GET", "/api/me")[1]["body"])
    assert (me["owner_routes"], me["handoff"]) == ("handoff", True)
    status, out = _relay_post(phone, "/api/relay/handoff", {})
    assert status == 200, out
    assert set(out) == {"code", "expires", "pairing", "open", "access"} and out["access"] == "chat"
    assert len(out["code"]) == 11 and out["code"][5] == "-"
    assert out["open"] == HOSTED + "?relay=" + urllib.parse.quote(web["base"], safe="")
    assert abs(out["expires"] - (time.time() + rc.PAIRING_TTL)) < 5
    (pairing,) = [p for p in f.relay.store.load()["pairings"] if p.get("handoff")]
    assert (pairing["origin"], pairing["by"], pairing["bundle"]) == \
        ("hosted", phone.session.device, f.bundle_hash())
    # The relay holds one blob under the code's mailbox, and nothing that reads.
    mailbox = rc.handoff_keys(out["code"], web["base"])[0]
    assert list(relay.handoffs) == [mailbox]
    held = json.dumps(relay.snapshot())
    assert out["code"] not in held and out["code"].replace("-", "") not in held
    assert pairing["psk"] not in held
    computer = _computer(web, out["code"])
    # One read only.
    with pytest.raises(urllib.error.HTTPError) as err:
        NO_PROXY.open(f"{web['base']}/h/{mailbox}", timeout=5)
    assert err.value.code == 404
    err.value.close()
    cid = computer.session.device
    assert computer.call("GET", "/api/me")[1]["s"] == 403  # waits
    rows = json.loads(phone.call("GET", "/api/relay/devices")[1]["body"])
    mine = {r["id"]: r for r in rows}
    assert mine[phone.session.device]["this_device"] is True
    assert mine[cid] == {"id": cid, "name": "loopback phone", "paired_at": mine[cid]["paired_at"],
                         "origin": "hosted", "pending": True, "code": computer.code,
                         "handoff": True, "by": phone.session.device, "pairing": out["pairing"],
                         "cards_allowed": False, "bundle_verified": False}
    assert _relay_post(phone, "/api/relay/approve", {"device": cid}) == (200, {"ok": True})
    me = json.loads(computer.call("GET", "/api/me")[1]["body"])
    assert me["card_actions"] is False and (me["owner_routes"], me["handoff"]) == ("none", False)
    text = json.dumps({"text": "from the computer"}).encode()
    assert computer.call("POST", "/api/channels/dm/messages", text)[1]["s"] == 200
    click = json.dumps({"action_id": "approve_send"}).encode()
    assert computer.call("POST", "/api/cards/dm/w1/actions", click)[1]["s"] == 403
    # The computer attests its copy like a relay copy.
    assert _attest(computer, f.bundle_hash())["s"] == 200
    assert {d["id"]: d for d in f.relay.devices()}[cid]["bundle_verified"] is True
    # The owner's DM: the code was asked for, then the computer approved
    # by name and six-digit code; the handoff code itself never.
    dm = [m["text"] for m in f.store.threads("dm", 50, None)]
    assert any("made a code to add a computer" in t and phone.session.device in t for t in dm), dm
    assert any("approved the computer" in t and cid in t
               and f"{computer.code[:3]} {computer.code[3:]}" in t for t in dm), dm
    assert not [t for t in dm if out["code"] in t or out["code"].replace("-", "") in t]
    # The phone allows card buttons on its computer when asked to.
    assert _relay_post(phone, "/api/relay/approve", {"device": cid, "cards": True})[0] == 200
    assert json.loads(computer.call("GET", "/api/me")[1]["body"])["card_actions"] is True


def test_the_relayed_owner_routes_refuse_each_other_case(web):
    """Each refusal of the relayed owner rule, through the relay: a hosted
    device and a relay-origin device reach none of the four routes; an
    own-origin phone approves and removes only a computer its own handoff
    set up: never itself, another phone, or another's computer."""
    f = web["fe"]

    def own_link():
        return _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"]
    phone = _phone(web, own_link())
    other = _phone(web, own_link())
    # A computer that the owner's handoff set up (by "owner"), and one the
    # other phone's handoff set up.
    status, _h, by_owner = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
    assert status == 200
    owners = _computer(web, by_owner["code"])
    assert {d["id"]: d for d in f.relay.devices()}[owners.session.device]["by"] == "owner"
    status, by_other = _relay_post(other, "/api/relay/handoff", {})
    assert status == 200
    others = _computer(web, by_other["code"])
    for target in (owners, others, other):
        status, out = _relay_post(phone, "/api/relay/approve", {"device": target.session.device})
        assert status == 403 and not JARGON.search(out["error"]), out
    assert _relay_post(phone, "/api/relay/approve", {"device": "0" * 16})[0] == 404
    assert _relay_post(phone, "/api/relay/approve", {"device": "nope"})[0] == 400
    assert all(d["pending"] for d in f.relay.devices()
               if d["id"] in (owners.session.device, others.session.device))
    # The owner's own web chat approves any of them.
    assert _call(f, "POST", "/api/relay/approve", {"device": owners.session.device},
                 _owner(f))[0] == 200
    # Removing: only a computer this phone added. Not itself, not the other
    # phone, not a computer the owner or the other phone added.
    for target in (phone, other, owners, others):
        status, out = _relay_post(phone, "/api/relay/revoke", {"device": target.session.device})
        assert status == 403 and not JARGON.search(out["error"]), out
    assert _relay_post(phone, "/api/relay/revoke", {"device": "0" * 16})[0] == 404
    assert phone.call("GET", "/api/me")[1]["s"] == 200
    assert other.session.device in {d["id"] for d in f.relay.devices()}
    # The hosted computer, once approved, and a relay-origin phone reach none.
    f.relay.relay_origin_access = "chat"
    relay_phone = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/", relay_origin=True))
    for device in (owners, relay_phone):
        assert json.loads(device.call("GET", "/api/me")[1]["body"])["owner_routes"] == "none"
        for method, path in sorted(rcl.RELAYED_OWNER_ROUTES):
            _, out = device.call(method, path, json.dumps({"device": other.session.device}).encode())
            assert out["s"] == 403, (path, out)
    # Even with card buttons allowed.
    assert f.relay.approve(owners.session.device, cards=True)
    _, out = owners.call("POST", "/api/relay/revoke",
                         json.dumps({"device": other.session.device}).encode())
    assert out["s"] == 403
    assert other.session.device in {d["id"] for d in f.relay.devices()}
    status, mine = _relay_post(phone, "/api/relay/handoff", {})
    assert status == 200
    added = _computer(web, mine["code"])
    assert _relay_post(phone, "/api/relay/revoke", {"device": added.session.device}) == \
        (200, {"ok": True})
    assert added.call("GET", "/api/me")[1]["k"] == "err"


def test_a_phone_through_the_relay_makes_few_handoff_codes(web):
    """A stolen phone cannot mint codes freely: two open at a time, ten a
    day, each refusal in plain words; the owner's own web chat has no
    such limit."""
    f = web["fe"]
    phone = _phone(web, _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"])
    for _ in range(rcl.HANDOFF_OPEN_PER_DEVICE):
        assert _relay_post(phone, "/api/relay/handoff", {})[0] == 200
    status, out = _relay_post(phone, "/api/relay/handoff", {})
    assert status == 429 and "two codes open" in out["error"] and not JARGON.search(out["error"])
    assert _call(f, "POST", "/api/relay/handoff", {}, _owner(f))[0] == 200  # the owner's own chat
    # Spent or ended codes free a place, up to ten a day.
    for _ in range(rcl.HANDOFF_DAY_PER_DEVICE - rcl.HANDOFF_OPEN_PER_DEVICE):
        for p in [p for p in f.relay.store.load()["pairings"] if p.get("by") == phone.session.device]:
            f.relay.store.drop_pairing(p["id"])
        web["relay"].handoffs.clear()  # the relay's own cap per install is not under test
        assert _relay_post(phone, "/api/relay/handoff", {})[0] == 200
    for p in [p for p in f.relay.store.load()["pairings"] if p.get("by") == phone.session.device]:
        f.relay.store.drop_pairing(p["id"])
    web["relay"].handoffs.clear()
    status, out = _relay_post(phone, "/api/relay/handoff", {})
    assert status == 429 and "ten codes today" in out["error"] and not JARGON.search(out["error"])
    log = f.relay.store.load()["handoff_log"][phone.session.device]
    assert len(log) == rcl.HANDOFF_DAY_PER_DEVICE
    # A day later the count starts again.
    f.relay.store._update(lambda d: d["handoff_log"].update(
        {phone.session.device: [t - 86400 for t in log]}))
    assert _relay_post(phone, "/api/relay/handoff", {})[0] == 200


def test_the_web_chat_checks_the_relayed_owner_mark(web):
    """The second lock, in the web chat: the mark must name the device the
    request came through, and that device must be own-origin and approved."""
    f = web["fe"]
    phone = _phone(web, _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"])
    waiting = _phone(web, _call(f, "POST", "/api/relay/pair", {}, _owner(f))[2]["link"],
                     approve=False)
    f.relay.relay_origin_access = "chat"
    relay_phone = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/", relay_origin=True))
    pid, wid, rid = phone.session.device, waiting.session.device, relay_phone.session.device
    good = {**_owner(f), "X-Tacitus-Relay": pid, "X-Tacitus-Relay-Device": pid}
    assert _call(f, "GET", "/api/relay/devices", headers=good)[0] == 200
    for marks in ({"X-Tacitus-Relay": pid},                                   # no mark
                  {"X-Tacitus-Relay-Device": pid},                            # mark alone
                  {"X-Tacitus-Relay": pid, "X-Tacitus-Relay-Device": wid},    # another device
                  {"X-Tacitus-Relay": wid, "X-Tacitus-Relay-Device": wid},    # waiting
                  {"X-Tacitus-Relay": rid, "X-Tacitus-Relay-Device": rid},    # relay origin
                  {"X-Tacitus-Relay": "0" * 16, "X-Tacitus-Relay-Device": "0" * 16},  # unknown
                  {"X-Tacitus-Relay": "x", "X-Tacitus-Relay-Device": "x"}):
        headers = {**_owner(f), **marks}
        assert _call(f, "GET", "/api/relay/devices", headers=headers)[0] == 403, marks
        assert _call(f, "POST", "/api/relay/handoff", {}, headers)[0] == 403, marks
    # A good mark opens only the four routes.
    for method, path in (("GET", "/api/relay/status"), ("POST", "/api/relay/pair"),
                         ("POST", "/api/pair")):
        assert _call(f, method, path, {} if method == "POST" else None, good)[0] == 403, path
    # It stands in for no credential: without the cookie, 401.
    assert _call(f, "GET", "/api/relay/devices",
                 headers={k: v for k, v in good.items() if k != "Cookie"})[0] == 401
    me = _call(f, "GET", "/api/me", headers={**_owner(f), "X-Tacitus-Relay": rid})[2]
    assert me["owner_routes"] == "none"
    me = _call(f, "GET", "/api/me", headers={**_owner(f), "X-Tacitus-Relay": pid})[2]
    assert me["owner_routes"] == "handoff"
    assert _call(f, "GET", "/api/me", headers=_owner(f))[2]["owner_routes"] == "all"


def test_the_handoff_route_says_why_it_cannot_work(web, monkeypatch):
    from botcore.chat.web_frontend import WebFrontend
    f = web["fe"]
    before = len(f.relay.store.load()["pairings"])
    # The hosted copy lists this relay: the address needs no ?relay=.
    rcl._hosted_relays_seen[HOSTED] = (time.time(), frozenset({web["base"]}))
    status, _h, out = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
    assert status == 200 and out["open"] == HOSTED
    rcl._hosted_relays_seen[HOSTED] = (time.time(), frozenset())
    # The relay does not take the blob: 503, and the pairing goes again.
    n = len(f.relay.store.load()["pairings"])

    def refuse(*a, **k):
        raise rcl.HandoffUnavailable("the relay answered 503")
    monkeypatch.setattr(rcl, "deposit_handoff", refuse)
    status, _h, out = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
    assert status == 503 and not JARGON.search(out["error"]), out
    assert len(f.relay.store.load()["pairings"]) == n
    monkeypatch.undo()
    # No bundle hash, or the handoff turned off: 409 in plain words.
    with mock.patch.object(WebFrontend, "bundle_hash", lambda self: ""):
        status, _h, out = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
        assert status == 409 and not JARGON.search(out["error"]), out
        st = _call(f, "GET", "/api/relay/status", headers=_owner(f))[2]
        assert st["handoff"] is False
    f.relay.hosted_app = ""
    status, _h, out = _call(f, "POST", "/api/relay/handoff", {}, _owner(f))
    assert status == 409 and not JARGON.search(out["error"]), out
    st = _call(f, "GET", "/api/relay/status", headers=_owner(f))[2]
    assert (st["handoff"], st["hosted_app"]) == (False, "")
    assert _call(f, "GET", "/api/me", headers=_owner(f))[2]["handoff"] is False
    assert len(f.relay.store.load()["pairings"]) == before + 1
    assert _call(f, "POST", "/api/relay/handoff", {})[0] == 401


def test_a_handoff_counts_toward_the_open_pairings(tmp_path):
    """The pairing behind a code is ordinary: ten minutes, one of eight."""
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.org")
    deposited = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout=None):
            deposited.append(json.loads(req.data))
            return Answer()
    for _ in range(10):
        out = rcl.make_handoff(store, Opener(), "https://relay.example.org", HOSTED, BUNDLE)
    pairings = store.load()["pairings"]
    assert len(pairings) == rcl.MAX_PAIRINGS and pairings[-1]["id"] == out["pairing"]
    assert all(p["handoff"] and p["by"] == "owner" and p["origin"] == "hosted" for p in pairings)
    assert all(d["ttl"] in (rc.PAIRING_TTL - 1, rc.PAIRING_TTL) for d in deposited)
    assert len({d["m"] for d in deposited}) == 10
    with pytest.raises(rcl.RelayError):
        rcl.make_handoff(store, Opener(), "https://relay.example.org", "", BUNDLE)
    with pytest.raises(rcl.RelayError):
        rcl.make_handoff(store, Opener(), "https://relay.example.org", HOSTED, "")


def test_the_cli_makes_a_handoff_code(tmp_path, capsys, monkeypatch, loop):
    monkeypatch.setattr(rcl, "_cli_bundle_hash", lambda path, url: BUNDLE)
    key = str(tmp_path / "relay.json")
    base = loop["base"]
    assert rcl.main(["init", "--key-file", key, "--relay-url", base]) == 0
    capsys.readouterr()
    rcl._hosted_relays_seen[HOSTED] = (time.time(), frozenset())
    try:
        assert rcl.main(["handoff", "--key-file", key]) == 0
    finally:
        rcl._hosted_relays_seen.pop(HOSTED, None)
    out = capsys.readouterr()
    lines = dict(line.split(": ", 1) for line in out.out.strip().splitlines())
    assert rc.normalize_handoff_code(lines["Code"]) and lines["Code"][5] == "-"
    assert lines["Open"] == HOSTED + "?relay=" + urllib.parse.quote(base, safe="")
    assert "Never send it through chat or mail" in out.err
    mailbox, k = rc.handoff_keys(lines["Code"], base)
    status, got = loop["relay"].take_handoff(mailbox)
    assert status == 200
    payload = rc.open_handoff(mailbox, k, got["c"])
    assert (payload.app, payload.bundle, payload.relay_url) == (HOSTED, BUNDLE, base)
    (p,) = rcl.KeyStore(key).load()["pairings"]
    assert (p["handoff"], p["by"], p["origin"]) == (True, "owner", "hosted")
    assert rcl.main(["handoff", "--key-file", key, "--hosted-app", ""]) == 1
    monkeypatch.setattr(rcl, "_cli_bundle_hash", lambda path, url: "")
    assert rcl.main(["handoff", "--key-file", key]) == 1
    assert "signs no app bundle" in capsys.readouterr().err
