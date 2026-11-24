"""Home in the web app, as a person sees it (docs/webchat.md, Home): the app
opens on it, the greeting and its thumbs, the day's split, the next gap,
what is left (each item opens in place: tests/test_webui_home_cards.py),
what changed and what got done, the
briefing that used to be a post, the done state, a red light, travel and
vacation, the `home` event, the check-in, the way back, the Settings page,
and the phone widths. The bot's side is in tests/test_home_screen.py; the
payloads are the bot's own (tests/home_fixtures.py). Made-up data only."""
from __future__ import annotations

import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import home_fixtures as F  # noqa: E402
from webui_browser import (  # noqa: E402,F401  (the fixtures)
    SIDEWAYS, SMALL_TARGETS, _request, app, browser, drawer_drawn, stub_factory, wait_for,
)

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
OK = {"level": "ok", "line": "All good", "conditions": [], "cleared": [], "as_of": time.time()}


def home(state="normal"):
    """Home as the bot builds it, its places named by the stub's channel ids."""
    import webui_stub_server as W
    return F.payload(state, dm=W.DM, main=W.MAIN)


def home_app(app, state="normal", **kw):
    """The app on Home as the bot builds it; its items, as the bot's own code
    answers them (tests/webui_stub_server.py, `home_src`)."""
    import webui_stub_server as W
    kw.setdefault("context", CLOCK)
    return app(home=home(state), home_src=lambda tz: F.sources(state, dm=W.DM, main=W.MAIN), **kw)


def test_the_app_opens_on_home_with_the_greeting_the_split_and_what_is_left(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, push=True)
    expect(page).to_have_url(f"{page.url.split('#')[0]}#/home")
    p = stub.home
    expect(page.locator("#channel-name")).to_have_text("Home")
    expect(page.locator(".h-greet")).to_have_text(p["greeting"]["text"])
    expect(page.locator(".h-day")).to_have_text(p["day"])
    t = p["tally"]
    assert t == {"done": 5, "left": 6}  # the draft that waits for Send is left, not also done
    expect(page.locator(".h-tally")).to_have_attribute("aria-label", "5 done today, 6 left for you. Open the first one.")
    expect(page.locator(".h-next")).to_contain_text("You have 50 minutes before your 2:00.")
    items = page.locator(".h-main .h-items > li")
    expect(items).to_have_count(6)
    expect(items.nth(0)).to_contain_text("Send email")
    expect(items.nth(0)).to_contain_text("Soon")
    expect(items.nth(1).locator(".h-item")).to_have_attribute("aria-expanded", "false")  # it opens in place
    # Home has no Send of its own: the buttons stay in the request's block
    assert page.locator(".h-page").get_by_role("button", name="Send", exact=True).count() == 0
    expect(page.locator("#home-link")).to_have_class("chan home-link active")
    expect(page.locator("#thread")).to_be_hidden()  # Home takes the conversation's room on a wide screen
    wait_for(lambda: stub.home_gets)
    assert stub.home_gets[0] == {"tz": F.ZONE_NAME, "checkin": True}
    wait_for(lambda: any(s.get("tag") == "home" for s in stub.seen))
    assert not errors, errors


def test_a_bot_without_home_opens_on_ask_tacitus(app):
    from playwright.sync_api import expect
    page, _stub, errors = app()
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    expect(page.locator("#home-link")).to_be_hidden()
    assert not errors, errors


def test_a_request_opens_its_block_there_and_an_item_asks_about_itself_in_a_new_conversation(app):
    """Opened in place, a request's card leads to its block in Action
    requests (Send stays there); any other item's one Ask opens a
    conversation where Tacitus names it, and nothing is sent for the owner."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = home_app(app)
    card = _request(stub, "Send this reply?")
    p = home()
    href = f"#/{W.DM}/{card['thread_key']}/{card['id']}"
    p["left"][0]["href"] = href
    stub.set_home(p)
    stub.items["request:w41"] = {**stub.real.item("request:w41"), "goto": href}  # the card the stub holds
    first = page.locator(".h-main .h-block").first
    first.locator(".h-item").click()
    first.get_by_role("link", name="Open it in Action requests").click()
    expect(page.locator("#channel-name")).to_have_text("Action requests")
    expect(page.locator(f".req[data-id='{card['id']}'] .req-body")).to_be_visible()
    page.click("#home-link")
    expect(page.locator(".h-greet")).to_be_visible()
    second = page.locator(".h-main .h-items > li").nth(1)
    second.locator(".h-item").click()
    expect(second.get_by_role("button", name="Ask", exact=True)).to_have_count(1)  # one Ask on the card
    second.get_by_role("button", name="Ask", exact=True).click()
    expect(page.locator("#messages .msg").first).to_contain_text("Data access form")
    expect(page.locator("#text")).to_have_value("")
    assert stub.posts == []  # nothing is sent until the owner sends it
    assert not errors, errors


def test_the_next_gap_opens_the_first_things_card(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    page.click(".h-next .btn")
    first = page.locator(".h-main .h-block").first
    expect(first.locator(".h-item")).to_have_attribute("aria-expanded", "true")
    expect(first.locator(".h-item")).to_be_focused()
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert not errors, errors


def test_the_thumbs_record_a_vote_once_and_a_plain_line_has_none(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "model")
    up = page.locator(".h-vote-btn[aria-label='More lines like this']")
    up.click()
    wait_for(lambda: stub.votes)
    assert stub.votes == [{"vote": "up", "text": stub.home["greeting"]["text"]}]
    expect(up).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".h-vote [role='status']")).to_have_text("Thanks. Noted.")
    expect(page.locator(".h-vote-btn").first).to_be_disabled()
    stub.set_home(home("overdue"))
    expect(page.locator(".h-greet")).to_have_text("1 thing is overdue. Start there.")
    expect(page.locator(".h-vote")).to_have_count(0)
    assert not errors, errors


def test_a_home_event_draws_the_done_state_and_reads_home_without_a_check_in(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    expect(page.locator(".h-greet")).to_be_visible()
    stub.set_home(home("done"))
    expect(page.locator(".h-clear")).to_contain_text("You are clear for today.")
    expect(page.locator(".h-main .h-items")).to_have_count(0)
    wait_for(lambda: len(stub.home_gets) >= 2)
    assert [g["checkin"] for g in stub.home_gets[1:]] == [False] * (len(stub.home_gets) - 1)
    assert not errors, errors


def test_a_red_light_says_so_first_and_opens_the_status_sheet(app):
    from playwright.sync_api import expect
    red = {**OK, "level": "problem", "line": "Mail sign-in needs you", "conditions": [
        {"id": "sentinel:mail_sign_in", "level": "problem", "title": "Mail sign-in needs you",
         "line": "Tacitus is signed out of Outlook.", "todo": "", "since": time.time(), "read": False}]}
    page, stub, errors = home_app(app, "red", status=red)
    expect(page.locator(".h-greet")).to_have_text("Tacitus needs a look. 6 things wait for you.")
    page.click(".h-alert .btn")
    expect(page.locator("#status")).to_be_visible()
    assert not errors, errors


def test_the_posts_own_list_and_its_not_checked_line_show_on_home(app):
    """Where the remote run kept its post's list, Home lists that: a
    question as Answer needed, and the post's own line when its check did
    not run; an item the review left out is not there."""
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "unchecked")
    expect(page.locator(".h-unchecked")).to_have_text(
        "Not checked before sending: Codex did not answer in time. Tacitus's own checks ran.")
    q = page.locator(".h-item", has_text="Join the committee this term? Yes or no.")
    expect(q.locator(".h-label")).to_have_text("Answer needed")
    expect(page.locator(".h-main")).not_to_contain_text("Send the slides to the committee")
    assert not errors, errors


def test_kept_mail_shows_its_fixed_words_its_outlook_link_and_no_way_to_ask(app):
    """An item kept from hosted models is keep_gate's fixed line: at local
    its date, at facts its sender and subject; opened, the post's link
    opens Outlook and nothing starts a conversation about it."""
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "kept")
    linked = page.locator(".h-block[data-ref='item:row-k1']")  # the post's own line, with its link
    linked.locator(".h-item").click()
    outlook = linked.get_by_role("link", name="Open in Outlook")
    expect(outlook).to_have_attribute("href", "https://outlook.example.com/mail/k1")
    expect(outlook).to_have_attribute("rel", "noopener noreferrer")
    expect(linked.locator(".h-choices .btn")).to_have_text(["Open in Outlook"])  # no words of its own
    assert linked.locator(".h-brief *, .h-links *").count() == 0
    expect(page.locator(".h-item", has_text="Seminar date")).to_have_count(0)  # a local reply names no subject
    expect(page.locator(".h-item", has_text="A kept message from Alex Kim on")).to_contain_text("Data access form")
    for block in page.locator(".h-block:has-text('kept')").all():  # So far today's rows too
        block.locator("> button").click()
        expect(block.locator(".h-body")).to_be_visible()
        assert block.get_by_role("button", name="Ask", exact=True).count() == 0
    expect(page.locator(".h-main")).not_to_contain_text("Seminar date")
    expect(page.locator(".h-main")).not_to_contain_text("Pat Example")
    assert not errors, errors


def test_vacation_folds_what_waits_under_peek_anyway(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "vacation")
    expect(page.locator(".h-greet")).to_contain_text("Nothing is on fire.")
    expect(page.locator(".h-mode")).to_contain_text("You are off until")
    expect(page.locator(".h-next")).to_have_count(0)  # no gap to fill
    peek = page.locator("details.h-peek")
    expect(peek.locator("summary")).to_have_text("Peek anyway")
    expect(peek.locator(".h-item").first).to_be_hidden()
    peek.locator("summary").click()
    expect(peek.locator(".h-item")).to_have_count(6)
    page.evaluate("document.querySelector('details.h-peek').dataset.old = '1'")
    stub.set_home(home("vacation"))  # a redraw keeps the part the owner opened
    expect(page.locator("details.h-peek[data-old]")).to_have_count(0)
    expect(peek).to_have_attribute("open", "")
    assert not errors, errors


def test_travel_shows_what_cannot_wait_and_holds_the_rest(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, "travel")
    expect(page.locator(".h-mode")).to_contain_text("Only what cannot wait shows here.")
    expect(page.locator(".h-main > section .h-items > li")).to_have_count(2)
    expect(page.locator("details.h-peek summary")).to_have_text("Show the rest (4)")
    assert not errors, errors


def test_the_briefing_waits_at_the_bottom_and_a_link_opens_it(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    rep = page.locator("#home-morning-briefing")
    expect(rep.locator("summary")).to_have_text("This morning's briefing")
    expect(rep).not_to_have_attribute("open", "")
    page.locator(".h-since a", has_text="new requests").wait_for()
    page.evaluate("goTo('#/home/morning-briefing')")
    expect(rep).to_have_attribute("open", "")
    expect(rep.locator(".text")).to_contain_text("Get done today")
    rep.locator(".btn", has_text="Ask").click()
    expect(page.locator("#text")).to_have_value("About this morning's briefing: ")
    assert not errors, errors


def test_since_and_done_link_to_their_places_and_back_returns_home(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app, viewport={"width": 390, "height": 844},
                                  context={"has_touch": True, "is_mobile": True, **CLOCK})
    p = home()
    import webui_stub_server as W
    href = page.evaluate("([c, t]) => hashFor(c, t)", [W.MAIN, W.WEEKLY])  # the stub's weekly plan
    p["since"]["lines"][-1]["href"] = href
    stub.set_home(p)
    page.locator(f".h-since a[href='{href}']").click()
    expect(page.locator("#messages .msg").first).to_be_visible()
    page.click("#back")
    expect(page.locator(".h-greet")).to_be_visible()
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert not errors, errors


def test_the_question_of_the_day_opens_a_conversation_for_the_answer(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    q = page.locator(".h-question")
    expect(q).to_contain_text("What counts as urgent for you?")
    expect(q).to_contain_text("(9 left)")
    q.locator(".btn").click()
    expect(page.locator("#text")).to_have_value('My answer to today\'s question ("What counts as urgent for you?"): ')
    assert not errors, errors


def test_the_brand_and_the_home_row_lead_home(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    drawer_drawn(page)
    page.click("#channels .chan:has-text('Ask Tacitus')")
    expect(page.locator("#channel-name")).to_have_text("Ask Tacitus")
    page.click("#channels .brand")
    expect(page.locator("#channel-name")).to_have_text("Home")
    assert not errors, errors


def test_settings_home_saves_the_tone_and_the_briefings_place(app):
    from playwright.sync_api import expect
    page, stub, errors = home_app(app)
    page.click("#open-settings")
    page.click("#go-home")
    expect(page.locator("#settings-title")).to_have_text("Home")
    expect(page.locator("#home-rows input[value='breezy']")).to_be_checked()
    page.click("#home-rows label:has-text('Facts only')")
    wait_for(lambda: stub.home_posts)
    assert stub.home_posts == [{"tone": "facts"}]
    expect(page.locator("#home-note")).to_have_text("Saved.")
    page.click("#home-rows label:has-text('As a message')")
    wait_for(lambda: len(stub.home_posts) == 2)
    assert stub.home_posts[1] == {"briefing": "post"} and stub.home_settings == {"tone": "facts", "briefing": "post"}
    assert not errors, errors


def test_a_phone_at_382_and_390_scrolls_up_and_down_only_and_every_control_takes_a_finger(app):
    from playwright.sync_api import expect
    for width in (382, 390):
        for state in ("normal", "vacation"):
            page, stub, errors = home_app(app, state, viewport={"width": width, "height": 800},
                                          context={"has_touch": True, "is_mobile": True, **CLOCK})
            expect(page.locator(".h-greet")).to_be_visible()
            page.evaluate("document.querySelectorAll('details.h-card').forEach((d) => { d.open = true; })")
            assert page.evaluate(SIDEWAYS)[0] == 0
            assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
            page.evaluate("$('thread-list').style.height = 'auto'; $('thread-list').style.overflow = 'visible'")
            small = page.evaluate(SMALL_TARGETS.replace("a.thread-item", "a.thread-item, .h-page a, .h-item"))
            assert small == [], (width, state, small)
            assert not errors, errors
