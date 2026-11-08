"""Images in mail on both providers and both harnesses.

A picture the body shows (an inline, cid: image) is an attachment, and
Graph's hasAttachments leaves it out. These tests hold that every reader
lists it with its flags (Outlook get-attachments, get-message,
get-conversation; Gmail's attachment parts), that the sealed record names
it, that `mail_actions.py describe-attachment` reads it through the local
model (stubbed) or, with none, saves it outside sealed mail, that the
mediator classifies the verb, and that a Codex chat turn keeps view_image
and names the verb. Every Graph, Gmail and model call is a stub; the
fixtures are made-up values.
"""
import base64
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

# gmail_client and graph_client bind EA_DIR at import.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-mail-images-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))

import codex_ghost as cg  # noqa: E402
import codex_turn as ct  # noqa: E402
import gmail_client  # noqa: E402
import graph_actions as ga  # noqa: E402
import graph_client as gc  # noqa: E402
import mail_actions  # noqa: E402
import mail_images  # noqa: E402
import mail_mediation as MM  # noqa: E402
import mail_mediator  # noqa: E402
import mail_privacy as mp  # noqa: E402
import remote_llm as rl  # noqa: E402

MID = "AAMkAGI2TG93AAA" + "x" * 40 + "="
CONV = "AAQkAGI2TG93AAA" + "c" * 30 + "="
# A 1x1 PNG.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4 made-up"

# Graph's attachment list for one message: a schedule pasted into the body,
# a signature logo, a photo attached in its own right, and a PDF.
OUTLOOK_ATTS = [
    {"id": "AAMkATT1=", "name": "image001.png", "contentType": "image/png", "size": 48210,
     "isInline": True, "contentId": "image001.png@01DB0000.00000000"},
    {"id": "AAMkATT2=", "name": "image002.png", "contentType": "image/png", "size": 2900,
     "isInline": True, "contentId": "image002.png@01DB0000.00000000"},
    {"id": "AAMkATT3=", "name": "site-photo.jpg", "contentType": "image/jpeg", "size": 120000,
     "isInline": False, "contentId": None},
    {"id": "AAMkATT4=", "name": "agenda.pdf", "contentType": "application/pdf", "size": 51000,
     "isInline": False, "contentId": None},
]
HTML = ('<html><body><p>The schedule for the visit is below.</p>'
        '<img src="cid:image001.png@01DB0000.00000000" width="600">'
        '<p>Thanks,<br>Pat</p><img src="cid:image002.png@01DB0000.00000000"></body></html>')


def _meta(atts):
    """What the $select listing returns: no contentId, no bytes."""
    return [{k: a[k] for k in ("id", "name", "contentType", "size", "isInline")} for a in atts]


# ── which attachments are images ──────────────────────────────────────

def test_image_detection_by_type_by_name_and_by_cid_reference():
    assert mail_images.is_image("x.bin", "image/png")
    assert mail_images.is_image("Scan.JPG", "application/octet-stream")
    assert not mail_images.is_image("agenda.pdf", "application/pdf")
    assert mail_images.has_cid_images(HTML)
    assert mail_images.has_cid_images("<IMG alt='s' SRC=\"cid:abc\">")
    assert not mail_images.has_cid_images('<img src="https://example.org/a.png">')
    assert not mail_images.has_cid_images("")
    rows = [mail_images.shape({"name": a["name"], "content_type": a["contentType"], "size": a["size"],
                               "is_inline": a["isInline"]}) for a in OUTLOOK_ATTS]
    assert [r["is_image"] for r in rows] == [True, True, True, False]
    assert [r.get("small", False) for r in rows] == [False, True, False, False]
    # the logo is listed but is no content image
    assert [r["name"] for r in mail_images.content_images(rows)] == ["image001.png", "site-photo.jpg"]


# ── Outlook: the listing and the reads ────────────────────────────────

def test_outlook_get_attachments_carries_the_inline_flags(monkeypatch):
    monkeypatch.setattr(ga.graph, "get_attachments", lambda mid: [dict(a, contentBytes="eA==") for a in OUTLOOK_ATTS])
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_get_attachments(SimpleNamespace(message_id=MID))
    rows = json.loads(buf.getvalue())
    assert [r["is_inline"] for r in rows] == [True, True, False, False]
    assert rows[0]["content_id"] == "image001.png@01DB0000.00000000" and rows[2]["content_id"] == ""
    assert [r["is_image"] for r in rows] == [True, True, True, False]
    assert rows[1]["small"] is True and "small" not in rows[0]
    assert "contentBytes" not in json.dumps(rows)


def test_outlook_get_message_lists_an_inline_image_hasattachments_misses(monkeypatch):
    msg = {"id": MID, "subject": "Visit", "conversationId": CONV, "hasAttachments": False,
           "from": {"emailAddress": {"address": "pat@example.org"}}, "toRecipients": [],
           "ccRecipients": [], "receivedDateTime": "2026-11-06T16:00:00Z",
           "body": {"contentType": "html", "content": HTML}}
    listed = []
    monkeypatch.setattr(ga.graph, "get_message_full", lambda mid: msg)
    monkeypatch.setattr(ga.graph, "get_attachment_list",
                        lambda mid: listed.append(mid) or _meta(OUTLOOK_ATTS[:2]))
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_get_message(SimpleNamespace(message_id=MID, raw_html=False))
    out = json.loads(buf.getvalue())
    assert listed == [MID], "a cid: body is read for its attachments though hasAttachments is false"
    assert out["has_attachments"] is False
    assert [(a["name"], a["is_inline"], a["is_image"]) for a in out["attachments"]] == [
        ("image001.png", True, True), ("image002.png", True, True)]
    assert "describe-attachment --provider outlook --message-id " + MID in out["images_hint"]
    # a plain message costs no second call
    listed.clear()
    plain = dict(msg, body={"contentType": "text", "content": "No pictures."})
    monkeypatch.setattr(ga.graph, "get_message_full", lambda mid: plain)
    with redirect_stdout(io.StringIO()):
        ga.cmd_get_message(SimpleNamespace(message_id=MID, raw_html=False))
    assert listed == []
    # only a logo: listed, no hint
    monkeypatch.setattr(ga.graph, "get_message_full", lambda mid: msg)
    monkeypatch.setattr(ga.graph, "get_attachment_list", lambda mid: _meta(OUTLOOK_ATTS[1:2]))
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_get_message(SimpleNamespace(message_id=MID, raw_html=False))
    out = json.loads(buf.getvalue())
    assert out["attachments"][0]["small"] is True and "images_hint" not in out


def test_outlook_get_message_survives_a_failed_listing(monkeypatch):
    msg = {"id": MID, "subject": "Visit", "hasAttachments": True, "from": {"emailAddress": {}},
           "body": {"contentType": "text", "content": "see attached"}}
    monkeypatch.setattr(ga.graph, "get_message_full", lambda mid: msg)

    def boom(mid):
        raise RuntimeError("Graph 503")
    monkeypatch.setattr(ga.graph, "get_attachment_list", boom)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_get_message(SimpleNamespace(message_id=MID, raw_html=False))
    out = json.loads(buf.getvalue())
    assert out["body"] == "see attached" and "Graph 503" in out["attachments_error"]


def test_outlook_get_conversation_reads_the_expanded_lists(monkeypatch):
    seen = {}
    thread = [
        {"id": "AAMkM1=", "subject": "Visit", "from": {"emailAddress": {"address": "pat@example.org"}},
         "receivedDateTime": "2026-11-05T16:00:00Z", "hasAttachments": False,
         "body": {"contentType": "html", "content": HTML}, "attachments": _meta(OUTLOOK_ATTS[:2])},
        {"id": "AAMkM2=", "subject": "RE: Visit", "from": {"emailAddress": {"address": "lee@example.org"}},
         "receivedDateTime": "2026-11-06T16:00:00Z", "hasAttachments": False,
         "body": {"contentType": "text", "content": "Thanks"}, "attachments": _meta(OUTLOOK_ATTS[1:2])},
    ]

    def fake(conv, **kw):
        seen.update(kw)
        return thread, False
    monkeypatch.setattr(ga.graph, "get_conversation_messages", fake)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ga.cmd_get_conversation(SimpleNamespace(conversation_id=CONV, top=50, max_chars=2000,
                                                full=False, include_drafts=False))
    out = json.loads(buf.getvalue())
    assert seen["with_attachments"] is True
    first, second = out["messages"]
    assert first["has_images"] is True and len(first["attachments"]) == 2
    assert "has_images" not in second and second["attachments"][0]["small"] is True
    assert out["images_hint"].startswith("1 message(s) here carry an image")


def test_graph_client_lists_metadata_and_expands_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "graph_get", lambda ep, params=None, **kw: calls.append((ep, params)) or {"value": []})
    gc.get_attachment_list(MID)
    assert calls[-1] == (f"/me/messages/{MID}/attachments", {"$select": gc.ATTACHMENT_META_FIELDS})
    assert "contentBytes" not in gc.ATTACHMENT_META_FIELDS and "isInline" in gc.ATTACHMENT_META_FIELDS
    gc.get_conversation_messages(CONV)
    assert "$expand" not in calls[-1][1]
    gc.get_conversation_messages(CONV, with_attachments=True)
    assert calls[-1][1]["$expand"] == f"attachments($select={gc.ATTACHMENT_META_FIELDS})"
    with pytest.raises(ValueError):
        gc.get_attachment_list(MID + "/send")


# ── Gmail: inline parts and their flags ───────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _gmail_message():
    """multipart/related with a body that shows a picture by Content-ID
    and names no file for it, beside an attached photo and a PDF."""
    return {"id": "18f0a1b2c3d4e5f6", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/related", "filename": "", "parts": [
            {"mimeType": "text/html", "filename": "",
             "body": {"data": _b64url(b'<p>Schedule:</p><img src="cid:ii_sched01">')}},
            {"mimeType": "image/png", "filename": "", "partId": "0.1",
             "headers": [{"name": "Content-ID", "value": "<ii_sched01>"},
                         {"name": "Content-Disposition", "value": "inline"}],
             "body": {"attachmentId": "ANGjdJ_inline_1", "size": 41000}}]},
        {"mimeType": "image/jpeg", "filename": "site-photo.jpg",
         "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="site-photo.jpg"'}],
         "body": {"attachmentId": "ANGjdJ_photo_2", "size": 90000}},
        {"mimeType": "application/pdf", "filename": "agenda.pdf",
         "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="agenda.pdf"'}],
         "body": {"attachmentId": "ANGjdJ_pdf_3", "size": 51000}},
    ]}}


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _GmailService:
    def __init__(self, message, store):
        self.message, self.store, self.fetched = message, store, []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId=None, id=None, format=None, messageId=None):
        if messageId is not None:           # attachments().get
            self.fetched.append(id)
            return _Exec({"data": self.store[id]})
        return _Exec(self.message)

    def attachments(self):
        return self


GMAIL_STORE = {"ANGjdJ_inline_1": _b64url(PNG), "ANGjdJ_photo_2": _b64url(b"jpegbytes"),
               "ANGjdJ_pdf_3": _b64url(PDF)}


def test_gmail_lists_an_inline_image_with_no_file_name(monkeypatch):
    svc = _GmailService(_gmail_message(), GMAIL_STORE)
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, interactive=False: svc)
    out = gmail_client.list_attachments("owner@example.com", "18f0a1b2c3d4e5f6")
    rows = out["attachments"]
    assert [r["attachment_id"] for r in rows] == ["ANGjdJ_inline_1", "ANGjdJ_photo_2", "ANGjdJ_pdf_3"]
    inline = rows[0]
    assert inline["filename"] == "" and inline["is_inline"] is True and inline["is_image"] is True
    assert inline["content_id"] == "ii_sched01"
    assert rows[1]["is_inline"] is False and rows[1]["is_image"] is True
    assert rows[2]["is_image"] is False
    # the text part of the body is still no attachment
    assert all(r["mime_type"] != "text/html" for r in rows)
    dest = Path(os.environ["EA_DIR"]) / "state" / "sched.png"
    got = gmail_client.download_attachment("owner@example.com", "18f0a1b2c3d4e5f6", str(dest),
                                           attachment_id="ANGjdJ_inline_1")
    assert "error" not in got and dest.read_bytes() == PNG


# ── describe-attachment ───────────────────────────────────────────────

def _describe(*argv):
    args = mail_actions.build_parser().parse_args(["describe-attachment", *argv])
    args._mediated = False
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    return rc, json.loads(buf.getvalue())


@pytest.fixture
def outlook(monkeypatch):
    fetched = []
    blobs = {"AAMkATT1=": PNG, "AAMkATT2=": PNG, "AAMkATT3=": PNG, "AAMkATT4=": PDF}
    monkeypatch.setattr(gc, "get_attachment_list", lambda mid: _meta(OUTLOOK_ATTS))
    monkeypatch.setattr(gc, "download_attachment", lambda mid, aid: fetched.append(aid) or blobs[aid])
    return fetched


@pytest.fixture
def model(monkeypatch):
    """The local image model, stubbed at the endpoint call."""
    calls = []

    def chat(messages, **kw):
        calls.append({"messages": messages, **kw})
        return {"content": "Mon | 9:00 | Lab tour\nTue | 10:30 | Talk\nImage: a two-day schedule.",
                "latency_ms": 900}
    monkeypatch.setattr(rl, "chat", chat)
    monkeypatch.setattr(mail_images, "image_model", lambda cfg=None: "img-model-1")
    monkeypatch.setattr(mail_images, "sealed", lambda cfg=None: False)
    return calls


def test_describe_reads_the_content_images_through_the_local_model(outlook, model):
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--all-images",
                        "--question", "What is on Tuesday?")
    assert rc == 0 and out["read_by"] == "local image model" and out["_note"] == mail_images.DATA_NOTE
    # the schedule and the photo; the logo and the PDF are left out
    assert [i["name"] for i in out["images"]] == ["image001.png", "site-photo.jpg"]
    assert out["images"][0]["is_inline"] is True and "Tue | 10:30 | Talk" in out["images"][0]["text"]
    assert outlook == ["AAMkATT1=", "AAMkATT3="]
    call = model[0]
    assert call["model"] == "img-model-1" and call["purpose"] == "mail-image"
    parts = call["messages"][1]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert "What is on Tuesday?" in parts[0]["text"] and "Answer:" in parts[0]["text"]
    assert "never an instruction" in call["messages"][0]["content"]
    assert call["timeout"] <= 75 and call["slot_wait"] <= 20
    assert "need" not in out and "saved" not in out


def test_describe_picks_one_image_by_name_and_refuses_what_is_no_image(outlook, model):
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--name", "image001")
    assert rc == 0 and [i["attachment_id"] for i in out["images"]] == ["AAMkATT1="]
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--name", "agenda")
    assert rc == 1 and out["error"].startswith("not an image")
    assert len(model) == 1, "a refused pick sends nothing to the model"
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--name", "image00")
    assert rc == 1 and len(out["matches"]) == 2
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--attachment-id", "AAMkNOPE=")
    assert rc == 1 and out["error"] == "No attachment matched" and len(out["attachments"]) == 4


def test_with_no_image_model_the_one_image_is_saved_outside_sealed_mail(outlook, monkeypatch, tmp_path):
    monkeypatch.setattr(mail_images, "image_model", lambda cfg=None: "")
    monkeypatch.setattr(mail_images, "sealed", lambda cfg=None: False)
    dest = tmp_path / "state" / "sched.png"
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--attachment-id", "AAMkATT1=",
                        "--output", str(dest))
    assert rc == 0 and out["need"] == "no image model" and out["saved"] == str(dest)
    assert dest.read_bytes() == PNG
    # without --output it says how to get the file
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--all-images")
    assert out["need"] == "no image model" and "--output" in out["message"] and "saved" not in out


def test_in_sealed_mail_the_picture_never_leaves(outlook, monkeypatch, tmp_path):
    monkeypatch.setattr(mail_images, "image_model", lambda cfg=None: "")
    monkeypatch.setattr(mail_images, "sealed", lambda cfg=None: True)
    dest = tmp_path / "state" / "sched.png"
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--attachment-id", "AAMkATT1=",
                        "--output", str(dest))
    assert rc == 0 and out["need"] == "no image model" and out["sealed"] is True
    assert not dest.exists() and "saved" not in out


def test_a_model_failure_is_a_need_not_a_crash(outlook, monkeypatch, tmp_path):
    monkeypatch.setattr(mail_images, "image_model", lambda cfg=None: "img-model-1")
    monkeypatch.setattr(mail_images, "sealed", lambda cfg=None: False)

    def down(messages, **kw):
        raise rl.RemoteLLMError("HTTP 400: model does not take images")
    monkeypatch.setattr(rl, "chat", down)
    dest = tmp_path / "state" / "a.png"
    rc, out = _describe("--provider", "outlook", "--message-id", MID, "--name", "image001",
                        "--output", str(dest))
    assert rc == 0 and out["need"] == "the image model did not answer"
    assert "does not take images" in out["images"][0]["error"] and dest.read_bytes() == PNG


def test_describe_on_gmail_reads_the_nameless_inline_image(monkeypatch, model):
    svc = _GmailService(_gmail_message(), GMAIL_STORE)
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, interactive=False: svc)
    rc, out = _describe("--provider", "gmail", "--account", "owner@example.com",
                        "--message-id", "18f0a1b2c3d4e5f6", "--all-images")
    assert rc == 0 and out["account"] == "owner@example.com"
    assert [i["attachment_id"] for i in out["images"]] == ["ANGjdJ_inline_1", "ANGjdJ_photo_2"]
    assert out["images"][0]["is_inline"] is True and out["images"][0]["text"].startswith("Mon | 9:00")
    assert "_part" not in json.dumps(out)
    assert svc.fetched == ["ANGjdJ_inline_1", "ANGjdJ_photo_2"]


def test_the_gmail_read_says_a_message_carries_an_image(monkeypatch):
    svc = _GmailService(_gmail_message(), GMAIL_STORE)
    monkeypatch.setattr(gmail_client, "_get_service", lambda email, interactive=False: svc)
    args = mail_actions.build_parser().parse_args(
        ["get-message", "--provider", "gmail", "--account", "owner@example.com",
         "--message-id", "18f0a1b2c3d4e5f6"])
    args._mediated = False
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert args.func(args) == 0
    out = json.loads(buf.getvalue())
    assert out["attachments"][0]["is_inline"] is True
    assert "--provider gmail --account owner@example.com --message-id 18f0a1b2c3d4e5f6" in out["images_hint"]


# ── the local image model ─────────────────────────────────────────────

def test_the_image_model_comes_from_the_config_or_the_model_name():
    base = {"endpoint": "https://llm.example.edu/v1", "image_model": None}
    assert rl.image_model(dict(base, model="qwen3.8-mm")) == "qwen3.8-mm"
    assert rl.image_model(dict(base, model="Qwen2.5-VL-72B-Instruct")) == "Qwen2.5-VL-72B-Instruct"
    assert rl.image_model(dict(base, model="llama-3.2-11b-vision")) == "llama-3.2-11b-vision"
    assert rl.image_model(dict(base, model="text-only-27b")) == ""
    assert rl.image_model(dict(base, model="summary-model")) == ""      # "mm" inside a word
    assert rl.image_model(dict(base, model="text-only-27b", image_model="pix-1")) == "pix-1"
    assert rl.image_model(dict(base, model="qwen3.8-mm", image_model="")) == ""
    assert rl.image_model(dict(base, endpoint="", model="qwen3.8-mm")) == ""
    assert rl.image_model(rl._off()) == ""


def test_the_config_reads_image_model(monkeypatch, tmp_path):
    import owner_trust
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    for raw, want in ((None, "qwen3.8-mm"), (False, ""), ("off", ""), ("pix-2", "pix-2")):
        cfg = {"endpoint": "https://llm.example.edu/v1", "model": "qwen3.8-mm"}
        if raw is not None:
            cfg["image_model"] = raw
        owner_trust.write_owner_file(priv / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
        assert rl.image_model() == want, raw
    monkeypatch.setattr(rl, "_owner", lambda: False)
    assert rl.image_model() == "", "a resident process never reaches the endpoint"


def test_an_image_read_has_its_own_breaker_and_counts_text_only():
    names = {}
    import retry_policy

    class B:
        def __init__(self, name, **kw):
            names["name"] = name
    real = retry_policy.CircuitBreaker
    retry_policy.CircuitBreaker = B
    try:
        rl._breaker("mail-image")
        assert names["name"] == "remote-llm-image"
        rl._breaker("mail-extract")
        assert names["name"] == "remote-llm"
    finally:
        retry_policy.CircuitBreaker = real
    content = [{"type": "text", "text": "Read this image."},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}]
    assert rl._content_chars(content) == len("Read this image.")
    assert rl._content_chars("abc") == 3


# ── the mediator ──────────────────────────────────────────────────────

def test_the_verb_is_a_mediated_read_with_its_output_confined(monkeypatch, tmp_path):
    monkeypatch.setattr(MM, "EA_DIR", tmp_path)
    (tmp_path / "state").mkdir()
    assert MM.is_safe("mail_actions", "describe-attachment")
    assert not MM.is_gated("mail_actions", "describe-attachment")
    assert "--output" in MM.OUTPUT_FLAGS
    ok = ["describe-attachment", "--provider", "outlook", "--message-id", MID,
          "--attachment-id", "AAMkATT1=", "--question", "What is on Tuesday?",
          "--output", str(tmp_path / "state" / "a.png")]
    assert MM.sanitize("mail_actions", "describe-attachment", ok) is None
    assert MM.sanitize("mail_actions", "describe-attachment",
                       ["describe-attachment", "--provider", "gmail", "--message-id", "18f0",
                        "--all-images"]) is None
    outside = ok[:-1] + ["/home/someone/.bashrc"]
    assert "outside ea-bot dirs" in MM.sanitize("mail_actions", "describe-attachment", outside)
    abbreviated = ok[:-2] + ["--outp", str(tmp_path / "state" / "a.png")]
    assert MM.sanitize("mail_actions", "describe-attachment", abbreviated)
    bad_id = ["describe-attachment", "--provider", "outlook", "--message-id", MID,
              "--attachment-id", "AAMk/../send"]
    assert "plain mail id" in MM.sanitize("mail_actions", "describe-attachment", bad_id)
    # not a draft verb: no draft lock
    assert MM.draft_lock_scope("mail_actions", "describe-attachment", ok) is None
    # it waits on a model, so it runs on a thread of its own
    assert ("mail_actions", "describe-attachment") in mail_mediator.ASYNC_VERBS
    # a Codex ghost runs it as a read
    assert cg.is_ghost_read("mail_actions", "describe-attachment")


def test_a_cron_lane_reads_no_image_of_a_kept_thread(monkeypatch):
    argv = ["describe-attachment", "--provider", "outlook", "--message-id", MID, "--all-images"]
    assert mp.as_graph_read("mail_actions", "describe-attachment", argv) == \
        ("graph_actions", "download-attachment")
    index = {"by_id": {MID: {"id": MID, "conversation_id": CONV, "keep_from_anthropic": "domain"}},
             "by_conv": {CONV: [{"id": MID, "keep_from_anthropic": "domain"}]}, "by_key": {}}
    monkeypatch.setattr(mp, "load_index", lambda: index)
    monkeypatch.setattr(mp.remote_llm, "enforcing", lambda part="mail", cfg=None: True)
    # The files of a kept message stay on the machine for every lane, a
    # room's turn too (keep levels: files are read only at full).
    assert mp.release_precheck("mail_actions", "describe-attachment", argv, "cron-brief-1") == mp.FILES_REFUSAL
    assert mp.release_precheck("mail_actions", "describe-attachment", argv, "room-run-1") == mp.FILES_REFUSAL
    assert mp.release_precheck("mail_actions", "get-message", ["get-message", "--provider", "outlook",
                                                               "--message-id", MID], "cron-brief-1") == mp.KEPT_REFUSAL


# ── the sealed record names the image ─────────────────────────────────

class _Graph:
    MAIL_FIELDS = "id"

    def __init__(self, atts):
        self.atts, self.listed = atts, []

    def get_attachments(self, mid):
        self.listed.append(mid)
        return [dict(a) for a in self.atts]

    def get_message_full(self, mid):
        return {"body": {"contentType": "html", "content": HTML}}


def test_the_record_lists_an_inline_image_hasattachments_misses():
    g = _Graph(OUTLOOK_ATTS[:2])
    msg = {"id": MID, "hasAttachments": False}
    atts, _text = mp.attachments_of(msg, g, html=HTML)
    assert g.listed == [MID]
    assert atts[0] == {"name": "image001.png", "type": "image/png", "size": 48210,
                       "inline": True, "image": True}
    # no flag and no cid: no call
    g.listed.clear()
    assert mp.attachments_of(msg, g, html="<p>hi</p>") == ([], "") and g.listed == []
    # a sealed read's row carries the list, and the extractor sees a file
    row = {"id": MID, "conversation_id": CONV, "has_attachments": False, "text": "x",
           "attachments": [{"name": "image001.png"}]}
    assert mp._graph_message(row)["hasAttachments"] is True


@pytest.fixture
def priv(tmp_path, monkeypatch):
    import owner_trust
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
    monkeypatch.setenv("EA_DIR", str(ea))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {"owner@example.edu"})
    monkeypatch.setattr(mp, "_profile_line", lambda: "The user is a professor.")
    monkeypatch.setattr(mp, "_frozen_setup", lambda: None)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "sealed",
           "scheduling": "off", "keep_from_anthropic": {"domains": [], "addresses": [], "admissions": False}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


RECORD = {"sensitivity": "normal", "confidential_kind": "none", "needs_reply": False,
          "ask": "", "deadline": "", "deadline_text": "", "scheduling": "none",
          "offered_times": [], "people": [], "tone": [], "state": "fyi",
          "domain": "professional", "summary": "Pat sends the visit schedule as a picture."}


def test_the_sealed_read_and_the_thread_brief_name_the_image(priv):
    msg = {"id": MID, "conversationId": CONV, "receivedDateTime": "2026-11-06T16:00:00Z",
           "subject": "Visit", "hasAttachments": False, "isDraft": False,
           "from": {"emailAddress": {"name": "P", "address": "pat@example.org"}},
           "toRecipients": [{"emailAddress": {"address": "owner@example.edu"}}], "ccRecipients": []}
    g = _Graph(OUTLOOK_ATTS[:2])
    rec = mp.process(msg, graph=g, cfg=rl.config(), chat=lambda messages, **kw: (dict(RECORD), {"latency_ms": 5}))
    assert rec["status"] == "ok"
    atts = rec["result"]["attachments"]
    assert [(a["name"], a["inline"], a["image"]) for a in atts] == [
        ("image001.png", True, True), ("image002.png", True, True)]
    rl.append(mp.EXTRACTIONS, rec)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    raw = json.dumps({"id": MID, "subject": "Visit", "from": {"address": "pat@example.org"},
                      "to": [], "cc": [], "received": msg["receivedDateTime"],
                      "body": "SECRET BODY", "body_type": "text", "conversation_id": CONV,
                      "has_attachments": False,
                      "attachments": [{"id": "AAMkATT1=", "name": "image001.png", "is_inline": True,
                                       "is_image": True, "size": 48210}],
                      "images_hint": mail_images.hint("outlook", MID)})
    out, refusal = mp.release_filter("graph_actions", "get-message", ["get-message", "--message-id", MID],
                                     "room-run-1", raw)
    got = json.loads(out)
    assert refusal == "" and "SECRET" not in out and got["sealed"] is True
    assert got["remote_record"]["attachments"][0]["inline"] is True
    assert got["attachments"][0]["is_image"] is True and "describe-attachment" in got["images_hint"]
    brief = mp.thread_brief(CONV)
    assert brief["messages"][0]["has_images"] is True
    assert brief["messages"][0]["attachments"] == ["image001.png", "image002.png"]


# ── Codex chat turns ──────────────────────────────────────────────────

def test_a_codex_chat_turn_keeps_view_image_and_names_the_image_verb(monkeypatch, tmp_path):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    (tmp_path / "private").mkdir(mode=0o700)
    monkeypatch.setattr(ct, "_resident_text", lambda path: "")
    monkeypatch.setattr(ct, "context_files", lambda cwd: [])
    monkeypatch.setattr(ct, "live_write_paths", lambda: [ct.REAL_EA_DIR / "state"])
    monkeypatch.setattr(ct, "_ensure_live_cwd", lambda runner=None: None)
    monkeypatch.setattr(ct, "_apply_staged", lambda text, runner=None: {})
    monkeypatch.setattr(ct, "_prefetch_sites", lambda runner=None: {"status": "ok", "sites": []})
    monkeypatch.setattr(ct, "_prefetch_pages", lambda text, runner=None: [])
    monkeypatch.setattr(cg, "ensure_codex_tmp", lambda runner=None: cg.CODEX_TMP)
    calls = []

    def run(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"output": "ok", "tool_calls": [], "usage": {}, "duration_s": 1, "exit_code": 0,
                "harness_error": "", "files": [], "thread_id": "th-img", "num_turns": 1}
    ct.run_turn("P", thread_key="t-img", run_id="run-img", live=True, fresh_prompt="P", codex_run=run)
    call = calls[0]
    assert "view_image" not in " ".join(map(str, call.get("disable") or ())), \
        "a chat turn opens a saved picture with view_image"
    assert "describe-attachment" in call["prompt"] and "view_image" in call["prompt"]
    # the tool-less calls keep every tool off, the image viewer included
    import hosted_text
    assert "view_image" in hosted_text.CODEX_OFF
