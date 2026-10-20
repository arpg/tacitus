#!/usr/bin/env python3
"""
poll_filler.py — Read and fill scheduling polls via Playwright.

Supports When2Meet, WhenIsGood, Doodle, Framadate, and Timeful. When2Meet,
WhenIsGood, Doodle, and Framadate use browser-based automation (Playwright).
Timeful uses its REST API directly (no browser needed).

Security: A hardcoded URL allowlist is enforced BEFORE any browser launches.
The allowlist is in source code, which is read-only for ea-bot (OS-enforced).
Prompt injection cannot modify it.

Dependencies:
    pip install playwright
    playwright install chromium

Usage:
    python3 poll_filler.py read-poll  --url <url> [--visible]
    python3 poll_filler.py fill-poll  --url <url> [--name "Name"] \
        (--available '[...]' | --available-file <path>) [--visible]

    The name on a poll is the user's configured display name, always:
    without --name it is that name, and a --name that is not it is refused,
    so the free-form field cannot carry anything else to an outside site.
    The name comes from the owner-private profile.json in an owner process
    and from user-profile.json otherwise. Each read and each submission
    goes on the reviewed outbound log (outbound_log.py): host, poll id,
    platform, the fields sent and their sizes.

    --available-file is strongly preferred for anything over ~5 slots:
    the agent writes the solver's slot list to disk and passes the path,
    eliminating hand-transcription bugs (e.g., silently dropping a slot
    while rebuilding the JSON array inline).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# -------------------------------------------------------------------
# URL allowlist — hardcoded, not configurable. Source is read-only for
# ea-bot, so the LLM cannot modify or bypass these patterns.
# -------------------------------------------------------------------
ALLOWED_URL_PATTERNS = [
    re.compile(r"^https://(www\.)?when2meet\.com/\?[\w\-]+$"),
    re.compile(r"^https://(www\.)?whenisgood\.net/[\w\-]+(/results)?$"),
    re.compile(r"^https://(www\.)?doodle\.com/(meeting/participate/id|poll|group-poll/participate)/[\w\-]+$"),
    re.compile(r"^https://(www\.)?(timeful\.app|betterwhen2meet\.com)/e/[\w\-]+$"),
    re.compile(r"^https://(www\.)?(beta\.)?framadate\.org/polls/[\w\-]+$"),
    # A Calendly meeting poll's share link; a booking page is booking_link.py's.
    re.compile(r"^https://(www\.)?calendly\.com/d/[\w\-]+(/[\w\-]+)?/?$"),
]

EA_DIR = os.environ.get("EA_DIR", os.path.expanduser("~/.ea"))
EXPORTS_DIR = os.path.join(EA_DIR, "exports")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outbound_log  # noqa: E402
import owner_trust  # noqa: E402


def _norm_name(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def _configured_name() -> str:
    """The user's display name for polls: the owner-private copy in an
    owner process when there is one, else the name in user-profile.json."""
    name = owner_trust.owner_profile_name()
    if name:
        return name
    profile_path = os.path.join(EA_DIR, "config", "user-profile.json")
    try:
        with open(profile_path) as f:
            profile = json.load(f)
    except (OSError, ValueError):
        return ""
    name = profile.get("name") if isinstance(profile, dict) else ""
    return " ".join(name.split()) if isinstance(name, str) else ""


def _pinned_name(requested) -> str:
    """The name to put on the poll. Exits when no name is configured or the
    request names someone else."""
    configured = _configured_name()
    if not configured:
        _fail("No display name is configured (the name in user-profile.json); "
              "a poll is filled only under the user's configured name")
    if requested and _norm_name(requested) != _norm_name(configured):
        _fail("--name must be the user's configured display name; leave it out "
              "and the configured name is used")
    return configured


def _poll_id(url: str) -> str:
    """The poll's id from its allowlisted URL (When2Meet keeps it in the
    query, the others in the last path segment)."""
    parsed = urlparse(url)
    if parsed.query and not parsed.path.strip("/"):
        return parsed.query[:80]
    segs = [x for x in parsed.path.split("/") if x and x != "results"]
    return (segs[-1] if segs else "")[:80]


def _get_user_email() -> str:
    """Read user's email from user-profile.json."""
    profile_path = os.path.join(EA_DIR, "config", "user-profile.json")
    try:
        with open(profile_path) as f:
            profile = json.load(f)
        return profile.get("email", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def _get_user_timezone() -> ZoneInfo:
    """The user's timezone: scheduling-preferences.json, else user_tz.py
    (the profile, then the system's zone)."""
    config_path = os.path.join(EA_DIR, "config", "scheduling-preferences.json")
    try:
        with open(config_path) as f:
            tz_name = json.load(f).get("timezone")
        if tz_name:
            return ZoneInfo(tz_name)
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return user_tz.zone(EA_DIR)


def _validate_url(url: str) -> str:
    """Validate URL against allowlist. Exits if not allowed."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        _fail(f"Only HTTPS URLs allowed, got {parsed.scheme}://")
    if not any(p.match(url) for p in ALLOWED_URL_PATTERNS):
        _fail(f"URL not on allowlist: {parsed.netloc}. "
              f"Allowed: when2meet.com, whenisgood.net, doodle.com, timeful.app, framadate.org, "
              f"calendly.com/d/<code> (a meeting poll)")
    return url


def _fail(msg: str):
    print(json.dumps({"error": msg}))
    sys.exit(1)


def _detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if "when2meet.com" in host:
        return "when2meet"
    if "whenisgood.net" in host:
        return "whenisgood"
    if "doodle.com" in host:
        return "doodle"
    if "timeful.app" in host or "betterwhen2meet.com" in host:
        return "timeful"
    if "framadate.org" in host:
        return "framadate"
    if "calendly.com" in host:
        return "calendly"
    _fail(f"Unsupported platform: {host}")


def _launch_browser(visible: bool = False):
    """Launch Playwright Chromium. Returns (playwright, browser, page)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _fail("playwright not installed. Run: pip install playwright && playwright install chromium")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not visible)
    context = browser.new_context()
    page = context.new_page()
    return pw, browser, page


def _screenshot(page, label: str) -> str:
    """Take screenshot, save to exports dir."""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(EXPORTS_DIR, f"poll-{label}-{ts}.png")
    page.screenshot(path=path, full_page=True)
    return path


# ===================================================================
# When2Meet
# ===================================================================

def _when2meet_read(page, url: str) -> dict:
    """Extract time slots from a When2Meet page."""
    page.goto(url, wait_until="networkidle")

    # Extract JS variables via page.evaluate()
    data = page.evaluate("""() => {
        const result = {
            timeOfSlot: typeof TimeOfSlot !== 'undefined' ? TimeOfSlot : [],
            eventName: '',
            timezone: ''
        };

        // Event name from the heading
        const h2 = document.querySelector('#NewEventNameID') ||
                    document.querySelector('h2') ||
                    document.querySelector('.event-name');
        if (h2) result.eventName = h2.textContent.trim();

        // Fallback to page title
        if (!result.eventName) {
            result.eventName = document.title.replace('When2Meet - ', '').trim();
        }

        return result;
    }""")

    time_of_slot = data.get("timeOfSlot", [])
    event_name = data.get("eventName", "Unknown Event")

    if not time_of_slot:
        _fail("Could not extract time slots from When2Meet page. "
              "The page may have changed structure.")

    # When2Meet timestamps are Unix epoch in the event's timezone.
    # The page displays times in the creator's timezone.
    # Convert to ISO datetimes.
    slots = []
    time_of_slot_sorted = sorted(time_of_slot)

    # Infer slot duration from gaps between consecutive timestamps
    if len(time_of_slot_sorted) >= 2:
        gaps = [time_of_slot_sorted[i+1] - time_of_slot_sorted[i]
                for i in range(min(10, len(time_of_slot_sorted) - 1))
                if time_of_slot_sorted[i+1] - time_of_slot_sorted[i] < 3600]
        slot_duration = min(gaps) if gaps else 900  # default 15 min
    else:
        slot_duration = 900

    slot_duration_minutes = slot_duration // 60

    for ts in time_of_slot_sorted:
        start_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        end_dt = start_dt + timedelta(seconds=slot_duration)
        slots.append({
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "slot_id": str(ts),
        })

    # Extract unique dates for summary
    dates = sorted(set(
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        for ts in time_of_slot_sorted
    ))

    return {
        "platform": "when2meet",
        "event_name": event_name,
        "url": url,
        "dates": dates,
        "slot_duration_minutes": slot_duration_minutes,
        "slots": slots,
    }


def _when2meet_fill(page, url: str, name: str, available_ids: list[str]) -> dict:
    """Fill availability on a When2Meet page and verify the save landed.

    A fill is only reported from SERVER state: client-side CSS the function
    applied itself would report success for a POST that never landed.

    Flow:
    1. Sign in (assigns UserID).
    2. POST to the real SaveTimes.php with the real params, AWAIT the
       response, and capture the HTTP status.
    3. Reload the page and verify UserID is present in AvailableAtSlot
       for the slots we claimed to fill. If verification fails, raise
       — do not report a successful fill from client-side state alone.
    """
    page.goto(url, wait_until="networkidle")

    # Sign in — required to get a UserID assigned by the server
    name_input = page.locator("#name")
    name_input.fill(name)
    sign_in = page.locator("input[type='button'][value='Sign In']")
    sign_in.click()
    page.wait_for_timeout(2000)

    # Build + POST + AWAIT server response, all in-page so we can reuse
    # the page's session (cookies/origin) for a same-origin POST.
    save_result = page.evaluate(
        """async (availableIds) => {
            const ids = new Set(availableIds.map(Number));
            const eventId = window.location.search.replace(/^\\?/, '').split('-')[0];
            const slotsToToggle = [];
            let binaryAvailability = "";
            let filled = 0;
            for (let i = 0; i < TimeOfSlot.length; i++) {
                const slot = TimeOfSlot[i];
                if (ids.has(slot)) {
                    binaryAvailability += "1";
                    slotsToToggle.push(slot);
                    filled++;
                    const cell = document.getElementById("YouTime" + slot);
                    if (cell) cell.style.backgroundColor = "#339933";
                } else {
                    binaryAvailability += "0";
                }
            }
            const passwordInput = document.getElementById('password');
            const params = new URLSearchParams({
                person: UserID,
                event: eventId,
                slots: slotsToToggle.join(","),
                availability: binaryAvailability,
                password: passwordInput ? passwordInput.value : "",
                ChangeToAvailable: "1",
            });
            const resp = await fetch("SaveTimes.php", {
                method: "POST",
                headers: {"Content-Type": "application/x-www-form-urlencoded"},
                body: params.toString(),
            });
            const body = await resp.text();
            return {
                filled_client: filled,
                total: TimeOfSlot.length,
                userId: UserID,
                eventId: eventId,
                slotsToToggle: slotsToToggle,
                httpStatus: resp.status,
                httpOk: resp.ok,
                responseBody: body.substring(0, 200),
            };
        }""",
        available_ids,
    )

    if not save_result.get("httpOk"):
        raise RuntimeError(
            "When2Meet SaveTimes.php returned status "
            f"{save_result.get('httpStatus')}. Body: "
            f"{save_result.get('responseBody', '')!r}. "
            "Fill NOT saved. Check whether W2M's API changed again."
        )

    # Server-side verification: reload the page so the client re-fetches
    # AvailableAtSlot / PeopleIDs from the server, then confirm our
    # UserID is recorded against at least the slots we said we filled.
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1000)
    verify = page.evaluate(
        """(params) => {
            const {userId, slotsToToggle} = params;
            if (typeof AvailableAtSlot === "undefined") {
                return {verified: false, reason: "AvailableAtSlot missing"};
            }
            const peopleIds = typeof PeopleIDs !== "undefined" ? PeopleIDs : [];
            if (!peopleIds.includes(userId)) {
                return {verified: false, reason:
                    "UserID " + userId + " not in PeopleIDs after save"};
            }
            let verifiedSlots = 0;
            for (const slot of slotsToToggle) {
                const idx = TimeOfSlot.indexOf(slot);
                if (idx < 0) continue;
                const at = AvailableAtSlot[idx] || [];
                if (at.includes(userId)) verifiedSlots++;
            }
            return {
                verified: verifiedSlots === slotsToToggle.length,
                verifiedSlots: verifiedSlots,
                expectedSlots: slotsToToggle.length,
            };
        }""",
        {"userId": save_result["userId"],
         "slotsToToggle": save_result["slotsToToggle"]},
    )

    screenshot_path = _screenshot(page, "when2meet-filled")

    if not verify.get("verified"):
        raise RuntimeError(
            "When2Meet save verification failed: "
            f"{verify.get('verifiedSlots', 0)}/{verify.get('expectedSlots', '?')} "
            f"slots found on server for UserID {save_result['userId']}. "
            f"Reason: {verify.get('reason', 'slot mismatch')}. "
            f"HTTP status was {save_result.get('httpStatus')}. "
            "Fill did not land — do NOT report success."
        )

    return {
        "filled": verify["verifiedSlots"],
        "total": len(available_ids),
        "screenshot": screenshot_path,
        "verified": True,
        "http_status": save_result["httpStatus"],
    }


# ===================================================================
# WhenIsGood
# ===================================================================

def _whenisgood_read(page, url: str) -> dict:
    """Extract time slots from a WhenIsGood page.

    WhenIsGood DOM structure:
      - Time grid cells: td.proposed with millisecond-timestamp IDs
        (e.g. id="1776060000000") and onmousedown="md(this);"
      - Date headers: td.dateHeader.weekday
      - Name field: input#name
      - Submit: input[name='sendResponse']
      - Hidden fields for selections: #myCanDos, #myCanDosBad, #myCanDosGood
    """
    # WhenIsGood respond URL ends with /results; the fill URL doesn't
    fill_url = re.sub(r"/results$", "", url)
    page.goto(fill_url, wait_until="networkidle")
    page.wait_for_timeout(2000)

    data = page.evaluate("""() => {
        const result = {
            eventName: document.title || '',
            slots: [],
        };

        // Clean up event name (strip "WhenIsGood - " prefix if present)
        result.eventName = result.eventName
            .replace(/^WhenIsGood\\s*[-:]\\s*/i, '')
            .trim();

        // Proposed time cells have class "proposed" and millisecond-timestamp IDs
        const cells = document.querySelectorAll('td.proposed');
        cells.forEach(cell => {
            const msId = cell.id;  // e.g. "1776060000000"
            if (msId && /^\\d+$/.test(msId)) {
                result.slots.push({
                    slot_id: msId,
                    ms_timestamp: parseInt(msId, 10),
                    text: cell.textContent.trim(),  // e.g. "6:00 am"
                });
            }
        });

        return result;
    }""")

    event_name = data.get("eventName", "Unknown Event")
    raw_slots = data.get("slots", [])

    if not raw_slots:
        _fail("Could not extract time slots from WhenIsGood page. "
              "No td.proposed cells found. The page may have changed structure.")

    # Convert millisecond timestamps to ISO datetimes.
    #
    # TIMEZONE HANDLING: WhenIsGood encodes the poll creator's local time
    # as if it were UTC. "9:00 AM Mountain" is stored as epoch for 09:00 UTC,
    # NOT 15:00 UTC. We detect this and reinterpret using the user's timezone
    # from scheduling-preferences.json, then convert to real UTC so the
    # scheduling engine can compare against actual calendar events.
    raw_slots.sort(key=lambda s: s["ms_timestamp"])

    # Infer slot duration from gaps between consecutive same-day timestamps
    if len(raw_slots) >= 2:
        gaps = []
        for i in range(min(20, len(raw_slots) - 1)):
            gap_ms = raw_slots[i + 1]["ms_timestamp"] - raw_slots[i]["ms_timestamp"]
            gap_min = gap_ms // 60000
            # Only consider small gaps (same day, not day-to-day jumps)
            if 0 < gap_min <= 60:
                gaps.append(gap_min)
        slot_duration_minutes = min(gaps) if gaps else 15
    else:
        slot_duration_minutes = 15

    # Load user's timezone for reinterpretation
    poll_tz = _get_user_timezone()

    slots = []
    dates_set = set()
    for s in raw_slots:
        ms = s["ms_timestamp"]
        # Step 1: Get the "fake UTC" datetime (actually local time)
        fake_utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        # Step 2: Reinterpret as the poll's local timezone
        local_naive = fake_utc.replace(tzinfo=None)
        local_aware = local_naive.replace(tzinfo=poll_tz)
        # Step 3: Convert to real UTC for calendar comparison
        real_utc = local_aware.astimezone(timezone.utc)

        end_dt = real_utc + timedelta(minutes=slot_duration_minutes)
        dates_set.add(local_aware.strftime("%Y-%m-%d"))
        slots.append({
            "start": real_utc.isoformat(),
            "end": end_dt.isoformat(),
            "slot_id": s["slot_id"],  # the millisecond timestamp string
            "local_time": local_aware.strftime("%a %b %d %I:%M %p"),
        })

    return {
        "platform": "whenisgood",
        "event_name": event_name,
        "url": url,
        "dates": sorted(dates_set),
        "slot_duration_minutes": slot_duration_minutes,
        "poll_timezone": str(poll_tz),
        "slots": slots,
    }


def _whenisgood_fill(page, url: str, name: str, available_ids: list[str]) -> dict:
    """Fill availability on a WhenIsGood page via direct form injection.

    Instead of simulating mouse events on each cell (slow, fragile),
    we inject the available slot IDs directly into WhenIsGood's hidden
    #myCanDos field and visually mark the cells as selected, then submit.

    WhenIsGood DOM contract:
      - #myCanDos: hidden input, comma-separated ms-timestamp IDs
      - td.proposed cells: each has id=<ms-timestamp>
      - Selected cells get class "good" added
      - input#name: respondent name
      - input[name='sendResponse']: submit button
    """
    fill_url = re.sub(r"/results$", "", url)
    page.goto(fill_url, wait_until="networkidle")
    page.wait_for_timeout(2000)

    # Enter respondent name
    name_field = page.locator("#name")
    if name_field.count() > 0:
        name_field.fill(name)

    # Inject available IDs into #myCanDos hidden field and mark cells
    ids_csv = ",".join(available_ids)
    filled = page.evaluate("""(idsCsv) => {
        const ids = idsCsv.split(',').filter(Boolean);
        let filled = 0;

        // Set the hidden field that WhenIsGood reads on submit
        const canDos = document.getElementById('myCanDos');
        if (canDos) {
            canDos.value = idsCsv;
        }

        // Visually mark cells as selected (class "good") so the
        // screenshot reflects the actual selection state
        ids.forEach(id => {
            const cell = document.getElementById(id);
            if (cell) {
                cell.classList.add('good');
                cell.classList.remove('bad');
                filled++;
            }
        });

        return filled;
    }""", ids_csv)

    page.wait_for_timeout(500)

    # Submit the form
    submit = page.locator("input[name='sendResponse']")
    if submit.count() > 0:
        submit.click()
        page.wait_for_timeout(3000)

    screenshot_path = _screenshot(page, "whenisgood-filled")

    return {
        "filled": filled,
        "total": len(available_ids),
        "screenshot": screenshot_path,
    }


# ===================================================================
# Doodle
# ===================================================================

def _doodle_dismiss_overlays(page):
    """Dismiss Doodle's cookie banner and any modal dialogs."""
    # Cookie consent banner
    try:
        accept = page.locator("text=Accept Cookies")
        if accept.count() > 0:
            accept.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass

    # Intro/welcome modal — Doodle shows a dialog with "Start voting" button
    try:
        start = page.locator("button:has-text('Start voting')")
        if start.count() > 0:
            start.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass

    # Generic modal close — fallback for any other modal overlay
    try:
        close = page.locator("dialog button:has-text('Close')").or_(
            page.locator("[data-testid='modal-backdrop']"))
        if close.count() > 0 and close.first.is_visible():
            # Try pressing Escape to dismiss
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception:
        pass


def _doodle_read(page, url: str) -> dict:
    """Extract options from a Doodle poll.

    Doodle DOM structure (group-poll):
      - Date headings: <h4> with class *section-date*
      - Time slot checkboxes: input[id^="time-slot-checkbox-"] with
        aria-label like "9:00 AM 1 h"
      - Each date section groups a heading + <ol> of slot <li> items
      - Timezone shown in page text: "<Country>, <Region>, <City> (GMT-6)"
      - Cookie consent banner blocks interaction until dismissed
    """
    # Doodle is a React SPA — networkidle never fires. Use domcontentloaded
    # and wait for the poll content to render.
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    _doodle_dismiss_overlays(page)

    data = page.evaluate("""() => {
        var result = {
            eventName: '',
            timezone: '',
            slots: [],
        };

        // Event title — first prominent heading
        var h1 = document.querySelector('h1');
        if (h1) result.eventName = h1.textContent.trim();

        // Timezone from page text (Doodle shows e.g. "(GMT-6)")
        var bodyText = document.body.innerText;
        var tzMatch = bodyText.match(/\\(GMT([+-]\\d+)\\)/);
        if (tzMatch) result.timezone = 'GMT' + tzMatch[1];

        // Walk the DOM in order, tracking the current date heading
        var currentDate = '';
        var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        var node;
        while (node = walker.nextNode()) {
            // Date headings
            if (node.tagName === 'H4' && node.textContent.match(/\\d{4}/)) {
                currentDate = node.textContent.trim();
            }
            // Time slot checkboxes
            if (node.tagName === 'INPUT' && node.id && node.id.startsWith('time-slot-checkbox-')) {
                var label = node.getAttribute('aria-label') || '';
                var durationMatch = label.match(/(\\d+)\\s*h/);
                var duration = durationMatch ? parseInt(durationMatch[1]) * 60 : 60;
                result.slots.push({
                    slot_id: node.id,
                    date: currentDate,
                    time_label: label.replace(/\\s*\\d+\\s*h\\s*$/, '').trim(),
                    duration_minutes: duration,
                });
            }
        }

        return result;
    }""")

    event_name = data.get("eventName", "Unknown Event")
    raw_slots = data.get("slots", [])

    if not raw_slots:
        _fail("Could not extract options from Doodle poll. "
              "The page may require authentication or has changed structure.")

    # Parse dates and times into ISO format
    poll_tz = _get_user_timezone()
    slots = []
    dates_set = set()
    duration_minutes = 60

    for s in raw_slots:
        date_str = s.get("date", "")
        time_label = s.get("time_label", "")
        duration_minutes = s.get("duration_minutes", 60)

        # Parse "Thursday, April 2, 2026" + "9:00 AM" into datetime
        try:
            dt_str = f"{date_str} {time_label}"
            # Handle various date formats Doodle might use
            for fmt in ["%A, %B %d, %Y %I:%M %p", "%A, %b %d, %Y %I:%M %p",
                        "%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p"]:
                try:
                    naive = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                # Could not parse — store raw display text
                slots.append({
                    "start": f"{date_str} {time_label}",
                    "end": "",
                    "slot_id": s["slot_id"],
                    "display": f"{date_str} {time_label}",
                })
                continue

            local_aware = naive.replace(tzinfo=poll_tz)
            real_utc = local_aware.astimezone(timezone.utc)
            end_utc = real_utc + timedelta(minutes=duration_minutes)
            dates_set.add(naive.strftime("%Y-%m-%d"))

            slots.append({
                "start": real_utc.isoformat(),
                "end": end_utc.isoformat(),
                "slot_id": s["slot_id"],
                "local_time": local_aware.strftime("%a %b %d %I:%M %p"),
            })
        except Exception:
            slots.append({
                "start": f"{date_str} {time_label}",
                "end": "",
                "slot_id": s["slot_id"],
                "display": f"{date_str} {time_label}",
            })

    return {
        "platform": "doodle",
        "event_name": event_name,
        "url": url,
        "dates": sorted(dates_set),
        "slot_duration_minutes": duration_minutes,
        "poll_timezone": str(poll_tz),
        "slots": slots,
    }


def _doodle_fill(page, url: str, name: str, available_ids: list[str]) -> dict:
    """Fill a Doodle poll by programmatically clicking available options.

    Doodle is a React app — we can't inject state directly because React
    reconciliation would overwrite it. However, we can batch the clicks
    efficiently by dispatching them all in rapid succession via evaluate(),
    then proceeding to name entry and submit.

    slot_ids are checkbox IDs like "time-slot-checkbox-e1WPBVNZ".
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    _doodle_dismiss_overlays(page)

    # Batch-click all available slots via a single evaluate call.
    # Doodle's React click handler is on the <li> ancestor of the checkbox.
    filled = page.evaluate("""(slotIds) => {
        let filled = 0;
        for (const id of slotIds) {
            const cb = document.getElementById(id);
            if (!cb) continue;
            // Find the <li> ancestor which is Doodle's React click target
            let li = cb.closest('li');
            if (li) {
                li.click();
            } else {
                cb.click();
            }
            filled++;
        }
        return filled;
    }""", available_ids)

    page.wait_for_timeout(1000)

    # Click "Continue" to proceed to name entry
    continue_btn = page.locator("button:has-text('Continue')")
    if continue_btn.count() > 0:
        continue_btn.click()
        page.wait_for_timeout(2000)

    # Enter name in the "Your name" field
    name_field = page.locator("input[placeholder*='name' i]").or_(
        page.locator("input[name='name']")).or_(
        page.locator("input[id*='name']"))
    if name_field.count() > 0:
        name_field.first.fill(name)

    # Enter email — Doodle requires email to submit votes
    email = _get_user_email()
    email_field = page.locator("input[placeholder*='email' i]").or_(
        page.locator("input[type='email']")).or_(
        page.locator("input[name='email']"))
    if email_field.count() > 0 and email:
        email_field.first.fill(email)

    _screenshot(page, "doodle-pre-submit")

    # Click "Submit vote"
    submit = page.locator("button:has-text('Submit vote')").or_(
        page.locator("button:has-text('Submit')")).or_(
        page.locator("button:has-text('Save')"))
    if submit.count() > 0:
        submit.first.click()
        page.wait_for_timeout(3000)

    screenshot_path = _screenshot(page, "doodle-filled")

    return {
        "filled": filled,
        "total": len(available_ids),
        "screenshot": screenshot_path,
    }


# ===================================================================
# Timeful (API-based — no browser needed)
# ===================================================================

def _timeful_event_id(url: str) -> str:
    """Extract event ID from a Timeful URL."""
    m = re.search(r"/e/([\w\-]+)", url)
    if not m:
        _fail(f"Could not extract event ID from Timeful URL: {url}")
    return m.group(1)


def _timeful_api(method: str, endpoint: str, body: dict | None = None) -> dict:
    """Call the Timeful REST API. Returns parsed JSON."""
    import urllib.request
    import urllib.error

    api_url = f"https://timeful.app/api{endpoint}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "EA-System/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        _fail(f"Timeful API {e.code}: {body_text[:200]}")
    except urllib.error.URLError as e:
        _fail(f"Timeful API connection error: {e.reason}")


def _timeful_read(url: str) -> dict:
    """Read a Timeful scheduling poll via REST API.

    Timeful (formerly Schej) stores events with:
      - dates: array of ISO datetime strings (one per day)
      - startTime / endTime: floats representing hours (e.g. 9.0, 17.5)
      - duration: slot granularity in minutes (15, 30, or 60)
      - timezone: IANA timezone string
    Availability is submitted as an array of UTC ISO timestamps.
    """
    event_id = _timeful_event_id(url)
    event = _timeful_api("GET", f"/events/{event_id}")

    event_name = event.get("name", "Unknown Event")
    raw_dates = event.get("dates", [])
    start_time = float(event.get("startTime", 9))
    end_time = float(event.get("endTime", 17))
    duration = int(event.get("duration", 30))
    tz_name = event.get("timezone", "")

    try:
        event_tz = ZoneInfo(tz_name) if tz_name else _get_user_timezone()
    except (KeyError, ValueError):
        event_tz = _get_user_timezone()

    # Parse dates — could be ISO strings, ms timestamps, or {"$date": "..."} objects
    dates = []
    for d in raw_dates:
        if isinstance(d, dict):
            d = d.get("$date", d.get("date", ""))
        if isinstance(d, (int, float)):
            ms = d if d > 1e12 else d * 1000
            utc_dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            local_dt = utc_dt.astimezone(event_tz)
            dates.append(local_dt.strftime("%Y-%m-%d"))
        elif isinstance(d, str) and len(d) > 10:
            # Full ISO string — convert to event tz to get correct local date
            utc_dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(event_tz)
            dates.append(local_dt.strftime("%Y-%m-%d"))
        elif isinstance(d, str):
            dates.append(d[:10])

    if not dates:
        _fail("Timeful event has no dates")

    # Generate time slots for each date
    slots = []
    dates_set = set()

    for date_str in sorted(set(dates)):
        base = datetime.strptime(date_str, "%Y-%m-%d")
        dates_set.add(date_str)

        cur_min = int(start_time * 60)
        end_min = int(end_time * 60)

        while cur_min < end_min:
            h, m = divmod(cur_min, 60)
            local_dt = base.replace(hour=h, minute=m, tzinfo=event_tz)
            utc_dt = local_dt.astimezone(timezone.utc)
            end_dt = utc_dt + timedelta(minutes=duration)

            # Timeful expects UTC ISO timestamps as availability identifiers
            slot_id = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            slots.append({
                "start": utc_dt.isoformat(),
                "end": end_dt.isoformat(),
                "slot_id": slot_id,
                "local_time": local_dt.strftime("%a %b %d %I:%M %p"),
            })

            cur_min += duration

    return {
        "platform": "timeful",
        "event_name": event_name,
        "url": url,
        "dates": sorted(dates_set),
        "slot_duration_minutes": duration,
        "poll_timezone": str(event_tz),
        "slots": slots,
    }


def _timeful_fill(url: str, name: str, available_ids: list[str]) -> dict:
    """Fill a Timeful poll via REST API (guest submission).

    available_ids are UTC ISO timestamp strings (the slot_id values from read).
    """
    event_id = _timeful_event_id(url)

    payload = {
        "availability": available_ids,
        "ifNeeded": [],
        "guest": True,
        "name": name,
    }

    email = _get_user_email()
    if email:
        payload["email"] = email

    _timeful_api("POST", f"/events/{event_id}/response", payload)

    return {
        "filled": len(available_ids),
        "total": len(available_ids),
        "screenshot": None,
    }


# ===================================================================
# Framadate (BETA / v2 — server-rendered Symfony, browser-filled)
# ===================================================================
#
# The current Framadate rewrite (beta.framadate.org/polls/<hash>) renders a
# classic HTML vote form: one column per date option, three radios per column
# (yes / maybe / no) named vote[answers][<N>][value], a vote[authorName] field,
# and a CSRF vote[_token]. Crucially, the answer index N does NOT match column
# order — N is assigned per option id, so we align each radio group to its
# header by the cell's position in the row. Getting this wrong votes on the
# wrong day. There is no public JSON API, so we drive it in a browser; the
# CSRF token and PHP session are handled by the browser for free.

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _framadate_parse_label(label: str, poll_tz):
    """Parse a Framadate column header like
    'Wed 12 Aug 2026 12-5 (Lunch 12-1, meeting 1-5)' into ISO start/end.

    Times are interpreted as afternoon (PM) unless clearly morning. Prefers a
    'meeting X-Y' range if present, else the first 'X-Y' range, else a 12-17
    default. Returns (start_iso, end_iso, date_str) or (None, None, None) if the
    date cannot be parsed (caller keeps the raw label as the display).
    """
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", label)
    if not m:
        return None, None, None
    day, mon_abbr, year = int(m.group(1)), m.group(2)[:3].title(), int(m.group(3))
    month = _MONTHS.get(mon_abbr)
    if not month:
        return None, None, None

    def _to_24h(h):
        # Afternoon poll: treat 1..7 as PM, keep 8..12 as-is (noon stays 12).
        return h + 12 if 1 <= h <= 7 else h

    rng = re.search(r"meeting\s+(\d{1,2})\s*-\s*(\d{1,2})", label, re.I) \
        or re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", label)
    if rng:
        sh, eh = _to_24h(int(rng.group(1))), _to_24h(int(rng.group(2)))
    else:
        sh, eh = 12, 17
    try:
        start = datetime(year, month, day, sh, 0, tzinfo=poll_tz)
        end = datetime(year, month, day, eh, 0, tzinfo=poll_tz)
    except ValueError:
        return None, None, f"{year:04d}-{month:02d}-{day:02d}"
    return (start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            f"{year:04d}-{month:02d}-{day:02d}")


def _framadate_read(page, url: str) -> dict:
    """Extract options from a Framadate BETA poll.

    slot_id is the option's answer index (the N in vote[answers][N]), as a
    string. Options are aligned to their date header by column position, NOT by
    N, because Framadate does not order N by column.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    data = page.evaluate(r"""() => {
        const out = {title: '', slots: []};
        const h1 = document.querySelector('h1');
        if (h1) out.title = h1.textContent.trim();

        // Column label per cell position, from the vote table header row.
        const headers = {};
        document.querySelectorAll('table thead th, table thead td').forEach((th, i) => {
            headers[i] = (th.getAttribute('aria-label') || th.textContent || '')
                .replace(/\s+/g, ' ').trim();
        });

        // One entry per answer group, aligned to its column by cell position.
        const seen = {};
        document.querySelectorAll('input[type=radio][name^="vote[answers]"]').forEach(inp => {
            const m = inp.name.match(/vote\[answers\]\[(\d+)\]/);
            if (!m) return;
            const idx = m[1];
            if (seen[idx] !== undefined) return;
            seen[idx] = 1;
            let cell = inp.closest('td,th');
            let colIndex = -1;
            if (cell) {
                const row = cell.closest('tr');
                colIndex = Array.from(row.children).indexOf(cell);
            }
            out.slots.push({slot_id: idx, colIndex: colIndex,
                            label: headers[colIndex] || ''});
        });
        return out;
    }""")

    raw_slots = data.get("slots", [])
    if not raw_slots:
        _fail("Could not extract options from Framadate poll. The page may have "
              "changed structure or the poll is closed/private.")

    poll_tz = _get_user_timezone()
    slots = []
    dates_set = set()
    # Keep slots in visual column order for readability.
    for s in sorted(raw_slots, key=lambda x: x.get("colIndex", 0)):
        label = s.get("label", "")
        start_iso, end_iso, date_str = _framadate_parse_label(label, poll_tz)
        if date_str:
            dates_set.add(date_str)
        slots.append({
            "slot_id": s["slot_id"],
            "start": start_iso or "",
            "end": end_iso or "",
            "date": date_str or "",
            "label": label,
        })

    return {
        "platform": "framadate",
        "event_name": data.get("title", "Unknown Event"),
        "url": url,
        "dates": sorted(dates_set),
        "poll_timezone": str(poll_tz),
        "answer_states": ["yes", "no"],
        "slots": slots,
    }


def _framadate_fill(page, url: str, name: str, available_ids: list[str]) -> dict:
    """Fill a Framadate BETA poll.

    available_ids are the answer-index slot_ids to mark 'yes'; every other
    option is marked 'no' (Framadate requires a value in each column, and needs
    at least one selection). The browser carries the CSRF token and session, so
    submitting the form is enough.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    avail = {str(x) for x in available_ids}

    all_idx = page.evaluate(r"""() => {
        const s = new Set();
        document.querySelectorAll('input[type=radio][name^="vote[answers]"]').forEach(i => {
            const m = i.name.match(/vote\[answers\]\[(\d+)\]/);
            if (m) s.add(m[1]);
        });
        return Array.from(s);
    }""")

    if not all_idx:
        _fail("No vote options found on the Framadate page.")

    unknown = avail - set(all_idx)
    if unknown:
        _fail(f"available slot_ids not present on poll: {sorted(unknown)}. "
              f"Poll has: {sorted(all_idx)}")

    # Framadate renders custom-styled radios inside a horizontally-scrolling
    # box that intercepts synthetic clicks, so set the radio state (and name)
    # directly in the DOM and fire change/input events. The checked radio is
    # exactly what the form POSTs, and the events keep the Stimulus validation
    # controller in sync.
    res = page.evaluate(
        """(args) => {
            const avail = new Set(args.avail);
            const groups = {};
            document.querySelectorAll('input[type=radio][name^="vote[answers]"]').forEach(inp => {
                const m = inp.name.match(/vote\\[answers\\]\\[(\\d+)\\]\\[value\\]/);
                if (!m) return;
                (groups[m[1]] = groups[m[1]] || []).push(inp);
            });
            let filled = 0, total = 0, missing = [];
            for (const idx of Object.keys(groups)) {
                total++;
                const want = avail.has(idx) ? 'yes' : 'no';
                const inp = groups[idx].find(i => i.value === want);
                if (!inp) { missing.push(idx + ':' + want); continue; }
                inp.checked = true;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                if (want === 'yes') filled++;
            }
            const nameInp = document.querySelector('input[name="vote[authorName]"]');
            let nameSet = false;
            if (nameInp) {
                nameInp.value = args.name;
                nameInp.dispatchEvent(new Event('input', {bubbles: true}));
                nameInp.dispatchEvent(new Event('change', {bubbles: true}));
                nameSet = true;
            }
            return {filled, total, missing, nameSet};
        }""",
        {"avail": list(avail), "name": name},
    )
    if res.get("missing"):
        _fail(f"Could not find radios for: {res['missing']}")
    if not res.get("nameSet"):
        _fail("Framadate author-name field not found.")
    filled = res["filled"]

    _screenshot(page, "framadate-pre-submit")

    # Click the real submit button (its Stimulus action disables the
    # leave-confirmation guard and runs vote validation). force=True because
    # the scrollbox can still intercept a normal click.
    page.locator('button[name="vote[submit]"]').first.click(force=True, timeout=5000)
    page.wait_for_timeout(3000)

    # Verify: after a successful vote Framadate re-renders the poll with the
    # new participant row containing the author's name.
    body_text = page.evaluate("() => document.body.innerText")
    verified = name in (body_text or "")
    screenshot_path = _screenshot(page, "framadate-filled")

    return {
        "filled": filled,
        "total": len(all_idx),
        "marked_no": len(all_idx) - filled,
        "verified": verified,
        "screenshot": screenshot_path,
    }


PLATFORM_READERS = {
    "when2meet": _when2meet_read,
    "whenisgood": _whenisgood_read,
    "doodle": _doodle_read,
    "framadate": _framadate_read,
}

PLATFORM_FILLERS = {
    "when2meet": _when2meet_fill,
    "whenisgood": _whenisgood_fill,
    "doodle": _doodle_fill,
    "framadate": _framadate_fill,
}

# Calendly meeting polls (poll_calendly.py): the read is two public GETs,
# the fill is a browser that stops before the vote unless --go is given.
import poll_calendly  # noqa: E402

FILL_DRY_RUN = True   # cmd_fill_poll sets it from --go


def _calendly_read(url: str) -> dict:
    try:
        return poll_calendly.read(url, user_tz=_get_user_timezone())
    except poll_calendly.CalendlyError as e:
        _fail(str(e))


def _calendly_fill(page, url: str, name: str, available_ids: list[str]) -> dict:
    return poll_calendly.fill(page, url, name, available_ids, email=_get_user_email(),
                              dry_run=FILL_DRY_RUN, screenshot=lambda label: _screenshot(page, label))


PLATFORM_FILLERS["calendly"] = _calendly_fill

# API-based platforms (no browser needed, different function signatures)
API_READERS = {
    "timeful": _timeful_read,
    "calendly": _calendly_read,
}

API_FILLERS = {
    "timeful": _timeful_fill,
}


def cmd_read_poll(args):
    """Read a scheduling poll and output all time slots as JSON."""
    url = _validate_url(args.url)
    platform = _detect_platform(url)
    outbound_log.note("fetch", "poll_filler", url=url, method="read-poll",
                      platform=platform, poll_id=_poll_id(url))

    if platform in API_READERS:
        result = API_READERS[platform](url)
    else:
        pw, browser, page = _launch_browser(visible=args.visible)
        try:
            result = PLATFORM_READERS[platform](page, url)
        finally:
            browser.close()
            pw.stop()

    print(json.dumps(result, indent=2))


def cmd_fill_poll(args):
    """Fill a scheduling poll with specified available slots.

    Priority: --available-file > --available (argparse enforces that
    exactly one is provided). The file form is strongly preferred when
    the slot list comes from another tool (e.g., scheduling_engine) —
    it eliminates a whole class of hand-transcription bugs where the
    agent rebuilds the JSON array inline and silently drops a slot.
    """
    url = _validate_url(args.url)
    platform = _detect_platform(url)
    name = _pinned_name(getattr(args, "name", None))

    if getattr(args, "available_file", None):
        with open(args.available_file) as f:
            raw = f.read().strip()
        available = json.loads(raw)
        if not isinstance(available, list):
            _fail(f"--available-file must contain a JSON array, got "
                  f"{type(available).__name__}")
    else:
        available = json.loads(args.available)

    if not available:
        _fail("No available slots provided")

    sent = {"name": name, "available": available}
    outcome = "error"
    global FILL_DRY_RUN
    FILL_DRY_RUN = not getattr(args, "go", False)
    try:
        if platform in API_FILLERS:
            result = API_FILLERS[platform](url, name, available)
        else:
            pw, browser, page = _launch_browser(visible=args.visible)
            try:
                result = PLATFORM_FILLERS[platform](page, url, name, available)
            finally:
                browser.close()
                pw.stop()
        outcome = "ok" if not (isinstance(result, dict) and result.get("error")) else "error"
    finally:
        # The record goes on whether or not the site took the answer: an
        # attempt has already sent the fields.
        outbound_log.note(
            "poll_submit", "poll_filler", url=url, platform=platform,
            poll_id=_poll_id(url), outcome=outcome, fields=sorted(sent),
            name=name, name_chars=len(name), slots=len(available),
            bytes=len(json.dumps(sent)))

    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Read and fill scheduling polls (When2Meet, WhenIsGood, Doodle, Framadate, Timeful)",
        allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # read-poll
    rp = sub.add_parser("read-poll", help="Extract time slots from a poll", allow_abbrev=False)
    rp.add_argument("--url", required=True, help="Poll URL")
    rp.add_argument("--visible", action="store_true",
                    help="Show browser window (for debugging)")
    rp.set_defaults(func=cmd_read_poll)

    # fill-poll — accepts --available (inline JSON) OR --available-file
    # (path to a file containing a JSON array). Prefer the file form;
    # see the skills/fill-poll.md rule about hand-transcription errors.
    fp = sub.add_parser("fill-poll", help="Fill availability on a poll", allow_abbrev=False)
    fp.add_argument("--url", required=True, help="Poll URL")
    fp.add_argument("--name", default=None,
                    help="The user's configured display name (the default); any other "
                         "name is refused")
    avail_group = fp.add_mutually_exclusive_group(required=True)
    avail_group.add_argument("--available",
                             help="JSON array of slot_ids (inline). Error-prone "
                                  "for >5 slots — prefer --available-file.")
    avail_group.add_argument("--available-file",
                             help="Path to a file containing a JSON array of "
                                  "slot_ids. Use when the list comes from "
                                  "scheduling_engine or another tool.")
    fp.add_argument("--visible", action="store_true",
                    help="Show browser window (for debugging)")
    fp.add_argument("--go", action="store_true",
                    help="Calendly: submit the vote. Without it the fill selects the slots, "
                         "fills the form, takes a screenshot and stops before the submit.")
    fp.set_defaults(func=cmd_fill_poll)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
