"""native_accounts: the macOS native-accounts backend on any OS.

The helper protocol and both transports, the gate on gated helper verbs,
the handles, the scope, each mail and calendar verb against
FakeNativeHelper (Graph shapes, the send gate, drafts edited in Mail), the
SAFE / GATED split read from mail_mediation, the doctor rows, the config
keys, and the presence and shape of the Swift helper, the JXA script, the
Info.plist, the build script and the launch agent. Nothing here runs
EventKit or Mail: those need a Mac (docs/macos-native-accounts.md, "First
tests on a Mac")."""
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import ea_config  # noqa: E402
import mail_mediation  # noqa: E402
import native_accounts as na  # noqa: E402

HELPER_DIR = ROOT / "deploy" / "macos" / "tacitus-native"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ME = "me@example.edu"

SCOPE = {
    "backend": {"mail": "macos", "calendar": "macos", "availability": "owa"},
    "mail": {"accounts": [{"name": "Work", "kind": "exchange", "addresses": [ME]}]},
    "calendar": {
        "calendars": [
            {"id": "CAL-WORK", "title": "Calendar", "write": True},
            {"id": "CAL-EA", "title": "EA-Managed", "write": True},
            {"id": "CAL-FAM", "title": "Family", "blocking": False},
            {"id": "CAL-PAT", "title": "Pat (shared)"},
        ],
        "default": "CAL-WORK", "ea_managed": "CAL-EA",
        "shared": {"pat@example.edu": "CAL-PAT"},
    },
    "contacts": {"enabled": True},
}


class Tokens:
    """A stand-in for graph_client's one-time token check."""

    def __init__(self):
        self.valid = set()
        self.calls = []

    def grant(self, token, action, identifier, fingerprint=""):
        self.valid.add((token, action, identifier, fingerprint or ""))

    def __call__(self, token, action, identifier, fingerprint=None):
        self.calls.append((token, action, identifier, fingerprint))
        key = (token, action, identifier, fingerprint or "")
        if key in self.valid:
            self.valid.discard(key)
            return True
        return False


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    return d


@pytest.fixture
def fake():
    f = na.FakeNativeHelper()
    f.add_account("Work", [ME], ["Inbox", "Sent Items", "Archive", "Drafts", "Deleted Items"])
    f.add_message("Work", "Inbox", "<root-1@example.org>", "Budget review",
                  "Alex Doe <alex@example.org>", to=[ME], cc=["kim@example.org"],
                  date="2026-10-20T10:00:00Z", content="Can we review the budget?")
    f.add_message("Work", "Sent Items", "<reply-2@example.edu>", "Re: Budget review",
                  ME, to=["alex@example.org"], date="2026-10-20T12:00:00Z",
                  content="Yes, Thursday works.", in_reply_to="<root-1@example.org>",
                  references="<root-1@example.org>")
    f.add_message("Work", "Inbox", "<reply-3@example.org>", "Re: Budget review",
                  "Alex Doe <alex@example.org>", to=[ME], date="2026-10-21T09:00:00Z",
                  content="Great.", in_reply_to="<reply-2@example.edu>",
                  references="<root-1@example.org> <reply-2@example.edu>")
    f.add_message("Work", "Inbox", "<other-4@example.org>", "Budget review",
                  "Kim Roe <kim@example.org>", to=[ME], date="2026-10-22T09:00:00Z",
                  content="A different thread with the same subject.")
    f.add_message("Work", "Archive", "<old-5@example.org>", "Lunch", "Sam <sam@example.org>",
                  to=[ME], date="2026-10-01T09:00:00Z", content="Lunch?")
    f.add_calendar("CAL-WORK", "Calendar")
    f.add_calendar("CAL-EA", "EA-Managed")
    f.add_calendar("CAL-FAM", "Family", writable=False)
    f.add_calendar("CAL-PAT", "Pat (shared)", writable=False)
    f.add_calendar("CAL-OTHER", "Not in scope")
    return f


@pytest.fixture
def scope():
    return na.scope_from_dict(SCOPE)


@pytest.fixture
def tokens():
    return Tokens()


@pytest.fixture
def mail(fake, scope, priv, tokens):
    return na.NativeMailClient(na.NativeHelper(fake), scope,
                               na.DraftRegistry(priv / na.DRAFTS_FILE),
                               approve=tokens, is_resident=lambda: False, now=lambda: NOW)


@pytest.fixture
def cal(fake, scope, tokens):
    return na.NativeCalendarClient(na.NativeHelper(fake), scope, approve=tokens, now=lambda: NOW)


# ── the protocol ─────────────────────────────────────────────────────

def test_a_request_carries_version_id_verb_and_args():
    req = na.make_request("list-events", {"calendars": ["A"]})
    assert req["v"] == na.PROTOCOL_VERSION and req["verb"] == "list-events"
    assert re.fullmatch(r"[0-9a-f]{32}", req["id"]) and req["args"] == {"calendars": ["A"]}
    with pytest.raises(na.NativeError) as e:
        na.make_request("send-everything")
    assert e.value.code == "unknown-verb"
    with pytest.raises(na.NativeError):
        na.make_request("status", request_id="../../etc/passwd")


def test_parse_response_takes_only_an_answer_to_this_request():
    req = na.make_request("status")
    ok = {"v": 1, "id": req["id"], "ok": True, "result": {"a": 1}}
    assert na.parse_response(req, json.dumps(ok).encode()) == {"a": 1}
    assert na.parse_response(req, ok) == {"a": 1}
    for bad, why in [({**ok, "id": "f" * 32}, "another request"),
                     ({**ok, "v": 2}, "protocol version"),
                     ({**ok, "result": [1]}, "no result object"),
                     ({"v": 1, "id": req["id"]}, "no ok field"),
                     (b"not json", "not JSON"), ([1, 2], "not a JSON object"),
                     (b"x" * (na.MAX_RESPONSE_BYTES + 1), "bytes")]:
        with pytest.raises(na.NativeError) as e:
            na.parse_response(req, bad)
        assert e.value.code == "protocol" and why in str(e.value), (bad, e.value)


def test_a_helper_error_keeps_its_code_and_an_unknown_code_is_internal():
    req = na.make_request("status")
    with pytest.raises(na.NativeError) as e:
        na.parse_response(req, {"v": 1, "id": req["id"], "ok": False,
                                "error": {"code": "permission-denied", "message": "no",
                                          "permission": "calendar"}})
    assert e.value.code == "permission-denied" and e.value.detail == {"permission": "calendar"}
    with pytest.raises(na.NativeError) as e:
        na.parse_response(req, {"v": 1, "id": req["id"], "ok": False,
                                "error": {"code": "weird", "message": "x"}})
    assert e.value.code == "internal"


def test_the_fake_answers_an_unknown_verb_as_the_helper_does(fake):
    req = na.make_request("status")
    req["verb"] = "launch-missiles"
    with pytest.raises(na.NativeError) as e:
        na.parse_response(req, fake(req))
    assert e.value.code == "unknown-verb"


def test_a_gated_helper_verb_needs_a_fresh_approval_for_that_verb(fake):
    gated = {v for v, k in na.HELPER_VERBS.items() if k == "gated"}
    assert gated == {"create-event", "update-event", "delete-event", "send-message"}
    h = na.NativeHelper(fake)
    args = {"account": "Work", "sender": ME, "to": [{"address": "a@example.org"}],
            "subject": "s", "body": "b"}
    for approval in (None, na._Approval("create-event")):
        with pytest.raises(na.NativeError) as e:
            h.call("send-message", args, approval=approval)
        assert e.value.code == "permission-denied"
    assert "send-message" not in fake.calls and fake.sent == []
    a = na._Approval("send-message")
    assert h.call("send-message", args, approval=a) == {"sent": True}
    with pytest.raises(na.NativeError):
        h.call("send-message", args, approval=a)      # spent
    assert len(fake.sent) == 1


def test_mail_verbs_are_the_helpers_mail_verbs():
    assert na.MAIL_HELPER_VERBS <= set(na.HELPER_VERBS)
    assert {v for v in na.HELPER_VERBS if v not in na.MAIL_HELPER_VERBS} == {
        "status", "request-access", "list-calendars", "list-events", "get-event",
        "create-event", "update-event", "delete-event", "find-contacts"}


# ── transports ───────────────────────────────────────────────────────

def _serve(queue: Path, fake, stop: threading.Event):
    while not stop.is_set():
        for p in sorted(queue.glob("req-*.json")):
            req = json.loads(p.read_bytes())
            p.unlink()
            tmp = queue / f".resp-{req['id']}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, fake(req))
            os.close(fd)
            os.replace(tmp, queue / f"resp-{req['id']}.json")
        time.sleep(0.01)


def test_the_queue_transport_round_trips_through_an_agent(tmp_path, fake):
    q = tmp_path / "queue"
    q.mkdir(mode=0o700)
    stop = threading.Event()
    t = threading.Thread(target=_serve, args=(q, fake, stop), daemon=True)
    t.start()
    try:
        h = na.NativeHelper(na.QueueTransport(q, timeout=5, poll_s=0.01))
        assert h.call("status")["permissions"]["calendar"] == "authorized"
        assert [c["id"] for c in h.call("list-calendars")["calendars"]][:2] == ["CAL-WORK", "CAL-EA"]
    finally:
        stop.set()
        t.join(2)
    assert list(q.iterdir()) == []


def test_the_queue_transport_times_out_and_withdraws_its_request(tmp_path):
    q = tmp_path / "queue"
    q.mkdir(mode=0o700)
    with pytest.raises(na.NativeError) as e:
        na.NativeHelper(na.QueueTransport(q, timeout=0.2, poll_s=0.05)).call("status")
    assert e.value.code == "timeout"
    assert list(q.iterdir()) == []


def test_the_queue_transport_refuses_a_queue_others_can_enter(tmp_path):
    q = tmp_path / "queue"
    q.mkdir(mode=0o755)
    with pytest.raises(na.NativeError) as e:
        na.NativeHelper(na.QueueTransport(q, timeout=1)).call("status")
    assert e.value.code == "protocol" and "0700" in str(e.value)
    assert not list(q.iterdir())


def test_the_subprocess_transport_runs_one_helper_per_call(tmp_path, fake):
    helper = tmp_path / "tacitus-native"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.argv[1:] == ['call']\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'v': 1, 'id': req['id'], 'ok': True,"
        " 'result': {'echo': req['verb']}}))\n")
    helper.chmod(0o755)
    h = na.NativeHelper(na.SubprocessTransport(helper, timeout=30))
    assert h.call("status") == {"echo": "status"}

    def silent(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 3, b"", b"dyld: missing symbol")
    with pytest.raises(na.NativeError) as e:
        na.NativeHelper(na.SubprocessTransport(helper, runner=silent)).call("status")
    assert e.value.code == "internal" and "missing symbol" in str(e.value)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    with pytest.raises(na.NativeError) as e:
        na.NativeHelper(na.SubprocessTransport(helper, runner=slow)).call("status")
    assert e.value.code == "timeout"


# ── handles and threads ──────────────────────────────────────────────

def test_handles_round_trip_and_are_plain_mail_ids():
    mid = "CAF=abc+def/ghi@mail.example.org"
    for h, back in [(na.message_handle(f"<{mid}>"), lambda h: na.message_id_of(h)),
                    (na.conversation_handle(mid), lambda h: na.root_of(h))]:
        assert mail_mediation.is_plain_id(h), h
        assert back(h) == mid
    eh = na.event_handle("ABC:DEF-123", "2026-10-27T15:00:00-06:00")
    assert mail_mediation.is_plain_id(eh)
    assert na.event_of(eh) == ("ABC:DEF-123", "2026-10-27T15:00:00-06:00")
    assert na.event_of(na.event_handle("X")) == ("X", "")
    assert mail_mediation.is_plain_id(na.new_draft_handle())
    with pytest.raises(ValueError):
        na.message_handle("x" * 400 + "@example.org")
    with pytest.raises(ValueError):
        na.message_id_of(na.conversation_handle("a@b"))


def test_a_thread_is_named_by_the_first_reference():
    assert na.thread_root({"message_id": "c@x", "headers": {
        "references": "<a@x> <b@x>", "in-reply-to": "<b@x>"}}) == "a@x"
    assert na.thread_root({"message_id": "c@x", "headers": {"in-reply-to": "<b@x>"}}) == "b@x"
    assert na.thread_root({"message_id": "<c@x>", "headers": {}}) == "c@x"


# ── scope and backends ───────────────────────────────────────────────

def test_the_scope_fills_mailbox_names_by_account_kind(scope):
    work = scope.account("Work")
    assert work.mailboxes["sentitems"] == "Sent Items" and work.search == ["Inbox", "Sent Items", "Archive"]
    gmail = na.scope_from_dict({"mail": {"accounts": [
        {"name": "G", "kind": "gmail", "addresses": ["g@example.com"],
         "mailboxes": {"archive": "Archive"}}]}}).account("G")
    assert gmail.mailboxes["sentitems"] == "[Gmail]/Sent Mail" and gmail.mailboxes["archive"] == "Archive"
    assert scope.addresses() == {ME}
    assert [(a.name, m, r) for a, m, r in scope.boxes("sentitems")] == [("Work", "Sent Items", "sentitems")]
    assert [m for _, m, _ in scope.boxes(None)] == ["Inbox", "Sent Items", "Archive"]
    with pytest.raises(na.NativeError) as e:
        scope.boxes("Personal Finance")
    assert e.value.code == "out-of-scope"
    with pytest.raises(na.NativeError):
        scope.account("Home")


@pytest.mark.parametrize("doc, why", [
    ({"mail": {"accounts": [{"name": "W", "addresses": []}]}}, "addresses"),
    ({"mail": {"accounts": [{"name": "W", "kind": "lotus", "addresses": ["a@b"]}]}}, "kind"),
    ({"mail": {"accounts": [{"name": "W", "addresses": ["a@b"], "mailboxes": {"spam": "X"}}]}}, "role"),
    ({"calendar": {"calendars": [{"id": "A"}], "default": "B"}}, "calendar.default"),
    ({"calendar": {"calendars": [{"id": "A"}], "shared": {"p@x": "Z"}}}, "shared"),
    ({"calendar": {"calendars": [{"title": "no id"}]}}, "EventKit id"),
])
def test_a_scope_that_names_something_unclear_is_refused(doc, why):
    with pytest.raises(ValueError) as e:
        na.scope_from_dict(doc)
    assert why in str(e.value)


def test_backends_default_to_graph_and_the_owner_file_outranks_ea_config(tmp_path):
    cfg = tmp_path / "ea-config.json"
    assert na.backends(cfg, scope=na.NativeScope()) == na.DEFAULT_BACKENDS
    cfg.write_text(json.dumps({"mail": {"backend": "macos"}, "calendar": {"backend": "bogus"},
                               "availability": {"provider": "none"}}))
    assert na.backends(cfg, scope=na.NativeScope()) == {
        "mail": "macos", "calendar": "graph", "availability": "none"}
    owner = na.scope_from_dict({"backend": {"mail": "graph", "calendar": "macos",
                                            "availability": "owa", "extra": "x"}})
    assert na.backends(cfg, scope=owner) == {"mail": "graph", "calendar": "macos", "availability": "owa"}


def test_the_config_template_and_defaults_name_the_backends():
    tpl = json.loads((ROOT / "config" / "ea-config.json").read_text())
    assert tpl["mail"] == {"backend": "graph"} and tpl["calendar"] == {"backend": "graph"}
    assert tpl["availability"] == {"provider": "graph"} and "owa" in tpl["backends_note"]
    assert ea_config.get_backends(ROOT / "config" / "ea-config.json") == na.DEFAULT_BACKENDS
    assert ea_config.get_backends(Path("/nonexistent/ea-config.json")) == na.DEFAULT_BACKENDS
    for kind, (key, choices) in ea_config.BACKEND_CHOICES.items():
        assert choices == na.BACKENDS[kind]


def test_the_example_scope_file_parses():
    doc = json.loads((HELPER_DIR / "native-accounts.example.json").read_text())
    s = na.scope_from_dict(doc)
    assert s.backend == {"mail": "macos", "calendar": "macos", "availability": "owa"}
    assert s.ea_managed in s.calendars and s.default_calendar in s.calendars


# ── mail reads ───────────────────────────────────────────────────────

def test_search_returns_graph_shaped_messages_newest_first(mail, fake):
    msgs, more = mail.search_messages("budget", top=10)
    assert [m["internetMessageId"] for m in msgs] == [
        "<other-4@example.org>", "<reply-3@example.org>", "<reply-2@example.edu>",
        "<root-1@example.org>"]
    assert more is False
    m = msgs[-1]
    assert m["from"] == {"emailAddress": {"name": "Alex Doe", "address": "alex@example.org"}}
    assert m["ccRecipients"] == [{"emailAddress": {"name": "", "address": "kim@example.org"}}]
    assert m["receivedDateTime"] == "2026-10-20T10:00:00Z"
    assert m["conversationId"] == na.conversation_handle("root-1@example.org")
    assert m["bodyPreview"].startswith("Can we review") and m["categories"] == []
    assert m["parentFolderId"] == "Work/Inbox" and m["mailbox"]["role"] == "inbox"
    assert mail_mediation.is_plain_id(m["id"]) and mail_mediation.is_plain_id(m["conversationId"])
    assert set(m) >= {
        "id", "subject", "from", "toRecipients", "ccRecipients", "receivedDateTime",
        "sentDateTime", "bodyPreview", "hasAttachments", "conversationId", "flag",
        "isRead", "importance", "categories", "isDraft"}
    only_sent, _ = mail.search_messages("budget", folder="sentitems")
    assert [m["internetMessageId"] for m in only_sent] == ["<reply-2@example.edu>"]
    with pytest.raises(na.NativeError) as e:
        mail.search_messages("x", folder="Private")
    assert e.value.code == "out-of-scope"


def test_list_mail_is_one_folder_newest_first_with_has_more(mail):
    msgs, more = mail.list_folder_messages("inbox", top=2)
    assert [m["internetMessageId"] for m in msgs] == ["<other-4@example.org>", "<reply-3@example.org>"]
    assert more is True
    assert mail.get_sent(days_back=14)[0]["internetMessageId"] == "<reply-2@example.edu>"
    assert mail.get_messages("archive", days_back=14) == []        # older than the window


def test_get_conversation_reads_the_thread_across_mailboxes_oldest_first(mail):
    conv = na.conversation_handle("root-1@example.org")
    msgs, truncated = mail.get_conversation_messages(conv)
    assert [m["internetMessageId"] for m in msgs] == [
        "<root-1@example.org>", "<reply-2@example.edu>", "<reply-3@example.org>"]
    assert truncated is False
    assert msgs[1]["mailbox"]["role"] == "sentitems" and msgs[1]["body"]["content"] == "Yes, Thursday works."
    two, truncated = mail.get_conversation_messages(conv, top=2)
    assert len(two) == 2 and truncated is True
    assert mail.conversation_latest_message(conv) == {
        "from": "alex@example.org", "recipients": [ME]}
    with pytest.raises(na.NativeError):
        mail.get_conversation_messages("AAQkADraw-graph-id")


def test_get_message_finds_the_inbox_copy(mail):
    m = mail.get_message_full(na.message_handle("reply-3@example.org"))
    assert m["body"]["content"] == "Great." and m["mailbox"]["role"] == "inbox"
    with pytest.raises(na.NativeError) as e:
        mail.get_message_full(na.message_handle("nobody@example.org"))
    assert e.value.code == "not-found"


# ── drafts ───────────────────────────────────────────────────────────

def _drafts(fake):
    return fake.accounts["Work"]["mailboxes"]["Drafts"]


def test_create_draft_saves_to_mail_and_keeps_the_eas_handle(mail, fake, priv):
    d = mail.create_draft(["pi@example.org"], "Site visit", "Does Tuesday suit you?", cc=["kim@example.org"])
    assert d["id"].startswith("nd_") and mail_mediation.is_plain_id(d["id"]) and d["isDraft"] is True
    assert d["toRecipients"] == [{"emailAddress": {"name": "", "address": "pi@example.org"}}]
    assert [r["subject"] for r in _drafts(fake)] == ["Site visit"]
    reg = json.loads((priv / na.DRAFTS_FILE).read_text())
    assert reg[d["id"]]["mail_message_id"] == _drafts(fake)[0]["message_id"]
    assert oct((priv / na.DRAFTS_FILE).stat().st_mode & 0o777) == "0o600"
    assert mail.get_message_full(d["id"])["body"]["content"] == "Does Tuesday suit you?"
    for bad in ({"body": "  "}, {"to": []}, {"content_type": "HTML"}):
        args = {"to": ["pi@example.org"], "subject": "s", "body": "b", **bad}
        with pytest.raises(na.NativeError):
            mail.create_draft(**args)


def test_create_reply_answers_everyone_but_the_owner_in_the_same_thread(mail, fake):
    d = mail.create_reply(na.message_handle("root-1@example.org"), "Thursday at 2 works.")
    assert [r["emailAddress"]["address"] for r in d["toRecipients"]] == ["alex@example.org"]
    assert [r["emailAddress"]["address"] for r in d["ccRecipients"]] == ["kim@example.org"]
    assert d["subject"] == "Re: Budget review"
    assert d["conversationId"] == na.conversation_handle("root-1@example.org")
    saved = _drafts(fake)[0]
    assert saved["headers"]["in-reply-to"] == "<root-1@example.org>"
    assert saved["content"].startswith("Thursday at 2 works.") and "> Can we review" in saved["content"]
    only = mail.create_reply(na.message_handle("root-1@example.org"), "Just you.", reply_all=False)
    assert only["ccRecipients"] == []


def test_update_draft_replaces_the_mail_copy_and_keeps_the_handle(mail, fake):
    d = mail.create_draft(["pi@example.org"], "Site visit", "First text.")
    fp1 = mail.draft_fingerprint(d["id"])
    old_mid = _drafts(fake)[0]["message_id"]
    u = mail.update_draft(d["id"], "Second text.")
    assert u["id"] == d["id"] and u["body"]["content"] == "Second text."
    assert [r["content"] for r in _drafts(fake)] == ["Second text."]
    assert _drafts(fake)[0]["message_id"] != old_mid
    assert mail.draft_fingerprint(d["id"]) != fp1
    mail.set_draft_recipients(d["id"], to=["dean-office@example.org"])
    assert [r["address"] for r in _drafts(fake)[0]["to"]] == ["dean-office@example.org"]
    mail.delete_draft(d["id"])
    assert _drafts(fake) == []
    with pytest.raises(na.NativeError):
        mail.get_message_full(d["id"])


# ── the send gate ────────────────────────────────────────────────────

def test_send_needs_the_fingerprint_and_a_valid_token(mail, fake, tokens):
    d = mail.create_draft(["pi@example.org"], "Site visit", "Does Tuesday suit you?")
    fp = mail.draft_fingerprint(d["id"])
    assert mail.send_draft(d["id"], "tok-1")["status"] == "blocked"            # no fingerprint
    assert mail.send_draft(d["id"], "tok-1", fp)["status"] == "blocked"        # no such token
    assert tokens.calls[-1] == ("tok-1", "send", d["id"], fp)
    tokens.grant("tok-1", "send", "nd_other", fp)                             # another draft's
    assert mail.send_draft(d["id"], "tok-1", fp)["status"] == "blocked"
    assert fake.sent == [] and "send-message" not in fake.calls
    tokens.grant("tok-2", "send", d["id"], fp)
    out = mail.send_draft(d["id"], "tok-2", fp)
    assert out == {"status": "sent", "warnings": []}
    assert [s["subject"] for s in fake.sent] == ["Site visit"]
    assert _drafts(fake) == [] and mail.drafts.get(d["id"]) is None
    assert mail.send_draft(d["id"], "tok-2", fp)["status"] == "blocked"        # token spent


def test_the_resident_never_sends(fake, scope, priv, tokens):
    m = na.NativeMailClient(na.NativeHelper(fake), scope, na.DraftRegistry(priv / na.DRAFTS_FILE),
                            approve=tokens, is_resident=lambda: True, now=lambda: NOW)
    d = m.create_draft(["pi@example.org"], "s", "b")
    fp = m.draft_fingerprint(d["id"])
    tokens.grant("tok", "send", d["id"], fp)
    assert m.send_draft(d["id"], "tok", fp)["status"] == "blocked"
    assert fake.sent == [] and tokens.calls == []


def test_a_draft_changed_after_approval_is_not_sent(mail, fake, tokens):
    d = mail.create_draft(["pi@example.org"], "Site visit", "Approved text.")
    fp = mail.draft_fingerprint(d["id"])
    mail.update_draft(d["id"], "Text changed after the card.")
    tokens.grant("tok", "send", d["id"], fp)
    out = mail.send_draft(d["id"], "tok", fp)
    assert out["status"] == "changed" and out["fingerprint"] != fp
    assert fake.sent == []


def test_a_draft_edited_in_mail_after_approval_is_not_sent(mail, fake, tokens):
    d = mail.create_draft(["pi@example.org"], "Site visit", "Approved text.")
    fp = mail.draft_fingerprint(d["id"])
    _drafts(fake)[0]["content"] = "The owner rewrote this in Mail."
    tokens.grant("tok", "send", d["id"], fp)
    out = mail.send_draft(d["id"], "tok", fp)
    assert out["status"] == "changed" and "edited in Mail" in out["error"]
    assert fake.sent == []
    _drafts(fake).clear()
    tokens.grant("tok2", "send", d["id"], fp)
    assert mail.send_draft(d["id"], "tok2", fp)["status"] == "error"          # draft gone


def test_a_reply_draft_with_mails_quote_still_matches_and_sends(mail, fake, tokens):
    d = mail.create_reply(na.message_handle("root-1@example.org"), "Thursday at 2 works.")
    fp = mail.draft_fingerprint(d["id"])
    tokens.grant("tok", "send", d["id"], fp)
    assert mail.send_draft(d["id"], "tok", fp)["status"] == "sent"
    sent = fake.sent[0]
    assert sent["in_reply_to"] == "<root-1@example.org>" and sent["subject"] == "Re: Budget review"


def test_archive_moves_an_inbox_message_to_the_archive_mailbox(mail, fake):
    out = mail.archive_message(na.message_handle("other-4@example.org"))
    assert out["parentFolderId"] == "Work/Archive"
    boxes = fake.accounts["Work"]["mailboxes"]
    assert "other-4@example.org" in [r["message_id"] for r in boxes["Archive"]]
    with pytest.raises(na.NativeError):
        mail.archive_message(na.message_handle("reply-2@example.edu"))      # a sent copy


# ── calendars ────────────────────────────────────────────────────────

def _events(fake):
    fake.add_event("CAL-WORK", "Group meeting", "2026-10-27T15:00:00-06:00",
                   "2026-10-27T16:00:00-06:00", id="EV-1",
                   organizer={"name": "Lee", "email": "lee@example.edu", "is_current_user": False},
                   attendees=[{"name": "Me", "email": ME, "status": "accepted", "role": "required",
                               "type": "person", "is_current_user": True},
                              {"name": "Room 101", "email": "room101@example.edu", "status": "accepted",
                               "role": "required", "type": "room", "is_current_user": False}])
    fake.add_event("CAL-WORK", "Conference", "2026-10-29T00:00:00-06:00",
                   "2026-10-30T23:59:59-06:00", id="EV-2", all_day=True)
    fake.add_event("CAL-FAM", "School pickup", "2026-10-27T14:00:00-06:00",
                   "2026-10-27T14:30:00-06:00", id="EV-3", availability="free")
    fake.add_event("CAL-PAT", "Pat's seminar", "2026-10-27T10:00:00-06:00",
                   "2026-10-27T11:00:00-06:00", id="EV-4")
    fake.add_event("CAL-OTHER", "Out of scope", "2026-10-27T10:00:00-06:00",
                   "2026-10-27T11:00:00-06:00", id="EV-5")


def test_calendar_view_is_graph_shaped_and_in_scope(cal, fake):
    _events(fake)
    evs = cal.get_calendar_view(days_ahead=14)
    assert [e["subject"] for e in evs] == ["School pickup", "Group meeting", "Conference"]
    meeting = evs[1]
    assert meeting["start"] == {"dateTime": "2026-10-27T21:00:00.0000000", "timeZone": "UTC"}
    assert meeting["showAs"] == "busy" and meeting["isOrganizer"] is False
    assert meeting["responseStatus"] == {"response": "accepted"}
    assert [a["type"] for a in meeting["attendees"]] == ["required", "resource"]
    assert meeting["source_calendar"] == "Calendar" and meeting["source_type"] == "macos"
    assert mail_mediation.is_plain_id(meeting["id"])
    allday = evs[2]
    assert allday["isAllDay"] and allday["start"]["dateTime"] == "2026-10-29T00:00:00.0000000"
    assert allday["end"]["dateTime"] == "2026-10-31T00:00:00.0000000"
    assert evs[0]["showAs"] == "free" and evs[0]["blocks_availability"] is False
    assert cal.get_event(meeting["id"])["subject"] == "Group meeting"
    with pytest.raises(na.NativeError) as e:
        cal.get_event(na.event_handle("EV-5"))
    assert e.value.code == "out-of-scope"


def test_free_busy_reads_own_and_shared_calendars_and_says_why_not_others(cal, fake):
    _events(fake)
    start = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)      # 10:00 local
    end = start + timedelta(hours=6)
    out = cal.get_schedule_detail([ME, "pat@example.edu", "dean@example.edu"], start, end, 60)
    assert out[ME] == {"view": "000002", "error": ""}               # the 15:00 meeting
    assert out["pat@example.edu"] == {"view": "200000", "error": ""}
    assert out["dean@example.edu"]["view"] == "" and "EventKit" in out["dean@example.edu"]["error"]
    assert cal.can_read("PAT@example.edu") and not cal.can_read("dean@example.edu")
    assert cal.get_schedule([ME], start, end, 60) == {ME: "000002"}


def test_create_event_is_gated_and_refuses_what_eventkit_cannot_do(cal, fake, tokens):
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    out = cal.create_event("Review", start, end, attendees=["pi@example.org"], confirm_token="t")
    assert out["status"] == "unsupported" and "attendees" in out["error"]
    assert tokens.calls == []                                       # no token spent on it
    assert cal.create_event("Series", start, end, recurrence={"pattern": {}})["status"] == "unsupported"
    assert cal.create_event("Review", start, end)["status"] == "blocked"
    assert "create-event" not in fake.calls
    tokens.grant("t", "event", "Review")
    ev = cal.create_event("Review", start, end, confirm_token="t")
    assert ev["subject"] == "Review" and ev["showAs"] == "busy" and ev["calendar_id"] == "CAL-WORK"
    blocked = cal.create_event("Family thing", start, end, calendar_id="CAL-FAM", confirm_token="x")
    assert blocked["status"] == "blocked" and "write" in blocked["error"]
    assert cal.create_event("x", start, end, calendar_id="CAL-OTHER")["status"] == "blocked"


def test_a_hold_on_ea_managed_needs_no_token_and_clear_hold_takes_only_holds(cal, fake, tokens):
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    hold = cal.create_event("Hold: site visit", start, start + timedelta(hours=1),
                            hold=True, calendar_id="CAL-EA")
    assert hold["categories"] == [na.EA_HOLD_CATEGORY] and hold["showAs"] == "tentative"
    assert tokens.calls == []
    _events(fake)
    meeting = na.event_handle("EV-1")
    assert cal.clear_hold(meeting)["status"] == "refused"
    assert "EV-1\x1f" in fake.events
    assert cal.clear_hold(hold["id"]) == {"status": "deleted", "id": hold["id"]}
    assert not any(e["title"] == "Hold: site visit" for e in fake.events.values())


def test_a_permission_the_owner_denied_comes_back_as_its_code(cal, fake):
    fake.permissions["calendar"] = "denied"
    with pytest.raises(na.NativeError) as e:
        cal.get_calendar_view()
    assert e.value.code == "permission-denied"


def test_contacts_resolve_a_name_to_an_address(fake, scope):
    fake.contacts = [{"name": "Robin Vale", "organization": "", "emails": ["robin@example.org"]}]
    h = na.NativeHelper(fake)
    assert na.resolve_contact(h, scope, "robin") == [{"name": "Robin Vale", "email": "robin@example.org"}]
    with pytest.raises(na.NativeError):
        na.resolve_contact(h, na.NativeScope(), "robin")


# ── the SAFE / GATED split ───────────────────────────────────────────

def test_every_native_verb_has_the_mediators_class_and_a_method():
    gated = set()
    for verb, (kind, method) in na.NATIVE_VERBS.items():
        klass = na.verb_class(verb)
        assert klass in ("SAFE", "GATED"), verb
        assert callable(getattr(na.NativeMailClient if kind == "mail" else na.NativeCalendarClient,
                                method)), verb
        if klass == "GATED":
            gated.add(verb)
    assert gated == {"send-draft", "create-event"}
    assert na.verb_class("send-everything") == "UNKNOWN"


# ── the doctor ───────────────────────────────────────────────────────

def _rows(**kw):
    return {r["name"]: r for r in na.doctor_rows(**kw)}


def test_doctor_is_one_pass_row_when_no_backend_is_macos(tmp_path):
    rows = na.doctor_rows(config_path=tmp_path / "none.json", scope=None, platform="linux")
    assert [(r["status"], r["name"]) for r in rows] == [("PASS", "native accounts (macOS)")]


def test_doctor_fails_macos_off_a_mac_and_without_a_scope(tmp_path):
    cfg = tmp_path / "ea-config.json"
    cfg.write_text(json.dumps({"mail": {"backend": "macos"}}))
    assert _rows(config_path=cfg, platform="linux")["native accounts (macOS)"]["status"] == "FAIL"
    rows = _rows(config_path=cfg, platform="darwin", scope_file=tmp_path / "missing.json")
    assert rows["native accounts: scope"]["status"] == "FAIL"


def test_doctor_reports_each_permission_account_and_calendar(tmp_path, fake, scope):
    cfg = tmp_path / "none.json"
    h = na.NativeHelper(fake)
    rows = _rows(config_path=cfg, scope=scope, helper=h, platform="darwin",
                 owa_token=lambda: "header.payload.sig")
    assert rows["native accounts: helper"]["status"] == "PASS"
    assert rows["native accounts: availability provider"]["detail"].startswith("owa")
    assert rows["native accounts: outlook-web-auth token"]["status"] == "PASS"
    no_token = _rows(config_path=cfg, scope=scope, helper=h, platform="darwin", owa_token=lambda: None)
    assert no_token["native accounts: outlook-web-auth token"]["status"] == "WARN"
    for what in ("calendar", "mail", "contacts"):
        assert rows[f"native accounts: {what} permission"]["status"] == "PASS"
    assert rows["native accounts: Mail account Work"]["status"] == "PASS"
    assert rows["native accounts: calendars"]["status"] == "PASS"

    fake.permissions["calendar"] = "not-determined"
    fake.permissions["contacts"] = "denied"
    fake.mail_running = False
    rows = _rows(config_path=cfg, scope=scope, helper=h, platform="darwin", owa_token=lambda: "t")
    assert rows["native accounts: calendar permission"]["status"] == "WARN"
    assert "request-access" in rows["native accounts: calendar permission"]["fix"]
    assert rows["native accounts: contacts permission"]["status"] == "FAIL"
    assert "Privacy & Security > Contacts" in rows["native accounts: contacts permission"]["fix"]
    assert rows["native accounts: mail permission"]["status"] == "WARN"

    fake.permissions.update(calendar="authorized", contacts="authorized")
    fake.mail_running = True
    del fake.calendars["CAL-PAT"]
    del fake.accounts["Work"]["mailboxes"]["Archive"]
    rows = _rows(config_path=cfg, scope=scope, helper=h, platform="darwin", owa_token=lambda: "t")
    assert rows["native accounts: calendars"]["status"] == "FAIL"
    assert "Archive" in rows["native accounts: Mail account Work"]["detail"]


def test_doctor_says_when_the_helper_does_not_answer(tmp_path, scope):
    def dead(req):
        raise na.NativeError("timeout", "no agent")
    rows = _rows(config_path=tmp_path / "x.json", scope=scope, helper=na.NativeHelper(dead),
                 platform="darwin", owa_token=lambda: "t")
    assert rows["native accounts: helper"]["status"] == "FAIL"
    assert "build.sh --install" in rows["native accounts: helper"]["fix"]


def test_doctor_script_runs_the_rows(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ea-doctor.py"), "--json",
                        "--ea-dir", str(tmp_path)], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "EA_PRIVATE_DIR": str(tmp_path / "priv")})
    names = {x["name"]: x for x in json.loads(r.stdout)["results"]}
    assert names["native accounts (macOS)"]["status"] == "PASS"


# ── the macOS files ──────────────────────────────────────────────────

def _swift():
    return (HELPER_DIR / "main.swift").read_text()


def _jxa():
    return (HELPER_DIR / "mail.js").read_text()


def test_the_swift_helper_is_present_and_serves_every_non_mail_verb():
    src = _swift()
    for module in ("EventKit", "Contacts", "Foundation", "CoreServices"):
        assert f"import {module}" in src
    for api in ("requestFullAccessToEvents", "AEDeterminePermissionToAutomateTarget",
                "predicateForEvents", "CNContactStore", "/usr/bin/osascript"):
        assert api in src, api
    assert f"let protocolVersion = {na.PROTOCOL_VERSION}" in src
    for verb in set(na.HELPER_VERBS) - na.MAIL_HELPER_VERBS:
        assert f'case "{verb}"' in src, verb
    swift_mail = set(re.findall(r'"([a-z-]+)"', src.split("let mailVerbs: Set<String> = [", 1)[1]
                                .split("]", 1)[0]))
    assert swift_mail == set(na.MAIL_HELPER_VERBS)
    assert "EKParticipant(" not in src                     # EventKit cannot make attendees
    codes = set(re.findall(r'code: "([a-z-]+)"', src))
    assert codes and codes <= na.ERROR_CODES, codes - na.ERROR_CODES


def test_the_jxa_script_serves_exactly_the_mail_verbs_and_never_evaluates_data():
    src = _jxa()
    keys = set(re.findall(r"^  '([a-z-]+)': ", src.split("const VERBS = {", 1)[1], re.M))
    assert keys == set(na.MAIL_HELPER_VERBS)
    assert "fileHandleWithStandardInput" in src and "function run(argv)" in src
    for forbidden in ("doShellScript", "eval(", "Function(", "runScript"):
        assert forbidden not in src, forbidden
    codes = set(re.findall(r"fail\('([a-z-]+)'", src)) | set(re.findall(r"code: '([a-z-]+)'", src))
    assert codes and codes <= na.ERROR_CODES, codes - na.ERROR_CODES


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_jxa_script_is_valid_javascript():
    r = subprocess.run(["node", "--check", str(HELPER_DIR / "mail.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_the_info_plist_carries_the_three_usage_descriptions():
    info = plistlib.loads((HELPER_DIR / "Info.plist").read_bytes())
    for key in ("NSCalendarsFullAccessUsageDescription", "NSContactsUsageDescription",
                "NSAppleEventsUsageDescription"):
        assert info.get(key, "").strip(), key
    assert info["CFBundleIdentifier"] == "org.ea-system.tacitus-native"


def test_the_build_script_embeds_the_plist_and_signs():
    src = (HELPER_DIR / "build.sh").read_text()
    assert subprocess.run(["bash", "-n", str(HELPER_DIR / "build.sh")]).returncode == 0
    for part in ("swiftc", "__info_plist", "codesign", "macos14.0", "launchctl bootstrap"):
        assert part in src, part
    assert os.access(HELPER_DIR / "build.sh", os.X_OK)


def test_the_launch_agent_runs_the_helper_in_the_owners_session():
    text = (HELPER_DIR / "org.ea-system.tacitus-native.plist.in").read_text()
    text = text.replace("@DEST@", "/usr/local/libexec/tacitus-native") \
        .replace("@QUEUE@", "/Users/owner/.ea-private/native-queue") \
        .replace("@OWNER_HOME@", "/Users/owner")
    agent = plistlib.loads(text.encode())
    assert agent["LimitLoadToSessionType"] == "Aqua"
    assert agent["ProgramArguments"] == ["/usr/local/libexec/tacitus-native/tacitus-native",
                                         "serve", "--queue", "/Users/owner/.ea-private/native-queue"]
    assert str(na.HELPER_BIN) == agent["ProgramArguments"][0]
    assert na.QUEUE_DIR in agent["ProgramArguments"][-1]
