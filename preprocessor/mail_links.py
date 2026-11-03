#!/usr/bin/env python3
"""Facts about an email body: its redirect links, where its quoted history
starts and where the part a card may collapse starts (verified_quote_start),
and the remote resources its HTML loads (remote_resources, at the end of
this file).

Redirect links. A link that goes through a wrapper instead of to its
address: Gmail's `https://www.google.com/url?q=<address>&...&ust=...`
(written into a draft by the claude.ai Gmail connector's create_draft, and
carried by any link copied out of a Google Calendar or Gmail page) and
Outlook's `*.safelinks.protection.outlook.com/?url=...`. The recipient sees
the wrapper, not the address, and a Gmail wrapper stops working about a day
after it was made. The Send card warns about them (redirect_warning).

Quoted history. A plain-text reply ends with an attribution line, "On <date>
<sender> wrote:" (Gmail wraps a long one over two lines), and then only
quoted lines, each starting with ">". quote_start finds that block, so
gmail_client keeps it when a reply draft's text is replaced, and the redirect
check looks at the new text only: a link inside the quoted email is the
sender's, not ours.
"""
from __future__ import annotations

import re

try:
    import card_text as _ct
except ModuleNotFoundError:  # imported as preprocessor.mail_links
    from preprocessor import card_text as _ct

REDIRECT_RE = re.compile(
    r"https?://(?:www\.)?google\.[a-z]{2,3}(?:\.[a-z]{2})?/url\?[^\s\"'<>]*"
    r"|https?://[a-z0-9.-]*safelinks\.protection\.outlook\.com/[^\s\"'<>]*",
    re.IGNORECASE)
_ATTRIBUTION_START_RE = re.compile(r"^On\s.+", re.IGNORECASE)
_WROTE_RE = re.compile(r"wrote:\s*$", re.IGNORECASE)


def redirect_links(text: str) -> list[str]:
    """The redirect-wrapped links in `text`, each once, in order."""
    found: list[str] = []
    for m in REDIRECT_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:)]")
        if url not in found:
            found.append(url)
    return found


def quote_start(text: str) -> int:
    """The index in `text` where its trailing quoted history begins: an
    attribution line (one line, or two when wrapped) followed by nothing
    but lines that start with ">" and blank lines. len(text) when there is
    none."""
    text = text or ""
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    for i, line in enumerate(lines):
        if not _ATTRIBUTION_START_RE.match(line.strip()):
            continue
        if _WROTE_RE.search(line):
            body = i + 1
        elif i + 1 < len(lines) and _WROTE_RE.search(lines[i + 1]) \
                and not lines[i + 1].lstrip().startswith(">"):
            body = i + 2
        else:
            continue
        rest = [ln.strip() for ln in lines[body:]]
        if any(rest) and all(not ln or ln.startswith(">") for ln in rest):
            return offsets[i]
    return len(text)


# How far into a body the quoted part may be looked for, in lines: a cap
# on the work, not on what a card shows (past it nothing is collapsed).
VERIFY_LINES_MAX = 4000
_QUOTE_MARK_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
_SPACE_RE = re.compile(r"\s+")


def _norm_quoted(text: str) -> str:
    """Text for comparing a quote with its original: quote marks at line
    starts and all white space removed, compatibility forms folded, case
    folded. Rewrapping and requoting change none of it."""
    import unicodedata
    t = _QUOTE_MARK_RE.sub("", unicodedata.normalize("NFKC", text or ""))
    return _SPACE_RE.sub("", t).casefold()


def verified_quote_start(text: str, start: int, references) -> int:
    """Where the part of `text` a card may collapse begins: the first line
    start at or after `start` from which the rest of `text` is, quote
    marks and white space aside, the end of one of `references` (the
    earlier messages of the thread, read by the owner). len(text) when no
    such line exists, when `start` is past the end, or when there are no
    references: then nothing is collapsed and the card shows it all.

    The quote boundary a draft carries (a "From:" header, an "On ...
    wrote:" line) is text the model can write, so it alone never decides
    what the card hides; only text that really ends an earlier message
    does. The header lines above that text stay in view."""
    text = text or ""
    n = len(text)
    start = max(0, int(start))
    if start >= n:
        return n
    refs = [r for r in (_norm_quoted(x) for x in references or []) if r]
    if not refs:
        return n
    cuts = [start]
    pos = text.find("\n", start)
    while pos != -1 and pos + 1 < n and len(cuts) < VERIFY_LINES_MAX:
        cuts.append(pos + 1)
        pos = text.find("\n", pos + 1)
    for cut in cuts:
        tail = _norm_quoted(text[cut:])
        if not tail:
            return n
        if any(r.endswith(tail) for r in refs):
            return cut
    return n


def new_text(text: str) -> str:
    """`text` without its trailing quoted history."""
    return (text or "")[:quote_start(text)]


def redirect_warning(links: list[str]) -> str:
    """The Send card line for a draft with redirect links, or ""."""
    if not links:
        return ""
    hosts = sorted({re.sub(r"^https?://([^/?#]+).*$", r"\1", u, flags=re.I).lower()
                    for u in links})
    n = len(links)
    return (f":warning: *Redirect links:* {n} link{'s' if n != 1 else ''} in this "
            f"draft go{'es' if n == 1 else ''} through {', '.join(hosts)}, not to the "
            f"real address. Tell the EA to replace "
            f"{'it' if n == 1 else 'them'} before you send.")


# Remote resources. An HTML body can load content from outside the message
# when it is opened: an image, a stylesheet, a frame or a CSS background.
# The card shows a text extract, which drops tag attributes, so the owner
# would not see these; the card names them on a warning line instead.
_TAG_RE = re.compile(r"<[a-z][^>]*>", re.IGNORECASE | re.DOTALL)
_RESOURCE_ATTR_RE = re.compile(
    r"""\s(src|srcset|lowsrc|dynsrc|background|poster|data|href)\s*=\s*"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_HREF_LOADS_RE = re.compile(r"^<\s*(link|image|use)\b", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)""",
                         re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_INLINE_SCHEMES = ("cid:", "mid:", "data:", "about:")


def _remote(url: str) -> bool:
    u = url.strip().lower()
    if not u or u.startswith(_INLINE_SCHEMES) or u.startswith("#"):
        return False
    return u.startswith("//") or bool(_SCHEME_RE.match(u))


def remote_resources(html: str) -> list[str]:
    """The remote URLs an HTML body loads when it is opened, in order and
    without repeats: src, srcset, background, poster and data attributes,
    href on <link>, <image> and <use>, CSS url() and @import. A cid: or
    data: resource is part of the message, so it is not listed. A link the
    reader clicks (<a href>) is not a resource; the text extract shows it."""
    import html as _html
    found: list[str] = []

    def _add(url: str) -> None:
        url = _html.unescape(url or "").strip()
        if _remote(url) and url not in found:
            found.append(url)

    text = html or ""
    for tag in _TAG_RE.finditer(text):
        t = tag.group(0)
        loads_href = bool(_HREF_LOADS_RE.match(t))
        for m in _RESOURCE_ATTR_RE.finditer(t):
            name = m.group(1).lower()
            value = next((g for g in m.groups()[1:] if g is not None), "")
            if name == "href" and not loads_href:
                continue
            if name == "srcset":
                for item in value.split(","):
                    _add(item.strip().split(" ")[0])
            else:
                _add(value)
    for rx in (_CSS_URL_RE, _CSS_IMPORT_RE):
        for m in rx.finditer(text):
            _add(next((g for g in m.groups() if g is not None), ""))
    return found


RESOURCES_SHOWN = 5


def remote_resource_warning(urls: list[str]) -> str:
    """The Send card line for a draft whose HTML loads remote resources,
    or "". Each address comes from the mail, so it is an inert span: one
    line, shown as written, and no markup in it renders."""
    if not urls:
        return ""
    n = len(urls)
    shown = ", ".join(_ct.inert(u, 300) for u in urls[:RESOURCES_SHOWN])
    more = f" and {n - RESOURCES_SHOWN} more" if n > RESOURCES_SHOWN else ""
    return (f":warning: *Remote content:* this draft loads {n} resource"
            f"{'s' if n != 1 else ''} from outside the message when it is opened, "
            f"which the text below does not show: {shown}{more}.")


_WD3 = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MO3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_WD_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
# "Thursday Aug 21", "Fri, August 21", "Wed Sept 3." — a weekday word
# followed by a month and day. First three letters key both maps, and
# every weekday and month is unique in its first three, so the loose
# tail (day/-day/abbrev with optional period) all collapse cleanly.
_WEEKDAY_DATE_RE = None


def weekday_date_mismatches(text: str, today=None) -> list:
    """Weekday+date pairs whose stated weekday contradicts the calendar.

    A drafted "Thursday Aug 21" when the 21st is a Friday is exactly the
    kind of hand-computed weekday an LLM gets wrong, and it shipped to a
    real recipient. This resolves the date to the year that puts it
    nearest the send date (scheduling is near-future), so it needs no
    year in the text, and reports the mismatch for preflight to block.
    Pure: no I/O, unit-testable.
    """
    import datetime as _dt
    global _WEEKDAY_DATE_RE
    if _WEEKDAY_DATE_RE is None:
        _WEEKDAY_DATE_RE = re.compile(
            r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+"
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
            r"(\d{1,2})\b", re.IGNORECASE)
    today = today or _dt.date.today()
    out = []
    for wd, mon, day in _WEEKDAY_DATE_RE.findall(text):
        stated = _WD3.get(wd.lower()[:3])
        month = _MO3.get(mon.lower()[:3])
        day = int(day)
        if stated is None or month is None or not (1 <= day <= 31):
            continue
        # Resolve to the nearest occurrence across last/this/next year.
        best = None
        for yr in (today.year - 1, today.year, today.year + 1):
            try:
                cand = _dt.date(yr, month, day)
            except ValueError:
                continue
            if best is None or abs((cand - today).days) < abs((best - today).days):
                best = cand
        if best is None or abs((best - today).days) > 400:
            continue  # far-off or impossible date: not a real appointment
        if best.weekday() != stated:
            out.append({
                "stated": _WD_FULL[stated],
                "actual": _WD_FULL[best.weekday()],
                "date": best.strftime("%b %-d %Y"),
            })
    return out


def _video_platform() -> str:
    """The institution's video platform from the profile, '' when unknown."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        return institution.video()["platform"]
    except Exception:
        return ""


def new_text_errors(new_body: str, is_html: bool) -> list[str]:
    """The hard blocks on a draft's NEW text (above any quoted thread):
    a Teams link when the institution uses another platform, an em-dash,
    en-dash or spaced double hyphen, and a weekday that is not the date's.
    `new_body` is HTML when `is_html` (the Teams check reads its links)."""
    errors = []
    # With no platform configured the check is skipped: nothing says
    # Teams is wrong for this install.
    platform = _video_platform()
    if "teams.microsoft.com" in new_body and platform and "teams" not in platform.lower():
        errors.append(
            "Body contains a Microsoft Teams link. The user's institution "
            f"uses {platform}, not Teams. Use "
            "--online with create-event to add the user's own link."
        )
    text = re.sub(r"<[^>]+>", " ", new_body) if is_html else new_body
    # User preference: never use em-dashes in drafted content. Covers U+2014
    # and U+2013 (en-dash).
    if "\u2014" in text or "\u2013" in text:
        errors.append(
            "Draft contains an em-dash (\u2014) or en-dash (\u2013). User preference: "
            "never use em-dashes in drafted content. Replace with a comma, "
            "period, or rephrase."
        )
    # The ASCII double-hyphen used as a dash. A range like "9-5" or a flag
    # "--online" is a single hyphen or unspaced, so key on " -- ".
    if " -- " in text:
        errors.append(
            "Draft contains ' -- ' (ASCII double-hyphen used as a dash). "
            "User preference: never use dashes in drafted content. Rephrase "
            "with a comma, colon, semicolon, or parentheses."
        )
    # Hand-computed weekdays are an LLM failure mode and one shipped to a
    # recipient, so this blocks the send.
    for mm in weekday_date_mismatches(text):
        errors.append(
            f"Draft says '{mm['stated']} {mm['date']}' but {mm['date']} "
            f"is a {mm['actual']}. Fix the weekday or the date before sending."
        )
    return errors
