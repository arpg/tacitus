"""The web app in a browser: its code against the bot's signed manifest
(docs/webchat.md) and new releases.

The service worker serves the signed files and refuses a changed one; an
open app offers a release the bot signs; a copy of the app from the relay
attests its check, while only the owner lets it press card buttons; and a
phone off the tailnet, or on the relay's origin, takes a new release from
the bot through the relay: the worker's code (bundle.js, which sw.js
loads) and the page's security policy with it.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    WEBUI, _kept, _owner_context, _pair_and_approve, _relay_bot, _send_card, _worker_state, app,
    browser, open_devices, relay_bot, stub_factory, wait_for,
)


def test_verify_bundle_checks_the_signature_the_key_and_the_hash(app):
    """bundle.js writes a manifest's canonical bytes as relay_crypto does,
    so the page's bundle hash is the bot's; a wrong hash, another key, a
    changed manifest or signature, or a field it does not know is refused."""
    pytest.importorskip("cryptography")
    page, stub, errors = app(sign=True)
    page.wait_for_selector(".thread-item")
    out = page.evaluate("""async ([doc, hash]) => {
        const res = {};
        const run = async (name, ...args) => {
            try { res[name] = (await verifyBundle(...args)).hash; }
            catch (e) { res[name] = (e instanceof BundleError ? 'refused: ' : 'error: ') + e.message; }
        };
        const copy = (fn) => { const d = structuredClone(doc); fn(d); return d; };
        await run('plain', doc);
        await run('hash', doc, hash);
        await run('pinned', doc, null, doc.manifest.key);
        await run('wrongHash', doc, '0'.repeat(64));
        await run('otherKey', doc, null, 'A'.repeat(43));
        await run('changed', copy((d) => { d.manifest.files['app.js'] = 'f'.repeat(64); }));
        await run('signature', copy((d) => { d.signature = (d.signature[0] === 'A' ? 'B' : 'A') + d.signature.slice(1); }));
        await run('extra', copy((d) => { d.manifest.note = 'x'; }));
        await run('float', copy((d) => { d.manifest.built_at += 0.5; }));
        return res;
    }""", [stub.bundle.doc, stub.bundle.hash])
    assert out["plain"] == out["hash"] == out["pinned"] == stub.bundle.hash, out
    for name in ("wrongHash", "otherKey", "changed", "signature", "extra", "float"):
        assert out[name].startswith("refused: "), (name, out[name])
    assert not errors, errors


def test_the_worker_serves_the_signed_app_and_refuses_a_changed_file(app):
    """With the bot's signed manifest the service worker serves the app's
    files that match it. A changed app.js from the server stops the app:
    every page shows the refusal, which runs no script, and the kept copy
    is gone. "Check again" runs the app once the server sends the signed
    file again."""
    pytest.importorskip("cryptography")
    from playwright.sync_api import expect
    page, stub, errors = app(sign=True)
    page.wait_for_selector(".thread-item")
    trusted = wait_for(lambda: _worker_state(page, "trusted"), 15)
    assert trusted["hash"] == stub.bundle.hash and trusted["pin"] is False
    page.reload()
    page.wait_for_selector(".thread-item")
    assert page.evaluate("Boolean(navigator.serviceWorker.controller)")
    assert not errors, errors

    stub.files_served["app.js"] = (WEBUI / "app.js").read_bytes() + b"\n// changed\n"
    page.reload()
    expect(page.locator("h1")).to_have_text("Tacitus stopped", timeout=15000)
    expect(page.locator("body")).to_contain_text("app.js")
    assert page.locator("script").count() == 0
    assert page.evaluate("caches.keys()") == ["tacitus-webchat-v3-bundle"]  # no copy is kept
    assert _worker_state(page, "refused") == {"file": "app.js"}
    page.reload()  # a refused app stays refused
    expect(page.locator("h1")).to_have_text("Tacitus stopped")

    stub.files_served.clear()
    page.click("text=Check again")
    page.wait_for_selector(".thread-item", timeout=15000)
    assert wait_for(lambda: _worker_state(page, "trusted"), 15)["hash"] == stub.bundle.hash
    assert _worker_state(page, "refused") is None


@pytest.fixture
def relay_bot_changed(tmp_path):
    """As relay_bot, with a relay whose copy of the app has a changed app.js."""
    copy = tmp_path / "relay-webui"
    copy.mkdir()
    for f in WEBUI.iterdir():
        (copy / f.name).write_bytes(f.read_bytes())
    (copy / "app.js").write_bytes((WEBUI / "app.js").read_bytes() + b"\n// changed\n")
    yield from _relay_bot(tmp_path, copy)


@pytest.fixture
def relay_bot_deploys(tmp_path):
    """As relay_bot, with a bot that serves and signs a copy of the app
    (`webui`) that a test changes, and signs again only when told."""
    copy = tmp_path / "bot-webui"
    copy.mkdir()
    for f in WEBUI.iterdir():
        (copy / f.name).write_bytes(f.read_bytes())
    yield from _relay_bot(tmp_path, WEBUI, copy)


KEEP_HEADERS = ["content-security-policy", "x-content-type-options", "referrer-policy", "cross-origin-resource-policy"]


def _kept_headers(page, name):
    """The security headers of the service worker's copy of one file."""
    return page.evaluate("""async ([name, keep]) => {
        const hit = await (await caches.open('tacitus-webchat-v3')).match(name === 'index.html' ? './' : name);
        return hit ? Object.fromEntries(keep.filter((k) => hit.headers.has(k)).map((k) => [k, hit.headers.get(k)])) : null;
    }""", [name, KEEP_HEADERS])


def _restart_worker(page):
    """Stops the app's service worker, as the browser does after a quiet
    spell: the next request starts it again from sw.js."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("ServiceWorker.enable")
    cdp.send("ServiceWorker.stopAllWorkers")
    cdp.detach()


# A test's next release of the worker's code (bundle.js): each answer it serves says so.
NEXT_WORKER = b"""
(() => {
  const w = self.TacitusWorker;
  const start = w.start;
  w.start = (names) => {
    const run = start(names);
    const serve = run.serve;
    return Object.assign(run, { serve: (req) => serve(req).then((res) => {
      if (!res.status) return res;
      const headers = new Headers(res.headers);
      headers.set('x-tacitus-release', 'next');
      return new Response(res.body, { status: res.status, headers });
    }) });
  };
})();
"""


def _hold_looks(page):
    """This page looks for no release while it is open (app.js lookAgain), so a
    test of what the next start takes sees the release then."""
    page.evaluate("S.looking = true")


def test_an_open_app_offers_a_release_the_bot_signs_and_takes_it_on_update(browser, relay_bot_deploys):
    """The bot signs the app again while the app is open on the tailnet (a
    deploy that changed only the app's files restarts nothing): the stream
    says `release`, the app checks the release and its worker keeps it, and
    a note above the composer offers it, with no reload by itself. Update
    reloads the page, which runs the new release, and the note is gone."""
    from playwright.sync_api import expect
    bot = relay_bot_deploys
    fe, webui = bot["fe"], bot["webui"]
    line = "A new version of Tacitus is ready."
    contexts = []
    try:
        page = _owner_context(browser, fe, contexts, workers=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(bot["public"] + "/")
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        first = fe.bundle_hash()
        wait_for(lambda: (_worker_state(page, "trusted") or {}).get("hash") == first, 15)
        expect(page.locator("#app-update")).to_be_hidden()
        page.evaluate("window.__open = true")
        css = (webui / "app.css").read_bytes() + b"\n/* the next release */\n"
        (webui / "app.css").write_bytes(css)
        assert fe.refresh_bundle() != first
        expect(page.locator("#app-update")).to_be_visible(timeout=20000)
        expect(page.locator("#app-update")).to_contain_text(line)
        expect(page.locator("#update-note")).to_have_text(line)
        assert page.evaluate("window.__open") is True  # no reload in the middle of a conversation
        assert _worker_state(page, "trusted")["hash"] == fe.bundle_hash()
        page.click("#app-update-go")
        page.wait_for_function("window.__open === undefined", timeout=15000)
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        assert page.evaluate("fetch('app.css').then((r) => r.text())") == css.decode()
        expect(page.locator("#app-update")).to_be_hidden()
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_phone_off_the_tailnet_takes_a_new_release_through_the_relay(browser, relay_bot_deploys, caplog):
    """A phone paired on the tailnet runs the copy its service worker
    checked there. Off the tailnet, the bot deploys a new app.css and signs
    again. At its next start (this test holds the look an open app makes) the phone fetches the signed manifest and the
    changed file from the bot through the relay and checks the file; its
    worker checks it again and keeps it. The phone offers the new version,
    and does not reload. A file that does not hash to the
    manifest is kept by neither, and the phone reports it to the bot."""
    import logging
    from playwright.sync_api import expect
    bot = relay_bot_deploys
    fe, webui = bot["fe"], bot["webui"]
    line = "A new version of Tacitus is ready."
    contexts = []
    try:
        _tail, page, errors, _asked = _pair_and_approve(browser, bot, contexts, workers=True)
        first = fe.bundle_hash()
        wait_for(lambda: (_worker_state(page, "trusted") or {}).get("hash") == first, 15)
        old = (webui / "app.css").read_bytes()
        assert _kept(page, "app.css") == old.decode()
        # Off the tailnet: nothing on the bot's own origin answers the page or its worker.
        page.context.route(re.compile(re.escape(bot["public"]) + "/.*"), lambda route: route.abort())
        new = old + b"\n/* the next release */\n"
        (webui / "app.css").write_bytes(new)
        _hold_looks(page)
        assert fe.refresh_bundle() != first

        # The bot serves app.css with bytes its manifest does not name.
        fe.note_served = lambda rel, data: True
        (webui / "app.css").write_bytes(old + b"\n/* not signed */\n")
        with caplog.at_level(logging.INFO, logger="chat.web"):
            page.reload()
            expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
            wait_for(lambda: any('"what": "update"' in r.getMessage() and "app.css does not match the manifest"
                                 in r.getMessage() for r in caplog.records), 20)
        assert _kept(page, "app.css") == old.decode()
        assert _worker_state(page, "trusted")["hash"] == first
        expect(page.locator("#update-note")).to_be_hidden()
        # The worker hashes what the page hands it again, and keeps nothing that differs.
        out = page.evaluate("""async () => toWorker({ fromRelay: await call('GET', '/bundle'),
            files: { 'app.css': { type: 'text/css', bytes: new TextEncoder().encode('body {}').buffer } } })""")
        assert out == {"error": "app.css does not match the manifest"}
        assert _kept(page, "app.css") == old.decode()
        assert _worker_state(page, "trusted")["hash"] == first

        # The bot serves the file it signed: the phone keeps it and says so.
        del fe.note_served
        (webui / "app.css").write_bytes(new)
        page.reload()
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        page.evaluate("window.__open = true")
        expect(page.locator("#app-update")).to_be_visible(timeout=20000)
        expect(page.locator("#update-note")).to_have_text(line)
        assert page.evaluate("window.__open") is True  # no reload in the middle of a conversation
        assert _kept(page, "app.css") == new.decode()
        assert _worker_state(page, "trusted")["hash"] == fe.bundle_hash()
        # The next start runs the new release from the worker's copy.
        page.reload()
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        assert page.evaluate("fetch('app.css').then((r) => r.text())") == new.decode()
        expect(page.locator("#update-note")).to_be_hidden()

        # A release that changes only the page's policy: the bot signs it (headers.json),
        # and the worker's copy of the page takes it, through the relay.
        policy = fe.html_csp + "; manifest-src 'self'"
        assert _kept_headers(page, "index.html")["content-security-policy"] == fe.html_csp
        fe.html_csp = policy
        _hold_looks(page)
        fe.refresh_bundle()
        page.reload()
        expect(page.locator("#app-update")).to_be_visible(timeout=20000)
        assert _kept_headers(page, "index.html") == {k.lower(): v for k, v in fe.page_headers()["index.html"].items()}
        assert _worker_state(page, "trusted")["headers"] == json.loads(fe.bundle.written["headers.json"])
        assert page.reload().headers["content-security-policy"] == policy  # the next start runs under it
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        # A bot that signs no headers (an earlier release): a page it brings keeps the kept copy's.
        fe.page_headers = lambda: None
        html = (webui / "index.html").read_bytes() + b"<!-- the next release -->\n"
        (webui / "index.html").write_bytes(html)
        _hold_looks(page)
        fe.refresh_bundle()
        assert "headers.json" not in fe.bundle.files
        page.reload()
        expect(page.locator("#app-update")).to_be_visible(timeout=20000)
        assert _kept(page, "index.html") == html.decode()
        assert _kept_headers(page, "index.html")["content-security-policy"] == policy
        assert _worker_state(page, "trusted")["headers"] is None
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def _relay_origin_phone(browser, relay_bot, contexts, bundle=None, approve=True):
    """A phone that opens a link to the relay's copy of the app with the
    bot's bundle hash (or `bundle`), as "Phone not on your network" makes one,
    pairs with no chat.json opt-in, and waits for approval; the bot
    approves it unless `approve` is false. (page, errors, device id)."""
    from playwright.sync_api import expect
    from botcore.chat.relay_client import relay_app_url
    fe = relay_bot["fe"]
    assert fe.relay.relay_origin_access == "off"
    link = fe.relay.new_pairing(app_url=relay_app_url(relay_bot["rbase"]), relay_origin=True,
                                bundle=bundle or fe.bundle_hash())
    assert link.startswith(relay_bot["rbase"] + "/#tr2=")
    ctx = browser.new_context()
    contexts.append(ctx)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(link)
    expect(page.locator("#relay-note")).to_contain_text("waits for approval", timeout=15000)
    assert "tr2=" not in page.url
    devices = fe.relay.devices()
    assert [(d["origin"], d["pending"], d["bundle_verified"]) for d in devices] == [("relay", True, False)]
    if approve:
        fe.relay.approve(devices[0]["id"])
    return page, errors, devices[0]["id"]


def test_a_relay_origin_phone_checks_its_app_and_presses_cards_when_the_owner_allows(browser, relay_bot):
    """A relay-origin phone runs the relay's copy of the app. The owner
    approves it under "Add a device" with card buttons left off (the
    default). It fetches the bot's signed manifest through the relay, its
    service worker finds every file of the copy in it, and the phone
    attests the bundle: the owner's list drops "app not checked", which
    grants nothing. It chats, its card buttons stay greyed out, and they work once
    the owner ticks "Allow card buttons on this phone" for it."""
    from playwright.sync_api import expect
    fe, got = relay_bot["fe"], relay_bot["got"]
    contexts = []
    try:
        _send_card(fe)
        page, errors, device = _relay_origin_phone(browser, relay_bot, contexts, approve=False)
        tail = _owner_context(browser, fe, contexts).new_page()
        tail.goto(relay_bot["public"] + "/")
        open_devices(tail)
        row = tail.locator(f"#phone-devices li[data-device='{device}']")
        expect(row).to_contain_text("waits for approval", timeout=10000)
        box = row.locator("input[type=checkbox]")
        expect(row.locator("label")).to_have_text("Allow card buttons on this phone")
        expect(box).not_to_be_checked()
        row.locator("button", has_text="Approve").click()
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=20000)
        (d,) = fe.relay.devices()
        assert d["pending"] is False and d["bundle_verified"] is True and d["cards_allowed"] is False
        expect(row).to_contain_text("not on your network", timeout=10000)
        expect(row).not_to_contain_text("app not checked")
        trusted = _worker_state(page, "trusted")
        assert trusted["hash"] == fe.bundle_hash() and trusted["pin"] is True
        assert page.evaluate("loadPairing().then((r) => r.signKey)") == fe.bundle.public
        assert page.evaluate("S.me.card_actions") is False
        page.evaluate("location.hash = '#/dm'")  # Action requests: the request opens in place
        page.locator(".req:has-text('Send this draft?') .req-head").click()
        expect(page.locator(".card button.primary")).to_be_disabled()
        expect(page.locator(".card .card-note")).to_contain_text("does not press card buttons")
        # Settings offers no pairing off the owner's own web chat, and no Models; Notifications stay.
        page.click("#open-settings")
        expect(page.locator("#go-notify")).to_be_visible()
        expect(page.locator("#pair-phone")).to_be_hidden()
        expect(page.locator("#go-routing")).to_be_hidden()
        expect(page.locator("#settings-conn")).to_have_text(re.compile(r"^Connected away from home, through \S+\.$"))
        page.click("#settings-close")

        # The owner allows card buttons for this device.
        row.locator("input[type=checkbox]").check()
        wait_for(lambda: fe.relay.devices()[0]["cards_allowed"], 10)
        page.reload()
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=20000)
        assert page.evaluate("S.me.card_actions") is True
        page.evaluate("location.hash = '#/dm'")
        head = page.locator(".req:has-text('Send this draft?') .req-head")
        if head.get_attribute("aria-expanded") == "true":
            head.click()  # folded and opened again, the card draws its buttons as this device may press them
        head.click()
        page.locator(".req .card button.primary").click()
        wait_for(lambda: any(getattr(m, "action_id", "") == "approve_send" for m in got), 10)
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_relay_origin_phone_refuses_a_copy_the_bot_did_not_sign(browser, relay_bot_changed):
    """The relay serves a copy of the app whose app.js the bot did not sign:
    once the phone has the bot's manifest, its service worker refuses the
    copy and drops it, and the phone attests nothing."""
    from playwright.sync_api import expect
    fe = relay_bot_changed["fe"]
    contexts = []
    try:
        page, errors, device = _relay_origin_phone(browser, relay_bot_changed, contexts)
        expect(page.locator("h1")).to_have_text("Tacitus stopped", timeout=20000)
        expect(page.locator("body")).to_contain_text("app.js")
        assert page.evaluate("caches.keys()") == ["tacitus-webchat-v3-bundle"]
        (row,) = fe.relay.devices()
        assert row["bundle_verified"] is False and row["cards_allowed"] is False
        assert "attested_bundle" not in fe.relay.store.device_by_id(device)
    finally:
        for c in contexts:
            c.close()


def test_a_relay_origin_phone_takes_a_new_release_from_the_bot_through_the_relay(browser, relay_bot_deploys):
    """A relay-origin phone runs the relay's copy of the app, checked and
    attested; its copy of the page carries the bot's signed policy. The bot
    deploys a release that the relay does not serve: a new bundle.js (the
    worker's code; sw.js unchanged) and app.css. At its next start the
    phone takes the changed files from the bot through the relay, checks
    each, keeps them, says so, and attests the new bundle. The relay's copy,
    a release behind, neither stops the phone nor moves it back. The worker
    runs the new bundle.js from its next start, the copy it kept, never the
    relay's; a kept copy that does not hash to the manifest does not run."""
    from playwright.sync_api import expect
    bot = relay_bot_deploys
    fe, webui, rbase = bot["fe"], bot["webui"], bot["rbase"]
    line = "A new version of Tacitus is ready."
    release = "fetch('app.css').then((r) => r.headers.get('x-tacitus-release'))"
    contexts = []
    try:
        page, errors, device = _relay_origin_phone(browser, bot, contexts)
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=20000)
        first = fe.bundle_hash()
        wait_for(lambda: fe.relay.store.device_by_id(device).get("attested_bundle") == first, 15)
        assert _worker_state(page, "trusted")["hash"] == first
        assert _kept_headers(page, "index.html")["content-security-policy"] == fe.html_csp  # not the relay's
        sw = fe.bundle.files["sw.js"]

        (webui / "bundle.js").write_bytes((webui / "bundle.js").read_bytes() + NEXT_WORKER)
        css = (webui / "app.css").read_bytes() + b"\n/* the next release */\n"
        (webui / "app.css").write_bytes(css)
        _hold_looks(page)
        assert fe.refresh_bundle() != first and fe.bundle.files["sw.js"] == sw
        page.reload()
        expect(page.locator("#app-update")).to_be_visible(timeout=20000)
        new = fe.bundle_hash()
        wait_for(lambda: fe.relay.store.device_by_id(device).get("attested_bundle") == new, 15)
        assert fe.relay.devices()[0]["bundle_verified"] is True
        assert _worker_state(page, "trusted") | {"files": None, "headers": None} == {
            "hash": new, "key": fe.bundle.public, "files": None, "headers": None, "pin": True}
        assert _kept(page, "app.css") == css.decode()
        assert _kept(page, "bundle.js") == (webui / "bundle.js").read_text()

        # The relay still serves its own release; the phone runs the bot's, from its copy.
        import urllib.request
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{rbase}/bundle.js", timeout=5) as r:
            assert r.read() == (WEBUI / "bundle.js").read_bytes()
        page.reload()
        expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
        expect(page.locator("#update-note")).to_be_hidden()
        assert page.evaluate("fetch('app.css').then((r) => r.text())") == css.decode()
        assert _worker_state(page, "trusted")["hash"] == new
        assert fe.relay.devices()[0]["bundle_verified"] is True
        # The worker runs the bundle.js it kept from its next start.
        _restart_worker(page)
        assert page.evaluate(release) == "next"
        # A kept copy that does not hash to the manifest never runs: the loader
        # takes the copy that came with sw.js (the relay's, a release behind).
        page.evaluate("""async () => {
            const c = await caches.open('tacitus-webchat-v3');
            const hit = await c.match('bundle.js');
            await c.put('bundle.js', new Response((await hit.text()) + '\\n// changed\\n', { headers: hit.headers }));
        }""")
        _restart_worker(page)
        assert page.evaluate(release) is None
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_relay_copy_paired_against_another_bundle_stops_before_the_chat(browser, relay_bot):
    """The QR's hash is not the bundle the bot signs (the relay and the bot
    on different releases): once approved, the phone's check of the bot's
    manifest (verifyBundle, bundle.js) refuses it, so the chat never opens
    and the phone attests nothing."""
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    contexts = []
    try:
        page, errors, device = _relay_origin_phone(browser, relay_bot, contexts, bundle="cd" * 32)
        expect(page.locator("h1")).to_have_text("Tacitus stopped", timeout=20000)
        expect(page.locator("body")).to_contain_text("manifest.sig.json")
        expect(page.locator(".chan")).to_have_count(0)
        assert fe.relay.devices()[0]["bundle_verified"] is False
        assert "attested_bundle" not in fe.relay.store.device_by_id(device)
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()
