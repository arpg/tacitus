"""
Slack notifier for EA preprocessor: the compatibility surface over
chat_notify.

Posts scheduling requests and priority alerts WITHOUT invoking Claude.
post_to_slack and scheduling_channel_key delegate to chat_notify, which
holds the quiet-hours gate and routes by the configured chat frontend
(Slack incoming webhooks, or the web chat when configured). Callers in
run-agent.sh, run-plugin.sh, quiet_hours, sentinel, calendar_reconciler
and the plugins keep importing this module unchanged.

This module carries the zero-token preprocessor alerts only.
"""

from typing import Optional

try:
    import chat_notify
except ImportError:
    from preprocessor import chat_notify

# The webhook file chat_notify reads: the owner-only private dir copy first,
# the ~/.ea/config copy only as a fallback. One lookup for every reader.
_webhooks_path = chat_notify._webhooks_path
WEBHOOKS_FILE = _webhooks_path()


def _load_webhooks() -> dict:
    return chat_notify._load_webhooks()


# Multi-post split marker. When this string appears in the text passed
# to post_to_slack, the text is split on the marker and each part is
# posted as its own top-level message (earlier parts land higher in
# scrollback). Skills that already emit the literal value keep working,
# so it must not be renamed.
SPLIT_MARKER = "---===FF_BRIEFING_SPLIT===---"


def post_to_slack(channel_key: str, text: str, blocks: Optional[list] = None,
                  priority: str = "normal", _bypass_quiet: bool = False) -> bool:
    """Post a message through chat_notify.post (quiet-hours gate, split,
    route by frontend). Returns True on success or deferral, False on a
    real send failure."""
    return bool(chat_notify.post(channel_key, text, blocks=blocks, priority=priority,
                                 _bypass_quiet=_bypass_quiet))


def scheduling_channel_key() -> str:
    """Route scheduling output to the dedicated scheduling channel when it
    is configured, else fall back to the main EA channel."""
    return chat_notify.scheduling_channel_key()


def calendar_channel_key() -> str:
    """Route calendar-protection warnings to the calendar target (the
    scheduling channel) when configured, else scheduling, else main."""
    return chat_notify.calendar_channel_key()


def health_channel_key() -> str:
    """Route health and boundary checks to the health channel when it is
    configured, else fall back to the main EA channel."""
    return chat_notify.health_channel_key()


def notify_scheduling_requests(scheduling_data: list, availability: list):
    """Post scheduling requests to the scheduling channel (or main EA channel)."""
    if not scheduling_data:
        return

    for req in scheduling_data:
        if req.get("staged_event"):
            continue  # an accepted time: its thread and Create card come from the bot
        slot_text = "\n".join(
            f"  • {s['day']}: {s['start'][11:16]} – {s['end'][11:16]}"
            for s in availability[:8]
        )

        text = (
            f"📅 *Scheduling request from {req['from_name'] or req['from']}*\n"
            f"Subject: {req['subject']}\n"
            f"Received: {req['received'][:10]}\n"
            f"Preview: _{req['body_preview'][:200]}_\n\n"
            f"*Free slots (all calendars merged):*\n"
            f"{slot_text}\n\n"
            f"Claude will propose specific times and draft a response shortly."
        )
        post_to_slack(scheduling_channel_key(), text)


def notify_priority_emails(priority_data: list):
    """Post priority alerts to #ea-assistant."""
    if not priority_data:
        return

    for email in priority_data:
        flags = []
        if email.get("is_vip"):
            flags.append("⭐ VIP")
        if email.get("is_urgent"):
            flags.append("🔴 Urgent")
        if email.get("is_flagged"):
            flags.append("🚩 Flagged")

        text = (
            f"{' '.join(flags)} *New priority email*\n"
            f"From: {email['from_name'] or email['from']}\n"
            f"Subject: {email['subject']}\n"
            f"Received: {email['received'][:16].replace('T', ' ')}\n"
            f"Preview: _{email['body_preview'][:200]}_\n\n"
            f"Reply *draft* to have Claude compose a response."
        )
        # Tag as urgent so it sorts to the top of the morning digest (and can
        # break through quiet hours if the user sets allow_urgent).
        post_to_slack("ea-assistant", text, priority="urgent")
