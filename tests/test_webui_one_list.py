"""One list in the web app, as a person sees it (docs/webchat.md, Home, "One
list"): Home holds a decision, a close proposal ("Looks done": Done with
its Undo, Not done) and a letter, each answered in place; a reply Home
started says it is being written and then, in the same block, becomes the
way to its Send card; Action requests and its count hold only what needs
the lock; Settings, Home, Where you answer turns it back. With one list
off, Action requests holds every card as before. An item unfolds in place:
its mail folded as a Send card folds it, what Tacitus has, and one note box
whose quick answers fill it and whose Ask sends it once; the note's answer
shows on the item. The row keeps Done, Later and No reply needed.

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
    expect(block.locator("[data-part='row'] .btn")).to_have_text(["Done", "Not done"])
    expect(block.locator("[data-part='tell'] textarea")).to_be_visible()                   # Ask is the note's
    expect(block.locator("[data-part='brief']")).to_contain_text("Tacitus thinks it is done: The slides went out on Friday")
    block.get_by_role("button", name="Not done").click()
    wait_for(lambda: stub.home_presses == [("close", "task-s2", True)])
    expect(block.locator(".h-note")).to_have_text(
        "Not done. It stays on your list.")
    letter = opened(page, "letter:lc-made-up-1")
    expect(letter.locator("[data-part='choices'] .btn").first).to_be_visible()
    names = letter.locator("[data-part='choices'] .btn").all_inner_texts()
    assert "Open it in Action requests" not in names and "Ask" not in names     # no card to open: its answers here
    expect(letter.locator("[data-part='tell'] textarea")).to_be_visible()                    # and the note in Ask's place
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
    expect(block.locator(".h-acts .btn, [data-part='choices'] .btn, textarea")).to_have_count(0)
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


def _with_talk(app, one=True):
    """One list's Home with the made-up room thread ("Project notes live
    here.") as a conversation that needs the owner."""
    from playwright.sync_api import expect
    state: dict = {"one_list": one}
    holder: dict = {}

    def src(tz=""):
        got = sources(state)(tz)
        room = next(m for m in holder["stub"].messages if m["channel"] == W.ROOM)
        got.conversations = [{"channel": W.ROOM, "thread_key": room["thread_key"], "state": "needs_you",
                              "text": "Which project is the new grant for?",
                              "state_reason": "Tacitus asked which project this is for.", "stale": False,
                              "_ids": [], "_convs": [], "_room": "example-project"}]
        return got
    page, stub, errors = app(home_src=lambda tz="": F.sources("normal", dm=W.DM, main=W.MAIN), one_list=one,
                             context=CLOCK)
    holder["stub"] = stub
    stub.home_src = src
    stub.real._sources = src
    stub.set_home(stub.real_payload())
    expect(page.locator("#live.on")).to_be_attached()
    settled(page)
    return page, stub, errors


def test_needs_you_is_part_of_home_with_one_count(app):
    from playwright.sync_api import expect
    page, stub, errors = _with_talk(app)
    expect(page.locator("#needs-link")).to_be_hidden()
    block = page.locator(".h-block[data-ref^='thread:']")
    expect(block.locator(".h-label")).to_have_text("Answer needed")
    expect(block.locator(".h-title")).to_have_text("Which project is the new grant for?")
    expect(block.locator("[data-part='row'] .btn")).to_have_text(["Done"])                  # on its folded row
    block.locator("> button").click()
    expect(block.get_by_role("button", name="Tell Tacitus", exact=True)).to_be_disabled()             # the note's, empty
    block.get_by_role("link", name="Open it there").click()
    expect(page.locator("#channel-name")).to_have_text("example-project")    # its room, its thread
    page.evaluate("location.hash = '#/needs'")
    expect(page.locator("#channel-name")).to_have_text("Home")               # no Needs you place
    assert not errors, errors


def test_needs_you_comes_back_when_the_owner_answers_in_action_requests(app):
    from playwright.sync_api import expect
    page, stub, errors = _with_talk(app)
    expect(page.locator("#needs-link")).to_be_hidden()
    page.click("#open-settings")
    page.click("#go-home")
    page.click("#home-rows label:has-text('In Action requests')")
    wait_for(lambda: stub.home_posts and stub.home_posts[-1] == {"answers": "requests"})
    assert wait_for(lambda: page.evaluate("$('needs-link').hidden") is False)
    page2, _stub2, errors2 = _with_talk(app, one=False)
    assert page2.evaluate("$('needs-link').hidden") is False
    assert page2.locator(".h-block[data-ref^='thread:']").count() == 0
    assert not errors and not errors2, errors + errors2


# ── an item unfolds in place: its mail, what Tacitus has, one note box ──

MAIL = {"conv-r1": [{"id": "m-1", "from": {"name": "Sam Rivera", "address": "sam.rivera@example.org"},
                     "sent": "2026-11-20T15:00:00Z", "subject": "Seminar date",
                     "text": "Could you give the seminar in March?\nAny Thursday works for us."}]}


def _unfold(app, **kw):
    page, stub, errors, state = one_list_app(app, **kw)
    stub.mails = MAIL
    return page, stub, errors, state


def test_a_reply_unfolds_in_place_with_its_mail_what_tacitus_has_and_one_note_box(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = _unfold(app, **PHONE)
    block = page.locator(".h-block[data-ref='reply:response-r1']")
    expect(block.locator("[data-part='row'] .btn")).to_have_text(["Later", "No reply needed"])   # folded
    expect(block.locator(".h-body")).to_be_hidden()
    assert block.get_by_role("button", name="Draft").count() == 0
    hash_before = page.evaluate("location.hash")
    block = opened(page, "reply:response-r1")
    assert page.evaluate("location.hash") == hash_before                         # in place: no new page
    parts = block.locator(".h-body > [data-part]").evaluate_all("(ns) => ns.map((n) => n.dataset.part)")
    assert parts.index("mail") < parts.index("brief") < parts.index("tell")      # mail, what Tacitus has, the note
    mail = block.locator("details.card-quoted")
    expect(mail.locator("summary")).to_have_text("The email from Sam Rivera, Nov 20")
    assert mail.get_attribute("open") is None                                    # folded, as on a Send card
    mail.locator("summary").click()
    expect(mail.locator("pre.card-body")).to_have_text("Could you give the seminar in March?\nAny Thursday works for us.")
    assert stub.mail_reads == ["conv-r1"] and stub.note_presses == [] and stub.acts == []   # no turn on unfold
    expect(block.locator("[data-part='row'] .btn")).to_have_text(["Later", "No reply needed"])   # the row keeps them
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")    # the phone's width
    assert not errors, errors


def test_quick_answers_fill_the_note_box_and_an_empty_note_cannot_be_sent(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = _unfold(app)
    block = opened(page, "reply:response-r1")
    box, ask = block.locator("[data-part='tell'] textarea"), block.get_by_role("button", name="Tell Tacitus", exact=True)
    expect(block.locator("label").filter(has_text="Tell Tacitus what to do")).to_be_visible()
    expect(ask).to_be_disabled()
    box.fill("   ")
    expect(ask).to_be_disabled()
    block.get_by_role("button", name="Yes", exact=True).click()
    expect(box).to_have_value("Yes")
    expect(box).to_be_focused()
    page.keyboard.type(", but after Dec 21")
    expect(box).to_have_value("Yes, but after Dec 21")
    block.get_by_role("button", name="No", exact=True).click()
    expect(box).to_have_value("No")                                              # a chip replaces the words
    expect(ask).to_be_enabled()
    assert stub.acts == [] and stub.note_presses == []                           # a chip never acts
    assert not errors, errors


def test_a_note_sends_once_and_its_answer_shows_on_the_item(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = _unfold(app, **PHONE)
    stub.deeper_delay = 0.3                                                      # its briefing comes as Tacitus works
    block = opened(page, "reply:response-r1")
    box = block.locator("[data-part='tell'] textarea")
    box.fill("When is the seminar room free?")
    block.get_by_role("button", name="Tell Tacitus", exact=True).click()
    expect(block.locator(".h-tell .req-busy")).to_have_text("Tacitus is working on it.")
    expect(block.locator(".h-asked")).to_have_text("You asked: When is the seminar room free?")
    expect(box).to_have_value("")
    expect(block.get_by_role("button", name="Tell Tacitus", exact=True)).to_be_disabled()
    box.fill("And the date?")
    box.press("Enter")
    expect(block.get_by_role("button", name="Tell Tacitus", exact=True)).to_be_disabled()  # one turn at a time
    assert [w for _i, w in stub.note_presses] == ["When is the seminar room free?"]
    ((info, _w),) = stub.note_presses
    assert info["key"] == "conv-r1" and info["lid"] == "response-r1"
    assert [a["choice"] for a in stub.acts] == ["note"]
    expect(page.locator(".h-block[data-ref='reply:response-r1'] .h-detail")).to_have_text("Tacitus is working on your note.")
    stub.notes[0].update(state="answered", said="It is free on March 5 and March 12.")
    stub.emit_item("reply:response-r1")
    expect(block.locator(".h-answer")).to_have_text("It is free on March 5 and March 12.")
    link = block.get_by_role("link", name="Open the conversation")
    assert link.get_attribute("href").endswith("/t-note")                       # the conversation stays reachable
    expect(block.get_by_role("button", name="Tell Tacitus", exact=True)).to_be_enabled()   # the words typed are kept
    expect(block.locator(".h-deeper .text")).to_be_visible()                     # the briefing is in
    expect(block.locator(".req-busy")).to_have_count(0)                          # no line says Tacitus still reads
    assert page.evaluate("location.hash") == "#/home"                            # he stays where he pressed
    assert not errors, errors


def test_with_one_list_off_an_item_opens_as_before(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = one_list_app(app, {"one_list": False})
    stub.mails = MAIL
    block = page.locator(".h-block[data-ref='reply:response-r1']")
    assert block.locator(".h-acts").count() == 0
    block = opened(page, "reply:response-r1")
    expect(block.locator("[data-part='choices'] .btn").first).to_have_text("Draft")
    assert block.locator("textarea, details.card-quoted").count() == 0 and stub.mail_reads == []
    assert not errors, errors


def test_the_folded_rows_buttons_act_without_unfolding(app):
    from playwright.sync_api import expect
    page, stub, errors, _state = _unfold(app, **PHONE)
    block = page.locator(".h-block[data-ref='reply:response-r1']")
    block.locator("[data-part='row']").get_by_role("button", name="Later").click()
    days = block.locator(".h-acts [data-part='in']")
    expect(days.get_by_role("button", name="Tomorrow")).to_be_visible()          # under the row, still folded
    expect(block.locator(".h-body")).to_be_hidden()
    days.get_by_role("button", name="Tomorrow").click()
    expect(block.locator(".h-said")).to_have_text("Later. It comes back tomorrow.")
    expect(block.get_by_role("button", name="Undo")).to_be_visible()
    assert [a["choice"] for a in stub.acts] == ["later"] and stub.note_presses == [] and stub.mail_reads == []
    other = page.locator(".h-block[data-ref='reply:response-r2']")
    other.locator("[data-part='row']").get_by_role("button", name="No reply needed").click()
    expect(other.locator(".h-said")).to_have_text("No reply needed. It is off your list.")
    assert not errors, errors
