"""The web app in a browser: installing on a phone, links and notifications.

iOS Safari shows the install line until it is hidden, the installed app
shows none, Chrome offers its button; permalinks and links to the bot's
own address stay in the app, other links open outside; a tapped
notification routes the open window; the composer stays above the
keyboard; and the service worker caches no API response.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    FAKE_PROMPT, FAKE_VIEWPORT, _function, app, browser, open_thread, read, stub_factory, wait_for,
)


# The drawer foot's entries on show.
FOOT = "[...$('drawer-foot').children].filter((el) => !el.hidden).map((el) => el.id)"


# -- installing on a phone -------------------------------------------------

# navigator.standalone exists on iOS only: false in Safari, true in the app
# opened from the Home Screen.
IOS_SAFARI = "Object.defineProperty(Navigator.prototype, 'standalone', { get: () => false, configurable: true });"


IOS_APP = "Object.defineProperty(Navigator.prototype, 'standalone', { get: () => true, configurable: true });"


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
    # The stub offers no push and no relay: the foot keeps Settings.
    assert page.evaluate(FOOT) == ["open-settings"]
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
    assert page.evaluate(FOOT) == ["open-settings"]
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
        '<p><a href="#/stream%3A9">weekly</a> and <a href="#/r1">room</a></p>')
    # Another origin, and this origin's other paths (a stored file), open outside the app's window.
    out = md(f"[file]({base}api/files/f1) [away](https://e.example/chat/#/x) {base}")
    assert out.count('target="_blank" rel="noopener noreferrer"') == 3, out
    page.evaluate("(s) => $('thread-list').prepend(markdown(s))", f"[weekly]({base}#/stream%3A9)")
    page.click("#thread-list a:has-text('weekly')")
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    assert len(page.context.pages) == 1
    assert not errors, errors


BOT_URL = "https://host.example.test:8444/chat"  # the bot's own address, as its links carry it


def test_a_link_to_the_bots_own_address_routes_in_place(app):
    """The bot builds its links on its own address (public_url), and this
    copy of the app may run at another: the website's copy, or a page on the
    bot's machine. A link to any address the app knows for itself, with a
    #/ route, routes in place and opens no window; an address it does not
    know still opens outside the app."""
    from urllib.parse import quote
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(public_url=BOT_URL)
    open_thread(page, "Draft a reply")
    base = page.url.split("#")[0]
    week = next(m for m in stub.messages if m["thread_key"] == W.WEEKLY)
    route = "#/" + "/".join(quote(x, safe="") for x in (W.MAIN, W.WEEKLY, week["id"]))
    stub.emit_bot(W.MAIN, f":lock: The Create button for this event is in your DM: [open it]({BOT_URL}/{route})",
                  thread_key=stub.messages[0]["thread_key"])
    opened = []
    page.context.on("page", lambda p: opened.append(p))
    link = page.locator("#messages a:has-text('open it')")
    expect(link).to_have_attribute("href", route)
    assert link.get_attribute("target") is None
    link.click()
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    expect(page.locator("#messages h3")).to_have_text("Monday")
    assert page.url == base + route
    page.wait_for_timeout(200)
    assert not opened and len(page.context.pages) == 1

    # The decision itself: the address with its base path, and a #/ route.
    def own(url):
        return page.evaluate("(u) => ownRoute(u)", url)
    assert own(f"{BOT_URL}/#/stream%3A9") == "#/stream%3A9"
    assert own("https://HOST.example.test:8444/chat/#/r1") == "#/r1"  # a host is any case
    assert own(base + "#/r1") == own("/chat/#/r1") == "#/r1"  # this page's own address
    for away in ("https://host.example.test/chat/#/r1",  # another port
                 f"{BOT_URL}/api/files/f1#/r1", f"{BOT_URL}/?x=1#/r1", f"{BOT_URL}/#/", f"{BOT_URL}/",
                 "https://user@host.example.test:8444/chat/#/r1", "//host.example.test:8444/chat/#/r1",
                 "https://e.example/chat/#/r1", "javascript:alert(1)//#/r1", "#/r1"):
        assert own(away) == "", away
    # The relay's copy of this browser's pairing is the app too.
    page.evaluate("RELAY = { rec: { relay: 'https://relay.example.test/relay' } }")
    assert own("https://relay.example.test/relay/#/r1") == "#/r1"
    page.evaluate("RELAY = null")
    # A tapped notification on the bot's own address routes this window too.
    page.evaluate("(u) => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { open: u } }))",
                  f"{BOT_URL}/#/stream%3A11")
    expect(page.locator(".chan.active .c-name")).to_have_text("Scheduling")
    assert not opened and not errors, errors


def test_a_tapped_notification_keeps_its_route_on_another_copy(app):
    """The worker opens a push's url when it is inside the app; a url on the
    bot's own address, outside this copy, opens this copy at the url's #/
    route; anything else opens the app's start page."""
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    src = _function(read("bundle.js"), "appUrl")
    cases = {"https://copy.example.test/#/r1": "https://copy.example.test/#/r1",
             f"{BOT_URL}/#/stream%3A6/t%3A1/w1": "https://copy.example.test/#/stream%3A6/t%3A1/w1",
             f"{BOT_URL}/": "https://copy.example.test/",
             "https://e.example/x#/a#b": "https://copy.example.test/",
             "": "https://copy.example.test/"}
    got = page.evaluate("(() => { const scopeUrl = () => new URL('https://copy.example.test/');\n"
                        + src + "\nreturn " + json.dumps(list(cases)) + ".map(appUrl); })()")
    assert got == list(cases.values())


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
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    page.evaluate(post, base + "#/stream%3A9")
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    assert page.url == base + "#/stream%3A9"
    assert not errors, errors


def test_the_composer_stays_above_the_keyboard(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(viewport={"width": 390, "height": 844}, init=FAKE_VIEWPORT)
    open_thread(page, "Draft a reply")
    tk = stub.messages[0]["thread_key"]
    for i in range(25):
        stub.emit_bot(W.MAIN, f"Line {i}", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('Line 24')")).to_be_in_viewport()
    # The keyboard opens for the focused composer: the app takes the visual
    # viewport's height, at the top, and the thread keeps its newest message in view.
    page.focus("#text")
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
