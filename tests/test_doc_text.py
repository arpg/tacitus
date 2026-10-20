"""doc_text.py: the read-only text of a .docx, PDF or text file for lanes
that run with an allowlist. A .docx is built here by hand (the XML parts a
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
    sheet = ea / "uploads" / "budget.xlsx"
    sheet.write_bytes(b"PK")
    with pytest.raises(dt.DocError, match="reads .docx, .pdf and text files"):
        dt.document_text(dt.allowed_path(str(sheet)))
    note = ea / "uploads" / "notes.md"
    note.write_text("# Notes\nline")
    assert dt.document_text(dt.allowed_path(str(note))) == "# notes.md\n\n# Notes\nline"


def test_long_text_pages(ea, capsys):
    p = ea / "state" / "long.txt"
    p.write_text("a" * 2500)
    assert dt.main([str(p), "--max-chars", "1000"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# long.txt\n\n" + "a" * 988)
    assert "[1512 more characters: --offset 1000]" in out
    assert dt.main([str(ea / "config")]) == 1
