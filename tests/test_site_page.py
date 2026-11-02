#!/usr/bin/env python3
"""The public page, site/index.html, names no third-party chat platform,
gives every image a text alternative, and links only to what exists.

GitHub Pages publishes site/ (.github/workflows/pages.yml). The chat on the
page is Tacitus's own, so the page names neither of the platforms an
install may also run on. A link into this repository
(https://github.com/arpg/tacitus/blob/main/<path>#<anchor>) names a file in
the tree, and its anchor a heading of that file as GitHub slugs it; a
relative link names a file beside the page; an in-page link names an id on
the page. Any other link is one of the few addresses in ALLOWED.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"
PAGE = SITE / "index.html"
BLOB = "https://github.com/arpg/tacitus/blob/main/"
# Addresses that are not files in the tree: the repository and its
# discussions on GitHub, and the font stylesheet.
ALLOWED = (
    "https://github.com/arpg/tacitus",
    "https://github.com/arpg/tacitus/discussions",
    "https://fonts.googleapis.com",
)
PLATFORMS = re.compile(r"zulip|slack", re.I)
DASHES = ("—", "&mdash;", "&#8212;", "&#x2014;")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.imgs: list[dict] = []
        self.svgs: list[dict] = []
        self._svg: dict | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag in ("a", "link") and a.get("href"):
            self.links.append(a["href"])
        if tag in ("img", "script") and a.get("src"):
            self.links.append(a["src"])
        if tag == "img":
            self.imgs.append(a)
        if tag == "svg":
            self._svg = {"label": a.get("aria-label", ""), "title": "", "hidden": a.get("aria-hidden") == "true"}
            self.svgs.append(self._svg)
        if tag == "title" and self._svg is not None:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "svg":
            self._svg = None

    def handle_data(self, data):
        if self._in_title and self._svg is not None:
            self._svg["title"] += data


def parse(html: str) -> _Page:
    p = _Page()
    p.feed(html)
    return p


def github_slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def heading_slugs(path: Path) -> set[str]:
    slugs, fence = set(), False
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("```"):
            fence = not fence
            continue
        if not fence and line.startswith("#"):
            slugs.add(github_slug(line.lstrip("#")))
    return slugs


def broken_links(page: _Page, repo: Path = REPO, site: Path = SITE) -> list[str]:
    bad = []
    for url in page.links:
        if url.startswith("#"):
            if url[1:] not in page.ids:
                bad.append(f"{url}: no element has this id")
        elif url.startswith(BLOB):
            rel, _, anchor = url[len(BLOB):].partition("#")
            target = repo / rel
            if not target.is_file():
                bad.append(f"{url}: {rel} is not a file in the repository")
            elif anchor and target.suffix == ".md" and anchor not in heading_slugs(target):
                bad.append(f"{url}: {rel} has no heading #{anchor}")
        elif re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) or url.startswith("//"):
            if url.rstrip("/") not in ALLOWED and not url.startswith(ALLOWED[2] + "/"):
                bad.append(f"{url}: an outside address the page does not allow")
        elif not (site / url.split("#")[0].split("?")[0]).is_file():
            bad.append(f"{url}: no such file beside the page")
    return bad


def test_the_page_names_no_chat_platform():
    hits = [f"{n}: {line.strip()[:100]}" for n, line in enumerate(PAGE.read_text().splitlines(), 1)
            if PLATFORMS.search(line)]
    assert not hits, "the page names a chat platform:\n" + "\n".join(hits)


def test_the_page_has_no_em_dash():
    text = PAGE.read_text()
    assert not [d for d in DASHES if d in text], "an em-dash on the page: use a comma, a colon or a full stop"


def test_every_image_has_alt_text():
    page = parse(PAGE.read_text())
    assert page.imgs, "the page shows the phone figure"
    missing = [i.get("src", "?") for i in page.imgs if not (i.get("alt") or "").strip()]
    assert not missing, f"images without alt text: {missing}"
    unnamed = [s for s in page.svgs if not s["hidden"] and not (s["title"].strip() or s["label"].strip())]
    assert not unnamed, "an illustration without a <title> or an aria-label"


def test_every_link_resolves():
    page = parse(PAGE.read_text())
    assert any(u.startswith(BLOB) for u in page.links), "the page links into the docs"
    bad = broken_links(page)
    assert not bad, "links that lead nowhere:\n" + "\n".join(bad)


def test_the_checks_catch_a_bad_page(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\n## Your steps\n\n```\n# not a heading\n```\n")
    site = tmp_path / "site"
    site.mkdir()
    (site / "figure.png").write_bytes(b"")
    html = (
        '<a href="#here">ok</a><p id="here"></p>'
        f'<a href="{BLOB}docs/guide.md#your-steps">ok</a>'
        f'<a href="{BLOB}docs/guide.md#not-a-heading">bad anchor</a>'
        f'<a href="{BLOB}docs/missing.md">bad file</a>'
        '<a href="#nowhere">bad id</a>'
        '<a href="https://example.org/">outside</a>'
        '<img src="figure.png" alt="A figure"><img src="gone.png" alt="">'
        '<svg role="img"><title>Named</title></svg><svg role="img"></svg>'
    )
    page = parse(html)
    bad = broken_links(page, repo=tmp_path, site=site)
    assert [b.split(": ")[0] for b in bad] == [
        f"{BLOB}docs/guide.md#not-a-heading", f"{BLOB}docs/missing.md", "#nowhere",
        "https://example.org/", "gone.png"]
    assert [i["src"] for i in page.imgs if not i.get("alt")] == ["gone.png"]
    assert [bool(s["title"]) for s in page.svgs] == [True, False]
    assert PLATFORMS.search("Runs beside Zulip") and not PLATFORMS.search("Runs on your phone")
