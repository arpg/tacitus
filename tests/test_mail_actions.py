"""The unified mail CLI (issue #76). Every verb runs a provider CLI's
verb (mail_actions.DELEGATE); these tests run it with fake provider CLIs
and check the argv each gets and the unified fields added to its output."""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import mail_actions  # noqa: E402
import mail_mediation as MM  # noqa: E402

ME = "me@example.com"
# what graph_actions search-mail prints for one hit, and a folder-miss note
GA_ROW = {"id": "AAMkOUT=", "subject": "Hello", "from": "a@example.edu", "is_draft": False,
          "own": False, "received": "2026-10-01T12:00:00Z", "has_attachments": False,
          "conversation_id": "AAQkCONV=", "to": ["me@example.edu"], "cc": [], "preview": "hi"}
# what graph_actions get-message prints
GA_MSG = {"id": "AAMkOUT=", "subject": "Hello",
          "from": {"name": "A Person", "address": "a@example.edu"},
          "to": [{"name": "", "address": "me@example.edu"}], "cc": [],
          "received": "2026-10-01T12:00:00Z", "body": "<p>Hi</p>", "body_type": "html",
          "body_text": "Hi", "has_attachments": False, "importance": "high",
          "conversation_id": "AAQkCONV="}
# what gmail_client search / get-message print
GM_ROW = {"id": "18abc", "thread_id": "18thr", "subject": "Hello",
          "from": '"B Person" <b@example.com>', "to": "me@example.com, c@example.com",
          "date": "Thu, 1 Oct 2026 12:00:00 +0000", "snippet": "hi",
          "label_ids": ["UNREAD", "INBOX"]}


def _body(text="Hello there.\n"):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(text)
    f.close()
    return f.name


def _write(*argv, gmail_msg=None, mediated=False, ga_out=None, gm_out=None, rc=0):
    """Run a unified verb with fake provider CLIs that print `ga_out` /
    `gm_out` (and exit `rc`); returns (rc, out, runs)."""
    runs = []
    ga = types.ModuleType("graph_actions")
    gm = types.ModuleType("gmail_client")

    ga.run_verb = lambda a: a.func(a)

    def _ga_func(a):
        runs.append(("graph_actions", [a.verb, *a.rest]))
        print(json.dumps(ga_out if ga_out is not None else {"status": "draft_created",
                                                             "id": "AAMkNEW="}))
        if rc:
            sys.exit(rc)
    ga.build_parser = lambda: type("P", (), {"parse_args": lambda self, argv: _ns(argv)})()

    def _ns(argv):
        n = types.SimpleNamespace(verb=argv[0], rest=argv[1:])
        n.func = _ga_func
        return n

    def gm_main():
        runs.append(("gmail_client", list(sys.argv[1:]),
                     os.environ.pop("EA_MEDIATED_VERB", None)))
        print(json.dumps(gm_out if gm_out is not None else {"draft_id": "r-9", "message_id": "m-9",
                                                             "thread_id": "t-9"}))
        if rc:
            sys.exit(rc)
    gm.main = gm_main
    gm.get_message = lambda acct, mid: (gmail_msg or {}) if mid == "18abc" else {"error": mid}
    gm._get_service = lambda acct: object()
    gm._newest_message_in_thread = lambda service, tid: "18abc" if tid == "18thr" else ""
    gm.__file__ = ga.__file__ = __file__
    real = {k: sys.modules.get(k) for k in ("graph_actions", "gmail_client")}
    sys.modules["graph_actions"], sys.modules["gmail_client"] = ga, gm
    try:
        args = mail_actions.build_parser().parse_args(list(argv))
        args._mediated = mediated
        out = io.StringIO()
        with redirect_stdout(out):
            rc = args.func(args)
    finally:
        for k, mod in real.items():
            if mod is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = mod
    return rc, json.loads(out.getvalue()), runs




# ---- the reads ------------------------------------------------------------

def test_search_runs_search_mail_and_keeps_its_fields_and_notes():
    note = {"_note": "0 hits in folder ... NOT evidence"}
    rc, out, runs = _write("search", "--provider", "outlook", "--query", "budget",
                           "--folder", "archive", "--top", "5", ga_out=[GA_ROW, note])
    assert rc == 0 and runs == [("graph_actions", ["search-mail", "--query", "budget",
                                                   "--top", "5", "--folder", "archive"])]
    row = out[0]
    assert row["own"] is False and row["has_attachments"] is False   # search-mail's own fields
    assert row["thread_id"] == row["conversation_id"] == "AAQkCONV="
    assert row["date"] == GA_ROW["received"] and row["provider"] == "outlook"
    assert out[1] == note                                              # its note, untouched


def test_search_gmail_normalizes_and_refuses_a_folder():
    _, out, runs = _write("search", "--provider", "gmail", "--query", "is:unread",
                          "--account", ME, gm_out=[GM_ROW])
    assert runs[0][1] == ["search", "--email", ME, "--query", "is:unread", "--max-results", "20"]
    row = out[0]
    assert row["from"] == "b@example.com" and row["from_name"] == "B Person"
    assert row["to"] == ["me@example.com", "c@example.com"] and row["preview"] == "hi"
    assert row["is_read"] is False and row["conversation_id"] == "18thr"
    assert row["account"] == ME
    rc, out, runs = _write("search", "--provider", "gmail", "--query", "x", "--account", ME,
                           "--folder", "inbox")
    assert rc == 2 and runs == [] and "label:" in out["error"]


def test_get_message_keeps_get_messages_fields():
    _, out, runs = _write("get-message", "--provider", "outlook", "--message-id", "AAMkOUT=",
                          "--raw-html", ga_out=dict(GA_MSG))
    assert runs[0][1] == ["get-message", "--message-id", "AAMkOUT=", "--raw-html"]
    assert out["from"] == "a@example.edu" and out["from_name"] == "A Person"
    assert out["to"] == ["me@example.edu"] and out["importance"] == "high"
    assert out["body_text"] == "Hi" and out["thread_id"] == "AAQkCONV="
    rc, out, runs = _write("get-message", "--provider", "gmail", "--message-id", "18abc",
                           "--raw-html", "--account", ME)
    assert rc == 2 and runs == []
    _, out, runs = _write("get-message", "--provider", "gmail", "--message-id", "18abc",
                          "--account", ME, gm_out=dict(GM_ROW, body="plain body"))
    assert runs[0][1] == ["get-message", "--email", ME, "--message-id", "18abc"]
    assert out["body"] == "plain body" and out["provider"] == "gmail"


def test_get_draft_keeps_the_raw_body():
    _, out, runs = _write("get-draft", "--provider", "outlook", "--draft-id", "AAMkOUT=",
                          ga_out=dict(GA_MSG))
    assert runs[0][1] == ["get-message", "--message-id", "AAMkOUT=", "--raw-html"]
    assert out["body"] == "<p>Hi</p>" and out["body_type"] == "HTML"
    assert out["to"] == "me@example.edu" and out["message_id"] == out["id"] == "AAMkOUT="
    _, out, runs = _write("get-draft", "--provider", "gmail", "--draft-id", "r-1",
                          "--account", ME, gm_out={"to": "x@example.com", "body": "draft"})
    assert runs[0][1] == ["get-draft", "--email", ME, "--draft-id", "r-1"]
    assert out["body_type"] == "Text" and out["account"] == ME


def test_mark_read_and_unread_both_providers():
    for verb, status in (("mark-read", "marked_read"), ("mark-unread", "marked_unread")):
        rc, out, runs = _write(verb, "--provider", "outlook", "--message-id", "AAMkOUT=",
                               ga_out={"message_id": "AAMkOUT="})
        assert rc == 0 and out["status"] == status
        assert runs[0][1] == [verb, "--message-id", "AAMkOUT="]
        rc, out, runs = _write(verb, "--provider", "gmail", "--message-id", "18abc",
                               "--account", ME, gm_out={"status": status, "message_id": "18abc"})
        assert rc == 0 and runs[0][1] == [verb, "--email", ME, "--message-id", "18abc"]


def test_a_provider_error_comes_back_with_a_failing_exit():
    rc, out, _ = _write("get-message", "--provider", "outlook", "--message-id", "AAMkOUT=",
                        ga_out={"error": "Could not fetch message: 404"})
    assert rc == 1 and out["error"].startswith("Could not fetch")
    rc, out, _ = _write("mark-read", "--provider", "gmail", "--message-id", "18abc",
                        "--account", ME, gm_out={"error": "Gmail API error"}, rc=1)
    assert rc == 1 and "error" in out


def test_gmail_account_defaults_to_the_profile():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config").mkdir()
        saved = os.environ.get("EA_DIR")
        os.environ["EA_DIR"] = d
        try:
            rc, out, _ = _write("get-message", "--provider", "gmail", "--message-id", "18abc")
            assert rc == 2 and "No Gmail account" in out["error"]
            (Path(d) / "config" / "user-profile.json").write_text(
                json.dumps({"gmail_accounts": ["first@example.com", "second@example.com"]}))
            _, _, runs = _write("get-message", "--provider", "gmail", "--message-id", "18abc",
                                gm_out=GM_ROW)
            assert runs[0][1][:3] == ["get-message", "--email", "first@example.com"]
        finally:
            if saved is None:
                os.environ.pop("EA_DIR", None)
            else:
                os.environ["EA_DIR"] = saved


def test_the_reads_are_mediated_and_nothing_else_is():
    assert MM.SAFE["mail_actions"] == {"search", "get-message", "get-draft", "mark-read",
                                       "mark-unread", "create-draft", "create-reply",
                                       "update-draft", "delete-draft", "create-forward",
                                       "preflight-check"}
    assert "mail_actions" not in MM.GATED
    import mail_mediator
    assert mail_mediator.TOOLS["mail_actions"].name == "mail_actions.py"
    src = (ROOT / "preprocessor" / "mail_actions.py").read_text()
    entry = src[src.index('if __name__ == "__main__":'):]
    # handles are resolved, then the call is forwarded, before main() runs
    assert entry.index("resolve_argv") < entry.index('forward_or_none("mail_actions"') \
        < entry.index("main()")
    # the provider CLIs load only inside a verb, after the forward
    assert "\nimport graph_actions" not in src and "\nimport gmail_client" not in src



# ---- step 2: draft writes ----------------------------------------------

def test_create_draft_runs_the_provider_verb_with_the_ledger_ids():
    f = _body()
    rc, out, runs = _write("create-draft", "--provider", "outlook", "--to", "a@example.edu",
                           "--subject", "Hi", "--body-file", f, "--ledger-id", "row-1",
                           "--ledger-id", "row-2")
    assert rc == 0 and out["id"] == "AAMkNEW=" and out["provider"] == "outlook"
    assert runs == [("graph_actions", ["create-draft", "--body", "-", "--body-file", f,
                                       "--ledger-id", "row-1", "--ledger-id", "row-2",
                                       "--to", "a@example.edu", "--subject", "Hi"])]
    rc, out, runs = _write("create-draft", "--provider", "gmail", "--account", "me@example.com",
                           "--to", "a@example.com", "--subject", "Hi", "--body-file", f,
                           "--bcc", "b@example.com")
    assert out["id"] == "r-9" and out["thread_id"] == "t-9" and out["account"] == "me@example.com"
    assert runs[0][1] == ["create-draft", "--email", "me@example.com", "--body-file", f,
                          "--to", "a@example.com", "--subject", "Hi", "--bcc", "b@example.com"]


def test_gmail_reply_is_reply_all_unless_sender_only():
    f = _body()
    msg = {"from": "B <b@example.com>", "to": "me@example.com, c@example.com",
           "cc": "d@example.com, B <b@example.com>"}
    _, _, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                        "--message-id", "18abc", "--body-file", f, gmail_msg=msg)
    argv = runs[0][1]
    assert argv[:2] == ["create-draft", "--email"] and "--reply-to-message-id" in argv
    assert argv[argv.index("--cc") + 1] == "c@example.com,d@example.com"
    _, _, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                        "--message-id", "18abc", "--body-file", f, "--sender-only", gmail_msg=msg)
    assert "--cc" not in runs[0][1]
    _, _, runs = _write("create-reply", "--provider", "outlook", "--message-id", "AAMkOUT=",
                        "--conversation-id", "AAQkCONV=", "--body-file", f, "--sender-only")
    assert "--sender-only" in runs[0][1] and "--conversation-id" in runs[0][1]


def test_gmail_reply_skips_a_thread_the_user_answered_last():
    """gmail_client answers the thread's newest message; when that is the
    user's own, the reply would go to the user. The skip is graph_actions's."""
    f = _body()
    own = {"from": "Me <me@example.com>", "to": "b@example.com", "cc": "c@example.com"}
    rc, out, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                           "--thread-id", "18thr", "--body-file", f, gmail_msg=own)
    assert rc == 0 and out["status"] == "skipped" and runs == []
    assert out["latest_sender"] == "me@example.com" and "--force" in out["override"]
    rc, out, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                           "--thread-id", "18thr", "--body-file", f, "--force", gmail_msg=own)
    assert rc == 0 and out["id"] == "r-9"
    assert runs[0][1][runs[0][1].index("--cc") + 1] == "b@example.com,c@example.com"
    rc, out, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                           "--thread-id", "18thr", "--body-file", f, "--sender-only",
                           gmail_msg=own)
    assert out["status"] == "skipped" and runs == []


def test_an_outlook_write_runs_through_graph_actions_run_verb():
    """A throttled mailbox ends the unified verb as it ends graph_actions:
    THROTTLED_EXIT and one plain line on stderr, no traceback."""
    import contextlib
    import importlib
    ga = importlib.import_module("graph_actions")
    runs = []

    def throttled(a):
        runs.append(a.command)
        raise RuntimeError("429 from the fake")
    pargs = ga.build_parser().parse_args(["delete-draft", "--message-id", "AAMkOUT="])
    pargs.func = throttled
    err, code = io.StringIO(), None
    with contextlib.redirect_stderr(err), \
         patch.object(ga.graph, "throttle_line",
                      lambda exc: "Outlook is throttling the mailbox; try later."):
        try:
            ga.run_verb(pargs)
        except SystemExit as e:
            code = e.code
    assert code == ga.THROTTLED_EXIT and runs == ["delete-draft"]
    assert err.getvalue().strip() == "Outlook is throttling the mailbox; try later."
    src = Path(mail_actions.__file__).read_text(encoding="utf-8")
    assert "graph_actions.run_verb(pargs)" in src and "pargs.func(pargs)" not in src


def test_writes_refuse_an_empty_body_and_outlook_only_gaps():
    for argv in (("create-draft", "--provider", "gmail", "--account", "me@example.com",
                  "--to", "a@example.com", "--subject", "Hi"),
                 ("update-draft", "--provider", "outlook", "--draft-id", "AAMkOUT=")):
        rc, out, runs = _write(*argv, "--body-file", _body("  \n"))
        assert rc == 2 and "empty" in out["error"] and runs == []
    rc, out, runs = _write("create-draft", "--provider", "outlook", "--to", "a@example.edu",
                           "--subject", "Hi", "--body-file", _body(), "--bcc", "b@example.edu")
    assert rc == 2 and "--bcc" in out["error"] and runs == []
    rc, out, runs = _write("update-draft", "--provider", "outlook", "--draft-id", "AAMkOUT=",
                           "--body-file", _body(), "--subject", "New")
    assert rc == 2 and runs == []


def test_update_and_delete_name_the_draft_the_provider_way():
    f = _body()
    _, _, runs = _write("update-draft", "--provider", "outlook", "--draft-id", "AAMkOUT=",
                        "--body-file", f)
    assert "--message-id" in runs[0][1] and "AAMkOUT=" in runs[0][1]
    _, out, runs = _write("delete-draft", "--provider", "gmail", "--account", "me@example.com",
                          "--draft-id", "r-1")
    assert runs[0][1] == ["delete-draft", "--email", "me@example.com", "--draft-id", "r-1"]


def test_a_mediated_gmail_write_does_not_take_the_lock_again():
    f = _body()
    _, _, runs = _write("update-draft", "--provider", "gmail", "--account", "me@example.com",
                        "--draft-id", "r-1", "--body-file", f, mediated=True)
    assert runs[0][2] == "update-draft"   # gmail_client's check_child_parse sees its verb
    _, _, runs = _write("update-draft", "--provider", "gmail", "--account", "me@example.com",
                        "--draft-id", "r-1", "--body-file", f)
    assert runs[0][2] is None



def test_every_provider_reply_and_update_option_is_reachable():
    """The provider draft verbs go on the deny list in step 4, so the
    options the EA uses there are here too."""
    f = _body()
    msg = {"from": "b@example.com", "to": "me@example.com, c@example.com", "cc": ""}
    _, _, runs = _write("create-reply", "--provider", "outlook", "--message-id", "AAMkOUT=",
                        "--body-file", f, "--force")
    assert "--force" in runs[0][1]
    _, _, runs = _write("create-reply", "--provider", "gmail", "--account", "me@example.com",
                        "--thread-id", "18thr", "--body-file", f, "--no-quote", "--force",
                        gmail_msg=msg)
    argv = runs[0][1]
    assert argv[argv.index("--thread-id") + 1] == "18thr" and "--no-quote" in argv
    assert argv[argv.index("--cc") + 1] == "c@example.com" and "--force" not in argv
    _, _, runs = _write("create-draft", "--provider", "outlook", "--to", "a@example.edu",
                        "--subject", "Re: new", "--body-file", f, "--force-new-thread")
    assert "--force-new-thread" in runs[0][1]
    _, _, runs = _write("update-draft", "--provider", "gmail", "--account", "me@example.com",
                        "--draft-id", "r-1", "--body-file", f, "--reply-to-message-id", "18abc",
                        "--no-quote")
    argv = runs[0][1]
    assert argv[argv.index("--reply-to-message-id") + 1] == "18abc" and "--no-quote" in argv
    for argv in (("create-reply", "--provider", "outlook", "--thread-id", "t"),
                 ("create-reply", "--provider", "outlook", "--message-id", "AAMk=", "--no-quote"),
                 ("update-draft", "--provider", "outlook", "--draft-id", "AAMk=",
                  "--reply-to-message-id", "x")):
        rc, out, runs = _write(*argv, "--body-file", f)
        assert rc == 2 and "Gmail only" in out["error"] and runs == [], argv


def test_a_ghost_run_gets_a_draft_id_back():
    import codex_ghost
    a = codex_ghost._draft_answer("mail_actions", "create-reply",
                                  ["create-reply", "--provider", "gmail"], "ghostdraft-1")
    assert a["id"] == "ghostdraft-1" and a["provider"] == "gmail"
    a = codex_ghost._draft_answer("mail_actions", "update-draft",
                                  ["update-draft", "--provider", "outlook", "--draft-id", "AAMk="], "")
    assert a == {"status": "updated", "id": "AAMk=", "provider": "outlook", "account": None}



# ---- step 3: create-forward and preflight-check -------------------------

def test_forward_and_preflight_run_the_provider_verbs():
    f = _body()
    _, out, runs = _write("create-forward", "--provider", "outlook", "--message-id", "AAMkOUT=",
                          "--to", "a@example.edu", "--body-file", f, "--ledger-id", "row-1")
    assert runs[0][1] == ["create-forward", "--body", "-", "--body-file", f,
                          "--ledger-id", "row-1", "--message-id", "AAMkOUT=",
                          "--to", "a@example.edu"]
    _, _, runs = _write("create-forward", "--provider", "gmail", "--account", "me@example.com",
                        "--message-id", "18abc", "--to", "a@example.com", "--body-file", f,
                        "--bcc", "b@example.com")
    assert runs[0][1] == ["create-forward", "--email", "me@example.com", "--body-file", f,
                          "--message-id", "18abc", "--to", "a@example.com",
                          "--bcc", "b@example.com"]
    rc, out, runs = _write("create-forward", "--provider", "outlook", "--message-id", "AAMk=",
                           "--to", "a@example.edu", "--body-file", f, "--bcc", "b@example.edu")
    assert rc == 2 and runs == []
    _, out, runs = _write("preflight-check", "--provider", "outlook", "--draft-id", "AAMkOUT=")
    assert runs[0][1] == ["preflight-check", "--message-id", "AAMkOUT="]
    _, out, runs = _write("preflight-check", "--provider", "gmail", "--account", "me@example.com",
                          "--draft-id", "r-1")
    assert runs[0][1] == ["preflight-check", "--email", "me@example.com", "--draft-id", "r-1"]


class _FakeGmailService:
    def __init__(self):
        self.created = []

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId, body):
        self.created.append(body)
        return self

    def execute(self):
        return {"id": "r-new", "message": {"id": "m-new", "threadId": "t-new"}}


def _gmail_lib(**patches):
    """The real gmail_client with its network edges replaced (restored by
    the caller)."""
    import gmail_client
    saved = {k: getattr(gmail_client, k) for k in patches}
    for k, v in patches.items():
        setattr(gmail_client, k, v)
    return gmail_client, saved


def test_the_gmail_forward_carries_the_whole_thread_newest_first():
    import base64
    from email import message_from_bytes
    svc = _FakeGmailService()
    thread = [{"id": "1", "from": "a@example.com", "to": "me@example.com", "cc": "",
               "date": "Mon, 1 Oct 2026", "subject": "Plan", "body": "first message"},
              {"id": "2", "from": "me@example.com", "to": "a@example.com", "cc": "c@example.com",
               "date": "Tue, 2 Oct 2026", "subject": "Re: Plan", "body": "second message",
               "attachments": [{"filename": "plan.pdf", "attachment_id": "att-1"}]},
              # an unsent reply of the user's on the thread is not forwarded
              {"id": "3", "from": "me@example.com", "to": "a@example.com", "cc": "",
               "date": "Wed, 3 Oct 2026", "subject": "Re: Plan", "body": "unsent draft text",
               "label_ids": ["DRAFT"]}]
    gm, saved = _gmail_lib(
        _get_service=lambda email, interactive=False: svc,
        get_message=lambda email, mid: dict(thread[1], thread_id="t-1"),
        get_thread=lambda email, tid: {"thread_id": tid, "messages": thread})
    try:
        out = gm.create_forward("me@example.com", "2", ["x@example.com"], "See below.\n")
    finally:
        for k, v in saved.items():
            setattr(gm, k, v)
    assert out["draft_id"] == "r-new" and out["forwarded"] == 2
    assert out["attachments_not_carried"] == ["plan.pdf"]
    msg = svc.created[0]["message"]
    assert "threadId" not in msg   # a forward opens a new conversation
    m = message_from_bytes(base64.urlsafe_b64decode(msg["raw"]))
    text = m.get_payload(decode=True).decode()
    assert m["Subject"] == "Fwd: Re: Plan" and m["To"] == "x@example.com"
    assert text.startswith("See below.") and text.count(gm.FORWARD_MARKER) == 2
    assert text.index("second message") < text.index("first message")
    assert "Cc: c@example.com" in text and "In-Reply-To" not in m
    assert "unsent draft text" not in text


def test_the_gmail_preflight_checks_the_new_text_like_outlook():
    def draft(body, subject="Re: Plan", to="a@example.com"):
        return lambda email, did, with_fingerprint=False: {
            "to": to, "cc": "", "subject": subject, "body": body, "attachments": []}
    quote = "\n\nOn Mon, 1 Oct 2026 A <a@example.com> wrote:\n> the original\n"
    gm, saved = _gmail_lib(get_draft=draft("Sounds good." + quote))
    try:
        r = gm.preflight_check("me@example.com", "r-1")
        assert r["ok"] and r["draft_summary"]["has_quoted_thread"], r
        gm.get_draft = draft("Sounds good \u2014 see you Thursday Oct 2." + quote)
        r = gm.preflight_check("me@example.com", "r-1")
        assert not r["ok"] and any("em-dash" in e for e in r["errors"])
        assert any("Thursday" in e for e in r["errors"])   # Oct 2 2026 is a Friday
        gm.get_draft = draft(quote.strip())
        r = gm.preflight_check("me@example.com", "r-1")
        assert any("only the quoted thread" in e for e in r["errors"])
        gm.get_draft = draft("Hi.", subject="Re: Plan", to="")
        r = gm.preflight_check("me@example.com", "r-1")
        assert "No recipients (To field is empty)." in r["errors"]
        assert any("no quoted original" in w for w in r["warnings"])
        # the em-dash in the quoted, forwarded thread is someone else's text
        gm.get_draft = draft("FYI.\n\n" + gm.FORWARD_MARKER + "\nFrom: a\n\nold \u2014 text",
                             subject="Fwd: Plan")
        r = gm.preflight_check("me@example.com", "r-1")
        assert r["ok"], r
    finally:
        for k, v in saved.items():
            setattr(gm, k, v)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")


# ---- step 4: the provider verbs are denied ------------------------------

def _parser_opts(mod, verb):
    import argparse
    subs = next(a for a in mod.build_parser()._actions
                if isinstance(a, argparse._SubParsersAction)).choices
    return {o for a in subs[verb]._actions for o in a.option_strings} - {"-h", "--help"}


# (unified verb, provider tool) -> provider options spelled another way here
SPELLED = {
    "--email": "--account", "--body": "--body-file (graph_actions reads the file)",
}
ELSEWHERE = {
    ("get-draft", "graph_actions"): {"--message-id": "--draft-id", "--raw-html": "always on"},
    ("update-draft", "graph_actions"): {"--message-id": "--draft-id"},
    ("delete-draft", "graph_actions"): {"--message-id": "--draft-id"},
    ("preflight-check", "graph_actions"): {"--message-id": "--draft-id"},
    ("create-reply", "gmail_client"): {"--reply-to-message-id": "--message-id"},
    # a Gmail create-draft that answers a message is create-reply
    ("create-draft", "gmail_client"): {"--reply-to-message-id": "create-reply --message-id",
                                       "--thread-id": "create-reply --thread-id",
                                       "--no-quote": "create-reply --no-quote"},
}


def test_every_provider_option_is_on_the_unified_verb():
    """The deny list leaves the unified CLI the only way to these verbs, so
    every option of each provider verb is here, or spelled another way."""
    import gmail_client
    import graph_actions
    mods = {"graph_actions": graph_actions, "gmail_client": gmail_client}
    for verb, by_provider in mail_actions.DELEGATE.items():
        ours = _parser_opts(mail_actions, verb)
        for tool, pverb in by_provider.values():
            missing = _parser_opts(mods[tool], pverb) - ours - set(SPELLED) \
                - set(ELSEWHERE.get((verb, tool), {}))
            assert not missing, (verb, tool, pverb, sorted(missing))


def test_the_new_reply_and_update_options_reach_the_provider():
    f = _body()
    _, _, runs = _write("create-reply", "--provider", "outlook", "--message-id", "AAMkOUT=",
                        "--body-file", f, "--to", "x@example.edu")
    assert runs[0][1][runs[0][1].index("--to") + 1] == "x@example.edu"
    # --to replaces the thread's people on Gmail too: no reply-all lookup
    _, _, runs = _write("create-reply", "--provider", "gmail", "--account", ME, "--message-id",
                        "18zzz", "--body-file", f, "--to", "x@example.com", "--subject", "S",
                        "--bcc", "b@example.com")
    argv = runs[0][1]
    assert argv[argv.index("--to") + 1] == "x@example.com" and "--cc" not in argv
    assert argv[argv.index("--subject") + 1] == "S" and "--bcc" in argv
    _, _, runs = _write("update-draft", "--provider", "gmail", "--account", ME, "--draft-id",
                        "r-1", "--body-file", f, "--thread-id", "18thr", "--bcc", "b@example.com")
    assert "--thread-id" in runs[0][1] and "--bcc" in runs[0][1]
    _, _, runs = _write("get-draft", "--provider", "gmail", "--account", ME, "--draft-id", "r-1",
                        "--with-fingerprint", gm_out={"body": "x"})
    assert runs[0][1][-1] == "--with-fingerprint"
    for argv in (("create-reply", "--provider", "outlook", "--message-id", "AAMk=",
                  "--body-file", f, "--bcc", "b@example.com"),
                 ("update-draft", "--provider", "outlook", "--draft-id", "AAMk=",
                  "--body-file", f, "--thread-id", "t"),
                 ("get-draft", "--provider", "outlook", "--draft-id", "AAMk=",
                  "--with-fingerprint")):
        rc, out, runs = _write(*argv)
        assert rc == 2 and "Gmail only" in out["error"] and runs == [], argv


def _denied():
    lines = (ROOT / "scripts" / "disallowed-tools.txt").read_text().splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def test_every_provider_verb_the_unified_cli_runs_is_denied_directly():
    denied = _denied()
    plugin = (ROOT / "run-plugin.sh").read_text()
    for by_provider in mail_actions.DELEGATE.values():
        for tool, verb in by_provider.values():
            for form in (f"Bash(python3 *{tool}.py {verb} *)", f"Bash(*{tool}.py {verb} *)"):
                assert form in denied, form
            if tool == "graph_actions":
                # a plugin's grant of the verb gives the unified one
                assert f"'{verb}': '" in plugin, verb
    # the verbs only one provider has stay open
    for tool, verb in (("graph_actions", "get-conversation"), ("gmail_client", "get-thread"),
                       ("gmail_client", "attach-file"), ("graph_actions", "snooze-message")):
        assert f"Bash(*{tool}.py {verb} *)" not in denied
    assert "Bash(python3 {script_dir}/preprocessor/mail_actions.py *)," in \
        (ROOT / "ea-chat-bot.py").read_text()
    assert "/preprocessor/mail_actions.py *)" in (ROOT / "run-agent.sh").read_text()


def test_no_ea_facing_text_names_a_denied_provider_verb():
    """A prompt, skill or guide that tells the EA to run a denied verb
    sends it into a refusal."""
    import re
    denied = {pv for by_provider in mail_actions.DELEGATE.values() for pv in by_provider.values()}
    files = [ROOT / "CLAUDE.md", ROOT / "run-agent.sh", ROOT / "ea-chat-bot.py",
             ROOT / "preprocessor" / "resident_context.py",
             ROOT / "preprocessor" / "prompt_logic.py",
             *(ROOT / "docs" / "ea").glob("*.md"), *(ROOT / "skills").glob("*.md"),
             *(ROOT / "prompts").glob("*.md")]
    for path in files:
        text = path.read_text()
        for tool, verb in denied:
            hit = re.search(rf"{tool}\.py[ \\\n]+{re.escape(verb)}\b(?!-)", text)
            assert not hit, (path.name, f"{tool}.py {verb}")
