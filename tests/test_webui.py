"""The web chat's static app (botcore/botcore/chat/webui/).

The files the server serves are there and well formed, the service worker
leaves the API alone, the app never parses text as HTML and fetches
nothing from elsewhere. Where Playwright and a browser are installed, the
app runs against the stub server (tests/webui_stub_server.py): a thread
renders, markup in text stays text, a card's buttons and select post to
the actions route, an event appends a message and marks unread, the
composer and the paperclip post, pairing answers a 401, and the service
worker caches no API response.
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
FILES = ["index.html", "app.js", "app.css", "manifest.webmanifest", "sw.js",
         "icon-192.png", "icon-512.png", "favicon.svg"]
TEXT_FILES = ["index.html", "app.js", "app.css", "manifest.webmanifest", "sw.js"]
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
    assert scripts == ["app.js"]
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
    shell = re.search(r"const SHELL = \[(.*?)\];", sw).group(1)
    assert "api" not in shell
    assert "POST" not in sw and "req.method !== 'GET'" in handler


def test_the_drawer_has_the_notifications_toggle():
    """A checkbox in the channel drawer turns Web Push on; the iOS hint sits
    beside it. A headless browser cannot grant push, so these are file
    checks."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    toggle = re.search(r'<input type="checkbox" id="notify">', nav)
    assert toggle and 'id="notify-hint"' in nav and "Add to Home Screen" in nav
    assert re.search(r'<footer id="drawer-foot" hidden>', nav), "hidden until the server offers push"
    js = read("app.js")
    assert "S.me.push" in js and "navigator.standalone !== false" in js
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
    assert lines <= 1600, f"{lines} lines: the contract keeps the app near 1,500"


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
