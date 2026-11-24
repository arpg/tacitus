"""The web app in a browser: a day the owner picks (docs/webchat.md, A day to
pick). Wherever Tacitus asks for a date (a card's action in Action
requests, a Home choice, Later, a window) the app draws one control: a
native date field within the bot's range that starts at its proposed day,
and one button that says the day in words before it goes. There is no line
to type a day in, so no day is ever asked twice. The proposed day is one
tap on the bot's own button; the bot's refusal shows in its own words in
the card, which keeps its place; a redraw keeps the control open with its
day; a label with no name is never drawn.

The browser fixtures and the helpers more than one file uses are in
tests/webui_browser.py. Made-up data only."""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import home_fixtures as F  # noqa: E402
import webui_stub_server as W  # noqa: E402
from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    SIDEWAYS, SIZES, SMALL_TARGETS, _request, app, browser, settled, stub_factory, wait_for,
)

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
TODAY = datetime.now(ZoneInfo(F.ZONE_NAME)).date()
FIRST, PROPOSED, LAST = TODAY + timedelta(days=1), TODAY + timedelta(days=8), TODAY + timedelta(days=365)
CASE = {"case": "lc-made-up-7"}


def words(d) -> str:
    """A day as the app says it: "Tue Dec 1", with its year when that is not this year."""
    return f"{d:%a} {d:%b} {d.day}" + (f", {d.year}" if d.year != TODAY.year else "")


def decision(stub, proposed=PROPOSED):
    """A new letter case's first card, as the bot posts it with a day: Yes by
    the proposed day (one tap, the day in its value), Yes by another date
    (the day control), No. With no day in the records, Yes asks for one."""
    rng = {"min": FIRST.isoformat(), "max": LAST.isoformat()}
    yes = [{"id": "letter_yes", "label": f":white_check_mark: Yes, by {words(proposed)}", "style": "primary", "word": "yes",
            "value": dict(CASE, due=proposed.isoformat()), "ask": "", "date": {}}] if proposed else []
    pick = {"id": "letter_yes_date", "label": ":calendar: Yes, by another date" if proposed else ":calendar: Yes, by a date",
            "style": "" if proposed else "primary", "word": "date", "value": CASE, "ask": "The date",
            "date": dict(rng, **({"proposed": proposed.isoformat()} if proposed else {}))}
    return card(stub, ":memo: *A letter request.* Will you write it?", kind="letter_ask", no_default=True,
                fields=[{"label": "From", "value": "Rowan Vale"}, {"label": "Subject", "value": "Letter for Rowan Vale"}],
                lines=["Letter for Rowan Vale.", "1. Harbor Tech PhD: due " + words(PROPOSED) + "."],
                actions=[*yes, pick, {"id": "letter_no", "label": ":no_entry_sign: No", "style": "", "word": "no",
                                      "value": CASE, "ask": "", "date": {}}])


def card(stub, title, **kw):
    """A card in Action requests with its title as the bot writes it (no lock added)."""
    return stub.add(W.DM, "", kind="card", event="card", card={
        "kind": "send", "title": title, "fields": [], "lines": [], "body": "", "quoted": "", "actions": [],
        "select": None, "block_id": "", "footer": "", "fallback_text": title, "thread_ts": "", "ea_channel": W.MAIN,
        "content_fingerprint": "", "no_default": False, **kw})


def started(app, **kw):
    """(page, stub, errors) for the app once it has drawn its first list."""
    page, stub, errors = app(**kw)
    page.wait_for_selector(".thread-item")
    return page, stub, errors


def opened(page, m):
    """Action requests with the request `m` open; its block and its card."""
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    block.locator(".req-head").click()
    return block, block.locator(".card")


@SIZES
def test_the_proposed_day_is_one_tap_and_another_day_comes_from_the_date_field(app, size):
    from playwright.sync_api import expect
    page, stub, errors = started(app, **(PHONE if size == "phone" else {"context": CLOCK}))
    m = decision(stub)
    block, card = opened(page, m)
    expect(card.locator(".card-actions:not(.day-pick) button")).to_have_text(
        [re.compile(f"Yes, by {words(PROPOSED)}$"), re.compile("Yes, by another date$"), re.compile("No$")])
    other = card.get_by_role("button", name=re.compile("Yes, by another date$"))
    field, use = card.get_by_label("The date"), card.locator(".day-pick .btn.primary")
    expect(other).to_have_attribute("aria-expanded", "false")
    expect(field).to_be_hidden()
    expect(card.locator("input:not([type='date'])")).to_have_count(0)  # no line to type a day in
    other.click()
    expect(other).to_have_attribute("aria-expanded", "true")
    expect(card.get_by_role("group", name="Yes, by another date")).to_be_visible()
    expect(field).to_be_focused()
    expect(field).to_have_attribute("type", "date")
    expect(field).to_have_value(PROPOSED.isoformat())  # it starts at the proposed day
    expect(field).to_have_attribute("min", FIRST.isoformat())
    expect(field).to_have_attribute("max", LAST.isoformat())
    expect(use).to_have_text(f"Use {words(PROPOSED)}")
    later = PROPOSED + timedelta(days=6)
    field.fill(later.isoformat())
    expect(use).to_have_text(f"Use {words(later)}")  # the day in words before it goes
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        page.set_viewport_size({"width": 382, "height": 760})  # the narrowest phone in use, folded
        settled(page)
        assert page.evaluate(SIDEWAYS) == [0, 0]
        assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
        box, edge = use.bounding_box(), card.bounding_box()
        assert box["x"] + box["width"] <= edge["x"] + edge["width"], (box, edge)
    use.click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks == [{"channel": m["channel"], "id": m["id"], "action_id": "letter_yes_date", "value": CASE,
                            "text": later.isoformat()}]  # the ISO day as the action's line, and nothing else
    # The proposed day: one tap on the bot's own button, no field, no line.
    again = decision(stub)
    _block, card = opened(page, again)
    card.get_by_role("button", name=re.compile(f"Yes, by {words(PROPOSED)}$")).click()
    wait_for(lambda: len(stub.clicks) == 2)
    assert stub.clicks[1] == {"channel": again["channel"], "id": again["id"], "action_id": "letter_yes",
                              "value": dict(CASE, due=PROPOSED.isoformat())}
    assert stub.refused_clicks == []
    assert not errors, errors


def test_a_day_out_of_range_never_goes_and_the_bots_refusal_shows_in_its_own_words(app):
    from playwright.sync_api import expect
    page, stub, errors = started(app, context=CLOCK)
    m = decision(stub, proposed=None)  # nothing in the records: Yes asks for the day
    block, card = opened(page, m)
    yes = card.get_by_role("button", name=re.compile("Yes, by a date$"))
    yes.focus()
    page.keyboard.press("Enter")  # the keyboard opens it too
    field, use, hint = card.get_by_label("The date"), card.locator(".day-pick .btn.primary"), card.locator(".day-hint")
    expect(field).to_be_focused()
    expect(field).to_have_value("")
    expect(use).to_have_text("Use this date")
    expect(use).to_be_disabled()
    expect(hint).to_have_text("Pick a day.")
    for out in (LAST + timedelta(days=1), FIRST - timedelta(days=1)):
        field.fill(out.isoformat())
        expect(use).to_be_disabled()
        expect(use).to_have_text("Use this date")
        expect(hint).to_have_text(f"Pick a day from {words(FIRST)} to {words(LAST)}.")
        field.press("Enter")
    settled(page)
    assert stub.clicks == [] and stub.refused_clicks == []  # nothing went
    # A day in range the bot refuses (the place closed meanwhile): its words in the card, which stays open where it was.
    stub.click_refusals["letter_yes_date"] = "That place is already closed."
    field.fill(FIRST.isoformat())
    expect(hint).to_have_text("")
    field.press("Enter")
    expect(card.locator(".card-note")).to_have_text("That place is already closed.")
    assert [r[0] for r in stub.refused_clicks] == ["letter_yes_date"] and stub.clicks == []
    expect(block).to_have_class(re.compile(r"\bopen\b"))
    expect(field).to_have_value(FIRST.isoformat())  # the day is kept
    expect(use).to_be_enabled()
    expect(use).to_have_text(f"Use {words(FIRST)}")
    expect(page.locator("#thread-list .reqs:not(.done) > .req")).to_have_count(1)  # no new request
    # The bot's own check holds a day the page did not check (the stub's, as the bot's web_action does).
    said = page.evaluate("""([ch, id]) => call('POST', `/cards/${enc(ch)}/${enc(id)}/actions`,
        { action_id: 'letter_yes_date', text: 'next Friday' }).catch((e) => e.message)""", [m["channel"], m["id"]])
    last = f"{LAST:%a %b} {LAST.day}" + (f", {LAST.year}" if LAST.year != FIRST.year else "")
    assert said == f"Pick a day from {FIRST:%a %b} {FIRST.day} to {last}."
    assert [e for e in errors if "status of 400" not in e] == [], errors  # the browser's own line for each refusal


def test_a_redraw_keeps_the_open_day_control_its_day_and_a_failed_press_keeps_the_card_in_place(app):
    """Action requests is drawn again on a card's arrival: the open block
    stays as it is, its day control open with the day picked and the focus in
    it. The bot changing the card in place (a press that did not work, its
    words in `failed`) draws it again: still open in its place, the control
    open with its day, the bot's words above the buttons."""
    from playwright.sync_api import expect
    page, stub, errors = started(app, context=CLOCK)
    m = decision(stub)
    block, card = opened(page, m)
    card.get_by_role("button", name=re.compile("Yes, by another date$")).click()
    field = card.get_by_label("The date")
    later = PROPOSED + timedelta(days=3)
    field.fill(later.isoformat())
    field.focus()
    page.evaluate("document.querySelector('.place-note').dataset.old = '1'")
    other = _request(stub, "Send the agenda?")  # a card comes: the list is read and drawn again
    expect(page.locator(f'.req[data-id="{other["id"]}"]')).to_have_count(1)
    expect(page.locator(".place-note[data-old]")).to_have_count(0)
    expect(field).to_be_focused()
    expect(field).to_have_value(later.isoformat())
    stub.change(m, "update", card=dict(m["card"], failed="I could not set that date. Try again."))
    expect(card.locator(".answer-failed")).to_have_text("I could not set that date. Try again.")
    expect(card.locator(".answer-failed")).to_have_attribute("role", "alert")
    expect(page.locator(".req.open")).to_have_attribute("data-id", m["id"])  # the same request, still open
    expect(field).to_be_visible()
    expect(field).to_have_value(later.isoformat())
    expect(card.locator(".day-pick .btn.primary")).to_have_text(f"Use {words(later)}")
    expect(page.locator(f'.req[data-id="{m["id"]}"] .req-flag')).to_have_count(0)  # no new look
    card.locator(".day-pick .btn.primary").click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["text"] == later.isoformat()
    assert not errors, errors


def test_the_lock_marks_only_a_card_whose_press_passes_the_confirmation_gate(app):
    """The lock is the mark of a button-gated action (a send, a meeting, a
    publish, a plugin's or the browser's write). A letter's decision, a
    Replies card or a close proposal sends nothing: each keeps its own
    leading emoji, and a lock in its title is dropped."""
    from playwright.sync_api import expect
    page, stub, errors = started(app, context=CLOCK)
    act = [{"id": "go", "label": "Go", "value": "v", "style": "primary", "word": "go", "ask": "", "date": {}}]
    gated = {k: card(stub, f":lock: *A {k} card*", kind=k, actions=act) for k in
             ("send", "bulk", "schedule", "event", "event_update", "event_delete", "plugin", "publish", "browser")}
    own = {"letter_ask": decision(stub), "replies": card(stub, ":inbox_tray: *Replies*", kind="replies", actions=act),
           "close": card(stub, ":lock: *Close these?*", kind="close", actions=act)}
    page.evaluate("location.hash = '#/stream%3A7'")
    for kind, m in {**gated, **own}.items():
        block = page.locator(f'.req[data-id="{m["id"]}"]')
        block.locator(".req-head").click()
        title = block.locator(".card-title")
        expect(title).to_be_visible()
        expect(title.locator(".emoji[aria-label='lock']")).to_have_count(1 if kind in gated else 0)
    expect(page.locator(f'.req[data-id="{own["letter_ask"]["id"]}"] .card-title .emoji').first).to_have_attribute(
        "aria-label", "memo")  # its own icon
    expect(page.locator(f'.req[data-id="{own["replies"]["id"]}"] .card-title .emoji').first).to_have_attribute(
        "aria-label", "inbox_tray")
    assert not errors, errors


def test_an_action_an_option_or_a_choice_with_no_name_is_never_drawn(app):
    """A part with no name (its label empty, or "Submitted: " with the name
    missing) gets no button, no menu entry and no Home choice: two such
    buttons could not be told apart."""
    from playwright.sync_api import expect
    page, stub, errors = app(context=CLOCK, home_src=lambda tz="": F.sources("normal", dm=W.DM, main=W.MAIN))
    page.wait_for_selector(".h-main .h-block")
    acts = [{"id": "letter_submitted", "label": f":white_check_mark: Submitted: {n}", "value": dict(CASE, dest=d),
             "style": "", "word": "", "ask": "", "date": {}} for n, d in (("Harbor Tech PhD", "p1"), ("", "p2"), (" ", "p3"))]
    acts += [{"id": "letter_blank", "label": "", "value": CASE, "style": "", "word": "blank", "ask": "", "date": {}},
             {"id": "letter_decline", "label": "Decline", "value": CASE, "style": "", "word": "", "ask": "", "date": {}}]
    m = _request(stub, "Letter for Rowan Vale", kind="letter", actions=acts,
                 select={"id": "letter_submitted", "placeholder": "Submitted another place",
                         "options": [["Pinecrest College MS", "p4"], ["", "p5"], [":white_check_mark: ", "p6"]]})
    _block, card = opened(page, m)
    expect(card.locator(".card-actions button")).to_have_text([re.compile("Submitted: Harbor Tech PhD$"), "Decline"])
    expect(card.locator("select option")).to_have_text(["Submitted another place", "Pinecrest College MS"])
    ref = "item:deadline-w1"
    item = stub.real.item(ref)
    stub.items[ref] = dict(item, choices=[{"id": "start", "label": "Start it with Tacitus", "style": "primary"},
                                          {"id": "gone", "label": "Submitted: "}, {"id": "blank", "label": ""}])
    page.evaluate("location.hash = '#/home'")
    block = page.locator(f".h-block[data-ref='{ref}']")
    block.locator("> button").click()
    expect(block.locator(".h-choices .btn")).to_have_text(["Start it with Tacitus"])
    assert not errors, errors


def test_a_home_choice_and_later_ask_for_their_day_with_the_same_control(app):
    """A Home choice with `input: "date"` opens the day control at its
    proposed day within its range and sends the ISO day; its proposed day is
    one tap on its own choice. Later's Pick a date is the same control."""
    from playwright.sync_api import expect
    page, stub, errors = app(context=CLOCK, home_src=lambda tz="": F.sources("normal", dm=W.DM, main=W.MAIN))
    ref = "item:deadline-w1"
    item = stub.real.item(ref)
    later = next(c for c in item["choices"] if c["id"] == "later")
    stub.items[ref] = dict(item, choices=[
        {"id": "case_yes", "label": f"Yes, by {words(PROPOSED)}", "style": "primary"},
        {"id": "case_date", "label": "Yes, by another date", "input": "date", "min": FIRST.isoformat(),
         "max": LAST.isoformat(), "proposed": PROPOSED.isoformat()}, later])
    stub.act_answers["case_date"] = {"ok": True, "said": "Due date set."}
    page.wait_for_selector(".h-main .h-block")
    block = page.locator(f".h-block[data-ref='{ref}']")
    block.locator("> button").click()
    pick = block.get_by_role("button", name="Yes, by another date")
    pick.click()
    expect(pick).to_have_attribute("aria-expanded", "true")
    field, use = block.get_by_label("The date"), block.locator(".day-pick .btn.primary")
    expect(field).to_be_focused()
    expect(field).to_have_value(PROPOSED.isoformat())
    expect(field).to_have_attribute("min", FIRST.isoformat())
    expect(field).to_have_attribute("max", LAST.isoformat())
    expect(use).to_have_text(f"Use {words(PROPOSED)}")
    expect(block.locator(".h-in input:not([type='date'])")).to_have_count(0)
    field.fill((LAST + timedelta(days=1)).isoformat())
    expect(use).to_be_disabled()
    day = PROPOSED + timedelta(days=2)
    field.fill(day.isoformat())
    use.click()
    wait_for(lambda: stub.acts)
    assert {k: stub.acts[0][k] for k in ("ref", "choice", "arg")} == {"ref": ref, "choice": "case_date", "arg": day.isoformat()}
    expect(block.locator(".h-note")).to_have_text("Due date set.")
    # Later's Pick a date: the same control, within Later's own range.
    block.get_by_role("button", name="Later").click()
    block.get_by_role("button", name="Pick a date").click()
    field = block.get_by_label("The date")
    expect(field).to_be_focused()
    expect(field).to_have_attribute("min", later["options"][-1]["min"])
    expect(block.locator(".day-pick .btn.primary")).to_be_disabled()
    assert not errors, errors


def test_a_window_asks_for_its_day_with_the_same_field(app):
    """A card button that opens a window with a day (Slack's datepicker
    element) shows the same date field with the day in words under it;
    Save sends the ISO day."""
    from playwright.sync_api import expect
    page, stub, errors = started(app, context=CLOCK)
    stub.modal_views["letter_change_date"] = {
        "type": "modal", "title": {"type": "plain_text", "text": "Change the date"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [{"type": "input", "block_id": "due", "label": {"type": "plain_text", "text": "Due date"},
                    "element": {"type": "datepicker", "action_id": "day", "initial_date": PROPOSED.isoformat()}}]}
    m = _request(stub, "Letter for Rowan Vale", kind="letter", actions=[
        {"id": "letter_change_date", "label": "Change the date", "value": CASE, "style": "", "word": "", "ask": "", "date": {}}])
    _block, card = opened(page, m)
    card.get_by_role("button", name="Change the date").click()
    dlg = page.locator("dialog#modal")
    field = dlg.get_by_label("Due date")
    expect(field).to_have_attribute("type", "date")
    expect(field).to_have_value(PROPOSED.isoformat())
    expect(dlg.locator(".day-hint")).to_have_text(words(PROPOSED))
    day = PROPOSED + timedelta(days=1)
    field.fill(day.isoformat())
    expect(dlg.locator(".day-hint")).to_have_text(words(day))
    dlg.get_by_role("button", name="Save").click()
    wait_for(lambda: stub.modal_saves)
    assert stub.modal_saves == [{"trigger": "web:letter_change_date", "values": {"due": {"day": day.isoformat()}}}]
    assert not errors, errors
