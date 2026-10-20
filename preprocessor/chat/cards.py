"""Confirmation cards: the neutral card model, the card registry and the
resolvers that turn an owner's reaction or typed code into an action.

A ConfirmationCard is what the core asks the owner to approve (send a
draft, create an event, pick a room). Each frontend renders it its own
way: Slack as Block Kit buttons, Zulip as text with a one-time code.
Whatever the owner does (click, react, type the code) reaches the core
as one ActionEvent.

The CardRegistry records every live card: where it was posted, when,
its code, and each action's value, word and style. The reaction and
code resolvers read only the registry row, so a card can be confirmed
after a restart. The registry file lives in the private directory
(EA_PRIVATE_DIR), out of reach of the LLM's Unix user.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .types import CODE_ALPHABET, ActionEvent, MessageRef


@dataclass
class CardAction:
    """One choice on a card. `word` is the verb the owner types with the
    code on a frontend without buttons ("send", "cancel", "send all")."""
    id: str
    label: str
    value: dict | str
    style: str = ""
    word: str = ""


@dataclass
class CardSelect:
    """A drop-down on a card; options are (label, value) pairs."""
    id: str
    placeholder: str
    options: list[tuple[str, str]]


@dataclass
class CardField:
    """A labelled line on a card, such as To or Subject."""
    label: str
    value: str


@dataclass
class ConfirmationCard:
    """A card the owner must act on. `kind` names the card type (send,
    schedule, event, event_update, event_delete, plugin, publish, bulk, room).

    `content_fingerprint` is, on a send or schedule card, the fingerprint of
    the draft content the card shows (draft_fingerprint.py). No frontend
    renders it; the bot keeps it in its owner-private registry and a click
    sends only a draft that still has it.

    `quoted` is, on a send or schedule card, the quoted thread of the draft
    (everything from the quote boundary on). A frontend shows it after the
    body in a block the owner can expand (a Zulip spoiler; Slack sections)."""
    kind: str
    title: str
    fields: list[CardField] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    body: str = ""
    quoted: str = ""
    actions: list[CardAction] = field(default_factory=list)
    select: CardSelect | None = None
    block_id: str = ""
    footer: str = ""
    fallback_text: str = ""
    thread_ts: str = ""
    ea_channel: str = ""
    content_fingerprint: str = ""
    # No default action: a thumbs up, a check or a bare code selects only an
    # action styled primary, never the first one. Set on a card that
    # withheld its approval, so a thumbs up meant as Send does nothing
    # instead of picking whatever action now comes first (Edit).
    no_default: bool = False


# Reactions the owner can put on a card, by the role of the action they pick.
REACTION_ROLES = {
    "primary": ("+1", "thumbs_up", "thumbsup", "check", "white_check_mark"),
    "cancel": ("-1", "thumbs_down", "thumbsdown", "cross_mark", "x"),
}

CODE_RE = re.compile(
    r"^\s*(?:(?P<word>[a-z][a-z ]{0,24}?)\s+)?(?P<code>[A-HJ-NP-Z2-9]{4,6})\s*[.!]?\s*$",
    re.IGNORECASE,
)

DEFAULT_MAX_AGE_S = 7 * 86400


def _norm_word(word: str) -> str:
    return " ".join(word.lower().split())


def _pick_primary(items: list[tuple[str, str, str]], default: bool = True) -> str | None:
    """The id of the first primary-styled item, else (with `default`) the
    first item."""
    for aid, style, _word in items:
        if style == "primary":
            return aid
    return items[0][0] if items and default else None


# Words that name the action a cross reaction should pick. The style is
# not enough: a delete card puts danger on Delete, its primary action.
CANCEL_WORDS = ("cancel", "keep", "skip", "dismiss", "one by one")


def _pick_cancel(items: list[tuple[str, str, str]]) -> str | None:
    """The id of the item whose word is a cancel word, else the first
    danger-styled item that is not the primary, else None."""
    for aid, _style, word in items:
        if _norm_word(word) in CANCEL_WORDS:
            return aid
    primary = _pick_primary(items)
    for aid, style, _word in items:
        if style == "danger" and aid != primary:
            return aid
    return None


def _card_items(card: ConfirmationCard) -> list[tuple[str, str, str]]:
    return [(a.id, a.style, a.word) for a in card.actions]


def _row_items(row: dict) -> list[tuple[str, str, str]]:
    return [(aid, a.get("style", ""), a.get("word", ""))
            for aid, a in (row.get("actions") or {}).items()]


def primary_action(card: ConfirmationCard) -> CardAction | None:
    """The action a bare code or a check reaction selects."""
    aid = _pick_primary(_card_items(card), not card.no_default)
    return next((a for a in card.actions if a.id == aid), None)


def cancel_action(card: ConfirmationCard) -> CardAction | None:
    """The action a cross reaction selects, or None when the card has none."""
    aid = _pick_cancel(_card_items(card))
    return next((a for a in card.actions if a.id == aid), None)


# The emoji at the front of an action's label: ":pencil2: Edit".
_LABEL_EMOJI_RE = re.compile(r"^\s*:([a-z0-9_+-]+):")


def action_reactions(card: ConfirmationCard) -> list[tuple[str, str]]:
    """[(emoji shortcode, action id)], one reaction per action, in action
    order, for a platform where reactions stand in for buttons: thumbs up
    for the primary action, thumbs down for the cancel action, and for any
    other the emoji its label opens with. An action with no emoji gets no
    reaction (its word and the code still select it); a duplicate emoji is
    skipped."""
    prim, canc = primary_action(card), cancel_action(card)
    out: list[tuple[str, str]] = []
    used: set[str] = set()
    for a in card.actions:
        if prim is not None and a.id == prim.id:
            name = "+1"
        elif canc is not None and a.id == canc.id:
            name = "-1"
        else:
            m = _LABEL_EMOJI_RE.match(a.label or "")
            name = m.group(1) if m else ""
        if name and name not in used:
            used.add(name)
            out.append((name, a.id))
    return out


def parse_code(text: str) -> tuple[str | None, str] | None:
    """(word or None, upper-case code) when text is a code reply, else None."""
    m = CODE_RE.match(text or "")
    if not m:
        return None
    word = m.group("word")
    return (_norm_word(word) if word else None, m.group("code").upper())


def _ref_key(channel: str, msg_id: str) -> str:
    return f"{channel}|{msg_id}"


class CardRegistry:
    """Live and retired cards in one JSON file, written atomically at 0600.

    Rows: {kind, channel, msg_id, card_thread, posted_at, code, actions:
    {id: {value, word, style, label}}, thread_ts, ea_channel, status,
    created}; status is "live" or "retired"; created is epoch seconds.
    card_thread is the thread the card itself was posted in (its own Zulip
    topic, or the DM); a typed code counts only there. thread_ts is the
    conversation the card reports back to.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.",
                                   suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._rows, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, ref: MessageRef, card: ConfirmationCard, *, code: str | None,
            reactions: dict | None = None, chips: list | None = None) -> dict:
        """Record a posted card and return its row. `reactions` maps each
        reaction name that selects an action to its id; `chips` names the
        reactions the frontend added to the card itself."""
        row = {
            "kind": card.kind,
            "channel": ref.channel,
            "msg_id": ref.id,
            "card_thread": ref.thread_key or "",
            "posted_at": ref.posted_at,
            "code": code.upper() if code else None,
            "actions": {a.id: {"value": a.value, "word": a.word,
                               "style": a.style, "label": a.label}
                        for a in card.actions},
            "thread_ts": card.thread_ts,
            "ea_channel": card.ea_channel,
            "status": "live",
            "created": time.time(),
        }
        if reactions:
            row["reactions"] = dict(reactions)
        if chips:
            row["chips"] = list(chips)
        if card.no_default:
            row["no_default"] = True
        with self._lock:
            self._rows[_ref_key(ref.channel, ref.id)] = row
            self._save()
        return dict(row)

    def by_ref(self, channel: str, msg_id: str) -> dict | None:
        """The row for a posted card, live or retired."""
        with self._lock:
            row = self._rows.get(_ref_key(channel, msg_id))
            return dict(row) if row else None

    def by_code(self, code: str, *, thread_key: str | None) -> dict | None:
        """The live row carrying this code, matched case-insensitively, of
        a card posted in thread `thread_key`. A code typed in another
        thread, or for a row that does not record its thread, finds
        nothing: a code is good only where its card is."""
        want = (code or "").upper()
        if not want or not thread_key:
            return None
        with self._lock:
            for row in self._rows.values():
                if (row.get("status") == "live" and (row.get("code") or "") == want
                        and row.get("card_thread") and row["card_thread"] == thread_key):
                    return dict(row)
        return None

    def posted_at(self, ref: MessageRef) -> float | None:
        """Epoch seconds the card was posted, None when unrecorded."""
        row = self.by_ref(ref.channel, ref.id)
        val = row.get("posted_at") if row else None
        return float(val) if isinstance(val, (int, float)) else None

    def retire(self, ref: MessageRef) -> bool:
        """Mark a card retired, so its code and reactions stop resolving."""
        with self._lock:
            row = self._rows.get(_ref_key(ref.channel, ref.id))
            if not row:
                return False
            row["status"] = "retired"
            self._save()
        return True

    def move_thread(self, old: str, new: str, channel: str = "") -> int:
        """Point every row, and every action value, that names thread `old`
        at `new` (and its ea_channel at `channel` when the thread changed
        channel); the number of rows changed."""
        def follow(d: dict) -> bool:
            if d.get("thread_ts") != old:
                return False
            d["thread_ts"] = new
            if channel and d.get("ea_channel"):
                d["ea_channel"] = channel
            return True

        changed = 0
        with self._lock:
            for row in self._rows.values():
                hit = follow(row)
                if row.get("card_thread") == old:
                    row["card_thread"] = new
                    hit = True
                for act in (row.get("actions") or {}).values():
                    val = act.get("value")
                    if isinstance(val, dict):
                        hit = follow(val) or hit
                changed += hit
            if changed:
                self._save()
        return changed

    def new_code(self) -> str:
        """A fresh 4-character code, unique among live rows."""
        with self._lock:
            live = {r.get("code") for r in self._rows.values() if r.get("status") == "live"}
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
            if code not in live:
                return code

    def sweep(self, max_age_s: float = DEFAULT_MAX_AGE_S) -> int:
        """Drop rows created more than max_age_s ago; the number dropped."""
        cutoff = time.time() - max_age_s
        with self._lock:
            old = [k for k, r in self._rows.items()
                   if float(r.get("created") or 0) < cutoff]
            for k in old:
                del self._rows[k]
            if old:
                self._save()
        return len(old)


def _event_for(row: dict, action_id: str | None, user_id: str,
               source: str) -> ActionEvent | None:
    if not action_id:
        return None
    action = (row.get("actions") or {}).get(action_id) or {}
    card = MessageRef(row["channel"], row["msg_id"],
                      row.get("thread_ts") or row["msg_id"], row.get("posted_at"))
    return ActionEvent(action_id=action_id, user_id=user_id, card=card,
                       value=action.get("value", ""), source=source)


def resolve_reaction(row: dict | None, emoji_name: str, user_id: str,
                     owner_id: str, *, self_id: str = "") -> ActionEvent | None:
    """The action an owner's reaction on a live card selects, else None.

    A card posted with reaction chips keeps them in row["reactions"]
    ({emoji name: action id}); that map is read first, so its chips win
    over the generic roles (a check chip means Handled on a draft card,
    not Send). Any other emoji falls back to the thumbs up / down roles.
    A reaction by the bot itself (`self_id`) never selects anything."""
    if not row or not owner_id or user_id != owner_id or row.get("status") != "live":
        return None
    if self_id and user_id == self_id:
        return None
    chip = (row.get("reactions") or {}).get(emoji_name)
    if chip and chip in (row.get("actions") or {}):
        return _event_for(row, chip, user_id, "reaction")
    items = _row_items(row)
    if emoji_name in REACTION_ROLES["primary"]:
        return _event_for(row, _pick_primary(items, not row.get("no_default")), user_id, "reaction")
    if emoji_name in REACTION_ROLES["cancel"]:
        return _event_for(row, _pick_cancel(items), user_id, "reaction")
    return None


def resolve_code(text: str, registry: CardRegistry, user_id: str,
                 owner_id: str, *, thread_key: str | None) -> ActionEvent | None:
    """The action an owner's code reply selects, else None.

    A bare code selects the primary action (none on a `no_default` card);
    a word before the code selects the action with that word. An unknown
    word resolves to None; the caller can tell that case apart with
    parse_code. The reply must be in the thread the card was posted in
    (`thread_key`).
    """
    if not owner_id or user_id != owner_id:
        return None
    parsed = parse_code(text)
    if not parsed:
        return None
    word, code = parsed
    row = registry.by_code(code, thread_key=thread_key)
    if not row:
        return None
    items = _row_items(row)
    if word is None:
        return _event_for(row, _pick_primary(items, not row.get("no_default")), user_id, "code")
    aid = next((a for a, _s, w in items if w and _norm_word(w) == word), None)
    return _event_for(row, aid, user_id, "code")
