"""The web app in a browser: the app teaches itself.

A welcome shown once per browser (and again from the drawer) whose
examples fill the composer, a New room dialog that asks the bot in the EA
channel, and empty lists that say what each place is for.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    DM_NOTE, PHONE, SIDEWAYS, SIZES, SMALL_TARGETS, app, browser, stub_factory, wait_for,
)
from plain_words import JARGON  # noqa: E402


def _drawer(page, size):
    if size == "phone":
        page.click("#menu")


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
    expect(card).to_contain_text("a request lands in Action requests with a Send or Create button. "
                                 "Nothing is sent until you tap it.")
    expect(page.locator("#welcome-dots")).to_have_attribute("aria-label", "Card 2 of 3")
    page.keyboard.press("ArrowRight")
    expect(card.locator("h2")).to_have_text("Needs you")
    expect(card).to_contain_text("The Needs you list at the top gathers the conversations waiting on your answer. "
                                 "Buttons to tap wait in Action requests. Tap Mark done when a conversation is handled.")
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
    # Settings, then "How Tacitus works" opens it again at the first card, the menu closed; Escape closes it.
    _drawer(page, size)
    page.click("#open-settings")
    page.click("#tour-again")
    expect(dlg).to_be_visible()
    expect(page.locator("#settings")).to_be_hidden()
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


def test_no_new_room_button_in_a_mode_without_rooms(app):
    """The assistant mode has one chat and no project rooms: /api/me says
    rooms false and the app shows no New room button."""
    from playwright.sync_api import expect
    page, _stub, errors = app(rooms=False)
    page.wait_for_selector(".thread-item")
    expect(page.locator("#new-room")).to_be_hidden()
    page2, _stub2, _ = app(rooms=True)
    page2.wait_for_selector(".thread-item")
    expect(page2.locator("#new-room")).to_be_visible()
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
    expect(page.locator("#channel-list .c-name", has_text=re.compile("^spring-tour$"))).to_have_count(1)
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
    expect(empty.locator("p")).to_have_text("Ask for something. For example:")  # Ask Tacitus opens first
    expect(page.locator("#thread-list .filters")).to_have_count(0)  # nothing to filter
    page.evaluate("location.hash = '#/stream%3A7'")
    expect(page.locator("#thread-list .place-note")).to_have_text(DM_NOTE)
    expect(empty).to_have_text("Nothing waits for you now.")
    for where, says in (("r1", "This room is for one project. Ask about it here and Tacitus keeps track of it."),
                        ("stream%3A11", "Tacitus finds meeting times here."),
                        ("digests", "Tacitus posts your mail digests here; it does not read replies.")):
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
