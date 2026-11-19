"""The web app in a browser: the composer.

It sends on Enter and starts threads, keeps a draft per conversation
(through a reload) and holds the files chosen, dropped or pasted until
Send posts them with the words in one request; the paperclip uploads, the
Claude button asks for a handoff, an edit puts the draft aside, pairing
answers a 401, a phone shows one pane with 44px targets, and the digest
channel shows no composer and says why.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    PHONE, SIDEWAYS, SIZES, SMALL_TARGETS, WEBUI, app, browser, echoed, open_thread, stub_factory,
    wait_for,
)
from plain_words import JARGON  # noqa: E402


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
    expect(page.locator("#pending .pend")).to_contain_text("icon-512.png")
    page.press("#text", "Enter")
    wait_for(lambda: stub.posts)
    assert stub.posts[0]["text"] == "The chart" and stub.posts[0]["file"]["name"] == "icon-512.png"
    expect(page.locator(".msg.own .file img")).to_have_count(2)
    assert not errors, errors


def test_the_claude_button_asks_for_a_handoff_and_its_file_goes_to_the_share_sheet(app):
    """skills/claude-handoff.md: the header's Claude button asks Tacitus, in
    the open conversation, for one file to take to the Claude app. That file
    on the reply carries Send to Claude, which hands the bytes it fetched
    when it showed to the share sheet in one tap. A page that cannot share
    a file saves it instead; no other file has the button."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    share = ("window.__shared = [];"
             "Object.defineProperty(navigator, 'canShare', { value: (d) => Boolean(d && d.files), configurable: true });"
             "Object.defineProperty(navigator, 'share', { value: async (d) => {"
             " window.__shared.push(await Promise.all(d.files.map(async (f) => [f.name, await f.text()]))); },"
             " configurable: true });")
    page, stub, errors = app(init=share)
    open_thread(page, "Here is the figure")
    tk = page.locator(".thread-item.active").get_attribute("data-thread")
    expect(page.locator("#to-claude")).to_be_visible()
    expect(page.locator(".to-claude-file")).to_have_count(0)
    page.click("#to-claude")
    wait_for(lambda: stub.posts)
    assert stub.posts == [{"channel": W.MAIN, "text": "Make a Claude handoff file for this conversation.",
                           "thread_key": tk, "file": None}]
    body = "# Field plan: talk-through\n\n## For Claude: how to run this conversation\n"
    stub.files["f9"] = ("field-plan-2026-11-17.claude.md", "text/markdown", body.encode())
    stub.add(W.MAIN, "", kind="file", thread_key=tk, event="message",
             file={"id": "f9", "name": "field-plan-2026-11-17.claude.md", "url": "",
                   "mimetype": "text/markdown", "size": len(body)})
    btn = page.locator(".file:has-text('field-plan') .to-claude-file")
    expect(btn).to_have_text("Send to Claude")
    btn.click()
    wait_for(lambda: page.evaluate("window.__shared.length"))
    assert page.evaluate("window.__shared") == [[["field-plan-2026-11-17.claude.md", body]]]
    page.click("#new-thread")
    expect(page.locator("#to-claude")).to_be_hidden()
    assert not errors, errors

    plain, stub2, errors2 = app(init="Object.defineProperty(navigator, 'canShare', { value: undefined, configurable: true });")
    open_thread(plain, "Here is the figure")
    tk2 = plain.locator(".thread-item.active").get_attribute("data-thread")
    stub2.files["f9"] = ("field-plan-2026-11-17.claude.md", "text/markdown", body.encode())
    stub2.add(W.MAIN, "", kind="file", thread_key=tk2, event="message",
              file={"id": "f9", "name": "field-plan-2026-11-17.claude.md", "url": "",
                    "mimetype": "text/markdown", "size": len(body)})
    expect(plain.locator(".to-claude-file")).to_have_text("Save for Claude")
    with plain.expect_download() as dl:
        plain.click(".to-claude-file")
    assert dl.value.suggested_filename == "field-plan-2026-11-17.claude.md"
    assert not errors2, errors2


def go(page, title):
    """Open a conversation from the list while another is open: its card is
    the active one once its route ran."""
    from playwright.sync_api import expect
    page.click(f".thread-item:has-text({json.dumps(title)})")
    expect(page.locator(".thread-item.active")).to_contain_text(title)


def _drafts(page) -> dict:
    """The drafts this browser keeps, by the place's key."""
    return page.evaluate("Object.fromEntries(Object.keys(localStorage).filter((k) => k.startsWith('tacitus-draft:'))"
                         ".map((k) => [k.slice(14), JSON.parse(localStorage.getItem(k))]))")


def test_a_draft_stays_with_its_conversation_and_goes_with_the_send(app):
    """What is typed in a conversation, or in a new one, stays there when
    the owner moves on, comes back on the return and after a reload, marks
    the conversation's card, and a send clears it."""
    from urllib.parse import quote
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    reply_key = page.locator(".thread-item.active").get_attribute("data-thread")
    page.fill("#text", "half a reply")
    expect(page.locator(".thread-item.active .t-draft")).to_have_text("Draft")
    go(page, "Here is the figure")
    figure_key = page.locator(".thread-item.active").get_attribute("data-thread")
    assert page.input_value("#text") == ""
    expect(page.locator(".thread-item:has-text('Draft a reply') .t-draft")).to_be_visible()
    page.fill("#text", "about the figure")
    page.click("#new-thread")
    expect(page.locator("#thread-title")).to_have_text("New conversation")
    assert page.input_value("#text") == ""
    page.fill("#text", "a new one")
    page.click(f"a.chan[href='#/{quote(W.SCHED, safe='')}']")
    expect(page.locator(".chan.active .c-name")).to_have_text("Scheduling")
    assert page.input_value("#text") == ""
    page.click(f"a.chan[href='#/{quote(W.MAIN, safe='')}']")
    expect(page.locator(".chan.active .c-name")).to_have_text("Ask Tacitus")
    assert page.input_value("#text") == "a new one"  # the place with no conversation open starts a new one
    go(page, "Draft a reply")
    assert page.input_value("#text") == "half a reply"
    kept = _drafts(page)
    assert (kept[f"{W.MAIN}\n{reply_key}"]["text"], kept[f"{W.MAIN}\n{figure_key}"]["text"], kept[f"{W.MAIN}\n+"]["text"]) == \
        ("half a reply", "about the figure", "a new one")
    # A reload keeps the words, and the mark.
    page.reload()
    page.wait_for_selector(".thread-item")
    expect(page.locator(".thread-item:has-text('Draft a reply') .t-draft")).to_be_visible()
    expect(page.locator("#text")).to_have_value("half a reply")
    go(page, "Here is the figure")
    expect(page.locator("#text")).to_have_value("about the figure")
    # A send clears its draft and the mark; another place's stays.
    go(page, "Draft a reply")
    page.press("#text", "Enter")
    wait_for(lambda: stub.posts)
    assert stub.posts[-1]["text"] == "half a reply" and stub.posts[-1]["thread_key"] == reply_key
    expect(page.locator("#text")).to_have_value("")
    expect(page.locator(".thread-item:has-text('Draft a reply') .t-draft")).to_have_count(0)
    assert f"{W.MAIN}\n{reply_key}" not in _drafts(page)
    assert _drafts(page)[f"{W.MAIN}\n{figure_key}"]["text"] == "about the figure"
    # A new conversation's send clears the new conversation's draft; what it opens has none.
    page.click("#new-thread")
    expect(page.locator("#text")).to_have_value("a new one")
    page.press("#text", "Enter")
    wait_for(lambda: len(stub.posts) == 2)
    new_key = next(m for m in stub.messages if m["text"] == "a new one")["thread_key"]
    expect(page).to_have_url(re.compile(re.escape(new_key) + "$"))
    expect(page.locator("#text")).to_have_value("")
    assert f"{W.MAIN}\n+" not in _drafts(page) and f"{W.MAIN}\n{new_key}" not in _drafts(page)
    assert not errors, errors


def test_words_typed_before_the_route_runs_go_to_the_conversation_clicked(app):
    """A click on a conversation changes the hash at once and its route runs
    a task later. Words typed in between stay in the composer, Send posts
    them to the conversation clicked, and no other place keeps them as a
    draft. Back moves the composer with the hash the same way."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    key = page.locator(".thread-item:has-text('Draft a reply')").get_attribute("data-thread")
    # The click and the typing in one task, ahead of the route.
    type_after_click = """([sel, words]) => {
        document.querySelector(sel).click();
        const t = $('text'); t.value = words; t.dispatchEvent(new Event('input'));
    }"""
    page.evaluate(type_after_click, [f".thread-item[data-thread={json.dumps(key)}]", "typed at once"])
    expect(page.locator(".thread-item.active")).to_contain_text("Draft a reply")  # the route ran
    expect(page.locator("#text")).to_have_value("typed at once")
    page.press("#text", "Enter")
    wait_for(lambda: stub.posts)
    assert (stub.posts[-1]["text"], stub.posts[-1]["thread_key"]) == ("typed at once", key)
    expect(page.locator("#text")).to_have_value("")
    assert _drafts(page) == {}
    # Back to the list, typing at once: the words are the channel's new conversation's.
    page.evaluate(type_after_click, ["#back", "a new one"])
    expect(page.locator("body")).to_have_attribute("data-view", "threads")
    expect(page.locator("#text")).to_have_value("a new one")
    assert {k: d["text"] for k, d in _drafts(page).items()} == {f"{W.MAIN}\n+": "a new one"}
    assert not errors, errors


def test_a_chosen_file_waits_for_send_and_goes_with_the_words(app):
    """Choosing files adds them to the composer and sends nothing; a cross
    takes one off; they stay with their conversation's draft; Send posts the
    words and every file in one request, in order, into the conversation."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Here is the figure")
    tk = page.locator(".thread-item.active").get_attribute("data-thread")
    page.fill("#text", "Both of these")
    page.set_input_files("#file", [str(WEBUI / "icon-512.png"), str(WEBUI / "favicon.svg"), str(WEBUI / "icon-192.png")])
    expect(page.locator("#pending .pend .file-name")).to_have_text(["icon-512.png", "favicon.svg", "icon-192.png"])
    page.wait_for_timeout(300)
    assert not stub.uploads and not stub.posts
    page.click("#pending button[aria-label='Remove favicon.svg']")
    expect(page.locator("#pending .pend .file-name")).to_have_text(["icon-512.png", "icon-192.png"])
    expect(page.locator("#pending button[aria-label='Remove icon-192.png']")).to_be_focused()
    expect(page.locator("#composer")).to_have_class(re.compile(r"\bhas-files\b"))  # Send is filled, as for words
    # The files are part of the conversation's draft: another conversation has none, and they come back.
    go(page, "Draft a reply")
    expect(page.locator("#pending")).to_be_hidden()
    go(page, "Here is the figure")
    expect(page.locator("#pending .pend .file-name")).to_have_text(["icon-512.png", "icon-192.png"])
    assert page.input_value("#text") == "Both of these"
    page.click("#send")
    wait_for(lambda: stub.uploads)
    up = stub.uploads[0]
    assert (up["text"], up["thread_key"]) == ("Both of these", tk) and up["nonce"]
    assert [name for name, _ in up["files"]] == ["icon-512.png", "icon-192.png"]
    assert up["files"][0][1] == (WEBUI / "icon-512.png").read_bytes()
    expect(page.locator("#pending")).to_be_hidden()
    expect(page.locator("#text")).to_have_value("")
    own = page.locator("#messages .msg.own.kind-file")
    expect(own).to_have_count(3)  # the conversation's own, then the two sent
    expect(own.nth(1)).to_contain_text("Both of these")
    expect(own.nth(1).locator(".file-name")).to_have_text("icon-512.png")
    expect(own.nth(2).locator(".file-name")).to_have_text("icon-192.png")
    assert len(stub.uploads) == 1
    assert not errors, errors


def test_a_dropped_or_pasted_file_waits_in_the_composer(app):
    """A file dropped on the conversation, or pasted into the field, waits
    with the others; a drop elsewhere adds nothing and opens nothing. Files
    alone send with no words. Away from home a send over 10 MB is refused
    with a plain note."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    open_thread(page, "Here is the figure")
    tk = page.locator(".thread-item.active").get_attribute("data-thread")
    drop = """([sel, name, size]) => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(size || 5)], name, { type: 'text/plain' }));
      const ev = new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true });
      document.querySelector(sel).dispatchEvent(ev);
      return ev.defaultPrevented;
    }"""
    assert page.evaluate(drop, ["#messages", "notes.txt", 0]) is True
    expect(page.locator("#pending .pend .file-name")).to_have_text(["notes.txt"])
    assert page.evaluate(drop, ["#thread-list", "missed.txt", 0]) is True  # prevented: the page stays
    expect(page.locator("#pending .pend")).to_have_count(1)
    page.fill("#text", "words")
    pasted = page.evaluate("""() => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'image.png', { type: 'image/png' }));
      const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
      document.querySelector('#text').dispatchEvent(ev);
      return ev.defaultPrevented;
    }""")
    assert pasted is True
    expect(page.locator("#pending .pend .file-name")).to_have_text(["notes.txt", "image.png"])
    assert page.input_value("#text") == "words"
    page.fill("#text", "")
    # Away from home a request carries at most 10 MB: the send waits, with a plain note.
    page.evaluate("T = { ...DIRECT, relay: true }")
    page.evaluate(drop, ["#composer", "big.bin", 11 * 1024 * 1024])
    page.click("#send")
    expect(page.locator("#composer-note")).to_have_text(
        "Away from home you can send at most 10 MB of files at once. Send larger ones when you are on your own network.")
    page.evaluate("T = DIRECT")
    page.click("#pending button[aria-label='Remove big.bin']")
    page.click("#send")
    wait_for(lambda: stub.uploads)
    up = stub.uploads[0]
    assert (up["text"], up["thread_key"]) == ("", tk)
    assert [name for name, _ in up["files"]] == ["notes.txt", "image.png"]
    expect(page.locator("#pending")).to_be_hidden()
    assert len(stub.uploads) == 1
    assert not errors, errors


def test_an_edit_puts_the_draft_and_its_files_aside(app):
    """An edit of the owner's message hides the files waiting and takes no
    new one; what it types is no draft; Cancel brings the draft back whole."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Here is the figure")
    tk = page.locator(".thread-item.active").get_attribute("data-thread")
    page.fill("#text", "Sent first")
    page.press("#text", "Enter")
    m, msg = echoed(page, stub, "Sent first")  # the stub's reply scrolls the thread, which closes a strip
    page.fill("#text", "the draft")
    page.set_input_files("#file", str(WEBUI / "favicon.svg"))
    msg.hover()
    msg.locator(".msg-act").click()
    page.click("#msg-strip button:has-text('Edit')")
    expect(page.locator("#pending")).to_be_hidden()
    page.fill("#text", "Sent first, changed")
    page.evaluate("""() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'late.txt', { type: 'text/plain' }));
      document.querySelector('#messages').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    }""")
    expect(page.locator("#composer-note")).to_have_text("Finish the edit first, then attach.")
    assert _drafts(page)[f"{W.MAIN}\n{tk}"] == {"text": "the draft", "files": ["favicon.svg"],
                                             "at": _drafts(page)[f"{W.MAIN}\n{tk}"]["at"]}
    page.click("#edit-cancel")
    expect(page.locator("#pending .pend .file-name")).to_have_text(["favicon.svg"])
    assert page.input_value("#text") == "the draft"
    assert not stub.uploads
    assert not errors, errors


def test_a_phone_keeps_44px_targets_with_files_waiting(app):
    from playwright.sync_api import expect
    page, stub, errors = app(**PHONE)
    open_thread(page, "Here is the figure")
    page.set_input_files("#file", [str(WEBUI / "icon-512.png"), str(WEBUI / "favicon.svg")])
    expect(page.locator("#pending .pend")).to_have_count(2)
    assert page.evaluate(SMALL_TARGETS) == []
    assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


def test_a_401_shows_the_pairing_form(app):
    from playwright.sync_api import expect
    page, stub, errors = app(pair_token="made-up-token")
    expect(page.locator("#pair")).to_be_visible()
    page.fill("#pair-token", "wrong")
    page.click("#pair-form button")
    expect(page.locator("#pair-error")).to_have_text("That code does not match.")
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
    expect(page.locator("#nav-edge")).to_be_hidden()  # a drawer has no edge to drag
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert not errors, errors


@SIZES
def test_the_digest_channel_shows_no_composer_and_says_why(app, size):
    """The bot posts digests and reads no reply there: the channel shows no
    New conversation, its threads no composer, and a line in its place says
    why; the other channels keep theirs."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    stub.add(W.DIGEST, "Kept mail: two messages from the lab.")
    page.evaluate(f"location.hash = '#/{W.DIGEST}'")
    expect(page.locator("#new-thread")).to_be_hidden()
    open_thread(page, "Kept mail")
    expect(page.locator("#composer")).to_be_hidden()
    note = page.locator("#composer-off")
    expect(note).to_be_visible()
    expect(note).to_have_text("Tacitus posts your mail digests here; it does not read replies.")
    assert not JARGON.search(note.inner_text())
    page.evaluate("location.hash = '#/stream%3A9'")
    expect(page.locator("#new-thread")).to_be_visible()
    open_thread(page, "Weekly plan")
    expect(page.locator("#composer")).to_be_visible()
    expect(note).to_be_hidden()
    assert not errors, errors
