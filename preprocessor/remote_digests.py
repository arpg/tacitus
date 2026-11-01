#!/usr/bin/env python3
"""Digests on the remote model: the list-digest and admissions-digest lanes
run in an owner process, with the remote LLM making the judgment that
skills/list-digest.md and skills/admissions-digest.md ask of Claude, so the
mail these lanes read never reaches Anthropic (docs/remote-llm.md,
"Digests on the remote model").

Verbs (owner process, from cron):
    remote_digests.py list-digest [--dry-run]
    remote_digests.py admissions-digest [--dry-run]

Switch: `"digests"` in remote-llm.json, `"remote"` or `"off"` (the default,
and always `off` outside an owner process). Off, a verb execs
run-agent.sh <agent> from the same tree, which is the Claude lane, so the
crontab points here and the switch alone decides. `--dry-run` always takes
the remote path: it collects, asks the model and builds the post, then
prints counts only. It posts nothing, archives nothing and writes no
report; the post it built is in the run record in the private store.

list-digest. `list_digest.collect()` reads the inbox as the preprocess step
does. Per email, the remote model judges the items against the user's
profile and the rules the entry carries (preferences.py scopes
`newsletters`, `newsletters:<list>` and the list's own `scopes`):
  * a `filter` list: one call picks the few relevant items;
  * a `prioritize` list: one call ranks three to six items, with at most
    two events "on the radar". With `follow_links`, a first call sets
    aside by name what is out of field or under an avoid rule, the page
    behind each remaining item is read here (at most MAX_FETCH per email,
    nearest deadline first, PAGE_CHARS each), and the ranking call reads
    those pages.
Code writes the post in the skill's shape. Links are chosen by index and
copied from the item, never typed by the model; the tally counts every
item, job posts dropped in code included. The post goes where the Claude
lanes' chat reader does not read (chat_notify.post_digest): the private
`mail-digest` stream on Zulip, the digest channel on the web chat, the
`mail-digest` webhook on Slack, or on Slack without that webhook
`ea-assistant`, as before. Only then does list_digest.archive() move the
mail to Archive (`posted`, or `silent` with no post when nothing was
worth posting). An endpoint failure or a failed post archives nothing:
the mail stays for the next run.

admissions-digest. `admissions_filter.collect()` reads the last week of
the admissions-inquiries folder. The model judges each sender under the
skill's criteria and the user's rules for the scopes the skill declares
(`admissions`): shortlist or not, one clause on fit, and the pattern of
the rest. Code writes the post. Nothing is moved or archived.

Link following runs with fetch_url's address policy (http and https only,
globally routable addresses, every redirect hop validated, DNS pinned to
the validated addresses) and plain requests: no JavaScript rendering and
no PDFs. fetch_url's CLI log in the resident's space is not written from
here; the host and outcome of each fetch are in the run record.

Where this differs from the Claude lanes:
  * an email the parser could not split (`unparsed`) is one item, the
    whole message, so at most one section of the post covers it;
  * a page that needs JavaScript reads as empty, and the item is judged
    on its newsletter line;
  * with chat on, the post is not copied to the resident's reports
    directory (that file is the delivery only for a profile without
    chat); the run record keeps it.

Store: <private_dir>/remote-llm/digests.jsonl, one record per run: counts,
the disposition, fetch outcomes (host and status) and the post text.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RUN_AGENT = REPO / "run-agent.sh"
ADMISSIONS_SKILL = REPO / "skills" / "admissions-digest.md"
EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
REPORT_DIR = EA_DIR / "state" / "reports"

AGENTS = ("list-digest", "admissions-digest")
RUNS = "digests.jsonl"
# The post's channel on Slack when no `mail-digest` webhook is mapped.
CHANNEL = "ea-assistant"

MAX_FETCH = 20
PAGE_CHARS = 2500
FETCH_TIMEOUT_S = 15
FETCH_MAX_BYTES = 2 * 1024 * 1024
SCREEN_BODY_CHARS = 300
ITEM_BODY_CHARS = 2200
RAW_CHARS = 14000
RANK_PAGE_BUDGET = 60000
MAX_PICKS = {"filter": 8, "prioritize": 6}
MAX_RADAR = 2
MAX_LINKS = 3
MAX_BULLETS = 4
ADMISSIONS_BATCH_CHARS = 40000
MAX_SHORTLIST = 5
RETRY_SLEEP_S = 10


# ── text ──────────────────────────────────────────────────────────────

_URL = re.compile(r"https?://\S+")
_CHAT_SPECIAL = re.compile(r"<[!@#][^>]*>")
_EMOJI = re.compile(r":[a-z0-9_+-]+:")


def _strip_dashes(text: str) -> str:
    try:
        from style_guard import strip_em_dashes
    except ModuleNotFoundError:
        from preprocessor.style_guard import strip_em_dashes
    text, _ = strip_em_dashes(text)
    return text.replace("—", ", ")


def clean(text, cap: int = 300) -> str:
    """One line of model or mail text made safe for the post: whitespace
    collapsed, em-dashes replaced, no URLs (links are rendered from the
    item), no markup that bolds or mentions, at most `cap` characters."""
    s = " ".join(str(text or "").split())
    s = _strip_dashes(s)
    s = _URL.sub("", s)
    s = _CHAT_SPECIAL.sub("", s)
    s = s.replace("*", "").replace("`", "")
    s = " ".join(s.split()).strip(" ,;")
    if len(s) > cap:
        s = s[:cap - 1].rstrip(" ,;") + "…"
    return s


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?…" else text + "."


def _local(iso) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _day(iso) -> str:
    dt = _local(iso)
    return dt.strftime("%a %d %b") if dt else ""


def _today() -> str:
    return datetime.now().astimezone().strftime("%A %Y-%m-%d")


def profile_text(prof: dict, who: str = "The user") -> str:
    prof = prof or {}
    bits = [str(prof[k]) for k in ("name", "role_description", "department", "university")
            if prof.get(k)]
    line = f"{who}: " + ", ".join(bits) + "." if bits else f"{who} is a university professor."
    if prof.get("bio_blurb"):
        line += "\nResearch: " + str(prof["bio_blurb"])[:1500]
    return line


def rules_block(selection: dict | None) -> str:
    """The user's rules for a prompt, in the shape preferences.render
    gives every lane."""
    import preferences
    text = preferences.render_selection(selection or {})
    return text or "(The user has no rules on file for this scope.)\n"


# ── link following ────────────────────────────────────────────────────

def fetch_page(url: str, *, max_chars: int = PAGE_CHARS, timeout: int = FETCH_TIMEOUT_S,
               get=None) -> dict:
    """The readable text of one page: {"url", "title", "text"}, or
    {"url", "error"}. Every hop passes fetch_url.validate_url and is made
    with DNS pinned to the addresses it validated; redirects are walked
    by hand. `get` replaces requests.get in the tests."""
    import requests
    import fetch_url
    get = get or requests.get
    if not url:
        return {"url": url, "error": "no link"}
    if urlparse(url).path.lower().endswith(".pdf"):
        return {"url": url, "error": "a PDF"}
    current = url
    try:
        for _ in range(fetch_url.MAX_REDIRECTS + 1):
            info = fetch_url.validate_url(current)
            with fetch_url.pinned_dns(info["host"], info["addrinfo"]):
                resp = get(current, headers={"User-Agent": fetch_url.USER_AGENT},
                           timeout=timeout, stream=True, allow_redirects=False)
                try:
                    status = resp.status_code
                    if status in fetch_url.REDIRECT_STATUSES:
                        loc = resp.headers.get("Location", "")
                        if not loc:
                            return {"url": url, "error": f"HTTP {status} with no Location"}
                        current = urljoin(current, loc)
                        continue
                    if status >= 400:
                        return {"url": url, "error": f"HTTP {status}"}
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "pdf" in ctype:
                        return {"url": url, "error": "a PDF"}
                    if ctype and "html" not in ctype and "text" not in ctype:
                        return {"url": url, "error": f"not a web page ({ctype[:40]})"}
                    body = b""
                    for chunk in resp.iter_content(chunk_size=65536):
                        body += chunk
                        if len(body) >= FETCH_MAX_BYTES:
                            break
                    encoding = resp.encoding or "utf-8"
                finally:
                    resp.close()
            title, _meta, text, _links, _full, _cut = fetch_url.extract(
                body.decode(encoding, errors="replace"), max_chars)
            text = (text or "").strip()
            if len(text) < 50:
                return {"url": current, "title": title or "",
                        "error": "little text (the page may need JavaScript)"}
            return {"url": current, "title": title or "", "text": text}
        return {"url": url, "error": "too many redirects"}
    except fetch_url.BlockedURL as e:
        return {"url": url, "error": f"blocked: {e}"[:200]}
    except Exception as e:
        return {"url": url, "error": str(e)[:200]}


_MD = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")


def deadline_key(deadline: str, today: date | None = None) -> date:
    """When an item is due, for ordering the fetches: the first m/d(/y) in
    the printed deadline, in the coming year when no year is printed. A
    past date, `rolling`, `anytime` or nothing sorts last."""
    today = today or date.today()
    last = date.max
    m = _MD.search(deadline or "")
    if not m:
        return last
    month, day = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        when = date(year, month, day)
    except ValueError:
        return last
    if not m.group(3) and when < today - timedelta(days=180):
        when = date(year + 1, month, day)
    return when if when >= today else last


# ── list digests: prompts ─────────────────────────────────────────────

_LIST_BASE = """You triage one mailing-list email for a professor's assistant: you pick out what the user would want to see. Answer with JSON only, in the given schema.

{profile}
Today is {today}.

The user's rules below are the judgment, and they outrank everything else in this prompt. Standing instructions come first. An `avoid` match is final. The facts decide fit and which awards are open to the user. The calibration rows set the bar: a run that posted too much is a warning to post less; a correction names a category to skip.

{rules}
Write plain, short sentences in the active voice, one fact per sentence. Never use an em-dash. No markdown and no URLs in any text field: links are chosen by their index."""

_FILTER_TASK = """Mode: filter. Post an item only if it matches a `prefer` rule or the user's work as the facts describe it, and no `avoid` rule. When unsure, drop it: a missed borderline item costs nothing, and a noisy post teaches the user to ignore the channel. Job posts were already removed in code.

`picks`, one per item you post:
- n: the item number.
- title: a short plain title, at most 12 words.
- detail: for a paper, the venue; for a digest post, the poster's name and affiliation when the item states them; else "".
- bullets: two to four facts: what it is, its size, sensors, task or scale. Facts only. At most one bullet on fit.
- links: the item's links to show, by index, at most three, the paper, code, data or submission page before a homepage; label each "Paper", "Code", "Data", "Page" or "".
`skipped`: every other item: its n and a category, a short plural phrase that names why it was dropped the way the avoid rules name it (for example "workshop or CFP notices", "special issues", "press pieces", "out-of-field posts").
list_title: the list's name as the email names it, at most five words."""

_SCREEN_TASK = """Mode: prioritize, pass 1 of 2. This is an opportunity newsletter. The user cannot follow every item and wants a ranked shortlist. Read the list's conventions first.

Set aside each item whose title alone places it outside the user's field (the facts) or inside an `avoid` rule: list it in `skipped` with its n and a category, a short plural phrase in the words of the avoid rules (for example "out-of-field programs", "standing BAAs", "news items", "events"). Put every other item's n in `check`: its page is read before pass 2 ranks it. When unsure, check it.
list_title: the newsletter's name as the email names it, at most five words."""

_RANK_TASK = """Mode: prioritize{pass_label}. This is an opportunity newsletter. The user cannot follow every item and wants a ranked shortlist. Read the list's conventions first.
{pages}
Eligibility: apply the facts about the user's career stage and status. Skip an award the user cannot hold (category "not eligible"). When eligibility turns on a fact the rules do not state, keep the item if the topic fits and state the condition in one bullet.

Rank: first fit with the `prefer` rules and the facts, then how near the deadline is, then award size and prestige. Something the user could submit to this cycle ranks above a prestige prize with long odds. An event counts only when it changes a funding outcome for the user (a sponsor briefing, an information session for a program they would apply to, a partnership workshop): at most two, in `radar`, never in `picks`.

`picks`: three to six items, best first. Fewer is fine; none is fine. Each:
- n: the item number.
- title: the program's name, short.
- detail: the deadline as printed and whether it is internal, a Limited Submission or rolling (for example "due 10/12; internal").
- bullets: two to four facts: what it funds, award amount and duration, an eligibility condition, the contact a Note names, the next step.
- links: by index, at most three; link 0 is the program's page.
`radar`: at most two events: n and a one-line note with the event's name and date.
`skipped`: every other item: its n and a category, a short plural phrase in the words of the avoid rules (for example "out-of-field programs", "not eligible", "standing BAAs", "events", "news items").
list_title: the newsletter's name as the email names it, at most five words."""

_PAGES_ON = """Under each item is the text of its page, or a note that the page could not be read; then judge the item on its title and deadline and say in one bullet that the page could not be read. Read each page for what it funds, eligibility (career stage, years since PhD, citizenship, discipline, institution limits), award size and duration, the real deadline (internal or sponsor), whether it is a limited submission, and what to do next.
"""
_PAGES_OFF = "Judge each item on its newsletter line and its note.\n"

_LINK = {"type": "object", "additionalProperties": False,
         "properties": {"index": {"type": "integer"}, "label": {"type": "string"}},
         "required": ["index", "label"]}
_PICK = {"type": "object", "additionalProperties": False,
         "properties": {"n": {"type": "integer"}, "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                        "links": {"type": "array", "items": _LINK}},
         "required": ["n", "title", "detail", "bullets", "links"]}
_SKIP = {"type": "object", "additionalProperties": False,
         "properties": {"n": {"type": "integer"}, "category": {"type": "string"}},
         "required": ["n", "category"]}
_RADAR = {"type": "object", "additionalProperties": False,
          "properties": {"n": {"type": "integer"}, "note": {"type": "string"}},
          "required": ["n", "note"]}


def _schema(**props) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": props, "required": list(props)}


FILTER_SCHEMA = _schema(list_title={"type": "string"},
                        picks={"type": "array", "items": _PICK},
                        skipped={"type": "array", "items": _SKIP})
SCREEN_SCHEMA = _schema(list_title={"type": "string"},
                        check={"type": "array", "items": {"type": "integer"}},
                        skipped={"type": "array", "items": _SKIP})
RANK_SCHEMA = _schema(list_title={"type": "string"},
                      picks={"type": "array", "items": _PICK},
                      radar={"type": "array", "items": _RADAR},
                      skipped={"type": "array", "items": _SKIP})


def _entry_head(e: dict) -> str:
    lines = [f"List: {e.get('list') or ''}",
             f"Email subject: {e.get('subject') or ''}",
             f"From: {e.get('from_name') or ''} <{e.get('from') or ''}>",
             f"Received: {e.get('received') or ''}",
             f"Items in the email: {e.get('items_total') or 0} "
             f"({len(e.get('dropped') or [])} job posts already removed in code)"]
    if e.get("notes"):
        lines.append(f"The list's conventions: {e['notes']}")
    if e.get("parse_warnings"):
        lines.append("Parser warnings: " + "; ".join(str(w) for w in e["parse_warnings"]))
    return "\n".join(lines)


def _item_block(it: dict, body_chars: int, page: dict | None = None,
                page_chars: int = PAGE_CHARS) -> str:
    lines = [f"[item {it.get('n')}] {it.get('title') or it.get('subject') or ''}"]
    if it.get("tags"):
        lines.append("tags: " + ", ".join(it["tags"]))
    if it.get("from_name"):
        lines.append(f"posted by: {it['from_name']}")
    for key in ("section", "deadline", "stage"):
        if it.get(key):
            lines.append(f"{key}: {it[key]}")
    if it.get("is_new"):
        lines.append("flagged NEW in the newsletter")
    for i, u in enumerate(it.get("links") or []):
        lines.append(f"link {i}: {u}")
    body = (it.get("body") or "")[:body_chars]
    if body:
        lines.append("text:\n" + body)
    if page is not None:
        if page.get("text"):
            title = f" ({page['title']})" if page.get("title") else ""
            lines.append(f"page{title}:\n" + page["text"][:page_chars])
        else:
            lines.append(f"page: could not be read ({page.get('error') or 'no text'})")
    return "\n".join(lines)


def _messages(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ── list digests: judgment ────────────────────────────────────────────

def _skips(raw, items: dict) -> dict:
    out = {}
    for s in raw or []:
        n = s.get("n") if isinstance(s, dict) else None
        if n in items and n not in out:
            out[n] = clean(s.get("category"), 60).rstrip(".")
    return out


def _picks(raw, items: dict, allowed, cap: int, prioritize: bool) -> list[dict]:
    out, seen = [], set()
    for p in raw or []:
        n = p.get("n") if isinstance(p, dict) else None
        if n not in allowed or n in seen:
            continue
        seen.add(n)
        it = items[n]
        links = [u for u in it.get("links") or [] if isinstance(u, str) and u]
        chosen: list[tuple[str, str]] = []
        for ln in p.get("links") or []:
            idx = ln.get("index") if isinstance(ln, dict) else None
            if isinstance(idx, int) and 0 <= idx < len(links) \
                    and links[idx] not in [u for _, u in chosen]:
                label = re.sub(r"[^A-Za-z0-9 ]", "", clean(ln.get("label"), 20)).strip()
                chosen.append((label, links[idx]))
        if not chosen and links:
            chosen = [("", links[0])]
        # A bullet that carries a URL is dropped whole: links come only
        # from the item, and a sentence with its URL cut out reads broken.
        bullets = [b for b in (clean(x, 300) for x in p.get("bullets") or []
                               if not _URL.search(str(x or ""))) if b]
        detail = clean(p.get("detail"), 160)
        if not detail and prioritize and it.get("deadline"):
            detail = f"due {clean(it['deadline'], 40)}"
        out.append({"n": n, "title": clean(p.get("title"), 140) or clean(it.get("title"), 140),
                    "detail": detail, "bullets": bullets[:MAX_BULLETS],
                    "links": chosen[:MAX_LINKS]})
        if len(out) >= cap:
            break
    return out


def judge_entry(entry: dict, *, profile: str, chat=None, fetch=None, cfg=None,
                today: date | None = None) -> dict:
    """The remote model's judgment of one email: {list_title, picks,
    radar, categories (n -> skip category), not_checked, fetches, calls}.
    Raises remote_llm.RemoteLLMError."""
    chat = chat or remote_llm.chat_json
    fetch = fetch or fetch_page
    today = today or date.today()
    items = {it["n"]: it for it in entry.get("items") or [] if isinstance(it.get("n"), int)}
    system = _LIST_BASE.format(profile=profile, today=_today(), rules=rules_block(entry.get("rules")))
    head = _entry_head(entry)
    raw = ""
    if entry.get("kind") == "unparsed" and entry.get("raw_text"):
        raw = ("\n\nThe parser could not split this email. Its whole text, which item 1 "
               "stands for:\n" + str(entry["raw_text"])[:RAW_CHARS])
    out = {"list_title": "", "picks": [], "radar": [], "categories": {},
           "not_checked": [], "fetches": [], "calls": 0}
    if not items:
        return out

    if (entry.get("mode") or "filter") != "prioritize":
        user = head + "\n\n" + "\n\n".join(_item_block(it, ITEM_BODY_CHARS) for it in items.values()) + raw
        obj, _ = chat(_messages(system + "\n\n" + _FILTER_TASK, user), purpose="digest-list",
                      schema=FILTER_SCHEMA, schema_name="list_digest_filter",
                      max_tokens=2500, cfg=cfg)
        out["calls"] = 1
        out["list_title"] = clean(obj.get("list_title"), 60)
        out["picks"] = _picks(obj.get("picks"), items, set(items), MAX_PICKS["filter"], False)
        out["categories"] = _skips(obj.get("skipped"), items)
        return out

    candidates = list(items)
    pages: dict[int, dict] = {}
    follow = bool(entry.get("follow_links"))
    if follow:
        user = head + "\n\n" + "\n\n".join(_item_block(it, SCREEN_BODY_CHARS) for it in items.values()) + raw
        obj, _ = chat(_messages(system + "\n\n" + _SCREEN_TASK, user), purpose="digest-list",
                      schema=SCREEN_SCHEMA, schema_name="list_digest_screen",
                      max_tokens=2000, cfg=cfg)
        out["calls"] += 1
        out["list_title"] = clean(obj.get("list_title"), 60)
        check = []
        for n in obj.get("check") or []:
            if n in items and n not in check:
                check.append(n)
        screened = {n: c for n, c in _skips(obj.get("skipped"), items).items() if n not in check}
        out["categories"].update(screened)
        # An item the model neither checked nor set aside is checked.
        check += [n for n in items if n not in check and n not in screened]
        check.sort(key=lambda n: (deadline_key(items[n].get("deadline", ""), today), n))
        candidates, out["not_checked"] = check[:MAX_FETCH], check[MAX_FETCH:]
        for n in candidates:
            url = (items[n].get("links") or [""])[0]
            page = fetch(url) if url else {"url": "", "error": "no link"}
            pages[n] = page
            out["fetches"].append({"n": n, "host": urlparse(url).netloc if url else "",
                                   "ok": bool(page.get("text")),
                                   "error": str(page.get("error") or "")[:120]})
    if not candidates:
        return out

    page_chars = PAGE_CHARS
    if pages:
        page_chars = max(500, min(PAGE_CHARS, RANK_PAGE_BUDGET // len(pages)))
    task = _RANK_TASK.format(pass_label=", pass 2 of 2" if follow else "",
                             pages=_PAGES_ON if follow else _PAGES_OFF)
    user = head + "\n\n" + "\n\n".join(
        _item_block(items[n], ITEM_BODY_CHARS, pages.get(n) if follow else None, page_chars)
        for n in candidates) + raw
    obj, _ = chat(_messages(system + "\n\n" + task, user), purpose="digest-list",
                  schema=RANK_SCHEMA, schema_name="list_digest_rank",
                  max_tokens=3000, cfg=cfg)
    out["calls"] += 1
    out["list_title"] = clean(obj.get("list_title"), 60) or out["list_title"]
    allowed = set(candidates)
    out["picks"] = _picks(obj.get("picks"), items, allowed, MAX_PICKS["prioritize"], True)
    picked = {p["n"] for p in out["picks"]}
    radar, seen = [], set()
    for r in obj.get("radar") or []:
        n = r.get("n") if isinstance(r, dict) else None
        if n in allowed and n not in picked and n not in seen and clean(r.get("note"), 160):
            seen.add(n)
            radar.append({"n": n, "note": clean(r.get("note"), 160).rstrip(".")})
    out["radar"] = radar[:MAX_RADAR]
    out["categories"].update({n: c for n, c in _skips(obj.get("skipped"), items).items()
                              if n in allowed and n not in picked})
    return out


# ── list digests: the post ────────────────────────────────────────────

_CODE_LABELS = {"job posts": "job post", "events": "event", "other items": "other item",
                "not checked": "not checked"}


def _count_phrase(prioritize: bool, k: int, total: int) -> str:
    if prioritize:
        if k == 0:
            return f"none of {total} items is worth your time."
        if k == 1:
            return f"1 of {total} items is worth your time."
        return f"{k} of {total} items are worth your time, ranked."
    if k == 0:
        return f"none of {total} items is relevant."
    if k == 1:
        return f"1 of {total} items is relevant."
    return f"{k} of {total} items are relevant."


def render_entry(entry: dict, j: dict) -> str:
    """One email's part of the post: header, one section per pick, the
    radar and not-checked lines, the tally."""
    prioritize = (entry.get("mode") or "filter") == "prioritize"
    emoji = entry.get("emoji") or ""
    if not _EMOJI.fullmatch(emoji):
        emoji = ":newspaper:"
    name = j.get("list_title") or clean(entry.get("list"), 60) or "Mailing list"
    if entry.get("kind") == "digest" and "digest" not in name.lower():
        name += " digest"
    items = {it["n"]: it for it in entry.get("items") or [] if isinstance(it.get("n"), int)}
    total = int(entry.get("items_total") or 0) or len(items) + len(entry.get("dropped") or [])
    picks = j.get("picks") or []
    day = _day(entry.get("received"))
    blocks = [f"{emoji} *{name}*{f' ({day})' if day else ''}: "
              f"{_count_phrase(prioritize, len(picks), total)}"]
    for i, p in enumerate(picks, 1):
        title = f"{i}. {p['title']}" if prioritize else p["title"]
        lines = [f"*{title}*" + (f" ({p['detail']})" if p.get("detail") else "")]
        lines += [f"• {_sentence(b)}" for b in p.get("bullets") or []]
        if p.get("links"):
            lines.append(" | ".join(f"{lab}: {u}" if lab else u for lab, u in p["links"]))
        blocks.append("\n".join(lines))
    extra = []
    radar = j.get("radar") or []
    if radar:
        extra.append("Also on the radar: " + "; ".join(r["note"] for r in radar) + ".")
    not_checked = [n for n in j.get("not_checked") or [] if n in items]
    if not_checked:
        extra.append("Not checked: " + "; ".join(clean(items[n].get("title"), 100)
                                                 for n in not_checked) + ".")
    if extra:
        blocks.append("\n".join(extra))

    labels: dict[str, str] = {}
    counts: Counter = Counter()

    def count(label: str, k: int = 1) -> None:
        key = label.lower()
        labels.setdefault(key, label)
        counts[key] += k

    jobs = len(entry.get("dropped") or [])
    if jobs:
        count("job posts", jobs)
    picked = {p["n"] for p in picks}
    radar_ns = {r["n"] for r in radar}
    cats = j.get("categories") or {}
    for n in items:
        if n in picked:
            continue
        count(cats.get(n) or ("events" if n in radar_ns else
                              "not checked" if n in not_checked else "other items"))
    skipped = sum(counts.values())
    if skipped:
        parts = []
        for key, k in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            label = labels[key]
            if k == 1 and label in _CODE_LABELS:
                label = _CODE_LABELS[label]
            parts.append(f"{k} {label}")
        blocks.append(f"{'Skipped' if prioritize else 'Dropped'} {skipped}: " + ", ".join(parts) + ".")
    return "\n\n".join(blocks)


def render_list_post(digests: list[dict], judged: list[dict]) -> tuple[str, str]:
    """(post text, disposition). `silent` with no text when no email holds
    anything worth posting."""
    if not any(j.get("picks") or j.get("radar") for j in judged):
        return "", "silent"
    return "\n\n".join(render_entry(e, j) for e, j in zip(digests, judged)), "posted"


# ── admissions digest ─────────────────────────────────────────────────

PATTERNS = {
    "templated": "templated asks",
    "generic-gap": "generic research-gap claims",
    "topic-list": "topic lists with no link to their own work",
    "follow-up-only": "follow-ups with no new content",
    "several-labs": "mail sent to several labs",
    "real-fit": "asks with some real fit",
    "other": "other asks",
}

ADMISSIONS_SCHEMA = _schema(senders={"type": "array", "items": {
    "type": "object", "additionalProperties": False,
    "properties": {"i": {"type": "integer"}, "shortlist": {"type": "boolean"},
                   "strength": {"type": "integer"}, "school": {"type": "string"},
                   "subject": {"type": "string"}, "fit": {"type": "string"},
                   "pattern": {"type": "string", "enum": list(PATTERNS)}},
    "required": ["i", "shortlist", "strength", "school", "subject", "fit", "pattern"]}})

_ADMISSIONS_PROMPT = """Prospective PhD and MS students write to a professor in volume. Many of these emails are machine-written: a name-dropped paper, a claimed "gap" in the professor's research, then the ask. You read a week of them and judge each sender for the professor's weekly digest. Answer with JSON only, in the given schema. You draft nothing and suggest no reply.

{profile}

Shortlist a sender only when the mail shows real fit. Real fit means at least one of:
- The work named is the professor's actual work, with a detail that only a reader of the paper would know, and the student's own prior work connects to it concretely: a system they built, a dataset, a paper.
- A referral: a named faculty member or collaborator sent them.
- A strong, checkable record: a first-author paper at a robotics or vision venue, a top program, a named lab they worked in.
- They are applying to {institution_program} this cycle, with a specific plan, not "PhD programs" in general.
Treat as templated, and do not shortlist:
- Praise that could fit any robotics lab, or a "gap" the student says they found that is generic ("robustness in adverse conditions").
- A long list of the professor's topics with no link to the student's work.
- Follow-ups whose only content is the earlier ask.
- One email that names several labs or programs, or says "Dear Professor".
When unsure, leave the sender off the shortlist. A missed borderline student can still apply through the program; a long shortlist teaches the professor to skip the post. Three or fewer names in a week is normal. Zero is fine.

{rules}
For each sender, by its number `i`:
- shortlist: true or false.
- strength: 1 (templated) to 5 (clear fit).
- school: the student's school as the mail states it, else "".
- subject: the subject of the first message, shortened to at most eight words.
- fit: for a shortlisted sender, one clause on the fit, at most 20 words; else "".
- pattern: the one that fits best: {patterns}.
Write plain, short words. Never use an em-dash. No markdown."""


def _sender_block(i: int, g: dict) -> str:
    lines = [f"[sender {i}] {g.get('name') or ''} <{g.get('from') or ''}> ({g.get('domain') or ''}); "
             f"{g.get('count') or len(g.get('messages') or [])} message(s), "
             f"{g.get('follow_ups') or 0} follow-up(s)"]
    for k, m in enumerate(g.get("messages") or [], 1):
        lines.append(f"message {k}: received {m.get('received') or ''}; subject: {m.get('subject') or ''}; "
                     f"attachments: {'yes' if m.get('has_attachments') else 'no'}; "
                     f"filter signals: {', '.join(m.get('kinds') or []) or 'none'}")
        lines.append(m.get("text") or "")
    return "\n".join(lines)


def _batches(blocks: list[tuple[int, str]], budget: int) -> list[list[tuple[int, str]]]:
    out, cur, size = [], [], 0
    for i, text in blocks:
        if cur and size + len(text) > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append((i, text))
        size += len(text)
    if cur:
        out.append(cur)
    return out


def admissions_rules(prefs_data: dict | None = None) -> str:
    """The user's rules for the scopes skills/admissions-digest.md
    declares, as every lane renders them."""
    import preferences
    scopes = preferences.scopes_from_skill(ADMISSIONS_SKILL)
    if scopes is None:
        scopes = ["admissions"]
    return preferences.render(scopes, prefs_data) or "(The user has no rules on file for this scope.)\n"


def _institution_program() -> str:
    """'the <institution> program', from the profile's institution block."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        name = institution.name()
    except Exception:
        name = ""
    return f"the {name} program" if name else "the professor's own program"


def judge_senders(senders: list[dict], *, profile: str, rules: str, chat=None,
                  cfg=None) -> tuple[dict, int]:
    """({sender index: verdict}, calls). Raises remote_llm.RemoteLLMError."""
    chat = chat or remote_llm.chat_json
    system = _ADMISSIONS_PROMPT.format(profile=profile, rules=rules,
                                       institution_program=_institution_program(),
                                       patterns=", ".join(f'"{p}"' for p in PATTERNS))
    verdicts: dict[int, dict] = {}
    calls = 0
    blocks = [(i, _sender_block(i, g)) for i, g in enumerate(senders, 1)]
    for batch in _batches(blocks, ADMISSIONS_BATCH_CHARS):
        user = (f"{len(batch)} sender(s) this week.\n\n" + "\n\n".join(t for _, t in batch))
        obj, _ = chat(_messages(system, user), purpose="digest-admissions",
                      schema=ADMISSIONS_SCHEMA, schema_name="admissions_digest",
                      max_tokens=min(4000, max(800, 200 + 150 * len(batch))), cfg=cfg)
        calls += 1
        wanted = {i for i, _ in batch}
        for v in obj.get("senders") or []:
            i = v.get("i") if isinstance(v, dict) else None
            if i not in wanted or i in verdicts:
                continue
            try:
                strength = min(5, max(1, int(v.get("strength") or 1)))
            except (TypeError, ValueError):
                strength = 1
            verdicts[i] = {"shortlist": v.get("shortlist") is True, "strength": strength,
                           "school": clean(v.get("school"), 80),
                           "subject": clean(v.get("subject"), 80),
                           "fit": clean(v.get("fit"), 200),
                           "pattern": v.get("pattern") if v.get("pattern") in PATTERNS else "other"}
    return verdicts, calls


def render_admissions_post(data: dict, senders: list[dict], verdicts: dict,
                           now: datetime | None = None) -> tuple[str, int]:
    """(post text, shortlisted count) in the shape of the skill's post."""
    now = now or datetime.now().astimezone()
    start = (now - timedelta(days=int(data.get("window_days") or 7))).date()
    monday = start - timedelta(days=start.weekday())
    n_msgs = int(data.get("messages") or sum(len(g.get("messages") or []) for g in senders))
    n_senders = int(data.get("senders") or len(senders))
    lines = [f":mortar_board: *Admissions inquiries: week of {monday.strftime('%a %d %b')}*",
             f"{n_msgs} email{'' if n_msgs == 1 else 's'} from {n_senders} "
             f"sender{'' if n_senders == 1 else 's'} went to the admissions-inquiries folder."]

    def name_of(g: dict) -> str:
        return clean(g.get("name"), 80) or clean(g.get("from"), 80) or "unknown sender"

    ranked = sorted((i for i, v in verdicts.items() if v["shortlist"]),
                    key=lambda i: (-verdicts[i]["strength"], i))
    shortlist = sorted(ranked[:MAX_SHORTLIST])
    lines.append("")
    if shortlist:
        lines.append(f"*Worth a look* ({len(shortlist)})")
        for i in shortlist:
            g, v = senders[i - 1], verdicts[i]
            first = (g.get("messages") or [{}])[0]
            where = v["school"] or clean(g.get("domain"), 60)
            subject = v["subject"] or clean(first.get("subject"), 80)
            fit = v["fit"][:1].upper() + v["fit"][1:]
            said = " ".join(_sentence(x) for x in (subject, fit) if x)
            lines.append(f"- *{name_of(g)}*" + (f" ({where})" if where else "")
                         + (f": {said}" if said else ""))
    else:
        lines.append("*Worth a look*: none this week.")

    rest = [i for i in range(1, len(senders) + 1) if i not in shortlist]
    if rest:
        patterns = Counter(verdicts.get(i, {}).get("pattern", "other") for i in rest)
        parts = [f"{k} {PATTERNS[p]}" for p, k in sorted(patterns.items(), key=lambda kv: (-kv[1], kv[0]))]
        follow = sum(1 for i in rest if (senders[i - 1].get("follow_ups") or 0) > 0)
        sentence = ", ".join(parts)
        if follow:
            sentence += f"; {follow} sent follow-ups"
        lines.append("")
        lines.append(f"*The rest* ({len(rest)}): " + ", ".join(name_of(senders[i - 1]) for i in rest)
                     + ". " + _sentence(sentence))
    errors = data.get("errors") or []
    if errors:
        lines.append("")
        lines.append(f":warning: {len(errors)} message(s) could not be read in full.")
    return "\n".join(lines), len(shortlist)


# ── delivery and records ──────────────────────────────────────────────

def _has_chat() -> bool:
    try:
        import ea_config
        return bool(ea_config.profile_features()["slack"])
    except Exception:
        return True


DIGEST_TOPIC = "List and admissions digests"


def _post(key: str, text: str) -> bool:
    """The digest place (chat_notify.post_digest), under the lane's key:
    the mail-digest stream on Zulip, the digest channel on the web chat,
    the `mail-digest` webhook on Slack. The briefing's chat reader reads
    none of them, so the post's text never reaches a Claude lane. On Slack
    without that webhook, the `ea-assistant` channel."""
    import chat_notify
    got = chat_notify.post_digest(key, text, topic=DIGEST_TOPIC)
    if got is None and chat_notify.frontend_name() == "slack":
        import slack_notifier
        return bool(slack_notifier.post_to_slack(CHANNEL, text))
    if got is None:
        print(f"remote_digests {key}: no digest channel on the {chat_notify.frontend_name()} "
              "chat; nothing posted", file=sys.stderr)
    return bool(got)


def _write_report(agent: str, text: str) -> bool:
    """The report file run-agent.sh writes for a profile without chat,
    written with the resident's access."""
    import resident_io
    resident_io.make_dir(REPORT_DIR)
    return resident_io.replace_text(REPORT_DIR / f"{agent}-latest.md", text + "\n")


def deliver(agent: str, text: str, *, post=None, has_chat=None, write_report=None) -> bool:
    """Post to the digest place under the agent's key, or, for a profile
    without chat, write the report file that is then the delivery. True
    when delivered."""
    chat_on = _has_chat() if has_chat is None else has_chat
    try:
        if not chat_on:
            return bool((write_report or _write_report)(agent, text))
        return bool((post or _post)(agent, text))
    except Exception as e:
        print(f"remote_digests {agent}: delivery failed: {str(e)[:200]}", file=sys.stderr)
        return False


def _record(rec: dict) -> None:
    try:
        remote_llm.append(RUNS, rec)
    except OSError as e:
        print(f"remote_digests: run record not written: {e}", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report(counts: dict) -> None:
    print(json.dumps(counts, sort_keys=True))


def _failed(agent: str, err: Exception, rec: dict) -> int:
    rec.update(ok=False, error=str(err)[:300])
    _record(rec)
    print(f"remote_digests {agent}: the remote model failed ({str(err)[:160]}); "
          f"nothing posted or archived, the mail stays for the next run", file=sys.stderr)
    return 1


# ── collection ────────────────────────────────────────────────────────

def _load_env() -> None:
    """The Graph settings and secrets run-agent.sh exports before its
    preprocess step, by the owner_trust rules."""
    _owner_trust.load_owner_env()


def _retry(fn, sleep=time.sleep):
    """One retry after a network error, as the preprocess step does."""
    import requests
    try:
        return fn()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"remote_digests: collection failed ({str(e)[:120]}); retrying in "
              f"{RETRY_SLEEP_S}s", file=sys.stderr)
        sleep(RETRY_SLEEP_S)
        return fn()


def _collect_lists() -> dict:
    _load_env()
    import list_digest
    return _retry(list_digest.collect)


def _archive_lists(data: dict, disposition: str) -> dict:
    import list_digest
    return list_digest.archive(data, disposition)


def _admissions_enabled() -> bool:
    import admissions_filter
    return admissions_filter.enabled()


def _collect_admissions() -> dict:
    _load_env()
    import admissions_filter
    return _retry(admissions_filter.collect)


def _collected(agent: str, collect) -> dict | None:
    """The collected input, or None after one stderr line when the mail
    could not be read."""
    try:
        return collect()
    except Exception as e:
        print(f"remote_digests {agent}: the mail could not be read ({type(e).__name__}: "
              f"{str(e)[:160]}); nothing posted or archived", file=sys.stderr)
        return None


# ── the lanes ─────────────────────────────────────────────────────────

def run_list_digest(*, dry_run: bool = False, cfg: dict | None = None, collect=None,
                    chat=None, fetch=None, post=None, archive=None, has_chat=None,
                    write_report=None, today: date | None = None) -> int:
    """The list-digest lane on the remote model. Returns the exit code."""
    rec = {"ts": _now_iso(), "agent": "list-digest", "dry_run": dry_run}
    data = _collected("list-digest", collect or _collect_lists)
    if data is None:
        return 1
    digests = data.get("digests") or []
    if not digests:
        _report({"agent": "list-digest", "digests": 0,
                 "collect_errors": len(data.get("errors") or [])})
        return 0
    profile = profile_text(data.get("profile") or {})
    judged = []
    try:
        for e in digests:
            judged.append(judge_entry(e, profile=profile, chat=chat, fetch=fetch, cfg=cfg,
                                      today=today))
    except remote_llm.RemoteLLMError as err:
        rec["digests"] = len(digests)
        return _failed("list-digest", err, rec)
    text, disposition = render_list_post(digests, judged)
    counts = {"agent": "list-digest", "dry_run": dry_run, "digests": len(digests),
              "items": sum(int(e.get("items_total") or 0) for e in digests),
              "picked": sum(len(j["picks"]) for j in judged),
              "radar": sum(len(j["radar"]) for j in judged),
              "fetched": sum(len(j["fetches"]) for j in judged),
              "fetch_failed": sum(1 for j in judged for f in j["fetches"] if not f["ok"]),
              "calls": sum(j["calls"] for j in judged),
              "disposition": disposition, "post_chars": len(text)}
    rec.update(counts, ok=True, post=text,
               lists=[e.get("list") for e in digests],
               fetches=[{"host": f["host"], "ok": f["ok"], "error": f["error"]}
                        for j in judged for f in j["fetches"]])
    if dry_run:
        _record(rec)
        _report(counts)
        return 0
    if disposition == "posted":
        if not deliver("list-digest", text, post=post, has_chat=has_chat,
                       write_report=write_report):
            rec.update(posted=False, archived=0)
            _record(rec)
            print("remote_digests list-digest: the post failed; nothing archived, "
                  "the mail stays for the next run", file=sys.stderr)
            return 1
        rec["posted"] = True
    try:
        summary = (archive or _archive_lists)(data, disposition)
    except Exception as e:
        summary = {"archived": 0, "failed": len(digests), "error": str(e)[:200]}
    rec["archive"] = {"archived": summary.get("archived"), "failed": summary.get("failed")}
    _record(rec)
    counts["archived"] = summary.get("archived")
    _report(counts)
    if summary.get("failed"):
        try:
            (post or _post)("list-digest",
                            ":warning: list-digest: a digest was judged but could not be "
                            "archived; it is still in the inbox. Details: the "
                            "remote_digests run record and the cron log.")
        except Exception:
            pass
        return 1
    return 0


def run_admissions_digest(*, dry_run: bool = False, cfg: dict | None = None, collect=None,
                          chat=None, post=None, enabled=None, has_chat=None,
                          write_report=None, prefs_data: dict | None = None,
                          now: datetime | None = None) -> int:
    """The admissions-digest lane on the remote model. Returns the exit
    code."""
    if not (enabled or _admissions_enabled)():
        _report({"agent": "admissions-digest", "disabled": True})
        return 0
    rec = {"ts": _now_iso(), "agent": "admissions-digest", "dry_run": dry_run}
    data = _collected("admissions-digest", collect or _collect_admissions)
    if data is None:
        return 1
    senders = data.get("digests") or []
    if not senders:
        errors = data.get("errors") or []
        _report({"agent": "admissions-digest", "senders": 0, "collect_errors": len(errors)})
        if errors:
            print("remote_digests admissions-digest: the folder could not be read; "
                  "nothing posted", file=sys.stderr)
            return 1
        return 0
    try:
        verdicts, calls = judge_senders(senders, profile=profile_text(data.get("profile") or {},
                                                                      "The professor"),
                                        rules=admissions_rules(prefs_data), chat=chat, cfg=cfg)
    except remote_llm.RemoteLLMError as err:
        rec["senders"] = len(senders)
        return _failed("admissions-digest", err, rec)
    text, k = render_admissions_post(data, senders, verdicts, now)
    counts = {"agent": "admissions-digest", "dry_run": dry_run, "senders": len(senders),
              "messages": int(data.get("messages") or 0), "shortlisted": k,
              "judged": len(verdicts), "calls": calls,
              "read_errors": len(data.get("errors") or []), "post_chars": len(text)}
    rec.update(counts, ok=True, post=text)
    if dry_run:
        _record(rec)
        _report(counts)
        return 0
    ok = deliver("admissions-digest", text, post=post, has_chat=has_chat, write_report=write_report)
    rec["posted"] = ok
    _record(rec)
    _report(dict(counts, posted=ok))
    if not ok:
        print("remote_digests admissions-digest: the post failed", file=sys.stderr)
        return 1
    return 0


# ── CLI ───────────────────────────────────────────────────────────────

def exec_lane(agent: str, execv=None) -> int:
    """Hand the run to the Claude lane: exec run-agent.sh <agent> from this
    tree. Returns only when the exec fails."""
    execv = execv or os.execv
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        execv(str(RUN_AGENT), [str(RUN_AGENT), agent])
    except OSError as e:
        print(f"remote_digests: could not run {RUN_AGENT}: {e}", file=sys.stderr)
        return 127
    return 0


def main(argv=None, *, execv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in AGENTS:
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true",
                       help="build the post on the remote model and print counts; "
                            "post, archive and write nothing")
    a = ap.parse_args(argv)
    cfg = remote_llm.config()
    # The digests switch, or the routing table's `digests` entry.
    if not remote_llm.local_for("digests", cfg.get("digests") == "remote", cfg) and not a.dry_run:
        return exec_lane(a.cmd, execv)
    if not _owner_trust.is_owner_process():
        print("remote_digests runs in the owner's processes only", file=sys.stderr)
        return 2
    if not a.dry_run:
        try:
            import ea_config
            if not ea_config.is_ea_enabled():
                print(f"{a.cmd}: EA paused (ea_enabled=false in ea-config.json). Skipping.")
                return 0
        except Exception:
            pass
    if a.cmd == "list-digest":
        return run_list_digest(dry_run=a.dry_run, cfg=cfg)
    return run_admissions_digest(dry_run=a.dry_run, cfg=cfg)


if __name__ == "__main__":
    raise SystemExit(main())
