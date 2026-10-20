#!/usr/bin/env python3
"""The draft-to-button chain for Gmail, the DM breadcrumb, and the
promise check.

What must hold: a Gmail draft made through gmail_client.py is journalled
with its system and account and routed to the Gmail Send block; a Gmail id
is read from any of the three tool-output shapes and never from an Outlook
id; every DM-delivered block leaves a pointer in the originating thread;
and a reply that names a button while the run posted none is answered
with a warning, not silence. Nothing here touches the send gate: the only
new mediated verb is a read (list-drafts).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))
import atomic_io as _atomic  # noqa: E402
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-buttons-")

import draft_events as de  # noqa: E402
import button_promises as bp  # noqa: E402

BOT_SRC = (REPO / "ea-chat-bot.py").read_text()


def _fn_src(name: str) -> str:
    """One top-level function's source: up to the next column-0 def or
    decorator (a decorated neighbour would otherwise ride along)."""
    start = BOT_SRC.index(f"\ndef {name}(")
    ends = [i for i in (BOT_SRC.find("\ndef ", start + 1), BOT_SRC.find("\n@", start + 1)) if i > 0]
    return BOT_SRC[start:min(ends)]


# --- journal: Gmail records ------------------------------------------------------

def test_record_carries_system_and_account():
    with tempfile.TemporaryDirectory() as td:
        de.JOURNAL = Path(td) / "draft-events.jsonl"
        de.record("created", "r6253984462940915209", verb="create-draft",
                  system="gmail", email="kit.kowalczyk1@example.com")
        de.record("created", "AAMkOUTLOOK=", verb="create-reply")
        evs = de.read_since(0)
        assert evs[0]["system"] == "gmail" and evs[0]["email"] == "kit.kowalczyk1@example.com"
        assert "system" not in evs[1] and "email" not in evs[1]
        assert de.is_gmail(evs[0]) and not de.is_gmail(evs[1])
        assert de.system_of(evs, "r6253984462940915209") == ("gmail", "kit.kowalczyk1@example.com")
        assert de.system_of(evs, "AAMkOUTLOOK=") == ("outlook", "")
        # An unstamped record with a Gmail-shaped id is still Gmail.
        assert de.system_of([{"draft_id": "r123456789", "action": "created"}],
                            "r123456789") == ("gmail", "")
        assert de.system_of([], "r123456789") == ("gmail", "")


def test_a_draft_names_the_ledger_rows_it_delivers():
    """--ledger-id on a draft verb stamps the record; a draft's create and
    update records add up to its rows, and a value that is not a row id
    is dropped."""
    with tempfile.TemporaryDirectory() as td:
        de.JOURNAL = Path(td) / "draft-events.jsonl"
        de.record("created", "AAMkD1=", verb="create-reply", ledger_ids=["row-a,row-b"])
        de.record("updated", "AAMkD1=", verb="update-draft",
                  ledger_ids=["row-b", "row-c", "x; rm -rf ~"])
        de.record("created", "AAMkD2=", verb="create-reply")
        evs = de.read_since(0)
        assert evs[0]["ledger_ids"] == ["row-a", "row-b"]
        assert "ledger_ids" not in evs[2]
        assert de.ledger_ids_of("AAMkD1=") == ["row-a", "row-b", "row-c"]
        assert de.ledger_ids_of("AAMkD2=") == [] and de.ledger_ids_of("") == []
    assert de.parse_ledger_ids("response-syn-qb01, kept-AAQk:1") == [
        "response-syn-qb01", "kept-AAQk:1"]
    assert de.parse_ledger_ids(None) == [] and de.parse_ledger_ids(["", " , "]) == []


def test_the_outlook_draft_verbs_take_ledger_id():
    import subprocess
    env = dict(os.environ, EA_DIR=tempfile.mkdtemp(prefix="ea-test-ledger-id-"))
    for verb in ("create-draft", "create-reply", "update-draft"):
        out = subprocess.run([sys.executable, str(REPO / "preprocessor" / "graph_actions.py"),
                              verb, "--help"], capture_output=True, text=True, env=env,
                             timeout=60)
        assert "--ledger-id" in out.stdout, (verb, out.stdout[-400:], out.stderr[-400:])


def test_gmail_ids_in_reads_every_tool_shape_and_no_outlook_id():
    mcp_new = '{"id": "r6253984462940915209", "message": {"id": "1a09"}}'
    mcp_old = '{"draftId": "r-123456789012"}'
    cli = '{\n  "draft_id": "r7000000000000000001",\n  "message_id": "1a0926e3604672c5"\n}'
    outlook = '{"status": "reply_draft_created", "id": "AAMkAQQQQQQQQQQQQQAQQQQQQQQQQQ="}'
    prose = "message id 1a0926e3604672c5 and r2d2 and r12345 are not drafts"
    assert de.gmail_ids_in(mcp_new) == ["r6253984462940915209"]
    assert de.gmail_ids_in(mcp_old) == ["r-123456789012"]
    assert de.gmail_ids_in(cli) == ["r7000000000000000001"]
    assert de.gmail_ids_in(outlook) == []
    assert de.gmail_ids_in(prose) == []
    assert de.gmail_ids_in(mcp_new + cli + mcp_new) == ["r6253984462940915209",
                                                        "r7000000000000000001"]
    assert de.gmail_ids_in("") == []


def test_gmail_client_journals_its_draft_mutations():
    """gmail_client.py create-draft / update-draft / delete-draft record to
    the journal (system gmail, the account), with the API stubbed."""
    import gmail_client as gc
    with tempfile.TemporaryDirectory() as td:
        de.JOURNAL = Path(td) / "draft-events.jsonl"
        body = Path(td) / "body.txt"
        body.write_text("Hi Tam,\n\nThe week of the 21st works.\n")
        real = (gc.create_draft, gc.update_draft, gc.delete_draft)
        gc.create_draft = lambda *a, **k: {"draft_id": "r111111111111", "message_id": "m",
                                          "thread_id": "t"}
        gc.update_draft = lambda *a, **k: {"draft_id": "r111111111111", "message_id": "m",
                                          "thread_id": "t"}
        gc.delete_draft = lambda *a, **k: {"status": "deleted", "draft_id": "r111111111111"}
        argv = sys.argv
        try:
            for cmd in (["create-draft", "--email", "kit.kowalczyk1@example.com", "--to", "tam.foxworth75@example.com",
                         "--subject", "Re: Advising", "--body-file", str(body),
                         "--ledger-id", "tam-advising"],
                        ["update-draft", "--email", "kit.kowalczyk1@example.com", "--draft-id",
                         "r111111111111", "--to", "tam.foxworth75@example.com", "--subject", "Re: Advising",
                         "--body-file", str(body)],
                        ["delete-draft", "--email", "kit.kowalczyk1@example.com", "--draft-id",
                         "r111111111111"]):
                sys.argv = ["gmail_client.py", *cmd]
                gc.main()
        finally:
            sys.argv = argv
            gc.create_draft, gc.update_draft, gc.delete_draft = real
        evs = de.read_since(0)
        assert [e["action"] for e in evs] == ["created", "updated", "deleted"], evs
        assert all(e["system"] == "gmail" and e["email"] == "kit.kowalczyk1@example.com" for e in evs)
        assert all(e["draft_id"] == "r111111111111" for e in evs)
        assert evs[0]["ledger_ids"] == ["tam-advising"] and "ledger_ids" not in evs[1]
        # A failed mutation journals nothing.
        gc.create_draft = lambda *a, **k: {"error": "Gmail API error"}
        try:
            sys.argv = ["gmail_client.py", "create-draft", "--email", "kit.kowalczyk1@example.com",
                        "--to", "tam.foxworth75@example.com", "--subject", "x", "--body-file", str(body)]
            gc.main()
        finally:
            sys.argv = argv
            gc.create_draft = real[0]
        assert len(de.read_since(0)) == 3


def test_list_drafts_is_a_registered_read():
    import gmail_client as gc
    import mail_mediation as M
    assert callable(gc.list_drafts)
    assert "list-drafts" in (REPO / "preprocessor" / "gmail_client.py").read_text()
    assert M.is_safe("gmail_client", "list-drafts")
    assert not M.is_gated("gmail_client", "list-drafts")
    # The gate itself is untouched.
    assert M.is_gated("gmail_client", "send-draft")
    assert M.is_gated("graph_actions", "send-draft")
    assert M.is_gated("graph_actions", "send-gmail-draft")


# --- promise check ---------------------------------------------------------------

def test_promised_reads_button_talk_and_ignores_reports_of_earlier_blocks():
    yes = [
        "Understood, no holds. The draft is ready as written; click Send on the :lock: block above and it goes to Tam.",
        ":email: Draft created to Tam re: Advising. The Send button is in your DM.",
        "Two Create buttons are posted for the holds.",
        "Click Schedule when you are ready.",
        "Review the Send/Cancel block and approve.",
        "The confirmation block shows the body that goes out.",
        # the card and code frontends (Zulip)
        "The confirmation card is in your ea-dm stream.",
        "React with :check: on the card to send it.",
        "The confirmation code is in your DM.",
        "Your code is in your DM; reply with it to confirm.",
    ]
    no = [
        ":email: Draft created to Tam Prescott re: advising, week of the 21st.",
        "Archived the three list digests and closed the workshop-paper item.",
        "That draft already has a live Send button from earlier; nothing new to post.",
        "The existing :lock: block still has buttons.",
        "Sent. The email to Nico went out at 15:48.",
        "",
    ]
    for t in yes:
        assert bp.promised(t), t
    for t in no:
        assert not bp.promised(t), t
    assert ":warning:" in bp.WARNING and ":lock:" in bp.WARNING
    assert "no :lock: block or card" in bp.WARNING


# --- bot wiring (source-level; ea-chat-bot.py is not importable) ----------------

def test_bot_captures_gmail_ids_from_tool_results_and_routes_by_system():
    assert "gmail_draft_ids: list = field(default_factory=list)" in BOT_SRC
    assert "gmail_draft_ids=gmail_draft_ids," in BOT_SRC
    assert BOT_SRC.count("_draft_events.gmail_ids_in(") >= 3     # stream, mono, resident
    assert BOT_SRC.count("_post_unjournalled_gmail_drafts(") == 3  # def + mono + resident
    # The old text-only regexes are gone.
    assert "gmail_match" not in BOT_SRC
    # Every path that posts a Send block for a journal record routes by system.
    for name in ("_reconfirm_updated_drafts", "_sweep_late_resident_drafts",
                 "_resident_post_run_detection", "_surface_run_drafts"):
        assert "_draft_events.system_of(" in _fn_src(name), name
    mono = BOT_SRC[BOT_SRC.index("Draft detection via the draft-events journal"):]
    mono = mono[:mono.index("_warn_if_button_promised")]
    assert ('_surface_run_drafts(channel, thread_ts, journal_offset, run_id, "mono",\n'
            '                                tool_calls=cr.tool_calls)') in mono
    surface = _fn_src("_surface_run_drafts")
    assert "_draft_events.system_of(run_events" in surface
    assert "_post_new_draft(" in surface and "_post_new_draft(" in _fn_src("_sweep_late_resident_drafts")


def test_gmail_tool_recognition():
    ns: dict = {}
    src = ("_GMAIL_DRAFT_TOOLS = " + BOT_SRC[BOT_SRC.index("_GMAIL_DRAFT_TOOLS = (") + len("_GMAIL_DRAFT_TOOLS = "):
                                             BOT_SRC.index(")", BOT_SRC.index("_GMAIL_DRAFT_TOOLS = (")) + 1]
           + "\n" + _fn_src("_is_gmail_draft_tool") + "\n" + _fn_src("_called_gmail_draft_tool"))
    exec(src, ns)
    is_tool, called = ns["_is_gmail_draft_tool"], ns["_called_gmail_draft_tool"]
    assert is_tool("mcp__claude_ai_Gmail__create_draft", {"to": "x"})
    assert is_tool("mcp__claude_ai_Gmail__update_draft", {})
    assert is_tool("Bash", {"command": "python3 /home/ea-bot/ea-system/preprocessor/gmail_client.py create-draft --email a --to b"})
    assert not is_tool("Bash", {"command": "python3 .../gmail_client.py search --query x"})
    assert not is_tool("Bash", {"command": "python3 .../graph_actions.py create-reply"})
    assert not is_tool("mcp__claude_ai_Gmail__search_threads", {})
    assert called([("mcp__claude_ai_Gmail__create_draft", 1.0, "", "", {})])
    assert not called([("Bash", 1.0, "", "", {}), ("Read", 0.1, "", "", {})])
    assert not called([])


def test_every_dm_block_leaves_a_pointer_and_is_tallied():
    # The posters: send, schedule, three event kinds, plugin cards.
    send = _fn_src("_post_send_confirmation")
    assert "_post_dm_breadcrumb(" in send and '_note_run_post("send")' in send
    sched = _fn_src("_post_schedule_send_confirmation")
    assert "_post_dm_breadcrumb(" in sched and '_note_run_post("schedule")' in sched
    deliver = _fn_src("_deliver_confirmation")
    assert "_post_dm_breadcrumb(" in deliver and "_note_run_post(kind)" in deliver
    for name, what in (("_post_event_confirmation", "Create"),
                       ("_post_event_update_confirmation", "Reschedule"),
                       ("_post_event_delete_confirmation", "Delete")):
        src = _fn_src(name)
        assert "_deliver_confirmation(" in src and what in src, name
        assert "_resolve_dm_channel()" not in src, f"{name} bypasses the shared delivery"
    assert "_deliver_confirmation(" in _fn_src("_post_plugin_card")
    # The breadcrumb never fires without a thread or when the request came from the DM.
    crumb = _fn_src("_post_dm_breadcrumb")
    assert "if not thread_ts or not dm_channel or not ea_channel or ea_channel == dm_channel" in crumb


def test_promise_check_runs_in_both_lanes_after_detection():
    assert BOT_SRC.count("_begin_run_tally()") == 3          # def + mono + resident
    assert BOT_SRC.count("_warn_if_button_promised(") == 3   # def + mono + resident
    resident = _fn_src("_try_resident_dispatch") if "\ndef _try_resident_dispatch(" in BOT_SRC \
        else BOT_SRC[BOT_SRC.index("_resident_post_run_detection(channel, thread_ts, journal_offset,"):]
    i = resident.index("_resident_post_run_detection(channel, thread_ts, journal_offset,")
    j = resident.index("_warn_if_button_promised(channel, thread_ts, reply")
    assert i < j, "the resident lane must detect before it judges the reply"
    mono_i = BOT_SRC.index('_claim_event_actions(newer_than=run_started),\n                "mono")')
    mono_j = BOT_SRC.index('_warn_if_button_promised(channel, thread_ts, response, "mono")')
    assert mono_i < mono_j
    # A ContextVar, so parallel dispatches keep separate tallies.
    assert 'contextvars.ContextVar("ea_run_posts"' in BOT_SRC


def test_prompts_no_longer_say_the_button_is_in_the_thread():
    assert "so the buttons land in this thread" not in BOT_SRC
    assert "pins the confirmation buttons to THIS conversation" not in BOT_SRC
    primer = (REPO / "preprocessor" / "resident_context.py").read_text()
    assert "in your DM" in primer and "never say the block is above" in primer
    claude_md = (REPO / "CLAUDE.md").read_text()
    assert "posted to the user's DM" in claude_md


def test_security_posture_unchanged():
    """No new send lever anywhere: the deny list still names every send verb,
    the mediator's GATED set is intact, and nothing in the new code calls a
    send verb."""
    import mail_mediation as M
    assert M.GATED["gmail_client"] == {"send-draft"}
    assert {"send-draft", "send-gmail-draft", "schedule-send", "request-confirm",
            "create-event", "update-event", "delete-event"} <= M.GATED["graph_actions"]
    deny = (REPO / "scripts" / "disallowed-tools.txt").read_text()
    for frag in ("graph_actions.py send-draft", "graph_actions.py send-gmail-draft",
                 "gmail_client.py send-draft", "mcp__claude_ai_Gmail__send_message"):
        assert frag in deny, frag
    new_code = "\n".join(_fn_src(n) for n in (
        "_post_new_draft", "_post_unjournalled_gmail_drafts", "_gmail_drafts_since",
        "_deliver_confirmation", "_post_dm_breadcrumb", "_warn_if_button_promised"))
    assert not re.search(r"send-draft|send-gmail-draft|send_message|send_draft\(", new_code)
    gmail_src = (REPO / "preprocessor" / "gmail_client.py").read_text()
    list_fn = gmail_src[gmail_src.index("def list_drafts("):gmail_src.index("def _journal(")]
    assert ".send(" not in list_fn and "drafts().send" not in list_fn


def test_send_claim_is_atomic_and_one_shot():
    """The double-send guard: build a tiny namespace with the registry
    primitives and the claim helper, then prove the first claim wins and a
    second is refused; a real failure restores the draft; an already-gone
    draft is consumed, not retried."""
    import types
    with tempfile.TemporaryDirectory() as td:
        ns = {
            "json": json, "os": os, "time": __import__("time"), "_atomic": _atomic,
            "threading": __import__("threading"),
            "EA_DIR": Path(td), "MAX_CONFIRM_AGE_HOURS": 48,
            "_private_state": lambda name, _d=Path(td): _d / "state" / name,
            "log": types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                                         warning=lambda *a, **k: None, error=lambda *a, **k: None),
            "app": types.SimpleNamespace(client=types.SimpleNamespace(
                chat_update=lambda **k: None, chat_postMessage=lambda **k: {"ts": "1.1"})),
            "_queue_bulk_candidate": lambda *a, **k: None,
        }
        start = BOT_SRC.index("# Draft-confirmation registry")
        end = BOT_SRC.index("def _replace_buttons_with_status")
        exec(compile(BOT_SRC[start:end], "bot[block]", "exec"), ns)
        note, claim = ns["_note_draft_confirmation"], ns["_claim_draft_for_send"]
        mark, state = ns["_mark_draft_confirmation"], ns["_draft_confirmation_state"]
        # A Gmail draft's block, recorded with its system.
        note("rID", "DM1", "1.1", "C1", "", email_system="gmail", gmail_email="kit.kowalczyk1@example.com")
        first = claim("rID")
        assert first and first["email_system"] == "gmail" and first["gmail_email"] == "kit.kowalczyk1@example.com"
        assert claim("rID") is None, "a second claim must be refused"
        assert state("rID") == "sending"
        # A genuine failure returns it to live -> claimable again.
        mark("rID", "live")
        assert claim("rID") is not None
        # Consumed is terminal: never claimable.
        mark("rID", "consumed")
        assert claim("rID") is None and state("rID") == "consumed"
        # The missing-draft classifier.
        miss = ns["_send_error_is_missing"]
        assert miss('<HttpError 404 ... "Requested entity was not found.">')
        assert miss(["ErrorItemNotFound"]) and miss("not found")
        assert not miss("Mailbox is over quota") and not miss("")


def test_bulk_card_routes_gmail_and_claims_each():
    send = _fn_src("handle_approve_bulk_send")
    assert "_claim_draft_for_send(draft_id)" in send, "bulk must claim, not just read status"
    assert 'entry.get("email_system")' in send and "email_system=system" in send
    assert 'entry.get("status") != "live"' not in send, "the pre-claim status read is replaced"


def test_approve_send_claims_before_spawning():
    h = _fn_src("handle_approve_send")
    assert h.index("_claim_draft_for_send(draft_id)") < h.index("_do_send")
    assert "Already sending" in h and "Already sent" in h
    # The token gate is still exactly the same call, unchanged.
    ex = _fn_src("_execute_send")
    assert 'request-confirm", "--action", "send"' in ex
    assert '"send-gmail-draft", "--email", gmail_email' in ex
    assert 'f"--confirm-token={token}"' in ex


def test_a_threadless_send_reports_on_its_card_alone():
    """A briefing card's draft has no thread. Its Sent line goes on the
    card; a second top-level post would open a "Sent." topic on Zulip."""
    ex = _fn_src("_execute_send")
    start = ex.index('if send_data.get("status") == "sent":')
    ok = ex[start:ex.index("return True", start)]
    assert "if thread_ts or not confirm_msg_ts:" in ok
    guard = ok.index("if thread_ts or not confirm_msg_ts:")
    assert ok.index("FRONTEND.post(channel=channel, thread_ts=thread_ts", guard) > guard


def test_a_draft_that_leaves_drafts_outside_its_card_is_retired_with_the_right_line():
    """Sent from Outlook: 'Sent from Outlook.' with the rows it closed on
    the same line (no post in the thread), admissions filing run, and the
    card filed out of the Inbox. Deleted there: 'Deleted in Outlook', also
    filed. Still a draft, or Graph down: untouched. Throttled unless
    forced."""
    import types
    from datetime import datetime, timezone
    ns = {"json": json, "time": __import__("time"), "datetime": datetime,
          "timezone": timezone, "CHANNEL_ID": "stream:4", "_USER_EMAIL": "emerson.vasquez27@example.edu",
          "log": types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                                       warning=lambda *a, **k: None)}
    src = BOT_SRC[BOT_SRC.index("VANISHED_DRAFT_CHECK_S = 300"):BOT_SRC.index("def _sweep_late_resident_drafts")]
    exec(compile(src, "bot[vanished]", "exec"), ns)
    posted = 1_790_000_000.0
    confirms = {
        "dSent": {"status": "live", "channel": "DM", "message_ts": "1", "posted_at": posted},
        "dGone": {"status": "live", "channel": "DM", "message_ts": "2", "posted_at": posted},
        "dLive": {"status": "live", "channel": "DM", "message_ts": "3", "posted_at": posted},
        "dDown": {"status": "live", "channel": "DM", "message_ts": "4", "posted_at": posted},
        "dDone": {"status": "consumed", "channel": "DM", "message_ts": "5", "posted_at": posted},
        "dGmail": {"status": "live", "channel": "DM", "message_ts": "6", "posted_at": posted,
                   "email_system": "gmail"},
    }
    convs = {"1": "cSent", "2": "cGone", "3": "cLive", "4": "cDown"}
    later = "2026-09-22T12:00:00Z"   # after `posted` (2026-09-21 14:13Z)

    def graph_json(*argv):
        if argv[0] == "get-message":
            return {"dLive": {"id": "dLive"}, "dDown": {"error": "503 Service Unavailable"}}.get(
                argv[2], {"error": "Could not fetch message: 404 Client Error"})
        if argv[2] == "cSent":
            return {"messages": [{"from": {"address": "nico.dalby65@example.edu"}, "sent": "2026-09-01T00:00:00Z"},
                                 {"from": {"address": "emerson.vasquez27@example.edu"}, "sent": later}]}
        return {"messages": [{"from": {"address": "nico.dalby65@example.edu"}, "sent": later}]}

    class Cards:
        def by_ref(self, channel, ts):
            return {"actions": {"approve_send": {"value": {"conversation_id": convs.get(ts, "")}}}}
    lines, marks, closed, filed, moved = [], [], [], [], []

    def close(ch, th, conv, d, **kw):
        closed.append(conv)
        kw["announced"].append("Reply to Nico about the review")

    ns.update({
        "_graph_json": graph_json, "_CARDS": Cards(), "_load_draft_confirms": lambda: confirms,
        "_mark_draft_confirmation": lambda d, st: marks.append((d, st)),
        "_replace_buttons_with_status": lambda ch, ts, text: lines.append((ts, text)),
        "_close_ledger_for_draft": close,
        "_file_admissions_after_send": lambda conv: filed.append(conv),
        "_retire_empty_bulk_cards": lambda: None,
        "_with_closed": lambda s, a: s + "".join(f"\n:white_check_mark: Closed: {d}" for d in a),
        "_card_ref": lambda ch, ts: (ch, ts),
        "FRONTEND": types.SimpleNamespace(file_card=lambda ref: moved.append(ref) or True),
    })
    assert ns["_reconcile_vanished_drafts"](force=True) == 2
    assert dict(lines) == {"1": ":incoming_envelope: Sent from Outlook.\n"
                                ":white_check_mark: Closed: Reply to Nico about the review",
                           "2": ":wastebasket: Deleted in Outlook; not sent."}
    assert sorted(marks) == [("dGone", "consumed"), ("dSent", "consumed")]
    assert closed == ["cSent"] and filed == ["cSent"]
    assert sorted(moved) == [("DM", "1"), ("DM", "2")]
    # throttled: an unforced second call inside the window does nothing
    lines.clear()
    assert ns["_reconcile_vanished_drafts"]() == 0 and lines == []


def test_a_draft_outcome_names_its_closed_rows_once():
    """Each draft outcome closes its rows first and names them on its one
    status line; none posts a 'Closed obligation' per row in the thread."""
    for fn in ("_execute_send", "handle_sent_myself", "handle_draft_handled",
               "_reconcile_vanished_drafts"):
        body = _fn_src(fn)
        i_close = body.index("_close_ledger_for_draft(")
        assert "announced=closed" in body[i_close:i_close + 300], fn
        assert "_with_closed(" in body[i_close:], fn
    row = _fn_src("_close_ledger_row")
    i = row.index("if announced is not None:")
    assert row.index("return", i) < row.index("Closed obligation"), "a collected row posts nothing"
    ns = {"_card_short": lambda label, v: str(v)}
    exec(_fn_src("_with_closed"), ns)
    assert ns["_with_closed"]("Sent.", []) == "Sent."
    assert ns["_with_closed"]("Sent.", ["A", "B"]) == ("Sent.\n:white_check_mark: Closed: A"
                                                        "\n:white_check_mark: Closed: B")


def test_a_send_files_an_applicant_conversation():
    ex = _fn_src("_execute_send")
    ok = ex[ex.index('if send_data.get("status") == "sent":'):]
    assert ok.index("_close_ledger_for_draft(") < ok.index("_file_admissions_after_send(conversation_id)")
    assert 'if email_system != "gmail":' in ok
    assert "_file_admissions_after_send(conversation_id)" in _fn_src("handle_sent_myself")
    f = _fn_src("_file_admissions_after_send")
    assert '"admissions_filter.py"' in f and '"file-conversation"' in f
    assert "_reconcile_vanished_drafts()" in BOT_SRC


def test_a_deleted_draft_retires_its_live_block_and_the_bulk_card():
    """A rewrite that deletes a draft after its block was posted must not
    leave a live Send button (it can only 404). The block becomes a status
    line, the id is finished, and a Send-all card with nothing live left
    is retired too."""
    import types
    with tempfile.TemporaryDirectory() as td:
        updates = []
        ns = {
            "json": json, "os": os, "time": __import__("time"), "_atomic": _atomic,
            "threading": __import__("threading"),
            "EA_DIR": Path(td), "MAX_CONFIRM_AGE_HOURS": 48,
            "_private_state": lambda name, _d=Path(td): _d / "state" / name,
            "log": types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                                         warning=lambda *a, **k: None, error=lambda *a, **k: None),
            "_queue_bulk_candidate": lambda *a, **k: None,
        }
        start = BOT_SRC.index("# Draft-confirmation registry")
        end = BOT_SRC.index("def _replace_buttons_with_status")
        exec(compile(BOT_SRC[start:end], "bot[block]", "exec"), ns)
        # the status replacer and the bulk store, stubbed at the boundary
        def _replace(channel, ts, text):
            updates.append((channel, ts, text)); ns["_retire_confirmation_message"](channel, ts)
        ns["_replace_buttons_with_status"] = _replace
        batches = {"b1": {"ids": ["rA", "rB"], "card_ts": "9.9", "dm_channel": "DM1"}}
        ns["_load_bulk_batches"] = lambda: json.loads(json.dumps(batches))
        ns["_save_bulk_batches"] = lambda data: (batches.clear(), batches.update(data))
        exec(compile(_fn_src("_retire_empty_bulk_cards"), "bot[fn]", "exec"), ns)
        note, retire, state = ns["_note_draft_confirmation"], ns["_retire_deleted_drafts"], ns["_draft_confirmation_state"]
        note("rA", "DM1", "1.1", "C1", "", email_system="gmail", gmail_email="kit.kowalczyk1@example.com")
        note("rB", "DM1", "1.2", "C1", "", email_system="gmail", gmail_email="kit.kowalczyk1@example.com")
        assert retire(["rA"], "test") == 1
        assert state("rA") == "consumed" and state("rB") == "live"
        assert updates[-1][:2] == ("DM1", "1.1") and "deleted" in updates[-1][2]
        assert "b1" in batches, "one live draft keeps the card"
        assert retire(["rB", "rNOPE"], "test") == 1
        assert state("rB") == "consumed"
        assert ("DM1", "9.9") in [u[:2] for u in updates], "the Send-all card is retired"
        assert "b1" not in batches
        assert retire(["rA"], "test") == 0, "idempotent"


def test_deleted_records_are_handled_in_both_lanes_and_the_sweep():
    assert '_retire_deleted_drafts(net["deleted"], label)' in _fn_src("_surface_run_drafts")
    assert '_retire_deleted_drafts(net["deleted"], "resident")' in _fn_src("_resident_post_run_detection")
    sweep = _fn_src("_sweep_late_resident_drafts")
    assert 'ev.get("action") == "deleted"' in sweep and '_retire_deleted_drafts([ev.get("draft_id") or ""], "sweep")' in sweep
    assert 'batches[batch_id]["card_ts"] = card_ts' in _fn_src("_post_bulk_card")



def test_dm_pointer_never_unfurls_the_block_it_points_at():
    """The breadcrumb links to the :lock: block in the DM. Slack unfurls a
    message permalink as a copy of that message, buttons included and
    clickable; a click on the copy edits the breadcrumb and can consume the
    staged payload while the real block stays live. The pointer posts with
    unfurls off, and the click handlers resolve an unfurl click back to the
    message it shows."""
    crumb = _fn_src("_post_dm_breadcrumb")
    assert "unfurl=False" in crumb
    # the Slack frontend resolves an unfurl click to the card it shows
    # (tests/test_slack_frontend.py::test_resolve_card_unfurl_redirect)
    assert "return ev.card" in _fn_src("_clicked_card")
    # nothing reads the clicked container message directly: every handler
    # reads ev.card, which the Slack frontend resolves through the unfurl
    assert 'body["message"]["ts"]' not in BOT_SRC, "a handler still reads the clicked container"
    assert BOT_SRC.count("_clicked_confirmation(body)") == 0
    guard = _fn_src("_reject_if_stale_confirmation")
    assert "card = _clicked_card(ev)" in guard
    assert "_CARDS.posted_at(card)" in guard and "FRONTEND.message_posted_at(card)" in guard


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
