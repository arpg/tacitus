"""The web chat's static app (botcore/botcore/chat/webui/).

The files the server serves are there and well formed, the service worker
leaves the API and the relay alone, the app never parses text as HTML and
fetches nothing from elsewhere. Where Playwright and a browser are
installed, the app runs against the stub server (tests/webui_stub_server.py):
a thread renders, markup in text stays text, a card's buttons and select
post to the actions route, an event appends a message and marks unread,
the composer and the paperclip post, pairing answers a 401, and the
service worker caches no API response. The relay's crypto reproduces the
Python vectors (tests/fixtures/relay_vectors.json), the QR encoder draws
codes a decoder reads, and a phone pairs and chats through a real relay
with the real web front end.
"""
import json
import re
import struct
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
WEBUI = TESTS.parent / "botcore" / "botcore" / "chat" / "webui"
FILES = ["index.html", "app.js", "qr.js", "app.css", "manifest.webmanifest", "sw.js",
         "icon-192.png", "icon-512.png", "favicon.svg"]
TEXT_FILES = ["index.html", "app.js", "qr.js", "app.css", "manifest.webmanifest", "sw.js"]
sys.path.insert(0, str(TESTS))


def read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


class Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def refs() -> list[tuple[str, dict]]:
    p = Refs()
    p.feed(read("index.html"))
    return p.tags


# -- the files -------------------------------------------------------------

def test_every_file_is_there():
    missing = [n for n in FILES if not (WEBUI / n).is_file()]
    assert not missing, missing


def test_the_page_loads_its_files_by_relative_path():
    tags = refs()
    scripts = [a.get("src") for t, a in tags if t == "script"]
    links = {a.get("rel"): a.get("href") for t, a in tags if t == "link"}
    assert scripts == ["qr.js", "app.js"]
    assert links["stylesheet"] == "app.css"
    assert links["manifest"] == "manifest.webmanifest"
    assert links["icon"] == "favicon.svg"
    # Under the base path (/chat/) only a relative reference reaches the file.
    for _t, a in tags:
        for key in ("src", "href"):
            if a.get(key):
                assert not re.match(r"^(/|[a-z]+:)", a[key]), a[key]


def test_no_inline_script_or_style():
    html = read("index.html")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html)


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def test_the_manifest_names_its_icons():
    man = json.loads(read("manifest.webmanifest"))
    assert man["name"] and man["short_name"]
    assert man["start_url"] == "./" and man["scope"] == "./"
    assert man["display"] == "standalone"
    sizes = set()
    for icon in man["icons"]:
        assert (WEBUI / icon["src"]).is_file(), icon["src"]
        if icon["type"] == "image/png":
            w, h = _png_size(WEBUI / icon["src"])
            assert icon["sizes"] == f"{w}x{h}"
            sizes.add(w)
    assert {192, 512} <= sizes


def test_the_service_worker_never_caches_the_api():
    sw = read("sw.js")
    handler = sw[sw.index("addEventListener('fetch'"):]
    guard = re.search(r"if \(url\.pathname\.includes\('/api/'\)\) return;", handler)
    assert guard, "the fetch handler must hand every /api/ request back to the browser"
    assert guard.start() < handler.index("respondWith")
    relay = re.search(r"if \(RELAY_ROUTE\.test\(url\.pathname\) \|\| url\.pathname === '/healthz'\) return;",
                      handler)
    assert relay and relay.start() < handler.index("respondWith"), "the relay's routes stay uncached"
    assert re.search(r"const RELAY_ROUTE = /\\/r\\/\[a-z2-7\]\{26\}\\//;", sw)
    shell = re.search(r"const SHELL = \[(.*?)\];", sw).group(1)
    assert "api" not in shell and "'qr.js'" in shell
    assert "POST" not in sw and "req.method !== 'GET'" in handler


def test_the_drawer_has_the_notifications_toggle():
    """A checkbox in the channel drawer turns Web Push on; the iOS hint sits
    beside it. A headless browser cannot grant push, so these are file
    checks."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    toggle = re.search(r'<label class="toggle" id="notify-row" hidden><input type="checkbox" id="notify">', nav)
    assert toggle and 'id="notify-hint"' in nav and "Add to Home Screen" in nav
    assert re.search(r'<footer id="drawer-foot" hidden>', nav), "hidden until the server offers push"
    js = read("app.js")
    assert "S.me.push" in js and "navigator.standalone !== false" in js
    assert "$('notify-row').hidden = false" in js
    assert "call('GET', '/push/vapid')" in js
    assert re.search(r"pushManager\.subscribe\(\{ userVisibleOnly: true, applicationServerKey: keyBytes\(key\) \}\)", js)
    assert "call('POST', '/push/subscribe', sub.toJSON())" in js
    assert "call('POST', '/push/unsubscribe', { endpoint: sub.endpoint })" in js


def _handler(sw: str, event: str) -> str:
    """The source of one service worker listener, up to the next one."""
    start = sw.index(f"addEventListener('{event}'")
    nxt = sw.find("self.addEventListener(", start + 1)
    return sw[start:nxt if nxt > 0 else len(sw)]


def test_the_service_worker_shows_pushes_and_opens_only_this_app():
    sw = read("sw.js")
    push = _handler(sw, "push")
    assert "event.waitUntil(self.registration.showNotification(" in push
    for key in ("body:", "icon:", "tag:", "data: { url:"):
        assert key in push, key
    click = _handler(sw, "notificationclick")
    assert "event.notification.close()" in click
    assert "clients.matchAll" in click and "openWindow(url)" in click and ".focus()" in click
    assert "appUrl(event.notification.data" in click
    app_url = sw[sw.index("function appUrl"):sw.index("self.addEventListener('notificationclick'")]
    assert "url.origin === scope.origin" in app_url and "startsWith(scope.pathname)" in app_url
    # a push never reads or writes the cache, and the fetch handler is unchanged
    assert "caches" not in push and "caches" not in click and "caches" not in app_url
    assert sw.index("addEventListener('push'") < sw.index("addEventListener('fetch'")


def test_the_app_never_parses_text_as_html():
    js = read("app.js")
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                 "createContextualFragment", "eval(", "new Function", "srcdoc"):
        assert word not in js, word


def test_nothing_comes_from_elsewhere():
    for name in ("index.html", "app.css", "sw.js", "manifest.webmanifest"):
        assert not re.search(r"https?://", read(name)), name
    css = read("app.css")
    assert "@import" not in css and "url(" not in css
    # The app's only URLs are the patterns that recognise links in text.
    assert not re.search(r"""["'`]https?://""", read("app.js"))


def test_the_app_stays_small():
    lines = sum(len(read(n).splitlines()) for n in TEXT_FILES)
    # 1,600 for the chat, and about 900 more for qr.js and the relay transport (docs/webchat-relay.md).
    assert lines <= 2500, f"{lines} lines: the contract keeps the app near 2,400 with the relay"


# -- in a browser, against the stub ---------------------------------------

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:  # noqa: BLE001 - no driver on this machine
        pytest.skip(f"playwright cannot start: {e}")
    try:
        b = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001 - no browser installed
        pw.stop()
        pytest.skip(f"no chromium for playwright: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def stub_factory():
    import webui_stub_server as W
    made = []

    def make(**kw):
        server, stub = W.serve(0, **kw)
        made.append((server, stub))
        return f"http://127.0.0.1:{server.server_address[1]}/chat/", stub
    yield make
    for server, stub in made:
        W.stop(server, stub)


@pytest.fixture
def app(browser, stub_factory):
    """(page, stub, errors) for the app on a fresh stub; errors collects
    page errors and console errors (a CSP refusal among them)."""
    contexts = []

    def open_app(viewport=None, **kw):
        url, stub = stub_factory(**kw)
        ctx = browser.new_context(viewport=viewport or {"width": 1280, "height": 800})
        contexts.append(ctx)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto(url)
        return page, stub, errors
    yield open_app
    for ctx in contexts:
        ctx.close()


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


def open_thread(page, title):
    page.click(f".thread-item:has-text({json.dumps(title)})")
    page.wait_for_selector("#messages .msg")


def test_a_thread_renders_and_markup_in_text_stays_text(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    expect(page.locator(".chan.active .c-name")).to_have_text("Direct messages")
    open_thread(page, "Draft a reply")
    expect(page.locator("#messages .msg")).to_have_count(3)
    expect(page.locator(".msg.own .chip")).to_have_count(2)
    expect(page.locator(".text strong").first).to_have_text("To:")
    expect(page.locator('.text a[href="https://example.org/guide"]')).to_have_text("the guide")

    open_thread(page, "Markup check")
    expect(page.locator(".text table td").first).to_have_text("Trip")
    expect(page.locator(".text").first).to_contain_text('<img src=x onerror="window.__xss=1">')
    assert page.locator(".text img, .text script, a[href^='javascript']").count() == 0
    assert page.evaluate("window.__xss") is None
    status = page.locator(".card.status")
    expect(status).to_contain_text("Sent to someone@example.org.")
    assert status.locator("button, select").count() == 0
    assert not errors, errors


def test_the_renderers_build_elements_and_keep_text_as_text(app):
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")

    def md(text):
        return page.evaluate(
            "(s) => { const d = document.createElement('div'); d.append(markdown(s)); return d.innerHTML; }", text)

    def card(text):
        return page.evaluate("(s) => cardText(s).innerHTML", text)

    assert md("**b** *i* ~~s~~ `c`") == "<p><strong>b</strong> <em>i</em> <s>s</s> <code>c</code></p>"
    assert md("<b>x</b> & y") == "<p>&lt;b&gt;x&lt;/b&gt; &amp; y</p>"
    assert md("[x](javascript:void) [y](https://e.example/a)") == (
        '<p>[x](javascript:void) <a href="https://e.example/a" target="_blank" '
        'rel="noopener noreferrer">y</a></p>')
    assert md("- a\n- b\n\n1. c") == "<ul><li>a</li><li>b</li></ul><ol><li>c</li></ol>"
    assert md("> q\n\n```\n<i>\n```") == "<blockquote><p>q</p></blockquote><pre><code>&lt;i&gt;</code></pre>"
    assert md("one\ntwo") == "<p>one<br>two</p>"
    # A time is not a shortcode; a known shortcode after it still is.
    out = md("at 10:30:00 :lock:")
    assert out.startswith("<p>at 10:30:00 <span") and "\U0001f512" in out
    assert md(":no_such_name:") == "<p>:no_such_name:</p>"
    # Card text is Slack mrkdwn: *bold*, and a labelled link shows its target.
    assert card("*Confirm* <https://e.example|here>") == (
        '<strong>Confirm</strong> here (<a href="https://e.example" target="_blank" '
        'rel="noopener noreferrer">https://e.example</a>)')
    assert card("\ue000*x* <b>\ue001") == '<code class="inert">*x* &lt;b&gt;</code>'
    assert page.evaluate("clean('a\u202eb\u200bc')") == "a[U+202E]b[U+200B]c"
    family = "\U0001f468\u200d\U0001f469"
    assert page.evaluate("(s) => clean(s)", family) == family
    assert page.evaluate("oneLine('x'.repeat(400), 300)").endswith("[cut: 132 more characters]")
    assert not errors, errors


def test_card_buttons_post_to_the_actions_route(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    card = next(m for m in stub.messages if m["kind"] == "card" and m["card"]["kind"] == "send")
    open_thread(page, "Draft a reply")
    box = page.locator(".card").first
    expect(box.locator(".card-title")).to_contain_text("Confirm send")
    expect(box.locator(".card-fields dd").nth(1)).to_have_text("Re: Budget question [U+202E]evil")
    expect(box.locator(".card-body").first).to_contain_text("<b>not bold</b>")
    assert box.locator("details.card-quoted").get_attribute("open") is None
    expect(box.locator("button.primary")).to_have_text("Send")
    expect(box.locator("button.plain")).to_have_text("Cancel")
    box.locator("button.primary").click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": card["channel"], "id": card["id"],
                            "action_id": "send", "value": {"draft": "d1"}}]
    # The bot answers with a status line, which replaces the card.
    expect(page.locator(".card.status:has-text('Done: send')")).to_be_visible()
    assert page.locator("#messages button").count() == 0
    assert not errors, errors


def test_a_select_posts_the_option_chosen(app):
    page, stub, errors = app()
    open_thread(page, "Open a room")
    page.select_option(".card-actions select", "p2")
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["action_id"] == "pick_project"
    assert stub.clicks[0]["selected"] == "p2"
    assert not errors, errors


def test_an_event_appends_a_message_and_marks_unread(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    expect(page.locator("#live.on")).to_be_attached()
    tk = stub.messages[0]["thread_key"]
    stub.emit_bot(W.DM, "Live **hello**", thread_key=tk)
    expect(page.locator("#messages .msg strong:has-text('hello')")).to_be_visible()
    expect(page.locator("#messages .msg")).to_have_count(4)

    main = page.locator('a.chan[href="#/stream%3A9"] .badge')
    expect(main).to_have_text("2")
    stub.emit_bot(W.MAIN, "Elsewhere", thread_key=W.WEEKLY)
    expect(main).to_have_text("3")
    expect(page).to_have_title("(3) Tacitus")

    stub.emit_bot(W.DM, "A brand new thread")
    item = page.locator(".thread-item:has-text('A brand new thread')")
    expect(item.locator(".badge")).to_have_text("1")
    item.click()
    expect(page.locator("#messages .msg")).to_have_count(1)
    expect(item.locator(".badge")).to_have_count(0)
    assert not errors, errors


def test_a_dropped_stream_resumes_from_the_last_event(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    expect(page.locator("#live.on")).to_be_attached()
    tk = stub.messages[0]["thread_key"]
    stub.emit_bot(W.DM, "Before the drop", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('Before the drop')")).to_be_visible()
    # The browser reconnects by itself and names the last event it saw.
    seen = str(stub.eseq)
    stub.drop_streams()
    stub.emit_bot(W.DM, "While away", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('While away')")).to_be_visible()
    assert any(s["last_event_id"] == seen for s in stub.streams), stub.streams
    # A refused reconnect closes the stream; the app opens a new one with ?since=.
    seen = str(stub.eseq)
    stub.drop_streams(refuse=1)
    stub.emit_bot(W.DM, "After a refusal", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('After a refusal')")).to_be_visible(timeout=10000)
    assert any(s["since"] == seen for s in stub.streams), stub.streams
    expect(page.locator("#messages .msg")).to_have_count(6)
    assert not [e for e in errors if "EventSource" not in e and "503" not in e], errors


def test_keys_with_reserved_characters_reach_the_api(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.click('a.chan[href="#/stream%3A9"]')
    open_thread(page, "Weekly plan")
    expect(page.locator("#messages .msg")).to_have_count(3)
    expect(page.locator("#messages h3")).to_have_text("Monday")
    assert "stream%3A9%3Atopic%3Aweekly%2520plan" in page.url
    assert not errors, errors


def test_the_composer_sends_on_enter_and_starts_threads(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    tk = stub.messages[0]["thread_key"]
    page.fill("#text", "a")
    page.press("#text", "Shift+Enter")
    page.type("#text", "b")
    assert page.input_value("#text") == "a\nb"
    assert not stub.posts
    page.fill("#text", "Hello there")
    page.press("#text", "Enter")
    wait_for(lambda: stub.posts)
    assert stub.posts[0]["text"] == "Hello there" and stub.posts[0]["thread_key"] == tk
    expect(page.locator("#messages .msg:has-text('Stub reply to: Hello there')")).to_be_visible()
    own = page.locator("#messages .msg.own:has-text('Hello there')")
    expect(own.locator(".chip")).to_have_attribute("title", ":white_check_mark:")
    assert page.input_value("#text") == ""

    page.click("#new-thread")
    page.fill("#text", "Start something")
    page.press("#text", "Enter")
    wait_for(lambda: len(stub.posts) == 2)
    assert stub.posts[1]["thread_key"] is None
    new_key = next(m for m in stub.messages if m["text"] == "Start something")["thread_key"]
    expect(page).to_have_url(re.compile(re.escape(new_key) + "$"))
    expect(page.locator(".thread-item.active")).to_contain_text("Start something")
    assert not errors, errors


def test_the_paperclip_uploads_and_files_render(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Here is the figure")
    img = page.locator(".file img").first
    expect(img).to_have_attribute("src", "/chat/api/files/f1")
    wait_for(lambda: page.evaluate("document.querySelector('.file img').naturalWidth") > 0)
    expect(page.locator(".file-link:has-text('notes.pdf') .size")).to_have_text("17 B")

    page.fill("#text", "The chart")
    page.set_input_files("#file", str(WEBUI / "icon-512.png"))
    wait_for(lambda: stub.posts)
    assert stub.posts[0]["text"] == "The chart" and stub.posts[0]["file"]["name"] == "icon-512.png"
    expect(page.locator(".msg.own .file img")).to_have_count(2)
    assert not errors, errors


def test_a_401_shows_the_pairing_form(app):
    from playwright.sync_api import expect
    page, stub, errors = app(pair_token="made-up-token")
    expect(page.locator("#pair")).to_be_visible()
    page.fill("#pair-token", "wrong")
    page.click("#pair-form button")
    expect(page.locator("#pair-error")).to_have_text("That token does not match.")
    page.fill("#pair-token", "made-up-token")
    page.click("#pair-form button")
    expect(page.locator("#pair")).to_be_hidden()
    expect(page.locator(".thread-item").first).to_be_visible()
    # The browser logs each refused request; nothing else may go wrong.
    assert not [e for e in errors if "401" not in e], errors


def test_the_phone_layout_shows_one_pane(app):
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 390, "height": 844})
    expect(page.locator("#threads")).to_be_visible()
    expect(page.locator("#thread")).to_be_hidden()
    open_thread(page, "Draft a reply")
    expect(page.locator("#threads")).to_be_hidden()
    page.click("#back")
    expect(page.locator("#threads")).to_be_visible()
    page.click("#menu")
    expect(page.locator("#channels")).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert not errors, errors


def test_the_service_worker_caches_no_api_response(app):
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    page.reload()
    open_thread(page, "Draft a reply")
    open_thread(page, "Here is the figure")
    cached = page.evaluate("""async () => {
        const urls = [];
        for (const key of await caches.keys()) {
            const cache = await caches.open(key);
            for (const req of await cache.keys()) urls.push(req.url);
        }
        return urls;
    }""")
    assert any(u.endswith("/chat/app.js") for u in cached), cached
    assert not [u for u in cached if "/api/" in u], cached
    assert not errors, errors


# -- the relay (docs/webchat-relay.md) -----------------------------------

ROOT = TESTS.parent
VECTORS = TESTS / "fixtures" / "relay_vectors.json"


def test_the_relay_keys_stay_in_webcrypto_and_indexeddb():
    js = read("app.js")
    assert "localStorage" not in js and "sessionStorage" not in js
    assert "indexedDB.open('tacitus-relay', 1)" in js
    # the device's private key is made non-extractable and never exported
    assert "generateKey({ name: 'X25519' }, false, ['deriveBits'])" in js
    assert "exportKey('raw', pair.publicKey)" in js and "exportKey('pkcs8'" not in js
    # the pairing secret leaves the address bar before anything else runs
    body = js[js.index("async function pairFromLink"):]
    assert body.split("\n")[1].strip() == "history.replaceState(null, '', location.pathname + location.search);"
    # an err frame is a hint: nothing deletes or clears the stored pairing
    for word in (".delete(", ".clear(", "deleteDatabase"):
        assert word not in js[js.index("function idb"):js.index("function deviceName")], word
    assert "st.delete" not in js and "deleteDatabase" not in js


def test_the_relay_key_never_rides_in_a_url():
    js = read("app.js")
    transport = js[js.index("function relayTransport"):js.index("async function pickTransport")]
    assert "'X-Relay-Key': rec.clientKey" in transport
    assert "/sse?t=${enc(ticket)}" in transport
    assert "key=" not in transport and "?f=" not in transport
    assert transport.count("credentials: 'omit'") == 2


def test_the_pairing_dialog_is_in_the_drawer():
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    assert '<button type="button" id="pair-phone" class="btn small" hidden>Pair a phone</button>' in nav
    assert '<dialog id="phone"' in html and '<canvas id="phone-qr"' in html
    js = read("app.js")
    assert "call('POST', '/relay/pair', {})" in js and "QR.draw($('phone-qr'), out.qr, 4)" in js
    assert "Boolean(S.me.relay) && !T.relay" in js  # on the tailnet only


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
        const [meta, body] = unpack(await openFrame(s, v.res.frame));
        out.res = { meta, body: b64e(body) };
        out.sse = unpack(await openFrame(s, v.sse.frame))[0];
        try { await openFrame(s, v.res.frame); out.replay = 'opened'; } catch (e) { out.replay = e.message; }
        const bent = { ...v.sse.frame, id: 'another' };
        try { await openFrame({ ...s, seen: new Set(), top: -1 }, bent); out.moved = 'opened'; } catch (e) { out.moved = e.name; }
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
    assert out["plain"] == v["plain_hello"]["frame"]
    assert not errors, errors


# Texts and the sha256 of each code's rows ("0"/"1", one row per line), as
# qr.js draws them. Each matched an independent encoder module for module
# (same mask) when pinned; versions 2, 7 (version information), 17 (the size
# of a pairing link) and 21.
QR_BASE = "https://host.example.test/chat/#tr1=" + "abcdefghijklmnopqrstuvwxyz0123456789-_" * 30
QR_PINNED = {
    20: ("4be9aa8f8e1321eecfe7179f523e9698365caa11ecc03bf98490d9106827193b", True),
    120: ("c3a6af83ee8ed179950024eafdf0f5322aea3e66580f20acd0cc95157a2be475", True),
    460: ("3e2c2f45d89bf51d44cb1a20b00cf48446ebe49148f275cc38c70bcf83c27349", False),
    700: ("ceb0b5e0444cb4410746dcac8d79256ee1eb57e4661c506ec4414897e1253ebc", True),
}


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


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay_bot(tmp_path):
    """A relay server (serving the app on its own origin too) and the real
    web front end, whose relay block names it."""
    pytest.importorskip("cryptography")
    sys.path.insert(0, str(ROOT / "relay"))
    sys.path.insert(0, str(ROOT / "botcore"))
    import threading
    import relay_server as rs
    from botcore.chat.web_frontend import WebFrontend
    relay = rs.Relay(rs.Limits(keepalive=0.5, client_timeout=15), log=lambda line: None)
    rsrv = rs.RelayHTTPServer(("127.0.0.1", 0), relay, str(WEBUI))
    threading.Thread(target=rsrv.serve_forever, daemon=True).start()
    rbase = f"http://127.0.0.1:{rsrv.server_address[1]}"
    port = _free_port()
    public = f"http://127.0.0.1:{port}/chat"
    fe = WebFrontend({"port": port, "public_url": public, "relay": {"url": rbase, "wait": 1}},
                     store_path=tmp_path / "priv" / "webchat.sqlite", webui_dir=WEBUI,
                     trusted_config=True)
    fe.poll_s = fe.pair_delay_s = 0.05
    got = []
    fe.registry.event("message")(got.append)
    fe.registry.action("approve_send")(got.append)
    fe.start()
    wait_for(lambda: fe.relay is not None and fe.relay.connected, 10)
    yield {"fe": fe, "relay": relay, "rbase": rbase, "public": public, "got": got}
    fe.stop()
    rsrv.shutdown()
    rsrv.server_close()


def _send_card(fe):
    from botcore.chat.cards import CardAction, ConfirmationCard
    card = ConfirmationCard(kind="send", title=":lock: Send this draft?", body="Hello there.",
                            actions=[CardAction("approve_send", "Send", {"t": 1}, "primary", "send"),
                                     CardAction("cancel_send", "Cancel", {"t": 1}, "", "cancel")])
    return fe.post_card("dm", card)


def _phone_page(browser, link, contexts, off_tailnet=False, **ctx_kw):
    """A page that opens a pairing link; (context, page, errors, urls asked).
    `off_tailnet` refuses the bot's own API, as a phone off the tailnet
    finds it, while the page itself still loads."""
    ctx = browser.new_context(**ctx_kw)
    contexts.append(ctx)
    if off_tailnet:
        ctx.route(re.compile(r".*/chat/api/.*"), lambda route: route.abort())
    page = ctx.new_page()
    errors, asked = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: asked.append(r.url))
    page.goto(link)
    return ctx, page, errors, asked


def test_a_phone_pairs_on_the_tailnet_and_chats_through_the_relay(browser, relay_bot):
    """The owner opens "Pair a phone" on the tailnet origin; a phone opens
    the link on that origin with the API out of reach (off the tailnet),
    pairs, and reads, writes and confirms a card through the relay."""
    from playwright.sync_api import expect
    fe, got = relay_bot["fe"], relay_bot["got"]
    contexts = []
    try:
        owner = browser.new_context()
        contexts.append(owner)
        owner.add_cookies([{"name": "ea_web", "value": fe.pair_token(), "domain": "127.0.0.1",
                            "path": "/chat", "httpOnly": True, "sameSite": "Strict"}])
        tail = owner.new_page()
        tail.goto(relay_bot["public"] + "/")
        tail.click("#pair-phone")
        expect(tail.locator("#phone-devices")).to_contain_text("No phone is paired yet.")
        wait_for(lambda: tail.input_value("#phone-link"), 10)
        link = tail.input_value("#phone-link")
        assert link.startswith(relay_bot["public"] + "/#tr1=")
        # The canvas holds the code of exactly that link, module for module.
        wrong = tail.evaluate("""(link) => {
            const grid = QR.encode(link);
            const g = $('phone-qr').getContext('2d');
            let bad = 0;
            grid.forEach((row, y) => row.forEach((on, x) => {
                const px = g.getImageData((x + 4) * 4 + 2, (y + 4) * 4 + 2, 1, 1).data[0];
                if ((px < 128) !== on) bad++;
            }));
            return bad;
        }""", link)
        assert wrong == 0

        ctx, page, errors, asked = _phone_page(browser, link, contexts, off_tailnet=True,
                                               service_workers="block")
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
        assert "tr1=" not in page.url
        expect(page.locator("#live.on")).to_be_attached(timeout=10000)
        devices = fe.relay.devices()
        assert [d["origin"] for d in devices] == ["own"]
        stored = page.evaluate("""async () => { const r = await loadPairing();
            return { type: r.priv.type, extractable: r.priv.extractable, device: r.device,
                     local: localStorage.length }; }""")
        assert stored == {"type": "private", "extractable": False, "device": devices[0]["id"],
                          "local": 0}

        fe.post("dm", "Hello through the relay")
        item = page.locator(".thread-item:has-text('Hello through the relay')")
        expect(item).to_be_visible(timeout=10000)
        item.click()
        page.fill("#text", "Answer from the phone")
        page.press("#text", "Enter")
        wait_for(lambda: any(getattr(m, "text", "") == "Answer from the phone" for m in got), 10)

        _send_card(fe)
        card = page.locator(".thread-item:has-text('Send this draft?')")
        expect(card).to_be_visible(timeout=10000)
        card.click()
        page.locator(".card button.primary").click()
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
        expect(page.locator("#relay-note")).to_contain_text("did not accept this phone", timeout=15000)
        assert page.evaluate("loadPairing().then((r) => Boolean(r && r.priv))")
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def test_a_phone_paired_on_the_relays_origin_cannot_press_card_buttons(browser, relay_bot):
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    contexts = []
    try:
        link = fe.relay.new_pairing(app_url=relay_bot["rbase"] + "/")
        assert link.startswith(relay_bot["rbase"] + "/#tr1=")
        _send_card(fe)
        ctx, page, errors, asked = _phone_page(browser, link, contexts)
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
        assert [d["origin"] for d in fe.relay.devices()] == ["relay"]
        assert page.evaluate("S.me.card_actions") is False
        page.locator(".thread-item:has-text('Send this draft?')").click()
        expect(page.locator(".card button.primary")).to_be_disabled()
        expect(page.locator(".card .card-note")).to_contain_text("Approve this on the tailnet")
        # The drawer offers no pairing off the tailnet.
        expect(page.locator("#pair-phone")).to_be_hidden()
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def _link_payload(**over) -> str:
    import base64
    obj = {"v": 1, "r": "https://relay.example.net", "i": "b" * 26,
           "k": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode(), "c": "made-up-key",
           "p": "0" * 32, "s": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode(),
           "x": int(time.time()) + 600}
    obj.update(over)
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_a_bad_pairing_link_is_refused_and_leaves_the_address_bar(browser, stub_factory):
    """On the tailnet the bot names its install: a link for another one is
    refused, as is an expired one; the fragment goes either way, and no
    pairing is kept."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    url, stub = stub_factory()
    for payload, says in ((_link_payload(), "another install"),
                          (_link_payload(i=W.STUB_INSTALL, x=int(time.time()) - 1), "expired"),
                          ("bm90LWpzb24", "Pairing failed")):
        ctx = browser.new_context()
        try:
            page = ctx.new_page()
            page.goto(url + "#tr1=" + payload)
            expect(page.locator("#toast")).to_contain_text(says)
            assert "tr1=" not in page.url
            assert page.evaluate("loadPairing().then((r) => r === undefined)")
            expect(page.locator(".thread-item").first).to_be_visible()  # the chat still starts
        finally:
            ctx.close()
