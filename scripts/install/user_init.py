#!/usr/bin/env python3
"""
EA System – User Initialization Script

Interactive setup for new users. Captures preferences, authenticates
with email/calendar services, and optionally analyzes writing style
from sent mail to build per-contact communication profiles.

Usage:
    python3 scripts/install/user_init.py                     # Full interactive setup
    python3 scripts/install/user_init.py --update            # Re-run on existing config
    python3 scripts/install/user_init.py --style-only        # Re-run writing style detection
    python3 scripts/install/user_init.py --phase tone --out FILE
                                             # The style baseline to FILE, not the
                                             # config dir (the container's owner side)
    python3 scripts/install/user_init.py --phase scheduling  # Run a single phase
    python3 scripts/install/user_init.py --ea-dir /tmp/test  # Override EA directory
    python3 scripts/install/user_init.py --chat zulip --zulip-site https://chat.example.edu \
        --zulip-bot-email ea-bot@chat.example.edu --zulip-owner-id 8
                                             # Talk to the EA over Zulip
    python3 scripts/install/user_init.py --answers tests/install/answers-email.json
                                             # Every answer from a file; no prompt
                                             # reads the terminal (install tests)

This script does NOT modify an existing EA setup unless --update is passed.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_DIR = EA_DIR / "config"
STATE_DIR = EA_DIR / "state"
AUTH_DIR = EA_DIR / "auth"

ALL_PHASES = [
    "identity", "scheduling", "inbox", "autonomy", "groups",
    "deadlines", "style", "auth", "tone", "reimbursement",
    "vip", "preferences", "push", "state", "summary",
]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

# Scripted answers (--answers FILE). When the file is given, every prompt
# takes its answer from it by key and input() is never called: an absent
# key means the prompt's default (what Enter gives), an empty value the
# same, and a required prompt with no default and no key stops the run
# and names the key. The keys are the explicit `key` of each prompt;
# docs/install-testing.md lists them, tests/install/answers-email.json
# is a complete example. Keys the run looked up go in ANSWERS_USED.
ANSWERS: dict | None = None
ANSWERS_USED: set[str] = set()


def answer_key(question: str) -> str:
    """A key from a question's text, for a prompt with no explicit key."""
    return re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")


def _scripted(key: str):
    """(found, value) from the answers file; (False, None) when the run
    is interactive or the key is absent."""
    if ANSWERS is None:
        return False, None
    ANSWERS_USED.add(key)
    if key in ANSWERS:
        return True, ANSWERS[key]
    return False, None


def _read(display: str) -> str:
    """One line from the terminal. With no terminal (Claude Code's Bash
    tool, a pipe) input() meets end of file: the run stops with the way
    to give the answers instead of a traceback."""
    try:
        return input(display)
    except EOFError:
        raise SystemExit(f"\nuser_init.py: no terminal to ask {display.strip()!r} on. "
                         "Run it in a terminal, or give every answer in a file: "
                         "--answers FILE (tests/install/answers-email.json is an example)")


def prompt(question: str, default: str = "", required: bool = False,
           key: str | None = None) -> str:
    """Ask a question with optional default. Returns stripped input."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        found, value = _scripted(key)
        answer = "" if value is None else str(value).strip()
        if answer:
            return answer
        if default:
            return default
        if required:
            raise SystemExit(f"--answers: '{key}' ({question}) is required and "
                             f"{'empty' if found else 'missing'} in the answers file")
        return ""
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "
    while True:
        answer = _read(display).strip()
        if not answer:
            if default:
                return default
            if required:
                print("  This field is required.")
                continue
            return ""
        return answer


def prompt_yn(question: str, default: bool = True, key: str | None = None) -> bool:
    """Ask a yes/no question."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        found, value = _scripted(key)
        if not found or value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower().startswith("y")
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _read(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def prompt_list(question: str, example: str = "", key: str | None = None) -> list[str]:
    """Ask for a comma-separated list."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        found, value = _scripted(key)
        if not found or value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raw = str(value).strip()
    else:
        hint = f" (e.g., {example})" if example else ""
        raw = _read(f"{question}{hint}: ").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def prompt_time(question: str, default: str = "09:00", key: str | None = None) -> str:
    """Ask for a time in HH:MM format."""
    while True:
        answer = prompt(question, default, key=key)
        if re.match(r"^\d{1,2}:\d{2}$", answer):
            return answer
        if ANSWERS is not None:
            raise SystemExit(f"--answers: '{key or answer_key(question)}' must be HH:MM, "
                             f"not {answer!r}")
        print("  Please enter time as HH:MM (e.g., 09:00, 17:30)")


def private_dir() -> Path:
    """The owner-private directory (EA_PRIVATE_DIR, else `.ea-private` in
    this user's passwd home), the way preprocessor/owner_trust.py finds it."""
    import pwd
    if os.environ.get("EA_PRIVATE_DIR"):
        return Path(os.environ["EA_PRIVATE_DIR"])
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea-private"


def private_secrets_file() -> Path:
    """The owner-private secrets.env, its directory made 0700 first."""
    priv = private_dir()
    priv.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(priv, 0o700)
    return priv / "secrets.env"


def load_existing(filename: str) -> dict:
    """Load existing config file if it exists, for --update mode."""
    path = CONFIG_DIR / filename
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def template_values(filename: str) -> dict:
    """The shipped template of a config file (config/<filename> in this
    checkout), or {} when there is none."""
    try:
        data = json.loads((Path(__file__).resolve().parents[2] / "config" / filename).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def without_template(existing: dict, template: dict) -> dict:
    """`existing` less every value still equal to the template's: scripts/install/setup.sh
    copies the templates first, and a placeholder ("Your Name", the
    example role) must never be offered as an answer's default."""
    out = {}
    for k, v in (existing or {}).items():
        t = template.get(k, None) if isinstance(template, dict) else None
        if isinstance(v, dict) and isinstance(t, dict):
            out[k] = without_template(v, t)
        elif k in template and v == t:
            continue
        else:
            out[k] = v
    return out


def save_config(filename: str, data: dict):
    """Save config JSON to EA_DIR/config/."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / filename
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  -> Saved {path}")


def merge_env_file(path: Path, data: dict, mode: int = 0o600):
    """Merge key=value pairs into an env file, preserving existing keys/order."""
    existing: dict = {}
    order: list = []
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                if k not in existing:
                    order.append(k)
                existing[k] = v.strip()
    for k, v in data.items():
        if k not in existing:
            order.append(k)
        existing[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={existing[k]}" for k in order) + "\n")
    path.chmod(mode)
    print(f"  -> Saved {path}")


def chat_block(frontend: str, site: str = "", bot_email: str = "",
               owner_id: str = "") -> dict | None:
    """The `chat` block for ea-config.json, or None for Slack (the default
    block in ea_config already means Slack, so nothing is written)."""
    if frontend != "zulip":
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
    import ea_config
    notify = ea_config.DEFAULTS["chat"]["zulip"]["notify"]
    return {
        "frontend": "zulip",
        "zulip": {
            "site": site.rstrip("/"),
            "bot_email": bot_email,
            "owner_user_id": str(owner_id),
            "stream": "ea",
            "sched_stream": "ea-scheduling",
            "dm_stream": "ea-dm",
            "notify": {k: dict(v) for k, v in notify.items()},
        },
    }


def write_chat_config(block: dict):
    """Merge the chat block into ea-config.json, keeping every other key."""
    data = load_existing("ea-config.json")
    data["chat"] = block
    save_config("ea-config.json", data)


def section_header(title: str):
    """Print a visible section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def detect_system_timezone() -> str:
    """Detect the system timezone, return IANA name or fallback."""
    import subprocess
    # Try macOS systemsetup (Darwin only)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["systemsetup", "-gettimezone"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Output: "Time Zone: America/Chicago"
                parts = result.stdout.strip().split(":", 1)
                if len(parts) == 2 and parts[1].strip():
                    return parts[1].strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # Try timedatectl (Linux/systemd)
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Try /etc/timezone (Debian/Ubuntu)
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        tz = tz_file.read_text().strip()
        if tz:
            return tz
    # Try TZ env var
    tz_env = os.environ.get("TZ", "")
    if tz_env:
        return tz_env
    return "America/New_York"


# ---------------------------------------------------------------------------
# Phase 1: Identity
# ---------------------------------------------------------------------------

def phase_identity(existing: dict = None) -> dict:
    """Collect user identity and profile information."""
    section_header("Phase 1: Identity & Profile")

    print("  Basic information about you. This is used for drafting emails,")
    print("  scheduling, and letter composition.\n")

    # Defaults come from what the user wrote, never from the template's
    # placeholders; the template's other keys are kept in the merge below.
    kept = existing or {}
    ex = without_template(kept, template_values("user-profile.json"))
    profile = {
        "name": prompt("Full name", ex.get("name", ""), required=True, key="full-name"),
        "email": prompt("Primary work email", ex.get("email", ""), required=True,
                        key="work-email"),
        "role_description": prompt(
            "Role description (title, focus area)",
            ex.get("role_description", ""), key="role-description",
        ),
        "department": prompt("Department", ex.get("department", ""), key="department"),
        "university": prompt("University/Organization", ex.get("university", ""),
                             key="university"),
        "office": prompt("Office location (optional)", ex.get("office", ""), key="office"),
        "slack_channel_name": prompt(
            "Slack channel name for EA communication",
            ex.get("slack_channel_name", "ea-assistant"), key="slack-channel-name",
        ),
        "bio_blurb": prompt(
            "Brief professional bio (for letter drafting, optional)",
            ex.get("bio_blurb", ""), key="bio-blurb",
        ),
        "calendar_accounts": prompt_list(
            "Google Calendar email(s), comma-separated",
            "you@gmail.com, shared@example.com", key="calendar-accounts",
        ) or ex.get("calendar_accounts", []),
    }
    profile["institution"] = institution_block(profile, ex)
    # The Create button reads the top-level link when it adds the meeting
    # link to an event; the institution block carries the same value.
    profile["zoom_link"] = profile["institution"]["video"]["link"]

    # Keys the template carries that this phase does not ask about
    # (gmail_accounts, family, the notes) stay as the existing file has them.
    merged = dict(kept)
    merged.update(profile)
    save_config("user-profile.json", merged)
    return merged


def institution_block(profile: dict, existing: dict) -> dict:
    """The `institution` block of user-profile.json (preprocessor/
    institution.py documents every key), asked after the identity
    questions. The mail domain of the work address is the first internal
    domain and the safety-rail domain unless the user says otherwise."""
    print("\n  Your institution. CLAUDE.md names none; these answers are rendered")
    print("  into the install overlay that every session carries.\n")
    ex = existing.get("institution") if isinstance(existing.get("institution"), dict) else {}
    own_domain = profile["email"].rsplit("@", 1)[-1].lower() if "@" in profile["email"] else ""
    name = prompt("Institution name", ex.get("name") or profile.get("university", ""),
                  key="institution-name")
    domains = prompt_list(
        "Mail domains that count as internal, comma-separated",
        own_domain or "example.edu", key="institution-domains",
    ) or list(ex.get("domains") or ([own_domain] if own_domain else []))
    related = prompt_list(
        "Sister-campus domains the admissions filter treats as internal (optional)",
        "med.example.edu", key="institution-related-domains",
    ) or list(ex.get("related_domains") or [])
    rail = prompt(
        "Safety-rail domain (a first email outside it is never sent on its own)",
        ex.get("safety_rail_domain") or (domains[0] if domains else ""),
        key="institution-safety-rail-domain",
    )
    ex_video = ex.get("video") if isinstance(ex.get("video"), dict) else {}
    platform = prompt("Video-meeting platform", ex_video.get("platform") or "Zoom",
                      key="video-platform")
    link = prompt("Your personal meeting link on it (optional)",
                  ex_video.get("link") or existing.get("zoom_link", ""), key="video-link")
    block = {
        "name": name,
        "domains": [d.lower().lstrip("@") for d in domains],
        "related_domains": [d.lower().lstrip("@") for d in related],
        "safety_rail_domain": rail.lower().lstrip("@"),
        "video": {"platform": platform, "link": link},
        "boilerplate_tokens": list(ex.get("boilerplate_tokens") or []),
        "boilerplate_phrases": list(ex.get("boilerplate_phrases") or []),
        "letterhead": ex.get("letterhead") if isinstance(ex.get("letterhead"), dict) else {
            "template_dir": "~/.ea/templates/letterhead",
            "asset_dirs": [],
            "required_assets": [],
        },
    }
    return block


# ---------------------------------------------------------------------------
# Phase 2: Scheduling Preferences
# ---------------------------------------------------------------------------

def phase_scheduling(existing: dict = None) -> dict:
    """Collect scheduling preferences."""
    section_header("Phase 2: Scheduling Preferences")

    print("  These control how the EA proposes meeting times, protects your")
    print("  focus blocks, and handles VIP contacts.\n")

    kept = existing or {}
    ex = kept
    ex_slot = ex.get("slot_selection", {})
    ex_wh = ex.get("working_hours", {})

    detected_tz = detect_system_timezone()
    tz = prompt("Timezone (IANA format)", ex.get("timezone") or detected_tz, key="timezone")

    print("  Working hours, no-meeting days, lunch, and time-of-day preferences")
    print("  are NOT stored in config anymore. They live as tokened recurring")
    print("  events on your EA-Managed Google calendar, so you can drag/edit them")
    print("  any time without a config change. Tokens (leading word of the title):")
    print("    wh = working hours (meetings only inside these)")
    print("    nm = no meetings (protected)   p0/p1/p2 = hard/soft/preference")
    print("    wr = working remotely (informational)")
    print("  After setup, add to the EA-Managed calendar e.g. 'wh Working Hours'")
    print("  9-5 Mon-Fri, 'nm Lunch' 12-1, and 'nm No-meeting Friday' (all day).")
    print("  See skills/scheduling-rules.md for the full model.\n")

    # Slot selection
    print("\n  Meeting scheduling behavior:")
    contiguous = prompt_yn(
        "Cluster meetings together (protect focus blocks)?",
        default=ex_slot.get("prefer_contiguous", True), key="cluster-meetings",
    )
    default_duration = int(prompt(
        "Default meeting duration (minutes)",
        str(ex_slot.get("preferred_duration_minutes", 30)), key="default-duration-minutes",
    ))

    print("\n  VIP contacts get extended hours and more time options.")
    vip_end = prompt_time(
        "Extended end time for VIP contacts",
        "17:00", key="vip-end-time",
    )

    # Calendar setup
    print("\n  Calendar configuration:")
    print("  Your Outlook calendar is always included (via Graph API).")
    google_accounts = prompt_list(
        "Google Calendar email(s) that block availability",
        "you@gmail.com", key="google-calendars",
    )

    prefs = {
        "timezone": tz,
        "_preferences_note": (
            "Day/time scheduling preferences (working hours, no-meeting blocks, "
            "lunch, no-meeting days, morning preference) live as tokened recurring "
            "events on the EA-Managed calendar, NOT here. See skills/scheduling-rules.md."),
        "calendars": {
            "outlook": {
                "type": "graph_api",
                "note": "Primary work calendar, read via Graph API in preprocessor",
            },
        },
        "slot_selection": {
            "preferred_duration_minutes": default_duration,
            "prefer_contiguous": contiguous,
            "contiguous_note": "If I have a meeting at 10:00-10:30, prefer suggesting 10:30 or 9:30 over a random 2pm slot. Cluster meetings together to preserve focus blocks.",
            "max_slot_duration_to_propose_minutes": 60,
            "vip_override": f"If user says someone is 'very important' or 'VIP', expand to all available times within working hours extended to {vip_end}, and remember that person as VIP for future requests.",
        },
        "response_style": {},  # Populated in Phase 7
        "vip_contacts": {
            "note": "Populated automatically when user says someone is important in Slack.",
            "addresses": [],
        },
    }

    # Add Google calendar entries
    for i, acct in enumerate(google_accounts):
        key = "google_primary" if i == 0 else f"google_{i+1}"
        prefs["calendars"][key] = {
            "email": acct,
            "type": "google_calendar_mcp",
            "blocks_availability": True,
        }

    # Keys this phase does not ask about (ea_managed_calendar, the family
    # calendars, their notes) stay as the existing file has them.
    merged = {k: v for k, v in kept.items() if k not in prefs}
    merged.update(prefs)
    save_config("scheduling-preferences.json", merged)
    return merged


# ---------------------------------------------------------------------------
# Phase 3: Inbox Philosophy
# ---------------------------------------------------------------------------

def phase_inbox(existing_autonomy: dict = None) -> dict:
    """Configure inbox management philosophy."""
    section_header("Phase 3: Inbox Management Philosophy")

    print("  The EA can treat your inbox in different ways:\n")
    print("  INBOX ZERO: Your inbox is a task queue. Every message in it is a")
    print("  potential action item. When you archive an email, it means 'handled.'")
    print("  The EA reads your entire inbox on each run.\n")
    print("  RELAXED: Your inbox is for reference. You may keep emails around")
    print("  without them being action items. Obligations are tracked")
    print("  independently via the ledger. The EA reads only recent messages.\n")

    inbox_zero = prompt_yn("Use Inbox Zero philosophy?", default=True, key="inbox-zero")

    print()
    if inbox_zero:
        print("  ARCHIVE-AS-COMPLETION: When you manually archive an email (from")
        print("  Outlook, your phone, etc.), the EA automatically marks the")
        print("  corresponding tracked item as 'done.'\n")
        archive_completes = prompt_yn("Enable archive-as-completion?", default=True,
                                      key="archive-as-completion")
    else:
        archive_completes = False
        print("  Archive-as-completion disabled (not applicable in relaxed mode).\n")

    print("  STALE THREAD THRESHOLD: How many days without a reply before the")
    print("  EA flags a conversation as potentially dropped?\n")
    stale_days = int(prompt("Stale thread threshold (days)", "3", key="stale-thread-days"))
    stale_days_weekly = max(stale_days - 1, 1)

    # Save preprocessor config
    preproc = {
        "stale_thread_days": stale_days,
        "stale_thread_days_weekly_audit": stale_days_weekly,
        "writing_style_window_days": 14,
        "inbox_philosophy": {
            "mode": "inbox_zero" if inbox_zero else "relaxed",
            "read_entire_inbox": inbox_zero,
        },
    }
    save_config("preprocessor-config.json", preproc)

    return {
        "inbox_zero": inbox_zero,
        "archive_completes": archive_completes,
        "stale_days": stale_days,
    }


# ---------------------------------------------------------------------------
# Phase 4: Autonomy Tiers
# ---------------------------------------------------------------------------

def phase_autonomy(inbox_config: dict, existing: dict = None) -> dict:
    """Configure autonomy tiers."""
    section_header("Phase 4: Autonomy Tiers")

    print("  The EA has three autonomy levels:\n")
    print("  ASK-FIRST:       Post what it wants to do in Slack, wait for approval.")
    print("  DO-AND-TELL:     Do it, then tell you what it did in Slack.")
    print("  FIRE-AND-FORGET: Do it silently, include in next briefing.\n")

    ex = existing or {}
    conservative = prompt_yn(
        "Start with conservative defaults (all user-facing tasks = ask-first)?",
        default=True, key="conservative-defaults",
    )

    if conservative:
        task_tiers = {
            "draft_email_response": {
                "tier": "ask-first",
                "note": "Show draft in Slack, wait for 'send' or edits.",
            },
            "send_scheduling_availability": {
                "tier": "ask-first",
                "promotable_per_contact": True,
                "note": "Can be promoted to do-and-tell for specific contacts.",
            },
            "upload_letter_of_recommendation": {
                "tier": "ask-first",
                "note": "Match LoR request against known letters, show in Slack for approval.",
            },
            "prep_reimbursement_email": {
                "tier": "ask-first",
                "note": "Compile receipts, draft email, show in Slack for approval.",
            },
            "create_calendar_hold": {
                "tier": "ask-first",
                "note": "Block focus time or travel time. Ask first.",
            },
            "archive_organize_inbox": {
                "tier": "ask-first",
                "note": "Move processed items, apply categories. Ask first.",
            },
            "update_obligations_ledger": {
                "tier": "fire-and-forget",
                "note": "Internal bookkeeping. Always autonomous.",
            },
            "post_slack_summary": {
                "tier": "fire-and-forget",
                "note": "Briefings and status updates. Always autonomous.",
            },
        }
    else:
        print("\n  For each task type, choose: ask-first, do-and-tell, or fire-and-forget.\n")
        task_types = [
            ("draft_email_response", "Drafting email responses", "ask-first"),
            ("send_scheduling_availability", "Sending scheduling availability", "ask-first"),
            ("upload_letter_of_recommendation", "Uploading letters of recommendation", "do-and-tell"),
            ("prep_reimbursement_email", "Preparing reimbursement emails", "ask-first"),
            ("create_calendar_hold", "Creating calendar holds/focus blocks", "do-and-tell"),
            ("archive_organize_inbox", "Archiving/organizing processed inbox items", "do-and-tell"),
            ("update_obligations_ledger", "Updating the obligations ledger", "fire-and-forget"),
            ("post_slack_summary", "Posting briefing summaries to Slack", "fire-and-forget"),
        ]
        task_tiers = {}
        for key, desc, default_tier in task_types:
            answer_name = "tier-" + key.replace("_", "-")
            tier = prompt(f"  {desc}", default_tier, key=answer_name)
            while tier not in ("ask-first", "do-and-tell", "fire-and-forget"):
                if ANSWERS is not None:
                    raise SystemExit(f"--answers: '{answer_name}' must be ask-first, "
                                     f"do-and-tell or fire-and-forget, not {tier!r}")
                print("    Choose: ask-first, do-and-tell, or fire-and-forget")
                tier = prompt(f"  {desc}", default_tier, key=answer_name)
            task_tiers[key] = {"tier": tier}

    threshold = int(prompt(
        "\nAuto-promote threshold (after N successes without edits, suggest promotion)",
        str(ex.get("escalation_rules", {}).get("promote_after_n_successes", {}).get("threshold", 5)),
        key="auto-promote-threshold",
    ))

    autonomy = {
        "_doc": "Controls how much autonomy the EA has for different task types.",
        "task_tiers": task_tiers,
        "per_contact_overrides": {
            "_doc": "When user says 'for Dr. Smith, just send scheduling' in Slack, add an entry here.",
        },
        "inbox_as_completion": {
            "_doc": "When enabled, archiving an email automatically closes the corresponding ledger item.",
            "enabled": inbox_config.get("archive_completes", True),
        },
        "escalation_rules": {
            "never_autonomous": [
                "Sending email to anyone outside the organization for the first time",
                "Anything involving money, contracts, or legal commitments",
                "Responding to senior leadership, program officers, or deans",
                "Declining invitations or requests",
            ],
            "promote_after_n_successes": {
                "threshold": threshold,
            },
        },
    }

    save_config("autonomy-tiers.json", autonomy)
    return autonomy


# ---------------------------------------------------------------------------
# Phase 5: Group Email Lists
# ---------------------------------------------------------------------------

def phase_groups() -> dict:
    """Configure group email lists for noisy thread detection."""
    section_header("Phase 5: Group Email Lists")

    print("  If you belong to group email lists (scouts, church, HOA, department")
    print("  committees, etc.) that produce reply-all storms, the EA can detect")
    print("  these and offer to summarize them instead of treating each reply as")
    print("  a separate action item.\n")

    groups = {}
    if prompt_yn("Do you have group email lists to configure?", default=False,
                 key="groups-configure"):
        while True:
            name = prompt("Group name (e.g., 'book_club', 'hoa')", required=True,
                          key="group-name")
            addresses = prompt_list(
                f"  Email addresses in '{name}' group (comma-separated)",
                "alice@gmail.com, bob@gmail.com", key="group-addresses",
            )
            if addresses:
                groups[name] = addresses
                print(f"  Added group '{name}' with {len(addresses)} addresses.")
            # An answers file names one group: the loop's questions share
            # their keys, so a scripted "add another" would repeat it.
            if ANSWERS is not None or not prompt_yn("\nAdd another group?", default=False,
                                                    key="group-add-another"):
                break

    save_config("group-senders.json", groups)
    return groups


# ---------------------------------------------------------------------------
# Phase 6: Deadline Sender Categories
# ---------------------------------------------------------------------------

def phase_deadlines() -> dict:
    """Configure deadline sender domain categories."""
    section_header("Phase 6: Deadline Sender Categories")

    print("  When the EA detects deadline keywords in emails, it can categorize")
    print("  them by the sender's domain. This helps prioritize: a grant deadline")
    print("  from nsf.gov is different from a utility bill from your HOA.\n")
    print("  Examples:")
    print("    example.edu   -> university")
    print("    nsf.gov       -> grants")
    print("    statefarm.com -> financial")
    print("    irs.gov       -> taxes\n")

    senders = {}
    if prompt_yn("Configure deadline sender categories?", default=False,
                 key="deadlines-configure"):
        while True:
            domain = prompt("Sender domain (e.g., 'nsf.gov')", key="deadline-domain")
            category = prompt(f"  Category for '{domain}'", "general", key="deadline-category")
            senders[domain] = category
            # One entry from an answers file (the loop's keys repeat).
            if ANSWERS is not None or not prompt_yn("Add another?", default=True,
                                                    key="deadline-add-another"):
                break

    save_config("deadline-senders.json", senders)
    return senders


# ---------------------------------------------------------------------------
# Phase 7: Response Style Defaults
# ---------------------------------------------------------------------------

def phase_response_style(scheduling_prefs: dict) -> dict:
    """Configure default response styles."""
    section_header("Phase 7: Response Style Defaults")

    print("  How should the EA draft responses? These are defaults that can")
    print("  be overridden per-contact based on writing style analysis.\n")

    style = {
        "default": prompt(
            "Default style",
            "Propose 2-3 specific times. Short, professional.", key="style-default",
        ),
        "internal": prompt(
            "Style for colleagues at your organization",
            "Friendly but efficient.", key="style-internal",
        ),
        "external_academic": prompt(
            "Style for external academics",
            "Professional, slightly more formal. Include timezone.",
            key="style-external-academic",
        ),
        "student": prompt(
            "Style for students",
            "Warm, brief.", key="style-student",
        ),
    }

    scheduling_prefs["response_style"] = style
    save_config("scheduling-preferences.json", scheduling_prefs)
    return style


# ---------------------------------------------------------------------------
# Phase 8: Auth Setup
# ---------------------------------------------------------------------------

def phase_auth(chat: str = "slack"):
    """Guide user through authentication setup."""
    section_header("Phase 8: Authentication Setup")

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    print("  The EA needs access to your email and calendar. This phase")
    print("  walks you through authenticating with each service.\n")

    # Graph API
    print("  --- Microsoft Graph API (Outlook) ---\n")
    print("  Choose how the EA authenticates to Microsoft Graph:")
    print("    1) confidential  Auth-code flow + client secret")
    print("                     (app registration; public client flows OFF)")
    print("    2) device_code   MSAL device code")
    print("                     (app registration; public client flows ON)")
    print("    3) interactive   MSAL browser sign-in")
    print("                     (app registration; public-client redirect URI)")
    print("    4) skip          Set up later")
    print("  See docs/AUTHENTICATION.md for how to pick. With no app registration,")
    print("  the owner plugin outlook-web-auth can supply the token (skip here, then")
    print("  see https://github.com/arpg/tacitus-plugin-outlook-web-auth).\n")

    choice = prompt("Graph auth method [1-4]", "1", key="graph-auth-method")
    method = {"1": "confidential", "2": "device_code", "3": "interactive",
              "4": "skip", "confidential": "confidential", "device_code": "device_code",
              "interactive": "interactive", "skip": "skip"}.get(choice, "confidential")

    if method == "skip":
        print("  Skipped. You can authenticate later via:")
        print("    python3 preprocessor/graph_client.py")
    else:
        env_data = {"GRAPH_AUTH_METHOD": method}
        env_data["GRAPH_CLIENT_ID"] = prompt(
            "Application (client) ID", required=True, key="graph-client-id")
        tenant = prompt("Tenant ID (blank = multi-tenant 'common')", "", key="graph-tenant-id")
        if tenant:
            env_data["GRAPH_AUTHORITY"] = (
                f"https://login.microsoftonline.com/{tenant}")

        if method == "confidential":
            redirect = prompt("Redirect URI (Web platform)",
                              "http://localhost:8400/callback", key="graph-redirect-uri")
            env_data["GRAPH_REDIRECT_URI"] = redirect
            secret = prompt("Client secret value", required=True, key="graph-client-secret")
            # The secret goes in the owner-private secrets.env, never in
            # ~/.ea (ea-bot space once the service account exists).
            merge_env_file(private_secrets_file(), {"GRAPH_CLIENT_SECRET": secret})
            os.environ["GRAPH_CLIENT_SECRET"] = secret

        merge_env_file(EA_DIR / ".env", env_data)
        for k, v in env_data.items():
            os.environ[k] = v  # so the in-process sign-in below picks them up

        if prompt_yn("Sign in to Microsoft Graph now?", default=True, key="graph-sign-in-now"):
            print("\n  Starting Graph API authentication...")
            print("  This will open a browser window for Microsoft login.\n")
            try:
                import importlib
                sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
                import graph_client as graph
                importlib.reload(graph)  # re-read the env vars set above
                if method == "confidential":
                    # Sign in, discover the app's granted scopes, and align the
                    # requested scopes to them (default stays the full superset).
                    info = graph.setup_confidential_and_align()
                    print(f"  Signed in as {info['user']}.")
                    print(f"  App grants: {', '.join(info['granted']) or '(none)'}")
                    print(f"  Requesting scopes: {', '.join(info['aligned_short'])}")
                    merge_env_file(
                        EA_DIR / ".env",
                        {"GRAPH_SCOPES": " ".join(info["aligned_short"])})
                    if info["dropped_short"]:
                        print("  Dropped (not granted by your app): "
                              f"{', '.join(info['dropped_short'])}")
                        print("  Ask your Entra admin to add these if you need "
                              "those features.")
                else:
                    graph.authenticate()
                print("  Graph API authentication successful!")
            except Exception as e:
                print(f"  Graph API auth failed: {e}")
                print("  Retry later: python3 preprocessor/graph_client.py")
        else:
            print("  Config saved. Sign in later with:")
            print("    python3 preprocessor/graph_client.py")

    # Google Calendar
    print("\n  --- Google Calendar ---\n")
    if prompt_yn("Set up Google Calendar now?", default=True, key="google-calendar-now"):
        secret_file = AUTH_DIR / "google_client_secret.json"
        if not secret_file.exists():
            print(f"\n  Place your Google OAuth2 client secret at:")
            print(f"    {secret_file}")
            print("  Then re-run: python3 scripts/install/user_init.py --phase auth")
            print("  See docs/SETUP.md 'Google OAuth app' for how to create the secret.")
        else:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
                import google_calendar_client as gcal
                gcal.authenticate_all()
                print("  Google Calendar authentication successful!")
            except Exception as e:
                print(f"  Google Calendar auth failed: {e}")
                print("  You can set this up later.")
    else:
        print("  Skipped.")

    if load_existing("ea-config.json").get("profile") in ("email", "review-only"):
        return  # no chat platform on this profile: no chat credentials to ask

    if chat == "zulip":
        print("\n  --- Zulip ---\n")
        print("  The EA talks to you in private Zulip streams (chat block in")
        print("  ea-config.json). Put the bot's API key in the owner-private")
        print(f"  {private_secrets_file()} (mode 600) as ZULIP_API_KEY=...")
        print("  scripts/zulip_bootstrap.py prints that line for you.")
        return

    # Slack
    print("\n  --- Slack ---\n")
    print("  The EA communicates with you via a private Slack channel.")
    print("  You need a Bot Token (xoxb-...) and App Token (xapp-...).\n")

    # The tokens go in the owner-private secrets.env; the ids in ~/.ea/.env
    # (mode 640, non-secret), as docs/SETUP.md 'The Slack environment files' says.
    secrets = {}
    bot_token = prompt("Slack Bot Token (xoxb-..., or 'skip')", "skip", key="slack-bot-token")
    if bot_token != "skip":
        secrets["SLACK_BOT_TOKEN"] = bot_token
    app_token = prompt("Slack App Token (xapp-..., or 'skip')", "skip", key="slack-app-token")
    if app_token != "skip":
        secrets["SLACK_APP_TOKEN"] = app_token

    env_data = {}
    channel_id = prompt("EA Slack Channel ID (C..., or 'skip')", "skip", key="slack-channel-id")
    if channel_id != "skip":
        env_data["EA_SLACK_CHANNEL_ID"] = channel_id
    channel_name = prompt("EA Slack Channel Name (or 'skip')", "skip", key="slack-channel-name-env")
    if channel_name != "skip":
        env_data["EA_SLACK_CHANNEL_NAME"] = channel_name
    owner_id = prompt("Your Slack member ID (U..., or 'skip')", "skip", key="slack-owner-id")
    if owner_id != "skip":
        env_data["EA_SLACK_OWNER_ID"] = owner_id

    if secrets:
        merge_env_file(private_secrets_file(), secrets)
    if env_data:
        merge_env_file(EA_DIR / ".env", env_data, mode=0o640)


# ---------------------------------------------------------------------------
# Phase 9: Writing Style Detection (Deep)
# ---------------------------------------------------------------------------

def analyze_writing_style_deep(sent: list, min_contact_emails: int = 2) -> dict:
    """
    Enhanced writing style analysis for initialization.

    Differences from the runtime analyze_writing_style() in preprocess.py:
    - Scans more sent mail (caller provides 90-day window)
    - Lower per-contact threshold (2 emails instead of 3)
    - Additional metrics: exclamation frequency, per-domain tone
    - Outputs a baseline file for long-term reference
    """
    GREETING_PATTERNS = [
        (r"^Hey (\w+)", "Hey {name},"),
        (r"^Hi (\w+)", "Hi {name},"),
        (r"^Hello (\w+)", "Hello {name},"),
        (r"^Dear (\w+)", "Dear {name},"),
        (r"^Good (?:morning|afternoon|evening)", "Good {time},"),
    ]
    CLOSING_PATTERNS = [
        "best", "cheers", "thanks", "thank you", "regards",
        "sincerely", "all the best", "take care", "warmly",
    ]

    contact_data = defaultdict(lambda: {
        "greetings": [], "closings": [], "lengths": [],
        "has_contractions": 0, "has_exclamation": 0, "count": 0,
    })
    domain_data = defaultdict(lambda: {
        "has_contractions": 0, "has_exclamation": 0, "count": 0,
    })
    all_greetings = []
    all_closings = []
    all_lengths = []
    contraction_count = 0
    exclamation_count = 0
    total_count = 0

    total = len(sent)
    for i, m in enumerate(sent):
        if i % 50 == 0 and total > 50:
            pct = int(i / total * 100)
            print(f"  Analyzing... {pct}% ({i}/{total})", end="\r")

        preview = m.get("bodyPreview", "").strip()
        if not preview or len(preview) < 20:
            continue

        to_list = m.get("toRecipients", [])
        if not to_list:
            continue
        recip_addr = to_list[0].get("emailAddress", {}).get("address", "").lower()
        domain = recip_addr.split("@")[-1] if "@" in recip_addr else ""

        total_count += 1
        word_count = len(preview.split())
        all_lengths.append(word_count)

        cd = contact_data[recip_addr]
        cd["count"] += 1
        cd["lengths"].append(word_count)

        dd = domain_data[domain]
        dd["count"] += 1

        # Detect greeting
        first_line = preview.split("\n")[0].strip().rstrip(",")
        for pat, fmt in GREETING_PATTERNS:
            if re.match(pat, first_line, re.IGNORECASE):
                all_greetings.append(fmt)
                cd["greetings"].append(fmt)
                break

        # Detect closing
        lines = [l.strip() for l in preview.split("\n") if l.strip()]
        for line in lines[-3:]:
            line_lower = line.rstrip(",.!").lower()
            for closing in CLOSING_PATTERNS:
                if line_lower == closing or line_lower.startswith(closing + ","):
                    all_closings.append(closing.title())
                    cd["closings"].append(closing.title())
                    break

        # Formality markers
        has_contraction = bool(re.search(
            r"\b(don't|can't|won't|wouldn't|shouldn't|couldn't|isn't|aren't|"
            r"I'm|I'll|I've|it's|that's|there's|we're|they're)\b",
            preview, re.IGNORECASE,
        ))
        if has_contraction:
            contraction_count += 1
            cd["has_contractions"] += 1
            dd["has_contractions"] += 1

        if "!" in preview:
            exclamation_count += 1
            cd["has_exclamation"] += 1
            dd["has_exclamation"] += 1

    if total > 50:
        print(f"  Analyzing... 100% ({total}/{total})    ")

    if total_count == 0:
        print("  No sent emails found to analyze.")
        return {}

    def most_common(lst):
        if not lst:
            return None
        counts = defaultdict(int)
        for item in lst:
            counts[item] += 1
        return max(counts, key=counts.get)

    def tone_from_rate(contraction_rate):
        if contraction_rate > 0.5:
            return "casual"
        elif contraction_rate > 0.2:
            return "casual-professional"
        return "formal"

    avg_len = sum(all_lengths) // len(all_lengths) if all_lengths else 80

    # Per-contact profiles
    per_contact = {}
    for addr, cd in contact_data.items():
        if cd["count"] < min_contact_emails:
            continue
        avg_contact_len = sum(cd["lengths"]) // len(cd["lengths"])
        contraction_rate = cd["has_contractions"] / cd["count"]
        per_contact[addr] = {
            "greeting": most_common(cd["greetings"]),
            "closing": most_common(cd["closings"]),
            "avg_length": avg_contact_len,
            "sample_count": cd["count"],
            "tone": tone_from_rate(contraction_rate),
        }

    # Per-domain tone
    per_domain_tone = {}
    for domain, dd in domain_data.items():
        if dd["count"] < 3:
            continue
        rate = dd["has_contractions"] / dd["count"]
        per_domain_tone[domain] = tone_from_rate(rate)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": total_count,
        "sample_window_days": 90,
        "default_greeting": most_common(all_greetings) or "Hi {name},",
        "default_closing": most_common(all_closings) or "Best,",
        "avg_length_words": avg_len,
        "uses_contractions": contraction_count > total_count * 0.3,
        "uses_exclamations": exclamation_count > total_count * 0.3,
        "per_contact": per_contact,
        "per_domain_tone": per_domain_tone,
    }

    return result


def phase_tone(out: Path | None = None):
    """Run deep writing style analysis on sent mail.

    `out` takes the baseline JSON instead of the config dir: the caller that
    holds the mail token is not the user who writes ea-bot's config, so it
    computes here and hands the file to that user."""
    section_header("Phase 9: Writing Style Detection")

    print("  This scans your last 90 days of sent email to learn your")
    print("  communication style: greetings, closings, formality level,")
    print("  and per-contact tone. This helps the EA draft replies that")
    print("  sound like you.\n")

    if not prompt_yn("Run writing style analysis now?", default=True, key="style-analysis-now"):
        print("  Skipped. You can run this later:")
        print("    python3 scripts/install/user_init.py --style-only")
        return

    print("\n  Fetching sent mail (last 90 days)...")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
        import graph_client as graph
        sent = graph.get_sent(days_back=90)
        # Cap at 500 messages
        if len(sent) > 500:
            print(f"  Found {len(sent)} sent emails. Analyzing first 500.")
            sent = sent[:500]
        else:
            print(f"  Found {len(sent)} sent emails.")

        result = analyze_writing_style_deep(sent)
        if result and out is not None:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(result, indent=2) + "\n")
            print(f"  -> Wrote {out}")
        elif result:
            save_config("writing-style-baseline.json", result)
        if result:
            print(f"\n  Style profile:")
            print(f"    Default greeting: {result.get('default_greeting')}")
            print(f"    Default closing:  {result.get('default_closing')}")
            print(f"    Avg email length: {result.get('avg_length_words')} words")
            print(f"    Uses contractions: {result.get('uses_contractions')}")
            print(f"    Per-contact profiles: {len(result.get('per_contact', {}))}")
            print(f"    Per-domain tones: {len(result.get('per_domain_tone', {}))}")
    except Exception as e:
        print(f"  Failed to analyze writing style: {e}")
        print("  Make sure Graph API is authenticated first (Phase 8).")
        print("  You can re-run: python3 scripts/install/user_init.py --style-only")


# ---------------------------------------------------------------------------
# Phase 10: Reimbursement Config (optional)
# ---------------------------------------------------------------------------

def phase_reimbursement():
    """Configure reimbursement settings."""
    section_header("Phase 10: Reimbursement Configuration (Optional)")

    print("  If your institution has a travel reimbursement form, the EA can")
    print("  help collect receipts and prepare submissions.\n")

    if not prompt_yn("Configure reimbursement automation?", default=False,
                     key="reimbursement-configure"):
        print("  Skipped.")
        return

    config = {
        "submitter": {
            "first_name": prompt("Your first name", required=True, key="reimbursement-first-name"),
            "last_name": prompt("Your last name", required=True, key="reimbursement-last-name"),
            "email": prompt("Submission email (may differ from work email)",
                            key="reimbursement-email"),
            "affiliation": prompt("Affiliation", "Faculty / Staff", key="reimbursement-affiliation"),
        },
        "form_url": prompt(
            "Reimbursement form URL (or leave empty if none)", key="reimbursement-form-url",
        ),
        "receipts_dir": str(EA_DIR / "receipts"),
    }

    save_config("reimbursement-config.json", config)


# ---------------------------------------------------------------------------
# Phase 11: VIP Senders
# ---------------------------------------------------------------------------

def phase_vip():
    """Seed VIP senders list."""
    section_header("Phase 11: VIP Senders")

    print("  VIP senders get priority alerts and extended scheduling hours.")
    print("  You can seed this list now, or let the EA learn VIPs over time")
    print("  when you say 'this person is very important' in Slack.\n")

    vip_path = CONFIG_DIR / "vip_senders.txt"
    existing = []
    if vip_path.exists():
        existing = [
            l.strip() for l in vip_path.read_text().splitlines()
            if l.strip() and not l.startswith("#")
        ]

    if existing:
        print(f"  Current VIPs: {', '.join(existing)}\n")

    addresses = prompt_list(
        "VIP email addresses (comma-separated, or leave empty)",
        "dean@example.edu, program-officer@example.org", key="vip-addresses",
    )
    if addresses or existing:
        all_vips = sorted(set(existing + addresses))
        lines = [
            "# VIP senders -- one email per line.",
            "# These contacts get priority alerts and extended scheduling hours.",
            "# The EA adds entries when you say 'this person is very important' in Slack.",
            "",
        ] + all_vips
        vip_path.write_text("\n".join(lines) + "\n")
        print(f"  -> Saved {vip_path} ({len(all_vips)} VIPs)")
    else:
        print("  No VIPs configured. The EA will learn them over time.")


# ---------------------------------------------------------------------------
# Phase 12: Phone push (optional)
# ---------------------------------------------------------------------------

def phase_push():
    """The ntfy push for an install with no chat platform (preprocessor/
    push_notify.py): the settings go in the owner-private push.json,
    never in ea-config.json, so the EA cannot choose where reports go."""
    section_header("Phase 12: Phone Push (Optional)")

    print("  An email or review-only install has no chat channel: a report is")
    print("  a file in ~/.ea/state/reports/. With the ntfy app on your phone,")
    print("  each report also sends a short push. The settings are written to")
    print(f"  {private_dir() / 'push.json'} (owner-only, mode 600).\n")

    if not prompt_yn("Send each report to your phone through ntfy?", default=False,
                     key="push-configure"):
        print("  Skipped. docs/SETUP.md 'Phone push' says how to add it later.")
        return

    settings = {
        "ntfy_url": prompt("ntfy server URL", "https://ntfy.sh", key="push-ntfy-url"),
        "topic": prompt("Topic (a long random name; on a public server it is the password)",
                        required=True, key="push-topic"),
        "include_text": prompt_yn("Include the report text in the push?", default=False,
                                  key="push-include-text"),
    }
    click = prompt("Web board URL to open from the push (optional)", "", key="push-click")
    if click:
        settings["click"] = click

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
    import push_notify
    bad = push_notify.problems(settings)
    if bad:
        for line in bad:
            print(f"  {line}")
        raise SystemExit("  push.json not written: fix the values above and run "
                         "python3 scripts/install/user_init.py --phase push again")
    for line in push_notify.warnings(settings):
        print(f"  note: {line}")

    priv = private_dir()
    priv.mkdir(mode=0o700, exist_ok=True)
    path = priv / push_notify.SETTINGS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    os.chmod(path, 0o600)
    print(f"  -> Saved {path}")
    print("  python3 preprocessor/push_notify.py test sends one push.")


# ---------------------------------------------------------------------------
# Phase 13: Initialize State Files
# Phase 11a: First-week preferences
# ---------------------------------------------------------------------------

def phase_preferences():
    """Ask the first-week question bank in one sitting and record each
    non-empty answer as a standing rule in the user's own words
    (preprocessor/onboarding.py). An empty answer leaves the question for
    a later `onboarding.py ask` or the morning briefing."""
    section_header("Phase 11a: First-Week Preferences")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
    import onboarding

    questions = onboarding.remaining_questions(ea_dir=EA_DIR)
    if not questions:
        print("  Every question is answered or skipped.")
        return

    print("  Each answer becomes a standing rule, in your words, in")
    print("  config/preferences.json; the briefings and drafts follow it.")
    print("  Press Enter to leave a question for later.\n")

    recorded = 0
    for q in questions:
        print(f"  {q['question']}")
        print(f"    Why: {q['why']}")
        print(f"    Example: {q['example']}")
        answer = prompt(f"  [{q['id']}]")
        if not answer:
            continue
        rule = onboarding.record_answer(q["id"], answer, ea_dir=EA_DIR)["rule"]
        print(f"    -> Recorded {rule['id']}")
        recorded += 1
        print()

    left = len(questions) - recorded
    print(f"\n  Recorded {recorded} rule(s). {left} question(s) left for")
    print("  `python3 preprocessor/onboarding.py ask --count 3` or the morning briefing.")


# ---------------------------------------------------------------------------
# Phase 12: Initialize State Files
# ---------------------------------------------------------------------------

def phase_state():
    """Create empty state files."""
    section_header("Phase 13: Initializing State Files")

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    state_files = {
        "obligations-ledger.json": "[]",
        "seen-messages.json": "[]",
        "seen-conversations.json": "{}",
        "surfaced-responses.json": "{}",
        "scheduled-sends.json": "[]",
        "snoozed-messages.json": "[]",
    }

    created = 0
    for filename, content in state_files.items():
        path = STATE_DIR / filename
        if not path.exists():
            path.write_text(content)
            created += 1

    print(f"  Initialized {created} state files in {STATE_DIR}")
    if created < len(state_files):
        print(f"  ({len(state_files) - created} already existed, not overwritten)")


# ---------------------------------------------------------------------------
# Phase 14: Summary
# ---------------------------------------------------------------------------

def phase_summary():
    """Print summary and next steps."""
    section_header("Setup Complete!")

    print(f"  EA directory: {EA_DIR}\n")
    print("  Config files created:")
    for f in sorted(CONFIG_DIR.iterdir()):
        print(f"    {f.name}")
    print()
    print("  Next steps:")
    print("  1. Review your config files and adjust as needed")
    print("  2. Set up cron jobs (see scripts/install/setup.sh for examples):")
    print("       - Morning briefing:  7 AM weekdays")
    print("       - Scheduling check:  every 30 min during business hours")
    print("       - Priority check:    every 15 min during business hours")
    print("       - Afternoon catchup: 3 PM weekdays")
    print("       - Weekly audit:      Sunday evening")
    print("  3. Set up MCP connectors for Gmail and Google Calendar")
    print("  4. Install the Slack bot (systemd service)")
    print("  5. Run your first briefing:")
    print(f"       EA_DIR={EA_DIR} ./run-agent.sh morning-briefing --model sonnet")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global EA_DIR, CONFIG_DIR, STATE_DIR, AUTH_DIR, ANSWERS

    parser = argparse.ArgumentParser(
        description="EA System User Initialization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--ea-dir", type=str, default=None,
        help="Override EA directory (default: ~/.ea or $EA_DIR)",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Re-run on existing config, showing current values as defaults",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing config without prompting",
    )
    parser.add_argument(
        "--style-only", action="store_true",
        help="Only run writing style detection (Phase 9)",
    )
    parser.add_argument(
        "--phase", type=str, choices=ALL_PHASES,
        help="Run a single phase",
    )
    parser.add_argument(
        "--out", type=Path, default=None, metavar="FILE",
        help="With --phase tone or --style-only: write the style baseline to FILE "
             "(mode 0600) instead of the config dir",
    )
    parser.add_argument(
        "--answers", type=str, default=None, metavar="FILE",
        help="JSON file of answers by prompt key; no prompt reads the terminal "
             "(tests/install/answers-email.json is a complete example)",
    )
    parser.add_argument(
        "--chat", choices=["slack", "zulip"], default="slack",
        help="Chat platform the EA talks to you on (default: slack)",
    )
    parser.add_argument("--zulip-site", default="",
                        help="Zulip server URL (required with --chat zulip)")
    parser.add_argument("--zulip-bot-email", default="",
                        help="EA bot's Zulip email (required with --chat zulip)")
    parser.add_argument("--zulip-owner-id", default="",
                        help="Your Zulip user id (required with --chat zulip)")
    args = parser.parse_args()
    ANSWERS = None
    if args.answers:
        try:
            loaded = json.loads(Path(args.answers).read_text())
        except (OSError, ValueError) as e:
            parser.error(f"--answers {args.answers}: {e}")
        if not isinstance(loaded, dict):
            parser.error(f"--answers {args.answers}: not a JSON object")
        ANSWERS = loaded
        ANSWERS_USED.clear()
    if args.chat == "zulip":
        missing = [f"--{n}" for n in ("zulip-site", "zulip-bot-email", "zulip-owner-id")
                   if not getattr(args, n.replace("-", "_"))]
        if missing:
            parser.error(f"--chat zulip requires {', '.join(missing)}")

    # Override EA_DIR if specified
    if args.ea_dir:
        EA_DIR = Path(args.ea_dir)
    CONFIG_DIR = EA_DIR / "config"
    STATE_DIR = EA_DIR / "state"
    AUTH_DIR = EA_DIR / "auth"

    # Safety check
    profile_path = CONFIG_DIR / "user-profile.json"
    if profile_path.exists() and not args.update and not args.force and not args.style_only and not args.phase:
        print(f"ERROR: An EA system is already configured at {EA_DIR}/")
        print()
        print("  To update the existing configuration:  python3 scripts/install/user_init.py --update")
        print("  To overwrite without prompting:        python3 scripts/install/user_init.py --force")
        print("  To set up a new EA for a different user:")
        print(f"    EA_DIR=/path/to/new/.ea python3 scripts/install/user_init.py")
        sys.exit(1)

    block = chat_block(args.chat, args.zulip_site, args.zulip_bot_email,
                       args.zulip_owner_id)
    if block:
        write_chat_config(block)

    # Style-only mode
    if args.out is not None and not (args.style_only or args.phase == "tone"):
        parser.error("--out goes with --phase tone or --style-only")
    if args.style_only:
        phase_tone(args.out)
        return

    # Single phase mode
    if args.phase:
        existing_profile = load_existing("user-profile.json")
        existing_sched = load_existing("scheduling-preferences.json")
        existing_autonomy = load_existing("autonomy-tiers.json")

        phase_map = {
            "identity": lambda: phase_identity(existing_profile),
            "scheduling": lambda: phase_scheduling(existing_sched),
            "inbox": lambda: phase_inbox(existing_autonomy),
            "autonomy": lambda: phase_autonomy({"archive_completes": True}, existing_autonomy),
            "groups": phase_groups,
            "deadlines": phase_deadlines,
            "style": lambda: phase_response_style(existing_sched),
            "auth": lambda: phase_auth(args.chat),
            "tone": lambda: phase_tone(args.out),
            "reimbursement": phase_reimbursement,
            "vip": phase_vip,
            "push": phase_push,
            "preferences": phase_preferences,
            "state": phase_state,
            "summary": phase_summary,
        }
        phase_map[args.phase]()
        return

    # Full setup
    print()
    print("=" * 60)
    print("  EA System – User Initialization")
    print("=" * 60)
    print()
    print(f"  EA directory: {EA_DIR}")
    print()
    print("  This wizard will walk you through configuring the EA system.")
    print("  Each section explains what it controls and why it matters.")
    print("  Press Enter to accept defaults shown in [brackets].")
    print()

    # Create directory tree
    for subdir in ["auth", "state", "logs", "config", "receipts", "letters", "templates"]:
        (EA_DIR / subdir).mkdir(parents=True, exist_ok=True)

    # Load existing configs for --update mode
    existing_profile = load_existing("user-profile.json") if args.update else {}
    existing_sched = load_existing("scheduling-preferences.json") if args.update else {}
    existing_autonomy = load_existing("autonomy-tiers.json") if args.update else {}

    # Run phases
    profile = phase_identity(existing_profile)
    sched_prefs = phase_scheduling(existing_sched)
    inbox_config = phase_inbox(existing_autonomy)
    phase_autonomy(inbox_config, existing_autonomy)
    phase_groups()
    phase_deadlines()
    phase_response_style(sched_prefs)
    phase_auth(args.chat)
    phase_tone()
    phase_reimbursement()
    phase_vip()
    phase_push()
    if prompt_yn("Answer the first-week preference questions now?", default=False):
        phase_preferences()
    else:
        print("  Later: python3 scripts/install/user_init.py --phase preferences, or one a day in chat.")
    phase_state()
    phase_summary()
    if ANSWERS is not None:
        unused = sorted(k for k in set(ANSWERS) - ANSWERS_USED if not k.startswith("_"))
        if unused:
            print(f"  note: answers never asked for in this run: {', '.join(unused)}")


if __name__ == "__main__":
    main()
