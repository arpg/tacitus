"""The web app in a browser: Add a device and Settings.

Pair a phone offers the relay on this machine's own address, Approve sends
the card-button choice, the Phones list stacks at phone width; the
Settings screen renders the routing table and saves it, the Models page
opens on one pick, the Settings menu fits a phone and shows only what the
bot serves, and Notifications save each change at once.

The browser fixtures (one Chromium per file, a fresh context and stub per
test) and the helpers more than one file uses are in tests/webui_browser.py;
tests/test_webui.py holds the checks that need no browser.
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui_browser import (  # noqa: E402,F401  (the fixtures and helpers)
    SIDEWAYS, SUBS, app, browser, open_devices, stub_factory, wait_for,
)
from plain_words import JARGON  # noqa: E402


def test_pair_a_phone_offers_the_relay_on_this_machines_own_address(app):
    """On a loopback origin (no public_url: no tailnet) "Add a device" makes
    no link a phone could not open. It offers "Phone not on your network",
    which asks for a link to the relay's copy of the app with the bot's
    bundle hash and says what the phone may do once approved."""
    from playwright.sync_api import expect
    pytest.importorskip("cryptography")
    page, stub, errors = app(relay_pairing=True, sign=True)
    page.wait_for_selector(".thread-item")
    open_devices(page)
    expect(page.locator("#phone-note")).to_contain_text('This address does not open on a phone. Use "Phone not on your network" instead.')
    button = page.locator("#phone-relay")
    expect(button).to_be_visible()
    expect(button).to_have_text("Phone not on your network")
    assert stub.pairs == [] and not page.input_value("#phone-link")
    button.click()
    wait_for(lambda: page.input_value("#phone-link"), 5)
    assert stub.pairs == [{"via": "relay"}]
    link = page.input_value("#phone-link")
    assert link.startswith("https://relay.example.net/#tr2=")
    import base64
    assert json.loads(base64.urlsafe_b64decode(link.split("#tr2=")[1] + "=="))["b"] == stub.bundle.hash
    expect(page.locator("#phone-note")).to_contain_text(
        "once you tap Approve below it can read and reply, and press buttons only if you allow it.")
    expect(page.locator("#phone-note")).to_contain_text("Never send this link through chat or mail.")
    expect(button).to_be_hidden()
    assert page.evaluate("$('phone-qr').width") > 0
    assert not errors, errors


def test_approve_sends_the_card_button_choice(app):
    """Beside a relay-origin phone that waits, "Allow card buttons on this
    phone" is off; Approve sends `cards` false, or true once ticked. An
    own-origin phone has no such box."""
    from playwright.sync_api import expect
    page, stub, errors = app(relay_pairing=True)
    stub.devices[:] = [
        {"id": "1" * 16, "name": "phone", "paired_at": 1793400000, "origin": "relay", "pending": True,
         "code": "123456", "cards_allowed": False, "bundle_verified": False},
        {"id": "2" * 16, "name": "tablet", "paired_at": 1793400000, "origin": "relay", "pending": True,
         "code": "654321", "cards_allowed": False, "bundle_verified": False},
        {"id": "3" * 16, "name": "laptop", "paired_at": 1793400000, "origin": "own", "pending": False}]
    page.wait_for_selector(".thread-item")
    open_devices(page)
    phone, tablet = (page.locator(f"#phone-devices li[data-device='{n * 16}']") for n in "12")
    expect(phone).to_contain_text("code 123 456, waits for approval")
    expect(phone.locator("input[type=checkbox]")).not_to_be_checked()
    expect(page.locator("#phone-devices li[data-device='3333333333333333'] input")).to_have_count(0)
    phone.locator("button", has_text="Approve").click()
    tablet.locator("input[type=checkbox]").check()
    tablet.locator("button", has_text="Approve").click()
    wait_for(lambda: len(stub.approvals) == 2, 5)
    assert stub.approvals == [{"device": "1" * 16, "cards": False}, {"device": "2" * 16, "cards": True}]
    expect(tablet).to_contain_text("not on your network, app not checked")
    expect(tablet.locator("input[type=checkbox]")).to_be_checked()
    assert not errors, errors


def test_each_phone_is_stacked_at_phone_width(app):
    """At 390px a phone that waits shows its name, then the card-button box,
    then Approve and Remove filling the last line, each 44px high; on a
    wide screen the same parts sit in one row."""
    from playwright.sync_api import expect
    device = {"id": "1" * 16, "name": "phone", "paired_at": 1793400000, "origin": "relay",
              "pending": True, "code": "123456", "cards_allowed": False, "bundle_verified": False}
    boxes = ("[...$('phone-devices').querySelector('li').children].map((c) => {"
             " const r = c.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })")
    for width, stacked in ((390, True), (1280, False)):
        page, stub, errors = app(relay_pairing=True, viewport={"width": width, "height": 844})
        stub.devices[:] = [dict(device)]
        page.wait_for_selector(".thread-item")
        if width < 900:
            page.click("#menu")
        open_devices(page)
        expect(page.locator("#phone-devices li[data-device]")).to_contain_text("waits for approval")
        (nl, nt, nw, _), (bl, bt, bw, _), approve, remove = page.evaluate(boxes)
        if stacked:
            assert nt < bt < approve[1] == remove[1] and nl == bl == approve[0]
            assert nw == bw == approve[2] + 8 + remove[2] and approve[2] == remove[2]
            assert approve[3] >= 44 and remove[3] >= 44
        else:
            assert nl < bl < approve[0] < remove[0]
        assert not errors, errors


def test_the_settings_screen_renders_the_rows_and_saves_the_table(app):
    """Under Advanced the screen draws a row per task from /settings/routing:
    the value checked, a value a floor refuses disabled with the floor's
    line under the row, the model it runs on now, and the local model's
    state. A task with a Codex route picks its hosted model, Claude or
    Codex; another says what Hosted means there. Save posts the enabled
    choices and redraws from the answer; a refusal stays on screen. The
    pick at the top saves the switch alone, at once, and the rows that
    follow it move with it."""
    from playwright.sync_api import expect
    page, stub, errors = app(settings=True)
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    page.click("#go-routing")
    expect(page.locator("#settings-title")).to_have_text("Models")
    # the one pick heads the page: the hosted model of every task not set apart
    switch = page.locator("#settings-default")
    expect(switch.locator(".s-name")).to_have_text("Which AI does the work")
    expect(switch.locator("label")).to_have_text(["Claude", "Codex"])
    expect(switch.locator("input[value=codex]")).to_be_checked()
    expect(switch.locator("p").first).to_have_text("Tacitus does its work with the AI you pick.")
    # these made-up rows run on the owner's own model and set one task apart: the line says so
    expect(switch.locator(".s-custom")).to_have_text(
        "Custom settings are on. Some tasks run on your own model or have their own pick. "
        "A pick here changes only the tasks that follow it.")
    advanced = page.locator("#settings-advanced")
    expect(page.locator("#settings-rows")).to_be_hidden()
    switch.locator("button", has_text="Open Advanced").click()   # it opens the rest in place
    expect(advanced).to_have_attribute("open", "")
    expect(advanced.locator("summary")).to_be_focused()
    expect(page.locator("#routing-settings .s-key").first).to_have_text(
        "Each task runs on your own model, on Claude or Codex, or on Auto, which lets Tacitus choose. "
        "A task that can use both Claude and Codex can be set apart from your pick at the top.")
    # the presets head Advanced: a button each, and Save current as...
    presets = page.locator("#settings-presets")
    expect(presets.locator(".s-name")).to_have_text("Presets")
    expect(presets.locator(".ways button")).to_have_text(["Private", "All Claude", "All Codex"])
    expect(presets.locator(".ways button[aria-pressed=true]")).to_have_count(0)
    rows = page.locator("#settings-rows li")
    expect(rows).to_have_count(4)
    expect(page.locator("#settings-local")).to_have_text("Local model local-27b: reachable. Answered 2 min ago.")
    drafting, mail, review, chat = (page.locator(f'#settings-rows li[data-task="{t}"]')
                                    for t in ("drafting", "mail", "review", "chat"))
    expect(drafting.locator(".s-name")).to_contain_text("Writing replies")
    expect(drafting.locator("input[value=auto]")).to_be_checked()
    expect(drafting.locator(".s-now")).to_have_text("Now on your own model. Auto picked your own model.")
    expect(drafting.locator(".s-note")).to_have_text("On Claude or Codex: the model that asks for the reply writes it.")
    # each choice reads your own model, the task's hosted model by name, or Auto
    expect(drafting.locator(".seg").first.locator("label")).to_have_text(["Your own model", "Claude", "Auto"])
    expect(drafting.locator(".s-hosted")).to_have_count(0)
    expect(drafting.locator(".s-floor")).to_have_count(0)
    expect(mail.locator("input[value=hosted]")).to_be_disabled()
    expect(mail.locator("input[value=local]")).to_be_enabled()
    expect(mail.locator(".s-floor")).to_have_text("Claude and Codex are off here while you keep some mail "
                                                  "away from hosted models (a domain, an address or "
                                                  "admissions mail), so that mail never reaches them.")
    expect(review.locator("input[value=hosted]")).to_be_checked()
    for v in ("local", "auto"):
        expect(review.locator(f"input[value={v}]")).to_be_disabled()
    expect(review.locator(".s-floor")).to_have_text("Always Claude or Codex: these are judgment calls.")
    expect(review.locator(".s-now")).to_have_text(
        "Now on Claude. These are judgment calls, so your own model never makes them.")
    expect(chat.locator(".s-hosted .s-sub")).to_have_text("Hosted model")
    expect(chat.locator("input[name=hosted-chat][value=codex]")).to_be_checked()
    expect(chat.locator(".s-hosted label")).to_have_text(["Claude", "Codex"])
    expect(chat.locator(".s-now")).to_have_text("Now on Codex. Auto picked Codex.")
    expect(chat.locator(".s-note")).to_contain_text("mail mediation is off")
    # the chat floor: your own model is not offered for talking with you yet, and the row says why
    expect(chat.locator(".seg").first.locator("label")).to_have_text(["Your own model", "Codex", "Auto"])
    expect(chat.locator("input[name=route-chat][value=local]")).to_be_disabled()
    expect(chat.locator(".s-floor")).to_have_text(
        "Your own model does not hold up in conversation yet. Talking with you and finding meeting times "
        "stay on Claude or Codex for now.")
    # a task that can use both says whether it follows the pick at the top or is set apart
    expect(chat.locator(".s-own")).to_have_text("Follows your pick at the top.")
    expect(review.locator(".s-own")).to_have_text("Set for this task. Use default")
    expect(review.locator("input[name=hosted-review][value=claude]")).to_be_checked()
    expect(drafting.locator(".s-own")).to_have_count(0)
    # the "?" beside a name the bot gives help for: it opens and closes the note under the row
    expect(review.locator(".s-help-btn")).to_have_count(0)
    expect(chat.locator(".s-help-btn")).to_have_count(0)
    ask = drafting.locator(".s-help-btn")
    expect(ask).to_have_attribute("aria-label", 'What is "Writing replies"?')
    expect(ask).to_have_attribute("aria-expanded", "false")
    expect(drafting.locator(".s-help")).to_be_hidden()
    ask.click()
    expect(drafting.locator(".s-help")).to_have_text(
        "Writes the text of an email reply in your voice. Nothing goes out until you tap Send.")
    expect(ask).to_have_attribute("aria-expanded", "true")
    expect(mail.locator(".s-help")).to_be_hidden()                 # each row opens its own
    ask.click()
    expect(drafting.locator(".s-help")).to_be_hidden()
    drafting.locator("input[value=hosted]").check()
    chat.locator("input[name=hosted-chat][value=claude]").check()
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text("Saved. Each task uses its model from its next run.")
    assert stub.settings_posts == [{"tasks": {"drafting": "hosted", "mail": "auto", "review": "hosted",
                                              "chat": "auto"}, "hosted": {"chat": "claude"}}]
    expect(drafting.locator("input[value=hosted]")).to_be_checked()
    expect(drafting.locator(".s-now")).to_have_text("Now on Claude. Set to Claude.")
    expect(chat.locator("input[name=hosted-chat][value=claude]")).to_be_checked()
    expect(chat.locator(".s-now")).to_have_text("Now on Claude. Auto picked Claude.")
    expect(chat.locator(".s-note")).to_have_count(0)
    expect(chat.locator(".s-own")).to_have_text("Set for this task. Use default")
    # Use default: the task follows the pick at the top again; Save sends 'default' for it
    # and nothing for a row left alone
    chat.locator(".s-own button").click()
    expect(chat.locator("input[name=hosted-chat][value=codex]")).to_be_checked()
    expect(chat.locator(".s-own")).to_have_text("Follows your pick at the top.")
    page.evaluate("document.getElementById('settings-note').textContent = ''")  # wait for this save's own line
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text("Saved. Each task uses its model from its next run.")
    assert stub.settings_posts[1] == {"tasks": {"drafting": "hosted", "mail": "auto", "review": "hosted",
                                                "chat": "auto"}, "hosted": {"chat": "default"}}
    expect(chat.locator(".s-own")).to_have_text("Follows your pick at the top.")
    # the pick at the top saves the switch alone, at once: a row that follows it moves with
    # it, a row set apart stays, and Advanced stays open
    switch.locator("input[value=claude]").check()
    expect(page.locator("#simple-note")).to_have_text("Saved. The change starts with each task's next run.")
    assert stub.settings_posts[2] == {"hosted_default": "claude"}
    expect(switch.locator("input[value=claude]")).to_be_checked()
    expect(chat.locator("input[name=hosted-chat][value=claude]")).to_be_checked()
    expect(chat.locator(".s-own")).to_have_text("Follows your pick at the top.")
    expect(review.locator("input[name=hosted-review][value=claude]")).to_be_checked()
    expect(review.locator(".s-own")).to_have_text("Set for this task. Use default")
    expect(advanced).to_have_attribute("open", "")
    # a value the bot refuses: the reason stays on screen and nothing changes
    page.evaluate("document.querySelector('#settings-rows li[data-task=review] input[value=local]').disabled = false")
    review.locator("input[value=local]").check()
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text(
        'Not saved: "Checking its own work" always stays on Claude or Codex: these are judgment calls, '
        'so your own model never makes them.')
    assert stub.routing["rows"][2]["value"] == "hosted"
    # a preset: pressed, then sent alone on Save; the rows redraw from the answer
    presets.locator(".ways button", has_text="All Claude").click()
    expect(page.locator("#settings-note")).to_have_text("Save applies All Claude.")
    page.click("#settings-save")
    expect(page.locator("#settings-note")).to_have_text(
        "Saved. Each task uses its model from its next run. Reading your mail stays on Auto while you keep "
        "some mail away from hosted models.")
    assert stub.settings_posts[-1] == {"preset": "claude"}
    expect(presets.locator(".ways button[aria-pressed=true]")).to_have_text("All Claude")
    expect(chat.locator("input[name=hosted-chat][value=claude]")).to_be_checked()
    # Save current as...: a name typed in the browser's own box keeps the table in force
    page.once("dialog", lambda d: d.accept("Travel week"))
    presets.locator("button", has_text="Save current as...").click()
    expect(page.locator("#settings-note")).to_have_text("Saved as Travel week.")
    assert stub.settings_posts[-1] == {"preset": {"save": "Travel week"}}
    expect(presets.locator(".ways button")).to_have_text(["Private", "All Claude", "All Codex", "Travel week"])
    page.click("#settings button[type=submit]")
    expect(page.locator("#settings")).not_to_be_visible()
    assert errors == ["Failed to load resource: the server responded with a status of 400 (Bad Request)"]


# The visible controls of the Settings dialog under 44px high, and whether it runs off the screen's side.
SETTINGS_SMALL = """() => [...document.querySelectorAll('#settings .seg label, #settings button, #settings .toggle')]
    .filter((el) => el.getClientRects().length).map((el) => el.getBoundingClientRect().height).filter((h) => h < 44)"""


SHOWN_ITEMS = "#settings-menu .s-item:visible .s-label"


def plain_routing(model: str = "claude") -> dict:
    """The stub's routing table as a plain install has it: every row on
    the hosted model `model` where it can be, nothing on the owner's own
    model and nothing set apart."""
    import webui_stub_server as W
    r = json.loads(json.dumps(W.ROUTING))
    r["hosted_default"] = model
    for row in r["rows"]:
        hosted = model if len(row["hosted_allowed"]) > 1 else row["hosted"]
        row.update(hosted=hosted, effective="hosted", runs_on=W.STUB_NAMES[hosted],
                   hosted_source="default" if len(row["hosted_allowed"]) > 1 else "fixed")
    return r


def open_models(app, routing=None, ready=None, **kw):
    """(page, stub, errors) with Settings open on the Models page, the stub
    serving `routing` (default its own) with `ready` models set up."""
    page, stub, errors = app(settings=True, **kw)
    if routing is not None:
        stub.routing = routing
    stub.ready = dict(ready or {})
    page.wait_for_selector(".thread-item")
    if page.locator("#menu").is_visible():
        page.click("#menu")
    page.click("#open-settings")
    page.click("#go-routing")
    return page, stub, errors


def test_the_models_page_opens_on_one_pick_for_a_plain_install(app):
    """A plain install's Models page asks one thing, Claude or Codex, with
    no line about custom settings; Advanced is closed, so no task, preset or
    own-model choice shows. A tap saves the switch alone, at once. Advanced
    opens on every option there is, each task following the pick, and it is
    closed again the next time the page opens."""
    from playwright.sync_api import expect
    page, stub, errors = open_models(app, plain_routing(), viewport={"width": 390, "height": 844},
                                     context={"has_touch": True, "is_mobile": True})
    pick = page.locator("#settings-default")
    expect(pick.locator(".s-name")).to_have_text("Which AI does the work")
    expect(pick.locator("label")).to_have_text(["Claude", "Codex"])
    expect(pick.locator("input[value=claude]")).to_be_checked()
    expect(pick.locator(".s-custom")).to_have_count(0)
    expect(pick).to_have_text("Which AI does the workClaudeCodexTacitus does its work with the AI you pick.")
    expect(page.locator("#settings-advanced")).not_to_have_attribute("open", "")
    for hidden in ("#settings-presets", "#settings-local", "#settings-rows", "#settings-save"):
        expect(page.locator(hidden)).to_be_hidden()
    expect(page.locator("#settings-advanced > summary")).to_have_text("Advanced")
    shown = page.locator("#routing-settings").inner_text()
    assert "own model" not in shown and not JARGON.search(shown), shown
    assert page.evaluate(SETTINGS_SMALL) == []
    pick.locator("label", has_text="Codex").tap()
    expect(page.locator("#simple-note")).to_have_text("Saved. The change starts with each task's next run.")
    assert stub.settings_posts == [{"hosted_default": "codex"}]
    expect(pick.locator("input[value=codex]")).to_be_checked()
    expect(page.locator("#settings-rows")).to_be_hidden()
    # Advanced: the presets, the local model's state, a row per task with every value, Save
    page.locator("#settings-advanced > summary").tap()
    expect(page.locator("#settings-presets .ways button")).to_have_text(["Private", "All Claude", "All Codex"])
    expect(page.locator("#settings-local")).to_be_visible()
    rows = page.locator("#settings-rows li")
    expect(rows).to_have_count(4)
    for task in ("drafting", "mail", "review", "chat"):
        expect(rows.locator(f"input[name=route-{task}]")).to_have_count(3)
    chat = page.locator('#settings-rows li[data-task="chat"]')
    expect(chat.locator("input[name=hosted-chat][value=codex]")).to_be_checked()
    expect(chat.locator(".s-own")).to_have_text("Follows your pick at the top.")
    expect(page.locator("#settings-save")).to_be_visible()
    page.click("#settings-back")
    page.click("#go-routing")
    expect(page.locator("#settings-advanced")).not_to_have_attribute("open", "")
    expect(page.locator("#simple-note")).to_have_text("")
    assert not errors, errors


def test_a_custom_install_keeps_its_settings_and_says_so(app):
    """An install with tasks on the owner's own model and a task set apart
    says so in one line under the pick, with a button that opens Advanced.
    A pick then sends the switch alone: the tasks on the owner's own model
    and the task set apart stay where they are, and the line stays."""
    from playwright.sync_api import expect
    page, stub, errors = open_models(app)
    pick = page.locator("#settings-default")
    line = pick.locator(".s-custom")
    expect(line).to_contain_text("Custom settings are on. Some tasks run on your own model or have their own pick.")
    expect(page.locator("#settings-rows")).to_be_hidden()
    pick.locator("label", has_text="Claude").click()
    expect(page.locator("#simple-note")).to_have_text("Saved. The change starts with each task's next run.")
    assert stub.settings_posts == [{"hosted_default": "claude"}]   # no task value, no override
    expect(line).to_be_visible()
    pick.locator("button", has_text="Open Advanced").click()
    for task in ("drafting", "mail"):
        expect(page.locator(f'#settings-rows li[data-task="{task}"] .s-now')).to_have_text(
            "Now on your own model. Auto picked your own model.")
    review = page.locator('#settings-rows li[data-task="review"]')
    expect(review.locator(".s-own")).to_have_text("Set for this task. Use default")
    assert [r["effective"] for r in stub.routing["rows"]][:2] == ["local", "local"]
    # a pick the bot refuses: its reason under the pick, and the saved pick checked again
    page.evaluate("document.querySelector('#settings-default input[value=codex]').value = 'gpt'")
    pick.locator("label", has_text="Codex").click()
    expect(page.locator("#simple-note")).to_have_text("Not saved: The hosted model is Claude or Codex.")
    expect(pick.locator("input[value=claude]")).to_be_checked()
    assert stub.routing["hosted_default"] == "claude"
    assert errors == ["Failed to load resource: the server responded with a status of 400 (Bad Request)"]


def test_with_one_ai_set_up_the_page_names_it_instead_of_a_choice(app):
    """With only Claude set up the page says Claude does the work and offers
    no dead choice. When the switch names the model that is not set up, the
    line says so and one button moves the work to the one that is."""
    from playwright.sync_api import expect
    page, stub, errors = open_models(app, plain_routing("claude"), ready={"codex": False})
    pick = page.locator("#settings-default")
    expect(pick.locator("p").first).to_have_text("Claude does the work. Codex is not set up on this computer.")
    expect(pick.locator("input")).to_have_count(0)
    expect(pick.locator("button")).to_have_count(0)
    expect(pick).to_have_text("Which AI does the workClaude does the work. Codex is not set up on this computer.")
    page.click("#settings-back")
    stub.routing = plain_routing("codex")
    page.click("#go-routing")
    expect(pick.locator("p").first).to_have_text(
        "Codex is picked, but it is not set up on this computer. Use Claude until it is.")
    expect(pick.locator("input")).to_have_count(0)
    pick.locator("button", has_text="Use Claude").click()
    expect(page.locator("#simple-note")).to_have_text("Saved. The change starts with each task's next run.")
    assert stub.settings_posts == [{"hosted_default": "claude"}]
    expect(pick.locator("p").first).to_have_text("Claude does the work. Codex is not set up on this computer.")
    expect(pick.locator("button")).to_have_count(0)
    assert not errors, errors


def test_the_settings_menu_fits_a_phone_and_shows_only_what_the_bot_serves(app):
    """On a phone Settings is a sheet docked to the bottom: a row per page
    (Notifications and Models where the bot serves them, and the tour), how
    this browser reaches Tacitus under them, each control a finger's 44px. A
    row opens its page in place with Back focused; Back and Escape come back
    to the menu on the row they left, and Escape there closes it. With no
    page served the menu holds the tour alone."""
    from playwright.sync_api import expect
    page, _stub, errors = app(viewport={"width": 390, "height": 844}, settings=True, push=True, subscriptions=SUBS,
                              context={"has_touch": True, "is_mobile": True, "device_scale_factor": 3})
    page.wait_for_selector(".thread-item")
    page.click("#menu")
    page.click("#open-settings")
    dlg = page.locator("#settings")
    expect(page.locator(SHOWN_ITEMS)).to_have_text(["Notifications", "Models", "How Tacitus works"])
    expect(page.locator("#settings-title")).to_have_text("Settings")
    expect(page.locator("#settings-back")).to_be_hidden()
    expect(page.locator("#settings-conn")).to_have_text("Connected on your own network.")
    assert page.evaluate("Math.round($('settings').getBoundingClientRect().bottom) === innerHeight")  # docked
    assert page.evaluate(SETTINGS_SMALL) == []
    for row, title, shown in (("go-notify", "Notifications", "#notify-kinds"), ("go-routing", "Models", "#settings-default")):
        page.click(f"#{row}")
        expect(page.locator("#settings-title")).to_have_text(title)
        expect(page.locator("#settings-back")).to_be_focused()
        expect(page.locator(shown).first).to_be_visible()
        expect(page.locator("#settings-menu")).to_be_hidden()
        assert page.evaluate(SETTINGS_SMALL) == [], title
        assert page.evaluate("$('settings').scrollWidth - $('settings').clientWidth") == 0
        assert page.evaluate(SIDEWAYS) == [0, 0]
        if row == "go-routing":   # Advanced opens with a tap; a tap on a "?" opens its note, and the sheet still fits
            expect(page.locator("#settings-rows")).to_be_hidden()
            summary = page.locator("#settings-advanced > summary")
            assert summary.bounding_box()["height"] >= 44
            summary.tap()
            expect(page.locator("#settings-rows li").first).to_be_visible()
            assert page.evaluate(SETTINGS_SMALL) == []
            page.locator("#settings-rows .s-help-btn").first.tap()
            expect(page.locator("#settings-rows .s-help").first).to_be_visible()
            assert page.evaluate(SETTINGS_SMALL) == []
            assert page.evaluate("$('settings').scrollWidth - $('settings').clientWidth") == 0
        if row == "go-notify":
            page.click("#settings-back")
        else:
            page.keyboard.press("Escape")
        expect(page.locator("#settings-title")).to_have_text("Settings")
        expect(page.locator(f"#{row}")).to_be_focused()
        expect(dlg).to_be_visible()
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    assert not errors, errors
    page, _stub, errors = app()
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    expect(page.locator(SHOWN_ITEMS)).to_have_text(["How Tacitus works"])
    assert not errors, errors


def test_notifications_say_where_they_go_and_whether_they_are_on_here(app):
    """The Notifications page names each browser that gets them by its push
    service's maker, this one as "This browser" with no Remove (its switch
    is above), the others with Remove. The menu's row and the hint beside
    Settings in the drawer say whether they are on here; blocked in the
    browser, the page says where to change it and the switch stays off."""
    import hashlib
    from playwright.sync_api import expect
    endpoint = "https://push.example.com/send/made-up"
    mine = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    subs = [dict(SUBS[0], id=mine), dict(SUBS[0], id="s2", host="web.push.apple.com"),
            dict(SUBS[0], id="s3", host="fcm.googleapis.com"), dict(SUBS[0], id="s4", host="updates.push.services.mozilla.com"),
            dict(SUBS[0], id="s5", host="wns2-par02p.notify.windows.com")]
    allowed = "Object.defineProperty(Notification, 'permission', { get: () => 'granted' });"  # headless Chromium says denied
    page, stub, errors = app(push=True, settings=True, subscriptions=subs, init=allowed)
    page.wait_for_selector(".thread-item")
    hint = page.locator("#settings-hint")
    expect(hint).to_have_text("Notifications off")  # this browser could get them, and does not
    page.click("#open-settings")
    expect(page.locator("#notify-detail")).to_have_text("Off for this browser")
    page.click("#go-notify")
    expect(page.locator("#notify-here")).to_have_text("Notifications on this browser")
    expect(page.locator("#notify")).to_be_enabled()
    expect(page.locator("#notify-why")).to_be_hidden()
    expect(page.locator("#subs-title")).to_have_text("Where notifications go")
    rows = page.locator("#push-subs li")
    expect(rows.locator("span")).to_have_text([re.compile(f"^{re.escape(n)}, since \\S") for n in (
        "A browser", "An iPhone, iPad or Mac", "Chrome or Android", "Firefox", "Microsoft Edge")])
    expect(rows.locator("button")).to_have_count(5)
    # Subscribed here: its own row says so and has no Remove, and the hint goes.
    page.evaluate(f"S.push.sub = {{ endpoint: '{endpoint}' }}; notifyState(); listSubs();")
    expect(rows.first.locator("span")).to_have_text(re.compile(r"^This browser, since \S"))
    expect(rows.first.locator("button")).to_have_count(0)
    expect(hint).to_be_hidden()
    rows.nth(1).locator("button").click()
    expect(rows).to_have_count(4)
    assert stub.revokes == [{"id": "s2"}]
    page.click("#settings-back")
    expect(page.locator("#notify-detail")).to_have_text("On for this browser")
    for text in page.locator("#settings").all_inner_texts():
        assert not JARGON.search(text), text
    assert not errors, errors
    # Blocked: the reason, on the row and the page; the switch cannot turn them on.
    page, stub, errors = app(push=True, init="Object.defineProperty(Notification, 'permission', { get: () => 'denied' });")
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    expect(page.locator("#notify-detail")).to_have_text("Blocked in the browser settings")
    page.click("#go-notify")
    expect(page.locator("#notify-why")).to_have_text("Notifications are blocked for this site in the browser settings.")
    expect(page.locator("#notify-kinds")).to_be_hidden()  # the bot serves no notify page here
    assert not errors, errors


def test_notifications_in_settings_save_each_change_at_once(app):
    """With push on, Settings' Notifications page shows the switches and the
    quiet-hours choice as /settings/notify says. Each change posts its one
    key and the section redraws from the answer; a refusal stays on screen
    and the section shows what is saved. With no notify page the switches
    stay hidden; without push the menu has no Notifications."""
    from playwright.sync_api import expect
    page, stub, errors = app(settings=True, push=True)
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    page.click("#go-notify")
    expect(page.locator("#notify-settings")).to_be_visible()
    expect(page.locator("#notify-kinds")).to_be_visible()
    rows = page.locator("#notify-rows")
    switches = [rows.locator(f"input[name={n}]") for n in ("briefings", "replies", "rooms")]
    hold, send = rows.locator("input[name=quiet]").nth(0), rows.locator("input[name=quiet]").nth(1)
    for box in switches:
        expect(box).to_be_checked()
    expect(hold).to_have_value("queue")
    expect(hold).to_be_checked()
    switches[2].uncheck()
    expect(page.locator("#notify-note")).to_have_text("Saved.")
    send.check()
    wait_for(lambda: len(stub.notify_posts) == 2)
    assert stub.notify_posts == [{"rooms": False}, {"quiet": "send"}]
    assert stub.notify == {"briefings": True, "replies": True, "rooms": False, "quiet": "send"}
    assert stub.settings_posts == []                     # the routing table is not saved with it
    # a refusal: the bot's words stay, and the choice goes back to what is saved
    page.evaluate("document.querySelector('#notify-rows input[value=queue]').value = 'later'")
    hold.check()
    expect(page.locator("#notify-note")).to_have_text(
        'Not saved: Quiet hours take "Hold until morning" or "Send anyway".')
    expect(send).to_be_checked()
    for text in page.locator("#notify-settings").all_inner_texts():
        assert not JARGON.search(text), text
    page.click("#settings button[type=submit]")
    page.click("#open-settings")                          # opened again: what the bot holds
    page.click("#go-notify")
    expect(switches[2]).not_to_be_checked()
    expect(send).to_be_checked()
    assert errors == ["Failed to load resource: the server responded with a status of 400 (Bad Request)"]
    # only the notify page: no Models
    page, _stub, errors = app(settings=["notify"], push=True)
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    expect(page.locator(SHOWN_ITEMS)).to_have_text(["Notifications", "How Tacitus works"])
    page.click("#go-notify")
    expect(page.locator("#notify-kinds")).to_be_visible()
    assert not errors, errors
    # push off: no Notifications, the Models page still there
    page, _stub, errors = app(settings=True)
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    expect(page.locator(SHOWN_ITEMS)).to_have_text(["Models", "How Tacitus works"])
    page, _stub, errors = app(settings=["notify"])
    page.wait_for_selector(".thread-item")
    page.click("#open-settings")
    expect(page.locator(SHOWN_ITEMS)).to_have_text(["How Tacitus works"])
    assert not errors, errors


DETAILS_REPLY = ("Tuesday at 2:00 works for both of you.\n\n---\n*EA · opus: 11s · 10% session*"
                 "\n\n─────\n:calendar: propose-meeting (duration=30) → 3 options /1.7s")


def test_agent_and_model_details_stay_out_of_a_reply_until_the_switch_shows_them(app):
    """With the bot's "Show agent and model details" off, a reply shows its
    words without the line that names the model and the time, or the
    scheduler's tool trace, and a trace posted on its own shows nothing.
    The switch in the Settings menu saves at once and draws the open
    conversation again with them; the next visit starts with them. A bot
    with no such switch leaves every reply whole."""
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app(details=False)
    ask = stub.add(W.MAIN, "Find a time for the review", author="owner")
    stub.add(W.MAIN, DETAILS_REPLY, thread_key=ask["thread_key"])
    stub.add(W.MAIN, "─────\n:calendar: solve → ok /0.4s", thread_key=ask["thread_key"])
    page.goto(page.url.split("#")[0] + f"#/{W.MAIN}/{ask['thread_key']}")
    reply = page.locator("#messages .msg.bot").first
    expect(reply).to_contain_text("Tuesday at 2:00 works for both of you.")
    expect(page.locator("#messages")).not_to_contain_text("opus: 11s")
    expect(page.locator("#messages")).not_to_contain_text("propose-meeting")
    expect(page.locator("#messages .msg.bot:visible")).to_have_count(1)  # the trace alone is hidden
    page.locator("#open-settings").click()
    row, box = page.locator("#details-row"), page.locator("#show-details")
    expect(row.locator(".s-label")).to_have_text("Show agent and model details")
    assert not JARGON.search(row.inner_text()), row.inner_text()
    expect(box).not_to_be_checked()
    row.click()
    expect(box).to_be_checked()
    wait_for(lambda: stub.details_posts == [{"on": True}])
    page.locator("#settings-close").click()
    expect(page.locator("#messages")).to_contain_text("EA · opus: 11s")
    expect(page.locator("#messages")).to_contain_text("solve")
    expect(page.locator("#messages .msg.bot:visible")).to_have_count(2)
    page.reload()
    expect(page.locator("#messages")).to_contain_text("propose-meeting")
    assert not errors, errors
    # a bot without the switch: no row, and every reply whole
    page, stub, errors = app()
    ask = stub.add(W.MAIN, "Find a time", author="owner")
    stub.add(W.MAIN, DETAILS_REPLY, thread_key=ask["thread_key"])
    page.goto(page.url.split("#")[0] + f"#/{W.MAIN}/{ask['thread_key']}")
    expect(page.locator("#messages")).to_contain_text("opus: 11s")
    page.locator("#open-settings").click()
    expect(page.locator("#details-row")).to_be_hidden()
    assert not errors, errors
