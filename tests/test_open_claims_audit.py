#!/usr/bin/env python3
"""The pre-send open-claim audit (preprocessor/open_claims_audit.py).

A briefing can present an id-less ledger row as open that the mailbox
has already answered. These tests drive the audit against such a
briefing and those rows, with the evidence injected (no Graph), and pin:
which rows count as claimed, what evidence reaches the prompt, how
verdicts are parsed and applied, what the rewrite note says, and how
run-agent.sh wires the step.

Usage:
    python3 tests/test_open_claims_audit.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-audit-"))
os.environ["EA_DIR"] = str(EA_DIR)
for sub in ("config", "state", "logs"):
    (EA_DIR / sub).mkdir(parents=True, exist_ok=True)
(EA_DIR / "config" / "user-profile.json").write_text(json.dumps(
    {"name": "Kit Calloway", "email": "wren.fairbanks84@example.edu"}))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import closure_scrubber as cs  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import open_claims_audit as oca  # noqa: E402

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

BRIEFING_TEXT = """:sunny: *Morning Briefing — Wednesday, Sep 2, 2026*

:dart: *Get done today*
1. *Confirm Vic Okonkwo's search-committee spot*: overdue since Aug 28, the last blocker on posting the Broad Area AI job ad.
2. *Clear the 4 JRNL reviews (0311, 0420, 0533, 0647) + the TJXT invite*: all overdue at once.
3. *RSVP to the Flint Mission Campaign (Mon Sep 8)*: Outlook still shows notResponded.
4. *DOE ASCR mail-in review decision, proposal 0000912870*: due Sep 6.

:leaves: *Flexes*
- Workshop-paper 2026 submission 13 (accepted): camera-ready, registration, reply to Harper.

:white_check_mark: *Quietly handled*: Okonkwo/TPEC teaching section sent.
"""


def _entry(id, type, desc, date, **kw):
    e = {"id": id, "type": type, "description": desc, "from_email": "",
         "from_name": "", "conversation_id": "", "source_message_id": "",
         "gmail_thread_id": "", "date_identified": date, "deadline": "",
         "notes": "", "last_action": "", "priority": "", "status": "open"}
    e.update(kw)
    return e


SEARCH = _entry("ai-search-okonkwo-confirm", "follow_up",
                 "Broad Area AI search committee: explicitly confirm Vic "
                 "Okonkwo's participation (never directly asked; assumed via Gus)",
                 "2026-08-24T02:07:20Z", deadline="2026-08-28")
SPARK = _entry("flint-sep8-rsvp", "action_item",
              "RSVP to the 'Flint Mission Campaign' invite on Mon Sep 8 -- "
              "Outlook shows my_response=notResponded",
              "2026-08-31T15:13:52Z", deadline="2026-09-08")
JRNL = _entry("jrnl-4-reviews-due-20260826", "deadline",
              "4 JRNL manuscript reviews now due at once: 0311, 0420, 0533, 0647",
              "2026-08-26T10:04:28Z")
DOE = _entry("response-doe-016", "response_needed",
             "Needs response: Request to Review DOE (ASCR) Proposal 0000912870",
             "2026-08-20T16:30:02Z", deadline="2026-09-06",
             conversation_id="conv-doe", source_message_id="msg-doe")
WORKSHOP = _entry("workshop-paper-2026-accept-sub13", "task",
              "Workshop-paper 2026 submission 13 ACCEPTED - handle camera-ready, registration, Harper reply",
              "2026-08-05T10:00:00Z", deadline="2026-09-15")
UNRELATED = _entry("northwind-visit-reply", "response_needed",
                   "Needs response: Re: Northwind visit? Kit Holloway wants target dates",
                   "2026-08-31T10:00:00Z")
CONFIRMED = _entry("spring-sym-20-participants", "task",
                   "Spring symposium: participant list draft to Dana Hargrove",
                   "2026-08-04T10:00:00Z", confirmed_open_at="2026-09-01T12:00:00Z")
OPEN = [SEARCH, SPARK, JRNL, DOE, WORKSHOP, UNRELATED, CONFIRMED]
TODAY_TOP = [{"source": "obligation", "ref": "jrnl-4-reviews-due-20260826"},
             {"source": "deadline", "ref": "AAMk-some-mail"},
             {"source": "obligation", "ref": "spring-sym-20-participants"}]


def _sent(subject, to, ts, names=()):
    names = dict(names)
    return {"subject": subject, "sentDateTime": ts, "receivedDateTime": ts,
            "from": {"emailAddress": {"name": "Kit", "address": "wren.fairbanks84@example.edu"}},
            "toRecipients": [{"emailAddress": {"name": names.get(a, ""), "address": a}} for a in to]}


SENT = [
    _sent("Welcome to the AY26-27 AI faculty search committee",
          ["gus.yarborough37@example.edu", "avery.corvino33@example.edu"],
          "2026-08-28T17:24:32Z",
          {"gus.yarborough37@example.edu": "Vic Okonkwo"}),
    _sent("Declined: Flint Mission Campaign", ["finley.stavros43@example.com"], "2026-09-01T16:04:12Z"),
    _sent("Re: Northwind visit?", ["nico.lindqvist80@example.com"], "2026-08-31T18:00:00Z"),
]
CLOSED_ROWS = [
    {"id": f"response-{n}", "type": "response_needed",
     "description": f"Needs response: Reviewer Assignments for Manuscript ID JRNL-2026-{n} are now due",
     "notes": "", "last_action": "2026-09-01: Auto-closed: email archived from inbox by user.",
     "conversation_id": f"conv-{n}", "created_at": "2026-09-01T10:02:25Z",
     "closed_at": "2026-09-01T13:00:05Z"}
    for n in ("0311", "0420", "0533", "0647")
]


# ── claims ───────────────────────────────────────────────────────────

def test_claims_are_the_rows_the_draft_presents_as_open():
    claims = oca.extract_claims(BRIEFING_TEXT, OPEN, TODAY_TOP, now=NOW)
    by = {c["id"]: c["why_mentioned"] for c in claims}
    assert by["jrnl-4-reviews-due-20260826"] == "today_top"
    assert by["ai-search-okonkwo-confirm"].startswith("tokens:"), by
    assert "okonkwo" in by["ai-search-okonkwo-confirm"]
    assert by["flint-sep8-rsvp"].startswith("tokens:"), by
    # a proposal number on a line is a mention by itself
    assert by["response-doe-016"] == "identifier:0000912870", by
    assert by["workshop-paper-2026-accept-sub13"].startswith("tokens:"), by
    # not in the draft at all
    assert "northwind-visit-reply" not in by
    # in today_top, but the user confirmed it open yesterday: not asked
    assert "spring-sym-20-participants" not in by
    # the queue comes first
    assert claims[0]["id"] == "jrnl-4-reviews-due-20260826"


def test_confirmed_open_expires():
    old = dict(CONFIRMED, confirmed_open_at="2026-08-20T12:00:00Z")
    claims = oca.extract_claims("Spring symposium participant list for Dana Hargrove", [old], [], now=NOW)
    assert [c["id"] for c in claims] == ["spring-sym-20-participants"]


def test_snoozed_and_follow_up_rows_are_never_claims():
    rows = [dict(SPARK, status="snoozed"), dict(SEARCH, status="follow_up")]
    assert oca.extract_claims(BRIEFING_TEXT, rows, [], now=NOW) == []


def test_one_common_word_is_not_a_mention():
    """'review' and 'search' alone must not make every row a claim."""
    rows = [_entry("x-1", "task", "Review the search plan for the robotics hire", "2026-08-01T00:00:00Z"),
            _entry("x-2", "task", "Search for a review venue", "2026-08-01T00:00:00Z")]
    claims = oca.extract_claims("Please review the workshop camera-ready.", rows, [], now=NOW)
    assert claims == []


# ── evidence and the prompt ───────────────────────────────────────────

def test_evidence_reaches_only_the_rows_it_concerns():
    idx = cs.build_closed_identifier_index(CLOSED_ROWS)
    kw = dict(inbox=[], sent=SENT, events=[], closed_index=idx, closed_rows=CLOSED_ROWS)
    tv = oca.evidence_for(SEARCH, **kw)
    assert any("Welcome to the AY26-27" in e for e in tv), tv
    su = oca.evidence_for(SPARK, **kw)
    assert any("Declined: Flint" in e for e in su), su
    cs4 = oca.evidence_for(JRNL, **kw)
    assert sum("same id jrnl:" in e for e in cs4) == 4, cs4
    assert oca.evidence_for(DOE, **kw) == []
    assert oca.evidence_for(WORKSHOP, **kw) == []


def test_prepare_asks_only_about_rows_with_evidence_and_writes_the_prompt():
    rec = oca.prepare(agent="morning-briefing", post=BRIEFING_TEXT,
                      briefing={"today_top": TODAY_TOP}, open_entries=OPEN,
                      inbox=[], sent=SENT, events=[], closed_rows=CLOSED_ROWS, now=NOW)
    assert rec["asked"] == 3, rec
    assert {c["id"] for c in rec["claims"]} >= {"ai-search-okonkwo-confirm", "flint-sep8-rsvp",
                                                 "jrnl-4-reviews-due-20260826", "response-doe-016"}
    p = rec["prompt"]
    assert "pre-send check" in p and "morning-briefing" in p
    for must in ("[1] id: jrnl-4-reviews-due-20260826", "Declined: Flint Mission Campaign",
                 "Welcome to the AY26-27", "same id jrnl:0647", '"verdicts"', "You have no tools"):
        assert must in p, must
    # rows without evidence are not in the question
    assert "response-doe-016" not in p and "workshop-paper-2026" not in p
    # STE-shaped: short lines, no em-dashes in the instructions
    assert "—" not in p.split("Items:")[0]


def test_prepare_with_nothing_claimed_asks_nothing():
    rec = oca.prepare(agent="afternoon-catchup", post=":coffee: quiet afternoon",
                      briefing={}, open_entries=OPEN, inbox=[], sent=SENT, events=[],
                      closed_rows=[], now=NOW)
    assert rec["asked"] == 0 and rec["prompt"] == "" and rec["claims"] == []


# ── verdicts ──────────────────────────────────────────────────────────

def test_parse_verdicts_tolerates_fences_and_prose():
    text = ('Here is my answer.\n```json\n{"verdicts": [\n'
            ' {"id": "a", "verdict": "done", "why": "Declined: sent 09-01."},\n'
            ' {"id": "b", "verdict": "OPEN", "why": "no evidence"},\n'
            ' {"id": "c", "verdict": "maybe", "why": "?"},\n'
            ' {"verdict": "done"}\n]}\n```\nThanks.')
    v = oca.parse_verdicts(text)
    assert [(x["id"], x["verdict"]) for x in v] == [("a", "done"), ("b", "open"), ("c", "unsure")]
    assert oca.parse_verdicts("no json here") == []
    assert oca.parse_verdicts("") == []
    assert oca.parse_verdicts('{"verdicts": [{"id": "a", "verdict": "done"}]} trailing }') \
        == [{"id": "a", "verdict": "done", "why": ""}]


# ── apply ─────────────────────────────────────────────────────────────

def _fresh_db():
    db = EA_DIR / "state" / "obligations-ledger.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(ledger_mod.SCHEMA)
    for m in getattr(ledger_mod, "MIGRATIONS", []):
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    now = "2026-09-01T00:00:00Z"
    for e in (SEARCH, SPARK, JRNL, DOE):
        conn.execute("INSERT INTO ledger (id, type, status, description, date_identified, "
                     "deadline, conversation_id, source_message_id, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (e["id"], e["type"], "open", e["description"], e["date_identified"],
                      e["deadline"], e["conversation_id"], "", now, now))
    conn.commit()
    conn.close()
    return db


def _status(db, lid):
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT status, last_action FROM ledger WHERE id=?", (lid,)).fetchone()
    conn.close()
    return row


def _proposal(db, lid) -> dict:
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT metadata FROM ledger WHERE id=?", (lid,)).fetchone()
    conn.close()
    return (json.loads(row[0] or "{}") or {}).get("close_proposal") or {}


def test_apply_proposes_done_rows_logs_them_and_writes_the_rewrite_note():
    """A model verdict is not decisive evidence (closure.py): a 'done' row
    stays open with a proposal the user confirms on the card."""
    db = _fresh_db()
    ledger_mod.DB_PATH = db
    rec = oca.prepare(agent="morning-briefing", post=BRIEFING_TEXT,
                      briefing={"today_top": TODAY_TOP}, open_entries=[SEARCH, SPARK, JRNL, DOE],
                      inbox=[], sent=SENT, events=[], closed_rows=CLOSED_ROWS, now=NOW)
    verdicts = oca.parse_verdicts(json.dumps({"verdicts": [
        {"id": "flint-sep8-rsvp", "verdict": "done", "why": "Declined: Flint Mission Campaign sent 2026-09-01."},
        {"id": "jrnl-4-reviews-due-20260826", "verdict": "done", "why": "All four reminders were closed by the user."},
        {"id": "ai-search-okonkwo-confirm", "verdict": "unsure", "why": "The Welcome mail went to him, but no confirmation back."},
        {"id": "response-doe-016", "verdict": "done", "why": "hallucinated: never asked"},
    ]}))
    log = EA_DIR / "logs" / "open-claims-audit.jsonl"
    lm = EA_DIR / "logs" / "missed-closures.log"
    import log_missed_closure as lmc
    lmc.LOG_PATH = lm
    summary = oca.apply(audit=rec, verdicts=verdicts, close=True, log_missed=True,
                        fetch_for_log=False, log_path=log)
    assert summary["closed"] == ["flint-sep8-rsvp", "jrnl-4-reviews-due-20260826"], summary
    assert summary["unsure"] == ["ai-search-okonkwo-confirm"]
    assert summary["failed"] == [] and summary["rewrite"] is True
    # the ledger rows stay open and carry the audit's proposal; the unasked
    # row is untouched
    assert _status(db, "flint-sep8-rsvp")[0] == "open"
    prop = _proposal(db, "flint-sep8-rsvp")
    assert "pre-send audit (morning-briefing)" in prop["why"]
    assert prop["signal"] == "llm:open-claims-audit"
    assert _status(db, "jrnl-4-reviews-due-20260826")[0] == "open"
    assert _proposal(db, "jrnl-4-reviews-due-20260826")
    assert _status(db, "response-doe-016")[0] == "open" and not _proposal(db, "response-doe-016")
    assert _status(db, "ai-search-okonkwo-confirm")[0] == "open"
    # the missed-closure backlog got both, tagged with the source
    recs = [json.loads(l) for l in lm.read_text().splitlines()]
    assert {r["ledger_id"] for r in recs} == {"flint-sep8-rsvp", "jrnl-4-reviews-due-20260826"}
    assert all(r["source"] == "pre-send-audit" for r in recs)
    # the audit log carries the run
    run = json.loads(log.read_text().splitlines()[-1])
    assert run["closed"] == summary["closed"] and run["agent"] == "morning-briefing"
    # the rewrite note names both kinds
    note = summary["note"]
    assert note.startswith("PRE-SEND AUDIT RESULT")
    assert "flint-sep8-rsvp" in note and "look done" in note
    assert "Looks done (confirm in the card)" in note
    assert "ai-search-okonkwo-confirm" in note and "still open?" in note
    assert "response-doe-016" not in note


def test_apply_with_all_open_changes_nothing():
    db = _fresh_db()
    ledger_mod.DB_PATH = db
    rec = oca.prepare(agent="weekly-audit", post=BRIEFING_TEXT, briefing={},
                      open_entries=[SPARK], inbox=[], sent=SENT, events=[],
                      closed_rows=[], now=NOW)
    summary = oca.apply(audit=rec, verdicts=[{"id": "flint-sep8-rsvp", "verdict": "open", "why": "x"}],
                        close=True, log_missed=False, log_path=EA_DIR / "logs" / "a.jsonl")
    assert summary["closed"] == [] and summary["note"] == "" and summary["rewrite"] is False
    assert _status(db, "flint-sep8-rsvp")[0] == "open"


def test_cli_round_trip_without_graph():
    import subprocess
    db = _fresh_db()
    d = EA_DIR / "cli"
    d.mkdir(exist_ok=True)
    (d / "draft.txt").write_text(BRIEFING_TEXT)
    (d / "briefing.json").write_text(json.dumps({"today_top": TODAY_TOP}))
    env = dict(os.environ, EA_DIR=str(EA_DIR))
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "open_claims_audit.py"),
                        "prepare", "--agent", "morning-briefing", "--post-file", str(d / "draft.txt"),
                        "--briefing", str(d / "briefing.json"), "--out", str(d / "audit.json"),
                        "--prompt-file", str(d / "prompt.txt"), "--no-fetch"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    # no mailbox: no evidence: nothing asked, and the run is still logged
    assert r.stdout.strip() == "0 0", r.stdout
    audit = json.loads((d / "audit.json").read_text())
    assert audit["asked"] == 0 and audit["fetch"].startswith("skipped")
    assert (d / "prompt.txt").read_text() == ""
    (d / "verdicts.txt").write_text("nothing")
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "open_claims_audit.py"),
                        "apply", "--audit", str(d / "audit.json"), "--verdicts-file", str(d / "verdicts.txt"),
                        "--note-file", str(d / "note.txt"), "--no-close"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"asked": 0, "closed": [], "unsure": [], "parked": [], "failed": 0,
                   "rewrite": False}
    assert (d / "note.txt").read_text() == ""


# ── run-agent.sh wiring ───────────────────────────────────────────────

def test_run_agent_wires_the_audit_between_validation_and_the_post():
    src = (REPO / "run-agent.sh").read_text()
    step = src.index("Step 3b: pre-send open-claim audit")
    assert src.index("VALIDATION=$(validate_output") < step < src.index("# Post to Slack via bot token")
    body = src[step:src.index("# Post to Slack via bot token")]
    assert "morning-briefing|afternoon-catchup|weekly-audit)" in body
    assert 'open_claims_audit.py" prepare' in body and 'open_claims_audit.py" apply' in body
    # the question is asked with NO tools, on the cheap model
    assert 'invoke_claude "$AUDIT_PROMPT" "sonnet" "" "$DISALLOWED_TOOLS"' in body
    # the rewrite reuses the agent's own prompt plus the note, once
    assert 'AUDIT_REWRITE_PROMPT="$PROMPT"$\'\\n\\n\'"$(cat "$AUDIT_NOTE")"' in body
    assert body.count("invoke_claude ") == 2
    # a rewrite that fails validation is discarded, never posted
    assert 'validate_output "$AUDIT_OUTPUT" "$AGENT"' in body
    # list-digest never runs it
    assert "list-digest" not in body.split("case \"$AGENT\" in")[1].split("esac")[0]
    # every claude call in the script goes through the same env block, and
    # takes its prompt on stdin (an argument is capped at 128 KiB)
    assert src.count('claude -p --model \\"\\$1\\"') == 3
    assert src.count("printf '%s' \"$") >= 3 and 'claude -p \\"\\$1\\"' not in src
    for prompt in ('"$PROMPT"', '"$RETRY_PROMPT"', '"$1"'):
        assert f"printf '%s' {prompt} | sudo -u ea-bot env" in src, prompt


def _shell_block() -> str:
    """The audit `case` block and the three helpers it calls, verbatim out
    of run-agent.sh, so the shell-level test runs the real thing."""
    src = (REPO / "run-agent.sh").read_text()
    start = src.index("# Step 3b: pre-send open-claim audit")
    start = src.rindex("# ----", 0, start)
    end = src.index("# list-digest: the skill answers", start)
    block = src[start:end]
    helpers = ""
    for name in ("strip_preamble", "validate_output"):
        i = src.index(f"{name}() {{")
        depth, j = 0, i
        while True:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        helpers += src[i:j + 1] + "\n"
    return helpers + block


def test_run_agent_audit_block_end_to_end_with_claude_stubbed():
    """The real shell block, the real prepare/apply CLI, a scratch ledger,
    and `invoke_claude` replaced by a stub: the verdict call returns a
    'done' for the JRNL umbrella, the rewrite call returns a corrected
    draft. The block must close the row, replace CLAUDE_OUTPUT with the
    rewrite, and log the run. A second variant returns an invalid rewrite
    and must keep the original draft."""
    import subprocess
    db = _fresh_db()
    conn = sqlite3.connect(str(db))
    for r in CLOSED_ROWS:
        conn.execute("INSERT INTO ledger (id, type, status, description, last_action, "
                     "conversation_id, created_at, updated_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (r["id"], r["type"], "completed", r["description"], r["last_action"],
                      r["conversation_id"], r["created_at"], r["closed_at"], r["closed_at"]))
    conn.commit()
    conn.close()
    state = EA_DIR / "state"
    (state / "briefing-latest.json").write_text(json.dumps({"today_top": TODAY_TOP}))
    log_file = EA_DIR / "logs" / "shell-run.log"
    for p in (EA_DIR / "logs").glob("*.jsonl"):
        p.unlink()
    (EA_DIR / "logs" / "missed-closures.log").unlink(missing_ok=True)

    def run(rewrite_reply: str) -> str:
        rewrite_file = EA_DIR / "stub-rewrite.txt"
        rewrite_file.write_text(rewrite_reply)
        draft_file = EA_DIR / "stub-draft.txt"
        draft_file.write_text(BRIEFING_TEXT)
        stub = f"""
invoke_claude() {{
    case "$5" in
        *-audit-rewrite) cat "{rewrite_file}" ;;
        *-audit) printf '%s' '{{"verdicts": [{{"id": "jrnl-4-reviews-due-20260826", "verdict": "done", "why": "every reminder row for 0311, 0420, 0533 and 0647 was closed by the user."}}]}}' ;;
    esac
}}
"""
        # The block defines the real invoke_claude (sudo + claude) and then
        # runs its case statement, so the stub has to land between the two:
        # after the definition, before `AUDIT_SUMMARY=""`.
        block = _shell_block()
        marker = 'AUDIT_SUMMARY=""'
        assert block.count(marker) == 1, "audit block anchor drifted"
        block = block.replace(marker, stub + "\n" + marker)
        script = (
            "set -uo pipefail\n"
            f'PYTHON3="{sys.executable}"\nSCRIPT_DIR="{REPO}"\nEA_DIR="{EA_DIR}"\n'
            f'STATE_DIR="{state}"\nLOG_FILE="{log_file}"\nOUTPUT_FILE="{state}/briefing-latest.json"\n'
            'AGENT="morning-briefing"\nVALIDATION="ok"\nMODEL="opus"\nALLOWED_TOOLS="Read"\n'
            'DISALLOWED_TOOLS=""\nSTRIP_PREAMBLE_MAX_LINES=12\nPROMPT="AGENT PROMPT SENTINEL"\n'
            f'CLAUDE_OUTPUT=$(cat "{draft_file}")\n'
            'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
            + block
            + '\nprintf "%s" "$CLAUDE_OUTPUT"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=300, env=dict(os.environ, EA_DIR=str(EA_DIR),
                                                 EA_AUDIT_NO_FETCH="1"))
        assert r.returncode == 0, (r.returncode, r.stderr[-800:], log_file.read_text()[-800:])
        return r.stdout

    out = run(":sunny: *Morning Briefing*\n:dart: rewritten queue without the JRNL item\n"
              ":white_check_mark: *Quietly handled*: JRNL reviews: all four reminder rows closed.")
    assert out.startswith(":sunny: *Morning Briefing*\n:dart: rewritten"), out[:200]
    assert _status(db, "jrnl-4-reviews-due-20260826")[0] == "open"
    assert "pre-send audit (morning-briefing)" in _proposal(db, "jrnl-4-reviews-due-20260826")["why"]
    logtxt = log_file.read_text()
    assert "Open-claim audit: 1 item(s) presented as open have contrary evidence" in logtxt
    assert "draft rewritten with the corrections" in logtxt
    runs = [json.loads(l) for l in (EA_DIR / "logs" / "open-claims-audit.jsonl").read_text().splitlines()]
    assert runs[-1]["closed"] == ["jrnl-4-reviews-due-20260826"] and runs[-1]["rewrite"] is True
    assert (state / "open-claims-audit-note.txt").read_text().startswith("PRE-SEND AUDIT RESULT")
    missed = [json.loads(l) for l in (EA_DIR / "logs" / "missed-closures.log").read_text().splitlines()]
    assert missed[-1]["ledger_id"] == "jrnl-4-reviews-due-20260826"
    assert missed[-1]["source"] == "pre-send-audit"

    # invalid rewrite (no anchor): the original draft survives, the row keeps its proposal
    db = _fresh_db()
    conn = sqlite3.connect(str(db))
    for r in CLOSED_ROWS:
        conn.execute("INSERT INTO ledger (id, type, status, description, last_action, "
                     "conversation_id, created_at, updated_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (r["id"], r["type"], "completed", r["description"], r["last_action"],
                      r["conversation_id"], r["created_at"], r["closed_at"], r["closed_at"]))
    conn.commit()
    conn.close()
    log_file.unlink()
    out = run("Here is what I would post: nothing with an anchor")
    assert out == BRIEFING_TEXT.rstrip("\n"), out[:200]
    assert _status(db, "jrnl-4-reviews-due-20260826")[0] == "open"
    assert _proposal(db, "jrnl-4-reviews-due-20260826")
    assert "rewrite failed validation; posting the original draft" in log_file.read_text()


def test_run_agent_audit_block_is_skipped_for_other_agents_and_bad_drafts():
    import subprocess
    for agent, validation in (("list-digest", "ok"), ("morning-briefing", "empty")):
        script = (
            "set -uo pipefail\n"
            f'PYTHON3="{sys.executable}"\nSCRIPT_DIR="{REPO}"\nEA_DIR="{EA_DIR}"\n'
            f'STATE_DIR="{EA_DIR}/state"\nLOG_FILE="{EA_DIR}/logs/skip.log"\nOUTPUT_FILE="/nonexistent"\n'
            f'AGENT="{agent}"\nVALIDATION="{validation}"\nMODEL="opus"\nALLOWED_TOOLS=""\n'
            'DISALLOWED_TOOLS=""\nSTRIP_PREAMBLE_MAX_LINES=12\nPROMPT="p"\nCLAUDE_OUTPUT=":sunny: x"\n'
            'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
            + _shell_block().replace('AUDIT_SUMMARY=""',
                                     'invoke_claude() { echo "MUST NOT BE CALLED" >&2; exit 9; }\nAUDIT_SUMMARY=""')
            + '\nprintf "%s|%s" "$CLAUDE_OUTPUT" "$AUDIT_SUMMARY"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                           env=dict(os.environ, EA_DIR=str(EA_DIR), EA_AUDIT_NO_FETCH="1"))
        assert r.returncode == 0 and r.stdout == ":sunny: x|", (agent, r.returncode, r.stdout, r.stderr[-300:])


# ── parked rows: open, but not actionable yet ─────────────────────────

SHOW = _entry("show-tickets-nov4", "task",
              "Buy 2 tickets (Kit and Casey) for the evening show, Leadership "
              "Speaker Series, Wed Nov 4 2026, Main Hall. Tickets go on "
              "sale online Tue Sep 8.",
              "2026-08-27T15:55:10Z", deadline="2026-09-08",
              follow_up_after="2026-09-08")
PARKED_LINE = "- Show tickets for you and Casey (Wed Nov 4, Main Hall). Buy by Sep 8.\n"
PARKED_TEXT = BRIEFING_TEXT.replace(":white_check_mark:", PARKED_LINE + ":white_check_mark:")


def test_parked_until_reads_both_dates_and_ignores_the_past():
    from datetime import date
    today = date(2026, 9, 2)
    assert ledger_mod.parked_until(SHOW, today) == "2026-09-08"
    assert ledger_mod.parked_until(dict(SHOW, follow_up_after="2026-09-02"), today) == ""
    assert ledger_mod.parked_until(dict(SHOW, follow_up_after="2026-08-30"), today) == ""
    assert ledger_mod.parked_until(dict(SHOW, follow_up_after="", snooze_until="2026-09-10T00:00:00Z"),
                                   today) == "2026-09-10"
    # the later of the two wins
    assert ledger_mod.parked_until(dict(SHOW, snooze_until="2026-09-20"), today) == "2026-09-20"
    for junk in (None, "", "soon", 20260908):
        assert ledger_mod.parked_until(dict(SHOW, follow_up_after=junk), today) == ""


def test_a_named_parked_row_is_a_parked_claim_whatever_its_status():
    for row in (SHOW, dict(SHOW, status="follow_up"), dict(SHOW, status="snoozed"),
                dict(SHOW, confirmed_open_at="2026-09-01T12:00:00Z")):
        claims = oca.extract_claims(PARKED_TEXT, OPEN + [row], TODAY_TOP, now=NOW)
        assert claims[0]["id"] == "show-tickets-nov4", claims[:2]
        assert claims[0]["parked_until"] == "2026-09-08"
        assert sum(1 for c in claims if c.get("parked_until")) == 1
    # not in the draft: not a claim at all
    assert all(c["id"] != "show-tickets-nov4"
               for c in oca.extract_claims(BRIEFING_TEXT, OPEN + [SHOW], TODAY_TOP, now=NOW))
    # its date has come: an ordinary claim again
    later = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)
    claims = oca.extract_claims(PARKED_TEXT, [SHOW], [], now=later)
    assert [c.get("parked_until") for c in claims] == [None]


def test_two_prose_words_do_not_drop_a_parked_row():
    """'I closed 7 items that turned out to be done' shares two words with
    a parked ledger-rule row; dropping a row edits the post, so that is
    not a mention."""
    row = _entry("ledger-sync-ea-rule-review", "task",
                 "Check whether the new ledger rule (an archive never closes an "
                 "EA-made row) leaves too many stale obligations; closed rows "
                 "that turned out wrong",
                 "2026-08-28T00:00:00Z", follow_up_after="2026-09-12")
    post = ":white_check_mark: *Quietly handled*: I closed 7 items that turned out to be done already.\n"
    assert oca.extract_claims(post, [row], [], now=NOW) == []


def test_prepare_lists_parked_rows_without_asking_or_fetching():
    rec = oca.prepare(agent="morning-briefing", post=PARKED_TEXT, briefing={},
                      open_entries=[SHOW], no_fetch=False, closed_rows=[], now=NOW)
    # only a parked row: no question, no mailbox fetch, no prompt
    assert rec["asked"] == 0 and rec["prompt"] == "" and rec["fetch"] == ""
    assert [p["id"] for p in rec["parked"]] == ["show-tickets-nov4"]
    assert rec["parked"][0]["parked_until"] == "2026-09-08"
    # with evidence-bearing rows beside it, the question leaves it out
    rec = oca.prepare(agent="morning-briefing", post=PARKED_TEXT,
                      briefing={"today_top": TODAY_TOP}, open_entries=OPEN + [SHOW],
                      inbox=[], sent=SENT, events=[], closed_rows=CLOSED_ROWS, now=NOW)
    assert rec["asked"] == 3 and "show-tickets" not in rec["prompt"]
    assert all(c["id"] != "show-tickets-nov4" for c in rec["claims"])


def test_apply_tells_the_rewrite_to_drop_parked_rows():
    rec = oca.prepare(agent="morning-briefing", post=PARKED_TEXT, briefing={},
                      open_entries=[SHOW], closed_rows=[], now=NOW)
    # a verdict about a parked row is never acted on
    summary = oca.apply(audit=rec, verdicts=[{"id": "show-tickets-nov4", "verdict": "done",
                                              "why": "x"}],
                        close=True, log_missed=False, log_path=EA_DIR / "logs" / "p.jsonl")
    assert summary["closed"] == [] and summary["parked"] == ["show-tickets-nov4"]
    assert summary["rewrite"] is True
    note = summary["note"]
    assert note.startswith("PRE-SEND AUDIT RESULT") and "parked" in note
    assert "show-tickets-nov4" in note and "not before Tue Sep 8" in note
    assert "Do not mention them" in note and "still open?" not in note
    assert "—" not in note


def test_run_agent_audit_block_drops_a_parked_row_without_a_verdict_call():
    import subprocess
    db = _fresh_db()
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO ledger (id, type, status, description, date_identified, deadline, "
                 "follow_up_after, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (SHOW["id"], SHOW["type"], "open", SHOW["description"], SHOW["date_identified"],
                  "2099-01-10", "2099-01-10", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"))
    conn.commit()
    conn.close()
    state = EA_DIR / "state"
    (state / "briefing-latest.json").write_text(json.dumps({"today_top": []}))
    log_file = EA_DIR / "logs" / "shell-parked.log"
    log_file.unlink(missing_ok=True)
    draft_file = EA_DIR / "stub-parked-draft.txt"
    draft_file.write_text(PARKED_TEXT)
    stub = """
invoke_claude() {
    case "$5" in
        *-audit-rewrite) printf '%s' ':sunny: *Morning Briefing*
:dart: the queue without the tickets' ;;
        *-audit) echo "VERDICT CALL MUST NOT RUN" >&2; exit 9 ;;
    esac
}
"""
    block = _shell_block().replace('AUDIT_SUMMARY=""', stub + '\nAUDIT_SUMMARY=""')
    script = (
        "set -uo pipefail\n"
        f'PYTHON3="{sys.executable}"\nSCRIPT_DIR="{REPO}"\nEA_DIR="{EA_DIR}"\n'
        f'STATE_DIR="{state}"\nLOG_FILE="{log_file}"\nOUTPUT_FILE="{state}/briefing-latest.json"\n'
        'AGENT="morning-briefing"\nVALIDATION="ok"\nMODEL="opus"\nALLOWED_TOOLS="Read"\n'
        'DISALLOWED_TOOLS=""\nSTRIP_PREAMBLE_MAX_LINES=12\nPROMPT="AGENT PROMPT SENTINEL"\n'
        f'CLAUDE_OUTPUT=$(cat "{draft_file}")\n'
        'log() { echo "[t] $*" >> "$LOG_FILE"; }\n'
        + block + '\nprintf "%s" "$CLAUDE_OUTPUT"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
                       env=dict(os.environ, EA_DIR=str(EA_DIR), EA_AUDIT_NO_FETCH="1"))
    assert r.returncode == 0, (r.returncode, r.stderr[-800:], log_file.read_text()[-800:])
    assert r.stdout.startswith(":sunny: *Morning Briefing*\n:dart: the queue without"), r.stdout[:200]
    logtxt = log_file.read_text()
    assert "1 item(s) in the draft are parked until a later date" in logtxt
    assert "contrary evidence; asking" not in logtxt
    assert "draft rewritten with the corrections" in logtxt
    assert "not before" in (state / "open-claims-audit-note.txt").read_text()
    assert _status(db, "show-tickets-nov4")[0] == "open"



# ── rows the post shows, and the answer a waiting row waits for ───────

WAIT = _entry("waiting-rook-funding", "waiting",
              "Waiting on Rook Palmer: likelihood the mooring funding comes through",
              "2026-09-01T16:39:00Z", from_email="rook.palmer17@example.edu",
              deadline="2026-09-03")


def _inbound(subject, frm, ts, conv=""):
    return {"id": f"in-{ts}", "subject": subject, "receivedDateTime": ts,
            "conversationId": conv,
            "from": {"emailAddress": {"name": "", "address": frm}},
            "toRecipients": [{"emailAddress": {"address": "wren.fairbanks84@example.edu"}}]}


def test_a_waiting_row_sees_the_archived_answer_and_the_closed_row_that_recorded_it():
    """The answer a waiting row waits on arrives, the user reads it and
    archives it, and its reply row closes on the archive. Neither the
    inbox nor the row's words show that; the archive and the closed
    row's notes do."""
    archived = [_inbound("Re: Spring student for the mooring", "rook.palmer17@example.edu",
                         "2026-09-01T17:46:00Z", conv="conv-mooring")]
    closed = [{"id": "response-mooring", "type": "response_needed",
               "description": "Needs response: Re: Spring student for the mooring",
               "notes": "Rook rated the funding better than 90% and agreed with the plan.",
               "last_action": "Auto-closed: email archived from inbox by user.",
               "conversation_id": "conv-mooring", "created_at": "2026-09-01T17:50:00Z",
               "closed_at": "2026-09-01T18:00:00Z", "from_email": "rook.palmer17@example.edu"}]
    ev = oca.evidence_for(WAIT, inbox=[], sent=[], events=[], archive=archived,
                          closed_rows=closed, df={})
    assert any(e.startswith("archived 2026-09-01T17:46:00Z: from the person this row waits on")
               for e in ev), ev
    assert any("closed row" in e and "same correspondent" in e and "better than 90%" in e
               for e in ev), ev
    # another person's mail is not the answer
    other = [_inbound("Re: Spring student", "sky.ito@example.edu", "2026-09-01T17:46:00Z")]
    assert oca.evidence_for(WAIT, inbox=other, sent=[], events=[], df={}) == []


def test_mail_in_the_rows_own_thread_is_evidence_whatever_its_words():
    row = dict(DOE, date_identified="2026-08-20T16:30:02Z")
    sent = [dict(_sent("RE: Final reminder", ["office@example.edu"], "2026-08-21T10:00:00Z"),
                 conversationId="conv-doe")]
    ev = oca.evidence_for(row, inbox=[], sent=sent, events=[], df={})
    assert ev and ev[0].startswith("sent 2026-08-21T10:00:00Z: in this row's thread"), ev


def test_every_row_the_post_shows_is_asked_even_without_evidence():
    """The renderer names its rows; words are not guessed for them, the
    cap for guessed rows does not cut them, and a row with no mail
    evidence is still asked: its own notes can say it is done."""
    rec = oca.prepare(agent="morning-briefing", post="nothing that names a row",
                      briefing={"today_top": []}, open_entries=OPEN + [WAIT],
                      inbox=[], sent=[], events=[], archive=[], closed_rows=[],
                      rendered=["waiting-rook-funding", "no-such-row", "spring-sym-20-participants"],
                      now=NOW)
    assert [c["id"] for c in rec["claims"]] == ["waiting-rook-funding"]   # confirmed-open skipped
    assert rec["claims"][0]["why_mentioned"] == "rendered"
    assert rec["asked"] == 1
    assert "(no mail, calendar or closed-row evidence)" in rec["prompt"]
    assert "A 'waiting' row waits on someone else" in rec["prompt"]
    assert "correspondent: rook.palmer17@example.edu" in rec["prompt"]


def test_kept_mail_filter_fails_closed():
    import mail_privacy
    saved = mail_privacy.load_index
    mail_privacy.load_index = lambda: (_ for _ in ()).throw(OSError("no index"))
    try:
        assert oca.without_kept([_inbound("x", "a@example.edu", "2026-09-01T00:00:00Z")]) == []
    finally:
        mail_privacy.load_index = saved


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
