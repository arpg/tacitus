"""The unified mail CLI's reads (issue #76, step 1): one stubbed-client
test per verb and provider. Each checks the normalized shape, the error
dict, and that the flags reach the client."""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import mail_actions  # noqa: E402
import mail_client  # noqa: E402
import mail_mediation as MM  # noqa: E402

OUTLOOK_MSG = {
    "id": "AAMkOUT=", "conversationId": "AAQkCONV=", "subject": "Hello",
    "from": {"emailAddress": {"address": "a@example.edu", "name": "A Person"}},
    "toRecipients": [{"emailAddress": {"address": "me@example.edu"}}],
    "ccRecipients": [], "receivedDateTime": "2026-10-01T12:00:00Z",
    "bodyPreview": "hi", "isRead": False, "isDraft": False,
    "body": {"contentType": "html", "content": "<p>Hi <a href='https://x.test'>link</a></p>"},
}
GMAIL_MSG = {
    "id": "18abc", "thread_id": "18thr", "subject": "Hello",
    "from": '"B Person" <b@example.com>', "to": "me@example.com, c@example.com",
    "cc": "", "date": "Thu, 1 Oct 2026 12:00:00 +0000", "snippet": "hi",
    "label_ids": ["UNREAD", "INBOX"], "body": "plain body",
}


def _stubs(fail=False):
    calls = []
    g = types.ModuleType("graph_client")
    m = types.ModuleType("gmail_client")

    def boom(*_a, **_k):
        raise RuntimeError("graph down")

    def search_messages(query, top=25, folder=None):
        calls.append(("outlook.search", query, top, folder))
        return [OUTLOOK_MSG], False
    g.search_messages = boom if fail else search_messages
    g.get_message_full = boom if fail else (
        lambda mid: calls.append(("outlook.get", mid)) or dict(OUTLOOK_MSG, id=mid))
    g.mark_message_read = boom if fail else (
        lambda mid: calls.append(("outlook.read", mid)) or {})
    g.mark_message_unread = boom if fail else (
        lambda mid: calls.append(("outlook.unread", mid)) or {})

    err = {"error": "Gmail API error: 404"}
    m.search_messages = lambda acct, q, max_results=20: (
        calls.append(("gmail.search", acct, q, max_results)) or ([err] if fail else [GMAIL_MSG]))
    m.get_message = lambda acct, mid: (
        calls.append(("gmail.get", acct, mid)) or (err if fail else dict(GMAIL_MSG, id=mid)))
    m.get_draft = lambda acct, did: (
        calls.append(("gmail.draft", acct, did)) or (err if fail else {
            "to": "x@example.com", "cc": "", "subject": "Re: Hello", "body": "draft"}))
    m.mark_read = lambda acct, mid: (
        calls.append(("gmail.read", acct, mid)) or (err if fail else {"status": "marked_read"}))
    m.mark_unread = lambda acct, mid: (
        calls.append(("gmail.unread", acct, mid)) or (err if fail else {"status": "marked_unread"}))
    sys.modules["graph_client"], sys.modules["gmail_client"] = g, m
    return calls


def _run(*argv, fail=False):
    real = {k: sys.modules.get(k) for k in ("graph_client", "gmail_client")}
    calls = _stubs(fail)
    saved = sys.argv
    sys.argv = ["mail_actions.py", *argv]
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            rc = mail_actions.main()
    finally:
        sys.argv = saved
        for k, mod in real.items():  # later tests in this process get the real clients
            if mod is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = mod
    return rc, json.loads(out.getvalue()), calls


def test_search_outlook_passes_folder_and_normalizes():
    rc, out, calls = _run("search", "--provider", "outlook", "--query", "budget",
                          "--folder", "archive", "--max-results", "5")
    assert rc == 0 and calls == [("outlook.search", "budget", 5, "archive")]
    assert out[0]["thread_id"] == "AAQkCONV=" and out[0]["from"] == "a@example.edu"
    assert out[0]["is_read"] is False and out[0]["provider"] == "outlook"
    assert out[0]["account"] is None


def test_search_gmail_normalizes_and_refuses_a_folder():
    rc, out, calls = _run("search", "--provider", "gmail", "--query", "is:unread",
                          "--account", "me@example.com")
    assert calls == [("gmail.search", "me@example.com", "is:unread", 20)]
    assert out[0]["from"] == "b@example.com"
    assert out[0]["to"] == ["me@example.com", "c@example.com"]
    assert out[0]["is_read"] is False and out[0]["account"] == "me@example.com"
    _, out, calls = _run("search", "--provider", "gmail", "--query", "x",
                         "--account", "me@example.com", "--folder", "inbox")
    assert calls == [] and "label:" in out[0]["error"]


def test_get_message_both_providers():
    _, out, _ = _run("get-message", "--provider", "outlook", "--message-id", "AAMkOUT=")
    assert out["body"] == "Hi link [https://x.test]" and out["id"] == "AAMkOUT="
    _, out, calls = _run("get-message", "--provider", "gmail", "--message-id", "18abc",
                         "--account", "me@example.com")
    assert calls == [("gmail.get", "me@example.com", "18abc")]
    assert out["body"] == "plain body" and out["provider"] == "gmail"


def test_get_draft_keeps_the_raw_body():
    _, out, _ = _run("get-draft", "--provider", "outlook", "--draft-id", "AAMkOUT=")
    assert out["body"].startswith("<p>Hi") and out["body_type"] == "HTML"
    _, out, _ = _run("get-draft", "--provider", "gmail", "--draft-id", "r-1",
                     "--account", "me@example.com")
    assert out["body_type"] == "Text" and out["account"] == "me@example.com"


def test_mark_read_both_providers():
    rc, out, calls = _run("mark-read", "--provider", "outlook", "--message-id", "AAMkOUT=")
    assert rc == 0 and out["status"] == "marked_read" and calls == [("outlook.read", "AAMkOUT=")]
    rc, out, _ = _run("mark-read", "--provider", "gmail", "--message-id", "18abc",
                      "--account", "me@example.com")
    assert rc == 0 and out["account"] == "me@example.com"


def test_mark_unread_both_providers():
    rc, out, calls = _run("mark-unread", "--provider", "outlook", "--message-id", "AAMkOUT=")
    assert rc == 0 and out["status"] == "marked_unread" and calls == [("outlook.unread", "AAMkOUT=")]
    rc, out, calls = _run("mark-unread", "--provider", "gmail", "--message-id", "18abc",
                          "--account", "me@example.com")
    assert rc == 0 and calls == [("gmail.unread", "me@example.com", "18abc")]
    assert out["account"] == "me@example.com"


def test_client_errors_come_back_as_an_error_dict():
    for argv in (("search", "--query", "x"), ("get-message", "--message-id", "AAMkOUT="),
                 ("get-draft", "--draft-id", "AAMkOUT="), ("mark-read", "--message-id", "AAMkOUT="),
                 ("mark-unread", "--message-id", "AAMkOUT=")):
        for provider in ("outlook", "gmail"):
            rc, out, _ = _run(argv[0], "--provider", provider, *argv[1:],
                              "--account", "me@example.com", fail=True)
            row = out[0] if isinstance(out, list) else out
            assert "error" in row, (argv, provider, out)
            if argv[0].startswith("mark-"):
                assert rc == 1


def test_gmail_account_defaults_to_the_profile():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config").mkdir()
        saved = os.environ.get("EA_DIR")
        os.environ["EA_DIR"] = d
        try:
            _, out, _ = _run("get-message", "--provider", "gmail", "--message-id", "18abc")
            assert "No Gmail account" in out["error"]
            (Path(d) / "config" / "user-profile.json").write_text(
                json.dumps({"gmail_accounts": ["first@example.com", "second@example.com"]}))
            _, _, calls = _run("get-message", "--provider", "gmail", "--message-id", "18abc")
            assert calls == [("gmail.get", "first@example.com", "18abc")]
        finally:
            if saved is None:
                os.environ.pop("EA_DIR", None)
            else:
                os.environ["EA_DIR"] = saved


def test_the_reads_are_mediated_and_nothing_else_is():
    assert MM.SAFE["mail_actions"] == {"search", "get-message", "get-draft", "mark-read",
                                       "mark-unread", "create-draft", "create-reply",
                                       "update-draft", "delete-draft"}
    assert "mail_actions" not in MM.GATED
    import mail_mediator
    assert mail_mediator.TOOLS["mail_actions"].name == "mail_actions.py"
    src = (ROOT / "preprocessor" / "mail_actions.py").read_text()
    entry = src[src.index('if __name__ == "__main__":'):]
    # handles are resolved, then the call is forwarded, before main() runs
    assert entry.index("resolve_argv") < entry.index('forward_or_none("mail_actions"') \
        < entry.index("main()")
    # the clients load only inside a verb, after the forward
    assert "\nimport mail_client" not in src


# ---- step 2: draft writes ----------------------------------------------

def _body(text="Hello there.\n"):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(text)
    f.close()
    return f.name


def _write(*argv, gmail_msg=None, mediated=False):
    """Run a write verb with fake provider CLIs; returns (rc, out, runs)."""
    runs = []
    ga = types.ModuleType("graph_actions")
    gm = types.ModuleType("gmail_client")

    ga._resolve_body = lambda a: None

    def _ga_func(a):
        runs.append(("graph_actions", [a.verb, *a.rest]))
        print(json.dumps({"status": "draft_created", "id": "AAMkNEW="}))
    ga.build_parser = lambda: type("P", (), {"parse_args": lambda self, argv: _ns(argv)})()

    def _ns(argv):
        n = types.SimpleNamespace(verb=argv[0], rest=argv[1:])
        n.func = _ga_func
        return n

    def gm_main():
        runs.append(("gmail_client", list(sys.argv[1:]),
                     os.environ.pop("EA_MEDIATED_VERB", None)))
        print(json.dumps({"draft_id": "r-9", "message_id": "m-9", "thread_id": "t-9"}))
    gm.main = gm_main
    gm.get_message = lambda acct, mid: gmail_msg or {}
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



def test_a_ghost_run_gets_a_draft_id_back():
    import codex_ghost
    a = codex_ghost._draft_answer("mail_actions", "create-reply",
                                  ["create-reply", "--provider", "gmail"], "ghostdraft-1")
    assert a["id"] == "ghostdraft-1" and a["provider"] == "gmail"
    a = codex_ghost._draft_answer("mail_actions", "update-draft",
                                  ["update-draft", "--provider", "outlook", "--draft-id", "AAMk="], "")
    assert a == {"status": "updated", "id": "AAMk=", "provider": "outlook", "account": None}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")
