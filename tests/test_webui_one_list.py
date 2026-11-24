"""One list in the web app, as a person sees it (docs/webchat.md, Home, "One
list"): Home holds a decision, a close proposal ("Looks done": Done with
its Undo, Not done) and a letter, each answered in place; a reply Home
started says it is being written and then, in the same block, becomes the
way to its Send card; Action requests and its count hold only what needs
the lock; Settings, Home, Where you answer turns it back. With one list
off, Action requests holds every card as before.

The bot's own code answers (tests/webui_stub_server.py, `home_src`,
`one_list`): Home, the items and the choices are home_screen's and
home_items' on made-up sources; the hooks into the bot are recorded.
Made-up data only."""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import home_fixtures as F  # noqa: E402
import webui_stub_server as W  # noqa: E402
from webui_browser import (  # noqa: E402,F401  (the fixtures)
    _request, app, browser, reply_quiet, settled, stub_factory, wait_for,
)
from test_webui_case_briefing import a_case  # noqa: E402

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
REQS = "#thread-list .reqs:not(.done) > .req"
SEND = {"conversation_id": "conv-r1", "draft_id": "d-made-up"}


def sources(state: dict):
    """One list's afternoon: task-s2 looks done to Tacitus, response-r2
    waits on the owner's answer; `state` holds what a test changes (the
    reply turns, the requests)."""
    def src(tz=""):
        got = F.sources("normal", dm=W.DM, main=W.MAIN)
        got.one_list = state.get("one_list", True)
        got.rows["task-s2"]["close_proposal"] = {"why": "The slides went out on Friday",
                                                 "first_at": "2026-11-20T10:00:00Z"}
        got.replies[1]["queued"] = {"outcome": "decision", "note": "Do you sign it as the lab's lead?",
                                    "choices": ["Yes, I can sign it", "No, ask the lab manager"]}
        got.turns = dict(state.get("turns") or {})
        if "requests" in state:
            got.requests = state["requests"]
        return got
    return src


def one_list_app(app, state=None, **kw):
    from playwright.sync_api import expect
    state = state if state is not None else {}
    kw.setdefault("context", CLOCK)
    page, stub, errors = app(home_src=sources(state), one_list=state.get("one_list", True), **kw)
    stub.letters = [a_case()]
    stub.deeper_stored, stub.deeper_delay = False, None
    stub.set_home(stub.real_payload())
    expect(page.locator("#live.on")).to_be_attached()
    expect(page.locator(".h-block[data-ref='proposal:task-s2']")).to_be_visible() if state.get("one_list", True) \
        else expect(page.locator(".h-main .h-block").first).to_be_visible()
    settled(page)
    return page, stub, errors, state


def opened(page, ref):
    from playwright.sync_api import expect
    block = page.locator(f".h-block[data-ref='{ref}']")
    block.locator("> button").click()
    expect(block.locator("> button")).to_have_attribute("aria-expanded", "true")
    expect(block.locator("[data-part='brief'] .h-quiet")).to_have_count(0)
    return block


def cards(stub):
    """What a day of one list's stand-ins would have left in Action requests
    before: a Send card, a reply's row card and a close card."""
    send = _request(stub, "Confirm send", fields=[{"label": "To", "value": "pat@example.org"},
                                                 {"label": "Subject", "value": "Re: Lab budget"}])
    row = _request(stub, "Needs response: Seminar date", kind="replies", actions=[
        {"id": "reply_draft", "label": ":pencil2: Draft it", "value": {"row": "response-r1"}, "style": "primary",
         "word": "draft"}] + reply_quiet({"row": "response-r1"}))
    close = _request(stub, "This looks done.", kind="close", actions=[
        {"id": "close_proposed", "label": "Close", "value": {"row": "task-s2"}, "style": "primary", "word": "close"}])
    return send, row, close


def test_home_holds_a_decision_a_close_proposal_and_a_letter_each_answered_in_place(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app, **PHONE)
    labels = page.locator(".h-main .h-block .h-label").all_inner_texts()
    assert "Looks done" in labels and "Answer needed" in labels and "Letter" in labels
    block = opened(page, "proposal:task-s2")
    expect(block.locator("[data-part='choices'] .btn")).to_have_text(["Done", "Not done", "Ask"])
    expect(block.locator("[data-part='brief']")).to_contain_text("Tacitus thinks it is done: The slides went out on Friday")
    block.get_by_role("button", name="Not done").click()
    wait_for(lambda: stub.home_presses == [("close", "task-s2", True)])
    expect(block.locator(".h-note")).to_have_text(
        "Not done. It stays on your list.")
    letter = opened(page, "letter:lc-made-up-1")
    expect(letter.locator("[data-part='choices'] .btn").first).to_be_visible()
    names = letter.locator("[data-part='choices'] .btn").all_inner_texts()
    assert "Open it in Action requests" not in names and names[-1] == "Ask"     # no card to open: its answers here
    assert not errors, errors


def test_done_on_a_close_proposal_waits_for_its_undo(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app)
    block = opened(page, "proposal:task-s2")
    block.get_by_role("button", name="Done", exact=True).click()
    expect(block.locator(".h-said")).to_have_text("Done. It is off your list.")
    expect(block.get_by_role("button", name="Undo")).to_be_visible()
    assert stub.home_presses == []                              # the close waits for the Undo window
    assert not errors, errors


def test_a_reply_being_written_turns_into_its_send_card_in_the_same_block(app):
    from playwright.sync_api import expect
    state: dict = {}
    page, stub, errors, state = one_list_app(app, state)
    ref = "reply:response-r1"
    block = opened(page, ref)
    state["turns"] = {"response-r1": {"state": "writing"}}
    stub.set_home(stub.real_payload())
    expect(block.locator(".h-detail")).to_have_text("Drafting the reply.")
    expect(block.locator("[data-part='brief'] .h-then")).to_have_text("Drafting the reply.")
    expect(block.locator("[data-part='choices'] .btn")).to_have_text(["Ask"])
    send = F.request("w61", "send", "Send email", "Re: Seminar date", "To Sam Rivera", dm=W.DM)
    send["card"]["actions"][0]["value"] = '{"conversation_id": "conv-r1", "draft_id": "d1"}'
    state.update(turns={}, requests={"waiting": [send], "done": []})
    stub.set_home(stub.real_payload())
    expect(block.locator(".h-label")).to_have_text("Send email")
    expect(block.locator("[data-part='choices'] a.btn")).to_have_text("Open it in Action requests")
    assert page.locator(f".h-block[data-ref='{ref}']").count() == 1
    assert not errors, errors


def test_action_requests_and_its_count_hold_only_what_needs_the_lock(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app)
    send, _row, _close = cards(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    expect(page.locator(REQS)).to_have_count(1)
    expect(page.locator(f'{REQS}[data-id="{send["id"]}"] .req-act')).to_have_text("Send email")
    wait_for(lambda: page.evaluate("(S.channels.find((c) => c.kind === 'dm') || {}).waiting") == 1)
    assert not errors, errors


def test_with_one_list_off_action_requests_holds_every_card_as_before(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app, {"one_list": False})
    cards(stub)
    assert page.locator(".h-block[data-ref='proposal:task-s2']").count() == 0
    page.evaluate("location.hash = '#/stream%3A7'")
    expect(page.locator(REQS)).to_have_count(3)
    wait_for(lambda: page.evaluate("(S.channels.find((c) => c.kind === 'dm') || {}).waiting") == 3)
    assert not errors, errors


def test_settings_where_you_answer_turns_it_back(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app)
    page.click("#open-settings")
    page.click("#go-home")
    expect(page.locator("input[name='answers'][value='home']")).to_be_checked()
    page.click("#home-rows label:has-text('In Action requests')")
    wait_for(lambda: stub.home_posts and stub.home_posts[-1] == {"answers": "requests"})
    expect(page.locator("#home-note")).to_have_text("Saved.")
    assert stub.one_list is False
    assert not errors, errors
