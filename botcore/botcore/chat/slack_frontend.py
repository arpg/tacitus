"""The Slack frontend: bolt and WebClient behind the ChatFrontend interface.

SlackFrontend keeps today's Slack behaviour byte for byte. It owns the
bolt App (Socket Mode) and the WebClient; every Web API call goes through
`self.client`. Inbound events become neutral types (`_inbound`,
`_action_event`, `_view_event`) and reach the core through the handler
registry. ConfirmationCards render to the exact Block Kit the bot built
before the card model existed; tests/fixtures/slack_cards holds that
Block Kit as goldens.

Error policy: a Web API failure raises, as the bare client call did,
except where the bot already swallowed it: react and unreact return
False, replace_card_with_status returns False, dm_channel returns "",
archived_map returns {}, download returns None, room_url falls back to
the app_redirect link and channel_info reports exists=False for
channel_not_found. Room calls map Slack's error strings to the typed
ChatError family.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import card_text as _ct
from . import files as _files

from .base import (
    AlreadyArchived,
    ChatError,
    ChatFrontend,
    Handler,
    Key,
    Limits,
    NotArchived,
    NotPermitted,
    RoomNameTaken,
)
from .cards import CardAction, CardField, CardSelect, ConfirmationCard
from .types import (
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
    ViewEvent,
)

log = logging.getLogger("ea-chat-bot")

# Slack caps a mrkdwn text object at 3000 characters; the status line and
# the room picker's head are cut below that, the card body at 2800.
STATUS_CHARS = 2900
BODY_CHARS = 2800
# Slack refuses a message with more than 50 blocks.
MAX_BLOCKS = 50
LABEL_CHARS = 75
BLOCK_ID_CHARS = 255
SELECT_OPTIONS_MAX = 100

QUOTED_LABEL = _ct.QUOTED_LABEL

ELLIPSIS = "\u2026"
EMDASH = "\u2014"


def _err_text(e: Exception) -> str:
    """The error text the bot's substring checks read: str(e) plus the
    API's error code when the exception carries a response."""
    text = str(e)
    resp = getattr(e, "response", None)
    try:
        code = resp.get("error") if resp is not None else None
    except Exception:
        code = None
    if code and code not in text:
        text = f"{text} ({code})"
    return text


def _ts_float(ts: Any) -> float | None:
    try:
        val = float(ts)
    except (TypeError, ValueError):
        return None
    return val if val else None


def _resp_get(resp: Any, key: str, default: Any = None) -> Any:
    """resp.get for a dict or a SlackResponse, the bot's
    `resp.get(k) if isinstance(resp, dict) else resp.data.get(k)` idiom."""
    if isinstance(resp, dict):
        return resp.get(key, default)
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        return data.get(key, default)
    try:
        return resp.get(key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _card_mrkdwn(text: str) -> str:
    """Card text the bot or a plugin template wrote (a title, a line, the
    footer): invisible characters marked, each card_text.inert span (a
    value from the model or mail) escaped so it renders as written."""
    return _ct.slack_inert(_ct.clean(text, keep_inert=True))


def _field_value(f: CardField) -> str:
    """A field's value as inert text: one line, cut at the field's limit
    with a marker, Slack's control characters escaped, so no link,
    mention or second field line can form from it."""
    return _ct.slack_escape(_ct.one_line(_ct.strip_inert(f.value), _ct.field_limit(f.label)))


def _field_text(f: CardField) -> str:
    return f"*{_ct.slack_escape(_ct.one_line(f.label, 60))}:* {_field_value(f)}"


def _body_mrkdwn(text: str) -> str:
    """Draft body text for a section: invisible characters marked and
    Slack's control characters escaped, so the body keeps its lines and
    its emphasis but no link with hidden target, mention or ping."""
    return _ct.slack_escape(_ct.clean(text))


def _fields_section(fields: list[CardField]) -> dict:
    return {"type": "section",
            "fields": [{"type": "mrkdwn", "text": _field_text(f)} for f in fields]}


def _divider() -> dict:
    return {"type": "divider"}


def _value_str(value: dict | str) -> str:
    """A button value: a str as given, anything else as json.dumps with
    the default separators, so a dict keeps its insertion order."""
    return value if isinstance(value, str) else json.dumps(value)


def _button(a: CardAction, cut: int | None = None) -> dict:
    label = _ct.one_line(_ct.strip_inert(a.label), 0)
    label = label[:cut] if cut else label
    btn = {"type": "button",
           "text": {"type": "plain_text", "text": label, "emoji": True},
           "action_id": a.id,
           "value": _value_str(a.value)}
    if a.style:
        btn["style"] = a.style
    return btn


def _actions_block(card: ConfirmationCard, elements: list[dict]) -> dict:
    block: dict = {"type": "actions"}
    if card.block_id:
        block["block_id"] = card.block_id[:BLOCK_ID_CHARS]
    block["elements"] = elements
    return block


def _footer(card: ConfirmationCard) -> list[dict]:
    if not card.footer:
        return []
    return [{"type": "context", "elements": [{"type": "mrkdwn",
                                              "text": _card_mrkdwn(card.footer)}]}]


def _split_field(card: ConfirmationCard, label: str) -> tuple[CardField | None, list[CardField]]:
    """The first field with this label, and every other field in order."""
    head = next((f for f in card.fields if f.label == label), None)
    rest = [f for f in card.fields if f is not head]
    return head, rest


def _body_text(body: str) -> str:
    text = _body_mrkdwn(body)[:BODY_CHARS] if body else "_(empty body)_"
    if len(_body_mrkdwn(body or "")) > BODY_CHARS:
        text += "\n_(truncated)_"
    return text


def _chunks(text: str, size: int) -> list[str]:
    """`text` in pieces of at most `size` characters, cut at line ends where
    a line allows it. Joined, the pieces are the text; a piece of only
    whitespace is left out, since Slack refuses an empty section."""
    out, cur = [], ""
    for line in (text or "").splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


def _quote_lines(text: str) -> str:
    return "\n".join(f">{ln}" for ln in (text or "").splitlines())


def _title_and_lines(card: ConfirmationCard) -> list[dict]:
    return [_section(_card_mrkdwn(card.title))] + \
        [_section(_card_mrkdwn(line)) for line in card.lines]


def _draft_head(card: ConfirmationCard) -> list[dict]:
    subject, rest = _split_field(card, "Subject")
    blocks = _title_and_lines(card)
    if rest:
        blocks.append(_fields_section(rest))
    if subject is not None:
        blocks.append(_section(_field_text(subject)))
    return blocks


def _draft_tail(card: ConfirmationCard, extra: list[dict] = ()) -> list[dict]:
    buttons = [_button(a) for a in card.actions] + list(extra)
    return [_divider(), _actions_block(card, buttons)] + _footer(card)


SHOW_QUOTED_ACTION = "show_quoted"


def _show_quoted_button(card: ConfirmationCard) -> list[dict]:
    """The Show thread button, carrying the draft's own value so the tap
    reads the same draft; none when no action names one."""
    value = next((a.value for a in card.actions
                  if isinstance(a.value, dict) and a.value.get("draft_id")), None)
    if value is None:
        return []
    return [{"type": "button", "text": {"type": "plain_text", "text": ":scroll: Show thread",
                                        "emoji": True},
             "action_id": SHOW_QUOTED_ACTION, "value": _value_str(value)}]


def _full_draft_blocks(card: ConfirmationCard) -> list[dict]:
    """The draft card with the whole body, split across sections of 2800.
    The quoted thread (thousands of characters on a long one) is one line
    under its label and a Show thread button that posts it under the card,
    as Zulip folds it into a spoiler."""
    body = [_section(c) for c in _chunks(_body_mrkdwn(card.body), BODY_CHARS)] or \
        [_section("_(empty body)_")]
    quoted, extra = [], []
    if (card.quoted or "").strip():
        extra = _show_quoted_button(card)
        how = "Tap *Show thread* to read it." if extra else "Open the draft to read it."
        quoted = [_section(f"*{QUOTED_LABEL}:* {len(card.quoted):,} characters, "
                           f"hidden. {how}")]
    return _draft_head(card) + body + quoted + _draft_tail(card, extra)


HIDE_QUOTED_ACTION = "hide_quoted"


def toggle_quoted_blocks(blocks: list[dict], quoted: str, show: bool) -> list[dict] | None:
    """A draft card's posted blocks with the quoted thread opened under its
    label (show) or folded back to one line, and the button flipped; None
    for a card without the label. An opened thread is cut to what fits in
    one message."""
    def _is_label(b):
        return b.get("type") == "section" and \
            str((b.get("text") or {}).get("text", "")).startswith(f"*{QUOTED_LABEL}:*")
    i = next((n for n, b in enumerate(blocks) if _is_label(b)), None)
    if i is None:
        return None
    j = i + 1
    while j < len(blocks) and str(blocks[j].get("block_id", "")).startswith("quoted_"):
        j += 1
    head, tail = blocks[:i], [dict(b) for b in blocks[j:]]
    secs = []
    if show:
        label = _section(f"*{QUOTED_LABEL}:*")
        chunks = _chunks(_quote_lines(_body_mrkdwn(quoted)), BODY_CHARS)
        room = max(MAX_BLOCKS - len(head) - len(tail) - 2, 0)
        secs = [dict(_section(c), block_id=f"quoted_{k}") for k, c in enumerate(chunks[:room])]
        if len(chunks) > room:
            secs.append(dict(_section("_(the rest of the thread is longer than a card shows; "
                                      "open the draft to read it)_"), block_id="quoted_cut"))
    else:
        label = _section(f"*{QUOTED_LABEL}:* {len(quoted):,} characters, hidden. "
                         "Tap *Show thread* to read it.")
    for b in tail:
        if b.get("type") == "actions":
            b["elements"] = [dict(e) for e in b.get("elements") or []]
            for e in b["elements"]:
                if e.get("action_id") in (SHOW_QUOTED_ACTION, HIDE_QUOTED_ACTION):
                    e["action_id"] = HIDE_QUOTED_ACTION if show else SHOW_QUOTED_ACTION
                    e["text"] = {"type": "plain_text", "emoji": True,
                                 "text": ":scroll: Hide thread" if show else ":scroll: Show thread"}
    return head + [label] + secs + tail


def quoted_thread_text(quoted: str) -> str:
    """The quoted thread as one message, quoted; Slack folds a long one
    behind its own Show more."""
    return f"*{QUOTED_LABEL}:*\n" + _quote_lines(_body_mrkdwn(quoted))[:39000]


def draft_card_fits(card: ConfirmationCard) -> bool:
    """True when the whole draft card is one message: Slack takes at most
    50 blocks."""
    return len(_full_draft_blocks(card)) <= MAX_BLOCKS


def _render_draft(card: ConfirmationCard) -> list[dict]:
    """send and schedule: title, one section per line (the scheduling
    warning), the fields except Subject as one fields section, Subject on
    its own, the body, a divider, the buttons. The whole body and the
    quoted thread when they fit in one message, else the body cut at
    2800."""
    if draft_card_fits(card):
        return _full_draft_blocks(card)
    return _draft_head(card) + [_section(_body_text(card.body))] + _draft_tail(card)


def _render_headline(card: ConfirmationCard, label: str) -> list[dict]:
    """event and event_update: title, the headline field (Subject or Event)
    on its own, the other fields as one fields section, a divider, the
    buttons."""
    head, rest = _split_field(card, label)
    blocks = _title_and_lines(card)
    if head is not None:
        blocks.append(_section(_field_text(head)))
    if rest:
        blocks.append(_fields_section(rest))
    blocks.append(_divider())
    blocks.append(_actions_block(card, [_button(a) for a in card.actions]))
    return blocks + _footer(card)


def _render_event(card: ConfirmationCard) -> list[dict]:
    return _render_headline(card, "Subject")


def _render_event_update(card: ConfirmationCard) -> list[dict]:
    return _render_headline(card, "Event")


def _render_event_delete(card: ConfirmationCard) -> list[dict]:
    """event_delete: title, every field as its own section, a divider,
    the buttons."""
    blocks = _title_and_lines(card)
    blocks += [_section(_field_text(f)) for f in card.fields]
    blocks.append(_divider())
    blocks.append(_actions_block(card, [_button(a) for a in card.actions]))
    return blocks + _footer(card)


def _render_plain(card: ConfirmationCard) -> list[dict]:
    """bulk, close and plugin cards: title, one section per line, then the
    buttons with no divider."""
    blocks = _title_and_lines(card)
    if card.fields:
        blocks.append(_fields_section(card.fields))
    blocks.append(_actions_block(card, [_button(a) for a in card.actions]))
    return blocks + _footer(card)


def _select_element(sel: CardSelect) -> dict:
    """A static_select; its placeholder and options carry no emoji key."""
    options = [{"text": {"type": "plain_text",
                         "text": _ct.one_line(_ct.strip_inert(label), 0)[:LABEL_CHARS]},
                "value": value[:LABEL_CHARS]}
               for label, value in sel.options][:SELECT_OPTIONS_MAX]
    return {"type": "static_select", "action_id": sel.id,
            "placeholder": {"type": "plain_text", "text": sel.placeholder},
            "options": options}


def _render_room_picker(card: ConfirmationCard) -> list[dict]:
    """room picker: the title and lines as one section cut at 2900, the
    buttons (labels cut at 75) with the menu placed before the first
    primary button, and the footer as a context block."""
    elements: list[dict] = []
    select = card.select if card.select and card.select.options else None
    for a in card.actions:
        if select is not None and a.style == "primary":
            elements.append(_select_element(select))
            select = None
        elements.append(_button(a, cut=LABEL_CHARS))
    if select is not None:
        elements.append(_select_element(select))
    blocks = [_section(_card_mrkdwn("\n".join([card.title] + list(card.lines)))[:STATUS_CHARS]),
              _actions_block(card, elements)]
    return blocks + _footer(card)


def _render_browser(card: ConfirmationCard) -> list[dict]:
    # A form approval must show the whole bound payload. The generic
    # status renderer truncates long cards; plain text also prevents a
    # form label or answer from disguising a link or mentioning a user.
    text = "\n".join(_ct.strip_inert(line) for line in [card.title, *card.lines])
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": chunk, "emoji": False}}
              for chunk in _chunks(text, 2800)]
    blocks += [_actions_block(card, [_button(a) for a in card.actions])]
    if len(blocks) > MAX_BLOCKS:
        raise ChatError("form approval is too large to display in full")
    return blocks + _footer(card)


RENDERERS = {
    "send": _render_draft,
    "schedule": _render_draft,
    "event": _render_event,
    "event_update": _render_event_update,
    "event_delete": _render_event_delete,
    "plugin": _render_plain,
    "publish": _render_plain,
    "browser": _render_browser,
    "bulk": _render_plain,
    "close": _render_plain,
    "room_picker": _render_room_picker,
    "room": _render_room_picker,
}


def _fallback(card: ConfirmationCard) -> str:
    if card.fallback_text:
        return _card_mrkdwn(card.fallback_text)
    return "\n".join([_card_mrkdwn(card.title)] + [_card_mrkdwn(ln) for ln in card.lines]
                     + [_field_text(f) for f in card.fields])


def render_card(card: ConfirmationCard) -> tuple[str, list[dict]]:
    """(text, blocks) for a card: the notification fallback text and the
    Block Kit. Raises ChatError for an unknown kind."""
    fn = RENDERERS.get(card.kind)
    if fn is None:
        raise ChatError(f"no Slack renderer for card kind {card.kind!r}")
    return _fallback(card), fn(card)


def status_blocks(text: str) -> tuple[str, list[dict]]:
    """The one context block that replaces a card's buttons, and its text,
    both cut at 2900 characters plus an ellipsis. An inert span in it (a
    plugin's busy line names an item value) is escaped."""
    text = _ct.slack_inert(text)
    safe = text if len(text) <= STATUS_CHARS else text[:STATUS_CHARS] + ELLIPSIS
    return safe, [{"type": "context", "elements": [{"type": "mrkdwn", "text": safe}]}]


# ---------------------------------------------------------------------------
# The frontend
# ---------------------------------------------------------------------------

class SlackFrontend(ChatFrontend):
    """Slack over Socket Mode (bolt), or a bare WebClient for CLI tools."""

    name = "slack"
    capabilities = frozenset({"buttons", "modals", "home_tab", "unfurl", "deep_links",
                              "select", "thread_broadcast", "reactions", "files",
                              "private_rooms"})
    limits = Limits(3000, 2800, 80, 0)

    # Platform wording the LLM prompts carry, verbatim from the bot's
    # prompt sites. The dashes are the prompt's own bytes.
    PROMPT_STRINGS = {
        "platform": "Slack",
        "format_rule_main": (
            "CRITICAL FORMATTING: Your output is posted to Slack. Use Slack mrkdwn:\n"
            "  *bold* (single asterisk) " + EMDASH
            + " NEVER **double asterisks** (renders as literal **)\n"
            "  _italic_ (underscore), :emoji_shortcodes: (not unicode)"),
        "format_rule_reply": (
            "CRITICAL FORMATTING: Your output is posted to Slack. Use Slack mrkdwn:\n"
            "  *bold* (single asterisk) " + EMDASH
            + " NEVER **double asterisks** (renders as literal **)\n"
            "  _italic_ (underscore), :emoji_shortcodes: (not unicode)"),
        "confirm_location": "the Send button is in your DM",
        "confirm_sentence": (
            "Every :lock: block (Send, Create, Schedule) is posted to the user's DM "
            "with the bot, with a one-line pointer in this thread; say \"the Send "
            "button is in your DM\", never \"above\"."),
        "history_note": (
            "You can use Slack MCP read tools to check channel history for context.\n"
            "Do NOT use Slack MCP to send or post messages " + EMDASH
            + " the bot posts your text output automatically."),
        "room_word": "channel",
        "continuation_header": (
            "[Continuing this Slack thread " + EMDASH
            + " you have full memory of the conversation above.]"),
        "output_only": "Output ONLY the response text for Slack. Keep it concise.",
        "output_only_resume": ("Output ONLY the Slack reply text (mrkdwn: "
                               "*single-asterisk bold*, :emoji_shortcodes:)."),
        "format_rule_closing": (
            "Slack formatting (mrkdwn): *bold* (single asterisk), _italic_ (underscore).\n"
            "NEVER use **double asterisks** " + EMDASH
            + " Slack renders them as literal characters.\n"
            "Use Slack emoji shortcodes (:white_check_mark:, :email:) not unicode emoji."),
        "confirm_sentence_wrapped": (
            "buttons themselves are posted to the user's DM with the bot, so say \"the\n"
            "button is in your DM\", never \"above\"."),
    }

    LLM_TOOL_GRANTS = ("mcp__claude_ai_Slack__slack_read_channel,"
                       "mcp__claude_ai_Slack__slack_read_thread,"
                       "mcp__claude_ai_Slack__slack_read_user_profile")

    def __init__(self, bot_token: str, app_token: str = "", *, channel_name: str,
                 channel_id: str, sched_channel: str = "", owner_id: str = "",
                 for_cli: bool = False, client: Any = None) -> None:
        super().__init__()
        self._bot_token = bot_token
        self._app_token = app_token
        self.for_cli = for_cli
        self.main_channel = channel_id
        self.main_channel_name = channel_name
        self.sched_channel = sched_channel
        self.owner_id = owner_id or ""
        self.bot_user_id = ""
        self._started = False
        self._team_cache: dict[str, str] = {}
        if for_cli:
            self.bolt = None
            if client is None:
                import slack_sdk
                client = slack_sdk.WebClient(token=bot_token)
            self.client = client
        else:
            # Attribute lookup at call time, so a test that swaps
            # slack_bolt.App for a stub gets the stub.
            import slack_bolt
            self.bolt = slack_bolt.App(token=bot_token)
            self.client = client if client is not None else self.bolt.client
            self.registry.bind_legacy = self._bind_legacy

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *,
                 for_cli: bool = False, client: Any = None) -> SlackFrontend:
        """Build from the environment with the bot's own reads: the bot
        token, the app token (not for CLI use), the EA channel name and id
        are required and raise KeyError when absent; the scheduling
        channel and the owner id are optional."""
        env = os.environ if env is None else env
        bot_token = env["SLACK_BOT_TOKEN"]
        app_token = "" if for_cli else env["SLACK_APP_TOKEN"]
        channel_name = env["EA_SLACK_CHANNEL_NAME"]
        channel_id = env["EA_SLACK_CHANNEL_ID"]
        sched = env.get("EA_SCHED_CHANNEL_ID", "").strip()
        owner = env.get("EA_SLACK_OWNER_ID") or ""
        return cls(bot_token, app_token, channel_name=channel_name, channel_id=channel_id,
                   sched_channel=sched, owner_id=owner, for_cli=for_cli, client=client)

    # lifecycle

    def _bind_legacy(self, kind: str, key: Key, fn: Handler) -> None:
        """Bind a bolt-style handler straight onto bolt. Bolt runs the first
        listener that matches, so these must be bound before start()
        registers the catch-alls."""
        if self._started:
            raise ChatError(f"legacy {kind} handler for {key!r} bound after start(); "
                            f"the catch-all listener would shadow it")
        if kind == "action":
            self.bolt.action(key)(fn)
        elif kind == "view":
            self.bolt.view(key)(fn)
        else:
            raise ChatError(f"unknown legacy handler kind {kind!r}")

    def start(self) -> None:
        """Identify the bot, then register the bolt listeners that feed the
        registry. The catch-alls go last so every handler bound earlier
        through the legacy bridge still matches first."""
        try:
            resp = self.client.auth_test()
            self.bot_user_id = resp["user_id"]
            log.info(f"Bot user ID: {self.bot_user_id}")
        except Exception as e:
            log.error(f"Failed to resolve bot user ID: {e}")
        if self.owner_id and self.owner_id == self.bot_user_id:
            raise ChatError("EA_SLACK_OWNER_ID is the bot's own user id; set it to the "
                            "owner's id, so the bot's own actions can never confirm a card")
        if self.bolt is None or self._started:
            self._started = True
            return
        bolt = self.bolt
        registry = self.registry

        @bolt.event("message")
        def _slack_message(event):
            registry.dispatch_message(self._inbound(event))

        @bolt.event("app_home_opened")
        def _slack_home_opened(event):
            registry.dispatch_event("home_opened", event)

        @bolt.action(re.compile(".*"))
        def _slack_action(ack, body):
            ack()
            registry.dispatch_action(self._action_event(body))

        @bolt.view(re.compile(".*"))
        def _slack_view(ack, body):
            ack()
            registry.dispatch_view(self._view_event(body))

        @bolt.event(re.compile(".*"))
        def _slack_event(event):
            registry.dispatch_event(event.get("type"), event)

        self._started = True

    def run_forever(self) -> None:
        """Run Socket Mode until the process stops."""
        if self.bolt is None:
            raise ChatError("a CLI SlackFrontend has no event loop")
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        SocketModeHandler(self.bolt, self._app_token).start()

    def ping(self) -> bool:
        try:
            resp = self.client.auth_test()
        except Exception as e:
            log.error(f"auth_test failed: {e}")
            return False
        return bool(_resp_get(resp, "ok"))

    # event translation

    def _message(self, m: dict, channel: str | None = None) -> InboundMessage:
        """One Slack message (event or history row) as an InboundMessage."""
        ts = str(m.get("ts") or "")
        thread_ts = m.get("thread_ts")
        raw_sub = m.get("subtype")
        if raw_sub is None:
            subtype, new_name = "", ""
        elif raw_sub in ("file_share", "thread_broadcast"):
            subtype, new_name = raw_sub, ""
        elif raw_sub == "channel_name":
            subtype, new_name = "channel_rename", m.get("name") or ""
        else:
            subtype, new_name = f"other:{raw_sub}", ""
        files = [FileRef(name=f.get("name", "unnamed") or "",
                         url=f.get("url_private", "") or "",
                         id=f.get("id", "") or "",
                         mimetype=f.get("mimetype", "") or "",
                         title=f.get("title", "") or "")
                 for f in (m.get("files") or []) if isinstance(f, dict)]
        user = m.get("user") or ""
        return InboundMessage(
            channel=str(m.get("channel") or channel or ""),
            msg_id=ts,
            text=m.get("text", "") or "",
            user_id=user,
            posted_at=_ts_float(ts) or 0.0,
            thread_key=thread_ts,
            is_reply=thread_ts is not None and thread_ts != ts,
            is_dm=m.get("channel_type") == "im",
            is_bot=bool(m.get("bot_id")),
            is_self=bool(user) and user == self.bot_user_id,
            subtype=subtype,
            new_name=new_name,
            files=files,
            reply_count=int(m.get("reply_count") or 0),
            raw=m,
        )

    def _inbound(self, event: dict) -> InboundMessage:
        """A bolt message event as an InboundMessage. thread_key is the
        event's thread_ts (None for a top-level message); is_dm follows
        channel_type == "im"."""
        return self._message(event)

    @staticmethod
    def _resolve_card(body: dict) -> MessageRef:
        """The message a click belongs to. A click on a permalink unfurl
        arrives with the containing message (the breadcrumb) as
        body["message"]; the card is the message the unfurl names."""
        channel = str((body.get("channel") or {}).get("id") or "")
        msg = body.get("message") or {}
        for att in msg.get("attachments") or []:
            if att.get("is_msg_unfurl") and att.get("channel_id") and att.get("ts"):
                ts = str(att["ts"])
                return MessageRef(str(att["channel_id"]), ts, ts, _ts_float(ts))
        ts = str(msg.get("ts") or "")
        return MessageRef(channel, ts, str(msg.get("thread_ts") or ts), _ts_float(ts))

    def _action_event(self, body: dict) -> ActionEvent:
        """A block_actions payload as an ActionEvent. The value is the
        parsed JSON when it is a JSON object, else the raw string."""
        action = (body.get("actions") or [{}])[0] or {}
        raw_value = action.get("value")
        value: dict | str = raw_value if isinstance(raw_value, str) else ""
        if isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                value = parsed
        selected = str(((action.get("selected_option") or {}).get("value")) or "")
        return ActionEvent(
            action_id=str(action.get("action_id") or ""),
            user_id=str((body.get("user") or {}).get("id") or ""),
            card=self._resolve_card(body),
            value=value,
            source="select" if action.get("type") == "static_select" else "button",
            selected=selected,
            block_id=str(action.get("block_id") or ""),
            trigger_id=str(body.get("trigger_id") or ""),
            raw=body,
        )

    @staticmethod
    def _view_event(body: dict) -> ViewEvent:
        """A view_submission payload as a ViewEvent."""
        view = body.get("view") or {}
        return ViewEvent(
            callback_id=str(view.get("callback_id") or ""),
            user_id=str((body.get("user") or {}).get("id") or ""),
            private_metadata=view.get("private_metadata") or "",
            values=((view.get("state") or {}).get("values")) or {},
            raw=body,
        )

    # messages

    def _ref(self, channel: str, resp: Any, thread_ts: str | None) -> MessageRef:
        ts = str(_resp_get(resp, "ts") or "")
        return MessageRef(channel, ts, thread_ts or ts, _ts_float(ts))

    def post(self, channel: str, text: str, *, thread_ts: str | None = None,
             unfurl: bool = True) -> MessageRef:
        kwargs: dict = {"channel": channel}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        # A line that names an item value (a plugin's button noun) carries
        # it as an inert span: escaped here, as on a card.
        kwargs["text"] = _ct.slack_spans(text) if _ct.INERT_OPEN in (text or "") else text
        if not unfurl:
            kwargs["unfurl_links"] = False
            kwargs["unfurl_media"] = False
        resp = self.client.chat_postMessage(**kwargs)
        return self._ref(channel, resp, thread_ts)

    def update(self, ref: MessageRef, text: str) -> bool:
        if _ct.INERT_OPEN in (text or ""):
            text = _ct.slack_spans(text)
        self.client.chat_update(channel=ref.channel, ts=ref.id, text=text)
        return True

    @staticmethod
    def _emoji(status: Status | str) -> str:
        return status.value if isinstance(status, Status) else str(status)

    def react(self, channel: str, msg_id: str, status: Status | str) -> bool:
        try:
            self.client.reactions_add(channel=channel, name=self._emoji(status),
                                      timestamp=msg_id)
            return True
        except Exception:
            return False

    def unreact(self, channel: str, msg_id: str, status: Status | str) -> bool:
        try:
            self.client.reactions_remove(channel=channel, name=self._emoji(status),
                                         timestamp=msg_id)
            return True
        except Exception:
            return False

    # cards

    def post_card(self, channel: str, card: ConfirmationCard, *,
                  thread_ts: str | None = None) -> MessageRef:
        text, blocks = render_card(card)
        kwargs: dict = {"channel": channel}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        kwargs["text"] = text
        kwargs["blocks"] = blocks
        resp = self.client.chat_postMessage(**kwargs)
        return self._ref(channel, resp, thread_ts)

    def card_fits(self, card: ConfirmationCard) -> bool:
        """A draft card fits when its whole body and quoted thread fit in
        50 blocks of sections of 2800; another card by the base rule."""
        if card.kind in ("send", "schedule"):
            return draft_card_fits(card)
        return super().card_fits(card)

    def update_card(self, ref: MessageRef, card: ConfirmationCard) -> bool:
        text, blocks = render_card(card)
        self.client.chat_update(channel=ref.channel, ts=ref.id, text=text, blocks=blocks)
        return True

    def replace_card_with_status(self, ref: MessageRef, text: str) -> bool:
        """One context block in place of the card. A fresh block rather
        than the original ones: Slack expands URLs and mentions server
        side, so a long stored body can fail validation on a round trip."""
        safe, blocks = status_blocks(text)
        try:
            self.client.chat_update(channel=ref.channel, ts=ref.id, text=safe, blocks=blocks)
            return True
        except Exception as e:
            log.warning(f"Failed to update confirmation message: {e}")
            return False

    def message_posted_at(self, ref: MessageRef) -> float | None:
        """A Slack ts is the post time in epoch seconds."""
        return _ts_float(ref.id)

    # conversations and rooms

    def dm_channel(self, user_id: str) -> str:
        if not user_id:
            log.warning("Cannot DM confirmation: OWNER_SLACK_USER_ID not set")
            return ""
        try:
            resp = self.client.conversations_open(users=[user_id])
            ch = _resp_get(resp, "channel") or {}
            if isinstance(ch, dict):
                ch = ch.get("id")
            return str(ch or "")
        except Exception as e:
            log.warning(f"Failed to open DM with owner: {e}")
            return ""

    def is_dm(self, channel: str) -> bool:
        return bool(channel) and channel.startswith("D")

    def channel_info(self, channel: str) -> ChannelInfo:
        """is_private is False only for a public channel (is_channel and not
        is_private), the test the privacy check makes."""
        try:
            resp = self.client.conversations_info(channel=channel)
        except Exception as e:
            if "channel_not_found" in _err_text(e):
                return ChannelInfo(channel, "", is_private=False, is_member=False,
                                   exists=False)
            raise ChatError(_err_text(e)) from e
        ch = _resp_get(resp, "channel") or {}
        is_im = bool(ch.get("is_im"))
        return ChannelInfo(
            id=str(ch.get("id") or channel),
            name=str(ch.get("name") or ""),
            is_private=bool(ch.get("is_private")) or not ch.get("is_channel"),
            is_member=bool(ch.get("is_member")) or is_im,
            is_archived=bool(ch.get("is_archived")),
            is_dm=is_im,
        )

    def channel_members(self, channel: str) -> set[str] | None:
        """The members of a conversation (conversations.members, every
        page); None on a failure."""
        out: set[str] = set()
        cursor = ""
        try:
            for _ in range(50):
                kw = {"channel": channel, "limit": 200}
                if cursor:
                    kw["cursor"] = cursor
                resp = self.client.conversations_members(**kw)
                out.update(str(m) for m in (_resp_get(resp, "members") or []))
                cursor = str((_resp_get(resp, "response_metadata") or {}).get("next_cursor") or "")
                if not cursor:
                    return out
        except Exception:
            return None
        return None

    def mention(self, channel: str) -> str:
        return f"<#{channel}>"

    def room_url(self, channel: str) -> str:
        """A deep link the board and App Home carry as the #ea- badge; the
        team id is looked up once."""
        try:
            if "team" not in self._team_cache:
                self._team_cache["team"] = self.client.auth_test().get("team_id", "")
            team = self._team_cache["team"]
        except Exception:
            team = ""
        return (f"https://app.slack.com/client/{team}/{channel}" if team
                else f"https://slack.com/app_redirect?channel={channel}")

    def permalink(self, channel: str, msg_id: str) -> str:
        resp = self.client.chat_getPermalink(channel=channel, message_ts=msg_id)
        return _resp_get(resp, "permalink", "") or ""

    def thread_replies(self, channel: str, thread_ts: str,
                       limit: int = 200) -> list[InboundMessage]:
        resp = self.client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        return [self._message(m, channel) for m in (_resp_get(resp, "messages") or [])]

    def recent_messages(self, channel: str, since_s: float,
                        limit: int = 200, *,
                        since_id: str | None = None) -> list[InboundMessage]:
        """conversations_history from since_s, oldest first. since_id is a
        ts; only messages after it are kept."""
        resp = self.client.conversations_history(channel=channel, oldest=str(since_s),
                                                 limit=limit)
        raw = _resp_get(resp, "messages") or []
        if not raw:
            log.info(f"recent_messages: Slack returned no messages for {channel} since "
                     f"{since_s:.0f} (ok={_resp_get(resp, 'ok')} warning="
                     f"{_resp_get(resp, 'warning')} error={_resp_get(resp, 'error')})")
        msgs = [self._message(m, channel) for m in raw]
        msgs.reverse()  # Slack lists newest first
        after = _ts_float(since_id) if since_id is not None else None
        if after is not None:
            msgs = [m for m in msgs if m.posted_at > after]
        return msgs

    def create_private_room(self, name: str, description: str = "") -> RoomRef:
        try:
            resp = self.client.conversations_create(name=name, is_private=True)
        except Exception as e:
            err = _err_text(e)
            if "missing_scope" in err:
                raise NotPermitted(err) from e
            if "name_taken" in err:
                raise RoomNameTaken(err) from e
            raise ChatError(err) from e
        cid = str((_resp_get(resp, "channel") or {})["id"])
        if description:
            try:
                self.set_description(cid, description)
            except Exception:
                pass
        return RoomRef(cid, name, self.room_url(cid))

    def invite(self, channel: str, user_id: str) -> None:
        self.client.conversations_invite(channel=channel, users=user_id)

    def set_description(self, channel: str, text: str) -> None:
        self.client.conversations_setTopic(channel=channel, topic=text)

    def archive(self, channel: str) -> None:
        try:
            self.client.conversations_archive(channel=channel)
        except Exception as e:
            err = _err_text(e)
            if "already_archived" in err:
                raise AlreadyArchived(err) from e
            raise ChatError(err) from e

    def unarchive(self, channel: str) -> None:
        try:
            self.client.conversations_unarchive(channel=channel)
        except Exception as e:
            err = _err_text(e)
            if "not_archived" in err:
                raise NotArchived(err) from e
            raise ChatError(err) from e

    def archived_map(self) -> dict[str, bool]:
        """Channel id to is_archived for every private channel the bot is
        in; {} when Slack cannot be asked."""
        out: dict[str, bool] = {}
        cursor = None
        try:
            while True:
                resp = self.client.conversations_list(types="private_channel",
                                                      exclude_archived=False,
                                                      limit=500, cursor=cursor)
                for c in _resp_get(resp, "channels") or []:
                    out[c["id"]] = bool(c.get("is_archived"))
                cursor = (_resp_get(resp, "response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        except Exception as e:
            log.warning(f"room hygiene: conversations_list failed: {str(e)[:120]}")
            return {}
        return out

    # files

    def upload(self, channel: str, path: str | Path, *, thread_ts: str | None = None,
               filename: str = "", title: str = "", comment: str = "") -> FileRef:
        kwargs: dict = {"channel": channel}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        kwargs["file"] = str(path)
        if filename:
            kwargs["filename"] = filename
        if title:
            kwargs["title"] = title
        if comment:
            kwargs["initial_comment"] = comment
        resp = self.client.files_upload_v2(**kwargs)
        f = _resp_get(resp, "file") or {}
        if not f:
            files = _resp_get(resp, "files") or []
            f = files[0] if files and isinstance(files[0], dict) else {}
        return FileRef(name=f.get("name") or filename or Path(path).name,
                       url=f.get("permalink") or f.get("url_private") or "",
                       id=f.get("id") or "",
                       mimetype=f.get("mimetype") or "",
                       title=f.get("title") or title)

    def download(self, ref: FileRef, dest_dir: str | Path) -> str | None:
        """Fetch url_private with the bot token into dest_dir as
        <YYYYmmdd-HHMMSS>_<name>, slashes and spaces made underscores.
        dest_dir is the resident's uploads dir, so the directory and the
        file are made with the resident's access (resident_io)."""
        import requests as req

        upload_dir = Path(dest_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = ref.name.replace("/", "_").replace(" ", "_")
        local_path = upload_dir / f"{stamp}_{safe_name}"
        try:
            resp = req.get(ref.url, headers={"Authorization": f"Bearer {self._bot_token}"},
                           timeout=30)
            resp.raise_for_status()
            if not (_files.make_dir(upload_dir, 0o2775)
                    and _files.replace_bytes(local_path, resp.content, 0o664)):
                raise OSError(f"could not save the download to {local_path}")
            log.info(f"Downloaded Slack file to {local_path} ({len(resp.content)} bytes)")
            return str(local_path)
        except Exception as e:
            log.error(f"Failed to download Slack file {ref.url}: {e}")
            return None

    # Slack-only surfaces

    def publish_home(self, user_id: str, view: dict) -> bool:
        self.client.views_publish(user_id=user_id, view=view)
        return True

    def open_modal(self, trigger_id: str, view: dict) -> bool:
        self.client.views_open(trigger_id=trigger_id, view=view)
        return True

    # LLM-facing

    def prompt_strings(self) -> dict[str, str]:
        return dict(self.PROMPT_STRINGS)

    def llm_tool_grants(self) -> str:
        return self.LLM_TOOL_GRANTS
