"""The closure eval (closure_eval.py): precision, recall and F1 for the
thread-state rules, the model and the ledger pipeline on a small
synthetic label set, the model's calibration, the disagreements, the
candidate-rule backtest, the written reports and the daily health line.
Every record here is made up."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import closure_eval as ce  # noqa: E402
import closure_labels as cl  # noqa: E402

NOW = 1_800_000_000.0
H = 3600.0


def tap(n, label, *, rule_state=None, rule_id=None, inter="owner_ask", prior=(None, None),
        at=NOW - 10 * H, **feats):
    return cl.record("owner-tap", "tap", label, key=f"tap{n}", at=at, thread=f"t{n}",
                     channel_kind="main", origin="owner", subject_type="lookup",
                     features={"rule_state": rule_state, "rule_id": rule_id,
                               "interaction_type": inter, "prior_state": prior[0],
                               "prior_by": prior[1], **feats})


def model(n, label, conf, *, kind="judgment", at=NOW - 20 * H, key=""):
    return cl.record("model", kind, label, key=key or f"m{n}{kind}{label}", at=at,
                     thread=f"t{n}", channel_kind="main", origin="owner",
                     features={"confidence": conf, "interaction_type": "owner_ask"})


def led(i, kind, label, how, typ="task", signal="user_archived"):
    return cl.record("ledger", kind, label, key=f"l{i}", at=NOW - 30 * H, ledger=f"r{i}",
                     channel_kind="mail", origin="mail",
                     features={"closed_how": how, "linked_ledger_type": typ, "signal": signal})


def synthetic():
    return [
        # 1: gold done; the rule said done, the model said done
        tap(1, "done", rule_state="done", rule_id="4-card-outcome", card_outcome="acted"),
        model(1, "done", 0.9),
        # 2: gold done; no rule; the model said needs_you, then an unsure done
        tap(2, "done", last_author="owner"),
        model(2, "needs_you", 0.8),
        model(2, "done", 0.4, kind="unsure", at=NOW - 15 * H),
        # 3: gold needs_you; the rule agreed; the model said done
        tap(3, "needs_you", rule_state="needs_you", rule_id="2-live-card", last_author="bot",
            hours_since_last_message=50.0),
        model(3, "done", 0.65),
        # 4: gold needs_you; the rule said done; the model's last state was needs_you
        tap(4, "needs_you", rule_state="done", rule_id="6-ledger-done", inter="room",
            prior=("needs_you", "model")),
        # a model answer after the tap does not count for it
        model(4, "done", 0.99, at=NOW - H),
        # the ledger: 4 rule closes, 1 model, 1 confirmed, 2 by the owner, 1 other
        led(1, "completed", "done", "rule"), led(2, "completed", "done", "rule"),
        led(3, "completed", "done", "rule", typ="response_needed"),
        led(4, "completed", "done", "rule", typ="response_needed", signal="linked_send"),
        led(5, "completed", "done", "model", signal="llm:reconcile"),
        led(6, "completed", "done", "confirmed", signal="user_confirmed"),
        led(7, "completed", "done", "owner", signal="user_stated"),
        led(8, "completed", "done", "owner", signal="user_stated"),
        led(9, "completed", "done", "other", signal=None),
        led(10, "reopened", "open", "rule", typ="response_needed", signal="linked_send"),
        led(11, "rejected", "open", "model", signal="llm:reconcile"),
        led(12, "confirmed", "done", "rule", signal="calendar_event_match"),
    ]


def test_the_rules_against_the_owner_s_taps():
    rep = ce.evaluate(synthetic(), now=NOW)
    r = rep["rules"]
    assert (r["tp"], r["fp"], r["fn"], r["tn"], r["covered"]) == (1, 1, 1, 1, 3)
    assert (r["precision"], r["recall"], r["f1"]) == (0.5, 0.5, 0.5)
    assert r["by_rule_id"]["6-ledger-done"]["fp"] == 1
    assert r["by_rule_id"]["model's"]["fn"] == 1
    assert r["by_interaction_type"]["room"]["fp"] == 1


def test_the_model_against_the_owner_s_taps_and_its_calibration():
    rep = ce.evaluate(synthetic(), now=NOW)
    m = rep["model"]
    # 1 right (done), 2 wrong (needs_you), 3 wrong (done), 4 right (its last state)
    assert (m["tp"], m["fp"], m["fn"], m["tn"], m["covered"]) == (1, 1, 1, 1, 4)
    assert m["f1"] == 0.5 and m["judgments"] == 4 and m["unsure"] == 1
    # the calibration reads the latest answer with a confidence: tap 2's unsure done
    assert m["calibration"] == {"0.0-0.6": {"n": 1, "accuracy": 1.0},
                                "0.6-0.7": {"n": 1, "accuracy": 0.0},
                                "0.9-1.0": {"n": 1, "accuracy": 1.0}}


def test_the_ledger_pipeline():
    rep = ce.evaluate(synthetic(), now=NOW)
    led_ = rep["ledger"]
    # caught 6 (4 rule, 1 model, 1 confirmed), 1 of them reopened; wrong 2; missed 2
    assert (led_["caught"], led_["missed"], led_["other"], led_["reopened"], led_["rejected"]) \
        == (6, 2, 1, 1, 1)
    assert (led_["tp"], led_["fp"], led_["fn"]) == (5, 2, 2)
    assert led_["f1"] == pytest.approx(5 / 7, abs=1e-3)
    rn = led_["by_ledger_type"]["response_needed"]
    assert (rn["caught"], rn["reopened"], rn["close_precision"]) == (2, 1, 0.5)
    assert led_["by_signal"]["llm:reconcile"]["proposal_precision"] == 0.0
    assert led_["by_signal"]["calendar_event_match"]["proposal_precision"] == 1.0
    assert led_["closed_how"] == {"rule": 4, "model": 1, "confirmed": 1, "owner": 2, "other": 1}


def test_disagreements_and_the_candidate_rules():
    rep = ce.evaluate(synthetic(), now=NOW)
    got = {(d["who"], d["rule_id"], d["predicted"], d["gold"]): d["count"]
           for d in rep["disagreements"]}
    assert got == {("rules", "6-ledger-done", "done", "needs_you"): 1,
                   ("model", "model", "needs_you", "done"): 1,
                   ("model", "model", "done", "needs_you"): 1}
    cand = {c["rule"]: c for c in rep["candidates"]}
    assert (cand["card_status_line"]["fires_on_gold"], cand["card_status_line"]["precision"],
            cand["card_status_line"]["recall"]) == (1, 1.0, 0.5)
    # 48 h of silence fires on tap 3, which still needed the owner
    assert (cand["silence_48h"]["fires_on_gold"], cand["silence_48h"]["precision"]) == (1, 0.0)
    assert cand["stuck_turn"]["fires"] == 0 and cand["stuck_turn"]["precision"] is None


def test_counts_window_and_summary_line():
    recs = synthetic()
    rep = ce.evaluate(recs, now=NOW)
    assert rep["labels"] == len(recs) and rep["owner_taps"] == 4
    # gold: 4 taps, the owner's 2 closes, a reopen, a rejection, a confirmation
    assert rep["gold"] == 9
    assert ce.summary_line(rep) == (
        f"Closure labels: {len(recs)} (last 30 days), 9 gold (4 owner taps). "
        "F1: rules 0.50 (n=4), model 0.50 (n=4), ledger 0.71 (n=9).")
    old = [cl.record("owner-tap", "tap", "done", key="old", at=NOW - 40 * 86400, thread="t9",
                     features={})]
    assert ce.evaluate(recs + old, now=NOW)["owner_taps"] == 4
    assert ce.evaluate(recs + old, days=None, now=NOW)["owner_taps"] == 5


def test_a_detector_that_never_says_closed_scores_zero():
    assert ce.prf(0, 0, 3)["f1"] == 0.0 and ce.prf(0, 0, 3)["precision"] is None
    assert ce.prf(0, 0, 0)["f1"] is None


def test_the_report_files_and_the_health_line(tmp_path):
    labels = tmp_path / "evals" / cl.LABELS_FILE
    assert ce.summary_lines(labels) == []
    cl.append(synthetic(), labels)
    rep = ce.write_report(labels=labels, now=NOW)
    md = labels.parent / ce.REPORT_MD
    js = labels.parent / ce.REPORT_JSON
    for f in (md, js):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    text = md.read_text()
    for heading in ("## Thread-state rules", "## Thread-state model", "Calibration",
                    "## Ledger closure pipeline", "## Top disagreements", "## Candidate rules"):
        assert heading in text
    assert ce.summary_line(rep) in text
    (line,) = ce.summary_lines(labels)
    assert line.startswith("Closure labels: ") and "F1: rules" in line


def test_the_daily_health_report_carries_the_line():
    src = (ROOT / "preprocessor" / "mail_privacy.py").read_text()
    assert "closure_eval.summary_lines()" in src


def test_the_cli_prints_one_line(tmp_path, capsys):
    labels = tmp_path / "evals" / cl.LABELS_FILE
    cl.append(synthetic(), labels)
    assert ce.main(["report", "--labels", str(labels), "--days", "0", "--no-write"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("Closure labels: ") and "all time" in out[0]
    assert not (labels.parent / ce.REPORT_MD).exists()
