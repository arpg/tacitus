#!/usr/bin/env python3
"""preprocessor/reply_files.py: which files a reply names, for the bot to
attach after the turn, and which the turn already uploaded itself."""
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import reply_files as RF  # noqa: E402

HOME = "/home/ea-bot"

# The reply that gave a path instead of the letter.
LETTER = (
    "The rest of the letter is unchanged. The PDF is 2 pages.\n\n"
    "*Files:* `~/.ea/letters/gray-college/gray-college.pdf`, with the "
    "`.tex` beside it. The Microsoft PDF is in the same folder for "
    "comparison. Upload the PDF with \"Choose File\" at "
    "http://careers.example.edu/ref/new/100001/0f1e2d3c4b5a69788796a5b4c3d2e1f0."
)


def test_the_letter_reply_names_one_file():
    assert RF.named_paths(LETTER, HOME) == [
        "/home/ea-bot/.ea/letters/gray-college/gray-college.pdf"]


def test_bare_paths_lose_their_trailing_punctuation():
    text = ("Saved to ~/.ea/docs/x/report.pdf. Also /home/ea-bot/.ea/docs/x/fig.png, "
            "and (~/.ea/docs/x/table.xlsx) and _~/.ea/docs/x/slides.pptx_ and "
            "*~/.ea/docs/x/data.csv*!")
    assert RF.named_paths(text, HOME) == [
        "/home/ea-bot/.ea/docs/x/report.pdf",
        "/home/ea-bot/.ea/docs/x/fig.png",
        "/home/ea-bot/.ea/docs/x/table.xlsx",
        "/home/ea-bot/.ea/docs/x/slides.pptx",
        "/home/ea-bot/.ea/docs/x/data.csv",
    ]


def test_a_code_span_may_hold_spaces():
    text = "It is `~/.ea/docs/nsf/Two pager v2.pdf` now."
    assert RF.named_paths(text, HOME) == ["/home/ea-bot/.ea/docs/nsf/Two pager v2.pdf"]


def test_what_is_not_a_path_to_attach():
    text = ("A bare name expeditions-two-pager.pdf, a relative docs/x.pdf, a URL "
            "https://example.org/files/paper.pdf and <https://a.b/c.pdf|link>, "
            "a source `~/.ea/letters/x.tex`, notes ~/.ea/docs/notes.md, a state "
            "file /home/ea-bot/.ea/state/pending-event.json, a folder "
            "`~/.ea/letters/gray-college/`, and C:/Users/x.pdf.")
    assert RF.named_paths(text, HOME) == []


def test_a_handoff_for_the_claude_app_attaches_and_other_markdown_does_not():
    """skills/claude-handoff.md: the one Markdown file made for the Claude
    app comes to the phone as an attachment; notes and sources stay paths."""
    text = ("`~/.ea/docs/bs-ai/bs-ai-proposal-2026-11-17.claude.md` has the proposal. "
            "Notes are at ~/.ea/docs/bs-ai/notes.md, the source at ~/.ea/docs/bs-ai/claude.md "
            "and a bare suffix at ~/.ea/docs/bs-ai/.claude.md; also ~/.ea/docs/x/Plan.CLAUDE.MD.")
    assert RF.named_paths(text, HOME) == [
        "/home/ea-bot/.ea/docs/bs-ai/bs-ai-proposal-2026-11-17.claude.md",
        "/home/ea-bot/.ea/docs/x/Plan.CLAUDE.MD",
    ]


def test_each_file_once_and_case_folded_types():
    text = ("`/home/ea-bot/.ea/docs/a/Report.PDF` and again "
            "~/.ea/docs/a/Report.PDF and ~/.ea/docs/a/../a/Report.PDF")
    assert RF.named_paths(text, HOME) == ["/home/ea-bot/.ea/docs/a/Report.PDF"]


def test_uploaded_names_come_from_upload_commands_only():
    commands = [
        "python3 /home/ea-bot/ea-system/preprocessor/chat_upload.py --channel stream:4 "
        "--thread-ts 'stream:4:topic:Letters' --file /home/ea-bot/.ea/letters/a/a.pdf "
        "--file=/tmp/claude-997/s/fig.png",
        "python3 /home/ea-bot/ea-system/preprocessor/slack_upload.py --channel C1 "
        "--file \"/home/ea-bot/.ea/docs/Two pager.pdf\"",
        "python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py attach-file "
        "--draft-id d --file /home/ea-bot/.ea/letters/other.pdf",
    ]
    assert RF.uploaded_names(commands) == {"a.pdf", "fig.png", "Two pager.pdf"}
    assert RF.uploaded_names(["chat_upload.py --file 'unclosed"]) == {"'unclosed"}
    assert RF.uploaded_names(None) == set()


def test_transcript_commands_reads_bash_tool_uses():
    rows = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Building the PDF."},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "pdflatex x.tex", "description": "build"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "Output written on x.pdf"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "python3 chat_upload.py --file /a/x.pdf"}}]}},
    ]
    raw = "\n".join(json.dumps(r) for r in rows) + "\nnot json \"tool_use\"\n"
    commands = RF.transcript_commands(raw)
    assert commands == ["pdflatex x.tex", "python3 chat_upload.py --file /a/x.pdf"]
    assert RF.uploaded_names(commands) == {"x.pdf"}
    assert RF.transcript_commands("") == []


def test_sent_log_passes_each_thread_and_digest_once():
    log = RF.SentLog(size=2)
    assert log.first_time("t1", "d1")
    assert not log.first_time("t1", "d1")
    assert log.first_time("t2", "d1")
    assert log.first_time("t1", "d2")
    # The oldest pair falls out once the log is full.
    assert log.first_time("t1", "d1")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok {t.__name__}")
            passed += 1
        except Exception:
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
