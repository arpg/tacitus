"""The state of a conversation in the web chat (docs/webchat-state.md).

Every thread holds one state: `needs_you` (the owner's next action is
required), `working` (the bot is on it), `waiting` (on someone else or on
a date), `done` (nothing left) or `none` (no action implied). `by` says
who set it: `rule`, `model` or `owner`; `reason` is one plain sentence
the owner reads.

decide() is the rules, a pure function over a thread's stored messages
(the store's message JSON, oldest first), the channel row, the linked
ledger rows, the notifier key that opened the thread, the relay's
devices and the previous state row. It returns (state, by, reason) when
a rule is decisive and None when the model must judge. The rules run in
the order docs/webchat-state.md gives and the first that fires wins.

judge_prompt() and parse_answer() are the two ends of the model step;
the host supplies the call itself (a callable from the prompt to the
model's text, or None when no model can answer), so this module has no
model client and no EA import. model_verdict() turns a parsed answer
into a state: below MIN_CONFIDENCE, or with an answer that does not
parse, the thread keeps its previous state and the reason says so.
batch_prompt() and parse_batch() are the same step for the hosted
model: one prompt with up to HOSTED_CAP threads, each under its ref,
and one JSON array back, each item checked as parse_answer checks one.

ThreadStates runs both over a WebStore on a daemon thread of the bot.
It reads the store's event log every `poll_s` seconds and runs the rules
on each thread a new message, a card outcome or a reaction changed, in
this process or another (a command-line sender). Every `tick_s` seconds
it runs the rules over every thread with a message in the last
ACTIVE_DAYS days and every thread in Needs you, then the model over the
threads the rules leave open (only threads the owner has written in:
rule 7 settles every other) whose newest message is past the
`judged_seq` of their row (or whose rule no longer holds), newest
first. The host's `route()` says which
model: `local` asks the local model one thread a call, at most
`model_cap` calls a tick; `hosted` makes one call a tick to the hosted
model with up to `hosted_cap` threads (on a worker thread once the
engine runs, so the rules never wait for it); `auto` asks the local
model and hands the threads it could not answer to the hosted call when
it does not answer; `off` asks no model. With the local model, a change
the rules cannot settle brings the next tick forward to `soon_s`
seconds after the last one; the hosted call keeps the tick's pace, so
one call holds the threads a tick gathers. The model's answer, a rule's
answer and the owner's override each write the store's `thread_state`
row, which appends a `thread_state` event when the state, `by` or the
reason changed.

Shadow mode. A card copied as text from Zulip's past (mirrored_card) has
no card JSON, so card_view() reads its text the way Zulip writes it
(zulip_frontend.card_text_state): the legend makes it a live card, a
status line a card acted on, and both decide() and judge_prompt() read
that view. While its stored text can still change (card_pending) the
engine asks the primary for the card as it stands now (`cards`, the
web front end's card_lookup): at most `card_cap` reads a tick, each
answer kept for one tick interval. With no primary to ask (the web chat
alone) the stored text is all there is.

A conversation's cards in Direct messages. A card the owner asked for in
another conversation lives in the DM, with a one-line pointer where it
was asked. The store links the two (its `card_link` table: from the
card's own `ea_channel` and `thread_ts`, and from the pointer's link,
pointer_targets()), and rule 2b (linked_state) reads the linked cards:
one still live is `needs_you`, all settled with no owner message since is
`done`. A change to a card runs the rules on the conversations it links
to; link_cards() links what the store held before the table and runs
the rules on every linked conversation, once at start.

The owner's change to a card. An owner message after every card the
conversation holds or links to, with no answer from Tacitus yet,
outranks a live card in the thread (rule 2) and in the DM (rule 2b):
the owner is changing what the card holds ("make it shorter"), so the
bot is `working`, and past the window the turn is stuck (rule 1a,
reply_over_card). A new card, a revised card or a message from the bot
answers it, and the card rules decide again.

The stale shelf and the nudge. A conversation's age counts from its
newest message, or from the owner's own tap when that is newer
(active_at; the bot's nudge does not count). Each tick sets the stale
mark of every `needs_you` or `waiting` state from that age: stale past
STALE_S, fresh again when a message lands, with one `thread_state`
event each time the mark changes (is_stale). A `needs_you` conversation
whose owner last wrote (or tapped) NUDGE_AFTER_S ago or more, and that
is not stale, gets one line (NUDGE_TEXT) from the bot (`nudge`, the web
front end's post into its own store): once per conversation, never on
an automated post, a card's own conversation or one with a live card, at
most `nudge_cap` a tick, and none while `quiet()` says the owner's quiet
hours hold. The rules and the model never read a nudge (its id is in
the store's `nudges` table), so it changes no state.

Observer. A host may set `observer` on the engine: it hears each owner
tap (`owner_tap`, from the web front end's override route, on a thread of
its own) and each model answer (with `via`, local or hosted), with the
thread as the rules read it, so the host can keep them as labelled
examples and act on a tap (the EA's web_thread_state: closure labels and
the ledger write-back). A failing observer is logged and changes no
state.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .cards import DEFAULT_MAX_AGE_S
from .zulip_frontend import card_text_state

log = logging.getLogger("chat.web.state")

STATES = ("needs_you", "working", "waiting", "done", "none")
# The states the owner sets with the override route.
SETTABLE = ("done", "needs_you", "waiting")
BY = ("rule", "model", "owner")
# The owner's last message counts as unanswered for this long.
WORKING_WINDOW_S = 20 * 60
# A card older than the card registry keeps its rows has buttons that no
# longer act (cards.CardRegistry.sweep).
CARD_LIVE_S = DEFAULT_MAX_AGE_S
ACTIVE_DAYS = 14
TICK_S = 300.0
SOON_S = 60.0
POLL_S = 1.0
MODEL_CAP = 20
# The hosted step: threads one call judges at most, and the bytes its
# prompt may hold (a command-line argument holds 128 KiB at most).
HOSTED_CAP = 20
HOSTED_PROMPT_BYTES = 100_000
ROUTES = ("local", "hosted", "auto", "off")
RULE_CAP = 500
# Reads of a mirrored card on the primary that one tick makes at most.
CARD_CAP = 30
# Threads whose linked ledger rows one read fetches.
BATCH = 50
MIN_CONFIDENCE = 0.6
# Store events that change a thread.
THREAD_EVENTS = frozenset({"message", "update", "reaction", "card", "status", "file"})
# The events that change a card, for the conversations it links to.
CARD_EVENTS = frozenset({"update", "card", "status"})
# The bot's reactions on the owner's message: at work, and a failed turn.
BUSY = frozenset({"eyes", "hourglass_flowing_sand"})
FAILED = frozenset({"x", "warning"})
NOT_ANSWERED = "Tacitus did not answer this. Ask again."
# The owner wrote after a live card and the bot is changing it (rule 1a).
CHANGE_WORKING = "Tacitus is working on your change."
# The relay's notice of a new device (relay_client._tell_owner) and the
# pairing code it names.
DEVICE_NOTICE_RE = re.compile(r"a new device asks to reach tacitus\b.*?pairing code (\d{3}) ?(\d{3})",
                              re.I | re.S)

OWNER_REASONS = {
    "done": "You marked this done.",
    "needs_you": "You marked this as needing you.",
    "waiting": "You marked this as waiting on someone else.",
}

# The first line of an automated post the bot writes when no notifier key
# was recorded for it (history copied from the primary, the bot's own
# notices): briefings, digests, audits and the health checks.
AUTOMATED_TITLE_RE = re.compile(
    r"^(?:(?:morning|midday|afternoon|evening) (?:briefing|update)\b"
    r"|weekly audit\b|a new device asks to reach\b|reconcile:"
    r"|ea (?:boundary|persistence|health)\b|release tests?\b|remote llm shadow run\b"
    r"|voice measurement\b|invite eval\b)"
    r"|\bdigest\b", re.I)
# Rule 7's reason names the post in plain words: the first line's known
# kind, else a notice (a notifier key was recorded) or a post.
_POST_NAMES = (
    (re.compile(r"^(morning|midday|afternoon|evening) (briefing|update)\b", re.I), ""),
    (re.compile(r"^weekly audit\b", re.I), "The weekly audit"),
    (re.compile(r"^a new device asks to reach\b", re.I), "A new-device notice"),
    (re.compile(r"^reconcile:", re.I), "The nightly ledger reconcile"),
    (re.compile(r"^(?:ea (?:boundary|persistence|health)|release tests?|remote llm shadow run"
                r"|voice measurement|invite eval)\b", re.I), "A health check from Tacitus"),
    (re.compile(r"\bdigest\b", re.I), "A digest"),
)
QUIET_TAIL = "it needs nothing from you until you write in it."
_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")
_MARKUP_RE = re.compile(r"[*_`~#>]+")
# Ledger ids as the bot writes them (`response-AbC12_x=`, `task-...`): a
# word, a dash, then id characters. The host's lookup keeps only the ones
# that are rows.
_LEDGER_ID_RE = re.compile(r"(?<![\w./+=-])([a-z][a-z0-9_]*-[A-Za-z0-9_+=-]{2,80})(?![\w+=/-])")
_CODE_SPAN_RE = re.compile(r"`([^`\n]{3,90})`")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\]\([^)]*\)")
_FENCE_RE = re.compile(r"```.*?```", re.S)
_QUESTION_RE = re.compile(r"\?(?:[\s*_)\"'\]]|$)")
LINK_CAP = 24

# Open rows of these types need the owner; an open row of any other type
# (task, deadline, waiting, reimbursement, dispute) is the model's to read.
LEDGER_NEEDS = frozenset({"response_needed", "scheduling", "draft_pending"})
LEDGER_REASONS = {
    "response_needed": "The linked ledger item still needs your reply.",
    "scheduling": "The linked ledger item is a meeting still to arrange.",
    "draft_pending": "The linked ledger item is a draft still waiting for you to send it.",
}


# ── reading a thread ──────────────────────────────────────────────────

def plain(text: str | None, cap: int = 120) -> str:
    """The first line of `text` without emoji shortcodes or Markdown marks."""
    line = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    line = _MARKUP_RE.sub("", _SHORTCODE_RE.sub("", line))
    line = re.sub(r"^[^\w(\[\"']+", "", line)
    return " ".join(line.split())[:cap]


def asks(text: str | None) -> bool:
    """True when a bot message asks the owner something: a question mark
    at the end of a sentence, outside links and code."""
    t = _FENCE_RE.sub(" ", str(text or ""))
    t = _URL_RE.sub(" ", t)
    t = re.sub(r"`[^`]*`", " ", t)
    return bool(_QUESTION_RE.search(t))


def _reactions(m: dict) -> set:
    r = m.get("reactions")
    return set(r) if isinstance(r, list) else set()


def _card(m: dict) -> dict:
    c = m.get("card")
    return c if isinstance(c, dict) else {}


def _live(m: dict, now: float) -> bool:
    """A card whose buttons still act: no outcome recorded (still a `card`
    row), at least one action or a select, and younger than the card
    registry keeps it."""
    if m.get("kind") != "card":
        return False
    c = _card(m)
    if not (c.get("actions") or c.get("select")):
        return False
    return now - float(m.get("posted_at") or now) < CARD_LIVE_S


def outcome(text: str | None) -> str:
    """What a card's status line says: `busy` (the bot is acting on the
    press), `failed` (it could not) or `acted`. The emoji names are the
    core's, or Zulip's for a status line copied from Zulip."""
    t = str(text or "").lstrip()
    if t.startswith((":hourglass", ":time_ticking:")):
        return "busy"
    if t.startswith((":x:", ":cross_mark:", ":warning:", ":no_entry:")):
        return "failed"
    return "acted"


def _card_title(m: dict) -> str:
    return plain(_card(m).get("title") or m.get("text"), 80)


# A card copied from Zulip's past opens with its lock title, after the
# owner's mention when the card carried one.
_MIRRORED_CARD_RE = re.compile(r"^\s*(?:@\*\*[^*\n]+\*\*\s*)?:lock(?:ed)?:")
_MENTION_RE = re.compile(r"^\s*@\*\*[^*\n]+\*\*\s*")
# The status of a card filed in Zulip's done stream with no status line
# that says it acted.
SETTLED_OUTSIDE = "Settled outside chat."


def mirrored_card(m: dict) -> bool:
    """True for a card copied as text from Zulip's past into a mirrored
    stream channel (`stream:<n>`): a history row with no card JSON whose
    text opens with the lock title and holds the card's rule. The one-line
    pointer to a card ("The Send button ... is in your DM") has no rule
    and is no card."""
    text = str(m.get("text") or "")
    return (bool(m.get("history")) and m.get("kind") == "message" and not m.get("card")
            and str(m.get("channel") or "").startswith("stream:")
            and bool(_MIRRORED_CARD_RE.match(text)) and card_text_state(text)[0] != "")


def card_pending(text: str | None) -> bool:
    """True while a mirrored card's text can still change on Zulip: its
    legend shows or a press is under way. A status line that acted or
    failed is final."""
    state, status = card_text_state(str(text or ""))
    return state == "live" or (state == "status" and outcome(status) == "busy")


def card_view(m: dict) -> dict:
    """A mirrored card as the card rules (2, 3c, 4) and the prompt read a
    card; any other message as it is. The text read is Zulip's answer (the
    `primary` the engine attaches: {"text", "filed", "edited_at"}), else
    the stored copy. While the legend shows, it is a `card` row with a live
    action (rule 2 still weighs its age). Once a status line holds the
    legend's place, it is a `status` row with that line. A card filed in
    the done stream with no status that acted was settled outside chat.

    The status counts from the card's own time, so every message after
    the card is later for rule 4, whether or not Zulip answered this
    time; a press under way counts from Zulip's last edit when known, for
    rule 3c's window."""
    if not mirrored_card(m):
        return m
    got = m.get("primary") if isinstance(m.get("primary"), dict) else {}
    text = str(got.get("text") or m.get("text") or "")
    state, status = card_text_state(text)
    if got.get("filed") and not (state == "status" and outcome(status) == "acted"):
        state, status = "status", SETTLED_OUTSIDE
    title = next((ln for ln in _MENTION_RE.sub("", text, count=1).splitlines() if ln.strip()),
                 "")
    if state == "live":
        return {**m, "kind": "card", "card": {"title": title, "actions": [{"id": "primary"}]}}
    if state == "status":
        at = m.get("posted_at")
        if outcome(status) == "busy":
            at = got.get("edited_at") or m.get("updated_at") or at
        return {**m, "kind": "status", "text": status, "card": {"title": title},
                "updated_at": float(at or 0)}
    return m


def _walk(obj: Any, keys: dict[str, list], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("conversation_id", "ledger_id") and isinstance(v, str) and v.strip():
                keys[k].append(v.strip())
            elif k == "ledger_ids" and isinstance(v, list):
                keys["ledger_id"] += [str(x).strip() for x in v if isinstance(x, str) and x.strip()]
            else:
                _walk(v, keys, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, keys, depth + 1)


def link_keys(messages: list[dict]) -> tuple[list[str], list[str]]:
    """(ledger ids, conversation ids) the thread names: the `ledger_id`,
    `ledger_ids` and `conversation_id` its cards carry, and the id-shaped
    words of its text (code spans first). Candidates: the host's lookup
    keeps the ones that are rows."""
    keys: dict[str, list] = {"conversation_id": [], "ledger_id": []}
    for m in messages:
        _walk(_card(m), keys)
    texts = [str(m.get("text") or "") for m in messages]
    words = [w for t in texts for span in _CODE_SPAN_RE.findall(t)
             for w in _LEDGER_ID_RE.findall(span)]
    words += [w for t in texts for w in _LEDGER_ID_RE.findall(t)]

    def uniq(seq: list[str]) -> list[str]:
        out: list[str] = []
        for s in seq:
            if s not in out:
                out.append(s)
            if len(out) >= LINK_CAP:
                break
        return out
    return uniq(keys["ledger_id"] + words), uniq(keys["conversation_id"])


# ── a conversation's cards in Direct messages, stale, the nudge ───────

# A needs_you or waiting conversation quiet this long sits on the stale
# shelf ("Older" in Needs you).
STALE_S = 7 * 86400
# A needs_you conversation whose owner last wrote this long ago gets the
# nudge, once.
NUDGE_AFTER_S = 24 * 3600
NUDGE_CAP = 10
NUDGE_TEXT = "Still need this? Reply here, or tap Mark done."
CARD_WAITS = "Its card in Direct messages waits for you."
CARD_BUSY = "Tacitus is acting on the button you pressed."
# The pointer the bot leaves where a card was asked for
# (ea-chat-bot._post_dm_breadcrumb: ":lock: <What> is in your DM: <link|open
# it>"), as the web chat stores it: a Markdown link after the words, the
# lock under its core name or Zulip's.
POINTER_RE = re.compile(r":lock(?:ed)?:[^\n]{0,160}? (?:is|are) in your DM:? *"
                        r"\[[^\]\n]{1,40}\]\((\S+?)\)")
# The card a pointer's link names: the web chat's own hash route
# (#/<channel>/<thread>/<message>), Zulip's narrow (.../near/<id>; an id
# is unique on the server) or Slack's permalink (/archives/<C>/p<ts>).
_WEB_LINK_RE = re.compile(r"#/([^/\s?#]+)/([^/\s?#]+)/([^/\s?#]+)$")
_ZULIP_LINK_RE = re.compile(r"/near/(\d+)$")
_SLACK_LINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?|$)")


def pointer_targets(text: str | None) -> list[tuple[str, str]]:
    """(card channel, card message id) for each pointer to a card in a bot
    message; the channel is "" when the link names only the id (Zulip)."""
    out: list[tuple[str, str]] = []
    for url in POINTER_RE.findall(str(text or "")):
        m = _WEB_LINK_RE.search(url)
        if m:
            got = (urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(3)))
        elif _ZULIP_LINK_RE.search(url):
            got = ("", _ZULIP_LINK_RE.search(url).group(1))
        elif _SLACK_LINK_RE.search(url):
            s = _SLACK_LINK_RE.search(url)
            got = (s.group(1), f"{s.group(2)}.{s.group(3)}")
        else:
            continue
        if got[1] and got not in out:
            out.append(got)
    return out


def card_origin(card: Any) -> tuple[str, str] | None:
    """(channel, thread key) of the conversation a card reports back to: its
    `ea_channel` and `thread_ts` (the card registry's link), or None."""
    if not isinstance(card, dict):
        return None
    ch, key = str(card.get("ea_channel") or ""), str(card.get("thread_ts") or "")
    return (ch, key) if ch and key else None


def linked_state(linked: list[dict], messages: list[dict],
                 now: float) -> tuple[str, str] | None:
    """Rule 2b over the cards the conversation links to in another thread
    (`linked`, store rows; a card copied from Zulip's past reads as
    card_view gives it): one live is needs_you; a press under way within
    WORKING_WINDOW_S is working; all acted on, with no owner message in
    the conversation after the last of them, is done. A failed, expired or
    busy-too-long card, a link to a card the store does not hold (kind
    `missing`), or no card at all, decides nothing."""
    linked = linked or []
    cards = [c for c in (card_view(x) for x in linked)
             if c.get("kind") in ("card", "status") and isinstance(c.get("card"), dict)]
    if not cards:
        return None
    if any(_live(c, now) for c in cards):
        return "needs_you", CARD_WAITS
    done = [c for c in cards if c.get("kind") == "status"]
    if any(outcome(c.get("text")) == "busy"
           and now - float(c.get("updated_at") or 0) < WORKING_WINDOW_S for c in done):
        return "working", CARD_BUSY
    if len(done) != len(cards) or any(outcome(c.get("text")) != "acted" for c in done) \
            or any(x.get("kind") == "missing" for x in linked):
        return None
    last = max(done, key=lambda c: float(c.get("updated_at") or c.get("posted_at") or 0))
    at = float(last.get("updated_at") or last.get("posted_at") or 0)
    if any(m.get("author") == "owner" and float(m.get("posted_at") or 0) > at for m in messages):
        return None
    what = plain(last.get("text"), 80)
    word = "card was" if len(cards) == 1 else "cards were"
    return "done", (f"Its {word} handled: {what}" if what else f"Its {word} handled.")


def _tap_at(prev: dict | None) -> float:
    """When the owner set the state: its `at` for an owner row, else 0."""
    prev = prev or {}
    return float(prev.get("at") or 0) if prev.get("by") == "owner" else 0.0


def is_stale(state: str | None, by: str | None, at: Any, last_at: Any, before: float) -> bool:
    """The stale shelf's test for one state row: a needs_you or waiting
    state whose conversation has been quiet since `before` or earlier.
    Quiet counts from `last_at` (the conversation's newest message; the
    state's own `at` when it has none) or from the owner's tap (`at` when
    `by` is owner), whichever is newer."""
    if state not in ("needs_you", "waiting"):
        return False
    active = float(last_at) if last_at is not None else float(at or 0)
    if by == "owner":
        active = max(active, float(at or 0))
    return active <= float(before)


def active_at(messages: list[dict], prev: dict | None) -> float:
    """When the conversation was last active: its newest message, or the
    owner's tap when that is newer (the stale shelf's clock)."""
    newest = max((float(m.get("posted_at") or 0) for m in messages or []), default=0.0)
    return max(newest, _tap_at(prev))


def nudge_since(messages: list[dict], prev: dict | None) -> float | None:
    """When the nudge's 24 hours start: the owner's last message in the
    conversation, or the owner's tap when that is newer. None when the
    owner never wrote in it nor tapped its state: no nudge then."""
    owner = [float(m.get("posted_at") or 0) for m in messages or [] if m.get("author") == "owner"]
    start = max([*owner, _tap_at(prev)], default=0.0)
    return start or None


def nudge_due(ctx: dict, now: float) -> bool:
    """True when the conversation gets the "Still need this?" line now: its
    state is needs_you, the owner last wrote in it (or tapped its state)
    NUDGE_AFTER_S ago or more (never: no nudge), it is not on the stale shelf (active within
    STALE_S: an older one shelves instead), it opens neither with an
    automated post nor with a card, and no card in it or linked to it is
    live (its button is the nudge). Once per conversation is the store's
    to keep."""
    prev = ctx.get("prev") or {}
    raw = ctx.get("messages") or []
    msgs = [card_view(m) for m in raw]
    if prev.get("state") != "needs_you" or not msgs:
        return False
    since = nudge_since(raw, prev)
    if since is None or now - since < NUDGE_AFTER_S or now - active_at(raw, prev) >= STALE_S:
        return False
    first = msgs[0]
    if first.get("kind") in ("card", "status") or (
            first.get("author") != "owner"
            and (ctx.get("origin") or AUTOMATED_TITLE_RE.search(plain(first.get("text"), 200)))):
        return False
    return not (any(_live(m, now) for m in msgs) or any(
        _live(card_view(c), now) for c in ctx.get("linked") or []))


# ── the rules ─────────────────────────────────────────────────────────

def _ledger_state(rows: list[dict]) -> tuple[str, str] | None:
    """Rule 6 over the linked rows: an open or follow_up row of a type in
    LEDGER_NEEDS is needs_you; else a waiting or snoozed status is
    waiting; else done when every row is done. An open row of another
    type leaves the thread to the model, with the row."""
    needs, waiting, done = [], [], []
    for r in rows or []:
        typ = str(r.get("type") or "")
        status = str(r.get("status") or "")
        if status in ("completed", "closed", "done"):
            done.append(r)
        elif status in ("waiting", "snoozed"):
            waiting.append(r)
        elif status in ("open", "follow_up") and typ in LEDGER_NEEDS:
            needs.append(r)
    if needs:
        return "needs_you", LEDGER_REASONS[str(needs[0].get("type"))]
    if waiting:
        snoozed = all(str(r.get("status")) == "snoozed" for r in waiting)
        return "waiting", ("The linked ledger item is snoozed until a later date." if snoozed
                           else "The linked ledger item waits on someone else.")
    if done and len(done) == len(rows):
        return "done", "The linked ledger item is closed."
    return None


def device_code(text: str | None) -> str:
    """The pairing code a new-device notice names ("123456"), or ""."""
    m = DEVICE_NOTICE_RE.search(plain_text(text))
    return m.group(1) + m.group(2) if m else ""


def post_name(first: dict, origin: str = "") -> str:
    """What a thread Tacitus started is, in plain words, for rule 7's
    reason: a card, a briefing, a digest, a health check, a notice (a
    notifier key was recorded), else a post."""
    if first.get("kind") in ("card", "status"):
        return "A card from Tacitus"
    line = plain(first.get("text"), 200)
    for rx, name in _POST_NAMES:
        m = rx.search(line)
        if m:
            return name or f"The {m.group(1).lower()} {m.group(2).lower()}"
    return "A notice from Tacitus" if origin else "A post from Tacitus"


def plain_text(text: str | None) -> str:
    return _MARKUP_RE.sub("", _SHORTCODE_RE.sub("", str(text or "")))


def _notice(m: dict) -> bool:
    """A bot message that is no answer: a progress line (still working) or
    a failed-turn notice."""
    return (m.get("author") != "owner" and m.get("kind") == "message"
            and outcome(m.get("text")) in ("busy", "failed"))


def unanswered(messages: list[dict], now: float) -> tuple[str, str, float] | None:
    """Rule 3's test: the owner wrote last and Tacitus has posted nothing
    since but progress lines or a failed-turn notice. (state, reason, when
    the owner wrote): needs_you at once on a failed turn, working for
    WORKING_WINDOW_S from the owner's message or the bot's last progress
    line, needs_you (a stuck turn) after. None when Tacitus answered, when
    the owner never wrote, and when the evidence is not decisive: a
    finished turn (the check) with no words, or an owner message copied
    from the primary's past (its reactions are not copied)."""
    owner_at = max((i for i, m in enumerate(messages) if m.get("author") == "owner"), default=None)
    if owner_at is None:
        return None
    mine, after = messages[owner_at], messages[owner_at + 1:]
    if mine.get("history") or not all(_notice(m) for m in after):
        return None
    r = _reactions(mine)
    wrote = float(mine.get("posted_at") or 0)
    if r & FAILED or any(outcome(m.get("text")) == "failed" for m in after):
        return "needs_you", NOT_ANSWERED, wrote
    if "white_check_mark" in r:
        return None
    at = max(float(m.get("posted_at") or 0) for m in [mine, *after])
    if now - at >= WORKING_WINDOW_S:
        return "needs_you", NOT_ANSWERED, wrote
    return "working", ("Tacitus is working on your last message." if r & BUSY or after
                       else "You wrote last and Tacitus has not answered yet."), wrote


def _changed_at(m: dict) -> float:
    """When a card last changed: its posting, a revision in place or its
    outcome."""
    return max(float(m.get("posted_at") or 0), float(m.get("updated_at") or 0))


def reply_over_card(messages: list[dict], linked: list[dict] | None,
                    now: float) -> tuple[str, str] | None:
    """Rule 1a, run where rule 2 or rule 2b finds a live card: the owner
    wrote after every card the conversation holds or links to (its
    posting, a revision in place, its outcome) and Tacitus has not
    answered (rule 3's test). The owner is changing what the card holds,
    so the bot is working on it: working with CHANGE_WORKING within
    WORKING_WINDOW_S, needs_you with NOT_ANSWERED after it or on a failed
    turn. None once Tacitus answers (a new card, a revised card or a
    message): the card rules decide. `messages` are read as card_view
    gives them."""
    got = unanswered(messages, now)
    if got is None:
        return None
    state, reason, wrote = got
    cards = [*messages, *(card_view(x) for x in linked or [])]
    last = max((_changed_at(c) for c in cards if c.get("kind") in ("card", "status")),
               default=0.0)
    if wrote <= last:
        return None
    return state, (CHANGE_WORKING if state == "working" else reason)


def decide(messages: list[dict], *, channel: dict | None = None,
           ledger: list[dict] | None = None, prev: dict | None = None, origin: str = "",
           devices: list[dict] | None = None, now: float | None = None,
           linked: list[dict] | None = None) -> tuple[str, str, str] | None:
    """(state, by, reason) when a rule is decisive, else None (the model
    judges; only ever for a thread the owner has written in, since rule 7
    settles every other). `messages` is the thread oldest first (a message may carry
    `history`: copied from the primary's past, without its reactions);
    `prev` the stored row ({"state", "by", "reason", "judged_seq"}) or
    None; `origin` the notifier key that opened the thread, "" when none
    was recorded; `devices` the relay's device rows, None when no relay
    runs; `linked` the cards in other threads the conversation links to
    (rule 2b). A card copied as text from Zulip's past is read as
    card_view gives it."""
    if not messages:
        return None
    now = time.time() if now is None else float(now)
    messages = [card_view(m) for m in messages]
    newest = max(int(m.get("seq") or 0) for m in messages)
    first = messages[0]

    # 1. The owner's override holds until a new message lands after it.
    if prev and prev.get("by") == "owner" and prev.get("state") in STATES \
            and newest <= int(prev.get("judged_seq") or 0):
        return prev["state"], "owner", str(prev.get("reason") or OWNER_REASONS.get(prev["state"], ""))

    # 2. A card whose buttons still act, unless the owner wrote after it
    # and Tacitus has not answered yet (1a).
    live = [m for m in messages if _live(m, now)]
    if live:
        got = reply_over_card(messages, linked, now)
        if got:
            return got[0], "rule", got[1]
        title = _card_title(live[-1])
        return ("needs_you", "rule",
                f'The card "{title}" still has its buttons.' if title
                else "A card here still has its buttons.")

    # 2a. The relay's notice of a new device: it needs the owner while the
    # device waits for approval, and is done once it is approved or removed.
    code = device_code(first.get("text")) if first.get("author") != "owner" else ""
    if code and devices is not None:
        if any(d.get("pending") and str(d.get("code") or "") == code for d in devices):
            return "needs_you", "rule", "A phone waits for your approval."
        return "done", "rule", "The phone was approved or removed."

    # 2b. The conversation's cards in Direct messages; a live one yields to
    # the owner's message after it as rule 2 does (1a).
    got = linked_state(linked or [], messages, now)
    if got and got[1] == CARD_WAITS:
        got = reply_over_card(messages, linked, now) or got
    if got:
        return got[0], "rule", got[1]

    # 3. The owner wrote last, and Tacitus has given no answer since: only
    # progress lines or a failed-turn notice. Working for WORKING_WINDOW_S,
    # then (or at once on a failed turn) it needs the owner. A finished
    # turn (the check) with no words, and a message copied from the
    # primary's past (its reactions are not copied), are the model's.
    got = unanswered(messages, now)
    if got:
        return got[0], "rule", got[1]

    # 3c. Tacitus is acting on a button the owner pressed.
    settled = [m for m in messages if m.get("kind") == "status"]
    latest = max(settled, key=lambda m: float(m.get("updated_at") or m.get("posted_at") or 0),
                 default=None)
    if latest is not None and outcome(latest.get("text")) == "busy" \
            and now - float(latest.get("updated_at") or 0) < WORKING_WINDOW_S:
        return "working", "rule", "Tacitus is acting on the button you pressed."

    # 4. A card acted on (its status line as the bot records it), and
    # nothing after it asks anything.
    if latest is not None and outcome(latest.get("text")) == "acted":
        at = float(latest.get("updated_at") or latest.get("posted_at") or 0)
        later = [m for m in messages if m is not latest and float(m.get("posted_at") or 0) > at]
        if not any(m.get("author") == "owner" or asks(m.get("text")) for m in later):
            what = plain(latest.get("text"), 80)
            return "done", "rule", (f"The card was answered: {what}" if what
                                    else "The card was answered.")

    # 5. (The bot's check on the first message is no rule: it marks a
    # finished turn, not a finished conversation.)

    # 6. A linked ledger row.
    got = _ledger_state(ledger or [])
    if got:
        return got[0], "rule", got[1]

    # 7. A thread Tacitus started and the owner never wrote in, whatever it
    # says: no state until the owner engages. The origin and the title name
    # the post in the reason only. So the model only reads threads the
    # owner has written in.
    if first.get("author") != "owner" and not any(m.get("author") == "owner" for m in messages):
        return "none", "rule", f"{post_name(first, origin)}: {QUIET_TAIL}"

    # 8. The model.
    return None


# ── the model step ────────────────────────────────────────────────────

# What each state means, the same words for the local and the hosted step.
STATES_TEXT = """The states:
- needs_you: the owner must do the next thing (answer a question from the bot, pick an option, decide, approve).
- working: the bot is busy with the owner's request and has not answered yet.
- waiting: the conversation waits on someone else (a reply from another person) or on a date.
- done: nothing is left to do in this conversation.
- none: no action is implied (a briefing, a digest, a notice, small talk)."""

PROMPT = """You read one conversation from a chat between a person (the owner) and their assistant bot. Say what state the conversation is in now.

""" + STATES_TEXT + """

Today is {today}. The channel is "{name}" (kind: {kind}).{ledger}

The messages, oldest first. Each line gives the author, the time and the text. A card shows its title and what happened to it.

{messages}

Answer with one JSON object and nothing else:
{{"state": "needs_you" | "working" | "waiting" | "done" | "none", "reason": "<one short sentence in plain words, for the owner>", "confidence": <a number from 0 to 1>}}"""

# The hosted step's prompt: the same words, for several conversations at
# once, each under its ref.
BATCH_PROMPT = """You read {count} conversations from a chat between a person (the owner) and their assistant bot. For each one, say what state it is in now. Judge each conversation by its own messages alone.

""" + STATES_TEXT + """

Today is {today}. Each conversation below opens with a line that gives its ref, then its channel and the items on the owner's task list it is linked to, if any. Its messages follow, oldest first. Each line gives the author, the time and the text. A card shows its title and what happened to it.

{conversations}

Answer with one JSON array and nothing else, with one object for each conversation:
[{{"ref": "<the conversation's ref>", "state": "needs_you" | "working" | "waiting" | "done" | "none", "reason": "<one short sentence in plain words, for the owner>", "confidence": <a number from 0 to 1>}}]"""
BATCH_HEAD = "=== conversation {ref} ==="

PROMPT_MESSAGES = 30
PROMPT_TEXT_CHARS = 500
PROMPT_CHARS = 9000
REASON_CHARS = 200
# The JSON schema of the answer, for a model server that constrains output.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"state": {"type": "string", "enum": list(STATES)},
                   "reason": {"type": "string"},
                   "confidence": {"type": "number"}},
    "required": ["state", "reason", "confidence"],
    "additionalProperties": False,
}


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _line(m: dict, now: float) -> str:
    who = "owner" if m.get("author") == "owner" else "bot"
    head = f"[{who}, {_when(m.get('posted_at'))}]"
    kind = m.get("kind")
    if kind == "card":
        state = "waiting for a button" if _live(m, now) else "no live buttons"
        return f'{head} card "{_card_title(m)}": {state}'
    if kind == "status":
        title = plain(_card(m).get("title"), 80)
        name = f'card "{title}"' if title else "card"
        return f"{head} {name}: {plain(m.get('text'), 160)}"
    text = " ".join(str(m.get("text") or "").split())
    if len(text) > PROMPT_TEXT_CHARS:
        text = text[:PROMPT_TEXT_CHARS] + "..."
    if kind == "file":
        name = (m.get("file") or {}).get("name") if isinstance(m.get("file"), dict) else ""
        text = f"(a file: {name}) {text}".strip()
    return f"{head} {text}"


def _message_lines(messages: list[dict], now: float) -> str:
    """The thread's lines for a prompt: the first message stays; past the
    caps the oldest of the rest go, and a line says how many."""
    lines = [_line(card_view(m), now) for m in messages]
    head, rest = lines[:1], lines[1:][-(PROMPT_MESSAGES - 1):]
    while rest and sum(len(x) + 1 for x in head + rest) > PROMPT_CHARS:
        rest = rest[1:]
    skipped = len(lines) - 1 - len(rest)
    return "\n".join(head + ([f"({skipped} earlier messages left out)"] if skipped > 0 else [])
                     + rest)


def _ledger_text(ledger: list[dict] | None) -> str:
    """The linked ledger rows for a prompt ("" with none): type, status,
    deadline and last action."""
    if not ledger:
        return ""
    items = []
    for r in ledger[:5]:
        item = f"type {r.get('type') or '?'}, status {r.get('status') or '?'}"
        if r.get("deadline"):
            item += f", deadline {str(r['deadline'])[:40]}"
        if r.get("last_action"):
            item += f", last action: {' '.join(str(r['last_action']).split())[:200]}"
        items.append(item)
    return "\nThe conversation is linked to items on the owner's task list:\n" + \
        "\n".join(f"- {i}" for i in items)


def _channel_words(channel: dict | None) -> tuple[str, str]:
    ch = channel or {}
    return str(ch.get("name") or ch.get("id") or "?"), str(ch.get("kind") or "?")


def _today(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%A %Y-%m-%d")


def judge_prompt(messages: list[dict], *, channel: dict | None = None,
                 ledger: list[dict] | None = None, now: float | None = None) -> str:
    """The model's prompt: the thread's messages (author, time, text, and a
    card's title and outcome, never its body or fields), the channel's kind
    and name, the linked ledger rows' type, status, deadline and last
    action, and today's date. The first message stays; past the caps the
    oldest of the rest go. A card copied as text from Zulip's past shows
    as card_view reads it."""
    now = time.time() if now is None else float(now)
    name, kind = _channel_words(channel)
    return PROMPT.format(today=_today(now), name=name, kind=kind, ledger=_ledger_text(ledger),
                         messages=_message_lines(messages, now))


def batch_part(ref: str, messages: list[dict], *, channel: dict | None = None,
               ledger: list[dict] | None = None, now: float | None = None) -> str:
    """One conversation's part of the hosted prompt: its ref line, its
    channel and linked rows, and its messages, as judge_prompt gives them.
    A message line always opens with its author, so no message can start
    a ref line of its own."""
    now = time.time() if now is None else float(now)
    name, kind = _channel_words(channel)
    return (f"{BATCH_HEAD.format(ref=ref)}\nThe channel is \"{name}\" (kind: {kind})."
            f"{_ledger_text(ledger)}\n\n{_message_lines(messages, now)}")


def batch_prompt(parts: list[str], *, now: float | None = None) -> str:
    """The hosted step's prompt over batch_part()'s parts."""
    now = time.time() if now is None else float(now)
    return BATCH_PROMPT.format(count=len(parts), today=_today(now),
                               conversations="\n\n".join(parts))


def _unfence(text: str) -> str:
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    return m.group(1) if m else t


def _checked(obj: Any) -> dict | None:
    """An answer object with exactly the keys state, reason and confidence,
    checked: a state of the five, a reason that is a non-empty string and a
    confidence that is a number from 0 to 1. The reason kept is its first
    sentence in plain words. None when it fails."""
    if not isinstance(obj, dict) or set(obj) != {"state", "reason", "confidence"}:
        return None
    state, reason, conf = obj["state"], obj["reason"], obj["confidence"]
    if state not in STATES or not isinstance(reason, str) or not reason.strip():
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) \
            or not 0 <= conf <= 1:
        return None
    # One plain sentence: the first, without emoji codes or Markdown marks.
    reason = " ".join(plain_text(reason).split())
    reason = re.split(r"(?<=[.!?])\s+", reason, maxsplit=1)[0]
    if not reason:
        return None
    if len(reason) > REASON_CHARS:
        reason = reason[:REASON_CHARS - 3].rstrip() + "..."
    return {"state": state, "reason": reason, "confidence": float(conf)}


def parse_answer(text: Any) -> dict | None:
    """{"state", "reason", "confidence"} from the model's text, or None.
    Strict: one JSON object (a code fence around it is allowed, nothing
    else), a state of the five, a reason that is a non-empty string and a
    confidence that is a number from 0 to 1. The reason kept is its first
    sentence in plain words."""
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(_unfence(text))
    except ValueError:
        return None
    return _checked(obj)


def parse_batch(text: Any, refs: list[str]) -> dict[str, dict | None]:
    """The hosted answer for each ref asked: {ref: answer, or None for an
    answer it could not use}. Strict: one JSON array (a code fence around
    it is allowed, nothing else) of objects, each with exactly the keys
    ref, state, reason and confidence, checked as parse_answer checks one.
    A ref with one such object gets its answer; a ref whose object fails
    the checks, or that the array answers more than once, gets None. A ref
    the array leaves out is not in the result: it was not answered, and
    the next tick asks again. An item for a ref not asked is dropped. A
    text that is not such an array gives None for every ref."""
    asked = [str(r) for r in refs]
    if not isinstance(text, str):
        return dict.fromkeys(asked)
    try:
        arr = json.loads(_unfence(text))
    except ValueError:
        return dict.fromkeys(asked)
    if not isinstance(arr, list):
        return dict.fromkeys(asked)
    out: dict[str, dict | None] = {}
    seen: set = set()
    for item in arr:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str) or ref not in asked:
            continue
        if ref in seen:
            out[ref] = None
            continue
        seen.add(ref)
        out[ref] = _checked({k: v for k, v in item.items() if k != "ref"})
    return out


def model_verdict(answer: dict | None, prev: dict | None) -> tuple[str, str, str]:
    """(state, "model", reason) for a parsed answer. Below MIN_CONFIDENCE,
    or with no answer that parses, the previous state stays and the
    reason says the model was unsure."""
    before = (prev or {}).get("state") if (prev or {}).get("state") in STATES else "none"
    if answer is None:
        return before, "model", "The model gave no answer it could use, so the state stays as it was."
    if answer["confidence"] < MIN_CONFIDENCE:
        return before, "model", "The model was unsure, so the state stays as it was."
    return answer["state"], "model", answer["reason"]


# ── the engine ────────────────────────────────────────────────────────

Ledger = Callable[[list, list], list]
Model = Callable[[str], "str | None"]
# hosted(prompt): the hosted model's text for a batch_prompt(), or None
# when it gave none (a failure: the states stay as they were).
Hosted = Callable[[str], "str | None"]
# route(): one of ROUTES, read at each tick.
Route = Callable[[], str]
CardRead = Callable[[str, str, str], "dict | None"]
# observer(event, ctx, *, now, store, **info): event "owner" (info: row,
# the state row the tap wrote, and before, the row it replaced) or "model"
# (info: answer, the parsed answer or None, row, the row written, and via,
# "local" or "hosted").
Observer = Callable[..., None]
# Seconds between two log lines about the same failing hosted call.
HOSTED_LOG_S = 3600.0


class ThreadStates:
    """The rules at once on every change, and the tick, over one store.

    `store` is a callable that returns the WebStore (the front end's
    property). `ledger(ledger_ids, conversation_ids)` returns the rows
    named by id or by conversation id ({"id", "type", "status",
    "conversation_id"}), for up to BATCH threads at once. `model(prompt)`
    returns the model's text, or None when no model can answer now (the
    tick then stops asking). `devices()` returns the relay's device rows
    (None with no relay), read only for a new-device notice.
    `hosted(prompt)` returns the hosted model's text for a batch of
    threads (batch_prompt), or None when it gave none; `route()` says each
    tick which model judges (ROUTES: local, hosted, auto or off). With no
    route, `model` judges when set, else `hosted`.
    `cards(channel, thread_key, msg_id)` returns a mirrored card as the
    primary holds it now ({"text", "filed", "edited_at"}, or None), and
    may write its current text into the store; card_cap reads a tick at
    most, each answer kept for tick_s. Each may be None: the rules alone
    set the state, and a mirrored card is read from its stored text.
    `nudge(channel, thread_key, text)` posts the bot's "Still need this?"
    line into the store alone and records it (a falsy answer: not posted);
    `quiet()` is True while the owner's quiet hours hold. With no nudge
    nothing is posted."""

    def __init__(self, store: Callable[[], Any], *, ledger: Ledger | None = None,
                 model: Model | None = None, devices: Callable[[], Any] | None = None,
                 cards: CardRead | None = None, hosted: Hosted | None = None,
                 route: Route | None = None, hosted_cap: int = HOSTED_CAP,
                 tick_s: float = TICK_S, soon_s: float = SOON_S,
                 poll_s: float = POLL_S, model_cap: int = MODEL_CAP, rule_cap: int = RULE_CAP,
                 card_cap: int = CARD_CAP, clock: Callable[[], float] = time.time,
                 nudge: Callable[[str, str, str], Any] | None = None,
                 quiet: Callable[[], bool] | None = None, nudge_cap: int = NUDGE_CAP) -> None:
        self._store = store
        self.ledger = ledger
        self.model = model
        self.hosted = hosted
        self.route = route
        self.hosted_cap = int(hosted_cap)
        # The route the last tick read (None before the first).
        self._route_now: str | None = None
        # The hosted call under way on its worker thread, once the engine runs.
        self._hosted_worker: threading.Thread | None = None
        self._hosted_logged: dict[str, float] = {}
        self.devices = devices
        self.cards = cards
        self.tick_s = float(tick_s)
        self.soon_s = float(soon_s)
        self.poll_s = float(poll_s)
        self.model_cap = int(model_cap)
        self.rule_cap = int(rule_cap)
        self.card_cap = int(card_cap)
        self.clock = clock
        self._seen: int | None = None
        self._due = False
        self._last_tick: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned: set = set()
        # (channel, message id) -> (when read, the primary's answer or None).
        self._card_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
        # Reads of the primary since the tick began.
        self._card_reads = 0
        self.nudge = nudge
        self.quiet = quiet
        self.nudge_cap = int(nudge_cap)
        # The host's observer of owner taps and model answers (owner_tap).
        self.observer: Observer | None = None

    # running

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._seen = self._store().max_event_seq()
        self._thread = threading.Thread(target=self._run, name="webchat-state", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    def _run(self) -> None:
        try:
            self.link_cards()
        except Exception:
            log.exception("web chat: the cards could not be linked to their conversations")
        while not self._stop.wait(self.poll_s):
            try:
                self.changed()
            except Exception as e:
                log.warning("web chat: the thread-state rules failed: %s", e)
            mono = time.monotonic()
            wait = self.soon_s if self._due else self.tick_s
            if self._last_tick is None or mono - self._last_tick >= wait:
                self._last_tick = mono
                self._due = False
                try:
                    self.tick()
                except Exception:
                    log.exception("web chat: the thread-state tick failed")

    def _warn(self, key: str, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def _hourly(self, key: str, msg: str, *args: Any) -> None:
        """Log `msg` at most once in HOSTED_LOG_S for `key`."""
        now = time.monotonic()
        if now - self._hosted_logged.get(key, -HOSTED_LOG_S - 1) >= HOSTED_LOG_S:
            self._hosted_logged[key] = now
            log.warning(msg, *args)

    def _route(self) -> str:
        """Which model judges now (ROUTES): the host's route(), else local
        with a model, hosted with only a hosted call, off with neither. A
        route that fails or names no route reads off."""
        if self.route is None:
            return "local" if self.model is not None else \
                "hosted" if self.hosted is not None else "off"
        try:
            got = str(self.route())
        except Exception as e:
            self._hourly("route", "web chat: the thread-state route could not be read, so no "
                                  "model judges this tick: %s", e)
            return "off"
        return got if got in ROUTES else "off"

    def _soon(self) -> bool:
        """True when a change the rules leave open brings the tick forward:
        with the local model only, since the hosted call keeps the tick's
        pace."""
        if self._route_now is None:
            self._route_now = self._route()
        return self._route_now in ("local", "auto") and self.model is not None

    # one thread, or a batch

    def _read_ledger(self, ids: list[str], convs: list[str]) -> list[dict]:
        if self.ledger is None or not (ids or convs):
            return []
        try:
            return [r for r in self.ledger(ids, convs) or [] if isinstance(r, dict)]
        except Exception as e:
            self._warn("ledger", "web chat: the ledger could not be read for thread states: %s", e)
            return []

    def _read_card(self, m: dict) -> dict | None:
        """The primary's answer for one mirrored card, kept for tick_s
        seconds. Past card_cap reads in this tick it waits for the next
        tick, and its stored text stands. None with no answer."""
        key = (str(m.get("channel") or ""), str(m.get("id") or ""))
        now = self.clock()
        hit = self._card_cache.get(key)
        if hit is not None and now - hit[0] < self.tick_s:
            return hit[1]
        read = self.cards
        if read is None or self._card_reads >= self.card_cap:
            return None
        self._card_reads += 1
        try:
            got = read(key[0], str(m.get("thread_key") or ""), key[1])
        except Exception as e:
            self._warn("cards", "web chat: a card could not be read from the primary for "
                                "thread states: %s", e)
            got = None
        got = got if isinstance(got, dict) else None
        self._card_cache[key] = (now, got)
        return got

    def _with_cards(self, msgs: list[dict]) -> list[dict]:
        """The thread with the primary's answer (`primary`) on each mirrored
        card whose stored text can still change; as it is with no primary
        to ask."""
        if self.cards is None:
            return msgs
        out = []
        for m in msgs:
            if mirrored_card(m) and card_pending(m.get("text")):
                got = self._read_card(m)
                if got is not None:
                    m = {**m, "primary": got}
            out.append(m)
        return out

    def contexts(self, keys: list[tuple[str, str]]) -> list[dict]:
        """What the rules read for each thread (its messages, channel row,
        linked ledger rows, previous row and origin), with one ledger read
        for the whole batch. A thread with no messages is left out."""
        store = self._store()
        chans: dict[str, Any] = {}
        out: list[dict] = []
        for channel, key in keys:
            msgs = self._with_cards(self._messages(store, channel, key))
            if not msgs:
                continue
            if channel not in chans:
                chans[channel] = store.channel(channel)
            ids, convs = link_keys(msgs) if self.ledger is not None else ([], [])
            out.append({"channel": channel, "key": key, "messages": msgs,
                        "channel_row": chans[channel], "ids": ids, "convs": convs,
                        "ledger": [], "prev": store.thread_state(channel, key),
                        "origin": store.thread_origin(channel, key),
                        "linked": self._with_cards(store.linked_cards(channel, key)),
                        "newest": max(int(m["seq"]) for m in msgs)})
        all_ids = sorted({i for c in out for i in c["ids"]})
        all_convs = sorted({v for c in out for v in c["convs"]})
        rows = self._read_ledger(all_ids, all_convs)
        devices = None
        if self.devices is not None and any(
                device_code(c["messages"][0].get("text")) for c in out):
            try:
                devices = self.devices()
            except Exception as e:
                self._warn("devices", "web chat: the relay's devices could not be read for "
                                      "thread states: %s", e)
        for c in out:
            ids, convs = set(c["ids"]), set(c["convs"])
            c["ledger"] = [r for r in rows if r.get("id") in ids
                           or (r.get("conversation_id") and r.get("conversation_id") in convs)]
            c["devices"] = devices
        return out

    def _apply(self, ctx: dict) -> str:
        """The rules on one context: "rule" when one was decisive (the row
        is written), "open" when the model must judge."""
        got = decide(ctx["messages"], channel=ctx["channel_row"], ledger=ctx["ledger"],
                     prev=ctx["prev"], origin=ctx["origin"], devices=ctx.get("devices"),
                     now=self.clock(), linked=ctx.get("linked"))
        if got is None:
            return "open"
        self._store().set_thread_state(ctx["channel"], ctx["key"], *got, judged_seq=ctx["newest"])
        return "rule"

    def rules(self, channel: str, key: str) -> tuple[str, dict | None]:
        """Run the rules on one thread: ("rule", ctx) when one was decisive
        and the row is written, ("open", ctx) when the model must judge,
        ("gone", None) for a thread with no messages."""
        got = self.contexts([(channel, key)])
        if not got:
            return "gone", None
        return self._apply(got[0]), got[0]

    def judge(self, ctx: dict) -> str:
        """The model on one thread the rules left open: "model" when it
        answered (the row is written), "unavailable" when no model could."""
        if self.model is None:
            return "unavailable"
        prompt = judge_prompt(ctx["messages"], channel=ctx["channel_row"], ledger=ctx["ledger"],
                              now=self.clock())
        try:
            text = self.model(prompt)
        except Exception as e:
            log.warning("web chat: the thread-state model failed: %s", e)
            text = None
        if text is None:
            return "unavailable"
        answer = parse_answer(text)
        if answer is None:
            log.info("web chat: the thread-state model's answer did not parse for %s/%s",
                     ctx["channel"], ctx["key"])
        self._write_answer(ctx, answer, "local")
        return "model"

    def _write_answer(self, ctx: dict, answer: dict | None, via: str) -> dict:
        """Write one model answer (a parsed answer, or None for one that did
        not parse) as the thread's state, and tell the observer."""
        state, by, reason = model_verdict(answer, ctx["prev"])
        row = self._store().set_thread_state(ctx["channel"], ctx["key"], state, by, reason,
                                             judged_seq=ctx["newest"])
        self._observe("model", ctx, answer=answer, row=row, via=via)
        return row

    def judge_hosted(self, keys: list[tuple[str, str]]) -> dict:
        """The hosted model on the threads the rules leave open (`keys`,
        newest first): one call over up to hosted_cap of them, as many as
        fit HOSTED_PROMPT_BYTES, each under its ref. Once the engine runs,
        the call goes on a worker thread and the tick does not wait for it;
        while one is under way no other starts. Returns {"calls" (0 or 1),
        "asked", and, when the tick waited for the call, "judged" and
        "unanswered"}."""
        out = {"calls": 0, "asked": 0}
        if self.hosted is None or not keys or self._stop.is_set():
            return out
        worker = self._hosted_worker
        if worker is not None and worker.is_alive():
            return out
        # Changes that landed since the tick read the threads go first.
        self.changed()
        batch: list[dict] = []
        for i in range(0, len(keys), BATCH):
            for ctx in self.contexts(keys[i:i + BATCH]):
                if self._apply(ctx) == "open":
                    batch.append(ctx)
            if len(batch) >= self.hosted_cap:
                break
        now = self.clock()
        items: list[tuple[str, dict]] = []
        parts: list[str] = []
        size = len(batch_prompt([], now=now).encode())
        for ctx in batch[:self.hosted_cap]:
            ref = f"c{len(items) + 1}"
            part = batch_part(ref, ctx["messages"], channel=ctx["channel_row"],
                              ledger=ctx["ledger"], now=now)
            need = len(part.encode()) + 2
            if items and size + need > HOSTED_PROMPT_BYTES:
                break
            items.append((ref, ctx))
            parts.append(part)
            size += need
        if not items:
            return out
        prompt = batch_prompt(parts, now=now)
        out.update(calls=1, asked=len(items))
        if self._thread is None:
            out.update(self._hosted_call(prompt, items))
            return out
        self._hosted_worker = threading.Thread(target=self._hosted_call, args=(prompt, items),
                                               name="webchat-state-hosted", daemon=True)
        self._hosted_worker.start()
        return out

    def _hosted_call(self, prompt: str, items: list[tuple[str, dict]]) -> dict:
        """Ask the hosted model and write its answers. No answer at all
        writes nothing (each thread keeps its state; the next tick asks
        again) and is logged once an hour. A thread the answer leaves out
        is asked again too; one whose item does not parse keeps its state
        with the model's "no answer it could use"."""
        got = {"judged": 0, "unanswered": len(items)}
        try:
            text = self.hosted(prompt) if self.hosted is not None else None
        except Exception as e:
            self._hourly("hosted", "web chat: the hosted model did not judge the thread states; "
                                   "they stay as they were: %s", e)
            text = None
        if text is None or self._stop.is_set():
            return got
        answers = parse_batch(text, [ref for ref, _ctx in items])
        for ref, ctx in items:
            if ref not in answers:
                continue
            try:
                self._write_answer(ctx, answers[ref], "hosted")
            except Exception as e:
                log.warning("web chat: a hosted thread state was not written: %s", e)
                continue
            got["judged"] += 1
            got["unanswered"] -= 1
        if got["unanswered"]:
            log.info("web chat: the hosted model left %d of %d threads unanswered; the next "
                     "tick asks again", got["unanswered"], len(items))
        return got

    # the two passes

    def changed(self) -> int:
        """Run the rules on every thread an event changed since the last
        call; how many threads. A thread the rules leave open brings the
        next tick forward."""
        store = self._store()
        if self._seen is None:
            self._seen = store.max_event_seq()
            return 0
        keys: list[tuple[str, str]] = []
        refs: list[tuple[str, str]] = []
        while True:
            batch = store.events_since(self._seen, 500)
            for seq, typ, payload in batch:
                self._seen = seq
                if typ not in THREAD_EVENTS:
                    continue
                try:
                    d = json.loads(payload)
                except ValueError:
                    continue
                k = (d.get("channel"), d.get("thread_key"))
                if isinstance(k[0], str) and isinstance(k[1], str) and k not in keys:
                    keys.append(k)
                # A card that settled, or whose copied text changed, moves the
                # conversations it links to.
                if typ in CARD_EVENTS and isinstance(k[0], str) and d.get("id"):
                    refs.append((k[0], str(d["id"])))
            if len(batch) < 500:
                break
        keys += [k for k in self._origins(store, refs) if k not in keys]
        for i in range(0, len(keys), BATCH):
            for ctx in self.contexts(keys[i:i + BATCH]):
                if self._apply(ctx) == "open" and self._soon():
                    self._due = True
        return len(keys)

    def tick(self) -> dict:
        """The rules over every thread active in the last ACTIVE_DAYS days
        (newest first, at most rule_cap) and every thread in Needs you,
        whatever its age (a state the rules would now settle, such as a
        model's state on a thread the owner never wrote in, moves without a
        new message), then the model over the ones they leave open (the
        owner has written in each) whose newest message is past their row's
        judged_seq, or whose row a rule set and no rule holds now, newest
        first. The route
        picks the model: local asks one thread a call, at most model_cap
        calls and none after a call no model could answer; hosted makes one
        call over up to hosted_cap threads (judge_hosted); auto asks the
        local model and, when it does not answer, hands the threads left to
        the hosted call. The primary's card reads start again at none, and
        an answer older than tick_s is dropped."""
        store = self._store()
        now = self.clock()
        route = self._route_now = self._route()
        self._card_reads = 0
        self._card_cache = {k: v for k, v in self._card_cache.items() if now - v[0] < self.tick_s}
        since = now - ACTIVE_DAYS * 86400
        rows = store.active_threads(since, self.rule_cap)
        keys = [(r["channel"], r["thread_key"]) for r in rows]
        seen = set(keys)
        for r in store.needs(self.rule_cap):
            k = (r["channel"], r["thread_key"])
            if k not in seen and len(keys) < self.rule_cap:
                seen.add(k)
                keys.append(k)
        open_: list[tuple[str, str]] = []
        decided = 0
        for i in range(0, len(keys), BATCH):
            for ctx in self.contexts(keys[i:i + BATCH]):
                if self._apply(ctx) == "rule":
                    decided += 1
                    continue
                prev = ctx["prev"] or {}
                if not prev or ctx["newest"] > int(prev.get("judged_seq") or 0) \
                        or prev.get("by") == "rule":
                    open_.append((ctx["channel"], ctx["key"]))
        calls = 0
        stopped = False
        # The threads the hosted call takes: every open one on the hosted
        # route (or auto with no local model to ask), and on auto the ones
        # left when the local model does not answer.
        left = open_ if route == "hosted" or (route == "auto" and self.model is None) else []
        if route in ("local", "auto") and self.model is not None:
            for i, (channel, key) in enumerate(open_):
                if calls >= self.model_cap or self._stop.is_set():
                    break
                # Changes that land while the model reads go first.
                self.changed()
                verdict, fresh = self.rules(channel, key)
                if verdict != "open":
                    continue
                calls += 1
                if self.judge(fresh) == "unavailable":
                    stopped = True
                    left = open_[i:] if route == "auto" else []
                    break
        if stopped and not left:
            log.info("web chat: no model answered for thread states; the rules alone set them "
                     "until the next tick")
        hosted = self.judge_hosted(left) if left else {"calls": 0, "asked": 0}
        return {"threads": len(keys), "rules": decided, "open": len(open_), "model_calls": calls,
                "stopped": stopped, "route": route, "hosted_calls": hosted["calls"],
                "hosted_threads": hosted["asked"], "card_reads": self._card_reads,
                "stale": self.shelve(now), "nudges": self.nudges(now)}

    # a conversation's cards in Direct messages, the stale shelf, the nudge

    @staticmethod
    def _messages(store: Any, channel: str, key: str) -> list[dict]:
        """The thread as the rules and the model read it: without the bot's
        nudges, which change no state."""
        msgs = store.thread(channel, key, history=True)
        nudged = store.nudge_ids(channel, key) if msgs else set()
        return [m for m in msgs if m.get("id") not in nudged] if nudged else msgs

    @staticmethod
    def _origins(store: Any, refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """The conversations linked to the changed messages (a card that
        settled, a card's text that changed), so their rules run too."""
        ids = [r for r in refs if r[1]]
        return store.card_origins(ids) if ids else []

    def link_cards(self) -> int:
        """Link the cards the store held before its card_link table to their
        conversations (once per store: card_link_backfill), then run the
        rules on every conversation that links to a card, at most rule_cap,
        newest first, whatever its age. How many ran."""
        store = self._store()
        store.backfill_card_links()
        keys = store.linked_threads(self.rule_cap)
        for i in range(0, len(keys), BATCH):
            for ctx in self.contexts(keys[i:i + BATCH]):
                self._apply(ctx)
        return len(keys)

    def shelve(self, now: float | None = None) -> int:
        """Set the stale mark of each state from its conversation's age
        (is_stale: a needs_you or waiting conversation quiet past STALE_S),
        one thread_state event for each mark that changes; how many
        changed."""
        now = self.clock() if now is None else float(now)
        return self._store().mark_stale(now - STALE_S)

    def _quiet_now(self) -> bool:
        if self.quiet is None:
            return False
        try:
            return bool(self.quiet())
        except Exception as e:
            self._warn("quiet", "web chat: quiet hours could not be read, so no nudge goes out: %s", e)
            return True

    def nudges(self, now: float | None = None) -> int:
        """Post the "Still need this?" line in each conversation nudge_due()
        picks, the longest waiting first, at most nudge_cap; none in quiet
        hours or with no nudge to post through. How many went out."""
        if self.nudge is None or self.nudge_cap <= 0:
            return 0
        now = self.clock() if now is None else float(now)
        if self._quiet_now():
            return 0
        store = self._store()
        # needs_you conversations whose owner last wrote (or tapped) at least
        # NUDGE_AFTER_S ago and that were active within STALE_S.
        keys = store.nudge_candidates(now - NUDGE_AFTER_S, now - STALE_S, RULE_CAP)
        posted = 0
        for channel, key in keys:
            if posted >= self.nudge_cap or self._stop.is_set():
                break
            msgs = self._messages(store, channel, key)
            ctx = {"messages": msgs, "prev": store.thread_state(channel, key),
                   "origin": store.thread_origin(channel, key),
                   "linked": store.linked_cards(channel, key)}
            if not nudge_due(ctx, now):
                continue
            try:
                ok = self.nudge(channel, key, NUDGE_TEXT)
            except Exception as e:
                self._warn("nudge", "web chat: a nudge could not be posted: %s", e)
                continue
            posted += 1 if ok else 0
        return posted

    # the observer: each owner tap and each model answer

    def owner_tap(self, channel: str, key: str, row: dict,
                  before: dict | None) -> threading.Thread | None:
        """Hand the owner's override to the observer: `row` is the state row
        the tap wrote, `before` the row it replaced. The thread is read as
        the rules read it, on a thread of its own, so the route answers at
        once. Returns that thread (None with no observer)."""
        if self.observer is None:
            return None

        def run() -> None:
            try:
                got = self.contexts([(channel, key)])
            except Exception as e:
                log.warning("web chat: the thread for an owner tap could not be read: %s", e)
                return
            if got:
                self._observe("owner", got[0], row=dict(row), before=dict(before or {}))
        t = threading.Thread(target=run, name="webchat-state-owner", daemon=True)
        t.start()
        return t

    def _observe(self, event: str, ctx: dict, **info: Any) -> None:
        obs = self.observer
        if obs is None:
            return
        try:
            obs(event, ctx, now=self.clock(), store=self._store(), **info)
        except Exception as e:
            self._warn(f"observer-{event}", "web chat: the thread-state observer failed on %s: "
                                            "%s", event, e)
