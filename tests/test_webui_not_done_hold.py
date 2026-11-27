"""Open, not now in the web app, as a person sees it (docs/webchat.md, Open,
not now): what the owner said Not done to is folded at the foot of Home,
one line an item with its count, out of Left for you; a tap on a line
unfolds it in place with its usual choices and what brings it back; Not
done on what looks done shows its line alone, as a Done does.

The bot's own code answers (tests/webui_stub_server.py, `home_src`): Home
and its items are home_screen's and home_items' on made-up sources where
the owner holds two rows. Made-up data only."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import home_fixtures as F  # noqa: E402
import webui_stub_server as W  # noqa: E402
from webui_browser import app, browser, settled, stub_factory, wait_for  # noqa: E402,F401  (the fixtures)

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
HELD = ".h-page > details.h-peek"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def held_sources(state: dict):
    """One list's afternoon where the owner holds the slides (a to-do, set
    aside yesterday) and the seminar date (a reply owed, three days ago);
    `state["proposal"]` puts a close proposal on the workshop review."""
    def src(tz=""):
        got = F.sources("normal", dm=W.DM, main=W.MAIN)
        got.one_list = True
        got.rows["response-r1"]["source_message_id"] = "m-r1"
        for lid, days in (("task-s2", 1), ("response-r1", 3)):
            at = got.now - days * 86400
            got.rows[lid]["hold"] = {"at": _iso(at), "day": datetime.fromtimestamp(at, F.ZONE).date().isoformat(),
                                     "inbound": "", "by": "user"}
        if state.get("proposal"):
            got.rows["deadline-w1"]["close_proposal"] = {"why": "The reviews went in", "first_at": _iso(got.now)}
        return got
    return src


def held_app(app, state=None, **kw):
    from playwright.sync_api import expect
    kw.setdefault("context", CLOCK)
    page, stub, errors = app(home_src=held_sources(state if state is not None else {}), one_list=True, **kw)
    stub.letters = []
    stub.set_home(stub.real_payload())
    expect(page.locator(HELD)).to_be_visible()
    settled(page)
    return page, stub, errors


def test_open_not_now_is_folded_at_the_foot_of_home_and_a_line_unfolds_in_place(app):
    from playwright.sync_api import expect
    page, stub, errors = held_app(app, **PHONE)
    held = page.locator(HELD)
    expect(held.locator("> summary")).to_have_text("Open, not now (2)")
    assert held.get_attribute("open") is None                                  # folded
    assert page.evaluate("document.querySelector('.h-page').lastElementChild.matches('details.h-peek')")
    for ref in ("item:task-s2", "reply:response-r1"):
        assert page.locator(f".h-main .h-block[data-ref='{ref}']").count() == 0   # not left for you
    held.locator("> summary").click()
    lines = held.locator(".h-block")
    expect(lines).to_have_count(2)
    expect(lines.locator(".h-title")).to_have_text(["Send the slides to the committee", "Seminar date"])
    expect(held.get_by_role("list", name="Open, not now")).to_be_visible()
    for i in range(2):                                                         # one line each: no label, no buttons
        line = lines.nth(i)
        expect(line.locator(".h-top")).to_be_hidden()
        expect(line.locator(".h-acts")).to_be_hidden()
        assert line.bounding_box()["height"] < 64
    slides = held.locator(".h-block[data-ref='item:task-s2']")
    slides.locator("> button").click()
    expect(slides.locator("> button")).to_have_attribute("aria-expanded", "true")
    expect(slides.locator("[data-part='brief']")).to_contain_text("You set it aside yesterday.")
    expect(slides.locator("[data-part='brief']")).to_contain_text("It stays here until you work on it.")
    expect(slides.locator("[data-part='row'] .btn")).to_have_text(["Done", "Later", "Later: waiting on them"])
    expect(slides.locator("[data-part='tell'] textarea")).to_be_visible()
    expect(slides.locator(".h-top .h-label")).to_have_text("To do")            # unfolded, it is a whole item
    assert stub.item_gets[-1] == "item:task-s2"                               # read from the bot when it opens
    assert not errors, errors


def test_not_done_on_what_looks_done_says_its_line_alone(app):
    from playwright.sync_api import expect
    page, stub, errors = held_app(app, {"proposal": True})
    block = page.locator(".h-main .h-block[data-ref='proposal:deadline-w1']")
    block.locator("> button").click()
    expect(block.locator("[data-part='row'] .btn")).to_have_text(["Done", "Not done"])
    block.get_by_role("button", name="Not done").click()
    wait_for(lambda: stub.home_presses == [("close", "deadline-w1", True)])
    expect(block.locator(".h-said")).to_have_text("Not done. Tacitus holds it until you want it.")
    expect(block.get_by_role("button", name="Done", exact=True)).to_have_count(0)  # no buttons under its line
    assert not errors, errors
