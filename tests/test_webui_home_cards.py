"""Home's items opened in place, as a person sees them (docs/webchat.md,
Home items): a tap or Enter opens an item's card, one at a time, with the
quick briefing, the deeper briefing the bot stored with its steps first
and the item's choices after them, all at once (a briefing asked for at
the tap, or a stale one's newer copy, comes in below everything); a long
briefing folded; a draft that keeps the owner on Home with a link; a
reply's own line, Later's days, a choice's line, Undo counting down to
the bot's window and the item leaving after it, a decision drawn as Action
requests draws it, the deeper briefing's working, stale, failed and capped
states, a Home read and the deeper briefing arriving while the owner
types, errors in plain words, the ring, kept mail, So far today, a device
that may not press card buttons, and the phone and desktop widths.

The bot's own code answers (tests/webui_stub_server.py, `home_src`): Home,
its items, the choices and Undo are home_screen's and home_items' on the
made-up sources of tests/home_fixtures.py; only home_deeper's model is a
stand-in. Made-up data only."""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import home_fixtures as F  # noqa: E402
import webui_stub_server as W  # noqa: E402
from webui_browser import (  # noqa: E402,F401  (the fixtures)
    SIDEWAYS, SMALL_TARGETS, app, browser, drawer_drawn, settled, stub_factory, wait_for,
)

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
BUSY = "Reading the messages..."
WRITING = "Tacitus is writing the reply. Its Send card comes to Action requests."


def sources(state="normal", decision=False):
    """Home's sources for the stub: one state of tests/home_fixtures.py, its
    second reply a decision with two answers where `decision`."""
    def src(tz=""):
        got = F.sources(state, dm=W.DM, main=W.MAIN)
        if decision:
            got.replies[1]["queued"] = {"outcome": "decision", "note": "Do you sign it as the lab's lead?",
                                        "choices": ["Yes, I can sign it", "No, ask the lab manager"]}
        return got
    return src


def home_app(app, state="normal", decision=False, **kw):
    """The app on Home as the bot builds it, its stream open (the events
    below reach it), the first read drawn."""
    from playwright.sync_api import expect
    kw.setdefault("context", CLOCK)
    page, stub, errors = app(home_src=sources(state, decision), **kw)
    expect(page.locator("#live.on")).to_be_attached()
    expect(page.locator(".h-main .h-block").first).to_be_visible()
    settled(page)
    return page, stub, errors


def opened(page, ref):
    """The block of item `ref`, opened with a tap on its head, once its card
    is drawn from the bot's answer."""
    from playwright.sync_api import expect
    block = page.locator(f".h-block[data-ref='{ref}']")
    block.locator("> button").click()
    expect(block.locator("> button")).to_have_attribute("aria-expanded", "true")
    expect(block.locator("[data-part='brief'] .h-quiet")).to_have_count(0)  # no longer Loading...
    return block


def act(stub, ref, choice, arg=None):
    """A POST /api/home/act as the page sends it: the item's version with it."""
    return {"ref": ref, "choice": choice, "version": stub.real.item(ref)["version"], **({} if arg is None else {"arg": arg})}


def finish(stub, ref):
    """The deeper briefing of `ref` is ready, as the bot's job ends."""
    job = stub.deepers[ref]
    job.clear()
    job.update(stub.deeper_result, at=1.0)
    stub.emit_item(ref)


def order(block):
    """The parts of an open card, in the order it shows them."""
    return block.evaluate("(e) => [...e.querySelector('.h-body').children].filter((c) => c.childElementCount)"
                          ".map((c) => c.dataset.part)")


def test_a_stored_briefing_opens_complete_above_the_choices_its_steps_first(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, **PHONE)
    ref = "item:deadline-w1"
    todo = opened(page, ref)
    deeper = todo.locator(".h-deeper")
    expect(deeper.locator(".text")).to_contain_text("The thread has three messages.")
    assert order(todo) == ["brief", "deeper", "choices", "links"]  # its steps, then the item's own choices
    expect(deeper.locator(".text > p").first).to_have_text("Now")  # the moment's lines, added at the tap
    expect(deeper.locator(".text li").first).to_have_text(re.compile(r"^It is due "))
    more = deeper.get_by_role("button", name="Show more")
    expect(deeper.locator(".text")).to_have_class(re.compile(r"\bh-fold\b"))  # long: folded to a few lines
    expect(more).to_have_attribute("aria-expanded", "false")
    more.click()
    expect(deeper.locator(".text")).not_to_have_class(re.compile(r"\bh-fold\b"))
    expect(deeper.get_by_role("button", name="Show less")).to_have_attribute("aria-expanded", "true")
    labels = [st["label"] for st in stub.real.item(ref)["deeper"]["steps"]]
    assert labels[1:] == ["Mark it done", "Continue in a conversation"] and labels[0].startswith("Wait until ")
    expect(deeper.locator("[role='group'] button")).to_have_text(labels)  # home_deeper's steps the item offers
    assert todo.locator(".req-busy").count() == 0
    expect(deeper).not_to_contain_text("Your own model")  # agent and model details are off
    page.evaluate("S.showDetails = true")
    opened(page, "reply:response-r2")
    todo = opened(page, ref)
    expect(todo.locator(".h-deeper .h-quiet")).to_contain_text("Your own model · ")
    settled(page)
    assert stub.deeper_posts == []  # nothing to write at tap time
    assert not errors, errors


def test_a_tap_shows_the_quick_briefing_and_the_choices_at_once_and_a_missing_briefing_below_them(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    stub.deeper_stored, stub.deeper_delay = False, None  # none written ahead; the job works until the test ends it
    ref = "reply:response-r2"
    block = page.locator(f".h-block[data-ref='{ref}']")
    head = block.locator(".h-item")
    expect(head).to_have_attribute("aria-expanded", "false")
    expect(block.locator(".h-body")).to_be_hidden()
    head.click()
    expect(head).to_have_attribute("aria-expanded", "true")
    expect(head).to_have_attribute("aria-controls", f"hb-{ref}")
    brief = block.locator(".h-brief")
    expect(brief.locator("p")).to_have_text(["A reply you owe Alex Kim.", "Sign the data access form and send it back.",
                                             "This needs a form filled in, not just a reply. Ask Tacitus to start it."])
    expect(brief.locator("li")).to_have_text(["It has waited 4 days for your reply.", "You last wrote on it 5 days ago."])
    choices = block.locator(".h-choices button")
    expect(choices).to_have_text(["Draft a reply", "Tell Tacitus what to say", "Not needed", "Later"])
    expect(block.locator(".h-choices [role='group']")).to_have_attribute("aria-label", "Next step")
    expect(block.locator(".h-links button")).to_have_text(["Ask about this"])
    expect(block.locator(".h-deeper .req-busy")).to_have_text(BUSY)
    expect(block.locator(".h-deeper [role='status']")).to_have_count(1)
    wait_for(lambda: stub.deeper_posts == [ref])  # asked for on the open, never pressed for
    settled(page)
    thumb = [choices.nth(i).bounding_box() for i in range(4)] + [block.locator(".h-links button").bounding_box()]
    finish(stub, ref)
    deeper = block.locator(".h-deeper")
    expect(deeper.locator(".text")).to_contain_text("The thread has three messages.")
    expect(deeper.locator(".text li")).to_have_count(4)
    steps = deeper.locator("[role='group'] button")
    expect(steps).to_have_count(4)
    # Words too long for the label show once, under the action's name.
    expect(steps.first).to_have_text("Draft a reply Thursday at 2:00 works for me. The room is held.")
    expect(steps.first.locator(".h-detail")).to_have_text("Thursday at 2:00 works for me. The room is held.")
    expect(steps.last).to_have_text("Continue in a conversation")
    assert order(block) == ["brief", "choices", "links", "deeper"]  # it came after the card opened: below everything
    settled(page)
    # nothing a thumb was about to tap has moved: the deeper briefing comes in below it
    assert [choices.nth(i).bounding_box() for i in range(4)] + [block.locator(".h-links button").bounding_box()] == thumb
    # one open at a time; Enter on a head opens it too
    other = page.locator(".h-block[data-ref='request:w42']")
    other.locator(".h-item").focus()
    page.keyboard.press("Enter")
    expect(other.locator(".h-item")).to_have_attribute("aria-expanded", "true")
    expect(head).to_have_attribute("aria-expanded", "false")
    expect(block.locator(".h-body")).to_be_hidden()
    expect(other.locator(".h-choices .btn")).to_have_text(["Open it in Action requests"])
    page.keyboard.press("Tab")  # the card's own controls follow its head
    expect(other.get_by_role("link", name="Open it in Action requests")).to_be_focused()
    # a tap again shows the briefing the bot keeps, at once, above the choices, and asks for none
    gets = stub.item_gets.count(ref)
    head.click()
    expect(deeper.locator(".text")).to_be_visible()
    assert block.locator(".req-busy").count() == 0
    assert order(block) == ["brief", "deeper", "choices", "links"]
    wait_for(lambda: stub.item_gets.count(ref) == gets + 1)
    settled(page)
    assert stub.deeper_posts == [ref, "request:w42"]
    assert page.locator(".h-page").get_by_role("button", name="Send", exact=True).count() == 0  # Home never sends
    assert not errors, errors


def test_a_to_do_and_a_request_lead_to_their_places(app):
    """A to-do starts a conversation with Tacitus; a request opens its block
    in Action requests, where its Send stays."""
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    todo = opened(page, "item:deadline-w1")
    expect(todo.locator(".h-choices button")).to_have_text(["Start it with Tacitus", "Done", "Waiting on someone", "Later"])
    expect(todo.locator(".h-brief p")).to_have_text(["A deadline, from Jo Rivera.", "Send your review of the four workshop papers.",
                                                     "Due tomorrow.", "Nothing on its own. Start it when you are ready."])
    todo.get_by_role("button", name="Start it with Tacitus").click()
    expect(page.locator("#toast")).to_have_text("Opened a conversation with Tacitus about it.")
    started = wait_for(lambda: next((m for m in stub.messages if m["author"] == "owner" and "workshop" in m["text"]), None))
    assert started["channel"] == W.MAIN and started["text"].startswith("Let's start this: ")
    expect(page.locator("#messages .msg").first).to_be_visible()  # the conversation, sent
    assert stub.acts == [act(stub, "item:deadline-w1", "start")]
    page.click("#home-link")
    req = opened(page, "request:w41")
    req.get_by_role("link", name="Open it in Action requests").click()  # its place, with nothing to ask the bot
    expect(page.locator("#channel-name")).to_have_text("Action requests")
    assert len(stub.acts) == 1
    assert not errors, errors


def test_a_decision_is_answered_with_the_decision_control_of_action_requests(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, decision=True)
    ref = "reply:response-r2"
    q = opened(page, ref)
    card = q.locator(".card.answer")
    expect(card.locator("legend")).to_have_text("Your answer")
    picks, field, go = card.locator(".answer-pick"), card.locator(".answer-ctl input"), card.locator(".answer-ctl .btn.primary")
    expect(picks).to_have_text(["Yes, I can sign it", "No, ask the lab manager"])
    expect(go).to_have_text("Draft the reply")
    expect(go).to_be_disabled()
    expect(card.locator(".answer-more .btn")).to_have_text(["Not needed", "Later"])
    expect(card.locator(".answer-q")).to_have_count(0)  # the quick briefing above says what it is
    card.get_by_role("button", name="Later").click()
    expect(q.locator(".h-in button").first).to_have_text("Tomorrow")
    field.fill("Yes, from January.")
    picks.first.click()
    expect(field).to_have_value("")  # a quick answer takes the place of a typed line
    go.click()
    expect(q.locator(".h-note")).to_contain_text(WRITING)  # Home stays, with the way to the draft's line
    expect(q.locator(".h-note a")).to_have_text("Open it in Action requests")
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert stub.acts == [act(stub, ref, "answer:1")]
    field = q.locator(".answer-ctl input")
    field.fill("Yes, from January.")
    field.press("Enter")
    wait_for(lambda: len(stub.acts) == 2)
    assert stub.acts[1] == act(stub, ref, "answer", "Yes, from January.")
    assert not errors, errors


def test_done_says_what_it_did_counts_undo_down_and_the_item_leaves_after_it(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, **PHONE)
    ref = "item:deadline-w1"
    todo = opened(page, ref)
    stub.act_delay = 1.0  # Home is read again, without the item, before the bot's answer comes
    todo.get_by_role("button", name="Done", exact=True).click()
    expect(todo.locator(".h-note")).to_have_text("Working...")
    expect(page.locator(".h-tally")).to_contain_text("5 things left")
    expect(todo.locator(".h-note")).to_have_text("Working...")  # the card stays where it was
    expect(todo.locator(".h-said")).to_have_text("Done. It is off your list.")
    stub.act_delay = 0
    assert stub.acts == [act(stub, ref, "done")]
    expect(page.locator(".h-main .h-items > li")).to_have_count(6)  # it keeps its place while Undo lasts
    expect(todo).to_have_class(re.compile(r"\bu-done\b"))
    left = todo.locator(".h-undo .h-quiet")
    expect(left).to_have_text(re.compile(r"^(29|30) seconds to undo$"))
    expect(left).to_have_text(re.compile(r"^(27|28) seconds to undo$"))  # it counts down
    todo.get_by_role("button", name="Undo").click()
    wait_for(lambda: len(stub.undos) == 1)
    expect(todo.locator(".h-note")).to_have_text("Undone. It is back on your list.")
    expect(todo.locator(".h-choices button")).to_have_count(4)
    expect(page.locator(".h-tally")).to_contain_text("6 things left")
    assert stub.ledger_runs == []  # nothing was written in the window
    # Not needed on a reply, and this time the window runs out (the bot's clock 26 seconds into it)
    reply = opened(page, "reply:response-r2")
    not_needed = reply.locator(".h-choices").get_by_role("button", name="Not needed")
    not_needed.scroll_into_view_if_needed()  # in view: no scroll of the browser's own before the tap
    settled(page)
    top = page.evaluate("$('thread-list').scrollTop")
    assert top > 0
    stub.clock_offset = -26
    not_needed.click()
    assert page.evaluate("$('thread-list').scrollTop") == top
    expect(reply.locator(".h-said")).to_have_text("Not needed. No reply goes out.")
    expect(reply.locator(".h-undo .h-quiet")).to_have_text(re.compile(r"^[1-4] seconds to undo$"))
    expect(reply).to_have_count(0, timeout=8000)
    expect(page.locator(".h-main .h-items > li")).to_have_count(5)
    assert page.evaluate("$('thread-list').scrollTop") == top  # the list does not jump
    assert not errors, errors


def test_later_offers_its_days_and_a_date(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    ref = "reply:response-r2"
    reply = opened(page, ref)
    later = reply.get_by_role("button", name="Later")
    later.click()
    expect(later).to_have_attribute("aria-expanded", "true")
    days = next(c for c in stub.real.item(ref)["choices"] if c["id"] == "later")["options"]
    opts = reply.locator(".h-in [role='group'] button")
    expect(opts).to_have_text([o["label"] for o in days])  # Tomorrow, Monday (unless that is tomorrow), Pick a date
    expect(opts.first).to_be_focused()
    assert stub.acts == []  # nothing yet
    opts.last.click()
    day = reply.get_by_label("The date")
    expect(day).to_be_focused()
    expect(day).to_have_attribute("min", days[-1]["min"])
    expect(day).to_have_attribute("max", days[-1]["max"])
    when = (date.fromisoformat(days[-1]["min"]) + timedelta(days=9)).isoformat()
    day.fill(when)
    reply.get_by_role("button", name="Use this date").click()
    wait_for(lambda: stub.acts == [act(stub, ref, "later", when)])
    expect(reply.locator(".h-said")).to_have_text(re.compile(r"^Later\. It comes back .+\.$"))
    todo = opened(page, "item:deadline-w1")
    todo.get_by_role("button", name="Later").click()
    todo.get_by_role("button", name="Tomorrow", exact=True).click()
    wait_for(lambda: len(stub.acts) == 2)
    assert stub.acts[1] == act(stub, "item:deadline-w1", "later", "tomorrow")
    expect(todo.locator(".h-said")).to_have_text("Later. It comes back tomorrow.")
    assert not errors, errors


def test_tell_tacitus_what_to_say_takes_one_line_and_drafts_from_it(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    ref = "reply:response-r2"
    reply = opened(page, ref)
    reply.get_by_role("button", name="Tell Tacitus what to say").click()
    field = reply.get_by_role("textbox", name="Tell Tacitus what to say")
    expect(field).to_be_focused()
    field.press("Enter")
    page.wait_for_timeout(200)
    assert stub.acts == []  # no words, nothing sent
    field.fill("Yes, the form is signed. It goes out Friday.")
    expect(reply.get_by_role("button", name="Draft it")).to_be_visible()
    assert page.locator(".h-page").get_by_role("button", name="Send", exact=True).count() == 0
    field.press("Enter")
    note = reply.locator(".h-note")
    expect(note).to_contain_text(WRITING)  # Home stays
    expect(field).to_have_count(0)  # the line closed once the bot had it
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert stub.acts == [act(stub, ref, "say", "Yes, the form is signed. It goes out Friday.")]
    note.get_by_role("link", name="Open it in Action requests").click()  # where the draft comes, with its Send
    expect(page.locator("#channel-name")).to_have_text("Action requests")
    expect(page.locator(".req.open")).to_contain_text("Writing the reply: Yes, the form is signed. It goes out Friday.")
    assert not errors, errors


def test_a_home_read_and_the_deeper_briefing_arriving_keep_the_open_card_and_the_words_typed(app):
    """Home is drawn again on each `home` event, and the card again when its
    deeper briefing comes: the open card, the field being typed in (the same
    field, its words, its caret and focus), a quick answer chosen and the
    list's scroll all stay as they were."""
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, decision=True, **PHONE)
    stub.deeper_stored, stub.deeper_delay = False, None
    ref = "reply:response-r1"
    reply = opened(page, ref)
    reply.scroll_into_view_if_needed()
    reply.get_by_role("button", name="Tell Tacitus what to say").click()
    field = reply.get_by_role("textbox", name="Tell Tacitus what to say")
    page.evaluate("document.activeElement.dataset.mine = '1'")
    settled(page)
    top = page.evaluate("$('thread-list').scrollTop")
    for n, w in enumerate(["Thursday", " at", " 2", " works", " for", " me."]):
        page.evaluate("document.querySelector('.h-tally').dataset.old = '1'")
        reads = len(stub.home_gets)
        stub.set_home(stub.real_payload())
        if n == 2:
            finish(stub, ref)  # the deeper briefing comes while the owner types
        page.keyboard.type(w)
        wait_for(lambda: len(stub.home_gets) > reads)
        expect(page.locator(".h-tally[data-old]")).to_have_count(0)  # Home was drawn again
    expect(reply.locator(".h-deeper .text")).to_be_visible()
    expect(field).to_have_value("Thursday at 2 works for me.")
    assert page.evaluate("[document.activeElement.dataset.mine, document.activeElement.selectionStart]") == ["1", 27]
    expect(reply.locator(".h-item")).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("$('thread-list').scrollTop") == top
    # a quick answer chosen in a decision stays chosen through a redraw
    q = opened(page, "reply:response-r2")
    q.locator(".answer-pick").first.click()
    reads = len(stub.home_gets)
    stub.set_home(stub.real_payload())
    wait_for(lambda: len(stub.home_gets) > reads)
    finish(stub, "reply:response-r2")
    expect(q.locator(".h-deeper .text")).to_be_visible()
    expect(q.locator(".answer-pick").first).to_have_attribute("aria-pressed", "true")
    expect(q.locator(".answer-ctl .btn.primary")).to_be_enabled()
    assert not errors, errors


def test_the_deeper_briefing_keeps_working_while_the_owner_is_away_and_shows_on_return(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    stub.deeper_stored, stub.deeper_delay = False, None
    ref = "item:deadline-w1"
    todo = opened(page, ref)
    expect(todo.locator(".req-busy")).to_have_text(BUSY)
    drawer_drawn(page)
    page.click("#channels .chan:has-text('Ask Tacitus')")
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    page.click("#home-link")
    expect(todo.locator(".h-item")).to_have_attribute("aria-expanded", "true")
    expect(todo.locator(".req-busy")).to_have_text(BUSY)  # still at work
    page.click("#channels .chan:has-text('Ask Tacitus')")
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    finish(stub, ref)  # it comes while the owner is away
    page.click("#home-link")
    expect(todo.locator(".h-deeper .text")).to_contain_text("The thread has three messages.")
    expect(todo.locator(".req-busy")).to_have_count(0)
    assert stub.deeper_posts == [ref]  # one job
    assert not errors, errors


def test_a_failed_or_capped_deeper_briefing_says_so_and_leaves_the_rest(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    ref = "item:deadline-w1"
    stub.deeper_stored = False
    good = stub.deeper_result
    stub.deeper_result = {"state": "failed", "note": W.D().NOTE_FAILED}
    todo = opened(page, ref)
    deeper = todo.locator(".h-deeper")
    expect(deeper).to_contain_text("Tacitus could not write this now. Open it again in a minute.")
    expect(todo.locator(".h-choices button")).to_have_count(4)
    expect(todo.locator(".h-brief")).to_contain_text("A deadline, from Jo Rivera.")
    stub.deeper_result = good
    deeper.get_by_role("button", name="Try again").click()
    expect(deeper.locator(".text")).to_contain_text("The thread has three messages.")
    assert stub.deeper_posts == [ref, ref]
    # the day's limit, in the bot's words, and no job asked for
    capped = "reply:response-r2"
    stub.deepers[capped] = {"state": "unavailable", "note": W.D().NOTE_CAP.format(n=40)}
    reply = opened(page, capped)
    expect(reply.locator(".h-deeper")).to_have_text("You reached today's limit of 40 deeper briefings. They start again tomorrow.")
    expect(reply.locator(".h-choices button")).to_have_count(4)
    wait_for(lambda: stub.item_gets.count(capped) == 1)
    settled(page)
    assert stub.deeper_posts == [ref, ref]
    assert not errors, errors


def test_a_step_runs_as_its_choice_and_continue_in_a_conversation_opens_one(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    ref = "item:deadline-w1"
    todo = opened(page, ref)
    todo.get_by_role("button", name="Mark it done").click()  # home_deeper's step, run as the item's Done
    wait_for(lambda: stub.acts == [act(stub, ref, "step:s3")])
    expect(todo.locator(".h-said")).to_have_text("Done. It is off your list.")
    todo.get_by_role("button", name="Undo").click()
    expect(todo.locator(".h-note")).to_have_text("Undone. It is back on your list.")
    todo.get_by_role("button", name="Continue in a conversation").click()
    expect(page.locator("#toast")).to_have_text("Opened a conversation about it, with the briefing.")
    expect(page.locator("#messages .msg").first).to_be_visible()
    wait_for(lambda: len(stub.acts) == 2)
    assert stub.acts[1] == act(stub, ref, "step:continue")
    assert not errors, errors


def test_errors_are_plain_words_in_the_card(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    todo = opened(page, "item:deadline-w1")
    stub.home_src = stub.real._sources = sources("done")  # the row closed from elsewhere; no event yet
    done = todo.get_by_role("button", name="Done", exact=True)
    done.click()
    expect(todo.locator(".h-note")).to_have_text("This is not on your list any more.")
    expect(done).to_be_enabled()
    expect(todo.locator(".h-said")).to_have_count(0)
    # an item the bot no longer lists, opened: its words, and the way to its place
    req = page.locator(".h-block[data-ref='request:w42']")
    req.locator(".h-item").click()
    expect(req.locator(".answer-failed")).to_have_text("Tacitus could not open this item: This is not on your list any more.")
    expect(req.get_by_role("link", name="Open it there")).to_have_attribute("href", "#/stream%3A7/tw42/w42")
    assert stub.deeper_posts == []
    assert [e for e in errors if "status of 404" not in e] == [], errors  # the browser's own line for the 404


def test_the_ring_opens_the_first_items_card(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    expect(page.locator(".h-tally")).to_have_attribute("aria-label", "6 done today, 6 left for you. Open the first one.")
    page.click(".h-tally")
    first = page.locator(".h-main .h-block").first
    expect(first.locator(".h-item")).to_have_attribute("aria-expanded", "true")
    expect(first.locator(".h-item")).to_be_focused()
    expect(first.locator(".h-choices .btn")).to_have_text(["Open it in Action requests"])
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert not errors, errors


def test_kept_mail_opens_to_its_fixed_line_outlook_done_and_later(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "kept")
    for ref in ("item:row-k1", "reply:response-r1", "reply:response-r2"):
        block = opened(page, ref)
        expect(block.locator(".h-choices .btn")).to_have_text(
            ["Open in Outlook"] if ref == "item:row-k1" else ["Open in Outlook", "Done", "Later"])
        expect(block.locator(".h-deeper")).to_have_text(W.D().NOTE_KEPT_NO_LOCAL)
        assert block.locator(".h-brief *, .h-links *").count() == 0  # no words of its own, no Ask about this
        if ref == "item:row-k1":  # the post's own link
            outlook = block.get_by_role("link", name="Open in Outlook")
            expect(outlook).to_have_attribute("href", "https://outlook.example.com/mail/k1")
            expect(outlook).to_have_attribute("target", "_blank")
    block.get_by_role("button", name="Done", exact=True).click()
    expect(block.locator(".h-said")).to_have_text("Done. It is off your list.")
    assert stub.deeper_posts == []  # no model reads kept mail from here
    assert not errors, errors


def test_a_device_that_may_not_press_card_buttons_sees_homes_choices_locked_too(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    page.evaluate("S.me.card_actions = false")
    todo = opened(page, "item:deadline-w1")
    for b in todo.locator(".h-choices button, .h-deeper [role='group'] button").all():
        expect(b).to_be_disabled()
    expect(todo.locator(".h-choices .card-note")).to_contain_text("does not press card buttons unless you allow it there.")
    expect(todo.get_by_role("button", name="Ask about this")).to_be_enabled()
    assert not errors, errors


@pytest.mark.parametrize("width", [382, 390, 1280])
def test_an_open_card_fits_a_phone_and_the_desktop_keeps_two_columns(app, width):
    from playwright.sync_api import expect
    phone = width < 600
    page, stub, errors = home_app(app, viewport={"width": width, "height": 800},
                                  context={"has_touch": True, "is_mobile": True, **CLOCK} if phone else CLOCK)
    reply = opened(page, "reply:response-r2")
    expect(reply.locator(".h-deeper .text")).to_be_visible()
    reply.get_by_role("button", name="Tell Tacitus what to say").click()
    expect(reply.get_by_role("textbox")).to_be_focused()
    settled(page)
    assert page.evaluate(SIDEWAYS)[0] == 0
    assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
    if phone:
        page.evaluate("$('thread-list').style.height = 'auto'; $('thread-list').style.overflow = 'visible'")
        small = page.evaluate(SMALL_TARGETS.replace("a.thread-item", "a.thread-item, .h-page a, .h-item, .h-body input"))
        assert small == [], (width, small)
    else:
        main, side = page.locator(".h-main").bounding_box(), page.locator(".h-side").bounding_box()
        assert side["x"] >= main["x"] + main["width"] and side["y"] == main["y"]  # side by side
    assert not errors, errors


def test_a_row_of_so_far_today_opens_in_place_and_not_done_yet_reopens_it(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    ref = "closed:row-c3"
    row = opened(page, ref)
    expect(row.locator(".h-row")).to_contain_text("Newsletter sign-up")
    expect(row.locator(".h-brief p")).to_have_text(["Tacitus closed it at 12:02."])
    row.get_by_role("button", name="Not done yet").click()
    wait_for(lambda: stub.acts == [act(stub, ref, "reopen")])
    expect(row.locator(".h-note")).to_have_text("Opened again. It is back on your list.")
    assert stub.ledger_runs[0][:2] == ("ledger.py", "update")  # the web chat's reopen, recorded and never run
    drafted = opened(page, "drafted:w41")
    expect(drafted.get_by_role("link", name="Open it in Action requests")).to_have_attribute("href", "#/stream%3A7/tw41/w41")
    expect(row.locator(".h-body")).to_be_hidden()  # one open at a time, across both lists
    assert not errors, errors


def test_a_stale_briefing_shows_while_a_newer_one_is_written_below_everything(app):
    """A stored briefing older than its thread shows its text at once with a
    quiet line and no steps, the page asks for a new one, and the new one
    comes in where the old one was, below the choices; the next open shows it
    above them."""
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    stub.deeper_stale, stub.deeper_delay = True, None
    ref = "reply:response-r1"
    reply = opened(page, ref)
    deeper = reply.locator(".h-deeper")
    expect(deeper.locator(".req-busy")).to_have_text(BUSY)
    expect(deeper.locator(".text")).to_contain_text("The thread has three messages.")
    expect(deeper.locator("button:not(.small)")).to_have_count(0)  # no step from the old one
    wait_for(lambda: stub.deeper_posts == [ref])
    assert order(reply) == ["brief", "choices", "links", "deeper"]
    finish(stub, ref)
    expect(deeper.locator(".req-busy")).to_have_count(0)
    expect(deeper.get_by_role("button", name="Continue in a conversation")).to_be_visible()
    assert order(reply) == ["brief", "choices", "links", "deeper"]
    opened(page, "reply:response-r2")
    reply = opened(page, ref)
    assert order(reply) == ["brief", "deeper", "choices", "links"]
    assert not errors, errors
