"""doc_write.py: a .docx from markdown, written only under the EA's own
folders, with headings, lists, tables and inline bold kept; a template's
styles survive and its body is replaced; the chat and cron lanes allow
the script."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import doc_write as dw  # noqa: E402

docx = pytest.importorskip("docx")

MD = """# Course proposal

AIEN 1300 is a **Python-first** course. It uses `numpy` from week one.

## Outcomes

- Write programs
  - with tests
- Read data

1. First
2. Second

> A note from the committee.

| Week | Topic |
|------|-------|
| 1 | Setup |
| 2 | Loops |

---

Closing paragraph in *italics* here.
"""


def _texts(path):
    d = docx.Document(str(path))
    return [(p.style.name, p.text) for p in d.paragraphs], d.tables


def test_markdown_becomes_a_docx_with_its_structure(tmp_path):
    out = dw.out_path(str(tmp_path / "docs" / "p" / "a.docx"), ea_dir=tmp_path)
    res = dw.build(MD, out, title="Proposal")
    assert res["ok"] and res["headings"] == 3 and res["tables"] == 1 and res["lists"] == 5
    paras, tables = _texts(out)
    styles = [s for s, _ in paras]
    assert styles[0] == "Title" and "Heading 1" in styles and "Heading 2" in styles
    assert ("List Bullet", "Write programs") in paras and ("List Bullet 2", "with tests") in paras
    assert ("List Number", "First") in paras
    assert tables[0].cell(0, 0).text == "Week" and tables[0].cell(2, 1).text == "Loops"
    body = next(p for p in docx.Document(str(out)).paragraphs if p.text.startswith("AIEN 1300"))
    bold = [r.text for r in body.runs if r.bold]
    code = [r.text for r in body.runs if r.font.name == "Consolas"]
    assert bold == ["Python-first"] and code == ["numpy"]
    assert any("Closing paragraph" in t for _, t in paras)


def test_the_output_stays_under_the_ea_folders(tmp_path):
    with pytest.raises(dw.DocError, match="outside"):
        dw.out_path(str(tmp_path / "elsewhere" / "a.docx"), ea_dir=tmp_path)
    with pytest.raises(dw.DocError, match="end in .docx"):
        dw.out_path(str(tmp_path / "docs" / "a.rtf"), ea_dir=tmp_path)
    p = dw.out_path(str(tmp_path / "letters" / "new" / "deep" / "a.docx"), ea_dir=tmp_path)
    assert p.parent.is_dir(), "the parent folder is made"


def test_a_template_keeps_its_styles_and_loses_its_body(tmp_path):
    tpl_dir = tmp_path / "uploads"
    tpl_dir.mkdir()
    d = docx.Document()
    d.styles["Normal"].font.name = "Garamond"
    d.add_paragraph("OLD BODY TEXT")
    d.sections[0].header.paragraphs[0].text = "Letterhead"
    d.save(str(tpl_dir / "tpl.docx"))
    out = dw.out_path(str(tmp_path / "docs" / "b.docx"), ea_dir=tmp_path)
    dw.build("New body.", out, template=dw.template_path(str(tpl_dir / "tpl.docx"), ea_dir=tmp_path))
    got = docx.Document(str(out))
    assert [p.text for p in got.paragraphs] == ["New body."]
    assert got.styles["Normal"].font.name == "Garamond"
    assert got.sections[0].header.paragraphs[0].text == "Letterhead"


def test_the_cli_writes_from_a_file_and_reports_json(tmp_path):
    (tmp_path / "docs").mkdir()
    src = tmp_path / "docs" / "in.md"
    src.write_text("# Hi\n\nBody.\n")
    env = {"EA_DIR": str(tmp_path), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "doc_write.py"), "docx", "--in", str(src),
                        "--out", str(tmp_path / "docs" / "out.docx")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["ok"] and d["headings"] == 1 and Path(d["path"]).is_file()
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "doc_write.py"), "docx", "--text", "x",
                        "--out", str(tmp_path / "out.docx")], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "outside" in json.loads(r.stdout)["error"]


def test_the_lanes_allow_the_writer():
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "Bash(python3 {script_dir}/preprocessor/doc_write.py *)," in bot
    assert "doc_write.py docx --in" in bot, "the chat prompt names the writer"
    agent = (ROOT / "run-agent.sh").read_text()
    assert 'preprocessor/doc_write.py *)' in agent
    core = (ROOT / "CLAUDE.md").read_text()
    assert "doc_write.py" in core and "docx --in <md> --out <path>" in core


COMMENTED = """# Review notes

The method uses **a Kalman filter** on the raw stream. The method is sound.

## Results

The method is sound here too, but the table needs units.

| Run | Error |
|-----|-------|
| 1 | 0.4 |
"""


def test_comments_anchor_on_their_text_across_runs_headings_and_cells(tmp_path):
    out = dw.out_path(str(tmp_path / "docs" / "r.docx"), ea_dir=tmp_path)
    comments = [
        {"anchor": "Kalman filter on the raw", "text": "Which variant?"},
        {"anchor": "The method is sound", "text": "First."},
        {"anchor": "The method is sound", "text": "Second.\n\nTwo paragraphs.", "author": "Rob"},
        {"anchor": "Error", "text": "Units?"},
        {"anchor": "Review notes", "text": "On the title."},
        {"anchor": "not in the document", "text": "dropped"},
    ]
    res = dw.build(COMMENTED, out, comments=comments, author="Jane Doe")
    assert res["comments"] == 5 and res["comments_missing"] == ["not in the document"]
    d = docx.Document(str(out))
    got = {c.text: (c.author, c.initials) for c in d.comments}
    assert got["Which variant?"] == ("Jane Doe", "JD") and got["Second.\nTwo paragraphs."] == ("Rob", "R")
    # the anchor spans a bold run and a plain one, split where the anchor starts and ends
    body = next(p for p in d.paragraphs if p.text.startswith("The method uses"))
    assert [r.text for r in body.runs if r.text] == \
        ["The method uses ", "a ", "Kalman filter", " on the raw", " stream. ", "The method is sound", "."]
    assert [r.text for r in body.runs if r.bold] == ["a ", "Kalman filter"]
    # the repeated phrase: the second comment takes the next occurrence
    import doc_text as dt
    text = dt.docx_text(out)
    assert "The method is sound[c3]." in text and "The method is sound[c4] here too" in text
    assert "Error[c5]" in text and "Review notes[c1]" in text


def test_the_cli_takes_a_comments_file_and_refuses_a_bad_one(tmp_path):
    (tmp_path / "docs").mkdir()
    src = tmp_path / "docs" / "in.md"
    src.write_text("# Hi\n\nA body sentence.\n")
    env = {"EA_DIR": str(tmp_path), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "user-profile.json").write_text(json.dumps({"name": "Jane Doe"}))
    cj = tmp_path / "docs" / "c.json"
    cj.write_text(json.dumps([{"anchor": "body sentence", "text": "Say more."}]))
    cmd = [sys.executable, str(ROOT / "preprocessor" / "doc_write.py"), "docx", "--in", str(src),
           "--out", str(tmp_path / "docs" / "out.docx"), "--comments", str(cj)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["ok"] and d["comments"] == 1 and d["comments_missing"] == []
    c = next(iter(docx.Document(d["path"]).comments))
    assert c.author == "Jane Doe" and c.text == "Say more."
    cj.write_text(json.dumps([{"text": "no anchor"}]))
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "anchor" in json.loads(r.stdout)["error"]
