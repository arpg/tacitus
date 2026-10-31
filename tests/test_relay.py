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
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "botcore"))
sys.path.insert(0, str(ROOT / "relay"))

pytest.importorskip("cryptography")

import relay_server as rs  # noqa: E402
from botcore.chat import relay_client as rcl  # noqa: E402
from botcore.chat import relay_crypto as rc  # noqa: E402

PAIR_TOKEN = "pair-token-for-tests"
OWN_URL = "https://host.example.test/chat"
REPLY_MARKER = "relay-reply-marker-7f3a2b"
EVENT_MARKER = "relay-event-marker-91c2d4"
REQUEST_MARKER = "relay-request-marker-55e1aa"
FILE_MARKER = b"relay-file-marker-0c9e33"
MARKERS = [REPLY_MARKER.encode(), EVENT_MARKER.encode(), REQUEST_MARKER.encode(), FILE_MARKER]
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
          "/api/files/f1", "/api/cards/C1/w12/actions"]
    bad = ["/api/pair", "/api/pair/", "/api/relay/pair", "/api/relay", "/index.html",
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
    for spelling in ("/api//cards/C1/w1/actions", "/api/%63ards/C1/w1/actions",
                     "/api/CARDS/C1/w1/actions", "/api/cards", "/api/cards?x=1"):
        assert not rcl.access_allows("chat", "POST", spelling), spelling



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
    dev = store.register(p.pairing_id, pub, "phone <script>")
    assert dev["name"] == "phone script" and store.device(pub)["id"] == dev["id"]
    with pytest.raises(rcl.RelayError):
        store.register(p.pairing_id, pub)
    late = store.add_pairing(ttl=1, now=time.time() - 10)
    assert store.pairing_psk(late.pairing_id) is None
    assert store.revoke(dev["id"]) and store.device(pub) is None
    os.chmod(store.path, 0o644)
    with pytest.raises(rcl.RelayError, match="readable"):
        rcl.KeyStore(store.path).load()


def test_owner_cli(tmp_path, capsys):
    key = str(tmp_path / "relay.json")
    assert rcl.main(["init", "--key-file", key, "--relay-url", "https://relay.example.net"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert made["relay"] == {"url": "https://relay.example.net",
                             "install_id": made["install_id"], "key_file": key}
    assert rcl.main(["init", "--key-file", key]) == 1  # never replaces an identity
    capsys.readouterr()
    assert rcl.main(["pair", "--key-file", key, "--public-url", "https://host.example.ts.net/chat"]) == 0
    link = capsys.readouterr().out.strip()
    p = rc.PairingPayload.from_string(link)
    assert link.startswith("https://host.example.ts.net/chat/#tr1=")
    assert p.install_id == made["install_id"] and p.relay_url == "https://relay.example.net"
    assert rcl.main(["pair", "--key-file", key, "--app-url", "https://host.example.ts.net/chat/"]) == 0
    out = capsys.readouterr()
    assert "relay-origin" in out.err  # an app url alone is not the bot's own origin
    pairings = rcl.KeyStore(key).load()["pairings"]
    assert [x["origin"] for x in pairings] == ["own", "relay"]
    assert rcl.main(["show", "--key-file", key]) == 0
    assert json.loads(capsys.readouterr().out)["open_pairings"] == 2
    assert rcl.main(["revoke", "--key-file", key, "--device", "0" * 16]) == 1


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
               "relay": {"url": base, "key_file": str(d / "relay-key.json"), "wait": 1}}
    client = rcl.start_relay(web_cfg, f"http://127.0.0.1:{api.server_address[1]}/chat")
    assert client is not None
    deadline = time.time() + 10
    while not client.connected and time.time() < deadline:
        time.sleep(0.05)
    assert client.connected, client.last_error
    phone = Phone(base, rc.PairingPayload.from_string(client.new_pairing(app_url=base + "/")))
    status, welcome = phone.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
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
    assert own.hello()[0] == 200
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
        assert r.headers["X-Content-Type-Options"] == "nosniff"
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
                  "frame": hello, "h": hexs(pending.h)},
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
    assert rc.PairingPayload.from_string(v["pairing"]["link"]).psk == psk


# ── the web front end's side: start, routes, policy ───────────────────

LOGIN = "owner@example.test"
OWNER_H = {"Tailscale-User-Login": LOGIN}


def _web(tmp_path, relay_cfg=None, **cfg):
    from botcore.chat.web_frontend import WebFrontend
    base = {"port": 0, "tailscale_login": LOGIN, "public_url": OWN_URL}
    if relay_cfg is not None:
        base["relay"] = relay_cfg
    base.update(cfg)
    f = WebFrontend(base, store_path=tmp_path / "priv" / "webchat.sqlite",
                    webui_dir=tmp_path / "webui")
    f.poll_s = f.pair_delay_s = 0.02
    return f


def _call(f, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
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
    f = _web(tmp_path, {"url": base, "wait": 1})
    f.start()
    deadline = time.time() + 10
    while f.relay is not None and not f.relay.connected and time.time() < deadline:
        time.sleep(0.05)
    assert f.relay is not None and f.relay.connected, f.relay and f.relay.last_error
    yield {"fe": f, "base": base, "dir": tmp_path}
    f.stop()
    server.shutdown()


def _phone(web, link):
    phone = Phone(web["base"], rc.PairingPayload.from_string(link))
    status, welcome = phone.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
    return phone


def test_the_web_front_end_runs_the_relay_client(web):
    f = web["fe"]
    client = f.relay
    assert isinstance(client, rcl.RelayClient) and client.local == f.loopback_url()
    # The key file defaults to the store's (owner-private) directory.
    assert (web["dir"] / "priv" / "webchat-relay.json").stat().st_mode & 0o077 == 0
    status, _h, me = _call(f, "GET", "/api/me", headers=OWNER_H)
    assert status == 200 and me["relay"] is True and me["card_actions"] is True
    f.stop()
    assert f.relay is None and client._stop.is_set()


def test_the_relay_routes_pair_list_and_revoke(web):
    f = web["fe"]
    status, _h, out = _call(f, "POST", "/api/relay/pair", {}, OWNER_H)
    assert status == 200 and out["qr"] == out["link"] and out["origin"] == "own"
    assert out["link"].startswith(OWN_URL + "/#tr1=")
    assert abs(out["expires"] - (time.time() + rc.PAIRING_TTL)) < 5
    assert _call(f, "GET", "/api/relay/devices", headers=OWNER_H)[2] == []
    phone = _phone(web, out["link"])
    devices = _call(f, "GET", "/api/relay/devices", headers=OWNER_H)[2]
    assert devices == [{"id": phone.session.device, "name": "loopback phone",
                        "paired_at": devices[0]["paired_at"], "origin": "own"}]
    status, _h, st = _call(f, "GET", "/api/relay/status", headers=OWNER_H)
    assert st["enabled"] and st["connected"] and st["devices"] == 1
    assert st["install_id"] == phone.payload.install_id
    assert _call(f, "POST", "/api/relay/revoke", {"device": "nope"}, OWNER_H)[0] == 400
    assert _call(f, "POST", "/api/relay/revoke", {"device": "0" * 16}, OWNER_H)[0] == 404
    assert _call(f, "POST", "/api/relay/revoke", {"device": phone.session.device},
                 OWNER_H)[:3:2] == (200, {"ok": True})
    assert _call(f, "GET", "/api/relay/devices", headers=OWNER_H)[2] == []
    _, out = phone.call("GET", "/api/me")
    assert out["k"] == "err" and out["e"] in ("session", "auth")
    # The routes are the owner's.
    assert _call(f, "POST", "/api/relay/pair", {})[0] == 401
    assert _call(f, "GET", "/api/relay/devices")[0] == 401


def test_relayed_requests_never_reach_the_pairing_routes(web):
    f = web["fe"]
    marks = {**OWNER_H, "X-Tacitus-Relay": "0123456789abcdef", "X-Tacitus-Relay-Cards": "allow"}
    for method, path in (("POST", "/api/relay/pair"), ("GET", "/api/relay/devices"),
                         ("GET", "/api/relay/status"), ("POST", "/api/relay/revoke"),
                         ("POST", "/api/pair"), ("POST", "/api/%72elay/pair")):
        assert _call(f, method, path, {} if method == "POST" else None, marks)[0] == 403, path
    own = _phone(web, _call(f, "POST", "/api/relay/pair", {}, OWNER_H)[2]["link"])
    before = len(f.relay.store.load()["pairings"])
    for method, path in (("POST", "/api/relay/pair"), ("POST", "/api/%72elay/pair"),
                         ("GET", "/api/relay/devices"), ("GET", "/api/Relay/status"),
                         ("POST", "/api/p%61ir"), ("POST", "/api/relay/revoke")):
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
    phone = _phone(web, f.relay.new_pairing(app_url=web["base"] + "/"))
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


def test_the_page_policy_names_the_relay_origin(tmp_path):
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<!doctype html><title>t</title>")
    cases = [
        (None, "connect-src 'self';"),
        ({"url": "https://Relay.Example.net:8443/x"},
         "connect-src 'self' https://relay.example.net:8443;"),
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
        assert _call(f, "GET", "/api/relay/status", headers=OWNER_H)[2] == {"enabled": False}
        assert _call(f, "POST", "/api/relay/pair", {}, OWNER_H)[0] == 404
        assert _call(f, "GET", "/api/me", headers=OWNER_H)[2]["relay"] is False
    finally:
        f.stop()


def test_a_relay_that_cannot_start_leaves_the_chat_running(tmp_path):
    f = _web(tmp_path, {"url": "http://relay.example.net", "key_file": str(tmp_path / "k.json")})
    f.start()
    try:
        assert f.relay is None  # plain http off loopback: refused, logged
        assert _call(f, "GET", "/api/me", headers=OWNER_H)[0] == 200
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
