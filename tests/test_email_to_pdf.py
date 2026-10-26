"""email_to_pdf: the Gmail read and the page it prints.

The renderer's network rules are in test_outbound_log.py."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import email_to_pdf as E  # noqa: E402


def _run_returning(monkeypatch, payload: dict, rc: int = 0):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(E.subprocess, "run", fake_run)
    return calls


def test_a_gmail_message_is_read_through_gmail_client_with_its_html(monkeypatch):
    calls = _run_returning(monkeypatch, {
        "id": "18f0a1b2c3d4e5f6", "subject": "Your booking is confirmed",
        "from": "Hotel Desk <desk@hotel.example>", "date": "Tue, 13 Oct 2026 09:12:00 +0000",
        "body": "Total 100.00", "html": "<html><body><p>Total 100.00</p></body></html>"})
    msg = E._fetch_gmail_message("someone@gmail.example", "18f0a1b2c3d4e5f6")
    argv = calls[0]
    assert Path(argv[1]).name == "gmail_client.py"
    assert argv[2:] == ["get-message", "--email", "someone@gmail.example",
                        "--message-id", "18f0a1b2c3d4e5f6"]
    assert msg == {"subject": "Your booking is confirmed",
                   "sender": "Hotel Desk <desk@hotel.example>",
                   "date": "Tue, 13 Oct 2026 09:12:00 +0000",
                   "body": "<html><body><p>Total 100.00</p></body></html>",
                   "body_type": "html"}


def test_a_gmail_message_with_no_html_part_prints_its_text(monkeypatch):
    _run_returning(monkeypatch, {"subject": "Paid", "from": "a@b.example", "date": "d",
                                 "body": "Paid 5 < 6", "html": ""})
    msg = E._fetch_gmail_message("someone@gmail.example", "18f0a1b2c3d4e5f6")
    assert msg["body"] == "Paid 5 < 6" and msg["body_type"] == "text"


def test_a_gmail_error_raises(monkeypatch):
    _run_returning(monkeypatch, {"error": "No Gmail credentials for someone@gmail.example"})
    with pytest.raises(RuntimeError, match="No Gmail credentials"):
        E._fetch_gmail_message("someone@gmail.example", "18f0a1b2c3d4e5f6")


def test_message_to_pdf_takes_the_gmail_path_only_when_named(monkeypatch):
    seen = []
    monkeypatch.setattr(E, "_fetch_gmail_message", lambda acct, mid: seen.append(("gmail", acct)) or 1 / 0)
    monkeypatch.setattr(E, "_fetch_message", lambda mid, g=None: seen.append(("outlook", mid)) or 1 / 0)
    pytest.importorskip("playwright.sync_api")
    for kw in ({"gmail": "someone@gmail.example"}, {}):
        with pytest.raises(ZeroDivisionError):
            E.message_to_pdf("m1", "/nonexistent/x.pdf", **kw)
    assert seen == [("gmail", "someone@gmail.example"), ("outlook", "m1")]


def test_the_header_keeps_a_named_sender_address():
    page = E._page_html({"subject": "R&D <receipt>", "sender": "Hotel Desk <desk@hotel.example>",
                         "date": "d", "body": "<p>x</p>", "body_type": "html"})
    assert "Hotel Desk &lt;desk@hotel.example&gt;" in page
    assert "R&amp;D &lt;receipt&gt;" in page


def test_a_text_body_is_escaped():
    page = E._page_html({"subject": "s", "sender": "a@b.example", "date": "d",
                         "body": "Total <b>not bold</b>", "body_type": "text"})
    assert "Total &lt;b&gt;not bold&lt;/b&gt;" in page and "<b>" not in page


def test_the_header_goes_inside_an_uppercase_body_tag():
    page = E._page_html({"subject": "s", "sender": "a@b.example", "date": "d",
                         "body": "<HTML><BODY bgcolor='#fff'><p>x</p></BODY></HTML>",
                         "body_type": "html"})
    assert page.index("ea-header") < page.index("<p>x</p>")
    assert "<div id='ea-body' bgcolor='#fff'>" in page
