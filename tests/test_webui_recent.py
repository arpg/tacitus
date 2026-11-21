"""The web app in a browser: Recent and the drawer's parts (docs/webchat.md,
Recent).

The drawer opens with Recent, the latest conversations of every place in
the bot's order (never a card), the same on a phone and a computer, then
Ask Tacitus and Action requests, then More; Action requests says what it
is for, takes taps only and counts the requests that wait. The app opens
on Ask Tacitus.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    DM_NOTE, PHONE, SIDEWAYS, SIZES, SMALL_TARGETS, WELCOMED, _request, app, browser, open_thread,
    stub_factory, wait_for,
)
from plain_words import JARGON  # noqa: E402


# -- Recent and the drawer's parts (docs/webchat.md, Recent) ----------------

RECENT = "#channel-list .recent .recent-item"


# The drawer's list from the top: Recent, each place by its name, More, Archived.
PARTS = """[...$('channel-list').children].map((el) => el.matches('section.recent') ? 'Recent'
  : el.matches('h2') ? el.textContent : el.matches('hr') ? '---' : el.matches('details') ? 'Archived'
  : el.querySelector('.c-name').textContent)"""


def _talk(stub):
    """Conversations for Recent, two hours old: one the owner wrote in per
    talk place, a briefing the bot opened, and one the owner typed in the
    DM before it took taps only."""
    import webui_stub_server as W
    t = time.time() - 7200
    return [stub.add(W.SCHED, "Find a time with the review panel.", author="owner", at=t),
            stub.add(W.ROOM, "What is left on the budget table?", author="owner", at=t + 60),
            stub.add(W.MAIN, "Morning briefing\n\nTwo meetings today.", at=t + 120),
            stub.add(W.MAIN, "Book the room for Friday.", author="owner", at=t + 180),
            stub.add(W.DM, "A quick ask from before.", author="owner", at=t + 240)]


@SIZES
def test_recent_heads_the_drawer_and_opens_a_conversation_in_place(app, size):
    """Recent lists the latest conversations of every place, newest first,
    each with its place, its time and its count; five show until Show more.
    Under it Ask Tacitus and Action requests, then More with the other
    places. A tap opens the conversation in place (on a phone the drawer
    closes on it), and reading it clears its count."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    sched = _talk(stub)[0]
    page.reload()
    page.wait_for_selector(RECENT, state="attached")  # in the drawer, shut on a phone
    assert page.evaluate(PARTS) == ["Recent", "Ask Tacitus", "Action requests", "More", "Scheduling",
                                    "Mail digests", "---", "example-project", "Archived"]
    rows = page.locator(RECENT)
    expect(rows).to_have_count(5)
    expect(rows.first.locator(".r-title")).to_have_text("A quick ask from before.")
    expect(rows.first.locator(".r-where")).to_contain_text("Action requests · ")
    new = stub.emit_bot(W.SCHED, "Thursday at ten works for the panel.", thread_key=sched["thread_key"])
    expect(rows.first.locator(".r-title")).to_have_text("Find a time with the review panel.")
    expect(rows.first.locator(".r-where")).to_contain_text("Scheduling · ")
    expect(rows.first.locator(".badge")).to_have_text("1")
    if size == "phone":
        page.click("#menu")
        wait_for(lambda: page.evaluate("document.getElementById('channels').getBoundingClientRect().left >= 0"))
        assert page.evaluate(SMALL_TARGETS) == []
        assert page.evaluate(SIDEWAYS) == [0, 0]
    more = page.locator("#recent-more")
    expect(more).to_have_text("Show more")
    more.click()
    expect(rows).to_have_count(7)  # every conversation: the five above and the two seeded ones
    expect(more).to_have_attribute("aria-expanded", "true")
    expect(more).to_have_text("Show less")
    expect(more).to_be_focused()
    rows.first.click()
    expect(page).to_have_url(re.compile(re.escape(f"#/stream%3A11/{new['thread_key']}") + "$"))
    expect(page.locator("#messages .msg")).to_have_count(2)
    expect(page.locator("#thread-title")).to_have_text("Find a time with the review panel.")
    if size == "phone":
        assert page.evaluate("document.body.classList.contains('drawer')") is False
        expect(page.locator("#thread")).to_be_visible()
    else:
        expect(page.locator("#channel-name")).to_have_text("Scheduling")
        expect(rows.first).to_have_class(re.compile(r"\bactive\b"))
    wait_for(lambda: any(p["thread_key"] == new["thread_key"] for p in stub.read_posts))
    expect(rows.first.locator(".badge")).to_have_count(0)
    assert not errors, errors


def test_recent_lists_conversations_only_never_a_request(app):
    """Recent holds conversations: a thread the owner wrote in and a
    briefing. A card in Action requests, read or unread, a card's status
    line, a notice the bot opened and a digest stay out; so does a card's
    thread from an answer that lists it (an older bot). A notice joins once
    the owner writes in it."""
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    _talk(stub)
    page.reload()
    page.wait_for_selector(RECENT)
    titles = lambda: page.locator(f"{RECENT} .r-title").all_text_contents()  # noqa: E731
    gets = stub.recent_gets
    waiting = _request(stub, "Send this reply?")
    stub.add(W.DM, ":white_check_mark: Sent.", kind="status", event="status", card={**waiting["card"], "title": "Sent one"})
    notice = stub.emit_bot(W.MAIN, "The calendar check found no clash.")
    stub.emit_bot(W.DIGEST, "Your mail digest for Tuesday.")
    caught_up = lambda: page.evaluate("S.recentAsOf") >= stub.eseq  # noqa: E731  (Recent as of the last event)
    wait_for(lambda: stub.recent_gets > gets and caught_up())
    shown = titles()
    assert shown[0] == "A quick ask from before." and "Morning briefing" in shown
    for out in ("Send this reply?", "Sent one", "Your mail digest for Tuesday.", notice["text"]):
        assert out not in shown, out
    stub.mark_read(W.DM, waiting["thread_key"])  # read, it stays out
    wait_for(caught_up)
    assert "Send this reply?" not in titles()
    # the owner writes in the notice's thread: now it is a conversation
    stub.add(W.MAIN, "Thanks, I will look.", author="owner", thread_key=notice["thread_key"], event="message")
    wait_for(lambda: titles()[:1] == ["The calendar check found no clash."])
    # an answer that lists a card's thread or a thread the bot opened in the DM: the page leaves it out
    page.evaluate("""([card, ch]) => { S.recent = [{ ...card, channel: ch, last_at: Date.now() / 1000 + 60 },
        ...S.recent]; renderChannels(); }""", [stub.row(waiting), W.DM])
    assert "Send this reply?" not in titles()
    assert not errors, errors


def test_recent_leaves_out_the_health_room_while_debug_mode_hides_it(app):
    """Debug mode off: the health room's conversation stays out of Recent,
    even from an answer that lists it. The switch on brings it in; off, it
    leaves again."""
    import webui_stub_server as W
    page, stub, errors = app(debug=False)
    page.wait_for_selector(".thread-item")
    report = next(m for m in stub.messages if m["channel"] == W.HEALTH)
    stub.add(W.HEALTH, "Which groups?", author="owner", thread_key=report["thread_key"])  # a conversation now
    page.reload()
    page.wait_for_selector(RECENT)
    shown = lambda: any("Reconcile" in t for t in page.locator(f"{RECENT} .r-title").all_text_contents())  # noqa: E731
    assert any("Reconcile" in r.get("text", "") for r in stub.recent()["threads"])  # the answer lists it
    assert not shown()
    page.locator("#open-settings").click()
    page.locator("#debug-row").click()
    wait_for(shown)
    page.locator("#debug-row").click()
    wait_for(lambda: not shown())
    assert not errors, errors


def test_two_devices_show_one_recent_list(browser, stub_factory):
    """Recent is the bot's order, not the page's: a phone and a computer on
    one bot list the same conversations in the same order, a message moves
    its conversation to the top on both, and a read on one clears its count
    on the other. An answer older than the list shown is dropped."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    url, stub = stub_factory()
    talk = _talk(stub)
    pages, errors = [], []
    for kw in (PHONE, {"viewport": {"width": 1280, "height": 800}}):
        ctx = browser.new_context(viewport=kw["viewport"], **kw.get("context", {}))
        ctx.add_init_script(WELCOMED)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector(RECENT, state="attached")
        pages.append(page)
    phone, computer = pages
    titles = lambda p: p.locator(f"{RECENT} .r-title").all_text_contents()  # noqa: E731
    try:
        assert titles(phone) == titles(computer) and len(titles(phone)) == 5
        room = talk[1]["thread_key"]
        stub.emit_bot(W.ROOM, "The budget table is in.", thread_key=room)
        for p in pages:
            wait_for(lambda p=p: titles(p)[0] == "What is left on the budget table?")
            expect(p.locator(f"{RECENT} >> nth=0 >> .badge")).to_have_text("1")
        assert titles(phone) == titles(computer)
        phone.click("#menu")
        phone.locator(RECENT).first.click()
        expect(phone.locator("#messages .msg")).to_have_count(2)
        expect(computer.locator(f"{RECENT} >> nth=0 >> .badge")).to_have_count(0)
        # an older answer than the list shown does not replace it
        computer.evaluate("S.recentAsOf = 1e9")
        stub.emit_bot(W.MAIN, "A newer message.", thread_key=talk[3]["thread_key"])
        wait_for(lambda: titles(phone)[0] == "Book the room for Friday.")
        computer.wait_for_timeout(600)
        assert titles(computer)[0] == "What is left on the budget table?"
        assert not errors, errors
    finally:
        for p in pages:
            p.context.close()


def test_a_name_the_owner_gave_titles_the_conversation_everywhere(app):
    """A conversation the owner named (`name this conversation`) shows the
    name in Recent, on its card in its place and in its header."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(RECENT)
    fig = next(m for m in stub.messages if m["text"] == "Here is the figure.")
    stub.names[fig["thread_key"]] = "Spring retreat plans"
    page.reload()
    page.wait_for_selector(RECENT)
    expect(page.locator(f"{RECENT} .r-title").first).to_have_text("Spring retreat plans")
    open_thread(page, "Spring retreat plans")  # its card in Direct messages, which opens first
    expect(page.locator("#thread-title")).to_have_text("Spring retreat plans")
    assert not errors, errors


@SIZES
def test_action_requests_takes_taps_only_and_says_so(app, size):
    """Action requests says what it is for above its requests. It has no
    composer and no New conversation: a line in the composer's place says
    to ask in Ask Tacitus, with a link there. Its count is the requests
    that wait for a tap, not unread messages; no other place shows the line."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    badge = page.locator('a.chan[href="#/stream%3A7"] .badge')
    expect(badge).to_have_count(0)  # the seeded request is answered
    req = _request(stub, "Send this reply?")
    expect(badge).to_have_text("1")
    expect(badge).to_have_attribute("aria-label", "1 waiting for you")
    stub.emit_bot(W.DM, "A notice that waits for nothing.")  # a message adds nothing to the count
    page.wait_for_timeout(500)
    expect(badge).to_have_text("1")
    page.evaluate("location.hash = '#/stream%3A7'")
    expect(page.locator("#channel-name")).to_have_text("Action requests")
    note = page.locator("#thread-list .place-note")
    expect(note).to_have_text(DM_NOTE)
    assert page.evaluate("$('thread-list').firstElementChild.className") == "place-note"
    expect(page.locator("#new-thread")).to_be_hidden()
    expect(page.locator("#composer")).to_be_hidden()
    off = page.locator("#thread-list .taps-note")  # at the foot of the requests: no box to type in
    expect(off).to_be_visible()
    expect(off).to_have_text("Here you only tap buttons. To ask Tacitus something, use Ask Tacitus.")
    assert not JARGON.search(off.inner_text())
    page.locator(".req:has-text('Send this reply?') .req-head").click()
    expect(page.locator(".req .card button:has-text('Send')")).to_be_visible()
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        assert page.evaluate(SIDEWAYS) == [0, 0]
    # its answer: the request leaves the count
    page.locator(".req .card button:has-text('Send')").click()
    wait_for(lambda: stub.clicks)
    expect(badge).to_have_count(0)
    assert [c["id"] for c in stub.clicks] == [req["id"]]
    off.locator("a").click()
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    expect(note).to_have_count(0)
    expect(page.locator("#new-thread")).to_be_visible()
    assert not errors, errors


def test_a_waiting_request_counts_in_the_tab_title_and_the_menu_dot(app):
    """The requests that wait for a tap count like unread messages: in the
    page's title and the menu button's dot, and leave them once answered."""
    from playwright.sync_api import expect
    page, stub, errors = app(**PHONE)
    page.wait_for_selector(".thread-item")
    expect(page).to_have_title("(2) Tacitus")  # the weekly plan's two unread replies
    expect(page.locator("#menu")).to_have_attribute("data-unread", "")  # that is the place on screen
    req = _request(stub, "Send this reply?")
    expect(page).to_have_title("(3) Tacitus")
    expect(page.locator("#menu")).to_have_attribute("data-unread", "1")
    stub.change(req, "status", kind="status", text=":white_check_mark: Sent.")
    expect(page).to_have_title("(2) Tacitus")
    expect(page.locator("#menu")).to_have_attribute("data-unread", "")
    assert not errors, errors


def test_the_app_opens_on_ask_tacitus(app):
    """With no place in its address, the app opens on Ask Tacitus, never on
    Action requests; an address that names a place still opens it."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    expect(page).to_have_url(re.compile("#/stream%3A9$"))
    page.goto(page.url.split("#")[0] + "#/stream%3A7")
    expect(page.locator("#channel-name")).to_have_text("Action requests")
    assert not errors, errors
