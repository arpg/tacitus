#!/usr/bin/env python3
"""
EA System – User Initialization Script

Interactive setup for new users. Captures preferences, authenticates
with email/calendar services, and optionally analyzes writing style
from sent mail to build per-contact communication profiles.

The questions are data in scripts/install/setup_questions.py; this
script asks them at the terminal, and scripts/install/setup_web.py asks
the same ones in a browser. Both check an answer with the same code and
write it with the same writer (the write_* functions below).

Usage:
    python3 scripts/install/user_init.py                     # Full interactive setup
    python3 scripts/install/user_init.py --update            # Re-run on existing config
    python3 scripts/install/user_init.py --style-only        # Re-run writing style detection
    python3 scripts/install/user_init.py --phase tone --out FILE
                                             # The style baseline to FILE, not the
                                             # config dir (the container's owner side)
    python3 scripts/install/user_init.py --phase scheduling  # Run a single phase
    python3 scripts/install/user_init.py --phase profile     # The profile and the notice,
                                             # as setup.sh --acknowledged records them
    python3 scripts/install/user_init.py --ea-dir /tmp/test  # Override EA directory
    python3 scripts/install/user_init.py --chat web --web-host host.tailnet.ts.net \
        --web-login you@example.edu         # Talk to the EA in the web chat
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
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_questions as Q  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_DIR = EA_DIR / "config"
STATE_DIR = EA_DIR / "state"
AUTH_DIR = EA_DIR / "auth"

ALL_PHASES = [
    "profile", "identity", "scheduling", "inbox", "autonomy", "groups",
    "deadlines", "style", "chat", "auth", "tone", "reimbursement",
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
# setup_questions.parse reads a value the same way for the setup page.
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
           key: str | None = None, check=None) -> str:
    """Ask a question with optional default. Returns stripped input.
    `check(answer)` names what is wrong with a non-empty answer, or None:
    the terminal asks again, an answers file stops the run."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        found, value = _scripted(key)
        answer = Q.parse("text", value, default)
        if not answer:
            if required:
                raise SystemExit(f"--answers: '{key}' ({question}) is required and "
                                 f"{'empty' if found else 'missing'} in the answers file")
            return ""
        err = check(answer) if check else None
        if err:
            raise SystemExit(f"--answers: '{key}' ({question.strip()}): {err}")
        return answer
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "
    while True:
        answer = _read(display).strip() or default
        if not answer:
            if required:
                print("  This field is required.")
                continue
            return ""
        err = check(answer) if check else None
        if err:
            print(f"  {err}")
            continue
        return answer


def prompt_yn(question: str, default: bool = True, key: str | None = None) -> bool:
    """Ask a yes/no question."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        _found, value = _scripted(key)
        return Q.parse("yesno", value, default)
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _read(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def prompt_list(question: str, example: str = "", key: str | None = None) -> list[str]:
    """Ask for a comma-separated list."""
    key = key or answer_key(question)
    if ANSWERS is not None:
        _found, value = _scripted(key)
        return Q.parse("list", value)
    hint = f" (e.g., {example})" if example else ""
    return Q.parse("list", _read(f"{question}{hint}: "))


def prompt_time(question: str, default: str = "09:00", key: str | None = None) -> str:
    """Ask for a time in HH:MM format."""
    return prompt(question, default, key=key,
                  check=lambda answer: Q.problem({"kind": "time"}, answer))


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


def web_chat_block(host: str = "", login: str = "", current: dict | None = None) -> dict:
    """The `chat` block that runs the web chat alone: `current` (the block
    ea-config.json has) with `"frontend": "web"`, no shadow, and the web
    block's port 8470 and base path `/chat`; `public_url` is the chat's own
    HTTPS address on the tailnet host, when one is given, and
    `tailscale_login` the owner's login, when given (docs/webchat.md,
    Config). The hardening step copies it into the owner-private chat.json
    once each value is typed back (owner_trust.py seed --chat)."""
    block = json.loads(json.dumps(current)) if isinstance(current, dict) else {}
    web = block.get("web") if isinstance(block.get("web"), dict) else {}
    web.update({"port": Q.WEB_PORT, "base_path": Q.WEB_BASE_PATH})
    host = str(host or "").strip().rstrip(".").lower()
    if host:
        web["public_url"] = f"https://{host}:{Q.WEB_SERVE_PORT}{Q.WEB_BASE_PATH}"
    if str(login or "").strip():
        web["tailscale_login"] = str(login).strip()
    block.update({"frontend": "web", "shadow": "", "web": web})
    return block


def chat_block(frontend: str, site: str = "", bot_email: str = "",
               owner_id: str = "", web_host: str = "", web_login: str = "") -> dict | None:
    """The `chat` block for ea-config.json, or None for Slack (the default
    block in ea_config already means Slack, so nothing is written)."""
    if frontend == "web":
        return web_chat_block(web_host, web_login,
                              load_existing("ea-config.json").get("chat"))
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
    """Merge the chat block into ea-config.json, keeping every other key;
    with no file yet it starts from the template, as setup.sh would."""
    path = CONFIG_DIR / "ea-config.json"
    data = load_existing("ea-config.json") if path.exists() else template_values("ea-config.json")
    data["chat"] = block
    save_config("ea-config.json", data)


def _recorded_chat(block: dict, chosen: str | None = None) -> str | None:
    """The chat platform this install already chose, offered as the
    default: `chosen` (--chat), a web or Zulip block, or a Slack block
    that the Slack ids in ~/.ea/.env back (the template's block says
    Slack with no choice behind it). None when nothing was chosen."""
    if chosen:
        return chosen
    frontend = block.get("frontend") if isinstance(block, dict) else None
    if frontend in ("web", "zulip"):
        return frontend
    if frontend == "slack":
        try:
            lines = (EA_DIR / ".env").read_text().splitlines()
        except OSError:
            return None
        if any(ln.split("=", 1)[0].strip() == "EA_SLACK_CHANNEL_ID" and ln.split("=", 1)[1].strip()
               for ln in lines if "=" in ln):
            return "slack"
    return None


def _web_host_of(block: dict) -> str:
    """The tailnet host in the web block's public_url, or ""."""
    web = block.get("web") if isinstance(block, dict) and isinstance(block.get("web"), dict) else {}
    import urllib.parse
    try:
        return urllib.parse.urlsplit(str(web.get("public_url") or "")).hostname or ""
    except ValueError:
        return ""


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
# The questions: setup_questions.py, for the terminal and the setup page
# ---------------------------------------------------------------------------

class AnswerError(Exception):
    """Answers a writer refuses, as {answer key: message}; the key "_phase"
    holds a message about the phase as a whole. The terminal prints them
    and stops; the setup page shows each under its field."""

    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def use_ea_dir(path) -> None:
    """Point every writer at the EA directory `path`."""
    global EA_DIR, CONFIG_DIR, STATE_DIR, AUTH_DIR
    EA_DIR = Path(path)
    CONFIG_DIR = EA_DIR / "config"
    STATE_DIR = EA_DIR / "state"
    AUTH_DIR = EA_DIR / "auth"


def make_tree() -> None:
    """The directories of a new EA directory."""
    for subdir in ["auth", "state", "logs", "config", "receipts", "letters", "templates"]:
        (EA_DIR / subdir).mkdir(parents=True, exist_ok=True)


def read_vips() -> list[str]:
    """The addresses in vip_senders.txt, comments left out."""
    vip_path = CONFIG_DIR / "vip_senders.txt"
    if not vip_path.exists():
        return []
    return [line.strip() for line in vip_path.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def _once(fn):
    """`fn`, called at most once."""
    memo: list = []

    def call():
        if not memo:
            memo.append(fn())
        return memo[0]
    return call


def context(chat: str | None = None, load: bool = True, tz=None) -> dict:
    """The facts the questions and the writers read.

    files: the existing user-profile.json, scheduling-preferences.json and
           autonomy-tiers.json, kept where a phase asks nothing (empty when
           `load` is false: a full run without --update);
    ex:    the same, offered as defaults, the profile without the template's
           placeholders;
    profile, chat: what ea-config.json records (`chat` wins over the file);
    acknowledged: whether the README notice is recorded as accepted;
    detected_timezone: `tz`, else this machine's zone, looked up on use."""
    cfg = load_existing("ea-config.json")
    files = {name: (load_existing(name) if load else {})
             for name in (Q.PROFILE_FILE, Q.SCHED_FILE, Q.AUTONOMY_FILE)}
    ex = dict(files)
    ex[Q.PROFILE_FILE] = without_template(files[Q.PROFILE_FILE],
                                          template_values(Q.PROFILE_FILE))
    ack = cfg.get("acknowledged") if isinstance(cfg.get("acknowledged"), dict) else {}
    block = cfg.get("chat") if isinstance(cfg.get("chat"), dict) else {}
    recorded = cfg.get("profile") if cfg.get("profile") in ("email", "review-only", "assistant") else None
    return {
        "files": files,
        "ex": ex,
        "recorded_profile": recorded,
        "profile": recorded or "assistant",
        "chat": chat or str(block.get("frontend") or "slack"),
        "chat_choice": _recorded_chat(block, chat),
        "web_host": _web_host_of(block),
        "web_login": str((block.get("web") or {}).get("tailscale_login") or "")
        if isinstance(block.get("web"), dict) else "",
        "acknowledged": bool(ack.get("responsibility")),
        "acknowledged_on": str(ack.get("responsibility") or ""),
        "detected_timezone": tz or _once(lambda: detect_system_timezone()),
        "private_dir": str(private_dir()),
        "vips": read_vips(),
    }


def print_intro(pid: str, ctx: dict) -> None:
    ph = Q.phase(pid)
    section_header(ph["header"])
    for line in ph.get("intro") or []:
        if "{private_dir}" in line:
            line = line.replace("{private_dir}", str(ctx.get("private_dir") or private_dir()))
        print(line)


def print_notice() -> None:
    """The README's "Your responsibility" paragraph, word for word."""
    import textwrap
    for para in Q.notice_text().split("\n"):
        print(textwrap.fill(para, width=72, initial_indent="  ", subsequent_indent="  "))
        print()


def ask_phase(pid: str, ctx: dict) -> dict:
    """The phase's answers, asked at the terminal or taken from the answers
    file: the questions of setup_questions.py, in their order, each with
    its notes, default and check."""
    def answer(q, default, values):
        for line in q.get("note") or []:
            print(line)
        if q.get("notice"):
            print_notice()
        label = Q.label_of(q, values)
        key, kind = q["key"], q["kind"]
        if kind == "yesno":
            got = prompt_yn(label, default=default, key=key)
            err = Q.problem(q, got)
            if err:
                raise SystemExit(f"  {err}")
            return got
        if kind == "list":
            while True:
                got = prompt_list(label, Q.example_of(q, values, ctx), key=key) or list(default)
                err = Q.problem(q, got)
                if not err:
                    return got
                if ANSWERS is not None:
                    raise SystemExit(f"--answers: '{key}' ({label.strip()}): {err}")
                print(f"  {err}")
        got = prompt(label, default, required=bool(q.get("required")), key=key,
                     check=lambda text: Q.problem(q, text))
        return Q.typed(q, got)

    again = None
    spec = Q.phase(pid).get("again")
    if spec and ANSWERS is None:
        # An answers file gives one entry: the loop's questions share their keys.
        def again(values):
            return prompt_yn(spec["label"], default=Q.default_of(spec, values, ctx), key=spec["key"])
    return Q.walk(pid, ctx, answer, "terminal", again)


# ---------------------------------------------------------------------------
# The writers: one per phase, the same for the terminal and the setup page
# ---------------------------------------------------------------------------

def write_profile(values: dict, ctx: dict) -> dict:
    """The profile and, once, the date the notice was accepted, in
    ea-config.json, as scripts/install/setup.sh --profile <name> --acknowledged
    records them. With no file yet it starts from the template, which
    setup.sh would have copied first; setup.sh run later keeps both."""
    path = CONFIG_DIR / "ea-config.json"
    data = load_existing("ea-config.json") if path.exists() else template_values("ea-config.json")
    data["profile"] = values["profile"]
    ack = data.get("acknowledged") if isinstance(data.get("acknowledged"), dict) else {}
    if values.get("acknowledged") and not ack.get("responsibility"):
        ack["responsibility"] = date.today().isoformat()
        data["acknowledged"] = ack
    save_config("ea-config.json", data)
    return data


def write_identity(values: dict, ctx: dict) -> dict:
    """user-profile.json: the identity answers and the `institution` block
    (preprocessor/institution.py documents every key). Keys the phase
    does not ask about (gmail_accounts, family, the notes, the
    institution's boilerplate and letterhead) stay as the file has them."""
    kept = ctx["files"][Q.PROFILE_FILE]
    raw_inst = kept.get("institution") if isinstance(kept.get("institution"), dict) else {}
    profile = {
        "name": values["full-name"],
        "email": values["work-email"],
        "role_description": values["role-description"],
        "department": values["department"],
        "university": values["university"],
        "office": values["office"],
        "slack_channel_name": values["slack-channel-name"],
        "bio_blurb": values["bio-blurb"],
        "calendar_accounts": values["calendar-accounts"],
    }
    letterhead = raw_inst.get("letterhead")
    profile["institution"] = {
        "name": values["institution-name"],
        "domains": [d.lower().lstrip("@") for d in values["institution-domains"]],
        "related_domains": [d.lower().lstrip("@") for d in values["institution-related-domains"]],
        "safety_rail_domain": values["institution-safety-rail-domain"].lower().lstrip("@"),
        "video": {"platform": values["video-platform"], "link": values["video-link"]},
        "boilerplate_tokens": list(raw_inst.get("boilerplate_tokens") or []),
        "boilerplate_phrases": list(raw_inst.get("boilerplate_phrases") or []),
        "letterhead": letterhead if isinstance(letterhead, dict) and letterhead else {
            "template_dir": "~/.ea/templates/letterhead",
            "asset_dirs": [],
            "required_assets": [],
        },
    }
    # The Create button reads the top-level link when it adds the meeting
    # link to an event; the institution block carries the same value.
    profile["zoom_link"] = profile["institution"]["video"]["link"]
    merged = dict(kept)
    merged.update(profile)
    save_config(Q.PROFILE_FILE, merged)
    return merged


def write_scheduling(values: dict, ctx: dict) -> dict:
    """scheduling-preferences.json. The reply styles (the style phase) and
    the VIP contacts the EA learned stay as the file has them, so this
    phase can run again; so do the keys it does not ask about
    (ea_managed_calendar, the family calendars, their notes)."""
    kept = ctx["files"][Q.SCHED_FILE]
    vip_end = values["vip-end-time"]
    style = kept.get("response_style")
    vips = kept.get("vip_contacts")
    prefs = {
        "timezone": values["timezone"],
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
            "preferred_duration_minutes": values["default-duration-minutes"],
            "prefer_contiguous": values["cluster-meetings"],
            "contiguous_note": "If I have a meeting at 10:00-10:30, prefer suggesting 10:30 or 9:30 over a random 2pm slot. Cluster meetings together to preserve focus blocks.",
            "max_slot_duration_to_propose_minutes": 60,
            "vip_override": f"If user says someone is 'very important' or 'VIP', expand to all available times within working hours extended to {vip_end}, and remember that person as VIP for future requests.",
        },
        "response_style": style if isinstance(style, dict) else {},  # the style phase writes it
        "vip_contacts": vips if isinstance(vips, dict) else {
            "note": "Populated automatically when user says someone is important in Slack.",
            "addresses": [],
        },
    }
    for i, acct in enumerate(values["google-calendars"]):
        key = "google_primary" if i == 0 else f"google_{i+1}"
        prefs["calendars"][key] = {
            "email": acct,
            "type": "google_calendar_mcp",
            "blocks_availability": True,
        }
    merged = {k: v for k, v in kept.items() if k not in prefs}
    merged.update(prefs)
    save_config(Q.SCHED_FILE, merged)
    return merged


def write_inbox(values: dict, ctx: dict | None = None) -> dict:
    """preprocessor-config.json; returns what the autonomy phase reads."""
    inbox_zero = values["inbox-zero"]
    archive_completes = bool(values.get("archive-as-completion")) if inbox_zero else False
    if not inbox_zero:
        print("  Archive-as-completion disabled (not applicable in relaxed mode).\n")
    stale_days = values["stale-thread-days"]
    preproc = {
        "stale_thread_days": stale_days,
        "stale_thread_days_weekly_audit": max(stale_days - 1, 1),
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


def archive_default(ctx: dict) -> bool:
    """archive-as-completion for an autonomy phase with no inbox phase
    before it: what autonomy-tiers.json says now, else on."""
    block = ctx["files"][Q.AUTONOMY_FILE].get("inbox_as_completion")
    if isinstance(block, dict) and isinstance(block.get("enabled"), bool):
        return block["enabled"]
    return True


CONSERVATIVE_TIERS = {
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


def write_autonomy(values: dict, ctx: dict, archive_completes: bool) -> dict:
    """autonomy-tiers.json."""
    if values["conservative-defaults"]:
        task_tiers = {k: dict(v) for k, v in CONSERVATIVE_TIERS.items()}
    else:
        task_tiers = {task: {"tier": values["tier-" + task.replace("_", "-")]}
                      for task, _desc, _tier in Q.TASK_TYPES}
    autonomy = {
        "_doc": "Controls how much autonomy the EA has for different task types.",
        "task_tiers": task_tiers,
        "per_contact_overrides": {
            "_doc": "When user says 'for Dr. Smith, just send scheduling' in Slack, add an entry here.",
        },
        "inbox_as_completion": {
            "_doc": "When enabled, archiving an email automatically closes the corresponding ledger item.",
            "enabled": archive_completes,
        },
        "escalation_rules": {
            "never_autonomous": [
                "Sending email to anyone outside the organization for the first time",
                "Anything involving money, contracts, or legal commitments",
                "Responding to senior leadership, program officers, or deans",
                "Declining invitations or requests",
            ],
            "promote_after_n_successes": {
                "threshold": values["auto-promote-threshold"],
            },
        },
    }
    save_config(Q.AUTONOMY_FILE, autonomy)
    return autonomy


def write_groups(values: dict, ctx: dict | None = None) -> dict:
    """group-senders.json: each group with at least one address."""
    groups = {}
    for entry in values["_entries"] if values["groups-configure"] else []:
        name, addresses = entry["group-name"], entry["group-addresses"]
        if addresses:
            groups[name] = addresses
            print(f"  Added group '{name}' with {len(addresses)} addresses.")
    save_config("group-senders.json", groups)
    return groups


def write_deadlines(values: dict, ctx: dict | None = None) -> dict:
    """deadline-senders.json: sender domain -> category."""
    senders = {}
    for entry in values["_entries"] if values["deadlines-configure"] else []:
        senders[entry["deadline-domain"]] = entry["deadline-category"]
    save_config("deadline-senders.json", senders)
    return senders


def write_style(values: dict, ctx: dict | None = None) -> dict:
    """The reply styles, into scheduling-preferences.json as it is now."""
    style = {
        "default": values["style-default"],
        "internal": values["style-internal"],
        "external_academic": values["style-external-academic"],
        "student": values["style-student"],
    }
    prefs = load_existing(Q.SCHED_FILE)
    prefs["response_style"] = style
    save_config(Q.SCHED_FILE, prefs)
    return style


def graph_sign_in(method: str) -> None:
    """The Microsoft Graph sign-in, in this process (the terminal only)."""
    print("\n  Starting Graph API authentication...")
    print("  This will open a browser window for Microsoft login.\n")
    try:
        import importlib
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
        import graph_client as graph
        importlib.reload(graph)  # re-read the env vars set before the call
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


def google_sign_in() -> None:
    """The Google Calendar sign-in, in this process (the terminal only)."""
    secret_file = AUTH_DIR / "google_client_secret.json"
    if not secret_file.exists():
        print(f"\n  Place your Google OAuth2 client secret at:")
        print(f"    {secret_file}")
        print("  Then re-run: python3 scripts/install/user_init.py --phase auth")
        print("  See docs/SETUP.md 'Google OAuth app' for how to create the secret.")
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
        import google_calendar_client as gcal
        gcal.setup_all_accounts()
        print("  Google Calendar authentication successful!")
    except Exception as e:
        print(f"  Google Calendar auth failed: {e}")
        print("  You can set this up later.")


WEB_SERVE_LINE = (f"sudo tailscale serve --bg --https={Q.WEB_SERVE_PORT} --set-path "
                  f"{Q.WEB_BASE_PATH} http://127.0.0.1:{Q.WEB_PORT}{Q.WEB_BASE_PATH}")


def write_chat(values: dict, ctx: dict | None = None) -> dict | None:
    """The chat platform into ea-config.json's chat block: the web block
    for `web` (web_chat_block), the frontend for zulip or slack when the
    block names another. The hardening step copies the block into the
    owner-private chat.json, where the bot reads it, after each trusted
    value is typed back. Nothing on a profile with no chat (not asked)."""
    platform = values.get("chat-platform")
    if not platform:
        return None
    path = CONFIG_DIR / "ea-config.json"
    current = load_existing("ea-config.json").get("chat") if path.exists() else None
    current = current if isinstance(current, dict) else {}
    block = None
    if platform == "web":
        block = web_chat_block(values.get("web-chat-host", ""), values.get("web-chat-login", ""),
                               current)
    elif (current.get("frontend") or "slack") != platform:
        block = {**json.loads(json.dumps(current)), "frontend": platform}
    if block is not None:
        write_chat_config(block)
    if platform == "web":
        web = block["web"]
        print(f"  The web chat: {web.get('public_url') or 'no address yet (add web.public_url)'}"
              f", served on 127.0.0.1:{web['port']}{web['base_path']}.")
        print("  Publish it on your tailnet, in a terminal of your own:")
        print(f"    {WEB_SERVE_LINE}")
        print("  The hardening step copies these settings into the owner-private chat.json")
        print("  once you type each one back (docs/SETUP.md 'Web chat').")
    elif platform == "zulip":
        print("  Zulip: scripts/zulip_bootstrap.py prints the chat block (server, bot, owner,")
        print("  streams) for ea-config.json (docs/SETUP.md 'Zulip server').")
    else:
        print("  Slack: the tokens and ids come in the next phase (docs/SETUP.md 'Slack').")
    return block


SLACK_SECRETS = (("slack-bot-token", "SLACK_BOT_TOKEN"), ("slack-app-token", "SLACK_APP_TOKEN"))
SLACK_ENV = (("slack-channel-id", "EA_SLACK_CHANNEL_ID"),
             ("slack-channel-name-env", "EA_SLACK_CHANNEL_NAME"),
             ("slack-owner-id", "EA_SLACK_OWNER_ID"))


def write_auth(values: dict, ctx: dict, sign_in: bool = False) -> None:
    """The Graph settings into ~/.ea/.env and the client secret into the
    owner-private secrets.env, never into ~/.ea (ea-bot space once the
    service account exists); the Slack ids into ~/.ea/.env (0640) and the
    Slack tokens into secrets.env. With `sign_in` (the terminal) it also
    signs in when the answers say now."""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    method = values["graph-auth-method"]
    if method == "skip":
        print("  Skipped. You can authenticate later via:")
        print("    python3 preprocessor/graph_client.py")
    else:
        env_data = {"GRAPH_AUTH_METHOD": method, "GRAPH_CLIENT_ID": values["graph-client-id"]}
        tenant = values.get("graph-tenant-id") or ""
        if tenant:
            env_data["GRAPH_AUTHORITY"] = f"https://login.microsoftonline.com/{tenant}"
        if method == "confidential":
            env_data["GRAPH_REDIRECT_URI"] = values["graph-redirect-uri"]
            secret = values["graph-client-secret"]
            merge_env_file(private_secrets_file(), {"GRAPH_CLIENT_SECRET": secret})
            if sign_in:
                os.environ["GRAPH_CLIENT_SECRET"] = secret
        merge_env_file(EA_DIR / ".env", env_data)
        if sign_in and values.get("graph-sign-in-now"):
            for k, v in env_data.items():
                os.environ[k] = v  # so the in-process sign-in picks them up
            graph_sign_in(method)
        else:
            print("  Config saved. Sign in later with:")
            print("    python3 preprocessor/graph_client.py")

    if sign_in:
        if values.get("google-calendar-now"):
            google_sign_in()
        else:
            print("  Skipped.")

    if ctx.get("profile") in ("email", "review-only"):
        return  # no chat platform on this profile: no chat credentials to ask
    if ctx.get("chat") == "web":
        print("\n  --- Web chat ---\n")
        print("  The web chat needs no token: the bot serves it and writes its own pairing")
        print("  token beside its store in the owner-private directory.")
        return
    if ctx.get("chat") == "zulip":
        print("\n  --- Zulip ---\n")
        print("  The EA talks to you in private Zulip streams (chat block in")
        print("  ea-config.json). Put the bot's API key in the owner-private")
        print(f"  {private_dir() / 'secrets.env'} (mode 600) as ZULIP_API_KEY=...")
        print("  scripts/zulip_bootstrap.py prints that line for you.")
        return
    # The tokens go in the owner-private secrets.env; the ids in ~/.ea/.env
    # (mode 640, non-secret), as docs/SETUP.md 'The Slack environment files' says.
    secrets = {name: values[key] for key, name in SLACK_SECRETS
               if values.get(key, "skip") != "skip"}
    env_data = {name: values[key] for key, name in SLACK_ENV
                if values.get(key, "skip") != "skip"}
    if secrets:
        merge_env_file(private_secrets_file(), secrets)
    if env_data:
        merge_env_file(EA_DIR / ".env", env_data, mode=0o640)


def write_reimbursement(values: dict, ctx: dict | None = None) -> dict | None:
    """reimbursement-config.json, when the answers turn it on."""
    if not values["reimbursement-configure"]:
        print("  Skipped.")
        return None
    config = {
        "submitter": {
            "first_name": values["reimbursement-first-name"],
            "last_name": values["reimbursement-last-name"],
            "email": values["reimbursement-email"],
            "affiliation": values["reimbursement-affiliation"],
        },
        "form_url": values["reimbursement-form-url"],
        "receipts_dir": str(EA_DIR / "receipts"),
    }
    save_config("reimbursement-config.json", config)
    return config


def write_vip(values: dict, ctx: dict | None = None) -> list[str]:
    """vip_senders.txt: the addresses already there and the new ones."""
    vip_path = CONFIG_DIR / "vip_senders.txt"
    existing = read_vips()
    addresses = values["vip-addresses"]
    if not (addresses or existing):
        print("  No VIPs configured. The EA will learn them over time.")
        return []
    all_vips = sorted(set(existing + addresses))
    lines = [
        "# VIP senders -- one email per line.",
        "# These contacts get priority alerts and extended scheduling hours.",
        "# The EA adds entries when you say 'this person is very important' in Slack.",
        "",
    ] + all_vips
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    vip_path.write_text("\n".join(lines) + "\n")
    print(f"  -> Saved {vip_path} ({len(all_vips)} VIPs)")
    return all_vips


# push_notify's problem lines start with the setting they name.
PUSH_KEYS = (("ntfy_url", "push-ntfy-url"), ("topic", "push-topic"), ("click", "push-click"))


def write_push(values: dict, ctx: dict | None = None) -> dict | None:
    """The ntfy push for an install with no chat platform (preprocessor/
    push_notify.py): the settings go in the owner-private push.json,
    never in ea-config.json, so the EA cannot choose where reports go.
    push_notify.problems checks them; AnswerError names what it refused."""
    if not values["push-configure"]:
        print("  Skipped. docs/SETUP.md 'Phone push' says how to add it later.")
        return None
    settings = {
        "ntfy_url": values["push-ntfy-url"],
        "topic": values["push-topic"],
        "include_text": values["push-include-text"],
    }
    if values.get("push-click"):
        settings["click"] = values["push-click"]

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocessor"))
    import push_notify
    bad = push_notify.problems(settings)
    if bad:
        errors: dict = {}
        for line in bad:
            key = next((k for word, k in PUSH_KEYS if line.startswith(word)), "_phase")
            errors[key] = f"{errors[key]} {line}" if key in errors else line
        raise AnswerError(errors)
    for line in push_notify.warnings(settings):
        print(f"  note: {line}")

    priv = private_dir()
    priv.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = priv / push_notify.SETTINGS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    os.chmod(path, 0o600)
    print(f"  -> Saved {path}")
    print("  python3 preprocessor/push_notify.py test sends one push.")
    return settings


def apply_phase(pid: str, values: dict, ctx: dict, inbox: dict | None = None):
    """Write one phase's checked answers: the setup page's way in to the
    writers the terminal phases call. `inbox` is the inbox phase's
    result, when the autonomy phase follows it."""
    if pid == "autonomy":
        archive = inbox["archive_completes"] if inbox else archive_default(ctx)
        return write_autonomy(values, ctx, archive)
    if pid == "state":
        make_tree()
        return write_state()
    writer = {
        "profile": write_profile, "identity": write_identity,
        "scheduling": write_scheduling, "inbox": write_inbox,
        "groups": write_groups, "deadlines": write_deadlines, "style": write_style,
        "chat": write_chat, "auth": write_auth, "reimbursement": write_reimbursement, "vip": write_vip,
        "push": write_push,
    }[pid]
    return writer(values, ctx)


# ---------------------------------------------------------------------------
# The phases at the terminal: header, notes, questions, writer
# ---------------------------------------------------------------------------

def phase_profile(ctx: dict) -> dict:
    """The install profile and the README notice (setup.sh records the
    same two; /setup and the setup page ask them first)."""
    print_intro("profile", ctx)
    if ctx.get("acknowledged"):
        print(f"  The notice was accepted on {ctx.get('acknowledged_on')}.\n")
    return write_profile(ask_phase("profile", ctx), ctx)


def phase_identity(ctx: dict) -> dict:
    """Collect user identity and profile information."""
    print_intro("identity", ctx)
    return write_identity(ask_phase("identity", ctx), ctx)


def phase_scheduling(ctx: dict) -> dict:
    """Collect scheduling preferences."""
    print_intro("scheduling", ctx)
    return write_scheduling(ask_phase("scheduling", ctx), ctx)


def phase_inbox(ctx: dict) -> dict:
    """Configure inbox management philosophy."""
    print_intro("inbox", ctx)
    return write_inbox(ask_phase("inbox", ctx), ctx)


def phase_autonomy(ctx: dict, inbox_config: dict | None = None) -> dict:
    """Configure autonomy tiers."""
    print_intro("autonomy", ctx)
    return apply_phase("autonomy", ask_phase("autonomy", ctx), ctx, inbox_config)


def phase_groups(ctx: dict) -> dict:
    """Configure group email lists for noisy thread detection."""
    print_intro("groups", ctx)
    return write_groups(ask_phase("groups", ctx), ctx)


def phase_deadlines(ctx: dict) -> dict:
    """Configure deadline sender domain categories."""
    print_intro("deadlines", ctx)
    return write_deadlines(ask_phase("deadlines", ctx), ctx)


def phase_response_style(ctx: dict) -> dict:
    """Configure default response styles."""
    print_intro("style", ctx)
    return write_style(ask_phase("style", ctx), ctx)


def phase_chat(ctx: dict) -> dict:
    """The chat platform (the assistant profile only); the phases after it
    read the choice from `ctx`."""
    print_intro("chat", ctx)
    values = ask_phase("chat", ctx)
    write_chat(values, ctx)
    if values.get("chat-platform"):
        ctx["chat"] = values["chat-platform"]
    return values


def phase_auth(ctx: dict):
    """Guide user through authentication setup."""
    print_intro("auth", ctx)
    write_auth(ask_phase("auth", ctx), ctx, sign_in=True)


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


def phase_tone(out: Path | None = None, ctx: dict | None = None):
    """Run deep writing style analysis on sent mail.

    `out` takes the baseline JSON instead of the config dir: the caller that
    holds the mail token is not the user who writes ea-bot's config, so it
    computes here and hands the file to that user."""
    ctx = ctx or {}
    print_intro("tone", ctx)

    if not ask_phase("tone", ctx)["style-analysis-now"]:
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
# Phases 10-12: reimbursement, VIP senders, phone push
# ---------------------------------------------------------------------------

def phase_reimbursement(ctx: dict):
    """Configure reimbursement settings."""
    print_intro("reimbursement", ctx)
    write_reimbursement(ask_phase("reimbursement", ctx), ctx)


def phase_vip(ctx: dict):
    """Seed VIP senders list."""
    print_intro("vip", ctx)
    existing = read_vips()
    if existing:
        print(f"  Current VIPs: {', '.join(existing)}\n")
    write_vip(ask_phase("vip", ctx), ctx)


def phase_push(ctx: dict):
    """The ntfy push for an install with no chat platform."""
    print_intro("push", ctx)
    try:
        write_push(ask_phase("push", ctx), ctx)
    except AnswerError as e:
        for line in e.errors.values():
            print(f"  {line}")
        raise SystemExit("  push.json not written: fix the values above and run "
                         "python3 scripts/install/user_init.py --phase push again")


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
    write_state()


def write_state() -> int:
    """The empty state files a new install needs; one already there is
    kept. Returns how many it created."""
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
    return created


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
    global ANSWERS

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
        "--chat", choices=["web", "zulip", "slack"], default=None,
        help="Chat platform the EA talks to you on: web (the chat the bot serves "
             "itself, recommended), zulip or slack (default: the chat block in "
             "ea-config.json, else the chat phase asks)",
    )
    parser.add_argument("--web-host", default="",
                        help="This machine's tailnet name, for the web chat's public_url "
                             "(with --chat web)")
    parser.add_argument("--web-login", default="",
                        help="Your Tailscale login, the web chat's owner (with --chat web)")
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
    if (args.web_host or args.web_login) and args.chat != "web":
        parser.error("--web-host and --web-login go with --chat web")
    host_q, login_q = (next(q for q in Q.phase("chat")["questions"] if q["key"] == k)
                       for k in ("web-chat-host", "web-chat-login"))
    for flag, q, value in (("--web-host", host_q, args.web_host),
                           ("--web-login", login_q, args.web_login)):
        err = Q.problem(q, value.strip())
        if err:
            parser.error(f"{flag}: {err}")

    # Override EA_DIR if specified
    use_ea_dir(args.ea_dir or EA_DIR)

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
                       args.zulip_owner_id, args.web_host, args.web_login)
    if block:
        write_chat_config(block)

    # Style-only mode
    if args.out is not None and not (args.style_only or args.phase == "tone"):
        parser.error("--out goes with --phase tone or --style-only")
    if args.style_only:
        phase_tone(args.out)
        return

    # Single phase mode: the existing config gives the defaults and the
    # keys a phase does not ask about.
    if args.phase:
        ctx = context(args.chat)
        phase_map = {
            "profile": lambda: phase_profile(ctx),
            "identity": lambda: phase_identity(ctx),
            "scheduling": lambda: phase_scheduling(ctx),
            "inbox": lambda: phase_inbox(ctx),
            "autonomy": lambda: phase_autonomy(ctx),
            "groups": lambda: phase_groups(ctx),
            "deadlines": lambda: phase_deadlines(ctx),
            "style": lambda: phase_response_style(ctx),
            "chat": lambda: phase_chat(ctx),
            "auth": lambda: phase_auth(ctx),
            "tone": lambda: phase_tone(args.out, ctx),
            "reimbursement": lambda: phase_reimbursement(ctx),
            "vip": lambda: phase_vip(ctx),
            "push": lambda: phase_push(ctx),
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

    make_tree()

    # The existing configs give the defaults only with --update.
    ctx = context(args.chat, load=args.update)

    # Run phases
    phase_identity(ctx)
    phase_scheduling(ctx)
    inbox_config = phase_inbox(ctx)
    phase_autonomy(ctx, inbox_config)
    phase_groups(ctx)
    phase_deadlines(ctx)
    phase_response_style(ctx)
    phase_chat(ctx)
    phase_auth(ctx)
    phase_tone(ctx=ctx)
    phase_reimbursement(ctx)
    phase_vip(ctx)
    phase_push(ctx)
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
