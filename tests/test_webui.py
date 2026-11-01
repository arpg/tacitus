"""The web chat's static app (botcore/botcore/chat/webui/).

The files the server serves are there and well formed, the service worker
leaves the API and the relay alone, the app never parses text as HTML and
fetches nothing from elsewhere, and its colours meet WCAG AA in both
themes. Where Playwright and a browser are installed, the app runs against
the stub server (tests/webui_stub_server.py):
a thread renders, markup in text stays text, a card's buttons and select
post to the actions route, an event appends a message and marks unread,
the composer and the paperclip post, pairing answers a 401, on a phone
every control is 44px high and nothing scrolls sideways, and the
service worker caches no API response. The relay's crypto reproduces the
Python vectors (tests/fixtures/relay_vectors.json), the QR encoder draws
codes a decoder reads, and a phone pairs and chats through a real relay
with the real web front end. The app checks its code against the bot's
signed manifest (bundle.js): the service worker serves the signed files
and refuses a changed one, a copy of the app from the relay attests
its check, while only the owner lets it press card buttons, and a phone
off the tailnet takes a new release from the bot through the relay. Each
thread card carries its state's sticker, Needs you gathers the threads
that wait on the owner, the chips filter a channel, and the owner's
override posts and follows the bot's `thread_state` events. The app
teaches itself: a welcome shown once per browser (and again from the
drawer) whose examples fill the composer, a New room dialog that asks the
bot in the EA channel, and empty lists that say what each place is for.
"""
import json
import re
import struct
import sys
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
WEBUI = TESTS.parent / "botcore" / "botcore" / "chat" / "webui"
FILES = ["index.html", "app.js", "qr.js", "bundle.js", "app.css", "manifest.webmanifest", "sw.js",
         "icon-180.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "favicon.svg"]
TEXT_FILES = ["index.html", "app.js", "qr.js", "bundle.js", "app.css", "manifest.webmanifest", "sw.js"]
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
    assert scripts == ["bundle.js", "qr.js", "app.js"]
    assert links["stylesheet"] == "app.css"
    assert links["manifest"] == "manifest.webmanifest"
    assert links["icon"] == "favicon.svg"
    assert links["apple-touch-icon"] == "icon-180.png"
    # The stylesheet comes first, so the first paint already has the app's background.
    assert [a.get("rel") for t, a in tags if t in ("link", "script")][0] == "stylesheet"
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


def _png(path: Path) -> tuple[int, int, int, list[bytes], list[bytes]]:
    """(width, height, colour type, chunk types, pixel rows) of an 8-bit,
    non-interlaced PNG, decoded with the standard library."""
    data = path.read_bytes()
    pos, idat, chunks, ihdr = 8, b"", [], ()
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunks.append(kind)
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data[pos + 8:pos + 21])
        elif kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    assert depth == 8 and interlace == 0 and ctype in (2, 6)
    bpp = 3 if ctype == 2 else 4
    raw, stride, rows, prev = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp)
    for y in range(h):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a, b = line[i - bpp] if i >= bpp else 0, prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if kind == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                a = a if pa <= pb and pa <= pc else b if pb <= pc else c
            line[i] = (line[i] + (0, a, b, (a + b) // 2, a)[kind]) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, ctype, chunks, rows


def test_the_manifest_names_its_icons():
    man = json.loads(read("manifest.webmanifest"))
    assert man["name"] and man["short_name"]
    # Relative to the manifest, which the page links by relative path: the base path, with its slash.
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
    masks = [i for i in man["icons"] if i["purpose"] == "maskable"]
    assert [(i["src"], i["sizes"]) for i in masks] == [("icon-maskable-512.png", "512x512")]
    # The splash's colour is the dark theme's background; the theme colour, its header.
    css = read("app.css")
    dark = css[css.index("@media (prefers-color-scheme: dark)"):]
    assert f"--bg: {man['background_color']};" in dark and f"--panel: {man['theme_color']};" in dark


def test_the_home_screen_icons_are_opaque_and_the_maskable_one_padded():
    blue = (44, 100, 214)
    w, h, ctype, chunks, rows = _png(WEBUI / "icon-180.png")
    assert (w, h) == (180, 180) and ctype == 2 and b"tRNS" not in chunks  # iOS shows no alpha
    assert tuple(rows[0][:3]) == blue and tuple(rows[-1][-3:]) == blue
    w, h, ctype, chunks, rows = _png(WEBUI / "icon-maskable-512.png")
    assert (w, h) == (512, 512) and ctype == 2 and b"tRNS" not in chunks
    outside, glyph = [], 0
    for y, row in enumerate(rows):
        for x in range(w):
            px = tuple(row[3 * x:3 * x + 3])
            if px == (255, 255, 255):
                glyph += 1
            if (x + .5 - w / 2) ** 2 + (y + .5 - h / 2) ** 2 > (.4 * w) ** 2 and px != blue:
                outside.append((x, y))
    # The glyph sits inside the safe zone, the circle of radius 40% that every mask keeps.
    assert glyph > 10000 and not outside, outside[:5]


def test_the_head_makes_an_installable_app():
    metas = {(a.get("name"), a.get("media")): a.get("content") for t, a in refs() if t == "meta" and a.get("name")}
    viewport = {k.strip() for k in metas[("viewport", None)].split(",")}
    assert {"viewport-fit=cover", "interactive-widget=resizes-content", "initial-scale=1"} <= viewport
    assert not {k for k in viewport if k.startswith(("maximum-scale", "user-scalable"))}, "zoom stays on"
    assert metas[("apple-mobile-web-app-capable", None)] == "yes"
    assert metas[("mobile-web-app-capable", None)] == "yes"
    assert metas[("apple-mobile-web-app-status-bar-style", None)] == "default"
    assert metas[("apple-mobile-web-app-title", None)] == "Tacitus"
    # The browser's bar matches the app's header (--panel) in each theme.
    css = read("app.css")
    light, dark = css[:css.index("@media (prefers-color-scheme: dark)")], css[css.index("@media (prefers-color-scheme: dark)"):]
    assert f"--panel: {metas[('theme-color', '(prefers-color-scheme: light)')]};" in light
    assert f"--panel: {metas[('theme-color', '(prefers-color-scheme: dark)')]};" in dark


def test_the_layout_keeps_clear_of_the_notch_and_the_home_indicator():
    css = read("app.css")

    def rule(selector):  # every block of the selector, the media queries' too
        return " ".join(re.findall(r"(?m)^\s*" + re.escape(selector) + r" \{([^}]*)\}", css))
    assert "env(safe-area-inset-top)" in rule(".bar")  # every header: the drawer's, the lists', the thread's
    assert "env(safe-area-inset-bottom)" in rule("#composer")
    assert "env(safe-area-inset-bottom)" in rule("#drawer-foot")
    assert "env(safe-area-inset-bottom)" in rule("#channel-list, #thread-list")
    assert "env(safe-area-inset-left)" in rule("#channels") and "env(safe-area-inset-right)" in rule("body")
    assert "height: var(--app-h, 100dvh)" in rule("body")
    assert "background: var(--bg)" in rule("html") and "-webkit-tap-highlight-color: transparent" in rule("html")
    assert "overscroll-behavior: none" in rule("html, body")  # no pull-to-refresh, no bounce of the page
    assert "overscroll-behavior: contain" in rule(".scroll")  # the thread pane scrolls itself only


def _theme_colours() -> dict[str, dict[str, str]]:
    """The colour variables of each theme, the dark one over the light."""
    css = read("app.css")
    at = css.index("@media (prefers-color-scheme: dark)")
    light = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", css[:at]))
    dark = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", css[at:css.index("}\n}", at)]))
    return {"light": light, "dark": {**light, **dark}}


def _contrast(a: str, b: str) -> float:
    def lum(c: str) -> float:
        rgb = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in rgb]
        return .2126 * r + .7152 * g + .0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def test_the_colours_meet_wcag_aa_in_both_themes():
    """Text 4.5:1 on every surface it sits on (WCAG AA), the status dots
    3:1 on the panel; the white of a badge and the text of a primary
    danger button are the colours the rules use."""
    css = read("app.css")
    assert re.search(r"\.badge \{[^}]*\bcolor: #fff;", css)
    assert ".btn.primary.danger { background: var(--danger); color: var(--accent-text); }" in css
    assert "#text::placeholder { color: var(--muted); }" in css
    text = [("text", s) for s in ("bg", "panel", "own", "accent-soft", "hover", "code-bg")]
    text += [("muted", s) for s in ("bg", "panel", "own", "accent-soft", "hover", "code-bg")]
    text += [("accent", "bg"), ("accent", "panel"), ("accent", "own"), ("accent-text", "accent"),
             ("accent-text", "danger"), ("danger", "bg"), ("danger", "panel"), ("#ffffff", "badge")]
    # The stickers (Needs you, Waiting, Working and Done), the chosen filter chip and the Needs you count.
    text += [("warn-text", "warn-soft"), ("warn-text", "panel"), ("accent", "accent-soft")]
    for rule in (".st-needs_you { background: var(--accent); color: var(--accent-text);",
                 ".st-waiting { background: var(--warn-soft); color: var(--warn-text); }",
                 "background: var(--hover); color: var(--muted);",  # the sticker's own, for Working and Done
                 ".filter.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent);",
                 ".badge.needs { background: var(--accent); color: var(--accent-text); }"):
        assert rule in css, rule
    for theme, v in _theme_colours().items():
        def ratio(a, b):
            return _contrast(v.get(a, a), v.get(b, b))
        low = [(a, b, round(ratio(a, b), 2)) for a, b in text if ratio(a, b) < 4.5]
        low += [(a, "panel", round(ratio(a, "panel"), 2)) for a in ("ok", "warn") if ratio(a, "panel") < 3]
        assert not low, (theme, low)


def test_the_service_worker_never_caches_the_api():
    sw = read("sw.js")
    handler = sw[sw.index("addEventListener('fetch'"):]
    guard = re.search(r"if \(url\.pathname\.includes\('/api/'\)\) return;", handler)
    assert guard, "the fetch handler must hand every /api/ request back to the browser"
    assert guard.start() < handler.index("respondWith")
    relay = re.search(r"if \(RELAY_ROUTE\.test\(url\.pathname\) \|\| url\.pathname\.endsWith\('/healthz'\)\) return;",
                      handler)
    assert relay and relay.start() < handler.index("respondWith"), "the relay's routes stay uncached"
    assert re.search(r"const RELAY_ROUTE = /\\/r\\/\[a-z2-7\]\{26\}\\//;", sw)
    shell = re.search(r"const SHELL = \[(.*?)\];", sw).group(1)
    assert "api" not in shell and "'qr.js'" in shell
    assert "POST" not in sw and "req.method !== 'GET'" in handler


def test_the_worker_checks_files_against_the_signed_manifest():
    """The worker loads the check the page uses, leaves the signed manifest
    to the browser, and its refusal page runs no script."""
    sw = read("sw.js")
    assert "importScripts('bundle.js');" in sw
    handler = _handler(sw, "fetch")
    assert "url.pathname.endsWith('/manifest.sig.json')) return;" in handler
    assert handler.index("manifest.sig.json") < handler.index("respondWith")
    refusal = sw[sw.index("function refusal"):sw.index("async function adopt")]
    assert "<script" not in refusal and "'Content-Security-Policy': \"default-src 'none'" in refusal
    bundle = read("bundle.js")
    assert "{ name: 'Ed25519' }" in bundle and "Object.assign(self, { BundleError, verifyBundle" in bundle


def test_the_drawer_has_the_notifications_toggle():
    """A checkbox in the channel drawer turns Web Push on; on iOS the install
    line beside it says how notifications come. A headless browser cannot
    grant push, so these are file checks."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    toggle = re.search(r'<label class="toggle" id="notify-row" hidden><input type="checkbox" id="notify">', nav)
    assert toggle and '<div id="install" class="install" hidden>' in nav and "notify-hint" not in html
    assert re.search(r'<footer id="drawer-foot" hidden>', nav), "hidden until it has something to show"
    js = read("app.js")
    assert "S.me.push" in js and "Tap Share, then Add to Home Screen, so notifications work." in js
    assert "$('notify-row').hidden = false" in js
    assert "call('GET', '/push/vapid')" in js
    assert re.search(r"pushManager\.subscribe\(\{ userVisibleOnly: true, applicationServerKey: keyBytes\(key\) \}\)", js)
    assert "call('POST', '/push/subscribe', await subBody(sub))" in js
    # The subscription names this browser's relay device, which takes it along when removed.
    assert "({ ...sub.toJSON(), device: (await loadPairing())?.device || undefined })" in js
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
    # a window navigate() cannot move routes itself from a message (app.js)
    assert ".catch(() => win.postMessage({ open: url }))" in click
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
    # 1,600 for the chat, about 900 for qr.js and the relay transport (docs/webchat-relay.md),
    # 60 for the relay's device approval, the pairing checks and the file rule, 280 for the
    # check of the app's code (bundle.js, the worker's and the page's part), 10 for the
    # card-button choice per relay-origin phone under "Pair a phone", and 70 for the
    # Settings screen (the routing table: a row per task, the floors, Save).
    # 2,940: 2,920 plus the phone dialog's one-tap setup and its plain notes.
    # 3,170: 3,120 plus the look (a radius and shadow scale, bubbles, blurred headers, the send state)
    # and the dual-screen split at the hinge. 3,190: the height pin that lets go when a screen changes size.
    # 3,240: 3,190 plus a new release through the relay (the page fetches it, the worker checks and keeps it).
    # 3,430: 3,240 plus the thread states (stickers, Needs you, the filter chips, the override and long press).
    # 3,630: 3,430 plus the teaching (the welcome's three cards, the examples, New room, the empty lists).
    assert lines <= 3630, f"{lines} lines: the contract keeps the app near 3,630 with the relay, the phone setup, the thread states and the teaching"


# -- in a browser, against the stub ---------------------------------------

# Every context starts as a browser that has closed the welcome once, so it
# covers nothing; `new_context(welcome=True)` leaves it to show.
WELCOMED = "try { localStorage.setItem('tacitus-welcome-seen', '1'); } catch (e) {}"


class _Browser:
    def __init__(self, b):
        self._b = b

    def new_context(self, welcome=False, **kw):
        ctx = self._b.new_context(**kw)
        if not welcome:
            ctx.add_init_script(WELCOMED)
        return ctx

    def __getattr__(self, name):
        return getattr(self._b, name)


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
    yield _Browser(b)
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

    def open_app(viewport=None, init=None, context=None, welcome=False, **kw):
        url, stub = stub_factory(**kw)
        ctx = browser.new_context(welcome=welcome, viewport=viewport or {"width": 1280, "height": 800}, **(context or {}))
        contexts.append(ctx)
        if init:
            ctx.add_init_script(init)
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
    # Zulip mentions and stream links show their names, never the id.
    assert md("@**Jane Doe|8** and @_**Jane Doe|8** in #**ea**") == (
        '<p><span class="mention">@Jane Doe</span> and <span class="mention">@Jane Doe</span> '
        'in <span class="mention">#ea</span></p>')
    assert page.evaluate("plainLine('@**Jane Doe|8** :lock: **done**')") == "@Jane Doe \U0001f512 done"
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


def test_a_screen_that_changes_size_is_not_held_at_the_old_height(app):
    """A foldable opening, or a window resized: the height pin the keyboard
    sets is dropped, and the panes follow the new viewport."""
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 390, "height": 844})
    open_thread(page, "Draft a reply")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == ""
    page.evaluate("document.documentElement.style.setProperty('--app-h', '300px')")  # as a closed screen's keyboard left it
    page.set_viewport_size({"width": 1000, "height": 700})
    expect(page.locator("#threads")).to_be_visible()
    page.wait_for_function("document.documentElement.style.getPropertyValue('--app-h') === ''")
    assert page.evaluate("Math.round(document.body.getBoundingClientRect().height)") == 700
    expect(page.locator("#composer")).to_be_in_viewport()
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
    # The channel with unread messages sits first; the DM, read and open, drops below it.
    expect(page.locator("#channel-list .chan .c-name").first).to_have_text("ea")

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


# The controls a finger presses, and the ones under 44px of the visible ones.
SMALL_TARGETS = """() => [...document.querySelectorAll(
    'button, select, textarea, summary, a.chan, a.thread-item, label.toggle')].filter((el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || getComputedStyle(el).visibility === 'hidden' || r.bottom < 0 || r.top > innerHeight) return false;
  return r.height < 44 || (el.classList.contains('icon') && r.width < 44);
}).map((el) => `${el.tagName} ${el.id || el.className} ${Math.round(el.getBoundingClientRect().height)}`)"""
SIDEWAYS = "[document.documentElement.scrollWidth - innerWidth, $('messages').scrollWidth - $('messages').clientWidth]"


def test_a_phone_gets_44px_targets_and_never_scrolls_sideways(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(viewport={"width": 390, "height": 844},
                             context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    page.wait_for_selector(".thread-item")
    # Closed, the drawer is out of the tab order.
    assert page.evaluate("getComputedStyle($('channels')).visibility") == "hidden"
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#menu")
    expect(page.locator("#channels")).to_be_in_viewport()
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#scrim", position={"x": 370, "y": 400})
    open_thread(page, "Draft a reply")
    page.evaluate("document.querySelector('.card-actions').scrollIntoView({ block: 'center' })")
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#back")
    open_thread(page, "Open a room")
    assert page.evaluate(SMALL_TARGETS) == []
    assert page.evaluate(SIDEWAYS) == [0, 0]
    stub.emit_bot(W.DM, "Wide: https://e.example/" + "a/" * 80 + " " + "x" * 120
                  + "\n\n```\n" + "y = 1  " * 40 + "\n```\n\n| a | b | c |\n|---|---|---|\n"
                  + "| one two three four | five six seven eight | nine ten eleven twelve |")
    page.click("#back")
    open_thread(page, "Wide:")
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


# -- the state of a thread (docs/webchat.md, The web app) -----------------

def _card(page, title):
    return page.locator(f".thread-item:has-text({json.dumps(title)})")


def test_each_card_shows_its_state_as_a_sticker_with_the_reason(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item .sticker")
    for title, state, word in (("Draft a reply", "needs_you", "Needs you"), ("Markup check", "done", "Done"),
                               ("Here is the figure", "working", "Working")):
        card = _card(page, title)
        reason = stub.states[(card.get_attribute("data-channel"), card.get_attribute("data-thread"))]["reason"]
        expect(card.locator(f".sticker.st-{state}")).to_have_text(word)
        expect(card).to_have_attribute("title", reason)
        expect(card.locator(".sticker")).to_have_attribute("aria-label", f"{word}: {reason}")
    # A thread with no state (`none`) has no sticker and no title.
    expect(_card(page, "Open a room").locator(".sticker")).to_have_count(0)
    assert _card(page, "Open a room").get_attribute("title") is None
    # Working pulses, except under reduced motion, which stops every animation.
    css = read("app.css")
    assert "animation: pulse" in css[css.index(".st-working::before"):]
    assert "*, ::before, ::after, ::backdrop { animation: none !important;" in css
    assert not errors, errors


def test_needs_you_gathers_the_threads_of_every_channel(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    count = page.locator("#needs-link #needs-count")
    expect(count).to_have_text("2")  # needs you, in the DM and in the room; the waiting one is not counted
    expect(count).to_have_attribute("aria-label", "2 threads need you")
    expect(page.locator("#channel-list .chan .c-name").first).to_have_text("ea")  # the channels follow it
    page.click("#needs-link")
    expect(page).to_have_url(re.compile("#/needs$"))
    expect(page.locator("#channel-name")).to_have_text("Needs you")
    expect(page.locator("#needs-link")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(".chan.active")).to_have_count(1)  # the entry, no channel
    expect(page.locator("#new-thread")).to_be_hidden()
    items = page.locator("#thread-list .thread-item")
    expect(items).to_have_count(3)
    # Needs you first, newest first, then waiting; each card names its channel.
    assert [c.split(" · ")[0] for c in items.locator(".t-meta").all_inner_texts()] == [
        "ea-example-project", "Direct messages", "ea-scheduling"]
    expect(items.nth(2).locator(".sticker")).to_have_text("Waiting")
    expect(page.locator("#thread-list .filters")).to_have_count(0)
    # A card opens its thread in its channel; the list stays beside it.
    sched = stub.thread(W.SCHED, next(m["thread_key"] for m in stub.messages if m["channel"] == W.SCHED))[0]
    items.nth(2).click()
    expect(page).to_have_url(re.compile(re.escape("#/stream%3A11/" + sched["thread_key"]) + "$"))
    expect(page.locator("#messages .msg")).to_have_count(1)
    expect(page.locator("#thread-list .thread-item.active")).to_contain_text("Finding a time")
    expect(page.locator("#channel-name")).to_have_text("Needs you")
    # A state the bot changes moves the count and the list at once.
    stub.set_state(W.SCHED, sched["thread_key"], "needs_you", reason="The other side picked a time.")
    expect(count).to_have_text("3")
    expect(items.nth(1)).to_contain_text("Finding a time")  # by its last message, between the other two
    expect(_card(page, "Finding a time").locator(".sticker")).to_have_text("Needs you")
    expect(_card(page, "Finding a time")).to_have_attribute("title", "The other side picked a time.")
    # A channel's list leaves it; on a new connection the list is read again.
    page.click('a.chan[href="#/stream%3A9"]')
    expect(page.locator("#channel-name")).to_have_text("ea")
    expect(page.locator(".chan.active .c-name")).to_have_text("ea")
    weekly = next(m for m in stub.messages if m["thread_key"] == W.WEEKLY)
    stub.states[(W.MAIN, W.WEEKLY)] = {"state": "needs_you", "by": "model", "reason": "A question waits.",
                                       "at": weekly["posted_at"]}
    stub.drop_streams()
    expect(count).to_have_text("4", timeout=10000)
    assert not errors, errors


def test_the_filter_chips_narrow_a_channel_and_persist(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    chips = page.locator("#thread-list .filters .filter")
    expect(chips).to_have_text(["All", "Needs you", "Waiting", "Done"])
    expect(page.locator(".filter.on")).to_have_text("All")
    total = page.locator("#thread-list .thread-item").count()
    page.click(".filter[data-filter='needs_you']")
    expect(page.locator("#thread-list .thread-item")).to_have_text([re.compile("Draft a reply")])
    expect(page.locator(".filter[data-filter='needs_you']")).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("localStorage.getItem('tacitus-thread-filter')") == "needs_you"
    page.reload()
    expect(page.locator(".filter.on")).to_have_text("Needs you")
    expect(page.locator("#thread-list .thread-item")).to_have_count(1)
    page.click(".filter[data-filter='done']")
    expect(page.locator("#thread-list .thread-item")).to_have_text([re.compile("Markup check")])
    page.click(".filter[data-filter='waiting']")
    expect(page.locator("#thread-list .thread-item")).to_have_count(0)
    expect(page.locator("#thread-list .empty")).to_have_text("No thread here is waiting.")
    page.click(".filter[data-filter='all']")
    expect(page.locator("#thread-list .thread-item")).to_have_count(total)
    assert not errors, errors


def test_the_override_posts_and_the_sticker_follows_the_event(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    tk = stub.messages[0]["thread_key"]
    btn = page.locator("#thread-state")
    expect(btn).to_be_hidden()  # no thread open
    open_thread(page, "Draft a reply")
    expect(btn).to_have_text("Mark done")
    btn.click()
    wait_for(lambda: stub.state_posts)
    assert stub.state_posts == [{"channel": W.DM, "thread_key": tk, "state": "done"}]
    card = _card(page, "Draft a reply")
    expect(card.locator(".sticker")).to_have_text("Done")
    expect(card).to_have_attribute("title", "You set this.")  # the bot's reason, from its answer and event
    expect(btn).to_have_text("Needs me")
    expect(page.locator("#needs-count")).to_have_text("1")
    # A rule moves it back: the event alone changes the sticker and the button.
    stub.set_state(W.DM, tk, "needs_you", reason="A new message asks for you.")
    expect(card.locator(".sticker.st-needs_you")).to_be_visible()
    expect(card).to_have_attribute("title", "A new message asks for you.")
    expect(btn).to_have_text("Mark done")
    expect(page.locator("#needs-count")).to_have_text("2")
    # Needs me sends needs_you.
    stub.set_state(W.DM, tk, "done", reason="The card was acted on.")
    expect(btn).to_have_text("Needs me")
    btn.click()
    wait_for(lambda: len(stub.state_posts) == 2)
    assert stub.state_posts[1]["state"] == "needs_you"
    expect(card.locator(".sticker.st-needs_you")).to_be_visible()
    assert not errors, errors


def test_a_long_press_on_a_phone_marks_a_thread(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(viewport={"width": 390, "height": 844},
                             context={"has_touch": True, "is_mobile": True})
    card = _card(page, "Here is the figure")
    expect(card.locator(".sticker")).to_have_text("Working")
    # A finger on the card: down, perhaps a move, up after 700 ms, then the
    # tap's click on what is under the finger then (the list may be redrawn).
    press = """([sel, move]) => new Promise((done) => {
        const r = document.querySelector(sel).getBoundingClientRect();
        const [x, y] = [r.left + 20, r.top + 10];
        const at = (type, dx) => document.elementFromPoint(x, y).dispatchEvent(new (type === 'click' ? MouseEvent : PointerEvent)(
            type, { pointerType: 'touch', bubbles: true, cancelable: true, clientX: x + dx, clientY: y }));
        at('pointerdown', 0);
        if (move) at('pointermove', move);
        setTimeout(() => { at('pointerup', move); at('click', move); setTimeout(() => done(location.hash), 50); }, 700);
    })"""
    sel = f".thread-item[data-thread={json.dumps(card.get_attribute('data-thread'))}] .t-title"
    # Moved, it is a scroll: nothing changes, and the tap opens the thread.
    assert page.evaluate(press, [sel, 30]).count("/") == 2
    page.click("#back")
    expect(card).to_be_visible()  # the list is back (the hash's route runs after the click)
    assert not stub.state_posts
    # Held still for 600 ms: the state changes, and the tap that ends the press opens nothing.
    assert page.evaluate(press, [sel, 0]).count("/") == 1
    wait_for(lambda: stub.state_posts)
    assert stub.state_posts[0]["state"] == "done" and stub.state_posts[0]["channel"] == W.DM
    expect(card.locator(".sticker")).to_have_text("Done")
    css = read("app.css")
    assert "-webkit-touch-callout: none; -webkit-user-select: none; user-select: none;" in css
    assert not errors, errors


def test_needs_you_on_a_phone_is_one_pane_with_back(app):
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 390, "height": 844},
                             context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    page.click("#needs-link")
    expect(page).to_have_url(re.compile("#/needs$"))
    expect(page.locator("body")).not_to_have_class(re.compile("drawer"))
    assert page.evaluate("document.body.dataset.view") == "threads"
    expect(page.locator("#threads")).to_be_visible()
    expect(page.locator("#thread")).to_be_hidden()
    assert page.evaluate(SMALL_TARGETS) == []
    page.locator("#thread-list .thread-item").first.click()
    expect(page.locator("#thread")).to_be_visible()
    expect(page.locator("#threads")).to_be_hidden()
    expect(page.locator("#thread-state")).to_be_visible()
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#back")
    expect(page).to_have_url(re.compile("#/needs$"))
    expect(page.locator("#threads")).to_be_visible()
    expect(page.locator("#thread-list .t-chan").first).to_be_visible()
    page.go_back()  # the browser's own back returns to the thread, and back again to the list
    expect(page.locator("#thread")).to_be_visible()
    page.go_back()
    expect(page).to_have_url(re.compile("#/needs$"))
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


# -- teaching: the welcome, the examples, New room, empty lists ------------

PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3}}
SIZES = pytest.mark.parametrize("size", ["phone", "desktop"])
# Words a first-day owner does not use: the app says room, conversation, card.
JARGON = re.compile(r"\b(channel|thread|session|context|specialist)s?\b", re.I)
# The drawer foot's entries on show.
FOOT = "[...$('drawer-foot').children].filter((el) => !el.hidden).map((el) => el.id)"


def _drawer(page, size):
    if size == "phone":
        page.click("#menu")


def _visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def test_the_teaching_copy_is_plain():
    """What the welcome, New room and the empty lists say passes the gym
    coach on a phone: no jargon, and no welcome card is about rooms."""
    html = read("index.html")

    def part(start):
        at = html.index(start)
        return html[at:html.index("</dialog>", at)]
    welcome, room = part('<dialog id="welcome"'), part('<dialog id="room"')
    js = read("app.js")
    teach = js[js.index("/* -- Teaching"):js.index("/* -- Code integrity")]
    said = [t for t in re.findall(r"'([^'\n]*)'", teach) if " " in t]
    for text in [_visible_text(welcome), _visible_text(room), *said]:
        assert not JARGON.search(text), text
    assert "room" not in _visible_text(welcome).lower()
    assert "Type what you want done, the way you would text an assistant." in welcome
    assert ("A room keeps one ongoing project together: its people, mail and decisions. "
            "Most people never need one on day one.") in room
    assert "`open a room for ${name}`" in teach  # the bot's own words for it (ea-chat-bot.py, _ROOM_VERB)


@SIZES
def test_the_welcome_shows_once_and_again_from_the_drawer(app, size):
    from playwright.sync_api import expect
    page, stub, errors = app(welcome=True, **(PHONE if size == "phone" else {}))
    dlg, card = page.locator("#welcome"), page.locator("#welcome-cards > section.on")
    expect(dlg).to_be_visible()
    expect(card.locator("h2")).to_have_text("Ask in plain words")
    expect(card.locator(".example")).to_have_text(["Reply to Sam and say Tuesday works.", "What is on tomorrow?",
                                                   "Find me an hour with Dana next week."])
    expect(page.locator("#welcome-dots")).to_have_attribute("aria-label", "Card 1 of 3")
    expect(dlg).to_be_focused()  # the dialog itself: no button wears a focus ring on open
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        assert page.evaluate(SIDEWAYS) == [0, 0]
        # A swipe to the left moves on, one to the right comes back.
        box = page.locator("#welcome-cards").bounding_box()
        y = box["y"] + box["height"] / 2
        for x0, x1, title in ((box["x"] + box["width"] - 20, box["x"] + 20, "Nothing goes out without you"),
                              (box["x"] + 20, box["x"] + box["width"] - 20, "Ask in plain words")):
            page.mouse.move(x0, y)
            page.mouse.down()
            page.mouse.move(x1, y, steps=5)
            page.mouse.up()
            expect(card.locator("h2")).to_have_text(title)
            expect(dlg).to_be_visible()  # the swipe began on an example, and did not tap it
    page.click("#welcome-next")
    expect(card.locator("h2")).to_have_text("Nothing goes out without you")
    expect(card).to_contain_text("a card lands in Direct messages with a Send or Create button. "
                                 "Nothing is sent until you tap it.")
    expect(page.locator("#welcome-dots")).to_have_attribute("aria-label", "Card 2 of 3")
    page.keyboard.press("ArrowRight")
    expect(card.locator("h2")).to_have_text("Needs you")
    expect(card).to_contain_text("The Needs you list at the top gathers everything waiting on you. "
                                 "Tap Mark done when it is handled.")
    expect(page.locator("#welcome-next")).to_have_text("Done")
    expect(page.locator("#welcome-skip")).to_be_hidden()
    page.click("#welcome-next")
    expect(dlg).to_be_hidden()
    wait_for(lambda: page.evaluate("localStorage.getItem('tacitus-welcome-seen')") == "1")  # on the close event
    # Closed once, it stays closed in this browser.
    page.reload()
    page.wait_for_selector(".thread-item")
    expect(page.locator("#tour-again")).to_be_attached()
    page.wait_for_timeout(300)
    expect(dlg).to_be_hidden()
    # "How Tacitus works" opens it again at the first card; Escape closes it.
    _drawer(page, size)
    page.click("#tour-again")
    expect(dlg).to_be_visible()
    expect(card.locator("h2")).to_have_text("Ask in plain words")
    expect(page.locator("body")).not_to_have_class(re.compile("drawer"))
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    assert not errors, errors


@SIZES
def test_an_example_fills_the_composer_in_the_ea_channel(app, size):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(welcome=True, **(PHONE if size == "phone" else {}))
    page.locator("#welcome .example", has_text="Reply to Sam").click()
    expect(page.locator("#welcome")).to_be_hidden()
    expect(page).to_have_url(re.compile(re.escape("#/stream%3A9/%2B") + "$"))
    expect(page.locator("#thread-title")).to_have_text("New conversation")
    expect(page.locator("#messages .empty")).to_have_text("Write your message below. It starts a new conversation.")
    expect(page.locator("#text")).to_be_focused()
    expect(page.locator("#composer")).to_be_in_viewport()
    assert page.input_value("#text") == "Reply to Sam and say Tuesday works."
    # The name is selected: typing replaces it.
    page.keyboard.type("Alex")
    assert page.input_value("#text") == "Reply to Alex and say Tuesday works."
    assert not stub.posts
    page.press("#text", "Enter")
    wait_for(lambda: stub.posts)
    assert stub.posts == [{"channel": W.MAIN, "text": "Reply to Alex and say Tuesday works.",
                           "thread_key": None, "file": None}]
    wait_for(lambda: page.evaluate("localStorage.getItem('tacitus-welcome-seen')") == "1")
    assert not errors, errors


@SIZES
def test_new_room_asks_the_bot_in_the_ea_channel_and_opens_the_conversation(app, size):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    _drawer(page, size)
    btn = page.locator("#new-room")
    expect(btn).to_have_text("New room")
    expect(btn).to_have_class(re.compile(r"\bplain\b"))  # muted, last in the drawer's foot
    assert page.evaluate("$('drawer-foot').lastElementChild.id") == "new-room"
    btn.click()
    dlg, name = page.locator("#room"), page.locator("#room-name")
    expect(dlg).to_be_visible()
    expect(name).to_be_focused()
    expect(page.locator("label[for=room-name]")).to_have_text("What is the project called?")
    expect(page.locator("#room-hint")).to_have_text("A room keeps one ongoing project together: its people, mail "
                                                    "and decisions. Most people never need one on day one.")
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
    # An empty name is refused where it is typed.
    page.click("#room-open")
    expect(page.locator("#room-error")).to_have_text("Type the name of the project.")
    expect(name).to_have_attribute("aria-invalid", "true")
    name.fill(" ")
    expect(page.locator("#room-error")).to_be_hidden()
    page.keyboard.press("Enter")
    expect(page.locator("#room-error")).to_be_visible()
    assert not stub.posts
    # Cancel and Escape close it and leave the drawer as it was.
    page.click("#room-cancel")
    expect(dlg).to_be_hidden()
    btn.click()
    expect(page.locator("#room-error")).to_be_hidden()
    assert name.input_value() == ""
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    expect(btn).to_be_visible()
    btn.click()
    name.fill("  Spring   tour ")
    page.click("#room-open")
    wait_for(lambda: stub.posts)
    assert stub.posts == [{"channel": W.MAIN, "text": "open a room for Spring tour", "thread_key": None, "file": None}]
    key = next(m for m in stub.messages if m["text"] == "open a room for Spring tour")["thread_key"]
    expect(page).to_have_url(re.compile(re.escape("#/stream%3A9/" + key) + "$"))
    expect(dlg).to_be_hidden()
    expect(page.locator("#messages .msg.own")).to_contain_text("open a room for Spring tour")
    # The bot answers there, and the room arrives in the drawer.
    expect(page.locator("#messages .msg.bot")).to_contain_text("Opened #ea-spring-tour")
    expect(page.locator("#channel-list .c-name", has_text="ea-spring-tour")).to_have_count(1)
    if size == "phone":
        assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


@SIZES
def test_an_empty_list_says_what_the_place_is_for(app, size):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    stub.messages[:] = [m for m in stub.messages if m["channel"] not in (W.DM, W.MAIN, W.SCHED, W.ROOM)]
    page.reload()
    empty = page.locator("#thread-list .empty")
    expect(empty).to_have_text("Cards that need your tap land here.")  # the DM opens first
    expect(page.locator("#thread-list .filters")).to_have_count(0)  # nothing to filter
    for where, says in (("r1", "This room is for one project. Ask about it here and Tacitus keeps track of it."),
                        ("stream%3A11", "Tacitus finds meeting times here.")):
        page.evaluate(f"location.hash = '#/{where}'")
        expect(empty).to_have_text(says)
    page.evaluate("location.hash = '#/stream%3A9'")
    expect(empty.locator("p")).to_have_text("Ask for something. For example:")
    example = empty.locator(".example")
    expect(example).to_have_text("Find me an hour with Dana next week.")
    for text in empty.all_inner_texts():
        assert not JARGON.search(text), text
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
    example.click()
    expect(page).to_have_url(re.compile(re.escape("#/stream%3A9/%2B") + "$"))
    expect(page.locator("#text")).to_be_focused()
    assert page.input_value("#text") == "Find me an hour with Dana next week."
    assert page.evaluate("[$('text').selectionStart, $('text').selectionEnd]") == [21, 25]
    assert not errors, errors


# -- installing on a phone -------------------------------------------------

# navigator.standalone exists on iOS only: false in Safari, true in the app
# opened from the Home Screen.
IOS_SAFARI = "Object.defineProperty(Navigator.prototype, 'standalone', { get: () => false, configurable: true });"
IOS_APP = "Object.defineProperty(Navigator.prototype, 'standalone', { get: () => true, configurable: true });"
# Chrome's install offer, as a page sees it.
FAKE_PROMPT = """() => {
    window.__prompted = window.__prompted || 0;
    const e = new Event('beforeinstallprompt', { cancelable: true });
    e.prompt = () => { window.__prompted += 1; return Promise.resolve({ outcome: 'accepted' }); };
    e.userChoice = Promise.resolve({ outcome: 'accepted', platform: 'web' });
    dispatchEvent(e);
    return e.defaultPrevented;
}"""
# iOS's keyboard shrinks the visual viewport and leaves the layout alone;
# a stand-in visualViewport the test resizes.
FAKE_VIEWPORT = """(() => {
    const vv = new EventTarget();
    Object.assign(vv, { width: 390, height: 844, scale: 1, offsetTop: 0, offsetLeft: 0, pageTop: 0, pageLeft: 0 });
    Object.defineProperty(window, 'visualViewport', { get: () => vv, configurable: true });
    window.__vv = vv;
})();"""


def test_ios_safari_shows_the_install_line_until_it_is_hidden(app):
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 390, "height": 844}, init=IOS_SAFARI)
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    line = page.locator("#install")
    expect(line).to_be_in_viewport()
    expect(page.locator("#install-text")).to_have_text("Tap Share, then Add to Home Screen, so notifications work.")
    expect(page.locator("#install-btn")).to_be_hidden()
    page.click("#install-close")
    expect(line).to_be_hidden()
    # The stub offers no push and no relay: the foot keeps the welcome's entry and New room.
    assert page.evaluate(FOOT) == ["push-subs", "tour-again", "new-room"]
    hidden_at = page.evaluate("Number(localStorage.getItem('tacitus-install-hidden'))")
    assert abs(hidden_at / 1000 - time.time()) < 60
    page.reload()
    page.wait_for_selector(".thread-item")
    page.click("#menu")  # closed, the drawer shows nothing
    expect(page.locator("#channels")).to_be_in_viewport()
    expect(line).to_be_hidden()
    # Thirty days on, it is back.
    page.evaluate("localStorage.setItem('tacitus-install-hidden', String(Date.now() - 31 * 864e5))")
    page.reload()
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    expect(line).to_be_visible()
    assert not errors, errors


def test_the_installed_app_shows_no_install_line(app):
    from playwright.sync_api import expect
    page, stub, errors = app(init=IOS_APP)
    page.wait_for_selector(".thread-item")
    page.evaluate(FAKE_PROMPT)
    expect(page.locator("#install")).to_be_hidden()
    assert page.evaluate(FOOT) == ["push-subs", "tour-again", "new-room"]
    assert not errors, errors


def test_chrome_offers_an_install_button(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    line, button = page.locator("#install"), page.locator("#install-btn")
    expect(line).to_be_hidden()  # not iOS, and no offer from the browser yet
    assert page.evaluate(FAKE_PROMPT) is True  # the app's line replaces Chrome's own banner
    expect(button).to_be_visible()
    expect(page.locator("#install-text")).to_have_text("Install Tacitus as an app.")
    button.click()
    assert page.evaluate("window.__prompted") == 1
    expect(line).to_be_hidden()
    # A later offer shows the line again; hidden, it stays hidden.
    page.evaluate(FAKE_PROMPT)
    expect(button).to_be_visible()
    page.click("#install-close")
    expect(line).to_be_hidden()
    page.evaluate(FAKE_PROMPT)
    expect(line).to_be_hidden()
    assert page.evaluate("window.__prompted") == 1
    assert not errors, errors


def test_permalinks_stay_in_the_app_and_other_links_open_outside(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    base = page.url.split("#")[0]

    def md(text):
        return page.evaluate(
            "(s) => { const d = document.createElement('div'); d.append(markdown(s)); return d.innerHTML; }", text)

    assert md(f"[weekly]({base}#/stream%3A9) and [room](/chat/#/r1)") == (
        f'<p><a href="{base}#/stream%3A9">weekly</a> and <a href="/chat/#/r1">room</a></p>')
    # Another origin, and this origin's other paths (a stored file), open outside the app's window.
    out = md(f"[file]({base}api/files/f1) [away](https://e.example/chat/#/x) {base}")
    assert out.count('target="_blank" rel="noopener noreferrer"') == 3, out
    page.evaluate("(s) => $('thread-list').prepend(markdown(s))", f"[weekly]({base}#/stream%3A9)")
    page.click("#thread-list a:has-text('weekly')")
    expect(page.locator(".chan.active .c-name")).to_have_text("ea")
    assert len(page.context.pages) == 1
    assert not errors, errors


def test_a_tapped_notification_routes_the_open_window(app):
    """sw.js posts the url to a window it cannot navigate; the page routes
    to it when it is a route of this app and ignores anything else."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    base = page.url.split("#")[0]
    post = "(u) => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { open: u } }))"
    for away in ("https://e.example/chat/#/stream%3A9", base + "api/files/f1#/stream%3A9", 42):
        page.evaluate(post, away)
    page.wait_for_timeout(100)
    expect(page.locator(".chan.active .c-name")).to_have_text("Direct messages")
    page.evaluate(post, base + "#/stream%3A9")
    expect(page.locator(".chan.active .c-name")).to_have_text("ea")
    assert page.url == base + "#/stream%3A9"
    assert not errors, errors


def test_the_composer_stays_above_the_keyboard(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(viewport={"width": 390, "height": 844}, init=FAKE_VIEWPORT)
    open_thread(page, "Draft a reply")
    tk = stub.messages[0]["thread_key"]
    for i in range(25):
        stub.emit_bot(W.DM, f"Line {i}", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('Line 24')")).to_be_in_viewport()
    # The keyboard opens: the app takes the visual viewport's height, at the
    # top, and the thread keeps its newest message in view.
    page.evaluate("__vv.height = 470; __vv.dispatchEvent(new Event('resize'))")
    expect(page.locator("body")).to_have_css("height", "470px")
    box = page.locator("#composer").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 470
    wait_for(lambda: page.evaluate("nearBottom()"))
    # Zoomed in, the app keeps the layout's height.
    page.evaluate("__vv.scale = 2; __vv.dispatchEvent(new Event('resize'))")
    expect(page.locator("body")).to_have_css("height", "844px")
    page.evaluate("__vv.scale = 1; __vv.dispatchEvent(new Event('resize'))")
    expect(page.locator("body")).to_have_css("height", "470px")
    # Read back up the thread, then send: the thread moves to the newest message.
    page.evaluate("$('messages').scrollTop = 0")
    wait_for(lambda: not page.evaluate("nearBottom()"))
    page.fill("#text", "From the phone")
    page.press("#text", "Enter")
    expect(page.locator("#messages .msg.own:has-text('From the phone')")).to_be_in_viewport()
    wait_for(lambda: page.evaluate("nearBottom()"))
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


# -- the app's code against the bot's signed manifest (docs/webchat.md) ----

def _worker_state(page, name):
    """One entry of the service worker's state (the trusted manifest, a
    refusal), as the page reads it from Cache Storage."""
    return page.evaluate("""async (name) => {
        const scope = (await navigator.serviceWorker.ready).scope;
        const hit = await (await caches.open('tacitus-webchat-v3-bundle')).match(new URL(`__bundle/${name}`, scope).href);
        return hit ? hit.json() : null;
    }""", name)


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


# -- the relay (docs/webchat-relay.md) -----------------------------------

ROOT = TESTS.parent
VECTORS = TESTS / "fixtures" / "relay_vectors.json"


def test_the_relay_keys_stay_in_webcrypto_and_indexeddb():
    js = read("app.js")
    # Web Storage holds four things, none a key: when this browser hid the
    # install line, the connection notes it reports to the bot, the
    # thread filter chosen (a read at start and a write per choice), and
    # that the welcome was closed once (a read after the channels load, a
    # write when it closes).
    assert "sessionStorage" not in js and js.count("localStorage") == 10
    assert all(f"localStorage.{verb}(WELCOME_KEY" in js for verb in ("getItem", "setItem"))
    assert all(f"localStorage.{verb}(INSTALL_KEY" in js for verb in ("getItem", "setItem"))
    assert all(f"localStorage.{verb}(FILTER_KEY" in js for verb in ("getItem", "setItem"))
    assert all(f"localStorage.{verb}(DIAG_KEY" in js for verb in ("getItem", "setItem", "removeItem"))
    assert "indexedDB.open('tacitus-relay', 1)" in js
    # The device's private key is kept as PKCS#8 bytes in IndexedDB (WebKit
    # gives no X25519 CryptoKey back from there) and used as a non-extractable
    # import of them; an ephemeral key is never exported. Nothing is exported
    # anywhere else, and nothing leaves IndexedDB.
    assert "generateKey({ name: 'X25519' }, keep, ['deriveBits'])" in js
    assert js.count("exportKey('pkcs8'") == 1 and "if (!keep) return { priv: pair.privateKey, pub };" in js
    assert "importKey('pkcs8', bytes, { name: 'X25519' }, false, ['deriveBits'])" in js
    # the bytes stay out of every frame: the hello and the transport never see them
    assert "privBytes" not in js[js.index("async function relayHello"):js.index("function idb")]
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
    assert '<button type="button" id="phone-relay" class="btn" hidden>Phone without Tailscale</button>' in html
    assert '<button type="button" id="phone-here" class="btn primary" hidden>Set up this phone</button>' in html
    js = read("app.js")
    assert "call('POST', '/relay/pair', via === 'relay' ? { via } : {})" in js and "QR.draw($('phone-qr'), out.qr, 4)" in js
    assert "Boolean(S.me.relay) && !T.relay" in js  # where the API answers: the tailnet or this machine
    assert "call('POST', `/relay/${verb}`, { device: d.id, cards })" in js  # Approve and Remove
    assert "'Allow card buttons on this phone'" in js  # beside a relay-origin phone, off by default
    assert "Never send this link through chat or mail." in js


def test_the_settings_screen_is_in_the_drawer():
    """A "Settings" entry at the drawer's foot opens a dialog with the
    local model's state, a row per task and Save; the app reads and saves
    the routing table at /settings/routing and shows the entry only when
    /api/me names that page, never on a relayed copy."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    assert '<button type="button" id="open-settings" class="btn small" hidden>Settings</button>' in nav
    dlg = html[html.index('<dialog id="settings"'):html.index("</dialog>", html.index('<dialog id="settings"'))]
    for part in ('<p id="settings-local">', '<ul id="settings-rows">', '<p id="settings-note" aria-live="polite">',
                 '<button type="button" id="settings-save" class="btn primary">Save</button>'):
        assert part in dlg, part
    js = read("app.js")
    assert "call('GET', '/settings/routing')" in js and "call('POST', '/settings/routing', { tasks })" in js
    assert "$('open-settings').hidden = T.relay || !(S.me.settings || []).includes('routing')" in js
    assert "disabled: !r.allowed.includes(v)" in js  # a floor locks a value
    assert ".chan, .seg label { min-height: 44px; }" in read("app.css")  # a finger's 44px


def test_the_relay_transport_takes_only_its_own_answers():
    """An answer opens only as the answer to its own request (or an event
    of its own stream); a POST is never sent twice on an err frame, which
    the relay can forge after the bot ran it."""
    js = read("app.js")
    transport = js[js.index("function relayTransport"):js.index("async function pickTransport")]
    assert "openFrame(s, await post('c', await sealFrame(s, 'req', id, plain)), 'res', id)" in transport
    assert "openFrame(s, fr, 'sse', opener.id)" in transport
    assert "frame.k !== kind || frame.id !== id" in js
    assert "if (attempt || method !== 'GET') throw new RelayError('unsure');" in transport
    assert "if (s.ctr >= 2 ** 48)" in js


def test_a_relayed_file_is_a_raster_image_or_plain_bytes():
    js = read("app.js")
    body = js[js.index("function relayFile"):js.index("const norm =")]
    assert "const RASTER = /^image\\/(png|jpeg|gif|webp)$/;" in js
    assert "RASTER.test(type) ? type : 'application/octet-stream'" in body
    assert "a.setAttribute('download', f.name || 'file');" in body and "a.removeAttribute('target');" in body


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
    yield from _relay_bot(tmp_path, WEBUI)


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


def _relay_bot(tmp_path, static, webui=WEBUI):
    pytest.importorskip("cryptography")
    sys.path.insert(0, str(ROOT / "relay"))
    sys.path.insert(0, str(ROOT / "botcore"))
    import threading
    import relay_server as rs
    from botcore.chat.web_frontend import WebFrontend
    relay = rs.Relay(rs.Limits(keepalive=0.5, client_timeout=15), log=lambda line: None)
    rsrv = rs.RelayHTTPServer(("127.0.0.1", 0), relay, str(static))
    threading.Thread(target=rsrv.serve_forever, daemon=True).start()
    rbase = f"http://127.0.0.1:{rsrv.server_address[1]}"
    port = _free_port()
    public = f"http://127.0.0.1:{port}/chat"
    fe = WebFrontend({"port": port, "public_url": public,
                      "relay": {"url": rbase, "wait": 1}},
                     store_path=tmp_path / "priv" / "webchat.sqlite", webui_dir=webui,
                     trusted_config=True)
    fe.poll_s = fe.pair_delay_s = 0.05
    if webui != WEBUI:
        fe.bundle_check_s = 3600
    got = []
    fe.registry.event("message")(got.append)
    fe.registry.action("approve_send")(got.append)
    fe.start()
    wait_for(lambda: fe.relay is not None and fe.relay.connected, 10)
    yield {"fe": fe, "relay": relay, "rbase": rbase, "public": public, "got": got, "webui": webui}
    fe.stop()
    rsrv.shutdown()
    rsrv.server_close()


def _send_card(fe):
    from botcore.chat.cards import CardAction, ConfirmationCard
    card = ConfirmationCard(kind="send", title=":lock: Send this draft?", body="Hello there.",
                            actions=[CardAction("approve_send", "Send", {"t": 1}, "primary", "send"),
                                     CardAction("cancel_send", "Cancel", {"t": 1}, "", "cancel")])
    return fe.post_card("dm", card)


def _owner_context(browser, fe, contexts, workers=False):
    """A browser context signed in to the web chat as the owner (on the
    tailnet the identity header does this; here the pairing cookie)."""
    ctx = browser.new_context(service_workers="allow" if workers else "block")
    contexts.append(ctx)
    ctx.add_cookies([{"name": "ea_web", "value": fe.pair_token(), "domain": "127.0.0.1",
                      "path": "/chat", "httpOnly": True, "sameSite": "Strict"}])
    return ctx


def _pair_and_approve(browser, relay_bot, contexts, workers=False):
    """The owner opens "Pair a phone" on the tailnet; the phone, on the
    tailnet too and signed in, opens the link and shows its code; the owner
    approves the phone whose code matches; then the phone leaves the
    tailnet. The phone runs the service worker when `workers`. (owner page,
    phone page, phone errors, URLs the phone asked)."""
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    tail = _owner_context(browser, fe, contexts).new_page()
    tail.goto(relay_bot["public"] + "/")
    tail.click("#pair-phone")
    expect(tail.locator("#phone-devices")).to_contain_text("No phone is set up yet.")
    expect(tail.locator("#phone-self")).to_contain_text("This browser is not set up yet.")
    wait_for(lambda: tail.input_value("#phone-link"), 10)
    link = tail.input_value("#phone-link")
    assert link.startswith(relay_bot["public"] + "/#tr2=")
    # The phone the owner holds has no second screen to scan from: "Pair this
    # phone" pairs this very browser from the same link, with no navigation.
    expect(tail.locator("#phone-here")).to_be_visible()
    tail.evaluate("() => { window.__paired = []; pairFromLink = (k, t, s) => { window.__paired.push([k, t, s]); }; }")
    tail.click("#phone-here")
    assert tail.evaluate("window.__paired") == [["tr2", link.split("#tr2=")[1], True]]
    assert not tail.evaluate("document.getElementById('phone').open")
    tail.click("#pair-phone")
    wait_for(lambda: tail.input_value("#phone-link"), 10)
    link = tail.input_value("#phone-link")
    expect(tail.locator("#phone-note")).to_contain_text("Never send this link through chat or mail.")
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

    ctx = _owner_context(browser, fe, contexts, workers)
    page = ctx.new_page()
    errors, asked = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: asked.append(r.url))
    page.goto(link)
    expect(page.locator("#relay-note")).to_contain_text("waits for approval", timeout=15000)
    assert "tr2=" not in page.url
    shown = re.search(r"code (\d{3} \d{3})", page.inner_text("#relay-note")).group(1)
    # The owner's list shows the new phone under the same code, and the DM says so.
    row = tail.locator("#phone-devices li", has_text="waits for approval")
    expect(row).to_contain_text(f"code {shown}", timeout=10000)
    assert [d["pending"] for d in fe.relay.devices()] == [True]
    dm = [m["text"] for m in fe.store.threads("dm", 50, None)]
    assert any(shown in t for t in dm), dm
    row.locator("button", has_text="Approve").click()
    expect(tail.locator("#phone-devices")).not_to_contain_text("waits for approval", timeout=10000)
    assert [d["pending"] for d in fe.relay.devices()] == [False]
    # The paired browser's own dialog says it is set up, under its code; the
    # device is named by what it is (a browser, not a home-screen app).
    page.click("#pair-phone")
    expect(page.locator("#phone-self")).to_contain_text(f"This browser is set up (code {shown}).")
    assert fe.relay.devices()[0]["name"].endswith(" browser")
    page.keyboard.press("Escape")
    # Off the tailnet now, with the app still open: the bot's own API is out
    # of reach. The next call fails on the network, picks the relay and goes
    # again; the event stream follows.
    ctx.route(re.compile(r".*/chat/api/.*"), lambda route: route.abort())
    assert page.evaluate("() => call('GET', '/channels').then((rows) => rows.length)") >= 1
    assert page.evaluate("() => T.relay") is True
    expect(page.locator("#live.on")).to_be_attached(timeout=15000)
    page.reload()
    expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
    expect(page.locator("#live.on")).to_be_attached(timeout=10000)
    return tail, page, errors, asked


def test_a_phone_pairs_on_the_tailnet_and_chats_through_the_relay(browser, relay_bot):
    """The owner opens "Pair a phone" on the tailnet origin; a phone opens
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
        forge["armed"] = True
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


def _kept(page, name):
    """The service worker's copy of one file of the app, as text; None without one."""
    return page.evaluate("""async (name) => {
        const hit = await (await caches.open('tacitus-webchat-v3')).match(name === 'index.html' ? './' : name);
        return hit ? hit.text() : null;
    }""", name)


def test_a_phone_off_the_tailnet_takes_a_new_release_through_the_relay(browser, relay_bot_deploys, caplog):
    """A phone paired on the tailnet runs the copy its service worker
    checked there. Off the tailnet, the bot deploys a new app.css and signs
    again. At its next start the phone fetches the signed manifest and the
    changed file from the bot through the relay and checks the file; its
    worker checks it again and keeps it. The phone says to close the app and
    open it again, and does not reload. A file that does not hash to the
    manifest is kept by neither, and the phone reports it to the bot."""
    import logging
    from playwright.sync_api import expect
    bot = relay_bot_deploys
    fe, webui = bot["fe"], bot["webui"]
    line = "Tacitus updated. Close the app and open it again."
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
        assert fe.refresh_bundle() != first

        # The bot serves app.css with bytes its manifest does not name.
        fe.note_served = lambda rel, data: True
        (webui / "app.css").write_bytes(old + b"\n/* not signed */\n")
        with caplog.at_level(logging.INFO, logger="chat.web"):
            page.reload()
            expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
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
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
        page.evaluate("window.__open = true")
        expect(page.locator("#toast")).to_have_text(line, timeout=20000)
        expect(page.locator("#update-note")).to_have_text(line)
        assert page.evaluate("window.__open") is True  # no reload in the middle of a conversation
        assert _kept(page, "app.css") == new.decode()
        assert _worker_state(page, "trusted")["hash"] == fe.bundle_hash()
        # The next start runs the new release from the worker's copy.
        page.reload()
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=15000)
        assert page.evaluate("fetch('app.css').then((r) => r.text())") == new.decode()
        expect(page.locator("#update-note")).to_be_hidden()
        assert not errors, errors
    finally:
        for c in contexts:
            c.close()


def _relay_origin_phone(browser, relay_bot, contexts, bundle=None, approve=True):
    """A phone that opens a link to the relay's copy of the app with the
    bot's bundle hash (or `bundle`), as "Pair through the relay" makes one,
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
    approves it under "Pair a phone" with card buttons left off (the
    default). It fetches the bot's signed manifest through the relay, its
    service worker finds every file of the copy in it, and the phone
    attests the bundle: the owner's list says "verified copy", which grants
    nothing. It chats, its card buttons stay greyed out, and they work once
    the owner ticks "Allow card buttons on this phone" for it."""
    from playwright.sync_api import expect
    fe, got = relay_bot["fe"], relay_bot["got"]
    contexts = []
    try:
        _send_card(fe)
        page, errors, device = _relay_origin_phone(browser, relay_bot, contexts, approve=False)
        tail = _owner_context(browser, fe, contexts).new_page()
        tail.goto(relay_bot["public"] + "/")
        tail.click("#pair-phone")
        row = tail.locator(f"#phone-devices li[data-device='{device}']")
        expect(row).to_contain_text("waits for approval", timeout=10000)
        box = row.locator("input[type=checkbox]")
        expect(row.locator("label")).to_have_text("Allow card buttons on this phone")
        expect(box).not_to_be_checked()
        row.locator("button", has_text="Approve").click()
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=20000)
        (d,) = fe.relay.devices()
        assert d["pending"] is False and d["bundle_verified"] is True and d["cards_allowed"] is False
        expect(row).to_contain_text("without Tailscale", timeout=10000)
        trusted = _worker_state(page, "trusted")
        assert trusted["hash"] == fe.bundle_hash() and trusted["pin"] is True
        assert page.evaluate("loadPairing().then((r) => r.signKey)") == fe.bundle.public
        assert page.evaluate("S.me.card_actions") is False
        page.locator(".thread-item:has-text('Send this draft?')").click()
        expect(page.locator(".card button.primary")).to_be_disabled()
        expect(page.locator(".card .card-note")).to_contain_text("does not press card buttons")
        # The drawer offers no pairing off the owner's own web chat.
        expect(page.locator("#pair-phone")).to_be_hidden()

        # The owner allows card buttons for this device.
        row.locator("input[type=checkbox]").check()
        wait_for(lambda: fe.relay.devices()[0]["cards_allowed"], 10)
        page.reload()
        expect(page.locator(".chan .c-name").first).to_be_visible(timeout=20000)
        assert page.evaluate("S.me.card_actions") is True
        page.locator(".thread-item:has-text('Send this draft?')").click()
        page.locator(".card button.primary").click()
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


def test_a_relay_copy_that_cannot_check_its_code_pairs_nothing(browser, stub_factory):
    """A copy of the app on the relay's origin whose bundle.js has no
    verifyBundle (one from before the signed bundle) stops on "Code
    verification unavailable" before it makes a key: nothing is paired."""
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
        expect(page.locator("body")).to_contain_text("Code verification unavailable")
        assert "tr2=" not in page.url
        assert page.evaluate("loadPairing().then((r) => r === undefined)")
        assert not errors, errors
    finally:
        ctx.close()


def test_pair_a_phone_offers_the_relay_on_this_machines_own_address(app):
    """On a loopback origin (no public_url: no tailnet) "Pair a phone" makes
    no link a phone could not open. It offers "Pair through the relay",
    which asks for a link to the relay's copy of the app with the bot's
    bundle hash and says what the phone may do once approved."""
    from playwright.sync_api import expect
    pytest.importorskip("cryptography")
    page, stub, errors = app(relay_pairing=True, sign=True)
    page.wait_for_selector(".thread-item")
    page.click("#pair-phone")
    expect(page.locator("#phone-note")).to_contain_text('This address does not open on a phone. Use "Phone without Tailscale" instead.')
    button = page.locator("#phone-relay")
    expect(button).to_be_visible()
    expect(button).to_have_text("Phone without Tailscale")
    assert stub.pairs == [] and not page.input_value("#phone-link")
    button.click()
    wait_for(lambda: page.input_value("#phone-link"), 5)
    assert stub.pairs == [{"via": "relay"}]
    link = page.input_value("#phone-link")
    assert link.startswith("https://relay.example.net/#tr2=")
    import base64
    assert json.loads(base64.urlsafe_b64decode(link.split("#tr2=")[1] + "=="))["b"] == stub.bundle.hash
    expect(page.locator("#phone-note")).to_contain_text(
        "once you tap Approve below it can read and reply, and press buttons only if you allow it.")
    expect(page.locator("#phone-note")).to_contain_text("Never send this link through chat or mail.")
    expect(button).to_be_hidden()
    assert page.evaluate("$('phone-qr').width") > 0
    assert not errors, errors


def test_approve_sends_the_card_button_choice(app):
    """Beside a relay-origin phone that waits, "Allow card buttons on this
    phone" is off; Approve sends `cards` false, or true once ticked. An
    own-origin phone has no such box."""
    from playwright.sync_api import expect
    page, stub, errors = app(relay_pairing=True)
    stub.devices[:] = [
        {"id": "1" * 16, "name": "phone", "paired_at": 1793400000, "origin": "relay", "pending": True,
         "code": "123456", "cards_allowed": False, "bundle_verified": False},
        {"id": "2" * 16, "name": "tablet", "paired_at": 1793400000, "origin": "relay", "pending": True,
         "code": "654321", "cards_allowed": False, "bundle_verified": False},
        {"id": "3" * 16, "name": "laptop", "paired_at": 1793400000, "origin": "own", "pending": False}]
    page.wait_for_selector(".thread-item")
    page.click("#pair-phone")
    phone, tablet = (page.locator(f"#phone-devices li[data-device='{n * 16}']") for n in "12")
    expect(phone).to_contain_text("code 123 456, waits for approval")
    expect(phone.locator("input[type=checkbox]")).not_to_be_checked()
    expect(page.locator("#phone-devices li[data-device='3333333333333333'] input")).to_have_count(0)
    phone.locator("button", has_text="Approve").click()
    tablet.locator("input[type=checkbox]").check()
    tablet.locator("button", has_text="Approve").click()
    wait_for(lambda: len(stub.approvals) == 2, 5)
    assert stub.approvals == [{"device": "1" * 16, "cards": False}, {"device": "2" * 16, "cards": True}]
    expect(tablet).to_contain_text("without Tailscale, code not checked")
    expect(tablet.locator("input[type=checkbox]")).to_be_checked()
    assert not errors, errors


def test_the_settings_screen_renders_the_rows_and_saves_the_table(app):
    """The screen draws a row per task from /settings/routing: the value
    checked, a value a floor refuses disabled with the floor's line under
    the row, the model it runs on now, and the local model's state at the
    top. Save posts the enabled choices and redraws from the answer; a
    refusal stays on screen."""
    from playwright.sync_api import expect
    page, stub, errors = app(settings=True)
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    rows = page.locator("#settings-rows li")
    expect(rows).to_have_count(3)
    expect(page.locator("#settings-local")).to_have_text("Local model local-27b: reachable. Answered 2 min ago.")
    drafting, mail, review = (page.locator(f'#settings-rows li[data-task="{t}"]') for t in ("drafting", "mail", "review"))
    expect(drafting.locator(".s-name")).to_have_text("Email bodies")
    expect(drafting.locator("input[value=auto]")).to_be_checked()
    expect(drafting.locator(".s-now")).to_have_text("Now local: the drafting switch is remote.")
    expect(drafting.locator(".s-floor")).to_have_count(0)
    expect(mail.locator("input[value=hosted]")).to_be_disabled()
    expect(mail.locator("input[value=local]")).to_be_enabled()
    expect(mail.locator(".s-floor")).to_have_text("Keep floor: hosted is not available while the keep list has entries.")
    expect(review.locator("input[value=hosted]")).to_be_checked()
    for v in ("local", "auto"):
        expect(review.locator(f"input[value={v}]")).to_be_disabled()
    expect(review.locator(".s-floor")).to_have_text("Stakes floor: these are judgment calls, so only the hosted model does them.")
    drafting.locator("input[value=hosted]").check()
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text("Saved. Each task uses its model from its next run.")
    assert stub.settings_posts == [{"tasks": {"drafting": "hosted", "mail": "auto", "review": "hosted"}}]
    expect(drafting.locator("input[value=hosted]")).to_be_checked()
    expect(drafting.locator(".s-now")).to_have_text("Now hosted: set to hosted.")
    # a value the bot refuses: the reason stays on screen and nothing changes
    page.evaluate("document.querySelector('#settings-rows li[data-task=review] input[value=local]').disabled = false")
    review.locator("input[value=local]").check()
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text("Not saved: review is hosted only (the stakes floor)")
    assert stub.routing["rows"][2]["value"] == "hosted"
    page.click("#settings button[type=submit]")
    expect(page.locator("#settings")).not_to_be_visible()
    assert errors == ["Failed to load resource: the server responded with a status of 400 (Bad Request)"]


def test_the_settings_screen_fits_a_phone_and_stays_hidden_without_the_page(app):
    from playwright.sync_api import expect
    page, _stub, errors = app(viewport={"width": 390, "height": 844}, settings=True,
                              context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    page.click("#open-settings")
    expect(page.locator("#settings-rows li")).to_have_count(3)
    small = page.evaluate("""() => [...document.querySelectorAll('#settings .seg label, #settings button')]
        .map((el) => el.getBoundingClientRect().height).filter((h) => h < 44)""")
    assert small == []
    assert page.evaluate("$('settings').scrollWidth - $('settings').clientWidth") == 0
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors
    page, _stub, errors = app()
    page.wait_for_selector(".thread-item")
    expect(page.locator("#open-settings")).to_be_hidden()
    assert not errors, errors


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
    for kind, payload, says in (("tr1", _link_payload(**mine), "no hash of the bot's code"),
                                ("tr2", _link_payload(**mine, v=2, a=url), "no hash of the bot's code"),
                                ("tr2", _link_payload(**mine, v=2, b="e" * 64, a="https://elsewhere.example.net/"),
                                 "opens the app elsewhere"),
                                ("tr1", _link_payload(**mine, v=2, b="e" * 64), "not a pairing link")):
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
    for payload, says, off in ((_link_payload(), "another install", False),
                               (_link_payload(i=W.STUB_INSTALL), "another install", False),
                               (_link_payload(**mine, x=int(time.time()) - 1), "expired", False),
                               (_link_payload(**mine), "on the tailnet", True),
                               ("bm90LWpzb24", "Pairing failed", False)):
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
