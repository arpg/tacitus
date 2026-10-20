#!/usr/bin/env python3
"""Tests for fetch_url.py's address policy.

fetch_url is the EA's only outbound-network primitive and it is handed
URLs that arrived in untrusted email, so these tests pin the guard:
scheme, private/loopback/link-local/metadata addresses on both IP
families, redirect hops, DNS pinning, and the audit log.

No test contacts a real internal service. The redirect cases use a
local http.server test double whose loopback address is whitelisted for
that one test only; the address it redirects to is classified for real.

Usage:
    python3 tests/test_fetch_url_ssrf.py
"""

import json
import os
import socket
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# EA_DIR must be a throwaway BEFORE the import: fetch_url resolves its
# log path at import time, and no test may write production state.
_TMP_EA = tempfile.mkdtemp(prefix="ea-test-fetchurl-")
os.environ["EA_DIR"] = _TMP_EA

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "preprocessor"))

import requests  # noqa: E402
import fetch_url as fu  # noqa: E402


def _blocked(url):
    """The BlockedURL message for `url`, or None when it passes."""
    try:
        fu.validate_url(url)
        return None
    except fu.BlockedURL as e:
        return str(e)


def _fake_dns(mapping):
    """Patch socket.getaddrinfo with a fixed host -> [ip] table."""
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        if host in mapping:
            out = []
            for ip in mapping[host]:
                if ":" in ip:
                    out.append((socket.AF_INET6, socket.SOCK_STREAM,
                                socket.IPPROTO_TCP, "", (ip, port, 0, 0)))
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM,
                                socket.IPPROTO_TCP, "", (ip, port)))
            return out
        raise socket.gaierror(-2, "Name or service not known")

    socket.getaddrinfo = fake
    return real


class _Double(BaseHTTPRequestHandler):
    """Stands in for an internal HTTP service. Records every hit."""

    hits = []
    redirect_to = ""

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"SECRET internal page"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Server:
    def __enter__(self):
        _Double.hits = []
        self.srv = HTTPServer(("127.0.0.1", 0), _Double)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def _allow_loopback():
    """Treat 127.0.0.1 as public for one test, so a real redirect walk can
    be exercised end to end. Everything else is classified for real."""
    real = fu._classify_address

    def patched(ip):
        return None if str(ip) == "127.0.0.1" else real(ip)

    fu._classify_address = patched
    return real


# ── Scheme ──────────────────────────────────────────────────────────────

def test_only_http_schemes(d):
    for url in ("file:///etc/passwd", "ftp://example.com/x",
                "javascript:alert(1)", "gopher://example.com/",
                "data:text/html,<b>hi", "example.com/no-scheme"):
        msg = _blocked(url)
        assert msg and "not allowed" in msg, f"{url!r} was not blocked: {msg}"
    assert _blocked("http:///nohost") is not None


# ── Literal addresses ───────────────────────────────────────────────────

def test_private_ipv4_literals_blocked(d):
    cases = {
        "http://127.0.0.1:8377/": "loopback",
        "http://127.1/": "loopback",
        "http://10.0.0.5/x": "private",
        "http://192.168.1.1/": "private",
        "http://172.16.4.4/": "private",
        "http://169.254.169.254/latest/meta-data/": "cloud metadata",
        "http://100.101.102.103:8377/": "non-routable",   # tailnet CGNAT
        "http://0.0.0.0/": "wildcard",
        "http://[::ffff:10.0.0.1]/": "private",
        "http://evil.example@127.0.0.1/": "loopback",    # userinfo trick
    }
    for url, want in cases.items():
        msg = _blocked(url)
        assert msg, f"{url!r} was NOT blocked"
        assert want in msg, f"{url!r}: {msg!r} lacks {want!r}"


def test_ipv6_literals_blocked(d):
    cases = {
        "http://[::1]/": "loopback",
        "http://[::1]:8377/x": "loopback",
        "http://[fe80::1]/": "link-local",
        "http://[fc00::1]/": "private",
        "http://[::]/": "wildcard",
        "http://[::ffff:127.0.0.1]/": "loopback",        # IPv4-mapped
        "http://[2002:7f00:1::1]/": "loopback",          # 6to4-wrapped
    }
    for url, want in cases.items():
        msg = _blocked(url)
        assert msg, f"{url!r} was NOT blocked"
        assert want in msg, f"{url!r}: {msg!r} lacks {want!r}"


def test_public_literal_allowed(d):
    info = fu.validate_url("https://93.184.216.34/page")
    assert info["ips"] == ["93.184.216.34"]
    assert info["port"] == 443
    assert fu.validate_url("http://[2606:4700::1111]/")["ips"] == \
        ["2606:4700::1111"]


# ── Resolved names ──────────────────────────────────────────────────────

def test_hostname_resolving_private_blocked(d):
    real = _fake_dns({"internal.example": ["10.1.2.3"],
                      "metadata.example": ["169.254.169.254"],
                      "mixed.example": ["93.184.216.34", "127.0.0.1"],
                      "good.example": ["93.184.216.34"]})
    try:
        assert "10.1.2.3" in _blocked("http://internal.example/admin")
        assert "cloud metadata" in _blocked("https://metadata.example/")
        # One bad answer among good ones still blocks: a rotating record
        # must not get through on luck of the draw.
        assert "127.0.0.1" in _blocked("http://mixed.example/")
        assert _blocked("https://good.example/page") is None
        # DNS failure is an error, not a policy block
        try:
            fu.validate_url("https://nx.example/")
            raise AssertionError("expected ConnectionError")
        except requests.ConnectionError as e:
            assert "DNS resolution failed" in str(e)
    finally:
        socket.getaddrinfo = real


def test_pinned_dns_scopes_and_restores(d):
    real = socket.getaddrinfo
    info = {"host": "pinned.example",
            "addrinfo": [(socket.AF_INET, socket.SOCK_STREAM,
                          socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]}
    with fu.pinned_dns(info["host"], info["addrinfo"]):
        got = socket.getaddrinfo("pinned.example", 443)
        assert got[0][4][0] == "93.184.216.34"
        assert socket.getaddrinfo is not real
    assert socket.getaddrinfo is real, "getaddrinfo not restored"
    # restored even when the body raises
    try:
        with fu.pinned_dns("x", []):
            raise ValueError("boom")
    except ValueError:
        pass
    assert socket.getaddrinfo is real


# ── Redirects ───────────────────────────────────────────────────────────

def test_redirect_to_private_blocked(d):
    """Real socket, real redirect walk: hop 1 is the local double, hop 2
    points at the cloud metadata address and must never be contacted."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    with _Server() as s:
        _Double.redirect_to = "http://169.254.169.254/latest/meta-data/"
        real = _allow_loopback()
        try:
            try:
                fu.fetch_with_requests(f"http://127.0.0.1:{s.port}/redir")
                raise AssertionError("redirect to metadata was followed")
            except fu.BlockedURL as e:
                assert "cloud metadata" in str(e), str(e)
        finally:
            fu._classify_address = real
        assert _Double.hits == ["/redir"], _Double.hits
    log = [json.loads(x) for x in
           fu.LOG_PATH.read_text().splitlines() if x.strip()]
    assert any(r["event"] == "fetch" and r["status"] == 302 for r in log)


def test_internal_service_never_contacted(d):
    """A live loopback service (the shape of ollama:11434 or the board on
    :8377) is refused before a socket opens: it records zero hits."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    with _Server() as s:
        out = fu.run(f"http://127.0.0.1:{s.port}/", 4000, False)
        assert out["error"].startswith("Blocked: "), out["error"]
        assert "loopback" in out["error"]
        assert out["content"] is None
        assert _Double.hits == [], \
            f"internal service was contacted: {_Double.hits}"


def test_redirect_limit(d):
    """A public->public redirect loop stops at MAX_REDIRECTS."""
    real_dns = _fake_dns({"loop.example": ["93.184.216.34"]})
    calls = []

    class FakeResp:
        status_code = 302
        headers = {"Location": "http://loop.example/next"}

        def close(self):
            pass

    real_get = fu.requests.get
    fu.requests.get = lambda url, **kw: (calls.append(url), FakeResp())[1]
    try:
        try:
            fu.fetch_with_requests("http://loop.example/start")
            raise AssertionError("expected a redirect-limit block")
        except fu.BlockedURL as e:
            assert "redirects" in str(e), str(e)
        assert len(calls) == fu.MAX_REDIRECTS + 1, len(calls)
    finally:
        fu.requests.get = real_get
        socket.getaddrinfo = real_dns


# ── Playwright path ─────────────────────────────────────────────────────

class _FakeRoute:
    def __init__(self):
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class _FakeRequest:
    def __init__(self, url):
        self.url = url


def test_playwright_route_guard_blocks_internal_subrequests(d):
    """--js renders in a browser, which fetches subresources and follows
    in-page navigations on its own; the route guard applies the same
    policy to each one."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    real = _fake_dns({"cdn.example": ["93.184.216.34"]})
    try:
        guard = fu._playwright_route_guard()
        for url in ("http://127.0.0.1:11434/api/tags",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://100.101.102.103:8377/api/board",
                    "file:///etc/passwd"):
            r = _FakeRoute()
            guard(r, _FakeRequest(url))
            assert r.action == "abort", f"{url} was allowed into the page"
        ok = _FakeRoute()
        guard(ok, _FakeRequest("https://cdn.example/style.css"))
        assert ok.action == "continue"
    finally:
        socket.getaddrinfo = real
    rows = [json.loads(x) for x in
            fu.LOG_PATH.read_text().splitlines() if x.strip()]
    assert sum(1 for r in rows if r["event"] == "blocked") == 4


# ── run(): contract, errors, logging ────────────────────────────────────

def test_run_blocks_and_logs(d):
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    out = fu.run("http://169.254.169.254/latest/meta-data/", 4000, False)
    assert out["error"] == ("Blocked: 169.254.169.254 is a "
                            "cloud metadata service"), out["error"]
    assert out["content"] is None
    # CLI JSON contract unchanged
    for key in ("url", "title", "meta_description", "content",
                "content_length", "truncated", "links", "method", "error"):
        assert key in out, key
    rows = [json.loads(x) for x in
            fu.LOG_PATH.read_text().splitlines() if x.strip()]
    assert rows and rows[-1]["event"] == "blocked"
    assert rows[-1]["url"] == "http://169.254.169.254/latest/meta-data/"
    assert "cloud metadata" in rows[-1]["reason"]


def test_run_public_fetch_mocked(d):
    """Happy path stays intact: no live network, but the whole run()
    path executes and logs URL + resolved IP + outcome."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    real_dns = _fake_dns({"prog.example": ["93.184.216.34"]})
    html = ("<html><head><title>Program</title>"
            "<meta name='description' content='A program'></head>"
            "<body><main><p>" + ("Eligibility details. " * 40) +
            "</p><a href='/apply'>Apply now</a></main></body></html>")

    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = html

        def iter_content(self, chunk_size=0):
            yield html.encode()

        def raise_for_status(self):
            pass

        def close(self):
            pass

    real_get = fu.requests.get
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["redirects"] = kw.get("allow_redirects")
        # DNS is pinned while the request runs
        seen["pinned"] = socket.getaddrinfo("prog.example", 443)[0][4][0]
        return FakeResp()

    fu.requests.get = fake_get
    try:
        out = fu.run("https://prog.example/programs", 4000, False)
    finally:
        fu.requests.get = real_get
        socket.getaddrinfo = real_dns
    assert out["error"] is None, out["error"]
    assert out["title"] == "Program"
    assert out["meta_description"] == "A program"
    assert "Eligibility details." in out["content"]
    assert out["links"] and out["links"][0]["href"] == "/apply"
    assert out["method"] == "requests"
    assert seen["redirects"] is False, "requests must not follow redirects"
    assert seen["pinned"] == "93.184.216.34"
    rows = [json.loads(x) for x in
            fu.LOG_PATH.read_text().splitlines() if x.strip()]
    fetches = [r for r in rows if r["event"] == "fetch"]
    assert fetches and fetches[-1]["ip"] == "93.184.216.34"
    assert fetches[-1]["url"] == "https://prog.example/programs"
    assert rows[-1]["event"] == "done" and rows[-1]["outcome"] == "ok"


def test_js_path_validates_before_launching_browser(d):
    """--js must not reach Playwright (or file://) for a blocked URL."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    out = fu.run("file:///etc/passwd", 4000, True)
    assert out["error"].startswith("Blocked: "), out["error"]
    out = fu.run("http://[::1]/", 4000, True)
    assert "loopback" in out["error"], out["error"]


def test_log_path_is_never_production(d):
    prod = Path(os.path.expanduser("~/.ea")) / "logs" / "fetch-url.log"
    assert str(fu.LOG_PATH) != str(prod), fu.LOG_PATH
    assert "/home/ea-bot" not in str(fu.LOG_PATH)
    # the module's own default follows EA_DIR, which the harness threw away
    assert str(fu.EA_DIR) == _TMP_EA


# ── images ──────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def test_page_images_lists_the_share_image_first(d):
    html = ("<html><head><meta property='og:image' content='/share.jpg'></head><body>"
            "<img src='photos/a.jpg' alt=' The  dome '><img src='data:image/png;base64,AAAA'>"
            "<img data-src='https://cdn.example/b.webp'><img src='/share.jpg'></body></html>")
    imgs = fu.page_images(html, "https://site.example/story/")
    assert imgs == [{"src": "https://site.example/share.jpg", "alt": "share image"},
                    {"src": "https://site.example/story/photos/a.jpg", "alt": "The dome"},
                    {"src": "https://cdn.example/b.webp", "alt": ""}], imgs


def _serve_bytes(body, ctype="image/png"):
    class Resp:
        status_code = 200
        headers = {"content-type": ctype}
        _content = b""

        @property
        def content(self):   # requests builds it from _content, which the fetch sets
            return self._content

        def iter_content(self, chunk_size=0):
            for i in range(0, len(body), 65536):
                yield body[i:i + 65536]

        def raise_for_status(self):
            pass

        def close(self):
            pass
    return Resp()


def _with_image(d, body, ctype="image/png"):
    """Point the module at scratch folders and a canned response."""
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    fu.EA_DIR = d / "ea"
    fu.DECKS_ROOT = d / "presentations" / "decks"
    (fu.EA_DIR / "state").mkdir(parents=True, exist_ok=True)
    (fu.DECKS_ROOT / "talk" / "public" / "img").mkdir(parents=True, exist_ok=True)
    real_get, real_dns = fu.requests.get, _fake_dns({"img.example": ["93.184.216.34"]})
    fu.requests.get = lambda url, **kw: _serve_bytes(body, ctype)
    return real_get, real_dns


def _restore(real_get, real_dns):
    fu.requests.get = real_get
    socket.getaddrinfo = real_dns
    fu.EA_DIR = Path(_TMP_EA)


def test_save_image_writes_a_real_image_to_a_new_file(d):
    real = _with_image(d, PNG)
    try:
        dest = fu.DECKS_ROOT / "talk" / "public" / "img" / "dome.jpg"
        out = fu.save_image("https://img.example/dome", str(dest))
        assert out["error"] is None, out
        saved = Path(out["saved"])
        assert saved.name == "dome.png", saved          # the suffix follows the bytes
        assert saved.read_bytes() == PNG and out["type"] == "png"
        assert (saved.stat().st_mode & 0o777) == 0o644
        again = fu.save_image("https://img.example/dome", str(dest))
        assert "exists" in again["error"] and saved.read_bytes() == PNG
        rows = [json.loads(x) for x in fu.LOG_PATH.read_text().splitlines() if x.strip()]
        assert any(r["event"] == "saved" and r["dest"] == str(saved) and r["sha256"] == out["sha256"]
                   and r["ip"] == "93.184.216.34" for r in rows), rows
    finally:
        _restore(*real)


def test_save_image_refuses_what_is_not_a_raster_image(d):
    for body, ctype in ((b"<html><body>photo page</body></html>", "text/html"), (SVG, "image/svg+xml")):
        real = _with_image(d, body, ctype)
        try:
            out = fu.save_image("https://img.example/x", str(fu.EA_DIR / "state" / "x.png"))
            assert "not a PNG, JPEG, GIF or WebP" in out["error"], out
            assert not list((fu.EA_DIR / "state").iterdir())
        finally:
            _restore(*real)


def test_save_image_writes_only_in_its_folders_and_under_its_cap(d):
    real = _with_image(d, PNG)
    try:
        outside = d / "elsewhere"
        outside.mkdir()
        assert "outside the folders" in fu.save_image("https://img.example/x", str(outside / "x.png"))["error"]
        escape = fu.EA_DIR / "state" / "out"
        escape.symlink_to(outside)
        assert "outside the folders" in fu.save_image("https://img.example/x", str(escape / "x.png"))["error"]
        assert "file name" in fu.save_image("https://img.example/x", str(fu.EA_DIR / "state" / ".hidden"))["error"]
        assert "file name" in fu.save_image("https://img.example/x", str(fu.EA_DIR / "state" / "a b.png"))["error"]
        link = fu.EA_DIR / "state" / "linked.png"
        link.symlink_to(outside / "target.png")
        assert "exists" in fu.save_image("https://img.example/x", str(link))["error"]
        assert not (outside / "target.png").exists() and list(outside.iterdir()) == []
        cap = fu.MAX_IMAGE_BYTES
        fu.MAX_IMAGE_BYTES = 10
        try:
            assert "over" in fu.save_image("https://img.example/x", str(fu.EA_DIR / "state" / "big.png"))["error"]
        finally:
            fu.MAX_IMAGE_BYTES = cap
    finally:
        _restore(*real)


def test_save_image_keeps_the_address_policy(d):
    fu.LOG_PATH = d / "logs" / "fetch-url.log"
    fu.EA_DIR = d / "ea"
    (fu.EA_DIR / "state").mkdir(parents=True)
    real_get = fu.requests.get
    fu.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("contacted"))
    try:
        out = fu.save_image("http://127.0.0.1/x.png", str(fu.EA_DIR / "state" / "x.png"))
        assert out["error"].startswith("Blocked"), out
        assert not list((fu.EA_DIR / "state").iterdir())
    finally:
        fu.requests.get = real_get
        fu.EA_DIR = Path(_TMP_EA)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"  ✅ {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"  ❌ {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
