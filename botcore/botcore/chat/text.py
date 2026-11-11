"""Text helpers shared by the frontends: chunking, mrkdwn conversion and
topic names.

The LLM writes Slack mrkdwn on every platform; a frontend that renders
another markup converts on the way out. mrkdwn_to_markdown is that
conversion for the Markdown the web chat renders.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from . import card_text as _ct


def smart_chunk(text: str, limit: int = 3000) -> list[str]:
    """Split text into chunks at paragraph boundaries, respecting the limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last double-newline within the limit
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            # Fall back to single newline
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            # Fall back to space
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


_CODE_SPLIT_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_ANGLE_RE = re.compile(r"<([^<>\s][^<>]*)>")
_URL_RE = re.compile(r"^([a-z][a-z0-9+.-]*:[^|\s]+)(?:\|(.*))?$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^#([A-Za-z0-9:_-]+)(?:\|(.*))?$")
_USER_RE = re.compile(r"^@([A-Za-z0-9]+)(?:\|.*)?$")
_BOLD_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE_RE = re.compile(r"(?<![\w~])~(?=\S)([^~\n]+?)(?<=\S)~(?![\w~])")
_HOLD_RE = re.compile(r"\x00(\d+)\x00")


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _convert_plain(text: str, mention: Callable[[str], str] | None) -> str:
    """Convert one stretch of text that holds no code."""
    held: list[str] = []

    def hold(out: str) -> str:
        held.append(out)
        return f"\x00{len(held) - 1}\x00"

    def angle(m: re.Match) -> str:
        inner = m.group(1)
        if cm := _CHANNEL_RE.match(inner):
            cid, name = cm.group(1), cm.group(2)
            if mention is not None:
                return hold(mention(cid))
            return hold(f"#{name or cid}")
        if um := _USER_RE.match(inner):
            return hold(f"@**{um.group(1)}**")
        if lm := _URL_RE.match(inner):
            url, label = lm.group(1), lm.group(2)
            url = _unescape(url)
            return hold(f"[{_unescape(label)}]({url})" if label else url)
        return m.group(0)

    text = _ANGLE_RE.sub(angle, text)
    text = _BOLD_RE.sub(r"**\1**", text)
    text = _ITALIC_RE.sub(r"*\1*", text)
    text = _STRIKE_RE.sub(r"~~\1~~", text)
    text = _unescape(text)
    return _HOLD_RE.sub(lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held)
                        else "", text)


# An inert span (card_text.inert) the template wrapped in backticks: the
# span is a code span already.
_TICKED_INERT_RE = re.compile(
    f"`({_ct.INERT_OPEN}[^{_ct.INERT_OPEN}{_ct.INERT_CLOSE}]*{_ct.INERT_CLOSE})`")
_INERT_HOLD_RE = re.compile(r"\x01(\d+)\x01")


def mrkdwn_to_markdown(text: str, mention: Callable[[str], str] | None = None) -> str:
    """Slack mrkdwn to the Markdown the web chat renders; code spans and
    fences stay as written, and emoji shortcodes keep their Slack names.

    `<#C123>` becomes mention("C123") when a mention function is given,
    else `#C123` (or `#name` for the `<#C123|name>` form). An inert span
    (card_text.inert) becomes a code span whatever it holds.
    """
    # The converter's own placeholders (\x00n\x00 and \x01n\x01) cannot
    # come from the text: a forged one would pick another span or none.
    text = (text or "").replace("\x00", "").replace("\x01", "")
    held: list[str] = []
    if _ct.INERT_OPEN in text:
        text = _TICKED_INERT_RE.sub(r"\1", text)

        def hold(m: re.Match) -> str:
            held.append(_ct.code_span(m.group(1)))
            return f"\x01{len(held) - 1}\x01"
        text = _ct.INERT_RE.sub(hold, text)
        text = text.replace(_ct.INERT_OPEN, "").replace(_ct.INERT_CLOSE, "")
    parts = _CODE_SPLIT_RE.split(text)
    out = "".join(p if i % 2 else _convert_plain(p, mention) for i, p in enumerate(parts))
    if held:
        out = _INERT_HOLD_RE.sub(
            lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held) else "", out)
    return out


# The first line of every message carried over from Slack history: the
# author, the original time and this mark. The web chat's store holds such
# messages in history imported from Zulip, where the port first put them.
# Readers of recent chat (the briefing's chat_read) skip marked messages,
# since their timestamps are the day of the port, not the day they were
# written.
PORTED_MARK = "from Slack"
_PORTED_RE = re.compile(r"\*\*[^*\n]+\*\* · \d{4}-\d{2}-\d{2} \d{2}:\d{2} · from Slack(?:\n|$)")


def ported_header(who: str, when: str) -> str:
    """The header line of a ported message; when is "YYYY-MM-DD HH:MM"."""
    return f"**{who}** · {when} · {PORTED_MARK}"


def is_ported(text: str) -> bool:
    """True for a message that opens with a ported_header line."""
    return bool(_PORTED_RE.match(text or ""))
