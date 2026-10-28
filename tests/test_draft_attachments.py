"""Files on a draft: remove-attachment for Outlook and Gmail, the draft-only
check every attachment verb and delete-draft share, and the journal record
that lets the bot re-render a live Send card. No network."""
import base64
import json
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import graph_actions as ga  # noqa: E402
import gmail_client  # noqa: E402


class FakeGraph:
    def __init__(self, is_draft=True, atts=None):
        self.is_draft, self.deleted, self.drafts_deleted = is_draft, [], []
        self.atts = atts if atts is not None else [
            {"id": "a1", "name": "brief-2026-10.pdf", "size": 10},
            {"id": "a2", "name": "budget.xlsx", "size": 20}]

    def graph_get(self, endpoint, params=None, **kw):
        return {"isDraft": self.is_draft}

    def get_attachments(self, mid):
        return list(self.atts)

    def graph_delete(self, endpoint):
        self.deleted.append(endpoint)

    def delete_draft(self, mid):
        self.drafts_deleted.append(mid)


@pytest.fixture
def outlook(monkeypatch):
    events = []
    monkeypatch.setattr(ga._draft_events, "record", lambda action, did, verb="", **k: events.append((action, did, verb)))
    monkeypatch.setattr(ga._draft_capture, "drop", lambda *a, **k: None)

    def use(fake):
        monkeypatch.setattr(ga, "graph", fake)
        return fake
    return use, events


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_remove_by_name_any_case_and_journal(outlook, capsys):
    use, events = outlook
    g = use(FakeGraph())
    ga.cmd_remove_attachment(Namespace(message_id="D1", name="BRIEF-2026-10.PDF", attachment_id=None))
    out = _out(capsys)
    assert out == {"status": "removed", "removed": "brief-2026-10.pdf", "remaining": ["budget.xlsx"]}
    assert g.deleted == ["/me/messages/D1/attachments/a1"]
    assert events == [("updated", "D1", "remove-attachment")]


def test_remove_by_id_unknown_and_ambiguous(outlook, capsys):
    use, _ = outlook
    g = use(FakeGraph(atts=[{"id": "x1", "name": "a.pdf"}, {"id": "x2", "name": "a.pdf"}]))
    ga.cmd_remove_attachment(Namespace(message_id="D1", name="a.pdf", attachment_id=None))
    assert "pass --attachment-id" in _out(capsys)["error"] and g.deleted == []
    ga.cmd_remove_attachment(Namespace(message_id="D1", name="zzz.pdf", attachment_id=None))
    assert _out(capsys)["attachments"] == ["a.pdf", "a.pdf"]
    ga.cmd_remove_attachment(Namespace(message_id="D1", name=None, attachment_id="x2"))
    assert _out(capsys)["removed"] == "a.pdf" and g.deleted == ["/me/messages/D1/attachments/x2"]


def test_received_mail_is_never_changed(outlook, capsys, tmp_path):
    use, events = outlook
    g = use(FakeGraph(is_draft=False))
    ga.cmd_remove_attachment(Namespace(message_id="M1", name="budget.xlsx", attachment_id=None))
    assert "not a draft" in _out(capsys)["error"]
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    ga.cmd_attach_file(Namespace(message_id="M1", file=str(f), name=None, content_type="application/pdf"))
    assert "not a draft" in _out(capsys)["error"]
    ga.cmd_delete_draft(Namespace(message_id="M1"))
    assert _out(capsys)["status"] == "refused"
    assert g.deleted == [] and g.drafts_deleted == [] and events == []


def test_attach_file_journals_an_update(outlook, capsys, tmp_path, monkeypatch):
    use, events = outlook
    g = use(FakeGraph())
    g._api_base = lambda: "https://graph.example/v1.0"
    g.OUTLOOK_REST_URL = "https://outlook.example"
    g._check_endpoint = lambda endpoint: g._api_base() + endpoint
    g._headers = lambda: {}
    import requests
    posted = []
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None:
                        posted.append(json) or types.SimpleNamespace(ok=True))
    f = tmp_path / "rev1.pdf"
    f.write_bytes(b"%PDF")
    ga.cmd_attach_file(Namespace(message_id="D1", file=str(f), name=None, content_type="application/pdf"))
    assert _out(capsys) == {"attached": True, "filename": "rev1.pdf"}
    assert events == [("updated", "D1", "attach-file")]
    sheet = tmp_path / "update.xlsx"
    sheet.write_bytes(b"PK")
    ga.cmd_attach_file(Namespace(message_id="D1", file=str(sheet), name=None, content_type=None))
    assert _out(capsys)["filename"] == "update.xlsx"
    assert posted[-1]["contentType"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", \
        "with no --content-type the type follows the file name, not PDF"


class _GmailService:
    def __init__(self, raw: bytes):
        self.raw = base64.urlsafe_b64encode(raw).decode("ascii")
        self.updated = []

    def users(self):
        return self

    def drafts(self):
        return self

    def get(self, userId=None, id=None, format=None):
        return types.SimpleNamespace(execute=lambda: {
            "id": id, "message": {"id": "mA", "threadId": "thr1", "raw": self.raw}})

    def update(self, userId=None, id=None, body=None):
        self.updated.append(body)
        return types.SimpleNamespace(execute=lambda: {
            "id": id, "message": {"id": "mB", "threadId": body["message"].get("threadId", "")}})


def _gmail_draft_with(files):
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    m = MIMEMultipart("mixed")
    m["To"], m["Subject"], m["In-Reply-To"] = "pat@example.org", "Re: brief", "<orig@x>"
    m.attach(MIMEText("Hi Pat,\n\nAttached.\n\nKit"))
    for name in files:
        part = MIMEApplication(b"data-" + name.encode())
        part.add_header("Content-Disposition", "attachment", filename=name)
        m.attach(part)
    return m.as_bytes()


def test_gmail_remove_keeps_the_rest_of_the_draft(monkeypatch):
    import email as _email
    svc = _GmailService(_gmail_draft_with(["old-brief.pdf", "notes.txt"]))
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, interactive=False: svc)
    res = gmail_client.remove_attachment("kit.kowalczyk1@example.com", "dX", "OLD-brief.pdf")
    assert res["removed"] == ["old-brief.pdf"] and res["remaining"] == ["notes.txt"]
    assert res["thread_id"] == "thr1"
    m = _email.message_from_bytes(base64.urlsafe_b64decode(svc.updated[-1]["message"]["raw"]))
    assert m["To"] == "pat@example.org" and m["In-Reply-To"] == "<orig@x>"
    parts = m.get_payload()
    assert "Attached." in parts[0].get_payload() and [p.get_filename() for p in parts[1:]] == ["notes.txt"]
    assert "attachments" in gmail_client.remove_attachment("kit.kowalczyk1@example.com", "dX", "nope.pdf")
    plain = _GmailService(b"To: flo.thornbury67@example.org\nSubject: s\n\nbody only\n")
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, interactive=False: plain)
    assert "no attachments" in gmail_client.remove_attachment("kit.kowalczyk1@example.com", "dX", "a.pdf")["error"]
    assert plain.updated == []


def test_the_verbs_are_mediated_safe():
    import mail_mediation as M
    assert M.is_safe("graph_actions", "remove-attachment")
    assert M.is_safe("gmail_client", "remove-attachment")
