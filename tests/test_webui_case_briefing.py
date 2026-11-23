"""The briefing on a case in the web app, as a person sees it
(docs/webchat.md, Home, "The briefing on a case"): a letter item opens
with its stored briefing above the choices (the moment's lines, what was
asked, its destinations, what came in and what is missing, then the
model's part, folded with Show more), its next steps as buttons that press
the case card's own answers (one asks for a date, one asks first, its
portal a link that posts nothing); a form (a `case` item) the same in its
own words; with none stored, the facts show at once and the rest comes in
below, moving nothing; and in Action requests the same briefing sits above
the case's card, folded, unfolding in place with the owner's words kept,
at the phone and desktop widths.

The bot's own code answers (tests/webui_stub_server.py, `home_src`,
`letters` and `work`): Home, the item, the act and case_briefing's state,
start and step over made-up cases with no ledger; only the model is a
stand-in.
Made-up data only."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import home_fixtures as F  # noqa: E402
import webui_stub_server as W  # noqa: E402
from webui_browser import (  # noqa: E402,F401  (the fixtures)
    SIDEWAYS, SMALL_TARGETS, _request, app, browser, settled, stub_factory, wait_for,
)

CLOCK = {"timezone_id": F.ZONE_NAME}
PHONE = {"viewport": {"width": 390, "height": 844},
         "context": {"has_touch": True, "is_mobile": True, "device_scale_factor": 3, **CLOCK}}
SIZES = pytest.mark.parametrize("size", ["phone", "desktop"])
CID = "lc-made-up-1"
REF = f"letter:{CID}"
FID = "case-form-made-up-1"
FREF = f"case:{FID}"
FORM_LINK = "https://forms.example.edu/annual/zz1"
PORTAL = "https://apply.example.com/ref/abc123"
SECTIONS = {"history": ["Rowan Vale asked for the letter and named Harbor Tech PhD.",
                        "Harbor Tech PhD sent its portal link."],
            "stakes": ["Harbor Tech PhD needs the letter by its date."],
            "unknown": ["The records do not show a statement."]}
# The date step is the case's one date: it names no part.
STEPS = [{"id": "s1", "kind": "draft", "params": {}},
         {"id": "s2", "kind": "open_link", "params": {"part": "p1"}},
         {"id": "s3", "kind": "set_due", "params": {}}]


def day(n: int) -> str:
    return (datetime.now(F.ZONE).date() + timedelta(days=n)).isoformat()


def day_words(n: int) -> str:
    d = datetime.now(F.ZONE).date() + timedelta(days=n)
    return f"{d:%a %b} {d.day}"


def a_case() -> dict:
    """A recommendation letter for a made-up person: one portal place due in
    six days, one by email with no date, a CV in."""
    def dest(i, name, due, method, link):
        return {"id": f"ld-{i}", "case": CID, "name": name, "due": due, "method": method, "link": link,
                "state": "accepted", "ledger_id": f"letter-{i}", "conversation_id": "", "sender": "", "draft": "",
                "submitted_at": "", "evidence": ""}
    return {"id": CID, "person_name": "Rowan Vale", "person_email": "rowan.vale@example.org", "kind": "recommendation",
            "status": "open", "decision": "accepted", "opened": day(-3) + "T09:00:00Z", "closed_at": "",
            "materials": [{"kind": "cv", "ref": "msg-1#Rowan_Vale_CV.pdf"}], "base_letter": "", "notes": "",
            "aliases": [], "keep_level": "full", "case_ledger_id": "", "questions": [],
            "destinations": [dest("p1", "Harbor Tech PhD", day(6), "portal", PORTAL),
                             dest("p2", "Pinecrest College MS", "", "email", "")]}


def stored(steps=None) -> dict:
    return {"version": "v1", "level": "full", "by": "local", "at": time.time() - 3600, "model": "",
            "text": W.D().render(SECTIONS), "steps": steps if steps is not None else STEPS}


def a_form() -> dict:
    """A disclosure form a made-up office asked for, due in four days on a
    form site, in the store's shape."""
    return {"id": FID, "kind": "form", "subkind": "disclosure", "counterpart_name": "Compliance Office",
            "counterpart_email": "compliance@example.edu", "title": "Annual disclosure", "status": "open",
            "decision": "accepted", "opened": day(-2) + "T09:00:00Z", "closed_at": "", "materials": [],
            "base_doc": "", "notes": "", "aliases": [], "keep_level": "full", "case_ledger_id": "",
            "questions": [], "messages": [],
            "parts": [{"id": "part-f1", "case": FID, "name": "Annual disclosure", "due": day(4), "method": "portal",
                       "link": FORM_LINK, "state": "accepted", "ledger_id": "", "conversation_id": "",
                       "sender": "", "draft": "", "delivered_at": "", "evidence": ""}]}


def case_app(app, *, stored_briefing=True, steps=None, drafted=False, work=(), **kw):
    """The app on Home with the made-up letter listed as a letter item (its
    first destination drafted where `drafted`) and `work` as case items."""
    from playwright.sync_api import expect
    kw.setdefault("context", CLOCK)
    page, stub, errors = app(home_src=lambda tz="": F.sources("normal", dm=W.DM, main=W.MAIN), **kw)
    case = a_case()
    if drafted:
        case["destinations"][0]["state"] = "drafted"
    stub.letters = [case]
    stub.work = list(work)
    stub.case_stored = stored(steps) if stored_briefing else None
    stub.case_answer = dict(SECTIONS, steps=[{"kind": "draft", "part": "", "date": "", "why": ""},
                                             {"kind": "set_due", "part": "p2", "date": "", "why": ""}])
    stub.set_home(stub.real_payload())
    expect(page.locator("#live.on")).to_be_attached()
    expect(page.locator(f".h-block[data-ref='{REF}']")).to_be_visible()
    settled(page)
    return page, stub, errors


def opened(page, ref=REF):
    from playwright.sync_api import expect
    block = page.locator(f".h-block[data-ref='{ref}']")
    block.locator("> button").click()
    expect(block.locator("> button")).to_have_attribute("aria-expanded", "true")
    expect(block.locator("[data-part='brief'] .h-quiet")).to_have_count(0)
    return block


def order(block):
    return block.evaluate("(e) => [...e.querySelector('.h-body').children].filter((c) => c.childElementCount)"
                          ".map((c) => c.dataset.part)")


@SIZES
def test_a_letter_item_opens_with_its_briefing_above_the_choices(app, size):
    from playwright.sync_api import expect
    page, stub, errors = case_app(app, **(PHONE if size == "phone" else {}))
    block = opened(page)
    deeper = block.locator(".h-deeper")
    text = deeper.locator(".text")
    expect(text).to_contain_text("What happened")
    assert order(block) == ["brief", "deeper", "choices", "links"]       # ready as it opened: above the choices
    expect(text.locator("> p")).to_have_text(["Now", "What was asked", "The destinations", "What came in", "Still missing",
                                              "What happened", "What is at stake", "What Tacitus does not know"])
    expect(text.locator("li").first).to_have_text(re.compile(r"^Next due .*, in 6 days: Harbor Tech PhD\.$"))
    expect(text).to_contain_text("Pinecrest College MS: no due date, to write, by email")
    expect(text).to_contain_text("A CV (Rowan_Vale_CV.pdf).")
    expect(text).to_have_class(re.compile(r"\bh-fold\b"))                 # long: folded
    deeper.get_by_role("button", name="Show more").click()
    expect(text).not_to_have_class(re.compile(r"\bh-fold\b"))
    steps = deeper.locator("[role='group'] > *")
    expect(steps).to_have_text(["Draft the letter", "Open the portal Harbor Tech PhD",
                                "Set the date", "Continue in a conversation"])
    portal = deeper.get_by_role("link", name="Open the portal Harbor Tech PhD")
    expect(portal).to_have_attribute("href", PORTAL)
    expect(portal).to_have_attribute("target", "_blank")
    expect(portal).to_have_attribute("rel", "noopener noreferrer")
    # a yes with no date of the case's own: Home asks for its one date
    expect(block.locator(".h-choices .btn")).to_have_text([f"By {day_words(6)}", "Another date",
                                                           "Open it in Action requests"])
    assert PORTAL not in text.inner_text() and "rowan.vale@example.org" not in block.inner_text()
    settled(page)
    assert stub.deeper_posts == []                                         # nothing to write at the tap
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        assert page.evaluate(SIDEWAYS) == [0, 0]
        assert page.evaluate("$('thread-list').scrollWidth - $('thread-list').clientWidth") == 0
    assert not errors, errors


def test_a_step_presses_the_case_cards_own_answer(app):
    from playwright.sync_api import expect
    page, stub, errors = case_app(app)
    block = opened(page)
    deeper = block.locator(".h-deeper")
    deeper.get_by_role("button", name="Draft the letter").click()
    wait_for(lambda: stub.letter_presses == [(CID, "draft", "", "")])
    expect(block.locator(".h-note")).to_have_text("Tacitus is on it. Its card is in Action requests.")
    assert [a["choice"] for a in stub.acts] == ["step:s1"]
    # the date step asks for its day first, then sets the case's one date
    block = opened(page) if block.locator(".h-body").is_hidden() else block
    step = block.locator(".h-deeper").get_by_role("button", name="Set the date")
    step.click()
    field = block.locator(".h-deeper input[type='date']")
    expect(field).to_be_focused()
    field.fill(day(12))
    picked = datetime.now(F.ZONE).date() + timedelta(days=12)
    block.locator(".h-deeper").get_by_role("button", name=re.compile(f"^Use {picked:%a} {picked:%b} {picked.day}")).click()
    wait_for(lambda: len(stub.letter_presses) == 2)
    assert stub.letter_presses[1] == (CID, "date", "", day(12))
    assert stub.acts[-1]["choice"] == "step:s3" and stub.acts[-1]["arg"] == day(12)
    assert not errors, errors


def test_a_step_that_asks_first_needs_the_owners_yes(app):
    """Mark it submitted is the card's Submitted for that destination: it
    asks first, and a No to the question presses nothing."""
    from playwright.sync_api import expect
    page, stub, errors = case_app(app, steps=[{"id": "s1", "kind": "mark_delivered", "params": {"part": "p1"}}],
                                  drafted=True)
    block = opened(page)
    step = block.locator(".h-deeper").get_by_role("button", name="Mark it submitted Harbor Tech PhD")
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    step.click()
    wait_for(lambda: asked)
    assert asked[0] == "Mark Harbor Tech PhD as submitted? Only do this once it went in."
    settled(page)
    assert stub.letter_presses == [] and stub.acts == []
    page.once("dialog", lambda d: d.accept())
    step.click()
    wait_for(lambda: stub.letter_presses == [(CID, "submitted", "ld-p1", "")])
    expect(block.locator(".h-note")).to_have_text("Tacitus is on it. Its card is in Action requests.")
    assert not errors, errors


def test_with_nothing_stored_the_facts_show_and_the_rest_comes_in_below(app):
    from playwright.sync_api import expect
    page, stub, errors = case_app(app, stored_briefing=False)
    block = opened(page)
    deeper = block.locator(".h-deeper")
    expect(deeper.locator(".text")).to_contain_text("The destinations")
    thumb = [b.bounding_box() for b in block.locator(".h-choices .btn").all()]
    wait_for(lambda: stub.deeper_posts == [REF])                           # asked for on the open
    expect(deeper.locator(".text")).to_contain_text("What happened")       # written, then read again
    expect(deeper.locator("[role='group'] button")).to_have_text(["Draft the letter", "Set the date",
                                                                  "Continue in a conversation"])
    assert order(block) == ["brief", "choices", "links", "deeper"]         # it came later: below everything
    settled(page)
    assert [b.bounding_box() for b in block.locator(".h-choices .btn").all()] == thumb
    assert stub.case_stored["by"] == "local" and [s["kind"] for s in stub.case_stored["steps"]] == ["draft", "set_due"]
    assert not errors, errors


def test_a_form_item_gets_the_same_briefing_in_its_own_words(app):
    """A `case` item (a form) opens with the briefing a letter gets, in the
    form's words, and its step presses the case card's own answer."""
    from playwright.sync_api import expect
    page, stub, errors = case_app(app, work=[a_form()], steps=[{"id": "s1", "kind": "draft", "params": {}},
                                                               {"id": "s2", "kind": "open_link",
                                                                "params": {"part": "p1"}}])
    stub.case_stored["text"] = W.D().render({"history": ["The compliance office asked for the annual disclosure."],
                                             "stakes": ["The disclosure is due by its date."], "unknown": []})
    block = opened(page, FREF)
    text = block.locator(".h-deeper .text")
    expect(text).to_contain_text("The forms")
    expect(text).to_contain_text("Annual disclosure: due ")
    expect(text).to_contain_text("The answers.")
    expect(text).not_to_contain_text("letter")
    steps = block.locator(".h-deeper [role='group'] > *")
    expect(steps).to_have_text(["Fill it in", "Open the link Annual disclosure", "Continue in a conversation"])
    expect(block.locator(".h-deeper").get_by_role("link", name="Open the link Annual disclosure")).to_have_attribute(
        "href", FORM_LINK)
    block.locator(".h-deeper").get_by_role("button", name="Fill it in").click()
    wait_for(lambda: stub.case_presses == [(FID, "draft", "", "")])
    assert stub.letter_presses == []
    assert not errors, errors


LONG = "\n\n".join(["Now\n- Next due Tue, Dec 1, in 6 days: Harbor Tech PhD.",
                    "Since you last looked\n- A new message from Rowan Vale on Sat, Nov 21: Rowan Vale sent a new date.",
                    "What was asked\n- A recommendation letter for Rowan Vale.\n- You said yes.",
                    "What happened\n- Rowan Vale asked for the letter.\n- Harbor Tech PhD sent its portal link.",
                    "What is at stake\n- Harbor Tech PhD needs the letter by its date."])


def letter_card(stub):
    value = {"case": CID, "rows": ["letter-p1"]}
    return _request(stub, ":memo: *Recommendation letter for Rowan Vale*: 0 of 2 submitted.", kind="letter",
                    lines=["1. Harbor Tech PhD: due Tue Dec 1, to write, portal link"],
                    fields=[{"label": "From", "value": "Rowan Vale"}],
                    actions=[{"id": "letter_draft", "label": ":pencil2: Draft the letter", "value": value,
                              "style": "primary", "word": "draft", "ask": "what to say in it"},
                             {"id": "letter_decline", "label": "Decline", "value": value, "style": "", "word": "decline"}])


@SIZES
def test_action_requests_shows_the_briefing_above_the_case_card(app, size):
    from playwright.sync_api import expect
    page, stub, errors = app(**(PHONE if size == "phone" else {}))
    page.wait_for_selector(".thread-item")
    m = letter_card(stub)
    stub.request_context[m["id"]] = {"briefing": {"text": LONG, "by": "local", "at": time.time()}}
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    expect(block.locator(".req-brief")).to_have_count(0)                  # folded block: no briefing yet
    block.locator(".req-head").click()
    brief = block.locator(".req-brief")
    expect(brief.locator(".h-label")).to_have_text("What Tacitus knows")
    assert block.evaluate("(e) => [...e.querySelector('.req-body').children].map((c) => c.className)")[:2] == \
        ["req-brief", "card"]                                              # the briefing, then its card
    expect(brief.locator(".text")).to_have_class(re.compile(r"\bh-fold\b"))
    expect(brief.locator(".text")).to_contain_text("A new message from Rowan Vale")
    field = block.locator(".card-ask")
    field.fill("Keep it to one page")
    more = brief.get_by_role("button", name="Show more")
    expect(more).to_have_attribute("aria-expanded", "false")
    more.click()
    expect(brief.locator(".text")).not_to_have_class(re.compile(r"\bh-fold\b"))
    expect(brief.get_by_role("button", name="Show less")).to_have_attribute("aria-expanded", "true")
    expect(field).to_have_value("Keep it to one page")                    # unfolded in place: the words stay
    # a new request arriving draws the list again; the open block stays as it is
    _request(stub, "Send this reply?")
    expect(page.locator("#thread-list .reqs:not(.done) > .req")).to_have_count(2)
    expect(field).to_have_value("Keep it to one page")
    expect(brief.locator(".text")).not_to_have_class(re.compile(r"\bh-fold\b"))
    block.get_by_role("button", name="Draft the letter").click()
    wait_for(lambda: stub.clicks)
    assert stub.clicks[0]["action_id"] == "letter_draft" and stub.clicks[0]["text"] == "Keep it to one page"
    if size == "phone":
        assert page.evaluate(SMALL_TARGETS) == []
        assert page.evaluate(SIDEWAYS) == [0, 0]
    assert not errors, errors


def test_a_short_briefing_is_not_folded(app):
    from playwright.sync_api import expect
    page, stub, errors = app()
    page.wait_for_selector(".thread-item")
    m = letter_card(stub)
    stub.request_context[m["id"]] = {"briefing": {"text": "Now\n- Next due Tue, Dec 1, in 6 days: Harbor Tech PhD."}}
    page.evaluate("location.hash = '#/stream%3A7'")
    block = page.locator(f'.req[data-id="{m["id"]}"]')
    block.locator(".req-head").click()
    expect(block.locator(".req-brief .text")).not_to_have_class(re.compile(r"\bh-fold\b"))
    expect(block.locator(".req-brief button")).to_have_count(0)
    other = _request(stub, "Send this reply?")
    other_block = page.locator(f'.req[data-id="{other["id"]}"]')
    other_block.locator(".req-head").click()
    expect(other_block.locator(".req-brief")).to_have_count(0)            # a card with no case has none
    assert json.dumps(stub.request_context).count("briefing") == 1
    assert not errors, errors
