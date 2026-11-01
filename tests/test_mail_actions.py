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
    sys.modules["graph_client"], sys.modules["gmail_client"] = g, m
    return calls


def _run(*argv, fail=False):
    calls = _stubs(fail)
    saved = sys.argv
    sys.argv = ["mail_actions.py", *argv]
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            rc = mail_actions.main()
    finally:
        sys.argv = saved
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


def test_client_errors_come_back_as_an_error_dict():
    for argv in (("search", "--query", "x"), ("get-message", "--message-id", "AAMkOUT="),
                 ("get-draft", "--draft-id", "AAMkOUT="), ("mark-read", "--message-id", "AAMkOUT=")):
        for provider in ("outlook", "gmail"):
            rc, out, _ = _run(argv[0], "--provider", provider, *argv[1:],
                              "--account", "me@example.com", fail=True)
            row = out[0] if isinstance(out, list) else out
            assert "error" in row, (argv, provider, out)
            if argv[0] == "mark-read":
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
    assert MM.SAFE["mail_actions"] == {"search", "get-message", "get-draft", "mark-read"}
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")
