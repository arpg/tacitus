#!/usr/bin/env python3
"""Calendly meeting polls for poll_filler: a browser-free read, a browser
fill that stops before the vote unless told to go.

A Calendly *meeting poll* (`calendly.com/d/<code>/<slug>`) asks each
invitee to tick the times that work; the organiser picks one later. It
is a vote, so it belongs with the other polls (ask-first), not with the
booking links (read-only).

The read needs no browser. The page itself is a JavaScript shell; the
slots come from two public GETs the page makes:

  /api/booking/scheduling_links/<code>            -> the share uuid
  /api/booking/event_types/lookup?share_uuid=...   -> the event type: uuid,
                                                      name, duration, zone,
                                                      start and end dates
  /api/booking/event_types/<uuid>/calendar/range?timezone=...&range_start=
      <start>&range_end=<end>                      -> days[].spots[].start_time

A slot's id is its local start, as Calendly prints it with the offset
(`2026-12-07T15:00:00-07:00`), so the fill can find the day heading and
the time button on the page from the id alone.

The fill is a browser: the page shows a day heading ("Monday, December 7")
and a button per time ("3:00 pm"); a vote is those clicks, "Next", the
name and the email, and a submit. `dry_run` does everything but the
submit and leaves a screenshot, so the owner can look before the vote
goes. Calendly has no invitee API for a vote.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = "https://calendly.com/api/booking"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) EA-System/1.0"


class CalendlyError(RuntimeError):
    pass


def poll_code(url: str) -> str:
    """The share code of a `calendly.com/d/<code>/...` link."""
    m = re.search(r"calendly\.com/d/([A-Za-z0-9-]+)", url)
    if not m:
        raise CalendlyError("not a Calendly meeting-poll link (calendly.com/d/<code>/...)")
    return m.group(1)


def api_get(path: str, params: dict | None = None, *, fetch=None) -> dict:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    if fetch is not None:
        return fetch(url)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if e.fp else ""
        raise CalendlyError(f"Calendly {e.code} on {path}: {body[:160]}") from None
    except (urllib.error.URLError, ValueError) as e:
        raise CalendlyError(f"Calendly request failed on {path}: {e}") from None


def read(url: str, *, fetch=None, user_tz: ZoneInfo | None = None) -> dict:
    """The poll's slots in poll_filler's shape: {"platform", "event_name",
    "url", "dates", "slot_duration_minutes", "poll_timezone", "organizer",
    "slots": [{"start", "end", "slot_id", "local_time"}]}."""
    code = poll_code(url)
    link = api_get(f"/scheduling_links/{code}", fetch=fetch)
    share = link.get("owner_uuid") or ""
    if not share:
        raise CalendlyError("the link names no share uuid")
    et = api_get("/event_types/lookup", {"share_uuid": share}, fetch=fetch)
    if et.get("booking_method") != "poll":
        raise CalendlyError(f"this Calendly link is a {et.get('booking_method') or 'booking page'}, not a meeting poll; "
                            "a booking page is read-only here (booking_link.py)")
    uuid = et.get("uuid") or ""
    tz_name = et.get("availability_timezone") or "UTC"
    try:
        poll_tz = ZoneInfo(tz_name)
    except Exception:
        poll_tz = timezone.utc
    duration = int(et.get("duration") or 60)
    start, end = et.get("start_date") or "", et.get("end_date") or ""
    rng = api_get(f"/event_types/{uuid}/calendar/range",
                  {"timezone": tz_name, "diagnostics": "false", "range_start": start, "range_end": end}, fetch=fetch)
    slots, dates = [], set()
    for day in rng.get("days") or []:
        for spot in day.get("spots") or []:
            if spot.get("status") not in (None, "available"):
                continue
            st = str(spot.get("start_time") or "")
            try:
                local = datetime.fromisoformat(st)
            except ValueError:
                continue
            if local.tzinfo is None:
                local = local.replace(tzinfo=poll_tz)
            utc = local.astimezone(timezone.utc)
            dates.add(local.strftime("%Y-%m-%d"))
            shown = local.astimezone(user_tz) if user_tz else local
            slots.append({"start": utc.isoformat(), "end": (utc + timedelta(minutes=duration)).isoformat(),
                          "slot_id": st, "local_time": shown.strftime("%a %b %d %I:%M %p")})
    return {"platform": "calendly", "event_name": et.get("name") or "", "url": url,
            "dates": sorted(dates), "slot_duration_minutes": duration, "poll_timezone": tz_name,
            "organizer": ((et.get("profile") or {}).get("name") or et.get("publishers_list") or ""),
            "event_uuid": uuid, "slots": slots}


def targets(slot_ids: list[str]) -> list[dict]:
    """What to click for each slot id: the day heading text and the time
    button text as the page prints them ("Monday, December 7", "3:00 pm")."""
    out = []
    for sid in slot_ids:
        local = datetime.fromisoformat(str(sid))
        day = f"{local.strftime('%A')}, {local.strftime('%B')} {local.day}"
        hour = local.strftime("%I").lstrip("0") or "12"
        t = f"{hour}:{local.strftime('%M')} {local.strftime('%p').lower()}"
        out.append({"slot_id": sid, "day": day, "time": t})
    return out


# The browser side. `page` is a Playwright page; everything the page does
# is behind this one function so the tests can run it against a fake.
_CLICK_JS = """(wanted) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const clicked = [], missing = [];
  const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,div,button'));
  for (const w of wanted) {
    let dayIdx = all.findIndex(e => e.children.length === 0 && norm(e.textContent) === norm(w.day));
    if (dayIdx < 0) { missing.push(w.slot_id); continue; }
    let found = null;
    for (let i = dayIdx + 1; i < all.length; i++) {
      const e = all[i];
      if (e.children.length === 0 && /^[a-z]+day, [a-z]+ \\d{1,2}$/.test(norm(e.textContent))) break;
      if (e.tagName === 'BUTTON' && norm(e.textContent) === norm(w.time)) { found = e; break; }
    }
    if (!found) { missing.push(w.slot_id); continue; }
    if (found.getAttribute('aria-pressed') !== 'true' && found.getAttribute('aria-checked') !== 'true') found.click();
    clicked.push(w.slot_id);
  }
  return {clicked, missing};
}"""


def fill(page, url: str, name: str, slot_ids: list[str], *, email: str, dry_run: bool = True,
         screenshot=None, wait_ms: int = 8000) -> dict:
    """Tick the slots, go to the form, enter the name and the email, and
    submit unless dry_run. Returns {"ok", "clicked", "missing", "submitted",
    "dry_run", "screenshot", "error"}."""
    shot = screenshot or (lambda label: "")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    for sel in ("button:has-text('Decline')", "button:has-text('I understand')"):
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=2000)
        except Exception:
            pass
    res = page.evaluate(_CLICK_JS, targets(slot_ids))
    clicked, missing = list(res.get("clicked") or []), list(res.get("missing") or [])
    if not clicked:
        return {"ok": False, "clicked": [], "missing": missing, "submitted": False, "dry_run": dry_run,
                "screenshot": shot("calendly-no-slots"), "error": "no slot button matched; nothing selected"}
    page.wait_for_timeout(500)
    nxt = page.locator("button:has-text('Next')")
    if not nxt.count():
        return {"ok": False, "clicked": clicked, "missing": missing, "submitted": False, "dry_run": dry_run,
                "screenshot": shot("calendly-no-next"), "error": "no Next button after selecting"}
    nxt.first.click()
    page.wait_for_timeout(4000)
    filled = {}
    for field, value, sels in (("name", name, ("#full_name", "input[name='full_name']", "input[name='name']",
                                               "input[autocomplete='name']")),
                               ("email", email, ("#email", "input[name='email']", "input[type='email']"))):
        for sel in sels:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(value)
                filled[field] = sel
                break
    if "name" not in filled or "email" not in filled:
        return {"ok": False, "clicked": clicked, "missing": missing, "submitted": False, "dry_run": dry_run,
                "screenshot": shot("calendly-form"), "error": f"form fields not found (found {sorted(filled)})"}
    pre = shot("calendly-pre-submit")
    if dry_run:
        return {"ok": True, "clicked": clicked, "missing": missing, "submitted": False, "dry_run": True,
                "screenshot": pre, "error": ""}
    submit = page.locator("button[type='submit']")
    if not submit.count():
        submit = page.locator("button:has-text('Submit')")
    if not submit.count():
        return {"ok": False, "clicked": clicked, "missing": missing, "submitted": False, "dry_run": False,
                "screenshot": pre, "error": "no submit button on the form"}
    submit.first.click()
    page.wait_for_timeout(6000)
    text = page.inner_text("body").lower()
    done = any(k in text for k in ("thank", "submitted", "confirmed", "you're all set", "response recorded",
                                   "we've sent", "times selected"))
    return {"ok": done, "clicked": clicked, "missing": missing, "submitted": done, "dry_run": False,
            "screenshot": shot("calendly-post-submit"),
            "error": "" if done else "submitted, but the page did not confirm; check the screenshot"}
