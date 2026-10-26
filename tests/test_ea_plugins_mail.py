"""ea_plugins.mail: plugin mail reads go through the mediated CLI and come
back in Graph's message shape.

The CLI is faked at mail._run_cli, so nothing here touches Graph or the
mediator queue.
"""
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea_plugins import mail  # noqa: E402

SENDER = "notices@journal.example.org"


# A window that reaches the fixture's September rows from today and stops
# well short of its 2020 row, so the tests do not age out.
DAYS = (datetime.now(timezone.utc) - datetime(2026, 9, 1, tzinfo=timezone.utc)).days + 2


def _rows():
    return [
        {"id": "m1", "subject": "Editor Assignment - JRNL-2026-0001",
         "from": SENDER, "received": "2026-09-20T08:00:00Z",
         "has_attachments": False, "conversation_id": "c1",
         "to": ["xan.thornbury33@example.edu"], "cc": [], "preview": "for the following paper"},
        {"id": "m2", "subject": "Reviewer Unavailable",
         "from": SENDER, "received": "2026-09-19T08:00:00Z",
         "has_attachments": False, "conversation_id": "c2",
         "to": ["xan.thornbury33@example.edu"], "cc": [], "preview": "..."},
        {"id": "m3", "subject": "Editor Assignment - JRNL-2020-0009",
         "from": SENDER, "received": "2020-01-01T08:00:00Z",
         "has_attachments": False, "conversation_id": "c3",
         "to": [], "cc": [], "preview": "old"},
        {"id": "m4", "subject": "Spoof", "from": "other@example.com",
         "received": "2026-09-20T09:00:00Z", "has_attachments": False,
         "conversation_id": "c4", "to": [], "cc": [], "preview": ""},
    ]


class FakeCli:
    def __init__(self, rows=None, fail_get=False):
        self.calls = []
        self.rows = _rows() if rows is None else rows
        self.fail_get = fail_get

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "search-mail":
            hint = ("  More results available. Refine --query or use "
                    "--top 500 to see more.\n")
            return hint + json.dumps(self.rows, indent=2) + "\n"
        if argv[0] == "get-message":
            mid = argv[argv.index("--message-id") + 1]
            if self.fail_get:
                return json.dumps({"error": "Could not fetch message: 404",
                                   "message_id": mid})
            return json.dumps({
                "id": mid, "subject": "Editor Assignment - JRNL-2026-0001",
                "from": {"address": SENDER, "name": "Journal System"},
                "to": [], "cc": [], "received": "2026-09-20T08:00:00Z",
                "body": "<p>Manuscript ID JRNL-2026-0001</p>",
                "body_type": "html", "has_attachments": False,
                "importance": "normal", "conversation_id": "c1",
            })
        raise AssertionError(f"unexpected verb {argv}")


def test_extract_json_tolerates_hint_lines():
    doc = mail._extract_json('hint line\n[{"id": "a"}]\n')
    assert doc == [{"id": "a"}]
    assert mail._extract_json("") is None
    try:
        mail._extract_json("no json here")
    except mail.MailError:
        pass
    else:
        raise AssertionError("expected MailError on non-JSON output")


def test_kql_since_is_utc_calendar_day():
    now = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
    assert mail.kql_since(30, now) == "2026-08-22"


def test_messages_from_filters_and_shapes(monkeypatch=None):
    fake = FakeCli()
    orig = mail._run_cli
    mail._run_cli = fake
    try:
        def only_assignments(row):
            return "Editor Assignment" in (row.get("subject") or "")
        msgs = mail.messages_from(SENDER, DAYS, prefilter=only_assignments)
    finally:
        mail._run_cli = orig
    # The search is mailbox-wide, dated, and bound to the sender.
    q = fake.calls[0]
    assert q[0] == "search-mail" and "--folder" not in q
    query = q[q.index("--query") + 1]
    assert query.startswith(f"from:{SENDER} received>=")
    # m2 fails the prefilter, m3 is outside the window, m4 is another sender:
    # exactly one body fetch, for m1.
    gets = [c for c in fake.calls if c[0] == "get-message"]
    assert [c[c.index("--message-id") + 1] for c in gets] == ["m1"]
    assert "--raw-html" in gets[0]
    assert [m["id"] for m in msgs] == ["m1"]
    m = msgs[0]
    assert m["from"]["emailAddress"]["address"] == SENDER
    assert m["from"]["emailAddress"]["name"] == "Journal System"
    assert m["body"]["content"].startswith("<p>")
    assert m["body"]["contentType"] == "html"
    assert m["receivedDateTime"] == "2026-09-20T08:00:00Z"
    assert m["conversationId"] == "c1"
    assert m["toRecipients"] == [{"emailAddress": {"address": "xan.thornbury33@example.edu"}}]


def test_messages_from_without_bodies_skips_get():
    fake = FakeCli()
    orig = mail._run_cli
    mail._run_cli = fake
    try:
        msgs = mail.messages_from(SENDER, DAYS, full_body=False)
    finally:
        mail._run_cli = orig
    assert all(c[0] == "search-mail" for c in fake.calls)
    assert [m["id"] for m in msgs] == ["m1", "m2"]
    assert "body" not in msgs[0]


def test_get_message_error_object_raises():
    fake = FakeCli(fail_get=True)
    orig = mail._run_cli
    mail._run_cli = fake
    try:
        try:
            mail.messages_from(SENDER, DAYS)
        except mail.MailError as e:
            assert "404" in str(e)
        else:
            raise AssertionError("expected MailError")
    finally:
        mail._run_cli = orig


def test_sender_must_be_a_plain_address():
    for bad in ("", "not an address", 'x@y.z" OR received>=2000-01-01'):
        try:
            mail.messages_from(bad, 5)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_search_drops_note_rows():
    orig = mail._run_cli
    mail._run_cli = lambda argv: json.dumps([{"_note": "0 hits in folder"}])
    try:
        assert mail.search("x", folder="inbox") == []
    finally:
        mail._run_cli = orig


def test_include_deleted_adds_deleted_items_once():
    deleted = [
        {"id": "m9", "subject": "Editor Assignment - JRNL-2026-0002", "from": SENDER,
         "received": "2026-09-21T08:00:00Z", "conversation_id": "c9", "to": [], "cc": []},
        {"id": "m1", "subject": "dup of a search hit", "from": SENDER,
         "received": "2026-09-20T08:00:00Z", "conversation_id": "c1", "to": [], "cc": []},
        {"id": "m8", "subject": "someone else", "from": "other@example.com",
         "received": "2026-09-21T09:00:00Z", "conversation_id": "c8", "to": [], "cc": []},
    ]
    fake = FakeCli()
    calls = []

    def cli(argv):
        calls.append(list(argv))
        if argv[0] == "list-mail":
            return json.dumps(deleted)
        return fake(argv)

    orig = mail._run_cli
    mail._run_cli = cli
    try:
        plain = mail.messages_from(SENDER, DAYS, full_body=False)
        both = mail.messages_from(SENDER, DAYS, full_body=False, include_deleted=True)
    finally:
        mail._run_cli = orig
    assert [m["id"] for m in plain] == ["m1", "m2"]
    assert [m["id"] for m in both] == ["m9", "m1", "m2"]
    listing = [c for c in calls if c[0] == "list-mail"]
    assert len(listing) == 1 and listing[0][1:3] == ["--folder", "deleteditems"]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
