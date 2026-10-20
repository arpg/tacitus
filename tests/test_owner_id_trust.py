"""Owner processes take no mail id from resident space on trust.

An id the owner reads from a file the resident writes (a staged event, the
draft journal, a ledger row, a deferred send) goes into a Graph path only
when it is a plain mail id; graph_client checks every id it puts into a
path; the mail tools refuse a non-plain id option whoever runs them; an
owner-side draft edit outside the CLI holds the draft lock; and the
Handled button closes nothing while the draft it deletes is still there.
Nothing here reaches the network."""
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-oid-"))
sys.path.insert(0, str(ROOT / "preprocessor"))
import graph_client as gc  # noqa: E402
import mail_mediation as mm  # noqa: E402
import draft_events as de  # noqa: E402

PLAIN = "AAMkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQAAAAAAQQ_QQQQ-QQQQ="
BAD = [PLAIN + "/send", PLAIN + "/attachments/x", "../events/x", PLAIN + " ", " " + PLAIN,
       PLAIN + "?x", PLAIN + "%2F", "", None, ["x"]]
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def _fn_src(name: str) -> str:
    start = BOT_SRC.index(f"\ndef {name}(")
    ends = [i for i in (BOT_SRC.find("\ndef ", start + 1), BOT_SRC.find("\n@", start + 1),
                        BOT_SRC.find("\n# ", start + 1)) if i > 0]
    return BOT_SRC[start:min(ends)]


# ── graph_client: every id in a path ────────────────────────────────────

def test_the_id_pattern_is_the_mediators():
    assert gc._PLAIN_ID_RE.pattern == mm._ID_RE.pattern
    assert gc._LONG_PLAIN_ID_RE.pattern == mm._LONG_ID_RE.pattern


@pytest.mark.parametrize("bad", BAD)
def test_graph_client_refuses_a_non_plain_id_before_any_request(bad, monkeypatch):
    calls = []
    for name in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(gc.requests, name, lambda *a, **k: calls.append(a))
    monkeypatch.setattr(gc, "_headers", lambda: {})
    monkeypatch.setattr(gc, "_validate_library_token", lambda *a: True)
    for fn, args in ((gc.get_message_full, (bad,)), (gc.get_attachments, (bad,)),
                     (gc.update_draft, (bad, "x")), (gc.delete_draft, (bad,)),
                     (gc.set_draft_recipients, (bad, ["a@example.org"])),
                     (gc.get_event, (bad,)), (gc.delete_event, (bad, "tok")),
                     (gc.clear_hold, (bad,)), (gc.download_attachment, (bad, PLAIN)),
                     (gc.download_attachment, (PLAIN, bad))):
        with pytest.raises(ValueError):
            fn(*args)
    if bad:  # an empty reply target makes a new draft, not a reply
        with pytest.raises(ValueError):
            gc.create_draft("s", "body", [], None, bad)
    assert calls == []


def test_a_plain_id_passes(monkeypatch):
    seen = []
    monkeypatch.setattr(gc, "graph_get", lambda ep, *a, **k: seen.append(ep) or {"id": PLAIN})
    gc.get_event(PLAIN)
    gc.get_message_full(PLAIN)
    assert seen == [f"/me/events/{PLAIN}", f"/me/messages/{PLAIN}"]
    assert gc._id("x" * 3000, long=True)


# ── the mail tools refuse a non-plain id option, whoever runs them ──────

def test_id_error_and_its_exemptions():
    assert mm.id_error(["delete-draft", "--message-id", PLAIN]) is None
    assert "plain mail id" in mm.id_error(["delete-draft", "--message-id", PLAIN + "/send"])
    assert "plain mail id" in mm.id_error([f"--event-id={PLAIN}/x"])
    assert mm.id_error(["create-reply", "--conversation-id", ""], allow_empty=True) is None
    assert mm.id_error(["create-reply", "--conversation-id", ""]) is not None
    # request-confirm's --message-id is a token subject, not a mail id.
    mm.refuse_unplain_ids("graph_actions", "request-confirm",
                          ["request-confirm", "--message-id", "acct@example.org:Lunch / talk"])
    with pytest.raises(SystemExit) as e:
        mm.refuse_unplain_ids("graph_actions", "delete-draft",
                              ["delete-draft", "--message-id", PLAIN + "/send"])
    assert e.value.code == 2


@pytest.mark.parametrize("tool", ["graph_actions", "gmail_client", "google_calendar_client"])
def test_each_mail_tool_calls_the_check_after_parsing(tool):
    src = (ROOT / "preprocessor" / f"{tool}.py").read_text()
    main = src[src.rindex("parse_args()"):]
    assert f'refuse_unplain_ids("{tool}"' in main
    assert main.index("check_child_parse") < main.index("refuse_unplain_ids")


# ── the draft journal ──────────────────────────────────────────────────

def test_the_journal_drops_a_record_whose_draft_id_is_not_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "JOURNAL", tmp_path / "draft-events.jsonl")
    lines = [{"action": "created", "draft_id": PLAIN, "run_id": "r1"},
             {"action": "created", "draft_id": PLAIN + "/send", "run_id": "r1"},
             {"action": "created", "draft_id": "../x", "run_id": "r1"},
             {"action": "created", "draft_id": ["x"], "run_id": "r1"},
             ["not", "a", "record"],
             {"action": "created", "draft_id": "r6253984462940915209", "system": "gmail"}]
    de.JOURNAL.write_text("".join(json.dumps(x) + "\n" for x in lines))
    ids = [ev["draft_id"] for _, ev in de.read_records(0)]
    assert ids == [PLAIN, "r6253984462940915209"]
    assert de.known_draft_ids() == {PLAIN, "r6253984462940915209"}
    assert [e["draft_id"] for e in de.read_since(0)] == ids


# ── the bot: staged ids ────────────────────────────────────────────────

def _bot_ns(**extra):
    ns = {"json": json, "Path": Path, "SCRIPT_DIR": ROOT, "re": __import__("re")}
    exec(compile(_fn_src("_plain_mail_id"), "ea-chat-bot.py", "exec"), ns)
    ns.update(extra)
    return ns


def test_the_bots_id_test_is_the_mediators():
    ns = _bot_ns()
    assert ns["_plain_mail_id"](PLAIN) is True          # the import works here
    assert ns["_plain_mail_id"]("r6253984462940915209") is True
    for bad in BAD:
        assert ns["_plain_mail_id"](bad) is False


def test_the_bot_refuses_a_staged_event_id_that_is_not_plain(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    ns = _bot_ns()
    exec(compile(_fn_src("_read_calendar_event"), "ea-chat-bot.py", "exec"), ns)
    for bad in (PLAIN + "/attachments/x", PLAIN + " ", " " + PLAIN, "a b", "../x"):
        assert "not a plain" in ns["_read_calendar_event"](bad)["error"]
        assert "not a plain" in ns["_read_calendar_event"](bad, "acct@example.org")["error"]
    assert "names no event_id" in ns["_read_calendar_event"]("  ")["error"]
    assert ran == []


def test_the_bot_refuses_a_deferred_send_with_a_non_plain_draft_id():
    posts = []
    ns = _bot_ns(FRONTEND=types.SimpleNamespace(post=lambda **k: posts.append(k["text"])),
                 datetime=__import__("datetime").datetime)
    ns["_fetch_draft_body"] = lambda d: pytest.fail("the draft was read")
    exec(compile(_fn_src("_post_schedule_send_confirmation"), "ea-chat-bot.py", "exec"), ns)
    ns["_post_schedule_send_confirmation"]("C1", "1.0", PLAIN + "/send", "2099-01-01T09:00")
    assert posts and "not a plain draft id" in posts[0]


# ── the Handled button ─────────────────────────────────────────────────

@pytest.mark.parametrize("rc,ok", [(0, True), (3, False), (1, False)])
def test_handled_delete_reports_a_busy_lock_as_failure(rc, ok, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(cmd, rc, "{}", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ns = {"SCRIPT_DIR": ROOT}
    exec(compile("HANDLED_DELETE_TIMEOUT_S = 120\n" + _fn_src("_handled_delete_draft"),
                 "ea-chat-bot.py", "exec"), ns)
    got, why = ns["_handled_delete_draft"](PLAIN)
    assert (got == 0) is ok
    if rc == 3:
        assert "being sent" in why
    import draft_lock
    assert seen["timeout"] > draft_lock.WAIT_S + 30


def test_handled_closes_and_archives_only_after_the_delete():
    body = _fn_src("handle_draft_handled")
    assert "timeout=20, cwd=str(SCRIPT_DIR))" not in body.split("archive-conversation")[0]
    i_del = body.index("_handled_delete_draft(draft_id)")
    i_fail = body.index("if rc != 0:")
    i_ret = body.index("return", i_fail)
    i_close = body.index("_close_ledger_for_draft(")
    i_arch = body.index("archive-conversation")
    i_done = body.index("Draft deleted, thread archived.")
    i_retired = body.index('_mark_draft_confirmation(draft_id, "retired")')
    assert i_del < i_fail < i_ret < min(i_close, i_arch, i_done, i_retired)
    assert "@app.action(\"draft_handled\")\ndef handle_draft_handled(" in BOT_SRC


# ── draft_ab: the owner-side edit holds the draft lock ─────────────────

def test_draft_ab_update_holds_the_draft_lock(tmp_path, monkeypatch):
    import draft_ab as ab
    import draft_lock
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    (tmp_path / "private").mkdir(mode=0o700)
    seen = []

    class G:
        def update_draft(self, mid, body):
            # A second holder cannot take the lock while the edit runs.
            with pytest.raises(draft_lock.Busy):
                with draft_lock.hold([mid], wait_s=0):
                    pass
            seen.append((mid, body))

    assert ab._update_locked(G(), PLAIN, "new body") is True
    assert seen == [(PLAIN, "new body")]
    with draft_lock.hold([PLAIN], wait_s=0):
        monkeypatch.setattr(draft_lock, "WAIT_S", 0.0)
        assert ab._update_locked(G(), PLAIN, "other") is False
    assert seen == [(PLAIN, "new body")]
    src = (ROOT / "preprocessor" / "draft_ab.py").read_text()
    assert src.count("graph.update_draft(") == 1, "every update goes through _update_locked"


# ── owner readers of ledger rows ───────────────────────────────────────

def test_ledger_sync_skips_a_row_whose_id_is_not_plain(monkeypatch):
    import ledger_sync as ls
    monkeypatch.setattr(ls, "_graph", lambda: pytest.fail("Graph was called"))
    state, live, note = ls._message_inbox_state(PLAIN + "/send", "inbox")
    assert state == ls.UNKNOWN and live is None and "plain" in note
    assert ls._thread_last_modified(PLAIN + "/move", "") is None


def test_the_dashboard_and_pdf_and_fingerprint_readers_check_ids():
    for f in ("project_dashboard.py", "email_to_pdf.py", "draft_fingerprint.py"):
        assert "is_plain_id(" in (ROOT / "preprocessor" / f).read_text(), f
    import draft_fingerprint as dfp
    with pytest.raises(ValueError):
        dfp.outlook_fingerprint(PLAIN + "/send", types.SimpleNamespace(
            graph_get=lambda *a, **k: pytest.fail("read")))
