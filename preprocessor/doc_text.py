#!/usr/bin/env python3
"""Print the text of a document the EA saved or the user uploaded.

The chat and cron lanes run with an allowlist and no general interpreter,
so a Word file needs a reader of its own. This one reads, and only reads:

  .docx        the body in order: headings as `#`, list items as `-`,
               tables as `|` rows; the text with tracked changes accepted
               (`--tracked` marks them `[+inserted+]` and `[-deleted-]`);
               reviewer comments as `[c1]` at their anchor and listed at
               the end with their authors
  .pdf         through pdf_utils (pypdf or pdftotext); a PDF that
               compile_letter.py built names its .tex source in the
               header, also when the PDF was copied or renamed since
  text files   .txt .md .csv .tsv .tex .json .html .xml .rtf as UTF-8

A .docx is a zip; it is opened read-only and only its XML parts are read,
each under a size cap, so nothing is extracted to disk.

The path must resolve, after every symlink, to a regular file under one
of the EA's document folders (READ_ROOTS under ~/.ea), never auth/,
config/ or logs/.

    python3 doc_text.py <path> [--tracked] [--max-chars 60000] [--offset 0]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
READ_ROOTS = ("state", "docs", "uploads", "letters", "research")
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_PART_BYTES = 40 * 1024 * 1024
DEFAULT_MAX_CHARS = 60_000
BUILDS_TAIL_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".tex", ".json", ".html", ".htm", ".xml", ".rtf"}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocError(Exception):
    pass


# ── the path ──────────────────────────────────────────────────────────

def allowed_path(raw: str, ea_dir: Path | None = None) -> Path:
    """The resolved file, or DocError when it is outside READ_ROOTS, not a
    regular file, or too large."""
    ea = (ea_dir or EA_DIR).expanduser()
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise DocError(f"no such file: {raw}") from None
    roots = []
    for name in READ_ROOTS:
        try:
            roots.append((ea / name).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(p.is_relative_to(r) for r in roots):
        raise DocError(f"{raw} is outside the folders doc_text reads "
                       f"(~/.ea/{{{','.join(READ_ROOTS)}}})")
    if not p.is_file():
        raise DocError(f"{raw} is not a regular file")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise DocError(f"{raw} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return p


# ── .docx ─────────────────────────────────────────────────────────────

def _part(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise DocError(f"{name} in the file is over {MAX_PART_BYTES // (1024 * 1024)} MB")
    with z.open(info) as f:
        data = f.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise DocError(f"{name} in the file is over {MAX_PART_BYTES // (1024 * 1024)} MB")
    return ET.fromstring(data)


class _Docx:
    def __init__(self, tracked: bool):
        self.tracked = tracked
        self.ins = 0
        self.dels = 0
        self.comment_at: dict[str, str] = {}   # comment id -> its paragraph
        self.comment_tag: dict[str, str] = {}  # comment id -> "c1"

    def _tag(self, cid: str) -> str:
        if cid not in self.comment_tag:
            self.comment_tag[cid] = f"c{len(self.comment_tag) + 1}"
        return self.comment_tag[cid]

    def runs(self, el: ET.Element, out: list[str], ids: list[str]):
        """Walk a paragraph's content in document order."""
        for child in el:
            tag = child.tag
            if tag == W + "t":
                out.append(child.text or "")
            elif tag == W + "delText":
                if self.tracked:
                    out.append(f"[-{child.text or ''}-]")
            elif tag == W + "tab":
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                out.append("\n")
            elif tag == W + "ins":
                self.ins += 1
                if self.tracked:
                    out.append("[+")
                self.runs(child, out, ids)
                if self.tracked:
                    out.append("+]")
            elif tag == W + "del":
                self.dels += 1
                self.runs(child, out, ids)
            elif tag == W + "commentReference":
                cid = child.get(W + "id") or ""
                ids.append(cid)
                out.append(f"[{self._tag(cid)}]")
            elif tag in (W + "r", W + "hyperlink", W + "smartTag", W + "fldSimple",
                         W + "sdt", W + "sdtContent", W + "moveTo"):
                self.runs(child, out, ids)

    def paragraph(self, p: ET.Element) -> str:
        out: list[str] = []
        ids: list[str] = []
        self.runs(p, out, ids)
        text = "".join(out).strip()
        ppr = p.find(W + "pPr")
        prefix = ""
        if ppr is not None:
            style = ppr.find(W + "pStyle")
            val = (style.get(W + "val") or "") if style is not None else ""
            if val.lower().startswith("heading") and val[7:].isdigit():
                prefix = "#" * min(int(val[7:]), 6) + " "
            elif val.lower() == "title":
                prefix = "# "
            elif ppr.find(W + "numPr") is not None:
                prefix = "- "
        for cid in ids:
            self.comment_at.setdefault(cid, text)
        return prefix + text if text else ""

    def block(self, el: ET.Element, lines: list[str]):
        for child in el:
            if child.tag == W + "p":
                t = self.paragraph(child)
                if t:
                    lines.append(t)
            elif child.tag == W + "tbl":
                for tr in child.iter(W + "tr"):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        sub: list[str] = []
                        self.block(tc, sub)
                        cells.append(" ".join(s.replace("\n", " ") for s in sub))
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
            elif child.tag in (W + "sdt", W + "sdtContent", W + "customXml"):
                self.block(child, lines)


def docx_text(path: Path, tracked: bool = False) -> str:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise DocError(f"{path.name} is not a .docx (not a zip); an old .doc needs a PDF or a .docx") from None
    with z:
        doc = _part(z, "word/document.xml")
        if doc is None:
            raise DocError(f"{path.name} has no word/document.xml")
        body = doc.find(W + "body")
        d = _Docx(tracked)
        lines: list[str] = []
        if body is not None:
            d.block(body, lines)
        comments = _part(z, "word/comments.xml")
    head = [f"# {path.name} (docx)"]
    if d.ins or d.dels:
        head.append(f"Tracked changes: {d.ins} insertion(s), {d.dels} deletion(s). "
                    + ("Shown as [+inserted+] and [-deleted-]." if tracked
                       else "Shown accepted; --tracked marks them."))
    out = head + [""] + lines
    if comments is not None:
        notes = []
        for c in comments.findall(W + "comment"):
            cid = c.get(W + "id") or ""
            sub: list[str] = []
            d.block(c, sub)
            text = " ".join(sub).strip()
            if not text:
                continue
            tag = d.comment_tag.get(cid) or d._tag(cid)
            where = d.comment_at.get(cid, "")
            anchor = f' on "{where[:80]}"' if where else ""
            notes.append(f"[{tag}] {c.get(W + 'author') or 'someone'}{anchor}: {text}")
        if notes:
            out += ["", f"Comments ({len(notes)}):"] + notes
    return "\n".join(out)


# ── the rest ──────────────────────────────────────────────────────────

def letter_source(data: bytes) -> Path | None:
    """The .tex compile_letter.py built this PDF from: the newest build
    whose PDF has these bytes, wherever the PDF lives now. None when no
    build matches or the .tex is gone."""
    builds = EA_DIR.expanduser() / "state" / "letter-builds.jsonl"
    try:
        with open(builds, "rb") as f:
            f.seek(max(0, os.path.getsize(builds) - BUILDS_TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("sha256") == digest and row.get("tex"):
            tex = Path(row["tex"])
            if tex.is_file():
                return tex
    return None


def pdf_text(path: Path) -> str:
    import pdf_utils
    data = path.read_bytes()
    text, status = pdf_utils.extract_text_from_pdf(data, max_pages=200)
    if status != pdf_utils.OK:
        raise DocError(f"{path.name}: no text ({status})")
    tex = letter_source(data)
    built = f"Built by compile_letter.py from {tex}; edit that file and compile again.\n\n" if tex else ""
    return f"# {path.name} (pdf)\n\n{built}{text}"


def document_text(path: Path, tracked: bool = False) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return docx_text(path, tracked)
    if suffix == ".pdf":
        return pdf_text(path)
    if suffix in TEXT_SUFFIXES:
        return f"# {path.name}\n\n" + path.read_text(encoding="utf-8", errors="replace")
    raise DocError(f"{path.name}: doc_text reads .docx, .pdf and text files, not {suffix or 'this type'}; "
                   "ask for a .docx or a PDF")


def page(text: str, max_chars: int, offset: int) -> str:
    chunk = text[offset:offset + max_chars]
    rest = len(text) - offset - len(chunk)
    if rest > 0:
        chunk += f"\n\n[{rest} more characters: --offset {offset + len(chunk)}]"
    return chunk


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print the text of a .docx, PDF or text file.",
                                 allow_abbrev=False)
    ap.add_argument("path")
    ap.add_argument("--tracked", action="store_true", help="mark tracked insertions and deletions")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--offset", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        text = document_text(allowed_path(a.path), a.tracked)
    except (DocError, ET.ParseError) as e:
        print(f"doc_text: {e}", file=sys.stderr)
        return 1
    print(page(text, max(1000, a.max_chars), max(0, a.offset)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
