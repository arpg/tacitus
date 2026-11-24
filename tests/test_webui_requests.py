"""Action requests in the web app, as a person sees it (docs/webchat.md,
Action requests): a reply that was not written, and a thread several
requests share. The browser fixtures are in tests/webui_browser.py; the
rest of Action requests is in tests/test_webui_action_requests.py. Made-up
data only."""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from webui_browser import (  # noqa: E402,F401  (the fixtures)
    PHONE, SIDEWAYS, SMALL_TARGETS, app, browser, reply_quiet, stub_factory, wait_for,
)

VALUE = {"row": "response-r1", "batch": "b1"}


def _not_written(stub, why="The thread has no address to answer."):
    """The card the bot posts when a reply turn could not write the reply:
    its reason, Try again (the answer it repeats), Open in Ask Tacitus,
    Later and No reply needed; no answer control."""
    import webui_stub_server as W
    acts = [{"id": "reply_draft", "label": ":repeat: Try again", "value": dict(VALUE, again="Yes, three years."),
             "style": "primary", "word": "again", "ask": ""},
            {"id": "reply_discuss", "label": ":speech_balloon: Open in Ask Tacitus", "value": dict(VALUE, why=why),
             "style": "", "word": "ask", "ask": ""}] + reply_quiet(VALUE)
    return stub.add(W.DM, "", kind="card", event="card", card={
        "kind": "replies", "title": f":inbox_tray: *Review request*\nFrom Pat, 2 days ago.\nI could not write it: {why}",
        "fields": [], "lines": [], "body": "", "quoted": "", "actions": acts, "select": None, "block_id": "",
        "footer": "", "fallback_text": "Review", "thread_ts": "", "ea_channel": W.DM, "content_fingerprint": "",
        "no_default": False, "question": {},
        "problem": {"text": why, "from": "Pat", "since": "", "about": "Review request"}})


def test_a_reply_that_was_not_written_says_why_and_offers_try_again_not_an_answer(app):
    """Folded, it reads Not written and its reason; opened, the reason with
    Try again first and Open in Ask Tacitus, no answer field pretending it
    is a question. Try again presses the card's own action, which carries
    the answer it repeats."""
    from playwright.sync_api import expect
    page, stub, errors = app(**PHONE)
    page.wait_for_selector(".thread-item")
    m = _not_written(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    expect(block.locator(".req-act")).to_have_text("Not written")
    expect(block.locator(".req-what")).to_have_text("The thread has no address to answer.")
    block.locator(".req-head").click()
    card = block.locator(".req-body .card")
    expect(card).to_contain_text("I could not write it: The thread has no address to answer.")
    expect(card.locator(".answer-ctl, input:not([type='date'])")).to_have_count(0)  # Later's day only
    buttons = card.locator(".card-actions button")
    expect(buttons.first).to_have_text("🔁 Try again")
    assert [buttons.nth(i).get_attribute("data-action") for i in range(4)] == \
        ["reply_draft", "reply_discuss", "reply_skip", "reply_not_needed"]
    assert page.evaluate(SMALL_TARGETS) == [] and page.evaluate(SIDEWAYS) == [0, 0]
    buttons.first.click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["action_id"] == "reply_draft"
    assert stub.clicks[0]["value"]["again"] == "Yes, three years."
    assert not errors, errors


def test_requests_that_share_a_thread_each_show_only_their_own_lines(app):
    """Two rows' cards in one thread: each block shows the bot's lines
    tagged with its own row, and an untagged line of the thread in none."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    parent = stub.add(W.DM, "One by one:", kind="status", card={"kind": "replies", "title": "Two wait"})
    a = _not_written(stub, "No address for row A.")
    b = _not_written(stub, "No address for row B.")
    for m in (a, b):
        m["thread_key"] = parent["thread_key"]
    stub.tags.update({a["id"]: "reply:a", b["id"]: "reply:b"})
    for text, tag in (("Writing the reply to A.", "reply:a"), ("Writing the reply to B.", "reply:b"),
                      ("Still working... (3m)", None)):
        line = stub.add(W.DM, text, thread_key=parent["thread_key"])
        if tag:
            stub.tags[line["id"]] = tag
    page.evaluate("location.hash = '#/stream%3A7'")
    for m, own in ((a, "Writing the reply to A."), (b, "Writing the reply to B.")):
        block = page.locator(f'.req[data-id="{m["id"]}"]')
        block.locator(".req-head").click()
        expect(block.locator(".req-line")).to_have_text([own])
    expect(page.locator(".req-line", has_text="Still working")).to_have_count(0)
    assert not errors, errors
