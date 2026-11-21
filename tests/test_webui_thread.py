"""The web app in a browser: a thread and its stream (tests/webui_stub_server.py
stands in for the bot).

A thread renders, markup in text stays text, the renderers build elements,
a card's buttons and select post to the actions route and an action that
asks takes a line of the owner's words. The keyboard pins the height only
for a focused field. An event appends a message and marks unread, a thread
read in one browser clears its count in another (a read in the background
clears nothing, and the return to the screen counts again), a dropped
stream resumes from the last event and reads the open thread again, a
routine reopen reads as reconnecting, and a send the host refuses goes
again with the same nonce.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    FAKE_HIDDEN, FAKE_VIEWPORT, WATCH_LIVE, app, browser, open_thread, stub_factory, wait_for,
)


def test_a_thread_renders_and_markup_in_text_stays_text(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
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


def test_the_drawer_names_each_place_in_plain_words(app):
    """The fixed places go by what they are for (Ask Tacitus, Action
    requests, Scheduling, Mail digests), a room by its name without the
    bot's "ea-" prefix; each link keeps the channel's id, and the header and
    a new conversation's box use the same names."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    links = page.locator("#channel-list > a.chan, #channel-list > .chan-row > a.chan")
    expect(links.locator(".c-name")).to_have_text(["Ask Tacitus", "Action requests", "Scheduling", "Mail digests",
                                                   "example-project"])  # the two places to talk, More by kind, rooms
    assert [links.nth(i).get_attribute("href") for i in range(5)] == [
        "#/stream%3A9", "#/stream%3A7", "#/stream%3A11", "#/digests", "#/r1"]
    expect(page.locator("#channel-list details .c-name")).to_have_text("old-project")
    expect(page.locator("#new-thread")).to_have_text("New conversation")
    expect(page.locator("#back")).to_have_attribute("aria-label", "Back to conversations")
    expect(page.locator("#menu")).to_have_attribute("aria-label", "Menu")
    for where, title, box in (("stream%3A9", "Ask Tacitus", "Ask Tacitus"),
                              ("stream%3A11", "Scheduling", "Message Scheduling")):
        page.evaluate(f"location.hash = '#/{where}/%2B'")
        expect(page.locator("#channel-name")).to_have_text(title)
        expect(page.locator("#text")).to_have_attribute("placeholder", box)
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
    assert md("> q\n\n```\n<i>\n```") == (
        '<blockquote><p>q</p></blockquote><div class="code-box"><pre><code>&lt;i&gt;</code></pre>'
        '<button type="button" class="btn small code-copy" aria-label="Copy this block">Copy</button></div>')
    assert md("one\ntwo") == "<p>one<br>two</p>"
    # A time is not a shortcode; a known shortcode after it still is.
    out = md("at 10:30:00 :lock:")
    assert out.startswith("<p>at 10:30:00 <span") and "\U0001f512" in out
    assert md(":no_such_name:") == "<p>:no_such_name:</p>"
    # Mentions and stream links in history imported from Zulip show their names,
    # never the id.
    assert md("@**Jane Doe|8** and @_**Jane Doe|8** in #**ea**") == (
        '<p><span class="mention">@Jane Doe</span> and <span class="mention">@Jane Doe</span> '
        'in <span class="mention">#ea</span></p>')
    assert page.evaluate("plainLine('@**Jane Doe|8** :lock: **done**')") == "@Jane Doe \U0001f512 done"
    # Card text is Slack mrkdwn: *bold*, and a labelled link shows its target.
    assert card("*Confirm* <https://e.example|here>") == (
        '<strong>Confirm</strong> here (<a href="https://e.example" target="_blank" '
        'rel="noopener noreferrer">https://e.example</a>)')
    # A long target (an Outlook link) shows its site, read from the address,
    # and the whole address on hover.
    long = "https://user@outlook.office365.com/owa/?ItemID=" + "A" * 200
    assert card(f"<{long}|Open the newest email>") == (
        f'Open the newest email (<a href="{long}" target="_blank" rel="noopener noreferrer" '
        f'title="{long}">https://outlook.office365.com/\u2026</a>)')
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
    assert page.locator("#messages .card button, #messages .card select").count() == 0
    assert not errors, errors


def test_a_card_button_that_opens_a_window_shows_it_and_saves_the_edit(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    stub.modal_views["send"] = {
        "type": "modal", "title": {"type": "plain_text", "text": "Edit draft"},
        "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{"type": "context", "elements": [{"type": "mrkdwn", "text": "*To:* pat@example.org"}]},
                   {"type": "input", "block_id": "body_block", "label": {"type": "plain_text", "text": "Body"},
                    "element": {"type": "plain_text_input", "action_id": "body", "multiline": True,
                                "initial_value": "Hi Pat,\n\nThanks"}}]}
    open_thread(page, "Draft a reply")
    page.locator(".card").first.locator("button.primary").click()
    dlg = page.locator("dialog#modal")
    expect(dlg).to_be_visible()
    expect(dlg.locator("h2")).to_have_text("Edit draft")
    expect(dlg.locator(".sheet-note")).to_contain_text("To: pat@example.org")
    field = dlg.locator("textarea")
    expect(field).to_have_value("Hi Pat,\n\nThanks")
    field.fill("Hi Pat,\n\nThanks a lot")
    dlg.locator("button[type=submit]").click()
    wait_for(lambda: stub.modal_saves)
    assert stub.modal_saves == [{"trigger": "web:send",
                                 "values": {"body_block": {"body": "Hi Pat,\n\nThanks a lot"}}}]
    expect(dlg).to_be_hidden()
    assert not errors, errors


def _reply_card(stub, thread_key):
    """A Replies row card in a thread, as the bot posts it."""
    import webui_stub_server as W
    value = {"row": "response-example"}
    return stub.add(W.MAIN, "", kind="card", thread_key=thread_key, event="message", card={
        "kind": "replies", "title": ":inbox_tray: *Reply to the example lab*", "fields": [],
        "lines": [], "body": "", "quoted": "",
        "actions": [{"id": "reply_draft", "label": ":pencil2: Draft it", "value": value,
                     "style": "primary", "word": "draft", "ask": "what the reply must say"},
                    {"id": "reply_skip", "label": "Skip 3 days", "value": value, "style": "",
                     "word": "skip", "ask": ""},
                    {"id": "reply_not_needed", "label": "Not needed", "value": value,
                     "style": "danger", "word": "not needed", "ask": ""}],
        "select": None, "block_id": "", "footer": "", "fallback_text": "A reply waits",
        "thread_ts": thread_key, "ea_channel": W.MAIN, "content_fingerprint": "",
        "no_default": False})


def test_an_action_that_asks_takes_a_line_of_the_owners_words(app):
    """A card whose action takes a line shows one field above its buttons.
    Enter in it, or the button, sends the words with that action; an empty
    field is a plain press, and no other action carries the words."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    assert page.locator(".card .card-ask").count() == 0          # the Send card asks for none
    tk = stub.messages[0]["thread_key"]
    first = _reply_card(stub, tk)
    box = page.locator(".card[data-kind='replies']")
    field = box.locator(".card-actions input.card-ask")
    expect(field).to_have_attribute("placeholder", "What the reply must say (optional)")
    assert field.get_attribute("maxlength") == "300"
    field.fill("  Say yes, after the 20th.  ")
    field.press("Enter")
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": first["channel"], "id": first["id"],
                            "action_id": "reply_draft", "value": {"row": "response-example"},
                            "text": "Say yes, after the 20th."}]
    expect(page.locator(".card.status:has-text('Done: reply_draft')")).to_be_visible()
    second = _reply_card(stub, tk)
    expect(box).to_have_count(1)
    box.locator("button.primary").click()                        # no words: a plain press
    wait_for(lambda: len(stub.clicks) == 2)
    assert stub.clicks[1] == {"channel": second["channel"], "id": second["id"],
                              "action_id": "reply_draft", "value": {"row": "response-example"},
                              "text": ""}
    expect(page.locator(".card.status:has-text('Done: reply_draft')")).to_have_count(2)
    third = _reply_card(stub, tk)
    box.locator("input.card-ask").fill("words for another action")
    box.locator("button:has-text('Not needed')").click()
    wait_for(lambda: len(stub.clicks) == 3)
    assert stub.clicks[2] == {"channel": third["channel"], "id": third["id"],
                              "action_id": "reply_not_needed",
                              "value": {"row": "response-example"}}
    assert not errors, errors


def test_a_select_posts_the_option_chosen(app):
    page, stub, errors = app()
    open_thread(page, "Open a room")
    page.select_option(".card-actions select", "p2")
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["action_id"] == "pick_project"
    assert stub.clicks[0]["selected"] == "p2"
    assert not errors, errors


def test_only_a_focused_field_lets_a_short_viewport_pin_the_height(app):
    """A visual viewport shorter than the window pins the app's height only
    while a text field has the focus (the keyboard); a foldable open in
    landscape that reports a short viewport keeps the stylesheet's height."""
    page, stub, errors = app(viewport={"width": 390, "height": 844})
    open_thread(page, "Draft a reply")
    page.evaluate("""() => {
      const fake = { scale: 1, height: 400, width: 390, offsetTop: 0, addEventListener() {}, removeEventListener() {} };
      Object.defineProperty(window, 'visualViewport', { value: fake, configurable: true });
    }""")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == ""
    # A garbage height (a foldable after a fold) pins nothing, focus or not.
    page.focus("#text")
    page.evaluate("window.visualViewport.height = -33554432; window.dispatchEvent(new Event('resize'))")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == ""
    page.evaluate("window.visualViewport.height = 400")
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == "400px"
    # The focus moving to Send keeps the pin for a second; then the keyboard is read as gone.
    page.evaluate("document.getElementById('text').blur()")
    page.wait_for_timeout(150)
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == "400px"
    page.wait_for_timeout(1000)
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == ""
    assert not errors, errors


def test_a_window_that_shrinks_with_the_keyboard_keeps_the_composer_in_view(app):
    """iOS 27 shrinks the window itself for the keyboard, and the window and the
    visual viewport disagree while the keyboard moves: the composer stays
    inside the visual viewport through every step, and the phone notes each
    keyboard change with where the composer landed."""
    page, stub, errors = app(viewport={"width": 382, "height": 678}, init=FAKE_VIEWPORT)
    page.evaluate("__vv.width = 382; __vv.height = 678")
    open_thread(page, "Draft a reply")
    page.evaluate("window.dispatchEvent(new Event('resize'))")  # the screen without a keyboard is known
    page.focus("#text")
    # The keyboard opens: the window and the visual viewport both read 306.
    page.set_viewport_size({"width": 382, "height": 306})
    page.evaluate("__vv.height = 306; __vv.dispatchEvent(new Event('resize'))")
    box = page.locator("#composer").bounding_box()
    assert box["y"] + box["height"] <= 306
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == "306px"
    # The window springs back to 678 while the visual viewport is still 306: the pin holds.
    page.set_viewport_size({"width": 382, "height": 678})
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == "306px"
    box = page.locator("#composer").bounding_box()
    assert box["y"] + box["height"] <= 306
    # The keyboard closes: the visual viewport grows, the pin goes, the window's height rules.
    page.evaluate("document.getElementById('text').blur()")
    page.evaluate("__vv.height = 678; __vv.dispatchEvent(new Event('resize'))")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == ""
    wait_for(lambda: any(e.get("what") == "keyboard" for d in stub.diags for e in d.get("entries", [])))
    notes = [e for d in stub.diags for e in d.get("entries", []) if e.get("what") == "keyboard"]
    assert [n["up"] for n in notes] == [True, False]
    assert notes[0]["composer"] <= 306 and notes[0]["thread"] == "open" and notes[0]["client"] in (306, 678)
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
    # Polled from here: wait_for_function's own polling evaluates a string, which the page's CSP refuses.
    wait_for(lambda: page.evaluate("document.documentElement.style.getPropertyValue('--app-h')") == "")
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
    hello = stub.emit_bot(W.MAIN, "Live **hello**", thread_key=tk)
    expect(page.locator("#messages .msg strong:has-text('hello')")).to_be_visible()
    expect(page.locator("#messages .msg")).to_have_count(4)
    # The message on screen is read: the bot hears it, up to that message.
    wait_for(lambda: {"channel": W.MAIN, "thread_key": tk, "upto": hello["seq"]} in stub.read_posts)
    assert tk not in stub.counts(W.MAIN)["unread_threads"]

    main = page.locator('a.chan[href="#/stream%3A9"] .badge')
    expect(main).to_have_text("2")
    stub.emit_bot(W.MAIN, "Elsewhere", thread_key=W.WEEKLY)
    expect(main).to_have_text("3")
    expect(page).to_have_title("(3) Tacitus")
    # The channel with unread messages sits first; the DM, read and open, drops below it.
    expect(page.locator("#channel-list .chan .c-name").first).to_have_text("Ask Tacitus")

    stub.emit_bot(W.MAIN, "A brand new thread")
    item = page.locator(".thread-item:has-text('A brand new thread')")
    expect(item.locator(".badge")).to_have_text("1")
    item.click()
    expect(page.locator("#messages .msg")).to_have_count(1)
    expect(item.locator(".badge")).to_have_count(0)
    assert not errors, errors


def test_a_thread_read_on_one_device_clears_its_count_on_the_other(browser, stub_factory):
    """Read state is the bot's: two browsers on one bot (a phone and a
    computer). Reading a thread on one clears its count on the other, on
    the thread's card, the channel's row, the menu's dot and the page's
    title, without a reload."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    url, stub = stub_factory()
    pages, errors = [], []
    for _ in range(2):
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector(".thread-item")
        pages.append(page)
    here, there = pages
    try:
        main = 'a.chan[href="#/stream%3A9"] .badge'
        weekly = there.locator(".thread-item:has-text('Weekly plan') .badge")
        there.click('a.chan[href="#/stream%3A9"]')
        expect(weekly).to_have_text("2")
        expect(there.locator(main)).to_have_text("2")
        expect(there).to_have_title("(2) Tacitus")
        here.click('a.chan[href="#/stream%3A9"]')
        open_thread(here, "Weekly plan")
        expect(weekly).to_have_count(0)
        expect(there.locator(main)).to_have_count(0)
        expect(there).to_have_title("Tacitus")
        assert [p["thread_key"] for p in stub.read_posts] == [W.WEEKLY]
        # A message in a room: the other place's dot on the menu, until it is read here.
        room = next(m for m in stub.messages if m["channel"] == W.ROOM)["thread_key"]
        stub.emit_bot(W.ROOM, "The budget table is in.", thread_key=room)
        expect(there.locator("#menu")).to_have_attribute("data-unread", "1")
        expect(there).to_have_title("(1) Tacitus")
        here.click('a.chan[href="#/r1"]')
        open_thread(here, "Project notes")
        expect(there.locator("#menu")).to_have_attribute("data-unread", "")
        expect(there).to_have_title("Tacitus")
        assert not errors, errors
    finally:
        for page in pages:
            page.context.close()


def test_a_read_in_the_background_clears_nothing_and_coming_back_counts_again(app):
    """A thread the app reads only to name a card (a message in a thread
    the list does not hold) stays unread here and at the bot, through a
    reload. Back on the screen the page takes every count from the bot
    again: an iPhone's home-screen app loses its stream while it is away."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    new = stub.emit_bot(W.MAIN, "A conversation the list does not hold")
    item = page.locator(".thread-item:has-text('A conversation the list does not hold')")
    expect(item.locator(".badge")).to_have_text("1")
    wait_for(lambda: (W.MAIN, new["thread_key"]) in stub.thread_gets)
    page.wait_for_timeout(600)
    expect(item.locator(".badge")).to_have_text("1")
    expect(page.locator('a.chan[href="#/stream%3A9"] .badge')).to_have_text("3")  # with the weekly plan's two
    assert stub.read_posts == [] and stub.counts(W.MAIN)["unread_threads"][new["thread_key"]] == 1
    page.reload()
    expect(item.locator(".badge")).to_have_text("1")
    # read elsewhere while this page's stream heard nothing: back on the screen, it asks the bot
    page.evaluate(FAKE_HIDDEN)
    page.evaluate("__hide(true)")
    stub.reads[(W.MAIN, new["thread_key"])] = new["seq"]
    page.evaluate("__hide(false)")
    expect(item.locator(".badge")).to_have_count(0)
    expect(page).to_have_title("(2) Tacitus")
    assert not errors, errors


def test_a_dropped_stream_resumes_from_the_last_event(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    expect(page.locator("#live.on")).to_be_attached()
    tk = stub.messages[0]["thread_key"]
    stub.emit_bot(W.MAIN, "Before the drop", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('Before the drop')")).to_be_visible()
    # The browser reconnects by itself and names the last event it saw.
    seen = str(stub.eseq)
    stub.drop_streams()
    stub.emit_bot(W.MAIN, "While away", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('While away')")).to_be_visible()
    # Each open reads the open thread again (resync), so a message can show before the stream that
    # brings it: the stub's record of the reconnect is what says how the browser asked.
    wait_for(lambda: any(s["last_event_id"] == seen for s in stub.streams))
    # A refused reconnect closes the stream; the app opens a new one with ?since=.
    seen = str(stub.eseq)
    stub.drop_streams(refuse=1)
    stub.emit_bot(W.MAIN, "After a refusal", thread_key=tk)
    expect(page.locator("#messages .msg:has-text('After a refusal')")).to_be_visible(timeout=10000)
    wait_for(lambda: any(s["since"] == seen for s in stub.streams), 10)
    expect(page.locator("#messages .msg")).to_have_count(6)
    assert not [e for e in errors if "EventSource" not in e and "503" not in e], errors


def test_a_routine_reopen_reads_reconnecting_and_only_a_long_drop_not_connected(app):
    """A stream the host drops and that opens again within the grace (a
    restart of the bot) reads "Reconnecting on your own network..." in the
    Settings menu, and the dot never turns to "not connected"; a stream down
    past the grace says since when, and both read "Connected" once it opens.
    On the owner's own network a hidden page keeps its stream."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    expect(page.locator("#live.on")).to_be_attached()
    page.click("#open-settings")
    expect(page.locator("#settings-conn")).to_have_text("Connected on your own network.")
    page.evaluate(WATCH_LIVE)
    stub.drop_streams()
    wait_for(lambda: "Reconnecting on your own network..." in page.evaluate("__line"))
    expect(page.locator("#live.on")).to_be_attached(timeout=10000)
    expect(page.locator("#settings-conn")).to_have_text("Connected on your own network.")
    assert "live wait" in page.evaluate("__live") and "live off" not in page.evaluate("__live")
    assert not [t for t in page.evaluate("__line") if t.startswith("Not connected")]

    page.evaluate("setLive.grace = 300")
    stub.drop_streams(refuse=2)
    expect(page.locator("#live.off")).to_be_attached(timeout=5000)
    expect(page.locator("#live")).to_have_attribute("title", "Not connected")
    expect(page.locator("#settings-conn")).to_have_text(
        re.compile(r"^Not connected since \d{1,2}:\d{2}\D{0,4}\. Trying again on your own network\.$"))
    expect(page.locator("#live.on")).to_be_attached(timeout=15000)
    expect(page.locator("#settings-conn")).to_have_text("Connected on your own network.")

    page.evaluate(FAKE_HIDDEN)
    page.evaluate("closeWhenHidden.ms = 50; __hide(true)")
    page.wait_for_timeout(300)
    assert page.evaluate("Boolean(S.es && !S.es.asleep)")
    page.evaluate("__hide(false)")
    expect(page.locator("#live.on")).to_be_attached()
    assert not [e for e in errors if "EventSource" not in e and "503" not in e], errors


def test_a_send_the_host_refuses_goes_again_with_the_same_nonce(app):
    """A deploy restarts the bot: the send meets a 503 (or no host at
    all). The composer keeps the text, says so, and sends again with the
    same nonce, so the host posts it once."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    stub.refuse_posts = 2
    page.fill("#text", "Hold this thought")
    page.press("#text", "Enter")
    expect(page.locator("#composer-note")).to_have_text("Tacitus is not answering (a restart?). Trying again.")
    assert page.input_value("#text") == "Hold this thought"
    wait_for(lambda: stub.posts, timeout=15)
    assert len(stub.refused_posts) == 2 and len(stub.posts) == 1
    assert stub.post_nonces[0] and stub.post_nonces[0] == stub.refused_posts[0].get("nonce")
    expect(page.locator("#messages .msg.own:has-text('Hold this thought')")).to_be_visible()
    assert page.input_value("#text") == "" and page.locator("#composer-note").text_content() == ""
    assert not [e for e in errors if "503" not in e], errors


def test_the_open_thread_is_read_again_when_the_stream_comes_back(app):
    """What the stream missed while it was down (a message the host
    stored with no event, as a restart in the middle of a reply can leave)
    shows once the stream is back: the open thread is read again."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    expect(page.locator("#live.on")).to_be_attached()
    tk = stub.messages[0]["thread_key"]
    stub.drop_streams(refuse=1)
    stub.add(W.MAIN, "Missed while away", author="bot", thread_key=tk)   # stored, no event
    expect(page.locator("#messages .msg:has-text('Missed while away')")).to_be_visible(timeout=15000)


def test_keys_with_reserved_characters_reach_the_api(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.click('a.chan[href="#/stream%3A9"]')
    open_thread(page, "Weekly plan")
    expect(page.locator("#messages .msg")).to_have_count(3)
    expect(page.locator("#messages h3")).to_have_text("Monday")
    assert "stream%3A9%3Atopic%3Aweekly%2520plan" in page.url
    assert not errors, errors
