#!/usr/bin/env python3
"""EA-written mail: which sent messages came from a body the EA wrote, so
the style profiles learn only from mail the user wrote.

A sent message whose own text comes mostly from an EA body is the EA's
writing, even after the user's edits. A style profile that folds it in
learns the writer's voice and hands it back to the writer as the user's:
each round moves the profile further from the user. So the profiles skip
such mail (style_profiles.update_from_messages) and purge what they
already hold (style_profiles.purge_ea_written).

The EA bodies come from two records that already exist:
  - draft_capture's store: the last EA body of every draft, captured when
    the draft is written, kept one month (RETENTION_DAYS there). It
    covers the profiles' own windows: each briefing's sent mail and the
    weekly three-week sync.
  - the remote writer's logs in the owner's private directory
    (remote-llm/draft-writer.jsonl, remote-llm/draft-ab.jsonl): every
    body the writer produced, from the day it went live. They cover the
    one-time purge of older mail. A body the subagent wrote before
    either record existed is not on file, so a profile can keep some of
    that mail.

The match: the share of the sent text's words that come, in runs of RUN
words or more, from one EA body for the same conversation (or, for a
body with no conversation on record, one written in the WINDOW_DAYS
before the send). At SHARE or above the message is EA-written; a message
the user rewrote past that point counts as theirs. On a month of real
sent mail the shares split in two: most messages near 0, most EA drafts
above 0.9, few in between.

Best-effort: a record that cannot be read adds nothing. `available` says
whether the owner's private directory was readable (an install without
the remote writer has no logs there, and that is a complete answer); a
one-time purge of older mail counts as done only then.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

HISTORY = (("remote-llm/draft-writer.jsonl", "body"),
           ("remote-llm/draft-ab.jsonl", "qwen_body"),
           ("remote-llm/draft-ab.jsonl", "claude_body"))
WINDOW_DAYS = 14
# The send may follow the last write by days; a body written more than
# this after the send is a later draft in the thread, not the one sent.
AFTER_SEND = timedelta(hours=1)
SHARE = 0.5
RUN = 3
_WORD = re.compile(r"[A-Za-z0-9']+")
_TAG = re.compile(r"<[^>]+>")


def _when(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


class Matcher:
    """Answers whether a sent message is EA-written, from EA bodies given
    as records of ts, conversation_id and text."""

    def __init__(self, records: list[dict], available: bool = True):
        self.available = available
        self.by_conv: dict[str, list[tuple[datetime | None, list[str]]]] = {}
        self.loose: list[tuple[datetime, list[str]]] = []
        for r in records:
            ws = words(r.get("text") or "")
            if not ws:
                continue
            when = _when(r.get("ts"))
            conv = r.get("conversation_id") or ""
            if conv:
                self.by_conv.setdefault(conv, []).append((when, ws))
            elif when:
                self.loose.append((when, ws))

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_conv.values()) + len(self.loose)

    def share(self, text: str, conversation_id: str = "", sent_at: str = "") -> float:
        """The largest share of `text`'s words that one EA body supplies."""
        sent = words(text)
        if not sent:
            return 0.0
        when = _when(sent_at)
        cands = [ws for ts, ws in self.by_conv.get(conversation_id or "", [])
                 if when is None or ts is None or ts <= when + AFTER_SEND]
        if when is not None:
            lo = when - timedelta(days=WINDOW_DAYS)
            cands += [ws for ts, ws in self.loose if lo <= ts <= when + AFTER_SEND]
        best = 0.0
        vocab = set(sent)
        run = min(RUN, len(sent))
        for ws in cands:
            # Cheap first cut: too few shared words cannot reach SHARE.
            if len(vocab & set(ws)) < SHARE * len(vocab):
                continue
            sm = SequenceMatcher(None, sent, ws, autojunk=False)
            # Runs of RUN words or more are copied phrases; single shared
            # words ("the", "I", "to") are in any two emails.
            got = sum(b.size for b in sm.get_matching_blocks() if b.size >= run)
            best = max(best, got / len(sent))
        return best

    def is_ea_written(self, text: str, conversation_id: str = "", sent_at: str = "") -> bool:
        return self.share(text, conversation_id, sent_at) >= SHARE


def _read_jsonl(path: Path):
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _captures() -> list[dict]:
    try:
        import draft_capture
        recs = draft_capture.load_all()
    except Exception:
        return []
    out = []
    for r in recs:
        body = r.get("body") or ""
        out.append({"ts": r.get("captured_at"), "conversation_id": r.get("conversation_id") or "",
                    "text": _TAG.sub(" ", body) if "<" in body else body})
    return out


def _history(folder: Path | None) -> tuple[list[dict], bool]:
    try:
        import owner_trust
        folder = folder or owner_trust.private_dir()
        if not os.access(folder, os.R_OK | os.X_OK):
            return [], False
    except Exception:
        return [], False
    out = []
    for rel, field in HISTORY:
        for r in _read_jsonl(folder / rel):
            if r.get(field):
                out.append({"ts": r.get("ts"), "text": r[field],
                            "conversation_id": r.get("conversation_id") or ""})
    return out, True


def load(folder: Path | None = None) -> Matcher:
    """The matcher over draft_capture's store and the writer's logs in
    `folder` (default: the owner's private directory)."""
    history, readable = _history(folder)
    return Matcher(_captures() + history, available=readable)
