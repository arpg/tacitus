"""Calendly meeting polls (preprocessor/poll_calendly.py): the read through
the two public endpoints, the slot ids and the click targets, and the fill
against a fake page that stops before the submit unless told to go."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
import poll_calendly as pc  # noqa: E402
import poll_filler as pf  # noqa: E402

URL = "https://calendly.com/d/d2kz-nx3-6x6/gilberto-thesis-proposal"


def _fetch(url):
    if "/scheduling_links/d2kz-nx3-6x6" in url:
        return {"owner_type": "Share", "owner_uuid": "share-1", "uid": "d2kz-nx3-6x6"}
    if "/event_types/lookup" in url and "share_uuid=share-1" in url:
        return {"uuid": "et-1", "name": "Thesis Proposal", "booking_method": "poll", "duration": 120,
                "availability_timezone": "America/Denver", "start_date": "2026-12-07", "end_date": "2026-12-08",
                "profile": {"name": "A Student"}}
    if "/event_types/et-1/calendar/range" in url:
        assert "range_start=2026-12-07" in url and "range_end=2026-12-08" in url and "America%2FDenver" in url
        return {"days": [
            {"date": "2026-12-07", "spots": [{"status": "available", "start_time": "2026-12-07T15:00:00-07:00"},
                                             {"status": "available", "start_time": "2026-12-07T16:00:00-07:00"}]},
            {"date": "2026-12-08", "spots": [{"status": "unavailable", "start_time": "2026-12-08T15:00:00-07:00"},
                                             {"status": "available", "start_time": "2026-12-08T17:00:00-07:00"}]}]}
    raise AssertionError(f"unexpected url {url}")


def test_the_read_lists_the_available_slots_with_their_local_start_as_the_id():
    out = pc.read(URL, fetch=_fetch, user_tz=ZoneInfo("America/Denver"))
    assert out["platform"] == "calendly" and out["event_name"] == "Thesis Proposal"
    assert out["slot_duration_minutes"] == 120 and out["poll_timezone"] == "America/Denver"
    assert out["dates"] == ["2026-12-07", "2026-12-08"] and out["organizer"] == "A Student"
    ids = [s["slot_id"] for s in out["slots"]]
    assert ids == ["2026-12-07T15:00:00-07:00", "2026-12-07T16:00:00-07:00", "2026-12-08T17:00:00-07:00"]
    first = out["slots"][0]
    assert first["start"] == "2026-12-07T22:00:00+00:00" and first["end"] == "2026-12-08T00:00:00+00:00"
    assert first["local_time"] == "Mon Dec 07 03:00 PM"


def test_a_booking_page_is_refused_as_a_poll():
    def fetch(url):
        if "/scheduling_links/" in url:
            return {"owner_uuid": "s"}
        return {"booking_method": "instant", "uuid": "x"}
    with pytest.raises(pc.CalendlyError, match="booking page"):
        pc.read("https://calendly.com/d/abc-def/x", fetch=fetch)
    with pytest.raises(pc.CalendlyError, match="not a Calendly meeting-poll link"):
        pc.poll_code("https://calendly.com/someone/30min")


def test_click_targets_are_the_page_texts():
    t = pc.targets(["2026-12-07T15:00:00-07:00", "2026-12-16T09:30:00-07:00"])
    assert t[0] == {"slot_id": "2026-12-07T15:00:00-07:00", "day": "Monday, December 7", "time": "3:00 pm"}
    assert t[1]["day"] == "Wednesday, December 16" and t[1]["time"] == "9:30 am"


class _Loc:
    def __init__(self, n, page, sel):
        self.n, self.page, self.sel = n, page, sel

    def count(self):
        return self.n

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.page.clicks.append(self.sel)

    def fill(self, value):
        self.page.filled[self.sel] = value


class _Page:
    """A fake Playwright page: the selectors present, which slots have a
    button on the page (`clicked` = the slot ids whose button exists), the
    page's own tally of selected times, and the body text after a submit."""
    def __init__(self, present, clicked, after="Thank you, your response was submitted"):
        self.present, self.clicked, self.after = set(present), clicked, after
        self.clicks, self.filled, self.gotos, self.submitted = [], {}, [], False
        self.buttons = {(t["day"], t["time"]) for t in pc.targets(clicked)}
        self.selected = 0

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        if sel.startswith("xpath="):
            m = __import__("re").search(r"text\(\)\)='([^']+)'\]/following::button\[normalize-space\(\)='([^']+)'\]", sel)
            key = (m.group(1), m.group(2)) if m else None
            loc = _Loc(1 if key in self.buttons else 0, self, sel)

            def click(timeout=None, _k=key):
                self.clicks.append(_k)
                self.selected += 1
            loc.click = click  # type: ignore[method-assign]
            return loc
        if sel == "button[type='submit']" and sel in self.present:
            loc = _Loc(1, self, sel)
            loc.click = lambda timeout=None: setattr(self, "submitted", True)  # type: ignore[method-assign]
            return loc
        return _Loc(1 if sel in self.present else 0, self, sel)

    def inner_text(self, sel):
        return self.after if self.submitted else f"{self.selected} times selected"


FORM = {"button:has-text('Next')", "#full_name", "#email", "button[type='submit']"}


def test_the_fill_stops_before_the_submit_unless_told_to_go():
    ids = ["2026-12-07T15:00:00-07:00", "2026-12-08T17:00:00-07:00"]
    page = _Page(FORM, clicked=ids[:1])
    shots = []
    res = pc.fill(page, URL, "Kit Calloway", ids, email="kit@example.edu", dry_run=True,
                  screenshot=lambda label: shots.append(label) or f"/x/{label}.png", wait_ms=0)
    assert res["ok"] and res["dry_run"] and not res["submitted"] and not page.submitted
    assert res["clicked"] == ids[:1] and res["missing"] == ids[1:]
    assert page.clicks[0] == ("Monday, December 7", "3:00 pm"), "a real pointer click on the day's button"
    assert pc.button_xpath("Monday, December 7", "3:00 pm").startswith("xpath=//*[normalize-space(text())='Monday, December 7']")
    assert pc.selected_count("12 times selected Next") == 12 and pc.selected_count("1 time selected") == 1
    assert pc.selected_count("nothing") == 0
    assert page.filled == {"#full_name": "Kit Calloway", "#email": "kit@example.edu"}
    assert res["screenshot"] == "/x/calendly-pre-submit.png" and shots == ["calendly-pre-submit"]
    page = _Page(FORM, clicked=ids)
    res = pc.fill(page, URL, "Kit Calloway", ids, email="kit@example.edu", dry_run=False, wait_ms=0)
    assert res["submitted"] and res["ok"] and page.submitted and res["missing"] == []


def test_the_fill_reports_a_missing_form_or_no_matching_slot():
    ids = ["2026-12-07T15:00:00-07:00"]
    res = pc.fill(_Page({"button:has-text('Next')"}, clicked=ids), URL, "K", ids, email="k@x", wait_ms=0)
    assert not res["ok"] and "form fields not found" in res["error"] and not res["submitted"]
    res = pc.fill(_Page(FORM, clicked=[]), URL, "K", ids, email="k@x", wait_ms=0)
    assert not res["ok"] and res["error"].startswith("no slot button matched")


def test_poll_filler_routes_calendly_and_keeps_the_vote_behind_go():
    assert pf._detect_platform(URL) == "calendly"
    assert pf._validate_url(URL) == URL
    assert "calendly" in pf.API_READERS and "calendly" in pf.PLATFORM_FILLERS
    assert "calendly" not in pf.API_FILLERS, "the vote is a browser"
    assert pf.FILL_DRY_RUN is True
    text = (REPO / "preprocessor" / "poll_filler.py").read_text()
    assert 'FILL_DRY_RUN = not getattr(args, "go", False)' in text and '"--go"' in text
    doc = (REPO / "docs" / "ea" / "polls.md").read_text()
    assert "Calendly" in doc and "--go" in doc
