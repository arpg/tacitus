"""The web chat's hardening (docs/SECURITY.md, Web chat): who may call the
API (Host, Sec-Fetch-Site, Origin), who the owner is (the login header only
from tailscaled's own connection), what the bot serves at all (a trusted
chat block, private files outside the resident's tree), how bodies and
pairing are bounded, the push subscriptions' life, and where the owner's
web messages are known from."""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(ROOT / "tests"))

import chat as chat_pkg  # noqa: E402
from chat import ChatError  # noqa: E402
from chat import web_frontend as wf  # noqa: E402
from chat.cards import CardAction, ConfirmationCard  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.shadow_frontend import WEB_PREFIX, ShadowFrontend, _to_mrkdwn  # noqa: E402
from chat.types import InboundMessage, MessageRef  # noqa: E402
from chat.web_frontend import WebFrontend  # noqa: E402
from test_web_frontend import (  # noqa: E402
    LOGIN, OWNER_H, PUBLIC_HOST, PUBLIC_ORIGIN, PUBLIC_URL, call, make, owner)

wp = wf.web_push


@pytest.fixture
def fe(tmp_path):
    f = make(tmp_path)
    f.start()
    yield f
    f.stop()


def raw(f: WebFrontend, head: str, body: bytes = b"") -> tuple[int, bytes]:
    """Send a request as bytes on a fresh loopback connection; (status,
    the whole answer)."""
    with socket.create_connection(("127.0.0.1", f.port), timeout=5) as s:
        s.sendall(head.replace("\n", "\r\n").encode() + b"\r\n" + body)
        got = b""
        while True:
            try:
                chunk = s.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            got += chunk
    line = got.split(b"\r\n", 1)[0].split()
    return (int(line[1]) if len(line) > 1 else 0), got


def _post_head(f: WebFrontend, path: str, extra: str = "") -> str:
    return (f"POST {f.base_path}{path} HTTP/1.1\nHost: {PUBLIC_HOST}\n"
            f"Tailscale-User-Login: {LOGIN}\nOrigin: {PUBLIC_ORIGIN}\n"
            f"Content-Type: application/json\n{extra}")


# S0-1: Host, Sec-Fetch-Site, Origin, CORP


def test_a_wrong_host_is_misdirected_before_auth(fe):
    for host in ("evil.example.test", "evil.example.test:8444", "host.example.test",
                 "host.example.test:443", "host.example.test:8445", f"127.0.0.1:{fe.port + 1}",
                 "127.0.0.1", f"10.0.0.1:{fe.port}", "", "user@host.example.test:8444",
                 f"localhost.evil.example.test:{fe.port}"):
        status, _h, body = owner(fe, "GET", "/api/me", headers={"Host": host})
        assert status == 421, host
        assert json.loads(body)["error"]
    for host in (PUBLIC_HOST, "HOST.example.test:8444", f"127.0.0.1:{fe.port}",
                 f"localhost:{fe.port}"):
        assert owner(fe, "GET", "/api/me", headers={"Host": host})[0] == 200, host
    # before auth: a stranger with a wrong Host learns nothing more than 421
    assert call(fe, "GET", "/api/me", headers={"Host": "evil.example.test"})[0] == 421
    # the static app is not behind the check
    assert call(fe, "GET", "/", headers={"Host": "evil.example.test"})[0] == 200


def test_a_default_port_host_matches_a_portless_public_url(tmp_path):
    f = make(tmp_path, public_url="https://host.example.test/chat")
    f.start()
    try:
        assert owner(f, "GET", "/api/me", headers={"Host": "host.example.test"})[0] == 200
        assert owner(f, "GET", "/api/me", headers={"Host": "host.example.test:443"})[0] == 200
        assert owner(f, "GET", "/api/me", headers={"Host": "host.example.test:8444"})[0] == 421
    finally:
        f.stop()


def test_another_site_is_refused_on_get_too(fe):
    for site in ("cross-site", "same-site", "Same-Site"):
        status, _h, _b = owner(fe, "GET", "/api/channels", headers={"Sec-Fetch-Site": site})
        assert status == 403, site
        assert owner(fe, "POST", "/api/channels/main/messages", {"text": "x"},
                     {"Sec-Fetch-Site": site})[0] == 403
    for site in ("same-origin", "none"):
        assert owner(fe, "GET", "/api/channels", headers={"Sec-Fetch-Site": site})[0] == 200


def test_a_post_needs_the_exact_public_origin(fe):
    path = "/api/channels/main/messages"
    assert owner(fe, "POST", path, {"text": "x"}, {"Origin": None})[0] == 403
    assert owner(fe, "POST", path, {"text": "x"}, {"Origin": PUBLIC_ORIGIN + "/"})[0] == 200
    assert owner(fe, "POST", path, {"text": "x"}, {"Origin": "https://host.example.test"})[0] == 403
    # a loopback origin is the relay client's, and only without the login header
    assert owner(fe, "POST", path, {"text": "x"},
                 {"Origin": f"http://127.0.0.1:{fe.port}"})[0] == 403
    token = fe.pair_token()
    cookie = {"Cookie": f"ea_web={token}"}
    for origin in (f"http://127.0.0.1:{fe.port}", f"http://localhost:{fe.port}"):
        assert call(fe, "POST", path, {"text": "x"}, {**cookie, "Origin": origin})[0] == 200
    for origin in (f"http://127.0.0.1:{fe.port + 1}", f"https://127.0.0.1:{fe.port}",
                   "http://127.0.0.1", None):
        assert call(fe, "POST", path, {"text": "x"}, {**cookie, "Origin": origin})[0] == 403
    # pairing too
    assert call(fe, "POST", "/api/pair", {"token": token}, {"Origin": None})[0] == 403
    assert call(fe, "POST", "/api/pair", {"token": token},
                {"Origin": "https://evil.example.test"})[0] == 403


def test_every_answer_is_same_origin_only(fe):
    for status, headers, _b in (owner(fe, "GET", "/api/me"), call(fe, "GET", "/api/me"),
                                call(fe, "GET", "/"), owner(fe, "GET", "/api/me",
                                                            headers={"Host": "x.example"})):
        assert headers["Cross-Origin-Resource-Policy"] == "same-origin", status
    assert "form-action 'none'" in wf.HTML_CSP


# S0-2: the login header counts only on tailscaled's own connection


def test_the_header_is_ignored_from_a_peer_that_is_not_tailscaled(tmp_path, monkeypatch):
    f = make(tmp_path, tailscaled=False)
    if not wf.PROC_NET[0].exists():
        # no /proc (macOS): stand in for the socket table
        monkeypatch.setattr(wf, "peer_uid", lambda peer, local: os.geteuid())
    f.start()
    try:
        me = os.geteuid()
        monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: me + 1)
        assert owner(f, "GET", "/api/me")[0] == 401
        monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: None)
        assert owner(f, "GET", "/api/me")[0] == 401
        monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: me)
        assert owner(f, "GET", "/api/me")[0] == 200
        # a stale uid is looked up again once on a mismatch
        seen = []

        def stale_then_fresh(refresh=False):
            seen.append(refresh)
            return me if refresh else me + 1
        monkeypatch.setattr(wf, "tailscaled_uid", stale_then_fresh)
        assert owner(f, "GET", "/api/me")[0] == 200 and seen == [False, True]
        # off the configured login, or with the header path turned off, no
        monkeypatch.setattr(wf, "tailscaled_uid", lambda refresh=False: me)
        assert owner(f, "GET", "/api/me", headers={"Tailscale-User-Login": "x@example.test"})[0] \
            == 401
        f.trust_tailscale_header = False
        assert owner(f, "GET", "/api/me")[0] == 401
        assert call(f, "GET", "/api/me", headers={"Cookie": f"ea_web={f.pair_token()}"})[0] == 200
    finally:
        f.stop()


def test_trust_tailscale_header_reads_from_the_block(tmp_path):
    for val, want in ((None, True), (True, True), (False, False), ("false", False),
                      ("off", False), (0, False), ("yes", True)):
        cfg = {} if val is None else {"trust_tailscale_header": val}
        assert WebFrontend(cfg, store_path=tmp_path / "s").trust_tailscale_header is want, val


def test_peer_uid_reads_the_client_socket_of_a_live_connection():
    if not wf.PROC_NET[0].exists():
        pytest.skip("no /proc/net/tcp here: the header path is off")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    conn, peer = srv.accept()
    try:
        assert wf.peer_uid(peer, conn.getsockname()) == os.geteuid()
        assert wf.peer_uid(peer, ("127.0.0.1", 1)) is None          # not this connection
        assert wf.peer_uid(("127.0.0.1", 1), conn.getsockname()) is None
        assert wf.peer_uid(("not an ip", 1), conn.getsockname()) is None
    finally:
        for s in (cli, conn, srv):
            s.close()


def test_peer_uid_skips_dead_and_ambiguous_entries(tmp_path, monkeypatch):
    head = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   "
            "uid  timeout inode\n")

    def line(local, remote, st, uid, inode="77"):
        return (f"   0: {local} {remote} {st} 00000000:00000000 00:00000000 00000000 "
                f"{uid:>5}        0 {inode} 1 0000000000000000 100 0 0 10 0\n")
    v4, v6 = tmp_path / "tcp", tmp_path / "tcp6"
    monkeypatch.setattr(wf, "PROC_NET", (v4, v6))
    peer, local = ("127.0.0.1", 50000), ("127.0.0.1", 8470)
    a, b = "0100007F:C350", "0100007F:2116"
    v4.write_text(head + line(a, b, "06", 0, "0") + line(a, b, "01", 1234))
    v6.write_text(head)
    assert wf.peer_uid(peer, local) == 1234                      # TIME_WAIT skipped
    v4.write_text(head + line(a, b, "01", 1234) + line(a, b, "01", 99))
    assert wf.peer_uid(peer, local) is None                      # two owners: none
    # an IPv4 client of a dual-stack server: the v4 table holds its socket
    v4.write_text(head + line(a, b, "01", 4321))
    assert wf.peer_uid(("::ffff:127.0.0.1", 50000), ("::ffff:127.0.0.1", 8470)) == 4321
    one = "00000000000000000000000001000000"
    v6.write_text(head + line(f"{one}:C350", f"{one}:2116", "01", 555))
    assert wf.peer_uid(("::1", 50000), ("::1", 8470)) == 555


def test_tailscaled_uid_is_kept_and_looked_up_again_on_request(monkeypatch):
    calls = []
    monkeypatch.setattr(wf, "_lookup_tailscaled_uid", lambda: calls.append(1) or 0)
    monkeypatch.setitem(wf._ts_cache, "at", 0.0)
    monkeypatch.setattr(wf, "TAILSCALED_RECHECK_S", 0.0)
    assert wf.tailscaled_uid() == 0 and wf.tailscaled_uid() == 0 and len(calls) == 1
    assert wf.tailscaled_uid(refresh=True) == 0 and len(calls) == 2
    monkeypatch.setattr(wf, "TAILSCALED_RECHECK_S", 3600.0)
    assert wf.tailscaled_uid(refresh=True) == 0 and len(calls) == 2   # rate-limited


# S1: what the bot serves at all


def test_a_block_from_ea_config_never_serves(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="chat.web")
    standalone = WebFrontend({"port": 0}, store_path=tmp_path / "a" / "webchat.sqlite")
    with pytest.raises(ChatError, match="owner-private chat.json"):
        standalone.start()
    assert standalone._server is None and not (tmp_path / "a").exists()
    # under a composite: logged, the shadow switched off, the primary alone
    primary = MemoryFrontend()
    web = WebFrontend({"port": 0}, store_path=tmp_path / "b" / "webchat.sqlite")
    fe = ShadowFrontend(primary, web, backfill_days=7)
    fe.start()
    try:
        assert web.refused and fe.shadow_off == web.refused and web._server is None
        ref = fe.post("C_MAIN", "still posted")
        assert ref == primary.posts[-1]["ref"]
        assert fe.backfill()["copied"] == 0 and fe._backfill_thread is None
        assert not (tmp_path / "b").exists()                  # nothing written there
        assert "not serving" in caplog.text and "runs on memory alone" in caplog.text
    finally:
        fe.stop()


def test_load_frontend_trusts_only_the_private_chat_json(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_pkg.owner_trust, "is_owner_process", lambda: True)
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    (priv / "chat.json").write_text(json.dumps({"frontend": "web", "web": {"port": 0}}))
    (priv / "chat.json").chmod(0o600)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    f = chat_pkg.load_frontend(env={})
    assert f.trusted_config is True and f.store_path == priv / "webchat.sqlite"
    f.start()
    try:
        assert f._server is not None and f.refused is None
        assert (priv / wf.PAIR_FILE).stat().st_mode & 0o777 == 0o600
    finally:
        f.stop()
    (tmp_path / "ea" / "config").mkdir(parents=True)
    (tmp_path / "ea" / "config" / "ea-config.json").write_text(
        json.dumps({"chat": {"frontend": "web", "web": {"port": 0}}}))
    assert chat_pkg.load_frontend(ea_dir=tmp_path / "ea", env={}).trusted_config is False


def test_the_store_and_token_must_be_private_and_outside_the_resident_tree(tmp_path):
    def try_start(f):
        try:
            f.start()
        except ChatError:
            pass
        finally:
            f.stop()
        return f.refused or ""

    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    f = WebFrontend({"port": 0}, store_path=open_dir / "webchat.sqlite", trusted_config=True)
    assert "must be 0700" in try_start(f)
    assert list(open_dir.iterdir()) == []                   # no store made there

    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    tok = priv / "webchat-pair.token"
    tok.write_text("x" * 43 + "\n")
    tok.chmod(0o644)
    f = WebFrontend({"port": 0}, store_path=priv / "webchat.sqlite", trusted_config=True)
    assert "must be 0600" in try_start(f)
    tok.chmod(0o600)
    os.link(tok, tmp_path / "second-name")
    assert "2 links" in try_start(WebFrontend({"port": 0}, store_path=priv / "webchat.sqlite",
                                              trusted_config=True))
    (tmp_path / "second-name").unlink()

    # a token file in a directory others may write
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    (shared / "t").write_text("y" * 43)
    (shared / "t").chmod(0o600)
    f = WebFrontend({"port": 0, "pair_token_file": str(shared / "t")},
                    store_path=priv / "webchat.sqlite", trusted_config=True)
    assert "writable by group or others" in try_start(f)
    shared.chmod(0o700)

    # inside the resident's tree, private or not: refused before anything
    # is written there
    tree = tmp_path / "resident-home"
    (tree / "state").mkdir(parents=True, mode=0o700)
    f = WebFrontend({"port": 0}, store_path=tree / "state" / "webchat.sqlite",
                    trusted_config=True, untrusted_roots=(tree,))
    assert "inside" in try_start(f)
    assert list((tree / "state").iterdir()) == []
    f = WebFrontend({"port": 0}, store_path=tree / "new" / "webchat.sqlite",
                    trusted_config=True, untrusted_roots=(tree,))
    assert "inside" in try_start(f) and not (tree / "new").exists()
    f = WebFrontend({"port": 0, "pair_token_file": str(tree / "state" / "t")},
                    store_path=priv / "webchat.sqlite", trusted_config=True,
                    untrusted_roots=(tree,))
    (tree / "state" / "t").write_text("z" * 43)
    (tree / "state" / "t").chmod(0o600)
    assert "inside" in try_start(f)

    # all in order: it serves
    f = WebFrontend({"port": 0}, store_path=priv / "webchat.sqlite", trusted_config=True,
                    untrusted_roots=(tree,))
    try:
        f.start()
        assert f.refused is None and f._server is not None
    finally:
        f.stop()


def test_the_untrusted_roots_name_the_ea_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    (tmp_path / "ea").mkdir()
    roots = chat_pkg.untrusted_roots()
    assert (tmp_path / "ea").resolve() in roots
    assert tmp_path.resolve() not in roots          # this user's own: not a resident's home


def test_a_writer_only_side_still_writes_an_untrusted_store(tmp_path):
    w = WebFrontend({}, store_path=tmp_path / "w" / "webchat.sqlite", writer_only=True)
    w.start()
    try:
        assert w.refused is None
        assert w.mirror_post(MessageRef("C", "1", "1", 1.0), "x")["text"] == "x"
    finally:
        w.stop()


# S2: bodies, connections, pairing


def test_a_chunk_size_must_be_plain_hex(fe):
    for size in ("-1", "+5", "0x5", " -0", "1_0", "5 5", "fffffffff"):
        status, _got = raw(fe, _post_head(fe, "/api/channels/main/messages",
                                          "Transfer-Encoding: chunked\n"),
                           f"{size}\r\nhello\r\n0\r\n\r\n".encode())
        assert status == 400, size
    body = b'{"text": "in a chunk"}'
    status, got = raw(fe, _post_head(fe, "/api/channels/main/messages",
                                     "Transfer-Encoding: chunked\n"),
                      f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
    assert status == 200 and b"in a chunk" in got


def test_a_chunk_past_the_limit_is_refused_before_it_is_read(fe):
    t0 = time.monotonic()
    status, got = raw(fe, _post_head(fe, "/api/channels/main/messages",
                                     "Transfer-Encoding: chunked\n"),
                      f"{wf.MAX_JSON_BYTES + 1:x}\r\n".encode())
    assert status == 400 and b"too large" in got and time.monotonic() - t0 < 5
    status, _got = raw(fe, _post_head(fe, "/api/channels/main/messages",
                                      "Transfer-Encoding: gzip, chunked\n"), b"0\r\n\r\n")
    assert status == 400
    status, _got = raw(fe, _post_head(fe, "/api/channels/main/messages",
                                      "Content-Length: +2\n"), b"{}")
    assert status == 400


def test_pairing_reads_only_its_small_body(fe):
    head = (f"POST {fe.base_path}/api/pair HTTP/1.1\nHost: 127.0.0.1:{fe.port}\n"
            f"Origin: http://127.0.0.1:{fe.port}\nContent-Type: application/json\n")
    big = json.dumps({"token": "x" * (wf.MAX_PAIR_BYTES + 10)}).encode()
    status, got = raw(fe, head + f"Content-Length: {len(big)}\n", big)
    assert status == 400 and b"too large" in got
    status, _got = raw(fe, head + "Transfer-Encoding: chunked\n",
                       f"{wf.MAX_PAIR_BYTES + 1:x}\r\n".encode())
    assert status == 400


def test_a_short_pairing_token_is_refused(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="chat.web")
    f = make(tmp_path)
    (tmp_path / "priv").mkdir(mode=0o700)
    tok = tmp_path / "priv" / wf.PAIR_FILE
    tok.write_text("short-token\n")
    tok.chmod(0o600)
    f.start()
    try:
        assert f.pair_token() == "" and f.pair_hash() == ""
        assert call(f, "POST", "/api/pair", {"token": "short-token"})[0] == 401
        assert call(f, "GET", "/api/me", headers={"Cookie": "ea_web=short-token"})[0] == 401
        assert caplog.text.count("shorter than 16") == 1        # said once per token
        tok.write_text("a" * 16)
        assert call(f, "POST", "/api/pair", {"token": "a" * 16})[0] == 200
    finally:
        f.stop()


def test_pairing_attempts_take_turns(fe, monkeypatch):
    monkeypatch.setattr(wf, "PAIR_WAIT_S", 0.05)
    assert fe._pair_lock.acquire()
    try:
        assert call(fe, "POST", "/api/pair", {"token": "nope"})[0] == 429
    finally:
        fe._pair_lock.release()
    monkeypatch.setattr(wf, "PAIR_WAIT_S", 10.0)
    fe.pair_delay_s = 0.3
    out: list[int] = []
    threads = [threading.Thread(target=lambda: out.append(
        call(fe, "POST", "/api/pair", {"token": "wrong"})[0])) for _ in range(3)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert out == [401, 401, 401] and time.monotonic() - t0 >= 0.85   # one pause at a time


def test_the_server_caps_open_connections_and_times_out(tmp_path):
    assert wf._Handler.timeout == 30
    f = make(tmp_path)
    f.start()
    try:
        srv = f._server
        srv._slots = threading.BoundedSemaphore(1)
        idle = socket.create_connection(("127.0.0.1", f.port), timeout=5)
        time.sleep(0.2)                                     # holds the one slot
        status, _got = raw(f, f"GET {f.base_path}/ HTTP/1.1\nHost: 127.0.0.1:{f.port}\n")
        assert status == 0                                  # closed at once, unanswered
        idle.close()
        end = time.time() + 5
        while time.time() < end:
            if raw(f, f"GET {f.base_path}/ HTTP/1.1\nHost: 127.0.0.1:{f.port}\n")[0] == 200:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the slot never came back")
    finally:
        f.stop()


def test_one_peer_address_cannot_hold_every_slot(tmp_path):
    """An address other than loopback holds at most MAX_PEER_CONNECTIONS
    of the MAX_CONNECTIONS slots; one more from it is closed at once,
    while loopback (the relay client, tailscale serve) and other addresses
    are served, and a closed connection gives its slot back."""
    assert wf.MAX_PEER_CONNECTIONS == 8 < wf.MAX_CONNECTIONS == 64
    f = make(tmp_path)
    f.start()
    held = []
    try:
        srv = f._server
        assert srv.max_per_peer == 8

        def connect(peer):
            ours, theirs = socket.socketpair()
            srv.process_request(theirs, (peer, 40000))
            held.append(ours)
            return ours

        for _ in range(8):
            connect("203.0.113.5")
        assert srv._peers == {"203.0.113.5": 8}
        extra = connect("203.0.113.5")
        extra.settimeout(2)
        assert extra.recv(1) == b"", "the ninth is closed at once"
        assert srv._peers == {"203.0.113.5": 8}
        connect("198.51.100.7")
        assert srv._peers["198.51.100.7"] == 1, "another address has its own"
        status, _ = raw(f, f"GET {f.base_path}/ HTTP/1.1\nHost: 127.0.0.1:{f.port}\n")
        assert status == 200, "loopback is not counted"
        held[0].close()
        end = time.time() + 5
        while time.time() < end and srv._peers.get("203.0.113.5") != 7:
            time.sleep(0.05)
        assert srv._peers["203.0.113.5"] == 7, "a closed connection gives its slot back"
    finally:
        for c in held:
            c.close()
        f.stop()


def test_the_per_peer_cap_is_a_setting():
    assert wf._peer_cap(None) == wf.MAX_PEER_CONNECTIONS
    assert wf._peer_cap(0) == 0 and wf._peer_cap("16") == 16 and wf._peer_cap(64) == 64
    for junk in (-1, 65, "x", True, 2.5, "-3"):
        assert wf._peer_cap(junk) == wf.MAX_PEER_CONNECTIONS, junk
    fe = WebFrontend.from_config({"peer_connections": 0}, store_path=Path("/nonexistent/x.sqlite"))
    assert fe.peer_connections == 0
    srv = wf._Server(("127.0.0.1", 0), wf._Handler, max_per_peer=0)
    try:
        assert srv._peer(("203.0.113.5", 1)) is None, "0 lifts the cap"
    finally:
        srv.server_close()


# S2: push subscriptions


FCM = "https://fcm.googleapis.com/fcm/send/made-up"


def _keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    point = key.public_key().public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": wp.b64u(point), "auth": wp.b64u(os.urandom(16))}


@pytest.fixture
def pushing(fe):
    if not wp.AVAILABLE:
        pytest.skip("web push needs the cryptography package")
    fe.push.send_one = lambda sub, payload, urgency="normal": "sent"
    return fe


def test_subscriptions_are_dropped_when_the_pairing_token_changes(pushing):
    fe = pushing
    assert owner(fe, "POST", "/api/push/subscribe", {"endpoint": FCM, "keys": _keys()})[0] == 200
    assert fe.notify("x", tag="a", wait=True)["sent"] == 1          # the same token: kept
    assert fe.store.push_count() == 1
    fe.pair_token_file.write_text("n" * 43 + "\n")                  # the owner replaces it
    assert fe.notify("x", tag="b", wait=True) == {"sent": 0, "failed": 0, "dropped": 0,
                                                  "held": 0}
    assert fe.store.push_count() == 0
    # one made under the new token stays
    assert owner(fe, "POST", "/api/push/subscribe", {"endpoint": FCM, "keys": _keys()})[0] == 200
    assert fe.notify("x", tag="c", wait=True)["sent"] == 1


def test_a_subscription_the_app_stopped_posting_is_dropped(pushing):
    fe = pushing
    fe.store.push_subscribe(FCM, _keys())
    fe.store.push_subscribe(FCM + "-2", _keys())
    with fe.store._lock, fe.store._conn:
        fe.store._conn.execute("UPDATE push_subscriptions SET last_seen=? WHERE endpoint=?",
                               (time.time() - 31 * 86400, FCM))
    assert fe.push.prune() == 1
    assert [r["endpoint"] for r in fe.store.push_subscriptions()] == [FCM + "-2"]
    fe.store.push_subscribe(FCM + "-2", _keys())                    # posted again: renewed
    assert fe.store.push_subscriptions()[0]["last_seen"] >= time.time() - 5


def test_the_subscription_list_and_revoke_routes(pushing):
    fe = pushing
    owner(fe, "POST", "/api/push/subscribe", {"endpoint": FCM, "keys": _keys()})
    status, _h, body = owner(fe, "GET", "/api/push/subscriptions")
    rows = json.loads(body)
    assert status == 200 and [r["host"] for r in rows] == ["fcm.googleapis.com"]
    assert "made-up" not in body.decode()                    # never the endpoint's path
    sid = rows[0]["id"]
    assert call(fe, "GET", "/api/push/subscriptions")[0] == 401
    assert owner(fe, "POST", "/api/push/revoke", {"id": sid},
                 {"X-Tacitus-Relay-Cards": "deny"})[0] == 403
    assert owner(fe, "POST", "/api/push/revoke", {})[0] == 400
    status, _h, body = owner(fe, "POST", "/api/push/revoke", {"id": sid})
    assert status == 200 and json.loads(body) == {"ok": True, "removed": True}
    assert json.loads(owner(fe, "POST", "/api/push/revoke", {"id": sid})[2])["removed"] is False
    assert fe.store.push_count() == 0


def test_a_store_from_before_takes_the_last_seen_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE push_subscriptions(endpoint TEXT PRIMARY KEY, keys TEXT, "
                       "created_at REAL, last_ok REAL, failures INT DEFAULT 0);"
                       f"INSERT INTO push_subscriptions VALUES ('{FCM}', '{{}}', 1.0, NULL, 0);")
    conn.commit()
    conn.close()
    store = wf.WebStore(path)
    assert store.push_list()[0]["last_seen"] == 1.0
    assert store.push_check_token("h1") == 0 and store.push_count() == 1   # recorded, kept
    assert store.push_check_token("") == 0 and store.push_count() == 1     # unreadable: no change
    assert store.push_check_token("h2") == 1 and store.push_count() == 0


def test_the_push_payload_is_padded_to_a_block():
    if not wp.AVAILABLE:
        pytest.skip("web push needs the cryptography package")
    keys = _keys()
    for n in (0, 1, 254, 255, 256, 3000):
        body = wp.encrypt(b"x" * n, wp.unb64u(keys["p256dh"]), wp.unb64u(keys["auth"]),
                          pad_to=wp.PAD_BLOCK)
        assert (len(body) - 86 - 16) % wp.PAD_BLOCK == 0, n
    plain = wp.encrypt(b"x" * 10, wp.unb64u(keys["p256dh"]), wp.unb64u(keys["auth"]))
    assert len(plain) - 86 - 16 == 11                                 # unpadded by default


def test_push_hosts_are_exact():
    assert all(not h.startswith(".") for h in wp.PUSH_HOSTS)
    for url in ("https://anything.googleapis.com/x", "https://www.google.com/x",
                "https://jmt17.google.com.evil.example/x", "https://a.web.push.apple.com/x"):
        assert not wp.endpoint_allowed(url), url


# S3: cards from the relay, card pushes, the owner's web ids


def test_the_card_route_refuses_a_relay_device_below_all(fe):
    ref = fe.post_card("dm", ConfirmationCard(kind="send", title="Send?", actions=[
        CardAction("approve_send", "Send", {"token": "t"}, "primary", "send")]))
    path = f"/api/cards/dm/{ref.id}/actions"
    hit = []
    fe.registry.action("approve_send")(hit.append)
    assert owner(fe, "POST", path, {"action_id": "approve_send"},
                 {"X-Tacitus-Relay": "d1", "X-Tacitus-Relay-Cards": "deny"})[0] == 403
    assert owner(fe, "POST", path, {"action_id": "approve_send"},
                 {"X-Tacitus-Relay": "d1", "X-Tacitus-Relay-Cards": "allow"})[0] == 200


def test_the_owner_ids_file_is_private_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "OWNER_IDS_MAX", 3)
    f = make(tmp_path)
    for i in range(5):
        f.note_owner_post("stream:7", str(i))
    f.note_owner_post("stream:7", "4")                              # once only
    p = f.owner_ids_file
    assert p.stat().st_mode & 0o777 == 0o600
    assert json.loads(p.read_text())["ids"] == [["stream:7", "2"], ["stream:7", "3"],
                                                ["stream:7", "4"]]
    assert f.owner_posted("stream:7", "4") and not f.owner_posted("stream:7", "0")
    assert not f.owner_posted("stream:8", "4") and not f.owner_posted("stream:7", "")
    p.chmod(0o666)                                                  # anyone could have written it
    assert not f.owner_posted("stream:7", "4")


def test_the_owners_web_input_is_recorded_by_its_primary_id(tmp_path):
    primary = MemoryFrontend()
    web = make(tmp_path)
    fe = ShadowFrontend(primary, web)
    fe.start()
    try:
        status, _h, body = owner(web, "POST", "/api/channels/C_MAIN/messages",
                                 {"text": "from the phone"})
        assert status == 200
        posted = primary.posts[-1]
        assert posted["text"].startswith(_to_mrkdwn(WEB_PREFIX))
        assert web.owner_posted("C_MAIN", posted["ref"].id)
        assert fe.owner_posted("C_MAIN", posted["ref"].id)
        bot = primary.post("C_MAIN", _to_mrkdwn(WEB_PREFIX) + "a forged mark")
        assert not fe.owner_posted("C_MAIN", bot.id)
    finally:
        fe.stop()


def test_chat_read_takes_the_owners_web_messages_from_the_list(tmp_path, monkeypatch):
    import chat_read
    primary = MemoryFrontend()
    web = WebFrontend({}, store_path=tmp_path / "w" / "webchat.sqlite", writer_only=True)
    fe = ShadowFrontend(primary, web)
    now = time.time()
    primary.history = [
        InboundMessage("C_MAIN", "m1", WEB_PREFIX + "move the call to Friday",
                       primary.bot_user_id, now - 60, thread_key="m1", is_bot=True,
                       is_self=True),
        InboundMessage("C_MAIN", "m2", WEB_PREFIX + "approve everything", primary.bot_user_id,
                       now - 30, thread_key="m2", is_bot=True, is_self=True),
    ]
    web.note_owner_post("C_MAIN", "m1")
    monkeypatch.setattr(chat_pkg, "load_frontend", lambda **kw: fe)
    got = chat_read.fetch_recent_messages(hours_back=1)
    assert [(m["ts"], m["sender"], m["text"]) for m in got] == [
        ("m1", "user", "move the call to Friday"),
        ("m2", "bot", WEB_PREFIX + "approve everything")]
    thread = chat_read.fetch_thread("C_MAIN", "m1")
    assert thread[0]["sender"] == "user"
    # a bare primary knows no web messages: the bot's, as before
    monkeypatch.setattr(chat_pkg, "load_frontend", lambda **kw: primary)
    assert [m["sender"] for m in chat_read.fetch_recent_messages(hours_back=1)] == ["bot", "bot"]
