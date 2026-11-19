#!/usr/bin/env python3
"""How far a ledger row, a briefing item or a hand-off may go to a hosted
model: its keep level (docs/remote-llm.md, "Keep from frontier models";
docs/llm-lanes.md 2.3).

A hosted model (Claude, Codex, any frontier model) works an item on its
own, with no owner asking in that turn, only at `summary` or `full`: a
hand-off to the EA channel, a scheduled lane's reply draft. Below that
(`local`, `facts`) the item stays on the owner's own model: the
remote briefing lists it under Needs you, and nothing hands it on.
Every path that decides this asks here, so one rule holds for all of
them: the remote briefing's candidates, hand-offs and reply queue, the
weekly audit's hand-offs, the bot's hand-off queue, a scheduled lane's
reply draft (draft_gate.cron_refusal), the inbox scan block and the
Replies card's rows, and the pre-send review's packet.

    label_level(item)    the level the item says itself, in code alone
    stricter(*levels)    the strictest of them
    levels(items)        {key: level}: the strictest of what each item
                         says and what the mail's records say
    annotate(items)      the same, written onto each item as `keep_level`
    may_hand_off(level)  True at `summary` and `full`
    split_handoffs(items)  (the ones a hosted model may take, the rest)
    refusal(items, ...)  '' or why a scheduled lane may not work them

What an item says itself (label_level): its `keep_level`, a `kept_level`
in plain words (privacy_rules.PLAIN), the same on its thread brief
(`thread_brief` or `brief`) and on its `metadata`, and a keep label
(`kept_from_frontier`, or the older `kept_from_claude`) on any of them.
A label with no level beside it reads `facts`, as does a reply-queue
entry's `kept`. What the records say: the strictest level among the thread's
records and the message's own (mail_privacy), and `local` for anyone on
the keep list among the item's addresses. When those cannot be read,
every item from mail (one with a conversation or message id) reads
`local`. The records live in the owner's private store, so every caller
is an owner process.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy_rules  # noqa: E402

LEVELS = privacy_rules.LEVELS
# The levels at which a hosted model may work an item on its own.
HANDOFF_LEVELS = privacy_rules.OPEN_LEVELS
LABELS = ("kept_from_frontier", "kept_from_claude")
_FROM_PLAIN = {v: k for k, v in privacy_rules.PLAIN.items()}
_ADDRESS_KEYS = ("from", "from_email", "sender")
_LIST_KEYS = ("to", "cc")

# The words the owner reads for an item kept from hosted models that
# needs more than a reply (the remote briefing's Needs you line).
KEPT_DECISION = ("Kept on your own model, so no hosted model works on it. It needs you: "
                 "open it in Outlook.")
# Why a scheduled lane may not draft on a kept thread (draft_gate).
KEPT_REFUSAL = "the thread is kept on the owner's own model"


def stricter(*found: str) -> str:
    """The strictest of these levels; `full` for none."""
    level = "full"
    for f in found:
        level = privacy_rules.stricter(level, f) if f in LEVELS else level
    return level


def may_hand_off(level) -> bool:
    """A hosted model may work the item with no owner asking: `summary`
    or `full`. Anything else, a missing level included, may not."""
    return level in HANDOFF_LEVELS


def _meta(item: dict) -> dict:
    raw = item.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            m = json.loads(raw)
        except ValueError:
            return {}
        return m if isinstance(m, dict) else {}
    return {}


def _brief(item: dict) -> dict:
    for obj in (item, _meta(item)):
        for key in ("thread_brief", "brief"):
            b = obj.get(key)
            if isinstance(b, dict):
                return b
    return {}


def _said(obj: dict) -> str:
    """The level one object states: its own level when it names one, else
    `facts` for a keep label or a `kept` flag, else ''."""
    lv = obj.get("keep_level")
    if lv in LEVELS:
        return lv
    lv = _FROM_PLAIN.get(str(obj.get("kept_level") or "").strip())
    if lv:
        return lv
    if any(obj.get(k) for k in LABELS) or obj.get("kept") is True:
        return "facts"
    return ""


def label_level(item: dict) -> str:
    """The strictest level the item, its metadata and its thread brief
    state; `full` when none states one. Code alone: no store is read."""
    level = "full"
    for obj in (item, _meta(item), _brief(item)):
        said = _said(obj) if isinstance(obj, dict) else ""
        if said:
            level = privacy_rules.stricter(level, said)
    return level


def _ids(item: dict) -> tuple[str, str]:
    brief = _brief(item)
    conv = str(item.get("conversation_id") or item.get("conversationId")
               or brief.get("conversation_id") or "")
    mid = str(item.get("source_message_id") or item.get("message_id") or "")
    return conv, mid


def _from_mail(item: dict) -> bool:
    conv, mid = _ids(item)
    return bool(conv or mid)


def _addresses(item: dict) -> list[str]:
    out = []
    for k in _ADDRESS_KEYS:
        v = item.get(k)
        if isinstance(v, dict):
            v = v.get("address") or (v.get("emailAddress") or {}).get("address")
        if isinstance(v, str) and "@" in v:
            out.append(v)
    for k in _LIST_KEYS:
        for v in item.get(k) or []:
            if isinstance(v, dict):
                v = v.get("address") or (v.get("emailAddress") or {}).get("address")
            if isinstance(v, str) and "@" in v:
                out.append(v)
    return out


def _records():
    """(config, index, keep context): the owner's keep list, the local
    model's records and the rules in force. With no local model the index
    is empty and the list too. Raises when they cannot be read."""
    import mail_privacy
    import remote_llm
    cfg = remote_llm.config()
    return cfg, mail_privacy.load_index(), mail_privacy.keep_context(cfg)


def _record_level(item: dict, cfg: dict, index: dict, ctx: dict) -> str:
    import mail_privacy
    import remote_llm
    level = "full"
    conv, mid = _ids(item)
    if conv:
        level = privacy_rules.stricter(level, mail_privacy.conversation_level(conv, index, ctx))
    rec = index["by_id"].get(mid) if mid else None
    if rec is not None:
        level = privacy_rules.stricter(level, mail_privacy.level_of(rec, index=index, ctx=ctx))
    if any(remote_llm.keep_reason(a, cfg) for a in _addresses(item)):
        level = "local"
    return level


def levels(items: Iterable[dict], *, key: Callable[[dict], str] | None = None,
           records=None, strict: bool = False) -> dict[str, str]:
    """{key(item): level} for each item, the strictest of label_level and
    the mail's records. `key` defaults to the row id (`id`, else
    `ledger_id`). `records` stands in for the store: (config, index,
    context), a function from an item to its records' level, or False for
    none. Fails closed: when the records cannot be read, or one item's
    check fails, an item from mail reads `local`; with `strict` the
    failure raises instead, for a caller that would rather ask again."""
    key = key or (lambda i: str(i.get("id") or i.get("ledger_id") or ""))
    items = [i for i in items if isinstance(i, dict)]
    out: dict[str, str] = {}
    try:
        recs = _records() if records is None else (records or None)
        if recs is not None and not callable(recs):
            cfg, index, ctx = recs
            recs = lambda i: _record_level(i, cfg, index, ctx)  # noqa: E731
    except Exception as e:
        if strict:
            raise
        print(f"keep_gate: the keep check could not run, mail items stay local: {str(e)[:120]}",
              file=sys.stderr)
        for i in items:
            out[key(i)] = "local" if _from_mail(i) else label_level(i)
        return out
    for i in items:
        level = label_level(i)
        if recs is not None and level != "local":
            try:
                found = recs(i)
            except Exception:
                if strict:
                    raise
                found = None
            if found in LEVELS:
                level = stricter(level, found)
            elif _from_mail(i):
                level = "local"
        k = key(i)
        out[k] = privacy_rules.stricter(out.get(k, "full"), level) if k in out else level
    return out


def annotate(items: Iterable[dict], *, records=None, strict: bool = False) -> list[dict]:
    """Write each item's level (levels) onto it as `keep_level`; returns
    the items."""
    items = [i for i in items if isinstance(i, dict)]
    found = levels(items, key=lambda i: str(id(i)), records=records, strict=strict)
    for i in items:
        i["keep_level"] = found.get(str(id(i)), "local")
    return items


def mark_kept(rows: Iterable[dict], *, records=None) -> list[dict]:
    """Reply-queue rows (ledger.reply_backlog) with `keep_level` and
    `kept` set from the shared check: kept below `summary`."""
    rows = annotate(rows, records=records)
    for r in rows:
        r["kept"] = bool(r.get("kept")) or not may_hand_off(r["keep_level"])
    return rows


def split_handoffs(items: Iterable[dict], *, records=None,
                   strict: bool = False) -> tuple[list[dict], list[dict]]:
    """(the hand-offs a hosted model may take, the ones it may not), each
    item checked again here with its own `keep_level` as one source."""
    items = annotate(items, records=records, strict=strict)
    ok = [i for i in items if may_hand_off(i["keep_level"])]
    return ok, [i for i in items if not may_hand_off(i["keep_level"])]


def refusal(items: Iterable[dict] = (), *, conversation_id: str = "", records=None) -> str:
    """'' when a hosted model may work these rows and this thread on its
    own, else KEPT_REFUSAL. Fails closed as levels() does."""
    pool = [i for i in items if isinstance(i, dict)]
    if conversation_id:
        pool.append({"conversation_id": conversation_id})
    if not pool:
        return ""
    found = levels(pool, key=lambda i: str(id(i)), records=records)
    return "" if all(may_hand_off(v) for v in found.values()) else KEPT_REFUSAL
