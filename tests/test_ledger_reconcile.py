"""The nightly ledger reconcile (preprocessor/ledger_reconcile.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-reconcile-"))
os.environ["EA_DIR"] = str(EA_DIR)
for sub in ("config", "state", "logs"):
    (EA_DIR / sub).mkdir(parents=True, exist_ok=True)
(EA_DIR / "config" / "user-profile.json").write_text(json.dumps(
    {"name": "Kit Example", "email": "kit.example@example.edu", "role": "Professor"}))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import closure  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import ledger_reconcile as lr  # noqa: E402
import mail_privacy as mp  # noqa: E402
import owner_trust  # noqa: E402
import project_store  # noqa: E402
import remote_llm as rl  # noqa: E402

NOW = datetime(2026, 10, 24, 7, 30, tzinfo=timezone.utc)
USER = {"kit.example@example.edu"}


def _rec(key, conv, received, frm, subject, summary, *, ask="", state="fyi", people=(),
         own=False, kept=""):
    r = {"ts": received, "key": key, "id": f"id-{key}", "conversation_id": conv,
         "received": received, "from": frm, "subject": subject, "status": "ok", "own": own,
         "result": {"summary": summary, "ask": ask, "state": state, "needs_reply": state == "needs-reply",
                    "deadline": "", "offered_times": [],
                    "people": [{"name": n, "role": r_} for n, r_ in people]}}
    if kept:
        r["keep_from_anthropic"] = kept
    return r


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_INDEX_CACHE", {"mtime": None, "index": None}, raising=False)
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "reconcile": "nightly"}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger_mod, "DB_PATH", path)
    monkeypatch.setattr(project_store, "DB_PATH", path)
    monkeypatch.setattr(project_store._ledger, "DB_PATH", path)
    ledger_mod._get_db().close()
    conn = project_store._get_db(path)
    conn.execute("INSERT INTO projects (id,name,domain,status,aliases,created_at,updated_at) "
                 "VALUES ('harbor-survey','Harbor survey','research','active','harbor',?,?)",
                 ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO people (email,name,created_at,updated_at) VALUES (?,?,?,?)",
                 ("dana.quill@example.org", "Dana Quill", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO project_people (project_id,email) VALUES ('harbor-survey','dana.quill@example.org')")
    conn.commit()
    return conn


def _rows(db):
    ledger_mod.add_entry("harbor-sonar-quote", "task",
                         "Harbor survey: get the sonar rental quote from Dana Quill",
                         from_email="dana.quill@example.org", from_name="Dana Quill")
    db.execute("INSERT OR IGNORE INTO project_ledger VALUES ('harbor-survey','harbor-sonar-quote')")
    db.commit()
    ledger_mod.add_entry("response-visit", "response_needed",
                         "Needs response: Re: Harbor site visit dates",
                         from_email="dana.quill@example.org", from_name="Dana Quill",
                         source_message_id="m-visit", conversation_id="CONV-VISIT")
    ledger_mod.add_entry("reply-lee-seminar", "response_needed",
                         "Reply to Lee Marlow about the seminar slot",
                         from_email="lee.marlow@example.com", from_name="Lee Marlow")
    ledger_mod.add_entry("badge-form", "task", "Badge form for the base visit",
                         source_message_id="m-badge", conversation_id="CONV-KEPT")
    db.commit()
    # harbor-survey rows: the quote (no thread) and the visit (thread); the
    # seminar row names no project; the badge row's thread is kept.
    ledger_mod.set_metadata("response-visit", thread_brief=mp.thread_brief("CONV-VISIT", user=USER, now=NOW))
    return ledger_mod.get_open_entries()


def _records():
    for r in (
        _rec("k1", "CONV-VISIT", "2026-10-20T10:00:00Z", "dana.quill@example.org",
             "Harbor site visit dates", "Dana asks which week suits the site visit.",
             ask="Pick a week for the site visit", state="needs-reply", people=(("Dana Quill", "sponsor"),)),
        _rec("k2", "CONV-VISIT", "2026-10-22T09:00:00Z", "kit.example@example.edu",
             "Re: Harbor site visit dates", "The user picks the week of Nov 9.", own=True),
        _rec("k3", "CONV-SONAR", "2026-10-21T12:00:00Z", "dana.quill@example.org",
             "Sonar rental quote for the harbor survey",
             "Dana sends the sonar rental quote: 4,200 for two weeks.", people=(("Dana Quill", "sponsor"),)),
        _rec("k4", "CONV-NEWS", "2026-10-21T12:00:00Z", "news@example.net",
             "Harbor news weekly", "A newsletter about harbors."),
        _rec("k5", "CONV-SEM", "2026-10-19T12:00:00Z", "lee.marlow@example.com",
             "Seminar slot in November", "Lee offers a seminar slot on Nov 12 or 19.",
             ask="Pick Nov 12 or 19", state="needs-reply", people=(("Lee Marlow", "host"),)),
        _rec("k6", "CONV-KEPT", "2026-10-21T12:00:00Z", "badges@base.mil",
             "Badge form", "A badge form.", kept="mil"),
    ):
        rl.append(mp.EXTRACTIONS, r)


def _judge(by_title: dict):
    """A judge that answers by row title: {title fragment: verdict fields};
    `thread: True` picks the row's first candidate thread. Refs come from
    the prompt, so the test needs no knowledge of the row order."""
    def judge(prompt):
        judge.prompts.append(prompt)
        rows = json.loads(prompt[prompt.index('{"rows"'):])["rows"]
        out = []
        for it in rows:
            for frag, fields in by_title.items():
                if frag.lower() in it["title"].lower():
                    v = {"ref": it["ref"], "verdict": "open", "note": "", "why": "", **fields}
                    if v.get("thread") is True:
                        cands = it.get("candidate_threads") or []
                        v["thread"] = cands[0]["ref"] if cands else ""
                    if v.get("of"):
                        v["of"] = next((o["ref"] for o in rows if v["of"].lower() in o["title"].lower()), "")
                    out.append(v)
        return json.dumps(out), 0.05
    judge.prompts = []
    return judge


def _run(db, judge, **kw):
    return lr.run(now=NOW, rows=_rows(db), judge=judge, sync=lambda: {"written": 1},
                  ping=lambda: True, store_conn=db, user=USER, **kw)


# ── matching and grouping ─────────────────────────────────────────────

def test_candidate_threads_match_an_idless_row_by_its_words_and_person(priv, db):
    _records()
    rows = _rows(db)
    df = lr.doc_freq(rows)
    table = lr.thread_table(mp.load_index(), now=NOW, user=USER)
    row = next(r for r in rows if r["id"] == "harbor-sonar-quote")
    cands = lr.candidate_threads(row, table, df, people={"dana.quill@example.org"})
    assert [c["conversation_id"] for c in cands][:1] == ["CONV-SONAR"]
    assert "sonar" in cands[0]["hits"]
    # the newsletter shares "harbor" alone, and a sender nobody named
    assert "CONV-NEWS" not in {c["conversation_id"] for c in cands}


def test_groups_place_an_unlinked_row_in_the_one_project_its_words_name(priv, db):
    rows = _rows(db)
    ledger_mod.add_entry("harbor-permit", "task", "Harbor survey: file the harbor permit")
    rows = ledger_mod.get_open_entries()
    grp, linked = lr.groups(rows, db)
    # the permit row names the project's alias; the visit row names its
    # alias and its person; the seminar and badge rows name nothing
    assert sorted(l["id"] for l in linked) == ["harbor-permit", "response-visit"]
    assert {l["project"] for l in linked} == {"harbor-survey"}
    by = {g["label"]: [r["id"] for r in g["rows"]] for g in grp}
    assert set(by["harbor-survey"]) == {"harbor-sonar-quote", "harbor-permit", "response-visit"}
    assert set(by["loose#1"]) == {"reply-lee-seminar", "badge-form"}
    assert db.execute("SELECT project_id FROM project_ledger WHERE ledger_id='harbor-permit'"
                      ).fetchone()[0] == "harbor-survey"


def test_a_kept_thread_reaches_the_judge_as_counts_only(priv, db):
    _records()
    rows = _rows(db)
    g = {"label": "loose#1", "project": "", "name": "", "people": set(),
         "rows": [r for r in rows if r["id"] == "badge-form"]}
    items, _ = lr.items_for(g, index=mp.load_index(), df={}, user=USER, now=NOW)
    assert items[0]["thread"] == {"kept": True, "message_count": 1, "latest": "2026-10-21T12:00:00Z",
                                  "last_from_user": "", "user_replied_after_latest": False}
    assert "badge" not in json.dumps(items[0]["thread"]).lower()


# ── the verdicts ──────────────────────────────────────────────────────

def test_parse_drops_invented_refs_threads_and_unexplained_verdicts():
    items = [{"ref": "R1", "candidate_threads": [{"ref": "T1"}]}, {"ref": "R2"}]
    threads = {"T1": {"conversation_id": "C1"}}
    text = 'Here you go:\n[{"ref":"R1","verdict":"done","thread":"T1","note":"x","why":""},' \
           '{"ref":"R2","verdict":"duplicate","of":"R2","note":"n"},' \
           '{"ref":"R9","verdict":"done","why":"made up"},' \
           '{"ref":"R1","verdict":"open"}]'
    out = lr.parse_verdicts(text, items, threads)
    assert [v["ref"] for v in out] == ["R1", "R2"]
    assert out[0]["verdict"] == "open" and out[0]["thread"] == "T1"      # done without a why
    assert out[1]["verdict"] == "open" and out[1]["of"] == ""            # duplicate of itself
    assert lr.parse_verdicts("no json here", items, threads) == []


def test_done_is_a_proposal_a_note_lands_in_last_action_and_a_thread_on_a_task_row(priv, db):
    _records()
    judge = _judge({"sonar rental": {"thread": True, "note": "Dana sent the quote on Oct 21."}})
    rec = _run(db, judge, only="harbor-survey")
    assert rec["groups"] == 1 and rec["failed"] == []
    assert rec["threaded"] == [{"id": "harbor-sonar-quote", "conversation_id": "CONV-SONAR", "on_row": True}]
    row = ledger_mod.get_entry("harbor-sonar-quote")
    assert row["conversation_id"] == "CONV-SONAR"
    assert row["last_action"].endswith("2026-10-24: Dana sent the quote on Oct 21. (reconcile)")
    assert row["reconcile"]["thread"] == "CONV-SONAR" and row["status"] == "open"
    filed = db.execute("SELECT project_id, conversation_id FROM project_messages").fetchall()
    assert [tuple(f) for f in filed] == [("harbor-survey", "CONV-SONAR")]
    # the prompt carried gists and asks, never a message body
    assert "sonar rental quote" in judge.prompts[0].lower()
    assert "4,200" in judge.prompts[0]        # the record's summary is what Claude sees
    runs = rl.read(lr.RUNS)
    assert runs[-1]["noted"] == [{"id": "harbor-sonar-quote", "note": "Dana sent the quote on Oct 21."}]


def test_done_and_duplicate_propose_and_never_close(priv, db):
    _records()
    judge = _judge({"site visit": {"verdict": "done", "why": "The user picked the week on Oct 22."},
                    "sonar rental": {"verdict": "duplicate", "of": "site visit", "why": "Same visit."}})
    rec = _run(db, judge)
    assert sorted(p["id"] for p in rec["proposed"]) == ["harbor-sonar-quote", "response-visit"]
    row = ledger_mod.get_entry("response-visit")
    assert row["status"] == "open"
    assert row["close_proposal"]["signal"] == lr.SIGNAL
    assert row["close_proposal"]["why"] == "The user picked the week on Oct 22."
    dup = ledger_mod.get_entry("harbor-sonar-quote")
    assert dup["status"] == "open" and dup["close_proposal"]["signal"] == lr.DUP_SIGNAL
    assert dup["close_proposal"]["why"] == "duplicate of response-visit: Same visit."
    conn = ledger_mod.guarded_connect(ledger_mod.DB_PATH)
    pending = {p["id"] for p in closure.pending_proposals(conn)}
    assert "response-visit" in pending
    conn.close()


def test_a_reply_row_keeps_its_thread_in_metadata_only(priv, db):
    _records()
    judge = _judge({"seminar slot": {"thread": True}})
    rec = _run(db, judge)
    assert rec["threaded"] == [{"id": "reply-lee-seminar", "conversation_id": "CONV-SEM", "on_row": False}]
    row = ledger_mod.get_entry("reply-lee-seminar")
    assert not row.get("conversation_id")
    assert row["reconcile"]["thread"] == "CONV-SEM"


def test_a_vetoed_row_is_skipped_not_proposed(priv, db):
    _records()
    conn = ledger_mod.guarded_connect(ledger_mod.DB_PATH)
    _rows(db)
    closure.ensure_schema(conn)
    closure.propose(conn, "response-visit", signal=lr.SIGNAL, actor="t", note="x")
    closure.reject(conn, "response-visit")
    conn.commit()
    conn.close()
    judge = _judge({"site visit": {"verdict": "done", "why": "He replied."}})
    rec = _run(db, judge)
    assert rec["proposed"] == []
    assert [s["id"] for s in rec["skipped"]] == ["response-visit"]


def test_dry_run_writes_nothing(priv, db):
    _records()
    judge = _judge({"sonar rental": {"thread": True, "note": "A note."}})
    rec = _run(db, judge, only="harbor-survey", dry_run=True)
    assert rec["noted"] and rec["threaded"]
    row = ledger_mod.get_entry("harbor-sonar-quote")
    assert not row.get("conversation_id") and "reconcile" not in row["last_action"]
    assert db.execute("SELECT count(*) FROM project_messages").fetchone()[0] == 0


# ── the rails ─────────────────────────────────────────────────────────

def test_the_night_is_skipped_when_the_remote_model_is_down(priv, db):
    _records()
    calls = []
    rec = lr.run(now=NOW, rows=_rows(db), judge=lambda p: calls.append(p),
                 sync=lambda: calls.append("sync"), ping=lambda: False, store_conn=db, user=USER)
    assert rec["skipped"] == "the remote model did not answer" and calls == []
    assert lr.summary_line(rec).startswith(":broom: Reconcile skipped")


def test_the_lane_is_off_without_its_switch_or_without_the_gate(priv):
    assert lr.active()
    assert lr.active({"reconcile": "nightly", "mail": "off"}) is False
    assert lr.active({"reconcile": "off", "mail": "enforce"}) is False
    owner_trust.write_owner_file(priv / rl.CONFIG_FILE, json.dumps(
        {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce"}).encode(),
        mode=0o600)
    assert lr.main(["run"]) == lr.EXIT_OFF == 3


def test_summary_line_counts(priv):
    rec = {"rows": 12, "groups": 3, "proposed": [{"id": "a"}], "noted": [1, 2], "threaded": [1],
           "linked": [1, 1], "failed": [], "cost_usd": 0.42}
    assert lr.summary_line(rec) == (":broom: Reconcile: 12 rows in 3 groups; 1 looks done (on the "
                                    "morning card); 2 notes; 1 rows given their thread; 2 rows "
                                    "placed in projects; $0.42.")
    assert "—" not in lr.SYSTEM
