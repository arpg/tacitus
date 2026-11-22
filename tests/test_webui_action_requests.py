"""The web app in a browser: Action requests as blocks (docs/webchat.md,
Action requests).

A request folds to what to decide and opens in place with its own
buttons, an answered one is one quiet line, a decision asks its question
and drafts the reply from one answer, and a link to a request opens its
block in place.

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
    PHONE, SIDEWAYS, SIZES, SMALL_TARGETS, _request, app, browser, open_thread, stub_factory,
    wait_for,
)


# -- Action requests as blocks (docs/webchat.md, Action requests) -------------

REQS = "#thread-list .reqs:not(.done) > .req"


def test_a_letter_card_shows_names_without_internal_unicode_delimiters(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    subject = "Recommendation letter for Avery Quinn"
    _request(stub, ":memo: *" + subject + "*", kind="letter", fields=[
        {"label": "From", "value": "\ue000Avery Quinn\ue001"},
        {"label": "Subject", "value": "\ue000" + subject + "\ue001"},
        {"label": "When", "value": "\ue000Next due Dec 1\ue001"}])
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(REQS).filter(has=page.locator(".req-what", has_text=subject))
    expect(block.locator(".req-what")).to_have_text(subject)
    expect(block.locator(".req-who")).to_have_text("From Avery Quinn · Next due Dec 1")
    block.locator(".req-head").click()
    expect(block.locator(".card-fields dd")).to_have_text(["Avery Quinn", subject, "Next due Dec 1"])
    assert "[U+E000]" not in block.inner_text() and "[U+E001]" not in block.inner_text()
    assert not errors, errors


def _requests(stub):
    """Four waiting requests with made-up data: an overdue reply (its
    deadline yesterday), a meeting that starts within a day, a calm reply,
    and an old card with no fields; two projects."""
    from datetime import date, datetime, timedelta
    now = time.time()
    over = _request(stub, "Send this reply?", at=now - 4 * 86400, fields=[
        {"label": "To", "value": "pat@example.org, lee@example.org, sam@example.org"},
        {"label": "CC", "value": "kim@example.org"}, {"label": "Subject", "value": "Re: Grant report figures"}],
        body="Hi Pat,\n\nThe figures are attached.\n\nThanks",
        actions=[{"id": "approve_send", "label": ":outbox_tray: Send", "value": {"d": 1}, "style": "primary", "word": "send"},
                 {"id": "edit_draft", "label": ":pencil2: Edit", "value": {"d": 1}, "style": "", "word": "edit"},
                 {"id": "cancel_send", "label": "Cancel", "value": {"d": 1}, "style": "danger", "word": "cancel"}])
    soon = _request(stub, "Create this event?", at=now - 3600, kind="event", fields=[
        {"label": "Subject", "value": "Review panel"}, {"label": "When", "value": "Thu, 10:00 to 10:30 AM"},
        {"label": "Attendees", "value": "sam@example.org"}],
        actions=[{"id": "approve_event", "label": "Create", "value": {"e": 1}, "style": "primary", "word": "create"}])
    calm = _request(stub, "Send this reply?", at=now - 600, fields=[
        {"label": "To", "value": "dana@example.org"}, {"label": "Subject", "value": "Re: Room for Friday"}])
    old = _request(stub, "Upload the letter?", at=now - 1200, kind="plugin")
    stub.request_context.update({
        over["id"]: {"project": "grant-report", "due": (date.today() - timedelta(days=1)).isoformat()},
        soon["id"]: {"project": "review-panel", "starts": (datetime.now() + timedelta(hours=20)).isoformat()}})
    return over, soon, calm, old


@SIZES
def test_a_request_folds_to_what_to_decide_and_opens_in_place_with_its_buttons(app, size):
    """Folded, a request shows its action in plain words, what and who it
    concerns, why it is urgent and its project, with no button of the
    card's; the most urgent first, the edge's colour carried by a word. A
    tap (or Enter) opens it in place to the card as it is, To, CC, subject
    and body with the same buttons, and a press goes through the card's own
    route. An old card with no fields shows its title."""
    from playwright.sync_api import expect
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    over, soon, calm, old = _requests(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    blocks = page.locator(REQS)
    expect(blocks).to_have_count(4)
    assert [blocks.nth(i).get_attribute("class") for i in range(4)] == [
        "req u-overdue", "req u-soon", "req u-calm", "req u-calm"]
    first = blocks.first
    expect(first.locator(".req-act")).to_have_text("Send email")
    expect(first.locator(".req-flag")).to_have_text("Overdue")
    expect(first.locator(".req-what")).to_have_text("Re: Grant report figures")
    expect(first.locator(".req-who")).to_have_text("To pat@example.org, lee@example.org, sam@example.org")
    expect(first.locator(".req-meta")).to_contain_text("Was due ")
    expect(first.locator(".req-proj")).to_have_text("grant-report")
    second = blocks.nth(1)
    expect(second.locator(".req-act")).to_have_text("Create meeting")
    expect(second.locator(".req-flag")).to_have_text("Soon")
    expect(second.locator(".req-who")).to_have_text("Attendees sam@example.org · Thu, 10:00 to 10:30 AM")
    expect(second.locator(".req-meta")).to_contain_text("Starts ")
    expect(blocks.nth(2).locator(".req-flag")).to_have_count(0)  # calm: no word, no colour of its own
    expect(blocks.nth(3).locator(".req-act")).to_have_text("Upload the letter?")  # an old card: its title
    # folded, nothing of the card's: no Send anywhere, no body
    expect(page.locator("#thread-list .card")).to_have_count(0)
    expect(page.locator("#thread-list [data-action], #thread-list .card-actions")).to_have_count(0)
    assert page.locator("#thread-list button").all_inner_texts().count("Send") == 0
    head = first.locator(".req-head")
    expect(head).to_have_attribute("aria-expanded", "false")
    head.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".req.open .req-head")).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".req.open .req-head")).to_be_focused()
    card = page.locator(".req.open .req-body .card")
    expect(card.locator(".card-fields dd")).to_have_text(
        ["pat@example.org, lee@example.org, sam@example.org", "kim@example.org", "Re: Grant report figures"])
    expect(card.locator(".card-body")).to_contain_text("The figures are attached.")
    assert page.evaluate("getComputedStyle(document.querySelector('.req.open .card-body')).maxHeight") == "none"
    expect(card.locator(".card-actions button")).to_have_text([re.compile("Send$"), re.compile("Edit$"), "Cancel"])
    assert [card.locator(".card-actions button").nth(i).get_attribute("data-action") for i in range(3)] == \
        ["approve_send", "edit_draft", "cancel_send"]  # the card's own buttons
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        page.set_viewport_size({"width": 382, "height": 760})  # the narrowest phone in use, folded
        page.wait_for_timeout(200)
        assert page.evaluate(SIDEWAYS) == [0, 0]
        assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
    card.locator("button:has-text('Send')").click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": over["channel"], "id": over["id"], "action_id": "approve_send",
                            "value": {"d": 1}}]  # the card's own route, its own value
    expect(blocks).to_have_count(3)  # answered, it folds under Earlier
    expect(page.locator("#earlier-toggle")).to_have_text("Earlier (2)")
    assert not errors, errors


def test_an_answered_request_is_one_quiet_line_and_opens_to_what_happened(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.evaluate("location.hash = '#/stream%3A7'")
    page.click("#earlier-toggle")
    line = page.locator("#thread-list .reqs.done .req").first
    expect(line.locator(".req-sum")).to_have_text(re.compile(r"^Send email: Re: Room booking · .*Sent to someone@example\.org\.$"))
    expect(line.locator(".req-time")).to_be_visible()
    assert line.bounding_box()["height"] < 60
    line.locator(".req-head").click()
    expect(line.locator(".req-body .card.status")).to_contain_text("Sent to someone@example.org.")
    expect(line.locator(".req-body button")).to_have_count(0)  # an answered card keeps no button
    assert not errors, errors


def _decision(stub, picks=True, at=None, **question):
    """A Replies row that waits on the owner's decision, as the bot posts it
    for the web chat: its quick answers, the answer line, Skip and Not
    needed, and the question it asks."""
    import webui_stub_server as W
    from datetime import datetime, timedelta
    value = {"row": "response-r1", "batch": "b1"}
    acts = ([{"id": "reply_choice_one", "label": ":one: Yes, I will review it", "style": "", "ask": "",
              "value": dict(value, choice="Yes, I will review it"), "word": "one"},
             {"id": "reply_choice_two", "label": ":two: No, I cannot make the deadline", "style": "",
              "value": dict(value, choice="No, I cannot make the deadline"), "word": "two", "ask": ""},
             {"id": "reply_draft", "label": ":pencil2: Other answer", "value": value, "style": "", "word": "other",
              "ask": "your answer"}] if picks else
            [{"id": "reply_draft", "label": ":pencil2: Draft the reply", "value": value, "style": "primary",
              "word": "draft", "ask": "your answer"}])
    acts += [{"id": "reply_skip", "label": ":fast_forward: Skip 3 days", "value": value, "style": "", "word": "skip", "ask": ""},
             {"id": "reply_not_needed", "label": "Not needed", "value": value, "style": "danger", "word": "not needed", "ask": ""}]
    q = {"text": "Will you review the workshop paper by December 1?", "from": "Pat",
         "since": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"), "about": "Review request", **question}
    return stub.add(W.DM, "", kind="card", event="card", at=at, card={
        "kind": "replies", "title": ":inbox_tray: *Review request*\nFrom Pat, 2 days ago.", "fields": [], "lines": [],
        "body": "", "quoted": "", "actions": acts, "select": None, "block_id": "", "footer": "", "fallback_text": "Review",
        "thread_ts": "", "ea_channel": W.DM, "content_fingerprint": "", "no_default": picks, "question": q})


@SIZES
def test_a_decision_asks_its_question_and_drafts_the_reply_from_one_answer(app, size):
    """A row that waits on the owner's decision folds to Answer needed and
    its question. Opened, the question is the headline, with who wrote and
    how long it has waited, then one Your answer control: the quick
    answers, a line, and Draft the reply, pressable only with an answer.
    Enter sends the line through the card's own action; the block shows the
    reply being written in place, then the draft's Send request opens in its
    place in the same list."""
    from playwright.sync_api import expect
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    stub.reply_flow = True
    page.wait_for_selector(".thread-item")
    m = _decision(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    expect(block.locator(".req-act")).to_have_text("Answer needed")
    expect(block.locator(".req-what")).to_have_text("Will you review the workshop paper by December 1?")
    expect(block.locator(".req-who")).to_have_text("From Pat")
    expect(block.locator(".card, [data-action]")).to_have_count(0)  # folded: nothing to press
    block.locator(".req-head").click()
    card = block.locator(".card.answer")
    expect(card.locator(".answer-q")).to_have_text("Will you review the workshop paper by December 1?")
    expect(card.locator(".answer-from")).to_have_text("From Pat · waiting 2 days")
    expect(block.locator(".req-what")).to_have_count(0)  # the question once, as the headline
    expect(card.locator("legend")).to_have_text("Your answer")
    picks, field = card.locator(".answer-pick"), card.locator(".answer-ctl input")
    expect(picks).to_have_text(["Yes, I will review it", "No, I cannot make the deadline"])
    go = card.locator(".answer-ctl .btn.primary")
    expect(go).to_have_text("Draft the reply")
    expect(go).to_be_disabled()
    field.press("Enter")
    page.wait_for_timeout(200)
    assert stub.clicks == []  # no answer, no press
    picks.first.click()
    expect(picks.first).to_have_attribute("aria-pressed", "true")
    expect(go).to_be_enabled()
    field.fill("Yes, if the deadline can move to December 8.")
    expect(picks.first).to_have_attribute("aria-pressed", "false")  # a typed line takes the place of a quick answer
    field.fill("")
    expect(go).to_be_disabled()
    expect(card.locator(".answer-more .btn")).to_have_text(["Skip 3 days", "Not needed"])  # quiet, below
    expect(card).not_to_contain_text("in this thread")
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        page.set_viewport_size({"width": 390, "height": 420})  # the keyboard takes the lower half
        field.focus()
        # The field scrolls into what the keyboard leaves 250 ms after the focus (app.js), later on a busy machine.
        expect(field).to_be_in_viewport(ratio=1)
        box = field.bounding_box()
        assert 0 <= box["y"] and box["y"] + box["height"] <= 420, box
        page.set_viewport_size({"width": 382, "height": 760})
        assert page.evaluate(SIDEWAYS) == [0, 0]
        assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
    field.fill("Yes, if the deadline can move to December 8.")
    field.press("Enter")
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": m["channel"], "id": m["id"], "action_id": "reply_draft",
                            "value": {"row": "response-r1", "batch": "b1"},
                            "text": "Yes, if the deadline can move to December 8."}]
    expect(page.locator(f'.req.u-working[data-id="{m["id"]}"] .req-busy')).to_have_text(
        "Writing the reply: Yes, if the deadline can move to December 8.")
    send = page.locator(".req.open:has(.card button:has-text('Send'))")
    expect(send).to_be_visible(timeout=8000)  # the draft's Send request, opened in its place
    expect(send.locator(".card-body")).to_contain_text("Yes, if the deadline can move to December 8")
    assert page.locator(f'.req[data-id="{m["id"]}"]').count() == 0 or \
        page.locator(f'.req.open[data-id="{m["id"]}"]').count() == 0
    assert not errors, errors


def test_a_quick_answer_goes_through_its_own_button_and_skip_stays_quiet(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    m = _decision(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    page.locator(f'.req[data-id="{m["id"]}"] .req-head').click()
    card = page.locator(".req.open .card.answer")
    card.locator(".answer-pick").nth(1).click()
    card.locator(".answer-ctl .btn.primary").click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": m["channel"], "id": m["id"], "action_id": "reply_choice_two",
                            "value": {"row": "response-r1", "batch": "b1", "choice": "No, I cannot make the deadline"}}]
    expect(page.locator(f'.req[data-id="{m["id"]}"]')).to_have_count(0)  # answered: under the shut Earlier
    other = _decision(stub)
    page.locator(f'.req[data-id="{other["id"]}"] .req-head').click()
    quiet = page.locator(f'.req[data-id="{other["id"]}"] .answer-more .btn')
    assert all("plain" in (quiet.nth(i).get_attribute("class") or "") for i in range(2))
    quiet.nth(1).click()  # Not needed: the card's own action, as before
    wait_for(lambda: len(stub.clicks) == 2)
    assert stub.clicks[1]["action_id"] == "reply_not_needed"
    assert not errors, errors


def test_a_redraw_keeps_the_open_decision_its_answer_and_the_words_typed(app):
    """Action requests is drawn again on a card's arrival and on a read: the
    open block whose request has not changed stays as it is, with the quick
    answer chosen, the words typed in its field and the focus there."""
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    m = _decision(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    block.locator(".req-head").click()
    card = block.locator(".card.answer")
    card.locator(".answer-pick").first.click()
    field = card.locator(".answer-ctl input")
    field.focus()
    page.keyboard.type("Yes, if")
    page.evaluate("document.querySelector('.place-note').dataset.old = '1'")
    other = _request(stub, "Send the agenda?")  # a card comes: the list is read and drawn again
    expect(page.locator(f'.req[data-id="{other["id"]}"]')).to_have_count(1)
    expect(page.locator(".place-note[data-old]")).to_have_count(0)
    page.keyboard.type(" the deadline moves.")
    expect(field).to_have_value("Yes, if the deadline moves.")
    expect(field).to_be_focused()
    field.press("Enter")
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["text"] == "Yes, if the deadline moves."
    assert not errors, errors


def test_a_draft_that_did_not_start_offers_try_again_with_the_answer_kept(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    m = _decision(stub, picks=False, failed=True, answer="Yes, if the deadline can move.")
    page.evaluate("location.hash = '#/stream%3A7'")
    page.locator(f'.req[data-id="{m["id"]}"] .req-head').click()
    card = page.locator(".req.open .card.answer")
    expect(card.locator(".answer-failed")).to_have_text("The draft did not start. Your answer is kept: tap Try again.")
    expect(card.locator(".answer-ctl input")).to_have_value("Yes, if the deadline can move.")
    go = card.locator(".answer-ctl .btn.primary")
    expect(go).to_have_text("Try again")
    expect(go).to_be_enabled()
    expect(card).not_to_contain_text("Ask me here")
    go.click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["text"] == "Yes, if the deadline can move."
    assert not errors, errors


def test_tacitus_s_one_more_question_opens_in_place_with_the_same_answer_control(app):
    """When the turn needs one more fact, the answered card links to a new
    card that asks it; the open block gives way to that one, which takes
    the answer the same way."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    m = _decision(stub)
    page.evaluate("location.hash = '#/stream%3A7'")
    page.locator(f'.req[data-id="{m["id"]}"] .req-head').click()
    follow = _decision(stub, picks=False, text="Which rate should the renewal state, the old or the new?")
    stub.change(m, "status", kind="status", text=":speech_balloon: One more answer is needed: "
                f"<http://127.0.0.1/chat/#/{W.DM.replace(':', '%3A')}/{follow['thread_key']}/{follow['id']}|open it>")
    opened = page.locator(f'.req.open[data-id="{follow["id"]}"]')
    expect(opened.locator(".answer-q")).to_have_text("Which rate should the renewal state, the old or the new?")
    expect(opened.locator(".answer-pick")).to_have_count(0)
    expect(opened.locator(".answer-ctl input")).to_have_attribute("placeholder", "Type your answer")
    expect(opened.locator(".answer-ctl input")).to_be_visible()
    assert not errors, errors


def test_a_link_to_a_request_opens_its_block_in_place(app):
    """The bot's pointer in a conversation links to the card's message: the
    link opens Action requests with that request open, never a thread."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    open_thread(page, "Draft a reply")
    req = _request(stub, "Send this reply?", fields=[{"label": "Subject", "value": "Re: Budget"}])
    stub.emit_bot(W.MAIN, f":lock: The Send button for this draft is in Action requests: "
                  f"[open it](/chat/#/stream%3A7/{req['thread_key']}/{req['id']})", thread_key=stub.messages[0]["thread_key"])
    page.locator("#messages a:has-text('open it')").click()
    expect(page).to_have_url(re.compile("#/stream%3A7$"))
    opened = page.locator(f'.req[data-id="{req["id"]}"]')
    expect(opened.locator(".req-head")).to_have_attribute("aria-expanded", "true")
    expect(opened.locator(".card button:has-text('Send')")).to_be_visible()
    assert not errors, errors
