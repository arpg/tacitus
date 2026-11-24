"""The web app in a browser: the relay (docs/webchat-relay.md).

The relay's crypto reproduces the Python vectors
(tests/fixtures/relay_vectors.json), the QR encoder draws codes a decoder
reads, a phone pairs and chats through a real relay with the real web
front end, a forged error never sends a message twice, and a pairing link
that is bad or lacks the bundle's hash is refused.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    FAKE_HIDDEN, QR_BASE, QR_PINNED, TESTS, WATCH_LIVE, _pair_and_approve, _send_card, app,
    browser, relay_bot, stub_factory, wait_for,
)


VECTORS = TESTS / "fixtures" / "relay_vectors.json"


def _vector_check(page, v):
    return page.evaluate("""async (v) => {
        const pkcs8 = (h) => crypto.subtle.importKey('pkcs8',
            cat(unhex('302e020100300506032b656e04220420'), unhex(h)), { name: 'X25519' }, false, ['deriveBits']);
        const rec = { install: v.bot.install_id, botPublic: b64d(v.bot.public),
                      priv: await pkcs8(v.device.private), pub: b64d(v.device.public) };
        const eph = { priv: await pkcs8(v.phone_ephemeral.private), pub: b64d(v.phone_ephemeral.public) };
        const out = {};
        const { frame, pending } = await relayHello(rec, { id: v.pairing.id, psk: b64d(v.pairing.psk) },
            { eph, payload: b64d(v.hello.payload), id: v.hello.frame.id });
        out.hello = frame;
        out.h = hex(pending.h);
        const keys = await sessionKeys(pending, b64d(v.bot_ephemeral.public));
        out.session = { k_c2b: hex(keys.c2b), k_b2c: hex(keys.b2c), sid: keys.sid };
        const s = await relayFinish(pending, v.welcome.frame);
        out.device = s.device;
        out.req_plaintext = b64e(pack(v.req.meta, b64d(v.req.body)));
        out.req = await sealFrame(s, 'req', v.req.frame.id, pack(v.req.meta, b64d(v.req.body)));
        try { await openFrame(s, v.res.frame, 'res', 'not-my-request'); out.other = 'opened'; } catch (e) { out.other = e.message; }
        try { await openFrame(s, v.res.frame, 'sse', v.res.frame.id); out.kind = 'opened'; } catch (e) { out.kind = e.message; }
        const [meta, body] = unpack(await openFrame(s, v.res.frame, 'res', v.res.frame.id));
        out.res = { meta, body: b64e(body) };
        out.sse = unpack(await openFrame(s, v.sse.frame, 'sse', v.sse.frame.id))[0];
        try { await openFrame(s, v.res.frame, 'res', v.res.frame.id); out.replay = 'opened'; } catch (e) { out.replay = e.message; }
        const bent = { ...v.sse.frame, id: 'another' };
        try { await openFrame({ ...s, seen: new Set(), top: -1 }, bent, 'sse', 'another'); out.moved = 'opened'; } catch (e) { out.moved = e.name; }
        out.code = await pairCode(unhex(v.hello.h));
        const plainEph = { priv: await pkcs8(v.plain_hello.phone_ephemeral.private),
                           pub: b64d(v.plain_hello.phone_ephemeral.public) };
        out.plain = (await relayHello(rec, null, { eph: plainEph, payload: b64d(v.hello.payload),
                                                   id: v.plain_hello.frame.id })).frame;
        return out;
    }""", v)


def test_the_app_crypto_reproduces_the_python_vectors(app):
    """The WebCrypto side of the protocol, byte for byte against the
    vectors relay_crypto made (tests/test_relay.py checks the file is
    what relay_crypto makes)."""
    v = json.loads(VECTORS.read_text())
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    out = _vector_check(page, v)
    assert out["hello"] == v["hello"]["frame"]
    assert out["h"] == v["hello"]["h"]
    assert out["session"] == v["session"]
    assert out["device"] == v["welcome"]["info"]["device"] == v["device"]["id"]
    assert out["req_plaintext"] == v["req"]["plaintext"]
    assert out["req"] == v["req"]["frame"]
    assert out["res"] == {"meta": v["res"]["meta"], "body": v["res"]["body"]}
    assert out["sse"] == v["sse"]["event"]
    assert out["replay"] == "replayed frame" and out["moved"] == "OperationError"
    assert out["other"] == "bad frame" and out["kind"] == "bad frame"
    assert out["code"] == v["hello"]["code"]  # the pairing code the bot shows
    assert out["plain"] == v["plain_hello"]["frame"]
    assert not errors, errors


def test_the_qr_encoder_draws_codes_a_decoder_reads(app):
    """The pinned codes, and where OpenCV is installed, its decoder reads
    them (its decoder misses some valid version 16 and 17 codes, those of
    the independent encoder too, so the 460-byte text is pinned only)."""
    import hashlib
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    grids = page.evaluate("(texts) => texts.map((t) => QR.encode(t).map((r) => r.map(Number).join('')))",
                          [QR_BASE[:n] for n in QR_PINNED])
    for (n, (sha, decodable)), rows in zip(QR_PINNED.items(), grids):
        assert hashlib.sha256("\n".join(rows).encode()).hexdigest() == sha, n
    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    det = cv2.QRCodeDetector()
    for (n, (sha, decodable)), rows in zip(QR_PINNED.items(), grids):
        if not decodable:
            continue
        g = np.array([[c == "1" for c in r] for r in rows], dtype=np.uint8)
        m = g.shape[0]
        img = np.full((m + 8, m + 8), 255, np.uint8)
        img[4:4 + m, 4:4 + m] = np.where(g == 1, 0, 255)
        img = cv2.resize(img, ((m + 8) * 8, (m + 8) * 8), interpolation=cv2.INTER_NEAREST)
        assert det.detectAndDecode(img)[0] == QR_BASE[:n], n
    assert not errors, errors


def test_a_phone_pairs_on_the_tailnet_and_chats_through_the_relay(browser, relay_bot):
    """The owner opens "Add a device" on the tailnet origin; a phone opens
    the link on that origin while on the tailnet, waits for the owner's
    approval by its code, then reads, writes and confirms a card through
    the relay from off the tailnet."""
    from playwright.sync_api import expect
    fe, got = relay_bot["fe"], relay_bot["got"]
    contexts = []
    try:
        tail, page, errors, asked = _pair_and_approve(browser, relay_bot, contexts)
        devices = fe.relay.devices()
        assert [d["origin"] for d in devices] == ["own"]
        stored = page.evaluate("""async () => { const r = await loadPairing();
            return { type: r.priv.type, extractable: r.priv.extractable, device: r.device,
                     local: Object.keys(localStorage) }; }""")
        assert stored == {"type": "private", "extractable": False, "device": devices[0]["id"],
                          "local": ["tacitus-welcome-seen"]}  # the test's own mark, nothing of the pairing
        expect(page.locator("#relay-note")).to_be_hidden()  # approved: the note went

        fe.post("main", "Hello through the relay")
        item = page.locator(".thread-item:has-text('Hello through the relay')")
        expect(item).to_be_visible(timeout=10000)
        item.click()
        page.fill("#text", "Answer from the phone")
        page.press("#text", "Enter")
        wait_for(lambda: any(getattr(m, "text", "") == "Answer from the phone" for m in got), 10)

        _send_card(fe)
        page.evaluate("location.hash = '#/dm'")  # Action requests: the request opens in place
        card = page.locator(".req:has-text('Send this draft?') .req-head")
        expect(card).to_be_visible(timeout=10000)
        card.click()
        page.locator(".req .card button.primary").click()
        wait_for(lambda: any(getattr(m, "action_id", "") == "approve_send" for m in got), 10)

        # The URLs the relay saw carry a one-time ticket, never the key.
        key = json.loads(page.evaluate("JSON.stringify(RELAY.rec.clientKey)"))
        relayed = [u for u in asked if u.startswith(relay_bot["rbase"] + "/r/")]
        assert relayed and all(re.search(r"/(c|sse)(\?t=[0-9a-f]{32})?$", u) for u in relayed), relayed
        assert any("/sse?t=" in u for u in relayed)
        assert not [u for u in asked if key in u]

        # The keys outlive a reload: a plain hello opens a new session.
        page.reload()
        expect(page.locator("#live.on")).to_be_attached(timeout=15000)

        # A removed phone hears "auth" and says so; it keeps its keys.
        fe.relay.revoke(devices[0]["id"])
        page.reload()
        expect(page.locator("#relay-note")).to_contain_text("did not accept this device", timeout=15000)
        assert page.evaluate("loadPairing().then((r) => Boolean(r && r.priv))")
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_page_hidden_for_long_lets_its_relayed_stream_go(browser, relay_bot):
    """Through the relay a page hidden for long closes its event stream,
    one of the few the relay keeps for an install; back in view it opens
    one at once, reads "Reconnecting" meanwhile, and shows what came while
    it was hidden."""
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    contexts = []
    try:
        _tail, page, errors, _asked = _pair_and_approve(browser, relay_bot, contexts)
        wait_for(lambda: fe.relay.status()["streams"] == 1, 10)
        page.evaluate(FAKE_HIDDEN)
        page.evaluate(WATCH_LIVE)
        page.evaluate("closeWhenHidden.ms = 200; __hide(true)")
        wait_for(lambda: fe.relay.status()["streams"] == 0, 10)  # the relay saw it go, and told the bot
        assert page.evaluate("S.es.asleep") is True
        fe.post("main", "Posted while the page was hidden")
        page.evaluate("__hide(false)")
        expect(page.locator(".thread-item:has-text('Posted while the page was hidden')")).to_be_visible(timeout=15000)
        expect(page.locator("#live.on")).to_be_attached(timeout=10000)
        assert "live wait" in page.evaluate("__live") and "live off" not in page.evaluate("__live")
        wait_for(lambda: fe.relay.status()["streams"] == 1, 10)
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_forged_error_never_sends_a_message_twice(browser, relay_bot):
    """The relay lets a POST through to the bot, drops the answer and forges
    `err session`: the phone says hello again but does not send the message
    a second time, and tells the owner it may have arrived."""
    from playwright.sync_api import expect
    fe, got = relay_bot["fe"], relay_bot["got"]
    contexts = []
    try:
        _tail, page, errors, _asked = _pair_and_approve(browser, relay_bot, contexts)
        page.locator("#channel-list .chan .c-name").first.click()  # a channel, not the Needs you entry
        forge = {"armed": False, "posts": 0}

        def relay_c(route):
            if not forge["armed"]:
                return route.continue_()
            forge["armed"] = False
            forge["posts"] += 1
            route.fetch()  # the bot gets the frame and runs the request
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": "x", "k": "err", "e": "session"}))
        page.route(re.compile(r".*/r/[a-z2-7]{26}/c$"), relay_c)
        page.locator("#new-thread").click()
        # The frames are sealed, so the route cannot tell the send from a
        # read the page makes on its own. The page arms the forge itself,
        # as the send leaves.
        page.expose_function("__armForge", lambda: forge.update(armed=True))
        page.evaluate("""() => { const api = RELAY.api; RELAY.api = async (method, path, body) => {
            if (method === 'POST' && path.endsWith('/messages')) await window.__armForge();
            return api(method, path, body); }; }""")
        page.fill("#text", "Sent once through the relay")
        page.press("#text", "Enter")
        expect(page.locator("#composer-note")).to_contain_text("may have it anyway", timeout=10000)
        wait_for(lambda: any(getattr(m, "text", "") == "Sent once through the relay" for m in got), 10)
        time.sleep(1.5)
        assert [getattr(m, "text", "") for m in got].count("Sent once through the relay") == 1
        assert forge["posts"] == 1
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_relay_copy_that_cannot_check_its_code_pairs_nothing(browser, stub_factory):
    """A copy of the app on the relay's origin whose bundle.js has no
    verifyBundle (one from before the signed bundle) stops on "cannot check
    its code" before it makes a key: nothing is paired."""
    from playwright.sync_api import expect
    pytest.importorskip("cryptography")
    import webui_stub_server as W
    url, stub = stub_factory(sign=True)
    stub.files_served["bundle.js"] = b"'use strict';\n"
    here = url.rstrip("/").rsplit("/chat", 1)[0]
    payload = _link_payload(r=here, i=W.STUB_INSTALL, k=W.STUB_BOT_PUBLIC, c=W.STUB_CLIENT_KEY,
                            v=2, b=stub.bundle.hash, a=url)
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "#tr2=" + payload)
        expect(page.locator("h1")).to_have_text("Tacitus stopped", timeout=10000)
        expect(page.locator("body")).to_contain_text("This copy of the app cannot check its code against your Tacitus")
        assert "tr2=" not in page.url
        assert page.evaluate("loadPairing().then((r) => r === undefined)")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_relayed_file_opens_only_as_a_raster_image(app):
    """Through the relay, a stored file becomes a blob with this page's
    origin: an SVG or an HTML file is plain bytes there, and every link
    downloads instead of opening a page."""
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    out = page.evaluate("""async () => {
        const kept = T;
        const files = { svg: ['<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', 'image/svg+xml'],
                        png: ['not really a png', 'image/png'], html: ['<p>page</p>', 'text/html'] };
        T = { relay: true, api: async (m, p) => { const [b, t] = files[p.split('/').pop()];
                                                   return new Response(b, { headers: { 'content-type': t } }); } };
        const made = new Map();  // blob URL -> the type the app gave its blob
        const create = URL.createObjectURL;
        URL.createObjectURL = (b) => { const u = create(b); made.set(u, b.type); return u; };
        const out = {};
        for (const [id, [, type]] of Object.entries(files)) {
            const box = renderFile({ id, name: `f.${id}`, mimetype: type, size: 1 });
            document.body.append(box);
            const link = box.querySelector('a.file-link');
            link.dispatchEvent(new MouseEvent('click', { cancelable: true }));
            await new Promise((r) => setTimeout(r, 300));
            const img = box.querySelector('img');
            const href = img ? img.src : link.href;
            out[id] = { type: made.get(href) || href,
                        download: [...box.querySelectorAll('a')].every((a) => a.hasAttribute('download')),
                        target: [...box.querySelectorAll('a')].some((a) => a.hasAttribute('target')) };
            box.remove();
        }
        T = kept;
        URL.createObjectURL = create;
        return out;
    }""")
    assert out == {"svg": {"type": "application/octet-stream", "download": True, "target": False},
                   "png": {"type": "image/png", "download": True, "target": False},
                   "html": {"type": "application/octet-stream", "download": True, "target": False}}


def test_the_app_crypto_reproduces_the_vectors_in_webkit(stub_factory):
    """The same vectors in WebKit (Safari's engine), where it is installed."""
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:  # noqa: BLE001 - no driver on this machine
        pytest.skip(f"playwright cannot start: {e}")
    try:
        try:
            wk = pw.webkit.launch()
        except Exception as e:  # noqa: BLE001 - no WebKit installed
            pytest.skip(f"no webkit for playwright: {e}")
        try:
            url, _stub = stub_factory()
            page = wk.new_page()
            page.goto(url)
            page.wait_for_selector(".thread-item")
            v = json.loads(VECTORS.read_text())
            out = _vector_check(page, v)
            assert out["hello"] == v["hello"]["frame"] and out["session"] == v["session"]
            assert out["req"] == v["req"]["frame"] and out["sse"] == v["sse"]["event"]
            assert out["code"] == v["hello"]["code"] and out["plain"] == v["plain_hello"]["frame"]
        finally:
            wk.close()
    finally:
        pw.stop()


def _link_payload(**over) -> str:
    import base64
    obj = {"v": 1, "r": "https://relay.example.net", "i": "b" * 26,
           "k": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode(), "c": "made-up-key",
           "p": "0" * 32, "s": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode(),
           "x": int(time.time()) + 600}
    obj.update(over)
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_a_relay_copy_link_without_the_bundle_hash_is_refused(browser, stub_factory):
    """A link to the relay's copy of the app (its relay is the page's own
    origin) must carry the hash of the bot's code: a version 1 link, or a
    version 2 one without `b`, pairs nothing; nor does a link whose `a`
    names another origin."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    url, stub = stub_factory()
    here = url.rstrip("/").rsplit("/chat", 1)[0]
    mine = {"r": here, "i": W.STUB_INSTALL, "k": W.STUB_BOT_PUBLIC, "c": W.STUB_CLIENT_KEY}
    for kind, payload, says in (("tr1", _link_payload(**mine), "no fingerprint of the app"),
                                ("tr2", _link_payload(**mine, v=2, a=url), "no fingerprint of the app"),
                                ("tr2", _link_payload(**mine, v=2, b="e" * 64, a="https://elsewhere.example.net/"),
                                 "opens the app elsewhere"),
                                ("tr1", _link_payload(**mine, v=2, b="e" * 64), "not a setup link")):
        ctx = browser.new_context()
        try:
            page = ctx.new_page()
            page.goto(url + f"#{kind}=" + payload)
            expect(page.locator("#toast")).to_contain_text(says)
            assert f"{kind}=" not in page.url
            assert page.evaluate("loadPairing().then((r) => r === undefined)")
        finally:
            ctx.close()


def test_a_bad_pairing_link_is_refused_and_leaves_the_address_bar(browser, stub_factory):
    """On the tailnet the bot names its install and its key: a link for
    another install or another key is refused, as is an expired one, and a
    link opened where the bot does not answer (off the tailnet); the
    fragment goes either way, and no pairing is kept."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    url, stub = stub_factory()
    mine = {"i": W.STUB_INSTALL, "k": W.STUB_BOT_PUBLIC}
    for payload, says, off in ((_link_payload(), "a different Tacitus", False),
                               (_link_payload(i=W.STUB_INSTALL), "a different Tacitus", False),
                               (_link_payload(**mine, x=int(time.time()) - 1), "expired", False),
                               (_link_payload(**mine), "on your own network", True),
                               ("bm90LWpzb24", "Setup failed", False)):
        ctx = browser.new_context()
        if off:
            ctx.route(re.compile(r".*/api/relay/status$"), lambda route: route.abort())
        try:
            page = ctx.new_page()
            page.goto(url + "#tr1=" + payload)
            expect(page.locator("#toast")).to_contain_text(says)
            assert "tr1=" not in page.url
            assert page.evaluate("loadPairing().then((r) => r === undefined)")
            expect(page.locator(".thread-item").first).to_be_visible()  # the chat still starts
        finally:
            ctx.close()


def test_an_unpaired_browser_off_the_network_is_told_how_to_set_up(app):
    """With no pairing in this browser's storage and the bot's own address
    unreachable (Tailscale off), the app says where it is reachable and
    what to do, instead of a bare "could not reach"."""
    import re
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.route(re.compile(r".*/chat/api/.*"), lambda route: route.abort("connectionfailed"))
    page.reload(wait_until="load")
    expect(page.locator("#toast")).to_contain_text("reachable only on your own network", timeout=15000)
    expect(page.locator("#toast")).to_contain_text('"Set up this phone"')
