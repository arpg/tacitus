"""doc_text.py: the read-only text of a .docx, .xlsx, PDF or text file for
lanes that run with an allowlist. A .docx is built here by hand (the XML parts a
word processor writes, with a tracked change and a reviewer comment);
every name in it is made up. Nothing reaches the network."""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import doc_text as dt  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {NS}><w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Letter of Intent</w:t></w:r></w:p>
  <w:p><w:r><w:t xml:space="preserve">Title: Training for </w:t></w:r>
       <w:del w:id="1" w:author="Ari"><w:r><w:delText>Old</w:delText></w:r></w:del>
       <w:ins w:id="2" w:author="Ari"><w:r><w:t>New</w:t></w:r></w:ins>
       <w:r><w:t xml:space="preserve"> Manufacturing</w:t></w:r>
       <w:r><w:commentReference w:id="7"/></w:r></w:p>
  <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>First aim</w:t></w:r></w:p>
  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Partner</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Role</w:t></w:r></w:p></w:tc></w:tr>
         <w:tr><w:tc><w:p><w:r><w:t>Lab A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Data</w:t><w:tab/><w:t>tools</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""

COMMENTS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments {NS}><w:comment w:id="7" w:author="Sky Ito"><w:p><w:r><w:t>Need something snappier.</w:t></w:r></w:p></w:comment></w:comments>"""


@pytest.fixture
def ea(tmp_path, monkeypatch):
    root = tmp_path / "ea"
    for d in ("state", "uploads", "config", "auth"):
        (root / d).mkdir(parents=True)
    monkeypatch.setattr(dt, "EA_DIR", root)
    return root


def _docx(path: Path, comments: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", DOCUMENT)
        if comments:
            z.writestr("word/comments.xml", COMMENTS)
    return path


def test_a_docx_reads_in_order_with_changes_accepted_and_comments_anchored(ea):
    p = _docx(ea / "uploads" / "Letter of Intent draft 3.docx")
    text = dt.document_text(dt.allowed_path(str(p)))
    lines = text.splitlines()
    assert lines[0] == "# Letter of Intent draft 3.docx (docx)"
    assert "Tracked changes: 1 insertion(s), 1 deletion(s). Shown accepted; --tracked marks them." in text
    assert "# Letter of Intent" in lines
    assert "Title: Training for New Manufacturing[c1]" in lines
    assert "- First aim" in lines
    assert "| Partner | Role |" in lines and "| Lab A | Data\ttools |" in lines
    assert text.index("# Letter of Intent") < text.index("Title:") < text.index("| Partner")
    assert lines[-2:] == ["Comments (1):",
                          '[c1] Sky Ito on "Title: Training for New Manufacturing[c1]": Need something snappier.']
    assert "Old" not in text


def test_tracked_marks_insertions_and_deletions(ea):
    p = _docx(ea / "state" / "draft.docx", comments=False)
    text = dt.document_text(dt.allowed_path(str(p)), tracked=True)
    assert "Title: Training for [-Old-][+New+] Manufacturing[c1]" in text
    assert "Comments" not in text


def test_only_the_document_folders_are_read(ea, tmp_path):
    secret = ea / "config" / "ea-config.json"
    secret.write_text("{}")
    with pytest.raises(dt.DocError, match="outside the folders"):
        dt.allowed_path(str(secret))
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(dt.DocError, match="outside the folders"):
        dt.allowed_path(str(outside))
    # a link inside an allowed folder that points out of it is refused
    link = ea / "uploads" / "link.txt"
    link.symlink_to(ea / "auth")
    with pytest.raises(dt.DocError):
        dt.allowed_path(str(link))
    (ea / "uploads" / "to-config.json").symlink_to(secret)
    with pytest.raises(dt.DocError, match="outside the folders"):
        dt.allowed_path(str(ea / "uploads" / "to-config.json"))
    with pytest.raises(dt.DocError, match="no such file"):
        dt.allowed_path(str(ea / "uploads" / "missing.docx"))


def test_other_types_say_what_is_read(ea):
    old = ea / "uploads" / "letter.docx"
    old.write_bytes(b"\xd0\xcf\x11\xe0 not a zip")
    with pytest.raises(dt.DocError, match="not a .docx"):
        dt.document_text(dt.allowed_path(str(old)))
    sheet = ea / "uploads" / "budget.xls"
    sheet.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(dt.DocError, match="reads .docx, .xlsx, .pdf and text files"):
        dt.document_text(dt.allowed_path(str(sheet)))
    fake = ea / "uploads" / "budget.xlsx"
    fake.write_bytes(b"PK")
    with pytest.raises(dt.DocError, match="not an .xlsx"):
        dt.document_text(dt.allowed_path(str(fake)))
    note = ea / "uploads" / "notes.md"
    note.write_text("# Notes\nline")
    assert dt.document_text(dt.allowed_path(str(note))) == "# notes.md\n\n# Notes\nline"


def test_an_xlsx_reads_every_sheet_with_cell_addresses(ea):
    openpyxl = pytest.importorskip("openpyxl")
    import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hires"
    ws.append(["Name", "Start", "FTE", "Notes"])
    ws.append(["Ann Lee", datetime.datetime(2027, 1, 1), 0.5, "line one\nline two"])
    ws.append([])
    ws.append(["Bo | Co", None, 20.0])
    wb.create_sheet("Empty")
    p = ea / "uploads" / "hires.xlsx"
    wb.save(str(p))
    text = dt.document_text(dt.allowed_path(str(p)))
    assert text.splitlines() == [
        "# hires.xlsx (xlsx, 2 sheets: Hires, Empty)",
        "",
        "## Hires",
        "| | A | B | C | D |",
        "| 1 | Name | Start | FTE | Notes |",
        "| 2 | Ann Lee | 2027-01-01 | 0.5 | line one<br>line two |",
        "| 4 | Bo \\| Co |  | 20 |  |",
        "",
        "## Empty",
        "(empty)",
    ]


def test_an_xlsx_that_unpacks_too_large_is_refused(ea, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x" * 5000
    p = ea / "uploads" / "big.xlsx"
    wb.save(str(p))
    monkeypatch.setattr(dt, "MAX_PART_BYTES", 2000)
    with pytest.raises(dt.DocError, match="over"):
        dt.document_text(dt.allowed_path(str(p)))


def test_long_text_pages(ea, capsys):
    p = ea / "state" / "long.txt"
    p.write_text("a" * 2500)
    assert dt.main([str(p), "--max-chars", "1000"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# long.txt\n\n" + "a" * 988)
    assert "[1512 more characters: --offset 1000]" in out
    assert dt.main([str(ea / "config")]) == 1


def test_a_letter_pdf_names_its_source_also_after_a_copy(ea, monkeypatch, tmp_path):
    import compile_letter as cl
    import pdf_utils
    monkeypatch.setattr(pdf_utils, "extract_text_from_pdf", lambda data, max_pages=200: ("Dear Sam", pdf_utils.OK))
    (ea / "letters" / "memo").mkdir(parents=True)
    (ea / "docs" / "proj").mkdir(parents=True)
    tex = ea / "letters" / "memo" / "memo.tex"
    tex.write_text("\\documentclass{letter}")
    built = ea / "letters" / "memo" / "memo.pdf"
    built.write_bytes(b"%PDF-1.4 memo")
    cl.record_build(tex, built, builds_file=ea / "state" / "letter-builds.jsonl")
    # A renamed copy in another folder still finds the .tex by its bytes.
    copy = ea / "docs" / "proj" / "Memo for the meeting.pdf"
    copy.write_bytes(built.read_bytes())
    out = dt.document_text(dt.allowed_path(str(copy), ea))
    assert f"compile_letter.py from {tex}" in out and "Dear Sam" in out
    # A PDF no build made, or one whose .tex is gone, has no source line.
    other = ea / "docs" / "proj" / "scan.pdf"
    other.write_bytes(b"%PDF-1.4 scan")
    assert "compile_letter" not in dt.document_text(dt.allowed_path(str(other), ea))
    tex.unlink()
    assert "compile_letter" not in dt.document_text(dt.allowed_path(str(copy), ea))
