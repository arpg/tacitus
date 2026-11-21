"""The web app in a browser: keyboard shortcuts.

Cmd+K, or Ctrl+K on Windows and Linux, opens Jump to: the places and the
conversations the app holds (Recent and each place it has opened), filtered
by the words typed, the arrows to move and Enter to open. / opens search
outside a text field and types a slash in one.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    app, browser, stub_factory,
)


def test_ctrl_k_and_cmd_k_jump_to_a_conversation_by_its_words(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    jump = page.locator("dialog#jump")
    for combo in ("Control+k", "Meta+k"):  # Windows and Linux, then the Mac
        page.keyboard.press(combo)
        expect(jump).to_be_visible()
        expect(jump.locator("input")).to_be_focused()
        expect(jump.locator("li .j-title").first).to_have_text("Ask Tacitus")  # the places first
        page.keyboard.press("Escape")
        expect(jump).to_be_hidden()
    page.locator("#text").click()
    page.keyboard.press("Control+k")  # from the reply box too
    expect(jump).to_be_visible()
    page.keyboard.type("draft a reply")
    hit = jump.locator("li .j-title")
    expect(hit).to_have_count(1)
    expect(hit).to_contain_text("Draft a reply")
    thread = next(m for m in stub.messages if "Draft a reply" in (m.get("text") or ""))
    page.keyboard.press("Enter")
    expect(jump).to_be_hidden()
    page.wait_for_function("(tk) => decodeURIComponent(location.hash).endsWith('/' + tk)", arg=thread["thread_key"])
    page.keyboard.press("Control+k")
    page.keyboard.type("no such place at all")
    expect(jump.locator(".j-none")).to_have_text("Nothing matches.")
    page.keyboard.press("Escape")
    assert not errors, errors


def test_the_arrows_pick_the_hit_enter_opens(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.keyboard.press("Control+k")
    items = page.locator("dialog#jump li")
    expect(items.first).to_have_class("on")
    page.keyboard.press("ArrowDown")
    expect(items.nth(1)).to_have_class("on")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")  # wraps to the last
    expect(items.last).to_have_class("on")
    assert not errors, errors


def test_slash_opens_search_outside_a_field_and_types_in_one(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.locator("#text").click()
    page.keyboard.type("a/b")
    expect(page.locator("#text")).to_have_value("a/b")
    expect(page.locator("#search")).to_be_hidden()
    page.locator("#text").blur()
    page.keyboard.press("/")
    expect(page.locator("#search-q")).to_be_focused()
    expect(page.locator("#search-q")).to_have_value("")
    assert not errors, errors
