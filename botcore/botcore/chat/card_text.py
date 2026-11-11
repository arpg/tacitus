"""Text that goes on a confirmation card, made safe to show.

Much of what a card shows comes from the model or from mail: a subject, a
display name, a location, a plugin item's fields, a commit subject, the
draft body. A frontend renders card text as markup, so that text could
forge a field line, the rule above the actions, the action legend or a
link whose text is not its target, mention someone, or hide characters
that reorder or conceal what the owner reads. This module is the one
place that makes such text inert.

- `clean` replaces every character that changes how text displays without
  being seen (bidi controls, zero-width characters, other control and
  format characters, private-use and tag characters) with a visible mark
  such as `[U+202E]`. Tab and newline stay; other line breaks become
  newlines.
- `one_line` cleans, folds every run of white space (newlines included)
  into one space, and cuts at `limit` characters with a visible marker
  naming how much was cut.
- `inert` wraps `one_line`'s result in two private-use sentinels. A card
  builder puts it around each model or mail value it writes into card
  markup (a plugin template, a proposal line, a commit subject). The
  frontends render the span between the sentinels as literal text: the
  web chat as a code span, Slack with its markup characters escaped.
  `clean` marks any sentinel already in the value, so a value cannot open
  or close a span of its own.

Field values (To, Subject, Location and the rest) are always rendered as
inert text by the frontends themselves; the builder need not wrap them.
"""
from __future__ import annotations

import re
import unicodedata

INERT_OPEN = "\ue000"
INERT_CLOSE = "\ue001"
INERT_RE = re.compile(f"{INERT_OPEN}([^{INERT_OPEN}{INERT_CLOSE}]*){INERT_CLOSE}")

# The longest single-line value a card shows; recipient lists get more
# room, so a long To line is still shown in full where a card can.
FIELD_CHARS = 300
RECIPIENT_FIELD_CHARS = 2000
RECIPIENT_LABELS = frozenset({"to", "cc", "bcc", "reply-to", "from", "attendees",
                              "optional"})
# The label of the collapsed part of a draft body. That part matched an
# earlier message of the thread (mail_links.verified_quote_start, run by
# the owner-side draft read), so the label says only where it is and what
# it matched.
QUOTED_LABEL = "Rest of the body: text that matches an earlier message of the thread"
CUT_MARK = "[cut: {n} more characters]"
CUT_RE = re.compile(r"\[cut: \d+ more characters\]$")

_LINE_BREAK_RE = re.compile("\r\n|[\r\u2028\u2029\x85\x0b\x0c]")

# Code points that render as nothing, or change the order or look of the
# text around them, beyond what Unicode's Cc/Cf/Co/Cs/Cn categories hold.
_INVISIBLE_EXTRA = frozenset(
    [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E,
     0x180F, 0x2800, 0x3164, 0xFFA0]
    + list(range(0xFFF9, 0xFFFC))
    + list(range(0xE0100, 0xE01F0)))
_ZWJ, _ZWNJ, _VS16 = 0x200D, 0x200C, 0xFE0F


def _mark(ch: str) -> str:
    return f"[U+{ord(ch):04X}]"


def _hidden(ch: str) -> bool:
    cp = ord(ch)
    if ch in "\t\n":
        return False
    if cp in _INVISIBLE_EXTRA:
        return True
    return unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Cn")


def _joiner_ok(text: str, i: int) -> bool:
    """A zero-width joiner inside an emoji sequence, or a non-joiner
    between two letters of a script that uses it, is part of the text."""
    prev = text[i - 1] if i > 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if not prev or not nxt:
        return False
    if ord(text[i]) == _ZWJ:
        def emoji(c: str) -> bool:
            return ord(c) == _VS16 or unicodedata.category(c) in ("So", "Sk")
        return emoji(prev) and emoji(nxt)
    return (ord(prev) > 0x7F and ord(nxt) > 0x7F
            and unicodedata.category(prev).startswith("L")
            and unicodedata.category(nxt).startswith("L"))


def clean(text, keep_inert: bool = False) -> str:
    """`text` with every invisible or reordering character shown as a
    mark (`[U+202E]`); other line breaks become newlines. `keep_inert`
    keeps the inert sentinels, for card text a builder wrote."""
    text = _LINE_BREAK_RE.sub("\n", "" if text is None else str(text))
    if text.isascii() and text.isprintable():
        return text
    out = []
    for i, ch in enumerate(text):
        if keep_inert and ch in (INERT_OPEN, INERT_CLOSE):
            out.append(ch)
        elif _hidden(ch) and not (ord(ch) in (_ZWJ, _ZWNJ) and _joiner_ok(text, i)):
            out.append(_mark(ch))
        else:
            out.append(ch)
    return "".join(out)


def one_line(text, limit: int = FIELD_CHARS, keep_inert: bool = False) -> str:
    """`text` cleaned and on one line, cut at `limit` characters with a
    visible marker; limit 0 does not cut. `keep_inert` keeps a builder's
    inert spans, and a cut inside one closes it."""
    flat = " ".join(clean(text, keep_inert).split())
    if limit and len(flat) > limit:
        keep = max(1, limit - len(CUT_MARK) - 6)
        head = flat[:keep].rstrip()
        if head.rfind(INERT_OPEN) > head.rfind(INERT_CLOSE):
            head += INERT_CLOSE
        return head + " " + CUT_MARK.format(n=len(flat) - keep)
    return flat


def was_cut(text: str) -> bool:
    """True when `text` ends with one_line's cut marker."""
    return bool(CUT_RE.search(text or ""))


def field_limit(label: str) -> int:
    """The length a card shows of a field's value."""
    return RECIPIENT_FIELD_CHARS if (label or "").strip().lower() in RECIPIENT_LABELS \
        else FIELD_CHARS


def inert(text, limit: int = FIELD_CHARS) -> str:
    """`text` as a span every frontend renders as literal text."""
    return f"{INERT_OPEN}{one_line(text, limit)}{INERT_CLOSE}"


def strip_inert(text: str) -> str:
    """`text` with the inert sentinels removed, the spans kept as text."""
    return (text or "").replace(INERT_OPEN, "").replace(INERT_CLOSE, "")


# ── Markdown ─────────────────────────────────────────────────────────

def code_span(text: str) -> str:
    """`text` as a Markdown code span: no markup, link, mention or emoji
    inside it renders. The delimiter is longer than any backtick run in
    the text, so the text cannot close it."""
    if not text:
        return ""
    run = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    tick = "`" * (run + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


# ── Slack ────────────────────────────────────────────────────────────

def slack_escape(text: str) -> str:
    """`text` with Slack's three control characters escaped: no link,
    mention or channel ping can form from it."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Slack's special tokens: a user or group mention, a channel link and a
# broadcast (`<!channel>`, `<!here>`, `<!subteam^ID>`). Card text the bot
# writes never holds one, so in card text each shows as typed.
_SLACK_SPECIAL_RE = re.compile(r"<([!@#][^<>]*)>")
# A labelled link, `<url|label>`, whose label hides where it points.
_SLACK_LABELLED_RE = re.compile(r"<([^<>|\s]+)\|([^<>]*)>")


def slack_defang(text: str) -> str:
    """Slack mrkdwn with every special token (`<!channel>`, `<@U1>`,
    `<#C1>`) shown as typed and every labelled link showing its target."""
    text = _SLACK_SPECIAL_RE.sub(lambda m: f"&lt;{m.group(1)}&gt;", text or "")
    return _SLACK_LABELLED_RE.sub(
        lambda m: (f"{m.group(2).strip()} (<{m.group(1)}>)" if m.group(2).strip()
                   else f"<{m.group(1)}>"), text)


def slack_spans(text: str) -> str:
    """`text` with each inert span escaped and stray sentinels dropped, the
    rest as written: for a plain post that names an item value."""
    return INERT_RE.sub(lambda m: slack_escape(m.group(1)), text or "").replace(
        INERT_OPEN, "").replace(INERT_CLOSE, "")


def slack_inert(text: str) -> str:
    """Card text for Slack mrkdwn: each inert span cleaned and escaped; in
    the rest, no special token pings or mentions anyone and no labelled
    link hides its target (slack_defang)."""
    out, pos = [], 0
    text = text or ""
    for m in INERT_RE.finditer(text):
        out.append(slack_defang(text[pos:m.start()]))
        out.append(slack_escape(m.group(1)))
        pos = m.end()
    out.append(slack_defang(text[pos:]))
    return "".join(out).replace(INERT_OPEN, "").replace(INERT_CLOSE, "")
