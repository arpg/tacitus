"""The web app in a browser: the owner's own actions.

The owner searches every conversation from the menu and lands on the hit,
reacts from a strip (a long press on a phone), edits or deletes their own
message in its first 15 minutes, and copies a code block with its own
Copy.

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
    PHONE, SIDEWAYS, SMALL_TARGETS, app, browser, echoed, open_thread, stub_factory, wait_for,
)


# -- the owner's own actions: search, reactions, edit and delete ----------

# A finger held still on the element `sel` matches for 700 ms, then lifted
# with the tap's click: the long press that opens a message's strip.
LONG_PRESS = """(sel) => new Promise((done) => {
    const r = document.querySelector(sel).getBoundingClientRect();
    const [x, y] = [r.left + 20, r.top + 12];
    const at = (type) => document.elementFromPoint(x, y).dispatchEvent(new (type === 'click' ? MouseEvent : PointerEvent)(
        type, { pointerType: 'touch', bubbles: true, cancelable: true, clientX: x, clientY: y }));
    at('pointerdown');
    setTimeout(() => { at('pointerup'); at('click'); setTimeout(() => done(location.hash), 50); }, 700);
})"""


def test_search_groups_the_hits_and_opens_a_conversation_at_its_hit(app):
    """The field at the top of the menu opens the search in the list's
    header; the hits come grouped by conversation with the words marked; a
    hit opens its conversation with that message ringed and marked, Back
    returns to the hits, and Escape ends the search."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    expect(page.locator("#search")).to_be_hidden()
    page.click("#search-open")
    expect(page.locator("#search-q")).to_be_focused()
    expect(page.locator("#search-q")).to_have_attribute("placeholder", "Search your conversations")
    expect(page.locator("#channel-name")).to_be_hidden()
    expect(page.locator("#thread-list .empty")).to_contain_text("Type a word or two")
    page.fill("#search-q", "budget")
    wait_for(lambda: any(a.get("q") == "budget" for a in stub.owner_actions))
    groups = page.locator(".s-group")
    expect(groups).to_have_count(1)  # the seeded thread: its question and the draft
    expect(groups.locator(".s-chan")).to_have_text("Ask Tacitus")
    expect(groups.locator(".s-hit").first.locator("mark")).to_have_text("budget")
    ask = stub.messages[0]
    stub.add(W.MAIN, "The budget covers both trips.", thread_key=ask["thread_key"], at=time.time() - 60)
    stub.add(W.SCHED, "The budget line for the trip is open.", at=time.time())
    page.fill("#search-q", "budget trip")
    expect(groups.first.locator(".s-chan")).to_have_text("Scheduling")  # newest first
    expect(groups).to_have_count(2)
    page.locator(".s-hit:has-text('both trips')").click()  # the card's text, in the draft's thread
    expect(page).to_have_url(re.compile(re.escape(ask["thread_key"]) + "$"))
    hit = page.locator("#messages .msg.hit")
    expect(hit).to_have_count(1)
    expect(hit.locator("mark").first).to_have_text(re.compile("budget|trip", re.I))
    assert page.evaluate("$('search').hidden") is False  # the hits stay beside the conversation
    page.press("#search-q", "Escape")
    expect(page.locator("#search")).to_be_hidden()
    expect(page.locator("#thread-list .thread-item").first).to_be_visible()
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    # One letter searches on Enter only; a place picked in the menu ends the search.
    page.click("#search-open")
    page.fill("#search-q", "x")
    page.wait_for_timeout(400)
    assert not any(a.get("q") == "x" for a in stub.owner_actions)
    page.press("#search-q", "Enter")
    wait_for(lambda: any(a.get("q") == "x" for a in stub.owner_actions))
    page.fill("#search-q", "nothing like this")
    expect(page.locator("#thread-list .empty")).to_have_text('Nothing found for "nothing like this".')
    page.click("#channel-list a.chan:has-text('Scheduling')")
    expect(page.locator("#search")).to_be_hidden()
    assert not errors, errors


def test_search_on_a_phone_is_one_pane_with_back(app):
    from playwright.sync_api import expect
    page, stub, errors = app(**PHONE)
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    assert page.evaluate(SMALL_TARGETS) == []
    page.click("#search-open")
    expect(page.locator("body")).not_to_have_class(re.compile("drawer"))
    page.fill("#search-q", "budget")
    page.locator(".s-hit").first.click()
    expect(page.locator("#thread")).to_be_visible()
    expect(page.locator("#threads")).to_be_hidden()
    expect(page.locator("#messages .msg.hit")).to_have_count(1)
    page.click("#back")
    expect(page.locator(".s-group").first).to_be_visible()  # Back returns to the hits
    assert page.evaluate(SMALL_TARGETS) == [] and page.evaluate(SIDEWAYS) == [0, 0]
    page.click("#search-close")
    expect(page.locator("#search")).to_be_hidden()
    expect(page.locator(".thread-item").first).to_be_visible()
    assert not errors, errors


def test_reactions_toggle_the_owner_s_own_beside_the_bot_s_marks(app):
    """The strip offers five reactions on any message; the owner's are kept
    apart from the bot's marks, counted with them in one chip and
    highlighted, and a tap on the chip takes the owner's away."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    ask = stub.messages[0]  # the owner's, three days old, with the bot's eyes and check
    own = page.locator(f"#messages .msg[data-id='{ask['id']}']")
    expect(own.locator(".chip")).to_have_count(2)
    own.hover()
    own.locator(".msg-act").click()
    strip = page.locator("#msg-strip")
    expect(strip.locator(".strip-emoji")).to_have_count(5)
    expect(strip.locator(".strip-emoji").first).to_be_focused()  # opened from the keyboard's way in
    expect(strip.locator("button.btn")).to_have_text(["Copy"])  # past 15 minutes: no Edit or Delete
    strip.locator(".strip-emoji[aria-label='Eyes']").click()
    expect(strip).to_have_count(0)
    wait_for(lambda: any(a.get("emoji") == "eyes" for a in stub.owner_actions))
    eyes = own.locator(".chip[title=':eyes:']")
    expect(eyes).to_have_class(re.compile(r"\bmine\b"))
    expect(eyes.locator(".n")).to_have_text("2")
    assert ask["reactions"] == ["eyes", "white_check_mark"]  # the bot's marks stay the bot's
    eyes.click()
    expect(eyes).not_to_have_class(re.compile(r"\bmine\b"))
    expect(eyes.locator(".n")).to_have_text("1")
    assert [a["emoji"] for a in stub.owner_actions if a.get("verb") == "react"] == ["eyes", "eyes"]
    # A reaction outside the owner's five (the bot's hourglass) is a plain chip.
    bot = next(m for m in stub.messages if m["text"].startswith("Draft created"))
    stub.change(bot, "reaction", reactions=["hourglass_flowing_sand"], updated_at=bot["updated_at"])
    chip = page.locator(f"#messages .msg[data-id='{bot['id']}'] .chip")
    expect(chip).to_have_attribute("title", ":hourglass_flowing_sand:")
    assert chip.evaluate("(el) => el.tagName") == "SPAN"
    assert not errors, errors


def test_a_long_press_on_a_phone_opens_the_strip(app):
    from playwright.sync_api import expect
    page, stub, errors = app(**PHONE)
    open_thread(page, "Draft a reply")
    bot = next(m for m in stub.messages if m["text"].startswith("Draft created"))
    hash_before = page.evaluate("location.hash")
    assert page.evaluate(LONG_PRESS, f"#messages .msg[data-id='{bot['id']}'] .text li") == hash_before
    strip = page.locator("#msg-strip")
    expect(strip).to_be_visible()
    expect(page.locator(".msg-act").first).to_be_hidden()  # a finger has no hover
    assert page.evaluate(SMALL_TARGETS) == [] and page.evaluate(SIDEWAYS) == [0, 0]
    box = strip.bounding_box()
    assert box["x"] >= 8 and box["x"] + box["width"] <= 390 - 8
    page.locator("#messages .msg").first.click(position={"x": 5, "y": 5})  # a tap elsewhere closes it
    expect(strip).to_have_count(0)
    assert page.evaluate("getComputedStyle(document.querySelector('.msg')).userSelect") == "none"
    assert not errors, errors


def test_the_owner_edits_and_deletes_their_own_message_in_its_first_15_minutes(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Here is the figure")
    page.fill("#text", "Plese send the figure")
    page.press("#text", "Enter")
    m, msg = echoed(page, stub, "Plese send the figure")  # the stub's reply scrolls the thread, which closes a strip
    page.fill("#text", "half typed")  # kept, and back after the edit
    msg.hover()
    msg.locator(".msg-act").click()
    expect(page.locator("#msg-strip button.btn")).to_have_text(["Edit", "Delete", "Copy"])
    page.click("#msg-strip button:has-text('Edit')")
    expect(page.locator("#editing")).to_be_visible()
    expect(page.locator("#editing")).to_contain_text("Editing your message")
    assert page.input_value("#text") == "Plese send the figure"
    assert page.evaluate("$('attach').disabled") is True
    expect(msg).to_have_class(re.compile(r"\bediting\b"))
    # A mark the bot puts on the message while the owner edits it draws the message again: the ring stays.
    stub.change(m, "reaction", reactions=["eyes"], updated_at=m["updated_at"])
    expect(msg.locator(".chip[title=':eyes:']")).to_be_visible()
    expect(msg).to_have_class(re.compile(r"\bediting\b"))
    expect(msg.locator(".edited")).to_have_count(0)
    m["posted_at"] -= 60  # a minute ago: an edit now shows as one
    page.fill("#text", "Please send the figure")
    page.press("#text", "Enter")
    wait_for(lambda: any(a.get("verb") == "edit" for a in stub.owner_actions))
    expect(msg.locator(".text")).to_have_text("Please send the figure")
    expect(msg.locator(".edited")).to_be_visible()
    expect(page.locator("#editing")).to_be_hidden()
    assert page.input_value("#text") == "half typed"
    assert len([p for p in stub.posts if p["thread_key"] == m["thread_key"]]) == 1  # an edit posts nothing new
    # Cancel puts back what was typed; Escape in the field does the same.
    msg.hover()
    msg.locator(".msg-act").click()
    page.click("#msg-strip button:has-text('Edit')")
    page.click("#edit-cancel")
    expect(page.locator("#editing")).to_be_hidden()
    assert page.input_value("#text") == "half typed"
    # The bot refuses an edit past the window, and the composer says why.
    msg.hover()
    msg.locator(".msg-act").click()
    page.click("#msg-strip button:has-text('Edit')")
    m["posted_at"] -= 16 * 60
    page.fill("#text", "Too late")
    page.press("#text", "Enter")
    expect(page.locator("#composer-note")).to_have_text(
        "Not changed: You can change a message only in its first 15 minutes.")
    page.press("#text", "Escape")
    expect(page.locator("#editing")).to_be_hidden()
    m["posted_at"] += 16 * 60
    # Delete asks once; the message stays as a note that it was removed.
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    msg.hover()
    msg.locator(".msg-act").click()
    page.click("#msg-strip button:has-text('Delete')")
    expect(msg.locator(".text")).to_have_text("You removed this message")
    assert asked == ["Delete this message? This cannot be undone."]
    expect(msg.locator(".msg-act, .chip")).to_have_count(0)
    assert [a["verb"] for a in stub.owner_actions if a.get("verb") in ("edit", "remove")] == ["edit", "edit", "remove"]
    assert errors == ["Failed to load resource: the server responded with a status of 409 (Conflict)"]  # the late edit


def test_a_code_block_carries_its_own_copy_button(app):
    """The fenced block gets a Copy that takes the block's text alone; the
    strip's Copy takes the whole message without the buttons' word."""
    from playwright.sync_api import expect
    page, stub, errors = app(context={"permissions": ["clipboard-read", "clipboard-write"]})
    open_thread(page, "Here is the figure")
    page.fill("#text", "Run this:\n```\nfirst line\nsecond line\n```")
    page.press("#text", "Enter")
    # The stub's reply scrolls the thread to its end, and a scroll closes the strip: it lands first.
    m, msg = echoed(page, stub, "Run this:\n```\nfirst line\nsecond line\n```")
    expect(msg.locator(".code-box pre")).to_have_text("first line\nsecond line")
    expect(msg.locator(".code-copy")).to_have_text("Copy")
    msg.locator(".code-copy").click()
    expect(page.locator("#toast")).to_have_text("Copied.")
    assert page.evaluate("() => navigator.clipboard.readText()") == "first line\nsecond line"
    msg.hover()
    msg.locator(".msg-act").click()
    page.click("#msg-strip button:has-text('Copy')")
    assert page.evaluate("() => navigator.clipboard.readText()") == "Run this:\n\nfirst line\nsecond line"
    assert not errors
