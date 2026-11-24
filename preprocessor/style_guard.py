"""Deterministic diction guard for EA output.

A pure-text scrub applied to every outbound Slack message, independent
of model adherence. Email drafts have their own enforcement layers
(per-prompt STYLE_RULES and the rewrite hook on created drafts); Slack
reply text has only this.

Scope is deliberately narrow:
  - em-dash (—) and double-hyphen surrounded by spaces ( -- ) become
    a comma joint, per the substitution rule (commas/colons/semicolons).
  - en-dash (–) is NOT touched: legitimate range typography
    ("4–6 PM", "Jun 19–30") that the briefings themselves use.
  - text inside backtick code spans / fenced blocks is left alone
    (commands and ids must not be rewritten).
"""
from __future__ import annotations

import re

_EM_DASH_JOINT = re.compile(r"\s*—\s*")
_DOUBLE_HYPHEN_JOINT = re.compile(r"(?<=\S) -- (?=\S)")
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def strip_em_dashes(text: str) -> tuple[str, int]:
    """Return (cleaned_text, replacements_made).

    Em-dashes used as a joint become ", "; leading/trailing ones are
    dropped. Code spans pass through untouched."""
    if not text or ("—" not in text and " -- " not in text):
        return text, 0
    count = 0
    parts = _CODE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:  # code span, untouched
            continue
        n_em = part.count("—")
        n_hy = len(_DOUBLE_HYPHEN_JOINT.findall(part))
        if not n_em and not n_hy:
            continue
        part = _EM_DASH_JOINT.sub(", ", part)
        part = _DOUBLE_HYPHEN_JOINT.sub(", ", part)
        # an em-dash at line start or end leaves a dangling ", "
        part = re.sub(r"(?m)^, ", "", part)
        part = re.sub(r"(?m), $", "", part)
        parts[i] = part
        count += n_em + n_hy
    return "".join(parts), count


# ── where a card's button is: the bot's own pointer says it ──────────
# The bot posts a one-line pointer to every :lock: card in the thread that
# asked for it (ea-chat-bot._post_dm_breadcrumb). A reply that says where
# the card is as well ("The Send button is on the card in Action requests.")
# says it twice, so that clause goes; the rest of the reply stays.

_PLACE = (r"(?:\*\*|_)?(?:Action requests|(?:[Yy]our|[Tt]he) DM(?: with the bot)?"
          r"|[A-Z][\w.'-]*['’]s DM)(?:\*\*|_)?")
_THING = (r"(?:(?:[Tt]he|[Ee]ach|[Ii]ts|[Tt]heir|[Yy]our)\s+)?"
          r"(?:(?:\*\*|`)?[A-Za-z:]+(?:\*\*|`)?\s+(?:and\s+(?:\*\*|`)?[A-Za-z]+(?:\*\*|`)?\s+)?)?"
          r"(?:buttons?|cards?|blocks?)")
_POINTER_CLAUSE = re.compile(
    _THING + r"\s+(?:is|are|sits?|waits?)\s+(?:on\s+(?:the|its|their)\s+cards?\s+)?in\s+" + _PLACE)
# Where the clause may start: a sentence's start, or a joint inside it.
_JOINT = re.compile(r"(?:[.!?]\s+|[;:]\s+|,?\s+and\s+|,\s+)")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_EMOJI_ONLY = re.compile(r"\s*(?::[a-z0-9_+-]+:\s*)*")
# The most words a clause that goes may hold.
POINTER_WORDS = 20


def _strip_pointer_line(line: str) -> tuple[str, int]:
    n, at = 0, 0
    while True:
        m = _POINTER_CLAUSE.search(line, at)
        if not m:
            return line, n
        # the clause runs to its sentence's end (the mark kept for the rest)
        e = _SENTENCE_END.search(line, m.end())
        end = e.end() if e else len(line)
        # and starts at the sentence's start or the last joint before it
        start, joint = 0, None
        for j in _JOINT.finditer(line, 0, m.start()):
            start, joint = j.end(), j
        lead = line[start:m.start()]
        words = len(line[start:end].split())
        if not _EMOJI_ONLY.fullmatch(lead) or words > POINTER_WORDS:
            at = m.end()   # words of the reply's own in the clause: it stays
            continue
        if joint is not None and not joint.group().lstrip().startswith((".", "!", "?")) \
                and line[:joint.start()].strip():
            # "Drafted; the Send button is in X." -> "Drafted."
            mark = line[end - 1] if e else "."
            line = line[:joint.start()].rstrip() + mark + line[end:]
        else:
            cut = joint.end() if joint is not None else 0
            line = line[:cut] + line[end:].lstrip()
        n += 1
        at = 0


def strip_card_pointers(text: str) -> tuple[str, int]:
    """Return (text, clauses removed): each clause that says where a
    confirmation card or its button is ("The Send button is on the card in
    Action requests.", "; the Send button is in your DM.") goes, at most
    POINTER_WORDS words of its sentence; a line left empty goes with it.
    Code spans pass through untouched."""
    if not text or not _POINTER_CLAUSE.search(text):
        return text, 0
    count = 0
    parts = _CODE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            continue
        lines = []
        for line in part.split("\n"):
            out, k = _strip_pointer_line(line)
            count += k
            if k and not out.strip():
                continue
            lines.append(out.rstrip() if k else out)
        parts[i] = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip("\n")
    return out, count


# ── a turn's lines in Action requests: plain words ───────────────────
# The web chat shows a reply turn's own lines on its request in Action
# requests, where the owner reads them as the request's story. A script or
# a mail verb's name and a draft's full id are the tools' words, not his.

_ID = r"[A-Za-z0-9+/_=-]{24,}"
_TOOL = (r"(?:[\w./-]*\.py(?:\s+[a-z][\w-]*)*"
         r"|(?:create|update|delete|send|get|list|search|attach|remove|note|scan|describe|"
         r"preflight|propose|close|mark|file|set)-[a-z][\w-]*)")
_ID_PHRASE = re.compile(rf"[ \t]*[,(]?[ \t]*\b(?:[Dd]raft\s+)?(?:ID|[Ii]d)\b:?[ \t]*`?{_ID}`?\)?")
_ID_CODE = re.compile(rf"`{_ID}`")
_TOOL_BY = re.compile(rf"\s+(?:with|via|using|through|by running|by calling|by)\s+`{_TOOL}`")
# A tool that opens a sentence, after a list mark or an emoji: the verb
# after it opens the sentence instead.
_TOOL_HEAD = re.compile(rf"((?:^[ \t]*(?:\d+[.)][ \t]+|[-*][ \t]+)?|[.!?][ \t]+)(?::[a-z0-9_+-]+:[ \t]*)*)"
                        rf"`{_TOOL}`[ \t]+([a-z])", re.M)


def plain_turn_text(text: str) -> tuple[str, int]:
    """Return (text, changes): the reply without a draft's or a message's
    full id ("Draft ID: `AAMk...`") and without the name of a script or a
    mail verb where it opens a sentence or names the tool a step used, each
    sentence kept readable: "`create-reply` created the draft" reads
    "Created the draft", "recorded with `ledger.py note-reply`" reads
    "recorded". A tool named anywhere else stays ("Run `ledger.py list`")."""
    if not text or "`" not in text and not re.search(r"\b(?:ID|[Ii]d)\b", text):
        return text, 0
    n = 0

    def sub(rx, repl, s):
        nonlocal n
        out, k = rx.subn(repl, s)
        n += k
        return out
    out = sub(_ID_PHRASE, "", text)
    out = sub(_ID_CODE, "", out)
    out = sub(_TOOL_BY, "", out)
    out = sub(_TOOL_HEAD, lambda m: m.group(1) + m.group(2).upper(), out)
    if not n:
        return text, 0
    out = re.sub(r"[ \t]+([.,;:!?])(?=\s|$)", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"(?m)^[ \t]*[.,;:][ \t]*$", "", out)
    out = re.sub(r"(?m)[ \t]+$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip("\n")
    return out, n
