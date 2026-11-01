#!/usr/bin/env python3
"""A send approval is bound to the content the owner reviewed, not to the
draft id alone.

update-draft, attach-file and remove-attachment are SAFE verbs the resident
calls freely, so a draft can change after its Send card is up, or after a
scheduled send is queued. What this file pins:

  1. THE FINGERPRINT  one SHA-256 over To, Cc, Bcc, Subject, body and the
                      attachment list (draft_fingerprint.py), the same on
                      Outlook and Gmail; every one of those fields moves it.
  2. THE SEND VERB    graph_client.send_draft / gmail_client.send_draft read
                      the draft again right before the send and refuse a
                      changed one; an approval with no fingerprint is refused.
  3. THE CLICK        _execute_send compares a fresh owner-side read with
                      the fingerprint the clicked card stored, posts a new
                      card instead of sending on a mismatch, and hands the
                      fingerprint to request-confirm.
  4. THE OWNER ID     with no owner id configured, every click is refused.
  5. THE TRIPWIRE     every SAFE verb in the mediation registry is classified
                      as draft-changing or not; each draft-changing verb is
                      run for real against a fake mailbox after a card is
                      posted and after a send is queued, and the click and
                      the cron both refuse to send. A new SAFE verb that the
                      classification cannot place fails here.
  6. THE LOCK         the send holds the draft's lock from its check to its
                      send, the mediator holds it around each draft-changing
                      verb, and its table names exactly those verbs.
  7. THE CARD         a body the card cannot show in full gets no Send or
                      Schedule button, and a click on one sends nothing; the
                      remote resources the HTML loads are named on the card.
  8. RAW HEADERS      raw To, Cc, Bcc, From and Reply-To values are hashed,
                      so headers that parse alike still differ.

Nothing here touches live state: EA_DIR is a sandbox, and Graph and Gmail
are in-memory fakes.
"""

import argparse
import ast
import functools
import base64
import copy
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import time
import traceback
import types
from contextlib import contextmanager, redirect_stdout
from email import message_from_bytes
from email import policy as email_policy
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))   # the bot's card code imports preprocessor.chat

_SANDBOX = tempfile.mkdtemp(prefix="ea-send-binding-tests-")
os.environ["EA_DIR"] = _SANDBOX
# The draft locks live in the owner-private directory: a sandbox one here.
os.environ["EA_PRIVATE_DIR"] = str(Path(_SANDBOX) / "private")
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ.pop("EA_SLACK_CHANNEL_ID", None)
os.environ.pop("EA_FORCE_MEDIATE", None)

import draft_fingerprint as DFP  # noqa: E402
import draft_lock as DL  # noqa: E402
import graph_client as gc  # noqa: E402
import mail_links as ML  # noqa: E402
import graph_actions as ga  # noqa: E402
import gmail_client as gm  # noqa: E402
import mail_actions as ma  # noqa: E402
import mail_mediation as MM  # noqa: E402
import scheduled_sends as S  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

DRAFT = "AAMkDraftUnderReview="
CONV = "AAQkConversation="
GDRAFT = "r-1234567890"
GMAIL = "owner.example@gmail.com"
WHEN = "2020-01-01T00:00"     # due, so the cron fires it


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _rcpt(addr, name=""):
    return {"emailAddress": {"address": addr, "name": name}}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeOutlook:
    """One Outlook draft in memory, reached through the graph_client
    functions the verbs call. Any route it does not know is a test failure,
    so a verb that grows a new Graph call is loud here."""

    def __init__(self):
        self.msg = {
            "id": DRAFT, "isDraft": True, "subject": "Re: budget",
            "toRecipients": [_rcpt("pat@example.org", "Pat")],
            "ccRecipients": [], "bccRecipients": [],
            "body": {"contentType": "Text", "content": "Numbers by Friday."},
            "conversationId": CONV, "lastModifiedDateTime": "2026-10-19T10:00:00Z",
        }
        self.atts = [{"id": "att1", "name": "a.pdf", "contentType": "application/pdf",
                      "size": 4, "contentBytes": _b64(b"PDF1")}]
        self.deleted = False
        self.sent = []
        self._n = 1
        # The sent and received messages of the draft's conversation.
        self.thread: list = []

    def _message(self):
        if self.deleted:
            raise RuntimeError("404 ErrorItemNotFound")
        m = copy.deepcopy(self.msg)
        m["hasAttachments"] = bool(self.atts)
        return m

    # graph_client surface
    def graph_get(self, path, params=None):
        if path.startswith("/me/mailFolders/drafts/messages"):
            return {"value": [] if self.deleted else
                    [{"id": DRAFT, "subject": self.msg["subject"],
                      "lastModifiedDateTime": self.msg["lastModifiedDateTime"]}]}
        if path == f"/me/messages/{DRAFT}":
            return self._message()
        if path == "/me/messages" and CONV in str((params or {}).get("$filter", "")):
            return {"value": copy.deepcopy(self.thread)}
        raise AssertionError(f"FakeOutlook: unexpected GET {path}")

    def get_message_full(self, message_id):
        return self.graph_get(f"/me/messages/{message_id}")

    def get_attachments(self, message_id):
        assert message_id == DRAFT, message_id
        if self.deleted:
            raise RuntimeError("404 ErrorItemNotFound")
        return copy.deepcopy(self.atts)

    def graph_patch(self, path, body):
        assert path == f"/me/messages/{DRAFT}", path
        if self.deleted:
            raise RuntimeError("404 ErrorItemNotFound")
        self.msg.update(copy.deepcopy(body))
        return self._message()

    def graph_delete(self, path):
        prefix = f"/me/messages/{DRAFT}/attachments/"
        assert path.startswith(prefix), path
        self.atts = [a for a in self.atts if a["id"] != path[len(prefix):]]

    def graph_post(self, path, body):
        if path == f"/me/messages/{DRAFT}/send":
            self.sent.append(copy.deepcopy(self._message()))
            return {}
        raise AssertionError(f"FakeOutlook: unexpected POST {path}")

    def conversation_latest_sender(self, conversation_id):
        return "pat@example.org"

    # requests surface (attach-file posts directly, delete-draft deletes)
    def requests_post(self, url, headers=None, json=None, **kw):
        assert url.endswith(f"/me/messages/{DRAFT}/attachments"), url
        self._n += 1
        raw = base64.b64decode(json["contentBytes"])
        self.atts.append({"id": f"att{self._n}", "name": json["name"],
                          "contentType": json.get("contentType", ""), "size": len(raw),
                          "contentBytes": json["contentBytes"]})
        return types.SimpleNamespace(ok=True, status_code=201, text="")

    def requests_delete(self, url, headers=None, **kw):
        assert url.endswith(f"/me/messages/{DRAFT}"), url
        self.deleted = True
        return types.SimpleNamespace(ok=True, status_code=204, raise_for_status=lambda: None)


def _journal_stub():
    return types.SimpleNamespace(record=lambda *a, **k: None,
                                 known_draft_ids=lambda: {DRAFT})


def _capture_stub():
    return types.SimpleNamespace(capture=lambda *a, **k: None, drop=lambda *a, **k: None,
                                 ids_for_conversation=lambda conv: {DRAFT})


@contextmanager
def outlook(fake=None):
    """graph_client and graph_actions wired to one FakeOutlook."""
    fake = fake or FakeOutlook()
    with patch.multiple(gc, graph_get=fake.graph_get, get_message_full=fake.get_message_full,
                        get_attachments=fake.get_attachments, graph_patch=fake.graph_patch,
                        graph_delete=fake.graph_delete, graph_post=fake.graph_post,
                        conversation_latest_sender=fake.conversation_latest_sender,
                        _api_base=lambda: "https://graph.microsoft.com/v1.0",
                        _headers=lambda: {}), \
         patch("requests.post", fake.requests_post), \
         patch("requests.delete", fake.requests_delete), \
         patch.object(ga, "_draft_events", _journal_stub()), \
         patch.object(ga, "_draft_capture", _capture_stub()):
        yield fake


def _mime(to="Pat <pat@example.org>", subject="Re: budget", body="Numbers by Friday.",
          files=(("a.pdf", b"PDF1"),)) -> bytes:
    m = MIMEMultipart("mixed")
    m["From"], m["To"], m["Subject"] = GMAIL, to, subject
    m.attach(MIMEText(body, "plain", "utf-8"))
    for name, data in files:
        part = MIMEApplication(data, "pdf")
        part.add_header("Content-Disposition", "attachment", filename=name)
        m.attach(part)
    return m.as_bytes()


def _payload(msg) -> dict:
    """The Gmail API's format="full" payload of a parsed message."""
    out = {"mimeType": msg.get_content_type(), "filename": msg.get_filename() or "",
           "headers": [{"name": k, "value": str(v)} for k, v in msg.items()]}
    if msg.is_multipart():
        out["body"] = {"size": 0}
        out["parts"] = [_payload(p) for p in msg.iter_parts()]
    else:
        data = msg.get_payload(decode=True) or b""
        out["body"] = {"size": len(data),
                       "data": base64.urlsafe_b64encode(data).decode("ascii")}
    return out


class FakeGmail:
    """One Gmail draft in memory behind the googleapiclient call chain. An
    update makes a new message id, as Gmail does."""

    def __init__(self):
        self.raw = _mime()
        self.version = 1
        self.deleted = False
        self.sent = []
        # Plain-text bodies of the thread's other messages.
        self.thread_bodies: list = []

    def _message(self, fmt):
        if self.deleted:
            raise RuntimeError("Requested entity was not found")
        msg = {"id": f"m{self.version}", "threadId": "t1"}
        if fmt == "raw":
            msg["raw"] = base64.urlsafe_b64encode(self.raw).decode("ascii")
        else:
            msg["payload"] = _payload(message_from_bytes(self.raw, policy=email_policy.default))
        return msg

    def users(self):
        return self

    def drafts(self):
        fake = self

        class _Drafts:
            def get(self, userId, id, format="full"):
                assert id == GDRAFT, id
                return _Exec(lambda: {"id": GDRAFT, "message": fake._message(format)})

            def update(self, userId, id, body):
                assert id == GDRAFT, id

                def _run():
                    fake.raw = base64.urlsafe_b64decode(body["message"]["raw"] + "==")
                    fake.version += 1
                    return {"id": GDRAFT, "message": {"id": f"m{fake.version}",
                                                      "threadId": "t1"}}
                return _Exec(_run)

            def delete(self, userId, id):
                def _run():
                    fake.deleted = True
                    return {}
                return _Exec(_run)

            def send(self, userId, body):
                def _run():
                    fake.sent.append(fake._message("raw"))
                    return {"id": "sent1", "threadId": "t1"}
                return _Exec(_run)
        return _Drafts()

    def messages(self):
        fake = self

        class _Messages:
            def get(self, userId, id, format="full"):
                assert id == f"m{fake.version}", (id, fake.version)
                return _Exec(lambda: fake._message(format))
        return _Messages()

    def threads(self):
        fake = self

        class _Threads:
            def get(self, userId, id, format="full"):
                assert id == "t1", id
                msgs = [{"id": f"o{i}", "threadId": "t1", "labelIds": ["INBOX"],
                         "payload": _payload(message_from_bytes(
                             _mime(body=b, files=()), policy=email_policy.default))}
                        for i, b in enumerate(fake.thread_bodies)]
                msgs.append({"id": f"m{fake.version}", "threadId": "t1", "labelIds": ["DRAFT"],
                             "payload": fake._message("full")["payload"]})
                return _Exec(lambda: {"id": "t1", "messages": msgs})
        return _Threads()


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


@contextmanager
def gmail(fake=None):
    fake = fake or FakeGmail()
    with patch.object(gm, "_get_service", lambda email, interactive=False: fake), \
         patch.object(gm, "_journal", lambda *a, **k: None):
        yield fake


@contextmanager
def sandbox():
    """Token store and scheduled-send queue in a scratch directory."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir()
        (tmp / "auth").mkdir()
        old = (ga.PENDING_CONFIRMS_FILE, ga.PENDING_CONFIRMS_LOCK,
               S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack)
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        S.SENDS_FILE = tmp / "state" / "scheduled-sends.json"
        S.HMAC_KEY_FILE = tmp / "auth" / "scheduled-send-hmac.key"
        S.HMAC_KEY_FILE.write_bytes(b"0123456789abcdef0123456789abcdef0123456789")
        S.HMAC_KEY_FILE.chmod(0o600)
        posts: list = []
        S._post_slack = posts.append
        try:
            yield tmp, posts
        finally:
            (ga.PENDING_CONFIRMS_FILE, ga.PENDING_CONFIRMS_LOCK,
             S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack) = old


def _out(fn, *a) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return json.loads(buf.getvalue())


def _cli_send(draft_id, fingerprint) -> dict:
    """The send the bot runs on a click: request-confirm's token, then
    send-draft (graph_actions, owner-side)."""
    tok = ga._create_confirmation("send", draft_id, "test", fingerprint=fingerprint)
    return _out(ga.cmd_send_draft, argparse.Namespace(message_id=draft_id, confirm_token=tok))


def _cli_send_gmail(fingerprint) -> dict:
    tok = ga._create_confirmation("send", GDRAFT, "test", fingerprint=fingerprint)
    return _out(ga.cmd_send_gmail_draft,
                argparse.Namespace(email=GMAIL, draft_id=GDRAFT, confirm_token=tok))


def _cron_with_queued(fingerprint) -> dict:
    """One send queued after an owner click on content `fingerprint`, then
    the cron pass."""
    tok = S.make_send_token(DRAFT, WHEN, fingerprint)
    S._save_sends([{"draft_id": DRAFT, "send_after": WHEN, "description": "budget reply",
                    "ledger_id": "", "confirm_token": tok, "fingerprint": fingerprint,
                    "created": "2026-10-19T09:00:00"}])
    return _out(S.cmd_check, None)


# ---------------------------------------------------------------------------
# 1. The fingerprint
# ---------------------------------------------------------------------------

def _outlook_fp(msg, atts):
    return DFP.fingerprint(DFP.outlook_parts(msg, atts))


def test_every_reviewed_field_moves_the_fingerprint():
    base = FakeOutlook()
    fp0 = _outlook_fp(base.msg, base.atts)
    assert fp0.startswith("sha256:") and fp0 == _outlook_fp(copy.deepcopy(base.msg),
                                                            copy.deepcopy(base.atts))
    edits = {
        "to": lambda m, a: m["toRecipients"].append(_rcpt("eve@example.net")),
        "to display name": lambda m, a: m["toRecipients"][0]["emailAddress"].update(name="Boss"),
        "cc": lambda m, a: m["ccRecipients"].append(_rcpt("eve@example.net")),
        "bcc": lambda m, a: m["bccRecipients"].append(_rcpt("eve@example.net")),
        "subject": lambda m, a: m.update(subject="Re: budget (final)"),
        "body": lambda m, a: m["body"].update(content="Numbers by Friday. Wire it to me."),
        "body type": lambda m, a: m["body"].update(contentType="HTML"),
        "attachment bytes": lambda m, a: a[0].update(contentBytes=_b64(b"PDF2")),
        "attachment name": lambda m, a: a[0].update(name="b.pdf"),
        "attachment added": lambda m, a: a.append(dict(a[0], id="att9")),
        "attachment removed": lambda m, a: a.clear(),
    }
    for label, edit in edits.items():
        m, a = copy.deepcopy(base.msg), copy.deepcopy(base.atts)
        edit(m, a)
        assert _outlook_fp(m, a) != fp0, f"{label} does not move the fingerprint"


def test_order_and_ids_that_do_not_change_content_do_not_move_it():
    m = FakeOutlook().msg
    m["toRecipients"] = [_rcpt("a@example.org"), _rcpt("b@example.org")]
    a = [{"id": "x1", "name": "a.pdf", "contentType": "application/pdf", "size": 4,
          "contentBytes": _b64(b"PDF1")}]
    fp = _outlook_fp(m, a)
    m2 = copy.deepcopy(m)
    m2["toRecipients"].reverse()
    a2 = [dict(a[0], id="x2")]   # the same file attached again
    assert _outlook_fp(m2, a2) == fp


def test_gmail_fingerprint_covers_the_same_fields():
    fp0 = DFP.fingerprint(DFP.gmail_parts(_mime()))
    assert fp0 == DFP.fingerprint(DFP.gmail_parts(_mime()))
    for label, raw in {"to": _mime(to="Eve <eve@example.net>"),
                       "subject": _mime(subject="Re: other"),
                       "body": _mime(body="Wire it to me."),
                       "attachment bytes": _mime(files=(("a.pdf", b"PDF2"),)),
                       "attachment name": _mime(files=(("b.pdf", b"PDF1"),)),
                       "no attachment": _mime(files=())}.items():
        assert DFP.fingerprint(DFP.gmail_parts(raw)) != fp0, label
    m = message_from_bytes(_mime(), policy=email_policy.default)
    m["Bcc"] = "eve@example.net"
    assert DFP.fingerprint(DFP.gmail_parts(m.as_bytes())) != fp0, "bcc"


# ---------------------------------------------------------------------------
# 2. The send verbs
# ---------------------------------------------------------------------------

def test_an_unchanged_outlook_draft_sends():
    with sandbox(), outlook() as fake:
        fp = gc.draft_fingerprint(DRAFT)
        out = _cli_send(DRAFT, fp)
        assert out.get("status") == "sent", out
        assert len(fake.sent) == 1


def test_a_changed_outlook_draft_is_refused_by_the_send_verb():
    for label, edit in {
        "recipients": lambda f: f.msg["toRecipients"].append(_rcpt("eve@example.net")),
        "body": lambda f: f.msg["body"].update(content="Wire it to me."),
        "attachment": lambda f: f.atts[0].update(contentBytes=_b64(b"EVIL")),
    }.items():
        with sandbox(), outlook() as fake:
            fp = gc.draft_fingerprint(DRAFT)
            edit(fake)
            out = _cli_send(DRAFT, fp)
            assert out.get("status") == "changed", (label, out)
            assert fake.sent == [], f"a draft with changed {label} was sent"


def test_a_send_without_a_fingerprint_is_refused():
    with sandbox(), outlook() as fake:
        assert "BLOCKED" in _cli_send(DRAFT, "").get("error", "")
        assert gc.send_draft(DRAFT, confirm_token=gc.issue_inprocess_approval(
            "send", DRAFT))["status"] == "blocked"
        assert fake.sent == []
        # request-confirm will not mint a send token without one
        out = _out(ga.cmd_request_confirm, argparse.Namespace(
            action="send", message_id=DRAFT, description="", fingerprint=""))
        assert "error" in out and "token" not in out


def test_gmail_unchanged_sends_and_changed_is_refused():
    with sandbox(), gmail() as fake:
        fp = gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True)["fingerprint"]
        assert _cli_send_gmail(fp).get("status") == "sent" and len(fake.sent) == 1
    with sandbox(), gmail() as fake:
        fp = gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True)["fingerprint"]
        fake.raw, fake.version = _mime(to="Eve <eve@example.net>"), fake.version + 1
        out = _cli_send_gmail(fp)
        assert out.get("status") == "changed", out
        assert fake.sent == []
        assert gm.send_draft(GMAIL, GDRAFT)["status"] == "blocked"


def test_a_scheduled_send_with_a_changed_draft_is_dropped():
    with sandbox() as (_tmp, posts), outlook() as fake:
        fp = gc.draft_fingerprint(DRAFT)
        fake.msg["body"]["content"] = "Wire it to me."
        out = _cron_with_queued(fp)
        assert out["sent"] == 0 and out["refused"] == 1 and out["remaining"] == 0
        assert fake.sent == [] and S._load_sends() == []
        assert any("changed after you approved it" in p for p in posts), posts
    with sandbox() as (_tmp, posts), outlook() as fake:
        out = _cron_with_queued(gc.draft_fingerprint(DRAFT))
        assert out["sent"] == 1 and len(fake.sent) == 1, (out, posts)


# ---------------------------------------------------------------------------
# 3. The click (ea-chat-bot.py)
# ---------------------------------------------------------------------------

def _bot_block(start: str, end: str) -> str:
    i = BOT_SRC.index(start)
    return BOT_SRC[i:BOT_SRC.index(end, i)]


def _fn_src(name: str) -> str:
    i = BOT_SRC.index(f"\ndef {name}(")
    return BOT_SRC[i:BOT_SRC.index("\ndef ", i + 1)]


class _Recorder:
    def __init__(self):
        self.statuses, self.cards, self.posts, self.marks = [], [], [], []


def _frontend_stub(rec=None, body_chars=2800):
    """A frontend whose one card shows `body_chars` characters of body and
    quoted thread together."""
    return types.SimpleNamespace(
        post=lambda **k: rec.posts.append(k) if rec is not None else None,
        card_fits=lambda card: len(card.body or "") + len(card.quoted or "") <= body_chars,
        limits=types.SimpleNamespace(card_body_chars=body_chars))


def _exec_cards(ns: dict) -> dict:
    """The bot's Send and Schedule card builders and their helpers."""
    from preprocessor import chat as _chat
    import dataclasses
    ns.setdefault("_chat", _chat)
    ns.setdefault("dataclasses", dataclasses)
    exec(compile(_bot_block("def _draft_fallback(", "\ndef _bulk_card("),
                 "ea-chat-bot.py[cards]", "exec"), ns)
    return ns


def _click_ns(current_fp: str, send_status: str = "sent", body: str = "b"):
    """_execute_send and _recard_changed_draft out of the bot source, with
    the draft read, the CLI calls and the chat stubbed."""
    rec = _Recorder()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        verb = cmd[2]
        out = {"preflight-check": {"ok": True, "errors": [], "warnings": []},
               "request-confirm": {"token": "TOK"},
               "send-draft": {"status": send_status},
               "send-gmail-draft": {"status": send_status}}[verb]
        return types.SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)

    ns = {
        "json": json, "SCRIPT_DIR": ROOT, "CHANNEL_ID": "C1",
        "log": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None,
                                     exception=lambda *a, **k: None),
        "FRONTEND": _frontend_stub(rec),
        "_replace_buttons_with_status": lambda ch, ts, text: rec.statuses.append((ts, text)),
        "_fetch_draft_body": lambda did, **kw: {"to": "pat@example.org", "subject": "Re: x",
                                                "body": body, "fingerprint": current_fp},
        "_mark_draft_confirmation": lambda did, status: rec.marks.append(status),
        "_post_send_confirmation": lambda *a, **k: rec.cards.append(("send", a, k)),
        "_post_schedule_send_confirmation": lambda *a, **k: rec.cards.append(("schedule", a)),
        "_close_ledger_for_draft": lambda *a, **k: None,
        "_with_closed": lambda status, closed: status,
        "_file_admissions_after_send": lambda *a, **k: None,
        "_send_error_is_missing": lambda e: False,
    }
    _exec_cards(ns)
    exec(compile(_bot_block("_CHANGED_DRAFT_NOTE = (", "\n# Deferred sends"),
                 "ea-chat-bot.py[send]", "exec"), ns)
    return ns, rec, calls, fake_run


def _click(ns, fake_run, fingerprint, **kw):
    with patch("subprocess.run", side_effect=fake_run):
        return ns["_execute_send"]("C1", "T1", "111.1", DRAFT, dm_channel="DM1",
                                   fingerprint=fingerprint, **kw)


def test_click_on_an_unchanged_draft_sends_with_the_fingerprint():
    ns, rec, calls, run = _click_ns("sha256:aa")
    assert _click(ns, run, "sha256:aa") is True
    confirm = next(c for c in calls if c[2] == "request-confirm")
    assert "--fingerprint=sha256:aa" in confirm
    assert any(c[2] == "send-draft" for c in calls) and rec.cards == []


def test_click_on_a_changed_draft_recards_and_sends_nothing():
    for system in ("outlook", "gmail"):
        ns, rec, calls, run = _click_ns("sha256:bb")
        assert _click(ns, run, "sha256:aa", email_system=system,
                      gmail_email=GMAIL if system == "gmail" else "") is False
        assert calls == [], f"{system}: a token was minted for changed content"
        assert rec.statuses and "changed after the card" in rec.statuses[-1][1]
        assert rec.cards and rec.cards[0][0] == "send", "no fresh card for the new content"


def test_click_on_a_card_with_no_stored_fingerprint_recards():
    """A card posted before content binding has no fingerprint: never send."""
    ns, rec, calls, run = _click_ns("sha256:aa")
    assert _click(ns, run, "") is False
    assert calls == [] and rec.cards


def test_a_change_between_the_click_and_the_send_recards():
    ns, rec, calls, run = _click_ns("sha256:aa", send_status="changed")
    assert _click(ns, run, "sha256:aa") is False
    assert rec.cards and "consumed" not in rec.marks


def test_the_card_and_the_registry_carry_the_fingerprint():
    assert 'content_fingerprint=draft.get("fingerprint", "")' in _fn_src("_send_card")
    assert 'content_fingerprint=draft.get("fingerprint", "")' in _fn_src("_schedule_card")
    assert 'fingerprint=getattr(card, "content_fingerprint"' in _fn_src("_post_send_confirmation")
    assert "_set_draft_fingerprint(" in _fn_src("_refresh_send_confirmation")
    assert "_set_draft_fingerprint(" in _fn_src("_apply_draft_edit")
    assert 'fingerprint=draft.get("fingerprint"' in _fn_src("_post_schedule_send_confirmation")
    handler = BOT_SRC[BOT_SRC.index('@app.action("approve_send")'):]
    handler = handler[:handler.index("\n@app.action(", 1)]
    assert "fingerprint=fingerprint" in handler and 'claimed.get("fingerprint")' in handler
    bulk = BOT_SRC[BOT_SRC.index('@app.action("approve_bulk_send")'):]
    bulk = bulk[:bulk.index("\n@app.", 1)]
    # the Send-all card sends a draft only with the fingerprint it listed
    assert 'listed.get(draft_id) != fingerprint' in bulk and "fingerprint=fingerprint" in bulk
    # the schedule click reads the registry row and checks it first
    sched = _fn_src("_execute_schedule_send")
    assert sched.index('get("fingerprint")') < sched.index('"request-confirm"')
    assert "--fingerprint={fingerprint}" in sched


def test_the_registry_stores_and_updates_the_fingerprint():
    import threading
    try:
        import atomic_io as _atomic
    except ModuleNotFoundError:
        from preprocessor import atomic_io as _atomic
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir()
        ns = {"json": json, "os": os, "time": time, "_atomic": _atomic,
              "threading": threading, "EA_DIR": tmp, "MAX_CONFIRM_AGE_HOURS": 48,
              "_private_state": lambda name: tmp / "state" / name,
              "_queue_bulk_candidate": lambda *a, **k: None,
              "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                           debug=lambda *a, **k: None,
                                           warning=lambda *a, **k: None)}
        exec(compile(_bot_block("_DRAFT_CONFIRMS = _private_state(", "\ndef _send_error_is_missing"),
                     "ea-chat-bot.py[registry]", "exec"), ns)
        ns["_note_draft_confirmation"](DRAFT, "DM1", "111.1", "C1", "T1", fingerprint="sha256:aa")
        ns["_set_draft_fingerprint"](DRAFT, "999.9", "sha256:zz")     # another card: no change
        assert ns["_load_draft_confirms"]()[DRAFT]["fingerprint"] == "sha256:aa"
        ns["_set_draft_fingerprint"](DRAFT, "111.1", "sha256:bb")     # this card re-rendered
        claimed = ns["_claim_draft_for_send"](DRAFT)
        assert claimed["fingerprint"] == "sha256:bb" and claimed["message_ts"] == "111.1"


# ---------------------------------------------------------------------------
# 4. The owner id
# ---------------------------------------------------------------------------

def _verify_ns(owner):
    rec = _Recorder()
    ns = {"OWNER_SLACK_USER_ID": owner, "time": time, "FRONTEND": _frontend_stub(rec),
          "log": types.SimpleNamespace(error=lambda *a, **k: None,
                                       warning=lambda *a, **k: None)}
    exec(compile(_fn_src("_verify_button_user"), "ea-chat-bot.py[owner]", "exec"), ns)
    exec(compile(_bot_block("_OWNERLESS_NOTICE_S = ", "\ndef _clicked_card"),
                 "ea-chat-bot.py[ownerless]", "exec"), ns)
    return ns, rec


def _click_ev(user, channel="D_CARD"):
    return types.SimpleNamespace(user_id=user,
                                 card=types.SimpleNamespace(channel=channel, id="111.1"))


def test_slack_with_no_owner_id_refuses_every_click():
    ns, _rec = _verify_ns(None)
    for user in ("U_FIRST", "U_SECOND", ""):
        assert ns["_verify_button_user"](_click_ev(user)) is False
    assert ns["OWNER_SLACK_USER_ID"] is None, "a click must not make its clicker the owner"
    ns, rec = _verify_ns("U_OWNER")
    assert ns["_verify_button_user"](_click_ev("U_OWNER")) is True
    assert ns["_verify_button_user"](_click_ev("U_OTHER")) is False
    assert ns["_verify_button_user"](_click_ev("")) is False
    assert rec.posts == [], "a configured owner id posts no owner-id notice"


def test_a_click_refused_for_no_owner_id_is_visible_where_the_card_is():
    ns, rec = _verify_ns(None)
    assert ns["_verify_button_user"](_click_ev("U_FIRST", "D_CARD")) is False
    assert len(rec.posts) == 1, rec.posts
    post = rec.posts[0]
    assert post["channel"] == "D_CARD"
    assert "refused" in post["text"] and "slack.owner_user_id" in post["text"]
    # a burst of clicks gets one notice per conversation, not one per click
    ns["_verify_button_user"](_click_ev("U_FIRST", "D_CARD"))
    assert len(rec.posts) == 1
    ns["_verify_button_user"](_click_ev("U_FIRST", "C_OTHER"))
    assert [p["channel"] for p in rec.posts] == ["D_CARD", "C_OTHER"]


# ---------------------------------------------------------------------------
# 5. The tripwire: every SAFE verb that can change a draft
# ---------------------------------------------------------------------------

# What, in a verb's own code or in a function it calls, says it writes to an
# existing draft: the journal record the card refresh follows, the Gmail
# draft rewrite, or the draft-only guard the in-place draft verbs share.
DRAFT_WRITE_MARKERS = ('_draft_events.record("updated"', '_draft_events.record("deleted"',
                       '_journal("updated"', '_journal("deleted"',
                       "drafts().update(", "drafts().delete(", "_not_a_draft(")

# SAFE verbs on the mail clients that do NOT change an existing draft, with
# the reason. Every SAFE mail verb is either detected by the markers or
# listed here; a new verb that is neither fails the test until someone
# decides which it is.
NOT_DRAFT_CHANGING = {
    "graph_actions": {
        "search-mail": "read", "list-mail": "read", "get-message": "read",
        "get-conversation": "read", "get-event": "read", "calendar-view": "read",
        "resolve-contact": "read", "get-attachments": "read",
        "download-attachment": "read; writes a local file",
        "scan-attachments": "read", "get-gmail-draft": "read", "preflight-check": "read",
        "create-draft": "makes a new draft id, which gets its own card",
        "archive-message": "moves received mail; content unchanged",
        "archive-conversation": "moves received mail; content unchanged",
        "snooze-message": "moves received mail; content unchanged",
        "mark-read": "the read flag only",
        "set-categories": "a mailbox label; not part of what is sent",
        "wake-snoozed": "moves received mail back to the inbox",
        "create-todo": "a To Do task, not mail",
        "clear-hold": "deletes an EA calendar hold, not mail",
    },
    "gmail_client": {
        "search": "read", "get-message": "read", "get-thread": "read", "get-draft": "read",
        "mark-read": "the read flag only",
        "list-attachments": "read", "download-attachment": "read; writes a local file",
        "list-drafts": "read",
        "create-draft": "makes a new draft id, which gets its own card",
    },
    "mail_actions": {
        "search": "read", "get-message": "read", "get-draft": "read",
        "mark-read": "the read flag only",
    },
}

# The other SAFE tools never reach a draft; their module source must not
# hold a draft write either.
NON_MAIL_TOOLS = {
    "google_calendar_client": "preprocessor/google_calendar_client.py",
    "scheduling_engine": "preprocessor/scheduling_engine.py",
    "deck_serve": "preprocessor/deck_serve.py",
    "email_writer": "preprocessor/email_writer.py",
    "plugin_cards": "preprocessor/plugin_cards.py",
}
NON_MAIL_DRAFT_MARKERS = ("update-draft", "update_draft(", "attach-file", "attach_file(",
                          "remove-attachment", "drafts().update(", "graph_patch(")

MAIL_MODULES = {"graph_actions": ga, "gmail_client": gm, "mail_actions": ma}


def _parser(module) -> argparse.ArgumentParser:
    """The CLI's own parser (its build_parser())."""
    return module.build_parser()


def _subparsers(parser) -> dict:
    return next(a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction)).choices


PARSERS = {
    "graph_actions": lambda: _parser(ga),
    "gmail_client": lambda: _parser(gm),
    "mail_actions": lambda: _parser(ma),
}


@functools.lru_cache(maxsize=None)
def _module_functions(module) -> dict:
    """{name: source} of a module's top-level functions, parsed once and
    sliced by line number (ast.get_source_segment re-splits the whole file
    on each call, which is quadratic on Python 3.10)."""
    src = Path(module.__file__).read_text()
    lines = src.splitlines(keepends=True)
    return {n.name: "".join(lines[n.lineno - 1:n.end_lineno])
            for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}


def _reach(seed_src: str, funcs: dict, also: dict) -> str:
    """seed_src plus every function it reaches by name in its own module,
    and graph_client functions it calls as graph.<name>."""
    seen, todo, text = set(), [seed_src], []
    while todo:
        s = todo.pop()
        text.append(s)
        for name in set(re.findall(r"\b([A-Za-z_]\w*)\(", s)):
            if name in funcs and name not in seen:
                seen.add(name)
                todo.append(funcs[name])
        for name in set(re.findall(r"\bgraph\.([A-Za-z_]\w*)\(", s)):
            key = "graph." + name
            if name in also and key not in seen:
                seen.add(key)
                todo.append(also[name])
    return "\n".join(text)


def _verb_code(tool: str, verb: str) -> str:
    """The code a verb runs: its handler and everything it reaches."""
    module = MAIL_MODULES[tool]
    funcs = _module_functions(module)
    gfuncs = _module_functions(gc)
    if tool != "gmail_client":
        handler = _subparsers(PARSERS[tool]())[verb].get_default("func")
        seed = funcs[handler.__name__]
    else:
        main = funcs["main"]
        m = re.search(r'args\.command == "' + re.escape(verb) + r'":\n(.*?)(?=\n    elif |\n    else:)',
                      main, re.S)
        assert m, f"gmail_client main() has no branch for {verb}"
        seed = m.group(1)
    return _reach(seed, funcs, gfuncs)


def draft_changing_verbs() -> dict:
    """{tool: {verb, ...}} of SAFE mail verbs that can change a draft,
    from the mediation registry and the verbs' own code."""
    out = {}
    for tool in MAIL_MODULES:
        out[tool] = {v for v in MM.SAFE.get(tool, set())
                     if any(k in _verb_code(tool, v) for k in DRAFT_WRITE_MARKERS)}
    return out


def test_every_safe_verb_is_classified():
    changing = draft_changing_verbs()
    for tool, verbs in MM.SAFE.items():
        if tool in MAIL_MODULES:
            listed = set(NOT_DRAFT_CHANGING[tool])
            unknown = set(verbs) - changing[tool] - listed
            assert not unknown, (
                f"{tool}: SAFE verb(s) {sorted(unknown)} are not classified. If the verb "
                f"can change a draft, it must journal the change; otherwise add it to "
                f"NOT_DRAFT_CHANGING with the reason")
            wrong = changing[tool] & listed
            assert not wrong, f"{tool}: {sorted(wrong)} write drafts but are listed as not"
            stale = listed - set(verbs)
            assert not stale, f"{tool}: {sorted(stale)} are listed but no longer SAFE"
        else:
            assert tool in NON_MAIL_TOOLS, (
                f"SAFE tool {tool} is not classified: add it to NON_MAIL_TOOLS if it "
                f"cannot reach a draft")
            src = (ROOT / NON_MAIL_TOOLS[tool]).read_text()
            hits = [k for k in NON_MAIL_DRAFT_MARKERS if k in src]
            assert not hits, f"{tool} now holds a draft write ({hits}); classify its verbs"
    # the detection finds the verbs known to edit drafts in place
    assert {"update-draft", "attach-file", "remove-attachment", "create-reply",
            "delete-draft"} <= changing["graph_actions"], changing
    assert {"update-draft", "attach-file", "remove-attachment",
            "delete-draft"} <= changing["gmail_client"], changing


def _placeholders(tmp: Path) -> dict:
    """Values for the required options of a draft verb. Each one makes the
    verb change the fake draft."""
    body = tmp / "changed-body.txt"
    body.write_text("Changed: wire the funds to the new account.\n")
    new_file = tmp / "new.pdf"
    new_file.write_bytes(b"%PDF-1.4 injected")
    return {"message_id": DRAFT, "draft_id": GDRAFT, "email": GMAIL,
            "conversation_id": CONV, "body": "Changed: wire the funds to the new account.",
            "body_file": str(body), "file": str(new_file), "name": "a.pdf",
            "attachment_id": "att1", "to": "eve@example.net", "subject": "Changed subject"}


def _argv(tool: str, verb: str, tmp: Path) -> list:
    sub = _subparsers(PARSERS[tool]())[verb]
    values = _placeholders(tmp)
    argv = [verb]
    required = [a for a in sub._actions if a.required and a.option_strings]
    for group in sub._mutually_exclusive_groups:
        if group.required:
            pick = next((a for a in group._group_actions if a.dest in values), None)
            assert pick is not None, f"{tool} {verb}: no placeholder for any of " \
                                     f"{[a.option_strings[0] for a in group._group_actions]}"
            required.append(pick)
    for action in required:
        assert action.dest in values, (
            f"tripwire: {tool} {verb} requires {action.option_strings[0]}; add a "
            f"placeholder that changes the draft to _placeholders()")
        argv += [action.option_strings[0], values[action.dest]]
    return argv


def _run_verb(tool: str, argv: list) -> None:
    if tool == "graph_actions":
        args = PARSERS[tool]().parse_args(argv)
        ga._resolve_body(args)
        with redirect_stdout(io.StringIO()):
            args.func(args)
    else:
        with patch.object(sys, "argv", ["gmail_client.py"] + argv), \
             redirect_stdout(io.StringIO()):
            try:
                gm.main()
            except SystemExit as e:
                assert not e.code, f"gmail_client {argv[0]} exited {e.code}"


def _fresh_fp(tool: str):
    try:
        if tool == "graph_actions":
            return gc.draft_fingerprint(DRAFT)
        return gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True).get("fingerprint")
    except Exception:
        return None   # the draft is gone


def test_each_draft_changing_verb_after_a_card_makes_the_click_refuse():
    for tool, verbs in draft_changing_verbs().items():
        for verb in sorted(verbs):
            with sandbox() as (tmp, _posts):
                fakes = outlook() if tool == "graph_actions" else gmail()
                with fakes as fake:
                    reviewed = _fresh_fp(tool)      # the card is posted
                    _run_verb(tool, _argv(tool, verb, tmp))
                    now = _fresh_fp(tool)
                    assert now != reviewed, (
                        f"{tool} {verb} ran but the fake draft did not change; give it "
                        f"a placeholder that changes it")
                    out = (_cli_send(DRAFT, reviewed) if tool == "graph_actions"
                           else _cli_send_gmail(reviewed))
                    assert fake.sent == [], f"{tool} {verb}: the click sent changed content"
                    assert out.get("status") != "sent", (tool, verb, out)


def test_each_draft_changing_verb_after_a_queued_send_makes_the_cron_refuse():
    # Deferred sends are Outlook only (the Schedule card refuses a Gmail
    # draft), so the cron side covers the Outlook verbs.
    for verb in sorted(draft_changing_verbs()["graph_actions"]):
        with sandbox() as (tmp, posts), outlook() as fake:
            reviewed = gc.draft_fingerprint(DRAFT)   # the Schedule click
            _run_verb("graph_actions", _argv("graph_actions", verb, tmp))
            out = _cron_with_queued(reviewed)
            assert fake.sent == [], f"{verb}: the cron sent changed content"
            assert out["sent"] == 0, (verb, out, posts)


# ---------------------------------------------------------------------------
# 6. The draft lock: no edit lands between the check and the send
# ---------------------------------------------------------------------------

def _held(draft_id=DRAFT, **kw) -> bool:
    """True when the lock for `draft_id` (or the mailbox) is held elsewhere
    right now."""
    try:
        with DL.hold([draft_id] if draft_id else [], wait_s=0, **kw):
            return False
    except DL.Busy:
        return True


def test_the_lock_excludes_the_same_draft_and_the_whole_mailbox():
    with DL.hold([DRAFT]):
        assert _held(DRAFT), "a second holder took the same draft's lock"
        assert not _held("AAMkSomeOtherDraft="), "another draft is blocked"
        assert _held(None, whole_mailbox=True), "a whole-mailbox holder got in"
    with DL.hold(whole_mailbox=True):
        assert _held(DRAFT) and _held("AAMkSomeOtherDraft=")
    assert not _held(DRAFT) and not _held(None, whole_mailbox=True)
    mode = (Path(os.environ["EA_PRIVATE_DIR"]) / "draft-locks").stat().st_mode & 0o777
    assert mode == 0o700, oct(mode)


def test_the_outlook_send_holds_the_lock_from_the_check_to_the_send():
    seen = {}
    with sandbox(), outlook() as fake:
        fp = gc.draft_fingerprint(DRAFT)
        real_get, real_post = fake.graph_get, fake.graph_post

        def get(path, params=None):
            if path == f"/me/messages/{DRAFT}":
                seen["check"] = seen.get("check", True) and _held()
            return real_get(path, params)

        def post(path, body):
            seen["send"] = _held()
            return real_post(path, body)
        with patch.multiple(gc, graph_get=get, graph_post=post):
            assert _cli_send(DRAFT, fp).get("status") == "sent"
    assert seen == {"check": True, "send": True}, seen
    assert not _held()


def test_the_gmail_send_holds_the_lock_from_the_check_to_the_send():
    seen = {}
    with sandbox(), gmail() as fake:
        fp = gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True)["fingerprint"]
        drafts = fake.drafts

        def locked_drafts():
            d = drafts()
            get, send = d.get, d.send

            def _get(userId, id, format="full"):
                if format == "raw":
                    seen["check"] = _held(GDRAFT)
                return get(userId, id, format)

            def _send(userId, body):
                seen["send"] = _held(GDRAFT)
                return send(userId, body)
            d.get, d.send = _get, _send
            return d
        fake.drafts = locked_drafts
        assert _cli_send_gmail(fp).get("status") == "sent"
    assert seen == {"check": True, "send": True}, seen


def test_a_send_while_the_draft_is_locked_sends_nothing():
    with sandbox(), outlook() as fake, patch.object(DL, "WAIT_S", 0.2):
        fp = gc.draft_fingerprint(DRAFT)
        with DL.hold([DRAFT]):
            tok = gc.issue_inprocess_approval("send", DRAFT, fp)
            out = gc.send_draft(DRAFT, confirm_token=tok, fingerprint=fp)
        assert out["status"] == "error" and "Nothing was sent" in out["error"], out
        assert fake.sent == []


def test_the_lock_table_names_every_draft_changing_verb():
    changing = draft_changing_verbs()
    for tool in MAIL_MODULES:
        table = MM.DRAFT_EDIT_VERBS[tool]
        assert set(table) == changing[tool], (
            f"{tool}: the mediator's lock table {sorted(table)} and the verbs that "
            f"write a draft {sorted(changing[tool])} differ")
        subs = _subparsers(PARSERS[tool]())
        for verb, flag in table.items():
            if flag is None:
                continue
            opts = {o: a for a in subs[verb]._actions for o in a.option_strings}
            assert flag in opts and opts[flag].required, (tool, verb, flag)
    assert MM.draft_lock_scope("graph_actions", "update-draft",
                               ["update-draft", "--message-id", DRAFT]) == ([DRAFT], False)
    assert MM.draft_lock_scope("gmail_client", "attach-file",
                               ["attach-file", f"--draft-id={GDRAFT}"]) == ([GDRAFT], False)
    assert MM.draft_lock_scope("graph_actions", "create-reply",
                               ["create-reply", "--message-id", "AAMkReceived="]) == ([], True)
    assert MM.draft_lock_scope("graph_actions", "update-draft", ["update-draft"]) == ([], True)
    assert MM.draft_lock_scope("graph_actions", "search-mail", ["search-mail"]) is None


def _mediator_execute(tool, verb, argv, run):
    """mail_mediator._execute with the child run, the answer and the audit
    stubbed. Returns the (stdout, stderr, rc) it answered with."""
    import mail_mediator as MED
    answers = []
    with patch.object(MED, "_respond", lambda rid, o, e, rc: answers.append((o, e, rc))), \
         patch.object(MED, "_audit", lambda *a: None), \
         patch.object(MED, "_cleanup", lambda *a: None), \
         patch.object(MED, "_note_release", lambda *a: None), \
         patch.object(MED, "_gate_filter", lambda t, v, a, r, out: (out, "")), \
         patch.object(MED, "DRAFT_LOCK_WAIT_S", 0.2), \
         patch.object(MED.subprocess, "run", run):
        MED._execute("rid1", tool, verb, argv, "", argv, None, [], [], "/nonexistent")
    return answers[-1]


def test_the_mediator_runs_a_draft_edit_under_the_draft_lock():
    def child(cmd, **kw):
        child.held = (_held(DRAFT), _held("AAMkSomeOtherDraft="))
        return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)
    _mediator_execute("graph_actions", "update-draft",
                      ["update-draft", "--message-id", DRAFT, "--body", "x"], child)
    assert child.held == (True, False), child.held
    _mediator_execute("graph_actions", "create-reply",
                      ["create-reply", "--message-id", "AAMkReceived=", "--body", "x"], child)
    assert child.held == (True, True), "create-reply must hold the whole mailbox"
    _mediator_execute("graph_actions", "search-mail", ["search-mail", "--query", "x"], child)
    assert child.held == (False, False), "a read takes no draft lock"
    _mediator_execute("gmail_client", "remove-attachment",
                      ["remove-attachment", "--email", GMAIL, "--draft-id", GDRAFT,
                       "--name", "a.pdf"], child)
    assert _held(GDRAFT) is False


def test_the_mediator_refuses_a_draft_edit_while_a_send_holds_the_lock():
    ran = []

    def child(cmd, **kw):
        ran.append(cmd)
        return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)
    with DL.hold([DRAFT]):     # a send is between its check and its send
        out, err, rc = _mediator_execute(
            "graph_actions", "attach-file",
            ["attach-file", "--message-id", DRAFT, "--file", "/tmp/x.pdf"], child)
    assert ran == [] and rc == 3 and "being sent" in err, (ran, err, rc)


@contextmanager
def _owner_cli(owner_side=True):
    """mail_mediation as an owner-side CLI run sees it, the lock it takes
    released afterwards."""
    with patch.object(MM, "owner_side", lambda: owner_side), \
         patch.object(MM, "_OWNER_CLI_LOCK", None):
        try:
            yield
        finally:
            if MM._OWNER_CLI_LOCK is not None:
                MM._OWNER_CLI_LOCK.__exit__(None, None, None)


def test_an_owner_side_draft_edit_holds_the_draft_lock():
    """The bot, the remote briefing and the crons run graph_actions and
    gmail_client as the owner, with no mediator in between. Their draft
    edits take the lock the mediator takes for the resident."""
    with _owner_cli():
        MM.hold_owner_draft_lock("graph_actions", "update-draft",
                                 ["update-draft", "--message-id", DRAFT, "--body", "x"],
                                 mediated=False)
        assert _held(DRAFT) and not _held("AAMkSomeOtherDraft=")
    assert not _held(DRAFT)
    with _owner_cli():
        MM.hold_owner_draft_lock("graph_actions", "create-reply",
                                 ["create-reply", "--message-id", "AAMkReceived="],
                                 mediated=False)
        assert _held("AAMkSomeOtherDraft="), "create-reply must hold the whole mailbox"
    with _owner_cli():
        MM.hold_owner_draft_lock("gmail_client", "attach-file",
                                 ["attach-file", "--email", GMAIL, "--draft-id", GDRAFT],
                                 mediated=False)
        assert _held(GDRAFT)
    # None for a read, for a child the mediator started (it holds the lock
    # itself), and on the resident's side.
    for tool, verb, argv, mediated, side in (
            ("graph_actions", "search-mail", ["search-mail", "--query", "x"], False, True),
            ("graph_actions", "update-draft", ["update-draft", "--message-id", DRAFT], True, True),
            ("graph_actions", "update-draft", ["update-draft", "--message-id", DRAFT], False, False)):
        with _owner_cli(side):
            MM.hold_owner_draft_lock(tool, verb, argv, mediated=mediated)
            assert MM._OWNER_CLI_LOCK is None and not _held(DRAFT), (verb, mediated, side)


def test_an_owner_side_draft_edit_waits_for_a_send_and_changes_nothing():
    with DL.hold([DRAFT]), _owner_cli(), patch.object(DL, "WAIT_S", 0.2):
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            try:
                MM.hold_owner_draft_lock("graph_actions", "remove-attachment",
                                         ["remove-attachment", "--message-id", DRAFT],
                                         mediated=False)
                raise AssertionError("the edit went ahead while a send held the lock")
            except SystemExit as e:
                assert e.code == 3
        assert "being sent" in err.getvalue()


def test_the_mail_clis_take_the_owner_lock_before_they_dispatch():
    """graph_actions, gmail_client and mail_actions take it right after the child-parse
    check and before the verb runs; remote_briefing's draft is a
    graph_actions.py create-reply run, so it holds the lock too."""
    for path, dispatch in ((ROOT / "preprocessor" / "graph_actions.py", "args.func(args)"),
                           (ROOT / "preprocessor" / "gmail_client.py",
                            'if args.setup or args.command == "setup":'),
                           (ROOT / "preprocessor" / "mail_actions.py", "return args.func(args)")):
        src = path.read_text()
        check = src.index("_mediated = _mm_child.check_child_parse(parser, args)")
        lock = src.index("_mm_child.hold_owner_draft_lock(", check)
        assert "mediated=_mediated" in src[lock:lock + 200], path.name
        assert lock < src.index(dispatch, check), path.name
    import remote_briefing
    import inspect
    src = inspect.getsource(remote_briefing.make_draft)
    assert '"graph_actions.py"), "create-reply"' in src
    assert "graph_client" not in src


def test_the_gmail_send_names_the_draft_by_id_only():
    """drafts.send takes a Draft resource. The installed discovery document
    says only that its `message` is "the message content of the draft", not
    that a send carrying it sends that content, so the send names the draft
    by id and the lock carries the guarantee."""
    src = _module_functions(gm)["_checked_send"]
    assert 'body={"id": draft_id}' in src


# ---------------------------------------------------------------------------
# 7. The approval covers no more than the card shows
# ---------------------------------------------------------------------------

def test_remote_resources_are_found_and_inline_ones_are_not():
    html = ('<html><head><link rel="stylesheet" href="https://css.example.net/a.css">'
            '<style>@import "https://css.example.net/b.css"; '
            'td { background: url(\'https://img.example.net/bg.png\') }</style></head>'
            '<body background="https://img.example.net/body.png">'
            '<img src="https://track.example.net/p.gif?u=1&amp;v=2" width=1>'
            '<img srcset="https://img.example.net/s1.png 1x, //img.example.net/s2.png 2x">'
            '<img src="cid:logo@x"><img src="data:image/png;base64,AAAA">'
            '<a href="https://example.org/read">read</a></body></html>')
    found = ML.remote_resources(html)
    assert found == ["https://css.example.net/a.css", "https://img.example.net/body.png",
                     "https://track.example.net/p.gif?u=1&v=2",
                     "https://img.example.net/s1.png", "//img.example.net/s2.png",
                     "https://img.example.net/bg.png", "https://css.example.net/b.css"], found
    assert ML.remote_resources("<p>plain</p>") == []
    line = ML.remote_resource_warning(found)
    assert "7 resources" in line and "https://track.example.net/p.gif?u=1&v=2" in line
    assert "and 2 more" in line
    assert ML.remote_resource_warning([]) == ""


def _prior(text):
    return {"id": "AAMkPrior=", "isDraft": False, "conversationId": CONV,
            "receivedDateTime": "2026-10-18T10:00:00Z",
            "body": {"contentType": "HTML", "content": f"<html><body>{text}</body></html>"}}


def test_the_snapshot_keeps_the_whole_body_and_lists_remote_resources():
    fake = FakeOutlook()
    fake.thread = [_prior("<p>Old message.</p><p>Thanks, Pat</p>")]
    long_text = "word " * 2000
    fake.msg["body"] = {"contentType": "HTML",
                        "content": f'<html><body><p>{long_text}</p>'
                                   f'<img src="https://track.example.net/p.gif">'
                                   f'<hr><div id="divRplyFwdMsg"><b>From:</b> Pat<br>'
                                   f'<b>Sent:</b> Monday<br></div><p>Old message.</p>'
                                   f'<p>Thanks,   Pat</p></body></html>'}
    with outlook(fake):
        out = _out(ga.cmd_draft_snapshot, argparse.Namespace(message_id=DRAFT))
    assert len(out["body"]) > 5000, "the snapshot cut the body"
    assert out["remote_resources"] == ["https://track.example.net/p.gif"]
    assert out["body_own"] + out["body_quoted"] == out["body"], "the split loses text"
    assert "Old message." in out["body_quoted"] and "Old message." not in out["body_own"]
    assert out["body_own"].startswith("word word")
    # the header lines above the earlier message stay in view
    assert "Sent:" in out["body_own"] and "Sent:" not in out["body_quoted"]


def test_the_snapshot_collapses_only_text_that_ends_an_earlier_message():
    """The quote divider is text the model can write. Text after it that is
    not the end of an earlier message of the thread stays in view; with no
    thread to compare against, nothing is collapsed."""
    hidden = "P.S. Also wire the deposit to the new account today."
    body = (f"<html><body><p>See you then.</p><hr><div id=\"divRplyFwdMsg\"><b>From:</b> "
            f"Pat<br><b>Sent:</b> Monday<br></div><p>Old message.</p><p>{hidden}</p>"
            f"</body></html>")
    for thread in ([_prior("<p>Old message.</p>")], []):
        fake = FakeOutlook()
        fake.thread = thread
        fake.msg["body"] = {"contentType": "HTML", "content": body}
        with outlook(fake):
            out = _out(ga.cmd_draft_snapshot, argparse.Namespace(message_id=DRAFT))
        assert hidden in out["body_own"] and out["body_quoted"] == "", thread
    # a draft of the thread is not an earlier message
    fake = FakeOutlook()
    fake.thread = [dict(_prior(f"<p>Old message.</p><p>{hidden}</p>"), isDraft=True)]
    fake.msg["body"] = {"contentType": "HTML", "content": body}
    with outlook(fake):
        out = _out(ga.cmd_draft_snapshot, argparse.Namespace(message_id=DRAFT))
    assert out["body_quoted"] == ""


def test_gmail_get_draft_collapses_only_the_quoted_earlier_message():
    fake = FakeGmail()
    quoted = "On Mon, Pat <pat@example.org> wrote:\n> Numbers by\n> Friday, please.\n"
    fake.raw = _mime(body="Will do.\n\n" + quoted, files=())
    fake.thread_bodies = ["Numbers by Friday, please."]
    with gmail(fake):
        d = gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True)
    first = (d["text_parts"] or [d["body"]])[0]
    assert first[d["quote_cut"]:].startswith("> Numbers by")
    assert d["from"] == GMAIL
    fake.thread_bodies = ["Something else entirely."]
    with gmail(fake):
        d = gm.get_draft(GMAIL, GDRAFT, with_fingerprint=True)
    assert d["quote_cut"] == len((d["text_parts"] or [d["body"]])[0])


def test_gmail_get_draft_lists_resources_and_every_text_part():
    fake = FakeGmail()
    m = MIMEMultipart("mixed")
    m["From"], m["To"], m["Subject"] = GMAIL, "Pat <pat@example.org>", "Re: budget"
    m.attach(MIMEText("Numbers by Friday.", "plain", "utf-8"))
    m.attach(MIMEText("Second text part.", "plain", "utf-8"))
    m.attach(MIMEText('<p>hi</p><img src="https://track.example.net/g.gif">', "html", "utf-8"))
    fake.raw = m.as_bytes()
    with gmail(fake):
        d = gm.get_draft(GMAIL, GDRAFT)
    assert d["remote_resources"] == ["https://track.example.net/g.gif"]
    assert d["text_parts"] == ["Numbers by Friday.", "Second text part."], d["text_parts"]
    with gmail():
        assert gm.get_draft(GMAIL, GDRAFT)["text_parts"] == []


def _fetch_ns(stdout: dict):
    """_fetch_draft_body out of the bot source, its CLI read stubbed."""
    ns = {"json": json, "re": re, "SCRIPT_DIR": ROOT, "_mail_links": ML}
    exec(compile(_fn_src("_fetch_draft_body"), "ea-chat-bot.py[fetch]", "exec"), ns)
    run = lambda cmd, **kw: types.SimpleNamespace(stdout=json.dumps(stdout), stderr="",
                                                   returncode=0)
    return ns, run


def test_the_card_read_splits_the_authored_text_from_the_quoted_thread():
    own = "Numbers by Friday.\n\n"
    quoted = "From: Pat\nSent: Monday\nTo: Owner\n\nOld message."
    ns, run = _fetch_ns({"to": [], "cc": [], "subject": "Re: budget", "body": own + quoted,
                         "body_type": "text (extracted from html)", "body_own": own,
                         "body_quoted": quoted, "fingerprint": "sha256:aa"})
    with patch("subprocess.run", run):
        d = ns["_fetch_draft_body"](DRAFT)
    assert d["body"] == own.strip() and d["quoted"] == quoted
    # Gmail: every text part, the first part's quoted history collapsed
    first = "Thanks.\n\nOn Mon, Pat <pat@example.org> wrote:\n> old text\n"
    cut = first.index("> old text")
    ns, run = _fetch_ns({"to": "pat@example.org", "subject": "Re: x", "body": first,
                         "text_parts": [first, "A second part."], "fingerprint": "f",
                         "quote_cut": cut, "from": GMAIL})
    with patch("subprocess.run", run):
        d = ns["_fetch_draft_body"](GDRAFT, email_system="gmail", gmail_email=GMAIL)
    assert d["body"].startswith("Thanks.") and "[Text part 2]\nA second part." in d["body"]
    assert "On Mon, Pat" in d["body"] and d["quoted"] == "> old text"
    assert d["from"] == GMAIL
    # no verified cut from the owner-side read: nothing is collapsed
    for bad in (None, -1, len(first) + 1, True, "3"):
        ns, run = _fetch_ns({"to": "pat@example.org", "subject": "Re: x", "body": first,
                             "fingerprint": "f", "quote_cut": bad})
        with patch("subprocess.run", run):
            d = ns["_fetch_draft_body"](GDRAFT, email_system="gmail", gmail_email=GMAIL)
        assert d["quoted"] == "" and "> old text" in d["body"], bad


def _card_ns(body_chars=2800):
    return _exec_cards({"FRONTEND": _frontend_stub(None, body_chars)})


def _draft(body, **kw):
    d = {"to": "pat@example.org", "cc": "", "bcc": "", "subject": "Re: budget",
         "body": body, "attachments": [], "fingerprint": "sha256:aa"}
    d.update(kw)
    return d


def test_a_draft_that_fits_one_card_keeps_send_and_carries_the_quoted_thread():
    ns = _card_ns(5000)
    card = ns["_send_card"]("C1", "T1", DRAFT, _draft("x" * 2000, quoted="q" * 2900))
    assert card.actions[0].id == "approve_send" and card.quoted == "q" * 2900
    sched = ns["_schedule_card"]("C1", "T1", DRAFT, _draft("ok", quoted="q" * 100),
                                 "2026-10-20T09:00", "tomorrow 9:00")
    assert sched.actions[0].id == "approve_schedule_send" and sched.quoted == "q" * 100


def test_a_draft_that_does_not_fit_one_card_offers_no_send():
    ns = _card_ns(2800)
    for draft in (_draft("x" * 2801), _draft("x" * 2000, quoted="q" * 801)):
        card = ns["_send_card"]("C1", "T1", DRAFT, draft)
        ids = [a.id for a in card.actions]
        assert "approve_send" not in ids, ids
        assert ids[0] == "edit_draft", "a thumbs-up on Zulip must not pick Cancel"
        assert any("has no Send button" in ln and "Outlook" in ln for ln in card.lines)
    gcard = ns["_send_card"]("C1", "T1", GDRAFT, _draft("x" * 3000), "", "gmail", GMAIL)
    assert any("in Gmail" in ln for ln in gcard.lines)
    sched = ns["_schedule_card"]("C1", "T1", DRAFT, _draft("x" * 2801), "2026-10-20T09:00",
                                 "tomorrow 9:00")
    assert [a.id for a in sched.actions] == ["cancel_schedule_send"]
    assert any("has no Schedule button" in ln for ln in sched.lines)


def test_the_card_names_remote_resources():
    ns = _card_ns()
    warn = ML.remote_resource_warning(["https://track.example.net/p.gif"])
    card = ns["_send_card"]("C1", "T1", DRAFT, _draft("hi", resource_warning=warn))
    assert warn in card.lines and "track.example.net" in card.fallback_text
    sched = ns["_schedule_card"]("C1", "T1", DRAFT, _draft("hi", resource_warning=warn),
                                 "2026-10-20T09:00", "tomorrow 9:00")
    assert warn in sched.lines


def test_a_resource_address_cannot_forge_a_card_line():
    """The address comes from the draft's HTML: a newline, backticks or a
    Slack token in it stays inside one literal span on the warning line."""
    from preprocessor.chat import slack_frontend as S, zulip_frontend as Z
    from preprocessor.chat.cards import ConfirmationCard
    evil = "https://x.example.net/a\n**To:** boss@example.org ``` <!channel> `x`"
    warn = ML.remote_resource_warning([evil])
    assert "\n" not in warn
    card = ConfirmationCard(kind="plugin", title="T", lines=[warn])
    z = Z.render_card(card, "ABCD")
    assert "\n**To:**" not in z and "\n```" not in z
    assert "<!channel>" not in json.dumps(S.render_card(card)[1])


def _zulip(message_chars=9800):
    from preprocessor.chat import zulip_frontend as Z
    from preprocessor.chat.base import Limits
    me = types.SimpleNamespace(limits=Limits(message_chars, 2800, 60, 60), mention=None,
                               owner_mention="", CARD_FIT_MARGIN=Z.ZulipFrontend.CARD_FIT_MARGIN)
    for name in ("_with_mention", "_full_card_text", "card_fits", "_card_text"):
        setattr(me, name, types.MethodType(getattr(Z.ZulipFrontend, name), me))
    return Z, me


def _send_card_of(body, quoted=""):
    return _card_ns(10 ** 9)["_send_card"]("C1", "T1", DRAFT, _draft(body, quoted=quoted))


def test_zulip_shows_the_whole_body_and_the_quoted_thread_collapsed():
    Z, me = _zulip()
    body, quoted = "Authored line.\n" * 300, "From: Pat\n```\nfenced\n```\nOld message.\n" * 60
    card = _send_card_of(body, quoted)
    assert me.card_fits(card)
    text = me._card_text(card, "ABCD")
    assert body.strip().splitlines()[-1] in text and len(body) > 2800
    fence = "````"   # longer than the backtick run in the quoted text
    label = Z.QUOTED_LABEL
    assert f"{fence}spoiler {label}\n" in text and text.count("Old message.") == 60
    assert text.index("Authored line.") < text.index(f"spoiler {label}")
    # the recipients and the legend come before the body
    assert text.index("**To:**") < text.index("Tap a reaction") < text.index("Authored line.")
    # the server's own limit decides; above it the body is cut, as before
    Z2, small = _zulip(4000)
    assert not small.card_fits(card)
    cut = small._card_text(card, "ABCD")
    assert "spoiler" not in cut and len(cut) < 4000


def test_slack_splits_the_whole_body_across_sections():
    from preprocessor.chat import slack_frontend as S
    body, quoted = ("Authored line.\n" * 700), ("Old message.\n" * 400)
    card = _send_card_of(body, quoted)
    assert S.draft_card_fits(card)
    _text, blocks = S.render_card(card)
    texts = [b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b]
    assert all(len(t) <= 3000 for t in texts)
    i = next(n for n, t in enumerate(texts) if t.startswith(f"*{S.QUOTED_LABEL}:*"))
    head = next(n for n, t in enumerate(texts) if t.startswith("*Subject:*")) + 1
    assert i - head > 1, "the body was not split"
    assert "".join(texts[head:i]) == body, "the body is not all on the card"
    # The quoted thread waits behind Show thread, which reads the same draft.
    assert "Old message." not in "".join(texts) and "hidden" in texts[i]
    buttons = {a.get("action_id"): a for b in blocks if b.get("type") == "actions"
               for a in b["elements"]}
    assert "approve_send" in buttons and S.SHOW_QUOTED_ACTION in buttons
    assert json.loads(buttons[S.SHOW_QUOTED_ACTION]["value"])["draft_id"] == DRAFT
    assert S.quoted_thread_text(quoted).count(">Old message.") == 400
    # A long thread no longer costs the card its Send button.
    assert S.draft_card_fits(_send_card_of("ok", "Old message.\n" * 20000))
    huge = _send_card_of("y" * (S.BODY_CHARS * S.MAX_BLOCKS))
    assert not S.draft_card_fits(huge)
    _t, cut = S.render_card(huge)
    assert len(cut) <= S.MAX_BLOCKS


def test_a_click_on_a_body_the_card_cannot_show_sends_nothing():
    ns, rec, calls, run = _click_ns("sha256:aa", body="x" * 2801)
    assert _click(ns, run, "sha256:aa") is False
    assert calls == [], "a token was minted for a body the card did not show"
    assert rec.statuses and "Nothing was sent" in rec.statuses[-1][1]
    assert "retired" in rec.marks


# ---------------------------------------------------------------------------
# 8. The raw address headers
# ---------------------------------------------------------------------------

def test_raw_address_headers_that_parse_alike_give_different_fingerprints():
    pairs = [("Pat <pat@example.org>", '"Pat" <pat@example.org>'),
             ("pat@example.org", "pat@example.org,"),
             ("pat@example.org", "PAT@example.org"),
             ("Pat <pat@example.org>", "Pat  <pat@example.org>")]
    for a, b in pairs:
        pa, pb = DFP.gmail_parts(_mime(to=a)), DFP.gmail_parts(_mime(to=b))
        assert DFP._addr_list(pa["to"]) == DFP._addr_list(pb["to"]), (a, b)
        assert DFP.fingerprint(pa) != DFP.fingerprint(pb), (a, b)
    base = DFP.fingerprint(DFP.gmail_parts(_mime()))
    for header, value in (("From", "Eve <eve@example.net>"),
                          ("Reply-To", "eve@example.net")):
        m = message_from_bytes(_mime(), policy=email_policy.default)
        if header in m:
            m.replace_header(header, value)
        else:
            m[header] = value
        assert DFP.fingerprint(DFP.gmail_parts(m.as_bytes())) != base, header


def test_outlook_from_reply_to_and_raw_recipients_move_the_fingerprint():
    base = FakeOutlook()
    fp0 = _outlook_fp(base.msg, base.atts)
    edits = {
        "from": lambda m: m.update({"from": _rcpt("eve@example.net")}),
        "replyTo": lambda m: m.update(replyTo=[_rcpt("eve@example.net")]),
        "address case": lambda m: m["toRecipients"][0]["emailAddress"].update(
            address="PAT@example.org"),
        "name spaces": lambda m: m["toRecipients"][0]["emailAddress"].update(name=" Pat "),
    }
    for label, edit in edits.items():
        m = copy.deepcopy(base.msg)
        edit(m)
        assert _outlook_fp(m, base.atts) != fp0, label
    assert "replyTo" in DFP.OUTLOOK_SELECT and "from" in DFP.OUTLOOK_SELECT

def test_show_thread_opens_the_quoted_thread_in_the_card_and_hide_folds_it():
    from preprocessor.chat import slack_frontend as S
    quoted = "Old message.\n" * 50
    _text, blocks = S.render_card(_send_card_of("Authored line.", quoted))
    opened = S.toggle_quoted_blocks(blocks, quoted, True)
    flat = json.dumps(opened)
    assert flat.count(">Old message.") == 50 and S.HIDE_QUOTED_ACTION in flat
    assert len(opened) <= S.MAX_BLOCKS
    folded = S.toggle_quoted_blocks(opened, quoted, False)
    assert "Old message." not in json.dumps(folded) and S.SHOW_QUOTED_ACTION in json.dumps(folded)
    assert len(folded) == len(blocks)
    huge = "Old line.\n" * 30000
    assert len(S.toggle_quoted_blocks(blocks, huge, True)) <= S.MAX_BLOCKS
    assert S.toggle_quoted_blocks([{"type": "divider"}], quoted, True) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

