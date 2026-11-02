#!/usr/bin/env python3
"""Tests for drive_client.py, the read-only Google Drive reader.

The reader exists because a private Google Docs link answers HTTP 401 to
fetch_url.py, and the claude.ai Drive connector does not load under a
setup-token login. Three layers:

  A) links: every link shape the EA meets becomes a Drive id; a link that
     names no file is refused with the right next step.
  B) the verbs against a fake Drive service: account fallback, the text
     exports, paging, search clauses, download formats and the errors the
     EA acts on.
  C) the boundary: the verbs the mediator runs are the read verbs only,
     their options are readable to its option check, the sign-in is not
     mediated, and fetch_url points a refused Google link at the reader.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

# No test may touch the live ~/.ea: EA_DIR is a sandbox before the import.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-drive-")

HERE = Path(__file__).resolve().parent
PREPROC = HERE.parent / "preprocessor"
sys.path.insert(0, str(PREPROC))

import pytest  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

import drive_client as D  # noqa: E402
import mail_mediation as M  # noqa: E402

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SHEET_ID = "1SheetIdSheetIdSheetIdSheetId01234"
FOLDER_ID = "1FolderIdFolderIdFolderId0123456"


def _http(status: int, reason: str = "", message: str = "") -> HttpError:
    body = {"error": {"code": status, "message": message or reason,
                      "errors": [{"reason": reason}] if reason else []}}
    return HttpError(types.SimpleNamespace(status=status, reason=reason),
                     json.dumps(body).encode())


class _Req:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeFiles:
    def __init__(self, metas=None, exports=None, media=None, listing=None):
        self.metas = metas or {}
        self.exports = exports or {}
        self.media = media or {}
        self.listing = listing if listing is not None else []
        self.calls = []

    def get(self, fileId, fields=None, supportsAllDrives=None):
        self.calls.append(("get", fileId))
        return _Req(self.metas.get(fileId, _http(404, "notFound")))

    def export(self, fileId, mimeType):
        self.calls.append(("export", fileId, mimeType))
        return _Req(self.exports.get((fileId, mimeType), _http(400, "badRequest")))

    def get_media(self, fileId, supportsAllDrives=None):
        self.calls.append(("get_media", fileId))
        return _Req(self.media.get(fileId, _http(404, "notFound")))

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return _Req({"files": self.listing})


class FakeService:
    def __init__(self, files: FakeFiles, user: str = "you@example.edu"):
        self._files = files
        self._user = user

    def files(self):
        return self._files

    def about(self):
        user = self._user
        return types.SimpleNamespace(get=lambda fields=None: _Req({"user": {"emailAddress": user}}))


def _doc_meta(fid=DOC_ID, mime=D.GOOGLE_DOC, name="Project notes", **extra):
    meta = {"id": fid, "name": name, "mimeType": mime, "modifiedTime": "2026-11-02T16:59:14Z",
            "webViewLink": f"https://docs.google.com/document/d/{fid}/edit",
            "owners": [{"emailAddress": "owner@example.edu"}]}
    meta.update(extra)
    return meta


@pytest.fixture
def drive(monkeypatch):
    """Two signed-in accounts; the test fills each one's fake service."""
    services: dict[str, FakeService] = {}
    monkeypatch.setattr(D, "signed_in_accounts", lambda: sorted(services))
    monkeypatch.setattr(D, "_service", lambda acct: services[acct])
    return services


# ---------------------------------------------------------------------------
# A) links
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("link", [
    f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=sharing",
    f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt",
    f"https://docs.google.com/document/u/1/d/{DOC_ID}/edit#heading=h.x",
    f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit#gid=0",
    f"https://docs.google.com/presentation/d/{DOC_ID}/edit",
    f"https://drive.google.com/file/d/{DOC_ID}/view?usp=drive_link",
    f"https://drive.google.com/open?id={DOC_ID}",
    f"https://drive.google.com/drive/folders/{DOC_ID}",
    f"https://drive.google.com/drive/u/0/folders/{DOC_ID}?usp=sharing",
    DOC_ID,
])
def test_every_link_shape_gives_the_file_id(link):
    assert D.file_id(link) == DOC_ID


@pytest.mark.parametrize("link,needle", [
    ("https://docs.google.com/document/d/e/2PACX-1vQabcdefghijklmnop/pub", "fetch_url.py"),
    ("https://example.com/document/d/" + DOC_ID, "fetch_url.py"),
    ("https://docs.google.com/document/", "address bar"),
    ("file:///etc/passwd", "fetch_url.py"),
    ("not a link", "fetch_url.py"),
])
def test_a_link_that_names_no_drive_file_is_refused_with_a_next_step(link, needle):
    with pytest.raises(D.DriveError) as e:
        D.file_id(link)
    assert needle in e.value.fix


def test_signed_in_accounts_come_from_the_token_files(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "AUTH_DIR", tmp_path)
    monkeypatch.setattr(D, "_checked_token_io", lambda: False)
    (tmp_path / "google_token_you_at_example_edu_drive.json").write_text(
        json.dumps({"refresh_token": "r", "account": "you@example.edu"}))
    # A token that claims another account than its file name is not used.
    (tmp_path / "google_token_other_at_example_edu_drive.json").write_text(
        json.dumps({"refresh_token": "r", "account": "you@example.edu"}))
    # Calendar and Gmail tokens are other files.
    (tmp_path / "google_token_you_at_gmail_com.json").write_text("{}")
    (tmp_path / "google_token_you_at_gmail_com_gmail.json").write_text("{}")
    assert D.signed_in_accounts() == ["you@example.edu"]


def test_a_saved_token_records_its_account(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "AUTH_DIR", tmp_path)
    monkeypatch.setattr(D, "_checked_token_io", lambda: False)
    creds = types.SimpleNamespace(to_json=lambda: json.dumps({"refresh_token": "r"}))
    D._save_token("you@example.edu", creds)
    path = tmp_path / "google_token_you_at_example_edu_drive.json"
    assert json.loads(path.read_text())["account"] == "you@example.edu"
    assert path.stat().st_mode & 0o077 == 0
    assert D.signed_in_accounts() == ["you@example.edu"]


# ---------------------------------------------------------------------------
# B) the verbs
# ---------------------------------------------------------------------------

def test_read_tries_each_account_until_one_opens_the_file(drive):
    drive["a@gmail.com"] = FakeService(FakeFiles())  # 404
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={DOC_ID: _doc_meta()},
        exports={(DOC_ID, "text/markdown"): b"\xef\xbb\xbf# Minutes\r\n\r\nItem one.\r\n"}))
    out = D.read(f"https://docs.google.com/document/d/{DOC_ID}/edit")
    assert out["status"] == "ok"
    assert out["account"] == "you@example.edu"
    assert out["kind"] == "document" and out["format"] == "text/markdown"
    assert out["text"] == "# Minutes\n\nItem one.\n"
    assert out["modified"] == "2026-11-02T16:59:14Z"
    assert out["truncated"] is False and out["next_start"] is None


def test_read_falls_back_to_plain_text_when_markdown_is_not_offered(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={DOC_ID: _doc_meta()}, exports={(DOC_ID, "text/plain"): b"plain notes"}))
    out = D.read(DOC_ID)
    assert out["format"] == "text/plain" and out["text"] == "plain notes"


def test_markdown_images_lose_their_base64_payload(drive):
    md = ("Notes ![][image1] and ![x](data:image/png;base64,AAAA) end\n\n"
          "[image1]: <data:image/png;base64," + "A" * 5000 + ">\n")
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={DOC_ID: _doc_meta()}, exports={(DOC_ID, "text/markdown"): md.encode()}))
    out = D.read(DOC_ID)
    assert "base64" not in out["text"]
    assert "![x](image)" in out["text"]
    assert out["chars"] < 200


def test_a_long_file_comes_in_parts_and_a_negative_start_reads_the_end(drive):
    text = "".join(f"{i:04d}" for i in range(2500))  # 10,000 characters
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={DOC_ID: _doc_meta()}, exports={(DOC_ID, "text/markdown"): text.encode()}))
    first = D.read(DOC_ID, max_chars=4000)
    assert first["text"] == text[:4000]
    assert first["truncated"] is True and first["next_start"] == 4000 and first["chars"] == 10000
    second = D.read(DOC_ID, start=first["next_start"], max_chars=4000)
    assert second["text"] == text[4000:8000]
    tail = D.read(DOC_ID, start=-1000)
    assert tail["text"] == text[-1000:] and tail["start"] == 9000 and tail["truncated"] is False


def test_a_sheet_reads_as_csv_with_a_note_about_the_other_sheets(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={SHEET_ID: _doc_meta(SHEET_ID, D.GOOGLE_SHEET, "Budget")},
        exports={(SHEET_ID, "text/csv"): b"a,b\n1,2\n"}))
    out = D.read(SHEET_ID)
    assert out["kind"] == "spreadsheet" and out["text"] == "a,b\n1,2\n"
    assert "first sheet only" in out["note"] and "xlsx" in out["note"]


def test_a_shortcut_reads_its_target(drive):
    files = FakeFiles(metas={
        "1ShortcutShortcutShortcut00": _doc_meta("1ShortcutShortcutShortcut00", D.GOOGLE_SHORTCUT,
                                                 shortcutDetails={"targetId": DOC_ID}),
        DOC_ID: _doc_meta()}, exports={(DOC_ID, "text/markdown"): b"target"})
    drive["you@example.edu"] = FakeService(files)
    assert D.read("1ShortcutShortcutShortcut00")["text"] == "target"


def test_a_plain_text_file_is_read_and_a_binary_one_is_sent_to_download(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={"1TextFileTextFileText": _doc_meta("1TextFileTextFileText", "text/plain", "n.txt",
                                                  size="12"),
               "1PdfFilePdfFilePdfFil": _doc_meta("1PdfFilePdfFilePdfFil", D.PDF, "r.pdf")},
        media={"1TextFileTextFileText": b"hello there\n"}))
    assert D.read("1TextFileTextFileText")["text"] == "hello there\n"
    with pytest.raises(D.DriveError) as e:
        D.read("1PdfFilePdfFilePdfFil")
    assert "download --output" in e.value.fix and "doc_text.py" in e.value.fix
    assert str(D.EA_DIR / "uploads") in e.value.fix


def test_a_folder_points_at_search(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={FOLDER_ID: _doc_meta(FOLDER_ID, D.GOOGLE_FOLDER, "Notes")}))
    with pytest.raises(D.DriveError) as e:
        D.read(FOLDER_ID)
    assert "search --folder" in e.value.fix


def test_no_account_can_open_it_asks_for_a_share_or_the_text(drive):
    drive["a@gmail.com"] = FakeService(FakeFiles())
    drive["you@example.edu"] = FakeService(FakeFiles(metas={DOC_ID: _http(403, "forbidden")}))
    with pytest.raises(D.DriveError) as e:
        D.read(DOC_ID)
    assert "no signed-in Google account can open this file" in e.value.message
    assert "a@gmail.com: cannot open it" in e.value.message
    assert e.value.fix == D.SHARE_FIX


def test_a_disabled_drive_api_names_the_console_step(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(metas={DOC_ID: _http(
        403, "accessNotConfigured",
        "Google Drive API has not been used in project 123 before or it is disabled.")}))
    with pytest.raises(D.DriveError) as e:
        D.read(DOC_ID)
    assert e.value.fix == D.API_FIX


def test_no_signed_in_account_names_the_chat_sign_in_then_the_terminal(drive):
    with pytest.raises(D.DriveError) as e:
        D.read(DOC_ID)
    assert "pending-google-signin.json" in e.value.fix and "connect Google Drive" in e.value.fix
    assert "drive_client.py setup --email" in e.value.fix


def test_an_account_that_is_not_signed_in_is_refused(drive):
    drive["you@example.edu"] = FakeService(FakeFiles())
    with pytest.raises(D.DriveError) as e:
        D.read(DOC_ID, account="someone@example.org")
    assert "not signed in" in e.value.message


def test_search_quotes_the_words_and_skips_the_order_for_a_text_search(drive):
    files = FakeFiles(listing=[{"id": DOC_ID, "name": "Notes", "mimeType": D.GOOGLE_DOC,
                                "modifiedTime": "2026-11-02T10:00:00Z", "webViewLink": "u",
                                "owners": [{"emailAddress": "o@example.edu"}]}])
    drive["you@example.edu"] = FakeService(files)
    out = D.search(query="Bob's \\ notes")
    kwargs = files.calls[-1][1]
    assert "(name contains 'Bob\\'s \\\\ notes' or fullText contains 'Bob\\'s \\\\ notes')" in kwargs["q"]
    assert "trashed = false" in kwargs["q"] and "orderBy" not in kwargs
    assert out["count"] == 1 and out["files"][0]["account"] == "you@example.edu"
    assert out["files"][0]["kind"] == "document"


def test_search_lists_a_folder_newest_first(drive):
    files = FakeFiles()
    drive["you@example.edu"] = FakeService(files)
    D.search(folder=f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    kwargs = files.calls[-1][1]
    assert f"'{FOLDER_ID}' in parents" in kwargs["q"]
    assert kwargs["orderBy"] == "modifiedTime desc"


def test_download_exports_a_google_file_in_its_default_format(drive, tmp_path):
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={DOC_ID: _doc_meta()}, exports={(DOC_ID, D.DOCX): b"PK\x03\x04docx"}))
    out = D.download(DOC_ID, str(tmp_path / "brief.docx"))
    assert (tmp_path / "brief.docx").read_bytes() == b"PK\x03\x04docx"
    assert out["format"] == "docx" and out["bytes"] == 8


def test_download_refuses_a_format_a_google_file_does_not_export(drive, tmp_path):
    drive["you@example.edu"] = FakeService(FakeFiles(metas={DOC_ID: _doc_meta()}))
    with pytest.raises(D.DriveError):
        D.download(DOC_ID, str(tmp_path / "x.xlsx"), fmt="xlsx")


def test_download_saves_a_stored_file_as_it_is_and_refuses_a_format(drive, tmp_path):
    pdf_id = "1PdfFilePdfFilePdfFil"
    drive["you@example.edu"] = FakeService(FakeFiles(
        metas={pdf_id: _doc_meta(pdf_id, D.PDF, "r.pdf", size="8")}, media={pdf_id: b"%PDF-1.7"}))
    D.download(pdf_id, str(tmp_path / "r.pdf"))
    assert (tmp_path / "r.pdf").read_bytes() == b"%PDF-1.7"
    with pytest.raises(D.DriveError):
        D.download(pdf_id, str(tmp_path / "r2.pdf"), fmt="docx")


def test_a_stored_file_over_the_limit_is_refused_before_the_download(drive, tmp_path):
    pdf_id = "1PdfFilePdfFilePdfFil"
    files = FakeFiles(metas={pdf_id: _doc_meta(pdf_id, D.PDF, "big.pdf",
                                               size=str(D.MAX_DOWNLOAD_BYTES + 1))})
    drive["you@example.edu"] = FakeService(files)
    with pytest.raises(D.DriveError):
        D.download(pdf_id, str(tmp_path / "big.pdf"))
    assert not any(c[0] == "get_media" for c in files.calls)


def test_the_cli_prints_an_error_as_json_with_its_fix(drive, capsys, monkeypatch):
    monkeypatch.setattr(M, "should_mediate", lambda: False)
    rc = D.main(["read", "--url", "https://example.com/x"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["status"] == "error" and "fetch_url.py" in out["fix"]


# ---------------------------------------------------------------------------
# C) the boundary
# ---------------------------------------------------------------------------

def test_the_mediator_runs_the_read_verbs_and_nothing_else():
    assert M.SAFE["drive_client"] == {"read", "search", "download", "accounts"}
    assert "drive_client" not in M.GATED
    for verb in ("setup", "test"):
        assert not M.is_safe("drive_client", verb)
    import mail_mediator
    assert mail_mediator.TOOLS["drive_client"].name == "drive_client.py"


def test_every_mediated_verb_is_a_subcommand_with_readable_options():
    parser = D.build_parser()
    subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    for verb in M.SAFE["drive_client"]:
        assert verb in subs.choices
        assert M.parser_options("drive_client", verb) is not None, verb


def test_the_mediator_checks_the_id_and_confines_the_output():
    assert M.sanitize("drive_client", "read", ["read", "--id", DOC_ID]) is None
    assert M.sanitize("drive_client", "read", ["read", "--id", "../../etc/passwd"])
    assert M.sanitize("drive_client", "download",
                      ["download", "--id", DOC_ID, "--output", "/home/someone/.ssh/x"])
    assert M.sanitize("drive_client", "read", ["read", "--url", "u", "--outp", "/tmp/x"])
    assert "--output" in M.OUTPUT_FLAGS


def test_the_scope_is_read_only():
    assert D.SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


def test_fetch_url_points_a_refused_google_link_at_the_reader(monkeypatch):
    import requests
    import fetch_url
    monkeypatch.setattr(fetch_url, "validate_url", lambda url: {"ips": ["192.0.2.1"]})
    monkeypatch.setattr(fetch_url, "_log_event", lambda *a, **k: None)

    def refused(url):
        resp = requests.Response()
        resp.status_code = 401
        raise requests.HTTPError(response=resp)
    monkeypatch.setattr(fetch_url, "fetch_with_requests", refused)
    out = fetch_url.run(f"https://docs.google.com/document/d/{DOC_ID}/edit", 4000, False)
    assert out["error"] == "HTTP 401" and "drive_client.py read" in out["hint"]
    other = fetch_url.run("https://example.com/private", 4000, False)
    assert "hint" not in other


def test_the_doctor_warns_for_a_listed_account_with_no_drive_sign_in(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ea_doctor_drive",
                                                  HERE.parent / "scripts" / "ea-doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    sys.modules["ea_doctor_drive"] = doctor
    spec.loader.exec_module(doctor)
    tree, auth = tmp_path / "ea", tmp_path / "ea" / "auth"
    (tree / "config").mkdir(parents=True)
    auth.mkdir()

    def row():
        doctor.RESULTS.clear()
        doctor.check_drive(tree, auth)
        [r] = doctor.RESULTS
        return r
    assert row()["status"] == "PASS" and "optional" in row()["detail"]
    (tree / "config" / "user-profile.json").write_text(
        json.dumps({"drive_accounts": ["you@example.edu", "a@gmail.com"]}))
    r = row()
    assert r["status"] == "WARN" and "you@example.edu" in r["detail"] and "a@gmail.com" in r["detail"]
    assert "drive_client.py setup --email" in r["fix"]
    (auth / "google_token_you_at_example_edu_drive.json").write_text("{}")
    (auth / "google_token_a_at_gmail_com_drive.json").write_text("{}")
    assert row()["status"] == "PASS"


def test_the_check_notes_a_sign_in_filed_under_another_address(drive):
    drive["you@example.edu"] = FakeService(FakeFiles(), user="you@example.edu")
    drive["alias@example.edu"] = FakeService(FakeFiles(), user="primary@example.edu")
    rows = {r["account"]: r for r in D.check_accounts()["accounts"]}
    assert rows["you@example.edu"]["ok"] and "note" not in rows["you@example.edu"]
    assert rows["alias@example.edu"]["ok"] and "primary@example.edu" in rows["alias@example.edu"]["note"]
