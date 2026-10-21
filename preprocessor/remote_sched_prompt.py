#!/usr/bin/env python3
"""remote_sched_prompt: the scheduling lane's prompt for the on-prem arm.

The lane prompt Claude gets is 50 to 60k characters: the operating
instructions, the profile, the whole scheduling-rules skill, the
staged-events guide, the user's rules and the request. A 27B model
loses the request in it. The on-prem arm gets `compose()`: the compact
instructions in prompts/CLAUDE-scheduling-remote.md (the exact commands, the
rules that decide a turn, the staged-event shape, the id handles),
the profile, the user's scheduling rules, and the request. About a
fifth of the length.

`from_lane_prompt()` rebuilds the compact prompt from a recorded lane
prompt (the replay): the profile and the request are taken from their
markers, the user rules from theirs.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REMOTE_FILE = REPO / "prompts/CLAUDE-scheduling-remote.md"
PROFILE_START, PROFILE_END = "=== USER PROFILE ===", "=== END USER PROFILE ==="
RULES_HEAD = "## User rules"
REQUEST_MARK = "The user posted"
PROFILE_KEYS = ("name", "email", "role", "department", "timezone", "institution", "title",
                "zoom_link", "calendar_accounts", "preferred_name")


def _profile_short(profile_text: str) -> str:
    """The profile's identity lines only (a JSON profile reduced to the
    keys a scheduling turn uses; other text as it is, capped)."""
    import json
    try:
        d = json.loads(profile_text)
    except (TypeError, ValueError):
        return str(profile_text or "")[:1500]
    if not isinstance(d, dict):
        return str(profile_text or "")[:1500]
    keep = {k: d[k] for k in PROFILE_KEYS if k in d}
    return json.dumps(keep, indent=1)


# Friendly names for the zones the instructions quote times in.
_ZONE_NAMES = {
    "America/Denver": "Mountain time", "America/Boise": "Mountain time",
    "America/Phoenix": "Arizona time", "America/Chicago": "Central time",
    "America/New_York": "Eastern time", "America/Los_Angeles": "Pacific time",
    "America/Anchorage": "Alaska time", "Pacific/Honolulu": "Hawaii time",
    "Europe/London": "UK time",
}


def local_zone_label() -> str:
    """The user's zone as the instructions name it ("Mountain time"), from
    scheduling-preferences.json, else user_tz.py (the profile, then the
    system's zone)."""
    import json
    import os
    ea_dir = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
    try:
        tz = json.loads((ea_dir / "config" / "scheduling-preferences.json").read_text()).get("timezone", "")
    except (OSError, ValueError, AttributeError):
        tz = ""
    if not tz:
        try:
            try:
                import user_tz
            except ImportError:
                from preprocessor import user_tz
            tz = user_tz.name(ea_dir)
        except Exception:
            return "the user's local time"
    return _ZONE_NAMES.get(tz, f"{tz} time")


def video_platform_label() -> str:
    """The institution's video platform ("Zoom"), from the profile."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        return institution.video_platform() or "the user's video platform"
    except Exception:
        return "the user's video platform"


def fill(instructions: str) -> str:
    """The instructions with this install's zone and video platform."""
    return (instructions.replace("{local_zone}", local_zone_label())
            .replace("{video_platform}", video_platform_label()))


def compose(profile_text: str, user_rules: str, request: str, *, instructions: str | None = None,
            today: str = "") -> str:
    """The compact prompt: instructions, profile, rules, request."""
    instructions = fill(instructions if instructions is not None else REMOTE_FILE.read_text())
    parts = [instructions.strip()]
    if today:
        parts.append(f"Today is {today}.")
    parts.append(f"{PROFILE_START}\n{_profile_short(profile_text)}\n{PROFILE_END}")
    if user_rules and user_rules.strip():
        parts.append(user_rules.strip()[:4000])
    parts.append(request.strip())
    return "\n\n".join(parts) + "\n"


def from_lane_prompt(lane_prompt: str, *, instructions: str | None = None) -> str:
    """The compact prompt for a recorded lane prompt."""
    p = lane_prompt or ""
    i, j = p.find(PROFILE_START), p.find(PROFILE_END)
    profile = p[i + len(PROFILE_START):j].strip() if 0 <= i < j else ""
    k = p.rfind(REQUEST_MARK)
    request = p[k:].strip() if k >= 0 else p[-3000:]
    r = p.find(RULES_HEAD)
    rules = ""
    if 0 <= r < (k if k >= 0 else len(p)):
        block = p[r:k if k >= 0 else len(p)]
        # The lane prompt's tools block follows the rules; cut it off.
        for stop in ("\nTools available:", "\n- Bash(", "\nTOOLS"):
            block = block.split(stop, 1)[0]
        rules = block.strip()[:4000]
    m = re.search(r"Today(?:'s date)? is ([A-Za-z]+,? [A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})", p)
    return compose(profile, rules, request, instructions=instructions, today=m.group(1) if m else "")
