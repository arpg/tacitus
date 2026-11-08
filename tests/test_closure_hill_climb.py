"""The closure hill climb (preprocessor/closure_policy.py) and the renewal
of a closed row (closure.note_renew).

Pinned here, bottom up:
  1. decisions(): the owner's verdicts read from ledger_events, per signal
     and population; a renewal is no false close; the owner's own acts and
     a resident's model are never in the loop.
  2. judge() and step(): promotion at the bar and not below it, demotion
     on a reopen, the way back, and a frozen policy.
  3. settle() with a policy: a promoted signal closes at once (never for a
     resident, a kept-open row or a waiting row), a demoted one proposes;
     no file, the switch off or no chat buttons is today's behaviour.
  4. note_renew(): the close stood, older evidence is refused, newer
     evidence proposes instead of being refused for good; the repair of
     rows renewed before it; report() and the scrubber's standing veto.
  5. update(), the CLI and the backtest's replay.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(1, str(ROOT))

import closure  # noqa: E402
import closure_policy as cp  # noqa: E402
import ledger as ledger_mod  # noqa: E402

POLICY = ROOT / "preprocessor" / "closure_policy.py"
LEDGER = ROOT / "preprocessor" / "ledger.py"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def ea(tmp_path, monkeypatch):
    """A scratch EA_DIR with a ledger, and no cached policy."""
    d = tmp_path / "ea"
    (d / "state").mkdir(parents=True)
    (d / "config").mkdir()
    monkeypatch.setenv("EA_DIR", str(d))
    cp.forget()
    yield d
    cp.forget()


def _db(ea: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(ea / "state" / "obligations-ledger.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    closure.ensure_schema(conn)
    return conn


def _add(conn, rid, *, type_="task", conv="", status="open", meta=None, source=None):
    born = "2026-08-01T12:00:00Z"
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, conversation_id, date_identified, "
        "last_action, metadata, created_at, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, type_, status, f"desc {rid}", conv, born, "", json.dumps(meta or {}), born, born,
         source))


def _row(conn, rid) -> dict:
    return ledger_mod._row_to_dict(conn.execute("SELECT * FROM ledger WHERE id = ?",
                                                (rid,)).fetchone())


def _ev(conn, eid, kind, ts, *, signal="", tier="", actor="t", evidence=None):
    closure.record_event(conn, eid, kind, tier=tier, signal=signal, actor=actor,
                         evidence=evidence, ts=_iso(ts))


def _policy(ea: Path, signals: dict, **extra) -> None:
    data = {"version": 1, "signals": {s: {"state": st, "since": "2026-09-01T00:00:00Z"}
                                      for s, st in signals.items()}, **extra}
    (ea / "state" / cp.POLICY_FILE).write_text(json.dumps(data))
    cp.forget()


def _confirms(conn, sig, n, *, start=T0, reject_at=()):
    """n proposals of `sig` on fresh rows, each settled a minute later:
    confirmed, or kept open for the indices in reject_at."""
    for i in range(n):
        eid = f"{sig.replace(':', '-')}-{i}"
        at = start + timedelta(hours=i)
        _ev(conn, eid, "propose", at, signal=sig, tier=closure.PROPOSAL)
        _ev(conn, eid, "reject" if i in reject_at else "confirm", at + timedelta(minutes=1),
            signal=sig, tier=closure.PROPOSAL,
            evidence={"signals": [sig]} if i in reject_at else None)


# ── 1. the decisions ──────────────────────────────────────────────────

def test_a_confirm_credits_every_signal_on_the_proposal_and_a_reject_names_them(ea):
    conn = _db(ea)
    _ev(conn, "t1", "propose", T0, signal="calendar_event_match", tier="proposal")
    _ev(conn, "t1", "propose", T0 + timedelta(minutes=5), signal="llm:reconcile",
        tier="proposal")
    _ev(conn, "t1", "confirm", T0 + timedelta(hours=1), signal="llm:reconcile",
        tier="decisive")
    _ev(conn, "t2", "propose", T0, signal="user_replied_outlook", tier="proposal")
    _ev(conn, "t2", "reject", T0 + timedelta(hours=1), signal="user_replied_outlook",
        evidence={"signals": ["user_replied_outlook"]})
    ds = cp.decisions(conn, now=T0 + timedelta(days=30))
    got = sorted((d.signal, d.track, d.right, d.acted) for d in ds)
    assert got == [("calendar_event_match", "proposal", True, _iso(T0)),
                   ("llm:reconcile", "proposal", True, _iso(T0 + timedelta(minutes=5))),
                   ("user_replied_outlook", "proposal", False, _iso(T0))]


def test_the_owners_word_while_a_proposal_stood_confirms_it(ea):
    conn = _db(ea)
    _ev(conn, "t1", "propose", T0, signal="llm:open-claims-audit", tier="proposal")
    _ev(conn, "t1", "close", T0 + timedelta(hours=2), signal="user_stated", tier="decisive")
    (d,) = cp.decisions(conn, now=T0 + timedelta(days=30))
    assert (d.signal, d.track, d.right) == ("llm:open-claims-audit", "proposal", True)


def test_a_close_stands_after_two_days_and_a_reopen_makes_it_wrong(ea):
    conn = _db(ea)
    _ev(conn, "r1", "close", T0, signal="user_archived", tier="decisive")
    _ev(conn, "r2", "close", T0, signal="user_archived", tier="decisive")
    _ev(conn, "r2", "reopen", T0 + timedelta(hours=3), signal="user_archived", actor="cli:chat")
    _ev(conn, "r3", "close", T0 + timedelta(days=4), signal="user_archived", tier="decisive")
    ds = cp.decisions(conn, now=T0 + timedelta(days=5))
    assert [(d.entry, d.right) for d in ds] == [("r1", True), ("r2", False)]
    # r3 is a day old: no verdict yet
    assert ds[0].known == _iso(T0 + timedelta(days=2))


def test_a_new_message_that_reopens_a_close_is_a_renewal_not_a_false_close(ea):
    conn = _db(ea)
    for eid, kind, actor in (("r1", "renew", "intake"), ("r2", "reopen", "intake")):
        _ev(conn, eid, "close", T0, signal="user_replied_outlook", tier="decisive")
        _ev(conn, eid, kind, T0 + timedelta(hours=5), signal="user_replied_outlook", actor=actor)
    ds = cp.decisions(conn, now=T0 + timedelta(hours=6))
    assert [(d.entry, d.right) for d in ds] == [("r1", True), ("r2", True)]


def test_the_owners_acts_plugins_and_a_residents_model_stay_out_of_the_loop(ea):
    conn = _db(ea)
    _add(conn, "p1", source="plugin:example")
    for sig in ("user_stated", "linked_send", "duplicate_of", "send_reply_row", "llm:chat",
                "llm:morning-briefing"):
        _ev(conn, f"x-{sig}", "close", T0, signal=sig, tier="decisive")
    _ev(conn, "p1", "close", T0, signal="user_archived", tier="decisive")
    assert cp.decisions(conn, now=T0 + timedelta(days=9)) == []
    assert not cp.in_loop("user_confirmed") and not cp.in_loop("bad signal")
    assert cp.in_loop("llm:reconcile") and cp.in_loop("calendar_event_match")


# ── 2. the moves ──────────────────────────────────────────────────────

def test_a_signal_the_owner_keeps_confirming_is_promoted_at_the_bar(ea):
    conn = _db(ea)
    _confirms(conn, "llm:reconcile-lapsed", 14)
    now = T0 + timedelta(days=5)
    ds = cp.decisions(conn, now=now)
    assert cp.judge(ds, None, _iso(now))["state"] == ""          # 14 is below the floor
    _confirms(conn, "llm:reconcile-lapsed", 15)
    ds = cp.decisions(conn, now=now)
    rec = cp.judge([d for d in ds if d.entry.endswith(tuple(f"-{i}" for i in range(15)))],
                   None, _iso(now))
    assert rec["state"] == cp.PROMOTED and rec["since"] == _iso(now)
    assert "none kept open in the last 10" in rec["why"]


def test_one_still_open_in_the_last_ten_holds_the_promotion(ea):
    conn = _db(ea)
    _confirms(conn, "llm:reconcile", 30, reject_at=(25,))        # 29 of 30 right
    ds = cp.decisions(conn, now=T0 + timedelta(days=5))
    assert cp.judge(ds, None, "2026-09-10T00:00:00Z")["state"] == ""
    # the same record with the "Still open" ten decisions back promotes
    conn.execute("DELETE FROM ledger_events")
    _confirms(conn, "llm:reconcile", 30, reject_at=(19,))
    ds = cp.decisions(conn, now=T0 + timedelta(days=5))
    assert cp.judge(ds, None, "2026-09-10T00:00:00Z")["state"] == cp.PROMOTED


def test_a_reopened_decisive_close_demotes_and_proposals_earn_it_back(ea):
    conn = _db(ea)
    for i in range(6):
        _ev(conn, f"r{i}", "close", T0 + timedelta(hours=i), signal="user_archived",
            tier="decisive")
    _ev(conn, "r5", "reopen", T0 + timedelta(hours=8), signal="user_archived", actor="cli:chat")
    now = T0 + timedelta(days=3)
    rec = cp.judge(cp.decisions(conn, now=now), None, _iso(now))
    assert rec["state"] == cp.DEMOTED
    # while demoted, its proposals are confirmed: 16 clean ones bring it back
    for i in range(16):
        at = now + timedelta(hours=i)
        _ev(conn, f"d{i}", "propose", at, signal="user_archived", tier=closure.DEMOTED)
        _ev(conn, f"d{i}", "confirm", at + timedelta(minutes=1), signal="user_archived")
    later = now + timedelta(days=2)
    back = cp.judge(cp.decisions(conn, now=later), rec, _iso(later))
    assert back["state"] == "" and "closes at once again" in back["why"]


def test_a_promoted_signal_whose_close_is_reopened_proposes_again(ea):
    conn = _db(ea)
    since = T0
    for i in range(3):
        _ev(conn, f"p{i}", "close", T0 + timedelta(hours=i + 1), signal="llm:reconcile",
            tier=closure.PROMOTED)
    _ev(conn, "p2", "reopen", T0 + timedelta(hours=9), signal="llm:reconcile", actor="cli:cli")
    now = T0 + timedelta(days=4)
    rec = cp.judge(cp.decisions(conn, now=now), {"state": cp.PROMOTED, "since": _iso(since)},
                   _iso(now))
    assert rec["state"] == "" and "proposes again" in rec["why"]


def test_a_frozen_policy_keeps_its_states(ea):
    conn = _db(ea)
    _confirms(conn, "llm:reconcile-lapsed", 16)
    ds = cp.decisions(conn, now=T0 + timedelta(days=5))
    signals, changes = cp.step({"frozen": True, "signals": {}}, ds, "2026-09-10T00:00:00Z")
    assert changes == [] and signals["llm:reconcile-lapsed"]["state"] == ""
    signals, changes = cp.step({"signals": {}}, ds, "2026-09-10T00:00:00Z")
    assert [c["to"] for c in changes] == [cp.PROMOTED]


# ── 3. settle with a policy ───────────────────────────────────────────

def test_a_promoted_signal_closes_at_once_and_keeps_the_line_for_the_note(ea):
    conn = _db(ea)
    _add(conn, "t1", conv="c1")
    _policy(ea, {"calendar_event_match": cp.PROMOTED})
    res = closure.settle(conn, "t1", signal="calendar_event_match", actor="closure_scrubber",
                         note="Auto-closed", why="the meeting is on your calendar")
    assert res["action"] == "closed" and res["tier"] == closure.PROMOTED
    row = _row(conn, "t1")
    assert row["closed_by"]["tier"] == closure.PROMOTED
    assert row["closed_by"]["why"] == "the meeting is on your calendar"
    (ev,) = [e for e in conn.execute("SELECT * FROM ledger_events WHERE kind = 'close'")]
    assert ev["tier"] == closure.PROMOTED
    assert [r["id"] for r in closure.promoted_closes(conn)] == ["t1"]
    assert closure.mark_noted(conn, "t1") and closure.promoted_closes(conn) == []


def test_promotion_never_reaches_a_resident_a_kept_open_row_or_a_wait(ea):
    conn = _db(ea)
    _add(conn, "t1")
    _add(conn, "t2", meta={"close_vetoes": {closure.KEPT_OPEN: "2026-08-02T00:00:00Z"}})
    _add(conn, "w1", type_="waiting")
    _policy(ea, {"llm:reconcile": cp.PROMOTED})
    assert closure.settle(conn, "t1", signal="llm:reconcile", actor="cli:chat",
                          note="n")["action"] == "proposed"
    assert closure.settle(conn, "t2", signal="llm:reconcile", actor="reconcile",
                          note="n")["action"] == "proposed"
    assert closure.settle(conn, "w1", signal="llm:reconcile", actor="reconcile",
                          note="n")["action"] == "proposed"
    out = closure.propose_or_promote(conn, "t1", signal="llm:reconcile", actor="reconcile",
                                     note="looks done")
    assert out["action"] == "closed" and _row(conn, "t1")["closed_by"]["tier"] == "promoted"


def test_a_demoted_signal_proposes_where_it_would_close(ea):
    conn = _db(ea)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _policy(ea, {"user_replied_outlook": cp.DEMOTED})
    res = closure.settle(conn, "response-a", signal="user_replied_outlook", actor="scrubber",
                         note="replied")
    assert res["action"] == "proposed" and _row(conn, "response-a")["status"] == "open"
    (ev,) = conn.execute("SELECT * FROM ledger_events WHERE kind = 'propose'").fetchall()
    assert ev["tier"] == closure.DEMOTED


def test_no_file_the_switch_off_or_no_buttons_is_todays_behaviour(ea, monkeypatch):
    conn = _db(ea)
    for rid in ("t1", "t2", "t3"):
        _add(conn, rid)
    assert closure.settle(conn, "t1", signal="calendar_event_match", actor="s",
                          note="n")["action"] == "proposed"
    (ea / "config" / "ea-config.json").write_text(json.dumps({"closure": {"hill_climb": False}}))
    import ea_config
    monkeypatch.setattr(ea_config, "CONFIG_FILE", ea / "config" / "ea-config.json")
    _policy(ea, {"calendar_event_match": cp.PROMOTED})
    assert closure.settle(conn, "t2", signal="calendar_event_match", actor="s",
                          note="n")["action"] == "proposed"
    (ea / "config" / "ea-config.json").write_text(json.dumps({"profile": "email"}))
    cp.forget()
    assert cp.state("calendar_event_match") == ""
    (ea / "config" / "ea-config.json").write_text(json.dumps({}))
    cp.forget()
    assert cp.state("calendar_event_match") == cp.PROMOTED
    assert closure.settle(conn, "t3", signal="calendar_event_match", actor="s",
                          note="n")["action"] == "closed"


def test_a_file_cannot_promote_the_owners_acts_or_a_residents_model(ea):
    _policy(ea, {"user_stated": cp.DEMOTED, "llm:chat": cp.PROMOTED, "linked_send": cp.DEMOTED})
    assert cp.state("user_stated") == cp.state("llm:chat") == cp.state("linked_send") == ""
    assert cp.load()["signals"] == {}


def test_a_reopen_of_an_at_once_close_moves_the_signal_at_once(ea):
    conn = _db(ea)
    _add(conn, "t1")
    _policy(ea, {"calendar_event_match": cp.PROMOTED})
    closure.settle(conn, "t1", signal="calendar_event_match", actor="s", note="n")
    row = conn.execute("SELECT * FROM ledger WHERE id = 't1'").fetchone()
    closure.note_reopen(conn, row, actor="cli:cli")
    cp.forget()
    assert cp.state("calendar_event_match") == ""
    (change,) = cp.load()["changes"]
    assert (change["from"], change["to"], change["posted"]) == (cp.PROMOTED, "", False)


# ── 4. a new message on a closed row ──────────────────────────────────

def _close_then_renew(conn, rid, signal, *, received):
    closure.settle(conn, rid, signal=signal, actor="ledger_sync", note="archived")
    row = conn.execute("SELECT * FROM ledger WHERE id = ?", (rid,)).fetchone()
    meta = closure.note_renew(conn, row, received=received)
    conn.execute("UPDATE ledger SET status = 'open', metadata = ? WHERE id = ?",
                 (json.dumps(meta), rid))


def test_a_renewed_row_hears_the_archive_of_its_new_mail(ea):
    """Before note_renew, intake's reopen vetoed the closing signal with no
    clock: the owner's archive of the new mail was refused for good, and he
    had to close the row in words. Now it proposes the close."""
    conn = _db(ea)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _close_then_renew(conn, "response-a", "user_archived", received="2026-09-02T10:00:00Z")
    row = _row(conn, "response-a")
    assert closure.KEPT_OPEN not in row["close_vetoes"] and "user_archived" not in row["close_vetoes"]
    assert closure.renewed_at(row) == "2026-09-02T09:59:59Z"
    res = closure.settle(conn, "response-a", signal="user_archived", actor="ledger_sync",
                         note="archived")
    assert res["action"] == "proposed"
    kinds = [e["kind"] for e in conn.execute(
        "SELECT kind FROM ledger_events WHERE entry_id = 'response-a' ORDER BY seq")]
    assert kinds == ["close", "renew", "propose"]


def test_evidence_from_before_the_new_message_is_refused(ea):
    conn = _db(ea)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _close_then_renew(conn, "response-a", "user_replied_outlook",
                      received="2026-09-02T10:00:00Z")
    old = closure.settle(conn, "response-a", signal="user_replied_outlook", actor="s",
                         note="n", evidence_ts="2026-09-02T09:00:00Z")
    assert old["action"] == "vetoed"
    new = closure.settle(conn, "response-a", signal="user_replied_outlook", actor="s",
                         note="n", evidence_ts="2026-09-02T11:00:00Z")
    assert new["action"] == "proposed"
    assert closure.settle(conn, "response-a", signal="user_stated", actor="s",
                          note="n")["action"] == "closed"


def test_the_report_counts_a_renewal_apart_from_a_false_close(ea):
    conn = _db(ea)
    _add(conn, "response-a", type_="response_needed")
    _add(conn, "response-b", type_="response_needed")
    _close_then_renew(conn, "response-a", "user_archived", received="2026-09-02T10:00:00Z")
    closure.settle(conn, "response-b", signal="user_archived", actor="s", note="n")
    _ev(conn, "response-b", "reopen", T0, signal="user_archived", actor="intake")
    rep = closure.report(conn)
    assert rep["false_closes"] == 0 and rep["renewed_closes"] == 2
    assert rep["signals"]["user_archived"]["renewed"] == 2


def test_rows_renewed_before_note_renew_are_repaired_once(ea):
    conn = _db(ea)
    stamp = "2026-10-23T13:00:07Z"
    _add(conn, "response-a", type_="response_needed",
         meta={"close_vetoes": {closure.KEPT_OPEN: stamp, "user_archived": stamp}})
    _add(conn, "response-b", type_="response_needed",
         meta={"close_vetoes": {closure.KEPT_OPEN: stamp, "user_archived": stamp}})
    for rid in ("response-a", "response-b"):
        _ev(conn, rid, "reopen", datetime(2026, 10, 23, 13, 0, 7, tzinfo=timezone.utc),
            signal="user_archived", actor="intake")
    _ev(conn, "response-b", "reject", T0, signal="user_archived")
    assert closure.repair_renewals(conn) == ["response-a"]
    assert _row(conn, "response-a")["close_vetoes"] == {closure.NEW_ASK: stamp}
    assert closure.KEPT_OPEN in _row(conn, "response-b")["close_vetoes"]
    assert closure.repair_renewals(conn) == []


def test_a_renewal_is_no_gold_reopen_in_the_label_store(ea):
    import closure_labels as cl
    conn = _db(ea)
    for rid in ("response-a", "response-b", "response-c"):
        _add(conn, rid, type_="response_needed")
    _ev(conn, "response-a", "renew", T0, signal="user_archived", actor="intake")
    _ev(conn, "response-b", "reopen", T0, signal="user_archived", actor="intake")
    _ev(conn, "response-c", "reopen", T0, signal="user_archived", actor="cli:chat")
    conn.commit()
    recs = cl.ledger_records(ea / "state" / "obligations-ledger.db", None, ea)
    reopened = [r for r in recs if r["kind"] == "reopened"]
    assert [r["ledger_ref"] for r in reopened] == [cl.ledger_ref("response-c")]


def test_the_scrubber_keeps_no_standing_veto_for_a_renewal():
    import closure_scrubber as cs
    store = cs.VetoStore(path=Path("/nonexistent/vetoes.json"))
    store.record_close("response-a", "user_replied_outlook")
    store.record_close("response-b", "user_replied_outlook")
    closed_at = store.data["closes"]["response-a"]["ts"]
    assert store.note_reopen("response-a", renewed_at="2999-01-01T00:00:00Z") == ""
    assert not store.is_vetoed("response-a", "user_replied_outlook")
    assert store.note_reopen("response-b", renewed_at="") == "user_replied_outlook"
    assert store.is_vetoed("response-b", "user_replied_outlook")
    assert closed_at


def test_a_close_that_sweeps_a_sibling_archives_the_siblings_own_mail(ea, monkeypatch):
    """The sweep marked a sibling archived before any mail moved, so a close
    on a thread that kept an open row never archived the sibling's mail,
    and the owner had to."""
    conn = _db(ea)
    _add(conn, "response-a", type_="response_needed", conv="c1")
    _add(conn, "response-b", type_="response_needed", conv="c1")
    _add(conn, "task-c", conv="c1")
    conn.execute("UPDATE ledger SET source_message_id = 'm-' || id")
    moved = []
    monkeypatch.setattr(ledger_mod, "_archive_email",
                        lambda mid: moved.append(mid) or {"archived": True})
    monkeypatch.setattr(ledger_mod, "_archive_conversation",
                        lambda cid: pytest.fail("the thread keeps an open row"))
    row = conn.execute("SELECT * FROM ledger WHERE id = 'response-a'").fetchone()
    res = closure.settle(conn, "response-a", signal="user_stated", actor="t", note="done")
    assert res["ids"] == ["response-a", "response-b"]
    out = ledger_mod._archive_after_close(conn, row, res["ids"], skip=False)
    assert moved == ["m-response-a", "m-response-b"] and out["conversation_kept"] == ["task-c"]


# ── 5. the nightly run, the CLI, the backtest ─────────────────────────

def test_the_nightly_run_writes_the_policy_and_posts_each_change_once(ea, monkeypatch):
    conn = _db(ea)
    now = datetime.now(timezone.utc)
    _confirms(conn, "llm:reconcile-lapsed", 16, start=now - timedelta(days=3))
    conn.commit()
    posted = []
    monkeypatch.setattr(cp, "_post", lambda lines: posted.append(lines) or True)
    out = cp.update(now=now)
    assert out["signals"] == {"llm:reconcile-lapsed": cp.PROMOTED}
    assert out["renewals_repaired"] == 0 and out["posted"] == 1
    (lines,) = posted
    assert lines == ["Closure policy: llm:reconcile-lapsed now closes at once. You get a note "
                     "with a Reopen button. Why: 16 of 16 proposals right; none kept open in "
                     "the last 10."]
    assert "\u2014" not in lines[0]
    data = cp.load()
    assert data["signals"]["llm:reconcile-lapsed"]["numbers"]["proposing"]["n"] == 16
    assert all(c["posted"] for c in data["changes"]) and data["renewals_repaired"]
    again = cp.update(now=now + timedelta(days=1))
    assert again["changes"] == [] and len(posted) == 1
    assert cp.state("llm:reconcile-lapsed") == cp.PROMOTED


def test_the_nightly_run_does_nothing_with_the_switch_off_and_dry_run_writes_nothing(ea,
                                                                                    monkeypatch):
    _db(ea).close()
    assert cp.update(dry_run=True) == {"decisions": 0, "changes": [], "signals": {}}
    assert not (ea / "state" / cp.POLICY_FILE).exists()
    import ea_config
    (ea / "config" / "ea-config.json").write_text(json.dumps({"closure": {"hill_climb": False}}))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", ea / "config" / "ea-config.json")
    assert cp.update() == {"skipped": "closure.hill_climb is off"}


def _run(ea: Path, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ, EA_DIR=str(ea))
    env.pop("EA_RUN_ID", None)
    return subprocess.run([sys.executable, *map(str, args)], env=env, capture_output=True,
                          text=True, timeout=60)


def test_show_freeze_and_reset(ea):
    r = _run(ea, POLICY, "show")
    assert r.returncode == 0 and "No closure policy yet" in r.stdout
    _policy(ea, {"llm:reconcile": cp.PROMOTED})
    assert "llm:reconcile" in _run(ea, POLICY, "show").stdout
    assert json.loads(_run(ea, POLICY, "freeze").stdout) == {"frozen": True}
    assert "(frozen)" in _run(ea, POLICY, "show").stdout
    assert json.loads(_run(ea, POLICY, "freeze", "--off").stdout) == {"frozen": False}
    assert json.loads(_run(ea, POLICY, "reset").stdout) == {"reset": True}
    data = json.loads(_run(ea, POLICY, "show", "--json").stdout)
    assert data["signals"] == {} and data["changes"][-1]["why"] == "the policy was reset"


def test_the_closure_report_shows_the_policy_column(ea):
    conn = _db(ea)
    _add(conn, "t1")
    closure.settle(conn, "t1", signal="calendar_event_match", actor="s", note="n")
    _add(conn, "t2")
    closure.settle(conn, "t2", signal="user_stated", actor="s", note="n")
    conn.commit()
    _policy(ea, {"calendar_event_match": cp.PROMOTED})
    rep = json.loads(_run(ea, LEDGER, "closure-report", "--days", "1").stdout)
    assert rep["signals"]["calendar_event_match"]["policy"] == cp.PROMOTED
    assert rep["signals"]["user_stated"]["policy"] == "never"
    assert rep["policy"]["enabled"] is True


def test_a_residents_propose_never_closes_and_promoted_noted_is_the_owners(ea):
    conn = _db(ea)
    _add(conn, "t1")
    conn.commit()
    _policy(ea, {"llm:reconcile": cp.PROMOTED})
    out = json.loads(_run(ea, LEDGER, "propose", "--id", "t1", "--signal", "llm:reconcile",
                          "--why", "looks done").stdout)
    assert out["action"] == "proposed"
    r = _run(ea, LEDGER, "promoted", "--noted", "t1")
    assert r.returncode == 1 and "bot's" in r.stdout
    assert json.loads(_run(ea, LEDGER, "promoted").stdout)["count"] == 0


def _dec(sig, track, right, at, entry):
    return cp.Decision(sig, track, right, _iso(at), _iso(at), _iso(at), entry)


def test_the_replay_counts_clicks_saved_and_a_wrong_close_demotes_at_once():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    history = [_dec("llm:reconcile", "proposal", True, start - timedelta(days=20, hours=-i),
                    f"h{i}") for i in range(16)]
    window = [_dec("llm:reconcile", "proposal", True, start + timedelta(days=1), "w1"),
              _dec("llm:reconcile", "proposal", False, start + timedelta(days=2), "w2"),
              _dec("llm:reconcile", "proposal", True, start + timedelta(days=2, hours=2), "w3")]
    rep = cp.replay(history + window, start=start, end=start + timedelta(days=5))
    row = rep["signals"]["llm:reconcile"]
    assert (row["clicks_saved"], row["wrong_closes"], row["decisions"]) == (1, 1, 3)
    assert rep["changes"][0]["to"] == cp.PROMOTED and rep["changes"][1]["to"] == ""
    assert rep["final"] == {}


def test_the_replay_counts_the_cost_and_the_gain_of_a_demotion():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ds = [_dec("user_archived", "decisive", False, start - timedelta(days=3), "a0"),
          _dec("user_archived", "decisive", True, start + timedelta(days=1), "a1"),
          _dec("user_archived", "decisive", False, start + timedelta(days=2), "a2")]
    rep = cp.replay(ds, start=start, end=start + timedelta(days=4))
    row = rep["signals"]["user_archived"]
    assert (row["clicks_added"], row["wrong_closes_avoided"]) == (1, 1)
    text = cp.render_backtest({**rep, "days": 4, "thresholds": cp.thresholds(), "renewals": {}})
    assert "user_archived" in text and "\u2014" not in text
