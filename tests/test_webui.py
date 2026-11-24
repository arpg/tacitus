"""The web chat's static app (botcore/botcore/chat/webui/), without a browser.

The files the server serves are there and well formed, the service worker
leaves the API and the relay alone, checks the files against the signed
manifest and shows pushes, the app never parses text as HTML and fetches
nothing from elsewhere, the page carries its own security policy, and its
colours meet WCAG AA in both themes. The relay keys stay in WebCrypto and
IndexedDB and never ride in a URL. Every string the person sees passes a
plain-words scan, every shortcode the bot writes has a glyph, and the app
stays within its line budget (tests/webui_budget/).

The app's browser tests are in tests/test_webui_<area>.py, a file per area
(the thread, the composer, the drawer, the relay, Settings, ...), on the
fixtures in tests/webui_browser.py; the wall runs those files side by side.
"""
import json
import re
import struct
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from plain_words import JARGON  # noqa: E402
from webui_browser import WEBUI, _function, read  # noqa: E402

# A file that still imports the browser fixtures from here gets them; a new
# file imports them from webui_browser.
from webui_browser import (  # noqa: E402,F401
    PHONE, SIDEWAYS, SIZES, SMALL_TARGETS, _request, app, browser, open_thread, stub_factory, wait_for,
)


# -- the files -------------------------------------------------------------

FILES = ["index.html", "app.js", "qr.js", "bundle.js", "app.css", "manifest.webmanifest", "sw.js",
         "icon-180.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "favicon.svg"]


TEXT_FILES = ["index.html", "app.js", "qr.js", "bundle.js", "app.css", "manifest.webmanifest", "sw.js"]


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
    worker = read("bundle.js")  # the worker's code, which sw.js runs with each request it lets through
    shell = re.search(r"const SHELL = \[(.*?)\];", worker).group(1)
    assert "api" not in shell and "'qr.js'" in shell
    assert "POST" not in sw and "POST" not in worker and "req.method !== 'GET'" in handler


def test_the_worker_checks_files_against_the_signed_manifest():
    """The worker loads the check the page uses, leaves the signed manifest
    to the browser, and its refusal page runs no script. sw.js is a loader:
    the worker's code is bundle.js, and the one text it runs as code is the
    kept copy of bundle.js whose sha256 the trusted manifest names, read from
    Cache Storage; it fetches nothing itself."""
    sw = read("sw.js")
    assert "importScripts('bundle.js');" in sw
    handler = _handler(sw, "fetch")
    assert "url.pathname.endsWith('/manifest.sig.json')) return;" in handler
    assert handler.index("manifest.sig.json") < handler.index("respondWith")
    bundle = read("bundle.js")
    refusal = bundle[bundle.index("function refusal"):bundle.index("function keepAs")]
    assert "<script" not in refusal and "'Content-Security-Policy': \"default-src 'none'" in refusal
    assert "{ name: 'Ed25519' }" in bundle and "Object.assign(self, { BundleError, verifyBundle" in bundle
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", sw, flags=re.S)  # the loader without its comments
    assert code.count("eval") == 1 and "(0, eval)(code);" in code and "Function(" not in code
    kept = code[code.index("async function keptCode"):code.index("const worker =")]
    assert "trusted.files['bundle.js']" in kept and "crypto.subtle.digest('SHA-256', bytes)" in kept
    assert "=== want ? new TextDecoder().decode(bytes) : null" in kept
    assert "fetch(" not in code and "eval" not in re.sub(r"/\*.*?\*/|//[^\n]*", "", bundle, flags=re.S)
    assert "TacitusWorker: { v: 1, start" in bundle and "const API = 1;" in code


def test_the_notifications_switch_is_under_settings():
    """The drawer's foot has one Settings entry; the switch that turns Web
    Push on for this browser is on its Notifications page, shown where the
    bot can push. On iOS the install line in the foot says how notifications
    come. A headless browser cannot grant push, so these are file checks."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    assert re.search(r'<footer id="drawer-foot" hidden>', nav), "hidden until it has something to show"
    assert '<button type="button" id="open-settings" class="chan" aria-haspopup="dialog" hidden>' in nav
    assert '<div id="install" class="install" hidden>' in nav and 'id="notify"' not in nav and "notify-hint" not in html
    dlg = html[html.index('<dialog id="settings"'):html.index("</dialog>", html.index('<dialog id="settings"'))]
    assert '<label class="toggle" id="notify-row"><input type="checkbox" role="switch" id="notify" disabled>' in dlg
    js = read("app.js")
    assert "S.me.push" in js and "Tap Share, then Add to Home Screen, so notifications work." in js
    assert "$('go-notify').hidden = !S.me.push" in js
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
    assert "addEventListener('push', (event) => run(event, 'push'))" in sw
    assert "addEventListener('notificationclick', (event) => run(event, 'click'))" in sw
    assert "event.waitUntil(worker.then((w) => w[name](event)))" in sw
    worker = read("bundle.js")
    push = _function(worker, "push")
    assert "return self.registration.showNotification(" in push
    for key in ("body:", "icon:", "tag,", "renotify: Boolean(tag)", "data: { url:"):
        assert key in push, key
    click = _function(worker, "click")
    assert "event.notification.close()" in click
    assert "clients.matchAll" in click and "openWindow(url)" in click and ".focus()" in click
    assert "appUrl(event.notification.data" in click
    # a window navigate() cannot move routes itself from a message (app.js)
    assert ".catch(() => win.postMessage({ open: url }))" in click
    app_url = _function(worker, "appUrl")
    assert "url.origin === scope.origin" in app_url and "startsWith(scope.pathname)" in app_url
    # a push never reads or writes the cache, and the fetch handler is unchanged
    assert "caches" not in push and "caches" not in click and "caches" not in app_url
    assert sw.index("addEventListener('push'") < sw.index("addEventListener('fetch'")


def test_a_file_in_the_home_screen_app_opens_a_sheet_not_a_page():
    """Issue #294: in the iOS home-screen app a file link took the app's one
    window to the file, which has no back button. There a tap opens a sheet
    whose Save hands the fetched file to the share sheet; the file is
    fetched before the tap that shares it."""
    js = read("app.js")
    render = js[js.index("function renderFile"):js.index("const homeScreenIOS")]
    assert "const sheet = url && homeScreenIOS();" in render
    assert "relayFile(f, box, !sheet)" in render
    assert "e.preventDefault(); fileSheet(f);" in render
    assert "const homeScreenIOS = () => navigator.standalone === true;" in js
    sheet = js[js.index("function fileSheet"):js.index("// Through the relay a stored file")]
    assert sheet.index("fetchFile(f).then") < sheet.index("save.onclick")
    assert "navigator.canShare({ files: [file] })" in sheet
    assert "await navigator.share({ files: [file] });" in sheet
    assert "e.name !== 'AbortError'" in sheet
    relay = js[js.index("function relayFile"):js.index("const norm =")]
    assert "if (!links) return;" in relay
    css = read("app.css")
    assert "#file-sheet::backdrop" in css and "#file-sheet .sheet" in css


def test_the_app_never_parses_text_as_html():
    js = read("app.js")
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                 "createContextualFragment", "eval(", "new Function", "srcdoc"):
        assert word not in js, word


def test_nothing_comes_from_elsewhere():
    for name in ("index.html", "app.css", "sw.js", "manifest.webmanifest"):
        text = read(name).replace(PAGE_CSP, "") if name == "index.html" else read(name)
        assert not re.search(r"https?://", text), name
    css = read("app.css")
    assert "@import" not in css and "url(" not in css
    # The app's only URLs are the patterns that recognise links in text.
    assert not re.search(r"""["'`]https?://""", read("app.js"))


# The page's own policy, for a host that sends no headers (the website's copy
# of the app): the bot's page policy, with connect-src open to any https relay
# (the pairing names it) and to a relay on this machine, which the app allows.
# frame-ancestors has no effect in a meta tag, so it is left out.
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self' https: http://127.0.0.1:* http://localhost:*; object-src 'none'; "
            "base-uri 'none'; form-action 'none'")


def test_the_page_carries_its_own_security_policy():
    tags = refs()
    metas = [a for t, a in tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
    assert [m["content"] for m in metas] == [PAGE_CSP]
    # Before the first stylesheet and script, so it covers them.
    order = [(t, a.get("http-equiv")) for t, a in tags if t in ("meta", "link", "script")]
    assert order.index(("meta", "Content-Security-Policy")) < order.index(("link", None))
    sys.path.insert(0, str(TESTS.parent / "botcore"))
    from botcore.chat.web_frontend import HTML_CSP
    bot = dict(d.split(" ", 1) for d in HTML_CSP.split("; "))
    mine = dict(d.split(" ", 1) for d in PAGE_CSP.split("; "))
    assert mine.pop("connect-src").startswith("'self' https:")
    assert mine == {k: v for k, v in bot.items() if k not in ("connect-src", "frame-ancestors")}


# The app's line budget is the sum of the grants in tests/webui_budget/:
# each line there is `+N  what the lines buy` (or -N for lines given back).
# 000-history.txt holds every grant up to the move to a file per change and
# is closed; a change that grows the app adds a file of its own, named after
# it, so two open pull requests never edit the same line.
BUDGET = TESTS / "webui_budget"
HISTORY_TOTAL = 5919  # 000-history.txt's sum, closed: new grants go in new files
_GRANT = re.compile(r"([+-]\d+)\s+(\S.*)")


def _grants() -> dict[str, list[tuple[int, str]]]:
    """Each budget file's grants, (lines, what they buy)."""
    grants = {}
    for path in sorted(BUDGET.glob("*.txt")):
        rows = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        rows = [ln for ln in rows if ln and not ln.startswith("#")]
        bad = [ln for ln in rows if not _GRANT.fullmatch(ln)]
        assert rows and not bad, f"{path.name}: each line is `+N  what the lines buy`; not {bad or 'empty'}"
        grants[path.name] = [(int(m[1]), m[2]) for m in map(_GRANT.fullmatch, rows)]
    return grants


def test_the_app_stays_small():
    """The app's text files hold no more lines than its budget grants. A
    change that grows the app says what the lines buy in a file of its own
    under tests/webui_budget/ (CONTRIBUTING.md, Browser tests)."""
    lines = sum(len(read(n).splitlines()) for n in TEXT_FILES)
    grants = _grants()
    assert sum(n for n, _ in grants.pop("000-history.txt")) == HISTORY_TOTAL, \
        "000-history.txt is closed: a new grant goes in a file of its own beside it"
    budget = HISTORY_TOTAL + sum(n for rows in grants.values() for n, _ in rows)
    assert lines <= budget, (
        f"{lines} lines, {lines - budget} over the budget of {budget}: add a file to tests/webui_budget/ "
        "named after the change, with a line `+N  what the lines buy`")


# A `:name:` in the sources that is not an emoji: a prompt that names the
# syntax, a POSIX class, a thread key's parts, a mail quote header, a URL.
NOT_EMOJI = {"emoji", "emoji_shortcodes", "space", "topic", "wrote", "https"}


_SHORTCODE_RE = re.compile(r"(?<![\w:]):([a-z][a-z0-9_+-]{1,40}):(?!\w)")


def test_every_shortcode_the_bot_and_its_prompts_write_has_a_glyph():
    """The app draws a shortcode only from its own table (EMOJI in
    app.js); any other shows as `:name:`, as the Reopen button's
    `:leftwards_arrow_with_hook:` did. Every shortcode the code writes,
    and every one a prompt, a skill or a topic guide shows the model, is
    in the table."""
    app = read("app.js")
    block = app[app.index("const EMOJI = "):app.index("}).flatMap(([glyph, names])")]
    known = {n for _glyph, names in re.findall(r"'([^']+)':\s*'([^']+)'", block)
             for n in names.split()}
    root = TESTS.parent
    sources = [root / "ea-chat-bot.py", root / "CLAUDE.md"]
    for sub, pattern in (("preprocessor", "*.py"), ("botcore/botcore", "*.py"),
                         ("scripts", "*.py"), ("scripts", "*.sh"),
                         ("prompts", "*.md"), ("skills", "*.md"), ("docs/ea", "*.md"),
                         (".claude/skills", "*.md")):
        sources += [p for p in sorted((root / sub).rglob(pattern)) if p.is_file()]
    missing = {}
    for path in sources:
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for name in _SHORTCODE_RE.findall(line):
                if name not in known and name not in NOT_EMOJI:
                    missing.setdefault(name, f"{path.relative_to(root)}:{i}")
    assert not missing, ("shortcodes the web app draws as text; add each to EMOJI in app.js "
                         f"(or to NOT_EMOJI here when it is no emoji): {missing}")


# -- the words the person reads -----------------------------------------

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


# A regex literal may follow these characters or words; after anything else a slash divides.
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")


_REGEX_WORDS = re.compile(r"\b(return|typeof|case|of|in)\s*$")


def _js_strings(src: str) -> list[str]:
    """Every string literal of a script: comments and regex literals skipped,
    a template's text with each ${...} as "${}" (its expressions read too)."""
    found = []

    def template(i):
        text = ""
        while src[i] != "`":
            if src[i] == "\\":
                text, i = text + src[i:i + 2], i + 2
            elif src.startswith("${", i):
                text, i = text + "${}", code(i + 2, True)
            else:
                text, i = text + src[i], i + 1
        found.append(text)
        return i + 1

    def code(i, nested=False):
        depth = 0
        while i < len(src):
            c, before = src[i], src[max(0, i - 40):i].rstrip()
            if src.startswith("//", i):
                i = (src.find("\n", i) + 1) or len(src)
            elif src.startswith("/*", i):
                i = src.index("*/", i) + 2
            elif c in "'\"":
                j = i + 1
                while src[j] != c:
                    j += 2 if src[j] == "\\" else 1
                found.append(src[i + 1:j])
                i = j + 1
            elif c == "`":
                i = template(i + 1)
            elif c == "/" and (not before or before[-1] in _REGEX_AFTER or _REGEX_WORDS.search(before)):
                j, in_class = i + 1, False
                while src[j] != "/" or in_class:
                    in_class = (in_class or src[j] == "[") and src[j] != "]"
                    j += 2 if src[j] == "\\" else 1
                i = j + 1
            else:
                if nested and c in "{}":
                    if c == "}" and not depth:
                        return i + 1
                    depth += 1 if c == "{" else -1
                i += 1
        return i
    code(0)
    return found


class _Shown(HTMLParser):
    """The text of a page and the attributes a screen reader reads."""
    def __init__(self):
        super().__init__()
        self.said: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.said += [v for k, v in attrs if k in ("aria-label", "title", "placeholder", "alt") and v]

    def handle_data(self, data):
        if data.strip():
            self.said.append(" ".join(data.split()))


# Literals the person never sees: a media query, a header, the relay
# protocol's key label (relay_crypto.py), a connection note for the bot
# (/api/diag; the worker's own answer, which a test pins), a file's name,
# and the check's errors (bundle.js), which go to the bot alone.
NOT_SHOWN = {"(display-mode: standalone)", "X-Relay-Key", "${} session", "${} does not match the manifest",
             "webchat-pair.token", "not a manifest", "not a signed manifest", "not the bundle the pairing named",
             "${} is not a headers block"}


# One word with no space that starts in lower case: an id, a class, a key, a route, an event name.
CODE_LIKE = re.compile(r"^[^\sA-Z]\S*$")


def test_every_string_the_person_sees_is_plain():
    """Each string literal of the app and its worker, each text and label of
    the page and the manifest's words pass the gym coach on a phone: none of
    the words JARGON names, save the literals that are code and NOT_SHOWN."""
    said = []
    for name in ("app.js", "sw.js", "bundle.js"):
        strings = _js_strings(read(name))
        # The reader keeps its place: the file's first, middle and last words are found.
        for sentinel in {"app.js": ("Hidden text", "Not done", "${}/sw.js"),
                         "sw.js": ("bundle.js", "notificationclick", "message"),
                         "bundle.js": ("not base64url", "Done.", "${} is not a headers block")}[name]:
            assert sentinel in strings, (name, sentinel)
        said += [s for s in strings if not CODE_LIKE.match(s) and s not in NOT_SHOWN]
    page = _Shown()
    page.feed(read("index.html"))
    said += [s for s in page.said if s not in NOT_SHOWN]
    man = json.loads(read("manifest.webmanifest"))
    said += [man["name"], man["short_name"], man["description"]]
    assert not [s for s in said if JARGON.search(s)]


# -- the relay's part of the app (docs/webchat-relay.md) ------------------

def test_the_relay_keys_stay_in_webcrypto_and_indexeddb():
    js = read("app.js")
    # Web Storage holds six things, none a key: when this browser hid the
    # install line, the connection notes it reports to the bot, the
    # thread filter chosen (a read at start and a write per choice), that
    # the welcome was closed once (a read after the channels load, a write
    # when it closes), the menu's width (a read at start, a write per
    # change, a removal when it goes back to its own), and the drafts, the
    # owner's words and their files' names per place (each read at start, a
    # write per change, a removal when one empties or falls past the newest
    # DRAFTS_KEPT, which reads them all).
    assert "sessionStorage" not in js and js.count("localStorage") == 18
    assert all(f"localStorage.{verb}(DRAFT_KEY" in js for verb in ("setItem", "removeItem"))
    assert "const draftKeys = () => Object.keys(localStorage).filter((k) => k.startsWith(DRAFT_KEY));" in js
    assert "JSON.parse(localStorage.getItem(k)) || {}" in js and "localStorage.removeItem(k)" in js
    assert all(f"localStorage.{verb}(NAV_KEY" in js for verb in ("getItem", "setItem", "removeItem"))
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
    # an err frame is a hint: nothing deletes or clears a stored pairing. The one
    # delete is a computer's first setup that failed with no earlier pairing (its
    # code is spent, so it shows the code screen again instead of a dead setup).
    for word in (".delete(", ".clear(", "deleteDatabase"):
        assert word not in js[js.index("function idb"):js.index("function deviceName")], word
    assert "deleteDatabase" not in js and js.count("st.delete") == 1
    assert ("if (saved && old) await savePairing(old).catch(() => {}); // a failed pairing keeps the earlier one\n"
            "    else if (saved && hosted) await idb('readwrite', (st) => st.delete('pairing'))") in js


def test_the_relay_key_never_rides_in_a_url():
    js = read("app.js")
    transport = js[js.index("function relayTransport"):js.index("async function pickTransport")]
    assert "'X-Relay-Key': rec.clientKey" in transport
    assert "/sse?t=${enc(ticket)}" in transport
    assert "key=" not in transport and "?f=" not in transport
    assert transport.count("credentials: 'omit'") == 2


def test_the_pairing_dialog_opens_from_settings():
    html = read("index.html")
    dlg = html[html.index('<dialog id="settings"'):html.index("</dialog>", html.index('<dialog id="settings"'))]
    assert '<button type="button" id="pair-phone" class="s-item" hidden><span class="s-label">Devices</span>' in dlg
    assert '<dialog id="phone"' in html and '<canvas id="phone-qr"' in html
    assert '<h2 id="phone-title">Add a device</h2>' in html
    for way, label in (("here", "This phone"), ("phone", "Another phone"), ("computer", "A computer")):
        assert f'<button type="button" id="way-{way}" aria-pressed="false">{label}</button>' in html
        assert f'<section id="add-{way}" class="way" hidden>' in html
    assert '<button type="button" id="phone-relay" class="btn" hidden>Phone not on your network</button>' in html
    assert '<button type="button" id="phone-here" class="btn primary" hidden>Set up this phone</button>' in html
    js = read("app.js")
    assert "call('POST', '/relay/pair', via === 'relay' ? { via } : {})" in js and "QR.draw($('phone-qr'), out.qr, 4)" in js
    assert "call('POST', '/relay/handoff', {})" in js
    # Where the API answers (the tailnet or this machine), or relayed when the bot lets this device
    # start a handoff and a code would work now.
    assert "routes === 'handoff' ? Boolean(S.me.handoff) : routes === 'all' && Boolean(S.me.relay)" in js
    assert "(S.me && S.me.owner_routes) || (T.relay ? 'none' : 'all')" in js
    assert "call('POST', `/relay/${verb}`, { device: d.id, cards })" in js  # Approve and Remove
    assert "'Allow card buttons on this phone'" in js  # beside a relay-origin phone, off by default
    assert "'Allow Send and Create buttons on this computer'" in js and "Allow Send and Create buttons on this computer" in html
    assert "Never send this link through chat or mail." in js and "Never send this code through chat or mail." in js


def test_the_settings_menu_is_in_the_drawer():
    """The one "Settings" entry at the drawer's foot opens a menu: Notifications
    (this browser's switch, the three switches and the quiet-hours choice, read
    and saved at /settings/notify, and where notifications go), Devices ("Add
    a device"), Models (the local model's state, a row per task and Save, at
    /settings/routing) and How Tacitus works, each a row with what it holds
    under its name. Notifications and Models open as pages in the dialog, with
    Back and Close at its top; the bot's switches and Models show when /api/me
    names their page, and never on a relayed copy, where the bot saves none."""
    html = read("index.html")
    nav = html[html.index('<nav id="channels"'):html.index("</nav>")]
    assert '<span class="c-name">Settings</span><span id="settings-hint" class="c-hint" hidden>Notifications off</span>' in nav
    dlg = html[html.index('<dialog id="settings"'):html.index("</dialog>", html.index('<dialog id="settings"'))]
    for part in ('<button type="button" id="settings-back" class="icon" aria-label="Back to Settings" hidden>',
                 '<button type="submit" id="settings-close" class="icon" aria-label="Close">',
                 '<button type="button" id="go-notify" class="s-item" hidden><span class="s-label">Notifications</span>',
                 '<button type="button" id="go-routing" class="s-item" hidden><span class="s-label">Models</span>',
                 '<button type="button" id="tour-again" class="s-item"><span class="s-label">How Tacitus works</span>',
                 '<p id="settings-conn" class="s-key"></p>'):
        assert part in dlg, part
    assert dlg.index('id="go-notify"') < dlg.index('id="pair-phone"') < dlg.index('id="go-routing"') < dlg.index('id="tour-again"')
    for part in ('<p id="settings-local">', '<ul id="settings-rows">', '<p id="settings-note" aria-live="polite">',
                 '<button type="button" id="settings-save" class="btn primary">Save</button>'):
        assert part in dlg[dlg.index('id="routing-settings"'):], part
    for part in ('<section id="notify-settings" aria-labelledby="settings-title" hidden>',
                 '<input type="checkbox" role="switch" name="briefings"> Briefings',
                 '<input type="checkbox" role="switch" name="replies"> Replies',
                 '<input type="checkbox" role="switch" name="rooms"> Rooms',
                 '<input type="radio" name="quiet" value="queue"> Hold until morning',
                 '<input type="radio" name="quiet" value="send"> Send anyway',
                 '<ul id="push-subs" class="subs" aria-labelledby="subs-title"></ul>',
                 '<section id="routing-settings" aria-labelledby="settings-title" hidden>'):
        assert part in dlg, part
    assert dlg.index('id="notify-settings"') < dlg.index('id="routing-settings"')
    js = read("app.js")
    assert "call('GET', '/settings/routing')" in js
    assert "const out = await call('POST', '/settings/routing', presetPick ? { preset: presetPick } : body);" in js
    assert "renderSettings(await call('POST', '/settings/routing', { hosted_default: model }));" in js  # the pick alone
    assert "call('GET', '/settings/notify')" in js and "call('POST', '/settings/notify', {" in js
    assert "notify: pages.includes('notify') && Boolean(S.me.push), routing: pages.includes('routing')" in js
    assert "$('notify-kinds').hidden = T.relay || !shown.notify;" in js
    assert "$('go-routing').hidden = T.relay || !shown.routing;" in js
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
