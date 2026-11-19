"""The web app in a browser: the state of a thread (docs/webchat.md, The web
app).

Each thread card carries its state's sticker, Needs you gathers the
threads that wait on the owner (this week apart from the older shelf),
the chips filter a channel, and the owner's override posts and follows
the bot's `thread_state` events.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    SIDEWAYS, SMALL_TARGETS, app, browser, open_thread, read, stub_factory, wait_for,
)


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
    expect(count).to_have_text("2")  # needs you, in Ask Tacitus and in the room; the waiting one is not counted
    expect(count).to_have_attribute("aria-label", "2 conversations need you")
    expect(page.locator("#channel-list .chan .c-name").first).to_have_text("Ask Tacitus")  # the channels follow it
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
        "example-project", "Ask Tacitus", "Scheduling"]
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
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
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
    expect(page.locator("#thread-list .empty")).to_have_text("Nothing here is waiting.")
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
    assert stub.state_posts == [{"channel": W.MAIN, "thread_key": tk, "state": "done"}]
    card = _card(page, "Draft a reply")
    expect(card.locator(".sticker")).to_have_text("Done")
    expect(card).to_have_attribute("title", "You set this.")  # the bot's reason, from its answer and event
    expect(btn).to_have_text("Needs me")
    expect(page.locator("#needs-count")).to_have_text("1")
    # A rule moves it back: the event alone changes the sticker and the button.
    stub.set_state(W.MAIN, tk, "needs_you", reason="A new message asks for you.")
    expect(card.locator(".sticker.st-needs_you")).to_be_visible()
    expect(card).to_have_attribute("title", "A new message asks for you.")
    expect(btn).to_have_text("Mark done")
    expect(page.locator("#needs-count")).to_have_text("2")
    # Needs me sends needs_you, once its label has stood long enough to be
    # the one aimed at: a tap right after a rule flipped it (a card's Close,
    # then Mark done at once) was aimed at the old label and does nothing.
    stub.set_state(W.MAIN, tk, "done", reason="The card was acted on.")
    expect(btn).to_have_text("Needs me")
    btn.click()
    page.wait_for_timeout(400)
    assert len(stub.state_posts) == 1
    page.wait_for_timeout(1500)
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
    assert stub.state_posts[0]["state"] == "done" and stub.state_posts[0]["channel"] == W.MAIN
    expect(card.locator(".sticker")).to_have_text("Done")
    css = read("app.css")
    assert "-webkit-touch-callout: none; -webkit-user-select: none; user-select: none;" in css
    assert not errors, errors


def test_needs_you_keeps_this_week_apart_from_the_older_shelf(app):
    from playwright.sync_api import expect
    page, stub, errors = app(viewport={"width": 390, "height": 844},
                             context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    old = stub.add_stale()
    page.reload()
    page.wait_for_selector(".thread-item")
    count = page.locator("#needs-count")
    expect(count).to_have_text("2")  # this week's only: the two old ones wait under Older
    page.click("#menu")
    page.click("#needs-link")
    expect(page.locator("#thread-list h2.needs-head")).to_have_text("This week")
    items = page.locator("#thread-list .thread-item")
    expect(items).to_have_count(3)
    toggle = page.locator("#older-toggle")
    expect(toggle).to_have_text("Older (3)")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator(".older .thread-item")).to_have_count(0)  # shut at first
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    older = page.locator(".older .thread-item")
    expect(older.locator(".sticker")).to_have_text(["Needs you · 9 days", "Needs you · 12 days",
                                                    "Waiting · 16 days"])
    expect(older.first.locator(".sticker")).to_have_class(re.compile(r"\bstale\b"))
    expect(older.first.locator(".sticker")).to_have_attribute("aria-label", "Needs you · 9 days: A question waits for you.")
    # muted: an old sticker takes neither the accent nor the amber
    bg = "(sel) => getComputedStyle(document.querySelector(sel)).backgroundColor"
    fresh_bg, stale_bg = page.evaluate(bg, ".sticker.st-needs_you:not(.stale)"), page.evaluate(bg, ".sticker.stale")
    assert stale_bg != fresh_bg and stale_bg != page.evaluate(bg, ".sticker.st-waiting:not(.stale)")
    assert page.evaluate(SMALL_TARGETS) == []
    assert page.evaluate(SIDEWAYS)[0] == 0
    # Mark all done asks once, in plain words; No changes nothing
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.click("#older-done")
    wait_for(lambda: asked)
    assert asked == ["Mark all 3 older items done? They leave Needs you. Nothing is sent or deleted."]
    assert not stub.state_posts
    page.once("dialog", lambda d: d.accept())
    page.click("#older-done")
    wait_for(lambda: len(stub.state_posts) == 3)
    assert sorted((p["channel"], p["thread_key"], p["state"]) for p in stub.state_posts) == \
        sorted((m["channel"], m["thread_key"], "done") for m in old)
    expect(page.locator("#thread-list .older")).to_have_count(0)
    expect(page.locator("#thread-list h2.needs-head")).to_have_count(0)
    expect(items).to_have_count(3)
    expect(count).to_have_text("2")
    assert not errors, errors


def test_a_state_the_bot_shelves_leaves_the_count_and_shows_its_age(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    count = page.locator("#needs-count")
    expect(count).to_have_text("2")
    expect(page.locator("#live.on")).to_be_attached()  # the stream is open: the events below reach the page
    page.click("#needs-link")
    expect(page.locator("#older-toggle")).to_have_count(0)
    expect(page.locator("#thread-list h2.needs-head")).to_have_count(0)
    tk = stub.messages[0]["thread_key"]
    stub.set_state(W.MAIN, tk, "needs_you", reason="The card still has its Send button.",
                   at=time.time() - 8 * 86400 - 60, stale=True)
    expect(count).to_have_text("1")
    expect(page.locator("#older-toggle")).to_have_text("Older (1)")
    expect(page.locator("#thread-list h2.needs-head")).to_have_text("This week")
    expect(page.locator("#thread-list .thread-item")).to_have_count(2)  # the room's and the waiting one
    # in its own channel the card keeps a muted sticker with its age, which
    # counts from the last message (the stub's, three days ago less a minute),
    # not from the state's eight days, as the bot ages the shelf
    page.click(f'a.chan[href="#/{W.MAIN.replace(":", "%3A")}"]')
    sticker = _card(page, "Draft a reply").locator(".sticker")
    expect(sticker).to_have_text("Needs you · 2 days")
    expect(sticker).to_have_class(re.compile(r"\bstale\b"))
    # a state the owner set ages from the owner's tap
    stub.set_state(W.MAIN, tk, "needs_you", "owner", "You set this.", at=time.time() - 10 * 86400 - 60, stale=True)
    expect(sticker).to_have_text("Needs you · 10 days")
    # the owner's Needs me makes it fresh again
    stub.set_state(W.MAIN, tk, "needs_you", "owner", "You set this.")
    expect(sticker).to_have_text("Needs you")
    expect(count).to_have_text("2")
    assert not errors, errors


def test_a_list_read_before_a_state_event_does_not_undo_it(app):
    """A Needs you read the bot answered before a thread's state changed,
    and that reaches the page after the change's event, leaves the event's
    state. The read after the event brings the bot's state, though a shelved
    state's time is in the past."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    count = page.locator("#needs-count")
    expect(count).to_have_text("2")
    expect(page.locator("#live.on")).to_be_attached()  # the stream is open: the event below reaches the page
    page.evaluate("window.__reads = 0; const read = loadNeeds; loadNeeds = async () => { await read(); window.__reads++; }")
    held = []

    def hold(route):  # the first read: answered by the bot now, given to the page later
        if held:
            return route.continue_()
        held.append((route, route.fetch()))
    page.route(re.compile(r".*/api/needs$"), hold)
    page.click("#needs-link")
    wait_for(lambda: page.wait_for_timeout(20) or held)  # a Playwright call, so the handler runs
    tk = stub.messages[0]["thread_key"]
    stub.set_state(W.MAIN, tk, "needs_you", reason="The card still has its Send button.",
                   at=time.time() - 8 * 86400 - 60, stale=True)
    expect(count).to_have_text("1")
    route, answer = held[0]
    route.fulfill(response=answer)
    wait_for(lambda: page.evaluate("window.__reads") >= 2)  # the held read, and the one the event asked for
    expect(count).to_have_text("1")
    expect(page.locator("#older-toggle")).to_have_text("Older (1)")
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
