"""The fixtures and helpers of the web app's browser tests.

The browser tests are in tests/test_webui_<area>.py, a file per area of the
app (the thread, the composer, the drawer, the relay, Settings, ...), so the
wall runs them side by side (tests/run_wall.py) and no file nears its time
limit; tests/test_webui.py holds the checks that need no browser. A file
imports what it uses from here, and with a fixture the fixtures it asks
for, since pytest finds a fixture by its name in the test's own module:

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from webui_browser import app, browser, stub_factory, wait_for  # noqa: E402,F401

`browser` is one Chromium per test file (each file that imports the
fixture holds its own, so several files also run in one pytest process);
`app(...)` opens the app on a fresh stub (tests/webui_stub_server.py) in a
fresh browser context, so no test sees another's storage, cookies or
service worker.

A stable test waits for what the person would see, with a web-first
assertion (`expect(...)`), never for a fixed time, and acts on an element
once the page has stopped moving it (CONTRIBUTING.md, Browser tests):
  - `echoed` after a post: the stub answers 0.3 s later (an eyes reaction,
    a reply and a check, as the bot does), and that answer moves the thread
    and draws the message again;
  - `drawer_drawn` before a step that holds a drawer row: Recent's first
    read draws the drawer again 300 ms after the app starts;
  - `wait_for` on a stub's record (`stub.posts`, `stub.clicks`, ...): the
    page can show a click's effect before its request reaches the stub.
"""
import inspect
import json
import re
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))


WEBUI = TESTS.parent / "botcore" / "botcore" / "chat" / "webui"


def read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    """The source of one function of the worker's code, up to the next blank line."""
    start = src.index(f"function {name}(")
    return src[start:src.index("\n\n", start)]


# -- the browser, the stub and the app -------------------------------------

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
    """fn's first true answer, asked every 20 ms for `timeout` seconds. On a
    stub's record, use it rather than a bare assert: a request the page
    sends after a click can land after the click's own check in the page."""
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.02)
    try:
        what = inspect.getsource(fn).strip()
    except (OSError, TypeError):
        what = repr(fn)
    raise AssertionError(f"timed out after {timeout} s waiting for: {what}")


def open_thread(page, title):
    page.click(f".thread-item:has-text({json.dumps(title)})")
    page.wait_for_selector("#messages .msg")


def settled(page):
    """Two animation frames from now: by then the page has dispatched the
    scroll events and the resize observations of what it last drew."""
    page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")


def drawer_drawn(page):
    """Once the drawer is drawn with Recent. The app reads Recent 300 ms
    after it starts (recentSoon) and draws the drawer again, which replaces
    every row in it; a step that holds a row (scroll_into_view_if_needed,
    a bounding box) taken before then fails with the row detached."""
    wait_for(lambda: page.evaluate("S.recentAsOf >= 0"))
    settled(page)


def echoed(page, stub, text):
    """The owner's message `text` once the stub's answer to it shows, and
    its locator. The stub answers a post 0.3 s later: an eyes reaction, the
    bot's reply (which scrolls the thread to its end, and a scroll closes an
    open strip) and a check (which draws the message again). A test that
    acts on the message before then races that answer."""
    from playwright.sync_api import expect
    m = wait_for(lambda: next((x for x in stub.messages if x["author"] == "owner" and x["text"] == text
                               and x.get("reactions") == ["white_check_mark"]), None))
    msg = page.locator(f"#messages .msg[data-id='{m['id']}']")
    expect(msg.locator(".chip[title=':white_check_mark:']")).to_be_visible()
    settled(page)
    return m, msg


# Records each class of the live dot and each text of the Settings line.
WATCH_LIVE = """() => { window.__live = []; window.__line = [];
    new MutationObserver(() => __live.push($('live').className)).observe($('live'), { attributes: true });
    new MutationObserver(() => __line.push($('settings-conn').textContent))
        .observe($('settings-conn'), { childList: true, characterData: true, subtree: true }); }"""


# document.hidden and visibilityState as a test sets them; __hide(on) changes both and says so.
FAKE_HIDDEN = """() => { window.__hidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__hidden });
    Object.defineProperty(document, 'visibilityState', { configurable: true,
        get: () => (window.__hidden ? 'hidden' : 'visible') });
    window.__hide = (on) => { window.__hidden = on; document.dispatchEvent(new Event('visibilitychange')); }; }"""


SUBS = [{"id": "s1", "host": "push.example.com", "created_at": 1767225600, "last_seen": None, "last_ok": None, "failures": 0},
        {"id": "s2", "host": "notifications.a-long-made-up-push-service.example.org", "created_at": 1767312000,
         "last_seen": None, "last_ok": None, "failures": 0}]


# The controls a finger presses, and the ones under 44px of the visible ones.
SMALL_TARGETS = """() => [...document.querySelectorAll(
    'button, select, textarea, summary, a.chan, a.thread-item, label.toggle')].filter((el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || getComputedStyle(el).visibility === 'hidden' || r.bottom < 0 || r.top > innerHeight) return false;
  return r.height < 44 || (el.classList.contains('icon') && r.width < 44);
}).map((el) => `${el.tagName} ${el.id || el.className} ${Math.round(el.getBoundingClientRect().height)}`)"""


SIDEWAYS = "[document.documentElement.scrollWidth - innerWidth, $('messages').scrollWidth - $('messages').clientWidth]"


# -- shared by several areas --------------------------------------------------

PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3}}


SIZES = pytest.mark.parametrize("size", ["phone", "desktop"])


def open_devices(page):
    """Settings, then Devices: the "Add a device" dialog, the menu closed."""
    page.click("#open-settings")
    page.click("#pair-phone")


# What Action requests says it is for, above its requests and as its empty note.
DM_NOTE = "Drafts, meetings and other requests wait here for your tap. Nothing goes out until you tap Send or Create."


def _request(stub, title="Send this reply?", at=None, event="card", **card):
    """A card in Action requests that waits for a tap, as the bot posts one."""
    import webui_stub_server as W
    return stub.add(W.DM, "", kind="card", at=at, event=event, card={
        "kind": "send", "title": f":lock: *{title}*", "fields": [], "lines": [], "body": "Tuesday works.",
        "quoted": "", "actions": [{"id": "send", "label": "Send", "value": "s", "style": "primary", "word": "send"}],
        "select": None, "block_id": "", "footer": "", "fallback_text": title, "thread_ts": "",
        "ea_channel": W.MAIN, "content_fingerprint": "", "no_default": False, **card})


def reply_quiet(value) -> list[dict]:
    """The two quiet choices of every Replies row card, as the bot posts
    them on the web chat (ea-chat-bot._reply_quiet): Later, a day from
    tomorrow to a year on that starts three days on, and No reply needed."""
    from datetime import date, timedelta
    day = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731
    return [{"id": "reply_skip", "label": "Later", "value": value, "style": "", "word": "later",
             "ask": "The day it comes back", "date": {"min": day(1), "max": day(366), "proposed": day(3)}},
            {"id": "reply_not_needed", "label": "No reply needed", "value": value, "style": "danger",
             "word": "no reply needed", "ask": ""}]


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


def _worker_state(page, name):
    """One entry of the service worker's state (the trusted manifest, a
    refusal), as the page reads it from Cache Storage."""
    return page.evaluate("""async (name) => {
        const scope = (await navigator.serviceWorker.ready).scope;
        const hit = await (await caches.open('tacitus-webchat-v3-bundle')).match(new URL(`__bundle/${name}`, scope).href);
        return hit ? hit.json() : null;
    }""", name)


# -- the relay (docs/webchat-relay.md) -----------------------------------

ROOT = TESTS.parent


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


def _relay_bot(tmp_path, static, webui=WEBUI):
    pytest.importorskip("cryptography")
    sys.path.insert(0, str(ROOT / "relay"))
    sys.path.insert(0, str(ROOT / "botcore"))
    import threading
    import relay_server as rs
    from botcore.chat.web_frontend import WebFrontend
    relay = rs.Relay(rs.Limits(keepalive=0.5, client_timeout=15), log=lambda line: None)
    rsrv = rs.RelayHTTPServer(("127.0.0.1", 0), relay, str(static))
    threading.Thread(target=rsrv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
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
    """The owner opens "Add a device" on the tailnet (Another phone, on a
    computer); the phone, on the
    tailnet too and signed in, opens the link and shows its code; the owner
    approves the phone whose code matches; then the phone leaves the
    tailnet. The phone runs the service worker when `workers`. (owner page,
    phone page, phone errors, URLs the phone asked)."""
    from playwright.sync_api import expect
    fe = relay_bot["fe"]
    tail = _owner_context(browser, fe, contexts).new_page()
    tail.goto(relay_bot["public"] + "/")
    open_devices(tail)
    expect(tail.locator("#phone-devices")).to_contain_text("No device is set up yet.")
    expect(tail.locator("#phone-self")).to_contain_text("This browser is not set up yet.")
    expect(tail.locator("#way-phone")).to_have_attribute("aria-pressed", "true")
    wait_for(lambda: tail.input_value("#phone-link"), 10)
    link = tail.input_value("#phone-link")
    assert link.startswith(relay_bot["public"] + "/#tr2=")
    # The phone the owner holds has no second screen to scan from: "This
    # computer" ("This phone" on a phone) pairs this very browser from the
    # same link, with no navigation.
    expect(tail.locator("#way-here")).to_have_text("This computer")
    tail.click("#way-here")
    expect(tail.locator("#phone-here")).to_be_visible()
    assert tail.input_value("#phone-link") == link  # the same pairing serves both ways
    tail.evaluate("() => { window.__paired = []; pairFromLink = (k, t, s) => { window.__paired.push([k, t, s]); }; }")
    tail.click("#phone-here")
    assert tail.evaluate("window.__paired") == [["tr2", link.split("#tr2=")[1], True]]
    assert not tail.evaluate("document.getElementById('phone').open")
    open_devices(tail)
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
    open_devices(page)
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
    expect(page.locator("#channel-list .chan .c-name").first).to_be_visible(timeout=15000)
    expect(page.locator("#live.on")).to_be_attached(timeout=10000)
    return tail, page, errors, asked


def _kept(page, name):
    """The service worker's copy of one file of the app, as text; None without one."""
    return page.evaluate("""async (name) => {
        const hit = await (await caches.open('tacitus-webchat-v3')).match(name === 'index.html' ? './' : name);
        return hit ? hit.text() : null;
    }""", name)
