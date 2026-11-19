"""The replies that wait, in a chat turn that scans the inbox
(preprocessor/reply_scan.py and its wiring in ea-chat-bot.py).

The shape this pins: the owner sends a long triage request that names
several items, or asks whether anything else needs handling. A reply owed
to a person the owner did not name (an open row, no deadline, one day
old, a briefing run's question for him) must reach the answer:

  1. the message is a scan (prompt_logic module `scan`);
  2. the prompt carries the reply queue, built in code, with that row,
     its ask and the queue's question, and the rule to account for every
     row; a kept thread shows no text;
  3. after the turn, a reply that leaves the row out gets a footer that
     names it; a reply that names the sender or acts on the row gets
     none;
  4. the bot reads the live queue with the replies that came today, a
     project room gets its project's rows and the count of the rest, and
     the footer runs after the reply check and before the usage line.

All people, subjects and ids are made up.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ledger  # noqa: E402
import prompt_logic  # noqa: E402
import reply_scan  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
TODAY = date(2026, 3, 11)
ZONE = ZoneInfo("America/Denver")
OWNER = reply_scan.owner_words("Jordan Ames", "jordan.ames@example.edu")

# The owner's request: eight items by name, none of them the reply below.
REQUEST = (
    "Scan my inbox, there is a lot in it today. 1) the travel form for Priya, "
    "2) the lab budget Dale sent, 3) the syllabus draft, 4) the workshop agenda, "
    "5) the reference letter for Omar, 6) the seminar room booking, "
    "7) the parking permit renewal, 8) the newsletter blurb.")
FOLLOW_UP = "Anything else we should handle?"


def _at(days_ago: int, hour: int = 14) -> str:
    local = datetime(TODAY.year, TODAY.month, TODAY.day, hour, tzinfo=ZONE) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(rid: str, name: str, addr: str, days_ago: int, subject: str, ask: str, *,
         deadline: str = "", queued: dict | None = None, scheduling: str = "none", **over) -> dict:
    """An open ledger row whose thread ends on the other side's message."""
    brief = {"needs_reply": True, "state": "scheduling" if scheduling != "none" else "needs-reply",
             "subject": subject, "ask": ask, "scheduling": scheduling, "deadline": deadline,
             "user_replied_after_latest": False, "last_inbound": _at(days_ago),
             "people": [{"name": name, "role": "sender"}],
             "messages": [{"id": f"m-{rid}", "from": addr, "own": False, "received": _at(days_ago),
                           "gist": f"{name} writes about {subject.lower()}."}]}
    row = {"id": f"response-{rid}", "type": "response_needed", "status": "open",
           "description": f"Needs response: {subject}", "from_email": addr, "from_name": name,
           "source_message_id": f"m-{rid}", "conversation_id": f"conv-{rid}",
           "date_identified": _at(days_ago), "deadline": deadline, "thread_brief": brief}
    if queued:
        row["reply_queue"] = dict(queued, inbound=_at(days_ago))
    row.update(over)
    return row


def _ledger_rows() -> list[dict]:
    return [
        _row("travel", "Priya Natarajan", "priya.n@example.edu", 2, "Travel form for the spring meeting",
             "Sign the travel form.", deadline="2026-03-12"),
        _row("budget", "Dale Whitcomb", "dale.w@example.edu", 3, "Lab budget revision",
             "Approve the revised lab budget.", deadline="2026-03-13"),
        _row("letter", "Omar Haddad", "omar.h@example.org", 4, "Reference letter request",
             "Confirm you will write the letter.", deadline="2026-03-20"),
        # The reply nobody named: one day old, no deadline, a run's question.
        _row("partner", "Wren Calloway", "wren.calloway@example.edu", 1,
             "Partnership office interview request",
             "Confirm whether you will meet for 30 minutes by video.", scheduling="request",
             queued={"outcome": "decision", "at": _at(0, 4),
                     "note": "Decide whether to take the 30-minute interview."}),
    ]


def _queue() -> list[dict]:
    """The queue as `ledger.py reply-queue --min-age 0` prints it."""
    return ledger.reply_backlog(_ledger_rows(), TODAY, zone=ZONE, draft_convs=None, min_days=0)


# ── 1. which messages are a scan ──────────────────────────────────────

def test_a_triage_request_and_anything_else_are_scans():
    for msg in (REQUEST, FOLLOW_UP, "anything else?", "What am I missing today?",
                "go through my inbox", "What else needs my attention?", "what's in the inbox",
                "which replies do I still owe", "list the open replies",
                "Thanks for the draft. Anything else?"):
        assert "scan" in prompt_logic.detect_needed_modules(msg), msg


def test_other_messages_are_not_scans():
    for msg in ("Draft a reply to Priya about the form", "review the email from Dale",
                "Anything else you need from me?", "What else do you need?",
                "Reschedule my 1:1 with Omar", "put it in my inbox",
                "Anything on the agenda for Friday?",
                "I'm still waiting for a reply from Dale about the budget"):
        assert "scan" not in prompt_logic.detect_needed_modules(msg), msg


def test_a_scan_never_takes_the_slim_reply_or_scheduling_prompt():
    """reply_only needs no module beyond drafting; scheduling_only needs
    scheduling alone. A scan module rules both out, so the turn gets the
    main prompt, where the block goes."""
    mods = prompt_logic.detect_needed_modules(
        "Anything else we should handle? Also schedule a meeting with Omar")
    assert {"scan", "scheduling"} <= mods and mods != {"scheduling"}


# ── 2. the block ──────────────────────────────────────────────────────

def test_the_block_carries_the_unnamed_reply_with_its_question():
    queue = _queue()
    assert [e["id"] for e in queue] == ["response-letter", "response-budget", "response-travel",
                                        "response-partner"]
    block, shown, hidden = reply_scan.block(queue)
    assert hidden == 0 and len(shown) == 4
    # A question for the owner comes first; the queue's order holds after it.
    assert [r["id"] for r in shown] == ["response-partner", "response-letter", "response-budget",
                                        "response-travel"]
    first = block.splitlines()[2]
    assert first.startswith("1. [response-partner] Wren Calloway, 1 day: "
                            "Partnership office interview request.")
    assert "Ask: Confirm whether you will meet for 30 minutes by video." in first
    assert "Queue: a question for the user: Decide whether to take" in first
    assert "4 threads wait for the user's reply or decision" in block
    assert reply_scan.RULE in block and "A row with no deadline counts." in block
    assert "A list filtered by due date is not the scan." in block
    assert block.startswith(reply_scan.HEADER) and block.endswith(reply_scan.FOOTER)
    # The thread's gist (the remote model's summary of the mail) stays out.
    assert "writes about" not in block


def test_a_kept_thread_shows_its_subject_and_sender_only():
    row = _row("kept", "Lee Varga", "lee.v@example.org", 3, "Contract question",
               "The secret terms of the deal.", queued={"outcome": "decision", "note": "Secret note"},
               kept_from_frontier="keep list")
    (entry,) = ledger.reply_backlog([row], TODAY, zone=ZONE, min_days=0)
    assert entry["kept"] is True
    block, _shown, _hidden = reply_scan.block([entry])
    assert "Lee Varga, 3 days: Contract question." in block
    assert "Kept from hosted models" in block and "mail_actions.py ask" in block
    assert "secret" not in block.lower()


def test_the_block_is_capped_and_counts_the_rest():
    rows = [dict(e, id=f"response-r{i}", age_days=30 - i) for i, e in
            enumerate([_queue()[0]] * 15)]
    block, shown, hidden = reply_scan.block(rows, owed=44)
    assert len(shown) == reply_scan.BLOCK_CAP and hidden == 44 - reply_scan.BLOCK_CAP
    assert f"And {hidden} more not shown (44 in all): say the count." in block


def test_the_block_stays_small():
    """Twelve rows at every field's cap: 450 bytes a row at most, and the
    header, the count and the rule under 1 kB."""
    long = {"outcome": "decision", "note": "Decide whether to accept the plan as written or ask "
                                            "for changes before the team meets again next week."}
    rows = [dict(_queue()[3], id=f"response-long{i}", description="Needs response: " + "x" * 200,
                 ask="y" * 300, queued=long, from_name=f"Person{i} Surname{i}") for i in range(20)]
    block, shown, _hidden = reply_scan.block(rows)
    assert len(shown) == 12 and len(block.encode()) < 12 * 450 + 1000


def test_an_empty_queue_says_so():
    block, shown, hidden = reply_scan.block([])
    assert (shown, hidden) == ([], 0)
    assert "No thread waits for the user's reply." in block and reply_scan.RULE not in block


def test_the_order_puts_questions_first_and_settled_rows_last():
    q = _queue()[0]
    rows = [dict(q, id="a", queued={"outcome": "left"}), dict(q, id="b", queued={"outcome": "drafted"}),
            dict(q, id="c", queued={}), dict(q, id="d", queued={"outcome": "decision"}),
            dict(q, id="e", queued={"outcome": "escalated"})]
    assert [r["id"] for r in reply_scan.rank(rows)] == ["d", "c", "e", "b", "a"]


def test_a_project_room_gets_its_projects_rows_and_the_count_of_the_rest():
    queue = _queue()
    place = {"response-partner": ["campus-ai"], "response-letter": ["campus-ai", "teaching"]}

    def placer(row):
        if row["id"] == "response-budget":
            raise RuntimeError("no store")
        return place.get(row["id"], [])
    mine, others = reply_scan.for_project(queue, "campus-ai", placer)
    assert [r["id"] for r in mine] == ["response-letter", "response-partner"] and others == 2
    block, _shown, _hidden = reply_scan.block(mine, outside=others)
    assert "2 threads wait in this project" in block
    assert "2 more replies wait outside this project." in block


# ── 3. the footer after the turn ──────────────────────────────────────

# What a turn that filters by due date writes: every named item, and the
# rows with dates as "the other priorities from the scan".
DEADLINE_REPLY = (
    "Done with the scan. Travel form for Priya: signed, due tomorrow. Dale's lab budget "
    "revision: approved. The syllabus draft, workshop agenda, seminar room, parking permit "
    "and newsletter blurb are handled. Other priorities from the scan: the reference letter "
    "for Omar (due Mar 20).")
# The commands of that turn: reads, no act on the unnamed row.
READS = [("Bash", 1.0, "", "", {"command": "python3 /x/preprocessor/graph_actions.py list-mail "
                                           "--folder inbox --top 60"}),
         ("Bash", 1.0, "", "", {"command": "python3 /x/preprocessor/ledger.py list --status open"}),
         ("Bash", 1.0, "", "", {"command": "python3 /x/preprocessor/ledger.py get --id "
                                           "response-partner"})]


def test_a_reply_that_leaves_the_row_out_gets_a_footer_that_names_it():
    _block, shown, hidden = reply_scan.block(_queue())
    missing = reply_scan.unanswered(shown, DEADLINE_REPLY, READS, owner=OWNER)
    assert [r["id"] for r in missing] == ["response-partner"]
    note = reply_scan.footer(missing, hidden=hidden)
    assert note == (":inbox_tray: *Also waiting for your reply:*\n"
                    "- Wren Calloway: Partnership office interview request "
                    "(1 day, needs your answer)")
    assert "\u2014" not in note


def test_no_footer_when_the_reply_names_the_sender():
    _block, shown, _hidden = reply_scan.block(_queue())
    covered = DEADLINE_REPLY + " Wren Calloway asks for a 30-minute video call: take it?"
    assert reply_scan.unanswered(shown, covered, READS, owner=OWNER) == []
    assert reply_scan.footer([]) == ""
    # The surname alone, or the first name beside a word of the subject.
    assert not reply_scan.unanswered(shown, DEADLINE_REPLY + " Calloway waits.", READS, owner=OWNER)
    assert not reply_scan.unanswered(shown, DEADLINE_REPLY + " Wren wants an interview.", READS,
                                     owner=OWNER)
    # The whole subject, or the ledger id.
    assert not reply_scan.unanswered(
        shown, DEADLINE_REPLY + " The partnership office interview request waits.", READS, owner=OWNER)
    assert not reply_scan.unanswered(shown, DEADLINE_REPLY + " (response-partner)", READS, owner=OWNER)


def test_no_footer_when_the_turn_acted_on_the_row():
    _block, shown, _hidden = reply_scan.block(_queue())
    for cmd in ("python3 /x/preprocessor/mail_actions.py create-reply --provider outlook "
                "--message-id m-partner --conversation-id conv-partner --body-file /s/b.txt "
                "--ledger-id response-partner",
                "python3 /x/preprocessor/ledger.py note-reply --id response-partner --outcome decision",
                "python3 /x/preprocessor/ledger.py propose --id response-partner --why done"):
        acted = READS + [("Bash", 1.0, cmd[:600], "", {"command": cmd})]
        assert reply_scan.unanswered(shown, DEADLINE_REPLY, acted, owner=OWNER) == [], cmd


def test_a_first_name_alone_or_the_owners_name_does_not_cover_a_row():
    row = dict(_queue()[3], from_name="Jordan Pike", subject="Grant report figures",
               description="Needs response: Grant report figures")
    # "Jordan" is the owner's name: it names nobody else in a reply to him.
    assert not reply_scan.named(row, "Jordan, here is the scan. Nothing else waits.", OWNER)
    assert reply_scan.named(row, "Pike sent the grant report figures.", OWNER)
    other = dict(row, from_name="Sam Ostrowski")
    assert not reply_scan.named(other, "Sam from the front office called.", OWNER)
    assert reply_scan.named(other, "Sam needs the grant figures.", OWNER)


def test_a_row_a_run_judged_to_need_no_reply_is_never_in_the_footer():
    rows = [dict(_queue()[3], queued={"outcome": "left", "note": "Only informs."})]
    assert reply_scan.unanswered(rows, "Nothing to say.", [], owner=OWNER) == []


def test_the_footer_names_five_and_counts_the_rest():
    q = _queue()[3]
    rows = [dict(q, id=f"response-p{i}", from_name=f"Pat{i} Lee{i}", queued={},
                 scheduling="none", state="needs-reply",
                 description=f"Needs response: Topic {i}", age_days=i) for i in range(7)]
    note = reply_scan.footer(rows, hidden=3)
    lines = note.splitlines()
    assert len(lines) == 1 + reply_scan.FOOTER_CAP + 1
    assert lines[1] == "- Pat0 Lee0: Topic 0 (today, no draft yet)"
    assert lines[-1] == "And 5 more wait for a reply."


def test_a_codex_turn_and_a_claude_turn_are_read_the_same_way():
    """Both harnesses give (name, seconds, summary, result, input)."""
    _block, shown, _hidden = reply_scan.block(_queue())
    cmd = "python3 /x/preprocessor/mail_actions.py create-draft --to w@example.edu --ledger-id response-partner"
    codex = [("Bash", 2.0, cmd, "ok", {"command": cmd})]
    claude_summary_only = [("Bash", 2.0, cmd)]
    for calls in (codex, claude_summary_only):
        assert "response-partner" not in [r["id"] for r in
                                          reply_scan.unanswered(shown, "", calls, owner=OWNER)]


# ── 4. the bot ────────────────────────────────────────────────────────

def _scan_bot(entries, *, scope=None, placer=None):
    start = BOT_SRC.index("def _reply_scan_owner(")
    end = BOT_SRC.index("_CONV_INDEX_CACHE: dict")
    calls = []

    def ledger_cli(*argv):
        calls.append(argv)
        return {"owed": len(entries), "entries": entries} if entries is not None else {}
    ns = {"json": json, "_ledger_cli": ledger_cli, "CHANNEL_ID": "ea",
          "_channel_scope": lambda ch: scope, "_USER_EMAIL": "jordan.ames@example.edu",
          "_profile_path": Path("/nonexistent/user-profile.json"),
          "_resident_io": types.SimpleNamespace(read_text=lambda p: json.dumps({"name": "Jordan Ames"})),
          "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)}
    exec(compile(BOT_SRC[start:end], "bot[reply-scan]", "exec"), ns)
    if placer is not None:
        ns["_reply_scan_placer"] = lambda: placer
    return ns, calls


def test_the_bot_reads_the_live_queue_with_todays_replies():
    ns, calls = _scan_bot(_queue())
    block, shown, hidden = ns["_reply_scan_for"]("ea")
    assert calls == [("reply-queue", "--min-age", "0")]
    assert "[response-partner] Wren Calloway" in block and hidden == 0 and len(shown) == 4
    assert ns["_reply_scan_owner"]() == {"jordan", "ames"}
    ns, _ = _scan_bot(None)
    assert ns["_reply_scan_for"]("ea") == ("", [], 0)


def test_the_bot_marks_a_row_the_mail_records_keep(monkeypatch):
    """The queue's own `kept` reads the row's labels; the bot asks the
    shared check (keep_gate), whose mail records know more. A row kept
    on the machine shows its subject, its sender and its age only."""
    import keep_gate
    monkeypatch.setattr(keep_gate, "_records", lambda: (
        lambda i: "local" if i.get("conversation_id") == "conv-letter" else "full"))
    ns, _ = _scan_bot(_queue())
    block, shown, _hidden = ns["_reply_scan_for"]("ea")
    line = next(ln for ln in block.splitlines() if "[response-letter]" in ln)
    assert "Kept from hosted models: no text here" in line and "Confirm you will write" not in block
    assert "Approve the revised lab budget." in block
    assert [r["id"] for r in shown if r["kept"]] == ["response-letter"]
    # When the check cannot run, every row reads kept.
    monkeypatch.setattr(keep_gate, "mark_kept", lambda rows: (_ for _ in ()).throw(OSError("locked")))
    block, shown, _hidden = ns["_reply_scan_for"]("ea")
    assert all(r["kept"] for r in shown) and "Approve the revised lab budget." not in block


def test_the_bot_slices_the_queue_in_a_project_room_and_falls_back_on_error():
    scope = {"kind": "project", "project": "campus-ai"}
    ns, _ = _scan_bot(_queue(), scope=scope,
                      placer=lambda r: ["campus-ai"] if r["id"] == "response-partner" else [])
    block, shown, _hidden = ns["_reply_scan_for"]("room-1")
    assert [r["id"] for r in shown] == ["response-partner"]
    assert "3 more replies wait outside this project." in block

    def broken():
        raise RuntimeError("store unreadable")
    ns, _ = _scan_bot(_queue(), scope=scope)
    ns["_reply_scan_placer"] = broken
    _block, shown, _hidden = ns["_reply_scan_for"]("room-1")
    assert len(shown) == 4                      # the whole queue, never nothing


def test_the_block_goes_into_both_prompts_and_the_footer_after_the_reply_check():
    turn = BOT_SRC[BOT_SRC.index("\ndef handle_message_async("):]
    assert 'if "scan" in needed_modules and not sched_lane:' in turn
    fresh = turn.index("full_prompt = prompt\n")
    assert turn.rindex("prompt = prompt + \"\\n\\n\" + scan_block", 0, fresh) > 0
    cont = turn.index('prompt = f"""{_PS[\'continuation_header\']}')
    draft_journal = turn.index("# Draft-events journal", cont)
    assert "prompt = prompt + \"\\n\\n\" + scan_block" in turn[cont:draft_journal]
    check, footer = turn.index("reply check skipped"), turn.index("_rs.footer(missing")
    style, usage = turn.index("strip_em_dashes as _strip_em"), turn.index("response += usage_footer")
    assert check < footer < style < usage


def test_the_turn_budget_no_longer_forbids_the_scan():
    turn = BOT_SRC[BOT_SRC.index("\ndef handle_message_async("):]
    budget = turn[turn.index("Do NOT:\n- Read CLAUDE.md"):]
    budget = budget[:budget.index("Plan your tool calls")]
    assert "only search if you need a specific ID" not in budget
    assert "not a simple task: the REPLIES THAT WAIT block" in budget
    line = budget.split("- Search the ledger")[1].split("\n- ")[0]
    assert "\u2014" not in line
