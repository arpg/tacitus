#!/usr/bin/env python3
"""Block Kit builder for the EA's Slack App Home tab.

The Home tab is the phone-native desk: ranked "here's where you left
off" cards with an Ask button that dispatches straight into the
resident (no marker messages, no event-delivery dependence — the
button click IS the trigger), link buttons to the working documents,
and a File-to select per unfiled ledger item that replaces the
webpage's drag-and-drop on mobile.

Pure functions only: the bot imports build_home_view() and publishes;
tests exercise the blocks without a Slack in sight.
"""

from __future__ import annotations

import json
from datetime import datetime

MAX_DESK = 6
MAX_UNFILED = 8
MAX_LINKS = 3
LOOSE_OPTION = "__loose__"


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_home_view(desk: list, unfiled: list, projects: list,
                    loose_count: int = 0,
                    generated_at: str | None = None) -> dict:
    """Return the views.publish payload's `view` dict."""
    blocks: list = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "Your desk", "emoji": True}},
        {"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"where you left off, most urgent first · "
                     f"updated {generated_at or datetime.now():%H:%M} · "
                     f"board: the Tailscale page"}]},
    ]

    for e in desk[:MAX_DESK]:
        meta = []
        if e.get("deadline"):
            dd = e.get("deadline_days")
            urgency = ("OVERDUE" if dd is not None and dd < 0
                       else f"{dd}d left" if dd is not None else "")
            meta.append(f"due {e['deadline']}"
                        + (f" · *{urgency}*" if urgency else ""))
        qd = e.get("quiet_days")
        if qd is not None:
            meta.append("active today" if qd == 0
                        else f"quiet {qd}d")
        ball = []
        if e.get("on_you"):
            ball.append(f"{e['on_you']} on you")
        if e.get("waiting"):
            ball.append(f"{e['waiting']} waiting on others")
        if e.get("scheduled"):
            ball.append(f"{e['scheduled']} scheduled later")
        if ball:
            meta.append(", ".join(ball))
        why = f"\n_{' · '.join(meta)}_" if meta else ""
        card_menu = {
            "type": "overflow",
            "action_id": "home_card_menu",
            "options": [
                {"text": {"type": "plain_text", "text": "📋 Post full brief"},
                 "value": json.dumps({"do": "brief", "project": e["id"]})},
                {"text": {"type": "plain_text", "text": "📝 Journal a note"},
                 "value": json.dumps({"do": "journal", "project": e["id"]})},
                {"text": {"type": "plain_text", "text": "😴 Mark dormant"},
                 "value": json.dumps({"do": "dormant", "project": e["id"]})},
            ],
        }
        # The #ea- badge: the project's Slack room, the same marker the
        # web board shows, linked so the card opens the room.
        chan = e.get("channel") or {}
        badge = ""
        if chan.get("name"):
            label = f"#{chan['name']}" + (" (archived)" if chan.get("archived") else "")
            badge = f"  <{chan['url']}|{label}>" if chan.get("url") else f"  `{label}`"
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f"*{_trim(e['name'], 60)}*{badge}{why}"},
                       "accessory": card_menu})
        # One context ELEMENT per line: each mrkdwn element carries its
        # own length budget, so a long tailnet resolver URL can never be
        # sliced mid-link by a shared cap. If a line would still blow the
        # element limit, the link is dropped before the text is; never
        # truncate inside <url|label> markup.
        ctx_lines = []
        if e.get("left_off"):
            ctx_lines.append(_trim("left off: " + e["left_off"], 140))
        for it in (e.get("items") or [])[:3]:
            due = f"  (due {it['deadline']})" if it.get("deadline") else ""
            desc = _trim(it["description"], 110)
            line = f"• <{it['email_link']}|{desc}>{due}" \
                if it.get("email_link") else f"• {desc}{due}"
            if len(line) > 250:
                line = _trim(f"• {desc}{due}", 250)
            ctx_lines.append(line)
        if e.get("open_items", 0) > 3:
            ctx_lines.append(f"…{e['open_items'] - 3} more open")
        if ctx_lines:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": ln} for ln in ctx_lines[:10]]})
        elements = []
        question = e.get("question") or (
            f"Where did we leave off on {e['name']}, and what's the next move?")
        elements.append({
            "type": "button",
            "action_id": "home_ask",
            "text": {"type": "plain_text",
                     "text": _trim("💬 " + question, 72), "emoji": True},
            "value": json.dumps({"project": e["id"],
                                 "question": question})[:1900],
        })
        elements.append({
            "type": "button",
            "action_id": "home_ask_custom",
            "text": {"type": "plain_text", "text": "✍️ Ask…", "emoji": True},
            "value": e["id"],
        })
        # Only web URLs become link buttons: Slack rejects the whole home
        # view for one file:// or mailto: url, and a project link list may
        # carry a local path. Those stay in the pack, off the card.
        web_links = [li for li in (e.get("links") or [])
                     if str(li.get("url") or "").lower().startswith(("http://", "https://"))]
        for li in web_links[:MAX_LINKS]:
            elements.append({
                "type": "button",
                "text": {"type": "plain_text",
                         "text": _trim("📄 " + (li.get("title") or "doc"), 40),
                         "emoji": True},
                "url": li["url"][:3000],
                "action_id": f"home_link_{abs(hash(li['url'])) % 10**8}",
            })
        blocks.append({"type": "actions", "elements": elements})
        blocks.append({"type": "divider"})

    if not desk:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": "_nothing urgent on the desk_ 🎉"}})

    if unfiled:
        blocks.append({"type": "header", "text": {
            "type": "plain_text",
            "text": f"Unfiled ({len(unfiled)})", "emoji": True}})
        options = [{"text": {"type": "plain_text",
                             "text": _trim(p["name"], 70)},
                    "value": p["id"]}
                   for p in projects[:98]]
        options.append({"text": {"type": "plain_text",
                                 "text": "🪁 keep loose (no project)"},
                        "value": LOOSE_OPTION})
        for it in unfiled[:MAX_UNFILED]:
            who = it.get("from_name") or it.get("from_email") or ""
            desc = _trim(it.get("description") or it["id"], 120)
            if it.get("email_link"):
                desc = f"<{it['email_link']}|{desc}>"
            if it.get("merge"):
                m = it["merge"]
                desc += (f"  🔗 _auto-merged: {m['entity']} "
                         f"×{m['count'] + 1}_")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"{desc}" + (f"\n_{who}_" if who else "")},
                "block_id": f"unfiled::{it['id']}"[:255],
            })
            blocks.append({
                "type": "actions",
                "block_id": f"unfiled-act::{it['id']}"[:255],
                "elements": [
                    {"type": "static_select",
                     "action_id": "home_file",
                     "placeholder": {"type": "plain_text",
                                     "text": "file to…"},
                     "options": options},
                    {"type": "button", "action_id": "home_ask_item",
                     "text": {"type": "plain_text", "text": "💬 Ask",
                              "emoji": True},
                     "value": it["id"]},
                    {"type": "button", "action_id": "home_item_done",
                     "text": {"type": "plain_text", "text": "✓ Done",
                              "emoji": True},
                     "value": it["id"],
                     "confirm": {
                         "title": {"type": "plain_text", "text": "Done?"},
                         "text": {"type": "plain_text",
                                  "text": "It leaves your list and its "
                                          "email is archived."},
                         "confirm": {"type": "plain_text", "text": "Done"},
                         "deny": {"type": "plain_text", "text": "Not done"}}},
                ],
            })
        if len(unfiled) > MAX_UNFILED:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": f"…and {len(unfiled) - MAX_UNFILED} more on the "
                         f"board's tray"}]})

    footer = [{"type": "mrkdwn",
               "text": f"🪁 {loose_count} loose"}] if loose_count else []
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": "home_ask_custom",
         "text": {"type": "plain_text", "text": "✍️ Ask the EA",
                  "emoji": True},
         "value": "__general__"},
        {"type": "button", "action_id": "home_refresh",
         "text": {"type": "plain_text", "text": "🔄 Refresh", "emoji": True}}]})
    if footer:
        blocks.append({"type": "context", "elements": footer})

    return {"type": "home", "blocks": blocks}


def parse_file_action(block_id: str, selected_value: str) -> dict:
    """(ledger_id, project|loose) out of a home_file interaction."""
    ledger_id = block_id.split("::", 1)[1] if "::" in block_id else ""
    return {"ledger_id": ledger_id,
            "loose": selected_value == LOOSE_OPTION,
            "project": None if selected_value == LOOSE_OPTION
            else selected_value}


def ask_modal(project: str | None = None, item: str | None = None,
              item_desc: str = "") -> dict:
    """views.open payload for the typed-question modal. Scope is a
    project, a single ledger item (its context rides along even with no
    project), or nothing (general ask)."""
    scoped_project = project and project != "__general__"
    if item:
        meta = json.dumps({"item": item})
        scope_line = f"about item: *{_trim(item_desc or item, 90)}*"
    elif scoped_project:
        meta = json.dumps({"project": project})
        scope_line = f"scoped to *{project}* — the project pack rides along"
    else:
        meta = "{}"
        scope_line = "no scope — general ask"
    return {
        "type": "modal",
        "callback_id": "home_ask_modal",
        "private_metadata": meta,
        "title": {"type": "plain_text", "text": "Ask the EA"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": scope_line}]},
            {"type": "input", "block_id": "q",
             "label": {"type": "plain_text", "text": "Message"},
             "element": {"type": "plain_text_input", "action_id": "text",
                         "multiline": True}},
        ],
    }


def journal_modal(project: str) -> dict:
    """views.open payload for the quick journal-note modal."""
    return {
        "type": "modal",
        "callback_id": "home_journal_modal",
        "private_metadata": project,
        "title": {"type": "plain_text", "text": "Journal a note"},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"project: *{project}*"}]},
            {"type": "input", "block_id": "note",
             "label": {"type": "plain_text", "text": "Note"},
             "element": {"type": "plain_text_input", "action_id": "text",
                         "multiline": True}},
            {"type": "input", "block_id": "kind", "optional": True,
             "label": {"type": "plain_text", "text": "Kind"},
             "element": {"type": "static_select", "action_id": "value",
                         "initial_option": {
                             "text": {"type": "plain_text", "text": "note"},
                             "value": "note"},
                         "options": [
                             {"text": {"type": "plain_text", "text": k},
                              "value": k}
                             for k in ("note", "decision", "milestone",
                                       "status")]}},
        ],
    }
