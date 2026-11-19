"""The status light in the web app, as a person sees it (docs/webchat.md,
Status light): the light per level with its shape and words, the tooltip
under a mouse and the keyboard, the sheet with its conditions, details,
Got it and Recently cleared, a device that may not open the details, the
live `light` event, the sheet a notification opens, and a phone's top bar
at 382 px with nothing overlapping. The bot's side is in
tests/test_status_light.py. Made-up data only."""
from __future__ import annotations

import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_webui import PHONE, SIDEWAYS, SMALL_TARGETS, app, browser, stub_factory, wait_for  # noqa: E402,F401  (the fixtures)

NOW = time.time()
OK = {"level": "ok", "line": "All good", "conditions": [], "cleared": [], "as_of": NOW}
SIGN_IN = {"id": "sentinel:mail_sign_in", "level": "problem", "title": "Mail sign-in needs you",
           "line": "Tacitus is signed out of Outlook, so it cannot read or send mail.",
           "todo": "Sign in to Outlook again on Tacitus's computer. The details say how.",
           "since": NOW - 3 * 3600, "read": False}
SLOW = {"id": "mail-service", "level": "warn", "title": "Outlook is not answering",
        "line": "Outlook is not answering, so mail is not read for now.",
        "todo": "Nothing to do. Tacitus keeps trying.", "since": NOW - 600, "read": False}
NOTICE = {"id": "notice:4", "level": "warn", "title": "A setting changed",
          "line": "Tacitus's helper changed one of the files that guide its later work.",
          "todo": "Read the change in the details. Tap Got it when you have read it.",
          "since": NOW - 3600, "read": True}
DRIVE = {"id": "drive", "level": "warn", "title": "Google Drive is not connected",
         "line": "Tacitus cannot open the Google Drive files your mail links to.",
         "todo": "Tap Connect, and Tacitus sends you a link to sign in.", "since": NOW - 86400,
         "read": False, "action": {"label": "Connect", "say": "connect Google Drive"}}
CLEARED = [{"id": "sentinel:cron_heartbeat", "level": "problem", "title": "Scheduled work stopped",
            "line": "", "since": NOW - 7200, "until": NOW - 3600, "how": "fixed"}]


def summary(*conds, cleared=()):
    rank = ["ok", "warn", "problem"]
    level = max((c["level"] for c in conds), key=rank.index, default="ok")
    line = "All good" if not conds else conds[0]["title"] if len(conds) == 1 else f"{len(conds)} things need a look"
    return {"level": level, "line": line, "conditions": list(conds), "cleared": list(cleared), "as_of": NOW}


def _shape(page, sel):
    return page.evaluate(f"[...document.querySelector('{sel}').querySelectorAll('svg > *')]"
                         ".map((e) => e.tagName + '.' + e.getAttribute('class')).join(' ')")


def test_the_light_says_each_level_in_shape_and_words_and_follows_the_bot(app):
    """Green is a ringed check, yellow a triangle, red a filled circle with
    a mark; the label says the bot's line. A `light` event redraws it, and a
    stream that stays down leaves a dashed ring that says it cannot know."""
    from playwright.sync_api import expect
    page, stub, errors = app(status=summary())
    light = page.locator("#light-nav")
    expect(light).to_have_attribute("aria-label", "Status: All good")
    expect(light).to_have_class("icon light-btn lv-ok")
    assert _shape(page, "#light-nav") == "circle.ring path.ink"
    expect(page.locator("#light")).to_be_hidden()  # one light at a time: the menu's column on a wide screen
    stub.set_status(summary(SLOW))
    expect(light).to_have_attribute("aria-label", "Status: Outlook is not answering")
    assert _shape(page, "#light-nav") == "path.fill path.mark circle.mark"
    stub.set_status(summary(SIGN_IN, SLOW))
    expect(light).to_have_attribute("aria-label", "Status: 2 things need a look")
    expect(light).to_have_class("icon light-btn lv-problem")
    assert _shape(page, "#light-nav") == "circle.fill path.mark circle.mark"
    assert not errors, errors
    page.evaluate("setLive.grace = 200")
    stub.drop_streams(refuse=2)
    expect(light).to_have_class("icon light-btn lv-unknown", timeout=5000)
    expect(light).to_have_attribute("aria-label", "Status: Not connected, so Tacitus cannot say")
    expect(light).to_have_class("icon light-btn lv-problem", timeout=15000)  # back, and read again


def test_the_tooltip_shows_under_a_mouse_and_the_keyboard(app):
    from playwright.sync_api import expect
    page, stub, errors = app(status=summary(SIGN_IN, SLOW))
    tip = page.locator("#light-nav-tip")
    expect(tip).to_be_hidden()
    page.hover("#light-nav")
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("2 things need a look")
    expect(tip.locator("span .lv")).to_have_count(2)
    expect(tip).to_contain_text("Mail sign-in needs you")
    page.mouse.move(900, 600)
    expect(tip).to_be_hidden()
    page.focus("#light-nav")
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(tip).to_be_visible()
    assert not errors, errors


def test_the_sheet_lists_each_condition_opens_its_details_and_keeps_them_through_a_redraw(app):
    """A press opens the sheet: the conditions in plain words, since when,
    what to do, Details folded until opened (the bot's detail and log lines,
    Copy); Got it moves a notice to Recently cleared; an action posts its
    words in a new Ask Tacitus conversation. A `light` event redraws the
    open sheet and an opened Details stays open."""
    from playwright.sync_api import expect
    page, stub, errors = app(status=summary(SIGN_IN, NOTICE, DRIVE, cleared=CLEARED))
    stub.status_details["sentinel:mail_sign_in"] = {
        "id": "sentinel:mail_sign_in", "detail": "Sentinel check mail_sign_in: Outlook sign-in is GONE",
        "log": ["2026-11-18T17:40:01Z PROBLEM mail_sign_in: Outlook sign-in is GONE"], "log_from": "sentinel.log"}
    page.click("#light-nav")
    sheet = page.locator("#status")
    expect(sheet).to_be_visible()
    expect(page.locator("#status-head")).to_contain_text("3 things need a look")
    conds = sheet.locator(".cond")
    expect(conds).to_have_count(3)
    first = conds.nth(0)
    expect(first).to_have_attribute("data-level", "problem")
    expect(first).to_contain_text("Tacitus is signed out of Outlook")
    expect(first).to_contain_text("Since ")
    expect(first.locator("pre")).to_have_count(0)
    first.locator("summary").click()
    expect(first.locator("pre").first).to_have_text("Sentinel check mail_sign_in: Outlook sign-in is GONE")
    expect(first.locator("pre").nth(1)).to_contain_text("PROBLEM mail_sign_in")
    expect(first.get_by_role("button", name="Copy")).to_be_visible()
    expect(sheet.locator("#status-cleared")).to_contain_text("Scheduled work stopped")
    stub.set_status(summary(SIGN_IN, NOTICE, DRIVE, SLOW, cleared=CLEARED))
    expect(conds).to_have_count(4)
    expect(sheet.locator('.cond[data-id="sentinel:mail_sign_in"] details')).to_have_attribute("open", "")
    sheet.locator('.cond[data-id="notice:4"]').get_by_role("button", name="Got it").click()
    wait_for(lambda: stub.status_reads == ["notice:4"])
    expect(sheet.locator('.cond[data-id="notice:4"]')).to_have_count(0)
    expect(sheet.locator("#status-cleared")).to_contain_text("A setting changed")
    sheet.locator('.cond[data-id="drive"]').get_by_role("button", name="Connect").click()
    wait_for(lambda: [p for p in stub.posts if p.get("text") == "connect Google Drive"])
    expect(sheet).to_be_hidden()
    assert not errors, errors


def test_a_device_away_from_home_reads_the_plain_words_without_details(app):
    from playwright.sync_api import expect
    page, stub, errors = app(status=summary(SIGN_IN, NOTICE), **PHONE)
    page.wait_for_selector("#light:not([hidden])", state="attached")
    stub.status_details_ok = False  # as the bot answers a device away from home
    page.evaluate("loadLight()")
    page.click("#light")
    sheet = page.locator("#status")
    expect(sheet.locator(".cond")).to_have_count(2)
    expect(sheet.locator(".cond details")).to_have_count(0)
    expect(sheet.get_by_role("button", name="Got it")).to_have_count(0)
    expect(page.locator("#status-off")).to_have_text("Details show only in Tacitus on your own network.")
    assert not errors, errors


def test_a_phones_top_bar_holds_the_light_with_nothing_overlapping_at_382px(app):
    """At 382 px: the list's bar (Menu, the place's name, the light, New
    conversation) and a conversation's bar (Back, its title, Claude, Mark
    done, and the light only while something needs a look) overlap nowhere,
    every control is 44 px, and nothing scrolls sideways. A notification's
    address (#/status) opens the sheet."""
    from playwright.sync_api import expect
    page, stub, errors = app(status=summary(), viewport={"width": 382, "height": 820},
                             context=PHONE["context"])
    page.wait_for_selector(".thread-item")
    overlaps = """(sel) => { const els = [...document.querySelectorAll(sel)].filter((e) => e.offsetParent);
      const r = els.map((e) => e.getBoundingClientRect()), out = [];
      for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++)
        if (r[i].right > r[j].left + 0.5 && r[j].right > r[i].left + 0.5) out.push([els[i].id, els[j].id]);
      return [els.map((e) => e.id), out]; }"""
    list_bar = "#threads > .bar > :not(form, .light-wrap), #threads > .bar .light-btn"
    expect(page.locator("#light")).to_be_visible()
    ids, bad = page.evaluate(overlaps, list_bar)
    assert "light" in ids and "new-thread" in ids and not bad, (ids, bad)
    page.click(".thread-item >> nth=0")
    page.wait_for_selector("#messages .msg")
    expect(page.locator("#light-here")).to_be_hidden()  # green: calm, out of a conversation's bar
    stub.set_status(summary(SIGN_IN))
    expect(page.locator("#light-here")).to_be_visible()
    ids, bad = page.evaluate(overlaps, "#thread > .bar > *")
    assert "light-here" in ids and "thread-state" in ids and not bad, (ids, bad)
    assert page.evaluate(SMALL_TARGETS) == [] and page.evaluate(SIDEWAYS) == [0, 0]
    page.evaluate("location.hash = '#/status'")
    expect(page.locator("#status")).to_be_visible()
    assert page.evaluate("location.hash") != "#/status"
    assert not errors, errors


def test_a_bot_without_the_light_shows_none_and_asks_nothing(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    expect(page.locator("#light-nav")).to_be_hidden()
    expect(page.locator("#light")).to_be_hidden()
    assert stub.status_gets == 0 and not errors, errors
