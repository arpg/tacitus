#!/usr/bin/env python3
"""Write a .docx from markdown or plain text, under the EA's own folders.

The chat lane allows only named scripts, so a Word file needs one: the
EA writes the content as markdown (the Write tool), then

  python3 doc_write.py docx --in <file.md> --out ~/.ea/docs/<project>/<name>.docx
      [--title "..."] [--template <existing .docx>] [--font Calibri --size 11]

`--text "..."` takes the content inline. The output must land under
~/.ea/{docs,letters,state,research}; the parent folder is made. A
template's body is replaced and its styles, headers and footers kept,
which is how a letterhead comes along.

Markdown understood: `#`..`####` headings, paragraphs (blank-line
separated), `-`/`*` bullets and `1.` numbers (two-space indent nests one
level), `>` quotes, pipe tables (the `|---|` row is the header rule),
`**bold**`, `*italic*`, `_italic_`, `` `code` ``, `---` as a page break.
Anything else is a paragraph. Output is one JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
WRITE_ROOTS = ("docs", "letters", "state", "research")
MAX_CHARS = 2_000_000


class DocError(Exception):
    pass


# ── the path ──────────────────────────────────────────────────────────

def out_path(raw: str, ea_dir: Path | None = None) -> Path:
    """The resolved output file: a .docx under one of WRITE_ROOTS, parent
    made; DocError otherwise."""
    ea = (ea_dir or EA_DIR).expanduser()
    p = Path(raw).expanduser()
    if p.suffix.lower() != ".docx":
        raise DocError(f"{raw}: the output must end in .docx")
    parent = p.parent.resolve()
    roots = [(ea / name).resolve() for name in WRITE_ROOTS]
    if not any(parent == r or parent.is_relative_to(r) for r in roots):
        raise DocError(f"{raw} is outside the folders doc_write writes "
                       f"(~/.ea/{{{','.join(WRITE_ROOTS)}}})")
    parent.mkdir(parents=True, exist_ok=True)
    return parent / p.name


def template_path(raw: str, ea_dir: Path | None = None) -> Path:
    ea = (ea_dir or EA_DIR).expanduser()
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise DocError(f"no such template: {raw}") from None
    roots = [(ea / name).resolve() for name in WRITE_ROOTS + ("uploads",)]
    if not any(p.is_relative_to(r) for r in roots) or p.suffix.lower() != ".docx":
        raise DocError(f"{raw}: a template is a .docx under ~/.ea")
    return p


# ── markdown ──────────────────────────────────────────────────────────

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)")


def inline_runs(text: str) -> list[tuple[str, dict]]:
    """[(text, {"bold"|"italic"|"code": True})...] for one line of markdown."""
    out = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            out.append((piece[2:-2], {"bold": True}))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append((piece[1:-1], {"code": True}))
        elif len(piece) > 2 and piece[0] == piece[-1] and piece[0] in "*_":
            out.append((piece[1:-1], {"italic": True}))
        else:
            out.append((piece, {}))
    return out


def blocks(text: str) -> list[dict]:
    """The document as blocks: {"kind": heading|paragraph|bullet|number|
    quote|table|break, ...}. A list item keeps its "level"; a table keeps
    its "rows" (the first row is the header)."""
    out: list[dict] = []
    para: list[str] = []
    table: list[list[str]] = []

    def flush_para():
        if para:
            out.append({"kind": "paragraph", "text": " ".join(s.strip() for s in para)})
            para.clear()

    def flush_table():
        if table:
            out.append({"kind": "table", "rows": [r[:] for r in table]})
            table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            flush_para()
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c or "---") for c in cells):
                continue
            table.append(cells)
            continue
        flush_table()
        if not stripped:
            flush_para()
            continue
        m = re.match(r"(#{1,4})\s+(.*)", stripped)
        if m:
            flush_para()
            out.append({"kind": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para()
            out.append({"kind": "break"})
            continue
        indent = len(line) - len(line.lstrip(" "))
        m = re.match(r"[-*+]\s+(.*)", stripped)
        if m:
            flush_para()
            out.append({"kind": "bullet", "level": min(indent // 2, 2), "text": m.group(1)})
            continue
        m = re.match(r"\d+[.)]\s+(.*)", stripped)
        if m:
            flush_para()
            out.append({"kind": "number", "level": min(indent // 2, 2), "text": m.group(1)})
            continue
        if stripped.startswith(">"):
            flush_para()
            out.append({"kind": "quote", "text": stripped.lstrip("> ").strip()})
            continue
        para.append(stripped)
    flush_para()
    flush_table()
    return out


# ── the document ──────────────────────────────────────────────────────

def _style(doc, name: str, fallback: str = "Normal"):
    try:
        doc.styles[name]
        return name
    except KeyError:
        return fallback


def _add_runs(paragraph, text: str, font: str | None, size: float | None):
    from docx.shared import Pt
    for piece, fmt in inline_runs(text):
        run = paragraph.add_run(piece)
        if fmt.get("bold"):
            run.bold = True
        if fmt.get("italic"):
            run.italic = True
        if fmt.get("code"):
            run.font.name = "Consolas"
        elif font:
            run.font.name = font
        if size and not fmt.get("code"):
            run.font.size = Pt(size)


def _clear_body(doc):
    body = doc.element.body
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


def build(text: str, out: Path, *, title: str = "", template: Path | None = None,
          font: str | None = None, size: float | None = None) -> dict:
    from docx import Document
    if len(text) > MAX_CHARS:
        raise DocError(f"the content is over {MAX_CHARS:,} characters")
    doc = Document(str(template)) if template else Document()
    if template:
        _clear_body(doc)
    if font or size:
        from docx.shared import Pt
        normal = doc.styles["Normal"]
        if font:
            normal.font.name = font
        if size:
            normal.font.size = Pt(size)
    counts = {"headings": 0, "paragraphs": 0, "lists": 0, "tables": 0}
    if title:
        doc.add_heading(title, level=0)
        counts["headings"] += 1
    for b in blocks(text):
        kind = b["kind"]
        if kind == "heading":
            p = doc.add_heading("", level=b["level"])
            _add_runs(p, b["text"], None, None)
            counts["headings"] += 1
        elif kind == "paragraph":
            p = doc.add_paragraph()
            _add_runs(p, b["text"], font, size)
            counts["paragraphs"] += 1
        elif kind in ("bullet", "number"):
            base = "List Bullet" if kind == "bullet" else "List Number"
            name = base + (f" {b['level'] + 1}" if b["level"] else "")
            p = doc.add_paragraph(style=_style(doc, name, _style(doc, base, "List Paragraph")))
            _add_runs(p, b["text"], font, size)
            counts["lists"] += 1
        elif kind == "quote":
            p = doc.add_paragraph(style=_style(doc, "Quote", "Normal"))
            _add_runs(p, b["text"], font, size)
            if p.style.name == "Normal":
                for r in p.runs:
                    r.italic = True
            counts["paragraphs"] += 1
        elif kind == "table":
            rows = b["rows"]
            width = max(len(r) for r in rows)
            t = doc.add_table(rows=len(rows), cols=width)
            t.style = _style(doc, "Table Grid", "Normal Table")
            for i, row in enumerate(rows):
                for j in range(width):
                    cell = t.cell(i, j)
                    cell.text = ""
                    _add_runs(cell.paragraphs[0], row[j] if j < len(row) else "", font, size)
                    if i == 0:
                        for r in cell.paragraphs[0].runs:
                            r.bold = True
            counts["tables"] += 1
        elif kind == "break":
            doc.add_page_break()
    doc.save(str(out))
    return {"ok": True, "path": str(out), **counts}


# ── CLI ───────────────────────────────────────────────────────────────

def cmd_docx(args) -> int:
    if bool(args.inp) == bool(args.text):
        print(json.dumps({"ok": False, "error": "give --in <file> or --text, not both"}))
        return 2
    try:
        if args.inp:
            src = Path(args.inp).expanduser()
            if not src.is_file():
                raise DocError(f"no such input: {args.inp}")
            text = src.read_text(encoding="utf-8", errors="replace")
        else:
            text = args.text
        out = out_path(args.out)
        tpl = template_path(args.template) if args.template else None
        res = build(text, out, title=args.title or "", template=tpl, font=args.font, size=args.size)
    except DocError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except Exception as e:  # python-docx errors: a bad template, a locked file
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}))
        return 1
    print(json.dumps(res))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("docx", help="write a .docx from markdown or text", allow_abbrev=False)
    p.add_argument("--in", dest="inp", help="the markdown or text file")
    p.add_argument("--text", help="the content inline")
    p.add_argument("--out", required=True, help="the .docx to write, under ~/.ea/{docs,letters,state,research}")
    p.add_argument("--title", default="", help="a title heading above the content")
    p.add_argument("--template", help="an existing .docx whose styles, headers and footers to keep")
    p.add_argument("--font", help="the body font, e.g. Calibri")
    p.add_argument("--size", type=float, help="the body size in points")
    p.set_defaults(func=cmd_docx)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
