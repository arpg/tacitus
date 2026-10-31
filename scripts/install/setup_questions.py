"""The install questions, as data both front ends read.

`scripts/install/user_init.py` asks them at the terminal (or takes them
from `--answers FILE`); `scripts/install/setup_web.py` sends them to the
setup page and resolves what the page posts. Each phase is a dict:

    id        the phase name (`user_init.py --phase <id>`)
    header    the terminal's section header
    title     the page's screen title
    web       True when the page shows the phase
    intro     lines the terminal prints after the header (the page joins
              them into paragraphs); `{private_dir}` names the
              owner-private directory
    web_intro the page's intro, when the terminal's does not fit there
    questions the questions, in the order they are asked
    repeat    keys asked again, as one more entry, while `again` says yes
              (the terminal's loop; an answers file and the page give one)
    again     the terminal's "add another" question

Each question is a dict:

    key        the answer key (docs/install-testing.md lists them)
    label      the terminal prompt; `{other-key}` takes an earlier answer
    web_label  the page's label, when the prompt does not read well there
    kind       text | secret | yesno | list | int | time | choice
    default    a list of sources, the first with a value wins:
               ["ex", file, "dotted.path"]  the existing config file
               ["answer", key]              an earlier answer
               ["domain_of", key]           the domain of an address answer
               ["first_of", key]            the first item of a list answer
               ["ctx", name]                a fact of this install
               ["literal", value]
    example    a list hint, as a string or as sources
    required   an empty answer with no default is an error
    check      email | emails | domains | timezone | url | host | login
    min        the least an `int` takes
    choices    [[value, page text, alias, ...], ...]
    must       a yes/no that must be yes
    when       a condition, or a list of them that all hold:
               {"key": k, "is": v} | {"key": k, "in": [...]} |
               {"key": k, "not_in": [...]}, or "ctx" in place of "key"
    note       lines the terminal prints before the question (the page
               shows them as help)
    notice     the README's "Your responsibility" paragraph goes before it
    terminal_only  only the terminal asks it (it acts at once: a sign-in,
               a scan, a loop)

A value comes in the shape an answers file gives it: a yes/no is a JSON
boolean or a string starting with "y"; a list is a JSON list or a
comma-separated string; anything else is text. An absent or empty value
takes the default. `parse` turns a raw value into a typed one, `problem`
says what is wrong with it, and `walk` runs a phase's questions in order
with a front end's own way to answer each.
"""
from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
NOTICE_HEADING = "## Your responsibility"

TIERS = ("ask-first", "do-and-tell", "fire-and-forget")
# (task key, what the terminal calls it, the default tier when the user
# sets each tier), in the order autonomy-tiers.json lists them.
TASK_TYPES = (
    ("draft_email_response", "Drafting email responses", "ask-first"),
    ("send_scheduling_availability", "Sending scheduling availability", "ask-first"),
    ("upload_letter_of_recommendation", "Uploading letters of recommendation", "do-and-tell"),
    ("prep_reimbursement_email", "Preparing reimbursement emails", "ask-first"),
    ("create_calendar_hold", "Creating calendar holds/focus blocks", "do-and-tell"),
    ("archive_organize_inbox", "Archiving/organizing processed inbox items", "do-and-tell"),
    ("update_obligations_ledger", "Updating the obligations ledger", "fire-and-forget"),
    ("post_slack_summary", "Posting briefing summaries to Slack", "fire-and-forget"),
)

PROFILE_FILE = "user-profile.json"
SCHED_FILE = "scheduling-preferences.json"
AUTONOMY_FILE = "autonomy-tiers.json"


def _ex(file: str, path: str) -> list:
    return ["ex", file, path]


def _lit(value) -> list:
    return ["literal", value]


def _q(key: str, label: str, kind: str = "text", **kw) -> dict:
    return {"key": key, "label": label, "kind": kind, **kw}


_CHAT_PROFILE = {"ctx": "profile", "not_in": ["email", "review-only"]}
_CHAT_ASKED = [_CHAT_PROFILE, {"ctx": "chat", "not_in": ["zulip", "web"]}]
_WEB_CHOSEN = [_CHAT_PROFILE, {"key": "chat-platform", "is": "web"}]
# The web chat's defaults (docs/webchat.md, Config) and the HTTPS port
# docs/SETUP.md "Web chat" publishes it on with `tailscale serve`.
WEB_PORT = 8470
WEB_BASE_PATH = "/chat"
WEB_SERVE_PORT = 8444
_GRAPH_APP = {"key": "graph-auth-method", "not_in": ["skip"]}
_GRAPH_SECRET = {"key": "graph-auth-method", "in": ["confidential"]}


def _tier_question(i: int, task: str, desc: str, tier: str) -> dict:
    q = _q("tier-" + task.replace("_", "-"), f"  {desc}", "choice",
           web_label=desc, choices=[[t, t] for t in TIERS], default=[_lit(tier)],
           when={"key": "conservative-defaults", "is": False})
    if i == 0:
        q["note"] = ["\n  For each task type, choose: ask-first, do-and-tell, or fire-and-forget.\n"]
    return q


PHASES: list[dict] = [
    {
        "id": "profile", "web": True,
        "header": "Profile and Notice",
        "title": "Profile",
        "intro": [
            "  The profile decides what the EA does and which questions follow.",
            "  Start with email; move to assistant when you want the chat rooms",
            "  and the Send and Create buttons.",
            "",
        ],
        "questions": [
            _q("profile", "Install profile (email, review-only or assistant)", "choice",
               web_label="Install profile",
               choices=[
                   ["email", "email (start here): triage, the ledger and drafts in your voice; "
                             "no chat platform, briefings as files and a phone push"],
                   ["review-only", "review-only: email without drafting; nothing is written "
                                   "to the mailbox"],
                   ["assistant", "assistant: everything, plus the chat platform, the Send and "
                                 "Create buttons, the rooms and plugins (about half a day)"],
               ],
               default=[["ctx", "recorded_profile"], _lit("email")]),
            _q("acknowledged", "Have you read the notice above, and do you accept that what "
                               "the EA sends and files under your name is yours?", "yesno",
               web_label="I have read the notice and accept that what the EA sends and files "
                         "under my name is mine",
               notice=True, must=True, default=[_lit(False)],
               when={"ctx": "acknowledged", "is": False}),
        ],
    },
    {
        "id": "identity", "web": True,
        "header": "Phase 1: Identity & Profile",
        "title": "You",
        "intro": [
            "  Basic information about you. This is used for drafting emails,",
            "  scheduling, and letter composition.\n",
        ],
        "questions": [
            _q("full-name", "Full name", required=True, default=[_ex(PROFILE_FILE, "name")]),
            _q("work-email", "Primary work email", required=True, check="email",
               default=[_ex(PROFILE_FILE, "email")]),
            _q("role-description", "Role description (title, focus area)",
               default=[_ex(PROFILE_FILE, "role_description")]),
            _q("department", "Department", default=[_ex(PROFILE_FILE, "department")]),
            _q("university", "University/Organization", default=[_ex(PROFILE_FILE, "university")]),
            _q("office", "Office location (optional)", default=[_ex(PROFILE_FILE, "office")]),
            _q("slack-channel-name", "Slack channel name for EA communication",
               default=[_ex(PROFILE_FILE, "slack_channel_name"), _lit("ea-assistant")]),
            _q("bio-blurb", "Brief professional bio (for letter drafting, optional)",
               default=[_ex(PROFILE_FILE, "bio_blurb")]),
            _q("calendar-accounts", "Google Calendar email(s), comma-separated", "list",
               check="emails", example="you@gmail.com, shared@example.com",
               default=[_ex(PROFILE_FILE, "calendar_accounts")]),
            _q("institution-name", "Institution name",
               note=["\n  Your institution. CLAUDE.md names none; these answers are rendered",
                     "  into the install overlay that every session carries.\n"],
               default=[_ex(PROFILE_FILE, "institution.name"), ["answer", "university"]]),
            _q("institution-domains", "Mail domains that count as internal, comma-separated",
               "list", check="domains",
               example=[["domain_of", "work-email"], _lit("example.edu")],
               default=[_ex(PROFILE_FILE, "institution.domains"), ["domain_of", "work-email"]]),
            _q("institution-related-domains",
               "Sister-campus domains the admissions filter treats as internal (optional)",
               "list", check="domains", example="med.example.edu",
               default=[_ex(PROFILE_FILE, "institution.related_domains")]),
            _q("institution-safety-rail-domain",
               "Safety-rail domain (a first email outside it is never sent on its own)",
               default=[_ex(PROFILE_FILE, "institution.safety_rail_domain"),
                        ["first_of", "institution-domains"]]),
            _q("video-platform", "Video-meeting platform",
               default=[_ex(PROFILE_FILE, "institution.video.platform"), _lit("Zoom")]),
            _q("video-link", "Your personal meeting link on it (optional)", check="url",
               default=[_ex(PROFILE_FILE, "institution.video.link"),
                        _ex(PROFILE_FILE, "zoom_link")]),
        ],
    },
    {
        "id": "scheduling", "web": True,
        "header": "Phase 2: Scheduling Preferences",
        "title": "Scheduling",
        "intro": [
            "  These control how the EA proposes meeting times, protects your",
            "  focus blocks, and handles VIP contacts.\n",
        ],
        "questions": [
            _q("timezone", "Timezone (IANA format)", check="timezone",
               default=[_ex(SCHED_FILE, "timezone"), ["ctx", "detected_timezone"]]),
            _q("cluster-meetings", "Cluster meetings together (protect focus blocks)?", "yesno",
               note=[
                   "  Working hours, no-meeting days, lunch, and time-of-day preferences",
                   "  are NOT stored in config anymore. They live as tokened recurring",
                   "  events on your EA-Managed Google calendar, so you can drag/edit them",
                   "  any time without a config change. Tokens (leading word of the title):",
                   "    wh = working hours (meetings only inside these)",
                   "    nm = no meetings (protected)   p0/p1/p2 = hard/soft/preference",
                   "    wr = working remotely (informational)",
                   "  After setup, add to the EA-Managed calendar e.g. 'wh Working Hours'",
                   "  9-5 Mon-Fri, 'nm Lunch' 12-1, and 'nm No-meeting Friday' (all day).",
                   "  See skills/scheduling-rules.md for the full model.\n",
                   "\n  Meeting scheduling behavior:",
               ],
               default=[_ex(SCHED_FILE, "slot_selection.prefer_contiguous"), _lit(True)]),
            _q("default-duration-minutes", "Default meeting duration (minutes)", "int", min=5,
               default=[_ex(SCHED_FILE, "slot_selection.preferred_duration_minutes"), _lit(30)]),
            _q("vip-end-time", "Extended end time for VIP contacts", "time",
               note=["\n  VIP contacts get extended hours and more time options."],
               default=[_lit("17:00")]),
            _q("google-calendars", "Google Calendar email(s) that block availability", "list",
               check="emails", example="you@gmail.com",
               note=["\n  Calendar configuration:",
                     "  Your Outlook calendar is always included (via Graph API)."]),
        ],
    },
    {
        "id": "inbox", "web": True,
        "header": "Phase 3: Inbox Management Philosophy",
        "title": "Inbox",
        "intro": [
            "  The EA can treat your inbox in different ways:\n",
            "  INBOX ZERO: Your inbox is a task queue. Every message in it is a",
            "  potential action item. When you archive an email, it means 'handled.'",
            "  The EA reads your entire inbox on each run.\n",
            "  RELAXED: Your inbox is for reference. You may keep emails around",
            "  without them being action items. Obligations are tracked",
            "  independently via the ledger. The EA reads only recent messages.\n",
        ],
        "questions": [
            _q("inbox-zero", "Use Inbox Zero philosophy?", "yesno", default=[_lit(True)]),
            _q("archive-as-completion", "Enable archive-as-completion?", "yesno",
               when={"key": "inbox-zero", "is": True},
               note=["",
                     "  ARCHIVE-AS-COMPLETION: When you manually archive an email (from",
                     "  Outlook, your phone, etc.), the EA automatically marks the",
                     "  corresponding tracked item as 'done.'\n"],
               default=[_lit(True)]),
            _q("stale-thread-days", "Stale thread threshold (days)", "int", min=1,
               note=["", "  STALE THREAD THRESHOLD: How many days without a reply before the",
                     "  EA flags a conversation as potentially dropped?\n"],
               default=[_lit(3)]),
        ],
    },
    {
        "id": "autonomy", "web": True,
        "header": "Phase 4: Autonomy Tiers",
        "title": "Autonomy",
        "intro": [
            "  The EA has three autonomy levels:\n",
            "  ASK-FIRST:       Post what it wants to do in Slack, wait for approval.",
            "  DO-AND-TELL:     Do it, then tell you what it did in Slack.",
            "  FIRE-AND-FORGET: Do it silently, include in next briefing.\n",
        ],
        "questions": [
            _q("conservative-defaults",
               "Start with conservative defaults (all user-facing tasks = ask-first)?", "yesno",
               default=[_lit(True)]),
            *[_tier_question(i, *t) for i, t in enumerate(TASK_TYPES)],
            _q("auto-promote-threshold",
               "\nAuto-promote threshold (after N successes without edits, suggest promotion)",
               "int", min=1,
               default=[_ex(AUTONOMY_FILE, "escalation_rules.promote_after_n_successes.threshold"),
                        _lit(5)]),
        ],
    },
    {
        "id": "groups", "web": True,
        "header": "Phase 5: Group Email Lists",
        "title": "Group lists",
        "intro": [
            "  If you belong to group email lists (scouts, church, HOA, department",
            "  committees, etc.) that produce reply-all storms, the EA can detect",
            "  these and offer to summarize them instead of treating each reply as",
            "  a separate action item.\n",
        ],
        "questions": [
            _q("groups-configure", "Do you have group email lists to configure?", "yesno",
               default=[_lit(False)]),
            _q("group-name", "Group name (e.g., 'book_club', 'hoa')", required=True,
               when={"key": "groups-configure", "is": True}),
            _q("group-addresses", "  Email addresses in '{group-name}' group (comma-separated)",
               "list", check="emails", example="alice@gmail.com, bob@gmail.com",
               when={"key": "groups-configure", "is": True}),
        ],
        "repeat": ["group-name", "group-addresses"],
        "again": _q("group-add-another", "\nAdd another group?", "yesno",
                    default=[_lit(False)], terminal_only=True),
    },
    {
        "id": "deadlines", "web": True,
        "header": "Phase 6: Deadline Sender Categories",
        "title": "Deadline senders",
        "intro": [
            "  When the EA detects deadline keywords in emails, it can categorize",
            "  them by the sender's domain. This helps prioritize: a grant deadline",
            "  from nsf.gov is different from a utility bill from your HOA.\n",
            "  Examples:",
            "    example.edu   -> university",
            "    nsf.gov       -> grants",
            "    statefarm.com -> financial",
            "    irs.gov       -> taxes\n",
        ],
        "questions": [
            _q("deadlines-configure", "Configure deadline sender categories?", "yesno",
               default=[_lit(False)]),
            _q("deadline-domain", "Sender domain (e.g., 'nsf.gov')", required=True,
               check="domain", when={"key": "deadlines-configure", "is": True}),
            _q("deadline-category", "  Category for '{deadline-domain}'",
               default=[_lit("general")], when={"key": "deadlines-configure", "is": True}),
        ],
        "repeat": ["deadline-domain", "deadline-category"],
        "again": _q("deadline-add-another", "Add another?", "yesno",
                    default=[_lit(True)], terminal_only=True),
    },
    {
        "id": "style", "web": True,
        "header": "Phase 7: Response Style Defaults",
        "title": "Reply style",
        "intro": [
            "  How should the EA draft responses? These are defaults that can",
            "  be overridden per-contact based on writing style analysis.\n",
        ],
        "questions": [
            _q("style-default", "Default style",
               default=[_lit("Propose 2-3 specific times. Short, professional.")]),
            _q("style-internal", "Style for colleagues at your organization",
               default=[_lit("Friendly but efficient.")]),
            _q("style-external-academic", "Style for external academics",
               default=[_lit("Professional, slightly more formal. Include timezone.")]),
            _q("style-student", "Style for students", default=[_lit("Warm, brief.")]),
        ],
    },
    {
        "id": "chat", "web": True,
        "header": "Phase 7b: Chat Platform",
        "title": "Chat",
        "intro": [
            "  The assistant profile talks to you in a chat. The web chat is the",
            "  bot's own: an app on your phone over your tailnet, with no chat",
            "  server and nothing stored outside this machine. Zulip is a chat",
            "  server you run here; Slack keeps every message, draft and card on",
            "  Slack's servers. docs/INSTALL.md 'Choosing a chat platform' compares",
            "  them.\n",
        ],
        "questions": [
            _q("chat-platform", "Chat platform (web, zulip or slack)", "choice",
               web_label="Chat platform",
               choices=[
                   ["web", "web (recommended): the chat the bot serves itself, as an app on "
                           "your phone over your tailnet; no chat server", "1"],
                   ["zulip", "zulip: a Zulip server you run on this machine in Docker", "2"],
                   ["slack", "slack: a Slack workspace; Slack keeps every message, draft "
                             "and card", "3"],
               ],
               default=[["ctx", "chat_choice"], _lit("web")], when=_CHAT_PROFILE),
            _q("web-chat-host", "This machine's tailnet name (empty: add it later)",
               web_label="This machine's tailnet name (empty: add it later)",
               note=["\n  The web chat's address is https://<tailnet name>:"
                     f"{WEB_SERVE_PORT}{WEB_BASE_PATH}.",
                     "  `tailscale status --json` shows the name as Self.DNSName.\n"],
               check="host", example="host.tailnet.ts.net",
               default=[["ctx", "web_host"]], when=_WEB_CHOSEN),
            _q("web-chat-login", "Your Tailscale login (empty: pair by token)",
               web_label="Your Tailscale login (empty: pair by token)",
               note=["\n  A request from this login is you. `tailscale whois <this machine's",
                     "  tailnet IP>` shows it. Left empty, you pair a browser once with the",
                     "  token the bot writes to its private directory.\n"],
               check="login", example="you@example.edu",
               default=[["ctx", "web_login"]], when=_WEB_CHOSEN),
        ],
    },
    {
        "id": "auth", "web": True,
        "header": "Phase 8: Authentication Setup",
        "title": "Outlook and chat",
        "web_intro": [
            "  The EA reads Outlook through your organization's app registration.",
            "  Give its values here; the sign-in itself runs on the machine after",
            "  this page, and the last screen has the command. docs/AUTHENTICATION.md",
            "  says how to pick a method. With no app registration, choose skip:",
            "  docs/it-request.md asks IT for one, and the owner plugin",
            "  outlook-web-auth can supply the token instead.",
        ],
        "intro": [
            "  The EA needs access to your email and calendar. This phase",
            "  walks you through authenticating with each service.\n",
            "  --- Microsoft Graph API (Outlook) ---\n",
            "  Choose how the EA authenticates to Microsoft Graph:",
            "    1) confidential  Auth-code flow + client secret",
            "                     (app registration; public client flows OFF)",
            "    2) device_code   MSAL device code",
            "                     (app registration; public client flows ON)",
            "    3) interactive   MSAL browser sign-in",
            "                     (app registration; public-client redirect URI)",
            "    4) skip          Set up later",
            "  See docs/AUTHENTICATION.md for how to pick. With no app registration,",
            "  the owner plugin outlook-web-auth can supply the token (skip here, then",
            "  see https://github.com/arpg/tacitus-plugin-outlook-web-auth).\n",
        ],
        "questions": [
            _q("graph-auth-method", "Graph auth method [1-4]", "choice",
               web_label="How the EA signs in to Microsoft Graph",
               choices=[
                   ["confidential", "confidential: auth-code flow and a client secret "
                                    "(public client flows off)", "1"],
                   ["device_code", "device_code: a code you enter in any browser "
                                   "(public client flows on)", "2"],
                   ["interactive", "interactive: a browser sign-in on this machine", "3"],
                   ["skip", "skip: set up later", "4"],
               ],
               default=[_lit("1")]),
            _q("graph-client-id", "Application (client) ID", required=True, when=_GRAPH_APP),
            _q("graph-tenant-id", "Tenant ID (blank = multi-tenant 'common')", when=_GRAPH_APP),
            _q("graph-redirect-uri", "Redirect URI (Web platform)", check="url",
               default=[_lit("http://localhost:8400/callback")], when=_GRAPH_SECRET),
            _q("graph-client-secret", "Client secret value", "secret", required=True,
               when=_GRAPH_SECRET),
            _q("graph-sign-in-now", "Sign in to Microsoft Graph now?", "yesno",
               default=[_lit(True)], when=_GRAPH_APP, terminal_only=True),
            _q("google-calendar-now", "Set up Google Calendar now?", "yesno",
               note=["\n  --- Google Calendar ---\n"], default=[_lit(True)], terminal_only=True),
            _q("slack-bot-token", "Slack Bot Token (xoxb-..., or 'skip')", "secret",
               web_label="Slack bot token (xoxb-...; empty skips it)",
               note=["\n  --- Slack ---\n",
                     "  The EA communicates with you via a private Slack channel.",
                     "  You need a Bot Token (xoxb-...) and App Token (xapp-...).\n"],
               default=[_lit("skip")], when=_CHAT_ASKED),
            _q("slack-app-token", "Slack App Token (xapp-..., or 'skip')", "secret",
               web_label="Slack app token (xapp-...; empty skips it)",
               default=[_lit("skip")], when=_CHAT_ASKED),
            _q("slack-channel-id", "EA Slack Channel ID (C..., or 'skip')",
               default=[_lit("skip")], when=_CHAT_ASKED),
            _q("slack-channel-name-env", "EA Slack Channel Name (or 'skip')",
               default=[_lit("skip")], when=_CHAT_ASKED),
            _q("slack-owner-id", "Your Slack member ID (U..., or 'skip')",
               default=[_lit("skip")], when=_CHAT_ASKED),
        ],
    },
    {
        "id": "tone", "web": False,
        "header": "Phase 9: Writing Style Detection",
        "title": "Writing style",
        "intro": [
            "  This scans your last 90 days of sent email to learn your",
            "  communication style: greetings, closings, formality level,",
            "  and per-contact tone. This helps the EA draft replies that",
            "  sound like you.\n",
        ],
        "questions": [
            _q("style-analysis-now", "Run writing style analysis now?", "yesno",
               default=[_lit(True)], terminal_only=True),
        ],
    },
    {
        "id": "reimbursement", "web": True,
        "header": "Phase 10: Reimbursement Configuration (Optional)",
        "title": "Reimbursement",
        "intro": [
            "  If your institution has a travel reimbursement form, the EA can",
            "  help collect receipts and prepare submissions.\n",
        ],
        "questions": [
            _q("reimbursement-configure", "Configure reimbursement automation?", "yesno",
               default=[_lit(False)]),
            _q("reimbursement-first-name", "Your first name", required=True,
               when={"key": "reimbursement-configure", "is": True}),
            _q("reimbursement-last-name", "Your last name", required=True,
               when={"key": "reimbursement-configure", "is": True}),
            _q("reimbursement-email", "Submission email (may differ from work email)",
               check="email", when={"key": "reimbursement-configure", "is": True}),
            _q("reimbursement-affiliation", "Affiliation", default=[_lit("Faculty / Staff")],
               when={"key": "reimbursement-configure", "is": True}),
            _q("reimbursement-form-url", "Reimbursement form URL (or leave empty if none)",
               check="url", when={"key": "reimbursement-configure", "is": True}),
        ],
    },
    {
        "id": "vip", "web": True,
        "header": "Phase 11: VIP Senders",
        "title": "VIP senders",
        "intro": [
            "  VIP senders get priority alerts and extended scheduling hours.",
            "  You can seed this list now, or let the EA learn VIPs over time",
            "  when you say 'this person is very important' in Slack.\n",
        ],
        "questions": [
            _q("vip-addresses", "VIP email addresses (comma-separated, or leave empty)", "list",
               check="emails", example="dean@example.edu, program-officer@example.org"),
        ],
    },
    {
        "id": "push", "web": True,
        "header": "Phase 12: Phone Push (Optional)",
        "title": "Phone push",
        "intro": [
            "  An email or review-only install has no chat channel: a report is",
            "  a file in ~/.ea/state/reports/. With the ntfy app on your phone,",
            "  each report also sends a short push. The settings are written to",
            "  {private_dir}/push.json (owner-only, mode 600).\n",
        ],
        "questions": [
            _q("push-configure", "Send each report to your phone through ntfy?", "yesno",
               default=[_lit(False)]),
            _q("push-ntfy-url", "ntfy server URL", default=[_lit("https://ntfy.sh")],
               when={"key": "push-configure", "is": True}),
            _q("push-topic", "Topic (a long random name; on a public server it is the password)",
               required=True, when={"key": "push-configure", "is": True}),
            _q("push-include-text", "Include the report text in the push?", "yesno",
               default=[_lit(False)], when={"key": "push-configure", "is": True}),
            _q("push-click", "Web board URL to open from the push (optional)",
               when={"key": "push-configure", "is": True}),
        ],
    },
]

_BY_ID = {p["id"]: p for p in PHASES}
TEXT_KINDS = ("text", "secret", "int", "time", "choice")
MAX_TEXT = 2000
MAX_ITEMS = 50
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_DOMAIN = re.compile(r"^@?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+\.?$")
_LOGIN = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+$")
_LABEL_REF = re.compile(r"\{([a-z0-9-]+)\}")

MESSAGES = {
    "required": "An answer is required.",
    "line": "One line of text, with no control characters.",
    "long": f"At most {MAX_TEXT} characters.",
    "items": f"At most {MAX_ITEMS} items.",
    "int": "A whole number of at least {min}.",
    "time": "A time as HH:MM, such as 09:00 or 17:30.",
    "choice": "One of: {choices}.",
    "email": "An email address, such as name@example.edu.",
    "emails": "Email addresses separated by commas; {bad} is not one.",
    "domain": "A mail domain, such as example.edu.",
    "domains": "Mail domains separated by commas, such as example.edu; {bad} is not one.",
    "timezone": "A timezone name, such as Europe/Berlin.",
    "url": "A web address starting with https:// or http://.",
    "host": "A host name, such as host.tailnet.ts.net, with no https:// and no port.",
    "login": "A login as Tailscale shows it, such as you@example.edu.",
    "must": "The install goes on only after you accept the notice.",
}


def phase(pid: str) -> dict:
    return _BY_ID[pid]


def web_phases() -> list[str]:
    return [p["id"] for p in PHASES if p.get("web")]


def questions(pid: str, front: str = "terminal") -> list[dict]:
    """The phase's questions for a front end: the page leaves out the ones
    only the terminal can act on."""
    qs = phase(pid)["questions"]
    return [q for q in qs if front == "terminal" or not q.get("terminal_only")]


def notice_text(readme: Path = README) -> str:
    """The README's "Your responsibility" paragraph, word for word, as one
    line per paragraph."""
    try:
        lines = readme.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == NOTICE_HEADING:
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            out.append(line)
    return "\n".join(paragraphs(out))


def paragraphs(lines: list[str]) -> list[str]:
    """Terminal lines as page paragraphs: a blank line ends a paragraph, a
    line indented four spaces or more (a list row) stands alone, the rest
    join with spaces."""
    out: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            out.append(" ".join(cur))
            cur.clear()
    for raw in lines:
        for line in str(raw).split("\n"):
            if not line.strip():
                flush()
            elif line.startswith("    "):
                flush()
                out.append(line.strip())
            else:
                cur.append(line.strip())
    flush()
    return out


# -- conditions and defaults ----------------------------------------------

def holds(cond, values: dict, ctx: dict) -> bool:
    if not cond:
        return True
    if isinstance(cond, list):
        return all(holds(c, values, ctx) for c in cond)
    got = ctx.get(cond["ctx"]) if "ctx" in cond else values.get(cond["key"])
    if "is" in cond:
        return got == cond["is"]
    if "in" in cond:
        return got in cond["in"]
    if "not_in" in cond:
        return got not in cond["not_in"]
    return True


def applies(q: dict, values: dict, ctx: dict) -> bool:
    return holds(q.get("when"), values, ctx)


def _dig(data, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _source(src: list, values: dict, ctx: dict):
    kind = src[0]
    if kind == "literal":
        return src[1]
    if kind == "ex":
        return _dig((ctx.get("ex") or {}).get(src[1]) or {}, src[2])
    if kind == "answer":
        return values.get(src[1])
    if kind == "domain_of":
        addr = str(values.get(src[1]) or "")
        return addr.rsplit("@", 1)[-1].lower() if "@" in addr else None
    if kind == "first_of":
        items = values.get(src[1]) or []
        return items[0] if isinstance(items, list) and items else None
    if kind == "ctx":
        got = ctx.get(src[1])
        return got() if callable(got) else got
    return None


def _first(sources, values: dict, ctx: dict):
    """The first source with a value: None, "" and [] have none; False and
    0 are values."""
    for src in sources or []:
        got = _source(src, values, ctx)
        if got is not None and got != "" and got != []:
            return got
    return None


def default_of(q: dict, values: dict, ctx: dict):
    """The default, typed by kind: a bool, a list, or a string (the text a
    terminal prompt shows in brackets)."""
    got = _first(q.get("default"), values, ctx)
    if q["kind"] == "yesno":
        return bool(got)
    if q["kind"] == "list":
        if got is None:
            return []
        return [str(i) for i in got] if isinstance(got, list) else [str(got)]
    return "" if got is None else str(got)


def example_of(q: dict, values: dict, ctx: dict) -> str:
    ex = q.get("example") or ""
    if isinstance(ex, list):
        got = _first(ex, values, ctx)
        return "" if got is None else str(got)
    return ex


def label_of(q: dict, values: dict) -> str:
    """The terminal prompt, `{key}` taking that earlier answer."""
    return _LABEL_REF.sub(lambda m: str(values.get(m.group(1), "")), q["label"])


def default_hint(q: dict) -> str:
    """What an empty answer takes, when that is an earlier answer."""
    names = {x["key"]: x for p in PHASES for x in p["questions"]}
    for src in q.get("default") or []:
        if src[0] in ("answer", "domain_of", "first_of") and src[1] in names:
            other = (names[src[1]].get("web_label") or names[src[1]]["label"]).strip()
            what = {"answer": "your answer to", "domain_of": "the domain of",
                    "first_of": "the first of"}[src[0]]
            return f"Left empty, it takes {what} \"{other}\"."
    return ""


# -- parsing and checks ---------------------------------------------------

def parse(kind: str, raw, default=None):
    """A raw answer as the answers file gives it, typed: bool for yesno, a
    list of strings for list (no default applied: the caller's fallback),
    stripped text otherwise, the default when absent or empty."""
    if kind == "yesno":
        if raw is None or raw == "":
            return bool(default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower().startswith("y")
    if kind == "list":
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    text = "" if raw is None else str(raw).strip()
    if text:
        return text
    return "" if default is None else str(default)


def _choice(q: dict, text: str) -> str | None:
    for row in q.get("choices") or []:
        if text == row[0] or text in row[2:]:
            return row[0]
    return None


def problem(q: dict, value) -> str | None:
    """What is wrong with a parsed answer, or None. An empty optional
    answer is never wrong."""
    kind, check = q["kind"], q.get("check")
    if kind == "yesno":
        return MESSAGES["must"] if q.get("must") and value is not True else None
    if kind == "list":
        if len(value) > MAX_ITEMS:
            return MESSAGES["items"]
        for item in value:
            if _CONTROL.search(item) or len(item) > MAX_TEXT:
                return MESSAGES["line"]
            if check == "emails" and not _EMAIL.match(item):
                return MESSAGES["emails"].format(bad=item)
            if check == "domains" and not _DOMAIN.match(item):
                return MESSAGES["domains"].format(bad=item)
        return None
    text = str(value)
    if not text:
        return MESSAGES["required"] if q.get("required") else None
    if _CONTROL.search(text):
        return MESSAGES["line"]
    if len(text) > MAX_TEXT:
        return MESSAGES["long"]
    if kind == "int":
        if not re.fullmatch(r"\d{1,6}", text) or int(text) < q.get("min", 0):
            return MESSAGES["int"].format(min=q.get("min", 0))
    elif kind == "time":
        if not _TIME.match(text):
            return MESSAGES["time"]
    elif kind == "choice":
        if _choice(q, text) is None:
            names = [", ".join([r[0], *r[2:]]) for r in q.get("choices") or []]
            return MESSAGES["choice"].format(choices="; ".join(names))
    if check == "email" and not _EMAIL.match(text):
        return MESSAGES["email"]
    if check == "domain" and not _DOMAIN.match(text):
        return MESSAGES["domain"]
    if check == "timezone" and not _zone_ok(text):
        return MESSAGES["timezone"]
    if check == "url" and not re.match(r"^https?://[^\s/]+", text):
        return MESSAGES["url"]
    if check == "host" and not _HOST.match(text):
        return MESSAGES["host"]
    if check == "login" and not _LOGIN.match(text):
        return MESSAGES["login"]
    return None


def _zone_ok(text: str) -> bool:
    """An IANA zone name the timezone database knows; the shape alone on
    a machine with no database to ask."""
    if not _ZONE.match(text):
        return False
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(text)
        return True
    except Exception:  # noqa: BLE001 - an unknown key or no database
        try:
            return not zoneinfo.available_timezones()
        except Exception:  # noqa: BLE001
            return True


def typed(q: dict, value):
    """A checked answer in the type its writer takes: an int, a choice's
    own value, else the value as it is."""
    if q["kind"] == "int" and value != "":
        return int(value)
    if q["kind"] == "choice" and value != "":
        return _choice(q, str(value))
    return value


# -- walking a phase ------------------------------------------------------

def walk(pid: str, ctx: dict, answer, front: str = "terminal", again=None) -> dict:
    """Ask a phase's questions in order and return {key: typed answer}.

    `answer(q, default, values)` is the front end's way to answer one
    question; it returns the typed value. A question whose condition
    fails is not asked. The repeat block is asked once, then once more
    for each time `again(values)` says yes (the terminal's loop); every
    entry is kept, in order, under "_entries"."""
    ph = phase(pid)
    rep = set(ph.get("repeat") or [])
    values: dict = {}
    entries: list[dict] = []
    for q in questions(pid, front):
        if q["key"] in rep or not applies(q, values, ctx):
            continue
        values[q["key"]] = answer(q, default_of(q, values, ctx), values)
    block = [q for q in questions(pid, front) if q["key"] in rep]
    if block and all(applies(q, values, ctx) for q in block):
        while True:
            entry: dict = {}
            for q in block:
                scope = {**values, **entry}
                entry[q["key"]] = answer(q, default_of(q, scope, ctx), scope)
            entries.append(entry)
            if not entries[1:]:
                values.update(entry)
            if again is None or not again(values):
                break
    values["_entries"] = entries
    return values


def resolve(pid: str, raw: dict, ctx: dict, front: str = "web",
            used: set | None = None) -> tuple[dict, dict]:
    """(values, errors) for one phase from an answers mapping, the way an
    answers file is read: every applicable question takes its key, an
    absent or empty one the default. Every error is kept, by key."""
    errors: dict = {}

    def answer(q, default, values):
        key = q["key"]
        if used is not None:
            used.add(key)
        value = parse(q["kind"], raw.get(key), default)
        if q["kind"] == "list" and not value:
            value = list(default)
        err = problem(q, value)
        if err:
            errors[key] = err
            return value
        return typed(q, value)
    return walk(pid, ctx, answer, front), errors


# -- the page's view ------------------------------------------------------

def _web_when(cond):
    """A condition without its install facts (already decided here), or
    None when nothing is left for the page to check."""
    if not cond:
        return None
    conds = cond if isinstance(cond, list) else [cond]
    left = [c for c in conds if "key" in c]
    return left or None


def describe(ctx: dict) -> list[dict]:
    """The page's phases: each web phase with the questions this install
    asks (a question that depends on the profile or the chat platform is
    in or out here), the help from the terminal's notes, and what an
    empty answer takes. A phase that asks this install nothing (the chat
    platform on a profile with no chat) is left out."""
    out = []
    for ph in PHASES:
        if not ph.get("web"):
            continue
        qs = []
        for q in questions(ph["id"], "web"):
            conds = q.get("when") or []
            conds = conds if isinstance(conds, list) else [conds]
            if not all(holds(c, {}, ctx) for c in conds if "ctx" in c):
                continue
            row = {
                "key": q["key"], "kind": q["kind"],
                "label": (q.get("web_label") or q["label"]).strip(),
                "help": paragraphs(q.get("note") or []),
                "required": bool(q.get("required")),
                "when": _web_when(q.get("when")),
                "example": example_of(q, {}, ctx),
                "hint": default_hint(q),
                "check": q.get("check") or "",
            }
            if q["kind"] == "choice":
                row["choices"] = [{"value": r[0], "label": r[1]} for r in q["choices"]]
            if q["kind"] == "int":
                row["min"] = q.get("min", 0)
            if q.get("must"):
                row["must"] = True
            if q.get("notice"):
                row["notice"] = notice_text()
            qs.append(row)
        if not qs:
            continue
        intro = [line.replace("{private_dir}", str(ctx.get("private_dir") or "~/.ea-private"))
                 for line in ph.get("web_intro") or ph.get("intro") or []]
        if ph["id"] == "vip" and ctx.get("vips"):
            intro.append(f"  Current VIPs: {', '.join(ctx['vips'])}. They stay; add more here.")
        out.append({"id": ph["id"], "title": ph["title"], "intro": paragraphs(intro),
                    "questions": qs})
    return out


def current(ctx: dict) -> dict:
    """Every web question's value now: the existing config, else the
    default (the `--update` view). A secret is never sent back."""
    values: dict = {}
    for pid in web_phases():
        for q in questions(pid, "web"):
            got = default_of(q, values, ctx)
            if q["kind"] == "secret":
                got = ""
            elif q["kind"] in ("int", "choice") and got != "" and problem(q, got) is None:
                got = typed(q, got)
            values[q["key"]] = got
    return values
